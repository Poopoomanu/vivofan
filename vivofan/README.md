# vivofan

A temperature-driven fan mode daemon for the ASUS Vivobook Pro 15 (K6502VJ).

The firmware gives Linux only three fan modes (see `../FINDINGS.md`), so vivofan
turns them into a soft fan curve:

| Smoothed temp | Mode |
|---|---|
| < 55 °C | quiet (Whisper) |
| 55–65 °C | balanced (Standard) |
| ≥ 65 °C | performance |
| ≥ 85 °C raw | performance immediately, overrides everything |

- Steps **up** as soon as a threshold is crossed. Steps **down** only after
  falling 6 °C below it **and** holding the mode for 30 s.
- On battery, stays at most at `balanced`, unless the emergency limit is hit.
- If you change the mode yourself (Fn key, GNOME/KDE power menu), it backs off for 10 min.
- If the EC resets its mode during suspend, vivofan puts its mode back.
- If CPU temperature can't be read 3 times in a row, it switches to performance.
- Reads the RTX 3050 temperature only while the GPU is already awake, so it never wakes the GPU.
- Keeps the CPU energy preference at `balance_power` in every mode (settable
  per mode in `vivofan.toml`), which power-profiles-daemon used to manage. This
  trims the boost spikes that heat the CPU. Raising it in performance mode made
  a fully loaded CPU jump from 66 °C to 93 °C in under a second, so the default
  no longer does.

All of this is set in `vivofan.toml`.

## Real fan curve (`fan_control = "ec"`)

On the K6502VJQ, vivofan can also set the fan speed directly, through the EC
channel MyASUS uses (see `../FINDINGS.md`, Path 3). Off by default.

- `fan_curve` maps smoothed temperature to fan % (30–100), linear in between.
  Below the first point the EC's own curve runs, so the fan still stops at idle.
- The CPU fan follows the CPU temperature and the GPU fan the GPU temperature
  (the CPU's while the GPU sleeps), on `gpu_fan_curve` or else `fan_curve`.
  Manual mode is global on the EC, so while one fan needs vivofan the other
  runs at least its curve's lowest point.
- On battery the fans are capped at `battery_max_percent` (default 60).
- Emergency temperature or an unreadable CPU temperature means 100%.
- If the EC drops manual mode (suspend, Fn+F), vivofan puts it back.
- The firmware fan modes are left alone in this mode.
- On stop, and through `ExecStopPost` even if vivofan crashes, the fans are
  handed back to the EC. By hand: `sudo vivofan --release`.
- Refuses to run on any other board.

Try it first: `sudo python3 vivofan.py --dry-run -v --config <a toml with fan_control = "ec">`.

## Overrides, status and the tray

```bash
sudo vivofan --set 100 --minutes 10   # fans 100% for 10 min ("ec")
sudo vivofan --set performance        # or a mode for 30 min ("modes")
sudo vivofan --set auto               # back to automatic
vivofan --status                      # sensors plus what the service is doing
```

The service writes its state to `/run/vivofan/status.json` every few seconds.
`vivofan-tray` (installed with an autostart entry) shows it in the panel and
offers the common overrides, asking for your password via `pkexec`.

## Safety net

- `Type=notify` with `WatchdogSec=30`: if vivofan hangs, systemd kills it,
  `ExecStopPost` hands the fans back to the laptop, and it restarts.
- `ExecStopPost` also runs after a crash or `systemctl stop`.
- Fan duty never goes below 30%, so vivofan can't stop the fans.

## Other ASUS laptops: `--probe`

```bash
sudo vivofan --probe
```

A read-only check whether this laptop has the same EC fan channel. Its only
write selects which fan to read; it never changes fan control, and it asks
first. It prints a report to paste into a GitHub issue, which is how more
boards can be added.

## Before installing (once Linux is on the laptop)

1. Kernel **6.12 or newer**: `uname -r`
2. `thermald` running: `systemctl status thermald`
3. NVIDIA proprietary driver with runtime power management, so the dGPU can power off.
4. Check the interface exists:
   ```bash
   cat /sys/firmware/acpi/platform_profile_choices
   ```
   You should see `quiet balanced performance` (or `low-power ...`).

## Try it without installing

```bash
sudo python3 vivofan.py --status
```

```bash
sudo python3 vivofan.py --dry-run --config vivofan.toml -v
```

Dry-run logs every decision without changing anything. Run a stress test in
another terminal (`stress-ng --cpu 0 -t 60`) and watch the decisions.

## Install

```bash
sudo ./install.sh
```

```bash
journalctl -u vivofan -f
```

The service conflicts with `power-profiles-daemon` / `tuned-ppd`, because those
also write `platform_profile`. Starting vivofan stops them.

## Tests

```bash
python3 -m unittest -v test_vivofan test_tray
```

Runs against a fake `/sys` tree, so it works on any OS.
