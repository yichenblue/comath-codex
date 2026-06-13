#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

from lib import ValidationError, load_status, raw_object_validation_errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python3 scripts/validate_raw_object_validation.py workstreams/<workstream-id>")
        return 2

    workstream = Path(argv[1]).resolve()
    try:
        status = load_status(workstream)
        errors = raw_object_validation_errors(workstream, status)
    except ValidationError as exc:
        print(exc)
        return 1

    if errors:
        print(f"Raw-object validation gate failed: {workstream}")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Raw-object validation gate passed: {workstream}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
