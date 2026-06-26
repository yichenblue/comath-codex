#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import ValidationError, repo_root, write_json


STOPWORDS = {
    "about",
    "above",
    "after",
    "again",
    "against",
    "also",
    "among",
    "analysis",
    "and",
    "appendix",
    "are",
    "because",
    "before",
    "between",
    "can",
    "check",
    "current",
    "derive",
    "does",
    "each",
    "equiv",
    "from",
    "give",
    "have",
    "into",
    "label",
    "lemma",
    "paper",
    "proof",
    "result",
    "section",
    "show",
    "source",
    "task",
    "term",
    "terms",
    "that",
    "the",
    "theorem",
    "their",
    "then",
    "there",
    "thm",
    "this",
    "train",
    "using",
    "with",
}

TECHNIQUE_PHRASES = [
    "deterministic equivalent",
    "deterministic equivalence",
    "resolvent deterministic equivalent",
    "two point deterministic equivalent",
    "two point resolvent",
    "linear spectral statistics",
    "weighted spectral measure",
    "stieltjes transform",
    "cauchy stieltjes transform",
    "marchenko pastur",
    "random feature",
    "random features",
    "random matrix",
    "high dimensional",
    "gradient descent",
    "stochastic gradient descent",
    "volterra equation",
    "contour integral",
    "asymptotic scaling",
    "scaling law",
    "kernel regression",
    "neural tangent kernel",
    "hermite expansion",
    "gaussian equivalent",
    "spiked covariance",
    "low rank perturbation",
    "woodbury",
    "resolvent",
    "ridge",
]

ALTERNATES = {
    "deterministic equivalent": [
        "deterministic approximation",
        "equivalent deterministic resolvent",
        "asymptotic equivalent",
    ],
    "stieltjes transform": [
        "cauchy transform",
        "resolvent trace",
        "limiting spectral distribution",
    ],
    "weighted spectral measure": [
        "eigenvector overlap measure",
        "weighted empirical spectral distribution",
        "spectral measure with weights",
    ],
    "random feature": [
        "random features model",
        "random kitchen sinks",
        "random feature regression",
    ],
    "gradient descent": [
        "training dynamics",
        "least squares gradient flow",
        "discrete gradient descent",
    ],
    "scaling law": [
        "power law",
        "learning curve asymptotics",
        "late time asymptotics",
    ],
}


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def strip_latex(value: str) -> str:
    value = re.sub(r"\\label\{([^{}]+)\}", r" \1 ", value)
    value = re.sub(r"\\cite[A-Za-z*]*\{([^{}]+)\}", r" \1 ", value)
    value = re.sub(r"\\[A-Za-z]+\*?(?:\[[^\]]*\])?(?:\{([^{}]*)\})?", r" \1 ", value)
    value = re.sub(r"[_{}^$&%#~]", " ", value)
    return normalize_space(value)


def resolve_existing_path(raw: str) -> Path | None:
    path = Path(raw)
    candidates = [path] if path.is_absolute() else [repo_root() / path, repo_root().parent / path]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return None


def read_source_context(paths: list[str], max_chars: int) -> str:
    chunks: list[str] = []
    used = 0
    for raw in paths:
        path = resolve_existing_path(raw)
        if path is None or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        chunks.append(text[:remaining])
        used += min(len(text), remaining)
    return "\n\n".join(chunks)


def extract_bib_keys(text: str) -> list[str]:
    keys: list[str] = []
    for match in re.finditer(r"\\cite[A-Za-z*]*\{([^{}]+)\}", text):
        keys.extend(item.strip() for item in match.group(1).split(",") if item.strip())
    return dedupe(keys)


def extract_labels(text: str) -> list[str]:
    labels = re.findall(r"\\label\{([^{}]+)\}", text)
    labels.extend(
        re.findall(
            r"\b(?:lem|lemma|prop|proposition|thm|theorem|cor|eq|equation|def|definition):"
            r"[A-Za-z0-9_.:-]+\b",
            text,
        )
    )
    return dedupe(labels)


def label_words(label: str) -> list[str]:
    tail = label.split(":", 1)[-1]
    return [
        item.lower()
        for item in re.split(r"[_\-.:\s]+", tail)
        if len(item) >= 3 and item.lower() not in STOPWORDS
    ]


def extract_phrases(text: str) -> list[str]:
    lowered = strip_latex(text).lower()
    hits = [phrase for phrase in TECHNIQUE_PHRASES if phrase in lowered]
    return dedupe(hits)


def extract_keywords(text: str, limit: int) -> list[str]:
    cleaned = strip_latex(text).lower()
    tokens = re.findall(r"[a-z][a-z0-9-]{2,}", cleaned)
    counts: dict[str, int] = {}
    for token in tokens:
        token = token.strip("-")
        if len(token) < 3 or token in STOPWORDS:
            continue
        counts[token] = counts.get(token, 0) + 1
    ranked = sorted(counts, key=lambda key: (-counts[key], key))
    return ranked[:limit]


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        normalized = normalize_space(str(item))
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def compact_query(parts: list[str], max_terms: int = 8) -> str:
    cleaned = dedupe([part for part in parts if part])
    return " ".join(cleaned[:max_terms])


