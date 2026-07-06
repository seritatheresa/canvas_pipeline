"""
canvas_backfill.py — Backfill the canvas-event-broker tracking tables
(GAP-01 / GAP-05 / GAP-09).

After a successful SIS import, the event broker has no knowledge of what
canvas_pipeline provisioned: ETSIS.CANVASLMS_SECTIONS and
ETSIS.CANVASLMS_ENROLLMENTS are empty for every pipeline-created course,
so the broker rejects legitimate EnrollStudent/DropStudent events as
"untracked". This module closes that gap:

  1. Looks up every course/section for the term via the Canvas REST API
     (the SIS Import API returns only aggregate counts, not Canvas IDs).
  2. Parses each sis_section_id back to its Banner term + CRN. The
     canonical format is TERM+CRN concatenated (e.g. 20262012345);
     legacy colon-separated IDs (term:crn[:suffix]) are also accepted.
  3. Resolves Canvas sis_user_id (Banner campus ID) to Banner PIDM via
     SPRIDEN, because the broker keys enrollments by PIDM (GAP-09).
  4. MERGEs rows into the tracking tables so re-runs are idempotent.

Usage (standalone):
    python canvas_backfill.py --term 202620
    python canvas_backfill.py --term 202620 --dry-run

Or from pipeline.py, which calls run_backfill() automatically after a
successful import.

Configuration comes from .env via config.py (CANVAS_URL, CANVAS_TOKEN,
VALLEYPROD_* Oracle credentials).
"""

import argparse
import json
import logging
import re
import sys
from pathlib import Path

import oracledb
import requests

import config

logger = logging.getLogger(__name__)

TRACKING_SCHEMA = "ETSIS"
SECTIONS_TABLE = "CANVASLMS_SECTIONS"
ENROLLMENTS_TABLE = "CANVASLMS_ENROLLMENTS"

# Enrollment types worth tracking (matches what the broker manages)
TRACKED_ENROLLMENT_TYPES = ("StudentEnrollment", "TeacherEnrollment")

# Oracle IN-list limit
_IN_CHUNK = 1000

# CANVASLMS_ENROLLMENTS.url is varchar2(128)
_URL_MAX = 128


# ── SIS ID parsing ────────────────────────────────────────────────────────────

_CANONICAL_RE = re.compile(r"^(\d{6})(\w+)$")


def parse_sis_section_id(sis_section_id):
    """Return (term, crn) parsed from a Canvas sis_section_id, or None.

    Mirrors Common.parseSisSectionId() in canvas-event-broker: canonical
    TERM+CRN concatenation first, legacy colon formats second.
    """
    if not sis_section_id:
        return None
    if ":" in sis_section_id:
        tokens = sis_section_id.split(":")
        if len(tokens) in (2, 3):
            return tokens[0], tokens[1]
        return None
    match = _CANONICAL_RE.match(sis_section_id)
    if match:
        return match.group(1), match.group(2)
    return None


# ── Canvas REST API ───────────────────────────────────────────────────────────

class CanvasLookup:
    """Minimal paginated Canvas REST client for the backfill lookups."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {config.CANVAS_TOKEN}",
        })
        self._base = config.CANVAS_URL

    def _get_paginated(self, path, params=None):
        """GET all pages of a Canvas collection endpoint."""
        url = f"{self._base}/api/v1{path}"
        params = dict(params or {}, per_page=100)
        results = []
        while url:
            response = self._session.get(url, params=params)
            response.raise_for_status()
            results.extend(response.json())
            url = response.links.get("next", {}).get("url")
            params = None  # 'next' URL already carries the query string
        return results

    def get_courses_for_term(self, enrollment_term_id):
        """All courses in the account for a Canvas enrollment term."""
        return self._get_paginated(
            f"/accounts/{config.CANVAS_ACCOUNT_ID}/courses",
            {"enrollment_term_id": enrollment_term_id},
        )

    def get_sections(self, course_id):
        """All sections of a course (includes sis_section_id)."""
        return self._get_paginated(f"/courses/{course_id}/sections")

    def get_enrollments(self, course_id):
        """Active student/teacher enrollments of a course."""
        return self._get_paginated(
            f"/courses/{course_id}/enrollments",
            {"type[]": list(TRACKED_ENROLLMENT_TYPES), "state[]": "active"},
        )


def resolve_term_id(sis_term_id):
    """Resolve a Banner term code to the Canvas enrollment term id.

    Reads output/canvas_terms.json (produced by canvas_terms.py), same as
    the other pipeline scripts.
    """
    terms_path = Path(config.OUTPUT_DIR) / "canvas_terms.json"
    if not terms_path.exists():
        raise FileNotFoundError(
            f"Terms file not found: {terms_path}\n"
            "Run canvas_terms.py first to generate it."
        )
    with open(terms_path, encoding="utf-8") as f:
        terms = json.load(f)
    for term in terms:
        if str(term.get("sis_term_id")) == str(sis_term_id):
            return term["id"]
    raise ValueError(
        f"sis_term_id '{sis_term_id}' not found in {terms_path}. "
        "Re-run canvas_terms.py to refresh the list."
    )


# ── Oracle helpers ────────────────────────────────────────────────────────────

def connect_oracle():
    """Open a thin-mode oracledb connection using the pipeline's credentials."""
    config.require_oracle_config()
    dsn = "{}:{}/{}".format(
        config.VALLEYPROD_HOST, config.VALLEYPROD_PORT, config.VALLEYPROD_SERVICE
    )
    return oracledb.connect(
        user=config.VALLEYPROD_USER,
        password=config.VALLEYPROD_PASSWORD,
        dsn=dsn,
    )


