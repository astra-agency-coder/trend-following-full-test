# GOLD HYBRID ENGINE — XAUUSD Only

نظام تداول آلي هجين (Paper Trading فقط) متخصص حصرياً بالذهب **XAUUSD**.
ست مدارس تداول مستقلة برمجياً + محرك قرار مركزي (Hybrid Decision Engine) يختار
المدرسة الأنسب لكل فرصة، في نظام هجين واحد — وليس ستة روبوتات منفصلة.

> **تنبيه ملزم**: هذا النظام **Paper Trading فقط**. لا يحتوي أي مسار لتنفيذ
> أوامر حقيقية، ولا يستخدم `order_send` أو أي دالة تنفيذ من MT5. اتصال MT5
> مخصص لقراءة الأسعار والبيانات فقط. كما أنه **Rule-Based بالكامل** — لا AI
> ولا Machine Learning في أي قرار تداول أو تصنيف سوق.

---

## المكونات الرئيسية

| المكوّن | المسار | الوظيفة |
|---|---|---|
| Market Data Engine | `app/data/` | MT5 read-only + Replay + تحقق من جودة البيانات + كشف الفجوات |
| Market Context Engine | `app/market_context/` | Trend / Volatility / Activity / Liquidity Proxy / Structure / Sessions + DST |
| Strategy Engines (×6) | `app/strategies/` | SMC-ICT, Wyckoff, Trend, MeanReversion, Breakout, OrderFlow |
| Hybrid Decision Engine | `app/hybrid/router.py` | الأهلية، الترتيب الحتمي، الاختيار، منع التضارب |
| Risk Engine | `app/risk/engine.py` | حجم المركز، حدود المخاطر، قاطع الخسارة اليومي |
| Paper Trading Engine | `app/execution/` | فتح/إدارة/إغلاق افتراضي + Spread/Slippage/Commission |
| Shadow Tracking | `app/shadow/` | تتبع الفرص المرفوضة (خارج ربح المحفظة) |
| Audit Journal | `app/journal/` | سجل append-only بسلسلة Hash + تصدير CSV/JSON |
| Performance Analytics | `app/reporting/` | كل المقاييس + تقسيم بالجلسة/المدرسة/الساعة |
| Telegram Bot | `app/telegram/` | إشعارات عربية غنية + أوامر تحكم محمية |
| Backtesting | `app/backtesting/` | نفس الـ Runtime عبر VirtualClock (بدون lookahead) |

## التشغيل السريع

```bash
cd gold_hybrid_engine
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# نسخ الإعدادات والأسرار
cp .env.example .env   # عدّل القيم

# تهيئة قاعدة البيانات
python scripts/init_db.py

# تشغيل دورة واحدة (تجربة سريعة)
python -m app.main --once

# تشغيل مستمر (يتطلب MT5 متصل على ويندوز أو MT5 gateway)
python -m app.main

# تشغيل تجريبي بدون MT5 (بيانات Replay محلية — للتطوير فقط)
python -m app.main --dry-run
```

## الاختبارات

```bash
python -m pytest tests/          # 104 اختبارات (unit + integration + ...)
```

تشمل: اختبارات Unit، Integration، قاعدة البيانات، المدارس الست، محرك القرار،
Paper Trading، Telegram مع Mock، انقطاع البيانات، إعادة التشغيل مع صفقات مفتوحة،
منع تكرار الإشارة، منع استخدام بيانات مستقبلية (No-Lookahead)، والـ Backtesting
مع قابلية إعادة الإنتاج.

## الوثائق

- `docs/INSTALL.md` — دليل التثبيت والتشغيل
- `docs/MT5_SETUP.md` — دليل إعداد MetaTrader 5
- `docs/TELEGRAM_SETUP.md` — دليل إعداد بوت Telegram
- `docs/DEPLOYMENT_VPS.md` — دليل النشر على VPS مع systemd
- `docs/STRATEGIES.md` — التوثيق الكامل لقواعد المدارس الست
- `docs/GLOSSARY.md` — مسرد المصطلحات
- `IMPLEMENTATION_REPORT.md` — ما بُني وما اختُبر وما يحتاج بيانات اعتماد خارجية

## هيكل المشروع

```
gold_hybrid_engine/
├── app/                    # الكود المصدري (كل المحركات)
│   ├── core/               # config, clock, enums, exceptions, logging
│   ├── data/               # محرك البيانات + MT5 adapter + Replay
│   ├── market_context/     # السياق + المؤشرات + الجلسات + البنية
│   ├── strategies/         # المدارس الست + السجل
│   ├── hybrid/             # محرك القرار الهجين
│   ├── risk/               # محرك المخاطر
│   ├── execution/          # محرك Paper Trading + نماذج التعبئة
│   ├── shadow/             # تتبع الظل
│   ├── persistence/        # النماذج + قاعدة البيانات + المستودعات
│   ├── journal/            # سجل التدقيق + المصدرين
│   ├── telegram/           # البوت + الرسائل + المصادقة
│   ├── reporting/          # الإحصائيات
│   ├── backtesting/        # محرك إعادة التشغيل التاريخي
│   ├── bootstrap.py        # الموصل الرئيسي HybridRuntime
│   └── main.py             # نقطة الدخول
├── config/                 # كل الإعدادات (YAML) + strategies/
├── scripts/                # تشغيل/إيقاف/فحص/تصدير/اختبار تاريخي
├── tests/                  # مجموعة الاختبارات (104)
├── deploy/                 # ملف systemd + إعدادات النشر
├── docs/                   # الأدلة
└── requirements.txt
```

## قواعد أمان غير قابلة للتجاوز

1. XAUUSD فقط (مع دعم اختلاف اسم الرمز لدى الوسطاء).
2. Paper Trading فقط — يرفض النظام الإقلاع إذا فُعّل أي مسار تنفيذ حقيقي.
3. Rule-Based فقط — لا AI/ML.
4. ممنوع Martingale / Grid / Averaging Down (مفحوصة في الـ config validator).
5. كل الشموع المستخدمة في القرارات مغلقة (No-Lookahead مفحوص باختبارات).
6. كل قرار/إشارة/رفض/صفقة مسجل بسلسلة Hash قابلة للتحقق.
7. أخطاء مدرسة واحدة لا تُسقط النظام (مفحوص باختبار).

---

## Web API & Dashboard (added in this platform build)

The engine now ships with an authenticated Web API layer (`app/webapi/`, FastAPI) and a
matching premium dashboard (see `../frontend`):

```bash
# demo (simulated market, no MT5 needed) — dashboard data + control work
python -m app.webapi --dry-run --port 8000

# production-ish: API + engine in one process (MT5 provider per config)
python -m app.webapi --engine --port 8000

# API only (reads the database; control endpoints answer 503 NO_RUNTIME)
python -m app.webapi --port 8000
```

- Envelope responses, session cookie + CSRF, OWNER/VIEWER roles, rate limits, idempotent
  pause/resume (audited), SSE stream at `/api/v1/stream/events`, OpenAPI at `/api/docs`.
- Dev credentials (admin/admin123, viewer/viewer123) apply **only** when `WEB_AUTH_USERS` is unset.
- Full contract: `../docs/API_INTEGRATION.md` · deployment: `../docs/DEPLOYMENT.md`.
