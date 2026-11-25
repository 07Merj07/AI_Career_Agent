"""
daily_pipeline.py
=================

The whole daily job, in one command:

    1. search and screen jobs          (job_search.run_job_search)
    2. save them in the database       (job_store) - already-sent jobs are skipped
    3. create a tailored CV per job    (cv_tailor: Word + PDF)
    4. send message + both CV files    (telegram_notifier)
    5. send a short daily summary

The system never applies to a job for you. You check and apply yourself.

Usage (from the AI_Career_Agent folder):
    python Python\\daily_pipeline.py              normal daily run
    python Python\\daily_pipeline.py --only-api   job API only (faster, no web search)
    python Python\\daily_pipeline.py --dry-run    everything except Telegram (prints instead)
"""

from __future__ import annotations

import argparse
import sys
import traceback
from contextlib import closing
from datetime import datetime
from pathlib import Path

import cover_letter
import cv_tailor
import job_store
import settings
import telegram_inbox
import match_scorer
import telegram_notifier
import job_search
from job_page_inspector import TIMEZONE

SEND_DECISIONS = ("CANDIDATE", "MANUAL_REVIEW")   # LinkedIn-manual and unverified jobs are not sent
MAX_JOBS_PER_RUN = 5                             # default; the real value comes from settings.json


def send_decisions() -> tuple:
    return SEND_DECISIONS if settings.get()["notifications"]["send_reviews"] else ("CANDIDATE",)


def process_jobs(jobs, *, connection, notify, tailor, scorer=None, letter=None, max_jobs=None) -> dict:
    """Store all jobs, then create CVs and notify for new suitable ones. Returns counters."""
    stats = {"found": len(jobs), "suitable": 0, "already_sent": 0, "sent": 0, "cv_followups": 0,
             "cv_failed": 0, "notify_failed": 0, "skipped_limit": 0}
    if max_jobs is None:
        max_jobs = int(settings.get()["notifications"]["max_jobs_per_day"])
    wanted = send_decisions()
    queue = []
    for job in jobs:
        job_id, _, status = job_store.save_job(connection, job, same_job_fn=job_search.same_job)
        job["db_job_id"] = job_id
        if job.get("screening_decision") not in wanted:
            continue
        stats["suitable"] += 1
        if status == "matched":            # message was sent earlier, but without a CV
            queue.append((job_id, job, "cv_only"))
            continue
        if job_store.was_sent(connection, job_id) or status == "sent":
            stats["already_sent"] += 1
            continue
        queue.append((job_id, job, "full"))

    # Match score for new jobs (Qwen + Python checks); best matches are sent first.
    for job_id, job, mode in queue:
        if mode == "full" and scorer is not None:
            print(f"\nSCORING: {job.get('job_title')} - {job.get('company')}")
            job["match"] = scorer(job)
            print(f"  {job['match'].get('score')} ({job['match'].get('label')})")
            job_store.set_match(connection, job_id, job["match"])
    queue.sort(key=lambda entry: (SEND_DECISIONS.index(entry[1]["screening_decision"]),
                                  -((entry[1].get("match") or {}).get("score") or 0)))
    for position, (job_id, job, mode) in enumerate(queue):
        if position >= max_jobs:
            stats["skipped_limit"] += 1      # stays "new": it will be sent on the next run
            continue
        title = f"{job.get('job_title')} - {job.get('company')}"
        print(f"\nPREPARING: {title}")

        cv = tailor(job)
        cv_ok = cv.get("status") in ("ok", "skipped")       # "skipped" = tailored CV switched off
        if not cv_ok:
            stats["cv_failed"] += 1
            print("  CV not created:", cv.get("message"))
            if mode == "cv_only":
                continue                     # still no CV: try again on a later run
        elif mode == "full":
            job_store.set_status(connection, job_id, "cv_generated")
        if letter is not None and mode == "full":
            cv["cover_letter"] = letter(job, cv.get("folder"))
            if cv["cover_letter"].get("status") != "ok":
                print("  Cover letter not created:", cv["cover_letter"].get("message"))

        try:
            message_id = notify(job, cv, cv_only=(mode == "cv_only"))
        except Exception as error:   # keep status: it will be retried next run
            stats["notify_failed"] += 1
            print("  Telegram failed:", error)
            continue
        if isinstance(message_id, int):
            job_store.save_telegram_message(connection, job_id, message_id)
        # "matched" = message sent without CV; the CV follows when it can be created.
        job_store.set_status(connection, job_id, "sent" if cv_ok else "matched", sent=True)
        stats["sent" if mode == "full" else "cv_followups"] += 1
        print("  Sent." if cv_ok else "  Sent without CV (CV will follow when possible).")
    return stats


def _caption(kind: str, extension: str, job: dict) -> str:
    return f"{kind} ({extension.upper()}) - {job.get('job_title')}" + (f" - {job['company']}" if job.get("company") else "")


