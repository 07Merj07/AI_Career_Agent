"""
job_store.py
============

Saves screened jobs into Database/career_agent.db
and remembers which jobs were already sent, so nothing is sent twice.

A job is recognised as "already known" when
  1. one of its URLs is already in job_sources, or
  2. its fingerprint (company + title + city) is already in jobs.dedup_key.

Status flow used here: new -> cv_generated -> sent   (ignored = rejected jobs)
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from job_page_inspector import TIMEZONE, find_turkish_places, normalize_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_FILE = PROJECT_ROOT / "Database" / "career_agent.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key TEXT NOT NULL UNIQUE,
    job_title TEXT NOT NULL,
    company TEXT NOT NULL,
    location TEXT,
    work_type TEXT,
    raw_description TEXT,
    language TEXT,
    status TEXT NOT NULL DEFAULT 'new'
        CHECK (status IN ('new','analyzed','matched','cv_generated','sent','ignored','closed')),
    analysis_json TEXT,
    match_score REAL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    posted_at TEXT,
    last_sent_at TEXT
);
CREATE TABLE IF NOT EXISTS job_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
    UNIQUE(source_name, source_url)
);
CREATE TABLE IF NOT EXISTS user_actions (
    job_id INTEGER PRIMARY KEY,
    action TEXT NOT NULL CHECK (action IN ('applied', 'not_interested')),
    acted_at TEXT NOT NULL,
    FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS telegram_messages (
    job_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (job_id, message_id)
);
CREATE TABLE IF NOT EXISTS reminders (
    job_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (job_id, kind)
);
CREATE TABLE IF NOT EXISTS bot_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company);
"""


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def connect(db_file: Path = DB_FILE) -> sqlite3.Connection:
    db_file.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_file)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)     # IF NOT EXISTS: never changes existing data
    columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
    for column in ("posted_at", "last_sent_at"):
        if column not in columns:
            connection.execute(f"ALTER TABLE jobs ADD COLUMN {column} TEXT")
    return connection


def clean_url(url) -> str:
    """Same URL from different searches -> same text (no tracking, no www, no fragment)."""
    try:
        parsed = urlparse(str(url or "").strip())
    except ValueError:
        return ""
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return ""
    host = parsed.hostname.casefold().removeprefix("www.")
    if host.endswith("linkedin.com"):
        host = "linkedin.com"                  # tr.linkedin.com == www.linkedin.com
    query = "" if host == "linkedin.com" else parsed.query
    path = parsed.path.rstrip("/")
    return f"https://{host}{path}" + (f"?{query}" if query else "")


def fingerprint(job: dict) -> str:
    company = " ".join(re.findall(r"[a-z0-9]+", normalize_text(job.get("company"))))
    title = " ".join(re.findall(r"[a-z0-9]+", normalize_text(job.get("job_title") or job.get("search_title"))))
    places = find_turkish_places(" ".join([job.get("location") or ""] + list(job.get("location_cities") or [])))
    city = "istanbul" if places["istanbul"] else (places["places"][0] if places["places"] else "")
    return f"fp:{company}|{title}|{city}"


def job_urls(job: dict) -> list[str]:
    urls = list(job.get("source_urls") or []) + [job.get("url"), job.get("search_url"), job.get("canonical_url")]
    return list(dict.fromkeys(u for u in (clean_url(x) for x in urls) if u))


DEDUP_FIELDS = ("job_title", "company", "location", "location_cities", "publication_date",
                "application_deadline", "job_identifier", "url_job_id", "url_job_id_namespace", "url")


def _stored_record(row) -> dict:
    try:
        stored = (json.loads(row["analysis_json"]) if row["analysis_json"] else {}).get("dedup") or {}
    except ValueError:
        stored = {}
    stored.setdefault("job_title", row["job_title"])
    stored.setdefault("company", row["company"])
    stored.setdefault("location", row["location"])
    stored["description_text"] = row["raw_description"] or ""
    return stored


