"""
Load a ParsedReport (produced by parser.parse_pdf) into the SQLite schema
defined in schema.sql. Kept separate from parsing so the parse step can be
inspected/debugged on its own before anything touches the database.
"""

from __future__ import annotations

import sqlite3
from parser import ParsedReport, SectionNode, ReqNode


def init_db(db_path: str, schema_path: str = "schema.sql") -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    with open(schema_path) as f:
        conn.executescript(f.read())
    conn.commit()
    return conn


def _infer_plan_type(plan_name: str) -> str:
    return "minor" if "minor" in plan_name.lower() else "major"


def _infer_college(report: ParsedReport) -> str | None:
    """The college isn't stored as its own clean field in the PDF -- it's
    embedded in a section header like 'COLLEGE OF LETTERS & SCIENCE PROGRAM
    REQUIREMENTS'. We extract it from whichever section was classified as
    category='college'.
    """
    for s in report.sections:
        if s.category == "college":
            name = s.name
            # strip a trailing "PROGRAM REQUIREMENTS" / "REQUIREMENTS" suffix if present
            for suffix in (" PROGRAM REQUIREMENTS", " REQUIREMENTS"):
                if name.upper().endswith(suffix):
                    name = name[: -len(suffix)]
                    break
            # strip a leading "COLLEGE OF " if present
            if name.upper().startswith("COLLEGE OF "):
                name = name[len("COLLEGE OF "):]
            return name.strip()
    return None


