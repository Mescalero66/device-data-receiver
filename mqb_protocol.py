"""Parsing, validation and reading tracking for $PMQB sentences.

Pure logic with no I/O, so it can be unit-tested. See INTERFACE.md for the
protocol this implements.
"""

import math
import re
from dataclasses import dataclass, field

ADDRESS = "PMQB"
MAX_LINE = 128                  # bytes, including the CR LF terminator
CYCLE_S = 150                   # each sensor is read once per cycle
STALE_AFTER_S = 300             # two cycles without a new t_s
LINK_DOWN_AFTER_S = 30          # no sentences at all
HIDDEN_RESTART_S = 600          # see Tracker.feed
PLACEHOLDER_R0 = 10.0

# Broadcast order. Receivers identify sentences by name, never by position.
SENSORS = ("MQ2", "MQ4", "MQ5", "MQ8", "MQ135", "MQ7")

GAS = {"MQ2": "SMK", "MQ4": "CH4", "MQ5": "LPG",
       "MQ7": "CO", "MQ8": "H2", "MQ135": "NH3"}

GAS_NAME = {"SMK": "smoke", "CH4": "methane", "LPG": "LPG",
            "CO": "carbon monoxide", "H2": "hydrogen", "NH3": "ammonia"}

CLEAN_AIR_PPM = {"MQ2": 13, "MQ4": 16, "MQ5": 0.85,
                 "MQ7": 0.65, "MQ8": 53, "MQ135": 4.3}

CURVE_B = {"MQ2": -2.440, "MQ4": -2.786, "MQ5": -2.431,
           "MQ7": -1.518, "MQ8": -0.688, "MQ135": -2.473}

_HEX = set("0123456789ABCDEFabcdef")
_INT = re.compile(r"\d+$")
_NUM = re.compile(r"[-+]?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?$")
_NONFINITE = {"inf", "infinity", "nan"}

# Exact formats the spec promises; anything else parses but is warned about.
_FORMAT = {
    "ppm": (re.compile(r"\d+\.\d{2}$"), "2 decimals"),
    "temp_c": (re.compile(r"-?\d+\.\d{2}$"), "2 decimals"),
    "r0": (re.compile(r"\d+\.\d{6}$"), "6 decimals"),
}


class ParseError(ValueError):
    """The line is not a usable sentence; the message says why."""


def checksum(body):
    """NMEA checksum: XOR of every character between '$' and '*'."""
    cs = 0
    for c in body:
        cs ^= ord(c)
    return cs


def make_sentence(body):
    """Frame a sentence body as a complete line, terminator included."""
    return "$%s*%02X\r\n" % (body, checksum(body))


def recompute_ppm(name, ppm, r0, r0_new):
    """ppm recomputed against a different R0 (Receiver Guidance)."""
    return ppm * (r0 / r0_new) ** CURVE_B[name]


@dataclass(frozen=True)
class Sentence:
    name: str
    t_s: int | None
    gas: str | None
    ppm: float | None
    temp_c: float | None
    tracking: bool | None
    r0: float | None
    warnings: tuple = ()        # spec deviations that did not stop parsing


def _decimal(text, label, warnings):
    if text == "":
        return None
    if _NUM.match(text):
        value = float(text)
        if math.isfinite(value):
            pattern, desc = _FORMAT[label]
            if not pattern.match(text):
                warnings.append("%s %r not in spec format (%s)" % (label, text, desc))
            return value
    elif text.lower().lstrip("+-") not in _NONFINITE:
        raise ParseError("%s is not a number: %r" % (label, text))
    warnings.append("%s %r is not a finite number, treated as empty" % (label, text))
    return None


def parse_sentence(line):
    """Parse one line (terminator optional).

    Returns a Sentence, or None for a valid NMEA line with a different
    address (which receivers ignore). Raises ParseError for anything corrupt.
    """
    warnings = []
    if line.endswith("\r\n"):
        line = line[:-2]
    elif line.endswith("\n") or line.endswith("\r"):
        line = line[:-1]
        warnings.append("line not terminated by CR LF")
    else:
        warnings.append("line not terminated by CR LF")

    if len(line) + 2 > MAX_LINE:
        raise ParseError("line longer than %d bytes" % MAX_LINE)
    if not line.startswith("$"):
        raise ParseError("does not start with '$'")
    if len(line) < 4 or line[-3] != "*" or not set(line[-2:]) <= _HEX:
        raise ParseError("no '*HH' checksum at end")

    body, cs_text = line[1:-3], line[-2:]
    calc = checksum(body)
    if calc != int(cs_text, 16):
        raise ParseError("checksum mismatch: sent %s, calculated %02X" % (cs_text, calc))
    if cs_text != cs_text.upper():
        warnings.append("checksum hex not uppercase")

    f = body.split(",")
    if f[0] != ADDRESS:
        return None
    if len(f) < 8:
        raise ParseError("expected 7 fields, got %d" % (len(f) - 1))
    if len(f) > 8:
        warnings.append("%d extra trailing field(s) ignored" % (len(f) - 8))

    name, t_text, gas, ppm_text, temp_text, trk_text, r0_text = f[1:8]

    if t_text == "":
        t_s = None
    elif _INT.match(t_text):
        t_s = int(t_text)
    else:
        raise ParseError("t_s is not an integer: %r" % t_text)

    if trk_text not in ("", "0", "1"):
        raise ParseError("tracking is not 0 or 1: %r" % trk_text)
    tracking = None if trk_text == "" else trk_text == "1"

    ppm = _decimal(ppm_text, "ppm", warnings)
    temp_c = _decimal(temp_text, "temp_c", warnings)
    r0 = _decimal(r0_text, "r0", warnings)

    if name == "":
        warnings.append("name is empty")
    if gas == "":
        warnings.append("gas is empty")
    elif name in GAS and gas != GAS[name]:
        warnings.append("gas %r, expected %r for %s" % (gas, GAS[name], name))
    if t_s is None and (ppm_text or temp_text or trk_text or r0_text):
        warnings.append("t_s empty but other reading fields present")
    if t_s is not None and (trk_text == "" or r0_text == ""):
        warnings.append("t_s present but tracking or r0 empty")

    return Sentence(name, t_s, gas or None, ppm, temp_c, tracking, r0,
                    tuple(warnings))


