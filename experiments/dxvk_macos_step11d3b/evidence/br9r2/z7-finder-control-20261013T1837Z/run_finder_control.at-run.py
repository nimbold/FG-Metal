#!/usr/bin/env python3
"""Compare direct AppKit target telemetry under a full Finder window."""
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
TARGET = HERE / "z7-finder-target"
TARGET_SOURCE = HERE / "diagnostic_target.swift"
TARGET_TELEMETRY = HERE / "target-telemetry.jsonl"
POLLER = HERE / "finder-window-poller"
POLLER_SOURCE = HERE / "finder_window_poller.swift"
WARMUP_SAMPLES = HERE / "finder-window-warmup.jsonl"
PUBLIC_SAMPLES = HERE / "finder-window-samples.jsonl"
TARGET_BOUNDS = {"x": 68, "y": 62, "width": 640, "height": 392}
TARGET_TITLE = "FG-Metal Z7 Finder Occlusion 20261013T1837Z"
EXPECTED_DISPLAY_ID = 1
WARMUP_MS = 2_000
POLL_MS = 22_000
PRE_SECONDS = 2.0
FINDER_HOLD_SECONDS = 14.0
POST_SECONDS = 3.0


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_message(process: subprocess.Popen[str], timeout: float = 20.0) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("diagnostic target stdout is unavailable")
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError("diagnostic target did not return a JSON response")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"diagnostic target exited before response: {process.poll()}")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("diagnostic target response is not a JSON object")
    return value


def command(process: subprocess.Popen[str], value: dict[str, Any]) -> dict[str, Any]:
    if process.stdin is None:
        raise RuntimeError("diagnostic target stdin is unavailable")
    process.stdin.write(json.dumps(value, separators=(",", ":")) + "\n")
    process.stdin.flush()
    return read_message(process)


def exact_rect(value: Any) -> bool:
    return isinstance(value, dict) and all(value.get(key) == expected
                                           for key, expected in TARGET_BOUNDS.items())


def finder_pid() -> int:
    result = subprocess.run(["/usr/bin/pgrep", "-x", "Finder"], check=True,
                            capture_output=True, text=True, timeout=5)
    pids = [int(line) for line in result.stdout.splitlines() if line.strip()]
    if len(pids) != 1:
        raise RuntimeError(f"expected one Finder process, found {pids}")
    return pids[0]


def run_osascript(script_path: Path, timeout: float = 20.0) -> str:
    result = subprocess.run(["/usr/bin/osascript", str(script_path)], check=True,
                            capture_output=True, text=True, timeout=timeout)
    return result.stdout.strip()


