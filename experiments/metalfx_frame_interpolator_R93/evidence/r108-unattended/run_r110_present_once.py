#!/usr/bin/env python3
"""Run one bounded, input-attributed Elden Ring R110 presentation candidate."""

from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time


ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
RUN_DIR = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/run-03-r110-title-presentation"
RUNTIME_DIR = Path("/tmp/fgmetal-r108-unattended")
CONDITION_FILE = RUNTIME_DIR / "capture-condition"
CAPTURE_TRIGGER = RUNTIME_DIR / "start-title-gpu-capture"
TARGET_FILE = RUNTIME_DIR / "r110-target-source-b"
WORLD_SCENE_CONFIRMATION = RUNTIME_DIR / "r110-world-scene-confirmed"
HELPER = RUNTIME_DIR / "send_bounded_input_to_game"
ACTION = "combined-right-forward"
CONDITION = "COMBINED"
# The live estimator queue trails source acquisition while it warms. Arm the
# exact tuple immediately after the capture window opens, before waiting for
# an internal frame, so the bounded camera sweep overlaps the selected pair.
# Four source frames fit the measured 16–41 ms acquisition cadence inside the
# helper's 250 ms camera sweep, with room before the 48-frame window ends.
TARGET_FRAME_AHEAD = 4


def stop(reason: str, observed: list[str] | None = None) -> None:
    if observed:
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        (RUN_DIR / "r110-present-controller-observed-tail.log").write_text(
            "\n".join(observed[-400:]) + "\n", encoding="utf-8"
        )
    print(f"R110_PRESENT_CONTROLLER=BLOCKED reason={reason}", file=sys.stderr)
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


