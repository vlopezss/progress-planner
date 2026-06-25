# APR Parser & Local Dev Database

Parses UC Berkeley Academic Progress Report (APR) PDFs into a structured
SQLite database, so a website can be built and tested against real-shaped
data without needing a live connection to Berkeley's systems or any real
student records.

## Viewing the data in a browser

For a quick visual check of what's in the database -- without writing SQL
or running a web server -- generate a standalone HTML viewer:

```bash
python3 export_for_viewer.py   # dev.db -> viewer_data.json
python3 build_viewer.py        # viewer_data.json + viewer_template.html -> viewer.html
```

Then just open `viewer.html` in any browser (double-click it, no server
needed). It shows the report header, the same four category tables
(University, College, Major, Minor) as collapsible nested rows with
color-coded status, course tables, and "eligible but not yet used" course
lists, plus a flat Course History tab.

Re-run both commands any time `dev.db` changes to refresh the viewer.

`viewer.html` embeds the report data directly in the page, so **don't
commit it if you've loaded a real (non-synthetic) student report** -- it's
gitignored by default for this reason.

## Setup

```bash
pip install -r requirements.txt
```

## Building the local dev database

Drop one or more APR PDFs into `sample_reports/`, then run:

```bash
python3 setup_dev_db.py --fresh
```

This creates `dev.db` in this folder. `--fresh` deletes any existing
`dev.db` first for a completely clean rebuild; omit it to just re-load
(re-running is always safe -- each report's rows are deleted and
re-inserted by student ID, so nothing duplicates).

To use a different filename or folder:

```bash
python3 setup_dev_db.py --db mytest.db --reports-dir other_folder/
```

## Loading the course catalog

