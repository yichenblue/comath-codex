#!/usr/bin/env python3
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

from assemble_agent_prompt import render_prompt
from lib import (
    ValidationError,
    acquire_write_locks,
    agent_runs_dir,
    append_message,
    assembled_prompts_dir,
    build_file_manifest,
    load_agent_registry,
    load_agent_task,
    repo_relative_path,
    repo_root,
    save_agent_registry,
    save_agent_task,
    scaffold_lock,
    source_guard_errors,
    task_path_from_arg,
    structured_output_required,
    worker_output_schema,
    write_json,
)


DEFAULT_CODEX_APP = "/Applications/Codex.app/Contents/Resources/codex"


def codex_executable(explicit: str | None) -> str:
    if explicit:
        return explicit
    discovered = shutil.which("codex")
    if discovered:
        return discovered
    if Path(DEFAULT_CODEX_APP).exists():
        return DEFAULT_CODEX_APP
    raise ValidationError("Could not find Codex CLI. Pass --codex-bin explicitly.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Spawn a task as an independent Codex CLI worker process.")
    parser.add_argument("task", help="Task id or path to tasks/<task-id>.json.")
    parser.add_argument("--codex-bin", help="Path to the Codex CLI binary.")
    parser.add_argument("--model", default="", help="Optional Codex model override.")
    parser.add_argument("--sandbox", default="workspace-write", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--ask-for-approval", default="never", choices=["untrusted", "on-request", "on-failure", "never"])
    parser.add_argument("--add-dir", action="append", default=[], help="Additional writable directory passed to codex exec.")
    parser.add_argument(
        "--worker-timeout-seconds",
        type=int,
        default=3600,
        help="Maximum seconds for the independent worker command. Use 0 to disable.",
    )
    parser.add_argument("--agent-id", help="Optional explicit worker id.")
    parser.add_argument("--nickname", default="", help="Optional human-readable worker nickname.")
    parser.add_argument("--dry-run", action="store_true", help="Print the command that would be launched without changing state.")
    return parser.parse_args()


def active_agent_errors(registry: dict, task: dict, agent_id: str) -> list[str]:
    errors: list[str] = []
    for agent in registry.get("agents", []):
        if not isinstance(agent, dict):
            continue
        if agent.get("status") not in {"SPAWNED", "RUNNING"}:
            continue
        if agent.get("agent_id") == agent_id:
            errors.append(f"Agent id {agent_id!r} is already active")
        if agent.get("task_id") == task["task_id"]:
            errors.append(f"Task {task['task_id']!r} already has an active registered agent")
    return errors


def codex_argv(
    args: argparse.Namespace,
    prompt_path: Path,
    last_message_path: Path,
    output_schema_path: Path | None,
) -> list[str]:
    default_add_dirs = [str(repo_root().parent)]
    argv = [
        codex_executable(args.codex_bin),
        "exec",
        "--skip-git-repo-check",
        "--json",
        "-C",
        str(repo_root()),
        "-s",
        args.sandbox,
        "-o",
        str(last_message_path),
    ]
    if args.model:
        argv.extend(["-m", args.model])
    if output_schema_path is not None:
        argv.extend(["--output-schema", str(output_schema_path)])
    for extra in default_add_dirs + args.add_dir:
        argv.extend(["--add-dir", extra])
    argv.append("-")
    return argv


def main() -> int:
    args = parse_args()
    task_path = task_path_from_arg(args.task)

    try:
        if args.worker_timeout_seconds < 0:
            raise ValidationError("--worker-timeout-seconds must be >= 0")
        task = load_agent_task(task_path)
        if task.get("status") not in {"READY", "IN_PROGRESS"}:
            raise ValidationError(f"{task_path}: task status must be READY or IN_PROGRESS before spawning")
        if task.get("spawn_status") not in {"NOT_SPAWNED", "CLOSED", "BLOCKED"}:
            raise ValidationError(f"{task_path}: spawn_status is already {task.get('spawn_status')!r}")
        if task.get("assigned_agent_id") and task.get("spawn_status") != "CLOSED":
            raise ValidationError(f"{task_path}: task is already assigned to {task.get('assigned_agent_id')!r}")
        guard_errors = source_guard_errors()
        if guard_errors:
            raise ValidationError(
                "Refusing to spawn worker while guarded paper source is dirty:\n"
                + "\n".join(f"- {error}" for error in guard_errors)
            )

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        agent_id = args.agent_id or f"codex_cli_{task['task_id']}_{uuid.uuid4().hex[:8]}"
        run_dir = agent_runs_dir() / task["task_id"] / f"{timestamp}_{agent_id}"
        prompt_path = assembled_prompts_dir() / f"{task['task_id']}.md"
        last_message_path = run_dir / "last_message.md"
        final_output_path = run_dir / "final_output.json"
        command_path = run_dir / "command.json"

        task_for_prompt = dict(task)
        task_for_prompt["execution_mode"] = "codex_cli"
        task_for_prompt["spawn_status"] = "RUNNING"
        task_for_prompt["assigned_agent_id"] = agent_id

        output_schema_path = repo_root() / worker_output_schema(task) if structured_output_required(task) else None
        argv = codex_argv(args, prompt_path, last_message_path, output_schema_path)
        wrapper_argv = [
            sys.executable,
            str(repo_root() / "scripts" / "run_codex_worker_process.py"),
            "--run-dir",
            str(run_dir),
            "--command-json",
            str(command_path),
        ]
        wrapper_argv.extend(["--timeout-seconds", str(args.worker_timeout_seconds)])
        if args.dry_run:
            print("Codex worker command:")
            print(" ".join(argv))
            print("Wrapper command:")
            print(" ".join(wrapper_argv))
            return 0

        with scaffold_lock():
            registry = load_agent_registry()
            errors = active_agent_errors(registry, task, agent_id)
            if errors:
                raise ValidationError("\n".join(errors))

            run_dir.mkdir(parents=True, exist_ok=False)
            assembled_prompts_dir().mkdir(parents=True, exist_ok=True)
            prompt = render_prompt(task_for_prompt)
            prompt_path.write_text(prompt, encoding="utf-8")
            (run_dir / "prompt.md").write_text(prompt, encoding="utf-8")
            write_json(command_path, {
                "argv": argv,
                "prompt_path": str(prompt_path),
                "last_message_path": str(last_message_path),
                "final_output_path": str(final_output_path),
                "structured_output_required": structured_output_required(task),
                "output_schema_path": str(output_schema_path) if output_schema_path is not None else None,
                "repo_root": str(repo_root()),
                "worker_timeout_seconds": args.worker_timeout_seconds,
            })

            task["execution_mode"] = "codex_cli"
            task["assigned_agent_id"] = agent_id
            task["status"] = "IN_PROGRESS"
            task["spawn_status"] = "RUNNING"
            task["runner"] = {
                "kind": "codex_cli",
                "run_dir": repo_relative_path(run_dir),
                "prompt_path": repo_relative_path(prompt_path),
                "last_message_path": repo_relative_path(last_message_path),
                "final_output_path": repo_relative_path(final_output_path),
                "structured_output_required": structured_output_required(task),
                "output_schema": worker_output_schema(task),
                "command_path": repo_relative_path(command_path),
                "exit_status_path": repo_relative_path(run_dir / "exit_status.json"),
                "diff_result_path": repo_relative_path(run_dir / "diff_result.json"),
                "spawned_at": datetime.now(timezone.utc).isoformat(),
                "worker_timeout_seconds": args.worker_timeout_seconds,
            }

            acquire_write_locks(task, agent_id, status_value="RUNNING")
            registry["agents"].append({
                "agent_id": agent_id,
                "nickname": args.nickname,
                "role": task["agent_type"],
                "task_id": task["task_id"],
                "task_path": repo_relative_path(task_path),
                "workstream_id": task["workstream_id"],
                "status": "RUNNING",
                "allowed_write_paths": task["allowed_write_paths"],
                "spawned_at": task["runner"]["spawned_at"],
                "started_at": task["runner"]["spawned_at"],
                "completed_at": None,
                "model": args.model,
                "reasoning_effort": "",
                "notes": "Spawned by scripts/spawn_task_cli.py.",
                "runner": task["runner"],
            })
            save_agent_registry(registry)
            save_agent_task(task_path, task)
            append_message(
                "codex_cli_worker_spawned",
                task["workstream_id"],
                f"Spawned Codex CLI worker {agent_id} for task {task['task_id']}.",
                task_path=repo_relative_path(task_path),
                agent_id=agent_id,
                run_dir=repo_relative_path(run_dir),
            )

        write_json(run_dir / "pre_manifest.json", build_file_manifest())
        process = subprocess.Popen(
            wrapper_argv,
            cwd=repo_root(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        write_json(run_dir / "run.json", {
            "wrapper_pid": process.pid,
            "wrapper_argv": wrapper_argv,
            "spawned_at": datetime.now(timezone.utc).isoformat(),
            "agent_id": agent_id,
            "task_id": task["task_id"],
        })
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Spawned {agent_id} for {task['task_id']}.")
    print(f"Run directory: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
