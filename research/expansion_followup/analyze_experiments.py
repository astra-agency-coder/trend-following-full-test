#!/usr/bin/env python3
"""Expansion Followup — analysis & reports generator.

Consumes runs/<variant>/{trades.csv, equity_curve.csv, decisions.csv,
run_summary.json, gate_params.json} produced by run_experiments.py and emits:

  exports/RESULTS.json            everything numeric, structured
  exports/COMPARISON.csv          one row per variant (comparison table)
  exports/MONTHLY_RESULTS.csv     variant x entry-month attribution
  exports/HOURLY_RESULTS.csv      variant x entry-hour attribution
  exports/WEEKDAY_RESULTS.csv     variant x entry-weekday attribution
  exports/TRADE_ANALYSIS.csv      per-trade long table (all variants, enriched)
  exports/<variant>_PERFORMANCE.json  per-variant full performance dump

Metric definitions follow the ORIGINAL pipeline (scripts/research/tf_postprocess.py)
so every number is apples-to-apples with the baseline report, with two
documented improvements:
  * regime tags use engine-correct close-time semantics (the original offline
    attribution included the just-opened M15 bar; gates themselves always ran
    on closed bars only);
  * added Sharpe (daily equity returns, annualized, sqrt(252)) and Bootstrap
    confidence intervals + permutation tests for the timing questions.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

RESEARCH_DIR = Path(__file__).resolve().parent
RUNS_DIR = RESEARCH_DIR / "runs"
EXPORTS = RESEARCH_DIR / "exports"
EXPORTS.mkdir(exist_ok=True)

BACKEND = Path("/home/z/my-project/work/platform/backend")
sys.path.insert(0, str(BACKEND))
from app.market_context.indicators import adx, atr, efficiency_ratio  # noqa: E402

DATA_M15 = Path("/home/z/my-project/work/platform/data/historical/xauusd/candles_m15/xauusd_m15_2020_2026.parquet")

FULL_START = datetime(2020, 1, 1, tzinfo=timezone.utc)
FULL_END = datetime(2026, 11, 13, 23, 59, tzinfo=timezone.utc)
TRAIN_END = FULL_START + timedelta(days=int((FULL_END - FULL_START).days * 0.6))
VAL_END = FULL_START + timedelta(days=int((FULL_END - FULL_START).days * 0.8))
ER_EXPANSION = 0.30
ADX_TREND_THRESHOLD = 22.0
ADX_WINDOW = 600  # == BarService.MAX_BUFFER
RNG = np.random.default_rng(42)
BOOT_N = 10000

VARIANTS = [
    ("baseline_repro", "Original Baseline (reproduction)"),
    ("exp1_expansion_only", "Expansion Only"),
    ("exp2_pullback_only", "Pullback Only"),
    ("exp3a_sunday_only", "Sunday Only"),
    ("exp3b_h23_only", "23:00 UTC Only"),
    ("exp3c_no_sunday", "Excluding Sunday"),
    ("exp3d_no_h23", "Excluding 23:00 UTC"),
    ("exp3e_sunday_h23", "Sunday+23:00 (exploratory)"),
    ("exp4a_exp_pullback", "Expansion + Pullback"),
    ("exp4b_exp_pb_timing", "Expansion + Pullback + Best Timing"),
]

# ---------------------------------------------------------------- indicators
def load_m15_features() -> pd.DataFrame:
    df = pd.read_parquet(DATA_M15)
    df["ts"] = pd.to_datetime(df["ts_utc"], utc=True)
    df["ct"] = df["ts"] + pd.Timedelta(minutes=15)  # close time
    df = df.sort_values("ct").reset_index(drop=True)
    h, l, c = df.high.values, df.low.values, df.close.values
    tr = np.empty(len(c))
    tr[0] = h[0] - l[0]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1])))
    atr = np.full(len(c), np.nan)
    if len(c) > 14:
        atr[14] = tr[1:15].mean()
        for i in range(15, len(c)):
            atr[i] = atr[i - 1] + (tr[i] - atr[i - 1]) / 14
    return pd.DataFrame({"ts": df.ts, "ct": df.ct, "close": c, "high": h, "low": l, "atr14": atr})


FEATS: pd.DataFrame | None = None


def regime_at(when: pd.Timestamp) -> dict:
    """Regime at a decision time — ENGINE-CORRECT: last M15 bar CLOSED at or
    before `when`, <=600-bar window (BarService.MAX_BUFFER), engine's own
    indicator functions."""
    idx = FEATS.ct.searchsorted(when, side="right") - 1
    if idx < 40:
        return {"entry_er20": None, "regime_state": "UNKNOWN",
                "entry_adx14": None, "entry_atr14": None, "regime_trend": "UNKNOWN", "regime_vol": "UNKNOWN"}
    sl = FEATS.iloc[max(0, idx - ADX_WINDOW + 1): idx + 1]
    bars = [SimpleNamespace(high=float(a), low=float(b), close=float(c))
            for a, b, c in zip(sl.high, sl.low, sl.close)]
    adx_v = adx(bars, 14)
    atr_v = atr(bars, 14)
    er_v = efficiency_ratio(bars, 20)
    atr_series = sl.atr14.values
    baseline = np.nanmean(atr_series[19:]) if len(atr_series) > 20 else np.nan
    ratio = atr_v / baseline if (atr_v and not pd.isna(baseline) and baseline > 0) else float("nan")
    return {
        "entry_er20": round(float(er_v), 3) if er_v is not None else None,
        "regime_state": ("EXPANSION" if (er_v is not None and er_v >= ER_EXPANSION) else "CONSOLIDATION"),
        "entry_adx14": round(float(adx_v), 2) if adx_v is not None else None,
        "entry_atr14": round(float(atr_v), 4) if atr_v is not None else None,
        "regime_trend": "TRENDING" if (adx_v is not None and adx_v >= ADX_TREND_THRESHOLD) else "RANGING",
        "regime_vol": ("HIGH_VOL" if (not pd.isna(ratio) and ratio > 1.25)
                       else ("LOW_VOL" if (not pd.isna(ratio) and ratio < 0.8) else "NORMAL_VOL")),
    }


# ---------------------------------------------------------------- metrics
def sharpe_daily(equity: pd.DataFrame) -> float | None:
    """Annualized Sharpe on daily last-equity returns (risk-free = 0)."""
    if equity is None or len(equity) < 100:
        return None
    eq = equity.copy()
    eq["date"] = eq.timestamp_utc.dt.date
    daily = eq.groupby("date").equity.last()
    rets = daily.pct_change().dropna()
    if len(rets) < 30 or rets.std() == 0 or not np.isfinite(rets.std()):
        return None
    return round(float(rets.mean() / rets.std() * np.sqrt(252)), 3)


def bootstrap_ci(net: np.ndarray, n: int = BOOT_N) -> dict:
    if len(net) == 0:
        return {"mean": None, "ci95_low": None, "ci95_high": None, "resamples": n, "note": "no trades"}
    idx = RNG.integers(0, len(net), size=(n, len(net)))
    means = net[idx].mean(axis=1)
    return {"mean": round(float(net.mean()), 3),
            "ci95_low": round(float(np.percentile(means, 2.5)), 3),
            "ci95_high": round(float(np.percentile(means, 97.5)), 3),
            "resamples": n,
            "p_positive": round(float((means > 0).mean()), 4)}


def permutation_test(net: np.ndarray, mask: np.ndarray, n: int = BOOT_N) -> dict:
    """Two-sided permutation test: mean(bucket) vs mean(complement), same run."""
    if mask.sum() < 5 or (~mask).sum() < 5:
        return {"p_value": None, "note": f"bucket n={int(mask.sum())}, complement n={int((~mask).sum())} — too small"}
    a = net[mask].astype(float)
    b = net[~mask].astype(float)
    obs = a.mean() - b.mean()
    pooled = net.astype(float)
    na, nb = len(a), len(b)
    cnt = 0
    for _ in range(n):
        perm = RNG.permutation(len(pooled))
        d = pooled[perm[:na]].mean() - pooled[perm[na:na + nb]].mean()
        if abs(d) >= abs(obs):
            cnt += 1
    return {"bucket_mean": round(float(a.mean()), 3), "complement_mean": round(float(b.mean()), 3),
            "diff": round(float(obs), 3), "p_value": round((cnt + 1) / (n + 1), 4),
            "bucket_n": na, "complement_n": nb, "permutations": n}


def metrics_block(trades: pd.DataFrame, equity: pd.DataFrame | None) -> dict:
    if len(trades) == 0:
        return {"total_trades": 0}
    net = trades.net_pnl.astype(float)
    gross = trades.gross_pnl.astype(float)
    wins, losses = net[net > 0], net[net <= 0]
    gp, gl = wins.sum(), -losses.sum()
    pf = gp / gl if gl > 0 else float("inf")
    risk = trades.planned_risk_amount.astype(float)
    avg_r = (net / risk.replace(0, np.nan)).mean()
    dur = (trades.closed_at_utc - trades.opened_at_utc).dt.total_seconds()
    cl = cw = 0
    for x in net:
        cl, cw = (cl + 1, 0) if x <= 0 else (0, cw + 1)
    block = {
        "total_trades": int(len(trades)),
        "buy_trades": int((trades.side == "BUY").sum()),
        "sell_trades": int((trades.side == "SELL").sum()),
        "starting_balance": 10000.0,
        "total_net_profit": round(net.sum(), 2),
        "gross_profit": round(gp, 2),
        "gross_loss": round(gl, 2),
        "gross_pnl_total": round(gross.sum(), 2),
        "profit_factor": round(pf, 4) if np.isfinite(pf) else None,
        "win_rate_pct": round(100 * (net > 0).mean(), 2),
        "loss_rate_pct": round(100 * (net <= 0).mean(), 2),
        "expectancy_per_trade": round(net.mean(), 2),
        "average_win": round(wins.mean(), 2) if len(wins) else None,
        "average_loss": round(losses.mean(), 2) if len(losses) else None,
        "average_r": round(float(avg_r), 4) if not pd.isna(avg_r) else None,
        "avg_planned_risk_amount": round(risk.mean(), 2),
        "largest_win": round(net.max(), 2),
        "largest_loss": round(net.min(), 2),
        "max_consecutive_wins": cw,
        "max_consecutive_losses": cl,
        "avg_trade_duration_hours": round(dur.mean() / 3600, 2),
        "median_trade_duration_hours": round(dur.median() / 3600, 2),
        "total_costs": round(float(trades.commission.astype(float).sum() + trades.swap.astype(float).sum()
                                   + trades.slippage_cost.astype(float).sum()), 2),
    }
    if equity is not None and len(equity) > 1:
        eqv = equity.equity.astype(float).values
        peak = np.maximum.accumulate(eqv)
        dd = eqv - peak
        mdd_i = int(np.argmin(dd))
        block.update({
            "ending_balance": round(float(eqv[-1]), 2),
            "max_drawdown_usd": round(float(dd.min()), 2),
            "relative_drawdown_pct": round(float((dd / peak).min()) * 100, 2),
            "max_drawdown_at": str(equity.timestamp_utc.iloc[mdd_i]),
            "recovery_factor": round(net.sum() / abs(dd.min()), 3) if dd.min() < 0 else None,
            "sharpe_daily_annualized": sharpe_daily(equity),
        })
    else:
        block["ending_balance"] = round(10000.0 + float(net.sum()), 2)
    return block


def by_key(trades: pd.DataFrame, key: str) -> dict:
    out = {}
    for val, grp in trades.groupby(trades[key].fillna("UNKNOWN")):
        net = grp.net_pnl.astype(float)
        gp = net[net > 0].sum()
        gl = -net[net <= 0].sum()
        out[str(val)] = {
            "trades": int(len(grp)),
            "gross_profit": round(gp, 2),
            "gross_loss": round(gl, 2),
            "net": round(net.sum(), 2),
            "win_rate_pct": round(100 * (net > 0).mean(), 2),
            "profit_factor": round(gp / gl, 3) if gl > 0 else None,
        }
    return out


def duration_bucket(hours: float) -> str:
    if hours < 1:
        return "<1h"
    if hours < 3:
        return "1-3h"
    if hours < 6:
        return "3-6h"
    return ">6h"


# ---------------------------------------------------------------- signals
def signals_from_decisions(decisions: pd.DataFrame, trades: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Same derivation as the original tf_postprocess.py."""
    sig_rows, rej_rows = [], []
    trade_decisions = set(trades.decision_id.dropna())
    dec = decisions[decisions.candidates_json.notna() & decisions.candidates_json.str.contains('"trend_following"', na=False)]
    for row in dec.itertuples(index=False):
        info = json.loads(row.candidates_json)[0]
        selected = row.status == "SELECTED" and row.selected_strategy == "trend_following"
        opened = row.decision_id in trade_decisions
        base = {
            "decision_id": row.decision_id,
            "timestamp_utc": row.timestamp_utc,
            "setup_type": info.get("opportunity_key", "").split(":")[1] if info.get("opportunity_key") else "",
            "direction": info.get("direction"),
            "entry_proposed": info.get("entry"),
            "stop_loss_proposed": info.get("sl"),
            "score": info.get("score"),
        }
        if selected and opened:
            sig_rows.append({**base, "outcome": "OPENED"})
        elif selected and not opened:
            rej_rows.append({**base, "rejection_stage": "RISK_ENGINE",
                             "rejection_codes": "RISK_REJECTED_AFTER_SELECTION (risk sizing/limits/daily loss/min-lot)"})
        else:
            codes = ";".join(info.get("rejection_codes") or [])
            if not codes:
                codes = row.hard_blocks or "NOT_SELECTED"
            stage = "ROUTER_ELIGIBILITY" if info.get("eligible") is False else "NOT_TOP_RANKED_OR_BLOCKED"
            rej_rows.append({**base, "rejection_stage": stage, "rejection_codes": codes})
    return pd.DataFrame(sig_rows), pd.DataFrame(rej_rows)


