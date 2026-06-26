---
name: comath-codex
description: Use for repo-local AI co-mathematician research workflows, including project briefs, goals, workstreams, proof/computation/literature/reviewer workers, claim registry updates, working-paper synthesis, and hard-gated promotion.
---

# Comath-Codex

Use this skill inside a repository containing the `comath-codex` scaffold. The scaffold turns Codex into a file-backed mathematical research workspace with project coordination, independent workers, reviewer gates, computation gates, failed-exploration records, and approved-claim synthesis.

## Default Intake

For a new user research task, prefer the standard intake unless the user asks for manual control:

```bash
python3 scripts/run_standard_task.py --objective "<full user task>"
```

Use `--dry-run` first when you need to inspect inferred goal, title, target labels, or source-context files before launching workers.

## Manual Cycle

When manual control is required, use the repo scripts instead of editing state files directly:

1. Confirm the goal is explicitly approved in `state/goal_approval_records.json`.
2. Create/start workstreams with `scripts/create_workstream.py` and `scripts/start_workstream.py`.
3. Create workers with `scripts/create_agent_task.py`.
4. Spawn and collect workers with `scripts/spawn_task_cli.py` and `scripts/collect_task_cli.py`.
5. Submit workstreams for review with `scripts/submit_for_review.py`.
6. Run readiness checks with `scripts/check_report_ready.py`.
7. Promote only through `scripts/promote_workstream.py`.
8. Update claims and working paper with `scripts/update_claim_registry.py`, `scripts/synthesize_working_paper.py`, and `scripts/check_working_paper.py`.

## Worker Boundaries

The main Codex thread is the project coordinator. Worker tasks must stay inside their task `allowed_write_paths`. Do not bypass write locks, reviewer approval, computation gates, or claim-level approval by manually editing global state, reviewer JSON, claim registry, or final working-paper claim artifacts.

## Review Discipline

Before treating a result as paper-ready, require:

- latest review round matches `status.json.current_review_round`;
- every latest-round reviewer decision is `APPROVE`;
- no latest-round `REQUEST_CHANGES` or `BLOCK`;
- every report claim id is explicitly listed in latest-round `approved_claims`;
- computation workstreams have fresh passing tests and source-faithfulness artifacts;
- proof workstreams that depend on computation evidence link the completed computation workstream.

If a path is blocked, record it through `scripts/mark_blocked.py` or the failed-exploration mechanism instead of silently discarding it.

## MCP And Automations

When scaffold-state MCP tools are available, prefer them for read-only status
queries before falling back to shell commands. Keep mutating operations on the
existing script gates unless the user explicitly asks for an MCP-backed
mutation.

For scheduled maintenance, use the read-only automation templates in
`automations/`. Do not schedule automations that promote workstreams, collect
workers, recover locks, or edit source files unless the user explicitly asks
for that exact behavior.
