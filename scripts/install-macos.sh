#!/usr/bin/env bash
# Install ClAudit as a macOS Login Item (launchd user agent), started minimized to the menu bar.
# Paths are detected at install time, so nothing machine-specific is committed to the repo.
#
#   ./scripts/install-macos.sh              # install + start now, run at every login
#   ./scripts/install-macos.sh --no-census  # same, with the anonymous heartbeat OFF
#   ./scripts/install-macos.sh --uninstall  # stop and remove the agent
set -euo pipefail
NO_CENSUS=0
for a in "$@"; do [ "$a" = "--no-census" ] && NO_CENSUS=1; done

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

RED=$'\033[31m'; NC=$'\033[0m'
if [ "$NO_CENSUS" = 1 ]; then
  "$PY" - <<'PYEOF2'
import json, os
p = os.path.expanduser("~/.claude/claudit/config.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    cfg = json.load(open(p, encoding="utf-8"))
except (OSError, ValueError):
    cfg = {}
cfg["census_anon"] = False; cfg["census_notice_shown"] = True
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=1)
PYEOF2
  echo "Anonymous install heartbeat: OFF (saved to ~/.claude/claudit/config.json)."
else
  echo "${RED}CENSUS: ClAudit sends an anonymous heartbeat every 10 minutes while it runs: a random node id,"
  echo "the version, the OS family, and git-or-pip, to a Cloudflare Worker the maintainer runs. No IP,"
  echo "hostname, account, or content is kept. It is ON by default."
  echo "Opt out: re-run with --no-census, or Settings > Census in the app, or CLAUDIT_NO_CENSUS=1.${NC}"
fi
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
echo "Installed Login Item -> $PLIST"
echo "Logs: $LOGS. Remove with: $0 --uninstall"
