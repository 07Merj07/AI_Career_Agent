"""
cv_builder.py
=============

Builds the CV as a Word (.docx) file with the SAME design as the original
Main CV (measured from the original PDF):

    Page        US Letter, margins 0.6" left/right, 0.5" top/bottom
    Name        Arial Bold 15.5 pt, dark blue #1B4F72, centred
    Contact     Arial 9.5 pt, grey #646464, centred, GitHub/Kaggle as links
    Headings    Arial Bold 10.5 pt, dark blue #1B4F72, upper case
    Entry line  Arial Bold 9.5 pt, near-black #141414
    Body        Arial 9 pt, dark grey #282828
    Bullets     "•" with 0.25" hanging indent

The CONTENT comes from a JSON file (CV/cv_content.json), never from code,
so a tailored CV is simply a modified copy of that content. The builder
never invents text: it only lays out what it is given.

Usage:
    python cv_builder.py                      -> builds CV/Template_Check.docx
    python cv_builder.py content.json out.docx
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.shared import Inches, Pt, RGBColor

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONTENT = PROJECT_ROOT / "CV" / "cv_content.json"

FONT = "Arial"
LINK_FONT = "Aptos"
BLUE = RGBColor(0x1B, 0x4F, 0x72)
BODY = RGBColor(0x28, 0x28, 0x28)
DARK = RGBColor(0x14, 0x14, 0x14)
GREY = RGBColor(0x64, 0x64, 0x64)

NAME_SIZE = Pt(15.5)
CONTACT_SIZE = Pt(9.5)
HEADING_SIZE = Pt(10.5)
ENTRY_SIZE = Pt(9.5)
BODY_SIZE = Pt(9)
LINE_SPACING = Pt(12)          # measured distance between wrapped body lines
BULLET_INDENT = Inches(0.25)

SECTION_TITLES = {
    "summary": "SUMMARY",
    "education": "EDUCATION",
    "experience": "EXPERIENCE",
    "projects": "PROJECTS",
    "skills": "SKILLS & LANGUAGES",
    "certifications": "CERTIFICATIONS",
}
DEFAULT_ORDER = ("summary", "education", "experience", "projects", "skills", "certifications")


# ============================================================
# Low-level helpers
# ============================================================

def _run(paragraph, text, *, size=BODY_SIZE, bold=False, color=BODY, font=FONT):
    run = paragraph.add_run(text)
    run.font.name = font
    run.font.size = size
    run.font.bold = bold
    run.font.color.rgb = color
    # Make sure East-Asian/complex-script slots also use the font (keeps "İ", "ç" etc. consistent).
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attribute in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attribute), font)
    return run


def _format(paragraph, *, before=0, after=0, line=LINE_SPACING, align=None, keep_next=False):
    fmt = paragraph.paragraph_format
    fmt.space_before = Pt(before)
    fmt.space_after = Pt(after)
    fmt.line_spacing = line
    fmt.keep_with_next = keep_next
    if align is not None:
        paragraph.alignment = align
    return paragraph


def _hyperlink(paragraph, text, url, *, size=Pt(9), color=GREY):
    part = paragraph.part
    rel_id = part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), rel_id)
    run = OxmlElement("w:r")
    rpr = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    for attribute in ("w:ascii", "w:hAnsi", "w:cs"):
        fonts.set(qn(attribute), LINK_FONT)
    rpr.append(fonts)
    colour = OxmlElement("w:color")
    colour.set(qn("w:val"), str(color))
    rpr.append(colour)
    for tag, value in (("w:sz", str(int(size.pt * 2))), ("w:szCs", str(int(size.pt * 2)))):
        element = OxmlElement(tag)
        element.set(qn("w:val"), value)
        rpr.append(element)
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    rpr.append(underline)
    run.append(rpr)
    text_element = OxmlElement("w:t")
    text_element.text = text
    text_element.set(qn("xml:space"), "preserve")
    run.append(text_element)
    link.append(run)
    paragraph._p.append(link)


def _bullet(document, *, before=0, after=2):
    paragraph = document.add_paragraph(style="List Bullet")
    _format(paragraph, before=before, after=after)
    fmt = paragraph.paragraph_format
    fmt.left_indent = BULLET_INDENT
    fmt.first_line_indent = -BULLET_INDENT
    return paragraph


# ============================================================
# Sections
# ============================================================

def _heading(document, title, *, first=False):
    paragraph = document.add_paragraph()
    _format(paragraph, before=12 if first else 26, after=5, keep_next=True)
    _run(paragraph, title, size=HEADING_SIZE, bold=True, color=BLUE)


def _entry_title(document, text, highlight=None, *, before=0):
    paragraph = document.add_paragraph()
    _format(paragraph, before=before, after=3, keep_next=True)
    _run(paragraph, text, size=ENTRY_SIZE, bold=True, color=DARK)
    if highlight:
        _run(paragraph, "  |  " + highlight, size=ENTRY_SIZE, bold=True, color=BLUE)


def _bullets(document, items):
    for item in items:
        paragraph = _bullet(document)
        if isinstance(item, dict):   # {"label": "Technical Skills", "value": "..."}
            _run(paragraph, f"{item['label']}: ", bold=True)
            _run(paragraph, item["value"])
        else:
            _run(paragraph, str(item))


def _entries(document, entries):
    for index, entry in enumerate(entries):
        _entry_title(document, entry["title"], entry.get("highlight"), before=0 if index == 0 else 5)
        _bullets(document, entry.get("bullets", []))


# ============================================================
# Public function
# ============================================================

def validate_content(content: dict) -> list[str]:
    """Return a list of problems (empty list = OK)."""
    problems = []
    for key in ("name", "contact"):
        if not content.get(key):
            problems.append(f"Missing '{key}'.")
    for section in content.get("section_order", DEFAULT_ORDER):
        if section not in SECTION_TITLES:
            problems.append(f"Unknown section '{section}'.")
    for section in ("education", "experience", "projects"):
        for entry in content.get(section, []):
            if not entry.get("title"):
                problems.append(f"An entry in '{section}' has no title.")
    return problems


def build_cv(content: dict, output_path) -> Path:
    problems = validate_content(content)
    if problems:
        raise ValueError("CV content is not valid: " + " ".join(problems))

    document = Document()
    section = document.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.left_margin = section.right_margin = Inches(0.6)
    section.top_margin = section.bottom_margin = Inches(0.5)

    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = BODY_SIZE
    normal.font.color.rgb = BODY

    # Name
    paragraph = document.add_paragraph()
    _format(paragraph, after=4, line=1.0, align=WD_ALIGN_PARAGRAPH.CENTER)
    _run(paragraph, content["name"].upper(), size=NAME_SIZE, bold=True, color=BLUE)

    # Contact line
    contact = content["contact"]
    paragraph = document.add_paragraph()
    _format(paragraph, after=0, align=WD_ALIGN_PARAGRAPH.CENTER)
    plain = [contact.get(key) for key in ("phone", "email", "location") if contact.get(key)]
    _run(paragraph, "  |  ".join(plain), size=CONTACT_SIZE, color=GREY)
    for link in contact.get("links", []):
        _run(paragraph, "  |  ", size=CONTACT_SIZE, color=GREY)
        _hyperlink(paragraph, link["text"], link["url"])

    # Sections
    for index, key in enumerate(content.get("section_order", DEFAULT_ORDER)):
        data = content.get(key)
        if not data:
            continue
        _heading(document, SECTION_TITLES[key], first=index == 0)
        if key == "summary":
            paragraph = document.add_paragraph()
            _format(paragraph)
            _run(paragraph, data)
        elif key in ("education", "experience", "projects"):
            _entries(document, data)
        else:  # skills, certifications
            _bullets(document, data)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.core_properties.author = content["name"]
    document.core_properties.title = f"{content['name']} - CV"
    document.save(output_path)
    return output_path


def load_content(path=DEFAULT_CONTENT) -> dict:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


if __name__ == "__main__":
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CONTENT
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else PROJECT_ROOT / "CV" / "Template_Check.docx"
    if target.resolve() == source.resolve() or target.suffix.casefold() != ".docx":
        raise SystemExit("Output must be a new .docx file.")
    print("Saved:", build_cv(load_content(source), target))
