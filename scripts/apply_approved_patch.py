#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from lib import (
    ValidationError,
    append_message,
    file_sha256,
    guarded_source_file,
    load_status,
    readiness_errors,
    repo_root,
    resolve_guarded_source_root,
    scaffold_lock,
    source_guard_errors,
    source_manifest_path,
    source_manifest_roots,
    status_path,
    write_json,
    write_source_manifest,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Apply a proposed paper-source patch only after workstream readiness gates pass."
    )
    parser.add_argument("workstream", help="Path like workstreams/<workstream-id>.")
    parser.add_argument(
        "--patch",
        default="artifacts/source_patches/proposed_source_patch.diff",
        help="Patch path relative to the workstream, unless absolute.",
    )
    parser.add_argument(
        "--strip",
        type=int,
        default=0,
        help="Patch strip level passed to patch -pN. Proposed patches should normally use -p0.",
    )
    parser.add_argument(
        "--cwd",
        choices=["workspace", "repo"],
        default="workspace",
        help="Apply patch from the workspace root or from comath-codex.",
    )
    parser.add_argument(
        "--attestation-only",
        action="store_true",
        help=(
            "Ratify an already-applied quarantined legacy source patch without "
            "running patch. Requires clean guarded source, passing readiness "
            "gates except source_patch_applied, and an attestation artifact."
        ),
    )
    parser.add_argument(
        "--attestation",
        default="artifacts/source_patches/source_patch_attestation.md",
        help="Attestation path relative to the workstream, unless absolute.",
    )
    return parser.parse_args()


def strip_patch_prefix(token: str) -> str | None:
    token = token.strip().split("\t", 1)[0]
    if token == "/dev/null":
        return None
    if token.startswith("a/") or token.startswith("b/"):
        token = token[2:]
    return token


def strip_patch_path_components(token: str, strip: int) -> Path | None:
    token = token.strip().split("\t", 1)[0]
    if token == "/dev/null":
        return None
    path = Path(token)
    if path.is_absolute():
        if strip == 0:
            return path
        parts = path.parts[1:]
    else:
        parts = path.parts
    if strip:
        if len(parts) <= strip:
            raise ValidationError(f"Patch target {token!r} has fewer than {strip} components to strip")
        parts = parts[strip:]
    if not parts:
        raise ValidationError(f"Patch target {token!r} resolved to an empty path after -p{strip}")
    return Path(*parts)


def patch_targets(patch_path: Path) -> list[str]:
    targets: list[str] = []
    for line in patch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("+++ ") or line.startswith("--- "):
            token = strip_patch_prefix(line[4:])
            if token and token not in targets:
                targets.append(token)
    return targets


def is_git_style_patch(patch_path: Path) -> bool:
    for line in patch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("diff --git "):
            return True
    return False


def target_candidates(token: str) -> list[Path]:
    raw = Path(token)
    if raw.is_absolute():
        return [raw.resolve()]
    workspace = repo_root().parent.resolve()
    return [
        (workspace / raw).resolve(),
        (repo_root() / raw).resolve(),
    ]


def patch_target_paths(patch_path: Path, cwd: Path, strip: int) -> list[Path]:
    targets: list[Path] = []
    for line in patch_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not (line.startswith("+++ ") or line.startswith("--- ")):
            continue
        stripped = strip_patch_path_components(line[4:], strip)
        if stripped is None:
            continue
        candidate = stripped.resolve() if stripped.is_absolute() else (cwd / stripped).resolve()
        if candidate not in targets:
            targets.append(candidate)
    return targets


def validate_patch_targets(patch_path: Path, cwd: Path, strip: int) -> list[Path]:
    roots = [resolve_guarded_source_root(root) for root in source_manifest_roots()]
    if not roots:
        raise ValidationError("No guarded source roots are configured.")
    targets = patch_target_paths(patch_path, cwd, strip)
    if not targets:
        raise ValidationError(f"No file targets found in patch: {patch_path}")
    errors: list[str] = []
    for target in targets:
        accepted = False
        for root in roots:
            try:
                target.relative_to(root)
                accepted = True
                break
            except ValueError:
                continue
        if accepted:
            continue
        token = str(target)
        for candidate in target_candidates(token):
            for root in roots:
                try:
                    candidate.relative_to(root)
                    accepted = True
                    break
                except ValueError:
                    continue
            if accepted:
                break
        if not accepted:
            errors.append(
                f"Patch target {token!r} is outside guarded source roots "
                f"{[str(root) for root in roots]}"
            )
    if errors:
        raise ValidationError("\n".join(errors))
    return targets


