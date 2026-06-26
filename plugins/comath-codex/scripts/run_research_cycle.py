#!/usr/bin/env python3
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

from lib import (
    ValidationError,
    load_agent_task,
    load_status,
    mark_status_blocked,
    project_workstream_path,
    read_json,
    release_write_locks,
    repo_root,
    save_agent_task,
    scaffold_lock,
    source_guard_errors,
    status_path,
    task_path_from_arg,
    task_requires_computation,
    task_requires_literature,
    tasks_dir,
    write_json,
)


def run_command(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        argv,
        cwd=repo_root(),
        text=True,
        capture_output=True,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    if check and completed.returncode != 0:
        raise ValidationError(f"Command failed with exit code {completed.returncode}: {' '.join(argv)}")
    return completed


def parse_created_workstream(output: str) -> str:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if line.startswith("Created workstreams/"):
            return line.removeprefix("Created workstreams/")
    raise ValidationError("Could not parse created workstream id from create_workstream.py output")


def parse_created_task(output: str) -> str:
    for line in reversed(output.splitlines()):
        line = line.strip()
        if "tasks/" in line and line.endswith(".json"):
            path = Path(line.split("tasks/", 1)[1])
            return path.stem
    raise ValidationError("Could not parse created task id from create_agent_task.py output")


def create_workstream(
    goal_id: str,
    workstream_type: str,
    title: str,
    *,
    requires_computation_gate: bool = False,
    linked_computation_workstream_id: str | None = None,
    computation_gate_reason: str | None = None,
) -> str:
    command = [
        sys.executable,
        "scripts/create_workstream.py",
        "--goal-id",
        goal_id,
        "--type",
        workstream_type,
        "--title",
        title,
    ]
    if requires_computation_gate:
        command.append("--requires-computation-gate")
    if linked_computation_workstream_id:
        command.extend(["--linked-computation-workstream-id", linked_computation_workstream_id])
    if computation_gate_reason:
        command.extend(["--computation-gate-reason", computation_gate_reason])
    completed = run_command(command)
    return parse_created_workstream(completed.stdout)


def start_workstream(workstream_id: str) -> None:
    run_command([sys.executable, "scripts/start_workstream.py", f"workstreams/{workstream_id}"])


def create_task(agent_type: str, workstream_id: str, objective: str, notes: str = "") -> str:
    command = [
        sys.executable,
        "scripts/create_agent_task.py",
        "--agent-type",
        agent_type,
        "--workstream-id",
        workstream_id,
        "--objective",
        objective,
    ]
    if notes:
        command.extend(["--notes", notes])
    completed = run_command(command)
    return parse_created_task(completed.stdout)


def append_task_inputs(task_id: str, inputs: list[str]) -> None:
    with scaffold_lock():
        task_path = task_path_from_arg(task_id)
        task = load_agent_task(task_path)
        current = list(task.get("input_files", []))
        for item in inputs:
            if item not in current:
                current.append(item)
        task["input_files"] = current
        write_json(task_path, task)


def update_status_metadata(workstream_id: str, metadata: dict) -> None:
    with scaffold_lock():
        workstream = repo_root() / "workstreams" / workstream_id
        status = load_status(workstream)
        status.update(metadata)
        write_json(status_path(workstream), status)


def spawn_task(task_id: str, args: argparse.Namespace) -> None:
    command = [sys.executable, "scripts/spawn_task_cli.py", f"tasks/{task_id}.json"]
    if args.model:
        command.extend(["--model", args.model])
    command.extend(["--sandbox", args.sandbox])
    command.extend(["--ask-for-approval", args.ask_for_approval])
    command.extend(["--worker-timeout-seconds", str(args.timeout_seconds)])
    run_command(command)


def collect_task(task_id: str) -> None:
    run_command([sys.executable, "scripts/collect_task_cli.py", f"tasks/{task_id}.json"])


def pid_is_running(pid: int | None) -> bool:
    if not pid:
        return False
    completed = subprocess.run(
        ["ps", "-p", str(pid), "-o", "pid="],
        text=True,
        capture_output=True,
    )
    return completed.returncode == 0 and bool(completed.stdout.strip())


def worker_wait_state(task_id: str) -> str:
    task_path = task_path_from_arg(task_id)
    task = read_json(task_path)
    runner = task.get("runner")
    if not isinstance(runner, dict) or not runner.get("run_dir"):
        return "missing_run_dir"
    run_dir = repo_root() / str(runner["run_dir"])
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


def wait_for_task_exit(task_id: str, timeout_seconds: int, poll_interval: float) -> str:
    started = time.monotonic()
    recoverable_failed_states = {"dead", "missing_run_dir", "missing_run_json", "bad_run_json"}
    while True:
        state = worker_wait_state(task_id)
        if state == "finished":
            return state
        if state in recoverable_failed_states:
            print(f"Worker task {task_id} entered recoverable failed state: {state}.")
            return state
        elapsed = time.monotonic() - started
        if elapsed > timeout_seconds:
            raise ValidationError(f"Timed out waiting for task {task_id} to write exit_status.json")
        time.sleep(poll_interval)


def run_worker(task_id: str, args: argparse.Namespace) -> None:
    if args.no_spawn:
        print(f"Prepared task {task_id}; --no-spawn set, not launching worker.")
        return
    spawn_task(task_id, args)
    if args.no_wait:
        print(f"Spawned task {task_id}; --no-wait set, not collecting yet.")
        return
    state = wait_for_task_exit(task_id, args.timeout_seconds + 30, args.poll_interval)
    if state != "finished":
        print(f"Invoking collection recovery for task {task_id} after state {state}.")
    collect_task(task_id)


def submit_for_review(workstream_id: str) -> None:
    run_command([sys.executable, "scripts/submit_for_review.py", f"workstreams/{workstream_id}"])


def promote(workstream_id: str) -> None:
    run_command([sys.executable, "scripts/promote_workstream.py", f"workstreams/{workstream_id}"])


def unique_workstream_ids(workstream_ids: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for workstream_id in workstream_ids:
        if workstream_id not in seen:
            seen.add(workstream_id)
            result.append(workstream_id)
    return result


def workstream_tasks(workstream_id: str) -> list[tuple[Path, dict]]:
    tasks: list[tuple[Path, dict]] = []
    for path in sorted(tasks_dir().glob("task_*.json")):
        try:
            task = load_agent_task(path)
        except ValidationError:
            continue
        if task.get("workstream_id") == workstream_id:
            tasks.append((path, task))
    return tasks


def recover_collectable_tasks(workstream_id: str) -> None:
    recoverable_states = {"finished", "dead", "missing_run_dir", "missing_run_json", "bad_run_json"}
    for path, task in workstream_tasks(workstream_id):
        if task.get("spawn_status") not in {"SPAWNED", "RUNNING"}:
            continue
        task_id = str(task["task_id"])
        try:
            state = worker_wait_state(task_id)
        except ValidationError:
            state = "bad_run_json"
        if state not in recoverable_states:
            continue
        completed = run_command(
            [sys.executable, "scripts/collect_task_cli.py", str(path)],
            check=False,
        )
        if completed.returncode not in {0, 1}:
            print(
                f"Cleanup collection for {task_id} returned {completed.returncode}; "
                "leaving task state unchanged.",
                file=sys.stderr,
            )


def workstream_has_active_task(workstream_id: str) -> bool:
    for _, task in workstream_tasks(workstream_id):
        if task.get("spawn_status") in {"SPAWNED", "RUNNING"}:
            return True
    return False


def block_unfinished_tasks(workstream_id: str, summary: str) -> int:
    with scaffold_lock():
        blocked = 0
        for path, task in workstream_tasks(workstream_id):
            if task.get("status") in {"DONE", "BLOCKED"}:
                continue
            if task.get("spawn_status") in {"SPAWNED", "RUNNING"}:
                continue
            task["status"] = "BLOCKED"
            task["spawn_status"] = "BLOCKED"
            task["completion_summary"] = summary
            release_write_locks(task["task_id"], final_status="BLOCKED")
            save_agent_task(path, task)
            blocked += 1
        return blocked


def block_partial_cycle_workstreams(workstream_ids: list[str], failure: ValidationError) -> None:
    reason = "run_research_cycle.py failed before completing this workstream."
    attempted_strategy = "Automatic run_research_cycle.py orchestration."
    evidence = str(failure)
    next_action = (
        "Inspect the failed worker run, failed exploration note, and workstream artifacts. "
        "Create a revised workstream or restart the cycle after fixing the blocker."
    )
    for workstream_id in reversed(unique_workstream_ids(workstream_ids)):
        try:
            recover_collectable_tasks(workstream_id)
            workstream = project_workstream_path(workstream_id)
            status = load_status(workstream)
            if status.get("status") == "COMPLETE":
                continue
            if status.get("status") == "BLOCKED":
                if workstream_has_active_task(workstream_id):
                    print(
                        f"Not closing queued tasks for {workstream_id}: it still has an active worker task.",
                        file=sys.stderr,
                    )
                    continue
                blocked_tasks = block_unfinished_tasks(
                    workstream_id,
                    "Blocked by failed run_research_cycle.py cycle after the workstream entered BLOCKED state.",
                )
                if blocked_tasks:
                    print(f"Closed {blocked_tasks} unfinished task(s) in blocked workstream {workstream_id}.")
                continue
            if workstream_has_active_task(workstream_id):
                print(
                    f"Not auto-blocking {workstream_id}: it still has an active worker task. "
                    "Collect or recover that task first.",
                    file=sys.stderr,
                )
                continue
            failure_note = mark_status_blocked(
                workstream,
                status,
                reason,
                attempted_strategy,
                evidence,
                next_action,
                event_type="research_cycle_partial_failure_blocked",
            )
            blocked_tasks = block_unfinished_tasks(
                workstream_id,
                f"Blocked by failed run_research_cycle.py cycle; see {failure_note.relative_to(repo_root())}.",
            )
            print(
                f"Blocked partial workstream {workstream_id}; "
                f"blocked {blocked_tasks} unfinished task(s)."
            )
        except ValidationError as cleanup_exc:
            print(f"Failed to block partial workstream {workstream_id}: {cleanup_exc}", file=sys.stderr)


def source_context_text(paths: list[str]) -> str:
    chunks: list[str] = []
    for raw in paths:
        path = Path(raw)
        if path.is_absolute():
            full = path
        else:
            full = repo_root() / path
            if not full.exists():
                full = repo_root().parent / path
        if full.exists():
            chunks.append(full.read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(chunks)


def classify_computation_requirement(args: argparse.Namespace) -> tuple[bool, list[str]]:
    if args.force_computation:
        return True, ["forced by --force-computation"]
    if args.no_computation:
        return False, []
    text = args.objective
    if args.target_label:
        text += "\n" + "\n".join(args.target_label)
    if args.source_context:
        text += "\n" + source_context_text(args.source_context)
    return task_requires_computation(text)


def classify_literature_requirement(args: argparse.Namespace) -> tuple[bool, list[str]]:
    if args.force_literature:
        return True, ["forced by --force-literature"]
    if args.no_literature:
        return False, []
    text = args.objective
    if args.target_label:
        text += "\n" + "\n".join(args.target_label)
    if args.source_context:
        text += "\n" + "\n".join(args.source_context)
    return task_requires_literature(text)


def literature_artifact_inputs(literature_id: str) -> list[str]:
    return [
        f"workstreams/{literature_id}/report.md",
        f"workstreams/{literature_id}/artifacts/search_plan.json",
        f"workstreams/{literature_id}/artifacts/search_plan.md",
        f"workstreams/{literature_id}/artifacts/literature_search_results.json",
        f"workstreams/{literature_id}/artifacts/literature_search.md",
        f"workstreams/{literature_id}/artifacts/citation_graph.json",
        f"workstreams/{literature_id}/artifacts/citation_graph.md",
        f"workstreams/{literature_id}/artifacts/search_coverage_validation.json",
        f"workstreams/{literature_id}/artifacts/literature_sources/source_manifest.json",
        f"workstreams/{literature_id}/artifacts/extracted_theorem_statements.json",
        f"workstreams/{literature_id}/artifacts/extracted_theorem_statements.md",
        f"workstreams/{literature_id}/artifacts/sources.md",
        f"workstreams/{literature_id}/artifacts/theorem_statements.md",
        f"workstreams/{literature_id}/artifacts/theorem_applicability_matrix.json",
        f"workstreams/{literature_id}/artifacts/theorem_statement_verification.json",
        f"workstreams/{literature_id}/artifacts/theorem_applicability_validation.json",
        f"workstreams/{literature_id}/artifacts/literature_gaps.md",
    ]


def literature_objective(args: argparse.Namespace) -> str:
    search_queries = [args.objective]
    if args.target_label:
        search_queries.extend(args.target_label)
    if args.source_context:
        search_queries.extend(Path(item).stem for item in args.source_context)
    parts = [
        args.objective,
        "",
        "Run as an independent specialized literature review sub-agent delegated by the workstream.",
        "First run scripts/plan_literature_queries.py to create broad query families, "
        "including method, object/model, exact-statement, alternate-terminology, citation-key, "
        "and negative/boundary-case queries.",
        "Then run scripts/literature_search.py for multiple high-priority planned queries, "
        "normally with --pages 2 or higher for external providers, and inspect and summarize "
        "the returned sources. Preserve provider diagnostics, failed external calls, and "
        "rate-limit errors in the literature artifacts.",
        "After initial search results exist, run scripts/literature_expand_graph.py to expand "
        "Semantic Scholar references and citations for seed papers when available. Treat this "
        "as recall expansion, not theorem applicability evidence.",
        "After search and citation expansion, run scripts/validate_search_coverage.py "
        "and fix search/follow-up coverage until artifacts/search_coverage_validation.json "
        "has passed=true.",
        "For arXiv results, run scripts/arxiv_source_extract.py to download "
        "available arXiv sources, parse LaTeX, and extract theorem-like, custom "
        "newtheorem, assumption, definition, corollary, condition, remark, example, "
        "and labeled equation environments.",
        "When theorem-like statements are needed downstream, cite the extracted "
        "source_statement_id values in artifacts/theorem_statements.md. If no "
        "exact theorem statements are available, write the exact sentence "
        "'No exact theorem statements.' in artifacts/theorem_statements.md.",
        "Fill artifacts/theorem_applicability_matrix.json alongside the Markdown "
        "matrix. For every downstream-usable theorem, record source hypotheses, "
        "target requirements, matched/missing hypotheses, notation mapping, "
        "match_status, usable_downstream, caveats, and downstream_use.",
        "Before finishing, run scripts/verify_theorem_statements.py for this "
        "workstream and fix theorem_statements.md until "
        "artifacts/theorem_statement_verification.json has verified=true.",
        "Then run scripts/validate_theorem_applicability.py and fix "
        "the applicability matrix until artifacts/theorem_applicability_validation.json "
        "has passed=true.",
        "Identify relevant references, exact theorem statements, definitions, "
        "known methods, pitfalls, and missing literature evidence.",
        "Use only repo-local sources, user-provided files, and explicitly "
        "available literature/search tools. If a source cannot be accessed, "
        "mark the claim as not verified instead of guessing.",
        "Separate exact source statements from informal paraphrases, and preserve source notation whenever possible.",
        "Record failed searches, ambiguous references, and source gaps in artifacts/literature_gaps.md.",
    ]
    parts.append("Search queries: " + " | ".join(search_queries))
    if args.target_label:
        parts.append("Target labels: " + ", ".join(args.target_label))
    if args.source_context:
        parts.append("Relevant source-context files: " + ", ".join(args.source_context))
    return "\n".join(parts).strip()


def proof_objective(
    args: argparse.Namespace,
    computation_required: bool,
    computation_id: str | None,
    literature_id: str | None,
) -> str:
    parts = [
        args.objective,
        "",
        "Run as an independent proof worker. Write a source-grounded report "
        "with claim ids, evidence, uncertainty, and failed attempts.",
        "Do not edit paper source files directly; write proposed source edits "
        "as a unified diff under artifacts/source_patches/.",
    ]
    if args.target_label:
        parts.append("Target labels: " + ", ".join(args.target_label))
    if args.source_context:
        parts.append("Relevant source-context files: " + ", ".join(args.source_context))
    if literature_id:
        parts.append(
            f"Upstream literature review workstream: {literature_id}. Use its "
            "sources, exact theorem statements, and literature gaps as context, "
            "but independently check that any imported statement applies to "
            "this task."
        )
    if computation_required:
        parts.append(
            "This workstream has a linked computation gate. If you propose a deterministic equivalent, "
            "deterministic limit, decomposition, leading-order specialization, or lemma-statement change, "
            "state exactly what the computation worker must test. Do not make the linked computation "
            "workstream's pending/incomplete status itself a C-### claim; record pending gate status only "
            "under Uncertainty And Gaps or Next Steps."
        )
        if computation_id:
            parts.append(f"Linked computation workstream: {computation_id}.")
    return "\n".join(parts).strip()


def computation_objective(args: argparse.Namespace, proof_id: str) -> str:
    parts = [
        args.objective,
        "",
        f"Run as an independent computation worker linked to proof workstream {proof_id}.",
        "Implement reproducible code/tests for a computation sanity check of "
        "the mathematical formula, decomposition, deterministic equivalent, "
        "deterministic limit, or leading-order specialization under discussion.",
        "Use an independent baseline where possible, such as direct dense "
        "matrix construction/inversion or a second implementation route.",
        "Record normalization, parameter choices, seeds, residual behavior, inconclusive cases, and failed runs.",
        "Make tests fail if the numerical or symbolic evidence contradicts the claimed formula.",
    ]
    if args.target_label:
        parts.append("Target labels: " + ", ".join(args.target_label))
    if args.source_context:
        parts.append("Relevant source-context files: " + ", ".join(args.source_context))
    return "\n".join(parts).strip()


def reviewer_objective(workstream_id: str, extra: str = "") -> str:
    text = (
        f"Review workstream {workstream_id}. Check report claims, artifacts, code/test evidence when present, "
        "uncertainty statements, and source grounding. Approve or block every C-### claim explicitly."
    )
    if extra:
        text += "\n" + extra
    return text


def run_review_cycle(workstream_id: str, args: argparse.Namespace, review_extra: str = "") -> str:
    submit_for_review(workstream_id)
    reviewer_task = create_task("reviewer", workstream_id, reviewer_objective(workstream_id, review_extra))
    run_worker(reviewer_task, args)
    promote(workstream_id)
    return reviewer_task


def finalize_project_outputs(args: argparse.Namespace) -> None:
    if args.no_finalize_outputs or args.no_spawn or args.no_wait:
        return
    run_command([sys.executable, "scripts/update_claim_registry.py"])
    run_command([sys.executable, "scripts/synthesize_working_paper.py"])
    run_command([sys.executable, "scripts/check_working_paper.py"])


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a complete independent-worker research cycle with optional "
            "automatic literature and computation gates."
        )
    )
    parser.add_argument("--goal-id", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--objective", required=True)
    parser.add_argument("--target-label", action="append", default=[])
    parser.add_argument(
        "--source-context",
        action="append",
        default=[],
        help="Repo-relative file included for classification.",
    )
    parser.add_argument("--force-computation", action="store_true")
    parser.add_argument("--no-computation", action="store_true")
    parser.add_argument("--force-literature", action="store_true")
    parser.add_argument("--no-literature", action="store_true")
    parser.add_argument("--no-spawn", action="store_true", help="Create workstreams/tasks but do not launch workers.")
    parser.add_argument("--no-wait", action="store_true", help="Launch workers but do not wait/collect/promote.")
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--poll-interval", type=float, default=10.0)
    parser.add_argument("--model", default="")
    parser.add_argument(
        "--sandbox",
        default="workspace-write",
        choices=["read-only", "workspace-write", "danger-full-access"],
    )
    parser.add_argument(
        "--ask-for-approval",
        default="never",
        choices=["untrusted", "on-request", "on-failure", "never"],
    )
    parser.add_argument("--no-finalize-outputs", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.force_computation and args.no_computation:
        print("--force-computation and --no-computation cannot be used together")
        return 2
    if args.force_literature and args.no_literature:
        print("--force-literature and --no-literature cannot be used together")
        return 2

    literature_required = False
    literature_triggers: list[str] = []
    computation_required = False
    computation_triggers: list[str] = []
    literature_id: str | None = None
    literature_task: str | None = None
    computation_id: str | None = None
    computation_task: str | None = None
    proof_id: str | None = None
    proof_task: str | None = None
    created_workstreams: list[str] = []

    try:
        guard_errors = source_guard_errors()
        if guard_errors:
            raise ValidationError(
                "Refusing to start research cycle while guarded paper source is dirty:\n"
                + "\n".join(f"- {error}" for error in guard_errors)
            )

        literature_required, literature_triggers = classify_literature_requirement(args)
        computation_required, computation_triggers = classify_computation_requirement(args)

        if literature_required:
            literature_id = create_workstream(
                args.goal_id,
                "literature",
                f"{args.title} Literature Review",
            )
            created_workstreams.append(literature_id)
            update_status_metadata(literature_id, {
                "literature_required": True,
                "theorem_statement_verification_required": True,
                "requires_independent_workers": True,
                "patch_target_labels": args.target_label,
            })

        if computation_required:
            computation_id = create_workstream(
                args.goal_id,
                "computation",
                f"{args.title} Computation Verification",
            )
            created_workstreams.append(computation_id)
            update_status_metadata(computation_id, {
                "computation_required": True,
                "requires_independent_workers": True,
                "patch_target_labels": args.target_label,
            })

        proof_id = create_workstream(
            args.goal_id,
            "proof",
            args.title,
            requires_computation_gate=computation_required,
            linked_computation_workstream_id=computation_id,
            computation_gate_reason=(
                "Automatic math-task classifier required computation verification. "
                "Trigger keywords: "
                f"{', '.join(computation_triggers) if computation_triggers else 'none'}."
            ) if computation_required else None,
        )
        created_workstreams.append(proof_id)
        update_status_metadata(proof_id, {
            "computation_required": computation_required,
            "requires_independent_workers": True,
            "patch_target_labels": args.target_label,
            "no_computation_waiver": (
                "User or coordinator invoked run_research_cycle.py with --no-computation."
                if args.no_computation else None
            ),
        })

        if literature_id:
            start_workstream(literature_id)
        if computation_id:
            start_workstream(computation_id)

        if literature_id:
            literature_task = create_task(
                "literature",
                literature_id,
                literature_objective(args),
                notes="Created by run_research_cycle.py as an upstream specialized literature review sub-agent.",
            )
            run_worker(literature_task, args)
            if not args.no_spawn and not args.no_wait:
                run_review_cycle(
                    literature_id,
                    args,
                    "For this literature workstream, approve only "
                    "source-grounded claims with exact references or explicit "
                    "not-verified caveats. Confirm that artifacts/search_coverage_validation.json "
                    "has passed=true, artifacts/theorem_statement_verification.json has "
                    "verified=true, and artifacts/theorem_applicability_validation.json has "
                    "passed=true before approving exact theorem-statement or applicability claims.",
                )

        start_workstream(proof_id)

        proof_task = create_task(
            "proof",
            proof_id,
            proof_objective(args, computation_required, computation_id, literature_id),
            notes="Created by run_research_cycle.py; coordinator must not do proof work locally.",
        )
        if literature_id:
            append_task_inputs(proof_task, literature_artifact_inputs(literature_id))
        run_worker(proof_task, args)

        if computation_required and computation_id:
            computation_task = create_task(
                "computation",
                computation_id,
                computation_objective(args, proof_id),
                notes="Created by run_research_cycle.py as automatic linked computation verification.",
            )
            computation_inputs = [
                f"workstreams/{proof_id}/report.md",
                f"workstreams/{proof_id}/status.json",
            ]
            if literature_id:
                computation_inputs.extend(literature_artifact_inputs(literature_id))
            append_task_inputs(computation_task, computation_inputs)
            run_worker(computation_task, args)

            if not args.no_spawn and not args.no_wait:
                run_review_cycle(
                    computation_id,
                    args,
                    "For this computation workstream, set tests_reviewed=true "
                    "and golden_values_approved=true only if the tests and "
                    "recorded values are valid.",
                )

        if not args.no_spawn and not args.no_wait:
            run_review_cycle(
                proof_id,
                args,
                "If this proof workstream has a linked computation gate, verify "
                "that the computation workstream is COMPLETE and "
                "reviewer-approved before approving proof claims.",
            )
            finalize_project_outputs(args)

    except ValidationError as exc:
        print(exc)
        block_partial_cycle_workstreams(created_workstreams, exc)
        return 1

    print("Research cycle prepared.")
    if literature_id:
        print(f"Literature workstream: {literature_id}")
    if literature_task:
        print(f"Literature task: {literature_task}")
    print(f"Proof workstream: {proof_id}")
    print(f"Proof task: {proof_task}")
    if computation_id:
        print(f"Computation workstream: {computation_id}")
    if computation_task:
        print(f"Computation task: {computation_task}")
    if literature_triggers:
        print("Literature triggers: " + ", ".join(literature_triggers))
    if computation_triggers:
        print("Computation triggers: " + ", ".join(computation_triggers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
