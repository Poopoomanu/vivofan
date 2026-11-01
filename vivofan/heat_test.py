#!/usr/bin/env python3
"""Heat test for vivofan: ramp up CPU load, stop it all at a temperature cutoff.

Adds busy-loop workers a few at a time and prints temps, fan and mode every
half second. The moment any CPU core reaches --cutoff, every worker is killed.
Then it keeps watching while the laptop cools, so you can see vivofan step down.
Needs no root: it only reads /sys and burns CPU.
"""

import argparse
import multiprocessing
import os
import time
from pathlib import Path

import vivofan


def burn():
    os.nice(19)  # stay behind anything the desktop needs
    while True:
        pass


def read(path):
    return vivofan.read_text(path) or "?"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--cutoff", type=float, default=75.0, help="°C on any core that stops the load")
    p.add_argument("--step", type=int, default=2, help="workers added each ramp step")
    p.add_argument("--every", type=float, default=15.0, help="seconds between ramp steps")
    p.add_argument("--max-load", type=float, default=180.0, help="seconds before the load stops anyway")
    p.add_argument("--cooldown", type=float, default=60.0, help="seconds to keep watching after the load stops")
    args = p.parse_args()

    sensors = vivofan.Sensors(use_gpu=False)
    profile = Path("/sys/firmware/acpi/platform_profile")
    epp = Path("/sys/devices/system/cpu/cpufreq/policy0/energy_performance_preference")
    workers = []
    start = time.monotonic()
    stopped_at = None
    reason = "time limit"

    def stop(why):
        nonlocal stopped_at, reason
        for w in workers:
            w.kill()
        for w in workers:
            w.join()
        workers.clear()
        stopped_at = time.monotonic()
        reason = why
        print(f"--- load stopped: {why}", flush=True)

    print(f"cutoff {args.cutoff:.0f}°C, +{args.step} workers every {args.every:.0f}s, "
          f"max {os.cpu_count()} workers", flush=True)
    try:
        while True:
            t = time.monotonic() - start
            temp = sensors.cpu_temp()
            if stopped_at is None:
                if temp is None:
                    stop("CPU temperature unreadable")
                elif temp >= args.cutoff:
                    stop(f"{temp:.0f}°C reached cutoff")
                elif t >= args.max_load:
                    stop("time limit")
                else:
                    want = min(os.cpu_count(), args.step * (1 + int(t // args.every)))
                    while len(workers) < want:
                        w = multiprocessing.Process(target=burn, daemon=True)
                        w.start()
                        workers.append(w)
            elif time.monotonic() - stopped_at >= args.cooldown:
                break
            temp_txt = f"{temp:.0f}°C" if temp is not None else "?"
            print(f"t={t:5.1f}s load={len(workers):2d} cpu={temp_txt} fan={sensors.fan_rpm()} "
                  f"mode={read(profile)} epp={read(epp)}", flush=True)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        if workers:
            stop("interrupted")
    print(f"done ({reason})")


if __name__ == "__main__":
    main()
