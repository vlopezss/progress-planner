"""
Build (or rebuild) the local development database from every PDF in
sample_reports/. This is the single entry point for setting up a database
to develop the website against, without needing any real student data or
a live connection to Berkeley's systems.

Usage:
    python3 setup_dev_db.py                  # rebuild dev.db from sample_reports/
    python3 setup_dev_db.py --db mytest.db   # use a different db filename
    python3 setup_dev_db.py --fresh          # delete the db file first, full rebuild

Each PDF in sample_reports/ is parsed and loaded as its own report, keyed by
the student ID found inside that PDF (NOT the filename). Re-running this
script is always safe -- load_report() deletes and re-inserts each report's
rows before inserting, so running it twice never duplicates data.
"""

import argparse
import os
import sys

from parser import parse_pdf
from load_db import init_db, seed_college_policy, load_report, DEFAULT_COLLEGE_FULL_TIME_POLICY

SAMPLE_REPORTS_DIR = os.path.join(os.path.dirname(__file__), "sample_reports")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="dev.db", help="SQLite file to create/update (default: dev.db)")
    ap.add_argument("--fresh", action="store_true", help="Delete the db file first for a completely clean rebuild")
    ap.add_argument("--reports-dir", default=SAMPLE_REPORTS_DIR, help="Folder of PDFs to load")
    args = ap.parse_args()

    if args.fresh and os.path.exists(args.db):
        os.remove(args.db)
        print(f"Removed existing {args.db}")

    if not os.path.isdir(args.reports_dir):
        print(f"No such directory: {args.reports_dir}", file=sys.stderr)
        sys.exit(1)

    pdf_files = sorted(
        f for f in os.listdir(args.reports_dir) if f.lower().endswith(".pdf")
    )
    if not pdf_files:
        print(f"No PDFs found in {args.reports_dir} -- nothing to load.")
        sys.exit(0)

    conn = init_db(args.db, schema_path=os.path.join(os.path.dirname(__file__), "schema.sql"))
    seed_college_policy(conn, DEFAULT_COLLEGE_FULL_TIME_POLICY)

    for filename in pdf_files:
        path = os.path.join(args.reports_dir, filename)
        print(f"Parsing {filename} ...")
        try:
            report = parse_pdf(path)
        except Exception as e:
            print(f"  FAILED to parse {filename}: {e}", file=sys.stderr)
            continue

        if not report.student_id:
            print(f"  WARNING: no student ID found in {filename}; skipping load.", file=sys.stderr)
            continue

        report_id = load_report(conn, report, DEFAULT_COLLEGE_FULL_TIME_POLICY)
        n_sections = len(report.sections)
        print(f"  Loaded report_id={report_id} ({report.student_name!r}), {n_sections} sections")

    conn.close()
    print(f"\nDone. Database ready at: {os.path.abspath(args.db)}")


if __name__ == "__main__":
    main()
