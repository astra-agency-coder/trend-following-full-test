#!/usr/bin/env bash
# Start GOLD HYBRID ENGINE (paper trading only).
set -euo pipefail
cd "$(dirname "$0")/.."

export LOG_DIR="${LOG_DIR:-logs}"
mkdir -p "$LOG_DIR" data exports

if [ ! -f .env ]; then
  echo "WARNING: .env not found — copy .env.example to .env and fill values." >&2
fi
# shellcheck disable=SC1091
[ -f .env ] && set -a && . ./.env && set +a

PYTHON_BIN="${PYTHON_BIN:-python3}"
if [ -f .venv/bin/python ]; then PYTHON_BIN=".venv/bin/python"; fi

case "${1:-live}" in
  live)
    exec "$PYTHON_BIN" -m app.main --poll-seconds "${POLL_SECONDS:-5}"
    ;;
  once)
    exec "$PYTHON_BIN" -m app.main --once
    ;;
  dry-run)
    echo "Running with REPLAY provider (no MT5 required)..."
    exec "$PYTHON_BIN" -m app.main --dry-run --poll-seconds "${POLL_SECONDS:-5}"
    ;;
  *)
    echo "Usage: $0 {live|once|dry-run}" >&2
    exit 1
    ;;
esac
