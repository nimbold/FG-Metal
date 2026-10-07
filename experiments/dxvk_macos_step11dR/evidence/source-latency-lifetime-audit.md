# Step 11D.1 — R5 Source-Latency and Lifetime Audit

**Disposition: PARTIAL / runtime gates remain open.** This audit is source review, not a runtime PASS. I did not launch or alter a Wine prefix.

## Candidate identity checked

- The candidate source used by `/private/tmp/fgmetal-step11d-r-current-r5-build-A-20261006/compile_commands.json` is `/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006`.
- The built D3D11 DLL hashes to `e747ca5a5c2087d1f788762a5c4bae13939593a2cb89dedf2f7c04999ab94de5`, matching the frozen R5 identity.
- The built DXGI DLL hashes to `58e972688c739ee8444c8efa7a175503ff8dd0774ceb63e9b8526168054d9132`, also matching.
- I did not independently reproduce the supplied source-tree digest or locate the PE bridge/native-provider binaries to rehash them.
- The existing `~/Library/Caches/FGMetalStep11D-R` prefixes contain different D3D11/DXGI hashes from the specified R5 DLLs. Their existence alone does not prove an R5 runtime. No runtime log in `experiments/dxvk_macos_step11dR` currently binds a test run to the specified R5 binaries and bridge/provider identities.

## Source Present and optional-work coupling

R5 has no direct source-Present CPU wait for Metal completion in the normal path:

- `D3D11SwapChain::Present` wraps `PresentImage` and the application-facing latency operations in [d3d11_swapchain.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/d3d11/d3d11_swapchain.cpp:268).
- `PresentImage` queues a CS callback; that callback records HISTORY and later calls `flushCommandList` and queues the source present. `FlushCsChunk` transfers the chunk to the CS worker and does not wait for Metal completion ([d3d11_context.h](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/d3d11/d3d11_context.h:1240)).
- The native `commit` is performed on the submission worker after the source submit/present entry, not by `D3D11SwapChain::Present` ([dxvk_queue.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_queue.cpp:545)). `pollConsumerStatus` is also outside the application Present call and checks completion without submitting a DONE wait when Metal is pending or failed ([dxvk_queue.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_queue.cpp:1017)).
- Internal finish entries are excluded from `applicationQueueLoad()`, so optional completion backlog does not itself consume source admission capacity ([dxvk_queue.h](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_queue.h:401)).

This establishes the **static hard invariant** for the ordinary path. It does not establish that Metal work never indirectly increases later source latency through CS backlog, the Vulkan queue mutex, WSI acquisition, app frame-latency pacing, or operating-system scheduling. There is also a source-priority race worth measuring: the scheduler checks for source work before acquiring an internal WSI image, but a source submit can arrive while the internal command list is recorded and before the internal worker owns `m_mutexQueue`; the second-stage code at [dxvk_queue.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_queue.cpp:1037) does not recheck the source queue before submitting the internal list.

## Timing-tail evidence gap

With `DXVK_INTERNAL_WSI_TRACE=1`, R5 records only one total `D3D11SwapChain::Present` duration per ordinary return (`duration-us`, `AppFrameId`) at [d3d11_swapchain.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/d3d11/d3d11_swapchain.cpp:329). It does not timestamp the WSI acquire, queue-lock wait, Metal bridge commit/query, internal slot state, source command recording/submit, swapchain recreation, or lifecycle transitions against that same frame. Early returns for test-present and failed device status also bypass that trace. The trace writes a log line every frame, so it is not suitable as unqualified low-overhead tail evidence.

Interop logs already carry useful IDs and partial durations (`AppFrameId`, `InteropJobId`, generation, slot/serial, READY/DONE/CONSUMED, copy-to-ready, ready-to-commit, capture-to-terminal/retire), but do not provide a joined stage timeline for every slow source Present. No >5/10/25/50/100 ms outlier list, p50/p95/p99/p99.9/max comparison, or causal explanation is present. Required comparison remains **UNVERIFIED** for SOURCE ONLY, Vulkan clear, and Metal roundtrip.

For the next trace-only build, use a fixed-size in-memory event buffer and dump after the run or only for slow frames; do not synchronously log each Present. Record monotonic stage timestamps plus IDs/state at: app Present entry/exit (including error paths), WSI acquire, CS callback dispatch/record/flush, queue admission and `m_mutexQueue` waits, source submission/present, provider commit/query, swapchain lifecycle, and retirement. Include a snapshot-valid bit so sampling never blocks a live queue or holds an interop lock. Keep a separate R5 binary run with trace disabled for cadence acceptance.

