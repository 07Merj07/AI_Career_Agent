"""
cover_letter.py
===============

Writes a short, job-specific cover letter (Word + PDF) from the CV content.

The same safety rules as the tailored CV apply to every paragraph
(cv_tailor.find_problems):
  * no skill, tool, number, degree or experience that is not in the CV,
  * no false claims ("graduated", "3 years of experience", "expert" ...),
  * a paragraph that breaks a rule is rewritten once; if it is still wrong it is dropped.
If fewer than 2 valid paragraphs remain, no cover letter is created (better none than a wrong one).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

import cv_builder
import cv_tailor
from job_page_inspector import TIMEZONE, normalize_text

MAX_PARAGRAPH_CHARS = 700
MIN_PARAGRAPHS = 2


def build_prompt(content: dict, job: dict) -> list[dict]:
    from match_scorer import cv_as_text
    system = """You write a short cover letter for a student, using ONLY facts from the CV below.
Return ONLY JSON: {"paragraphs": ["...", "...", "..."]}
Write exactly 3 paragraphs, 60-110 words each, in English, first person ("I"):
1. Which role at which company, and who the candidate is (final-year student, degree name in full).
2. The one or two CV experiences that fit this job best, with their real results/numbers from the CV.
3. Why this company/role interests the candidate (based on the job ad) and a polite closing.
Rules: never add a skill, tool, programme, number, degree or experience that is not in the CV;
never claim to have graduated or to have "X years of experience"; a research proposal stays a "proposal";
no greeting line and no signature (they are added automatically)."""
    user = (f"CV:\n{cv_as_text(content)}\n\nJOB TITLE: {job.get('job_title') or job.get('search_title')}\n"
            f"COMPANY: {job.get('company') or 'the company'}\n"
            f"JOB DESCRIPTION:\n{(job.get('description_text') or '')[:4000]}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _allowed_job_terms(job: dict) -> set[str]:
    """Tool-like terms of the ad, minus the company and job-title words (those may be named)."""
    terms = cv_tailor._job_terms(job.get("description_text") or "")
    own_words = set(re.findall(r"[a-z0-9+#&./-]+", normalize_text(f"{job.get('company')} {job.get('job_title')}")))
    return {t for t in terms if t not in own_words}


def check_paragraph(text: str, content: dict, job: dict) -> list[str]:
    master_text = cv_tailor._all_text(content) + " " + str(job.get("company") or "") + " " + \
        str(job.get("job_title") or "")
    problems = cv_tailor.find_problems(text, original_text="", master_text=master_text,
                                       job_terms=_allowed_job_terms(job), check_numbers_exact=False)
    if len(text) > MAX_PARAGRAPH_CHARS:
        problems.append("paragraph too long")
    if len(text.split()) < 25:
        problems.append("paragraph too short")
    return problems


def write_paragraphs(content: dict, job: dict, *, chat_fn=None) -> tuple[list[str], list[dict]]:
    """Returns (valid paragraphs, rejected [{text, reasons}])."""
    data = cv_tailor.ask_qwen(build_prompt(content, job), chat_fn=chat_fn)
    paragraphs = [" ".join(str(p).split()) for p in (data.get("paragraphs") or []) if str(p).strip()][:3]
    valid, rejected = [], []
    for index, text in enumerate(paragraphs):
        problems = check_paragraph(text, content, job)
        if problems:   # one rewrite with the reasons
            retry_messages = build_prompt(content, job) + [{
                "role": "user",
                "content": f"Paragraph {index + 1} was rejected: {'; '.join(problems)}. Rewrite only this paragraph "
                           f'following every rule. Return ONLY JSON: {{"paragraph": "..."}}\nParagraph: {text}'}]
            try:
                retry = cv_tailor.ask_qwen(retry_messages, chat_fn=chat_fn)
                new_text = " ".join(str(retry.get("paragraph") or "").split())
            except Exception:
                new_text = ""
            new_problems = check_paragraph(new_text, content, job) if new_text else ["no rewrite"]
            if new_problems:
                rejected.append({"text": text, "reasons": problems, "rewrite": new_text, "rewrite_reasons": new_problems})
                continue
            text = new_text
        valid.append(text)
    return valid, rejected


def build_document(content: dict, job: dict, paragraphs: list[str], output_path: Path) -> Path:
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.left_margin = section.right_margin = Inches(1)
    section.top_margin = section.bottom_margin = Inches(0.8)
    normal = document.styles["Normal"]
    normal.font.name, normal.font.size = cv_builder.FONT, Pt(10.5)

    heading = document.add_paragraph()
    cv_builder._format(heading, after=2, line=1.0)
    cv_builder._run(heading, content["name"].upper(), size=cv_builder.NAME_SIZE, bold=True, color=cv_builder.BLUE)
    contact = content.get("contact", {})
    line = document.add_paragraph()
    cv_builder._format(line, after=14)
    cv_builder._run(line, "  |  ".join(contact.get(k) for k in ("phone", "email", "location") if contact.get(k)),
                    size=Pt(9.5), color=cv_builder.GREY)

    for text, after in ((datetime.now(TIMEZONE).strftime("%d %B %Y"), 12),
                        (f"Dear {job.get('company') or 'Hiring'} Hiring Team,"
                         if job.get("company") else "Dear Hiring Team,", 10)):
        paragraph = document.add_paragraph()
        cv_builder._format(paragraph, after=after, line=1.15)
        cv_builder._run(paragraph, text, size=Pt(10.5))
    for text in paragraphs:
        paragraph = document.add_paragraph()
        cv_builder._format(paragraph, after=10, line=1.2)
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        cv_builder._run(paragraph, text, size=Pt(10.5))
    for text, bold in (("Sincerely,", False), (content["name"], True)):
        paragraph = document.add_paragraph()
        cv_builder._format(paragraph, after=2, line=1.15)
        cv_builder._run(paragraph, text, size=Pt(10.5), bold=bold)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    return output_path


def create_for_job(job: dict, folder: Path | None = None, *, content: dict | None = None, chat_fn=None) -> dict:
    """Returns {"status": "ok"|"failed", "docx", "pdf", "message", "rejected"}. Never raises."""
    result = {"status": "failed", "docx": None, "pdf": None, "message": "", "rejected": []}
    if len(job.get("description_text") or "") < 200:
        result["message"] = "Job description too short for a cover letter."
        return result
    try:
        content = content or cv_builder.load_content()
        paragraphs, rejected = write_paragraphs(content, job, chat_fn=chat_fn)
    except Exception as error:
        result["message"] = f"Qwen/Ollama error: {type(error).__name__}: {error}"
        return result
    result["rejected"] = rejected
    if len(paragraphs) < MIN_PARAGRAPHS:
        result["message"] = "Not enough paragraphs passed the safety checks; no cover letter created."
        return result
    folder = Path(folder) if folder else cv_tailor.new_output_folder(job)
    name = cv_tailor.cv_file_name(job, content.get("name", "")).removesuffix("_CV") + "_Cover_Letter"
    docx_path = build_document(content, job, paragraphs, folder / f"{name}.docx")
    pdf_path, note = cv_tailor.convert_to_pdf(docx_path)
    (folder / "cover_letter_report.json").write_text(json.dumps(
        {"paragraphs": paragraphs, "rejected": rejected, "pdf": note}, ensure_ascii=False, indent=2), encoding="utf-8")
    result.update({"status": "ok", "docx": str(docx_path), "pdf": str(pdf_path) if pdf_path else None,
                   "message": note})
    return result
