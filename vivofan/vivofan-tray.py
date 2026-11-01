#!/usr/bin/env python3
"""vivofan-tray - panel icon for vivofan: temperatures, fan speeds, mode, quick overrides.

Reads the status the vivofan service publishes in /run/vivofan/status.json
(falls back to plain sysfs if the service isn't running). The override items
run `pkexec vivofan --set ...`, which asks for your password.
Needs python3-gi and gir1.2-ayatanaappindicator3-0.1.
"""

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

STATUS = Path("/run/vivofan/status.json")
ICON_DIRS = [Path(__file__).resolve().parent, Path("/usr/local/share/vivofan"), Path("/usr/share/vivofan")]
REFRESH_S = 2
STALE_S = 30


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def sysfs_fallback(root="/"):
    """What we can see without the service: hottest core, CPU fan, firmware mode."""
    temps, rpm = [], None
    base = Path(root, "sys/class/hwmon")
    for hw in sorted(base.iterdir()) if base.is_dir() else []:
        name = read_text(hw / "name")
        if name == "coretemp":
            for f in hw.glob("temp*_input"):
                raw = read_text(f)
                if raw and raw.isdigit():
                    temps.append(int(raw) / 1000)
        elif name == "asus":
            raw = read_text(hw / "fan1_input")
            rpm = int(raw) if raw and raw.isdigit() else None
    return {"cpu": max(temps) if temps else None, "rpm": rpm,
            "mode": read_text(Path(root, "sys/firmware/acpi/platform_profile"))}


def load_status(path=STATUS, now=None):
    """The service's status dict, or None if it's missing or stale."""
    try:
        st = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    now = time.time() if now is None else now
    return st if now - st.get("time", 0) <= STALE_S else None


def deg(t):
    return "?" if t is None else f"{t:.0f} °C"


def summarize(st, fallback, now=None):
    """Panel label and menu lines for a status dict (or None plus the sysfs fallback)."""
    now = time.time() if now is None else now
    if st is None:
        return {
            "label": f"{deg(fallback['cpu'])}",
            "lines": [f"CPU {deg(fallback['cpu'])}",
                      f"CPU fan {fallback['rpm'] if fallback['rpm'] is not None else '?'} rpm",
                      f"Mode {fallback['mode'] or '?'}",
                      "vivofan service is not running"],
            "fan_control": None,
        }
    temps = f"CPU {deg(st.get('cpu'))}"
    if st.get("gpu") is not None:
        temps += f" · GPU {deg(st['gpu'])}"
    lines = [temps, f"Smoothed {st.get('smoothed', '?')} °C"]
    for f in st.get("fans", []):
        if f.get("rpm") is None and f.get("percent") is None:
            continue
        txt = f"{f['name'].upper()} fan {f['rpm'] if f.get('rpm') is not None else '?'} rpm"
        if f.get("percent") is not None:
            txt += f" · {f['percent']}%"
        lines.append(txt)
    if st.get("fan_control") == "ec":
        lines.append("Fan curve: " + ("laptop's own (idle)" if st.get("ec_auto") else "vivofan"))
    else:
        lines.append(f"Mode {st.get('mode', '?')}")
    ov = st.get("override")
    if ov:
        unit = "%" if st.get("fan_control") == "ec" else ""
        lines.append(f"Override {ov['value']}{unit} for {max(0, ov['until'] - now) / 60:.0f} more min")
    if st.get("on_ac") is False:
        lines.append("On battery")
    cpu_fan = next((f for f in st.get("fans", []) if f["name"] == "cpu"), {})
    label = deg(st.get("cpu"))
    if cpu_fan.get("rpm") is not None:
        label += f" · {cpu_fan['rpm']} rpm"
    return {"label": label, "lines": lines, "fan_control": st.get("fan_control")}


def override_actions(fan_control):
    """(menu text, vivofan --set args) for the quick actions."""
    if fan_control == "ec":
        return [("Fans automatic", ["auto"]),
                ("Fans 60% for 30 min", ["60", "--minutes", "30"]),
                ("Fans 100% for 10 min", ["100", "--minutes", "10"])]
    return [("Mode automatic", ["auto"]),
            ("Quiet for 30 min", ["quiet", "--minutes", "30"]),
            ("Performance for 30 min", ["performance", "--minutes", "30"])]


def vivofan_cmd():
    for c in (shutil.which("vivofan"), "/usr/local/bin/vivofan", "/usr/bin/vivofan"):
        if c and Path(c).exists():
            return c
    return None


def main():
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator, GLib, Gtk

    icon_dir = next((d for d in ICON_DIRS if (d / "vivofan.svg").exists()), None)
    ind = AppIndicator.Indicator.new("vivofan", "vivofan" if icon_dir else "dialog-information",
                                     AppIndicator.IndicatorCategory.HARDWARE)
    if icon_dir:
        ind.set_icon_theme_path(str(icon_dir))
    ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
    ind.set_title("vivofan")

    menu = Gtk.Menu()
    info_items, action_items = [], []
    state = {"fan_control": "unset"}

    def run_set(_item, args):
        cmd = vivofan_cmd()
        if cmd:
            subprocess.Popen(["pkexec", cmd, "--set", *args])

    def show_log(_item):
        term = shutil.which("x-terminal-emulator") or shutil.which("gnome-terminal")
        if term:
            subprocess.Popen([term, "-e", "journalctl -u vivofan -f"])

    def open_settings(_item):
        cmd = shutil.which("vivofan-settings")
        if cmd:
            subprocess.Popen([cmd])

    def rebuild_actions(fan_control):
        for item in action_items:
            menu.remove(item)
        action_items.clear()
        entries = [Gtk.SeparatorMenuItem()]
        for text, args in override_actions(fan_control):
            item = Gtk.MenuItem(label=text)
            item.connect("activate", run_set, args)
            entries.append(item)
        entries.append(Gtk.SeparatorMenuItem())
        settings_item = Gtk.MenuItem(label="Fan curve settings…")
        settings_item.connect("activate", open_settings)
        entries.append(settings_item)
        log_item = Gtk.MenuItem(label="Show log")
        log_item.connect("activate", show_log)
        quit_item = Gtk.MenuItem(label="Hide icon")
        quit_item.connect("activate", lambda _i: Gtk.main_quit())
        entries += [log_item, quit_item]
        for item in entries:
            menu.append(item)
            item.show()
            action_items.append(item)

    def refresh():
        s = summarize(load_status(), sysfs_fallback())
        while len(info_items) < len(s["lines"]):
            item = Gtk.MenuItem(label="")
            item.set_sensitive(False)
            menu.insert(item, len(info_items))
            item.show()
            info_items.append(item)
        for i, item in enumerate(info_items):
            if i < len(s["lines"]):
                item.set_label(s["lines"][i])
                item.show()
            else:
                item.hide()
        if s["fan_control"] != state["fan_control"]:
            state["fan_control"] = s["fan_control"]
            rebuild_actions(s["fan_control"])
        ind.set_label(s["label"], "000 °C · 0000 rpm")
        return True

    refresh()
    ind.set_menu(menu)
    GLib.timeout_add_seconds(REFRESH_S, refresh)
    Gtk.main()


if __name__ == "__main__":
    sys.exit(main())
