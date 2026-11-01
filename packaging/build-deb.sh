#!/bin/sh
# Build dist/vivofan_<version>_all.deb from this repo. Needs dpkg-deb (no root).
set -eu

here=$(cd "$(dirname "$0")/.." && pwd)
src="$here/vivofan"
version=$(python3 "$src/vivofan.py" --version | cut -d' ' -f2)
pkg=$(mktemp -d)
trap 'rm -rf "$pkg"' EXIT

install -D -m 0755 "$src/vivofan.py" "$pkg/usr/bin/vivofan"
install -D -m 0755 "$src/vivofan-tray.py" "$pkg/usr/bin/vivofan-tray"
install -D -m 0644 "$src/vivofan.svg" "$pkg/usr/share/vivofan/vivofan.svg"
install -D -m 0644 "$src/vivofan.toml" "$pkg/etc/vivofan.toml"
mkdir -p "$pkg/usr/lib/systemd/system" "$pkg/etc/xdg/autostart" "$pkg/usr/share/doc/vivofan" "$pkg/DEBIAN"
sed 's#/usr/local/bin/#/usr/bin/#g' "$src/vivofan.service" > "$pkg/usr/lib/systemd/system/vivofan.service"
sed 's#/usr/local/share/#/usr/share/#g' "$src/vivofan-tray.desktop" > "$pkg/etc/xdg/autostart/vivofan-tray.desktop"
chmod 0644 "$pkg/usr/lib/systemd/system/vivofan.service" "$pkg/etc/xdg/autostart/vivofan-tray.desktop"
install -m 0644 "$here/README.md" "$here/FINDINGS.md" "$pkg/usr/share/doc/vivofan/"
cat > "$pkg/usr/share/doc/vivofan/copyright" <<'EOF'
vivofan - https://github.com/Poopoomanu/vivofan
License: GPL-3.0-or-later. On Debian systems the full text is in
/usr/share/common-licenses/GPL-3.
EOF

cat > "$pkg/DEBIAN/control" <<EOF
Package: vivofan
Version: $version
Architecture: all
Maintainer: Poopoomanu <127021170+Poopoomanu@users.noreply.github.com>
Depends: python3 (>= 3.11), systemd
Recommends: python3-gi, gir1.2-ayatanaappindicator3-0.1, pkexec | policykit-1
Section: utils
Priority: optional
Homepage: https://github.com/Poopoomanu/vivofan
Description: fan control for the ASUS Vivobook Pro 15 (K6502VJ / Q530VJ)
 Switches the firmware fan modes by temperature and, optionally, drives both
 fans directly with a real fan curve over the MyASUS EC channel. Includes a
 tray icon. Direct fan control only runs on board K6502VJQ.
EOF

printf '/etc/vivofan.toml\n/etc/xdg/autostart/vivofan-tray.desktop\n' > "$pkg/DEBIAN/conffiles"

cat > "$pkg/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = configure ] && [ -d /run/systemd/system ]; then
    if [ -e /etc/systemd/system/vivofan.service ]; then
        echo "note: /etc/systemd/system/vivofan.service (from install.sh) overrides this package;"
        echo "      remove it with the repo's uninstall.sh, then reinstall this package."
    fi
    # These also write platform_profile and would fight vivofan (postrm unmasks them).
    for svc in power-profiles-daemon tuned-ppd; do
        if systemctl cat "$svc.service" >/dev/null 2>&1; then
            systemctl disable --now "$svc.service" 2>/dev/null || true
            systemctl mask "$svc.service" || true
        fi
    done
    systemctl daemon-reload
    systemctl enable vivofan.service
    systemctl restart vivofan.service || true
fi
EOF

cat > "$pkg/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
# Stopping runs ExecStopPost, which hands the fans back to the laptop.
if [ "$1" = remove ] && [ -d /run/systemd/system ]; then
    systemctl disable --now vivofan.service || true
fi
EOF

cat > "$pkg/DEBIAN/postrm" <<'EOF'
#!/bin/sh
set -e
if { [ "$1" = remove ] || [ "$1" = purge ]; } && [ -d /run/systemd/system ]; then
    systemctl daemon-reload || true
    for svc in power-profiles-daemon tuned-ppd; do
        if [ "$(systemctl is-enabled "$svc.service" 2>/dev/null)" = masked ]; then
            systemctl unmask "$svc.service" || true
        fi
    done
    if systemctl cat power-profiles-daemon.service >/dev/null 2>&1; then
        systemctl enable --now power-profiles-daemon.service || true
    fi
fi
EOF
chmod 0755 "$pkg/DEBIAN/postinst" "$pkg/DEBIAN/prerm" "$pkg/DEBIAN/postrm"
chmod 0644 "$pkg/usr/share/doc/vivofan/copyright"
# mktemp -d is 0700 and the umask may leave dirs group-writable; dpkg must not
# carry either onto real system directories.
find "$pkg" -type d -exec chmod 0755 {} +

mkdir -p "$here/dist"
out="$here/dist/vivofan_${version}_all.deb"
dpkg-deb --root-owner-group --build "$pkg" "$out" >/dev/null
echo "$out"
