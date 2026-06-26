#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from create_agent_task import (
    default_allowed_write_paths,
    default_input_files,
    default_required_outputs,
    next_task_id,
    rel,
)
from lib import (
    DEFAULT_WORKER_OUTPUT_SCHEMA,
    ValidationError,
    append_message,
    load_agent_task,
    project_workstream_path,
    read_json,
    repo_root,
    safe_id,
    scaffold_lock,
    tasks_dir,
    validate_agent_task,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Approve a sub-agent request and create a Codex worker task.")
    parser.add_argument("request", help="Path to subagent_request_###.json.")
    parser.add_argument("--task-id")
    parser.add_argument("--execution-mode", default="codex_cli", choices=["local_thread", "codex_worker", "codex_cli", "external"])
    parser.add_argument("--notes", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request_path = Path(args.request).resolve()
    try:
        with scaffold_lock():
            request = read_json(request_path)
            if request.get("status") != "PENDING":
                raise ValidationError(f"{request_path}: request status must be PENDING")
            workstream_id = request.get("workstream_id")
            agent_type = request.get("requested_agent_type")
            objective = request.get("objective")
            if not isinstance(workstream_id, str) or not workstream_id:
                raise ValidationError(f"{request_path}: missing workstream_id")
            if not isinstance(agent_type, str) or not agent_type:
                raise ValidationError(f"{request_path}: missing requested_agent_type")
            if not isinstance(objective, str) or not objective:
                raise ValidationError(f"{request_path}: missing objective")

            workstream_path = project_workstream_path(workstream_id)
            task_id = safe_id(args.task_id) if args.task_id else next_task_id(agent_type, workstream_id)
            requested_paths = request.get("allowed_write_paths") or []
            if requested_paths and not all(isinstance(item, str) and item for item in requested_paths):
                raise ValidationError(f"{request_path}: allowed_write_paths must be a list of nonempty strings")
            allowed_write_paths = sorted(set(default_allowed_write_paths(agent_type, workstream_path) + requested_paths))
            required_outputs = default_required_outputs(agent_type, workstream_path)
            if agent_type == "reviewer":
                review_output = rel(workstream_path / "review" / f"{task_id}.json")
                allowed_write_paths = [review_output]
                required_outputs = [review_output]

            task = {
                "task_id": task_id,
                "agent_type": agent_type,
                "workstream_id": workstream_id,
                "status": "READY",
                "execution_mode": args.execution_mode,
                "spawn_status": "NOT_SPAWNED",
                "assigned_agent_id": None,
                "parent_task_id": request.get("requested_by_task"),
                "parent_workstream_id": workstream_id,
                "subagent_request_id": request.get("request_id"),
                "objective": objective,
                "input_files": default_input_files(workstream_path),
                "allowed_write_paths": allowed_write_paths,
                "required_outputs": required_outputs,
                "structured_output_required": True,
                "output_schema": DEFAULT_WORKER_OUTPUT_SCHEMA,
                "success_criteria": [
                    "Stay within allowed_write_paths.",
                    "Do not edit files outside this task's allowed_write_paths, even if they appear relevant.",
                    "Do not revert or overwrite edits made by other concurrent agents.",
                    "Report findings back into the assigned workstream artifacts or review file.",
                    "Use C-### ids for substantive claims that should be reviewed.",
                    "Do not introduce new notation unless necessary; define and justify any necessary new notation at first use.",
                    "Do not mark workstream COMPLETE manually.",
                ],
                "notes": args.notes,
                "handoff_summary": "",
                "completion_summary": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            task_path = tasks_dir() / f"{task_id}.json"
            if task_path.exists():
                raise ValidationError(f"Task already exists: {task_path}")
            errors = validate_agent_task(task, task_path)
            if errors:
                raise ValidationError("\n".join(errors))
            write_json(task_path, task)
            load_agent_task(task_path)

            request["status"] = "APPROVED"
            request["created_task_id"] = task_id
            request["approved_at"] = datetime.now(timezone.utc).isoformat()
            write_json(request_path, request)
            append_message(
                "subagent_request_approved",
                workstream_id,
                f"Approved sub-agent request {request.get('request_id')} and created task {task_id}.",
                request_path=str(request_path.relative_to(repo_root())),
                task_path=str(task_path.relative_to(repo_root())),
                requested_agent_type=agent_type,
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Approved {request_path.relative_to(repo_root())}")
    print(f"Created {task_path.relative_to(repo_root())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
