#!/usr/bin/env bash
# Install ClAudit as a macOS Login Item (launchd user agent), started minimized to the menu bar.
# Paths are detected at install time, so nothing machine-specific is committed to the repo.
#
#   ./scripts/install-macos.sh              # install + start now, run at every login
#   ./scripts/install-macos.sh --uninstall  # stop and remove the agent
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
PY="$(command -v python3)"
LABEL="com.sworrl.claudit"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"
LOGS="$HOME/Library/Logs/ClAudit"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  rm -f "$PLIST"
  echo "Removed $PLIST"
  exit 0
fi

mkdir -p "$AGENTS" "$LOGS"
# gh / claude / agy usually live in /opt/homebrew/bin or /usr/local/bin, which launchd does not put
# on PATH by default, so the agent carries the interactive shell's PATH.
cat > "$PLIST" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PY</string>
    <string>$DIR/claudit_gui.py</string>
    <string>--interval</string><string>30</string>
    <string>--hidden</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><false/>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$LOGS/claudit.log</string>
  <key>StandardErrorPath</key><string>$LOGS/claudit.err</string>
</dict>
</plist>
PL

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
echo "Installed Login Item -> $PLIST"
echo "Logs: $LOGS. Remove with: $0 --uninstall"
