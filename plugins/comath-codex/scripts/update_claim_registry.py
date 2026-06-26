#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from lib import (
    ValidationError,
    append_message,
    claim_registry_path,
    latest_review_round,
    latest_reviews,
    load_project_state,
    load_reviews,
    load_status,
    readiness_errors,
    report_claims,
    report_path,
    repo_root,
    scaffold_lock,
    write_json,
)


def relative(path: Path) -> str:
    return str(path.resolve().relative_to(repo_root()))


def completed_workstream_paths() -> list[Path]:
    project_state = load_project_state()
    paths: list[Path] = []
    for item in project_state.get("workstreams", []):
        if not isinstance(item, dict):
            continue
        if item.get("status") != "COMPLETE":
            continue
        rel_path = item.get("path")
        if isinstance(rel_path, str):
            paths.append(repo_root() / rel_path)
    return paths


def claim_entries_for_workstream(workstream: Path) -> list[dict]:
    status = load_status(workstream)
    errors = readiness_errors(workstream)
    if errors:
        raise ValidationError(
            f"Completed workstream {workstream} does not pass readiness checks:\n"
            + "\n".join(f"- {error}" for error in errors)
        )

    reviews = load_reviews(workstream)
    round_number = latest_review_round(reviews)
    latest = latest_reviews(reviews)
    approved: set[str] = set()
    reviewer_ids: list[str] = []
    review_files: list[str] = []
    for review in latest:
        reviewer_ids.append(str(review.get("reviewer_id")))
        review_files.append(str(Path(review["_path"]).resolve().relative_to(repo_root())))
        approved.update(str(item) for item in review.get("approved_claims", []))

    entries: list[dict] = []
    for claim in report_claims(report_path(workstream)):
        claim_id = claim["claim_id"]
        if claim_id not in approved:
            raise ValidationError(f"{workstream}: claim {claim_id} is not approved by latest reviewers")
        claim_key = f"{status['id']}:{claim_id}"
        entries.append({
            "claim_key": claim_key,
            "claim_id": claim_id,
            "text": claim["text"],
            "status": "approved",
            "source_workstream": status["id"],
            "source_report": relative(report_path(workstream)),
            "source_status": relative(workstream / "status.json"),
            "review_round": round_number,
            "reviewers": sorted(set(reviewer_ids)),
            "review_files": sorted(set(review_files)),
            "supporting_artifacts_dir": relative(workstream / "artifacts"),
        })
    return entries


def main() -> int:
    try:
        with scaffold_lock():
            claims: list[dict] = []
            skipped: list[dict] = []
            seen: set[str] = set()
            for workstream in completed_workstream_paths():
                try:
                    entries = claim_entries_for_workstream(workstream)
                except ValidationError as exc:
                    skipped.append({
                        "workstream": relative(workstream),
                        "reason": str(exc),
                    })
                    continue
                for entry in entries:
                    if entry["claim_key"] in seen:
                        raise ValidationError(f"Duplicate claim key {entry['claim_key']}")
                    seen.add(entry["claim_key"])
                    claims.append(entry)

            registry = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "source": "scripts/update_claim_registry.py",
                "claims": sorted(claims, key=lambda item: item["claim_key"]),
                "skipped_workstreams": skipped,
            }
            write_json(claim_registry_path(), registry)
            append_message(
                "claim_registry_updated",
                None,
                f"Updated claim registry with {len(claims)} approved claims; skipped {len(skipped)} completed workstreams.",
                claim_registry=str(claim_registry_path().relative_to(repo_root())),
                claim_count=len(claims),
                skipped_workstream_count=len(skipped),
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(f"Updated {claim_registry_path()} with {len(claims)} approved claims.")
    if skipped:
        print(f"Skipped {len(skipped)} completed workstream(s) that no longer pass readiness gates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
