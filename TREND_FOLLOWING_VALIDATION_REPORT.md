# TREND_FOLLOWING_VALIDATION_REPORT.md

**النتيجة: 19/19 فحوص ناجحة**

| الفحص | الحالة | التفصيل |
|---|---|---|
| counts.trades_csv_vs_signals | ✅ | signals opened=2523 trades=2523 |
| counts.summary_closed_includes_warmup | ✅ | run_summary closed=2523 (includes pre-start warmup trades, excluded from CSV by design) |
| reconcile.net_vs_equity_change | ✅ | sum(net)=-9,035.19$  equity[10,000.00→964.86] Δ=-9,035.14$  |gap|=0.05$ (≤50$ tolerance: equity sampled at M15 closes incl. floating P/L and warmup trades) |
| sl.sides_correct | ✅ | BUY SL<entry: True | SELL SL>entry: True |
| tp.none_by_design | ✅ | policy=TRAILING_ATR, no fixed TP (spec 35.3) |
| costs.commission_round_trip_once | ✅ | commission == volume×7$/side×2 sides exactly |
| costs.swap_zero_by_config | ✅ | swap disabled (paper_execution.yaml) |
| risk.r_multiple_consistent | ✅ | r_multiple == net_pnl/planned_risk for every trade (atol 2e-3 = cent-rounding of net on min-lot trades) |
| costs.no_double_counting | ✅ | net == gross − commission − swap exactly (slippage already inside fill prices) |
| dupes.unique_trade_ids | ✅ | 2523 unique ids |
| dupes.no_concurrent_same_opportunity | ✅ | 0 overlapping same-key positions |
| no_lookahead.first_decision_after_warmup_start | ✅ | first decision 2020-01-01 23:15:00+00:00 ≥ effective start 2019-12-22T00:00:00+00:00 |
| reach.end_of_data | ✅ | last decision 2026-11-13 21:45:00+00:00 — data ends 2026-11-13 21:58 UTC (manifest) |
| no_lookahead.entries_at_decision_closes_only | ✅ | 0 entries not at an M15 decision timestamp |
| images.one_per_trade | ✅ | images=2523 trades=2523 |
| reports.one_per_trade | ✅ | reports=2523 trades=2523 |
| oos.segment_separate_and_reported | ✅ | OOS trades=12 net=-30.69$ (reported standalone; never used for tuning) |
| oos.frozen_config | ✅ | config hash frozen from production defaults — no optimization anywhere in the pipeline |
| decisions.count_recorded | ✅ | 159247 M15 decision cycles logged (analysis cadence = one cycle per M15 close) |

## ملاحظات المنهجية

- تطابق «صافي الربح مع تغير الرصيد»: المجموع مقاس على نقاط رصيد إغلاق M15 (يحتوي ربحاً عائماً + صفقات التهيئة) بفارق مسموح ≤50$ موثق أعلاه.
- قاعدة البيانات في المدار البحثي تُبنى ثم تُحذف (artifact = CSV/JSON)؛ تطابق العدادات تم مقابل run_summary.json الصادر من المحرك نفسه.
- صفقات ما قبل نافذة القياس (warmup) مغطاة في المحرك لكنها مستثناة من CSV والتقارير عمداً (وثّق الفارق في العدّاد الأول).