#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from lib import (
    ValidationError,
    build_file_manifest,
    load_agent_task,
    manifest_changes,
    read_json,
    repo_root,
    task_path_from_arg,
    worker_diff_errors,
    write_json,
)


def resolve_run_dir(task: dict, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    runner = task.get("runner")
    if isinstance(runner, dict) and runner.get("run_dir"):
        return (repo_root() / str(runner["run_dir"])).resolve()
    raise ValidationError("No run directory supplied and task has no runner.run_dir field")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate that a worker changed only allowed_write_paths.")
    parser.add_argument("task", help="Task id or path to tasks/<task-id>.json.")
    parser.add_argument("--run-dir", help="Run directory containing pre_manifest.json.")
    parser.add_argument("--write-result", action="store_true", help="Write diff_result.json under the run directory.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        task_path = task_path_from_arg(args.task)
        task = load_agent_task(task_path)
        run_dir = resolve_run_dir(task, args.run_dir)
        before = read_json(run_dir / "pre_manifest.json")
        after = build_file_manifest()
        changes = manifest_changes(before, after)
        errors = worker_diff_errors(task, changes)
        result = {
            "task_id": task["task_id"],
            "run_dir": str(run_dir.relative_to(repo_root())),
            "changes": changes,
            "allowed_write_paths": task["allowed_write_paths"],
            "passed": not errors,
            "errors": errors,
        }
        if args.write_result:
            write_json(run_dir / "diff_result.json", result)
    except ValidationError as exc:
        print(exc)
        return 1

    changed_count = sum(len(items) for items in changes.values())
    if errors:
        print(f"Worker diff validation failed for {task['task_id']} ({changed_count} changed file(s)):")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Worker diff validation passed for {task['task_id']} ({changed_count} changed file(s)).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
