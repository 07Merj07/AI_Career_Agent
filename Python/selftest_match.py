"""
selftest_match.py
=================

Offline tests for match_scorer.py (Qwen is simulated).   Run:  python Python\\selftest_match.py
"""

import json
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import daily_pipeline
import job_store
import match_scorer
import telegram_notifier
from selftest_cv import MASTER

JOB = {"job_title": "Data Analyst Intern", "company": "Acme",
       "description_text": "We need SQL, Python, Power BI, fluent English and a final-year student. " * 6}


def fake_chat(requirements):
    def chat(**kwargs):
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps({"requirements": requirements})))
    return chat


class ScoreTests(unittest.TestCase):
    def score(self, requirements):
        return match_scorer.score_job(JOB, content=MASTER, chat_fn=fake_chat(requirements))

    def test_claims_are_checked_against_the_cv(self):
        result = self.score([
            {"requirement": "SQL", "type": "required", "status": "met", "evidence": "Python, SQL, R"},
            {"requirement": "Power BI dashboards", "type": "required", "status": "met", "evidence": "Data Analysis"},
            {"requirement": "Big Data experience", "type": "preferred", "status": "met",
             "evidence": "built big data pipelines on Spark"},
            {"requirement": "Fluent English", "type": "required", "status": "met", "evidence": "English (C1)"},
        ])
        status = {r["requirement"]: r["status"] for r in result["requirements"]}
        self.assertEqual(status["SQL"], "met")
        self.assertEqual(status["Power BI dashboards"], "not_met")      # tool not in CV
        self.assertEqual(status["Big Data experience"], "not_met")      # evidence not in CV
        self.assertEqual(status["Fluent English"], "met")
        # weights: required 2, preferred 1 -> earned 2+2 of 2+2+1+2 = 4/7
        self.assertEqual(result["score"], round(100 * 4 / 7))
        self.assertIn("Power BI dashboards", [g["requirement"] for g in result["gaps"]])

    def test_missing_required_costs_more_than_missing_preferred(self):
        base = [{"requirement": "SQL", "type": "required", "status": "met", "evidence": "SQL"},
                {"requirement": "Python", "type": "required", "status": "met", "evidence": "Python"}]
        missing_required = self.score(base + [{"requirement": "Tableau", "type": "required", "status": "not_met"}])
        missing_preferred = self.score(base + [{"requirement": "Tableau", "type": "preferred", "status": "not_met"}])
        self.assertLess(missing_required["score"], missing_preferred["score"])

    def test_bad_answers_give_no_score(self):
        self.assertIsNone(self.score([{"requirement": "SQL", "status": "met", "evidence": "SQL"}])["score"])
        self.assertIsNone(self.score("not a list")["score"])
        broken = match_scorer.score_job(JOB, content=MASTER,
                                        chat_fn=lambda **k: (_ for _ in ()).throw(ConnectionError("down")))
        self.assertIn("Ollama", broken["error"])

    def test_graduation_year_and_soft_traits(self):
        result = self.score([
            {"requirement": "Bachelor's or master's degree in 2028", "type": "required", "status": "met",
             "evidence": "Management Information Systems"},
            {"requirement": "Passion to learn and grow yourself", "type": "required", "status": "met",
             "evidence": "Python Training"},
            {"requirement": "SQL", "type": "required", "status": "met", "evidence": "SQL"},
            {"requirement": "Python", "type": "required", "status": "met", "evidence": "Python"},
        ])
        status = {r["requirement"]: r["status"] for r in result["requirements"]}
        self.assertEqual(status["Bachelor's or master's degree in 2028"], "partial")
        self.assertNotIn("Passion to learn and grow yourself", status)
        self.assertEqual(result["to_check"][0]["requirement"], "Bachelor's or master's degree in 2028")
        self.assertEqual(result["score"], round(100 * 5 / 6))
        text = telegram_notifier.job_message(dict(JOB, match=result))
        self.assertIn("CHECK YOURSELF", text)

    def test_graduation_year_compared_with_cv(self):
        content = json.loads(json.dumps(MASTER))
        content["education"][0]["highlight"] = "GPA: 3.35  |  Expected 2027"
        reqs = [{"requirement": "Bachelor's or master's degree in 2028", "type": "required", "status": "met",
                 "evidence": "Management Information Systems"},
                {"requirement": "SQL", "type": "required", "status": "met", "evidence": "SQL"},
                {"requirement": "Python", "type": "required", "status": "met", "evidence": "Python"}]
        wrong_year = match_scorer.score_job(JOB, content=content, chat_fn=fake_chat(reqs))
        degree = wrong_year["requirements"][0]
        self.assertEqual(degree["status"], "not_met")
        self.assertIn("2027", degree["note"])
        reqs[0]["requirement"] = "Graduating in 2027"
        reqs[0]["status"] = "not_met"
        right_year = match_scorer.score_job(JOB, content=content, chat_fn=fake_chat(reqs))
        self.assertEqual(right_year["requirements"][0]["status"], "met")
        self.assertEqual(right_year["score"], 100)

    def test_alternatives_and_cv_skills(self):
        result = self.score([
            {"requirement": "Proficiency in R, Python, or Stata", "type": "required", "status": "not_met"},
            {"requirement": "Commitment to the UN Charter", "type": "required", "status": "not_met"},
            {"requirement": "SQL", "type": "required", "status": "met", "evidence": "SQL"},
            {"requirement": "Excel", "type": "required", "status": "met", "evidence": "Excel"},
        ])
        status = {r["requirement"]: r["status"] for r in result["requirements"]}
        self.assertEqual(status["Proficiency in R, Python, or Stata"], "met")     # CV lists Python and R
        self.assertNotIn("Commitment to the UN Charter", status)                  # value statement, not scored
        self.assertEqual(result["score"], 100)

    def test_scoring_uses_fixed_settings(self):
        seen = {}

        def chat(**kwargs):
            seen.update(kwargs.get("options") or {})
            return SimpleNamespace(message=SimpleNamespace(content=json.dumps({"requirements": []})))
        match_scorer.score_job(JOB, content=MASTER, chat_fn=chat)
        self.assertEqual((seen.get("temperature"), seen.get("seed")), (0, 42))

    def test_interest_and_communication_not_scored(self):
        for text in ("Interest in software projects and business analysis", "Ability to communicate effectively",
                     "Strong communication skills"):
            self.assertTrue(match_scorer.is_soft_trait(text), text)
        for text in ("Basic knowledge of SQL", "C1 level English proficiency",
                     "Experience creating test scenarios", "Undergraduate degree in MIS"):
            self.assertFalse(match_scorer.is_soft_trait(text), text)

    def test_labels(self):
        self.assertEqual([match_scorer.label(s) for s in (90, 65, 45, 10, None)],
                         ["Strong match", "Good match", "Partial match", "Weak match", "not available"])