def query_record(query: str, reason: str, priority: str, providers: list[str]) -> dict[str, Any]:
    return {
        "query": normalize_space(query),
        "reason": reason,
        "priority": priority,
        "provider_hints": providers,
    }


def build_query_families(
    objective: str,
    labels: list[str],
    source_text: str,
    max_queries: int,
) -> list[dict[str, Any]]:
    combined = "\n".join([objective, source_text, " ".join(labels)])
    phrases = extract_phrases(combined)
    keywords = extract_keywords(combined, limit=24)
    label_terms = dedupe([word for label in labels for word in label_words(label)])
    bib_keys = extract_bib_keys(combined)

    if not phrases:
        phrases = keywords[:4]

    families: list[dict[str, Any]] = []

    core_queries = [
        query_record(
            compact_query(phrases[:3] + keywords[:5]),
            "Broad query combining the most frequent technical phrases and keywords.",
            "high",
            ["arxiv", "semantic_scholar", "crossref", "local"],
        )
    ]
    if objective:
        core_queries.append(
            query_record(
                compact_query(extract_keywords(objective, limit=10), max_terms=10),
                "Objective-only query to avoid overfitting to source-context terminology.",
                "high",
                ["semantic_scholar", "arxiv"],
            )
        )
    families.append({
        "family": "core_problem",
        "reason": "Find papers directly matching the assigned mathematical problem.",
        "queries": [item for item in core_queries if item["query"]],
    })

    method_queries: list[dict[str, Any]] = []
    for phrase in phrases[:8]:
        context = [phrase]
        for anchor in ["random matrix", "learning theory", "high dimensional", "asymptotic"]:
            method_queries.append(
                query_record(
                    compact_query(context + [anchor]),
                    f"Method query for technical phrase `{phrase}` with adjacent field `{anchor}`.",
                    "high" if anchor in {"random matrix", "learning theory"} else "medium",
                    ["arxiv", "semantic_scholar"],
                )
            )
    families.append({
        "family": "method_and_technique",
        "reason": "Search by proof technique and mathematical method rather than by the exact problem wording.",
        "queries": method_queries[:max_queries],
    })

    object_queries: list[dict[str, Any]] = []
    object_terms = dedupe(label_terms + [kw for kw in keywords if kw in {"kernel", "resolvent", "volterra", "ridge", "feature", "features", "sgd", "loss"}])
    for term in object_terms[:8]:
        object_queries.append(
            query_record(
                compact_query([term, "random features", "deterministic equivalent"]),
                f"Object-level query centered on `{term}`.",
                "medium",
                ["arxiv", "semantic_scholar", "local"],
            )
        )
    families.append({
        "family": "objects_and_models",
        "reason": "Search by the mathematical object, model class, or target label terminology.",
        "queries": object_queries[:max_queries],
    })

    theorem_queries: list[dict[str, Any]] = []
    for phrase in phrases[:6]:
        theorem_queries.extend([
            query_record(
                compact_query([phrase, "theorem", "assumption"]),
                f"Exact-statement query for theorems and assumptions around `{phrase}`.",
                "medium",
                ["arxiv", "semantic_scholar", "local"],
            ),
            query_record(
                compact_query([phrase, "lemma", "proof", "appendix"]),
                f"Appendix-level query for lemmas and proof details around `{phrase}`.",
                "medium",
                ["arxiv", "local"],
            ),
        ])
    families.append({
        "family": "exact_statements",
        "reason": "Find exact theorem, lemma, proposition, definition, and assumption statements for downstream applicability checks.",
        "queries": theorem_queries[:max_queries],
    })

    alternate_queries: list[dict[str, Any]] = []
    for phrase in phrases:
        for alternate in ALTERNATES.get(phrase, []):
            alternate_queries.append(
                query_record(
                    compact_query([alternate] + keywords[:4]),
                    f"Alternate-terminology query for `{phrase}`.",
                    "medium",
                    ["semantic_scholar", "arxiv", "crossref"],
                )
            )
    families.append({
        "family": "alternate_terminology",
        "reason": "Reduce missed papers caused by different terminology for the same technique.",
        "queries": alternate_queries[:max_queries],
    })

    citation_queries: list[dict[str, Any]] = []
    for key in bib_keys[:12]:
        citation_queries.append(
            query_record(
                compact_query([key] + phrases[:2] + keywords[:3]),
                f"Citation-key query anchored at `{key}`.",
                "high",
                ["local", "semantic_scholar", "crossref"],
            )
        )
    if citation_queries:
        families.append({
            "family": "citation_keys",
            "reason": "Search around bibliography keys already present in the source context.",
            "queries": citation_queries[:max_queries],
        })

    negative_queries = [
        query_record(
            compact_query(phrases[:2] + ["limitations", "counterexample"]),
            "Search for known limitations, counterexamples, or non-applicability results.",
            "low",
            ["semantic_scholar", "arxiv"],
        ),
        query_record(
            compact_query(phrases[:2] + ["failure", "assumption"]),
            "Search for assumption failures or boundary cases.",
            "low",
            ["semantic_scholar", "arxiv"],
        ),
    ]
    families.append({
        "family": "negative_and_boundary_cases",
        "reason": "Find pitfalls and conditions under which adjacent theorems do not apply.",
        "queries": [item for item in negative_queries if item["query"]],
    })

    for family in families:
        family["queries"] = dedupe_query_records(family["queries"])[:max_queries]
    return [family for family in families if family["queries"]]


