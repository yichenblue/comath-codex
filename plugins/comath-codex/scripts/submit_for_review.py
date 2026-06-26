#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from lib import (
    ValidationError,
    formula_trace_errors,
    append_message,
    load_status,
    raw_object_validation_errors,
    report_has_required_sections,
    report_path,
    scaffold_lock,
    source_faithfulness_test_errors,
    source_setting_manifest_errors,
    status_path,
    sync_project_workstream_status,
    validate_or_raise_status,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move a workstream into REVIEWING.")
    parser.add_argument("workstream", help="Path like workstreams/ws_001_literature.")
    parser.add_argument(
        "--allow-tbd",
        action="store_true",
        help="Allow unresolved TBD markers when submitting for early review.",
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
            if current not in {"RUNNING", "APPROVED"}:
                raise ValidationError(f"Cannot submit for review from status {current!r}; use RUNNING or APPROVED.")
            if status.get("blocked_reason"):
                raise ValidationError("Cannot submit for review while blocked_reason is set.")

            report_errors = report_has_required_sections(report_path(workstream))
            report_errors.extend(source_setting_manifest_errors(workstream, status))
            report_errors.extend(source_faithfulness_test_errors(workstream, status))
            report_errors.extend(formula_trace_errors(workstream, status))
            report_errors.extend(raw_object_validation_errors(workstream, status))
            if args.allow_tbd:
                report_errors = [error for error in report_errors if "contains unresolved TBD markers" not in error]
            if report_errors:
                raise ValidationError("\n".join(report_errors))

            current_round = status["current_review_round"]
            max_rounds = status["max_review_rounds"]
            if current_round >= max_rounds:
                raise ValidationError(
                    f"Review round limit reached ({current_round}/{max_rounds}). "
                    "Use mark_blocked.py to escalate."
                )

            status["status"] = "REVIEWING"
            status["current_review_round"] = current_round + 1
            status["review_passed"] = False
            write_json(status_path(workstream), status)
            sync_project_workstream_status(status["id"], "REVIEWING")
            append_message(
                "workstream_submitted_for_review",
                status["id"],
                f"Workstream {status['id']} submitted for review round {status['current_review_round']}.",
                review_round=status["current_review_round"],
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Submitted {status['id']} for review round {status['current_review_round']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
