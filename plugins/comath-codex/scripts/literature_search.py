#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from typing import Any

from lib import ValidationError, repo_root, write_json


DEFAULT_PROVIDERS = ["local", "arxiv", "semantic_scholar", "crossref"]
USER_AGENT = "comath-codex-literature-search/1.1"
LOCAL_SEARCH_SUFFIXES = {".tex", ".bib", ".md"}
LOCAL_SEARCH_SKIP_PARTS = {"agent_runs", "workstreams", "tasks", "__pycache__", ".git"}
LOCAL_BM25_K1 = 1.5
LOCAL_BM25_B = 0.75

LOCAL_STOPWORDS = {
    "about",
    "after",
    "against",
    "also",
    "among",
    "and",
    "appendix",
    "are",
    "because",
    "before",
    "between",
    "can",
    "current",
    "does",
    "each",
    "from",
    "have",
    "into",
    "label",
    "lemma",
    "paper",
    "proof",
    "result",
    "section",
    "source",
    "that",
    "the",
    "their",
    "then",
    "there",
    "this",
    "using",
    "with",
}


def slug(value: str, max_len: int = 80) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return (value or "query")[:max_len]


def fetch_text(url: str, timeout: float, headers: dict[str, str] | None = None) -> str:
    request_headers = {"User-Agent": USER_AGENT}
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, headers=request_headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", errors="replace")


def normalize_space(value: str | None) -> str:
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip()


def strip_markup(value: str | None) -> str:
    if not value:
        return ""
    value = re.sub(r"<[^>]+>", " ", value)
    return normalize_space(value)


def search_terms(query: str, *, min_len: int = 3, max_terms: int = 8) -> list[str]:
    terms: list[str] = []
    for term in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", query.lower()):
        term = term.strip("_-")
        if len(term) >= min_len and term not in LOCAL_STOPWORDS and term not in terms:
            terms.append(term)
        if len(terms) >= max_terms:
            break
    return terms


def unique_results(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    unique: list[dict[str, Any]] = []
    for item in results:
        key = result_key(item)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def local_tokens(value: str) -> list[str]:
    tokens = []
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_:-]{2,}", value.lower()):
        token = token.strip("_:-")
        if len(token) >= 3 and token not in LOCAL_STOPWORDS:
            tokens.append(token)
    return tokens


def local_phrases(query: str) -> list[str]:
    quoted = re.findall(r'"([^"]{3,})"', query)
    candidates = quoted + re.findall(
        r"\b(?:deterministic equivalent|deterministic equivalence|random features?|"
        r"stieltjes transform|weighted spectral measure|gradient descent|"
        r"scaling law|contour integral|marchenko pastur|kernel regression)\b",
        query,
        flags=re.IGNORECASE,
    )
    return sorted({normalize_space(item).lower() for item in candidates if normalize_space(item)})


def local_snippets(text: str, query_terms: list[str], phrases: list[str], limit: int = 3) -> list[str]:
    lines = text.splitlines()
    snippets: list[str] = []
    for index, line in enumerate(lines, start=1):
        lowered = line.lower()
        if any(phrase in lowered for phrase in phrases) or any(term in lowered for term in query_terms):
            snippets.append(f"L{index}: {normalize_space(line)[:240]}")
        if len(snippets) >= limit:
            break
    return snippets


