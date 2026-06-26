Audit unfinished Comath Codex workstreams.

Use repo-local scaffold tools only. Start with:

```text
python3 scripts/automation_health_check.py --json
python3 scripts/validate_status.py
python3 scripts/validate_agent_task.py
```

Identify workstreams in RUNNING, REVIEWING, APPROVED, DRAFT, or BLOCKED state.
For each unfinished workstream, report:

- workstream id and status;
- active or stale worker tasks, if any;
- active write locks, if any;
- the next safe coordinator action.

Do not promote, collect, spawn, recover locks, edit source files, or modify
state unless the user explicitly asks in the current thread.
