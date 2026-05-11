# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, redirect, url_for, flash, session, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
import json, re, time, threading, datetime, random, os, uuid
from pathlib import Path
from urllib.parse import urljoin, quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import logging
import urllib3
from urllib3.exceptions import InsecureRequestWarning
urllib3.disable_warnings(InsecureRequestWarning)
import cloudscraper

app = Flask(__name__)
app.secret_key = "haraj_super_secret_key_v18_final_launch"

# دالة تنسيق الوقت بتوقيت السعودية
def format_time_ksa(dt, format_type='full'):
    if dt is None:
        return ''
    ksa_time = dt + datetime.timedelta(hours=3)
    if format_type == 'full':
        return ksa_time.strftime('%Y-%m-%d %I:%M %p').replace('AM', 'صباحاً').replace('PM', 'مساءً')
    elif format_type == 'short' or format_type == 'time':
        return ksa_time.strftime('%I:%M %p').replace('AM', 'صباحاً').replace('PM', 'مساءً')
    elif format_type == 'date':
        return ksa_time.strftime('%Y-%m-%d')
    else:
        return str(ksa_time)

app.jinja_env.globals.update(format_time_ksa=format_time_ksa, datetime=datetime.datetime)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app.jinja_env.globals.update(now=datetime.datetime.now)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# مجلد رفع صور التحويل (نستخدم /tmp على Render لأنه مضمون الكتابة)
if os.environ.get('RENDER') or not os.access(BASE_DIR, os.W_OK):
    UPLOAD_FOLDER = '/tmp/uploads/renewal_proofs'
else:
    UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads', 'renewal_proofs')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 5 * 1024 * 1024  # 5 ميجابايت

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'pdf'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ================= ربط قاعدة البيانات السحابية =================
db_url = os.environ.get("DATABASE_URL")
if db_url:
    if db_url.startswith("postgres://"):
        db_url = db_url.replace("postgres://", "postgresql://", 1)
    app.config['SQLALCHEMY_DATABASE_URI'] = db_url
else:
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'haraj.db')

app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {
    "pool_pre_ping": True,
    "pool_recycle": 280,
}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

APP_BASE_DIR = Path(__file__).resolve().parent
SUBS_BASE_DIR = APP_BASE_DIR / "subs"
SUBS_BASE_DIR.mkdir(exist_ok=True)
REMINDERS_FILE = SUBS_BASE_DIR / "reminders_sent.json"
STATS_FILE = SUBS_BASE_DIR / "daily_stats.json"
WHATSAPP_LOG_FILE = SUBS_BASE_DIR / "whatsapp_logs.json"

HARAJ_BASE = "https://haraj.com.sa"
HARAJ_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "ar-SA"}
ACTIVE_THREADS = {} 

seen_file_lock = threading.Lock()
audit_log_lock = threading.Lock()

def get_ksa_time():
    return datetime.datetime.utcnow() + datetime.timedelta(hours=3)

stats_lock = threading.Lock()

def get_daily_stats():
    now_date = get_ksa_time().strftime('%Y-%m-%d')
    default_stats = {"date": now_date, "visitors": 0, "bot_visits": 0, "registrations": 0, "messages_sent": 0}
    if STATS_FILE.exists():
        try:
            with open(STATS_FILE, 'r') as f:
                stats = json.load(f)
            if stats.get("date") != now_date: return default_stats
            return stats
        except: return default_stats
    return default_stats

def update_daily_stat(key, count=1):
    with stats_lock:
        stats = get_daily_stats()
        stats[key] = stats.get(key, 0) + count
        try:
            with open(STATS_FILE, 'w') as f: json.dump(stats, f)
        except: pass

whatsapp_logs_lock = threading.Lock()

def log_whatsapp_attempt(to_number, success, response_data, message_text=""):
    with whatsapp_logs_lock:
        logs = []
        if WHATSAPP_LOG_FILE.exists():
            try:
                with open(WHATSAPP_LOG_FILE, 'r') as f:
                    logs = json.load(f)
            except:
                logs = []
        logs.append({
            "timestamp": datetime.datetime.now().isoformat(),
            "to": to_number,
            "success": success,
            "response": response_data,
            "message_preview": message_text[:50] + "..." if len(message_text) > 50 else message_text
        })
        if len(logs) > 1000:
            logs = logs[-1000:]
        try:
            with open(WHATSAPP_LOG_FILE, 'w') as f:
                json.dump(logs, f, indent=2, ensure_ascii=False)
        except:
            pass

def cleanup_old_logs():
    while True:
        time.sleep(3600)
        with app.app_context():
            try:
                total_logs = AdLog.query.count()
                if total_logs > 2000:
                    excess = total_logs - 2000
                    old_logs = AdLog.query.order_by(AdLog.timestamp.asc()).limit(excess).all()
                    for l in old_logs:
                        db.session.delete(l)
                    db.session.commit()
                cutoff_date = datetime.datetime.utcnow() - datetime.timedelta(days=90)
                AuditLog.query.filter(AuditLog.timestamp < cutoff_date).delete()
                db.session.commit()
            except:
                pass

def daily_background_tasks():
    while True:
        time.sleep(1800)
        with app.app_context():
            try:
                now = get_ksa_time()
                settings = SystemSettings.query.first()
                notify = AdminNotifySettings.query.first()
                if notify and notify.admin_phone:
                    if now.hour >= 23 and notify.last_report_date < now.date():
                        ds = get_daily_stats()
                        msg = f"📊 تقرير نهاية اليوم لمنصة (راصد حراج):\n\n👥 زوار بشريين: {ds['visitors']}\n🤖 زيارات الروبوت: {ds['bot_visits']}\n🆕 تسجيلات جديدة: {ds['registrations']}\n💬 رسائل أُرسلت: {ds['messages_sent']}\n\nيعطيك العافية 🚀"
                        send_user_message(notify.admin_phone, msg, is_admin=True)
                        notify.last_report_date = now.date()
                        db.session.commit()
                if now.hour >= 16:
                    if REMINDERS_FILE.exists():
                        with open(REMINDERS_FILE, 'r') as f: sent_reminders = json.load(f)
                    else:
                        sent_reminders = {}
                    users = User.query.filter(User.account_expiration.isnot(None)).all()
                    for u in users:
                        exp_date_str = u.account_expiration.strftime('%Y-%m-%d')
                        if u.account_expiration.date() == now.date() + datetime.timedelta(days=1):
                            uid_str = str(u.id)
                            if sent_reminders.get(uid_str) != exp_date_str:
                                msg = f"🌸 مرحباً {u.username},\n\nنذكرك بحب أن اشتراكك في **راصد حراج** سينتهي غداً {u.account_expiration.strftime('%Y-%m-%d')}. 🗓️\n\nنتمنى أن تكون استمتعت بخدمتنا، ولضمان استمرار رصد صيداتك الموفقة بدون انقطاع، يمكنك التواصل معنا لتجديد الاشتراك. نحن هنا لخدمتك دائماً! 💙\n\nشكراً لثقتك بنا."
                                if send_user_message(u.phone, msg, user_id=u.id):
                                    sent_reminders[uid_str] = exp_date_str
                                    with open(REMINDERS_FILE, 'w') as f: json.dump(sent_reminders, f)
            except Exception as e:
                logger.error(f"خطأ في المهام الخلفية: {str(e)}")