def source_root_file_set(roots: list[Path]) -> set[Path]:
    files: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                files.add(path.resolve())
    return files


def snapshot_source_files(roots: list[Path], extra_paths: list[Path]) -> dict[Path, tuple[bytes, int]]:
    targets = {path.resolve() for path in extra_paths if path.exists() and path.is_file()}
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*"):
            if guarded_source_file(path):
                targets.add(path.resolve())
    snapshot: dict[Path, tuple[bytes, int]] = {}
    for path in targets:
        stat = path.stat()
        snapshot[path] = (path.read_bytes(), stat.st_mode)
    return snapshot


def metadata_snapshot(paths: list[Path]) -> dict[Path, bytes | None]:
    snapshot: dict[Path, bytes | None] = {}
    for path in paths:
        snapshot[path] = path.read_bytes() if path.exists() else None
    return snapshot


def restore_metadata(snapshot: dict[Path, bytes | None]) -> None:
    for path, data in snapshot.items():
        if data is None:
            if path.exists():
                path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def rollback_source_roots(
    roots: list[Path],
    pre_files: set[Path],
    source_snapshot: dict[Path, tuple[bytes, int]],
) -> None:
    current_files = source_root_file_set(roots)
    for path in sorted(current_files - pre_files, key=lambda item: len(item.parts), reverse=True):
        path.unlink()
    for path, (data, mode) in source_snapshot.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        os.chmod(path, mode)


def run_patch_command(command: list[str], cwd: Path, failure_label: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True)
    if completed.returncode != 0:
        raise ValidationError(
            f"{failure_label} failed:\n"
            f"command: {' '.join(command)}\n"
            f"cwd: {cwd}\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}"
        )
    return completed


def patch_commands(patch_path: Path, strip: int) -> tuple[list[str], list[str], str]:
    if is_git_style_patch(patch_path):
        return (
            ["git", "apply", "--check", f"-p{strip}", str(patch_path)],
            ["git", "apply", f"-p{strip}", str(patch_path)],
            "git apply",
        )
    return (
        ["patch", "--dry-run", f"-p{strip}", "-N", "-i", str(patch_path)],
        ["patch", f"-p{strip}", "-N", "-i", str(patch_path)],
        "patch",
    )


