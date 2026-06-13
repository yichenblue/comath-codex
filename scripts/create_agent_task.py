#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone

from lib import (
    AGENT_TYPES,
    ValidationError,
    append_message,
    load_status,
    project_workstream_path,
    repo_root,
    safe_id,
    scaffold_lock,
    tasks_dir,
    validate_agent_task,
    write_json,
)


DEFAULT_REQUIRED_OUTPUTS = {
    "literature": [
        "report.md",
        "artifacts/search_plan.md",
        "artifacts/literature_search_results.json",
        "artifacts/literature_search.md",
        "artifacts/followup_queries.md",
        "artifacts/literature_sources/source_manifest.json",
        "artifacts/extracted_theorem_statements.json",
        "artifacts/extracted_theorem_statements.md",
        "artifacts/sources.md",
        "artifacts/theorem_statements.md",
        "artifacts/theorem_applicability_matrix.md",
        "artifacts/theorem_statement_verification.json",
        "artifacts/literature_gaps.md",
    ],
    "proof": ["report.md", "artifacts/proof_sketches.md", "artifacts/gap_list.md", "artifacts/counterexamples.md"],
    "computation": [
        "report.md",
        "artifacts/source_setting_manifest.json",
        "artifacts/source_faithfulness_tests.json",
        "artifacts/formula_trace.json",
        "artifacts/raw_object_validation.json",
        "artifacts/test_run.json",
    ],
    "reviewer": ["review/review_result.json"],
    "synthesis": ["working_paper/claim_registry.json", "working_paper/approved_claims.tex"],
    "workstream_coordinator": ["report.md"],
}


def rel(path) -> str:
    return str(path.resolve().relative_to(repo_root()))


def next_task_id(agent_type: str, workstream_id: str) -> str:
    prefix = f"task_"
    highest = 0
    for path in tasks_dir().glob("task_*.json"):
        stem = path.stem
        parts = stem.split("_")
        if len(parts) >= 2 and parts[0] == "task" and parts[1].isdigit():
            highest = max(highest, int(parts[1]))
    return f"{prefix}{highest + 1:03d}_{safe_id(agent_type)}_{safe_id(workstream_id)}"


def existing_workstream_files(workstream_path, rel_paths: list[str]) -> list[str]:
    files: list[str] = []
    for rel_path in rel_paths:
        path = workstream_path / rel_path
        if path.exists() and path.is_file():
            files.append(rel(path))
    return files


def default_input_files(workstream_path, agent_type: str | None = None) -> list[str]:
    status = load_status(workstream_path)
    goal_id = status["goal_id"]
    files = [
        "research_question.md",
        f"goals/{goal_id}.md",
        rel(workstream_path / "instructions.md"),
        rel(workstream_path / "report.md"),
        rel(workstream_path / "status.json"),
    ]
    if agent_type == "reviewer":
        files.extend(existing_workstream_files(workstream_path, [
            "artifacts/search_plan.md",
            "artifacts/literature_search_results.json",
            "artifacts/literature_search.md",
            "artifacts/followup_queries.md",
            "artifacts/literature_sources/source_manifest.json",
            "artifacts/extracted_theorem_statements.json",
            "artifacts/extracted_theorem_statements.md",
            "artifacts/sources.md",
            "artifacts/theorem_statements.md",
            "artifacts/theorem_applicability_matrix.md",
            "artifacts/theorem_statement_verification.json",
            "artifacts/literature_gaps.md",
            "artifacts/proof_sketches.md",
            "artifacts/gap_list.md",
            "artifacts/counterexamples.md",
            "artifacts/source_patches/source_patch_summary.md",
            "artifacts/source_setting_manifest.json",
            "artifacts/source_faithfulness_tests.json",
            "artifacts/formula_trace.json",
            "artifacts/raw_object_validation.json",
            "artifacts/test_run.json",
        ]))
    return list(dict.fromkeys(files))


