#!/usr/bin/env python3
"""Supervised 10-second full-speed test over the MyASUS EC channel (0x25C/0x25D).

Plan (see FINDINGS.md, Path 3):
  1. Preflight, no control writes: root, CPU <= 70 °C, fan count 2,
     mode register 0x31 == 0 (EC automatic), both tachs readable, baseline rpm.
  2. Arm the release: try/finally plus SIGINT/SIGTERM/SIGHUP handlers.
  3. Apply (same order as asus-fan-control-ec): select 0, 0x31=1 (manual),
     then for each fan: select, 0x35=255.
  4. Watch both tachs for 10 s. Release early if a fan drops below 1000 rpm and
     under half its baseline for 2 samples in a row, the CPU reaches 85 °C, or
     the EC stops answering.
  5. Release: for each fan, select, 0x31=0. Read 0x31 back, retry up to 3 times.
  6. Watch 10 s more to see the EC curve take over again.

Writes are limited, enforced in Guarded.write(): 0x32 in {0,1}, 0x31 in {0,1},
0x35 == 255 only (never a low duty that could stop a fan).
--dry-run prints every port transaction without touching the hardware.
"""

import argparse
import os
import signal
import sys
import time

from ec_crosscheck import CMD, DATA, Ec, Timeout, cpu_temp, kernel_rpm

ALLOWED_WRITES = {0x32: {0, 1}, 0x31: {0, 1}, 0x35: {255}}
ALLOWED_READS = {0x30, 0x31, 0x33, 0x34}
MAX_START_TEMP = 70
ABORT_TEMP = 85
STALL_RPM = 1000


class Guarded(Ec):
    def __init__(self, dry_run, duties=frozenset({255})):
        self.dry_run = dry_run
        self.allowed = {**ALLOWED_WRITES, 0x35: set(duties)}
        if not dry_run:
            super().__init__()

    def read(self, reg):
        if reg not in ALLOWED_READS:
            raise ValueError(f"refusing to read {reg:#x}")
        if self.dry_run:
            print(f"    [dry-run] read  {reg:#04x}")
            return {0x30: 2, 0x31: 0}.get(reg, 0x09)  # fake ~2300 rpm tach
        for attempt in range(2):
            try:
                return self.xact((0x02, reg, 0x00), True)
            except Timeout:
                if attempt:
                    raise

    def write(self, reg, value):
        if value not in self.allowed.get(reg, ()):
            raise ValueError(f"refusing to write {value} to {reg:#x}")
        if self.dry_run:
            print(f"    [dry-run] write {reg:#04x} = {value}  (out {CMD:#x}<-FF,DD; {DATA:#x}<-82,{reg:02X},{value:02X})")
            return
        self.xact((0x82, reg, value), False)
        time.sleep(0.02)

    def select(self, fan):
        self.write(0x32, fan)

    def rpm(self, fan):
        self.select(fan)
        return (self.read(0x34) << 8) | self.read(0x33)


def sample(ec):
    return [ec.rpm(0), ec.rpm(1)], kernel_rpm(), cpu_temp()


def show(label, fans, kernel, temp):
    print(f"{label:>10}  cpu {temp:3d}C  kernel {kernel:5d}  fan0 {fans[0]:5d}  fan1 {fans[1]:5d}", flush=True)


def release(ec):
    for attempt in range(1, 4):
        try:
            for fan in (0, 1):
                ec.select(fan)
                ec.write(0x31, 0)
            modes = []
            for fan in (0, 1):
                ec.select(fan)
                modes.append(ec.read(0x31))
            if not any(modes):
                print(f"released to EC automatic (0x31 reads {modes}, attempt {attempt})")
                return True
            print(f"still manual after release attempt {attempt}: 0x31 = {modes}")
        except Timeout as e:
            print(f"release attempt {attempt} timed out: {e}")
        time.sleep(0.5)
    print("!!! The EC did not confirm the release. The fans may stay at a fixed speed.")
    print("!!! Reboot to reset it. If that doesn't help: shut down, plug in the charger,")
    print("!!! hold the power button for 40 seconds (ASUS EC reset).")
    return False


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="print transactions, touch nothing")
    p.add_argument("--seconds", type=float, default=10.0, help="time at full speed (max 15)")
    args = p.parse_args()
    if not 1 <= args.seconds <= 15:
        sys.exit("--seconds must be 1..15")
    if not args.dry_run and os.geteuid() != 0:
        sys.exit("run as root (or use --dry-run)")

    ec = Guarded(args.dry_run)

    # 1. Preflight: reads and fan select only.
    temp = cpu_temp()
    if temp is None or temp > MAX_START_TEMP:
        sys.exit(f"CPU is {temp}°C; let it cool below {MAX_START_TEMP}°C first")
    if ec.read(0x30) != 2:
        sys.exit("fan count is not 2, stopping")
    ec.select(0)
    if ec.read(0x31) != 0:
        sys.exit("fans are not in EC automatic mode, stopping")
    base = [0, 0]
    for _ in range(3):
        fans, kernel, temp = sample(ec)
        show("baseline", fans, kernel, temp)
        base = [max(b, f) for b, f in zip(base, fans)]
        time.sleep(0.5)

    # 2. Arm the release before the first control write.
    def on_signal(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    released = False
    peak = [0, 0]
    reason = "time up"
    try:
        # 3. Apply.
        print("--- manual mode, duty 255 on both fans")
        ec.select(0)
        ec.write(0x31, 1)
        for fan in (0, 1):
            ec.select(fan)
            ec.write(0x35, 255)

        # 4. Watch.
        stalls = 0
        end = time.monotonic() + args.seconds
        while time.monotonic() < end:
            time.sleep(0.5)
            fans, kernel, temp = sample(ec)
            show("manual", fans, kernel, temp)
            peak = [max(pk, f) for pk, f in zip(peak, fans)]
            stalled = any(f < STALL_RPM and f < b / 2 for f, b in zip(fans, base))
            stalls = stalls + 1 if stalled else 0
            if stalls >= 2:
                reason = "a fan stalled"
                break
            if temp >= ABORT_TEMP:
                reason = f"CPU reached {temp}°C"
                break
    except Timeout as e:
        reason = f"EC stopped answering: {e}"
    except KeyboardInterrupt as e:
        reason = f"interrupted ({e or 'Ctrl-C'})"
    except Exception as e:  # still release, then report
        reason = f"error: {e!r}"
    finally:
        # 5. Release, always. Ignore further Ctrl-C so it can't cut the release short.
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        print(f"--- releasing ({reason})")
        released = release(ec)

    # 6. Watch the EC take over again.
    if released:
        for _ in range(10):
            time.sleep(1)
            fans, kernel, temp = sample(ec)
            show("after", fans, kernel, temp)

    print()
    print(f"baseline fan0 {base[0]} / fan1 {base[1]} rpm, peak in manual fan0 {peak[0]} / fan1 {peak[1]} rpm")
    worked = [pk >= b + 1000 for pk, b in zip(peak, base)]
    print("full speed took effect: " + ", ".join(f"fan{i} {'YES' if w else 'no'}" for i, w in enumerate(worked)))
    return 0 if released else 2


if __name__ == "__main__":
    sys.exit(main())
