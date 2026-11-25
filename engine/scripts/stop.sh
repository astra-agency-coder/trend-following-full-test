#!/usr/bin/env bash
# Stop the engine gracefully (SIGTERM -> finishes current cycle, closes nothing).
set -euo pipefail
PID_FILE="${1:-engine.pid}"
if [ ! -f "$PID_FILE" ]; then
  echo "no pid file at $PID_FILE" >&2
  exit 1
fi
PID="$(cat "$PID_FILE")"
if kill -0 "$PID" 2>/dev/null; then
  kill -TERM "$PID"
  for _ in $(seq 1 30); do
    kill -0 "$PID" 2>/dev/null || { rm -f "$PID_FILE"; echo "stopped"; exit 0; }
    sleep 1
  done
  echo "did not stop in 30s, sending SIGKILL" >&2
  kill -KILL "$PID" || true
fi
rm -f "$PID_FILE"
