# Agent Task Schema

This directory stores task JSON files for Codex worker agents.

The task schema is intentionally thin. It does not run agents by itself. It fixes the input/output contract so the project coordinator can hand a task to a Codex worker, and the hard gates can later verify outputs.

## Task Lifecycle

Allowed task statuses:

- `DRAFT`
- `READY`
- `IN_PROGRESS`
- `DONE`
- `BLOCKED`

## Required Fields

```json
{
  "task_id": "task_001_literature_ws_001",
  "agent_type": "literature",
  "workstream_id": "ws_001_literature",
  "status": "READY",
  "execution_mode": "codex_cli",
  "spawn_status": "NOT_SPAWNED",
  "assigned_agent_id": null,
  "parent_task_id": null,
  "parent_workstream_id": null,
  "subagent_request_id": null,
  "objective": "Build a source-grounded literature map.",
  "input_files": [
    "research_question.md",
    "goals/goal_001.md",
    "workstreams/ws_001_literature/instructions.md",
    "workstreams/ws_001_literature/report.md"
  ],
  "allowed_write_paths": [
    "workstreams/ws_001_literature/report.md",
    "workstreams/ws_001_literature/artifacts/sources.md"
  ],
  "required_outputs": [
    "workstreams/ws_001_literature/report.md"
  ],
  "success_criteria": [
    "Every substantive claim uses a C-### id.",
    "New notation is avoided unless necessary, and necessary new notation is defined and justified at first use.",
    "Unverified claims are marked explicitly."
  ],
  "handoff_summary": "",
  "completion_summary": "",
  "notes": ""
}
```

## Runtime Fields

- `execution_mode`: `codex_cli`, `codex_worker`, `local_thread`, or `external`.
- `spawn_status`: `NOT_SPAWNED`, `SPAWNED`, `RUNNING`, `DONE`, `BLOCKED`, or `CLOSED`.
- `assigned_agent_id`: worker id assigned by `scripts/spawn_task_cli.py` or returned by Codex `spawn_agent` and recorded by `scripts/register_agent.py`.
- `parent_task_id`: the workstream coordinator or parent task that requested this task, if any.
- `subagent_request_id`: the approved request id under `workstreams/<id>/subagent_requests/`, if this task came from a sub-agent request.

For repo-local independent workers, use `scripts/spawn_task_cli.py`; it launches a separate `codex exec` process, records a pre-run file manifest, acquires write locks, and stores logs under `agent_runs/`. For in-thread Codex sub-agents, the project coordinator can still call Codex `spawn_agent`, then record the returned id with `scripts/register_agent.py`.

## Agent Types

- `literature`
- `proof`
- `computation`
- `reviewer`
- `synthesis`
- `workstream_coordinator`

Non-synthesis tasks must keep `allowed_write_paths` and `required_outputs` inside their assigned workstream directory.

Reviewer tasks should write only to their assigned review JSON path. Synthesis tasks are the only task type allowed to write to `working_paper/`.
