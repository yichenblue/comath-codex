from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import traceback
import unittest
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


ALLOWED_STATUSES = {
    "DRAFT",
    "APPROVED",
    "RUNNING",
    "REVIEWING",
    "BLOCKED",
    "COMPLETE",
}

ALLOWED_DECISIONS = {
    "APPROVE",
    "REQUEST_CHANGES",
    "BLOCK",
}

REQUIRED_REPORT_SECTIONS = [
    "## Summary",
    "## Inputs",
    "## Actions Taken",
    "## Claims",
    "## Evidence And Artifacts",
    "## Error Decomposition",
    "## Uncertainty And Gaps",
    "## Failed Attempts",
    "## Next Steps",
]

REQUIRED_ERROR_DECOMPOSITION_LABELS = {
    "source-setting error": re.compile(r"\bsource[- ]setting error\b", re.IGNORECASE),
    "finite-n / Monte Carlo error": re.compile(
        r"(finite[- ]?\\?\(?n\\?\)?|finite[- ]?n).*monte\s+carlo|monte\s+carlo.*(finite[- ]?\\?\(?n\\?\)?|finite[- ]?n)",
        re.IGNORECASE,
    ),
    "numerical quadrature / branch error": re.compile(
        r"numerical\s+quadrature.*branch|branch.*numerical\s+quadrature",
        re.IGNORECASE,
    ),
    "theorem-level discrepancy": re.compile(r"\btheorem[- ]level discrepancy\b", re.IGNORECASE),
}

CLAIM_ID_PATTERN = re.compile(r"\bC-[A-Za-z0-9][A-Za-z0-9_-]*\b")
APPROVED_CLAIM_PATTERN = re.compile(r"\\approvedclaim\{([^{}]+)\}")

AGENT_TYPES = {
    "literature",
    "proof",
    "computation",
    "reviewer",
    "synthesis",
    "workstream_coordinator",
}

AGENT_PROMPT_FILES = {
    "literature": "prompts/literature_agent.md",
    "proof": "prompts/proof_agent.md",
    "computation": "prompts/computation_agent.md",
    "reviewer": "prompts/reviewer_agent.md",
    "synthesis": "prompts/synthesis_agent.md",
    "workstream_coordinator": "prompts/workstream_coordinator.md",
}

TASK_STATUSES = {
    "DRAFT",
    "READY",
    "IN_PROGRESS",
    "DONE",
    "BLOCKED",
}

EXECUTION_MODES = {
    "local_thread",
    "codex_worker",
    "codex_cli",
    "external",
}

SPAWN_STATUSES = {
    "NOT_SPAWNED",
    "SPAWNED",
    "RUNNING",
    "DONE",
    "BLOCKED",
    "CLOSED",
}

AGENT_REGISTRY_STATUSES = {
    "SPAWNED",
    "RUNNING",
    "DONE",
    "BLOCKED",
    "CLOSED",
}


COMPUTATION_TRIGGER_KEYWORDS = [
    "decomposition",
    "leading order",
    "leading-order",
    "subleading",
    "o_{\\mathbb p}",
    "o_{\\mathbb p}(n)",
    "deterministic limit",
    "deterministic equivalence",
    "deterministic equivalent",
    "resolvent",
    "woodbury",
    "kernel",
    "matrix identity",
    "matrix simulation",
    "numerical simulation",
    "specialization",
    "formula correctness",
    "lemma statement patch",
    "patch the lemma",
    "source has been patched",
]

LITERATURE_TRIGGER_KEYWORDS = [
    "literature review",
    "literature search",
    "literature",
    "reference",
    "references",
    "citation",
    "citations",
    "prior work",
    "previous work",
    "related work",
    "known result",
    "known results",
    "known theorem",
    "theorem statement",
    "theorem statements",
    "source paper",
    "external paper",
    "arxiv",
    "bibliography",
    "survey",
    "find papers",
    "exact statement",
    "exact statements",
    "existing result",
    "existing results",
]

GUARDED_SOURCE_EXTENSIONS = {
    ".tex",
    ".bib",
    ".sty",
    ".cls",
    ".bst",
    ".bbx",
    ".cbx",
    ".ltx",
}

SOURCE_MANIFEST_VERSION = 1
DEFAULT_TEST_TIMEOUT_SECONDS = 300
TEST_TIMEOUT_EXIT_CODE = 124

WORKSTREAM_AGENT_TYPES = {
    "literature": "literature",
    "proof": "proof",
    "computation": "computation",
    "synthesis": "synthesis",
    "general": "workstream_coordinator",
}


class ValidationError(Exception):
    pass


_SCAFFOLD_LOCK_DEPTH = 0
_SCAFFOLD_LOCK_HANDLE: Any | None = None


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def project_state_path() -> Path:
    return repo_root() / "state" / "project_state.json"


def message_queue_path() -> Path:
    return repo_root() / "state" / "message_queue.jsonl"


def agent_registry_path() -> Path:
    return repo_root() / "state" / "agent_registry.json"


def write_locks_path() -> Path:
    return repo_root() / "state" / "write_locks.json"


def scaffold_lock_path() -> Path:
    return repo_root() / "state" / ".scaffold.lock"


@contextlib.contextmanager
def scaffold_lock() -> Any:
    global _SCAFFOLD_LOCK_DEPTH, _SCAFFOLD_LOCK_HANDLE
    if _SCAFFOLD_LOCK_DEPTH > 0:
        _SCAFFOLD_LOCK_DEPTH += 1
        try:
            yield
        finally:
            _SCAFFOLD_LOCK_DEPTH -= 1
        return

    path = scaffold_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        _SCAFFOLD_LOCK_HANDLE = handle
        _SCAFFOLD_LOCK_DEPTH = 1
        yield
    finally:
        _SCAFFOLD_LOCK_DEPTH = 0
        _SCAFFOLD_LOCK_HANDLE = None
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def source_manifest_path() -> Path:
    return repo_root() / "state" / "source_manifest.json"


def goal_approval_records_path() -> Path:
    return repo_root() / "state" / "goal_approval_records.json"


def tasks_dir() -> Path:
    return repo_root() / "tasks"


def assembled_prompts_dir() -> Path:
    return repo_root() / "tasks" / "assembled_prompts"


def agent_runs_dir() -> Path:
    return repo_root() / "agent_runs"


