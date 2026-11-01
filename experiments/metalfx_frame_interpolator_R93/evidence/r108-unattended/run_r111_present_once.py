#!/usr/bin/env python3
"""Run one bounded event-locked Elden Ring camera A→M→B candidate."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import time


ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
BASE = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended"
RUN_DIR = BASE / "run-04-r111-event-locked-title-presentation"
RUNTIME_DIR = Path("/tmp/fgmetal-r108-unattended")
CONDITION_FILE = RUNTIME_DIR / "capture-condition"
CAPTURE_TRIGGER = RUNTIME_DIR / "start-title-gpu-capture"
TARGET_FILE = RUNTIME_DIR / "r111-target-source-b"
WORLD_SCENE_CONFIRMATION = RUNTIME_DIR / "r111-world-scene-confirmed"
ATTRIBUTION_FILE = RUNTIME_DIR / "r111-camera-attribution-verified"
HELPER = RUNTIME_DIR / "send_bounded_input_to_game"
CONDITION = "CAMERA"
ACTION = "camera-right-stream"
WARMUP_DEADLINE_SECONDS = 35.0
INPUT_START_DEADLINE_SECONDS = 6.0
SOURCE_A_DEADLINE_SECONDS = 2.0
SOURCE_B_DEADLINE_SECONDS = 0.75
PAIR_PROCESSING_DEADLINE_SECONDS = 25.0
POST_ORDER_DEADLINE_SECONDS = 12.0
MAX_ACQUISITIONS_BEFORE_A = 8
MAX_ACQUISITIONS_AFTER_A = 4

ACQUISITION = re.compile(
    r"R108_SOURCE_FRAME_ACQUIRED pid=(\d+) seq=(\d+) acquiredAtMonotonicNs=(\d+)"
)
INPUT_FIRST = re.compile(
    r"R108_INPUT_TO_GAME=FIRST_EVENT pid=(\d+) firstEventMonotonicNs=(\d+)"
)
INPUT_EVENT = re.compile(
    r"R108_INPUT_TO_GAME=EVENT pid=(\d+) action=camera-right-stream "
    r"eventIndex=(\d+) postedMonotonicNs=(\d+) deltaPx=(-?\d+)"
)
CAPTURE_ARMED = re.compile(
    r"R108_TITLE_CAPTURE=ARMED window=(\d+) .*startSequence=(\d+) "
    r"expectedEndSequence=(\d+)"
)
PROVIDER_READY = re.compile(
    r"R108_PROVIDER_WARM_STATE=READY .*?nextExpectedEligiblePair=(\d+)->(\d+) "
    r"condition=CAMERA .*?internalPresentId=(\d+) eventTimestampMonotonicNs=(\d+)"
)


class AttemptError(RuntimeError):
    pass


def monotonic_ns() -> int:
    return time.clock_gettime_ns(time.CLOCK_MONOTONIC)


def append_log(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")


def atomic_text(path: Path, value: str) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding="utf-8")
    os.replace(temporary, path)


def atomic_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def helper_run(action: str, expected_pid: int | None = None) -> str:
    command = [str(HELPER), action]
    if expected_pid is not None:
        command.append(str(expected_pid))
    try:
        completed = subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=6
        )
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise AttemptError(f"bounded-input-helper-{action}-failed:{detail.strip()}") from error
    return completed.stdout.strip()


def verified_pid() -> int:
    ready = helper_run("check")
    match = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", ready)
    if not match:
        raise AttemptError("safe-target-check-returned-no-pid")
    return int(match.group(1))


def observer_for_pid(pid: int) -> Path:
    path = RUNTIME_DIR / f"observer.{pid}.log"
    try:
        os.kill(pid, 0)
        stat = path.stat()
        contents = path.read_bytes()
    except OSError as error:
        raise AttemptError("verified-window-owner-has-no-live-observer-log") from error
    head = contents[:65_536].decode("utf-8", errors="replace")
    tail = contents[-262_144:].decode("utf-8", errors="replace")
    joined = head + tail
    if not re.search(rf"TITLE_LAYER_SELECTED pid={pid} ", joined):
        raise AttemptError("observer-log-does-not-bind-to-verified-window-owner")
    if "HOOK_INSTALLED" not in head:
        raise AttemptError("observer-hooks-not-confirmed-in-process")
    if not re.search(
        r"LOADED pid=" + str(pid) + r" .*?presentation=1 r111CameraAttribution=1 "
        r".*?framebufferOnly=1 titleCapture=1",
        head,
    ):
        raise AttemptError("observer-log-does-not-confirm-r111-presentation-capture-mode")
    latest = re.findall(
        rf"R108_SOURCE_FRAME_ACQUIRED pid={pid} seq=\d+ acquiredAtMonotonicNs=(\d+)",
        tail,
    )
    if not latest or monotonic_ns() - int(latest[-1]) > 10_000_000_000:
        raise AttemptError("observer-has-no-recent-source-acquisition-for-verified-pid")
    if time.time() - stat.st_mtime > 10:
        raise AttemptError("observer-source-acquisition-is-stale")
    return path


def file_sha256(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class LiveStreams:
    def __init__(self, observer_stream, pid: int, input_process: subprocess.Popen[bytes]):
        self.observer = observer_stream
        self.input_process = input_process
        self.pid = pid
        self.observer_buffer = b""
        self.input_buffer = b""
        self.observed: list[str] = []
        self.input_lines: list[str] = []
        self.acquisitions: list[dict[str, int]] = []
        self.camera_events: list[dict[str, int]] = []
        self.first_event_ns: int | None = None
        self.input_bound_reached = False
        self.input_error_lines: list[str] = []

    def _observer_line(self, line: str) -> None:
        self.observed.append(line)
        match = ACQUISITION.search(line)
        if match and int(match.group(1)) == self.pid:
            self.acquisitions.append({
                "sequence": int(match.group(2)),
                "acquiredAtMonotonicNs": int(match.group(3)),
            })

    def _input_line(self, line: str) -> None:
        self.input_lines.append(line)
        first = INPUT_FIRST.search(line)
        if first:
            if int(first.group(1)) != self.pid:
                self.input_error_lines.append(line)
            else:
                self.first_event_ns = int(first.group(2))
        event = INPUT_EVENT.search(line)
        if event:
            if int(event.group(1)) != self.pid:
                self.input_error_lines.append(line)
            else:
                self.camera_events.append({
                    "eventIndex": int(event.group(2)),
                    "postedMonotonicNs": int(event.group(3)),
                    "deltaPx": int(event.group(4)),
                })
        if "R108_INPUT_TO_GAME=STREAM_BOUND_REACHED" in line:
            self.input_bound_reached = True

    def pump(self) -> list[str]:
        newly_observed: list[str] = []
        chunk = self.observer.read()
        if chunk:
            self.observer_buffer += chunk
            while b"\n" in self.observer_buffer:
                raw, self.observer_buffer = self.observer_buffer.split(b"\n", 1)
                line = raw.decode("utf-8", errors="replace")
                self._observer_line(line)
                newly_observed.append(line)

        stream = self.input_process.stdout
        if stream is not None:
            try:
                readable, _, _ = select.select([stream], [], [], 0)
                if readable:
                    data = os.read(stream.fileno(), 65_536)
                    if data:
                        self.input_buffer += data
                        while b"\n" in self.input_buffer:
                            raw, self.input_buffer = self.input_buffer.split(b"\n", 1)
                            self._input_line(raw.decode("utf-8", errors="replace"))
            except (OSError, ValueError):
                pass
        return newly_observed


def run_attempt() -> dict[str, object]:
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    controller_log = RUN_DIR / "r111-event-locked-controller.log"
    result: dict[str, object] = {
        "schema": "fgmetal-r111-event-locked-present-controller-v1",
        "condition": CONDITION,
        "action": ACTION,
        "cameraInputActive": False,
        "successfulCameraEvents": [],
        "futureSourceLeaks": 0,
        "presentationVerdict": "NOT_RUN",
    }
    input_process: subprocess.Popen[bytes] | None = None
    streams: LiveStreams | None = None

    def state(event: str, **fields: object) -> None:
        line = "CAMERA_INPUT_" + event + (" " + " ".join(f"{key}={value}" for key, value in fields.items()) if fields else "")
        append_log(controller_log, line)

    def terminate_input(reason: str, source_boundary_ns: int | None = None) -> int:
        if input_process is None:
            return source_boundary_ns or 0
        if input_process.poll() is None:
            input_process.terminate()
        try:
            input_process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            input_process.kill()
            input_process.wait(timeout=0.5)
        if streams is not None:
            streams.pump()
        stopped_ns = monotonic_ns() if source_boundary_ns is not None else 0
        state_fields = {"reason": reason}
        if stopped_ns:
            state_fields["observerClockMonotonicNs"] = stopped_ns
        else:
            state_fields["controllerHostMonotonicNs"] = monotonic_ns()
        state("ACTIVE_STOPPED", **state_fields)
        result["cameraInputActive"] = False
        if stopped_ns:
            result["cameraInputStoppedAtMonotonicNs"] = stopped_ns
        return stopped_ns

    try:
        if not HELPER.is_file():
            raise AttemptError("input-helper-not-built")
        pid = verified_pid()
        observer = observer_for_pid(pid)
        result.update({"gamePid": pid, "observerLog": str(observer)})

        WORLD_SCENE_CONFIRMATION.write_text("VISIBLE_WORLD\n", encoding="utf-8")
        for stale in (CAPTURE_TRIGGER, TARGET_FILE, ATTRIBUTION_FILE):
            if stale.exists():
                stale.unlink()
        CONDITION_FILE.write_text(f"{CONDITION}\n", encoding="utf-8")

        with observer.open("rb") as source:
            source.seek(0, os.SEEK_END)
            CAPTURE_TRIGGER.write_text("R111 one-shot event-locked capture\n", encoding="utf-8")
            deadline = time.monotonic() + WARMUP_DEADLINE_SECONDS
            observed: list[str] = []
            window: dict[str, int] | None = None
            provider_line: str | None = None
            latest_acquisition_sequence = 0
            while time.monotonic() < deadline and (window is None or provider_line is None):
                chunk = source.read()
                if chunk:
                    for raw in chunk.splitlines():
                        line = raw.decode("utf-8", errors="replace")
                        observed.append(line)
                        acquisition = ACQUISITION.search(line)
                        if acquisition and int(acquisition.group(1)) == pid:
                            latest_acquisition_sequence = int(acquisition.group(2))
                        armed = CAPTURE_ARMED.search(line)
                        if armed:
                            window = {
                                "window": int(armed.group(1)),
                                "startSequence": int(armed.group(2)),
                                "endSequence": int(armed.group(3)),
                            }
                            window["line"] = line  # type: ignore[assignment]
                        warm = PROVIDER_READY.search(line)
                        if warm:
                            provider_line = line
                            result["providerWarmup"] = {
                                "line": line,
                                "nextPair": [int(warm.group(1)), int(warm.group(2))],
                                "internalPresentId": int(warm.group(3)),
                                "eventTimestampMonotonicNs": int(warm.group(4)),
                            }
                time.sleep(0.01)
            if window is None:
                raise AttemptError("capture-window-not-armed-before-warmup-deadline")
            if provider_line is None:
                raise AttemptError("provider-did-not-generate-warm-internal-frame")
            remaining = window["endSequence"] - latest_acquisition_sequence
            result["captureWindow"] = window
            result["sourceRunwayAfterWarmup"] = remaining
            if remaining < 12:
                raise AttemptError("insufficient-source-runway-after-provider-warmup")

        live_observer = observer.open("rb")
        live_observer.seek(0, os.SEEK_END)
        input_process = subprocess.Popen(
            [str(HELPER), ACTION, str(pid)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        streams = LiveStreams(live_observer, pid, input_process)
        start_deadline = time.monotonic() + INPUT_START_DEADLINE_SECONDS
        while streams.first_event_ns is None and time.monotonic() < start_deadline:
            streams.pump()
            if input_process.poll() is not None:
                stderr = input_process.stderr.read().decode("utf-8", errors="replace") if input_process.stderr else ""
                raise AttemptError(f"camera-helper-ended-before-first-event:{stderr.strip()}")
            time.sleep(0.002)
        if streams.first_event_ns is None:
            raise AttemptError("camera-helper-first-event-timeout")
        active_start_ns = streams.first_event_ns
        result["cameraInputStartedAtMonotonicNs"] = active_start_ns
        result["cameraInputActive"] = True
        state("ACTIVE_STARTED", firstSuccessfulEventMonotonicNs=active_start_ns,
              pid=pid, route="CGEventPostFrontmostVerified")

        source_a: dict[str, int] | None = None
        source_b: dict[str, int] | None = None
        target_arm_line: str | None = None
        a_deadline = time.monotonic() + SOURCE_A_DEADLINE_SECONDS
        b_deadline: float | None = None
        acquisitions_after_input_start = 0
        next_line_index = 0

        while source_b is None:
            new_lines = streams.pump()
            while next_line_index < len(streams.observed):
                line = streams.observed[next_line_index]
                next_line_index += 1
                if "R110_PRESENT_TARGET=ARMED" in line:
                    target_arm_line = line
                for leak in re.finditer(r"R103_FUTURE_SOURCE_LEAK=DETECTED", line):
                    result["futureSourceLeaks"] = int(result["futureSourceLeaks"]) + 1
                match = ACQUISITION.search(line)
                if not match or int(match.group(1)) != pid:
                    continue
                acquired = {
                    "sequence": int(match.group(2)),
                    "acquiredAtMonotonicNs": int(match.group(3)),
                }
                if acquired["acquiredAtMonotonicNs"] > active_start_ns:
                    acquisitions_after_input_start += 1
                if source_a is None and acquired["acquiredAtMonotonicNs"] > active_start_ns:
                    source_a = acquired
                    source_b_sequence = source_a["sequence"] + 1
                    result["sourceA"] = source_a
                    result["candidateSourceBSequence"] = source_b_sequence
                    result["sourceAcquisitionsAfterInputStartBeforeA"] = acquisitions_after_input_start
                    state("SOURCE_A_SELECTED", sequence=source_a["sequence"],
                          acquiredAtMonotonicNs=source_a["acquiredAtMonotonicNs"])
                    atomic_text(TARGET_FILE, f"{source_b_sequence}\n")
                    b_deadline = time.monotonic() + SOURCE_B_DEADLINE_SECONDS
                    continue
                if source_a is None:
                    continue
                if acquired["sequence"] == source_a["sequence"] + 1:
                    source_b = acquired
                    result["sourceB"] = source_b
                    active_stop_ns = terminate_input(
                        "exact-source-B-acquired",
                        acquired["acquiredAtMonotonicNs"],
                    )
                    result["cameraInputActiveInterval"] = {
                        "startMonotonicNs": active_start_ns,
                        "stopObservedMonotonicNs": active_stop_ns,
                    }
                    break
                if acquired["sequence"] > source_a["sequence"] + 1:
                    terminate_input("next-acquired-source-was-not-consecutive")
                    raise AttemptError("first-source-after-A-was-not-consecutive")

            if source_b is not None:
                break
            if source_a is None:
                if acquisitions_after_input_start >= MAX_ACQUISITIONS_BEFORE_A or time.monotonic() >= a_deadline:
                    terminate_input("source-A-bound-exhausted")
                    raise AttemptError("no-source-A-within-bounded-live-acquisitions")
            else:
                later_count = sum(1 for item in streams.acquisitions
                                  if item["sequence"] > source_a["sequence"])
                if later_count >= MAX_ACQUISITIONS_AFTER_A or (
                    b_deadline is not None and time.monotonic() >= b_deadline
                ):
                    terminate_input("source-B-bound-exhausted")
                    raise AttemptError("no-consecutive-source-B-within-bound")
            if input_process.poll() is not None:
                streams.pump()
                if source_b is None:
                    terminate_input("bounded-camera-event-stream-ended-before-source-B")
                    raise AttemptError("camera-input-bound-ended-before-exact-source-B")
            time.sleep(0.002)

        result["targetArmedLine"] = target_arm_line
        exact_pair = f"{source_a['sequence']}->{source_b['sequence']}"
        result["exactPair"] = exact_pair
        input_by_ns = [item["postedMonotonicNs"] for item in streams.camera_events]
        between_events = [timestamp for timestamp in input_by_ns
                          if source_a["acquiredAtMonotonicNs"] < timestamp < source_b["acquiredAtMonotonicNs"]]
        try:
            os.kill(pid, 0)
            pid_alive_after_b = True
        except OSError:
            pid_alive_after_b = False
        identity_stable = pid_alive_after_b and not streams.input_error_lines
        attribution_verified = bool(
            source_b["sequence"] == source_a["sequence"] + 1
            and active_start_ns < source_a["acquiredAtMonotonicNs"]
            and between_events
            and source_b["acquiredAtMonotonicNs"] <= result["cameraInputActiveInterval"]["stopObservedMonotonicNs"]
            and identity_stable
        )
        result["successfulCameraEvents"] = streams.camera_events
        result["inputLogTail"] = streams.input_lines[-80:]
        result["cameraEventsStrictlyBetweenAandB"] = between_events
        result["gamePidAliveAfterB"] = pid_alive_after_b
        result["exactProcessAndWindowStillValidAfterB"] = identity_stable
        result["cameraPairAttribution"] = "VERIFIED" if attribution_verified else "REJECTED"
        if attribution_verified:
            marker = (
                f"pid={pid} A={source_a['sequence']} B={source_b['sequence']} "
                f"activeStartNs={active_start_ns} "
                f"sourceAAcquiredNs={source_a['acquiredAtMonotonicNs']} "
                f"cameraEventNs={between_events[0]} "
                f"sourceBAcquiredNs={source_b['acquiredAtMonotonicNs']} "
                f"activeStopNs={result['cameraInputActiveInterval']['stopObservedMonotonicNs']}\n"
            )
            atomic_text(ATTRIBUTION_FILE, marker)
            result["cameraAttributionMarker"] = str(ATTRIBUTION_FILE)
        else:
            append_log(controller_log, "CAMERA_PAIR_ATTRIBUTION=REJECTED pair=" + exact_pair)

        # Publish the pair timing proof before the fast presentation gate. The
        # full window-server identity check runs only after this atomic marker
        # exists, so that its window enumeration cannot make attribution late.
        try:
            identity_after_b = helper_run("check", pid)
            result["postBCameraIdentityCheck"] = identity_after_b
            exact_identity_after_b = bool(
                re.search(rf"(?:^|\s)pid={pid}(?:\s|$)", identity_after_b)
                and "window=ELDEN RING™" in identity_after_b
            )
            result["exactProcessAndWindowStillValidAfterB"] = exact_identity_after_b
            if not exact_identity_after_b:
                result["postBCameraIdentityCheckError"] = "post-B exact window identity did not match"
        except AttemptError as error:
            result["postBCameraIdentityCheckError"] = str(error)
            result["exactProcessAndWindowStillValidAfterB"] = False

        processing_deadline = time.monotonic() + PAIR_PROCESSING_DEADLINE_SECONDS
        line_cursor = next_line_index
        while time.monotonic() < processing_deadline:
            streams.pump()
            while line_cursor < len(streams.observed):
                line = streams.observed[line_cursor]
                line_cursor += 1
                if "R102_SOURCE_TICKET_RELEASED" in line and f"sourceSequence={source_b['sequence']} " in line:
                    result["sourceTicketReleasedLine"] = line
                if "DRAWABLE_PRODUCER_COMPLETED" in line and f"seq={source_b['sequence']} " in line:
                    result["sourceBProducerCompletedLine"] = line
                if "R108_SOURCE_PRODUCER_COMMIT sourceSequence=" in line and f"sourceSequence={source_b['sequence']} " in line:
                    result["sourceBProducerCommitLine"] = line
                if "R110_SOURCE_PRODUCER_COMMIT_RETURN" in line and f"sequence={source_b['sequence']} " in line:
                    result["sourceBProducerCommitReturnLine"] = line
                if "R102_EVENT" in line and "event=SOURCE_B_WSI_DISPATCH" in line and f"sourceSequence={source_b['sequence']} " in line:
                    result["sourceBWsiDispatchLine"] = line
                if f"pair={exact_pair} " not in line:
                    continue
                if "R110_PRESENT_TARGET=ARMED" in line:
                    target_arm_line = line
                if "R103_SOURCE_FRONTIER=CLOSED" in line and f"pair={exact_pair} " in line:
                    result["sourceFrontierClosedLine"] = line
                if "R102_SOURCE_TICKET=DEFERRED" in line and f"pair={exact_pair} " in line:
                    result["sourceBDeferredLine"] = line
                if "R110_FAST_PRESENT_ELIGIBILITY=" in line:
                    result["fastMotionGateLine"] = line
                if "R110_PRESENT_M_READY" in line:
                    result["internalMReadyLine"] = line
                if "R110_TITLE_WSI_SEQUENCE=" in line:
                    result["titleWsiSequenceLine"] = line
                if "R110_METALFX_DIAGNOSTIC=" in line:
                    result["postAttemptMetalFxDiagnosticLine"] = line
                if "R103_SOURCE_FRONTIER=OPEN" in line:
                    result["frontierOpenLine"] = line
                if "R103_FUTURE_SOURCE_LEAK=DETECTED" in line:
                    result["futureSourceLeaks"] = int(result["futureSourceLeaks"]) + 1
            gate = result.get("fastMotionGateLine")
            if gate and "R110_FAST_PRESENT_ELIGIBILITY=REJECTED" in str(gate):
                if result.get("sourceTicketReleasedLine") and result.get("frontierOpenLine"):
                    result["presentationVerdict"] = "FALLBACK_FAST_GATE_REJECTED"
                    break
            if result.get("titleWsiSequenceLine") and "=VALIDATED" in str(result["titleWsiSequenceLine"]):
                result["presentationVerdict"] = "A_TO_M_TO_B_VALIDATED"
                break
            time.sleep(0.003)
        if result["presentationVerdict"] == "NOT_RUN":
            result["presentationVerdict"] = "PROCESSING_OR_PRESENTATION_TIMEOUT"

        # Capture WSI identities and display evidence through SOURCE C/reopen.
        order_deadline = time.monotonic() + POST_ORDER_DEADLINE_SECONDS
        line_cursor = 0
        source_wsi: dict[str, str] = {}
        internal_wsi: str | None = None
        internal_callback: str | None = None
        source_b_dispatch: str | None = None
        internal_acquired: str | None = None
        current_source_c: str | None = None
        internal_displayed: str | None = None
        c_sequence = source_b["sequence"] + 1
        while time.monotonic() < order_deadline:
            streams.pump()
            while line_cursor < len(streams.observed):
                line = streams.observed[line_cursor]
                line_cursor += 1
                if "R110_WSI_ORDER_EVENT origin=SOURCE" in line:
                    sequence_match = re.search(r"sourceSequence=(\d+)", line)
                    if sequence_match:
                        sequence = int(sequence_match.group(1))
                        if sequence in (source_a["sequence"], source_b["sequence"], c_sequence):
                            source_wsi[str(sequence)] = line
                            if sequence == c_sequence:
                                current_source_c = line
                if "R110_WSI_ORDER_EVENT origin=INTERNAL" in line and f"pair={exact_pair} " in line:
                    internal_wsi = line
                if "R110_METALFX_DIAGNOSTIC=" in line and f"pair={exact_pair} " in line:
                    result["postAttemptMetalFxDiagnosticLine"] = line
                if "R102_SOURCE_TICKET=DEFERRED" in line and f"pair={exact_pair} " in line:
                    result["sourceBDeferredLine"] = line
                if "R103_SOURCE_FRONTIER=CLOSED" in line and f"pair={exact_pair} " in line:
                    result["sourceFrontierClosedLine"] = line
                if "R102_SOURCE_TICKET_RELEASED" in line and f"pair={exact_pair} " in line:
                    result["sourceTicketReleasedLine"] = line
                if "R103_SOURCE_FRONTIER=OPEN" in line and f"pair={exact_pair} " in line:
                    result["frontierOpenLine"] = line
                if "R103_FUTURE_SOURCE_LEAK=DETECTED" in line:
                    result["futureSourceLeaks"] = int(result["futureSourceLeaks"]) + 1
                if "R102_INTERNAL_DRAWABLE_ACQUIRED" in line and f"sourcePair={exact_pair} " in line:
                    internal_acquired = line
                if "R102_INTERNAL_PRESENTED_CALLBACK" in line and f"sourcePair={exact_pair} " in line:
                    internal_callback = line
                if "R101_WSI_DISPLAYED origin=INTERNAL" in line:
                    wsi = re.search(r"wsiPresentId=(\d+)", line)
                    if wsi and internal_wsi and f"wsiPresentId={wsi.group(1)} " in internal_wsi:
                        internal_displayed = line
                if "R102_EVENT" in line and "event=SOURCE_B_WSI_DISPATCH" in line and f"sourceSequence={source_b['sequence']} " in line:
                    source_b_dispatch = line
            if current_source_c and result.get("frontierOpenLine"):
                break
            time.sleep(0.003)

        result["sourceAWsiLine"] = source_wsi.get(str(source_a["sequence"]))
        result["internalWsiLine"] = internal_wsi
        result["internalDrawableAcquiredLine"] = internal_acquired
        result["internalDisplayedCallbackLine"] = internal_callback
        result["internalWsiDisplayedLine"] = internal_displayed
        result["sourceBWsiDispatchLine"] = source_b_dispatch
        result["sourceBWsiLine"] = source_wsi.get(str(source_b["sequence"]))
        result["sourceCWsiLine"] = current_source_c
        result["sourceCSequence"] = c_sequence if current_source_c else None
        result["titleWsiSequenceLine"] = result.get("titleWsiSequenceLine")
        result["presentationVerdict"] = result["presentationVerdict"]
        result["artifacts"] = {
            "sourceA": {"path": str(RUN_DIR / f"source-sequence-{source_a['sequence']}.raw"),
                        "sha256": file_sha256(RUN_DIR / f"source-sequence-{source_a['sequence']}.raw")},
            "sourceB": {"path": str(RUN_DIR / f"source-sequence-{source_b['sequence']}.raw"),
                        "sha256": file_sha256(RUN_DIR / f"source-sequence-{source_b['sequence']}.raw")},
            "motion": {"path": str(RUN_DIR / f"motion-pair-{source_a['sequence']}-{source_b['sequence']}.rg16f"),
                       "sha256": file_sha256(RUN_DIR / f"motion-pair-{source_a['sequence']}-{source_b['sequence']}.rg16f")},
            "midpoint": {"path": str(RUN_DIR / f"title-M-for-{source_a['sequence']}-{source_b['sequence']}.rgba16f"),
                         "sha256": file_sha256(RUN_DIR / f"title-M-for-{source_a['sequence']}-{source_b['sequence']}.rgba16f")},
        }
        try:
            result["gameIdentityCheckAfterAttempt"] = helper_run("check", pid)
            result["gameAliveAndWindowValidAfterAttempt"] = True
        except AttemptError as error:
            result["gameAliveAndWindowValidAfterAttempt"] = False
            result["gameIdentityCheckError"] = str(error)

        shutil.copyfile(observer, RUN_DIR / f"observer-r111-present-{pid}.log")
        atomic_json(RUN_DIR / "r111-event-locked-controller.json", result)
        return result
    except AttemptError as error:
        if input_process is not None and input_process.poll() is None:
            terminate_input("controller-recovery:" + str(error))
        result["attemptError"] = str(error)
        result["presentationVerdict"] = "ABORTED_SAFE_FALLBACK"
        if streams is not None:
            result["successfulCameraEvents"] = streams.camera_events
            result["observedTail"] = streams.observed[-160:]
            result["inputLogTail"] = streams.input_lines[-80:]
            if streams.observer_buffer:
                result["pendingObserverTail"] = streams.observer_buffer.decode("utf-8", errors="replace")
            observer = RUN_DIR / f"observer-r111-present-{result.get('gamePid', 0)}.log"
            try:
                shutil.copyfile(result["observerLog"], observer)
            except (KeyError, OSError):
                pass
        atomic_json(RUN_DIR / "r111-event-locked-controller.json", result)
        append_log(controller_log, "R111_ATTEMPT=ABORTED_SAFE_FALLBACK reason=" + str(error))
        raise
    finally:
        if streams is not None:
            streams.observer.close()
        if input_process is not None:
            if input_process.poll() is None:
                input_process.terminate()
            try:
                input_process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                input_process.kill()


def main() -> int:
    try:
        result = run_attempt()
    except AttemptError as error:
        print(f"R111_PRESENT_CONTROLLER=ABORTED_SAFE_FALLBACK reason={error}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    print("R111_PRESENT_CONTROLLER=ATTEMPT_COMPLETE slowDiagnostics=post-attempt")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
