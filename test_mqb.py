"""Tests against the examples and rules in INTERFACE.md.  Run: python -m unittest -v"""

import csv
import os
import tempfile
import unittest

from mqb_core import CsvLogger, LineFramer
from mqb_protocol import (ParseError, Tracker, checksum, make_sentence, parse_sentence,
                          recompute_ppm)
from mqb_sim import SimBoard

# Every example sentence in INTERFACE.md.
SPEC_EXAMPLES = """
$PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2E
$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78
$PMQB,MQ2,,SMK,,,,*59
$PMQB,MQ4,,CH4,,,,*35
$PMQB,MQ5,,LPG,,,,*50
$PMQB,MQ8,,H2,,,,*7C
$PMQB,MQ135,,NH3,,,,*3C
$PMQB,MQ7,,CO,,,,*05
$PMQB,MQ4,51,CH4,16.32,22.44,0,5.383251*12
$PMQB,MQ5,76,LPG,0.85,22.44,0,4.912730*48
$PMQB,MQ8,101,H2,52.54,22.50,0,1.274406*63
$PMQB,MQ135,126,NH3,4.30,22.50,0,8.031977*16
$PMQB,MQ7,151,CO,0.65,22.50,0,2.150834*21
$PMQB,MQ2,176,SMK,12.81,22.50,0,0.606521*4E
$PMQB,MQ2,86426,SMK,14.02,23.19,1,0.611204*46
$PMQB,MQ5,86326,LPG,0.91,23.13,1,4.928110*7A
$PMQB,MQ8,86351,H2,55.10,23.13,1,1.280533*66
$PMQB,MQ135,86376,NH3,4.62,23.13,1,8.064209*1D
$PMQB,MQ7,86401,CO,0.71,23.19,1,2.163390*22
$PMQB,MQ4,86601,CH4,,23.19,1,5.401877*0C
$PMQB,MQ2,175,SMK,12.97,,0,0.612840*6E
$PMQB,MQ4,51,CH4,2.87,22.44,0,10.000000*13
$PMQB,MQ2,259226,SMK,14.10,23.44,1,0.611204*7B
$PMQB,MQ2,26,SMK,14.08,23.50,1,0.611204*7B
""".split()


def line(body):
    return make_sentence(body)


class ParseTests(unittest.TestCase):
    def test_spec_examples_parse_cleanly(self):
        for text in SPEC_EXAMPLES:
            with self.subTest(text):
                s = parse_sentence(text + "\r\n")
                self.assertIsNotNone(s)
                self.assertEqual(s.warnings, ())

    def test_worked_checksum(self):
        self.assertEqual(checksum("PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521"), 0x78)

    def test_fields(self):
        s = parse_sentence("$PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2E\r\n")
        self.assertEqual((s.name, s.t_s, s.gas, s.ppm, s.temp_c, s.tracking, s.r0),
                         ("MQ4", 86451, "CH4", 17.85, 23.19, True, 5.401877))

    def test_empty_fields_are_null_not_zero(self):
        s = parse_sentence("$PMQB,MQ2,,SMK,,,,*59\r\n")
        self.assertEqual((s.t_s, s.ppm, s.temp_c, s.tracking, s.r0), (None,) * 5)
        self.assertEqual(s.gas, "SMK")

    def test_bad_checksum_rejected(self):
        with self.assertRaisesRegex(ParseError, "checksum"):
            parse_sentence("$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*79\r\n")

    def test_lowercase_checksum_accepted_with_warning(self):
        s = parse_sentence("$PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2e\r\n")
        self.assertIn("checksum hex not uppercase", s.warnings)

    def test_malformed(self):
        for bad in ("PMQB,MQ2,,SMK,,,,*59", "$PMQB,MQ2,,SMK,,,,*5", "$PMQB,MQ2,,SMK,,,,",
                    line("PMQB,MQ2,,SMK,,,"), line("PMQB,MQ2,abc,SMK,,,,"),
                    line("PMQB,MQ2,26,SMK,1.00,22.44,2,0.606521"),
                    line("PMQB,MQ2,26,SMK,12.x,22.44,0,0.606521"),
                    line("PMQB,MQ2,26,SMK,%s,22.44,0,0.606521" % ("1" * 120))):
            with self.subTest(bad):
                self.assertRaises(ParseError, parse_sentence, bad)

    def test_other_address_ignored(self):
        self.assertIsNone(parse_sentence(line("GPGGA,1,2,3")))

    def test_extra_fields_ignored_with_warning(self):
        s = parse_sentence(line("PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521,x"))
        self.assertEqual(s.ppm, 12.97)
        self.assertTrue(any("extra" in w for w in s.warnings))

    def test_nonfinite_ppm_treated_as_empty(self):
        for v in ("inf", "nan", "-inf"):
            s = parse_sentence(line("PMQB,MQ2,26,SMK,%s,22.44,0,0.606521" % v))
            self.assertIsNone(s.ppm)
            self.assertTrue(s.warnings)

    def test_format_and_gas_warnings(self):
        s = parse_sentence(line("PMQB,MQ2,26,CO,12.9,22.44,0,0.6065"))
        self.assertEqual(s.ppm, 12.9)
        self.assertEqual(len(s.warnings), 3)

    def test_recompute_example(self):
        self.assertAlmostEqual(recompute_ppm("MQ4", 17.85, 5.401877, 5.383251), 17.68, places=2)