threading.Thread(target=cleanup_old_logs, daemon=True).start()
threading.Thread(target=daily_background_tasks, daemon=True).start()

def monitor_threads_health():
    while True:
        time.sleep(600)
        with app.app_context():
            try:
                active_subs = Subscription.query.filter_by(status='active').all()
                for sub in active_subs:
                    if not sub.owner.is_active_account:
                        continue
                    if sub.owner.account_expiration and sub.owner.account_expiration <= datetime.datetime.now():
                        continue
                    thread = ACTIVE_THREADS.get(sub.id)
                    if not thread or not thread.is_alive():
                        logger.warning(f"الخيط للاشتراك {sub.id} غير نشط، جاري إعادة تشغيله...")
                        if sub.id in ACTIVE_THREADS:
                            try: ACTIVE_THREADS[sub.id].stop()
                            except: pass
                            del ACTIVE_THREADS[sub.id]
                        start_thread_for_sub(sub)
                        notify_msg = f"🔄 تم إعادة تشغيل رادار المستخدم {sub.owner.username} (الاشتراك {sub.id}) تلقائياً."
                        send_user_message(sub.owner.phone, notify_msg, user_id=sub.owner.id)
            except Exception as e:
                logger.error(f"خطأ في مراقبة الخيوط: {str(e)}")

threading.Thread(target=monitor_threads_health, daemon=True).start()

def log_audit_async(user_id, action, details=None, ip_address=None):
    def _log():
        with app.app_context():
            try:
                details_json = json.dumps(details, ensure_ascii=False, default=str) if details else None
                log_entry = AuditLog(user_id=user_id, action=action, details=details_json, ip_address=ip_address)
                db.session.add(log_entry)
                db.session.commit()
            except Exception as e:
                logger.error(f"فشل تسجيل التدقيق: {e}")
    threading.Thread(target=_log, daemon=True).start()

# ================= النماذج =================
class SystemSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    whatsapp_token = db.Column(db.String(255), default="7a203d6ba6f4325ed3261ea87f6b2e751250ad97")
    trial_days = db.Column(db.Integer, default=2)
    bank_account_number = db.Column(db.String(50), default="")
    bank_account_name = db.Column(db.String(100), default="")
    bank_qr_text = db.Column(db.Text, default="")
    subscription_week_price = db.Column(db.Integer, default=5)
    messaging_method = db.Column(db.String(10), default='whatsapp')
    telegram_bot_token = db.Column(db.String(255), default='')
    telegram_chat_id = db.Column(db.String(50), default='')

class AdminNotifySettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_phone = db.Column(db.String(20), default="")
    daily_visitors = db.Column(db.Integer, default=0)
    last_report_date = db.Column(db.Date, default=datetime.date.today)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='user')
    is_active_account = db.Column(db.Boolean, default=True)
    account_expiration = db.Column(db.DateTime, nullable=True)
    telegram_chat_id = db.Column(db.String(50), nullable=True)
    subscription = db.relationship('Subscription', backref='owner', uselist=False, lazy=True)
    logs = db.relationship('AdLog', backref='owner', lazy=True)
    renewal_requests = db.relationship('RenewalRequest', backref='owner', lazy=True)

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    keywords = db.Column(db.String(500), nullable=False)
    recipients = db.Column(db.String(100), nullable=False) 
    status = db.Column(db.String(20), default='active') 
    sent_count = db.Column(db.Integer, default=0)
    cities = db.Column(db.String(500), default="")
    city_filter_enabled = db.Column(db.Boolean, default=False)
    excluded_words = db.Column(db.String(500), default="")
    exclude_enabled = db.Column(db.Boolean, default=False)
    quiet_enabled = db.Column(db.Boolean, default=False)
    quiet_start_hour = db.Column(db.Integer, default=1)
    quiet_start_minute = db.Column(db.Integer, default=0)
    quiet_end_hour = db.Column(db.Integer, default=6)
    quiet_end_minute = db.Column(db.Integer, default=0)
    sleep_minutes = db.Column(db.Integer, default=15) 
    end_ts = db.Column(db.String(50))

class AdLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    keyword_matched = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)

class RenewalRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    weeks = db.Column(db.Integer, nullable=False)
    amount = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), default='pending')
    proof_filename = db.Column(db.String(255), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    processed_at = db.Column(db.DateTime, nullable=True)

class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    action = db.Column(db.String(50), nullable=False)
    details = db.Column(db.Text, nullable=True)
    ip_address = db.Column(db.String(45), nullable=True)
    timestamp = db.Column(db.DateTime, default=datetime.datetime.utcnow)
    user = db.relationship('User', backref='audit_logs', lazy=True)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ================= دوال مساعدة =================
_AR_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_AR_NORM_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ؤ": "و", "ئ": "ي", "ى": "ي", "ة": "ه"})

def normalize_text(s):
    s = (s or "").lower()
    s = _AR_DIACRITICS_RE.sub("", s)
    s = s.translate(_AR_NORM_MAP)
    s = re.sub(r"[^\u0600-\u06FFa-z0-9\s]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def matches_keyword_precise(text, kw, excluded_list, exclude_enabled):
    nt = normalize_text(text)
    if exclude_enabled and excluded_list:
        for neg in excluded_list:
            norm_neg = normalize_text(neg)
            if norm_neg and re.search(r'(^|\s)' + re.escape(norm_neg) + r'($|\s)', nt): 
                return False
    norm_kw = normalize_text(kw)
    if not norm_kw: return True
    return bool(re.search(r'(^|\s)' + re.escape(norm_kw) + r'($|\s)', nt))

def is_target_city(full_text, cities_list, city_filter_enabled):
    if not city_filter_enabled or not cities_list: return True
    if not full_text: return False
    ft_lower = full_text.lower()
    for tc in cities_list:
        if tc.strip().lower() in ft_lower: return True
    return False

def is_quiet_now(enabled, sh, sm, eh, em):
    if not enabled: return False
    now = get_ksa_time()
    now_min = now.hour * 60 + now.minute
    start_min = sh * 60 + sm
    end_min = eh * 60 + em
    if start_min == end_min: return True
    if start_min < end_min: return start_min <= now_min < end_min
    return (now_min >= start_min) or (now_min < end_min)

def create_session():
    session = cloudscraper.create_scraper()
    session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"})
    retries = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504))
    session.mount("https://", HTTPAdapter(max_retries=retries))
    return session

