"""Receiving and logging: byte framing, serial/simulated sources, CSV logs,
and the worker thread that ties them to the protocol Tracker.
"""

import csv
import os
import threading
import time
from datetime import datetime

import serial
import serial.tools.list_ports

from mqb_protocol import MAX_LINE, SENSORS, ParseError, Sentence, Tracker, parse_sentence

BAUD = 115200
ESPRESSIF_VID = 0x303A


# --- framing --------------------------------------------------------------

class LineFramer:
    """Splits a byte stream into candidate sentences.

    Bytes before a '$' are discarded. A '$' always starts a new sentence
    (no field may contain one), so a line cut short by noise resynchronises
    on the next sentence instead of corrupting it.
    """

    def __init__(self):
        self.buf = None             # None while hunting for '$'
        self.too_long = False

    def feed(self, data):
        """Return a list of (line_bytes, error) pairs; error is None if OK."""
        out = []
        for b in data:
            if b == 0x24:                               # '$'
                if self.buf:
                    out.append((bytes(self.buf), "incomplete sentence (next '$' arrived first)"))
                self.buf = bytearray(b"$")
                self.too_long = False
            elif self.buf is None:
                continue
            elif b == 0x0A:                             # '\n'
                self.buf.append(b)
                if self.too_long:
                    out.append((bytes(self.buf[:MAX_LINE]), "line longer than %d bytes" % MAX_LINE))
                else:
                    out.append((bytes(self.buf), None))
                self.buf = None
            elif len(self.buf) >= MAX_LINE:
                self.too_long = True
            else:
                self.buf.append(b)
        return out


# --- sources ----------------------------------------------------------------

def list_ports():
    """[(device, description)], USB-UART adapters first, Bluetooth last."""
    def rank(p):
        if "bluetooth" in (p.description or "").lower() or "BTHENUM" in (p.hwid or ""):
            return 2
        return 1 if p.vid == ESPRESSIF_VID else 0
    ports = sorted(serial.tools.list_ports.comports(), key=lambda p: (rank(p), p.device))
    result = []
    for p in ports:
        desc = p.description or ""
        if p.vid == ESPRESSIF_VID:
            desc += "  [ESP32 native USB]"
        result.append((p.device, desc))
    return result


class SerialSource:
    """Yields chunks of bytes from a serial port, reconnecting if it drops."""

    def __init__(self, port, on_event):
        self.port = port
        self.on_event = on_event
        self.label = port

    def _open(self):
        ser = serial.Serial()
        ser.port = self.port
        ser.baudrate = BAUD
        ser.bytesize = serial.EIGHTBITS
        ser.parity = serial.PARITY_NONE
        ser.stopbits = serial.STOPBITS_ONE
        ser.timeout = 0.2
        # Deassert DTR/RTS *before* opening. On ESP32 boards these lines are
        # often wired to EN/IO0 for auto-reset, and opening a port with them
        # asserted can reset the board or hold it in the bootloader.
        ser.dtr = False
        ser.rts = False
        ser.open()
        return ser

    def chunks(self, stop):
        ser = None
        failed = False
        while not stop.is_set():
            if ser is None:
                try:
                    ser = self._open()
                    self.on_event("info", "opened %s at %d 8N1" % (self.port, BAUD))
                    failed = False
                except (serial.SerialException, OSError) as e:
                    if not failed:
                        self.on_event("error", "cannot open %s: %s (retrying)" % (self.port, e))
                        failed = True
                    stop.wait(2)
                    continue
            try:
                data = ser.read(ser.in_waiting or 1)
            except (serial.SerialException, OSError) as e:
                self.on_event("error", "%s lost: %s (reconnecting)" % (self.port, e))
                ser.close()
                ser = None
                failed = True
                stop.wait(2)
                continue
            if data:
                yield data
        if ser is not None:
            ser.close()


class SimSource:
    def __init__(self, speed=10.0, **board_args):
        from mqb_sim import SimBoard
        self.restart_after = board_args.pop("restart_after", None)
        self.board = SimBoard(**board_args)
        self.speed = speed
        self.label = "simulator x%g" % speed

    def chunks(self, stop):
        from mqb_sim import stream
        yield from stream(self.board, speed=self.speed,
                          restart_after=self.restart_after, stop=stop)


# --- logging ----------------------------------------------------------------

CSV_FIELDS = ["pc_time", "board_time_est", "sensor", "gas", "t_s", "ppm",
              "temp_c", "tracking", "r0_kohm", "flags"]


