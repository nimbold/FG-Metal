#!/usr/bin/env python3
"""Run one R114 in-process target-latch title tuple.

The controller may only:
  - validate the exact session/layer/device;
  - arm the one-shot experiment before any pair is selected;
  - start bounded capture windows;
  - observe the physical mouse (listen-only) as independent motion evidence;
  - collect the observer's own reservation / frontier / gate / WSI evidence.

It never writes an exact A/B source pair: candidate selection belongs to the
observer process.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import subprocess
import sys
import time


ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
OUT_ROOT = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r114-inprocess-latch"
ATTEMPT_NUMBER = int(sys.argv[2]) if len(sys.argv) > 2 else 1
OUT = OUT_ROOT / f"attempt-{ATTEMPT_NUMBER:02d}"
RUNTIME = Path("/tmp/fgmetal-r108-unattended")
PID = int(sys.argv[1])
OBSERVER = RUNTIME / f"observer.{PID}.log"
MOUSE_WATCH = RUNTIME / "r114_physical_mouse_watch"
TARGET_CHECK = RUNTIME / "send_bounded_character_forward_to_game"
TRIGGER = RUNTIME / "start-title-gpu-capture"
CONDITION = RUNTIME / "capture-condition"
WORLD = RUNTIME / "r112-world-scene-confirmed"
ARM = RUNTIME / "r114-arm-next-moving-pair"
LEGACY_TARGET = RUNTIME / "r112-target-source-b"
CONTROLLER_LOG = OUT / "r114-controller.log"
MOUSE_LOG = OUT / "r114-physical-mouse-events.log"
RESULT_JSON = OUT / "r114-controller.json"
EXPECTED_GAME = "/Users/nima/Games/Elden Ring/Game/eldenring.exe"

ACQUISITION = re.compile(
    rf"R108_SOURCE_FRAME_ACQUIRED pid={PID} seq=(\d+) acquiredAtMonotonicNs=(\d+)"
)
CAPTURE_ARMED = re.compile(
    r"R108_TITLE_CAPTURE=ARMED window=(\d+) startSequence=(\d+) "
    r"expectedEndSequence=(\d+)"
)
MOUSE_EVENT = re.compile(
    r"R114_PHYSICAL_MOUSE_EVENT pid=(\d+) monotonicNs=(\d+) x=(-?\d+) y=(-?\d+) "
    r"deltaX=(-?\d+) deltaY=(-?\d+) withinGameWindow=(\d) frontmostPid=(-?\d+) "
    r"topWindowPid=(-?\d+) eventType=(\d+)"
)

PROTOCOL_TOKENS = (
    "R114_NEXT_MOVING_PAIR_ARMED",
    "R114_MANUAL_MOTION_READY",
    "R114_PROVIDER_WARM",
    "R114_MOTION_OBSERVED",
    "R114_NEXT_PAIR_RESERVED",
    "R114_NEXT_PAIR_RESERVE_SKIPPED",
    "R114_FRONTIER_CLOSE_AT_COMMIT",
    "R114_SOURCE_ADMISSION_CLASS",
    "R114_LATCH=",
    "R103_SOURCE_FRONTIER=",
    "R103_FRONTIER_CLOSE_REJECTED",
    "R102_SOURCE_TICKET=DEFERRED",
    "R102_WSI_DISPATCH",
    "R110_FAST_PRESENT_ELIGIBILITY=",
    "R110_PRESENT_M_READY",
    "R110_TITLE_WSI_SEQUENCE=",
    "R110_WSI_ORDER_EVENT",
    "R101_WSI_DISPATCH origin=INTERNAL",
    "R101_WSI_DISPLAYED origin=INTERNAL",
    "R102_INTERNAL_DRAWABLE_ACQUIRE",
    "R102_INTERNAL_DRAWABLE_ACQUIRED",
    "R102_INTERNAL_PRESENTED_CALLBACK",
    "R102_SOURCE_FORWARD_BEGIN",
    "R102_SOURCE_TICKET_RELEASED",
    "GPU_RESIDENT_CONTIGUOUS_A_TO_M_TO_B",
    "FIRST_REAL_TITLE_D3DMETAL_A_TO_M_TO_B",
    "R108_TITLE_CAPTURE=SOURCE_WINDOW_COMPLETE",
    "R108_METALFX_TRANSITION",
    "R108_PROVIDER_WARM_STATE=READY",
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
    if (f"pid={PID}" not in ready or "executable=eldenring.exe" not in ready
            or "window=ELDEN RING™" not in ready):
        raise StopRun("exact-game-window-check-failed")
    return ready


def latest_acquisition(contents: str) -> tuple[int, int]:
    matches = list(ACQUISITION.finditer(contents))
    if not matches:
        raise StopRun("no-live-source-acquisitions")
    match = matches[-1]
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    result: dict[str, object] = {
        "schema": "fgmetal-r114-inprocess-target-latch-once-v1",
        "gamePid": PID,
        "attemptNumber": ATTEMPT_NUMBER,
        "observerLog": str(OBSERVER),
        "manualPhysicalInputOnly": True,
        "syntheticGameplayInputSent": False,
        "externalExactPairSelection": False,
        "titlePresentationVerdict": "NOT_RUN",
        "sourceLeaksWhileFrontierClosed": 0,
        "missedTargetAdmissions": 0,
        "preFrontierAdmissions": 0,
    }
    watcher: subprocess.Popen[str] | None = None
    try:
        if not OBSERVER.is_file() or not TARGET_CHECK.is_file() or not MOUSE_WATCH.is_file():
            raise StopRun("observer-or-read-only-input-tools-missing")
        try:
            verify_line = verify_target()
        except (StopRun, OSError, subprocess.SubprocessError) as window_error:
            verify_line = f"window-check-unavailable:{window_error}"
            result["windowCheckUnavailable"] = str(window_error)
        observer_text = OBSERVER.read_text(encoding="utf-8", errors="replace")
        loaded_lines = [line for line in observer_text.splitlines()
                        if line.startswith(f"LOADED pid={PID} ")]
        if not loaded_lines:
            raise StopRun("exact-pid-has-no-loaded-observer")
        loaded = loaded_lines[0]
        if ("target=/Users/nima/Games/Elden Ring/Game/eldenring.exe targetArgMatch=1" not in loaded
                or "presentation=1" not in loaded
                or "framebufferOnly=1 titleCapture=1" not in loaded
                or "r114InprocessLatch=1" not in loaded):
            raise StopRun("observer-mode-or-executable-identity-mismatch")
        layer_lines = [line for line in observer_text.splitlines()
                       if line.startswith(f"TITLE_LAYER_SELECTED pid={PID} ")]
        if (not layer_lines or "deviceName=Apple M3" not in layer_lines[0]
                or "registryID=4294968554" not in layer_lines[0]):
            raise StopRun("exact-cametal-layer-or-device-not-verified")
        latest, acquired_ns = latest_acquisition(observer_text)
        if monotonic_ns() - acquired_ns > 10_000_000_000:
            raise StopRun("source-acquisition-is-stale")
        result.update({
            "exactGameWindowCheck": verify_line,
            "observerLoadedLine": loaded,
            "cametalLayerLine": layer_lines[0],
            "lastPreCaptureSourceSequence": latest,
        })
        log(f"R114_PREFLIGHT=READY pid={PID} executable={EXPECTED_GAME} window=ELDEN RING™ "
            f"layerAndDevice=VERIFIED liveSourceSequence={latest} "
            f"inProcessLatch=1 externalPairSelection=NONE")

        for path in (TRIGGER, ARM, LEGACY_TARGET):
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
            result["physicalMouseMonitor"] = "unavailable:no-stdout"
            watcher = None
            mouse_ready_line = "unavailable"
        ready_deadline = time.monotonic() + 4
        mouse_ready_line = ""
        while time.monotonic() < ready_deadline:
            readable, _, _ = select.select([watcher.stdout], [], [], 0.1)
            if readable:
                line = watcher.stdout.readline().strip()
                if line.startswith("R114_MOUSE_MONITOR=READY"):
                    mouse_ready_line = line
                    break
                if line.startswith("R114_MOUSE_MONITOR=BLOCKED"):
                    result["physicalMouseMonitor"] = line
                    log(f"R114_PHYSICAL_MOUSE_MONITOR=BLOCKED continuing=1 detail={line}")
                    try:
                        watcher.terminate()
                        watcher.wait(timeout=1)
                    except (subprocess.TimeoutExpired, OSError):
                        watcher.kill()
                    watcher = None
                    mouse_ready_line = "unavailable:blocked"
                    break
            if watcher.poll() is not None:
                error = watcher.stderr.read().strip() if watcher.stderr else ""
                result["physicalMouseMonitor"] = f"unavailable:exited:{error}"
                log(f"R114_PHYSICAL_MOUSE_MONITOR=EXITED continuing=1 detail={error}")
                watcher = None
                mouse_ready_line = "unavailable:exited"
                break
        if not mouse_ready_line:
            result["physicalMouseMonitor"] = "unavailable:readiness-timeout"
            mouse_ready_line = "unavailable:readiness-timeout"
        if watcher is not None and not mouse_ready_line.startswith("R114_MOUSE_MONITOR=READY"):
            try:
                watcher.terminate()
                watcher.wait(timeout=1)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    watcher.kill()
                except OSError:
                    pass
            watcher = None
        if mouse_ready_line.startswith("R114_MOUSE_MONITOR=READY") and f"centerTopWindowPid={PID}" not in mouse_ready_line:
            result["physicalMouseMonitor"] = "unavailable:window-not-topmost"
            mouse_ready_line = "unavailable:window-not-topmost"
            if watcher is not None:
                watcher.terminate()
                watcher = None
        MOUSE_LOG.write_text(mouse_ready_line + "\n", encoding="utf-8")
        result["physicalMouseMonitorReady"] = mouse_ready_line
        if not mouse_ready_line.startswith("R114_MOUSE_MONITOR=READY"):
            log("R114_PHYSICAL_MOUSE_MONITOR=UNAVAILABLE continuing=1 evidence=observer-motion-field-only")
        else:
            result["physicalMouseMonitor"] = "ready"

        armed_line = ""
        reserved_pair: tuple[int, int] | None = None
        protocol: list[str] = []
        seen: set[str] = set()
        renewals = 0
        mouse_events = 0
        cumulative_abs_dx = 0
        first_mouse_ns = 0
        window_ordinal = 0

        with OBSERVER.open("rb") as log_stream:
            log_stream.seek(0, os.SEEK_END)
            # Wait for the exact game process to be frontmost before arming, so
            # bounded capture windows are never consumed while the user is on
            # another desktop reading the controller log.
            front_deadline = time.monotonic() + 180
            front_seen = False
            log("R114_WAITING_FOR_GAME_FRONTMOST pid=%d timeoutSeconds=180" % PID)
            while time.monotonic() < front_deadline:
                try:
                    front = subprocess.run(["/tmp/frontmost"], capture_output=True,
                                           text=True, timeout=3).stdout.strip()
                except (OSError, subprocess.SubprocessError):
                    front = ""
                if front == str(PID):
                    front_seen = True
                    break
                time.sleep(0.5)
            result["gameFrontmostBeforeArm"] = front_seen
            log(f"R114_GAME_FRONTMOST={1 if front_seen else 0} pid={PID}")
            if not front_seen:
                raise StopRun("game-never-became-frontmost-before-arm")

            # One-shot arm request: this only starts the experiment. It does not
            # name any source sequence.
            ARM.write_text("R114 one-shot in-process next-moving-pair arm\n", encoding="utf-8")
            result["armRequestMonotonicNs"] = monotonic_ns()
            TRIGGER.write_text("R114 bounded capture window 1\n", encoding="utf-8")
            log(f"R114_ARM_REQUESTED pid={PID} source=controller-pre-motion externalPairSelection=NONE")

            deadline = time.monotonic() + 150
            gate_accepted = gate_rejected = False
            deferred = internal_dispatch = internal_displayed = False
            order_validated = latch_consumed = False
            while time.monotonic() < deadline:
                watch_streams = [log_stream] + ([watcher.stdout] if watcher else [])
                readable, _, _ = select.select(watch_streams, [], [], 0.02)
                if log_stream in readable:
                    chunk = log_stream.read()
                    if chunk:
                        for raw in chunk.splitlines():
                            line = raw.decode("utf-8", errors="replace")
                            if any(token in line for token in PROTOCOL_TOKENS) and line not in seen:
                                seen.add(line)
                                protocol.append(line)
                                if "R114_NEXT_MOVING_PAIR_ARMED" in line:
                                    armed_line = line
                                    result["armLine"] = line
                                    log(line)
                                if "R114_MANUAL_MOTION_READY" in line:
                                    result["manualMotionReadyMonotonicNs"] = monotonic_ns()
                                    log("R114_MANUAL_MOTION_READY_OBSERVED")
                                if "R114_NEXT_PAIR_RESERVED" in line:
                                    match = re.search(r"pair=(\d+)->(\d+)", line)
                                    if match:
                                        reserved_pair = (int(match.group(1)), int(match.group(2)))
                                        result["reservedPair"] = {
                                            "sourceA": reserved_pair[0],
                                            "sourceB": reserved_pair[1],
                                            "line": line,
                                        }
                                    log(line)
                                if "R114_SOURCE_ADMISSION_CLASS=SOURCE_LEAK_WHILE_FRONTIER_CLOSED" in line:
                                    result["sourceLeaksWhileFrontierClosed"] = int(
                                        result["sourceLeaksWhileFrontierClosed"]) + 1
                                if "R114_SOURCE_ADMISSION_CLASS=MISSED_TARGET_ADMISSION" in line:
                                    result["missedTargetAdmissions"] = int(
                                        result["missedTargetAdmissions"]) + 1
                                if "R110_FAST_PRESENT_ELIGIBILITY=ACCEPTED" in line:
                                    gate_accepted = True
                                if "R110_FAST_PRESENT_ELIGIBILITY=REJECTED" in line:
                                    gate_rejected = True
                                if "R102_SOURCE_TICKET=DEFERRED" in line:
                                    deferred = True
                                if "R101_WSI_DISPATCH origin=INTERNAL" in line:
                                    internal_dispatch = True
                                if ("R101_WSI_DISPLAYED origin=INTERNAL" in line
                                        and "reachedDisplay=1" in line):
                                    internal_displayed = True
                                if "R110_TITLE_WSI_SEQUENCE=VALIDATED" in line:
                                    order_validated = True
                                if "R114_LATCH=CONSUMED" in line:
                                    latch_consumed = True
                                if ("R108_TITLE_CAPTURE=SOURCE_WINDOW_COMPLETE" in line
                                        and reserved_pair is None and renewals < 11):
                                    renewals += 1
                                    TRIGGER.write_text(
                                        f"R114 bounded capture window renewal {renewals}\n",
                                        encoding="utf-8")
                                    log(f"R114_CAPTURE_WINDOW_RENEWAL={renewals}")
                                match = CAPTURE_ARMED.search(line)
                                if match:
                                    window_ordinal = int(match.group(1))
                                    result["captureWindow"] = {
                                        "ordinal": window_ordinal,
                                        "startSequence": int(match.group(2)),
                                        "endSequence": int(match.group(3)),
                                    }
                if watcher and watcher.stdout in readable:
                    raw_event = watcher.stdout.readline().strip()
                    with MOUSE_LOG.open("a", encoding="utf-8") as mouse_stream:
                        mouse_stream.write(raw_event + "\n")
                    event = MOUSE_EVENT.search(raw_event)
                    if (event and int(event.group(1)) == PID and event.group(7) == "1"
                            and int(event.group(9)) == PID):
                        mouse_events += 1
                        cumulative_abs_dx += abs(int(event.group(5)))
                        if not first_mouse_ns:
                            first_mouse_ns = int(event.group(2))
                if watcher and watcher.poll() is not None:
                    error = watcher.stderr.read().strip() if watcher.stderr else ""
                    log(f"R114_PHYSICAL_MOUSE_MONITOR=EXITED error={error} continuing=1")
                    watcher = None

                if latch_consumed and internal_displayed and order_validated:
                    result["titlePresentationVerdict"] = "VALIDATED_A_TO_M_TO_B"
                    break
                if gate_rejected and reserved_pair is not None:
                    # Let the accepted recovery forward retained B, reopen the
                    # frontier and admit SOURCE C before stopping.
                    all_text = OBSERVER.read_text(encoding="utf-8", errors="replace")
                    recovered = (
                        "R114_LATCH=FAILED" in all_text
                        and "R103_SOURCE_FRONTIER=OPEN" in all_text
                    )
                    if recovered:
                        result["titlePresentationVerdict"] = "NOT_VALIDATED_FAST_GATE_REJECTED"
                        break

        result["protocolEvidence"] = protocol
        result["physicalMouseEventCount"] = mouse_events
        result["physicalCumulativeAbsDeltaX"] = cumulative_abs_dx
        result["firstPhysicalMouseEventMonotonicNs"] = first_mouse_ns
        result["captureWindowRenewals"] = renewals
        result["gateAccepted"] = gate_accepted
        result["gateRejected"] = gate_rejected
        result["sourceBDeferred"] = deferred
        result["internalWsiDispatch"] = internal_dispatch
        result["internalDisplayed"] = internal_displayed
        result["titleWsiOrderValidated"] = order_validated
        result["latchConsumed"] = latch_consumed
        if result["titlePresentationVerdict"] == "NOT_RUN":
            if reserved_pair is None:
                result["titlePresentationVerdict"] = "NOT_VALIDATED_NO_IN_PROCESS_RESERVATION"
            else:
                result["titlePresentationVerdict"] = "NOT_VALIDATED_PRESENTATION_TIMEOUT"
        write_json(result)
        log(f"R114_CONTROLLER_COMPLETE verdict={result['titlePresentationVerdict']} "
            f"reservedPair={reserved_pair} leaksWhileClosed={result['sourceLeaksWhileFrontierClosed']}")
        return 0 if result["titlePresentationVerdict"] == "VALIDATED_A_TO_M_TO_B" else 1
    except (StopRun, OSError, subprocess.SubprocessError) as error:
        result["abortReason"] = str(error)
        result["titlePresentationVerdict"] = "NOT_VALIDATED_SAFE_STOP"
        write_json(result)
        log(f"R114_CONTROLLER=SAFE_STOP reason={error}")
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
        print("usage: run_r114_inprocess_latch_once.py PID [ATTEMPT_NUMBER]", file=sys.stderr)
        raise SystemExit(2)
    raise SystemExit(main())