def extract_ads(html_bytes, base_url):
    soup = BeautifulSoup(html_bytes, "html.parser")
    ads = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if re.match(r"https?://(?:www\.)?haraj\.com(?:\.sa)?/\d+/.+", urljoin(base_url, href)):
            ads.append((a.get_text(strip=True) or "إعلان", urljoin(base_url, href)))
    return list(dict.fromkeys(ads))

# ================= دوال الإرسال =================
def send_whatsapp(req_session, token, to_msisdn, text, max_retries=3):
    url = "https://whatsapp.tkwin.com.sa/api/v1/send"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    logger.info(f"📤 إرسال واتساب إلى {to_msisdn} - {text[:50]}...")
    response_data = {"status_code": None, "text": "", "json": None}
    for attempt in range(1, max_retries + 1):
        try:
            resp = req_session.post(url, json={"to": to_msisdn, "message": text}, headers=headers, timeout=20, verify=False)
            response_data["status_code"] = resp.status_code
            response_data["text"] = resp.text[:200]
            try:
                result = resp.json()
                response_data["json"] = result
            except:
                result = None
            success = False
            if resp.status_code == 200:
                if isinstance(result, dict):
                    if result.get("success") or result.get("status") == "sent" or result.get("message_id"):
                        success = True
            log_whatsapp_attempt(to_msisdn, success, response_data, text)
            if success:
                update_daily_stat('messages_sent')
                logger.info(f"✅ تم الإرسال إلى {to_msisdn}")
                return True
            else:
                logger.error(f"❌ فشل الإرسال إلى {to_msisdn} - Status: {resp.status_code}")
                if attempt < max_retries: time.sleep(2 ** attempt)
                else: return False
        except Exception as e:
            logger.error(f"❌ استثناء: {e}")
            if attempt < max_retries: time.sleep(2 ** attempt)
            else: return False
    return False

def send_telegram(bot_token, chat_id, text):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=15)
        if r.status_code == 200 and r.json().get("ok"):
            logger.info(f"✅ تيليجرام إلى {chat_id}")
            return True
        else:
            logger.error(f"❌ فشل تيليجرام: {r.text[:100]}")
            return False
    except Exception as e:
        logger.error(f"❌ استثناء تيليجرام: {e}")
        return False

def _send_user_message(destination, message, user_id=None, is_admin=False):
    """
    المنطق الداخلي لإرسال رسالة.
    """
    settings = SystemSettings.query.first()
    if not settings:
        return False
    method = settings.messaging_method

    if method == 'whatsapp':
        token = settings.whatsapp_token
        return send_whatsapp(create_session(), token, destination, message)
    elif method == 'telegram':
        bot_token = settings.telegram_bot_token
        if not bot_token:
            logger.error("❌ بوت تيليجرام غير معرف")
            return False
        if is_admin:
            target_chat = settings.telegram_chat_id
        elif user_id:
            user = User.query.get(user_id)
            target_chat = user.telegram_chat_id if user and user.telegram_chat_id else settings.telegram_chat_id
        else:
            target_chat = settings.telegram_chat_id
        if not target_chat:
            logger.error("❌ لا يوجد chat_id للإرسال")
            return False
        return send_telegram(bot_token, target_chat, message)
    return False

def send_user_message(destination, message, user_id=None, is_admin=False):
    """
    واجهة إرسال آمنة يمكن استدعاؤها من أي مكان (مع أو بدون سياق Flask).
    """
    try:
        from flask import current_app
        if current_app:
            # نحن داخل سياق التطبيق بالفعل
            return _send_user_message(destination, message, user_id, is_admin)
    except (RuntimeError, Exception):
        pass
    # لا يوجد سياق تطبيق، ننشئ واحدًا
    with app.app_context():
        return _send_user_message(destination, message, user_id, is_admin)

# --- نهاية الجزء الأول ---
# يتبع في الرد التالي مع بقية المسارات وخيوط المراقبة والترحيل.
# ================= مسار عرض صورة الإثبات =================
@app.route('/admin/view_proof/<int:request_id>')
@login_required
def view_proof(request_id):
    if current_user.role != 'admin':
        return "غير مصرح", 403
    req = RenewalRequest.query.get_or_404(request_id)
    if not req.proof_filename:
        flash('لا توجد صورة مرفقة.', 'warning')
        return redirect(url_for('admin_renewal_requests'))
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], req.proof_filename)
    if not os.path.exists(filepath):
        flash('الملف غير موجود.', 'danger')
        return redirect(url_for('admin_renewal_requests'))
    return send_from_directory(app.config['UPLOAD_FOLDER'], req.proof_filename)

# ================= سجل الواتساب =================
@app.route('/admin/whatsapp_logs')
@login_required
def admin_whatsapp_logs():
    if current_user.role != 'admin':
        return "غير مصرح", 403
    logs = []
    try:
        if WHATSAPP_LOG_FILE.exists():
            with open(WHATSAPP_LOG_FILE, 'r', encoding='utf-8') as f:
                logs = json.loads(f.read().strip() or '[]')
    except:
        logs = []
    logs.reverse()
    return render_template('whatsapp_logs.html', logs=logs)

@app.route('/admin/clear_whatsapp_logs')
@login_required
def admin_clear_whatsapp_logs():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    try:
        with open(WHATSAPP_LOG_FILE, 'w') as f:
            json.dump([], f)
        flash('✅ تم حذف سجلات الواتساب.', 'success')
    except Exception as e:
        flash(f'❌ خطأ: {e}', 'danger')
    return redirect(url_for('admin_whatsapp_logs'))

@app.route('/admin/clear_archive')
@login_required
def admin_clear_archive():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    try:
        deleted = AdLog.query.delete()
        db.session.commit()
        flash(f'✅ تم حذف {deleted} سجل من الأرشيف.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ خطأ: {e}', 'danger')
    return redirect(url_for('admin_ads_log'))

