#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from lib import (
    DEFAULT_TEST_TIMEOUT_SECONDS,
    ValidationError,
    add_project_workstream,
    append_message,
    load_project_state,
    project_state_path,
    repo_root,
    safe_id,
    scaffold_lock,
    validate_goal_approval_gate,
    write_json,
)


WORKSTREAM_TYPES = {"literature", "proof", "computation", "synthesis", "general"}


def next_workstream_number(project_state: dict) -> int:
    highest = 0
    for item in project_state.get("workstreams", []):
        if not isinstance(item, dict):
            continue
        workstream_id = item.get("id", "")
        parts = workstream_id.split("_")
        if len(parts) >= 2 and parts[0] == "ws" and parts[1].isdigit():
            highest = max(highest, int(parts[1]))
    return highest + 1


def goal_status(project_state: dict, goal_id: str) -> str | None:
    for item in project_state.get("goals", []):
        if isinstance(item, dict) and item.get("id") == goal_id:
            status = item.get("status")
            return status if isinstance(status, str) else None
    return None


def status_template(
    workstream_id: str,
    goal_id: str,
    workstream_type: str,
    requires_computation_gate: bool = False,
    linked_computation_workstream_id: str | None = None,
    computation_gate_reason: str | None = None,
) -> dict:
    is_computation = workstream_type == "computation"
    is_literature = workstream_type == "literature"
    return {
        "id": workstream_id,
        "goal_id": goal_id,
        "type": workstream_type,
        "status": "DRAFT",
        "blocked_reason": None,
        "tests_required": is_computation,
        "tests_passed": False if is_computation else None,
        "test_timeout_seconds": DEFAULT_TEST_TIMEOUT_SECONDS if is_computation else None,
        "review_required": True,
        "review_passed": False,
        "golden_values_approved": False if is_computation else None,
        "requires_computation_gate": requires_computation_gate,
        "linked_computation_workstream_id": linked_computation_workstream_id,
        "computation_gate_reason": computation_gate_reason,
        "requires_independent_workers": True,
        "literature_required": is_literature,
        "theorem_statement_verification_required": is_literature,
        "computation_required": requires_computation_gate,
        "source_patch_proposed": False,
        "source_patch_applied": False,
        "patch_target_labels": [],
        "no_computation_waiver": None,
        "current_review_round": 0,
        "max_review_rounds": 3,
        "finalized": False,
    }


def report_template(title: str, goal_id: str) -> str:
    return f"""# {title}

## Summary

TBD.

## Inputs

- `../../research_question.md`
- `../../goals/{goal_id}.md`

## Actions Taken

TBD.

## Claims

TBD. Use claim ids, for example:

- C-001: Precise claim text.

If there are no substantive claims, write `No substantive claims.`

## Evidence And Artifacts

TBD.

## Error Decomposition

- Source-setting error: TBD.
- finite-\\(n\\) / Monte Carlo error: TBD.
- Numerical quadrature / branch error: TBD.
- Theorem-level discrepancy: TBD.

## Uncertainty And Gaps

TBD.

## Failed Attempts

TBD.

## Next Steps

TBD.
"""


def instructions_template(title: str, goal_id: str, workstream_type: str) -> str:
    return f"""# {title}

## Assigned Goal

`goals/{goal_id}.md`

## Type

`{workstream_type}`

## Mission

TBD.

## Required Outputs

- `report.md`
- supporting artifacts under `artifacts/`
- process notes under `logs/`
- reviewer JSON files under `review/`

## Completion Gate

Use `scripts/submit_for_review.py` to enter review and `scripts/promote_workstream.py` to complete. Do not set `COMPLETE` manually.
If this proof workstream proposes or patches a leading-order paper-source formula, enable a linked computation gate with `scripts/set_computation_gate.py` before promotion.
"""


