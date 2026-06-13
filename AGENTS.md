# Codex Co-Mathematician Scaffold Instructions

When working inside `comath-codex/`, the main Codex thread is the project
coordinator.

For mathematical research tasks, use the scaffold state machine: workstreams,
agent tasks, reviewer JSON, promotion gates, claim registry, and working-paper
synthesis. Do not bypass review, claim-level approval, computation gates, or
worker-provenance gates.

New goals must remain `DRAFT` until the user explicitly approves them. Record
that approval with `scripts/approve_goal.py`; do not mark new goals `APPROVED`
by editing `state/project_state.json` directly.

## Project-Specific Conventions

Record project-specific mathematical conventions in `project_brief.md` and
`research_question.md` after the user explicitly approves them. Do not encode
paper-specific assumptions in prompts, scripts, or global state unless they are
intended to apply to every future project that uses this scaffold.

Paper source files outside `comath-codex/` are guarded sources. Do not edit
them directly from the coordinator thread or from worker tasks. Proof workers
must write proposed source changes as patch files under
`workstreams/<id>/artifacts/source_patches/`. Apply a proposed paper-source
patch only with `scripts/apply_approved_patch.py` after computation,
reviewer, claim-level, and worker-provenance gates pass.

Codex CLI workers must write only inside their assigned `allowed_write_paths`.
They must not create or modify scaffold-level files unless that path is listed
in the task JSON.
