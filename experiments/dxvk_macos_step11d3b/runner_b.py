#!/usr/bin/env python3
"""Run one serialized STEP 11D.3-B Wine Vulkan experiment under PrefixLease."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
STORAGE = REPO / "experiments/storage"
sys.path.insert(0, str(STORAGE))
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE / "evidence/br9r2"))
import build_retention  # noqa: E402
import storage_policy  # noqa: E402
from trace_validator import BareWineTraceValidator, TraceValidator, ValidationError  # noqa: E402
from experiments.dxvk_macos_step11d3b.loader_evidence import parse_loader_evidence
from experiments.dxvk_macos_step11d3b.trace_processes import build_trace_index, validate_target_association
from lifecycle_window_observer import current_canonical_uncovered_streak  # noqa: E402

RUNTIME = Path("/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine")
ICD = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json")
MOLTENVK_ORIGINAL = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib")
MOLTENVK_ICD_LIBRARY = ICD.parent / "libMoltenVK.dylib"
RUNTIME_MANIFEST = STORAGE / "runtime-manifests/step11d_r_active_runtime.json"
APP = HERE / "control/bare_vulkan_app-start-gated.exe"
APP_SOURCE = HERE / "bare_vulkan_app.cpp"
APP_BUILD_RECEIPT = HERE / "evidence/br9r2/bare-vulkan-app-start-gate-build.json"
PROXY = HERE / "control/libvulkan-trace-start-gated-configured.dylib"
PROXY_SOURCE = HERE / "trace_loader.mm"
PROCESS_RUNNER = HERE / "wine_process_runner.py"
WINEVULKAN_DLL = RUNTIME / "lib/wine/x86_64-windows/winevulkan.dll"
WINEVULKAN_SO = RUNTIME / "lib/wine/x86_64-unix/winevulkan.so"
WINE_HOST_LOADER = RUNTIME / "lib/wine/x86_64-unix/win32u.so"
WINEMAC_SO = RUNTIME / "lib/wine/x86_64-unix/winemac.so"
WINE = RUNTIME / "bin/wine"
WINE_HOST_EXECUTABLE = RUNTIME / "lib/wine/x86_64-unix/wine"
WINESERVER = RUNTIME / "bin/wineserver"
TRACE_VALIDATOR = STORAGE / "trace_validator.py"
BR9R2_EVIDENCE = HERE / "evidence/br9r2"
PUBLIC_WINDOW_OBSERVER_SOURCE = BR9R2_EVIDENCE / "public_window_observer.swift"
LIFECYCLE_OBSERVER_CONTROLLER = BR9R2_EVIDENCE / "lifecycle_window_observer.py"
AUTHORIZATION_SMOKE_CONTROLLER = BR9R2_EVIDENCE / "authorization_smoke_controller.py"
COVERER_SEQUENCE_CONTROLLER = BR9R2_EVIDENCE / "coverer_sequence.py"
CONTROL_VERSION = "step11d3b-br9r2-start-gated-visible-state-v11"
EXPECTED_MOLTENVK_VERSION = "MoltenVK v1.4.2"
SMOKE_AUTH_FIELD = "br2_smoke_authorization_hmac_sha256"
WINDOW_TITLE = "FG-Metal Step 11D.3-B Bare Vulkan"
GIB = 1024 ** 3
MIB = 1024 ** 2
FORBIDDEN_MODULE_PARTS = (
    "dxvk", "dxgi", "d3d9", "d3d10", "d3d11", "d3d12", "fgmetalbridge",
    "rife", "framegen", "frame_generation", "fgmetal",
)


def observer_evidence_root() -> Path:
    """Resolve the controller's private transient root or the legacy evidence root."""
    override = os.environ.get("FGMETAL_BR9R_EVIDENCE_ROOT")
    if override is None:
        return BR9R2_EVIDENCE.resolve(strict=True)
    root = Path(override)
    if (not root.is_absolute() or root.parent != Path("/private/tmp")
            or not re.fullmatch(r"codex-br9r2-[a-z0-9-]{8,96}", root.name)
            or root.resolve(strict=True) != root):
        raise RuntimeError("BR9R transient evidence root is not a canonical /private/tmp/codex-br9r2-* path")
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or root.is_symlink()
            or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("BR9R transient evidence root is not an owned mode-0700 directory")
    return root


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json_exclusive(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise


def observer_paths_from_environment(*, required: bool) -> dict[str, Path] | None:
    environment_keys = {
        "handoff": "FG_PUBLIC_WINDOW_OBSERVER_HANDOFF_PATH",
        "completion": "FG_PUBLIC_WINDOW_OBSERVER_COMPLETION_PATH",
        "result": "FG_PUBLIC_WINDOW_OBSERVER_RESULT_PATH",
        "start_signal": "FG_PUBLIC_WINDOW_OBSERVER_START_SIGNAL_PATH",
    }
    raw = {name: os.environ.get(key) for name, key in environment_keys.items()}
    if all(value is None for value in raw.values()):
        if required:
            raise RuntimeError("this experiment requires the lifecycle-bound public window observer controller")
        return None
    if any(not isinstance(value, str) or not value.startswith("/") for value in raw.values()):
        raise RuntimeError("public window observer controller paths must be supplied together as absolute paths")
    paths = {name: Path(value) for name, value in raw.items() if value is not None}
    expected_names = {
        "handoff": "target-window-observer-input.json",
        "completion": "experiment-complete.json",
        "result": "observer-result.json",
        "start_signal": "authorization-start-signal.json",
    }
    parent = paths["handoff"].parent.resolve(strict=True)
    evidence_root = observer_evidence_root()
    if not parent.is_relative_to(evidence_root):
        raise RuntimeError("public window observer evidence must remain under the configured BR9R evidence root")
    if any(path.parent.resolve(strict=True) != parent or path.name != expected_names[name]
           for name, path in paths.items()):
        raise RuntimeError("public window observer evidence paths do not match the fixed per-run layout")
    if (paths["handoff"].exists() or paths["handoff"].is_symlink()
            or paths["completion"].exists() or paths["completion"].is_symlink()
            or paths["start_signal"].exists() or paths["start_signal"].is_symlink()):
        raise RuntimeError("public window observer handoff/completion/start files already exist")
    latest = parent / "observer-latest.json"
    if latest.is_symlink() or not latest.is_file():
        raise RuntimeError("public window observer did not persist its startup handshake")
    try:
        latest_value = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("public window observer startup handshake is unreadable") from error
    if latest_value.get("event") != "observer_started":
        raise RuntimeError("public window observer startup handshake is not in the waiting state")
    paths["latest"] = latest
    return paths


def signal_observer_complete(paths: dict[str, Path] | None, *, experiment_id: str,
                             target_pid: int | None, runner_returncode: int | None,
                             experiment_token: str | None,
                             target_process_exit_monotonic_ns: int | None = None) -> None:
    if paths is None:
        return
    if target_pid is not None and (not isinstance(experiment_token, str)
                                   or not re.fullmatch(r"[0-9a-f]{32}", experiment_token)):
        raise RuntimeError("target completion signal requires the exact experiment token")
    signal_monotonic_ns = time.monotonic_ns()
    if (target_process_exit_monotonic_ns is not None
            and (type(target_process_exit_monotonic_ns) is not int
                 or target_process_exit_monotonic_ns <= 0
                 or signal_monotonic_ns < target_process_exit_monotonic_ns)):
        raise RuntimeError("target completion signal must follow the verified process exit")
    if paths["completion"].exists() or paths["completion"].is_symlink():
        if paths["completion"].is_symlink() or not paths["completion"].is_file():
            raise RuntimeError("public window observer completion path is unsafe")
        prior = json.loads(paths["completion"].read_text(encoding="utf-8"))
        if (prior.get("run_id") != experiment_id
                or prior.get("target_unix_pid") != target_pid
                or prior.get("experiment_token") != experiment_token
                or prior.get("runner_pid") != os.getpid()
                or prior.get("target_process_exit_monotonic_ns")
                != target_process_exit_monotonic_ns):
            raise RuntimeError("public window observer completion belongs to a different target identity")
        return
    write_json_exclusive(paths["completion"], {
        "schema_version": 1,
        "run_id": experiment_id,
        "status": "RUNNER_FINISHED",
        "runner_pid": os.getpid(),
        "runner_monotonic_ns": signal_monotonic_ns,
        "runner_finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_unix_pid": target_pid,
        "experiment_token": experiment_token if target_pid is not None else None,
        "target_process_exit_monotonic_ns": target_process_exit_monotonic_ns,
        "process_runner_returncode": runner_returncode,
    })


def load_final_observer_result(paths: dict[str, Path] | None,
                              *, wait_seconds: float = 20.0) -> tuple[dict[str, Any] | None, str | None]:
    if paths is None:
        return None, "public window observer was not started"
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        path = paths["result"]
        if path.is_file() and not path.is_symlink():
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict) and value.get("endedAtUTC"):
                return value, None
        time.sleep(0.05)
    return None, "public window observer did not finalize on the experiment-complete signal"


