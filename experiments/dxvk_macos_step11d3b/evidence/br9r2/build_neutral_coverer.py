#!/usr/bin/env python3
"""Build the BR9R2 public-AppKit coverer under a projected storage admission."""
from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]
EXPERIMENT = REPO / "experiments/dxvk_macos_step11d3b"
EVIDENCE = EXPERIMENT / "evidence/br9r2"
SOURCE = EXPERIMENT / "neutral_coverer.swift"
CONTROLLER = EVIDENCE / "coverer_sequence.py"
REPORT = EXPERIMENT / "STEP-11D3-BR9R2-REPORT.md"
EXPECTED_SOURCE_SHA256 = "8618bfa139df18d7da6c4db46518b8cf690c8c42bb568aa8470d118c8c6c0b82"
EXPECTED_CONTROLLER_SHA256 = "f650bb05d5d152fa37d97c2c4d246eaa71a3facc41dcf8c16aa2f6037a0b57f5"
EXPECTED_BINARY_MAX_BYTES = 8 * 1024 * 1024
DECLARED_COMPILER_PEAK_BYTES = 128 * 1024 * 1024
DECLARED_ARTIFACT_BYTES = 8 * 1024 * 1024
MIB = 1024 * 1024

sys.path.insert(0, str(REPO / "experiments/storage"))
import build_retention  # noqa: E402
import storage_policy  # noqa: E402


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_small(command: list[str], env: dict[str, str], *, timeout: int = 30) -> str:
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            env=env, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {command[0]}: {result.stdout[-2000:]}")
    return result.stdout.strip()


def run_compiler(command: list[str], env: dict[str, str], log_path: Path) -> None:
    with log_path.open("xb") as log:
        process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, env=env,
                                   start_new_session=True)
        try:
            result = process.wait(timeout=300)
        except subprocess.TimeoutExpired as error:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except OSError:
                    pass
                process.wait()
            raise RuntimeError("Swift compiler exceeded the five-minute bound") from error
        log.flush()
        os.fsync(log.fileno())
    if result != 0:
        tail = log_path.read_text(errors="replace")[-4000:]
        raise RuntimeError(f"Swift compiler failed ({result}):\n{tail}")


