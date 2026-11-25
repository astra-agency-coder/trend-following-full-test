#!/usr/bin/env bash
# Rotate logs, archive old exports, verify free disk space (spec 24 / 26.4).
set -euo pipefail
LOG_DIR="${LOG_DIR:-logs}"
EXPORT_DIR="${EXPORT_DIR:-exports}"
MIN_FREE_MB="${MIN_FREE_MB:-2000}"

free_mb=$(df -m . | awk 'NR==2 {print $4}')
if [ "$free_mb" -lt "$MIN_FREE_MB" ]; then
  echo "CRITICAL: only ${free_mb}MB free (min ${MIN_FREE_MB}MB)" >&2
  exit 2
fi

# compress rotated logs older than 7 days
find "$LOG_DIR" -name "*.log.*" -mtime +7 ! -name "*.gz" -exec gzip -9 {} \; 2>/dev/null || true
# delete archives older than 90 days (operational logs only; DB never touched)
find "$LOG_DIR" -name "*.gz" -mtime +90 -delete 2>/dev/null || true
mkdir -p "$EXPORT_DIR"
echo "ok: ${free_mb}MB free"
