# دليل التثبيت والتشغيل — INSTALL

## المتطلبات

- Python 3.11+ (مطوَّر ومختبر على 3.12)
- (للإنتاج) PostgreSQL 14+ — أو SQLite للتطوير/الاختبار
- (للبيانات الحية) MetaTrader 5 يعمل على جهاز Windows — انظر `MT5_SETUP.md`

## خطوات التثبيت

```bash
# 1) استخراج المشروع
cd gold_hybrid_engine

# 2) بيئة افتراضية
python3 -m venv .venv
source .venv/bin/activate        # Linux
# .venv\Scripts\activate         # Windows

# 3) الاعتماديات
pip install -r requirements.txt
# على جهاز Windows الذي سيقرأ MT5 فقط:
# pip install MetaTrader5

# 4) الأسرار والإعدادات
cp .env.example .env
# عدّل: DATABASE_URL, MT5_*, TELEGRAM_*

# 5) تهيئة قاعدة البيانات (24 جدولاً)
python scripts/init_db.py
# أو مع ملفات migration (إن وُجدت):
python scripts/init_db.py --migrations

# 6) التحقق من صحة كل شيء
python -m pytest tests/          # يجب أن تنجح 104/104
```

## التشغيل

```bash
# دورة تحليل واحدة ثم خروج (مناسب للفحص)
python -m app.main --once

# تشغيل مستمر (الوضع الطبيعي على VPS)
python -m app.main --poll-seconds 5

# تشغيل محلي بدون MT5 (بيانات Replay افتراضية — للتطوير فقط، ليست بيانات حقيقية)
python -m app.main --dry-run
```

أو عبر السكربتات:

```bash
./scripts/start.sh live      # تشغيل مستمر
./scripts/start.sh once      # دورة واحدة
./scripts/start.sh dry-run   # بدون MT5
./scripts/stop.sh            # إيقاف نظيف (ينهي الدورة الحالية أولاً)
./scripts/restart.sh         # إعادة تشغيل مع استعادة الحالة
./scripts/healthcheck.sh     # فحص الصحة (0=سليم, 1=متحفظ, 2=متوقف)
```

## إعادة التشغيل واستعادة الحالة

- الصفقات المفتوحة تُحفظ في قاعدة البيانات وتُستعاد تلقائياً عند الإقلاع.
- مفاتيح الفرص المنفذة (`opportunity_key`) تبقى محفوظة، فلا تُنفَّذ الإشارة
  نفسها مرتين بعد إعادة التشغيل (مفحوص باختبار `TestRestartRecovery`).
- كل صفقة تظل مرتبطة بالمدرسة التي فتحتها حتى إغلاقها.

## قاعدة البيانات

| الوضع | رابط |
|---|---|
| تطوير/اختبار | `sqlite:///gold_hybrid.db` |
| إنتاج (موصى به) | `postgresql+psycopg2://user:pass@localhost:5432/gold_hybrid` |

النسخ الاحتياطي (إنتاج): نسخة يومية لقاعدة PostgreSQL (`pg_dump`) + اختبار
استعادة دوري. البيانات المالية والسجلات لا تُحذف تلقائياً.

## التصدير والفحوصات

```bash
python scripts/export_journal.py --out exports/journal            # كل الجداول CSV/JSON
python scripts/export_journal.py --table paper_trades --format csv
python scripts/verify_audit_chain.py                              # تحقق سلسلة Hash
python scripts/generate_report.py --type daily                    # تقرير JSON
python scripts/run_backtest.py --days 3                           # اختبار تاريخي (Replay)
python scripts/run_backtest.py --bars-csv data/xauusd_m15.csv     # ببيانات حقيقية مستوردة
```

## استكشاف الأخطاء

| المشكلة | السبب المحتمل | الحل |
|---|---|---|
| `MetaTrader5 package not available` | تشغيل على Linux | استخدم `--dry-run` أو اربط MT5 gateway (انظر MT5_SETUP) |
| `no gold symbol found` | اسم الرمز مختلف عند الوسيط | اضبط `SOURCE_SYMBOL` في `.env` |
| `DATA_FEED_DEGRADED` متكرر | MT5 غير متصل أو السوق مغلق | تحقق من اتصال الوسيط؛ السوق مغلق عطلات نهاية الأسبوع |
| رسائل Telegram لا تصل | توكن/Chat ID | راجع `TELEGRAM_SETUP.md` وتحقق من `TELEGRAM_ALLOWED_USER_IDS` |
