# ASUS Vivobook Pro 15 (K6502VJ, BIOS 311) — fan control map

Source: ACPI tables dumped from the Windows registry (`acpi/`), decompiled with
Intel iasl 20260408 (`dsl/`). Line numbers refer to `dsl/DSDT-Notebook.dsl`.

## Path 1 — ASUS WMI (`\_SB.ATKD.WMNB`) — the one that works

| WMI ID | Call | Firmware behaviour | Line |
|---|---|---|---|
| `0x00110019` | DSTS | `GQFS()` → `0x00030000 \| (QFAN & 0x0F)` | 127198, 129196 |
| `0x00110019` | DEVS | `FANL(arg)`: write `QFAN`, send EC cmd `0x72`/`0xB4`, set DTT `ODV1`/`ODV2`, notify `IETM` + `NPCF` | 127699, 129158 |
| `0x00110013` | DSTS | EC SRAM bank 2, regs `0x98` (hi) / `0x99` (lo) → RPM/100 \| `0x10000` | 127254 |
| `0x00110011`, `0x0011001A`, `0x0011001E` | both | stubs, return 0 | — |

`QFAN` values (matches MyASUS + Linux `asus-wmi` "VIVO" policy):

| QFAN | Mode | DTT vars |
|---|---|---|
| 0 | Standard | ODV1=0, ODV2=0 |
| 1 | Performance | ODV2=1 |
| 2 | Whisper | ODV1=1, ODV2=0 |

Absent: GPU fan (`0x00110014`), fan curves (`0x00110024/25`), ROG policy (`0x00120075`).

## Path 2 — ACPI 4.0 fan `TFN1` (INTC10A2) — broken in firmware

`SSDT-DptfTabl.dsl:1924`. Present and enabled (`FND1`), Windows binds it as
"Intel Innovation Platform Framework Fan Participant". `_FPS` advertises
0–100% in 13 steps (100% ≈ 12000 RPM). But `_FSL`/`_FST` use EC fields
`PENV`, `PINV`, `PPSL`, `PPSH`, `CFSP` that are only declared `External` —
no table defines them. Intel reference-design leftover; expect
`AE_NOT_FOUND` if Linux `acpi-fan` calls them.

## EC facts

- EC RAM is memory-mapped: `OperationRegion (ERAM, SystemMemory, 0xFE0B0300, 0xFF)` (line 125230).
- `QFAN` = ERAM offset `0x61`.
- Command/data ports `0x66`/`0x62`. `ECD2(cmd, data)` writes both; `SRAM(bank, off)` = cmd `0x92`.
- The actual temperature→PWM curve lives inside EC firmware; ACPI never exposes it.

## Linux plan

1. Kernel ≥ 6.12 (`asus-wmi` Vivobook policy) → `/sys/firmware/acpi/platform_profile`
   (`quiet` / `balanced` / `performance`) and `fan1_input` under the `asus` hwmon.
2. `thermald` (reads the `IETM` ODVs and DTT tables that Windows uses).
3. NVIDIA proprietary driver with runtime PM so the RTX 3050 powers down.
4. Optional project: a userspace daemon that switches QFAN mode from temperatures
   (a "soft" fan curve). True custom curves would need EC firmware reverse engineering.

## Path 3 — MyASUS "healthy table" EC channel (ports 0x25C/0x25D) — WORKS, incl. full speed

The channel MyASUS Fan Diagnosis uses (`AsusWinIO64.dll` → `AsusSAIO.sys`), as
documented by [Keyitdev/asus-fan-control-ec](https://github.com/Keyitdev/asus-fan-control-ec).
The Windows install here ships the same driver: `AsusSAIO.sys` contains ports
`0x25C`/`0x25D` and IOCTL `0x80102070`; the DLL exports `HealthyTable_SetFanPwmDuty`,
`HealthyTable_SetFanTestMode`, `HealthyTable_SetFanIndex`.

Tested on Linux 2026-11-01:

| Reg | Result here |
|---|---|
| `0x30` fan count | 2 |
| `0x31` mode | 0 = EC automatic, 1 = manual. Readable and writable |
| `0x33`/`0x34` tach | works. fan0 = CPU fan: over 45 samples it always agreed with ACPI `fan1_input` (which truncates to 100 rpm); fan1 (likely GPU fan) did not |
| `0x35` duty | write works (255 = full speed on both fans); **read times out** (works on TUF models) |

- No MMIO aperture at `0xFE0B0B7C` (reads `FFFF`); TUF's `0xFEDD8B7C` is AMD-specific.
- **Full speed confirmed** with `research/ec_fullspeed_test.py`: select 0,
  `0x31`=1, then per fan select + `0x35`=255; both fans spun up. Release
  (per fan select + `0x31`=0) was accepted: afterwards the EC curve resumed and
  stopped the fan at idle (0 rpm at 48 °C).
- Duty can't be read back, so the tach registers are the only confirmation of a write.
- **Duty sweep** (`research/ec_duty_sweep.py`, CPU idle ~47 °C), settled rpm:

  | Duty | fan0 (CPU) | fan1 (GPU) |
  |---|---|---|
  | 100% (255) | 7243 | 7284 |
  | 80% (204) | 6137 | 6250 |
  | 60% (153) | 4893 | 5085 |
  | 40% (102) | 3513 | 3739 |
  | 30% (76) | 2691 | 2918 |

  For comparison, the firmware's own performance mode peaked around 2900 rpm under load.
  After release fan0 spun down to 0 within ~5 s (EC idle fan-stop); fan1 wound down slowly.
- Untested: duties below 30%, and whether a platform_profile switch (EC cmd
  `0x72`) drops manual mode.
- Scripts: `research/ec_crosscheck.py` (read-only + select),
  `research/ec_fullspeed_test.py` (supervised 10 s full speed, always releases).