def read_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ValidationError(f"Missing JSON file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValidationError(f"Expected JSON object in {path}")
    return data


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_name = handle.name
            json.dump(data, handle, indent=2, sort_keys=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if tmp_name:
            tmp_path = Path(tmp_name)
            if tmp_path.exists():
                tmp_path.unlink()


def load_project_state() -> dict[str, Any]:
    return read_json(project_state_path())


def save_project_state(project_state: dict[str, Any]) -> None:
    with scaffold_lock():
        write_json(project_state_path(), project_state)


def load_goal_approval_records() -> dict[str, Any]:
    path = goal_approval_records_path()
    if not path.exists():
        return {
            "version": 1,
            "gate": "goal_user_confirmation_required",
            "legacy_approved_goals": [],
            "approvals": [],
        }
    records = read_json(path)
    records.setdefault("version", 1)
    records.setdefault("gate", "goal_user_confirmation_required")
    records.setdefault("legacy_approved_goals", [])
    records.setdefault("approvals", [])
    return records


def save_goal_approval_records(records: dict[str, Any]) -> None:
    with scaffold_lock():
        write_json(goal_approval_records_path(), records)


def goal_is_confirmed_approved(goal_id: str, records: dict[str, Any] | None = None) -> bool:
    data = records if records is not None else load_goal_approval_records()
    legacy = data.get("legacy_approved_goals", [])
    if isinstance(legacy, list) and goal_id in legacy:
        return True
    approvals = data.get("approvals", [])
    if not isinstance(approvals, list):
        return False
    for approval in approvals:
        if not isinstance(approval, dict):
            continue
        if approval.get("goal_id") != goal_id:
            continue
        if approval.get("approval_type") != "user_confirmation":
            continue
        confirmation = approval.get("user_confirmation")
        if isinstance(confirmation, str) and confirmation.strip():
            return True
    return False


def goal_approval_gate_errors(project_state: dict[str, Any], source: Path) -> list[str]:
    errors: list[str] = []
    goals = project_state.get("goals", [])
    if not isinstance(goals, list):
        return [f"{source}: goals must be a list"]

    records = load_goal_approval_records()
    legacy = records.get("legacy_approved_goals", [])
    approvals = records.get("approvals", [])
    if not isinstance(legacy, list) or not all(isinstance(item, str) for item in legacy):
        errors.append(f"{goal_approval_records_path()}: legacy_approved_goals must be a list of strings")
        legacy = []
    if not isinstance(approvals, list):
        errors.append(f"{goal_approval_records_path()}: approvals must be a list")
        approvals = []

    known_goals: set[str] = set()
    seen_goals: set[str] = set()
    for goal in goals:
        if not isinstance(goal, dict):
            errors.append(f"{source}: goal entries must be objects")
            continue
        goal_id = goal.get("id")
        if not isinstance(goal_id, str) or not goal_id:
            errors.append(f"{source}: goal entry has missing id")
            continue
        if goal_id in seen_goals:
            errors.append(f"{source}: duplicate goal id {goal_id}")
        seen_goals.add(goal_id)
        known_goals.add(goal_id)
        status = goal.get("status")
        if status not in {"DRAFT", "APPROVED"}:
            errors.append(f"{source}: goal {goal_id} has invalid status {status!r}")
        if status == "APPROVED" and not goal_is_confirmed_approved(goal_id, records):
            errors.append(
                f"{source}: goal {goal_id} is APPROVED but has no user confirmation "
                f"record in {goal_approval_records_path().relative_to(repo_root())}. "
                "New goals must remain DRAFT until approved with scripts/approve_goal.py."
            )

    for goal_id in legacy:
        if goal_id not in known_goals:
            errors.append(f"{goal_approval_records_path()}: legacy goal {goal_id} is not in project_state.json")

    approved_record_goals: set[str] = set()
    for index, approval in enumerate(approvals):
        if not isinstance(approval, dict):
            errors.append(f"{goal_approval_records_path()}: approvals[{index}] must be an object")
            continue
        goal_id = approval.get("goal_id")
        if not isinstance(goal_id, str) or not goal_id:
            errors.append(f"{goal_approval_records_path()}: approvals[{index}] has missing goal_id")
            continue
        if goal_id not in known_goals:
            errors.append(f"{goal_approval_records_path()}: approval references unknown goal {goal_id}")
        if approval.get("approval_type") != "user_confirmation":
            errors.append(f"{goal_approval_records_path()}: approval for {goal_id} must have approval_type=user_confirmation")
        confirmation = approval.get("user_confirmation")
        if not isinstance(confirmation, str) or not confirmation.strip():
            errors.append(f"{goal_approval_records_path()}: approval for {goal_id} must include nonempty user_confirmation")
        if goal_id in approved_record_goals:
            errors.append(f"{goal_approval_records_path()}: duplicate approval record for {goal_id}")
        approved_record_goals.add(goal_id)

    return errors


def validate_goal_approval_gate(project_state: dict[str, Any], source: Path | None = None) -> None:
    errors = goal_approval_gate_errors(project_state, source or project_state_path())
    if errors:
        raise ValidationError("\n".join(errors))


def load_agent_registry() -> dict[str, Any]:
    path = agent_registry_path()
    if not path.exists():
        return {"agents": []}
    registry = read_json(path)
    if "agents" not in registry:
        registry["agents"] = []
    if not isinstance(registry["agents"], list):
        raise ValidationError(f"{path}: agents must be a list")
    return registry


def save_agent_registry(registry: dict[str, Any]) -> None:
    with scaffold_lock():
        write_json(agent_registry_path(), registry)


def load_write_locks() -> dict[str, Any]:
    path = write_locks_path()
    if not path.exists():
        return {"locks": []}
    locks = read_json(path)
    if "locks" not in locks:
        locks["locks"] = []
    if not isinstance(locks["locks"], list):
        raise ValidationError(f"{path}: locks must be a list")
    return locks


def save_write_locks(locks: dict[str, Any]) -> None:
    with scaffold_lock():
        write_json(write_locks_path(), locks)


def append_message(event_type: str, workstream_id: str | None, message: str, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "type": event_type,
        "workstream_id": workstream_id,
        "message": message,
    }
    payload.update(extra)
    path = message_queue_path()
    line = json.dumps(payload, sort_keys=False) + "\n"
    with scaffold_lock():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)


def sync_project_workstream_status(workstream_id: str, status_value: str) -> None:
    if status_value not in ALLOWED_STATUSES:
        raise ValidationError(f"Invalid status {status_value!r}")
    with scaffold_lock():
        project_state = load_project_state()
        updated = False
        for item in project_state.get("workstreams", []):
            if item.get("id") == workstream_id:
                item["status"] = status_value
                updated = True
                break
        if not updated:
            raise ValidationError(f"Workstream {workstream_id!r} not found in project_state.json")
        save_project_state(project_state)


def add_project_workstream(workstream_id: str, rel_path: str, status_value: str) -> None:
    if status_value not in ALLOWED_STATUSES:
        raise ValidationError(f"Invalid status {status_value!r}")
    with scaffold_lock():
        project_state = load_project_state()
        workstreams = project_state.setdefault("workstreams", [])
        if not isinstance(workstreams, list):
            raise ValidationError("project_state.json field workstreams must be a list")
        if any(item.get("id") == workstream_id for item in workstreams if isinstance(item, dict)):
            raise ValidationError(f"Workstream {workstream_id!r} already exists in project_state.json")
        workstreams.append({
            "id": workstream_id,
            "path": rel_path,
            "status": status_value,
        })
        save_project_state(project_state)


def project_workstream_path(workstream_id: str) -> Path:
    project_state = load_project_state()
    for item in project_state.get("workstreams", []):
        if isinstance(item, dict) and item.get("id") == workstream_id:
            rel_path = item.get("path")
            if not isinstance(rel_path, str):
                raise ValidationError(f"Workstream {workstream_id!r} has invalid path in project_state.json")
            return repo_root() / rel_path
    raise ValidationError(f"Workstream {workstream_id!r} not found in project_state.json")


def ensure_relative_repo_path(value: str, field: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        raise ValidationError(f"{field} must be repo-relative, got absolute path {value!r}")
    if ".." in path.parts:
        raise ValidationError(f"{field} must not contain '..': {value!r}")
    return path


def validate_agent_task(task: dict[str, Any], source: Path) -> list[str]:
    errors: list[str] = []
    required = [
        "task_id",
        "agent_type",
        "workstream_id",
        "status",
        "objective",
        "input_files",
        "allowed_write_paths",
        "required_outputs",
        "success_criteria",
    ]
    for key in required:
        if key not in task:
            errors.append(f"{source}: missing key {key}")

    if task.get("agent_type") not in AGENT_TYPES:
        errors.append(f"{source}: invalid agent_type {task.get('agent_type')!r}")
    if task.get("status") not in TASK_STATUSES:
        errors.append(f"{source}: invalid task status {task.get('status')!r}")

    execution_mode = task.get("execution_mode", "local_thread")
    if execution_mode not in EXECUTION_MODES:
        errors.append(f"{source}: invalid execution_mode {execution_mode!r}")
    spawn_status = task.get("spawn_status", "NOT_SPAWNED")
    if spawn_status not in SPAWN_STATUSES:
        errors.append(f"{source}: invalid spawn_status {spawn_status!r}")
    assigned_agent_id = task.get("assigned_agent_id")
    if assigned_agent_id is not None and not isinstance(assigned_agent_id, str):
        errors.append(f"{source}: assigned_agent_id must be a string or null")
    for optional_id in ["parent_task_id", "parent_workstream_id", "subagent_request_id"]:
        value = task.get(optional_id)
        if value is not None and not isinstance(value, str):
            errors.append(f"{source}: {optional_id} must be a string or null")
    for optional_text in ["handoff_summary", "completion_summary"]:
        value = task.get(optional_text, "")
        if not isinstance(value, str):
            errors.append(f"{source}: {optional_text} must be a string")

    list_fields = ["input_files", "allowed_write_paths", "required_outputs", "success_criteria"]
    for field in list_fields:
        if field in task and not isinstance(task[field], list):
            errors.append(f"{source}: {field} must be a list")

    workstream_id = task.get("workstream_id")
    workstream_path: Path | None = None
    if isinstance(workstream_id, str) and workstream_id:
        try:
            workstream_path = project_workstream_path(workstream_id)
        except ValidationError as exc:
            errors.append(str(exc))
    else:
        errors.append(f"{source}: workstream_id must be a nonempty string")

    root = repo_root()
    for field in ["input_files", "allowed_write_paths", "required_outputs"]:
        for raw in task.get(field, []) if isinstance(task.get(field), list) else []:
            if not isinstance(raw, str):
                errors.append(f"{source}: {field} entries must be strings")
                continue
            try:
                rel_path = ensure_relative_repo_path(raw, field)
            except ValidationError as exc:
                errors.append(f"{source}: {exc}")
                continue
            full_path = root / rel_path
            if field == "input_files" and not full_path.exists():
                errors.append(f"{source}: input file does not exist: {raw}")
            if field in {"allowed_write_paths", "required_outputs"} and workstream_path is not None:
                try:
                    full_path.resolve().relative_to(workstream_path.resolve())
                except ValueError:
                    if task.get("agent_type") != "synthesis":
                        errors.append(
                            f"{source}: {field} entry must stay inside assigned workstream "
                            f"for non-synthesis tasks: {raw}"
                        )

    prompt_file = AGENT_PROMPT_FILES.get(str(task.get("agent_type")))
    if prompt_file and not (root / prompt_file).exists():
        errors.append(f"{source}: prompt file missing: {prompt_file}")

    return errors


def load_agent_task(path: Path) -> dict[str, Any]:
    task = read_json(path)
    errors = validate_agent_task(task, path)
    if errors:
        raise ValidationError("\n".join(errors))
    return task


def save_agent_task(path: Path, task: dict[str, Any]) -> None:
    errors = validate_agent_task(task, path)
    if errors:
        raise ValidationError("\n".join(errors))
    write_json(path, task)


def task_path_for_id(task_id: str) -> Path:
    return tasks_dir() / f"{task_id}.json"


def all_agent_tasks() -> list[tuple[Path, dict[str, Any]]]:
    tasks: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(tasks_dir().glob("task_*.json")):
        try:
            task = load_agent_task(path)
        except ValidationError:
            continue
        tasks.append((path, task))
    return tasks


def completed_codex_cli_tasks(workstream_id: str, agent_type: str) -> list[dict[str, Any]]:
    completed: list[dict[str, Any]] = []
    for _, task in all_agent_tasks():
        runner = task.get("runner")
        if not isinstance(runner, dict):
            runner = {}
        if (
            task.get("workstream_id") == workstream_id
            and task.get("agent_type") == agent_type
            and task.get("execution_mode") == "codex_cli"
            and task.get("status") == "DONE"
            and task.get("spawn_status") == "DONE"
            and runner.get("kind") == "codex_cli"
        ):
            completed.append(task)
    return completed


def independent_worker_provenance_errors(workstream: Path, status: dict[str, Any]) -> list[str]:
    if status.get("requires_independent_workers") is not True:
        return []

    workstream_id = str(status.get("id"))
    workstream_type = str(status.get("type"))
    errors: list[str] = []

    expected_agent = WORKSTREAM_AGENT_TYPES.get(workstream_type)
    if expected_agent is None:
        errors.append(f"{workstream}: unknown workstream type {workstream_type!r}")
    elif not completed_codex_cli_tasks(workstream_id, expected_agent):
        errors.append(
            f"{workstream}: requires independent workers but has no completed codex_cli "
            f"{expected_agent} task"
        )

    if status.get("review_required") is True and not completed_codex_cli_tasks(workstream_id, "reviewer"):
        errors.append(
            f"{workstream}: requires independent workers but has no completed codex_cli reviewer task"
        )

    return errors


def task_requires_computation(text: str) -> tuple[bool, list[str]]:
    lowered = text.lower()
    hits: list[str] = []
    for keyword in COMPUTATION_TRIGGER_KEYWORDS:
        if keyword.lower() in lowered:
            hits.append(keyword)
    return bool(hits), sorted(set(hits))


def task_requires_literature(text: str) -> tuple[bool, list[str]]:
    lowered = text.lower()
    hits: list[str] = []
    for keyword in LITERATURE_TRIGGER_KEYWORDS:
        if keyword.lower() in lowered:
            hits.append(keyword)
    return bool(hits), sorted(set(hits))


def task_path_from_arg(value: str) -> Path:
    path = Path(value)
    if path.suffix == ".json" or "/" in value:
        return path.resolve()
    return task_path_for_id(value).resolve()


def repo_relative_path(path: Path) -> str:
    return str(path.resolve().relative_to(repo_root()))


def should_skip_manifest_path(path: Path, base: Path) -> bool:
    try:
        rel = path.resolve().relative_to(base.resolve())
    except ValueError:
        return True
    parts = rel.parts
    if not parts:
        return True
    if parts[0] == "agent_runs":
        return True
    if "__pycache__" in parts:
        return True
    if path.name in {".DS_Store"}:
        return True
    if path.suffix in {".pyc", ".pyo"}:
        return True
    return False


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def guarded_source_file(path: Path) -> bool:
    return path.is_file() and path.suffix.lower() in GUARDED_SOURCE_EXTENSIONS


def source_root_has_guarded_files(path: Path) -> bool:
    if not path.is_dir():
        return False
    for candidate in path.rglob("*"):
        if guarded_source_file(candidate):
            return True
    return False


def discover_default_source_roots() -> list[str]:
    """Discover top-level LaTeX source folders adjacent to this scaffold."""
    workspace = repo_root().parent.resolve()
    roots: list[str] = []
    if not workspace.exists():
        return roots
    for child in sorted(workspace.iterdir(), key=lambda item: item.name):
        if child.resolve() == repo_root().resolve():
            continue
        if child.is_dir() and source_root_has_guarded_files(child):
            roots.append(str(Path("..") / child.name))
    return roots


def resolve_guarded_source_root(raw: str) -> Path:
    raw_path = Path(raw)
    path = raw_path if raw_path.is_absolute() else repo_root() / raw_path
    resolved = path.resolve()
    workspace = repo_root().parent.resolve()
    try:
        resolved.relative_to(workspace)
    except ValueError as exc:
        raise ValidationError(
            f"Guarded source root must stay under workspace {workspace}: {raw!r}"
        ) from exc
    if resolved == repo_root().resolve():
        raise ValidationError("Guarded source root must not be the scaffold repository itself")
    if not resolved.exists():
        raise ValidationError(f"Guarded source root does not exist: {raw!r}")
    if not resolved.is_dir():
        raise ValidationError(f"Guarded source root is not a directory: {raw!r}")
    return resolved


def source_manifest_roots(manifest: dict[str, Any] | None = None) -> list[str]:
    if manifest is None:
        path = source_manifest_path()
        if path.exists():
            manifest = read_json(path)
    if manifest is not None:
        roots = manifest.get("guarded_roots", [])
        if not isinstance(roots, list) or not all(isinstance(item, str) for item in roots):
            raise ValidationError(f"{source_manifest_path()}: guarded_roots must be a list of strings")
        return sorted(set(roots))
    return discover_default_source_roots()


def source_manifest_file_key(root_entry: str, root_path: Path, file_path: Path) -> str:
    rel = file_path.resolve().relative_to(root_path.resolve())
    return str(Path(root_entry) / rel)


def build_source_file_manifest(guarded_roots: list[str] | None = None) -> dict[str, Any]:
    roots = sorted(set(guarded_roots if guarded_roots is not None else source_manifest_roots()))
    files: dict[str, Any] = {}
    root_errors: list[str] = []
    for root_entry in roots:
        try:
            root_path = resolve_guarded_source_root(root_entry)
        except ValidationError as exc:
            root_errors.append(str(exc))
            continue
        for path in sorted(root_path.rglob("*")):
            if not guarded_source_file(path):
                continue
            key = source_manifest_file_key(root_entry, root_path, path)
            stat = path.stat()
            files[key] = {
                "sha256": file_sha256(path),
                "size": stat.st_size,
            }
    return {
        "version": SOURCE_MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "guarded_roots": roots,
        "root_errors": root_errors,
        "files": files,
    }


def write_source_manifest(guarded_roots: list[str] | None = None) -> dict[str, Any]:
    manifest = build_source_file_manifest(guarded_roots)
    if manifest.get("root_errors"):
        raise ValidationError("\n".join(str(item) for item in manifest["root_errors"]))
    path = source_manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, manifest)
    return manifest


def build_file_manifest(base: Path | None = None) -> dict[str, Any]:
    root = (base or repo_root()).resolve()
    files: dict[str, Any] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if should_skip_manifest_path(path, root):
            continue
        rel = str(path.resolve().relative_to(root))
        stat = path.stat()
        files[rel] = {
            "sha256": file_sha256(path),
            "size": stat.st_size,
        }
    if root == repo_root().resolve() and source_manifest_path().exists():
        source_manifest = build_source_file_manifest()
        for key, value in source_manifest.get("files", {}).items():
            files[key] = value
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "files": files,
    }


