# Step 11C — Renderer internal WSI presentation proof

Date: 2026-10-05. **Step 11C verdict: PARTIAL PASS.** Windowed runs prove extra renderer-issued Vulkan WSI presents with separate source/internal identities and unchanged measured DXGI accounting. The user directly confirmed that cyan and magenta are visible as brief flashes in the experiment window. That visual confirmation was on the immediately preceding post-audit candidate; the newest build has a separate matched API run. The scheduler remains far below doubled cadence, and fullscreen, recreation, failure-injection, and pending-work shutdown matrices remain unverified. Step 11D is not cleared.

## Frozen identities and patch layering

- DXVK-MacOS base: `8d348236e14a3db25ffbe528a83010b3dd69a3ef`.
- Step 11B.1 prerequisite remains separate: `patches/prerequisite-step11b1.patch`, SHA256 `4f4973000dce0783d05e11b7acae0efdff2d6bfe898bf49305408d72280e758`. It contains only `beginExternalRendering() -> endCurrentPass(false)` before `endCurrentCommands()`.
- Corrected Step 11B.1 D3D11 DLL reference: SHA256 `b63c935e55a0854dc6857414fceaf7fde850da9f201e027b41a4cf97fed59352`.
- Step 11C downstream patch: `patches/internal-presentation-experiment.patch`, SHA256 `4df3af55c9491a44d5e8cec79d458bf7cecc18d71b61312b19e80b461023d352`. It excludes the prerequisite context fix.
- Final rebuilt C D3D11 DLL: SHA256 `3c39a706d2fb577825592b9bfbc335feb218f93f933e7b83a25dcda8e1f32c3c`. Frozen DXGI DLL: `17cbe8e84906ce9657493b1301e712cfc8123413034666fd9387dfe1bbf5b33c`.
- A is pristine and clean. B contains only the prerequisite change. C is based on the same commit plus the prerequisite and the downstream patch. B was not edited while developing C.
- Runtime pins: MoltenVK v1.4.2, commit `db66022459ffb663aa2b50f6b018bc2e124f5edf`; MetalSharp Wine 11.17.

Layer identities and hashes are also recorded in `evidence/phase0-layered-identities.json`.
The temporary A/B/C checkouts, Wine prefixes, build tree, generated evidence binaries, and redundant multi-megabyte Wine prefix-initialization logs were removed after exporting both patch layers and recording run hashes, renderer/application logs, CSVs, and analysis JSON.

## Pre-implementation reviews

Eight requested read-only audits were completed before implementation, followed by an independent adversarial review.

1. **Presenter state:** source and internal output must share one Presenter. The sync semaphore ring (`m_frameIndex`) and acquired-image ring (`m_imageIndex`) are distinct. The implementation uses one active lease and `m_presentPending`; source pre-acquire remains blocking and internal pre-acquire is nonblocking.
2. **DXGI accounting:** `m_frameId`, GetLastPresentCount, GetFrameStatistics, frame-latency signaling, latency tracking, and Reflex/NV IDs are app-owned. WSI IDs, image leases, ring advancement, and internal retirement are WSI-owned. Timing feedback must be split.
3. **Submission queue:** source and internal output must use the serialized graphics queue owner. Internal completion cannot call `signalFrame`. A queue worker must not call `waitForIdle()` on itself.
4. **Acquire/present serialization:** optional output checks source intent, uses a try-lock and cached image, and drops if source work or WSI ownership conflicts. There is no guarantee of zero source wait after G has acquired an image, so source latency must be measured.
5. **Scheduler:** bounded midpoint deadlines and capacity one were recommended; the target pattern was A, G, B. Timing intervals are monotonic; actual display timing remains distinct from GPU/present retirement.
6. **Invariant testing:** preserve the captured GetFrameStatistics lag envelope (0–2 in the Step 11B.1 baseline), compare GetBuffer identity only within each run, and treat the harness's zero-timeout waitable polling as asynchronous observations.
7. **Vulkan timing:** MoltenVK exposes `VK_GOOGLE_display_timing` in the pinned runtime, but DXVK has no integration. If added later, use WSI-domain IDs and distinguish actual display timestamps from present fences/waits.
8. **Adversarial review:** source-only app signaling was confirmed. The review required retaining a submitted internal lease until GPU timeline retirement, rejecting source work that arrives during internal command recording, and checking surface/swapchain state at the serialized WSI boundary. The final candidate includes these guards and allocates each WSI ID only after validation, immediately before the WSI call.

