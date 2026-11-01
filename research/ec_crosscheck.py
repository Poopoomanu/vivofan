#!/usr/bin/env python3
"""Cross-check the EC "healthy table" fan speed registers against the kernel.

Talks to the EC channel MyASUS uses (ports 0x25C data / 0x25D command, found by
Keyitdev/asus-fan-control-ec) and compares each fan's tach registers with the
CPU fan speed the kernel reads through ACPI (asus hwmon fan1_input).

It can only do two things, enforced below:
  - read registers 0x30 (fan count), 0x33/0x34 (tach low/high)
  - write register 0x32 (fan select) with 0 or 1
It never touches 0x31 (manual mode) or 0x35 (duty). Run as root.
"""

import os
import sys
import time
from pathlib import Path

DATA, CMD = 0x25C, 0x25D
OBF, IBF = 0x01, 0x02
READABLE = {0x30, 0x33, 0x34}
SELECT = 0x32


class Timeout(Exception):
    pass


class Ec:
    def __init__(self):
        self.fd = os.open("/dev/port", os.O_RDWR)

    def inb(self, port):
        return os.pread(self.fd, 1, port)[0]

    def outb(self, port, value):
        os.pwrite(self.fd, bytes([value]), port)

    def wait(self, bit, want):
        for _ in range(1000):
            if bool(self.inb(CMD) & bit) == want:
                return
            time.sleep(0.0001)
        raise Timeout(f"status bit {bit:#x} never became {want}")

    def xact(self, payload, want_result):
        # Same sequence as asus-fan-control-ec ec_once(): drain, preamble, 0xDD, payload.
        for _ in range(1000):
            if not self.inb(CMD) & OBF:
                break
            self.inb(DATA)
            time.sleep(0.0001)
        else:
            raise Timeout("output buffer never drained")
        self.wait(IBF, False)
        self.outb(CMD, 0xFF)
        self.wait(IBF, False)
        self.outb(CMD, 0xDD)
        for b in payload:
            self.wait(IBF, False)
            self.outb(DATA, b)
        self.wait(IBF, False)
        if want_result:
            self.wait(OBF, True)
            return self.inb(DATA)
        return None

    def read(self, reg):
        if reg not in READABLE:
            raise ValueError(f"refusing to read {reg:#x}")
        for attempt in range(2):
            try:
                return self.xact((0x02, reg, 0x00), True)
            except Timeout:
                if attempt:
                    raise

    def select(self, fan):
        if fan not in (0, 1):
            raise ValueError("fan must be 0 or 1")
        self.xact((0x82, SELECT, fan), False)
        time.sleep(0.02)

    def rpm(self, fan):
        self.select(fan)
        hi = self.read(0x34)
        lo = self.read(0x33)
        return (hi << 8) | lo


def kernel_rpm():
    for hw in Path("/sys/class/hwmon").iterdir():
        if (hw / "name").read_text().strip() == "asus":
            return int((hw / "fan1_input").read_text())
    return None


def cpu_temp():
    for hw in Path("/sys/class/hwmon").iterdir():
        if (hw / "name").read_text().strip() == "coretemp":
            return int((hw / "temp1_input").read_text()) // 1000
    return None


def main():
    samples = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    if os.geteuid() != 0:
        sys.exit("run as root: sudo python3 ec_crosscheck.py")
    ec = Ec()
    count = ec.read(0x30)
    print(f"fan count (0x30): {count}")
    if count not in (1, 2):
        sys.exit("unexpected fan count, stopping")

    rows = []
    print(f"{'t':>4}  {'cpu':>4}  {'kernel':>6}  {'fan0':>6}  {'fan1':>6}")
    for i in range(samples):
        k = kernel_rpm()
        fans = [ec.rpm(f) for f in range(count)]
        rows.append((k, fans))
        f1 = f"{fans[1]:6d}" if count > 1 else "     -"
        print(f"{i * 2:3d}s  {cpu_temp():3d}C  {k:6d}  {fans[0]:6d}  {f1}", flush=True)
        time.sleep(2)

    print()
    for f in range(count):
        diffs = [abs(k - fans[f]) for k, fans in rows if k is not None]
        moving = len({fans[f] // 100 for _, fans in rows}) > 1
        print(f"fan{f}: mean |kernel - ec| = {sum(diffs) / len(diffs):.0f} rpm, "
              f"max {max(diffs)} rpm, {'changes' if moving else 'flat'} over the run")
    print("kernel rpm is rounded to 100 by the firmware, so ~100 rpm of difference is a match")


if __name__ == "__main__":
    main()