def manifest_changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, list[str]]:
    before_files = before.get("files", {})
    after_files = after.get("files", {})
    if not isinstance(before_files, dict) or not isinstance(after_files, dict):
        raise ValidationError("Manifest files fields must be JSON objects")

    before_paths = set(before_files)
    after_paths = set(after_files)
    added = sorted(after_paths - before_paths)
    deleted = sorted(before_paths - after_paths)
    modified = sorted(
        path
        for path in before_paths & after_paths
        if before_files[path].get("sha256") != after_files[path].get("sha256")
        or before_files[path].get("size") != after_files[path].get("size")
    )
    return {
        "added": added,
        "modified": modified,
        "deleted": deleted,
    }


def source_guard_errors() -> list[str]:
    path = source_manifest_path()
    if not path.exists():
        discovered = discover_default_source_roots()
        if discovered:
            return [
                f"{path}: missing guarded source manifest. Run "
                "`python3 scripts/init_source_manifest.py` before spawning workers or applying source patches."
            ]
        return []

    try:
        expected = read_json(path)
        roots = source_manifest_roots(expected)
        current = build_source_file_manifest(roots)
    except ValidationError as exc:
        return [str(exc)]

    errors: list[str] = []
    if expected.get("version") != SOURCE_MANIFEST_VERSION:
        errors.append(
            f"{path}: unsupported source manifest version {expected.get('version')!r}; "
            f"expected {SOURCE_MANIFEST_VERSION}"
        )
    if current.get("root_errors"):
        errors.extend(str(item) for item in current["root_errors"])

    changes = manifest_changes(expected, current)
    drift = any(changes.get(category) for category in ["added", "modified", "deleted"])
    if drift:
        errors.append(
            "Guarded paper source drift detected. Do not edit paper source directly; "
            "write a proposed patch under the workstream artifacts and apply it only with "
            "`scripts/apply_approved_patch.py` after review/computation gates pass."
        )
        for category in ["added", "modified", "deleted"]:
            for item in changes.get(category, []):
                errors.append(f"guarded source {category}: {item}")
    return errors


def path_is_allowed_by_task(path: str, task: dict[str, Any]) -> bool:
    for allowed in task.get("allowed_write_paths", []):
        if not isinstance(allowed, str):
            continue
        try:
            if paths_overlap(path, allowed):
                return True
        except ValidationError:
            continue
    return False


def worker_diff_errors(task: dict[str, Any], changes: dict[str, list[str]]) -> list[str]:
    errors: list[str] = []
    for category in ["added", "modified", "deleted"]:
        for path in changes.get(category, []):
            if not path_is_allowed_by_task(path, task):
                errors.append(
                    f"{category}: {path} is outside allowed_write_paths for task {task.get('task_id')}"
                )
    return errors


def paths_overlap(first: str, second: str) -> bool:
    root = repo_root()
    first_path = (root / ensure_relative_repo_path(first, "path")).resolve()
    second_path = (root / ensure_relative_repo_path(second, "path")).resolve()
    try:
        first_path.relative_to(second_path)
        return True
    except ValueError:
        pass
    try:
        second_path.relative_to(first_path)
        return True
    except ValueError:
        return False


def active_lock_status(status: str | None) -> bool:
    return status in {"SPAWNED", "RUNNING"}


def write_lock_conflicts(paths: list[str], owner_task_id: str | None = None) -> list[str]:
    locks = load_write_locks()
    conflicts: list[str] = []
    for existing in locks.get("locks", []):
        if not isinstance(existing, dict) or not active_lock_status(existing.get("status")):
            continue
        if owner_task_id and existing.get("task_id") == owner_task_id:
            continue
        existing_path = existing.get("path")
        if not isinstance(existing_path, str):
            continue
        for path in paths:
            if paths_overlap(path, existing_path):
                conflicts.append(
                    f"{path} overlaps active lock {existing_path} "
                    f"held by task {existing.get('task_id')} / agent {existing.get('agent_id')}"
                )
    return conflicts