The reviewer noted that device module detachment skips the normal `waitForIdle()` path. The ordinary tested process exits completed, but module-detach shutdown with active internal work remains unverified.

## Implemented path

`AppFrameId`, `WsiPresentId`, and `InternalPresentId` are distinct types. Each queued present has an explicit `APPLICATION_SOURCE` or `INTERNAL_OUTPUT` origin and validates which optional ID is present. In the final run, source AppFrameIds were consecutive while internal records had no AppFrameId.

Both output types reach the same `Presenter::presentImage()` and the same sole `vkQueuePresentKHR` call site, under the existing graphics-queue mutex. Internal scheduling is opt-in through `DXVK_INTERNAL_WSI_PROOF=1` and is restricted to the D3D11 proof path. The internal request queue holds at most one pending opportunity. A request has a midpoint target, a finite deadline with margin, a generation, and a pattern ID. Queue-full, deadline, and source-queue conflicts drop G instead of delaying it into a backlog.

G is a renderer-generated full-image Vulkan clear alternating magenta and cyan on a cached, acquired WSI image. It has no source history and uses no CPU image readback, screen capture, Metal interop, private Metal/MoltenVK API, or neural code.

Completion is split. Application source frames alone call `signalFrame`, update the app completion watermark, notify the latency tracker, and signal the DXGI frame-latency object. Internal output only advances its internal retirement watermark. GetFrameStatistics reads app-only timing feedback; internal timing is held separately. `VK_GOOGLE_display_timing` was not integrated.

The WSI logs contain one raw `vkQueuePresentKHR` call site. Source and internal presents share the same Presenter, queue lock, semaphore pool, swapchain, image ring, and generation. Image-lease validation runs before command submission. If the final post-submit validation ever rejects an internal frame, its lease remains pending until the finish worker observes the GPU timeline, then marks the swapchain dirty and retires it. Internal presentation also holds the surface mutex across the final dirty check and WSI operation, ordering resize/settings changes around that operation. A renderer-side rejection consumes no WsiPresentId; IDs are assigned at the actual `vkQueuePresentKHR` dispatch boundary.

Internal frames do not wait for display completion or run the source FPS limiter on the Presenter frame worker. This keeps optional G retirement from delaying later application frame-latency signals. That retirement is a GPU/present-submission lifecycle event, not proof of display scanout.

## Runtime evidence

### Latest matched 15 Hz A/B

The newest 20-second pair used the same rebuilt D3D11 DLL (`3c39a706…`), frozen DXGI DLL, application executable, and Wine/MoltenVK runtime:

- Internal disabled: `evidence/final-current-disabled-15hz-20s/`
- Internal enabled: `evidence/final-current-internal-15hz-20s/`
- Analyzer output: `evidence/final-current-15hz-20s-comparison.json`

Both processes exited 0. Each made 301 successful application Presents, and GetLastPresentCount ended at 301. The experiment dispatched 301 source WSI presents plus 42 internal presents, for 343 total WSI presents (about 17.15/s, compared with 15.05 application Presents/s). It did not reach the intended doubled cadence. Internal drops were 155 queue-full, 97 deadline-expired, 4 deadline-expired-before-submit, and 3 source-pending. There were no WSI presentation failures. The analyzer verified unique monotonic WSI IDs, source AppFrameIds only for app Presents, no AppFrameId on internal entries, ordered acquisition serials, exactly one terminal outcome per internal request, and no duplicate/stale image retirement assertion.

The matched API observations were unchanged within the measured workload: Present HRESULT and GetLastPresentCount sequences matched; each GetLastPresentCount reached 301; GetFrameStatistics had 300 successful samples and stayed within the frozen 1–2-frame lag envelope; zero-timeout waitable polls were 300 signaled and one timeout in each run; app frame-latency signal records were 301 per run and source-only; maximum frame latency, backbuffer index sequence, per-run GetBuffer(0) identity stability, and device-removed queries matched. This is an application-accounting pass for the measured 20-second workload, not a lifecycle proof.

Measured application Present CPU durations (microseconds):

