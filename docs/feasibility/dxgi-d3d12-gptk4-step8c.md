# Step 8C — Cooperative DXGI/D3D12 validation on Highball GPTK 4

**Decision: PASS for the cooperative presentation experiment.** The public DXGI/D3D12 harness ran through Highball’s GPTK 4.0 beta 2 / D3DMetal 4.0b2 runtime, accessed D3D12 swapchain buffers, performed GPU-only work, and inserted a GPU-generated G between source frames. DXGI monitor-presentation counters advanced for every submitted image, and Apple’s Metal Performance HUD independently recorded the present cadence. This is credible display-side evidence for the controlled harness.

This does **not** validate transparent interception of an unmodified game, identify G’s pixels through readback, or establish a production-quality 2x path. The presentation deadline log has large late offsets and a few long intervals. There was no RIFE, no 60→120 test because the available display is 60 Hz, and no 30-minute soak. No D3DMetal private implementation detail was inspected.

## Runtime identity

The test used the engine installed for `/Applications/Highball.app`, not the earlier package attributed to Highball in the Step 8B report. Highball 0.10.1’s public source and engine manifest identify the actual runtime as follows:

| Component | Installed identity | In-process evidence | Verification |
|---|---|---|---|
| Highball | 0.10.1 | App at `/Applications/Highball.app` | App `Info.plist` |
| Wine engine | `x64-sikarugir10.0_6-r14`; Wine 10.0 Sikarugir | Runner `<ENGINE>/engine/bin/wine`; Wine reports `wine-10.0 (Sikarugir)` | Public engine manifest and runner version |
| GPTK/D3DMetal package | `D3DMetal-4.0b2 (Game Porting Toolkit 4.0 beta 2, redist/lib)`; manifest component SHA-256 `96cbbe89b71cb07cc33bd761ae4b79452b9cdf3198593dfb779036caf85f07a9` | `<ENGINE>/renderers/d3dmetal/external/D3DMetal.framework/Versions/A/D3DMetal` loaded | Highball manifest, public framework `Info.plist` (`CFBundleShortVersionString=4.0b2`, `CFBundleVersion=4.0b2`), and `DYLD_PRINT_LIBRARIES` |
| GPTK shared D3D runtime | Engine’s `libd3dshared.dylib` | `<ENGINE>/renderers/d3dmetal/external/libd3dshared.dylib` loaded in the same process | `DYLD_PRINT_LIBRARIES` |
| DXGI entry point | Wine builtin `dxgi.dll` | `C:\windows\system32\dxgi.dll`, provider `builtin` | `GetModuleFileNameW` and Wine `+loaddll` trace |
| D3D12 entry point | Wine builtin `d3d12.dll`; optional Highball timestamp-shim component 20260915 was in the configured renderer path | `C:\windows\system32\d3d12.dll` and the shim’s `apd12.dll` path both appear as builtins in the Wine trace | `GetModuleFileNameW`, Wine `+loaddll`, and Highball manifest |

Highball’s engine manifest labels its component GPTK 4.0 beta 2 / D3DMetal 4.0b2; this is the package actually tested. It is not identified here as a separate Apple package named “GPTK 4.0-2.” The Step 8B report’s `com.gamemac.www` package attribution was incorrect: that was a different GameHub/Proton tree, and its run did not validate this Highball runtime. The Step 8B results remain useful only as evidence about that earlier Wine-builtin run.

The host was macOS 27.0.1 (build 26A434), arm64, Apple M3. CoreGraphics reported the built-in Liquid Retina display at 60 Hz and its five available display modes all at 60 Hz. The disposable prefix was `$HOME/Library/Caches/FG-Metal-Step8C/validated-prefix-2`, initialized as `win64` with the Highball runner. The exact path/version evidence and filtered loader excerpt are in [the runtime evidence file](../../experiments/dxgi_public_probe/evidence/step8c-runtime-load.txt).

### Highball environment reproduced

