"""
job_search.py
====================

Search -> pre-filter -> page inspection -> screening -> duplicate merge -> report.

Roles
-----
* Qwen (Ollama) only PLANS search queries through the web_search tool.
  It never decides whether a job is valid and never writes the final list.
* Python runs the searches (DDGS), opens pages via job_page_inspector.py and
  applies every rule deterministically.
* If Ollama is not running, built-in seed queries are used instead.

Final decisions (screening_decision)
------------------------------------
CANDIDATE              all automatic checks passed (you still apply yourself)
MANUAL_REVIEW          passed hard rules, but something needs a human look
LINKEDIN_MANUAL_CHECK  LinkedIn result; not fetched automatically (terms of use),
                       date not verified - open it yourself
UNVERIFIED_DATE        publication date could not be verified on the page
UNVERIFIED             page or key facts (location, title) could not be verified
REJECTED               breaks a rule (too old, closed, senior, 2+ years required, wrong location...)

Usage (from the Python folder, with the virtual environment active)
-------------------------------------------------------------------
    python job_search.py                 full run with Qwen
    python job_search.py --no-llm        seed queries only (no Ollama)
    python job_search.py --url <URL>     check one job page and print the result
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import job_page_inspector as inspector
import job_sources
import settings
from job_page_inspector import TIMEZONE, find_turkish_places, normalize_text


# ============================================================
# 1. CONFIGURATION
# ============================================================

OLLAMA_OPTIONS = {"temperature": 0.3, "num_ctx": 8192}
REGION = "tr-tr"

MAX_RESULTS_PER_SEARCH = 8
MIN_SEARCH_CALLS = 6
MAX_SEARCH_CALLS = 10
MAX_AGENT_STEPS = 12
MAX_INSPECT_CALLS = 25
SECONDS_BETWEEN_SEARCHES = 2.0

def max_age_hours_default() -> int:          # job boards and company sites (settings.json)
    return int(settings.get()["freshness"]["max_age_days"]) * 24


def max_age_hours_linkedin() -> int:         # LinkedIn jobs (settings.json)
    return int(settings.get()["freshness"]["max_age_hours_linkedin"])


PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "Logs"

def seed_queries() -> list[str]:
    """Used when Qwen is unavailable or makes too few searches. Built from settings.json."""
    city = settings.city()
    queries = [f"{q} {city}" for q in settings.get()["search"]["job_api_queries"]]
    if settings.get()["location"]["country_code"] == "tr" and not settings.get()["roles"]["role_keywords"]:
        queries += [f"veri analisti stajyer {city}", f"iş analisti stajyer {city}", f"yapay zeka stajyer {city}"]
    first = settings.get()["search"]["job_api_queries"][0] if settings.get()["search"]["job_api_queries"] else "intern"
    queries += [f"site:boards.greenhouse.io {city} intern", f"site:jobs.lever.co {city} intern",
                f"site:linkedin.com/jobs {first} {city}"]
    return list(dict.fromkeys(queries))

DECISION_ORDER = (
    "CANDIDATE", "MANUAL_REVIEW", "LINKEDIN_MANUAL_CHECK",
    "UNVERIFIED_DATE", "UNVERIFIED", "REJECTED",
)


# ============================================================
# 2. URL AND QUERY HELPERS
# ============================================================

_TRACKING_PARAMS = {
    "trk", "trackingid", "refid", "lipi", "mc_cid", "mc_eid", "gclid", "fbclid",
    "msclkid", "gh_src", "lever-source", "lever-origin", "src", "ref", "source",
}


def normalize_url(url) -> str:
    """Stable key for a URL: lower-case host, no www, no fragment, no tracking parameters."""
    try:
        parsed = urlparse(str(url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ""
        host = parsed.hostname.casefold().removeprefix("www.")
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        query = sorted(
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_PARAMS
        )
        clean = parsed._replace(scheme="https", netloc=netloc, query=urlencode(query), fragment="")
        return urlunparse(clean).rstrip("/")
    except (TypeError, ValueError):
        return ""


def is_linkedin_url(url) -> bool:
    host = (urlparse(str(url or "")).hostname or "").casefold()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def clean_search_query(query: str, current_year: int | None = None) -> str:
    """
    Remove years the model adds (e.g. 2024 or even 2026). A year in the query makes
    the search engine match only pages that contain that number, so results drop.
    Freshness is checked on the page itself, not through the query.
    """
    query = re.sub(r"\b(19|20)\d{2}\b", " ", str(query or ""))
    return re.sub(r"\s+", " ", query).strip()[:200]


# ============================================================
# 3. SEARCH STATE AND WEB SEARCH TOOL
# ============================================================

@dataclass
class SearchState:
    results: list = field(default_factory=list)
    seen_url_keys: dict = field(default_factory=dict)   # url_key -> index in results
    queries: list = field(default_factory=list)
    query_keys: set = field(default_factory=set)
    search_calls: int = 0
    errors: list = field(default_factory=list)


def _ddgs_search(query: str, timelimit: str, max_results: int) -> list:
    try:
        from ddgs import DDGS
    except ImportError:
        try:  # older package name
            from duckduckgo_search import DDGS
        except ImportError:
            raise RuntimeError("The 'ddgs' package is not installed (pip install ddgs).") from None
    return DDGS(timeout=15).text(
        query, region=REGION, safesearch="moderate", timelimit=timelimit, max_results=max_results,
    ) or []


def _search_with_fallback(search_fn, query, timelimit, max_results):
    """
    Returns (results, note) or (None, error_message).
    The 1-week filter is only a search-engine hint (the real date is verified on the page),
    so if it finds nothing we try the past month. LinkedIn keeps its 1-day filter, because
    LinkedIn pages are not opened and that filter is their only freshness signal.
    """
    try:
        results = search_fn(query, timelimit, max_results) or []
        if results or timelimit != "w":
            return results, ""
    except Exception as error:
        if timelimit != "w":
            return None, f"{type(error).__name__}: {error}"
    if SECONDS_BETWEEN_SEARCHES > 0:
        time.sleep(SECONDS_BETWEEN_SEARCHES)
    try:
        results = search_fn(query, "m", max_results) or []
    except Exception as error:
        return None, f"No results in the past week or month ({type(error).__name__}: {error})"
    if not results:
        return None, "No results in the past week or month."
    return results, "No results in the past week; used past-month search (dates are still checked on each page)."


def run_web_search(state: SearchState, query, max_results=MAX_RESULTS_PER_SEARCH, *, search_fn=None) -> dict:
    """Run one search, store new unique results, return a short summary."""
    search_fn = search_fn or _ddgs_search
    if not isinstance(query, str) or not query.strip():
        return {"error": "Search query is empty.", "new_results": 0}
    if state.search_calls >= MAX_SEARCH_CALLS:
        return {"error": "Maximum number of searches reached. Stop searching.", "new_results": 0}

    query = clean_search_query(query, datetime.now(TIMEZONE).year)
    key = normalize_text(query)
    if key in state.query_keys:
        return {"error": "This query was already used. Use a different one.", "new_results": 0}
    try:
        max_results = max(1, min(int(max_results), MAX_RESULTS_PER_SEARCH))
    except (TypeError, ValueError):
        max_results = MAX_RESULTS_PER_SEARCH

    state.query_keys.add(key)
    state.queries.append(query)
    state.search_calls += 1
    linkedin_query = "linkedin" in key
    timelimit = "d" if linkedin_query else "w"
    print(f"\nSEARCH {state.search_calls}/{MAX_SEARCH_CALLS}: {query}  (filter: {timelimit})")

    if state.search_calls > 1 and SECONDS_BETWEEN_SEARCHES > 0:
        time.sleep(SECONDS_BETWEEN_SEARCHES)
    raw_results, message = _search_with_fallback(search_fn, query, timelimit, max_results)
    if raw_results is None:
        print("SEARCH ERROR:", message)
        state.errors.append({"query": query, "error": message})
        return {"query": query, "error": message, "new_results": 0}
    if raw_results and message:
        print(" ", message)
        timelimit = "m"

    new_items = []
    for raw in raw_results or []:
        title = str(raw.get("title") or "").strip()
        url = str(raw.get("href") or raw.get("url") or "").strip()
        snippet = str(raw.get("body") or raw.get("snippet") or "").strip()
        url_key = normalize_url(url)
        if not url_key or not title:
            continue
        if url_key in state.seen_url_keys:
            existing = state.results[state.seen_url_keys[url_key]]
            if query not in existing["search_queries"]:
                existing["search_queries"].append(query)
            if len(snippet) > len(existing["snippet"]):
                existing["snippet"] = snippet
            continue
        item = {
            "title": title, "url": url, "url_key": url_key, "snippet": snippet,
            "search_queries": [query], "search_time_filter": timelimit,
        }
        state.seen_url_keys[url_key] = len(state.results)
        state.results.append(item)
        new_items.append(item)

    print(f"  {len(new_items)} new result(s)")
    return {
        "query": query,
        "new_results": len(new_items),
        # Kept short on purpose: a 4B model has a small context window.
        "titles": [f"{item['title'][:90]} ({urlparse(item['url']).hostname})" for item in new_items],
        "note": "Search-engine recency is NOT the job's publication date; Python will verify pages.",
    }


WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "Search the public web for job postings. Returns titles of new results.",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search query in English or Turkish."},
                "max_results": {"type": "integer", "description": f"1-{MAX_RESULTS_PER_SEARCH}"},
            },
            "required": ["query"],
        },
    },
}


# ============================================================
# 4. QWEN QUERY PLANNING
# ============================================================

def _system_prompt(now: datetime) -> str:
    custom_roles = settings.get()["roles"]["role_keywords"]
    roles = ", ".join(custom_roles) if custom_roles else (
        "Data Analyst, Business Analyst, Business Intelligence / BI, Data Science, AI / Machine Learning,\n"
        "Data Analytics, Product Analytics / Product Analyst, AI Product, Quantitative Research")
    return f"""You plan web searches for a job-search program.
