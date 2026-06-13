#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib import (
    ValidationError,
    active_lock_status,
    load_agent_task,
    load_status,
    load_write_locks,
    project_workstream_path,
    read_json,
    repo_root,
    save_write_locks,
    scaffold_lock,
    tasks_dir,
)


def task_files() -> list[Path]:
    return sorted(tasks_dir().glob("task_*.json"))


def load_tasks() -> list[tuple[Path, dict]]:
    loaded: list[tuple[Path, dict]] = []
    for path in task_files():
        try:
            loaded.append((path, load_agent_task(path)))
        except ValidationError as exc:
            print(f"Skipping invalid task {path}: {exc}", file=sys.stderr)
    return loaded


def active_task_count(tasks: list[tuple[Path, dict]]) -> int:
    return sum(
        1
        for _, task in tasks
        if task.get("spawn_status") in {"SPAWNED", "RUNNING"}
    )


def pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid="],
        text=True,
        capture_output=True,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def normalize_task_ids(values: list[str]) -> set[str]:
    ids: set[str] = set()
    for value in values:
        path = Path(value)
        ids.add(path.stem if path.suffix == ".json" else path.name)
    return ids


def require_scope(args: argparse.Namespace) -> None:
    has_scope = bool(args.all_tasks or args.task_id or args.workstream_id or args.goal_id)
    if not has_scope:
        raise ValidationError(
            "Refusing to schedule without an explicit scope. Use one or more of "
            "--task-id, --workstream-id, --goal-id, or pass --all intentionally."
        )


def task_goal_id(task: dict) -> str | None:
    workstream_id = task.get("workstream_id")
    if not isinstance(workstream_id, str) or not workstream_id:
        return None
    try:
        return str(load_status(project_workstream_path(workstream_id)).get("goal_id"))
    except ValidationError:
        return None


def task_in_scope(path: Path, task: dict, args: argparse.Namespace) -> bool:
    if args.agent_type and task.get("agent_type") != args.agent_type:
        return False

    task_ids = normalize_task_ids(args.task_id)
    if task_ids and task.get("task_id") not in task_ids and path.stem not in task_ids:
        return False

    workstream_ids = set(args.workstream_id)
    if workstream_ids and task.get("workstream_id") not in workstream_ids:
        return False

    goal_ids = set(args.goal_id)
    if goal_ids and task_goal_id(task) not in goal_ids:
        return False

    return True


def scoped_tasks(args: argparse.Namespace) -> list[tuple[Path, dict]]:
    require_scope(args)
    tasks = [
        (path, task)
        for path, task in load_tasks()
        if args.all_tasks or task_in_scope(path, task, args)
    ]
    if args.all_tasks:
        return [
            (path, task)
            for path, task in tasks
            if task_in_scope(path, task, args)
        ]
    return tasks


def run_dir_for_task(task: dict) -> Path | None:
    runner = task.get("runner")
    if not isinstance(runner, dict) or not runner.get("run_dir"):
        return None
    return repo_root() / str(runner["run_dir"])


def worker_process_state(task: dict) -> str:
    run_dir = run_dir_for_task(task)
    if run_dir is None:
        return "missing_run_dir"
    if (run_dir / "exit_status.json").exists():
        return "finished"
    run_json_path = run_dir / "run.json"
    if not run_json_path.exists():
        return "missing_run_json"
    try:
        run_json = read_json(run_json_path)
    except ValidationError:
        return "bad_run_json"
    pid = run_json.get("wrapper_pid")
    if isinstance(pid, int) and pid_is_running(pid):
        return "running"
    return "dead"


def spawn_ready(args: argparse.Namespace) -> int:
    all_tasks = load_tasks()
    tasks = scoped_tasks(args)
    active = active_task_count(all_tasks)
    spawned = 0
    for path, task in tasks:
        if active + spawned >= args.max_agents:
            break
        if task.get("status") != "READY":
            continue
        if task.get("spawn_status") != "NOT_SPAWNED":
            continue
        if task.get("execution_mode") not in {"codex_cli", "codex_worker"}:
            continue
        command = [
            sys.executable,
            str(repo_root() / "scripts" / "spawn_task_cli.py"),
            str(path),
            "--sandbox",
            args.sandbox,
            "--ask-for-approval",
            args.ask_for_approval,
        ]
        if args.model:
            command.extend(["--model", args.model])
        command.extend(["--worker-timeout-seconds", str(args.worker_timeout_seconds)])
        completed = subprocess.run(command, cwd=repo_root(), text=True)
        if completed.returncode == 0:
            spawned += 1
        else:
            return completed.returncode
    print(f"Spawned {spawned} ready task(s).")
    return 0


def collect_finished(args: argparse.Namespace) -> int:
    tasks = scoped_tasks(args)
    collected = 0
    failed = 0
    recovered_failed = 0
    recoverable_failed_states = {"dead", "missing_run_dir", "missing_run_json", "bad_run_json"}
    for path, task in tasks:
        if task.get("spawn_status") != "RUNNING":
            continue
        process_state = worker_process_state(task)
        if process_state == "running":
            continue
        if process_state in recoverable_failed_states and args.no_recover_dead:
            continue
        if process_state not in {"finished"} | recoverable_failed_states:
            continue
        command = [
            sys.executable,
            str(repo_root() / "scripts" / "collect_task_cli.py"),
            str(path),
        ]
        if args.no_block_workstream:
            command.append("--no-block-workstream")
        completed = subprocess.run(command, cwd=repo_root(), text=True)
        if completed.returncode == 0:
            collected += 1
        elif process_state in recoverable_failed_states:
            try:
                updated_task = load_agent_task(path)
            except ValidationError:
                failed += 1
                continue
            if updated_task.get("spawn_status") in {"BLOCKED", "DONE"}:
                recovered_failed += 1
            else:
                failed += 1
        else:
            failed += 1
    print(
        f"Collected {collected} finished task(s); "
        f"{recovered_failed} failed RUNNING task(s) recovered to BLOCKED; "
        f"{failed} failed collection."
    )
    return 1 if failed else 0


