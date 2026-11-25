"""
selftest_pipeline.py
====================

Offline tests for job_store.py, telegram_notifier.py and daily_pipeline.py.
No real Telegram, Ollama or internet is used.   Run:  python Python\\selftest_pipeline.py
"""

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

import daily_pipeline
import job_store
import telegram_notifier


def job(title, company, url, decision="CANDIDATE", **extra):
    record = {"job_title": title, "company": company, "url": url, "location": "Istanbul, Türkiye",
              "location_cities": ["Istanbul"], "screening_decision": decision,
              "screening_reason": "ok", "publication_date": "2026-11-23T10:00:00+03:00",
              "description_text": "Data analysis internship.", "application_links": [{"text": "Apply", "url": url}],
              "experience_reason": "No experience requirement found.", "review_flags": []}
    record.update(extra)
    return record


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "test.db"

    def test_same_job_from_another_url_is_recognised(self):
        with closing(job_store.connect(self.db)) as connection:
            first_id, new1, _ = job_store.save_job(connection, job(
                "Data Analyst Intern", "Acme", "https://tr.linkedin.com/jobs/view/data-analyst-intern-at-acme-111"))
            # same LinkedIn job, different sub-domain + tracking
            second_id, new2, _ = job_store.save_job(connection, job(
                "Data Analyst Intern", "Acme", "https://www.linkedin.com/jobs/view/data-analyst-intern-at-acme-111?trk=x"))
            # same company/title/city, another site
            third_id, new3, _ = job_store.save_job(connection, job(
                "Data Analyst Intern", "Acme", "https://careers.acme.com/jobs/9"))
        self.assertEqual((new1, new2, new3), (True, False, False))
        self.assertEqual(first_id, second_id)
        self.assertEqual(first_id, third_id)

    def test_different_positions_are_separate(self):
        with closing(job_store.connect(self.db)) as connection:
            a, _, _ = job_store.save_job(connection, job("Data Analyst Intern", "Acme", "https://a.com/1"))
            b, _, _ = job_store.save_job(connection, job("BI Intern", "Acme", "https://a.com/2"))
        self.assertNotEqual(a, b)

    def test_rejected_jobs_are_ignored_status(self):
        with closing(job_store.connect(self.db)) as connection:
            job_store.save_job(connection, job("Senior Analyst", "Acme", "https://a.com/3", decision="REJECTED"))
        self.assertEqual(job_store.summary(self.db), {"ignored": 1})

    def test_existing_schema_is_kept(self):
        import sqlite3
        connection = sqlite3.connect(self.db)
        connection.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, dedup_key TEXT NOT NULL UNIQUE,"
                           " job_title TEXT NOT NULL, company TEXT NOT NULL, location TEXT, work_type TEXT,"
                           " raw_description TEXT, language TEXT, status TEXT NOT NULL DEFAULT 'new',"
                           " analysis_json TEXT, match_score REAL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        connection.commit()
        connection.close()
        with closing(job_store.connect(self.db)) as connection:   # adds posted_at / last_sent_at
            job_store.save_job(connection, job("Data Intern", "Acme", "https://a.com/4"))
            columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        self.assertTrue({"posted_at", "last_sent_at"} <= columns)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "test.db"
        self.sent, self.tailored = [], []

    def run_once(self, jobs, *, notify_fails=False, max_jobs=5, cv_ok=True):
        def notify(job_record, cv, cv_only=False):
            if notify_fails:
                raise RuntimeError("network down")
            self.sent.append(("CV " if cv_only else "") + job_record["job_title"])

        def tailor(job_record):
            self.tailored.append(job_record["job_title"])
            if not cv_ok:
                return {"status": "failed", "message": "description too short"}
            return {"status": "ok", "docx": "x.docx", "pdf": "x.pdf"}

        with closing(job_store.connect(self.db)) as connection:
            return daily_pipeline.process_jobs(jobs, connection=connection, notify=notify, tailor=tailor,
                                               max_jobs=max_jobs)

    def test_nothing_is_sent_twice(self):
        jobs = [job("Data Analyst Intern", "Acme", "https://a.com/1"),
                job("BI Intern", "Beta", "https://b.com/1", decision="MANUAL_REVIEW"),
                job("Senior Analyst", "Gamma", "https://c.com/1", decision="REJECTED"),
                job("Data Intern", "Delta", "https://linkedin.com/jobs/view/1", decision="LINKEDIN_MANUAL_CHECK")]
        first = self.run_once(jobs)
        second = self.run_once(jobs)
        self.assertEqual(first["sent"], 2)
        self.assertEqual(second["sent"], 0)
        self.assertEqual(second["already_sent"], 2)
        self.assertEqual(self.sent, ["Data Analyst Intern", "BI Intern"])   # candidates first

    def test_failed_telegram_is_retried_next_run(self):
        jobs = [job("Data Analyst Intern", "Acme", "https://a.com/1")]
        failed = self.run_once(jobs, notify_fails=True)
        retried = self.run_once(jobs)
        self.assertEqual((failed["sent"], failed["notify_failed"]), (0, 1))
        self.assertEqual(retried["sent"], 1)

    def test_daily_limit_keeps_rest_for_tomorrow(self):
        jobs = [job(f"Data Intern {i}", f"Company {i}", f"https://x.com/{i}") for i in range(4)]
        today = self.run_once(jobs, max_jobs=2)
        tomorrow = self.run_once(jobs, max_jobs=2)
        self.assertEqual((today["sent"], today["skipped_limit"]), (2, 2))
        self.assertEqual(tomorrow["sent"], 2)

    def test_cv_follows_later_without_repeating_the_message(self):
        jobs = [job("Data Analyst Intern", "Acme", "https://a.com/1")]
        first = self.run_once(jobs, cv_ok=False)       # message sent, no CV
        second = self.run_once(jobs, cv_ok=False)      # still no CV: nothing sent again
        third = self.run_once(jobs, cv_ok=True)        # CV now possible: only the CV is sent
        fourth = self.run_once(jobs, cv_ok=True)       # done: nothing more
        self.assertEqual(self.sent, ["Data Analyst Intern", "CV Data Analyst Intern"])
        self.assertEqual((first["sent"], third["cv_followups"], fourth["already_sent"]), (1, 1, 1))
        self.assertEqual(second["sent"] + second["cv_followups"], 0)

    def test_dry_run_never_changes_the_real_database(self):
        real_db = Path(tempfile.mkdtemp()) / "real.db"
        with closing(job_store.connect(real_db)):
            pass
        before = real_db.read_bytes()
        jobs = [job("Data Analyst Intern", "Acme", "https://a.com/1")]
        with mock.patch.object(job_store, "DB_FILE", real_db), \
                mock.patch.object(daily_pipeline.job_search, "run_job_search", return_value=(jobs, None)), \
                mock.patch.object(daily_pipeline.cv_tailor, "tailor_for_job", return_value={"status": "failed", "message": "x"}), \
                mock.patch.object(daily_pipeline.match_scorer, "score_job", return_value={"score": 50}):
            self.assertEqual(daily_pipeline.main(["--dry-run", "--only-api"]), 0)
        self.assertEqual(real_db.read_bytes(), before)

    def test_summary_text(self):
        text = daily_pipeline.summary_text({"found": 10, "suitable": 2, "already_sent": 2, "sent": 0,
                                            "cv_failed": 0, "notify_failed": 0, "skipped_limit": 0})
        self.assertIn("no new suitable jobs", text)


