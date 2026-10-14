#!/usr/bin/env python3
"""Run one ordinary AppKit occlusion control with the pinned neutral coverer."""
from __future__ import annotations

import hashlib
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
TARGET = HERE / "z7-appkit-target"
TARGET_SOURCE = HERE / "diagnostic_target.swift"
TARGET_TELEMETRY = HERE / "target-telemetry.jsonl"
PUBLIC_POLLER = (REPO / "experiments/dxvk_macos_step11d3b/evidence/br9r2"
                 / "coverer-self-visibility-20261013-02/public-cg-poller")
PUBLIC_POLLER_SOURCE = PUBLIC_POLLER.with_name("public_cg_poller.swift")
WARMUP_SAMPLES = HERE / "public-window-warmup.jsonl"
PUBLIC_SAMPLES = HERE / "public-window-samples.jsonl"
PINNED_COVERER = Path(
    "/Users/nima/Library/Caches/FGMetal/artifacts/sha256/"
    "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc/neutral-coverer"
)
EXPECTED_COVERER_SHA256 = "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc"
EXPECTED_TARGET_CG = {"x": 68, "y": 62, "width": 640, "height": 392}
EXPECTED_DISPLAY_ID = 1
POLL_DURATION_MS = 20_000
WARMUP_DURATION_MS = 2_000
PRE_SECONDS = 3.0
COVERED_SECONDS = 10.0
POST_SECONDS = 5.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_message(process: subprocess.Popen[str], timeout: float = 20.0) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("diagnostic process stdout is unavailable")
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError("diagnostic process did not return a JSON response")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"diagnostic process exited before response: {process.poll()}")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("diagnostic response is not a JSON object")
    return value


def command(process: subprocess.Popen[str], value: dict[str, Any]) -> dict[str, Any]:
    if process.stdin is None:
        raise RuntimeError("diagnostic process stdin is unavailable")
    process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
    process.stdin.flush()
    return read_message(process)