def dedupe_query_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for record in records:
        query = normalize_space(str(record.get("query", "")))
        key = query.lower()
        if not query or key in seen:
            continue
        seen.add(key)
        item = dict(record)
        item["query"] = query
        result.append(item)
    return result


def markdown_plan(payload: dict[str, Any]) -> str:
    lines = [
        "# Search Plan",
        "",
        f"Generated at: `{payload['generated_at']}`",
        "",
        "## Search Queries",
        "",
    ]
    for family in payload["query_families"]:
        lines.extend([f"### {family['family']}", "", family["reason"], ""])
        for item in family["queries"]:
            providers = ", ".join(item["provider_hints"])
            lines.append(f"- `{item['query']}`")
            lines.append(f"  - Reason: {item['reason']}")
            lines.append(f"  - Priority: {item['priority']}")
            lines.append(f"  - Provider hints: {providers}")
        lines.append("")

    lines.extend([
        "## Rationale",
        "",
        "The query plan separates direct problem wording from method names, object/model terms, exact-statement searches, alternate terminology, citation-key anchors, and negative or boundary-case searches. This is intended to improve recall before downstream theorem applicability checks.",
        "",
        "## Search Coverage",
        "",
        "- Planned providers: local, arXiv, Semantic Scholar, Crossref.",
        "- Run high-priority core and method queries first.",
        "- Use alternate-terminology and citation-key queries as follow-ups when initial results are narrow.",
        "- Record unavailable providers or failed searches in `artifacts/literature_search_results.json` and `artifacts/followup_queries.md`.",
        "",
        "## Suggested Command",
        "",
        "```text",
        payload["suggested_literature_search_command"],
        "```",
    ])
    return "\n".join(lines).rstrip() + "\n"


def suggested_command(workstream: str | None, families: list[dict[str, Any]], max_command_queries: int) -> str:
    queries: list[str] = []
    for family in families:
        for item in family["queries"]:
            if item.get("priority") in {"high", "medium"}:
                queries.append(item["query"])
    queries = dedupe(queries)[:max_command_queries]
    workstream_arg = workstream or "workstreams/<workstream-id>"
    parts = ["python3 scripts/literature_search.py", f"  --workstream {workstream_arg}"]
    for query in queries:
        escaped = query.replace('"', '\\"')
        parts.append(f'  --query "{escaped}"')
    return " \\\n".join(parts)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate broad literature-search query families for a literature workstream."
    )
    parser.add_argument("--workstream", help="Path like workstreams/<id>. Required unless --dry-run is used.")
    parser.add_argument("--objective", required=True)
    parser.add_argument("--target-label", action="append", default=[])
    parser.add_argument("--source-context", action="append", default=[])
    parser.add_argument("--max-source-chars", type=int, default=200_000)
    parser.add_argument("--max-queries-per-family", type=int, default=12)
    parser.add_argument("--max-command-queries", type=int, default=12)
    parser.add_argument("--json-output", default="artifacts/search_plan.json")
    parser.add_argument("--markdown-output", default="artifacts/search_plan.md")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if not args.workstream and not args.dry_run:
            raise ValidationError("--workstream is required unless --dry-run is set")

        source_text = read_source_context(args.source_context, args.max_source_chars)
        labels = dedupe(list(args.target_label) + extract_labels(args.objective) + extract_labels(source_text))
        families = build_query_families(
            args.objective,
            labels,
            source_text,
            max(1, args.max_queries_per_family),
        )
        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": "scripts/plan_literature_queries.py",
            "objective": args.objective,
            "target_labels": labels,
            "source_contexts": args.source_context,
            "extracted_bib_keys": extract_bib_keys("\n".join([args.objective, source_text])),
            "extracted_technical_phrases": extract_phrases("\n".join([args.objective, source_text])),
            "query_families": families,
            "suggested_literature_search_command": suggested_command(
                args.workstream,
                families,
                max(1, args.max_command_queries),
            ),
        }
        if args.dry_run:
            print(json.dumps(payload, indent=2, sort_keys=False))
            return 0

        workstream = resolve_existing_path(args.workstream)
        if workstream is None:
            raise ValidationError(f"Workstream does not exist: {args.workstream}")
        workstream.relative_to(repo_root() / "workstreams")
        json_path = workstream / args.json_output
        markdown_path = workstream / args.markdown_output
        json_path.parent.mkdir(parents=True, exist_ok=True)
        markdown_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(json_path, payload)
        markdown_path.write_text(markdown_plan(payload), encoding="utf-8")
    except (ValidationError, ValueError) as exc:
        print(exc)
        return 1

    print(f"Wrote {json_path}")
    print(f"Wrote {markdown_path}")
    print(f"Query families: {len(families)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
