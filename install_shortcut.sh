#!/usr/bin/env bash
# Install the Holiday icon and a desktop entry so the taskbar shows the logo instead of the generic icon.
#
#   ./install_shortcut.sh
#
# What it does (per user, no root needed):
#   1. copies the PNG icons to ~/.local/share/icons/hicolor/<size>/apps/qa-tools.png
#   2. writes ~/.local/share/applications/qa-tools.desktop, which starts the QA Tools window (./qa)
#   3. removes the "FM26 CAN Monitor" entry and icons that an earlier version of this script installed
#   4. refreshes the icon and desktop caches when the tools are present
#
# The taskbar matches a window to this entry by its app_id / WM_CLASS "qa-tools",
# which the app already sets. Log out and back in (or restart the compositor) if the
# taskbar still shows the old icon for an already-running window.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
icons=$here/src/qatools/shell/icons

apps=${XDG_DATA_HOME:-$HOME/.local/share}/applications
icondir=${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor
mkdir -p "$apps"

for s in 16 24 32 48 64 128 256; do
  dst=$icondir/${s}x${s}/apps
  mkdir -p "$dst"
  install -m 0644 "$icons/logo-$s.png" "$dst/qa-tools.png"
  rm -f "$dst/canmon.png"
done
rm -f "$apps/canmon.desktop"

# Icon points at an absolute PNG path, not a theme name, so WSLg does not depend on an icon-theme lookup.
cat > "$apps/qa-tools.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=QA Tools
Comment=Holiday Robotics verification tools (CAN Monitor)
Exec=$here/qa
Icon=$icondir/256x256/apps/qa-tools.png
Terminal=false
Categories=Utility;Engineering;
StartupWMClass=qa-tools
DESKTOP
chmod 0644 "$apps/qa-tools.desktop"

command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t "$icondir" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$apps" 2>/dev/null || true

echo "Installed icon and launcher."
echo "  icons   -> $icondir/<size>/apps/qa-tools.png"
echo "  desktop -> $apps/qa-tools.desktop"
echo "If a window is already open, close and reopen it so the taskbar picks up the new icon."
