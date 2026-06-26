#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from lib import (
    ValidationError,
    append_message,
    load_agent_registry,
    load_agent_task,
    load_write_locks,
    save_agent_registry,
    save_agent_task,
    save_write_locks,
    scaffold_lock,
    task_path_from_arg,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mark a registered Codex worker as RUNNING.")
    parser.add_argument("task", help="Task id or path to tasks/<task-id>.json.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    task_path = task_path_from_arg(args.task)
    try:
        task = load_agent_task(task_path)
        agent_id = task.get("assigned_agent_id")
        if not agent_id:
            raise ValidationError(f"{task_path}: task has no assigned_agent_id")

        with scaffold_lock():
            timestamp = datetime.now(timezone.utc).isoformat()
            registry = load_agent_registry()
            found = False
            for agent in registry.get("agents", []):
                if isinstance(agent, dict) and agent.get("task_id") == task["task_id"] and agent.get("agent_id") == agent_id:
                    agent["status"] = "RUNNING"
                    agent["started_at"] = agent.get("started_at") or timestamp
                    found = True
            if not found:
                raise ValidationError(f"No registry entry found for task {task['task_id']} / agent {agent_id}")
            save_agent_registry(registry)

            locks = load_write_locks()
            for lock in locks.get("locks", []):
                if isinstance(lock, dict) and lock.get("task_id") == task["task_id"] and lock.get("agent_id") == agent_id:
                    if lock.get("status") == "SPAWNED":
                        lock["status"] = "RUNNING"
            save_write_locks(locks)

            task["status"] = "IN_PROGRESS"
            task["spawn_status"] = "RUNNING"
            save_agent_task(task_path, task)
            append_message(
                "codex_worker_started",
                task["workstream_id"],
                f"Codex worker {agent_id} started task {task['task_id']}.",
                agent_id=agent_id,
                task_path=str(task_path),
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Marked task {task['task_id']} / agent {agent_id} RUNNING.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
