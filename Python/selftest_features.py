"""
selftest_features.py
====================

Offline tests for settings.json, Telegram buttons / reminders and the cover letter.
Run:  python Python\\selftest_features.py
"""

import copy
import json
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from docx import Document

import cover_letter
import job_search as agent
import job_store
import settings
import telegram_inbox
import telegram_notifier
from selftest_cv import MASTER
from selftest_pipeline import FakeHttp, job

TZ = settings.DEFAULTS and agent.TIMEZONE


def with_settings(**changes):
    """Patch settings.get() with DEFAULTS + changes, e.g. with_settings(location={"city": "Ankara"})."""
    data = copy.deepcopy(settings.DEFAULTS)
    for section, values in changes.items():
        data[section].update(values)
    return mock.patch.object(settings, "get", return_value=data)


class SettingsTests(unittest.TestCase):
    def test_defaults_and_user_file(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "settings.json").write_text(json.dumps({"location": {"city": "Izmir"},
                                                          "notifications": {"max_jobs_per_day": 2}}))
        with mock.patch.object(settings, "SETTINGS_FILE", folder / "settings.json"), \
                mock.patch.object(settings, "ENV_FILE", folder / "none.env"), \
                mock.patch.dict("os.environ", {"JOB_LOCATION": "", "JOB_COUNTRY": ""}):
            data = settings.reload()
        settings.get.cache_clear()
        self.assertEqual(data["location"]["city"], "Izmir")
        self.assertEqual(data["notifications"]["max_jobs_per_day"], 2)
        self.assertEqual(data["freshness"]["max_age_hours_linkedin"], 72)       # default kept

    def test_broken_settings_file_falls_back_to_defaults(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "settings.json").write_text("{ not json")
        with mock.patch.object(settings, "SETTINGS_FILE", folder / "settings.json"), \
                mock.patch.object(settings, "ENV_FILE", folder / "none.env"), \
                mock.patch.dict("os.environ", {"JOB_LOCATION": "", "JOB_COUNTRY": ""}):
            data = settings.reload()
        settings.get.cache_clear()
        self.assertEqual(data["location"]["city"], "Istanbul")

    def test_linkedin_window_is_72_hours(self):
        self.assertEqual(settings.DEFAULTS["freshness"]["max_age_hours_linkedin"], 72)

    def test_other_city(self):
        record = {"location": "Çankaya, Ankara, Türkiye", "location_cities": ["Ankara"], "location_countries": ["TR"]}
        with with_settings(location={"city": "Ankara"}):
            self.assertEqual(agent.assess_location(record)["action"], "accept")
            self.assertEqual(agent.assess_location({"location": "Istanbul", "location_cities": ["Istanbul"],
                                                    "location_countries": ["TR"]})["action"], "review")
        with with_settings(location={"city": "Ankara", "accept_other_cities_for_review": False}):
            self.assertEqual(agent.assess_location({"location": "Istanbul", "location_cities": ["Istanbul"],
                                                    "location_countries": ["TR"]})["action"], "reject")

    def test_custom_roles(self):
        with with_settings(roles={"role_keywords": ["digital marketing", "social media"]}):
            self.assertEqual(agent.assess_title("Digital Marketing Intern")["status"], "ok")
            self.assertEqual(agent.assess_title("Data Analyst Intern")["status"], "reject")
        self.assertEqual(agent.assess_title("Digital Marketing Intern")["status"], "reject")   # built-in list

    def test_queries_follow_city(self):
        with with_settings(location={"city": "Bursa"}):
            self.assertTrue(all("Bursa" in q for q in agent.seed_queries()))


class TelegramButtonTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "t.db"
        self.patches = [mock.patch.object(telegram_notifier, "find_chat_id", return_value="555"),
                        mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "1:x"})]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)

    def saved_job(self, connection, **extra):
        record = job("Data Analyst Intern", "Acme", "https://acme.com/jobs/1", **extra)
        job_id, _, _ = job_store.save_job(connection, record)
        job_store.set_status(connection, job_id, "sent", sent=True)
        return job_id

    def test_button_press_is_saved_and_foreign_chats_ignored(self):
        with closing(job_store.connect(self.db)) as connection:
            job_id = self.saved_job(connection)
            http = FakeHttp([
                {"update_id": 10, "callback_query": {"id": "q1", "data": f"applied:{job_id}",
                                                    "message": {"message_id": 7, "chat": {"id": 555}}}},
                {"update_id": 11, "callback_query": {"id": "q2", "data": f"not_interested:{job_id}",
                                                    "message": {"message_id": 7, "chat": {"id": 999}}}},
            ])
            counts = telegram_inbox.process_updates(connection, http=http)
            self.assertEqual(job_store.get_action(connection, job_id), "applied")
            self.assertEqual((counts["actions"], counts["ignored"]), (1, 1))
            self.assertEqual(job_store.get_state(connection, "telegram_offset"), "11")
            self.assertIn("editMessageReplyMarkup", [c[0] for c in http.calls])

    def test_button_gives_instant_feedback_and_collapses(self):
        with closing(job_store.connect(self.db)) as connection:
            job_id = self.saved_job(connection)
            http = FakeHttp([
                {"update_id": 20, "callback_query": {"id": "q1", "data": f"not_interested:{job_id}",
                                                    "message": {"message_id": 7, "chat": {"id": 555}}}},
                {"update_id": 21, "callback_query": {"id": "q2", "data": "noop",
                                                    "message": {"message_id": 7, "chat": {"id": 555}}}},
            ])
            telegram_inbox.process_updates(connection, http=http)
        answers = [c[1]["text"] for c in http.calls if c[0] == "answerCallbackQuery"]
        self.assertEqual(answers, [telegram_inbox.ACTION_TOASTS["not_interested"], "Already saved ✓"])
        markup = [json.loads(c[1]["reply_markup"]) for c in http.calls if c[0] == "editMessageReplyMarkup"]
        self.assertEqual(markup, [{"inline_keyboard": [[{"text": "👎 Not interested", "callback_data": "noop"}]]}])

    def test_bot_loop_answers_and_survives_errors(self):
        folder = Path(tempfile.mkdtemp())
        with closing(job_store.connect(self.db)) as connection:
            job_id = self.saved_job(connection)
        good = FakeHttp([{"update_id": 30, "callback_query": {"id": "q", "data": f"applied:{job_id}",
                                                              "message": {"message_id": 9, "chat": {"id": 555}}}}])
        calls = {"n": 0}

        class Flaky:
            def post(self, url, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise telegram_notifier.requests.ConnectionError("PC just woke up")
                return good.post(url, **kwargs)

        with mock.patch.object(job_store, "DB_FILE", self.db), \
             mock.patch.object(telegram_inbox, "HEARTBEAT_FILE", folder / "beat.txt"):
            self.assertFalse(telegram_inbox.listener_running())
            telegram_inbox.listen(http=Flaky(), wait=1, max_loops=2, sleep=lambda seconds: None)
            self.assertTrue(telegram_inbox.listener_running())          # heartbeat written
            self.assertEqual(telegram_inbox.run(http=good)["actions"], 0)   # one-shot steps aside
            self.assertEqual(telegram_inbox.listen(http=good, max_loops=1), 0)  # second bot exits
        with closing(job_store.connect(self.db)) as connection:
            self.assertEqual(job_store.get_action(connection, job_id), "applied")

    def test_every_message_gets_an_answer(self):
        with closing(job_store.connect(self.db)) as connection:
            self.saved_job(connection)
            http = FakeHttp([{"update_id": 40 + i, "message": {"chat": {"id": 555}, "text": text}}
                             for i, text in enumerate(["status", "/status@MyBot", "hello", "/Durum"])])
            logged = []
            counts = telegram_inbox.process_updates(connection, http=http, log=logged.append)
        texts = [c[1]["text"] for c in http.calls if c[0] == "sendMessage"]
        self.assertEqual(counts["commands"], 4)
        self.assertEqual(["YOUR JOBS" in t for t in texts], [True, True, False, True])
        self.assertEqual(len(logged), 4)

    def test_status_command(self):
        with closing(job_store.connect(self.db)) as connection:
            job_id = self.saved_job(connection)
            job_store.set_action(connection, job_id, "applied")
            http = FakeHttp([{"update_id": 3, "message": {"chat": {"id": 555}, "text": "/status"}}])
            telegram_inbox.process_updates(connection, http=http)
            sent_texts = [c[1]["text"] for c in http.calls if c[0] == "sendMessage"]
        self.assertTrue(any("Applied:" in text and "Data Analyst Intern" in text for text in sent_texts))

    def test_deadline_reminder_once_and_not_after_answer(self):
        now = datetime(2026, 12, 11, 10, 0, tzinfo=agent.TIMEZONE)
        with closing(job_store.connect(self.db)) as connection:
            soon = self.saved_job(connection, application_deadline="2026-12-14T23:59:00+03:00")
            other = job_store.save_job(connection, job("BI Intern", "Beta", "https://beta.com/2",
                                                       application_deadline="2026-12-13T23:59:00+03:00"))[0]
            job_store.set_status(connection, other, "sent", sent=True)
            job_store.set_action(connection, other, "not_interested")
            first = telegram_inbox.send_reminders(connection, http=FakeHttp(), now=now)
            second = telegram_inbox.send_reminders(connection, http=FakeHttp(), now=now)
        self.assertEqual((first, second), (1, 0))

    def test_job_message_has_buttons(self):
        http = FakeHttp()
        telegram_notifier.send_message("hi", http=http, chat_id="555", buttons=telegram_notifier.job_buttons(4))
        markup = json.loads(http.calls[0][1]["reply_markup"])
        self.assertEqual(markup["inline_keyboard"][0][0]["callback_data"], "applied:4")


JOB = {"job_title": "Data Analyst Intern", "company": "Acme",
       "description_text": "Acme is looking for a Data Analyst Intern to build reports with SQL and Python. " * 6}

GOOD = ["I am applying for the Data Analyst Intern position at Acme. I am a final-year Management Information "
        "Systems student with hands-on experience in data analytics and AI from my internships.",
        "At my hospital group internship I consolidated, cleaned, and validated datasets from 40+ hospitals and "
        "developed routine reports and automated data workflows in Excel, which taught me careful data work.",
        "Acme's focus on reporting with SQL and Python matches the skills I want to grow. Thank you for considering "
        "my application; I would be glad to discuss how I can contribute to your team."]


def chat_answers(*answers):
    queue = list(answers)

    def chat(**kwargs):
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps(queue.pop(0))))
    return chat


