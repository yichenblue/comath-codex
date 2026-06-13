#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from lib import ValidationError, read_json, tasks_dir, validate_agent_task


def task_paths(argv: list[str]) -> list[Path]:
    if len(argv) > 1:
        return [Path(item).resolve() for item in argv[1:]]
    return sorted(tasks_dir().glob("task_*.json"))


def main(argv: list[str]) -> int:
    errors: list[str] = []
    paths = task_paths(argv)
    if not paths:
        print("No task JSON files found.")
        return 0
    for path in paths:
        try:
            task = read_json(path)
        except ValidationError as exc:
            errors.append(str(exc))
            continue
        errors.extend(validate_agent_task(task, path))

    if errors:
        print("Agent task validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print(f"Agent task validation passed for {len(paths)} task file(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

