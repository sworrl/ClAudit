#!/usr/bin/env bash
# Install a ClAudit desktop launcher (and optionally autostart) for the current user.
# Paths are detected at install time, so nothing machine-specific is committed to the repo.
#
#   ./scripts/install-linux.sh                         # start-menu launcher only
#   ./scripts/install-linux.sh --autostart             # also start on login
#   ./scripts/install-linux.sh --no-census             # install with the anonymous heartbeat OFF
set -euo pipefail
NO_CENSUS=0; AUTOSTART=0
for a in "$@"; do
  case "$a" in
    --no-census) NO_CENSUS=1 ;;
    --autostart) AUTOSTART=1 ;;
  esac
done

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

if [ "$AUTOSTART" = 1 ]; then
  AS="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
  mkdir -p "$AS"
  cp "$DESKTOP" "$AS/claudit.desktop"
  echo "Enabled autostart   -> $AS/claudit.desktop"
fi

update-desktop-database "$APPS" 2>/dev/null || true
census_config() {   # write census_anon into ~/.claude/claudit/config.json without disturbing other keys
  "$PY" - "$1" <<'PYEOF2'
import json, os, sys
p = os.path.expanduser("~/.claude/claudit/config.json"); os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    cfg = json.load(open(p, encoding="utf-8"))
except (OSError, ValueError):
    cfg = {}
cfg["census_anon"] = sys.argv[1] == "on"; cfg["census_notice_shown"] = True
json.dump(cfg, open(p, "w", encoding="utf-8"), indent=1)
PYEOF2
}
RED=$'\033[31m'; NC=$'\033[0m'
if [ "$NO_CENSUS" = 1 ]; then
  census_config off
  echo "Anonymous install heartbeat: OFF (saved to ~/.claude/claudit/config.json)."
else
  echo "${RED}CENSUS: ClAudit sends an anonymous heartbeat every 10 minutes while it runs: a random node id,"
  echo "the version, the OS family, and git-or-pip, to a Cloudflare Worker the maintainer runs. No IP,"
  echo "hostname, account, or content is kept. It is ON by default."
  echo "Opt out: re-run with --no-census, or Settings > Census in the app, or CLAUDIT_NO_CENSUS=1.${NC}"
fi
echo "Done. Launch 'ClAudit' from your app menu (notify-only by default; add --auto to auto-file)."