def lock_matches_scope(
    lock: dict,
    args: argparse.Namespace,
    scoped_by_id: dict[str, tuple[Path, dict]],
) -> bool:
    task_id = lock.get("task_id")
    task_ids = normalize_task_ids(args.task_id)
    if task_ids and task_id not in task_ids:
        return False

    workstream_ids = set(args.workstream_id)
    if workstream_ids and lock.get("workstream_id") not in workstream_ids:
        return False

    if args.goal_id or args.agent_type:
        return isinstance(task_id, str) and task_id in scoped_by_id

    if args.all_tasks:
        return True
    if task_ids:
        return True
    if workstream_ids:
        return True
    return isinstance(task_id, str) and task_id in scoped_by_id


def recover_locks(args: argparse.Namespace) -> int:
    tasks = scoped_tasks(args)
    scoped_by_id = {
        str(task["task_id"]): (path, task)
        for path, task in tasks
        if isinstance(task.get("task_id"), str)
    }
    with scaffold_lock():
        locks = load_write_locks()
        recovered = 0
        skipped_active = 0
        skipped_out_of_scope = 0
        timestamp = datetime.now(timezone.utc).isoformat()

        for lock in locks.get("locks", []):
            if not isinstance(lock, dict) or not active_lock_status(lock.get("status")):
                continue
            if not lock_matches_scope(lock, args, scoped_by_id):
                skipped_out_of_scope += 1
                continue

            task_id = lock.get("task_id")
            task_tuple = scoped_by_id.get(str(task_id)) if isinstance(task_id, str) else None
            reason: str | None = None
            if task_tuple is None:
                reason = "task JSON is missing or outside the resolvable scoped task set"
            else:
                _, task = task_tuple
                if task.get("spawn_status") not in {"SPAWNED", "RUNNING"}:
                    reason = f"task spawn_status is {task.get('spawn_status')!r}"

            if reason is None:
                skipped_active += 1
                continue

            recovered += 1
            print(f"Recovering lock for task {task_id}: {lock.get('path')} ({reason})")
            if not args.dry_run:
                lock["status"] = args.final_status
                lock["released_at"] = timestamp
                lock["recovery_reason"] = reason

        if not args.dry_run:
            save_write_locks(locks)
    mode = "Would recover" if args.dry_run else "Recovered"
    print(
        f"{mode} {recovered} orphan active lock(s); "
        f"skipped {skipped_active} active task lock(s), {skipped_out_of_scope} out-of-scope lock(s)."
    )
    return 0


def add_scope_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-id", action="append", default=[], help="Task id or tasks/<id>.json to include.")
    parser.add_argument("--workstream-id", action="append", default=[], help="Workstream id to include.")
    parser.add_argument("--goal-id", action="append", default=[], help="Goal id to include.")
    parser.add_argument(
        "--all",
        dest="all_tasks",
        action="store_true",
        help="Intentionally allow scheduler to scan all tasks. Filters still apply if provided.",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Minimal scheduler for Codex CLI worker tasks.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    spawn = subparsers.add_parser("spawn-ready", help="Spawn READY tasks up to a concurrency limit.")
    add_scope_args(spawn)
    spawn.add_argument("--max-agents", type=int, default=1)
    spawn.add_argument("--agent-type", choices=["literature", "proof", "computation", "reviewer", "synthesis", "workstream_coordinator"])
    spawn.add_argument("--model", default="")
    spawn.add_argument("--worker-timeout-seconds", type=int, default=3600)
    spawn.add_argument("--sandbox", default="workspace-write", choices=["read-only", "workspace-write", "danger-full-access"])
    spawn.add_argument("--ask-for-approval", default="never", choices=["untrusted", "on-request", "on-failure", "never"])
    spawn.set_defaults(func=spawn_ready)

    collect = subparsers.add_parser("collect-finished", help="Collect scoped RUNNING tasks that finished or whose worker died.")
    add_scope_args(collect)
    collect.add_argument("--agent-type", choices=["literature", "proof", "computation", "reviewer", "synthesis", "workstream_coordinator"])
    collect.add_argument("--no-block-workstream", action="store_true")
    collect.add_argument(
        "--no-recover-dead",
        action="store_true",
        help=(
            "Do not collect RUNNING tasks with recoverable failed runner state, "
            "including dead pid, missing run_dir, missing run.json, or bad run.json."
        ),
    )
    collect.set_defaults(func=collect_finished)

    recover = subparsers.add_parser("recover-locks", help="Release scoped active write locks that no longer have an active task owner.")
    add_scope_args(recover)
    recover.add_argument("--agent-type", choices=["literature", "proof", "computation", "reviewer", "synthesis", "workstream_coordinator"])
    recover.add_argument("--dry-run", action="store_true")
    recover.add_argument("--final-status", choices=["BLOCKED", "CLOSED"], default="CLOSED")
    recover.set_defaults(func=recover_locks)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        return args.func(args)
    except ValidationError as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
