#!/usr/bin/env python3
"""Run the corrected FLIP_SEQUENTIAL app on the exact cached 11C DLL pair."""
from __future__ import annotations

import hashlib
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = ROOT / "experiments/dxvk_macos_step11dR/evidence"
FINAL = ROOT / "experiments/dxvk_macos_step11dR/evidence/p0.2-r-current-r5/dlls"
APP = Path("/Users/nima/.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/p0.2/d3d11-flip-sequential-invariant-static.exe")
RUNTIME = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/runtime-frozen/runtime/wine")
WINE = RUNTIME / "bin/wine"
ICD = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json")
LOADER = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override")
OUT = ROOT / "experiments/dxvk_macos_step11dR/evidence/p0.2-r-current-r5/runs"
DEFAULT_EXPECTED = {
    "d3d11.dll": "c7f65abc004c154f935836ab0d682f8339270c2251b13af1326094acec82399e",
    "dxgi.dll": "58e972688c739ee8444c8efa7a175503ff8dd0774ceb63e9b8526168054d9132",
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def run(name: str, internal: bool, dll_source: Path, expected: dict[str, str], duration: int,
        output_root: Path, wine_debug: str, trace_backbuffers: bool) -> None:
    dest = (output_root / name).resolve()
    dll_source = dll_source.resolve()
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copy2(APP, dest / APP.name)
    for dll in expected:
        shutil.copy2(dll_source / dll, dest / dll)
    for dll, wanted in expected.items():
        actual = digest(dest / dll)
        if actual != wanted:
            raise SystemExit(f"refusing to run: {dll} hash {actual} != {wanted}")

    prefix = Path(f"/Users/nima/Library/Caches/FGMetalStep11D-R/step11d1-flipseq-{name}-20261006-prefix")
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("DXVK_", "MVK_", "MTL_", "WINE", "FG_", "DYLD_", "VK_")):
            env.pop(key, None)
    env.update({
        "WINEPREFIX": str(prefix),
        "WINEDLLPATH": ":".join((str(RUNTIME / "lib/wine/x86_64-unix"),
                                  str(RUNTIME / "lib/wine/x86_64-windows"),
                                  str(RUNTIME / "lib/wine/i386-windows"))),
        "WINEARCH": "win64",
        "WINEDEBUG": wine_debug,
        "WINEDLLOVERRIDES": "d3d11,dxgi=n,b",
        "DXVK_LOG_LEVEL": "info",
        "DXVK_LOG_PATH": str(dest),
        "DXVK_HUD": "",
        "DXVK_INTERNAL_WSI_TRACE": "1",
        "VK_ICD_FILENAMES": str(ICD),
        "VK_DRIVER_FILES": str(ICD),
        "DYLD_LIBRARY_PATH": str(LOADER),
        "DYLD_FALLBACK_LIBRARY_PATH": f"{LOADER}:/usr/local/lib:/usr/lib",
        "FG_AUDIT_ENABLE": "1",
        "FG_AUDIT_RUN_SECONDS": str(duration),
        "FG_AUDIT_LIFECYCLE_TEST": "0",
        "FG_AUDIT_SKIP_RESIZE": "1",
        "FG_AUDIT_WAITABLE": "1",
        "FG_AB_BGRA": "0",
    })
    if trace_backbuffers:
        env["DXVK_D3D11_BACKBUFFER_TRACE"] = "1"
    if internal:
        env["DXVK_INTERNAL_WSI_PROOF"] = "1"

    preflight = ROOT / "experiments/storage/prefix_preflight.py"
    subprocess.run([sys.executable, str(preflight), str(prefix), str(Path(__file__).resolve())], check=True)
    with (dest / "wineboot.log").open("w") as log:
        subprocess.run([str(WINE), "wineboot", "-u"], env=env, stdout=log,
                       stderr=subprocess.STDOUT, timeout=90, check=True)
    started = time.monotonic()
    with (dest / "runtime.log").open("w") as log:
        proc = subprocess.Popen([str(WINE), str(dest / APP.name)], cwd=dest,
                                env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            status: int | str = proc.wait(timeout=duration + 90)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                status = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                status = proc.wait()
            status = f"TIMEOUT_AFTER_TERMINATION_{status}"

    app_csv = dest / "d3d11_clear_window_app.csv"
    semantics_csv = dest / "semantics.csv"
    app_rows = app_csv.read_text(errors="replace").splitlines() if app_csv.exists() else []
    semantic_rows = semantics_csv.read_text(errors="replace").splitlines() if semantics_csv.exists() else []
    presents = [row for row in app_rows if row.startswith("present,")]
    observations = [row for row in semantic_rows if row.startswith("observe,")]
    runtime_text = (dest / "runtime.log").read_text(errors="replace")
    internal_events = [line for line in runtime_text.splitlines()
                       if "InternalWSI:" in line and "opportunity=" not in line]
    backbuffer_events = [line for line in runtime_text.splitlines()
                         if "D3D11BackBufferTrace:" in line]
    app_rotations = [line for line in backbuffer_events
                     if "event=app-rotation " in line]
    presents_without_rotation = [line for line in backbuffer_events
                                 if "event=app-rotation-skipped " in line]
    rejected_presents = [line for line in backbuffer_events
                         if "event=present-rejected " in line]
    result = {
        "name": name,
        "exit_status": status,
        "wall_seconds": time.monotonic() - started,
        "internal_wsi_enabled": internal,
        "requested_source_hz": 30,
        "duration_seconds": duration,
        "swap_effect": "DXGI_SWAP_EFFECT_FLIP_SEQUENTIAL",
        "buffer_count": 2,
        "backbuffer_trace_enabled": trace_backbuffers,
        "waitable_requested": True,
        "app_present_rows": len(presents),
        "last_app_present_row": presents[-1] if presents else None,
        "semantics_observation_rows": len(observations),
        "last_semantics_observation": observations[-1] if observations else None,
        "internal_wsi_log_lines": len(internal_events),
        "backbuffer_trace_lines": len(backbuffer_events),
        "backbuffer_create_lines": sum("event=create " in line for line in backbuffer_events),
        "backbuffer_present_begin_lines": sum("event=present-begin " in line for line in backbuffer_events),
        "backbuffer_rotation_slot_lines": len(app_rotations),
        "app_rotation_skipped_lines": len(presents_without_rotation),
        "rejected_application_presents": len(rejected_presents),
        "dll_source": str(dll_source),
        "dll_sha256": {dll: digest(dest / dll) for dll in expected},
        "app_sha256": digest(dest / APP.name),
        "human_visual_confirmation": "not requested for this accounting run",
        "screen_capture": False,
        "cpu_pixel_readback": False,
    }
    (dest / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dll-dir", type=Path, default=FINAL)
    parser.add_argument("--d3d11-sha256", default=DEFAULT_EXPECTED["d3d11.dll"])
    parser.add_argument("--dxgi-sha256", default=DEFAULT_EXPECTED["dxgi.dll"])
    parser.add_argument("--duration", type=int, default=30)
    parser.add_argument("--output-dir", type=Path, default=OUT)
    parser.add_argument("--wine-debug", default="-all")
    parser.add_argument("--only", choices=("source", "internal", "both"), default="both")
    parser.add_argument("--no-backbuffer-trace", action="store_true")
    args = parser.parse_args()
    expected = {"d3d11.dll": args.d3d11_sha256, "dxgi.dll": args.dxgi_sha256}
    args.output_dir.mkdir(parents=True, exist_ok=True)
    if args.only in ("source", "both"):
        run("flipseq-source-only", internal=False, dll_source=args.dll_dir,
            expected=expected, duration=args.duration, output_root=args.output_dir,
            wine_debug=args.wine_debug, trace_backbuffers=not args.no_backbuffer_trace)
    if args.only in ("internal", "both"):
        run("flipseq-internal", internal=True, dll_source=args.dll_dir,
            expected=expected, duration=args.duration, output_root=args.output_dir,
            wine_debug=args.wine_debug, trace_backbuffers=not args.no_backbuffer_trace)
