# Step 10B.1 — DXMT display-tick presentation prototype

**Result: FAIL.** The version-pinned downstream path builds and runs, and it records real `CAMetalDrawable.presentedTime` feedback. It does not meet the 30→60 acceptance criteria: the final 60-second run averaged 57.358 timestamped presentations/s, logged 148 drawables with `presentedTime == 0` plus one worker deadline drop before `present()`, and had 41 generated presentations without both matching visible source endpoints immediately around them. More seriously, a source `Present1` can be suppressed before its asynchronous history copy succeeds, so a copy failure can lose that accepted source frame.

## Scope and artifacts

**VERIFIED**

- Pristine DXMT checkout: `/tmp/dxmt-fb451568-evidence.opkf8x/repo`, clean at `fb4515681daefb789a4d0f403c4bdbca88f3b3de` before patch export.
- Downstream worktree: `/tmp/dxmt-fb451568-evidence.opkf8x/downstream`, same base commit. The changes remain local and uncommitted.
- The exact-baseline controlled D3D11 run before edits passed; its record is [baseline.txt](../../experiments/dxmt_framegen/evidence/step10b1/baseline/baseline.txt). That verifies app execution and resize setup, not scanout or frame generation.
- The downstream patch contains 16 DXMT files. It applies cleanly to the pristine checkout with `git apply --check`; `git diff --check` passes. Patch SHA-256: `2e728eb22f3d15b41154ef3f1e70b555adda47cc2239334479caf03eb1eb0c0c`.
- The DXMT cross-build succeeded with `ninja -C /tmp/dxmt-step10b1-wine-x64`. The native bridge links the local model-free Framegen backend; no RIFE code is included.
- No upstream PR, commit, or push was made. The pristine checkout and its `AGENTS.md` were not changed.
- The integration uses the typed WineMetal thunk layer and public Metal APIs; it does not use D3DMetal internals, private Wine hooks, game patches, extra DXGI Presents, CPU image readback, screen capture, or RIFE.

The build enabled `enable_dxmt_framegen_experiment` with `framegen_root=/Users/<user>/Documents/Code/FG-Metal` and `framegen_static_library=/Users/<user>/Documents/Code/FG-Metal/build/step10b-synthetic-x86_64/framegen-core/src/libframegen.a`. The staged engine's key artifact hashes were `d3d10core.dll=44af8f611706820d32559f4b3f7045f8cbebb4919a6c7099812d2b5c5ca6d4d2`, `d3d11.dll=e9a717e7c02c66d444a063f08f23eba5404d8ec53bd90eaa520b81a7423604ca`, `dxgi.dll=cc846a43a42983fb42e2e6209ced832c1fb915bbd27e17f6e7ebf780eea3cd6f`, and `winemetal.so=45e7b95241a77adba7adea932291205d9fc7bf9f7a19bc51044ab276e72e3112`. The instrumented app SHA-256 is `bf7cd489ff9e482089010261d6cc8536eabe27951d652f949a0f7ffef66178b9`.

Deliverables:

- [Downstream DXMT patch](../../experiments/dxmt_framegen/patches/step10b1-dxmt-downstream.patch)
- [Final 60-second summary](../../experiments/dxmt_framegen/evidence/step10b1/acceptance-60s-20261003-final-reviewed/summary.txt)
- [Final native event log](../../experiments/dxmt_framegen/evidence/step10b1/acceptance-60s-20261003-final-reviewed/native.csv)
- [Final app progression log](../../experiments/dxmt_framegen/evidence/step10b1/acceptance-60s-20261003-final-reviewed/d3d11_clear_window_app.csv)
- [Run analyzer](../../tools/dxmt/analyze_framegen_run.py)

The patch changes `meson.build`, `meson.options`, `src/d3d11/d3d11_swapchain.cpp`, the DXMT command-queue/context/Presenter files, and WineMetal's `Metal.hpp`, Unix bridge/build/thunk files, and public typed declarations. The native Objective-C/Metal implementation is `src/winemetal/unix/dxmt_framegen_bridge.mm`.

## Architecture

```mermaid
flowchart LR
    App[Controlled D3D11 app] --> P[DXGI Present1]
    P --> Q[DXMT app command chunk]
    Q -->|post-commit on same Metal queue| C[GPU blit into owned private history texture]
    C --> H[Per-swapchain source history]
    H --> G[Native synthetic GPU generator]
    G --> R[Exact adjacent pair G(A,B)]
    DL[CAMetalDisplayLink callback] --> T[Bounded tick queue: timing + retained drawable]
    T --> W[Per-swapchain presentation worker]
    H --> W
    R --> W
    W --> S[DXMT Presenter shared conversion path]
    S --> D[Callback-supplied CAMetalDrawable]
    D --> O[drawable.present()]
    O --> F[presentedTime / GPU completion feedback]
```

