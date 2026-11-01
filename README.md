# vivofan

Fan control for the ASUS Vivobook Pro 15 (K6502VJ / Q530VJ, board K6502VJQ) on Linux.

- **`vivofan/`**: the daemon. Switches the firmware's three fan modes by
  temperature, or (`fan_control = "ec"`) drives fan duty directly with a real
  fan curve. See [`vivofan/README.md`](vivofan/README.md).
- **[`FINDINGS.md`](FINDINGS.md)**: how fan control works on this laptop: the
  firmware's WMI fan modes, the broken ACPI fan, and the MyASUS EC channel
  (ports `0x25C`/`0x25D`) that allows 30–100% duty on both fans.
- **`research/`**: the scripts used to verify the EC channel
  (read-only cross-check, supervised full-speed test, duty sweep).

> **Warning:** direct EC fan control is tested only on board `K6502VJQ`, and
> vivofan refuses to use it on any other board. Writing to an embedded
> controller can affect more than fans. Use at your own risk.

The EC protocol comes from
[Keyitdev/asus-fan-control-ec](https://github.com/Keyitdev/asus-fan-control-ec).

## License

GPL-3.0-or-later. See [`LICENSE`](LICENSE).
