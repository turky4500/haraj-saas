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

app = Flask(__name__)
app.secret_key = "haraj_super_secret_key_v18_final_launch"

@app.before_request
def redirect_to_hetzner():
    if 'onrender.com' in request.host:
        return redirect("http://95.217.133.90:5000" + request.full_path, code=301)


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

# أقفال للملفات المشتركة
seen_file_lock = threading.Lock()
audit_log_lock = threading.Lock()  # قفل لسجل التدقيق

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
            "message_preview": message_text[:50] + "..." if len(message_text) > 50 else message_text,
            "message_text": message_text
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
                
                # تنظيف سجل التدقيق القديم (أكبر من 90 يوماً)
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
                token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
                url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
                notify = AdminNotifySettings.query.first()
                if notify and notify.admin_phone:
                    if now.hour >= 23 and notify.last_report_date < now.date():
                        ds = get_daily_stats()
                        msg = f"📊 تقرير نهاية اليوم لمنصة (راصد حراج):\n\n👥 زوار بشريين: {ds['visitors']}\n🤖 زيارات الروبوت: {ds['bot_visits']}\n🆕 تسجيلات جديدة: {ds['registrations']}\n💬 رسائل أُرسلت: {ds['messages_sent']}\n\nيعطيك العافية 🚀"
                        send_whatsapp(create_session(), token, notify.admin_phone, msg, url=url)
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
                                if send_whatsapp(create_session(), token, u.phone, msg, url=url):
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
                            try:
                                ACTIVE_THREADS[sub.id].stop()
                            except:
                                pass
                            del ACTIVE_THREADS[sub.id]
                        start_thread_for_sub(sub)
                        settings = SystemSettings.query.first()
                        token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
                        url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
                        notify = AdminNotifySettings.query.first()
                        if notify and notify.admin_phone:
                            msg = f"🔄 تم إعادة تشغيل رادار المستخدم {sub.owner.username} (الاشتراك {sub.id}) تلقائياً."
                            send_whatsapp(create_session(), token, notify.admin_phone, msg, url=url)
            except Exception as e:
                logger.error(f"خطأ في مراقبة الخيوط: {str(e)}")

threading.Thread(target=monitor_threads_health, daemon=True).start()

# ================= دوال سجل التدقيق =================
def log_audit_async(user_id, action, details=None, ip_address=None):
    """تسجيل حدث تدقيق بشكل غير متزامن لتجنب تأخير الاستجابة"""
    def _log():
        with app.app_context():
            try:
                # تحويل التفاصيل إلى JSON مع الحفاظ على العربية
                details_json = None
                if details:
                    details_json = json.dumps(details, ensure_ascii=False, default=str)
                
                log_entry = AuditLog(
                    user_id=user_id,
                    action=action,
                    details=details_json,
                    ip_address=ip_address
                )
                db.session.add(log_entry)
                db.session.commit()
            except Exception as e:
                logger.error(f"فشل تسجيل التدقيق: {e}")
    threading.Thread(target=_log, daemon=True).start()

# ================= النماذج =================
class SystemSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    whatsapp_token = db.Column(db.String(255), default="sau11zbtz1ruma8o2k5tt")
    trial_days = db.Column(db.Integer, default=2)
    # إعدادات الدفع البنكي
    bank_account_number = db.Column(db.String(50), default="")
    bank_account_name = db.Column(db.String(100), default="")
    bank_qr_text = db.Column(db.Text, default="")
    subscription_week_price = db.Column(db.Integer, default=5)
    # إعدادات البوابات المتعددة
    active_gateway = db.Column(db.String(10), default="1")
    gateway_1_name = db.Column(db.String(100), default="البوابة الأولى")
    gateway_2_name = db.Column(db.String(100), default="البوابة الثانية")
    gateway_url_1 = db.Column(db.String(255), default="http://127.0.0.1:3000/api/v1/send")
    gateway_url_2 = db.Column(db.String(255), default="https://whatsapp.tkwin.com.sa/api/v1/send")
    whatsapp_token_2 = db.Column(db.String(255), default="7a203d6ba6f4325ed3261ea87f6b2e751250ad97")

    @property
    def active_whatsapp_token(self):
        if self.active_gateway == '2':
            return self.whatsapp_token_2 or "7a203d6ba6f4325ed3261ea87f6b2e751250ad97"
        return self.whatsapp_token or "sau11zbtz1ruma8o2k5tt"

    @property
    def active_whatsapp_url(self):
        if self.active_gateway == '2':
            return self.gateway_url_2 or "https://whatsapp.tkwin.com.sa/api/v1/send"
        return self.gateway_url_1 or "http://127.0.0.1:3000/api/v1/send"

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
    status = db.Column(db.String(20), default='pending')  # pending, approved, rejected
    proof_filename = db.Column(db.String(255), nullable=True)  # اسم الملف المحفوظ
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
    if re.search(r'(^|\s)' + re.escape(norm_kw) + r'($|\s)', nt):
        return True
    return False

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
    req_session = requests.Session()
    retries = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504))
    req_session.mount("https://", HTTPAdapter(max_retries=retries))
    return req_session

def extract_ads(html_bytes, base_url):
    soup = BeautifulSoup(html_bytes, "html.parser")
    ads = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if re.match(r"https?://(?:www\.)?haraj\.com(?:\.sa)?/\d+/.+", urljoin(base_url, href)):
            ads.append((a.get_text(strip=True) or "إعلان", urljoin(base_url, href)))
    return list(dict.fromkeys(ads))

# ================= دالة إرسال الواتساب =================
def send_whatsapp(req_session, token, to_msisdn, text, url="http://127.0.0.1:3000/api/v1/send", max_retries=3):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    
    logger.info(f"📤 محاولة إرسال واتساب إلى {to_msisdn} - نص الرسالة: {text[:50]}...")
    
    response_data = {"status_code": None, "text": "", "json": None}
    
    for attempt in range(1, max_retries + 1):
        try:
            response = req_session.post(
                url, 
                json={"to": to_msisdn, "message": text}, 
                headers=headers, 
                timeout=20, 
                verify=False
            )
            
            response_data["status_code"] = response.status_code
            response_data["text"] = response.text
            
            try:
                result = response.json()
                response_data["json"] = result
                logger.info(f"📥 JSON الرد: {result}")
            except:
                result = {"raw_text": response.text}
                logger.info(f"📥 نص الرد: {response.text}")
            
            success = False
            if response.status_code == 200:
                if isinstance(result, dict):
                    # إذا فيه error فالرد = فشل حتى لو كود 200
                    if result.get("error"):
                        success = False
                        logger.error(f"❌ API أرجع خطأ: {result.get('error')}")
                    elif result.get("success") is True:
                        success = True
                    elif result.get("status") == "sent":
                        success = True
                    elif result.get("message_id"):
                        success = True
                    else:
                        success = False
                else:
                    success = False
            
            log_whatsapp_attempt(to_msisdn, success, response_data, text)
            
            if success:
                update_daily_stat('messages_sent')
                logger.info(f"✅ تم إرسال الرسالة بنجاح إلى {to_msisdn} (محاولة {attempt})")
                return True
            else:
                logger.error(f"❌ فشل إرسال الرسالة إلى {to_msisdn} (محاولة {attempt}): {response.status_code} - {response.text}")
                if attempt < max_retries:
                    wait_time = 2 ** attempt
                    logger.info(f"⏳ انتظار {wait_time} ثواني قبل إعادة المحاولة...")
                    time.sleep(wait_time)
                    continue
                else:
                    return False
                    
        except requests.exceptions.RequestException as e:
            logger.error(f"❌ خطأ في الاتصال (محاولة {attempt}): {str(e)}")
            response_data["error"] = str(e)
            if attempt < max_retries:
                wait_time = 2 ** attempt
                logger.info(f"⏳ انتظار {wait_time} ثواني قبل إعادة المحاولة...")
                time.sleep(wait_time)
            else:
                log_whatsapp_attempt(to_msisdn, False, response_data, text)
                return False
    
    return False