def review_template(workstream_type: str) -> dict:
    is_computation = workstream_type == "computation"
    return {
        "reviewer_id": "reviewer_001",
        "round": 1,
        "decision": "REQUEST_CHANGES",
        "summary": "",
        "required_changes": [],
        "approved_claims": [],
        "blocked_claims": [],
        "golden_values_approved": False if is_computation else None,
        "tests_reviewed": False if is_computation else None,
        "checklist": {
            "source_setting": {
                "status": "FAIL",
                "evidence": "",
            },
            "tests": {
                "status": "FAIL",
                "evidence": "",
            },
            "formula_trace": {
                "status": "FAIL",
                "evidence": "",
            },
            "raw_object_validation": {
                "status": "FAIL",
                "evidence": "",
            },
            "error_decomposition": {
                "status": "FAIL",
                "evidence": "",
            },
        },
    }


def source_setting_manifest_template() -> dict:
    return {
        "schema_version": 1,
        "manifest_status": "DRAFT",
        "source_locations": [
            "TBD: repo-relative source files, labels, theorem names, or workstream artifacts used to define the simulation setting"
        ],
        "dimension_scaling": {
            "n": "TBD: sample size definition and tested values",
            "d": "TBD: ambient/input dimension definition and relation to n",
            "p": "TBD: feature dimension definition and relation to n",
            "q": "TBD: q definition, e.g. p/n, and tested regimes",
            "phi": "TBD: phi definition, e.g. d/n",
            "psi": "TBD: psi definition, e.g. d/p",
            "relationships": [
                "TBD: explicit asymptotic relationships such as p asymp n, d=phi n, p=q n"
            ],
        },
        "random_objects": {
            "F0": {
                "construction": "TBD: formula or explicit not used statement",
                "shape": "TBD",
                "role": "TBD",
            },
            "F1": {
                "construction": "TBD: formula or explicit not used statement",
                "shape": "TBD",
                "role": "TBD",
            },
            "beta": {
                "construction": "TBD: e.g. beta=n^{-1}X^T y or explicit not used statement",
                "shape": "TBD",
                "role": "TBD",
            },
            "y": {
                "construction": "TBD: training response construction or explicit not used statement",
                "shape": "TBD",
                "role": "TBD",
            },
            "y_tilde": {
                "construction": "TBD: second-half/test response construction or explicit not used statement",
                "shape": "TBD",
                "role": "TBD",
            },
        },
        "hermite_convention": {
            "family": "TBD: probabilists, normalized, or not used",
            "H2": "TBD: e.g. H_2(x)=x^2-1 or not used",
            "c_star_sq": "TBD: e.g. sum_{k>=1} k! c_{star,k}^2 or not used",
            "coefficient_norm": "TBD: coefficient normalization convention or not used",
        },
        "normalization": {
            "matrix_scale": "TBD: one of FFt_over_n, FFt_over_np, ridge_form, or other with explanation",
            "ridge_form": "TBD: e.g. FF^T + lambda n I, FF^T/n + lambda I, or not used",
            "notes": "TBD: residual/loss/trace normalization details",
        },
        "compared_objects": [
            {
                "name": "TBD",
                "object_type": "raw_finite_object",
                "formula_or_description": "TBD",
                "normalization": "TBD",
            },
            {
                "name": "TBD",
                "object_type": "deterministic_equivalent",
                "formula_or_description": "TBD",
                "normalization": "TBD",
            },
        ],
        "source_faithfulness_checks": [
            "TBD: shape/scaling/source-convention checks the tests or script must enforce"
        ],
        "notes": "TBD: any intentional surrogate, approximation, or deviation from the paper source setting",
    }


