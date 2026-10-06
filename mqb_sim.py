"""Simulated MQ board, for testing the receiver without hardware.

Follows the timing in INTERFACE.md: a broadcast of six sentences every
`interval` seconds, one sensor read every 25s in a 150s cycle starting at
t_s = 26, temperature refreshed every 50s.
"""

import math
import random
import time

from mqb_protocol import CLEAN_AIR_PPM, CYCLE_S, GAS, PLACEHOLDER_R0, SENSORS, make_sentence

READ_ORDER = ("MQ2", "MQ4", "MQ5", "MQ8", "MQ135", "MQ7")
FIRST_READ = {n: 26 + 25 * i for i, n in enumerate(READ_ORDER)}
R0 = {"MQ2": 0.606521, "MQ4": 5.383251, "MQ5": 4.912730,
      "MQ8": 1.274406, "MQ135": 8.031977, "MQ7": 2.150834}
WARMUP_S = 26 * 3600


class SimBoard:
    def __init__(self, start_uptime=0, saved_baseline=True, fault_rate=0.0,
                 corrupt_rate=0.0, failed_calibration=(), seed=None):
        self.rng = random.Random(seed)
        self.saved_baseline = saved_baseline
        self.fault_rate = fault_rate
        self.corrupt_rate = corrupt_rate
        self.failed_calibration = set(failed_calibration)
        self.level = {n: 0.0 for n in SENSORS}      # log-ratio above clean air
        self.plume = 0.0
        self.reboot(start_uptime)

    def reboot(self, uptime=0):
        self.uptime = uptime
        self.sentences = {n: "%s,,%s,,,," % (n, GAS[n]) for n in SENSORS}
        self.next_read = {}
        for n, first in FIRST_READ.items():
            k = max(0, (uptime - first) // CYCLE_S)
            self.next_read[n] = first + CYCLE_S * k

    def _temperature(self, t):
        t = t - t % 50                              # refreshed every 50s
        c = 22.5 + 0.8 * math.sin(t / 5000.0)
        return round(c * 16) / 16                   # DS18X20: 1/16 C steps

    def _read(self, name, t):
        # Mean-reverting random walk on log(ppm), plus an occasional plume
        # that every sensor sees in proportion to its cross-sensitivity.
        self.level[name] = 0.9 * self.level[name] + self.rng.gauss(0, 0.04)
        ppm = CLEAN_AIR_PPM[name] * math.exp(self.level[name] + self.plume)
        tracking = self.saved_baseline or t >= WARMUP_S
        r0 = R0[name]
        if not tracking and name in self.failed_calibration:
            r0, ppm = PLACEHOLDER_R0, ppm * 0.2
        ppm_text = "" if self.rng.random() < self.fault_rate else "%.2f" % ppm
        return "%s,%d,%s,%s,%.2f,%d,%.6f" % (
            name, t, GAS[name], ppm_text, self._temperature(t), tracking, r0)

    def broadcast(self):
        """Advance to the current uptime and return the six lines to send."""
        if self.rng.random() < 0.01:
            self.plume = self.rng.uniform(0.5, 2.0)
        self.plume *= 0.85
        for n in READ_ORDER:
            while self.next_read[n] <= self.uptime:
                self.sentences[n] = self._read(n, self.next_read[n])
                self.next_read[n] += CYCLE_S
        lines = []
        for n in SENSORS:
            line = make_sentence("PMQB," + self.sentences[n])
            if self.rng.random() < self.corrupt_rate:
                i = self.rng.randrange(1, len(line) - 5)
                line = line[:i] + chr(ord(line[i]) ^ 0x01) + line[i + 1:]
            lines.append(line)
        return lines

    def advance(self, seconds):
        self.uptime += seconds


def stream(board, interval=10, speed=1.0, restart_after=None, stop=None):
    """Yield broadcasts as bytes, in real time scaled by `speed`.

    The first broadcast is sent immediately. `stop` is an optional
    threading.Event to end the stream.
    """
    while stop is None or not stop.is_set():
        if restart_after is not None and board.uptime >= restart_after:
            board.reboot(0)
            restart_after = None
        yield "".join(board.broadcast()).encode("ascii")
        board.advance(interval)
        deadline = time.monotonic() + interval / speed
        while time.monotonic() < deadline:
            if stop is not None and stop.wait(0.05):
                return
            if stop is None:
                time.sleep(0.05)


if __name__ == "__main__":
    # Print a quick sample: a booting board, then 3 minutes of broadcasts.
    b = SimBoard(seed=1)
    for _ in range(19):
        print("".join(b.broadcast()), end="")
        print()
        b.advance(10)
