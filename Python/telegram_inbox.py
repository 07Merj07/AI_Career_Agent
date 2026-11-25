"""
telegram_inbox.py
=================

Reads what you did in Telegram and sends deadline reminders.

* "✅ Applied" / "👎 Not interested" buttons under every job message
* commands you can type to the bot:
      /status   jobs you applied to, open jobs and their deadlines
      /help     short help
* reminder N days before the deadline of a job you have not answered yet
  (N = notifications.deadline_reminder_days in settings.json)

Two ways to run it (no Ollama and no job API needed, so it is cheap):

    python Python\\telegram_inbox.py --listen   the bot: answers buttons and /status instantly
                                                (started in the background by schedule_tasks.ps1)
    python Python\\telegram_inbox.py            one check, then exit
"""

from __future__ import annotations

import html
import json
import sys
import time
from contextlib import closing
from datetime import datetime

import job_store
import settings
import telegram_notifier
from job_page_inspector import TIMEZONE

ACTION_LABELS = {"applied": "✅ Applied", "not_interested": "👎 Not interested"}
ACTION_TOASTS = {"applied": "✅ Saved: you applied to this job", "not_interested": "👎 Saved: not interested"}
HEARTBEAT_FILE = job_store.DB_FILE.parent / "bot_heartbeat.txt"
HEARTBEAT_MAX_AGE = 150      # seconds; the bot writes it before every wait (at most ~60 s)
LOG_FILE = job_store.DB_FILE.parent.parent / "Logs" / "bot.log"


def _e(value) -> str:
    return html.escape(str(value or ""))


def status_text(connection) -> str:
    jobs = job_store.sent_jobs(connection)
    if not jobs:
        return "No jobs have been sent yet."
    now = datetime.now(TIMEZONE)

    def line(job):
        parts = [f"• {_e(job['job_title'])} - {_e(job['company'])}"]
        if job["deadline"]:
            days = (job["deadline"].date() - now.date()).days
            parts.append(f"(deadline {job['deadline']:%d.%m}" + (f", {days} days left)" if days >= 0 else ", passed)"))
        return " ".join(parts)

    applied = [j for j in jobs if j["action"] == "applied"]
    open_jobs = [j for j in jobs if j["action"] is None and not (j["deadline"] and j["deadline"].date() < now.date())]
    skipped = [j for j in jobs if j["action"] == "not_interested"]
    lines = [f"<b>YOUR JOBS</b>  (sent: {len(jobs)}, applied: {len(applied)}, not interested: {len(skipped)})"]
    if applied:
        lines += ["", "<b>Applied:</b>"] + [line(j) for j in applied[:15]]
    if open_jobs:
        open_jobs.sort(key=lambda j: j["deadline"] or datetime.max.replace(tzinfo=TIMEZONE))
        lines += ["", "<b>Not answered yet:</b>"] + [line(j) for j in open_jobs[:15]]
    return "\n".join(lines)


HELP_TEXT = ("<b>AI Career Agent</b> is running ✅\n"
             "Every evening you get new matching jobs with a tailored CV and cover letter.\n\n"
             "Buttons under a job: ✅ Applied / 👎 Not interested\n"
             "/status - your applications and deadlines\n/help - this message")


def process_updates(connection, *, http=None, wait: int = 0, log=None) -> dict:
    """Read new button presses and commands. Only messages from YOUR chat are accepted.
    wait > 0 keeps the request open up to `wait` seconds until something arrives (used by the bot)."""
    counts = {"actions": 0, "commands": 0, "ignored": 0}
    my_chat = str(telegram_notifier.find_chat_id(http=http))
    offset = int(job_store.get_state(connection, "telegram_offset", 0) or 0)
    updates = telegram_notifier._call("getUpdates", http=http, retries=0 if wait else 2, data={
        "offset": offset + 1 if offset else 0, "timeout": wait,
        "allowed_updates": json.dumps(["message", "callback_query"])})
    for update in updates if isinstance(updates, list) else []:
        offset = max(offset, int(update.get("update_id", 0)))
        job_store.set_state(connection, "telegram_offset", offset)   # never handle an update twice
        callback = update.get("callback_query")
        if log:
            log("button " + str((callback or {}).get("data")) if callback
                else "message " + repr(str((update.get("message") or {}).get("text") or "")[:40]))
        if callback:
            chat = str(((callback.get("message") or {}).get("chat") or {}).get("id"))
            action, _, job_id = str(callback.get("data") or "").partition(":")
            if chat != my_chat:
                counts["ignored"] += 1
                continue
            if action == "noop":              # the button that already shows your answer
                telegram_notifier.answer_button(callback.get("id"), "Already saved ✓", http=http)
                continue
            if action not in ACTION_LABELS or not job_id.isdigit() \
                    or not job_store.set_action(connection, int(job_id), action):
                counts["ignored"] += 1
                telegram_notifier.answer_button(callback.get("id"), "This job is no longer in the database.",
                                                http=http)
                continue
            counts["actions"] += 1
            telegram_notifier.answer_button(callback.get("id"), ACTION_TOASTS[action], http=http)
            message_id = (callback.get("message") or {}).get("message_id")
            if message_id:
                telegram_notifier.replace_buttons(chat, message_id, ACTION_LABELS[action], http=http)
            continue
        message = update.get("message") or {}
        chat = str((message.get("chat") or {}).get("id"))
        text = str(message.get("text") or "").strip().casefold()
        if chat != my_chat:
            counts["ignored"] += 1
            continue
        command = text.lstrip("/").split("@")[0].split()[0] if text.strip("/ ") else ""
        if command in ("status", "durum"):
            telegram_notifier.send_message(status_text(connection), http=http, chat_id=my_chat)
        else:     # /help, /start or anything else: answer, so you always see that the bot is alive
            telegram_notifier.send_message(HELP_TEXT, http=http, chat_id=my_chat)
        counts["commands"] += 1
    return counts


