"""
State machine that walks the APR document line-by-line (plus per-page tables)
and emits a structured tree: sections -> requirements (nested) -> course rows.

This module does NOT touch the database; it produces plain Python objects
that load_db.py then inserts. Keeping parse and load separate makes it much
easier to inspect/debug the parse step on its own.
"""

from __future__ import annotations

import re
import pdfplumber
from dataclasses import dataclass, field

from grammar import (
    RE_SECTION_HEADER, RE_REQ_HEADER, RE_STATUS, RE_COURSES_LINE,
    RE_UNITS_LINE, RE_GPA_LINE, RE_PAGE_MARKER, RE_ID_LINE, RE_FIELD_LINE,
    RE_REQ_DESIGNATION, classify_section, parse_course_table_rows,
    parse_available_courses,
)


@dataclass
class ReqNode:
    name: str
    code: str | None = None
    status: str | None = None
    units_required: float | None = None
    units_used: float | None = None
    units_needed: float | None = None
    courses_required: float | None = None
    courses_used: float | None = None
    courses_needed: float | None = None
    gpa_required: float | None = None
    gpa_completed: float | None = None
    notes: list[str] = field(default_factory=list)
    courses: list = field(default_factory=list)          # ParsedCourseRow
    courses_available: list = field(default_factory=list)  # (subject, catalog_nbr)
    children: list["ReqNode"] = field(default_factory=list)
    depth: int = 0


@dataclass
class SectionNode:
    name: str
    code: str | None
    category: str
    page_number: int
    overall_status: str | None = None
    requirements: list[ReqNode] = field(default_factory=list)


@dataclass
class ParsedReport:
    student_id: str = ""
    student_name: str = ""
    report_date: str = ""
    academic_career: str = ""
    academic_program: str = ""
    requirement_term: str = ""
    academic_plans: list[str] = field(default_factory=list)
    sections: list[SectionNode] = field(default_factory=list)
    course_history: list = field(default_factory=list)  # (term, subj, catnbr, title, grade, units, type, designation)


def _is_blank_or_link_noise(line: str) -> bool:
    # The "this link opens in a new tab" boilerplate clutters prose paragraphs.
    # We keep it in notes (don't want to silently drop real content) but this
    # helper is here in case we want to filter later.
    return not line.strip()


def _extract_positioned_lines(page) -> list[tuple[float, str, bool]]:
    """Reconstruct text lines from words, keeping each line's y-position (top)
    and whether it is underlined with a FULL-WIDTH underline (a structural
    heading marker in this document), as opposed to a partial-width underline
    (a hyperlink within a sentence, e.g. "this link opens in a new tab").

    This document underlines every heading -- section headers, coded
    requirement headers, AND bare sub-requirement names with no code at all
    (e.g. "Entry Level Writing", "American History & Institutions") -- with a
    line whose x-span matches the text's x-span almost exactly (coverage ~1.0).
    Hyperlinks are also underlined, but only under the linked phrase, which is
    a small fraction of the full line's width (coverage well under 0.3 in
    every case observed). This gives a much more reliable heading signal than
    inferring from text shape/length alone, which is what we originally did
    and is the main known weak point of this parser.
    """
    words = page.extract_words()
    lines: dict[float, list] = {}
    for w in words:
        key = round(w["top"], 1)
        # merge keys within ~1pt of an existing key (word-wrap rounding noise)
        matched_key = None
        for k in lines:
            if abs(k - key) <= 1.0:
                matched_key = k
                break
        lines.setdefault(matched_key if matched_key is not None else key, []).append(w)

    # horizontal vector lines (height ~0) are candidate underlines
    underlines = [l for l in page.lines if l.get("height", 1) == 0.0]

    out = []
    for top in sorted(lines.keys()):
        ws = sorted(lines[top], key=lambda w: w["x0"])
        text = " ".join(w["text"] for w in ws)
        x0, x1 = ws[0]["x0"], ws[-1]["x1"]
        full_span = x1 - x0

        is_underlined = False
        if full_span > 0:
            for ul in underlines:
                ul_top = ul["top"]
                if 0 < (ul_top - top) < 12:  # underline sits just below this text's baseline
                    ul_span = ul["x1"] - ul["x0"]
                    coverage = ul_span / full_span
                    if coverage >= 0.85:
                        is_underlined = True
                        break

        out.append((top, text, is_underlined))
    return out


