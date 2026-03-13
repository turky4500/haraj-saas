"""
محرك البوت - مأخوذ من الكود الأصلي وتم تكييفه للعمل مع قاعدة البيانات
"""
import json, re, time, threading, datetime, random, logging
from pathlib import Path
from typing import List, Tuple, Optional, Dict
from urllib.parse import urljoin, quote
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

try:
    import certifi
    CERT_BUNDLE = certifi.where()
except Exception:
    CERT_BUNDLE = True

HARAJ_BASE = "https://haraj.com.sa"
WHATSAPP_API_URLS = [
    "https://whatsapp.tkwin.com.sa/api/v1/send",
    "https://whatsapp.tkwin.com.sa/api/v1/send/"
]
HARAJ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Haraj Monitor SaaS; +https://example.com)",
    "Accept-Language": "ar-SA,ar;q=0.9"
}
CITY_CHECK_WORKERS = 4
MAX_SEND_PER_CYCLE = 20
SSL_WARNED = set()

logger = logging.getLogger("haraj_bot")

# ========== أدوات مساعدة ==========

def create_session():
    s = requests.Session()
    retry = Retry(total=3, backoff_factor=1.0, status_forcelist=(429,500,502,503,504),
                  allowed_methods=("GET","POST"), raise_on_status=False)
    s.mount("https://", HTTPAdapter(max_retries=retry, pool_connections=20, pool_maxsize=20))
    s.mount("http://",  HTTPAdapter(max_retries=retry))
    return s

def safe_get(session, url, timeout=30):
    host = url.split("/")[2]
    try:
        r = session.get(url, headers=HARAJ_HEADERS, timeout=timeout, verify=CERT_BUNDLE)
        r.raise_for_status()
        return r.content
    except requests.exceptions.SSLError:
        if host not in SSL_WARNED:
            SSL_WARNED.add(host)
            logger.warning(f"SSL fallback for {host}")
        r = session.get(url, headers=HARAJ_HEADERS, timeout=timeout, verify=False)
        r.raise_for_status()
        return r.content

def send_whatsapp(session, token: str, to: str, text: str) -> bool:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
        "Accept": "application/json"
    }
    payload = {"to": to, "message": text}
    for url in WHATSAPP_API_URLS:
        try:
            r = session.post(url, json=payload, headers=headers, timeout=25, verify=CERT_BUNDLE)
            if 200 <= r.status_code < 300:
                return True
            if r.status_code in (429, 500, 502, 503, 504):
                for delay in (2, 5):
                    time.sleep(delay)
                    r = session.post(url, json=payload, headers=headers, timeout=25, verify=CERT_BUNDLE)
                    if 200 <= r.status_code < 300:
                        return True
        except Exception as e:
            logger.error(f"WhatsApp error: {e}")
    return False

def extract_ads(html_bytes: bytes, base_url: str) -> List[Tuple[str,str]]:
    soup = BeautifulSoup(html_bytes, "html.parser")
    pattern = re.compile(r"https?://(?:www\.)?haraj\.com(?:\.sa)?/\d+/.+")
    ads, seen = [], set()
    for a in soup.find_all("a", href=True):
        abs_url = urljoin(base_url, a["href"].strip())
        if pattern.match(abs_url) and abs_url not in seen:
            seen.add(abs_url)
            ads.append((a.get_text(strip=True) or "إعلان في حراج", abs_url))
    return ads

def extract_ad_id(url: str) -> str:
    m = re.search(r"/(\d+)(?:/|$)", url)
    return m.group(1) if m else url

_AR_NORM = str.maketrans({"أ":"ا","إ":"ا","آ":"ا","ؤ":"و","ئ":"ي","ى":"ي","ة":"ه"})
_DIACRITICS = re.compile(r"[\u064B-\u0652\u0670\u0640]")

def normalize(s: str) -> str:
    s = _DIACRITICS.sub("", (s or "").lower())
    s = s.translate(_AR_NORM)
    s = re.sub(r"[^\u0600-\u06FFa-z0-9\s]+"," ",s)
    return re.sub(r"\s+"," ",s).strip()

