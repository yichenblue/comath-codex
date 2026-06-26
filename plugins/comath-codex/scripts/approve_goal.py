#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

from lib import (
    ValidationError,
    append_message,
    file_sha256,
    load_goal_approval_records,
    load_project_state,
    repo_root,
    save_goal_approval_records,
    save_project_state,
    scaffold_lock,
    validate_goal_approval_gate,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Approve a DRAFT goal only with an explicit user confirmation record."
    )
    parser.add_argument("--goal-id", required=True, help="Goal id, for example goal_010.")
    parser.add_argument(
        "--user-confirmation",
        required=True,
        help="Verbatim or concise record of the user's explicit approval.",
    )
    parser.add_argument(
        "--approved-by",
        default="user",
        help="Who approved the goal. Defaults to user.",
    )
    return parser.parse_args()


def goal_file_hash(goal: dict) -> tuple[str | None, str | None]:
    rel_file = goal.get("file")
    if not isinstance(rel_file, str) or not rel_file:
        return None, None
    path = repo_root() / rel_file
    if not path.exists():
        return rel_file, None
    return rel_file, file_sha256(path)


def upsert_approval(records: dict, approval: dict) -> None:
    approvals = records.setdefault("approvals", [])
    if not isinstance(approvals, list):
        raise ValidationError("state/goal_approval_records.json: approvals must be a list")
    retained = [
        item for item in approvals
        if not (isinstance(item, dict) and item.get("goal_id") == approval["goal_id"])
    ]
    retained.append(approval)
    records["approvals"] = retained


def main() -> int:
    args = parse_args()
    confirmation = args.user_confirmation.strip()
    if not confirmation:
        print("--user-confirmation must be nonempty")
        return 2

    try:
        with scaffold_lock():
            project_state = load_project_state()
            goals = project_state.get("goals", [])
            if not isinstance(goals, list):
                raise ValidationError("state/project_state.json: goals must be a list")

            target: dict | None = None
            for goal in goals:
                if isinstance(goal, dict) and goal.get("id") == args.goal_id:
                    target = goal
                    break
            if target is None:
                raise ValidationError(f"Goal {args.goal_id!r} not found in state/project_state.json")

            rel_file, digest = goal_file_hash(target)
            now = datetime.now(timezone.utc).isoformat()
            records = load_goal_approval_records()
            upsert_approval(records, {
                "goal_id": args.goal_id,
                "approval_type": "user_confirmation",
                "approved_by": args.approved_by,
                "approved_at": now,
                "user_confirmation": confirmation,
                "goal_file": rel_file,
                "goal_file_sha256": digest,
            })

            previous_status = target.get("status")
            target["status"] = "APPROVED"

            save_goal_approval_records(records)
            validate_goal_approval_gate(project_state)
            save_project_state(project_state)
            append_message(
                "goal_approved",
                None,
                f"Approved goal {args.goal_id} with explicit user confirmation.",
                goal_id=args.goal_id,
                previous_status=previous_status,
                approved_by=args.approved_by,
                goal_file=rel_file,
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Approved {args.goal_id}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
