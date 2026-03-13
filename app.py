# -*- coding: utf-8 -*-
"""
Haraj Monitor Web Version
محرك النظام الأساسي مبني على Flask مع تشغيل Threads في الخلفية.
"""

from flask import Flask, render_template, request, redirect, url_for, jsonify
import json, re, time, threading, datetime, os, random, shutil
from pathlib import Path
from urllib.parse import urljoin, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    from bs4 import BeautifulSoup
except ImportError:
    raise SystemExit("الرجاء تثبيت المكتبات من requirements.txt")

try:
    import certifi
    CERT_BUNDLE = certifi.where()
except Exception:
    CERT_BUNDLE = True

# ===== إعدادات التطبيق الأساسية =====
app = Flask(__name__)
app.secret_key = "haraj_super_secret_key"

APP_BASE_DIR = Path(__file__).resolve().parent
SUBS_BASE_DIR = APP_BASE_DIR / "subs"
SUBS_BASE_DIR.mkdir(exist_ok=True)
COUNTER_FILE = SUBS_BASE_DIR / "id_counter.json"

DEFAULT_TOKEN = "5a4b25b5228bab88c0df7aac67a458b45e63442f"
HARAJ_BASE = "https://haraj.com.sa"
WHATSAPP_API_URLS = ["https://whatsapp.tkwin.com.sa/api/v1/send"]
HARAJ_HEADERS = {"User-Agent": "Mozilla/5.0 (Haraj Monitor Web by Turki)", "Accept-Language": "ar-SA,ar;q=0.9"}

_FILE_LOCK = threading.RLock()

# متغيرات جلوبال للنظام
GLOBAL_STATE = {"is_running": False}
ACTIVE_SUBS = {} # {sub_id: {"thread": obj, "cfg": dict}}
SYSTEM_LOGS = []

# ===== دوال المساعدة =====
def log_system(msg):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{timestamp}] {msg}"
    print(log_line)
    SYSTEM_LOGS.append(log_line)
    if len(SYSTEM_LOGS) > 100:
        SYSTEM_LOGS.pop(0)

def load_json(path: Path, default):
    with _FILE_LOCK:
        if path.exists():
            try: return json.loads(path.read_text(encoding="utf-8"))
            except: return default
        return default

def save_json(path: Path, data):
    with _FILE_LOCK:
        temp_path = path.with_suffix('.tmp')
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(path)

def create_session():
    session = requests.Session()
    retries = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retries)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session

def extract_ads(html_bytes, base_url):
    soup = BeautifulSoup(html_bytes, "html.parser")
    ads = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if re.match(r"https?://(?:www\.)?haraj\.com(?:\.sa)?/\d+/.+", urljoin(base_url, href)):
            ads.append((a.get_text(strip=True) or "إعلان", urljoin(base_url, href)))
    return list(dict.fromkeys(ads)) # إزالة المكرر

def send_whatsapp_message(session, token, to_msisdn, text):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"to": to_msisdn, "message": text}
    try:
        r = session.post(WHATSAPP_API_URLS[0], json=payload, headers=headers, timeout=20, verify=False)
        return 200 <= r.status_code < 300
    except:
        return False

# ===== خيط المراقبة الأساسي لكل اشتراك =====
class SubscriptionMonitorThread(threading.Thread):
    def __init__(self, sub_id, cfg):
        super().__init__(daemon=True)
        self.sub_id = sub_id
        self.cfg = cfg
        self.stop_evt = threading.Event()
        self.session = create_session()
        self.sub_dir = SUBS_BASE_DIR / f"sub_{self.sub_id}"
        self.sub_dir.mkdir(exist_ok=True)
        self.SEEN_FILE = self.sub_dir / "seen_ids.json"
        self.seen_ids = set(load_json(self.SEEN_FILE, []))
        self.sent_total = 0

    def run(self):
        log_system(f"🚀 بدء تشغيل خيط الاشتراك #{self.sub_id}")
        keywords = self.cfg.get("keywords", [""])
        recipients = self.cfg.get("recipients", [])
        token = self.cfg.get("token", DEFAULT_TOKEN)
        
        while not self.stop_evt.is_set():
            if not GLOBAL_STATE["is_running"]:
                time.sleep(2)
                continue
            
            for kw in keywords:
                if self.stop_evt.is_set(): break
                url = f"{HARAJ_BASE}/search/{quote(kw, safe='')}/" if kw else f"{HARAJ_BASE}/"
                try:
                    html = self.session.get(url, headers=HARAJ_HEADERS, timeout=15, verify=False).content
                    for title, ad_url in extract_ads(html, HARAJ_BASE):
                        ad_id = re.search(r"/(\d+)(?:/|$)", ad_url).group(1)
                        if ad_id not in self.seen_ids:
                            msg = f"إعلان جديد ({kw}):\n{title}\n{ad_url}"
                            if send_whatsapp_message(self.session, token, recipients[0], msg):
                                self.seen_ids.add(ad_id)
                                self.sent_total += 1
                                save_json(self.SEEN_FILE, list(self.seen_ids))
                                log_system(f"✅ تم إرسال إعلان للاشتراك #{self.sub_id}")
                            time.sleep(random.uniform(5, 10)) # حماية واتساب
                except Exception as e:
                    log_system(f"⚠️ خطأ في سحب حراج للاشتراك #{self.sub_id}: {e}")
                
            time.sleep(120) # راحة دقيقتين بين كل فحص كامل

    def stop(self):
        self.stop_evt.set()


