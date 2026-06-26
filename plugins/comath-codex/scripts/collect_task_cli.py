#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from lib import (
    ValidationError,
    append_message,
    build_file_manifest,
    load_agent_registry,
    load_agent_task,
    load_status,
    manifest_changes,
    mark_status_blocked,
    project_workstream_path,
    read_json,
    record_failed_exploration,
    release_write_locks,
    report_has_required_sections,
    report_path,
    repo_relative_path,
    repo_root,
    run_workstream_tests,
    save_agent_registry,
    save_agent_task,
    scaffold_lock,
    source_guard_errors,
    status_path,
    structured_output_required,
    task_path_from_arg,
    validate_review,
    worker_diff_errors,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect a finished Codex CLI worker task.")
    parser.add_argument("task", help="Task id or path to tasks/<task-id>.json.")
    parser.add_argument("--run-dir", help="Override task.runner.run_dir.")
    parser.add_argument(
        "--no-block-workstream",
        action="store_true",
        help="Do not mark the workstream BLOCKED when collection fails.",
    )
    return parser.parse_args()


def resolve_run_dir(task: dict, explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    runner = task.get("runner")
    if isinstance(runner, dict) and runner.get("run_dir"):
        return (repo_root() / str(runner["run_dir"])).resolve()
    raise ValidationError("No run directory supplied and task has no runner.run_dir field")


def pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid="],
        text=True,
        capture_output=True,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def required_output_errors(task: dict) -> list[str]:
    errors: list[str] = []
    for raw in task.get("required_outputs", []):
        if not isinstance(raw, str):
            errors.append(f"Invalid required output path: {raw!r}")
            continue
        path = repo_root() / raw
        if not path.exists():
            errors.append(f"Missing required output: {raw}")
    return errors


def extract_json_object(text: str) -> dict:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end <= start:
            raise ValidationError("Structured worker output is not a JSON object")
        try:
            parsed = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValidationError(f"Structured worker output is invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValidationError("Structured worker output must be a JSON object")
    return parsed


def list_of_strings(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def validate_worker_final_output(payload: dict, task: dict) -> list[str]:
    errors: list[str] = []
    required = [
        "schema_version",
        "task_id",
        "agent_type",
        "workstream_id",
        "completion_status",
        "summary",
        "outputs_written",
        "claims",
        "evidence",
        "uncertainty_and_gaps",
        "failed_attempts",
        "next_steps",
        "review",
        "computation",
        "paper_source_changes",
    ]
    for key in required:
        if key not in payload:
            errors.append(f"Structured worker output missing key: {key}")

    if payload.get("schema_version") != 1:
        errors.append("Structured worker output schema_version must be 1")
    for key in ["task_id", "agent_type", "workstream_id"]:
        if payload.get(key) != task.get(key):
            errors.append(
                f"Structured worker output {key}={payload.get(key)!r} does not match task {task.get(key)!r}"
            )
    if payload.get("completion_status") not in {"done", "blocked", "needs_followup"}:
        errors.append("Structured worker output completion_status must be done, blocked, or needs_followup")
    if payload.get("completion_status") == "blocked":
        errors.append("Structured worker output reports completion_status=blocked")
    if not isinstance(payload.get("summary"), str) or not payload.get("summary", "").strip():
        errors.append("Structured worker output summary must be a nonempty string")

    for key in ["outputs_written", "evidence", "uncertainty_and_gaps", "failed_attempts", "next_steps"]:
        if key in payload and not list_of_strings(payload.get(key)):
            errors.append(f"Structured worker output {key} must be a list of strings")

    claims = payload.get("claims")
    if "claims" in payload:
        if not isinstance(claims, list):
            errors.append("Structured worker output claims must be a list")
        else:
            for index, claim in enumerate(claims):
                if not isinstance(claim, dict):
                    errors.append(f"Structured worker output claims[{index}] must be an object")
                    continue
                for field in ["claim_id", "text", "status"]:
                    if not isinstance(claim.get(field), str):
                        errors.append(f"Structured worker output claims[{index}].{field} must be a string")
                if claim.get("status") not in {"proposed", "approved_by_worker", "blocked", "not_applicable"}:
                    errors.append(f"Structured worker output claims[{index}].status has invalid value")

    review = payload.get("review")
    if "review" in payload:
        if not isinstance(review, dict):
            errors.append("Structured worker output review must be an object")
        else:
            if not isinstance(review.get("decision"), str):
                errors.append("Structured worker output review.decision must be a string")
            for key in ["approved_claims", "blocked_claims"]:
                if not list_of_strings(review.get(key)):
                    errors.append(f"Structured worker output review.{key} must be a list of strings")

    computation = payload.get("computation")
    if "computation" in payload:
        if not isinstance(computation, dict):
            errors.append("Structured worker output computation must be an object")
        else:
            for key in ["tests_run", "tests_passed"]:
                if not isinstance(computation.get(key), bool):
                    errors.append(f"Structured worker output computation.{key} must be a boolean")
            if not isinstance(computation.get("test_summary"), str):
                errors.append("Structured worker output computation.test_summary must be a string")

    paper_source_changes = payload.get("paper_source_changes")
    if "paper_source_changes" in payload:
        if not isinstance(paper_source_changes, dict):
            errors.append("Structured worker output paper_source_changes must be an object")
        else:
            for key in ["proposed_patch", "direct_source_edit"]:
                if not isinstance(paper_source_changes.get(key), bool):
                    errors.append(f"Structured worker output paper_source_changes.{key} must be a boolean")
            if not isinstance(paper_source_changes.get("summary"), str):
                errors.append("Structured worker output paper_source_changes.summary must be a string")

    return errors


def structured_output_errors(task: dict, run_dir: Path, command_json: dict) -> list[str]:
    if not structured_output_required(task):
        return []
    raw_last_message = command_json.get("last_message_path")
    raw_final_output = command_json.get("final_output_path")
    if not isinstance(raw_last_message, str) or not raw_last_message:
        return ["Missing last_message_path in worker command metadata"]
    last_message_path = Path(raw_last_message)
    if not last_message_path.is_absolute():
        last_message_path = repo_root() / last_message_path
    if not last_message_path.exists():
        return [f"Missing structured final message: {last_message_path}"]

    try:
        payload = extract_json_object(last_message_path.read_text(encoding="utf-8"))
    except OSError as exc:
        return [f"Could not read structured final message: {exc}"]
    except ValidationError as exc:
        return [str(exc)]

    errors = validate_worker_final_output(payload, task)
    if errors:
        return errors

    final_output_path = Path(raw_final_output) if isinstance(raw_final_output, str) and raw_final_output else run_dir / "final_output.json"
    if not final_output_path.is_absolute():
        final_output_path = repo_root() / final_output_path
    write_json(final_output_path, payload)
    return []


def reviewer_output_errors(task: dict) -> list[str]:
    errors: list[str] = []
    for raw in task.get("required_outputs", []):
        if not isinstance(raw, str) or not raw.endswith(".json"):
            continue
        path = repo_root() / raw
        if not path.exists():
            continue
        try:
            review = read_json(path)
        except ValidationError as exc:
            errors.append(str(exc))
            continue
        review["_path"] = str(path)
        errors.extend(validate_review(review))
    return errors


def reviewer_scope_exempt(task: dict) -> bool:
    text = " ".join(
        str(task.get(key, ""))
        for key in ["objective", "notes", "handoff_summary"]
    ).lower()
    return any(
        phrase in text
        for phrase in [
            "debug worker infrastructure",
            "debug review infrastructure",
            "agent_runs",
            "assembled_prompts",
            "review performance",
            "worker performance",
        ]
    )


FORBIDDEN_REVIEWER_COMMAND_PATTERNS = [
    (re.compile(r"\brg\b[^\n\r]*\s\.(?:\s|$|['\";])"), "repo-wide `rg ... .` search"),
    (re.compile(r"\bfind\s+\.(?:\s|$|['\";])"), "repo-wide `find .` search"),
    (re.compile(r"\brg\b[^\n\r]*(?:^|\s|['\"])(?:agent_runs|tasks|tasks/assembled_prompts)(?:\s|$|/|['\"])"), "reviewer search of historical worker/task artifacts"),
    (re.compile(r"\bfind\s+(?:agent_runs|tasks|tasks/assembled_prompts)(?:\s|$|/|['\"])"), "reviewer find over historical worker/task artifacts"),
    (re.compile(r"\brg\b[^\n\r]*(?:^|\s|['\"])workstreams(?:\s|$|['\"])"), "reviewer search over all workstreams"),
    (re.compile(r"\bfind\s+workstreams(?:\s|$|['\"])"), "reviewer find over all workstreams"),
]


def reviewer_command_scope_errors(task: dict, run_dir: Path) -> list[str]:
    if task.get("agent_type") != "reviewer" or reviewer_scope_exempt(task):
        return []

    events_path = run_dir / "events.jsonl"
    if not events_path.exists():
        return []

    errors: list[str] = []
    seen: set[str] = set()
    with events_path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "command_execution":
                continue
            command = item.get("command")
            if not isinstance(command, str) or command in seen:
                continue
            seen.add(command)
            for pattern, label in FORBIDDEN_REVIEWER_COMMAND_PATTERNS:
                if pattern.search(command):
                    errors.append(
                        "Reviewer command scope violation: "
                        f"{label} at {events_path}:{line_number}: {command}"
                    )
                    break
    return errors


def reviewer_output_for_task(task: dict) -> dict | None:
    for raw in task.get("required_outputs", []):
        if not isinstance(raw, str) or not raw.endswith(".json"):
            continue
        path = repo_root() / raw
        if path.exists():
            review = read_json(path)
            review["_path"] = str(path)
            return review
    return None


def sync_reviewer_status(task: dict) -> None:
    if task.get("agent_type") != "reviewer":
        return
    review = reviewer_output_for_task(task)
    if review is None:
        return
    with scaffold_lock():
        workstream = project_workstream_path(task["workstream_id"])
        status = load_status(workstream)
        status["review_passed"] = review.get("decision") == "APPROVE"
        if status.get("type") == "computation":
            status["golden_values_approved"] = review.get("golden_values_approved") is True
        write_json(status_path(workstream), status)


def report_output_errors(task: dict) -> list[str]:
    if task.get("agent_type") == "reviewer":
        return []
    workstream = project_workstream_path(task["workstream_id"])
    if str(report_path(workstream).relative_to(repo_root())) not in task.get("required_outputs", []):
        return []
    return report_has_required_sections(report_path(workstream))


def finalize_task(task_path: Path, task: dict, final_status: str, summary: str) -> None:
    with scaffold_lock():
        timestamp = datetime.now(timezone.utc).isoformat()
        registry = load_agent_registry()
        agent_id = task.get("assigned_agent_id")
        found = False
        for agent in registry.get("agents", []):
            if isinstance(agent, dict) and agent.get("task_id") == task["task_id"] and agent.get("agent_id") == agent_id:
                agent["status"] = final_status
                agent["completed_at"] = timestamp
                agent["completion_summary"] = summary
                found = True
        if not found and agent_id:
            registry.setdefault("agents", []).append({
                "agent_id": agent_id,
                "role": task.get("agent_type"),
                "task_id": task["task_id"],
                "task_path": repo_relative_path(task_path),
                "workstream_id": task["workstream_id"],
                "status": final_status,
                "allowed_write_paths": task.get("allowed_write_paths", []),
                "spawned_at": None,
                "started_at": None,
                "completed_at": timestamp,
                "completion_summary": summary,
            })
        save_agent_registry(registry)

        release_write_locks(task["task_id"], final_status=final_status)
        task["status"] = "DONE" if final_status == "DONE" else "BLOCKED"
        task["spawn_status"] = final_status
        task["completion_summary"] = summary
        save_agent_task(task_path, task)
        append_message(
            "codex_cli_worker_collected",
            task["workstream_id"],
            f"Collected Codex CLI worker {agent_id} for task {task['task_id']} with status {final_status}.",
            agent_id=agent_id,
            task_path=repo_relative_path(task_path),
            completion_summary=summary,
        )


def block_or_record(task: dict, errors: list[str], no_block_workstream: bool) -> str:
    evidence = "\n".join(f"- {error}" for error in errors)
    reason = "Codex CLI worker task failed collection gates."
    next_action = "Inspect the run directory, fix the task outputs, then create or respawn a revised task."
    workstream = project_workstream_path(task["workstream_id"])
    if no_block_workstream:
        path = record_failed_exploration(
            task["workstream_id"],
            reason,
            f"Collect task {task['task_id']}.",
            evidence,
            next_action,
        )
        return str(path.relative_to(repo_root()))

    status = load_status(workstream)
    if status.get("status") == "COMPLETE":
        path = record_failed_exploration(
            task["workstream_id"],
            reason,
            f"Collect task {task['task_id']} for an already complete workstream.",
            evidence,
            next_action,
        )
        return str(path.relative_to(repo_root()))

    path = mark_status_blocked(
        workstream,
        status,
        reason,
        f"Collect task {task['task_id']}.",
        evidence,
        next_action,
        event_type="codex_cli_worker_collection_failed",
    )
    return str(path.relative_to(repo_root()))


def fail_collection(
    task_path: Path,
    task: dict,
    run_dir: Path | None,
    errors: list[str],
    no_block_workstream: bool,
) -> int:
    failure_note = block_or_record(task, errors, no_block_workstream)
    if run_dir is not None and run_dir.exists():
        write_json(run_dir / "collection_result.json", {
            "task_id": task["task_id"],
            "passed": False,
            "errors": errors,
            "failure_note": failure_note,
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })
    finalize_task(task_path, task, "BLOCKED", f"Collection failed; see {failure_note}.")
    print(f"Collection failed for {task['task_id']}.")
    for error in errors:
        print(f"- {error}")
    print(f"Recorded failed exploration: {failure_note}")
    return 1


def main() -> int:
    args = parse_args()
    task_path = task_path_from_arg(args.task)
    try:
        task = load_agent_task(task_path)
    except ValidationError as exc:
        print(exc)
        return 1

    try:
        run_dir = resolve_run_dir(task, args.run_dir)
    except ValidationError as exc:
        return fail_collection(task_path, task, None, [str(exc)], args.no_block_workstream)

    run_json_path = run_dir / "run.json"
    exit_status_path = run_dir / "exit_status.json"
    if not run_json_path.exists():
        return fail_collection(
            task_path,
            task,
            run_dir,
            [f"Missing run metadata: {run_json_path}"],
            args.no_block_workstream,
        )

    try:
        run_json = read_json(run_json_path)
    except ValidationError as exc:
        return fail_collection(task_path, task, run_dir, [str(exc)], args.no_block_workstream)

    if not exit_status_path.exists():
        pid = run_json.get("wrapper_pid")
        if isinstance(pid, int) and pid_is_running(pid):
            print(f"Worker for {task['task_id']} is still running (pid {pid}).")
            return 2
        return fail_collection(
            task_path,
            task,
            run_dir,
            [f"Worker process is not running, but no exit status exists: {exit_status_path}"],
            args.no_block_workstream,
        )

    try:
        exit_status = read_json(exit_status_path)
        errors: list[str] = []
        if exit_status.get("exit_code") != 0:
            if exit_status.get("timed_out") is True:
                errors.append(
                    "Codex CLI worker timed out "
                    f"after {exit_status.get('timeout_seconds')} seconds"
                )
            else:
                errors.append(f"Codex CLI worker exited with code {exit_status.get('exit_code')}")

        test_result: dict | None = None
        computation_workstream: Path | None = None
        if task.get("agent_type") == "computation":
            computation_workstream = project_workstream_path(task["workstream_id"])
            test_result = run_workstream_tests(computation_workstream, update_status=False)
            if test_result.get("passed") is not True:
                errors.append(
                    f"Computation tests failed during collection: exit_code={test_result.get('exit_code')}, "
                    f"reason={test_result.get('reason')}"
                )

        before = read_json(run_dir / "pre_manifest.json")
        after = build_file_manifest()
        changes = manifest_changes(before, after)
        diff_errors = worker_diff_errors(task, changes)
        errors.extend(diff_errors)
        errors.extend(source_guard_errors())
        write_json(run_dir / "diff_result.json", {
            "task_id": task["task_id"],
            "changes": changes,
            "allowed_write_paths": task["allowed_write_paths"],
            "passed": not diff_errors,
            "errors": diff_errors,
        })

        errors.extend(required_output_errors(task))
        errors.extend(report_output_errors(task))
        errors.extend(structured_output_errors(task, run_dir, read_json(run_dir / "command.json")))

        if task.get("agent_type") == "reviewer":
            errors.extend(reviewer_output_errors(task))
            errors.extend(reviewer_command_scope_errors(task, run_dir))

        if errors:
            return fail_collection(task_path, task, run_dir, errors, args.no_block_workstream)

        if computation_workstream is not None and test_result is not None:
            with scaffold_lock():
                status = load_status(computation_workstream)
                status["tests_passed"] = test_result.get("passed") is True
                write_json(status_path(computation_workstream), status)

        sync_reviewer_status(task)

        write_json(run_dir / "collection_result.json", {
            "task_id": task["task_id"],
            "passed": True,
            "errors": [],
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })
        finalize_task(task_path, task, "DONE", "Collection gates passed.")
    except ValidationError as exc:
        return fail_collection(task_path, task, run_dir, [str(exc)], args.no_block_workstream)

    print(f"Collected {task['task_id']} successfully.")
    print(f"Run directory: {run_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
