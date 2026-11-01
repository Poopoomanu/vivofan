#!/usr/bin/env python3
"""Supervised duty sweep over the MyASUS EC channel: 100% -> 80 -> 60 -> 40 -> 30%.

Uses the same guarded port access and release as ec_fullspeed_test.py.
Starts at full speed and steps down, so the fans are already spinning before
any low duty (a fan can fail to start from rest at a low duty).

  - Stops vivofan for the test and starts it again at the end.
  - Holds each step 6 s; the settled speed is the mean of the last 3 samples.
  - Releases early if a fan reads under 300 rpm for 3 samples in a row (stall:
    lower steps are skipped), the CPU reaches 85 °C, or the EC stops answering.
  - Always releases to EC automatic (0x31=0), also on Ctrl-C.
Duty writes are limited to the five sweep values. Run as root, or --dry-run.
"""

import argparse
import os
import signal
import subprocess
import sys
import time

from ec_fullspeed_test import ABORT_TEMP, MAX_START_TEMP, Guarded, Timeout, release, sample, show

STEPS = [100, 80, 60, 40, 30]  # percent
HOLD = 6
STALL_RPM = 300


def duty(percent):
    return round(percent * 255 / 100)


def vivofan_active():
    return subprocess.run(["systemctl", "is-active", "--quiet", "vivofan"]).returncode == 0


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--dry-run", action="store_true", help="print transactions, touch nothing")
    args = p.parse_args()
    if not args.dry_run and os.geteuid() != 0:
        sys.exit("run as root (or use --dry-run)")

    ec = Guarded(args.dry_run, duties={duty(s) for s in STEPS})

    temp = sample(ec)[2]
    if temp is None or temp > MAX_START_TEMP:
        sys.exit(f"CPU is {temp}°C; let it cool below {MAX_START_TEMP}°C first")
    if ec.read(0x30) != 2:
        sys.exit("fan count is not 2, stopping")
    ec.select(0)
    if ec.read(0x31) != 0:
        sys.exit("fans are not in EC automatic mode, stopping")

    restart_vivofan = not args.dry_run and vivofan_active()
    if restart_vivofan:
        subprocess.run(["systemctl", "stop", "vivofan"], check=True)
        print("stopped vivofan for the test")

    def on_signal(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, on_signal)

    results = []
    reason = "sweep finished"
    try:
        ec.select(0)
        ec.write(0x31, 1)
        for pct in STEPS:
            for fan in (0, 1):
                ec.select(fan)
                ec.write(0x35, duty(pct))
            print(f"--- {pct}% (duty {duty(pct)})")
            history, low = [], 0
            for _ in range(HOLD):
                time.sleep(1)
                fans, kernel, temp = sample(ec)
                show(f"{pct}%", fans, kernel, temp)
                history.append(fans)
                low = low + 1 if min(fans) < STALL_RPM else 0
                if temp >= ABORT_TEMP:
                    raise RuntimeError(f"CPU reached {temp}°C")
                if low >= 3:
                    break
            last = history[-3:]
            settled = [round(sum(f[i] for f in last) / len(last)) for i in (0, 1)]
            results.append((pct, settled))
            if low >= 3:
                reason = f"a fan stalled at {pct}%, lower steps skipped"
                break
    except Timeout as e:
        reason = f"EC stopped answering: {e}"
    except KeyboardInterrupt as e:
        reason = f"interrupted ({e or 'Ctrl-C'})"
    except Exception as e:  # still release, then report
        reason = f"stopped: {e}"
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, signal.SIG_IGN)
        print(f"--- releasing ({reason})")
        released = release(ec)
        if restart_vivofan:
            subprocess.run(["systemctl", "start", "vivofan"])
            print(f"vivofan: {'active' if vivofan_active() else 'NOT running, start it: sudo systemctl start vivofan'}")

    if released:
        for _ in range(8):
            time.sleep(1)
            show("after", *sample(ec))

    print()
    print("duty   fan0 rpm  fan1 rpm   (settled, mean of last 3 samples)")
    for pct, (f0, f1) in results:
        print(f"{pct:3d}%   {f0:8d}  {f1:8d}")
    return 0 if released else 2


if __name__ == "__main__":
    sys.exit(main())
