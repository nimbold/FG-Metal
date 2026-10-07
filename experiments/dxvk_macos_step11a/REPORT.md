# Step 11A — DXVK-MacOS Framegen Feasibility, WSI Ownership, and Public Metal Interop

**Audit date:** 2026-10-04  
**Result:** **FAIL for the tested pinned x86_64 Wine/D3D11 runtime lane; architecture remains unproven, not disproven.**  
**Scope:** source and binary audit, unmodified x86_64 DXVK build, isolated Wine runtime, D3D11 baseline, public Vulkan WSI/Metal probes. No Framegen code, RIFE, DXMT, or Highball changes.

## Decision

The pinned DXVK source separates D3D11 application backbuffers and DXGI accounting from the Vulkan `Presenter` swapchain. That makes renderer-owned extra WSI presents **plausible in principle** and gives this path a meaningful architectural distinction from the DXMT investigation. It does not prove a safe extra-present scheduler: the current Presenter is designed around one acquire/present progression, and D3D11 passes a shared app-facing frame-latency signal into it. A safe design must separate application and WSI identities, application completion signaling, and application-facing frame-statistics feedback, while serializing all WSI ownership.

The exact x86_64 D3D11 baseline did not render reliably. The Wine-side MoltenVK startup succeeded, DXVK created a 640×360 FIFO swapchain, and the application recorded successful `Present` HRESULTs in a short run. A captured baseline recheck then failed while compiling DXVK’s built-in vertex pipeline: MoltenVK reported `DrawIndex is not supported in MSL`, followed by `VK_ERROR_INITIALIZATION_FAILED` and a DXVK command-thread failure. Resize, a healthy sustained render, fullscreen, and clean renderer shutdown therefore remain unverified. Phase 8/9 were not attempted on this unhealthy baseline.

Separate native Vulkan probes against the same MoltenVK dylib file in the tested Wine bundle demonstrated FIFO WSI timing, `VK_EXT_metal_objects` texture export, and public shared-event synchronization. These tests ran outside Wine/winevulkan. They do not establish D3D11/DXVK integration or application invariants. The version metadata is internally inconsistent: Wine/MoltenVK startup reports 1.4.1 / Vulkan 1.4.334 and the Wine `libvulkan.dylib` dependency metadata names MoltenVK current version 1.4.1, while `VkPhysicalDeviceDriverProperties` from the same file reports driverInfo 1.4.3. No MoltenVK source revision is pinned by DXVK-MacOS.

**Recommendation:** do not integrate Framegen yet. First resolve the exact runtime’s D3D11 shader-translation failure and re-establish a stable unmodified baseline. Then implement only a bounded synthetic WSI/application-invariant probe with separate application and WSI IDs, app-only completion and timing feedback, and a single presentation owner. Do not begin RIFE work based on this audit.

Status labels below mean **VERIFIED** (observed in the stated environment), **SUPPORTED INFERENCE** (source-backed but not exercised end to end), or **NOT TESTED**.

## 1. Exact source pin and environment

| Item | Recorded value |
|---|---|
| Repository | `https://github.com/metalsharp/DXVK-MacOS.git` |
| Fetched `main` at audit start and rechecked | `8d348236e14a3db25ffbe528a83010b3dd69a3ef` |
| Pinned checkout | detached at that exact SHA; checkout status clean |
| Commit date / subject | `2026-09-21T01:04:50-06:00` / `P2: Phase 2 complete — D3D9/D3D8 device-level support verified` |
| Host | Apple M3 MacBook Air (Mac15,12), macOS 27.0.1 build 26A434, arm64 host; x86_64 Wine/probes ran under Rosetta |
| Toolchain | Xcode 27.0 build 27A266a; Apple Clang 21.0.0; Meson 1.12.1; Ninja 1.13.2; MinGW-w64 GCC 16.2.0 |
| Display | `NSScreen.maximumFramesPerSecond = 60`; System Information listed 2560×1664. The Xcode trace exported a 2940×1912 backing surface, so its rows were not used to infer the active app window's display resolution. |

Recursive submodules at the pinned checkout:

