#!/bin/bash
# Build the ptalk-signature-native .deb on the Pi (run from the source root).
set -e
SRC="$(cd "$(dirname "$0")" && pwd)"
VER=0.6.1
PKG=ptalk-signature-native
OUT="$HOME/ptalk-build"
STAGE="$OUT/${PKG}_${VER}"

echo "=== syntax check ==="
python3 -m py_compile "$SRC/ptalk/"*.py
echo "OK"

rm -rf "$OUT"
mkdir -p "$STAGE"

install -d "$STAGE/opt/ptalk-signature/ptalk"
install -m 644 "$SRC/ptalk/"*.py "$STAGE/opt/ptalk-signature/ptalk/"

# bundle UI assets (character images + logos)
install -d "$STAGE/opt/ptalk-signature/assets"
install -m 644 "$SRC/assets_src/"*.png "$STAGE/opt/ptalk-signature/assets/"

install -d "$STAGE/usr/bin"
install -m 755 "$SRC/pkg/ptalk-signature" "$STAGE/usr/bin/ptalk-signature"

install -d "$STAGE/usr/share/applications"
install -m 644 "$SRC/pkg/ptalk-signature.desktop" "$STAGE/usr/share/applications/"

install -d "$STAGE/etc/ptalk-signature"
install -m 644 "$SRC/pkg/config.toml" "$STAGE/etc/ptalk-signature/config.toml"

install -d "$STAGE/lib/systemd/system"
install -m 644 "$SRC/pkg/ptalk-signature-kiosk.service" "$STAGE/lib/systemd/system/"

# polkit rule: let netdev/sudo (local session) control Wi-Fi from the app
install -d "$STAGE/etc/polkit-1/rules.d"
install -m 644 "$SRC/pkg/eldercare-nm.rules" "$STAGE/etc/polkit-1/rules.d/50-eldercare-nm.rules"

install -d "$STAGE/DEBIAN"
install -m 644 "$SRC/pkg/control" "$STAGE/DEBIAN/control"
install -m 755 "$SRC/pkg/postinst" "$STAGE/DEBIAN/postinst"
echo "/etc/ptalk-signature/config.toml" > "$STAGE/DEBIAN/conffiles"

dpkg-deb --root-owner-group --build "$STAGE"
DEB="$OUT/${PKG}_${VER}.deb"
echo
echo "=== BUILT: $DEB ==="
ls -l "$DEB"
echo
echo "=== dpkg-deb -I ==="
dpkg-deb -I "$DEB"
echo
echo "=== contents ==="
dpkg-deb -c "$DEB"