def telegram_notify(job: dict, cv: dict, *, cv_only: bool = False):
    """Sends the job message (with Applied / Not interested buttons), CV and cover letter. Returns message_id."""
    if cv_only:
        telegram_notifier.send_message(
            "<b>Tailored CV for a job sent earlier:</b>\n" +
            telegram_notifier._e(f"{job.get('job_title')} - {job.get('company')}"))
        for key in ("docx", "pdf"):
            if cv.get(key):
                telegram_notifier.send_file(cv[key], caption=_caption("CV", key, job))
        return
    text = telegram_notifier.job_message(job)
    if cv.get("status") not in ("ok", "skipped"):
        text += "\n\n<i>Tailored CV could not be created: " + telegram_notifier._e(cv.get("message")) + "</i>"
    buttons = telegram_notifier.job_buttons(job["db_job_id"]) if job.get("db_job_id") else None
    message_id = telegram_notifier.send_message(text, buttons=buttons)
    for key in ("docx", "pdf"):
        if cv.get(key):
            telegram_notifier.send_file(cv[key], caption=_caption("CV", key, job))
    letter = cv.get("cover_letter") or {}
    for key in ("docx", "pdf"):
        if letter.get(key):
            telegram_notifier.send_file(letter[key], caption=_caption("Cover letter", key, job))
    return message_id


def print_notify(job: dict, cv: dict, *, cv_only: bool = False) -> None:
    print("  [dry-run] Telegram message:\n" + telegram_notifier.job_message(job))
    letter = cv.get("cover_letter") or {}
    print("  [dry-run] files:", cv.get("docx"), cv.get("pdf"), letter.get("docx"), letter.get("pdf"))


def summary_text(stats: dict) -> str:
    today = datetime.now(TIMEZONE).strftime("%d.%m.%Y")
    if stats["sent"] == 0 and stats["suitable"] == stats["already_sent"]:
        head = f"Daily search {today}: no new suitable jobs today."
    else:
        head = f"Daily search {today}: {stats['sent']} new job(s) sent."
    details = [f"Jobs checked: {stats['found']}", f"Suitable: {stats['suitable']}",
               f"Already sent before: {stats['already_sent']}"]
    if stats["skipped_limit"]:
        details.append(f"Waiting for tomorrow (daily limit): {stats['skipped_limit']}")
    if stats.get("cv_followups"):
        details.append(f"CVs sent for earlier jobs: {stats['cv_followups']}")
    if stats["cv_failed"]:
        details.append(f"CV problems: {stats['cv_failed']} (missing job description or Ollama not running)")
    if stats["notify_failed"]:
        details.append(f"Telegram problems: {stats['notify_failed']} (will retry next run)")
    if stats.get("applied_total") is not None:
        details.append(f"You applied to {stats['applied_total']} job(s) so far. Type /status to see them.")
    return head + "\n" + "\n".join(details)


def test_latest(*, dry_run: bool = False) -> int:
    """Try the Telegram message + match score on the newest report without touching anything else."""
    import json
    report = cv_tailor.newest_report()
    if report is None:
        print("No job search report found in Logs.")
        return 1
    jobs = [j for j in json.loads(report.read_text(encoding="utf-8")).get("jobs", [])
            if j.get("screening_decision") in send_decisions()]
    print("Report:", report, "| suitable jobs:", len(jobs))
    for job in jobs[:int(settings.get()["notifications"]["max_jobs_per_day"])]:
        print(f"\nSCORING: {job.get('job_title')} - {job.get('company')}")
        job["match"] = match_scorer.score_job(job)
        print(f"  {job['match'].get('score')} ({job['match'].get('label')})  {job['match'].get('error') or ''}")
        for requirement in job["match"].get("requirements", []):
            print(f"   [{requirement['status']:8}] ({requirement['type']}) {requirement['requirement']}"
                  f"  {requirement['evidence'] or requirement['note']}")
        text = "<b>[TEST]</b>\n" + telegram_notifier.job_message(job)
        if dry_run:
            print(text)
        else:
            telegram_notifier.send_message(text)
            print("  Sent to Telegram.")
    return 0


