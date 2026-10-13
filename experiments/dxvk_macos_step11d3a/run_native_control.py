#!/usr/bin/env python3
"""Run the exact CAS-bound native MoltenVK control under NativeRunLease."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
STORAGE_DIR = REPO / "experiments/storage"
RUNS_ROOT = EXPERIMENT_DIR / "evidence/native-runs"
EXPECTED_BUILD_ID = "step11d3a-mvk-x86_64-20261008-12"
ARCHITECTURE = "x86_64"
PROCESS_TRANSLATED = 1
MAX_LOG_BYTES = 512 * 1024 * 1024
MIB = 1024 * 1024
_ALLOWED_CHILD_DYLD_KEYS = frozenset({"DYLD_LIBRARY_PATH", "DYLD_PRINT_LIBRARIES"})
_DYLD_ENVIRONMENT_PREFIXES = ("DYLD_", "__XPC_DYLD_")
_ALLOWED_EVIDENCE_ENVIRONMENT_KEYS = frozenset({
    "PATH", "LANG", "HOME", "TMPDIR", "DYLD_LIBRARY_PATH", "DYLD_PRINT_LIBRARIES",
})

sys.path.insert(0, str(STORAGE_DIR))
import build_retention  # noqa: E402
import storage_policy  # noqa: E402
from trace_validator import TraceValidator, ValidationError  # noqa: E402


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(storage_policy._read_file_nofollow(path))


def write_observer_handoff(path: Path, value: dict[str, Any]) -> str:
    expected_parent = (EXPERIMENT_DIR / "../dxvk_macos_step11d3b/evidence/br9r2").resolve()
    require(path.name == "target-window-observer-input.json"
            and path.parent.resolve(strict=True) == expected_parent,
            "public window observer handoff path is outside the fixed BR9R2 evidence directory")
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        | getattr(os, "O_CLOEXEC", 0), 0o600)
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        raise
    return sha256_file(path)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def child_environment_evidence(sealed_environment: dict[str, str], *,
                               ambient_environment: dict[str, str] | None = None) -> dict[str, Any]:
    """Record only the explicit child environment and ambient loader key names.

    Ambient values can contain credentials or user paths, so the evidence keeps
    key names only. The child values are safe to retain because NativeRunLease
    builds this mapping from the runner's explicit allowlist and scratch paths.
    """
    ambient = os.environ if ambient_environment is None else ambient_environment
    require(set(sealed_environment).issubset(_ALLOWED_EVIDENCE_ENVIRONMENT_KEYS),
            "runner refuses to persist a child environment outside its explicit evidence allowlist")
    sealed = {key: sealed_environment[key] for key in sorted(sealed_environment)}
    ambient_dyld_keys = sorted(
        key for key in ambient
        if key.upper().startswith(_DYLD_ENVIRONMENT_PREFIXES))
    child_dyld_keys = sorted(
        key for key in sealed
        if key.upper().startswith(_DYLD_ENVIRONMENT_PREFIXES))
    unexpected_child_dyld_keys = sorted(set(child_dyld_keys) - _ALLOWED_CHILD_DYLD_KEYS)
    ambient_injection_keys_in_child = sorted(
        (set(ambient_dyld_keys) & set(child_dyld_keys)) - _ALLOWED_CHILD_DYLD_KEYS)
    canonical = json.dumps(sealed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return {
        "inheritance_policy": "explicit NativeRunLease environment; ambient values are not passed",
        "sealed_child_environment": sealed,
        "sealed_child_environment_sha256": sha256_bytes(canonical),
        "ambient_dyld_key_names_in_supervisor": ambient_dyld_keys,
        "sealed_child_dyld_key_names": child_dyld_keys,
        "ambient_dyld_injection_key_names_in_child": ambient_injection_keys_in_child,
        "unexpected_child_dyld_key_names": unexpected_child_dyld_keys,
    }


def verify_child_environment(sealed_environment: dict[str, str], *,
                             expected_dylib_directory: Path,
                             ambient_environment: dict[str, str] | None = None) -> dict[str, Any]:
    """Require an exact lease-built environment before recording or launching."""
    evidence = child_environment_evidence(
        sealed_environment, ambient_environment=ambient_environment)
    require(not evidence["unexpected_child_dyld_key_names"]
            and not evidence["ambient_dyld_injection_key_names_in_child"],
            "NativeRunLease environment contains an unexpected DYLD injection variable")
    require(set(evidence["sealed_child_dyld_key_names"]) == _ALLOWED_CHILD_DYLD_KEYS,
            "NativeRunLease environment is missing its exact synthesized DYLD controls")
    require(set(evidence["sealed_child_environment"]) == _ALLOWED_EVIDENCE_ENVIRONMENT_KEYS,
            "NativeRunLease child environment differs from the exact native runner allowlist")
    expected_search_paths = list(dict.fromkeys([
        str(expected_dylib_directory.resolve(strict=True)), "/usr/lib",
    ]))
    actual_search_paths = sealed_environment["DYLD_LIBRARY_PATH"].split(":")
    require(actual_search_paths == expected_search_paths,
            "NativeRunLease DYLD_LIBRARY_PATH differs from the exact candidate and system library roots")
    return evidence


def dyld_moltenvk_images(stderr_text: str, expected_dylib: Path) -> dict[str, Any]:
    """Return actual dyld-reported MoltenVK image paths, rejecting alternatives."""
    expected = expected_dylib.resolve(strict=True)
    loaded: list[dict[str, str]] = []
    for line_number, line in enumerate(stderr_text.splitlines(), 1):
        if "moltenvk" not in line.casefold():
            continue
        match = re.match(r"^\s*dyld(?:\[\d+\])?:\s*(.+?)\s*$", line, re.IGNORECASE)
        require(match is not None,
                f"stderr line {line_number} mentions MoltenVK but is not a DYLD_PRINT_LIBRARIES record")
        image = match.group(1)
        image_uuid = None
        uuid_match = re.match(r"^<([0-9a-fA-F-]{36})>\s+", image)
        if uuid_match is not None:
            image_uuid = uuid_match.group(1).upper()
            image = image[uuid_match.end():]
        # DYLD_PRINT_LIBRARIES may append an address annotation after the image.
        image = re.sub(r"\s+\(offset\s+0x[0-9a-fA-F]+\)\s*$", "", image)
        require(image.startswith("/"),
                f"stderr line {line_number} did not report an absolute MoltenVK image path")
        require(image == str(expected_dylib),
                f"dyld reported a different MoltenVK path than the manifest: expected={expected_dylib} actual={image}")
        image_path = Path(image)
        require("moltenvk" in image_path.name.casefold(),
                f"stderr line {line_number} reported a non-MoltenVK image for a MoltenVK match")
        try:
            resolved = image_path.resolve(strict=True)
        except OSError as error:
            raise RuntimeError(f"stderr line {line_number} names an unavailable MoltenVK image: {image}") from error
        require(resolved == expected,
                f"dyld loaded an alternate MoltenVK image: expected={expected} actual={resolved}")
        row = {"reported_path": image, "resolved_path": str(resolved)}
        if image_uuid is not None:
            row["reported_uuid"] = image_uuid
        loaded.append(row)
    require(bool(loaded), "DYLD_PRINT_LIBRARIES did not report a MoltenVK image")
    unique_paths = sorted({row["resolved_path"] for row in loaded})
    require(unique_paths == [str(expected)],
            "DYLD_PRINT_LIBRARIES did not resolve to exactly the manifest MoltenVK image")
    return {
        "image_count": len(loaded),
        "unique_resolved_paths": unique_paths,
        "images": loaded,
    }


def validate_loader_probe(stdout_text: str, stderr_text: str, *, dylib_path: Path) -> dict[str, Any]:
    """Require the live probe and dyld logs to identify the exact CAS image."""
    match = re.search(
        r"LOADER_PROBE=PASS symbol=vkCreateInstance path=(\S+) compiled_arch=(arm64|x86_64) process_translated=(-?\d+)",
        stdout_text,
    )
    require(match is not None, "native loader probe did not report the actual MoltenVK image")
    require(match.group(1) == str(dylib_path),
            f"native loader reported a different MoltenVK path than the manifest: {match.group(1)}")
    try:
        loaded_path = Path(match.group(1)).resolve(strict=True)
    except OSError as error:
        raise RuntimeError("native loader probe path does not resolve to an existing image") from error
    expected_path = dylib_path.resolve(strict=True)
    require(loaded_path == expected_path,
            f"native loader resolved a different MoltenVK path than the signed artifact: {loaded_path}")
    require(match.group(2) == ARCHITECTURE and int(match.group(3)) == PROCESS_TRANSLATED,
            "native loader probe architecture/translation differs from the integrated 11D.2 path")
    dyld_images = dyld_moltenvk_images(stderr_text, dylib_path)
    return {
        "status": "PASS",
        "loaded_moltenvk_path": str(loaded_path),
        "compiled_architecture": match.group(2),
        "process_translated": int(match.group(3)),
        "dyld_printed_moltenvk_images": dyld_images,
    }


def validate_graphics_trace_events(events: list[dict[str, Any]], *, requested_seconds: int,
                                   dylib_path: Path) -> dict[str, Any]:
    """Return the compact offline validation result that gates graphics PASS."""
    validator = TraceValidator(
        expected_architecture=ARCHITECTURE,
        expected_process_translated=PROCESS_TRANSLATED,
        expected_library_path=str(dylib_path),
    )
    report = validator.validate(events)
    errors = list(report.errors)
    is_valid = report.is_valid
    if report.requested_run_seconds != requested_seconds:
        errors.append(
            "trace requested duration differs from runner mode: "
            f"expected={requested_seconds}s trace={report.requested_run_seconds}s")
        is_valid = False
    quantitative_ready = is_valid and report.quantitative_ready
    return {
        "status": "PASS" if is_valid and quantitative_ready else "FAIL",
        "is_valid": is_valid,
        "quantitative_ready": quantitative_ready,
        "expected_requested_seconds": requested_seconds,
        "requested_run_seconds": report.requested_run_seconds,
        "actual_run_duration_ns": report.actual_run_duration_ns,
        "frame_loop_elapsed_ns": report.frame_loop_elapsed_ns,
        "total_events": report.total_events,
        "app_events_count": report.app_events_count,
        "moltenvk_events_count": report.moltenvk_events_count,
        "frame_count": report.frame_count,
        "complete_frame_count": report.complete_frame_count,
        "displayed_frame_count": report.displayed_frame_count,
        "dropped_frame_count": report.dropped_frame_count,
        "buffer_dropped_count": report.buffer_dropped_count,
        "logger_producer_gate_rejected_count": report.logger_producer_gate_rejected_count,
        "logger_serialization_failure_count": report.logger_serialization_failure_count,
        "logger_write_failure_count": report.logger_write_failure_count,
        "presentation_timing_extension_available": report.presentation_timing_extension_available,
        "presentation_completion_records": report.presentation_completion_records,
        "unobserved_presentation_timing_records": report.unobserved_presentation_timing_records,
        "execution_architecture": report.execution_architecture,
        "configured_layer_state": report.configured_layer_state,
        "swapchain_configuration": report.swapchain_configuration,
        "join_edge_count": len(report.join_edges),
        "errors": errors,
        "warnings": report.warnings,
    }


def validate_graphics_trace_file(trace_path: Path, *, requested_seconds: int,
                                 dylib_path: Path) -> dict[str, Any]:
    try:
        events = TraceValidator().load_file(trace_path)
    except (OSError, ValidationError, UnicodeError) as error:
        return {
            "status": "FAIL",
            "is_valid": False,
            "quantitative_ready": False,
            "expected_requested_seconds": requested_seconds,
            "errors": [f"offline trace parsing failed: {error}"],
        }
    return validate_graphics_trace_events(
        events, requested_seconds=requested_seconds, dylib_path=dylib_path)


def verified_artifacts(build_id: str) -> tuple[dict[str, Any], str, dict[str, dict[str, Any]]]:
    require(build_id == EXPECTED_BUILD_ID, "native runner accepts only the completed primary x86_64 build")
    manifest, manifest_sha = build_retention._verify_build_manifest(build_id)
    identity = manifest.get("build_identity", {})
    request = identity.get("native_moltenvk_request", {})
    require(manifest.get("kind") == "moltenvk"
            and manifest.get("configuration_key_provenance") == "pre-build frozen request",
            "native run requires a verified fresh native MoltenVK build manifest")
    require(request.get("architectures") == [ARCHITECTURE]
            and request.get("pristine_git_commit") == "db66022459ffb663aa2b50f6b018bc2e124f5edf"
            and request.get("patch_sha256") == json.loads(
                (EXPERIMENT_DIR / "prepared-source-identity.json").read_text())["patch_sha256"],
            "build manifest does not bind the pinned x86_64 MoltenVK instrumentation request")
    prepared = identity.get("prepared_source_verification")
    require(isinstance(prepared, dict) and prepared.get("status") == "verified_prepared_source"
            and prepared.get("root_relative_path") == "MoltenVKSource"
            and prepared.get("pristine_git_commit") == request.get("pristine_git_commit")
            and prepared.get("pristine_git_tree") == request.get("pristine_git_tree")
            and prepared.get("prepared_source_tree_sha256")
            and prepared.get("prepared_source_file_count", 0) > 0,
            "build manifest lacks post-build prepared-source verification")
    by_role = {row.get("role"): row for row in manifest.get("artifacts", [])}
    require("native_control" in by_role and "moltenvk_dylib" in by_role,
            "verified native build is missing a required output role")
    for role in ("native_control", "moltenvk_dylib"):
        row = by_role[role]
        require(build_retention._verify_artifact(row), f"CAS output verification failed for {role}")
        require(row.get("architectures") == [ARCHITECTURE],
                f"native output architecture is not {ARCHITECTURE}: {role}")
    return manifest, manifest_sha, by_role


def run_parameters(mode: str) -> tuple[int, int, int]:
    """Return frame-loop seconds, lease timeout, and retained-output estimate."""
    parameters = {
        "probe": (0, 60, 32 * MIB),
        "smoke": (30, 120, 512 * MIB),
        "occlusion": (80, 180, 512 * MIB),
        "calibration": (90, 180, 512 * MIB),
        "baseline": (300, 420, 512 * MIB),
    }
    try:
        return parameters[mode]
    except KeyError as error:
        raise ValueError(f"unsupported run mode: {mode}") from error


def run_once(mode: str, experiment_id: str, build_id: str) -> dict[str, Any]:
    seconds, expected_duration, persistent_estimate = run_parameters(mode)

    build_manifest, build_manifest_sha, artifacts = verified_artifacts(build_id)
    executable_row = artifacts["native_control"]
    dylib_row = artifacts["moltenvk_dylib"]
    executable = Path(executable_row["canonical_artifact_path"])
    dylib = Path(dylib_row["canonical_artifact_path"])
    executable_sha = executable_row["sha256"]
    dylib_sha = dylib_row["sha256"]
    output_dir = RUNS_ROOT / experiment_id
    prefix_before = storage_policy.prefix_state_fingerprint()
    preserved_before = [str(path) for path in storage_policy.preserved_prefixes()]
    uncertified_before = [str(path) for path in storage_policy.uncertified_prefixes()]
    require(not uncertified_before, "uncertified prefix cleanup is pending")

    manifest = storage_policy.begin_experiment(
        experiment_id=experiment_id,
        purpose=f"STEP 11D.3-A native x86_64 MoltenVK {mode} run",
        candidate_hashes={
            "native_control_sha256": executable_sha,
            "instrumented_moltenvk_sha256": dylib_sha,
            "native_build_manifest_sha256": build_manifest_sha,
            "instrumentation_patch_sha256": build_manifest["build_identity"]["source_identity"]["patch_sha256"],
            "native_architecture": ARCHITECTURE,
            "expected_process_translated": str(PROCESS_TRANSLATED),
        },
        expected_duration_seconds=expected_duration,
        expected_prefix_bytes=0,
        expected_build_bytes=0,
        estimated_persistent_bytes=persistent_estimate,
        storage_justification=(
            "Retain only the native run receipt, compact analysis, and a complete compressed trace; "
            "the uncompressed trace is removed after offline validation."
        ),
        authorized_runner_paths=[Path(__file__).resolve()],
        native_architecture=ARCHITECTURE,
        output_dir=output_dir,
        additional={
            "native_build_id": build_id,
            "native_build_manifest_id": build_manifest["build_manifest_id"],
            "native_build_manifest_sha256": build_manifest_sha,
            "native_control_sha256": executable_sha,
            "instrumented_moltenvk_sha256": dylib_sha,
            "process_translation_expected": PROCESS_TRANSLATED,
            "run_mode": mode,
            "requested_seconds": seconds,
        },
    )
    require(manifest["decision"] == "ALLOW",
            "Gate 0 refused native run admission: " + "; ".join(manifest.get("blockers", [])))

    lease = storage_policy.NativeRunLease(
        experiment_id=experiment_id,
        executable_path=executable,
        output_dir=output_dir,
        candidate_dylibs={"moltenvk": dylib},
        timeout_seconds=expected_duration,
        expected_executable_sha256=executable_sha,
        expected_dylib_hashes={"moltenvk": dylib_sha},
        expected_architecture=ARCHITECTURE,
        max_log_bytes=MAX_LOG_BYTES,
        env={
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
        },
    )
    stdout_path = output_dir / "stdout.log"
    stderr_path = output_dir / "stderr.log"
    trace_path = output_dir / "trace.jsonl"
    observer_handoff_path_value = os.environ.get("FG_PUBLIC_WINDOW_OBSERVER_HANDOFF_PATH")
    observer_env_keys = {
        "FG_PUBLIC_WINDOW_OBSERVER_HANDOFF_PATH",
        "FG_PUBLIC_WINDOW_OBSERVER_COMPLETION_PATH",
        "FG_PUBLIC_WINDOW_OBSERVER_RESULT_PATH",
    }
    observer_env_present = observer_env_keys.intersection(os.environ)
    require(not observer_env_present or observer_env_present == observer_env_keys,
            "public window observer paths must be supplied together")
    observer_handoff_path = (Path(observer_handoff_path_value)
                             if observer_handoff_path_value else None)
    observer_token: str | None = None
    observer_handoff_sha256: str | None = None
    started = time.monotonic()
    with lease:
        active_marker = storage_policy.assert_native_lease(
            experiment_id, executable, runner_path=Path(__file__).resolve())
        require(active_marker.get("native_architecture") == ARCHITECTURE,
                "active NativeRunLease marker has the wrong architecture")
        scratch = lease.scratch_directory
        sealed_environment = lease.set_env({
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
        })
        environment_evidence = verify_child_environment(
            sealed_environment, expected_dylib_directory=dylib.parent)
        command = ([str(executable), "--loader-probe"] if mode == "probe" else
                   [str(executable), "--seconds", str(seconds), "--trace", str(trace_path)])
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = lease.start_native_process(command, stdout=stdout, stderr=stderr, start_new_session=True)
            if observer_handoff_path is not None:
                runner_monotonic_ns = time.monotonic_ns()
                observer_token = secrets.token_hex(16)
                identity = {"path": str(executable.resolve(strict=True)), "sha256": executable_sha}
                handoff = {
                    "schema_version": 2,
                    "target_kind": "native",
                    "window_telemetry_expected": False,
                    "run_id": experiment_id,
                    "experiment_token": observer_token,
                    "target_unix_pid": process.pid,
                    "target_title": "STEP 11D.3-A Native MoltenVK Drawable Baseline",
                    "expected_native_identity": identity,
                    "expected_target_process_identity": identity,
                    "target_trace_path": str(trace_path.resolve()),
                    "runner_pid": os.getpid(),
                    "runner_monotonic_ns": runner_monotonic_ns,
                    "runner_started_at_utc": datetime.now(timezone.utc).isoformat(),
                    "runner_source_path": str(Path(__file__).resolve()),
                    "runner_source_sha256": sha256_file(Path(__file__).resolve()),
                }
                observer_handoff_sha256 = write_observer_handoff(observer_handoff_path, handoff)
            return_code = lease.wait_process(process, timeout_seconds=expected_duration,
                                             poll_interval_seconds=0.5)
        elapsed_seconds = time.monotonic() - started
        require(return_code == 0, f"native control returned {return_code}")
        require(lease._bound_executable_identity is not None
                and lease._bound_executable_identity[0] == executable_sha,
                "NativeRunLease executable binding differs from the build manifest")
        require(lease._bound_dylibs.get("moltenvk", {}).get("sha256") == dylib_sha,
                "NativeRunLease dylib binding differs from the build manifest")

    receipt_path = output_dir / "native-run-cleanup.json"
    receipt = json.loads(storage_policy._read_file_nofollow(receipt_path))
    require(storage_policy._verify_storage_evidence(receipt, "cleanup_receipt_hmac_sha256"),
            "NativeRunLease cleanup receipt signature is invalid")
    require(receipt.get("status") == "PASS" and receipt.get("storage_hygiene_status") == "PASS"
            and receipt.get("temporary_removed") is True,
            "NativeRunLease POST cleanup did not pass")
    require(not any(name.startswith(".run_scratch_") for name in os.listdir(output_dir)),
            "NativeRunLease scratch directory remains after POST")
    prefix_after = storage_policy.prefix_state_fingerprint()
    preserved_after = [str(path) for path in storage_policy.preserved_prefixes()]
    uncertified_after = [str(path) for path in storage_policy.uncertified_prefixes()]
    require(prefix_after["sha256"] == prefix_before["sha256"]
            and preserved_after == preserved_before
            and uncertified_after == uncertified_before == [],
            "native run changed the Wine prefix inventory")

    loader_probe: dict[str, Any] | None = None
    trace_validation: dict[str, Any] | None = None
    if mode == "probe":
        stdout_text = stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
        loader_probe = validate_loader_probe(stdout_text, stderr_text, dylib_path=dylib)
    else:
        if not trace_path.is_file() or trace_path.stat().st_size <= 0:
            trace_validation = {
                "status": "FAIL",
                "is_valid": False,
                "quantitative_ready": False,
                "expected_requested_seconds": seconds,
                "errors": ["native graphics process produced no trace"],
            }
        else:
            trace_validation = validate_graphics_trace_file(
                trace_path, requested_seconds=seconds, dylib_path=dylib)

    gate0_blockers = storage_policy._gate0_blockers()
    mode_validation_pass = (loader_probe is not None and loader_probe.get("status") == "PASS"
                            if mode == "probe" else
                            trace_validation is not None
                            and trace_validation.get("status") == "PASS"
                            and trace_validation.get("quantitative_ready") is True)
    run_status = "PASS" if mode_validation_pass and not gate0_blockers else "FAIL"
    result = {
        "status": run_status,
        "mode": mode,
        "experiment_id": experiment_id,
        "experiment_manifest_sha256": sha256_file(storage_policy._experiment_manifest_path(experiment_id)),
        "native_build_id": build_id,
        "native_build_manifest_id": build_manifest["build_manifest_id"],
        "native_build_manifest_sha256": build_manifest_sha,
        "native_control_path": str(executable),
        "native_control_sha256": executable_sha,
        "native_control_architecture": ARCHITECTURE,
        "process_translated_expected": PROCESS_TRANSLATED,
        "instrumented_moltenvk_path": str(dylib),
        "instrumented_moltenvk_sha256": dylib_sha,
        "child_environment_evidence": environment_evidence,
        "return_code": return_code,
        "elapsed_seconds": elapsed_seconds,
        "stdout_path": str(stdout_path),
        "stdout_sha256": sha256_file(stdout_path),
        "stderr_path": str(stderr_path),
        "stderr_sha256": sha256_file(stderr_path),
        "trace_path": str(trace_path) if mode != "probe" else None,
        "trace_sha256": sha256_file(trace_path) if mode != "probe" else None,
        "trace_size_bytes": trace_path.stat().st_size if mode != "probe" else 0,
        "loader_probe": loader_probe,
        "trace_validation": trace_validation,
        "trace_validation_status": trace_validation.get("status") if trace_validation else None,
        "public_window_observer_handoff": ({
            "path": str(observer_handoff_path),
            "sha256": observer_handoff_sha256,
            "target_unix_pid": (json.loads(observer_handoff_path.read_text()).get("target_unix_pid")
                                if observer_handoff_sha256 else None),
            "experiment_token": observer_token,
        } if observer_handoff_path is not None else None),
        "native_run_cleanup_receipt_path": str(receipt_path),
        "native_run_cleanup_receipt_sha256": sha256_file(receipt_path),
        "native_run_cleanup_receipt": receipt,
        "gate0_post_blockers": gate0_blockers,
        "wine_prefix_state_before": prefix_before,
        "wine_prefix_state_after": prefix_after,
        "completed_at_unix": time.time(),
    }
    result_path = output_dir / "run-result.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "status": result["status"],
        "mode": mode,
        "experiment_id": experiment_id,
        "return_code": return_code,
        "elapsed_seconds": round(elapsed_seconds, 3),
        "trace_path": result["trace_path"],
        "trace_size_bytes": result["trace_size_bytes"],
        "cleanup_receipt": result["native_run_cleanup_receipt_path"],
    }, sort_keys=True))
    if run_status != "PASS":
        reasons = list(gate0_blockers)
        if trace_validation and trace_validation.get("status") != "PASS":
            reasons.extend(str(error) for error in trace_validation.get("errors", []))
        if mode == "probe" and loader_probe is None:
            reasons.append("native MoltenVK loader probe did not pass")
        raise RuntimeError("native run did not pass validation: " + "; ".join(reasons))
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("probe", "smoke", "occlusion", "calibration", "baseline"))
    parser.add_argument("experiment_id")
    parser.add_argument("--build-id", default=EXPECTED_BUILD_ID)
    args = parser.parse_args()
    try:
        run_once(args.mode, args.experiment_id, args.build_id)
        return 0
    except BaseException as error:
        print(f"NATIVE_RUN_FAILURE={error!r}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
