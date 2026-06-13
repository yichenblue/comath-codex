#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

from lib import ValidationError, readiness_errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python3 scripts/check_report_ready.py workstreams/<workstream-id>")
        return 2

    workstream = Path(argv[1]).resolve()
    try:
        errors = readiness_errors(workstream)
    except ValidationError as exc:
        print(exc)
        return 1

    if errors:
        print(f"Workstream is not ready: {workstream}")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Workstream is ready for promotion: {workstream}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

