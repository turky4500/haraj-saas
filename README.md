# 🚀 حراج مونيتور SaaS — دليل التشغيل

## هيكل المشروع
```
haraj-saas/
├── main.py              ← التطبيق الرئيسي (FastAPI)
├── database.py          ← قاعدة البيانات (SQLite)
├── requirements.txt     ← المكتبات المطلوبة
├── bot/
│   └── haraj_bot.py     ← محرك البوت
├── templates/           ← صفحات HTML
│   ├── base.html
│   ├── login.html
│   ├── register.html
│   ├── dashboard.html
│   ├── edit_sub.html
│   ├── admin.html
│   ├── admin_users.html
│   ├── admin_user_detail.html
│   ├── admin_settings.html
│   └── admin_logs.html
└── static/              ← ملفات CSS/JS إضافية
```

## التشغيل المحلي

```bash
# 1. تثبيت المكتبات
pip install -r requirements.txt

# 2. تشغيل الموقع
uvicorn main:app --reload --port 8000

# 3. افتح المتصفح
http://localhost:8000
```

## بيانات الدخول الافتراضية
- **الأدمن**: admin@haraj.com / admin123

## الرفع على Railway

1. ارفع المجلد على GitHub
2. افتح railway.app وأنشئ مشروع جديد
3. اربطه بـ GitHub
4. أضف متغير البيئة: `SECRET_KEY=your-secret-key`
5. أضف ملف `Procfile`:
   ```
   web: uvicorn main:app --host 0.0.0.0 --port $PORT
   ```

## الإعدادات المهمة بعد الرفع

1. سجّل دخول كأدمن
2. اذهب لـ **الإعدادات**
3. أضف **توكن تكوين** للواتساب
4. غيّر **كلمة مرور الأدمن** (من قاعدة البيانات)

## مزايا النظام
- ✅ تسجيل مستخدمين مع اشتراك تجريبي مجاني (يومان)
- ✅ الأدمن يضيف/يمدد/يوقف اشتراكات
- ✅ كل مستخدم يحدد كلماته وإعداداته
- ✅ التوكن مركزي عند الأدمن فقط
- ✅ البوت يشتغل 24/7 في الخلفية
- ✅ سجلات كاملة لكل الاشتراكات
