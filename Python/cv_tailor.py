"""
cv_tailor.py
============

Creates a job-specific CV (Word + PDF) from the master CV content.

What Qwen may do
----------------
* rewrite the SUMMARY for the job,
* reorder the bullets inside each experience / project entry,
* lightly reword a bullet,
* reorder the technical skills.

What Python guarantees (every Qwen suggestion is checked)
--------------------------------------------------------
* No new tools / skills: a technology that is not in the master CV
  (e.g. "Power BI" or "Tableau" copied from the job ad) makes the text invalid.
* Numbers stay exact: "40+" hospitals, GPA 3.35 ... no new or changed numbers.
* No false claims: "graduated", "X years of experience", "expert" ... are rejected.
* "Research proposal" stays a proposal.
* Skills can only be reordered, never added or removed.
* Any invalid suggestion is replaced by the ORIGINAL text, and the reason is
  written to tailoring_report.json in the output folder.
* The design is untouched (cv_builder.py), the master files are never changed,
  existing output folders are never overwritten.

Usage (from the AI_Career_Agent folder):
    python Python\\cv_tailor.py                    -> CANDIDATE jobs of the newest report in Logs
    python Python\\cv_tailor.py --pick 2           -> only job number 2 of that list
    python Python\\cv_tailor.py --report Logs\\job_search_XXXX.json
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import cv_builder
import settings
from job_page_inspector import TIMEZONE, normalize_text

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = PROJECT_ROOT / "Logs"
OUTPUT_DIR = PROJECT_ROOT / "Generated_CVs"
MODEL = settings.DEFAULTS["ai"]["model"]   # the real model name comes from settings.json
OLLAMA_OPTIONS = {"temperature": 0.2, "num_ctx": 8192}
MAX_JOBS_PER_RUN = 5
MAX_SUMMARY_CHARS = 480
MAX_DESCRIPTION_CHARS = 5000

# Tools / skills that must never appear unless the master CV already contains them.
KNOWN_SKILLS = (
    "power bi", "tableau", "looker", "qlik", "spark", "hadoop", "aws", "azure", "gcp", "google cloud",
    "docker", "kubernetes", "java", "javascript", "typescript", "scala", "c++", "c#", "sas", "spss",
    "stata", "matlab", "vba", "dax", "airflow", "dbt", "snowflake", "bigquery", "databricks", "pandas",
    "numpy", "scikit-learn", "tensorflow", "pytorch", "keras", "langchain", "llm", "git", "github actions",
    "jira", "salesforce", "sap", "mongodb", "postgresql", "mysql", "nosql", "etl", "a/b testing",
    "big data", "deep learning", "computer vision", "econometrics", "figma", "alteryx", "kobo",
    "google analytics", "power apps", "power automate", "statistics", "statistical modeling",
    "data engineering", "data warehouse", "cloud", "html", "css", "react", "node.js", "flask", "django",
)

# Claims that are false for the user (still a student, internships only).
FORBIDDEN_CLAIMS = (
    r"\bgraduated\b", r"\bgraduate of\b", r"\bdegree holder\b", r"\bholds? a (bachelor|master)",
    r"\b\d+\+?\s*(years?|yrs?)\b", r"\byears? of (professional |work )?experience\b",
    r"\bexpert\b", r"\bseasoned\b", r"\bsenior (analyst|engineer|developer|consultant)\b",
    r"\bnative\b", r"\bfluent in (russian|german|french|spanish|turkish)\b",
    r"\bcertified\b", r"\bpublished\b", r"\baccepted (research|project)\b", r"\bfunded\b",
)

# Words that must survive a rewrite of a bullet if the original contains them.
MUST_KEEP = ("proposal", "40+", "tubitak", "2209-a", "faculty supervision", "led")

_STOPWORDS = {
    "with", "that", "this", "from", "your", "will", "have", "into", "their", "they", "them", "what", "which",
    "about", "while", "where", "when", "also", "such", "more", "most", "work", "working", "team", "teams",
    "role", "able", "ability", "strong", "skills", "using", "used", "based", "within", "across", "other",
    "well", "good", "great", "very", "each", "every", "some", "make", "made", "help", "helps", "including",
}


# ============================================================
# 1. VALIDATION
# ============================================================

def _all_text(content) -> str:
    if isinstance(content, dict):
        return " ".join(_all_text(v) for v in content.values())
    if isinstance(content, list):
        return " ".join(_all_text(v) for v in content)
    return str(content or "")


def _numbers(text) -> set[str]:
    return set(re.findall(r"\d+(?:[.,]\d+)?\+?", str(text or "")))


def _job_terms(description: str) -> set[str]:
    """
    Tool-like terms from the job ad: acronyms (KOBO, SPSS), CamelCase names (PowerPoint, BigQuery)
    and names with digits or symbols (C++, S4HANA). Ordinary capitalised words are not included,
    so normal vocabulary such as "Insights" may still be used.
    """
    terms = set()
    words = []
    for line in (description or "").splitlines():
        letters = [c for c in line if c.isalpha()]
        shouting = len(letters) >= 12 and sum(c.isupper() for c in letters) / len(letters) > 0.6
        for word in re.findall(r"[A-Za-z][A-Za-z0-9+#&./-]*", line):
            words.append((word, shouting))
    for word, shouting in words:
        word = word.strip(".-/")
        if normalize_text(word) in _COMMON_WORDS:
            continue
        is_acronym = 2 <= len(word) <= 8 and word.isupper() and not shouting
        is_camel = bool(re.search(r"[a-z][A-Z]", word))
        has_symbol = bool(re.search(r"[0-9+#]", word))
        if is_acronym or is_camel or has_symbol:
            terms.add(normalize_text(word))
    return {t for t in terms if t}


_COMMON_WORDS = {
    "a", "an", "the", "and", "or", "of", "to", "in", "into", "on", "at", "for", "with", "by", "from", "is",
    "are", "be", "you", "your", "we", "our", "us", "it", "as", "if", "all", "new", "can", "will", "not",
}

_GENERIC_OK = {
    "ai", "data", "analytics", "python", "sql", "excel", "english", "russian", "istanbul", "turkey",
    "the", "and", "machine", "learning", "nlp", "mvp", "prd", "prds", "living", "mis", "erasmus",
}


def find_problems(new_text: str, *, original_text: str, master_text: str, job_terms: set[str],
                  check_numbers_exact: bool) -> list[str]:
    """Return reasons why new_text is not acceptable (empty list = acceptable)."""
    problems = []
    new_norm = normalize_text(new_text)
    master_norm = normalize_text(master_text)
    original_norm = normalize_text(original_text)
    if not new_norm:
        return ["empty text"]

    for skill in KNOWN_SKILLS:
        if re.search(rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9])", new_norm) and \
                not re.search(rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9])", master_norm):
            problems.append(f"adds a skill/tool not in the CV: '{skill}'")

    master_words = set(re.findall(r"[a-z0-9+#&./-]+", master_norm))
    for word in set(re.findall(r"[a-z0-9+#&./-]+", new_norm)):
        word = word.strip(".-/")
        if word in job_terms and word not in master_words and word not in _GENERIC_OK and len(word) > 2:
            problems.append(f"copies a term from the job ad that is not in the CV: '{word}'")

    for pattern in FORBIDDEN_CLAIMS:
        if re.search(pattern, new_norm) and not re.search(pattern, original_norm):
            problems.append(f"makes a claim that is not true for the CV ('{pattern}')")

    new_numbers, original_numbers = _numbers(new_text), _numbers(original_text)
    allowed_numbers = original_numbers if check_numbers_exact else _numbers(master_text)
    if new_numbers - allowed_numbers:
        problems.append(f"adds or changes numbers: {sorted(new_numbers - allowed_numbers)}")
    if check_numbers_exact and original_numbers - new_numbers:
        problems.append(f"drops numbers: {sorted(original_numbers - new_numbers)}")

    for word in MUST_KEEP:
        pattern = rf"(?<![a-z0-9]){re.escape(word)}(?![a-z0-9])"
        if re.search(pattern, original_norm) and not re.search(pattern, new_norm):
            problems.append(f"drops an important word: '{word}'")
    return list(dict.fromkeys(problems))


# ============================================================
# 2. QWEN PROMPT
# ============================================================

def bullet_ids(content: dict) -> dict:
    """{'E1B1': (section, entry_index, bullet_index, text), ...}"""
    ids = {}
    for section, prefix in (("experience", "E"), ("projects", "P")):
        for e_index, entry in enumerate(content.get(section, [])):
            for b_index, text in enumerate(entry.get("bullets", [])):
                ids[f"{prefix}{e_index + 1}B{b_index + 1}"] = (section, e_index, b_index, text)
    return ids


def _technical_skills(content: dict):
    for index, item in enumerate(content.get("skills", [])):
        if isinstance(item, dict) and normalize_text(item.get("label")).startswith("technical"):
            return index, [s.strip() for s in item["value"].split(",") if s.strip()]
    return None, []


def build_prompt(content: dict, job: dict) -> list[dict]:
    ids = bullet_ids(content)
    _, skills = _technical_skills(content)
    cv_lines = [f"SUMMARY: {content.get('summary', '')}"]
    for section, prefix in (("experience", "E"), ("projects", "P")):
        for e_index, entry in enumerate(content.get(section, [])):
            cv_lines.append(f"{prefix}{e_index + 1} {entry['title']}")
            for bullet_id, (sec, e, b, text) in ids.items():
                if sec == section and e == e_index:
                    cv_lines.append(f"  {bullet_id}: {text}")
    cv_lines.append("TECHNICAL SKILLS: " + ", ".join(skills))
    education = content.get("education", [{}])[0].get("title", "")

    system = f"""You tailor a student's CV to one job. You may ONLY use facts that are in the CV below.
