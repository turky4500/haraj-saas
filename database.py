import sqlite3
import hashlib
from datetime import datetime
from pathlib import Path

DB_PATH = Path("haraj_saas.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # تفعيل وضع WAL يمنع تعارض القراءة والكتابة (بديل الـ Lock في النسخة القديمة)
    conn.execute("PRAGMA journal_mode=WAL") 
    return conn

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def init_db():
    conn = get_db()
    c = conn.cursor()

    # إعدادات النظام
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL
    )""")

    # المستخدمين
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )""")

    # الاشتراكات (شملت كل خيارات الكود الأصلي)
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        keywords TEXT DEFAULT '[]',
        cities TEXT DEFAULT '[]',
        city_filter_enabled INTEGER DEFAULT 1,
        exclude_enabled INTEGER DEFAULT 0,
        excluded_words TEXT DEFAULT '[]',
        whatsapp_number TEXT NOT NULL,
        sleep_minutes INTEGER DEFAULT 15,
        quiet_enabled INTEGER DEFAULT 0,
        quiet_start_hour INTEGER DEFAULT 1,
        quiet_start_minute INTEGER DEFAULT 0,
        quiet_end_hour INTEGER DEFAULT 6,
        quiet_end_minute INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        starts_at TEXT DEFAULT (datetime('now', 'localtime')),
        expires_at TEXT NOT NULL,
        sent_total INTEGER DEFAULT 0,
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")

    # السجلات (Logs)
    c.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subscription_id INTEGER,
        message TEXT NOT NULL,
        level TEXT DEFAULT 'info',
        created_at TEXT DEFAULT (datetime('now', 'localtime'))
    )""")

    # الإعلانات المرسلة (بديل ملفات seen_ids.json)
    c.execute("""CREATE TABLE IF NOT EXISTS sent_ads (
        subscription_id INTEGER NOT NULL,
        ad_id TEXT NOT NULL,
        UNIQUE(subscription_id, ad_id)
    )""")

    # إنشاء مدير افتراضي إذا لم يوجد
    admin_email = "admin@haraj.com"
    if not c.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone():
        c.execute("""INSERT INTO users (name, email, phone, password_hash, role)
                     VALUES (?, ?, ?, ?, ?)""",
                  ("مدير النظام", admin_email, "0500000000", hash_password("admin123"), "admin"))
        
    # إعدادات افتراضية
    defaults = [("whatsapp_token", ""), ("site_name", "راصد حراج"), ("bot_global_status", "1")]
    for k, v in defaults:
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))

    conn.commit()
    conn.close()
    print("✅ تم بناء قاعدة البيانات بنجاح.")
