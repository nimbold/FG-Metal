# Step 11B — MoltenVK repair and gated DXVK WSI experiment

Audit date: 2026-10-04. **Gate 1 FAIL: the DrawIndex blocker is repaired, but the unmodified Wine/DXVK D3D11 window remains black. Gate 2 was not reached.**

The user directly observed the Wine D3D11 window as “it's black screen.” The separate native window, “Step 11B NATIVE Vulkan clear control,” rendered red and green correctly according to the user's direct observation. Successful HRESULTs, successful shader compilation, and clean process exits do not satisfy the healthy-rendering gate. The remaining blocker is incorrect/absent visible D3D11 content through Wine/DXVK; its precise source location is unresolved. This is not proof against the proposed independent internal WSI architecture.

No DXVK presentation code was changed. No experimental downstream presentation worktree, internal scheduler, generated-image path, RIFE, Metal inference, Highball modification, or DXMT modification was made. Pre-existing workspace changes were preserved.

## Gate 1 evidence

### Old runtime identity and preservation — VERIFIED

The original runtime remains at `/tmp/fgmetal-dxvk-macos-audit-run/runtime/wine`. Its prefix, DLLs, and original logs were not overwritten. The candidate is an APFS copy-on-write copy at `/tmp/fgmetal-step11b/runtime-candidate/wine`, with a separate prefix at `/tmp/fgmetal-step11b/prefix`. Copies share storage safely rather than hard-linking writable files.

The misleading Step 11A comparison used **two different implementation files**:

| Lane | Implementation | SHA-256 | Identity |
|---|---|---|---|
| Old Wine/winevulkan/DXVK | `lib/wine/x86_64-unix/libvulkan.dylib` | `1d57a14b4a0d380420ab566884018fd83ba57381aa44e464f79472a77273b6bc` | x86_64; MoltenVK 1.4.1; Vulkan 1.4.334; direct `driverInfo=1.4.1` |
| Old native direct probe / separate ICD payload | `lib/wine/x86_64-unix/libMoltenVK.1.dylib` | `8249d81ebf2d46f82b16ca166c2e5cca5d76d91d0a412cd6d3db1aaa6e8430bf` | universal x86_64 + arm64; reports MoltenVK 1.4.3; Vulkan 1.4.357; direct `driverInfo=1.4.3` |
| New candidate Wine and direct probes | locally built pinned 1.4.2 payload | `df43b3a65b67efb06f673fc8f0e5cedf7f54276f5911f479c17050c04b1fd12e` | universal x86_64 + arm64; reports MoltenVK 1.4.2; Vulkan 1.4.357 |

`otool -L` prints the dylib's own `LC_ID_DYLIB` first. Old `libvulkan.dylib`'s `@rpath/libMoltenVK.1.dylib` / current-version 1.4.1 is **its own install name**, not a dependency on the separate 1.4.3 library. Its actual load dependencies are Apple frameworks/system libraries. It is a MoltenVK implementation, not a thin Vulkan loader. Thus replacing only the separate `libMoltenVK.1.dylib` would not repair Wine's active provider.

The old native probe's loaded image was verified by LLDB. Old `libvulkan.dylib` was directly `dlopen`ed with `dladdr` reporting the exact implementation path. The original 11A Wine PID's dyld image list was not captured. A new negative-control replay with the unchanged old runtime and a separately copied prefix now verifies its loaded `winevulkan.so` and `libvulkan.dylib` paths, startup 1.4.1, and the same DrawIndex/CS-thread failure. The app stalled and the watchdog terminated it after 55 seconds; this is a reproduced negative result, not a clean shutdown. See [old replay](evidence/negative-old-hud-10s/runtime.log) and [result](evidence/negative-old-hud-10s/result.json). **The new Wine/DXVK path is runtime-verified** by `DYLD_PRINT_LIBRARIES`: `/private/tmp/fgmetal-step11b/runtime-candidate/wine/lib/wine/x86_64-unix/libvulkan.dylib`, with startup 1.4.2. `/tmp` and `/private/tmp` resolve to the same files on this host.

