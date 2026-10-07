# Step 11B.1 — D3D11 black-output localization and downstream correction

Date: 2026-10-04. **PASS — healthy visible D3D11 baseline established with a localized, narrow downstream correction.** Gate 2 is not attempted and RIFE remains blocked. No internal presents or frame generation are implemented.

## Proven technical cause

The failing stage is **APP SOURCE preparation**, before the ordinary DXVK present blitter. `ClearRenderTargetView` records a deferred clear against the application backbuffer's RTV. The pinned `beginExternalRendering()` only invokes `endCurrentCommands()` and `beginCurrentCommands()`. `endCurrentCommands()` uses `endCurrentPass(true)`, which suspends the render pass. In the clear-only case, it does not execute `prepareShaderReadableImages(false)` and therefore does not materialize the deferred clear. The external blitter bypasses the context's ordinary shader-read resource preparation and samples the originally initialized black image.

This is not a wrong-backbuffer allocation in the tested DISCARD path: the trace correlates the pending clear and sampled source by Vulkan image, image object, and backing storage. They match. DISCARD creates one app backbuffer here and does not invoke `RotateBackBuffers`, even though the DXGI descriptor requests two buffers.

The runtime localization sequence was:

| Experiment | Direct user observation | Conclusion |
|---|---|---|
| Unchanged pinned repository BGRA8 smoke | BLACK | Reproduces with repository smoke; not unique to our harness |
| Controlled RGBA8 | BLACK | Baseline negative |
| Same executable, BGRA8 switch only | BLACK | Format alone rejected |
| Direct acquired-WSI GPU magenta clear; blitter bypassed | VISIBLE MAGENTA | Existing Wine/DXVK WSI presentation can show GPU-written color |
| Ordinary blitter pipeline, constant magenta fragment output | VISIBLE MAGENTA | Blitter graphics target/pipeline/present path works without sampling |
| Normal sampling shader, renderer-owned GPU-cleared RGBA8 source | VISIBLE GREEN | Normal source sampling works for an internal image |
| App source with metadata tracing, source flush disabled | BLACK | Reproduced normal-path failure with substitutions off |
| Identical binary, only source flush enabled | VISIBLE CHANGING COLOR | Materializing deferred clears restores app content |

The last two probes use identical executable and renderer DLL hashes. Ignoring per-run prefix/log paths, their recorded environments differ only in `FG_STEP11B1_FLUSH_SOURCE=1`. For the first 16 frames, the source probe logs `pending_clears=1` after external preparation in the failing lane, and `pending_clears=0` in the corrected lane. Both trace the same image/object/storage for the clear and sampled source. API clear values in the logs are application command metadata, not image-content readback. [Automated correlation check](evidence/source-localization-check.json) and [validator](evidence/verify-source-localization.py).

## Narrow final correction

The final downstream patch adds `endCurrentPass(false)` before `endCurrentCommands()` in `DxvkContext::beginExternalRendering`. Ending the pass without suspension flushes deferred clears and restores shader-readable layouts before external consumers bypass context preparation. Existing queue ordering, synchronization, acquire/present calls, and DXGI accounting are retained. No source format is forced or reinterpreted.

- Only renderer file changed: `src/dxvk/dxvk_context.cpp`, three inserted lines including two comment lines.
- [Final patch](evidence/final-fix.patch).
- [Final build identity](evidence/final-build-identity.json).
- [Fixed D3D11 DLL](evidence/fixed-d3d11.dll); unchanged [pinned DXGI DLL](evidence/pinned-dxgi.dll).
- Disposable downstream worktree: `/tmp/fgmetal-step11b1/dxvk-diagnostic`.
- Final DLL contains no `FG_STEP11B1` diagnostic mode strings; all diagnostic source changes and the constant shader were removed before its build. The preserved [complete diagnostics patch](evidence/complete-diagnostics.patch) is test-only and requires explicit environment opt-in, default OFF.

This external-rendering boundary is also used by D3D9 presentation/video paths. Its source-preparation contract applies there too, but D3D9/game workloads are not runtime-validated in this step.

