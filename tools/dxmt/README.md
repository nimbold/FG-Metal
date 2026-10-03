# DXMT Step 10 reproducibility

These helpers build and run a local DXMT copy. They do not install into the
user's Highball engine or reuse a normal Wine prefix. DXMT source changes for
this experiment belong in a separate downstream worktree, never the pinned
baseline checkout.

Runtime-copy, staging, and launch helpers canonicalize their paths and require
the engine copy and Wine prefix to be under a temporary directory. Prefix and
run directories must be fresh; choose new names for each run. Runtime staging
prepares every replacement before installing any, rejects symlink/non-file
targets, and rolls back already replaced files if an install step fails.

## Framegen baseline

```sh
./tools/dxmt/build_framegen_baseline.sh
```

Pass an alternate build directory as the first argument. The script builds the
current Framegen checkout and runs all registered CTest tests, including the
Metal C API test when the host supports it.

## Pinned DXMT x86_64 cross-build

Use the audited source revision and both pinned submodules. The required
toolchain prefixes are an x86_64 LLVM 15 macOS build and the Wine 8.16 install
prefix used by DXMT CI:

```sh
git clone https://github.com/3Shain/dxmt.git /tmp/dxmt-framegen-step10
git -C /tmp/dxmt-framegen-step10 checkout fb4515681daefb789a4d0f403c4bdbca88f3b3de
git -C /tmp/dxmt-framegen-step10 submodule update --init --recursive
meson setup /tmp/dxmt-framegen-step10-build /tmp/dxmt-framegen-step10 \
  --cross-file /tmp/dxmt-framegen-step10/build-win64.txt \
  -Dnative_llvm_path="$LLVM15_X86_64" \
  -Dwine_install_path="$WINE_816_INSTALL" \
  -Denable_d3d12=false -Denable_nvapi=false -Denable_nvngx=false \
  --buildtype release
meson compile -C /tmp/dxmt-framegen-step10-build -j 8 -v
```

The recorded successful build used Meson 1.12.1, Ninja 1.13.2, MinGW-w64
14.0.0_3, x86_64 MinGW GCC 16.2.0, Wine tag `v8.16-3shain`, and LLVM 15.0.7.
It produced the four PE runtime DLLs and x86_64 `winemetal.so`. The target's
compiler warnings are retained in `experiments/dxmt_framegen/evidence/step10a/dxmt/`.
There is no separate offline Metal shader target in this revision: DXMT embeds
its generated Metal library, and the Framegen Metal test compiles its shader
source at runtime.

## Isolated Wine runtime and baseline

Copy the installed Sikarugir x86_64 engine to a new temporary directory, stage
only the DXMT runtime outputs, and use a new prefix:

```sh
./tools/dxmt/copy_runtime_for_smoke.sh "$SOURCE_ENGINE" /tmp/dxmt-engine-copy
./tools/dxmt/stage_dxmt_runtime.sh /tmp/dxmt-engine-copy \
  /tmp/dxmt-framegen-step10-build "$LLVM15_X86_64/lib"
./tools/dxmt/build_d3d11_clear_window.sh
./tools/dxmt/run_d3d11_baseline.sh /tmp/dxmt-engine-copy \
  /tmp/dxmt-baseline-prefix \
  ./build/step10a-d3d11-clear-window/d3d11_clear_window.exe
```

Use a fresh engine destination when copying, and a new prefix name for every
baseline run. If the destination exists, `copy_runtime_for_smoke.sh` refuses to
reuse it. If the relative `libinotify` target already exists beside the
destination engine, it is reused only when it matches the source byte-for-byte.

`stage_dxmt_runtime.sh` copies only `d3d10core.dll`, `d3d11.dll`, `dxgi.dll`,
`winemetal.dll`, `winemetal.so`, and the matching LLVM 15 `libc++`/`libc++abi`
runtime dylibs. Highball's `libinotify.0.dylib` engine symlink is resolved in
the disposable copy. The installed engine and normal prefixes stay untouched.

The 10-second D3D11 app clears a color, calls `Present(1, 0)`, resizes its
swapchain at frame 90, and exits nonzero on D3D/device/presentation/resize
failure. The successful Wine10 launch loaded the built-in DXMT `winemetal.dll`,
`DXGI.DLL`, and `d3d11.dll`, selected D3D feature level 11_0, and completed
with exit status 0. Its evidence proves the DXMT render/Present path executed;
no physical pixel scanout was visually inspected.

The successful Wine10 engine copy also needed the LLVM 15 `libc++.1.dylib` and
`libc++abi.1.dylib` beside `winemetal.so`. An earlier Wine10 attempt without
those dylibs failed during module initialization. That setup failure and the
separate Wine 8.17 fallback failure are retained in the evidence folder.

The native texture diagnostic is deliberately separate from the baseline. Its
patch is retained at
`experiments/dxmt_framegen/evidence/step10a/runtime/native-winemetal-resource-diagnostic.patch`.
It logs native `MTLDevice` identity and source texture width, height, format,
and sample count without reading pixels.

## Display-link timing probe

```sh
./tools/dxmt/run_cametal_displaylink_probe.sh /tmp/cametal-displaylink.csv
python3 ./tools/dxmt/summarize_cametal_displaylink.py /tmp/cametal-displaylink.csv
```

The native probe runs for 15 seconds on the active display. It records
`CAMetalDisplayLink` callback cadence, target and target-presentation fields,
and a Mach host-time correlation. It does not render, request drawables, or
measure scanout. Its output path must be new. The retained 2026-10-03 run measured 59.8636 callbacks/s and
16.6664 ms mean target-presentation intervals on the 60 Hz built-in display.

The framegen experiment runner also requires a fresh output directory and
Wine prefix. Its analyzer accepts raw CSV and `.csv.gz` evidence. A callback
ledger mismatch, missing application/native log, or unverified callback ledger
makes the runner fail instead of returning a successful run status.

## Evidence locations

- `experiments/dxmt_framegen/evidence/step10a/` — exact build, timing, runtime,
  module-loader, and WineMetal resource evidence.
- `experiments/dxmt_framegen/evidence/step10b/` — synthetic backend and
  display-path experiment, with its own explicit limits.
- `docs/feasibility/dxmt-framegen-step10.md` — consolidated outcome, source
  architecture, licensing, reviewer findings, and unresolved integration work.
