#!/usr/bin/env python3
"""Run the Step 11B.1 RGBA/BGRA clear harness on the Step 11C DXVK DLL."""

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
bgra = "--bgra" in sys.argv[3:]
root = Path(__file__).resolve().parent
out = root / name
out.mkdir(parents=True, exist_ok=True)

wine = Path("/tmp/fgmetal-step11b1/runtime-frozen/wine/bin/wine")
exe = Path("/tmp/fgmetal-step11b1/format-ab.exe")
d3d11 = Path("/tmp/fgmetal-step11c/build/src/d3d11/d3d11.dll")
dxgi = root / "baseline-semantics-15s/dxgi.dll"
prefix = Path(os.environ.get(
    "FG_STEP11C_PREFIX", f"/tmp/fgmetal-step11c/prefix-{name}"))

for target, source in {exe.name: exe, "d3d11.dll": d3d11, "dxgi.dll": dxgi}.items():
    shutil.copy2(source, out / target)

env = os.environ.copy()
for key in list(env):
    if key.startswith(("DXVK_", "MVK_", "MTL_", "WINE", "FG_", "DYLD_")):
        env.pop(key)
env.update(
    WINEPREFIX=str(prefix),
    WINELOADER=str(wine),
    WINESERVER=str(wine.with_name("wineserver")),
    WINEDLLOVERRIDES="d3d11,dxgi=n,b",
    WINEDEBUG="+loaddll",
    DXVK_LOG_LEVEL="info",
    DXVK_LOG_PATH=str(out),
    DXVK_HUD="",
    DYLD_PRINT_LIBRARIES="1",
    FG_AUDIT_ENABLE="1",
    FG_AUDIT_RUN_SECONDS=str(seconds),
    FG_AUDIT_SKIP_RESIZE="1",
    FG_AB_BGRA="1" if bgra else "0",
)

preflight = Path(__file__).resolve().parents[3] / "experiments/storage/prefix_preflight.py"
subprocess.run([sys.executable, str(preflight), str(prefix), str(Path(__file__).resolve())],
               env=env, check=True)
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
    "format": "BGRA8" if bgra else "RGBA8",
    "internal_wsi_enabled": False,
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
