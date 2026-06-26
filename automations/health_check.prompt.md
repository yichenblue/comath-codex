Run a read-only Comath Codex scaffold health check.

Use the repository-local command:

```text
python3 scripts/automation_health_check.py --json
```

Report only if at least one of these is true:

- scaffold health check fails;
- there are active agents;
- there are active write locks;
- there are unfinished workstreams whose status is not COMPLETE.

When reporting, include the failing command output, active agent ids, active
write-lock paths, and unfinished workstream ids. Do not modify files.
