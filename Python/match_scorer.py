"""
match_scorer.py
===============

Match score (0-100) between a job and the CV - explainable and not random.

1. Qwen lists the concrete requirements of the job ad (skills, tools, education,
   languages, experience, availability) and, for each one, says whether the CV
   meets it - and must COPY the supporting words from the CV as evidence.
2. Python checks every claim:
     * "met" / "partial" without real evidence from the CV  -> counted as not met
     * a tool that is not in the CV (e.g. Power BI)          -> always not met
3. Python computes the score:
     required requirement = weight 2, preferred = weight 1
     met = 100 %, partial = 50 %, not met = 0 %
   score = weighted share of what is met.
   A missing REQUIRED item therefore always lowers the score visibly.
"""

from __future__ import annotations

import json
import re

import cv_tailor
from job_page_inspector import normalize_text

MAX_REQUIREMENTS = 12
# Temperature 0 + fixed seed: the same job gets the same answer every time.
SCORING_OPTIONS = {"temperature": 0, "seed": 42, "num_ctx": 8192}
MAX_DESCRIPTION_CHARS = 5000
WEIGHTS = {"required": 2.0, "preferred": 1.0}
CREDIT = {"met": 1.0, "partial": 0.5, "not_met": 0.0}


def label(score: int | None) -> str:
    if score is None:
        return "not available"
    if score >= 80:
        return "Strong match"
    if score >= 60:
        return "Good match"
    if score >= 40:
        return "Partial match"
    return "Weak match"


def cv_as_text(content: dict) -> str:
    lines = [content.get("summary", "")]
    for section in ("education", "experience", "projects"):
        for entry in content.get(section, []):
            lines.append(entry.get("title", "") + (" | " + entry["highlight"] if entry.get("highlight") else ""))
            lines += [f"- {b}" for b in entry.get("bullets", [])]
    for item in content.get("skills", []):
        lines.append(f"{item['label']}: {item['value']}" if isinstance(item, dict) else str(item))
    lines += [f"- {c}" for c in content.get("certifications", [])]
    return "\n".join(line for line in lines if line)


def build_prompt(content: dict, job: dict) -> list[dict]:
    system = f"""You compare a job advertisement with a candidate's CV.
Return ONLY JSON:
{{"requirements": [{{"requirement": "...", "type": "required", "status": "met", "evidence": "..."}}]}}
Rules:
- List the {MAX_REQUIREMENTS} or fewer most important CONCRETE requirements of the job ad: skills, tools,
  education/degree, graduation year, languages, experience, availability. Do NOT list personality traits
  (passion, motivation, curiosity, attitude, team player).
- If the requirement contains a date or number (e.g. "degree in 2028", "2 years"), it is "met" only if the CV
  shows that exact date or number.
- If the ad gives ALTERNATIVES ("one of the following", "or"), write them as ONE requirement
  (e.g. "Final-year Bachelor's OR Master's student OR recent graduate"), never as separate requirements.
- "type": "preferred" only if the ad says preferred / a plus / nice to have / desirable / advantage;
  otherwise "required".
- "status": "met" only if the CV clearly shows it; "partial" if the CV shows something close;
  otherwise "not_met". Never guess.
- "evidence": copy the exact supporting words from the CV (a few words). Empty if not_met."""
    user = (f"CV:\n{cv_as_text(content)}\n\nJOB TITLE: {job.get('job_title') or job.get('search_title')}\n"
            f"COMPANY: {job.get('company') or 'unknown'}\n"
            f"JOB DESCRIPTION:\n{(job.get('description_text') or '')[:MAX_DESCRIPTION_CHARS]}")
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def evidence_found(evidence: str, cv_text: str) -> bool:
    """At least 80 % of the evidence words (3+ letters) must really be in the CV."""
    words = [w for w in re.findall(r"[a-z0-9+#]+", normalize_text(evidence)) if len(w) >= 2]
    if not words:
        return False
    cv_words = set(re.findall(r"[a-z0-9+#]+", normalize_text(cv_text)))
    return sum(w in cv_words for w in words) / len(words) >= 0.8