def helper_start_action(action: str, expected_pid: int):
    process = subprocess.Popen(
        [str(HELPER), action, str(expected_pid)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    deadline = time.monotonic() + 6.0
    while time.monotonic() < deadline:
        ready, _, _ = select.select([process.stdout], [], [], 0.05)
        if ready:
            line = process.stdout.readline()
            if line:
                match = re.search(
                    r"R108_INPUT_TO_GAME=FIRST_EVENT pid=(\d+) firstEventMonotonicNs=(\d+)",
                    line,
                )
                if match and int(match.group(1)) == expected_pid:
                    return process, line.rstrip("\n"), int(match.group(2))
                if line.strip():
                    stop(f"input-helper-returned-invalid-first-event:{line.strip()}")
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            stop(f"input-helper-ended-before-first-event:{process.returncode}:{stderr.strip() or stdout.strip()}")
    stdout, stderr = process.communicate()
    stop(f"input-helper-first-event-timeout:{stderr.strip() or stdout.strip()}")


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
        stop("observer-log-does-not-confirm-r110-presentation-capture-mode")
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
    WORLD_SCENE_CONFIRMATION.write_text("VISIBLE_WORLD\n", encoding="utf-8")
    if CAPTURE_TRIGGER.exists():
        CAPTURE_TRIGGER.unlink()
    if TARGET_FILE.exists():
        TARGET_FILE.unlink()
    CONDITION_FILE.write_text(f"{CONDITION}\n", encoding="utf-8")

    observed: list[str] = []
    result: dict[str, object] = {
        "schema": "fgmetal-r110-present-controller-v1",
        "observerPid": pid,
        "observerLog": str(observer_log),
        "condition": CONDITION,
        "action": ACTION,
        "worldSceneConfirmation": "user-confirmed-visible-in-world-before-target-arm",
    }
    with observer_log.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        CAPTURE_TRIGGER.write_text("R110 one-shot bounded capture\n", encoding="utf-8")
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

        action_process, first_event_line, first_event_ns = helper_start_action(ACTION, pid)
        observed.append(first_event_line)
        drain_available(stream, observed)
        acquisitions = parse_acquisitions(observed)
        frontier = max(acquisitions, default=int(armed["startSequence"]) - 1)
        runway = int(armed["endSequence"]) - frontier
        target_b = frontier + TARGET_FRAME_AHEAD
        if target_b + 2 > int(armed["endSequence"]):
            action_process.communicate(timeout=6)
            stop("target-source-pair-outside-capture-runway", observed)
        target_temp = TARGET_FILE.with_suffix(".tmp")
        target_temp.write_text(f"{target_b}\n", encoding="utf-8")
        os.replace(target_temp, TARGET_FILE)
        result.update({
            "captureWindow": armed,
            "sourceAcquisitionFrontierBeforeInput": frontier,
            "remainingSourceRunwayBeforeInput": runway,
            "targetSourcePair": {"A": target_b - 1, "B": target_b},
        })

        target_a = target_b - 1
        exact_pair = f"{target_a}->{target_b}"
        acquisition = None
        commit = None
        fast_gate = None
        target_armed = None
        provider_warm_line = None
        m_ready = None
        pair_deadline = time.monotonic() + 20.0
        while (acquisition is None or commit is None or fast_gate is None or
               target_armed is None):
            line = next_line(stream, pair_deadline, observed)
            if ("R110_PRESENT_TARGET=ARMED" in line and
                    f"sourcePair={exact_pair} " in line):
                target_armed = line
            if (provider_warm_line is None and
                    "R108_PROVIDER_WARM_STATE=READY" in line and
                    f"condition={CONDITION} " in line):
                provider_warm_line = line
            acquisition_match = re.search(
                rf"R108_SOURCE_FRAME_ACQUIRED pid={pid} seq={target_b} acquiredAtMonotonicNs=(\d+)",
                line,
            )
            if acquisition_match:
                acquisition = int(acquisition_match.group(1))
                if acquisition <= first_event_ns:
                    action_process.communicate(timeout=6)
                    stop("target-source-B-acquired-before-input-event", observed)

            commit_match = re.search(
                rf"R108_SOURCE_PRODUCER_COMMIT sourceSequence={target_b} .*?"
                r"originalCommitEntryMonotonicNs=(\d+) originalCommitReturnMonotonicNs=(\d+)",
                line,
            )
            if commit_match:
                commit = (int(commit_match.group(1)), int(commit_match.group(2)))
                if commit[0] <= first_event_ns or commit[1] <= first_event_ns:
                    action_process.communicate(timeout=6)
                    stop("target-source-B-producer-commit-not-after-input", observed)

            if f"R110_FAST_PRESENT_ELIGIBILITY=" in line and f"pair={exact_pair} " in line:
                fast_gate = line

            if "R110_PRESENT_M_READY" in line:
                pair_match = re.search(r"pair=(\d+)->(\d+)", line)
                if pair_match and pair_match.group(0).removeprefix("pair=") == exact_pair:
                    m_ready = line

        input_tail, input_error = action_process.communicate(timeout=6)
        if action_process.returncode != 0:
            stop(f"bounded-input-helper-action-failed:{input_error.strip() or input_tail.strip()}", observed)
        input_result = f"{first_event_line}\n{input_tail.strip()}".strip()
        action_pid = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", input_result)
        event = re.search(
            r"startMonotonicNs=(\d+) firstEventMonotonicNs=(\d+) "
            r"lastEventMonotonicNs=(\d+) endMonotonicNs=(\d+)",
            input_result,
        )
        if not action_pid or int(action_pid.group(1)) != pid or not event:
            stop("input-helper-returned-invalid-target-or-timestamps", observed)
        first_event_ns = int(event.group(2))
        result.update({
            "inputResult": input_result,
            "inputFirstEventMonotonicNs": first_event_ns,
            "inputLastEventMonotonicNs": int(event.group(3)),
        })

        if target_armed is None:
            stop("presentation-target-was-not-armed-for-exact-pair", observed)
        commit_return = re.search(
            rf"R110_SOURCE_PRODUCER_COMMIT_RETURN sequence={target_b} .*?originalCommitReturnMonotonicNs=(\d+)",
            "\n".join(observed),
        )
        if not commit_return or int(commit_return.group(1)) <= first_event_ns:
            stop("exact-target-producer-commit-return-not-after-input", observed)
        accepted = "R110_FAST_PRESENT_ELIGIBILITY=ACCEPTED" in (fast_gate or "")
        title_sequence = None
        output_diagnostic = None
        source_b_dispatch = None
        if accepted:
            while m_ready is None or title_sequence is None or output_diagnostic is None:
                line = next_line(stream, pair_deadline, observed)
                if "R110_PRESENT_M_READY" in line and f"pair={exact_pair} " in line:
                    m_ready = line
                if f"R110_TITLE_WSI_SEQUENCE=" in line and f"pair={exact_pair} " in line:
                    title_sequence = line
                if f"R110_METALFX_DIAGNOSTIC=" in line and f"pair={exact_pair} " in line:
                    output_diagnostic = line
        else:
            source_b_dispatch = next((line for line in observed
                if "R110_WSI_ORDER_EVENT origin=SOURCE " in line
                and f"sourceSequence={target_b} " in line), None)
            while source_b_dispatch is None:
                line = next_line(stream, pair_deadline, observed)
                if ("R110_WSI_ORDER_EVENT origin=SOURCE " in line
                    and f"sourceSequence={target_b} " in line):
                    source_b_dispatch = line

        def sha256(path: Path) -> str | None:
            if not path.is_file():
                return None
            digest = hashlib.sha256()
            with path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    digest.update(block)
            return digest.hexdigest()

        artifact_paths = {
            "sourceA": RUN_DIR / f"source-sequence-{target_a}.raw",
            "sourceB": RUN_DIR / f"source-sequence-{target_b}.raw",
            "motion": RUN_DIR / f"motion-pair-{target_a}-{target_b}.rg16f",
            "midpoint": RUN_DIR / f"title-M-for-{target_a}-{target_b}.rgba16f",
        }
        result.update({
            "targetSourceBAcquiredAtMonotonicNs": acquisition,
            "targetSourceBProducerCommitEntryAndReturnMonotonicNs": list(commit),
            "targetSourceBCommitReturnMonotonicNs": int(commit_return.group(1)),
            "providerWarmLine": provider_warm_line,
            "presentationTargetArmedLine": target_armed,
            "fastEligibility": fast_gate,
            "mReadyLine": m_ready,
            "titleWsiSequence": title_sequence,
            "sourceBFallbackDispatch": source_b_dispatch,
            "postAttemptOutputDiagnostic": output_diagnostic,
            "artifacts": {name: {"path": str(path), "sha256": sha256(path)}
                          for name, path in artifact_paths.items()},
        })

        result_path = RUN_DIR / "r110-present-controller.json"
        result_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    shutil.copyfile(observer_log, RUN_DIR / f"observer-r110-present-{pid}.log")
    print(json.dumps(result, indent=2))
    print("R110_PRESENT_CONTROLLER=ATTEMPT_COMPLETE slowDiagnostics=post-attempt")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as error:
        print(f"R110_PRESENT_CONTROLLER=FAILED detail={error}", file=sys.stderr)
        raise
