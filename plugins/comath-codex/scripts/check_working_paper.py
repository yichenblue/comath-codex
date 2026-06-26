#!/usr/bin/env python3
from __future__ import annotations

import argparse

from lib import (
    ValidationError,
    approved_claim_keys,
    approved_claims_tex_path,
    claim_registry_path,
    latex_approved_claim_keys,
    load_claim_registry,
    working_paper_tex_path,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate working paper claim provenance.")
    parser.add_argument(
        "--allow-tbd",
        action="store_true",
        help="Allow unresolved TBD markers in working_paper.tex.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    errors: list[str] = []

    try:
        registry = load_claim_registry()
    except ValidationError as exc:
        print(exc)
        return 1

    registry_keys = approved_claim_keys(registry)
    if len(registry_keys) != len(registry.get("claims", [])):
        errors.append(f"{claim_registry_path()}: every registry claim must have status approved and a claim_key")

    paper = working_paper_tex_path()
    if not paper.exists():
        errors.append(f"Missing working paper: {paper}")
    else:
        paper_text = paper.read_text(encoding="utf-8")
        if "\\input{approved_claims.tex}" not in paper_text:
            errors.append(f"{paper}: must include \\input{{approved_claims.tex}}")
        if "TBD" in paper_text and not args.allow_tbd:
            errors.append(f"{paper}: contains unresolved TBD markers")

    approved_fragment = approved_claims_tex_path()
    if not approved_fragment.exists():
        errors.append(f"Missing approved claims fragment: {approved_fragment}")

    referenced_keys: list[str] = []
    referenced_keys.extend(latex_approved_claim_keys(paper))
    referenced_keys.extend(latex_approved_claim_keys(approved_fragment))
    missing = sorted(set(referenced_keys) - registry_keys)
    if missing:
        errors.append(f"Working paper references claim keys not approved in registry: {missing}")

    unused = sorted(registry_keys - set(referenced_keys))
    if registry_keys and unused:
        errors.append(f"Approved registry claims not referenced by working paper fragments: {unused}")

    if errors:
        print("Working paper gate failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    print("Working paper gate passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