def source_faithfulness_tests_template() -> dict:
    return {
        "schema_version": 1,
        "overall_status": "PENDING",
        "checks": [
            {
                "check_id": "shapes_and_scaling",
                "status": "PENDING",
                "evidence": "TBD: verify shapes and scaling for n,d,p,q,phi,psi and all simulated matrices/vectors",
            },
            {
                "check_id": "beta_is_noisy_estimator",
                "status": "PENDING",
                "evidence": "TBD: verify beta = n^{-1} X^T y is used and beta_star is not substituted for beta",
            },
            {
                "check_id": "hermite_factorial_convention",
                "status": "PENDING",
                "evidence": "TBD: verify c_star^2 = sum_k k! c_{star,k}^2 and the Hermite family used in simulation",
            },
            {
                "check_id": "raw_direct_solve_vs_spectral",
                "status": "PENDING",
                "max_error": None,
                "tolerance": 1.0e-10,
                "evidence": "TBD: verify raw finite quantity by both direct solve and spectral identity",
            },
            {
                "check_id": "decomposition_identity",
                "status": "PENDING",
                "max_error": None,
                "tolerance": 1.0e-10,
                "evidence": "TBD: verify decomposition identity error is at most 1e-10",
            },
        ],
        "notes": "Set overall_status to PASS only after every required check is PASS.",
    }


def formula_trace_template() -> dict:
    return {
        "schema_version": 1,
        "overall_status": "PENDING",
        "not_applicable_reason": "",
        "formulas": [
            {
                "formula_id": "TBD",
                "formula_type": "deterministic_equivalent",
                "source_label": "TBD: source theorem/lemma/corollary/equation label",
                "source_location": "TBD: repo-relative file path and nearby section/line if available",
                "theorem_or_result_name": "TBD",
                "implementation_path": "TBD: repo-relative code path",
                "code_function": "TBD: function or method implementing the formula",
                "independent_sanity_tests": [
                    {
                        "test_name": "TBD",
                        "test_file": "TBD: repo-relative test path",
                        "status": "PENDING",
                        "evidence": "TBD: what independent sanity check was run"
                    }
                ],
                "notes": "TBD"
            }
        ],
        "notes": "If this computation implements no theorem/DE/contour formula, set overall_status=NOT_APPLICABLE, provide not_applicable_reason, and set formulas=[].",
    }


def raw_object_validation_template() -> dict:
    return {
        "schema_version": 1,
        "overall_status": "PENDING",
        "not_applicable_reason": "",
        "validations": [
            {
                "object_name": "TBD: must exactly match a raw_finite_object name in source_setting_manifest.json",
                "status": "PENDING",
                "source_label": "TBD: source theorem/lemma/equation label or explicit not labeled",
                "primary_implementation": "TBD: e.g. spectral identity, direct solve, direct matrix multiplication",
                "independent_implementation": "TBD: distinct implementation used as a check",
                "max_error": None,
                "tolerance": 1.0e-10,
                "evidence": "TBD: where the comparison is implemented and what grid/cases passed",
                "notes": "TBD"
            }
        ],
        "notes": "Every compared object with object_type=raw_finite_object in source_setting_manifest.json must have a PASS validation here. If there are no raw finite objects, set overall_status=NOT_APPLICABLE, provide not_applicable_reason, and set validations=[].",
    }


