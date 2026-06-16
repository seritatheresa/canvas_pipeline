"""
update_section_dates.py - Update Canvas section start/end dates from a CSV file.

CSV columns used:
  canvas_section_id  - Canvas section ID
  sectionStartDate   - ISO 8601 start date (e.g. 2026-06-01T08:00:00-6:00)
  sectionEndDate     - ISO 8601 end date   (e.g. 2026-06-25T23:59:00-5:00)

Usage:
  python update_section_dates.py [--csv PATH] [--dry-run]
"""

import argparse
import csv
import logging
import sys
import time
from pathlib import Path

import requests

# Re-use existing config (CANVAS_URL and CANVAS_TOKEN from .env)
sys.path.insert(0, str(Path(__file__).parent))
import config  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_CSV = Path(__file__).parent.parent / "summer2dates.csv"


def update_section(session: requests.Session, section_id: str,
                   start_at: str, end_at: str, dry_run: bool) -> bool:
    """PUT /api/v1/sections/:id to update start/end dates.

    Returns True on success, False on failure.
    """
    url = f"{config.CANVAS_URL}/api/v1/sections/{section_id}"
    payload = {
        "course_section[start_at]": start_at,
        "course_section[end_at]": end_at,
    }

    if dry_run:
        logger.info("[DRY RUN] Would update section %s: start=%s end=%s",
                    section_id, start_at, end_at)
        return True

    try:
        response = session.put(url, data=payload, timeout=30)
        response.raise_for_status()
        logger.info("Updated section %s: start=%s end=%s",
                    section_id, start_at, end_at)
        return True
    except requests.HTTPError as exc:
        logger.error("HTTP %s for section %s: %s",
                     exc.response.status_code, section_id, exc.response.text)
        return False
    except requests.RequestException as exc:
        logger.error("Request failed for section %s: %s", section_id, exc)
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Update Canvas section dates from a CSV file.")
    parser.add_argument(
        "--csv", default=str(DEFAULT_CSV),
        help=f"Path to the CSV file (default: {DEFAULT_CSV})")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be updated without making API calls")
    parser.add_argument(
        "--delay", type=float, default=0.1,
        help="Seconds to wait between requests to avoid rate limiting (default: 0.1)")
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        logger.error("CSV file not found: %s", csv_path)
        sys.exit(1)

    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {config.CANVAS_TOKEN}",
        "Accept": "application/json",
    })

    success_count = 0
    failure_count = 0
    skipped_count = 0

    with csv_path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        # Validate required columns exist
        required_columns = {"canvas_section_id", "sectionStartDate", "sectionEndDate"}
        if not required_columns.issubset(set(reader.fieldnames or [])):
            missing = required_columns - set(reader.fieldnames or [])
            logger.error("CSV is missing required columns: %s", missing)
            sys.exit(1)

        for row_num, row in enumerate(reader, start=2):  # start=2 accounts for header row
            section_id = row.get("canvas_section_id", "").strip()
            start_at   = row.get("sectionStartDate", "").strip()
            end_at     = row.get("sectionEndDate", "").strip()

            if not section_id:
                logger.warning("Row %d: missing canvas_section_id, skipping", row_num)
                skipped_count += 1
                continue

            if not start_at and not end_at:
                logger.warning("Row %d (section %s): no dates provided, skipping",
                               row_num, section_id)
                skipped_count += 1
                continue

            ok = update_section(session, section_id, start_at, end_at, args.dry_run)
            if ok:
                success_count += 1
            else:
                failure_count += 1

            if args.delay > 0 and not args.dry_run:
                time.sleep(args.delay)

    logger.info("Done. Success: %d | Failed: %d | Skipped: %d",
                success_count, failure_count, skipped_count)

    if failure_count:
        sys.exit(1)


if __name__ == "__main__":
    main()
