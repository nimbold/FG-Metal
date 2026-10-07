# Step 11B Subagent 6 — Vulkan WSI serialization design audit

## Scope and status

Read-only audit of `metalsharp/DXVK-MacOS` pinned at `8d348236e14a3db25ffbe528a83010b3dd69a3ef`, checked out under `/tmp/fgmetal-step11b-dxvk-source`. The checkout is clean. No DXVK source was changed and no WSI experiment was run. Gate 1 failed visual validation, so this is a future design audit only; Gate 2 implementation must remain stopped.

## Existing owners and state

The pinned Presenter is built around one shared image lease, not independent source and internal streams:

- `Presenter::acquireNextImage` runs on the D3D11 application's Present thread (`src/d3d11/d3d11_swapchain.cpp:427-430`). It takes `m_surfaceMutex`, waits for `!m_presentPending`, and then uses the global `m_frameIndex`, `m_imageIndex`, `m_acquireStatus`, and `m_semaphores` state (`src/dxvk/dxvk_presenter.cpp:78-105, 125-167`). Acquires use an infinite timeout. On successful acquisition, it sets the single `m_presentPending` flag.
- The D3D11 caller captures the acquired WSI image and semaphore in its source blit command-stream job. That job blits the app backbuffer into the WSI image, calls `synchronizeWsi`, flushes the command list, and queues the Present (`src/d3d11/d3d11_swapchain.cpp:480-530`). Backbuffer rotation is queued after that source-copy job (`:532-535`).
- One `dxvk-submit` worker processes both Vulkan command submissions and Presenter presents. It holds `m_mutexQueue` around the queue submit and `Presenter::presentImage` call (`src/dxvk/dxvk_queue.cpp:131-185`). `presentImage` calls `vkQueuePresentKHR`, advances the global semaphore ring on success, pre-acquires the next image with an infinite timeout, then clears `m_presentPending` (`src/dxvk/dxvk_presenter.cpp:265-333`).
- Swapchain creation/destruction and surface recreation are coordinated by `m_surfaceMutex`/`m_presentPending`, with frame-thread drainage and swapchain-fence waits in teardown (`src/dxvk/dxvk_presenter.cpp:390-400, 589-623, 1874-1924`). The separate `dxvk-frame` thread waits on present IDs for feedback (`:1975-2049`). It is not a safe second WSI owner.
- `DxvkSubmissionQueue::finishCmdLists` routes every present entry through `Presenter::signalFrame` (`src/dxvk/dxvk_queue.cpp:283-288`), so any internal record also needs a separate completion path; this finding overlaps the identity/completion audit.

The raw WSI call sites in the pinned DXVK source are exactly three `vkAcquireNextImageKHR` calls, one `vkQueuePresentKHR`, one `vkCreateSwapchainKHR`, and one `vkDestroySwapchainKHR`, all in `src/dxvk/dxvk_presenter.cpp` (acquire lines 103, 127, 314; present 265; create 878; destroy 1900). Today these calls are split across the app thread, submit worker, and whichever thread enters recreation/destruction. The locks make the current single-source path work; they do not define a second independent acquired-image lane.

