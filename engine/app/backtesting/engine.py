"""Backtesting / replay engine (spec 16 / 46).

Drives the SAME HybridRuntime over historical bars with a VirtualClock.
No-lookahead guarantees come from:
- strategies only see closed bars as-of the virtual time,
- ReplayProvider.get_latest_tick respects virtual now,
- snapshots / indicators slice at the decision timestamp.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from app.bootstrap import HybridRuntime
from app.core.clock import VirtualClock
from app.core.config import ConfigVersion
from app.core.logging_setup import get_logger
from app.data.replay_provider import ReplayProvider
from app.reporting import metrics as perf

logger = get_logger("backtesting.engine")


@dataclass
class BacktestResult:
    run_id: str
    started_at: datetime
    ended_at: datetime
    cycles: int
    metrics: dict
    by_strategy: dict = field(default_factory=dict)
    by_session: dict = field(default_factory=dict)
    router_quality: dict = field(default_factory=dict)
    shadow: dict = field(default_factory=dict)
    reproducibility: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "ended_at": self.ended_at.isoformat(),
            "cycles": self.cycles,
            "metrics": self.metrics,
            "by_strategy": self.by_strategy,
            "by_session": self.by_session,
            "router_quality": self.router_quality,
            "shadow": self.shadow,
            "reproducibility": self.reproducibility,
        }


class BacktestEngine:
    def __init__(self, *, config_version: ConfigVersion, database, bars, ticks, spec, step_seconds: float = 60.0):
        self.config_version = config_version
        self.database = database
        self.bars = bars
        self.ticks = ticks
        self.spec = spec
        self.step_seconds = step_seconds

    def run(self, *, start_utc: datetime, end_utc: datetime) -> BacktestResult:
        from app.core.ids import new_uuid

        run_id = new_uuid()[:8]
        provider = ReplayProvider(self.spec, bars=self.bars, ticks=self.ticks)
        clock = VirtualClock(start_utc)
        runtime = HybridRuntime(
            config_version=self.config_version,
            database=self.database,
            provider=provider,
            clock=clock,
            run_mode="BACKTEST",
        )
        runtime.start()

        now = start_utc
        cycles = 0
        while now <= end_utc:
            provider.set_virtual_now(now)
            runtime.run_cycle(now_utc=now)
            now = now + timedelta(seconds=self.step_seconds)
            cycles += 1
            if cycles % 500 == 0:
                logger.info(f"backtest progress: {now.isoformat()}")

        runtime.stop("BACKTEST_COMPLETE")
        metrics = perf.compute_metrics(runtime.paper.closed_trades).to_dict()
        return BacktestResult(
            run_id=run_id,
            started_at=start_utc,
            ended_at=end_utc,
            cycles=cycles,
            metrics=metrics,
            by_strategy=perf.performance_by_strategy(runtime.paper.closed_trades),
            by_session=perf.performance_by_session(runtime.paper.closed_trades),
            router_quality=perf.router_quality(runtime.decisions, runtime.shadow),
            shadow=runtime.shadow.summary(),
            reproducibility={
                "config_hash": self.config_version.config_hash,
                "strategy_versions": {s.strategy_id: s.strategy_version for s in runtime.registry.enabled_strategies()},
                "router_version": runtime.router.router_version,
                "random_seed": self.config_version.data.get("backtesting", {}).get("random_seed"),
                "step_seconds": self.step_seconds,
                "bars_count": len(self.bars),
                "ticks_count": len(self.ticks),
            },
        )
