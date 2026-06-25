-- Academic Progress Report (APR) parsed schema
-- Verification stage: SQLite, self-referencing requirement hierarchy

CREATE TABLE IF NOT EXISTS report_metadata (
    report_id TEXT PRIMARY KEY,
    student_name TEXT,
    student_id TEXT,
    report_date TEXT,
    academic_career TEXT,
    academic_program TEXT,
    requirement_term TEXT,
    college TEXT,                  -- derived from college section header text
    min_units_full_time INTEGER    -- NOT parsed from PDF; hardcoded lookup by college
);

-- One row per academic plan listed (a report can have multiple: major(s) + minor(s))
CREATE TABLE IF NOT EXISTS academic_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT REFERENCES report_metadata(report_id),
    plan_name TEXT,                -- e.g. "Statistics BA Major - Regular Acad/Prfnl"
    plan_type TEXT                 -- 'major' or 'minor', inferred from text
);

-- Top-level groupings: University, Berkeley Campus, College, Major, Minor
CREATE TABLE IF NOT EXISTS sections (
    section_id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT REFERENCES report_metadata(report_id),
    category TEXT,                 -- 'university', 'college', 'major', 'minor', 'in_progress', 'additional'
    section_code TEXT,             -- e.g. 'RG-0131'
    section_name TEXT,             -- e.g. 'UNIVERSITY OF CALIFORNIA AND BERKELEY CAMPUS REQUIREMENTS'
    overall_status TEXT,           -- e.g. 'Overall Requirement Not Satisfied', or NULL if not stated
    page_number INTEGER,
    sort_order INTEGER             -- preserves original document order
);

-- Requirements and sub-requirements, arbitrary depth via parent_requirement_id
CREATE TABLE IF NOT EXISTS requirements (
    requirement_id INTEGER PRIMARY KEY AUTOINCREMENT,
    section_id INTEGER REFERENCES sections(section_id),
    parent_requirement_id INTEGER REFERENCES requirements(requirement_id),
    requirement_code TEXT,          -- e.g. 'R-0002', may be NULL for sub-requirements without their own code
    requirement_name TEXT,          -- e.g. 'American History & Institutions'
    status TEXT,                    -- 'Satisfied' / 'Not Satisfied' / other observed status text
    units_required REAL,
    units_used REAL,
    units_needed REAL,
    courses_required REAL,
    courses_used REAL,
    courses_needed REAL,
    gpa_required REAL,
    gpa_completed REAL,
    notes TEXT,                     -- explanatory free text directly under the status line
    depth INTEGER,                  -- 0 = top-level requirement under a section, increases with nesting
    sort_order INTEGER
);

-- Courses used to satisfy a specific requirement (from "Courses Used" tables)
CREATE TABLE IF NOT EXISTS requirement_courses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER REFERENCES requirements(requirement_id),
    term TEXT,
    subject TEXT,
    catalog_nbr TEXT,
    course_title TEXT,
    grade TEXT,
    units REAL,
    course_type TEXT                -- EN/TR/TE/IP/TA/TB/TC etc.
);

-- Courses listed as available-but-unused options (from "Courses Available" free text lists)
CREATE TABLE IF NOT EXISTS requirement_courses_available (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requirement_id INTEGER REFERENCES requirements(requirement_id),
    subject TEXT,
    catalog_nbr TEXT
);

-- Full flat course history at the end of the report (independent of requirement satisfaction)
CREATE TABLE IF NOT EXISTS course_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id TEXT REFERENCES report_metadata(report_id),
    term TEXT,
    subject TEXT,
    catalog_nbr TEXT,
    title TEXT,
    grade TEXT,
    units REAL,
    course_type TEXT,
    requirement_designation TEXT    -- e.g. 'AC - American Cultures', NULL if none listed
);

-- Hardcoded lookup, NOT derived from the PDF: minimum units per term to be
-- considered a full-time undergraduate, by college. Source: Berkeley
-- Registrar / Financial Aid policy pages, not the APR itself.
CREATE TABLE IF NOT EXISTS college_full_time_policy (
    college TEXT PRIMARY KEY,
    min_units_full_time INTEGER,
    source_note TEXT
);

-- Undergraduate course catalog (1-199 numbering only -- see
-- load_catalog.py / README for the cleaning rules applied before this
-- table is populated: graduate/professional courses numbered 200+ are
-- excluded, and a small number of genuine duplicate catalog rows
-- (same course, inconsistent spelling/title-history) are merged into one.
CREATE TABLE IF NOT EXISTS courses (
    course_id INTEGER PRIMARY KEY AUTOINCREMENT,
    subject TEXT NOT NULL,
    catalog_nbr TEXT NOT NULL,
    department TEXT,
    title TEXT,
    units_min REAL,
    units_max REAL,
    description TEXT,
    cross_listed_raw TEXT,          -- raw "Cross-Listed Course(s)" text, comma-separated SUBJECTCATNBR pairs
    repeat_rules TEXT,
    repeat_rules_special TEXT,
    offering_notes TEXT,            -- concatenation of the free-text "Offering..." columns (no structured term data exists in the source CSV)
    is_ambiguous_duplicate INTEGER DEFAULT 0  -- 1 if this subject+catalog_nbr legitimately
                                               -- maps to more than one distinct course in the
                                               -- source catalog (e.g. MUSIC 150A) and could not
                                               -- be safely auto-merged; see README
);

-- A UNIQUE index rather than a table-level UNIQUE constraint, so that
-- legitimately ambiguous pairs (is_ambiguous_duplicate = 1) can still be
-- inserted as multiple rows without violating it -- enforced instead in
-- load_catalog.py's cleaning step, not at the schema level.
CREATE INDEX IF NOT EXISTS idx_courses_subject_catalog_nbr ON courses(subject, catalog_nbr);