def main() -> int:
    for path in (TARGET, TARGET_SOURCE, POLLER, POLLER_SOURCE):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"required regular file is missing or unsafe: {path}")
    for path in (TARGET_TELEMETRY, WARMUP_SAMPLES, PUBLIC_SAMPLES, HERE / "controller.json"):
        if path.exists() or path.is_symlink():
            raise RuntimeError(f"refusing to replace existing output: {path}")

    timeline: dict[str, Any] = {
        "schemaVersion": 1,
        "diagnostic": "ordinary-finder-full-cover-appkit-control",
        "startedAtUTC": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "targetTitle": TARGET_TITLE,
        "targetBoundsCG": TARGET_BOUNDS,
        "expectedDisplayID": EXPECTED_DISPLAY_ID,
        "targetSourcePath": str(TARGET_SOURCE),
        "targetSourceSHA256": sha256(TARGET_SOURCE),
        "pollerSourcePath": str(POLLER_SOURCE),
        "pollerSourceSHA256": sha256(POLLER_SOURCE),
        "pollerBinaryPath": str(POLLER),
        "pollerBinarySHA256": sha256(POLLER),
        "targetBinarySHA256": sha256(TARGET),
        "finderWindowCreatedByThisRun": False,
    }
    target_log_path = HERE / "target-process.log"
    poller_log_path = HERE / "finder-poller-process.log"
    target_log = target_log_path.open("x", encoding="utf-8")
    poller_log = poller_log_path.open("x", encoding="utf-8")
    target_process: subprocess.Popen[str] | None = None
    poller_process: subprocess.Popen[str] | None = None
    finder_window_id: int | None = None
    return_code = 1
    try:
        pid_before = finder_pid()
        timeline["finderPIDBefore"] = pid_before
        target_process = subprocess.Popen(
            [str(TARGET), TARGET_TITLE, str(TARGET_TELEMETRY)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=target_log,
            text=True, bufsize=1, close_fds=True,
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
        baseline = command(target_process, {"command": "status"})
        if not (baseline.get("isActive") is True and baseline.get("isKey") is True
                and baseline.get("isMain") is True and baseline.get("isVisible") is True
                and baseline.get("visibleBitSet") is True):
            baseline = command(target_process, {"command": "activate"})
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                if (baseline.get("isActive") is True and baseline.get("isKey") is True
                        and baseline.get("isMain") is True and baseline.get("isVisible") is True
                        and baseline.get("visibleBitSet") is True):
                    break
                time.sleep(0.25)
                baseline = command(target_process, {"command": "status"})
        if not (baseline.get("isActive") is True and baseline.get("isKey") is True
                and baseline.get("isMain") is True and baseline.get("isVisible") is True
                and baseline.get("visibleBitSet") is True):
            raise RuntimeError(f"AppKit target lacks active visible baseline: {baseline}")
        timeline["targetActiveBaseline"] = baseline

        geometry = command(target_process, {"command": "wine_geometry"})
        if geometry.get("status") != "PASS":
            raise RuntimeError(f"AppKit target geometry command failed: {geometry}")
        timeline["targetGeometryCommand"] = geometry
        time.sleep(1.0)

        warmup = subprocess.run(
            [str(POLLER), str(target_pid), str(target_window), str(pid_before),
             str(WARMUP_MS), str(WARMUP_SAMPLES)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=poller_log, text=True,
            close_fds=True, timeout=10,
        )
        if warmup.returncode != 0:
            raise RuntimeError(f"Finder geometry warmup poller failed: {warmup.returncode}")
        warmup_rows = [json.loads(line) for line in WARMUP_SAMPLES.read_text(encoding="utf-8").splitlines()]
        valid_warmup = [row for row in warmup_rows
                        if row.get("querySucceeded") is True
                        and row.get("targetOnScreen") is True
                        and row.get("finderFullCover") is False
                        and (row.get("target") or {}).get("ownerPID") == target_pid
                        and (row.get("target") or {}).get("windowNumber") == target_window
                        and exact_rect((row.get("target") or {}).get("bounds"))]
        if len(valid_warmup) < 50:
            raise RuntimeError(f"target CG geometry did not stabilize uncovered: {len(valid_warmup)} samples")
        timeline["uncoveredGeometryWarmup"] = {
            "sampleCount": len(warmup_rows),
            "exactUncoveredTargetSamples": len(valid_warmup),
            "firstExactAtMonotonicNS": valid_warmup[0].get("sampledAtMonotonicNS"),
            "lastExactAtMonotonicNS": valid_warmup[-1].get("sampledAtMonotonicNS"),
            "targetAppKitWindowNumber": target_window,
            "targetPublicCGWindowNumber": (valid_warmup[-1].get("target") or {}).get("windowNumber"),
            "targetAppKitStateAtEnd": {key: baseline.get(key) for key in (
                "isActive", "isKey", "isMain", "isVisible", "visibleBitSet", "occlusionState")},
        }

        poller_process = subprocess.Popen(
            [str(POLLER), str(target_pid), str(target_window), str(pid_before),
             str(POLL_MS), str(PUBLIC_SAMPLES)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=poller_log, text=True, bufsize=1,
            close_fds=True,
        )
        timeline["pollerStartedMonotonicNS"] = time.monotonic_ns()
        time.sleep(PRE_SECONDS)

        create_script = HERE / "finder-create-window.applescript"
        create_script.write_text(
            'tell application "Finder"\n'
            '    activate\n'
            '    set z7Window to make new Finder window\n'
            '    return id of z7Window\n'
            'end tell\n', encoding="utf-8")
        timeline["finderCreateCommandStartedMonotonicNS"] = time.monotonic_ns()
        created_id = run_osascript(create_script)
        finder_window_id = int(created_id)
        timeline["finderWindowCreatedByThisRun"] = True
        timeline["finderWindowID"] = finder_window_id
        timeline["finderCreateCommandCompletedMonotonicNS"] = time.monotonic_ns()
        if finder_pid() != pid_before:
            raise RuntimeError("Finder PID changed during control")

        configure_script = HERE / "finder-configure-window.applescript"
        configure_script.write_text(
            'tell application "Finder"\n'
            f'    set z7Window to window id {finder_window_id}\n'
            '    set target of z7Window to POSIX file "/System/Applications"\n'
            '    delay 0.5\n'
            '    set bounds of z7Window to {68, 62, 708, 454}\n'
            '    return {id of z7Window, bounds of z7Window}\n'
            'end tell\n', encoding="utf-8")
        configure_result = run_osascript(configure_script)
        timeline["finderConfigureResult"] = configure_result
        timeline["finderConfiguredAtMonotonicNS"] = time.monotonic_ns()
        timeline["finderControlStartedMonotonicNS"] = time.monotonic_ns()
        time.sleep(FINDER_HOLD_SECONDS)
        during_status = command(target_process, {"command": "status"})
        timeline["targetDirectStatusDuringFinder"] = during_status

        close_script = HERE / "finder-close-window.applescript"
        close_script.write_text(
            'tell application "Finder"\n'
            f'    close window id {finder_window_id}\n'
            '    return "closed"\n'
            'end tell\n', encoding="utf-8")
        timeline["finderCloseCommandStartedMonotonicNS"] = time.monotonic_ns()
        timeline["finderCloseResult"] = run_osascript(close_script)
        timeline["finderClosedAtMonotonicNS"] = time.monotonic_ns()
        time.sleep(POST_SECONDS)
        timeline["targetDirectStatusAfterFinder"] = command(target_process, {"command": "status"})

        poller_exit = poller_process.wait(timeout=10)
        timeline["pollerExitCode"] = poller_exit
        if poller_exit != 0:
            raise RuntimeError(f"Finder public geometry poller exited {poller_exit}")
        timeline["pollerEndedMonotonicNS"] = time.monotonic_ns()

        timeline["targetQuit"] = command(target_process, {"command": "quit"})
        timeline["targetExitCode"] = target_process.wait(timeout=10)
        if timeline["targetExitCode"] != 0:
            raise RuntimeError("AppKit target did not exit cleanly")
        timeline["result"] = "CAPTURED"
        return_code = 0
    except BaseException as error:
        timeline["result"] = "FAIL"
        timeline["error"] = f"{type(error).__name__}: {error}"
        return_code = 1
    finally:
        if finder_window_id is not None:
            close_script = HERE / "finder-close-window.applescript"
            if not close_script.exists():
                close_script.write_text(
                    'tell application "Finder"\n'
                    f'    close window id {finder_window_id}\n'
                    'end tell\n', encoding="utf-8")
            try:
                if timeline.get("finderClosedAtMonotonicNS") is None:
                    run_osascript(close_script, timeout=10)
                    timeline["finderClosedDuringCleanup"] = True
            except BaseException as error:
                timeline["finderCleanupError"] = f"{type(error).__name__}: {error}"
        if poller_process is not None and poller_process.poll() is None:
            poller_process.terminate()
            try:
                poller_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                poller_process.kill()
                poller_process.wait(timeout=5)
            timeline["pollerExitCodeAfterCleanup"] = poller_process.returncode
        if target_process is not None and target_process.poll() is None:
            try:
                command(target_process, {"command": "quit"})
                timeline["targetExitCode"] = target_process.wait(timeout=8)
            except BaseException:
                target_process.terminate()
                try:
                    target_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    target_process.kill()
                    target_process.wait(timeout=5)
                timeline["targetExitCodeAfterCleanup"] = target_process.returncode
        timeline["finishedAtUTC"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        (HERE / "controller.json").write_text(
            json.dumps(timeline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        target_log.close()
        poller_log.close()
    print(json.dumps({key: timeline.get(key) for key in (
        "result", "error", "targetReady", "finderPIDBefore", "finderWindowID",
        "finderConfigureResult", "targetDirectStatusDuringFinder",
        "finderCloseResult", "pollerExitCode", "targetExitCode")}, indent=2, sort_keys=True))
    return return_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BaseException as error:
        print(f"Z7 Finder control setup failed: {type(error).__name__}: {error}", file=sys.stderr)
        raise SystemExit(1)
