#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

from lib import ValidationError, load_project_state, project_state_path, repo_root, validate_goal_approval_gate


STANDARD_PREFIXES = [
    "use the current scaffold standard way to handle this task:",
    "use the current scaffold's standard way to handle this task:",
    "use the scaffold standard way to handle this task:",
    "use the scaffold's standard way to handle this task:",
]

LABEL_COMMAND_RE = re.compile(r"\\label\{([^{}]+)\}")
RAW_LABEL_RE = re.compile(
    r"\b(?:lem|lemma|prop|proposition|thm|theorem|cor|eq|equation|fig|tab|table|def|definition|ass|assumption|alg):"
    r"[A-Za-z0-9_.:-]+\b"
)
BACKTICK_TEX_PATH_RE = re.compile(r"`([^`]+\.tex)`")
LOOSE_TEX_PATH_RE = re.compile(r"(?:(?:\.\.?/|/)?[^\s`\"'，。；;]+\.tex)")


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def strip_standard_prefix(text: str) -> str:
    stripped = text.strip()
    lowered = stripped.lower()
    for prefix in STANDARD_PREFIXES:
        index = lowered.find(prefix.lower())
        if index >= 0:
            return stripped[index + len(prefix) :].strip()
    return stripped


def infer_goal_id() -> str:
    state = load_project_state()
    validate_goal_approval_gate(state, project_state_path())
    goals = state.get("goals", [])
    candidates = [
        item.get("id")
        for item in goals
        if isinstance(item, dict) and item.get("status") == "APPROVED" and isinstance(item.get("id"), str)
    ]
    if not candidates:
        raise ValidationError(
            "Could not infer an APPROVED goal id from state/project_state.json. "
            "Create a DRAFT goal, then approve it with scripts/approve_goal.py after explicit user confirmation."
        )

    def sort_key(goal_id: str) -> tuple[int, str]:
        match = re.match(r"goal_(\d+)$", goal_id)
        return (int(match.group(1)) if match else -1, goal_id)

    return max(candidates, key=sort_key)


def extract_labels(text: str) -> list[str]:
    labels = LABEL_COMMAND_RE.findall(text)
    labels.extend(RAW_LABEL_RE.findall(text))
    return unique(labels)


def title_from_label(label: str) -> str:
    tail = label.split(":", 1)[-1]
    words = re.split(r"[_\-.]+", tail)
    cleaned = [word for word in words if word]
    if not cleaned:
        return "Standard Scaffold Task"
    return " ".join(word.upper() if word.lower() in {"sgd", "de"} else word.capitalize() for word in cleaned)


def infer_title(objective: str, labels: list[str]) -> str:
    if labels:
        return title_from_label(labels[0])
    compact = re.sub(r"\s+", " ", objective).strip()
    compact = re.sub(r"[`$\\{}]+", "", compact)
    if not compact:
        return "Standard Scaffold Task"
    return compact[:80].rstrip(" ,.;:，。；：") or "Standard Scaffold Task"


def path_for_command(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo_root()))
    except ValueError:
        return os.path.relpath(path.resolve(), repo_root())


def resolve_tex_path(raw: str) -> str | None:
    cleaned = raw.strip().strip("`'\"")
    if not cleaned:
        return None
    path = Path(cleaned)
    candidates = []
    if path.is_absolute():
        candidates.append(path)
    else:
        candidates.append(repo_root() / path)
        candidates.append(repo_root().parent / path)
    for candidate in candidates:
        if candidate.exists() and candidate.suffix == ".tex":
            return path_for_command(candidate)
    return None


def explicit_source_contexts(text: str) -> list[str]:
    contexts: list[str] = []
    for raw in BACKTICK_TEX_PATH_RE.findall(text):
        resolved = resolve_tex_path(raw)
        if resolved:
            contexts.append(resolved)
    for raw in LOOSE_TEX_PATH_RE.findall(text):
        resolved = resolve_tex_path(raw)
        if resolved:
            contexts.append(resolved)
    return unique(contexts)


def skip_source_search_path(path: Path) -> bool:
    blocked_parts = {
        ".git",
        "__pycache__",
        "agent_runs",
        "comath-codex",
        "failed_explorations",
        "tasks",
        "workstreams",
    }
    return any(part in blocked_parts for part in path.parts)