class RealCaseTests(unittest.TestCase):
    """26.11.2026: the same UNDP vacancy arrived from 3 sites and had already been sent on 24.11."""
    TITLE = "Quantitative Research Intern - Crisis Response and Resilience Portfolio - Office Based"

    def records(self):
        import job_search as agent
        oracle = job(self.TITLE, None, "https://estm.fa.em2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX_1/job/38132",
                     job_identifier="38132", application_deadline="2026-12-14T23:59:00+03:00",
                     publication_date="2026-11-20T17:18:40+03:00", location="Istanbul, Türkiye")
        unatlas = job(self.TITLE, "UNDP", "https://unatlas.org/job/undp/38132",
                      application_deadline="2026-12-14T23:59:00+03:00", publication_date="2026-11-20T10:00:00+03:00",
                      location="Istanbul")
        openopp = job("Quantitative Research Intern in Crisis Response and Resilience at UNDP",
                      "United Nations Development Programme",
                      "https://openopportunities.org/opportunities/quantitative-research-intern-in-crisis",
                      application_deadline="2026-12-15T23:59:00+03:00", publication_date="2026-11-20T09:00:00+03:00",
                      location="Istanbul")
        for record in (oracle, unatlas, openopp):
            record["source_type"] = agent.source_type(record["url"])
        return agent, oracle, unatlas, openopp

    def test_three_copies_become_one_job_with_company(self):
        agent, oracle, unatlas, openopp = self.records()
        merged = agent.deduplicate_jobs([oracle, unatlas, openopp])
        self.assertEqual(len(merged), 1)
        self.assertTrue(merged[0]["url"].startswith("https://estm.fa.em2.oraclecloud.com"))   # official source
        self.assertEqual(merged[0]["company"], "UNDP")                                       # filled from a copy

    def test_job_sent_earlier_via_linkedin_is_recognised(self):
        agent, oracle, _, _ = self.records()
        db = Path(tempfile.mkdtemp()) / "t.db"
        linkedin = job(self.TITLE, "UNDP Careers",
                       "https://tr.linkedin.com/jobs/view/quantitative-research-intern-at-undp-careers-4492957125",
                       publication_date="2026-11-23T19:00:00+03:00", location="Greater Istanbul")
        with closing(job_store.connect(db)) as connection:
            first_id, _, _ = job_store.save_job(connection, linkedin, same_job_fn=agent.same_job)
            second_id, is_new, _ = job_store.save_job(connection, oracle, same_job_fn=agent.same_job)
        self.assertEqual(first_id, second_id)
        self.assertFalse(is_new)

    def test_different_companies_same_title_stay_separate(self):
        agent, *_ = self.records()
        a = job("Data Analyst Intern", "Acme", "https://acme.com/jobs/1", publication_date="2026-11-24T10:00:00+03:00")
        b = job("Data Analyst Intern", "Beta", "https://beta.com/jobs/7", publication_date="2026-11-24T10:00:00+03:00")
        self.assertFalse(agent.same_job(a, b))