If you have a UC Berkeley course catalog export (CSV, from the
[Undergraduate Catalog](https://undergraduate.catalog.berkeley.edu/)),
place it in `catalog_data/` and load it into the same database:

```bash
pip install pandas   # only needed for this step
python3 load_catalog.py --csv catalog_data/your-catalog-export.csv --db dev.db
```

This populates the `courses` table, which `requirement_courses_available`
rows (from the progress report) can be joined against to get real course
titles, units, and descriptions instead of just a bare subject+number pair.

**Cleaning rules applied automatically** (see `load_catalog.py` docstring
for full detail):
- Filtered to undergraduate only (course numbers 1-199; Berkeley's own
  convention is 1-99 lower division, 100-199 upper division, 200+ is
  graduate/professional)
- True duplicate rows (same Subject+Number+Title, an export artifact) are
  merged into one, keeping whichever row has the fuller description
- A handful of Subject+Number pairs that genuinely map to more than one
  distinct course (e.g. `MUSIC 150A` is two different courses depending on
  the catalog year) are kept as multiple rows and flagged with
  `is_ambiguous_duplicate = 1`, since we can't safely guess which one is
  "correct" -- don't silently pick one in your own queries either

Re-run `load_catalog.py` any time you get a newer catalog export; it wipes
and reloads the whole `courses` table each time (it's a global lookup
table, not tied to a specific student report, so there's no per-report
delete/re-insert logic like `load_report()` has).

## Inspecting the database

```bash
python3 inspect_db.py                       # show every loaded report
python3 inspect_db.py --report-id 3037229508  # show just one
```

This prints a readable tree of every section/requirement/status, plus the
derived college and full-time-unit minimum, without needing to write SQL.

## What's in the database

See `schema.sql` for the full table definitions. Summary:

- `report_metadata` -- one row per student report (name, ID, college, etc.)
- `academic_plans` -- the major(s)/minor(s) listed on the report
- `sections` -- top-level groupings: University, College, Major, Minor, etc.
  (`category` column: `university` / `college` / `major` / `minor` /
  `in_progress` / `additional` / `info`)
- `requirements` -- individual requirements and sub-requirements, nested via
  `parent_requirement_id` (self-referencing, arbitrary depth)
- `requirement_courses` -- courses used to satisfy each requirement
- `requirement_courses_available` -- courses listed as eligible-but-unused
  options for a requirement
- `course_history` -- the full flat list of every course taken, independent
  of which requirement(s) it satisfies
- `college_full_time_policy` -- **hardcoded, not parsed from the PDF** --
  minimum units per term to count as full-time, by college. Source this
  from Berkeley's Registrar/Financial Aid pages, not the APR; verify
  current values before relying on this for real advising.
- `courses` -- the UC Berkeley undergraduate course catalog (loaded
  separately via `load_catalog.py`, not derived from the progress report).
  Join `requirement_courses_available` against this on
  `(subject, catalog_nbr)` to get real titles/units/descriptions for the
  "eligible but not yet used" course lists.

### Example queries

Reconstruct one of the four requirement-category tables:

```sql
SELECT s.section_name, r.requirement_name, r.status, r.depth
FROM sections s
JOIN requirements r ON r.section_id = s.section_id
WHERE s.category = 'major' AND s.report_id = '3037229508'
ORDER BY s.sort_order, r.sort_order;
```

Get a student's full-time unit minimum:

```sql
SELECT rm.college, cfp.min_units_full_time
FROM report_metadata rm
LEFT JOIN college_full_time_policy cfp ON rm.college = cfp.college
WHERE rm.report_id = '3037229508';
```

Get real course titles/units for a requirement's "eligible but not yet
used" options (requires `load_catalog.py` to have been run):

```sql
SELECT rca.subject, rca.catalog_nbr, c.title, c.units_min, c.units_max
FROM requirement_courses_available rca
LEFT JOIN courses c ON rca.subject = c.subject AND rca.catalog_nbr = c.catalog_nbr
WHERE rca.requirement_id = 42;  -- substitute a real requirement_id
```

## How the parser works (and its known limitations)

`parser.py` reads each PDF page using `pdfplumber`, reconstructing text
lines with their pixel position (needed to correctly match course tables
to the requirement they belong to -- some pages have several small course
tables stacked close together, and some tables span a page break).

**Heading detection** is the trickiest part of this document: most
requirement and sub-requirement names have no machine-readable code or
distinguishing punctuation. The key discovery that makes this reliable is
that **every heading in this document is underlined with a line spanning
its full text width**, while hyperlinks within body text are also
underlined but only under the linked phrase (a small fraction of the
line's width). The parser detects this directly from the PDF's vector
line objects rather than guessing from text shape, which is far more
robust to different majors/minors having differently-worded sub-requirement
names.

Known limitations, carried forward intentionally rather than silently
papered over:

- **Duplicate courses across requirements are not deduplicated.** The same
  course can count toward multiple requirements simultaneously (e.g. one
  STAT course satisfying both a major requirement and a minor requirement)
  -- this is correct and expected on Berkeley's own reports, but if your
  website does any cross-requirement aggregation, be aware a single course
  row can legitimately appear under several different `requirement_id`s.
- **`sum(requirement_courses.units)` will not always equal
  `requirements.units_used`.** Berkeley's system applies its own unit-cap
  adjustments (e.g. capping "Minimum Total Units" at exactly 120, excess PE
  unit limits) that aren't a simple sum of the listed course rows. Trust
  the requirement's own stated `units_required`/`units_used`, don't
  recompute it from the course list.
- **Verified against one sample report only** (Statistics BA major + Data
  Science minor, College of Letters & Science). The underline-based heading
  detection is a structural signal that should generalize well to other
  majors/colleges, but has not yet been confirmed against a second, 
  differently-shaped report. If you get a report from a different college
  (e.g. Engineering) or with different requirement structures, re-run
  `setup_dev_db.py` against it and spot-check the output with
  `inspect_db.py` before trusting it.
- ~~`requirements.notes` sometimes has a re-serialized course table glued
  onto the end of otherwise-correct explanatory text~~ **Fixed.** The
  parser now skips the plain-text lines that duplicate a just-consumed
  course table (`_skip_duplicate_course_row_lines` in `parser.py`), so
  `notes` only contains genuine explanatory prose. The HTML viewer's
  defensive truncation (`cleanNote()` in `viewer_template.html`) is no
  longer needed for this document but is left in place as a safety net.
- **Cross-page table splits are detected by a table's BOTTOM edge, not its
  top.** An earlier version of this parser checked whether a matched
  table's top y-coordinate was near the bottom of the page to decide
  whether it might continue onto the next page. This silently undercounted
  long tables that start mid-page but run off the bottom edge purely
  because they have many rows (found via "Minimum Total Units", a 40-row
  table starting 64% down the page -- only the first 20 rows were captured
  until this was fixed). If you see a `requirement_courses` count that
  looks short for a table you know is large in the source PDF, this
  bottom-vs-top distinction is the first thing to check.
- **College full-time-unit minimums are a hardcoded guess**, not derived
  from the PDF. Confirm current values against Berkeley's official policy
  before using this for real advising decisions.
- **The course catalog CSV's `Terms Offered` column is empty for every
  single row** in the export we tested -- whatever semester(s) a course
  runs in is only available as unstructured free text in the
  `offering_notes` field (e.g. "Offered every fall.", "Offered alternate
  years."), not as a clean queryable value. If your website needs to know
  "is this offered this semester," that information isn't reliably
  extractable from this data source as-is.
- **Course codes don't always match exactly between the catalog and the
  progress report**, specifically around Berkeley's "C"-prefix
  cross-listing notation. For example, the progress report lists
  `COMPSCI 88`, but the catalog only has `COMPSCI C88C` (no plain `88`
  entry) -- about 3% of `requirement_courses_available` pairs fail to
  match the catalog for this or similar reasons (confirmed: a handful are
  genuinely retired/renumbered courses not in the catalog snapshot at
  all). Don't assume an unmatched join means the course doesn't exist.
- **A small number of catalog rows are genuinely ambiguous**
  (`is_ambiguous_duplicate = 1` -- see "Loading the course catalog" above).
  Don't silently pick the first matching row in a query; surface both
  options or flag the ambiguity if it matters for your use case.

## Files

| File | Purpose |
|---|---|
| `schema.sql` | SQLite table definitions |
| `grammar.py` | Regex patterns and small parsing helpers |
| `parser.py` | PDF -> structured `ParsedReport` tree |
| `load_db.py` | `ParsedReport` -> SQLite rows |
| `load_catalog.py` | Course catalog CSV -> SQLite `courses` table (cleaned/deduplicated) |
| `setup_dev_db.py` | Entry point: rebuild `dev.db` from `sample_reports/` |
| `inspect_db.py` | Read-only CLI tool to view what's in the database |
| `export_for_viewer.py` | `dev.db` -> `viewer_data.json` |
| `viewer_template.html` | HTML/CSS/JS template for the browser viewer |
| `build_viewer.py` | Injects `viewer_data.json` into the template -> `viewer.html` |
| `sample_reports/` | PDFs to load (gitignored if they contain real data) |
| `catalog_data/` | Course catalog CSV exports (gitignored by default; see `.gitignore`) |
