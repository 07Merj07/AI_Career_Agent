"""
setup_check.py
==============

Checks that everything is installed and configured, and tells you how to fix
what is missing. Safe to run any time; it changes nothing.

    python Python\\setup_check.py              basic checks
    python Python\\setup_check.py --telegram   also send a test message to your bot
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"
results = []


def report(status: str, title: str, hint: str = "") -> None:
    results.append(status)
    print(f"{status} {title}")
    if hint and status != OK:
        print(f"       -> {hint}")


def check_python() -> None:
    version = sys.version_info
    if version >= (3, 11):
        report(OK, f"Python {version.major}.{version.minor}")
    else:
        report(FAIL, f"Python {version.major}.{version.minor} is too old", "Install Python 3.11 or newer.")


def check_packages() -> None:
    needed = {"requests": "requests", "bs4": "beautifulsoup4", "docx": "python-docx", "ollama": "ollama",
              "ddgs": "ddgs"}
    missing = []
    for module, package in needed.items():
        try:
            importlib.import_module(module)
        except ImportError:
            missing.append(package)
    if missing:
        report(FAIL, "Missing Python packages: " + ", ".join(missing), "Run: pip install -r requirements.txt")
    else:
        report(OK, "Python packages installed")
    try:
        importlib.import_module("docx2pdf")
        report(OK, "PDF export (docx2pdf + Microsoft Word)")
    except ImportError:
        report(WARN, "docx2pdf not installed: CVs will be Word only (or PDF via LibreOffice if installed)",
               "pip install docx2pdf  (needs Microsoft Word)")


def check_files() -> None:
    import settings
    for name, example in ((".env", ".env.example"), ("settings.json", "settings.example.json"),
                          ("CV/cv_content.json", "CV/cv_content.example.json")):
        if (ROOT / name).exists():
            report(OK, f"{name} found")
        else:
            report(FAIL, f"{name} is missing", f"Copy {example} to {name} and fill it in (setup.bat does the copy).")
    try:
        json.loads((ROOT / "settings.json").read_text(encoding="utf-8-sig"))
        data = settings.reload()
        report(OK, f"settings.json is valid (city: {data['location']['city']}, model: {data['ai']['model']})")
    except FileNotFoundError:
        pass
    except ValueError as error:
        report(FAIL, f"settings.json is not valid JSON: {error}", "Check commas and quotes, or copy the example again.")

    settings.load_env_file()
    for key, why in (("RAPIDAPI_KEY", "job API (JSearch) - without it only web search is used"),
                     ("TELEGRAM_BOT_TOKEN", "Telegram notifications")):
        value = os.environ.get(key, "").strip()
        if value and "paste" not in value.casefold() and "your" not in value.casefold():
            report(OK, f"{key} is set")
        else:
            report(FAIL if key == "TELEGRAM_BOT_TOKEN" else WARN, f"{key} is not set ({why})",
                   "Add it to the .env file (see README).")


def check_cv() -> None:
    try:
        import cv_builder
        content = cv_builder.load_content()
    except FileNotFoundError:
        return
    except ValueError as error:
        report(FAIL, f"CV/cv_content.json is not valid JSON: {error}")
        return
    problems = cv_builder.validate_content(content)
    if problems:
        report(FAIL, "CV content has problems: " + " ".join(problems))
    elif "example" in json.dumps(content).casefold() or content.get("name") == "Alex Demir":
        report(WARN, "CV/cv_content.json still contains the example person", "Replace it with your own CV content.")
    else:
        report(OK, f"CV content OK ({content['name']})")


def check_ollama() -> None:
    import settings
    model = settings.model()
    try:
        import ollama
        listed = ollama.list()
        names = [getattr(m, "model", None) or (m.get("model") if isinstance(m, dict) else None) or
                 (m.get("name") if isinstance(m, dict) else None)
                 for m in (getattr(listed, "models", None) or listed.get("models", []))]
    except Exception as error:
        report(FAIL, f"Ollama is not reachable ({type(error).__name__})",
               "Install Ollama from ollama.com and make sure it is running.")
        return
    if any(name and name.split(":latest")[0] == model.split(":latest")[0] for name in names):
        report(OK, f"Ollama is running and model '{model}' is installed")
    else:
        report(FAIL, f"Model '{model}' is not installed in Ollama", f"Run: ollama pull {model}")


def check_telegram(send: bool) -> None:
    import telegram_notifier
    try:
        chat_id = telegram_notifier.find_chat_id()
        report(OK, "Telegram bot works and your chat was found")
        if send:
            telegram_notifier.send_message("✅ Setup check: AI Career Agent can reach you.", chat_id=chat_id)
            report(OK, "Test message sent - check Telegram")
    except Exception as error:
        report(FAIL, f"Telegram: {error}", "Open your bot in Telegram, press Start (or send /start), then run again.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the AI Career Agent setup")
    parser.add_argument("--telegram", action="store_true", help="also send a test message")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    print("AI Career Agent - setup check\n")
    check_python()
    check_packages()
    check_files()
    check_cv()
    check_ollama()
    if os.environ.get("TELEGRAM_BOT_TOKEN"):
        check_telegram(args.telegram)
    failed = results.count(FAIL)
    print(f"\n{'Everything looks good!' if not failed else f'{failed} problem(s) to fix (see -> hints above).'}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
