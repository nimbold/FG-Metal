#!/usr/bin/env python3
"""Compile and supervise the lifecycle-bound public window observer."""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import re
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

HERE = pathlib.Path(__file__).resolve().parent
OBSERVER_SOURCE = HERE / "public_window_observer.swift"
EXPECTED_COVERER_SHA256 = "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc"
OBSERVER_WATCHDOG_SECONDS = 1800
START_GATE_SIGNAL_NAME = "authorization-start-signal.json"
UNCOVERED_STABILITY_NS = 2_000_000_000
SAMPLE_GAP_LIMIT_NS = 1_500_000_000


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_canonical_uncovered_streak(samples: list[dict[str, Any]], *,
                                       target_pid: int,
                                       latest_state: dict[str, Any] | None) -> dict[str, Any] | None:
    """Return a qualifying streak only when its final sample is the persisted latest state."""
    if not samples or not isinstance(latest_state, dict):
        return None
    latest_sample = latest_state.get("latestSample")
    canonical_state = latest_state.get("canonicalUncovered")
    if not isinstance(latest_sample, dict) or latest_sample != samples[-1]:
        return None
    if (not isinstance(canonical_state, dict)
            or canonical_state.get("attained") is not True):
        return None

    def qualifies(sample: dict[str, Any]) -> bool:
        target = sample.get("target")
        accessibility = target.get("accessibility") if isinstance(target, dict) else None
        display = target.get("displayRelationship") if isinstance(target, dict) else None
        app = sample.get("app")
        telemetry = sample.get("inProcessTelemetry")
        return bool(
            sample.get("targetKind") == "wine"
            and sample.get("windowTelemetryExpected") is True
            and sample.get("canonicalUncoveredEligible") is True
            and sample.get("allWindowListQuerySucceeded") is True
            and sample.get("onScreenWindowListQuerySucceeded") is True
            and sample.get("aboveTargetWindowDetailsComplete") is True
            and sample.get("covererProcessEnumerationSucceeded") is True
            and sample.get("alphaOpaqueFullCoverCandidatePresent") is False
            and sample.get("neutralCovererProcessPresent") is False
            and sample.get("neutralCovererWindowPresent") is False
            and sample.get("neutralCovererProcesses") == []
            and sample.get("neutralCovererWindows") == []
            and isinstance(target, dict)
            and target.get("ownerPID") == target_pid
            and isinstance(target.get("windowID"), int)
            and target.get("windowID", 0) > 0
            and type(target.get("rankFrontToBack")) is int
            and target.get("rankFrontToBack", -1) >= 0
            and target.get("rankSource") == "onScreenWindowList"
            and target.get("titleMatched") is True
            and target.get("onScreen") is True
            and target.get("processAlive") is True
            and target.get("processIdentityMatches") is True
            and target.get("windowIdentityAmbiguous") is False
            and target.get("accessibilityMinimized") is False
            and isinstance(accessibility, dict)
            and accessibility.get("accessibilityTrusted") is True
            and accessibility.get("focused") is True
            and isinstance(display, dict)
            and display.get("uniqueExpectedDisplayMatch") is True
            and display.get("targetFullyOnExpectedDisplay") is True
            and isinstance(app, dict)
            and app.get("isActive") is True
            and isinstance(telemetry, dict)
            and telemetry.get("available") is True
            and telemetry.get("fresh") is True
            and telemetry.get("windowVisible") is True
            and telemetry.get("windowOccluded") is False
            and telemetry.get("visibleBitSet") is True
            and telemetry.get("windowMiniaturized") is False
            and telemetry.get("windowKey") is True
            and telemetry.get("windowMain") is True
            and telemetry.get("appActive") is True
        )

    if not qualifies(samples[-1]):
        return None
    window_id = samples[-1]["target"]["windowID"]
    streak = [samples[-1]]
    for sample in reversed(samples[:-1]):
        if not qualifies(sample) or sample["target"].get("windowID") != window_id:
            break
        previous_ns = streak[-1].get("sampledAtMonotonicNS")
        current_ns = sample.get("sampledAtMonotonicNS")
        if (type(previous_ns) is not int or type(current_ns) is not int
                or previous_ns <= current_ns
                or previous_ns - current_ns > SAMPLE_GAP_LIMIT_NS):
            break
        streak.append(sample)
    streak.reverse()
    points: list[tuple[int, int, int]] = []
    for sample in streak:
        sample_ns = sample.get("sampledAtMonotonicNS")
        telemetry = sample.get("inProcessTelemetry")
        sequence = telemetry.get("telemetrySequence") if isinstance(telemetry, dict) else None
        telemetry_ns = telemetry.get("timestampMachNS") if isinstance(telemetry, dict) else None
        if (type(sample_ns) is not int or sample_ns <= 0 or type(sequence) is not int
                or sequence < 0 or type(telemetry_ns) is not int or telemetry_ns <= 0
                or telemetry_ns > sample_ns):
            return None
        points.append((sample_ns, sequence, telemetry_ns))
    for previous, current in zip(points, points[1:]):
        if (current[0] <= previous[0] or current[1] < previous[1]
                or current[2] < previous[2]
                or (current[1] == previous[1] and current[2] != previous[2])
                or (current[1] > previous[1] and current[2] <= previous[2])):
            return None
    start_ns, first_sequence, first_telemetry_ns = points[0]
    end_ns, last_sequence, last_telemetry_ns = points[-1]
    if (len(streak) < 3 or end_ns - start_ns < UNCOVERED_STABILITY_NS
            or last_sequence <= first_sequence or last_telemetry_ns <= first_telemetry_ns):
        return None
    return {
        "stable_start_monotonic_ns": start_ns,
        "stability_proven_monotonic_ns": end_ns,
        "stable_duration_ns": end_ns - start_ns,
        "sample_count": len(streak),
        "target_window_id": window_id,
        "telemetry_sequence_start": first_sequence,
        "telemetry_sequence_end": last_sequence,
        "telemetry_timestamp_start_mach_ns": first_telemetry_ns,
        "telemetry_timestamp_end_mach_ns": last_telemetry_ns,
        "telemetry_progress_required": True,
        "telemetry_progress_verified": True,
        "title_matched": True,
        "target_process_identity_matches": True,
        "target_visible": True,
        "window_unoccluded": True,
        "app_active": True,
        "neutral_coverer_process_count": 0,
        "neutral_coverer_window_count": 0,
        "samples": streak,
    }


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


