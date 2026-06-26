#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def find_repo_root() -> Path:
    current = Path.cwd().resolve()
    for candidate in [current, *current.parents]:
        if (candidate / "scripts" / "promote_workstream.py").exists() and (candidate / "state").exists():
            return candidate
    return current


def read_payload() -> Any:
    try:
        raw = sys.stdin.read()
    except OSError:
        return None
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def collect_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            out.extend(collect_strings(key))
            out.extend(collect_strings(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(collect_strings(item))
        return out
    return []


def run_check(root: Path, args: list[str]) -> tuple[int, str]:
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    completed = subprocess.run(
        [sys.executable, *args],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def block(message: str) -> int:
    print(f"comath-codex hard gate blocked this action: {message}", file=sys.stderr)
    return 1


def pre_tool_gate(root: Path, payload: Any) -> int:
    text = "\n".join(collect_strings(payload))
    if not text:
        return 0

    normalized = text.replace("\\/", "/")

    if "working_paper/claim_registry.json" in normalized and "scripts/update_claim_registry.py" not in normalized:
        return block("claim registry must be updated through scripts/update_claim_registry.py")

    if "scripts/promote_workstream.py" in normalized:
        workstreams = sorted(set(re.findall(r"workstreams/[A-Za-z0-9_.:-]+", normalized)))
        for workstream in workstreams:
            code, output = run_check(root, ["scripts/check_report_ready.py", workstream])
            if code != 0:
                detail = output or f"{workstream} is not ready for promotion"
                return block(detail)

    if "scripts/update_claim_registry.py" in normalized:
        code, output = run_check(root, ["scripts/validate_status.py"])
        if code != 0:
            return block(output or "status validation failed before claim registry update")

    return 0


def stop_gate(root: Path) -> int:
    hard_checks = [
        ["scripts/validate_status.py"],
        ["scripts/validate_agent_task.py"],
    ]
    failures: list[str] = []
    warnings: list[str] = []
    for args in hard_checks:
        script = root / args[0]
        if not script.exists():
            continue
        code, output = run_check(root, args)
        if code != 0:
            failures.append(f"$ {' '.join(args)}\n{output}")

    working_paper_check = ["scripts/check_working_paper.py"]
    if (root / working_paper_check[0]).exists():
        code, output = run_check(root, working_paper_check)
        message = f"$ {' '.join(working_paper_check)}\n{output}"
        if code != 0 and os.environ.get("COMATH_CODEX_STRICT_WORKING_PAPER_GATE") == "1":
            failures.append(message)
        elif code != 0:
            warnings.append(message)

    if failures:
        print("comath-codex stop gate found scaffold validation failures:", file=sys.stderr)
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    if warnings:
        print("comath-codex stop gate warnings:", file=sys.stderr)
        for warning in warnings:
            print(warning, file=sys.stderr)
    return 0


def main() -> int:
    event = sys.argv[1] if len(sys.argv) > 1 else "stop"
    root = find_repo_root()
    payload = read_payload()
    if event == "pre_tool":
        return pre_tool_gate(root, payload)
    if event == "stop":
        return stop_gate(root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