class CoverLetterTests(unittest.TestCase):
    def test_valid_letter_is_built(self):
        folder = Path(tempfile.mkdtemp())
        with mock.patch.object(cover_letter.cv_tailor, "convert_to_pdf", return_value=(None, "skipped")):
            result = cover_letter.create_for_job(JOB, folder, content=MASTER,
                                                 chat_fn=chat_answers({"paragraphs": GOOD}))
        self.assertEqual(result["status"], "ok", result)
        self.assertTrue(result["docx"].endswith("Test_Person_Acme_Cover_Letter.docx"))
        text = "\n".join(p.text for p in Document(result["docx"]).paragraphs)
        self.assertIn("Dear Acme Hiring Team,", text)
        self.assertIn("40+ hospitals", text)

    def test_invented_skill_is_rewritten_or_dropped(self):
        bad = list(GOOD)
        bad[1] = ("At my internship I built Power BI dashboards and Tableau reports for 40+ hospitals, and I have "
                  "3 years of experience in data analysis across many different teams and projects.")
        fixed = {"paragraph": GOOD[1]}
        result_paragraphs, rejected = cover_letter.write_paragraphs(
            MASTER, JOB, chat_fn=chat_answers({"paragraphs": bad}, fixed))
        self.assertEqual(result_paragraphs[1], GOOD[1])
        self.assertNotIn("Power BI", " ".join(result_paragraphs))

        still_bad = {"paragraph": bad[1]}
        paragraphs, rejected = cover_letter.write_paragraphs(
            MASTER, JOB, chat_fn=chat_answers({"paragraphs": bad}, still_bad))
        self.assertEqual(len(paragraphs), 2)
        self.assertEqual(len(rejected), 1)

    def test_too_few_valid_paragraphs_means_no_letter(self):
        bad = {"paragraphs": ["I am an expert with 5 years of experience in Tableau and Power BI dashboards and much more "
                              "than that in every possible area of analytics and engineering work.", "Short."]}
        result = cover_letter.create_for_job(JOB, Path(tempfile.mkdtemp()), content=MASTER,
                                             chat_fn=chat_answers(bad, {"paragraph": ""}, {"paragraph": ""}))
        self.assertEqual(result["status"], "failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
