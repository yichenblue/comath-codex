#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

from lib import (
    ValidationError,
    load_status,
    mark_status_blocked,
    read_json,
    readiness_errors,
    repo_root,
    run_workstream_tests,
    scaffold_lock,
    status_path,
    test_run_path,
    write_json,
)


def update_project_state(workstream_id: str) -> None:
    with scaffold_lock():
        root = repo_root()
        project_state_path = root / "state" / "project_state.json"
        project_state = read_json(project_state_path)
        for item in project_state.get("workstreams", []):
            if item.get("id") == workstream_id:
                item["status"] = "COMPLETE"
        write_json(project_state_path, project_state)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: python3 scripts/promote_workstream.py workstreams/<workstream-id>")
        return 2

    workstream = Path(argv[1]).resolve()
    try:
        status = load_status(workstream)
        if status.get("status") == "COMPLETE":
            print(f"Workstream {status['id']} is already COMPLETE.")
            return 0

        if status.get("tests_required") is True:
            if status.get("status") not in {"APPROVED", "RUNNING", "REVIEWING"}:
                errors = readiness_errors(workstream)
                print(f"Refusing to promote workstream: {workstream}")
                for error in errors:
                    print(f"- {error}")
                return 1

            test_result = run_workstream_tests(workstream, update_status=True)
            status = load_status(workstream)
            if test_result.get("passed") is not True:
                evidence = (
                    f"Promotion test run failed. "
                    f"test_run={test_run_path(workstream).relative_to(workstream)}, "
                    f"exit_code={test_result.get('exit_code')}, "
                    f"reason={test_result.get('reason')}"
                )
                failure_note = mark_status_blocked(
                    workstream,
                    status,
                    "Promotion test run failed.",
                    "Reran computation tests during promotion.",
                    evidence,
                    "Fix the computation workstream, rerun tests, then restart with start_workstream.py --resume-blocked.",
                    event_type="computation_tests_failed",
                )
                print(f"Refusing to promote workstream: {workstream}")
                print("- computation tests failed during promotion")
                print(f"- recorded test run: {test_run_path(workstream)}")
                print(f"- recorded failed exploration: {failure_note}")
                return 1

        errors = readiness_errors(workstream)
    except ValidationError as exc:
        print(exc)
        return 1

    if errors:
        print(f"Refusing to promote workstream: {workstream}")
        for error in errors:
            print(f"- {error}")
        return 1

    with scaffold_lock():
        status = read_json(status_path(workstream))
        status["status"] = "COMPLETE"
        status["review_passed"] = True
        status["finalized"] = True
        write_json(status_path(workstream), status)
        update_project_state(status["id"])
    print(f"Promoted {status['id']} to COMPLETE.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