| Path | Revision |
|---|---|
| `include/native/directx` | `9df86f2341616ef1888ae59919feaa6d4fad693d` |
| `include/spirv` | `04f10f650d514df88b76d25e83db360142c7b174` |
| `include/vulkan` | `8864cdc896bbc2a9b6eb36b3218fc9ef57908d77` (Vulkan-Headers 1.4.350) |
| `subprojects/dxbc-spirv` | `bf14419e5fa7eacb817b7b632f03cb61d61bbad7` |
| `subprojects/dxbc-spirv/submodules/spirv_headers` | `c8ad050fcb29e42a2f57d9f59e97488f465c436d` |
| `subprojects/libdisplay-info` | `275e6459c7ab1ddd4b125f28d0440716e4888078` |

The repository’s documented Wine route uses MetalSharp Wine 11.17. I extracted the official `metalsharp-runtime.tar.zst` payload into `/tmp/fgmetal-dxvk-macos-audit-run/runtime/wine`; its SHA-256 is `5bdf6637ac9f6064280c0f663e517d8772b70ecc0a51935650d3431b9a2864f8` (434,826,051 bytes), matching the current v0.76 bundle manifest. Runtime identity was `wine-11.17 (MetalSharp 1.0)`. A fresh disposable wow64 prefix is at `/tmp/fgmetal-dxvk-macos-audit-run/prefix`; `wine cmd /c ver` identified Windows 10.0.19045. Runtime extraction occupied 2.2 GiB. Neither an installed Highball runtime nor a normal game prefix was used.

The repository’s `patches/wine-vulkan-portability.patch` is the documented public portability change. No Wine source or installed runtime was edited for this test. The Wine process enumerated the Apple M3 through the bundled Vulkan/MoltenVK route; the runtime reported `VK_KHR_portability_subset` and `VK_KHR_portability_enumeration`.

Runtime Vulkan payload SHA-256 values: `libvulkan.dylib` `1d57a14b4a0d380420ab566884018fd83ba57381aa44e464f79472a77273b6bc`; `winevulkan.so` `3e70ba096e6a3027f2276976535ac39e47022fdf7e6e54ddec6556cbfd82baef`; x86_64 ICD manifest `0dcbf7707cc0a347d0ba2941e835e5e92709919370a1bb0fc252e8dc4d95d322`; bundle-level ICD manifest `578ff08cd0d8734619357541771a5abc9c3470ca300030219a971a9e9dbbe466`.

The pinned repository’s MoltenVK version notes are inconsistent: `docs/PHASE2-REPORT.md` says the Wine x86_64 slice is 1.4.1 / Vulkan 1.4.334 and the arm64 slice is 1.4.3 / Vulkan 1.4.357, while `docs/LAB-MATRIX.md` and `docs/BASELINES.md` describe the active Wine bundle as 1.4.3. The M3 Wine startup matched the former x86_64 report. The x86_64 direct `driverInfo` result still reported 1.4.3, so that field is not a reliable source-version pin here.

## 2. License and provenance audit

| Component | Observed license / provenance | Audit note |
|---|---|---|
| DXVK-MacOS root | zlib/libpng; root `LICENSE` SHA-256 `a5cb1a6ded7d2d7e92d550ba28edd21be2d1d4044662b399887351023e30ce64` | **VERIFIED.** Exact hash match with upstream DXVK v3.1 `LICENSE`. |
| `dxbc-spirv` | MIT; `LICENSE` SHA-256 `0f8a5e687bea942a30a8e39b9915294fbe44eca866e71307a9b7083ee0e69ad8` | Nested SPIRV-Headers is Apache-2.0/MIT; SPDX documentation includes CC-BY-4.0 material. |
| Vulkan-Headers | Apache-2.0 OR MIT | License files are present in the pinned submodule. |
| SPIRV-Headers | Apache-2.0 OR MIT | Some registry/documentation material carries CC-BY-4.0 notices. |
| `libdisplay-info` | MIT; `LICENSE` SHA-256 `15b396244e58830c5614b9394f4deccfe684970cd507f299383ab57ad339eedd` | Test data includes a separate CC-BY-4.0 notice. |
| MinGW DirectX headers | LGPL-2.1+ with the header/interface use clarification in its README | Its README refers to `COPYING.LGPLv2.1` and `COPYING.MinGW-w64-runtime.txt`; both are absent. `COPYING.MinGW-w64.txt` is present but is the MinGW-w64 notice. Preserve the header README and resolve notice completeness before redistributing those sources/binaries. |
| Vendored OpenVR headers | BSD-3-Clause | Header license file present. |
| Wine portability patch/runtime | Wine-derived runtime; Wine is LGPL-2.1-or-later | DXVK-MacOS contains a portability patch but does not pin a Wine source commit or runtime binary. The extracted runtime subtree had no files named license/copying/notice. Keep Wine provenance and notices separate. |
| MoltenVK | Upstream project Apache-2.0 | Not a DXVK-MacOS submodule or source pin. The tested MetalSharp runtime dylib SHA-256 is `8249d81ebf2d46f82b16ca166c2e5cca5d76d91d0a412cd6d3db1aaa6e8430bf`; its source commit was not identified. The extracted runtime subtree had no separate license/notice file. |

