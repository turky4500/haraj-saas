# -*- coding: utf-8 -*-
from flask import Flask, render_template, request, redirect, url_for, flash
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
app.secret_key = "haraj_super_secret_key_v2"
# إعداد قاعدة البيانات
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///haraj.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db = SQLAlchemy(app)
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

# مسارات حفظ الملفات
APP_BASE_DIR = Path(__file__).resolve().parent
SUBS_BASE_DIR = APP_BASE_DIR / "subs"
SUBS_BASE_DIR.mkdir(exist_ok=True)

# متغيرات عامة
DEFAULT_TOKEN = "5a4b25b5228bab88c0df7aac67a458b45e63442f"
HARAJ_BASE = "https://haraj.com.sa"
HARAJ_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Language": "ar-SA"}
ACTIVE_THREADS = {} # {sub_id: thread_object}

# ================= النماذج (قاعدة البيانات) =================
class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='user') # 'admin' or 'user'
    is_active_account = db.Column(db.Boolean, default=True)
    subscriptions = db.relationship('Subscription', backref='owner', lazy=True)

class Subscription(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    keywords = db.Column(db.String(500), nullable=False)
    cities = db.Column(db.String(500))
    recipients = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), default='active') # 'active' or 'stopped'
    sent_count = db.Column(db.Integer, default=0)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# ================= دوال المساعدة للرصد =================
def create_session():
    session = requests.Session()
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

def send_whatsapp(session, token, to_msisdn, text):
    url = "https://whatsapp.tkwin.com.sa/api/v1/send"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = session.post(url, json={"to": to_msisdn, "message": text}, headers=headers, timeout=20, verify=False)
        return 200 <= r.status_code < 300
    except:
        return False

# ================= خيط المراقبة (Thread) =================
class MonitorThread(threading.Thread):
    def __init__(self, sub_id, keywords, recipients):
        super().__init__(daemon=True)
        self.sub_id = sub_id
        self.keywords = [k.strip() for k in keywords.split(',') if k.strip()]
        self.recipients = recipients.split(',')[0].strip()
        self.stop_evt = threading.Event()
        self.session = create_session()
        self.seen_file = SUBS_BASE_DIR / f"seen_{self.sub_id}.json"
        
        if self.seen_file.exists():
            with open(self.seen_file, 'r') as f: self.seen_ids = set(json.load(f))
        else:
            self.seen_ids = set()

    def run(self):
        while not self.stop_evt.is_set():
            for kw in self.keywords:
                if self.stop_evt.is_set(): break
                url = f"{HARAJ_BASE}/search/{quote(kw, safe='')}/" if kw else f"{HARAJ_BASE}/"
                try:
                    html = self.session.get(url, headers=HARAJ_HEADERS, timeout=15, verify=False).content
                    for title, ad_url in extract_ads(html, HARAJ_BASE):
                        ad_id = re.search(r"/(\d+)(?:/|$)", ad_url).group(1)
                        if ad_id not in self.seen_ids:
                            msg = f"إعلان جديد ({kw}):\n{title}\n{ad_url}"
                            if send_whatsapp(self.session, DEFAULT_TOKEN, self.recipients, msg):
                                self.seen_ids.add(ad_id)
                                with open(self.seen_file, 'w') as f: json.dump(list(self.seen_ids), f)
                                
                                # تحديث العداد في قاعدة البيانات
                                with app.app_context():
                                    sub = Subscription.query.get(self.sub_id)
                                    if sub:
                                        sub.sent_count += 1
                                        db.session.commit()
                            time.sleep(random.uniform(5, 10))
                except:
                    pass
            time.sleep(120)

    def stop(self):
        self.stop_evt.set()

# ================= المسارات (Routes) =================

@app.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('admin_dashboard') if current_user.role == 'admin' else url_for('user_dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        user = User.query.filter_by(username=username).first()
        if user and check_password_hash(user.password, password):
            if not user.is_active_account:
                flash('حسابك موقوف من قبل الإدارة.', 'danger')
                return redirect(url_for('login'))
            login_user(user)
            return redirect(url_for('index'))
        flash('بيانات الدخول غير صحيحة!', 'danger')
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if User.query.filter_by(username=username).first():
            flash('اسم المستخدم موجود مسبقاً!', 'warning')
        else:
            new_user = User(username=username, password=generate_password_hash(password, method='pbkdf2:sha256'))
            # أول مستخدم يسجل يصير إدمن تلقائياً
            if User.query.count() == 0:
                new_user.role = 'admin'
            db.session.add(new_user)
            db.session.commit()
            flash('تم التسجيل بنجاح، يمكنك الدخول الآن.', 'success')
            return redirect(url_for('login'))
    return render_template('register.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/user_dashboard', methods=['GET', 'POST'])
@login_required
def user_dashboard():
    if current_user.role == 'admin': return redirect(url_for('admin_dashboard'))
    
    if request.method == 'POST':
        name = request.form.get('name')
        keywords = request.form.get('keywords')
        recipients = request.form.get('recipients')
        
        new_sub = Subscription(user_id=current_user.id, name=name, keywords=keywords, recipients=recipients)
        db.session.add(new_sub)
        db.session.commit()
        
        # تشغيل الخيط
        t = MonitorThread(new_sub.id, keywords, recipients)
        ACTIVE_THREADS[new_sub.id] = t
        t.start()
        
        flash('تمت إضافة الاشتراك وبدأ الرصد!', 'success')
        return redirect(url_for('user_dashboard'))
        
    subs = Subscription.query.filter_by(user_id=current_user.id).all()
    return render_template('user.html', subs=subs)

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
        flash('تم حذف الاشتراك بنجاح.', 'info')
    return redirect(request.referrer)

@app.route('/admin_dashboard')
@login_required
def admin_dashboard():
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    users = User.query.all()
    all_subs = Subscription.query.all()
    return render_template('admin.html', users=users, subs=all_subs, active_threads=ACTIVE_THREADS)

@app.route('/toggle_user/<int:user_id>')
@login_required
def toggle_user(user_id):
    if current_user.role != 'admin': return redirect(url_for('user_dashboard'))
    user = User.query.get_or_404(user_id)
    if user.id != current_user.id:
        user.is_active_account = not user.is_active_account
        # إذا تم إيقافه، نوقف كل اشتراكاته
        if not user.is_active_account:
            for sub in user.subscriptions:
                if sub.id in ACTIVE_THREADS:
                    ACTIVE_THREADS[sub.id].stop()
                    del ACTIVE_THREADS[sub.id]
        db.session.commit()
    return redirect(url_for('admin_dashboard'))

# إنشاء قاعدة البيانات عند أول تشغيل
with app.app_context():
    db.create_all()

if __name__ == '__main__':
    # إعادة تشغيل الاشتراكات النشطة عند إقلاع السيرفر
    with app.app_context():
        active_subs = Subscription.query.filter_by(status='active').all()
        for sub in active_subs:
            # نتأكد إن حساب راعي الاشتراك شغال
            if sub.owner.is_active_account:
                t = MonitorThread(sub.id, sub.keywords, sub.recipients)
                ACTIVE_THREADS[sub.id] = t
                t.start()
    app.run(host='0.0.0.0', port=5000)
