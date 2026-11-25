#!/usr/bin/env python3
"""Expansion Followup — charts: equity curves, drawdown, monthly/weekday/hour attribution."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import arabic_reshaper
import matplotlib
matplotlib.use("Agg")
import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from bidi.algorithm import get_display

RESEARCH_DIR = Path(__file__).resolve().parent
RUNS_DIR = RESEARCH_DIR / "runs"
EXPORTS = RESEARCH_DIR / "exports"
CHARTS = RESEARCH_DIR / "charts"
CHARTS.mkdir(exist_ok=True)

fm.fontManager.addfont("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
plt.rcParams["font.sans-serif"] = ["DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False

LABELS = {
    "baseline_repro": "الأصل (إعادة إنتاج)", "exp1_expansion_only": "التوسع فقط",
    "exp2_pullback_only": "Pullback فقط", "exp3a_sunday_only": "الأحد فقط",
    "exp3b_h23_only": "ساعة 23 فقط", "exp3c_no_sunday": "بدون الأحد",
    "exp3d_no_h23": "بدون ساعة 23", "exp3e_sunday_h23": "الأحد+ساعة 23",
    "exp4a_exp_pullback": "توسع+Pullback", "exp4b_exp_pb_timing": "توسع+Pullback+توقيت",
}
COLORS = {
    "baseline_repro": "black", "exp1_expansion_only": "tab:blue", "exp2_pullback_only": "tab:orange",
    "exp3a_sunday_only": "tab:green", "exp3b_h23_only": "tab:red", "exp3c_no_sunday": "tab:purple",
    "exp3d_no_h23": "tab:brown", "exp3e_sunday_h23": "tab:pink",
    "exp4a_exp_pullback": "tab:cyan", "exp4b_exp_pb_timing": "tab:olive",
}


def ar(text: str) -> str:
    return get_display(arabic_reshaper.reshape(text))


def equity(vid: str) -> pd.DataFrame:
    eq = pd.read_csv(RUNS_DIR / vid / "equity_curve.csv")
    eq["timestamp_utc"] = pd.to_datetime(eq.timestamp_utc, utc=True)
    return eq.set_index("timestamp_utc").equity


def main() -> None:
    variants = [p.name for p in sorted(RUNS_DIR.iterdir()) if p.is_dir() and (p / "run_summary.json").exists()]

    # 1) equity curves (downsampled to ~4000 points per variant for plotting)
    fig, ax = plt.subplots(figsize=(12, 6), constrained_layout=True)
    for v in variants:
        eq = equity(v)
        if len(eq) > 4000:
            eq = eq.iloc[:: len(eq) // 4000]
        lw = 2.2 if v == "baseline_repro" else 1.1
        ax.plot(eq.index, eq.values, lw=lw, label=ar(LABELS.get(v, v)), color=COLORS.get(v))
    ax.axhline(10000, color="gray", ls="--", lw=0.8)
    ax.set_title(ar("منحنى رأس المال — تجارب تتبع الاتجاه (XAUUSD M15، رأس مال 10,000$)"))
    ax.set_ylabel(ar("رأس المال $"))
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    ax.grid(alpha=0.3)
    fig.savefig(CHARTS / "equity_curves.png", dpi=130)
    plt.close(fig)

    # 2) drawdown curves
    fig, ax = plt.subplots(figsize=(12, 5), constrained_layout=True)
    for v in variants:
        eq = equity(v).astype(float)
        dd = eq - np.maximum.accumulate(eq)
        if len(dd) > 4000:
            dd = dd.iloc[:: len(dd) // 4000]
        lw = 2.2 if v == "baseline_repro" else 1.0
        ax.plot(dd.index, dd.values, lw=lw, label=ar(LABELS.get(v, v)), color=COLORS.get(v))
    ax.set_title(ar("التراجع عن القمة — تجارب تتبع الاتجاه"))
    ax.set_ylabel(ar("التراجع $ (سالب تحت القمة)"))
    ax.legend(fontsize=8, loc="upper left", bbox_to_anchor=(1.01, 1.0))
    ax.grid(alpha=0.3)
    fig.savefig(CHARTS / "drawdown_curves.png", dpi=130)
    plt.close(fig)

    # 3) monthly net heatmap (variant x month)
    ta = pd.read_csv(EXPORTS / "TRADE_ANALYSIS.csv")
    ta["opened_at_utc"] = pd.to_datetime(ta.opened_at_utc, utc=True)
    ta["month"] = ta.opened_at_utc.dt.to_period("M").astype(str)
    piv = ta.pivot_table(index="variant", columns="month", values="net_pnl", aggfunc="sum")
    order = [v for v in COLORS if v in piv.index]
    piv = piv.reindex(order)
    fig, ax = plt.subplots(figsize=(14, 5), constrained_layout=True)
    vmax = np.nanpercentile(np.abs(piv.values), 98)
    im = ax.imshow(piv.values, aspect="auto", cmap="RdYlGn", vmin=-vmax, vmax=vmax)
    ax.set_xticks(range(len(piv.columns)), piv.columns, rotation=90, fontsize=6)
    ax.set_yticks(range(len(piv.index)), [ar(LABELS.get(v, v)) for v in piv.index], fontsize=8)
    ax.set_title(ar("صافي الربح الشهري لكل تجربة (حسب شهر الدخول)"))
    fig.colorbar(im, ax=ax, label=ar("صافي $"))
    fig.savefig(CHARTS / "monthly_net_heatmap.png", dpi=130)
    plt.close(fig)

    # 4) weekday + hour attribution (baseline vs key experiments)
    key = ["baseline_repro", "exp1_expansion_only", "exp2_pullback_only",
           "exp3a_sunday_only", "exp3b_h23_only", "exp4a_exp_pullback", "exp4b_exp_pb_timing"]
    wk_order = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.5), constrained_layout=True)
    for v in key:
        sub = ta[ta.variant == v]
        if not len(sub):
            continue
        g = sub.groupby(sub.opened_at_utc.dt.weekday).net_pnl.sum()
        axes[0].plot([wk_order[i] for i in g.index], g.values, marker="o", ms=4,
                     label=ar(LABELS.get(v, v)), color=COLORS.get(v))
        g2 = sub.groupby(sub.opened_at_utc.dt.hour).net_pnl.sum()
        axes[1].plot(g2.index, g2.values, marker="o", ms=3,
                     label=ar(LABELS.get(v, v)), color=COLORS.get(v))
    axes[0].axhline(0, color="gray", lw=0.8)
    axes[0].set_title(ar("صافي الربح حسب يوم الدخول (UTC)"))
    axes[0].set_ylabel(ar("صافي $"))
    axes[0].grid(alpha=0.3)
    axes[1].axhline(0, color="gray", lw=0.8)
    axes[1].set_title(ar("صافي الربح حسب ساعة الدخول (UTC)"))
    axes[1].set_xlabel(ar("ساعة UTC"))
    axes[1].grid(alpha=0.3)
    axes[1].legend(fontsize=7, loc="upper left", bbox_to_anchor=(1.02, 1.0))
    fig.savefig(CHARTS / "weekday_hour_attribution.png", dpi=130)
    plt.close(fig)

    # 5) comparison bars (net P/L + trades)
    comp = pd.read_csv(EXPORTS / "COMPARISON.csv")
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), constrained_layout=True)
    names = [ar(LABELS.get(v, v)) for v in comp.variant_id]
    axes[0].barh(names, comp.Net_PL_usd, color=[COLORS.get(v) for v in comp.variant_id])
    axes[0].axvline(0, color="gray", lw=0.8)
    axes[0].set_title(ar("صافي الربح/الخسارة لكل تجربة"))
    for i, (net, n) in enumerate(zip(comp.Net_PL_usd, comp.Trades)):
        axes[0].text(net, i, f"  {net:,.0f}$ / {n} صفقة", va="center", fontsize=7,
                     ha="left" if net >= 0 else "right")
    axes[1].barh(names, comp.ProfitFactor, color=[COLORS.get(v) for v in comp.variant_id])
    axes[1].axvline(1.0, color="red", ls="--", lw=1)
    axes[1].set_title(ar("Profit Factor لكل تجربة (الحد 1.0)"))
    axes[1].grid(alpha=0.3)
    fig.savefig(CHARTS / "comparison_bars.png", dpi=130)
    plt.close(fig)

    print(f"charts done → {CHARTS}", flush=True)


if __name__ == "__main__":
    main()
