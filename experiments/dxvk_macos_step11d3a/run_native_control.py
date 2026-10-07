#!/usr/bin/env python3
"""Run the exact CAS-bound native MoltenVK control under NativeRunLease."""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
EXPERIMENT_DIR = Path(__file__).resolve().parent
STORAGE_DIR = REPO / "experiments/storage"
RUNS_ROOT = EXPERIMENT_DIR / "evidence/native-runs"
EXPECTED_BUILD_ID = "step11d3a-mvk-x86_64-20261008-02"
ARCHITECTURE = "x86_64"
PROCESS_TRANSLATED = 1
MAX_LOG_BYTES = 512 * 1024 * 1024
MIB = 1024 * 1024

sys.path.insert(0, str(STORAGE_DIR))
import build_retention  # noqa: E402
import storage_policy  # noqa: E402
from trace_validator import TraceValidator, ValidationError  # noqa: E402


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    return sha256_bytes(storage_policy._read_file_nofollow(path))


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


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


def run_once(mode: str, experiment_id: str, build_id: str) -> dict[str, Any]:
    if mode == "probe":
        seconds = 0
        expected_duration = 60
        persistent_estimate = 32 * MIB
    elif mode == "smoke":
        seconds = 30
        expected_duration = 120
        persistent_estimate = 512 * MIB
    elif mode == "baseline":
        seconds = 300
        expected_duration = 420
        persistent_estimate = 512 * MIB
    else:
        raise ValueError(f"unsupported run mode: {mode}")

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
        allowed_dyld_keys = {"DYLD_LIBRARY_PATH", "DYLD_PRINT_LIBRARIES"}
        actual_dyld_keys = {key for key in sealed_environment if key.startswith("DYLD_")}
        require(actual_dyld_keys == allowed_dyld_keys,
                "NativeRunLease environment contains an unexpected DYLD injection variable")
        require(str(dylib.parent) in sealed_environment["DYLD_LIBRARY_PATH"].split(":"),
                "NativeRunLease loader path omits the exact candidate dylib directory")
        command = ([str(executable), "--loader-probe"] if mode == "probe" else
                   [str(executable), "--seconds", str(seconds), "--trace", str(trace_path)])
        with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
            process = lease.start_native_process(command, stdout=stdout, stderr=stderr, start_new_session=True)
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
        match = re.search(
            r"LOADER_PROBE=PASS symbol=vkCreateInstance path=(\S+) compiled_arch=(arm64|x86_64) process_translated=(-?\d+)",
            stdout_text,
        )
        require(match is not None, "native loader probe did not report the actual MoltenVK image")
        loaded_path = Path(match.group(1)).resolve(strict=True)
        require(loaded_path == dylib.resolve(strict=True),
                "native loader resolved a different MoltenVK path than the signed artifact")
        require(match.group(2) == ARCHITECTURE and int(match.group(3)) == PROCESS_TRANSLATED,
                "native loader probe architecture/translation differs from the integrated 11D.2 path")
        stderr_text = stderr_path.read_text(encoding="utf-8", errors="replace")
        require(str(dylib) in stderr_text,
                "DYLD_PRINT_LIBRARIES did not show the provenance-bound MoltenVK artifact")
        loader_probe = {
            "status": "PASS",
            "loaded_moltenvk_path": str(loaded_path),
            "compiled_architecture": match.group(2),
            "process_translated": int(match.group(3)),
            "ambient_dyld_variables_in_child": [],
        }
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
    parser.add_argument("mode", choices=("probe", "smoke", "baseline"))
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