def local_candidate_paths(base: Path) -> list[Path]:
    paths: list[Path] = []
    for path in base.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in LOCAL_SEARCH_SUFFIXES:
            continue
        rel_parts = path.relative_to(base).parts
        if any(part in LOCAL_SEARCH_SKIP_PARTS for part in rel_parts):
            continue
        paths.append(path)
    return sorted(paths)


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
    query_terms = local_tokens(query)
    if not query_terms:
        query_terms = [query.lower()]
    phrases = local_phrases(query)
    base = repo_root().parent
    documents: list[dict[str, Any]] = []
    document_frequency: Counter[str] = Counter()
    for path in local_candidate_paths(base):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tokens = local_tokens(text)
        if not tokens:
            continue
        counts = Counter(tokens)
        for term in set(tokens):
            document_frequency[term] += 1
        documents.append({
            "path": path,
            "text": text,
            "length": len(tokens),
            "counts": counts,
        })

    if not documents:
        return {"provider": "local", "results": [], "errors": []}

    average_length = sum(doc["length"] for doc in documents) / len(documents)
    scored: list[dict[str, Any]] = []
    for doc in documents:
        score = 0.0
        matched_terms: list[str] = []
        for term in query_terms:
            tf = doc["counts"].get(term, 0)
            if tf <= 0:
                continue
            matched_terms.append(term)
            df = document_frequency.get(term, 0)
            idf = math.log(1.0 + (len(documents) - df + 0.5) / (df + 0.5))
            denom = tf + LOCAL_BM25_K1 * (1.0 - LOCAL_BM25_B + LOCAL_BM25_B * doc["length"] / average_length)
            score += idf * tf * (LOCAL_BM25_K1 + 1.0) / denom

        phrase_hits = []
        lowered = doc["text"].lower()
        for phrase in phrases:
            if phrase in lowered:
                phrase_hits.append(phrase)
                score += 2.0

        if score <= 0:
            continue
        path = doc["path"]
        scored.append({
            "provider": "local",
            "title": path.name,
            "url": str(path.relative_to(base)),
            "year": None,
            "authors": [],
            "abstract": "",
            "source_id": str(path.relative_to(base)),
            "venue": "",
            "citation_count": None,
            "matched_terms": sorted(set(matched_terms)),
            "matched_phrases": phrase_hits,
            "local_bm25_score": score,
            "snippets": local_snippets(doc["text"], query_terms, phrases),
        })
    candidates = sorted(
        scored,
        key=lambda item: (-float(item.get("local_bm25_score") or 0.0), str(item.get("url") or "")),
    )[: max(1, limit)]
    return {"provider": "local", "results": candidates, "errors": []}


def arxiv_query_variants(query: str) -> list[tuple[str, str]]:
    terms = search_terms(query, max_terms=6)
    variants: list[tuple[str, str]] = [("all_raw", f"all:{query}")]
    if terms:
        all_terms = " AND ".join(f"all:{term}" for term in terms)
        title_abs_terms = " OR ".join([
            "(" + " AND ".join(f"ti:{term}" for term in terms[:5]) + ")",
            "(" + " AND ".join(f"abs:{term}" for term in terms[:5]) + ")",
        ])
        variants.extend([
            ("all_terms", all_terms),
            ("title_or_abstract_terms", title_abs_terms),
        ])
    quoted = [normalize_space(item) for item in re.findall(r'"([^"]{3,})"', query)]
    for phrase in quoted[:2]:
        variants.append((f"exact_phrase_{len(variants)}", f'all:"{phrase}"'))
    return variants


def arxiv_search(query: str, limit: int, timeout: float, pages: int) -> dict[str, Any]:
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    ns = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    per_page = max(1, min(limit, 100))
    variants = arxiv_query_variants(query)
    for strategy, search_query in variants:
        for page in range(max(1, pages)):
            params = urllib.parse.urlencode({
                "search_query": search_query,
                "start": page * per_page,
                "max_results": per_page,
                "sortBy": "relevance",
                "sortOrder": "descending",
            })
            url = f"https://export.arxiv.org/api/query?{params}"
            try:
                payload = fetch_text(url, timeout)
                root = ET.fromstring(payload)
            except (OSError, urllib.error.URLError, TimeoutError, ET.ParseError) as exc:
                errors.append(f"{strategy} page {page + 1}: {exc}")
                continue
            for rank, entry in enumerate(root.findall("atom:entry", ns), start=1 + page * per_page):
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
                    "search_strategy": strategy,
                    "provider_rank": rank,
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
                    "categories": categories,
                })
    return {
        "provider": "arxiv",
        "results": unique_results(results)[: max(1, limit)],
        "errors": errors,
        "diagnostics": {
            "strategy_count": len(variants),
            "strategies": [name for name, _ in variants],
            "pages_per_strategy": max(1, pages),
            "raw_result_count": len(results),
        },
    }


