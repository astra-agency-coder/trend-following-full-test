#!/usr/bin/env python3
"""Expansion Followup — programmatic verification of every experiment run.

Checks per variant:
  V1  files exist and CSV/JSON parse cleanly
  V2  unique trade ids; net == gross - commission - swap exactly; costs once
  V3  r_multiple == net/planned_risk (atol 2e-3)
  V4  SL side correct for BUY/SELL
  V5  equity reconcile: |sum(net) - (ending_balance - 10000)| <= 50$ tolerance
      (equity sampled at M15 closes incl. floating P/L — same tolerance as original)
  V6  GATE COMPLIANCE (the critical correctness proof):
      - exp1/exp4a/exp4b: every entry regime ER20 >= 0.30 (engine fn, closed bars)
      - exp2/exp4a/exp4b: every trade setup_type == TREND_PULLBACK_CONTINUATION
      - exp3a: all entries Sunday; exp3b: all entries hour==23 UTC;
        exp3c: no Sunday entries; exp3d: no 23:00 entries; exp3e: Sunday & 23:00
      - baseline_repro: no gate
  V7  baseline_repro reproduces the ORIGINAL base run row-for-row (deterministic
      engine): trade sequence (opened/closed/side/setup/entry/exit/net/SL) identical
      to results/TREND_FOLLOWING_FULL_TEST/runs/base/trades.csv

Exit code 0 = all checks pass.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

RESEARCH_DIR = Path(__file__).resolve().parent
RUNS_DIR = RESEARCH_DIR / "runs"
ORIGINAL_BASE = Path("/home/z/my-project/work/platform/results/TREND_FOLLOWING_FULL_TEST/runs/base/trades.csv")
BACKEND = Path("/home/z/my-project/work/platform/backend")
sys.path.insert(0, str(BACKEND))
from app.market_context.indicators import efficiency_ratio  # noqa: E402

DATA_M15 = Path("/home/z/my-project/work/platform/data/historical/xauusd/candles_m15/xauusd_m15_2020_2026.parquet")
ER_EXPANSION = 0.30

RESULTS: list[dict] = []
FAILED = 0


def check(variant: str, cid: str, ok: bool, detail: str) -> None:
    global FAILED
    if not ok:
        FAILED += 1
    RESULTS.append({"variant": variant, "check": cid, "pass": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {variant} {cid}: {detail}", flush=True)


def load_feats() -> pd.DataFrame:
    df = pd.read_parquet(DATA_M15)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df["ct"] = df["ts"] + pd.Timedelta(minutes=15)
    return df.sort_values("ct").reset_index(drop=True)


def er_at(feats: pd.DataFrame, when: pd.Timestamp) -> float | None:
    idx = feats.ct.searchsorted(when, side="right") - 1
    if idx < 40:
        return None
    sl = feats.iloc[max(0, idx - 599): idx + 1]
    bars = [SimpleNamespace(high=float(a), low=float(b), close=float(c))
            for a, b, c in zip(sl.high, sl.low, sl.close)]
    return efficiency_ratio(bars, 20)


def verify(variant: str, feats: pd.DataFrame) -> None:
    run_dir = RUNS_DIR / variant
    # V1 files exist & parse
    try:
        trades = pd.read_csv(run_dir / "trades.csv")
        equity = pd.read_csv(run_dir / "equity_curve.csv")
        summary = json.loads((run_dir / "run_summary.json").read_text())
        gate = json.loads((run_dir / "gate_params.json").read_text())
        check(variant, "V1_files_parse", True,
              f"trades={len(trades)} equity_rows={len(equity)} decisions_summary_keys={len(summary)}")
    except Exception as e:
        check(variant, "V1_files_parse", False, str(e))
        return

    if len(trades):
        # V2 accounting identity
        lhs = (trades.gross_pnl - trades.commission - trades.swap).round(6)
        bad = int((lhs != trades.net_pnl.round(6)).sum())
        check(variant, "V2_net_identity", bad == 0, f"net == gross - commission - swap violations={bad}")
        check(variant, "V2_unique_ids", trades.trade_id.is_unique, f"unique trade ids={trades.trade_id.is_unique}")

        # V3 r_multiple
        r = (trades.net_pnl / trades.planned_risk_amount.replace(0, np.nan) - trades.r_multiple).abs()
        bad = int((r > 2e-3).sum())
        check(variant, "V3_r_multiple", bad == 0, f"r_multiple consistent violations={bad}")

        # V4 SL sides
        buy_ok = bool((trades[trades.side == "BUY"].stop_loss_initial < trades[trades.side == "BUY"].entry_price).all())
        sell_ok = bool((trades[trades.side == "SELL"].stop_loss_initial > trades[trades.side == "SELL"].entry_price).all())
        check(variant, "V4_sl_sides", buy_ok and sell_ok, f"BUY SL<entry={buy_ok} SELL SL>entry={sell_ok}")

        # V5 equity reconcile
        delta = float(equity.equity.iloc[-1]) - 10000.0
        gap = abs(float(trades.net_pnl.sum()) - delta)
        check(variant, "V5_equity_reconcile", gap <= 50.0,
              f"sum(net)={trades.net_pnl.sum():.2f} equityΔ={delta:.2f} |gap|={gap:.2f}$ (tol 50$ M15-sampled floating P/L)")

        # V6 gate compliance
        opened = pd.to_datetime(trades.opened_at_utc, utc=True)
        gname = gate["gate"]["name"] if isinstance(gate.get("gate"), dict) else str(gate.get("gate"))
        if variant in ("exp1_expansion_only", "exp4a_exp_pullback", "exp4b_exp_pb_timing"):
            print(f"[{variant}] computing ER at {len(trades)} entries...", flush=True)
            ers = [er_at(feats, t) for t in opened]
            bad = sum(1 for e in ers if e is None or e < ER_EXPANSION)
            check(variant, "V6_expansion_compliance", bad == 0,
                  f"entries with ER20<{ER_EXPANSION}: {bad}/{len(ers)} (min ER={min(e for e in ers if e is not None):.3f})")
        if variant in ("exp2_pullback_only", "exp4a_exp_pullback", "exp4b_exp_pb_timing"):
            bad = int((trades.setup_type != "TREND_PULLBACK_CONTINUATION").sum())
            check(variant, "V6_pullback_only", bad == 0,
                  f"non-pullback trades={bad} setups={trades.setup_type.value_counts().to_dict()}")
        if variant == "exp3a_sunday_only":
            bad = int((opened.dt.weekday != 6).sum())
            check(variant, "V6_sunday_only", bad == 0, f"non-Sunday entries={bad}")
        if variant == "exp3b_h23_only":
            bad = int((opened.dt.hour != 23).sum())
            check(variant, "V6_h23_only", bad == 0, f"non-23:00 entries={bad}")
        if variant == "exp3c_no_sunday":
            bad = int((opened.dt.weekday == 6).sum())
            check(variant, "V6_no_sunday", bad == 0, f"Sunday entries={bad}")
        if variant == "exp3d_no_h23":
            bad = int((opened.dt.hour == 23).sum())
            check(variant, "V6_no_h23", bad == 0, f"23:00 entries={bad}")
        if variant == "exp3e_sunday_h23":
            bad = int(((opened.dt.weekday != 6) | (opened.dt.hour != 23)).sum())
            check(variant, "V6_sunday_h23", bad == 0, f"violating entries={bad} n={len(trades)}")
        if variant == "baseline_repro":
            check(variant, "V6_no_gate", gname == "none", f"gate={gname}")

    # V7 baseline reproduction vs original (row-for-row)
    if variant == "baseline_repro":
        orig = pd.read_csv(ORIGINAL_BASE)
        cols = ["opened_at_utc", "closed_at_utc", "side", "setup_type", "entry_price",
                "exit_price", "stop_loss_initial", "volume_lots", "gross_pnl", "net_pnl", "r_multiple"]
        a = trades[cols].reset_index(drop=True)
        b = orig[cols].reset_index(drop=True)
        if len(a) != len(b):
            check(variant, "V7_row_for_row_repro", False, f"row count {len(a)} vs original {len(b)}")
        else:
            neq = 0
            for c in cols:
                if a[c].dtype.kind in "fi":
                    neq += int((a[c].astype(float).round(6) != b[c].astype(float).round(6)).sum())
                else:
                    neq += int((a[c].astype(str) != b[c].astype(str)).sum())
            check(variant, "V7_row_for_row_repro", neq == 0,
                  f"{len(a)} trades compared on {len(cols)} fields (ids excluded) — mismatched values={neq}")


def main() -> None:
    print("loading M15 features for ER compliance checks...", flush=True)
    feats = load_feats()
    variants = sorted(p.name for p in RUNS_DIR.iterdir() if p.is_dir() and (p / "run_summary.json").exists())
    print(f"variants found: {variants}", flush=True)
    for v in variants:
        verify(v, feats)
    (RESEARCH_DIR / "exports").mkdir(exist_ok=True)
    (RESEARCH_DIR / "exports" / "VERIFICATION.json").write_text(json.dumps({
        "generated_at_utc": pd.Timestamp.utcnow().isoformat(),
        "all_passed": FAILED == 0,
        "failed_checks": FAILED,
        "checks": RESULTS}, indent=1))
    print(f"\nVERIFICATION {'PASSED' if FAILED == 0 else 'FAILED'}: {len(RESULTS)} checks, {FAILED} failures", flush=True)
    sys.exit(0 if FAILED == 0 else 1)


if __name__ == "__main__":
    main()