# ---------------------------------------------------------------- per variant
def analyze_variant(vid: str) -> dict:
    run_dir = RUNS_DIR / vid
    trades = pd.read_csv(run_dir / "trades.csv")
    equity = pd.read_csv(run_dir / "equity_curve.csv")
    equity["timestamp_utc"] = pd.to_datetime(equity.timestamp_utc, utc=True)
    summary = json.loads((run_dir / "run_summary.json").read_text())
    gate_params = json.loads((run_dir / "gate_params.json").read_text())

    trades["opened_at_utc"] = pd.to_datetime(trades.opened_at_utc, utc=True)
    trades["closed_at_utc"] = pd.to_datetime(trades.closed_at_utc, utc=True)
    print(f"[{vid}] regime tagging {len(trades)} trades...", flush=True)
    regs = pd.DataFrame([regime_at(t) for t in trades.opened_at_utc])
    trades = pd.concat([trades.reset_index(drop=True), regs], axis=1)
    trades["variant"] = vid
    trades["entry_hour"] = trades.opened_at_utc.dt.hour
    trades["entry_weekday"] = trades.opened_at_utc.dt.day_name()
    trades["entry_month"] = trades.opened_at_utc.dt.to_period("M").astype(str)
    trades["duration_hours"] = (trades.closed_at_utc - trades.opened_at_utc).dt.total_seconds() / 3600
    trades["duration_bucket"] = trades.duration_hours.map(duration_bucket)
    trades["segment"] = trades.closed_at_utc.map(
        lambda t: "TRAIN_60" if t <= TRAIN_END else ("VALIDATION_20" if t <= VAL_END else "OUT_OF_SAMPLE_20"))

    overall = metrics_block(trades, equity)
    overall["sharpe_note"] = "daily last-equity returns, annualized sqrt(252); risk-free=0; misleading if trades are few"
    overall["bootstrap_expectancy"] = bootstrap_ci(trades.net_pnl.astype(float).values) if len(trades) else {}

    # segments + WF folds (frozen config — same windows as the original report)
    seg_metrics = {s: metrics_block(trades[trades.segment == s], equity[equity.timestamp_utc <=
                        (TRAIN_END if s == "TRAIN_60" else VAL_END if s == "VALIDATION_20" else FULL_END)])
                   for s in ["TRAIN_60", "VALIDATION_20", "OUT_OF_SAMPLE_20"]}
    folds = []
    cur = datetime(2021, 7, 1, tzinfo=timezone.utc)
    while cur + timedelta(days=182) <= FULL_END:
        nxt = cur + timedelta(days=182)
        ft = trades[(trades.closed_at_utc >= pd.Timestamp(cur)) & (trades.closed_at_utc < pd.Timestamp(nxt))]
        fe = equity[(equity.timestamp_utc >= pd.Timestamp(cur)) & (equity.timestamp_utc < pd.Timestamp(nxt))]
        folds.append({"fold": len(folds) + 1,
                      "test_window": f"{cur.date()}→{(nxt - timedelta(days=1)).date()}",
                      **metrics_block(ft, fe if len(fe) > 1 else None)})
        cur = nxt

    attribution = {
        "by_entry_month": by_key(trades, "entry_month"),
        "by_entry_weekday": by_key(trades, "entry_weekday"),
        "by_entry_hour": by_key(trades, "entry_hour"),
        "by_regime_state": by_key(trades, "regime_state"),
        "by_regime_trend": by_key(trades, "regime_trend"),
        "by_regime_volatility": by_key(trades, "regime_vol"),
        "by_direction": by_key(trades, "side"),
        "by_duration_bucket": by_key(trades, "duration_bucket"),
        "by_setup_type": by_key(trades, "setup_type"),
        "by_entry_session": by_key(trades, "entry_session"),
        "by_exit_reason": by_key(trades, "exit_reason"),
    }

    # timing questions answered on the BASELINE run (bucket vs complement, same run)
    timing_tests = {}
    if vid == "baseline_repro" and len(trades):
        sun = (trades.opened_at_utc.dt.weekday == 6).values
        h23 = (trades.opened_at_utc.dt.hour == 23).values
        net = trades.net_pnl.astype(float).values
        timing_tests = {"sunday_vs_rest": permutation_test(net, sun),
                        "hour23_vs_rest": permutation_test(net, h23)}

    # signals / rejected (same derivation as original)
    print(f"[{vid}] parsing decisions.csv for signals/rejections...", flush=True)
    dec = pd.read_csv(run_dir / "decisions.csv", usecols=["decision_id", "timestamp_utc", "status",
                                                          "selected_strategy", "hard_blocks", "candidates_json"])
    sig, rej = signals_from_decisions(dec, trades)
    del dec
    signals_block = {
        "discovered": int(len(sig) + len(rej)),
        "opened": int(len(sig)),
        "rejected": int(len(rej)),
        "rejected_by_risk_engine": int((rej.rejection_stage == "RISK_ENGINE").sum()) if len(rej) else 0,
        "rejection_stage_counts": rej.rejection_stage.value_counts().to_dict() if len(rej) else {},
        "gate_blocked_evaluations": gate_params["blocked_evaluations"],
    }
    rej.to_csv(EXPORTS / f"{vid}_REJECTED_SIGNALS.csv", index=False)

    period_days = max(1, (trades.opened_at_utc.max() - trades.opened_at_utc.min()).days) if len(trades) else 1
    perf = {
        "variant": vid,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "engine_summary": {k: summary.get(k) for k in ["decisions", "closed_trades", "final_balance",
                                                       "initial_balance", "final_equity", "elapsed_seconds"]},
        "gate": gate_params,
        "overall": overall,
        "segments_60_20_20": {"train_end": str(TRAIN_END.date()), "val_end": str(VAL_END.date()), **seg_metrics},
        "walk_forward_folds_frozen_config": folds,
        "attribution": attribution,
        "timing_permutation_tests": timing_tests,
        "signals": signals_block,
        "trades_per_day": round(len(trades) / period_days, 3),
        "first_trade_utc": str(trades.opened_at_utc.min()) if len(trades) else None,
        "last_trade_utc": str(trades.closed_at_utc.max()) if len(trades) else None,
    }
    (EXPORTS / f"{vid}_PERFORMANCE.json").write_text(json.dumps(perf, indent=1, ensure_ascii=False, default=str))
    return {"trades": trades, "perf": perf}


