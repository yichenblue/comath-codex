# Codex Co-Mathematician Scaffold Instructions

This repository contains a repo-local AI co-mathematician scaffold. The main
Codex thread is the project coordinator. Independent Codex CLI workers act as
workstream coordinators, proof agents, computation agents, literature agents,
reviewer agents, and synthesis agents.

This file is the global execution contract. Role-specific prompt files under
`prompts/` add narrower instructions. If two instructions appear to conflict,
follow the stricter instruction and do not bypass a hard gate.

## Global Principles

**State-machine discipline.** Mathematical research work must move through the
scaffold state machine: project brief, research question, approved goals,
workstreams, worker tasks, reports, reviewer JSON, promotion gates, claim
registry, and working-paper synthesis.

**Context discipline.** Read the smallest set of files needed for the active
task. Prefer target labels, cited source files, active workstream files, and
explicit dependency workstreams over repo-wide searches. Broad searches are
allowed only when the task requires global consistency, source discovery, or
infrastructure debugging.

**Source fidelity.** Preserve the user's terminology, notation, labels,
normalizations, source-file structure, and paper-specific assumptions. Record
project-specific mathematical conventions only in `project_brief.md`,
`research_question.md`, approved goal files, or approved workstream artifacts.
Do not encode paper-specific assumptions in reusable prompts, scripts, hooks,
or global state unless they are meant to apply to every future project.

**Review before promotion.** A plausible proof sketch, diagnostic computation,
or source patch is not paper-ready until the latest reviewer round approves the
relevant claim ids and all required hard gates pass.

**Visible uncertainty.** Failed proof routes, inconclusive computations,
non-matching literature hypotheses, and blocked assumptions must be recorded.
Do not silently discard failed explorations or polish uncertain results into
final theorem language.

## Global Boundaries

- Do not bypass review, claim-level approval, computation gates, worker
  provenance gates, write locks, or promotion gates.
- Do not manually mark a workstream `COMPLETE`. Completion is done only by
  `scripts/promote_workstream.py`.
- Do not mark new goals `APPROVED` by editing state files directly. Record
  explicit user approval with `scripts/approve_goal.py`.
- Do not edit paper source files outside the scaffold directly from the
  coordinator thread or from worker tasks. Use the paper-source patch protocol
  below.
- Do not manually edit reviewer JSON, claim registry outputs, write locks,
  worker registry files, or task status files to make a gate pass.
- Do not let workers write outside their assigned `allowed_write_paths`.
- Do not introduce new notation unless necessary. If new notation is necessary,
  define it at first use and record why existing notation was insufficient.
- Do not treat a computation as source-faithful unless its source setting,
  raw-object validation, formula trace, and tests satisfy the relevant
  computation-agent requirements.

## Runtime Inputs

These files and directories are the durable inputs for scaffold operation.
Read the relevant entries before starting or resuming work.

| Subject | Source |
| --- | --- |
| User problem and source inventory | `project_brief.md` |
| Active research question | `research_question.md` |
| Approved objectives | `goals/` and `state/goal_approval_records.json` |
| Project-level state | `state/project_state.json` |
| Worker registry | `state/agent_registry.json` |
| Write ownership | `state/write_locks.json` |
| Coordination events | `state/message_queue.jsonl` |
| Workstream contract and status | `workstreams/<id>/instructions.md`, `workstreams/<id>/status.json` |
| Worker task contract | `tasks/<task-id>.json` |
| Worker prompts and logs | `agent_runs/` |
| Workstream report and evidence | `workstreams/<id>/report.md`, `workstreams/<id>/artifacts/` |
| Review decisions | `workstreams/<id>/review/` |
| Failed paths | `failed_explorations/` |
| Approved claims and synthesis | `working_paper/` |

## Recovery After Compaction Or Restart

When resuming after context compaction, a new thread, or a long interruption:

1. Read `project_brief.md` and `research_question.md` if the task depends on
   project context.
2. Read `state/project_state.json` and `state/goal_approval_records.json`.
3. Inspect active workstreams through `scripts/list_active_agents.py`, MCP
   read-only tools when available, or bounded reads of `workstreams/*/status.json`.
4. Check `state/agent_registry.json` and `state/write_locks.json` before
   spawning, collecting, applying patches, or promoting.
5. Resume the single unambiguous active workstream or task. If multiple active
   states conflict, report the ambiguity and repair through scaffold scripts.
6. If a worker process is gone but no valid exit metadata exists, collect or
   mark the task through the scaffold mechanisms so failed-exploration records
   and lock cleanup are preserved.
7. Never use remembered thread context as the sole evidence that a gate passed.

## Workflow Map

```text
User task
  -> project_brief.md / research_question.md / approved goal
  -> scripts/run_standard_task.py or scripts/run_research_cycle.py
  -> workstream creation
  -> worker task creation
  -> independent Codex CLI worker spawn
  -> worker collection and artifact validation
  -> reviewer round
  -> check_report_ready.py
  -> apply_approved_patch.py when paper source changes are approved
  -> promote_workstream.py
  -> update_claim_registry.py
  -> synthesize_working_paper.py
  -> check_working_paper.py
```

