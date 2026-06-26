#!/usr/bin/env python3
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import re
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib import ValidationError, read_json, repo_root, write_json


USER_AGENT = "comath-codex-arxiv-source-extract/1.0"
BASE_ENV_KINDS = {
    "theorem": "theorem",
    "thm": "theorem",
    "lemma": "lemma",
    "lem": "lemma",
    "proposition": "proposition",
    "prop": "proposition",
    "corollary": "corollary",
    "cor": "corollary",
    "definition": "definition",
    "defn": "definition",
    "def": "definition",
    "assumption": "assumption",
    "assumptions": "assumption",
    "condition": "condition",
    "conditions": "condition",
    "remark": "remark",
    "rem": "remark",
    "example": "example",
}
EQUATION_ENVS = {
    "equation",
    "align",
    "gather",
    "multline",
    "eqnarray",
    "flalign",
    "alignat",
}


def safe_name(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    value = re.sub(r"_+", "_", value).strip("_")
    return value or "source"


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch_bytes(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


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


def arxiv_ids_from_results(results: dict[str, Any]) -> list[dict[str, str]]:
    papers: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in results.get("merged_results", []):
        if not isinstance(item, dict):
            continue
        source_url = str(item.get("source_url") or "")
        source_id = str(item.get("source_id") or "")
        provider = str(item.get("provider") or "")
        providers = item.get("providers", [])
        is_arxiv = provider == "arxiv" or (
            isinstance(providers, list) and "arxiv" in providers
        )
        if not is_arxiv and "export.arxiv.org/e-print/" not in source_url:
            continue
        arxiv_id = (source_id or source_url.rsplit("/", 1)[-1]).strip()
        if not arxiv_id or arxiv_id in seen:
            continue
        seen.add(arxiv_id)
        papers.append({
            "arxiv_id": arxiv_id,
            "title": str(item.get("title") or ""),
            "source_url": source_url or f"https://export.arxiv.org/e-print/{arxiv_id}",
        })
    return papers


def safe_extract_tar(data: bytes, destination: Path) -> list[str]:
    extracted: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as archive:
        for member in archive.getmembers():
            target = (destination / member.name).resolve()
            try:
                target.relative_to(destination.resolve())
            except ValueError as exc:
                raise ValidationError(f"Unsafe tar member path: {member.name}") from exc
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            handle = archive.extractfile(member)
            if handle is None:
                continue
            target.write_bytes(handle.read())
            extracted.append(str(target.relative_to(destination)))
    return extracted


def unpack_arxiv_source(data: bytes, destination: Path) -> tuple[str, list[str]]:
    destination.mkdir(parents=True, exist_ok=True)
    try:
        extracted = safe_extract_tar(data, destination)
        return "tar", extracted
    except (tarfile.TarError, EOFError):
        pass

    try:
        text_bytes = gzip.decompress(data)
        mode = "gzip-single-file"
    except OSError:
        text_bytes = data
        mode = "single-file"
    output = destination / "source.tex"
    output.write_bytes(text_bytes)
    return mode, [output.name]


def strip_latex_comments(text: str) -> str:
    cleaned_lines: list[str] = []
    for line in text.splitlines():
        escaped = False
        kept: list[str] = []
        for char in line:
            if char == "%" and not escaped:
                break
            kept.append(char)
            if char == "\\" and not escaped:
                escaped = True
            else:
                escaped = False
        cleaned_lines.append("".join(kept))
    return "\n".join(cleaned_lines)


def normalize_statement(text: str) -> str:
    text = re.sub(r"\\label\{[^{}]+\}", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def line_number(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def find_environment_end(text: str, env: str, start: int) -> int | None:
    match = re.search(rf"\\end\{{{re.escape(env)}\}}", text[start:])
    if match is None:
        return None
    return start + match.start()


def kind_from_title(value: str) -> str:
    lowered = normalize_statement(value).lower()
    for token, kind in [
        ("theorem", "theorem"),
        ("lemma", "lemma"),
        ("proposition", "proposition"),
        ("corollary", "corollary"),
        ("definition", "definition"),
        ("assumption", "assumption"),
        ("condition", "condition"),
        ("remark", "remark"),
        ("example", "example"),
    ]:
        if token in lowered:
            return kind
    return "theorem_like"


def theorem_env_map(text: str) -> dict[str, str]:
    envs = dict(BASE_ENV_KINDS)
    newtheorem_re = re.compile(
        r"\\newtheorem\*?\s*\{(?P<env>[A-Za-z][A-Za-z0-9*_-]*)\}"
        r"(?:\[[^\]]+\])?"
        r"\s*\{(?P<title>[^{}]+)\}",
        re.IGNORECASE,
    )
    for match in newtheorem_re.finditer(text):
        env = match.group("env").rstrip("*").lower()
        title = match.group("title")
        envs[env] = kind_from_title(title)
    return envs


def extract_from_tex(path: Path, root: Path, arxiv_id: str) -> list[dict[str, Any]]:
    try:
        original_text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    text = strip_latex_comments(original_text)
    env_map = theorem_env_map(text)
    begin_re = re.compile(
        r"\\begin\{(?P<env>[A-Za-z][A-Za-z0-9*_-]*)\}"
        r"(?:\[(?P<title>(?:[^\]\\]|\\.)*)\])?",
        re.IGNORECASE,
    )
    rel_path = str(path.relative_to(root))
    results: list[dict[str, Any]] = []
    for index, match in enumerate(begin_re.finditer(text), start=1):
        env = match.group("env")
        base_env = env.rstrip("*").lower()
        body_end = find_environment_end(text, env, match.end())
        if body_end is None:
            continue
        body = text[match.end():body_end]
        label_match = re.search(r"\\label\{([^{}]+)\}", body)
        label = label_match.group(1) if label_match else ""
        if base_env in env_map:
            kind = env_map[base_env]
            statement_id = f"arxiv:{arxiv_id}:{rel_path}:{kind}:{index}"
            results.append({
                "statement_id": statement_id,
                "arxiv_id": arxiv_id,
                "source_file": rel_path,
                "environment": base_env,
                "kind": kind,
                "title": normalize_statement(match.group("title") or ""),
                "label": label,
                "line": line_number(text, match.start()),
                "statement": normalize_statement(body),
            })
        elif base_env in EQUATION_ENVS or base_env.rstrip("*") in EQUATION_ENVS:
            if not label:
                continue
            statement_id = f"arxiv:{arxiv_id}:{rel_path}:equation:{index}"
            results.append({
                "statement_id": statement_id,
                "arxiv_id": arxiv_id,
                "source_file": rel_path,
                "environment": base_env,
                "kind": "equation",
                "title": "",
                "label": label,
                "line": line_number(text, match.start()),
                "statement": normalize_statement(body),
            })
    return results


def extract_theorem_statements(source_root: Path, arxiv_id: str) -> list[dict[str, Any]]:
    statements: list[dict[str, Any]] = []
    for path in sorted(source_root.rglob("*.tex")):
        statements.extend(extract_from_tex(path, source_root, arxiv_id))
    return statements


def source_root_for_manifest(path: Path, workstream: Path) -> str:
    try:
        return str(path.relative_to(workstream))
    except ValueError:
        return str(path)


def write_markdown(path: Path, statements: list[dict[str, Any]], failures: list[dict[str, str]]) -> None:
    lines = [
        "# Extracted Theorem Statements",
        "",
        f"Generated at: `{datetime.now(timezone.utc).isoformat()}`",
        "",
    ]
    if failures:
        lines.extend(["## Source Download Failures", ""])
        for failure in failures:
            lines.append(f"- `{failure['arxiv_id']}`: {failure['error']}")
        lines.append("")
    if not statements:
        lines.append("No theorem-like, definition, assumption, or labeled equation environments were extracted.")
    for item in statements:
        lines.extend([
            f"## {str(item.get('kind') or item['environment']).title()}",
            "",
            f"- source_statement_id: `{item['statement_id']}`",
            f"- arXiv id: `{item['arxiv_id']}`",
            f"- source file: `{item['source_file']}`",
            f"- line: `{item['line']}`",
            f"- environment: `{item['environment']}`",
            f"- label: `{item['label'] or 'not available'}`",
            f"- title: `{item['title'] or 'not available'}`",
            "",
            "```tex",
            item["statement"],
            "```",
            "",
        ])
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download arXiv source archives and extract theorem/lemma/proposition LaTeX environments."
    )
    parser.add_argument("--workstream", required=True, help="Path like workstreams/<id>.")
    parser.add_argument("--results", default="artifacts/literature_search_results.json")
    parser.add_argument("--arxiv-id", action="append", default=[])
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    try:
        workstream = resolve_workstream(args.workstream)
        results_path = workstream / args.results
        search_results = read_json(results_path) if results_path.exists() else {}
        papers = arxiv_ids_from_results(search_results)
        for arxiv_id in args.arxiv_id:
            if not any(item["arxiv_id"] == arxiv_id for item in papers):
                papers.append({
                    "arxiv_id": arxiv_id,
                    "title": "",
                    "source_url": f"https://export.arxiv.org/e-print/{arxiv_id}",
                })
        papers = papers[: max(0, args.limit)]

        if args.dry_run:
            temp_dir = tempfile.TemporaryDirectory(prefix="comath_arxiv_extract_")
            base = Path(temp_dir.name) / "arxiv"
        else:
            base = workstream / "artifacts" / "literature_sources" / "arxiv"

        all_statements: list[dict[str, Any]] = []
        source_manifest: list[dict[str, Any]] = []
        failures: list[dict[str, str]] = []
        for paper in papers:
            arxiv_id = paper["arxiv_id"]
            source_url = paper["source_url"]
            target = base / safe_name(arxiv_id)
            try:
                data = fetch_bytes(source_url, args.timeout)
                mode, files = unpack_arxiv_source(data, target / "source")
                statements = extract_theorem_statements(target / "source", arxiv_id)
                all_statements.extend(statements)
                source_manifest.append({
                    "arxiv_id": arxiv_id,
                    "title": paper.get("title", ""),
                    "source_url": source_url,
                    "source_sha256": sha256_bytes(data),
                    "mode": mode,
                    "source_root": source_root_for_manifest(target / "source", workstream),
                    "files": files,
                    "extracted_statement_count": len(statements),
                    "status": "ok",
                })
            except (OSError, urllib.error.URLError, TimeoutError, ValidationError) as exc:
                failures.append({"arxiv_id": arxiv_id, "error": str(exc)})
                source_manifest.append({
                    "arxiv_id": arxiv_id,
                    "title": paper.get("title", ""),
                    "source_url": source_url,
                    "status": "error",
                    "error": str(exc),
                })

        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "generated_by": "scripts/arxiv_source_extract.py",
            "source_manifest": source_manifest,
            "statements": all_statements,
            "failures": failures,
        }
        if args.dry_run:
            print(json.dumps(payload, indent=2, sort_keys=False))
            return 0

        json_path = workstream / "artifacts" / "extracted_theorem_statements.json"
        md_path = workstream / "artifacts" / "extracted_theorem_statements.md"
        manifest_path = workstream / "artifacts" / "literature_sources" / "source_manifest.json"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        write_json(json_path, payload)
        write_json(manifest_path, {"schema_version": 1, "sources": source_manifest})
        write_markdown(md_path, all_statements, failures)
    except (ValidationError, ValueError) as exc:
        print(exc)
        return 1
    finally:
        if temp_dir is not None:
            temp_dir.cleanup()

    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print(f"Wrote {manifest_path}")
    print(f"Extracted statements: {len(all_statements)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
