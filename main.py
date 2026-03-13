"""
Haraj SaaS - التطبيق الرئيسي
"""
import json, logging, os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from database import get_db, hash_password, init_db, DB_PATH
from bot.haraj_bot import BotManager

# ===== إعداد التطبيق =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("haraj_app")

app = FastAPI(title="Haraj SaaS")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "haraj-secret-2024"))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ===== دوال قاعدة البيانات للبوت =====

def db_get_all_active_subs():
    conn = get_db()
    rows = conn.execute("SELECT * FROM subscriptions WHERE status='active'").fetchall()
    conn.close()
    return [dict(r) for r in rows]

def db_get_sub(sub_id: int):
    conn = get_db()
    row = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    conn.close()
    return dict(row) if row else None

def db_get_token():
    conn = get_db()
    row = conn.execute("SELECT value FROM settings WHERE key='whatsapp_token'").fetchone()
    conn.close()
    return row["value"] if row else ""

def db_mark_sent(sub_id: int, ad_id: str, check_only=False, title="", url="") -> bool:
    conn = get_db()
    if check_only:
        r = conn.execute("SELECT 1 FROM sent_ads WHERE subscription_id=? AND ad_id=?", (sub_id, ad_id)).fetchone()
        conn.close()
        return r is not None
    try:
        conn.execute("INSERT OR IGNORE INTO sent_ads (subscription_id, ad_id, ad_title, ad_url) VALUES (?,?,?,?)",
                     (sub_id, ad_id, title, url))
        conn.commit()
    except Exception: pass
    conn.close()
    return False

def db_add_log(sub_id: int, msg: str, level="info"):
    try:
        conn = get_db()
        conn.execute("INSERT INTO logs (subscription_id, message, level) VALUES (?,?,?)", (sub_id, msg, level))
        conn.commit()
        conn.close()
    except Exception: pass

def db_update_total(sub_id: int, total: int):
    conn = get_db()
    conn.execute("UPDATE subscriptions SET sent_total=? WHERE id=?", (total, sub_id))
    conn.commit()
    conn.close()

# ===== تشغيل البوت =====
bot = BotManager(db_get_all_active_subs, db_get_token, db_mark_sent,
                 db_add_log, db_update_total, db_get_sub)

@app.on_event("startup")
async def startup():
    init_db()
    bot.start_all_active()
    logger.info("✅ التطبيق بدأ والبوت يعمل")

# ===== مساعدات الجلسة =====

def get_current_user(request: Request):
    return request.session.get("user")

def require_login(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user

def require_admin(request: Request):
    user = require_login(request)
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="ممنوع")
    return user

# ============================
# ========= الصفحات ==========
# ============================

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = get_current_user(request)
    if user:
        if user["role"] == "admin":
            return RedirectResponse("/admin", status_code=302)
        return RedirectResponse("/dashboard", status_code=302)
    return RedirectResponse("/login", status_code=302)

# ===== تسجيل الدخول =====

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=? AND password_hash=? AND is_active=1",
                        (email, hash_password(password))).fetchone()
    conn.close()
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "البريد أو كلمة المرور غلط"})
    request.session["user"] = {"id": user["id"], "name": user["name"],
                                "email": user["email"], "role": user["role"]}
    if user["role"] == "admin":
        return RedirectResponse("/admin", status_code=302)
    return RedirectResponse("/dashboard", status_code=302)

# ===== إنشاء حساب جديد =====

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register_post(request: Request,
                        name: str = Form(...), email: str = Form(...),
                        phone: str = Form(...), password: str = Form(...)):
    conn = get_db()
    existing = conn.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if existing:
        conn.close()
        return templates.TemplateResponse("register.html", {"request": request, "error": "البريد مسجل مسبقاً"})

    cursor = conn.execute("INSERT INTO users (name, email, phone, password_hash) VALUES (?,?,?,?)",
                          (name, email, phone, hash_password(password)))
    user_id = cursor.lastrowid

    # اشتراك تجريبي مجاني
    trial_row = conn.execute("SELECT value FROM settings WHERE key='trial_days'").fetchone()
    trial_days = int(trial_row["value"]) if trial_row else 2
    expires = datetime.now() + timedelta(days=trial_days)
    conn.execute("""INSERT INTO subscriptions (user_id, name, whatsapp_number, expires_at, status)
                    VALUES (?,?,?,?,?)""",
                 (user_id, f"اشتراك {name}", phone, expires.isoformat(), "active"))
    conn.commit()
    sub_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()

    # تشغيل البوت للاشتراك الجديد
    bot.start_sub(sub_id)

    request.session["user"] = {"id": user_id, "name": name, "email": email, "role": "user"}
    return RedirectResponse("/dashboard", status_code=302)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

# ===== لوحة المستخدم =====