# ================= خيط المراقبة =================
class MonitorThread(threading.Thread):
    def __init__(self, sub_config):
        super().__init__(daemon=True)
        self.cfg = sub_config
        self.stop_evt = threading.Event()
        self.req_session = create_session()
        self.seen_file = SUBS_BASE_DIR / f"seen_{self.cfg['id']}.json"
        self._load_seen()
        self.queue_file = SUBS_BASE_DIR / f"queue_{self.cfg['id']}.json"
        self.queued_ads = json.load(open(self.queue_file, 'r')) if self.queue_file.exists() else []

    def _load_seen(self):
        with seen_file_lock:
            if self.seen_file.exists():
                try:
                    self.seen_ids = set(json.load(open(self.seen_file)))
                except:
                    self.seen_ids = set()
            else:
                self.seen_ids = set()

    def _save_seen(self):
        with seen_file_lock:
            with open(self.seen_file, 'w') as f:
                json.dump(list(self.seen_ids), f)

    def run(self):
        while not self.stop_evt.is_set():
            try:
                with app.app_context():
                    sub = Subscription.query.get(self.cfg['id'])
                    if not sub: break
                    user = sub.owner
                    if not user or not user.is_active_account or (user.account_expiration and user.account_expiration < datetime.datetime.now()):
                        if sub.status == 'active':
                            sub.status = 'paused'
                            db.session.commit()
                            send_user_message(self.cfg['recipients'], "🌸 انتهى اشتراكك في راصد حراج.", user_id=user.id if user else None)
                        break
                    # تحديث الإعدادات من قاعدة البيانات
                    self.cfg['keywords'] = [k.strip() for k in sub.keywords.split(',') if k.strip()]
                    self.cfg['cities'] = [c.strip() for c in sub.cities.split(',') if c.strip()]
                    self.cfg['city_filter_enabled'] = sub.city_filter_enabled
                    self.cfg['excluded_words'] = [e.strip() for e in sub.excluded_words.split(',') if e.strip()]
                    self.cfg['exclude_enabled'] = sub.exclude_enabled
                    self.cfg['quiet_enabled'] = sub.quiet_enabled
                    self.cfg['q_sh'] = sub.quiet_start_hour
                    self.cfg['q_sm'] = sub.quiet_start_minute
                    self.cfg['q_eh'] = sub.quiet_end_hour
                    self.cfg['q_em'] = sub.quiet_end_minute
                    
                    currently_quiet = is_quiet_now(self.cfg['quiet_enabled'], self.cfg['q_sh'], self.cfg['q_sm'], self.cfg['q_eh'], self.cfg['q_em'])

                    if not currently_quiet and self.queued_ads:
                        send_user_message(self.cfg['recipients'], "🌅 انتهت فترة الهدوء، إليك الإعلانات المخزنة:", user_id=user.id)
                        time.sleep(2)
                        for ad in self.queued_ads:
                            if self.stop_evt.is_set(): break
                            msg = f"إعلان ({ad['kw']}):\n{ad['title']}\n{ad['url']}\n\n⚙️ https://haraj-saas.onrender.com"
                            send_user_message(self.cfg['recipients'], msg, user_id=user.id)
                            time.sleep(random.uniform(4, 8))
                        self.queued_ads = []
                        if self.queue_file.exists(): json.dump([], open(self.queue_file, 'w'))

                    for kw in self.cfg['keywords']:
                        if self.stop_evt.is_set(): break
                        for page in range(1, 4):
                            if self.stop_evt.is_set(): break
                            url = f"{HARAJ_BASE}/search/{quote(kw)}/page/{page}" if page > 1 else f"{HARAJ_BASE}/search/{quote(kw)}"
                            try:
                                html = self.req_session.get(url, headers=HARAJ_HEADERS, timeout=15, verify=False).content
                                for title, ad_url in extract_ads(html, HARAJ_BASE):
                                    ad_id = re.search(r"/(\d+)(?:/|$)", ad_url).group(1)
                                    if ad_id not in self.seen_ids:
                                        ad_html = self.req_session.get(ad_url, headers=HARAJ_HEADERS, timeout=15, verify=False).content
                                        soup = BeautifulSoup(ad_html, 'html.parser')
                                        full_text = soup.get_text(" ", strip=True)
                                        if is_target_city(full_text, self.cfg['cities'], self.cfg['city_filter_enabled']) and \
                                           matches_keyword_precise(full_text, kw, self.cfg['excluded_words'], self.cfg['exclude_enabled']):
                                            self.seen_ids.add(ad_id)
                                            self._save_seen()
                                            with app.app_context():
                                                if AdLog.query.filter_by(user_id=self.cfg['user_id'], url=ad_url).first():
                                                    continue
                                            if currently_quiet:
                                                self.queued_ads.append({'kw': kw, 'title': title, 'url': ad_url})
                                                json.dump(self.queued_ads, open(self.queue_file, 'w'))
                                            else:
                                                time.sleep(random.uniform(30, 60))
                                                msg = f"إعلان جديد ({kw}):\n{title}\n{ad_url}\n\n⚙️ https://haraj-saas.onrender.com"
                                                send_user_message(self.cfg['recipients'], msg, user_id=user.id)
                                            with app.app_context():
                                                sub = Subscription.query.get(self.cfg['id'])
                                                if sub:
                                                    sub.sent_count += 1
                                                    db.session.add(AdLog(user_id=self.cfg['user_id'], title=title, url=ad_url, keyword_matched=kw))
                                                    db.session.commit()
                            except Exception as e:
                                logger.error(f"خطأ رصد ({kw}، صفحة {page}): {e}")
                            time.sleep(random.uniform(3, 7))
                    time.sleep(self.cfg['sleep_minutes'] * 60)
            except Exception as e:
                logger.error(f"خطأ غير متوقع: {e}")
                time.sleep(60)
                continue

    def stop(self):
        self.stop_evt.set()

def start_thread_for_sub(sub):
    cfg = {
        'id': sub.id, 'user_id': sub.user_id,
        'keywords': [k.strip() for k in sub.keywords.split(',') if k.strip()],
        'recipients': sub.recipients.split(',')[0].strip(),
        'cities': [c.strip() for c in sub.cities.split(',') if c.strip()],
        'city_filter_enabled': sub.city_filter_enabled,
        'excluded_words': [e.strip() for e in sub.excluded_words.split(',') if e.strip()],
        'exclude_enabled': sub.exclude_enabled,
        'quiet_enabled': sub.quiet_enabled,
        'q_sh': sub.quiet_start_hour, 'q_sm': sub.quiet_start_minute,
        'q_eh': sub.quiet_end_hour, 'q_em': sub.quiet_end_minute,
        'sleep_minutes': sub.sleep_minutes, 'end_ts': sub.end_ts
    }
    t = MonitorThread(cfg)
    ACTIVE_THREADS[sub.id] = t
    t.start()
    logger.info(f"✅ بدء خيط الاشتراك {sub.id}")