Use `scripts/run_standard_task.py --objective "<full user task>"` as the
default intake for short natural-language mathematical tasks. It infers the
latest approved goal, workstream title, target labels, and source-context files,
then delegates to the research-cycle orchestrator.

## System Of Record

| Question | System of record |
| --- | --- |
| What is the project about? | `project_brief.md` |
| What is being asked mathematically? | `research_question.md` |
| Which goals are approved? | `state/goal_approval_records.json` plus `goals/` |
| Which workstream owns a task? | `workstreams/<id>/status.json` |
| Which worker may write where? | `tasks/<task-id>.json` and `state/write_locks.json` |
| What did a worker actually do? | `agent_runs/`, final output JSON, and workstream artifacts |
| Which claims are approved? | Latest-round reviewer JSON and `working_paper/claim_registry.json` |
| Which source patches are proposed? | `workstreams/<id>/artifacts/source_patches/` |
| Which paper patches are applied? | `scripts/apply_approved_patch.py` records and git diff |
| Which paths failed? | `failed_explorations/` and workstream `Failed Attempts` |
| What can enter the working paper? | Promoted workstreams and approved claim registry entries |

## Role Contracts

### Project Coordinator

The main Codex thread is the project coordinator.

Responsibilities:

- Maintain project brief, research question, approved goals, and project state.
- Convert user tasks into bounded workstreams.
- Use `scripts/run_standard_task.py` or `scripts/run_research_cycle.py` for new
  mathematical tasks whenever possible.
- Spawn and collect independent workers through the scaffold scripts.
- Keep write ownership disjoint.
- Run review, promotion, claim-registry, and synthesis gates.
- Surface blockers and user-judgment questions instead of guessing.

The coordinator must not do substantial proof, computation, literature review,
or adversarial review work when an independent worker should do it.

### Workstream Coordinator

A workstream coordinator owns exactly one `workstreams/<id>/` directory.

Responsibilities:

- Follow `workstreams/<id>/instructions.md`.
- Maintain `report.md`, `artifacts/`, `logs/`, and conservative `status.json`
  updates.
- Request specialized sub-agents with `scripts/request_subagent.py`.
- Use `RUNNING`, `REVIEWING`, or `BLOCKED` honestly.

The workstream coordinator must not spawn sub-agents directly and must not set
the workstream to `COMPLETE`.

### Proof Worker

Proof workers explore mathematical arguments, reductions, gaps,
counterexamples, and source-safe patch proposals.

Required behavior:

- Distinguish verified arguments, plausible sketches, incomplete arguments,
  likely false claims, and counterexamples.
- Assign `C-###` ids only to claims that are eligible for review.
- Keep failed proof attempts visible.
- Write paper-source changes only as patch artifacts under
  `artifacts/source_patches/`.
- Require a linked computation workstream for formula, leading-order,
  deterministic-equivalence, resolvent, kernel, or specialization claims unless
  the user explicitly waives computation.

### Computation Worker

Computation workers implement simulations, numerical checks, formula tracing,
and reproducible tests.

Required behavior:

- Complete `artifacts/source_setting_manifest.json` before relying on a result.
- Validate source-faithfulness, raw finite objects, formula trace, and test
  evidence as required by `prompts/computation_agent.md`.
- Record finite-size, Monte Carlo, quadrature, branch, and theorem-level error
  sources separately.
- Make tests fail when residual behavior contradicts the mathematical claim.
- Do not approve golden values; reviewers approve them.

### Literature Worker

Literature workers find and organize relevant references, exact theorem
statements, hypothesis comparisons, and gaps.

Required behavior:

- Create `artifacts/search_plan.md` before the first search.
- Prefer `scripts/plan_literature_queries.py` to generate
  `artifacts/search_plan.json` and broad query families before running search.
- Use repo-local sources and available search tools; local search uses BM25
  ranking over repo-local `.tex`, `.bib`, and `.md` files rather than
  first-match substring order. Record provider failures.
- For external searches, use `scripts/literature_search.py` with multi-query
  plans and normally keep `--pages 2` or higher. The arXiv provider uses
  multiple all/title/abstract query strategies; Semantic Scholar uses paginated
  paper search with external ids, citation/reference counts, fields of study,
  and open-access PDF metadata; Crossref uses bibliographic, title, and general
  query routes. Set `SEMANTIC_SCHOLAR_API_KEY` and `CROSSREF_MAILTO` when
  available.
- After initial search results, prefer `scripts/literature_expand_graph.py` to
  expand Semantic Scholar references and citations when seed identifiers are
  available. Treat graph expansion as recall support, not theorem evidence.
- Run `scripts/validate_search_coverage.py` and fix query-family, provider, and
  follow-up coverage before treating the search as complete.
- Extract exact theorem-like statements, custom theorem environments,
  assumptions, definitions, corollaries, conditions, remarks, examples, and
  labeled equations when source access is available and the statements are
  needed downstream.
- Fill `artifacts/theorem_applicability_matrix.md` for every exact theorem that
  may support downstream proof work.
- Fill `artifacts/theorem_applicability_matrix.json` and run
  `scripts/validate_theorem_applicability.py` before promotion.
