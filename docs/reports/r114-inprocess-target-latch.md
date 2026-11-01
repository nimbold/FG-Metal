# FG-METAL — R114 In-Process Target Latch Title Display Report

**Verdict:** `FIRST_REAL_TITLE_D3DMETAL_A_TO_M_TO_B = VALIDATED (CONSTANT_DEPTH_PROTOTYPE)`

A real in-world Elden Ring frame pair `2749→2750`, captured during physical camera rotation, was reserved **inside the game process**, held at the accepted producer-commit seam, gated by the unchanged R110 fast gate (`ACCEPTED`, 91.84 % active motion), interpolated by MetalFX into a GPU-resident frame `M` (`InternalPresentId=14`), presented through the game's own `CAMetalLayer` (`reachedDisplay=1`, `WsiPresentId=2750`), followed by the retained SOURCE `B` (`WsiPresentId=2751`), with the frontier reopened and the game stable for 9,864 further frames. Zero SOURCE dispatches crossed the frontier while it was closed.

---

## 1. WorkBuddy/model execution summary

- Harness: WorkBuddy AI. Orchestrator: DeepSeek V4.1 Flash (Max) — repository reading, decomposition, implementation, build, live-run decisions, evidence review, this report.
- One independent reviewer subagent (high-reasoning tier per harness routing) audited the latch ordering against eight specific questions before the live run; it found one real blocker (capture-window renewal required `ARMED`, while arming required a live window — a circular dependency), which was fixed and re-verified by the same reviewer. No other models were used and no fallback occurred.
- No agent army: one implementation owner (orchestrator), one independent reviewer, and the orchestrator as runtime/evidence owner.
- Live attempts were bounded and every failure was a safe fallback; the decisive defect found by the live run (B forwarding deadlocked inside `CAMetalLayer`'s presented callback) was a genuine product bug that no static review had exposed.

## 2. In-world title process

- Fresh Highball process `63130`, engine `x64-sikarugir10.0_6-r29`, executable `/Users/nima/Games/Elden Ring/Game/eldenring.exe`, window `ELDEN RING™`.
- Observer loaded at launch with `presentation=1`, `framebufferOnly=1`, `titleCapture=1`, `r114InprocessLatch=1`.
- Title layer `0x60000327c240`, device `0x7fb27c0e4800`, Apple M3 registry ID `4294968554`, layer size `1472×956`, drawable `1470×956`, `framebufferOnly` cleared and validated.
- The user confirmed the open world; the run was performed in fullscreen on the game's own desktop.

## 3. Provider warm-up

- Capture window 1 covered `2735..2782` (48 frames).
- The provider reached `READY` on `2736→2737` with `InternalPresentId` allocated, leaving 37 frames of runway at the arm point.
- No fixed sleep was used as a correctness condition.

## 4. In-process next-pair arm

- The controller wrote one arm file (`r114-arm-next-moving-pair`) before any motion. It carried no source sequence.
- The observer armed itself at `sequence=2745` once every precondition held in-process: exact process, selected layer/device, world-scene confirmation, `providerWarmCount=1`, active window with runway ≥ 8, no ticket, frontier `OPEN` and unspent.
- `R114_NEXT_MOVING_PAIR_ARMED` and `R114_MANUAL_MOTION_READY` were emitted by the observer, not the controller.

## 5. Physical-motion window

- The user rotated the camera physically with the touchpad. No synthetic gameplay input was sent at any point.
- The host lacks Input Monitoring permission, so the independent mouse listener could not run; physical motion is evidenced by the accepted R110 estimator field (91.8 % of vectors above 0.5 px on the reserved pair, maximum 8.148 px) plus the user's attested action.
- The controller waits for the exact game process to become frontmost before arming, so desktop switching cannot consume a capture window.

## 6. In-process pair reservation

```
R114_MOTION_OBSERVED  pair=2738->2739 activeFraction=0.890696
R114_NEXT_PAIR_RESERVED pair=2749->2750 motionEvidencePair=2738->2739
                        lastCommittedSourceSequence=2748 leadFrames=1
                        captureWindow=2735..2782 state=RESERVED
                        externalPairSelection=NONE
```

- The reservation bound `pairA = lastCommitted + 1`, i.e. the next pair that had not yet reached the commit seam, 50 µs after the motion observation.
- No file, socket or external message named the pair. The legacy file-backed target path is short-circuited when the latch is enabled.
- Because reservation precedes A's producer completion, A's WSI dispatch is still attributable to the tuple, and A's source diagnostics are still captured.

## 7. Producer-commit frontier close

```
R103_SOURCE_FRONTIER=CLOSING_FOR_PAIR pair=2749->2750 producerStatus=2
R114_FRONTIER_CLOSE_AT_COMMIT reserveLeadNs=25590000 reservedBeforeBPresent=1
R103_SOURCE_FRONTIER=CLOSED operationsInFlight=0
```

- The accepted R103 seam is unchanged: B's `commit` return closes the future SOURCE frontier before any motion analysis or provider work.
- The close happened 25.6 ms after reservation and strictly before B's present.

## 8. SOURCE B deferral

```
R102_SOURCE_TICKET=DEFERRED ticketId=1 appFrameId=2750 sourceSequence=2750
  drawableID=2749 selector=presentAfterMinimumDuration: argument=0.016666667
  producerStatus=3 wsiPresentId=UNALLOCATED state=DEFERRED frontierClosed=1
```

- B was deferred at its present hook with drawable, texture, layer, device, producer command buffer, original selector and original argument preserved.
- No `WsiPresentId` was allocated at deferral.

## 9. Fast-gate result

```
R110_FAST_PRESENT_ELIGIBILITY=ACCEPTED pair=2749->2750 internalPresentId=14
  sourceWindow=1 worldSceneConfirmed=1 inputAttributed=1 estimatorCompleted=1
  motionSamples=1405320 finiteMotion=1405320 activeOver0_5px=1290637
  activeFraction=0.918394 maxMagnitude=8.148446
  outputFinite=1 outputSamples=1405320 finiteOutput=1405320 colorSamples=87713
  meanAbsDiffA=0.007883 meanAbsDiffB=0.007316
  thresholds=activeFraction>=0.15,endpointDifference>=0.002,maxMagnitude<=256
  fastMetricsGpuMs=0.382 reason=none
```

- Gate unchanged: exact pair lineage, finite motion, ≥ 15 % active coverage, ≤ 256 px magnitude, finite MetalFX output, ≥ 0.002 endpoint distance to each endpoint, verified in-world state.
- No threshold, estimator parameter or provider setting was modified.

## 10. Estimator/MetalFX timing

| Stage | Value |
|---|---:|
| Estimator GPU time (2749→2750) | 15.421 ms |
| MetalFX transition | 4.437 ms |
| Fast-gate metrics GPU | 0.382 ms |
| Result | `INTERNAL_FRAME`, `InternalPresentId=14` |

Constant-depth prototype (`CONSTANT_DEPTH_PROTOTYPE`), no source pixel CPU round-trip and no file transport on the product path.

## 11. INTERNAL acquisition

```
R102_INTERNAL_DRAWABLE_ACQUIRED origin=INTERNAL internalPresentId=14
  drawable=0x600000650700 drawableID=2750 layer=0x60000327c240
  device=0x7fb27c0e4800 size=1470x956 pixelFormat=80 framebufferOnly=0 usage=23
```

- Acquired from the exact Elden Ring `CAMetalLayer` through the original `nextDrawable`, tagged `origin=INTERNAL` before acquisition, bypassing the closed SOURCE frontier.
- Same layer, same device, correct dimensions and format, `framebufferOnly=0`.

## 12. INTERNAL WSI dispatch

```
INTERNAL_PRESENT_RENDER=COMPLETED drawableID=2750 sourceTexture=0x7f8ceacc3390
R102_WSI_DISPATCH origin=INTERNAL internalPresentId=14 wsiPresentId=2750
  appFrameId=NONE sourcePair=2749->2750
```

- M was copied into the INTERNAL drawable through the already validated title presentation pipeline.
- `WsiPresentId` was allocated immediately before dispatch; M received no `AppFrameId`.

## 13. M displayed callback

```
R102_INTERNAL_PRESENTED_CALLBACK internalPresentId=14 wsiPresentId=2750
  pair=2749->2750 presentedTime=93581.195172125 reachedDisplay=1
```

- Public presented callback reported `reachedDisplay=1`. Title display is claimed on this evidence, not on WSI submission.

## 14. Retained B forwarding

```
R104_TIMESTAMP event=source_b_forward_begin  appFrameId=2750 wsiPresentId=2751
R104_TIMESTAMP event=source_b_forward_complete appFrameId=2750 wsiPresentId=2751
R102_SOURCE_TICKET_RELEASED outcome=A_TO_M_TO_B_FORWARDED
```

- B was forwarded with its original selector `presentAfterMinimumDuration:` and original argument `0.016666667`, keeping `AppFrameId=2750` and SOURCE identity.
- `WsiPresentId=2751` was allocated immediately before that dispatch.

## 15. Actual A/M/B/C WSI order

| Order | Origin | Identity |
|---:|---|---|
| 2749 | SOURCE A | `wsiPresentId=2749` |
| 2750 | INTERNAL M | `internalPresentId=14`, `wsiPresentId=2750` |
| 2751 | SOURCE B | `wsiPresentId=2751`, `appFrameId=2750` |
| 2752+ | SOURCE C… | after frontier reopen |

```
R110_TITLE_WSI_SEQUENCE=VALIDATED sourceAOrder=2749 internalOrder=2750
  sourceBOrder=2751 noSourceDispatchBetween=1
GPU_RESIDENT_CONTIGUOUS_A_TO_M_TO_B=VALIDATED
```

## 16. Frontier/leak classification

- `SOURCE_LEAK_WHILE_FRONTIER_CLOSED = 0`.
- `MISSED_TARGET_ADMISSION = 0`.
- Pre-frontier SOURCE traffic (normal frames before reservation) is counted separately and is not a frontier failure.
- `R103_SOURCE_FRONTIER=OPEN … contiguousCandidate=1 futureSourceLeaks=0`; 9,864 SOURCE frames were acquired after the reopen, so C and subsequent rendering proceeded normally.

## 17. Post-success visual assessment

Artifacts: `evidence/r114-inprocess-latch/post-success/` (A, M, B, three-panel composite, zoomed player and background crops, amplified difference map, `r114-post-success-visual.json`).

| Metric | Value |
|---|---:|
| Active motion coverage (>0.5 px) | 91.81 % |
| Mean / max motion magnitude | 2.633 px / 8.148 px |
| Endpoint mean abs diff (A↔B) | 0.01280 |
| M vs A / M vs B | 0.00789 / 0.00734 |
| Zero-motion 50/50 blend vs M | 0.00417 |
| Player ROI: M vs A / M vs B / zero vs M | 0.01067 / 0.01032 / 0.00527 |
| World ROI: M vs A / M vs B / zero vs M | 0.00793 / 0.00720 / 0.00457 |
| Pixels within 8/255 of the zero-motion blend | 96.36 % |

- M sits between the endpoints and departs from a naive cross-fade by 0.0042 mean absolute error, concentrated on high-frequency edges (rock silhouettes, distant structure, sword, foliage) — the signature of motion-compensated interpolation rather than a blend, with flat regions essentially identical.
- Visual classification: **`NONE_OBVIOUS`**. The composite and 2×/3× zooms show a clean in-between frame with no doubling, tearing, smearing or missing geometry that can be pointed to. Caveat: this pair's motion was modest (max 8.15 px), so it exercises interpolation accuracy far less than a fast pan would; a high-magnitude pair remains the stronger visual test.
- Presentation correctness and visual quality are separate verdicts: presentation is validated; visual quality is `NONE_OBVIOUS` at this motion level only.

## 18. Title-presentation verdict

`FIRST_REAL_TITLE_D3DMETAL_A_TO_M_TO_B = VALIDATED (CONSTANT_DEPTH_PROTOTYPE)`

All eighteen success conditions hold: in-world scene, physical camera movement, exact consecutive A/B, in-process reservation before B's presentation, frontier close at the producer-commit seam, B deferred before ordinary WSI, unchanged gate pass, GPU-resident M, `InternalPresentId`, `WsiPresentId`, INTERNAL drawable from the game's own layer, M WSI dispatch, `reachedDisplay=1`, retained B dispatched afterwards with preserved identity, zero SOURCE leaks while closed, SOURCE C only after reopen, and a stable game.

Failures that preceded it, recorded for completeness:

| Attempt | Outcome |
|---|---|
| 4 | reserved `5368→5369`; gate rejected, 1.77 % active (camera stopped before the reserved pair) |
| 6 | reserved `2793→2794`; gate rejected, 12.59 % (swipe decayed) |
| 7 | reserved `5562→5563`; gate rejected, 6.46 % (drag ended early) |
| 8 | gate `ACCEPTED`; `INTERNAL_PRESENT_PREFLIGHT=FAILED pipeline=0x0` — `FGMETAL_R100_PRESENT` missing from the launch environment |
| 9 | gate `ACCEPTED`; M displayed (`reachedDisplay=1`); B's forward deadlocked inside `CAMetalLayer`'s presented callback and froze the game |

Attempt 9's deadlock was a real product defect in the success path, invisible until M actually reached the display: forwarding B synchronously from inside `didPresentAtTime:` re-enters the layer's present lock. B's forward now hops to a background queue; ordering is unchanged because M has already reached the display when the callback fires.

## 19. Exact next PRODUCT blocker

The one-shot tuple lifecycle is complete, but it is still **one tuple per process**: after a consumed tuple the frontier is spent and the latch is terminal by design. The next product blocker is therefore the **re-armable tuple lifecycle under continuous bounded 2× interpolation** — tuple reuse without process restart, pacing against the display deadline, latency of the deferred-B hold (B is currently held for the whole gate + MetalFX window), removal of the remaining blocking waits in the acquisition/dispatch path, and stability over thousands of consecutive tuples. None of that was attempted here, and none of it should be inferred from this single validated tuple.

### Evidence

- Observer log: `/tmp/fgmetal-r108-unattended/observer.63130.log` (runtime; key lines reproduced above)
- Controller record: `experiments/metalfx_frame_interpolator_R93/evidence/r114-inprocess-latch/attempt-10/r114-controller.json` (reconstructed from the observer log — the controller process was terminated when its parent shell exited after the run completed)
- Pair artifacts: `experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/run-06-r114-inprocess-latch-title-display/` — `source-sequence-2749.raw`, `source-sequence-2750.raw`, `motion-pair-2749-2750.rg16f`, `title-M-for-2749-2750.rgba16f` (failed-attempt diagnostics pruned from 5.2 GB to 91 MB)
- Post-success visuals: `experiments/metalfx_frame_interpolator_R93/evidence/r114-inprocess-latch/post-success/`
- Implementation: `experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/observer-title-inprocess-r114-present.m`, build `build_r114_tools.sh`, dylib `libfgmetal_r114_present_probe.dylib` (sha256 `10c879a2ce616cf9…`)
- Runner: `experiments/metalfx_frame_interpolator_R93/evidence/r114-inprocess-latch/run_r114_inprocess_latch_once.py`; bottle rollback `restore_bottles.sh`
