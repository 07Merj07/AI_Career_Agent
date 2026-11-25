"""
job_sources.py
==============

Job-listing APIs (aggregators). They return real, individual job postings with
dates and full descriptions, so no site list has to be maintained by hand.

Currently supported:
    JSearch (Google for Jobs, via RapidAPI) - free "Basic" plan.

Every user puts their OWN free key in the .env file in the project folder:

    RAPIDAPI_KEY=your_key_here
    JOB_LOCATION=Istanbul
    JOB_COUNTRY=tr

The .env file must never be uploaded to GitHub (it is listed in .gitignore).

Each job is converted into the SAME data contract as job_page_inspector.py,
so job_search.py screens it with exactly the same rules.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests

import job_page_inspector as inspector
import settings as app_settings
from job_page_inspector import TIMEZONE, find_turkish_places, normalize_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
USAGE_FILE = PROJECT_ROOT / "Logs" / "jsearch_usage.json"

JSEARCH_HOST = "jsearch.p.rapidapi.com"
# JSearch v5 uses /search-v2; older versions used /search. The first one that works is remembered.
# A different address can be forced with JSEARCH_URL=... in .env.
JSEARCH_URLS = ("https://jsearch.p.rapidapi.com/search-v2", "https://jsearch.p.rapidapi.com/search")
_working_url = None
JSEARCH_TIMEOUT = (10, 40)

# The free plan allows 200 requests per month. Keep a safety margin.
DEFAULT_MONTHLY_LIMIT = 190

# One request per query per run. 6 queries x 30 days = 180 requests per month.
ROLE_QUERIES = (
    "data analyst intern",
    "business intelligence intern",
    "data science intern",
    "AI machine learning intern",
    "junior data analyst",
)
# 5 searches x 30 days = 150 requests; the rest of the free 200 is kept for job-detail calls.
MAX_DETAIL_CALLS_PER_RUN = 3
DEBUG_FILE = PROJECT_ROOT / "Logs" / "jsearch_debug.json"
JSEARCH_DETAIL_URLS = ("https://jsearch.p.rapidapi.com/job-details-v2",
                       "https://jsearch.p.rapidapi.com/job-details")


# ============================================================
# 1. SETTINGS FROM .env
# ============================================================

def load_env_file(path: Path = None) -> None:
    """Kept for compatibility: the .env reader now lives in settings.py."""
    app_settings.load_env_file(path or ENV_FILE)


def settings() -> dict:
    """Secret key from .env; city, country, queries and limits from settings.json."""
    config = app_settings.get()
    load_env_file()
    key = os.environ.get("RAPIDAPI_KEY", "").strip()
    if key.casefold() in ("", "paste_your_key_here", "your_key_here"):
        key = ""
    try:
        limit = int(os.environ.get("JSEARCH_MONTHLY_LIMIT") or config["search"]["job_api_monthly_limit"])
    except (TypeError, ValueError):
        limit = DEFAULT_MONTHLY_LIMIT
    return {
        "key": key,
        "location": app_settings.city(),
        "country": str(config["location"]["country_code"]).strip().casefold() or "tr",
        "monthly_limit": limit,
        "queries": [str(q).strip() for q in config["search"]["job_api_queries"] if str(q).strip()],
    }


# ============================================================
# 2. MONTHLY QUOTA (so the free plan is never exceeded)
# ============================================================

def _read_usage() -> dict:
    month = datetime.now(TIMEZONE).strftime("%Y-%m")
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        if data.get("month") == month:
            return data
    except (OSError, ValueError):
        pass
    return {"month": month, "requests": 0}


def _save_usage(data: dict) -> None:
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as error:
        print("Could not save JSearch usage counter:", error)


# ============================================================
# 3. JSEARCH REQUEST
# ============================================================

def jsearch_request(query: str, *, key: str, country: str, date_posted: str = "week",
                    http_get=None) -> dict:
    """One JSearch call. Returns {"ok", "jobs", "error"}; never raises."""
    global _working_url
    http_get = http_get or requests.get
    forced = os.environ.get("JSEARCH_URL", "").strip()
    candidates = [forced] if forced else ([_working_url] if _working_url else list(JSEARCH_URLS))
    response = None
    for url in candidates:
        try:
            response = http_get(
                url,
                params={"query": query, "page": "1", "num_pages": "1",
                        "country": country, "date_posted": date_posted},
                headers={"x-rapidapi-key": key, "x-rapidapi-host": JSEARCH_HOST},
                timeout=JSEARCH_TIMEOUT,
            )
        except requests.RequestException as error:
            return {"ok": False, "jobs": [], "error": f"Network error: {error}"}
        if response.status_code != 404:
            _working_url = url
            break
    if response is None or response.status_code == 404:
        return {"ok": False, "jobs": [], "error": "Endpoint not found (HTTP 404). Copy the URL from the "
                "JSearch 'Code snippet' tab into .env as JSEARCH_URL=..."}

    if response.status_code in (401, 403):
        return {"ok": False, "jobs": [], "error": "API key rejected (check RAPIDAPI_KEY and the JSearch subscription)."}
    if response.status_code == 429:
        return {"ok": False, "jobs": [], "error": "Monthly free quota or rate limit reached."}
    if response.status_code != 200:
        return {"ok": False, "jobs": [], "error": f"HTTP {response.status_code}"}
    try:
        payload = response.json()
    except ValueError:
        return {"ok": False, "jobs": [], "error": "Response was not JSON."}
    jobs = (payload.get("data") or {}).get("jobs") if isinstance(payload.get("data"), dict) else payload.get("data")
    if not isinstance(jobs, list):
        return {"ok": False, "jobs": [], "error": f"Unexpected response: {str(payload)[:200]}"}
    return {"ok": True, "jobs": [job for job in jobs if isinstance(job, dict)], "error": None}


# ============================================================
# 4. CONVERT A JSEARCH JOB INTO THE INSPECTOR CONTRACT
# ============================================================

def _posted_date(job: dict):
    raw = job.get("job_posted_at_datetime_utc")
    if not raw:
        timestamp = job.get("job_posted_at_timestamp")
        if isinstance(timestamp, (int, float)):
            raw = datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()
    published, _ = inspector.parse_iso_datetime(raw) if raw else (None, None)
    if published is None:
        return None, None
    # LinkedIn / Google show "today", "3 days ago": only the DAY is reliable.
    relative = normalize_text(job.get("job_posted_at"))
    precision = "datetime" if re.search(r"\b(hour|minute|saat|dakika)", relative) else "date"
    return published, precision


def job_description(job: dict) -> str:
    """Description text from whichever field the API filled."""
    for key in ("job_description", "description", "job_description_text"):
        if str(job.get(key) or "").strip():
            return str(job[key]).strip()
    highlights = job.get("job_highlights")
    if isinstance(highlights, dict) and highlights:
        parts = []
        for heading, items in highlights.items():
            parts.append(str(heading))
            parts += [f"- {item}" for item in (items if isinstance(items, list) else [items])]
        return "\n".join(parts)
    return ""


def fetch_job_description(job_id: str, *, key: str, country: str, http_get=None) -> str:
    """One 'Job Details' call for a job whose search result had no description."""
    http_get = http_get or requests.get
    for url in JSEARCH_DETAIL_URLS:
        try:
            response = http_get(url, params={"job_id": job_id, "country": country},
                                headers={"x-rapidapi-key": key, "x-rapidapi-host": JSEARCH_HOST},
                                timeout=JSEARCH_TIMEOUT)
        except requests.RequestException:
            return ""
        if response.status_code == 404:
            continue
        if response.status_code != 200:
            return ""
        try:
            data = response.json().get("data")
        except (ValueError, AttributeError):
            return ""
        if isinstance(data, dict):
            data = data.get("jobs") or [data]
        if isinstance(data, list) and data and isinstance(data[0], dict):
            return job_description(data[0])
        return ""
    return ""


def _write_debug(url_used: str, jobs: list) -> None:
    """Keep a tiny sample of the API's answer, so format changes can be diagnosed."""
    try:
        first = jobs[0] if jobs else {}
        DEBUG_FILE.parent.mkdir(parents=True, exist_ok=True)
        DEBUG_FILE.write_text(json.dumps({
            "checked_at": datetime.now(TIMEZONE).isoformat(), "url": url_used, "jobs": len(jobs),
            "fields_of_first_job": sorted(first.keys()),
            "description_length": len(job_description(first)),
        }, indent=2), encoding="utf-8")
    except OSError:
        pass


