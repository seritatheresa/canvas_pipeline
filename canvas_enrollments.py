"""
canvas_enrollments.py - List course enrollments and accept enrollment invitations.

Examples:
  python canvas_enrollments.py list --course-id 10284 --section-id 10721
  python canvas_enrollments.py accept --course-id 10284 --enrollment-id 123456
  python canvas_enrollments.py accept --course-id 10284 --enrollment-id 123456 --method PUT
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from typing import Any

import requests
import pandas as pd

import config

logger = logging.getLogger(__name__)


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {config.CANVAS_TOKEN}",
            "Accept": "application/json",
        }
    )
    return session


def _fetch_enrollments(
    session: requests.Session,
    course_id: int,
    section_id: int | None = None,
    per_page: int = 100,
) -> pd.DataFrame:
    url = f"{config.CANVAS_URL}/api/v1/courses/{course_id}/enrollments"
    params: dict[str, Any] = {
        "per_page": per_page,
        "include[]": ["user"],
    }

    all_rows: list[dict[str, Any]] = []
    while url:
        response = session.get(url, params=params, timeout=30)
        response.raise_for_status()
        rows = response.json()
        all_rows.extend(rows)

        next_url = response.links.get("next", {}).get("url")
        url = next_url if next_url else ""
        # Query params should only be sent on the first request;
        # Canvas next links already include pagination params.
        params = {}

    df = pd.json_normalize(all_rows, sep=".")
    if not df.empty:
        df.rename(columns={"id": "enrollment_id", "user.name": "user_name"}, inplace=True)

    if section_id is not None and not df.empty:
        section_values = pd.to_numeric(df.get("course_section_id"), errors="coerce")
        df = df[section_values == section_id]

    return df.reset_index(drop=True)


def _save_json(data: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data.to_dict(orient="records"), f, indent=2, default=str)


def _accept_invitation(
    session: requests.Session,
    course_id: int,
    enrollment_id: int,
    method: str = "POST",
) -> dict[str, Any]:
    method = method.upper().strip()
    url = f"{config.CANVAS_URL}/api/v1/courses/{course_id}/enrollments/{enrollment_id}/accept"

    if method == "POST":
        response = session.post(url, timeout=30)
    elif method == "PUT":
        response = session.put(url, timeout=30)
    else:
        raise ValueError("method must be POST or PUT")

    response.raise_for_status()
    return response.json()


def _cmd_list(args: argparse.Namespace) -> int:
    session = _session()
    enrollments_df = _fetch_enrollments(
        session=session,
        course_id=args.course_id,
        section_id=args.section_id,
        per_page=args.per_page,
    )

    logger.info(
        "Found %d enrollments in course %s%s",
        len(enrollments_df),
        args.course_id,
        f" section {args.section_id}" if args.section_id else "",
    )

    if args.out:
        out_path = Path(args.out)
    else:
        if args.section_id:
            out_path = Path(config.OUTPUT_DIR) / f"course_{args.course_id}_section_{args.section_id}_enrollments.json"
        else:
            out_path = Path(config.OUTPUT_DIR) / f"course_{args.course_id}_enrollments.json"

    _save_json(enrollments_df, out_path)
    logger.info("Saved enrollments JSON to %s", out_path)

    # Print a compact preview for convenience.
    preview_columns = [
        column
        for column in [
            "enrollment_id",
            "user_id",
            "course_section_id",
            "type",
            "enrollment_state",
            "invitation_accepted",
            "user_name",
        ]
        if column in enrollments_df.columns
    ]
    preview_df = enrollments_df[preview_columns].head(10) if preview_columns else enrollments_df.head(10)
    print(preview_df.to_json(orient="records", indent=2, default_handler=str))
    if len(enrollments_df) > 10:
        print(f"... ({len(enrollments_df) - 10} more rows)")

    return 0


def _cmd_accept(args: argparse.Namespace) -> int:
    session = _session()

    try:
        data = _accept_invitation(
            session=session,
            course_id=args.course_id,
            enrollment_id=args.enrollment_id,
            method=args.method,
        )
    except requests.HTTPError as exc:
        body = exc.response.text if exc.response is not None else ""
        logger.error("Accept invitation failed (%s): %s", exc, body[:1000])
        return 1

    print(json.dumps(data, indent=2))
    logger.info(
        "Invitation accepted for course_id=%s enrollment_id=%s using %s",
        args.course_id,
        args.enrollment_id,
        args.method.upper(),
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="List Canvas course enrollments and accept enrollment invitations."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Fetch enrollments for a Canvas course (optionally by section).")
    p_list.add_argument("--course-id", type=int, required=True, help="Canvas course ID (e.g. 10284)")
    p_list.add_argument("--section-id", type=int, help="Canvas section ID (e.g. 10721)")
    p_list.add_argument("--per-page", type=int, default=100, help="Canvas page size (default: 100)")
    p_list.add_argument("--out", help="Optional output JSON file path")
    p_list.set_defaults(func=_cmd_list)

    p_accept = sub.add_parser("accept", help="Accept a course invitation for an enrollment id.")
    p_accept.add_argument("--course-id", type=int, required=True, help="Canvas course ID")
    p_accept.add_argument("--enrollment-id", type=int, required=True, help="Canvas enrollment ID to accept")
    p_accept.add_argument(
        "--method",
        choices=["POST", "PUT", "post", "put"],
        default="POST",
        help="HTTP method to use (default: POST).",
    )
    p_accept.set_defaults(func=_cmd_accept)

    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
