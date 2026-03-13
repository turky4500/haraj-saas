import json, re, time, threading, datetime, random, logging
from urllib.parse import urljoin, quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

# إخفاء تحذيرات SSL
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger("haraj_bot")

HARAJ_BASE = "https://haraj.com.sa"
WHATSAPP_API_URLS = [
    "https://whatsapp.tkwin.com.sa/api/v1/send",
    "https://whatsapp.tkwin.com.sa/api/v1/send/"
]
HARAJ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Haraj Monitor SaaS; +https://example.com/contact)",
    "Accept-Language": "ar-SA,ar;q=0.9"
}

# ===== معالجة النصوص (تجاهل الهمزات) =====
_AR_DIACRITICS_RE = re.compile(r"[\u064B-\u0652\u0670\u0640]")
_AR_NORM_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ؤ": "و", "ئ": "ي", "ى": "ي", "ة": "ه"})

def normalize_text(s: str) -> str:
    s = (s or "").lower()
    s = _AR_DIACRITICS_RE.sub("", s)
    s = s.translate(_AR_NORM_MAP)
    s = re.sub(r"[^\u0600-\u06FFa-z0-9\s]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def matches_keyword(text: str, kw: str, excluded: list) -> bool:
    nt = normalize_text(text)
    for neg in excluded:
        neg_norm = normalize_text(neg)
        if neg_norm and re.search(r'(^|\s)' + re.escape(neg_norm) + r'($|\s)', nt):
            return False
    kw_tokens = [t for t in normalize_text(kw).split() if t]
    if not kw_tokens: return True
    for token in kw_tokens:
        if not re.search(r'(^|\s)' + re.escape(token) + r'($|\s)', nt):
            return False
    return True

# ===== وظائف الاتصال =====
def create_session():
    s = requests.Session()
    retries = Retry(total=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retries, pool_connections=50, pool_maxsize=50)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s

def extract_ads(html_bytes: bytes) -> list:
    soup = BeautifulSoup(html_bytes, "html.parser")
    ads = []
    pattern = re.compile(r"https?://(?:www\.)?haraj\.com(?:\.sa)?/\d+/.+")
    for a in soup.find_all("a", href=True):
        url = urljoin(HARAJ_BASE, a["href"].strip())
        if pattern.match(url):
            title = a.get_text(strip=True) or "إعلان في حراج"
            ads.append((title, url))
    return ads

def extract_ad_id(url: str) -> str:
    m = re.search(r"/(\d+)(?:/|$)", url)
    return m.group(1) if m else url

def send_whatsapp(session, token: str, to: str, msg: str) -> bool:
    headers = {"Authorization": f"Bearer {token.strip()}", "Content-Type": "application/json"}
    
    # [تم الإصلاح]: المتغير لازم يكون "to" وليس "phone" عشان تقبله منصة تكوين!
    payload = {"to": to.strip(), "message": msg} 
    
    for api_url in WHATSAPP_API_URLS:
        for delay in [0, 2]:
            if delay: time.sleep(delay)
            try:
                r = session.post(api_url, json=payload, headers=headers, timeout=20, verify=False)
                if 200 <= r.status_code < 300: 
                    return True
                else:
                    # تسجيل الخطأ في السيرفر عشان نقدر نتتبعه
                    logger.error(f"WhatsApp API Error {r.status_code}: {r.text}")
            except Exception as e:
                logger.error(f"WhatsApp Connection Error: {e}")
                continue
    return False

# ===== خيط المراقبة (Thread) =====
class SubMonitor(threading.Thread):
    def __init__(self, sub_id: int, db_get_sub, db_get_token, db_mark_sent, db_add_log, db_update_total):
        super().__init__(daemon=True, name=f"sub-{sub_id}")
        self.sub_id = sub_id
        self._get_sub = db_get_sub
        self._get_token = db_get_token
        self._mark_sent = db_mark_sent
        self._add_log = db_add_log
        self._update_total = db_update_total
        self.stop_evt = threading.Event()
        self.session = create_session()
        self.ad_cache = {}

    def run(self):
        self._add_log(self.sub_id, "بدأ المراقبة")
        while not self.stop_evt.is_set():
            cfg = self._get_sub(self.sub_id)
            if not cfg or cfg["status"] != "active":
                break
                
            try:
                expires = datetime.datetime.fromisoformat(cfg["expires_at"])
                if datetime.datetime.now() >= expires:
                    self._add_log(self.sub_id, "انتهت مدة الاشتراك.", "warning")
                    break
            except: pass

            token = self._get_token()
            if not token:
                time.sleep(60)
                continue

            # إعدادات أوقات الهدوء
            quiet = False
            if cfg.get("quiet_enabled"):
                now = datetime.datetime.now()
                nm = now.hour * 60 + now.minute
                sm = cfg["quiet_start_hour"] * 60 + cfg["quiet_start_minute"]
                em = cfg["quiet_end_hour"] * 60 + cfg["quiet_end_minute"]
                if sm < em: quiet = sm <= nm < em
                else: quiet = nm >= sm or nm < em

            keywords = json.loads(cfg.get("keywords", "[]")) or [""]
            cities = json.loads(cfg.get("cities", "[]"))
            excluded = json.loads(cfg.get("excluded_words", "[]")) if cfg.get("exclude_enabled") else []
            sleep_sec = max(5, int(cfg.get("sleep_minutes", 15))) * 60
            
            sent_this_cycle = 0
            
            for kw in keywords:
                if self.stop_evt.is_set(): break
                url = f"{HARAJ_BASE}/search/{quote(kw, safe='')}/" if kw else f"{HARAJ_BASE}/"
                for page in range(1, 3): # فحص أول صفحتين
                    if self.stop_evt.is_set(): break
                    purl = f"{url}?page={page}" if page > 1 else url
                    
                    try:
                        r = self.session.get(purl, headers=HARAJ_HEADERS, timeout=20, verify=False)
                        ads = extract_ads(r.content)
                    except Exception:
                        continue

                    for title, ad_url in ads:
                        if self.stop_evt.is_set(): break
                        ad_id = extract_ad_id(ad_url)
                        
                        # هل أرسلناه من قبل؟
                        if self._mark_sent(self.sub_id, ad_id, check_only=True):
                            continue

                        # فحص المدينة والتطابق الدقيق
                        if ad_id not in self.ad_cache:
                            try:
                                ar = self.session.get(ad_url, headers=HARAJ_HEADERS, timeout=15, verify=False)
                                soup = BeautifulSoup(ar.content, "html.parser")
                                self.ad_cache[ad_id] = soup.get_text(" ", strip=True)
                            except: continue
                            
                        full_text = self.ad_cache[ad_id]
                        if cfg.get("city_filter_enabled") and cities:
                            if not any(c.lower() in full_text.lower() for c in cities):
                                continue
                                
                        if not matches_keyword(full_text, kw, excluded):
                            continue
                            
                        # تم التطابق!
                        if quiet:
                            self._mark_sent(self.sub_id, ad_id)
                            continue
                            
                        msg = f"🔔 إعلان جديد ({cfg['name']})\n📌 {title}\n🔗 {ad_url}"
                        
                        # حماية الرقم (تأخير 30-60 ثانية)
                        delay = random.uniform(30, 60)
                        end_sleep = time.time() + delay
                        while time.time() < end_sleep and not self.stop_evt.is_set():
                            time.sleep(1)
                            
                        if self.stop_evt.is_set(): break
                        
                        if send_whatsapp(self.session, token, cfg["whatsapp_number"], msg):
                            self._mark_sent(self.sub_id, ad_id)
                            sent_this_cycle += 1
                            
                    time.sleep(random.uniform(1, 3)) # راحة بين الصفحات
                    
            if sent_this_cycle > 0:
                new_total = cfg["sent_total"] + sent_this_cycle
                self._update_total(self.sub_id, new_total)

            # النوم العميق بين الفحوصات
            end_sleep = time.time() + sleep_sec
            while time.time() < end_sleep and not self.stop_evt.is_set():
                time.sleep(2)

        self._add_log(self.sub_id, "توقف المراقبة")

    def stop(self):
        self.stop_evt.set()

# ===== مدير البوتات (يتحكم بكل الخيوط) =====
class BotManager:
    def __init__(self, db_get_all, db_get_token, db_mark_sent, db_add_log, db_update_total, db_get_sub):
        self.threads = {}
        self.lock = threading.Lock()
        self._get_all = db_get_all
        self._get_token = db_get_token
        self._mark_sent = db_mark_sent
        self._add_log = db_add_log
        self._update_total = db_update_total
        self._get_sub = db_get_sub

    def start_sub(self, sub_id: int):
        with self.lock:
            if sub_id in self.threads and self.threads[sub_id].is_alive():
                return
            th = SubMonitor(sub_id, self._get_sub, self._get_token, self._mark_sent, self._add_log, self._update_total)
            self.threads[sub_id] = th
            th.start()

    def stop_sub(self, sub_id: int):
        with self.lock:
            th = self.threads.get(sub_id)
            if th and th.is_alive():
                th.stop()

    def reload_sub(self, sub_id: int):
        self.stop_sub(sub_id)
        # إعطاء مساحة للخيط القديم ليموت
        time.sleep(1) 
        self.start_sub(sub_id)

    def start_all(self):
        for sub in self._get_all():
            self.start_sub(sub["id"])