Do not flatten these licenses. Future Framegen core remains Apache-2.0; edits in DXVK-MacOS remain under the renderer’s license; any MoltenVK changes remain under MoltenVK’s license; Wine and vendored components keep their own notices.

## 3. Unmodified build

Followed the README’s x86_64 Meson cross-build lane. `glslangValidator` was initially missing; I installed Homebrew `glslang` 16.6.0 (Homebrew also installed SPIRV-Tools 1.4.363.0 and SPIRV-Headers 1.4.363.0). The repository was not modified. The documented `dxbc-spirv-moltenvk.patch` was deliberately not applied for this unmodified build.

The focused D3D11/DXGI targets built successfully:

```sh
meson setup /tmp/fgmetal-dxvk-macos-build-unmodified \
  --cross-file build-win64.txt --buildtype release \
  --prefix /tmp/fgmetal-dxvk-macos-stage-unmodified/x86_64
ninja -C /tmp/fgmetal-dxvk-macos-build-unmodified \
  src/d3d11/d3d11.dll src/dxgi/dxgi.dll
```

The targeted Ninja build completed 7/7 steps with no target-build warnings recorded. Artifacts:

| Artifact | Format | SHA-256 |
|---|---|---|
| `d3d11.dll` | PE32+ x86_64 | `6940b883fb4bc1b744fb7ca3aa6a486303eaf180c9fe85c03ed7cb175057bf28` |
| `dxgi.dll` | PE32+ x86_64 | `17cbe8e84906ce9657493b1301e712cfc8123413034666fd9387dfe1bbf5b33c` |

**Storage retention reconciliation (2026-10-07):** the completed build tree named by the historical commands above was artifactized and retired under build-manifest ID `4e761353dcbfaaf9b676465d66f35a4d4070cf0b0ef2481971d80fa4c17f580f` ([manifest](../storage/build-manifests/legacy-fgmetal-dxvk-macos-build-unmodified-b1351348.json)). The retained binary identities are the SHA-256 values above, stored at `/Users/nima/Library/Caches/FGMetal/artifacts/sha256/6940b883fb4bc1b744fb7ca3aa6a486303eaf180c9fe85c03ed7cb175057bf28/d3d11.dll` and `/Users/nima/Library/Caches/FGMetal/artifacts/sha256/17cbe8e84906ce9657493b1301e712cfc8123413034666fd9387dfe1bbf5b33c/dxgi.dll`. The historical command paths are not live build directories.

The full default compile was also attempted and stopped in D3D9 because the pinned `dxbc-spirv` submodule does not expose `deAliasedSamplers` until the repository-documented compatibility patch is applied. An unrelated existing `%s` format warning also appeared while compiling `tools/probe/probe.c`. Those broad-build diagnostics did not block the requested x64 D3D11/DXGI targets. Build and stage directories occupied 60 MiB and 40 MiB respectively. No i386 build was run.

## 4. Isolated D3D11 baseline

The controlled app was copied from `tools/dxmt/test_apps/d3d11_clear_window.cpp` into `/tmp`, with only its environment-variable prefix changed from `DXMT_FRAMEGEN` to `FG_AUDIT`. The repository test source was not edited. The x64 test executable SHA-256 is `18509edad2e1ae8d49941bd1868dd384d03cf693cc66b5542e7bba1611e21163`. The app requested `DXGI_FORMAT_R8G8B8A8_UNORM`, 640×360, two application buffers, `DXGI_SWAP_EFFECT_DISCARD`, windowed mode, and no frame-latency-waitable-object flag.