TIME_FMT = "%Y-%m-%d %H:%M:%S"


def _fmt_time(epoch):
    return datetime.fromtimestamp(epoch).strftime(TIME_FMT)


def _fmt(v, spec):
    return "" if v is None else spec % v


class DailyFile:
    """An append-mode file per local day, named <prefix>_YYYY-MM-DD<ext>."""

    def __init__(self, folder, prefix, ext, header=None):
        self.folder, self.prefix, self.ext, self.header = folder, prefix, ext, header
        self.day = None
        self.fh = None
        self.path = None

    def file_for(self, epoch):
        day = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
        if day != self.day:
            self.close()
            os.makedirs(self.folder, exist_ok=True)
            self.path = os.path.join(self.folder, "%s_%s%s" % (self.prefix, day, self.ext))
            new = not os.path.exists(self.path) or os.path.getsize(self.path) == 0
            self.fh = open(self.path, "a", newline="", encoding="ascii", errors="replace")
            if new and self.header:
                self.fh.write(self.header)
            self.day = day
        return self.fh

    def close(self):
        if self.fh:
            self.fh.close()
            self.fh = None
            self.day = None


class CsvLogger:
    """One row per new reading (repeats are not logged), flushed per row."""

    def __init__(self, folder):
        self.daily = DailyFile(folder, "mqb", ".csv", ",".join(CSV_FIELDS) + "\r\n")

    @property
    def path(self):
        return self.daily.path

    def write(self, r):
        s = r.sentence
        fh = self.daily.file_for(r.rx_time)
        csv.writer(fh).writerow([
            _fmt_time(r.rx_time), _fmt_time(r.est_time), s.name, s.gas or "",
            s.t_s, _fmt(s.ppm, "%.2f"), _fmt(s.temp_c, "%.2f"),
            "" if s.tracking is None else int(s.tracking), _fmt(s.r0, "%.6f"),
            " ".join(r.flags),
        ])
        fh.flush()

    def close(self):
        self.daily.close()


def read_csv_logs(paths):
    """Load readings back from CSV logs written by CsvLogger, for plotting.

    Rows from all the files are replayed in time order through a Tracker, so
    a reading logged twice (e.g. the program was restarted mid-cycle) counts
    once, and board restarts are found as they were live, even between files.

    Returns (points, restarts, skipped): points maps sensor -> time-ordered
    [(rx_time, ppm, tracking, temp_c)], restarts lists the times of detected board
    restarts and skipped counts rows that could not be read. Raises
    ValueError for a file that is not an MQB CSV log.
    """
    rows, skipped = [], 0
    for path in paths:
        with open(path, newline="", encoding="ascii", errors="replace") as fh:
            reader = csv.DictReader(fh)
            missing = {"pc_time", "sensor", "t_s", "ppm"} - set(reader.fieldnames or ())
            if missing:
                raise ValueError("%s is not an MQB CSV log (no %s column)"
                                 % (os.path.basename(path), ", ".join(sorted(missing))))
            for row in reader:
                try:
                    rx = datetime.strptime(row["pc_time"], TIME_FMT).timestamp()
                    ppm = float(row["ppm"]) if row["ppm"] else None
                    temp = float(row["temp_c"]) if row.get("temp_c") else None
                    tracking = {"1": True, "0": False}.get(row.get("tracking") or "")
                    s = Sentence(row["sensor"], int(row["t_s"]), row.get("gas") or None,
                                 ppm, temp, tracking, None)
                except (TypeError, ValueError):     # short or garbled row
                    skipped += 1
                    continue
                rows.append((rx, s))
    rows.sort(key=lambda r: r[0])
    tracker = Tracker()
    points = {n: [] for n in SENSORS}
    restarts = []
    for rx, s in rows:
        up = tracker.feed(s, rx)
        if up.restart:
            restarts.append(rx)
        if up.reading:
            points[s.name].append((rx, s.ppm, s.tracking, s.temp_c))
    return points, restarts, skipped


class RawLogger:
    """Every received line, verbatim, with receive time and verdict."""

    def __init__(self, folder):
        self.daily = DailyFile(folder, "mqb_raw", ".log")

    def write(self, now, line, verdict):
        fh = self.daily.file_for(now)
        stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        fh.write("%s\t%s\t%s\n" % (stamp, verdict, line.rstrip("\r\n")))
        fh.flush()

    def close(self):
        self.daily.close()


