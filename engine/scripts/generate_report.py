#!/usr/bin/env python3
"""Daily/weekly report generator (prints to stdout; used by cron/telegram).

Usage: python scripts/generate_report.py --type daily|weekly [--db sqlite:///gold_hybrid.db]
"""
from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.bootstrap import HybridRuntime  # noqa: F401  (documents wiring)
from app.core.config import ConfigLoader
from app.persistence.database import Database
from app.persistence.repositories.journal import JournalRepository
from app.reporting import metrics as perf


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config")
    parser.add_argument("--db", default="sqlite:///gold_hybrid.db")
    parser.add_argument("--type", choices=["daily", "weekly"], default="daily")
    args = parser.parse_args()

    config_version = ConfigLoader(args.config).load()
    database = Database(args.db)
    repo = JournalRepository(database)
    _, _ = repo.ensure_portfolio(
        name="HYBRID_MAIN",
        initial_balance=float(config_version.data.get("risk", {}).get("initial_balance", 10000)),
    )
    portfolio_id = None
    from sqlalchemy import select

    from app.persistence.models import PaperPortfolioRow

    with database.session() as session:
        row = session.execute(select(PaperPortfolioRow).where(PaperPortfolioRow.name == "HYBRID_MAIN")).scalar_one()
        portfolio_id = row.id

    trades = repo.load_closed_trades(portfolio_id)
    now = datetime.now(UTC)
    if args.type == "daily":
        report = perf.daily_report(trades, [], day_start_utc=now - timedelta(days=1), day_end_utc=now)
    else:
        report = perf.weekly_report(trades, [], None, start_utc=now - timedelta(days=7), end_utc=now)
    import json

    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
