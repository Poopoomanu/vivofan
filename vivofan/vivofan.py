#!/usr/bin/env python3
"""vivofan - temperature-driven fan mode switching for ASUS Vivobooks.

The K6502VJ firmware has no fan curve interface. The EC only takes three
fan modes (QFAN 0/1/2 via asus-wmi ID 0x00110019). This daemon builds a
"soft" fan curve on top: it watches CPU (and optionally GPU) temperature
and moves between quiet / balanced / performance with hysteresis, going
up quickly and coming down slowly.

Needs Linux >= 6.12 (asus-wmi Vivobook thermal policy) and root.
No third-party dependencies.
"""

import argparse
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field, fields
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

log = logging.getLogger("vivofan")

# Ordered from least to most cooling.
MODES = ("quiet", "balanced", "performance")

# platform_profile names the kernel may use for each mode.
PROFILE_ALIASES = {
    "quiet": ("quiet", "low-power"),
    "balanced": ("balanced",),
    "performance": ("performance",),
}

# asus-wmi throttle_thermal_policy sysfs values (same for ROG and Vivobook).
POLICY_VALUES = {"balanced": "0", "performance": "1", "quiet": "2"}

# EC fan control: only on the board it was tested on (see FINDINGS.md, Path 3).
EC_BOARDS = {"K6502VJQ"}
MIN_PERCENT = 30  # lowest duty tested; lower might not keep the fans turning


@dataclass
class Config:
    interval: float = 2.0             # seconds between samples
    smoothing: float = 0.3            # EMA weight of each new sample (0..1]
    quiet_below: float = 55.0         # quiet -> balanced at/above this
    performance_above: float = 65.0   # balanced -> performance at/above this
    hysteresis: float = 6.0           # degrees below a threshold before stepping down
    emergency: float = 85.0           # raw temp that forces performance immediately
    min_dwell: float = 30.0           # seconds a mode must hold before stepping down
    battery_max_mode: str = "balanced"
    manual_override: float = 600.0    # seconds to back off after a manual mode change
    use_gpu: bool = True
    exit_mode: str = "balanced"
    # CPU energy_performance_preference per mode; "" leaves it alone.
    epp_quiet: str = "balance_power"
    epp_balanced: str = "balance_power"
    epp_performance: str = "balance_power"
    # "modes": switch the three firmware fan modes (above).
    # "ec": drive fan duty directly over the MyASUS EC channel with fan_curve.
    fan_control: str = "modes"
    # [smoothed °C, fan %] points, linear in between. Below the first point the
    # EC's own curve runs (idle fan-stop); at/above the last, its percent.
    fan_curve: list = field(default_factory=lambda: [[50, 30], [60, 40], [70, 60], [80, 80], [88, 100]])
    ec_refresh: float = 30.0          # re-send the duty this often (it can't be read back)

    def epp_for(self, mode):
        return getattr(self, f"epp_{mode}")

    def validate(self):
        for name in ("battery_max_mode", "exit_mode"):
            if getattr(self, name) not in MODES:
                raise ValueError(f"{name} must be one of {MODES}")
        if not 0 < self.smoothing <= 1:
            raise ValueError("smoothing must be in (0, 1]")
        if self.performance_above - self.hysteresis < self.quiet_below:
            raise ValueError("performance_above - hysteresis must be >= quiet_below")
        if self.emergency <= self.performance_above:
            raise ValueError("emergency must be above performance_above")
        if self.fan_control not in ("modes", "ec"):
            raise ValueError('fan_control must be "modes" or "ec"')
        pts = self.fan_curve
        if not pts or any(len(p) != 2 for p in pts):
            raise ValueError("fan_curve must be a list of [temp, percent] pairs")
        temps = [t for t, _ in pts]
        pcts = [p for _, p in pts]
        if temps != sorted(set(temps)):
            raise ValueError("fan_curve temperatures must strictly increase")
        if pcts != sorted(pcts) or pcts[0] < MIN_PERCENT or pcts[-1] > 100:
            raise ValueError(f"fan_curve percents must rise and stay within {MIN_PERCENT}..100")