**VERIFIED:** Wine loaded pinned DXVK `v3.1-macos1.0-48-g8d348236` and MoltenVK on Apple M3. DXVK selected D3D11 feature level 11.0 and created a separate 640×360 `VK_FORMAT_B8G8R8A8_UNORM` FIFO WSI swapchain with three images. A short app CSV recorded 24 `S_OK` Presents, `GetCurrentBackBufferIndex` zero before/after each, `GetBuffer(0)` returning `S_OK`, and the window visible/foreground. This proves the call path reaches Present and returns success; it does not prove that frames were rendered correctly or that the baseline was stable.

The captured recheck log ends with:

```text
[mvk-error] SPIR-V to MSL conversion error: DrawIndex is not supported in MSL.
[mvk-error] VK_ERROR_INITIALIZATION_FAILED: Vertex shader function could not be compiled into pipeline.
err:   Exception on CS thread!
err:   Failed to create built-in graphics pipeline: VK_ERROR_INITIALIZATION_FAILED
```

The generated DXVK built-in vertex SPIR-V was also inspected: it decorates `VertexIndex`; the exact reason MoltenVK’s translator reports `DrawIndex` was not root-caused. No renderer workaround was attempted. The app was stopped after the render/CS failure rather than counted as a clean stable run.

| Baseline check | Result |
|---|---|
| DXVK loaded / D3D11 FL 11.0 | **VERIFIED** |
| MoltenVK loaded / swapchain created / FIFO mode | **VERIFIED** |
| Present API path and short `S_OK` sample | **VERIFIED**, but not healthy-render evidence |
| Stable visible rendering | **FAIL** — pipeline creation error above |
| Resize behavior | **NOT TESTED** |
| Fullscreen transition | **NOT TESTED** |
| Frame-latency waitable-object semantics | **NOT TESTED**; test app did not request the flag |
| `GetBuffer(0)` pointer identity/progression | **NOT TESTED**; only `S_OK` was recorded |
| Clean renderer shutdown after sustained rendering | **NOT TESTED** |

## 5. Vulkan capabilities on the M3 runtime

The capability probe used an x86_64 process and the exact `libMoltenVK.1.dylib` file extracted from the Wine runtime. The same file hash appeared at both runtime paths. Wine’s MoltenVK startup log reported 1.4.1 / Vulkan 1.4.334, and the Wine `libvulkan.dylib` dependency metadata declares MoltenVK current version 1.4.1. The direct probe’s driver-properties structure instead returned `driverInfo=1.4.3` and physical-device API `1.3.357`. Treat the binary hash as exact and the source/build version as **UNVERIFIED**; this version-field mismatch needs resolution before release claims.

| Capability | Runtime observation |
|---|---|
| Physical device | Apple M3; vendor `0x106b`, device `0x1b000209`; driver ID 14, MoltenVK |
| Queue family | Family 0, graphics/compute/transfer, surface-present supported |
| `VK_KHR_swapchain` | Spec version 70 |
| `VK_KHR_present_id` / `VK_KHR_present_wait` | Spec version 1 each; Wine DXVK feature query reports `presentId=1`, `presentWait=1` |
| `VK_KHR_present_id2` / `VK_KHR_present_wait2` | Spec version 1 each; Wine DXVK reports `presentId2=0`, `presentWait2=0` |
| `VK_EXT_swapchain_maintenance1` / `VK_KHR_swapchain_maintenance1` | Spec version 1 each; DXVK reports maintenance feature `1` |
| `VK_GOOGLE_display_timing` | Spec version 1; direct query calls succeed |
| `VK_EXT_metal_objects` | Device extension spec version 2 |
| `VK_KHR_portability_subset` | Spec version 1 |
| `VK_EXT_present_timing` | Not supported (`specVersion=0`; DXVK logs `presentTiming=0`) |
| Present modes | `FIFO_KHR`, `IMMEDIATE_KHR`; no MAILBOX or FIFO_RELAXED |
| Surface image counts | Minimum 2, maximum 3; tested FIFO swapchain created 3 images |
| Surface extent / format | 640×360; BGRA8 UNORM / SRGB nonlinear among advertised formats |
| Refresh | `vkGetRefreshCycleDurationGOOGLE` returned 16,666,666 ns, consistent with 60 Hz |