## Preserved identities and evidence

| Component | Identity |
|---|---|
| DXVK-MacOS source | `metalsharp/DXVK-MacOS`, `8d348236e14a3db25ffbe528a83010b3dd69a3ef` |
| MoltenVK | `v1.4.2`, `db66022459ffb663aa2b50f6b018bc2e124f5edf` |
| MoltenVK candidate binary | SHA-256 `df43b3a65b67efb06f673fc8f0e5cedf7f54276f5911f479c17050c04b1fd12e` |
| Wine runtime | Wine 11.17 (MetalSharp); frozen APFS copy under `/tmp/fgmetal-step11b1/runtime-frozen/wine` |
| Original D3D11 DLL | SHA-256 `6940b883fb4bc1b744fb7ca3aa6a486303eaf180c9fe85c03ed7cb175057bf28` |
| Original DXGI DLL | SHA-256 `17cbe8e84906ce9657493b1301e712cfc8123413034666fd9387dfe1bbf5b33c` |
| Original harness executable | SHA-256 `18509edad2e1ae8d49941bd1868dd384d03cf693cc66b5542e7bba1611e21163` |
| Controlled A/B executable | SHA-256 `4419ad8d46e2d96df93a4bf8d7d8e0cb4fee853282414fdbc28fe5f4d210e973` |
| Final corrected D3D11 DLL | SHA-256 `b63c935e55a0854dc6857414fceaf7fde850da9f201e027b41a4cf97fed59352` |

[Full frozen file manifest](evidence/frozen-identities.json) records 6,772 files including Wine/winevulkan, MoltenVK, original DLLs/harness and native control. Original payloads, harness and native executable are also copied under `/tmp/fgmetal-step11b1/preserved`. [Step 11B report](evidence/step11b-preserved-report.md) and [black 60-second evidence](evidence/black-baseline/) are preserved. The old 1.4.1 negative control is not re-run or changed. MoltenVK source and Wine source are not patched.

The first patched BGRA prefix preparation hit ENOSPC before application launch; this rejected attempt is preserved. Completed task-owned disposable prefixes were removed after registry snapshots were saved, and BGRA was retried from a fresh prefix. At completion, all Step 11B.1 disposable prefixes were cleaned after registry snapshots; their application logs and exact payloads remain in evidence. Original Step 11A/B prefixes and runtime payloads were not removed.

Every run uses a dedicated disposable prefix and exact runtime/DLL copies. Loader logs record actual native DLL/provider paths. The original historical black baseline and candidate runtime remain intact. No normal game prefix, Highball, DXMT, or pre-existing FG-Metal change is modified.

## Repository smoke and format A/B

The repository smoke source is copied byte-for-byte from the exact source pin and built without D3D11 behavior changes. Linking adds `-luuid` to resolve the SDK IID; it does not change rendering behavior. The 3,600-frame black run takes approximately 44 seconds, exits 0, creates the device and FIFO WSI, and reports its throughput. **Its source ignores each Present HRESULT, so individual successful HRESULTs are not claimed.** The logs and exact source/executable hashes are retained.

The controlled A/B uses one executable. `FG_AB_BGRA` changes only the swapchain format; both lanes use 640×360, BufferCount=2, DISCARD, flags=0, default RTV, the same window/pump, Present(1,0), 60-second duration, ~30 Hz pacing, and large red/green/blue/white clears. Both use the same pinned DLLs/runtime and identical fresh-prefix preparation. The fresh-prefix RGBA8 replay records 1,801 S_OK Presents; BGRA8 records 1,801 S_OK Presents; both exit 0 and are user-observed BLACK. Both select BGRA8 UNORM/SRGB_NONLINEAR FIFO WSI with three images.

The initial successful RGBA lane reused its isolated prefix after the rejected loader attempt. The completed fresh-prefix `rgba8-clean-replay` is the strict A/B comparison lane; the initial lane is retained separately. [Exact A/B checker result](evidence/exact-ab-check.json) verifies binary/DLL equality and the normalized environment difference.