# ================= المسارات =================
@app.route('/')
def index():
    if request.headers.get('X-Keep-Alive-Bot'):
        update_daily_stat('bot_visits')
        return "Bot OK", 200
    if 'visited_today' not in session:
        session['visited_today'] = True
        update_daily_stat('visitors')
        notify = AdminNotifySettings.query.first()
        if notify:
            notify.daily_visitors += 1
            db.session.commit()
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard') if current_user.role == 'admin' else url_for('user_dashboard'))
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password, request.form.get('password')):
            if not user.is_active_account:
                flash('الحساب موقوف.', 'danger')
                return redirect(url_for('login'))
            login_user(user)
            log_audit_async(user.id, 'login', {'مستخدم': user.username}, request.remote_addr)
            return redirect(url_for('admin_dashboard') if user.role == 'admin' else url_for('user_dashboard'))
        flash('بيانات غير صحيحة.', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        phone = request.form.get('phone')
        password = request.form.get('password')
        if User.query.filter((User.username == username) | (User.phone == phone)).first():
            flash('المستخدم مسجل.', 'danger')
            return redirect(url_for('register'))
        otp = str(random.randint(1000, 9999))
        session['temp_user'] = {'username': username, 'phone': phone, 'password': generate_password_hash(password, method='pbkdf2:sha256')}
        session['otp'] = otp
        send_user_message(phone, f"كود التفعيل: *{otp}*")
        return redirect(url_for('verify'))
    return render_template('register.html')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if request.method == 'POST':
        if request.form.get('otp') == session.get('otp'):
            temp = session['temp_user']
            new_user = User(username=temp['username'], phone=temp['phone'], password=temp['password'])
            settings = SystemSettings.query.first()
            trial = settings.trial_days if settings else 2
            if User.query.count() == 0:
                new_user.role = 'admin'
            else:
                new_user.account_expiration = datetime.datetime.now() + datetime.timedelta(days=trial)
            db.session.add(new_user)
            db.session.commit()
            update_daily_stat('registrations')
            notify = AdminNotifySettings.query.first()
            if notify and notify.admin_phone and new_user.role != 'admin':
                send_user_message(notify.admin_phone, f"🔔 مستخدم جديد: {new_user.username}", is_admin=True)
            login_user(new_user)
            session.pop('temp_user', None); session.pop('otp', None)
            log_audit_async(new_user.id, 'register', {'مستخدم': new_user.username}, request.remote_addr)
            flash('تم التسجيل.', 'success')
            return redirect(url_for('user_dashboard'))
        flash('كود خطأ.', 'danger')
    return render_template('verify.html')

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        user = User.query.filter_by(phone=request.form.get('phone')).first()
        if user:
            otp = str(random.randint(1000, 9999))
            session['reset_phone'] = user.phone
            session['reset_otp'] = otp
            send_user_message(user.phone, f"كود استعادة كلمة المرور: *{otp}*", user_id=user.id)
            return redirect(url_for('reset_password'))
        flash('الرقم غير مسجل.', 'danger')
    return render_template('forgot_password.html')

@app.route('/forgot_username', methods=['GET', 'POST'])
def forgot_username():
    if request.method == 'POST':
        user = User.query.filter_by(phone=request.form.get('phone')).first()
        if user:
            send_user_message(user.phone, f"اسم المستخدم: *{user.username}*", user_id=user.id)
            flash('تم إرسال اسم المستخدم.', 'success')
            return redirect(url_for('login'))
        flash('الرقم غير مسجل.', 'danger')
    return render_template('forgot_username.html')

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    if 'reset_phone' not in session: return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        if request.form.get('otp') == session.get('reset_otp'):
            user = User.query.filter_by(phone=session['reset_phone']).first()
            user.password = generate_password_hash(request.form.get('new_password'), method='pbkdf2:sha256')
            db.session.commit()
            session.pop('reset_phone', None); session.pop('reset_otp', None)
            log_audit_async(user.id, 'password_reset', {}, request.remote_addr)
            flash('تم تغيير كلمة المرور.', 'success')
            return redirect(url_for('login'))
        flash('كود خطأ.', 'danger')
    return render_template('reset_password.html')

@app.route('/logout')
@login_required
def logout():
    log_audit_async(current_user.id, 'logout', {}, request.remote_addr)
    logout_user()
    return redirect(url_for('index'))

@app.route('/user_profile', methods=['GET', 'POST'])
@login_required
def user_profile():
    if request.method == 'POST':
        if check_password_hash(current_user.password, request.form.get('old_password')):
            current_user.password = generate_password_hash(request.form.get('new_password'), method='pbkdf2:sha256')
            current_user.telegram_chat_id = request.form.get('telegram_chat_id', '').strip() or None
            db.session.commit()
            log_audit_async(current_user.id, 'change_password', {}, request.remote_addr)
            flash('تم تحديث البيانات.', 'success')
            return redirect(url_for('user_profile'))
        flash('كلمة المرور الحالية خطأ.', 'danger')
    return render_template('user_profile.html')

@app.route('/user_dashboard', methods=['GET', 'POST'])
@login_required
def user_dashboard():
    if current_user.role == 'admin' and 'admin_impersonating' not in session:
        return redirect(url_for('admin_dashboard'))
    sub = Subscription.query.filter_by(user_id=current_user.id).first()
    logs = AdLog.query.filter_by(user_id=current_user.id).order_by(AdLog.timestamp.desc()).limit(100).all()
    is_expired = current_user.account_expiration and datetime.datetime.now() > current_user.account_expiration
    if request.method == 'POST':
        name = request.form.get('name')
        keywords = request.form.get('keywords')
        cities = request.form.get('cities', '')
        city_en = 'city_filter_enabled' in request.form
        excl_words = request.form.get('excluded_words', '')
        excl_en = 'exclude_enabled' in request.form
        quiet_en = 'quiet_enabled' in request.form
        q_sh = int(request.form.get('q_sh', 1))
        q_eh = int(request.form.get('q_eh', 6))
        end_time = current_user.account_expiration.isoformat() if current_user.account_expiration else ""
        
        old_kw = sub.keywords if sub else ''
        audit_details = {'الكلمات قبل': old_kw, 'الكلمات بعد': keywords, 'مدن': cities or 'كل المدن', 'محظورة': excl_words or 'لا يوجد', 'هدوء': 'مفعل' if quiet_en else 'غير مفعل'}
        
        if sub:
            new_status = 'paused' if is_expired else sub.status
            if sub.id in ACTIVE_THREADS:
                ACTIVE_THREADS[sub.id].stop(); del ACTIVE_THREADS[sub.id]
            sub.name = name; sub.keywords = keywords; sub.cities = cities; sub.city_filter_enabled = city_en
            sub.excluded_words = excl_words; sub.exclude_enabled = excl_en; sub.quiet_enabled = quiet_en
            sub.quiet_start_hour = q_sh; sub.quiet_end_hour = q_eh; sub.end_ts = end_time; sub.status = new_status
            db.session.commit()
            if not is_expired and new_status == 'active':
                start_thread_for_sub(sub)
            flash('تم التعديل.', 'success')
        else:
            new_sub = Subscription(user_id=current_user.id, name=name, keywords=keywords, recipients=current_user.phone,
                                   cities=cities, city_filter_enabled=city_en, excluded_words=excl_words, exclude_enabled=excl_en,
                                   quiet_enabled=quiet_en, quiet_start_hour=q_sh, quiet_end_hour=q_eh, sleep_minutes=15,
                                   end_ts=end_time, status='paused' if is_expired else 'active')
            db.session.add(new_sub); db.session.commit()
            if not is_expired: start_thread_for_sub(new_sub)
            flash('تم الحفظ.', 'success')
        log_audit_async(current_user.id, 'update_subscription', audit_details, request.remote_addr)
        return redirect(url_for('user_dashboard'))
    return render_template('user.html', sub=sub, logs=logs, is_expired=is_expired)

# ==== تجديد الاشتراك ====
@app.route('/renew_subscription', methods=['GET', 'POST'])
@login_required
def renew_subscription():
    if current_user.role == 'admin': return redirect(url_for('admin_dashboard'))
    settings = SystemSettings.query.first()
    week_price = settings.subscription_week_price if settings else 5
    if RenewalRequest.query.filter_by(user_id=current_user.id, status='pending').first():
        flash('لديك طلب معلق.', 'info')
        return render_template('renew_pending.html')
    if request.method == 'POST':
        try:
            weeks = int(request.form.get('weeks'))
        except:
            flash('عدد غير صالح.', 'danger'); return redirect(url_for('renew_subscription'))
        amount = weeks * week_price
        proof = None
        if 'payment_proof' in request.files:
            file = request.files['payment_proof']
            if file and allowed_file(file.filename):
                ext = file.filename.rsplit('.', 1)[1].lower()
                fname = f"{uuid.uuid4().hex}_{current_user.id}.{ext}"
                file.save(os.path.join(app.config['UPLOAD_FOLDER'], fname))
                proof = fname
        new_req = RenewalRequest(user_id=current_user.id, weeks=weeks, amount=amount, proof_filename=proof)
        db.session.add(new_req); db.session.commit()
        notify = AdminNotifySettings.query.first()
        if notify and notify.admin_phone:
            send_user_message(notify.admin_phone, f"🔔 طلب تجديد: {current_user.username} {weeks} أسبوع بـ{amount} ريال", is_admin=True)
        log_audit_async(current_user.id, 'renewal_request', {'weeks': weeks}, request.remote_addr)
        flash('تم استلام طلبك.', 'success')
        return render_template('renew_pending.html')
    return render_template('renew.html', settings=settings, week_price=week_price)

# ==== مسارات الإدارة ====
@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    total = User.query.count()
    active = User.query.filter_by(is_active_account=True).count()
    pending = RenewalRequest.query.filter_by(status='pending').count()
    recent = AdLog.query.filter(AdLog.timestamp >= datetime.datetime.utcnow() - datetime.timedelta(days=7)).all()
    daily_ads = {}
    for i in range(6, -1, -1):
        day = (datetime.datetime.utcnow() - datetime.timedelta(days=i)).strftime('%m-%d')
        daily_ads[day] = 0
    for log in recent:
        day = log.timestamp.strftime('%m-%d')
        if day in daily_ads: daily_ads[day] += 1
    return render_template('admin_dashboard.html', total_users=total, active_users=active,
                           inactive_users=total - active, pending_renewals_count=pending,
                           chart_labels=list(daily_ads.keys()), chart_data=list(daily_ads.values()),
                           active_threads=ACTIVE_THREADS)

@app.route('/admin/users')
@login_required
def admin_users():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    search = request.args.get('search', '').strip()
    filter_status = request.args.get('filter', 'all')
    page = request.args.get('page', 1, type=int)
    query = User.query.outerjoin(Subscription)
    if search:
        query = query.filter(db.or_(User.username.ilike(f'%{search}%'), User.phone.ilike(f'%{search}%'), Subscription.name.ilike(f'%{search}%')))
    if filter_status == 'active': query = query.filter(User.is_active_account == True)
    elif filter_status == 'inactive': query = query.filter(User.is_active_account == False)
    elif filter_status == 'expired': query = query.filter(User.account_expiration.isnot(None), User.account_expiration < datetime.datetime.now())
    users_paginated = query.order_by(User.id.desc()).paginate(page=page, per_page=20, error_out=False)
    return render_template('admin_users.html', users=users_paginated, search=search, filter_status=filter_status)

@app.route('/admin/ads_log')
@login_required
def admin_ads_log():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    query = db.session.query(AdLog, User).join(User)
    if search:
        query = query.filter(db.or_(User.username.ilike(f'%{search}%'), AdLog.keyword_matched.ilike(f'%{search}%'), AdLog.title.ilike(f'%{search}%')))
    logs_paginated = query.order_by(AdLog.timestamp.desc()).paginate(page=page, per_page=50, error_out=False)
    return render_template('admin_ads_log.html', logs=logs_paginated, search=search)

@app.route('/admin/audit_log')
@login_required
def admin_audit_log():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    search = request.args.get('search', '').strip()
    action_filter = request.args.get('action', '')
    page = request.args.get('page', 1, type=int)
    query = AuditLog.query.outerjoin(User)
    if search: query = query.filter(db.or_(User.username.ilike(f'%{search}%'), AuditLog.action.ilike(f'%{search}%'), AuditLog.ip_address.ilike(f'%{search}%')))
    if action_filter: query = query.filter(AuditLog.action == action_filter)
    logs_paginated = query.order_by(AuditLog.timestamp.desc()).paginate(page=page, per_page=30, error_out=False)
    actions = [a[0] for a in db.session.query(AuditLog.action).distinct().all()]
    return render_template('admin_audit_log.html', logs=logs_paginated, search=search, action_filter=action_filter, actions=actions)

@app.route('/admin/clear_audit_log')
@login_required
def admin_clear_audit_log():
    if current_user.role != 'admin': return redirect(url_for('index'))
    num = AuditLog.query.delete(); db.session.commit()
    flash(f'✅ تم حذف {num} سجل تدقيق.', 'success')
    return redirect(url_for('admin_audit_log'))

@app.route('/admin_statistics')
@login_required
def admin_statistics():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    stats = get_daily_stats()
    total_users = User.query.count()
    active_subs = Subscription.query.filter_by(status='active').count()
    total_ads = AdLog.query.count()
    return render_template('admin_statistics.html', daily_stats=stats, total_users=total_users, active_subs=active_subs, total_ads_logged=total_ads)

@app.route('/admin_settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    settings = SystemSettings.query.first()
    notify = AdminNotifySettings.query.first()
    if request.method == 'POST':
        settings.whatsapp_token = request.form.get('whatsapp_token')
        settings.trial_days = int(request.form.get('trial_days', 2))
        settings.bank_account_number = request.form.get('bank_account_number', '')
        settings.bank_account_name = request.form.get('bank_account_name', '')
        settings.bank_qr_text = request.form.get('bank_qr_text', '')
        settings.subscription_week_price = int(request.form.get('subscription_week_price', 5))
        settings.messaging_method = request.form.get('messaging_method', 'whatsapp')
        settings.telegram_bot_token = request.form.get('telegram_bot_token', '')
        settings.telegram_chat_id = request.form.get('telegram_chat_id', '')
        if notify:
            notify.admin_phone = request.form.get('admin_phone', '')
        db.session.commit()
        flash('تم حفظ الإعدادات.', 'success')
        return redirect(url_for('admin_settings'))
    return render_template('admin_settings.html', settings=settings, notify=notify)

@app.route('/admin_add_user', methods=['GET', 'POST'])
@login_required
def admin_add_user():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    if request.method == 'POST':
        username = request.form.get('username')
        phone = request.form.get('phone')
        password = request.form.get('password')
        exp_date = request.form.get('account_expiration')
        if User.query.filter(db.or_(User.username == username, User.phone == phone)).first():
            flash('مستخدم موجود.', 'danger')
            return redirect(url_for('admin_add_user'))
        new_user = User(username=username, phone=phone, password=generate_password_hash(password, method='pbkdf2:sha256'),
                        account_expiration=datetime.datetime.strptime(exp_date, '%Y-%m-%d') if exp_date else None)
        db.session.add(new_user); db.session.commit()
        if request.form.get('keywords'):
            sub = Subscription(user_id=new_user.id, name=request.form.get('name', 'اشتراك'), keywords=request.form.get('keywords'),
                               recipients=phone, cities=request.form.get('cities', ''), city_filter_enabled='city_filter_enabled' in request.form,
                               excluded_words=request.form.get('excluded_words', ''), exclude_enabled='exclude_enabled' in request.form,
                               quiet_enabled='quiet_enabled' in request.form, quiet_start_hour=int(request.form.get('q_sh', 1)),
                               quiet_end_hour=int(request.form.get('q_eh', 6)), sleep_minutes=15,
                               end_ts=exp_date if exp_date else "")
            db.session.add(sub); db.session.commit()
            if not exp_date or datetime.datetime.strptime(exp_date, '%Y-%m-%d') > datetime.datetime.now():
                start_thread_for_sub(sub)
        send_user_message(phone, f"تم إنشاء حسابك في راصد حراج. تاريخ الانتهاء: {exp_date or 'مفتوح'}", user_id=new_user.id)
        flash('تم إضافة العميل.', 'success')
        return redirect(url_for('admin_users'))
    return render_template('admin_add_user.html')

@app.route('/admin_edit_user/<int:user_id>', methods=['GET', 'POST'])
@login_required
def admin_edit_user(user_id):
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    user = User.query.get_or_404(user_id)
    if request.method == 'POST':
        user.username = request.form.get('username')
        user.phone = request.form.get('phone')
        if request.form.get('password'):
            user.password = generate_password_hash(request.form.get('password'), method='pbkdf2:sha256')
        user.account_expiration = datetime.datetime.strptime(request.form.get('account_expiration'), '%Y-%m-%d') if request.form.get('account_expiration') else None
        if user.subscription:
            user.subscription.recipients = user.phone
            user.subscription.end_ts = user.account_expiration.isoformat() if user.account_expiration else ""
        db.session.commit()
        flash('تم التعديل.', 'success')
        return redirect(url_for('admin_users'))
    return render_template('admin_edit_user.html', user=user)

@app.route('/toggle_user/<int:user_id>')
@login_required
def toggle_user(user_id):
    if current_user.role == 'admin':
        user = User.query.get_or_404(user_id)
        if user.id != current_user.id:
            user.is_active_account = not user.is_active_account
            if not user.is_active_account and user.subscription and user.subscription.id in ACTIVE_THREADS:
                ACTIVE_THREADS[user.subscription.id].stop(); del ACTIVE_THREADS[user.subscription.id]
            db.session.commit()
    return redirect(request.referrer or url_for('admin_users'))

@app.route('/admin_toggle_sub/<int:sub_id>')
@login_required
def admin_toggle_sub(sub_id):
    if current_user.role != 'admin': return redirect(url_for('index'))
    sub = Subscription.query.get_or_404(sub_id)
    if sub.status == 'active':
        sub.status = 'paused'
        if sub.id in ACTIVE_THREADS: ACTIVE_THREADS[sub.id].stop(); del ACTIVE_THREADS[sub.id]
        flash('تم الإيقاف.', 'warning')
    else:
        user = sub.owner
        if user.account_expiration and datetime.datetime.now() > user.account_expiration:
            flash('الحساب منتهي.', 'danger')
        else:
            sub.status = 'active'; start_thread_for_sub(sub)
            flash('تم التشغيل.', 'success')
    db.session.commit()
    return redirect(request.referrer or url_for('admin_users'))

@app.route('/impersonate/<int:user_id>')
@login_required
def impersonate(user_id):
    if current_user.role != 'admin': return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    session['admin_impersonating'] = current_user.id
    login_user(user)
    flash(f'دخلت كـ {user.username}', 'warning')
    return redirect(url_for('user_dashboard'))

@app.route('/revert_impersonate')
@login_required
def revert_impersonate():
    if 'admin_impersonating' in session:
        admin = User.query.get(session['admin_impersonating'])
        if admin:
            login_user(admin)
            session.pop('admin_impersonating', None)
    return redirect(url_for('admin_dashboard'))

@app.route('/admin_delete_user/<int:user_id>')
@login_required
def admin_delete_user(user_id):
    if current_user.role != 'admin': return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash('لا يمكن حذف نفسك.', 'danger')
        return redirect(url_for('admin_users'))
    try:
        if user.subscription and user.subscription.id in ACTIVE_THREADS:
            ACTIVE_THREADS[user.subscription.id].stop(); del ACTIVE_THREADS[user.subscription.id]
        if user.subscription:
            sid = user.subscription.id
            for f in [SUBS_BASE_DIR / f"seen_{sid}.json", SUBS_BASE_DIR / f"queue_{sid}.json"]:
                if f.exists(): f.unlink()
        AdLog.query.filter_by(user_id=user.id).delete()
        if user.subscription: db.session.delete(user.subscription)
        db.session.delete(user); db.session.commit()
        flash('تم الحذف.', 'success')
    except Exception as e:
        db.session.rollback(); flash(f'خطأ: {e}', 'danger')
    return redirect(url_for('admin_users'))

@app.route('/toggle_sub/<int:sub_id>')
@login_required
def toggle_sub(sub_id):
    sub = Subscription.query.get_or_404(sub_id)
    if sub.user_id == current_user.id or current_user.role == 'admin':
        if sub.status == 'active':
            sub.status = 'paused'
            if sub.id in ACTIVE_THREADS: ACTIVE_THREADS[sub.id].stop(); del ACTIVE_THREADS[sub.id]
            flash('تم الإيقاف.', 'warning')
        else:
            user = sub.owner
            if user.account_expiration and datetime.datetime.now() > user.account_expiration:
                if current_user.role != 'admin':
                    flash('الحساب منتهي.', 'danger')
                    return redirect(request.referrer or url_for('user_dashboard'))
            sub.status = 'active'; start_thread_for_sub(sub)
            flash('تم التشغيل.', 'success')
        db.session.commit()
    return redirect(request.referrer or url_for('user_dashboard'))

@app.route('/delete_sub/<int:sub_id>')
@login_required
def delete_sub(sub_id):
    sub = Subscription.query.get_or_404(sub_id)
    if sub.user_id == current_user.id or current_user.role == 'admin':
        if sub.id in ACTIVE_THREADS: ACTIVE_THREADS[sub.id].stop(); del ACTIVE_THREADS[sub.id]
        db.session.delete(sub); db.session.commit()
        flash('تم حذف الاشتراك.', 'info')
    return redirect(request.referrer or url_for('user_dashboard'))

@app.route('/admin_update_sleep/<int:sub_id>', methods=['POST'])
@login_required
def admin_update_sleep(sub_id):
    if current_user.role != 'admin': return redirect(url_for('index'))
    sub = Subscription.query.get_or_404(sub_id)
    new_sleep = request.form.get('sleep_minutes', type=int)
    if new_sleep and new_sleep > 0:
        sub.sleep_minutes = new_sleep
        if sub.status == 'active':
            if sub.id in ACTIVE_THREADS: ACTIVE_THREADS[sub.id].stop(); del ACTIVE_THREADS[sub.id]
            start_thread_for_sub(sub)
        db.session.commit()
        flash('تم تحديث سرعة الفحص.', 'success')
    return redirect(request.referrer or url_for('admin_users'))

@app.route('/admin/renewal_requests')
@login_required
def admin_renewal_requests():
    if current_user.role != 'admin': return redirect(url_for('index'))
    requests_list = RenewalRequest.query.order_by(RenewalRequest.created_at.desc()).all()
    pending_count = RenewalRequest.query.filter_by(status='pending').count()
    return render_template('admin_renewals.html', requests=requests_list, pending_count=pending_count)

@app.route('/admin/process_renewal/<int:req_id>/<action>')
@login_required
def process_renewal(req_id, action):
    if current_user.role != 'admin': return redirect(url_for('index'))
    req = RenewalRequest.query.get_or_404(req_id)
    if req.status != 'pending':
        flash('تمت معالجته مسبقاً.', 'warning')
        return redirect(url_for('admin_renewal_requests'))
    user = req.owner
    if action == 'approve':
        if user.account_expiration and user.account_expiration > datetime.datetime.now():
            user.account_expiration += datetime.timedelta(weeks=req.weeks)
        else:
            user.account_expiration = datetime.datetime.now() + datetime.timedelta(weeks=req.weeks)
        req.status = 'approved'; req.processed_at = datetime.datetime.utcnow()
        if user.subscription:
            if user.subscription.status != 'active': user.subscription.status = 'active'
            if user.subscription.id not in ACTIVE_THREADS or not ACTIVE_THREADS[user.subscription.id].is_alive():
                start_thread_for_sub(user.subscription)
        if req.proof_filename:
            pf = os.path.join(app.config['UPLOAD_FOLDER'], req.proof_filename)
            if os.path.exists(pf): os.remove(pf)
        db.session.commit()
        send_user_message(user.phone, f"🎉 تم تجديد اشتراكك {req.weeks} أسبوع حتى {user.account_expiration.strftime('%Y-%m-%d')}", user_id=user.id)
        flash('تمت الموافقة.', 'success')
    elif action == 'reject':
        req.status = 'rejected'; req.processed_at = datetime.datetime.utcnow()
        db.session.commit()
        if req.proof_filename:
            pf = os.path.join(app.config['UPLOAD_FOLDER'], req.proof_filename)
            if os.path.exists(pf): os.remove(pf)
        flash('تم الرفض.', 'warning')
    return redirect(url_for('admin_renewal_requests'))

# ================= بدء التشغيل والترحيل =================
with app.app_context():
    db.create_all()
    try:
        with db.engine.connect() as conn:
            dialect = conn.engine.dialect.name

            # --- أعمدة system_settings ---
            if dialect == 'postgresql':
                for col, typ in [
                    ("messaging_method", "VARCHAR(10) DEFAULT 'whatsapp'"),
                    ("telegram_bot_token", "VARCHAR(255) DEFAULT ''"),
                    ("telegram_chat_id", "VARCHAR(50) DEFAULT ''")
                ]:
                    try:
                        conn.execute(db.text(f"ALTER TABLE system_settings ADD COLUMN IF NOT EXISTS {col} {typ}"))
                        conn.commit()
                    except Exception:
                        try:
                            conn.execute(db.text(f"ALTER TABLE system_settings ADD COLUMN {col} {typ}"))
                            conn.commit()
                        except Exception as ex:
                            if 'already exists' not in str(ex):
                                logger.warning(f"system_settings.{col}: {ex}")

            # --- عمود user.telegram_chat_id ---
            if dialect == 'postgresql':
                try:
                    conn.execute(db.text('ALTER TABLE "user" ADD COLUMN IF NOT EXISTS telegram_chat_id VARCHAR(50)'))
                    conn.commit()
                except Exception:
                    try:
                        conn.execute(db.text('ALTER TABLE "user" ADD COLUMN telegram_chat_id VARCHAR(50)'))
                        conn.commit()
                    except Exception as ex:
                        if 'already exists' not in str(ex):
                            logger.warning(f"user.telegram_chat_id: {ex}")
            elif dialect == 'sqlite':
                try:
                    conn.execute(db.text("ALTER TABLE user ADD COLUMN telegram_chat_id VARCHAR(50)"))
                    conn.commit()
                except Exception as ex:
                    if 'duplicate column' not in str(ex).lower():
                        logger.warning(f"SQLite user.telegram_chat_id: {ex}")

    except Exception as e:
        logger.error(f"خطأ ترحيل: {e}")

    if not SystemSettings.query.first():
        db.session.add(SystemSettings())
        db.session.commit()
    if not AdminNotifySettings.query.first():
        db.session.add(AdminNotifySettings())
        db.session.commit()

with app.app_context():
    for sub in Subscription.query.filter_by(status='active').all():
        if sub.owner.is_active_account and (not sub.owner.account_expiration or sub.owner.account_expiration > datetime.datetime.now()):
            start_thread_for_sub(sub)

if __name__ == '__main__':
    with app.app_context():
        for sub in Subscription.query.filter_by(status='active').all():
            if sub.owner.is_active_account and (not sub.owner.account_expiration or sub.owner.account_expiration > datetime.datetime.now()):
                start_thread_for_sub(sub)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