Each `MTLD3D11SwapChain` owns its `DXMTFramegenSession`, monotonic source sequence, worker, and command-queue reference. The native session assigns a separate presenter ID and owns its epoch, bounded tick queue, up to six history records, generated pair records, timing state, and circuit breaker. It configures `CAMetalDisplayLink` for a preferred fixed 60 Hz range with preferred frame latency 1. State is not keyed by HWND. A two-swapchain run was not performed.

The display-link callback only reads its timing values, retains the callback drawable, pushes a bounded record, and wakes the worker. The queue limit is three; overflow retires the oldest tick. The worker selects an already-ready source or generated texture and sends it through the existing DXMT Presenter conversion using the external drawable. The callback does not call Framegen or acquire DXMT renderer locks.

## Source capture and synchronization

**VERIFIED by source and runtime logs:** capture occurs in DXGI `Present1` from the current D3D11 Metal texture, before DXMT's final Presenter conversion. Only windowed, SDR, single-sample RGBA8/BGRA8 Presents with `SyncInterval == 1`, zero flags, no Present parameters, and MetalFX disabled are eligible. Other Presents use the ordinary path.

For each eligible source, WineMetal allocates a private Metal history texture and encodes a GPU blit. DXMT attaches a post-commit task to the application's command chunk; the task commits the copy on the same Metal queue after the app-render command buffer commits. Metal completion status, rather than an event signal alone, controls whether the source can enter generation. The generator accepts only two adjacent source IDs whose copies both completed successfully. The synthetic backend owns a separate Metal command queue and blends the two native textures on the GPU; there is no CPU pixel readback or screen capture in this path.

The bridge exposes typed WMT calls for session lifecycle, source reservation/copy completion, tick wait, selection, and presentation reporting. WMT handles remain opaque to Windows-side code; Objective-C handles are unwrapped only in the WineMetal Unix bridge. The local synthetic library used for this build is `build/step10b-synthetic-x86_64/framegen-core/src/libframegen.a` (SHA-256 `dbed8452d8458641aee9ca6b58188ba5a3b4873ff2e0dfd357143fafade19d54`).

The clock bridge maps `CACurrentMediaTime()` values for callback, deadline, target, and actual presentation into the native `mach_absolute_time()` nanosecond epoch. Source arrival, copy completion, generator submission/readiness, worker submission, GPU completion, and presentation feedback are logged in that same epoch; raw Core Animation time bit patterns are retained in the CSV.

The worker dequeues without renderer locks and then takes the D3D11 device mutex before using Presenter/layer state. `Present1`, resize, and fullscreen transitions use that mutex. Resize and fullscreen changes wait for DXMT GPU idle before resetting the framegen epoch. Teardown waits for DXMT GPU idle, stops and invalidates the display link, drains callbacks, joins the worker, and releases the session. This lock/lifetime order is present in source; the complete pending-work teardown matrix was not run.

Apple documents `targetTimestamp` as the deadline to call the update drawable's `present()` and says the GPU may finish afterward according to preferred frame latency. The worker checks that deadline before encoding and immediately before its direct `drawable.present()` call; it commits rendering work before that call, consistent with the display-link callback guidance. [`targetTimestamp`](https://developer.apple.com/documentation/quartzcore/cametaldisplaylink/update/targettimestamp) · [`metalDisplayLink(_:needsUpdate:)`](https://developer.apple.com/documentation/quartzcore/cametaldisplaylinkdelegate/metaldisplaylink%28_%3Aneedsupdate%3A%29)

## Application Present semantics and progression

**VERIFIED by source inspection:** `Present1` validates the sync interval, handles `DXGI_PRESENT_TEST` without presenting, flushes and returns `DXGI_STATUS_OCCLUDED` when appropriate, and otherwise records the existing DXMT frame-latency fence and commits the application chunk. It unlocks the device mutex before `PresentBoundary()` and returns the DXGI result; it does not wait for actual scanout or for synthetic generation. The renderer worker does not call DXGI `Present1` and does not increment the application's Present count.

The implementation preserves DXMT's existing app-side frame-latency path. `SyncFrame()` waits on the existing per-swapchain CPU fence when the frame-latency limit requires it; `GetFrameLatencyWaitableObject()` duplicates the existing semaphore only when the swapchain flag is set; and the app command chunk releases the semaphore. The worker's display refreshes do not signal that app-facing semaphore. The waitable-object behavior was not separately exercised by the test app.