# ================= مسار عرض صورة الإثبات (للأدمن فقط) =================
@app.route('/admin/view_proof/<int:request_id>')
@login_required
def view_proof(request_id):
    if current_user.role != 'admin':
        return "غير مصرح", 403
    req = RenewalRequest.query.get_or_404(request_id)
    if not req.proof_filename:
        flash('لا توجد صورة مرفقة لهذا الطلب.', 'warning')
        return redirect(url_for('admin_renewal_requests'))
    # التأكد من وجود الملف
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], req.proof_filename)
    if not os.path.exists(filepath):
        flash('ملف الصورة غير موجود (ربما تم حذفه تلقائياً).', 'danger')
        return redirect(url_for('admin_renewal_requests'))
    return send_from_directory(app.config['UPLOAD_FOLDER'], req.proof_filename)

# ================= صفحة سجل الواتساب =================
@app.route('/admin/whatsapp_logs')
@login_required
def admin_whatsapp_logs():
    if current_user.role != 'admin':
        return "غير مصرح", 403
    
    logs = []
    try:
        if WHATSAPP_LOG_FILE.exists():
            with open(WHATSAPP_LOG_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
                if content:
                    logs = json.loads(content)
                else:
                    logs = []
        else:
            logs = []
    except (json.JSONDecodeError, IOError, Exception) as e:
        logger.error(f"خطأ في قراءة سجل الواتساب: {str(e)}")
        # في حالة تلف الملف، نبدأ بسجل فارغ ونحتفظ بنسخة احتياطية
        backup_file = WHATSAPP_LOG_FILE.with_suffix('.json.bak')
        if WHATSAPP_LOG_FILE.exists():
            try:
                WHATSAPP_LOG_FILE.rename(backup_file)
            except:
                pass
        logs = []
        flash('تم اكتشاف تلف في ملف السجل وتم إنشاء ملف جديد.', 'warning')
    
    # عكس الترتيب لعرض الأحدث أولاً
    logs.reverse()
    return render_template('whatsapp_logs.html', logs=logs)

# ================= مسار حذف سجل الواتساب =================
@app.route('/admin/clear_whatsapp_logs')
@login_required
def admin_clear_whatsapp_logs():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    try:
        with open(WHATSAPP_LOG_FILE, 'w') as f:
            json.dump([], f)
        flash('✅ تم حذف جميع سجلات الواتساب بنجاح.', 'success')
    except Exception as e:
        flash(f'❌ حدث خطأ أثناء حذف السجلات: {str(e)}', 'danger')
    
    return redirect(url_for('admin_whatsapp_logs'))

# ================= مسار إعادة إرسال رسالة من السجل =================
@app.route('/admin/resend_whatsapp', methods=['POST'])
@login_required
def admin_resend_whatsapp():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    to_number = request.form.get('to')
    message_text = request.form.get('message')
    
    if not to_number or not message_text:
        flash('❌ رقم الهاتف أو نص الرسالة غير مكتمل.', 'danger')
        return redirect(url_for('admin_whatsapp_logs'))
    
    settings = SystemSettings.query.first()
    token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
    url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
    
    success = send_whatsapp(create_session(), token, to_number, message_text, url=url)
    if success:
        flash('✅ تم إعادة إرسال الرسالة بنجاح! 🚀', 'success')
    else:
        flash('❌ فشل إعادة إرسال الرسالة، يرجى مراجعة سجل الأخطاء.', 'danger')
        
    return redirect(url_for('admin_whatsapp_logs'))

# ================= مسار حذف الأرشيف =================
@app.route('/admin/clear_archive')
@login_required
def admin_clear_archive():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    try:
        num_deleted = AdLog.query.delete()
        db.session.commit()
        flash(f'✅ تم حذف {num_deleted} سجل من الأرشيف بنجاح.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ حدث خطأ أثناء حذف الأرشيف: {str(e)}', 'danger')
    
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
        if self.queue_file.exists():
            with open(self.queue_file, 'r') as f: self.queued_ads = json.load(f)
        else:
            self.queued_ads = []

    def _load_seen(self):
        with seen_file_lock:
            if self.seen_file.exists():
                try:
                    with open(self.seen_file, 'r') as f:
                        self.seen_ids = set(json.load(f))
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
                    # --- إعادة تحميل إعدادات الاشتراك من قاعدة البيانات ---
                    try:
                        sub = Subscription.query.get(self.cfg['id'])
                        if not sub:
                            logger.error(f"الاشتراك {self.cfg['id']} لم يعد موجوداً. إيقاف الخيط.")
                            break
                        
                        user = sub.owner
                        if not user or not user.is_active_account or (user.account_expiration and user.account_expiration < datetime.datetime.now()):
                            if sub.status == 'active': 
                                sub.status = 'paused'
                                db.session.commit()
                                settings = SystemSettings.query.first()
                                current_token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
                                current_url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
                                exp_msg = f"🌸 مرحباً {user.username},\n\nنأمل أن تكون أيامك مليئة بالصيدات الموفقة! مع الأسف، اشتراكك في **راصد حراج** قد انتهى اليوم. 📅\n\nلكن لا تقلق، رادارك ما زال محفوظاً وجاهزاً للاستئناف فور تجديد الاشتراك. نحن هنا لخدمتك دائماً ونسعد بعودتك إلينا. 💙\n\nإذا كان لديك أي استفسار، تواصل معنا بكل حب.\n\nشكراً لثقتك، وإلى لقاء قريب بإذن الله 🌹"
                                send_whatsapp(self.req_session, current_token, self.cfg['recipients'], exp_msg, url=current_url)
                            logger.info(f"الاشتراك {self.cfg['id']} غير نشط أو منتهي. إيقاف الخيط.")
                            break
                        
                        # تحديث التكوين من قاعدة البيانات (عدا sleep_minutes)
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
                    except Exception as reload_err:
                        logger.error(f"خطأ في إعادة تحميل إعدادات الاشتراك {self.cfg['id']}: {reload_err}")
                        # نستمر بالإعدادات القديمة دون إنهاء الخيط
                    
                    settings = SystemSettings.query.first()
                    current_token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
                    current_url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
                    
                    currently_quiet = is_quiet_now(self.cfg['quiet_enabled'], self.cfg['q_sh'], self.cfg['q_sm'], self.cfg['q_eh'], self.cfg['q_em'])

                    if not currently_quiet and self.queued_ads:
                        wake_msg = "🌅 انتهت فترة الهدوء!\nإليك الإعلانات التي تم رصدها وتخزينها أثناء فترة توقف الإشعارات:"
                        send_whatsapp(self.req_session, current_token, self.cfg['recipients'], wake_msg, url=current_url)
                        time.sleep(3)
                        for ad in self.queued_ads:
                            if self.stop_evt.is_set(): break
                            msg = f"إعلان ({ad['kw']}):\n{ad['title']}\n{ad['url']}\n\n⚙️ تذكير لطيف: تقدر تتحكم بإعدادات الرصد ومتابعة أرشيف إعلاناتك بكل سهولة من هنا:\n🔗 https://haraj-saas.duckdns.org"
                            send_whatsapp(self.req_session, current_token, self.cfg['recipients'], msg, url=current_url)
                            time.sleep(random.uniform(5, 10))
                        
                        self.queued_ads = []
                        if self.queue_file.exists():
                            with open(self.queue_file, 'w') as f: json.dump(self.queued_ads, f)
                    
                    for kw in self.cfg['keywords']:
                        if self.stop_evt.is_set(): break
                        
                        for page in range(1, 4):
                            if self.stop_evt.is_set(): break
                            
                            if kw:
                                url = f"{HARAJ_BASE}/search/{quote(kw, safe='')}/page/{page}" if page > 1 else f"{HARAJ_BASE}/search/{quote(kw, safe='')}/"
                            else:
                                url = f"{HARAJ_BASE}/page/{page}" if page > 1 else f"{HARAJ_BASE}/"
                                
                            try:
                                html = self.req_session.get(url, headers=HARAJ_HEADERS, timeout=15, verify=False).content
                                for title, ad_url in extract_ads(html, HARAJ_BASE):
                                    ad_id = re.search(r"/(\d+)(?:/|$)", ad_url).group(1)
                                    if ad_id not in self.seen_ids:
                                        ad_html = self.req_session.get(ad_url, headers=HARAJ_HEADERS, timeout=15, verify=False).content
                                        soup = BeautifulSoup(ad_html, "html.parser")
                                        full_text = soup.get_text(" ", strip=True)
                                        
                                        if is_target_city(full_text, self.cfg['cities'], self.cfg['city_filter_enabled']) and \
                                           matches_keyword_precise(full_text, kw, self.cfg['excluded_words'], self.cfg['exclude_enabled']):
                                            
                                            self.seen_ids.add(ad_id)
                                            self._save_seen()
                                            
                                            with app.app_context():
                                                existing = AdLog.query.filter_by(user_id=self.cfg['user_id'], url=ad_url).first()
                                                if existing:
                                                    continue
                                            
                                            if currently_quiet:
                                                self.queued_ads.append({'kw': kw, 'title': title, 'url': ad_url})
                                                with open(self.queue_file, 'w') as f: json.dump(self.queued_ads, f)
                                            else:
                                                delay = random.uniform(30, 60)
                                                time.sleep(delay)
                                                msg = f"إعلان جديد ({kw}):\n{title}\n{ad_url}\n\n⚙️ تذكير لطيف: تقدر تتحكم بإعدادات الرصد ومتابعة أرشيف إعلاناتك بكل سهولة من هنا:\n🔗 https://haraj-saas.duckdns.org"
                                                send_whatsapp(self.req_session, current_token, self.cfg['recipients'], msg, url=current_url)
                                                
                                            with app.app_context():
                                                log_sub = Subscription.query.get(self.cfg['id'])
                                                if log_sub:
                                                    log_sub.sent_count += 1
                                                    new_log = AdLog(
                                                        user_id=self.cfg['user_id'], 
                                                        title=title, 
                                                        url=ad_url, 
                                                        keyword_matched=kw
                                                    )
                                                    db.session.add(new_log)
                                                    db.session.commit()
                            except Exception as e:
                                logger.error(f"خطأ في رصد الإعلانات (كلمة: {kw}، صفحة: {page}) للاشتراك {self.cfg['id']}: {str(e)}")
                                # استمرار الحلقة دون إنهاء الخيط
                                pass
                            time.sleep(random.uniform(3, 7))
                    
                    sleep_seconds = self.cfg['sleep_minutes'] * 60
                    for _ in range(sleep_seconds):
                        if self.stop_evt.is_set(): break
                        time.sleep(1)
            except Exception as e:
                # أي خطأ غير متوقع لا ينهي الخيط إلا إذا كان خطأ فادحاً (مثل MemoryError)
                logger.error(f"خطأ غير متوقع في خيط الاشتراك {self.cfg['id']}: {str(e)}")
                # انتظار قليل ثم إعادة المحاولة
                time.sleep(60)
                continue

    def stop(self):
        self.stop_evt.set()


def pre_populate_seen_ids(sub_id, keywords):
    """
    عند إنشاء اشتراك جديد، يتم مسح الإعلانات الحالية في الصفحات 1-3
    وحفظ معرّفاتها كـ"مشاهدة" دون إرسال أي رسائل.
    هذا يضمن أن المستخدم الجديد يستقبل فقط الإعلانات الجديدة من لحظة التسجيل.
    """
    seen_file = SUBS_BASE_DIR / f"seen_{sub_id}.json"
    if seen_file.exists():
        return  # الاشتراك قديم وله ملف seen بالفعل

    logger.info(f"⏳ بدء المسح الصامت للاشتراك الجديد {sub_id} ...")
    req_session = create_session()
    seen_ids = set()
    try:
        for kw in keywords:
            for page in range(1, 4):
                if kw:
                    url = f"{HARAJ_BASE}/search/{quote(kw, safe='')}/page/{page}" if page > 1 else f"{HARAJ_BASE}/search/{quote(kw, safe='')}/"
                else:
                    url = f"{HARAJ_BASE}/page/{page}" if page > 1 else f"{HARAJ_BASE}/"
                try:
                    html = req_session.get(url, headers=HARAJ_HEADERS, timeout=15, verify=False).content
                    for _, ad_url in extract_ads(html, HARAJ_BASE):
                        m = re.search(r"/(\d+)(?:/|$)", ad_url)
                        if m:
                            seen_ids.add(m.group(1))
                except Exception as e:
                    logger.warning(f"خطأ في المسح الصامت (كلمة: {kw}، صفحة: {page}): {e}")
                time.sleep(1)
    finally:
        with seen_file_lock:
            with open(seen_file, 'w') as f:
                json.dump(list(seen_ids), f)
        logger.info(f"✅ تم حفظ {len(seen_ids)} إعلان كـ'مشاهد' للاشتراك الجديد {sub_id} بدون إرسال.")

def start_thread_for_sub(sub, is_new=False):
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
    # إذا كان اشتراكاً جديداً، نجري مسحاً صامتاً أولاً لتجنب إرسال الإعلانات القديمة
    if is_new:
        def _pre_and_start():
            pre_populate_seen_ids(sub.id, cfg['keywords'])
            t = MonitorThread(cfg)
            ACTIVE_THREADS[sub.id] = t
            t.start()
            logger.info(f"✅ تم بدء خيط للاشتراك {sub.id} للمستخدم {sub.owner.username}")
        threading.Thread(target=_pre_and_start, daemon=True).start()
    else:
        t = MonitorThread(cfg)
        ACTIVE_THREADS[sub.id] = t
        t.start()
        logger.info(f"✅ تم بدء خيط للاشتراك {sub.id} للمستخدم {sub.owner.username}")



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
        username = request.form.get('username')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, request.form.get('password')):
            if not user.is_active_account:
                flash('حسابك موقوف من قبل الإدارة.', 'danger')
                return redirect(url_for('login'))
            login_user(user)
            log_audit_async(user.id, 'login', {'نوع الحدث': 'تسجيل دخول', 'اسم المستخدم': username}, request.remote_addr)
            return redirect(url_for('admin_dashboard') if user.role == 'admin' else url_for('user_dashboard'))
        # تسجيل محاولة فاشلة (اختياري)
        flash('بيانات الدخول غير صحيحة!', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        phone = request.form.get('phone')
        password = request.form.get('password')
        
        existing_user = User.query.filter(
            (User.username == username) | (User.phone == phone)
        ).first()
        if existing_user:
            flash('اسم المستخدم أو رقم الجوال مسجل مسبقاً!', 'danger')
            return redirect(url_for('register'))
        
        otp = str(random.randint(1000, 9999))
        session['temp_user'] = {
            'username': username,
            'phone': phone,
            'password': generate_password_hash(password, method='pbkdf2:sha256')
        }
        session['otp'] = otp
        
        settings = SystemSettings.query.first()
        current_token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
        current_url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
        
        otp_msg = f"مرحباً بك في راصد حراج! 🎯\n\nكود التفعيل الخاص بك هو: *{otp}*\n\nيرجى إدخاله في الموقع لإكمال التسجيل."
        send_whatsapp(create_session(), current_token, phone, otp_msg, url=current_url)
        
        return redirect(url_for('verify'))
    return render_template('register.html')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if request.method == 'POST':
        if request.form.get('otp') == session.get('otp'):
            temp = session['temp_user']
            new_user = User(
                username=temp['username'],
                phone=temp['phone'],
                password=temp['password']
            )
            
            settings = SystemSettings.query.first()
            trial_days = settings.trial_days if settings else 2

            if User.query.count() == 0: 
                new_user.role = 'admin'
                new_user.account_expiration = None 
            else:
                new_user.account_expiration = datetime.datetime.now() + datetime.timedelta(days=trial_days)

            db.session.add(new_user)
            db.session.commit()
            
            update_daily_stat('registrations')
            
            notify = AdminNotifySettings.query.first()
            if notify and notify.admin_phone and new_user.role != 'admin':
                admin_token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
                admin_url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
                admin_msg = f"🔔 عميل جديد سجل بالمنصة!\n\n👤 الاسم: {new_user.username}\n📱 الجوال: {new_user.phone}"
                send_whatsapp(create_session(), admin_token, notify.admin_phone, admin_msg, url=admin_url)
            
            login_user(new_user)
            session.pop('temp_user', None)
            session.pop('otp', None)
            
            log_audit_async(new_user.id, 'register', {'نوع الحدث': 'تسجيل جديد', 'المستخدم': new_user.username, 'الجوال': new_user.phone}, request.remote_addr)
            flash('تم التسجيل والدخول بنجاح! مرحباً بك 🚀', 'success')
            return redirect(url_for('user_dashboard'))
            
        flash('كود التحقق غير صحيح!', 'danger')
    return render_template('verify.html')

@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    if request.method == 'POST':
        phone = request.form.get('phone')
        user = User.query.filter_by(phone=phone).first()
        if user:
            otp = str(random.randint(1000, 9999))
            session['reset_phone'] = phone
            session['reset_otp'] = otp
            
            settings = SystemSettings.query.first()
            current_token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
            current_url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
            reset_msg = f"أهلاً بك 🛡️\n\nكود استعادة كلمة المرور لحسابك هو: *{otp}*"
            send_whatsapp(create_session(), current_token, phone, reset_msg, url=current_url)
            
            return redirect(url_for('reset_password'))
        flash('رقم الجوال غير مسجل بالنظام!', 'danger')
    return render_template('forgot_password.html')

@app.route('/forgot_username', methods=['GET', 'POST'])
def forgot_username():
    if request.method == 'POST':
        phone = request.form.get('phone')
        user = User.query.filter_by(phone=phone).first()
        if user:
            settings = SystemSettings.query.first()
            current_token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
            current_url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
            username_msg = f"أهلاً بك في راصد حراج 🛡️\n\nاسم المستخدم الخاص بك هو: *{user.username}*\n\nيمكنك الآن تسجيل الدخول بكل سهولة. إذا واجهت أي مشكلة، تواصل مع الدعم."
            if send_whatsapp(create_session(), current_token, phone, username_msg, url=current_url):
                flash('تم إرسال اسم المستخدم إلى رقم جوالك المسجل.', 'success')
            else:
                flash('تعذر إرسال اسم المستخدم حالياً، حاول مرة أخرى لاحقاً.', 'warning')
            return redirect(url_for('login'))
        else:
            flash('رقم الجوال غير مسجل بالنظام!', 'danger')
    return render_template('forgot_username.html')

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    if 'reset_phone' not in session: return redirect(url_for('forgot_password'))
    if request.method == 'POST':
        if request.form.get('otp') == session.get('reset_otp'):
            user = User.query.filter_by(phone=session['reset_phone']).first()
            user.password = generate_password_hash(request.form.get('new_password'), method='pbkdf2:sha256')
            db.session.commit()
            session.pop('reset_phone', None)
            session.pop('reset_otp', None)
            log_audit_async(user.id, 'password_reset', {'نوع الحدث': 'استعادة كلمة المرور'}, request.remote_addr)
            flash('تم تغيير كلمة المرور بنجاح! يمكنك الدخول الآن.', 'success')
            return redirect(url_for('login'))
        flash('كود التحقق غير صحيح!', 'danger')
    return render_template('reset_password.html')

@app.route('/logout')
@login_required
def logout():
    log_audit_async(current_user.id, 'logout', {'نوع الحدث': 'تسجيل خروج'}, request.remote_addr)
    logout_user()
    return redirect(url_for('index'))

@app.route('/user_profile', methods=['GET', 'POST'])
@login_required
def user_profile():
    if request.method == 'POST':
        old_password = request.form.get('old_password')
        new_password = request.form.get('new_password')
        if check_password_hash(current_user.password, old_password):
            current_user.password = generate_password_hash(new_password, method='pbkdf2:sha256')
            db.session.commit()
            log_audit_async(current_user.id, 'change_password', {'نوع الحدث': 'تغيير كلمة المرور'}, request.remote_addr)
            flash('تم تغيير كلمة المرور بنجاح! 🔒', 'success')
            return redirect(url_for('user_profile'))
        else:
            flash('كلمة المرور الحالية غير صحيحة.', 'danger')
    return render_template('user_profile.html')

@app.route('/user_dashboard', methods=['GET', 'POST'])
@login_required
def user_dashboard():
    if current_user.role == 'admin' and 'admin_impersonating' not in session:
        return redirect(url_for('admin_dashboard'))
    
    # السماح بالدخول حتى لو كان الاشتراك منتهياً، لكن نمنع التفعيل لاحقاً
    sub = Subscription.query.filter_by(user_id=current_user.id).first()
    logs = AdLog.query.filter_by(user_id=current_user.id).order_by(AdLog.timestamp.desc()).limit(100).all()

    is_expired = False
    if current_user.account_expiration and datetime.datetime.now() > current_user.account_expiration:
        is_expired = True

    if request.method == 'POST':
        # السماح بتعديل الإعدادات حتى لو كان منتهياً، لكن لا نسمح بتفعيل الاشتراك
        name = request.form.get('name')
        keywords = request.form.get('keywords')
        cities = request.form.get('cities', '')
        city_filter_enabled = 'city_filter_enabled' in request.form
        excluded_words = request.form.get('excluded_words', '')
        exclude_enabled = 'exclude_enabled' in request.form
        quiet_enabled = 'quiet_enabled' in request.form
        q_sh = int(request.form.get('q_sh', 1))
        q_eh = int(request.form.get('q_eh', 6))
        end_time = current_user.account_expiration.isoformat() if current_user.account_expiration else ""
        
        # تجهيز تفاصيل سجل التدقيق
        old_kw = sub.keywords if sub else ''
        new_kw = keywords
        audit_details = {
            'نوع التغيير': 'تعديل إعدادات الرادار',
            'الكلمات قبل': old_kw if old_kw else 'لا يوجد',
            'الكلمات بعد': new_kw,
            'المدن': cities if cities else 'كل المدن',
            'الكلمات المحظورة': excluded_words if excluded_words else 'لا يوجد',
            'وقت الهدوء': 'مفعل' if quiet_enabled else 'غير مفعل'
        }
        
        settings = SystemSettings.query.first()
        current_token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
        current_url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"

        if sub:
            # إذا كان الاشتراك منتهياً، نجبر الحالة على 'paused' بغض النظر عن القيمة المرسلة
            new_status = 'active'
            if is_expired:
                new_status = 'paused'
            else:
                new_status = sub.status  # الإبقاء على الحالة الحالية (لن تتغير من النموذج)

            if sub.id in ACTIVE_THREADS:
                ACTIVE_THREADS[sub.id].stop()
                del ACTIVE_THREADS[sub.id]
            
            queue_file = SUBS_BASE_DIR / f"queue_{sub.id}.json"
            if queue_file.exists():
                queue_file.unlink()
            
            sub.name = name
            sub.keywords = keywords
            sub.cities = cities
            sub.city_filter_enabled = city_filter_enabled
            sub.excluded_words = excluded_words
            sub.exclude_enabled = exclude_enabled
            sub.quiet_enabled = quiet_enabled
            sub.quiet_start_hour = q_sh
            sub.quiet_start_minute = 0
            sub.quiet_end_hour = q_eh
            sub.quiet_end_minute = 0
            sub.end_ts = end_time
            sub.status = new_status
            db.session.commit()
            
            if not is_expired and new_status == 'active':
                start_thread_for_sub(sub)
            
            log_audit_async(current_user.id, 'update_subscription', audit_details, request.remote_addr)
            flash('تم تعديل الاشتراك وتحديث الرصد!', 'success')
        else:
            # إنشاء اشتراك جديد (نفس المنطق: إذا كان منتهياً يكون paused)
            initial_status = 'paused' if is_expired else 'active'
            new_sub = Subscription(
                user_id=current_user.id,
                name=name,
                keywords=keywords,
                recipients=current_user.phone,
                cities=cities,
                city_filter_enabled=city_filter_enabled,
                excluded_words=excluded_words,
                exclude_enabled=exclude_enabled,
                quiet_enabled=quiet_enabled,
                quiet_start_hour=q_sh,
                quiet_start_minute=0,
                quiet_end_hour=q_eh,
                quiet_end_minute=0,
                sleep_minutes=15,
                end_ts=end_time,
                status=initial_status
            )
            db.session.add(new_sub)
            db.session.commit()
            if not is_expired:
                start_thread_for_sub(new_sub, is_new=True)
            
            exp_text = current_user.account_expiration.strftime('%Y-%m-%d') if current_user.account_expiration else "مفتوح"
            welcome_msg = f"مرحباً بك في راصد حراج! 🎯\nتم تفعيل الرادار الخاص بك بنجاح.\n\nالاسم: {name}\nتاريخ الانتهاء: {exp_text}\n\nنتمنى لك صيدات موفقة! 🚀"
            if not is_expired:
                send_whatsapp(create_session(), current_token, current_user.phone, welcome_msg, url=current_url)

            log_audit_async(current_user.id, 'create_subscription', audit_details, request.remote_addr)
            flash('تم حفظ الاشتراك وبدأ الرصد!' if not is_expired else 'تم حفظ الاشتراك. لا يمكن تشغيل الرادار حتى يتم تجديد الاشتراك.', 'success')
            
        return redirect(url_for('user_dashboard'))
        
    return render_template('user.html', sub=sub, logs=logs, is_expired=is_expired)

# ================= مسار تجديد الاشتراك =================
@app.route('/renew_subscription', methods=['GET', 'POST'])
@login_required
def renew_subscription():
    if current_user.role == 'admin':
        flash('حساب الإدارة ليس له تجديد.', 'info')
        return redirect(url_for('admin_dashboard'))
    
    settings = SystemSettings.query.first()
    week_price = settings.subscription_week_price if settings else 5
    
    # منع تقديم طلب جديد إذا كان هناك طلب معلق
    pending_req = RenewalRequest.query.filter_by(user_id=current_user.id, status='pending').first()
    if pending_req:
        flash('لديك طلب تجديد قيد المراجعة بالفعل. سنقوم بتفعيل اشتراكك فور التحقق من الحوالة.', 'info')
        return render_template('renew_pending.html')
    
    if request.method == 'POST':
        try:
            weeks = int(request.form.get('weeks', 0))
            if weeks <= 0:
                flash('الرجاء اختيار عدد أسابيع صحيح.', 'danger')
                return redirect(url_for('renew_subscription'))
        except:
            flash('عدد الأسابيع غير صالح.', 'danger')
            return redirect(url_for('renew_subscription'))
        
        amount = weeks * week_price
        
        # معالجة رفع الصورة (اختياري)
        proof_filename = None
        if 'payment_proof' in request.files:
            file = request.files['payment_proof']
            if file and file.filename != '' and allowed_file(file.filename):
                # إنشاء اسم فريد للملف
                ext = file.filename.rsplit('.', 1)[1].lower()
                filename = f"{uuid.uuid4().hex}_{current_user.id}.{ext}"
                filepath = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                file.save(filepath)
                proof_filename = filename
        
        # إنشاء طلب التجديد
        new_req = RenewalRequest(
            user_id=current_user.id,
            weeks=weeks,
            amount=amount,
            status='pending',
            proof_filename=proof_filename
        )
        db.session.add(new_req)
        db.session.commit()
        
        # إرسال إشعار للإدارة
        settings = SystemSettings.query.first()
        token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
        url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
        notify = AdminNotifySettings.query.first()
        if notify and notify.admin_phone:
            admin_msg = f"🔔 طلب تجديد جديد:\n👤 المستخدم: {current_user.username}\n📱 الجوال: {current_user.phone}\n📆 عدد الأسابيع: {weeks}\n💰 المبلغ: {amount} ريال\n📎 إثبات: {'مرفق' if proof_filename else 'غير مرفق'}"
            send_whatsapp(create_session(), token, notify.admin_phone, admin_msg, url=url)
        
        log_audit_async(current_user.id, 'renewal_request', {'نوع الحدث': 'طلب تجديد', 'الأسابيع': weeks, 'المبلغ': amount}, request.remote_addr)
        flash('تم استلام طلب التجديد بنجاح. سنقوم بمراجعة الحوالة وتفعيل اشتراكك قريباً.', 'success')
        return render_template('renew_pending.html')
    
    # GET: عرض صفحة التجديد
    return render_template('renew.html', settings=settings, week_price=week_price)

# ================= مسارات إدارة طلبات التجديد (للأدمن) =================
@app.route('/admin/renewal_requests')
@login_required
def admin_renewal_requests():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    requests_list = RenewalRequest.query.order_by(RenewalRequest.created_at.desc()).all()
    pending_count = RenewalRequest.query.filter_by(status='pending').count()
    return render_template('admin_renewals.html', requests=requests_list, pending_count=pending_count)

@app.route('/admin/process_renewal/<int:req_id>/<action>')
@login_required
def process_renewal(req_id, action):
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    req = RenewalRequest.query.get_or_404(req_id)
    if req.status != 'pending':
        flash('تم معالجة هذا الطلب مسبقاً.', 'warning')
        return redirect(url_for('admin_renewal_requests'))
    
    user = User.query.get(req.user_id)
    settings = SystemSettings.query.first()
    token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
    url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
    
    if action == 'approve':
        # تحديث تاريخ انتهاء الاشتراك
        if user.account_expiration and user.account_expiration > datetime.datetime.now():
            # إذا كان الاشتراك لا يزال سارياً، نضيف المدة
            user.account_expiration = user.account_expiration + datetime.timedelta(weeks=req.weeks)
        else:
            # إذا كان منتهياً، نبدأ من الآن
            user.account_expiration = datetime.datetime.now() + datetime.timedelta(weeks=req.weeks)
        
        req.status = 'approved'
        req.processed_at = datetime.datetime.utcnow()
        db.session.commit()
        
        # إذا كان الاشتراك موجوداً وموقوفاً، نعيد تشغيله
        if user.subscription:
            if user.subscription.status != 'active':
                user.subscription.status = 'active'
                db.session.commit()
            # إعادة تشغيل الخيط إذا لم يكن نشطاً
            if user.subscription.id not in ACTIVE_THREADS:
                start_thread_for_sub(user.subscription)
            elif not ACTIVE_THREADS[user.subscription.id].is_alive():
                # إذا كان الخيط ميتاً، نعيد تشغيله
                try:
                    ACTIVE_THREADS[user.subscription.id].stop()
                except:
                    pass
                del ACTIVE_THREADS[user.subscription.id]
                start_thread_for_sub(user.subscription)
        
        # حذف ملف الصورة بعد المعالجة (إذا وجد)
        if req.proof_filename:
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], req.proof_filename)
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except:
                    pass
        
        # إرسال إشعار للمستخدم
        exp_date_str = user.account_expiration.strftime('%Y-%m-%d')
        user_msg = f"🎉 مبروك! تم تجديد اشتراكك في راصد حراج بنجاح.\n\n📆 المدة: {req.weeks} أسابيع\n📅 تاريخ الانتهاء الجديد: {exp_date_str}\n\nرادارك نشط الآن، نتمنى لك صيدات موفقة! 🚀"
        send_whatsapp(create_session(), token, user.phone, user_msg, url=url)
        
        log_audit_async(current_user.id, 'approve_renewal', {'نوع الحدث': 'قبول طلب تجديد', 'المستخدم': user.username, 'الأسابيع': req.weeks}, request.remote_addr)
        flash(f'تمت الموافقة على طلب {user.username} وتمديد الاشتراك.', 'success')
    
    elif action == 'reject':
        req.status = 'rejected'
        req.processed_at = datetime.datetime.utcnow()
        db.session.commit()
        
        # حذف ملف الصورة بعد الرفض
        if req.proof_filename:
            filepath = os.path.join(app.config['UPLOAD_FOLDER'], req.proof_filename)
            if os.path.exists(filepath):
                try:
                    os.remove(filepath)
                except:
                    pass
        
        log_audit_async(current_user.id, 'reject_renewal', {'نوع الحدث': 'رفض طلب تجديد', 'المستخدم': user.username}, request.remote_addr)
        flash(f'تم رفض طلب {user.username}.', 'warning')
    
    else:
        flash('إجراء غير معروف.', 'danger')
    
    return redirect(url_for('admin_renewal_requests'))

