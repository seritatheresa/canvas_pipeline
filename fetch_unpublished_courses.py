#!/usr/bin/env python3
"""
fetch_unpublished_courses.py — Fetch unpublished courses and sections with July 7 start dates.

Automatically detects the current term, or specify a different term with --term.

Generates:
1. Daily briefing of all unpublished courses
2. Detailed report of courses with sections starting on July 7, 2026

Usage:
    python fetch_unpublished_courses.py              # Uses current term
    python fetch_unpublished_courses.py --term 202710  # Uses Summer 2026
    python fetch_unpublished_courses.py --term 202730  # Uses Fall 2026
"""

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path

import requests

import config

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

TARGET_DATE = datetime(2026, 7, 7).date()
END_DATE = datetime(2026, 7, 31).date()


def calculate_current_term():
    """Calculate the current Banner SIS term ID based on today's date.

    Banner term format: YYYYMM where MM is:
    - 10 = Fall
    - 20 = Spring
    - 30 = Summer

    Returns the SIS term ID string (e.g., "202710" for Summer 2026).
    """
    today = datetime.now()
    year = today.year
    month = today.month

    # Determine term based on month
    if month >= 8:  # August or later = Fall
        term_month = "10"
        term_year = year
    elif month >= 5:  # May-July = Summer
        term_month = "30"
        term_year = year
    else:  # January-April = Spring
        term_month = "20"
        term_year = year

    sis_term_id = f"{term_year}{term_month}"
    return sis_term_id


def resolve_term_id(sis_term_id: str) -> int:
    """Resolve SIS term ID to Canvas internal term ID.

    Reads output/canvas_terms.json (run canvas_terms.py first).
    Raises ValueError if the sis_term_id is not found.
    """
    terms_path = Path(config.OUTPUT_DIR) / "canvas_terms.json"
    if not terms_path.exists():
        logger.warning(
            f"Terms file not found: {terms_path}\n"
            "Run canvas_terms.py first to generate it, or the term lookup may fail."
        )
        return None

    with open(terms_path) as f:
        terms = json.load(f)

    for term in terms:
        if str(term.get("sis_term_id")) == str(sis_term_id):
            return term["id"]

    raise ValueError(
        f"sis_term_id '{sis_term_id}' not found in {terms_path}.\n"
        "Check the value or re-run canvas_terms.py to refresh the list."
    )


def fetch_unpublished_courses(enrollment_term_id: int):
    """Fetch all unpublished courses from Canvas for a specific enrollment term."""
    url = f"{config.CANVAS_URL}/api/v1/accounts/{config.CANVAS_ACCOUNT_ID}/courses"
    headers = {"Authorization": f"Bearer {config.CANVAS_TOKEN}"}

    params = {
        "per_page": 100,
        "workflow_state": "unpublished",
        "enrollment_term_id": enrollment_term_id,
        "include": ["sections", "term"]
    }

    all_courses = []
    page = 1

    logger.info(f"Fetching unpublished courses from Canvas (term ID: {enrollment_term_id})...")

    while True:
        params["page"] = page
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            response.raise_for_status()

            courses = response.json()
            if not courses:
                break

            all_courses.extend(courses)
            logger.info(f"  Fetched page {page} ({len(courses)} courses)")
            page += 1

            # Check for next link in headers
            if "next" not in response.links:
                break

        except requests.exceptions.RequestException as e:
            logger.error(f"Error fetching courses: {e}")
            break

    logger.info(f"Total unpublished courses found: {len(all_courses)}")
    return all_courses


def filter_courses_with_july7_sections(courses):
    """Filter courses that have sections starting on July 7, 2026."""
    matching_courses = []

    for course in courses:
        if "sections" not in course or not course["sections"]:
            continue

        matching_sections = []
        for section in course["sections"]:
            if "start_at" not in section or not section["start_at"]:
                continue

            try:
                start_date = datetime.fromisoformat(
                    section["start_at"].replace("Z", "+00:00")
                ).date()

                if start_date == TARGET_DATE:
                    matching_sections.append(section)
            except (ValueError, AttributeError):
                continue

        if matching_sections:
            course["matching_sections"] = matching_sections
            matching_courses.append(course)

    return matching_courses


def generate_briefing(courses):
    """Generate daily briefing data."""
    briefing = {
        "generated_at": datetime.now().isoformat(),
        "date": str(TARGET_DATE),
        "summary": {
            "total_unpublished_courses": len(courses),
            "courses_with_july7_sections": 0,
            "total_july7_sections": 0
        },
        "courses_by_department": {},
        "critical_items": []
    }

    july7_courses = filter_courses_with_july7_sections(courses)
    briefing["summary"]["courses_with_july7_sections"] = len(july7_courses)

    for course in july7_courses:
        briefing["summary"]["total_july7_sections"] += len(
            course.get("matching_sections", [])
        )

    # Group by account/department
    for course in courses:
        account_id = course.get("account_id", "Unknown")
        if account_id not in briefing["courses_by_department"]:
            briefing["courses_by_department"][account_id] = []

        briefing["courses_by_department"][account_id].append({
            "id": course["id"],
            "name": course["name"],
            "course_code": course.get("course_code", "N/A"),
            "workflow_state": course.get("workflow_state", "unknown")
        })

    # Critical items: courses with July 7 sections
    for course in july7_courses:
        briefing["critical_items"].append({
            "course_id": course["id"],
            "course_name": course["name"],
            "sections_starting_july7": len(course.get("matching_sections", [])),
            "section_names": [s.get("name", "Unnamed")
                            for s in course.get("matching_sections", [])]
        })

    return briefing