class LifecycleWindowObserver:
    """Observer lifetime follows the experiment-complete signal, not launch time."""

    def __init__(self, *, evidence_root: pathlib.Path, runs_root: pathlib.Path, tag: str,
                 coverer_path: pathlib.Path, expected_display_id: int = 1):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}", tag):
            raise ValueError("observer tag must be a short unique alphanumeric identifier")
        if not coverer_path.is_file() or coverer_path.is_symlink():
            raise RuntimeError(f"neutral coverer is missing or unsafe: {coverer_path}")
        coverer_sha = sha256(coverer_path)
        if coverer_sha != EXPECTED_COVERER_SHA256:
            raise RuntimeError("refusing observer start because the immutable coverer hash changed")
        runs_root_absolute = pathlib.Path(os.path.abspath(runs_root))
        try:
            runs_root_info = runs_root_absolute.lstat()
            runs_root_resolved = runs_root_absolute.resolve(strict=True)
        except OSError as error:
            raise RuntimeError("trusted observer runs root is unavailable") from error
        if (runs_root_absolute.is_symlink() or not runs_root_absolute.is_dir()
                or runs_root_resolved != runs_root_absolute):
            raise RuntimeError("trusted observer runs root is not a canonical real directory")
        if runs_root_info.st_uid != os.geteuid() or runs_root_info.st_mode & 0o022:
            raise RuntimeError("trusted observer runs root has unsafe ownership or permissions")
        self.directory = evidence_root / f"{tag}-observer"
        self.directory.mkdir(parents=True, exist_ok=False)
        self.handoff_path = self.directory / "target-window-observer-input.json"
        self.completion_path = self.directory / "experiment-complete.json"
        self.result_path = self.directory / "observer-result.json"
        self.samples_path = self.directory / "observer-samples.jsonl"
        self.latest_path = self.directory / "observer-latest.json"
        self.start_gate_signal_path = self.directory / START_GATE_SIGNAL_NAME
        self.binary_path = self.directory / "public-window-observer"
        self.stdout_path = self.directory / "observer.stdout.log"
        self.stderr_path = self.directory / "observer.stderr.log"
        self.compile_log_path = self.directory / "observer-compile.log"
        self.metadata_path = self.directory / "observer-controller.json"
        self.coverer_path = coverer_path.resolve(strict=True)
        self.coverer_sha = coverer_sha
        self.runs_root = runs_root_absolute
        self.expected_display_id = expected_display_id
        self.process: subprocess.Popen[bytes] | None = None
        self.stdout = None
        self.stderr = None
        self.metadata: dict[str, Any] = {
            "schema_version": 1,
            "tag": tag,
            "created_at_utc": utc_now(),
            "created_monotonic_ns": time.monotonic_ns(),
            "controller_source_path": str(pathlib.Path(__file__).resolve()),
            "controller_source_sha256": sha256(pathlib.Path(__file__).resolve()),
            "observer_source_path": str(OBSERVER_SOURCE.resolve(strict=True)),
            "observer_source_sha256": sha256(OBSERVER_SOURCE),
            "coverer_binary_path": str(self.coverer_path),
            "coverer_binary_sha256": coverer_sha,
            "expected_display_id": expected_display_id,
            "trusted_runs_root": str(self.runs_root),
            "safety_watchdog_seconds": OBSERVER_WATCHDOG_SECONDS,
            "handoff_path": str(self.handoff_path),
            "completion_path": str(self.completion_path),
            "result_path": str(self.result_path),
            "samples_path": str(self.samples_path),
            "latest_path": str(self.latest_path),
            "start_gate_signal_path": str(self.start_gate_signal_path),
        }

    def build_and_start(self, *, startup_timeout_seconds: float = 60.0) -> None:
        compile_command = [
            "/usr/bin/swiftc", "-O", "-framework", "AppKit", "-framework",
            "ApplicationServices", "-framework", "CoreGraphics",
            "-o", str(self.binary_path), str(OBSERVER_SOURCE),
        ]
        self.metadata["compiler_command"] = compile_command
        started = time.monotonic()
        completed = subprocess.run(
            compile_command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=startup_timeout_seconds, check=False,
        )
        self.compile_log_path.write_text(completed.stdout, encoding="utf-8")
        self.metadata["compile_elapsed_seconds"] = time.monotonic() - started
        self.metadata["compile_returncode"] = completed.returncode
        if completed.returncode != 0 or not self.binary_path.is_file() or self.binary_path.is_symlink():
            self.metadata["status"] = "OBSERVER_COMPILE_FAIL"
            atomic_json(self.metadata_path, self.metadata)
            raise RuntimeError(f"public window observer compilation failed; see {self.compile_log_path}")
        self.metadata["observer_binary_sha256"] = sha256(self.binary_path)
        self.metadata["observer_binary_size_bytes"] = self.binary_path.stat().st_size
        self.stdout = self.stdout_path.open("xb")
        self.stderr = self.stderr_path.open("xb")
        self.process = subprocess.Popen(
            [
                str(self.binary_path), str(self.handoff_path), str(self.completion_path),
                str(self.result_path), str(self.samples_path), str(self.latest_path),
                str(self.coverer_path), str(self.expected_display_id), str(self.runs_root),
            ],
            cwd=str(self.directory), stdin=subprocess.DEVNULL,
            stdout=self.stdout, stderr=self.stderr, close_fds=True,
        )
        self.metadata["observer_pid"] = self.process.pid
        self.metadata["observer_started_at_utc"] = utc_now()
        self.metadata["observer_started_monotonic_ns"] = time.monotonic_ns()
        self.metadata["status"] = "WAITING_FOR_TARGET_HANDOFF"
        atomic_json(self.metadata_path, self.metadata)

        deadline = time.monotonic() + startup_timeout_seconds
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                self.metadata["status"] = "OBSERVER_STARTUP_FAIL"
                self.metadata["observer_returncode"] = self.process.returncode
                self.metadata["observer_finished_at_utc"] = utc_now()
                atomic_json(self.metadata_path, self.metadata)
                raise RuntimeError(
                    f"public window observer exited before start handshake ({self.process.returncode}); "
                    f"see {self.stderr_path}"
                )
            latest = self.latest()
            if latest is not None and latest.get("event") == "observer_started":
                self.metadata["start_handshake_at_utc"] = utc_now()
                self.metadata["start_handshake_monotonic_ns"] = time.monotonic_ns()
                self.metadata["status"] = "RUNNING_WAITING_FOR_TARGET"
                atomic_json(self.metadata_path, self.metadata)
                return
            time.sleep(0.05)
        self.metadata["status"] = "OBSERVER_START_HANDSHAKE_TIMEOUT"
        self.metadata["startup_handshake_timed_out"] = True
        atomic_json(self.metadata_path, self.metadata)
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.stdout is not None:
            self.stdout.close()
            self.stdout = None
        if self.stderr is not None:
            self.stderr.close()
            self.stderr = None
        raise TimeoutError(f"public window observer did not acknowledge startup; see {self.stderr_path}")

    def runner_environment(self) -> dict[str, str]:
        if self.process is None or self.process.poll() is not None:
            raise RuntimeError("observer must be running before its paths are handed to the experiment runner")
        return {
            "FG_PUBLIC_WINDOW_OBSERVER_HANDOFF_PATH": str(self.handoff_path),
            "FG_PUBLIC_WINDOW_OBSERVER_COMPLETION_PATH": str(self.completion_path),
            "FG_PUBLIC_WINDOW_OBSERVER_RESULT_PATH": str(self.result_path),
            "FG_PUBLIC_WINDOW_OBSERVER_START_SIGNAL_PATH": str(self.start_gate_signal_path),
        }

    def latest(self) -> dict[str, Any] | None:
        try:
            if self.latest_path.is_symlink() or not self.latest_path.is_file():
                return None
            value = json.loads(self.latest_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def result(self) -> dict[str, Any] | None:
        try:
            if self.result_path.is_symlink() or not self.result_path.is_file():
                return None
            value = json.loads(self.result_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def signal_completion(self, *, run_id: str, status: str,
                          runner_returncode: int | None,
                          target_pid: int | None = None,
                          experiment_token: str | None = None) -> None:
        if target_pid is not None and (not isinstance(experiment_token, str)
                                       or not re.fullmatch(r"[0-9a-f]{32}", experiment_token)):
            raise RuntimeError("target completion signal requires the exact experiment token")
        if self.completion_path.exists() or self.completion_path.is_symlink():
            if self.completion_path.is_symlink() or not self.completion_path.is_file():
                raise RuntimeError("experiment-complete signal is unsafe")
            prior = json.loads(self.completion_path.read_text(encoding="utf-8"))
            if (prior.get("run_id") != run_id
                    or (target_pid is not None and prior.get("target_unix_pid") != target_pid)
                    or prior.get("experiment_token") != experiment_token):
                raise RuntimeError("experiment-complete signal belongs to a different target identity")
            return
        atomic_json(self.completion_path, {
            "schema_version": 1,
            "run_id": run_id,
            "status": status,
            "runner_pid": os.getpid(),
            "runner_monotonic_ns": time.monotonic_ns(),
            "runner_finished_at_utc": utc_now(),
            "target_unix_pid": target_pid,
            "experiment_token": experiment_token if target_pid is not None else None,
            "process_runner_returncode": runner_returncode,
        })

    def wait(self, *, timeout_seconds: float = OBSERVER_WATCHDOG_SECONDS + 60) -> dict[str, Any] | None:
        if self.process is None:
            raise RuntimeError("observer was not started")
        try:
            returncode = self.process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired as error:
            self.metadata["safety_watchdog_wait_expired"] = True
            self.metadata["status"] = "OBSERVER_SAFETY_TIMEOUT"
            atomic_json(self.metadata_path, self.metadata)
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
            raise RuntimeError("public window observer safety watchdog expired") from error
        result = self.result()
        self.metadata["observer_returncode"] = returncode
        self.metadata["observer_finished_at_utc"] = utc_now()
        self.metadata["observer_finished_monotonic_ns"] = time.monotonic_ns()
        self.metadata["observer_result_sha256"] = (
            sha256(self.result_path) if self.result_path.is_file() and not self.result_path.is_symlink() else None
        )
        self.metadata["status"] = "PASS" if returncode == 0 and result is not None else "FAIL"
        atomic_json(self.metadata_path, self.metadata)
        if self.stdout is not None:
            self.stdout.close()
            self.stdout = None
        if self.stderr is not None:
            self.stderr.close()
            self.stderr = None
        return result

    def controller_evidence(self) -> dict[str, Any]:
        return dict(self.metadata)
