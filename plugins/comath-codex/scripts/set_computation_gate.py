#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from lib import (
    ValidationError,
    append_message,
    load_status,
    project_workstream_path,
    scaffold_lock,
    status_path,
    write_json,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Require a completed computation workstream before a proof workstream can promote."
    )
    parser.add_argument("proof_workstream", help="Path like workstreams/ws_010_proof.")
    parser.add_argument("--linked-computation-workstream-id", required=True)
    parser.add_argument(
        "--reason",
        default=(
            "Proof workstream proposes or patches a leading-order mathematical source formula; "
            "numerical or symbolic computation sanity checks are required before promotion."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    proof_workstream = Path(args.proof_workstream).resolve()

    try:
        with scaffold_lock():
            proof_status = load_status(proof_workstream)
            if proof_status.get("type") != "proof":
                raise ValidationError("Computation gates can only be attached to proof workstreams.")

            linked_workstream = project_workstream_path(args.linked_computation_workstream_id)
            linked_status = load_status(linked_workstream)
            if linked_status.get("type") != "computation":
                raise ValidationError(
                    f"Linked workstream {args.linked_computation_workstream_id!r} is not a computation workstream."
                )

            proof_status["requires_computation_gate"] = True
            proof_status["linked_computation_workstream_id"] = args.linked_computation_workstream_id
            proof_status["computation_gate_reason"] = args.reason
            proof_status["computation_required"] = True
            write_json(status_path(proof_workstream), proof_status)
            append_message(
                "computation_gate_set",
                proof_status["id"],
                f"Proof workstream {proof_status['id']} now requires computation gate {args.linked_computation_workstream_id}.",
                linked_computation_workstream_id=args.linked_computation_workstream_id,
                reason=args.reason,
            )
    except ValidationError as exc:
        print(exc)
        return 1

    print(
        f"Set computation gate for {proof_status['id']} -> "
        f"{args.linked_computation_workstream_id}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
