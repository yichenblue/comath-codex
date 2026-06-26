# Codex Co-Mathematician Scaffold

This scaffold implements a repo-local version of the AI co-mathematician workflow using Codex-native primitives:

- the repo filesystem as the stateful workspace and shared file system;
- the main Codex thread as the project coordinator agent;
- Codex worker agents as workstream coordinators and specialized sub-agents;
- JSON status files and Python scripts as hard programmatic constraints;
- Markdown/LaTeX artifacts as working papers and auditable reports.

The scaffold is intentionally product-light. It does not try to build a web UI first. It makes the research process explicit, file-backed, reviewable, and hard to accidentally mark complete.

## Directory Map

```text
comath-codex/
  .agents/skills/comath-codex/
  .codex/hooks.json
  project_brief.md
  research_question.md
  goals/
  prompts/
  scripts/
  state/
  tasks/
  workstreams/
  working_paper/
  failed_explorations/
```

## Codex Integration

This repository includes a repo-local Codex skill at
`.agents/skills/comath-codex/`. Invoke it explicitly as `$comath-codex` or let
Codex use it implicitly for scaffold, workstream, worker, reviewer, claim
registry, and working-paper tasks.

The repository also includes Codex lifecycle hooks under `.codex/`. The hooks
embed the scaffold's hard gates into Codex by checking promotion and claim
registry actions before they run, and by running lightweight scaffold validation
at turn stop. If Codex reports that hooks need trust review, inspect them with
the Codex hook UI and trust the repo-local hook definitions before relying on
them.

## Plugin Packaging

This repository is also packaged as a local Codex plugin:

```text
.agents/plugins/marketplace.json
plugins/comath-codex/
  .codex-plugin/plugin.json
  skills/comath-codex/
  scripts/
  schemas/
  prompts/
  hooks/
```

The repo marketplace points to `./plugins/comath-codex`. Restart Codex after
pulling plugin changes so the app can rediscover the local marketplace and the
packaged `$comath-codex` skill.

The plugin package includes the hard-gate hook script as a resource, but it
does not auto-install lifecycle hooks into arbitrary target repositories.
Repo-local hooks remain under `.codex/` because they depend on the scaffold
state layout in the current repository and should be reviewed/trusted per
project.

Validate the packaged plugin with:

```text
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/comath-codex
```

## MCP Server

The repo-local MCP server is `scripts/mcp_scaffold_server.py`. The project
config in `.codex/config.toml` registers it as `comath_codex`.

Initial tools are read-only:

- `list_project_state`
- `list_workstreams`
- `get_workstream`
- `list_tasks`
- `check_report_ready`
- `run_health_check`

These tools expose scaffold state to Codex without requiring ad hoc shell
parsing. Mutating operations such as promotion, collection, or lock recovery
remain script-gated and are not exposed as MCP tools by default.

## Automations

Automation prompt templates live under `automations/`. The default health-check
entry point is:

```text
python3 scripts/automation_health_check.py --json
```

Use Codex app automations for scheduled read-only maintenance such as daily
health checks or unfinished-workstream audits. Keep scheduled jobs read-only
unless a user explicitly approves a mutating automation.

## Minimal Workflow

1. Fill in `project_brief.md` with the user's problem, background, uploaded sources, constraints, and desired output.
2. The project coordinator drafts `research_question.md` and one or more files under `goals/`.
3. The user approves or revises the research question and goals. New goals must
   remain `DRAFT` until an explicit user confirmation is recorded with:

```text
python3 scripts/approve_goal.py \
  --goal-id goal_010 \
  --user-confirmation "User explicitly approved goal_010 for use."
```

   `validate_status.py`, `run_standard_task.py`, and `create_workstream.py`
   reject `APPROVED` goals that lack either a legacy pre-gate entry or a
   user-confirmation record in `state/goal_approval_records.json`.
4. For new mathematical work, prefer the standard task intake. This is the
   coordinator-facing command behind the user prompt
   `use the current scaffold's standard way to handle this task: ...`:

