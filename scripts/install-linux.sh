#!/usr/bin/env bash
# Install a ClAudit desktop launcher (and optionally autostart) for the current user.
# Paths are detected at install time, so nothing machine-specific is committed to the repo.
#
#   ./scripts/install-linux.sh              # start-menu launcher only
#   ./scripts/install-linux.sh --autostart  # also start on login
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
DESKTOP="$APPS/claudit.desktop"
mkdir -p "$APPS"

cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=ClAudit
Comment=Watch Claude Code for false-positive safety/AUP blocks
Exec=$PY $DIR/claudit_gui.py --interval 30
Icon=$DIR/claudit_icon.png
Terminal=false
Categories=Development;Utility;
StartupNotify=false
EOF
echo "Installed launcher  -> $DESKTOP"

if [ "${1:-}" = "--autostart" ]; then
  AS="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
  mkdir -p "$AS"
  cp "$DESKTOP" "$AS/claudit.desktop"
  echo "Enabled autostart   -> $AS/claudit.desktop"
fi

update-desktop-database "$APPS" 2>/dev/null || true
echo "Done. Launch 'ClAudit' from your app menu (notify-only by default; add --auto to auto-file)."