# --- health -------------------------------------------------------------------

class HealthWatch:
    """Turns link-down / stale-sensor state into change messages.

    Polled from the UI thread; it only reads the Tracker.
    """

    def __init__(self, tracker, started):
        self.tracker = tracker
        self.started = started
        self.down = True
        self.stale = set()
        self.warned_silent = False

    def check(self, now):
        msgs = []
        t = self.tracker
        if t.last_rx is None:
            if not self.warned_silent and now - self.started > 30:
                self.warned_silent = True
                msgs.append(("error", "no $PMQB sentences in 30s - check the port, that board TX "
                                      "goes to adapter RX, and that the grounds are connected"))
            return msgs
        down = t.link_down(now)
        if down != self.down:
            self.down = down
            msgs.append(("error", "no data for %ds - board or link down" % (now - t.last_rx))
                        if down else ("info", "receiving data"))
        stale = set(t.stale_sensors(now))
        for n in sorted(stale - self.stale):
            msgs.append(("warning", "%s stale: no new reading for %ds" % (n, now - t.since[n])))
        for n in sorted(self.stale - stale):
            if not down:
                msgs.append(("info", "%s updating again" % n))
        self.stale = stale
        return msgs


# --- worker -------------------------------------------------------------------

class Stats:
    def __init__(self):
        self.lines = 0          # framed lines received
        self.valid = 0          # $PMQB sentences that parsed
        self.rejected = 0       # discarded: bad checksum, malformed, too long
        self.warnings = 0       # parsed, but deviated from the spec
        self.readings = 0       # new readings logged


class Receiver:
    """Runs a source on a background thread and reports through `emit`.

    emit(kind, payload) is called on the worker thread with:
      ("sentence", (Sentence, now))   every valid sentence, repeats included
      ("reading", Reading)            each new reading (also written to CSV)
      ("log", (level, text))          events: restarts, rejects, warnings, I/O
    """

    def __init__(self, source, emit, log_folder, raw_log=False):
        self.source = source
        self.emit = emit
        self.tracker = Tracker()
        self.stats = Stats()
        self.csv = CsvLogger(log_folder)
        self.raw = RawLogger(log_folder) if raw_log else None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self._seen_warnings = {}

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=3)

    def _log(self, level, text):
        self.emit("log", (level, text))

    def _run(self):
        framer = LineFramer()
        self._log("info", "receiving from %s" % self.source.label)
        try:
            for chunk in self.source.chunks(self.stop_event):
                for raw, err in framer.feed(chunk):
                    self._handle(raw, err, time.time())
        except Exception as e:                      # keep the GUI alive
            self._log("error", "receiver stopped: %r" % e)
        finally:
            self.csv.close()
            if self.raw:
                self.raw.close()

    def _handle(self, raw, err, now):
        self.stats.lines += 1
        line = raw.decode("ascii", errors="replace")
        s = None
        if err is None:
            try:
                raw.decode("ascii")
                s = parse_sentence(line)
                verdict = "ok" if s is not None else "other-address"
            except UnicodeDecodeError:
                err = "non-ASCII bytes"
            except ParseError as e:
                err = str(e)
        if err is not None:
            self.stats.rejected += 1
            verdict = "REJECT " + err
            self._log("reject", "%s: %s" % (err, line.strip()))
        if self.raw:
            self.raw.write(now, line, verdict if not (s and s.warnings) else
                           "WARN " + "; ".join(s.warnings))
        if s is None:
            return

        self.stats.valid += 1
        if s.warnings:
            self.stats.warnings += 1
            for w in s.warnings:
                self._warn_once(s.name, w)

        up = self.tracker.feed(s, now)
        if up.restart:
            self._log("restart", "board restart detected: " + up.restart)
        for note in up.notes:
            self._log("notice", note)
        self.emit("sentence", (s, now))
        if up.reading:
            self.stats.readings += 1
            self.csv.write(up.reading)
            self.emit("reading", up.reading)

    def _warn_once(self, name, text):
        # A deviation repeats in every broadcast (~15x per reading), so report
        # each distinct one once, then again only every 100 occurrences.
        key = (name, text.split("'")[0])
        n = self._seen_warnings.get(key, 0)
        self._seen_warnings[key] = n + 1
        if n % 100 == 0:
            more = " (seen %d times)" % (n + 1) if n else ""
            self._log("warning", "%s: %s%s" % (name, text, more))
