#!/usr/bin/env python3
from __future__ import annotations

import argparse

from lib import (
    ValidationError,
    discover_default_source_roots,
    source_guard_errors,
    source_manifest_path,
    write_source_manifest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize or refresh the guarded paper-source manifest."
    )
    parser.add_argument(
        "--root",
        action="append",
        default=[],
        help="Guarded source root relative to comath-codex, for example '../Paper/files'.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing manifest after checking current guarded sources are clean.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    path = source_manifest_path()
    if path.exists() and not args.force:
        print(f"Refusing to overwrite existing source manifest: {path}")
        print("Use --force after you intentionally accept the current source tree as baseline.")
        return 1

    if path.exists() and args.force:
        errors = source_guard_errors()
        if errors:
            print("Refusing to refresh source manifest while guarded source drift is present:")
            for error in errors:
                print(f"- {error}")
            return 1

    roots = args.root or discover_default_source_roots()
    if not roots:
        print("No guarded LaTeX source roots discovered. Pass --root explicitly.")
        return 1

    try:
        manifest = write_source_manifest(roots)
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Wrote {path}")
    print("Guarded roots:")
    for root in manifest["guarded_roots"]:
        print(f"- {root}")
    print(f"Tracked source files: {len(manifest['files'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
