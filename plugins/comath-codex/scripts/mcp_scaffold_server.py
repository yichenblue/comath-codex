#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable


SERVER_NAME = "comath-codex"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"


def repo_root() -> Path:
    env_root = os.environ.get("COMATH_CODEX_REPO_ROOT")
    candidates: list[Path] = []
    if env_root:
        candidates.append(Path(env_root).expanduser())
    cwd = Path.cwd().resolve()
    candidates.extend([cwd, *cwd.parents])
    candidates.append(Path(__file__).resolve().parents[1])
    for candidate in candidates:
        root = candidate.resolve()
        if (root / "state" / "project_state.json").exists() and (root / "scripts").is_dir():
            return root
    return Path(__file__).resolve().parents[1]


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_response(message_id: Any, result: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message_id, "result": result}) + "\n")
    sys.stdout.flush()


def write_error(message_id: Any, code: int, message: str) -> None:
    sys.stdout.write(
        json.dumps({
            "jsonrpc": "2.0",
            "id": message_id,
            "error": {
                "code": code,
                "message": message,
            },
        })
        + "\n"
    )
    sys.stdout.flush()


def tool_result(payload: Any, *, is_error: bool = False) -> dict[str, Any]:
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, indent=2, sort_keys=False),
            }
        ],
        "isError": is_error,
    }


def repo_relative(path: Path) -> str:
    return str(path.resolve().relative_to(repo_root()))


def run_script(args: list[str], timeout: int = 120) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, *args],
        cwd=repo_root(),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )
    return {
        "command": [sys.executable, *args],
        "exit_code": completed.returncode,
        "output": completed.stdout,
    }


def list_project_state(_: dict[str, Any]) -> dict[str, Any]:
    root = repo_root()
    state = read_json(root / "state" / "project_state.json")
    registry = read_json(root / "state" / "agent_registry.json")
    locks = read_json(root / "state" / "write_locks.json")
    claim_registry_path = root / "working_paper" / "claim_registry.json"
    claim_registry = read_json(claim_registry_path) if claim_registry_path.exists() else {"claims": []}
    return {
        "project_state": state,
        "active_agents": [
            agent
            for agent in registry.get("agents", [])
            if isinstance(agent, dict) and agent.get("status") in {"SPAWNED", "RUNNING"}
        ],
        "active_write_locks": [
            lock
            for lock in locks.get("locks", [])
            if isinstance(lock, dict) and lock.get("status") in {"SPAWNED", "RUNNING", "ACTIVE"}
        ],
        "approved_claim_count": len(claim_registry.get("claims", [])) if isinstance(claim_registry, dict) else 0,
    }


def list_workstreams(arguments: dict[str, Any]) -> dict[str, Any]:
    status_filter = arguments.get("status")
    project_state = read_json(repo_root() / "state" / "project_state.json")
    rows: list[dict[str, Any]] = []
    for item in project_state.get("workstreams", []):
        if not isinstance(item, dict):
            continue
        if isinstance(status_filter, str) and status_filter and item.get("status") != status_filter:
            continue
        row = dict(item)
        rel_path = item.get("path")
        if isinstance(rel_path, str) and (repo_root() / rel_path / "status.json").exists():
            status = read_json(repo_root() / rel_path / "status.json")
            row["type"] = status.get("type")
            row["title"] = status.get("title")
            row["review_passed"] = status.get("review_passed")
            row["tests_required"] = status.get("tests_required")
        rows.append(row)
    return {"workstreams": rows}


def get_workstream(arguments: dict[str, Any]) -> dict[str, Any]:
    workstream_id = arguments.get("workstream_id")
    if not isinstance(workstream_id, str) or not workstream_id:
        raise ValueError("workstream_id is required")
    workstream = repo_root() / "workstreams" / workstream_id
    status_path = workstream / "status.json"
    if not status_path.exists():
        raise ValueError(f"Unknown workstream: {workstream_id}")
    report_path = workstream / "report.md"
    report_excerpt = ""
    if report_path.exists():
        report_excerpt = report_path.read_text(encoding="utf-8", errors="replace")[:8000]
    return {
        "status": read_json(status_path),
        "report_path": repo_relative(report_path) if report_path.exists() else None,
        "report_excerpt": report_excerpt,
        "review_files": [
            repo_relative(path)
            for path in sorted((workstream / "review").glob("*.json"))
        ],
        "artifact_files": [
            repo_relative(path)
            for path in sorted((workstream / "artifacts").rglob("*"))
            if path.is_file()
        ][:200],
    }