def find_existing(connection, job: dict, same_job_fn=None):
    for url in job_urls(job):
        row = connection.execute(
            "SELECT jobs.* FROM job_sources JOIN jobs ON jobs.id = job_sources.job_id "
            "WHERE job_sources.source_url = ?", (url,)).fetchone()
        if row:
            return row
    row = connection.execute("SELECT * FROM jobs WHERE dedup_key = ?", (fingerprint(job),)).fetchone()
    if row or same_job_fn is None:
        return row
    # Same vacancy published on another site with a different employer spelling or URL:
    # compare with recent jobs using the same rules as the search (job_search.same_job).
    for candidate in connection.execute(
            "SELECT * FROM jobs WHERE created_at >= datetime('now', '-60 days') OR status != 'ignored' "
            "ORDER BY id DESC LIMIT 500"):
        if same_job_fn(job, _stored_record(candidate)):
            return candidate
    return None


def save_job(connection, job: dict, same_job_fn=None) -> tuple[int, bool, str]:
    """Insert or update one screened job. Returns (job_id, is_new, current_status)."""
    now = _now()
    existing = find_existing(connection, job, same_job_fn)
    analysis = json.dumps({
        "screening_decision": job.get("screening_decision"),
        "screening_reason": job.get("screening_reason"),
        "experience_status": job.get("experience_status"),
        "location_status": job.get("location_status"),
        "review_flags": job.get("review_flags"),
        "data_source": job.get("data_source"),
        "application_links": job.get("application_links"),
        "dedup": {key: job.get(key) for key in DEDUP_FIELDS},
    }, ensure_ascii=False)
    decision = job.get("screening_decision")
    if existing is None:
        status = "ignored" if decision == "REJECTED" else "new"
        cursor = connection.execute(
            "INSERT INTO jobs (dedup_key, job_title, company, location, work_type, raw_description, language,"
            " status, analysis_json, created_at, updated_at, posted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (fingerprint(job), job.get("job_title") or job.get("search_title") or "Unknown title",
             job.get("company") or "Unknown company", job.get("location"), job.get("work_model"),
             job.get("description_text"), None, status, analysis, now, now, job.get("publication_date")))
        job_id, is_new = cursor.lastrowid, True
    else:
        job_id, is_new, status = existing["id"], False, existing["status"]
        # Keep facts we already know (e.g. the deadline) when a new copy of the job lacks them.
        try:
            old = json.loads(existing["analysis_json"]) if existing["analysis_json"] else {}
        except ValueError:
            old = {}
        merged = json.loads(analysis)
        merged["dedup"] = {**(old.get("dedup") or {}),
                           **{k: v for k, v in (merged.get("dedup") or {}).items() if v not in (None, "", [])}}
        if "match" in old:
            merged["match"] = old["match"]
        connection.execute("UPDATE jobs SET updated_at = ?, analysis_json = ? WHERE id = ?",
                           (now, json.dumps(merged, ensure_ascii=False), job_id))

    for url in job_urls(job):
        connection.execute(
            "INSERT INTO job_sources (job_id, source_name, source_url, first_seen_at, last_seen_at) "
            "VALUES (?,?,?,?,?) ON CONFLICT(source_name, source_url) DO UPDATE SET last_seen_at = excluded.last_seen_at",
            (job_id, urlparse(url).hostname or "unknown", url, now, now))
    connection.commit()
    return job_id, is_new, status


def set_status(connection, job_id: int, status: str, *, sent: bool = False) -> None:
    now = _now()
    if sent:
        connection.execute("UPDATE jobs SET status = ?, updated_at = ?, last_sent_at = ? WHERE id = ?",
                           (status, now, now, job_id))
    else:
        connection.execute("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?", (status, now, job_id))
    connection.commit()


