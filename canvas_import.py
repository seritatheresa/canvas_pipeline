"""
canvas_import.py — Canvas SIS Import API uploader.

Bundles the generated CSV files into a ZIP archive, POSTs it to the
Canvas SIS Import endpoint, and polls until the import reaches a
terminal state (imported / failed / aborted).

Canvas API reference:
    POST  /api/v1/accounts/:account_id/sis_imports
    GET   /api/v1/accounts/:account_id/sis_imports/:id

Usage:
    from canvas_import import CanvasSISImporter
    importer = CanvasSISImporter()
    result = importer.run(csv_files, term_code="202620", dry_run=False)
"""

import io
import json
import logging
import time
import zipfile
from pathlib import Path
from typing import Optional

import requests

import config

logger = logging.getLogger(__name__)

# Terminal states reported by Canvas
TERMINAL_STATES = {"imported", "imported_with_messages", "failed", "aborted", "failed_with_messages"}

# How often to poll Canvas for import status (seconds)
POLL_INTERVAL = 10

# Maximum total wait time before giving up (seconds)
MAX_WAIT = 3600  # 1 hour


class CanvasSISImporter:
    """Handles zipping CSVs and uploading them via the Canvas SIS Import API."""

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {config.CANVAS_TOKEN}",
        })
        self._base = config.CANVAS_URL
        self._account = config.CANVAS_ACCOUNT_ID

    # ── Public API ────────────────────────────────────────────────────────────

    # ── Term lookup ───────────────────────────────────────────────────────────

    @staticmethod
    def _canvas_term_id(term_code: str) -> str:
        """Return the Canvas numeric term id for a given banner term code."""
        terms_path = Path(config.OUTPUT_DIR) / "canvas_terms.json"
        with open(terms_path, encoding="utf-8") as fh:
            terms = json.load(fh)
        for term in terms:
            if term.get("sis_term_id") == str(term_code):
                return str(term["id"])
        raise ValueError(
            f"No Canvas term found for banner term code '{term_code}' "
            f"in {terms_path}"
        )

    # ── Public API ────────────────────────────────────────────────────────────

    def run(
        self,
        csv_files: dict[str, Path],
        term_code: str,
        dry_run: bool = False,
    ) -> dict:
        """
        Zip the CSVs and submit them as a Canvas SIS Import.

        Args:
            csv_files:  {filename: Path} mapping returned by transform.write_csvs()
            term_code:  The ValleyPROD term code string, used for the ZIP filename.
            dry_run:    If True, build the ZIP but do NOT upload. Useful for testing.

        Returns:
            The final Canvas SIS import object (dict) or a dry-run summary.
        """
        zip_path = self._build_zip(csv_files, term_code)
        canvas_term_id = self._canvas_term_id(term_code)
        use_batch_mode = self._should_use_batch_mode(csv_files)
        logger.info("Resolved banner term %s → Canvas term id %s", term_code, canvas_term_id)
        logger.info("Upload mode: %s", "batch" if use_batch_mode else "standard")

        # Batch mode deletes any enrollment in the term that is missing from
        # this file — a truncated Banner extract could mass-drop students.
        # Refuse to proceed if the dataset shrank suspiciously vs the last run.
        if use_batch_mode and not dry_run:
            self._check_batch_safety(csv_files, term_code)

        if dry_run:
            logger.info("[DRY RUN] Would upload %s to Canvas — skipping.", zip_path)
            return {
                "dry_run": True,
                "zip": str(zip_path),
                "csv_files": list(csv_files.keys()),
                "batch_mode": use_batch_mode,
                "batch_mode_term_id": canvas_term_id if use_batch_mode else None,
            }

        import_id = self._submit(zip_path, canvas_term_id, use_batch_mode)
        result = self._poll(import_id)
        self._log_result(result)

        # Record row counts for the next run's batch-mode safety check
        if result.get("workflow_state") in ("imported", "imported_with_messages"):
            self._save_batch_stats(csv_files, term_code)

        return result

    @staticmethod
    def _should_use_batch_mode(csv_files: dict[str, Path]) -> bool:
        """
        Use Canvas batch mode only when course and section CSVs were skipped.

        In this pipeline, missing courses.csv / sections.csv means those
        DataFrames were empty and intentionally not written.
        """
        return "courses.csv" not in csv_files and "sections.csv" not in csv_files

    # ── Batch-mode safety guard ───────────────────────────────────────────────

    # Abort a batch-mode upload if enrollments.csv shrank more than this
    # fraction versus the last successful import. Override the threshold with
    # BATCH_MODE_MAX_SHRINK_PCT (0–100) or bypass entirely for one run with
    # BATCH_MODE_FORCE=true (e.g. after finals, when mass drops are real).
    _DEFAULT_MAX_SHRINK_PCT = 20.0

    @staticmethod
    def _csv_row_count(path: Path) -> int:
        """Data rows in a CSV (excludes the header line)."""
        with open(path, encoding="utf-8") as fh:
            return max(sum(1 for _ in fh) - 1, 0)

    @staticmethod
    def _stats_path(term_code: str) -> Path:
        return Path(config.OUTPUT_DIR) / term_code / "batch_guard.json"

    def _check_batch_safety(self, csv_files: dict[str, Path], term_code: str) -> None:
        """Raise RuntimeError instead of letting a suspicious batch import run."""
        import os

        if os.environ.get("BATCH_MODE_FORCE", "").lower() == "true":
            logger.warning("BATCH_MODE_FORCE=true — batch-mode safety check bypassed.")
            return

        enrollments = csv_files.get("enrollments.csv")
        if enrollments is None:
            raise RuntimeError(
                "Batch-mode import with no enrollments.csv would delete every "
                "enrollment in the term. Aborting. If this is intentional, set "
                "BATCH_MODE_FORCE=true for this run."
            )

        current = self._csv_row_count(enrollments)
        stats_path = self._stats_path(term_code)
        if not stats_path.exists():
            logger.info(
                "No previous batch stats at %s — first batch run for this "
                "term, skipping shrink check (%d enrollment rows).",
                stats_path, current,
            )
            return

        with open(stats_path, encoding="utf-8") as fh:
            previous = json.load(fh).get("enrollment_rows", 0)

        threshold = float(
            os.environ.get("BATCH_MODE_MAX_SHRINK_PCT", self._DEFAULT_MAX_SHRINK_PCT)
        )
        if previous > 0:
            shrink_pct = (previous - current) / previous * 100
            if shrink_pct > threshold:
                raise RuntimeError(
                    f"Batch-mode safety check failed: enrollments.csv has "
                    f"{current} rows vs {previous} in the last successful "
                    f"import ({shrink_pct:.1f}% shrink > {threshold:.0f}% "
                    f"threshold). A truncated Banner extract would mass-drop "
                    f"students in Canvas. Verify the extract; if the shrink "
                    f"is legitimate, re-run with BATCH_MODE_FORCE=true."
                )
        logger.info(
            "Batch-mode safety check passed: %d rows now vs %d previously.",
            current, previous,
        )

    def _save_batch_stats(self, csv_files: dict[str, Path], term_code: str) -> None:
        enrollments = csv_files.get("enrollments.csv")
        if enrollments is None:
            return
        stats_path = self._stats_path(term_code)
        stats_path.parent.mkdir(parents=True, exist_ok=True)
        with open(stats_path, "w", encoding="utf-8") as fh:
            json.dump({
                "enrollment_rows": self._csv_row_count(enrollments),
                "term": term_code,
            }, fh)
        logger.debug("Saved batch guard stats to %s", stats_path)

    # ── ZIP builder ───────────────────────────────────────────────────────────

    def _build_zip(self, csv_files: dict[str, Path], term_code: str) -> Path:
        """Create a ZIP archive containing all CSV files."""
        zip_dir  = csv_files[next(iter(csv_files))].parent
        zip_path = zip_dir / f"sis_import_{term_code}.zip"

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, path in csv_files.items():
                zf.write(path, arcname=name)
                logger.debug("  → zipped %s", name)

        size_kb = zip_path.stat().st_size / 1024
        logger.info("Created ZIP: %s (%.1f KB)", zip_path, size_kb)
        return zip_path

    # ── Upload ────────────────────────────────────────────────────────────────

    def _submit(self, zip_path: Path, canvas_term_id: str, use_batch_mode: bool) -> str:
        """POST the ZIP to Canvas and return the import ID."""
        url = f"{self._base}/api/v1/accounts/{self._account}/sis_imports"

        payload = {
            "import_type": "instructure_csv",
        }
        if use_batch_mode:
            payload.update({
                "batch_mode": "true",
                "batch_mode_term_id": canvas_term_id,
            })

        logger.info("Submitting SIS import to %s …", url)
        with open(zip_path, "rb") as f:
            response = self._session.post(
                url,
                files={"attachment": (zip_path.name, f, "application/zip")},
                data=payload,
                timeout=120,
            )

        if response.status_code not in (200, 201):
            logger.error("Canvas returned HTTP %s: %s", response.status_code, response.text[:500])
            response.raise_for_status()

        data = response.json()
        import_id = str(data["id"])
        logger.info("SIS import accepted — import_id=%s", import_id)
        return import_id

    # ── Polling ───────────────────────────────────────────────────────────────

    def _poll(self, import_id: str) -> dict:
        """Poll Canvas until the import reaches a terminal state."""
        url = (
            f"{self._base}/api/v1/accounts/{self._account}"
            f"/sis_imports/{import_id}"
        )
        elapsed = 0

        while elapsed < MAX_WAIT:
            time.sleep(POLL_INTERVAL)
            elapsed += POLL_INTERVAL

            try:
                resp = self._session.get(url, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except requests.RequestException as exc:
                logger.warning("Polling error (will retry): %s", exc)
                continue

            state = data.get("workflow_state", "")
            progress = data.get("progress", 0)
            logger.info(
                "  import_id=%s  state=%-30s  progress=%s%%",
                import_id, state, progress,
            )

            if state in TERMINAL_STATES:
                return data

        logger.error("SIS import timed out after %d seconds.", MAX_WAIT)
        return {"workflow_state": "timeout", "id": import_id}

    # ── Result logging ────────────────────────────────────────────────────────

    def _log_result(self, result: dict):
        state = result.get("workflow_state", "unknown")
        counts = result.get("data", {}).get("counts", {})

        if state in ("imported", "imported_with_messages"):
            logger.info("✓ SIS import completed successfully (state=%s)", state)
            if counts:
                for entity, n in counts.items():
                    if n:
                        logger.info("    %-20s %d", entity, n)
        else:
            logger.error("✗ SIS import ended with state=%s", state)

        # Surface any processing warnings/errors
        processing_errors = result.get("processing_errors", [])
        processing_warnings = result.get("processing_warnings", [])
        for err in processing_errors[:20]:
            logger.error("  Canvas error: %s", err)
        for warn in processing_warnings[:20]:
            logger.warning("  Canvas warning: %s", warn)