Current date: {now:%Y-%m-%d}. Target location: {settings.city()} ({settings.get()["location"]["country_code"].upper()}).

Target roles (internship or genuine junior/entry level only):
{roles}.

Rules:
- Call web_search once per query. Use distinct English AND Turkish queries
  (Turkish words: stajyer, staj, veri analisti, iş analisti, iş zekası, yapay zeka).
- Mix general queries with site-specific ones (site:boards.greenhouse.io, site:jobs.lever.co,
  site:kariyer.net, site:linkedin.com/jobs).
- Never add past years. Never search for courses, certificates, CV examples or salaries.
- Do not search for senior, lead or manager roles, or new-graduate programmes.
- Make at least {MIN_SEARCH_CALLS} searches and never repeat a query.
- Do NOT write a job list. When you are done searching, reply only with: DONE"""


def plan_searches_with_llm(state: SearchState, *, search_fn=None, chat_fn=None) -> bool:
    """Let Qwen choose queries. Returns False if Ollama could not be used."""
    if chat_fn is None:
        try:
            from ollama import chat as chat_fn
        except ImportError:
            print("Ollama Python package not installed; using seed queries.")
            return False

    now = datetime.now(TIMEZONE)
    messages = [
        {"role": "system", "content": _system_prompt(now)},
        {"role": "user", "content": f"Plan and run the searches for current {settings.city()} internships and junior roles."},
    ]

    def call_model():
        try:
            return chat_fn(model=settings.model(), messages=messages, tools=[WEB_SEARCH_TOOL],
                           think=False, stream=False, options=OLLAMA_OPTIONS)
        except TypeError:  # older client without the "think" argument
            return chat_fn(model=settings.model(), messages=messages, tools=[WEB_SEARCH_TOOL],
                           stream=False, options=OLLAMA_OPTIONS)

    for step in range(1, MAX_AGENT_STEPS + 1):
        if state.search_calls >= MAX_SEARCH_CALLS:
            break
        print(f"\nAGENT STEP {step}/{MAX_AGENT_STEPS}")
        try:
            response = call_model()
        except Exception as error:
            print("OLLAMA ERROR:", f"{type(error).__name__}: {error}")
            return state.search_calls > 0

        message = response.message
        tool_calls = list(message.tool_calls or [])
        messages.append(message)

        if not tool_calls:
            if state.search_calls < MIN_SEARCH_CALLS:
                messages.append({
                    "role": "user",
                    "content": f"You made {state.search_calls} searches. Make new, different searches "
                               f"until you reach at least {MIN_SEARCH_CALLS}.",
                })
                continue
            break

        for call in tool_calls:
            name = call.function.name
            arguments = call.function.arguments if isinstance(call.function.arguments, dict) else {}
            if name == "web_search":
                summary = run_web_search(state, arguments.get("query"),
                                         arguments.get("max_results", MAX_RESULTS_PER_SEARCH),
                                         search_fn=search_fn)
            else:
                summary = {"error": f"Unknown tool: {name}"}
            messages.append({"role": "tool", "tool_name": name,
                             "content": json.dumps(summary, ensure_ascii=False)})
    return True


def top_up_with_seed_queries(state: SearchState, *, search_fn=None) -> None:
    for query in seed_queries():
        if state.search_calls >= MIN_SEARCH_CALLS or state.search_calls >= MAX_SEARCH_CALLS:
            return
        if normalize_text(clean_search_query(query, datetime.now(TIMEZONE).year)) in state.query_keys:
            continue
        run_web_search(state, query, search_fn=search_fn)


# ============================================================
# 5. ROLE / LEVEL RULES (used for search results AND pages)
# ============================================================

def _any(patterns, text) -> bool:
    return any(re.search(pattern, text) for pattern in patterns)


ROLE_PATTERNS = (
    r"\bdata (analyst|analytics|analysis|science|scientist|insights?|intern|internship|"
    r"trainee|team intern|ai|governance|quality|visuali[sz]ation)\b",
    r"\banalytics\b",
    r"\bbusiness (analyst|analysis|intelligence)\b",
    r"\bbi\b", r"\bpower bi\b",
    r"\bproduct analy(st|tics)\b",
    r"\bproduct (manager|management|owner) (intern|internship|stajyer\w*|trainee)\b",
    r"\b(ai|a\.i\.|genai|gen ai|llm|nlp)\b", r"\bartificial intelligence\b",
    r"\bmachine learning\b", r"\bml\b", r"\bdeep learning\b",
    r"\bquant(itative)? (research|researcher|analyst)\b",
    r"\breporting (analyst|intern)\b",
    r"\bveri\b", r"\byapay zeka\b", r"\bmakine ogrenme\w*\b", r"\bis analist\w*\b",
    r"\bis zekasi\b", r"\braporlama\b",
)
RESEARCH_ASSISTANT = r"\bresearch assistant\b"

LEVEL_PATTERNS = (
    r"\bintern\b", r"\binterns\b", r"\binternship\b", r"\bstaj\w*\b", r"\btrainee\b",
    r"\bworking student\b", r"\bwerkstudent\w*\b", r"\bjunior\b", r"\bjr\b",
    r"\bentry[- ]level\b", r"\buzman yardimcisi\b", r"\bpart[- ]time student\b",
    r"\bresearch assistant\b",
)
INTERN_PATTERNS = (r"\bintern\b", r"\binternship\b", r"\bstaj\w*\b", r"\btrainee\b",
                   r"\bworking student\b", r"\bwerkstudent\w*\b")

SENIOR_PATTERNS = (
    r"\bsenior\b", r"\bsr\b", r"\blead\b", r"\bleader\b", r"\bprincipal\b", r"\bhead of\b",
    r"\bdirector\b", r"\bchief\b", r"\bvp\b", r"\bvice president\b", r"\bkidemli\b",
    r"\bmudur\w*\b", r"\byonetici\b", r"\bstaff (data|engineer|scientist|analyst)\b",
    r"\b(ii|iii|iv)\b", r"\bmid[- ]level\b", r"\bexperienced\b", r"\bdeneyimli\b",
)
MANAGER_PATTERN = r"\bmanager\b"

NEW_GRAD_PATTERNS = (
    r"\bnew grad\w*\b", r"\bgraduate (program|programme|scheme|trainee)\b",
    r"\bmanagement trainee\b", r"\bmt program\w*\b", r"\byeni mezun\b",
)

NON_TARGET_FUNCTIONS = (
    r"\bsales\b", r"\bsatis\b", r"\bcustomer (service|support)\b", r"\bmusteri temsilcisi\b",
    r"\baccount(ing|ant)\b", r"\bmuhasebe\b", r"\blegal\b", r"\bhukuk\b", r"\bgraphic design\w*\b",
    r"\bcall center\b", r"\bcagri merkezi\b", r"\bdata entry\b", r"\bveri giris\w*\b",
    r"\bsoftware (developer|engineer)\b", r"\b(frontend|backend|front[- ]end|back[- ]end|full[- ]?stack)\b",
    r"\bmobile developer\b", r"\bdevops\b",
)
STRONG_ROLE_PATTERNS = (
    r"\bdata (analyst|analytics|science|scientist)\b", r"\bbusiness (analyst|intelligence)\b",
    r"\bmachine learning\b", r"\bartificial intelligence\b", r"\b(ai|ml) (engineer|intern|developer)\b",
    r"\bveri analist\w*\b", r"\bveri bilim\w*\b", r"\bis zekasi\b",
)

NON_JOB_PATTERNS = (
    r"\bcourses?\b", r"\bkurs\w*\b", r"\bbootcamp\b", r"\bcertificat\w*\b", r"\bsertifika\w*\b",
    r"\btutorial\b", r"\blearn\b", r"\begitim(i|leri)?\b", r"\b(resume|cv) (examples?|templates?|ornek\w*)\b",
    r"\bsalary\b", r"\bsalaries\b", r"\bmaas\w*\b", r"\binterview questions\b", r"\bmulakat soru\w*\b",
    r"\bhow to become\b", r"\bnasil olunur\b", r"\bfellowship\b", r"\bblog\b",
)

GENERIC_LISTING_PATTERNS = (
    r"\b\d[\d.,]*\+?\s*(jobs|job openings|vacancies|is ilani|ilan)\b",
    r"\bjobs in\b", r"\b(staj|is) ilanlari\b", r"\binternship (openings|opportunities)\b",
    r"\bcurrent openings\b", r"\bvacancies in\b", r"\bjob search\b", r"\bkariyer firsatlari\b",
)
GENERIC_PATH_MARKERS = (
    "/jobs/search", "/job-search", "/jobs/istanbul", "/is-ilanlari", "/internships/city/",
    "/vacancies/istanbul", "/careers/search", "/learning/", "/courses/", "/blog/",
    "/salaries/", "/maaslar/", "/q-", "/search",
)

_LINKEDIN_TITLE = re.compile(
    r"^(?P<company>.+?)\s+(?:is\s+)?hiring\s+(?P<title>.+?)"
    r"(?:\s+in\s+(?P<location>[^|]+?))?\s*(?:\|\s*linkedin)?\s*$",
    re.IGNORECASE,
)


_LINKEDIN_TR_TITLE = re.compile(r"^(?P<title>.+?)\s+-\s+(?P<company>[^—–]+?)\s+[—–]\s+(?P<location>.+)$")


def parse_linkedin_title(title: str) -> dict:
    """
    English: 'Acme hiring Data Analyst Intern in Istanbul, Türkiye | LinkedIn'
    Turkish: 'Data Analyst - Paribu — İstanbul, Türkiye | LinkedIn İş İlanları'
    """
    clean = re.sub(r"\s*[|\-–—]\s*LinkedIn(\s+(İş|Is|iş)\s+İlanları|\s+Jobs)?\s*$", "",
                   str(title or ""), flags=re.I).strip()
    for pattern in (_LINKEDIN_TITLE, _LINKEDIN_TR_TITLE):
        match = pattern.match(clean)
        if match:
            return {k: (v or "").strip() or None for k, v in match.groupdict().items()}
    return {"company": None, "title": clean or None, "location": None}


def _keyword_patterns(keywords) -> tuple:
    return tuple(rf"\b{re.escape(normalize_text(k))}\b" for k in keywords if str(k).strip())


def role_patterns() -> tuple:
    custom = settings.get()["roles"]["role_keywords"]
    return _keyword_patterns(custom) if custom else ROLE_PATTERNS


def level_patterns() -> tuple:
    custom = settings.get()["roles"]["level_keywords"]
    return _keyword_patterns(custom) if custom else LEVEL_PATTERNS


def assess_title(title, *, extra_title="", employment_type="") -> dict:
    """
    Role/level/seniority check.
    Returns {"status": "ok" | "reject", "reasons": [...], "flags": [...], "is_intern": bool}.
    """
    def prepare(text):
        # "A.I." / "A.I" -> "ai"; "Jr." -> "jr"
        return re.sub(r"\ba\.\s?i\b\.?", "ai", normalize_text(text))

    main = prepare(title)
    extra = prepare(extra_title)
    both = f"{main} {extra}".strip()
    employment = normalize_text(employment_type)
    reasons, flags = [], []

    is_intern = _any(INTERN_PATTERNS, both) or "intern" in employment
    has_level = _any(level_patterns(), both) or "intern" in employment

    if _any(NEW_GRAD_PATTERNS, both):
        reasons.append("New-graduate programme (outside the target).")
    if _any(SENIOR_PATTERNS, main or extra):
        reasons.append("Senior / lead / management level title.")
    if re.search(MANAGER_PATTERN, main or extra) and not is_intern:
        reasons.append("Manager title that is not an internship.")

    custom_roles = bool(settings.get()["roles"]["role_keywords"])
    role_main = _any(role_patterns(), main)
    role_extra = _any(role_patterns(), extra)
    research_ok = bool(re.search(RESEARCH_ASSISTANT, both)) and bool(
        re.search(r"\b(data|ai|analytics|machine learning|veri|yapay zeka)\b", both))
    if not (role_main or role_extra or research_ok):
        reasons.append("Title does not match the target data / BI / AI / analyst roles.")
    elif not custom_roles and _any(NON_TARGET_FUNCTIONS, main or extra) \
            and not _any(STRONG_ROLE_PATTERNS, main or extra):
        reasons.append("Title is a different function (sales, software development, data entry...).")
    elif main and not role_main and role_extra:
        flags.append("Target role appears only in the search title, not the page title.")

    if not has_level:
        reasons.append("Title is not clearly an internship or junior / entry-level role.")

    return {"status": "reject" if reasons else "ok", "reasons": reasons, "flags": flags,
            "is_intern": is_intern}


def prefilter_search_result(item: dict) -> tuple[bool, str]:
    """Cheap check on a search result BEFORE any page is opened."""
    url = item.get("url", "")
    if not inspector.check_url(url, resolve_dns=False)[0]:
        return False, "invalid_or_unsafe_url"
    parsed = urlparse(url)
    host = (parsed.hostname or "").casefold()
    path = (parsed.path + ("?" + parsed.query if parsed.query else "")).casefold()

    if (host.endswith("bing.com") and "/aclick" in path) or (host.endswith("google.com") and path.startswith("/url")) \
            or (host.endswith("duckduckgo.com") and "/y.js" in path):
        return False, "search_engine_ad_or_redirect"

    title = item.get("title", "")
    if is_linkedin_url(url):
        if "/jobs/view/" not in path:
            return False, "linkedin_not_individual_job"
        title = parse_linkedin_title(title)["title"] or title
    elif any(marker in path for marker in GENERIC_PATH_MARKERS):
        return False, "generic_listing_page"

    norm_title = normalize_text(title)
    if _any(GENERIC_LISTING_PATTERNS, norm_title):
        return False, "generic_listing_page"
    if _any(NON_JOB_PATTERNS, norm_title):
        return False, "not_a_job_course_or_article"

    verdict = assess_title(title)
    if verdict["status"] == "reject":
        # Level words often appear only in the snippet ("internship", "staj").
        only_level_missing = verdict["reasons"] == [
            "Title is not clearly an internship or junior / entry-level role."
        ]
        if only_level_missing and _any(level_patterns(), normalize_text(item.get("snippet"))):
            return True, "ok_level_in_snippet"
        return False, "title: " + " ".join(verdict["reasons"])
    return True, "ok"


# ============================================================
# 6. EXPERIENCE REQUIREMENT
# ============================================================

_NUM = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten|bir|iki|uc|dort|bes|alti|yedi|sekiz|dokuz)"
_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
              "eight": 8, "nine": 9, "ten": 10, "bir": 1, "iki": 2, "uc": 3, "dort": 4, "bes": 5,
              "alti": 6, "yedi": 7, "sekiz": 8, "dokuz": 9}
_YEAR = r"(?:years?|yrs?|yil\w*|senelik|sene)"
_EXP = r"(?:experience\w*|exp\b|deneyim\w*|tecrube\w*)"
_RANGE = rf"{_NUM}\s*\+?(?:\s*(?:-|–|to|ile|ila|or)\s*{_NUM})?\s*\+?"
_GAP = r"(?:\W+\w+){0,4}?\W+"

EXPERIENCE_PATTERNS = (
    re.compile(rf"\b{_RANGE}\s*{_YEAR}['’]?s?{_GAP}{_EXP}"),
    re.compile(rf"{_EXP}{_GAP}(?:of\s+)?(?:at least|minimum|min|en az|over|more than)?\s*{_RANGE}\s*{_YEAR}\b"),
    re.compile(rf"\b(?:at least|minimum|min|en az|more than|over)\s+{_RANGE}\s*{_YEAR}\s+(?:in|of|as|within|working)\b"),
)

_PREFERRED = re.compile(
    r"\b(preferred|preferably|preferable|desirable|desired|nice[- ]to[- ]have|is a plus|a plus|"
    r"big plus|bonus|advantage\w*|ideally|would be great|beneficial|tercih\w*|avantaj\w*)\b"
)
_NEGATION = re.compile(
    r"(\bnot (required|necessary|mandatory|essential|a requirement|needed)\b|"
    r"\bno (prior |previous |professional |work |formal )*(experience|deneyim)\w*\b|"
    r"\bwithout (prior |previous )?experience\b|\b(deneyim|tecrube)\w* (sarti )?"
    r"(aranmamaktadir|aranmiyor|gerekmez|gerekmemektedir|sart degil)|\b(deneyimsiz|tecrubesiz)\b)"
)
_MANDATORY = re.compile(
    r"\b(required|requires?|requirement\w*|must|minimum|min|at least|mandatory|essential|"
    r"need(ed|s)?|necessary|should have|zorunlu\w*|gerekli\w*|gerekir|gerekmektedir|"
    r"aranmaktadir|araniyor|aranir|sart\w*|en az|olmali\w*|sahip olmak)\b"
)
_SCHOOL_CONTEXT = re.compile(
    r"\b(school|study|studies|degree|graduat\w*|university|semester|remaining|left|okul\w*|"
    r"mezun\w*|ogrenim\w*|egitim\w*|universite\w*|donem\w*|kalan|sinif\w*)\b"
)
_GENERIC_EXP_REQUIRED = re.compile(
    r"\b(previous|prior|professional|work|industry|relevant|related|full[- ]time|is|sektor|"
    r"profesyonel)\s+(work\s+)?(experience|deneyim\w*|tecrube\w*)"
)
_REQUIRED_HEADING = re.compile(
    r"(requirements|qualifications|required|must have|what you need|what we.?re looking for|"
    r"what you.?ll bring|who you are|you have|aranan nitelikler|genel nitelikler|nitelikler|"
    r"gereksinimler|beklentilerimiz|aradigimiz)"
)
_PREFERRED_HEADING = re.compile(r"(preferred|nice to have|bonus|pluses|desired|tercih|avantaj|ekstra)")
_OTHER_HEADING = re.compile(
    r"(responsibilities|what you.?ll do|your role|the role|about (us|the)|benefits|we offer|"
    r"sorumluluk\w*|is tanimi|gorev\w*|sunduklarimiz|hakkimizda|yan haklar)"
)

EXPERIENCE_SEVERITY = {
    "reject_2plus_required": 6, "review_1year_required": 5, "review_unclear_duration": 4,
    "review_unspecified_required": 3, "preferred_only": 2, "explicitly_not_required": 1,
    "no_requirement_found": 0,
}


def _to_int(token) -> int | None:
    if token is None:
        return None
    return int(token) if token.isdigit() else _NUM_WORDS.get(token)


def _split_clauses(text: str) -> list[str]:
    text = re.sub(r"\b(min|e\.g|i\.e|vs|approx|yrs|incl)\.", r"\1", text)
    parts = re.split(r"\n|(?<=[.!?;])\s+|\s*[•·▪◦●■]\s*|\s+-\s+", text)
    return [part.strip(" -*:\t") for part in parts if part and part.strip(" -*:\t")]


def assess_experience(description: str, *, is_intern: bool = True) -> dict:
    """
    Find experience requirements sentence by sentence and return the MOST SEVERE finding.
    Result: {"status", "min_years", "reason", "evidence": [short sentences]}.
    """
    text = normalize_text(description, keep_newlines=True)
    if len(text) < 30:
        return {"status": "not_verified", "min_years": None,
                "reason": "Job description was unavailable or too short.", "evidence": []}

    findings = []   # (status, years, clause)
    section = "none"
    for clause in _split_clauses(text):
        has_years = any(pattern.search(clause) for pattern in EXPERIENCE_PATTERNS)
        if len(clause) <= 70 and not has_years:
            if _PREFERRED_HEADING.search(clause):
                section = "preferred"
                continue
            if _REQUIRED_HEADING.search(clause):
                section = "required"
                continue
            if _OTHER_HEADING.search(clause):
                section = "other"
                continue

        negated = bool(_NEGATION.search(clause))
        preferred = bool(_PREFERRED.search(clause)) or section == "preferred"
        mandatory = bool(_MANDATORY.search(clause)) or section == "required"

        years = None
        for pattern in EXPERIENCE_PATTERNS:
            for match in pattern.finditer(clause):
                if pattern is EXPERIENCE_PATTERNS[2] and _SCHOOL_CONTEXT.search(clause):
                    continue
                numbers = [_to_int(g) for g in match.groups() if g]
                numbers = [n for n in numbers if n is not None]
                if numbers:
                    low = min(numbers)
                    years = low if years is None else max(years, low)

        if negated:
            findings.append(("explicitly_not_required", 0, clause))
        elif years is not None and years >= 1:
            if preferred:
                findings.append(("preferred_only", years, clause))
            elif mandatory and years >= 2:
                findings.append(("reject_2plus_required", years, clause))
            elif mandatory:
                findings.append(("review_1year_required", years, clause))
            else:
                findings.append(("review_unclear_duration", years, clause))
        elif _GENERIC_EXP_REQUIRED.search(clause) and mandatory and not preferred:
            findings.append(("review_unspecified_required", None, clause))

    if not findings:
        status, years, evidence = "no_requirement_found", None, []
    else:
        status = max(findings, key=lambda f: EXPERIENCE_SEVERITY[f[0]])[0]
        relevant = [f for f in findings if f[0] == status]
        years = max((f[1] for f in relevant if f[1] is not None), default=None)
        evidence = [f[2][:160] for f in relevant[:3]]

    reasons = {
        "reject_2plus_required": f"Mandatory experience of {years}+ years is required.",
        "review_1year_required": "1 year of experience is mandatory; check whether internships count.",
        "review_unclear_duration": f"{years} year(s) of experience is mentioned but it is unclear whether it is mandatory.",
        "review_unspecified_required": "Previous professional experience is required, but no duration is given.",
        "preferred_only": "Experience is only listed as preferred / a plus.",
        "explicitly_not_required": "The description says previous experience is not required.",
        "no_requirement_found": "No experience requirement found in the description.",
    }
    result = {"status": status, "min_years": years, "reason": reasons[status], "evidence": evidence}
    if status == "no_requirement_found" and not is_intern:
        result["status"] = "review_no_requirement_junior"
        result["reason"] = "Junior role with no stated experience requirement; check manually."
    return result


# ============================================================
# 7. LOCATION
# ============================================================

def assess_location(record: dict) -> dict:
    """
    Return {"status", "action": accept|review|reject|unverified, "reason"}.
    The target city comes from settings.json (location.city). Locations are recognised
    for Türkiye (81 provinces + Istanbul districts).
    """
    cities = [str(c) for c in record.get("location_cities") or []]
    countries = [str(c) for c in record.get("location_countries") or []]
    location_text = record.get("location") or ""
    work_model = record.get("work_model")
    remote_tr = record.get("remote_turkey_eligible")
    config = settings.get()["location"]
    target = normalize_text(settings.city())
    target_label = settings.city()

    places = find_turkish_places(" , ".join(cities + [location_text]))
    in_target = (target == "istanbul" and places["istanbul"]) or target in places["places"]
    in_turkey = places["turkey"] or "TR" in countries
    foreign = [c for c in countries if c != "TR"]

    if work_model == "remote":
        if remote_tr is True or (remote_tr is None and in_turkey):
            if config.get("accept_remote_in_country", True):
                return {"status": "remote_turkey", "action": "accept",
                        "reason": "Remote work that is open to Turkey."}
            return {"status": "remote_turkey", "action": "review", "reason": "Remote work (check if you want remote)."}
        if remote_tr is False or foreign:
            return {"status": "remote_not_turkey", "action": "reject",
                    "reason": f"Remote, but not open to applicants in Turkey ({', '.join(foreign) or 'other countries'})."}
        return {"status": "remote_country_unverified", "action": "review",
                "reason": "Remote, but the allowed countries are not stated."}
    if in_target:
        return {"status": target.replace(" ", "_"), "action": "accept", "reason": f"Work location is {target_label}."}
    if in_turkey:
        action = "review" if config.get("accept_other_cities_for_review", True) else "reject"
        if places["places"]:
            return {"status": "turkey_other_city", "action": action,
                    "reason": f"In Turkey but outside {target_label} ({', '.join(places['places'])})."}
        return {"status": "turkey_city_unknown", "action": "review",
                "reason": "In Turkey, but the city is not stated."}
    if foreign:
        return {"status": "other_country", "action": "reject",
                "reason": f"Work location is outside Turkey: {location_text or ', '.join(foreign)}."}
    if location_text or cities:
        return {"status": "location_unrecognized", "action": "unverified",
                "reason": f"Location could not be matched to {target_label} or Turkey: {location_text}."}
    return {"status": "not_verified", "action": "unverified",
            "reason": "The work location was not found on the page."}


# ============================================================
# 8. SCREENING (inspection result -> final decision)
# ============================================================

def source_type(url) -> str:
    host = (urlparse(str(url or "")).hostname or "").casefold()
    path = urlparse(str(url or "")).path.casefold()
    aggregators = ("openopportunities.org", "globalroles.com", "unjobs.org", "offcycle.net",
                   "jobleads.com", "jooble.org", "careerjet.com", "talent.com", "adzuna.com")
    boards = ("kariyer.net", "youthall.com", "indeed.com", "glassdoor.com", "secretcv.com",
              "yenibiris.com", "eleman.net", "toptalent.co", "anbean.com", "wellfound.com")
    ats = ("greenhouse.io", "lever.co", "myworkdayjobs.com", "smartrecruiters.com", "ashbyhq.com",
           "workable.com", "oraclecloud.com", "successfactors.com", "taleo.net", "icims.com",
           "personio.de", "personio.com", "recruitee.com", "teamtailor.com", "bamboohr.com")
    matches = lambda domains: any(host == d or host.endswith("." + d) for d in domains)  # noqa: E731
    if matches(aggregators):
        return "aggregator"
    if is_linkedin_url(url):
        return "linkedin"
    if matches(boards):
        return "job_board"
    if matches(ats) or host.startswith(("careers.", "jobs.", "kariyer.")) or "/careers" in path \
            or "/kariyer" in path or "candidateexperience" in path:
        return "official"
    return "other"


SOURCE_PRIORITY = {"official": 3, "linkedin": 2, "job_board": 1, "other": 1, "aggregator": 0}


def _attach_search_info(record: dict, item: dict | None) -> None:
    item = item or {}
    record["search_title"] = item.get("title", "")
    record["search_url"] = item.get("url", "")
    record["search_snippet"] = item.get("snippet", "")
    record["search_queries"] = list(item.get("search_queries", []))
    primary_url = record.get("url") or record.get("search_url") or record.get("requested_url")
    record["source_type"] = source_type(primary_url)


def screen_job(inspection: dict, search_item: dict | None = None) -> dict:
    """Combine the inspector's facts with the title, location, experience and date rules."""
    record = dict(inspection)
    _attach_search_info(record, search_item)
    rejects, flags, unverified = [], [], []
    fetch_status = record.get("fetch_status")
    fetched = fetch_status == "ok"

    # Page-level decision from the inspector (closed, expired, too old...)
    if record.get("decision") == "REJECTED":
        rejects.append(record.get("reason") or "Rejected by page inspection.")

    # Title / level
    title = record.get("job_title") or ""
    if title or record["search_title"]:
        title_check = assess_title(title or record["search_title"],
                                   extra_title=record["search_title"] if title else "",
                                   employment_type=record.get("employment_type") or "")
        rejects += title_check["reasons"]
        flags += title_check["flags"]
    else:  # nothing to judge: the page was not read and there is no search title
        title_check = {"status": "not_verified", "reasons": [], "flags": [], "is_intern": True}
        unverified.append("Job title could not be verified.")
    record["title_check"] = title_check

    # Location
    location_check = assess_location(record)
    record["location_status"] = location_check["status"]
    record["location_reason"] = location_check["reason"]
    if location_check["action"] == "reject":
        rejects.append(location_check["reason"])
    elif location_check["action"] == "review":
        flags.append(location_check["reason"])
    elif location_check["action"] == "unverified" and fetched:
        unverified.append(location_check["reason"])

    # Experience
    if fetched:
        experience = assess_experience(record.get("description_text") or "", is_intern=title_check["is_intern"])
    else:
        experience = {"status": "not_verified", "min_years": None,
                      "reason": "Page was not read, so experience could not be checked.", "evidence": []}
    record["experience_status"] = experience["status"]
    record["experience_reason"] = experience["reason"]
    record["experience_evidence"] = experience["evidence"]
    if experience["status"] == "reject_2plus_required":
        rejects.append(experience["reason"])
    elif experience["status"].startswith("review_") or (fetched and experience["status"] == "not_verified"):
        flags.append(experience["reason"])

    # Readability, date and application details (only for pages that were read)
    if not fetched:
        unverified.append(record.get("reason") or f"Page could not be read ({fetch_status}).")
    else:
        if record.get("freshness") == "future_date":
            unverified.append(inspector.REASON_TEXT["future_date"])
        date_source = (record.get("field_sources") or {}).get("publication_date", "")
        if date_source in ("visible_text", "html_time_tag"):
            flags.append("Publication date was read from page text, not structured data; double-check it.")
        if record.get("application_status") == "no_apply_link_found":
            flags.append("No application link was found on the page.")

    if rejects:
        decision = "REJECTED"
    elif fetched and record.get("freshness") == "not_found":
        decision = "UNVERIFIED_DATE"
        unverified.insert(0, inspector.REASON_TEXT["date_not_found"])
    elif unverified or record.get("decision") == "UNVERIFIED":
        decision = "UNVERIFIED"
        if record.get("decision") == "UNVERIFIED" and record.get("reason") not in unverified:
            unverified.append(record.get("reason"))
    elif flags:
        decision = "MANUAL_REVIEW"
    else:
        decision = "CANDIDATE"

    record["review_flags"] = list(dict.fromkeys(flags))
    record["screening_decision"] = decision
    reasons = rejects if decision == "REJECTED" else unverified if decision.startswith("UNVERIFIED") else flags
    record["screening_reason"] = " ".join(dict.fromkeys(r for r in reasons if r)) or \
        "Passed all automatic checks. Open the page and apply yourself."
    return record