def load_config(path):
    cfg = Config()
    if path is None or not Path(path).exists():
        return cfg
    if tomllib is None:
        log.warning("Python < 3.11, ignoring %s and using defaults", path)
        return cfg
    with open(path, "rb") as f:
        data = tomllib.load(f)
    known = {f.name: f.type for f in fields(Config)}
    for key, value in data.items():
        if key not in known:
            log.warning("unknown config key %r ignored", key)
            continue
        setattr(cfg, key, type(getattr(cfg, key))(value))
    cfg.validate()
    return cfg


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def read_millidegrees(path):
    raw = read_text(path)
    try:
        return int(raw) / 1000.0
    except (TypeError, ValueError):
        return None


def find_hwmon(root, names):
    """Return hwmon dirs whose `name` file is in `names`."""
    base = Path(root, "sys/class/hwmon")
    if not base.is_dir():
        return []
    return [d for d in sorted(base.iterdir()) if read_text(d / "name") in names]


class Sensors:
    def __init__(self, root="/", use_gpu=True):
        self.root = root
        self.use_gpu = use_gpu
        self.cpu_hwmon = find_hwmon(root, {"coretemp"})
        self.gpu_hwmon = find_hwmon(root, {"nouveau", "nvidia"})
        self.fan_hwmon = find_hwmon(root, {"asus"})
        self.nvidia_pci = self._find_nvidia_pci()
        self.nvidia_smi = shutil.which("nvidia-smi")

    def _find_nvidia_pci(self):
        base = Path(self.root, "sys/bus/pci/devices")
        if not base.is_dir():
            return None
        for dev in sorted(base.iterdir()):
            if read_text(dev / "vendor") == "0x10de" and (read_text(dev / "class") or "").startswith("0x03"):
                return dev
        return None

    def cpu_temp(self):
        temps = []
        for d in self.cpu_hwmon:
            temps += [t for t in map(read_millidegrees, d.glob("temp*_input")) if t is not None]
        if not temps:
            zones = Path(self.root, "sys/class/thermal")
            if zones.is_dir():
                for z in zones.glob("thermal_zone*"):
                    if read_text(z / "type") == "x86_pkg_temp":
                        t = read_millidegrees(z / "temp")
                        if t is not None:
                            temps.append(t)
        return max(temps) if temps else None

    def gpu_temp(self):
        if not self.use_gpu:
            return None
        for d in self.gpu_hwmon:
            t = read_millidegrees(d / "temp1_input")
            if t is not None:
                return t
        # Proprietary driver: only ask nvidia-smi while the GPU is awake.
        # Querying a runtime-suspended GPU would power it up and add heat.
        if self.nvidia_pci is None or self.nvidia_smi is None:
            return None
        if read_text(self.nvidia_pci / "power/runtime_status") != "active":
            return None
        try:
            out = subprocess.run(
                [self.nvidia_smi, "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=3,
            ).stdout
            return float(out.split()[0])
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return None

    def fan_rpm(self):
        for d in self.fan_hwmon:
            raw = read_text(d / "fan1_input")
            if raw and raw.isdigit():
                return int(raw)
        return None

    def on_ac(self):
        base = Path(self.root, "sys/class/power_supply")
        mains = [d for d in base.glob("*") if read_text(d / "type") == "Mains"] if base.is_dir() else []
        if not mains:
            return True
        return any(read_text(d / "online") == "1" for d in mains)


class Backend:
    """Writes the fan mode through platform_profile, or asus-wmi as a fallback."""

    def __init__(self, root="/", dry_run=False):
        self.dry_run = dry_run
        self.profile = Path(root, "sys/firmware/acpi/platform_profile")
        self.policy = Path(root, "sys/devices/platform/asus-nb-wmi/throttle_thermal_policy")
        if self.profile.exists():
            choices = (read_text(Path(root, "sys/firmware/acpi/platform_profile_choices")) or "").split()
            self.names = {}
            for mode, aliases in PROFILE_ALIASES.items():
                match = next((a for a in aliases if a in choices), None)
                if match is None:
                    raise RuntimeError(f"platform_profile has no {mode!r} choice (have: {choices})")
                self.names[mode] = match
            self.path = self.profile
        elif self.policy.exists():
            self.names = dict(POLICY_VALUES)
            self.path = self.policy
        else:
            raise RuntimeError(
                "no fan mode interface found: need platform_profile or asus-nb-wmi "
                "throttle_thermal_policy (kernel >= 6.12 with asus-nb-wmi loaded)"
            )
        self.reverse = {v: k for k, v in self.names.items()}
        # Dry-run pretends its writes landed, so the daemon doesn't mistake the
        # unchanged sysfs value for a manual change and pause itself.
        self.simulated = None
        self.real_at_write = None

    def read(self):
        real = self.reverse.get(read_text(self.path))
        if self.simulated is not None:
            if real == self.real_at_write:
                return self.simulated
            self.simulated = None  # someone really changed the mode
        return real

    def write(self, mode):
        if self.dry_run:
            log.info("[dry-run] would write %r to %s", self.names[mode], self.path)
            self.real_at_write = self.reverse.get(read_text(self.path))
            self.simulated = mode
            return
        self.path.write_text(self.names[mode])


class Epp:
    """Sets the CPU energy/performance preference (intel_pstate, amd-pstate) per mode.

    power-profiles-daemon used to do this; vivofan masks it, so without this
    the preference is stuck at whatever the kernel booted with.
    """

    def __init__(self, cfg, root="/", dry_run=False):
        self.cfg = cfg
        self.dry_run = dry_run
        base = Path(root, "sys/devices/system/cpu/cpufreq")
        self.paths = sorted(base.glob("policy*/energy_performance_preference")) if base.is_dir() else []
        available = set()
        for p in self.paths:
            available |= set((read_text(p.with_name("energy_performance_available_preferences")) or "").split())
        for mode in MODES:
            want = cfg.epp_for(mode)
            if want and self.paths and want not in available:
                raise ValueError(f"epp_{mode} = {want!r} not supported (have: {sorted(available)})")
        self.last = None
        self.warned = False

    def write(self, mode):
        want = self.cfg.epp_for(mode)
        if not want or not self.paths or want == self.last:
            return
        if self.dry_run:
            log.info("[dry-run] would set CPU energy preference %r", want)
            self.last = want
            return
        try:
            for p in self.paths:
                p.write_text(want)
        except OSError as e:
            # EBUSY when the performance governor is in use; not fatal.
            if not self.warned:
                log.warning("could not set CPU energy preference %r: %s", want, e)
                self.warned = True
            return
        log.debug("CPU energy preference %s", want)
        self.last = want


class EcTimeout(OSError):
    pass


class PortIO:
    """The MyASUS "healthy table" EC channel: data 0x25C, command 0x25D.

    Protocol from Keyitdev/asus-fan-control-ec (reverse engineered AsusSAIO.sys).
    Register 0x31 = control mode (0 EC auto, 1 manual, global), 0x32 = fan select,
    0x33/0x34 = tach lo/hi, 0x35 = duty (write-only on the K6502VJ).
    """

    DATA, CMD = 0x25C, 0x25D
    OBF, IBF = 0x01, 0x02

    def __init__(self):
        self.fd = os.open("/dev/port", os.O_RDWR)

    def _in(self, port):
        return os.pread(self.fd, 1, port)[0]

    def _out(self, port, value):
        os.pwrite(self.fd, bytes([value]), port)

    def _wait(self, bit, want):
        for _ in range(1000):
            if bool(self._in(self.CMD) & bit) == want:
                return
            time.sleep(0.0001)
        raise EcTimeout(f"EC status bit {bit:#x} never became {want}")

    def _xact(self, payload, want_result):
        for _ in range(1000):
            if not self._in(self.CMD) & self.OBF:
                break
            self._in(self.DATA)
            time.sleep(0.0001)
        else:
            raise EcTimeout("EC output buffer never drained")
        self._wait(self.IBF, False)
        self._out(self.CMD, 0xFF)
        self._wait(self.IBF, False)
        self._out(self.CMD, 0xDD)
        for b in payload:
            self._wait(self.IBF, False)
            self._out(self.DATA, b)
        self._wait(self.IBF, False)
        if want_result:
            self._wait(self.OBF, True)
            return self._in(self.DATA)
        return None

    def read(self, reg):
        try:
            return self._xact((0x02, reg, 0x00), True)
        except EcTimeout:
            return self._xact((0x02, reg, 0x00), True)

    def write(self, reg, value):
        self._xact((0x82, reg, value), False)
        time.sleep(0.02)


class DryIO:
    """Stands in for PortIO in --dry-run: logs writes, remembers them for reads."""

    def __init__(self):
        self.regs = {0x30: 2, 0x31: 0}

    def read(self, reg):
        return self.regs.get(reg, 0)

    def write(self, reg, value):
        (log.debug if reg == 0x32 else log.info)("[dry-run] EC write reg %#04x = %d", reg, value)
        self.regs[reg] = value


class FanEc:
    """Fan duty over the EC channel, with only the writes vivofan needs."""

    def __init__(self, io):
        self.io = io
        self.percent = None   # last duty sent, None while the EC curve runs
        self.sent_at = float("-inf")

    def _select(self, fan):
        self.io.write(0x32, fan)

    def manual(self):
        self._select(0)
        return self.io.read(0x31) == 1

    def rpm(self, fan):
        self._select(fan)
        return (self.io.read(0x34) << 8) | self.io.read(0x33)

    def set(self, percent, now):
        percent = max(MIN_PERCENT, min(100, round(percent)))
        duty = round(percent * 255 / 100)
        # Mode first, then duty: in auto the EC overwrites the duty register.
        self._select(0)
        self.io.write(0x31, 1)
        for fan in (0, 1):
            self._select(fan)
            self.io.write(0x35, duty)
        self.percent = percent
        self.sent_at = now

    def release(self):
        """Hand both fans back to the EC curve. Returns True once 0x31 reads 0."""
        for _ in range(3):
            for fan in (0, 1):
                self._select(fan)
                self.io.write(0x31, 0)
            if not self.manual():
                self.percent = None
                return True
        return False


def curve_percent(temp, cfg, active):
    """Fan % for a smoothed temp, or None to leave the EC's own curve running.

    Once active, stays active until `hysteresis` below the first point, so it
    doesn't flap between the EC curve and our lowest duty.
    """
    pts = cfg.fan_curve
    start = pts[0][0] - (cfg.hysteresis if active else 0)
    if temp < start:
        return None
    if temp >= pts[-1][0]:
        return pts[-1][1]
    for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
        if t0 <= temp < t1:
            return p0 + (p1 - p0) * (temp - t0) / (t1 - t0)
    return pts[0][1]  # inside the hysteresis band below the first point


def board_name(root="/"):
    return read_text(Path(root, "sys/class/dmi/id/board_name"))


def make_io():
    return PortIO()  # tests replace this


def decide(temp, current, cfg):
    """Pick the mode for a smoothed temperature, with hysteresis on the way down."""
    up = {"quiet": cfg.quiet_below, "balanced": cfg.performance_above}
    down = {"balanced": cfg.quiet_below - cfg.hysteresis,
            "performance": cfg.performance_above - cfg.hysteresis}
    idx = MODES.index(current)
    while idx < len(MODES) - 1 and temp >= up[MODES[idx]]:
        idx += 1
    while idx > 0 and temp < down[MODES[idx]]:
        idx -= 1
    return MODES[idx]


def clock():
    # CLOCK_BOOTTIME keeps counting through suspend, so we can spot resumes.
    if hasattr(time, "CLOCK_BOOTTIME"):
        return time.clock_gettime(time.CLOCK_BOOTTIME)
    return time.monotonic()


class Daemon:
    MAX_SENSOR_FAILURES = 3

    def __init__(self, cfg, sensors, backend, epp=None, fan=None):
        self.cfg = cfg
        self.sensors = sensors
        self.backend = backend
        self.epp = epp
        self.fan = fan  # FanEc when fan_control = "ec"
        self.mode = backend.read() or "balanced"
        self.last_written = self.mode
        self.last_change = float("-inf")
        self.last_step = None
        self.override_until = float("-inf")
        self.ema = None
        self.failures = 0

    def apply(self, mode, now, reason):
        if mode != self.mode:
            log.info("%s -> %s (%s)", self.mode, mode, reason)
        self.backend.write(mode)
        if self.epp:
            self.epp.write(mode)
        self.mode = mode
        self.last_written = mode
        self.last_change = now

    def step(self, now):
        cfg = self.cfg
        resumed = self.last_step is not None and now - self.last_step > cfg.interval * 5
        self.last_step = now

        cpu = self.sensors.cpu_temp()
        if cpu is None:
            self.failures += 1
            if self.failures >= self.MAX_SENSOR_FAILURES:
                if self.fan:
                    if self.fan.percent != 100 or not self.fan.manual():
                        log.info("CPU temperature unreadable, failing safe: fans 100%%")
                        self.fan.set(100, now)
                elif self.mode != "performance":
                    self.apply("performance", now, "CPU temperature unreadable, failing safe")
            return
        self.failures = 0
        gpu = self.sensors.gpu_temp()
        raw = max(t for t in (cpu, gpu) if t is not None)
        self.ema = raw if self.ema is None or resumed else self.ema + cfg.smoothing * (raw - self.ema)

        if self.fan:
            self.step_fan(now, cpu, gpu, raw)
            return

        actual = self.backend.read()
        if log.isEnabledFor(logging.DEBUG):
            gpu_txt = f"{gpu:.0f}°C" if gpu is not None else "-"
            log.debug("cpu %.0f°C gpu %s smoothed %.1f°C fan %s rpm mode %s%s",
                      cpu, gpu_txt, self.ema, self.sensors.fan_rpm(), actual,
                      "" if self.sensors.on_ac() else " (battery)")
        if actual is not None and actual != self.last_written:
            if resumed:
                # The EC can reset its mode across suspend; that is not the user.
                log.info("mode is %s after resume, re-applying %s", actual, self.mode)
                self.apply(self.mode, now, "resume")
            else:
                log.info("mode changed to %s outside vivofan, pausing for %ds", actual, cfg.manual_override)
                self.override_until = now + cfg.manual_override
                self.mode = self.last_written = actual
                if self.epp:
                    self.epp.write(actual)  # follow the mode the user picked

        if raw >= cfg.emergency:
            self.override_until = float("-inf")
            if self.mode != "performance" or actual != "performance":
                self.apply("performance", now, f"emergency {raw:.0f}°C")
            return

        if now < self.override_until:
            return

        target = decide(self.ema, self.mode, cfg)
        if not self.sensors.on_ac() and MODES.index(target) > MODES.index(cfg.battery_max_mode):
            target = cfg.battery_max_mode
        if target == self.mode:
            return
        stepping_down = MODES.index(target) < MODES.index(self.mode)
        if stepping_down and now - self.last_change < cfg.min_dwell:
            return
        gpu_txt = f", gpu {gpu:.0f}°C" if gpu is not None else ""
        rpm = self.sensors.fan_rpm()
        rpm_txt = f", fan {rpm} rpm" if rpm is not None else ""
        self.apply(target, now, f"cpu {cpu:.0f}°C{gpu_txt}, smoothed {self.ema:.1f}°C{rpm_txt}")

    def step_fan(self, now, cpu, gpu, raw):
        """fan_control = "ec": set fan duty from fan_curve; firmware modes are left alone."""
        cfg, fan = self.cfg, self.fan
        if raw >= cfg.emergency:
            want, why = 100, f"emergency {raw:.0f}°C"
        else:
            want = curve_percent(self.ema, cfg, fan.percent is not None)
            gpu_txt = f", gpu {gpu:.0f}°C" if gpu is not None else ""
            why = f"cpu {cpu:.0f}°C{gpu_txt}, smoothed {self.ema:.1f}°C"
        log.debug("%s -> %s", why, "EC curve" if want is None else f"{want:.0f}%")

        if want is None:
            if fan.percent is not None:
                log.info("fans %d%% -> EC automatic (%s)", fan.percent, why)
                if not fan.release():
                    log.warning("EC did not confirm the release; will retry")
                    fan.percent = 0  # keep trying on the next step
            return

        target = max(MIN_PERCENT, min(100, round(want)))
        manual = fan.manual()
        if fan.percent is not None and not manual:
            # Suspend or a firmware mode switch can drop manual mode.
            log.info("EC left manual mode, re-applying %d%%", target)
        changed = fan.percent is None or abs(target - fan.percent) >= 3 or (
            target in (MIN_PERCENT, 100) and target != fan.percent)
        if changed:
            before = "EC automatic" if fan.percent is None else f"{fan.percent}%"
            log.info("fans %s -> %d%% (%s)", before, target, why)
        if changed or not manual or now - fan.sent_at >= cfg.ec_refresh:
            fan.set(target, now)


def status(sensors, backend, epp):
    cpu, gpu, rpm = sensors.cpu_temp(), sensors.gpu_temp(), sensors.fan_rpm()
    prefs = sorted({read_text(p) for p in epp.paths})
    print(f"interface : {backend.path}")
    print(f"modes     : {backend.names}")
    print(f"current   : {backend.read()}")
    print(f"cpu epp   : {', '.join(p or '?' for p in prefs) if prefs else 'unavailable'}")
    print(f"cpu temp  : {cpu if cpu is not None else 'unavailable'}")
    print(f"gpu temp  : {gpu if gpu is not None else 'unavailable (or GPU asleep)'}")
    print(f"fan rpm   : {rpm if rpm is not None else 'unavailable'}")
    print(f"on AC     : {sensors.on_ac()}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--config", default="/etc/vivofan.toml")
    p.add_argument("--dry-run", action="store_true", help="log decisions without writing")
    p.add_argument("--status", action="store_true", help="print sensors and interface, then exit")
    p.add_argument("--release", action="store_true",
                   help='hand the fans back to the EC curve (fan_control = "ec"), then exit')
    p.add_argument("--verbose", "-v", action="store_true")
    p.add_argument("--root", default="/", help=argparse.SUPPRESS)  # fake sysfs root for tests
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    try:
        cfg = load_config(args.config)
        fan = None
        if cfg.fan_control == "ec":
            board = board_name(args.root)
            if board not in EC_BOARDS:
                raise RuntimeError(f'fan_control = "ec" is only tested on {sorted(EC_BOARDS)}, '
                                   f"this board is {board!r}")
            fan = FanEc(DryIO() if args.dry_run else make_io())
        if args.release:
            if fan and not fan.release():
                log.error("EC did not confirm the release")
                return 1
            return 0
        sensors = Sensors(args.root, cfg.use_gpu)
        backend = Backend(args.root, args.dry_run)
        epp = Epp(cfg, args.root, args.dry_run)
    except (RuntimeError, ValueError, OSError) as e:
        log.error("%s", e)
        return 1

    if args.status:
        status(sensors, backend, epp)
        if fan:
            print(f"ec fans   : {'manual' if fan.manual() else 'EC automatic'}, "
                  f"cpu fan {fan.rpm(0)} rpm, gpu fan {fan.rpm(1)} rpm")
        return 0

    daemon = Daemon(cfg, sensors, backend, epp, fan)
    epp.write(daemon.mode)
    running = True

    def stop(signum, _frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    log.info("started: %s via %s, current mode %s", "dry-run" if args.dry_run else "live",
             "EC fan curve" if fan else backend.path, daemon.mode)

    while running:
        try:
            daemon.step(clock())
        except OSError as e:
            log.warning("step failed: %s", e)
        time.sleep(cfg.interval)

    if fan:
        # The service's ExecStopPost runs --release too, in case we die before this.
        try:
            log.info("fans back to EC automatic" if fan.release() else "EC did not confirm the release")
        except OSError as e:
            log.warning("could not release the fans: %s", e)
    elif clock() >= daemon.override_until:
        try:
            daemon.apply(cfg.exit_mode, clock(), "exiting")
        except OSError as e:
            log.warning("could not restore %s: %s", cfg.exit_mode, e)
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
