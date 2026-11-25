"""
telegram_notifier.py
====================

Sends job messages and CV files through your own Telegram bot.

.env (project folder):
    TELEGRAM_BOT_TOKEN=123456:ABC...      (from @BotFather - keep secret)
    TELEGRAM_CHAT_ID=...                  (optional; found automatically)

The chat ID is found automatically the first time: send /start to your bot,
then run this file once:   python Python\\telegram_notifier.py
The ID is stored in Database/telegram_chat.json (not uploaded to GitHub).
"""

from __future__ import annotations

import html
import json
import os
import time
from pathlib import Path

import requests

import job_sources   # reuses the .env reader

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CHAT_FILE = PROJECT_ROOT / "Database" / "telegram_chat.json"
API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT = (10, 60)
MAX_MESSAGE = 4000          # Telegram limit is 4096 characters


class TelegramError(RuntimeError):
    pass


def _token() -> str:
    job_sources.load_env_file()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token.casefold() in ("buraya_token", "paste_your_token_here"):
        raise TelegramError("TELEGRAM_BOT_TOKEN is not set in .env")
    return token


def _call(method: str, *, http=None, data=None, files=None, retries: int = 2) -> dict:
    http = http or requests
    url = API.format(token=_token(), method=method)
    for attempt in range(retries + 1):
        try:
            response = http.post(url, data=data, files=files, timeout=TIMEOUT)
        except requests.RequestException as error:
            if attempt == retries:
                raise TelegramError(f"Network error: {type(error).__name__}") from None
            time.sleep(3)
            continue
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        if response.status_code == 429 and attempt < retries:     # too many messages: wait as told
            time.sleep(int((payload.get("parameters") or {}).get("retry_after", 5)) + 1)
            continue
        if response.status_code == 401:
            raise TelegramError("Bot token rejected (check TELEGRAM_BOT_TOKEN).")
        if not payload.get("ok"):
            raise TelegramError(f"Telegram error {response.status_code}: {payload.get('description', '')}")
        return payload.get("result") or {}
    raise TelegramError("Telegram request failed.")


def find_chat_id(*, http=None) -> str:
    """Saved ID -> TELEGRAM_CHAT_ID in .env -> latest private chat that wrote to the bot."""
    job_sources.load_env_file()
    configured = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if configured:
        return configured
    try:
        saved = json.loads(CHAT_FILE.read_text(encoding="utf-8")).get("chat_id")
        if saved:
            return str(saved)
    except (OSError, ValueError):
        pass

    updates = _call("getUpdates", http=http, data={"timeout": 0, "allowed_updates": json.dumps(["message", "callback_query"])})
    chat_id = None
    for update in updates if isinstance(updates, list) else []:
        chat = ((update.get("message") or {}).get("chat") or {})
        if chat.get("type") == "private":
            chat_id = chat.get("id")
    if chat_id is None:
        raise TelegramError("No chat found. Open your bot in Telegram, send /start, then run again.")
    CHAT_FILE.parent.mkdir(parents=True, exist_ok=True)
    CHAT_FILE.write_text(json.dumps({"chat_id": chat_id}), encoding="utf-8")
    return str(chat_id)


def send_message(text: str, *, http=None, chat_id=None, buttons=None):
    """Send (long texts are split). Returns the message_id of the last part. buttons: inline keyboard rows."""
    chat_id = chat_id or find_chat_id(http=http)
    parts = [text[start:start + MAX_MESSAGE] for start in range(0, len(text), MAX_MESSAGE)] or [""]
    result = {}
    for index, part in enumerate(parts):
        data = {"chat_id": chat_id, "text": part, "parse_mode": "HTML", "disable_web_page_preview": "true"}
        if buttons and index == len(parts) - 1:
            data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
        result = _call("sendMessage", http=http, data=data)
    return result.get("message_id") if isinstance(result, dict) else None


def job_buttons(job_id: int) -> list:
    return [[{"text": "✅ Applied", "callback_data": f"applied:{job_id}"},
             {"text": "👎 Not interested", "callback_data": f"not_interested:{job_id}"}]]


