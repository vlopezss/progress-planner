"""
APR (Academic Progress Report) PDF parser.

Strategy:
  - Use pdfplumber per-page extract_text() for the narrative skeleton
    (headers, requirement names, status lines, Courses:/Units:/GPA: lines).
  - Use pdfplumber per-page extract_tables() for "Courses Used" tables,
    since bordered-table extraction correctly handles multi-line course
    titles that a plain text line-splitter would break.
  - Walk the merged line stream as a state machine, anchoring on:
      * ALL-CAPS section headers ending in (RG-####)
      * "Title (R-####)" requirement headers
      * bare Title-Case lines immediately followed by a status line
        (Satisfied / Not Satisfied / Overall Requirement Not Satisfied)
      * Courses:/Units:/GPA: numeric lines
      * "Courses Used" / "Courses Available" markers

Known limitations (documented, not silently swallowed):
  - Mangled glyphs in the source PDF show up as "?" (e.g. "?Passed Grade
    Limit" should be a fraction symbol). We pass these through as-is.
  - Course tables that span a page break are extracted per-page; rows are
    still attributed to the correct requirement via stateful tracking, but
    a table that is *itself* split across pages will appear as two
    separate extracted tables that we concatenate before insertion.
"""

from __future__ import annotations

import re
import sqlite3
import pdfplumber
from dataclasses import dataclass, field


# ---------- regexes for line classification ----------

RE_SECTION_HEADER = re.compile(r"^(?P<name>[A-Za-z0-9 &,:\-/']+)\s+\(RG-(?P<code>\d+)\)$")
RE_REQ_HEADER = re.compile(r"^(?P<name>.+?)\s+\(R-(?P<code>\d+)\)$")
RE_STATUS = re.compile(r"^(Satisfied|Not Satisfied|Overall Requirement Not Satisfied)$")
RE_COURSES_LINE = re.compile(
    r"^Courses:\s*([\d.]+)\s*required,\s*([\d.]+)\s*used(?:,\s*([\d.]+)\s*needed)?$"
)
RE_UNITS_LINE = re.compile(
    r"^Units:\s*([\d.]+)\s*required,\s*([\d.]+)\s*used(?:,\s*([\d.]+)\s*needed)?$"
)
RE_GPA_LINE = re.compile(r"^GPA:\s*([\d.]+)\s*required,\s*([\d.]+)\s*completed$")
RE_PAGE_MARKER = re.compile(r"^Page (\d+) of (\d+)$")
RE_ID_LINE = re.compile(r"^ID:\s*(\S+)\s+(.+?)\s+Report Date:\s*(\S+)$")
RE_FIELD_LINE = re.compile(r"^(AcademicCareer|AcademicProgram|AcademicPlan|RequirementTerm):\s*(.+)$")
RE_REQ_DESIGNATION = re.compile(r"^Requirement Designation:\s*(.+)$")

# section header category inference, by keyword in the header name
SECTION_CATEGORY_RULES = [
    ("IMPORTANT INFORMATION", "info"),
    ("IN-PROGRESS COURSEWORK", "in_progress"),
    ("UNIVERSITY OF CALIFORNIA AND BERKELEY CAMPUS", "university"),
    ("COLLEGE OF", "college"),
    ("ADDITIONAL COURSEWORK", "additional"),
]


def classify_section(name: str, known_plans: list[str]) -> str:
    upper = name.upper()
    for keyword, category in SECTION_CATEGORY_RULES:
        if keyword in upper:
            return category
    # major/minor sections are named after the plan itself, e.g. "STATISTICS BA",
    # "DATA SCIENCE Minor" -- match against the academic plans declared on page 1
    if "MINOR" in upper:
        return "minor"
    # default: if it matches a major plan name fragment, call it major
    for plan in known_plans:
        plan_upper = plan.upper()
        if any(word in upper for word in plan_upper.split() if len(word) > 3):
            return "major"
    return "major"  # conservative default for unmatched plan-like headers


@dataclass
class ParsedCourseRow:
    term: str
    subject: str
    catalog_nbr: str
    course_title: str
    grade: str
    units: float | None
    course_type: str


def parse_course_table_rows(table_rows: list[list[str]]) -> list[ParsedCourseRow]:
    """Convert a pdfplumber-extracted table (with header row) into course rows."""
    out = []
    if not table_rows:
        return out
    header = [c.strip() if c else "" for c in table_rows[0]]
    expected = ["Term", "Subject", "Catalog Nbr", "Course Title", "Grade", "Units", "Type"]
    if header[:len(expected)] != expected:
        return out  # not a "Courses Used" table; skip
    for row in table_rows[1:]:
        if not row or len(row) < 7:
            continue
        term, subject, catalog_nbr, title, grade, units, ctype = row[:7]
        title = (title or "").replace("\n", " ").strip()
        units_val = None
        try:
            units_val = float(units) if units and units.strip() else None
        except ValueError:
            units_val = None
        out.append(ParsedCourseRow(
            term=(term or "").strip(),
            subject=(subject or "").strip(),
            catalog_nbr=(catalog_nbr or "").strip(),
            course_title=title,
            grade=(grade or "").strip(),
            units=units_val,
            course_type=(ctype or "").strip(),
        ))
    return out


def parse_available_courses(text_block: str) -> list[tuple[str, str]]:
    """Parse a comma-separated 'Courses Available' free-text block into (subject, catalog_nbr) pairs.

    Format observed: "AFRICAM C134, AFRICAM 134, ... DATA\nC104, ..." -- entries are
    "SUBJECT CATALOGNBR" pairs separated by commas; line wraps occur mid-list and even
    mid-entry (subject on one line, catalog number on next), so we join the whole block
    with spaces first, then split on commas.
    """
    joined = " ".join(text_block.split("\n"))
    joined = re.sub(r"\s+", " ", joined).strip()
    entries = [e.strip() for e in joined.split(",") if e.strip()]
    pairs = []
    for entry in entries:
        parts = entry.rsplit(" ", 1)
        if len(parts) == 2:
            pairs.append((parts[0].strip(), parts[1].strip()))
        else:
            pairs.append((entry.strip(), ""))
    return pairs