```text
python3 scripts/run_standard_task.py \
  --objective "use the current scaffold's standard way to handle this task: <precise mathematical task>"
```

The intake script infers the latest approved goal, a workstream title, target
labels such as `\label{lem:...}`, and `.tex` source-context files containing
those labels, then delegates to `scripts/run_research_cycle.py`.

5. For explicit manual parameter control, call the automatic independent-worker
   cycle directly:

```text
python3 scripts/run_research_cycle.py \
  --goal-id <goal-id> \
  --title "Short Workstream Title" \
  --objective "Precise mathematical task." \
  --target-label "lem:optional_label" \
  --source-context "../path/to/source.tex"
```

6. The cycle creates workstreams/tasks, launches independent Codex CLI workers, collects outputs, runs review/promotion gates, and updates working-paper claim artifacts.
7. For manual control, the project coordinator can create or update workstreams under `workstreams/`.
8. Create Codex worker tasks with `python3 scripts/create_agent_task.py`.
9. Spawn independent worker processes with `python3 scripts/spawn_task_cli.py tasks/<task-id>.json`.
10. Collect finished workers with `python3 scripts/collect_task_cli.py tasks/<task-id>.json`.
11. Workstream coordinators update their own `report.md`, `artifacts/`, `logs/`, and `status.json`.
12. Reviewer agents write JSON review results under each workstream's `review/` directory.
13. Run `python3 scripts/validate_status.py` from `comath-codex/`.
14. Run `python3 scripts/validate_agent_task.py`.
15. Run `python3 scripts/check_report_ready.py workstreams/<workstream-id>`.
16. If ready, run `python3 scripts/promote_workstream.py workstreams/<workstream-id>`.
17. Run `python3 scripts/update_claim_registry.py`.
18. Run `python3 scripts/synthesize_working_paper.py`.
19. Run `python3 scripts/check_working_paper.py`.
20. The project coordinator manually edits narrative sections in `working_paper/working_paper.tex` without bypassing claim provenance.

## Parallel Codex Worker Runtime

The scaffold can launch repo-local independent workers through Codex CLI. The
current Codex thread acts as the project coordinator: it creates tasks, spawns
worker processes, collects their outputs, and runs promotion gates. Literature,
proof, computation, and reviewer workers run as separate `codex exec`
processes.

For mathematical tasks, `scripts/run_research_cycle.py` is the default
orchestration layer. It classifies the objective and source context. When it
detects a literature/reference/prior-work/exact-theorem-statement task, it
automatically creates an upstream literature review workstream, runs and reviews
the literature worker, and passes its report and artifacts to downstream proof
and computation workers. When it detects a
formula/decomposition/deterministic-equivalence/resolvent/kernel/specialization
style task, it automatically creates a linked computation workstream, attaches
the computation gate to the proof workstream, runs the computation worker,
reviews it, and only then reviews/promotes the proof workstream.

For short coordinator prompts, `scripts/run_standard_task.py` is the default intake layer. It accepts the full user task text, strips the standard scaffold trigger phrase when present, infers task metadata, and calls `scripts/run_research_cycle.py`. The main Codex thread should use this path whenever the user writes `use the current scaffold's standard way to handle this task: ...`.

Runtime state is stored in:

- `state/agent_registry.json` for spawned worker ids, roles, task ids, and lifecycle status;
- `state/write_locks.json` for active write ownership;
- `state/message_queue.jsonl` for coordination events.
- `agent_runs/` for worker prompts, JSONL Codex events, stderr logs, exit status, pre-run manifests, and diff validation results.
- `schemas/worker_final_output.schema.json` for machine-readable worker final
  summaries enforced through `codex exec --output-schema`.

Parallel worker flow:

1. Create a task with `scripts/create_agent_task.py`.
2. Spawn it as a separate Codex CLI worker:

```text
python3 scripts/spawn_task_cli.py tasks/<task-id>.json
```

