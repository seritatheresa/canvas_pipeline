"""
canvas_login_activity_pull.py — Fetch enrollment-level last-activity data for
online/dual-enrollment courses, covering BOTH students and instructors, so we
can report who has not logged into Canvas as of a given date.

Written to match the conventions of canvas_analytics_pull.py / canvas_users.py
in this pipeline (same config.py, same pagination pattern via response.links,
same logging/argparse style, same online/DE course filter).

WHY THIS SCRIPT (rather than reusing analytics_bundle.json):
The Canvas Analytics API's student_summaries endpoint only covers students
and only reports page-view *counts*, not an actual last-activity timestamp,
and it has no instructor equivalent at all. The Canvas Enrollments API
(GET /courses/:id/enrollments) returns a `last_activity_at` timestamp on
every enrollment object — student, teacher, or TA — which is exactly what
"has this person logged in" needs. This script pulls that, per matched
online/DE course, for every enrollment type.

WHY THIS RUNS HERE, NOT FROM CLAUDE:
Claude's cloud/device shell traffic is routed through an organization egress
proxy that does not currently allow mvsu.instructure.com, so Claude can't
execute this script itself. Run it yourself from a normal terminal on a
machine with real network access to Canvas (the same one the rest of this
pipeline already uses), then hand the resulting JSON file back to Claude.

Usage:
    python canvas_login_activity_pull.py --sis-term-id 202710
    python canvas_login_activity_pull.py --sis-term-id 202710 --pattern "E0\\d,DE"

Requires output/canvas_terms.json and output/canvas_courses_<sis_term_id>.json
to already exist (run canvas_terms.py and canvas_courses.py <sis_term_id> first,
or this script will fetch/refresh both automatically, same as
canvas_analytics_pull.py).

Configuration is read from .env via config.py (same CANVAS_URL / CANVAS_TOKEN /
CANVAS_ACCOUNT_ID used by the rest of this pipeline).

Output:
    output/analytics/<sis_term_id>/enrollment_activity.json
        one row per enrollment: course_id, course_code, course_name,
        enrollment_type (StudentEnrollment / TeacherEnrollment / TaEnrollment),
        enrollment_state, user_id, user_name, sis_user_id, login_id,
        last_activity_at (null if never active in that course),
        total_activity_time (seconds, Canvas's own tracked total)
"""

import argparse
import datetime
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

import config

logger = logging.getLogger(__name__)

DEFAULT_PATTERN = r"(E0\d|DE)"
ENROLLMENT_TYPES = ["StudentEnrollment", "TeacherEnrollment", "TaEnrollment"]


def load_json(path: Path):
    with open(path) as f:
        return json.load(f)


def save_json(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def ensure_terms_and_courses(sis_term_id: str) -> tuple[Path, Path]:
    output_dir = Path(config.OUTPUT_DIR)
    terms_path = output_dir / "canvas_terms.json"
    courses_path = output_dir / f"canvas_courses_{sis_term_id}.json"

    if not terms_path.exists():
        logger.info("canvas_terms.json not found — running canvas_terms.py")
        subprocess.run([sys.executable, "canvas_terms.py"], check=True)

    if not courses_path.exists():
        logger.info("%s not found — running canvas_courses.py %s", courses_path.name, sis_term_id)
        subprocess.run([sys.executable, "canvas_courses.py", sis_term_id], check=True)

    return terms_path, courses_path


def resolve_term_id(sis_term_id: str, terms_path: Path) -> int:
    terms = load_json(terms_path)
    for t in terms:
        if str(t.get("sis_term_id")) == str(sis_term_id):
            return t["id"]
    raise ValueError(f"sis_term_id '{sis_term_id}' not found in {terms_path}.")


def select_online_courses(courses_path: Path, pattern: str) -> list:
    rx = re.compile(pattern, re.IGNORECASE)
    courses = load_json(courses_path)
    matched = [
        c for c in courses
        if rx.search(c.get("course_code", "") or "") or rx.search(c.get("name", "") or "")
    ]
    return matched


def fetch_course_enrollments(course_id: int, headers: dict) -> list:
    url = f"{config.CANVAS_URL}/api/v1/courses/{course_id}/enrollments"
    params = {
        "per_page": 100,
        "include[]": "user",
        "type[]": ENROLLMENT_TYPES,
        "state[]": ["active", "invited", "completed", "inactive"],
    }
    all_rows = []
    first = True
    while url:
        if first:
            response = requests.get(url, headers=headers, params=params)
            first = False
        else:
            response = requests.get(url, headers=headers)
        response.raise_for_status()
        all_rows.extend(response.json())
        url = response.links.get("next", {}).get("url")
    return all_rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Pull enrollment-level last-activity data (students + instructors) for online/DE courses.")
    parser.add_argument("--sis-term-id", required=True, help="Banner term code, e.g. 202710")
    parser.add_argument(
        "--pattern", default=DEFAULT_PATTERN,
        help=r"Regex used against course_code/name to select online/DE sections (default: E0\d or DE)",
    )
    parser.add_argument(
        "--sleep", type=float, default=0.25,
        help="Seconds to sleep between course-level API calls (default: 0.25).",
    )
    args = parser.parse_args()

    terms_path, courses_path = ensure_terms_and_courses(args.sis_term_id)
    enrollment_term_id = resolve_term_id(args.sis_term_id, terms_path)
    logger.info("Resolved sis_term_id=%s -> Canvas enrollment_term_id=%s", args.sis_term_id, enrollment_term_id)

    matched = select_online_courses(courses_path, args.pattern)
    logger.info("Matched %d online/DE courses out of the term's full course list", len(matched))

    out_dir = Path(config.OUTPUT_DIR) / "analytics" / args.sis_term_id

    headers = {"Authorization": f"Bearer {config.CANVAS_TOKEN}"}
    start = time.time()

    rows = []
    failed = []
    for i, c in enumerate(matched, start=1):
        cid = c["id"]
        logger.info("[%d/%d] course %s — %s (%s)", i, len(matched), cid, c.get("course_code"), c.get("name"))
        try:
            enrollments = fetch_course_enrollments(cid, headers)
            for e in enrollments:
                user = e.get("user", {}) or {}
                rows.append({
                    "course_id": cid,
                    "course_code": c.get("course_code"),
                    "course_name": c.get("name"),
                    "enrollment_id": e.get("id"),
                    "enrollment_type": e.get("type"),
                    "enrollment_state": e.get("enrollment_state"),
                    "user_id": e.get("user_id"),
                    "user_name": user.get("name") or user.get("short_name"),
                    "sis_user_id": user.get("sis_user_id"),
                    "login_id": user.get("login_id"),
                    "last_activity_at": e.get("last_activity_at"),
                    "total_activity_time": e.get("total_activity_time"),
                })
        except requests.exceptions.HTTPError as e:
            logger.warning("  failed for course %s: %s", cid, e)
            failed.append({"course_id": cid, "course_code": c.get("course_code"), "error": str(e)})
        time.sleep(args.sleep)

    out = {
        "fetched_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "sis_term_id": args.sis_term_id,
        "enrollment_term_id": enrollment_term_id,
        "pattern": args.pattern,
        "matched_course_count": len(matched),
        "failed_course_count": len(failed),
        "failed_courses": failed,
        "enrollments": rows,
    }
    save_json(out, out_dir / "enrollment_activity.json")

    logger.info(
        "Done in %.1fs — %d enrollment rows across %d/%d courses. Send %s to Claude.",
        time.time() - start, len(rows), len(matched) - len(failed), len(matched),
        out_dir / "enrollment_activity.json",
    )


if __name__ == "__main__":
    main()