SOFT_TRAITS = re.compile(
    r"\b(passion\w*|passionate|motivated|motivation|eager\w*|curious|curiosity|enthusias\w*|attitude|"
    r"mindset|team player|self[- ]starter|proactive|hard[- ]working|positive|energetic|ambitious|"
    r"learn and grow|willing\w*|dedicated|detail[- ]oriented|growth|commitment|committed|integrity|"
    r"values|charter|sensitivity|discretion|interest|interested|interests|communicate|communication|"
    r"interpersonal|organi[sz]ed|organi[sz]ational|problem[- ]solving|analytical thinking|flexib\w*|"
    r"adaptab\w*|responsib\w*|work independently)\b")


def is_soft_trait(requirement: str) -> bool:
    """Personality traits cannot be proven from a CV, so they are not scored."""
    return bool(SOFT_TRAITS.search(normalize_text(requirement)))


def _unverified_numbers(requirement: str, evidence: str) -> list[str]:
    """Years and numbers in a requirement (e.g. 'degree in 2028') must appear in the evidence."""
    numbers = re.findall(r"\b(?:19|20)\d{2}\b|\b\d+\+?\s*(?:years?|months?|days?)\b", normalize_text(requirement))
    return [n for n in numbers if n not in normalize_text(evidence)]


_DEGREE_WORDS = re.compile(r"\b(degree|graduat\w*|bachelor\w*|master\w*|class of|mezun\w*|lisans)\b")


def cv_graduation_year(cv_text: str) -> str | None:
    match = re.search(r"\b(?:expected|class of|graduation:?)\s*(?:[a-z]+\s+)?((?:19|20)\d{2})\b",
                      normalize_text(cv_text))
    return match.group(1) if match else None


def _graduation_conflict(requirement: str, cv_text: str):
    """('not_met' | 'met' | None, note) for requirements like 'degree in 2028'."""
    req = normalize_text(requirement)
    years = re.findall(r"\b(?:19|20)\d{2}\b", req)
    cv_year = cv_graduation_year(cv_text)
    if not years or not cv_year or not _DEGREE_WORDS.search(req):
        return None, ""
    if cv_year in years:
        return "met", f"expected graduation {cv_year}"
    return "not_met", f"CV says expected graduation {cv_year}, the job asks for {', '.join(years)}"


def cv_skills(content: dict) -> list[str]:
    for item in content.get("skills", []):
        if isinstance(item, dict) and normalize_text(item.get("label")).startswith("technical"):
            return [s.strip() for s in item["value"].split(",") if s.strip()]
    return []


def _skills_in_requirement(requirement: str, skills: list[str]) -> list[str]:
    req = normalize_text(requirement)
    return [skill for skill in skills
            if re.search(rf"(?<![a-z0-9]){re.escape(normalize_text(skill))}(?![a-z0-9])", req)]


def _tool_missing(requirement: str, cv_text: str) -> str | None:
    req, cv = normalize_text(requirement), normalize_text(cv_text)
    for tool in cv_tailor.KNOWN_SKILLS:
        pattern = rf"(?<![a-z0-9]){re.escape(tool)}(?![a-z0-9])"
        if re.search(pattern, req) and not re.search(pattern, cv):
            return tool
    return None