def validate_smoke_observer(observer: dict[str, Any] | None, *,
                            experiment_id: str, target_pid: int | None,
                            runner_pid: int | None,
                            completion_runner_pid: int | None,
                            token: str,
                            expected_process_identity: dict[str, str],
                            trace_index: dict[str, Any] | None,
                            execution_result: dict[str, Any] | None,
                            target_run_end_monotonic_ns: int | None) -> dict[str, Any]:
    errors: list[str] = []
    terminal_ax_gap_count = 0
    if not isinstance(observer, dict):
        return {"status": "FAIL", "errors": ["final public window observer evidence is missing"]}
    handoff = observer.get("targetIdentityHandoff")
    if not isinstance(handoff, dict):
        errors.append("target PID handoff is missing")
        handoff = {}
    if observer.get("targetRunID") != experiment_id or handoff.get("run_id") != experiment_id:
        errors.append("observer run_id differs from the authenticated runner")
    if observer.get("experimentToken") != token or handoff.get("experiment_token") != token:
        errors.append("observer experiment token differs from the authenticated runner")
    if target_pid is None or observer.get("targetPID") != target_pid or handoff.get("target_unix_pid") != target_pid:
        errors.append("observer PID differs from the actual PrefixLease Wine child PID")
    if runner_pid is None or handoff.get("runner_pid") != runner_pid:
        errors.append("target PID handoff does not come from the actual PrefixLease process runner")
    expected_trace_path = None
    if target_pid is not None:
        expected_trace_path = str(
            (HERE / "runs" / experiment_id / "host-traces"
             / f"host-trace.{target_pid}.jsonl").resolve()
        )
    indexed_trace_path = trace_index.get("target_trace_path") if isinstance(trace_index, dict) else None
    indexed_targets = (trace_index.get("processes", [])
                       if isinstance(trace_index, dict) else [])
    indexed_target = next((row for row in indexed_targets
                           if isinstance(row, dict) and row.get("trace_role") == "target"), None)
    if (not isinstance(trace_index, dict) or trace_index.get("status") != "PASS"
            or trace_index.get("target_unix_pid") != target_pid
            or trace_index.get("experiment_token") != token
            or indexed_trace_path != expected_trace_path
            or not isinstance(indexed_target, dict)
            or indexed_target.get("host_pid") != target_pid
            or indexed_target.get("target_unix_pid") != target_pid
            or indexed_target.get("path") != expected_trace_path
            or indexed_target.get("ownership_valid") is not True
            or not isinstance(indexed_target.get("record_count"), int)
            or indexed_target.get("record_count", 0) <= 0):
        errors.append("validated trace index does not select this run's exact target trace")
    if handoff.get("target_trace_path") != indexed_trace_path or indexed_trace_path != expected_trace_path:
        errors.append("observer handoff trace path differs from the exact indexed run-local target trace")
    expected_pe = handoff.get("expected_pe_identity")
    if not isinstance(expected_pe, dict) or expected_pe.get("path") != str(APP.resolve()):
        errors.append("observer handoff does not name the exact expected PE")
    elif expected_pe.get("sha256") != sha256(APP):
        errors.append("observer handoff PE SHA-256 differs from the current runner input")
    process_identity = handoff.get("expected_target_process_identity")
    if (handoff.get("schema_version") != 2 or handoff.get("target_kind") != "wine"
            or handoff.get("window_telemetry_expected") is not True):
        errors.append("observer handoff does not identify an instrumented Wine target")
    if (not isinstance(process_identity, dict)
            or process_identity.get("role") != expected_process_identity.get("role")
            or process_identity.get("path") != expected_process_identity.get("path")
            or process_identity.get("sha256") != expected_process_identity.get("sha256")):
        errors.append("observer handoff does not bind the pinned target process image")
    observed_process_path = observer.get("targetProcessImagePath")
    observed_process_hash = observer.get("targetProcessImageSHA256")
    if (observed_process_path != expected_process_identity.get("path")
            or observed_process_hash != expected_process_identity.get("sha256")):
        errors.append("same-PID Wine trace did not identify the pinned Wine host image")
    if observer.get("targetProcessIdentityVerified") is not True:
        errors.append("observer did not verify Wine host identity from the handed-off PID's token-bound trace")
    if observer.get("targetProcessImageVerificationSource") != (
            "same-PID same-token process_identity and startup_module_identity trace"):
        errors.append("observer used an unexpected Wine process-image identity source")
    identity_evidence = observer.get("targetProcessIdentityEvidence")
    if (not isinstance(identity_evidence, dict)
            or identity_evidence.get("source") != (
                "same-PID same-token process_identity and startup_module_identity trace")
            or identity_evidence.get("verified") is not True
            or identity_evidence.get("path") != expected_process_identity.get("path")
            or identity_evidence.get("sha256") != expected_process_identity.get("sha256")):
        errors.append("same-PID Wine process identity evidence is missing or mismatched")
        identity_evidence = {}
    process_row = identity_evidence.get("processIdentity")
    if (not isinstance(process_row, dict)
            or process_row.get("origin") != "INSTRUMENTATION"
            or process_row.get("event") != "process_identity"
            or process_row.get("trace_role") != "target"
            or process_row.get("host_pid") != target_pid
            or process_row.get("target_unix_pid") != target_pid
            or process_row.get("pid") != target_pid
            or process_row.get("experiment_token") != token
            or process_row.get("startup_id") != f"pid-{target_pid}"
            or not isinstance(process_row.get("process_start_monotonic_ns"), int)
            or process_row.get("process_start_monotonic_ns", 0) <= 0
            or process_row.get("main_executable_path") != expected_process_identity.get("path")
            or process_row.get("main_executable_sha256") != expected_process_identity.get("sha256")):
        errors.append("same-PID Wine process_identity row is incomplete or mismatched")
    module_row = identity_evidence.get("mainExecutableModuleIdentity")
    if (not isinstance(module_row, dict)
            or module_row.get("origin") != "INSTRUMENTATION"
            or module_row.get("event") != "startup_module_identity"
            or module_row.get("trace_role") != "target"
            or module_row.get("host_pid") != target_pid
            or module_row.get("target_unix_pid") != target_pid
            or module_row.get("experiment_token") != token
            or module_row.get("startup_id") != f"pid-{target_pid}"
            or module_row.get("role") != "wine"
            or module_row.get("identity_kind") != "dyld-main-executable"
            or module_row.get("loaded_in_process") is not True
            or module_row.get("identity_match") is not True
            or module_row.get("path") != expected_process_identity.get("path")
            or module_row.get("sha256") != expected_process_identity.get("sha256")
            or module_row.get("observed_path") != expected_process_identity.get("path")
            or module_row.get("observed_sha256") != expected_process_identity.get("sha256")):
        errors.append("same-PID Wine main-executable module row is incomplete or mismatched")
    completion = observer.get("experimentCompleteSignal")
    if (not isinstance(completion, dict)
            or completion.get("run_id") != experiment_id
            or completion.get("target_unix_pid") != target_pid
            or completion.get("experiment_token") != token
            or completion.get("status") != "RUNNER_FINISHED"
            or type(completion.get("process_runner_returncode")) is not int
            or completion.get("process_runner_returncode") != 0):
        errors.append("observer completion signal does not bind this run, target PID, and token")
    process_exit_ns = (execution_result.get("target_process_exit_monotonic_ns")
                       if isinstance(execution_result, dict) else None)
    process_exit_valid = bool(
        isinstance(execution_result, dict)
        and execution_result.get("experiment_id") == experiment_id
        and execution_result.get("experiment_token") == token
        and execution_result.get("target_unix_pid") == target_pid
        and type(execution_result.get("process_returncode")) is int
        and execution_result.get("process_returncode") == 0
        and execution_result.get("timed_out") is False
        and type(process_exit_ns) is int and process_exit_ns > 0
    )
    if not process_exit_valid:
        errors.append("execution result does not bind a successful, non-timeout exit of this run's target PID")
    if (not isinstance(completion, dict)
            or completion.get("target_process_exit_monotonic_ns") != process_exit_ns):
        errors.append("observer completion signal does not bind the target process exit timestamp")
    completion_ns = (completion.get("runner_monotonic_ns")
                     if isinstance(completion, dict) else None)
    if (completion_runner_pid is None
            or not isinstance(completion, dict)
            or completion.get("runner_pid") != completion_runner_pid):
        errors.append("observer completion signal was not written by this runner process")
    if (type(completion_ns) is not int or completion_ns <= 0
            or process_exit_valid and completion_ns < process_exit_ns):
        errors.append("observer completion signal does not occur after the verified target exit")
    observer_end_ns = observer.get("endedAtMonotonicNS")
    if (type(observer_end_ns) is not int or observer_end_ns <= 0
            or type(completion_ns) is int and observer_end_ns < completion_ns):
        errors.append("observer result does not end after the authenticated completion signal")
    run_end_valid = type(target_run_end_monotonic_ns) is int and target_run_end_monotonic_ns > 0
    if not run_end_valid:
        errors.append("validated target trace has no final presentation timestamp")
    if (run_end_valid and process_exit_valid
            and (process_exit_ns < target_run_end_monotonic_ns
                 or process_exit_ns - target_run_end_monotonic_ns > 1_500_000_000)):
        errors.append("target process exit is not within one observer interval of the validated final presentation")
    stable = observer.get("canonicalUncoveredState")
    if not isinstance(stable, dict) or stable.get("attained") is not True:
        errors.append("canonical uncovered state did not have three samples over at least two seconds")
    elif ((stable.get("stableDurationNS") or 0) < 2_000_000_000
          or (stable.get("sampleCountInLongestQualifyingStreak") or 0) < 3):
        errors.append("canonical uncovered-state stability interval is insufficient")
    elif (stable.get("telemetryProgressRequired") is not True
          or stable.get("telemetryProgressVerified") is not True
          or (stable.get("telemetrySequenceAdvanceCountInLongestQualifyingStreak") or 0) < 1):
        errors.append("canonical uncovered-state streak lacks advancing Wine AppKit telemetry")
    if observer.get("watchdogTriggered") is not False:
        errors.append("observer safety watchdog fired")
    if observer.get("error") is not None:
        errors.append(f"observer reported an error: {observer.get('error')}")
    if observer.get("covererProcessesAtObserverStart") != []:
        errors.append("a neutral-coverer process was present when the observer started")
    if observer.get("covererWindowsAtObserverStart") != []:
        errors.append("a neutral-coverer window was present when the observer started")
    if observer.get("covererProcessEnumerationSucceededAtObserverStart") is not True:
        errors.append("coverer process enumeration failed when the observer started")
    if observer.get("observerStartWindowQuerySucceeded") is not True:
        errors.append("CGWindow enumeration failed when the observer started")
    for query in observer.get("observerQueriesBeforeTargetHandoff", []):
        if query.get("covererProcesses") or query.get("covererWindows"):
            errors.append("a neutral-coverer process/window was present before target handoff")
            break
    samples = observer.get("samples")
    if not isinstance(samples, list) or not samples:
        errors.append("observer captured no target-window samples")
        samples = []
    target_samples = [sample for sample in samples
                      if isinstance(sample.get("target"), dict)
                      and sample["target"].get("windowID") is not None]
    if not target_samples:
        errors.append("observer never identified a target window owned by the Wine PID")
    if any(sample.get("neutralCovererProcesses") or sample.get("neutralCovererWindows")
           for sample in samples):
        errors.append("neutral-coverer process/window was present during the uncovered authorization smoke")
    if any(sample.get("alphaOpaqueFullCoverCandidatePresent") is True for sample in target_samples):
        errors.append("an opaque full-cover candidate overlapped the target during authorization")
    eligible = [sample for sample in target_samples
                if sample.get("canonicalUncoveredEligible") is True]
    expected_window_id = observer.get("targetWindowID")

    def exact_target_identity(sample: dict[str, Any]) -> bool:
        target = sample.get("target")
        return bool(
            isinstance(target, dict)
            and target.get("ownerPID") == target_pid
            and target.get("windowID") == expected_window_id
            and target.get("processAlive") is True
            and target.get("processIdentityMatches") is True
            and target.get("processImagePath") == expected_process_identity.get("path")
            and target.get("processImageSHA256") == expected_process_identity.get("sha256")
        )

    if any(not exact_target_identity(sample) for sample in eligible):
        errors.append("canonical uncovered sample does not match the exact handed-off PID, window, and image")
    if not eligible:
        errors.append("no target window sample independently satisfied canonical uncovered state")
    else:
        stable_end = stable.get("stabilityProvenMonotonicNS") if isinstance(stable, dict) else None
        def terminal_ax_query_miss(sample: dict[str, Any]) -> bool:
            """Allow one otherwise-clear final sample when only AX focus data is unavailable."""
            target = sample.get("target")
            accessibility = target.get("accessibility") if isinstance(target, dict) else None
            display = target.get("displayRelationship") if isinstance(target, dict) else None
            telemetry = sample.get("inProcessTelemetry")
            app = sample.get("app")
            query = sample.get("query")
            sample_ns = sample.get("sampledAtMonotonicNS")
            telemetry_ns = (telemetry.get("timestampMachNS")
                            if isinstance(telemetry, dict) else None)
            telemetry_age_ns = (telemetry.get("ageNS")
                                if isinstance(telemetry, dict) else None)
            return bool(
                process_exit_valid and run_end_valid
                and type(sample_ns) is int
                and sample_ns <= target_run_end_monotonic_ns
                and target_run_end_monotonic_ns - sample_ns <= 1_500_000_000
                and 0 <= process_exit_ns - sample_ns <= 1_500_000_000
                and isinstance(target, dict)
                and target.get("ownerPID") == target_pid
                and target.get("windowID") == observer.get("targetWindowID")
                and target.get("processAlive") is True
                and target.get("processIdentityMatches") is True
                and target.get("processImagePath") == expected_process_identity.get("path")
                and target.get("processImageSHA256") == expected_process_identity.get("sha256")
                and target.get("titleMatched") is True
                and target.get("onScreen") is True
                and target.get("cgWindowIsOnscreen") is True
                and type(target.get("rankFrontToBack")) is int
                and target.get("rankFrontToBack", -1) >= 0
                and target.get("rankSource") == "onScreenWindowList"
                and target.get("windowIdentityAmbiguous") is False
                and target.get("accessibilityMinimized") is None
                and target.get("appKitMiniaturized") is False
                and isinstance(accessibility, dict)
                and accessibility.get("accessibilityTrusted") is None
                and accessibility.get("focused") is None
                and accessibility.get("minimized") is None
                and target.get("alpha") == 1
                and isinstance(display, dict)
                and display.get("uniqueExpectedDisplayMatch") is True
                and display.get("targetFullyOnExpectedDisplay") is True
                and isinstance(app, dict) and app.get("isActive") is True
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
                and type(telemetry_ns) is int and 0 < telemetry_ns <= sample_ns
                and type(telemetry_age_ns) is int
                and 0 <= telemetry_age_ns <= 1_500_000_000
                and sample.get("allWindowListQuerySucceeded") is True
                and sample.get("onScreenWindowListQuerySucceeded") is True
                and sample.get("aboveTargetWindowDetailsComplete") is True
                and sample.get("covererProcessEnumerationSucceeded") is True
                and sample.get("alphaOpaqueFullCoverCandidatePresent") is False
                and sample.get("neutralCovererProcessPresent") is False
                and sample.get("neutralCovererWindowPresent") is False
                and sample.get("neutralCovererCoveringWindowPresent") is False
                and sample.get("neutralCovererProcesses") == []
                and sample.get("neutralCovererWindows") == []
                and isinstance(query, dict)
                and query.get("status") == "TARGET_WINDOW_FOUND"
                and target.get("windowIdentityError") is None
            )

        if isinstance(stable_end, int) and run_end_valid and process_exit_valid:
            post_attainment = []
            for sample in samples:
                if not isinstance(sample, dict):
                    errors.append("observer sample is not a structured row")
                    continue
                sample_ns = sample.get("sampledAtMonotonicNS")
                if type(sample_ns) is not int or sample_ns <= 0:
                    errors.append("observer sample has no valid monotonic timestamp")
                    continue
                if sample_ns >= stable_end:
                    post_attainment.append(sample)
            terminal_ax_sample_ns = None
            for sample in post_attainment:
                sample_ns = sample["sampledAtMonotonicNS"]
                target = sample.get("target")
                if sample_ns >= process_exit_ns:
                    if (not isinstance(target, dict)
                            or target.get("processAlive") is not False
                            or target.get("windowID") is not None):
                        errors.append("observer still reports a target process/window after its verified exit")
                    continue
                if sample_ns > target_run_end_monotonic_ns:
                    continue
                if (sample.get("canonicalUncoveredEligible") is True
                        and exact_target_identity(sample)):
                    continue
                if terminal_ax_query_miss(sample):
                    terminal_ax_gap_count += 1
                    terminal_ax_sample_ns = sample_ns
                    continue
                errors.append("target left the canonical uncovered state during its measured run")
                break
            if terminal_ax_gap_count > 1:
                errors.append("more than one terminal accessibility focus query was unavailable")
            if terminal_ax_sample_ns is not None and any(
                    sample["sampledAtMonotonicNS"] > terminal_ax_sample_ns
                    and sample["sampledAtMonotonicNS"] < process_exit_ns
                    and isinstance(sample.get("target"), dict)
                    and (sample["target"].get("processAlive") is True
                         or sample["target"].get("windowID") is not None)
                    for sample in post_attainment):
                errors.append("terminal accessibility query miss was not followed by target exit")
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "run_id": observer.get("targetRunID"),
        "target_unix_pid": observer.get("targetPID"),
        "runner_pid": handoff.get("runner_pid"),
        "target_window_id": observer.get("targetWindowID"),
        "sample_count": observer.get("sampleCount"),
        "canonical_uncovered_state": stable,
        "target_run_end_monotonic_ns": target_run_end_monotonic_ns,
        "target_process_exit_monotonic_ns": process_exit_ns,
        "terminal_accessibility_gap_samples_accepted": terminal_ax_gap_count,
        "experiment_complete_signal": completion,
        "neutral_coverer_processes_at_start": observer.get("covererProcessesAtObserverStart"),
        "neutral_coverer_windows_at_start": observer.get("covererWindowsAtObserverStart"),
        "observer_error": observer.get("error"),
    }