class TrackerTests(unittest.TestCase):
    def feed(self, tr, text, now):
        return tr.feed(parse_sentence(text), now)

    def test_new_repeat_restart(self):
        tr = Tracker()
        self.assertIsNone(self.feed(tr, "$PMQB,MQ2,,SMK,,,,*59", 1000).reading)
        r = self.feed(tr, "$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78", 1030).reading
        self.assertEqual(r.est_time, 1030)
        self.assertIn("provisional", r.flags)
        self.assertIsNone(self.feed(tr, "$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78", 1040).reading)
        up = self.feed(tr, "$PMQB,MQ2,176,SMK,12.81,22.50,0,0.606521*4E", 1180)
        self.assertEqual(up.reading.sentence.t_s, 176)
        self.assertEqual(up.notes, [])
        self.assertEqual(tr.boot_offset, 1004)
        up = self.feed(tr, "$PMQB,MQ2,26,SMK,14.08,23.50,1,0.611204*7B", 1300)
        self.assertIn("backwards", up.restart)
        self.assertIsNotNone(up.reading)
        self.assertEqual(tr.boot_offset, 1274)

    def test_empty_after_reading_is_restart(self):
        tr = Tracker()
        self.feed(tr, "$PMQB,MQ2,259226,SMK,14.10,23.44,1,0.611204*7B", 5000)
        up = self.feed(tr, "$PMQB,MQ2,,SMK,,,,*59", 5010)
        self.assertIn("empty again", up.restart)
        self.assertIsNone(self.feed(tr, "$PMQB,MQ4,,CH4,,,,*35", 5010).restart)
        self.assertEqual(tr.restarts, 1)

    def test_fault_reading_and_missed_cycles(self):
        tr = Tracker()
        self.feed(tr, "$PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2E", 0)
        up = self.feed(tr, "$PMQB,MQ4,86601,CH4,,23.19,1,5.401877*0C", 150)
        self.assertIn("no_ppm", up.reading.flags)
        up = self.feed(tr, line("PMQB,MQ4,87051,CH4,17.85,23.19,1,5.401877"), 600)
        self.assertEqual(up.notes, ["MQ4 missed 2 reading(s)"])

    def test_unseen_restart_inferred(self):
        tr = Tracker()
        self.feed(tr, line("PMQB,MQ2,100026,SMK,1.00,22.44,1,0.606521"), 1_000_000)
        # link down for 2 days; board rebooted and ran 1.5 days -> t_s larger
        up = self.feed(tr, line("PMQB,MQ2,129626,SMK,1.00,22.44,1,0.606521"), 1_000_000 + 172800)
        self.assertIn("unseen restart", up.restart)

    def test_stale_and_link_down(self):
        tr = Tracker()
        self.feed(tr, "$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78", 0)
        self.feed(tr, "$PMQB,MQ2,26,SMK,12.97,22.44,0,0.606521*78", 310)
        self.assertIn("MQ2", tr.stale_sensors(310))
        self.assertTrue(tr.link_down(341))
        self.assertEqual(tr.stale_sensors(341), [])

    def test_unknown_name_ignored(self):
        tr = Tracker()
        up = self.feed(tr, line("PMQB,MQ9,26,CO,1.00,22.44,0,1.000000"), 0)
        self.assertIsNone(up.reading)
        self.assertEqual(len(up.notes), 1)


