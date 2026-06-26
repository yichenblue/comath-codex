#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from lib import (
    ValidationError,
    append_message,
    load_agent_registry,
    load_agent_task,
    release_write_locks,
    save_agent_registry,
    save_agent_task,
    scaffold_lock,
    task_path_from_arg,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Mark a registered Codex worker as finished and release write locks.")
    parser.add_argument("task", help="Task id or path to tasks/<task-id>.json.")
    parser.add_argument("--status", default="DONE", choices=["DONE", "BLOCKED", "CLOSED"])
    parser.add_argument("--summary", default="")
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
                    agent["status"] = args.status
                    agent["completed_at"] = timestamp
                    agent["completion_summary"] = args.summary
                    found = True
            if not found:
                raise ValidationError(f"No registry entry found for task {task['task_id']} / agent {agent_id}")
            save_agent_registry(registry)

            release_write_locks(task["task_id"], final_status=args.status)
            task["status"] = "DONE" if args.status == "DONE" else "BLOCKED"
            task["spawn_status"] = args.status
            task["completion_summary"] = args.summary
            save_agent_task(task_path, task)
            append_message(
                "codex_worker_finished",
                task["workstream_id"],
                f"Codex worker {agent_id} finished task {task['task_id']} with status {args.status}.",
                agent_id=agent_id,
                task_path=str(task_path),
                completion_summary=args.summary,
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Marked task {task['task_id']} / agent {agent_id} {args.status}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