`GetCurrentBackBufferIndex()` in this DXMT revision is hard-coded to return zero (the source has a TODO for sequential swapchains). The instrumented app queried it before and after each Present: all 1,801 values were zero, `GetBuffer(0)` succeeded every time, and all Present HRESULTs were `S_OK`. This confirms no observed change to the current DXMT app-facing index, but it does not test flip-buffer progression. Resize ordering is implemented as above; dynamic resize itself was not exercised during the 60-second run.

**VERIFIED in the final run:** 1,801 application Presents over 59.987 seconds (30.023 Hz), zero Present HRESULT errors, zero `GetBuffer(0)` query errors. The native worker separately logged 3,438 drawables with positive `presentedTime`; those are renderer presentation events, not extra DXGI Presents.

The final run used the staged engine and the app binary above with `DXMT_FRAMEGEN_ENABLE=1`, `DXMT_FRAMEGEN_RUN_SECONDS=60`, and `DXMT_FRAMEGEN_LOG` pointed at the retained `native.csv`, through `tools/dxmt/run_d3d11_baseline.sh` with the disposable prefix `/tmp/dxmt-step10b1-accept-prefix.QRZKXlAO/prefix`.

## Pairing, readiness, and 60-second results

The scheduler retains exact pair keys `(A.id, B.id)`, submits only adjacent IDs, and selects a generated texture only after Metal reports generation complete and its ready time is before the pair's scheduled deadline. The source period is initialized to 33.333 ms and is fixed for this controlled 30 FPS app; adaptation to variable source cadence was not implemented. A smoothed display phase supplies lookahead so the B source can arrive before the midpoint output is due.

The final run's analyzer sorts source and generated feedback by normalized `presentedTime`, not worker submission sequence:

| Measure | Result |
|---|---:|
| App Presents | 1,801 / 59.987 s = 30.023 Hz |
| Successful history copies | 1,801 |
| Display-link driven positive `presentedTime` events | 3,438 = 57.358 Hz |
| Source / generated displayed events | 1,796 / 1,642 |
| Exact adjacent pair requests / malformed requests | 1,799 / 0 |
| Malformed displayed pair IDs | 0 |
| Immediate A→G(A,B)→B triples | 1,601 of 1,642 generated displays |
| Generated displays missing a visible A or B endpoint | 41 (20 missing A, 21 missing B) |
| Source regressions / repeated displayed sources | 0 / 17 |
| `presentation_dropped` / `generated_dropped` | 149 (148 `presentedTime == 0`, 1 pre-present deadline drop) / 34 |
| Expired display ticks | 5 |

The 41 incomplete triples have correct logged adjacent pair IDs, but the corresponding neighboring source endpoint was not positively timestamped immediately before or after G. They therefore do not count as correctly bracketed output. Apple's `presentedTime` is zero when the drawable was not presented or its frame was dropped; the trace uses positive times for display-order analysis. [`presentedTime`](https://developer.apple.com/documentation/metal/mtldrawable/presentedtime)

| Timing metric | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| Display interval | 16.666 ms | 45.609 ms | 47.010 ms | 94.875 ms |
| History-copy completion from source timestamp | 1.937 ms | 3.256 ms | 9.369 ms | 64.081 ms |
| Generation submit to ready | 15.819 ms | 17.059 ms | 19.091 ms | 70.158 ms |
| Presentation GPU submit to completion | 0.817 ms | 1.522 ms | 2.163 ms | 13.986 ms |
| Deadline slack at presentation submit | 16.122 ms | 16.560 ms | 16.594 ms | 16.616 ms |
| Display callback duration | 10.833 µs | 24.209 µs | 41.125 µs | 1,117.041 µs |
| Tick queue depth | 0 | 0 | 0 | 2 |

This misses a 60 Hz target and contains long output intervals and dropped presentations. Average rate alone is not sufficient for acceptance.

## Failure injection

Nine short runs each made 91 application Presents at about 30 Hz, returned `S_OK`, and had no `GetBuffer(0)` query errors. That verifies app/API progression only; it does not mean every accepted source was displayed.