| Run | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| Internal disabled | 65 | 223 | 617 | 25,948 |
| Internal enabled | 68 | 289 | 731 | 17,235 |

The experiment added 3 μs at p50, 66 μs at p95, and 114 μs at p99; its maximum was lower in this pair. This short 15 Hz run did not show substantial source-Present blocking, but it does not cover slow GPU, queue saturation, resize, occlusion, fullscreen, or shutdown with pending internal work.

### Earlier 30 Hz and visual runs

The earlier same-binary 30 Hz comparison is in:

- `evidence/renderer-disabled-final-candidate-15s/`
- `evidence/cooperative-final-candidate-30hz-15s/`
- `evidence/cooperative-final-candidate-30hz-15s/invariant-analysis.json`

The earlier same-binary 15 Hz pair is in `evidence/lowrate-disabled-final-lock-15hz-15s/` and `evidence/cooperative-lowrate-final-lock-15hz-15s/`.

The earlier 30 Hz pair used DLL hash `09e73ab505da1cb5740fdd2d413f331144f58ca2def357426633762525cbfe03`, before the final review hardening changed source-arrival rejection, internal frame-worker waiting, and WSI-ID allocation/logging. Both exited 0. The experiment ran windowed with the application reporting visible, non-iconic, and foreground state. The earlier 15 Hz pair used DLL hash `dec55b55d4cc85bb9e003af53f5bfd116119750d3b5301311714a0a1b78a4ca8`; tracing did not change the scheduler or output path.

| Source cadence | App Presents | Internal WSI presents | Total WSI presents | Internal/source ratio | Internal drops |
|---|---:|---:|---:|---:|---|
| 15 Hz, 15 s | 226 | 43 | 269 | 0.190 | 111 queue-full, 71 deadline-expired |
| 30 Hz, 15 s | 451 | 76 | 527 | 0.169 | 225 queue-full, 148 deadline-expired, 1 source-queue-nonempty |
| 30 Hz, 60 s visual follow-up | 1,801 | 262 | 2,063 | 0.145 | 876 queue-full, 625 deadline-expired, 35 source-pending, 3 source-queue-nonempty |

The low-rate experiment reaches about 18 WSI calls/s rather than the intended 30. The 30 Hz 15-second experiment reaches about 35 WSI calls/s rather than about 60. The 60-second visual follow-up reached about 34.4 total WSI calls/s (about 4.4 internal presents/s), still far below the doubled-cadence target. These are successful distinct `vkQueuePresentKHR` calls (`VK_SUCCESS`), not proven scanout counts. The logs show actual call ordering, for example source WSI 16, magenta internal WSI 17, then source WSI 18. Internal IDs and total WSI/acquisition serials are contiguous within each run. The logged swapchain generation stayed at 2. No duplicate/out-of-order or stale-retirement assertion fired. The code bounds pending queue depth to one; it does not yet report a sampled high-water mark.

### DXGI invariant comparison

For that earlier 30 Hz A/B, all 451 Present calls returned `S_OK`; GetLastPresentCount was exactly 1…451 in both runs. Each run had one initial `DXGI_ERROR_FRAME_STATISTICS_DISJOINT` followed by 450 successful statistics samples; successful PresentCount stayed one frame behind and within the frozen 0–2 frame baseline envelope. Maximum frame latency Set/Get remained 1. All zero-timeout waitable polls returned signaled in both runs, and 451 unique source app-frame signal calls were logged in each. Internal entries had no AppFrameId and did not create app-frame latency signal records. Backbuffer index stayed 0, GetBuffer(0) identity was stable within each run, and device-removed queries stayed `S_OK`.

The harness uses a DISCARD swapchain with one actual backbuffer, so index 0 does not prove multi-buffer progression. Pointer identities are intentionally compared only within a process. The 15 Hz A/B also passed the same counter, statistics, waitable, buffer identity, and device-state checks.

### Source Present latency

Earlier 30 Hz A/B CPU durations, measured around the application `Present` call:

| Run | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| Internal disabled | 24 μs | 49 μs | 92 μs | 7,587 μs |
| Internal enabled | 25 μs | 70 μs | 117 μs | 8,298 μs |

