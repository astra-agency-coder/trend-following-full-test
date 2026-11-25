#!/usr/bin/env python3
"""Run a deterministic backtest over synthetic or imported history.

Usage:
  python scripts/run_backtest.py --days 5 --step 900 --out exports/backtest.json
  python scripts/run_backtest.py --bars-csv data/xauusd_m15.csv   (columns: open_time,open,high,low,close[,volume])
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtesting.engine import BacktestEngine
from app.core.config import ConfigLoader
from app.data.contracts import MarketBar, SymbolSpecification
from app.data.replay_provider import bars_to_ticks, generate_synthetic_bars
from app.persistence.database import Database


def load_bars_csv(path: Path, timeframe: str) -> list[MarketBar]:
    import csv

    bars: list[MarketBar] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            open_time = datetime.fromisoformat(row["open_time"])
            if open_time.tzinfo is None:
                open_time = open_time.replace(tzinfo=UTC)
            seconds = {"M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400}[timeframe]
            bars.append(
                MarketBar(
                    canonical_symbol="XAUUSD",
                    source_symbol="IMPORT",
                    timeframe=timeframe,
                    open_time_utc=open_time,
                    close_time_utc=open_time + timedelta(seconds=seconds),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    tick_volume=float(row["volume"]) if row.get("volume") else None,
                    is_complete=True,
                    source_type="IMPORTED_HISTORY",
                )
            )
    return bars


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config")
    parser.add_argument("--db", default="sqlite:///gold_hybrid_backtest.db")
    parser.add_argument("--bars-csv", default=None, help="CSV of historical bars (else synthetic demo data)")
    parser.add_argument("--timeframe", default="M15")
    parser.add_argument("--days", type=float, default=3.0)
    parser.add_argument("--step", type=float, default=900.0, help="cycle step seconds")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", default="exports/backtest_result.json")
    args = parser.parse_args()

    config_version = ConfigLoader(args.config).load()
    database = Database(args.db)
    database.create_all()

    spec = SymbolSpecification(
        canonical_symbol="XAUUSD", source_symbol="XAUUSD", broker_name="BACKTEST",
        digits=2, point=0.01, contract_size=100.0,
        volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_size=0.01, tick_value=1.0,
    )

    if args.bars_csv:
        bars = load_bars_csv(Path(args.bars_csv), args.timeframe)
        print(f"loaded {len(bars)} bars from {args.bars_csv}")
    else:
        print("no --bars-csv given: using deterministic synthetic data (NOT real market data)")
        start = datetime(2026, 1, 5, tzinfo=UTC)
        bars = generate_synthetic_bars(
            timeframe=args.timeframe, count=int(args.days * 96 * (900 / {"M5": 300, "M15": 900}[args.timeframe])),
            start=start, trend=0.08, volatility=2.5, seed=args.seed,
        )

    # aggregate a higher timeframe from the base for context
    ticks = bars_to_ticks(bars)
    end_utc = bars[-1].close_time_utc
    start_utc = end_utc - timedelta(days=args.days)

    engine = BacktestEngine(
        config_version=config_version,
        database=database,
        bars=bars,
        ticks=ticks,
        spec=spec,
        step_seconds=args.step,
    )
    result = engine.run(start_utc=start_utc, end_utc=end_utc)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result.metrics, ensure_ascii=False, indent=2))
    print(f"\nresult saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
