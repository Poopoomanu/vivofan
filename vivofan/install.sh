#!/bin/sh
# Install vivofan as a systemd service. Run as root from this folder.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo ./install.sh" >&2
    exit 1
fi

cd "$(dirname "$0")"

# Refuse to install if there is no fan mode interface to drive.
python3 vivofan.py --status --config vivofan.toml

install -m 0755 vivofan.py /usr/local/bin/vivofan
if [ -e /etc/vivofan.toml ]; then
    echo "keeping existing /etc/vivofan.toml"
else
    install -m 0644 vivofan.toml /etc/vivofan.toml
fi
install -m 0644 vivofan.service /etc/systemd/system/vivofan.service

# These also write platform_profile. Mask them, not just stop them: they are
# D-Bus activated, and starting one would stop vivofan through Conflicts=.
# uninstall.sh unmasks them.
for svc in power-profiles-daemon tuned-ppd; do
    if systemctl cat "$svc.service" >/dev/null 2>&1; then
        echo "masking $svc (it also writes platform_profile)"
        systemctl disable --now "$svc.service" 2>/dev/null || true
        systemctl mask "$svc.service"
    fi
done

systemctl daemon-reload
systemctl enable vivofan.service
systemctl restart vivofan.service  # picks up a new vivofan.py on reinstall
systemctl --no-pager status vivofan.service
echo "logs: journalctl -u vivofan -f"
