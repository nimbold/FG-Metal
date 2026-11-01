#!/usr/bin/env python3
"""Run one R113 title tuple from observed physical mouse movement only."""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time


ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
OUT_ROOT = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r113-physical-camera"
ATTEMPT_NUMBER = int(sys.argv[2]) if len(sys.argv) > 2 else 1
OUT = OUT_ROOT / f"attempt-{ATTEMPT_NUMBER:02d}"
RUNTIME = Path("/tmp/fgmetal-r108-unattended")
OBSERVER = RUNTIME / f"observer.{sys.argv[1]}.log"
MOUSE_WATCH = OUT_ROOT / "r113_physical_mouse_watch"
TARGET_CHECK = RUNTIME / "send_bounded_character_forward_to_game"
TRIGGER = RUNTIME / "start-title-gpu-capture"
CONDITION = RUNTIME / "capture-condition"
WORLD = RUNTIME / "r112-world-scene-confirmed"
TARGET_B = RUNTIME / "r112-target-source-b"
CHARACTER_ATTRIBUTION = RUNTIME / "r112-character-attribution-verified"
CONTROLLER_LOG = OUT / "r113-controller.log"
MOUSE_LOG = OUT / "r113-physical-mouse-events.log"
RESULT_JSON = OUT / "r113-controller.json"
EXPECTED_GAME = "/Users/nima/Games/Elden Ring/Game/eldenring.exe"
PID = int(sys.argv[1])

ACQUISITION = re.compile(
    rf"R108_SOURCE_FRAME_ACQUIRED pid={PID} seq=(\d+) acquiredAtMonotonicNs=(\d+)"
)
CAPTURE_ARMED = re.compile(
    r"R108_TITLE_CAPTURE=ARMED window=(\d+) .*startSequence=(\d+) "
    r"expectedEndSequence=(\d+)"
)
PROVIDER_READY = re.compile(
    r"R108_PROVIDER_WARM_STATE=READY completedEligiblePair=(\d+)->(\d+) "
    r"nextExpectedEligiblePair=(\d+)->(\d+) condition=CAMERA "
    r"internalPresentId=(\d+) eventTimestampMonotonicNs=(\d+)"
)
MOUSE_EVENT = re.compile(
    r"R113_PHYSICAL_MOUSE_EVENT pid=(\d+) monotonicNs=(\d+) x=(-?\d+) y=(-?\d+) "
    r"deltaX=(-?\d+) deltaY=(-?\d+) withinGameWindow=(\d) frontmostPid=(-?\d+) "
    r"topWindowPid=(-?\d+) eventType=(\d+)"
)


class StopRun(RuntimeError):
    pass


def log(line: str) -> None:
    with CONTROLLER_LOG.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
    print(line, flush=True)


def monotonic_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def write_json(value: dict[str, object]) -> None:
    temporary = RESULT_JSON.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, RESULT_JSON)


def verify_target() -> str:
    completed = subprocess.run(
        [str(TARGET_CHECK), "check", str(PID)],
        check=True, capture_output=True, text=True, timeout=6,
    )
    ready = completed.stdout.strip()
    if f"pid={PID}" not in ready or "executable=eldenring.exe" not in ready or "window=ELDEN RING™" not in ready:
        raise StopRun("exact-game-window-check-failed")
    return ready


