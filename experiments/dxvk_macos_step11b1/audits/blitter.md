# Step 11B1 audit — one-shot DXVK WSI clear and constant-fragment probes

**Pinned source:** `/tmp/fgmetal-dxvk-macos-audit-20261004` and `/tmp/fgmetal-step11b-dxvk-source`, both at `8d348236e14a3db25ffbe528a83010b3dd69a3ef`.

**Scope:** Read-only source audit of D3D11 present, the acquired WSI image, DXVK external command recording, the swapchain blitter, and submit/present synchronization. No DXVK source, shader, or runtime was changed or run for this audit. No capture or readback was used.

## Finding

The smallest WSI isolation is a single D3D11 `Present` whose queued CS callback clears the already-acquired Vulkan WSI image with `DxvkCommandList::cmdClearColorImage`, then runs the existing `synchronizeWsi` → `flushCommandList` → `DxvkDevice::presentImage` tail. For this W1 probe, **skip `cBlitter->present` entirely**. A clear recorded before the ordinary blit would be overwritten by the blit, so it would not test whether a raw WSI clear reaches the surface.

`Presenter` creates swapchain images with both `VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT` and `VK_IMAGE_USAGE_TRANSFER_DST_BIT` (`src/dxvk/dxvk_presenter.cpp:829-837`) and imports their intended layout as `VK_IMAGE_LAYOUT_PRESENT_SRC_KHR` (`:892-919`). Thus `vkCmdClearColorImage` is supported by the declared image usage. It needs no image view, sampled-image descriptor, shader, or D3D11 source image.

The source path currently flushes app work and acquires the WSI image in `D3D11SwapChain::PresentImage` (`src/d3d11/d3d11_swapchain.cpp:411-440`). It captures `cBackBuffer` as the acquired WSI target view and `cSwapImage` as the D3D11 source view (`:484-508`); these names are easy to confuse. The callback starts external rendering, invokes the blitter, attaches WSI semaphores, flushes, then queues one present (`:517-530`). The target view can be omitted on W1: capture the acquired `Rc<DxvkImage>` directly, and do not call `GetBackBufferView` or source color-space compatibility work for that branch.

## W1 direct clear proposal

Use a conspicuous test color such as opaque magenta. Inside the same callback, after `ctx->beginExternalRendering()`, record the following against the acquired image. This is an API-level sketch; it does not modify the pinned source.

```cpp
auto image = cWsiImage;
auto range = image->getAvailableSubresources();

VkImageMemoryBarrier2 barrier = { VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER_2 };
barrier.dstAccessMask = VK_ACCESS_2_TRANSFER_WRITE_BIT;
barrier.dstStageMask = VK_PIPELINE_STAGE_2_TRANSFER_BIT;
barrier.oldLayout = VK_IMAGE_LAYOUT_UNDEFINED;
barrier.newLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
barrier.image = image->handle();
barrier.subresourceRange = range;

VkDependencyInfo dep = { VK_STRUCTURE_TYPE_DEPENDENCY_INFO };
dep.imageMemoryBarrierCount = 1;
dep.pImageMemoryBarriers = &barrier;
contextObjects->cmdPipelineBarrier(DxvkCmdBuffer::ExecBuffer, &dep);

VkClearColorValue color = { .float32 = { 1.0f, 0.0f, 1.0f, 1.0f } };
contextObjects->cmdClearColorImage(DxvkCmdBuffer::ExecBuffer,
  image->handle(), VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL, &color, 1, &range);

barrier.srcAccessMask = VK_ACCESS_2_TRANSFER_WRITE_BIT;
barrier.srcStageMask = VK_PIPELINE_STAGE_2_TRANSFER_BIT;
barrier.dstAccessMask = VK_ACCESS_2_MEMORY_READ_BIT;
barrier.dstStageMask = VK_PIPELINE_STAGE_2_ALL_COMMANDS_BIT;
barrier.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_DST_OPTIMAL;
barrier.newLayout = image->info().layout; // PRESENT_SRC_KHR for imported WSI images
contextObjects->cmdPipelineBarrier(DxvkCmdBuffer::ExecBuffer, &dep);
contextObjects->track(image, DxvkAccess::Write);
```

The first barrier discards prior WSI contents, as the existing blitter also starts its target transition from `UNDEFINED` (`src/dxvk/dxvk_swapchain_blitter.cpp:76-92`). The destination transfer stage/access make the clear legal. The second barrier returns the image to its imported `PRESENT_SRC_KHR` layout and orders the transfer write before later use; its destination masks mirror the existing blitter's render-to-present dependency (`:129-145`). Keep the imported image's declared layout as `PRESENT_SRC_KHR`; do not leave it in `TRANSFER_DST_OPTIMAL` or mutate the image's stored layout.

