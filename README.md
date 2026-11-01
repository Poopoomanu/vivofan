# vivofan

[![tests](https://github.com/Poopoomanu/vivofan/actions/workflows/tests.yml/badge.svg)](https://github.com/Poopoomanu/vivofan/actions/workflows/tests.yml)

Fan control for the ASUS Vivobook Pro 15 (K6502VJ / Q530VJ, board K6502VJQ) on Linux.

On Linux this laptop only offers three firmware fan modes, and the usual tools
(fan curves, `pwm1_enable`, NBFC) don't work. vivofan fixes that:

- **Fan modes by temperature:** switches quiet / balanced / performance
  automatically, and keeps the CPU on an energy-saving preference so it runs cooler.
- **A real fan curve** (optional): sets the fan speed directly, 30–100%, up to
  about 7250 rpm. The CPU fan follows the CPU temperature and the GPU fan the
  GPU's, with a gentler cap on battery.
- **Tray icon** with temperatures, fan speeds and one-click overrides
  (e.g. "Fans 100% for 10 min").
- **Safe by design:** never below 30%, fans handed back to the laptop when
  vivofan stops, crashes or hangs (systemd watchdog).

> **Warning:** the direct fan curve is tested only on board `K6502VJQ`, and
> vivofan refuses to use it on any other board. Writing to an embedded
> controller can affect more than fans. Use at your own risk.

## Install guide

Tested on Linux Mint 22.3 (kernel 6.14), BIOS K6502VJQ.311.

### 1. Check your laptop

```bash
cat /sys/class/dmi/id/board_name
```

It must print `K6502VJQ`. Then check the kernel is 6.12 or newer and that the
fan modes are available:

```bash
uname -r
```

```bash
cat /sys/firmware/acpi/platform_profile_choices
```

You should see `quiet balanced performance`.

### 2. Download

**Easiest:** download `vivofan_<version>_all.deb` from the
[latest release](https://github.com/Poopoomanu/vivofan/releases/latest) and
install it (it sets everything up, including the tray icon):

```bash
sudo apt install ./vivofan_*_all.deb
```

Then skip to step 5. **Or** use the latest code from git:

```bash
sudo apt install -y git
```

```bash
git clone https://github.com/Poopoomanu/vivofan.git
```

```bash
cd vivofan/vivofan
```

### 3. Try it without installing

```bash
sudo python3 vivofan.py --status
```

This shows the temperatures, fan speed and current mode. To watch what vivofan
*would* do without changing anything (Ctrl-C to stop):

```bash
sudo python3 vivofan.py --dry-run --config vivofan.toml -v
```

### 4. Install

```bash
sudo sh install.sh
```

vivofan now runs in the background and starts at every boot. The installer
turns off `power-profiles-daemon`, which would otherwise fight vivofan over the
fan mode, so the power-mode menu in your panel stops having an effect.

Watch it work:

```bash
journalctl -u vivofan -f
```

### 5. Optional: turn on the real fan curve

Open the settings file:

```bash
sudo nano /etc/vivofan.toml
```

Change `fan_control = "modes"` to `fan_control = "ec"`, save (Ctrl-O, Enter,
Ctrl-X), and restart:

```bash
sudo systemctl restart vivofan
```

The default curve:

| CPU temperature | Fan speed |
|---|---|
| under 50 °C | the laptop's own control (fan stops at idle) |
| 50 °C | 30% (~2700 rpm) |
| 60 °C | 40% |
| 70 °C | 60% (~4900 rpm) |
| 80 °C | 80% |
| 88 °C and up | 100% (~7250 rpm) |

The CPU fan follows the CPU temperature and the GPU fan the GPU's (or the
CPU's while the GPU sleeps). On battery the fans are capped at 60%
(`battery_max_percent`). You can change the curves with `fan_curve` and
`gpu_fan_curve` in `/etc/vivofan.toml`. If the CPU gets too
hot or its temperature can't be read, the fans go to 100%. When vivofan stops,
even after a crash, the fans go back to the laptop's own control.

### Everyday use

- **Tray icon:** shows temperatures, both fan speeds and the mode; its menu has
  quick overrides. It starts at login (or run `vivofan-tray`).
- **Force a speed for a while:** `sudo vivofan --set 100 --minutes 10`, and
  `sudo vivofan --set auto` to go back. With fan modes instead of the curve:
  `sudo vivofan --set performance`.
- **See what it's doing:** `vivofan --status`.

### Undo

Hand the fans back to the laptop right away:

```bash
sudo vivofan --release
```

Remove vivofan completely and turn `power-profiles-daemon` back on (run from
the `vivofan/vivofan` folder):

```bash
sudo sh uninstall.sh
```

If you installed the `.deb`: `sudo apt remove vivofan`.

## Got a different ASUS laptop?

The direct fan curve only runs on boards that have been checked. Help add
yours: run the read-only probe and paste its output into a
[new issue](https://github.com/Poopoomanu/vivofan/issues/new).

```bash
sudo python3 vivofan.py --probe
```

It only reads fan registers (its one write picks which fan to read), never
changes fan control, and asks before it touches the hardware. The fan modes
part of vivofan already works on most recent ASUS laptops.

## Other Linux tips for this laptop

- **NVIDIA RTX 3050:** Linux Mint starts on the open `nouveau` driver, which is
  slow. Install NVIDIA's own driver in **Driver Manager** (pick the recommended
  `nvidia-driver-…`) and reboot. The GPU then works properly and powers off
  when idle, so the laptop runs cooler.
- **Thunderbolt 4 / USB4:** one USB-C port supports Thunderbolt (look for the ⚡
  icon). New Thunderbolt devices need approving once: find the device with
  `boltctl list`, then `sudo boltctl enroll <device-id>`.
- **thermald** should be running (`systemctl status thermald`). Mint has it on
  by default.

## What's in this repo

- **`vivofan/`**: the daemon, its settings, installer and tests. Details in
  [`vivofan/README.md`](vivofan/README.md).
- **[`FINDINGS.md`](FINDINGS.md)**: how fan control works on this laptop: the
  firmware's WMI fan modes, the broken ACPI fan, and the MyASUS EC channel
  (ports `0x25C`/`0x25D`) that allows 30–100% duty on both fans.
- **`research/`**: the scripts used to verify the EC channel
  (read-only cross-check, supervised full-speed test, duty sweep).

The EC protocol comes from
[Keyitdev/asus-fan-control-ec](https://github.com/Keyitdev/asus-fan-control-ec).

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
