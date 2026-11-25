"""
settings.py
===========

All user-adjustable settings in ONE place.

    settings.json  (project folder)  -> your choices: city, roles, limits ...   (not secret)
    .env           (project folder)  -> your secrets: API key, Telegram token  (never share)

Copy settings.example.json to settings.json and edit it. Anything you leave out
uses the defaults below, so an empty settings.json also works.
"""

from __future__ import annotations

import copy
import json
import os
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SETTINGS_FILE = PROJECT_ROOT / "settings.json"
ENV_FILE = PROJECT_ROOT / ".env"

DEFAULTS = {
    "location": {
        "city": "Istanbul",          # any city in Türkiye, e.g. "Ankara", "Izmir"
        "country_code": "tr",
        "accept_other_cities_for_review": True,
        "accept_remote_in_country": True,
    },
    "search": {
        # One JSearch request per entry per day ("in <city>" is added automatically).
        "job_api_queries": [
            "data analyst intern",
            "business intelligence intern",
            "data science intern",
            "AI machine learning intern",
            "junior data analyst",
        ],
        "web_search": True,                 # extra search with Qwen + DuckDuckGo
        "job_api_monthly_limit": 190,       # free JSearch plan = 200 / month
    },
    "roles": {
        # Leave empty to use the built-in data / BI / AI / analyst role list.
        # Example for another field: ["marketing", "digital marketing", "social media"]
        "role_keywords": [],
        "level_keywords": [],               # empty = intern / junior / entry level / stajyer ...
    },
    "freshness": {
        "max_age_days": 7,                  # job boards, company sites
        "max_age_hours_linkedin": 72,       # LinkedIn jobs (no job is ever sent twice)
    },
    "notifications": {
        "max_jobs_per_day": 5,
        "send_reviews": True,               # also send "JOB TO REVIEW" jobs
        "deadline_reminder_days": 3,
        "daily_summary": True,
    },
    "documents": {
        "tailored_cv": True,
        "cover_letter": True,
    },
    "ai": {
        "model": "qwen3.5:4b",
    },
}


def _merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        elif key in result or not key.startswith("_"):
            result[key] = value
    return result


def load_env_file(path: Path = ENV_FILE) -> None:
    """Minimal .env reader (KEY=VALUE lines). Existing environment variables win."""
    if not path.exists():
        return
    try:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError as error:
        print("Could not read .env:", error)


@lru_cache(maxsize=1)
def get() -> dict:
    """Defaults <- settings.json <- old .env keys (JOB_LOCATION / JOB_COUNTRY, kept for compatibility)."""
    data = copy.deepcopy(DEFAULTS)
    if SETTINGS_FILE.exists():
        try:
            data = _merge(data, json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig")))
        except (OSError, ValueError) as error:
            print(f"settings.json could not be read ({error}); using defaults.")
    load_env_file()
    if os.environ.get("JOB_LOCATION") and not _user_set("location", "city"):
        data["location"]["city"] = os.environ["JOB_LOCATION"].strip()
    if os.environ.get("JOB_COUNTRY") and not _user_set("location", "country_code"):
        data["location"]["country_code"] = os.environ["JOB_COUNTRY"].strip().casefold()
    if os.environ.get("JSEARCH_MONTHLY_LIMIT"):
        try:
            data["search"]["job_api_monthly_limit"] = int(os.environ["JSEARCH_MONTHLY_LIMIT"])
        except ValueError:
            pass
    return data


def _user_set(section: str, key: str) -> bool:
    try:
        return key in json.loads(SETTINGS_FILE.read_text(encoding="utf-8-sig")).get(section, {})
    except (OSError, ValueError, AttributeError):
        return False


def reload() -> dict:
    get.cache_clear()
    return get()


def city() -> str:
    return str(get()["location"]["city"]).strip() or "Istanbul"


def model() -> str:
    return str(get()["ai"]["model"]).strip() or "qwen3.5:4b"
