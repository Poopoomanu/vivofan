"""Tests for vivofan against a fake sysfs tree. Run: python -m unittest test_vivofan"""

import json
import os
import socket
import tempfile
import time
import unittest
from pathlib import Path

import vivofan
from vivofan import (Backend, Config, Daemon, Epp, FanEc, ProbeIO, Sensors, curve_percent, decide,
                     probe, read_override, sd_notify, set_override)


class FakeSys:
    """A minimal /sys shaped like the K6502VJ under Linux >= 6.12."""

    def __init__(self, profile=True, choices="quiet balanced performance"):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.write("sys/class/dmi/id/board_name", "K6502VJQ")
        self.write("sys/class/hwmon/hwmon3/name", "coretemp")
        self.write("sys/class/hwmon/hwmon5/name", "asus")
        self.write("sys/class/hwmon/hwmon5/fan1_input", "3100")
        self.write("sys/class/power_supply/AC0/type", "Mains")
        self.write("sys/class/power_supply/AC0/online", "1")
        self.write("sys/bus/pci/devices/0000-01-00.0/vendor", "0x10de")
        self.write("sys/bus/pci/devices/0000-01-00.0/class", "0x030000")
        self.write("sys/bus/pci/devices/0000-01-00.0/power/runtime_status", "suspended")
        for n in (0, 1):
            self.write(f"sys/devices/system/cpu/cpufreq/policy{n}/energy_performance_preference",
                       "balance_performance")
            self.write(f"sys/devices/system/cpu/cpufreq/policy{n}/energy_performance_available_preferences",
                       "default performance balance_performance balance_power power")
        if profile:
            self.write("sys/firmware/acpi/platform_profile", "balanced")
            self.write("sys/firmware/acpi/platform_profile_choices", choices)
        else:
            self.write("sys/devices/platform/asus-nb-wmi/throttle_thermal_policy", "0")
        self.cpu(45)

    def write(self, rel, text):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n")

    def read(self, rel):
        return (self.root / rel).read_text().strip()

    def cpu(self, *temps):
        for i, t in enumerate(temps, 1):
            self.write(f"sys/class/hwmon/hwmon3/temp{i}_input", str(int(t * 1000)))

    def profile(self):
        return self.read("sys/firmware/acpi/platform_profile")

    def epp(self):
        return {self.read(f"sys/devices/system/cpu/cpufreq/policy{n}/energy_performance_preference")
                for n in (0, 1)}

    def cleanup(self):
        self._tmp.cleanup()


def make_daemon(fs, dry_run=False, fan=None, **overrides):
    cfg = Config(smoothing=1.0, **overrides)  # no smoothing: each step sees the raw temp
    cfg.validate()
    sensors = Sensors(fs.root, cfg.use_gpu)
    sensors.nvidia_smi = None
    return Daemon(cfg, sensors, Backend(fs.root, dry_run), Epp(cfg, fs.root, dry_run), fan)


class FakeEc:
    """The K6502VJ EC's healthy table as seen through 0x25C/0x25D."""

    def __init__(self):
        self.mode = 0            # 0x31, global
        self.index = 0           # 0x32
        self.duty = [None, None] # 0x35, write-only on the real EC
        self.tach = [2500, 2400]
        self.writes = []
        self.sticky = False      # refuse to leave manual mode

    def read(self, reg):
        if reg == 0x30:
            return 2
        if reg == 0x31:
            return self.mode
        if reg == 0x33:
            return self.tach[self.index] & 0xFF
        if reg == 0x34:
            return self.tach[self.index] >> 8
        raise OSError(f"EC read {reg:#x} timed out")

    def write(self, reg, value):
        self.writes.append((reg, value))
        if reg == 0x31 and not (self.sticky and value == 0):
            self.mode = value
        elif reg == 0x32:
            self.index = value
        elif reg == 0x35:
            self.duty[self.index] = value