def listener_running() -> bool:
    """True while the background bot (--listen) is alive; then nobody else may read Telegram updates."""
    try:
        return time.time() - HEARTBEAT_FILE.stat().st_mtime < HEARTBEAT_MAX_AGE
    except OSError:
        return False


def _heartbeat() -> None:
    HEARTBEAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT_FILE.write_text(datetime.now(TIMEZONE).isoformat(timespec="seconds"), encoding="utf-8")


def listen(*, http=None, wait: int = 50, reminder_every: int = 3600, max_loops: int | None = None,
           sleep=time.sleep) -> int:
    """The bot: waits for button presses / commands and answers at once. Runs until the PC shuts down."""
    if listener_running():
        print("The bot is already running.")
        return 0
    def log(text):
        print(f"{datetime.now(TIMEZONE):%Y-%m-%d %H:%M:%S} {text}", flush=True)

    log("bot started")
    try:      # shows /status and /help in the "Menu" button of the chat
        telegram_notifier._call("setMyCommands", http=http, retries=0, data={"commands": json.dumps([
            {"command": "status", "description": "Your applications and deadlines"},
            {"command": "help", "description": "What this bot does"}])})
    except Exception as error:
        log(f"menu not set: {error}")
    last_reminders = 0.0
    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        _heartbeat()
        try:
            with closing(job_store.connect(job_store.DB_FILE)) as connection:
                counts = process_updates(connection, http=http, wait=wait, log=log)
                if time.monotonic() - last_reminders >= reminder_every or not last_reminders:
                    counts["reminders"] = send_reminders(connection, http=http)
                    last_reminders = time.monotonic()
            if counts.get("reminders"):
                log(f"{counts['reminders']} deadline reminder(s) sent")
        except Exception as error:    # no internet, PC woke up, Telegram busy... wait and go on
            log(f"{type(error).__name__}: {error}")
            sleep(15)
    return 0


def send_reminders(connection, *, http=None, now=None) -> int:
    days = int(settings.get()["notifications"]["deadline_reminder_days"])
    if days <= 0:
        return 0
    sent = 0
    for job in job_store.due_reminders(connection, days, now=now):
        when = "today" if job["days_left"] == 0 else f"in {job['days_left']} day(s)"
        text = (f"⏰ <b>DEADLINE {when.upper()}</b>\n\n{_e(job['job_title'])} - {_e(job['company'])}\n"
                f"Deadline: {job['deadline']:%d.%m.%Y}")
        if job.get("url"):
            text += f'\n\n<a href="{html.escape(job["url"], quote=True)}">Open the job</a>'
        message_id = telegram_notifier.send_message(text, http=http, buttons=telegram_notifier.job_buttons(job["id"]))
        if message_id:
            job_store.save_telegram_message(connection, job["id"], message_id)
        job_store.mark_reminded(connection, job["id"])
        sent += 1
    return sent


def run(*, http=None) -> dict:
    """One check. If the background bot is running, it already handles the updates."""
    with closing(job_store.connect(job_store.DB_FILE)) as connection:
        if listener_running():
            counts = {"actions": 0, "commands": 0, "ignored": 0, "note": "the bot is running and handles them"}
        else:
            counts = process_updates(connection, http=http)
        counts["reminders"] = send_reminders(connection, http=http)
    return counts


if __name__ == "__main__":
    if sys.stdout is None or sys.stderr is None:      # started with pythonw (no window): write to a log
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = sys.stderr = open(LOG_FILE, "a", encoding="utf-8", buffering=1)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    if "--listen" in sys.argv:
        raise SystemExit(listen())
    try:
        result = run()
        print("Telegram inbox:", result)
    except telegram_notifier.TelegramError as error:
        print("Telegram problem:", error)
        raise SystemExit(1)
