#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from lib import (
    ValidationError,
    load_status,
    mark_status_blocked,
    status_path,
    validate_or_raise_status,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mark a workstream BLOCKED and record a failure note.")
    parser.add_argument("workstream", help="Path like workstreams/ws_001_literature.")
    parser.add_argument("--reason", required=True, help="Short blocker reason.")
    parser.add_argument(
        "--attempted-strategy",
        default="Not specified.",
        help="Strategy that led to the blocker.",
    )
    parser.add_argument(
        "--evidence",
        default="Not specified.",
        help="Review, test, or artifact evidence for the blocker.",
    )
    parser.add_argument(
        "--next-action",
        default="Escalate to the project coordinator or user.",
        help="Recommended next action.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    workstream = Path(args.workstream).resolve()

    try:
        status = load_status(workstream)
        validate_or_raise_status(status, status_path(workstream))
        failure_note = mark_status_blocked(
            workstream,
            status,
            args.reason,
            args.attempted_strategy,
            args.evidence,
            args.next_action,
        )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Marked {status['id']} BLOCKED.")
    print(f"Failure note: {failure_note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
