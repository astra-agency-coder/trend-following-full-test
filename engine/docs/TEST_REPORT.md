# تقرير نتائج الاختبارات — TEST_REPORT

**التاريخ:** 2026-11-24
**البيئة:** Linux x86_64 — Python 3.12.16 — pytest 9.0.2
**الأمر:** `python -m pytest tests/`
**النتيجة النهائية: ✅ 104 passed / 0 failed / 0 errors — في 42.9 ثانية**

## التوزيع حسب المجموعة

| الملف | العدد | ما يغطيه |
|---|---|---|
| `tests/unit/test_core.py` | 24 | الإعدادات (تحقق الورقة-فقط، رفض Martingale، تجاوزات البيئة)، مدققات البيانات (tick قديم/متصالب/صفري، OHLC، فجوات الشموع)، المؤشرات (SMA/EMA/ATR/RSI/z-score)، تأكيد الـ Swings بلا lookahead، الجلسات وDST (التبديل الربيعي والخريفي 2026، التداخل، عطلة نهاية الأسبوع)، سلسلة التدقيق (سلامة + كشف التلاعب + كشف الحذف) |
| `tests/execution/test_paper_trading.py` | 20 | حساب الحجم (رياضياً، التقريب للأسفل دائماً، حد أدنى، خطر ما بعد التقريب)، موافقات المخاطر (الخطر المجمّع، قاطع الخسارة اليومي، حد المدرسة الواحدة)، آلة حالة الصفقة (انتقال غير قانوني يرفض)، تنفيذ SL/TP على الأسعار القابلة للتنفيذ (BUY يغلق على Bid)، فجوة السعر تُعبّأ عند أول سعر متاح، SL يتضيّق فقط، العمولات (ذهاب/عودة/صفر/نموذج غير مهيأ)، الانزلاق دائماً معاكس، استعادة الصفقات المفتوحة بعد إعادة التشغيل |
| `tests/hybrid/test_router.py` | 15 | الأهلية (SL بالجانب الخطأ، فرصة منتهية، غير مفعّلة، بلا خروج قابل للتنفيذ، سياسة Trailing مقبولة)، الاختيار (مرشح واحد يكفي، الأفضل يفوز، لا إجماع مطلوب)، منع التكرار، حالات NO_CANDIDATE/BLOCKED_BY_DATA/BLOCKED_BY_SYSTEM، الترتيب حتمي عبر 5 تشغيلات، توثيق القرار (عربي + مكونات الدرجة) |
| `tests/integration/test_runtime.py` | 12 | دورات كاملة مع تحقق الاستمرارية في قاعدة البيانات (125 تقييماً/25 قراراً)، دورة حياة الصفقة (فتح/إغلاق + أحداث)، الصفقة مرتبطة بمدرستها، انهيار مدرسة لا يوقف النظام (تقييم ERROR لكل دورة والباقي يعمل)، انقطاع البيانات يمنع الدخول ويسجل حدث صحة، فصل المزود، **No-Lookahead** (المؤشرات والتقييمات لا تتغير بإضافة شموع مستقبلية، لا tick مستقبلي في Replay)، **منع تكرار الفرصة** عبر 50 دورة، **إعادة التشغيل**: صفقات مفتوحة مستعادة + مفاتيح محفوظة + استمرار الدورات |
| `tests/integration/test_telegram.py` | 13 | Outbox (إرسال + منع التكرار بمفتاح الرسالة)، إعادة المحاولة التراجعية، الاستسلام بعد 5 محاولات، تعطيل Telegram لا يرسل شيئاً، قائمة المستخدمين البيضاء، رفض غياب المعرّف، حد المعدل، الأوامر (ردود كل الأوامر الـ12 الموثقة، أمر غير معروف، مستخدم غير مصرح مُتجاهل، تدفق pause/resume، كل أمر مُسجَّل في التدقيق) |
| `tests/integration/test_database.py` | 9 | وجود الـ24 جدولاً، قيد فريد للشموع، NOT NULL لوقف الخسارة، مفتاح Outbox فريد، JSON ذهاب/عودة، هجرات idempotent، **حمايات Paper-Only**: grep آلي يمنع دوال التنفيذ في الكود، الـ MT5 adapter بلا أي دالة تشبه التنفيذ، رفض الإقلاع عند تفعيل تنفيذ حقيقي |
| `tests/integration/test_backtest.py` | 3 | تشغيل backtest كامل مع مقاييس، **قابلية إعادة الإنتاج** (نفس البيانات + البذرة = نتائج مطابقة تماماً على قاعدتين منفصلتين)، قراءة الساعة الحقيقية ممنوعة داخل الـ backtest |
| `tests/strategies/test_strategies.py` | 8 | كل المدارس تقيّم بلا أخطاء مع أسباب رفض آلية، بيانات غير صالحة تحجب التقييم، سوق مسطح لا ينتج مرشحين، عقد المرشح كامل (اتجاه/دخول/SL/TP أو سياسة خروج/مفتاح فرصة)، كشف FVG وDisplacement وتجميع المستويات المتساوية في SMC، حالة OrderFlow المعطلة بسبب البيانات |

