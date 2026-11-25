"""
job_page_inspector.py
=====================

Opens ONE public job page safely and extracts facts that can be verified on
the page itself. It never logs in, never solves CAPTCHAs and never tries to
get around access restrictions.

Data contract (used by job_search.py)
--------------------------------------------
inspect_job_page() and parse_job_page() ALWAYS return a dict that contains
every key listed in RESULT_FIELDS, including on error paths. Missing facts
are None / [] / "" - they are never guessed.

    decision         "OK"         -> page readable, date fresh, not closed
                     "REJECTED"   -> closed, expired or too old
                     "UNVERIFIED" -> page or key facts could not be verified
    fetch_status     ok | invalid_url | unsafe_url | dns_failed |
                     skipped_by_policy | robots_disallowed |
                     robots_unavailable | unsafe_redirect |
                     too_many_redirects | http_error | blocked_by_site |
                     login_required | not_html | timeout | network_error |
                     blocked_address | javascript_required | parse_error
    freshness        fresh | too_old | future_date | not_found | not_checked
    field_sources    {"job_title": "json_ld", "publication_date": "meta", ...}

Facts are taken in this order: Schema.org JobPosting JSON-LD -> microdata ->
JobPosting inside other embedded JSON -> HTML fallback (meta tags, headings,
labelled text). field_sources records where every value came from.

Security
--------
* Only http/https on ports 80/443, no credentials in the URL.
* Host names are resolved and EVERY address must be public. Numeric host
  tricks (2130706433, 0x7f000001) are rejected.
* The real socket is checked again after connecting, so a DNS answer that
  changes between the check and the request (DNS rebinding) is also blocked.
* Every redirect target goes through the same checks (max 5 redirects).
* System proxy settings are ignored, timeouts are set, downloads are capped.
* robots.txt is respected; LinkedIn pages are not fetched (terms of use).
"""

from __future__ import annotations

import html
import ipaddress
import json
import math
import re
import socket
import time as time_module
import unicodedata
from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool


# ============================================================
# 1. SETTINGS
# ============================================================

CONTRACT_VERSION = 2
TIMEZONE = ZoneInfo("Europe/Istanbul")

REQUEST_TIMEOUT = (5, 15)            # (connect, read) seconds
MAX_PAGE_SIZE = 2 * 1024 * 1024      # 2 MB per job page
MAX_ROBOTS_SIZE = 512 * 1024
MAX_REDIRECTS = 5
MAX_DESCRIPTION_CHARS = 40_000
DEFAULT_MAX_AGE_HOURS = 7 * 24
FUTURE_DATE_TOLERANCE = timedelta(hours=12)   # time-zone differences
PER_HOST_DELAY_SECONDS = 1.5                  # politeness between requests

USER_AGENT = "AI-Career-Agent/1.0 (personal job-search assistant; respects robots.txt)"
ROBOTS_USER_AGENT = "AI-Career-Agent"

# Pages on these domains are never downloaded automatically.
NO_FETCH_DOMAINS = ("linkedin.com",)

BLOCKED_ADDRESS_MARKER = "BLOCKED_NON_PUBLIC_ADDRESS"

REASON_TEXT = {
    "invalid_url": "The URL is not a valid public http/https address.",
    "unsafe_url": "The URL points to a local, private or otherwise non-public address.",
    "dns_failed": "The host name could not be resolved.",
    "skipped_by_policy": "This site is not fetched automatically (terms of use); check it manually.",
    "robots_disallowed": "The site's robots.txt does not allow automated access to this page.",
    "robots_unavailable": "The site's robots.txt could not be read, so the page was not fetched.",
    "unsafe_redirect": "The page redirected to a non-public or invalid address.",
    "too_many_redirects": "Too many redirects.",
    "http_error": "The page did not return HTTP 200.",
    "page_not_found": "The page no longer exists (HTTP 404/410).",
    "blocked_by_site": "The site blocked automated access (bot protection or CAPTCHA).",
    "login_required": "The page requires a login.",
    "not_html": "The response is not an HTML page.",
    "timeout": "The page did not respond in time.",
    "network_error": "The page could not be downloaded.",
    "blocked_address": "The connection reached a non-public address and was stopped.",
    "javascript_required": "The page content is built by JavaScript and could not be read.",
    "parse_error": "The page could not be analysed.",
    "no_title": "No job title could be found on the page.",
    "closed_signal": "The page says applications are closed.",
    "deadline_expired": "The application deadline has passed.",
    "date_not_found": "The publication date could not be verified on the page.",
    "date_too_old": "The publication date is older than the allowed window.",
    "future_date": "The publication date is in the future (probably wrong data).",
    "truncated_page": "The page was larger than the download limit; only part of it was read.",
}


# ============================================================
# 2. RESULT CONTRACT
# ============================================================

def empty_result(url: str = "") -> dict:
    """Return a result dict with every contract field set to 'unknown'."""
    return {
        "contract_version": CONTRACT_VERSION,
        "requested_url": url,
        "url": None,                       # final URL after safe redirects
        "canonical_url": None,
        "http_status": None,
        "fetch_status": "not_started",
        "data_source": None,               # json_ld | microdata | embedded_json | html_fallback
        "job_title": None,
        "company": None,
        "location": None,                  # human-readable location text
        "location_cities": [],             # raw locality/region strings
        "location_countries": [],          # "TR" for Turkey, otherwise as written
        "work_model": None,                # onsite | hybrid | remote | None
        "remote_turkey_eligible": None,    # True / False / None (unknown)
        "employment_type": None,
        "job_identifier": None,            # identifier from structured data
        "url_job_id": None,                # ID found in the URL
        "url_job_id_namespace": None,      # where that ID is valid (e.g. "greenhouse")
        "description_text": "",
        "publication_date": None,          # ISO string, Europe/Istanbul
        "publication_date_precision": None,  # "date" | "datetime"
        "age_hours": None,
        "freshness": "not_checked",
        "max_age_hours": None,
        "application_deadline": None,
        "deadline_status": "not_provided",  # not_provided | open | expired
        "closed_signals": [],
        "application_links": [],
        "application_status": "unknown",   # closed | apply_link_found | no_apply_link_found | unknown
        "field_sources": {},
        "page_truncated": False,
        "decision": "UNVERIFIED",
        "reason_codes": [],
        "reason": "",
        "checked_at": None,
    }


RESULT_FIELDS = tuple(empty_result().keys())


def _finish(result: dict, decision: str, codes: list[str]) -> dict:
    unique_codes = list(dict.fromkeys(code for code in codes if code))
    result["decision"] = decision
    result["reason_codes"] = unique_codes
    result["reason"] = " ".join(REASON_TEXT.get(code, code) for code in unique_codes)
    if decision == "OK" and not result["reason"]:
        result["reason"] = "Page readable; publication date within the allowed window; no closed signals."
    return result


# ============================================================
# 3. TEXT HELPERS
# ============================================================

def normalize_text(value, *, keep_newlines: bool = False) -> str:
    """Lower-case, remove Turkish/other accents, collapse whitespace."""
    text = str(value or "").casefold().replace("ı", "i")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    if keep_newlines:
        lines = (re.sub(r"[^\S\n]+", " ", line).strip() for line in text.split("\n"))
        return "\n".join(line for line in lines if line)
    return re.sub(r"\s+", " ", text).strip()


