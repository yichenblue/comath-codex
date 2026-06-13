#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from lib import (
    AGENT_TYPES,
    ValidationError,
    append_message,
    ensure_relative_repo_path,
    project_workstream_path,
    repo_root,
    scaffold_lock,
    write_json,
)


def next_request_id(request_dir: Path) -> str:
    highest = 0
    for path in request_dir.glob("subagent_request_*.json"):
        parts = path.stem.split("_")
        if parts and parts[-1].isdigit():
            highest = max(highest, int(parts[-1]))
    return f"subagent_request_{highest + 1:03d}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a workstream-local request for a specialized sub-agent.")
    parser.add_argument("--workstream-id", required=True)
    parser.add_argument("--requested-by-task", required=True)
    parser.add_argument("--agent-type", required=True, choices=sorted(AGENT_TYPES - {"workstream_coordinator"}))
    parser.add_argument("--objective", required=True)
    parser.add_argument("--reason", required=True)
    parser.add_argument(
        "--allowed-write-path",
        action="append",
        default=[],
        help="Optional repo-relative write path requested for the sub-agent. May be repeated.",
    )
    parser.add_argument("--notes", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with scaffold_lock():
            workstream_path = project_workstream_path(args.workstream_id)
            request_dir = workstream_path / "subagent_requests"
            request_dir.mkdir(parents=True, exist_ok=True)
            for raw_path in args.allowed_write_path:
                ensure_relative_repo_path(raw_path, "allowed_write_path")
            request_id = next_request_id(request_dir)
            request = {
                "request_id": request_id,
                "status": "PENDING",
                "workstream_id": args.workstream_id,
                "requested_by_task": args.requested_by_task,
                "requested_agent_type": args.agent_type,
                "objective": args.objective,
                "reason": args.reason,
                "allowed_write_paths": args.allowed_write_path,
                "created_task_id": None,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "approved_at": None,
                "rejected_at": None,
                "notes": args.notes,
            }
            output = request_dir / f"{request_id}.json"
            if output.exists():
                raise ValidationError(f"Sub-agent request already exists: {output}")
            write_json(output, request)
            append_message(
                "subagent_requested",
                args.workstream_id,
                f"Sub-agent request {request_id} created for {args.agent_type}.",
                request_path=str(output.relative_to(repo_root())),
                requested_agent_type=args.agent_type,
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Created {output.relative_to(repo_root())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
