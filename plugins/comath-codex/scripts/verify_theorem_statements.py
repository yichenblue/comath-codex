#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import ValidationError, read_json, repo_root, write_json


NO_EXACT_STATEMENTS_RE = re.compile(r"\bno exact theorem statements\b", re.IGNORECASE)
SOURCE_ID_LINE_RE = re.compile(r"source[_ -]statement[_ -]ids?\s*:\s*(?P<value>.+)", re.IGNORECASE)


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


def file_sha256(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_available_statements(path: Path) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    if not path.exists():
        return {}, {}
    payload = read_json(path)
    statements: dict[str, dict[str, Any]] = {}
    for item in payload.get("statements", []):
        if not isinstance(item, dict):
            continue
        statement_id = item.get("statement_id")
        if isinstance(statement_id, str) and statement_id:
            statements[statement_id] = item
    return statements, payload


def extract_declared_source_ids(text: str) -> list[str]:
    ids: list[str] = []
    for line in text.splitlines():
        match = SOURCE_ID_LINE_RE.search(line)
        if match is None:
            continue
        raw = match.group("value")
        raw = raw.replace("`", " ").replace(",", " ")
        ids.extend(item for item in raw.split() if item.startswith("arxiv:"))
    return sorted(set(ids))


def referenced_statement_ids(text: str, available_ids: set[str]) -> list[str]:
    explicit = set(extract_declared_source_ids(text))
    implicit = {statement_id for statement_id in available_ids if statement_id in text}
    return sorted((explicit | implicit) & available_ids)


def verification_result(workstream: Path) -> dict[str, Any]:
    theorem_md = workstream / "artifacts" / "theorem_statements.md"
    extracted_json = workstream / "artifacts" / "extracted_theorem_statements.json"
    errors: list[str] = []
    warnings: list[str] = []

    if theorem_md.exists():
        text = theorem_md.read_text(encoding="utf-8", errors="replace")
    else:
        text = ""
        errors.append(f"Missing theorem statements file: {theorem_md}")

    if "TBD" in text:
        errors.append("artifacts/theorem_statements.md still contains TBD")

    statements, extracted = load_available_statements(extracted_json)
    available_ids = set(statements)
    declared_ids = extract_declared_source_ids(text)
    cited_ids = referenced_statement_ids(text, available_ids)
    unknown_declared_ids = sorted(set(declared_ids) - available_ids)
    no_exact = bool(NO_EXACT_STATEMENTS_RE.search(text))

    if unknown_declared_ids:
        errors.append(
            "Unknown source_statement_id values in artifacts/theorem_statements.md: "
            + ", ".join(unknown_declared_ids)
        )
    if not no_exact and not cited_ids:
        errors.append(
            "No verified source_statement_id is cited. Either cite an extracted "
            "source_statement_id or write 'No exact theorem statements.'"
        )
    if no_exact and cited_ids:
        warnings.append(
            "Both 'No exact theorem statements.' and verified source_statement_id "
            "citations are present; remove the contradictory marker if exact statements are used."
        )

    extracted_count = len(extracted.get("statements", [])) if isinstance(extracted, dict) else 0
    if extracted_count == 0 and cited_ids:
        errors.append("Theorem statements cite extracted ids, but no extracted statements exist.")

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "scripts/verify_theorem_statements.py",
        "verified": not errors,
        "errors": errors,
        "warnings": warnings,
        "no_exact_theorem_statements": no_exact,
        "available_statement_count": len(available_ids),
        "cited_statement_ids": cited_ids,
        "unknown_declared_statement_ids": unknown_declared_ids,
        "input_files": {
            "theorem_statements_md": str(theorem_md.relative_to(workstream)),
            "theorem_statements_md_sha256": file_sha256(theorem_md),
            "extracted_theorem_statements_json": str(extracted_json.relative_to(workstream)),
            "extracted_theorem_statements_json_sha256": file_sha256(extracted_json),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify that theorem_statements.md cites extracted theorem-like source statements."
    )
    parser.add_argument("workstream", help="Path like workstreams/<id>.")
    parser.add_argument(
        "--output",
        default="artifacts/theorem_statement_verification.json",
        help="Output path relative to the workstream.",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        workstream = resolve_workstream(args.workstream)
        result = verification_result(workstream)
        if args.dry_run:
            print(json.dumps(result, indent=2, sort_keys=False))
        else:
            output = (workstream / args.output).resolve()
            output.relative_to(workstream)
            output.parent.mkdir(parents=True, exist_ok=True)
            write_json(output, result)
            print(f"Wrote {output}")
        return 0 if result["verified"] else 1
    except (ValidationError, ValueError) as exc:
        print(exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
