#!/usr/bin/env bash
# Restart with state preservation: open trades are restored from DB on boot;
# executed opportunity keys prevent double execution (spec 22).
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
"$DIR/stop.sh" engine.pid || true
"$DIR/start.sh" live &
echo $! > engine.pid
echo "restarted with pid $(cat engine.pid)"
