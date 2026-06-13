#!/usr/bin/env python3
from __future__ import annotations

from lib import ValidationError, load_agent_registry, load_write_locks


def main() -> int:
    try:
        registry = load_agent_registry()
        active = [
            agent for agent in registry.get("agents", [])
            if isinstance(agent, dict) and agent.get("status") in {"SPAWNED", "RUNNING"}
        ]
        locks = load_write_locks()
    except ValidationError as exc:
        print(exc)
        return 1

    if not active:
        print("No active registered Codex workers.")
    else:
        print("Active registered Codex workers:")
        for agent in active:
            print(
                f"- {agent.get('agent_id')} "
                f"role={agent.get('role')} task={agent.get('task_id')} "
                f"workstream={agent.get('workstream_id')} status={agent.get('status')}"
            )

    active_locks = [
        lock for lock in locks.get("locks", [])
        if isinstance(lock, dict) and lock.get("status") in {"SPAWNED", "RUNNING"}
    ]
    if active_locks:
        print("Active write locks:")
        for lock in active_locks:
            print(
                f"- {lock.get('path')} "
                f"task={lock.get('task_id')} agent={lock.get('agent_id')} status={lock.get('status')}"
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
