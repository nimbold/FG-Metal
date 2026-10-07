#!/usr/bin/env python3
"""Run the slow color source with the source-built DXVK Metal roundtrip."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO / "experiments/storage"))
from storage_policy import (  # noqa: E402
    PREFIX_TEMPLATE,
    PrefixLease,
    begin_experiment,
    assert_prefix_lease,
    open_canonical_artifact,
    current_lease,
    make_experiment_id,
    scoped_lease,
)

SOURCE_HZ = int(sys.argv[1])
DURATION = int(sys.argv[2])
RUN_NAME = sys.argv[3] if len(sys.argv) > 3 else "diag-smoke-10s"
OUT = HERE / RUN_NAME
APP_ARTIFACT_SHA256 = "f9e581029c215edc2ecad46f5c22cc1b0b0cfc8d17fbaec5f7d96667fc556169"
DXVK_ARTIFACTS = {
    "d3d11.dll": "cc09178473bef9b6af9fec5821d9d56ad089d61b9daf0af139c132b379f46297",
    "dxgi.dll": "2c90d3fc3f364a13d003c2dfe1a08ccdffe5bca9a9d6cde8408b65d6a5450f88",
}
OVERLAY_ARTIFACTS = {
    "x86_64-windows/FGMetalBridge.dll": "7a4d53fbf6e4730c93119759d521d91bbf5bfa419bde6df7d63d2ca7b0d500e3",
    "x86_64-unix/fgmetalbridge.so": "ecc9ce4c552f093a501ed1ac1186bbd086b2db4465830a07d119a6c4b49dd5d2",
}
RUNTIME = Path("/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine")
RUNTIME_MANIFEST = REPO / "experiments/storage/runtime-manifests/step11d_r_active_runtime.json"
RUNTIME_MANIFEST_SHA256 = "662a47726872910cf7e0c9f16d3d416a0183cff0c974ba47150ea916b4186a95"
ICD = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json")
ICD_SHA256 = "578ff08cd0d8734619357541771a5abc9c3470ca300030219a971a9e9dbbe466"
MOLTENVK_ARTIFACT_SHA256 = "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f"
PREFIX = Path(f"/Users/nima/Library/Caches/FGMetalStep11D-R/step11d2-{RUN_NAME}-prefix")
EXPERIMENT_MANIFEST_PATH: str | None = None


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _run() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to overwrite preserved run directory: {OUT}")
    for required in (RUNTIME / "bin/wine", ICD, RUNTIME_MANIFEST):
        if not required.is_file():
            raise SystemExit(f"missing required runtime/build input: {required}")
    if sha(RUNTIME_MANIFEST) != RUNTIME_MANIFEST_SHA256:
        raise RuntimeError("certified Wine runtime manifest changed")
    if sha(ICD) != ICD_SHA256:
        raise RuntimeError("MoltenVK ICD JSON changed")

    OUT.mkdir(parents=True)
    loader_override = OUT / "loader-override"
    loader_override.mkdir()
    pinned_moltenvk = OUT / "pinned-libMoltenVK.1.dylib"
    for target in (loader_override / "libMoltenVK.dylib", pinned_moltenvk):
        with open_canonical_artifact(MOLTENVK_ARTIFACT_SHA256) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if sha(target) != MOLTENVK_ARTIFACT_SHA256:
            raise RuntimeError(f"copied canonical artifact failed its SHA-256 check: {target}")
    overlay = OUT / "overlay"
    overlay.mkdir()
    for relative, digest in OVERLAY_ARTIFACTS.items():
        target = overlay / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with open_canonical_artifact(digest) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if sha(target) != digest:
            raise RuntimeError(f"copied canonical artifact failed its SHA-256 check: {target}")
    for filename, digest in DXVK_ARTIFACTS.items():
        target = OUT / filename
        with open_canonical_artifact(digest) as source, target.open("xb") as output:
            shutil.copyfileobj(source, output)
            output.flush()
            os.fsync(output.fileno())
        if sha(target) != digest:
            raise RuntimeError(f"copied canonical artifact failed its SHA-256 check: {target}")
    app = OUT / "slow-visual.exe"
    with open_canonical_artifact(APP_ARTIFACT_SHA256) as source, app.open("xb") as output:
        shutil.copyfileobj(source, output)
        output.flush()
        os.fsync(output.fileno())
    if sha(app) != APP_ARTIFACT_SHA256:
        raise RuntimeError(f"copied canonical app artifact failed its SHA-256 check: {app}")
    config = OUT / "dxvk.conf"
    config.write_text("dxvk.enableMetalInterop = True\n", encoding="utf-8")

    env = os.environ.copy()
    for key in list(env):
        if (key.startswith(("WINE", "DXVK_", "FG_", "MVK_", "MTL_", "VK_", "DYLD_", "LD_",
                            "__XPC_DYLD_", "PYTHON"))
                or key in {"BASH_ENV", "ENV", "GCONV_PATH", "NODE_OPTIONS", "PERL5OPT", "RUBYOPT"}):
            env.pop(key, None)
    runtime_paths = [
        RUNTIME / "lib/wine/x86_64-unix",
        RUNTIME / "lib/wine/x86_64-windows",
        RUNTIME / "lib/wine/i386-windows",
    ]
    dyld_path = ":".join((str(loader_override), str(runtime_paths[0]),
                           "/private/tmp/fgmetal-vulkan-link"))
    fallback_path = ":".join((dyld_path, str(RUNTIME / "lib"),
                                "/usr/local/lib", "/usr/lib"))
    env.update({
        "WINEPREFIX": str(PREFIX),
        "WINEARCH": "win64",
        "WINELOADER": str(RUNTIME / "bin/wine"),
        "WINESERVER": str(RUNTIME / "bin/wineserver"),
        "WINEDATADIR": str(RUNTIME / "share"),
        "WINEDLLPATH": ":".join((str(overlay), *(str(path) for path in runtime_paths))),
        "WINEDLLOVERRIDES": "d3d11,dxgi=n,b",
        "WINEDEBUG": "+err,+loaddll",
        "VK_ICD_FILENAMES": str(ICD),
        "VK_DRIVER_FILES": str(ICD),
        "DYLD_LIBRARY_PATH": dyld_path,
        "DYLD_FALLBACK_LIBRARY_PATH": fallback_path,
        "DYLD_PRINT_LIBRARIES": "1",
        "DXVK_CONFIG_FILE": str(config),
        "DXVK_LOG_LEVEL": "info",
        "DXVK_LOG_PATH": str(OUT),
        "DXVK_INTERNAL_WSI_TRACE": "1",
        "FG_AUDIT_ENABLE": "1",
        "FG_AUDIT_RUN_SECONDS": str(DURATION),
        "FG_AUDIT_LIFECYCLE_TEST": "0",
        "FG_AUDIT_SKIP_RESIZE": "1",
        "FG_AUDIT_WAITABLE": "1",
        "FG_AUDIT_SOURCE_HZ": str(SOURCE_HZ),
        "FG_AUDIT_FRAMES_PER_COLOR": str(SOURCE_HZ),
        "FG_AB_BGRA": "0",
    })
    command = ["/usr/bin/arch", "-x86_64", str(RUNTIME / "bin/wine"), str(app)]
    manifest = {
        "duration_seconds": DURATION,
        "source_hz": SOURCE_HZ,
        "frames_per_color": SOURCE_HZ,
        "source_colors": ["red", "green", "blue", "white"],
        "metal_invert_expected": ["cyan", "magenta", "yellow", "black"],
        "internal_wsi_proof_override": False,
        "slot_polling_change": "submission worker snapshots and polls every eligible terminal slot per pass; bridge and poll calls serialized",
        "dxvk_enable_metal_interop": True,
        "swap_effect": "DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL",
        "dll_sha256": {
            "d3d11.dll": sha(OUT / "d3d11.dll"),
            "dxgi.dll": sha(OUT / "dxgi.dll"),
            "FGMetalBridge.dll": sha(overlay / "x86_64-windows/FGMetalBridge.dll"),
            "fgmetalbridge.so": sha(overlay / "x86_64-unix/fgmetalbridge.so"),
        },
        "app_sha256": sha(app),
        "wine_sha256": sha(RUNTIME / "bin/wine"),
        "winevulkan_pe_sha256": sha(RUNTIME / "lib/wine/x86_64-windows/winevulkan.dll"),
        "winevulkan_unix_sha256": sha(RUNTIME / "lib/wine/x86_64-unix/winevulkan.so"),
        "moltenvk_sha256": sha(pinned_moltenvk),
        "wine_runtime_manifest_sha256": sha(RUNTIME_MANIFEST),
        "moltenvk_icd_json_sha256": sha(ICD),
        "runtime": str(RUNTIME),
        "wine_dll_search_path": env["WINEDLLPATH"].split(":"),
        "dyld_library_path": dyld_path.split(":"),
        "dxvk_config": config.read_text(),
        "screen_capture": False,
        "cpu_pixel_readback": False,
        "source_build_id": "step11d2-diagnostic-c-20261006",
        "frozen_r5_source_sha256": "044d03510e5b1c57fead0aed1a31f03105c78b17dcc303d7053b4811ab270c0b",
        "diagnostic_provider_protocol_bytes": 560,
        "native_provider_build_id": "step11d2-diagnostic-c-20261006",
        "human_visual_confirmation": "pending",
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    lease = current_lease()
    lease.set_env(env)
    lease.materialize()
    assert_prefix_lease(lease.prefix, env["WINELOADER"], Path(__file__).resolve())
    with (OUT / "wineboot.log").open("w") as log:
        wineboot = lease.start_wine_process(
            ["/usr/bin/arch", "-x86_64", str(RUNTIME / "bin/wine"), "wineboot", "-u"],
            env=env, runner_path=Path(__file__).resolve(), stdout=log, stderr=subprocess.STDOUT)
        wineboot_rc = lease.wait_process(wineboot, timeout_seconds=90)
        if wineboot_rc:
            raise subprocess.CalledProcessError(wineboot_rc, wineboot.args)
    assert_prefix_lease(lease.prefix, env["WINELOADER"], Path(__file__).resolve())
    start = time.monotonic()
    with (OUT / "runtime.log").open("w") as log:
        process = lease.start_wine_process(command, env=env, runner_path=Path(__file__).resolve(),
                                           stdout=log, stderr=subprocess.STDOUT)
        try:
            returncode: int | str = lease.wait_process(
                process, timeout_seconds=DURATION + 90)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                returncode = process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                returncode = process.wait()
            returncode = f"TIMEOUT_AFTER_TERMINATION_{returncode}"

    log_text = (OUT / "runtime.log").read_text(errors="replace")
    d3d_log = OUT / "slow-visual_d3d11.log"
    dxgi_log = OUT / "slow-visual_dxgi.log"
    dxvk_text = "\n".join(path.read_text(errors="replace") for path in (d3d_log, dxgi_log)
                           if path.exists())
    source_csv = OUT / "d3d11_clear_window_app.csv"
    source_rows = 0
    if source_csv.exists():
        source_rows = sum(line.startswith("present,") for line in
                          source_csv.read_text(errors="replace").splitlines())
    result = {
        **manifest,
        "returncode": returncode,
        "elapsed_seconds": round(time.monotonic() - start, 3),
        "source_present_rows": source_rows,
        "provider_ready": "MetalInterop: provider ready" in dxvk_text,
        "native_d3d11_loaded": any("d3d11.dll" in line and ": native" in line
                                    for line in log_text.splitlines()),
        "native_dxgi_loaded": any("dxgi.dll" in line and ": native" in line
                                  for line in log_text.splitlines()),
        "metal_job_commits": dxvk_text.count("MetalInterop: committed InteropJobId="),
        "metal_vulkan_consumers": dxvk_text.count("MetalInterop: Vulkan-consumer InteropJobId="),
        "internal_metal_presents": dxvk_text.count("MetalInterop: WSI-submit InteropJobId="),
        "internal_clear_fallbacks": dxvk_text.count("pattern=synthetic-clear"),
        "moltenvk_1_4_2_reported": "MoltenVK version 1.4.2" in log_text,
        "moltenvk_loaded_paths": [line.rsplit(" ", 1)[-1] for line in log_text.splitlines()
                                  if line.startswith("dyld[") and "libMoltenVK" in line],
        "screen_capture": False,
        "cpu_pixel_readback": False,
        "human_visual_confirmation": "pending",
        "experiment_manifest_path": EXPERIMENT_MANIFEST_PATH,
        "storage_watch_events": lease.storage_watch_events,
    }
    (OUT / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if returncode == 0 else 1


def main() -> int:
    global EXPERIMENT_MANIFEST_PATH
    if OUT.exists():
        raise SystemExit(f"refusing to overwrite preserved run directory: {OUT}")
    runtime_manifest = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
    experiment_id = make_experiment_id(RUN_NAME)
    manifest = begin_experiment(
        experiment_id=experiment_id,
        purpose="Step 11D.2 diagnostic runtime with bounded disposable Wine prefix",
        candidate_hashes={
            "app.exe": APP_ARTIFACT_SHA256,
            "d3d11.dll": "cc09178473bef9b6af9fec5821d9d56ad089d61b9daf0af139c132b379f46297",
            "dxgi.dll": "2c90d3fc3f364a13d003c2dfe1a08ccdffe5bca9a9d6cde8408b65d6a5450f88",
            "FGMetalBridge.dll": "7a4d53fbf6e4730c93119759d521d91bbf5bfa419bde6df7d63d2ca7b0d500e3",
            "fgmetalbridge.so": "ecc9ce4c552f093a501ed1ac1186bbd086b2db4465830a07d119a6c4b49dd5d2",
            "wine_runtime_tree": runtime_manifest["tree_sha256"],
            "wine_runtime_manifest": RUNTIME_MANIFEST_SHA256,
            "wine_binary": sha(RUNTIME / "bin/wine"),
            "moltenvk_icd_json": ICD_SHA256,
            "moltenvk": MOLTENVK_ARTIFACT_SHA256,
        },
        expected_duration_seconds=DURATION,
        expected_prefix_bytes=3 * 1024**3,
        expected_build_bytes=0,
        estimated_persistent_bytes=200 * 1024**2,
        storage_justification=("Bounded diagnostic outputs retain only compact logs and CSV; "
                               "the prefix is removed and no trace or runtime copy is retained."),
        output_dir=OUT,
        authorized_runner_paths=[Path(__file__).resolve()],
        additional={"source_build_id": "step11d2-diagnostic-c-20261006",
                    "cleanup_policy": "clone certified template; wineserver kill/wait and remove in finally",
                    "retention_policy": "keep compact logs/manifests and canonical binary references; remove the prefix"},
    )
    experiment_id = manifest["experiment_id"]
    EXPERIMENT_MANIFEST_PATH = manifest["manifest_path"]
    lease = PrefixLease(PREFIX, OUT, experiment_id, RUNTIME / "bin/wineserver",
                        template=PREFIX_TEMPLATE)
    with scoped_lease(lease):
        return _run()


if __name__ == "__main__":
    raise SystemExit(main())