The Wine startup log lists the required extensions, but the feature fields above are not all enabled by Wine/DXVK. The capability probe printed its results before exiting with status 139 during teardown; that cleanup fault was not investigated. Capability/export operations had already returned success.

## 6. DXVK Present call graph and ownership

Pinned source references below point to the exact audited commit:

1. Application `IDXGISwapChain::Present` enters `D3D11SwapChain::Present` and calls `PresentImage` (`src/d3d11/d3d11_swapchain.cpp:260-304, 411-480`).
2. `PresentImage` ends/flushes app rendering, reports app present-begin to the latency tracker, then calls `Presenter::acquireNextImage` (`:417-430`).
3. The acquired `backBuffer` is a `DxvkImage` from the **Presenter’s Vulkan WSI swapchain**. The application source is separately obtained as `GetBackBufferView()` (`:501-504`).
4. The command-stream job blits the application source into the acquired WSI image, synchronizes WSI, flushes the command list, and calls `DxvkDevice::presentImage` (`:517-529`; `src/dxvk/dxvk_device.cpp:604-622`).
5. `DxvkSubmissionQueue` serializes command submission and present work under `m_mutexQueue` (`src/dxvk/dxvk_queue.cpp:152-184`). `Presenter::presentImage` submits `vkQueuePresentKHR`, updates its WSI ring, and pre-acquires the next WSI image (`src/dxvk/dxvk_presenter.cpp:171-335`).
6. After scheduling the application frame, D3D11 rotates storage among its app backbuffers and flushes its command stream (`src/d3d11/d3d11_swapchain.cpp:553-572`). `D3D11SwapChain::GetImageIndex()` returns constant 0 (`:155-157`) for this interface; the test observed 0 throughout. The standalone `DxgiSwapChain` implementation instead delegates `GetCurrentBackBufferIndex()` to its Presenter (`src/dxgi/dxgi_swapchain.cpp:126`).

This confirms distinct application source storage and renderer WSI output storage. The successful D3D11 `PresentImage` path increments `m_frameId` after a successful WSI acquire and before the asynchronous command-stream presentation (`d3d11_swapchain.cpp:480`). That ID drives `GetLastPresentCount`, latency calls/callbacks, `DxvkDevice::presentImage`, Vulkan present IDs, Presenter frame tracking and completion (`d3d11_swapchain.cpp:343-346, 425-480, 713-738`; `dxvk_presenter.cpp:180-255, 295-353, 1998-2048`).

There is a second app-facing coupling beyond the latency tracker. `D3D11SwapChain::CreatePresenter` gives the Presenter `m_frameLatencySignal` (`d3d11_swapchain.cpp:576-595`), and `SyncFrameLatency` registers application frame-latency callbacks against the same signal (`:712-721`). The queue completion path calls `Presenter::signalFrame(frameId, tracker)` for every queued presentation even when `tracker` is null (`src/dxvk/dxvk_queue.cpp:283-288`). `signalFrame` and the Presenter frame thread update `m_lastSignaled`/`m_lastCompleted` and may signal the shared `m_signal` (`src/dxvk/dxvk_presenter.cpp:337-352, 2036-2048`). Also, `D3D11SwapChain::GetFrameStatistics` reads the shared Presenter timing feedback’s `frameId` and `presentTime` (`d3d11_swapchain.cpp:349-358`; `dxvk_presenter.cpp:583-585, 1559-1567, 1676-1682`). Therefore, a null latency tracker and a distinct WSI ID alone do **not** preserve DXGI semantics.

### Central ownership gate

**SUPPORTED INFERENCE, not runtime proof:** Vulkan WSI image state is separate from D3D11 backbuffer progression, so the architecture does not inherently require one DXGI `Present` per `vkQueuePresentKHR`. A renderer-owned scheduler could conceptually repeat/present generated output using the WSI swapchain while app-facing counts and buffer rotation advance only for app calls.

