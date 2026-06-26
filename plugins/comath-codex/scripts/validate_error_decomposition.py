#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

from lib import report_error_decomposition_errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python3 scripts/validate_error_decomposition.py workstreams/<workstream-id>|path/to/report.md")
        return 2

    target = Path(argv[1]).resolve()
    report = target / "report.md" if target.is_dir() else target
    errors = report_error_decomposition_errors(report)
    if errors:
        print(f"Error-decomposition gate failed: {report}")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Error-decomposition gate passed: {report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