The first attempted A/B executable could not load `libwinpthread-1.dll` and never created a device. This rejected attempt is retained in `rgba8-loader-failure`; the rebuilt shared A/B binary links its runtime statically. It is not a visual-negative test.

This defect is reproducible in the repository smoke and clear-only controlled harness on this M3/Wine candidate. It is not specific to RGBA8. Source evidence indicates an external-rendering preparation defect independent of channel order; cross-hardware/OS reproduction is not established. Historical repository performance results are not accepted as prior visual-validation evidence.

## Independent read-only investigations

The requested audit roles were run using GPT-6 Luna Max subagent requests; source instrumentation followed the initial investigations.

1. [Harness differential](audits/harness.md): format ranked first before A/B; documents size, style, RTV binding/lifetime, pump, colors, pacing, refresh-rate and lifecycle differences. Both use DISCARD/two buffers/flags=0/interval 1. OM binding is not required for explicit RTV clear, and the OM-bound repository smoke also failed.
2. [Format chain](audits/format.md): regular RGBA/BGRA mappings, identity swizzles, Metal RGBA/BGRA formats, typed/sRGB view families and blitter specialization. No causal format fix justified.
3. [Present blitter](audits/blitter.md): exact acquired-target/source distinction and raw clear substitution with the same synchronization/present tail. Constant shader pipeline cache identity is separated in diagnostics.
4. [App source/storage](audits/app-storage.md): deferred clears, flush ordering, DISCARD one-allocation/no-rotation, and conditional sequential rotation concerns. Runtime trace confirms the clear-source hypothesis for this DISCARD workload; sequential storage concerns are not exercised or claimed as this cause.
5. [Surface comparison](audits/surface.md): native Metal-surface route versus Wine Win32-surface route and usage differences. No causal public-property mismatch established. Direct WSI visibility makes further surface experiments unnecessary.
6. [Diagnostic methodology](audits/methodology.md): one-stage substitutions and explicit APP SOURCE / BLITTER / WSI TARGET / SURFACE/PRESENT branches. Empty renderer-error logs are not full Vulkan validation-layer coverage.
7. [Adversarial final review](audits/adversarial-final.md): independently checked the exact patch/helpers, hashes, source-trace identity correlation and single-variable environment difference. Supports the clear-only cause/fix; explicitly separates parent-relayed human observations from source/API evidence. The final review is updated against the completed runtime evidence; its evidence boundaries remain explicit.

## Final normal-path validation

Every row below uses the final fixed DLL (`b63c935e…`) and unchanged pinned DXGI DLL, with diagnostic code removed. Direct user observations are recorded in [visual observations](evidence/visual-observations.json); structural results are in [all-run summary](evidence/all-run-summary.json). No visual status is inferred from Present HRESULT, GPU completion, throughput, trace or lack of errors.

| Normal-path run | Application/API result | Direct user visual result | Exit |
|---|---|---|---|
| Exact original RGBA8 executable, 30 s | 901 S_OK Presents | Changing colors through shutdown | 0 |
| Exact original RGBA8 executable, 60 s | 1,801 S_OK Presents | Changing colors through shutdown | 0 |
| Controlled BGRA8 source, 60 s | 1,800 S_OK Presents | Red/green/blue/white changes through shutdown | 0 |
| Unchanged repository BGRA8 smoke | 3,600 frames, ~56.86 s app-loop duration; per-Present HRESULT not recorded by source | Changing colors through shutdown | 0 |
| Exact original RGBA8 lifecycle, 65 s | 1,951 Presents: 1,920 S_OK, 31 DXGI_STATUS_OCCLUDED during minimize | All transitions visibly healthy | 0 |
| Step 11B waitable-object diagnostic, 30 s | 900 S_OK Presents, normal sampled presentation | Changing colors through shutdown | 0 |

Lifecycle records resize to 800×450 at ~3.0 s, minimize at ~25.0 s, restore at ~26.0 s, fullscreen enter at ~35.0 s, fullscreen exit at ~36.5 s, swapchain recreation at ~45.0 s and orderly shutdown at ~65.0 s. API results are successful, and the user explicitly confirmed visible health across all those transitions. Independent focus loss without minimization is not tested.

