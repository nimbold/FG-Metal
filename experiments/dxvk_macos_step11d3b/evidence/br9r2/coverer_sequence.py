#!/usr/bin/env python3
"""Run one lifecycle-observed 20/40/20 neutral-coverer sequence."""
from __future__ import annotations

import argparse
import json
import math
import os
import pathlib
import select
import re
import statistics
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from lifecycle_window_observer import LifecycleWindowObserver, sha256, utc_now

REPO = pathlib.Path(__file__).resolve().parents[4]
EXPERIMENT = REPO / "experiments/dxvk_macos_step11d3b"
NATIVE_EXPERIMENT = REPO / "experiments/dxvk_macos_step11d3a"
RUNS = EXPERIMENT / "runs"
NATIVE_RUNS = NATIVE_EXPERIMENT / "evidence/native-runs"


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
WINE_TITLE = "FG-Metal Step 11D.3-B Bare Vulkan"
NATIVE_TITLE = "STEP 11D.3-A Native MoltenVK Drawable Baseline"
WINE_ORIGIN = "vkAcquireNextImageKHR_begin"
NATIVE_ORIGIN = "vkAcquireNextImageKHR_begin"
EXPECTED_COVERER_SHA256 = "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc"
UNCOVERED_SECONDS = 20
COVERED_SECONDS = 40
RESTORED_SECONDS = 20
STATE_STABILITY_NS = 2_000_000_000
SAMPLE_GAP_LIMIT_NS = 1_500_000_000
TELEMETRY_STALE_AFTER_NS = 2_000_000_000
STATE_OBSERVATION_GRACE_NS = SAMPLE_GAP_LIMIT_NS
RUNNER_WATCHDOG_SECONDS = 900
OBSERVER_SAMPLE_INTERVAL_SECONDS = 0.5

CALIBRATION_TRANSITIONS = {
    "WAIT_UNCOVERED": {"UNCOVERED_ATTAINED"},
    "UNCOVERED_ATTAINED": {"COVER_ACTION_ISSUED"},
    "COVER_ACTION_ISSUED": {"COVERED_ATTAINED"},
    "COVERED_ATTAINED": {"RESTORE_ACTION_ISSUED"},
    "RESTORE_ACTION_ISSUED": {"RESTORED_ATTAINED"},
    "RESTORED_ATTAINED": {"COMPLETE"},
    "COMPLETE": set(),
}


class CalibrationStateMachine:
    """Fail-closed ordering for the uncovered/cover/restore calibration."""

    def __init__(self) -> None:
        self.state = "WAIT_UNCOVERED"
        self.history = [{"state": self.state, "monotonic_ns": None}]

    def transition(self, state: str, *, monotonic_ns: int) -> dict[str, Any]:
        if state not in CALIBRATION_TRANSITIONS.get(self.state, set()):
            raise RuntimeError(f"impossible calibration transition: {self.state} -> {state}")
        if type(monotonic_ns) is not int or monotonic_ns <= 0:
            raise RuntimeError("calibration transition requires a positive monotonic timestamp")
        previous = self.history[-1]["monotonic_ns"]
        if previous is not None and monotonic_ns <= previous:
            raise RuntimeError("calibration transitions must have strictly increasing timestamps")
        item = {"state": state, "monotonic_ns": monotonic_ns}
        self.state = state
        self.history.append(item)
        return item


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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