The experiment's p99 increase was 25 μs and its max was 711 μs higher in this matched run; this does not show substantial source blocking. An earlier pre-final run produced a 25.499 ms outlier, which did not reproduce in the matched pair. The earlier 15 Hz pair in the linked evidence folders measured p50/p95/p99/max of 27/54/142/6,960 μs disabled and 30/90/163/8,385 μs enabled.

### Direct visual and timing gates

The user directly confirmed: “Cyan and magneta are visible but in flashes in your second window that launched.” This identifies the renderer-generated cyan and magenta output as brief visible flashes. The observation was made on the 20-second `postaudit-final-internal-15hz` run (D3D11 DLL SHA256 `0c8eedad69d98a6c0e98c44a5d1120274d0f324733b15ad68542d6e1e7abe1c8`), before the final lifecycle hardening rebuild (`3c39a706…`). The synthetic pattern implementation was unchanged by that hardening. A record of the direct confirmation is stored in `evidence/visual-user-confirmation-20261005-postaudit.json`. The earlier 60-second candidate logged 131 magenta and 131 cyan pattern presents, but logs alone are not visual evidence. No screen capture was used.

`VK_GOOGLE_display_timing` is not wired into DXVK. Thus actualPresentTime, presentMargin, and refresh duration were not captured in these runs. The earlier pinned MoltenVK probe is observe-only background evidence, not an internal-DXVK display trace.

## Lifecycle, failure, and shutdown evidence

The 60-second visual follow-up used the 30 Hz application harness but produced only 262 internal presents for 1,801 source presents, so it does not qualify as a successful 30→60 run. A duration-matched internal-disabled control and five-minute soak were not run. Also not run: fullscreen, resize/recreation during queued G, minimize/occlusion, injected queue/acquire/submit/present/deadline failures, or the six-state shutdown matrix. The final harness set `FG_AUDIT_SKIP_RESIZE=1` and did not enable lifecycle actions. Ordinary tested processes exited cleanly, but this is not evidence for pending-G teardown or fullscreen/recreation safety. No Xcode Display trace was collected.

## Build and Step 11B.1 regression recheck

The final D3D11 and DXGI targets built successfully, as did the modified D3D9 swapchain source object. Linking the full D3D9 target remains blocked by the existing `dxbc-spirv::Converter::Options::deAliasedSamplers` mismatch in `d3d9_shader.cpp`; that failure is outside the Step 11C edits. With internal presentation disabled and the final C D3D11 DLL, the RGBA8 and BGRA8 smoke runs each lasted 15 seconds and exited 0. Their manifests are `evidence/final-current-rgba8-15s/result.json` and `evidence/final-current-bgra8-15s/result.json`. These exact-build format runs were not separately visually confirmed; the Step 11B.1 visual baseline remains the prior direct visual pass.

The final source review also fixed two lifecycle/exception gaps. `m_swapchainChanging` makes the Presenter worker leave its bounded present-wait slice during swapchain recreation and skip stale timing/FPS work. Submission and finish workers now expose active-entry state so normal frontend `waitForIdle()` waits for worker-local references before teardown. D3D11 allocates the next command chunk before enqueueing the current one, preventing an allocation failure after enqueue from opening a duplicate application-frame-ID window. These changes built and the latest matched A/B passed, but lifecycle matrices were not run.

One shutdown edge remains unresolved: D3D11/D3D9 module detachment deliberately skips the normal queue drain. A finish worker's latency-tracker reference can then be the last Presenter→Device owner and cause Device destruction on its own finish worker, risking self-join/termination. This path has not been fixed or verified, so full shutdown safety is not claimed.

## Gate result

**PARTIAL PASS.** The latest rebuilt binary proves extra renderer-issued Vulkan WSI calls use separate source/internal identity domains while the measured app-facing DXGI counters, statistics lag, waitable polling, frame-latency signaling, buffer observations, and device status match the disabled A/B. The user directly confirmed brief visible cyan and magenta flashes on the immediately preceding post-audit candidate; this visual observation is not misattributed to the exact latest binary. The latest 15 Hz matched run reached 343 total WSI calls for 301 app Presents, so the requested doubled cadence is not demonstrated. Fullscreen, resize/recreation during G, occlusion, failure injection, and pending-work shutdown remain unverified; module-detachment shutdown has a known unresolved self-join risk. No Step 11D authorization follows from this result. RIFE, Metal inference, Highball, and DXMT were not modified.