## اختبارات البنود الإلزامية في المواصفة (خريطة المطابقة)

| البند الإلزامي | الاختبار |
|---|---|
| Unit Tests | `test_core.py` (24) |
| Integration Tests | `test_runtime.py`, `test_telegram.py`, `test_database.py` |
| Database Tests | `test_database.py::TestSchema`, `TestRoundTrip` |
| Strategy Tests | `test_strategies.py` |
| Decision Engine Tests | `test_router.py` |
| Paper Trading Tests | `test_paper_trading.py` |
| Telegram Tests (Mock) | `test_telegram.py` (MockTelegramClient) |
| Data Disconnection Tests | `test_runtime.py::TestDataDisconnection` |
| Restart Recovery Tests | `test_paper_trading.py::TestRestartRecovery` + `test_runtime.py::TestRestartRecovery` |
| Duplicate Signal Tests | `test_runtime.py::TestDuplicateSignals` + `test_router.py` |
| No Lookahead Tests | `test_runtime.py::TestNoLookahead` + `test_core.py::test_swings_confirmation_no_lookahead` |
| Historical Backtesting | `test_backtest.py` (reproducibility + virtual clock) |
| منع التداول الحقيقي (اختبار H) | `test_database.py::TestPaperOnlyGuards` |

## منهجية البيانات

جميع بيانات الاختبار **حتمية** (مولّد LCG ببذور ثابتة — `generate_synthetic_bars`)
أو مصنوعة يدوياً (شموع مسطحة، أنماط FVG/Displacement مركّبة). لا اعتماد على
شبكة أو MT5 أو Telegram حقيقي؛ الـ Mocks مدمجة (`MockTelegramClient`،
`ReplayProvider`). أي فشل قابل للتكرار بنفس الخطوات.

## تشغيلات الإصلاح الموثقة

عُثر على 10 إخفاقات في التشغيل الأول وشُخّصت أصولها وصُلحت ثم أعيد التشغيل:
(1) خطأ حسابي في توقع حجم الاختبار، (2) استيراد `Protocol` متأخر في
mt5_adapter، (3) grep يلتقط ثابت الحظر نفسه، (4) `replace` على frozen
dataclass، (5) ترتيب المرشحين في الاختبار، (6) سياسة Break-Even أغلقت دون
TP في اختبار التكاليف، (7) بيانات `data_quality` نصية بدل enum، (8) backoff
يمنع المحاولة الثانية فوراً (اختبار يضبط وقت الاستحقاق)، (9) عمر التِك اللانهائي
في BACKTEST عطّل كشف الانقطاع (استُبدل بـ step×10)، (10) اسم ثابت خاطئ
(`DEFAULT_CANDIDATES`). التشغيل النهائي: **104/104 ✅**.
