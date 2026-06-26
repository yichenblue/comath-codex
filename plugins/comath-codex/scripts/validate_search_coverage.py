#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import ValidationError, read_json, repo_root, write_json


DEFAULT_REQUIRED_FAMILIES = [
    "core_problem",
    "method_and_technique",
    "exact_statements",
    "alternate_terminology",
]

DEFAULT_REQUIRED_PROVIDERS = [
    "local",
    "arxiv",
    "semantic_scholar",
    "crossref",
]


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


def safe_read_json(path: Path) -> tuple[dict[str, Any], str | None]:
    try:
        return read_json(path), None
    except ValidationError as exc:
        return {}, str(exc)


def bullet_count(text: str) -> int:
    return len(re.findall(r"(?m)^\s*[-*]\s+", text))


def declared_no_followups(text: str) -> bool:
    return bool(
        re.search(
            r"\b(no|none|not)\s+(follow[- ]?up|additional)\b|"
            r"\bno follow[- ]?up quer(?:y|ies)\b",
            text,
            flags=re.IGNORECASE,
        )
    )


def family_queries(plan: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for family in plan.get("query_families", []):
        if not isinstance(family, dict):
            continue
        name = str(family.get("family") or "")
        queries = family.get("queries", [])
        if name and isinstance(queries, list):
            result[name] = [item for item in queries if isinstance(item, dict) and item.get("query")]
    return result


def provider_attempts(results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    attempts: dict[str, dict[str, Any]] = {}
    for run in results.get("provider_runs", []):
        if not isinstance(run, dict):
            continue
        provider = str(run.get("provider") or "")
        if not provider:
            continue
        entry = attempts.setdefault(provider, {"runs": 0, "results": 0, "errors": []})
        entry["runs"] += 1
        if isinstance(run.get("results"), list):
            entry["results"] += len(run["results"])
        errors = run.get("errors")
        if isinstance(errors, list):
            entry["errors"].extend(str(item) for item in errors)
        diagnostics = run.get("diagnostics")
        if isinstance(diagnostics, dict):
            strategy_count = diagnostics.get("strategy_count")
            pages = diagnostics.get("pages_per_strategy")
            if isinstance(strategy_count, int):
                entry["max_strategy_count"] = max(int(entry.get("max_strategy_count", 0)), strategy_count)
            if isinstance(pages, int):
                entry["max_pages_per_strategy"] = max(int(entry.get("max_pages_per_strategy", 0)), pages)
    return attempts


def validation_result(
    workstream: Path,
    *,
    required_families: list[str],
    required_providers: list[str],
    min_total_queries: int,
    min_queries_per_required_family: int,
    min_provider_runs: int,
    min_followups_when_recommended: int,
) -> dict[str, Any]:
    artifacts = workstream / "artifacts"
    errors: list[str] = []
    warnings: list[str] = []

    plan_path = artifacts / "search_plan.json"
    results_path = artifacts / "literature_search_results.json"
    citation_path = artifacts / "citation_graph.json"
    followup_path = artifacts / "followup_queries.md"

    plan, plan_error = safe_read_json(plan_path)
    results, results_error = safe_read_json(results_path)
    citation_graph, citation_error = safe_read_json(citation_path)
    followup_text = followup_path.read_text(encoding="utf-8", errors="replace") if followup_path.exists() else ""

    for label, error in [
        ("search_plan.json", plan_error),
        ("literature_search_results.json", results_error),
        ("citation_graph.json", citation_error),
    ]:
        if error:
            errors.append(f"{label}: {error}")

    families = family_queries(plan)
    total_planned_queries = sum(len(items) for items in families.values())
    if total_planned_queries < min_total_queries:
        errors.append(
            f"search_plan.json: planned query count {total_planned_queries} is below required minimum {min_total_queries}"
        )
    missing_families = [name for name in required_families if name not in families]
    if missing_families:
        errors.append("search_plan.json: missing required query families: " + ", ".join(missing_families))
    for name in required_families:
        count = len(families.get(name, []))
        if count < min_queries_per_required_family:
            errors.append(
                f"search_plan.json: family {name!r} has {count} query/queries, "
                f"below required minimum {min_queries_per_required_family}"
            )

    attempts = provider_attempts(results)
    attempted_providers = sorted(attempts)
    for provider in required_providers:
        if provider not in attempts:
            errors.append(f"literature_search_results.json: required provider {provider!r} was not attempted")
            continue
        if attempts[provider]["runs"] < min_provider_runs:
            errors.append(
                f"literature_search_results.json: provider {provider!r} has "
                f"{attempts[provider]['runs']} run(s), below required minimum {min_provider_runs}"
            )
        if attempts[provider]["errors"]:
            warnings.append(
                f"literature_search_results.json: provider {provider!r} recorded "
                f"{len(attempts[provider]['errors'])} error(s); this is allowed for coverage "
                "only if the failures are discussed in the literature report."
            )

    provider_results = {provider: data["results"] for provider, data in attempts.items()}
    if not any(count > 0 for count in provider_results.values()):
        warnings.append("literature_search_results.json: all attempted providers returned zero results")
    external_diagnostics = {
        provider: {
            "max_strategy_count": data.get("max_strategy_count", 0),
            "max_pages_per_strategy": data.get("max_pages_per_strategy", 0),
        }
        for provider, data in attempts.items()
        if provider in {"arxiv", "semantic_scholar", "crossref"}
    }
    for provider, diagnostics in external_diagnostics.items():
        if int(diagnostics.get("max_pages_per_strategy") or 0) < 2:
            warnings.append(
                f"literature_search_results.json: provider {provider!r} did not record a multi-page external search; "
                "use literature_search.py --pages 2 or explain why a quick search was sufficient."
            )
        if provider in {"arxiv", "crossref"} and int(diagnostics.get("max_strategy_count") or 0) < 2:
            warnings.append(
                f"literature_search_results.json: provider {provider!r} did not record multiple query strategies; "
                "rerun with the enhanced literature_search.py or explain the provider limitation."
            )

    recommended = citation_graph.get("recommended_followup_queries", [])
    if not isinstance(recommended, list):
        recommended = []
    merged_related = citation_graph.get("merged_related_results", [])
    if not isinstance(merged_related, list):
        merged_related = []
    citation_errors = citation_graph.get("errors", [])
    if isinstance(citation_errors, list) and citation_errors:
        warnings.append(
            f"citation_graph.json: Semantic Scholar expansion recorded {len(citation_errors)} error(s)"
        )

    followup_bullets = bullet_count(followup_text)
    no_followups = declared_no_followups(followup_text)
    if recommended:
        if followup_bullets < min_followups_when_recommended:
            errors.append(
                f"followup_queries.md: citation graph recommended {len(recommended)} follow-up query/queries, "
                f"but followup_queries.md has only {followup_bullets} bullet(s)"
            )
        if no_followups:
            errors.append(
                "followup_queries.md: declares no follow-up despite citation_graph.json recommended follow-up queries"
            )
    elif not no_followups and followup_bullets == 0:
        errors.append(
            "followup_queries.md: must either record follow-up queries or explicitly state why no follow-up was justified"
        )

    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generated_by": "scripts/validate_search_coverage.py",
        "passed": not errors,
        "errors": errors,
        "warnings": warnings,
        "metrics": {
            "planned_query_family_count": len(families),
            "planned_query_count": total_planned_queries,
            "required_families": required_families,
            "attempted_providers": attempted_providers,
            "required_providers": required_providers,
            "provider_result_counts": provider_results,
            "external_provider_diagnostics": external_diagnostics,
            "citation_graph_related_result_count": len(merged_related),
            "citation_graph_recommended_followup_count": len(recommended),
            "followup_bullet_count": followup_bullets,
            "declared_no_followups": no_followups,
        },
        "input_files": {
            "search_plan_json": str(plan_path.relative_to(workstream)),
            "literature_search_results_json": str(results_path.relative_to(workstream)),
            "citation_graph_json": str(citation_path.relative_to(workstream)),
            "followup_queries_md": str(followup_path.relative_to(workstream)),
        },
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate literature-search query-family, provider, and follow-up coverage."
    )
    parser.add_argument("workstream", help="Path like workstreams/<id>.")
    parser.add_argument("--required-family", action="append", default=[])
    parser.add_argument("--required-provider", action="append", default=[])
    parser.add_argument("--min-total-queries", type=int, default=8)
    parser.add_argument("--min-queries-per-required-family", type=int, default=1)
    parser.add_argument("--min-provider-runs", type=int, default=1)
    parser.add_argument("--min-followups-when-recommended", type=int, default=1)
    parser.add_argument("--output", default="artifacts/search_coverage_validation.json")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        workstream = resolve_workstream(args.workstream)
        result = validation_result(
            workstream,
            required_families=args.required_family or DEFAULT_REQUIRED_FAMILIES,
            required_providers=args.required_provider or DEFAULT_REQUIRED_PROVIDERS,
            min_total_queries=max(0, args.min_total_queries),
            min_queries_per_required_family=max(0, args.min_queries_per_required_family),
            min_provider_runs=max(0, args.min_provider_runs),
            min_followups_when_recommended=max(0, args.min_followups_when_recommended),
        )
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