@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    subs = conn.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY created_at DESC",
                        (user["id"],)).fetchall()
    conn.close()
    now = datetime.now()
    subs_data = []
    for s in subs:
        d = dict(s)
        try:
            exp = datetime.fromisoformat(d["expires_at"])
            d["days_left"] = max(0, (exp - now).days)
            d["expired"] = now >= exp
        except Exception:
            d["days_left"] = 0
            d["expired"] = True
        d["bot_running"] = bot.threads.get(d["id"]) and bot.threads[d["id"]].is_alive()
        subs_data.append(d)
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "subs": subs_data})

# ===== تعديل اشتراك المستخدم =====

@app.get("/subscription/{sub_id}/edit", response_class=HTMLResponse)
async def edit_sub_page(request: Request, sub_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    sub = conn.execute("SELECT * FROM subscriptions WHERE id=? AND user_id=?",
                       (sub_id, user["id"])).fetchone()
    conn.close()
    if not sub:
        raise HTTPException(404)
    sub_data = dict(sub)
    sub_data["keywords"] = json.loads(sub_data.get("keywords","[]"))
    sub_data["cities"] = json.loads(sub_data.get("cities","[]"))
    sub_data["excluded_words"] = json.loads(sub_data.get("excluded_words","[]"))
    return templates.TemplateResponse("edit_sub.html", {"request": request, "user": user, "sub": sub_data})

@app.post("/subscription/{sub_id}/edit")
async def edit_sub_post(request: Request, sub_id: int):
    user = get_current_user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    keywords_raw = form.get("keywords","")
    cities_raw = form.get("cities","")
    excluded_raw = form.get("excluded_words","")
    keywords = [k.strip() for k in keywords_raw.split("\n") if k.strip()]
    cities = [c.strip() for c in cities_raw.split("\n") if c.strip()]
    excluded = [e.strip() for e in excluded_raw.split("\n") if e.strip()]

    conn = get_db()
    conn.execute("""UPDATE subscriptions SET
        keywords=?, cities=?, excluded_words=?,
        city_filter_enabled=?, quiet_enabled=?,
        quiet_start_hour=?, quiet_start_minute=?,
        quiet_end_hour=?, quiet_end_minute=?,
        sleep_minutes=?, name=?
        WHERE id=? AND user_id=?""",
        (json.dumps(keywords, ensure_ascii=False),
         json.dumps(cities, ensure_ascii=False),
         json.dumps(excluded, ensure_ascii=False),
         1 if form.get("city_filter_enabled") else 0,
         1 if form.get("quiet_enabled") else 0,
         int(form.get("quiet_start_hour", 1)),
         int(form.get("quiet_start_minute", 0)),
         int(form.get("quiet_end_hour", 6)),
         int(form.get("quiet_end_minute", 0)),
         int(form.get("sleep_minutes", 15)),
         form.get("name","").strip() or "اشتراكي",
         sub_id, user["id"]))
    conn.commit()
    conn.close()
    bot.reload_sub(sub_id)
    return RedirectResponse("/dashboard", status_code=302)

# ============================
# ====== لوحة الأدمن =========
# ============================

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    total_users = conn.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0]
    total_subs  = conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0]
    active_subs = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active' AND expires_at > datetime('now')").fetchone()[0]
    total_sent  = conn.execute("SELECT SUM(sent_total) FROM subscriptions").fetchone()[0] or 0
    recent_logs = conn.execute("SELECT l.*, s.name as sub_name FROM logs l LEFT JOIN subscriptions s ON l.subscription_id=s.id ORDER BY l.created_at DESC LIMIT 30").fetchall()
    conn.close()
    bot_status = bot.status()
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user,
        "stats": {"users": total_users, "subs": total_subs, "active": active_subs, "sent": total_sent},
        "recent_logs": [dict(l) for l in recent_logs],
        "bot_status": bot_status
    })

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    users = conn.execute("""
        SELECT u.*, COUNT(s.id) as sub_count,
        SUM(CASE WHEN s.expires_at > datetime('now') AND s.status='active' THEN 1 ELSE 0 END) as active_subs
        FROM users u LEFT JOIN subscriptions s ON u.id=s.user_id
        WHERE u.role='user' GROUP BY u.id ORDER BY u.created_at DESC
    """).fetchall()
    conn.close()
    return templates.TemplateResponse("admin_users.html", {"request": request, "user": user, "users": [dict(u) for u in users]})

