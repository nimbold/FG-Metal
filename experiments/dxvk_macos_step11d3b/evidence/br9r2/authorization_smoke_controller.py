#!/usr/bin/env python3
"""Run one 30-second Wine authorization smoke with a lifecycle-bound observer."""
from __future__ import annotations

import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from lifecycle_window_observer import (
    LifecycleWindowObserver,
    current_canonical_uncovered_streak,
    sha256,
    utc_now,
)

REPO = pathlib.Path(__file__).resolve().parents[4]
EXPERIMENT = REPO / "experiments/dxvk_macos_step11d3b"


def _configured_evidence_root() -> pathlib.Path:
    override = os.environ.get("FGMETAL_BR9R_EVIDENCE_ROOT")
    if override is None:
        return pathlib.Path(__file__).resolve().parent
    root = pathlib.Path(override)
    if (not root.is_absolute() or root.parent != pathlib.Path("/private/tmp")
            or not re.fullmatch(r"codex-br9r2-[a-z0-9-]{8,96}", root.name)
            or root.resolve(strict=False) != root):
        raise ValueError("BR9R transient evidence root must be a canonical private /private/tmp/codex-br9r2-* path")
    root.mkdir(mode=0o700, exist_ok=True)
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or root.is_symlink()
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ValueError("BR9R transient evidence root must be an owned mode-0700 directory")
    return root


EVIDENCE = _configured_evidence_root()
RUNNER = EXPERIMENT / "runner_b.py"
APP = EXPERIMENT / "control/bare_vulkan_app-start-gated.exe"
WINE_HOST_EXECUTABLE = pathlib.Path(
    "/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine/"
    "lib/wine/x86_64-unix/wine"
)
COVERER = pathlib.Path(
    "/Users/nima/Library/Caches/FGMetal/artifacts/sha256/"
    "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc/"
    "neutral-coverer"
)
# Bound gate acquisition and runner completion separately. Reset only after the
# qualifying signal and controller record are written.
RUNNER_WATCHDOG_SECONDS = 1200
START_GATE_SAMPLE_MAX_AGE_NS = 1_500_000_000
RUN_ID_PATTERN = re.compile(r"step11d3b-smoke-\d{8}-\d{6}-[0-9a-f]{8}")
TOKEN_PATTERN = re.compile(r"[0-9a-f]{32}")


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def read_runner_result(stdout_path: pathlib.Path) -> dict[str, Any] | None:
    try:
        text = stdout_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    matches = list(re.finditer(r'(?m)^\{\s*$', text))
    for match in reversed(matches):
        try:
            value, _end = json.JSONDecoder().raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "experiment_id" in value:
            return value
    return None


def read_json(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def read_observer_samples(path: pathlib.Path) -> list[dict[str, Any]]:
    if path.is_symlink() or not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict) and value.get("event") == "sample":
                    rows.append(value)
    except OSError:
        return []
    return rows