def parse_pdf(path: str) -> ParsedReport:
    report = ParsedReport()

    with pdfplumber.open(path) as pdf:
        all_lines: list[tuple[int, float, str, bool]] = []   # (page_number, top_y, line, is_underlined)
        page_tables: dict[int, list] = {}        # page_number -> list of (top_y, table_rows)
        page_heights: dict[int, float] = {}

        for i, page in enumerate(pdf.pages):
            page_num = i + 1
            page_heights[page_num] = page.height
            for top, text, is_underlined in _extract_positioned_lines(page):
                all_lines.append((page_num, top, text, is_underlined))

            tables_with_pos = []
            for t in page.find_tables():
                rows = t.extract()
                tables_with_pos.append((t.bbox[1], rows, t.bbox[3]))  # (top_y, rows, bottom_y)
            tables_with_pos.sort(key=lambda x: x[0])
            page_tables[page_num] = tables_with_pos

        _parse_lines(all_lines, page_tables, page_heights, report)

    return report


def _parse_lines(all_lines, page_tables, page_heights, report: ParsedReport):
    current_section: SectionNode | None = None
    # stack of (ReqNode, depth) representing currently open requirement nesting
    req_stack: list[ReqNode] = []
    in_course_history = False

    i = 0
    n = len(all_lines)
    while i < n:
        page_num, top_y, raw_line, is_underlined = all_lines[i]
        line = raw_line.strip()
        i += 1

        if not line:
            continue

        m = RE_PAGE_MARKER.match(line)
        if m:
            continue

        m = RE_ID_LINE.match(line)
        if m:
            report.student_id, report.student_name, report.report_date = m.groups()
            continue

        m = RE_FIELD_LINE.match(line)
        if m:
            field_name, value = m.groups()
            if field_name == "AcademicCareer":
                report.academic_career = value
            elif field_name == "AcademicProgram":
                report.academic_program = value
            elif field_name == "AcademicPlan":
                report.academic_plans.append(value)
            elif field_name == "RequirementTerm":
                report.requirement_term = value
            continue

        # --- Course History section marker (flat list at the end) ---
        if line == "Course History":
            in_course_history = True
            current_section = None
            req_stack = []
            continue

        if in_course_history:
            m = RE_REQ_DESIGNATION.match(line)
            if m:
                # Applies to the most recently appended course_history row
                if report.course_history:
                    last = report.course_history[-1]
                    report.course_history[-1] = last[:-1] + (m.group(1),)
                continue
            # try to parse as a course history row: "TERM SUBJECT CATNBR TITLE [GRADE] UNITS TYPE"
            row = _try_parse_history_line(line)
            if row:
                report.course_history.append(row)
                continue
            # Could be a continuation/topic line (e.g. "Course Topic: 55 - ...");
            # skip rather than guess.
            continue

        # --- "Courses Used" / "Courses Available" markers ---
        if line == "Courses Used":
            target = req_stack[-1] if req_stack else None
            if target is not None:
                table = _pull_table_near(page_num, top_y, page_tables, page_heights)
                if table:
                    rows = parse_course_table_rows(table)
                    target.courses.extend(rows)
                    # The same course rows also exist as plain text lines right
                    # after this "Courses Used" label (find_tables() and the
                    # word-based text extraction both see the same PDF content,
                    # just structured differently). If we don't skip those text
                    # lines here, they fall through to the generic "free text ->
                    # note" handler below and contaminate requirements.notes
                    # with a re-serialized course row (observed bug, see README).
                    i = _skip_duplicate_course_row_lines(all_lines, i, n, len(rows))
            continue

        if line == "Courses Available":
            target = req_stack[-1] if req_stack else None
            if target is not None:
                # gather subsequent lines until we hit a line that looks like a new
                # heading (now detected via the underline flag, not text shape) or
                # another structural marker. Page-footer lines ("Page N of 12") can
                # land in the middle of this block when the course list spans a
                # page break, and must be skipped here too -- not just at the
                # top-level loop -- or the footer text gets glued onto the next
                # subject name (observed bug: "Page 11 of 12 NWMEDIA" as a subject).
                block_lines = []
                while i < n:
                    _, _, peek, peek_underlined = all_lines[i]
                    peek_stripped = peek.strip()
                    if not peek_stripped:
                        i += 1
                        continue
                    if RE_PAGE_MARKER.match(peek_stripped):
                        i += 1
                        continue
                    if peek_underlined or _looks_like_structural_line(peek_stripped):
                        break
                    block_lines.append(peek_stripped)
                    i += 1
                pairs = parse_available_courses("\n".join(block_lines))
                target.courses_available.extend(pairs)
            continue

        # --- numeric summary lines apply to the innermost open requirement ---
        m = RE_COURSES_LINE.match(line)
        if m and req_stack:
            req_stack[-1].courses_required = float(m.group(1))
            req_stack[-1].courses_used = float(m.group(2))
            req_stack[-1].courses_needed = float(m.group(3)) if m.group(3) else None
            continue

        m = RE_UNITS_LINE.match(line)
        if m and req_stack:
            req_stack[-1].units_required = float(m.group(1))
            req_stack[-1].units_used = float(m.group(2))
            req_stack[-1].units_needed = float(m.group(3)) if m.group(3) else None
            continue

        m = RE_GPA_LINE.match(line)
        if m and req_stack:
            req_stack[-1].gpa_required = float(m.group(1))
            req_stack[-1].gpa_completed = float(m.group(2))
            continue

        # --- top-level section header: "NAME (RG-####)" ---
        m = RE_SECTION_HEADER.match(line)
        if m and is_underlined:
            name = m.group("name").strip()
            code = "RG-" + m.group("code")
            category = classify_section(name, report.academic_plans)
            current_section = SectionNode(name=name, code=code, category=category, page_number=page_num)
            report.sections.append(current_section)
            req_stack = []
            # A section header is sometimes immediately followed by its own
            # overall status line, with no requirement name in between
            # (e.g. "DATA SCIENCE Minor (RG-1215)" -> "Overall Requirement Not Satisfied").
            if i < n:
                _, _, peek_raw, _ = all_lines[i]
                peek = peek_raw.strip()
                if RE_STATUS.match(peek):
                    current_section.overall_status = peek
                    i += 1
            continue

        # --- requirement header with code: "Name (R-####)" ---
        m = RE_REQ_HEADER.match(line)
        if m and is_underlined and current_section is not None:
            name = m.group("name").strip()
            code = "R-" + m.group("code")
            depth = _infer_depth_for_coded(req_stack)
            node = ReqNode(name=name, code=code, depth=depth)
            # A coded requirement header is very often immediately followed by its
            # own status line (e.g. "Upper Division Requirements (R-0211)" ->
            # "Overall Requirement Not Satisfied"), unlike a bare sub-requirement
            # name, which is *itself* the line right before the status.
            if i < n:
                _, _, peek_raw, _ = all_lines[i]
                peek = peek_raw.strip()
                if RE_STATUS.match(peek):
                    node.status = peek
                    i += 1
            _attach_node(node, req_stack, current_section)
            continue

        # --- bare requirement/sub-requirement name (no code), detected via
        # full-width underline rather than guessing from text shape/length.
        # This is the key fix: the document underlines headings ("Entry Level
        # Writing", "American History & Institutions", "Core: Concepts of
        # Probability") the same way it underlines "STATISTICS BA (RG-0151)" --
        # full-line-width underline -- while ordinary prose and hyperlinked
        # phrases within a sentence are never underlined at full width. ---
        if is_underlined and current_section is not None and not RE_STATUS.match(line):
            status_line = None
            depth = _infer_depth(line, req_stack)
            if i < n:
                _, _, next_raw, _ = all_lines[i]
                next_line = next_raw.strip()
                if RE_STATUS.match(next_line):
                    status_line = next_line
                    i += 1
            node = ReqNode(name=line, status=status_line, depth=depth)
            _attach_node(node, req_stack, current_section)
            continue

        # --- otherwise: free text -> attach as a note to innermost open requirement ---
        if req_stack:
            req_stack[-1].notes.append(line)
        # else: prose belonging to section-level intro text; discarded (not modeled)
        continue


