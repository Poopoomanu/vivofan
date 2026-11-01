#!/usr/bin/env python3
"""vivofan-settings - edit vivofan's fan curves with the mouse.

Drag the dots on the CPU and GPU fan curves, pick a preset, set the battery
cap, then Apply: the new settings go to `pkexec vivofan --install-config`,
which validates them, installs /etc/vivofan.toml and restarts the service.
Needs python3-gi (GTK 3).
"""

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CONFIG = Path("/etc/vivofan.toml")
DEFAULT_CONFIG = Path(__file__).resolve().parent / "vivofan.toml"
STATUS = Path("/run/vivofan/status.json")
EC_BOARDS = {"K6502VJQ"}
MIN_PERCENT = 30
T_MIN, T_MAX = 30, 100  # editable temperature range, °C

PRESETS = {
    "Silent": [[55, 30], [65, 35], [75, 50], [85, 75], [92, 100]],
    "Balanced": [[50, 30], [60, 40], [70, 60], [80, 80], [88, 100]],
    "Cool": [[45, 35], [55, 50], [65, 70], [75, 90], [82, 100]],
    "Max": [[40, 60], [50, 75], [60, 90], [70, 100]],
}


def move_point(curve, i, temp, pct):
    """Return a copy of `curve` with point i moved to (temp, pct), kept valid.

    Temperatures stay strictly increasing (1 °C apart) inside T_MIN..T_MAX;
    percents never fall (a fan curve that drops makes no sense) and stay in
    MIN_PERCENT..100.
    """
    pts = [list(p) for p in curve]
    lo_t = pts[i - 1][0] + 1 if i > 0 else T_MIN
    hi_t = pts[i + 1][0] - 1 if i + 1 < len(pts) else T_MAX
    lo_p = pts[i - 1][1] if i > 0 else MIN_PERCENT
    hi_p = pts[i + 1][1] if i + 1 < len(pts) else 100
    pts[i] = [int(round(min(max(temp, lo_t), hi_t))), int(round(min(max(pct, lo_p), hi_p)))]
    return pts


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return str(v)


def update_config_text(text, values):
    """Set `key = value` lines in a TOML text, keeping comments; append missing keys."""
    for key, value in values.items():
        line = f"{key} = {toml_value(value)}"
        pattern = re.compile(rf"^{re.escape(key)}\s*=[^#\n]*?(\s*#.*)?$", re.M)
        if pattern.search(text):
            text = pattern.sub(lambda m: line + (m.group(1) or ""), text, count=1)
        else:
            text = text.rstrip("\n") + f"\n{line}\n"
    return text


def load_settings(path):
    """The keys the editor changes, read from a vivofan.toml (defaults if missing)."""
    import tomllib
    data = {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, ValueError):
        pass
    return {
        "fan_control": data.get("fan_control", "modes"),
        "fan_curve": data.get("fan_curve", PRESETS["Balanced"]),
        "gpu_fan_curve": data.get("gpu_fan_curve", []),
        "battery_max_percent": int(data.get("battery_max_percent", 60)),
    }


def current_temps():
    try:
        st = json.loads(STATUS.read_text())
        return st.get("cpu"), st.get("gpu")
    except (OSError, ValueError):
        return None, None


def board():
    try:
        return Path("/sys/class/dmi/id/board_name").read_text().strip()
    except OSError:
        return "?"


def vivofan_cmd():
    for c in (shutil.which("vivofan"), "/usr/local/bin/vivofan", "/usr/bin/vivofan"):
        if c and Path(c).exists():
            return c
    return None


