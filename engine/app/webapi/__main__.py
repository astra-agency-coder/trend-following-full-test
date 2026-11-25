"""Web API entrypoint: ``python -m app.webapi [--engine]``.

- Default: API-only mode (reads DATABASE_URL; control endpoints need --engine).
- ``--engine``: runs the HybridRuntime in a background thread inside the
  same process so the dashboard shows live data and control works.
"""
from __future__ import annotations

import argparse
import os

import uvicorn


def main() -> int:
    parser = argparse.ArgumentParser(description="GOLD HYBRID ENGINE Web API")
    parser.add_argument("--host", default=os.environ.get("WEB_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("WEB_PORT", "8000")))
    parser.add_argument("--engine", action="store_true", help="run the paper-trading engine in this process")
    parser.add_argument("--dry-run", action="store_true", help="engine with replay provider (no MT5, demo data)")
    parser.add_argument("--config", default=None, help="config directory")
    parser.add_argument("--db", default=None, help="database URL (overrides DATABASE_URL)")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()

    from app.core.config import ConfigLoader
    from app.core.logging_setup import setup_logging
    from app.persistence.database import Database
    from app.webapi import create_app

    setup_logging(log_dir=os.environ.get("LOG_DIR", "logs"), level=os.environ.get("LOG_LEVEL", "INFO"))

    config_version = ConfigLoader(args.config).load()
    config = config_version.data
    db_url = args.db or os.environ.get("DATABASE_URL") or config.get("database", {}).get("url", "sqlite:///gold_hybrid.db")
    database = Database(db_url)
    database.create_all()

    runtime = None
    run_mode = "STANDALONE"
    cors_origins = os.environ.get("WEB_CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",")
    if args.engine or args.dry_run:
        from app.bootstrap import HybridRuntime
        from app.core.clock import SystemClock

        from app.main import build_provider

        if args.dry_run:
            config["market_data"]["provider"] = "replay"
            run_mode = "DEMO"
        else:
            run_mode = "EMBEDDED_ENGINE"
        provider = build_provider(config, SystemClock())
        provider.connect()
        runtime = HybridRuntime(config_version=config_version, database=database, provider=provider, run_mode="LIVE_PAPER")
        runtime.config["market_data"]["provider"] = config["market_data"]["provider"]

    app = create_app(database=database, config=config, runtime=runtime, run_mode=run_mode, cors_origins=cors_origins)

    if runtime is not None:
        from app.webapi.dependencies import get_state
        from app.webapi.engine_runner import EngineRunner

        state = get_state()
        runner = EngineRunner(state, poll_seconds=args.poll_seconds)
        runner.start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