def write_exclusive_file(path: pathlib.Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                         | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        # Publish only a complete, durable file and refuse to replace any prior input.
        os.link(temporary, path, follow_symlinks=False)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        raise
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def write_start_gate_signal(path: pathlib.Path, value: dict[str, Any]) -> None:
    raw = (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()
    write_exclusive_file(path, raw)


def qualifying_start_gate_signal(observer: LifecycleWindowObserver) -> tuple[dict[str, Any] | None, str | None]:
    handoff = read_json(observer.handoff_path)
    if handoff is None:
        return None, "actual Wine target identity handoff is not available yet"
    run_id = handoff.get("run_id")
    target_pid = handoff.get("target_unix_pid")
    token = handoff.get("experiment_token")
    if (not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id)
            or type(target_pid) is not int or target_pid <= 1
            or not isinstance(token, str) or not TOKEN_PATTERN.fullmatch(token)
            or handoff.get("schema_version") != 2
            or handoff.get("target_kind") != "wine"
            or handoff.get("window_telemetry_expected") is not True
            or handoff.get("target_title") != "FG-Metal Step 11D.3-B Bare Vulkan"
            or type(handoff.get("runner_pid")) is not int
            or handoff.get("runner_pid", 0) <= 1
            or type(handoff.get("runner_monotonic_ns")) is not int
            or handoff.get("runner_monotonic_ns", 0) <= 0):
        return None, "target handoff is malformed or does not identify the actual Wine target"
    expected_trace = (EXPERIMENT / "runs" / run_id / "host-traces"
                      / f"host-trace.{target_pid}.jsonl").resolve()
    expected_pe = handoff.get("expected_pe_identity")
    expected_process = handoff.get("expected_target_process_identity")
    if (handoff.get("target_trace_path") != str(expected_trace)
            or not isinstance(expected_pe, dict)
            or expected_pe.get("path") != str(APP.resolve())
            or expected_pe.get("sha256") != sha256(APP)
            or not isinstance(expected_process, dict)
            or expected_process.get("role") != "wine_host_image"
            or expected_process.get("path") != str(WINE_HOST_EXECUTABLE.resolve())
            or expected_process.get("sha256") != sha256(WINE_HOST_EXECUTABLE)):
        return None, "target handoff does not bind the exact PE, Wine host image, and run-local trace"
    latest = observer.latest()
    samples = read_observer_samples(observer.samples_path)
    proof = current_canonical_uncovered_streak(
        samples, target_pid=target_pid, latest_state=latest)
    if proof is None:
        return None, "latest target samples have not yet established a current two-second uncovered streak"
    latest_sample_ns = samples[-1].get("sampledAtMonotonicNS")
    pre_snapshot_ns = time.monotonic_ns()
    if (type(latest_sample_ns) is not int or pre_snapshot_ns < latest_sample_ns
            or pre_snapshot_ns - latest_sample_ns > START_GATE_SAMPLE_MAX_AGE_NS):
        return None, "latest canonical target sample is stale before start-gate evidence capture"
    latest_sample = samples[-1]
    target = latest_sample["target"]
    streak_samples = proof.pop("samples")
    attainment_suffix = str(proof["stability_proven_monotonic_ns"])
    state_snapshot_path = observer.directory / f"authorization-start-state-{attainment_suffix}.json"
    streak_snapshot_path = observer.directory / f"authorization-start-streak-{attainment_suffix}.jsonl"
    state_snapshot_bytes = (json.dumps(latest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    streak_snapshot_bytes = b"".join(
        (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        for row in streak_samples
    )
    write_exclusive_file(state_snapshot_path, state_snapshot_bytes)
    write_exclusive_file(streak_snapshot_path, streak_snapshot_bytes)
    released_ns = time.monotonic_ns()
    if (released_ns < latest_sample_ns
            or released_ns - latest_sample_ns > START_GATE_SAMPLE_MAX_AGE_NS
            or released_ns < proof["stability_proven_monotonic_ns"]):
        return None, "latest canonical target sample became stale before start-gate release"
    gate_signal = {
        "schema_version": 1,
        "status": "CANONICAL_UNCOVERED_ATTAINED",
        "run_id": run_id,
        "target_unix_pid": target_pid,
        "experiment_token": token,
        **proof,
        "canonical_uncovered_attained": True,
        "released_monotonic_ns": released_ns,
        "released_at_utc": utc_now(),
        "clock_domain": "darwin_mach_absolute_time_ns",
        "target_window_owner_pid": target.get("ownerPID"),
        "target_trace_path": handoff["target_trace_path"],
        "expected_pe_identity": expected_pe,
        "expected_target_process_identity": expected_process,
        "observer_latest_path": str(observer.latest_path.resolve()),
        "observer_latest_sha256": sha256(observer.latest_path),
        "observer_samples_path": str(observer.samples_path.resolve()),
        "observer_samples_sha256": sha256(observer.samples_path),
        "start_state_snapshot_path": str(state_snapshot_path.resolve()),
        "start_state_snapshot_sha256": sha256(state_snapshot_path),
        "start_streak_snapshot_path": str(streak_snapshot_path.resolve()),
        "start_streak_snapshot_sha256": sha256(streak_snapshot_path),
        "observer_source_sha256": observer.metadata["observer_source_sha256"],
        "observer_binary_sha256": observer.metadata.get("observer_binary_sha256"),
        "coverer_binary_sha256": observer.coverer_sha,
        "current_sample_age_at_release_ns": released_ns - latest_sample_ns,
    }
    if released_ns < proof["stability_proven_monotonic_ns"]:
        return None, "Darwin monotonic clock did not order the start-gate release after attainment"
    return gate_signal, None


def run() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    tag = f"wine-auth-{stamp}-{uuid.uuid4().hex[:8]}"
    record_path = EVIDENCE / f"{tag}-controller.json"
    stdout_path = EVIDENCE / f"{tag}-runner.log"
    observer = LifecycleWindowObserver(
        evidence_root=EVIDENCE, runs_root=EXPERIMENT / "runs",
        tag=tag, coverer_path=COVERER, expected_display_id=1,
    )
    record: dict[str, Any] = {
        "schema_version": 1,
        "tag": tag,
        "created_at_utc": utc_now(),
        "controller_path": str(pathlib.Path(__file__).resolve()),
        "controller_sha256": sha256(pathlib.Path(__file__).resolve()),
        "runner_path": str(RUNNER.resolve(strict=True)),
        "runner_sha256": sha256(RUNNER),
        "runner_command": [sys.executable, str(RUNNER), "smoke"],
        "observer": observer.controller_evidence(),
        "status": "PREPARING",
    }
    atomic_json(record_path, record)
    runner: subprocess.Popen[bytes] | None = None
    runner_returncode: int | None = None
    start_gate_signal: dict[str, Any] | None = None
    last_start_gate_reason: str | None = None
    last_gate_sample_monotonic_ns: int | None = None
    try:
        observer.build_and_start()
        record["observer"] = observer.controller_evidence()
        record["observer_started"] = True
        record["status"] = "OBSERVER_RUNNING"
        atomic_json(record_path, record)
        environment = os.environ.copy()
        environment.update(observer.runner_environment())
        with stdout_path.open("xb") as log:
            runner = subprocess.Popen(
                [sys.executable, str(RUNNER), "smoke"],
                cwd=str(REPO), env=environment, stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            record["runner_pid"] = runner.pid
            record["runner_started_at_utc"] = utc_now()
            record["runner_started_monotonic_ns"] = time.monotonic_ns()
            record["status"] = "RUNNING"
            atomic_json(record_path, record)
            runner_deadline = time.monotonic() + RUNNER_WATCHDOG_SECONDS
            runner_watchdog_phase = "start_gate_acquisition"
            while runner.poll() is None and time.monotonic() < runner_deadline:
                if start_gate_signal is None:
                    latest = observer.latest()
                    latest_sample = latest.get("latestSample") if isinstance(latest, dict) else None
                    marker = latest_sample.get("sampledAtMonotonicNS") if isinstance(latest_sample, dict) else None
                    if type(marker) is int and marker != last_gate_sample_monotonic_ns:
                        last_gate_sample_monotonic_ns = marker
                        try:
                            candidate, last_start_gate_reason = qualifying_start_gate_signal(observer)
                        except BaseException as gate_error:
                            candidate = None
                            last_start_gate_reason = (
                                f"{type(gate_error).__name__}: {gate_error}")
                        if candidate is not None:
                            write_start_gate_signal(observer.start_gate_signal_path, candidate)
                            start_gate_signal = candidate
                            record["start_gate"] = {
                                "status": "RELEASED",
                                "path": str(observer.start_gate_signal_path.resolve()),
                                "sha256": sha256(observer.start_gate_signal_path),
                                "signal": candidate,
                            }
                            record["start_gate_released_at_utc"] = candidate["released_at_utc"]
                            record["start_gate_released_monotonic_ns"] = candidate["released_monotonic_ns"]
                            atomic_json(record_path, record)
                            runner_watchdog_phase = "post_gate_completion"
                            runner_deadline = time.monotonic() + RUNNER_WATCHDOG_SECONDS
                try:
                    runner.wait(timeout=0.1)
                except subprocess.TimeoutExpired:
                    pass
            if runner.poll() is None:
                record["runner_watchdog_triggered"] = True
                record["runner_watchdog_phase"] = runner_watchdog_phase
                record["runner_watchdog_triggered_at_utc"] = utc_now()
                try:
                    atomic_json(record_path, record)
                finally:
                    runner.terminate()
                    try:
                        runner_returncode = runner.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        runner.kill()
                        runner_returncode = runner.wait(timeout=10)
            else:
                runner_returncode = runner.returncode
        if start_gate_signal is None:
            record["start_gate"] = {
                "status": "NOT_RELEASED",
                "signal_path": str(observer.start_gate_signal_path),
                "last_qualification_reason": last_start_gate_reason,
            }
        else:
            persisted_signal = read_json(observer.start_gate_signal_path)
            record["start_gate"]["persisted_signal_matches"] = persisted_signal == start_gate_signal
            record["start_gate"]["sha256"] = sha256(observer.start_gate_signal_path)
        result = read_runner_result(stdout_path)
        handoff = None
        if observer.handoff_path.is_file() and not observer.handoff_path.is_symlink():
            handoff = json.loads(observer.handoff_path.read_text(encoding="utf-8"))
        run_id = (handoff.get("run_id") if isinstance(handoff, dict)
                  else (result.get("experiment_id") if result else f"controller:{tag}"))
        target_pid = handoff.get("target_unix_pid") if isinstance(handoff, dict) else None
        target_token = handoff.get("experiment_token") if isinstance(handoff, dict) else None
        observer.signal_completion(
            run_id=str(run_id),
            status="RUNNER_FINISHED" if runner_returncode == 0 else "RUNNER_FAILED",
            runner_returncode=runner_returncode,
            target_pid=target_pid if isinstance(target_pid, int) else None,
            experiment_token=target_token if isinstance(target_token, str) else None,
        )
        observer_result = observer.wait()
        run_result_path = None
        if result and isinstance(result.get("report"), str):
            run_result_path = pathlib.Path(result["report"])
        if run_result_path and run_result_path.is_file() and not run_result_path.is_symlink():
            run_result = json.loads(run_result_path.read_text(encoding="utf-8"))
            record["run_result"] = {
                "path": str(run_result_path.resolve()),
                "sha256": sha256(run_result_path),
                "status": run_result.get("status"),
                "is_valid": run_result.get("is_valid"),
                "quantitative_ready": run_result.get("validation", {}).get("quantitative_ready"),
                "smoke_signature": run_result.get("smoke_signature"),
                "positive_displayed_callback_count": run_result.get("smoke_signature", {}).get(
                    "displayed_callback_count"),
                "raw_zero_fraction": run_result.get("smoke_signature", {}).get("raw_zero_fraction"),
                "public_window_observer": run_result.get("public_window_observer"),
                "gate0_pre": run_result.get("gate0_pre"),
                "gate0_post": run_result.get("gate0_post"),
                "prefix_cleanup_pass": run_result.get("prefix_cleanup_pass"),
                "start_gate_validation": run_result.get("start_gate_validation"),
                "start_gate_signal": run_result.get("start_gate_signal"),
            }
        record["runner_stdout_path"] = str(stdout_path.resolve())
        record["runner_stdout_sha256"] = sha256(stdout_path)
        record["runner_returncode"] = runner_returncode
        record["runner_result"] = result
        record["observer_result"] = {
            "path": str(observer.result_path.resolve()),
            "sha256": sha256(observer.result_path),
            "sample_count": observer_result.get("sampleCount") if observer_result else None,
            "target_pid": observer_result.get("targetPID") if observer_result else None,
            "canonical_uncovered_state": observer_result.get("canonicalUncoveredState")
            if observer_result else None,
            "error": observer_result.get("error") if observer_result else None,
        }
        record["observer"] = observer.controller_evidence()
        run_status = record.get("run_result", {}).get("status")
        signature = record.get("run_result", {}).get("smoke_signature")
        record["status"] = (
            "PASS" if record.get("runner_watchdog_triggered") is not True
            and runner_returncode == 0 and run_status == "PASS"
            and record.get("run_result", {}).get("is_valid") is True
            and record.get("run_result", {}).get("quantitative_ready") is True
            and isinstance(signature, dict) and signature.get("healthy_refresh") is True
            and isinstance(signature.get("displayed_callback_count"), int)
            and signature.get("displayed_callback_count", 0) > 0
            and isinstance(signature.get("displayed_fps"), (int, float))
            and signature.get("displayed_fps", 0) > 0
            and isinstance(signature.get("raw_zero_fraction"), (int, float))
            and signature.get("raw_zero_fraction", 1) <= 0.05
            and record.get("start_gate", {}).get("status") == "RELEASED"
            and record.get("start_gate", {}).get("persisted_signal_matches") is True
            and isinstance(record.get("run_result", {}).get("start_gate_validation"), dict)
            and record["run_result"]["start_gate_validation"].get("status") == "PASS"
            and observer_result is not None
            and observer_result.get("canonicalUncoveredState", {}).get("attained") is True
            else "FAIL"
        )
        record["finished_at_utc"] = utc_now()
        record["finished_monotonic_ns"] = time.monotonic_ns()
        atomic_json(record_path, record)
        print(json.dumps({
            "status": record["status"],
            "controller_record": str(record_path.resolve()),
            "run_result": record.get("run_result"),
            "observer_result": record.get("observer_result"),
        }, indent=2, sort_keys=True))
        return 0 if record["status"] == "PASS" else 1
    except BaseException as error:
        record["controller_error"] = f"{type(error).__name__}: {error}"
        record["runner_returncode"] = runner_returncode
        record["runner_stdout_path"] = str(stdout_path)
        record["observer"] = observer.controller_evidence()
        if observer.process is not None and observer.process.poll() is None:
            result = read_runner_result(stdout_path)
            handoff = None
            if observer.handoff_path.is_file() and not observer.handoff_path.is_symlink():
                try:
                    handoff = json.loads(observer.handoff_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    handoff = None
            target_pid = (handoff or {}).get("target_unix_pid")
            target_token = (handoff or {}).get("experiment_token")
            observer.signal_completion(
                run_id=str((handoff or {}).get("run_id")
                           or (result or {}).get("experiment_id") or f"controller:{tag}"),
                status="CONTROLLER_FAILED",
                runner_returncode=runner_returncode,
                target_pid=target_pid if isinstance(target_pid, int) else None,
                experiment_token=target_token if isinstance(target_token, str) else None,
            )
            try:
                record["observer_result"] = observer.wait(timeout_seconds=120)
            except BaseException as observer_error:
                record["observer_error"] = f"{type(observer_error).__name__}: {observer_error}"
        record["finished_at_utc"] = utc_now()
        record["status"] = "FAIL"
        atomic_json(record_path, record)
        raise


if __name__ == "__main__":
    raise SystemExit(run())