def summarize_smoke_signature(validation: dict[str, Any] | None) -> dict[str, Any]:
    """Derive the admitted refresh signature from validated per-callback records."""
    if not isinstance(validation, dict):
        return {"status": "INCOMPLETE", "healthy_refresh": False}
    frames = validation.get("frames")
    app_configuration = validation.get("app_configuration")
    if not isinstance(frames, list) or not isinstance(app_configuration, dict):
        return {"status": "INCOMPLETE", "healthy_refresh": False}
    duration_ns = app_configuration.get("actual_run_ns")
    callbacks = [frame for frame in frames if isinstance(frame, dict)
                 and isinstance(frame.get("presented_time_mach_ns"), int)]
    if (not isinstance(duration_ns, int) or duration_ns <= 0
            or len(callbacks) != len(frames) or not callbacks):
        return {
            "status": "INCOMPLETE", "healthy_refresh": False,
            "callback_count": len(callbacks), "frame_count": len(frames),
            "duration_ns": duration_ns,
        }
    positive = sum(frame["presented_time_mach_ns"] > 0 for frame in callbacks)
    zeros = sum(frame["presented_time_mach_ns"] == 0 for frame in callbacks)
    duration_seconds = duration_ns / 1_000_000_000
    acquire_fps = validation.get("acquired_fps")
    displayed_fps = positive / duration_seconds
    zero_fraction = zeros / len(callbacks)
    healthy = bool(
        validation.get("quantitative_ready") is True
        and isinstance(acquire_fps, (int, float))
        and 45 <= acquire_fps <= 75
        and 45 <= displayed_fps <= 75
        and zero_fraction <= 0.05
        and validation.get("displayed_frames") == positive
        and validation.get("frame_count") == len(callbacks)
    )
    pathological = bool(acquire_fps >= 100 and displayed_fps <= 5 and zero_fraction >= 0.5) \
        if isinstance(acquire_fps, (int, float)) else False
    return {
        "status": "HEALTHY_REFRESH" if healthy else
                  "FAST_ZERO" if pathological else "DIFFERENT_OR_UNHEALTHY",
        "healthy_refresh": healthy,
        "acquired_fps": acquire_fps,
        "displayed_fps": displayed_fps,
        "displayed_callback_count": positive,
        "raw_zero_callback_count": zeros,
        "callback_count": len(callbacks),
        "raw_zero_fraction": zero_fraction,
        "duration_seconds": duration_seconds,
        "quantitative_ready": validation.get("quantitative_ready") is True,
        "validator_frame_count": validation.get("frame_count"),
        "validator_displayed_frames": validation.get("displayed_frames"),
    }


