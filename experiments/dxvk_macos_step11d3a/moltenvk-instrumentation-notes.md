# MoltenVK native WSI instrumentation notes

The patch targets the clean MoltenVK checkout at commit `db66022459ffb663aa2b50f6b018bc2e124f5edf` (v1.4.2). It was generated from `git show HEAD:<path>` text without editing the pristine checkout. Apply it at the MoltenVK repository root. Set `FG_NATIVE_MVK_TRACE` to a JSONL output path to enable logging; tracing is disabled when the variable is absent or empty.

Patch artifact SHA-256: `a9795dae571d4ea50cede0c50973b6f2298c1e7caeb19be638697114936d1cef`.

## Event coverage

The patch records `queue_submit_begin` / `queue_submit_end` for `vkQueueSubmit` and `vkQueueSubmit2`; `acquire_begin` / `acquire_end` for `vkAcquireNextImageKHR` and `vkAcquireNextImage2KHR`; and per-image `present_request` events at `vkQueuePresentKHR`. `acquire_image_assigned` records the exact swapchain image and acquisition sequence while MoltenVK assigns it.

Drawable-side events are `next_drawable_begin` / `next_drawable_end`, `metal_present_request`, `presented_handler_registered`, `presented_callback`, `present_command_buffer_complete`, `drawable_release`, and `make_available`. The simulator-only fallback is named `presentation_completion_assumed` so it cannot be mistaken for an observed presentation callback.

Each row is a raw JSONL event with `origin: "NATIVE_SOURCE"`, atomic `event_id`, `timestamp_ns`, `tid`, pointer identities for swapchain/image/drawable/texture/queue when available, image index, acquisition sequence, Vulkan present IDs when available, result, and optional timing fields. Pointer values are process-local identifiers; do not join them across runs.

## Correlation and clocks

The native trace cannot know the renderer's application frame ordinal, so `frame_index` is emitted as `null`. The native application should log its assigned frame index together with the returned swapchain image index. Join that app row to native events with swapchain identity, image index, and the acquisition sequence from `acquire_end` / `acquire_image_assigned`. Async presentation events capture the sequence and swapchain identity before scheduling; release and availability rows use image identity, index, and sequence because the swapchain may already be detached. Generic `queue_submit_begin` / `queue_submit_end` events carry queue identity and operation timing only; there is no command-buffer-to-image or submit-to-application-frame mapping, so those submit events cannot be correlated to a particular app frame from this trace alone.

`timestamp_ns`, `call_begin_ns`, and `call_end_ns` use `mach_absolute_time()` converted with `mach_timebase_info`. The native app's phase analysis must use that same absolute-time clock domain. `next_drawable_end` carries `call_begin_ns`, `call_end_ns`, and `duration_ns` sampled around the `CAMetalLayer.nextDrawable` property call itself. Submit/acquire/present end rows carry the same fields around the corresponding MoltenVK operation, after the begin event has been written. `event_id` reflects serialized file-write order; under concurrent logging, timestamps can appear out of event-ID order.

## Overhead and limits

The writer uses one process-wide mutex and a 64 KiB buffered `FILE`; it flushes every 64 events and normal process exit flushes the remainder. Each enabled event performs formatting and synchronous serialized file writes, which can delay the thread emitting it. Static source review found no logger-lock cycle, but synchronous logging can still perturb hot paths, including threads that happen to hold driver locks; this contention was not measured. The operation bounds exclude that row-writing time, but the preceding begin-row write can delay when the measured operation starts. This overhead was not measured because the patch has not been built or run. An abnormal process exit can lose fewer than 64 buffered rows.

All event names and detail strings are fixed source literals, so the compact JSON string writer does not escape arbitrary application input. This patch observes drawable acquisition/presentation and public Vulkan entrypoints; it does not add CAMetalLayer configuration-change records or assign application frame indices.

## Validation state

`git apply --check` passes against the pinned clean MoltenVK checkout, and the checkout remains unchanged. The patch has not been compiled, linked, or exercised in a native launch. No storage-aware runner or native run evidence is claimed here; those are separate from patch preparation.