class FramerAndSimTests(unittest.TestCase):
    def test_framer_resync_and_split(self):
        fr = LineFramer()
        data = b"\x00garbage\r\n$PMQB,MQ2,,SM$PMQB,MQ2,,SMK,,,,*59\r\n$PMQB,MQ4,,CH"
        out = fr.feed(data) + fr.feed(b"4,,,,*35\r\n" + b"$" + b"x" * 200 + b"\r\n")
        self.assertEqual([e is None for _, e in out], [False, True, True, False])
        self.assertEqual(out[1][0], b"$PMQB,MQ2,,SMK,,,,*59\r\n")
        self.assertIn("longer", out[3][1])

    def test_sim_boot_sequence(self):
        board, tr, fr, readings = SimBoard(seed=3), Tracker(), LineFramer(), []
        for i in range(40):                         # 400 s of broadcasts
            for raw, err in fr.feed("".join(board.broadcast()).encode()):
                self.assertIsNone(err)
                s = parse_sentence(raw.decode())
                self.assertEqual(s.warnings, ())
                up = tr.feed(s, 1000 + i * 10)
                if up.reading:
                    readings.append((s.name, s.t_s))
            board.advance(10)
        self.assertEqual(readings[:7], [("MQ2", 26), ("MQ4", 51), ("MQ5", 76), ("MQ8", 101),
                                        ("MQ135", 126), ("MQ7", 151), ("MQ2", 176)])
        # true boot is 1000; t_s=26 is first broadcast at uptime 30 -> 4s late
        self.assertEqual(tr.boot_offset, 1004)

    def test_sim_corruption_is_caught(self):
        board, fr = SimBoard(seed=5, corrupt_rate=0.2, start_uptime=1000), LineFramer()
        bad = 0
        for _ in range(50):
            for raw, err in fr.feed("".join(board.broadcast()).encode()):
                try:
                    parse_sentence(raw.decode())
                except ParseError:
                    bad += 1
            board.advance(10)
        self.assertGreater(bad, 30)


class CsvTests(unittest.TestCase):
    def test_rows(self):
        tr = Tracker()
        with tempfile.TemporaryDirectory() as d:
            log = CsvLogger(d)
            for text, now in (("$PMQB,MQ4,86451,CH4,17.85,23.19,1,5.401877*2E", 1.7e9),
                              ("$PMQB,MQ4,86601,CH4,,23.19,1,5.401877*0C", 1.7e9 + 150)):
                log.write(tr.feed(parse_sentence(text), now).reading)
            log.close()
            with open(log.daily.path or os.path.join(d, os.listdir(d)[0]), newline="") as fh:
                rows = list(csv.DictReader(fh))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["ppm"], "17.85")
        self.assertEqual(rows[0]["r0_kohm"], "5.401877")
        self.assertEqual(rows[1]["ppm"], "")
        self.assertEqual(rows[1]["flags"], "no_ppm")


if __name__ == "__main__":
    unittest.main()
