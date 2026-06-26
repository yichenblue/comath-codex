#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

from lib import (
    ValidationError,
    goal_approval_gate_errors,
    read_json,
    repo_root,
    source_guard_errors,
    validate_status_object,
)


def main() -> int:
    root = repo_root()
    errors: list[str] = []

    project_state_path = root / "state" / "project_state.json"
    try:
        project_state = read_json(project_state_path)
    except ValidationError as exc:
        print(exc)
        return 1

    project_status = project_state.get("status")
    if project_status not in {"DRAFT", "APPROVED", "RUNNING", "BLOCKED", "COMPLETE"}:
        errors.append(f"{project_state_path}: invalid project status {project_status!r}")

    errors.extend(goal_approval_gate_errors(project_state, project_state_path))

    workstreams = project_state.get("workstreams", [])
    if not isinstance(workstreams, list):
        errors.append(f"{project_state_path}: workstreams must be a list")
        workstreams = []

    seen_ids: set[str] = set()
    for item in workstreams:
        if not isinstance(item, dict):
            errors.append(f"{project_state_path}: workstream entry must be an object")
            continue
        workstream_id = item.get("id")
        rel_path = item.get("path")
        if not isinstance(workstream_id, str) or not workstream_id:
            errors.append(f"{project_state_path}: workstream has missing id")
            continue
        if workstream_id in seen_ids:
            errors.append(f"{project_state_path}: duplicate workstream id {workstream_id}")
        seen_ids.add(workstream_id)
        if not isinstance(rel_path, str) or not rel_path:
            errors.append(f"{project_state_path}: workstream {workstream_id} has missing path")
            continue

        workstream_path = root / rel_path
        status_path = workstream_path / "status.json"
        try:
            status = read_json(status_path)
        except ValidationError as exc:
            errors.append(str(exc))
            continue

        errors.extend(validate_status_object(status, status_path))
        if status.get("id") != workstream_id:
            errors.append(f"{status_path}: id does not match project_state id {workstream_id}")
        if item.get("status") != status.get("status"):
            errors.append(
                f"{project_state_path}: status for {workstream_id} is {item.get('status')!r}, "
                f"but status.json is {status.get('status')!r}"
            )

    errors.extend(source_guard_errors())

    if errors:
        print("Status validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Status validation passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
