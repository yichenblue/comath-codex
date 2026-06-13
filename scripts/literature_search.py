#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import ValidationError, repo_root, write_json


DEFAULT_PROVIDERS = ["local", "arxiv", "semantic_scholar", "crossref"]
USER_AGENT = "comath-codex-literature-search/1.0"


def slug(value: str, max_len: int = 80) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return (value or "query")[:max_len]


def fetch_text(url: str, timeout: float) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def normalize_space(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def year_from_parts(parts: Any) -> int | None:
    if not isinstance(parts, dict):
        return None
    date_parts = parts.get("date-parts")
    if (
        isinstance(date_parts, list)
        and date_parts
        and isinstance(date_parts[0], list)
        and date_parts[0]
    ):
        year = date_parts[0][0]
        if isinstance(year, int):
            return year
    return None


def local_search(query: str, limit: int) -> dict[str, Any]:
    terms = [term.lower() for term in re.findall(r"[A-Za-z0-9_:-]{3,}", query)]
    if not terms:
        terms = [query.lower()]
    candidates: list[dict[str, Any]] = []
    base = repo_root().parent
    skip_parts = {"agent_runs", "workstreams", "tasks", "__pycache__", ".git"}
    for path in base.rglob("*"):
        if len(candidates) >= limit:
            break
        if not path.is_file() or path.suffix.lower() not in {".tex", ".bib", ".md"}:
            continue
        rel_parts = path.relative_to(base).parts
        if any(part in skip_parts for part in rel_parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lowered = text.lower()
        hits = [term for term in terms if term in lowered]
        if not hits:
            continue
        lines = text.splitlines()
        snippets: list[str] = []
        for index, line in enumerate(lines, start=1):
            if any(term in line.lower() for term in hits):
                snippets.append(f"L{index}: {normalize_space(line)[:240]}")
            if len(snippets) >= 3:
                break
        candidates.append({
            "provider": "local",
            "title": path.name,
            "url": str(path.relative_to(base)),
            "year": None,
            "authors": [],
            "abstract": "",
            "source_id": str(path.relative_to(base)),
            "venue": "",
            "citation_count": None,
            "matched_terms": hits,
            "snippets": snippets,
        })
    return {"provider": "local", "results": candidates, "errors": []}


def arxiv_search(query: str, limit: int, timeout: float) -> dict[str, Any]:
    params = urllib.parse.urlencode({
        "search_query": f"all:{query}",
        "start": 0,
        "max_results": limit,
        "sortBy": "relevance",
        "sortOrder": "descending",
    })
    url = f"https://export.arxiv.org/api/query?{params}"
    try:
        payload = fetch_text(url, timeout)
        root = ET.fromstring(payload)
    except (OSError, urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
        return {"provider": "arxiv", "results": [], "errors": [str(exc)]}

    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    results: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", ns):
        arxiv_url = normalize_space(entry.findtext("atom:id", default="", namespaces=ns))
        arxiv_id = arxiv_url.rsplit("/", 1)[-1]
        authors = [
            normalize_space(author.findtext("atom:name", default="", namespaces=ns))
            for author in entry.findall("atom:author", ns)
        ]
        categories = [
            item.attrib.get("term", "")
            for item in entry.findall("atom:category", ns)
            if item.attrib.get("term")
        ]
        pdf_url = ""
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href", "")
                break
        results.append({
            "provider": "arxiv",
            "title": normalize_space(entry.findtext("atom:title", default="", namespaces=ns)),
            "url": arxiv_url,
            "year": normalize_space(entry.findtext("atom:published", default="", namespaces=ns))[:4],
            "authors": authors,
            "abstract": normalize_space(entry.findtext("atom:summary", default="", namespaces=ns)),
            "source_id": arxiv_id,
            "venue": ", ".join(categories),
            "citation_count": None,
            "pdf_url": pdf_url,
            "source_url": f"https://export.arxiv.org/e-print/{arxiv_id}" if arxiv_id else "",
        })
    return {"provider": "arxiv", "results": results, "errors": []}


def semantic_scholar_search(query: str, limit: int, timeout: float) -> dict[str, Any]:
    fields = ",".join([
        "title",
        "authors",
        "year",
        "abstract",
        "url",
        "venue",
        "citationCount",
        "externalIds",
        "publicationDate",
    ])
    params = urllib.parse.urlencode({"query": query, "limit": limit, "fields": fields})
    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
    try:
        payload = fetch_text(url, timeout)
        data = json.loads(payload)
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"provider": "semantic_scholar", "results": [], "errors": [str(exc)]}

    results: list[dict[str, Any]] = []
    for item in data.get("data", []):
        if not isinstance(item, dict):
            continue
        authors = [
            str(author.get("name", ""))
            for author in item.get("authors", [])
            if isinstance(author, dict) and author.get("name")
        ]
        external_ids = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
        results.append({
            "provider": "semantic_scholar",
            "title": normalize_space(item.get("title")),
            "url": item.get("url") or "",
            "year": item.get("year"),
            "authors": authors,
            "abstract": normalize_space(item.get("abstract")),
            "source_id": item.get("paperId") or external_ids.get("DOI") or "",
            "venue": item.get("venue") or "",
            "citation_count": item.get("citationCount"),
            "external_ids": external_ids,
            "publication_date": item.get("publicationDate"),
        })
    return {"provider": "semantic_scholar", "results": results, "errors": []}


def crossref_search(query: str, limit: int, timeout: float) -> dict[str, Any]:
    params = urllib.parse.urlencode({
        "query": query,
        "rows": limit,
        "select": "DOI,title,author,published-print,published-online,container-title,URL,is-referenced-by-count,type",
    })
    url = f"https://api.crossref.org/works?{params}"
    try:
        payload = fetch_text(url, timeout)
        data = json.loads(payload)
    except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        return {"provider": "crossref", "results": [], "errors": [str(exc)]}

    items = data.get("message", {}).get("items", [])
    results: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = item.get("title") if isinstance(item.get("title"), list) else []
        container = item.get("container-title") if isinstance(item.get("container-title"), list) else []
        authors = []
        for author in item.get("author", []):
            if isinstance(author, dict):
                name = normalize_space(" ".join([
                    str(author.get("given", "")),
                    str(author.get("family", "")),
                ]))
                if name:
                    authors.append(name)
        year = year_from_parts(item.get("published-print")) or year_from_parts(item.get("published-online"))
        doi = item.get("DOI") or ""
        results.append({
            "provider": "crossref",
            "title": normalize_space(title[0] if title else ""),
            "url": item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
            "year": year,
            "authors": authors,
            "abstract": "",
            "source_id": doi,
            "venue": normalize_space(container[0] if container else ""),
            "citation_count": item.get("is-referenced-by-count"),
            "type": item.get("type") or "",
        })
    return {"provider": "crossref", "results": results, "errors": []}


def result_key(item: dict[str, Any]) -> str:
    external_ids = item.get("external_ids") if isinstance(item.get("external_ids"), dict) else {}
    doi = str(external_ids.get("DOI") or "")
    source_id = str(item.get("source_id") or "")
    title = normalize_space(str(item.get("title") or "")).lower()
    if doi:
        return "doi:" + doi.lower()
    if item.get("provider") == "arxiv" and source_id:
        return "arxiv:" + source_id.lower()
    if title:
        return "title:" + re.sub(r"[^a-z0-9]+", " ", title)
    return json.dumps(item, sort_keys=True)


def merge_results(provider_runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for run in provider_runs:
        for item in run.get("results", []):
            key = result_key(item)
            if key not in merged:
                record = dict(item)
                record["providers"] = [item.get("provider")]
                merged[key] = record
            else:
                providers = merged[key].setdefault("providers", [])
                provider = item.get("provider")
                if provider not in providers:
                    providers.append(provider)
                for field in ["abstract", "url", "venue", "year"]:
                    if not merged[key].get(field) and item.get(field):
                        merged[key][field] = item[field]
    return list(merged.values())


def markdown_result(item: dict[str, Any], index: int) -> str:
    authors = ", ".join(item.get("authors", [])[:6])
    if len(item.get("authors", [])) > 6:
        authors += ", et al."
    lines = [
        f"### {index}. {item.get('title') or 'Untitled'}",
        "",
        f"- Providers: {', '.join(item.get('providers', [item.get('provider', '')]))}",
        f"- Year: {item.get('year') or 'not available'}",
        f"- Authors: {authors or 'not available'}",
        f"- Venue/category: {item.get('venue') or 'not available'}",
        f"- URL: {item.get('url') or 'not available'}",
    ]
    if item.get("pdf_url"):
        lines.append(f"- PDF: {item['pdf_url']}")
    if item.get("source_url"):
        lines.append(f"- arXiv source: {item['source_url']}")
    if item.get("citation_count") is not None:
        lines.append(f"- Citation count: {item['citation_count']}")
    abstract = item.get("abstract")
    if abstract:
        lines.extend(["", normalize_space(str(abstract))[:1200]])
    snippets = item.get("snippets") if isinstance(item.get("snippets"), list) else []
    if snippets:
        lines.extend(["", "Local snippets:"])
        for snippet in snippets:
            lines.append(f"- {snippet}")
    return "\n".join(lines)


def write_markdown(path: Path, query: str, provider_runs: list[dict[str, Any]], merged: list[dict[str, Any]]) -> None:
    lines = [
        "# Literature Search",
        "",
        f"Query: `{query}`",
        f"Generated at: `{datetime.now(timezone.utc).isoformat()}`",
        "",
        "## Provider Status",
        "",
    ]
    for run in provider_runs:
        errors = run.get("errors") or []
        status = "ok" if not errors else "error"
        lines.append(f"- {run['provider']}: {status}; results={len(run.get('results', []))}")
        for error in errors:
            lines.append(f"  - `{error}`")
    lines.extend(["", "## Merged Results", ""])
    if not merged:
        lines.append("No verified results were returned. Treat this as a literature gap.")
    for index, item in enumerate(merged, start=1):
        lines.append(markdown_result(item, index))
        lines.append("")
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run repo-local and external literature search for a workstream."
    )
    parser.add_argument("--workstream", required=True, help="Path like workstreams/<id>.")
    parser.add_argument("--query", action="append", required=True)
    parser.add_argument(
        "--provider",
        action="append",
        choices=["all", "local", "arxiv", "semantic_scholar", "crossref"],
        default=[],
    )
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--json-output",
        default="artifacts/literature_search_results.json",
        help="Output path relative to the workstream.",
    )
    parser.add_argument(
        "--markdown-output",
        default="artifacts/literature_search.md",
        help="Output path relative to the workstream.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Print JSON without writing output files.")
    return parser.parse_args()


def providers_from_args(values: list[str]) -> list[str]:
    if not values or "all" in values:
        return DEFAULT_PROVIDERS
    result: list[str] = []
    for value in values:
        if value not in result:
            result.append(value)
    return result


def run_provider(provider: str, query: str, limit: int, timeout: float) -> dict[str, Any]:
    if provider == "local":
        return local_search(query, limit)
    if provider == "arxiv":
        return arxiv_search(query, limit, timeout)
    if provider == "semantic_scholar":
        return semantic_scholar_search(query, limit, timeout)
    if provider == "crossref":
        return crossref_search(query, limit, timeout)
    raise ValidationError(f"Unknown provider: {provider}")


def main() -> int:
    args = parse_args()
    try:
        workstream = Path(args.workstream)
        if not workstream.is_absolute():
            workstream = repo_root() / workstream
        workstream = workstream.resolve()
        repo_relative = workstream.relative_to(repo_root())
        if not str(repo_relative).startswith("workstreams/"):
            raise ValidationError("--workstream must be under comath-codex/workstreams/")
        if not workstream.exists():
            raise ValidationError(f"Workstream does not exist: {workstream}")
        providers = providers_from_args(args.provider)
        provider_runs: list[dict[str, Any]] = []
        for query in args.query:
            for provider in providers:
                run = run_provider(provider, query, max(1, args.limit), args.timeout)
                run["query"] = query
                provider_runs.append(run)
        merged = merge_results(provider_runs)
        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "queries": args.query,
            "providers": providers,
            "provider_runs": provider_runs,
            "merged_results": merged,
        }
        if args.dry_run:
            print(json.dumps(payload, indent=2, sort_keys=False))
            return 0
        json_path = workstream / args.json_output
        markdown_path = workstream / args.markdown_output
        json_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(json_path, payload)
        write_markdown(markdown_path, "; ".join(args.query), provider_runs, merged)
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Wrote {json_path}")
    print(f"Wrote {markdown_path}")
    print(f"Merged results: {len(merged)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