def _table_columns(cursor, table_name):
    """Return the set of column names for a tracking table (uppercase)."""
    cursor.execute(
        """
        SELECT column_name FROM all_tab_columns
        WHERE  owner = :owner AND table_name = :table_name
        """,
        owner=TRACKING_SCHEMA, table_name=table_name,
    )
    return {row[0] for row in cursor.fetchall()}


def resolve_pidms(cursor, campus_ids):
    """Map Banner campus IDs (Canvas sis_user_id) to PIDMs via SPRIDEN.

    The broker keys CANVASLMS_ENROLLMENTS by PIDM (GAP-09), so every
    enrollment row must carry the resolved PIDM. Only current identity
    records are used (spriden_change_ind IS NULL).
    """
    campus_ids = [c for c in set(campus_ids) if c]
    pidm_map = {}
    for i in range(0, len(campus_ids), _IN_CHUNK):
        chunk = campus_ids[i:i + _IN_CHUNK]
        binds = {f"id{n}": v for n, v in enumerate(chunk)}
        placeholders = ", ".join(f":id{n}" for n in range(len(chunk)))
        cursor.execute(
            f"""
            SELECT spriden_id, spriden_pidm
            FROM   spriden
            WHERE  spriden_change_ind IS NULL
              AND  spriden_id IN ({placeholders})
            """,
            binds,
        )
        pidm_map.update({row[0]: row[1] for row in cursor.fetchall()})
    return pidm_map


# ── MERGE statements (idempotent — safe to re-run) ────────────────────────────

def _merge_section_sql(has_sis_columns):
    """Build the sections MERGE. TrackCourseSection.sql in the broker inserts
    sis_course_id/sis_section_id, but InstallDbObjects.sql does not define
    them — so adapt to whichever shape the deployed table actually has."""
    insert_cols = "term, crn, course_id, section_id"
    insert_vals = "src.term, src.crn, src.course_id, src.section_id"
    if has_sis_columns:
        insert_cols += ", sis_course_id, sis_section_id"
        insert_vals += ", src.sis_course_id, src.sis_section_id"
    return f"""
        MERGE INTO {TRACKING_SCHEMA}.{SECTIONS_TABLE} tgt
        USING (SELECT :term AS term, :crn AS crn,
                      :course_id AS course_id, :section_id AS section_id,
                      :sis_course_id AS sis_course_id,
                      :sis_section_id AS sis_section_id
               FROM dual) src
        ON (tgt.term = src.term AND tgt.crn = src.crn
            AND tgt.section_id = src.section_id
            AND tgt.course_id = src.course_id)
        WHEN NOT MATCHED THEN
            INSERT ({insert_cols}) VALUES ({insert_vals})
    """


_MERGE_ENROLLMENT_SQL = f"""
    MERGE INTO {TRACKING_SCHEMA}.{ENROLLMENTS_TABLE} tgt
    USING (SELECT :term AS term, :crn AS crn, :type AS type, :pidm AS pidm,
                  :user_id AS user_id, :enrollment_id AS enrollment_id,
                  :course_id AS course_id, :section_id AS section_id,
                  :url AS url
           FROM dual) src
    ON (tgt.term = src.term AND tgt.crn = src.crn
        AND tgt.enrollment_id = src.enrollment_id
        AND tgt.course_id = src.course_id
        AND tgt.section_id = src.section_id)
    WHEN NOT MATCHED THEN
        INSERT (term, crn, type, pidm, user_id, enrollment_id,
                course_id, section_id, url)
        VALUES (src.term, src.crn, src.type, src.pidm, src.user_id,
                src.enrollment_id, src.course_id, src.section_id, src.url)
"""


# ── Main backfill routine ─────────────────────────────────────────────────────

