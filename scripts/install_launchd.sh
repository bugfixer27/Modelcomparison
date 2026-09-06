#!/bin/zsh
# Installs (or reinstalls) the hourly launchd agent for the current user.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DST="$HOME/Library/LaunchAgents/com.scoreboard.run.plist"
mkdir -p "$HOME/Library/LaunchAgents" "$ROOT/store/logs"
sed "s#__ROOT__#$ROOT#g" "$ROOT/launchd/com.scoreboard.run.plist" > "$DST"
launchctl bootout "gui/$(id -u)/com.scoreboard.run" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$DST"
launchctl enable "gui/$(id -u)/com.scoreboard.run"
echo "installed $DST"
launchctl print "gui/$(id -u)/com.scoreboard.run" | grep -E "state|last exit|runs" || true
echo "uninstall with: launchctl bootout gui/$(id -u)/com.scoreboard.run && rm $DST"
