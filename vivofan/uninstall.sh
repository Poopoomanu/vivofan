#!/bin/sh
# Remove vivofan and give fan modes back to power-profiles-daemon. Run as root.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "run as root: sudo sh uninstall.sh" >&2
    exit 1
fi

systemctl disable --now vivofan.service 2>/dev/null || true
rm -f /etc/systemd/system/vivofan.service /usr/local/bin/vivofan
echo "kept /etc/vivofan.toml (delete it yourself if you don't want it)"
systemctl daemon-reload

for svc in power-profiles-daemon tuned-ppd; do
    if [ "$(systemctl is-enabled "$svc.service" 2>/dev/null)" = masked ]; then
        echo "unmasking $svc"
        systemctl unmask "$svc.service"
    fi
done
if systemctl cat power-profiles-daemon.service >/dev/null 2>&1; then
    systemctl enable --now power-profiles-daemon.service
fi
echo "fan mode: $(cat /sys/firmware/acpi/platform_profile 2>/dev/null || echo ?)"