This acquires write locks, assembles the worker prompt, records a file manifest, and starts a background process.

3. Check active workers:

```text
python3 scripts/list_active_agents.py
```

4. When the worker writes `exit_status.json`, collect it:

```text
python3 scripts/collect_task_cli.py tasks/<task-id>.json
```

Collection rejects failed exits, missing run metadata, missing required outputs, invalid reviewer JSON, failed computation tests, and any file diff outside `allowed_write_paths`. If the runner metadata is missing or malformed, or if the worker process is gone but no `exit_status.json` exists, collection records a failed exploration, marks the task `BLOCKED`, and releases its write locks. If the process is still alive, collection exits with status `2` and leaves the task running.
For `codex_cli` workers, collection also requires the final assistant message to
parse as JSON conforming to `schemas/worker_final_output.schema.json`; the
parsed object is stored as `agent_runs/.../final_output.json`.

For simple polling, always provide an explicit scope. This prevents the
scheduler from accidentally spawning or collecting historical tasks from older
workstreams:

```text
python3 scripts/scheduler.py spawn-ready \
  --workstream-id <workstream-id> \
  --max-agents 2

python3 scripts/scheduler.py collect-finished \
  --workstream-id <workstream-id>
```

`collect-finished` also recovers scoped failed `RUNNING` workers by default:
missing runner metadata, missing or malformed `run.json`, and dead wrapper pids
without `exit_status.json` are sent through collection so the task becomes
`BLOCKED`, a failed exploration is recorded, and write locks are released. Pass
`--no-recover-dead` to disable this recovery path.

Use `--task-id <task-id>` for a single task, `--goal-id <goal-id>` for all tasks
attached to a goal, or `--all` only when intentionally operating on the whole
task queue.

If an active write lock remains after its task owner has already reached a
terminal state, recover it with an explicit scope:

```text
python3 scripts/scheduler.py recover-locks \
  --workstream-id <workstream-id>
```

Use `--dry-run` first to inspect what would be released. `recover-locks` is for
orphan locks only; dead worker tasks should normally be handled through
`collect-finished` so their failed exploration record is preserved.

The older in-thread Codex `spawn_agent` path is still supported: call Codex `spawn_agent`, then record the returned id with `scripts/register_agent.py`, `scripts/mark_agent_started.py`, and `scripts/mark_agent_done.py`.

Only the project coordinator should register workers, release locks, run promotion gates, or edit global project state. Worker agents should stay inside their task's `allowed_write_paths`.

## Sub-agent Requests

Workstream coordinators do not directly spawn specialized sub-agents. They request them through files under `workstreams/<id>/subagent_requests/`:

```text
python3 scripts/request_subagent.py \
  --workstream-id <workstream-id> \
  --requested-by-task <task-id> \
  --agent-type computation \
  --objective "Search for counterexamples up to the configured bound." \
  --reason "The proof branch needs finite obstruction data."
```

The project coordinator approves a request and turns it into a task:

```text
python3 scripts/approve_subagent_request.py workstreams/<id>/subagent_requests/subagent_request_001.json
```

The resulting task is then assembled, spawned, registered, and tracked like any other Codex worker task.

## Notation Discipline

Agents should not introduce new notation unless it is necessary for the assigned task. They should reuse the notation already present in the user brief, source material, workstream reports, and working paper. If new notation is necessary, it must be defined at first use and the report should explain why existing notation was insufficient.

## Hard Gates

A workstream cannot be promoted to `COMPLETE` unless:

- every `APPROVED` goal has a legacy pre-gate entry or explicit user
  confirmation in `state/goal_approval_records.json`;
- its `status.json` is valid;
- its report exists and contains required sections;
- its report contains `## Error Decomposition` and explicitly separates
  source-setting error, finite-\(n\) / Monte Carlo error, numerical quadrature
  / branch error, and theorem-level discrepancy;
