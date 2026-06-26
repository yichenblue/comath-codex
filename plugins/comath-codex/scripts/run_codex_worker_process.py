#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import traceback
from datetime import datetime, timezone
from pathlib import Path


WORKER_TIMEOUT_EXIT_CODE = 124
WORKER_TIMEOUT_GRACE_SECONDS = 10


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2, sort_keys=False)
        handle.write("\n")


def read_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return data


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Codex CLI worker command and record its exit status.")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--command-json", required=True)
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=3600,
        help="Maximum seconds to allow the worker command to run. Use 0 to disable.",
    )
    return parser.parse_args()


def run_worker_command(
    argv: list[str],
    *,
    stdin,
    stdout,
    stderr,
    timeout_seconds: int,
) -> tuple[int, bool]:
    process = subprocess.Popen(
        argv,
        stdin=stdin,
        stdout=stdout,
        stderr=stderr,
        text=True,
        start_new_session=True,
    )
    try:
        return process.wait(timeout=timeout_seconds if timeout_seconds > 0 else None), False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=WORKER_TIMEOUT_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
        return WORKER_TIMEOUT_EXIT_CODE, True


def main() -> int:
    args = parse_args()
    run_dir = Path(args.run_dir).resolve()
    command_json = Path(args.command_json).resolve()
    exit_status_path = run_dir / "exit_status.json"

    started_at = datetime.now(timezone.utc).isoformat()
    try:
        config = read_json(command_json)
        argv = config["argv"]
        prompt_path = Path(config["prompt_path"]).resolve()
        stdout_path = run_dir / "events.jsonl"
        stderr_path = run_dir / "stderr.log"

        write_json(run_dir / "worker_started.json", {
            "started_at": started_at,
            "argv": argv,
            "prompt_path": str(prompt_path),
        })

        with prompt_path.open("r", encoding="utf-8") as stdin:
            with stdout_path.open("w", encoding="utf-8") as stdout:
                with stderr_path.open("w", encoding="utf-8") as stderr:
                    exit_code, timed_out = run_worker_command(
                        argv,
                        stdin=stdin,
                        stdout=stdout,
                        stderr=stderr,
                        timeout_seconds=args.timeout_seconds,
                    )

        finished_at = datetime.now(timezone.utc).isoformat()
        write_json(exit_status_path, {
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "timeout_seconds": args.timeout_seconds if args.timeout_seconds > 0 else None,
            "argv": argv,
            "prompt_path": str(prompt_path),
            "stdout_path": str(stdout_path),
            "stderr_path": str(stderr_path),
        })
        return exit_code
    except Exception as exc:
        finished_at = datetime.now(timezone.utc).isoformat()
        write_json(exit_status_path, {
            "started_at": started_at,
            "finished_at": finished_at,
            "exit_code": 255,
            "error": str(exc),
            "traceback": traceback.format_exc(),
        })
        return 255


if __name__ == "__main__":
    raise SystemExit(main())