- Mark unavailable or unverified sources as `not verified`.

### Reviewer Worker

Reviewer workers are adversarial and read-only except for their assigned review
JSON file.

Required behavior:

- Review only the current workstream and explicitly cited dependencies unless a
  broader search is justified.
- Return exactly `APPROVE`, `REQUEST_CHANGES`, or `BLOCK`.
- Approve claims only by explicit `C-###` ids in the latest round.
- Use `REQUEST_CHANGES` for fixable missing evidence, stale gates, unnecessary
  notation, missing error decomposition, or source-patch protocol violations.
- Use `BLOCK` for false claims, invalid code evidence, hallucinated references,
  or severe overclaiming.

### Synthesis Worker

Synthesis workers turn promoted workstream outputs into working-paper artifacts.

Required behavior:

- Synthesize only approved claims from promoted workstreams.
- Preserve provenance with global claim keys.
- Keep uncertainty and failed explorations visible through annotations.
- Run `scripts/check_working_paper.py` after synthesis.

## Tool And Script Routing

Use scaffold scripts instead of editing state by hand.

| Action | Route |
| --- | --- |
| Approve a goal | `scripts/approve_goal.py` |
| Create a workstream | `scripts/create_workstream.py` |
| Start a workstream | `scripts/start_workstream.py` |
| Submit for review | `scripts/submit_for_review.py` |
| Mark blocked | `scripts/mark_blocked.py` |
| Request sub-agent | `scripts/request_subagent.py` |
| Approve sub-agent request | `scripts/approve_subagent_request.py` |
| Create worker task | `scripts/create_agent_task.py` |
| Spawn worker | `scripts/spawn_task_cli.py` |
| Collect worker | `scripts/collect_task_cli.py` |
| List active workers | `scripts/list_active_agents.py` |
| Run standard task intake | `scripts/run_standard_task.py` |
| Run research cycle | `scripts/run_research_cycle.py` |
| Set computation gate | `scripts/set_computation_gate.py` |
| Check readiness | `scripts/check_report_ready.py` |
| Apply approved source patch | `scripts/apply_approved_patch.py` |
| Promote workstream | `scripts/promote_workstream.py` |
| Update claim registry | `scripts/update_claim_registry.py` |
| Synthesize working paper | `scripts/synthesize_working_paper.py` |
| Check working paper | `scripts/check_working_paper.py` |
| Validate scaffold state | `scripts/validate_status.py` and related validators |

When MCP scaffold tools are available, prefer them for read-only status queries
before shell parsing. Keep mutating operations on the script-gated paths unless
the user explicitly asks for an MCP-backed mutation.

## Paper Source Edit Protocol

Paper source files outside this scaffold are guarded sources.

1. A proof worker proposes changes as a unified diff under
   `workstreams/<id>/artifacts/source_patches/`.
2. The workstream report explains the mathematical reason for the patch and
   assigns claim ids to substantive claims.
3. Any required computation workstream completes with passing tests and review.
4. The latest reviewer round approves the relevant claims and has no
   `REQUEST_CHANGES` or `BLOCK`.
5. `scripts/check_report_ready.py workstreams/<id>` passes.
6. The coordinator applies the patch only with
   `scripts/apply_approved_patch.py`.
7. The coordinator promotes only with `scripts/promote_workstream.py`.

If a source patch has not passed these steps, treat it as draft only.

## Review And Promotion Gates

A workstream is ready for promotion only when all applicable conditions hold:

- `status.json.current_review_round` matches the latest review files.
- Every latest-round reviewer decision is `APPROVE`.
- No latest-round review has `REQUEST_CHANGES` or `BLOCK`.
- Every substantive report claim id is listed in latest-round
  `approved_claims`.
- `blocked_claims` is empty.
- Required computation gates are linked, complete, reviewed, and fresh.
- Required tests pass when rerun by the promotion gate.
- Worker final output JSON is valid against
  `schemas/worker_final_output.schema.json` when applicable.
- Source patch provenance is present for every paper-source change.
- The report contains the required error decomposition.

Do not argue around a failed gate. Fix the evidence, request changes, block the
workstream, or ask the user for mathematical judgment.

## Claim Registry And Working Paper Rules

- Only promoted workstreams contribute approved claims.
- Claim ids inside workstream reports are local ids such as `C-001`.
- Global claim keys must include workstream provenance, for example
  `ws_012_example:C-001`.
- Working-paper text must not strengthen, generalize, or polish a claim beyond
  what the approved workstream and reviewer JSON support.
- Native mathematical artifacts may include working-paper text, annotations,
  margin notes, source patch artifacts, theorem applicability matrices,
  computation manifests, and failed-exploration notes.

## Failure And Blocked Path Rules

Use `scripts/mark_blocked.py` or the collection failure mechanisms when a path
is blocked. A blocked or failed route should record:

- the mathematical or infrastructure reason;
- what was tried;
- what evidence failed;
- whether the issue is source-setting, finite-size, numerical, theorem-level,
  literature, reviewer, or user-judgment related;
- what would be needed to resume.

Do not remove failed-exploration records merely because a later route succeeds.
