# Agent Runs

This directory stores per-task Codex CLI worker runtime artifacts.

Each run directory may contain:

- `prompt.md`: the exact prompt sent to the worker;
- `command.json`: the Codex CLI command specification;
- `events.jsonl`: Codex CLI JSONL event output;
- `stderr.log`: process stderr;
- `last_message.md`: final worker message from Codex CLI;
- `pre_manifest.json`: file hash manifest captured immediately before launch;
- `diff_result.json`: allowlist diff validation written during collection;
- `exit_status.json`: worker process exit code and timing;
- `collection_result.json`: collector gate result.

These files are coordinator/runtime artifacts. Worker outputs still belong under the task's assigned `allowed_write_paths`.
