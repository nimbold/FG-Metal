# WSI owner review

Scope: read-only review of `metalsharp/DXVK-MacOS` at `8d348236e14a3db25ffbe528a83010b3dd69a3ef`, cross-checked against `audits/bookkeeping.md`. No source or runtime files were changed. Gate 1 remains failed for the black Wine/DXVK output, so this is a design review only; it does not authorize Gate 2 implementation. The user reports that the separate native Vulkan red/green clear control renders correctly, which supports isolating the failure to the Wine/DXVK path but does not change the gate result.

## Verified current ownership

The existing source path is split across threads. `D3D11SwapChain::PresentImage` calls `Presenter::acquireNextImage` on the application thread (`src/d3d11/d3d11_swapchain.cpp:430`). That method takes `m_surfaceMutex`, waits until `m_presentPending` is false, updates or recreates the swapchain, selects `m_semaphores[m_frameIndex]`, acquires `m_imageIndex`, returns the image and semaphores, and sets `m_presentPending` (`src/dxvk/dxvk_presenter.cpp:78-168`). The lock is released when acquire returns, while the acquired image lease remains outstanding.

Later, the command-stream submission reaches `DxvkSubmissionQueue::submitCmdLists`. Its submit thread holds `m_mutexQueue` while it submits command lists or calls `Presenter::presentImage` (`src/dxvk/dxvk_queue.cpp:154-181`). `presentImage` reads and advances the Presenter semaphore ring, presents the shared `m_imageIndex`, pushes a completion record, and on success pre-acquires the next image. It then clears `m_presentPending` under `m_surfaceMutex` and wakes the application thread (`src/dxvk/dxvk_presenter.cpp:171-333`). Swapchain recreation is reached from Presenter paths protected by `m_surfaceMutex`; `destroySwapchain` drains the present-wait frame queue and waits the per-slot fences before destroying the images, semaphores, and swapchain (`src/dxvk/dxvk_presenter.cpp:589-607`, `1880-1911`).

The frame worker is a retirement observer, not a second producer: it waits for present completion and advances `m_lastCompleted` / the shared signal (`src/dxvk/dxvk_presenter.cpp:1978-2055`). Its `pushFrame` producer blocks when the fixed frame queue is full (`:1959-1975`). An internal request routed through this blocking path could therefore stall its caller.

## Why the current mutexes do not make an internal path safe

`m_surfaceMutex` protects selected surface/configuration operations and the source-path condition-variable handoff. It does not stay held from acquire through render submission and present, and `presentImage` mutates the image/semaphore indices outside it. `m_presentPending` works as a one-outstanding-source-present handshake because the current API flow acquires on the app thread and presents on the known submit thread. A second requester that calls acquire or changes the indices can race the returned lease, choose the same semaphore slot, or overwrite the image identity before the first record is presented.

`m_mutexQueue` serializes calls using the graphics `VkQueue`, including command submission and `vkQueuePresentKHR`. It does not serialize `vkAcquireNextImageKHR`, per-image/per-semaphore ownership, `m_imageIndex`/`m_frameIndex` transitions, swapchain generation changes, frame-queue admission, or the lifetime of an acquired image between acquire and submit. A lock around each raw Vulkan call would still be insufficient: the required unit is the entire lease state transition from selecting an available slot through terminal present/retirement, including recreation.

## Owner and drop-G design

Route both source presents and generated clears through one Presenter-owned WSI request service, with Presenter as the only owner allowed to mutate swapchain generation, acquired image, semaphore ring, or WSI IDs. A request record should contain kind (`SOURCE` or `INTERNAL`), its app/internal identity, WSI ID once reserved, generation, image index and sync slot once leased, and a terminal result/retirement state. The owner performs this sequence atomically with respect to competing WSI requests:

1. Select the next request, always giving an accepted source request priority over queued internal work.
2. Acquire an image and grant its lease to exactly that record; no caller may retain the current global `m_imageIndex` as an implicit lease.
3. Submit the record's output work on the graphics queue, present that same leased image, advance the associated ring state, and retain the record until its GPU/WSI resources are retired.
4. Recreate only at an owner-controlled generation boundary: stop admitting internal records for the old generation, cancel queued stale G work, retire or explicitly release every acquired/submitted lease, drain old present waits/fences, then publish the new generation.

Keep the frame worker for asynchronous completion observation, but hand it immutable records keyed by WSI ID and generation. It must not infer ownership from mutable current swapchain indices. Preserve the separate `AppFrameId`, `WsiPresentId`, and `InternalPresentId` contract in `bookkeeping.md`; only source records update DXGI counts/statistics, source backbuffer rotation, latency tracking, and the app frame-latency signal.

Make G admission a bounded `tryEnqueue`: if a source request is active/pending, the owner is unavailable, the internal queue is full, or the request's generation is stale, drop that G immediately. Do not wait on `m_presentPending`, `m_frameDrain`, an infinite-timeout acquire, present completion, or the app fence for internal work. When servicing G, use a nonblocking/short-timeout acquire and drop on `VK_NOT_READY`/timeout. Once an image has been acquired, the record owns a real WSI lease and cannot simply be forgotten; every post-acquire failure or cancellation needs a defined release/retirement route before the slot can be reused. Bound generated command work, and never make a source Present wait for G scanout or G completion. Replace or bypass `pushFrame` for internal admission so queue pressure drops G instead of blocking the producer.

This design is supported by the pinned source flow, but no timing, starvation, recreation, error, or shutdown behavior has been tested. Gate 1's black Wine/DXVK result remains the blocker for implementation.
