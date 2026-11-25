#!/usr/bin/env python3
"""Export audit/research tables to CSV/JSON (spec 12.3 / 40.8).

Usage:
  python scripts/export_journal.py --out exports/journal [--from 2026-01-01] [--to 2026-02-01]
  python scripts/export_journal.py --out exports/journal --table paper_trades --format csv
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.journal.exporters import JournalExporter
from app.persistence.database import Database


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="sqlite:///gold_hybrid.db")
    parser.add_argument("--out", default="exports/journal")
    parser.add_argument("--table", default=None, help="single table (default: all)")
    parser.add_argument("--format", default=None, choices=["csv", "json"])
    parser.add_argument("--from", dest="from_dt", default=None, help="ISO date YYYY-MM-DD")
    parser.add_argument("--to", dest="to_dt", default=None, help="ISO date YYYY-MM-DD (exclusive)")
    args = parser.parse_args()

    database = Database(args.db)
    exporter = JournalExporter(database)

    start = datetime.fromisoformat(args.from_dt).replace(tzinfo=UTC) if args.from_dt else None
    end = datetime.fromisoformat(args.to_dt).replace(tzinfo=UTC) if args.to_dt else None

    out_dir = Path(args.out)
    if args.table:
        fmt = args.format or ("json" if args.table in ("market_snapshots", "strategy_evaluations", "router_decisions") else "csv")
        path = exporter.export(args.table, fmt=fmt, output_dir=out_dir, start_utc=start, end_utc=end)
        paths = [path]
    else:
        paths = exporter.export_all(output_dir=out_dir, start_utc=start, end_utc=end)
    manifest = exporter.manifest(paths, out_dir, scope=f"export {start or 'begin'}..{end or 'now'}")

    for p in paths + [manifest]:
        print(f"exported {p} ({p.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