def jsearch_job_to_record(job: dict, *, now=None, max_age_hours=None) -> dict:
    """Map one JSearch job onto the shared result contract (all fields present)."""
    now = now or datetime.now(TIMEZONE)
    apply_links = []
    for option in job.get("apply_options") or []:
        link = str(option.get("apply_link") or "").strip()
        if link and inspector.check_url(link, resolve_dns=False)[0]:
            apply_links.append({"text": f"Apply via {option.get('publisher') or 'source'}", "url": link})
    main_url = str(job.get("job_apply_link") or (apply_links[0]["url"] if apply_links else "")).strip()
    is_linkedin = "linkedin" in normalize_text(job.get("job_publisher")) or "linkedin.com" in main_url.casefold()
    if max_age_hours is None:
        freshness = app_settings.get()["freshness"]
        max_age_hours = int(freshness["max_age_hours_linkedin"]) if is_linkedin else int(freshness["max_age_days"]) * 24

    record = inspector.empty_result(main_url)
    record.update({
        "url": main_url or None,
        "http_status": None,
        "fetch_status": "ok",
        "data_source": "jsearch_api",
        "job_title": inspector._clean_inline(job.get("job_title")),
        "company": inspector._clean_inline(job.get("employer_name")),
        "description_text": job_description(job)[:inspector.MAX_DESCRIPTION_CHARS],
        "jsearch_job_id": job.get("job_id"),
        "job_identifier": None,
        "application_links": apply_links[:5],
        "application_status": "apply_link_found" if apply_links else "no_apply_link_found",
        "checked_at": now.isoformat(),
        "max_age_hours": max_age_hours,
    })

    # Location: job_country is unreliable in JSearch (often "US"), so it is ignored.
    location_text = inspector._clean_inline(job.get("job_location")) or ", ".join(
        str(part) for part in (job.get("job_city"), job.get("job_state")) if part)
    record["location"] = location_text or None
    found = find_turkish_places(" ".join(str(p) for p in (location_text, job.get("job_city"), job.get("job_state")) if p))
    record["location_cities"] = [p.title() for p in found["places"]] or (
        [str(job["job_city"])] if job.get("job_city") else [])
    record["location_countries"] = ["TR"] if found["turkey"] else []

    if job.get("job_is_remote") is True:
        record["work_model"] = "remote"
        record["remote_turkey_eligible"] = True if found["turkey"] else None
    else:
        record["work_model"] = inspector._detect_work_model(record["job_title"], location_text)

    namespace, url_id = inspector.extract_url_job_id(main_url)
    record["url_job_id_namespace"], record["url_job_id"] = namespace, url_id

    published, precision = _posted_date(job)
    record["publication_date"] = published.isoformat() if published else None
    record["publication_date_precision"] = precision
    record["freshness"], record["age_hours"] = inspector._assess_freshness(published, precision, now, max_age_hours)
    record["field_sources"] = {name: "jsearch_api" for name in (
        "job_title", "company", "location", "description_text", "publication_date")}

    if record["freshness"] == "too_old":
        return inspector._finish(record, "REJECTED", ["date_too_old"])
    codes = []
    if record["freshness"] == "not_found":
        codes.append("date_not_found")
    elif record["freshness"] == "future_date":
        codes.append("future_date")
    if not record["job_title"]:
        codes.append("no_title")
    return inspector._finish(record, "UNVERIFIED" if codes else "OK", codes)


