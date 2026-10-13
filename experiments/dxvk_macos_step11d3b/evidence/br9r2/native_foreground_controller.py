#!/usr/bin/env python3
"""Run one fresh Native foreground control with the lifecycle public observer."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from lifecycle_window_observer import LifecycleWindowObserver, sha256, utc_now

REPO = pathlib.Path(__file__).resolve().parents[4]
NATIVE_RUNNER = REPO / "experiments/dxvk_macos_step11d3a/run_native_control.py"
NATIVE_RUNS = REPO / "experiments/dxvk_macos_step11d3a/evidence/native-runs"
STORAGE_MANIFESTS = REPO / "experiments/storage/manifests"
EVIDENCE = pathlib.Path(__file__).resolve().parent
COVERER = pathlib.Path(
    "/Users/nima/Library/Caches/FGMetal/artifacts/sha256/"
    "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc/"
    "neutral-coverer"
)
NATIVE_TITLE = "STEP 11D.3-A Native MoltenVK Drawable Baseline"
RUNNER_WATCHDOG_SECONDS = 600


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def read_json(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def validate_observer(observer: dict[str, Any] | None, *, run_id: str,
                      handoff: dict[str, Any] | None,
                      native_path: str | None, native_sha256: str | None) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(observer, dict):
        return {"status": "FAIL", "errors": ["final public observer result is missing"]}
    if not isinstance(handoff, dict):
        errors.append("actual Native target PID handoff is missing")
        handoff = {}
    if (handoff.get("schema_version") != 2 or handoff.get("target_kind") != "native"
            or handoff.get("window_telemetry_expected") is not False):
        errors.append("handoff does not identify the Native target without Wine-only telemetry")
    if observer.get("targetRunID") != run_id or handoff.get("run_id") != run_id:
        errors.append("observer run ID differs from the Native run")
    if observer.get("targetPID") != handoff.get("target_unix_pid"):
        errors.append("observer target PID differs from the NativeRunLease child PID")
    if observer.get("experimentToken") != handoff.get("experiment_token"):
        errors.append("observer token differs from the Native target handoff")
    if handoff.get("target_title") != NATIVE_TITLE:
        errors.append("Native target window title differs from the accepted control")
    expected_native = handoff.get("expected_native_identity")
    expected_process = handoff.get("expected_target_process_identity")
    for identity in (expected_native, expected_process):
        if (not isinstance(identity, dict) or identity.get("path") != native_path
                or identity.get("sha256") != native_sha256):
            errors.append("handoff does not bind the exact accepted Native executable path and hash")
            break
    stable = observer.get("canonicalUncoveredState")
    if not isinstance(stable, dict) or stable.get("attained") is not True:
        errors.append("Native foreground did not establish three uncovered samples over two seconds")
    elif (stable.get("stableDurationNS", 0) < 2_000_000_000
          or stable.get("sampleCountInLongestQualifyingStreak", 0) < 3):
        errors.append("Native uncovered stability interval is insufficient")
    if observer.get("watchdogTriggered") is not False or observer.get("error") is not None:
        errors.append("lifecycle observer reported an error or safety watchdog")
    if observer.get("covererProcessesAtObserverStart") != []:
        errors.append("neutral coverer process existed when Native observer started")
    if observer.get("covererWindowsAtObserverStart") != []:
        errors.append("neutral coverer window existed when Native observer started")
    if observer.get("covererProcessEnumerationSucceededAtObserverStart") is not True:
        errors.append("coverer process enumeration failed when Native observer started")
    if observer.get("observerStartWindowQuerySucceeded") is not True:
        errors.append("CGWindow enumeration failed when Native observer started")
    samples = observer.get("samples")
    if not isinstance(samples, list):
        errors.append("Native observer sample list is missing")
        samples = []
    target_samples = [sample for sample in samples
                      if isinstance(sample, dict)
                      and isinstance(sample.get("target"), dict)
                      and sample["target"].get("windowID") is not None]
    if not target_samples:
        errors.append("no public CGWindow row was owned by the actual Native target PID")
    if any(sample["target"].get("titleMatched") is not True for sample in target_samples):
        errors.append("Native public window title did not match the exact expected target title")
    if any(sample.get("canonicalUncoveredEligible") is not True for sample in target_samples):
        errors.append("Native target was not continuously in canonical uncovered state")
    if any(sample.get("neutralCovererProcesses") or sample.get("neutralCovererWindows")
           for sample in samples):
        errors.append("neutral coverer process/window appeared during Native foreground control")
    if any(sample.get("allWindowListQuerySucceeded") is not True
           or sample.get("onScreenWindowListQuerySucceeded") is not True
           or sample.get("covererProcessEnumerationSucceeded") is not True
           for sample in samples):
        errors.append("public target-window or coverer-process query failed during Native foreground control")
    completion = observer.get("experimentCompleteSignal")
    if (not isinstance(completion, dict) or completion.get("run_id") != run_id
            or completion.get("target_unix_pid") != handoff.get("target_unix_pid")
            or completion.get("experiment_token") != handoff.get("experiment_token")
            or completion.get("status") != "RUNNER_FINISHED"
            or completion.get("process_runner_returncode") != 0):
        errors.append("observer completion signal does not bind the Native target identity")
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "run_id": run_id,
        "target_pid": observer.get("targetPID"),
        "target_window_id": observer.get("targetWindowID"),
        "canonical_uncovered_state": stable,
        "sample_count": observer.get("sampleCount"),
    }


def run() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    run_id = f"step11d3a-native-smoke-br9r2-{stamp}-{uuid.uuid4().hex[:8]}"
    tag = f"native-foreground-{stamp}-{uuid.uuid4().hex[:8]}"
    record_path = EVIDENCE / f"{tag}-controller.json"
    stdout_path = EVIDENCE / f"{tag}-runner.log"
    result_path = NATIVE_RUNS / run_id / "run-result.json"
    observer = LifecycleWindowObserver(
        evidence_root=EVIDENCE, runs_root=NATIVE_RUNS, tag=tag,
        coverer_path=COVERER, expected_display_id=1,
    )
    record: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "tag": tag,
        "created_at_utc": utc_now(),
        "controller_path": str(pathlib.Path(__file__).resolve()),
        "controller_sha256": sha256(pathlib.Path(__file__).resolve()),
        "runner_path": str(NATIVE_RUNNER.resolve(strict=True)),
        "runner_sha256": sha256(NATIVE_RUNNER),
        "runner_command": [sys.executable, str(NATIVE_RUNNER), "smoke", run_id],
        "observer": observer.controller_evidence(),
        "status": "PREPARING",
    }
    atomic_json(record_path, record)
    runner: subprocess.Popen[bytes] | None = None
    runner_returncode: int | None = None
    try:
        observer.build_and_start()
        environment = os.environ.copy()
        environment.update(observer.runner_environment())
        record["observer"] = observer.controller_evidence()
        record["observer_started"] = True
        record["status"] = "OBSERVER_RUNNING"
        atomic_json(record_path, record)
        with stdout_path.open("xb") as log:
            runner = subprocess.Popen(
                [sys.executable, str(NATIVE_RUNNER), "smoke", run_id],
                cwd=str(REPO), env=environment,
                stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            record["runner_pid"] = runner.pid
            record["runner_started_at_utc"] = utc_now()
            record["runner_started_monotonic_ns"] = time.monotonic_ns()
            record["status"] = "RUNNING"
            atomic_json(record_path, record)
            try:
                runner_returncode = runner.wait(timeout=RUNNER_WATCHDOG_SECONDS)
            except subprocess.TimeoutExpired:
                record["runner_watchdog_triggered"] = True
                atomic_json(record_path, record)
                runner.terminate()
                try:
                    runner_returncode = runner.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner_returncode = runner.wait(timeout=10)

        handoff = read_json(observer.handoff_path)
        target_pid = handoff.get("target_unix_pid") if handoff else None
        token = handoff.get("experiment_token") if handoff else None
        observer.signal_completion(
            run_id=str(handoff.get("run_id") if handoff else run_id),
            status="RUNNER_FINISHED" if runner_returncode == 0 else "RUNNER_FAILED",
            runner_returncode=runner_returncode,
            target_pid=target_pid if isinstance(target_pid, int) else None,
            experiment_token=token if isinstance(token, str) else None,
        )
        observer_result = observer.wait()
        run_result = read_json(result_path)
        manifest_path = STORAGE_MANIFESTS / f"{run_id}.json"
        manifest = read_json(manifest_path)
        native_path = run_result.get("native_control_path") if run_result else None
        native_sha = run_result.get("native_control_sha256") if run_result else None
        validation = run_result.get("trace_validation", {}) if run_result else {}
        observer_validation = validate_observer(
            observer_result, run_id=run_id, handoff=handoff,
            native_path=native_path, native_sha256=native_sha,
        )
        record["runner_returncode"] = runner_returncode
        record["runner_log_path"] = str(stdout_path.resolve())
        record["runner_log_sha256"] = sha256(stdout_path)
        record["run_result"] = ({
            "path": str(result_path.resolve()),
            "sha256": sha256(result_path),
            "status": run_result.get("status"),
            "mode": run_result.get("mode"),
            "return_code": run_result.get("return_code"),
            "trace_validation": validation,
            "native_run_cleanup_receipt": run_result.get("native_run_cleanup_receipt"),
            "gate0_post_blockers": run_result.get("gate0_post_blockers"),
            "native_control_path": native_path,
            "native_control_sha256": native_sha,
        } if run_result else None)
        record["storage_manifest"] = ({
            "path": str(manifest_path.resolve()),
            "sha256": sha256(manifest_path),
            "decision": manifest.get("decision"),
            "blockers": manifest.get("blockers"),
        } if manifest else None)
        record["observer_result"] = {
            "path": str(observer.result_path.resolve()),
            "sha256": sha256(observer.result_path),
            "validation": observer_validation,
        }
        record["observer"] = observer.controller_evidence()
        record["status"] = (
            "PASS" if runner_returncode == 0 and run_result is not None
            and run_result.get("status") == "PASS"
            and validation.get("quantitative_ready") is True
            and run_result.get("native_run_cleanup_receipt", {}).get("status") == "PASS"
            and run_result.get("gate0_post_blockers") == []
            and manifest is not None and manifest.get("decision") == "ALLOW"
            and observer_validation.get("status") == "PASS"
            else "FAIL"
        )
        record["finished_at_utc"] = utc_now()
        atomic_json(record_path, record)
        print(json.dumps({
            "status": record["status"],
            "controller_record": str(record_path.resolve()),
            "run_result": record.get("run_result"),
            "observer_result": record.get("observer_result"),
            "storage_manifest": record.get("storage_manifest"),
        }, indent=2, sort_keys=True))
        return 0 if record["status"] == "PASS" else 1
    except BaseException as error:
        record["controller_error"] = f"{type(error).__name__}: {error}"
        if runner is not None and runner.poll() is None:
            try:
                runner_returncode = runner.wait(timeout=RUNNER_WATCHDOG_SECONDS)
            except subprocess.TimeoutExpired:
                runner.terminate()
                try:
                    runner_returncode = runner.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner_returncode = runner.wait(timeout=10)
        handoff = read_json(observer.handoff_path)
        if observer.process is not None and observer.process.poll() is None:
            observer.signal_completion(
                run_id=str(handoff.get("run_id") if handoff else run_id),
                status="CONTROLLER_FAILED",
                runner_returncode=runner_returncode,
                target_pid=(handoff.get("target_unix_pid") if handoff else None),
                experiment_token=(handoff.get("experiment_token") if handoff else None),
            )
            try:
                record["observer_result"] = observer.wait()
            except BaseException as observer_error:
                record["observer_error"] = f"{type(observer_error).__name__}: {observer_error}"
        record["observer"] = observer.controller_evidence()
        record["runner_returncode"] = runner_returncode
        record["status"] = "FAIL"
        record["finished_at_utc"] = utc_now()
        atomic_json(record_path, record)
        raise


if __name__ == "__main__":
    raise SystemExit(run())