def _clean_inline(value) -> str | None:
    if value is None:
        return None
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _text_value(value) -> str | None:
    """Read a Schema.org value that may be a string, number, dict or list."""
    if isinstance(value, list):
        for item in value:
            found = _text_value(item)
            if found:
                return found
        return None
    if isinstance(value, dict):
        for key in ("name", "@value", "value", "@id"):
            if value.get(key):
                return _text_value(value[key])
        return None
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return _clean_inline(value)
    return None


def _html_to_text(raw) -> str:
    """Convert an HTML fragment (possibly entity-escaped) to plain text with line breaks."""
    text = str(raw or "")
    if "&lt;" in text and "<" not in text:
        text = html.unescape(text)
    soup = BeautifulSoup(text, "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for block in soup.find_all(["p", "li", "div", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "ul", "ol"]):
        block.insert_before("\n")
        block.insert_after("\n")
    lines = (re.sub(r"[^\S\n]+", " ", html.unescape(line)).strip()
             for line in soup.get_text().split("\n"))
    return "\n".join(line for line in lines if line)[:MAX_DESCRIPTION_CHARS]


# ============================================================
# 4. TURKISH PLACES AND COUNTRIES
# ============================================================

TURKISH_PROVINCES = frozenset("""
adana adiyaman afyonkarahisar agri aksaray amasya ankara antalya ardahan artvin
aydin balikesir bartin batman bayburt bilecik bingol bitlis bolu burdur bursa
canakkale cankiri corum denizli diyarbakir duzce edirne elazig erzincan erzurum
eskisehir gaziantep giresun gumushane hakkari hatay igdir isparta istanbul izmir
kahramanmaras karabuk karaman kars kastamonu kayseri kilis kirikkale kirklareli
kirsehir kocaeli konya kutahya malatya manisa mardin mersin mugla mus nevsehir
nigde ordu osmaniye rize sakarya samsun sanliurfa siirt sinop sirnak sivas
tekirdag tokat trabzon tunceli usak van yalova yozgat zonguldak
""".split())

ISTANBUL_AREAS = frozenset("""
adalar arnavutkoy atasehir avcilar bagcilar bahcelievler bakirkoy basaksehir
bayrampasa besiktas beykoz beylikduzu beyoglu buyukcekmece catalca cekmekoy
esenler esenyurt eyupsultan fatih gaziosmanpasa gungoren kadikoy kagithane
kartal kucukcekmece maltepe pendik sancaktepe sariyer silivri sultanbeyli
sultangazi sile sisli tuzla umraniye uskudar zeytinburnu maslak levent etiler
mecidiyekoy kozyatagi
""".split())

_TURKEY_ALIASES = {
    "tr", "tur", "turkey", "turkiye", "republic of turkey", "republic of turkiye",
    "turkiye cumhuriyeti",
}


def normalize_country(value) -> str | None:
    """Return 'TR' for any spelling of Turkey, otherwise the cleaned value."""
    text = _text_value(value)
    if not text:
        return None
    normalized = normalize_text(text).strip(" .")
    if normalized in _TURKEY_ALIASES:
        return "TR"
    return text.upper() if len(text) == 2 else text


def find_turkish_places(text) -> dict:
    """Find Turkish provinces, Istanbul districts and 'Turkey' in a short location text."""
    normalized = normalize_text(text)
    words = set(re.findall(r"[a-z]+", normalized))
    places = sorted(words & (TURKISH_PROVINCES | ISTANBUL_AREAS))
    istanbul = "istanbul" in words or bool(words & ISTANBUL_AREAS)
    turkey = bool(re.search(r"\b(turkey|turkiye)\b", normalized)) or bool(places)
    return {"places": places, "istanbul": istanbul, "turkey": turkey}


# ============================================================
# 5. URL SAFETY
# ============================================================

_BLOCKED_HOST_SUFFIXES = (
    ".localhost", ".local", ".internal", ".lan", ".home", ".corp", ".intranet",
    ".arpa", ".onion", ".test", ".invalid", ".example",
)
_NUMERIC_HOST = re.compile(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]*|[0-9]+))*\.?")


def _is_public_ip(ip) -> bool:
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return bool(ip.is_global) and not ip.is_multicast


def _resolve_addresses(host: str, port: int) -> list[str]:
    """Resolve a host name to IP strings. Separate function so tests can replace it."""
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [info[4][0] for info in infos]


def check_url(url, *, resolve_dns: bool = True) -> tuple[bool, str]:
    """
    Return (True, "ok") only for a public http/https URL.
    Otherwise (False, reason) where reason is invalid_url, unsafe_url or dns_failed.
    """
    try:
        parsed = urlparse(str(url or "").strip())
        port = parsed.port
    except (TypeError, ValueError):
        return False, "invalid_url"

    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False, "invalid_url"
    if parsed.username or parsed.password:
        return False, "unsafe_url"
    if port not in (None, 80, 443):
        return False, "unsafe_url"

    host = parsed.hostname.rstrip(".").casefold()
    if not host or host == "localhost" or host.endswith(_BLOCKED_HOST_SUFFIXES):
        return False, "unsafe_url"

    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None
    if literal_ip is not None:
        return (True, "ok") if _is_public_ip(literal_ip) else (False, "unsafe_url")

    # Integer / hex / octal host forms (e.g. 2130706433 == 127.0.0.1).
    if _NUMERIC_HOST.fullmatch(host) or "." not in host:
        return False, "unsafe_url"
    if not re.fullmatch(r"[a-z0-9._-]+", host):
        # Internationalised names are allowed only in punycode form.
        try:
            host.encode("idna")
        except UnicodeError:
            return False, "invalid_url"

    if not resolve_dns:
        return True, "ok"

    try:
        addresses = _resolve_addresses(host, port or (443 if parsed.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError, OSError):
        return False, "dns_failed"
    if not addresses:
        return False, "dns_failed"
    for address in addresses:
        try:
            ip = ipaddress.ip_address(str(address).split("%")[0])
        except ValueError:
            return False, "unsafe_url"
        if not _is_public_ip(ip):
            return False, "unsafe_url"
    return True, "ok"


def is_no_fetch_domain(url) -> bool:
    host = (urlparse(str(url or "")).hostname or "").casefold()
    return any(host == domain or host.endswith("." + domain) for domain in NO_FETCH_DOMAINS)


# ---- connection-level guard (protects against DNS rebinding) ----

class BlockedAddressError(OSError):
    pass


def _ensure_public_peer(sock) -> None:
    try:
        peer = sock.getpeername()[0]
        ip = ipaddress.ip_address(str(peer).split("%")[0])
    except (OSError, ValueError) as error:
        raise BlockedAddressError(f"{BLOCKED_ADDRESS_MARKER}: peer unknown ({error})") from None
    if not _is_public_ip(ip):
        raise BlockedAddressError(f"{BLOCKED_ADDRESS_MARKER}: {peer}")


class _GuardedHTTPConnection(HTTPConnection):
    def _new_conn(self):
        sock = super()._new_conn()
        try:
            _ensure_public_peer(sock)
        except BlockedAddressError:
            sock.close()
            raise
        return sock


class _GuardedHTTPSConnection(HTTPSConnection):
    def _new_conn(self):
        sock = super()._new_conn()
        try:
            _ensure_public_peer(sock)
        except BlockedAddressError:
            sock.close()
            raise
        return sock


class _GuardedHTTPPool(HTTPConnectionPool):
    ConnectionCls = _GuardedHTTPConnection


class _GuardedHTTPSPool(HTTPSConnectionPool):
    ConnectionCls = _GuardedHTTPSConnection


class _GuardedAdapter(HTTPAdapter):
    def init_poolmanager(self, *args, **kwargs):
        super().init_poolmanager(*args, **kwargs)
        self.poolmanager.pool_classes_by_scheme = {
            "http": _GuardedHTTPPool,
            "https": _GuardedHTTPSPool,
        }


def create_session() -> requests.Session:
    """HTTP session that refuses non-public peers and ignores system proxies."""
    session = requests.Session()
    session.trust_env = False
    adapter = _GuardedAdapter(max_retries=0)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9,tr;q=0.8",
    })
    return session