**Not safe to call directly today:** the existing Presenter waits on `m_presentPending`, holds one acquire/present progression, couples its frame ID to presentation/timing/lifetime bookkeeping, and uses the same serialized graphics queue. Simply invoking its present method for G frames would contaminate DXGI-facing counts, the shared `CallbackFence`, and `GetFrameStatistics`; a null tracker does not suppress the queue’s `signalFrame` call. It could also race acquisition or backbuffer reuse. Vulkan requires external synchronization for host access to a queue. A future design needs one presentation owner, distinct `appFrameId` and monotonically unique `wsiPresentId`, plus separate generated-frame completion state. G frames must not call the app-facing `Presenter::signalFrame` path, advance the app completion watermark, signal `m_frameLatencySignal`, or become the newest DXGI timing feedback. `GetFrameStatistics` must expose app-present feedback only. The app history copy must be ordered before D3D11 buffer-storage rotation. A dedicated worker must not make the game thread wait on WSI acquisition. Source-level evidence does not yet prove these properties.

**Phase 8 / 9:** **NOT TESTED.** No internal DXVK WSI presents were added. The baseline failure prevented a meaningful alternating source/G run and application-invariant comparison. A future invariant probe must include app Present count/HRESULTs, `GetLastPresentCount`, backbuffer index and buffer progression, frame-latency waitable behavior where enabled, `GetFrameStatistics`, resize, and shutdown; generated frames must not alter those app-facing observations. The test app here did not enable the waitable-object flag. The 120-present standalone Vulkan probe below is not that proof: it had no D3D11 application, no 30-Hz source Presents, and no DXVK-owned worker.

**Independent adversarial review:** the distinct D3D11 source and Presenter WSI images plausibly avoid the specific DXMT ownership coupling, but no implementation or runtime evidence proves the extra-present lane. The shared completion fence, timing feedback, Presenter pending-acquire state, and queue serialization make a naïve call unsafe. Separate application and WSI IDs alone are insufficient; generated completion and timing must also be isolated from DXGI. The direct Vulkan extension probes do not repair the Wine D3D11 baseline failure. Recommendation remains: do not integrate Framegen until the renderer baseline is healthy, then require the alternating WSI and unchanged-DXGI invariant proof.

## 7. Public Vulkan timing and MoltenVK risk

The standalone probe presented 120 simple source/synthetic-color frames through a 3-image FIFO WSI swapchain. In the isolated `VK_GOOGLE_display_timing` path, MoltenVK returned 60 past timing entries over about 1.97 s and a 16,666,666 ns refresh period. For IDs 58–117, the 59 actual-present intervals averaged 16,666,471 ns (min 16,666,375; max 16,666,583). With 60-Hz desired times, `actualPresentTime - desiredPresentTime` averaged -481,849 ns (range -487,630 to -476,127). This is useful display-timing feedback from the same dylib in a standalone process, with a consistent ~0.48 ms early offset. Since the Vulkan contract defines `desiredPresentTime` as an earliest-present bound, that negative delta needs clock-domain/calibration investigation before relying on desired-time scheduling. It is not evidence of an integrated DXVK scheduler.

A separate probe chained Google timing with KHR present IDs, present-wait and swapchain-maintenance present fences. Although all 120 `vkQueuePresentKHR` calls returned success, only 60 timing records were returned and the measured intervals/deltas were inconsistent with the FIFO refresh; present-fence and present-wait completion averaged about 1.12 ms after queueing. The combination was not isolated further. Treat present-wait/fences as resource-retirement signals, not display timestamps, until validated for this runtime. The isolated Google-only result is the usable timing observation.

MoltenVK’s public Vulkan timing path can therefore be used without CAMetalDisplayLink in a direct probe. DXVK-MacOS currently uses `VK_EXT_present_timing` for Presenter scheduling/feedback; that extension is absent and its log says `Timing: no`. The audited DXVK source does not implement `VK_GOOGLE_display_timing`. A new public Google-timing integration would be required.

An Xcode Metal System Trace was captured for the standalone probe. It recorded 120 Core Animation present requests, but the exported system display-surface rows (51 rows, roughly 33.33 ms durations, Direct-to-Display false) could not be reliably associated with the 640×360 probe layer and did not corroborate the Vulkan cadence. Do not treat Xcode trace, direct-to-display status, or the requested desired-time values as verified for DXVK’s Wine window.