def semantic_scholar_search(query: str, limit: int, timeout: float, pages: int) -> dict[str, Any]:
    fields = ",".join([
        "title",
        "authors",
        "year",
        "abstract",
        "url",
        "venue",
        "citationCount",
        "referenceCount",
        "influentialCitationCount",
        "externalIds",
        "publicationDate",
        "publicationTypes",
        "fieldsOfStudy",
        "s2FieldsOfStudy",
        "journal",
        "isOpenAccess",
        "openAccessPdf",
        "tldr",
    ])
    headers: dict[str, str] = {}
    api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY")
    if api_key:
        headers["x-api-key"] = api_key
    per_page = max(1, min(limit, 100))
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    for page in range(max(1, pages)):
        params = urllib.parse.urlencode({
            "query": query,
            "limit": per_page,
            "offset": page * per_page,
            "fields": fields,
        })
        url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}"
        try:
            payload = fetch_text(url, timeout, headers=headers)
            data = json.loads(payload)
        except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            errors.append(f"page {page + 1}: {exc}")
            continue
        for rank, item in enumerate(data.get("data", []), start=1 + page * per_page):
            if not isinstance(item, dict):
                continue
            authors = [
                str(author.get("name", ""))
                for author in item.get("authors", [])
                if isinstance(author, dict) and author.get("name")
            ]
            external_ids = item.get("externalIds") if isinstance(item.get("externalIds"), dict) else {}
            open_access_pdf = item.get("openAccessPdf") if isinstance(item.get("openAccessPdf"), dict) else {}
            tldr = item.get("tldr") if isinstance(item.get("tldr"), dict) else {}
            journal = item.get("journal") if isinstance(item.get("journal"), dict) else {}
            results.append({
                "provider": "semantic_scholar",
                "provider_rank": rank,
                "title": normalize_space(item.get("title")),
                "url": item.get("url") or "",
                "year": item.get("year"),
                "authors": authors,
                "abstract": normalize_space(item.get("abstract")),
                "source_id": item.get("paperId") or external_ids.get("DOI") or "",
                "venue": item.get("venue") or normalize_space(journal.get("name")),
                "citation_count": item.get("citationCount"),
                "reference_count": item.get("referenceCount"),
                "influential_citation_count": item.get("influentialCitationCount"),
                "external_ids": external_ids,
                "publication_date": item.get("publicationDate"),
                "publication_types": item.get("publicationTypes") or [],
                "fields_of_study": item.get("fieldsOfStudy") or [],
                "s2_fields_of_study": item.get("s2FieldsOfStudy") or [],
                "is_open_access": item.get("isOpenAccess"),
                "open_access_pdf": open_access_pdf,
                "open_access_pdf_url": open_access_pdf.get("url") or "",
                "tldr": normalize_space(tldr.get("text")),
            })
    return {
        "provider": "semantic_scholar",
        "results": unique_results(results)[: max(1, limit)],
        "errors": errors,
        "diagnostics": {
            "strategy_count": 1,
            "strategies": ["paper_search_offset_pages"],
            "pages_per_strategy": max(1, pages),
            "raw_result_count": len(results),
            "api_key_used": bool(api_key),
        },
    }


def crossref_query_variants(query: str) -> list[tuple[str, dict[str, str]]]:
    variants = [
        ("bibliographic", {"query.bibliographic": query}),
        ("general", {"query": query}),
    ]
    terms = search_terms(query, max_terms=7)
    if terms:
        variants.append(("title_terms", {"query.title": " ".join(terms)}))
    quoted = [normalize_space(item) for item in re.findall(r'"([^"]{3,})"', query)]
    for phrase in quoted[:2]:
        variants.append((f"title_exact_{len(variants)}", {"query.title": phrase}))
    return variants