def read_execution_result(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    """Load the process runner record without following links or assuming it exists."""
    if path.is_symlink():
        return None, "execution result is a symlink"
    if not path.is_file():
        return None, "execution result is missing"
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, f"execution result could not be read: {exc}"
    if not isinstance(result, dict):
        return None, "execution result must contain a JSON object"
    return result, None


def file_identity(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.is_symlink():
        raise RuntimeError(f"required pinned file is missing, non-regular, or a symlink: {path}")
    return {"path": str(path.resolve(strict=True)), "sha256": sha256(path),
            "size_bytes": path.stat().st_size}


def candidate_files() -> dict[str, dict[str, Any]]:
    files = {
        "wine_host_image": WINE_HOST_EXECUTABLE, "wine_launcher": WINE,
        "wineserver": WINESERVER,
        "winevulkan_dll": WINEVULKAN_DLL, "winevulkan_so": WINEVULKAN_SO,
        "wine_host_loader": WINE_HOST_LOADER, "winemac_so": WINEMAC_SO,
        "moltenvk_original": MOLTENVK_ORIGINAL,
        "moltenvk_icd_library": MOLTENVK_ICD_LIBRARY,
        "moltenvk_icd": ICD, "wine_runtime_manifest": RUNTIME_MANIFEST,
        "app": APP, "app_source": APP_SOURCE, "trace_proxy": PROXY,
        "app_build_receipt": APP_BUILD_RECEIPT,
        "trace_proxy_build_receipt": BR9R2_EVIDENCE / "start-gate-telemetry-post-config-proxy-build.json",
        "trace_proxy_source": PROXY_SOURCE, "process_runner": PROCESS_RUNNER,
        "trace_validator": TRACE_VALIDATOR, "runner": HERE / "runner_b.py",
        "storage_policy": STORAGE / "storage_policy.py",
        "trace_processes": HERE / "trace_processes.py",
        "loader_evidence": HERE / "loader_evidence.py",
    }
    identities = {name: file_identity(path) for name, path in files.items()}
    expected = {
        "wine_host_image": "38525a48b11217238977eff6fa1f573ffed88a8534975c9477827e886ec0ec94",
        "wine_launcher": "946a7032484d02981182d59f82edda03f40f38c510e0208d48482c0830ef20e9",
        "wineserver": "2af98e4aedd73515e5980e21585ebb6e6ad6c705242d760df24b7fd83fe2d51a",
        "winevulkan_dll": "a18992aca34493aae325a5c1c6ddc78b905876fb65b2ca91fe7b1882b33fd2dc",
        "winevulkan_so": "6362dd64890738166dda79bdd9fcc94df55857c1f9756a3268ae91b8d3c38d88",
        "wine_host_loader": "b43893430e929e17086653d9c2266a0d6dd4863b8739c005c3c9a9bdb2a86f1a",
        "winemac_so": "e39ec04fbe3db971e272e13209bffc5ccb059232015b565e5082e161069df199",
        "moltenvk_original": "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f",
        "moltenvk_icd_library": "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f",
        "moltenvk_icd": "578ff08cd0d8734619357541771a5abc9c3470ca300030219a971a9e9dbbe466",
        "wine_runtime_manifest": "662a47726872910cf7e0c9f16d3d416a0183cff0c974ba47150ea916b4186a95",
        "app_source": "b51e1e2636587c5aedeaee2aa6915a0ed9de5019db1097606baba2d1a4d22c16",
        "app": "31fd01f5b59c408053a3f82146427fae9d1b5b515ce9fe2ed681358aed8ad088",
        "app_build_receipt": "e0128f9001a0f9da84f16e2534e29223075048111a6896e2ddf0f933379032ee",
        "trace_proxy": "dd8a87b3743abe07fa428c02c5a4f737100e0893c13c91a45aeee530736a5290",
        "trace_proxy_source": "26e942682d93c91153f5e0a2baa6c4767a0047c4b9dcc38075c57dc1d789059c",
        "trace_proxy_build_receipt": "0e1960e749d6aba4b046b38e68146faeb838def9891ac538f2568f1067b3a654",
    }
    for name, digest in expected.items():
        if identities[name]["sha256"] != digest:
            raise RuntimeError(f"pinned B input changed: {name} {identities[name]['sha256']} != {digest}")
    template_dll = storage_policy.PREFIX_TEMPLATE / "drive_c/windows/system32/winevulkan.dll"
    template_hash = sha256(template_dll)
    if template_hash != identities["winevulkan_dll"]["sha256"]:
        raise RuntimeError("certified prefix template's builtin winevulkan.dll differs from the pinned Wine module")
    runtime_manifest = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
    if runtime_manifest.get("tree_sha256") != "2fea825b205073bf011ce948ec4cbe6560d9d90e26982233ad05822a17a92bfe":
        raise RuntimeError("pinned Wine runtime tree manifest changed")
    identities["template_winevulkan_dll"] = file_identity(template_dll)
    identities["wine_runtime_tree"] = {
        "path": str(RUNTIME), "sha256": runtime_manifest["tree_sha256"],
        "manifest_path": str(RUNTIME_MANIFEST.resolve()),
        "manifest_sha256": identities["wine_runtime_manifest"]["sha256"],
    }
    return identities


def wine_execution_binding(inputs: dict[str, dict[str, Any]]) -> dict[str, str]:
    """Keep the certified launcher and the loaded host image independent."""
    return {
        "wine_launcher_path": inputs["wine_launcher"]["path"],
        "wine_launcher_sha256": inputs["wine_launcher"]["sha256"],
        "wine_host_image_path": inputs["wine_host_image"]["path"],
        "wine_host_image_sha256": inputs["wine_host_image"]["sha256"],
        "wine_runtime_tree_manifest_sha256": inputs["wine_runtime_manifest"]["sha256"],
    }


def wine_candidate_hashes(inputs: dict[str, dict[str, Any]]) -> dict[str, str]:
    candidate_hashes = {name: row["sha256"] for name, row in inputs.items()
                        if name not in {"wine_runtime_tree", "template_winevulkan_dll"}}
    candidate_hashes["wine_runtime_tree"] = inputs["wine_runtime_tree"]["sha256"]
    candidate_hashes["wine_binary"] = inputs["wine_launcher"]["sha256"]
    candidate_hashes["wine_host_image"] = inputs["wine_host_image"]["sha256"]
    candidate_hashes["wine_launcher_binary"] = inputs["wine_launcher"]["sha256"]
    candidate_hashes["moltenvk"] = inputs["moltenvk_original"]["sha256"]
    return candidate_hashes


def smoke_authorization_bindings() -> dict[str, Any]:
    identities = candidate_files()
    return {
        "control_version": CONTROL_VERSION,
        "architecture": "x86_64-under-Rosetta",
        "candidate_identities": {name: row["sha256"] for name, row in identities.items()},
        "gate0_control_hashes": storage_policy._gate0_control_hashes(),
        "gate0_policy_sha256": sha256(STORAGE / "storage_policy.py"),
        "runner_sha256": sha256(HERE / "runner_b.py"),
        "process_runner_sha256": sha256(PROCESS_RUNNER),
        "trace_validator_sha256": sha256(TRACE_VALIDATOR),
        "public_window_observer_source_sha256": sha256(PUBLIC_WINDOW_OBSERVER_SOURCE),
        "lifecycle_observer_controller_sha256": sha256(LIFECYCLE_OBSERVER_CONTROLLER),
        "authorization_smoke_controller_sha256": sha256(AUTHORIZATION_SMOKE_CONTROLLER),
        "coverer_sequence_controller_sha256": sha256(COVERER_SEQUENCE_CONTROLLER),
        "validator_contract": "bare-wine-trace-validator-preframe-dll-binding-startup-and-clock-v6",
    }


def automatic_post_matches(receipt: dict[str, Any] | None, experiment_id: str,
                           prefix: Path, manifest_sha256: str) -> bool:
    if not isinstance(receipt, dict) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        return False
    identity = receipt.get("wineprefix_identity", {})
    shutdown = receipt.get("wineserver_shutdown", [])
    return bool(
        storage_policy._verify_storage_evidence(receipt, "cleanup_receipt_hmac_sha256")
        and receipt.get("experiment_id") == experiment_id
        and receipt.get("prefix_path") == str(prefix)
        and receipt.get("status") == "PASS"
        and receipt.get("storage_hygiene_status") == "PASS"
        and receipt.get("free_space_status") == "PASS"
        and receipt.get("prefix_deleted") is True
        and receipt.get("active_lease_marker_removed_after_receipt") is True
        and identity.get("experiment_id") == experiment_id
        and identity.get("manifest_sha256") == manifest_sha256
        and all(identity.get(key) == str(prefix) for key in (
            "canonical_absolute_path", "launch_WINEPREFIX", "cleanup_WINEPREFIX"))
        and len(shutdown) == 2
        and {row.get("flag") for row in shutdown} == {"-k", "-w"}
        and all(row.get("returncode") == 0 and row.get("WINEPREFIX") == str(prefix)
                and row.get("prefix_identity", {}).get("experiment_id") == experiment_id
                and row.get("prefix_identity", {}).get("manifest_sha256") == manifest_sha256
                for row in shutdown))


def verify_smoke_authorization(smoke_id: str, *, runs_root: Path | None = None,
                               current_bindings: dict[str, Any] | None = None) -> tuple[dict[str, Any], Path]:
    if not re.fullmatch(r"step11d3b-smoke-\d{8}-\d{6}-[0-9a-f]{8}", smoke_id):
        raise RuntimeError("baseline authorization requires the exact ID of a new BR2 smoke")
    root = (HERE / "runs") if runs_root is None else runs_root
    run_dir = root / smoke_id
    auth_path = run_dir / "smoke-authorization.json"
    result_path = run_dir / "run-result.json"
    if (not auth_path.is_file() or auth_path.is_symlink()
            or not result_path.is_file() or result_path.is_symlink()):
        raise RuntimeError("baseline requires an authenticated prior smoke artifact and run result")
    try:
        authorization = json.loads(auth_path.read_text(encoding="utf-8"))
        run_result_bytes = result_path.read_bytes()
        run_result = json.loads(run_result_bytes)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("prior smoke authorization artifacts are unreadable") from error
    if not storage_policy._verify_storage_evidence(authorization, SMOKE_AUTH_FIELD):
        raise RuntimeError("prior smoke authorization HMAC is missing or invalid")
    if (authorization.get("experiment_id") != smoke_id
            or authorization.get("phase") != "smoke"
            or authorization.get("status") != "PASS"
            or authorization.get("is_valid") is not True
            or authorization.get("quantitative_ready") is not True
            or authorization.get("prefixlease_automatic_post") != "PASS"
            or authorization.get("gate0_post") != "PASS"
            or not isinstance(authorization.get("smoke_signature"), dict)
            or authorization["smoke_signature"].get("healthy_refresh") is not True
            or authorization.get("run_result_path") != str(result_path.resolve())
            or authorization.get("run_result_sha256") != hashlib.sha256(run_result_bytes).hexdigest()):
        raise RuntimeError("prior smoke authorization does not bind a complete passing smoke and automatic POST")
    if (run_result.get("experiment_id") != smoke_id
            or run_result.get("phase") != "smoke"
            or run_result.get("status") != "PASS"
            or run_result.get("is_valid") is not True
            or run_result.get("process_returncode") != 0
            or run_result.get("timed_out") is not False
            or run_result.get("duration_seconds_requested") != 30
            or run_result.get("prefix_cleanup_pass") is not True
            or run_result.get("screenshot_check_pass") is not True
            or run_result.get("gate0_post", {}).get("gate0_status") != "PASS"
            or run_result.get("validation", {}).get("is_valid") is not True
            or run_result.get("validation", {}).get("quantitative_ready") is not True
            or not isinstance(run_result.get("smoke_signature"), dict)
            or run_result["smoke_signature"].get("healthy_refresh") is not True
            or run_result.get("start_gate_validation", {}).get("status") != "PASS"
            or authorization.get("smoke_signature") != run_result.get("smoke_signature")
            or authorization.get("start_gate_validation") != run_result.get("start_gate_validation")
            or authorization.get("start_gate_signal") != run_result.get("start_gate_signal")):
        raise RuntimeError("prior smoke run result no longer satisfies the baseline admission contract")
    observer_binding = run_result.get("public_window_observer", {})
    observer_path = Path(observer_binding.get("path", ""))
    if (observer_binding.get("status") != "PASS"
            or observer_binding.get("canonical_uncovered_state", {}).get("attained") is not True
            or not observer_path.is_absolute() or observer_path.is_symlink()
            or not observer_path.is_file()
            or sha256(observer_path) != observer_binding.get("sha256")):
        raise RuntimeError("prior smoke lacks a final hash-bound canonical uncovered observer result")
    if not automatic_post_matches(
            run_result.get("prefix_cleanup"), smoke_id,
            storage_policy.PREFIX_ROOT / f"{smoke_id}-prefix",
            str(run_result.get("storage_manifest_sha256", ""))):
        raise RuntimeError("prior smoke automatic POST does not bind this smoke prefix and manifest")
    current = current_bindings if current_bindings is not None else smoke_authorization_bindings()
    if (authorization.get("bindings") != current
            or run_result.get("control_binding_snapshot") != current):
        raise RuntimeError("prior smoke authorization is stale because a runtime or control hash changed")
    if authorization.get("trace_artifacts") != run_result.get("trace_artifacts"):
        raise RuntimeError("prior smoke trace artifact bindings differ")
    verify_trace_artifact_bindings(run_dir, authorization.get("trace_artifacts"),
                                   authorization_smoke=True)
    return authorization, auth_path


def verify_trace_artifact_bindings(run_dir: Path, artifacts: dict | None, *,
                                   authorization_smoke: bool = False) -> None:
    required_roles = {"target_host_trace", "trace_index", "app_trace"}
    if authorization_smoke:
        required_roles |= {
            "public_window_start_signal", "public_window_start_state",
            "public_window_start_streak",
        }
    if not isinstance(artifacts, dict) or set(artifacts) != required_roles:
        raise RuntimeError("prior smoke requires complete target, index, app, and start-gate bindings")
    for role, row in artifacts.items():
        path = Path(row.get("path", ""))
        if role in {"target_host_trace", "trace_index", "app_trace"}:
            expected_parent = run_dir / "host-traces" if role == "target_host_trace" else run_dir
            expected_name = {"trace_index": "trace-index.json", "app_trace": "app-trace.jsonl"}.get(role)
            path_valid = (
                path.parent == expected_parent
                and (expected_name is None or path.name == expected_name)
                and (role != "target_host_trace"
                     or re.fullmatch(r"host-trace\.[1-9][0-9]*\.jsonl", path.name) is not None)
            )
        else:
            evidence_root = observer_evidence_root()
            expected_names = {
                "public_window_start_signal": "authorization-start-signal.json",
                "public_window_start_state": r"authorization-start-state-[1-9][0-9]*\.json",
                "public_window_start_streak": r"authorization-start-streak-[1-9][0-9]*\.jsonl",
            }
            expected_name = expected_names[role]
            name_valid = (path.name == expected_name if role == "public_window_start_signal"
                          else re.fullmatch(expected_name, path.name) is not None)
            path_valid = path.parent.parent == evidence_root and path.parent.name.endswith("-observer") and name_valid
        if (not path_valid or not path.is_absolute()
                or str(path.resolve(strict=True)) != str(path) or path.is_symlink()
                or not path.is_file() or sha256(path) != row.get("sha256")):
            raise RuntimeError(f"prior smoke {role} artifact is missing, unsafe, or changed")


def validate_start_gate_signal(path: Path, *, experiment_id: str,
                               target_pid: int | None, token: str) -> tuple[dict[str, Any] | None,
                                                                             dict[str, Any]]:
    errors: list[str] = []
    if path.is_symlink() or not path.is_file():
        return None, {"status": "FAIL", "errors": ["public-window start-gate signal is missing or unsafe"]}
    try:
        raw = path.read_bytes()
        signal = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        return None, {"status": "FAIL", "errors": [f"public-window start-gate signal is unreadable: {error}"]}
    if not isinstance(signal, dict):
        return None, {"status": "FAIL", "errors": ["public-window start-gate signal is not an object"]}
    if (signal.get("schema_version") != 1
            or signal.get("status") != "CANONICAL_UNCOVERED_ATTAINED"
            or signal.get("run_id") != experiment_id
            or signal.get("target_unix_pid") != target_pid
            or signal.get("experiment_token") != token
            or signal.get("clock_domain") != "darwin_mach_absolute_time_ns"):
        errors.append("start-gate signal does not bind this run, target PID, token, or monotonic clock")
    if (signal.get("canonical_uncovered_attained") is not True
            or signal.get("title_matched") is not True
            or signal.get("target_process_identity_matches") is not True
            or signal.get("target_visible") is not True
            or signal.get("window_unoccluded") is not True
            or signal.get("app_active") is not True
            or signal.get("telemetry_progress_required") is not True
            or signal.get("telemetry_progress_verified") is not True
            or signal.get("neutral_coverer_process_count") != 0
            or signal.get("neutral_coverer_window_count") != 0):
        errors.append("start-gate signal lacks canonical visible, identity, telemetry, or no-coverer evidence")
    stable_start = signal.get("stable_start_monotonic_ns")
    attained = signal.get("stability_proven_monotonic_ns")
    stable_duration = signal.get("stable_duration_ns")
    sample_count = signal.get("sample_count")
    window_id = signal.get("target_window_id")
    released = signal.get("released_monotonic_ns")
    sample_age = signal.get("current_sample_age_at_release_ns")
    if (type(stable_start) is not int or type(attained) is not int
            or attained < stable_start or attained - stable_start < 2_000_000_000
            or type(stable_duration) is not int or stable_duration < 2_000_000_000
            or type(sample_count) is not int or sample_count < 3
            or type(window_id) is not int or window_id <= 0
            or type(released) is not int or released < attained
            or type(sample_age) is not int or sample_age < 0
            or sample_age > 1_500_000_000 or released - attained != sample_age):
        errors.append("start-gate timing or stable-state counts are insufficient or inconsistent")
    if signal.get("coverer_binary_sha256") != "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc":
        errors.append("start-gate signal does not bind the immutable neutral-coverer identity")
    observer_directory = path.parent.resolve(strict=True)
    evidence_paths: dict[str, Path] = {}
    for role, path_key, hash_key, pattern in (
        ("state", "start_state_snapshot_path", "start_state_snapshot_sha256",
         r"authorization-start-state-[1-9][0-9]*\.json"),
        ("streak", "start_streak_snapshot_path", "start_streak_snapshot_sha256",
         r"authorization-start-streak-[1-9][0-9]*\.jsonl"),
    ):
        raw_path = signal.get(path_key)
        expected_hash = signal.get(hash_key)
        if not isinstance(raw_path, str):
            errors.append(f"start-gate {role} snapshot path is missing")
            continue
        snapshot_path = Path(raw_path)
        if (not snapshot_path.is_absolute() or snapshot_path.parent != observer_directory
                or re.fullmatch(pattern, snapshot_path.name) is None
                or snapshot_path.is_symlink() or not snapshot_path.is_file()
                or str(snapshot_path.resolve(strict=True)) != raw_path
                or not isinstance(expected_hash, str) or sha256(snapshot_path) != expected_hash):
            errors.append(f"start-gate {role} snapshot is missing, unsafe, or changed")
        else:
            evidence_paths[role] = snapshot_path
    if not errors and set(evidence_paths) == {"state", "streak"}:
        try:
            latest_state = json.loads(evidence_paths["state"].read_text(encoding="utf-8"))
            samples = [json.loads(line) for line in evidence_paths["streak"].read_text(encoding="utf-8").splitlines()]
            proof = current_canonical_uncovered_streak(
                samples, target_pid=target_pid or 0, latest_state=latest_state)
        except (OSError, json.JSONDecodeError, TypeError) as error:
            proof = None
            errors.append(f"start-gate stable-state snapshots are unreadable: {error}")
        if proof is None:
            errors.append("start-gate samples do not independently establish the current uncovered streak")
        else:
            for key in ("stable_start_monotonic_ns", "stability_proven_monotonic_ns",
                        "stable_duration_ns", "sample_count", "target_window_id",
                        "telemetry_sequence_start", "telemetry_sequence_end",
                        "telemetry_timestamp_start_mach_ns", "telemetry_timestamp_end_mach_ns"):
                if proof.get(key) != signal.get(key):
                    errors.append(f"start-gate signal {key} differs from its persisted sample proof")
                    break
    handoff_trace = (HERE / "runs" / experiment_id / "host-traces"
                     / f"host-trace.{target_pid}.jsonl").resolve()
    if signal.get("target_trace_path") != str(handoff_trace):
        errors.append("start-gate signal target trace path differs from this run's exact target trace")
    expected_pe = signal.get("expected_pe_identity")
    if (not isinstance(expected_pe, dict) or expected_pe.get("path") != str(APP.resolve())
            or expected_pe.get("sha256") != sha256(APP)):
        errors.append("start-gate signal does not bind the current gated PE identity")
    expected_process = signal.get("expected_target_process_identity")
    if (not isinstance(expected_process, dict)
            or expected_process.get("path") != str(WINE_HOST_EXECUTABLE.resolve())
            or expected_process.get("sha256") != sha256(WINE_HOST_EXECUTABLE)):
        errors.append("start-gate signal does not bind the pinned Wine host process image")
    evidence = {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "path": str(path.resolve()),
        "sha256": sha256(path),
        "run_id": signal.get("run_id"),
        "target_unix_pid": signal.get("target_unix_pid"),
        "experiment_token": signal.get("experiment_token"),
        "stability_proven_monotonic_ns": attained,
        "released_monotonic_ns": released,
        "stable_duration_ns": stable_duration,
        "sample_count": sample_count,
        "target_window_id": window_id,
        "samples": {role: str(snapshot.resolve()) for role, snapshot in evidence_paths.items()},
    }
    return signal, evidence


def validate_start_gate_order(signal: dict[str, Any] | None, *,
                              experiment_id: str, target_pid: int | None,
                              token: str, signal_path: Path,
                              app_events: list[dict[str, Any]],
                              host_events: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[str] = []
    if not isinstance(signal, dict):
        return {"status": "FAIL", "errors": ["validated start-gate signal is unavailable"]}
    app_configs = [row for row in app_events
                   if row.get("origin") == "APP_CONFIG" and row.get("event") == "app_configuration"]
    if len(app_configs) != 1:
        errors.append("app trace does not contain exactly one gated app configuration")
        app_config: dict[str, Any] = {}
    else:
        app_config = app_configs[0]
    wait_begin = app_config.get("public_window_start_gate_wait_begin_qpc_ticks")
    passed = app_config.get("public_window_start_gate_passed_qpc_ticks")
    if (app_config.get("public_window_start_gate_required") is not True
            or app_config.get("public_window_start_gate_passed") is not True
            or app_config.get("run_id") != experiment_id
            or app_config.get("target_unix_pid") != target_pid
            or app_config.get("experiment_token") != token
            or app_config.get("public_window_start_gate_released_monotonic_ns")
            != signal.get("released_monotonic_ns")
            or str(app_config.get("public_window_start_gate_path", "")).casefold()
            != windows_path(signal_path).casefold()
            or type(wait_begin) is not int or wait_begin <= 0
            or type(passed) is not int or passed <= wait_begin):
        errors.append("app trace does not attest to passing this run's public-window start gate")
    app_acquisitions = [row for row in app_events
                        if row.get("origin") == "APP_SOURCE"
                        and row.get("event") == "app_vkAcquireNextImageKHR_begin"]
    first_app_acquisition = min(
        (row.get("call_begin_qpc_ticks") for row in app_acquisitions
         if type(row.get("call_begin_qpc_ticks")) is int), default=None)
    if (first_app_acquisition is None or type(passed) is not int
            or first_app_acquisition <= passed):
        errors.append("first app acquire did not follow the app's recorded gate release")
    host_acquisitions = [row for row in host_events
                         if row.get("origin") == "MVK_BOUNDARY"
                         and row.get("event") == "vkAcquireNextImageKHR_begin"
                         and row.get("trace_role") == "target"
                         and row.get("host_pid") == target_pid
                         and row.get("target_unix_pid") == target_pid
                         and row.get("experiment_token") == token]
    first_host_acquisition = min(
        (row.get("ts_mach_ns") for row in host_acquisitions
         if type(row.get("ts_mach_ns")) is int), default=None)
    release_ns = signal.get("released_monotonic_ns")
    if (not host_acquisitions or type(first_host_acquisition) is not int
            or type(release_ns) is not int or first_host_acquisition <= release_ns):
        errors.append("first target vkAcquireNextImageKHR call did not follow uncovered-state attainment and release")
    if any(type(row.get("ts_mach_ns")) is not int or row["ts_mach_ns"] <= release_ns
           for row in host_acquisitions if type(release_ns) is int):
        errors.append("a target vkAcquireNextImageKHR call preceded the canonical start-gate release")
    return {
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
        "run_id": experiment_id,
        "target_unix_pid": target_pid,
        "experiment_token": token,
        "app_start_gate_wait_begin_qpc_ticks": wait_begin,
        "app_start_gate_passed_qpc_ticks": passed,
        "first_app_acquire_qpc_ticks": first_app_acquisition,
        "start_gate_released_monotonic_ns": release_ns,
        "first_host_acquire_mach_ns": first_host_acquisition,
        "host_acquisition_count": len(host_acquisitions),
        "signal_path": str(signal_path.resolve()) if signal_path.exists() else str(signal_path),
    }


def gate0_snapshot(stage: str) -> dict[str, Any]:
    blockers = storage_policy._gate0_blockers()
    free_bytes = storage_policy.disk_free_bytes(storage_policy.REPO)
    project = storage_policy.measure_project_usage()
    scoped_bytes = storage_policy.conservative_project_usage_bytes(project)
    attestation = json.loads(storage_policy.GATE0_ATTESTATION_PATH.read_text(encoding="utf-8"))
    retention = attestation.get("build_retention_state", {})
    active_prefixes = [str(path) for path in storage_policy._active_lease_markers()]
    uncertified = [str(path) for path in storage_policy.uncertified_prefixes()]
    return {
        "stage": stage, "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "gate0_status": "PASS" if not blockers else "BLOCKED",
        "gate0_blockers": blockers,
        "free_bytes": free_bytes, "free_gib": free_bytes / GIB,
        "scoped_fgmetal_bytes": scoped_bytes, "scoped_fgmetal_gib": scoped_bytes / GIB,
        "formal_free_minimum_bytes": storage_policy.FREE_MINIMUM,
        "preferred_free_target_bytes": storage_policy.FREE_PREFERRED,
        "formal_scope_maximum_bytes": storage_policy.GATE0_PROJECT_BUDGET,
        "scoped_warning_threshold_bytes": storage_policy.PROJECT_WARNING,
        "emergency_hard_stop_bytes": storage_policy.FREE_HARD_STOP,
        "storage_warnings": (
            ([f"free disk is below the {storage_policy.FREE_PREFERRED // GIB} GiB preferred target"]
             if free_bytes < storage_policy.FREE_PREFERRED else [])
            + ([f"scoped allocation exceeds the {storage_policy.PROJECT_WARNING // GIB} GiB warning threshold"]
               if scoped_bytes > storage_policy.PROJECT_WARNING else [])),
        "active_prefix_leases": active_prefixes,
        "active_build_count": retention.get("active_build_count"),
        "active_builds": [] if retention.get("active_build_count") == 0 else None,
        "preserved_prefixes": [str(path) for path in storage_policy.preserved_prefixes()],
        "uncertified_prefixes": uncertified,
    }


def windows_path(path: Path) -> str:
    absolute = str(path.resolve())
    if not absolute.startswith("/"):
        raise ValueError("Wine Z: trace output path must be absolute")
    return "Z:" + absolute.replace("/", "\\")


def clean_environment(prefix: Path, output: Path, app_trace_path: Path,
                      host_trace_path: Path, duration_seconds: int, *, experiment_token: str,
                      experiment_id: str | None = None,
                      start_gate_signal_path: Path | None = None) -> dict[str, str]:
    env = {key: os.environ[key] for key in
           ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TERM")
           if key in os.environ}
    env["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    runtime_paths = (
        RUNTIME / "lib/wine/x86_64-unix",
        RUNTIME / "lib/wine/x86_64-windows",
        RUNTIME / "lib/wine/i386-windows",
    )
    proxy_path = output / "trace-loader/libMoltenVK.dylib"
    moltenvk_path = output / "mvk/libMoltenVK.1.dylib"
    dyld_paths = (proxy_path.parent, moltenvk_path.parent, runtime_paths[0])
    fallback_paths = (*dyld_paths, RUNTIME / "lib")
    env.update({
        "WINEPREFIX": str(prefix), "WINEARCH": "win64",
        "WINELOADER": str(WINE), "WINESERVER": str(WINESERVER),
        "WINEDATADIR": str(RUNTIME / "share"),
        "WINEDLLPATH": ":".join(str(path) for path in runtime_paths),
        "WINEDEBUG": "+err,+pid,+tid,+loaddll",
        # PrefixLease checks these manifest bytes against the certified
        # runtime identity. Wine's retained win32u path directly dlopens
        # libMoltenVK.dylib; this manifest is not used to steer that route.
        "VK_DRIVER_FILES": str(ICD),
        "VK_ICD_FILENAMES": str(ICD),
        "DYLD_LIBRARY_PATH": ":".join(str(path) for path in dyld_paths),
        "DYLD_FALLBACK_LIBRARY_PATH": ":".join(str(path) for path in fallback_paths),
        "DYLD_PRINT_LIBRARIES": "1",
        "FG_B_ORIGINAL_MOLTENVK_LIBRARY": str(moltenvk_path),
        "FG_B_ORIGINAL_MOLTENVK_SHA256": sha256(MOLTENVK_ORIGINAL),
        "FG_B_TRACE_PROXY_PATH": str(proxy_path),
        "FG_B_TRACE_PROXY_SHA256": sha256(PROXY),
        "FG_B_EXPECTED_MOLTENVK_VERSION": EXPECTED_MOLTENVK_VERSION,
        "FG_B_EXPECTED_WINE_PATH": str(WINE_HOST_EXECUTABLE),
        "FG_B_EXPECTED_WINE_SHA256": sha256(WINE_HOST_EXECUTABLE),
        "FG_B_WINE_LAUNCHER_PATH": str(WINE),
        "FG_B_WINE_LAUNCHER_SHA256": sha256(WINE),
        "FG_B_WINESERVER_PATH": str(WINESERVER),
        "FG_B_WINESERVER_SHA256": sha256(WINESERVER),
        "FG_B_WINEVULKAN_DLL_PATH": str(prefix / "drive_c/windows/system32/winevulkan.dll"),
        "FG_B_WINEVULKAN_DLL_SHA256": sha256(WINEVULKAN_DLL),
        "FG_B_WINEVULKAN_SO_PATH": str(WINEVULKAN_SO),
        "FG_B_WINEVULKAN_SO_SHA256": sha256(WINEVULKAN_SO),
        "FG_B_HOST_LOADER_PATH": str(WINE_HOST_LOADER),
        "FG_B_HOST_LOADER_SHA256": sha256(WINE_HOST_LOADER),
        "FG_B_WINEMAC_PATH": str(WINEMAC_SO),
        "FG_B_WINEMAC_SHA256": sha256(WINEMAC_SO),
        "FG_B_TRACE_DIRECTORY": str(host_trace_path),
        "FG_B_TRACE_DIRECTORY_DEVICE": str(host_trace_path.stat().st_dev),
        "FG_B_TRACE_DIRECTORY_INODE": str(host_trace_path.stat().st_ino),
        "FG_B_EXPERIMENT_TOKEN": experiment_token,
        "FG_APP_TRACE_PATH": windows_path(app_trace_path),
        "FG_RUN_SECONDS": str(duration_seconds),
        "FG_PUBLIC_WINDOW_START_GATE_REQUIRED": "1" if start_gate_signal_path is not None else "0",
    })
    if experiment_id is not None:
        env["FG_RUN_ID"] = experiment_id
    if start_gate_signal_path is not None:
        env["FG_PUBLIC_WINDOW_START_GATE_PATH"] = windows_path(start_gate_signal_path)
    storage_policy._validate_wine_child_environment(env)
    return env


def copy_verified(source: Path, target: Path, expected_sha: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise FileExistsError(f"refusing to replace an existing run input: {target}")
    shutil.copyfile(source, target, follow_symlinks=False)
    if sha256(target) != expected_sha:
        raise RuntimeError(f"copied input failed exact SHA-256 verification: {target}")


def capture_window(output_path: Path, target_pid: int | None = None) -> dict[str, Any]:
    swift = f'''import AppKit
let wanted = {json.dumps(WINDOW_TITLE)}
let targetPID = {target_pid or 0}
if let rows = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID) as? [[String: Any]] {{
  let matches = rows.filter {{ row in
    (row[kCGWindowName as String] as? String) == wanted
      && (targetPID == 0 || (row[kCGWindowOwnerPID as String] as? NSNumber)?.intValue == targetPID)
      && ((row[kCGWindowLayer as String] as? NSNumber)?.intValue ?? -1) == 0
      && ((row[kCGWindowAlpha as String] as? NSNumber)?.doubleValue ?? 0) > 0.95
  }}
  if let match = matches.first,
     let number = match[kCGWindowNumber as String] as? NSNumber {{
    print(number.intValue)
  }}
}}
'''
    capture_started_ns = time.monotonic_ns()
    listing = subprocess.run(["/usr/bin/swift", "-e", swift], text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             timeout=30, check=False)
    raw_ids = [line.strip() for line in listing.stdout.splitlines() if line.strip().isdigit()]
    if listing.returncode != 0 or not raw_ids:
        return {"status": "FAIL", "reason": "visible Wine app window was not discoverable",
                "swift_returncode": listing.returncode,
                "swift_stderr_tail": listing.stderr[-1000:]}
    window_id = raw_ids[0]
    captured = subprocess.run(["/usr/sbin/screencapture", "-x", "-l", window_id,
                                str(output_path)], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=20, check=False)
    if captured.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        return {"status": "FAIL", "reason": "target-window screenshot failed",
                "window_id": window_id, "returncode": captured.returncode,
                "stderr_tail": captured.stderr[-1000:]}
    return {"status": "PASS", "window_id": int(window_id),
            "monotonic_capture_started_ns": capture_started_ns,
            "monotonic_capture_finished_ns": time.monotonic_ns(),
            "focus_mutated": False,
            "path": str(output_path), "size_bytes": output_path.stat().st_size,
            "sha256": sha256(output_path)}


def classify_screenshot_samples(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Reject blank window warm-up captures; compare two accepted frames at finalization."""
    if len(samples) not in {1, 2}:
        return {"status": "FAIL", "reason": "one readiness sample or two final samples are required"}
    if any(row.get("sampled_pixels", 0) <= 0
           or row.get("nonblack_ratio", 0) < 0.10
           or row.get("distinct_quantized_colors", 0) < 2 for row in samples):
        return {"status": "FAIL", "reason": "smoke window was black or lacked spatial color content",
                "samples": samples}
    if len(samples) == 1:
        return {"status": "PASS", "reason": None, "content_visible": True, "samples": samples}
    means = [row["mean_rgb"] for row in samples]
    frame_change = sum(abs(float(means[0][i]) - float(means[1][i])) for i in range(3))
    return {"status": "PASS" if frame_change >= 0.03 else "FAIL",
            "reason": None if frame_change >= 0.03 else "separated smoke screenshots did not change visibly",
            "mean_rgb_l1_delta": frame_change, "samples": samples}


def inspect_screenshot_content(screenshots: list[dict[str, Any]], output: Path) -> dict[str, Any]:
    if len(screenshots) not in {1, 2}:
        return {"status": "FAIL", "reason": "one readiness sample or two final samples are required"}
    paths = [str(Path(row["path"]).resolve(strict=True)) for row in screenshots]
    swift = f'''import AppKit
import Foundation
let paths = {json.dumps(paths)}
var results: [[String: Any]] = []
for path in paths {{
  guard let data = try? Data(contentsOf: URL(fileURLWithPath: path)),
        let image = NSBitmapImageRep(data: data) else {{ fatalError("image decode failed") }}
  let width = image.pixelsWide
  let height = image.pixelsHigh
  let stepX = max(1, width / 128)
  let stepY = max(1, height / 128)
  var sum = [Double](repeating: 0, count: 3)
  var sampled = 0
  var nonblack = 0
  var colors = Set<String>()
  for y in stride(from: 0, to: height, by: stepY) {{
    for x in stride(from: 0, to: width, by: stepX) {{
      guard let color = image.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB) else {{ continue }}
      let rgb = [color.redComponent, color.greenComponent, color.blueComponent]
      for i in 0..<3 {{ sum[i] += rgb[i] }}
      if rgb.max()! > 0.04 {{ nonblack += 1 }}
      colors.insert(rgb.map {{ String(Int(($0 * 31).rounded())) }}.joined(separator: ","))
      sampled += 1
    }}
  }}
  results.append(["path": path, "width": width, "height": height,
                  "sampled_pixels": sampled,
                  "nonblack_ratio": sampled == 0 ? 0 : Double(nonblack) / Double(sampled),
                  "mean_rgb": sampled == 0 ? [0, 0, 0] : sum.map {{ $0 / Double(sampled) }},
                  "distinct_quantized_colors": colors.count])
}}
let encoded = try JSONSerialization.data(withJSONObject: results, options: [.sortedKeys])
print(String(data: encoded, encoding: .utf8)!)
'''
    completed = subprocess.run(["/usr/bin/swift", "-e", swift], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=90, check=False)
    if completed.returncode != 0:
        return {"status": "FAIL", "reason": "native screenshot content analysis failed",
                "returncode": completed.returncode, "stderr_tail": completed.stderr[-1000:]}
    try:
        samples = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        return {"status": "FAIL", "reason": f"screenshot content analysis returned invalid data: {error}"}
    if not isinstance(samples, list):
        return {"status": "FAIL", "reason": "screenshot content analysis returned a non-list result"}
    result = classify_screenshot_samples(samples)
    if len(samples) == 2:
        (output / "screenshot-content-check.json").write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def wait_short(process: subprocess.Popen[Any], seconds: int) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return False
        time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    return process.poll() is None


def phase_command(mode: str, baseline_index: int | None = None) -> tuple[int, str]:
    if mode == "smoke":
        return 30, "smoke"
    if mode == "sample" and baseline_index in range(1, 8):
        return 60, f"br7-sample-{baseline_index:02d}"
    if mode == "state" and baseline_index in range(1, 9):
        return 80, f"br8-state-{baseline_index:02d}"
    if mode == "calibration" and baseline_index == 5:
        return 90, "br9r-calibration"
    if mode == "confirm" and baseline_index == 1:
        return 300, "br7-confirm-01"
    if mode == "baseline" and baseline_index in (1, 2, 3):
        return 300, f"baseline-{baseline_index:02d}"
    raise ValueError("usage: runner_b.py smoke | baseline 1|2|3")


def run_one(mode: str, baseline_index: int | None = None,
            smoke_authorization_id: str | None = None) -> dict[str, Any]:
    observer_paths = observer_paths_from_environment(
        required=mode in {"smoke", "calibration"})
    duration_seconds, phase = phase_command(mode, baseline_index)
    prior_smoke: dict[str, Any] | None = None
    prior_smoke_path: Path | None = None
    if mode in {"baseline", "sample", "confirm", "state", "calibration"}:
        if smoke_authorization_id is None:
            raise RuntimeError("baseline mode requires a matching authenticated prior smoke ID")
        prior_smoke, prior_smoke_path = verify_smoke_authorization(smoke_authorization_id)
    elif smoke_authorization_id is not None:
        raise RuntimeError("smoke mode cannot consume a prior smoke authorization")
    nonce = uuid.uuid4().hex[:8]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    experiment_id = f"step11d3b-{phase}-{stamp}-{nonce}"
    output = HERE / "runs" / experiment_id
    prefix = storage_policy.PREFIX_ROOT / f"{experiment_id}-prefix"
    if output.exists() or output.is_symlink():
        raise FileExistsError(f"refusing to reuse B run output: {output}")

    pre = gate0_snapshot("PRE")
    if pre["gate0_status"] != "PASS":
        raise RuntimeError("Gate 0 PRE is blocked: " + "; ".join(pre["gate0_blockers"]))
    if (pre["free_bytes"] < storage_policy.FREE_MINIMUM
            or pre["scoped_fgmetal_bytes"] > storage_policy.GATE0_PROJECT_BUDGET):
        raise RuntimeError("Gate 0 PRE violates the current free-space/project thresholds")
    inputs = candidate_files()
    binding_snapshot = smoke_authorization_bindings()
    input_hashes = {name: row["sha256"] for name, row in inputs.items()}
    if binding_snapshot.get("candidate_identities") != input_hashes:
        raise RuntimeError("candidate runtime identity changed while binding the run inputs")
    if prior_smoke is not None and prior_smoke.get("bindings") != binding_snapshot:
        raise RuntimeError("baseline runtime or controls changed after prior-smoke verification")
    candidate_hashes = wine_candidate_hashes(inputs)
    if prior_smoke_path is not None:
        candidate_hashes["prior_smoke_authorization"] = sha256(prior_smoke_path)
    runner = PROCESS_RUNNER.resolve(strict=True)
    manifest = storage_policy.begin_experiment(
        experiment_id=experiment_id,
        purpose=f"STEP 11D.3-B bare Wine Vulkan {phase}: pinned Wine 11.17 + winevulkan + MoltenVK 1.4.2 + WineMetalLayer; no DXVK/D3D/interop/scheduler",
        candidate_hashes=candidate_hashes,
        expected_duration_seconds=duration_seconds,
        expected_prefix_bytes=3 * GIB,
        expected_build_bytes=0,
        estimated_persistent_bytes=(100 if mode == "smoke" else 300) * MIB,
        storage_justification=(None if mode == "smoke" else
            "Retain bounded app and asynchronous host traces, qualitative screenshots, loader log, run manifest, and PrefixLease cleanup receipt for one serialized observation run."),
        authorized_runner_paths=[runner],
        output_dir=output,
        additional={"experiment_step": "11D.3-B", "run_phase": phase,
                    "trace_contract_version": CONTROL_VERSION,
                    "requested_window": [640, 360], "requested_format": "VK_FORMAT_B8G8R8A8_UNORM",
                    "requested_present_mode": "VK_PRESENT_MODE_FIFO_KHR",
                    "requested_swapchain_images": 3, "architecture": "x86_64-under-Rosetta",
                    **wine_execution_binding(inputs)},
    )
    output.mkdir(parents=True, exist_ok=False)
    (output / "trace-loader").mkdir()
    (output / "mvk").mkdir()
    copied_proxy = output / "trace-loader/libMoltenVK.dylib"
    copy_verified(PROXY, copied_proxy, inputs["trace_proxy"]["sha256"])
    copied_mvk = output / "mvk/libMoltenVK.1.dylib"
    copy_verified(MOLTENVK_ORIGINAL, copied_mvk, inputs["moltenvk_original"]["sha256"])
    copied_mvk_alias = output / "mvk/libMoltenVK.dylib"
    copy_verified(MOLTENVK_ORIGINAL, copied_mvk_alias, inputs["moltenvk_original"]["sha256"])
    app_trace = output / "app-trace.jsonl"
    host_trace_directory = output / "host-traces"
    host_trace_directory.mkdir(mode=0o700)
    host_trace = None
    trace_artifacts = None
    trace_index = None
    runtime_log = output / "runtime.log"
    env = clean_environment(prefix, output, app_trace, host_trace_directory, duration_seconds,
                            experiment_token=sha256(Path(manifest["manifest_path"]))[:32],
                            experiment_id=experiment_id,
                            start_gate_signal_path=(observer_paths["start_signal"]
                                if mode == "smoke" and observer_paths is not None else None))
    if mode == "smoke" and observer_paths is None:
        raise RuntimeError("authorization smoke requires the lifecycle observer's start-gate signal path")
    manifest_record = {
        "schema_version": 1, "experiment_id": experiment_id, "phase": phase,
        "duration_seconds": duration_seconds, "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "gate0_pre": pre, "storage_manifest_path": manifest["manifest_path"],
        "candidate_identities": inputs,
        **wine_execution_binding(inputs),
        "wine_version": "wine-11.17 (WineForge 0.6.0.5)",
        "moltenvk_version": "MoltenVK v1.4.2, commit db66022459ffb663aa2b50f6b018bc2e124f5edf",
        "runtime_root": str(RUNTIME), "moltenvk_original_path": str(MOLTENVK_ORIGINAL),
        "moltenvk_icd_path": str(ICD), "winevulkan_dll_path": str(WINEVULKAN_DLL),
        "winevulkan_so_path": str(WINEVULKAN_SO),
        "wine_host_loader_path": str(WINE_HOST_LOADER),
        "wine_host_loader_role": "win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib; no Khronos loader",
        "winemac_so_path": str(WINEMAC_SO),
        "prefix_template": str(storage_policy.PREFIX_TEMPLATE),
        "prefix_path": str(prefix), "prefix_template_runtime_identity":
            json.loads((storage_policy.PREFIX_TEMPLATE / "FGMETAL_PREFIX_TEMPLATE.json").read_text())["runtime_identity"],
        "app_path": str(APP), "app_trace_path_host": str(app_trace),
        "app_trace_path_wine": env["FG_APP_TRACE_PATH"], "host_trace_directory": str(host_trace_directory),
        "trace_proxy_selected_path": str(copied_proxy),
        "moltenvk_selected_path_candidates": [str(copied_mvk), str(copied_mvk_alias)],
        "command": [str(WINE), env["FG_APP_TRACE_PATH"]],
        "environment": env,
        "control_binding_snapshot": binding_snapshot,
        "requested": {"architecture": "x86_64 under Rosetta", "windowed": True,
                      "window_client_px": [640, 360], "format": "BGRA8 UNORM",
                      "present_mode": "FIFO", "swapchain_images": 3},
        "presentation_behavior_modified": False,
        "br7_instrumentation_difference": "Removed BR6 one-off focus/front-ordering and unset drawableSize mutation; retained Wine/MoltenVK/FIFO configuration unchanged",
        "visual_sample_clock": "Python monotonic_ns, Darwin mach_absolute_time; bounded start/end brackets, qualitative only",
        "app_is_renderer_owned_clear_color_only": True,
        "excluded_components": ["DXVK", "D3D11", "D3D9", "DXGI", "frame generation",
                                "Vulkan-to-Metal interop provider", "FGMetalBridge",
                                "internal generated-frame scheduler", "RIFE", "neural model"],
    }
    (output / "run-manifest.json").write_text(json.dumps(manifest_record, indent=2, sort_keys=True) + "\n")

    process_returncode: int | None = None
    timed_out = False
    exception_text: str | None = None
    screenshots: list[dict[str, Any]] = []
    screenshot_capture_attempts: list[dict[str, Any]] = []
    screenshot_attempt_index = 0
    runtime_elapsed = 0.0
    cleanup_receipt: dict[str, Any] | None = None
    validation: dict[str, Any] | None = None
    loader_evidence: dict[str, Any] | None = None
    execution_result: dict[str, Any] | None = None
    execution_result_error: str | None = None
    start_gate_signal: dict[str, Any] | None = None
    start_gate_signal_evidence: dict[str, Any] = (
        {"status": "NOT_REQUIRED", "errors": []} if mode != "smoke"
        else {"status": "FAIL", "errors": ["public-window start-gate signal was not validated"]})
    start_gate_order_evidence: dict[str, Any] = (
        {"status": "NOT_REQUIRED", "errors": []} if mode != "smoke"
        else {"status": "FAIL", "errors": ["first acquire ordering was not validated"]})
    start_gate_validation: dict[str, Any] = (
        {"status": "NOT_REQUIRED", "errors": []} if mode != "smoke"
        else {"status": "FAIL", "errors": ["public-window start-gate validation is incomplete"]})
    prefix_winevulkan_path = prefix / "drive_c/windows/system32/winevulkan.dll"
    app_windows_path = env["FG_APP_TRACE_PATH"].casefold()
    process_context_path = output / "process-context.json"
    execution_result_path = output / "execution-result.json"
    command = [str(WINE), str(APP)]
    (output / "run-manifest.json").write_text(
        json.dumps({**manifest_record, "command": command,
                    "prefix_winevulkan_path": str(prefix_winevulkan_path),
                    "prior_smoke_authorization": (
                        {"path": str(prior_smoke_path.resolve()),
                         "sha256": sha256(prior_smoke_path),
                         "experiment_id": prior_smoke.get("experiment_id")}
                        if prior_smoke is not None and prior_smoke_path is not None else None)},
                   indent=2, sort_keys=True) + "\n")
    process_context = {
        "experiment_id": experiment_id, "prefix": str(prefix), "output": str(output),
        "wine_server": str(WINESERVER), "environment": env, "command": command,
        "duration_seconds": duration_seconds, "runtime_log": str(runtime_log),
        "execution_result": str(execution_result_path),
        "target_title": WINDOW_TITLE,
        "expected_pe_identity": {
            "path": inputs["app"]["path"], "sha256": inputs["app"]["sha256"],
        },
        "expected_target_process_identity": {
            "role": "wine_host_image",
            "path": inputs["wine_host_image"]["path"],
            "sha256": inputs["wine_host_image"]["sha256"],
        },
        "target_trace_directory": str(host_trace_directory.resolve()),
        "observer_handoff_path": str(observer_paths["handoff"]) if observer_paths else None,
        "public_window_start_gate_required": mode == "smoke",
        "public_window_start_gate_signal_path": (
            str(observer_paths["start_signal"]) if mode == "smoke" and observer_paths else None),
        "start_gate_safety_watchdog_seconds": 900 if mode == "smoke" else None,
    }
    process_context_path.write_text(json.dumps(process_context, indent=2, sort_keys=True) + "\n")
    process = None
    runner_returncode = None
    controller_log = output / "process-runner.log"
    try:
        with controller_log.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [sys.executable, str(PROCESS_RUNNER), str(process_context_path)],
                cwd=str(REPO), stdout=log, stderr=subprocess.STDOUT, text=True)
            if mode == "smoke":
                smoke_deadline = time.monotonic() + duration_seconds + 1050
                last_capture_at = 0.0
                next_capture_at = time.monotonic()
                while process.poll() is None and len(screenshots) < 2 and time.monotonic() < smoke_deadline:
                    now = time.monotonic()
                    host_trace_bytes = max(
                        [p.stat().st_size for p in host_trace_directory.glob("host-trace.*.jsonl")],
                        default=0,
                    )
                    rendering_active = (
                        len(screenshots) > 0
                        or (app_trace.is_file() and app_trace.stat().st_size > 100_000)
                        or host_trace_bytes > 20_000
                    )
                    if runtime_log.is_file() and rendering_active and now >= next_capture_at:
                        screenshot_attempt_index += 1
                        screenshot_path = output / f"smoke-window-attempt-{screenshot_attempt_index:03d}.png"
                        observation = capture_window(screenshot_path)
                        if observation.get("status") == "PASS":
                            observation["captured_after_first_success_seconds"] = (
                                0.0 if not screenshots else now - last_capture_at
                            )
                            content_check = inspect_screenshot_content([observation], output)
                            screenshot_capture_attempts.append({
                                "capture": observation,
                                "content_check": content_check,
                                "accepted": content_check.get("status") == "PASS",
                            })
                            if (content_check.get("status") == "PASS"
                                    and (not screenshots or now - last_capture_at >= 10.0)):
                                screenshots.append(observation)
                                last_capture_at = now
                                next_capture_at = now + 10.0
                            elif content_check.get("status") != "PASS":
                                next_capture_at = now + 4.0
                            else:
                                next_capture_at = last_capture_at + 10.0
                        else:
                            screenshot_capture_attempts.append({
                                "capture": observation,
                                "content_check": None,
                                "accepted": False,
                            })
                            next_capture_at = now + 4.0
                    try:
                        process.wait(timeout=min(0.5, max(0.05, next_capture_at - time.monotonic())))
                    except subprocess.TimeoutExpired:
                        pass
            if mode in {"sample", "confirm"}:
                sample_origin_ns = None
                pending = [5, 15, 30, 45, 59] if mode == "sample" else [5, 15, 30, 45, 60]
                deadline = time.monotonic() + duration_seconds + 300
                while process.poll() is None and pending and time.monotonic() < deadline:
                    target_pid = None
                    if sample_origin_ns is None:
                        for target_trace in host_trace_directory.glob("host-trace.*.jsonl"):
                            with target_trace.open() as host_stream:
                                for line in host_stream:
                                    try:
                                        row = json.loads(line)
                                    except json.JSONDecodeError:
                                        continue
                                    if (row.get("event") == "vkAcquireNextImageKHR_begin"
                                            and row.get("origin") == "MVK_BOUNDARY"
                                            and row.get("trace_role") == "target"
                                            and row.get("host_pid") == row.get("target_unix_pid")):
                                        sample_origin_ns = row.get("ts_mach_ns")
                                        sample_target_pid = row.get("target_unix_pid")
                                        break
                            if sample_origin_ns is not None:
                                break
                    if sample_origin_ns is not None:
                        target_pid = sample_target_pid
                    if sample_origin_ns is not None and time.monotonic_ns() >= sample_origin_ns + pending[0] * 1_000_000_000:
                        target_second = pending.pop(0)
                        observation = capture_window(output / f"observation-{target_second:02d}.png", target_pid)
                        observation["requested_loop_second"] = target_second
                        observation["loop_start_mach_ns"] = sample_origin_ns
                        screenshots.append(observation)
                        (output / "visual-observations.json").write_text(json.dumps(screenshots, indent=2) + "\n")
                    try:
                        process.wait(timeout=0.25)
                    except subprocess.TimeoutExpired:
                        pass
            try:
                runner_returncode = process.wait(timeout=duration_seconds + 1050)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.terminate()
                try:
                    runner_returncode = process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    runner_returncode = process.wait(timeout=10)
    except BaseException as exc:
        exception_text = f"{type(exc).__name__}: {exc}"

    if observer_paths is not None:
        interim_execution_result, _interim_error = read_execution_result(execution_result_path)
        interim_pid = (interim_execution_result.get("target_unix_pid")
                       if interim_execution_result is not None else None)
        try:
            signal_observer_complete(
                observer_paths, experiment_id=experiment_id,
                target_pid=interim_pid if isinstance(interim_pid, int) else None,
                runner_returncode=runner_returncode,
                experiment_token=(env["FG_B_EXPERIMENT_TOKEN"]
                                  if isinstance(interim_pid, int) else None),
                target_process_exit_monotonic_ns=(
                    interim_execution_result.get("target_process_exit_monotonic_ns")
                    if interim_execution_result is not None else None),
            )
        except BaseException as exc:
            exception_text = exception_text or (
                f"public window observer completion signal failed: {type(exc).__name__}: {exc}")
        public_window_observer_result, observer_wait_error = load_final_observer_result(observer_paths)
        if observer_wait_error:
            exception_text = exception_text or observer_wait_error
    else:
        public_window_observer_result = None

    execution_result, execution_result_error = read_execution_result(execution_result_path)
    if execution_result is not None:
        process_returncode = execution_result.get("process_returncode")
        timed_out = timed_out or bool(execution_result.get("timed_out"))
        try:
            runtime_elapsed = float(execution_result.get("runtime_process_elapsed_seconds", 0.0))
        except (TypeError, ValueError) as exc:
            exception_text = exception_text or f"execution result elapsed time is invalid: {exc!r}"
    elif runner_returncode not in (None, 0):
        detail = f": {execution_result_error}" if execution_result_error else ""
        exception_text = (exception_text or
                          f"PrefixLease process runner exited {runner_returncode} without execution result{detail}")
    elif execution_result_error:
        exception_text = exception_text or execution_result_error

    if mode == "smoke":
        if observer_paths is None or execution_result is None:
            start_gate_signal_evidence = {
                "status": "FAIL",
                "errors": ["start-gate evidence lacks observer paths or the actual Wine target PID"],
            }
        else:
            try:
                start_gate_signal, start_gate_signal_evidence = validate_start_gate_signal(
                    observer_paths["start_signal"],
                    experiment_id=experiment_id,
                    target_pid=execution_result.get("target_unix_pid"),
                    token=env["FG_B_EXPERIMENT_TOKEN"],
                )
            except BaseException as exc:
                start_gate_signal_evidence = {
                    "status": "FAIL",
                    "errors": [f"start-gate evidence validation failed: {type(exc).__name__}: {exc}"],
                }

    cleanup_path = output / "prefix-cleanup.json"
    if cleanup_path.is_file() and not cleanup_path.is_symlink():
        try:
            cleanup_receipt = json.loads(cleanup_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            exception_text = exception_text or f"cleanup receipt parse failed: {exc!r}"
    if not runtime_log.is_file():
        validation_error = "Wine runtime log was not created"
        exception_text = exception_text or validation_error
        validation = {"is_valid": False, "error": validation_error}
    elif execution_result is None:
        validation_error = execution_result_error or "execution result is unavailable"
        exception_text = exception_text or validation_error
        validation = {"is_valid": False, "error": validation_error}
    else:
        try:
            if (execution_result.get("prefix_winevulkan_path") != str(prefix_winevulkan_path)
                    or execution_result.get("prefix_winevulkan_sha256") != inputs["winevulkan_dll"]["sha256"]):
                raise ValidationError("captured materialized DLL identity differs from the exact leased prefix/runtime")
            prefix_winevulkan_identity = {
                "path": execution_result["prefix_winevulkan_path"],
                "sha256": execution_result["prefix_winevulkan_sha256"],
            }
            trace_index = build_trace_index(output, execution_result["target_unix_pid"],
                                            env["FG_B_EXPERIMENT_TOKEN"])
            # The index selects by PrefixLease's exec PID, never by arrival order.
            host_trace = Path(trace_index["target_trace_path"])
            host_events = TraceValidator().load_file(host_trace)
            app_events = TraceValidator().load_file(app_trace)
            if mode == "smoke":
                start_gate_order_evidence = validate_start_gate_order(
                    start_gate_signal,
                    experiment_id=experiment_id,
                    target_pid=execution_result.get("target_unix_pid"),
                    token=env["FG_B_EXPERIMENT_TOKEN"],
                    signal_path=observer_paths["start_signal"] if observer_paths else Path(""),
                    app_events=app_events,
                    host_events=host_events,
                )
                gate_errors = [
                    *start_gate_signal_evidence.get("errors", []),
                    *start_gate_order_evidence.get("errors", []),
                ]
                start_gate_validation = {
                    "status": "PASS" if not gate_errors else "FAIL",
                    "errors": gate_errors,
                    "signal": start_gate_signal_evidence,
                    "ordering": start_gate_order_evidence,
                }
            startup_identities = {
                "wine": {"path": inputs["wine_host_image"]["path"], "sha256": inputs["wine_host_image"]["sha256"]},
                "wine_launcher": {"path": inputs["wine_launcher"]["path"],
                                  "sha256": inputs["wine_launcher"]["sha256"]},
                "wineserver": {"path": inputs["wineserver"]["path"], "sha256": inputs["wineserver"]["sha256"]},
                "winevulkan_dll": {"path": prefix_winevulkan_identity["path"],
                                   "sha256": prefix_winevulkan_identity["sha256"]},
                "winevulkan_so": {"path": inputs["winevulkan_so"]["path"],
                                   "sha256": inputs["winevulkan_so"]["sha256"]},
                "wine_host_loader": {"path": inputs["wine_host_loader"]["path"],
                                     "sha256": inputs["wine_host_loader"]["sha256"]},
                "winemac_so": {"path": inputs["winemac_so"]["path"],
                               "sha256": inputs["winemac_so"]["sha256"]},
                "trace_proxy": {"path": str(copied_proxy.resolve()),
                                "sha256": inputs["trace_proxy"]["sha256"]},
                "moltenvk": {"path": str(copied_mvk.resolve()),
                             "sha256": inputs["moltenvk_original"]["sha256"]},
            }
            validate_target_association(app_events, trace_index)
            app_configs = [row for row in app_events if row.get("origin") == "APP_CONFIG"]
            if len(app_configs) != 1:
                raise ValidationError("expected exactly one target APP_CONFIG")
            loader_evidence = parse_loader_evidence(
                runtime_log, output, prefix_winevulkan_identity,
                target_unix_pid=execution_result["target_unix_pid"],
                app_windows_pid=app_configs[0]["process_id"], host_trace_path=host_trace,
                expected_module_identities=startup_identities, expected_app_path=APP,
                expected_app_sha256=inputs["app"]["sha256"],
                app_windows_path=windows_path(APP),
                expected_moltenvk_version=EXPECTED_MOLTENVK_VERSION,
                expected_runtime_winevulkan_path=WINEVULKAN_DLL,
                app_config=app_configs[0])
            trace_artifacts = {role: {"path": str(path.resolve()), "sha256": sha256(path)}
                               for role, path in (("target_host_trace", host_trace),
                                                  ("trace_index", output / "trace-index.json"),
                                                  ("app_trace", app_trace))}
            if mode == "smoke" and observer_paths is not None and start_gate_signal is not None:
                gate_state_path = Path(start_gate_signal["start_state_snapshot_path"])
                gate_streak_path = Path(start_gate_signal["start_streak_snapshot_path"])
                trace_artifacts.update({
                    "public_window_start_signal": {
                        "path": str(observer_paths["start_signal"].resolve()),
                        "sha256": sha256(observer_paths["start_signal"]),
                    },
                    "public_window_start_state": {
                        "path": str(gate_state_path.resolve()), "sha256": sha256(gate_state_path),
                    },
                    "public_window_start_streak": {
                        "path": str(gate_streak_path.resolve()), "sha256": sha256(gate_streak_path),
                    },
                })
            verify_trace_artifact_bindings(output, trace_artifacts,
                                           authorization_smoke=mode == "smoke")
            validator = BareWineTraceValidator(
                expected_original_vulkan_path=str(copied_mvk),
                expected_moltenvk_paths=[str(copied_mvk)],
                expected_startup_identities=startup_identities,
                expected_app_path=str(APP), expected_app_sha256=inputs["app"]["sha256"],
                expected_moltenvk_version=EXPECTED_MOLTENVK_VERSION,
                expected_duration_seconds=duration_seconds,
                expected_target_unix_pid=execution_result["target_unix_pid"],
                expected_experiment_token=env["FG_B_EXPERIMENT_TOKEN"])
            validation = validator.validate(app_events, host_events, loader_evidence=loader_evidence)
            app_config = validator._one(validator._rows(app_events, "APP_CONFIG", "app_configuration"),
                                        "app configuration")
            if str(app_config.get("trace_path", "")).casefold() != app_windows_path:
                raise ValidationError("app trace path in the PE does not match the exact per-run output")
            validation["app_config_trace_path_matches"] = True
        except BaseException as exc:
            exception_text = exception_text or f"trace validation failed: {type(exc).__name__}: {exc}"
            validation = {"is_valid": False, "error": str(exc)}
            index_path = output / "trace-index.json"
            if index_path.is_file() and not index_path.is_symlink():
                trace_index = json.loads(index_path.read_text())
            pid = execution_result.get("target_unix_pid") if execution_result is not None else None
            if isinstance(pid, int) and pid > 0:
                candidate_target = host_trace_directory / f"host-trace.{pid}.jsonl"
                if candidate_target.is_file() and not candidate_target.is_symlink():
                    host_trace = candidate_target

    try:
        binding_stable = smoke_authorization_bindings() == binding_snapshot
    except BaseException as exc:
        binding_stable = False
        exception_text = exception_text or f"runtime/control identity recheck failed: {type(exc).__name__}: {exc}"
    post = gate0_snapshot("POST")
    storage_manifest_sha256 = sha256(Path(manifest["manifest_path"]))
    cleanup_good = automatic_post_matches(cleanup_receipt, experiment_id, prefix,
                                          storage_manifest_sha256)
    screenshot_content = (inspect_screenshot_content(screenshots, output)
                          if mode == "smoke" else None)
    screenshots_good = (mode != "smoke" or
                        len(screenshots) == 2
                        and all(row.get("status") == "PASS" for row in screenshots)
                        and screenshot_content is not None
                        and screenshot_content.get("status") == "PASS")
    app_good = process_returncode == 0 and not timed_out
    post_good = (post["gate0_status"] == "PASS"
                 and post["free_bytes"] >= storage_policy.FREE_MINIMUM
                 and post["scoped_fgmetal_bytes"] <= storage_policy.GATE0_PROJECT_BUDGET)
    trace_good = bool(validation and validation.get("is_valid") is True
                      and validation.get("quantitative_ready") is True)
    smoke_signature = (summarize_smoke_signature(validation)
                       if mode == "smoke" else None)
    smoke_signature_good = (mode != "smoke"
                            or isinstance(smoke_signature, dict)
                            and smoke_signature.get("healthy_refresh") is True)
    observer_validation = (
        validate_smoke_observer(
            public_window_observer_result,
            experiment_id=experiment_id,
            target_pid=(execution_result.get("target_unix_pid")
                        if execution_result is not None else None),
            runner_pid=process.pid if process is not None else None,
            completion_runner_pid=os.getpid(),
            token=env["FG_B_EXPERIMENT_TOKEN"],
            expected_process_identity={
                "role": "wine_host_image", "path": inputs["wine_host_image"]["path"],
                "sha256": inputs["wine_host_image"]["sha256"],
            },
            trace_index=trace_index,
            execution_result=execution_result,
            target_run_end_monotonic_ns=(
                max((frame.get("presented_callback_mach_ns", 0)
                     for frame in validation.get("frames", [])
                     if isinstance(frame, dict)
                     and type(frame.get("presented_callback_mach_ns")) is int
                     and frame.get("presented_callback_mach_ns", 0) > 0),
                    default=None)
                if isinstance(validation, dict) else None),
        )
        if mode == "smoke" else
        ({"status": "PASS", "errors": []} if observer_paths is None else {
            "status": "PASS" if public_window_observer_result
            and public_window_observer_result.get("error") is None
            and public_window_observer_result.get("watchdogTriggered") is False
            else "FAIL",
            "errors": ([] if public_window_observer_result
                       and public_window_observer_result.get("error") is None
                       and public_window_observer_result.get("watchdogTriggered") is False
                       else ["lifecycle observer did not finish cleanly"]),
        })
    )
    public_observer_binding: dict[str, Any] | None = None
    if observer_paths is not None:
        observer_result_file = observer_paths["result"]
        public_observer_binding = {
            "status": observer_validation.get("status"),
            "path": str(observer_result_file.resolve()) if observer_result_file.is_file() else str(observer_result_file),
            "sha256": sha256(observer_result_file)
            if observer_result_file.is_file() and not observer_result_file.is_symlink() else None,
            "run_id": experiment_id,
            "target_unix_pid": (
                public_window_observer_result.get("targetPID")
                if public_window_observer_result else None),
            "canonical_uncovered_state": (
                public_window_observer_result.get("canonicalUncoveredState", {})
                if public_window_observer_result else {}),
            "start_gate_validation": start_gate_validation,
            "start_gate_signal_path": (
                start_gate_signal_evidence.get("path") if mode == "smoke" else None),
            "start_gate_signal_sha256": (
                start_gate_signal_evidence.get("sha256") if mode == "smoke" else None),
            "validation": observer_validation,
        }
    observer_good = mode != "smoke" or observer_validation.get("status") == "PASS"
    start_gate_good = mode != "smoke" or start_gate_validation.get("status") == "PASS"
    valid = (app_good and trace_good and cleanup_good and post_good
             and observer_good
             and start_gate_good
             and screenshots_good and binding_stable and smoke_signature_good
             and exception_text is None)
    result = {
        "schema_version": 1, "experiment_id": experiment_id, "phase": phase,
        "status": "PASS" if valid else "FAIL", "is_valid": valid,
        "duration_seconds_requested": duration_seconds,
        "process_returncode": process_returncode, "timed_out": timed_out,
        "runtime_process_elapsed_seconds": runtime_elapsed,
        "exception": exception_text, "execution_result_error": execution_result_error,
        "gate0_pre": pre, "gate0_post": post,
        "storage_manifest_sha256": storage_manifest_sha256,
        "prefix_cleanup": cleanup_receipt, "prefix_cleanup_pass": cleanup_good,
        "screenshot_checks": screenshots, "screenshot_content_check": screenshot_content,
        "screenshot_check_pass": screenshots_good,
        "screenshot_capture_attempts": screenshot_capture_attempts,
        "loader_evidence": loader_evidence, "validation": validation,
        "smoke_signature": smoke_signature,
        "start_gate_signal": start_gate_signal,
        "start_gate_signal_validation": start_gate_signal_evidence,
        "start_gate_ordering": start_gate_order_evidence,
        "start_gate_validation": start_gate_validation,
        "public_window_observer": public_observer_binding,
        "control_binding_snapshot": binding_snapshot,
        "control_binding_stable_through_run": binding_stable,
        "runtime_log_path": str(runtime_log), "process_runner_log_path": str(controller_log),
        "process_runner_returncode": runner_returncode,
        "app_trace_path": str(app_trace) if app_trace.is_file() else None,
        "host_trace_path": str(host_trace) if host_trace is not None and host_trace.is_file() else None,
        "trace_index": trace_index, "trace_artifacts": trace_artifacts,
        "host_trace_directory": str(host_trace_directory),
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    result_path = output / "run-result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    authorization_path = None
    if mode == "smoke" and valid and trace_good and validation is not None:
        authorization = {
            "schema_version": 1, "experiment_id": experiment_id, "phase": "smoke",
            "status": "PASS", "is_valid": True,
            "quantitative_ready": validation.get("quantitative_ready") is True,
            "smoke_signature": smoke_signature,
            "prefixlease_automatic_post": "PASS" if cleanup_good else "FAIL",
            "gate0_post": "PASS" if post_good else "FAIL",
            "run_result_path": str(result_path.resolve()),
            "run_result_sha256": sha256(result_path),
            "bindings": binding_snapshot, "trace_artifacts": trace_artifacts,
            "public_window_observer": public_observer_binding,
            "start_gate_signal": start_gate_signal,
            "start_gate_validation": start_gate_validation,
            "authenticated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        if authorization["quantitative_ready"] and cleanup_good and post_good:
            sealed_authorization = storage_policy._seal_storage_evidence(
                authorization, SMOKE_AUTH_FIELD)
            authorization_path = output / "smoke-authorization.json"
            authorization_path.write_text(
                json.dumps(sealed_authorization, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
    print(json.dumps({"status": result["status"], "experiment_id": experiment_id,
                      "report": str(result_path), "gate0_pre": pre["gate0_status"],
                      "gate0_post": post["gate0_status"],
                      "trace_validation": trace_good,
                      "smoke_signature": smoke_signature,
                      "presented_fps": validation.get("presented_fps") if validation else None,
                      "next_drawable_max_ns": (validation.get("next_drawable", {}).get("maximum_ns")
                                                if validation else None),
                      "smoke_authorization": (str(authorization_path)
                                              if authorization_path else None),
                      "exception": exception_text}, indent=2, sort_keys=True))
    return result


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "smoke":
        result = run_one("smoke")
    elif (len(sys.argv) == 4 and sys.argv[1] in {"baseline", "sample", "confirm", "state"}):
        result = run_one(sys.argv[1], int(sys.argv[2]), sys.argv[3])
    elif (len(sys.argv) == 4 and sys.argv[1] == "calibration"):
        result = run_one("calibration", int(sys.argv[2]), sys.argv[3])
    else:
        print("usage: runner_b.py smoke | baseline 1|2|3 <passing-smoke-id> | calibration 5 <passing-smoke-id>", file=sys.stderr)
        return 2
    return 0 if result.get("is_valid") else 1


if __name__ == "__main__":
    raise SystemExit(main())
