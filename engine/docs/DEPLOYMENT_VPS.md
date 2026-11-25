# دليل النشر على VPS — DEPLOYMENT_VPS

مختبر على Linux (Python 3.12). النمط الموصى به: المحرك على VPS لينكس +
PostgreSQL، والبيانات من MT5 على جهاز Windows أو عبر استيراد دوري
(انظر `MT5_SETUP.md` — النمط B).

## 1) تجهيز الخادم

```bash
sudo apt update && sudo apt install -y python3.11 python3.11-venv postgresql
sudo useradd -m -s /bin/bash goldengine
sudo mkdir -p /opt/gold-hybrid-engine && sudo chown goldengine:goldengine /opt/gold-hybrid-engine
```

## 2) قاعدة البيانات (PostgreSQL للإنتاج)

```bash
sudo -u postgres psql <<'SQL'
CREATE USER goldengine WITH PASSWORD 'CHANGE_ME_STRONG';
CREATE DATABASE gold_hybrid OWNER goldengine;
SQL
```

## 3) نشر المشروع

```bash
sudo -u goldengine -i
cd /opt/gold-hybrid-engine
# انسخ المشروع هنا (git clone أو scp أو unzip)
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env && nano .env    # املأ الأسرار (chmod 600 .env)
.venv/bin/python scripts/init_db.py  # 24 جدولاً + الفهارس
.venv/bin/python -m pytest tests/    # تحقق نهائي
```

## 4) الخدمة الدائمة (systemd)

```bash
sudo cp deploy/gold-hybrid-engine.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gold-hybrid-engine
sudo systemctl status gold-hybrid-engine
journalctl -u gold-hybrid-engine -f          # متابعة السجلات
```

الملف المرفق يتضمن: `Restart=always` (إعادة تشغيل بعد 15 ثانية من أي انهيار)،
`TimeoutStopSec=60` (إنهاء نظيف بعد اكتمال الدورة الجارية)، وتقوية أمان
(`NoNewPrivileges`, `ProtectSystem=full`, `ProtectHome`, `PrivateTmp`).

## 5) مراقبة الصحة والتدوير

```bash
# cron (crontab -e):
*/5 * * * * /opt/gold-hybrid-engine/scripts/healthcheck.sh >> logs/health.log 2>&1
15 3 * * * /opt/gold-hybrid-engine/scripts/maintenance.sh >> logs/maintenance.log 2>&1
0 6 * * 1 cd /opt/gold-hybrid-engine && .venv/bin/python scripts/generate_report.py --type weekly >> logs/weekly.log 2>&1
```

- `healthcheck.sh`: يقرأ آخر سجل JSON ويعلن HEALTHY/DEGRADED/DOWN.
- `maintenance.sh`: يضغط سجلات .log الأقدم من 7 أيام، يحذف المضغوط الأقدم من
  90 يوماً، ويتحقق من مساحة القرص (حد أدنى 2GB — قاعدة البيانات لا تُمس).
- ملفات السجل نفسها دوّارة من داخل بايثون (`RotatingFileHandler` — 20MB × 10).

## 6) النسخ الاحتياطي والاستعادة

```bash
# نسخة يومية (cron 04:00):
0 4 * * * sudo -u postgres pg_dump gold_hybrid | gzip > /var/backups/gold_hybrid_$(date +\%F).sql.gz

# استعادة:
gunzip -c /var/backups/gold_hybrid_2026-01-20.sql.gz | sudo -u postgres psql gold_hybrid
```

اختبر الاستعادة دورياً على خادم تجريبي. البيانات المالية/السجلات لا تُحذف
تلقائياً؛ Tick data القديمة يمكن أرشفتها بحسب سياسة قابلة للضبط مع إبقاء
الشموع والملخصات لإعادة الاختبار.

## 7) سلوك إعادة التشغيل (مهم)

- عند إعادة تشغيل الخدمة مع صفقات مفتوحة: تُستعاد الصفقات من قاعدة البيانات
  مع سياسات إدارتها الأصلية (المدرسة، SL/TP، Excursion tracking).
- مفاتيح الفرص المنفذة محفوظة، لذا لا تُنفَّذ إشارة مرتين بعد ال restart.
- آخر حالة سوق تُبنى من جديد عند أول دورة؛ لا اعتماد على أي ذاكرة معلقة.

## 8) إدارة الأسرار

- كل الأسرار في `.env` (صلاحية 600، خارج git عبر `.gitignore`).
- النظام يقرأها من متغيرات البيئة فقط، ويمسحها من أي سجل
  (`token`, `password`... → `***REDACTED***`).
- لا تضع أسراراً في ملفات `config/*.yaml` إطلاقاً.
