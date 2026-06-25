"""
Quick verification / inspection tool for the local dev database.
Prints a readable summary of every loaded report so you can sanity-check
the data without writing SQL by hand.

Usage:
    python3 inspect_db.py                  # inspect dev.db
    python3 inspect_db.py --db mytest.db
    python3 inspect_db.py --report-id 3037229508   # only show one report
"""

import argparse
import sqlite3


def print_report(conn: sqlite3.Connection, report_id: str):
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT * FROM report_metadata WHERE report_id = ?", (report_id,))
    meta = cur.fetchone()
    if not meta:
        print(f"No report found with report_id={report_id}")
        return

    print("=" * 70)
    print(f"{meta['student_name']}  (ID: {meta['student_id']})")
    print(f"Report date: {meta['report_date']}  |  College: {meta['college']}")
    print(f"Full-time minimum units: {meta['min_units_full_time']}")
    print("=" * 70)

    cur.execute("SELECT plan_name, plan_type FROM academic_plans WHERE report_id = ?", (report_id,))
    print("\nAcademic plans:")
    for row in cur.fetchall():
        print(f"  - [{row['plan_type']}] {row['plan_name']}")

    for category in ["university", "college", "major", "minor"]:
        cur.execute(
            "SELECT section_id, section_name, overall_status FROM sections WHERE report_id = ? AND category = ? ORDER BY sort_order",
            (report_id, category),
        )
        sections = cur.fetchall()
        if not sections:
            continue
        print(f"\n--- {category.upper()} ---")
        for sec in sections:
            print(f"  {sec['section_name']}" + (f"  [{sec['overall_status']}]" if sec["overall_status"] else ""))
            cur.execute(
                """SELECT requirement_id, requirement_name, requirement_code, status, depth,
                          units_required, units_used, courses_required, courses_used,
                          gpa_required, gpa_completed
                   FROM requirements WHERE section_id = ? ORDER BY sort_order""",
                (sec["section_id"],),
            )
            for req in cur.fetchall():
                indent = "    " + "  " * req["depth"]
                detail = ""
                if req["units_required"] is not None:
                    detail = f" (units: {req['units_used']}/{req['units_required']})"
                elif req["courses_required"] is not None:
                    detail = f" (courses: {req['courses_used']}/{req['courses_required']})"
                elif req["gpa_required"] is not None:
                    detail = f" (gpa: {req['gpa_completed']}/{req['gpa_required']})"
                status = f" [{req['status']}]" if req["status"] else ""
                print(f"{indent}{req['requirement_name']}{status}{detail}")

    cur.execute("SELECT COUNT(*) as n FROM course_history WHERE report_id = ?", (report_id,))
    print(f"\nCourse history: {cur.fetchone()['n']} rows")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="dev.db")
    ap.add_argument("--report-id", default=None, help="Only show this report_id (default: show all)")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if args.report_id:
        report_ids = [args.report_id]
    else:
        cur.execute("SELECT report_id FROM report_metadata ORDER BY report_id")
        report_ids = [row["report_id"] for row in cur.fetchall()]

    if not report_ids:
        print("No reports loaded in this database yet. Run setup_dev_db.py first.")
        return

    for rid in report_ids:
        print_report(conn, rid)
        print()


if __name__ == "__main__":
    main()