_DEFAULT_SESSION = None
_LAST_REQUEST_TIME: dict[str, float] = {}
_ROBOTS_CACHE: dict[str, tuple] = {}


def _default_session() -> requests.Session:
    global _DEFAULT_SESSION
    if _DEFAULT_SESSION is None:
        _DEFAULT_SESSION = create_session()
    return _DEFAULT_SESSION


def _wait_for_host(host: str) -> None:
    delay = PER_HOST_DELAY_SECONDS
    if delay <= 0 or not host:
        return
    last = _LAST_REQUEST_TIME.get(host)
    if last is not None:
        remaining = delay - (time_module.monotonic() - last)
        if remaining > 0:
            time_module.sleep(remaining)
    _LAST_REQUEST_TIME[host] = time_module.monotonic()


# ============================================================
# 6. SAFE DOWNLOAD AND ROBOTS.TXT
# ============================================================

def safe_fetch(url, *, session=None, accept="text/html,application/xhtml+xml",
               max_bytes=MAX_PAGE_SIZE, hop_guard=None) -> dict:
    """
    Download a URL while checking every redirect hop.
    hop_guard(url) may return an error code to stop before a hop is requested.
    """
    out = {
        "ok": False, "status_code": None, "final_url": url, "headers": {},
        "content": b"", "truncated": False, "error_code": None, "error": None,
    }
    session = session or _default_session()
    current = url
    response = None
    try:
        for hop in range(MAX_REDIRECTS + 1):
            safe, why = check_url(current)
            if not safe:
                out["error_code"] = why if hop == 0 else "unsafe_redirect"
                out["error"] = f"{why}: {current}"
                return out
            if hop_guard is not None:
                guard_code = hop_guard(current)
                if guard_code:
                    out["error_code"] = guard_code
                    out["final_url"] = current
                    return out

            _wait_for_host(urlparse(current).hostname or "")
            response = session.get(
                current,
                headers={"Accept": accept},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
                stream=True,
            )
            if response.status_code in (301, 302, 303, 307, 308):
                location = response.headers.get("Location")
                response.close()
                response = None
                if not location:
                    out["error_code"] = "network_error"
                    out["error"] = "Redirect without Location header."
                    return out
                current = urljoin(current, location.strip())
                out["final_url"] = current
                continue
            break
        else:
            out["error_code"] = "too_many_redirects"
            return out

        out["status_code"] = response.status_code
        out["headers"] = {str(k).casefold(): str(v) for k, v in response.headers.items()}
        out["final_url"] = current

        content = bytearray()
        for chunk in response.iter_content(chunk_size=65536):
            if not chunk:
                continue
            remaining = max_bytes - len(content)
            if len(chunk) > remaining:
                content.extend(chunk[:remaining])
                out["truncated"] = True
                break
            content.extend(chunk)
        out["content"] = bytes(content)
        out["ok"] = True
        return out

    except requests.Timeout as error:
        out["error_code"], out["error"] = "timeout", str(error)
    except (requests.RequestException, OSError) as error:
        message = str(error)
        out["error_code"] = "blocked_address" if BLOCKED_ADDRESS_MARKER in message else "network_error"
        out["error"] = message
    finally:
        if response is not None:
            response.close()
    return out


def robots_allows(url, *, session=None) -> tuple[bool | None, str]:
    """
    (True, ...) allowed, (False, "robots_disallowed") forbidden,
    (None, "robots_unavailable") robots.txt could not be read (server error / network).
    Follows RFC 9309: a missing robots.txt (4xx) means everything is allowed.
    """
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}".casefold()
    entry = _ROBOTS_CACHE.get(origin)
    if entry is None:
        fetched = safe_fetch(origin + "/robots.txt", session=session,
                             accept="text/plain,*/*;q=0.5", max_bytes=MAX_ROBOTS_SIZE)
        status = fetched["status_code"]
        if fetched["ok"] and status == 200:
            parser = RobotFileParser()
            parser.parse(fetched["content"].decode("utf-8", errors="replace").splitlines())
            parser.modified()
            entry = ("parser", parser)
        elif fetched["ok"] and status is not None and 400 <= status < 500:
            entry = ("allow_all", None)
        else:
            entry = ("unavailable", None)
        _ROBOTS_CACHE[origin] = entry

    kind, parser = entry
    if kind == "allow_all":
        return True, "robots_not_found"
    if kind == "parser":
        allowed = parser.can_fetch(ROBOTS_USER_AGENT, url)
        return allowed, ("robots_allowed" if allowed else "robots_disallowed")
    return None, "robots_unavailable"


# ============================================================
# 7. DATE PARSING
# ============================================================

_MONTHS = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sept": 9, "sep": 9, "october": 10,
    "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
    "ocak": 1, "subat": 2, "mart": 3, "nisan": 4, "mayis": 5, "haziran": 6,
    "temmuz": 7, "agustos": 8, "eylul": 9, "ekim": 10, "kasim": 11, "aralik": 12,
}

_RELATIVE = re.compile(
    r"\b(\d{1,3}|an?|one|bir)\+?\s*"
    r"(minutes?|mins?|hours?|hrs?|days?|weeks?|months?|dakika|saat|gun|hafta|ay)\s+(ago|once)\b"
)


def _make_date(year, month, day, *, end_of_day=False):
    try:
        base = datetime(int(year), int(month), int(day))
    except ValueError:
        return None
    return datetime.combine(base.date(), time.max if end_of_day else time.min, tzinfo=TIMEZONE)


def _plausible(dt: datetime | None, now: datetime) -> bool:
    return dt is not None and datetime(2000, 1, 1, tzinfo=TIMEZONE) <= dt <= now + timedelta(days=5 * 366)


def parse_iso_datetime(value, *, end_of_day=False, now=None) -> tuple[datetime | None, str | None]:
    """Parse a structured-data date. Returns (datetime in Istanbul time, 'date'|'datetime')."""
    now = now or datetime.now(TIMEZONE)
    text = _text_value(value)
    if not text:
        return None, None
    text = text.strip()

    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if match:
        dt = _make_date(*match.groups(), end_of_day=end_of_day)
        return (dt, "date") if _plausible(dt, now) else (None, None)

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return parse_date_text(text, now=now, end_of_day=end_of_day)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TIMEZONE)
    dt = dt.astimezone(TIMEZONE)
    return (dt, "datetime") if _plausible(dt, now) else (None, None)