@app.get("/admin/users/{uid}", response_class=HTMLResponse)
async def admin_user_detail(request: Request, uid: int):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    target = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not target:
        conn.close()
        raise HTTPException(404)
    subs = conn.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY created_at DESC", (uid,)).fetchall()
    conn.close()
    now = datetime.now()
    subs_data = []
    for s in subs:
        d = dict(s)
        try:
            exp = datetime.fromisoformat(d["expires_at"])
            d["days_left"] = max(0, (exp - now).days)
            d["expired"] = now >= exp
        except Exception:
            d["days_left"] = 0
            d["expired"] = True
        d["bot_running"] = bot.threads.get(d["id"]) and bot.threads[d["id"]].is_alive()
        subs_data.append(d)
    return templates.TemplateResponse("admin_user_detail.html", {
        "request": request, "user": user,
        "target": dict(target), "subs": subs_data
    })

# ===== API الأدمن =====

@app.post("/admin/users/{uid}/toggle")
async def admin_toggle_user(request: Request, uid: int):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return JSONResponse({"error": "ممنوع"}, status_code=403)
    conn = get_db()
    current = conn.execute("SELECT is_active FROM users WHERE id=?", (uid,)).fetchone()
    new_val = 0 if current["is_active"] else 1
    conn.execute("UPDATE users SET is_active=? WHERE id=?", (new_val, uid))
    conn.commit()
    conn.close()
    return JSONResponse({"active": new_val})

@app.post("/admin/subscriptions/{sub_id}/extend")
async def admin_extend_sub(request: Request, sub_id: int, days: int = Form(7)):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return JSONResponse({"error": "ممنوع"}, status_code=403)
    conn = get_db()
    sub = conn.execute("SELECT * FROM subscriptions WHERE id=?", (sub_id,)).fetchone()
    if not sub:
        conn.close()
        return JSONResponse({"error": "غير موجود"}, status_code=404)
    try:
        current_exp = datetime.fromisoformat(sub["expires_at"])
        if current_exp < datetime.now():
            current_exp = datetime.now()
    except Exception:
        current_exp = datetime.now()
    new_exp = current_exp + timedelta(days=days)
    conn.execute("UPDATE subscriptions SET expires_at=?, status='active' WHERE id=?",
                 (new_exp.isoformat(), sub_id))
    conn.commit()
    conn.close()
    bot.start_sub(sub_id)
    return JSONResponse({"new_expires": new_exp.strftime("%Y-%m-%d")})

@app.post("/admin/subscriptions/add")
async def admin_add_sub(request: Request,
                        user_id: int = Form(...), name: str = Form(...),
                        whatsapp: str = Form(...), days: int = Form(30)):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/admin/users", status_code=302)
    expires = datetime.now() + timedelta(days=days)
    conn = get_db()
    conn.execute("""INSERT INTO subscriptions (user_id, name, whatsapp_number, expires_at, status)
                    VALUES (?,?,?,?,?)""", (user_id, name, whatsapp, expires.isoformat(), "active"))
    conn.commit()
    sub_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.close()
    bot.start_sub(sub_id)
    return RedirectResponse(f"/admin/users/{user_id}", status_code=302)

@app.post("/admin/subscriptions/{sub_id}/stop")
async def admin_stop_sub(request: Request, sub_id: int):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return JSONResponse({"error": "ممنوع"}, status_code=403)
    bot.stop_sub(sub_id)
    conn = get_db()
    conn.execute("UPDATE subscriptions SET status='paused' WHERE id=?", (sub_id,))
    conn.commit()
    conn.close()
    return JSONResponse({"status": "stopped"})

@app.post("/admin/subscriptions/{sub_id}/start")
async def admin_start_sub(request: Request, sub_id: int):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return JSONResponse({"error": "ممنوع"}, status_code=403)
    conn = get_db()
    conn.execute("UPDATE subscriptions SET status='active' WHERE id=?", (sub_id,))
    conn.commit()
    conn.close()
    bot.start_sub(sub_id)
    return JSONResponse({"status": "started"})

@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    rows = conn.execute("SELECT * FROM settings").fetchall()
    conn.close()
    settings = {r["key"]: r["value"] for r in rows}
    return templates.TemplateResponse("admin_settings.html", {"request": request, "user": user, "settings": settings})

@app.post("/admin/settings")
async def admin_settings_post(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    conn = get_db()
    for key in ("whatsapp_token", "trial_days", "site_name"):
        val = form.get(key, "")
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, val))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/settings?saved=1", status_code=302)

@app.get("/admin/logs", response_class=HTMLResponse)
async def admin_logs(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        return RedirectResponse("/login", status_code=302)
    conn = get_db()
    logs = conn.execute("""SELECT l.*, s.name as sub_name, u.name as user_name
        FROM logs l
        LEFT JOIN subscriptions s ON l.subscription_id=s.id
        LEFT JOIN users u ON s.user_id=u.id
        ORDER BY l.created_at DESC LIMIT 200""").fetchall()
    conn.close()
    return templates.TemplateResponse("admin_logs.html", {"request": request, "user": user,
                                                           "logs": [dict(l) for l in logs]})