def matches(text: str, kw: str, excluded: list) -> bool:
    nt = normalize(text)
    for neg in excluded:
        nn = normalize(neg)
        if nn and re.search(r'(^|\s)'+re.escape(nn)+r'($|\s)', nt):
            return False
    tokens = normalize(kw).split()
    if not tokens:
        return True
    return all(re.search(r'(^|\s)'+re.escape(t)+r'($|\s)', nt) for t in tokens)

def is_quiet(cfg: dict) -> bool:
    if not cfg.get("quiet_enabled"):
        return False
    now = datetime.datetime.now()
    nm = now.hour*60 + now.minute
    sm = cfg.get("quiet_start_hour",1)*60 + cfg.get("quiet_start_minute",0)
    em = cfg.get("quiet_end_hour",6)*60   + cfg.get("quiet_end_minute",0)
    if sm == em: return True
    if sm < em:  return sm <= nm < em
    return nm >= sm or nm < em

# ========== خيط المراقبة لكل اشتراك ==========

class SubMonitor(threading.Thread):
    def __init__(self, sub_id: int, get_cfg_fn, get_token_fn, mark_sent_fn, add_log_fn, update_sent_total_fn):
        super().__init__(daemon=True, name=f"sub-{sub_id}")
        self.sub_id = sub_id
        self._get_cfg = get_cfg_fn
        self._get_token = get_token_fn
        self._mark_sent = mark_sent_fn
        self._add_log = add_log_fn
        self._update_total = update_sent_total_fn
        self.stop_evt = threading.Event()
        self.reload_evt = threading.Event()
        self.session = create_session()
        self.ad_cache: Dict[str,str] = {}
        self.sent_total = 0

    def log(self, msg: str, level="info"):
        logger.info(f"[sub-{self.sub_id}] {msg}")
        self._add_log(self.sub_id, msg, level)

    def run(self):
        self.log("بدأ المراقبة")
        while not self.stop_evt.is_set():
            cfg = self._get_cfg(self.sub_id)
            if not cfg:
                self.log("الاشتراك غير موجود، إيقاف.")
                break

            # فحص الانتهاء
            try:
                expires = datetime.datetime.fromisoformat(cfg["expires_at"])
                if datetime.datetime.now() >= expires:
                    self.log("انتهت مدة الاشتراك.")
                    break
            except Exception:
                pass

            token = self._get_token()
            if not token:
                self.log("لا يوجد توكن واتساب، انتظار...", "warning")
                self._sleep(60)
                continue

            quiet = is_quiet(cfg)
            keywords = json.loads(cfg.get("keywords","[]")) or [""]
            cities   = json.loads(cfg.get("cities","[]"))
            excluded = json.loads(cfg.get("excluded_words","[]"))
            city_filter = bool(cfg.get("city_filter_enabled", 1))
            recipients = [cfg["whatsapp_number"]]
            sub_name = cfg["name"]
            sleep_sec = max(5, int(cfg.get("sleep_minutes",15))) * 60

            sent = self._run_cycle(cfg, token, quiet, keywords, cities, excluded,
                                   city_filter, recipients, sub_name)
            self.sent_total += sent
            self._update_total(self.sub_id, self.sent_total)

            # نوم مع إمكانية إيقاف مبكر
            self._sleep(sleep_sec)

        self.log("توقف المراقبة.")

    def _sleep(self, seconds: float):
        end = time.time() + seconds
        while time.time() < end:
            if self.stop_evt.is_set() or self.reload_evt.is_set():
                self.reload_evt.clear()
                break
            time.sleep(min(1.0, end - time.time()))

    def _run_cycle(self, cfg, token, quiet, keywords, cities, excluded,
                   city_filter, recipients, sub_name) -> int:
        candidates = []
        cycle_seen = set()

        for kw in keywords:
            kw = kw.strip()
            url = f"{HARAJ_BASE}/search/{quote(kw,safe='')}/" if kw else f"{HARAJ_BASE}/"
            for page in range(1,4):
                purl = f"{url}?page={page}" if page > 1 else url
                try:
                    html = safe_get(self.session, purl)
                except Exception as e:
                    self.log(f"فشل جلب الصفحة: {e}", "error")
                    continue
                for title, ad_url in extract_ads(html, HARAJ_BASE):
                    ad_id = extract_ad_id(ad_url)
                    if self._mark_sent(self.sub_id, ad_id, check_only=True):
                        continue
                    if ad_id in cycle_seen:
                        continue
                    cycle_seen.add(ad_id)
                    candidates.append((kw, title, ad_url))
                time.sleep(random.uniform(1, 2))

        if not candidates:
            return 0

        to_send = []

        def check_one(item):
            kw, title, ad_url = item
            ad_id = extract_ad_id(ad_url)
            full_text = self.ad_cache.get(ad_id)
            if not full_text:
                try:
                    ad_html = safe_get(self.session, ad_url)
                    soup = BeautifulSoup(ad_html, "html.parser")
                    full_text = soup.get_text(" ", strip=True)
                    self.ad_cache[ad_id] = full_text
                    if len(self.ad_cache) > 500:
                        oldest = list(self.ad_cache.keys())[:100]
                        for k in oldest: del self.ad_cache[k]
                except Exception:
                    return None
            if city_filter and cities:
                ft_lower = full_text.lower()
                if not any(c.lower() in ft_lower for c in cities):
                    return None
            if not matches(full_text, kw, excluded):
                return None
            return item

        with ThreadPoolExecutor(max_workers=CITY_CHECK_WORKERS) as ex:
            futures = [ex.submit(check_one, c) for c in candidates]
            for fut in as_completed(futures):
                if self.stop_evt.is_set(): break
                r = fut.result()
                if r: to_send.append(r)

        sent = 0
        city_label = "، ".join(cities) if city_filter and cities else "كل المدن"

        for idx, (kw, title, ad_url) in enumerate(to_send, 1):
            if self.stop_evt.is_set(): break
            if idx > MAX_SEND_PER_CYCLE: break
            ad_id = extract_ad_id(ad_url)

            if quiet:
                self.log(f"[هدوء] حفظ: {title}")
                continue

            msg = f"🔔 إعلان جديد ({sub_name})\n📍 {city_label}\n📌 {title}\n🔗 {ad_url}"

            for to in recipients:
                delay = random.uniform(10, 30)
                self.log(f"انتظار {delay:.0f}s قبل الإرسال...")
                self._sleep(delay)
                if self.stop_evt.is_set(): break
                ok = send_whatsapp(self.session, token, to, msg)
                if ok:
                    self._mark_sent(self.sub_id, ad_id, title=title, url=ad_url)
                    sent += 1
                    self.log(f"✅ أُرسل: {title}")
                else:
                    self.log(f"❌ فشل الإرسال: {title}", "error")

        return sent

    def stop(self):
        self.stop_evt.set()

    def reload(self):
        self.reload_evt.set()


