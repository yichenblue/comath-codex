#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from lib import (
    AGENT_PROMPT_FILES,
    ValidationError,
    append_message,
    assembled_prompts_dir,
    load_agent_task,
    read_json,
    repo_root,
)


def read_text_file(rel_path: str) -> str:
    path = repo_root() / rel_path
    return path.read_text(encoding="utf-8")


def render_prompt(task: dict) -> str:
    prompt_file = AGENT_PROMPT_FILES[task["agent_type"]]
    role_prompt = read_text_file(prompt_file)

    lines: list[str] = [
        role_prompt.rstrip(),
        "",
        "# Assigned Agent Task",
        "",
        f"Task id: `{task['task_id']}`",
        f"Agent type: `{task['agent_type']}`",
        f"Workstream id: `{task['workstream_id']}`",
        f"Task status: `{task['status']}`",
        f"Execution mode: `{task.get('execution_mode', 'local_thread')}`",
        f"Spawn status: `{task.get('spawn_status', 'NOT_SPAWNED')}`",
        f"Assigned agent id: `{task.get('assigned_agent_id')}`",
    ]
    if task.get("parent_task_id") or task.get("subagent_request_id"):
        lines.extend([
            f"Parent task id: `{task.get('parent_task_id')}`",
            f"Sub-agent request id: `{task.get('subagent_request_id')}`",
        ])
    lines.extend([
        "",
        "## Objective",
        "",
        str(task["objective"]),
        "",
        "## Allowed Write Paths",
        "",
    ])
    for path in task["allowed_write_paths"]:
        lines.append(f"- `{path}`")

    lines.extend([
        "",
        "## Required Outputs",
        "",
    ])
    for path in task["required_outputs"]:
        lines.append(f"- `{path}`")

    lines.extend([
        "",
        "## Success Criteria",
        "",
    ])
    for item in task["success_criteria"]:
        lines.append(f"- {item}")

    if task.get("notes"):
        lines.extend(["", "## Notes", "", str(task["notes"])])

    lines.extend([
        "",
        "## Input File Contents",
        "",
    ])
    for rel_path in task["input_files"]:
        text = read_text_file(rel_path)
        lines.extend([
            f"### `{rel_path}`",
            "",
            "```text",
            text.rstrip(),
            "```",
            "",
        ])

    lines.extend([
        "## Execution Rules",
        "",
        "- You are an independent worker process. Use only the files, task prompt, and repo-local context you can inspect yourself.",
        "- Work only within the allowed write paths.",
        "- You are not alone in the codebase; other Codex workers may be editing their own allowed paths concurrently.",
        "- Do not revert, overwrite, or reformat work outside your allowed write paths.",
        "- Assume a collector will reject this task if the final file diff touches paths outside allowed_write_paths.",
        "- Do not mark a workstream COMPLETE manually.",
        "- If blocked, explain the blocker in the report and ask the project coordinator to run `mark_blocked.py`.",
        "- Preserve uncertainty and failed attempts explicitly.",
        "- Do not introduce new notation unless necessary; define and justify any necessary new notation at first use.",
    ])
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assemble a ready-to-send Codex worker prompt from a task JSON file.")
    parser.add_argument("task", help="Path to task JSON.")
    parser.add_argument("--stdout", action="store_true", help="Print prompt instead of writing to tasks/assembled_prompts/.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    task_path = Path(args.task).resolve()
    try:
        task = load_agent_task(task_path)
        prompt = render_prompt(task)
        if args.stdout:
            print(prompt, end="")
        else:
            assembled_prompts_dir().mkdir(parents=True, exist_ok=True)
            output = assembled_prompts_dir() / f"{task['task_id']}.md"
            output.write_text(prompt, encoding="utf-8")
            append_message(
                "agent_prompt_assembled",
                task["workstream_id"],
                f"Assembled prompt for task {task['task_id']}.",
                task_path=str(task_path.relative_to(repo_root())),
                prompt_path=str(output.relative_to(repo_root())),
            )
            print(f"Wrote {output}")
    except ValidationError as exc:
        print(exc)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
