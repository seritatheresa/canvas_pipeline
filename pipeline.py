"""
pipeline.py — Main entry point for the ValleyPROD → Canvas provisioning pipeline.

Usage:
    python pipeline.py --term 202620
    python pipeline.py --term 202620 --dry-run
    python pipeline.py --term 202620 --skip-upload   # extract + CSV only
    python pipeline.py --term 202620 --csv-only      # same as --skip-upload

The script:
  1. Reads credentials from .env (via config.py)
  2. Connects to ValleyPROD Oracle and runs the three parameterized queries
  3. Transforms the results into Canvas SIS CSV files
  4. Zips and uploads to Canvas SIS Import API
  5. Polls until the import finishes and logs the outcome
  6. Writes a timestamped log file to ./output/<term>/pipeline_<timestamp>.log

Exit codes:
    0  — success (or dry-run)
    1  — import failed / aborted
    2  — configuration or connection error
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import config  # sets up root logging
from valleyprod import ValleyPRODExtract
from canvas_import import CanvasSISImporter
from transform import write_csvs

logger = logging.getLogger("pipeline")


# ── Argument parsing ──────────────────────────────────────────────────────────

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ValleyPROD → Canvas SIS provisioning pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--term", "-t",
        required=True,
        help="ValleyPROD term code, e.g. 202620",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build CSVs and ZIP but do NOT upload to Canvas.",
    )
    parser.add_argument(
        "--skip-upload", "--csv-only",
        action="store_true",
        dest="skip_upload",
        help="Extract from ValleyPROD and write CSVs, but skip the Canvas upload.",
    )
    parser.add_argument(
        "--output-dir",
        default=config.OUTPUT_DIR,
        help=f"Directory for generated files (default: {config.OUTPUT_DIR})",
    )
    return parser.parse_args(argv)


# ── File handler for per-run log ──────────────────────────────────────────────

def _add_file_handler(term_code: str, output_dir: str):
    log_dir = Path(output_dir) / term_code
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path  = log_dir / f"pipeline_{timestamp}.log"

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logging.getLogger().addHandler(fh)
    logger.info("Log file: %s", log_path)


# ── Main ──────────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    args = parse_args(argv)
    term = args.term.strip()

    _add_file_handler(term, args.output_dir)

    logger.info("=" * 60)
    logger.info("Canvas Provisioning Pipeline")
    logger.info("Term:        %s", term)
    logger.info("Canvas URL:  %s", config.CANVAS_URL)
    logger.info("Dry run:     %s", args.dry_run or args.skip_upload)
    logger.info("=" * 60)

    start = time.time()

    # ── Step 1: Extract from ValleyPROD ──────────────────────────────────────
    try:
        with ValleyPRODExtract() as valleyprod:
            logger.info("Step 1/3 — Extracting from ValleyPROD …")
            students_df    = valleyprod.get_students(term)
            enrollments_df = valleyprod.get_enrollments(term)
            courses_df     = valleyprod.get_courses(term)
    except Exception as exc:
        logger.error("ValleyPROD extraction failed: %s", exc, exc_info=True)
        return 2

    if students_df.empty:
        logger.error("No students found for term %s. Aborting.", term)
        return 2

    # ── Step 2: Transform to Canvas CSVs ──────────────────────────────────────
    logger.info("Step 2/3 — Building Canvas SIS CSV files …")

    # Load existing Canvas courses for this term so we can skip re-importing them.
    canvas_courses_path = Path(args.output_dir) / f"canvas_courses_{term}.json"
    existing_canvas_course_ids: set | None = None
    if canvas_courses_path.exists():
        with open(canvas_courses_path) as f:
            canvas_courses = json.load(f)
        existing_canvas_course_ids = {
            c["sis_course_id"] for c in canvas_courses if c.get("sis_course_id")
        }
        logger.info(
            "Loaded %d existing Canvas course IDs from %s",
            len(existing_canvas_course_ids), canvas_courses_path,
        )
    else:
        logger.warning(
            "canvas_courses_%s.json not found — courses.csv will include all courses. "
            "Run canvas_courses.py %s first to enable filtering.",
            term, term,
        )

    # Load existing Canvas users so we can skip re-importing them.
    canvas_users_path = Path(args.output_dir) / "canvas_users.json"
    existing_canvas_user_ids: set | None = None
    if canvas_users_path.exists():
        with open(canvas_users_path) as f:
            canvas_users = json.load(f)
        existing_canvas_user_ids = {
            u["sis_user_id"] for u in canvas_users if u.get("sis_user_id")
        }
        logger.info(
            "Loaded %d existing Canvas user IDs from %s",
            len(existing_canvas_user_ids), canvas_users_path,
        )
    else:
        logger.warning(
            "canvas_users.json not found — users.csv will include all users. "
            "Run canvas_users.py first to enable filtering.",
        )

    try:
        csv_files = write_csvs(
            students_df=students_df,
            enrollments_df=enrollments_df,
            courses_df=courses_df,
            output_dir=args.output_dir,
            term_code=term,
            existing_canvas_course_ids=existing_canvas_course_ids,
            existing_canvas_user_ids=existing_canvas_user_ids,
        )
    except Exception as exc:
        logger.error("CSV generation failed: %s", exc, exc_info=True)
        return 2

    logger.info("CSV files written to: %s/%s/", args.output_dir, term)
    for name in csv_files:
        logger.info("  %s", name)

    if args.skip_upload:
        logger.info("--skip-upload set — stopping before Canvas upload.")
        logger.info("Done in %.1f seconds.", time.time() - start)
        return 0

    # ── Step 3: Upload to Canvas ──────────────────────────────────────────────
    logger.info("Step 3/3 — Uploading to Canvas SIS Import API …")
    try:
        importer = CanvasSISImporter()
        result   = importer.run(csv_files, term_code=term, dry_run=args.dry_run)
    except Exception as exc:
        logger.error("Canvas upload failed: %s", exc, exc_info=True)
        return 2

    # ── Outcome ───────────────────────────────────────────────────────────────
    elapsed = time.time() - start
    state   = result.get("workflow_state", "dry_run")

    if args.dry_run:
        logger.info("Dry run complete in %.1f seconds.", elapsed)
        return 0

    if state in ("imported", "imported_with_messages"):
        logger.info("Pipeline SUCCEEDED in %.1f seconds. (state=%s)", elapsed, state)
        return 0
    else:
        logger.error("Pipeline FAILED in %.1f seconds. (state=%s)", elapsed, state)
        return 1


if __name__ == "__main__":
    sys.exit(main())
