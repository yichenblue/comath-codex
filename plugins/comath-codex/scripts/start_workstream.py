#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from lib import (
    ValidationError,
    append_message,
    load_status,
    scaffold_lock,
    status_path,
    sync_project_workstream_status,
    validate_or_raise_status,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move a workstream into RUNNING.")
    parser.add_argument("workstream", help="Path like workstreams/ws_001_literature.")
    parser.add_argument(
        "--resume-blocked",
        action="store_true",
        help="Allow BLOCKED -> RUNNING and clear blocked_reason.",
    )
    parser.add_argument(
        "--revise",
        action="store_true",
        help="Allow REVIEWING -> RUNNING after reviewer-requested changes.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workstream = Path(args.workstream).resolve()

    try:
        with scaffold_lock():
            status = load_status(workstream)
            validate_or_raise_status(status, status_path(workstream))
            current = status["status"]

            allowed = {"DRAFT", "APPROVED"}
            if args.resume_blocked:
                allowed.add("BLOCKED")
            if args.revise:
                allowed.add("REVIEWING")

            if current not in allowed:
                raise ValidationError(
                    f"Cannot start workstream from status {current!r}. "
                    "Allowed statuses are DRAFT/APPROVED, plus BLOCKED with --resume-blocked "
                    "or REVIEWING with --revise."
                )
            if current == "COMPLETE":
                raise ValidationError("Cannot restart a COMPLETE workstream.")

            status["status"] = "RUNNING"
            if args.resume_blocked:
                status["blocked_reason"] = None
            write_json(status_path(workstream), status)
            sync_project_workstream_status(status["id"], "RUNNING")
            append_message(
                "workstream_started",
                status["id"],
                f"Workstream {status['id']} moved from {current} to RUNNING.",
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Started {status['id']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
