"""Modbus TCP polling: a minimal client (read functions 1-4), value decoding,
the polling thread, its CSV log and the saved connection/graph settings.

Standard library only. Register numbers are protocol addresses (0-based),
as in the device documentation; ref() gives the 1-based "40001" style too.
"""

import csv
import json
import math
import os
import socket
import struct
import threading
import time

from mqb_core import DailyFile, _fmt_time

# Register type -> (function code, CSV column prefix, reference digit, max per read)
REG_TYPES = {
    "Holding registers": (3, "hr", 4, 125),
    "Input registers": (4, "ir", 3, 125),
    "Coils": (1, "co", 0, 2000),
    "Discrete inputs": (2, "di", 1, 2000),
}

EXCEPTIONS = {1: "illegal function", 2: "illegal data address", 3: "illegal data value",
              4: "server device failure", 5: "acknowledge", 6: "server device busy",
              10: "gateway path unavailable", 11: "gateway target device failed to respond"}

FORMATS = ("uint16", "int16", "uint32", "int32", "float32")

# What a graph shows -> (unit, default format, default scale)
QUANTITIES = {
    "Temperature": ("\N{DEGREE SIGN}C", "int16", 0.1),
    "Humidity": ("%RH", "uint16", 1.0),
    "ppm": ("ppm", "uint16", 1.0),
    "ppb": ("ppb", "uint16", 1.0),
}

MAX_GRAPHS = 6


class ModbusError(Exception):
    """The device answered with a Modbus exception response."""

    def __init__(self, code):
        self.code = code
        super().__init__("device replied with exception %d (%s)"
                         % (code, EXCEPTIONS.get(code, "unknown")))


def ref(reg_type, address):
    """The conventional 1-based reference, e.g. holding register 10 -> 40011."""
    digit = REG_TYPES[reg_type][2]
    return ("%d%04d" if address < 9999 else "%d%05d") % (digit, address + 1)


