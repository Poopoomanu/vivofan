"""Scan raw AML tables for ASUS WMI device IDs (DEVS/DSTS) without decompiling.

AML encodes a 32-bit constant as DWordPrefix (0x0C) + 4 little-endian bytes,
so every ID the firmware compares against shows up as that byte pattern.
Names come from the Linux kernel's include/linux/platform_data/x86/asus-wmi.h.
"""
import struct
import sys
from collections import defaultdict
from pathlib import Path

KNOWN = {
    0x00110012: "FAN_CTRL (legacy)",
    0x00110013: "CPU_FAN_CTRL",
    0x00110014: "GPU_FAN_CTRL",
    0x00110031: "MID_FAN_CTRL",
    0x00110018: "FAN_BOOST_MODE",
    0x00110019: "THROTTLE_THERMAL_POLICY_VIVO",
    0x00110024: "CPU_FAN_CURVE",
    0x00110025: "GPU_FAN_CURVE",
    0x00110032: "MID_FAN_CURVE",
    0x00120075: "THROTTLE_THERMAL_POLICY",
    0x00120057: "RSOC (battery charge limit)",
    0x00120066: "PPT_PL2_SPPT",
    0x001200A0: "PPT_PL1_SPL",
    0x001200A3: "PPT_FPPT",
    0x001200C0: "NV_DYN_BOOST",
    0x001200C2: "NV_THERM_TARGET",
    0x00120093: "PPT_PL3_FPPT",
    0x00090016: "GPU_MUX",
    0x00090020: "DGPU (eco mode)",
    0x00050021: "KBD_BACKLIGHT",
    0x00060023: "MINI_LED_MODE",
    0x00050019: "PANEL_OD",
}


def scan(path: Path, hits):
    data = path.read_bytes()
    i = data.find(b"\x0c")
    while i != -1 and i + 5 <= len(data):
        (val,) = struct.unpack_from("<I", data, i + 1)
        hi = val >> 16
        # ASUS WMI device IDs live in 0x0001xxxx..0x0013xxxx with a small low word
        if 0x0001 <= hi <= 0x0013 and (val & 0xFFFF) <= 0x00FF:
            hits[val].append((path.name, i))
        i = data.find(b"\x0c", i + 1)


def main(folder):
    hits = defaultdict(list)
    for p in sorted(Path(folder).glob("*.aml")):
        scan(p, hits)
    for val in sorted(hits):
        locs = hits[val]
        tables = sorted({t for t, _ in locs})
        name = KNOWN.get(val, "")
        print(f"0x{val:08X}  x{len(locs):<3} {name:<30} {', '.join(tables)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "acpi")