I read Highball’s public engine manifest and `Bottle.environment` implementation at source revision [`e9e7472`](https://github.com/gauthierpiarrette/highball/tree/e9e7472f3fe870aa83071d183f263acf09c2b9e0). For the D3DMetal renderer, the harness used the same public launch configuration, with diagnostic variables added for loader and HUD logging:

```text
WINEPREFIX=$HOME/Library/Caches/FG-Metal-Step8C/validated-prefix-2
WINEARCH=win64
WINEMSYNC=1
WINEESYNC=0
CX_FWD_COMPAT_GL_CTX=1
D3DM_MTL4=0
WINEDLLPATH_PREPEND=<ENGINE>/renderers/d3dmetal-tsshim/wine:<ENGINE>/renderers/d3dmetal/wine:<ENGINE>/renderers/dxmt/wine:<ENGINE>/frameworks/renderer/d9vk/wine
WINEDLLOVERRIDES=dxgi,d3d11,d3d10core,d3d12,d3d12core=n,b;winemenubuilder.exe=d
HB_D3D12_REAL=Z:<ENGINE>/renderers/d3dmetal-tsshim/wine/x86_64-windows/apd12.dll
CX_D3DMETALPATH=<ENGINE>/renderers/d3dmetal/external
DYLD_FALLBACK_FRAMEWORK_PATH=<ENGINE>/renderers/d3dmetal/external:<ENGINE>/frameworks
DYLD_FALLBACK_LIBRARY_PATH=<ENGINE>/renderers/d3dmetal/external:<ENGINE>/frameworks:<ENGINE>/frameworks/GStreamer.framework/Versions/1.0/lib
GST_PLUGIN_PATH=<ENGINE>/frameworks/GStreamer.framework/Versions/1.0/lib/gstreamer-1.0
WINEDEBUG=fixme-all,+loaddll                 # loader-diagnostic addition
DYLD_PRINT_LIBRARIES=1                       # loader-diagnostic addition
MTL_HUD_ENABLED=1                            # display-metric addition
MTL_HUD_LOG_ENABLED=1                        # display-metric addition
```

In that block, `<ENGINE>` means `$HOME/Library/Application Support/Highball/engines/x64-sikarugir10.0_6-r14`; `HB_D3D12_REAL` uses the equivalent Wine `Z:\` path to the same engine file. The process did not set `WINE_GPTK_LIBD3DSHARED_PATH` or `DYLD_FRAMEWORK_PATH`; Highball’s actual configuration locates the runtime with `CX_D3DMETALPATH` and the fallback framework/library paths above. `D3DM_MTL4=0` is the engine’s recorded setting. No Highball application code or normal game prefix was changed. The run required MinGW-w64, not full Xcode or the Metal Toolchain.

## Experimental architecture and public APIs

```text
Cooperative Win32 harness
  → CreateDXGIFactory2 / IDXGIFactory2
  → D3D12CreateDevice / ID3D12Device
  → CreateCommandQueue / direct ID3D12CommandQueue
  → CreateSwapChainForHwnd / IDXGISwapChain1
  → explicitly loaded fg_probe.dll receives the public COM interfaces
  → Wine DXGI/D3D12 builtins configured with Highball’s D3DMetal renderer
  → GPTK 4.0b2 libd3dshared + D3DMetal.framework
```

This remains an explicitly cooperative adapter: the harness creates the swapchain, calls `LoadLibraryW("fg_probe.dll")`, and passes its factory, device, direct queue, and swapchain to the adapter. It is not a DXGI proxy, COM vtable patch, injected library, or unmodified game. The experiment proves the public D3D12 path above D3DMetal in a controlled app; it does not prove that Highball automatically attaches the adapter to a game.

The harness created a 960×540, three-buffer, `R8G8B8A8_UNORM` flip-discard swapchain. `IDXGISwapChain1`, `IDXGISwapChain3`, and `IDXGISwapChain4` queries succeeded. It called `GetBuffer` for each `ID3D12Resource`, logged canonical `IUnknown` identity, and verified width, height, format, and 2D resource dimension. The adapter also created two library-owned committed D3D12 textures.

Adapter GPU work used the application’s direct queue, command allocators/list, render-target views, `CopyResource`, `ClearRenderTargetView`, documented transition barriers, and `ID3D12Fence`. The adapter copied source buffer content into an owned D3D12 texture, rendered the diagnostic output, transitioned resources back to `PRESENT`, signaled a fence, and waited for completion before buffer reuse/presentation or resize. It did not create an `MTLTexture`, access a Metal object, read pixels on the CPU, or capture the screen.

The synthetic content is intentionally simple and identifiable:

- A, B, and C are harness-controlled full-frame source colors with different GPU-drawn geometric markers.
- Between sources, G is drawn on the GPU with a base color calculated as the scalar midpoint `0.5*A + 0.5*B`, plus a distinct green center-cross marker.
- The GPU also copies source content through adapter-owned D3D12 resources. G is a synthetic presentation probe, not a texture-to-texture blend or a neural interpolation result.

The observed sequence was `A, G(A,B), B, G(B,C), C, ...`. The single cooperative swapchain alternated `Present` and `Present1`; each call requested sync interval 1. This is enough to test presentation ownership and cadence without pretending G came from RIFE.

## 30→60 result

The main run requested 30 source frames/s and 60 output frames/s for 60 seconds. It completed in 60.033930 seconds with 1,800 source frames, 1,799 G frames, and 3,599 successful presents: 901 `Present` calls and 2,698 `Present1` calls. The sequence contained 1 A, 900 B, 899 C, and 1,799 G records. Every Present, `GetLastPresentCount`, `GetFrameStatistics`, and logged fence operation returned `S_OK`; there were no device-removal results or process crashes.

### Display-side evidence

After each Present/Present1, the harness queried `GetFrameStatistics` and `GetLastPresentCount`. All 3,599 statistics queries returned `S_OK`. The first observation established a baseline; each of the following 3,598 observations advanced both `DXGI_FRAME_STATISTICS.PresentCount` and `PresentRefreshCount` by exactly one. The final values were 3,599. Microsoft defines `PresentCount` as images presented to the monitor and `PresentRefreshCount` as the refresh at which the last image was presented; these counters need not equal the number of Present calls. That makes them materially stronger than call counts alone. See [`DXGI_FRAME_STATISTICS`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/ns-dxgi-dxgi_frame_statistics).

The Apple Metal Performance HUD independently logged 59 once-per-second batches containing 3,594 frame present intervals. Of those intervals, 3,589 were 16.67 ms, one was 27.76 ms, three were 33.33 ms, and one was 50.00 ms. Median, p95, and p99 were 16.67 ms; maximum was 50.00 ms. This corroborates output near 60 Hz with occasional long gaps. Apple documents that HUD logging reports frame numbers and per-frame present intervals; the recorded HUD lines are preserved in [the display evidence file](../../experiments/dxgi_public_probe/evidence/step8c-30to60-metal-hud.log). See [Apple’s Metal Performance HUD logging documentation](https://developer.apple.com/documentation/xcode/monitoring-your-metal-apps-graphics-performance).

Together, the controlled A/G/B sequence and one-image/one-refresh DXGI increments after each G provide credible evidence that the synthetic G presentations reached the display between source presentations. The HUD-reported present cadence corroborates that output ran near 60 Hz. This does not reveal the actual pixel values of each displayed image. The evidence supports the requested feasibility gate without claiming a screenshot-level identity check.

### Timing and resource measurements

QPC ran at 10 MHz. Values below are milliseconds computed from the harness timestamps. “Fence completion observed” is when the CPU observed the D3D12 fence complete; it is not a hardware GPU timestamp.

| Measurement | Samples | Median | p95 | p99 | Max |
|---|---:|---:|---:|---:|---:|
| Source production interval | 1,799 | 33.339 | 34.362 | 35.472 | 94.596 |
| G GPU-submit interval | 1,798 | 33.381 | 34.899 | 35.840 | 102.103 |
| Present call start interval | 3,598 | 16.569 | 20.957 | 22.135 | 68.767 |
| Present API call duration | 3,599 | 0.0166 | 0.0303 | 0.0547 | 0.1938 |
| GPU fence signal to CPU-observed completion before Present | 3,599 | 1.834 | 14.922 | 15.852 | 68.648 |

The harness also logged absolute QPC deadline lateness separately from the actual call and display intervals. G schedule lateness was median 48.613 ms, p95 51.249 ms, p99 52.345 ms, max 83.967 ms. Source Present schedule lateness was median 32.748 ms, p95 36.126 ms, p99 37.147 ms, max 68.384 ms. At source frame 900, source-production scheduling was about 33.066 ms late and retained roughly that phase offset afterward. The monitor counters and HUD still show the near-60-Hz display sequence, but the deadline figures expose added phase delay and the long-interval tail; this was not a zero-latency or perfect-cadence result.

Metal HUD memory values rose from 20.78 to 23.91 MB graphics memory and from 169.40 to 182.66 MB process memory over the 60-second run. That short observation cannot distinguish warm-up/allocation from a leak. The harness has no drawable-starvation counter; the measured evidence is successful Present/fence results and HUD-reported present intervals, not a 30-minute stability claim.

The full per-presentation timeline correlates frame kind, source and G production QPCs, fence signal/completion observation, Present QPCs, schedule deadlines, and monitor/refresh counter deltas in [the 30→60 CSV](../../experiments/dxgi_public_probe/evidence/step8c-30to60-timeline.csv).

## 60→120 result

**NOT RUN — suitable output unavailable.** CoreGraphics reported only 60 Hz modes on the available built-in display. We did not evaluate 60→120 on that output.

## Short lifecycle validation

A separate 10-second 30→60 run completed with 300 source frames, 299 G frames, 599 presents, and final monitor/refresh counts of 599. It then:

- resized the window and swapchain to 1024×576 and reacquired all three buffers;
- switched windowed → fullscreen → windowed, verifying `GetFullscreenState` after both changes;
- restored 960×540 and reacquired all buffers again;
- transferred focus from the main harness window to a second helper window, observed main-window focus loss, then restored focus and verified recovery.

Every `ResizeBuffers`, `SetFullscreenState`, `GetFullscreenState`, focus check, and final run result succeeded. The focus exercise uses two Win32 windows in the test process; it does not claim an OS-level alt-tab test. The checks were short and cooperative, not a repeated resize/recreation stress test. The filtered calls and results are in [the lifecycle evidence file](../../experiments/dxgi_public_probe/evidence/step8c-lifecycle.txt).

## Boundary, decision, and next step

| Success criterion | Result |
|---|---|
| Actual Highball GPTK 4 / D3DMetal 4 runtime verified in process | **PASS** — public manifest/Info.plist plus loader traces |
| Public DXGI swapchain and D3D12 backbuffers accessed | **PASS** — documented COM interfaces, `GetBuffer`, descriptors, identities |
| GPU-only work on the public D3D12 path | **PASS** — copies, clears, barriers, direct queue and fences |
| Extra synthetic frame between source frames | **PASS** — 1,799 G frames; monitor and refresh counts advanced after every output |
| 30→60 on supported hardware output | **PASS with timing caveats** — HUD median/p95/p99 16.67 ms, maximum 50 ms; deadline lateness and Present-gap tail are reported above |
| 60→120 | **NOT RUN** — no 120-Hz output available |
| Short resize/fullscreen/focus recovery | **PASS** — one cooperative lifecycle sequence |
| Transparent attachment to an unmodified game | **NOT TESTED** |
| 30-minute soak / long-term memory or starvation behavior | **NOT RUN** |

**Recommendation: proceed to a new Step 8D that tests only public transparent DXGI attachment/wrapping for an unmodified Wine application.** The cooperative GPTK 4 result justifies that narrowly scoped follow-up; it does not justify a D3D12 inference backend or product integration. Keep the current Metal and D3D12 resource contracts separate, leave RIFE disconnected, and retain all public-interface boundaries. Stop here until Step 8D is separately authorized.

## Independent boundary and evidence review

An independent GPT-6 Luna Max reviewer audited this report, the harness changes, and the retained runtime and presentation evidence without inspecting D3DMetal private implementation material. The requested Extra High setting was unavailable in the subagent controls, so Max was used as the closest supported Luna effort.

The reviewer retained the **narrow cooperative synthetic-presentation PASS**. It confirmed the timeline and HUD sample counts and agreed that the public DXGI monitor counters support the reported cadence, while cautioning that neither those counters nor the HUD establish exact pixel identity. The report now says “HUD-reported present intervals/cadence” rather than claiming on-glass pixel verification. The reviewer also confirmed that `D3DM_MTL4=0` is disclosed: this validates the GPTK 4.0b2 runtime, but not its Metal 4 backend mode.

One evidence limitation remains explicit: full raw diagnostic logs were kept under `/tmp` and are referenced by hashes, rather than copied into the repository. The retained timeline CSV, HUD log, lifecycle excerpt, and runtime-load excerpt were sufficient for the reviewer’s narrow conclusion, but the full raw run is less independently replayable.
