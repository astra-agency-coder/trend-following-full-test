# دليل إعداد MetaTrader 5 — MT5_SETUP

## نمطا التشغيل المدعومان

### النمط A: المحرك على نفس جهاز Windows (الأبسط)

حزمة `MetaTrader5` لبايثون تعمل على Windows فقط.

1. ثبّت MetaTrader 5 من وسيطك وافتح حساب **Demo** (قراءة بيانات فقط — لا حاجة
   لتمكين التداول من الإعدادات أصلاً).
2. فعّل عرض الرمز في Market Watch: ابحث عن `XAUUSD` (أو `GOLD`/`XAUUSDm` حسب
   الوسيط) واضغط Show.
3. ثبّت بايثون 3.11+ ثم:
   ```powershell
   pip install -r requirements.txt
   pip install MetaTrader5
   ```
4. املأ `.env`:
   ```ini
   MARKET_DATA_PROVIDER=mt5
   MT5_LOGIN=12345678          # اختياري: تسجيل دخول برمجي
   MT5_PASSWORD=xxxxx
   MT5_SERVER=Broker-Server
   MT5_TERMINAL_PATH=C:\Program Files\MetaTrader 5\terminal64.exe
   SOURCE_SYMBOL=XAUUSD        # اسم الرمز عند وسيطك
   ```
5. شغّل: `python -m app.main`

بدون بيانات دخول MT5، يستخدم الـ adapter الاتصال التفاعلي مع أي طرفية مفتوحة
على نفس الجهاز (`initialize()` بدون credentials) — وهذا يكفي لقراءة الأسعار.

### النمط B: المحرك على Linux VPS + MT5 على Windows (الموصى به للإنتاج)

حزّة MT5 تعمل على Windows، والمحرك مصمم للعمل على VPS لينكس 24/7. الحل
المعماري المدمج:

1. شغّل طرفية MT5 على أي جهاز Windows (منزلي أو VPS رخيص) مع السماح بـ
   "Algo Trading" من زر الأداة.
2. المحرك يقرأ البيانات عبر أي Bridge/Gateway تختاره:
   - **خيار 1 — استيراد دوري**: صدّر الشموع CSV من MT5 (كل ساعة مثلاً)
     واستوردها: `python scripts/run_backtest.py --bars-csv data/xauusd_m15.csv`
     وملء `market_bars` للتشغيل شبه الحي من قاعدة البيانات.
   - **خيار 2 — ZeroMQ/REST gateway مخصص**: طبقة `MarketDataProvider`
     (بروتوكول في `app/data/mt5_adapter.py`) قبل أربع دوال فقط:
     `connect`, `get_symbol_info`, `get_latest_tick`, `get_bars`. نفّذها
     في ملف جديد يقرأ من gateway لديك — دون تغيير أي شيء آخر في النظام.
3. لهذا السبب بُنيت واجهة موحدة: إضافة مصدر بيانات جديد لا تتطلب إعادة بناء.

## ملاحظات فنية مهمة

- **قراءة فقط**: الـ adapter لا يستورد ولا يستدعي `order_send` أو أي دالة
  تنفيذ — مفحوص آلياً باختبار `test_no_real_order_tables_or_functions`.
- **اسم الرمز**: يُحل تلقائياً من قائمة المرشحين
  (`XAUUSD, XAUUSDm, XAUUSD.a, GOLD...`) أو عبر `SOURCE_SYMBOL` الصريح.
  تُخزن مواصفات الرمز (digits, point, contract size, volume step) في جدول
  `symbol_specs` وتُستخدم في حساب حجم المركز.
- **توقيت MT5**: الطرفية تعيد وقت السيرفر بلا منطقة زمنية؛ النظام يعامله
  كـ UTC ويوثّق كل الطوابع الزمنية بالتوقيت العالمي في قاعدة البيانات.
- **إعادة الاتصال**: عند انقطاع MT5 تصدر حالة `DATA_FEED_DEGRADED` وتُوقف
  الدخول الجديد (وليس النظام)، وتُستأنف تلقائياً عند عودة التدفق
  (`DATA_FEED_RECOVERED`) — مفحوص باختبار `TestDataDisconnection`.
- **السبريد**: يقرأ من Bid/Ask الحقيقيين لكل tick، وليس قيمة افتراضية.
- **السوق مغلق**: عطلات نهاية الأسبوع تظهر `CLOSED` في الجلسات، وفرق العمر
  الكبير بين التِكات يظهر كـ `STALE` — سلوك مقصود لا خطأ.
