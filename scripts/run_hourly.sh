#!/bin/zsh
# launchd entry point. Runs the hourly scoreboard update from the project root with the venv.
set -u
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1
export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:$PATH"
# skip if a previous run is still going (launchd can catch up several times after sleep)
LOCK="$ROOT/store/.run.lock"
if [ -e "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then echo "run already in progress"; exit 0; fi
echo $$ > "$LOCK"
trap 'rm -f "$LOCK"' EXIT
exec "$ROOT/.venv/bin/python" -m scoreboard.run "$@"