@app.route('/toggle_sub/<int:sub_id>')
@login_required
def toggle_sub(sub_id):
    sub = Subscription.query.get_or_404(sub_id)
    if sub.user_id == current_user.id or current_user.role == 'admin':
        if sub.status == 'active':
            sub.status = 'paused'
            if sub.id in ACTIVE_THREADS:
                ACTIVE_THREADS[sub.id].stop()
                del ACTIVE_THREADS[sub.id]
            log_audit_async(current_user.id, 'pause_subscription', {'نوع الحدث': 'إيقاف الرادار', 'رقم الاشتراك': sub_id}, request.remote_addr)
            flash('تم إيقاف الاشتراك مؤقتاً ⏸', 'warning')
        else:
            user_owner = User.query.get(sub.user_id)
            # منع التفعيل إذا كان الحساب منتهياً ولم يكن الفاعل أدمن (باستثناء إذا كان الأدمن منتحلاً؟)
            if user_owner.account_expiration and datetime.datetime.now() > user_owner.account_expiration:
                # نسمح للأدمن بالتجاوز
                if current_user.role != 'admin':
                    flash('لا يمكن تفعيل الاشتراك لأن الحساب منتهي الصلاحية. يرجى تجديد الاشتراك.', 'danger')
                    return redirect(request.referrer)
            sub.status = 'active'
            start_thread_for_sub(sub)
            log_audit_async(current_user.id, 'resume_subscription', {'نوع الحدث': 'استئناف الرادار', 'رقم الاشتراك': sub_id}, request.remote_addr)
            flash('تم استئناف الاشتراك بنجاح ▶️', 'success')
        db.session.commit()
    return redirect(request.referrer)