def parse_date_text(text, *, now=None, end_of_day=False) -> tuple[datetime | None, str | None]:
    """Parse dates written for humans: '20 Kasım 2026', 'Nov 20, 2026', '3 days ago', '20.11.2026'."""
    now = now or datetime.now(TIMEZONE)
    t = normalize_text(text)
    if not t:
        return None, None

    if re.search(r"\b(today|just now|just posted|bugun|az once)\b", t):
        return now, "date"
    if re.search(r"\b(yesterday|dun)\b", t):
        return now - timedelta(days=1), "date"

    match = _RELATIVE.search(t)
    if match:
        raw_number, unit = match.group(1), match.group(2)
        number = 1 if raw_number in ("a", "an", "one", "bir") else int(raw_number)
        if unit.startswith(("min", "dakika")):
            return now - timedelta(minutes=number), "datetime"
        if unit.startswith(("hour", "hr", "saat")):
            return now - timedelta(hours=number), "datetime"
        if unit.startswith(("day", "gun")):
            return now - timedelta(days=number), "date"
        if unit.startswith(("week", "hafta")):
            return now - timedelta(weeks=number), "date"
        return now - timedelta(days=30 * number), "date"

    candidates = []   # (position in text, datetime) - the earliest date in the text wins
    match = re.search(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", t)
    if match:
        candidates.append((match.start(), _make_date(*match.groups(), end_of_day=end_of_day)))

    match = re.search(r"\b(\d{1,2})([./-])(\d{1,2})\2(20\d{2})\b", t)
    if match:
        first, separator, second, year = match.groups()
        first, second = int(first), int(second)
        if separator == "." or first > 12:
            candidates.append((match.start(), _make_date(year, second, first, end_of_day=end_of_day)))
        elif second > 12:
            candidates.append((match.start(), _make_date(year, first, second, end_of_day=end_of_day)))
        # both <= 12 with "/" or "-": day/month order is ambiguous -> ignore

    match = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\.?,?\s+(20\d{2})\b", t)
    if match and match.group(2) in _MONTHS:
        candidates.append((match.start(), _make_date(match.group(3), _MONTHS[match.group(2)],
                                                     match.group(1), end_of_day=end_of_day)))

    match = re.search(r"\b([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(20\d{2})\b", t)
    if match and match.group(1) in _MONTHS:
        candidates.append((match.start(), _make_date(match.group(3), _MONTHS[match.group(1)],
                                                     match.group(2), end_of_day=end_of_day)))

    for _, dt in sorted(candidates, key=lambda pair: pair[0]):
        if _plausible(dt, now):
            return dt, "date"
    return None, None


# ============================================================
# 8. URL JOB IDS
# ============================================================

def extract_url_job_id(url) -> tuple[str | None, str | None]:
    """Return (namespace, id). IDs are only comparable inside the same namespace."""
    url = str(url or "")
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = parsed.path
    query = {k.casefold(): v for k, v in parse_qs(parsed.query).items()}

    rules = (
        ("linkedin.com", r"/jobs/view/(?:[^/?#]*?-)?(\d{6,})", "linkedin"),
        ("greenhouse.io", r"/jobs/(\d+)", "greenhouse"),
        ("lever.co", r"^/[^/]+/([0-9a-f]{8}-[0-9a-f-]{27,})", "lever"),
        ("ashbyhq.com", r"^/[^/]+/([0-9a-f]{8}-[0-9a-f-]{27,})", "ashby"),
        ("smartrecruiters.com", r"^/[^/]+/(\d{6,})", "smartrecruiters"),
        ("workable.com", r"/j/([A-Z0-9]{6,})", "workable"),
        ("kariyer.net", r"-(\d{5,})/?$", "kariyer.net"),
    )
    for domain, pattern, namespace in rules:
        if host == domain or host.endswith("." + domain):
            match = re.search(pattern, path, re.IGNORECASE)
            if match:
                return namespace, match.group(1).casefold()

    if "myworkdayjobs.com" in host:
        match = re.search(r"_(R?-?[0-9][A-Za-z0-9-]{3,})(?:[/?#]|$)", path)
        if match:
            return f"workday:{host.split('.')[0]}", match.group(1).casefold()
    if "oraclecloud.com" in host:
        match = re.search(r"/requisitions/(?:preview/)?(?:job/)?(\d+)", path, re.IGNORECASE)
        if match:
            return f"oracle:{host}", match.group(1)

    if "gh_jid" in query:
        return "greenhouse", query["gh_jid"][0].casefold()
    for key in ("jobid", "job_id", "requisitionid", "reqid", "jid", "vacancyid", "postingid"):
        if key in query and query[key][0].strip():
            return f"host:{host.removeprefix('www.')}", query[key][0].strip().casefold()
    return None, None


def _company_from_url(url) -> str | None:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").casefold()
    parts = [part for part in parsed.path.split("/") if part]
    if (host.endswith("greenhouse.io") or host.endswith("lever.co") or host.endswith("ashbyhq.com")
            or host.endswith("smartrecruiters.com")) and parts:
        return parts[0].replace("-", " ")
    if host.endswith("workable.com") and parts:
        return parts[0].replace("-", " ")
    for suffix in (".myworkdayjobs.com", ".recruitee.com", ".teamtailor.com", ".bamboohr.com"):
        if host.endswith(suffix):
            return host.split(".")[0].replace("-", " ")
    if ".jobs.personio." in host:
        return host.split(".")[0].replace("-", " ")
    return None


# ============================================================
# 9. STRUCTURED DATA (JSON-LD, MICRODATA, EMBEDDED JSON)
# ============================================================

def _is_job_posting(data) -> bool:
    types = data.get("@type", [])
    for item in _as_list(types):
        if str(item).rstrip("/").split("/")[-1].casefold() == "jobposting":
            return True
    return False


def _find_job_posting(data, depth=0):
    if depth > 25:
        return None
    if isinstance(data, dict):
        if _is_job_posting(data):
            return data
        for value in data.values():
            found = _find_job_posting(value, depth + 1)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_job_posting(item, depth + 1)
            if found:
                return found
    return None


def _load_json(raw: str):
    raw = (raw or "").strip()
    if not raw:
        return None
    for candidate in (raw, html.unescape(raw), re.sub(r",\s*([}\]])", r"\1", raw)):
        try:
            return json.loads(candidate, strict=False)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
    return None


def _microdata_value(element):
    if element is None:
        return None
    if element.has_attr("content"):
        return element.get("content")
    if element.name == "time" and element.has_attr("datetime"):
        return element.get("datetime")
    if element.name in ("a", "link") and element.has_attr("href"):
        return element.get("href")
    return element.get_text(" ", strip=True)


def _itemprop(name):
    return {"itemprop": re.compile(rf"(^|\s){name}(\s|$)")}


def _microdata_posting(soup):
    root = soup.find(attrs={"itemtype": re.compile(r"schema\.org/JobPosting", re.I)})
    if root is None:
        return None
    data = {"@type": "JobPosting"}
    for prop in ("title", "datePosted", "validThrough", "employmentType", "identifier", "jobLocationType"):
        element = root.find(attrs=_itemprop(prop))
        if element is not None:
            data[prop] = _microdata_value(element)
    description = root.find(attrs=_itemprop("description"))
    if description is not None:
        data["description"] = description.get("content") or str(description)
    organization = root.find(attrs=_itemprop("hiringOrganization"))
    if organization is not None:
        name = organization.find(attrs=_itemprop("name"))
        data["hiringOrganization"] = {"name": _microdata_value(name if name is not None else organization)}
    locations = []
    for place in root.find_all(attrs=_itemprop("jobLocation")):
        address = {}
        for key in ("addressLocality", "addressRegion", "addressCountry"):
            element = place.find(attrs=_itemprop(key))
            if element is not None:
                address[key] = _microdata_value(element)
        locations.append({"address": address or _microdata_value(place)})
    if locations:
        data["jobLocation"] = locations
    return data if data.get("title") or data.get("datePosted") else None


def _find_structured_posting(soup):
    for script in soup.find_all("script", attrs={"type": re.compile(r"application/ld\+json", re.I)}):
        posting = _find_job_posting(_load_json(script.string or script.get_text()))
        if posting:
            return posting, "json_ld"
    posting = _microdata_posting(soup)
    if posting:
        return posting, "microdata"
    for script in soup.find_all("script", attrs={"type": re.compile(r"^application/json$", re.I)}):
        raw = script.string or script.get_text()
        if raw and len(raw) < 1_500_000 and "jobposting" in raw.casefold():
            posting = _find_job_posting(_load_json(raw))
            if posting:
                return posting, "embedded_json"
    return None, None


def _parse_places(places) -> tuple[list[str], list[str], list[str]]:
    """Return (cities_and_regions, countries, free_texts) from Schema.org Place values."""
    cities, countries, texts = [], [], []
    for place in _as_list(places):
        if isinstance(place, str):
            texts.append(_clean_inline(place))
            continue
        if not isinstance(place, dict):
            continue
        address = place.get("address", place)
        for item in _as_list(address):
            if isinstance(item, str):
                texts.append(_clean_inline(item))
                continue
            if not isinstance(item, dict):
                continue
            for key in ("addressLocality", "addressRegion"):
                value = _text_value(item.get(key))
                if value:
                    cities.append(value)
            country = normalize_country(item.get("addressCountry"))
            if country:
                countries.append(country)
        if not place.get("address"):
            name = _text_value(place.get("name"))
            if name and name not in cities:
                texts.append(name)
    return ([c for c in cities if c], [c for c in countries if c], [t for t in texts if t])


def _fields_from_posting(posting: dict, now: datetime) -> dict:
    """Extract contract fields from a JobPosting dict. Missing facts stay None."""
    fields = {}
    fields["job_title"] = _text_value(posting.get("title")) or _text_value(posting.get("name"))
    organization = posting.get("hiringOrganization")
    if isinstance(organization, list):
        organization = organization[0] if organization else None
    fields["company"] = _text_value(organization)

    description = posting.get("description")
    fields["description_text"] = _html_to_text(description) if description else ""

    fields["publication"] = parse_iso_datetime(posting.get("datePosted"), now=now)
    fields["deadline"] = parse_iso_datetime(posting.get("validThrough"), end_of_day=True, now=now)

    employment = [_text_value(item) for item in _as_list(posting.get("employmentType"))]
    employment = [item for item in employment if item]
    fields["employment_type"] = ", ".join(employment) if employment else None

    identifier = posting.get("identifier")
    if isinstance(identifier, list):
        identifier = identifier[0] if identifier else None
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name")
    fields["job_identifier"] = _clean_inline(identifier) if identifier not in (None, "") else None

    cities, countries, texts = _parse_places(posting.get("jobLocation"))
    fields["cities"], fields["countries"], fields["location_texts"] = cities, countries, texts

    applicant_countries = []
    for item in _as_list(posting.get("applicantLocationRequirements")):
        value = item.get("name") or item.get("addressCountry") if isinstance(item, dict) else item
        country = normalize_country(value)
        if country:
            applicant_countries.append(country)
    fields["applicant_countries"] = applicant_countries

    location_type = normalize_text(" ".join(str(v) for v in _as_list(posting.get("jobLocationType"))))
    fields["telecommute"] = "telecommute" in location_type or "remote" in location_type
    return fields


# ============================================================
# 10. HTML FALLBACK
# ============================================================

_POSTED_LABEL = re.compile(
    r"(date posted|posted on|posted date|posting date|published on|publication date|"
    r"date published|yayin tarihi|yayinlanma tarihi|ilan tarihi|ilan yayin tarihi|"
    r"\bposted\b|\bpublished\b|\byayinlandi\b)"
)
_DEADLINE_LABEL = re.compile(
    r"(application deadline|deadline|closing date|apply by|applications close|"
    r"last date to apply|son basvuru tarihi|basvuru son tarihi|son basvuru|ilan bitis tarihi)"
)
_LOCATION_LABEL = re.compile(
    r"^(location|locations|job location|work location|office location|city|"
    r"lokasyon|konum|sehir|calisma yeri|is yeri|is lokasyonu|calisma lokasyonu)\s*(?:[:\-–]\s*(.*)|$)"
)
_WORK_MODEL_LABEL = re.compile(
    r"^(work model|workplace type|workplace|work type|remote status|"
    r"calisma sekli|calisma modeli|calisma tipi)\s*(?:[:\-–]\s*(.*)|$)"
)
_DESCRIPTION_CONTAINER = re.compile(
    r"(job[-_ ]?description|jobdescription|job[-_ ]?details|job[-_ ]?body|job[-_ ]?content|"
    r"posting|vacancy|jobad|ilan[-_ ]?detay|ilan[-_ ]?aciklama|description)", re.I
)

CLOSED_PHRASES = (
    "no longer accepting applications", "applications are closed", "application period has closed",
    "job is no longer available", "this job is no longer available", "position is no longer available",
    "position has been filled", "this job is closed", "this job has expired", "job has been closed",
    "posting has expired", "vacancy is closed", "applications are no longer being accepted",
    "this position is closed", "no longer open for applications",
    "basvurular sona erdi", "basvuru suresi doldu", "ilan kapatildi", "artik basvuru kabul edilmiyor",
    "ilan yayindan kaldirildi", "bu ilan artik aktif degil", "basvuruya kapali", "ilan suresi doldu",
    "bu ilan yayinda degil",
)

BLOCK_PAGE_MARKERS = (
    "captcha", "verify you are human", "are you a robot", "unusual traffic", "access denied",
    "request blocked", "attention required", "ddos protection", "robot olmadiginizi",
    "checking your browser",
)

PLATFORM_SITE_NAMES = frozenset({
    "greenhouse", "lever", "workday", "smartrecruiters", "workable", "ashby", "recruitee",
    "teamtailor", "personio", "bamboohr", "oracle", "successfactors", "taleo", "icims",
    "kariyer.net", "kariyer net", "indeed", "glassdoor", "linkedin", "youthall", "secretcv",
    "yenibiris", "eleman.net", "toptalent", "anbean", "jobleads", "careers", "jobs",
})

_APPLY_TEXT = re.compile(
    r"\b(apply|applying|apply now|easy apply|submit (your )?application|start application|"
    r"basvur\w*|hemen basvur\w*|basvuru yap\w*|ilana basvur\w*|basvuru formu)\b"
)


def _meta(soup, *names):
    for name in names:
        tag = (soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
               or soup.find("meta", attrs={"itemprop": name}))
        if tag and tag.get("content") and tag.get("content").strip():
            return tag.get("content").strip()
    return None


def _strip_site_suffix(title):
    if not title:
        return title
    parts = re.split(r"\s+[|–—]\s+", title)
    return parts[0].strip() if parts and parts[0].strip() else title


def _labelled_date(norm_text: str, label: re.Pattern, now, *, end_of_day=False, look_back=False):
    for match in label.finditer(norm_text):
        # Only the label's own line, or the next line when the label stands alone.
        after_lines = [line for line in norm_text[match.end(): match.end() + 120].split("\n")]
        after = after_lines[0].strip(" :-–")
        if not after and len(after_lines) > 1:
            after = after_lines[1]
        dt, precision = parse_date_text(after, now=now, end_of_day=end_of_day)
        if dt is None and look_back:
            before = norm_text[max(0, match.start() - 40): match.start()]
            dt, precision = parse_date_text(before, now=now, end_of_day=end_of_day)
        if dt is not None:
            return dt, precision
    return None, None


def _time_tag_dates(soup, now):
    posted = deadline = (None, None)
    for tag in soup.find_all("time"):
        value = tag.get("datetime") or tag.get_text(" ", strip=True)
        parent = tag.parent
        context = normalize_text(parent.get_text(" ", strip=True) if parent else "")[:160]
        if posted[0] is None and _POSTED_LABEL.search(context):
            posted = parse_iso_datetime(value, now=now)
        elif deadline[0] is None and _DEADLINE_LABEL.search(context):
            deadline = parse_iso_datetime(value, end_of_day=True, now=now)
    return posted, deadline


def _labelled_value(lines, norm_lines, pattern):
    for index, norm_line in enumerate(norm_lines):
        match = pattern.match(norm_line)
        if not match:
            continue
        # Value on the same line; otherwise the next line (label on its own line).
        value = ""
        if match.group(2):
            original = lines[index]
            value = re.split(r"\s*[:\-–]\s*", original, maxsplit=1)[-1].strip()
        if not value and index + 1 < len(lines):
            value = lines[index + 1].strip()
        if value and len(value) <= 120:
            return value
    return None


def _detect_work_model(*texts) -> str | None:
    text = normalize_text(" ".join(t for t in texts if t))
    if re.search(r"\b(hybrid|hibrit)\b", text):
        return "hybrid"
    if re.search(r"\b(remote|uzaktan|work from home|evden calisma|fully remote)\b", text):
        return "remote"
    if re.search(r"\b(on[- ]?site|in[- ]office|office[- ]based|ofisten|ofis ici|yerinde)\b", text):
        return "onsite"
    return None


def _application_links(soup, page_url):
    found, seen = [], set()
    for anchor in soup.find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("mailto:", "javascript:", "#", "tel:")):
            continue
        text = normalize_text(anchor.get_text(" ", strip=True) or anchor.get("aria-label") or anchor.get("title"))
        absolute = urljoin(page_url, href)
        path = urlparse(absolute).path.casefold()
        if "how to apply" in text or "nasil basvur" in text:
            continue
        if not (_APPLY_TEXT.search(text) or re.search(r"/(apply|application|basvur\w*)(/|$)", path)):
            continue
        if not check_url(absolute, resolve_dns=False)[0] or absolute in seen:
            continue
        seen.add(absolute)
        found.append({"text": (anchor.get_text(" ", strip=True) or "")[:100], "url": absolute})
        if len(found) >= 5:
            break
    return found