def reading_flags(s):
    """Data-quality notes for a reading, as short machine-friendly words."""
    flags = []
    if s.ppm is None:
        flags.append("no_ppm")
    if s.temp_c is None:
        flags.append("uncompensated")
    if s.tracking is False:
        flags.append("provisional")
        if s.r0 == PLACEHOLDER_R0:
            flags.append("placeholder_r0")
    if s.temp_c is not None and not 0 <= s.temp_c <= 50:
        flags.append("temp_out_of_range")
    return flags


@dataclass
class Reading:
    """A new reading, i.e. a sentence whose t_s had not been seen before."""
    sentence: Sentence
    rx_time: float              # receiver clock (epoch s) when first seen
    est_time: float             # estimated time the board took the reading
    flags: list = field(default_factory=list)

    @property
    def name(self):
        return self.sentence.name


@dataclass
class Update:
    reading: Reading | None = None
    restart: str | None = None  # why a board restart was detected
    notes: list = field(default_factory=list)


class Tracker:
    """Turns the repeating broadcast into a stream of new readings.

    Implements the "Detecting New Readings", "Detecting Stale Data" and
    "Timestamps" receiver guidance.
    """

    def __init__(self):
        self.restarts = 0
        self.latest = {}            # name -> Sentence, repeats included
        self.last_rx = None         # time of the last valid sentence
        self._unknown = set()
        self._reset(None)

    def _reset(self, now):
        self.last_t = {}            # name -> last t_s seen
        self.since = {n: now for n in SENSORS}   # name -> when t_s last changed
        self.boot_offset = None     # estimated boot time (epoch s)

    def feed(self, s, now):
        up = Update()
        self.last_rx = now
        if s.name not in GAS:
            if s.name not in self._unknown:
                self._unknown.add(s.name)
                up.notes.append("ignoring unknown sensor name %r" % s.name)
            return up
        self.latest[s.name] = s
        if self.since[s.name] is None:
            self.since[s.name] = now

        prev = self.last_t.get(s.name)
        if s.t_s is None:
            if prev is not None:
                self._restart(up, now, "%s is empty again (was t_s=%d)" % (s.name, prev))
            return up
        if prev is not None:
            if s.t_s == prev:
                return up
            if s.t_s < prev:
                self._restart(up, now, "%s t_s went backwards, %d -> %d" % (s.name, prev, s.t_s))
            else:
                step = s.t_s - prev
                if step % CYCLE_S:
                    up.notes.append("%s t_s advanced by %d, expected a multiple of %d"
                                    % (s.name, step, CYCLE_S))
                elif step > CYCLE_S:
                    up.notes.append("%s missed %d reading(s)" % (s.name, step // CYCLE_S - 1))

        # receive_time - t_s is never earlier than the boot time, and a fresh
        # reading lands within one broadcast interval of it, so the minimum is
        # the best boot-time estimate. A value far *later* than the estimate
        # means the board rebooted unseen (e.g. while the link was down) and
        # its uptime has since passed the old t_s, so it did not go backwards.
        offset = now - s.t_s
        if (self.boot_offset is not None and up.restart is None
                and offset - self.boot_offset > HIDDEN_RESTART_S):
            self._restart(up, now, "%s timing implies an unseen restart (boot ~%ds later than estimated)"
                          % (s.name, offset - self.boot_offset))
        if self.boot_offset is None or offset < self.boot_offset:
            self.boot_offset = offset

        self.last_t[s.name] = s.t_s
        self.since[s.name] = now
        up.reading = Reading(s, now, self.boot_offset + s.t_s, reading_flags(s))
        return up

    def _restart(self, up, now, why):
        self.restarts += 1
        up.restart = why
        self._reset(now)

    def stale_sensors(self, now):
        """Sensors whose t_s has not changed for two cycles while data flows."""
        if self.link_down(now):
            return []
        return [n for n, t in self.since.items()
                if t is not None and now - t > STALE_AFTER_S]

    def link_down(self, now):
        return self.last_rx is None or now - self.last_rx > LINK_DOWN_AFTER_S