def source_contexts_for_labels(labels: list[str], max_files: int = 6) -> list[str]:
    if not labels:
        return []
    contexts: list[str] = []
    search_root = repo_root().parent
    needles = [f"\\label{{{label}}}" for label in labels]
    for path in search_root.rglob("*.tex"):
        if skip_source_search_path(path.relative_to(search_root)):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if any(needle in text for needle in needles):
            contexts.append(path_for_command(path))
            if len(contexts) >= max_files:
                break
    return unique(contexts)


def build_cycle_command(args: argparse.Namespace) -> tuple[list[str], dict[str, object]]:
    objective = strip_standard_prefix(args.objective)
    if not objective:
        raise ValidationError("Objective is empty after removing the standard scaffold prefix.")

    labels = unique(list(args.target_label) + extract_labels(objective))
    source_contexts = unique(
        list(args.source_context)
        + explicit_source_contexts(objective)
        + source_contexts_for_labels(labels)
    )
    goal_id = args.goal_id or infer_goal_id()
    title = args.title or infer_title(objective, labels)

    command = [
        sys.executable,
        "scripts/run_research_cycle.py",
        "--goal-id",
        goal_id,
        "--title",
        title,
        "--objective",
        objective,
    ]
    for label in labels:
        command.extend(["--target-label", label])
    for source_context in source_contexts:
        command.extend(["--source-context", source_context])
    if args.force_computation:
        command.append("--force-computation")
    if args.no_computation:
        command.append("--no-computation")
    if args.force_literature:
        command.append("--force-literature")
    if args.no_literature:
        command.append("--no-literature")
    if args.no_spawn:
        command.append("--no-spawn")
    if args.no_wait:
        command.append("--no-wait")
    command.extend(["--timeout-seconds", str(args.timeout_seconds)])
    command.extend(["--poll-interval", str(args.poll_interval)])
    if args.model:
        command.extend(["--model", args.model])
    command.extend(["--sandbox", args.sandbox])
    command.extend(["--ask-for-approval", args.ask_for_approval])
    if args.no_finalize_outputs:
        command.append("--no-finalize-outputs")

    metadata = {
        "goal_id": goal_id,
        "title": title,
        "objective": objective,
        "target_labels": labels,
        "source_contexts": source_contexts,
    }
    return command, metadata


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the scaffold's standard task flow from a short user objective. "
            "This infers goal/title/labels/source context and delegates to run_research_cycle.py."
        )
    )
    parser.add_argument("--objective", required=True, help="Task text, optionally including the standard scaffold prefix.")
    parser.add_argument("--goal-id", help="Override inferred goal id. Defaults to latest APPROVED goal.")
    parser.add_argument("--title", help="Override inferred workstream title.")
    parser.add_argument("--target-label", action="append", default=[])
    parser.add_argument("--source-context", action="append", default=[])
    parser.add_argument("--force-computation", action="store_true")
    parser.add_argument("--no-computation", action="store_true")
    parser.add_argument("--force-literature", action="store_true")
    parser.add_argument("--no-literature", action="store_true")
    parser.add_argument("--no-spawn", action="store_true")
    parser.add_argument("--no-wait", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--model", default="")
    parser.add_argument("--sandbox", default="workspace-write", choices=["read-only", "workspace-write", "danger-full-access"])
    parser.add_argument("--ask-for-approval", default="never", choices=["untrusted", "on-request", "on-failure", "never"])
    parser.add_argument("--no-finalize-outputs", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="Print inferred metadata and command without running it.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.force_computation and args.no_computation:
        print("--force-computation and --no-computation cannot be used together")
        return 2
    if args.force_literature and args.no_literature:
        print("--force-literature and --no-literature cannot be used together")
        return 2
    try:
        command, metadata = build_cycle_command(args)
        print("Standard scaffold task inference:")
        for key, value in metadata.items():
            print(f"- {key}: {value}")
        print("Delegated command:")
        print(shlex.join(command))
        if args.dry_run:
            return 0
        completed = subprocess.run(command, cwd=repo_root(), text=True)
        return completed.returncode
    except ValidationError as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