def acquire_write_locks(task: dict[str, Any], agent_id: str, status_value: str = "SPAWNED") -> None:
    if status_value not in {"SPAWNED", "RUNNING"}:
        raise ValidationError(f"Invalid initial lock status {status_value!r}")
    task_id = str(task["task_id"])
    paths = [str(path) for path in task.get("allowed_write_paths", [])]
    with scaffold_lock():
        conflicts = write_lock_conflicts(paths, owner_task_id=task_id)
        if conflicts:
            raise ValidationError("Write lock conflict:\n" + "\n".join(f"- {item}" for item in conflicts))

        locks = load_write_locks()
        timestamp = datetime.now(timezone.utc).isoformat()
        retained = [
            lock for lock in locks.get("locks", [])
            if not (isinstance(lock, dict) and lock.get("task_id") == task_id and active_lock_status(lock.get("status")))
        ]
        for path in paths:
            retained.append({
                "path": path,
                "task_id": task_id,
                "agent_id": agent_id,
                "workstream_id": task.get("workstream_id"),
                "status": status_value,
                "acquired_at": timestamp,
                "released_at": None,
            })
        locks["locks"] = retained
        save_write_locks(locks)


def release_write_locks(task_id: str, final_status: str = "DONE") -> None:
    if final_status not in {"DONE", "BLOCKED", "CLOSED"}:
        raise ValidationError(f"Invalid final lock status {final_status!r}")
    with scaffold_lock():
        locks = load_write_locks()
        timestamp = datetime.now(timezone.utc).isoformat()
        for lock in locks.get("locks", []):
            if isinstance(lock, dict) and lock.get("task_id") == task_id and active_lock_status(lock.get("status")):
                lock["status"] = final_status
                lock["released_at"] = timestamp
        save_write_locks(locks)


