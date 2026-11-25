"""Entry point: python -m app.main [--config DIR] [--db URL] [--once] [--replay ...]"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import time
from datetime import timedelta

from app.bootstrap import HybridRuntime
from app.core.config import ConfigLoader
from app.core.exceptions import GoldHybridError
from app.core.logging_setup import get_logger, setup_logging
from app.data.replay_provider import ReplayProvider, generate_synthetic_bars, bars_to_ticks
from app.persistence.database import Database

logger = get_logger("main")


def build_provider(config, clock):
    """Choose the market data provider from config/env."""
    md_cfg = config.get("market_data", {})
    provider_name = md_cfg.get("provider", "mt5").lower()
    if provider_name == "mt5":
        from app.data.mt5_adapter import Mt5Adapter

        return Mt5Adapter(
            login=int(os.environ.get("MT5_LOGIN") or 0) or None,
            password=os.environ.get("MT5_PASSWORD") or None,
            server=os.environ.get("MT5_SERVER") or None,
            terminal_path=os.environ.get("MT5_TERMINAL_PATH") or None,
            source_symbol=md_cfg.get("source_symbol"),
            broker_name=os.environ.get("MT5_SERVER") or "UNKNOWN",
        )
    if provider_name in ("replay", "mock"):
        # live-clock synthetic market used for demos/dev on non-Windows hosts
        from app.data.contracts import SymbolSpecification
        from app.data.replay_provider import LiveReplayProvider

        spec = SymbolSpecification(
            canonical_symbol="XAUUSD", source_symbol="XAUUSD", broker_name="LIVE_REPLAY_DEMO",
            digits=2, point=0.01, contract_size=100.0,
            volume_min=0.01, volume_max=100.0, volume_step=0.01, tick_size=0.01, tick_value=1.0,
        )
        return LiveReplayProvider(spec)
    raise GoldHybridError(f"unknown provider {provider_name!r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="GOLD HYBRID ENGINE — XAUUSD paper trading backend")
    parser.add_argument("--config", default=None, help="config directory")
    parser.add_argument("--db", default=None, help="database URL (overrides DATABASE_URL)")
    parser.add_argument("--once", action="store_true", help="run a single analysis cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="run cycles with replay provider (no MT5 needed)")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()

    setup_logging(
        log_dir=os.environ.get("LOG_DIR", "logs"),
        level=os.environ.get("LOG_LEVEL", "INFO"),
    )

    try:
        config_version = ConfigLoader(args.config).load()
    except GoldHybridError as exc:
        logger.error(f"configuration error: {exc} details={exc.details}")
        return 2
    config = config_version.data

    db_url = args.db or os.environ.get("DATABASE_URL") or config.get("database", {}).get("url", "sqlite:///gold_hybrid.db")
    database = Database(db_url)
    database.create_all()

    if args.dry_run:
        config["market_data"]["provider"] = "replay"

    provider = build_provider(config, __import__("app.core.clock", fromlist=["SystemClock"]).SystemClock())
    try:
        provider.connect()
    except GoldHybridError as exc:
        logger.error(f"provider connection failed: {exc}")
        return 3

    runtime = HybridRuntime(
        config_version=config_version,
        database=database,
        provider=provider,
    )
    runtime.start()

    stop_requested = {"flag": False}

    def _handle_signal(signum, frame):
        stop_requested["flag"] = True
        logger.info(f"signal {signum} received — stopping after current cycle")

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    from app.core.clock import SystemClock

    clock = SystemClock()
    try:
        if args.once:
            runtime.run_cycle()
        else:
            while not stop_requested["flag"]:
                runtime.run_cycle()
                # telegram commands
                if runtime.telegram_enabled:
                    try:
                        updates = runtime.outbox.client.get_updates(timeout=1)
                        runtime.command_handler.handle_updates(updates)
                    except Exception:
                        logger.exception("telegram poll failed")
                time.sleep(args.poll_seconds)
    finally:
        runtime.stop("SHUTDOWN")
        try:
            provider.disconnect()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
