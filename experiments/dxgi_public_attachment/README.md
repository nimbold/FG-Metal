# Step 8D.1 public DXGI attachment prototype

This folder contains a test-only native `dxgi.dll` proxy and controlled D3D12 executables. The controlled apps import only Windows DXGI/D3D12 APIs and contain no Framegen calls. See the [Step 8D.1 feasibility report](../../docs/feasibility/dxgi-d3d12-gptk4-step8d1.md) for the decision, coverage limits, and preserved run evidence. The [Step 8D report](../../docs/feasibility/dxgi-d3d12-gptk4-step8d.md) records the earlier experiment.

## Selected layout

The prototype uses an app-local native `dxgi.dll` proxy plus typed COM wrappers for `IDXGIFactory7`, `IDXGIAdapter4`, `IDXGIOutput6`, and `IDXGISwapChain4`. It keeps a canonical inner-`IUnknown` identity registry and a public-interface address map for output unwrapping. Unknown successful `QueryInterface` results and the decode-swapchain route remain public-interface escapes. This is not a general DXGI replacement.

For the tested Highball engine, `LoadLibraryW(L"C:\\windows\\system32\\dxgi.dll")` returned a distinct Wine builtin provider while the app-local proxy was active. This is verified for this Highball configuration, not a general Wine proxy-forwarding contract. Keep the proxy beside the selected executable; putting it in `system32` would make the provider lookup resolve back to the proxy and be rejected as recursion.

The controlled matrix shows that a same-chain synthetic extra Present advances the provider's current backbuffer index: a dynamic-index app passes, while four local-index/fence/allocator policies fail. Copy-only and pass-through work for all five controlled cases. The normal build disables synthetic G; the tested diagnostic requires the explicit `FG_DXGI_ENABLE_UNSAFE_CONTROLLED_TEST_G` build define and `FG_DXGI_G=1`. Use that build only with the controlled progression test. The current Highball DComp Device2/3 entry points return `E_NOTIMPL`; no separate composition path is established. See the report for exact limitations.

## Build

Build from the repository root with MinGW-w64:

```sh
x86_64-w64-mingw32-g++ -std=c++17 -O2 -Wall -Wextra -Wno-cast-function-type \
  -shared -static -static-libgcc -static-libstdc++ \
  experiments/dxgi_public_attachment/dxgi_proxy.cpp \
  experiments/dxgi_public_attachment/dxgi_proxy.def \
  -o /tmp/step8d1_dxgi_proxy.dll -ldxgi -ld3d12 -ldxguid

x86_64-w64-mingw32-g++ -std=c++17 -O2 -Wall -Wextra -mwindows \
  experiments/dxgi_public_attachment/unmodified_d3d12_app.cpp \
  -o /tmp/step8d1_unmodified_d3d12.exe -ld3d12 -ldxgi -ldxguid -lpsapi

x86_64-w64-mingw32-g++ -std=c++17 -O2 -Wall -Wextra -mwindows \
  experiments/dxgi_public_attachment/lifecycle_app.cpp \
  -o /tmp/step8d1_lifecycle_app.exe -ld3d12 -ldxgi -ldxguid -lpsapi

x86_64-w64-mingw32-g++ -std=c++17 -O2 -Wall -Wextra -mwindows \
  experiments/dxgi_public_attachment/two_swapchains_app.cpp \
  -o /tmp/step8d1_two_swapchains.exe -ld3d12 -ldxgi -ldxguid -lpsapi

x86_64-w64-mingw32-g++ -std=c++17 -O2 -Wall -Wextra -mwindows \
  experiments/dxgi_public_attachment/progression_cases_app.cpp \
  -o /tmp/step8d1_progression.exe -ld3d12 -ldxgi -ldxguid -lpsapi
```

To reproduce the historical same-chain G experiment, compile a separate controlled-only proxy with the explicit unsafe-test define:

```sh
x86_64-w64-mingw32-g++ -std=c++17 -O2 -Wall -Wextra -Wno-cast-function-type \
  -DFG_DXGI_ENABLE_UNSAFE_CONTROLLED_TEST_G=1 \
  -shared -static -static-libgcc -static-libstdc++ \
  experiments/dxgi_public_attachment/dxgi_proxy.cpp \
  experiments/dxgi_public_attachment/dxgi_proxy.def \
  -o /tmp/step8d1_dxgi_proxy_gtest.dll -ldxgi -ld3d12 -ldxguid
```

The define intentionally enables behavior already shown to break application-owned swapchain progression. Do not ship this build or use it on another application.

The matrix and failpoint runners default to the controlled-only proxy at `/tmp/step8d1_dxgi_proxy_gtest.dll`. They fail if that explicitly enabled build is missing, so a disabled G path cannot silently turn a synthetic test into pass-through.

The `step8d1-final-matrix/run-manifest.txt` and `step8d1-final-failpoints/run-manifest.txt` record the tested controlled-app and proxy binary hashes and runtime selection.

## Highball test configuration

The run script uses the Highball 0.10.1 engine `x64-sikarugir10.0_6-r14`, a disposable prefix, and these Wine overrides:

```text
WINEDLLOVERRIDES=dxgi=n,b;d3d11,d3d10core,d3d12,d3d12core=n,b;winemenubuilder.exe=d
```

Example from a directory containing the app-local DLL and controlled executable:

```sh
FG_STEP8D_PREFIX="$HOME/Library/Caches/FG-Metal-Step8D-20261002/transparent-prefix" \
FG_DXGI_LOG=1 FG_DXGI_COPY=1 FG_DXGI_G=0 \
bash /path/to/FG-Metal/experiments/dxgi_public_attachment/run_highball.sh ./step8d_unmodified.exe
```

The run script sets the Highball renderer paths and public per-prefix Wine environment. No Highball source change was made. The tested setup proves that this configuration can select the app-local proxy for the controlled application; it does not establish one-click Highball packaging or compatibility with existing app-local DXGI files.

## Diagnostic controls

All proxy features default off.

- `FG_DXGI_LOG=1`: record factory, swapchain, resize, present, copy, and synthetic-present events.
- `FG_DXGI_COPY=1`: copy the currently presented D3D12 backbuffer to an adapter-owned D3D12 resource on the same direct queue. No CPU pixel readback occurs.
- `FG_DXGI_G=1`: only honored in a proxy compiled with `FG_DXGI_ENABLE_UNSAFE_CONTROLLED_TEST_G=1`. It clears the current backbuffer magenta and issues an extra same-chain `Present`; the experiment showed that this advances the application's backbuffer index and breaks four tested local-index/fence/allocator policies. Never enable this outside the controlled progression test.
- `FG_DXGI_G_SYNC_INTERVAL=0`: request sync interval zero for the synthetic G; default is one.
- `FG_DXGI_TEST_FAIL_GPU_WORK=1`: inject a failure before adapter GPU work.
- `FG_DXGI_TEST_FAILPOINT=<name>`: test-only one-shot failure points; see `run_failpoint_matrix.sh` for the exercised list.

The proxy only enables D3D12 work for a direct queue on a one-node device with node mask 0 or 1. It expects a flip-model, non-protected swapchain and treats the current app backbuffer as `PRESENT` at Present time, as required by the documented D3D12 swapchain contract. `DXGI_PRESENT_TEST` calls are forwarded without adapter GPU work. Provider `Present` and resize calls run outside the per-swapchain state lock; a resize generation check suppresses G when lifecycle changes during a source Present. Concurrent source Presents disable adapter work for that swapchain. Unsupported device/queue/resource configurations are logged and left on the original present path.

The synthetic G changes the underlying swapchain's current index in the tested provider. The controlled app that re-queries `GetCurrentBackBufferIndex` each frame passes; four controlled local-index/fence/allocator policies fail. The result is **PARTIAL PASS**, not safe transparent synthetic presentation. Use `run_progression_matrix.sh`, `run_failpoint_matrix.sh`, and `run_overhead_trials.sh` only in the controlled disposable-prefix experiment.
