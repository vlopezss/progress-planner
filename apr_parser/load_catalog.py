"""
Clean and load the UC Berkeley course catalog export (CSV) into the
`courses` table. Kept separate from the progress-report pipeline since
this is a different data source with its own quirks.

Cleaning rules applied (see README "Course catalog data quality notes"
for the full reasoning behind each):

1. FILTER TO UNDERGRADUATE: keep only courses numbered 1-199 (Berkeley's
   own documented convention: 1-99 lower division, 100-199 upper division,
   200+ is graduate/professional). The numeric part is extracted from the
   Course Number field regardless of letter prefix/suffix (e.g. "N190G"
   -> 190, "C8" -> 8).

2. DEDUPLICATE Subject+Course Number groups. The raw CSV has a small
   number (well under 1% of rows) of repeated Subject+Number pairs. Each
   group is classified automatically:
     - SAME TITLE (regardless of description differences): treated as a
       true duplicate row (export artifact). We keep the row with the
       longer, less placeholder-heavy description and drop the rest.
     - DIFFERENT TITLES: this could be (a) a genuine title-history /
       spelling artifact for the same actual course (e.g. "Orthopedic"
       vs "Orthopaedic Biomechanics"), or (b) two genuinely different
       courses that happen to share a catalog number. We cannot reliably
       tell these apart from the data alone, so rather than guessing, we
       keep ALL rows in the group and mark them is_ambiguous_duplicate=1
       so the website / future code can decide how to handle them (e.g.
       showing both options to the person rather than silently picking
       one) instead of us discarding data that might be needed.

Usage:
    python3 load_catalog.py --csv courses-report.csv --db dev.db
"""

import argparse
import re
import sqlite3

import pandas as pd

PLACEHOLDER = "-"


def extract_course_num(catalog_nbr: str) -> int | None:
    m = re.search(r"(\d+)", str(catalog_nbr))
    return int(m.group(1)) if m else None


def description_quality(desc: str) -> int:
    """Higher is better. Used to pick which row to keep among true duplicates."""
    if not isinstance(desc, str) or desc.strip() in ("", PLACEHOLDER):
        return 0
    return len(desc)


def clean_catalog(csv_path: str) -> pd.DataFrame:
    df = pd.read_csv(csv_path)

    df["_num"] = df["Course Number"].apply(extract_course_num)
    before = len(df)
    df = df[df["_num"].notna() & (df["_num"] < 200)].copy()
    print(f"Filtered to undergraduate (1-199): kept {len(df)} of {before} rows")

    # classify and resolve duplicate Subject+Course Number groups
    dupe_mask = df.duplicated(subset=["Subject", "Course Number"], keep=False)
    dupe_groups = df[dupe_mask].groupby(["Subject", "Course Number"])

    rows_to_drop = []
    ambiguous_keys = set()

    for key, group in dupe_groups:
        titles = group["Course Title"].unique()
        if len(titles) == 1:
            # true duplicate: keep the row with the best (longest, non-placeholder) description
            best_idx = group["Course Description"].apply(description_quality).idxmax()
            drop_idx = [i for i in group.index if i != best_idx]
            rows_to_drop.extend(drop_idx)
        else:
            # genuinely different titles under the same number -- can't safely
            # auto-merge; keep all rows, flag them
            ambiguous_keys.add(key)

    if rows_to_drop:
        print(f"Dropping {len(rows_to_drop)} true-duplicate rows (same title, kept best description)")
        df = df.drop(index=rows_to_drop)

    df["is_ambiguous_duplicate"] = df.apply(
        lambda r: 1 if (r["Subject"], r["Course Number"]) in ambiguous_keys else 0, axis=1
    )
    if ambiguous_keys:
        print(f"Flagged {len(ambiguous_keys)} Subject+Number pairs as ambiguous duplicates (kept all rows): {sorted(ambiguous_keys)}")

    return df


def build_offering_notes(row) -> str | None:
    parts = []
    for col in ["Offering Information", "Offering Details", "Additional Offering Information"]:
        val = row.get(col)
        if isinstance(val, str) and val.strip() and val.strip() != PLACEHOLDER:
            parts.append(val.strip())
    return " | ".join(parts) if parts else None


def load_courses(conn: sqlite3.Connection, df: pd.DataFrame):
    cur = conn.cursor()
    cur.execute("DELETE FROM courses")

    def clean_field(val):
        if not isinstance(val, str):
            return None
        val = val.strip()
        return None if val == PLACEHOLDER or val == "" else val

    n_inserted = 0
    for _, row in df.iterrows():
        cur.execute(
            """INSERT INTO courses
               (subject, catalog_nbr, department, title, units_min, units_max,
                description, cross_listed_raw, repeat_rules, repeat_rules_special,
                offering_notes, is_ambiguous_duplicate)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["Subject"],
                row["Course Number"],
                clean_field(row.get("Department(s)")),
                clean_field(row.get("Course Title")),
                row.get("Credits - Units - Minimum Units"),
                row.get("Credits - Units - Maximum Units"),
                clean_field(row.get("Course Description")),
                clean_field(row.get("Cross-Listed Course(s)")),
                clean_field(row.get("Repeat Rules")),
                clean_field(row.get("Repeat Rule: Special Circumstances")),
                build_offering_notes(row),
                int(row["is_ambiguous_duplicate"]),
            ),
        )
        n_inserted += 1

    conn.commit()
    print(f"Inserted {n_inserted} courses into the database")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--csv", required=True, help="Path to the course catalog CSV export")
    ap.add_argument("--db", default="dev.db")
    args = ap.parse_args()

    df = clean_catalog(args.csv)

    conn = sqlite3.connect(args.db)
    load_courses(conn, df)
    conn.close()


if __name__ == "__main__":
    main()