class IntegrationTests(unittest.TestCase):
    def test_best_match_sent_first_and_saved(self):
        db = Path(tempfile.mkdtemp()) / "t.db"
        jobs = [dict(JOB, job_title="Data Intern A", company="Alpha", url="https://a.com/1",
                     screening_decision="CANDIDATE", location="Istanbul",
                     description_text="Alpha builds payment dashboards with SQL and Python. " * 10),
                dict(JOB, job_title="Data Intern B", company="Beta", url="https://b.com/1",
                     screening_decision="CANDIDATE", location="Istanbul",
                     description_text="Beta analyses retail sales data in Excel and R every week. " * 10)]
        scores = {"Data Intern A": 40, "Data Intern B": 85}
        sent = []
        with closing(job_store.connect(db)) as connection:
            daily_pipeline.process_jobs(
                jobs, connection=connection,
                notify=lambda job, cv, cv_only=False: sent.append(job["job_title"]),
                tailor=lambda job: {"status": "ok", "docx": "a.docx", "pdf": "a.pdf"},
                scorer=lambda job: {"score": scores[job["job_title"]], "label": "x", "strengths": [], "gaps": []})
            saved = dict(connection.execute("SELECT job_title, match_score FROM jobs").fetchall())
        self.assertEqual(sent, ["Data Intern B", "Data Intern A"])
        self.assertEqual(saved["Data Intern B"], 85)

    def test_message_shows_score_strengths_and_gaps(self):
        job = dict(JOB, match={"score": 72, "label": "Good match",
                               "strengths": [{"requirement": "SQL", "evidence": "Python, SQL, R"}],
                               "gaps": [{"requirement": "Power BI", "type": "required"}]})
        text = telegram_notifier.job_message(job)
        self.assertIn("MATCH SCORE: 72/100", text)
        self.assertIn("WHY IT MATCHES", text)
        self.assertIn("Power BI", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
