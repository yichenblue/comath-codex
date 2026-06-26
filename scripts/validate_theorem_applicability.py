#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import ValidationError, read_json, repo_root, write_json


ALLOWED_MATCH_STATUSES = {"match", "partial_match", "non_match", "not_verified"}


def resolve_workstream(raw: str) -> Path:
    path = Path(raw)
    candidates = [path] if path.is_absolute() else [repo_root() / path, repo_root().parent / path]
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.exists():
            resolved.relative_to(repo_root() / "workstreams")
            return resolved
    resolved = candidates[0].resolve()
    resolved.relative_to(repo_root() / "workstreams")
    return resolved


def load_available_statement_ids(workstream: Path) -> set[str]:
    path = workstream / "artifacts" / "extracted_theorem_statements.json"
    if not path.exists():
        return set()
    payload = read_json(path)
    result: set[str] = set()
    for item in payload.get("statements", []):
        if isinstance(item, dict) and isinstance(item.get("statement_id"), str):
            result.add(item["statement_id"])
    return result


def nonempty_list(value: Any) -> bool:
    return isinstance(value, list) and any(str(item).strip() for item in value)


def validate_entry(entry: dict[str, Any], index: int, available_ids: set[str]) -> list[str]:
    prefix = f"entries[{index}]"
    errors: list[str] = []

    source_id = str(entry.get("source_statement_id") or "").strip()
    if not source_id:
        errors.append(f"{prefix}: missing source_statement_id")
    elif source_id not in available_ids and source_id not in {"not_available", "not_applicable"}:
        errors.append(f"{prefix}: source_statement_id {source_id!r} is not present in extracted_theorem_statements.json")

    match_status = str(entry.get("match_status") or "").strip()
    if match_status not in ALLOWED_MATCH_STATUSES:
        errors.append(
            f"{prefix}: match_status must be one of {sorted(ALLOWED_MATCH_STATUSES)}, got {match_status!r}"
        )

    usable = entry.get("usable_downstream")
    if not isinstance(usable, bool):
        errors.append(f"{prefix}: usable_downstream must be a boolean")
        usable = False

    if match_status in {"non_match", "not_verified"} and usable is True:
        errors.append(f"{prefix}: usable_downstream must be false when match_status is {match_status!r}")

    if source_id in {"not_available", "not_applicable"} and match_status != "not_verified":
        errors.append(f"{prefix}: unavailable source statements must have match_status='not_verified'")

    missing = entry.get("missing_hypotheses")
    if missing is not None and not isinstance(missing, list):
        errors.append(f"{prefix}: missing_hypotheses must be a list")
        missing = []
    if match_status == "match" and missing:
        errors.append(f"{prefix}: match_status='match' cannot have missing_hypotheses")

    caveats = entry.get("caveats")
    if caveats is not None and not isinstance(caveats, list):
        errors.append(f"{prefix}: caveats must be a list")
        caveats = []

    if match_status == "partial_match" and not caveats:
        errors.append(f"{prefix}: partial_match requires nonempty caveats")

    if usable is True:
        for field in [
            "source_hypotheses",
            "target_setting_requirements",
            "matched_hypotheses",
        ]:
            if not nonempty_list(entry.get(field)):
                errors.append(f"{prefix}: usable downstream theorem requires nonempty {field}")
        notation_mapping = entry.get("notation_mapping")
        if not isinstance(notation_mapping, (dict, list)) or not notation_mapping:
            errors.append(f"{prefix}: usable downstream theorem requires nonempty notation_mapping")
        if not str(entry.get("downstream_use") or "").strip():
            errors.append(f"{prefix}: usable downstream theorem requires downstream_use")

    return errors


def validation_result(workstream: Path) -> dict[str, Any]:
    matrix_path = workstream / "artifacts" / "theorem_applicability_matrix.json"
    errors: list[str] = []
    warnings: list[str] = []

    try:
        matrix = read_json(matrix_path)
    except ValidationError as exc:
        matrix = {}
        errors.append(str(exc))

    available_ids = load_available_statement_ids(workstream)
    entries = matrix.get("entries", [])
    if not isinstance(entries, list):
        errors.append("theorem_applicability_matrix.json: entries must be a list")
        entries = []

    no_exact = bool(matrix.get("no_exact_theorem_statements"))
    no_exact_reason = str(matrix.get("no_exact_theorem_statements_reason") or "").strip()
    if no_exact:
        if entries:
            errors.append("theorem_applicability_matrix.json: no_exact_theorem_statements=true but entries is nonempty")
        if not no_exact_reason:
            errors.append("theorem_applicability_matrix.json: no_exact_theorem_statements_reason is required")
    else:
        if not entries:
            errors.append(
                "theorem_applicability_matrix.json: entries is empty; set no_exact_theorem_statements=true with a reason if no exact statements are used"
            )

    usable_count = 0
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            errors.append(f"entries[{index}]: entry must be an object")
            continue
        errors.extend(validate_entry(entry, index, available_ids))
        if entry.get("usable_downstream") is True:
            usable_count += 1

    if entries and usable_count == 0:
        warnings.append(
            "No theorem is marked usable_downstream=true. This is allowed, but downstream proof workers must treat the literature as context only."
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "scripts/validate_theorem_applicability.py",
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "available_extracted_statement_count": len(available_ids),
            "applicability_entry_count": len(entries),
            "usable_downstream_entry_count": usable_count,
            "no_exact_theorem_statements": no_exact,
        },
        "input_files": {
            "theorem_applicability_matrix_json": str(matrix_path.relative_to(workstream)),
            "extracted_theorem_statements_json": "artifacts/extracted_theorem_statements.json",
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate structured theorem applicability matrix entries."
    )
    parser.add_argument("workstream", help="Path like workstreams/<id>.")
    parser.add_argument("--output", default="artifacts/theorem_applicability_validation.json")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        workstream = resolve_workstream(args.workstream)
        result = validation_result(workstream)
        if args.dry_run:
            print(json.dumps(result, indent=2, sort_keys=False))
        else:
            output = (workstream / args.output).resolve()
            output.relative_to(workstream)
            output.parent.mkdir(parents=True, exist_ok=True)
            write_json(output, result)
            print(f"Wrote {output}")
        return 0 if result["passed"] else 1
    except (ValidationError, ValueError) as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