def run_backfill(term_code, dry_run=False):
    """Backfill CANVASLMS_SECTIONS and CANVASLMS_ENROLLMENTS for a term.

    Returns a stats dict:
        {sections, enrollments, skipped_sections, unresolved_users}
    Raises on connection/API errors so callers can treat failure distinctly
    from an import failure.
    """
    stats = {
        "sections": 0,
        "enrollments": 0,
        "skipped_sections": 0,
        "unresolved_users": 0,
    }

    canvas = CanvasLookup()
    enrollment_term_id = resolve_term_id(term_code)
    logger.info(
        "Backfill: term %s (Canvas term id %s)%s",
        term_code, enrollment_term_id, " [DRY RUN]" if dry_run else "",
    )

    # ── Gather sections from Canvas ───────────────────────────────────────
    courses = canvas.get_courses_for_term(enrollment_term_id)
    logger.info("Backfill: found %d Canvas courses for term %s", len(courses), term_code)

    # section rows keyed by Canvas section id, for enrollment resolution
    section_rows = {}
    for course in courses:
        for section in canvas.get_sections(course["id"]):
            parsed = parse_sis_section_id(section.get("sis_section_id"))
            if parsed is None or parsed[0] != str(term_code):
                stats["skipped_sections"] += 1
                logger.debug(
                    "Skipping section %s — unparseable/foreign sis_section_id %r",
                    section["id"], section.get("sis_section_id"),
                )
                continue
            _, crn = parsed
            section_rows[section["id"]] = {
                "term": str(term_code),
                "crn": crn,
                "course_id": section["course_id"],
                "section_id": section["id"],
                "sis_course_id": course.get("sis_course_id"),
                "sis_section_id": section.get("sis_section_id"),
            }

    # ── Gather enrollments from Canvas ────────────────────────────────────
    enrollment_rows = []
    campus_ids = []
    for course in courses:
        for enrollment in canvas.get_enrollments(course["id"]):
            section_row = section_rows.get(enrollment.get("course_section_id"))
            if section_row is None:
                continue  # enrollment in a skipped/foreign section
            sis_user_id = enrollment.get("sis_user_id") or (
                enrollment.get("user") or {}
            ).get("sis_user_id")
            enrollment_rows.append({
                "term": section_row["term"],
                "crn": section_row["crn"],
                "type": enrollment["type"],
                "sis_user_id": sis_user_id,
                "user_id": enrollment["user_id"],
                "enrollment_id": enrollment["id"],
                "course_id": enrollment["course_id"],
                "section_id": enrollment["course_section_id"],
                "url": (enrollment.get("html_url") or "")[:_URL_MAX],
            })
            campus_ids.append(sis_user_id)

    logger.info(
        "Backfill: %d sections, %d enrollments gathered (%d sections skipped)",
        len(section_rows), len(enrollment_rows), stats["skipped_sections"],
    )

    if dry_run:
        stats["sections"] = len(section_rows)
        stats["enrollments"] = len(enrollment_rows)
        logger.info("Backfill dry run complete — no database writes performed.")
        return stats

    # ── Write to Oracle ───────────────────────────────────────────────────
    conn = connect_oracle()
    try:
        cursor = conn.cursor()

        # Adapt to the deployed CANVASLMS_SECTIONS shape (see _merge_section_sql)
        section_columns = _table_columns(cursor, SECTIONS_TABLE)
        has_sis_columns = {"SIS_COURSE_ID", "SIS_SECTION_ID"} <= section_columns
        if not has_sis_columns:
            logger.warning(
                "%s.%s has no sis_course_id/sis_section_id columns — "
                "inserting without them. Consider ALTERing the table to match "
                "the broker's TrackCourseSection.sql.",
                TRACKING_SCHEMA, SECTIONS_TABLE,
            )
        merge_section_sql = _merge_section_sql(has_sis_columns)

        # PIDM resolution (GAP-09)
        pidm_map = resolve_pidms(cursor, campus_ids)

        for row in section_rows.values():
            cursor.execute(merge_section_sql, row)
            stats["sections"] += cursor.rowcount

        for row in enrollment_rows:
            pidm = pidm_map.get(row["sis_user_id"])
            if pidm is None:
                stats["unresolved_users"] += 1
                logger.warning(
                    "No SPRIDEN match for sis_user_id %r "
                    "(term=%s crn=%s enrollment=%s) — row skipped",
                    row["sis_user_id"], row["term"], row["crn"],
                    row["enrollment_id"],
                )
                continue
            params = {k: v for k, v in row.items() if k != "sis_user_id"}
            params["pidm"] = pidm
            cursor.execute(_MERGE_ENROLLMENT_SQL, params)
            stats["enrollments"] += cursor.rowcount

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    logger.info(
        "Backfill complete: %d section rows and %d enrollment rows merged "
        "(%d sections skipped, %d users unresolved)",
        stats["sections"], stats["enrollments"],
        stats["skipped_sections"], stats["unresolved_users"],
    )
    return stats


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Backfill canvas-event-broker tracking tables from Canvas",
    )
    parser.add_argument("--term", "-t", required=True,
                        help="Banner term code, e.g. 202620")
    parser.add_argument("--dry-run", action="store_true",
                        help="Fetch and report counts, but write nothing to Oracle.")
    args = parser.parse_args(argv)

    try:
        stats = run_backfill(args.term.strip(), dry_run=args.dry_run)
    except Exception as exc:
        logger.error("Backfill failed: %s", exc, exc_info=True)
        return 1

    logger.info("Backfill stats: %s", stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
