"""
transform.py — ValleyPROD DataFrames → Canvas SIS CSV files.

Converts the raw ValleyPROD query results into the exact CSV format that the
Canvas SIS Import API expects, then writes them to the output directory.

Canvas SIS CSV spec:
  https://canvas.instructure.com/doc/api/file.sis_csv.html

Files produced:
  users.csv        — one row per unique person (students + faculty)
  sections.csv     — one row per course section
  enrollments.csv  — one row per person-section pair
"""

import logging
import re
from pathlib import Path

import pandas as pd

import config

logger = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _clean_name(value) -> str:
    """Strip whitespace and return empty string for None/NaN."""
    if pd.isna(value) or value is None:
        return ""
    return str(value).strip()


def _make_login_id(email: str, student_id: str) -> str:
    """
    Return the university (MVSU) email as the Canvas login_id.
    The SQL query always constructs this as
    lower(first_initial)+lower(last_name, hyphens removed)+"1"@students.mvsu.edu.
    Falls back to student_id only when no email is present.
    """
    if email:
        return email.lower()
    return str(student_id).strip()


def _sortable_name(first: str, last: str) -> str:
    return f"{last}, {first}".strip(", ")


# ── Main transform functions ──────────────────────────────────────────────────

def build_users_csv(
    students_df: pd.DataFrame,
    existing_sis_ids: set | None = None,
) -> pd.DataFrame:
    """
    Build the Canvas users.csv from the ValleyPROD students DataFrame.

    Canvas required columns:
        user_id, login_id, first_name, last_name, email, status

    Canvas optional columns we also include:
        full_name, sortable_name, authentication_provider_id, integration_id

    Parameters
    ----------
    existing_sis_ids
        Set of sis_user_id values already present in Canvas (from
        canvas_users.json).  Users whose user_id is in this set are
        excluded from the output — they don't need to be re-imported.
    """
    if students_df.empty:
        logger.warning("students_df is empty — users.csv will have no rows.")
        return pd.DataFrame()

    rows = []
    seen_user_ids = set()

    for _, row in students_df.iterrows():
        user_id = _clean_name(row.get("student_id"))
        if not user_id or user_id in seen_user_ids:
            continue
        seen_user_ids.add(user_id)

        first          = _clean_name(row.get("first_name"))
        last           = _clean_name(row.get("last_name"))
        email          = _clean_name(row.get("email"))
        integration_id = _clean_name(row.get("integration_id"))

        rows.append({
            "user_id":                   user_id,
            "login_id":                  _make_login_id(email, user_id),
            "authentication_provider_id": config.CANVAS_AUTH_PROVIDER_ID,
            "first_name":                first,
            "last_name":                 last,
            "full_name":                 f"{first} {last}".strip(),
            "sortable_name":             _sortable_name(first, last),
            "email":                     email,
            "status":                    "active",
            "integration_id":            integration_id,
        })

    df = pd.DataFrame(rows)

    if existing_sis_ids:
        before = len(df)
        df = df[~df["user_id"].isin(existing_sis_ids)]
        logger.info(
            "users.csv: skipped %d users already in Canvas; %d new users remain",
            before - len(df), len(df),
        )
    else:
        logger.info("users.csv: %d unique users (no existing-user filter applied)", len(df))

    return df