def set_match(connection, job_id: int, match: dict) -> None:
    row = connection.execute("SELECT analysis_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
    try:
        analysis = json.loads(row["analysis_json"]) if row and row["analysis_json"] else {}
    except ValueError:
        analysis = {}
    analysis["match"] = {k: match.get(k) for k in ("score", "label", "requirements", "error")}
    connection.execute("UPDATE jobs SET match_score = ?, analysis_json = ?, updated_at = ? WHERE id = ?",
                       (match.get("score"), json.dumps(analysis, ensure_ascii=False), _now(), job_id))
    connection.commit()


def was_sent(connection, job_id: int) -> bool:
    row = connection.execute("SELECT last_sent_at FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row["last_sent_at"])


def summary(db_file: Path = DB_FILE) -> dict:
    with closing(connect(db_file)) as connection:
        return {row["status"]: row["n"] for row in
                connection.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")}


# ============================================================
# Your answers from Telegram ("Applied" / "Not interested") and reminders
# ============================================================

def set_action(connection, job_id: int, action: str) -> bool:
    if action not in ("applied", "not_interested"):
        return False
    if not connection.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone():
        return False
    connection.execute(
        "INSERT INTO user_actions (job_id, action, acted_at) VALUES (?,?,?) "
        "ON CONFLICT(job_id) DO UPDATE SET action = excluded.action, acted_at = excluded.acted_at",
        (job_id, action, _now()))
    connection.commit()
    return True


def get_action(connection, job_id: int) -> str | None:
    row = connection.execute("SELECT action FROM user_actions WHERE job_id = ?", (job_id,)).fetchone()
    return row["action"] if row else None


def save_telegram_message(connection, job_id: int, message_id: int) -> None:
    connection.execute("INSERT OR IGNORE INTO telegram_messages (job_id, message_id, sent_at) VALUES (?,?,?)",
                       (job_id, message_id, _now()))
    connection.commit()


def get_state(connection, key: str, default=None):
    row = connection.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_state(connection, key: str, value) -> None:
    connection.execute("INSERT INTO bot_state (key, value) VALUES (?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
    connection.commit()


def _deadline(row):
    try:
        analysis = json.loads(row["analysis_json"]) if row["analysis_json"] else {}
    except ValueError:
        return None
    value = (analysis.get("dedup") or {}).get("application_deadline") or analysis.get("application_deadline")
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def sent_jobs(connection) -> list[dict]:
    """Every job you were notified about, with your answer and its deadline."""
    rows = connection.execute(
        "SELECT jobs.*, user_actions.action FROM jobs LEFT JOIN user_actions ON user_actions.job_id = jobs.id "
        "WHERE jobs.last_sent_at IS NOT NULL ORDER BY jobs.last_sent_at DESC").fetchall()
    result = []
    for row in rows:
        url = connection.execute("SELECT source_url FROM job_sources WHERE job_id = ? ORDER BY id LIMIT 1",
                                 (row["id"],)).fetchone()
        result.append({"id": row["id"], "job_title": row["job_title"], "company": row["company"],
                       "action": row["action"], "deadline": _deadline(row), "match_score": row["match_score"],
                       "sent_at": row["last_sent_at"], "url": url["source_url"] if url else None})
    return result


def load_job(connection, job_id: int) -> dict | None:
    """Rebuilds a saved job (title, company, description, links...) from the database."""
    row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    try:
        analysis = json.loads(row["analysis_json"]) if row["analysis_json"] else {}
    except ValueError:
        analysis = {}
    job = {key: value for key, value in (analysis.get("dedup") or {}).items() if value not in (None, "", [])}
    for key in ("screening_decision", "screening_reason", "experience_status", "location_status",
                "review_flags", "data_source", "application_links"):
        if analysis.get(key) is not None:
            job[key] = analysis[key]
    job.setdefault("job_title", row["job_title"])
    job.setdefault("company", row["company"])
    job.setdefault("location", row["location"])
    job["work_model"] = row["work_type"]
    job["description_text"] = row["raw_description"] or ""
    job["db_job_id"] = row["id"]
    if not job.get("url"):
        source = connection.execute("SELECT source_url FROM job_sources WHERE job_id = ? ORDER BY id LIMIT 1",
                                    (job_id,)).fetchone()
        job["url"] = source["source_url"] if source else None
    return job


def due_reminders(connection, days: int, now=None) -> list[dict]:
    """Sent jobs without an answer (or marked applied? no) whose deadline is within `days` days."""
    now = now or datetime.now(TIMEZONE)
    due = []
    for job in sent_jobs(connection):
        deadline = job["deadline"]
        if job["action"] is not None or deadline is None:
            continue
        days_left = (deadline.date() - now.date()).days
        if 0 <= days_left <= days and not connection.execute(
                "SELECT 1 FROM reminders WHERE job_id = ? AND kind = 'deadline'", (job["id"],)).fetchone():
            due.append({**job, "days_left": days_left})
    return due


def mark_reminded(connection, job_id: int, kind: str = "deadline") -> None:
    connection.execute("INSERT OR IGNORE INTO reminders (job_id, kind, sent_at) VALUES (?,?,?)", (job_id, kind, _now()))
    connection.commit()
