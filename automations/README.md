# Codex Automation Templates

These prompts are templates for Codex app automations. They are intentionally
read-only by default. Test an automation prompt manually before scheduling it.

Recommended first automation:

- `health_check.prompt.md`: daily or weekly scaffold consistency check.

Use worktree execution for scheduled jobs that might write files. Use local
execution only when you want the automation to inspect the active checkout.