def build_sections_csv(courses_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build the Canvas sections.csv from the ValleyPROD courses DataFrame.

    Canvas required columns:
        section_id, course_id, name, status

    Canvas optional:
        start_date, end_date, integration_id
    """
    if courses_df.empty:
        logger.warning("courses_df is empty — sections.csv will have no rows.")
        return pd.DataFrame()

    df = courses_df[
        ["section_id", "course_id", "short_name", "status", "start_date", "end_date", "integration_id"]
    ].copy()

    df.rename(columns={"short_name": "name"}, inplace=True)
    df.drop_duplicates(subset=["section_id"], inplace=True)

    logger.info("sections.csv: %d sections", len(df))
    return df


def build_courses_csv(
    courses_df: pd.DataFrame,
    existing_sis_ids: set | None = None,
) -> pd.DataFrame:
    """
    Build the Canvas courses.csv from the ValleyPROD courses DataFrame.

    Canvas required columns:
        course_id, short_name, long_name, account_id, term_id, status

    Canvas optional columns we also include:
        integration_id, course_format

    Parameters
    ----------
    existing_sis_ids
        Set of sis_course_id values already present in Canvas (from
        canvas_courses.json).  Courses whose course_id is in this set
        are excluded from the output — they don't need to be re-imported.
    """
    if courses_df.empty:
        logger.warning("courses_df is empty — courses.csv will have no rows.")
        return pd.DataFrame()

    df = courses_df[
        ["course_id", "short_name", "long_name", "account_id", "term_id", "status", "integration_id", "format"]
    ].copy()
    df.rename(columns={"format": "course_format"}, inplace=True)
    df.drop_duplicates(subset=["course_id"], inplace=True)

    if existing_sis_ids:
        before = len(df)
        df = df[~df["course_id"].isin(existing_sis_ids)]
        logger.info(
            "courses.csv: skipped %d courses already in Canvas; %d new courses remain",
            before - len(df), len(df),
        )
    else:
        logger.info("courses.csv: %d courses (no existing-course filter applied)", len(df))

    return df


def build_enrollments_csv(enrollments_df: pd.DataFrame) -> pd.DataFrame:
    """
    Build the Canvas enrollments.csv from the ValleyPROD enrollments DataFrame.

    Canvas required columns:
        section_id, user_id, role, status

    Role values recognised by Canvas: student, teacher, ta, observer, designer
    """
    if enrollments_df.empty:
        logger.warning("enrollments_df is empty — enrollments.csv will have no rows.")
        return pd.DataFrame()

    df = enrollments_df[["section_id", "user_id", "role", "status"]].copy()
    df.drop_duplicates(subset=["section_id", "user_id", "role"], inplace=True)

    logger.info(
        "enrollments.csv: %d rows (%d students, %d teachers)",
        len(df),
        (df["role"] == "student").sum(),
        (df["role"] == "teacher").sum(),
    )
    return df


# ── Write to disk ─────────────────────────────────────────────────────────────

def write_csvs(
    students_df: pd.DataFrame,
    enrollments_df: pd.DataFrame,
    courses_df: pd.DataFrame,
    output_dir: str,
    term_code: str,
    existing_canvas_course_ids: set | None = None,
    existing_canvas_user_ids: set | None = None,
) -> dict[str, Path]:
    """
    Transform ValleyPROD DataFrames and write all four CSV files.

    Returns a dict mapping csv name → Path so the caller can zip them.

    Parameters
    ----------
    existing_canvas_course_ids
        Optional set of sis_course_id values already in Canvas.  Passed
        through to build_courses_csv to suppress already-provisioned courses.
    existing_canvas_user_ids
        Optional set of sis_user_id values already in Canvas.  Passed
        through to build_users_csv to suppress already-provisioned users.
    """
    out = Path(output_dir) / term_code
    out.mkdir(parents=True, exist_ok=True)

    files: dict[str, Path] = {}

    courses_out = build_courses_csv(courses_df, existing_canvas_course_ids)
    new_course_ids = set(courses_out["course_id"]) if not courses_out.empty else set()
    new_courses_df = courses_df[courses_df["course_id"].isin(new_course_ids)]

    csv_map = {
        "users.csv":       build_users_csv(students_df, existing_canvas_user_ids),
        "courses.csv":     courses_out,
        "sections.csv":    build_sections_csv(new_courses_df),
        "enrollments.csv": build_enrollments_csv(enrollments_df),
    }

    for name, df in csv_map.items():
        path = out / name
        if df.empty:
            logger.warning("Skipping %s — empty DataFrame.", name)
            continue
        df.to_csv(path, index=False)
        logger.info("Wrote %s (%d rows) → %s", name, len(df), path)
        files[name] = path

    return files
