#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def run_check(args: list[str], timeout: int = 180) -> dict[str, Any]:
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


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def active_agents() -> list[dict[str, Any]]:
    path = repo_root() / "state" / "agent_registry.json"
    if not path.exists():
        return []
    registry = read_json(path)
    return [
        agent
        for agent in registry.get("agents", [])
        if isinstance(agent, dict) and agent.get("status") in {"SPAWNED", "RUNNING"}
    ]


def active_locks() -> list[dict[str, Any]]:
    path = repo_root() / "state" / "write_locks.json"
    if not path.exists():
        return []
    locks = read_json(path)
    return [
        lock
        for lock in locks.get("locks", [])
        if isinstance(lock, dict) and lock.get("status") in {"SPAWNED", "RUNNING", "ACTIVE"}
    ]


def unfinished_workstreams() -> list[dict[str, Any]]:
    path = repo_root() / "state" / "project_state.json"
    if not path.exists():
        return []
    state = read_json(path)
    rows: list[dict[str, Any]] = []
    for item in state.get("workstreams", []):
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status in {"COMPLETE"}:
            continue
        row = {
            "id": item.get("id"),
            "status": status,
            "path": item.get("path"),
        }
        rel_path = item.get("path")
        if isinstance(rel_path, str):
            status_path = repo_root() / rel_path / "status.json"
            if status_path.exists():
                detail = read_json(status_path)
                row["type"] = detail.get("type")
                row["title"] = detail.get("title")
                row["review_passed"] = detail.get("review_passed")
                row["tests_required"] = detail.get("tests_required")
        rows.append(row)
    return rows


def build_report() -> dict[str, Any]:
    checks = [
        ["scripts/validate_status.py"],
        ["scripts/validate_agent_task.py"],
        ["scripts/check_working_paper.py"],
        ["scripts/list_active_agents.py"],
    ]
    results = [run_check(check) for check in checks]
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source": "scripts/automation_health_check.py",
        "passed": all(result["exit_code"] == 0 for result in results[:3]),
        "checks": results,
        "active_agents": active_agents(),
        "active_write_locks": active_locks(),
        "unfinished_workstreams": unfinished_workstreams(),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a read-only scaffold health check for Codex automations.")
    parser.add_argument("--json", action="store_true", help="Print the full JSON report.")
    parser.add_argument("--output", help="Optional repo-relative output JSON path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    report = build_report()
    if args.output:
        output_path = repo_root() / args.output
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, indent=2, sort_keys=False) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=False))
    else:
        status = "PASS" if report["passed"] else "FAIL"
        print(f"Scaffold health check: {status}")
        print(f"Active agents: {len(report['active_agents'])}")
        print(f"Active write locks: {len(report['active_write_locks'])}")
        print(f"Unfinished workstreams: {len(report['unfinished_workstreams'])}")
        for check in report["checks"]:
            if check["exit_code"] != 0:
                print(f"- failed: {' '.join(check['command'][1:])}")
                print(check["output"].strip())
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
