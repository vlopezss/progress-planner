"""
Export the dev database into a single JSON file shaped for the HTML viewer.
Run this any time dev.db changes (after setup_dev_db.py) and before opening
the viewer, so the viewer reflects the current database contents.

Usage:
    python3 export_for_viewer.py                 # dev.db -> viewer_data.json
    python3 export_for_viewer.py --db mytest.db --out custom.json
"""

import argparse
import json
import sqlite3


def build_requirement_tree(cur, section_id):
    """Fetch all requirements for a section and assemble them into a nested
    tree via parent_requirement_id, preserving document order (sort_order).
    """
    cur.execute(
        """SELECT requirement_id, parent_requirement_id, requirement_code,
                  requirement_name, status, units_required, units_used, units_needed,
                  courses_required, courses_used, courses_needed,
                  gpa_required, gpa_completed, notes, depth, sort_order
           FROM requirements WHERE section_id = ? ORDER BY sort_order""",
        (section_id,),
    )
    rows = cur.fetchall()

    nodes = {}
    children_of = {}
    roots = []

    for row in rows:
        node = {
            "requirement_id": row["requirement_id"],
            "code": row["requirement_code"],
            "name": row["requirement_name"],
            "status": row["status"],
            "units_required": row["units_required"],
            "units_used": row["units_used"],
            "units_needed": row["units_needed"],
            "courses_required": row["courses_required"],
            "courses_used": row["courses_used"],
            "courses_needed": row["courses_needed"],
            "gpa_required": row["gpa_required"],
            "gpa_completed": row["gpa_completed"],
            "notes": row["notes"],
            "courses": [],
            "courses_available": [],
            "children": [],
        }
        nodes[row["requirement_id"]] = node
        parent_id = row["parent_requirement_id"]
        if parent_id is None:
            roots.append(node)
        else:
            children_of.setdefault(parent_id, []).append(node)

    for parent_id, kids in children_of.items():
        if parent_id in nodes:
            nodes[parent_id]["children"] = kids

    # attach courses and courses_available to each node
    for req_id, node in nodes.items():
        cur.execute(
            """SELECT term, subject, catalog_nbr, course_title, grade, units, course_type
               FROM requirement_courses WHERE requirement_id = ? ORDER BY id""",
            (req_id,),
        )
        node["courses"] = [dict(r) for r in cur.fetchall()]

        cur.execute(
            """SELECT subject, catalog_nbr FROM requirement_courses_available
               WHERE requirement_id = ? ORDER BY id""",
            (req_id,),
        )
        node["courses_available"] = [dict(r) for r in cur.fetchall()]

    return roots


def export_report(cur, report_id):
    cur.execute("SELECT * FROM report_metadata WHERE report_id = ?", (report_id,))
    meta_row = cur.fetchone()
    meta = dict(meta_row) if meta_row else {}

    cur.execute(
        "SELECT plan_name, plan_type FROM academic_plans WHERE report_id = ?",
        (report_id,),
    )
    plans = [dict(r) for r in cur.fetchall()]

    categories = {}
    for category in ["university", "college", "major", "minor", "in_progress", "additional"]:
        cur.execute(
            """SELECT section_id, section_code, section_name, overall_status, sort_order
               FROM sections WHERE report_id = ? AND category = ? ORDER BY sort_order""",
            (report_id, category),
        )
        sections = []
        for sec in cur.fetchall():
            sections.append({
                "section_code": sec["section_code"],
                "section_name": sec["section_name"],
                "overall_status": sec["overall_status"],
                "requirements": build_requirement_tree(cur, sec["section_id"]),
            })
        if sections:
            categories[category] = sections

    cur.execute(
        """SELECT term, subject, catalog_nbr, title, grade, units, course_type, requirement_designation
           FROM course_history WHERE report_id = ? ORDER BY id""",
        (report_id,),
    )
    course_history = [dict(r) for r in cur.fetchall()]

    return {
        "metadata": meta,
        "academic_plans": plans,
        "categories": categories,
        "course_history": course_history,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="dev.db")
    ap.add_argument("--out", default="viewer_data.json")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT report_id FROM report_metadata ORDER BY report_id")
    report_ids = [r["report_id"] for r in cur.fetchall()]

    if not report_ids:
        print(f"No reports found in {args.db}. Run setup_dev_db.py first.")
        return

    reports = [export_report(cur, rid) for rid in report_ids]

    with open(args.out, "w") as f:
        json.dump({"reports": reports}, f, indent=2)

    print(f"Exported {len(reports)} report(s) to {args.out}")
    conn.close()


if __name__ == "__main__":
    main()