@app.route('/delete_sub/<int:sub_id>')
@login_required
def delete_sub(sub_id):
    sub = Subscription.query.get_or_404(sub_id)
    if sub.user_id == current_user.id or current_user.role == 'admin':
        if sub.id in ACTIVE_THREADS:
            ACTIVE_THREADS[sub.id].stop()
            del ACTIVE_THREADS[sub.id]
        db.session.delete(sub)
        db.session.commit()
        log_audit_async(current_user.id, 'delete_subscription', {'نوع الحدث': 'حذف الاشتراك', 'رقم الاشتراك': sub_id}, request.remote_addr)
        flash('تم حذف الاشتراك نهائياً 🗑️', 'info')
    return redirect(request.referrer)

@app.route('/admin_update_sleep/<int:sub_id>', methods=['POST'])
@login_required
def admin_update_sleep(sub_id):
    if current_user.role != 'admin': return redirect(url_for('index'))
    sub = Subscription.query.get_or_404(sub_id)
    new_sleep = request.form.get('sleep_minutes', type=int)
    
    if new_sleep and new_sleep > 0:
        old_sleep = sub.sleep_minutes
        sub.sleep_minutes = new_sleep
        db.session.commit()
        
        if sub.status == 'active':
            if sub.id in ACTIVE_THREADS:
                ACTIVE_THREADS[sub.id].stop()
                del ACTIVE_THREADS[sub.id]
            start_thread_for_sub(sub)
        
        log_audit_async(current_user.id, 'update_sleep', {'نوع الحدث': 'تغيير سرعة الفحص', 'رقم الاشتراك': sub_id, 'القديم': old_sleep, 'الجديد': new_sleep}, request.remote_addr)
        flash('تم تحديث مدة الفحص (الدورة) للعميل بنجاح.', 'success')
        
    return redirect(request.referrer)