Upstream MoltenVK issue [#2791](https://github.com/KhronosGroup/MoltenVK/issues/2791) remains open. Its report reproduces a drawable lifetime race on MoltenVK 1.4.1 and 1.4.2 on an Intel/AMD Mac, three-image IMMEDIATE mode and sustained uncapped ~100–400 fps; it reports no reproduction when display-paced at 60 fps. The tested runtime’s startup log names 1.4.1, so the version overlaps, but this M3 FIFO 60-Hz target is outside the issue’s reproduced configuration. The issue also says swapchain-maintenance present fences did not prevent its race. Current MoltenVK `main` rechecked at `52aa21f54d7a84c5c441fc26359692b0980b384c`; no landed fix was identified. This is a targeted risk, not evidence that FIFO 60 necessarily fails.

## 8. `VK_EXT_metal_objects` and history design

The same hashed bundled dylib successfully:

- exported its `MTLDevice` and `MTLCommandQueue` when export intent was supplied at instance creation; both corresponded to the Apple M3 device;
- exported an intentionally exportable 256×144, single-sample `VK_FORMAT_B8G8R8A8_UNORM` image as an `MTLTexture`, matching the same device, dimensions, `MTLPixelFormatBGRA8Unorm` (80), sample count 1, and stable repeated-export identity;
- exported an `MTLSharedEvent` from a Vulkan timeline semaphore. Metal→Vulkan value 7 and Vulkan→Metal value 9 both worked. Importing that event into a second Vulkan semaphore and signaling it from Metal at value 11 let `vkWaitSemaphores` complete at 11.

No CPU pixel readback was used. These tests ran directly against the dylib, not through Wine/winevulkan. They did not export an arbitrary DXVK game image, import a Metal-written texture for Vulkan sampling, test a `VkEvent`, or prove shared queue ordering inside DXVK.

The public extension requires export intent when each Vulkan object is created. The device/queue intent belongs in `VkInstanceCreateInfo`; texture intent in the image’s `VkImageCreateInfo` (and view intent if exporting a view); shared-event intent in `VkSemaphoreCreateInfo` or `VkEventCreateInfo`. Existing arbitrary images cannot be retroactively made exportable. See the [Khronos extension design](https://docs.vulkan.org/features/latest/features/proposals/VK_EXT_metal_objects.html).

**Recommended eventual history design:** start with A, a Framegen-owned Vulkan history image created with Metal-texture export intent; copy the app source into it before app storage rotates, and keep it immutable until Framegen finishes. The synthetic path should remain Vulkan compute→Vulkan WSI. For later Metal/RIFE inference, export a selected history/output `MTLTexture` and an explicit shared event. Vulkan→Metal→Vulkan image-layout/access ownership still needs an end-to-end GPU synchronization probe; the event export alone does not prove it.

### SDR format boundary

For the controlled case, source `DXGI_FORMAT_R8G8B8A8_UNORM` maps to a Vulkan RGBA8 source image. DXVK’s D3D11 Presenter WSI output was `VK_FORMAT_B8G8R8A8_UNORM`; the Metal-object probe maps the latter to `MTLPixelFormatBGRA8Unorm`. Re-enter future generated frames at the Presenter blit/composition input, before the final WSI image, so DXVK applies output format conversion/scaling once. Restrict the first implementation to SDR, single-sample RGBA8/BGRA8 and bypass unsupported formats. HDR was not tested.

## 9. Threading, lifecycle, and packaging plan (design only)

**One presentation owner:** serialize `vkAcquireNextImageKHR`, queue submissions, `vkQueuePresentKHR`, swapchain recreation and present-fence state through one renderer-owned owner/queue abstraction. Do not allow a second thread to issue raw presents against DXVK’s graphics queue or mutate Presenter’s acquire ring. Use asynchronous request queues so the game thread does not block waiting for a drawable/acquire during generated frames.

**Lifecycle plan:** on resize/fullscreen change, stop accepting old-generation source/G jobs; finish or discard queued G work; wait for submissions and presentation-resource retirement; release history references; then recreate the WSI swapchain and restart the scheduler with a new generation. Pause scheduling while minimized/occluded. On device loss, stop queue work and invalidate all Framegen generations. During shutdown, stop the scheduler and retire all owned resources before destroying Presenter/swapchain state. Use present fences/waits only for the resource-retirement condition they actually signal; use `VK_GOOGLE_display_timing` for display feedback. This is a design, not implemented or runtime-verified.

**Highball implications:** this repository supplies the D3D8/9/10/11 DXVK lane, not a replacement for the separate D3D12 VKD3D-Proton lane. Later packaging would need matched DXVK Windows DLLs, MoltenVK, the Vulkan loader alias, ICD manifest and Wine portability support; a Framegen library and model payload come later if justified. Keep the VKD3D-Proton D3D12 route separate. Highball was not inspected or changed for this step.

## 10. Phase status and evidence boundaries

| Phase | Result |
|---|---|
| 1 — pin current source, submodules, license | **VERIFIED** |
| 2 — unmodified x86_64 D3D11/DXGI build | **VERIFIED** focused targets; full build requires repository-documented dxbc-spirv patch |
| 3 — isolated Wine runtime and portability | **VERIFIED** runtime identity/prefix/device enumeration; source revision not pinned by DXVK-MacOS |
| 4 — D3D11 baseline | **FAIL** stable rendering; Present API sampled but shader pipeline fails |
| 5 — capability probe | **VERIFIED** against same runtime dylib; direct probe process teardown status 139 and version-report mismatch noted |
| 6 — Present call graph | **VERIFIED** from pinned source |
| 7 — DXGI/WSI ownership | **SUPPORTED INFERENCE** that separate WSI scheduling is possible; safe extra scheduling not proven, including app completion/statistics isolation |
| 8 — DXVK synthetic WSI scheduler | **NOT TESTED** |
| 9 — DXGI application invariants | **NOT TESTED** |
| 10 — Vulkan timing | **VERIFIED** in standalone direct probe; **NOT TESTED** in DXVK/Wine window |
| 11 — public Metal export/synchronization | **VERIFIED** in standalone direct probe; **NOT TESTED** through Wine/winevulkan |
| 12 — zero-copy history | **SUPPORTED INFERENCE / DESIGN ONLY** |
| 13 — SDR format boundary | **SUPPORTED INFERENCE** from source and probes; HDR not tested |
| 14 — windowed/fullscreen baseline | **PARTIAL** windowed swapchain creation only; fullscreen not tested |
| 15 — WSI synchronization/ownership | **VERIFIED** source constraints; runtime worker design not tested |
| 16 — lifecycle | **DESIGN ONLY** |
| 17 — Highball compatibility | **DESIGN ONLY**; no Highball changes |
| 18 — RIFE | **NOT STARTED**, as requested |

The audit clone remains clean at the pin. In the shared workspace, pre-existing `README.md` and DXMT Step 10B evidence changes were left untouched. The only new workspace artifact is this `experiments/dxvk_macos_step11a/` audit folder. All build/runtime/probe scratch outside it remains under `/tmp/fgmetal-dxvk-macos-*`.

## References

- [Pinned DXVK-MacOS source](https://github.com/metalsharp/DXVK-MacOS/tree/8d348236e14a3db25ffbe528a83010b3dd69a3ef) and its [macOS build instructions](https://github.com/metalsharp/DXVK-MacOS/blob/8d348236e14a3db25ffbe528a83010b3dd69a3ef/README.md).
- [MetalSharp v0.76 bundle manifest](https://github.com/metalsharp/MetalSharp/releases/download/v0.76.0/metalsharp-bundle-manifest.tsv) (the tested runtime archive hash matches the published manifest).
- [MoltenVK issue #2791](https://github.com/KhronosGroup/MoltenVK/issues/2791).
- Vulkan public API references: [`VK_EXT_metal_objects`](https://docs.vulkan.org/features/latest/features/proposals/VK_EXT_metal_objects.html), [`VkPresentTimeGOOGLE`](https://docs.vulkan.org/refpages/latest/refpages/source/VkPresentTimeGOOGLE.html), [`VkPastPresentationTimingGOOGLE`](https://docs.vulkan.org/refpages/latest/refpages/source/VkPastPresentationTimingGOOGLE.html), and [`vkQueuePresentKHR`](https://docs.vulkan.org/refpages/latest/refpages/source/vkQueuePresentKHR.html).
