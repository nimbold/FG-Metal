# Step 11B.1 harness differential

Read-only comparison of the pinned repository smoke source and the Step 11B controlled clear harness. No harness or DXVK source was changed, and no display capture or pixel readback was used.

## Identity

- Repository: `/tmp/fgmetal-dxvk-macos-audit-20261004`, `HEAD=8d348236e14a3db25ffbe528a83010b3dd69a3ef`; working tree was clean at inspection.
- Repository smoke SHA-256: `5e169c47609c90cc1de0e820e4f81a662562eb1d47b70fe52a7ce72032c9d6dd` (`tools/smoke/d3d11_smoke.c`).
- Step 11B source SHA-256: `ba861cdd5e493b2c053cb1b5b5309cf780d585dd5141205ddd7aa05302564d85` (`/tmp/fgmetal-step11b/app/d3d11_clear_window.cpp`). This source is outside the pinned repository, so its Git identity is not established here.
- This is a source comparison only; it does not establish which binaries or environment values were used in prior runs, nor a visual result for the repository smoke.

## Requested differences

| Property | Repository `d3d11_smoke.c` | Step 11B controlled harness | Black-output relevance |
|---|---|---|---|
| Swap-chain format | `DXGI_FORMAT_B8G8R8A8_UNORM` | `DXGI_FORMAT_R8G8B8A8_UNORM` | **Rank 1; strongest controlled hypothesis.** It changes the app backbuffer's DXGI/Vulkan format while the observed Presenter WSI target is BGRA8. Plausible format/view/swizzle/sample-path issue, but source comparison alone does not prove causality. |
| Size | 320×240 swap-chain buffer | 640×360 initially; explicit resize to 800×450 after frame 90 unless `FG_AUDIT_SKIP_RESIZE=1` | Low. Clear is extent-independent; size could expose allocation, extent, or resize-specific bugs. The controlled harness adds a resize/recreation path absent from the smoke. |
| Swap effect | `DXGI_SWAP_EFFECT_DISCARD` | `DXGI_SWAP_EFFECT_DISCARD` | No difference. |
| Buffer count | 2 | 2 | No difference. |
| Device flags | `0` | `0` | No difference. Both request hardware D3D11 with no explicit feature-level list. The controlled harness supplies an output pointer for the selected feature level; the smoke passes null, which should not change device selection. |
| Clear target and operation | Get buffer 0, create default RTV, bind it with `OMSetRenderTargets`, clear it, then present | Get buffer 0, create default RTV, clear it, then present; it does not bind the RTV with `OMSetRenderTargets` | Low-to-moderate implementation hypothesis. D3D11 `ClearRenderTargetView` names its target explicitly and does not require that RTV to be currently bound, so the calls should clear the same resource. Still, the extra bind in the smoke is a meaningful one-line differential worth keeping controlled if the format A/B does not explain the result. |
| RTV/backbuffer lifetime | Holds the `GetBuffer(0)` texture reference until shutdown; retains one RTV | Releases the texture reference immediately after RTV creation; releases/recreates RTV after resize | Low. Creating an RTV retains its resource; releasing the separate texture interface should not change the resource being cleared. Resize makes the controlled case exercise a new allocation after frame 90. |
| Window style | `WS_OVERLAPPEDWINDOW`; outer size is 320×240; ANSI class/window; shown with `SW_SHOW` | `WS_OVERLAPPEDWINDOW`; uses `AdjustWindowRectEx` to request a 640×360 client area; Unicode; uses startup show command, and framegen mode additionally forces show/foreground | Low. The style matches. Size, show state, focus, or occlusion can affect what a human sees, but there is no source evidence of a hidden window in framegen mode; the harness logs visibility/iconic/foreground state. |
| Message pump | Presents first, then drains messages with `PeekMessageA`/`DispatchMessageA`; no `TranslateMessage` | Drains messages with `PeekMessageW`, `TranslateMessage`, `DispatchMessageW` before rendering/presenting | Very low. Both pump messages. The main behavioral addition is controlled `WM_SIZE` handling, which invokes `ResizeBuffers`; the smoke does not resize. |
| Present interval | `Present(1, 0)` | `Present(1, 0)` | No difference. Both request interval 1 and flags 0. |
| Color sequence | Per-frame arithmetic RGB values: `{i%256, 3i%256, 7i%256}/255`, alpha 1; frame 0 is black and the first few frames are very dark | Four fixed colors (blue, red, green, purple), each held 60 frames; alpha 1. In framegen mode it paces to 30 FPS, so each color lasts about two seconds | Low as a rendering cause; moderate as a visual-detection confound. The smoke begins with black and changes continuously, whereas the controlled harness begins blue and makes discrete changes. The patterns are materially different, but neither predicts a persistently black image. The current four colors are less intense than the requested future red/green/blue/white A/B pattern. |

## Additional behavioral differences

- The repository smoke defaults to 300 frames and accepts a frame-count argument; with interval 1 this is roughly five seconds at 60 Hz, but actual duration depends on runtime. The controlled harness defaults to 10 seconds when `FG_AUDIT_ENABLE` is absent, and to 65 seconds when present; `FG_AUDIT_RUN_SECONDS` can select 1–1800 seconds in that mode. The prior 30/60-second invocation cannot be reconstructed from source alone.
- The controlled harness has environment-controlled static-color, resize-skip, lifecycle, and framegen-toggle modes. `FG_AUDIT_STATIC_TEXTURE=1` pins the first (blue) clear color; it does not select black. Lifecycle mode minimizes/restores, attempts fullscreen enter/exit, and recreates the swap chain at later times. These behaviors make its execution profile broader than the smoke and should be recorded for any A/B run.
- The controlled harness queries `IDXGISwapChain3` and samples the current buffer index and `GetBuffer(0)` for logging; it checks `Present` failures and records per-frame state. The smoke does not query these interfaces and ignores the `Present` HRESULT. These are observation changes; no clear-source difference is apparent from the code.
- The controlled harness writes CSV logs and flushes periodically, and its framegen mode paces to 30 FPS. The repository smoke has no pacing beyond interval-1 presentation. This changes CPU/present cadence, not the requested clear or swap-chain format.
- The controlled harness explicitly requests a 60/1 refresh rate; the repository smoke leaves refresh rate zero-initialized. Both are windowed. No causal relevance is established for this difference.

## Likelihood order and conclusion

1. **BGRA8 versus RGBA8 source format** is the highest-priority differential, matching the requested first A/B variable and directly changing the format presented to DXVK's source-sampling path. Run the exact one-variable format A/B before attributing causality.
2. **Clear pattern** is the next most relevant to whether a human can quickly detect changing output. It cannot explain a confirmed black frame sequence, but it can make a brief visual inspection misleading.
3. **Binding RTV before clear** is a useful low-cost control if the format result is inconclusive. D3D11 semantics say the explicit RTV argument identifies the resource to clear, so lack of an OM bind is not by itself evidence of a bug.
4. **Resize/recreation and dimensions** are secondary candidates because only the controlled harness exercises them and its RTV is replaced after `ResizeBuffers`. They are less likely to explain black output before the first resize.
5. Window focus/show behavior, message-pump order, logging/queries, refresh-rate field, texture-interface lifetime, and frame pacing are lower-likelihood based on this source comparison.

The source confirms the format differential and several test-profile differences, but does not establish that BGRA8 is causal or that the repository smoke rendered visibly. The prescribed exact BGRA8/RGBA8 A/B remains the decisive next test.