| Injection | Observed native result | Assessment |
|---|---|---|
| Allocation / copy preflight | `history_copy_failed`, then ordinary app Present processing continues | The injected branch runs before Metal allocation/copy encoding; actual allocator exhaustion and an actual failed blit were not forced. |
| Async copy completion | One asynchronous copy failure opens the circuit immediately | **BLOCKER:** the accepted source's normal Presenter call was already suppressed, so that first source can be lost; no replay exists. |
| Generator submission | Injected submission failures open the circuit; app API calls continue | Source fallback is exercised, but this does not address history-copy failure. |
| Synthetic generation | Generator failure opens the circuit; app API calls continue | No app-call deadlock observed. |
| Generated completion | Injected completion errors retire generated results and open the circuit | App API calls continue; only three positively timestamped source outputs were observed in this short case. |
| Drawable unavailable | On the final-build recheck, 3 nil-drawable ticks were logged and opened the circuit; only 2 sources had been accepted and none had a native displayed event | **BLOCKER:** two app source Presents were suppressed before the drawable failure caused fallback. The worker was corrected to run native selection before discarding a nil-drawable tick. The three-second recheck still demonstrates source-loss behavior. |
| Expired generation deadline | 94 generated outputs were dropped, no generated output was displayed, and 168 sources had positive presentation feedback | Late G is dropped; output pacing still had 12 presentation drops. The injection forces the deadline-selection failure branch rather than waiting for a naturally expired generation. |
| Worker shutdown with work pending | Worker stopped after a submitted presentation; app completed 91 Presents and exited 0 | No hang observed for this one shutdown timing. Other pending-copy/generation/drawable-wait teardown states were not separately synchronized and tested. |

## Lifecycle, HUD, and performance boundaries

- **VERIFIED:** the 60-second app and all short fault-injection app runs exited successfully. The worker-shutdown injection did not hang.
- **VERIFIED in source, NOT TESTED at runtime:** resize/fullscreen transitions serialize through the device mutex and drain DXMT GPU work before epoch reset; teardown invalidates the display link and waits for callback drainage.
- **IMPORTANT unresolved lifecycle limitation:** when a Present is unsupported or generation is disabled, the normal path stops the session to avoid the observed `nextDrawable()` crash while a display link is active. That stop is not restartable for the same swapchain; the code's later “re-enter” branch cannot restore a stopped display link. Dynamic disable/enable, focus recovery, fullscreen/windowed transitions, swapchain recreation, and resize were not run.
- **NOT TESTED:** two simultaneous swapchains; Metal HUD interval evidence (no HUD telemetry export was available, and screen capture is prohibited by the task); CPU overhead; memory use; retained texture/ticket growth; GPU overhead compared with baseline; a separate “infrastructure enabled, generation disabled” performance run.
- The pre-edit baseline only records successful app execution. The synthetic run supplies source/display rates and GPU/callback timings, but there is no comparable baseline CPU/GPU/memory dataset.
- No 15-minute soak was run because the mandatory 60-second acceptance run failed.

## Independent review findings

Nine independent GPT-6 Luna Extra High reviews covered the requested areas. The orchestrator reviewed the downstream diff and ran `git diff --check` and patch applicability validation.

| Review scope | Classification and disposition |
|---|---|
| DXGI Present and frame latency | **BLOCKER — unresolved:** accepted Present suppression precedes async copy success; failure can lose the source. |
| Display worker concurrency | **IMPORTANT — addressed in code:** device-mutex ordering and teardown drain were tightened; complete transition/teardown runtime coverage remains missing. |
| WineMetal native bridge | **IMPORTANT — addressed in code:** callback invalidation and in-flight callback drainage precede state release. The nil-drawable early-exit gap was fixed and rechecked. |
| Metal resource lifetime | **IMPORTANT — addressed in code:** source history owns distinct private textures and pending GPU work is retained through completion. Full lifecycle stress remains untested. |
| Framegen ticket/completion lifetime | **IMPORTANT — addressed in code:** both endpoints are retained while generation is pending; retired work is fenced by status and epoch. |
| Timestamp/deadline correctness | **IMPORTANT — addressed in code:** CA timestamps map into mach nanoseconds and the worker checks the presentation deadline immediately before calling `present()`. Deadline stress still fails acceptance. |
| A/G/B pairing | **BLOCKER — unresolved acceptance result:** pair IDs are adjacent and well formed, but 41 generated outputs lack both positively timed immediate endpoints. |
| Lifecycle/teardown | **IMPORTANT — unresolved:** unsupported/disabled transitions do not restart the display link; comprehensive resize/fullscreen/recreate tests were not run. |
| Fail-open behavior | **BLOCKER — unresolved:** asynchronous history-copy and missing-drawable faults can suppress already accepted app sources. |

## Recommendation

Keep this local patch as a diagnostic prototype and do not begin RIFE. The next step should fix the accepted-source ownership contract so the original source remains presentable until its history copy succeeds (including a replay/fallback path on asynchronous failure), make display-link stop/restart transitions real, and stabilize immediate A/G/B presentation. Then rerun the complete failure matrix, resize/focus/fullscreen/recreation and pending-work teardown cases, and a fresh 60-second run. Start the 15-minute soak only after those gates pass.

**Final classification: FAIL.** The prototype proves that DXMT can feed its source textures through a native display-tick path, but it does not yet safely preserve source presentation or sustain correct 30→60 ordering.
