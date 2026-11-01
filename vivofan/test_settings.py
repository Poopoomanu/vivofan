"""Tests for vivofan-settings' curve editing and config writing (no GTK needed).

Run: python -m unittest test_settings
"""

import importlib.util
import tempfile
import tomllib
import unittest
from pathlib import Path

import vivofan

spec = importlib.util.spec_from_file_location("settings", Path(__file__).with_name("vivofan-settings.py"))
settings = importlib.util.module_from_spec(spec)
spec.loader.exec_module(settings)

CURVE = [[50, 30], [60, 40], [70, 60], [80, 80], [88, 100]]


class MovePointTests(unittest.TestCase):
    def test_free_move(self):
        self.assertEqual(settings.move_point(CURVE, 2, 66.4, 55.6)[2], [66, 56])

    def test_stays_between_neighbours(self):
        moved = settings.move_point(CURVE, 2, 95, 99)  # past the next point
        self.assertEqual(moved[2], [79, 80])
        moved = settings.move_point(CURVE, 2, 10, 5)   # before the previous one
        self.assertEqual(moved[2], [61, 40])

    def test_never_below_30_or_above_100(self):
        self.assertEqual(settings.move_point(CURVE, 0, 20, 0)[0], [30, 30])
        self.assertEqual(settings.move_point(CURVE, 4, 120, 150)[4], [100, 100])

    def test_original_untouched(self):
        before = [list(p) for p in CURVE]
        settings.move_point(CURVE, 1, 65, 50)
        self.assertEqual(CURVE, before)

    def test_any_drag_keeps_the_curve_valid(self):
        curve = [list(p) for p in CURVE]
        for i, (t, p) in enumerate([(10, 0), (200, 200), (55, 90), (45, 10), (90, 35)] * 3):
            curve = settings.move_point(curve, i % len(curve), t, p)
            vivofan.validate_curve("fan_curve", curve)


class PresetTests(unittest.TestCase):
    def test_presets_are_valid_curves(self):
        for name, curve in settings.PRESETS.items():
            with self.subTest(name=name):
                vivofan.validate_curve(name, curve)

    def test_balanced_is_the_default(self):
        self.assertEqual(settings.PRESETS["Balanced"], vivofan.Config().fan_curve)


class ConfigTextTests(unittest.TestCase):
    TEXT = ('interval = 2.0            # how often to sample\n'
            'fan_control = "modes"\n'
            'fan_curve = [[50, 30], [88, 100]]  # keep me\n')

    def test_replaces_values_and_keeps_comments(self):
        out = settings.update_config_text(self.TEXT, {
            "fan_control": "ec", "fan_curve": [[45, 35], [80, 100]]})
        self.assertIn('fan_control = "ec"\n', out)
        self.assertIn("fan_curve = [[45, 35], [80, 100]]  # keep me\n", out)
        self.assertIn("# how often to sample", out)

    def test_appends_missing_keys(self):
        out = settings.update_config_text(self.TEXT, {"gpu_fan_curve": [], "battery_max_percent": 70})
        data = tomllib.loads(out)
        self.assertEqual((data["gpu_fan_curve"], data["battery_max_percent"]), ([], 70))

    def test_result_loads_in_vivofan(self):
        out = settings.update_config_text(Path(__file__).with_name("vivofan.toml").read_text(), {
            "fan_control": "ec", "fan_curve": settings.PRESETS["Cool"],
            "gpu_fan_curve": settings.PRESETS["Silent"], "battery_max_percent": 50})
        with tempfile.NamedTemporaryFile("w", suffix=".toml") as f:
            f.write(out)
            f.flush()
            cfg = vivofan.load_config(f.name)
        self.assertEqual((cfg.fan_control, cfg.fan_curve, cfg.gpu_fan_curve, cfg.battery_max_percent),
                         ("ec", settings.PRESETS["Cool"], settings.PRESETS["Silent"], 50))

    def test_load_settings_defaults(self):
        got = settings.load_settings("/nonexistent/vivofan.toml")
        self.assertEqual(got["fan_control"], "modes")
        self.assertEqual(got["fan_curve"], settings.PRESETS["Balanced"])


if __name__ == "__main__":
    unittest.main()
