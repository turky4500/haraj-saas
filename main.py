"""
راصد حراج 2.0 - التطبيق الرئيسي (FastAPI)
"""
import json, logging, os, random
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from database import get_db, hash_password, init_db, DB_PATH
from bot.haraj_bot import BotManager, send_whatsapp

# إعداد السجلات (Logs)
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("haraj_app")

# تجهيز المجلدات
Path("static").mkdir(exist_ok=True)
Path("templates").mkdir(exist_ok=True)

app = FastAPI(title="راصد حراج")
app.add_middleware(SessionMiddleware, secret_key=os.environ.get("SECRET_KEY", "haraj-super-secret-2026"))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# ==========================================
# وظائف مساعدة لربط البوت بقاعدة البيانات
# ==========================================
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

def db_mark_sent(sub_id: int, ad_id: str, check_only=False) -> bool:
    conn = get_db()
    if check_only:
        r = conn.execute("SELECT 1 FROM sent_ads WHERE subscription_id=? AND ad_id=?", (sub_id, ad_id)).fetchone()
        conn.close()
        return r is not None
    try:
        conn.execute("INSERT OR IGNORE INTO sent_ads (subscription_id, ad_id) VALUES (?,?)", (sub_id, ad_id))
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

# تهيئة مدير البوت
bot = BotManager(db_get_all_active_subs, db_get_token, db_mark_sent, db_add_log, db_update_total, db_get_sub)

@app.on_event("startup")
async def startup():
    init_db()
    bot.start_all()
    logger.info("🚀 تم تشغيل راصد حراج 2.0 وبدء البوتات")

def get_current_user(request: Request):
    return request.session.get("user")

def get_setting(key: str, default: str = "") -> str:
    try:
        conn = get_db()
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        conn.close()
        return row["value"] if row else default
    except Exception:
        return default

# ==========================================
# الصفحات العامة ونظام تسجيل الدخول (Auth)
# ==========================================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = get_current_user(request)
    if user:
        return RedirectResponse("/admin" if user["role"] == "admin" else "/dashboard", status_code=302)
    
    conn = get_db()
    settings = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings").fetchall()}
    conn.close()
    return templates.TemplateResponse("landing.html", {"request": request, "settings": settings})

@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login")
async def login_post(request: Request, email: str = Form(...), password: str = Form(...)):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE email=? AND password_hash=? AND is_active=1",
                        (email.strip(), hash_password(password))).fetchone()
    conn.close()
    if not user:
        return templates.TemplateResponse("login.html", {"request": request, "error": "بيانات الدخول غير صحيحة"})
    
    request.session["user"] = {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"]}
    return RedirectResponse("/admin" if user["role"] == "admin" else "/dashboard", status_code=302)

@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    return templates.TemplateResponse("register.html", {"request": request})

@app.post("/register")
async def register_post(request: Request, name: str = Form(...), email: str = Form(...),
                        phone: str = Form(...), password: str = Form(...)):
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE email=?", (email.strip(),)).fetchone():
        conn.close()
        return templates.TemplateResponse("register.html", {"request": request, "error": "البريد الإلكتروني مسجل مسبقاً"})
    conn.close()

    # إنشاء رمز تحقق OTP
    otp_code = str(random.randint(1000, 9999))
    request.session["pending_user"] = {
        "name": name.strip(), "email": email.strip(), "phone": phone.strip(), "password": password, "otp": otp_code
    }
    
    token = get_setting("whatsapp_token")
    if token:
        # إرسال رسالة الواتساب بدون استخدام session (لأن دالة send_whatsapp تستخدم requests.post مباشرة)
        import requests
        req_session = requests.Session()
        send_whatsapp(req_session, token, phone.strip(), f"مرحباً {name} 👋\nرمز التحقق الخاص بك لراصد حراج هو:\n*{otp_code}*")
    
    return RedirectResponse("/verify", status_code=302)

@app.get("/verify", response_class=HTMLResponse)
async def verify_page(request: Request):
    if "pending_user" not in request.session: return RedirectResponse("/register", status_code=302)
    return templates.TemplateResponse("verify.html", {"request": request})

@app.post("/verify")
async def verify_post(request: Request, otp: str = Form(...)):
    pending = request.session.get("pending_user")
    if not pending or otp.strip() != pending["otp"]:
        return templates.TemplateResponse("verify.html", {"request": request, "error": "رمز التحقق غير صحيح"})

    conn = get_db()
    cursor = conn.execute("INSERT INTO users (name, email, phone, password_hash) VALUES (?,?,?,?)",
                          (pending["name"], pending["email"], pending["phone"], hash_password(pending["password"])))
    user_id = cursor.lastrowid
    
    trial_days = int(get_setting("trial_days", "2"))
    expires = datetime.now() + timedelta(days=trial_days)
    
    conn.execute("""INSERT INTO subscriptions (user_id, name, whatsapp_number, expires_at, status, keywords)
                    VALUES (?,?,?,?,?,?)""",
                 (user_id, f"اشتراك {pending['name']}", pending["phone"], expires.isoformat(), "active", "[]"))
    conn.commit()
    conn.close()
    
    request.session.pop("pending_user", None)
    
    # رسالة ترحيبية
    token = get_setting("whatsapp_token")
    if token:
        import requests
        req_session = requests.Session()
        welcome = f"مرحباً بك في *راصد حراج* 🔍\nتم تفعيل اشتراكك التجريبي بنجاح.\n\n🌐 https://haraj-saas.onrender.com"
        send_whatsapp(req_session, token, pending["phone"], welcome)

    request.session["user"] = {"id": user_id, "name": pending["name"], "email": pending["email"], "role": "user"}
    bot.start_sub(cursor.lastrowid) # تشغيل البوت فوراً
    return RedirectResponse("/dashboard", status_code=302)

@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=302)