Rules:
- Never add a skill, tool, programme, number, degree or experience that is not in the CV.
- The person is a final-year student (not graduated). Do not mention years of experience.
- A research proposal must stay a "proposal".
- Keep every number exactly as written (e.g. 40+).
- Write in English. Summary: 2-3 sentences, max {MAX_SUMMARY_CHARS} characters, no first person "I".
- The summary must start like "Final-year Management Information Systems student ..." (write the degree in
  full, never "MIS") and connect the CV's real experience to what this job focuses on.
- Only reword a bullet if you add a word from the job description that the CV truly supports;
  otherwise return the bullet text unchanged. Keep "Led", "under faculty supervision" and all numbers.
Return ONLY JSON:
{{"summary": "...",
 "bullets": [{{"id": "E1B2", "text": "..."}}, ...],
 "skills_order": ["...", ...]}}
"bullets": every bullet id from the CV, most relevant to the job first; "text" may be lightly reworded to
use the job's vocabulary only where the CV already supports it.
"skills_order": exactly the same skills as TECHNICAL SKILLS, most relevant first."""

    user = (
        f"EDUCATION: {education}\n\nCV:\n" + "\n".join(cv_lines) +
        f"\n\nJOB TITLE: {job.get('job_title') or job.get('search_title')}\n"
        f"COMPANY: {job.get('company') or 'unknown'}\n"
        f"JOB DESCRIPTION:\n{(job.get('description_text') or '')[:MAX_DESCRIPTION_CHARS]}"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def ask_qwen(messages, *, chat_fn=None, options=None) -> dict:
    if chat_fn is None:
        from ollama import chat as chat_fn
    options = options or OLLAMA_OPTIONS
    try:
        response = chat_fn(model=settings.model(), messages=messages, format="json", think=False,
                           stream=False, options=options)
    except TypeError:
        response = chat_fn(model=settings.model(), messages=messages, format="json", stream=False, options=options)
    text = response.message.content if hasattr(response, "message") else response["message"]["content"]
    match = re.search(r"\{.*\}", text or "", re.S)
    data = json.loads(match.group(0) if match else text)
    if not isinstance(data, dict):
        raise ValueError("Qwen did not return a JSON object.")
    return data


# ============================================================
# 3. APPLY SUGGESTIONS SAFELY
# ============================================================

def _words(text) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9+#-]{3,}", normalize_text(text)) if w not in _STOPWORDS}


def _adds_job_vocabulary(original: str, new: str, job: dict) -> bool:
    """A rewrite is only worth it if it brings in at least one word that the job ad uses."""
    added = _words(new) - _words(original)
    job_words = _words(f"{job.get('job_title') or ''} {job.get('description_text') or ''}")
    return bool(added & job_words)


# Focus areas, recognised in the JOB TITLE and in the ROLE part of each CV entry title.
FOCUS_TAGS = {
    "data": r"\b(data|analyst|analytics|analysis|bi|business intelligence|insights?|reporting|quantitative|"
            r"veri|analist\w*|raporlama)\b",
    "ai": r"\b(ai|a\.i\.?|artificial intelligence|machine learning|ml|nlp|llm|yapay zeka)\b",
    "product": r"\b(product|urun)\b",
    "research": r"\b(research\w*|arastirma\w*|r&d)\b",
}


def focus_tags(text: str) -> set[str]:
    text = normalize_text(text)
    return {tag for tag, pattern in FOCUS_TAGS.items() if re.search(pattern, text)}


def entry_tags(entry: dict) -> set[str]:
    """Tags of the ROLE ('AI & Product Development Intern'), not of the organisation name."""
    return focus_tags(str(entry.get("title", "")).split("|")[0])


def _reorder_by_relevance(content: dict, job: dict, report: dict) -> None:
    """
    Simple, explainable rules based on the job TITLE:
      * an experience entry whose role matches MORE focus areas of the job title moves up
        (ties keep the original order);
      * the Projects section moves above Experience only for research jobs, when a project
        is itself a research project.
    """
    job_tags = focus_tags(job.get("job_title") or job.get("search_title") or "")
    if not job_tags:
        return
    experience = content.get("experience") or []
    if len(experience) > 1:
        scores = [len(entry_tags(entry) & job_tags) for entry in experience]
        order = sorted(range(len(experience)), key=lambda i: -scores[i])   # stable: ties keep order
        if order != list(range(len(experience))):
            content["experience"] = [experience[i] for i in order]
            report["accepted"].append({"part": "experience_order", "job_focus": sorted(job_tags),
                                       "new": [experience[i]["title"] for i in order]})

    projects = content.get("projects") or []
    order = list(content.get("section_order") or cv_builder.DEFAULT_ORDER)
    if "research" in job_tags and any("research" in entry_tags(p) for p in projects) \
            and "projects" in order and "experience" in order \
            and order.index("projects") > order.index("experience"):
        e, p = order.index("experience"), order.index("projects")
        order[e], order[p] = order[p], order[e]
        content["section_order"] = order
        report["accepted"].append({"part": "section_order", "job_focus": sorted(job_tags), "new": order})


def apply_suggestions(master: dict, suggestions: dict, job: dict) -> tuple[dict, dict]:
    """Return (tailored_content, report). Every change is validated; invalid ones are reverted."""
    content = copy.deepcopy(master)
    master_text = _all_text(master)
    terms = _job_terms(job.get("description_text") or "")
    report = {"accepted": [], "rejected": []}

    # Summary
    summary = str(suggestions.get("summary") or "").strip()
    if summary:
        problems = find_problems(summary, original_text=master.get("summary", ""), master_text=master_text,
                                 job_terms=terms, check_numbers_exact=False)
        if len(summary) > MAX_SUMMARY_CHARS:
            problems.append(f"too long ({len(summary)} > {MAX_SUMMARY_CHARS} characters)")
        summary_norm = normalize_text(summary)
        if "management information systems" not in summary_norm:
            problems.append("must name the degree in full: 'Management Information Systems'")
        if "student" not in summary_norm:
            problems.append("must say that the person is a student")
        if problems:
            report["rejected"].append({"part": "summary", "suggestion": summary, "reasons": problems})
        else:
            content["summary"] = summary
            report["accepted"].append({"part": "summary", "new": summary})

    # Bullets: order + light rewording
    ids = bullet_ids(master)
    order = []
    rewrites = {}
    for item in suggestions.get("bullets") or []:
        if not isinstance(item, dict) or item.get("id") not in ids or item["id"] in order:
            continue
        order.append(item["id"])
        text = str(item.get("text") or "").strip()
        original = ids[item["id"]][3]
        if text and text != original:
            problems = find_problems(text, original_text=original, master_text=master_text,
                                     job_terms=terms, check_numbers_exact=True)
            if len(text) > len(original) * 1.3 + 20:
                problems.append("much longer than the original (page would overflow)")
            if not problems and not _adds_job_vocabulary(original, text, job):
                problems.append("cosmetic change only (no job-relevant word added); original wording kept")
            if problems:
                report["rejected"].append({"part": item["id"], "suggestion": text, "reasons": problems})
            else:
                rewrites[item["id"]] = text
                report["accepted"].append({"part": item["id"], "old": original, "new": text})
    order += [bullet_id for bullet_id in ids if bullet_id not in order]

    for section in ("experience", "projects"):
        for e_index, entry in enumerate(content.get(section, [])):
            entry_ids = [i for i in order if ids[i][0] == section and ids[i][1] == e_index]
            entry["bullets"] = [rewrites.get(i, ids[i][3]) for i in entry_ids]

    # Experience entries and the Projects section: most relevant first (decided by Python, not Qwen)
    _reorder_by_relevance(content, job, report)

    # Skills: reorder only
    index, skills = _technical_skills(master)
    proposed = [str(s).strip() for s in suggestions.get("skills_order") or []]
    if index is not None and proposed:
        if sorted(map(normalize_text, proposed)) == sorted(map(normalize_text, skills)):
            by_norm = {normalize_text(s): s for s in skills}
            content["skills"][index]["value"] = ", ".join(by_norm[normalize_text(s)] for s in proposed)
            report["accepted"].append({"part": "skills_order", "new": content["skills"][index]["value"]})
        else:
            report["rejected"].append({"part": "skills_order", "suggestion": proposed,
                                       "reasons": ["skills were added or removed (only reordering is allowed)"]})
    return content, report


# ============================================================
# 4. OUTPUT FILES
# ============================================================

def safe_folder_name(job: dict) -> str:
    raw = f"{job.get('company') or 'Company'}_{job.get('job_title') or job.get('search_title') or 'Job'}"
    name = normalize_text(raw)
    name = re.sub(r"[^a-z0-9]+", "_", name).strip("_")
    return "_".join(part.capitalize() for part in name.split("_"))[:80] or "Job"


def _file_part(text: str, limit: int) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "_", normalize_text(text)).strip("_")
    return "_".join(part.capitalize() for part in cleaned.split("_") if part)[:limit]


def cv_file_name(job: dict, person_name: str = "") -> str:
    """('Procter & Gamble', 'Mert Can Yalçın') -> 'Mert_Can_Yalcin_Procter_Gamble_CV' (seen by the recruiter)."""
    company = _file_part(job.get("company") or "", 40) or _file_part(job.get("job_title") or "", 40) or "Job"
    person = _file_part(person_name, 40)
    return f"{person}_{company}_CV" if person else f"{company}_CV"


def new_output_folder(job: dict) -> Path:
    base = OUTPUT_DIR / safe_folder_name(job)
    folder, counter = base, 2
    while folder.exists():                     # never overwrite an earlier CV
        folder = Path(f"{base}_{counter}")
        counter += 1
    folder.mkdir(parents=True)
    return folder


def convert_to_pdf(docx_path: Path) -> tuple[Path | None, str]:
    """Word (docx2pdf) gives the most faithful PDF on Windows; LibreOffice is the free fallback."""
    pdf_path = docx_path.with_suffix(".pdf")
    try:
        from docx2pdf import convert
        convert(str(docx_path), str(pdf_path))
        if pdf_path.exists():
            return pdf_path, "Microsoft Word (docx2pdf)"
    except Exception as error:  # not installed or Word not available
        word_error = f"{type(error).__name__}: {error}"
    else:
        word_error = "docx2pdf produced no file"

    soffice = shutil.which("soffice") or shutil.which("libreoffice")
    for candidate in (r"C:\Program Files\LibreOffice\program\soffice.exe",
                      r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
        if not soffice and Path(candidate).exists():
            soffice = candidate
    if soffice:
        try:
            subprocess.run([soffice, "--headless", "--convert-to", "pdf", "--outdir",
                            str(docx_path.parent), str(docx_path)], check=True, timeout=120,
                           capture_output=True)
            if pdf_path.exists():
                return pdf_path, "LibreOffice"
        except (subprocess.SubprocessError, OSError) as error:
            return None, f"LibreOffice failed: {error}"
    return None, f"No PDF converter available ({word_error}). Run: pip install docx2pdf"


def tailor_for_job(job: dict, *, master: dict | None = None, chat_fn=None) -> dict:
    master = master or cv_builder.load_content()
    result = {"job_title": job.get("job_title"), "company": job.get("company"), "folder": None,
              "docx": None, "pdf": None, "status": "failed", "message": ""}
    if len(job.get("description_text") or "") < 200:
        result["message"] = "Job description too short to tailor the CV."
        return result
    try:
        suggestions = ask_qwen(build_prompt(master, job), chat_fn=chat_fn)
    except Exception as error:
        result["message"] = f"Qwen/Ollama error: {type(error).__name__}: {error}"
        return result

    content, report = apply_suggestions(master, suggestions, job)
    rejected_summary = next((r for r in report["rejected"] if r["part"] == "summary"), None)
    if rejected_summary:   # give Qwen one chance to fix the summary
        retry_messages = build_prompt(master, job) + [{
            "role": "user",
            "content": "Your summary was rejected: " + "; ".join(rejected_summary["reasons"]) +
                       '. Write a new summary that follows all rules. Return ONLY JSON: {"summary": "..."}',
        }]
        try:
            retry = ask_qwen(retry_messages, chat_fn=chat_fn)
            retry_content, retry_report = apply_suggestions(master, {"summary": retry.get("summary")}, job)
            accepted = [a for a in retry_report["accepted"] if a["part"] == "summary"]
            if accepted:
                content["summary"] = retry_content["summary"]
                report["accepted"].append({**accepted[0], "note": "accepted on second attempt"})
            else:
                report["rejected"] += [{**r, "note": "second attempt"} for r in retry_report["rejected"]
                                       if r["part"] == "summary"]
        except Exception as error:
            report["rejected"].append({"part": "summary", "reasons": [f"retry failed: {error}"]})
    folder = new_output_folder(job)
    docx_path = cv_builder.build_cv(content, folder / f"{cv_file_name(job, master.get('name', ''))}.docx")
    pdf_path, pdf_note = convert_to_pdf(docx_path)
    report.update({
        "job": {k: job.get(k) for k in ("job_title", "company", "location", "url", "publication_date")},
        "created_at": datetime.now(TIMEZONE).isoformat(), "pdf": pdf_note,
    })
    with open(folder / "tailoring_report.json", "x", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    with open(folder / "tailored_content.json", "x", encoding="utf-8") as handle:
        json.dump(content, handle, ensure_ascii=False, indent=2)

    result.update({"folder": str(folder), "docx": str(docx_path), "pdf": str(pdf_path) if pdf_path else None,
                   "status": "ok", "accepted": len(report["accepted"]), "rejected": len(report["rejected"]),
                   "message": pdf_note})
    return result


# ============================================================
# 5. COMMAND LINE
# ============================================================

def newest_report() -> Path | None:
    reports = sorted(LOG_DIR.glob("job_search_*.json"), key=lambda p: p.stat().st_mtime)
    return reports[-1] if reports else None


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    parser = argparse.ArgumentParser(description="Create job-specific CVs (Word + PDF).")
    parser.add_argument("--report", help="job search JSON report (default: newest in Logs)")
    parser.add_argument("--pick", type=int, help="only this job number from the candidate list")
    parser.add_argument("--include-review", action="store_true", help="also MANUAL_REVIEW jobs")
    args = parser.parse_args(argv)

    report_path = Path(args.report) if args.report else newest_report()
    if not report_path or not report_path.exists():
        print("No job search report found. Run job_search.py first.")
        return 1
    with open(report_path, encoding="utf-8") as handle:
        jobs = json.load(handle).get("jobs", [])
    wanted = {"CANDIDATE", "MANUAL_REVIEW"} if args.include_review else {"CANDIDATE"}
    jobs = [job for job in jobs if job.get("screening_decision") in wanted]

    print("Report:", report_path)
    if not jobs:
        print("No suitable jobs in this report.")
        return 0
    for number, job in enumerate(jobs, start=1):
        print(f"  [{number}] {job.get('job_title')} - {job.get('company')}")
    if args.pick:
        if not 1 <= args.pick <= len(jobs):
            print("Invalid --pick number.")
            return 1
        jobs = [jobs[args.pick - 1]]
    jobs = jobs[:MAX_JOBS_PER_RUN]

    for job in jobs:
        print(f"\nTailoring CV for: {job.get('job_title')} - {job.get('company')}  (Qwen is thinking...)")
        result = tailor_for_job(job)
        if result["status"] != "ok":
            print("  FAILED:", result["message"])
            continue
        print(f"  Word: {result['docx']}")
        print(f"  PDF:  {result['pdf'] or 'not created - ' + result['message']}")
        print(f"  Changes accepted: {result['accepted']}, rejected by safety checks: {result['rejected']}"
              "  (details: tailoring_report.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