def linkedin_manual_record(item: dict) -> dict:
    """LinkedIn result: not fetched (terms of use). Only search-result facts, clearly marked."""
    parts = parse_linkedin_title(item.get("title", ""))
    record = inspector.empty_result(item.get("url", ""))
    record.update({
        "url": item.get("url"), "fetch_status": "skipped_by_policy",
        "job_title": parts["title"], "company": parts["company"], "location": parts["location"],
        "checked_at": datetime.now(TIMEZONE).isoformat(), "max_age_hours": max_age_hours_linkedin(),
        "field_sources": {"job_title": "search_result_title", "company": "search_result_title",
                          "location": "search_result_title"},
    })
    namespace, url_id = inspector.extract_url_job_id(item.get("url"))
    record["url_job_id_namespace"], record["url_job_id"] = namespace, url_id
    if parts["location"]:
        found = find_turkish_places(parts["location"])
        record["location_cities"] = [p.title() for p in found["places"]]
        if found["turkey"]:
            record["location_countries"] = ["TR"]
    record = _finish_inspection_reason(record, "skipped_by_policy")
    _attach_search_info(record, item)

    title_check = assess_title(parts["title"] or item.get("title", ""))
    location_check = assess_location(record)
    record["title_check"] = title_check
    record["location_status"], record["location_reason"] = location_check["status"], location_check["reason"]
    record["experience_status"], record["experience_reason"] = "not_verified", "LinkedIn page was not opened."
    record["experience_evidence"] = []
    rejects = list(title_check["reasons"])
    if location_check["action"] == "reject":
        rejects.append(location_check["reason"])
    record["review_flags"] = []
    if rejects:
        record["screening_decision"] = "REJECTED"
        record["screening_reason"] = " ".join(rejects)
    else:
        record["screening_decision"] = "LINKEDIN_MANUAL_CHECK"
        record["screening_reason"] = (
            "LinkedIn is not fetched automatically. Search filter was 'past "
            f"{'24 hours' if item.get('search_time_filter') == 'd' else 'week'}' (search engine only). "
            "Open it and confirm it was posted within 24 hours."
        )
    return record