# ============================================================
# 5. MAIN FUNCTION USED BY THE AGENT
# ============================================================

def fetch_jsearch_jobs(*, http_get=None, queries=None, needs_details=None) -> dict:
    """
    Run the JSearch queries within the monthly quota.
    Returns {"enabled", "records": [(record, search_item)], "requests_used", "errors"}.
    """
    config = settings()
    if not config["key"]:
        return {"enabled": False, "records": [], "requests_used": 0,
                "errors": ["RAPIDAPI_KEY not set in .env - JSearch skipped."]}

    queries = list(queries) if queries is not None else (config["queries"] or list(ROLE_QUERIES))
    usage = _read_usage()
    records, errors, seen_ids = [], [], set()
    for role in queries:
        if usage["requests"] >= config["monthly_limit"]:
            errors.append(f"Monthly JSearch limit reached ({usage['requests']}/{config['monthly_limit']}).")
            break
        query = f"{role} in {config['location']}"
        print(f"\nJSEARCH: {query}  (country={config['country']}, past week)")
        result = jsearch_request(query, key=config["key"], country=config["country"], http_get=http_get)
        usage["requests"] += 1
        _save_usage(usage)
        if not result["ok"]:
            print("  JSEARCH ERROR:", result["error"])
            errors.append(f"{query}: {result['error']}")
            if any(word in result["error"] for word in ("key rejected", "quota", "Endpoint not found")):
                break   # the same error would repeat for every query - do not waste requests
            continue

        if result["jobs"]:
            _write_debug(_working_url or "", result["jobs"])
        new = 0
        for job in result["jobs"]:
            job_key = job.get("job_id") or job.get("job_apply_link")
            if not job_key or job_key in seen_ids:
                continue
            seen_ids.add(job_key)
            record = jsearch_job_to_record(job)
            item = {"title": job.get("job_title") or "", "url": record.get("url") or "",
                    "snippet": "", "search_queries": [f"jsearch: {query}"], "search_time_filter": "jsearch_week"}
            records.append((record, item))
            new += 1
        print(f"  {len(result['jobs'])} job(s), {new} new")

    # Fill missing descriptions, but only for jobs that look suitable (saves the free quota).
    detail_calls = 0
    for record, _ in records:
        if len(record.get("description_text") or "") >= 200 or not record.get("jsearch_job_id"):
            continue
        if needs_details is not None and not needs_details(record):
            continue
        if detail_calls >= MAX_DETAIL_CALLS_PER_RUN or usage["requests"] >= config["monthly_limit"]:
            errors.append("Some descriptions are missing (detail-call limit reached).")
            break
        print(f"\nJSEARCH DETAILS: {record.get('job_title')} - {record.get('company')}")
        text = fetch_job_description(record["jsearch_job_id"], key=config["key"],
                                     country=config["country"], http_get=http_get)
        detail_calls += 1
        usage["requests"] += 1
        _save_usage(usage)
        if text:
            record["description_text"] = text[:inspector.MAX_DESCRIPTION_CHARS]
            record["field_sources"]["description_text"] = "jsearch_api.job_details"
            print(f"  description: {len(text)} characters")
        else:
            print("  no description available")

    return {"enabled": True, "records": records, "requests_used": usage["requests"],
            "monthly_limit": config["monthly_limit"], "errors": errors}