def main():
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gdk, GLib, Gtk

    source = CONFIG if CONFIG.exists() else DEFAULT_CONFIG
    base_text = source.read_text() if source.exists() else ""
    saved = load_settings(source)
    ec_ok = board() in EC_BOARDS
    s = {
        "fan_control": saved["fan_control"],
        "curves": [saved["fan_curve"], saved["gpu_fan_curve"] or [list(p) for p in saved["fan_curve"]]],
        "gpu_same": not saved["gpu_fan_curve"],
        "battery": saved["battery_max_percent"],
        "fan": 0, "drag": None, "temps": current_temps(),
    }

    win = Gtk.Window(title="vivofan settings")
    win.set_default_size(620, 560)
    win.connect("destroy", Gtk.main_quit)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, margin=14)
    win.add(box)

    # Fan control mode
    mode_box = Gtk.Box(spacing=12)
    mode_box.pack_start(Gtk.Label(label="Fan control:"), False, False, 0)
    rb_modes = Gtk.RadioButton.new_with_label(None, "Firmware modes")
    rb_ec = Gtk.RadioButton.new_with_label_from_widget(rb_modes, "Custom fan curve")
    rb_ec.set_active(s["fan_control"] == "ec")
    rb_ec.set_sensitive(ec_ok)
    if not ec_ok:
        rb_ec.set_tooltip_text(f"Only tested on {', '.join(sorted(EC_BOARDS))}; this board is {board()}")
    mode_box.pack_start(rb_modes, False, False, 0)
    mode_box.pack_start(rb_ec, False, False, 0)
    box.pack_start(mode_box, False, False, 0)

    # CPU / GPU switch
    fan_box = Gtk.Box(spacing=12)
    rb_cpu = Gtk.RadioButton.new_with_label(None, "CPU fan")
    rb_gpu = Gtk.RadioButton.new_with_label_from_widget(rb_cpu, "GPU fan")
    same = Gtk.CheckButton(label="GPU fan uses the CPU curve")
    same.set_active(s["gpu_same"])
    for w in (rb_cpu, rb_gpu, same):
        fan_box.pack_start(w, False, False, 0)
    box.pack_start(fan_box, False, False, 0)

    area = Gtk.DrawingArea()
    area.set_size_request(560, 300)
    area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                    | Gdk.EventMask.POINTER_MOTION_MASK)
    box.pack_start(area, True, True, 0)

    hint = Gtk.Label(xalign=0)
    hint.set_line_wrap(True)
    box.pack_start(hint, False, False, 0)

    preset_box = Gtk.Box(spacing=6)
    preset_box.pack_start(Gtk.Label(label="Presets:"), False, False, 0)
    box.pack_start(preset_box, False, False, 0)

    bat_box = Gtk.Box(spacing=8)
    bat_box.pack_start(Gtk.Label(label="Max fan speed on battery"), False, False, 0)
    bat = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, MIN_PERCENT, 100, 5)
    bat.set_value(s["battery"])
    bat.set_hexpand(True)
    bat.connect("format-value", lambda _s, v: f"{v:.0f}%")
    bat_box.pack_start(bat, True, True, 0)
    box.pack_start(bat_box, False, False, 0)

    buttons = Gtk.Box(spacing=8)
    reset = Gtk.Button(label="Reset")
    apply = Gtk.Button(label="Apply")
    apply.get_style_context().add_class("suggested-action")
    buttons.pack_end(apply, False, False, 0)
    buttons.pack_end(reset, False, False, 0)
    box.pack_start(buttons, False, False, 0)

    def editing():
        """Index of the curve being edited (GPU edits the CPU curve while 'same' is on)."""
        return 0 if s["gpu_same"] else s["fan"]

    def geometry():
        w, h = area.get_allocated_width(), area.get_allocated_height()
        left, right, top, bottom = 48, 16, 14, 34
        def to_xy(t, p):
            return (left + (t - T_MIN) / (T_MAX - T_MIN) * (w - left - right),
                    top + (100 - p) / 100 * (h - top - bottom))
        def to_tp(x, y):
            return (T_MIN + (x - left) / (w - left - right) * (T_MAX - T_MIN),
                    100 - (y - top) / (h - top - bottom) * 100)
        return to_xy, to_tp, left, right, top, bottom, w, h

    def update_hint():
        curve = s["curves"][editing()]
        which = "CPU and GPU fans" if s["gpu_same"] else ("CPU fan" if s["fan"] == 0 else "GPU fan")
        text = (f"Editing the {which}. Drag the dots. Below {curve[0][0]} °C the laptop's own curve "
                f"runs (the fan can stop at idle). Never below {MIN_PERCENT}%; 85 °C or an unreadable "
                f"sensor always means 100%.")
        if not rb_ec.get_active():
            text = "Firmware modes are selected: the curve below is saved but not used. " + text
        hint.set_text(text)

    def draw(_w, cr):
        to_xy, _tp, left, right, top, bottom, w, h = geometry()
        fg = area.get_style_context().get_color(Gtk.StateFlags.NORMAL)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        x0, y30 = to_xy(T_MIN, MIN_PERCENT)
        x1, y0 = to_xy(T_MAX, 0)
        cr.rectangle(x0, y30, x1 - x0, y0 - y30)  # below the 30% floor
        cr.fill()
        cr.set_line_width(1)
        cr.select_font_face("Sans")
        cr.set_font_size(10)
        for t in range(T_MIN, T_MAX + 1, 10):
            x, _ = to_xy(t, 0)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
            cr.move_to(x, top); cr.line_to(x, h - bottom); cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.7)
            cr.move_to(x - 10, h - bottom + 14); cr.show_text(f"{t}°")
        for p in range(0, 101, 20):
            _, y = to_xy(T_MIN, p)
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
            cr.move_to(left, y); cr.line_to(w - right, y); cr.stroke()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.7)
            cr.move_to(6, y + 4); cr.show_text(f"{p}%")
        # current temperature
        temp = s["temps"][1 if s["fan"] == 1 and s["temps"][1] is not None else 0]
        if temp is not None and T_MIN <= temp <= T_MAX:
            x, _ = to_xy(temp, 0)
            cr.set_source_rgba(0.95, 0.45, 0.2, 0.9)
            cr.set_dash([4, 3]); cr.move_to(x, top); cr.line_to(x, h - bottom); cr.stroke(); cr.set_dash([])
            cr.move_to(x + 4, top + 12); cr.show_text(f"now {temp:.0f}°")
        # the curve: flat 100% after the last point
        curve = s["curves"][editing()]
        cr.set_source_rgba(0.25, 0.6, 1.0, 1)
        cr.set_line_width(2.5)
        pts = [to_xy(t, p) for t, p in curve]
        cr.move_to(*pts[0])
        for x, y in pts[1:]:
            cr.line_to(x, y)
        cr.line_to(to_xy(T_MAX, curve[-1][1])[0], pts[-1][1])
        cr.stroke()
        for i, (x, y) in enumerate(pts):
            cr.arc(x, y, 7 if s["drag"] == i else 5.5, 0, 6.2832)
            cr.fill()
            t, p = curve[i]
            # Below-right of the dot: the curve only rises, so that spot is clear.
            cr.move_to(x + 7, y + 16); cr.show_text(f"{t}° {p}%")
        if curve[0][0] > T_MIN + 8:
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            cr.move_to(left + 6, to_xy(T_MIN, MIN_PERCENT)[1] + 16)
            cr.show_text("laptop's own curve")

    def nearest(x, y):
        to_xy = geometry()[0]
        best, dist = None, 14 ** 2
        for i, (t, p) in enumerate(s["curves"][editing()]):
            px, py = to_xy(t, p)
            d = (px - x) ** 2 + (py - y) ** 2
            if d < dist:
                best, dist = i, d
        return best

    def on_press(_w, ev):
        s["drag"] = nearest(ev.x, ev.y)
        area.queue_draw()

    def on_motion(_w, ev):
        if s["drag"] is None:
            return
        t, p = geometry()[1](ev.x, ev.y)
        s["curves"][editing()] = move_point(s["curves"][editing()], s["drag"], t, p)
        area.queue_draw()

    def on_release(_w, _ev):
        s["drag"] = None
        area.queue_draw()

    def set_preset(_b, name):
        s["curves"][editing()] = [list(p) for p in PRESETS[name]]
        area.queue_draw()
        update_hint()

    for name in PRESETS:
        b = Gtk.Button(label=name)
        b.connect("clicked", set_preset, name)
        preset_box.pack_start(b, False, False, 0)

    def on_fan(_b):
        s["fan"] = 1 if rb_gpu.get_active() else 0
        area.queue_draw()
        update_hint()

    def on_same(_b):
        s["gpu_same"] = same.get_active()
        if not s["gpu_same"]:
            s["curves"][1] = [list(p) for p in s["curves"][0]]
        area.queue_draw()
        update_hint()

    def on_reset(_b):
        fresh = load_settings(source)
        s["curves"] = [fresh["fan_curve"], fresh["gpu_fan_curve"] or [list(p) for p in fresh["fan_curve"]]]
        s["gpu_same"] = not fresh["gpu_fan_curve"]
        same.set_active(s["gpu_same"])
        rb_ec.set_active(fresh["fan_control"] == "ec")
        rb_modes.set_active(fresh["fan_control"] != "ec")
        bat.set_value(fresh["battery_max_percent"])
        area.queue_draw()
        update_hint()

    def message(kind, title, text):
        d = Gtk.MessageDialog(transient_for=win, modal=True, message_type=kind,
                              buttons=Gtk.ButtonsType.OK, text=title)
        d.format_secondary_text(text)
        d.run()
        d.destroy()

    def on_apply(_b):
        cmd = vivofan_cmd()
        if not cmd:
            message(Gtk.MessageType.ERROR, "vivofan is not installed", "Install vivofan first.")
            return
        values = {
            "fan_control": "ec" if rb_ec.get_active() else "modes",
            "fan_curve": s["curves"][0],
            "gpu_fan_curve": [] if s["gpu_same"] else s["curves"][1],
            "battery_max_percent": int(bat.get_value()),
        }
        with tempfile.NamedTemporaryFile("w", suffix=".toml", prefix="vivofan-", delete=False) as f:
            f.write(update_config_text(base_text, values))
            tmp = f.name
        Path(tmp).chmod(0o644)
        r = subprocess.run(["pkexec", cmd, "--install-config", tmp], capture_output=True, text=True)
        Path(tmp).unlink(missing_ok=True)
        if r.returncode == 0:
            message(Gtk.MessageType.INFO, "Applied", "vivofan restarted with the new settings.")
        elif r.returncode in (126, 127):
            pass  # password dialog cancelled
        else:
            message(Gtk.MessageType.ERROR, "Not applied", (r.stderr or r.stdout).strip()[-400:])

    def tick():
        s["temps"] = current_temps()
        area.queue_draw()
        return True

    area.connect("draw", draw)
    area.connect("button-press-event", on_press)
    area.connect("motion-notify-event", on_motion)
    area.connect("button-release-event", on_release)
    rb_cpu.connect("toggled", on_fan)
    same.connect("toggled", on_same)
    rb_ec.connect("toggled", lambda _b: update_hint())
    reset.connect("clicked", on_reset)
    apply.connect("clicked", on_apply)
    GLib.timeout_add_seconds(2, tick)
    update_hint()
    win.show_all()
    Gtk.main()


if __name__ == "__main__":
    sys.exit(main())
