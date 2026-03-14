# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, redirect, url_for, flash, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.security import generate_password_hash, check_password_hash
import json, re, time, threading, datetime, random, os
from pathlib import Path
from urllib.parse import urljoin, quote
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

app = Flask(__name__)
app.secret_key = "haraj_super_secret_key_v10"

app.jinja_env.globals.update(now=datetime.datetime.now)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + os.path.join(BASE_DIR, 'haraj.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

APP_BASE_DIR = Path(__file__).resolve().parent
SUBS_BASE_DIR = APP_BASE_DIR / "subs"
SUBS_BASE_DIR.mkdir(exist_ok=True)

HARAJ_BASE = "https://haraj.com.sa"
HARAJ_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "ar-SA"}
ACTIVE_THREADS = {} 

# ================= باتش منع السكون =================
def keep_alive_patch():
    while True:
        try:
            requests.get("https://haraj-saas.onrender.com/", timeout=10)
        except: pass
        time.sleep(600) 

threading.Thread(target=keep_alive_patch, daemon=True).start()

# ================= النماذج (قاعدة البيانات) =================
class SystemSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    whatsapp_token = db.Column(db.String(255), default="7a203d6ba6f4325ed3261ea87f6b2e751250ad97")
    trial_days = db.Column(db.Integer, default=2)

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

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ================= دوال المساعدة =================
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
            if normalize_text(neg) and re.search(r'(^|\s)' + re.escape(normalize_text(neg)) + r'($|\s)', nt): 
                return False
    kw_tokens = [t for t in normalize_text(kw).split() if t]
    if not kw_tokens: return True
    for token in kw_tokens:
        if not re.search(r'(^|\s)' + re.escape(token) + r'($|\s)', nt): return False
    return True

def is_target_city(full_text, cities_list, city_filter_enabled):
    if not city_filter_enabled or not cities_list: return True
    if not full_text: return False
    ft_lower = full_text.lower()
    for tc in cities_list:
        if tc.strip().lower() in ft_lower: return True
    return False

def is_quiet_now(enabled, sh, sm, eh, em):
    if not enabled: return False
    now = datetime.datetime.now()
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

def send_whatsapp(req_session, token, to_msisdn, text):
    url = "https://whatsapp.tkwin.com.sa/api/v1/send"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = req_session.post(url, json={"to": to_msisdn, "message": text}, headers=headers, timeout=20, verify=False)
        return 200 <= r.status_code < 300
    except:
        return False

# ================= خيط المراقبة (Thread) =================
class MonitorThread(threading.Thread):
    def __init__(self, sub_config):
        super().__init__(daemon=True)
        self.cfg = sub_config
        self.stop_evt = threading.Event()
        self.req_session = create_session()
        self.seen_file = SUBS_BASE_DIR / f"seen_{self.cfg['id']}.json"
        if self.seen_file.exists():
            with open(self.seen_file, 'r') as f: self.seen_ids = set(json.load(f))
        else:
            self.seen_ids = set()

    def run(self):
        while not self.stop_evt.is_set():
            with app.app_context():
                user = User.query.get(self.cfg['user_id'])
                sub = Subscription.query.get(self.cfg['id'])
                settings = SystemSettings.query.first()
                current_token = settings.whatsapp_token if settings else "7a203d6ba6f4325ed3261ea87f6b2e751250ad97"

                if not user or not user.is_active_account or not sub or sub.status != 'active' or (user.account_expiration and user.account_expiration < datetime.datetime.now()):
                    if sub: 
                        sub.status = 'paused'
                        db.session.commit()
                    break 
                
            if not is_quiet_now(self.cfg['quiet_enabled'], self.cfg['q_sh'], self.cfg['q_sm'], self.cfg['q_eh'], self.cfg['q_em']):
                for kw in self.cfg['keywords']:
                    if self.stop_evt.is_set(): break
                    
                    # فحص أول 3 صفحات كما هو بالمنطق الأساسي
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
                                        
                                        delay = random.uniform(30, 60)
                                        time.sleep(delay)
                                        
                                        msg = f"إعلان جديد ({kw}):\n{title}\n{ad_url}"
                                        if send_whatsapp(self.req_session, current_token, self.cfg['recipients'], msg):
                                            self.seen_ids.add(ad_id)
                                            with open(self.seen_file, 'w') as f: json.dump(list(self.seen_ids), f)
                                            
                                            with app.app_context():
                                                log_sub = Subscription.query.get(self.cfg['id'])
                                                if log_sub:
                                                    log_sub.sent_count += 1
                                                    new_log = AdLog(user_id=self.cfg['user_id'], title=title, url=ad_url, keyword_matched=kw)
                                                    db.session.add(new_log)
                                                    db.session.commit()
                        except:
                            pass
                        
                        # تأخير بسيط بين صفحات حراج عشان ما ننحظر
                        time.sleep(random.uniform(3, 7))
            
            sleep_seconds = self.cfg['sleep_minutes'] * 60
            for _ in range(sleep_seconds):
                if self.stop_evt.is_set(): break
                time.sleep(1)

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