def default_success_criteria(agent_type: str) -> list[str]:
    criteria = [
        "Stay within allowed_write_paths.",
        "Do not edit files outside this task's allowed_write_paths, even if they appear relevant.",
        "Do not revert or overwrite edits made by other concurrent agents.",
        "Update report.md with actions taken, claims, evidence, uncertainty, failed attempts, and next steps when report.md is in scope.",
        "Use C-### ids for substantive claims that should be reviewed.",
        "For computation tasks, complete artifacts/source_setting_manifest.json before running the main experiment or claiming results.",
        "For computation tasks, complete artifacts/source_faithfulness_tests.json with all required checks PASS before claiming results.",
        "For computation tasks, complete artifacts/formula_trace.json for every theorem, deterministic-equivalent, or contour formula implemented in code.",
        "For computation tasks, complete artifacts/raw_object_validation.json with one PASS validation for every raw_finite_object in source_setting_manifest.json.",
        "Do not introduce new notation unless necessary; define and justify any necessary new notation at first use.",
        "If a proof task proposes or patches a leading-order paper-source formula, require a linked completed computation workstream before promotion.",
        "Do not edit paper source files directly. Put proposed source edits in artifacts/source_patches/proposed_source_patch.diff.",
        "Do not mark workstream COMPLETE manually.",
    ]
    if agent_type == "reviewer":
        criteria.extend([
            "Do not run repo-wide searches such as `rg ... .` or `find .`; restrict reads to embedded inputs, the current workstream, explicitly cited dependency workstreams, scripts, and cited source files.",
            "Do not search `agent_runs/`, `tasks/`, or `tasks/assembled_prompts/` unless the objective explicitly asks you to debug worker infrastructure.",
            "For proof audits with source_patch_proposed=false and requires_computation_gate=false, use the lightweight review path: status, report, standard artifacts, claim ids, error decomposition, cited source anchors, and source-patch flags.",
        ])
    return criteria


def default_allowed_write_paths(agent_type: str, workstream_path) -> list[str]:
    if agent_type == "synthesis":
        return [
            "working_paper/working_paper.tex",
            "working_paper/annotations.md",
            "working_paper/claim_registry.json",
            "working_paper/approved_claims.tex",
        ]

    paths = [
        rel(workstream_path / "report.md"),
        rel(workstream_path / "logs"),
    ]
    if agent_type == "computation":
        paths.extend([
            rel(workstream_path / "src"),
            rel(workstream_path / "tests"),
            rel(workstream_path / "artifacts" / "results"),
        ])
    if agent_type == "literature":
        paths.append(rel(workstream_path / "artifacts" / "literature_sources"))
    if agent_type == "proof":
        paths.append(rel(workstream_path / "artifacts" / "source_patches"))
    for output in DEFAULT_REQUIRED_OUTPUTS.get(agent_type, []):
        if output.startswith("working_paper/"):
            paths.append(output)
        else:
            paths.append(rel(workstream_path / output))
    return sorted(set(paths))


def default_required_outputs(agent_type: str, workstream_path) -> list[str]:
    outputs: list[str] = []
    for output in DEFAULT_REQUIRED_OUTPUTS.get(agent_type, ["report.md"]):
        if output.startswith("working_paper/"):
            outputs.append(output)
        else:
            outputs.append(rel(workstream_path / output))
    return outputs


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a Codex worker task JSON file.")
    parser.add_argument("--agent-type", required=True, choices=sorted(AGENT_TYPES))
    parser.add_argument("--workstream-id", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--task-id", help="Optional explicit task id.")
    parser.add_argument("--status", default="READY", choices=["DRAFT", "READY"])
    parser.add_argument("--execution-mode", default="codex_cli", choices=["local_thread", "codex_worker", "codex_cli", "external"])
    parser.add_argument("--parent-task-id")
    parser.add_argument("--parent-workstream-id")
    parser.add_argument("--subagent-request-id")
    parser.add_argument("--notes", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        with scaffold_lock():
            workstream_path = project_workstream_path(args.workstream_id)
            task_id = safe_id(args.task_id) if args.task_id else next_task_id(args.agent_type, args.workstream_id)
            allowed_write_paths = default_allowed_write_paths(args.agent_type, workstream_path)
            required_outputs = default_required_outputs(args.agent_type, workstream_path)
            if args.agent_type == "reviewer":
                review_output = rel(workstream_path / "review" / f"{task_id}.json")
                allowed_write_paths = [review_output]
                required_outputs = [review_output]
            task = {
                "task_id": task_id,
                "agent_type": args.agent_type,
                "workstream_id": args.workstream_id,
                "status": args.status,
                "execution_mode": args.execution_mode,
                "spawn_status": "NOT_SPAWNED",
                "assigned_agent_id": None,
                "parent_task_id": args.parent_task_id,
                "parent_workstream_id": args.parent_workstream_id,
                "subagent_request_id": args.subagent_request_id,
                "objective": args.objective,
                "input_files": default_input_files(workstream_path, args.agent_type),
                "allowed_write_paths": allowed_write_paths,
                "required_outputs": required_outputs,
                "success_criteria": default_success_criteria(args.agent_type),
                "notes": args.notes,
                "handoff_summary": "",
                "completion_summary": "",
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            path = tasks_dir() / f"{task_id}.json"
            if path.exists():
                raise ValidationError(f"Task already exists: {path}")
            errors = validate_agent_task(task, path)
            if errors:
                raise ValidationError("\n".join(errors))
            write_json(path, task)
            append_message(
                "agent_task_created",
                args.workstream_id,
                f"Created task {task_id} for {args.agent_type}.",
                task_path=str(path.relative_to(repo_root())),
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Created {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