- the latest reviewer round matches `status.json.current_review_round`;
- every review in the latest round is `APPROVE`;
- the latest round contains no `REQUEST_CHANGES` or `BLOCK`;
- every claim id in the report's `Claims` section is listed in latest-round `approved_claims`;
- literature workstreams with `theorem_statement_verification_required=true`
  have `artifacts/theorem_statement_verification.json` with `verified=true`;
- computation workstreams pass a fresh test run during promotion;
- computation workstreams have a passing `artifacts/test_run.json` generated by the test runner;
- computation workstreams must complete
  `artifacts/source_setting_manifest.json` before tests, review, or promotion.
  The manifest records the source setting for \(n,d,p,q,\phi,\psi\), the
  constructions of \(\bm F_0,\bm F_1,\bm\beta,\bm y,\widetilde{\bm y}\), the
  Hermite convention, the matrix/ridge/loss normalization, and whether each
  compared quantity is a raw finite object, deterministic equivalent, contour
  approximation, simulation target, diagnostic, or other explicit type;
- computation workstreams must complete
  `artifacts/source_faithfulness_tests.json` before tests, review, or
  promotion.  The required checks are shapes/scaling,
  \(\bm\beta=n^{-1}X^\top y\) rather than \(\bm\beta_\star\),
  \(c_\star^2=\sum_k k!c_{\star,k}^2\), raw finite quantity agreement between
  direct solve and spectral identity, and decomposition identity error at most
  \(10^{-10}\);
- computation workstreams must complete `artifacts/formula_trace.json`.  Every
  theorem formula, deterministic-equivalent formula, or contour formula
  implemented in code must list its source label/location, result name, code
  path/function, and at least one independent sanity test.  Computations that
  implement no such formula must explicitly mark the formula trace
  `NOT_APPLICABLE` with a reason;
- computation workstreams must complete `artifacts/raw_object_validation.json`.
  Every compared object declared as `raw_finite_object` in
  `artifacts/source_setting_manifest.json` must have its own validation record
  with a primary implementation, an independent implementation, and
  `max_error <= 1e-10`;
- reviewer JSON must include a `checklist` that confirms source setting,
  tests, formula trace, raw object validation, and error decomposition.  A
  review cannot approve with any checklist item marked `FAIL`;
- proof workstreams that propose or patch leading-order paper-source formulas
  must set `requires_computation_gate=true` in `status.json` and link a
  completed computation workstream with passing tests and reviewer-approved
  test evidence;
- incomplete proof workstreams whose reports appear to propose or apply a
  leading-order lemma source patch are rejected if the computation gate is not
  enabled;
- independent worker tasks are rejected at collection time if their file diff
  touches paths outside the task's `allowed_write_paths`;
- workstreams created with `requires_independent_workers=true` have completed
  `codex_cli` worker provenance for their main agent role and reviewer role;
- unresolved blockers are absent.

If a computation workstream fails tests during promotion, `promote_workstream.py` marks it `BLOCKED`, writes a failure note under `failed_explorations/`, and records a message queue event. If a workstream cannot pass review within its configured review limit, move it to `BLOCKED` with `mark_blocked.py`.

Literature review can be forced or waived from the standard entry points:

```text
python3 scripts/run_standard_task.py \
  --objective "<task>" \
  --force-literature

python3 scripts/run_standard_task.py \
  --objective "<task>" \
  --no-literature
```

The literature worker writes `artifacts/search_plan.md`,
`artifacts/search_plan.json`,
`artifacts/literature_search_results.json`, `artifacts/literature_search.md`,
`artifacts/followup_queries.md`,
`artifacts/citation_graph.json`, `artifacts/citation_graph.md`,
`artifacts/search_coverage_validation.json`,
`artifacts/literature_sources/source_manifest.json`,
`artifacts/extracted_theorem_statements.json`,
`artifacts/extracted_theorem_statements.md`, `artifacts/sources.md`,
`artifacts/theorem_statements.md`,
`artifacts/theorem_applicability_matrix.md`,
`artifacts/theorem_applicability_matrix.json`,
`artifacts/theorem_statement_verification.json`,
`artifacts/theorem_applicability_validation.json`,
`artifacts/literature_gaps.md`, and `report.md`. Its claims must be reviewed
before downstream work treats them as approved context.