def list_tasks(arguments: dict[str, Any]) -> dict[str, Any]:
    workstream_id = arguments.get("workstream_id")
    status_filter = arguments.get("status")
    tasks: list[dict[str, Any]] = []
    for path in sorted((repo_root() / "tasks").glob("task_*.json")):
        task = read_json(path)
        if isinstance(workstream_id, str) and workstream_id and task.get("workstream_id") != workstream_id:
            continue
        if isinstance(status_filter, str) and status_filter and task.get("status") != status_filter:
            continue
        tasks.append({
            "path": repo_relative(path),
            "task_id": task.get("task_id"),
            "agent_type": task.get("agent_type"),
            "workstream_id": task.get("workstream_id"),
            "status": task.get("status"),
            "spawn_status": task.get("spawn_status"),
            "assigned_agent_id": task.get("assigned_agent_id"),
            "structured_output_required": task.get("structured_output_required"),
            "output_schema": task.get("output_schema"),
        })
    return {"tasks": tasks}


def check_report_ready(arguments: dict[str, Any]) -> dict[str, Any]:
    workstream_id = arguments.get("workstream_id")
    if not isinstance(workstream_id, str) or not workstream_id:
        raise ValueError("workstream_id is required")
    return run_script(["scripts/check_report_ready.py", f"workstreams/{workstream_id}"])


def run_health_check(_: dict[str, Any]) -> dict[str, Any]:
    checks = [
        ["scripts/validate_status.py"],
        ["scripts/validate_agent_task.py"],
        ["scripts/check_working_paper.py"],
        ["scripts/list_active_agents.py"],
    ]
    results = [run_script(check) for check in checks]
    return {
        "passed": all(result["exit_code"] == 0 for result in results[:3]),
        "results": results,
    }


TOOLS: dict[str, tuple[str, dict[str, Any], Callable[[dict[str, Any]], dict[str, Any]]]] = {
    "list_project_state": (
        "Return project state, active agents, active write locks, and approved claim count.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        list_project_state,
    ),
    "list_workstreams": (
        "List workstreams, optionally filtered by status.",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "status": {"type": "string"}
            },
        },
        list_workstreams,
    ),
    "get_workstream": (
        "Return one workstream's status, report excerpt, review files, and artifact files.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["workstream_id"],
            "properties": {
                "workstream_id": {"type": "string"}
            },
        },
        get_workstream,
    ),
    "list_tasks": (
        "List agent task records, optionally filtered by workstream id or task status.",
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "workstream_id": {"type": "string"},
                "status": {"type": "string"},
            },
        },
        list_tasks,
    ),
    "check_report_ready": (
        "Run the report-readiness gate for one workstream.",
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["workstream_id"],
            "properties": {
                "workstream_id": {"type": "string"}
            },
        },
        check_report_ready,
    ),
    "run_health_check": (
        "Run read-only scaffold health checks and return command outputs.",
        {"type": "object", "additionalProperties": False, "properties": {}},
        run_health_check,
    ),
}


def handle_initialize(message_id: Any) -> None:
    write_response(
        message_id,
        {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        },
    )


def handle_tools_list(message_id: Any) -> None:
    write_response(
        message_id,
        {
            "tools": [
                {
                    "name": name,
                    "description": description,
                    "inputSchema": input_schema,
                }
                for name, (description, input_schema, _) in TOOLS.items()
            ]
        },
    )


def handle_tools_call(message_id: Any, params: dict[str, Any]) -> None:
    name = params.get("name")
    arguments = params.get("arguments") or {}
    if not isinstance(name, str) or name not in TOOLS:
        write_response(message_id, tool_result({"error": f"Unknown tool: {name}"}, is_error=True))
        return
    if not isinstance(arguments, dict):
        write_response(message_id, tool_result({"error": "arguments must be an object"}, is_error=True))
        return
    _, _, func = TOOLS[name]
    try:
        payload = func(arguments)
    except Exception as exc:
        write_response(message_id, tool_result({"error": str(exc)}, is_error=True))
        return
    write_response(message_id, tool_result(payload))


def serve() -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        message_id = message.get("id")
        method = message.get("method")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if message_id is None:
            continue
        if method == "initialize":
            handle_initialize(message_id)
        elif method == "tools/list":
            handle_tools_list(message_id)
        elif method == "tools/call":
            handle_tools_call(message_id, params)
        else:
            write_error(message_id, -32601, f"Method not found: {method}")
    return 0


if __name__ == "__main__":
    raise SystemExit(serve())
