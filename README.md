# Canvas Pipeline

Automated provisioning pipeline that extracts student, enrollment, and course
data from **ValleyPROD** (Ellucian Banner / Oracle) and imports it into
**Canvas LMS** via the SIS Import API.

---

## How it works

```
Canvas API                         ValleyPROD (Oracle)
      │                                    │
canvas_terms.py                    SQL queries (queries/*.sql)
canvas_courses.py <term>                   │
canvas_users.py                            ▼
      │                           valleyprod.py  ──►  transform.py  ──►  canvas_import.py
      ▼                           (extract)           (build CSVs)       (zip + upload)
output/canvas_*.json
(used to filter new-only records)
```

1. **Pre-fetch Canvas state** (optional but recommended — enables new-only filtering):
   - `canvas_terms.py` — fetches all active enrollment terms → `output/canvas_terms.json`
   - `canvas_courses.py <term>` — fetches existing courses for a term → `output/canvas_courses_<term>.json`
   - `canvas_users.py` — fetches all existing users → `output/canvas_users.json`
2. Connects to ValleyPROD Oracle using `python-oracledb` (thin mode — no Oracle Client required).
3. Runs three parameterized SQL queries for the given term code.
4. Transforms results into Canvas SIS CSV format.  Records already present in Canvas are automatically excluded:
   - `users.csv` — skips users whose SIS ID is already in `canvas_users.json`
   - `courses.csv` — skips courses whose SIS course ID is already in `canvas_courses_<term>.json`
   - `sections.csv` — sections for new courses only (mirrors `courses.csv` filtering)
   - `enrollments.csv` — all enrollments for the term
      - Upload mode safety: batch mode is used only when both `courses.csv` and `sections.csv`
        are skipped because their DataFrames are empty; otherwise a standard SIS import is used.
5. Zips the CSVs and POSTs them to the Canvas SIS Import API.
6. Polls Canvas until the import reaches a terminal state and logs the outcome.
7. Writes a timestamped log to `./output/<term>/pipeline_<timestamp>.log`.

### CSV fields

| File | Notable fields |
|------|---------------|
| `users.csv` | `user_id`, `login_id`, `first_name`, `last_name`, `email`, `status`, `integration_id` (PIDM). `login_id` and `email` are always the student's university MVSU email. If no `@mvsu.edu` address exists in Banner, one is constructed as `lower(first_name).lower(last_name)@mvsu.edu`. |
| `courses.csv` | `course_id`, `short_name`, `long_name`, `account_id`, `term_id`, `status`, `integration_id` (TERM+CRN), `course_format` |
| `sections.csv` | `section_id`, `course_id`, `name`, `status`, `start_date`, `end_date`, `integration_id` |
| `enrollments.csv` | `course_id`, `user_id`, `role`, `section_id`, `status` |

---

## Prerequisites

- Python 3.11+
- Network access to ValleyPROD (`10.1.129.50:1521`) and Canvas LMS

---

## Setup

### 1. Create a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

### 2. Configure `.env`

Copy `.env.example` to `.env` and fill in your values:

```ini
# ValleyPROD Oracle Database
# python-oracledb thin mode — no Oracle Client or ODBC driver required.
VALLEYPROD_HOST=10.1.129.50
VALLEYPROD_PORT=1521
VALLEYPROD_SERVICE=PROD
VALLEYPROD_USER=your_username
VALLEYPROD_PASSWORD=your_password

# Canvas LMS
CANVAS_URL=https://your-institution.instructure.com
CANVAS_TOKEN=your_admin_api_token
CANVAS_ACCOUNT_ID=1
CANVAS_AUTH_PROVIDER_ID=108

# Pipeline
OUTPUT_DIR=./output
LOG_LEVEL=INFO
```

> **Never commit `.env` to version control.**

---

## Usage

### Recommended workflow

```powershell
# 1. Refresh the list of active Canvas terms
python canvas_terms.py

# 2. Fetch existing courses for the target term (enables new-course filtering)
python canvas_courses.py 202710

# 3. Fetch existing users (enables new-user filtering)
#    Skips automatically if canvas_users.json is < 7 days old.
#    Only re-run after a pipeline import has added new users to Canvas.
python canvas_users.py                  # skips if file is fresh
python canvas_users.py --force          # always re-fetch
python canvas_users.py --max-age-days 1 # re-fetch if file is > 1 day old

# 4. Run the full pipeline
python pipeline.py --term 202710
```

Steps 1–3 only need to be rerun when you want to refresh what Canvas already
contains.  `canvas_users.py` in particular is slow (~6 min for 11k users), so
it skips the fetch when the output file is still fresh.  Re-run it with
`--force` after a pipeline import that added new students.

### Pipeline options

```powershell
# Full run — extract, transform, and upload to Canvas
python pipeline.py --term 202710

# Build CSVs and ZIP but skip the Canvas upload
python pipeline.py --term 202710 --dry-run

# Extract and write CSVs only (no upload)
python pipeline.py --term 202710 --skip-upload
```

### Enrollment invitation workflow (Canvas API)

```powershell
# List enrollments for a course section
python canvas_enrollments.py list --course-id 10284 --section-id 10721

# Accept an invitation for one enrollment (Canvas endpoint uses POST)
python canvas_enrollments.py accept --course-id 10284 --enrollment-id 184579

# Optional: force PUT if you need to test method behavior in your environment
python canvas_enrollments.py accept --course-id 10284 --enrollment-id 184579 --method PUT
```

The list command writes JSON output to `output/course_<course_id>_section_<section_id>_enrollments.json`
(or `output/course_<course_id>_enrollments.json` when no section is given).

### Exit codes

| Code | Meaning |
|------|---------|
| `0`  | Success (or dry-run) |
| `1`  | Canvas import failed / aborted |
| `2`  | Configuration or connection error |

---

## Project structure

```
canvas_pipeline/
├── pipeline.py          # Main entry point
├── valleyprod.py        # Oracle connector (python-oracledb thin mode)
├── transform.py         # ValleyPROD DataFrames → Canvas SIS CSVs
├── canvas_import.py     # Canvas SIS Import API uploader
├── canvas_terms.py      # Fetch active Canvas enrollment terms → canvas_terms.json
├── canvas_courses.py    # Fetch existing Canvas courses for a term → canvas_courses_<term>.json
├── canvas_users.py      # Fetch existing Canvas users → canvas_users.json
├── canvas_enrollments.py # List enrollments and accept invitation endpoints
├── config.py            # Central config loader (reads .env)
├── requirements.txt     # Python dependencies
├── queries/
│   ├── students.sql     # Returns students for a term
│   ├── enrollments.sql  # Returns student + faculty enrollments
│   └── courses.sql      # Returns course sections (includes integration_id, course_format)
└── output/
    ├── canvas_terms.json            # Active Canvas terms (from canvas_terms.py)
    ├── canvas_courses_<term>.json   # Existing courses for a term (from canvas_courses.py)
    ├── canvas_users.json            # Existing Canvas users (from canvas_users.py)
    └── <term>/                      # Generated CSVs, ZIPs, and log files
```

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `oracledb` | Oracle database driver (thin mode — no client install needed) |
| `pandas` | DataFrame manipulation and CSV output |
| `requests` | Canvas REST API calls |
| `python-dotenv` | Loads `.env` into environment variables |