Tracking the acquired `DxvkImage` with `contextObjects->track(image, DxvkAccess::Write)` is required to keep the resource alive through asynchronous submission. The ordinary blitter tracks its WSI target similarly (`dxvk_swapchain_blitter.cpp:362-364`). `getAvailableSubresources()` provides the image's full color range (`dxvk_image.h:616-629`).

The callback must retain this exact tail once, after recording the clear:

```cpp
ctx->synchronizeWsi(cSync);
ctx->flushCommandList(nullptr, nullptr);
cDevice->presentImage(cPresenter, cLatency, cFrameId,
  cDirtyRects.size(), cDirtyRects.data(), nullptr);
```

`synchronizeWsi` attaches the acquire/present binary semaphores to the active command list (`dxvk_context.h:107-114`). Submission waits on the acquire semaphore at `TOP_OF_PIPE` on the first submission and signals the present semaphore at `BOTTOM_OF_PIPE` on the last (`dxvk_cmdlist.cpp:327-355`); the WSI submission also uses a follow-up timeline-only submit before reusing semaphores (`:365-374`). The submission queue serializes command-list submission and presentation on its queue (`dxvk_queue.cpp:152-185`). `Presenter::presentImage` passes the present semaphore as `VkPresentInfoKHR::pWaitSemaphores` to `vkQueuePresentKHR` (`dxvk_presenter.cpp:240-266`). Do not add a second WSI acquire/present, custom semaphore, queue-idle wait, or extra flush. The normal Presenter may pre-acquire the next image after a successful present (`dxvk_presenter.cpp:308-317`); that is not a second present.

For a strict one-present diagnostic, use one test-only mode with a disabled/`None` default and consume it only after a successful acquire, once per process. A value such as `clear-once` can select W1. Capture the selected mode by value in the existing CS callback; leave DXGI's ordinary caller-facing `Present`, count/latency handling, backbuffer rotation, and its single existing `presentImage` call intact. If the first Present is occluded or acquisition fails, leave the one-shot pending until an acquired image can actually be tested. With no explicit opt-in, keep the production blitter path byte-for-byte selected.

## Constant-fragment probe after W1

If W1 displays the clear but normal app content remains black, the next smallest step retains the existing blitter's attachment setup, draw, WSI synchronization, and present while removing source sampling/color conversion from the fragment stage. Add a diagnostic fragment shader that writes one constant `vec4` and no descriptors, then select it for one blit only. Keep `dxvk_present_vert` and the existing three-vertex triangle (`src/dxvk/shaders/dxvk_present_vert.vert:3-11`; draw call at `dxvk_swapchain_blitter.cpp:350-357`).

The normal pipeline currently chooses among four fragment modules according to source sample count and scaling (`dxvk_swapchain_blitter.cpp:717-770`), so changing only `dxvk_present_frag.frag` would miss scaled or multisampled cases. A scoped test route should pass a one-shot boolean/mode into `DxvkSwapchainBlitter::present`, select the constant module in `createBlitPipeline`, and include that diagnostic bit in `DxvkSwapchainPipelineKey::hash` and `eq`; otherwise the existing pipeline cache can alias the test pipeline with a normal pipeline. Register the extra GLSL source in `src/dxvk/meson.build:1-27` so the build generates its SPIR-V header. Suppress HUD/cursor composition on that test frame if a uniform target is required; the current blitter can draw HUD/cursor after its main triangle (`dxvk_swapchain_blitter.cpp:119-125`).

This probe still needs the D3D11 source view for the normal `present` setup and can still bind unused descriptors through the existing pipeline layout, but the substituted fragment shader must not read them. The W1 clear is the earlier, cleaner test of the WSI acquire/target/present chain; the constant-fragment case then adds graphics pipeline and attachment coverage without relying on app backbuffer contents.

## Evidence boundary

Current task status relayed by the parent is that controlled RGBA8 and BGRA8 D3D11 windows both remain black. That observation does not identify whether the app source, DXVK blitter, WSI target, or surface-composition stage first diverges. W1 divides those hypotheses: a visible WSI clear places the defect before source-independent WSI presentation; a still-black WSI clear leaves the target, submission/present, or surface-composition boundary unresolved. The constant-fragment probe then separates ordinary source sampling from pipeline/render-target output. These are diagnostic outcomes, not proof of pixel values: this audit performs no capture/readback and does not claim a runtime result. No renderer mutation is justified by source inspection alone.