# ==========================================
# لوحة تحكم المستخدم (Dashboard)
# ==========================================
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login", status_code=302)
    
    conn = get_db()
    subs = conn.execute("SELECT * FROM subscriptions WHERE user_id=? ORDER BY id DESC", (user["id"],)).fetchall()
    conn.close()
    
    now = datetime.now()
    subs_data = []
    for s in subs:
        d = dict(s)
        try:
            exp = datetime.fromisoformat(d["expires_at"])
            d["days_left"] = max(0, (exp - now).days)
            d["expired"] = now >= exp
        except:
            d["days_left"] = 0; d["expired"] = True
            
        d["bot_running"] = d["id"] in bot.threads and bot.threads[d["id"]].is_alive()
        d["keywords"] = json.loads(d.get("keywords", "[]"))
        subs_data.append(d)
        
    return templates.TemplateResponse("dashboard.html", {"request": request, "user": user, "subs": subs_data})

@app.get("/subscription/{sub_id}/edit", response_class=HTMLResponse)
async def edit_sub_page(request: Request, sub_id: int):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login", status_code=302)
    
    conn = get_db()
    sub = conn.execute("SELECT * FROM subscriptions WHERE id=? AND user_id=?", (sub_id, user["id"])).fetchone()
    conn.close()
    if not sub: raise HTTPException(404)
    
    d = dict(sub)
    d["keywords"] = json.loads(d.get("keywords", "[]"))
    d["cities"] = json.loads(d.get("cities", "[]"))
    d["excluded_words"] = json.loads(d.get("excluded_words", "[]"))
    return templates.TemplateResponse("edit_sub.html", {"request": request, "user": user, "sub": d})

