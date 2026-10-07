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
SOURCE_HZ = int(sys.argv[1])
DURATION = int(sys.argv[2])
OUT = HERE / "A3"
BUILD = Path("/private/tmp/fgmetal-step11d-r-current-r5-build-A-20261006")
DLLS = BUILD / "src"
APP_SOURCE = Path("/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/evidence/renderer-integration/slow-visual-metal.exe")
BRIDGE_BUILD = Path("/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/bridge-gpu-proof/build/status-v3-current-20261006")
OVERLAY_SOURCE = BRIDGE_BUILD / "overlay"
RUNTIME = Path("/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine")
ICD = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json")
LOADER_OVERRIDE = BRIDGE_BUILD / "mvk142-loader-override"
PINNED_MOLTENVK = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib")
PREFIX = Path("/Users/nima/Library/Caches/FGMetalStep11D-R/step11d1-cadence-A3-prefix")


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def main() -> int:
    if OUT.exists():
        raise SystemExit(f"refusing to overwrite preserved run directory: {OUT}")
    for required in (
        BUILD / "src/d3d11/d3d11.dll", BUILD / "src/dxgi/dxgi.dll",
        APP_SOURCE, OVERLAY_SOURCE / "x86_64-windows/FGMetalBridge.dll",
        OVERLAY_SOURCE / "x86_64-unix/fgmetalbridge.so", RUNTIME / "bin/wine", ICD,
        LOADER_OVERRIDE / "libMoltenVK.dylib", PINNED_MOLTENVK,
    ):
        if not required.is_file():
            raise SystemExit(f"missing required runtime/build input: {required}")

    OUT.mkdir(parents=True)
    overlay = OUT / "overlay"
    shutil.copytree(OVERLAY_SOURCE, overlay)
    for source, destination in ((BUILD / "src/d3d11/d3d11.dll", OUT / "d3d11.dll"),
                                (BUILD / "src/dxgi/dxgi.dll", OUT / "dxgi.dll")):
        try:
            os.link(source, destination)
        except OSError:
            shutil.copy2(source, destination)
    app = OUT / "slow-visual.exe"
    shutil.copy2(APP_SOURCE, app)
    config = OUT / "dxvk.conf"
    config.write_text("dxvk.enableMetalInterop = True\n", encoding="utf-8")

    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("WINE", "DXVK_", "FG_", "MVK_", "MTL_", "VK_", "DYLD_")):
            env.pop(key, None)
    runtime_paths = [
        RUNTIME / "lib/wine/x86_64-unix",
        RUNTIME / "lib/wine/x86_64-windows",
        RUNTIME / "lib/wine/i386-windows",
    ]
    dyld_path = ":".join((str(LOADER_OVERRIDE), str(runtime_paths[0]),
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
    command = [
        "/usr/bin/arch", "-x86_64", "/usr/bin/env",
        f"DYLD_LIBRARY_PATH={dyld_path}",
        f"DYLD_FALLBACK_LIBRARY_PATH={fallback_path}",
        "DYLD_PRINT_LIBRARIES=1", str(RUNTIME / "bin/wine"), str(app),
    ]
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
            "FGMetalBridge.dll": sha(OVERLAY_SOURCE / "x86_64-windows/FGMetalBridge.dll"),
            "fgmetalbridge.so": sha(overlay / "x86_64-unix/fgmetalbridge.so"),
        },
        "app_sha256": sha(app),
        "wine_sha256": sha(RUNTIME / "bin/wine"),
        "winevulkan_pe_sha256": sha(RUNTIME / "lib/wine/x86_64-windows/winevulkan.dll"),
        "winevulkan_unix_sha256": sha(RUNTIME / "lib/wine/x86_64-unix/winevulkan.so"),
        "moltenvk_sha256": sha(PINNED_MOLTENVK),
        "wine_runtime": subprocess.run(
            ["/usr/bin/arch", "-x86_64", str(RUNTIME / "bin/wine"), "--version"],
            env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=15, check=False).stdout.strip(),
        "runtime": str(RUNTIME),
        "wine_dll_search_path": env["WINEDLLPATH"].split(":"),
        "dyld_library_path": dyld_path.split(":"),
        "dxvk_config": config.read_text(),
        "screen_capture": False,
        "cpu_pixel_readback": False,
        "source_build_id": "current-r5-build-A-20261006",
        "native_provider_build_id": "status-v3-current-20261006",
        "human_visual_confirmation": "pending",
    }
    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    preflight = Path(__file__).resolve().parents[4] / "experiments/storage/prefix_preflight.py"
    subprocess.run([sys.executable, str(preflight), str(PREFIX), str(Path(__file__).resolve())], check=True)
    with (OUT / "wineboot.log").open("w") as log:
        subprocess.run([str(RUNTIME / "bin/wine"), "wineboot", "-u"], env=env,
                       stdout=log, stderr=subprocess.STDOUT, timeout=90, check=True)
    start = time.monotonic()
    with (OUT / "runtime.log").open("w") as log:
        process = subprocess.Popen(command, cwd=OUT, env=env, stdout=log,
                                   stderr=subprocess.STDOUT)
        try:
            returncode: int | str = process.wait(timeout=DURATION + 90)
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
    }
    (OUT / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0 if returncode == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