def _finish_inspection_reason(record, code):
    record["decision"] = "UNVERIFIED"
    record["reason_codes"] = [code]
    record["reason"] = inspector.REASON_TEXT.get(code, code)
    return record


def evaluate_search_item(item: dict, *, inspect_fn=None) -> dict:
    inspect_fn = inspect_fn or inspector.inspect_job_page
    if is_linkedin_url(item.get("url")):
        return linkedin_manual_record(item)
    inspection = inspect_fn(item["url"], max_age_hours=max_age_hours_default())
    missing = [key for key in inspector.RESULT_FIELDS if key not in inspection]
    if missing:  # contract violation - never trust a partial result
        inspection = {**inspector.empty_result(item["url"]), **inspection}
        inspection["decision"] = "UNVERIFIED"
        inspection["reason"] = f"Inspector result was missing fields: {missing}"
    return screen_job(inspection, item)


# ============================================================
# 9. DUPLICATE DETECTION
# ============================================================

_LEGAL_SUFFIXES = {
    "inc", "llc", "ltd", "limited", "gmbh", "ag", "sa", "as", "a s", "sti", "san", "tic", "ve",
    "corp", "corporation", "co", "company", "group", "holding", "plc", "bv", "nv", "srl",
    "anonim", "sirketi", "limited", "teknoloji", "technologies", "technology",
}
_STOP = {"of", "and", "the", "for", "de", "la", "du", "ve"}