def full_test(*, job_id: int | None = None) -> int:
    """End-to-end check on a job you already received: score, CV, cover letter, Telegram with buttons.
    Nothing is searched and the job's status in the database is not changed."""
    print("FULL TEST - every step on one real job\n")
    with closing(job_store.connect(job_store.DB_FILE)) as connection:
        if job_id is None:
            sent = [row["id"] for row in job_store.sent_jobs(connection)]   # newest first
            if not sent:
                print("No job has been sent yet. Run run_daily.bat once first.")
                return 1
            with_text = [i for i in sent
                         if len((job_store.load_job(connection, i) or {}).get("description_text") or "") >= 200]
            job_id = (with_text or sent)[0]
        job = job_store.load_job(connection, job_id)
    if job is None:
        print("Job", job_id, "not found.")
        return 1
    results = []

    def step(name, ok, detail=""):
        results.append(ok)
        print(f"[{'OK' if ok else 'FAIL':4}] {name}" + (f"  - {detail}" if detail else ""))

    print(f"Job #{job_id}: {job.get('job_title')} - {job.get('company')}"
          f"  (description: {len(job.get('description_text') or '')} characters)\n")
    job["match"] = match_scorer.score_job(job)
    step("Match score (Ollama)", job["match"].get("score") is not None,
         f"{job['match'].get('score')}/100 {job['match'].get('label') or job['match'].get('error') or ''}")
    cv = cv_tailor.tailor_for_job(job)
    step("Tailored CV (Word)", bool(cv.get("docx")), cv.get("docx") or cv.get("message") or "")
    step("Tailored CV (PDF)", bool(cv.get("pdf")), cv.get("pdf") or "no PDF")
    cv["cover_letter"] = cover_letter.create_for_job(job, cv.get("folder"))
    letter = cv["cover_letter"]
    step("Cover letter", letter.get("status") == "ok", letter.get("docx") or letter.get("message") or "")
    try:
        telegram_notifier.send_message("<b>[TEST] Full check</b> - the next message is a test copy of a job "
                                       "you already received. Its buttons work for that job.")
        message_id = telegram_notify(job, cv)
        step("Telegram message + buttons + files", isinstance(message_id, int), f"message id {message_id}")
    except Exception as error:
        step("Telegram message + buttons + files", False, str(error))
    print(f"\n{sum(results)}/{len(results)} steps OK.")
    if all(results):
        print("Now press a button under the test message and type /status in Telegram."
              + ("" if telegram_inbox.listener_running() else
                 "\nThe bot is not running, so then run:  .venv\\Scripts\\python.exe Python\\telegram_inbox.py"))
    return 0 if all(results) else 1


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="AI Career Agent - daily run")
    parser.add_argument("--only-api", action="store_true", help="job API only (no web search)")
    parser.add_argument("--no-llm", action="store_true", help="web search without Qwen query planning")
    parser.add_argument("--dry-run", action="store_true", help="do not send Telegram messages")
    parser.add_argument("--test-latest", action="store_true",
                        help="score the suitable jobs of the newest report and send them as [TEST] "
                             "(no search, no CV, database unchanged)")
    parser.add_argument("--full-test", action="store_true",
                        help="run score + CV + cover letter + Telegram (with buttons) on a job you already "
                             "received; no search, job status unchanged")
    parser.add_argument("--job-id", type=int, help="with --full-test: which job (default: the newest one)")
    args = parser.parse_args(argv)
    if args.full_test:
        return full_test(job_id=args.job_id)
    if args.test_latest:
        return test_latest(dry_run=args.dry_run)
    notify = print_notify if args.dry_run else telegram_notify

    print("=" * 60)
    print("AI CAREER AGENT - DAILY RUN", datetime.now(TIMEZONE).strftime("%Y-%m-%d %H:%M"))
    print("=" * 60)

    if not args.dry_run:
        try:
            telegram_notifier.find_chat_id()
        except Exception as error:
            print("Telegram is not ready:", error)
            print("Fix it (see telegram_notifier.py) or run with --dry-run.")
            return 1

    config = settings.get()
    if not args.dry_run and not telegram_inbox.listener_running():   # read your button presses first
        try:
            with closing(job_store.connect()) as connection:
                print("Telegram inbox:", telegram_inbox.process_updates(connection))
        except Exception as error:
            print("Telegram inbox could not be read:", error)

    def tailor(job):
        if not config["documents"]["tailored_cv"]:
            return {"status": "skipped", "message": "tailored CV switched off in settings.json"}
        return cv_tailor.tailor_for_job(job)

    def letter(job, folder):
        return cover_letter.create_for_job(job, folder)

    try:
        only_api = args.only_api or not config["search"]["web_search"]
        jobs, _ = job_search.run_job_search(use_llm=not args.no_llm, only_api=only_api)
        db_file = job_store.DB_FILE
        if args.dry_run:   # work on a temporary copy: the real database is never changed
            import shutil
            import tempfile
            db_file = Path(tempfile.mkdtemp()) / "dry_run.db"
            if job_store.DB_FILE.exists():
                shutil.copy2(job_store.DB_FILE, db_file)
            print("DRY RUN: using a temporary copy of the database; nothing is sent or saved.")
        with closing(job_store.connect(db_file)) as connection:
            stats = process_jobs(jobs, connection=connection, notify=notify, tailor=tailor,
                                 scorer=lambda job: match_scorer.score_job(job),
                                 letter=letter if config["documents"]["cover_letter"] else None)
            stats["applied_total"] = connection.execute(
                "SELECT COUNT(*) FROM user_actions WHERE action = 'applied'").fetchone()[0]
            if not args.dry_run:
                try:
                    stats["reminders"] = telegram_inbox.send_reminders(connection)
                except Exception as error:
                    print("Deadline reminders could not be sent:", error)
    except Exception as error:
        message = f"AI Career Agent stopped with an error: {type(error).__name__}: {error}"
        print(message)
        traceback.print_exc()
        if not args.dry_run:
            try:
                telegram_notifier.send_message(telegram_notifier._e(message))
            except Exception:
                pass
        return 1

    text = summary_text(stats)
    print("\n" + text)
    if not args.dry_run and config["notifications"]["daily_summary"]:
        try:
            telegram_notifier.send_message(telegram_notifier._e(text))
        except Exception as error:
            print("Summary could not be sent:", error)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