def _infer_depth_for_coded(req_stack: list[ReqNode]) -> int:
    """Coded requirement headers ('Name (R-####)') are, in every case observed in
    this document, top-level requirements directly under their section -- never
    nested inside another requirement. So they always reset depth to 0.
    """
    return 0


def _attach_node(node: ReqNode, req_stack: list[ReqNode], current_section: SectionNode):
    """Attach a newly found requirement node at the correct depth in the stack."""
    if node.depth == 0 or not req_stack:
        current_section.requirements.append(node)
        req_stack.clear()
        req_stack.append(node)
    else:
        # pop stack back to the parent depth, then nest under it
        while req_stack and req_stack[-1].depth >= node.depth:
            req_stack.pop()
        if req_stack:
            req_stack[-1].children.append(node)
        else:
            current_section.requirements.append(node)
        req_stack.append(node)


def _infer_depth(name_line: str, req_stack: list[ReqNode]) -> int:
    """Bare-name sub-requirements (no (R-####) code) are, in every case observed
    in this document, siblings of one another directly under their nearest coded
    ancestor -- never nested under another bare sub-requirement. So depth is the
    nearest *coded* ancestor's depth + 1, not simply "one deeper than whatever is
    on top of the stack."
    """
    for node in reversed(req_stack):
        if node.code is not None:
            return node.depth + 1
    return 1 if req_stack else 0


