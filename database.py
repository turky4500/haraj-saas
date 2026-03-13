import sqlite3
import hashlib
import os
from datetime import datetime, timedelta
from pathlib import Path

DB_PATH = Path("haraj_saas.db")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

def init_db():
    conn = get_db()
    c = conn.cursor()

    # جدول الإعدادات (للأدمن)
    c.execute("""CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at TEXT DEFAULT (datetime('now'))
    )""")

    # جدول المستخدمين
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL,
        phone TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        is_active INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now'))
    )""")

    # جدول الاشتراكات
    c.execute("""CREATE TABLE IF NOT EXISTS subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        keywords TEXT DEFAULT '[]',
        cities TEXT DEFAULT '[]',
        city_filter_enabled INTEGER DEFAULT 1,
        excluded_words TEXT DEFAULT '[]',
        whatsapp_number TEXT NOT NULL,
        sleep_minutes INTEGER DEFAULT 15,
        quiet_enabled INTEGER DEFAULT 0,
        quiet_start_hour INTEGER DEFAULT 1,
        quiet_start_minute INTEGER DEFAULT 0,
        quiet_end_hour INTEGER DEFAULT 6,
        quiet_end_minute INTEGER DEFAULT 0,
        status TEXT DEFAULT 'active',
        starts_at TEXT DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL,
        sent_total INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now')),
        FOREIGN KEY(user_id) REFERENCES users(id)
    )""")

    # جدول السجلات
    c.execute("""CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subscription_id INTEGER,
        user_id INTEGER,
        message TEXT NOT NULL,
        level TEXT DEFAULT 'info',
        created_at TEXT DEFAULT (datetime('now'))
    )""")

    # جدول الإشعارات المرسلة
    c.execute("""CREATE TABLE IF NOT EXISTS sent_ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        subscription_id INTEGER NOT NULL,
        ad_id TEXT NOT NULL,
        ad_title TEXT,
        ad_url TEXT,
        sent_at TEXT DEFAULT (datetime('now')),
        UNIQUE(subscription_id, ad_id)
    )""")

    # إعدادات افتراضية
    defaults = [
        ("whatsapp_token", ""),
        ("trial_days", "2"),
        ("site_name", "حراج مونيتور"),
    ]
    for key, value in defaults:
        c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (key, value))

    # إنشاء حساب أدمن افتراضي
    admin_email = "admin@haraj.com"
    existing = c.execute("SELECT id FROM users WHERE email=?", (admin_email,)).fetchone()
    if not existing:
        c.execute("""INSERT INTO users (name, email, phone, password_hash, role)
                     VALUES (?, ?, ?, ?, ?)""",
                  ("المدير", admin_email, "0500000000", hash_password("admin123"), "admin"))

    conn.commit()
    conn.close()
    print("✅ قاعدة البيانات جاهزة")
    print("👤 الأدمن: admin@haraj.com / admin123")