def create_type_artifacts(workstream_path: Path, workstream_type: str) -> None:
    artifacts = workstream_path / "artifacts"
    artifacts.mkdir(parents=True, exist_ok=False)
    if workstream_type == "literature":
        (artifacts / "literature_sources").mkdir()
        (artifacts / "literature_sources" / "source_manifest.json").write_text(
            json.dumps({"sources": []}, indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts / "search_plan.md").write_text(
            "# Search Plan\n\n"
            "## Search Queries\n\n"
            "TBD. List multiple English search queries, one per bullet, with a short reason.\n\n"
            "## Rationale\n\n"
            "TBD. Explain query families, expected technique clusters, and why these terms match the assigned problem.\n\n"
            "## Search Coverage\n\n"
            "TBD. Record which providers or repo-local sources were searched and which searches failed or were unavailable.\n",
            encoding="utf-8",
        )
        (artifacts / "literature_search_results.json").write_text(
            json.dumps({
                "schema_version": 1,
                "generated_at": None,
                "queries": [],
                "providers": [],
                "provider_runs": [],
                "merged_results": [],
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts / "literature_search.md").write_text("# Literature Search\n\nTBD.\n", encoding="utf-8")
        (artifacts / "followup_queries.md").write_text(
            "# Follow-up Queries\n\n"
            "## Follow-up Queries\n\n"
            "TBD. Record initial result, follow-up query, reason, provider/source, and outcome.\n\n"
            "## Effect On Conclusions\n\n"
            "TBD. State whether each follow-up changed the literature map, theorem applicability, or gaps.\n",
            encoding="utf-8",
        )
        (artifacts / "extracted_theorem_statements.json").write_text(
            json.dumps({
                "schema_version": 1,
                "generated_at": None,
                "source_manifest": [],
                "statements": [],
                "failures": [],
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts / "extracted_theorem_statements.md").write_text(
            "# Extracted Theorem Statements\n\nTBD.\n",
            encoding="utf-8",
        )
        (artifacts / "sources.md").write_text("# Sources\n\nTBD.\n", encoding="utf-8")
        (artifacts / "theorem_statements.md").write_text("# Theorem Statements\n\nTBD.\n", encoding="utf-8")
        (artifacts / "theorem_applicability_matrix.md").write_text(
            "# Theorem Applicability Matrix\n\n"
            "## Applicability Matrix\n\n"
            "TBD. For each exact theorem, lemma, or proposition used, compare source_statement_id, source hypotheses, target paper setting, match status, and caveats.\n\n"
            "## Non-Matches Or Caveats\n\n"
            "TBD. Record mismatched hypotheses and statements that should not be imported.\n",
            encoding="utf-8",
        )
        (artifacts / "theorem_statement_verification.json").write_text(
            json.dumps({
                "schema_version": 1,
                "generated_at": None,
                "verified": False,
                "errors": ["Theorem statements have not been verified yet."],
                "warnings": [],
                "no_exact_theorem_statements": False,
                "available_statement_count": 0,
                "cited_statement_ids": [],
                "source_statement_id_lines": [],
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts / "literature_gaps.md").write_text("# Literature Gaps\n\nTBD.\n", encoding="utf-8")
    elif workstream_type == "proof":
        (artifacts / "proof_sketches.md").write_text("# Proof Sketches\n\nTBD.\n", encoding="utf-8")
        (artifacts / "gap_list.md").write_text("# Gap List\n\nTBD.\n", encoding="utf-8")
        (artifacts / "counterexamples.md").write_text("# Counterexamples And Obstructions\n\nTBD.\n", encoding="utf-8")
    elif workstream_type == "computation":
        (artifacts / "results").mkdir()
        (artifacts / "results" / "README.md").write_text("# Results\n\nTBD.\n", encoding="utf-8")
        (artifacts / "source_setting_manifest.json").write_text(
            json.dumps(source_setting_manifest_template(), indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts / "source_faithfulness_tests.json").write_text(
            json.dumps(source_faithfulness_tests_template(), indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts / "formula_trace.json").write_text(
            json.dumps(formula_trace_template(), indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts / "raw_object_validation.json").write_text(
            json.dumps(raw_object_validation_template(), indent=2) + "\n",
            encoding="utf-8",
        )
        (artifacts / "test_run.json").write_text(
            json.dumps({
                "tests_required": True,
                "passed": False,
                "reason": "Tests have not been run yet.",
            }, indent=2) + "\n",
            encoding="utf-8",
        )
        (workstream_path / "src").mkdir()
        (workstream_path / "src" / "README.md").write_text("# Source\n\nTBD.\n", encoding="utf-8")
        (workstream_path / "tests").mkdir()
        (workstream_path / "tests" / "README.md").write_text("# Tests\n\nTBD.\n", encoding="utf-8")
    else:
        (artifacts / "README.md").write_text("# Artifacts\n\nTBD.\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a new workstream scaffold.")
    parser.add_argument("--goal-id", required=True, help="Existing goal id, for example goal_001.")
    parser.add_argument("--type", required=True, choices=sorted(WORKSTREAM_TYPES))
    parser.add_argument("--title", required=True, help="Human-readable workstream title.")
    parser.add_argument("--id", help="Optional explicit workstream id. Defaults to ws_<next>_<title>.")
    parser.add_argument(
        "--requires-computation-gate",
        action="store_true",
        help="For proof workstreams that propose or patch leading-order source formulas.",
    )
    parser.add_argument(
        "--linked-computation-workstream-id",
        help="Optional computation workstream id that must complete before this proof workstream can promote.",
    )
    parser.add_argument(
        "--computation-gate-reason",
        help="Short reason for requiring the computation gate.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = repo_root()
    workstream_path: Path | None = None
    created_workstream_path = False
    registered_in_project_state = False

    try:
        with scaffold_lock():
            project_state = load_project_state()
            validate_goal_approval_gate(project_state, project_state_path())
            status = goal_status(project_state, args.goal_id)
            if status is None:
                raise ValidationError(f"Goal {args.goal_id!r} not found in state/project_state.json")
            if status != "APPROVED":
                raise ValidationError(
                    f"Goal {args.goal_id!r} is {status!r}; workstreams can only be created for APPROVED goals. "
                    "Use scripts/approve_goal.py after explicit user confirmation."
                )
            if args.requires_computation_gate and args.type != "proof":
                raise ValidationError("--requires-computation-gate is only valid for proof workstreams")
            if args.linked_computation_workstream_id and not args.requires_computation_gate:
                raise ValidationError("--linked-computation-workstream-id requires --requires-computation-gate")

            if args.id:
                workstream_id = safe_id(args.id)
            else:
                number = next_workstream_number(project_state)
                workstream_id = f"ws_{number:03d}_{safe_id(args.title)}"

            rel_path = f"workstreams/{workstream_id}"
            workstream_path = root / rel_path
            if workstream_path.exists():
                raise ValidationError(f"Workstream path already exists: {workstream_path}")

            workstream_path.mkdir(parents=True)
            created_workstream_path = True
            (workstream_path / "logs").mkdir()
            (workstream_path / "review").mkdir()
            (workstream_path / "subagent_requests").mkdir()
            create_type_artifacts(workstream_path, args.type)

            title = f"Workstream {workstream_id}: {args.title}"
            (workstream_path / "instructions.md").write_text(
                instructions_template(title, args.goal_id, args.type),
                encoding="utf-8",
            )
            (workstream_path / "report.md").write_text(
                report_template(title, args.goal_id),
                encoding="utf-8",
            )
            (workstream_path / "logs" / "README.md").write_text(
                "# Logs\n\nRecord workstream updates, failed attempts, and command notes here.\n",
                encoding="utf-8",
            )
            write_json(
                workstream_path / "status.json",
                status_template(
                    workstream_id,
                    args.goal_id,
                    args.type,
                    requires_computation_gate=args.requires_computation_gate,
                    linked_computation_workstream_id=args.linked_computation_workstream_id,
                    computation_gate_reason=args.computation_gate_reason,
                ),
            )
            write_json(workstream_path / "review" / "review_result.template.json", review_template(args.type))
            (workstream_path / "subagent_requests" / "README.md").write_text(
                "# Sub-agent Requests\n\n"
                "Workstream coordinators can request specialized sub-agents here. "
                "The project coordinator approves requests and creates Codex worker tasks.\n",
                encoding="utf-8",
            )
            add_project_workstream(workstream_id, rel_path, "DRAFT")
            registered_in_project_state = True
            append_message(
                "workstream_created",
                workstream_id,
                f"Created {workstream_id} for {args.goal_id}.",
                path=rel_path,
                workstream_type=args.type,
            )
    except Exception as exc:
        if created_workstream_path and not registered_in_project_state and workstream_path is not None and workstream_path.exists():
            try:
                shutil.rmtree(workstream_path)
            except OSError as rollback_exc:
                print(f"Failed to roll back partially created workstream {workstream_path}: {rollback_exc}")
        print(exc)
        return 1

    print(f"Created {rel_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