def _looks_like_structural_line(line: str) -> bool:
    """Used to know when to stop consuming a 'Courses Available' free-text block.
    Stops on: a status line itself, a coded section/requirement header, the
    'Courses Used'/'Courses Available'/'Course History' markers, or a Courses:/
    Units:/GPA: summary line. The caller additionally stops on any underlined
    line (the real heading signal -- see _extract_positioned_lines), which
    covers bare sub-requirement names like "Electives" that have no code and
    no distinctive regex shape of their own.
    """
    if not line:
        return False
    if RE_STATUS.match(line):
        return True
    if RE_SECTION_HEADER.match(line):
        return True
    if RE_REQ_HEADER.match(line):
        return True
    if line in ("Courses Used", "Courses Available", "Course History"):
        return True
    if RE_COURSES_LINE.match(line) or RE_UNITS_LINE.match(line) or RE_GPA_LINE.match(line):
        return True
    return False


_COURSE_ROW_TERM_RE = re.compile(r"^\d{4}\s+(Sum|Fall|Spr|Win)\b")
_COURSE_TABLE_HEADER_TEXT = "Term Subject Catalog Nbr Course Title Grade Units Type"
_PAREN_CONTINUATION_RE = re.compile(r"^\(.*\)$")


def _skip_duplicate_course_row_lines(all_lines, i: int, n: int, expected_count: int) -> int:
    """After consuming a "Courses Used" table via _pull_table_near, advance
    past the plain-text lines that represent the SAME course rows (the word-
    based text extraction sees this content too, in addition to find_tables()
    -- see the comment at the call site). We recognize a course-row line by
    its leading "YYYY Term" pattern (e.g. "2023 Fall ..."), and also skip a
    bare table-header text line and any page-marker line that appear in
    between, since both can land in the middle of a multi-row table on a
    page break without being a "real" new line of content.

    A course title that wraps onto a second physical line (e.g. "PE
    ACTIVITIES" / "(Yoga-Restorative 1)") produces its own standalone text
    line with no leading term -- we tolerate exactly that shape (parenthesized
    continuation text) immediately after a matched course-row line, without
    counting it toward expected_count, so it doesn't prematurely end the skip.

    expected_count caps how many course-row-shaped lines we skip, so a
    genuinely new course-row-shaped sentence belonging to whatever comes
    next is never accidentally consumed.
    """
    skipped = 0
    while i < n and skipped < expected_count:
        _, _, peek, peek_underlined = all_lines[i]
        peek_stripped = peek.strip()
        if not peek_stripped:
            i += 1
            continue
        if peek_underlined:
            break
        if peek_stripped == _COURSE_TABLE_HEADER_TEXT or RE_PAGE_MARKER.match(peek_stripped):
            i += 1
            continue
        if _COURSE_ROW_TERM_RE.match(peek_stripped):
            i += 1
            skipped += 1
            continue
        if _PAREN_CONTINUATION_RE.match(peek_stripped):
            i += 1
            continue
        break
    return i


