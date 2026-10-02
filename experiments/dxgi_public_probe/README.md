# Public DXGI/D3D12 probe

This cooperative Win32 harness tests documented DXGI/D3D12 calls under Wine. The executable creates and owns its own D3D12 device and swapchain, explicitly loads `fg_probe.dll`, and passes public COM interfaces to it. It does not inject into or intercept an unmodified game. Step 8C ran it through the actual Highball GPTK 4.0 beta 2 / D3DMetal 4.0b2 runtime; see the [Step 8C report](../../docs/feasibility/dxgi-d3d12-gptk4-step8c.md). The earlier [Step 8B report](../../docs/feasibility/dxgi-d3d12-gptk4-step8b.md) now includes a correction that its runner was GameHub/Proton, not Highball.

The adapter retrieves swapchain backbuffers with `GetBuffer`, copies source content to D3D12 resources that it owns, issues GPU copy/clear commands with explicit barriers and fences, and requests interleaved `Present`/`Present1` calls on the same cooperative swapchain. Source frames A/B/C use distinct GPU markers; G uses their scalar midpoint as a GPU clear color plus a green center-cross marker. G is a synthetic diagnostic image, not a texture blend or neural interpolation. The probe uses no screen capture or CPU pixel readback, and it does not convert D3D12 resources to Metal textures.

## Build

The spike was cross-compiled on macOS with Homebrew MinGW-w64 14.0.0. Full Xcode and the Metal Toolchain were not required.

```sh
x86_64-w64-mingw32-g++ -std=c++17 -O2 -Wall -Wextra -Wno-cast-function-type \
  -shared experiments/dxgi_public_probe/adapter.cpp \
  -o /tmp/fg_probe.dll -ld3d12 -ldxgi

x86_64-w64-mingw32-g++ -std=c++17 -O2 -Wall -Wextra -Wno-cast-function-type \
  experiments/dxgi_public_probe/harness.cpp \
  -o /tmp/dxgi_probe.exe -ld3d12 -ldxgi -luser32

mkdir -p /tmp/fg-dxgi-probe
cp /tmp/fg_probe.dll /tmp/dxgi_probe.exe /tmp/fg-dxgi-probe/
```

## Run

Run from the directory containing both files. For an ordinary Wine smoke check, set `WINE` to a runner and `WINEPREFIX` to a disposable prefix. Display-cadence results require a supported runtime and display-side counters; Present calls alone are insufficient.

```sh
cd /tmp/fg-dxgi-probe
WINEPREFIX=/tmp/fg-dxgi-probe-prefix WINEDEBUG=+loaddll,+fixme \
  "$WINE" ./dxgi_probe.exe --source=30 --seconds=5 --lifecycle

WINEPREFIX=/tmp/fg-dxgi-probe-prefix WINEDEBUG=+loaddll,+fixme \
  "$WINE" ./dxgi_probe.exe --source=60 --seconds=5
```

The validated Highball command used `--source=30 --seconds=60`; it ran from a disposable prefix with Highball’s manifest and renderer environment, plus `DYLD_PRINT_LIBRARIES=1`, `MTL_HUD_ENABLED=1`, and `MTL_HUD_LOG_ENABLED=1`. The exact versions, environment, loader trace, output mode, and measurements are in the [Step 8C report](../../docs/feasibility/dxgi-d3d12-gptk4-step8c.md) and [preserved evidence](evidence/). For a short lifecycle pass, use `--source=30 --seconds=10 --lifecycle` under the same runtime. Do not use `--source=60` as a 60→120 display test unless a 120-Hz output is verified.

Arguments select 30 or 60 source FPS, duration in seconds (up to 600), and optional `--lifecycle` for a resize, fullscreen/windowed changes, and helper-window focus recovery. Output is appended to `dxgi_probe.log` in the current working directory. The adapter logs module paths, swapchain interfaces and descriptors, resource identities, source/G/present QPC timestamps, `GetLastPresentCount`, `GetFrameStatistics`, GPU/fence results, resize, fullscreen, and focus outcomes.

Do not interpret Present HRESULTs or `GetLastPresentCount` alone as proof that every image reached the display. Pair public monitor-presentation statistics with independent display-side timing, and report timing outliers and any identity limits.