The literature worker can run the repo-local retrieval layer:

```text
python3 scripts/literature_search.py \
  --workstream workstreams/<literature-workstream-id> \
  --query "<mathematical query>" \
  --pages 2
```

By default this searches repo-local sources and, when network access is
available, arXiv, Semantic Scholar, and Crossref. Local search uses BM25-style
ranking. The external providers use multiple recall strategies: arXiv runs
all/title/abstract variants across pages; Semantic Scholar runs paginated paper
search with external ids, citation/reference counts, fields of study, and
open-access PDF metadata; Crossref runs bibliographic, title, and general query
routes. Results are written to
`artifacts/literature_search_results.json` and
`artifacts/literature_search.md`. Provider failures are recorded as evidence;
the worker must mark unavailable sources as `not verified` rather than guessing.
Promotion also requires a filled search plan, follow-up query log, and theorem
applicability matrix, plus passing search-coverage and theorem-applicability
validation artifacts. These artifacts must not contain `TBD`.

Attach the gate with:

```text
python3 scripts/set_computation_gate.py \
  workstreams/<proof-workstream-id> \
  --linked-computation-workstream-id <computation-workstream-id>
```

You can also create a proof workstream with the gate already enabled:

```text
python3 scripts/create_workstream.py \
  --goal-id goal_001 \
  --type proof \
  --title "Leading Formula Audit" \
  --requires-computation-gate \
  --linked-computation-workstream-id ws_010_computation_check
```

The test runner uses `pytest` when available. If `pytest` is not installed, it falls back to an internal lightweight runner for no-argument Python functions named `test_*` in `test_*.py` or `*_test.py` files.

## Claim Format

Use claim ids in every workstream report:

```text
## Claims

- C-001: Precise claim text.
- C-002: Another claim.
```

If the report has no substantive mathematical claims, write:

```text
No substantive claims.
```

Reviewer JSON files must approve claim ids explicitly:

```json
{
  "decision": "APPROVE",
  "approved_claims": ["C-001", "C-002"],
  "blocked_claims": []
}
```

`update_claim_registry.py` converts local workstream claim ids into global claim keys:

```text
workstream_id:C-001
```

Working paper results should cite approved claims with:

```tex
\approvedclaim{workstream_id:C-001}{Claim text}
```

The working paper gate rejects claim keys that are not present as approved entries in `working_paper/claim_registry.json`.

## Agent Roles

Use the templates in `prompts/` when spawning or instructing Codex workers:

- `project_coordinator.md`
- `workstream_coordinator.md`
- `literature_agent.md`
- `proof_agent.md`
- `computation_agent.md`
- `reviewer_agent.md`
- `synthesis_agent.md`

## Agent Task Schema

Use `tasks/*.json` to define Codex worker tasks. A task fixes:

- agent type;
- assigned workstream;
- execution mode and spawn status;
- assigned Codex worker id, once spawned;
- objective;
- input files;
- allowed write paths;
- required outputs;
- success criteria.

Create a task:

```text
python3 scripts/create_agent_task.py \
  --agent-type literature \
  --workstream-id ws_001_literature \
  --objective "Build a source-grounded literature map."
```

Assemble a ready-to-send worker prompt:

```text
python3 scripts/assemble_agent_prompt.py tasks/task_001_literature_ws_001_literature.json
```

Validate all task files:

```text
python3 scripts/validate_agent_task.py
```

## Status Values

Allowed status values:

- `DRAFT`
- `APPROVED`
- `RUNNING`
- `REVIEWING`
- `BLOCKED`
- `COMPLETE`

Only validation scripts should promote a workstream to `COMPLETE`.