def main() -> int:
    args = parse_args()
    workstream = Path(args.workstream).resolve()
    patch_path = Path(args.patch)
    if not patch_path.is_absolute():
        patch_path = workstream / patch_path
    attestation_path = Path(args.attestation)
    if not attestation_path.is_absolute():
        attestation_path = workstream / attestation_path

    try:
        with scaffold_lock():
            status = load_status(workstream)
            if status.get("source_patch_applied") is True:
                raise ValidationError(
                    f"{status_path(workstream)}: source_patch_applied is already true; refusing to apply twice"
                )
            if args.attestation_only:
                if not attestation_path.exists():
                    raise ValidationError(f"Missing source patch attestation: {attestation_path}")
                quarantine = status.get("legacy_source_patch_quarantine")
                if not isinstance(quarantine, dict):
                    raise ValidationError(
                        f"{status_path(workstream)}: attestation-only mode requires "
                        "legacy_source_patch_quarantine metadata"
                    )
            elif not patch_path.exists():
                raise ValidationError(f"Missing proposed source patch: {patch_path}")

            pre_errors = source_guard_errors()
            if pre_errors:
                raise ValidationError(
                    "Refusing to apply approved patch because guarded source is already dirty:\n"
                    + "\n".join(f"- {error}" for error in pre_errors)
                )

            readiness = readiness_errors(workstream, require_source_patch_application=False)
            if readiness:
                raise ValidationError(
                    "Refusing to apply source patch because workstream is not ready:\n"
                    + "\n".join(f"- {error}" for error in readiness)
                )

            cwd = repo_root().parent if args.cwd == "workspace" else repo_root()
            source_roots = [resolve_guarded_source_root(root) for root in source_manifest_roots()]
            metadata_before = metadata_snapshot([source_manifest_path(), status_path(workstream)])
            pre_files: set[Path] = set()
            source_snapshot: dict[Path, tuple[bytes, int]] = {}
            source_snapshot_ready = False

            try:
                if not args.attestation_only:
                    targets = validate_patch_targets(patch_path, cwd, args.strip)
                    pre_files = source_root_file_set(source_roots)
                    source_snapshot = snapshot_source_files(source_roots, targets)
                    source_snapshot_ready = True
                    dry_run_command, command, apply_tool = patch_commands(patch_path, args.strip)
                    run_patch_command(dry_run_command, cwd, f"{apply_tool} dry run")
                    run_patch_command(command, cwd, f"{apply_tool} command")

                manifest = write_source_manifest(source_manifest_roots())
                status = load_status(workstream)
                status["source_patch_applied"] = True
                if args.attestation_only:
                    quarantine = status.get("legacy_source_patch_quarantine")
                    if isinstance(quarantine, dict):
                        quarantine["state"] = "resolved_after_attestation_only_ratification"
                        quarantine["required_next_step"] = (
                            "None. The already-applied historical source state was ratified "
                            "after independent proof/reviewer gates and recorded through "
                            "scripts/apply_approved_patch.py --attestation-only."
                        )
                    status["source_patch_application"] = {
                        "script": "scripts/apply_approved_patch.py",
                        "applied_at": datetime.now(timezone.utc).isoformat(),
                        "mode": "attestation_only_legacy_already_applied",
                        "attestation_file": str(attestation_path.resolve().relative_to(workstream)),
                        "attestation_sha256": file_sha256(attestation_path),
                        "source_manifest": str(source_manifest_path().relative_to(repo_root())),
                        "tracked_source_files": len(manifest["files"]),
                    }
                else:
                    status["source_patch_application"] = {
                        "script": "scripts/apply_approved_patch.py",
                        "applied_at": datetime.now(timezone.utc).isoformat(),
                        "patch_file": str(patch_path.resolve().relative_to(workstream)),
                        "patch_sha256": file_sha256(patch_path),
                        "apply_tool": apply_tool,
                        "strip": args.strip,
                        "source_manifest": str(source_manifest_path().relative_to(repo_root())),
                        "tracked_source_files": len(manifest["files"]),
                    }
                write_json(status_path(workstream), status)
                event_type = (
                    "legacy_source_patch_attestation_ratified"
                    if args.attestation_only
                    else "approved_source_patch_applied"
                )
                event_message = (
                    f"Ratified already-applied legacy source patch for {status['id']}."
                    if args.attestation_only
                    else f"Applied approved source patch for {status['id']}."
                )
                append_message(
                    event_type,
                    status["id"],
                    event_message,
                    patch_file=str(attestation_path.resolve() if args.attestation_only else patch_path.resolve()),
                    source_manifest=str(source_manifest_path()),
                )
            except Exception as exc:
                rollback_errors: list[str] = []
                if not args.attestation_only and source_snapshot_ready:
                    try:
                        rollback_source_roots(source_roots, pre_files, source_snapshot)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"source rollback failed: {rollback_exc}")
                try:
                    restore_metadata(metadata_before)
                except Exception as rollback_exc:
                    rollback_errors.append(f"metadata rollback failed: {rollback_exc}")
                try:
                    append_message(
                        "approved_source_patch_failed_rolled_back",
                        status.get("id"),
                        "Approved source patch application failed; rollback attempted.",
                        patch_file=str(attestation_path.resolve() if args.attestation_only else patch_path.resolve()),
                        rollback_errors=rollback_errors,
                    )
                except Exception as message_exc:
                    rollback_errors.append(f"failure message write failed: {message_exc}")
                message = (
                    "Source patch transaction failed. Guarded sources and scaffold metadata "
                    "were restored to the pre-apply snapshot."
                )
                if rollback_errors:
                    message += "\nRollback errors:\n" + "\n".join(f"- {error}" for error in rollback_errors)
                message += f"\nOriginal error:\n{exc}"
                raise ValidationError(message) from exc
    except ValidationError as exc:
        print(exc)
        return 1

    if args.attestation_only:
        print(f"Ratified already-applied source patch via attestation: {attestation_path}")
    else:
        print(f"Applied approved patch: {patch_path}")
    print(f"Updated guarded source manifest: {source_manifest_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