def company_tokens(name) -> list[str]:
    words = re.findall(r"[a-z0-9]+", normalize_text(name))
    return [w for w in words if w not in _LEGAL_SUFFIXES]


def companies_match(first, second) -> bool:
    a, b = company_tokens(first), company_tokens(second)
    if not a or not b:
        return False
    if a == b:
        return True
    initials_a = "".join(w[0] for w in a if w not in _STOP)
    initials_b = "".join(w[0] for w in b if w not in _STOP)
    if (len(a) == 1 and a[0] == initials_b) or (len(b) == 1 and b[0] == initials_a):
        return True   # "UNDP" == "United Nations Development Programme"
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(" ".join(shorter)) >= 4 and " ".join(shorter) in " ".join(longer)


_TITLE_NOISE = re.compile(r"\b(istanbul|turkiye|turkey|remote|hybrid|hibrit|m/f/d|f/m/d|tr)\b")


def title_similarity(first, second) -> float:
    a = _TITLE_NOISE.sub(" ", normalize_text(first))
    b = _TITLE_NOISE.sub(" ", normalize_text(second))
    a, b = re.sub(r"\W+", " ", a).strip(), re.sub(r"\W+", " ", b).strip()
    if not a or not b:
        return 0.0
    tokens_a, tokens_b = set(a.split()), set(b.split())
    jaccard = len(tokens_a & tokens_b) / len(tokens_a | tokens_b)
    return max(SequenceMatcher(None, a, b).ratio(), jaccard)


