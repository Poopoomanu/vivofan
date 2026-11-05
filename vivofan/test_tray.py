"""Tests for vivofan-tray's status handling (no GTK needed). Run: python -m unittest test_tray"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location("tray", Path(__file__).with_name("vivofan-tray.py"))
tray = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tray)

EC_STATUS = {
    "time": 1000.0, "fan_control": "ec", "mode": "balanced", "cpu": 65, "gpu": 50,
    "smoothed": 64.2, "on_ac": False, "ec_auto": False,
    "fans": [{"name": "cpu", "rpm": 4100, "percent": 50}, {"name": "gpu", "rpm": 3300, "percent": 40}],
    "override": {"value": "80", "until": 1600.0},
}


class SummarizeTests(unittest.TestCase):
    def test_ec_status(self):
        s = tray.summarize(EC_STATUS, None, now=1000.0)
        self.assertEqual(s["label"], "65 °C · 4100 rpm")
        self.assertIn("CPU 65 °C · GPU 50 °C", s["lines"])
        self.assertIn("CPU fan 4100 rpm · 50%", s["lines"])
        self.assertIn("GPU fan 3300 rpm · 40%", s["lines"])
        self.assertIn("Fan curve: vivofan", s["lines"])
        self.assertIn("Override 80% for 10 more min", s["lines"])
        self.assertIn("On battery", s["lines"])

    def test_modes_status_skips_unknown_gpu_fan(self):
        st = dict(EC_STATUS, fan_control="modes", gpu=None, on_ac=True, override=None,
                  fans=[{"name": "cpu", "rpm": 2500, "percent": None},
                        {"name": "gpu", "rpm": None, "percent": None}])
        s = tray.summarize(st, None, now=1000.0)
        self.assertIn("Mode balanced", s["lines"])
        self.assertFalse(any(line.startswith("GPU fan") for line in s["lines"]))
        self.assertNotIn("On battery", s["lines"])

    def test_service_not_running(self):
        s = tray.summarize(None, {"cpu": 52.0, "rpm": 0, "mode": "quiet"})
        self.assertEqual(s["label"], "52 °C")
        self.assertIn("vivofan service is not running", s["lines"])

    def test_actions_match_fan_control(self):
        self.assertIn(("Fans 100% for 10 min", ["100", "--minutes", "10"]), tray.override_actions("ec"))
        self.assertIn(("Performance for 30 min", ["performance", "--minutes", "30"]),
                      tray.override_actions("modes"))


class LoadTests(unittest.TestCase):
    def test_stale_or_missing_status_is_none(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "status.json")
            self.assertIsNone(tray.load_status(p, now=1000.0))
            p.write_text(json.dumps(EC_STATUS))
            self.assertIsNotNone(tray.load_status(p, now=1010.0))
            self.assertIsNone(tray.load_status(p, now=1100.0))

    def test_sysfs_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel, text in (("sys/class/hwmon/hwmon3/name", "coretemp"),
                              ("sys/class/hwmon/hwmon3/temp1_input", "51000"),
                              ("sys/class/hwmon/hwmon3/temp2_input", "58000"),
                              ("sys/class/hwmon/hwmon7/name", "asus"),
                              ("sys/class/hwmon/hwmon7/fan1_input", "2400"),
                              ("sys/firmware/acpi/platform_profile", "balanced")):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(text + "\n")
            self.assertEqual(tray.sysfs_fallback(root), {"cpu": 58.0, "rpm": 2400, "mode": "balanced"})


class OverheatTests(unittest.TestCase):
    def test_beeps_at_limit_then_repeats(self):
        alarm = tray.Overheat(80, 20)
        self.assertTrue(alarm.check(81, 100))    # first beep
        self.assertFalse(alarm.check(82, 110))   # too soon
        self.assertTrue(alarm.check(82, 120))    # repeat after 20 s

    def test_quiet_below_limit(self):
        alarm = tray.Overheat(80, 20)
        self.assertFalse(alarm.check(79.9, 100))

    def test_keeps_beeping_until_5_degrees_under(self):
        alarm = tray.Overheat(80, 20)
        self.assertTrue(alarm.check(85, 100))
        self.assertTrue(alarm.check(77, 120))    # still above 75: keep going
        self.assertFalse(alarm.check(75, 140))   # cleared
        self.assertFalse(alarm.check(78, 141))   # below the limit and cleared: stays quiet
        self.assertTrue(alarm.check(80, 200))    # new episode beeps at once

    def test_off_when_limit_zero_or_unknown_temp(self):
        self.assertFalse(tray.Overheat(0, 20).check(95, 100))
        self.assertFalse(tray.Overheat(80, 20).check(None, 100))

    def test_load_alarm_settings(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "vivofan.toml"
            p.write_text("alarm_temp = 75.0\nalarm_repeat = 10.0\n")
            self.assertEqual(tray.load_alarm_settings(p), (75.0, 10.0))
            self.assertEqual(tray.load_alarm_settings(Path(d) / "missing.toml"), (80.0, 20.0))


if __name__ == "__main__":
    unittest.main()