class FakeHttp:
    def __init__(self, updates=None):
        self.calls = []
        self.updates = updates or []

    def post(self, url, data=None, files=None, timeout=None):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, data, bool(files)))
        result = self.updates if method == "getUpdates" else {"message_id": 1}
        return mock.Mock(status_code=200, json=lambda: {"ok": True, "result": result})


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        patches = [mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": ""}),
                   mock.patch.object(telegram_notifier, "CHAT_FILE", self.tmp / "chat.json"),
                   mock.patch.object(telegram_notifier.job_sources, "ENV_FILE", self.tmp / "no.env")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def test_chat_id_found_and_saved(self):
        http = FakeHttp([{"message": {"chat": {"id": 555, "type": "private"}, "text": "/start"}}])
        self.assertEqual(telegram_notifier.find_chat_id(http=http), "555")
        self.assertEqual(json.loads((self.tmp / "chat.json").read_text())["chat_id"], 555)

    def test_no_start_message_gives_clear_error(self):
        with self.assertRaises(telegram_notifier.TelegramError) as caught:
            telegram_notifier.find_chat_id(http=FakeHttp([]))
        self.assertIn("/start", str(caught.exception))

    def test_message_and_file(self):
        http = FakeHttp()
        text = telegram_notifier.job_message(job("Data <Analyst> Intern", "P&G", "https://a.com/1"))
        self.assertIn("Data &lt;Analyst&gt; Intern", text)         # HTML is escaped
        telegram_notifier.send_message(text, http=http, chat_id="1")
        cv = self.tmp / "Tailored_CV.pdf"
        cv.write_bytes(b"%PDF-1.4")
        telegram_notifier.send_file(cv, http=http, chat_id="1")
        self.assertEqual([c[0] for c in http.calls], ["sendMessage", "sendDocument"])
        self.assertTrue(http.calls[1][2])

    def test_missing_token(self):
        with mock.patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": ""}):
            with self.assertRaises(telegram_notifier.TelegramError):
                telegram_notifier.send_message("hi", chat_id="1", http=FakeHttp())


class FullTestTests(unittest.TestCase):
    def setUp(self):
        self.db = Path(tempfile.mkdtemp()) / "test.db"
        long_text = "Data analysis internship with SQL and Python. " * 10
        with closing(job_store.connect(self.db)) as connection:
            self.old_id, _, _ = job_store.save_job(connection, job("Old Intern", "Acme", "https://a.com/old",
                                                                   description_text=long_text))
            self.new_id, _, _ = job_store.save_job(connection, job("New Intern", "Beta", "https://b.com/new",
                                                                   description_text="short"))
            job_store.set_status(connection, self.old_id, "sent", sent=True)
            job_store.set_status(connection, self.new_id, "sent", sent=True)

    def test_load_job_rebuilds_the_record(self):
        with closing(job_store.connect(self.db)) as connection:
            loaded = job_store.load_job(connection, self.old_id)
        self.assertEqual((loaded["job_title"], loaded["company"], loaded["db_job_id"]),
                         ("Old Intern", "Acme", self.old_id))
        self.assertGreater(len(loaded["description_text"]), 200)
        self.assertEqual(loaded["url"], "https://a.com/old")

    def test_full_test_runs_every_step_without_changing_status(self):
        sent = {}
        with mock.patch.object(job_store, "DB_FILE", self.db), \
             mock.patch.object(daily_pipeline.match_scorer, "score_job", return_value={"score": 70, "label": "good"}), \
             mock.patch.object(daily_pipeline.cv_tailor, "tailor_for_job",
                               return_value={"status": "ok", "docx": "a.docx", "pdf": "a.pdf", "folder": "f"}), \
             mock.patch.object(daily_pipeline.cover_letter, "create_for_job",
                               return_value={"status": "ok", "docx": "l.docx", "pdf": "l.pdf"}), \
             mock.patch.object(daily_pipeline.telegram_notifier, "send_message", return_value=1), \
             mock.patch.object(daily_pipeline, "telegram_notify",
                               side_effect=lambda j, cv: sent.update(job=j) or 42):
            code = daily_pipeline.full_test()
        self.assertEqual(code, 0)
        self.assertEqual(sent["job"]["db_job_id"], self.old_id)     # the one with a real description
        with closing(job_store.connect(self.db)) as connection:
            statuses = {row["id"]: row["status"] for row in connection.execute("SELECT id, status FROM jobs")}
        self.assertEqual(statuses, {self.old_id: "sent", self.new_id: "sent"})

    def test_full_test_reports_failed_step(self):
        with mock.patch.object(job_store, "DB_FILE", self.db), \
             mock.patch.object(daily_pipeline.match_scorer, "score_job", return_value={"score": None, "error": "x"}), \
             mock.patch.object(daily_pipeline.cv_tailor, "tailor_for_job", return_value={"status": "error", "message": "Ollama down"}), \
             mock.patch.object(daily_pipeline.cover_letter, "create_for_job", return_value={"status": "error", "message": "x"}), \
             mock.patch.object(daily_pipeline.telegram_notifier, "send_message", return_value=1), \
             mock.patch.object(daily_pipeline, "telegram_notify", return_value=5):
            self.assertEqual(daily_pipeline.full_test(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