def _description_similarity(first, second) -> float | None:
    a, b = normalize_text(first)[:4000], normalize_text(second)[:4000]
    if len(a) < 300 or len(b) < 300:
        return None
    shingles = lambda t: {tuple(t.split()[i:i + 3]) for i in range(max(0, len(t.split()) - 2))}  # noqa: E731
    sa, sb = shingles(a), shingles(b)
    return len(sa & sb) / len(sa | sb) if sa and sb else None


def _parse_dt(value):
    try:
        return datetime.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def _id_in_url(identifier, url) -> bool:
    """An official requisition number (4+ digits) quoted in another site's URL, e.g. .../undp/38132."""
    identifier = normalize_text(identifier)
    if not re.fullmatch(r"\d{4,}", identifier or ""):
        return False
    return identifier in re.split(r"[^0-9]+", urlparse(str(url or "")).path + "?" + urlparse(str(url or "")).query)


def _same_day(a, b, days: int) -> bool:
    date_a, date_b = _parse_dt(a), _parse_dt(b)
    return bool(date_a and date_b and abs((date_a.date() - date_b.date()).days) <= days)


def same_job(first: dict, second: dict) -> bool:
    # 0. Evidence that does not depend on the company name (aggregators often write the
    #    employer differently: "UNDP", "UNDP Careers", "United Nations Development Programme").
    if first.get("url_job_id") and first.get("url_job_id") == second.get("url_job_id") \
            and first.get("url_job_id_namespace") == second.get("url_job_id_namespace"):
        return True
    for record, other in ((first, second), (second, first)):
        if _id_in_url(record.get("job_identifier"), other.get("url")) or \
                _id_in_url(record.get("url_job_id"), other.get("url")):
            if title_similarity(record.get("job_title"), other.get("job_title")) >= 0.6:
                return True
    # Same title and practically the same long description = a re-post of the same vacancy,
    # even under another employer name or another job ID (seen on LinkedIn).
    title_similarity_value = title_similarity(first.get("job_title") or first.get("search_title"),
                                              second.get("job_title") or second.get("search_title"))
    if title_similarity_value >= 0.9:
        description = _description_similarity(first.get("description_text"), second.get("description_text"))
        if description is not None and description >= 0.95:
            return True
    # Two different IDs from the same system = two different vacancies.
    if first.get("url_job_id") and second.get("url_job_id") \
            and first.get("url_job_id_namespace") == second.get("url_job_id_namespace"):
        return first["url_job_id"] == second["url_job_id"]
    companies_known_and_different = bool(company_tokens(first.get("company")) and company_tokens(second.get("company"))
                                         and not companies_match(first.get("company"), second.get("company")))
    title_a = first.get("job_title") or first.get("search_title")
    title_b = second.get("job_title") or second.get("search_title")
    similarity = title_similarity(title_a, title_b)
    if similarity >= 0.9 and not companies_known_and_different:
        description = _description_similarity(first.get("description_text"), second.get("description_text"))
        if description is not None and description >= 0.85:
            return True
    places_a = find_turkish_places(" ".join([first.get("location") or ""] + list(first.get("location_cities") or [])))
    places_b = find_turkish_places(" ".join([second.get("location") or ""] + list(second.get("location_cities") or [])))
    same_city = bool(set(places_a["places"]) & set(places_b["places"]))
    # Same long title, same city, same deadline or publication day -> same vacancy,
    # unless both employers are known and clearly different.
    if same_city and not companies_known_and_different and similarity >= 0.8 and (
            _same_day(first.get("application_deadline"), second.get("application_deadline"), 1)
            or _same_day(first.get("publication_date"), second.get("publication_date"), 3)):
        return True

    if not companies_match(first.get("company"), second.get("company")):
        return False

    # 1. Structured identifiers (requisition ID) - strongest evidence.
    id_a, id_b = normalize_text(first.get("job_identifier")), normalize_text(second.get("job_identifier"))
    host_a = (urlparse(first.get("url") or "").hostname or "").removeprefix("www.")
    host_b = (urlparse(second.get("url") or "").hostname or "").removeprefix("www.")
    if id_a and id_b:
        if id_a == id_b:
            return True
        if host_a == host_b:
            return False
    for record, other in ((first, second), (second, first)):   # official ID quoted in an aggregator URL
        if record.get("job_identifier") and normalize_text(record["job_identifier"]) == other.get("url_job_id"):
            return True

    # 2. URL IDs - only comparable inside the same namespace.
    if first.get("url_job_id") and first.get("url_job_id_namespace") == second.get("url_job_id_namespace") \
            and second.get("url_job_id"):
        return first["url_job_id"] == second["url_job_id"]

    # 3. Same canonical URL.
    canon_a = normalize_url(first.get("canonical_url") or first.get("url"))
    canon_b = normalize_url(second.get("canonical_url") or second.get("url"))
    if canon_a and canon_a == canon_b:
        return True

    # 4. Careful fuzzy match: same title, same city AND (close dates OR similar description).
    title_a = first.get("job_title") or first.get("search_title")
    title_b = second.get("job_title") or second.get("search_title")
    similarity = title_similarity(title_a, title_b)
    if similarity < 0.85:
        return False

    places_a = set(find_turkish_places(" ".join([first.get("location") or ""] + list(first.get("location_cities") or [])))["places"])
    places_b = set(find_turkish_places(" ".join([second.get("location") or ""] + list(second.get("location_cities") or [])))["places"])
    if places_a and places_b and not places_a & places_b:
        return False

    # A LinkedIn search result has no date/description: allow only a near-exact match.
    if "LINKEDIN_MANUAL_CHECK" in (first.get("screening_decision"), second.get("screening_decision")):
        return similarity >= 0.9 and bool(places_a & places_b)

    date_a, date_b = _parse_dt(first.get("publication_date")), _parse_dt(second.get("publication_date"))
    dates_close = bool(date_a and date_b and abs((date_a - date_b).total_seconds()) <= 3 * 24 * 3600)
    dates_far = bool(date_a and date_b and not dates_close)
    description = _description_similarity(first.get("description_text"), second.get("description_text"))
    if dates_far and (description is None or description < 0.8):
        return False
    return (dates_close and bool(places_a & places_b)) or (description is not None and description >= 0.6)