def read_observer_samples(path: pathlib.Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        if path.is_symlink() or not path.is_file():
            return rows
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


def latest_sample(samples: list[dict[str, Any]]) -> dict[str, Any] | None:
    return samples[-1] if samples else None


def telemetry_progress_evidence(samples: list[dict[str, Any]], *,
                                start_ns: int | None = None,
                                end_ns: int | None = None) -> dict[str, Any]:
    expected = any(row.get("windowTelemetryExpected") is True for row in samples)
    if not expected:
        return {"required": False, "verified": True, "checked_interval_count": 0,
                "invalid_reason": None}
    if any(row.get("windowTelemetryExpected") is not True for row in samples):
        return {"required": True, "verified": False, "checked_interval_count": 0,
                "invalid_reason": "telemetry expectation changed within the interval"}

    points: list[tuple[int, int, int]] = []
    for row in samples:
        sample_stamp = row.get("sampledAtMonotonicNS")
        telemetry = row.get("inProcessTelemetry")
        sequence = telemetry.get("telemetrySequence") if isinstance(telemetry, dict) else None
        telemetry_stamp = telemetry.get("timestampMachNS") if isinstance(telemetry, dict) else None
        if (type(sample_stamp) is not int or type(sequence) is not int or sequence < 0
                or type(telemetry_stamp) is not int or telemetry_stamp <= 0
                or telemetry_stamp > sample_stamp
                or sample_stamp - telemetry_stamp > TELEMETRY_STALE_AFTER_NS
                or telemetry.get("available") is not True or telemetry.get("fresh") is not True):
            return {"required": True, "verified": False, "checked_interval_count": 0,
                    "invalid_reason": "a Wine sample lacks fresh, non-stale sequenced AppKit telemetry"}
        points.append((sample_stamp, sequence, telemetry_stamp))
    for previous, current in zip(points, points[1:]):
        if (current[0] <= previous[0] or current[1] < previous[1] or current[2] < previous[2]
                or (current[1] == previous[1] and current[2] != previous[2])
                or (current[1] > previous[1] and current[2] <= previous[2])):
            return {"required": True, "verified": False, "checked_interval_count": 0,
                    "invalid_reason": "AppKit telemetry sequence and timestamp are not monotonic"}

    interval_start = start_ns if start_ns is not None else points[0][0]
    interval_end = end_ns if end_ns is not None else points[-1][0]
    interval_points = [point for point in points
                       if interval_start <= point[0] <= interval_end]
    checked = max(0, len(interval_points) - 1)
    progressed = bool(interval_points and interval_points[-1][1] > interval_points[0][1]
                      and interval_points[-1][2] > interval_points[0][2])
    return {
        "required": True,
        "verified": checked > 0 and progressed,
        "checked_interval_count": checked,
        "invalid_reason": None if checked > 0 and progressed
        else "target-owned telemetry did not advance during the qualifying interval",
        "first_sequence": points[0][1],
        "last_sequence": points[-1][1],
        "first_timestamp_mach_ns": points[0][2],
        "last_timestamp_mach_ns": points[-1][2],
    }


def current_qualifying_streak(samples: list[dict[str, Any]],
                              predicate: Callable[[dict[str, Any]], bool], *,
                              expected_target_pid: int | None = None,
                              expected_window_id: int | None = None) -> dict[str, Any] | None:
    if not samples:
        return None
    streak: list[dict[str, Any]] = []
    for sample in samples:
        stamp = sample.get("sampledAtMonotonicNS")
        target = sample.get("target")
        window_id = target.get("windowID") if isinstance(target, dict) else None
        owner_pid = target.get("ownerPID") if isinstance(target, dict) else None
        valid = (type(stamp) is int and stamp > 0 and type(window_id) is int and window_id > 0
                 and (expected_target_pid is None or owner_pid == expected_target_pid)
                 and (expected_window_id is None or window_id == expected_window_id)
                 and predicate(sample))
        if not valid:
            streak = []
            continue
        if streak:
            previous_stamp = streak[-1]["sampledAtMonotonicNS"]
            previous_target = streak[-1].get("target") or {}
            if (stamp <= previous_stamp or stamp - previous_stamp > SAMPLE_GAP_LIMIT_NS
                    or window_id != previous_target.get("windowID")
                    or owner_pid != previous_target.get("ownerPID")):
                streak = []
        streak.append(sample)
    if not streak or not predicate(samples[-1]):
        return None
    window_id = streak[-1]["target"]["windowID"]
    start = streak[0]["sampledAtMonotonicNS"]
    end = streak[-1]["sampledAtMonotonicNS"]
    if len(streak) < 3 or end - start < STATE_STABILITY_NS:
        return None
    telemetry_progress = telemetry_progress_evidence(streak, start_ns=start, end_ns=end)
    if not telemetry_progress["verified"]:
        return None
    return {
        "first_sample_monotonic_ns": start,
        "stability_proven_monotonic_ns": end,
        "stable_duration_ns": end - start,
        "sample_count": len(streak),
        "target_window_id": window_id,
        "samples": streak,
        "telemetry_progress": telemetry_progress,
    }


def transition_calibration(machine: CalibrationStateMachine,
                           record: dict[str, Any], state: str, *,
                           monotonic_ns: int | None = None) -> None:
    transition = machine.transition(
        state, monotonic_ns=time.monotonic_ns() if monotonic_ns is None else monotonic_ns)
    record["calibration_state"] = state
    record.setdefault("calibration_state_history", []).append(transition)


def proof_within_action_phase(*, proof_ns: int, start_ns: int, end_ns: int) -> bool:
    """Attainment must be observed by the action-phase end, excluding grace."""
    return (type(proof_ns) is int and type(start_ns) is int and type(end_ns) is int
            and start_ns < proof_ns <= end_ns)


def canonical_sample(sample: dict[str, Any], *, target_pid: int | None = None,
                     window_id: int | None = None) -> bool:
    target = sample.get("target")
    return (sample.get("canonicalUncoveredEligible") is True
            and isinstance(target, dict) and target.get("titleMatched") is True
            and (target_pid is None or target.get("ownerPID") == target_pid)
            and (window_id is None or target.get("windowID") == window_id))


def exact_covered_sample(sample: dict[str, Any], *, target_pid: int,
                         target_window_id: int, coverer_pid: int,
                         coverer_window_id: int, telemetry_expected: bool) -> bool:
    target = sample.get("target")
    if not isinstance(target, dict):
        return False
    target_rank = target.get("rankFrontToBack")
    if (sample.get("targetKind") not in {"wine", "native"}
            or target.get("ownerPID") != target_pid
            or target.get("windowID") != target_window_id
            or not isinstance(target_window_id, int) or target_window_id <= 0
            or target.get("titleMatched") is not True
            or target.get("onScreen") is not True
            or target.get("processAlive") is not True
            or target.get("processIdentityMatches") is not True
            or target.get("windowIdentityAmbiguous") is not False
            or not isinstance(target_rank, int)
            or target_rank < 0
            or target.get("rankSource") != "onScreenWindowList"
            or sample.get("allWindowListQuerySucceeded") is not True
            or sample.get("onScreenWindowListQuerySucceeded") is not True
            or sample.get("covererProcessEnumerationSucceeded") is not True
            or sample.get("windowTelemetryExpected") is not telemetry_expected):
        return False
    processes = sample.get("neutralCovererProcesses")
    windows = sample.get("neutralCovererWindows")
    if not isinstance(processes, list) or not isinstance(windows, list):
        return False
    process_match = any(
        isinstance(row, dict) and row.get("pid") == coverer_pid
        and row.get("sha256MatchesPinnedCoverer") is True
        and row.get("sha256") == EXPECTED_COVERER_SHA256
        for row in processes
    )
    if not process_match:
        return False
    window_match = any(
        isinstance(row, dict)
        and row.get("ownerPID") == coverer_pid
        and row.get("windowID") == coverer_window_id
        and isinstance(coverer_window_id, int) and coverer_window_id > 0
        and row.get("onScreen") is True
        and row.get("rankSource") == "onScreenWindowList"
        and row.get("fullyContainsTarget") is True
        and isinstance(row.get("alpha"), (int, float)) and row["alpha"] >= 0.99
        and isinstance(row.get("rankFrontToBack"), int)
        and row["rankFrontToBack"] >= 0
        and row["rankFrontToBack"] < target_rank
        for row in windows
    )
    if not window_match or target.get("covererOnExpectedDisplay") is not True:
        return False
    if telemetry_expected:
        telemetry = sample.get("inProcessTelemetry")
        if (not isinstance(telemetry, dict) or telemetry.get("available") is not True
                or telemetry.get("fresh") is not True
                or telemetry.get("windowOccluded") is not True
                or telemetry.get("visibleBitSet") is not False):
            return False
    return True


def restored_sample(sample: dict[str, Any], *, target_pid: int | None = None,
                    window_id: int | None = None) -> bool:
    target = sample.get("target")
    return (sample.get("restoredEligible") is True
            and isinstance(target, dict) and target.get("titleMatched") is True
            and (target_pid is None or target.get("ownerPID") == target_pid)
            and (window_id is None or target.get("windowID") == window_id))


def timeline_evidence(samples: list[dict[str, Any]], *, start_ns: int, end_ns: int,
                      predicate: Callable[[dict[str, Any]], bool],
                      observation_grace_ns: int = 0,
                      require_boundary_sample: bool = False) -> dict[str, Any]:
    if type(observation_grace_ns) is not int or observation_grace_ns < 0:
        raise ValueError("observation grace must be a nonnegative integer nanosecond duration")
    in_range = [row for row in samples
                if isinstance(row.get("sampledAtMonotonicNS"), int)
                and start_ns <= row["sampledAtMonotonicNS"] <= end_ns + observation_grace_ns]
    stamps = [row["sampledAtMonotonicNS"] for row in in_range]
    gaps = [right - left for left, right in zip(stamps, stamps[1:])]
    first_covered = bool(stamps and stamps[0] <= start_ns + SAMPLE_GAP_LIMIT_NS)
    boundary_seen = bool(stamps and stamps[-1] >= end_ns)
    last_covered = (boundary_seen if require_boundary_sample else
                    bool(stamps and stamps[-1] >= end_ns - SAMPLE_GAP_LIMIT_NS))
    bad_samples = [row for row in in_range if not predicate(row)]
    telemetry_progress = telemetry_progress_evidence(
        in_range, start_ns=start_ns, end_ns=end_ns)
    return {
        "start_monotonic_ns": start_ns,
        "end_monotonic_ns": end_ns,
        "duration_seconds": (end_ns - start_ns) / 1_000_000_000,
        "observer_sample_count": len(in_range),
        "action_phase_duration_ns": end_ns - start_ns,
        "action_phase_duration_seconds": (end_ns - start_ns) / 1_000_000_000,
        "observation_grace_limit_ns": observation_grace_ns,
        "observation_end_monotonic_ns": stamps[-1] if stamps else None,
        "final_boundary_sample_observed": boundary_seen,
        "first_sample_within_gap_limit": first_covered,
        "last_sample_within_gap_limit": last_covered,
        "maximum_sample_gap_ns": max(gaps) if gaps else None,
        "maximum_sample_gap_within_limit": bool(gaps) and max(gaps) <= SAMPLE_GAP_LIMIT_NS,
        "invalid_sample_count": len(bad_samples),
        "telemetry_progress": telemetry_progress,
        "pass": bool(in_range and first_covered and last_covered and gaps
                     and max(gaps) <= SAMPLE_GAP_LIMIT_NS and not bad_samples
                     and telemetry_progress["verified"]),
    }


def wait_for_boundary_sample(observer: LifecycleWindowObserver,
                             runner: subprocess.Popen[bytes], *, end_ns: int,
                             predicate: Callable[[dict[str, Any]], bool]) -> bool:
    """Wait only for the final scheduled state sample; phase measurement stays fixed."""
    deadline_ns = end_ns + STATE_OBSERVATION_GRACE_NS
    while runner.poll() is None and time.monotonic_ns() <= deadline_ns:
        samples, latest, _state = current_observer_state(observer)
        if latest is not None:
            if not predicate(latest):
                return False
            stamp = latest.get("sampledAtMonotonicNS")
            if type(stamp) is int and stamp >= end_ns:
                return True
        time.sleep(0.05)
    _samples, latest, _state = current_observer_state(observer)
    stamp = latest.get("sampledAtMonotonicNS") if latest is not None else None
    return bool(latest is not None and predicate(latest) and type(stamp) is int
                and end_ns <= stamp <= deadline_ns)


def percentile(values: list[int], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize_trace_interval(trace_path: pathlib.Path, *, kind: str, start_ns: int,
                             end_ns: int, target_pid: int,
                             experiment_token: str | None) -> dict[str, Any]:
    if trace_path.is_symlink() or not trace_path.is_file():
        raise RuntimeError(f"target trace is missing or unsafe: {trace_path}")
    acquire_times: list[int] = []
    callback_rows: list[tuple[int, int]] = []
    next_drawable: list[int] = []
    submit: list[int] = []
    process_started = False
    with trace_path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            if kind == "wine":
                if (row.get("host_pid") != target_pid or row.get("target_unix_pid") != target_pid
                        or row.get("experiment_token") != experiment_token
                        or row.get("trace_role") != "target"):
                    continue
            elif row.get("event") == "process_start":
                process_started = row.get("pid") == target_pid
            elif not process_started:
                # Native trace files belong to one exact run and begin with its PID-bound process_start.
                continue

            event = row.get("event")
            if kind == "wine":
                expected_origin = {
                    WINE_ORIGIN: "WINE_SOURCE",
                    "vkQueueSubmit2_end": "WINE_SOURCE",
                    "next_drawable_end": "LAYER_SOURCE",
                    "presented_callback": "LAYER_SOURCE",
                }.get(event)
                if expected_origin is not None and row.get("origin") != expected_origin:
                    continue
            stamp = row.get("ts_mach_ns")
            stamp = stamp if isinstance(stamp, int) else None
            in_phase = stamp is not None and start_ns <= stamp < end_ns
            if event == (WINE_ORIGIN if kind == "wine" else NATIVE_ORIGIN) and in_phase:
                acquire_times.append(stamp)
            if event == "presented_callback" and in_phase:
                presented = row.get("presented_time_mach_ns")
                if isinstance(presented, int):
                    callback_rows.append((stamp, presented))
            if event == "next_drawable_end" and in_phase:
                duration = row.get("duration_ns")
                if isinstance(duration, int) and duration >= 0:
                    next_drawable.append(duration)
            if event == "vkQueueSubmit2_end" and in_phase:
                duration = row.get("duration_ns")
                if isinstance(duration, int) and duration >= 0:
                    submit.append(duration)
    duration_seconds = (end_ns - start_ns) / 1_000_000_000
    positive = sum(1 for _stamp, presented in callback_rows if presented > 0)
    zero = sum(1 for _stamp, presented in callback_rows if presented == 0)
    callbacks = len(callback_rows)
    return {
        "trace_path": str(trace_path.resolve()),
        "trace_sha256": sha256(trace_path),
        "phase_start_monotonic_ns": start_ns,
        "phase_end_monotonic_ns": end_ns,
        "phase_duration_seconds": duration_seconds,
        "target_pid": target_pid,
        "acquire_count": len(acquire_times),
        "acquired_fps": len(acquire_times) / duration_seconds if duration_seconds > 0 else None,
        "callback_count": callbacks,
        "displayed_callback_count": positive,
        "displayed_fps": positive / duration_seconds if duration_seconds > 0 else None,
        "raw_zero_callback_count": zero,
        "raw_zero_fraction": zero / callbacks if callbacks else None,
        "next_drawable_ms": {
            "count": len(next_drawable),
            "median": statistics.median(next_drawable) / 1_000_000 if next_drawable else None,
            "p99": percentile(next_drawable, 0.99) / 1_000_000 if next_drawable else None,
            "max": max(next_drawable) / 1_000_000 if next_drawable else None,
        },
        "submit_ms": {
            "count": len(submit),
            "median": statistics.median(submit) / 1_000_000 if submit else None,
            "p99": percentile(submit, 0.99) / 1_000_000 if submit else None,
            "max": max(submit) / 1_000_000 if submit else None,
        },
        "coverage_complete": bool(callbacks and len(acquire_times)
                                   and abs(len(acquire_times) - callbacks) <= max(3, len(acquire_times) * 0.02)),
    }


def classify_metrics(metrics: dict[str, Any]) -> str:
    acquire = metrics.get("acquired_fps")
    display = metrics.get("displayed_fps")
    zeros = metrics.get("raw_zero_fraction")
    if not all(isinstance(value, (int, float)) for value in (acquire, display, zeros)):
        return "UNCLASSIFIED"
    if acquire >= 100 and display <= 5 and zeros >= 0.5:
        return "FAST_ZERO"
    if 45 <= acquire <= 75 and 45 <= display <= 75 and zeros <= 0.05:
        return "HEALTHY_REFRESH"
    return "DIFFERENT_OR_UNHEALTHY"


class Coverer:
    def __init__(self, binary: pathlib.Path):
        self.process = subprocess.Popen(
            [str(binary)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, bufsize=1, close_fds=True,
        )
        try:
            self.ready = self.read_response(20)
            if self.ready.get("event") != "ready" or self.ready.get("status") != "PASS":
                raise RuntimeError(f"coverer did not become ready: {self.ready}")
        except BaseException:
            if self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            raise

    def read_response(self, timeout: float = 10.0) -> dict[str, Any]:
        if self.process.stdout is None:
            raise RuntimeError("coverer stdout is unavailable")
        ready, _, _ = select.select([self.process.stdout], [], [], timeout)
        if not ready:
            raise TimeoutError("coverer did not return a command response")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"coverer exited before replying ({self.process.poll()})")
        value = json.loads(line)
        if not isinstance(value, dict):
            raise RuntimeError("coverer returned a non-object response")
        return value

    def command(self, name: str, **fields: Any) -> dict[str, Any]:
        if self.process.poll() is not None or self.process.stdin is None:
            raise RuntimeError(f"coverer process exited before {name}: {self.process.returncode}")
        self.process.stdin.write(json.dumps({"command": name, **fields}) + "\n")
        self.process.stdin.flush()
        return self.read_response(10)

    def quit(self) -> None:
        if self.process.poll() is None and self.process.stdin is not None:
            try:
                self.command("quit")
            except Exception:
                self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)


def verify_covering_status(response: dict[str, Any], *, target_pid: int,
                           target_window_id: int | None = None) -> bool:
    return bool(
        response.get("status") == "PASS"
        and response.get("target_pid") == target_pid
        and (target_window_id is None or response.get("target_window_number") == target_window_id)
        and response.get("coverer_is_visible") is True
        and response.get("coverer_is_opaque") is True
        and response.get("coverer_alpha_value") == 1
        and response.get("coverer_cg_alpha") == 1
        and response.get("target_area_coverage_fraction", 0) >= 0.999999
        and response.get("target_fully_covered_by_geometry") is True
        and response.get("front_of_target") is True
        and isinstance(response.get("coverer_front_to_back_rank"), int)
        and isinstance(response.get("target_front_to_back_rank"), int)
        and response["coverer_front_to_back_rank"] < response["target_front_to_back_rank"]
        and isinstance(response.get("coverer_cg_layer"), int)
        and isinstance(response.get("target_cg_layer"), int)
        and response["coverer_cg_layer"] > response["target_cg_layer"]
        and response.get("coverer_is_key") is False
        and response.get("coverer_is_main") is False
        and response.get("coverer_application_is_active") is False
    )


def current_observer_state(observer: LifecycleWindowObserver) -> tuple[
        list[dict[str, Any]], dict[str, Any] | None, dict[str, Any] | None]:
    samples = read_observer_samples(observer.samples_path)
    return samples, latest_sample(samples), observer.latest()


def safe_handoff(observer: LifecycleWindowObserver) -> dict[str, Any] | None:
    return read_json(observer.handoff_path)


def runner_command(kind: str, identifier: str) -> list[str]:
    if kind == "wine":
        return [sys.executable, str(EXPERIMENT / "runner_b.py"), "calibration", "5", identifier]
    return [sys.executable, str(NATIVE_EXPERIMENT / "run_native_control.py"), "calibration", identifier]


def run_sequence(kind: str, identifier: str, tag: str,
                 binary: pathlib.Path) -> int:
    if kind not in {"wine", "native"}:
        raise ValueError("kind must be wine or native")
    if not tag.replace("-", "").replace("_", "").isalnum():
        raise ValueError("tag must be alphanumeric with optional hyphens/underscores")
    binary = binary.resolve(strict=True)
    if not binary.is_file() or binary.is_symlink():
        raise RuntimeError(f"coverer binary is missing or unsafe: {binary}")
    coverer_sha = sha256(binary)
    if coverer_sha != EXPECTED_COVERER_SHA256:
        raise RuntimeError("refusing to run because the immutable neutral-coverer hash changed")
    record_path = EVIDENCE / f"{tag}-controller.json"
    if record_path.exists() or record_path.is_symlink():
        raise FileExistsError(f"refusing to replace controller evidence: {record_path}")

    title = WINE_TITLE if kind == "wine" else NATIVE_TITLE
    run_dir = (RUNS / identifier) if kind == "wine" else (NATIVE_RUNS / identifier)
    if kind == "native" and (run_dir.exists() or run_dir.is_symlink()):
        raise FileExistsError(f"refusing to reuse Native run directory: {run_dir}")

    record: dict[str, Any] = {
        "schema_version": 2,
        "kind": kind,
        "requested_run_identifier": identifier,
        "tag": tag,
        "created_at_utc": utc_now(),
        "controller_source_path": str(pathlib.Path(__file__).resolve()),
        "controller_source_sha256": sha256(pathlib.Path(__file__).resolve()),
        "coverer_source_path": str((EXPERIMENT / "neutral_coverer.swift").resolve(strict=True)),
        "coverer_source_sha256": sha256(EXPERIMENT / "neutral_coverer.swift"),
        "coverer_binary_path": str(binary),
        "coverer_binary_sha256": coverer_sha,
        "target_title": title,
        "requested_sequence_seconds": {
            "uncovered": UNCOVERED_SECONDS,
            "covered": COVERED_SECONDS,
            "restored": RESTORED_SECONDS,
        },
        "observer_sample_interval_seconds": OBSERVER_SAMPLE_INTERVAL_SECONDS,
        "state_observation_grace_ns": STATE_OBSERVATION_GRACE_NS,
        "phase_metric_origins": ({
            "acquire": "WINE_SOURCE",
            "submit": "WINE_SOURCE",
            "next_drawable": "LAYER_SOURCE",
            "presented_callback": "LAYER_SOURCE",
        } if kind == "wine" else {
            "native_trace": "target_pid_bound_native_trace"
        }),
        "calibration_state": "WAIT_UNCOVERED",
        "calibration_state_history": [{"state": "WAIT_UNCOVERED", "monotonic_ns": None}],
        "actions": [],
        "phase_metrics": {},
        "phase_errors": [],
    }
    observer = LifecycleWindowObserver(
        evidence_root=EVIDENCE, runs_root=(RUNS if kind == "wine" else NATIVE_RUNS), tag=tag,
        coverer_path=binary, expected_display_id=1,
    )
    record["observer_source_sha256"] = observer.metadata["observer_source_sha256"]
    record["observer_controller_source_sha256"] = observer.metadata["controller_source_sha256"]
    record["observer"] = observer.controller_evidence()
    atomic_json(record_path, record)

    runner_log_path = EVIDENCE / f"{tag}-runner.log"
    runner_log = runner_log_path.open("x", encoding="utf-8")
    runner: subprocess.Popen[bytes] | None = None
    coverer: Coverer | None = None
    handoff: dict[str, Any] | None = None
    runner_returncode: int | None = None
    observer_result: dict[str, Any] | None = None
    calibration = CalibrationStateMachine()
    try:
        observer.build_and_start()
        record["observer"] = observer.controller_evidence()
        record["status"] = "OBSERVER_RUNNING"
        atomic_json(record_path, record)
        environment = os.environ.copy()
        environment.update(observer.runner_environment())
        runner = subprocess.Popen(
            runner_command(kind, identifier), cwd=str(REPO), env=environment,
            stdout=runner_log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        record["runner_command"] = runner.args
        record["runner_pid"] = runner.pid
        record["runner_started_at_utc"] = utc_now()
        record["runner_started_monotonic_ns"] = time.monotonic_ns()
        record["status"] = "RUNNING_WAITING_FOR_TARGET"
        atomic_json(record_path, record)

        runner_started_monotonic = time.monotonic()
        baseline_streak: dict[str, Any] | None = None
        while runner.poll() is None and baseline_streak is None:
            if time.monotonic() - runner_started_monotonic > RUNNER_WATCHDOG_SECONDS:
                record["phase_errors"].append("runner lifecycle safety watchdog expired while waiting for uncovered state")
                runner.terminate()
                break
            handoff = safe_handoff(observer)
            samples, latest, latest_state = current_observer_state(observer)
            if handoff is not None:
                if handoff.get("run_id") != identifier and kind == "native":
                    raise RuntimeError("Native target handoff run_id differs from the requested run")
                if handoff.get("target_title") != title:
                    raise RuntimeError("target handoff window title differs from the accepted target")
                baseline_streak = current_qualifying_streak(
                    samples, canonical_sample,
                    expected_target_pid=handoff.get("target_unix_pid"))
                if baseline_streak is not None:
                    break
            time.sleep(OBSERVER_SAMPLE_INTERVAL_SECONDS)
        if handoff is None:
            record["phase_errors"].append("runner did not publish an actual target PID handoff")
        elif baseline_streak is None:
            record["phase_errors"].append(
                "target never established three consecutive uncovered samples spanning two seconds")

        run_dir: pathlib.Path | None = None
        if handoff is not None:
            if kind == "wine":
                run_dir = RUNS / str(handoff.get("run_id"))
            else:
                run_dir = NATIVE_RUNS / str(handoff.get("run_id"))
            record["target_handoff"] = handoff
            record["target_pid"] = handoff.get("target_unix_pid")
            record["target_kind"] = handoff.get("target_kind")
            record["target_trace_path"] = handoff.get("target_trace_path")
            record["run_directory"] = str(run_dir.resolve())
            record["uncovered_stable_attainment"] = baseline_streak
            if baseline_streak is not None:
                transition_calibration(
                    calibration, record, "UNCOVERED_ATTAINED",
                    monotonic_ns=baseline_streak["stability_proven_monotonic_ns"])
            record["observer"] = observer.controller_evidence()
            atomic_json(record_path, record)

        uncovered_started_ns = None
        uncovered_ended_ns = None
        covered_started_ns = None
        covered_ended_ns = None
        restored_started_ns = None
        restored_ended_ns = None
        target_window_id: int | None = None
        coverer_pid: int | None = None
        coverer_window_id: int | None = None
        telemetry_expected = handoff.get("window_telemetry_expected") is True if handoff else False

        if handoff is not None and baseline_streak is not None:
            target_pid = handoff["target_unix_pid"]
            target_window_id = baseline_streak["target_window_id"]
            uncovered_predicate = lambda sample: canonical_sample(
                sample, target_pid=target_pid, window_id=target_window_id)
            uncovered_started_ns = baseline_streak["stability_proven_monotonic_ns"]
            uncovered_ended_ns = uncovered_started_ns + UNCOVERED_SECONDS * 1_000_000_000
            record["phase_timeline"] = {
                "uncovered_start_monotonic_ns": uncovered_started_ns,
                "uncovered_expected_end_monotonic_ns": uncovered_ended_ns,
                "uncovered_action_duration_ns": UNCOVERED_SECONDS * 1_000_000_000,
                "state_observation_grace_limit_ns": STATE_OBSERVATION_GRACE_NS,
                "start_basis": "end of the live >=2-second canonical uncovered interval",
            }
            record["status"] = "CANONICAL_UNCOVERED_HOLD"
            atomic_json(record_path, record)
            while time.monotonic_ns() < uncovered_ended_ns and runner.poll() is None:
                samples, latest, _state = current_observer_state(observer)
                if latest is not None and not uncovered_predicate(latest):
                    record["phase_errors"].append("canonical uncovered state changed during the 20-second baseline")
                    break
                time.sleep(OBSERVER_SAMPLE_INTERVAL_SECONDS)
            boundary_sample_observed = wait_for_boundary_sample(
                observer, runner, end_ns=uncovered_ended_ns,
                predicate=uncovered_predicate)
            samples, latest, _state = current_observer_state(observer)
            uncovered_end_actual = time.monotonic_ns()
            uncovered_timeline = timeline_evidence(
                samples, start_ns=uncovered_started_ns, end_ns=uncovered_ended_ns,
                predicate=uncovered_predicate,
                observation_grace_ns=STATE_OBSERVATION_GRACE_NS,
                require_boundary_sample=True,
            )
            uncovered_timeline["controller_end_monotonic_ns"] = uncovered_end_actual
            uncovered_timeline["boundary_sample_wait_succeeded"] = boundary_sample_observed
            record["phase_metrics"]["uncovered_observer_timeline"] = uncovered_timeline
            if not uncovered_timeline["pass"] or runner.poll() is not None:
                record["phase_errors"].append("20-second uncovered phase was not continuously verified")
            else:
                try:
                    if latest is None or not uncovered_predicate(latest):
                        raise RuntimeError("target was not canonical uncovered immediately before coverer launch")
                    coverer = Coverer(binary)
                    coverer_pid = coverer.ready.get("pid")
                    record["coverer_ready"] = coverer.ready
                    record["coverer_pid"] = coverer_pid
                    bind_response = coverer.command("target", pid=target_pid, title=title)
                    record["actions"].append({
                        "action": "bind_target",
                        "controller_monotonic_ns": time.monotonic_ns(),
                        "response": bind_response,
                    })
                    if bind_response.get("status") != "PASS":
                        raise RuntimeError(f"coverer target binding failed: {bind_response}")
                    show_started = time.monotonic_ns()
                    transition_calibration(
                        calibration, record, "COVER_ACTION_ISSUED", monotonic_ns=show_started)
                    show_response = coverer.command("show")
                    status = show_response
                    record["actions"].append({
                        "action": "show_coverer",
                        "start_monotonic_ns": show_started,
                        "response": show_response,
                        "attained": False,
                    })
                    atomic_json(record_path, record)
                    shown_target_window_id = show_response.get("target_window_number")
                    coverer_window_id = show_response.get("coverer_window_number")
                    if (shown_target_window_id != target_window_id
                            or not isinstance(target_window_id, int) or not isinstance(coverer_pid, int)
                            or not isinstance(coverer_window_id, int)):
                        raise RuntimeError("coverer response changed or omitted its bound target/coverer PID or window ID")
                    if not verify_covering_status(
                            show_response, target_pid=target_pid,
                            target_window_id=target_window_id):
                        raise RuntimeError(f"neutral coverer did not report full opaque coverage: {show_response}")

                    coverage_predicate = lambda sample: exact_covered_sample(
                        sample, target_pid=target_pid, target_window_id=target_window_id,
                        coverer_pid=coverer_pid, coverer_window_id=coverer_window_id,
                        telemetry_expected=telemetry_expected,
                    )
                    coverage_streak = None
                    while runner.poll() is None:
                        samples, _latest, _state = current_observer_state(observer)
                        coverage_streak = current_qualifying_streak(
                            samples, coverage_predicate,
                            expected_target_pid=target_pid,
                            expected_window_id=target_window_id)
                        if coverage_streak is not None:
                            break
                        status = coverer.command("status")
                        if not verify_covering_status(
                                status, target_pid=target_pid,
                                target_window_id=target_window_id):
                            raise RuntimeError(f"coverer lost full opaque coverage before observer confirmation: {status}")
                        time.sleep(OBSERVER_SAMPLE_INTERVAL_SECONDS)
                    if coverage_streak is None:
                        raise RuntimeError("observer did not prove exact full coverage for three samples over two seconds")
                    covered_started_ns = coverage_streak["first_sample_monotonic_ns"]
                    covered_proven_ns = coverage_streak["stability_proven_monotonic_ns"]
                    transition_calibration(
                        calibration, record, "COVERED_ATTAINED",
                        monotonic_ns=covered_proven_ns)
                    covered_ended_ns = covered_started_ns + COVERED_SECONDS * 1_000_000_000
                    record["phase_timeline"].update({
                        "covered_start_monotonic_ns": covered_started_ns,
                        "covered_stability_proven_monotonic_ns": covered_proven_ns,
                        "covered_expected_end_monotonic_ns": covered_ended_ns,
                        "covered_action_duration_ns": COVERED_SECONDS * 1_000_000_000,
                        "state_observation_grace_limit_ns": STATE_OBSERVATION_GRACE_NS,
                    })
                    record["actions"][-1]["attained"] = True
                    record["actions"][-1]["attainment"] = coverage_streak
                    record["status"] = "VERIFIED_FULL_COVER_HOLD"
                    atomic_json(record_path, record)
                    next_status_at = time.monotonic() + 5
                    while time.monotonic_ns() < covered_ended_ns and runner.poll() is None:
                        samples, latest, _state = current_observer_state(observer)
                        if latest is not None and not coverage_predicate(latest):
                            record["phase_errors"].append("verified full cover state changed during the 40-second hold")
                            break
                        if time.monotonic() >= next_status_at:
                            status = coverer.command("status")
                            record["actions"].append({
                                "action": "covered_hold_snapshot",
                                "controller_monotonic_ns": time.monotonic_ns(),
                                "response": status,
                                "attained": verify_covering_status(
                                    status, target_pid=target_pid,
                                    target_window_id=target_window_id),
                            })
                            if not record["actions"][-1]["attained"]:
                                record["phase_errors"].append("coverer status lost full coverage during the 40-second hold")
                                break
                            next_status_at += 5
                            atomic_json(record_path, record)
                        time.sleep(OBSERVER_SAMPLE_INTERVAL_SECONDS)
                    covered_boundary_sample_observed = wait_for_boundary_sample(
                        observer, runner, end_ns=covered_ended_ns,
                        predicate=coverage_predicate)
                    samples, _latest, _state = current_observer_state(observer)
                    coverage_timeline = timeline_evidence(
                        samples, start_ns=covered_started_ns, end_ns=covered_ended_ns,
                        predicate=coverage_predicate,
                        observation_grace_ns=STATE_OBSERVATION_GRACE_NS,
                        require_boundary_sample=True,
                    )
                    coverage_timeline["boundary_sample_wait_succeeded"] = covered_boundary_sample_observed
                    record["phase_metrics"]["covered_observer_timeline"] = coverage_timeline
                    covered_phase_pass = coverage_timeline["pass"] and runner.poll() is None
                    if not covered_phase_pass:
                        record["phase_errors"].append("40-second full-cover phase was not continuously verified")
                    hide_started = time.monotonic_ns()
                    hide_response = coverer.command("hide")
                    record["actions"].append({
                        "action": "hide_coverer",
                        "start_monotonic_ns": hide_started,
                        "end_monotonic_ns": time.monotonic_ns(),
                        "response": hide_response,
                        "coverer_hidden": hide_response.get("coverer_is_visible") is False,
                    })
                    record["phase_timeline"]["hide_start_monotonic_ns"] = hide_started
                    hide_ended = time.monotonic_ns()
                    record["phase_timeline"]["hide_end_monotonic_ns"] = hide_ended
                    atomic_json(record_path, record)

                    if not covered_phase_pass:
                        raise RuntimeError("restoration cannot be attained without a verified covered phase")
                    if hide_response.get("coverer_is_visible") is not False:
                        raise RuntimeError("restore action did not prove the coverer was hidden")
                    transition_calibration(
                        calibration, record, "RESTORE_ACTION_ISSUED", monotonic_ns=hide_started)

                    restored_predicate = lambda sample: restored_sample(
                        sample, target_pid=target_pid, window_id=target_window_id)
                    restore_streak = None
                    while runner.poll() is None:
                        samples, _latest, _state = current_observer_state(observer)
                        post_hide_samples = [
                            sample for sample in samples
                            if type(sample.get("sampledAtMonotonicNS")) is int
                            and sample["sampledAtMonotonicNS"] > hide_ended
                        ]
                        restore_streak = current_qualifying_streak(
                            post_hide_samples, restored_predicate,
                            expected_target_pid=target_pid,
                            expected_window_id=target_window_id)
                        if restore_streak is not None:
                            break
                        time.sleep(OBSERVER_SAMPLE_INTERVAL_SECONDS)
                    if restore_streak is None:
                        raise RuntimeError("target did not regain a stable visible, active, uncovered state after HIDE")
                    restored_proven_ns = restore_streak["stability_proven_monotonic_ns"]
                    restored_started_ns = hide_ended
                    restored_ended_ns = restored_started_ns + RESTORED_SECONDS * 1_000_000_000
                    restored_attained_in_phase = proof_within_action_phase(
                        proof_ns=restored_proven_ns, start_ns=restored_started_ns,
                        end_ns=restored_ended_ns)
                    record["phase_timeline"].update({
                        "restored_start_monotonic_ns": restored_started_ns,
                        "restored_stability_proven_monotonic_ns": restored_proven_ns,
                        "restored_expected_end_monotonic_ns": restored_ended_ns,
                        "restored_action_duration_ns": RESTORED_SECONDS * 1_000_000_000,
                        "restored_state_attained_within_action_phase": restored_attained_in_phase,
                        "restored_state_attainment_wait_ns": restored_proven_ns - restored_started_ns,
                        "state_observation_grace_limit_ns": STATE_OBSERVATION_GRACE_NS,
                    })
                    record["actions"][-1]["restoration_attainment"] = restore_streak
                    if not restored_attained_in_phase:
                        record["status"] = "RESTORATION_ATTAINED_AFTER_ACTION_PHASE"
                        atomic_json(record_path, record)
                        raise RuntimeError("restored state stability was not attained within the 20-second action phase")
                    transition_calibration(
                        calibration, record, "RESTORED_ATTAINED",
                        monotonic_ns=restored_proven_ns)
                    record["actions"][-1]["restoration_attainment"] = restore_streak
                    record["status"] = "VERIFIED_RESTORED_HOLD"
                    atomic_json(record_path, record)
                    while time.monotonic_ns() < restored_ended_ns and runner.poll() is None:
                        samples, latest, _state = current_observer_state(observer)
                        if latest is not None and not restored_predicate(latest):
                            record["phase_errors"].append("restored visible state changed during the 20-second hold")
                            break
                        time.sleep(OBSERVER_SAMPLE_INTERVAL_SECONDS)
                    restored_boundary_sample_observed = wait_for_boundary_sample(
                        observer, runner, end_ns=restored_ended_ns,
                        predicate=restored_predicate)
                    samples, _latest, _state = current_observer_state(observer)
                    restored_timeline = timeline_evidence(
                        samples, start_ns=restored_started_ns, end_ns=restored_ended_ns,
                        predicate=restored_predicate,
                        observation_grace_ns=STATE_OBSERVATION_GRACE_NS,
                        require_boundary_sample=True,
                    )
                    restored_timeline["boundary_sample_wait_succeeded"] = restored_boundary_sample_observed
                    record["phase_metrics"]["restored_observer_timeline"] = restored_timeline
                    restored_phase_pass = restored_timeline["pass"] and runner.poll() is None
                    if not restored_phase_pass:
                        record["phase_errors"].append("20-second restored phase was not continuously verified")
                    if coverer is not None:
                        status = coverer.command("status")
                        record["actions"].append({
                            "action": "restored_coverer_snapshot",
                            "controller_monotonic_ns": time.monotonic_ns(),
                            "response": status,
                            "coverer_hidden": status.get("coverer_is_visible") is False,
                        })
                        if record["actions"][-1]["coverer_hidden"] is not True:
                            record["phase_errors"].append("neutral coverer did not remain hidden after HIDE")
                    if restored_phase_pass and not record["phase_errors"]:
                        transition_calibration(calibration, record, "COMPLETE")
                except BaseException as error:
                    record["phase_errors"].append(f"{type(error).__name__}: {error}")
                    record["controller_error"] = f"{type(error).__name__}: {error}"
                    if coverer is not None:
                        try:
                            response = coverer.command("hide")
                            record["actions"].append({
                                "action": "failure_hide_coverer",
                                "controller_monotonic_ns": time.monotonic_ns(),
                                "response": response,
                            })
                        except BaseException as hide_error:
                            record["phase_errors"].append(f"failure hide error: {type(hide_error).__name__}: {hide_error}")
        else:
            # The target must complete its managed run; an unverified baseline is never covered.
            record["status"] = "RUNNING_WITHOUT_COVERER"
            atomic_json(record_path, record)

        record["target_handoff"] = handoff
        record["status"] = record.get("status", "RUNNING")
        atomic_json(record_path, record)
        try:
            runner_returncode = runner.wait(timeout=RUNNER_WATCHDOG_SECONDS)
        except subprocess.TimeoutExpired:
            record["runner_watchdog_triggered"] = True
            record["phase_errors"].append("managed experiment exceeded the controller runner watchdog")
            atomic_json(record_path, record)
            runner.terminate()
            try:
                runner_returncode = runner.wait(timeout=30)
            except subprocess.TimeoutExpired:
                runner.kill()
                runner_returncode = runner.wait(timeout=10)

        result_path = run_dir / "run-result.json" if run_dir is not None else None
        run_result = read_json(result_path) if result_path is not None else None
        record["run_result"] = ({
            "path": str(result_path.resolve()),
            "sha256": sha256(result_path),
            "status": run_result.get("status"),
            "is_valid": run_result.get("is_valid"),
            "quantitative_ready": (run_result.get("validation", {}).get("quantitative_ready")
                                    if kind == "wine" else
                                    run_result.get("trace_validation", {}).get("quantitative_ready")),
            "trace_validation_status": (run_result.get("validation", {}).get("status")
                                        if kind == "wine" else run_result.get("trace_validation_status")),
            "gate0_pre": run_result.get("gate0_pre"),
            "gate0_post": run_result.get("gate0_post"),
            "gate0_post_blockers": run_result.get("gate0_post_blockers"),
            "prefix_cleanup_pass": run_result.get("prefix_cleanup_pass"),
            "native_cleanup_receipt": run_result.get("native_run_cleanup_receipt"),
            "trace_index": run_result.get("trace_index"),
            "trace_path": run_result.get("trace_path"),
            "trace_artifacts": run_result.get("trace_artifacts"),
        } if run_result else None)
        record["runner_returncode"] = runner_returncode
        record["runner_log_path"] = str(runner_log_path.resolve())
        record["runner_log_sha256"] = sha256(runner_log_path)

        trace_index = run_result.get("trace_index") if isinstance(run_result, dict) else None
        if kind == "wine":
            indexed_path = trace_index.get("target_trace_path") if isinstance(trace_index, dict) else None
            selected_targets = (trace_index.get("processes", [])
                                if isinstance(trace_index, dict) else [])
            selected_target = next((row for row in selected_targets
                                    if isinstance(row, dict) and row.get("trace_role") == "target"), None)
            trace_artifacts = run_result.get("trace_artifacts") if isinstance(run_result, dict) else None
            target_trace_artifact = (trace_artifacts.get("target_host_trace")
                                     if isinstance(trace_artifacts, dict) else None)
            runner_trace_path = (target_trace_artifact.get("path")
                                 if isinstance(target_trace_artifact, dict) else None)
            expected_trace_path = (str((run_dir / "host-traces"
                                        / f"host-trace.{handoff.get('target_unix_pid')}.jsonl").resolve())
                                   if handoff and run_dir is not None else None)
            trace_binding_pass = bool(
                handoff and isinstance(expected_trace_path, str)
                and handoff.get("target_trace_path") == expected_trace_path
                and runner_trace_path == expected_trace_path
                and isinstance(trace_index, dict)
                and trace_index.get("status") == "PASS"
                and trace_index.get("target_unix_pid") == handoff.get("target_unix_pid")
                and trace_index.get("experiment_token") == handoff.get("experiment_token")
                and indexed_path == expected_trace_path
                and isinstance(selected_target, dict)
                and selected_target.get("host_pid") == handoff.get("target_unix_pid")
                and selected_target.get("target_unix_pid") == handoff.get("target_unix_pid")
                and selected_target.get("path") == expected_trace_path
                and selected_target.get("ownership_valid") is True
                and type(selected_target.get("record_count")) is int
                and selected_target.get("record_count", 0) > 0
            )
        else:
            runner_trace_path = run_result.get("trace_path") if isinstance(run_result, dict) else None
            expected_trace_path = str((run_dir / "trace.jsonl").resolve()) if run_dir is not None else None
            trace_binding_pass = bool(
                handoff and isinstance(expected_trace_path, str)
                and handoff.get("target_trace_path") == expected_trace_path
                and runner_trace_path == expected_trace_path
            )
        record["target_trace_binding_pass"] = trace_binding_pass
        if not trace_binding_pass:
            record["phase_errors"].append(
                "target PID handoff trace path does not match the exact runner-indexed trace")

        if run_result is not None and handoff is not None and uncovered_started_ns is not None:
            trace_value = runner_trace_path
            if isinstance(trace_value, str):
                trace_path = pathlib.Path(trace_value)
                record["target_trace"] = {
                    "path": str(trace_path.resolve()),
                    "sha256": sha256(trace_path),
                    "size_bytes": trace_path.stat().st_size,
                }
                trace_token = handoff.get("experiment_token") if kind == "wine" else None
                for name, start, end in (
                    ("uncovered", uncovered_started_ns, uncovered_ended_ns),
                    ("covered", covered_started_ns, covered_ended_ns),
                    ("restored", restored_started_ns, restored_ended_ns),
                ):
                    if isinstance(start, int) and isinstance(end, int):
                        metrics = summarize_trace_interval(
                            trace_path, kind=kind, start_ns=start, end_ns=end,
                            target_pid=handoff["target_unix_pid"], experiment_token=trace_token,
                        )
                        metrics["signature"] = classify_metrics(metrics)
                        record["phase_metrics"][name] = metrics

        if coverer is not None:
            coverer.quit()
            coverer = None
        handoff = safe_handoff(observer) or handoff
        completion_run_id = handoff.get("run_id") if handoff else identifier
        completion_pid = handoff.get("target_unix_pid") if handoff else None
        completion_token = handoff.get("experiment_token") if handoff else None
        observer.signal_completion(
            run_id=str(completion_run_id),
            status="RUNNER_FINISHED" if runner_returncode == 0 else "RUNNER_FAILED",
            runner_returncode=runner_returncode,
            target_pid=completion_pid if isinstance(completion_pid, int) else None,
            experiment_token=completion_token if isinstance(completion_token, str) else None,
        )
        observer_result = observer.wait()
        record["observer_result_path"] = str(observer.result_path.resolve())
        record["observer_result_sha256"] = sha256(observer.result_path)
        record["observer_result"] = observer_result
        record["observer"] = observer.controller_evidence()
        completion = observer_result.get("experimentCompleteSignal") if observer_result else None
        record["observer_completion_pass"] = bool(
            observer_result is not None and observer_result.get("error") is None
            and observer_result.get("watchdogTriggered") is False
            and observer_result.get("targetRunID") == completion_run_id
            and observer_result.get("targetPID") == completion_pid
            and observer_result.get("experimentToken") == completion_token
            and observer_result.get("targetKind") == kind
            and observer_result.get("windowTelemetryExpected") is (kind == "wine")
            and isinstance(completion, dict)
            and completion.get("status") == "RUNNER_FINISHED"
            and completion.get("process_runner_returncode") == 0
        )
        if not record["observer_completion_pass"]:
            record["phase_errors"].append("final lifecycle observer did not bind and finish this target cleanly")

        observer_states = (observer_result if isinstance(observer_result, dict) else {})
        state_results = {}
        for name, key in (("uncovered", "canonicalUncoveredState"),
                          ("covered", "coveredState"),
                          ("restored", "restoredState")):
            state = observer_states.get(key)
            state_results[name] = bool(
                isinstance(state, dict) and state.get("attained") is True
                and type(state.get("stableDurationNS")) is int
                and state.get("stableDurationNS", 0) >= STATE_STABILITY_NS
                and type(state.get("sampleCountInLongestQualifyingStreak")) is int
                and state.get("sampleCountInLongestQualifyingStreak", 0) >= 3
                and state.get("telemetryProgressRequired") is (kind == "wine")
                and state.get("telemetryProgressVerified") is True
                and (kind != "wine"
                     or type(state.get("telemetrySequenceAdvanceCountInLongestQualifyingStreak")) is int
                     and state.get("telemetrySequenceAdvanceCountInLongestQualifyingStreak", 0) >= 1)
            )
        attained_states = {row["state"] for row in calibration.history}
        state_results["covered"] = state_results["covered"] and {
            "COVER_ACTION_ISSUED", "COVERED_ATTAINED"}.issubset(attained_states)
        state_results["restored"] = state_results["restored"] and {
            "COVERED_ATTAINED", "RESTORE_ACTION_ISSUED", "RESTORED_ATTAINED"}.issubset(attained_states)
        record["observer_stable_states_pass"] = state_results
        if not all(state_results.values()):
            record["phase_errors"].append(
                "observer uncovered, covered, and restored streaks lack the required stable state evidence")

        trace_good = bool(record.get("run_result")
                          and record["run_result"].get("status") == "PASS"
                          and record["run_result"].get("quantitative_ready") is True)
        all_phases_pass = all(
            isinstance(record.get("phase_metrics", {}).get(f"{name}_observer_timeline"), dict)
            and record["phase_metrics"][f"{name}_observer_timeline"].get("pass") is True
            for name in ("uncovered", "covered", "restored")
        )
        record["status"] = (
            "PASS" if runner_returncode == 0 and trace_good and all_phases_pass
            and record.get("target_trace_binding_pass") is True
            and all(state_results.values())
            and record.get("observer_completion_pass") is True
            and calibration.state == "COMPLETE"
            and not record["phase_errors"] else "FAIL"
        )
        record["calibration_state"] = calibration.state
        record["calibration_state_history"] = calibration.history
        record["finished_at_utc"] = utc_now()
        record["finished_monotonic_ns"] = time.monotonic_ns()
        atomic_json(record_path, record)
        print(json.dumps({
            "status": record["status"],
            "controller_record": str(record_path.resolve()),
            "run_result": record.get("run_result"),
            "phase_metrics": record.get("phase_metrics"),
            "phase_errors": record.get("phase_errors"),
            "observer_result_path": record.get("observer_result_path"),
        }, indent=2, sort_keys=True))
        return 0 if record["status"] == "PASS" else 1
    except BaseException as error:
        record["controller_error"] = f"{type(error).__name__}: {error}"
        record["phase_errors"].append(record["controller_error"])
        if runner is not None and runner.poll() is None:
            try:
                runner_returncode = runner.wait(timeout=RUNNER_WATCHDOG_SECONDS)
            except subprocess.TimeoutExpired:
                record["runner_watchdog_triggered"] = True
                runner.terminate()
                try:
                    runner_returncode = runner.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    runner_returncode = runner.wait(timeout=10)
        handoff = safe_handoff(observer) or handoff
        if observer.process is not None and observer.process.poll() is None:
            observer.signal_completion(
                run_id=str(handoff.get("run_id") if handoff else identifier),
                status="CONTROLLER_FAILED",
                runner_returncode=runner_returncode,
                target_pid=(handoff.get("target_unix_pid") if handoff else None),
                experiment_token=(handoff.get("experiment_token") if handoff else None),
            )
            try:
                observer_result = observer.wait()
                record["observer_result"] = observer_result
            except BaseException as observer_error:
                record["observer_error"] = f"{type(observer_error).__name__}: {observer_error}"
        record["observer"] = observer.controller_evidence()
        record["runner_returncode"] = runner_returncode
        record["status"] = "FAIL"
        record["finished_at_utc"] = utc_now()
        atomic_json(record_path, record)
        raise
    finally:
        if coverer is not None:
            coverer.quit()
        if runner_log is not None:
            runner_log.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("wine", "native"))
    parser.add_argument("identifier", help="passing Wine smoke ID or unique Native run ID")
    parser.add_argument("tag", help="unique evidence tag")
    parser.add_argument("--coverer", type=pathlib.Path,
                        default=pathlib.Path(
                            "/Users/nima/Library/Caches/FGMetal/artifacts/sha256/"
                            "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc/"
                            "neutral-coverer"))
    arguments = parser.parse_args()
    return run_sequence(arguments.kind, arguments.identifier, arguments.tag, arguments.coverer)


if __name__ == "__main__":
    raise SystemExit(main())