def _description_fallback(soup) -> str:
    best_text = ""
    for element in soup.find_all(["div", "section", "article", "main"]):
        marker = " ".join([element.get("id") or ""] + list(element.get("class") or []))
        if not marker or not _DESCRIPTION_CONTAINER.search(marker):
            continue
        text = _html_to_text(str(element))
        if len(text) > len(best_text):
            best_text = text
    if len(best_text) < 200:
        for name in ("main", "article"):
            element = soup.find(name)
            if element is not None:
                text = _html_to_text(str(element))
                if len(text) > len(best_text):
                    best_text = text
    if len(best_text) < 200 and soup.body is not None:
        text = _html_to_text(str(soup.body))
        if len(text) > len(best_text):
            best_text = text
    return best_text[:MAX_DESCRIPTION_CHARS]


# ============================================================
# 11. FRESHNESS
# ============================================================

def _assess_freshness(published, precision, now, max_age_hours):
    if published is None:
        return "not_found", None
    age_hours = round((now - published).total_seconds() / 3600, 1)
    if precision == "date":
        age_days = (now.date() - published.date()).days
        if age_days < -1:
            return "future_date", age_hours
        allowed_days = max(1, math.ceil(max_age_hours / 24))
        return ("fresh" if age_days <= allowed_days else "too_old"), age_hours
    if now - published < -FUTURE_DATE_TOLERANCE:
        return "future_date", age_hours
    return ("fresh" if now - published <= timedelta(hours=max_age_hours) else "too_old"), age_hours