# ================= مسارات الإدارة (الجديدة) =================
@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    
    total_users = User.query.count()
    active_users = User.query.filter_by(is_active_account=True).count()
    inactive_users = total_users - active_users
    pending_renewals_count = RenewalRequest.query.filter_by(status='pending').count()

    seven_days_ago = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    recent_logs = AdLog.query.filter(AdLog.timestamp >= seven_days_ago).all()
    
    daily_ads = {}
    for i in range(6, -1, -1):
        day = (datetime.datetime.utcnow() - datetime.timedelta(days=i)).strftime('%m-%d')
        daily_ads[day] = 0

    for log in recent_logs:
        day = log.timestamp.strftime('%m-%d')
        if day in daily_ads:
            daily_ads[day] += 1

    chart_labels = list(daily_ads.keys())
    chart_data = list(daily_ads.values())
    
    return render_template('admin_dashboard.html',
                           total_users=total_users, active_users=active_users, inactive_users=inactive_users,
                           active_threads=ACTIVE_THREADS,
                           pending_renewals_count=pending_renewals_count,
                           chart_labels=chart_labels, chart_data=chart_data)

@app.route('/admin/users')
@login_required
def admin_users():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    
    # معاملات البحث والتصفية
    search = request.args.get('search', '').strip()
    filter_status = request.args.get('filter', 'all')
    page = request.args.get('page', 1, type=int)
    per_page = 20
    
    query = User.query.outerjoin(Subscription)
    
    if search:
        # البحث في اسم المستخدم، رقم الجوال، أو اسم الاشتراك
        query = query.filter(
            db.or_(
                User.username.ilike(f'%{search}%'),
                User.phone.ilike(f'%{search}%'),
                Subscription.name.ilike(f'%{search}%')
            )
        )
    
    if filter_status == 'active':
        query = query.filter(User.is_active_account == True)
    elif filter_status == 'inactive':
        query = query.filter(User.is_active_account == False)
    elif filter_status == 'expired':
        query = query.filter(User.account_expiration.isnot(None), User.account_expiration < datetime.datetime.now())
    
    users_paginated = query.order_by(User.id.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    return render_template('admin_users.html', users=users_paginated, search=search, filter_status=filter_status)

@app.route('/admin/ads_log')
@login_required
def admin_ads_log():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    
    search = request.args.get('search', '').strip()
    page = request.args.get('page', 1, type=int)
    per_page = 50
    
    query = db.session.query(AdLog, User).join(User, AdLog.user_id == User.id)
    
    if search:
        query = query.filter(
            db.or_(
                User.username.ilike(f'%{search}%'),
                AdLog.keyword_matched.ilike(f'%{search}%'),
                AdLog.title.ilike(f'%{search}%')
            )
        )
    
    logs_paginated = query.order_by(AdLog.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    return render_template('admin_ads_log.html', logs=logs_paginated, search=search)

@app.route('/admin/audit_log')
@login_required
def admin_audit_log():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    
    search = request.args.get('search', '').strip()
    action_filter = request.args.get('action', '')
    page = request.args.get('page', 1, type=int)
    per_page = 30
    
    query = AuditLog.query.outerjoin(User, AuditLog.user_id == User.id)
    
    if search:
        query = query.filter(
            db.or_(
                User.username.ilike(f'%{search}%'),
                AuditLog.action.ilike(f'%{search}%'),
                AuditLog.ip_address.ilike(f'%{search}%')
            )
        )
    
    if action_filter:
        query = query.filter(AuditLog.action == action_filter)
    
    logs_paginated = query.order_by(AuditLog.timestamp.desc()).paginate(page=page, per_page=per_page, error_out=False)
    
    # قائمة بأنواع الإجراءات الموجودة لفلتر
    actions = db.session.query(AuditLog.action).distinct().all()
    actions = [a[0] for a in actions]
    
    return render_template('admin_audit_log.html', logs=logs_paginated, search=search, action_filter=action_filter, actions=actions)

@app.route('/admin/clear_audit_log')
@login_required
def admin_clear_audit_log():
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    try:
        num_deleted = AuditLog.query.delete()
        db.session.commit()
        log_audit_async(current_user.id, 'clear_audit_log', {'نوع الحدث': 'حذف سجل التدقيق', 'العدد': num_deleted}, request.remote_addr)
        flash(f'✅ تم حذف {num_deleted} سجل تدقيق بنجاح.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'❌ حدث خطأ أثناء حذف سجل التدقيق: {str(e)}', 'danger')
    
    return redirect(url_for('admin_audit_log'))

@app.route('/admin_statistics')
@login_required
def admin_statistics():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    daily_stats = get_daily_stats()
    total_users = User.query.count()
    active_subs = Subscription.query.filter_by(status='active').count()
    total_ads_logged = AdLog.query.count()
    return render_template('admin_statistics.html', daily_stats=daily_stats, total_users=total_users, active_subs=active_subs, total_ads_logged=total_ads_logged)

@app.route('/admin_settings', methods=['GET', 'POST'])
@login_required
def admin_settings():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    settings = SystemSettings.query.first()
    notify = AdminNotifySettings.query.first()
    
    if request.method == 'POST':
        settings.whatsapp_token = request.form.get('whatsapp_token')
        settings.trial_days = int(request.form.get('trial_days', 2))
        # إعدادات البنك
        settings.bank_account_number = request.form.get('bank_account_number', '')
        settings.bank_account_name = request.form.get('bank_account_name', '')
        settings.bank_qr_text = request.form.get('bank_qr_text', '')
        settings.subscription_week_price = int(request.form.get('subscription_week_price', 5))
        
        # إعدادات البوابات المتعددة
        settings.active_gateway = request.form.get('active_gateway', '1')
        settings.gateway_1_name = request.form.get('gateway_1_name', 'البوابة الأولى')
        settings.gateway_2_name = request.form.get('gateway_2_name', 'البوابة الثانية')
        settings.gateway_url_1 = request.form.get('gateway_url_1', 'http://127.0.0.1:3000/api/v1/send')
        settings.gateway_url_2 = request.form.get('gateway_url_2', 'https://whatsapp.tkwin.com.sa/api/v1/send')
        settings.whatsapp_token_2 = request.form.get('whatsapp_token_2', '')
        
        if notify:
            notify.admin_phone = request.form.get('admin_phone', '')
            
        db.session.commit()
        log_audit_async(current_user.id, 'update_settings', {'نوع الحدث': 'تحديث إعدادات النظام'}, request.remote_addr)
        flash('تم حفظ إعدادات النظام بنجاح ⚙️', 'success')
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
        exp_date_str = request.form.get('account_expiration')

        sub_name = request.form.get('name')
        keywords = request.form.get('keywords')
        cities = request.form.get('cities', '')
        city_filter_enabled = 'city_filter_enabled' in request.form
        excluded_words = request.form.get('excluded_words', '')
        exclude_enabled = 'exclude_enabled' in request.form
        quiet_enabled = 'quiet_enabled' in request.form
        q_sh = int(request.form.get('q_sh', 1))
        q_eh = int(request.form.get('q_eh', 6))

        if User.query.filter_by(username=username).first() or User.query.filter_by(phone=phone).first():
            flash('اسم المستخدم أو رقم الجوال مسجل مسبقاً!', 'danger')
            return redirect(url_for('admin_add_user'))

        exp_date = datetime.datetime.strptime(exp_date_str, '%Y-%m-%d') if exp_date_str else None

        new_user = User(
            username=username, phone=phone,
            password=generate_password_hash(password, method='pbkdf2:sha256'),
            account_expiration=exp_date
        )
        db.session.add(new_user)
        db.session.commit()

        settings = SystemSettings.query.first()
        current_token = settings.active_whatsapp_token if settings else "sau11zbtz1ruma8o2k5tt"
        current_url = settings.active_whatsapp_url if settings else "http://127.0.0.1:3000/api/v1/send"
        exp_text = exp_date.strftime('%Y-%m-%d') if exp_date else "مفتوح"
        welcome_msg = f"مرحباً بك في راصد حراج! 🎯\nتم إنشاء حسابك وتفعيل الرادار بنجاح من قبل الإدارة.\n\nتاريخ الانتهاء: {exp_text}\n\nنتمنى لك صيدات موفقة! 🚀"
        send_whatsapp(create_session(), current_token, phone, welcome_msg, url=current_url)

        if keywords:
            end_ts = exp_date.isoformat() if exp_date else ""
            new_sub = Subscription(
                user_id=new_user.id, name=sub_name or "اشتراك جديد", keywords=keywords, recipients=phone,
                cities=cities, city_filter_enabled=city_filter_enabled,
                excluded_words=excluded_words, exclude_enabled=exclude_enabled,
                quiet_enabled=quiet_enabled, quiet_start_hour=q_sh, quiet_start_minute=0, quiet_end_hour=q_eh, quiet_end_minute=0,
                sleep_minutes=15, end_ts=end_ts
            )
            db.session.add(new_sub)
            db.session.commit()
            
            if not exp_date or exp_date > datetime.datetime.now():
                start_thread_for_sub(new_sub, is_new=True)

        log_audit_async(current_user.id, 'admin_add_user', {'نوع الحدث': 'إضافة عميل من الإدارة', 'المستخدم': username, 'الجوال': phone}, request.remote_addr)
        flash('تم إضافة العميل وإعداد راداره بنجاح! 🚀', 'success')
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
        new_pass = request.form.get('password')
        exp_date_str = request.form.get('account_expiration')
        
        if new_pass:
            user.password = generate_password_hash(new_pass, method='pbkdf2:sha256')
            
        if exp_date_str:
            user.account_expiration = datetime.datetime.strptime(exp_date_str, '%Y-%m-%d')
        else:
            user.account_expiration = None 
        
        if user.subscription:
            user.subscription.recipients = user.phone
            user.subscription.end_ts = user.account_expiration.isoformat() if user.account_expiration else ""
            
        db.session.commit()
        log_audit_async(current_user.id, 'admin_edit_user', {'نوع الحدث': 'تعديل عميل من الإدارة', 'رقم المستخدم': user_id, 'المستخدم': user.username}, request.remote_addr)
        flash(f'تم تعديل بيانات العميل {user.username} بنجاح.', 'success')
        return redirect(url_for('admin_users'))
    return render_template('admin_edit_user.html', user=user)

@app.route('/toggle_user/<int:user_id>')
@login_required
def toggle_user(user_id):
    if current_user.role == 'admin':
        user = User.query.get_or_404(user_id)
        if user.id != current_user.id:
            user.is_active_account = not user.is_active_account
            if not user.is_active_account and user.subscription:
                sub_id = user.subscription.id
                if sub_id in ACTIVE_THREADS:
                    ACTIVE_THREADS[sub_id].stop()
                    del ACTIVE_THREADS[sub_id]
            db.session.commit()
            action = 'تعطيل' if not user.is_active_account else 'تفعيل'
            log_audit_async(current_user.id, f'admin_{action}_user', {'نوع الحدث': f'{action} حساب مستخدم', 'المستخدم': user.username}, request.remote_addr)
    return redirect(request.referrer)

@app.route('/admin_toggle_sub/<int:sub_id>')
@login_required
def admin_toggle_sub(sub_id):
    if current_user.role != 'admin': return redirect(url_for('index'))
    sub = Subscription.query.get_or_404(sub_id)
    if sub.status == 'active':
        sub.status = 'paused'
        if sub.id in ACTIVE_THREADS:
            ACTIVE_THREADS[sub.id].stop()
            del ACTIVE_THREADS[sub.id]
        log_audit_async(current_user.id, 'admin_pause_sub', {'نوع الحدث': 'إيقاف رادار من الإدارة', 'رقم الاشتراك': sub_id}, request.remote_addr)
        flash('تم إيقاف اشتراك العميل بنجاح.', 'warning')
    else:
        user_owner = User.query.get(sub.user_id)
        if user_owner.account_expiration and datetime.datetime.now() > user_owner.account_expiration:
            flash('لا يمكن استئناف اشتراك العميل لأن حسابه منتهي الصلاحية!', 'danger')
        else:
            sub.status = 'active'
            start_thread_for_sub(sub)
            log_audit_async(current_user.id, 'admin_resume_sub', {'نوع الحدث': 'استئناف رادار من الإدارة', 'رقم الاشتراك': sub_id}, request.remote_addr)
            flash('تم استئناف اشتراك العميل بنجاح.', 'success')
    db.session.commit()
    return redirect(request.referrer)

@app.route('/impersonate/<int:user_id>')
@login_required
def impersonate(user_id):
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    user = User.query.get_or_404(user_id)
    session['admin_impersonating'] = current_user.id
    login_user(user)
    log_audit_async(current_user.id, 'impersonate', {'نوع الحدث': 'انتحال حساب', 'الحساب الهدف': user.username}, request.remote_addr)
    flash(f'أنت الآن تتصفح وتتحكم بحساب العميل: {user.username}', 'warning')
    return redirect(url_for('user_dashboard'))

@app.route('/revert_impersonate')
@login_required
def revert_impersonate():
    if 'admin_impersonating' in session:
        admin_user = User.query.get(session['admin_impersonating'])
        if admin_user:
            login_user(admin_user)
            session.pop('admin_impersonating', None)
            log_audit_async(admin_user.id, 'revert_impersonate', {'نوع الحدث': 'عودة من انتحال حساب'}, request.remote_addr)
            flash('تمت العودة لحساب الإدارة بنجاح.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/admin_delete_user/<int:user_id>')
@login_required
def admin_delete_user(user_id):
    if current_user.role != 'admin':
        return redirect(url_for('index'))
    
    user = User.query.get_or_404(user_id)
    
    if user.id == current_user.id:
        flash('لا يمكنك حذف حسابك الخاص!', 'danger')
        return redirect(url_for('admin_users'))
    
    try:
        if user.subscription and user.subscription.id in ACTIVE_THREADS:
            ACTIVE_THREADS[user.subscription.id].stop()
            del ACTIVE_THREADS[user.subscription.id]
        
        if user.subscription:
            sub_id = user.subscription.id
            seen_file = SUBS_BASE_DIR / f"seen_{sub_id}.json"
            queue_file = SUBS_BASE_DIR / f"queue_{sub_id}.json"
            if seen_file.exists():
                seen_file.unlink()
            if queue_file.exists():
                queue_file.unlink()
        
        AdLog.query.filter_by(user_id=user.id).delete()
        
        if user.subscription:
            db.session.delete(user.subscription)
        
        db.session.delete(user)
        db.session.commit()
        
        log_audit_async(current_user.id, 'admin_delete_user', {'نوع الحدث': 'حذف عميل من الإدارة', 'المستخدم': user.username}, request.remote_addr)
        flash(f'تم حذف المستخدم {user.username} وجميع بياناته بنجاح.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'حدث خطأ أثناء الحذف: {str(e)}', 'danger')
    
    return redirect(url_for('admin_users'))

import traceback

try:
    with app.app_context():
        db.create_all()
        
        # === ترحيل آمن: إضافة أعمدة system_settings إذا لم تكن موجودة ===
        try:
            from sqlalchemy import inspect
            inspector = inspect(db.engine)
            if 'system_settings' in inspector.get_table_names():
                existing_columns = {col['name'] for col in inspector.get_columns('system_settings')}
                
                columns_to_add = [
                    ("bank_account_number", "VARCHAR(50) DEFAULT ''"),
                    ("bank_account_name", "VARCHAR(100) DEFAULT ''"),
                    ("bank_qr_text", "TEXT DEFAULT ''"),
                    ("subscription_week_price", "INTEGER DEFAULT 5"),
                    ("active_gateway", "VARCHAR(10) DEFAULT '1'"),
                    ("gateway_1_name", "VARCHAR(100) DEFAULT 'البوابة الأولى'"),
                    ("gateway_2_name", "VARCHAR(100) DEFAULT 'البوابة الثانية'"),
                    ("gateway_url_1", "VARCHAR(255) DEFAULT 'http://127.0.0.1:3000/api/v1/send'"),
                    ("gateway_url_2", "VARCHAR(255) DEFAULT 'https://whatsapp.tkwin.com.sa/api/v1/send'"),
                    ("whatsapp_token_2", "VARCHAR(255) DEFAULT '7a203d6ba6f4325ed3261ea87f6b2e751250ad97'")
                ]
                
                for col_name, col_type in columns_to_add:
                    if col_name not in existing_columns:
                        try:
                            db.session.execute(db.text(f"ALTER TABLE system_settings ADD COLUMN {col_name} {col_type}"))
                            db.session.commit()
                            logger.info(f"✅ تم إضافة العمود {col_name} بنجاح.")
                        except Exception as alter_err:
                            db.session.rollback()
                            logger.error(f"❌ تعذر إضافة العمود {col_name}: {alter_err}")
        except Exception as e:
            logger.error(f"❌ خطأ أثناء الترحيل: {e}")

        db.create_all()
        
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
except Exception as startup_err:
    logger.error(f"❌ خطأ فادح أثناء تشغيل/بدء التطبيق: {startup_err}")
    logger.error(traceback.format_exc())
    raise startup_err

# ================= SEO: robots.txt و sitemap.xml =================
@app.route('/robots.txt')
def robots_txt():
    robots_content = """User-agent: *
Allow: /
Disallow: /admin
Disallow: /user_dashboard
Disallow: /login
Disallow: /register
Disallow: /verify
Disallow: /forgot_password
Disallow: /forgot_username
Disallow: /reset_password
Disallow: /user_profile
Disallow: /toggle_sub
Disallow: /delete_sub
Disallow: /impersonate
Disallow: /revert_impersonate

Sitemap: https://haraj-saas.duckdns.org/sitemap.xml
"""
    return robots_content, 200, {'Content-Type': 'text/plain; charset=utf-8'}

@app.route('/sitemap.xml')
def sitemap_xml():
    sitemap_content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url>
    <loc>https://haraj-saas.duckdns.org/</loc>
    <lastmod>2026-06-03</lastmod>
    <changefreq>weekly</changefreq>
    <priority>1.0</priority>
  </url>
  <url>
    <loc>https://haraj-saas.duckdns.org/login</loc>
    <lastmod>2026-06-03</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.8</priority>
  </url>
  <url>
    <loc>https://haraj-saas.duckdns.org/register</loc>
    <lastmod>2026-06-03</lastmod>
    <changefreq>monthly</changefreq>
    <priority>0.9</priority>
  </url>
</urlset>"""
    return sitemap_content, 200, {'Content-Type': 'application/xml; charset=utf-8'}

if __name__ == '__main__':
    with app.app_context():
        for sub in Subscription.query.filter_by(status='active').all():
            if sub.owner.is_active_account and (not sub.owner.account_expiration or sub.owner.account_expiration > datetime.datetime.now()):
                start_thread_for_sub(sub)
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
