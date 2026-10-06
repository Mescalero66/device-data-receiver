"""Tests for the Modbus TCP client, decoding, poller and settings.  Run: python -m unittest -v"""

import csv
import json
import os
import socket
import tempfile
import threading
import time
import unittest

from modbus_core import (ModbusError, ModbusPoller, ModbusTcpClient, decode, load_settings,
                         ref, save_settings)
from modbus_sim import SimServer, SimSensor


class DecodeTests(unittest.TestCase):
    def test_formats(self):
        regs = [0x00D7, 0xFF38, 0x0001, 0x86A0, 0x41BC, 0x0000]
        self.assertEqual(decode(regs, 0, "uint16"), 215)
        self.assertEqual(decode(regs, 1, "int16"), -200)
        self.assertEqual(decode(regs, 1, "uint16"), 0xFF38)
        self.assertEqual(decode(regs, 2, "uint32"), 100000)
        self.assertEqual(decode([0xFFFF, 0xFFFE], 0, "int32"), -2)
        self.assertEqual(decode(regs, 4, "float32"), 23.5)

    def test_missing(self):
        self.assertIsNone(decode([1, None, 3], 1, "uint16"))
        self.assertIsNone(decode([1, None, 3], 0, "uint32"))
        self.assertIsNone(decode([1, 2], 1, "uint32"))       # second word not polled
        self.assertIsNone(decode([1, 2], -1, "uint16"))
        self.assertIsNone(decode(None, 0, "uint16"))         # failed poll
        self.assertIsNone(decode([0x7FC0, 0], 0, "float32"))  # NaN

    def test_ref(self):
        self.assertEqual(ref("Holding registers", 10), "40011")
        self.assertEqual(ref("Input registers", 0), "30001")
        self.assertEqual(ref("Coils", 5), "00006")
        self.assertEqual(ref("Holding registers", 20000), "420001")


class SimTestCase(unittest.TestCase):
    block_reads = True

    def setUp(self):
        self.server = SimServer(sensor=SimSensor(seed=1), block_reads=self.block_reads).start()
        self.addCleanup(self.server.stop)


class ClientTests(SimTestCase):
    def test_read_registers(self):
        c = ModbusTcpClient("127.0.0.1", self.server.port)
        self.addCleanup(c.close)
        for fc in (3, 4):
            regs = c.read(fc, 10, 22)
            self.assertEqual(len(regs), 22)
            self.assertTrue(150 < regs[0] < 280, regs[0])       # temperature x10
            self.assertEqual(regs[2], 0)                        # sensor 2 not fitted
        self.assertEqual(c.read(1, 10, 3), [1, 1, 0])           # coils: nonzero -> 1

    def test_exceptions(self):
        c = ModbusTcpClient("127.0.0.1", self.server.port)
        self.addCleanup(c.close)
        with self.assertRaises(ModbusError) as cm:
            c.read(3, 0, 5)
        self.assertEqual(cm.exception.code, 2)
        with self.assertRaises(ModbusError) as cm:
            c.read(3, 10, 200)
        self.assertEqual(cm.exception.code, 3)
        self.assertEqual(len(c.read(3, 49, 1)), 1)              # still usable afterwards


def run_poller(port, folder, start=10, count=22, polls=2, host="127.0.0.1"):
    events, done = [], threading.Event()

    def emit(kind, payload):
        events.append((kind, payload))
        if sum(k == "poll" for k, _ in events) >= polls:
            done.set()

    p = ModbusPoller(host, port, 1, "Holding registers", start, count, 0.1, emit, folder)
    p.start()
    done.wait(10)
    p.stop()
    return p, [pl for k, pl in events if k == "poll"], [pl for k, pl in events if k == "log"]


class PollerTests(SimTestCase):
    def test_polls_and_csv(self):
        with tempfile.TemporaryDirectory() as d:
            p, polls, logs = run_poller(self.server.port, d)
            with open(p.csv.path, newline="") as fh:
                rows = list(csv.reader(fh))
            # A different register range must not append under the old header.
            p2, _, _ = run_poller(self.server.port, d, start=30, count=2, polls=1)
            self.assertTrue(p2.csv.path.endswith("_2.csv"), p2.csv.path)
        self.assertGreaterEqual(len(polls), 2)
        self.assertTrue(all(len(v) == 22 for t, v in polls))
        self.assertEqual(rows[0][:3], ["pc_time", "hr10", "hr11"])
        self.assertEqual(len(rows[0]), 23)
        self.assertEqual(len(rows), len(polls) + 1)
        self.assertEqual(p.failed, 0)

    def test_no_device(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()                                               # nothing listens here
        with tempfile.TemporaryDirectory() as d:
            p, polls, logs = run_poller(port, d, polls=2)
            self.assertFalse(os.listdir(d) if os.path.isdir(d) else [])   # nothing logged
        self.assertTrue(all(v is None for t, v in polls))
        errors = [t for lv, t in logs if lv == "error"]
        self.assertEqual(len(errors), 1, errors)                # reported once, not per poll


class SingleReadPollerTests(SimTestCase):
    block_reads = False

    def test_falls_back_to_single_reads(self):
        with tempfile.TemporaryDirectory() as d:
            p, polls, logs = run_poller(self.server.port, d, start=8, count=4)
        values = polls[-1][1]
        self.assertEqual(values[:2], [None, None])              # 8 and 9 are not defined
        self.assertIsNotNone(values[2])
        self.assertTrue(any("one at a time" in t for lv, t in logs))
        self.assertTrue(p.single)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "s.json")
            s = load_settings(path)                             # missing file
            self.assertEqual((s["host"], s["port"], s["start"]), ("192.168.1.23", 502, 10))
            self.assertEqual(len(s["graphs"]), 6)
            self.assertEqual(s["graphs"][0]["register"], 10)
            self.assertIsNone(s["graphs"][5]["register"])
            s["host"], s["interval"] = "10.0.0.5", 2.5
            s["graphs"][4]["register"] = 12
            save_settings(path, s)
            self.assertEqual(load_settings(path), s)
            with open(path, "w") as fh:
                json.dump({"port": True, "interval": 3, "reg_type": "nonsense",
                           "graphs": [{"register": "x", "quantity": "speed", "scale": "big"}]}, fh)
            s = load_settings(path)
            self.assertEqual((s["port"], s["interval"], s["reg_type"]), (502, 3.0, "Holding registers"))
            self.assertEqual(s["graphs"][0], {"register": None, "name": "", "quantity": "Temperature",
                                              "format": "int16", "scale": 1.0})
            with open(path, "w") as fh:
                fh.write("not json")
            self.assertEqual(load_settings(path)["host"], "192.168.1.23")


if __name__ == "__main__":
    unittest.main()