class DecideTests(unittest.TestCase):
    cfg = Config()

    def test_table(self):
        cases = [
            (40, "quiet", "quiet"),
            (55, "quiet", "balanced"),
            (85, "quiet", "performance"),
            (52, "balanced", "balanced"),   # inside hysteresis band
            (48, "balanced", "quiet"),
            (64, "balanced", "balanced"),
            (65, "balanced", "performance"),
            (60, "performance", "performance"),
            (58, "performance", "balanced"),
            (30, "performance", "quiet"),
        ]
        for temp, current, want in cases:
            with self.subTest(temp=temp, current=current):
                self.assertEqual(decide(temp, current, self.cfg), want)

    def test_validate_rejects_overlapping_thresholds(self):
        with self.assertRaises(ValueError):
            Config(quiet_below=70, performance_above=72, hysteresis=6).validate()


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()

    def tearDown(self):
        self.fs.cleanup()

    def test_platform_profile_roundtrip(self):
        b = Backend(self.fs.root)
        b.write("quiet")
        self.assertEqual(self.fs.profile(), "quiet")
        self.assertEqual(b.read(), "quiet")

    def test_low_power_alias(self):
        fs = FakeSys(choices="low-power balanced performance")
        try:
            b = Backend(fs.root)
            b.write("quiet")
            self.assertEqual(fs.profile(), "low-power")
            self.assertEqual(b.read(), "quiet")
        finally:
            fs.cleanup()

    def test_policy_fallback_uses_vivo_values(self):
        fs = FakeSys(profile=False)
        try:
            b = Backend(fs.root)
            b.write("quiet")
            self.assertEqual(fs.read("sys/devices/platform/asus-nb-wmi/throttle_thermal_policy"), "2")
            b.write("performance")
            self.assertEqual(b.read(), "performance")
        finally:
            fs.cleanup()

    def test_missing_interface(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(RuntimeError):
                Backend(d)

    def test_dry_run_does_not_write(self):
        Backend(self.fs.root, dry_run=True).write("performance")
        self.assertEqual(self.fs.profile(), "balanced")

    def test_dry_run_reads_back_simulated_mode(self):
        b = Backend(self.fs.root, dry_run=True)
        b.write("performance")
        self.assertEqual(b.read(), "performance")
        self.fs.write("sys/firmware/acpi/platform_profile", "quiet")  # real change wins
        self.assertEqual(b.read(), "quiet")


class EppTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()

    def tearDown(self):
        self.fs.cleanup()

    def test_writes_every_policy(self):
        Epp(Config(), self.fs.root).write("quiet")
        self.assertEqual(self.fs.epp(), {"balance_power"})

    def test_empty_setting_leaves_it_alone(self):
        Epp(Config(epp_quiet=""), self.fs.root).write("quiet")
        self.assertEqual(self.fs.epp(), {"balance_performance"})

    def test_rejects_unsupported_choice(self):
        with self.assertRaises(ValueError):
            Epp(Config(epp_quiet="turbo"), self.fs.root)

    def test_dry_run_does_not_write(self):
        Epp(Config(), self.fs.root, dry_run=True).write("quiet")
        self.assertEqual(self.fs.epp(), {"balance_performance"})

    def test_no_cpufreq_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as d:
            Epp(Config(epp_quiet="turbo"), d).write("quiet")  # nothing to validate against


class SensorTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()

    def tearDown(self):
        self.fs.cleanup()

    def test_cpu_takes_hottest_core(self):
        self.fs.cpu(60, 71, 65)
        self.assertEqual(Sensors(self.fs.root).cpu_temp(), 71)

    def test_thermal_zone_fallback(self):
        for f in (self.fs.root / "sys/class/hwmon/hwmon3").glob("temp*"):
            f.unlink()
        self.fs.write("sys/class/thermal/thermal_zone4/type", "x86_pkg_temp")
        self.fs.write("sys/class/thermal/thermal_zone4/temp", "66000")
        self.assertEqual(Sensors(self.fs.root).cpu_temp(), 66)

    def test_suspended_gpu_is_not_queried(self):
        s = Sensors(self.fs.root)
        s.nvidia_smi = "/should/not/run"
        self.assertIsNone(s.gpu_temp())

    def test_fan_and_ac(self):
        s = Sensors(self.fs.root)
        self.assertEqual(s.fan_rpm(), 3100)
        self.assertTrue(s.on_ac())
        self.fs.write("sys/class/power_supply/AC0/online", "0")
        self.assertFalse(s.on_ac())


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()

    def tearDown(self):
        self.fs.cleanup()

    def test_steps_up_immediately_and_down_after_dwell(self):
        d = make_daemon(self.fs)
        self.fs.cpu(70)
        d.step(0)
        self.assertEqual(self.fs.profile(), "performance")
        self.fs.cpu(50)
        d.step(2)
        self.assertEqual(self.fs.profile(), "performance")  # dwell not over
        d.step(31)
        self.assertEqual(self.fs.profile(), "balanced")

    def test_battery_caps_mode(self):
        self.fs.write("sys/class/power_supply/AC0/online", "0")
        d = make_daemon(self.fs)
        self.fs.cpu(70)
        d.step(0)
        self.assertEqual(self.fs.profile(), "balanced")

    def test_emergency_overrides_battery_and_manual(self):
        self.fs.write("sys/class/power_supply/AC0/online", "0")
        d = make_daemon(self.fs)
        self.fs.write("sys/firmware/acpi/platform_profile", "quiet")  # user pressed Fn+F
        self.fs.cpu(60)
        d.step(0)
        self.assertEqual(self.fs.profile(), "quiet")  # respected
        self.fs.cpu(95)
        d.step(2)
        self.assertEqual(self.fs.profile(), "performance")

    def test_manual_change_pauses_then_resumes(self):
        d = make_daemon(self.fs, manual_override=100)
        self.fs.write("sys/firmware/acpi/platform_profile", "performance")
        self.fs.cpu(40)
        d.step(0)
        d.step(50)
        self.assertEqual(self.fs.profile(), "performance")
        d.step(101)
        d.step(103)
        self.assertEqual(self.fs.profile(), "quiet")

    def test_resume_is_not_treated_as_manual(self):
        d = make_daemon(self.fs)
        self.fs.cpu(60)
        d.step(0)
        self.assertEqual(self.fs.profile(), "balanced")
        self.fs.write("sys/firmware/acpi/platform_profile", "quiet")  # EC reset across suspend
        d.step(5000)
        self.assertEqual(self.fs.profile(), "balanced")
        self.assertEqual(d.override_until, float("-inf"))

    def test_dry_run_keeps_deciding_after_a_change(self):
        d = make_daemon(self.fs, dry_run=True)
        self.fs.cpu(70)
        d.step(0)
        self.assertEqual(d.mode, "performance")
        self.fs.cpu(40)
        d.step(2)
        d.step(31)
        self.assertEqual(d.mode, "quiet")
        self.assertEqual(d.override_until, float("-inf"))  # not mistaken for manual
        self.assertEqual(self.fs.profile(), "balanced")    # nothing written

    def test_dry_run_still_sees_manual_change(self):
        d = make_daemon(self.fs, dry_run=True, manual_override=100)
        self.fs.cpu(70)
        d.step(0)
        self.fs.write("sys/firmware/acpi/platform_profile", "quiet")  # user pressed Fn+F
        d.step(2)
        self.assertEqual(d.mode, "quiet")
        self.assertEqual(d.override_until, 102)

    def test_epp_follows_mode(self):
        d = make_daemon(self.fs, epp_performance="balance_performance")
        self.fs.cpu(70)
        d.step(0)
        self.assertEqual(self.fs.epp(), {"balance_performance"})
        self.fs.cpu(40)
        d.step(31)
        self.assertEqual(self.fs.profile(), "quiet")
        self.assertEqual(self.fs.epp(), {"balance_power"})

    def test_epp_follows_manual_change(self):
        d = make_daemon(self.fs, epp_performance="balance_performance")
        self.fs.write("sys/firmware/acpi/platform_profile", "performance")  # user pressed Fn+F
        self.fs.cpu(60)
        d.step(0)
        self.assertEqual(self.fs.epp(), {"balance_performance"})

    def test_verbose_logs_every_sample(self):
        d = make_daemon(self.fs)
        self.fs.cpu(60)
        with self.assertLogs("vivofan", level="DEBUG") as logs:
            d.step(0)
        self.assertTrue(any("cpu 60°C" in line and "fan 3100 rpm" in line for line in logs.output))

    def test_unreadable_cpu_fails_safe(self):
        d = make_daemon(self.fs)
        for f in (self.fs.root / "sys/class/hwmon/hwmon3").glob("temp*"):
            f.unlink()
        for t in range(3):
            d.step(t * 2)
        self.assertEqual(self.fs.profile(), "performance")

    def test_smoothing_ignores_single_spike(self):
        cfg = Config()
        sensors = Sensors(self.fs.root)
        d = Daemon(cfg, sensors, Backend(self.fs.root))
        self.fs.cpu(50)
        d.step(0)
        self.fs.cpu(80)  # one-sample spike
        d.step(2)
        self.assertEqual(self.fs.profile(), "balanced")

    def test_status_command(self):
        rc = vivofan.main(["--root", str(self.fs.root), "--config", "nonexistent", "--status"])
        self.assertEqual(rc, 0)


class CurveTests(unittest.TestCase):
    cfg = Config()  # [[50, 30], [60, 40], [70, 60], [80, 80], [88, 100]], hysteresis 6

    def test_table(self):
        cases = [
            (45, False, None),   # EC curve below the first point
            (50, False, 30),
            (65, False, 50),
            (84, False, 90),
            (95, False, 100),
            (46, True, 30),      # active: holds the lowest duty in the hysteresis band
            (43, True, None),
        ]
        for temp, active, want in cases:
            with self.subTest(temp=temp, active=active):
                self.assertEqual(curve_percent(temp, self.cfg.fan_curve, self.cfg.hysteresis, active), want)

    def test_validate_rejects_bad_curves(self):
        for curve in ([], [[50, 30], [50, 40]], [[50, 40], [60, 30]], [[50, 10]], [[50, 30], [60, 120]]):
            with self.subTest(curve=curve), self.assertRaises(ValueError):
                Config(fan_curve=curve).validate()
        with self.assertRaises(ValueError):
            Config(fan_control="pwm").validate()


class FanEcTests(unittest.TestCase):
    def test_set_writes_mode_then_duty_on_both_fans(self):
        ec = FakeEc()
        FanEc(ec).set(100, 0)
        self.assertEqual(ec.writes, [(0x32, 0), (0x31, 1), (0x32, 0), (0x35, 255), (0x32, 1), (0x35, 255)])
        self.assertEqual(ec.duty, [255, 255])

    def test_set_clamps_to_tested_range(self):
        ec = FakeEc()
        fan = FanEc(ec)
        fan.set(10, 0)
        self.assertEqual((fan.percent, ec.duty), ([30, 30], [76, 76]))

    def test_release(self):
        ec = FakeEc()
        fan = FanEc(ec)
        fan.set(60, 0)
        self.assertTrue(fan.release())
        self.assertEqual((ec.mode, fan.percent), (0, None))

    def test_release_reports_failure(self):
        ec = FakeEc()
        fan = FanEc(ec)
        fan.set(60, 0)
        ec.sticky = True
        self.assertFalse(fan.release())


class EcDaemonTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()
        self.ec = FakeEc()
        self.d = make_daemon(self.fs, fan=FanEc(self.ec), fan_control="ec")

    def tearDown(self):
        self.fs.cleanup()

    def test_cool_leaves_ec_curve_alone(self):
        self.fs.cpu(40)
        self.d.step(0)
        self.assertEqual(self.ec.writes, [])

    def test_curve_drives_duty_and_releases_when_cool(self):
        self.fs.cpu(65)
        self.d.step(0)
        self.assertEqual((self.ec.mode, self.ec.duty), (1, [128, 128]))  # 50%
        self.fs.cpu(46)
        self.d.step(2)
        self.assertEqual(self.ec.mode, 1)  # hysteresis band: still ours
        self.fs.cpu(40)
        self.d.step(4)
        self.assertEqual(self.ec.mode, 0)

    def test_firmware_modes_untouched(self):
        self.fs.cpu(75)
        self.d.step(0)
        self.assertEqual(self.fs.profile(), "balanced")

    def test_emergency_is_full_speed(self):
        self.fs.cpu(95)
        self.d.step(0)
        self.assertEqual(self.ec.duty, [255, 255])

    def test_unreadable_cpu_is_full_speed(self):
        for f in (self.fs.root / "sys/class/hwmon/hwmon3").glob("temp*"):
            f.unlink()
        for t in range(3):
            self.d.step(t * 2)
        self.assertEqual((self.ec.mode, self.ec.duty), (1, [255, 255]))

    def test_reapplies_when_ec_drops_manual(self):
        self.fs.cpu(65)
        self.d.step(0)
        self.ec.mode = 0  # suspend or Fn+F
        self.d.step(2)
        self.assertEqual(self.ec.mode, 1)

    def test_small_changes_wait_for_refresh(self):
        self.fs.cpu(65)
        self.d.step(0)
        sent = len(self.ec.writes)
        self.fs.cpu(65.5)
        self.d.step(2)
        reads_only = [w for w in self.ec.writes[sent:] if w[0] != 0x32]
        self.assertEqual(reads_only, [])
        self.d.step(40)  # past ec_refresh
        self.assertIn((0x35, 130), self.ec.writes[sent:])  # 51%


class EcMainTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()
        self.ec = FakeEc()
        self.ec.mode = 1
        self.orig = vivofan.make_io
        vivofan.make_io = lambda: self.ec
        self.conf = self.fs.root / "vivofan.toml"

    def tearDown(self):
        vivofan.make_io = self.orig
        self.fs.cleanup()

    def run_main(self, conf, *extra):
        self.conf.write_text(conf)
        return vivofan.main(["--root", str(self.fs.root), "--config", str(self.conf), *extra])

    def test_release_command(self):
        self.assertEqual(self.run_main('fan_control = "ec"\n', "--release"), 0)
        self.assertEqual(self.ec.mode, 0)

    def test_release_is_a_no_op_in_modes(self):
        self.assertEqual(self.run_main("", "--release"), 0)
        self.assertEqual(self.ec.writes, [])

    def test_refuses_other_boards(self):
        self.fs.write("sys/class/dmi/id/board_name", "N6506MU")
        self.assertEqual(self.run_main('fan_control = "ec"\n', "--release"), 1)
        self.assertEqual(self.ec.writes, [])


class WatchdogTests(unittest.TestCase):
    def test_sd_notify_sends_to_notify_socket(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "notify")
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as srv:
                srv.bind(path)
                old = os.environ.get("NOTIFY_SOCKET")
                os.environ["NOTIFY_SOCKET"] = path
                try:
                    sd_notify("WATCHDOG=1")
                finally:
                    if old is None:
                        del os.environ["NOTIFY_SOCKET"]
                    else:
                        os.environ["NOTIFY_SOCKET"] = old
                self.assertEqual(srv.recv(64), b"WATCHDOG=1")

    def test_sd_notify_outside_systemd_is_a_no_op(self):
        old = os.environ.pop("NOTIFY_SOCKET", None)
        try:
            sd_notify("READY=1")
        finally:
            if old is not None:
                os.environ["NOTIFY_SOCKET"] = old


class SplitCurveTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()
        self.ec = FakeEc()

    def tearDown(self):
        self.fs.cleanup()

    def daemon(self, gpu=None, **overrides):
        if gpu is not None:
            self.fs.write("sys/class/hwmon/hwmon6/name", "nouveau")
            self.fs.write("sys/class/hwmon/hwmon6/temp1_input", str(gpu * 1000))
        return make_daemon(self.fs, fan=FanEc(self.ec), fan_control="ec", **overrides)

    def test_gpu_fan_follows_gpu_temp(self):
        d = self.daemon(gpu=70)
        self.fs.cpu(45)  # CPU cool: its fan sits at the curve's lowest point
        d.step(0)
        self.assertEqual(d.fan.percent, [30, 60])

    def test_gpu_fan_follows_cpu_while_gpu_sleeps(self):
        d = self.daemon()
        self.fs.cpu(70)
        d.step(0)
        self.assertEqual(d.fan.percent, [60, 60])

    def test_own_gpu_curve(self):
        d = self.daemon(gpu=60, gpu_fan_curve=[[40, 50], [60, 90]])
        self.fs.cpu(65)
        d.step(0)
        self.assertEqual(d.fan.percent, [50, 90])

    def test_battery_caps_fans(self):
        self.fs.write("sys/class/power_supply/AC0/online", "0")
        d = self.daemon()
        self.fs.cpu(84)
        d.step(0)
        self.assertEqual(d.fan.percent, [60, 60])

    def test_emergency_ignores_battery_cap(self):
        self.fs.write("sys/class/power_supply/AC0/online", "0")
        d = self.daemon()
        self.fs.cpu(95)
        d.step(0)
        self.assertEqual(d.fan.percent, [100, 100])

    def test_validates_gpu_curve_and_battery_cap(self):
        with self.assertRaises(ValueError):
            Config(gpu_fan_curve=[[50, 10]]).validate()
        with self.assertRaises(ValueError):
            Config(battery_max_percent=20).validate()


class OverrideTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()
        self.run = self.fs.root / "run/vivofan"
        self.run.mkdir(parents=True)
        self.now = 1000.0

    def tearDown(self):
        self.fs.cleanup()

    def wall(self):
        return self.now

    def daemon(self, **overrides):
        d = make_daemon(self.fs, **overrides)
        d.run_dir = self.run
        d.wall = self.wall
        return d

    def test_set_and_clear(self):
        cfg = Config(fan_control="ec")
        set_override(self.run, "80", 10, cfg, wall=self.wall)
        self.assertEqual(read_override(self.run, self.now), "80")
        self.assertIsNone(read_override(self.run, self.now + 601))  # expired
        set_override(self.run, "auto", 10, cfg, wall=self.wall)
        self.assertIsNone(read_override(self.run, self.now))

    def test_set_validates(self):
        for value, cfg in (("20", Config(fan_control="ec")), ("turbo", Config(fan_control="ec")),
                           ("80", Config()), ("loud", Config())):
            with self.subTest(value=value), self.assertRaises(ValueError):
                set_override(self.run, value, 10, cfg, wall=self.wall)

    def test_set_needs_running_service(self):
        with self.assertRaises(RuntimeError):
            set_override(self.fs.root / "nope", "80", 10, Config(fan_control="ec"))

    def test_ec_override_wins_over_curve(self):
        ec = FakeEc()
        d = self.daemon(fan=FanEc(ec), fan_control="ec")
        set_override(self.run, "90", 10, d.cfg, wall=self.wall)
        self.fs.cpu(40)  # curve would leave the EC in charge
        d.step(0)
        self.assertEqual((ec.mode, ec.duty), (1, [230, 230]))
        self.now += 601  # expired
        d.step(2)
        self.assertEqual(ec.mode, 0)

    def test_mode_override(self):
        d = self.daemon()
        set_override(self.run, "performance", 10, d.cfg, wall=self.wall)
        self.fs.cpu(40)
        d.step(0)
        self.assertEqual(self.fs.profile(), "performance")

    def test_invalid_override_is_ignored(self):
        d = self.daemon()
        (self.run / "override.json").write_text(json.dumps({"value": "80", "until": self.now + 60}))
        self.fs.cpu(40)
        with self.assertLogs("vivofan", level="WARNING"):
            d.step(0)
        self.assertEqual(self.fs.profile(), "quiet")

    def test_status_file(self):
        ec = FakeEc()
        d = self.daemon(fan=FanEc(ec), fan_control="ec")
        self.fs.cpu(65)
        d.step(0)
        st = json.loads((self.run / "status.json").read_text())
        self.assertEqual(st["cpu"], 65)
        self.assertFalse(st["ec_auto"])
        self.assertEqual([f["percent"] for f in st["fans"]], [50, 50])
        self.assertEqual([f["rpm"] for f in st["fans"]], [2500, 2400])

    def test_status_command_reads_service_status(self):
        d = self.daemon()
        d.wall = time.time  # service_status checks freshness against real time
        self.fs.cpu(50)
        d.step(0)
        rc = vivofan.main(["--root", str(self.fs.root), "--config", "nonexistent", "--status"])
        self.assertEqual(rc, 0)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()
        self.fs.write("sys/class/dmi/id/sys_vendor", "ASUSTeK COMPUTER INC.")
        self.ec = FakeEc()
        self.ec.status = lambda: 0x00

    def tearDown(self):
        self.fs.cleanup()

    def run_probe(self, **kw):
        kw.setdefault("io_factory", lambda: self.ec)
        kw.setdefault("assume_yes", True)
        return probe(self.fs.root, samples=3, sleep=lambda s: None, **kw)

    def test_present(self):
        self.assertEqual(self.run_probe(), 0)
        self.assertTrue(all(reg == 0x32 for reg, _ in self.ec.writes))

    def test_not_asus(self):
        self.fs.write("sys/class/dmi/id/sys_vendor", "LENOVO")
        opened = []
        self.assertEqual(self.run_probe(io_factory=lambda: opened.append(1)), 1)
        self.assertEqual(opened, [])

    def test_no_channel(self):
        self.ec.status = lambda: 0xFF
        self.assertEqual(self.run_probe(), 1)
        self.assertEqual(self.ec.writes, [])

    def test_declined(self):
        self.assertEqual(self.run_probe(assume_yes=False, ask=lambda q: "no"), 1)
        self.assertEqual(self.ec.writes, [])

    def test_probe_io_refuses_control_writes(self):
        io = ProbeIO.__new__(ProbeIO)  # no /dev/port needed: the guard runs first
        for reg, value in ((0x31, 1), (0x35, 255), (0x32, 2)):
            with self.subTest(reg=reg), self.assertRaises(ValueError):
                io.write(reg, value)


class InstallConfigTests(unittest.TestCase):
    def setUp(self):
        self.fs = FakeSys()
        (self.fs.root / "etc").mkdir()
        self.dest = self.fs.root / "etc/vivofan.toml"
        self.dest.write_text("# old\n")
        self.new = self.fs.root / "new.toml"

    def tearDown(self):
        self.fs.cleanup()

    def run_install(self, text):
        self.new.write_text(text)
        return vivofan.main(["--root", str(self.fs.root), "--install-config", str(self.new)])

    def test_installs_valid_config(self):
        text = 'fan_control = "ec"\nfan_curve = [[45, 35], [80, 100]]\n'
        self.assertEqual(self.run_install(text), 0)
        self.assertEqual(self.dest.read_text(), text)
        self.assertEqual(self.dest.stat().st_mode & 0o777, 0o644)

    def test_rejects_bad_values_and_leaves_old_file(self):
        for text in ('fan_curve = [[50, 10]]\n', 'fan_curve = [[60, 30], [50, 40]]\n',
                     'battery_max_percent = 5\n', 'fan_control = "turbo"\n', 'not toml [[[\n'):
            with self.subTest(text=text):
                self.assertEqual(self.run_install(text), 1)
                self.assertEqual(self.dest.read_text(), "# old\n")

    def test_rejects_ec_on_other_boards(self):
        self.fs.write("sys/class/dmi/id/board_name", "N6506MU")
        self.assertEqual(self.run_install('fan_control = "ec"\n'), 1)
        self.assertEqual(self.dest.read_text(), "# old\n")


if __name__ == "__main__":
    unittest.main()