def main() -> int:
    source_sha = sha256_file(SOURCE)
    controller_sha = sha256_file(CONTROLLER)
    if source_sha != EXPECTED_SOURCE_SHA256:
        raise RuntimeError(f"prepared coverer source changed: {source_sha}")
    if controller_sha != EXPECTED_CONTROLLER_SHA256:
        raise RuntimeError(f"prepared controller changed: {controller_sha}")
    if not REPORT.is_file() or REPORT.is_symlink():
        raise RuntimeError("BR9R2 report is missing or unsafe")

    swiftc = Path("/usr/bin/swiftc").resolve(strict=True)
    swift = Path("/usr/bin/swift").resolve(strict=True)
    xcrun = Path("/usr/bin/xcrun").resolve(strict=True)
    identifier = f"step11d3b-br9r2-coverer-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:8]}"
    output = EVIDENCE / f"coverer-build-{identifier}"
    binary = output / "neutral-coverer"
    tmp = output / ".tmp"
    env = dict(os.environ)
    env.update({"TMPDIR": str(tmp), "TMP": str(tmp), "TEMP": str(tmp), "HOME": str(tmp / "home"),
                "CLANG_MODULE_CACHE_PATH": str(tmp / "clang-module-cache"),
                "SWIFT_MODULECACHE_PATH": str(tmp / "swift-module-cache")})

    # The single declared allocation conservatively includes the compiler's
    # 128 MiB temporary peak and the 8 MiB retained artifact. begin_experiment
    # then adds its canonical 10%/256 MiB uncertainty margin to that peak.
    manifest = storage_policy.begin_experiment(
        experiment_id=identifier,
        purpose="STEP 11D.3-BR9R2 immutable neutral AppKit coverer build",
        candidate_hashes={
            "neutral_coverer_source": source_sha,
            "coverer_sequence_controller": controller_sha,
            "br9r2_report": sha256_file(REPORT),
            "storage_policy": sha256_file(REPO / "experiments/storage/storage_policy.py"),
        },
        expected_duration_seconds=360,
        expected_prefix_bytes=0,
        expected_build_bytes=0,
        estimated_persistent_bytes=DECLARED_COMPILER_PEAK_BYTES + DECLARED_ARTIFACT_BYTES,
        storage_justification=(
            "Conservatively reserve the one-file Swift compile peak (128 MiB temporary plus 8 MiB artifact) "
            "as a single admitted allocation; only the exact artifact and compact provenance are retained."),
        authorized_runner_paths=[Path(__file__).resolve()],
        output_dir=output,
        additional={
            "admission_kind": "small-single-file-compiler-peak",
            "declared_temporary_peak_bytes": DECLARED_COMPILER_PEAK_BYTES,
            "declared_artifact_bytes": DECLARED_ARTIFACT_BYTES,
            "artifact_retention": "canonical SHA-256 artifact store",
            "graphics_experiment_run": False,
        },
    )
    if manifest.get("decision") != "ALLOW":
        raise RuntimeError("coverer build admission was denied: " + "; ".join(manifest.get("blockers", [])))

    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    tmp.mkdir(mode=0o700)
    (tmp / "home").mkdir(mode=0o700)
    swift_version = run_small([str(swiftc), "--version"], env)
    sdk_path = run_small([str(xcrun), "--sdk", "macosx", "--show-sdk-path"], env)
    sdk_version = run_small([str(xcrun), "--sdk", "macosx", "--show-sdk-version"], env)
    window_levels = run_small(
        [str(swift), "-e", "import AppKit; print(NSWindow.Level.modalPanel.rawValue); print(NSWindow.Level.floating.rawValue)"],
        env,
    ).splitlines()
    if len(window_levels) != 2:
        raise RuntimeError("AppKit did not report the modal-panel and floating window levels")

    compiler_command = [
        str(swiftc), "-O", "-parse-as-library", "-target", "arm64-apple-macosx12.0",
        "-module-cache-path", str(tmp / "swift-module-cache"),
        "-framework", "AppKit", "-framework", "CoreGraphics", "-framework", "Foundation",
        "-o", str(binary), str(SOURCE),
    ]
    run_compiler(compiler_command, env, output / "swiftc.log")
    if not binary.is_file() or binary.is_symlink():
        raise RuntimeError("Swift compiler did not produce a regular coverer executable")
    size_bytes = binary.stat().st_size
    if size_bytes > EXPECTED_BINARY_MAX_BYTES:
        raise RuntimeError(f"coverer exceeded the 8 MiB artifact bound: {size_bytes}")
    architecture = run_small(["/usr/bin/lipo", "-archs", str(binary)], env)
    binary_sha = sha256_file(binary)
    artifact = build_retention._artifactize(binary)
    if artifact.get("sha256") != binary_sha or not build_retention._verify_artifact(artifact):
        raise RuntimeError("canonical artifact store did not verify the exact compiled coverer")
    canonical_binary = Path(artifact["canonical_artifact_path"])
    if canonical_binary.is_symlink() or sha256_file(canonical_binary) != binary_sha:
        raise RuntimeError("canonical coverer artifact identity differs from compiler output")

    # The controller rejects symlinked executables, so pass the canonical
    # regular file directly. Remove only build temporaries after artifactization.
    binary.unlink()
    for child in list(tmp.iterdir()):
        if child.is_dir() and not child.is_symlink():
            import shutil
            shutil.rmtree(child)
        elif child.exists() or child.is_symlink():
            child.unlink()
    tmp.rmdir()

    free_after = storage_policy.disk_free_bytes(REPO)
    usage_after = storage_policy.measure_project_usage()
    scoped_after = storage_policy.conservative_project_usage_bytes(usage_after)
    post_admission = storage_policy.projected_storage_admission(
        current_free_bytes=free_after, current_scoped_bytes=scoped_after, peak_additional_bytes=0)
    if post_admission.get("blockers"):
        raise RuntimeError("post-build storage state crossed policy: " + "; ".join(post_admission["blockers"]))

    record: dict[str, Any] = {
        "schema_version": 1,
        "status": "PASS",
        "experiment_id": identifier,
        "manifest_path": manifest["manifest_path"],
        "manifest_sha256": sha256_file(Path(manifest["manifest_path"])),
        "admission": {
            "decision": manifest["decision"],
            "estimated_persistent_bytes": manifest["estimated_persistent_bytes"],
            "estimated_uncertainty_margin_bytes": manifest["estimated_uncertainty_margin_bytes"],
            "estimated_projected_new_disk_bytes": manifest["estimated_projected_new_disk_bytes"],
            "projected_free_bytes": manifest["projected_free_bytes"],
            "projected_scoped_project_bytes": manifest["projected_scoped_project_bytes"],
        },
        "source_path": str(SOURCE),
        "source_sha256": source_sha,
        "binary_path": str(canonical_binary),
        "binary_sha256": binary_sha,
        "binary_size_bytes": size_bytes,
        "architecture": architecture,
        "toolchain": {"swiftc": str(swiftc), "swift_version": swift_version,
                      "macos_sdk": sdk_version, "sdk_path": sdk_path},
        "appkit": {"level_name": "NSModalPanelWindowLevel / NSWindow.Level.modalPanel",
                   "modal_panel_level_value": int(window_levels[0]),
                   "native_a_floating_level_value": int(window_levels[1]),
                   "opacity_configuration": {"is_opaque": True, "alpha_value": 1.0,
                                               "black_content_view": True},
                   "animated": False, "metal": False, "vulkan": False,
                   "screen_capture": False, "target_modification": False},
        "compiler_command": compiler_command,
        "compiler_log_path": str(output / "swiftc.log"),
        "artifact_manifest_path": artifact["artifact_manifest_path"],
        "artifact_manifest_sha256": sha256_file(Path(artifact["artifact_manifest_path"])),
        "post_build_storage": {"free_bytes": free_after, "scoped_bytes": scoped_after,
                               "projected_admission_blockers": post_admission["blockers"]},
        "completed_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    record_path = output / "coverer-build-provenance.json"
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(record, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        result = main()
    except Exception as error:
        print(f"COVERER_BUILD_FAILURE={error!r}", file=sys.stderr)
        raise
    raise SystemExit(result)