def generate_detailed_report(courses):
    """Generate detailed report of courses with July 7 sections."""
    july7_courses = filter_courses_with_july7_sections(courses)

    report = {
        "generated_at": datetime.now().isoformat(),
        "target_date": str(TARGET_DATE),
        "total_courses_in_report": len(july7_courses),
        "courses": []
    }

    for course in sorted(july7_courses, key=lambda c: c.get("name", "")):
        course_entry = {
            "id": course["id"],
            "name": course["name"],
            "course_code": course.get("course_code", "N/A"),
            "account_id": course.get("account_id"),
            "workflow_state": course.get("workflow_state"),
            "sis_course_id": course.get("sis_course_id"),
            "sections": []
        }

        for section in course.get("matching_sections", []):
            section_entry = {
                "id": section["id"],
                "name": section.get("name", "Unnamed"),
                "start_at": section.get("start_at"),
                "end_at": section.get("end_at"),
                "sis_section_id": section.get("sis_section_id")
            }
            course_entry["sections"].append(section_entry)

        report["courses"].append(course_entry)

    return report


def main():
    """Main execution."""
    parser = argparse.ArgumentParser(
        description="Fetch unpublished Canvas courses and generate briefing/report",
        epilog="Examples:\n"
               "  python fetch_unpublished_courses.py              # Current term\n"
               "  python fetch_unpublished_courses.py --term 202710  # Summer 2026\n"
               "  python fetch_unpublished_courses.py --term 202630  # Fall 2026"
    )
    parser.add_argument(
        "--term",
        type=str,
        default=None,
        help="SIS term ID (e.g., 202710 for Summer 2026). Defaults to current term."
    )
    args = parser.parse_args()

    # Determine term
    if args.term:
        sis_term_id = args.term
        logger.info(f"Using specified term: {sis_term_id}")
    else:
        sis_term_id = calculate_current_term()
        logger.info(f"Using current term (auto-detected): {sis_term_id}")

    # Resolve to Canvas term ID
    try:
        enrollment_term_id = resolve_term_id(sis_term_id)
        if enrollment_term_id is None:
            logger.warning(
                f"Could not resolve term {sis_term_id} from canvas_terms.json.\n"
                "Run canvas_terms.py first, or specify the enrollment_term_id directly.\n"
                "Proceeding with term lookup from Canvas API..."
            )
            # For now, we'd need the Canvas ID; this is a limitation
            return
    except ValueError as e:
        logger.error(str(e))
        return

    logger.info(f"Daily Briefing - Unpublished Courses Report")
    logger.info(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info(f"SIS Term ID: {sis_term_id} → Canvas Term ID: {enrollment_term_id}")
    logger.info(f"Target date for section starts: {TARGET_DATE}")
    logger.info("-" * 60)

    # Fetch courses
    courses = fetch_unpublished_courses(enrollment_term_id)

    if not courses:
        logger.warning("No unpublished courses found.")
        return

    # Generate briefing
    briefing = generate_briefing(courses)

    # Generate detailed report
    report = generate_detailed_report(courses)

    # Save files
    output_dir = Path(config.OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    briefing_path = output_dir / f"briefing_unpublished_courses_{sis_term_id}.json"
    report_path = output_dir / f"report_unpublished_courses_july7_{sis_term_id}.json"

    with open(briefing_path, "w") as f:
        json.dump(briefing, f, indent=2, default=str)
    logger.info(f"Briefing saved to: {briefing_path}")

    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    logger.info(f"Detailed report saved to: {report_path}")

    # Print summary
    logger.info("-" * 60)
    logger.info("BRIEFING SUMMARY")
    logger.info(f"Total unpublished courses: {briefing['summary']['total_unpublished_courses']}")
    logger.info(f"Courses with July 7 sections: {briefing['summary']['courses_with_july7_sections']}")
    logger.info(f"Total sections starting July 7: {briefing['summary']['total_july7_sections']}")

    if briefing["critical_items"]:
        logger.info("\nCRITICAL ITEMS (Courses with July 7 start sections):")
        for item in briefing["critical_items"][:10]:  # Show first 10
            logger.info(f"  • {item['course_name']} ({item['sections_starting_july7']} sections)")
            for section_name in item['section_names']:
                logger.info(f"    - {section_name}")


if __name__ == "__main__":
    main()