class ModbusTcpClient:
    """Reads coils/registers from one Modbus TCP device (functions 1-4).

    Network problems and malformed replies raise OSError (socket.timeout is
    one); a Modbus exception reply raises ModbusError.
    """

    def __init__(self, host, port=502, unit=1, timeout=3.0):
        self.unit = unit
        self.tid = 0
        self.sock = socket.create_connection((host, port), timeout=timeout)

    def read(self, fc, address, count):
        self.tid = (self.tid + 1) & 0xFFFF
        self.sock.sendall(struct.pack(">HHHBBHH", self.tid, 0, 6, self.unit, fc, address, count))
        tid, proto, length, _unit = struct.unpack(">HHHB", self._recv(7))
        if not 2 <= length <= 254:
            raise OSError("bad Modbus reply length %d" % length)
        pdu = self._recv(length - 1)
        # A reply to an earlier, timed-out request would have an older id.
        if tid != self.tid or proto != 0:
            raise OSError("Modbus reply does not match the request")
        if pdu[0] == fc | 0x80:
            raise ModbusError(pdu[1] if len(pdu) > 1 else 0)
        if pdu[0] != fc or len(pdu) < 2 or pdu[1] != len(pdu) - 2:
            raise OSError("malformed Modbus reply")
        data = pdu[2:]
        if fc in (3, 4):
            if len(data) != 2 * count:
                raise OSError("Modbus reply has %d registers, expected %d" % (len(data) // 2, count))
            return list(struct.unpack(">%dH" % count, data))
        if len(data) != (count + 7) // 8:
            raise OSError("Modbus reply has the wrong number of bits")
        return [(data[i // 8] >> (i % 8)) & 1 for i in range(count)]

    def _recv(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise OSError("connection closed by the device")
            buf += chunk
        return buf

    def close(self):
        self.sock.close()


def decode(values, offset, fmt):
    """The value at `offset` in a list of raw 16-bit registers, or None.

    32-bit formats use two registers, high word first (the Modbus norm).
    None if a register is missing/unreadable or a float is not finite.
    """
    words = 1 if fmt.endswith("16") else 2
    if values is None or offset < 0 or offset + words > len(values):
        return None
    w = values[offset:offset + words]
    if None in w:
        return None
    if fmt == "uint16":
        return w[0]
    if fmt == "int16":
        return w[0] - 0x10000 if w[0] & 0x8000 else w[0]
    v = (w[0] << 16) | w[1]
    if fmt == "uint32":
        return v
    if fmt == "int32":
        return v - 0x100000000 if v & 0x80000000 else v
    f = struct.unpack(">f", struct.pack(">I", v))[0]
    return f if math.isfinite(f) else None


# --- logging ----------------------------------------------------------------

class ModbusCsvLogger:
    """One row per successful poll with every register's raw value.

    Raw values (before format and scale) are logged so any register can be
    graphed later, whatever the graph setup was at the time.
    """

    def __init__(self, folder, host, reg_type, start, count):
        prefix = REG_TYPES[reg_type][1]
        header = ["pc_time"] + ["%s%d" % (prefix, a) for a in range(start, start + count)]
        name = "modbus_" + "".join(c if c.isalnum() or c in ".-" else "_" for c in host)
        self.daily = DailyFile(folder, name, ".csv", ",".join(header) + "\r\n")

    @property
    def path(self):
        return self.daily.path

    def write(self, now, values):
        fh = self.daily.file_for(now)
        csv.writer(fh).writerow([_fmt_time(now)] + ["" if v is None else v for v in values])
        fh.flush()

    def close(self):
        self.daily.close()


# --- polling ------------------------------------------------------------------

class ModbusPoller:
    """Polls one block of registers on a background thread.

    emit(kind, payload) is called on the worker thread with:
      ("poll", (now, values))   values: list of raw ints (None = unreadable),
                                or None if the whole poll failed
      ("log", (level, text))    connection changes and errors, each once
    `interval` may be changed while running.
    """

    def __init__(self, host, port, unit, reg_type, start, count, interval, emit, log_folder):
        self.host, self.port, self.unit = host, port, unit
        self.reg_type, self.first, self.count = reg_type, start, count
        self.fc = REG_TYPES[reg_type][0]
        self.interval = interval
        self.emit = emit
        self.csv = ModbusCsvLogger(log_folder, host, reg_type, start, count)
        self.polls = self.failed = 0
        self.last_ok = None
        self.single = False         # device refused block reads; read one at a time
        self._error = None          # last error reported, so each is logged once
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.thread.join(timeout=5)

    def _log(self, level, text):
        self.emit("log", (level, text))

    def _run(self):
        client = None
        last = self.first + self.count - 1
        self._log("info", "polling %s:%d unit %d, %s %d-%d (%s-%s) every %gs" % (
            self.host, self.port, self.unit, self.reg_type.lower(), self.first, last,
            ref(self.reg_type, self.first), ref(self.reg_type, last), self.interval))
        try:
            while not self.stop_event.is_set():
                began = time.monotonic()
                now = time.time()
                values = None
                try:
                    if client is None:
                        client = ModbusTcpClient(self.host, self.port, self.unit)
                        self._log("info", "connected to %s:%d" % (self.host, self.port))
                    values = self._read(client)
                except ModbusError as e:
                    self._fail("error", str(e))
                except OSError as e:
                    if client is not None:
                        client.close()
                        client = None
                    self._fail("error", "%s:%d not responding: %s (retrying)"
                               % (self.host, self.port, str(e) or type(e).__name__))
                self.polls += 1
                if values is not None:
                    if self._error:
                        self._log("info", "%s:%d responding again" % (self.host, self.port))
                        self._error = None
                    self.last_ok = now
                    self.csv.write(now, values)
                else:
                    self.failed += 1
                self.emit("poll", (now, values))
                self.stop_event.wait(max(0.05, self.interval - (time.monotonic() - began)))
        except Exception as e:                      # keep the GUI alive
            self._log("error", "Modbus poller stopped: %r" % e)
        finally:
            if client is not None:
                client.close()
            self.csv.close()

    def _fail(self, level, text):
        if text != self._error:
            self._log(level, text)
            self._error = text

    def _read(self, client):
        if not self.single:
            try:
                return client.read(self.fc, self.first, self.count)
            except ModbusError as e:
                if e.code != 2 or self.count == 1:
                    raise
            # Some devices only answer for addresses they define, so a block
            # spanning unused ones is refused as a whole.
            self.single = True
            self._log("warning", "device refused reading %d registers at once (illegal data "
                                 "address); reading them one at a time" % self.count)
        values = []
        for a in range(self.first, self.first + self.count):
            try:
                values.append(client.read(self.fc, a, 1)[0])
            except ModbusError:
                values.append(None)
        if all(v is None for v in values):
            raise ModbusError(2)
        return values


# --- settings -----------------------------------------------------------------

# Defaults suit the PowerTec Pico environment sensor with one sensor of each
# kind fitted: temperature x10 at 10, humidity at 11, eCO2 ppm at 30, TVOC ppb at 31.
DEFAULT_SETTINGS = {
    "host": "192.168.1.23", "port": 502, "unit": 1, "reg_type": "Holding registers",
    "start": 10, "count": 22, "interval": 5.0, "sim": False,
    "graphs": [
        {"register": 10, "name": "Temp 01", "quantity": "Temperature", "format": "int16", "scale": 0.1},
        {"register": 11, "name": "Humi 01", "quantity": "Humidity", "format": "uint16", "scale": 1.0},
        {"register": 30, "name": "AQS1 eCO2", "quantity": "ppm", "format": "uint16", "scale": 1.0},
        {"register": 31, "name": "AQS1 TVOC", "quantity": "ppb", "format": "uint16", "scale": 1.0},
    ],
}

EMPTY_GRAPH = {"register": None, "name": "", "quantity": "Temperature", "format": "int16", "scale": 0.1}


def load_settings(path):
    """Saved settings merged over the defaults; bad or missing values fall back."""
    s = json.loads(json.dumps(DEFAULT_SETTINGS))
    try:
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        saved = {}
    if isinstance(saved, dict):
        for k, default in DEFAULT_SETTINGS.items():
            v = saved.get(k)
            if k == "graphs" or isinstance(v, bool) != isinstance(default, bool):
                continue
            if isinstance(default, float) and isinstance(v, (int, float)):
                s[k] = float(v)
            elif type(v) is type(default):
                s[k] = v
        if isinstance(saved.get("graphs"), list):
            s["graphs"] = [g for g in saved["graphs"] if isinstance(g, dict)]
    if s["reg_type"] not in REG_TYPES:
        s["reg_type"] = DEFAULT_SETTINGS["reg_type"]
    graphs = []
    for g in (s["graphs"] + [{}] * MAX_GRAPHS)[:MAX_GRAPHS]:
        g = dict(EMPTY_GRAPH, **g)
        if g["quantity"] not in QUANTITIES:
            g["quantity"] = EMPTY_GRAPH["quantity"]
        if g["format"] not in FORMATS:
            g["format"] = QUANTITIES[g["quantity"]][1]
        if not isinstance(g["register"], int):
            g["register"] = None
        if not isinstance(g["scale"], (int, float)) or not math.isfinite(g["scale"]):
            g["scale"] = 1.0
        graphs.append(g)
    s["graphs"] = graphs
    return s


def save_settings(path, settings):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(settings, fh, indent=2)
    os.replace(tmp, path)
