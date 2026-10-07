# DXGI / WSI bookkeeping split audit

Audit target: `metalsharp/DXVK-MacOS` commit `8d348236e14a3db25ffbe528a83010b3dd69a3ef`. Read-only source audit; no DXVK files were changed. This is a Gate 2 design note only. Gate 2 must remain unimplemented unless Gate 1 passes.

## Finding

The pinned D3D11 path intentionally uses one `frameId` for two roles: the application frame sequence and the Vulkan WSI present ID. That value also keys the Presenter completion signal and the most recent timing feedback. This works while every WSI present comes from an application `Present`. An internal G present inserted into the same sequence would either reuse a Vulkan present ID or, if it advanced the shared value, leak into DXGI counts, timing, and frame-latency signaling.

The clean split is three explicit identities plus a per-present record. Keep an app-only sequence for DXGI and application latency; use one monotonically increasing WSI sequence for every `vkQueuePresentKHR`; use a distinct internal ID for generated requests and their renderer-only completion. Every WSI record carries its kind and maps to exactly one optional app or internal identity. Internal records never update app fields or signal the app fence.

## Current source flow

`D3D11SwapChain::m_frameId` starts at `DXGI_MAX_SWAP_CHAIN_BUFFERS`. After a successful WSI acquire and source-copy setup, `PresentImage` increments it and captures that value into the command-stream closure. `GetLastPresentCount` subtracts the initial bias. The same ID goes into `DxvkDevice::presentImage`, then `DxvkPresentInfo`, `Presenter::presentImage`, the Vulkan present-ID structures, and the queued `PresenterFrame`. Relevant source: [D3D11 swap-chain present path](https://github.com/metalsharp/DXVK-MacOS/blob/8d348236e14a3db25ffbe528a83010b3dd69a3ef/src/d3d11/d3d11_swapchain.cpp#L256), [source image submission and frame increment](https://github.com/metalsharp/DXVK-MacOS/blob/8d348236e14a3db25ffbe528a83010b3dd69a3ef/src/d3d11/d3d11_swapchain.cpp#L411), [Presenter present path](https://github.com/metalsharp/DXVK-MacOS/blob/8d348236e14a3db25ffbe528a83010b3dd69a3ef/src/dxvk/dxvk_presenter.cpp#L171), and [queue forwarding](https://github.com/metalsharp/DXVK-MacOS/blob/8d348236e14a3db25ffbe528a83010b3dd69a3ef/src/dxvk/dxvk_queue.cpp#L170).

The app-facing surfaces are coupled to that sequence:

- `GetLastPresentCount` returns `m_frameId - DXGI_MAX_SWAP_CHAIN_BUFFERS` directly.
- `GetFrameStatistics` currently consumes the Presenter's single `PresenterTimingFeedback.frameId` and converts that ID to a DXGI present count. If it becomes a WSI ID, an internal G can become the newest app statistic.
- `Present` calls `SyncFrameLatency` on success and failure. That waits on `m_frameLatencySignal` and registers a semaphore-release callback at the current app ID. `Presenter::signalFrame` and the frame worker advance one shared `m_lastSignaled` / `m_lastCompleted` pair and signal this callback fence.
- The D3D11 Reflex/built-in latency tracker receives frame IDs around CPU Present, command submission, GPU work, Present, and frame-statistics queries. Internal output must not be assigned to that tracker.
- The DXGI wrapper delegates `GetBuffer`, `GetCurrentBackBufferIndex`, `GetLastPresentCount`, and `GetFrameStatistics` through the D3D11 presenter. Its own `m_presentId` and output frame statistics also advance through `PresentBase`; internal output must not call that front-end path.

There is a serious failure mode if a WSI ID is allowed to signal the existing app fence. `sync::Signal` requires monotonically increasing values. `CallbackFence` runs every callback at or below the value it receives, and `SyncFrameLatency` waits and registers callbacks in the current `m_frameId` domain. A doubled WSI sequence can therefore make later application waits return immediately and release waitable-object callbacks before their application frames complete. Keeping the existing `m_frameId` unchanged while inserting G instead reuses Vulkan present IDs and makes timing / present-wait correlation ambiguous.

## ID and record contract

| Identity | Owner and increment point | May affect | Must not affect |
|---|---|---|---|
| `AppFrameId` | D3D11 swap chain; assigned only when an application source Present reaches the existing accepted-source point after WSI acquire. Preserve the current nonzero base/bias or replace it with an explicit base conversion. | DXGI `GetLastPresentCount`, latest source-only `GetFrameStatistics`, app frame-latency wait/callbacks, source latency tracker, source backbuffer rotation. | WSI uniqueness for internal output or renderer G completion. |
| `WsiPresentId` | Single Presenter/WSI owner; reserve once per actual `vkQueuePresentKHR` attempt, source or internal, and never reuse it within the swap-chain generation. Keep a monotonic allocator across recreation if possible. | `VkPresentIdKHR` / `VkPresentId2KHR`, present-wait, display-timing report correlation, WSI retirement, WSI cadence and WSI-only timing schedule. | DXGI present counts, app latency callbacks, source completion watermark. |
| `InternalPresentId` | Experimental scheduler; assign to each G request accepted into renderer scheduling. It remains local to generated work, including a request later dropped or failed. | G job ownership, diagnostics, internal completion/retirement and drop accounting. | DXGI state, `AppFrameId`, Reflex/NV application reports, app signal/fence, source backbuffers. |

Each submitted output should have a record similar to:

```text
WsiPresentRecord {
  WsiPresentId wsiId;
  Kind kind;                         // SOURCE or INTERNAL
  optional<AppFrameId> appFrameId;
  optional<InternalPresentId> internalId;
  uint64_t swapchainGeneration;
  uint32_t imageIndex;
  queue-submit / present result;
  desired time, actual-present time, margin when available;
  resource-retirement status;
}
```

The record should use an explicit kind/shutdown field rather than overloading `frameId == 0` as both identity and the frame-worker exit sentinel. A dropped G that never calls `vkQueuePresentKHR` gets no WSI ID; a present API call that returns an error still consumes its reserved WSI ID and is recorded, so a later ID is not reused.

## Exact Presenter paths that need the split

1. **Vulkan present IDs and wait:** `Presenter::presentImage` currently writes its `frameId` into `VkPresentIdKHR` and `VkPresentId2KHR`, then the frame worker uses the same value in `vkWaitForPresentKHR` / `vkWaitForPresent2KHR`. These values must become `WsiPresentId` for every source/internal present.

2. **Timing IDs and scheduling:** `VkPresentTimingInfoEXT` computes absolute target time using `(frameId - referenceFrameId) * frameIntervalNs`; `PresenterFrame.frameId` is compared with `report.presentId`; `PresenterTimingInfo.lastFrameId` and the single `PresenterTimingFeedback.frameId` are also these IDs. The WSI timing mode should remain keyed by `WsiPresentId`. Split the returned timing views: internal WSI timing and an app timing record selected only from `SOURCE` records and translated back to `AppFrameId`. `GetFrameStatistics` must read the latter. A G report may advance WSI timing and cadence but cannot become the app's newest statistics result.

3. **Completion and callback signal:** `DxvkSubmissionQueue::finishCmdLists` calls `Presenter::signalFrame` for every present entry. The frame worker later updates `m_lastCompleted` and may call `m_signal->signal(frame.frameId)`. Branch on present kind. Source records may update app completion and signal the existing `m_frameLatencySignal` at the matching `AppFrameId`; internal records update only G completion and WSI retirement. Keep independent `lastAppCompleted`, `lastWsiRetired`, and per-job/last-internal retirement state. Do not compare `AppFrameId` with `WsiPresentId`.

4. **Frame-latency waitable object:** Keep `D3D11SwapChain::SyncFrameLatency` and its `CallbackFence` in the app ID domain. Only accepted app Presents may wait on the app completion watermark and register or release an app callback. An internal completion must never call `signalFrame`, `m_signal->signal`, or invoke the source path's `SyncFrameLatency`.

5. **DXVK latency / Reflex markers:** `DxvkQueue` feeds tracker callbacks and `DxvkCommandSubmission::submit` attaches `VkLatencySubmissionPresentIdNV`; `Presenter::setLatencyMarkerNv` also accepts a present ID. Keep generated requests out of `DxvkLatencyTracker`. For source frames, map `AppFrameId` to the assigned `WsiPresentId` wherever Vulkan NV markers need the actual present identifier, while keeping tracker-local IDs distinct. The current Reflex tracker already maps application frame IDs to its own tracker IDs and translates NV `report.presentID` back to app IDs; that mapping must remain source-only and must be reconciled with the new WSI ID. Do not feed G IDs into the Reflex mapping/report ring.

6. **Source buffers:** The D3D11 implementation's `GetImageIndex()` currently returns zero; `GetBuffer(i)` returns D3D11 texture objects. For sequential swap effects, source Present alone rotates the backing image allocations through `RotateBackBuffers`. An internal request must use a renderer-owned image, and must not call the DXGI `PresentBase`, increment the wrapper `m_presentId`, rotate source backbuffers, or overwrite a source image before its WSI copy has completed.

## One WSI owner

The current implementation is not a single-thread owner for all WSI state. `D3D11SwapChain::PresentImage` asks `Presenter::acquireNextImage` on the application thread. Command-stream work later enters `DxvkSubmissionQueue`, whose submit thread calls `Presenter::presentImage`; that method calls `vkQueuePresentKHR`, advances the semaphore ring, and eagerly acquires the next image. `m_presentPending` plus `m_surfaceMutex` / `m_surfaceCond` serialize the existing source path, while `m_mutexQueue` serializes queue submit/present calls. They do not make a second raw-Vulkan internal presenter safe: acquire, present, `m_imageIndex`, `m_frameIndex`, `m_acquireStatus`, `m_semaphores`, and swap-chain recreation form one mutable state machine.

The downstream proof should route both `SOURCE` and `INTERNAL` requests through one Presenter-owned WSI request service. That owner alone changes swap-chain generation, acquires images, grants an image lease to the corresponding render/clear request, submits the output work on the graphics queue, presents it, advances the semaphore/image ring, and drains old-generation retirements before recreation. No scheduler or second present thread should call raw Vulkan WSI functions. Existing frame-worker timing/retirement may observe owner records, but it must not perform independent acquire/present operations.

An application source request keeps its current priority and may wait for its own WSI lease as the current path already does. An internal request uses a bounded, nonblocking enqueue/availability check. If the owner is serving a source request, the internal queue is full, the image is unavailable, or its generation is stale, drop G immediately. Source submission must not wait for a generated display opportunity. Split the current unconditional blocking `pushFrame` behavior for internal work: an internal record queue overflow drops the G request; it cannot make the game Present block.

## Invariants to carry into an eventual proof

- `AppFrameId` advances only at the source-app Present acceptance point. `WsiPresentId` advances for each WSI call regardless of kind. No counter aliases another.
- App-facing count, statistics, waitable-object callback, completion watermark, Reflex/NV reports, and source backbuffer progression change only for source records.
- Each WSI image has a single owner state (`Available`, `Acquired(record)`, `Submitted(record)`, `Presented(record)`, `Retired`). It cannot be acquired twice or presented by a different record. A queued request owns its image/semaphores until a terminal retirement/error path.
- A source image is copied/submitted before the source app is allowed to reuse the backing allocation. An internal job writes only the Vulkan-owned G image and its acquired WSI image.
- Every queued record holds its swap-chain generation; recreation stops accepting internal work, invalidates queued old-generation G work, retires acquired/submitted WSI resources, then resumes under a new generation. Source Present remains safe across the transition.
- Failure to prepare, submit, present, time, or retire a G record drops that record, releases any acquired WSI image through a defined path, and does not advance app counters or signal the app fence. Source failures retain the existing DXGI result and semaphore-release behavior.
- Shutdown has a typed drain/cancel policy for an internal request before acquire, after acquire, after submit, and while present retirement is pending. It cannot depend on the app frame-latency fence to retire internal records.

## Classification

- **Verified from source:** the D3D11 path increments the app present sequence once in `PresentImage`, exposes it through `GetLastPresentCount`, sources `GetFrameStatistics` from one shared Presenter feedback record, and uses the same `frameId` for WSI present IDs, present waits, timing correlation, latency calls, and completion signaling. Current WSI operations span the app thread and submission queue thread.
- **Supported design inference:** an internal output can be isolated if records carry kind plus separate app/WSI/internal IDs, source callbacks and statistics filter strictly to source records, and one WSI owner serializes all raw WSI state.
- **Not tested:** any Gate 2 implementation, app-invariant behavior, 30→60 cadence, Vulkan display timing, windowed/fullscreen behavior, recreation, failure injection, or shutdown. The source audit does not authorize or justify proceeding before Gate 1 passes.