def load_report(conn: sqlite3.Connection, report: ParsedReport, college_policy: dict[str, int] | None = None):
    """Insert a single ParsedReport into the database. report.student_id is
    used as the report_id primary key, since each PDF corresponds to exactly
    one student's report as of one report_date.
    """
    cur = conn.cursor()
    report_id = report.student_id

    college = _infer_college(report)
    min_units_full_time = None
    if college_policy:
        normalized_policy = {k.upper(): v for k, v in college_policy.items()}
        if college and college.upper() in normalized_policy:
            min_units_full_time = normalized_policy[college.upper()]

    cur.execute(
        """INSERT OR REPLACE INTO report_metadata
           (report_id, student_name, student_id, report_date, academic_career,
            academic_program, requirement_term, college, min_units_full_time)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (report_id, report.student_name, report.student_id, report.report_date,
         report.academic_career, report.academic_program, report.requirement_term,
         college, min_units_full_time),
    )

    # clear any prior load for this report_id so re-running the loader is idempotent
    cur.execute("DELETE FROM academic_plans WHERE report_id = ?", (report_id,))
    cur.execute(
        """DELETE FROM requirement_courses WHERE requirement_id IN
           (SELECT requirement_id FROM requirements WHERE section_id IN
            (SELECT section_id FROM sections WHERE report_id = ?))""",
        (report_id,),
    )
    cur.execute(
        """DELETE FROM requirement_courses_available WHERE requirement_id IN
           (SELECT requirement_id FROM requirements WHERE section_id IN
            (SELECT section_id FROM sections WHERE report_id = ?))""",
        (report_id,),
    )
    cur.execute(
        """DELETE FROM requirements WHERE section_id IN
           (SELECT section_id FROM sections WHERE report_id = ?)""",
        (report_id,),
    )
    cur.execute("DELETE FROM sections WHERE report_id = ?", (report_id,))
    cur.execute("DELETE FROM course_history WHERE report_id = ?", (report_id,))

    for plan_name in report.academic_plans:
        cur.execute(
            "INSERT INTO academic_plans (report_id, plan_name, plan_type) VALUES (?, ?, ?)",
            (report_id, plan_name, _infer_plan_type(plan_name)),
        )

    for sort_order, section in enumerate(report.sections):
        cur.execute(
            """INSERT INTO sections (report_id, category, section_code, section_name,
               overall_status, page_number, sort_order) VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (report_id, section.category, section.code, section.name,
             section.overall_status, section.page_number, sort_order),
        )
        section_id = cur.lastrowid

        req_sort = 0
        for req in section.requirements:
            req_sort = _insert_requirement_tree(cur, section_id, None, req, req_sort)

    for row in report.course_history:
        term, subject, catalog_nbr, title, grade, units, course_type, designation = row
        cur.execute(
            """INSERT INTO course_history (report_id, term, subject, catalog_nbr, title,
               grade, units, course_type, requirement_designation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (report_id, term, subject, catalog_nbr, title, grade, units, course_type, designation),
        )

    conn.commit()
    return report_id


def _insert_requirement_tree(cur, section_id: int, parent_requirement_id: int | None,
                              node: ReqNode, sort_order: int) -> int:
    """Recursively insert a ReqNode and its children. Returns the next sort_order
    value to use (so siblings across recursive calls stay in document order).
    """
    notes_text = " ".join(node.notes) if node.notes else None
    cur.execute(
        """INSERT INTO requirements
           (section_id, parent_requirement_id, requirement_code, requirement_name,
            status, units_required, units_used, units_needed,
            courses_required, courses_used, courses_needed,
            gpa_required, gpa_completed, notes, depth, sort_order)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (section_id, parent_requirement_id, node.code, node.name, node.status,
         node.units_required, node.units_used, node.units_needed,
         node.courses_required, node.courses_used, node.courses_needed,
         node.gpa_required, node.gpa_completed, notes_text, node.depth, sort_order),
    )
    requirement_id = cur.lastrowid
    sort_order += 1

    for course in node.courses:
        cur.execute(
            """INSERT INTO requirement_courses
               (requirement_id, term, subject, catalog_nbr, course_title, grade, units, course_type)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (requirement_id, course.term, course.subject, course.catalog_nbr,
             course.course_title, course.grade, course.units, course.course_type),
        )

    for subject, catalog_nbr in node.courses_available:
        cur.execute(
            """INSERT INTO requirement_courses_available (requirement_id, subject, catalog_nbr)
               VALUES (?, ?, ?)""",
            (requirement_id, subject, catalog_nbr),
        )

    for child in node.children:
        sort_order = _insert_requirement_tree(cur, section_id, requirement_id, child, sort_order)

    return sort_order


# Hardcoded full-time-unit-minimum policy, by college. NOT derivable from the
# PDF -- see schema.sql's college_full_time_policy table comment. Values
# reflect Berkeley's general undergraduate full-time minimum; some colleges
# / programs may differ and should be confirmed against the Registrar's
# current published policy before being relied on for advising purposes.
DEFAULT_COLLEGE_FULL_TIME_POLICY = {
    "LETTERS & SCIENCE": 12,
    "ENGINEERING": 12,
    "ENVIRONMENTAL DESIGN": 12,
    "CHEMISTRY": 12,
    "NATURAL RESOURCES": 12,
    "HAAS SCHOOL OF BUSINESS": 12,
}


def seed_college_policy(conn: sqlite3.Connection, policy: dict[str, int] = DEFAULT_COLLEGE_FULL_TIME_POLICY):
    cur = conn.cursor()
    for college, min_units in policy.items():
        cur.execute(
            """INSERT OR REPLACE INTO college_full_time_policy (college, min_units_full_time, source_note)
               VALUES (?, ?, ?)""",
            (college, min_units, "Hardcoded; verify against current Berkeley Registrar policy"),
        )
    conn.commit()


if __name__ == "__main__":
    import sys
    from parser import parse_pdf

    pdf_path = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads/progress_report.pdf"
    db_path = sys.argv[2] if len(sys.argv) > 2 else "apr.db"

    report = parse_pdf(pdf_path)
    conn = init_db(db_path)
    seed_college_policy(conn)
    report_id = load_report(conn, report, DEFAULT_COLLEGE_FULL_TIME_POLICY)
    print(f"Loaded report_id={report_id} into {db_path}")
    conn.close()
