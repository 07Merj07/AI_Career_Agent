"""
selftest_cv.py
==============

Offline tests for cv_builder.py and cv_tailor.py (Qwen is simulated).
Run from the AI_Career_Agent folder:   python Python\\selftest_cv.py
"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from docx import Document

import cv_builder
import cv_tailor

MASTER = {
    "name": "Test Person",
    "contact": {"phone": "+90 500 000 00 00", "email": "test@example.com", "location": "Istanbul, Turkey",
                "links": [{"text": "GitHub: test", "url": "https://github.com/test"}]},
    "summary": "Senior Management Information Systems student with hands-on experience in data analytics and AI.",
    "education": [{"title": "Management Information Systems | Test University", "highlight": "GPA: 3.35",
                   "bullets": ["Ranked in the top 20,000 in the national exam."]}],
    "experience": [
        {"title": "AI & Data Intern | Hospital Group",
         "bullets": ["Consolidated, cleaned, and validated datasets from 40+ hospitals.",
                     "Developed routine reports and automated data workflows in Excel."]},
        {"title": "AI & Product Intern | Startup",
         "bullets": ["Worked with founders on AI-driven workflows and prompt engineering."]},
    ],
    "projects": [{"title": "Research Project Proposal | Team Leader",
                  "bullets": ["Led a team preparing a TÜBİTAK 2209-A research proposal on AI in psychotherapy."]}],
    "skills": [{"label": "Technical Skills", "value": "Python, SQL, R, Excel, Data Analysis"},
               {"label": "Languages", "value": "English (C1), Russian (B1)"}],
    "certifications": ["Python Training"],
}

JOB = {
    "job_title": "Data Analyst Intern", "company": "Acme & Co",
    "description_text": ("We are looking for a Data Analyst Intern. You will build dashboards in Power BI and "
                         "Tableau, write SQL, use KOBO and SPSS, and work with Big Data. ") * 4,
}


def fake_chat(payload):
    def chat(**kwargs):
        return SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))
    return chat


class BuilderTests(unittest.TestCase):
    def test_builds_docx_with_all_text(self):
        path = Path(tempfile.mkdtemp()) / "cv.docx"
        cv_builder.build_cv(MASTER, path)
        text = "\n".join(p.text for p in Document(path).paragraphs)
        for expected in ("TEST PERSON", "SUMMARY", "40+ hospitals", "GPA: 3.35", "Technical Skills:", "TÜBİTAK"):
            self.assertIn(expected, text)

    def test_invalid_content_rejected(self):
        with self.assertRaises(ValueError):
            cv_builder.build_cv({"name": ""}, Path(tempfile.mkdtemp()) / "x.docx")


class ValidationTests(unittest.TestCase):
    def apply(self, payload):
        return cv_tailor.apply_suggestions(MASTER, payload, JOB)

    def test_good_suggestions_are_applied(self):
        content, report = self.apply({
            "summary": "Final-year Management Information Systems student with hands-on experience in data "
                       "analytics, reporting and AI, seeking a data analyst internship.",
            "bullets": [{"id": "E1B2", "text": "Developed routine reports and automated data workflows in Excel."},
                        {"id": "E1B1", "text": "Consolidated, cleaned, and validated datasets from 40+ hospitals for analyst reporting."}],
            "skills_order": ["SQL", "Excel", "Python", "Data Analysis", "R"],
        })
        self.assertIn("seeking a data analyst internship", content["summary"])
        self.assertTrue(content["experience"][0]["bullets"][0].startswith("Developed"))
        self.assertEqual(content["skills"][0]["value"], "SQL, Excel, Python, Data Analysis, R")
        self.assertEqual(report["rejected"], [])

    def test_invented_tools_are_rejected(self):
        content, report = self.apply({
            "summary": "Student experienced in Power BI and Tableau dashboards.",
            "bullets": [{"id": "E1B2", "text": "Built Power BI dashboards and automated Excel workflows."},
                        {"id": "E2B1", "text": "Worked with founders on AI workflows using KOBO and prompt engineering."}],
        })
        self.assertEqual(content["summary"], MASTER["summary"])
        self.assertNotIn("power bi", json.dumps(content).lower())
        self.assertIn(MASTER["experience"][0]["bullets"][1], content["experience"][0]["bullets"])
        self.assertEqual(content["experience"][1]["bullets"][0], MASTER["experience"][1]["bullets"][0])
        self.assertEqual(len(report["rejected"]), 3)

    def test_numbers_and_claims_protected(self):
        content, report = self.apply({
            "summary": "Graduated analyst with 3 years of experience.",
            "bullets": [{"id": "E1B1", "text": "Cleaned datasets from 50+ hospitals."},
                        {"id": "P1B1", "text": "Led a team on an accepted TÜBİTAK 2209-A research project on AI."}],
        })
        self.assertEqual(content["summary"], MASTER["summary"])
        self.assertIn("40+", content["experience"][0]["bullets"][0])
        self.assertIn("proposal", content["projects"][0]["bullets"][0])
        self.assertEqual(len(report["rejected"]), 3)

    def test_skills_cannot_be_added(self):
        content, report = self.apply({"skills_order": ["Python", "SQL", "Power BI", "R", "Excel"]})
        self.assertEqual(content["skills"][0]["value"], MASTER["skills"][0]["value"])
        self.assertEqual(report["rejected"][0]["part"], "skills_order")

    def test_missing_bullets_are_kept(self):
        content, _ = self.apply({"bullets": [{"id": "E1B2", "text": ""}]})
        self.assertEqual(len(content["experience"][0]["bullets"]), 2)
        self.assertEqual(content["experience"][0]["bullets"][0], MASTER["experience"][0]["bullets"][1])

    def test_quality_rules(self):
        master = json.loads(json.dumps(MASTER))
        master["projects"][0]["bullets"][0] += " under faculty supervision"
        content, report = cv_tailor.apply_suggestions(master, {
            "summary": "Final-year MIS student with data analytics experience.",
            "bullets": [{"id": "E1B2", "text": "Built routine reports and automated data workflows in Excel."},
                        {"id": "P1B1", "text": "Led a team preparing a TÜBİTAK 2209-A research proposal on AI in psychotherapy."}],
        }, JOB)
        reasons = " ".join(" ".join(r["reasons"]) for r in report["rejected"])
        self.assertIn("Management Information Systems", reasons)
        self.assertIn("cosmetic change only", reasons)
        self.assertIn("faculty supervision", reasons)
        self.assertEqual(content["summary"], master["summary"])

    def test_summary_retry(self):
        answers = iter([{"summary": "MIS student."},
                        {"summary": "Final-year Management Information Systems student focused on data analytics."}])

        def chat(**kwargs):
            return SimpleNamespace(message=SimpleNamespace(content=json.dumps(next(answers))))

        out = Path(tempfile.mkdtemp())
        with mock.patch.object(cv_tailor, "OUTPUT_DIR", out), \
                mock.patch.object(cv_tailor, "convert_to_pdf", return_value=(None, "skipped")):
            result = cv_tailor.tailor_for_job(JOB, master=MASTER, chat_fn=chat)
        content = json.loads((Path(result["folder"]) / "tailored_content.json").read_text(encoding="utf-8"))
        self.assertTrue(content["summary"].startswith("Final-year Management Information Systems"))

    def test_order_follows_job_title(self):
        master = json.loads(json.dumps(MASTER))
        master["experience"].append({"title": "Board Member & Team Leader | AI for Social Benefit Club",
                                     "bullets": ["Led student teams."]})
        product, report = cv_tailor.apply_suggestions(master, {}, {"job_title": "AI Product Intern",
                                                                   "description_text": ""})
        self.assertTrue(product["experience"][0]["title"].startswith("AI & Product Intern"))
        self.assertTrue(product["experience"][2]["title"].startswith("Board Member"))   # club stays last
        data, _ = cv_tailor.apply_suggestions(master, {}, {"job_title": "Data Analyst Intern", "description_text": ""})
        self.assertEqual([e["title"] for e in data["experience"]], [e["title"] for e in master["experience"]])
        self.assertNotIn("section_order", data)
        research, _ = cv_tailor.apply_suggestions(master, {}, {"job_title": "Quantitative Research Intern",
                                                               "description_text": ""})
        order = research["section_order"]
        self.assertLess(order.index("projects"), order.index("experience"))

    def test_master_is_never_modified(self):
        before = json.dumps(MASTER, sort_keys=True)
        self.apply({"summary": "Data analytics student.", "skills_order": ["R", "SQL", "Python", "Excel", "Data Analysis"]})
        self.assertEqual(json.dumps(MASTER, sort_keys=True), before)


class EndToEndTests(unittest.TestCase):
    def test_tailor_creates_new_folder_and_never_overwrites(self):
        out = Path(tempfile.mkdtemp())
        payload = {"summary": "Final-year Management Information Systems student focused on data analytics and AI.",
                   "bullets": [], "skills_order": []}
        with mock.patch.object(cv_tailor, "OUTPUT_DIR", out), \
                mock.patch.object(cv_tailor, "convert_to_pdf", return_value=(None, "skipped in test")):
            first = cv_tailor.tailor_for_job(JOB, master=MASTER, chat_fn=fake_chat(payload))
            second = cv_tailor.tailor_for_job(JOB, master=MASTER, chat_fn=fake_chat(payload))
        self.assertEqual(first["status"], "ok")
        self.assertNotEqual(first["folder"], second["folder"])
        self.assertTrue(Path(first["docx"]).exists())
        report = json.loads((Path(first["folder"]) / "tailoring_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["accepted"][0]["part"], "summary")

    def test_ollama_error_is_reported(self):
        def broken(**kwargs):
            raise ConnectionError("Ollama is not running")
        result = cv_tailor.tailor_for_job(JOB, master=MASTER, chat_fn=broken)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Ollama", result["message"])

    def test_cv_file_name_uses_company(self):
        self.assertEqual(cv_tailor.cv_file_name({"company": "Vodafone"}, "Mert Can Yalçın"), "Mert_Can_Yalcin_Vodafone_CV")
        self.assertEqual(cv_tailor.cv_file_name({"company": "Procter & Gamble"}), "Procter_Gamble_CV")
        self.assertEqual(cv_tailor.cv_file_name({"company": "Türk Telekom A.Ş."}), "Turk_Telekom_A_S_CV")
        self.assertEqual(cv_tailor.cv_file_name({}), "Job_CV")
        self.assertEqual(cv_tailor.cv_file_name({"job_title": "Quantitative Research Intern"}, "Mert Can Yalçın"),
                         "Mert_Can_Yalcin_Quantitative_Research_Intern_CV")

    def test_folder_name_is_safe(self):
        self.assertEqual(cv_tailor.safe_folder_name({"company": "Procter & Gamble",
                                                     "job_title": "Summer Internship 2027: Analytics & Insights"}),
                         "Procter_Gamble_Summer_Internship_2027_Analytics_Insights")


if __name__ == "__main__":
    unittest.main(verbosity=2)
