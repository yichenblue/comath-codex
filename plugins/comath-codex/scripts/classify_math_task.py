#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib import ValidationError, repo_root, task_requires_computation, task_requires_literature


def read_source_context(paths: list[str]) -> str:
    chunks: list[str] = []
    root = repo_root()
    for raw in paths:
        path = Path(raw)
        if path.is_absolute():
            full_path = path
        else:
            full_path = root / path
            if not full_path.exists():
                full_path = root.parent / path
        if not full_path.exists():
            raise ValidationError(f"Source context file does not exist: {raw}")
        chunks.append(full_path.read_text(encoding="utf-8", errors="replace"))
    return "\n\n".join(chunks)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Classify whether a mathematical task should get literature or computation workstreams."
    )
    parser.add_argument("--objective", required=True)
    parser.add_argument("--target-label", action="append", default=[])
    parser.add_argument("--source-context", action="append", default=[], help="Repo-relative source file to include.")
    parser.add_argument("--json", action="store_true", help="Print a JSON object.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    text = args.objective
    if args.target_label:
        text += "\n" + "\n".join(args.target_label)
    if args.source_context:
        try:
            text += "\n" + read_source_context(args.source_context)
        except ValidationError as exc:
            print(exc)
            return 1

    literature_required, literature_triggers = task_requires_literature(text)
    computation_required, computation_triggers = task_requires_computation(text)
    result = {
        "literature_required": literature_required,
        "literature_trigger_keywords": literature_triggers,
        "computation_required": computation_required,
        "computation_trigger_keywords": computation_triggers,
    }
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=False))
    else:
        print(f"literature_required={str(literature_required).lower()}")
        if literature_triggers:
            print("literature_trigger_keywords=" + ", ".join(literature_triggers))
        print(f"computation_required={str(computation_required).lower()}")
        if computation_triggers:
            print("computation_trigger_keywords=" + ", ".join(computation_triggers))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