# ============================================================
# 12. PAGE PARSER (pure function: no network)
# ============================================================

def parse_job_page(content, page_url, *, now=None, max_age_hours=DEFAULT_MAX_AGE_HOURS,
                   http_status=200, encoding=None, requested_url=None) -> dict:
    """Analyse downloaded HTML. Usable on its own in tests."""
    now = now or datetime.now(TIMEZONE)
    result = empty_result(requested_url or page_url)
    result.update({
        "url": page_url, "http_status": http_status, "checked_at": now.isoformat(),
        "max_age_hours": max_age_hours, "fetch_status": "ok",
    })
    sources = result["field_sources"]

    try:
        if isinstance(content, bytes):
            soup = BeautifulSoup(content, "html.parser", from_encoding=encoding)
        else:
            soup = BeautifulSoup(str(content or ""), "html.parser")

        posting, posting_source = _find_structured_posting(soup)
        structured = _fields_from_posting(posting, now) if posting else {}
        result["data_source"] = posting_source or "html_fallback"

        og_title = _meta(soup, "og:title", "twitter:title")
        site_name = _meta(soup, "og:site_name", "application-name")
        meta_published = _meta(soup, "article:published_time", "datePosted", "publish_date", "pubdate")
        canonical_tag = soup.find("link", rel=lambda value: value and "canonical" in str(value).casefold())
        canonical = canonical_tag.get("href") if canonical_tag else _meta(soup, "og:url")
        if canonical:
            canonical = urljoin(page_url, canonical.strip())
            if check_url(canonical, resolve_dns=False)[0]:
                result["canonical_url"] = canonical

        h1 = soup.find("h1")
        h1_text = _clean_inline(h1.get_text(" ", strip=True)) if h1 else None
        title_tag = _clean_inline(soup.title.get_text(" ", strip=True)) if soup.title else None

        time_posted, time_deadline = _time_tag_dates(soup, now)

        for tag in soup(["script", "style", "noscript", "template", "svg", "iframe"]):
            tag.decompose()
        lines = [re.sub(r"\s+", " ", line).strip() for line in soup.get_text("\n").split("\n")]
        lines = [line for line in lines if line]
        norm_lines = [normalize_text(line) for line in lines]
        norm_text = "\n".join(norm_lines)

        result["application_links"] = _application_links(soup, page_url)

        # Bot protection / JavaScript-only pages: report honestly instead of guessing.
        if len(norm_text) < 3000 and any(marker in norm_text for marker in BLOCK_PAGE_MARKERS):
            result["fetch_status"] = "blocked_by_site"
            return _finish(result, "UNVERIFIED", ["blocked_by_site"])
        needs_js = "enable javascript" in norm_text or "javascript etkin" in norm_text
        if posting is None and (len(norm_text) < 200 or (needs_js and len(norm_text) < 800)):
            result["fetch_status"] = "javascript_required"
            return _finish(result, "UNVERIFIED", ["javascript_required"])

        # <form> is NOT removed: some sites (ASP.NET) wrap the whole page in one.
        for tag in soup(["nav", "header", "footer", "aside"]):
            tag.decompose()

        # ---- title ----
        if structured.get("job_title"):
            result["job_title"], sources["job_title"] = structured["job_title"], posting_source
        elif h1_text and len(h1_text) <= 200:
            result["job_title"], sources["job_title"] = h1_text, "html_h1"
        elif og_title:
            result["job_title"], sources["job_title"] = _strip_site_suffix(_clean_inline(og_title)), "meta_og_title"
        elif title_tag:
            result["job_title"], sources["job_title"] = _strip_site_suffix(title_tag), "html_title"

        # ---- company ----
        title_company = None
        for candidate in (og_title, title_tag):
            match = re.search(r"\bat\s+([^|–—\-]{2,60}?)\s*(?:[|–—\-]|$)", candidate or "")
            if match:
                title_company = match.group(1).strip()
                break
        if site_name and normalize_text(site_name) in PLATFORM_SITE_NAMES:
            site_name = None  # "Greenhouse", "Kariyer.net"... are platforms, not employers
        for value, source in ((structured.get("company"), posting_source),
                              (_company_from_url(page_url), "url_slug"),
                              (title_company, "title_text"),
                              (_clean_inline(site_name), "meta_site_name")):
            if value:
                result["company"], sources["company"] = value, source
                break

        # ---- description ----
        description = structured.get("description_text") or ""
        if len(description) >= 150:
            sources["description_text"] = posting_source
        else:
            fallback = _description_fallback(soup)
            if len(fallback) > len(description):
                description = fallback
                sources["description_text"] = "html_fallback"
            elif description:
                sources["description_text"] = posting_source
        result["description_text"] = description

        # ---- location ----
        cities = list(structured.get("cities") or [])
        countries = list(structured.get("countries") or [])
        texts = list(structured.get("location_texts") or [])
        if cities or countries or texts:
            sources["location"] = posting_source
        else:
            labelled = _labelled_value(lines, norm_lines, _LOCATION_LABEL)
            if labelled:
                texts.append(labelled)
                sources["location"] = "visible_text_label"
            elif result["job_title"] and find_turkish_places(result["job_title"])["places"]:
                texts.append(result["job_title"])
                sources["location"] = "job_title"
        for text in texts + cities:
            if find_turkish_places(text)["turkey"] and "TR" not in countries:
                countries.append("TR")
        for text in texts:
            for place in find_turkish_places(text)["places"]:
                if place not in [normalize_text(c) for c in cities]:
                    cities.append(place.title())
        # Display text uses the page's own words; recognised places only fill location_cities.
        display_parts = list(structured.get("cities") or []) + texts
        display_parts += [c if c != "TR" else "Türkiye" for c in countries
                          if not (c == "TR" and find_turkish_places(" ".join(display_parts))["turkey"])]
        result["location_cities"] = list(dict.fromkeys(cities))
        result["location_countries"] = list(dict.fromkeys(countries))
        result["location"] = ", ".join(dict.fromkeys(p for p in display_parts if p)) or None

        # ---- work model and remote eligibility ----
        work_label = _labelled_value(lines, norm_lines, _WORK_MODEL_LABEL)
        location_raw = " ".join(texts)
        if structured.get("telecommute"):
            result["work_model"], sources["work_model"] = "remote", posting_source
        else:
            model = _detect_work_model(work_label, location_raw, result["job_title"])
            if model:
                result["work_model"], sources["work_model"] = model, "visible_text"

        applicant_countries = structured.get("applicant_countries") or []
        if applicant_countries:
            result["remote_turkey_eligible"] = "TR" in applicant_countries
            sources["remote_turkey_eligible"] = posting_source
        elif result["work_model"] == "remote" and "TR" in result["location_countries"]:
            result["remote_turkey_eligible"] = True
            sources["remote_turkey_eligible"] = sources.get("location")

        # ---- employment type / identifiers ----
        result["employment_type"] = structured.get("employment_type")
        if result["employment_type"]:
            sources["employment_type"] = posting_source
        result["job_identifier"] = structured.get("job_identifier")
        if result["job_identifier"]:
            sources["job_identifier"] = posting_source
        namespace, url_id = extract_url_job_id(result["canonical_url"] or page_url)
        if not url_id and requested_url:
            namespace, url_id = extract_url_job_id(requested_url)
        result["url_job_id_namespace"], result["url_job_id"] = namespace, url_id

        # ---- publication date ----
        published, precision = structured.get("publication") or (None, None)
        if published is not None:
            sources["publication_date"] = f"{posting_source}.datePosted"
        else:
            for (dt, prec), source in (
                (parse_iso_datetime(meta_published, now=now) if meta_published else (None, None), "meta"),
                (time_posted, "html_time_tag"),
                (_labelled_date(norm_text, _POSTED_LABEL, now, look_back=True), "visible_text"),
            ):
                if dt is not None:
                    published, precision = dt, prec
                    sources["publication_date"] = source
                    break
        result["publication_date"] = published.isoformat() if published else None
        result["publication_date_precision"] = precision
        result["freshness"], result["age_hours"] = _assess_freshness(published, precision, now, max_age_hours)

        # ---- deadline ----
        deadline, _ = structured.get("deadline") or (None, None)
        if deadline is not None:
            sources["application_deadline"] = f"{posting_source}.validThrough"
        else:
            for (dt, _), source in ((time_deadline, "html_time_tag"),
                                    (_labelled_date(norm_text, _DEADLINE_LABEL, now, end_of_day=True), "visible_text")):
                if dt is not None:
                    deadline = dt
                    sources["application_deadline"] = source
                    break
        result["application_deadline"] = deadline.isoformat() if deadline else None
        if deadline is not None:
            result["deadline_status"] = "expired" if deadline < now else "open"

        # ---- closed signals and application status ----
        result["closed_signals"] = [phrase for phrase in CLOSED_PHRASES if phrase in norm_text]
        if result["closed_signals"] or result["deadline_status"] == "expired":
            result["application_status"] = "closed"
        elif result["application_links"]:
            result["application_status"] = "apply_link_found"
        else:
            result["application_status"] = "no_apply_link_found"

        # ---- page decision ----
        codes = []
        if result["closed_signals"]:
            codes.append("closed_signal")
        if result["deadline_status"] == "expired":
            codes.append("deadline_expired")
        if result["freshness"] == "too_old":
            codes.append("date_too_old")
        if codes:
            return _finish(result, "REJECTED", codes)

        if result["freshness"] == "not_found":
            codes.append("date_not_found")
        elif result["freshness"] == "future_date":
            codes.append("future_date")
        if not result["job_title"]:
            codes.append("no_title")
        if codes:
            return _finish(result, "UNVERIFIED", codes)
        return _finish(result, "OK", [])

    except Exception as error:  # parsing must never crash the agent
        result["fetch_status"] = "parse_error"
        result = _finish(result, "UNVERIFIED", ["parse_error"])
        result["reason"] += f" ({type(error).__name__}: {error})"
        return result