def answer_button(callback_query_id: str, text: str, *, http=None) -> None:
    try:   # Telegram only accepts answers for a short time; late answers are harmless to skip
        _call("answerCallbackQuery", http=http, data={"callback_query_id": callback_query_id, "text": text},
              retries=0)
    except TelegramError:
        pass


def replace_buttons(chat_id, message_id: int, label: str, *, http=None) -> None:
    try:
        _call("editMessageReplyMarkup", http=http, retries=0, data={
            "chat_id": chat_id, "message_id": message_id,
            "reply_markup": json.dumps({"inline_keyboard": [[{"text": label, "callback_data": "noop"}]]})})
    except TelegramError:
        pass


def send_file(path, *, caption: str = "", http=None, chat_id=None) -> None:
    path = Path(path)
    if not path.exists():
        raise TelegramError(f"File not found: {path.name}")
    chat_id = chat_id or find_chat_id(http=http)
    with open(path, "rb") as handle:
        _call("sendDocument", http=http, data={"chat_id": chat_id, "caption": caption[:1000]},
              files={"document": (path.name, handle)})


# ============================================================
# Message text
# ============================================================

def _e(value) -> str:
    return html.escape(str(value if value not in (None, "") else "Not verified"))


def job_message(job: dict) -> str:
    links = job.get("application_links") or []
    apply_url = links[0]["url"] if links else (job.get("url") or job.get("search_url") or "")
    posted = (job.get("publication_date") or "")[:10] or "Not verified"
    flags = job.get("review_flags") or []
    header = "NEW JOB OPPORTUNITY" if job.get("screening_decision") == "CANDIDATE" else "JOB TO REVIEW"
    lines = [
        f"<b>{header}</b>",
        "",
        f"<b>Position:</b> {_e(job.get('job_title') or job.get('search_title'))}",
        f"<b>Company:</b> {_e(job.get('company'))}",
        f"<b>Location:</b> {_e(job.get('location'))}" + (f" ({_e(job.get('work_model'))})" if job.get("work_model") else ""),
        f"<b>Posted:</b> {_e(posted)}",
    ]
    if job.get("application_deadline"):
        lines.append(f"<b>Deadline:</b> {_e(job['application_deadline'][:10])}")
    match = job.get("match") or {}
    if match.get("score") is not None:
        lines += ["", f"<b>MATCH SCORE: {match['score']}/100</b> ({_e(match.get('label'))})"]
        if match.get("strengths"):
            lines += ["", "<b>WHY IT MATCHES:</b>"] + [
                f"- {_e(r['requirement'])}" + (f" <i>({_e(r['evidence'][:60])})</i>" if r.get("evidence") else "")
                for r in match["strengths"]]
        if match.get("to_check"):
            lines += ["", "<b>CHECK YOURSELF:</b>"] + [
                f"- {_e(r['requirement'])}" + (f" <i>({_e(r['note'])})</i>" if r.get("note") else "")
                for r in match["to_check"]]
        if match.get("gaps"):
            lines += ["", "<b>GAPS:</b>"] + [
                f"- {_e(r['requirement'])}" + (" <i>(required)</i>" if r.get("type") == "required" else "")
                for r in match["gaps"]]
    elif match:
        lines += ["", f"<i>Match score not available: {_e(match.get('error'))}</i>"]
    lines += ["", f"<b>Experience:</b> {_e(job.get('experience_reason'))}"]
    if flags:
        lines += ["", "<b>CHECK BEFORE APPLYING:</b>"] + [f"- {_e(flag)}" for flag in flags[:5]]
    if apply_url:
        lines += ["", f'<b>APPLY:</b> <a href="{html.escape(apply_url, quote=True)}">{_e(apply_url[:80])}</a>']
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        found = find_chat_id()
        send_message("✅ AI Career Agent is connected. Job notifications will arrive here.")
        print("Telegram works. Chat ID saved:", found)
    except TelegramError as error:
        print("Telegram problem:", error)
        raise SystemExit(1)