def crossref_search(query: str, limit: int, timeout: float, pages: int) -> dict[str, Any]:
    errors: list[str] = []
    results: list[dict[str, Any]] = []
    variants = crossref_query_variants(query)
    mailto = os.environ.get("CROSSREF_MAILTO")
    per_page = max(1, min(limit, 100))
    for strategy, query_params in variants:
        for page in range(max(1, pages)):
            params_dict = {
                **query_params,
                "rows": str(per_page),
                "offset": str(page * per_page),
                "sort": "relevance",
                "order": "desc",
                "select": (
                    "DOI,title,author,published-print,published-online,published,"
                    "container-title,URL,is-referenced-by-count,type,abstract,subject,"
                    "reference-count,ISSN,ISBN,publisher"
                ),
            }
            if mailto:
                params_dict["mailto"] = mailto
            params = urllib.parse.urlencode(params_dict)
            url = f"https://api.crossref.org/works?{params}"
            try:
                payload = fetch_text(url, timeout)
                data = json.loads(payload)
            except (OSError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                errors.append(f"{strategy} page {page + 1}: {exc}")
                continue

            items = data.get("message", {}).get("items", [])
            for rank, item in enumerate(items, start=1 + page * per_page):
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
                year = (
                    year_from_parts(item.get("published-print"))
                    or year_from_parts(item.get("published-online"))
                    or year_from_parts(item.get("published"))
                )
                doi = item.get("DOI") or ""
                results.append({
                    "provider": "crossref",
                    "search_strategy": strategy,
                    "provider_rank": rank,
                    "title": normalize_space(title[0] if title else ""),
                    "url": item.get("URL") or (f"https://doi.org/{doi}" if doi else ""),
                    "year": year,
                    "authors": authors,
                    "abstract": strip_markup(item.get("abstract")),
                    "source_id": doi,
                    "venue": normalize_space(container[0] if container else ""),
                    "citation_count": item.get("is-referenced-by-count"),
                    "reference_count": item.get("reference-count"),
                    "type": item.get("type") or "",
                    "subjects": item.get("subject") or [],
                    "publisher": item.get("publisher") or "",
                    "issn": item.get("ISSN") or [],
                    "isbn": item.get("ISBN") or [],
                })
    return {
        "provider": "crossref",
        "results": unique_results(results)[: max(1, limit)],
        "errors": errors,
        "diagnostics": {
            "strategy_count": len(variants),
            "strategies": [name for name, _ in variants],
            "pages_per_strategy": max(1, pages),
            "raw_result_count": len(results),
            "mailto_used": bool(mailto),
        },
    }


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
                for field in [
                    "citation_count",
                    "reference_count",
                    "influential_citation_count",
                    "pdf_url",
                    "source_url",
                    "open_access_pdf_url",
                    "external_ids",
                ]:
                    if merged[key].get(field) is None or not merged[key].get(field):
                        if item.get(field) is not None and item.get(field) != "":
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
    if item.get("open_access_pdf_url"):
        lines.append(f"- Open-access PDF: {item['open_access_pdf_url']}")
    if item.get("source_url"):
        lines.append(f"- arXiv source: {item['source_url']}")
    external_ids = item.get("external_ids") if isinstance(item.get("external_ids"), dict) else {}
    doi = external_ids.get("DOI") or (item.get("source_id") if item.get("provider") == "crossref" else "")
    arxiv_id = external_ids.get("ArXiv") or (item.get("source_id") if item.get("provider") == "arxiv" else "")
    if doi:
        lines.append(f"- DOI: {doi}")
    if arxiv_id:
        lines.append(f"- arXiv id: {arxiv_id}")
    if item.get("citation_count") is not None:
        lines.append(f"- Citation count: {item['citation_count']}")
    if item.get("reference_count") is not None:
        lines.append(f"- Reference count: {item['reference_count']}")
    if item.get("influential_citation_count") is not None:
        lines.append(f"- Influential citation count: {item['influential_citation_count']}")
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
        diagnostics = run.get("diagnostics") if isinstance(run.get("diagnostics"), dict) else {}
        diagnostic_bits = []
        if diagnostics:
            if diagnostics.get("strategy_count") is not None:
                diagnostic_bits.append(f"strategies={diagnostics['strategy_count']}")
            if diagnostics.get("pages_per_strategy") is not None:
                diagnostic_bits.append(f"pages={diagnostics['pages_per_strategy']}")
            if diagnostics.get("raw_result_count") is not None:
                diagnostic_bits.append(f"raw_results={diagnostics['raw_result_count']}")
        suffix = "; " + "; ".join(diagnostic_bits) if diagnostic_bits else ""
        lines.append(f"- {run['provider']}: {status}; results={len(run.get('results', []))}{suffix}")
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
    parser.add_argument(
        "--pages",
        type=int,
        default=2,
        help="External-provider pages per query strategy. Local search ignores this.",
    )
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


def run_provider(provider: str, query: str, limit: int, timeout: float, pages: int) -> dict[str, Any]:
    if provider == "local":
        return local_search(query, limit)
    if provider == "arxiv":
        return arxiv_search(query, limit, timeout, pages)
    if provider == "semantic_scholar":
        return semantic_scholar_search(query, limit, timeout, pages)
    if provider == "crossref":
        return crossref_search(query, limit, timeout, pages)
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
                run = run_provider(provider, query, max(1, args.limit), args.timeout, max(1, args.pages))
                run["query"] = query
                provider_runs.append(run)
        merged = merge_results(provider_runs)
        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "queries": args.query,
            "providers": providers,
            "limit_per_provider_run": max(1, args.limit),
            "external_pages_per_strategy": max(1, args.pages),
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