def safe_id(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    normalized = re.sub(r"_+", "_", normalized).strip("_")
    if not normalized:
        raise ValidationError("Generated empty id")
    return normalized


def status_path(workstream: Path) -> Path:
    return workstream / "status.json"


def report_path(workstream: Path) -> Path:
    return workstream / "report.md"


def review_dir(workstream: Path) -> Path:
    return workstream / "review"


def artifacts_dir(workstream: Path) -> Path:
    return workstream / "artifacts"


def test_run_path(workstream: Path) -> Path:
    return artifacts_dir(workstream) / "test_run.json"


def source_setting_manifest_path(workstream: Path) -> Path:
    return artifacts_dir(workstream) / "source_setting_manifest.json"


def source_faithfulness_tests_path(workstream: Path) -> Path:
    return artifacts_dir(workstream) / "source_faithfulness_tests.json"


def formula_trace_path(workstream: Path) -> Path:
    return artifacts_dir(workstream) / "formula_trace.json"


def raw_object_validation_path(workstream: Path) -> Path:
    return artifacts_dir(workstream) / "raw_object_validation.json"


def load_status(workstream: Path) -> dict[str, Any]:
    return read_json(status_path(workstream))


SOURCE_SETTING_REQUIRED_TOP_LEVEL = [
    "schema_version",
    "manifest_status",
    "dimension_scaling",
    "random_objects",
    "hermite_convention",
    "normalization",
    "compared_objects",
]

SOURCE_SETTING_DIMENSION_KEYS = ["n", "d", "p", "q", "phi", "psi", "relationships"]
SOURCE_SETTING_RANDOM_OBJECTS = ["F0", "F1", "beta", "y", "y_tilde"]
SOURCE_SETTING_HERMITE_KEYS = ["family", "H2", "c_star_sq", "coefficient_norm"]
SOURCE_SETTING_NORMALIZATION_KEYS = ["matrix_scale", "ridge_form", "notes"]
SOURCE_SETTING_OBJECT_TYPES = {
    "raw_finite_object",
    "deterministic_equivalent",
    "contour_approximation",
    "simulation_target",
    "diagnostic",
    "other",
}

SOURCE_FAITHFULNESS_REQUIRED_CHECKS = {
    "shapes_and_scaling",
    "beta_is_noisy_estimator",
    "hermite_factorial_convention",
    "raw_direct_solve_vs_spectral",
    "decomposition_identity",
}
SOURCE_FAITHFULNESS_DEFAULT_TOLERANCE = 1.0e-10

FORMULA_TRACE_OBJECT_TYPES = {"deterministic_equivalent", "contour_approximation"}
FORMULA_TRACE_TYPES = {"theorem_formula", "deterministic_equivalent", "contour_approximation", "other"}
FORMULA_TRACE_ALLOWED_OVERALL = {"PASS", "NOT_APPLICABLE"}
RAW_OBJECT_VALIDATION_DEFAULT_TOLERANCE = 1.0e-10

REVIEW_CHECKLIST_ITEMS = {
    "source_setting",
    "tests",
    "formula_trace",
    "raw_object_validation",
    "error_decomposition",
}
REVIEW_CHECKLIST_STATUSES = {"PASS", "NOT_APPLICABLE", "FAIL"}


def source_setting_value_is_unfilled(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return True
        lowered = stripped.lower()
        return lowered in {"tbd", "todo", "unknown", "unset", "fill me"} or "tbd" in lowered
    if isinstance(value, list):
        return not value or any(source_setting_value_is_unfilled(item) for item in value)
    if isinstance(value, dict):
        return not value or any(source_setting_value_is_unfilled(item) for item in value.values())
    return False


def source_setting_manifest_errors(workstream: Path, status: dict[str, Any] | None = None) -> list[str]:
    if status is None:
        status = load_status(workstream)
    if status.get("type") != "computation":
        return []

    path = source_setting_manifest_path(workstream)
    try:
        manifest = read_json(path)
    except ValidationError as exc:
        return [str(exc)]

    errors: list[str] = []
    for key in SOURCE_SETTING_REQUIRED_TOP_LEVEL:
        if key not in manifest:
            errors.append(f"{path}: missing top-level key {key}")

    if manifest.get("schema_version") != 1:
        errors.append(f"{path}: schema_version must be 1")
    if manifest.get("manifest_status") != "COMPLETE":
        errors.append(f"{path}: manifest_status must be COMPLETE before computation tests/review")

    dimension = manifest.get("dimension_scaling")
    if not isinstance(dimension, dict):
        errors.append(f"{path}: dimension_scaling must be an object")
    else:
        for key in SOURCE_SETTING_DIMENSION_KEYS:
            if key not in dimension:
                errors.append(f"{path}: dimension_scaling missing {key}")
            elif source_setting_value_is_unfilled(dimension[key]):
                errors.append(f"{path}: dimension_scaling.{key} is not filled")

    random_objects = manifest.get("random_objects")
    if not isinstance(random_objects, dict):
        errors.append(f"{path}: random_objects must be an object")
    else:
        for key in SOURCE_SETTING_RANDOM_OBJECTS:
            value = random_objects.get(key)
            if not isinstance(value, dict):
                errors.append(f"{path}: random_objects.{key} must be an object")
                continue
            for required in ["construction", "shape", "role"]:
                if required not in value:
                    errors.append(f"{path}: random_objects.{key} missing {required}")
                elif source_setting_value_is_unfilled(value[required]):
                    errors.append(f"{path}: random_objects.{key}.{required} is not filled")

    hermite = manifest.get("hermite_convention")
    if not isinstance(hermite, dict):
        errors.append(f"{path}: hermite_convention must be an object")
    else:
        for key in SOURCE_SETTING_HERMITE_KEYS:
            if key not in hermite:
                errors.append(f"{path}: hermite_convention missing {key}")
            elif source_setting_value_is_unfilled(hermite[key]):
                errors.append(f"{path}: hermite_convention.{key} is not filled")

    normalization = manifest.get("normalization")
    if not isinstance(normalization, dict):
        errors.append(f"{path}: normalization must be an object")
    else:
        for key in SOURCE_SETTING_NORMALIZATION_KEYS:
            if key not in normalization:
                errors.append(f"{path}: normalization missing {key}")
            elif source_setting_value_is_unfilled(normalization[key]):
                errors.append(f"{path}: normalization.{key} is not filled")

    compared_objects = manifest.get("compared_objects")
    if not isinstance(compared_objects, list) or not compared_objects:
        errors.append(f"{path}: compared_objects must be a nonempty list")
    else:
        for index, item in enumerate(compared_objects):
            if not isinstance(item, dict):
                errors.append(f"{path}: compared_objects[{index}] must be an object")
                continue
            object_type = item.get("object_type")
            if object_type not in SOURCE_SETTING_OBJECT_TYPES:
                errors.append(
                    f"{path}: compared_objects[{index}].object_type must be one of "
                    f"{sorted(SOURCE_SETTING_OBJECT_TYPES)}, got {object_type!r}"
                )
            for key in ["name", "object_type", "formula_or_description", "normalization"]:
                if key not in item:
                    errors.append(f"{path}: compared_objects[{index}] missing {key}")
                elif source_setting_value_is_unfilled(item[key]):
                    errors.append(f"{path}: compared_objects[{index}].{key} is not filled")

    for optional_key in ["source_locations", "source_faithfulness_checks"]:
        if optional_key in manifest and source_setting_value_is_unfilled(manifest[optional_key]):
            errors.append(f"{path}: {optional_key} is present but not filled")

    return errors


def source_faithfulness_test_errors(workstream: Path, status: dict[str, Any] | None = None) -> list[str]:
    if status is None:
        status = load_status(workstream)
    if status.get("type") != "computation":
        return []

    path = source_faithfulness_tests_path(workstream)
    try:
        payload = read_json(path)
    except ValidationError as exc:
        return [str(exc)]

    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append(f"{path}: schema_version must be 1")
    if payload.get("overall_status") != "PASS":
        errors.append(f"{path}: overall_status must be PASS before computation tests/review")

    checks = payload.get("checks")
    if not isinstance(checks, list) or not checks:
        errors.append(f"{path}: checks must be a nonempty list")
        return errors

    by_id: dict[str, dict[str, Any]] = {}
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            errors.append(f"{path}: checks[{index}] must be an object")
            continue
        check_id = check.get("check_id")
        if not isinstance(check_id, str) or not check_id:
            errors.append(f"{path}: checks[{index}].check_id must be a nonempty string")
            continue
        if check_id in by_id:
            errors.append(f"{path}: duplicate check_id {check_id!r}")
        by_id[check_id] = check

    missing = sorted(SOURCE_FAITHFULNESS_REQUIRED_CHECKS - set(by_id))
    if missing:
        errors.append(f"{path}: missing required source-faithfulness checks {missing}")

    for check_id in sorted(SOURCE_FAITHFULNESS_REQUIRED_CHECKS & set(by_id)):
        check = by_id[check_id]
        if check.get("status") != "PASS":
            errors.append(f"{path}: check {check_id} status must be PASS")
        evidence = check.get("evidence")
        if source_setting_value_is_unfilled(evidence):
            errors.append(f"{path}: check {check_id} evidence is not filled")
        if check_id in {"raw_direct_solve_vs_spectral", "decomposition_identity"}:
            max_error = check.get("max_error")
            if not isinstance(max_error, (int, float)):
                errors.append(f"{path}: check {check_id} must include numeric max_error")
            elif float(max_error) > SOURCE_FAITHFULNESS_DEFAULT_TOLERANCE:
                errors.append(
                    f"{path}: check {check_id} max_error={float(max_error):.3g} exceeds "
                    f"{SOURCE_FAITHFULNESS_DEFAULT_TOLERANCE:.1e}"
                )
            tolerance = check.get("tolerance")
            if not isinstance(tolerance, (int, float)):
                errors.append(f"{path}: check {check_id} must include numeric tolerance")
            elif float(tolerance) > SOURCE_FAITHFULNESS_DEFAULT_TOLERANCE:
                errors.append(
                    f"{path}: check {check_id} tolerance={float(tolerance):.3g} exceeds "
                    f"the scaffold hard tolerance {SOURCE_FAITHFULNESS_DEFAULT_TOLERANCE:.1e}"
                )

    return errors


def formula_trace_is_required(workstream: Path, status: dict[str, Any] | None = None) -> tuple[bool, list[str]]:
    if status is None:
        status = load_status(workstream)
    if status.get("type") != "computation":
        return (False, [])

    path = source_setting_manifest_path(workstream)
    try:
        manifest = read_json(path)
    except ValidationError as exc:
        return (False, [str(exc)])

    compared_objects = manifest.get("compared_objects", [])
    if not isinstance(compared_objects, list):
        return (False, [f"{path}: compared_objects must be a list before formula trace can be checked"])

    formula_objects: list[str] = []
    for index, item in enumerate(compared_objects):
        if not isinstance(item, dict):
            continue
        object_type = item.get("object_type")
        if object_type in FORMULA_TRACE_OBJECT_TYPES:
            name = item.get("name")
            formula_objects.append(str(name) if isinstance(name, str) and name.strip() else f"compared_objects[{index}]")
    return (bool(formula_objects), formula_objects)


def formula_trace_errors(workstream: Path, status: dict[str, Any] | None = None) -> list[str]:
    if status is None:
        status = load_status(workstream)
    if status.get("type") != "computation":
        return []

    required, required_evidence = formula_trace_is_required(workstream, status)
    path = formula_trace_path(workstream)
    try:
        trace = read_json(path)
    except ValidationError as exc:
        return [str(exc)]

    errors: list[str] = []
    if trace.get("schema_version") != 1:
        errors.append(f"{path}: schema_version must be 1")

    overall = trace.get("overall_status")
    if overall not in FORMULA_TRACE_ALLOWED_OVERALL:
        errors.append(f"{path}: overall_status must be PASS or NOT_APPLICABLE")
    if required and overall != "PASS":
        errors.append(
            f"{path}: formula trace is required because source_setting_manifest compared_objects "
            f"include {required_evidence}, so overall_status must be PASS"
        )
    if not required and overall == "NOT_APPLICABLE":
        reason = trace.get("not_applicable_reason")
        if source_setting_value_is_unfilled(reason):
            errors.append(f"{path}: not_applicable_reason must be filled when overall_status is NOT_APPLICABLE")

    formulas = trace.get("formulas")
    if overall == "PASS":
        if not isinstance(formulas, list) or not formulas:
            errors.append(f"{path}: formulas must be a nonempty list when overall_status is PASS")
        elif isinstance(formulas, list):
            seen_formula_ids: set[str] = set()
            for index, formula in enumerate(formulas):
                if not isinstance(formula, dict):
                    errors.append(f"{path}: formulas[{index}] must be an object")
                    continue
                formula_id = formula.get("formula_id")
                if not isinstance(formula_id, str) or not formula_id.strip():
                    errors.append(f"{path}: formulas[{index}].formula_id must be a nonempty string")
                elif formula_id in seen_formula_ids:
                    errors.append(f"{path}: duplicate formula_id {formula_id!r}")
                else:
                    seen_formula_ids.add(formula_id)
                formula_type = formula.get("formula_type")
                if formula_type not in FORMULA_TRACE_TYPES:
                    errors.append(
                        f"{path}: formulas[{index}].formula_type must be one of "
                        f"{sorted(FORMULA_TRACE_TYPES)}, got {formula_type!r}"
                    )
                for key in [
                    "source_label",
                    "source_location",
                    "theorem_or_result_name",
                    "implementation_path",
                    "code_function",
                ]:
                    if key not in formula:
                        errors.append(f"{path}: formulas[{index}] missing {key}")
                    elif source_setting_value_is_unfilled(formula[key]):
                        errors.append(f"{path}: formulas[{index}].{key} is not filled")
                tests = formula.get("independent_sanity_tests")
                if not isinstance(tests, list) or not tests:
                    errors.append(f"{path}: formulas[{index}].independent_sanity_tests must be a nonempty list")
                    continue
                for test_index, test in enumerate(tests):
                    if not isinstance(test, dict):
                        errors.append(f"{path}: formulas[{index}].independent_sanity_tests[{test_index}] must be an object")
                        continue
                    if test.get("status") != "PASS":
                        errors.append(
                            f"{path}: formulas[{index}].independent_sanity_tests[{test_index}].status must be PASS"
                        )
                    for key in ["test_name", "test_file", "evidence"]:
                        if key not in test:
                            errors.append(
                                f"{path}: formulas[{index}].independent_sanity_tests[{test_index}] missing {key}"
                            )
                        elif source_setting_value_is_unfilled(test[key]):
                            errors.append(
                                f"{path}: formulas[{index}].independent_sanity_tests[{test_index}].{key} is not filled"
                            )
    elif isinstance(formulas, list) and formulas:
        errors.append(f"{path}: formulas must be empty when overall_status is NOT_APPLICABLE")

    return errors


def raw_finite_object_names(workstream: Path, status: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    if status is None:
        status = load_status(workstream)
    if status.get("type") != "computation":
        return ([], [])

    path = source_setting_manifest_path(workstream)
    try:
        manifest = read_json(path)
    except ValidationError as exc:
        return ([], [str(exc)])

    compared_objects = manifest.get("compared_objects", [])
    if not isinstance(compared_objects, list):
        return ([], [f"{path}: compared_objects must be a list before raw object validation can be checked"])

    names: list[str] = []
    errors: list[str] = []
    for index, item in enumerate(compared_objects):
        if not isinstance(item, dict):
            continue
        if item.get("object_type") != "raw_finite_object":
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            errors.append(f"{path}: raw_finite_object compared_objects[{index}] has missing name")
            continue
        names.append(name.strip())
    return (names, errors)


def raw_object_validation_errors(workstream: Path, status: dict[str, Any] | None = None) -> list[str]:
    if status is None:
        status = load_status(workstream)
    if status.get("type") != "computation":
        return []

    raw_names, name_errors = raw_finite_object_names(workstream, status)
    errors: list[str] = list(name_errors)
    path = raw_object_validation_path(workstream)
    try:
        payload = read_json(path)
    except ValidationError as exc:
        errors.append(str(exc))
        return errors

    if payload.get("schema_version") != 1:
        errors.append(f"{path}: schema_version must be 1")

    overall = payload.get("overall_status")
    if overall not in {"PASS", "NOT_APPLICABLE"}:
        errors.append(f"{path}: overall_status must be PASS or NOT_APPLICABLE")
    if raw_names and overall != "PASS":
        errors.append(
            f"{path}: raw object validation is required because source_setting_manifest "
            f"declares raw_finite_object entries {raw_names}; overall_status must be PASS"
        )
    if not raw_names and overall == "NOT_APPLICABLE":
        reason = payload.get("not_applicable_reason")
        if source_setting_value_is_unfilled(reason):
            errors.append(f"{path}: not_applicable_reason must be filled when overall_status is NOT_APPLICABLE")

    validations = payload.get("validations")
    if overall == "PASS":
        if not isinstance(validations, list) or not validations:
            errors.append(f"{path}: validations must be a nonempty list when overall_status is PASS")
            return errors
        by_name: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(validations):
            if not isinstance(item, dict):
                errors.append(f"{path}: validations[{index}] must be an object")
                continue
            object_name = item.get("object_name")
            if not isinstance(object_name, str) or not object_name.strip():
                errors.append(f"{path}: validations[{index}].object_name must be a nonempty string")
                continue
            object_name = object_name.strip()
            if object_name in by_name:
                errors.append(f"{path}: duplicate validation for object_name {object_name!r}")
            by_name[object_name] = item

        missing = sorted(set(raw_names) - set(by_name))
        if missing:
            errors.append(f"{path}: missing validations for raw_finite_object entries {missing}")
        unexpected = sorted(set(by_name) - set(raw_names))
        if unexpected:
            errors.append(f"{path}: validations include objects not declared as raw_finite_object {unexpected}")

        for object_name in sorted(set(raw_names) & set(by_name)):
            item = by_name[object_name]
            if item.get("status") != "PASS":
                errors.append(f"{path}: validation for {object_name!r} must have status PASS")
            for key in [
                "source_label",
                "primary_implementation",
                "independent_implementation",
                "evidence",
            ]:
                if key not in item:
                    errors.append(f"{path}: validation for {object_name!r} missing {key}")
                elif source_setting_value_is_unfilled(item[key]):
                    errors.append(f"{path}: validation for {object_name!r}.{key} is not filled")
            max_error = item.get("max_error")
            if not isinstance(max_error, (int, float)):
                errors.append(f"{path}: validation for {object_name!r} must include numeric max_error")
            elif float(max_error) > RAW_OBJECT_VALIDATION_DEFAULT_TOLERANCE:
                errors.append(
                    f"{path}: validation for {object_name!r} max_error={float(max_error):.3g} "
                    f"exceeds {RAW_OBJECT_VALIDATION_DEFAULT_TOLERANCE:.1e}"
                )
            tolerance = item.get("tolerance")
            if not isinstance(tolerance, (int, float)):
                errors.append(f"{path}: validation for {object_name!r} must include numeric tolerance")
            elif float(tolerance) > RAW_OBJECT_VALIDATION_DEFAULT_TOLERANCE:
                errors.append(
                    f"{path}: validation for {object_name!r} tolerance={float(tolerance):.3g} "
                    f"exceeds scaffold hard tolerance {RAW_OBJECT_VALIDATION_DEFAULT_TOLERANCE:.1e}"
                )
    elif isinstance(validations, list) and validations:
        errors.append(f"{path}: validations must be empty when overall_status is NOT_APPLICABLE")

    return errors


def has_python_test_files(workstream: Path) -> bool:
    test_dir = workstream / "tests"
    if not test_dir.exists():
        return False
    for pattern in ["test_*.py", "*_test.py"]:
        if any(test_dir.rglob(pattern)):
            return True
    return False


def pytest_available() -> bool:
    try:
        completed = subprocess.run(
            [sys.executable, "-c", "import pytest"],
            text=True,
            capture_output=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def test_timeout_seconds(status: dict[str, Any]) -> int:
    value = status.get("test_timeout_seconds", DEFAULT_TEST_TIMEOUT_SECONDS)
    if value is None:
        return DEFAULT_TEST_TIMEOUT_SECONDS
    if not isinstance(value, int) or value <= 0:
        raise ValidationError("status.test_timeout_seconds must be a positive integer when present")
    return value


def normalize_timeout_output(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def simple_python_test_command(test_dir: Path) -> list[str]:
    script = (
        "from pathlib import Path\n"
        "import sys\n"
        f"sys.path.insert(0, {str((repo_root() / 'scripts').resolve())!r})\n"
        "from lib import simple_python_test_run\n"
        "exit_code, stdout, stderr = simple_python_test_run(Path(sys.argv[1]))\n"
        "sys.stdout.write(stdout)\n"
        "sys.stderr.write(stderr)\n"
        "raise SystemExit(exit_code)\n"
    )
    return [sys.executable, "-c", script, str(test_dir)]


def run_test_subprocess(command: list[str], workstream: Path, timeout_seconds: int) -> tuple[int, str, str, str | None, bool]:
    try:
        completed = subprocess.run(
            command,
            cwd=workstream,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = normalize_timeout_output(exc.stdout)
        stderr = normalize_timeout_output(exc.stderr)
        message = f"Test command timed out after {timeout_seconds} seconds."
        if stderr:
            stderr = stderr.rstrip() + "\n" + message
        else:
            stderr = message
        return TEST_TIMEOUT_EXIT_CODE, stdout, stderr, message, True
    return completed.returncode, completed.stdout, completed.stderr, None, False


def simple_python_test_run(test_dir: Path) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    failures = 0
    executed = 0
    test_files = sorted(set(test_dir.rglob("test_*.py")) | set(test_dir.rglob("*_test.py")))

    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        for index, path in enumerate(test_files):
            module_name = f"comath_smoke_test_{index}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise RuntimeError(f"Could not load test file {path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                test_names = sorted(name for name in vars(module) if name.startswith("test_"))
                for name in test_names:
                    candidate = getattr(module, name)
                    if not callable(candidate):
                        continue
                    executed += 1
                    try:
                        candidate()
                        print(f"PASS {path.name}::{name}")
                    except Exception:
                        failures += 1
                        print(f"FAIL {path.name}::{name}")
                        traceback.print_exc()
                suite = unittest.defaultTestLoader.loadTestsFromModule(module)
                case_count = suite.countTestCases()
                if case_count:
                    executed += case_count
                    runner = unittest.TextTestRunner(stream=stdout, verbosity=2)
                    result = runner.run(suite)
                    if not result.wasSuccessful():
                        failures += len(result.failures) + len(result.errors)
                elif not test_names:
                    failures += 1
                    print(f"FAIL {path.name}::no_tests_found")
            except Exception:
                failures += 1
                print(f"FAIL {path.name}::module_load")
                traceback.print_exc()

        if not test_files:
            failures += 1
            print(f"{test_dir}: no test files found")
        elif executed == 0:
            failures += 1
            print(f"{test_dir}: no tests executed")

    return (1 if failures else 0, stdout.getvalue(), stderr.getvalue())


def run_workstream_tests(workstream: Path, update_status: bool = True) -> dict[str, Any]:
    status = load_status(workstream)
    if status.get("tests_required") is not True:
        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "workstream_id": status.get("id"),
            "tests_required": False,
            "passed": True,
            "exit_code": 0,
            "command": None,
            "timeout_seconds": None,
            "timed_out": False,
            "stdout": "",
            "stderr": "",
            "reason": "Tests are not required for this workstream.",
        }
        return result

    test_dir = workstream / "tests"
    timeout_seconds = test_timeout_seconds(status)
    use_pytest = pytest_available()
    command = [sys.executable, "-m", "pytest", str(test_dir)] if use_pytest else simple_python_test_command(test_dir)
    result: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "workstream_id": status.get("id"),
        "tests_required": True,
        "passed": False,
        "exit_code": None,
        "command": command,
        "timeout_seconds": timeout_seconds,
        "timed_out": False,
        "stdout": "",
        "stderr": "",
        "reason": None,
    }

    if not has_python_test_files(workstream):
        result["exit_code"] = 1
        result["reason"] = f"No Python test files found under {test_dir}."
    elif status.get("type") == "computation" and (manifest_errors := source_setting_manifest_errors(workstream, status)):
        result["exit_code"] = 1
        result["reason"] = "Computation source_setting_manifest.json did not pass validation."
        result["stdout"] = "\n".join(manifest_errors)
    elif status.get("type") == "computation" and (faithfulness_errors := source_faithfulness_test_errors(workstream, status)):
        result["exit_code"] = 1
        result["reason"] = "Computation source_faithfulness_tests.json did not pass validation."
        result["stdout"] = "\n".join(faithfulness_errors)
    elif status.get("type") == "computation" and (trace_errors := formula_trace_errors(workstream, status)):
        result["exit_code"] = 1
        result["reason"] = "Computation formula_trace.json did not pass validation."
        result["stdout"] = "\n".join(trace_errors)
    elif status.get("type") == "computation" and (raw_validation_errors := raw_object_validation_errors(workstream, status)):
        result["exit_code"] = 1
        result["reason"] = "Computation raw_object_validation.json did not pass validation."
        result["stdout"] = "\n".join(raw_validation_errors)
    else:
        exit_code, stdout, stderr, timeout_reason, timed_out = run_test_subprocess(
            command,
            workstream,
            timeout_seconds,
        )
        result["exit_code"] = exit_code
        result["passed"] = exit_code == 0
        result["timed_out"] = timed_out
        result["stdout"] = stdout
        result["stderr"] = stderr
        if timeout_reason is not None:
            result["reason"] = timeout_reason
        elif exit_code != 0:
            result["reason"] = (
                "pytest returned a nonzero exit code."
                if use_pytest
                else "internal simple test runner returned a nonzero exit code."
            )

    artifacts_dir(workstream).mkdir(parents=True, exist_ok=True)
    write_json(test_run_path(workstream), result)

    if update_status:
        status["tests_passed"] = result["passed"]
        write_json(status_path(workstream), status)

    return result


def latest_test_run_errors(workstream: Path) -> list[str]:
    path = test_run_path(workstream)
    if not path.exists():
        return [f"{workstream}: missing artifacts/test_run.json"]
    try:
        result = read_json(path)
    except ValidationError as exc:
        return [str(exc)]
    errors: list[str] = []
    if result.get("tests_required") is not True:
        errors.append(f"{path}: expected tests_required=true for computation workstream")
    if result.get("timed_out") is True:
        errors.append(f"{path}: latest recorded test run timed out after {result.get('timeout_seconds')} seconds")
    if result.get("passed") is not True:
        errors.append(f"{path}: latest recorded test run did not pass")
    if result.get("exit_code") != 0:
        errors.append(f"{path}: latest recorded test exit_code is {result.get('exit_code')!r}")
    if not result.get("command"):
        errors.append(f"{path}: missing recorded test command")
    return errors


def record_failed_exploration(
    workstream_id: str,
    reason: str,
    attempted_strategy: str,
    evidence: str,
    next_action: str,
) -> Path:
    with scaffold_lock():
        root = repo_root()
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        directory = root / "failed_explorations"
        path = directory / f"{timestamp}_{workstream_id}.md"
        suffix = 1
        while path.exists():
            suffix += 1
            path = directory / f"{timestamp}_{workstream_id}_{suffix}.md"
        text = f"""# Failed Exploration: {workstream_id}

## Date

{timestamp}

## Workstream

{workstream_id}

## Reason

{reason}

## Attempted Strategy

{attempted_strategy}

## Evidence

{evidence}

## Recommended Next Action

{next_action}
"""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path


def mark_status_blocked(
    workstream: Path,
    status: dict[str, Any],
    reason: str,
    attempted_strategy: str,
    evidence: str,
    next_action: str,
    event_type: str = "workstream_blocked",
) -> Path:
    with scaffold_lock():
        if status.get("status") == "COMPLETE":
            raise ValidationError("Cannot mark a COMPLETE workstream as BLOCKED.")
        status["status"] = "BLOCKED"
        status["blocked_reason"] = reason
        status["finalized"] = False
        write_json(status_path(workstream), status)
        sync_project_workstream_status(status["id"], "BLOCKED")
        failure_note = record_failed_exploration(
            status["id"],
            reason,
            attempted_strategy,
            evidence,
            next_action,
        )
        append_message(
            event_type,
            status["id"],
            f"Workstream {status['id']} blocked: {reason}",
            failure_note=str(failure_note.relative_to(repo_root())),
        )
        return failure_note


def validate_status_object(status: dict[str, Any], source: Path) -> list[str]:
    errors: list[str] = []
    required_keys = [
        "id",
        "goal_id",
        "type",
        "status",
        "blocked_reason",
        "tests_required",
        "tests_passed",
        "review_required",
        "review_passed",
        "golden_values_approved",
        "requires_computation_gate",
        "linked_computation_workstream_id",
        "computation_gate_reason",
        "current_review_round",
        "max_review_rounds",
        "finalized",
    ]
    for key in required_keys:
        if key not in status:
            errors.append(f"{source}: missing key {key}")

    value = status.get("status")
    if value not in ALLOWED_STATUSES:
        errors.append(f"{source}: invalid status {value!r}")

    current_round = status.get("current_review_round")
    max_rounds = status.get("max_review_rounds")
    if not isinstance(current_round, int) or current_round < 0:
        errors.append(f"{source}: current_review_round must be a nonnegative integer")
    if not isinstance(max_rounds, int) or max_rounds < 1:
        errors.append(f"{source}: max_review_rounds must be a positive integer")
    if isinstance(current_round, int) and isinstance(max_rounds, int):
        if current_round > max_rounds:
            errors.append(f"{source}: current_review_round exceeds max_review_rounds")

    if status.get("status") == "BLOCKED" and not status.get("blocked_reason"):
        errors.append(f"{source}: BLOCKED workstream must include blocked_reason")

    if status.get("finalized") is True and status.get("status") != "COMPLETE":
        errors.append(f"{source}: finalized=true requires status COMPLETE")

    if not isinstance(status.get("requires_computation_gate"), bool):
        errors.append(f"{source}: requires_computation_gate must be a boolean")
    linked_computation = status.get("linked_computation_workstream_id")
    if linked_computation is not None and not isinstance(linked_computation, str):
        errors.append(f"{source}: linked_computation_workstream_id must be null or a string")
    reason = status.get("computation_gate_reason")
    if reason is not None and not isinstance(reason, str):
        errors.append(f"{source}: computation_gate_reason must be null or a string")
    if status.get("requires_computation_gate") is True and status.get("type") != "proof":
        errors.append(f"{source}: requires_computation_gate=true is only supported for proof workstreams")

    optional_booleans = [
        "requires_independent_workers",
        "literature_required",
        "theorem_statement_verification_required",
        "computation_required",
        "source_patch_proposed",
        "source_patch_applied",
    ]
    for key in optional_booleans:
        if key in status and not isinstance(status.get(key), bool):
            errors.append(f"{source}: {key} must be a boolean when present")
    source_patch_application = status.get("source_patch_application")
    if (
        status.get("source_patch_applied") is True
        and status.get("status") != "COMPLETE"
        and not (
            isinstance(source_patch_application, dict)
            and source_patch_application.get("script") == "scripts/apply_approved_patch.py"
        )
    ):
        errors.append(
            f"{source}: source_patch_applied=true is only valid after promotion. "
            "The only pre-promotion exception is a patch applied by "
            "scripts/apply_approved_patch.py after readiness gates pass."
        )
    if source_patch_application is not None and not isinstance(source_patch_application, dict):
        errors.append(f"{source}: source_patch_application must be null or an object when present")
    if "patch_target_labels" in status and not isinstance(status.get("patch_target_labels"), list):
        errors.append(f"{source}: patch_target_labels must be a list when present")
    if "no_computation_waiver" in status:
        waiver = status.get("no_computation_waiver")
        if waiver is not None and not isinstance(waiver, str):
            errors.append(f"{source}: no_computation_waiver must be null or a string")
    if "test_timeout_seconds" in status:
        timeout_value = status.get("test_timeout_seconds")
        if timeout_value is not None and (not isinstance(timeout_value, int) or timeout_value <= 0):
            errors.append(f"{source}: test_timeout_seconds must be null or a positive integer")

    return errors


def validate_or_raise_status(status: dict[str, Any], source: Path) -> None:
    errors = validate_status_object(status, source)
    if errors:
        raise ValidationError("\n".join(errors))


def load_reviews(workstream: Path) -> list[dict[str, Any]]:
    directory = review_dir(workstream)
    if not directory.exists():
        return []
    reviews: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".template.json"):
            continue
        review = read_json(path)
        review["_path"] = str(path)
        reviews.append(review)
    return reviews


def validate_review(review: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    source = review.get("_path", "<review>")
    for key in ["reviewer_id", "round", "decision", "summary"]:
        if key not in review:
            errors.append(f"{source}: missing key {key}")
    if review.get("decision") not in ALLOWED_DECISIONS:
        errors.append(f"{source}: invalid decision {review.get('decision')!r}")
    if not isinstance(review.get("round"), int) or review.get("round", 0) < 1:
        errors.append(f"{source}: round must be a positive integer")
    for key in ["required_changes", "approved_claims", "blocked_claims"]:
        if key not in review:
            errors.append(f"{source}: missing key {key}")
        elif not isinstance(review.get(key), list):
            errors.append(f"{source}: {key} must be a list")

    checklist = review.get("checklist")
    if not isinstance(checklist, dict):
        errors.append(f"{source}: missing or invalid checklist object")
    else:
        missing = sorted(REVIEW_CHECKLIST_ITEMS - set(checklist))
        if missing:
            errors.append(f"{source}: checklist missing required items {missing}")
        for key in sorted(REVIEW_CHECKLIST_ITEMS & set(checklist)):
            item = checklist.get(key)
            if not isinstance(item, dict):
                errors.append(f"{source}: checklist.{key} must be an object")
                continue
            status_value = item.get("status")
            if status_value not in REVIEW_CHECKLIST_STATUSES:
                errors.append(
                    f"{source}: checklist.{key}.status must be one of "
                    f"{sorted(REVIEW_CHECKLIST_STATUSES)}, got {status_value!r}"
                )
            evidence = item.get("evidence")
            if source_setting_value_is_unfilled(evidence):
                errors.append(f"{source}: checklist.{key}.evidence is not filled")
            if review.get("decision") == "APPROVE" and status_value == "FAIL":
                errors.append(f"{source}: APPROVE review cannot have checklist.{key}.status=FAIL")

    if review.get("decision") == "APPROVE":
        if review.get("required_changes"):
            errors.append(f"{source}: APPROVE review must not include required_changes")
        if review.get("blocked_claims"):
            errors.append(f"{source}: APPROVE review must not include blocked_claims")
    return errors


def latest_review_round(reviews: list[dict[str, Any]]) -> int | None:
    rounds = [review.get("round") for review in reviews if isinstance(review.get("round"), int)]
    if not rounds:
        return None
    return max(rounds)


def latest_reviews(reviews: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest_round = latest_review_round(reviews)
    if latest_round is None:
        return []
    return [review for review in reviews if review.get("round") == latest_round]


def markdown_section(text: str, heading: str) -> str:
    marker = f"## {heading}"
    start = text.find(marker)
    if start < 0:
        return ""
    start = text.find("\n", start)
    if start < 0:
        return ""
    next_heading = text.find("\n## ", start + 1)
    if next_heading < 0:
        return text[start + 1 :]
    return text[start + 1 : next_heading]


def report_error_decomposition_errors(path: Path) -> list[str]:
    if not path.exists():
        return [f"Missing report: {path}"]
    text = path.read_text(encoding="utf-8")
    section = markdown_section(text, "Error Decomposition")
    errors: list[str] = []
    if not section.strip():
        return [f"{path}: Error Decomposition section is empty"]
    for label, pattern in REQUIRED_ERROR_DECOMPOSITION_LABELS.items():
        if not pattern.search(section):
            errors.append(
                f"{path}: Error Decomposition section must explicitly include "
                f"{label!r}. Use 'not applicable' only after the label when appropriate."
            )
    return errors


def report_claim_ids(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    claims_section = markdown_section(text, "Claims")
    return sorted(set(CLAIM_ID_PATTERN.findall(claims_section)))


def report_claims(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    claims_section = markdown_section(text, "Claims")
    claims: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in claims_section.splitlines():
        match = CLAIM_ID_PATTERN.search(line)
        if not match:
            continue
        claim_id = match.group(0)
        if claim_id in seen:
            continue
        seen.add(claim_id)
        claim_text = line[match.end() :].strip()
        claim_text = claim_text.lstrip(":").strip()
        if not claim_text:
            claim_text = line.strip()
        claims.append({
            "claim_id": claim_id,
            "text": claim_text,
        })
    return claims


def claims_section_declares_no_claims(path: Path) -> bool:
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    claims_section = markdown_section(text, "Claims").lower()
    return "no substantive claims" in claims_section or "no claims" in claims_section


def report_has_required_sections(path: Path) -> list[str]:
    if not path.exists():
        return [f"Missing report: {path}"]
    text = path.read_text(encoding="utf-8")
    errors: list[str] = []
    for section in REQUIRED_REPORT_SECTIONS:
        if section not in text:
            errors.append(f"{path}: missing section {section}")
    if "TBD" in text:
        errors.append(f"{path}: contains unresolved TBD markers")
    errors.extend(report_error_decomposition_errors(path))
    claim_ids = report_claim_ids(path)
    if not claim_ids and not claims_section_declares_no_claims(path) and "TBD" not in text:
        errors.append(
            f"{path}: Claims section must contain claim ids like C-001 or explicitly say "
            "'No substantive claims.'"
        )
    return errors


def working_paper_dir() -> Path:
    return repo_root() / "working_paper"


def claim_registry_path() -> Path:
    return working_paper_dir() / "claim_registry.json"


def approved_claims_tex_path() -> Path:
    return working_paper_dir() / "approved_claims.tex"


def working_paper_tex_path() -> Path:
    return working_paper_dir() / "working_paper.tex"


def load_claim_registry() -> dict[str, Any]:
    path = claim_registry_path()
    if not path.exists():
        return {"claims": []}
    registry = read_json(path)
    if "claims" not in registry or not isinstance(registry["claims"], list):
        raise ValidationError(f"{path}: expected field claims to be a list")
    return registry


def approved_claim_keys(registry: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for claim in registry.get("claims", []):
        if isinstance(claim, dict) and claim.get("status") == "approved" and claim.get("claim_key"):
            keys.add(str(claim["claim_key"]))
    return keys


def latex_approved_claim_keys(path: Path) -> list[str]:
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    return APPROVED_CLAIM_PATTERN.findall(text)


def latex_escape_claim_text(text: str) -> str:
    replacements = {
        "\\": r"\textbackslash{}",
        "&": r"\&",
        "%": r"\%",
        "$": r"\$",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
        "~": r"\textasciitilde{}",
        "^": r"\textasciicircum{}",
    }
    return "".join(replacements.get(char, char) for char in text)


def review_gate_errors(workstream: Path, status: dict[str, Any], reviews: list[dict[str, Any]]) -> list[str]:
    errors: list[str] = []
    if status.get("review_required") is not True:
        return errors

    if not reviews:
        errors.append(f"{workstream}: no reviewer JSON files found")
        return errors

    current_round = status.get("current_review_round")
    latest_round = latest_review_round(reviews)
    round_reviews = latest_reviews(reviews)
    if latest_round is None:
        errors.append(f"{workstream}: no valid review rounds found")
        return errors
    if current_round != latest_round:
        errors.append(
            f"{workstream}: latest reviewer round is {latest_round}, "
            f"but status.json current_review_round is {current_round}"
        )

    decisions = [review.get("decision") for review in round_reviews]
    if any(decision == "BLOCK" for decision in decisions):
        errors.append(f"{workstream}: latest review round {latest_round} includes BLOCK")
    if any(decision == "REQUEST_CHANGES" for decision in decisions):
        errors.append(f"{workstream}: latest review round {latest_round} includes REQUEST_CHANGES")
    if not round_reviews or any(decision != "APPROVE" for decision in decisions):
        errors.append(f"{workstream}: all reviews in latest round {latest_round} must be APPROVE")

    claim_ids = set(report_claim_ids(report_path(workstream)))
    if claim_ids:
        approved_claims: set[str] = set()
        blocked_claims: set[str] = set()
        for review in round_reviews:
            approved_claims.update(str(item) for item in review.get("approved_claims", []))
            blocked_claims.update(str(item) for item in review.get("blocked_claims", []))
        missing = sorted(claim_ids - approved_claims)
        if missing:
            errors.append(
                f"{workstream}: latest review round {latest_round} did not approve claims {missing}"
            )
        blocked = sorted(claim_ids & blocked_claims)
        if blocked:
            errors.append(
                f"{workstream}: latest review round {latest_round} still blocks claims {blocked}"
            )
    elif not claims_section_declares_no_claims(report_path(workstream)):
        errors.append(
            f"{workstream}: claim-level review requires claim ids in report.md "
            "or an explicit 'No substantive claims.' statement"
        )

    return errors


def theorem_statement_verification_gate_errors(workstream: Path, status: dict[str, Any]) -> list[str]:
    if status.get("type") != "literature":
        return []
    if status.get("theorem_statement_verification_required") is not True:
        return []

    path = artifacts_dir(workstream) / "theorem_statement_verification.json"
    try:
        result = read_json(path)
    except ValidationError as exc:
        return [str(exc)]

    errors: list[str] = []
    if result.get("verified") is not True:
        recorded_errors = result.get("errors", [])
        if isinstance(recorded_errors, list) and recorded_errors:
            for item in recorded_errors:
                errors.append(f"{path}: {item}")
        else:
            errors.append(f"{path}: theorem statement verification did not pass")
    if not result.get("generated_at"):
        errors.append(f"{path}: missing generated_at; run scripts/verify_theorem_statements.py")
    return errors


def literature_artifact_gate_errors(workstream: Path, status: dict[str, Any]) -> list[str]:
    if status.get("type") != "literature":
        return []

    required = {
        "search_plan.md": [
            "## Search Queries",
            "## Rationale",
            "## Search Coverage",
        ],
        "followup_queries.md": [
            "## Follow-up Queries",
            "## Effect On Conclusions",
        ],
        "theorem_applicability_matrix.md": [
            "## Applicability Matrix",
            "## Non-Matches Or Caveats",
        ],
    }
    errors: list[str] = []
    for relative, headings in required.items():
        path = artifacts_dir(workstream) / relative
        if not path.exists():
            errors.append(f"{path}: missing required literature artifact")
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            errors.append(f"{path}: required literature artifact is empty")
            continue
        if "TBD" in text:
            errors.append(f"{path}: contains unresolved TBD markers")
        for heading in headings:
            if heading not in text:
                errors.append(f"{path}: missing section {heading}")
    return errors


def report_appears_to_need_computation_gate(workstream: Path, status: dict[str, Any]) -> bool:
    if status.get("type") != "proof":
        return False
    if status.get("status") == "COMPLETE":
        return False
    if status.get("requires_computation_gate") is True:
        return False
    path = report_path(workstream)
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8").lower()
    leading_markers = [
        "leading-order specialization",
        "leading order specialization",
        "leading-order formula",
        "leading order formula",
        "deterministic limit",
        "deterministic equivalence",
        "deterministic equivalent",
        "o_{\\mathbb p}(n)",
        "o_{\\mathbb p}",
    ]
    patch_markers = [
        "paper source has been patched",
        "source has been patched",
        "patch `lem",
        "patch the lemma",
        "lemma statement",
        "modify `lem",
        "modifies `lem",
        "patched at",
    ]
    return any(marker in text for marker in leading_markers) and any(
        marker in text for marker in patch_markers
    )


def computation_gate_errors(
    workstream: Path,
    status: dict[str, Any],
    seen: set[str],
) -> list[str]:
    errors: list[str] = []

    if report_appears_to_need_computation_gate(workstream, status):
        errors.append(
            f"{workstream}: proof report appears to propose or apply a leading-order lemma "
            "source patch, but requires_computation_gate is false. Link a completed "
            "computation workstream before promotion."
        )

    if status.get("requires_computation_gate") is not True:
        return errors

    linked_id = status.get("linked_computation_workstream_id")
    if not isinstance(linked_id, str) or not linked_id.strip():
        errors.append(f"{workstream}: requires_computation_gate=true but no linked computation workstream is set")
        return errors

    try:
        linked_workstream = project_workstream_path(linked_id)
        linked_status = load_status(linked_workstream)
    except ValidationError as exc:
        errors.append(f"{workstream}: invalid linked computation workstream {linked_id!r}: {exc}")
        return errors

    if linked_status.get("type") != "computation":
        errors.append(f"{workstream}: linked workstream {linked_id!r} is not a computation workstream")
        return errors
    if linked_status.get("status") != "COMPLETE":
        errors.append(f"{workstream}: linked computation workstream {linked_id!r} is not COMPLETE")

    linked_errors = readiness_errors(linked_workstream, seen)
    if linked_errors:
        errors.append(f"{workstream}: linked computation workstream {linked_id!r} is not ready")
        errors.extend(f"  {error}" for error in linked_errors)

    return errors


def source_patch_application_errors(workstream: Path, status: dict[str, Any]) -> list[str]:
    if status.get("type") != "proof":
        return []
    if status.get("source_patch_proposed") is not True:
        return []
    if status.get("source_patch_applied") is True:
        return []
    return [
        f"{workstream}: source_patch_proposed=true but source_patch_applied is not true. "
        "After review/computation gates pass, apply the proposed patch with "
        "`python3 scripts/apply_approved_patch.py "
        f"{workstream.relative_to(repo_root())}` before promotion."
    ]


def readiness_errors(
    workstream: Path,
    seen: set[str] | None = None,
    require_source_patch_application: bool = True,
) -> list[str]:
    root_check = seen is None
    if seen is None:
        seen = set()
    resolved = str(workstream.resolve())
    if resolved in seen:
        return [f"{workstream}: cyclic workstream readiness dependency"]
    seen = set(seen)
    seen.add(resolved)

    status = load_status(workstream)
    errors = validate_status_object(status, status_path(workstream))
    errors.extend(report_has_required_sections(report_path(workstream)))
    errors.extend(source_setting_manifest_errors(workstream, status))
    errors.extend(source_faithfulness_test_errors(workstream, status))
    errors.extend(formula_trace_errors(workstream, status))
    errors.extend(raw_object_validation_errors(workstream, status))

    if status.get("status") == "BLOCKED":
        errors.append(f"{workstream}: workstream is BLOCKED")
    if status.get("status") == "DRAFT":
        errors.append(f"{workstream}: workstream is still DRAFT")
    if status.get("blocked_reason"):
        errors.append(f"{workstream}: blocked_reason is set")

    if status.get("tests_required") is True and status.get("tests_passed") is not True:
        errors.append(f"{workstream}: tests are required but not marked passed")
    if status.get("tests_required") is True:
        errors.extend(latest_test_run_errors(workstream))

    reviews = load_reviews(workstream)
    for review in reviews:
        errors.extend(validate_review(review))

    errors.extend(independent_worker_provenance_errors(workstream, status))
    errors.extend(review_gate_errors(workstream, status, reviews))
    errors.extend(theorem_statement_verification_gate_errors(workstream, status))
    errors.extend(literature_artifact_gate_errors(workstream, status))
    errors.extend(computation_gate_errors(workstream, status, seen))
    if root_check and require_source_patch_application:
        errors.extend(source_patch_application_errors(workstream, status))
    if root_check:
        errors.extend(source_guard_errors())

    if status.get("type") == "computation":
        if status.get("golden_values_approved") is not True:
            errors.append(f"{workstream}: golden values are not approved")
        latest = latest_reviews(reviews)
        if not any(review.get("tests_reviewed") is True for review in latest):
            errors.append(f"{workstream}: no reviewer marked tests_reviewed=true")
        if not any(review.get("golden_values_approved") is True for review in latest):
            errors.append(f"{workstream}: no reviewer approved golden values")

    return errors