def acquisition_state(contents: str) -> tuple[int, int]:
    matches = list(ACQUISITION.finditer(contents))
    if not matches:
        raise StopRun("no-live-source-acquisitions")
    match = matches[-1]
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {
        "schema": "fgmetal-r113-physical-camera-once-v1",
        "gamePid": PID,
        "attemptNumber": ATTEMPT_NUMBER,
        "observerLog": str(OBSERVER),
        "manualPhysicalInputOnly": True,
        "syntheticGameplayInputSent": False,
        "titlePresentationVerdict": "NOT_RUN",
        "futureSourceLeaks": 0,
    }
    watcher: subprocess.Popen[str] | None = None
    try:
        if not OBSERVER.is_file() or not TARGET_CHECK.is_file() or not MOUSE_WATCH.is_file():
            raise StopRun("observer-or-read-only-input-tools-missing")
        verify_line = verify_target()
        observer_text = OBSERVER.read_text(encoding="utf-8", errors="replace")
        loaded_lines = [line for line in observer_text.splitlines() if line.startswith(f"LOADED pid={PID} ")]
        if not loaded_lines:
            raise StopRun("exact-pid-has-no-loaded-observer")
        loaded = loaded_lines[0]
        if f"target={EXPECTED_GAME} targetArgMatch=1" not in loaded or "presentation=1" not in loaded or "framebufferOnly=1 titleCapture=1" not in loaded:
            raise StopRun("observer-mode-or-executable-identity-mismatch")
        layer_lines = [line for line in observer_text.splitlines() if line.startswith(f"TITLE_LAYER_SELECTED pid={PID} ")]
        if not layer_lines or "deviceName=Apple M3" not in layer_lines[0] or "registryID=4294968554" not in layer_lines[0] or "framebufferOnly=1" not in layer_lines[0]:
            raise StopRun("exact-cametal-layer-or-device-not-verified")
        latest, acquired_ns = acquisition_state(observer_text)
        if monotonic_ns() - acquired_ns > 10_000_000_000:
            raise StopRun("source-acquisition-is-stale")
        result.update({
            "exactGameWindowCheck": verify_line,
            "observerLoadedLine": loaded,
            "cametalLayerLine": layer_lines[0],
            "lastPreCaptureSourceSequence": latest,
        })
        log(f"R113_PREFLIGHT=READY pid={PID} executable={EXPECTED_GAME} window=ELDEN RING™ layerAndDevice=VERIFIED liveSourceSequence={latest} worldScene=USER_CONFIRMED")

        for path in (TRIGGER, TARGET_B, CHARACTER_ATTRIBUTION):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        WORLD.write_text("VISIBLE_WORLD\n", encoding="utf-8")
        CONDITION.write_text("CAMERA\n", encoding="utf-8")

        watcher = subprocess.Popen(
            [str(MOUSE_WATCH), str(PID)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        if watcher.stdout is None:
            raise StopRun("physical-mouse-monitor-stdout-unavailable")
        ready_deadline = time.monotonic() + 4
        mouse_ready_line = ""
        while time.monotonic() < ready_deadline:
            readable, _, _ = select.select([watcher.stdout], [], [], 0.1)
            if readable:
                line = watcher.stdout.readline().strip()
                if line.startswith("R113_MOUSE_MONITOR=READY"):
                    mouse_ready_line = line
                    break
                if line.startswith("R113_MOUSE_MONITOR=BLOCKED"):
                    raise StopRun(line)
            if watcher.poll() is not None:
                error = watcher.stderr.read().strip() if watcher.stderr else ""
                raise StopRun(f"physical-mouse-monitor-exited:{error}")
        if not mouse_ready_line:
            raise StopRun("physical-mouse-monitor-readiness-timeout")
        if f"centerTopWindowPid={PID}" not in mouse_ready_line:
            raise StopRun("elden-ring-window-is-not-topmost-at-screen-center")
        MOUSE_LOG.write_text(mouse_ready_line + "\n", encoding="utf-8")
        result["physicalMouseMonitorReady"] = mouse_ready_line

        with OBSERVER.open("rb") as log_stream:
            log_stream.seek(0, os.SEEK_END)
            TRIGGER.write_text("R113 one-shot physical-camera capture\n", encoding="utf-8")
            result["captureTriggerMonotonicNs"] = monotonic_ns()
            log(f"R113_CAPTURE_TRIGGERED pid={PID} source=confirmed-world-user")

            deadline = time.monotonic() + 12
            start_sequence = end_sequence = 0
            warm: dict[str, int] | None = None
            latest_sequence = latest_timestamp = 0
            while time.monotonic() < deadline and warm is None:
                chunk = log_stream.read()
                if chunk:
                    for raw in chunk.splitlines():
                        line = raw.decode("utf-8", errors="replace")
                        armed = CAPTURE_ARMED.search(line)
                        if armed:
                            start_sequence = int(armed.group(2))
                            end_sequence = int(armed.group(3))
                            result["captureWindowArmedLine"] = line
                            log(line)
                        acquisition = ACQUISITION.search(line)
                        if acquisition:
                            latest_sequence = int(acquisition.group(1))
                            latest_timestamp = int(acquisition.group(2))
                        provider = PROVIDER_READY.search(line)
                        if provider:
                            warm = {
                                "sourceA": int(provider.group(1)),
                                "sourceB": int(provider.group(2)),
                                "nextA": int(provider.group(3)),
                                "nextB": int(provider.group(4)),
                                "internalPresentId": int(provider.group(5)),
                                "eventTimestampMonotonicNs": int(provider.group(6)),
                            }
                            result["providerWarmStateLine"] = line
                            log(line)
                if latest_timestamp and monotonic_ns() - latest_timestamp > 10_000_000_000:
                    raise StopRun("live-source-acquisition-stalled")
                time.sleep(0.002)
            if not start_sequence or not end_sequence:
                raise StopRun("capture-window-not-armed")
            if warm is None:
                raise StopRun("provider-did-not-reach-ready-state")
            runway = end_sequence - latest_sequence
            result["sourceRunwayAfterProviderWarm"] = runway
            result["providerWarmState"] = warm
            if runway < 8:
                raise StopRun("insufficient-source-runway-after-provider-warm")
            result["manualMotionReadyMonotonicNs"] = monotonic_ns()
            log("R113_MANUAL_MOTION_READY")
            log("USER_ACTION: Physically move the actual mouse horizontally to rotate the Elden Ring camera steadily for about 1–2 seconds. No keyboard movement. This listener observes physical events only.")

            physical_events: deque[tuple[int, int]] = deque()
            chosen_pair: tuple[int, int] | None = None
            window_complete_seen = False
            while latest_sequence < end_sequence and not window_complete_seen and chosen_pair is None:
                readable, _, _ = select.select([watcher.stdout, log_stream], [], [], 0.01)
                if watcher.stdout in readable:
                    raw_event = watcher.stdout.readline().strip()
                    with MOUSE_LOG.open("a", encoding="utf-8") as mouse_stream:
                        mouse_stream.write(raw_event + "\n")
                    event = MOUSE_EVENT.search(raw_event)
                    if event and int(event.group(1)) == PID and event.group(7) == "1" and int(event.group(9)) == PID:
                        event_ns, delta_x = int(event.group(2)), int(event.group(5))
                        if event_ns >= int(result["manualMotionReadyMonotonicNs"]):
                            physical_events.append((event_ns, delta_x))
                            while physical_events and event_ns - physical_events[0][0] > 350_000_000:
                                physical_events.popleft()
                            if len(physical_events) >= 3 and sum(abs(dx) for _, dx in physical_events) >= 12:
                                latest_contents = OBSERVER.read_text(encoding="utf-8", errors="replace")
                                current, _ = acquisition_state(latest_contents)
                                pair_a = max(current + 1, start_sequence + 1)
                                pair_b = pair_a + 1
                                if pair_b <= end_sequence:
                                    chosen_pair = (pair_a, pair_b)
                                    marker = f"pid={PID} A={pair_a} B={pair_b} input=physical-mouse-camera movementFirstEventNs={physical_events[0][0]} movementDetectedNs={event_ns} cumulativeAbsDeltaX={sum(abs(dx) for _, dx in physical_events)}\n"
                                    (OUT / "r113-physical-camera-attribution.txt").write_text(marker, encoding="utf-8")
                                    TARGET_B.write_text(f"{pair_b}\n", encoding="utf-8")
                                    result["selectedPair"] = {
                                        "sourceA": pair_a,
                                        "sourceB": pair_b,
                                        "firstPhysicalMouseEventMonotonicNs": physical_events[0][0],
                                        "movementDetectedMonotonicNs": event_ns,
                                        "cumulativeAbsoluteHorizontalDelta": sum(abs(dx) for _, dx in physical_events),
                                    }
                                    log(f"R113_PHYSICAL_CAMERA_PAIR_SELECTED pair={pair_a}->{pair_b} source=observed-user-mouse-movement selection=next-consecutive-pair")
                                    break
                                raise StopRun("no-source-runway-remains-for-consecutive-pair")
                if log_stream in readable:
                    chunk = log_stream.read()
                    if chunk:
                        for raw in chunk.splitlines():
                            line = raw.decode("utf-8", errors="replace")
                            acquisition = ACQUISITION.search(line)
                            if acquisition:
                                latest_sequence = int(acquisition.group(1))
                                latest_timestamp = int(acquisition.group(2))
                            if "R108_TITLE_CAPTURE=SOURCE_WINDOW_COMPLETE" in line and f"endSequence={end_sequence} " in line:
                                window_complete_seen = True
                if watcher.poll() is not None:
                    error = watcher.stderr.read().strip() if watcher.stderr else ""
                    raise StopRun(f"physical-mouse-monitor-exited-during-window:{error}")
            if chosen_pair is None:
                raise StopRun("no-qualifying-physical-mouse-movement-before-bounded-window-complete")

        pair_a, pair_b = chosen_pair
        pair_text = f"{pair_a}->{pair_b}"
        target_deadline = time.monotonic() + 2
        while time.monotonic() < target_deadline:
            contents = OBSERVER.read_text(encoding="utf-8", errors="replace")
            armed = next((line for line in contents.splitlines() if "R110_PRESENT_TARGET=ARMED" in line and f"sourcePair={pair_text} " in line), None)
            if armed:
                result["targetArmedLine"] = armed
                log(armed)
                break
            time.sleep(0.01)
        if not result.get("targetArmedLine"):
            raise StopRun("observer-did-not-arm-selected-exact-pair")

        processing_deadline = time.monotonic() + 40
        interesting: list[str] = []
        seen_protocol_lines: set[str] = set()
        gate_rejected = False
        gate_accepted = False
        order_validated = False
        internal_displayed = False
        internal_id: str | None = None
        with OBSERVER.open("rb") as live_log:
            live_log.seek(0, os.SEEK_END)
            while time.monotonic() < processing_deadline:
                chunk = live_log.read()
                if chunk:
                    for raw in chunk.splitlines():
                        line = raw.decode("utf-8", errors="replace")
                        relevant = (
                            pair_text in line
                            or line.startswith("R108_TITLE_CAPTURE=PROCESSED_WINDOW_COMPLETE")
                            or "R103_FUTURE_SOURCE_LEAK=DETECTED" in line
                            or (internal_id is not None and f"internalPresentId={internal_id}" in line)
                        )
                        if not relevant or line in seen_protocol_lines:
                            continue
                        seen_protocol_lines.add(line)
                        if any(token in line for token in (
                            "R110_FAST_PRESENT_ELIGIBILITY=", "R102_SOURCE_TICKET=DEFERRED",
                            "R103_SOURCE_FRONTIER=CLOSED", "R103_SOURCE_FRONTIER=OPEN",
                            "R110_PRESENT_M_READY", "R110_TITLE_WSI_SEQUENCE=",
                            "R101_WSI_DISPATCH origin=INTERNAL", "R101_WSI_DISPLAYED origin=INTERNAL",
                            "R102_EVENT", "R110_METALFX_DIAGNOSTIC=",
                            "R108_TITLE_CAPTURE=PROCESSED_WINDOW_COMPLETE",
                            "R103_FUTURE_SOURCE_LEAK=DETECTED", "R110_SOURCE_PRODUCER_COMMIT_RETURN",
                        )):
                            interesting.append(line)
                            if "R103_FUTURE_SOURCE_LEAK=DETECTED" in line:
                                result["futureSourceLeaks"] = int(result["futureSourceLeaks"]) + 1
                            if "R110_FAST_PRESENT_ELIGIBILITY=ACCEPTED" in line:
                                gate_accepted = True
                            if "R110_FAST_PRESENT_ELIGIBILITY=REJECTED" in line:
                                gate_rejected = True
                            if "R110_PRESENT_M_READY" in line:
                                match_id = re.search(r"internalPresentId=(\d+)", line)
                                if match_id:
                                    internal_id = match_id.group(1)
                            if "R110_TITLE_WSI_SEQUENCE=VALIDATED" in line:
                                order_validated = True
                            if "R101_WSI_DISPLAYED origin=INTERNAL" in line and "reachedDisplay=1" in line:
                                internal_displayed = True
                if gate_rejected:
                    # Allow the accepted recovery path to release B, reopen
                    # the frontier, and advance C before recording the stop.
                    all_text = OBSERVER.read_text(encoding="utf-8", errors="replace")
                    recovered = (
                        f"sourceSequence={pair_b}" in all_text
                        and "R103_SOURCE_FRONTIER=OPEN" in all_text
                        and re.search(rf"R110_WSI_ORDER_EVENT origin=SOURCE order=\d+ sourceSequence={pair_b + 1} ", all_text)
                    )
                    if recovered:
                        result["titlePresentationVerdict"] = "NOT_VALIDATED_FAST_GATE_REJECTED"
                        break
                elif gate_accepted and order_validated and internal_displayed:
                    result["titlePresentationVerdict"] = "VALIDATED_A_TO_M_TO_B"
                    break
                time.sleep(0.025)
        result["protocolEvidence"] = interesting
        if result["titlePresentationVerdict"] == "NOT_RUN":
            result["titlePresentationVerdict"] = "NOT_VALIDATED_PRESENTATION_TIMEOUT"
        write_json(result)
        log(f"R113_CONTROLLER_COMPLETE verdict={result['titlePresentationVerdict']} pair={pair_text} futureSourceLeaks={result['futureSourceLeaks']}")
        return 0 if result["titlePresentationVerdict"] == "VALIDATED_A_TO_M_TO_B" else 1
    except (StopRun, OSError, subprocess.SubprocessError) as error:
        result["abortReason"] = str(error)
        result["titlePresentationVerdict"] = "NOT_VALIDATED_SAFE_STOP"
        write_json(result)
        log(f"R113_CONTROLLER=SAFE_STOP reason={error}")
        return 1
    finally:
        if watcher and watcher.poll() is None:
            watcher.terminate()
            try:
                watcher.wait(timeout=1)
            except subprocess.TimeoutExpired:
                watcher.kill()
                watcher.wait(timeout=1)


if __name__ == "__main__":
    if len(sys.argv) not in (2, 3):
        print("usage: run_r113_physical_camera_once.py PID [ATTEMPT_NUMBER]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
