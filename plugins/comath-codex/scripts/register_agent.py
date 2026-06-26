#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from lib import (
    AGENT_REGISTRY_STATUSES,
    ValidationError,
    acquire_write_locks,
    append_message,
    load_agent_registry,
    load_agent_task,
    repo_root,
    save_agent_registry,
    save_agent_task,
    scaffold_lock,
    task_path_from_arg,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Register a spawned Codex worker against a task and acquire its write locks."
    )
    parser.add_argument("task", help="Task id or path to tasks/<task-id>.json.")
    parser.add_argument("--agent-id", required=True, help="Codex runtime agent id returned by spawn_agent.")
    parser.add_argument("--nickname", default="", help="Optional human-readable worker nickname.")
    parser.add_argument("--status", default="SPAWNED", choices=sorted(AGENT_REGISTRY_STATUSES - {"DONE", "BLOCKED", "CLOSED"}))
    parser.add_argument("--model", default="")
    parser.add_argument("--reasoning-effort", default="")
    parser.add_argument("--notes", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    task_path = task_path_from_arg(args.task)
    try:
        task = load_agent_task(task_path)
        if task.get("execution_mode", "local_thread") not in {"codex_worker", "codex_cli", "external"}:
            raise ValidationError(
                f"{task_path}: execution_mode must be 'codex_worker', 'codex_cli', or 'external' before registering a worker"
            )
        if task.get("assigned_agent_id") and task.get("assigned_agent_id") != args.agent_id:
            raise ValidationError(
                f"{task_path}: task is already assigned to {task.get('assigned_agent_id')!r}"
            )

        with scaffold_lock():
            registry = load_agent_registry()
            for agent in registry.get("agents", []):
                if not isinstance(agent, dict):
                    continue
                if agent.get("agent_id") == args.agent_id and agent.get("status") in {"SPAWNED", "RUNNING"}:
                    raise ValidationError(f"Agent {args.agent_id!r} is already registered as active")
                if agent.get("task_id") == task["task_id"] and agent.get("status") in {"SPAWNED", "RUNNING"}:
                    raise ValidationError(f"Task {task['task_id']!r} already has an active registered agent")

            acquire_write_locks(task, args.agent_id, status_value=args.status)
            timestamp = datetime.now(timezone.utc).isoformat()
            registry["agents"].append({
                "agent_id": args.agent_id,
                "nickname": args.nickname,
                "role": task["agent_type"],
                "task_id": task["task_id"],
                "task_path": str(task_path.relative_to(repo_root())),
                "workstream_id": task["workstream_id"],
                "status": args.status,
                "allowed_write_paths": task["allowed_write_paths"],
                "spawned_at": timestamp,
                "started_at": None,
                "completed_at": None,
                "model": args.model,
                "reasoning_effort": args.reasoning_effort,
                "notes": args.notes,
            })
            save_agent_registry(registry)

            task["assigned_agent_id"] = args.agent_id
            task["spawn_status"] = args.status
            task["status"] = "IN_PROGRESS"
            save_agent_task(task_path, task)
            append_message(
                "codex_worker_registered",
                task["workstream_id"],
                f"Registered Codex worker {args.agent_id} for task {task['task_id']}.",
                task_path=str(task_path.relative_to(repo_root())),
                agent_id=args.agent_id,
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Registered agent {args.agent_id} for task {task['task_id']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