def terminate_owned(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def main() -> int:
    for path in (TARGET, TARGET_SOURCE, PUBLIC_POLLER, PUBLIC_POLLER_SOURCE, PINNED_COVERER):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"required regular file is missing or unsafe: {path}")
    coverer_sha = sha256(PINNED_COVERER)
    if coverer_sha != EXPECTED_COVERER_SHA256:
        raise RuntimeError(f"frozen coverer digest mismatch: {coverer_sha}")
    for path in (TARGET_TELEMETRY, WARMUP_SAMPLES, PUBLIC_SAMPLES):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"refusing to replace existing output: {path}")

    title = "FG-Metal Z7 AppKit Occlusion 20261013T1826Z"
    target_log = (HERE / "target-process.log").open("x", encoding="utf-8")
    coverer_log = (HERE / "coverer-process.log").open("x", encoding="utf-8")
    poller_log = (HERE / "public-poller-process.log").open("x", encoding="utf-8")
    timeline: dict[str, Any] = {
        "schemaVersion": 1,
        "diagnostic": "pure-appkit-occlusion-control",
        "startedAtUTC": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "covererPath": str(PINNED_COVERER),
        "covererSHA256": coverer_sha,
        "targetSourcePath": str(TARGET_SOURCE),
        "targetSourceSHA256": sha256(TARGET_SOURCE),
        "publicPollerSourcePath": str(PUBLIC_POLLER_SOURCE),
        "publicPollerSourceSHA256": sha256(PUBLIC_POLLER_SOURCE),
        "publicPollerBinaryPath": str(PUBLIC_POLLER),
        "publicPollerBinarySHA256": sha256(PUBLIC_POLLER),
        "targetBoundsCG": EXPECTED_TARGET_CG,
        "expectedDisplayID": EXPECTED_DISPLAY_ID,
        "coverageRequestedSeconds": COVERED_SECONDS,
        "events": [],
    }
    target_process: subprocess.Popen[str] | None = None
    coverer_process: subprocess.Popen[str] | None = None
    poller_process: subprocess.Popen[str] | None = None
    try:
        target_process = subprocess.Popen(
            [str(TARGET), title, str(TARGET_TELEMETRY)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=target_log, text=True, bufsize=1,
            close_fds=True,
        )
        target_ready = read_message(target_process)
        if target_ready.get("event") != "ready" or target_ready.get("pid") != target_process.pid:
            raise RuntimeError(f"AppKit target failed readiness: {target_ready}")
        target_pid = int(target_ready["pid"])
        target_window = int(target_ready["windowNumber"])
        if target_window <= 0 or target_ready.get("screenID") != EXPECTED_DISPLAY_ID:
            raise RuntimeError(f"AppKit target identity/display mismatch: {target_ready}")
        timeline["targetReady"] = target_ready

        time.sleep(1.0)
        baseline_status = command(target_process, {"command": "status"})
        if not (baseline_status.get("isActive") is True
                and baseline_status.get("isKey") is True
                and baseline_status.get("isMain") is True
                and baseline_status.get("isVisible") is True
                and baseline_status.get("visibleBitSet") is True):
            baseline_status = command(target_process, {"command": "activate"})
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if (baseline_status.get("isActive") is True
                        and baseline_status.get("isKey") is True
                        and baseline_status.get("isMain") is True
                        and baseline_status.get("isVisible") is True
                        and baseline_status.get("visibleBitSet") is True):
                    break
                time.sleep(0.25)
                baseline_status = command(target_process, {"command": "status"})
        if not (baseline_status.get("isActive") is True
                and baseline_status.get("isKey") is True
                and baseline_status.get("isMain") is True
                and baseline_status.get("isVisible") is True
                and baseline_status.get("visibleBitSet") is True):
            raise RuntimeError(f"AppKit target lacks an active visible baseline: {baseline_status}")
        timeline["targetActiveBaseline"] = baseline_status

        target_geometry = command(target_process, {"command": "wine_geometry"})
        timeline["targetGeometryCommand"] = target_geometry
        if target_geometry.get("status") != "PASS":
            raise RuntimeError(f"AppKit target geometry command failed: {target_geometry}")
        time.sleep(1.0)

        coverer_process = subprocess.Popen(
            [str(PINNED_COVERER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=coverer_log, text=True, bufsize=1, close_fds=True,
        )
        coverer_ready = read_message(coverer_process)
        if coverer_ready.get("event") != "ready" or coverer_ready.get("status") != "PASS":
            raise RuntimeError(f"frozen coverer failed readiness: {coverer_ready}")
        coverer_status = command(coverer_process, {"command": "status"})
        if coverer_status.get("status") != "PASS":
            raise RuntimeError(f"frozen coverer status failed: {coverer_status}")
        coverer_pid = int(coverer_status["coverer_pid"])
        coverer_window = int(coverer_status["coverer_window_number"])
        if coverer_pid != coverer_process.pid or coverer_window <= 0:
            raise RuntimeError(f"coverer process/window identity mismatch: {coverer_status}")
        timeline["covererReady"] = coverer_ready
        timeline["covererInitialStatus"] = coverer_status

        warmup_process = subprocess.run(
            [str(PUBLIC_POLLER), str(target_pid), str(target_window),
             str(coverer_pid), str(coverer_window), str(WARMUP_DURATION_MS),
             str(WARMUP_SAMPLES)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=poller_log, text=True, close_fds=True, timeout=10,
        )
        if warmup_process.returncode != 0:
            raise RuntimeError(f"public geometry warmup failed with exit {warmup_process.returncode}")
        warmup_rows = [json.loads(line) for line in WARMUP_SAMPLES.read_text(encoding="utf-8").splitlines()]
        exact_bounds = {"x": 68, "y": 62, "width": 640, "height": 392}
        valid_warmup = [row for row in warmup_rows
                        if row.get("querySucceeded") is True
                        and row.get("targetOnScreen") is True
                        and row.get("covererOnScreen") is False
                        and (row.get("target") or {}).get("ownerPID") == target_pid
                        and (row.get("target") or {}).get("windowNumber") == target_window
                        and (row.get("target") or {}).get("bounds") == exact_bounds]
        if len(valid_warmup) < 50:
            raise RuntimeError(f"target CG geometry did not stabilize before coverage: {len(valid_warmup)} qualifying warmup samples")
        latest_rows = [json.loads(line) for line in TARGET_TELEMETRY.read_text(encoding="utf-8").splitlines()]
        latest_telemetry = latest_rows[-1] if latest_rows else {}
        if not (latest_telemetry.get("isActive") is True
                and latest_telemetry.get("isKey") is True
                and latest_telemetry.get("isMain") is True
                and latest_telemetry.get("isVisible") is True
                and latest_telemetry.get("visibleBitSet") is True):
            raise RuntimeError(f"target lost its direct AppKit visible baseline before coverage: {latest_telemetry}")
        timeline["targetGeometryWarmup"] = {
            "sampleCount": len(warmup_rows),
            "exactUncoveredTargetSampleCount": len(valid_warmup),
            "firstExactAtMonotonicNS": valid_warmup[0].get("sampledAtMonotonicNS"),
            "lastExactAtMonotonicNS": valid_warmup[-1].get("sampledAtMonotonicNS"),
            "targetAppKitWindowNumber": target_window,
            "targetPublicCGWindowNumber": (valid_warmup[-1].get("target") or {}).get("windowNumber"),
            "targetAppKitStateAtEnd": {key: latest_telemetry.get(key) for key in (
                "isActive", "isKey", "isMain", "isVisible", "visibleBitSet", "occlusionState")},
        }

        poller_process = subprocess.Popen(
            [str(PUBLIC_POLLER), str(target_pid), str(target_window),
             str(coverer_pid), str(coverer_window), str(POLL_DURATION_MS),
             str(PUBLIC_SAMPLES)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=poller_log, text=True, close_fds=True,
        )
        timeline["pollerStartedMonotonicNS"] = time.monotonic_ns()
        time.sleep(PRE_SECONDS)

        timeline["showCommandStartedMonotonicNS"] = time.monotonic_ns()
        target_binding = command(coverer_process, {
            "command": "target", "pid": target_pid, "title": title,
        })
        if (target_binding.get("status") != "PASS"
                or target_binding.get("target_window_number") != target_window):
            raise RuntimeError(f"coverer did not bind the exact target window: {target_binding}")
        show_response = command(coverer_process, {"command": "show"})
        timeline["showCommandAcknowledgedMonotonicNS"] = time.monotonic_ns()
        if (show_response.get("status") != "PASS"
                or show_response.get("target_window_number") != target_window
                or show_response.get("coverer_window_number") != coverer_window):
            raise RuntimeError(f"frozen coverer SHOW command failed: {show_response}")
        timeline["covererTargetBinding"] = target_binding
        timeline["covererShowResponse"] = show_response
        time.sleep(COVERED_SECONDS)

        timeline["hideCommandStartedMonotonicNS"] = time.monotonic_ns()
        hide_response = command(coverer_process, {"command": "hide"})
        timeline["hideCommandAcknowledgedMonotonicNS"] = time.monotonic_ns()
        if hide_response.get("status") != "PASS":
            raise RuntimeError(f"frozen coverer HIDE command failed: {hide_response}")
        timeline["covererHideResponse"] = hide_response
        time.sleep(POST_SECONDS)

        poller_exit = poller_process.wait(timeout=10)
        timeline["pollerExitCode"] = poller_exit
        if poller_exit != 0:
            raise RuntimeError(f"independent public CG poller failed with exit {poller_exit}")
        timeline["pollerEndedMonotonicNS"] = time.monotonic_ns()

        target_quit = command(target_process, {"command": "quit"})
        coverer_quit = command(coverer_process, {"command": "quit"})
        timeline["targetQuit"] = target_quit
        timeline["covererQuit"] = coverer_quit
        target_exit = target_process.wait(timeout=10)
        coverer_exit = coverer_process.wait(timeout=10)
        timeline["targetExitCode"] = target_exit
        timeline["covererExitCode"] = coverer_exit
        if target_exit != 0 or coverer_exit != 0:
            raise RuntimeError("diagnostic target or coverer did not exit cleanly")
        timeline["result"] = "CAPTURED"
        return_code = 0
    except BaseException as error:
        timeline["result"] = "FAIL"
        timeline["error"] = f"{type(error).__name__}: {error}"
        return_code = 1
    finally:
        if poller_process is not None and poller_process.poll() is None:
            terminate_owned(poller_process)
            timeline["pollerExitCodeAfterCleanup"] = poller_process.returncode
        for process in (target_process, coverer_process):
            if process is not None and process.poll() is None:
                try:
                    command(process, {"command": "quit"})
                    process.wait(timeout=8)
                except BaseException:
                    terminate_owned(process)
        if target_process is not None:
            timeline.setdefault("targetExitCode", target_process.poll())
        if coverer_process is not None:
            timeline.setdefault("covererExitCode", coverer_process.poll())
        timeline["finishedAtUTC"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (HERE / "controller.json").write_text(
            json.dumps(timeline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        target_log.close()
        coverer_log.close()
        poller_log.close()
    print(json.dumps({key: timeline.get(key) for key in (
        "result", "error", "targetReady", "covererInitialStatus",
        "covererShowResponse", "covererHideResponse", "pollerExitCode",
        "targetExitCode", "covererExitCode")}, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as error:
        print(f"Z7 AppKit control setup failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