def deduplicate_jobs(records: list[dict]) -> list[dict]:
    """Group records that describe the same vacancy; keep the most official source as primary."""
    parent = list(range(len(records)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if same_job(records[i], records[j]):
                parent[find(j)] = find(i)

    groups: dict[int, list[dict]] = {}
    for index, record in enumerate(records):
        groups.setdefault(find(index), []).append(record)

    merged = []
    for group in groups.values():
        group.sort(key=lambda r: (
            SOURCE_PRIORITY.get(r.get("source_type"), 1),
            r.get("fetch_status") == "ok",
            -DECISION_ORDER.index(r.get("screening_decision", "UNVERIFIED")),
        ), reverse=True)
        primary = dict(group[0])
        # Fill facts the primary source lacks from the other copies (e.g. Oracle page without employer).
        for key in ("company", "location", "publication_date", "application_deadline", "job_identifier"):
            if not primary.get(key):
                primary[key] = next((r[key] for r in group[1:] if r.get(key)), primary.get(key))
        if len(primary.get("description_text") or "") < 200:
            longest = max(group, key=lambda r: len(r.get("description_text") or ""))
            primary["description_text"] = longest.get("description_text") or primary.get("description_text")
        primary["source_urls"] = list(dict.fromkeys(
            r.get("url") or r.get("search_url") for r in group if r.get("url") or r.get("search_url")))
        primary["search_queries"] = list(dict.fromkeys(q for r in group for q in r.get("search_queries", [])))
        primary["duplicate_source_count"] = len(group)
        primary["other_source_decisions"] = [
            {"url": r.get("url") or r.get("search_url"), "decision": r.get("screening_decision")}
            for r in group[1:]
        ]
        merged.append(primary)
    merged.sort(key=lambda r: DECISION_ORDER.index(r.get("screening_decision", "UNVERIFIED")))
    return merged


# ============================================================
# 10. PIPELINE
# ============================================================

def inspect_candidates(state: SearchState, *, inspect_fn=None) -> tuple[list[dict], dict]:
    print("\n" + "=" * 60)
    print("PRE-FILTER AND PAGE INSPECTION")
    print("=" * 60)

    candidates, prefilter_stats = [], {}
    for item in state.results:
        accepted, code = prefilter_search_result(item)
        item["prefilter_result"] = code          # saved in the JSON report for diagnosis
        prefilter_stats[code.split(":")[0]] = prefilter_stats.get(code.split(":")[0], 0) + 1
        if accepted:
            candidates.append(item)

    rejected = [item for item in state.results if item not in candidates]
    if rejected:
        print("\nSearch results removed by the pre-filter (not job pages or not a target role):")
        for item in rejected[:30]:
            print(f"  - {item['title'][:70]}  [{item['prefilter_result'][:70]}]")
        if len(rejected) > 30:
            print(f"  ... and {len(rejected) - 30} more (see the JSON report)")

    def priority(item):
        snippet_hint = any(term in normalize_text(f"{item['title']} {item['snippet']}")
                           for term in (normalize_text(settings.city()), "turkiye", "turkey"))
        return (0 if snippet_hint else 1, -SOURCE_PRIORITY.get(source_type(item["url"]), 1))

    candidates.sort(key=priority)
    print(f"Search results: {len(state.results)} | passed pre-filter: {len(candidates)}")

    records, inspected = [], 0
    for item in candidates:
        linkedin = is_linkedin_url(item["url"])
        if not linkedin and inspected >= MAX_INSPECT_CALLS:
            print("Inspection limit reached.")
            break
        if not linkedin:
            inspected += 1
            print(f"\nINSPECT {inspected}/{MAX_INSPECT_CALLS}: {item['title'][:90]}\n  {item['url']}")
        record = evaluate_search_item(item, inspect_fn=inspect_fn)
        records.append(record)
        if not linkedin:
            print(f"  -> {record['screening_decision']}: {record['screening_reason'][:200]}")
    prefilter_stats["pages_inspected"] = inspected
    return records, prefilter_stats


def run_search_phase(*, use_llm: bool = True, search_fn=None, chat_fn=None) -> SearchState:
    state = SearchState()
    if use_llm:
        plan_searches_with_llm(state, search_fn=search_fn, chat_fn=chat_fn)
    if state.search_calls < MIN_SEARCH_CALLS:
        print(f"\nOnly {state.search_calls} searches so far; adding seed queries.")
        top_up_with_seed_queries(state, search_fn=search_fn)
    return state


# ============================================================
# 11. REPORT
# ============================================================

def print_report(jobs: list[dict], state: SearchState, stats: dict) -> None:
    print("\n" + "=" * 60)
    print("FINAL JOB SCREENING REPORT")
    print("=" * 60)
    print("Searches:", state.search_calls, "| unique search results:", len(state.results),
          "| pages inspected:", stats.get("pages_inspected", 0), "| unique jobs:", len(jobs))
    counts = {d: sum(1 for j in jobs if j["screening_decision"] == d) for d in DECISION_ORDER}
    print("Decisions:", ", ".join(f"{d}={n}" for d, n in counts.items() if n) or "none")

    for decision in DECISION_ORDER:
        group = [j for j in jobs if j["screening_decision"] == decision]
        if not group:
            continue
        print(f"\n---------------- {decision} ({len(group)}) ----------------")
        for index, job in enumerate(group, start=1):
            title = job.get("job_title") or job.get("search_title") or "Unknown title"
            if decision == "REJECTED":
                print(f"[{index}] {title[:80]} -> {job['screening_reason'][:160]}")
                continue
            sources = job.get("field_sources") or {}
            print(f"\n[{index}] {title}")
            print("  Company:    ", job.get("company") or "Not verified")
            print("  Location:   ", job.get("location") or "Not verified",
                  f"({job.get('location_status')}, {job.get('work_model') or 'work model unknown'})")
            print("  Published:  ", job.get("publication_date") or "Not verified",
                  f"[source: {sources.get('publication_date', '-')}]")
            print("  Deadline:   ", job.get("application_deadline") or "Not provided")
            print("  Experience: ", job.get("experience_status"), "-", job.get("experience_reason"))
            print("  Reason:     ", job.get("screening_reason"))
            links = job.get("application_links") or []
            print("  Page:       ", job.get("url") or job.get("search_url"))
            if links:
                print("  Apply link: ", links[0]["url"])
            if job.get("duplicate_source_count", 1) > 1:
                print("  Same job also at:")
                for url in job["source_urls"][1:]:
                    print("   -", url)


def save_report(jobs: list[dict], state: SearchState, stats: dict) -> Path | None:
    """Write a NEW timestamped JSON file into Logs/ (never overwrites)."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(TIMEZONE).strftime("%Y%m%d_%H%M%S")
        path = LOG_DIR / f"job_search_{stamp}.json"
        counter = 1
        while path.exists():
            path = LOG_DIR / f"job_search_{stamp}_{counter}.json"
            counter += 1
        payload = {
            "generated_at": datetime.now(TIMEZONE).isoformat(),
            "contract_version": inspector.CONTRACT_VERSION,
            "queries": state.queries, "search_errors": state.errors, "prefilter_stats": stats,
            "jobs": jobs,
            "all_search_results": state.results,
        }
        with open(path, "x", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, default=str)
        return path
    except OSError as error:
        print("Could not save the JSON report:", error)
        return None


# ============================================================
# 12. MAIN
# ============================================================

def check_single_url(url: str) -> dict:
    item = {"title": "", "url": url, "snippet": "", "search_queries": []}
    record = evaluate_search_item(item)
    shown = dict(record)
    shown["description_text"] = (shown.get("description_text") or "")[:600]
    print(json.dumps(shown, ensure_ascii=False, indent=2, default=str))
    return record


def run_job_search(*, use_llm: bool = True, only_api: bool = False) -> tuple[list[dict], "Path | None"]:
    """Full search: job API + (optionally) web search. Returns (unique screened jobs, report path)."""
    # 1. Job API (JSearch). Skipped automatically when no RAPIDAPI_KEY is set.
    print("\n" + "=" * 60)
    print("JOB API (JSearch)")
    print("=" * 60)
    def worth_details(record):   # only fresh jobs whose title already fits the target roles
        return record.get("freshness") == "fresh" and assess_title(record.get("job_title") or "")["status"] == "ok"

    api = job_sources.fetch_jsearch_jobs(needs_details=worth_details)
    api_records = [screen_job(record, item) for record, item in api["records"]]
    if api["enabled"]:
        print(f"\nJSearch: {len(api_records)} job(s); requests this month: "
              f"{api['requests_used']}/{api['monthly_limit']}")
    for error in api["errors"]:
        print("  Note:", error)

    # 2. Web search (Qwen + DDGS) and page inspection.
    if only_api:
        state, records, stats = SearchState(), [], {}
    else:
        state = run_search_phase(use_llm=use_llm)
        records, stats = inspect_candidates(state)
    stats["jsearch"] = {k: v for k, v in api.items() if k != "records"}

    jobs = deduplicate_jobs(api_records + records)
    print_report(jobs, state, stats)
    path = save_report(jobs, state, stats)
    if path:
        print("\nJSON report saved:", path)
    return jobs, path


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="AI Career Agent - job search and verification")
    parser.add_argument("--no-llm", action="store_true", help="do not use Ollama; use seed queries")
    parser.add_argument("--url", help="inspect a single job page and print the result")
    parser.add_argument("--only-api", action="store_true", help="use only the job API (no web search)")
    args = parser.parse_args(argv)

    if args.url:
        check_single_url(args.url)
        return 0

    print("=" * 60)
    print("AI CAREER AGENT - JOB SEARCH")
    print("=" * 60)
    print("Model:", "none (seed queries)" if args.no_llm else settings.model())
    print("Time:", datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"))
    print(f"Location: {settings.city()} | date rules: LinkedIn {max_age_hours_linkedin()} h, "
          f"other sources {max_age_hours_default() // 24} days")

    run_job_search(use_llm=not args.no_llm, only_api=args.only_api)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nAgent stopped by user.")
