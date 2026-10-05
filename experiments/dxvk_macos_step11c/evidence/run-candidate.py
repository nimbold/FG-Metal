#!/usr/bin/env python3
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


name = sys.argv[1]
seconds = int(sys.argv[2]) if len(sys.argv) > 2 else 15
internal = "--internal" in sys.argv[3:]
slow = "--slow" in sys.argv[3:]
root = Path(__file__).resolve().parent
out = root / name
out.mkdir(parents=True, exist_ok=True)

wine = Path("/tmp/fgmetal-step11b1/runtime-frozen/wine/bin/wine")
exe = root / "baseline-semantics-15s/d3d11-semantics.exe"
if slow:
    exe = root / "d3d11-semantics-15hz.exe"
d3d11 = Path("/tmp/fgmetal-step11c/build/src/d3d11/d3d11.dll")
baseline_dxgi = root / "baseline-semantics-15s/dxgi.dll"
prefix = Path(os.environ.get(
    "FG_STEP11C_PREFIX", f"/tmp/fgmetal-step11c/prefix-{name}"))
prefix.mkdir(parents=True, exist_ok=True)

for target, source in {
    exe.name: exe,
    "d3d11.dll": d3d11,
    "dxgi.dll": baseline_dxgi,
}.items():
    shutil.copy2(source, out / target)

env = os.environ.copy()
for key in list(env):
    if key.startswith(("DXVK_", "MVK_", "MTL_", "WINE", "FG_", "DYLD_")):
        env.pop(key)

env.update(
    WINEPREFIX=str(prefix),
    WINEDLLOVERRIDES="d3d11,dxgi=n,b",
    WINEDEBUG="+loaddll",
    DXVK_LOG_LEVEL="info",
    DXVK_LOG_PATH=str(out),
    DXVK_HUD="",
    DXVK_INTERNAL_WSI_TRACE="1",
    DYLD_PRINT_LIBRARIES="1",
    FG_AUDIT_ENABLE="1",
    FG_AUDIT_RUN_SECONDS=str(seconds),
    FG_AUDIT_SKIP_RESIZE="1",
    FG_AUDIT_WAITABLE="1",
    FG_AB_BGRA="0",
)
if internal:
    env["DXVK_INTERNAL_WSI_PROOF"] = "1"

with (out / "prefix-init.log").open("w") as log:
    subprocess.run([str(wine), "wineboot", "-u"], env=env,
                   stdout=log, stderr=subprocess.STDOUT, timeout=60, check=True)

start = time.monotonic()
with (out / "runtime.log").open("w") as log:
    process = subprocess.Popen([str(wine), str(out / exe.name)], cwd=out,
                               env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        exit_status = process.wait(timeout=seconds + 90)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        exit_status = "TIMEOUT"

result = {
    "name": name,
    "exit_status": exit_status,
    "wall_seconds": time.monotonic() - start,
    "internal_wsi_enabled": internal,
    "source_cadence_variant": "15hz" if slow else "30hz-baseline-harness",
    "visual": "DIRECT VISUAL CHECK REQUIRED",
    "environment": {
        key: value for key, value in env.items()
        if key.startswith(("DXVK_", "WINE", "FG_", "DYLD_", "MVK_", "MTL_"))
    },
    "sha256": {
        path.name: hashlib.file_digest(path.open("rb"), "sha256").hexdigest()
        for path in out.iterdir() if path.suffix in {".exe", ".dll"}
    },
}
(out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