Vulkan requires host access to the swapchain and acquire semaphore/fence to be externally synchronized for acquire, queue access to be externally synchronized for present, and all acquired-image uses to complete before swapchain destruction. Present also releases image acquisition, after which that image cannot be reused until reacquired. See the [Vulkan `vkAcquireNextImageKHR` reference](https://docs.vulkan.org/refpages/latest/refpages/source/vkAcquireNextImageKHR.html), [`vkQueuePresentKHR`](https://docs.vulkan.org/refpages/latest/refpages/source/vkQueuePresentKHR.html), and [`vkDestroySwapchainKHR`](https://docs.vulkan.org/refpages/latest/refpages/source/vkDestroySwapchainKHR.html).

## Recommended single-owner shape

Use one DXVK-owned WSI executor/coordinator as the sole owner of the Presenter's swapchain state and every raw WSI call. The natural integration point is the existing submission coordinator because its `dxvk-submit` worker already serializes `vkQueueSubmit` and `vkQueuePresentKHR` behind `m_mutexQueue`. Keep `Presenter` as the state owner; do not add another thread that directly calls acquire or present. Route source and internal requests through one bounded mailbox with source priority.

Represent acquired state explicitly as a lease/ticket rather than reading the current global indices later. A ticket should retain the swapchain generation, WSI image index, semaphore/fence slot, imported `DxvkImage`, lease kind (`SOURCE` or `INTERNAL`), and the associated app/internal IDs. The owner alone creates, acquires, submits against, presents, advances the ring, and retires those tickets. It must reject stale-generation tickets. In the existing source path, application image acquisition is synchronous and the returned image is captured by the D3D11 blit job; moving the Vulkan call to the owner therefore requires a source-acquire request/result handshake, while keeping source-copy and source-present ordering intact.

The owner should service requests in this order:

1. A queued source acquire or source-present continuation always wins; stop accepting internal work while a source transaction is active.
2. Run already-ordered application command submissions and source-copy jobs without inserting an internal job before the source copy or source present that consumes its lease.
3. Consider one internal request only when no source acquire is waiting, no source lease is active, the source image and queue resources are safe, and the internal output is already prepared. Use nonblocking/bounded internal resource acquisition; on no image, queue pressure, stale generation, or deadline miss, drop the internal request.

The internal request must never wait for a future display opportunity. A pending internal request cannot hold the owner in a wait; the executor must wake for source work and always recheck source priority immediately before beginning internal WSI work. Vulkan itself permits `vkQueuePresentKHR` to block for a finite time, so source priority cannot preempt an internal Vulkan call once entered. This is a residual bound to measure in any future Gate 2 run. The design can prevent waiting behind a queued G opportunity and can ensure a failed/unavailable G is dropped, but it cannot promise zero latency from an already executing WSI call without changing the source API scheduling contract.

Keep source rendering independent of internal output. Source jobs continue to capture/copy the app backbuffer before the D3D11 backbuffer ring rotates. Internal rendering targets its own synthetic Vulkan image or a separately acquired WSI image and cannot overwrite a source image or use a source lease. The internal record must have a separate completion/retirement path and must not enter app timing, latency, frame-ID, or signal paths.

## Recreation and shutdown ordering

Treat every resize, fullscreen transition, surface invalidation, and out-of-date result as a generation boundary owned by the same executor:

1. Close internal admission and increment/mark the generation as retiring.
2. Drop queued internal jobs from the old generation. Allow source requests to continue under DXVK's established HRESULT behavior.
3. Complete or safely retire every acquired source/internal ticket and its command submission. An acquired internal image cannot simply be abandoned; present it through the owner or use a supported release operation with valid synchronization. The pinned source currently does not call `vkReleaseSwapchainImagesEXT`.
4. Drain the old present-feedback records and wait for present fences or other valid image-use retirement. Keep the existing `m_frameQueue` drainage semantics. A resource-retirement fence is not evidence of display time.
5. Destroy the old swapchain and imported image wrappers only after all uses finish, recreate the surface/swapchain and per-image sync state atomically, reset indices, then publish the new generation and reopen source/internal routing.

Shutdown follows the same owner protocol: close internal admission, drop unacquired G requests, finish or retire acquired internal tickets, drain submitted work and presentation records, destroy swapchain/surface resources, then stop worker threads. A queued internal request must never keep teardown waiting for a scheduler deadline.

## Queue-drain caveat

Do not call the existing `DxvkDevice::waitForIdle()` from a swapchain-recreation item running on `dxvk-submit` without redesigning queue draining. `waitForIdle()` first waits for `m_submitQueue` and `m_finishQueue` to empty, then locks the device queue and calls `vkDeviceWaitIdle` (`src/dxvk/dxvk_device.cpp:694-702`). The submit worker removes its current entry only after processing it (`src/dxvk/dxvk_queue.cpp:149-150, 217-218`), so invoking that helper from inside the worker can wait for the worker's own still-queued entry. The existing comment in `destroySwapchain` also expressly warns against calling it while the submission queue is locked (`src/dxvk/dxvk_presenter.cpp:1874-1879`).

A future owner implementation needs an owner-aware barrier/watermark: drain and retire all queue work preceding the recreation request, prevent later WSI operations from crossing the barrier, then perform the idle/retirement operation without waiting for the current owner entry to remove itself. It must also serialize DXVK's external queue-lock path (`lockDeviceQueue`/`unlockDeviceQueue`, `dxvk_queue.cpp:115-127`) against the same queue. Merely adding another mutex around acquire/present would not solve the self-drain or state-ownership problem.

## Gate 2 design readiness

**Supported design conclusion:** a single owner with explicit source/internal tickets and source-priority dispatch is the minimum coherent model; the current global `m_imageIndex`/`m_frameIndex`/`m_acquireStatus` and `m_presentPending` state cannot safely serve two independently scheduled output streams.

**Unresolved before implementation:** source-acquire handshake details and its HRESULT/occlusion contract; owner-aware queue drain for swapchain recreation; measurable worst-case source delay behind an in-flight internal present; internal acquired-image cleanup when command submission fails; and separate internal finish/retirement records. No code or runtime test resolves these items. Gate 1 failed in this Step 11B run, so Gate 2 was correctly not attempted.