# ============================================================
# 13. ORACLE RECRUITING (JavaScript career sites, e.g. UNDP)
# ============================================================
# Oracle "CandidateExperience" pages are empty HTML shells filled by JavaScript.
# The browser loads the job from a public JSON endpoint on the same host (no login).
# We read that same public data, still through every safety and robots.txt check.

_ORACLE_JOB_PATH = re.compile(r"/sites/([A-Za-z0-9_-]+)/(?:requisitions/)?(?:preview/)?job/(\d+)", re.I)


def oracle_api_url(url) -> str | None:
    parsed = urlparse(str(url or ""))
    host = (parsed.hostname or "").casefold()
    if not host.endswith("oraclecloud.com"):
        return None
    match = _ORACLE_JOB_PATH.search(parsed.path)
    if not match:
        return None
    site, job_id = match.groups()
    return (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
            f"?expand=all&onlyData=true&finder=ById;Id=%22{job_id}%22,siteNumber={site}")


def _oracle_posting(item: dict, job_id: str) -> dict:
    description = "\n".join(str(item[key]) for key in (
        "ExternalDescriptionStr", "ExternalResponsibilitiesStr", "ExternalQualificationsStr",
        "CorporateDescriptionStr", "OrganizationDescriptionStr") if item.get(key))
    locations = [{"address": {"addressLocality": item.get("PrimaryLocation"),
                              "addressCountry": item.get("PrimaryLocationCountry")}}]
    for extra in _as_list(item.get("secondaryLocations")):
        if isinstance(extra, dict):
            locations.append({"address": {"addressLocality": extra.get("Name"),
                                          "addressCountry": extra.get("CountryCode")}})
    posting = {
        "@type": "JobPosting", "title": item.get("Title"),
        "datePosted": item.get("ExternalPostedStartDate"),
        "validThrough": item.get("ExternalPostedEndDate"),
        "description": description, "identifier": str(item.get("Id") or job_id),
        "employmentType": item.get("JobSchedule") or item.get("ContractType"),
        "jobLocation": locations,
    }
    workplace = normalize_text(f"{item.get('WorkplaceType') or ''} {item.get('WorkplaceTypeCode') or ''}")
    if "remote" in workplace:
        posting["jobLocationType"] = "TELECOMMUTE"
    return posting