## Waitable / DXGI baseline

The unmodified Step 11B semantics executable requests FLIP_DISCARD with `DXGI_SWAP_CHAIN_FLAG_FRAME_LATENCY_WAITABLE_OBJECT`, on the final normal renderer. This is a healthy pre-internal-present baseline; no internal present is issued.

- `GetLastPresentCount`: all 900 observations equal the application sequence, all S_OK.
- `GetFrameStatistics`: first `DXGI_ERROR_FRAME_STATISTICS_DISJOINT`, then 899 S_OK observations; final PresentCount=899 versus final LastPresentCount=900, consistent with asynchronous completion. The runtime warns that frame statistics may be inaccurate; refresh counts/QPC are retained, not treated as physical display-cadence proof.
- Waitable semaphore: non-null handle `0xAC`; zero-timeout polls return 899 signals and one timeout.
- Frame latency: SetMaximumFrameLatency(1) and GetMaximumFrameLatency succeed and return 1. A deliberately stalled consumer is not tested, so actual throttling behavior is not independently established.
- Backbuffer: index 0 throughout; stable buffer-0 COM identity `0x8c92d0`. The pinned `GetImageIndex()` returns 0; stable wrapper identity is not evidence about WSI image identity or backing-allocation progression.
- Device removed state: all 900 queries S_OK. Shutdown exits 0.

[Raw semantics](evidence/final-waitable-30s/semantics.csv) retains all counts, statistics, refresh counts, QPC, wait results, buffer/index and device state.

## Regression and preservation checks

The regression workloads exercise the reported failure rather than inspecting implementation text: unbound clear-only RGBA8 → BGRA8 WSI, BGRA8 → BGRA8 WSI, the repository's OM-bound clear-only smoke, original 30/60-second behavior, lifecycle/recreation, and waitable semantics. Human visual observations are an explicit gate; API success cannot substitute for them.

- [Source-localization validator](evidence/verify-source-localization.py): PASS for binary equality, one-variable environment, and clear/sample image-object-storage correspondence with pending state 1→0.
- [Final regression evidence validator](evidence/validate-final.py): [PASS](evidence/final-validation.json) for final DLL equality, substitutions absent, API results/durations, all four requested color IDs, lifecycle success, waitable/device observations and the separately recorded direct visual observations. Its initial color-column parser was corrected to use the `color_id` header; no renderer change was made for that parser correction.
- Focused D3D11/DXGI build and final D3D11 rebuild succeed; final downstream `git diff --check` passes.
- [Preservation verification](evidence/preservation-verification.json): all 6,772 frozen files match, and every copied black-baseline file matches the original evidence byte-for-byte. Pinned DXVK and MoltenVK source trees remain clean.
- Regression boundaries: local Apple M3/Wine 11.17/MoltenVK 1.4.2 only; no cross-platform, game-rendered-scene, D3D9, HDR or multi-buffer sequential runtime claim.

All final runs retain the same MoltenVK 1.4.2 provider hash and report no current shader compilation, command-thread or device-loss error. These are local runtime observations, not exhaustive MoltenVK validation or global stability proof.

## Scope and limitations

No screen capture, CPU pixel readback, CPU image upload, Metal-direct substitution or GPU content-signature readback is used. No additional WSI Present is introduced. No AppFrameId/WsiPresentId split, internal scheduler, source history, frame generation or RIFE is implemented. Gate 2 remains NOT REACHED by design until this baseline gate is fully verified. The source correction and binaries are local downstream artifacts, not published or installed upstream.


## Result

**PASS for Step 11B.1.** The black-output stage and clear-only technical cause are established; the narrow correction is retained; normal DXVK presentation passes direct 30/60-second and lifecycle visual checks; shutdown is clean; waitable/DXGI semantics are recorded. The precise RGBA/BGRA mappings are preserved. Diagnostic substitutions are absent from the final renderer. No screen capture or CPU pixel readback is used.

READY FOR STEP 11C — INTERNAL WSI PRESENTATION PROOF