# ================= المسارات (Routes) =================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard') if current_user.role == 'admin' else url_for('user_dashboard'))
    
    if request.method == 'POST':
        user = User.query.filter_by(username=request.form.get('username')).first()
        if user and check_password_hash(user.password, request.form.get('password')):
            if not user.is_active_account:
                flash('حسابك موقوف من قبل الإدارة.', 'danger')
                return redirect(url_for('login'))
            login_user(user)
            return redirect(url_for('admin_dashboard') if user.role == 'admin' else url_for('user_dashboard'))
        flash('بيانات الدخول غير صحيحة!', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        phone = request.form.get('phone')
        otp = str(random.randint(1000, 9999))
        session['temp_user'] = {
            'username': request.form.get('username'), 'phone': phone,
            'password': generate_password_hash(request.form.get('password'), method='pbkdf2:sha256')
        }
        session['otp'] = otp
        
        settings = SystemSettings.query.first()
        current_token = settings.whatsapp_token if settings else "7a203d6ba6f4325ed3261ea87f6b2e751250ad97"
        
        send_whatsapp(create_session(), current_token, phone, f"كود التحقق الخاص بك هو: {otp}")
        print(f"\n[ OTP CODE for {phone} ]: {otp} \n")
        return redirect(url_for('verify'))
    return render_template('register.html')

@app.route('/verify', methods=['GET', 'POST'])
def verify():
    if request.method == 'POST':
        if request.form.get('otp') == session.get('otp'):
            temp = session['temp_user']
            new_user = User(username=temp['username'], phone=temp['phone'], password=temp['password'])
            
            settings = SystemSettings.query.first()
            trial_days = settings.trial_days if settings else 2

            if User.query.count() == 0: 
                new_user.role = 'admin'
                new_user.account_expiration = None 
            else:
                new_user.account_expiration = datetime.datetime.now() + datetime.timedelta(days=trial_days)

            db.session.add(new_user)
            db.session.commit()
            
            login_user(new_user)
            session.pop('temp_user', None)
            session.pop('otp', None)
            
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
            current_token = settings.whatsapp_token if settings else "7a203d6ba6f4325ed3261ea87f6b2e751250ad97"
            send_whatsapp(create_session(), current_token, phone, f"كود استعادة كلمة المرور: {otp}")
            
            print(f"\n[ RESET OTP for {phone} ]: {otp} \n")
            return redirect(url_for('reset_password'))
        flash('رقم الجوال غير مسجل بالنظام!', 'danger')
    return render_template('forgot_password.html')

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
            flash('تم تغيير كلمة المرور بنجاح! يمكنك الدخول الآن.', 'success')
            return redirect(url_for('login'))
        flash('كود التحقق غير صحيح!', 'danger')
    return render_template('reset_password.html')

@app.route('/logout')
@login_required
def logout():
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
    
    sub = Subscription.query.filter_by(user_id=current_user.id).first()
    logs = AdLog.query.filter_by(user_id=current_user.id).order_by(AdLog.timestamp.desc()).limit(100).all()

    is_expired = False
    if current_user.account_expiration and datetime.datetime.now() > current_user.account_expiration:
        is_expired = True

    if request.method == 'POST':
        if is_expired:
            flash('عذراً، اشتراكك منتهي! لا يمكنك التعديل.', 'danger')
            return redirect(url_for('user_dashboard'))

        name = request.form.get('name')
        keywords = request.form.get('keywords')
        cities = request.form.get('cities', '')
        city_filter_enabled = 'city_filter_enabled' in request.form
        excluded_words = request.form.get('excluded_words', '')
        exclude_enabled = 'exclude_enabled' in request.form
        quiet_enabled = 'quiet_enabled' in request.form
        q_sh = int(request.form.get('q_sh', 1))
        q_sm = int(request.form.get('q_sm', 0))
        q_eh = int(request.form.get('q_eh', 6))
        q_em = int(request.form.get('q_em', 0))
        end_time = current_user.account_expiration.isoformat() if current_user.account_expiration else ""
        
        if sub:
            if sub.id in ACTIVE_THREADS:
                ACTIVE_THREADS[sub.id].stop()
                del ACTIVE_THREADS[sub.id]
            sub.name = name; sub.keywords = keywords; sub.cities = cities
            sub.city_filter_enabled = city_filter_enabled; sub.excluded_words = excluded_words
            sub.exclude_enabled = exclude_enabled; sub.quiet_enabled = quiet_enabled
            sub.quiet_start_hour = q_sh; sub.quiet_start_minute = q_sm
            sub.quiet_end_hour = q_eh; sub.quiet_end_minute = q_em
            sub.end_ts = end_time; sub.status = 'active'
            db.session.commit()
            start_thread_for_sub(sub)
            flash('تم تعديل الاشتراك وتحديث الرصد!', 'success')
        else:
            new_sub = Subscription(
                user_id=current_user.id, name=name, keywords=keywords, recipients=current_user.phone,
                cities=cities, city_filter_enabled=city_filter_enabled,
                excluded_words=excluded_words, exclude_enabled=exclud