def _inspect_oracle_api(url, *, session, hop_guard, now, max_age_hours) -> dict | None:
    """Return a full contract result built from Oracle's public JSON, or None if not possible."""
    api_url = oracle_api_url(url)
    if not api_url:
        return None
    fetched = safe_fetch(api_url, session=session, accept="application/json", hop_guard=hop_guard)
    if not fetched["ok"] or fetched["status_code"] != 200:
        return None
    data = _load_json(fetched["content"].decode("utf-8", errors="replace"))
    items = data.get("items") if isinstance(data, dict) else None
    if not items or not isinstance(items[0], dict) or not items[0].get("Title"):
        return None
    item = items[0]
    posting = _oracle_posting(item, _ORACLE_JOB_PATH.search(urlparse(url).path).group(2))
    safe_json = json.dumps(posting, ensure_ascii=False).replace("</", "<\\/")
    page = (f'<html><head><script type="application/ld+json">{safe_json}</script></head><body>'
            f"<h1>{html.escape(str(item.get('Title')))}</h1>"
            f'<a href="{html.escape(url, quote=True)}">Apply (button on the Oracle job page)</a></body></html>')
    result = parse_job_page(page, url, now=now, max_age_hours=max_age_hours, requested_url=url)
    result["data_source"] = "oracle_public_api"
    result["field_sources"] = {key: value.replace("json_ld", "oracle_public_api")
                               for key, value in result["field_sources"].items()}
    workplace = normalize_text(f"{item.get('WorkplaceType') or ''} {item.get('WorkplaceTypeCode') or ''}")
    if "hybrid" in workplace:
        result["work_model"] = "hybrid"
        result["field_sources"]["work_model"] = "oracle_public_api"
    elif "on site" in workplace or "on_site" in workplace or "onsite" in workplace:
        result["work_model"] = "onsite"
        result["field_sources"]["work_model"] = "oracle_public_api"
    return result

# ============================================================
# 14. MAIN ENTRY POINT
# ============================================================

_LOGIN_PATH = re.compile(r"/(login|log-in|signin|sign-in|authwall|uas/login|account/login|oturum-ac|giris)\b", re.I)


def inspect_job_page(url, *, max_age_hours=DEFAULT_MAX_AGE_HOURS, session=None,
                     check_robots=True, now=None) -> dict:
    """
    Inspect one public job page. Never raises; always returns the full contract.
    max_age_hours: allowed age of the publication date (168 = 7 days, 24 for LinkedIn).
    """
    now = now or datetime.now(TIMEZONE)
    result = empty_result(url)
    result["checked_at"] = now.isoformat()
    result["max_age_hours"] = max_age_hours

    def fail(fetch_status, decision="UNVERIFIED", code=None, **extra):
        result["fetch_status"] = fetch_status
        result.update(extra)
        return _finish(result, decision, [code or fetch_status])

    try:
        safe, why = check_url(url)
        if not safe:
            return fail(why)
        if is_no_fetch_domain(url):
            return fail("skipped_by_policy")

        def hop_guard(hop_url):
            if is_no_fetch_domain(hop_url):
                return "skipped_by_policy"
            if check_robots:
                allowed, code = robots_allows(hop_url, session=session)
                if allowed is not True:
                    return code
            return None

        fetched = safe_fetch(url, session=session, hop_guard=hop_guard)
        result["url"] = fetched["final_url"]
        if not fetched["ok"]:
            return fail(fetched["error_code"] or "network_error")

        status = fetched["status_code"]
        result["http_status"] = status
        if status in (401, 403, 429, 999):
            return fail("blocked_by_site")
        if status in (404, 410):
            return fail("http_error", code="page_not_found")
        if status != 200:
            return fail("http_error")
        if _LOGIN_PATH.search(urlparse(fetched["final_url"]).path):
            return fail("login_required")

        content_type = fetched["headers"].get("content-type", "").casefold()
        if "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            return fail("not_html")
        charset = re.search(r"charset=([\w.-]+)", content_type)

        parsed = parse_job_page(
            fetched["content"], fetched["final_url"], now=now, max_age_hours=max_age_hours,
            http_status=status, encoding=charset.group(1) if charset else None, requested_url=url,
        )
        if parsed["fetch_status"] == "javascript_required":
            oracle = _inspect_oracle_api(fetched["final_url"], session=session, hop_guard=hop_guard,
                                         now=now, max_age_hours=max_age_hours)
            if oracle is not None:
                return oracle
        parsed["page_truncated"] = fetched["truncated"]
        if fetched["truncated"] and parsed["decision"] == "OK":
            parsed["reason"] += " " + REASON_TEXT["truncated_page"]
        return parsed

    except Exception as error:  # the agent must always get a dict back
        result = fail("parse_error")
        result["reason"] += f" ({type(error).__name__}: {error})"
        return result


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("Usage: python job_page_inspector.py <job-page-url>")
        raise SystemExit(2)
    output = inspect_job_page(sys.argv[1])
    output["description_text"] = output["description_text"][:800]
    print(json.dumps(output, ensure_ascii=False, indent=2))
