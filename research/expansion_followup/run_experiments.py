#!/usr/bin/env python3
"""Expansion Followup — Trend Following gate experiments (research only).

Runs the REAL production engine (via scripts/research/harness.py →
app.bootstrap.HybridRuntime) with RUNTIME-ONLY entry gates. No platform file
is modified: gates are installed by monkey-patching TrendFollowingStrategy.evaluate
inside this process only, and are restored after every variant.

Gates apply to NEW ENTRIES ONLY (exit / trade management / risk / sizing /
costs are exactly the frozen production behaviour).

Variants (all on the ORIGINAL base cost scenario: spread 0.25 / slippage 2pt /
commission 7$/lot/side, swap disabled — identical to the original baseline run):
  baseline_repro       no gate — must reproduce the original base run exactly
  exp1_expansion_only  entries only when ER20 >= 0.30 (engine's own
                       efficiency_ratio() on the same closed-M15 window the
                       strategy consumes; <=600-bar buffer = BarService.MAX_BUFFER)
  exp2_pullback_only   DONCHIAN_BREAKOUT setup disabled by neutralizing the
                       module-level donchian_channel name inside the original
                       evaluate() — pullback rules run unchanged
  exp3a_sunday_only    entries only on Sunday (UTC)
  exp3b_h23_only       entries only at decision hour 23 UTC (23:00-23:59)
  exp3c_no_sunday      entries on all weekdays except Sunday
  exp3d_no_h23         entries in all hours except 23 UTC
  exp3e_sunday_h23     entries only on Sunday AND hour 23 (EXPLORATORY — small n)
  exp4a_exp_pullback   ExpansionGate + pullback-only
  exp4b_exp_pb_timing  ExpansionGate + pullback-only + best timing filter from
                       exp3 (pre-registered selection rule: max net P/L, tie-break
                       max profit factor; the selection itself is a multiple-
                       comparison risk and is reported as such)

Usage:  run_experiments.py --variant <id> [--smoke]
        (one variant per process; a shell supervisor chains them sequentially)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

RESEARCH_DIR = Path(__file__).resolve().parent
RUNS_DIR = RESEARCH_DIR / "runs"

sys.path.insert(0, "/home/z/my-project/scripts/research")
import harness  # noqa: E402

RUN_START = datetime(2020, 1, 1, tzinfo=timezone.utc)
RUN_END = datetime(2026, 11, 13, 23, 59, tzinfo=timezone.utc)
SMOKE_END = datetime(2020, 3, 1, 23, 59, tzinfo=timezone.utc)

# frozen base cost scenario — identical to the original baseline run
SCEN = {"spread_price": 0.25, "slippage_fixed_points": 2.0, "commission_per_lot_side": 7.0}
harness.SPREAD_PRICE = SCEN["spread_price"]
harness.SPREAD_POINTS = SCEN["spread_price"] / 0.01

_COST_OVERRIDES = {
    "paper_execution.slippage_fixed_points": SCEN["slippage_fixed_points"],
    "paper_execution.commission_per_lot_side": SCEN["commission_per_lot_side"],
    "paper_execution.commission_explicitly_configured": True,
}
_orig_build_config = harness.build_config


def build_config_base(enabled, overrides_=_COST_OVERRIDES):
    return _orig_build_config(enabled, overrides_)


harness.build_config = build_config_base

# --- gate machinery (runtime-only; nothing on disk is modified) -----------
from app.market_context.indicators import efficiency_ratio  # noqa: E402
from app.strategies.trend_following import strategy as tf_mod  # noqa: E402
from app.strategies.trend_following.strategy import TrendFollowingStrategy  # noqa: E402

ER_EXPANSION = 0.30  # same threshold as the original analysis (tf_postprocess.py)


class Gate:
    """Base entry gate: check() -> (allowed, detail). pre()/post() wrap the run."""
    name = "gate_base"
    codes: tuple = ()

    def check(self, strat, snapshot, bar_service, config):
        return True, ""

    def describe(self) -> dict:
        return {}

    def pre(self):
        pass

    def post(self):
        pass


class NoGate(Gate):
    name = "none"


class TimingGate(Gate):
    """UTC decision-time gate. Decision time == entry moment (immediate fill at
    the M15 close tick). weekdays: Mon=0..Sun=6; hours: 0..23. None = allow-all."""

    def __init__(self, name, weekdays=None, hours=None, code="RESEARCH_TIMING_GATE"):
        self.name = name
        self.weekdays = weekdays
        self.hours = hours
        self.codes = (code,)

    def check(self, strat, snapshot, bar_service, config):
        ts = snapshot.timestamp_utc  # aware UTC datetime
        if self.weekdays is not None and ts.weekday() not in self.weekdays:
            return False, f"utc_weekday={ts.weekday()} not allowed"
        if self.hours is not None and ts.hour not in self.hours:
            return False, f"utc_hour={ts.hour} not allowed"
        return True, ""

    def describe(self):
        return {"utc_weekdays_allowed": sorted(self.weekdays) if self.weekdays is not None else "all",
                "utc_hours_allowed": sorted(self.hours) if self.hours is not None else "all"}


class ExpansionGate(Gate):
    """Entries only when ER20 >= 0.30 — engine's own efficiency_ratio() on the
    same closed-M15 bars window the strategy itself uses (bar_service buffer)."""
    name = "expansion_only"
    codes = ("RESEARCH_EXPANSION_ONLY",)

    def check(self, strat, snapshot, bar_service, config):
        cfg = config.get("strategies", {}).get("trend_following", {})
        tf = cfg.get("timeframe", "M15")
        bars = bar_service.closed_bars(tf, as_of=snapshot.timestamp_utc)
        er = efficiency_ratio(bars, 20)
        if er is None:
            return False, f"er20=None(bars={len(bars)}) -> treated as CONSOLIDATION"
        if er < ER_EXPANSION:
            return False, f"er20={er:.3f} < {ER_EXPANSION}"
        return True, f"er20={er:.3f}"

    def describe(self):
        return {"er_period": 20, "er_threshold": ER_EXPANSION,
                "window": "bar_service closed M15 buffer (<=600 bars), no lookahead"}


class PullbackOnlyGate(Gate):
    """Disable DONCHIAN_BREAKOUT by neutralizing the module-level donchian_channel
    name used inside the ORIGINAL evaluate() — the original code path then falls
    through to the untouched pullback logic. Exits/management unchanged."""
    name = "pullback_only"
    codes = ("RESEARCH_PULLBACK_ONLY",)

    def pre(self):
        self._old = tf_mod.donchian_channel
        tf_mod.donchian_channel = lambda *a, **k: None

    def post(self):
        tf_mod.donchian_channel = self._old

    def describe(self):
        return {"disabled_setup": "DONCHIAN_BREAKOUT",
                "method": "module-level donchian_channel neutralized during evaluate()"}


class CompositeGate(Gate):
    def __init__(self, name, gates: list[Gate]):
        self.name = name
        self.gates = gates
        self.codes = tuple(code for g in gates for code in g.codes)

    def check(self, strat, snapshot, bar_service, config):
        details = []
        for g in self.gates:
            ok, detail = g.check(strat, snapshot, bar_service, config)
            if not ok:
                return False, f"{g.name}: {detail}"
            details.append(detail)
        return True, "; ".join(details)

    def pre(self):
        for g in self.gates:
            g.pre()

    def post(self):
        for g in reversed(self.gates):
            g.post()

    def describe(self):
        return {g.name: g.describe() for g in self.gates}


def log(vid: str, msg: str) -> None:
    line = f"[{datetime.now(timezone.utc).isoformat()}] [{vid}] {msg}"
    print(line, flush=True)
    with open(RUNS_DIR / "progress.log", "a") as f:
        f.write(line + "\n")


def run_variant(vid: str, gate: Gate, start_utc: datetime, end_utc: datetime) -> dict:
    out_dir = RUNS_DIR / vid
    out_dir.mkdir(parents=True, exist_ok=True)
    counter = {"allowed": 0, "blocked": 0}
    orig_evaluate = TrendFollowingStrategy.evaluate

    def gated_evaluate(self, snapshot, bar_service, config):
        ok, detail = gate.check(self, snapshot, bar_service, config)
        if ok:
            counter["allowed"] += 1
            return orig_evaluate(self, snapshot, bar_service, config)
        counter["blocked"] += 1
        ev = self.base_evaluation(snapshot, config.get("_config_version", "unknown"), snapshot.timestamp_utc)
        ev.rejection_reason_codes = list(gate.codes)
        ev.notes = f"research gate [{gate.name}] blocked: {detail}"
        return self.finish(ev, snapshot.timestamp_utc)

    gate.pre()
    TrendFollowingStrategy.evaluate = gated_evaluate
    t0 = time.time()
    try:
        stats = harness.run_backtest(
            strategy_ids=["trend_following"],
            start_utc=start_utc, end_utc=end_utc, out_dir=out_dir, label=f"tf_ef_{vid}",
        )
    finally:
        TrendFollowingStrategy.evaluate = orig_evaluate
        gate.post()

    stats["gate"] = {
        "name": gate.name,
        "blocked_evaluations": counter["blocked"],
        "allowed_evaluations": counter["allowed"],
        "params": gate.describe(),
    }
    stats["cost_scenario"] = SCEN
    stats["variant_id"] = vid
    (out_dir / "run_summary.json").write_text(json.dumps(stats, indent=1, default=str))
    (out_dir / "gate_params.json").write_text(json.dumps({
        "variant_id": vid, "gate": gate.describe(), "gate_codes": list(gate.codes),
        "blocked_evaluations": counter["blocked"], "allowed_evaluations": counter["allowed"],
        "cost_scenario": SCEN,
        "note": "gates are runtime-only patches inside this process; no platform file modified; "
                "gates apply to NEW ENTRIES ONLY — exits/risk/sizing/costs are frozen production behaviour",
    }, indent=1))
    stats["wall_seconds_incl_load"] = round(time.time() - t0, 1)
    log(vid, f"DONE wall={stats['wall_seconds_incl_load']}s engine={stats.get('elapsed_seconds')}s "
             f"trades={stats.get('closed_trades')} balance={stats.get('final_balance')} "
             f"gate_blocked={counter['blocked']}")
    return stats


def best_timing_selection() -> dict:
    """Pre-registered rule: among exp3a..exp3e pick max net P/L (tie-break max PF).
    Reads trades.csv of finished exp3 runs. The choice is itself a multiple-
    comparison selection and is flagged as such in the reports."""
    import csv as _csv

    candidates = {}
    for vid in ["exp3a_sunday_only", "exp3b_h23_only", "exp3c_no_sunday",
                "exp3d_no_h23", "exp3e_sunday_h23"]:
        p = RUNS_DIR / vid / "trades.csv"
        if not p.exists():
            raise RuntimeError(f"exp3 run missing: {p}")
        with open(p) as f:
            rows = list(_csv.DictReader(f))
        net = sum(float(r["net_pnl"]) for r in rows)
        gp = sum(float(r["net_pnl"]) for r in rows if float(r["net_pnl"]) > 0)
        gl = -sum(float(r["net_pnl"]) for r in rows if float(r["net_pnl"]) <= 0)
        pf = gp / gl if gl > 0 else float("inf")
        candidates[vid] = {"net": round(net, 2), "pf": round(pf, 4), "trades": len(rows)}
    best = max(candidates, key=lambda k: (candidates[k]["net"], candidates[k]["pf"]))
    sel = {"rule": "max net P/L among exp3 variants (tie-break: max PF)",
           "candidates": candidates, "selected": best,
           "multiple_testing_note": "selection is post-hoc; exp4b inherits its risk of chance superiority"}
    (RUNS_DIR / "exp4b_selection.json").write_text(json.dumps(sel, indent=1))
    log("exp4b", f"best timing filter selected: {best} (net={candidates[best]['net']})")
    return sel


def build_variant(vid: str) -> Gate:
    g = {
        "baseline_repro": lambda: NoGate(),
        "exp1_expansion_only": lambda: ExpansionGate(),
        "exp2_pullback_only": lambda: PullbackOnlyGate(),
        "exp3a_sunday_only": lambda: TimingGate("sunday_only", weekdays={6}),
        "exp3b_h23_only": lambda: TimingGate("h23_only", hours={23}),
        "exp3c_no_sunday": lambda: TimingGate("no_sunday", weekdays={0, 1, 2, 3, 4, 5}),
        "exp3d_no_h23": lambda: TimingGate("no_h23", hours=set(range(23))),
        "exp3e_sunday_h23": lambda: TimingGate("sunday_h23", weekdays={6}, hours={23}),
        "exp4a_exp_pullback": lambda: CompositeGate("expansion+pullback", [ExpansionGate(), PullbackOnlyGate()]),
    }
    if vid in g:
        return g[vid]()
    if vid == "exp4b_exp_pb_timing":
        sel = best_timing_selection()
        best = sel["selected"]
        tg = {
            "exp3a_sunday_only": TimingGate("sunday_only", weekdays={6}),
            "exp3b_h23_only": TimingGate("h23_only", hours={23}),
            "exp3c_no_sunday": TimingGate("no_sunday", weekdays={0, 1, 2, 3, 4, 5}),
            "exp3d_no_h23": TimingGate("no_h23", hours=set(range(23))),
            "exp3e_sunday_h23": TimingGate("sunday_h23", weekdays={6}, hours={23}),
        }[best]
        return CompositeGate(f"expansion+pullback+{tg.name}", [ExpansionGate(), PullbackOnlyGate(), tg])
    raise SystemExit(f"unknown variant: {vid}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", required=True)
    ap.add_argument("--smoke", action="store_true", help="2-month window into runs_smoke/")
    args = ap.parse_args()

    if args.smoke:
        global RUNS_DIR
        RUNS_DIR = RESEARCH_DIR / "runs_smoke"
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    gate = build_variant(args.variant)
    log(args.variant, f"START gate={gate.name} smoke={args.smoke}")
    start_utc, end_utc = RUN_START, (SMOKE_END if args.smoke else RUN_END)
    stats = run_variant(args.variant, gate, start_utc, end_utc)
    if args.smoke:
        n = stats.get("closed_trades", 0)
        assert n >= 1, f"smoke run produced no trades: {stats}"
        log(args.variant, f"SMOKE OK trades={n}")


if __name__ == "__main__":
    main()