def main() -> None:
    global FEATS
    print("loading M15 features (engine-correct close-time window)...", flush=True)
    FEATS = load_m15_features()

    all_trades, perfs = [], {}
    for vid, _label in VARIANTS:
        res = analyze_variant(vid)
        all_trades.append(res["trades"])
        perfs[vid] = res["perf"]
        o = res["perf"]["overall"]
        print(f"[{vid}] net={o.get('total_net_profit')} trades={o.get('total_trades')} "
              f"PF={o.get('profit_factor')} WR={o.get('win_rate_pct')}% "
              f"maxDD={o.get('max_drawdown_usd')} end={o.get('ending_balance')}", flush=True)

    pd.concat(all_trades, ignore_index=True).to_csv(EXPORTS / "TRADE_ANALYSIS.csv", index=False)

    # ---- comparison table -------------------------------------------------
    rows = []
    for vid, label in VARIANTS:
        p = perfs[vid]["overall"]
        rows.append({
            "Strategy": label,
            "variant_id": vid,
            "Trades": p.get("total_trades", 0),
            "WinRate_pct": p.get("win_rate_pct"),
            "ProfitFactor": p.get("profit_factor"),
            "Net_PL_usd": p.get("total_net_profit"),
            "Gross_Profit_usd": p.get("gross_profit"),
            "Gross_Loss_usd": p.get("gross_loss"),
            "MaxDD_usd": p.get("max_drawdown_usd"),
            "MaxDD_pct": p.get("relative_drawdown_pct"),
            "Expectancy_usd_per_trade": p.get("expectancy_per_trade"),
            "Expectancy_CI95": (f"[{p['bootstrap_expectancy']['ci95_low']}, {p['bootstrap_expectancy']['ci95_high']}]"
                                if p.get("bootstrap_expectancy", {}).get("ci95_low") is not None else None),
            "Average_R": p.get("average_r"),
            "Ending_Balance_usd": p.get("ending_balance"),
            "Total_Costs_usd": p.get("total_costs"),
            "Sharpe_daily_annualized": p.get("sharpe_daily_annualized"),
            "Avg_Duration_h": p.get("avg_trade_duration_hours"),
            "Max_consec_losses": p.get("max_consecutive_losses"),
            "Signals_Rejected_by_Risk": perfs[vid]["signals"]["rejected_by_risk_engine"],
            "Gate_Blocked_Evaluations": perfs[vid]["signals"]["gate_blocked_evaluations"],
        })
    comp = pd.DataFrame(rows)
    comp.to_csv(EXPORTS / "COMPARISON.csv", index=False)

    # ---- attribution CSVs (long format across variants) -------------------
    def attribution_csv(key: str, fname: str):
        recs = []
        for vid, label in VARIANTS:
            for k, v in perfs[vid]["attribution"][key].items():
                recs.append({"variant": vid, "bucket": k, **v})
        pd.DataFrame(recs).to_csv(EXPORTS / fname, index=False)

    attribution_csv("by_entry_month", "MONTHLY_RESULTS.csv")
    attribution_csv("by_entry_hour", "HOURLY_RESULTS.csv")
    attribution_csv("by_entry_weekday", "WEEKDAY_RESULTS.csv")

    (EXPORTS / "RESULTS.json").write_text(json.dumps({
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "methodology": {
            "engine": "production HybridRuntime via scripts/research/harness.py (unchanged)",
            "gates": "runtime-only monkey-patches on TrendFollowingStrategy.evaluate; new entries only",
            "costs": "original base scenario: spread 0.25 / slippage 2pt / commission 7$/lot/side, swap off",
            "splits": f"60/20/20 chronological: train_end={TRAIN_END.date()} val_end={VAL_END.date()}",
            "walk_forward": "10 folds x 182d test, expanding train, frozen config (same as original)",
            "regime": f"ER20 >= {ER_EXPANSION} = EXPANSION (engine fn, closed-bar window <=600)",
            "bootstrap": f"{BOOT_N} resamples, seed 42; permutation tests {BOOT_N} shuffles, two-sided",
            "multiple_testing": "10 variants compared; expect ~1-2 false positives at p<0.05 among timing buckets; treat small-n results as exploratory",
        },
        "comparison": rows,
        "timing_tests_baseline": perfs["baseline_repro"]["timing_permutation_tests"],
        "variants": {vid: perfs[vid] for vid, _ in VARIANTS},
    }, indent=1, ensure_ascii=False, default=str))

    print("ANALYSIS DONE → exports/", flush=True)


if __name__ == "__main__":
    main()
