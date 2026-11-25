#!/usr/bin/env bash
# Health check for VPS monitoring (systemd watchdog / cron).
# Exit codes: 0 healthy, 1 degraded, 2 down.
set -euo pipefail
DB="${DB_URL:-sqlite:///gold_hybrid.db}"
LOG_DIR="${LOG_DIR:-logs}"
python3 - "$DB" "$LOG_DIR" <<'PY'
import sys
from datetime import datetime, timedelta, UTC
from pathlib import Path

db_url, log_dir = sys.argv[1], Path(sys.argv[2])
log_file = log_dir / "engine.log"

if not log_file.exists():
    print("DOWN: no engine log")
    sys.exit(2)

import json
last = None
with open(log_file, encoding="utf-8") as fh:
    for line in fh:
        try:
            last = json.loads(line)
        except json.JSONDecodeError:
            continue
if last is None:
    print("DOWN: empty log")
    sys.exit(2)

ts = datetime.strptime(last["ts"], "%Y-%m-%dT%H:%M:%S%z")
age = (datetime.now(UTC) - ts).total_seconds()
if age > 300:
    print(f"DOWN: last log entry {int(age)}s ago")
    sys.exit(2)
if last.get("level") in ("ERROR", "CRITICAL"):
    print(f"DEGRADED: last entry is {last['level']}")
    sys.exit(1)
print(f"HEALTHY: last entry {int(age)}s ago")
sys.exit(0)
PY
