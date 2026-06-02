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

        if dry_run:
            logger.info("[DRY RUN] Would upload %s to Canvas — skipping.", zip_path)
            return {"dry_run": True, "zip": str(zip_path), "csv_files": list(csv_files.keys())}

        import_id = self._submit(zip_path)
        result = self._poll(import_id)
        self._log_result(result)
        return result

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

    def _submit(self, zip_path: Path) -> str:
        """POST the ZIP to Canvas and return the import ID."""
        url = f"{self._base}/api/v1/accounts/{self._account}/sis_imports"

        logger.info("Submitting SIS import to %s …", url)
        with open(zip_path, "rb") as f:
            response = self._session.post(
                url,
                files={"attachment": (zip_path.name, f, "application/zip")},
                data={"import_type": "instructure_csv"},
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
