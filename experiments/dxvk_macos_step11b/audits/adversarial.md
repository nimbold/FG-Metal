# Step 11B adversarial evidence review

Scope: read-only review of `REPORT.md`, run artifacts and diagnostic sources, plus the pinned DXVK source. No runtime, DXVK or test code was modified.

## Adjudication

**Gate 1 fails. Gate 2 was correctly stopped and is not tested.** The user-observed black Wine/DXVK window is dispositive for the required visible-rendering baseline. The separate native Vulkan clear-control window being visibly red/green shows that the pinned MoltenVK 1.4.2 can produce native Vulkan WSI output; it does not validate the Wine/DXVK source-image-to-present-image path. Protocol success cannot be used to upgrade the black lane to a healthy baseline.

## Evidence that survives adversarial review

- The direct DrawIndex probe loads explicit dylib paths and prints `dladdr` identity. On the old Wine provider (MoltenVK 1.4.1), the explicit DrawIndex shader reaches the exact `DrawIndex is not supported in MSL` conversion error and `VK_ERROR_INITIALIZATION_FAILED`; on the pinned 1.4.2 binary it creates the graphics pipeline successfully. The old-provider `dxvk_present_vert` control succeeds. This is strong, scoped causal evidence that 1.4.2 repairs the DrawIndex translation gap. Pipeline creation compiles the shader, but the probe does not issue a draw or establish rendered pixels.
- The 60-second Wine/DXVK run records 1,800 application `Present` calls, all `S_OK`, and exit 0. The 30-second run records 901 calls. The lifecycle run completes resize/minimize/restore/fullscreen/recreation API operations and exits 0. These establish repeated API/renderer operation and lifecycle-call success, not visible output.
- The Wine candidate's `DYLD_PRINT_LIBRARIES` path and MoltenVK startup version establish that the tested candidate process loaded its candidate-local 1.4.2 `libvulkan.dylib`. The separate native clear-control identifies the same pinned build and its user-confirmed red/green window. These are distinct processes and distinct presentation paths.
- No DXVK presentation patch or internal WSI request is present in the reviewed evidence. There is no AppFrameId/WsiPresentId split or evidence that internal presents preserve DXGI semantics.

## Claims that need tighter wording or evidence

1. **Window flags and color IDs are not pixel evidence.** `d3d11-semantics.cpp` records that the window is visible/foreground and that the CPU selected one of four clear colors, then calls `ClearRenderTargetView` and `Present`. Those observations do not establish that the clear reached the source texture, that the Presenter blit sampled it, or that Wine's layer displayed it. The user's black-window observation correctly overrides these structural indicators. The unresolved failure boundary remains between source backbuffer contents, DXVK's blit/WSI image, and Wine/macOS composition.

2. **The lifecycle row is not literally “all S_OK.”** `run-summary.json` records 1,921 `S_OK` calls and 30 `0x087a0001` (`DXGI_STATUS_OCCLUDED`) results in the 65-second lifecycle run; the latter occur while minimized and are success-severity statuses. Report it as 1,921 `S_OK` plus 30 expected/observed occluded statuses, exit 0. `focus_loss` in the lifecycle CSV is implemented by `ShowWindow(SW_MINIMIZE)`, so this tests minimize/restore, not independent focus-loss/recovery behavior. API transitions pass; visual correctness during them does not.

3. **Waitable-object polling has narrow coverage.** The waitable test correctly requests `FLIP_DISCARD` plus `DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT`, receives a valid handle, and observes `SetMaximumFrameLatency(1)` / `GetMaximumFrameLatency()==1`. Its `WaitForSingleObject(handle, 0)` is called *after* each Present, so it consumes/polls the signal without blocking before the next frame. It does not test wait-before-render pacing, a deliberately delayed consumer, latency saturation, or application callback behavior. There are no renderer latency-callback observations in the test. Call the result “zero-timeout waitable-handle signal observations,” not a full frame-latency behavior pass.

4. **The measured backbuffer index cannot establish rotation or a future no-mutation invariant.** The CSV reports index 0 on every row, but the pinned source's `D3D11SwapChain::GetImageIndex()` is hard-coded to return 0 (`src/d3d11/d3d11_swapchain.cpp:155`). `GetBuffer(0)` returns the retained COM wrapper in `m_backBuffers[0]`; a stable pointer is expected for that buffer object and says nothing about which WSI image was presented or whether its contents changed. Preserve the recorded values, but do not treat them as independently validated backbuffer progression. This point is separate from the already decisive visible-rendering failure.

5. **Do not call the native 1,800-present run a physical 60 Hz proof.** `native-clear.log` reports 1,800 successful presents, 60 retained Google display-timing records, and `elapsed_ns=21472660583` (~21.47 seconds). That duration and the timing-history snapshot do not prove 1,800 physical display updates, nor application-to-internal cadence. The report already appropriately treats this as native colored-output isolation only.

6. **The persisted runner does not encode the reported no-HUD/HUD A/B.** The current `evidence/run-baseline.py` unconditionally sets `DXVK_HUD='fps'` for every invocation, while the report describes `baseline-30s` as no-HUD and `baseline-30s-hud` as HUD-enabled. The results JSON records no HUD/environment snapshot, and both runs carry the same DLL hashes and similar counters. This may reflect a runner edited between runs, but the checked-in evidence does not reproduce the no-HUD distinction. Keep the independent direct DrawIndex A/B as the proof of the MoltenVK fix; qualify the per-run HUD attribution unless an exact environment record or preserved pre-edit runner is attached.

## Required evidence boundary

`VERIFIED`: upstream/runtime DrawIndex translation repair; candidate provider identity; API-level repeat Present/lifecycle behavior; user-observed black Wine/DXVK output; user-observed colored native Vulkan control; no Gate 2 implementation in the reviewed changes.

`SUPPORTED INFERENCE`: the black output is downstream of the native MoltenVK clear path, in the Wine/DXVK output route. The exact faulty stage is not identified.

`NOT TESTED`: visibly healthy Wine/D3D11 content; pixel correctness at source-backbuffer, post-blit WSI and composed-window boundaries; frame-latency throttling under a waiting consumer; valid flip-model index rotation in this fork; any internal presentation or its DXGI invariants; physical 30-to-60 display cadence.

The correct current classification is **FAIL — RIFE STILL BLOCKED**. A future Gate 1 retry should first identify the first black boundary using source and post-blit GPU-side evidence, then rerun the visible 60-second and lifecycle baseline. Gate 2 remains out of scope until that succeeds.
