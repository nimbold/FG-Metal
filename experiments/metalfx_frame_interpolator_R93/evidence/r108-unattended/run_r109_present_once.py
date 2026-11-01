#!/usr/bin/env python3
"""Run one bounded, input-attributed Elden Ring R109 presentation candidate."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time


ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
RUN_DIR = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/run-02-title-presentation"
RUNTIME_DIR = Path("/tmp/fgmetal-r108-unattended")
CONDITION_FILE = RUNTIME_DIR / "capture-condition"
CAPTURE_TRIGGER = RUNTIME_DIR / "start-title-gpu-capture"
TARGET_FILE = RUNTIME_DIR / "r109-target-source-b"
HELPER = RUNTIME_DIR / "send_bounded_input_to_game"
MIN_POST_WARM_SOURCE_RUNWAY = 20
# The verified-window check and input helper may span several render frames.
# Choosing B this far ahead keeps it well inside the 300 ms hold after the
# successful first input event instead of selecting a near-immediate still pair.
TARGET_FRAME_AHEAD = 16


def stop(reason: str, observed: list[str] | None = None) -> None:
    if observed:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / "r109-present-controller-observed-tail.log").write_text(
            "\n".join(observed[-400:]) + "\n", encoding="utf-8"
        )
    print(f"R109_PRESENT_CONTROLLER=BLOCKED reason={reason}", file=sys.stderr)
    raise SystemExit(2)


def helper_run(action: str, expected_pid: int | None = None) -> str:
    command = [str(HELPER), action]
    if expected_pid is not None:
        command.append(str(expected_pid))
    try:
        result = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=6
        )
    except (subprocess.SubprocessError, OSError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        stop(f"bounded-input-helper-{action}-failed:{detail.strip()}")
    return result.stdout.strip()


def next_line(stream, deadline: float, observed: list[str]) -> str:
    while time.monotonic() < deadline:
        line = stream.readline()
        if line:
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            observed.append(decoded)
            return decoded
        time.sleep(0.05)
    stop("timed-out-waiting-for-observer-event", observed)


def drain_available(stream, observed: list[str]) -> None:
    while True:
        line = stream.readline()
        if not line:
            return
        observed.append(line.decode("utf-8", errors="replace").rstrip("\n"))


def parse_acquisitions(lines: list[str]) -> dict[int, int]:
    result: dict[int, int] = {}
    pattern = re.compile(
        r"R108_SOURCE_FRAME_ACQUIRED pid=\d+ seq=(\d+) acquiredAtMonotonicNs=(\d+)"
    )
    for line in lines:
        match = pattern.search(line)
        if match:
            result[int(match.group(1))] = int(match.group(2))
    return result


def read_observer(pid: int) -> Path:
    path = RUNTIME_DIR / f"observer.{pid}.log"
    try:
        os.kill(pid, 0)
        data = path.read_bytes()
    except OSError:
        stop("verified-window-owner-has-no-observer-log")
    head = data[:65_536].decode("utf-8", errors="replace")
    tail = data[-262_144:].decode("utf-8", errors="replace")
    if not re.search(rf"TITLE_LAYER_SELECTED pid={pid} ", head + tail):
        stop("observer-log-does-not-bind-to-verified-window-owner")
    if "HOOK_INSTALLED" not in head:
        stop("observer-hooks-not-confirmed-in-process")
    loaded = re.search(rf"LOADED pid={pid} .*?presentation=1 .*?framebufferOnly=1 titleCapture=1", head)
    if not loaded:
        stop("observer-log-does-not-confirm-r109-presentation-capture-mode")
    return path


def main() -> int:
    if not HELPER.is_file():
        stop("input-helper-not-built")

    ready = helper_run("check")
    pid_match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", ready)
    if not pid_match:
        stop("safe-target-check-returned-no-pid")
    pid = int(pid_match.group(1))
    observer_log = read_observer(pid)

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if CAPTURE_TRIGGER.exists():
        CAPTURE_TRIGGER.unlink()
    if TARGET_FILE.exists():
        TARGET_FILE.unlink()
    CONDITION_FILE.write_text("CHARACTER\n", encoding="utf-8")

    observed: list[str] = []
    result: dict[str, object] = {
        "schema": "fgmetal-r109-present-controller-v1",
        "observerPid": pid,
        "observerLog": str(observer_log),
        "condition": "CHARACTER",
        "action": "character-forward",
    }
    with observer_log.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        CAPTURE_TRIGGER.write_text("R109 one-shot bounded capture\n", encoding="utf-8")
        deadline = time.monotonic() + 35.0

        armed: dict[str, object] | None = None
        while armed is None:
            line = next_line(stream, deadline, observed)
            match = re.search(
                r"R108_TITLE_CAPTURE=ARMED window=(\d+) .*startSequence=(\d+) expectedEndSequence=(\d+)",
                line,
            )
            if match:
                armed = {
                    "window": int(match.group(1)),
                    "startSequence": int(match.group(2)),
                    "endSequence": int(match.group(3)),
                    "line": line,
                }

        warm_line = None
        warm_time = None
        while warm_line is None:
            line = next_line(stream, deadline, observed)
            if "R108_PROVIDER_WARM_STATE=READY" not in line or "condition=CHARACTER " not in line:
                continue
            warm_match = re.search(r"eventTimestampMonotonicNs=(\d+)", line)
            if not warm_match:
                continue
            warm_line, warm_time = line, int(warm_match.group(1))

        refreshed = helper_run("check", pid)
        refreshed_pid = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", refreshed)
        if not refreshed_pid or int(refreshed_pid.group(1)) != pid:
            stop("verified-window-owner-changed-before-input", observed)

        drain_available(stream, observed)
        acquisitions = parse_acquisitions(observed)
        frontier = max(acquisitions, default=int(armed["startSequence"]) - 1)
        runway = int(armed["endSequence"]) - frontier
        if runway < MIN_POST_WARM_SOURCE_RUNWAY:
            stop("insufficient-source-runway-after-provider-warm", observed)
        target_b = frontier + TARGET_FRAME_AHEAD
        if target_b + 2 > int(armed["endSequence"]):
            stop("target-source-pair-outside-capture-runway", observed)
        target_temp = TARGET_FILE.with_suffix(".tmp")
        target_temp.write_text(f"{target_b}\n", encoding="utf-8")
        os.replace(target_temp, TARGET_FILE)
        result.update({
            "captureWindow": armed,
            "providerWarmLine": warm_line,
            "providerWarmMonotonicNs": warm_time,
            "sourceAcquisitionFrontierBeforeInput": frontier,
            "remainingSourceRunwayBeforeInput": runway,
            "targetSourcePair": {"A": target_b - 1, "B": target_b},
        })

        input_result = helper_run("character-forward", pid)
        action_pid = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", input_result)
        event = re.search(
            r"startMonotonicNs=(\d+) firstEventMonotonicNs=(\d+) "
            r"lastEventMonotonicNs=(\d+) endMonotonicNs=(\d+)",
            input_result,
        )
        if not action_pid or int(action_pid.group(1)) != pid or not event:
            stop("input-helper-returned-invalid-target-or-timestamps", observed)
        first_event_ns = int(event.group(2))
        if int(event.group(1)) <= int(warm_time):
            stop("input-event-did-not-follow-provider-warm-event", observed)
        result.update({
            "inputResult": input_result,
            "inputFirstEventMonotonicNs": first_event_ns,
            "inputLastEventMonotonicNs": int(event.group(3)),
        })

        target_a = target_b - 1
        acquisition = None
        commit = None
        motion_saved = False
        m_ready = None
        exact_pair = f"{target_a}->{target_b}"
        pair_deadline = time.monotonic() + 20.0
        while acquisition is None or commit is None or not motion_saved or m_ready is None:
            line = next_line(stream, pair_deadline, observed)
            acquisition_match = re.search(
                rf"R108_SOURCE_FRAME_ACQUIRED pid={pid} seq={target_b} acquiredAtMonotonicNs=(\d+)",
                line,
            )
            if acquisition_match:
                acquisition = int(acquisition_match.group(1))
                if acquisition <= first_event_ns:
                    stop("target-source-B-acquired-before-input-event", observed)

            commit_match = re.search(
                rf"R108_SOURCE_PRODUCER_COMMIT sourceSequence={target_b} .*?"
                r"originalCommitEntryMonotonicNs=(\d+) originalCommitReturnMonotonicNs=(\d+)",
                line,
            )
            if commit_match:
                commit = (int(commit_match.group(1)), int(commit_match.group(2)))
                if commit[0] <= first_event_ns or commit[1] <= first_event_ns:
                    stop("target-source-B-producer-commit-not-after-input", observed)

            if f"R108_MOTION_DIAGNOSTIC_EXPORT=SAVED pair={exact_pair} " in line:
                motion_saved = True

            if "R109_PRESENT_M_READY" in line:
                pair_match = re.search(r"pair=(\d+)->(\d+)", line)
                if pair_match and pair_match.group(0).removeprefix("pair=") == exact_pair:
                    m_ready = line

        exact_present = re.search(rf"R109_PRESENT_TARGET=ARMED sourcePair={exact_pair} ", "\n".join(observed))
        if not exact_present:
            stop("presentation-target-was-not-armed-for-exact-pair", observed)
        commit_return = re.search(
            rf"R109_SOURCE_PRODUCER_COMMIT_RETURN sequence={target_b} .*?originalCommitReturnMonotonicNs=(\d+)",
            "\n".join(observed),
        )
        if not commit_return or int(commit_return.group(1)) <= first_event_ns:
            stop("exact-target-producer-commit-return-not-after-input", observed)
        ready_match = re.search(r"artifactSha256=([0-9a-f]{64}) reviewNonce=([A-F0-9-]{36})", m_ready or "")
        if not ready_match:
            stop("M-ready-event-has-no-pair-bound-review-token", observed)
        result.update({
            "targetSourceBAcquiredAtMonotonicNs": acquisition,
            "targetSourceBProducerCommitEntryAndReturnMonotonicNs": list(commit),
            "targetSourceBCommitReturnMonotonicNs": int(commit_return.group(1)),
            "motionDiagnosticSavedForExactPair": motion_saved,
            "mReadyLine": m_ready,
            "artifactSha256": ready_match.group(1),
            "reviewNonce": ready_match.group(2),
            "decisionPath": str(RUNTIME_DIR / f"r109-quality-decision.{pid}"),
        })

        result_path = RUN_DIR / "r109-present-controller.json"
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    shutil.copyfile(observer_log, RUN_DIR / f"observer-r109-present-{pid}.log")
    print(json.dumps(result, indent=2))
    print("R109_PRESENT_CONTROLLER=M_READY exactPairAwaitingOfflineMotionAndArtifactReview=1")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:
        print(f"R109_PRESENT_CONTROLLER=FAILED detail={error}", file=sys.stderr)
        raise