The independent [identity audit](audits/identity.md) records path inventory, hashes, slices, install IDs, current/compatibility versions, load relationships, and ICD paths. No upstream v1.4.3 release tag was found; the bundled 1.4.3 report is a binary's version string, not a verified release/source pin. Do not claim it is an official 1.4.3 release.

[Negative-control manifest](evidence/negative-control-manifest.json), [after-test hashes](evidence/negative-control-after.json), and [runtime comparison](evidence/runtime-comparison.json) retain the exact old payload identities. All 25 negative-control records still match. Critical old Vulkan/MoltenVK payloads, Wine Vulkan bridge, ICDs, DXVK DLLs, and original logs are also durably preserved in [negative-control-payloads.tar.gz](evidence/negative-control-payloads.tar.gz), SHA-256 `6dae32aaeb734031fc808ad74db7a89199ca9a13fb7cba3aa7e22a21224b4f86`; the archive is not a complete runnable Wine distribution. Wine/wineserver, `winevulkan.so` and `.115`, ICDs, and the pinned DXVK DLLs remain unchanged. Only three MoltenVK implementation payload files in the candidate were replaced; see [staging manifest](evidence/staging-manifest.json).

### Upstream DrawIndex change and build — VERIFIED

[MoltenVK v1.4.2](https://github.com/KhronosGroup/MoltenVK/releases/tag/v1.4.2) explicitly adds macOS `gl_DrawID` / `DrawIndex` support. The minimum release was selected rather than moving main.

| Pin | Value |
|---|---|
| MoltenVK tag | `v1.4.2` |
| Commit | `db66022459ffb663aa2b50f6b018bc2e124f5edf` |
| Release date | July 24, 2026; release commit July 23 |
| SPIRV-Cross | `6c09849fe88c48eaed08413aa022aaa136a3a057` |
| LICENSE SHA-256 | `cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30` |
| Upstream main comparison | `52aa21f54d7a84c5c441fc26359692b0980b384c`; same SPIRV-Cross pin at audit time |
| Old v1.4.1 SPIRV-Cross | `adec7acbf41a988713cdb85f93f26c8ca5ea863e`; explicitly rejects DrawIndex |
| SPIRV-Cross DrawIndex support | PR [#2634](https://github.com/KhronosGroup/SPIRV-Cross/pull/2634), merge `38681a30e09679191cc3957719eeee76024f6daf`, May 20, 2026 |

Built the unmodified 1.4.2 source with `./fetchDependencies --macos` followed by `make macos`. Full-history dependency downloads were replaced by exact-revision shallow fetches to avoid unnecessary transfers; no pinned source or dependency revision was changed. Xcode 27.0 / Apple Clang 21, macOS 27.0.1, SDK 27.0; dynamic target `ARCHS=arm64 x86_64`, deployment target 12.0. Binary slices and `LC_BUILD_VERSION` were inspected. `MVK_USE_METAL_PRIVATE_API` defaults to 0 and was not enabled. The packaging aggregate's host-only build-settings output is not the actual dylib architecture; the dynamic-target settings and binary confirm both slices.

See [DrawIndex audit](audits/drawindex.md), [build identity](evidence/build-identity.json), [dynamic target settings](evidence/dynamic-build-settings.log), [dependency build log](evidence/build-dependencies.log), and [build log](evidence/build.log). These include all dependency pins, toolchain, Mach-O loads, and binary hash.

### Direct DrawIndex A/B — VERIFIED

The x86_64 [pipeline probe](evidence/pipeline-probe.cpp) explicitly loads the library argument, reports `dladdr`, enables available `shaderDrawParameters`, creates a minimal graphics pipeline with the same [DrawIndex SPIR-V](evidence/drawindex.vert.spv), then destroys its Vulkan objects. The shader uses both `VertexIndex` and `DrawIndex`; SPIR-V validation passed.

| Provider | Result |
|---|---|
| Actual old Wine implementation, 1.4.1 | `DrawIndex is not supported in MSL`; pipeline result `-3` / `VK_ERROR_INITIALIZATION_FAILED`; intentional test exit 10; orderly teardown |
| New pinned 1.4.2 | pipeline result 0; process exit 0; orderly teardown |
| Exact ordinary DXVK `dxvk_present_vert` on old provider | pipeline result 0; process exit 0 |

See [old log](evidence/drawindex-old.log), [new log](evidence/drawindex-new.log), and [ordinary DXVK vertex control](evidence/dxvk-vertex-old.log).

**Important attribution correction:** the ordinary Presenter blit shader uses `VertexIndex`, not `DrawIndex`, and is valid Vulkan SPIR-V. The only pinned built-in source explicitly using `gl_DrawID` is HUD text, which selects per-draw text data. Its generated SPIR-V contains both built-ins and passes Vulkan 1.3 validation. The old generic pipeline exception did not name the module or capture the original HUD environment. Therefore the exact old caller remains **SUPPORTED INFERENCE**, most plausibly HUD text, rather than proven ordinary presentation shader failure. New HUD-enabled runs compiled and completed; the Metal/MoltenVK debug log contains the translated HUD DrawIndex buffer path. See the [shader/source and upgrade-safety audit](audits/shader.md) and captured [HUD SPIR-V](evidence/hud_text_vert.spv).

### Teardown 139 repair — VERIFIED

The original native capability executable reproduced `EXC_BAD_ACCESS` in `objc_release` during `AutoreleasePoolPage::releaseUntil` / `objc_autoreleasePoolPop`. An ARC rebuild with `NSZombieEnabled=YES` identified `-[NSKVONotifying_NSWindow release]: message sent to deallocated instance`.

The new [interop probe](evidence/interop-probe.mm) sets `releasedWhenClosed=NO`, preserving ARC ownership when `close` is called. It also destroys the Vulkan image before freeing its bound memory. Vulkan device/surface/instance destruction remains before window close; exported shared-event command buffers are waited before their Vulkan owners are destroyed. This is an ownership repair, not `_Exit`, a suppressed exception, or a deliberately leaked manual-retain workaround.

The repaired ARC probe exits 0 on both the original direct-probe library (bundled 1.4.3) and the pinned new 1.4.2. [LLDB crash](evidence/teardown-crash-lldb.log), [zombie diagnosis](evidence/teardown-zombie.log), [fixed old probe](evidence/interop-old-fixed.log), [new probe](evidence/interop-new.log).

### Unmodified D3D11 runtime results — API/structural observations VERIFIED; healthy rendering FAIL

The candidate uses the exact Step 11A executable and DLL hashes:

- `d3d11.dll`: `6940b883fb4bc1b744fb7ca3aa6a486303eaf180c9fe85c03ed7cb175057bf28`
- `dxgi.dll`: `17cbe8e84906ce9657493b1301e712cfc8123413034666fd9387dfe1bbf5b33c`
- Original test executable: `18509edad2e1ae8d49941bd1868dd384d03cf693cc66b5542e7bba1611e21163`

The source pin remains clean at `8d348236e14a3db25ffbe528a83010b3dd69a3ef`. The renderer and Wine code were not patched. The copied app's `FG_AUDIT_ENABLE` selects diagnostics and source pacing only; no pinned renderer code reads it. Legacy app/CSV names containing “Framegen” and its environment-toggle events do not enable any generated output. A copied prefix isolates these tests. FPS HUD was requested for the repeated runs. The debug shader dump verifies the DrawIndex HUD path compiled on the candidate. The retained runner now defaults to FPS HUD, supports `--no-hud`, and records environment snapshots for future runs. Archived results did not persist those snapshots, so their historical HUD settings are not independently verified by the saved result files and are not used as A/B proof.

| Run | Application Presents | Present HRESULT | Exit | What this establishes |
|---|---:|---|---|---|
| Initial 30 s smoke | 901 | all S_OK | 0 | Initial API/structural smoke |
| 30 s, FPS HUD requested | 901 | all S_OK | 0 | Repeated API smoke; requested HUD |
| 60 s, FPS HUD requested | 1800 | all S_OK | 0 | Sustained ~30 Hz application calls, no logged renderer/device failure |
| 65 s lifecycle, FPS HUD requested | 1951 | 1921 S_OK; 30 DXGI_STATUS_OCCLUDED | 0 | Successful API transitions and repeated WSI recreation |
| 30 s waitable/debug diagnostic | 901 | all S_OK | 0 | App-facing diagnostic observations on a visibly unhealthy lane |

The original app logs all four intended clear-color IDs, visible/foreground window state, and ~30 Hz pacing. These are **not pixel-render evidence**. The user's Wine-window observation was black. Consequently the 30/60-second calls are not accepted healthy rendering passes.

Lifecycle log records resize to 800×450, minimize at ~25 s, restore at ~26 s, fullscreen enter ~35 s, exit ~36.6 s, and swapchain recreation ~45 s, all successful. The 30 occluded statuses occur while minimized. The lifecycle event named `focus_loss` is implemented by minimize, so independent focus loss without minimization was not tested. The DXVK/MoltenVK logs show recreated FIFO swapchains at corresponding extents and clean shutdown. Visual correctness across these transitions remains failed/unverified rather than passed.

A Windows Vulkan enumeration diagnostic loaded Wine's `vulkan-1.dll`, reported Apple M3 / `driverInfo=1.4.2`, created a device, and exited 0. Wine filters portability enumeration/subset from its Windows-facing extension lists, whereas its native DXVK startup shows portability handling. Do not misreport the Windows diagnostic's zero extension flags as missing native portability. The new provider/device were proven natively before staging; the separate Windows enumeration diagnostic was run after the first DXVK smokes, not before them. See [Wine enumeration log](evidence/wine-enumeration.log).

Public Metal API Validation and MoltenVK debug were enabled for the waitable diagnostic. No shader translation failure, DXVK CS failure, command-submission failure, or device-loss pattern was found. Wine logged unrelated missing EGL/Bluetooth setup diagnostics; this lane is Vulkan and successfully created its renderer/device. These do not explain the black surface. Lack of a validation error is not proof of correct content.

See [run summary](evidence/run-summary.json), individual run folders, [runner](evidence/run-baseline.py), and [lifecycle CSV](evidence/lifecycle-65s/d3d11_clear_window_lifecycle.csv).

### Frame-latency observations — VERIFIED diagnostic, not accepted healthy baseline

The separate [controlled app extension](evidence/d3d11-semantics.cpp) requests flip-discard and `DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT`. It records counts, statistics, buffer identity/index, device status, and zero-timeout semaphore waits. Renderer DLLs are still unchanged.

`GetFrameLatencyWaitableObject` returned a non-null handle; `SetMaximumFrameLatency(1)` and `GetMaximumFrameLatency` returned S_OK/configured value 1; actual throttling under a delayed consumer was not tested. All 901 observed `GetLastPresentCount` values equal the app sequence. Final statistics PresentCount was 900 versus last app count 901, consistent with asynchronous completion. The initial statistics result was `DXGI_ERROR_FRAME_STATISTICS_DISJOINT`; later observations succeeded. Wait polls returned 900 signals and one timeout. Buffer-0 COM identity stayed constant, backbuffer index was 0, and all device-removed queries returned S_OK. The pinned `GetImageIndex()` is hard-coded to return 0, and a stable COM wrapper is not a WSI image identity; these observations do not independently establish backing-allocation progression.

This test consumes the waitable signals after each app Present. It does not prove latency limits under a deliberately stalled consumer, provide renderer callback instrumentation, or establish invariant preservation under internal presents. No internal present was issued. The black output prevents treating it as the required healthy pre-G baseline. [Semantics CSV](evidence/waitable-debug-30s/semantics.csv).

### Native rendering isolation and Metal export — VERIFIED, separate process

The same pinned 1.4.2 library was explicitly linked into the [native Vulkan clear control](evidence/native-clear-control.mm). It issued 1800 successful FIFO WSI presents, completed submission-fence waits, and exited 0. The user confirmed visible red/green alternation. This establishes colored native Vulkan output on the candidate library, not correctness of the Wine/DXVK sampled blit path. Its inherited standalone log labels `source`/`synthetic` describe two clear colors; they are **not** application DXGI and renderer-internal DXVK outputs. The native log's `desired_ns` field retains an unused calculated proposal from the earlier probe; the actual submitted `desiredPresentTime` is zero, so this control uses no desired-time scheduling. Its elapsed time and 60-entry Google-timing snapshot were not used to claim physical 60 Hz output or an integrated cadence proof. [Native log](evidence/native-clear.log).

The repaired public interop probe on 1.4.2 also exported matching Apple M3 `MTLDevice`, `MTLCommandQueue`, 256×144 BGRA8 `MTLTexture` with stable identity, and `MTLSharedEvent`. Metal→Vulkan value 7, Vulkan→Metal value 9, and imported event wait at 11 succeeded, with exit 0. Value 9 was signaled through host `vkSignalSemaphore`, not a Vulkan GPU submission, so GPU Vulkan→Metal queue ordering and a GPU image roundtrip are not established. This is an early standalone upgrade regression observation. It is **not B17 after a successful WSI proof**, and does not test arbitrary DXVK image history, Metal writes to history, or RIFE.

## Gate 2 — NOT TESTED

The required visible baseline failed, so the stop rule was applied before any DXVK presentation mutation. No AppFrameId/WsiPresentId/InternalPresentId implementation, completion split, single-owner scheduler, 30→60 invariant run, Vulkan Google timing integration, failure injection, internal resize/shutdown testing, or trace was performed.

The [bookkeeping audit](audits/bookkeeping.md), [independent bookkeeping review](audits/bookkeeping-review.md), [serialization audit](audits/serialization.md), and [independent WSI-owner review](audits/wsi-owner-review.md) identify the necessary split and serialized owner, but design is not implementation or runtime proof. There is no justification for reusing the D3D11 frame ID or routing internal completion through app-facing `signalFrame`. A native standalone clear test cannot substitute for the DXGI/WSI split. The existing frame statistics, app completion fence, latency signaling, pending-acquire ring, and present-retirement paths still require separation before internal output can be tried. Reflex/NV tracking already uses mapped tracker IDs, which must be reconciled with a future WSI ID domain rather than treated as identical numeric IDs. Asynchronous queue-present failures do not flow back as the original DXGI Present HRESULT. The serialization audit also identifies a future self-deadlock if existing `waitForIdle()` is called from a submit-worker recreation item; that needs an owner-aware barrier/watermark. Already-entered Vulkan present calls cannot simply be preempted, so source delay must be measured before claiming drop-G/source-priority safety.

## Independent findings and classification

Seven distinct subagents were used across the initial audits and independent follow-up reviews, with GPT-6 Luna max requested for the subagent work. The audits covered DrawIndex history, runtime identity, shader attribution, isolated-upgrade safety, app/WSI bookkeeping, serialization, and adversarial evidence review. Their reports are retained under [audits](audits/). The [adversarial review](audits/adversarial.md) confirmed the gate failure and required tighter wording for occlusion statuses, minimize versus independent focus loss, waitable polling coverage, hard-coded backbuffer indices, native versus physical presentation counts, and the initial run's unpersisted HUD environment. Those corrections are incorporated here. The [final adversarial review](audits/final-adversarial-review.md) confirms the failure classification and notes that the original runner critique is historical: future invocations now support explicit HUD selection/environment snapshots, while the archived runs still lack them. Advisory source conclusions were checked against pins, hashes, direct probes, runtime logs, and user visual observations.

**VERIFIED:** distinct old implementations explain the version mismatch; exact pinned 1.4.2 build and x86_64 slice; direct old/new DrawIndex failure/success; NSWindow ownership crash and clean repaired ARC teardown; new Wine provider identity; repeated app Presents and successful lifecycle APIs; standalone new native colored output and public Metal export/event operations.

**SUPPORTED INFERENCE:** the original unnamed DrawIndex failure was the HUD text pipeline; the remaining black-output defect belongs to the Wine/DXVK content/surface path rather than an inability of this pinned MoltenVK to present colored images. The exact failing content/surface stage is unresolved.

**NOT TESTED:** a healthy visible Wine/D3D11 baseline, visible lifecycle correctness, all internal DXVK WSI behavior, app invariants under G, integrated 30→60 cadence, integrated Google timing, trace/display correlation, generated failure handling, or any zero-copy Vulkan-history/Metal roundtrip/RIFE integration.

**FAIL**

**RIFE STILL BLOCKED.**
