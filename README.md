# مدرسة Trend Following — اختبار تاريخي كامل ومستقل على XAUUSD

> **ملاحظة:** الصور (8 رسوم إحصائية + صورة لكل صفقة) مستثناة من هذا المستودع لتخفيفه، وهي متوفرة في أرشيف الاختبار الكامل.

## الخلاصة في 30 ثانية

اختبار مستقل كامل لمدرسة **تتبع الاتجاه (Trend Following) وحدها** على الذهب (XAUUSD) عبر محرك التشغيل الإنتاجي الحقيقي، بإعدادات الإنتاج **المجمدة** وبلا أي تحسين لمعاملات. النتيجة **سلبية بوضوح** في السيناريوهات الثلاثة:

| السيناريو | افتراضات التكلفة (سبريد/انزلاق/عمولة لكل لوط لكل جهة) | الصفقات | الرصيد النهائي | صافي الربح |
|---|---|---|---|---|
| base | 0.25$ / 2 نقطة / 7$ | 2,523 | 964.86$ | **-9,035.14$** |
| conservative | 0.40$ / 4 نقطة / 9$ | 2,211 | 718.90$ | **-9,281.10$** |
| high | 0.60$ / 8 نقطة / 12$ | 1,915 | 561.63$ | **-9,438.37$** |

حساب ورقي برأس مال 10,000$ ومخاطرة 0.5%/صفقة. بالسيناريو الأساسي: PF **0.5712**، Win **29.49%**، أقصى تراجع **-9,148.57$ (-90.46%)**، و**إفلاس وظيفي في 2025-09** (رصيد ~965$ يجعل مخاطرة أي صفقة بأدنى لوط أكبر من حد المخاطرة المجمد 0.5% → 1,776 رفضاً متتالياً من محرك المخاطر حتى نهاية البيانات).

**الحكم:** المحرك والتنفيذ سليمان ومختبَران (13/13 اختبارات سلامة اصطناعية، 103/103 حزمة المشروع، 19/19 تحقق نهائي آلي) — المشكلة في قواعد المدرسة نفسها بهذه الإعدادات على الذهب M15: معدل تداول مرتفع (~36.9/شهر)، توقع سالب لكل صفقة (-3.58$)، وتكاليف تتراكم على صفقات قصيرة العمر (وسيط 1.51 ساعة).

## المنهجية

- **البيانات:** HistData.com — XAUUSD M1 Bid OHLC، من 2020-01-01 حتى 2026-11-13، 2,389,789 شمعة صالحة، تغطية 96.64% من تقويم السوق، لا شموع اصطناعية ولا ملء فجوات. Bid-only → Ask مركب → لذلك **ثلاثة سيناريوهات تكلفة اصطناعية موثقة** (ليست سبريداً تاريخياً حقيقياً).
- **التنفيذ:** دورة تحليل واحدة كل إغلاق M15 (159,247 دورة)، دخول فوري على تيك الإغلاق + انزلاق، شموع داخلية محافظة [O,L,H,L,C] مع فحص جهة الوقف قبل الهدف، وقف متحرك تشديد-فقط، بلا هدف ثابت (سياسة TRAILING_ATR).
- **10 أيام تهيئة** قبل نافذة القياس، صفقاتها مستثناة من النتائج.
- 10,168 إشارة مكتشفة → 2,523 منفذة، 7,645 مرفوضة (7552 منها بعد اختيار المخاطر).
- **تقسيم زمني 60/20/20 بلا عشوائية:** تدريب حتى 2024-02-13 (-8,750.66$)، تحقق حتى 2025-06-29 (-253.84$)، **خارج العينة** (-30.69$ على 12 صفقة فقط — اقرأ القسم 4.1 من التقرير النهائي: الحساب كان قد دُمّر وظيفياً قبل معظم نافذة OOS).
- **Walk-forward بـ10 طيات** بإعدادات مجمدة: كل الطيات التسع النشطة خاسرة (PF بين 0.013 و0.672).

## محتويات المستودع

| المسار | المحتوى |
|---|---|
| `TREND_FOLLOWING_FINAL_REPORT.md` | التقرير النهائي الكامل (الأرقام، التقسيمات، متى تكسب المدرسة ومتى تخسر) |
| `TREND_FOLLOWING_INTEGRITY_AUDIT.md` | تدقيق الكود كاملاً (14 سؤالاً) مع الملاحظات TF-1/TF-2/TF-3 |
| `TREND_FOLLOWING_VALIDATION_REPORT.md` | التحقق النهائي الآلي 19/19 |
| `TREND_FOLLOWING_PERFORMANCE.json` / `RUN_METADATA.json` | الأداء والبيانات الوصفية للتشغيل |
| `TREND_FOLLOWING_TRADES.csv` / `_SIGNALS.csv` / `_REJECTED_SIGNALS.csv` / `_DECISION_LOG.csv` / `_EQUITY.csv` | كل صفقة وإشارة وقرار ونقطة رصيد |
| `runs/base\|conservative\|high/` | مخرجات المحرك الخام للسيناريوهات الثلاثة |
| `trade_reports/` | تقرير Markdown لكل صفقة من الـ2,523 |
| `code_backup/` | نسخة كود المدرسة + المحرك التنفيذي + إعدادات YAML المجمدة وقت الاختبار |
| `data_quality/` | جودة بيانات HistData والتغطية والفجوات |
| `engine/` | **الكود المصدري الكامل للمشروع** (محرك gold_hybrid_engine: app/ scripts/ config/ tests/ docs/ deploy/) — المصدر الذي شغّل الاختبار |
| `research/` | (فرع `research/expansion-followup`) تجارب المتابعة: إعادة إنتاج الأساس + فلاتر التوسع/الارتداد/التوقيت + تركيباتها |

## English summary

An independent, full-period integrity test of a **Trend Following** strategy school (only — no other schools, no hybrid router) on **XAUUSD (gold) M15**, executed by the real production engine with **frozen production settings and zero parameter optimization**. Data: HistData.com M1 bid candles, 2020-01-01 → 2026-11-13 (2,389,789 bars, 96.64% market-calendar coverage). Paper account of $10,000 with 0.5% risk per trade.

**Result: clearly negative.** Net P&L of **-$9,035.14 (base)**, **-$9,281.10 (conservative)** and **-$9,438.37 (high)** across three documented synthetic cost scenarios (2,523 / 2,211 / 1,915 closed trades). Profit Factor 0.5712, Win rate 29.49%, max drawdown -90.46%, with a **functional bankruptcy in Sep 2025** (balance ~$965 → risk engine rejects every selection until the end of data). The engine and execution passed all integrity checks (13/13 synthetic scenario tests, 103/103 full suite, 19/19 automated validation) — the failure lies in the school's rules themselves under these settings on gold: high trade frequency (~36.9/month), negative expectancy (-$3.58/trade), and cost drag on short-lived trades (median 1.51h). The strategy only shines in strong-expansion segments (see the final report, section 7).

**This is a negative result published honestly** — the goal of the test was correctness and reproducibility, not profit.

## تنويه

هذا اختبار تاريخي على بيانات ماضية بحساب ورقي، لأغراض بحثية وتوثيقية فقط، وليس نصيحة مالية ولا توصية تداول.