# ========== مدير كل الاشتراكات ==========

class BotManager:
    def __init__(self, db_get_fn, db_get_token_fn, db_mark_sent_fn,
                 db_add_log_fn, db_update_total_fn, db_get_sub_fn):
        self._db_get = db_get_fn
        self._get_token = db_get_token_fn
        self._mark_sent = db_mark_sent_fn
        self._add_log = db_add_log_fn
        self._update_total = db_update_total_fn
        self._get_sub = db_get_sub_fn
        self.threads: Dict[int, SubMonitor] = {}
        self._lock = threading.Lock()

    def start_sub(self, sub_id: int):
        with self._lock:
            th = self.threads.get(sub_id)
            if th and th.is_alive():
                return
            th = SubMonitor(
                sub_id, self._get_sub, self._get_token,
                self._mark_sent, self._add_log, self._update_total
            )
            self.threads[sub_id] = th
            th.start()
            logger.info(f"Started sub-{sub_id}")

    def stop_sub(self, sub_id: int):
        with self._lock:
            th = self.threads.get(sub_id)
            if th and th.is_alive():
                th.stop()

    def reload_sub(self, sub_id: int):
        with self._lock:
            th = self.threads.get(sub_id)
            if th and th.is_alive():
                th.reload()

    def status(self) -> dict:
        with self._lock:
            return {sid: th.is_alive() for sid, th in self.threads.items()}

    def start_all_active(self):
        subs = self._db_get()
        for sub in subs:
            if sub["status"] == "active":
                expires = datetime.datetime.fromisoformat(sub["expires_at"])
                if datetime.datetime.now() < expires:
                    self.start_sub(sub["id"])