@app.post("/subscription/{sub_id}/edit")
async def edit_sub_post(request: Request, sub_id: int):
    user = get_current_user(request)
    if not user: return RedirectResponse("/login", status_code=302)
    
    form = await request.form()
    keywords = [k.strip() for k in form.get("keywords","").split("\n") if k.strip()]
    cities = [c.strip() for c in form.get("cities","").split("\n") if c.strip()]
    excluded = [e.strip() for e in form.get("excluded_words","").split("\n") if e.strip()]
    
    conn = get_db()
    conn.execute("""UPDATE subscriptions SET
        name=?, keywords=?, cities=?, excluded_words=?,
        city_filter_enabled=?, exclude_enabled=?,
        quiet_enabled=?, quiet_start_hour=?, quiet_start_minute=?,
        quiet_end_hour=?, quiet_end_minute=?
        WHERE id=? AND user_id=?""",
        (form.get("name", "اشتراكي"),
         json.dumps(keywords, ensure_ascii=False),
         json.dumps(cities, ensure_ascii=False),
         json.dumps(excluded, ensure_ascii=False),
         1 if form.get("city_filter_enabled") else 0,
         1 if form.get("exclude_enabled") else 0,
         1 if form.get("quiet_enabled") else 0,
         int(form.get("quiet_start_hour", 1)), int(form.get("quiet_start_minute", 0)),
         int(form.get("quiet_end_hour", 6)), int(form.get("quiet_end_minute", 0)),
         sub_id, user["id"]))
    conn.commit()
    conn.close()
    
    bot.reload_sub(sub_id) # إعادة تحميل الإعدادات في البوت
    return RedirectResponse("/dashboard", status_code=302)

# ==========================================
# لوحة تحكم الإدارة (Admin)
# ==========================================
def check_admin(request: Request):
    user = get_current_user(request)
    if not user or user["role"] != "admin":
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return user

@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard(request: Request):
    user = check_admin(request)
    conn = get_db()
    stats = {
        "users": conn.execute("SELECT COUNT(*) FROM users WHERE role='user'").fetchone()[0],
        "subs": conn.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0],
        "active": conn.execute("SELECT COUNT(*) FROM subscriptions WHERE status='active' AND expires_at > datetime('now')").fetchone()[0],
        "sent": conn.execute("SELECT SUM(sent_total) FROM subscriptions").fetchone()[0] or 0
    }
    recent_logs = conn.execute("""
        SELECT l.*, s.name as sub_name FROM logs l 
        LEFT JOIN subscriptions s ON l.subscription_id=s.id 
        ORDER BY l.id DESC LIMIT 20
    """).fetchall()
    conn.close()
    
    # حالة الخيوط
    bot_status = {sid: th.is_alive() for sid, th in bot.threads.items()}
    return templates.TemplateResponse("admin.html", {
        "request": request, "user": user, "stats": stats,
        "recent_logs": [dict(l) for l in recent_logs], "bot_status": bot_status
    })

@app.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request):
    user = check_admin(request)
    conn = get_db()
    users = conn.execute("""
        SELECT u.*, COUNT(s.id) as sub_count 
        FROM users u LEFT JOIN subscriptions s ON u.id=s.user_id
        WHERE u.role='user' GROUP BY u.id ORDER BY u.id DESC
    """).fetchall()
    conn.close()
    return templates.TemplateResponse("admin_users.html", {"request": request, "user": user, "users": [dict(u) for u in users]})

@app.get("/admin/settings", response_class=HTMLResponse)
async def admin_settings_page(request: Request):
    user = check_admin(request)
    conn = get_db()
    settings = {r["key"]: r["value"] for r in conn.execute("SELECT * FROM settings").fetchall()}
    conn.close()
    return templates.TemplateResponse("admin_settings.html", {"request": request, "user": user, "settings": settings})

@app.post("/admin/settings")
async def admin_settings_post(request: Request):
    user = check_admin(request)
    form = await request.form()
    conn = get_db()
    for key, val in form.items():
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, str(val).strip()))
    conn.commit()
    conn.close()
    return RedirectResponse("/admin/settings?saved=1", status_code=302)

# أوامر إيقاف/تشغيل الاشتراكات للأدمن
@app.post("/admin/sub/{sub_id}/{action}")
async def admin_sub_action(request: Request, sub_id: int, action: str):
    check_admin(request)
    if action == "stop":
        bot.stop_sub(sub_id)
        conn = get_db()
        conn.execute("UPDATE subscriptions SET status='paused' WHERE id=?", (sub_id,))
        conn.commit(); conn.close()
    elif action == "start":
        conn = get_db()
        conn.execute("UPDATE subscriptions SET status='active' WHERE id=?", (sub_id,))
        conn.commit(); conn.close()
        bot.start_sub(sub_id)
    return JSONResponse({"status": "success"})
