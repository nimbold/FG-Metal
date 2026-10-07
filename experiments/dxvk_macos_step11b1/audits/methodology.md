# Step 11B1 diagnostic methodology

## Current evidence

The repository smoke is the exact `tools/smoke/d3d11_smoke.c` source (SHA-256 recorded in `harness.md`). The controlled RGBA8/BGRA8 comparison uses one executable and the same DXVK DLLs; `FG_AB_BGRA` is the only intended case switch. The recorded outcome is user-observed **BLACK** for the repository smoke and both formats, with no screenshot or pixel readback. DXVK logs report no renderer error lines, and the controlled runs record successful `Present` results. The existing native control is known visibly good and remains the comparator. These observations do not locate the first failing stage. An empty error-log scan is not equivalent to Vulkan validation-layer coverage; keep that evidence claim separate.

The pinned DXVK source is `8d348236e14a3db25ffbe528a83010b3dd69a3ef`; MoltenVK is v1.4.2. `blitter.md` correctly identifies the acquired `backBuffer`/WSI image separately from the D3D11 `GetBackBufferView()` source and gives the smallest useful target-only probe.

## W1: isolate acquired WSI target and presentation

Add a disabled-by-default diagnostic mode in the existing `D3D11SwapChain::PresentImage` callback. After `beginExternalRendering`, capture the acquired `Rc<DxvkImage>` by value, transition its available color subresources from `UNDEFINED` to `TRANSFER_DST_OPTIMAL`, issue `cmdClearColorImage` with opaque magenta, then transition to the imported image's declared present layout. Track the image for write access. `Presenter` declares `TRANSFER_DST` usage for swapchain images, so this bypasses the D3D11 source, image view, and `cBlitter->present` while using a supported operation. Preserve exactly the existing `synchronizeWsi(cSync)`, one `flushCommandList`, and one `presentImage` tail. Do not call the blitter afterward, because it overwrites the clear.

Keep the diagnostic mode active across frames so the user can observe a stable color. Each successfully acquired app `Present` then produces one clear and one existing WSI `presentImage`; this is still one WSI present per app present, not a second presentation. Preserve ordinary behavior when the mode is disabled. A visible magenta result shows the acquired-target write, submission/synchronization, and visible presentation route work together. It does not identify which of those sub-steps would fail for app content. A black result leaves target transition/write, submission, WSI presentation, and host surface composition unresolved.

## Follow-up only after a visible W1

Proceed one substitution at a time, observing directly and retaining the same acquire/synchronize/flush/present path:

1. Replace the blitter fragment output with a constant magenta value while retaining its attachment setup and draw. Suppress HUD/cursor composition for this diagnostic. Visible output covers graphics-pipeline/attachment output and the WSI tail without source sampling; black output points toward that render path, subject to the same visual boundary.
2. Restore the normal blitter shader but feed it a GPU-cleared internal source image. Visible output validates normal source sampling/blitting for controlled image contents; black output narrows the issue to source view, sampling, conversion, or blitter setup.
3. Feed the same blitter path the app's cleared backbuffer. If the internal source works but the app source does not, investigate app clear/storage and source lifetime/state. In particular, a successful API `ClearRenderTargetView` call alone does not prove the clear was materialized in storage before an external shader reads it.

Do not combine substitutions in one mode: each result should change only the stage under test. This order keeps a deferred app clear candidate separate from WSI visibility and shader/attachment output.

## If W1 is black

Keep the already-visible native control as the comparator; do not add a new Metal path or an independent renderer sample. First (S1), compare live public WSI and native-control values in the same session: surface format/color space, extent and scale, present mode, image usage, transform/composite-alpha, and image count. Record both sides so any mismatch is concrete. Next (S2), instrument one W1 frame to correlate the acquired swapchain image/index with the image receiving the clear and both layout transitions, then correlate that command list's acquire wait/present signal with submission and `vkQueuePresentKHR` for the same image. This proves command and synchronization routing only; it does not prove pixel contents reached the display.

Only if S1 or S2 exposes a concrete public-setting mismatch, run at most two follow-up A/B experiments, each changing one mismatched setting while holding the others and the known-visible control fixed. If no mismatch is found, stop at the unresolved WSI-target/submission/surface boundary and report that limit. Do not capture or read back pixels. Report direct visual outcome, app `Present` status, renderer/API diagnostics, and exact binary/source identities separately; do not call the result pixel-verified.