def check_requirements(raw: list, cv_text: str, skills: list[str] | None = None) -> list[dict]:
    checked, seen = [], set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = " ".join(str(item.get("requirement") or "").split())[:200]
        key = normalize_text(text)
        if not text or key in seen:
            continue
        seen.add(key)
        if is_soft_trait(text):
            continue
        kind = str(item.get("type") or "").strip().casefold()
        kind = kind if kind in WEIGHTS else "required"
        status = str(item.get("status") or "").strip().casefold().replace(" ", "_").replace("-", "_")
        status = status if status in CREDIT else "not_met"
        evidence = " ".join(str(item.get("evidence") or "").split())[:200]
        note = ""
        if status != "not_met" and not evidence_found(evidence, cv_text):
            status, note = "not_met", "no supporting evidence in the CV"
        graduation, graduation_note = _graduation_conflict(text, cv_text)
        unverified = _unverified_numbers(text, evidence)
        if graduation is not None:
            status, note = graduation, graduation_note
            evidence = f"Expected {cv_graduation_year(cv_text)}" if graduation == "met" else ""
        elif unverified and status == "met":
            status = "partial"
            note = f"cannot be confirmed from the CV ({', '.join(unverified)}) - check yourself"
        # The CV's own skill list is the truth: "R, Python or Stata" is met if the CV lists R or Python.
        found_skills = _skills_in_requirement(text, skills or [])
        alternatives = bool(re.search(r"\b(or|veya|ya da)\b|/", normalize_text(text)))
        if found_skills and status == "not_met" and graduation is None:
            status = "met" if alternatives or len(found_skills) >= 2 else "partial"
            evidence, note = ", ".join(found_skills), "skill listed in the CV"
        missing_tool = _tool_missing(text, cv_text)
        satisfied_by_alternative = bool(found_skills) and alternatives
        if missing_tool and status != "not_met" and not satisfied_by_alternative:
            status, note = "not_met", f"'{missing_tool}' is not in the CV"
        checked.append({"requirement": text, "type": kind, "status": status,
                        "evidence": evidence if status != "not_met" else "", "note": note})
        if len(checked) >= MAX_REQUIREMENTS:
            break
    return checked


def compute_score(requirements: list[dict]) -> int | None:
    if len(requirements) < 3:
        return None          # too little information for an honest score
    total = sum(WEIGHTS[r["type"]] for r in requirements)
    earned = sum(WEIGHTS[r["type"]] * CREDIT[r["status"]] for r in requirements)
    return round(100 * earned / total)


def score_job(job: dict, *, content: dict | None = None, chat_fn=None) -> dict:
    """Returns {"score", "label", "strengths", "gaps", "requirements", "error"}. Never raises."""
    result = {"score": None, "label": label(None), "strengths": [], "gaps": [], "requirements": [], "error": None}
    if len(job.get("description_text") or "") < 200:
        result["error"] = "Job description too short for a match score."
        return result
    try:
        content = content or cv_tailor.cv_builder.load_content()
        data = cv_tailor.ask_qwen(build_prompt(content, job), chat_fn=chat_fn, options=SCORING_OPTIONS)
    except Exception as error:
        result["error"] = f"Qwen/Ollama error: {type(error).__name__}: {error}"
        return result

    requirements = check_requirements(data.get("requirements"), cv_as_text(content), cv_skills(content))
    score = compute_score(requirements)
    order = {"required": 0, "preferred": 1}
    met = [r for r in requirements if r["status"] == "met"]
    gaps = [r for r in requirements if r["status"] == "not_met"]
    to_check = [r for r in requirements if r["status"] == "partial"]
    result.update({
        "score": score, "label": label(score), "requirements": requirements,
        "strengths": sorted(met, key=lambda r: (order[r["type"]], r["status"] != "met"))[:4],
        "gaps": sorted(gaps, key=lambda r: order[r["type"]])[:4],
        "to_check": sorted(to_check, key=lambda r: order[r["type"]])[:3],
    })
    if score is None:
        result["error"] = "Too few concrete requirements could be read from the job ad."
    return result


if __name__ == "__main__":
    import sys
    from pathlib import Path

    if len(sys.argv) != 2:
        print("Usage: python match_scorer.py <job_search_report.json>   (scores its CANDIDATE jobs)")
        raise SystemExit(2)
    jobs = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")).get("jobs", [])
    for job in [j for j in jobs if j.get("screening_decision") in ("CANDIDATE", "MANUAL_REVIEW")]:
        match = score_job(job)
        print(f"\n{job.get('job_title')} - {job.get('company')}: {match['score']} ({match['label']})")
        for r in match["requirements"]:
            print(f"  [{r['status']:8}] ({r['type']}) {r['requirement']}  {r['evidence'] or r['note']}")