def _pull_table_near(page_num: int, after_top_y: float, page_tables: dict, page_heights: dict):
    """Find the nearest not-yet-consumed table on this page whose top y-coordinate
    is greater than (i.e. visually below) the 'Courses Used' label's y-position,
    and mark it consumed. This replaces a blind per-page index cursor, which broke
    when several small one-row tables sit close together (each 'Core: X' sub-
    requirement has its own tiny table) or when a table from the previous
    section's content continues across a page break and would otherwise be
    mistakenly re-claimed by an unrelated requirement on the new page.

    Cross-page split handling: when a "Courses Used" table is cut off by a page
    break, pdfplumber's per-page extraction can produce a small table at the
    bottom of the current page (header + whatever rows fit) and a second table
    at the very top of the next page holding the remaining rows. Two distinct
    patterns are possible, and we've observed both in this single document:
      (a) TRUE CONTINUATION: the next-page table's first data row picks up
          right where the page-bottom table left off, with NO overlap (e.g.
          "18 UC Berkeley Upper Division Residence Units": page 6 ends with
          just ENGLISH 198, page 7 starts with STAT 133 and continues on).
          Here we CONCATENATE both tables' rows.
      (b) DUPLICATE RE-RENDER: the next-page table independently belongs to a
          *different*, unrelated, already-complete table that merely happens
          to start at the top of the next page (e.g. a short one-row table
          like "Core: Concepts of Probability"'s own table). We distinguish
          (a) from (b) by checking row overlap: if the rows are identical
          (same content), it's NOT a continuation -- it's a separate table
          that coincidentally starts at the page top, and we must not merge it.
    """
    tables = page_tables.get(page_num, [])
    page_height = page_heights.get(page_num)
    best_idx = None
    best_top = None
    for idx in range(len(tables)):
        entry = tables[idx]
        top, rows = entry[0], entry[1]
        is_consumed = len(entry) >= 4 and entry[3]
        if is_consumed:
            continue
        if top >= after_top_y - 2:  # small tolerance for same-line rounding
            if best_top is None or top < best_top:
                best_top = top
                best_idx = idx

    if best_idx is not None:
        top, rows, bottom = tables[best_idx][0], tables[best_idx][1], tables[best_idx][2]
        # Truncation signal: does THIS table's BOTTOM edge sit near the bottom
        # of the page? (Not whether it STARTS near the bottom -- a table can
        # start halfway down the page and still run off the bottom edge if it
        # has enough rows, as happened with "Minimum Total Units"' 20-row
        # table starting at 64% down the page but ending at 90%.)
        near_bottom = page_height is not None and bottom > page_height * 0.85
        if near_bottom:
            next_tables = page_tables.get(page_num + 1, [])
            for n_idx in range(len(next_tables)):
                n_entry = next_tables[n_idx]
                n_top, n_rows = n_entry[0], n_entry[1]
                n_is_consumed = len(n_entry) >= 4 and n_entry[3]
                if n_is_consumed or n_top > 100:
                    continue
                # rows[0] is the header in both tables; compare data rows only
                this_data = rows[1:]
                next_data = n_rows[1:]
                if not this_data or not next_data:
                    continue
                if this_data[-1] == next_data[0]:
                    # overlap: next-page table re-includes our last row as its
                    # first row -- skip that duplicate row, append the rest.
                    tables[best_idx] = (top, rows, bottom, True)
                    next_tables[n_idx] = (n_top, n_rows, n_entry[2], True)
                    return rows[:1] + this_data + next_data[1:]
                else:
                    # no overlap: true continuation, concatenate directly.
                    tables[best_idx] = (top, rows, bottom, True)
                    next_tables[n_idx] = (n_top, n_rows, n_entry[2], True)
                    return rows[:1] + this_data + next_data
        tables[best_idx] = (top, rows, bottom, True)  # mark consumed
        return rows

    # Fallback: table starts at the top of the next page (label was last thing
    # on this page, table didn't fit before the page break at all).
    next_tables = page_tables.get(page_num + 1, [])
    for idx in range(len(next_tables)):
        entry = next_tables[idx]
        top, rows, bottom = entry[0], entry[1], entry[2]
        is_consumed = len(entry) >= 4 and entry[3]
        if is_consumed:
            continue
        next_tables[idx] = (top, rows, bottom, True)
        return rows

    return None


_HISTORY_LINE_RE = re.compile(
    r"^(?P<term>\d{4} (?:Sum|Fall|Spr|Win))\s+"
    r"(?P<subject>[A-Z]+)\s+"
    r"(?P<catalog_nbr>[A-Z0-9]+)\s+"
    r"(?P<rest>.+)$"
)


def _try_parse_history_line(line: str):
    m = _HISTORY_LINE_RE.match(line)
    if not m:
        return None
    term, subject, catalog_nbr, rest = m.group("term"), m.group("subject"), m.group("catalog_nbr"), m.group("rest")
    # rest = "TITLE WORDS [GRADE] UNITS TYPE"
    tokens = rest.split()
    if len(tokens) < 2:
        return None
    course_type = tokens[-1]
    units_str = tokens[-2]
    try:
        units = float(units_str)
    except ValueError:
        return None
    remaining = tokens[:-2]
    grade = None
    # grade is alphanumeric-ish (e.g. B+, TA, CR, P, F, W) and immediately precedes units;
    # title is the rest. We peel off the last remaining token as grade IF it doesn't look
    # like an obvious title word (heuristic: short, mostly upper, <=3 chars or has +/-).
    if remaining and (len(remaining[-1]) <= 3 or "+" in remaining[-1] or "-" in remaining[-1]):
        grade = remaining[-1]
        title = " ".join(remaining[:-1])
    else:
        title = " ".join(remaining)
        grade = ""
    return (term, subject, catalog_nbr, title.strip(), grade, units, course_type, None)