## Generation invalidation

The source has several sound static guards: each internal request carries the job generation; it is checked before WSI acquire and before consumer submit; the Presenter validates the acquired lease against the current generation; and the output conversion is submitted while holding external Vulkan queue synchronization. Old slots are marked retired when a new generation is created and are erased only after they are no longer busy ([dxvk_metal_interop.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_metal_interop.cpp:195), [dxvk_queue.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_queue.cpp:734), [dxvk_presenter.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_presenter.cpp:470)).

These guards are **not runtime evidence** for resize during HISTORY writing, METAL_IN_FLIGHT, READY_FOR_VULKAN, or Vulkan consumption. R5 has no Metal-specific gate at those states. The existing test gates are WSI queue stages (`armed`, `claimed`, `image-acquired`, `command-recording`, `gpu-pending`, `present-pending`, `submission-queued`, `retirement-pending`); they cannot prove all seven Metal-slot shutdown states.

## Normal shutdown and final-job drain

The R5 shutdown order is structurally plausible:

1. `DxvkDevice::beginShutdown` closes interop admission and internal scheduling.
2. The non-worker owner closes source admission and drains/joins the submission workers; worker self-join terminates instead of deadlocking.
3. `DxvkMetalInterop::shutdown` waits Vulkan idle, terminalizes remaining jobs, polls/releases committed native jobs, calls provider shutdown, then clears exported slot references.
4. Presenter roots and worker threads are retired before the device root is released.

Relevant code: [dxvk_device.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_device.cpp:76), [dxvk_queue.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_queue.cpp:188), and [dxvk_metal_interop.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_metal_interop.cpp:1173). D1's generic nine-stage shutdown evidence from Step 11C.1R2 does not cover the seven requested interop slot states.

R5 stores exported Metal texture/event handles as raw integer fields. The slot destructor destroys its Vulkan semaphore; native-provider shutdown is the visible provider cleanup boundary. The provider implementation and a per-object retain/release ledger are not present in this source snapshot, so release of every exported Objective-C reference is **not independently verified here**.

The harness still needs a last-Present stop boundary followed by normal COM/device teardown and a ledger assertion that all committed jobs terminalized and all submitted Vulkan consumers retired. No arbitrary delay is needed: explicitly release the renderer/device after the final source Present, let the normal owner-side drain run, and collect the complete terminal/retirement log after process exit. Existing final-job incompleteness has not been closed by the evidence in this directory.

## Process termination and lifecycle scope

The D1 process-lifetime registry is intentionally heap-rooted, and the process-detach branches skip normal joins/Metal shutdown so OS teardown reclaims process-lifetime resources ([dxvk_process_lifetime.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_process_lifetime.cpp:26), [dxvk_device.cpp](/private/tmp/fgmetal-step11d-r-source-snapshot-current-r5-20261006/src/dxvk/dxvk_device.cpp:91)). Prior D1 offset runs were source-only. No active-interop process-termination offsets were verified here.

The Step 11C.1R2 30 Hz lifecycle, 30 Hz source soak, and nine-stage normal shutdown results are useful regressions but are not R5 Metal-roundtrip lifecycle/shutdown evidence. Resize/fullscreen/windowed/minimize/restore at 30 Hz with R5 interop, and the 5-minute R5 roundtrip soak, remain **UNVERIFIED**.

## Required closure items for this track

- Bind each runtime run to the exact R5 D3D11/DXGI DLLs, PE bridge, and native provider actually loaded.
- Capture minimally perturbing per-frame/stage timing and explain every source-present tail above the requested thresholds; compare the three path controls.
- Add deterministic test-only gates for FREE, VULKAN_WRITING_HISTORY, READY_FOR_METAL, METAL_IN_FLIGHT, READY_FOR_VULKAN, VULKAN_CONSUMING, and RETIRING. Test normal explicit teardown from a non-worker owner at each gate and prove all slot/job/WSI retirement invariants.
- Repeat generation invalidation at the four requested active states and 30 Hz lifecycle transitions with source continuation.
- Run process termination offsets with actual Metal work active and verify no normal teardown, self-join, hang, or termination.
- Close the final-job drain with explicit normal teardown and post-run ledger readback, then run the minimal-instrumentation 5-minute soak.
