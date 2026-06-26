#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import ValidationError, read_json, repo_root, write_json


USER_AGENT = "comath-codex-literature-graph-expansion/1.0"

SEMANTIC_SCHOLAR_FIELDS = ",".join([
    "title",
    "authors",
    "year",
    "abstract",
    "url",
    "venue",
    "citationCount",
    "referenceCount",
    "externalIds",
    "publicationDate",
    "references.title",
    "references.authors",
    "references.year",
    "references.abstract",
    "references.url",
    "references.venue",
    "references.citationCount",
    "references.referenceCount",
    "references.externalIds",
    "references.publicationDate",
    "citations.title",
    "citations.authors",
    "citations.year",
    "citations.abstract",
    "citations.url",
    "citations.venue",
    "citations.citationCount",
    "citations.referenceCount",
    "citations.externalIds",
    "citations.publicationDate",
])


def normalize_space(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


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


def fetch_json(url: str, timeout: float) -> dict[str, Any]:
    headers = {"User-Agent": USER_AGENT}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def semantic_scholar_url(paper_id: str) -> str:
    encoded = urllib.parse.quote(paper_id, safe=":")
    params = urllib.parse.urlencode({"fields": SEMANTIC_SCHOLAR_FIELDS})
    return f"https://api.semanticscholar.org/graph/v1/paper/{encoded}?{params}"


def seed_identifier(item: dict[str, Any]) -> str | None:
    external_ids = item.get("external_ids") if isinstance(item.get("external_ids"), dict) else {}
    providers = item.get("providers") if isinstance(item.get("providers"), list) else []
    provider = str(item.get("provider") or "")
    source_id = normalize_space(item.get("source_id"))

    if "CorpusId" in external_ids:
        return "CorpusId:" + normalize_space(external_ids["CorpusId"])
    if "DOI" in external_ids:
        return "DOI:" + normalize_space(external_ids["DOI"])
    if "ArXiv" in external_ids:
        return "ARXIV:" + normalize_space(external_ids["ArXiv"])
    if provider == "semantic_scholar" or "semantic_scholar" in providers:
        if source_id and not source_id.lower().startswith(("doi:", "arxiv:")):
            return source_id
    if provider == "crossref" and source_id:
        return "DOI:" + source_id
    if provider == "arxiv" and source_id:
        return "ARXIV:" + source_id
    return None


def authors(item: dict[str, Any], limit: int = 8) -> list[str]:
    raw = item.get("authors")
    if not isinstance(raw, list):
        return []
    names: list[str] = []
    for author in raw[:limit]:
        if isinstance(author, dict):
            name = normalize_space(author.get("name"))
        else:
            name = normalize_space(author)
        if name:
            names.append(name)
    return names


def normalize_paper(
    item: dict[str, Any],
    *,
    relation: str,
    seed_title: str,
    seed_identifier_value: str,
) -> dict[str, Any]:
    external_ids = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
    return {
        "relation": relation,
        "seed_title": seed_title,
        "seed_identifier": seed_identifier_value,
        "paper_id": normalize_space(item.get("paperId")),
        "title": normalize_space(item.get("title")),
        "year": item.get("year"),
        "authors": authors(item),
        "abstract": normalize_space(item.get("abstract")),
        "url": normalize_space(item.get("url")),
        "venue": normalize_space(item.get("venue")),
        "citation_count": item.get("citationCount"),
        "reference_count": item.get("referenceCount"),
        "external_ids": external_ids,
        "publication_date": item.get("publicationDate"),
    }


def paper_key(item: dict[str, Any]) -> str:
    external_ids = item.get("external_ids") if isinstance(item.get("external_ids"), dict) else {}
    doi = normalize_space(external_ids.get("DOI"))
    arxiv = normalize_space(external_ids.get("ArXiv"))
    paper_id = normalize_space(item.get("paper_id"))
    title = normalize_space(item.get("title")).lower()
    if doi:
        return "doi:" + doi.lower()
    if arxiv:
        return "arxiv:" + arxiv.lower()
    if paper_id:
        return "paper:" + paper_id
    if title:
        return "title:" + re.sub(r"[^a-z0-9]+", " ", title)
    return json.dumps(item, sort_keys=True)


def merge_expanded_results(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in items:
        key = paper_key(item)
        if key not in merged:
            record = dict(item)
            record["relations"] = [item["relation"]]
            record["seed_titles"] = [item["seed_title"]]
            record["seed_identifiers"] = [item["seed_identifier"]]
            merged[key] = record
            continue
        record = merged[key]
        for target_key, source_key in [
            ("relations", "relation"),
            ("seed_titles", "seed_title"),
            ("seed_identifiers", "seed_identifier"),
        ]:
            values = record.setdefault(target_key, [])
            value = item.get(source_key)
            if value and value not in values:
                values.append(value)
        for field in ["abstract", "url", "venue", "year", "citation_count", "reference_count"]:
            if not record.get(field) and item.get(field):
                record[field] = item[field]
    return sorted(
        merged.values(),
        key=lambda item: (
            -int(item.get("citation_count") or 0),
            str(item.get("year") or ""),
            str(item.get("title") or ""),
        ),
    )


def seed_records(results: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    seeds: list[dict[str, Any]] = []
    for item in results.get("merged_results", []):
        if not isinstance(item, dict):
            continue
        identifier = seed_identifier(item)
        if not identifier:
            continue
        seeds.append({
            "identifier": identifier,
            "title": normalize_space(item.get("title")),
            "providers": item.get("providers", [item.get("provider", "")]),
            "year": item.get("year"),
            "citation_count": item.get("citation_count"),
            "source_id": item.get("source_id"),
        })
        if len(seeds) >= limit:
            break
    return seeds


def extract_relation_items(data: dict[str, Any], relation: str, limit: int, seed: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get(relation)
    if not isinstance(raw, list):
        return []
    relation_name = "reference" if relation == "references" else "citation"
    result: list[dict[str, Any]] = []
    for item in raw[: max(0, limit)]:
        if isinstance(item, dict) and item.get("title"):
            result.append(
                normalize_paper(
                    item,
                    relation=relation_name,
                    seed_title=seed["title"],
                    seed_identifier_value=seed["identifier"],
                )
            )
    return result


def followup_queries(merged: list[dict[str, Any]], limit: int) -> list[dict[str, str]]:
    queries: list[dict[str, str]] = []
    for item in merged[:limit]:
        title = normalize_space(item.get("title"))
        if not title:
            continue
        queries.append({
            "query": title,
            "reason": "Citation-graph expansion found this related paper; search the title to collect metadata, versions, and source availability.",
            "source": "semantic_scholar_citation_graph",
        })
    return queries


def markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# Citation Graph Expansion",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "## Provider Status",
        "",
    ]
    if payload["errors"]:
        for error in payload["errors"]:
            lines.append(f"- `{error['seed_identifier']}`: {error['error']}")
    else:
        lines.append("- Semantic Scholar expansion completed without recorded provider errors.")

    lines.extend(["", "## Seeds", ""])
    if not payload["seeds"]:
        lines.append("No expandable Semantic Scholar, DOI, or arXiv seeds were found.")
    for seed in payload["seeds"]:
        lines.append(f"- `{seed['identifier']}`: {seed.get('title') or 'untitled'}")

    lines.extend(["", "## Expanded Results", ""])
    if not payload["merged_related_results"]:
        lines.append("No citation-graph results were returned. Treat this as a search-coverage gap.")
    for index, item in enumerate(payload["merged_related_results"], start=1):
        authors_text = ", ".join(item.get("authors", [])[:6]) or "not available"
        lines.extend([
            f"### {index}. {item.get('title') or 'Untitled'}",
            "",
            f"- Relations: {', '.join(item.get('relations', [item.get('relation', '')]))}",
            f"- Seed titles: {', '.join(item.get('seed_titles', []))}",
            f"- Year: {item.get('year') or 'not available'}",
            f"- Authors: {authors_text}",
            f"- Venue: {item.get('venue') or 'not available'}",
            f"- Citation count: {item.get('citation_count') if item.get('citation_count') is not None else 'not available'}",
            f"- URL: {item.get('url') or 'not available'}",
            "",
        ])
        if item.get("abstract"):
            lines.append(normalize_space(item["abstract"])[:1200])
            lines.append("")

    lines.extend(["## Recommended Follow-up Queries", ""])
    if not payload["recommended_followup_queries"]:
        lines.append("No follow-up title queries were generated.")
    for item in payload["recommended_followup_queries"]:
        lines.append(f"- `{item['query']}`")
        lines.append(f"  - Reason: {item['reason']}")
    return "\n".join(lines).rstrip() + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Expand literature search results through Semantic Scholar references and citations."
    )
    parser.add_argument("--workstream", required=True, help="Path like workstreams/<id>.")
    parser.add_argument("--results", default="artifacts/literature_search_results.json")
    parser.add_argument("--seed-limit", type=int, default=8)
    parser.add_argument("--references-limit", type=int, default=25)
    parser.add_argument("--citations-limit", type=int, default=25)
    parser.add_argument("--followup-limit", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--sleep-seconds", type=float, default=1.0)
    parser.add_argument("--json-output", default="artifacts/citation_graph.json")
    parser.add_argument("--markdown-output", default="artifacts/citation_graph.md")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        workstream = resolve_workstream(args.workstream)
        results_path = workstream / args.results
        if not results_path.exists():
            raise ValidationError(f"Missing literature search results: {results_path}")
        results = read_json(results_path)
        seeds = seed_records(results, max(1, args.seed_limit))

        expanded: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        seed_payloads: list[dict[str, Any]] = []
        for index, seed in enumerate(seeds):
            if index > 0 and args.sleep_seconds > 0:
                time.sleep(args.sleep_seconds)
            try:
                data = fetch_json(semantic_scholar_url(seed["identifier"]), args.timeout)
                seed_payloads.append({
                    "identifier": seed["identifier"],
                    "title": seed.get("title", ""),
                    "reference_count": data.get("referenceCount"),
                    "citation_count": data.get("citationCount"),
                    "returned_references": len(data.get("references", []) if isinstance(data.get("references"), list) else []),
                    "returned_citations": len(data.get("citations", []) if isinstance(data.get("citations"), list) else []),
                })
                expanded.extend(extract_relation_items(data, "references", args.references_limit, seed))
                expanded.extend(extract_relation_items(data, "citations", args.citations_limit, seed))
            except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                errors.append({"seed_identifier": seed["identifier"], "error": str(exc)})

        merged = merge_expanded_results(expanded)
        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": "scripts/literature_expand_graph.py",
            "input_results": args.results,
            "seed_limit": args.seed_limit,
            "references_limit": args.references_limit,
            "citations_limit": args.citations_limit,
            "seeds": seeds,
            "seed_payloads": seed_payloads,
            "expanded_results": expanded,
            "merged_related_results": merged,
            "recommended_followup_queries": followup_queries(merged, max(0, args.followup_limit)),
            "errors": errors,
            "notes": [
                "This graph expansion is a recall tool, not evidence that a theorem applies.",
                "Downstream claims still require exact source access and theorem applicability checks.",
            ],
        }
        if args.dry_run:
            print(json.dumps(payload, indent=2, sort_keys=False))
            return 0

        json_path = workstream / args.json_output
        markdown_path = workstream / args.markdown_output
        json_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(json_path, payload)
        markdown_path.write_text(markdown_report(payload), encoding="utf-8")
    except (ValidationError, ValueError) as exc:
        print(exc)
        return 1

    print(f"Wrote {json_path}")
    print(f"Wrote {markdown_path}")
    print(f"Seeds: {len(seeds)}")
    print(f"Merged related results: {len(merged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