# ===== دوال تشغيل الـ Flask =====
@app.route('/')
def login():
    return render_template('login.html')

@app.route('/admin')
def admin_dashboard():
    return render_template('admin.html', global_running=GLOBAL_STATE["is_running"], logs=SYSTEM_LOGS[::-1])

@app.route('/user')
def user_dashboard():
    return render_template('user.html', subs=ACTIVE_SUBS)

@app.route('/api/toggle_global')
def toggle_global():
    GLOBAL_STATE["is_running"] = not GLOBAL_STATE["is_running"]
    state_str = "شغال" if GLOBAL_STATE["is_running"] else "متوقف"
    log_system(f"⚙️ تم تغيير حالة النظام العام إلى: {state_str}")
    return redirect(url_for('admin_dashboard'))

@app.route('/api/add_sub', methods=['POST'])
def add_sub():
    j = load_json(COUNTER_FILE, {"next_id": 1})
    sub_id = j["next_id"]
    
    cfg = {
        "name": request.form.get("name"),
        "keywords": [k.strip() for k in request.form.get("keywords", "").split(",") if k.strip()],
        "cities": [c.strip() for c in request.form.get("cities", "").split(",") if c.strip()],
        "recipients": [request.form.get("recipients", "")],
        "token": DEFAULT_TOKEN
    }
    
    save_json(SUBS_BASE_DIR / f"sub_{sub_id}_cfg.json", cfg)
    save_json(COUNTER_FILE, {"next_id": sub_id + 1})
    
    thread = SubscriptionMonitorThread(sub_id, cfg)
    ACTIVE_SUBS[sub_id] = {"cfg": cfg, "thread": thread}
    thread.start()
    
    log_system(f"تمت إضافة اشتراك جديد: {cfg['name']}")
    return redirect(url_for('user_dashboard'))

@app.route('/api/delete_sub/<int:sub_id>')
def delete_sub(sub_id):
    if sub_id in ACTIVE_SUBS:
        ACTIVE_SUBS[sub_id]["thread"].stop()
        del ACTIVE_SUBS[sub_id]
        shutil.rmtree(SUBS_BASE_DIR / f"sub_{sub_id}", ignore_errors=True)
        cfg_file = SUBS_BASE_DIR / f"sub_{sub_id}_cfg.json"
        if cfg_file.exists(): cfg_file.unlink()
        log_system(f"🗑 تم حذف الاشتراك رقم #{sub_id}")
    return redirect(url_for('user_dashboard'))

def load_existing_subs():
    for file in SUBS_BASE_DIR.glob("*_cfg.json"):
        sub_id = int(file.name.split("_")[1])
        cfg = load_json(file, {})
        thread = SubscriptionMonitorThread(sub_id, cfg)
        ACTIVE_SUBS[sub_id] = {"cfg": cfg, "thread": thread}
        thread.start()
    if ACTIVE_SUBS: log_system(f"تم تحميل {len(ACTIVE_SUBS)} اشتراكات سابقة.")

if __name__ == '__main__':
    load_existing_subs()
    # يتم تشغيله على بورت 5000، جاهز للرفع على الريندر
    app.run(host='0.0.0.0', port=5000, debug=False)
