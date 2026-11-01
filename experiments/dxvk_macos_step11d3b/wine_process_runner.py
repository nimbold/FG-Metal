#!/usr/bin/env python3
"""Narrow PrefixLease-owned process entry point for STEP 11D.3-B."""
from __future__ import annotations

import json
import hashlib
import os
import pathlib
import subprocess
import sys
import time

STORAGE = pathlib.Path(__file__).resolve().parents[1] / "storage"
sys.path.insert(0, str(STORAGE))
from storage_policy import PrefixLease, scoped_lease


def write_handoff(path: pathlib.Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        | os.O_CLOEXEC,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def run_managed_process(context: dict, lease: PrefixLease) -> int:
    environment = context["environment"]
    lease.set_env(environment)
    lease.materialize()
    command = context["command"]
    log_path = pathlib.Path(context["runtime_log"])
    result_path = pathlib.Path(context["execution_result"])
    prefix_winevulkan_path = pathlib.Path(context["prefix"]) / "drive_c/windows/system32/winevulkan.dll"
    process_returncode = None
    target_unix_pid = None
    target_process_exit_monotonic_ns = None
    handoff_path = (pathlib.Path(context["observer_handoff_path"])
                    if context.get("observer_handoff_path") else None)
    handoff_sha256 = None
    handoff_monotonic_ns = None
    timed_out = False
    timeout_error = None
    process_error = None
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as log:
        try:
            process = lease.start_wine_process(
                command,
                env=environment,
                runner_path=pathlib.Path(__file__).resolve(strict=True),
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
            )
            target_unix_pid = process.pid
            if handoff_path is not None:
                handoff_monotonic_ns = time.monotonic_ns()
                expected_pe = context["expected_pe_identity"]
                expected_process = context["expected_target_process_identity"]
                trace_path = (
                    pathlib.Path(context["target_trace_directory"])
                    / f"host-trace.{target_unix_pid}.jsonl"
                )
                handoff = {
                    "schema_version": 2,
                    "target_kind": "wine",
                    "window_telemetry_expected": True,
                    "run_id": context["experiment_id"],
                    "experiment_token": environment["FG_B_EXPERIMENT_TOKEN"],
                    "target_unix_pid": target_unix_pid,
                    "target_title": context["target_title"],
                    "expected_pe_identity": {
                        "path": expected_pe["path"],
                        "sha256": expected_pe["sha256"],
                    },
                    "expected_target_process_identity": {
                        "role": expected_process.get("role", "target_process"),
                        "path": expected_process["path"],
                        "sha256": expected_process["sha256"],
                    },
                    "target_trace_path": str(trace_path.resolve()),
                    "runner_pid": os.getpid(),
                    "runner_monotonic_ns": handoff_monotonic_ns,
                    "runner_started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "runner_source_path": str(pathlib.Path(__file__).resolve()),
                    "runner_source_sha256": hashlib.sha256(pathlib.Path(__file__).read_bytes()).hexdigest(),
                }
                write_handoff(handoff_path, handoff)
                handoff_sha256 = hashlib.sha256(handoff_path.read_bytes()).hexdigest()
            try:
                process_returncode = lease.wait_process(
                    process,
                    timeout_seconds=(context["duration_seconds"] + 90
                                     + int(context.get("start_gate_safety_watchdog_seconds") or 0)),
                    output_limit_bytes=context.get("output_limit_bytes"),
                )
                target_process_exit_monotonic_ns = time.monotonic_ns()
            except RuntimeError as error:
                process_error = f"RuntimeError: {error}"
                process_returncode = process.poll()
                if process_returncode is not None:
                    target_process_exit_monotonic_ns = time.monotonic_ns()
        except subprocess.TimeoutExpired as error:
            timed_out = True
            process_returncode = 124
            timeout_error = error
    elapsed = time.monotonic() - started
    record = {
        "experiment_id": context.get("experiment_id"),
        "experiment_token": environment.get("FG_B_EXPERIMENT_TOKEN"),
        "process_returncode": process_returncode,
        "target_unix_pid": target_unix_pid,
        "target_process_exit_monotonic_ns": target_process_exit_monotonic_ns,
        "target_window_observer_handoff_path": str(handoff_path) if handoff_path else None,
        "target_window_observer_handoff_sha256": handoff_sha256,
        "target_window_observer_handoff_monotonic_ns": handoff_monotonic_ns,
        "timed_out": timed_out,
        "managed_process_error": process_error,
        "runtime_process_elapsed_seconds": elapsed,
        "prefix_winevulkan_path": str(
            prefix_winevulkan_path.resolve(strict=True)
        ),
        "prefix_winevulkan_sha256": hashlib.sha256(prefix_winevulkan_path.read_bytes()).hexdigest(),
        "finished_at_unix": time.time(),
    }
    result_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    # Let PrefixLease preserve the actual child failure in its signed receipt.
    # The execution record and runtime log remain separate durable evidence.
    if timeout_error is not None:
        raise timeout_error
    if process_error is not None:
        raise RuntimeError(process_error)
    if process_returncode != 0:
        error_tail = log_path.read_text(encoding="utf-8", errors="replace")[-2000:]
        raise RuntimeError(f"Wine child exited {process_returncode}: {error_tail}")
    return 0


def main() -> int:
    if len(sys.argv) != 2:
        return 2
    context = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
    prefix = pathlib.Path(context["prefix"])
    output = pathlib.Path(context["output"])
    lease = PrefixLease(
        prefix=prefix,
        output_dir=output,
        experiment_id=context["experiment_id"],
        wine_server=pathlib.Path(context["wine_server"]),
        env=context["environment"],
    )
    with scoped_lease(lease):
        return run_managed_process(context, lease)


if __name__ == "__main__":
    raise SystemExit(main())
