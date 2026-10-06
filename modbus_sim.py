"""Simulated Modbus TCP environment sensor, for testing without hardware.

Follows the PowerTec Pico environment sensor register map: addresses 10-49,
temperature (deg C x10) and humidity alternating at 10-25, eCO2 (ppm) and
TVOC (ppb) alternating at 30-45. Only sensor 1 of each kind is fitted
(10, 11, 30, 31); the other registers read 0. Every register type returns
the same values; addresses outside 10-49 get an illegal data address reply.

    python modbus_sim.py            serve on 127.0.0.1:5020 until Ctrl+C
"""

import math
import random
import socketserver
import struct
import threading
import time

FIRST, LAST = 10, 49


class SimSensor:
    def __init__(self, seed=None):
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.co2_plume = 0.0

    def registers(self, now=None):
        """Raw 16-bit values for addresses FIRST..LAST at time `now`."""
        t = time.time() if now is None else now
        with self.lock:
            if self.rng.random() < 0.02:
                self.co2_plume = self.rng.uniform(200, 800)
            self.co2_plume *= 0.9
            temp = 21.5 + 1.5 * math.sin(t / 600) + self.rng.gauss(0, 0.05)
            humi = 45 + 5 * math.sin(t / 900) + self.rng.gauss(0, 0.3)
            co2 = 420 + 30 * math.sin(t / 300) + self.co2_plume + self.rng.gauss(0, 5)
            tvoc = 25 + self.co2_plume / 20 + abs(self.rng.gauss(0, 3))
        regs = dict.fromkeys(range(FIRST, LAST + 1), 0)
        regs[10] = round(temp * 10) & 0xFFFF            # int16, two's complement if < 0
        regs[11] = round(humi)
        regs[30] = round(co2)
        regs[31] = round(tvoc)
        return regs


class _Handler(socketserver.BaseRequestHandler):
    def handle(self):
        sock = self.request
        while True:
            head = _recv(sock, 7)
            if head is None:
                return
            tid, proto, length, unit = struct.unpack(">HHHB", head)
            pdu = _recv(sock, length - 1) if length > 1 else None
            if pdu is None:
                return
            reply = self.server.respond(pdu)
            sock.sendall(struct.pack(">HHHB", tid, proto, len(reply) + 1, unit) + reply)


def _recv(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
    return buf


class SimServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, host="127.0.0.1", port=0, sensor=None, block_reads=True):
        """block_reads=False refuses reads of more than one register, like
        devices that only answer for addresses they define individually."""
        self.sensor = sensor or SimSensor()
        self.block_reads = block_reads
        super().__init__((host, port), _Handler)
        self.thread = threading.Thread(target=self.serve_forever, daemon=True)

    @property
    def port(self):
        return self.server_address[1]

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.shutdown()
        self.server_close()

    def respond(self, pdu):
        fc = pdu[0]
        if fc not in (1, 2, 3, 4) or len(pdu) != 5:
            return bytes([fc | 0x80, 1])
        address, count = struct.unpack(">HH", pdu[1:5])
        limit = 125 if fc in (3, 4) else 2000
        if not 1 <= count <= limit:
            return bytes([fc | 0x80, 3])
        if address < FIRST or address + count - 1 > LAST or (count > 1 and not self.block_reads):
            return bytes([fc | 0x80, 2])
        regs = self.sensor.registers()
        values = [regs[a] for a in range(address, address + count)]
        if fc in (3, 4):
            data = struct.pack(">%dH" % count, *values)
        else:
            bits = [1 if v else 0 for v in values]
            data = bytes(sum(b << i for i, b in enumerate(bits[j:j + 8])) for j in range(0, count, 8))
        return bytes([fc, len(data)]) + data


if __name__ == "__main__":
    server = SimServer(port=5020)
    print("Simulated environment sensor on 127.0.0.1:%d (Ctrl+C to stop)" % server.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    server.server_close()
