#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

from lib import ValidationError, load_status, run_workstream_tests


def main(argv: list[str]) -> int:
    if len(argv) not in {2, 3}:
        print("Usage: python3 scripts/run_tests.py workstreams/<workstream-id> [--update-status]")
        return 2

    workstream = Path(argv[1]).resolve()
    update_status = len(argv) == 3 and argv[2] == "--update-status"
    if len(argv) == 3 and not update_status:
        print(f"Unknown option: {argv[2]}")
        return 2

    try:
        status = load_status(workstream)
    except ValidationError as exc:
        print(exc)
        return 1

    if status.get("tests_required") is not True:
        print(f"Tests are not required for {workstream.name}.")
        return 0

    result = run_workstream_tests(workstream, update_status=update_status)
    print(f"Test command: {result.get('command')}")
    print(f"Passed: {result['passed']}")
    if result.get("reason"):
        print(f"Reason: {result['reason']}")
    print(f"Recorded: {workstream / 'artifacts' / 'test_run.json'}")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
