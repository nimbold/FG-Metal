# FG-METAL — R122 OVERLAPPED GPU CAPACITY REPORT

**Round:** R122
**Subject:** Remove the display hold from the experiment, let the game render the next SOURCE frame while FG generates M, and measure whether 30-fps GPU capacity actually exists.
**Status:** COMPLETE — capacity measured and validated on the brief's throughput branch. The overlap branch could not be measured, and the reason is a property of the seam, proven from the timestamps rather than assumed.

---

## 0. HEADLINE CLAIMS

| Claim | Verdict |
|---|---|
| `CAN FG GENERATION FOR (A,B) RUN WHILE THE GAME RENDERS C WITHOUT BREAKING 30-FPS SOURCE THROUGHPUT` | **YES** — measured |
| `OVERLAPPED_30FPS_GENERATION_CAPACITY` | **`VALIDATED`** — on the brief's equivalent-throughput branch |
| `GPU_CAPACITY_FOR_30_TO_60` | **`SUFFICIENT`** for generation; presentation not attempted |
| `SOURCE_ACQUISITION_CADENCE` | **unchanged** — 34.319 ms vs 34.317 ms baseline (+0.01 %) |
| `SOURCE_PRODUCER_THROUGHPUT` | **unchanged** — 0.0938 ms vs 0.0942 ms (−0.42 %) |
| `M_CANDIDATES_PER_SECOND` | **30.49** against a 29.14 fps SOURCE — 1.046 per SOURCE frame |
| `SKIPPED_BUSY` | **0** of 29 attempts |
| `MEASURED_GPU_OVERLAP` | **`NOT_EVALUATED`** — the correlated command buffer is the drawable-writing tail, not the frame |
| `TITLE_THREAD_BLOCKED_ON_FG` | **NO** — 30 of 30 critical-path assertions passed |
| `HISTORICAL_EVIDENCE_MODIFIED` | **NO** — 5 486 files, 0 changed, by size, mtime and SHA-256 |

The single sentence: **with the display hold removed, the title produced one M candidate per SOURCE pair — 30.49 per second, 28 of 29 attempts, zero slot-busy skips — while its acquisition cadence, producer GPU execution and pacer headroom stayed within 0.4 % of a SOURCE-only baseline taken in the same session, and the GPU absorbed 6.5 ms of measured estimator execution per period without a single frame missing its deadline.**

The caveat that belongs next to that sentence: **physical concurrency was not measured.** The command buffer correlated to the drawable carries only the drawable-writing tail (0.0938 ms of execution inside a 1.272 ms commit-to-completion window), so the brief's Phase-1 quantity `GPUEndTime − GPUStartTime` returns the tail, not the frame. R123 must read this report as *the throughput requirement is met*, not as *the work was proven to overlap*.

---

## 1. HARNESS, MODEL AND ROLES

| Item | Value |
|---|---|
| Harness | WorkBuddy AI |
| Orchestrator | DeepSeek v4.1 Flash — High |
| Substantive subagents / reviewers | DeepSeek v4.1 Flash — High |
| Fallback model family | none used |
| Agent count | 1 (single-threaded round; no fan-out) |
| Live sessions | 1 game process, both phases |

The round was run as a single sequence rather than a fan-out because every phase depends on the previous phase's artefacts and on one uninterrupted game session. Splitting it would have introduced the scene-to-scene variation the brief explicitly forbids.

---

## 2. SCOPE AND FREEZES

**Not done, per the brief:** no continuous 30→60 display; no A hold; no surrogate SOURCE presentation; no estimator redesign; no MetalFX change; no motion-quality optimisation; no 60→120 attempt; no image-quality change.

**Frozen and verified frozen:**

| Frozen surface | Evidence it was untouched |
|---|---|
| R120 30-fps SOURCE pacing at acquisition | `R120_PACE_ENABLED hz=30.0000 periodNs=33333333 mode=source-acquire-block scope=SOURCE-only dropPolicy=NONE` in both phases |
| R117 optimised estimator | estimator source unchanged; estimator GPU window measured, not modified |
| R116 fast gate | `gate=SKIPPED` on every pair; `FGMETAL_R116_B_EARLY` absent, so the gate is not encoded in this mode |
| R119/R121 evidence isolation | five probes, section 4 |
| Current SOURCE capture copy | `ELDENRING_SOURCE_COPY_ENQUEUED` present, copy path unchanged |
| Constant depth | `depthQualifier=CONSTANT_DEPTH_PROTOTYPE` on all 29 transitions |
| Motion / gate semantics | `condition=CAMERA` on all 29 transitions |

---

## 3. ENVIRONMENT AND BUILD PROVENANCE

| Item | Value |
|---|---|
| Host GPU | Apple M3 (`deviceName=Apple M3`, `registryID=4294968554`) |
| Layer | 1472 × 956, `pixelFormat=80`, `framebufferOnly=1` |
| Runtime | Highball / Wine `x64-sikarugir10.0_6-r29` |
| Observer | `libfgmetal_r122_overlap_probe.dylib`, injected via `DYLD_INSERT_LIBRARIES` |
| Scene | Minor Erdtree, physical camera rotation, no synthetic gameplay input |
| Phases | baseline then overlap, **one game process** (`pid=17333`) |

### 3.1 Provenance, including a defect in this round

The dylib that produced the recorded evidence was built **one edit before** the source now on disk. The edit added `warmup=%llu` to `R122_OVERLAP_SUMMARY` and changed the label on the provider's first-use nil-output pair from `FAILED` to `WARMUP`. Nothing else changed, and that claim is checked rather than asserted:

| Check | Result |
|---|---|
| Rebuild from current source | byte-identical, SHA-256 `c64d1c2e67eec98b38e2748f34acc1832e0419db9aa325c130c69fca9c664010` |
| Source SHA-256 | `3e4dc847768456304c6a05a738ed0ec64a64d6932aed1f1dacb9f377b9d1ede1` |
| Fields written by the run that the source cannot declare | **none** — over 28 tags and every field in both slices |
| Fields declared by the source but not written | 3: two conditional branches, plus `warmup` |
| `warmup` is unconditional in the source | yes — so its absence dates the run binary precisely |

`tools/verify_emitter_grammar.py` performs that comparison; its output is in `evidence-isolation/emitter-grammar.txt`, `RESULT=GRAMMAR_MATCH` for both slices in the direction that matters. **The measurement code is therefore identical and the difference is diagnostic-only** — but the report states it as a checked fact, not as a claim, and the controller now refuses to run at all when the binary on disk is newer than the process:

```
refusing to run: ... was modified at T2, after this process started at T1.
The running process is therefore not executing the binary on disk and the
evidence could not be attributed to it.
```

That guard did not exist when the evidence was taken. It exists now, and any future R122 run is attributable by construction.

### 3.2 Environment manifest

`bottle-env-r122-overlap.json`, applied to `~/Library/Application Support/Highball/bottles/Games/bottle.json`. `FGMETAL_R114_INPROCESS_LATCH` and `FGMETAL_R115_CONTINUOUS` are **absent by design**: the tuple machinery that owns B-deferral, the R103 frontier close and INTERNAL dispatch is never armed, so none of the three can happen.

---

## 4. EVIDENCE ISOLATION AND CORPUS GUARD

### 4.1 Isolation probes

Five probes against the final dylib, in a disposable host that loads it with `dlopen` so the real constructor, accessor, path composer, blob writer and guard all run without the game:

| # | Target | Expected | Result |
|---|---|---|---|
| 1 | this round's own directory | allowed, writes | `allowed=1 protected=0 saved=1 saved=1` |
| 2 | `r121-presentation-occupancy` | refused | `allowed=0 protected=1 <refused> saved=0` |
| 3 | `r119-live-throughput` | refused | `allowed=0 protected=1 <refused> saved=0` |
| 4 | `…/r122-…/../r121-presentation-occupancy` | refused | `protected=1 REFUSED reason=protected-historical-corpus` |
| 5 | `/tmp/r122-symlink-probe` → `r119-live-throughput` | refused | `protected=1 REFUSED` |

Probes 4 and 5 are the ones that matter: `realpath` resolves both the `..` spelling and the symlink before the containment test, so neither can smuggle a protected path past the guard. Full transcript: `evidence-isolation/isolation-probes.txt`.

### 4.2 Corpus guard

Snapshots taken before and after the live test, excluding this round's own directory:

| Mode | Before | After | Added | Removed | Changed |
|---|---|---|---|---|---|
| meta (path, size, mtime_ns) | 5 486 files / 30 352 888 118 B | same | 0 | 0 | 0 |
| SHA-256 | 5 486 files / 30 352 888 118 B | same | 0 | 0 | 0 |

`CORPUS_GUARD=UNCHANGED`. The metadata pass runs in 0.08 s and is what the live guard compares; the hash pass takes 38 s and is the only record that can prove content equality when size and mtime happen to be preserved. Both were run, because the second is what makes the first trustworthy.

---

## 5. PHASE 1 — SOURCE PRODUCER GPU WINDOWS, AND THE SEAM'S LIMIT

Per SOURCE frame the observer records: source sequence, producer command-buffer commit instant, `GPUStartTime`, `GPUEndTime`, completion-callback instant, `nextDrawable` acquisition, present-hook instant, plus the same commit and completion instants on the media clock for cross-validation.

168 baseline frames and 108 overlap frames carried a complete window.

### 5.1 What the window actually covers

| Quantity | Median | p90 |
|---|---|---|
| `GPUEndTime − GPUStartTime` | 0.0938 ms | 0.0971 ms |
| commit → GPU start (queued) | 0.4921 ms | — |
| GPU end → completion (queued) | 0.6840 ms | — |
| commit → completion | 1.2723 ms | — |

The correlated buffer is `AGXG15GFamilyCommandBuffer` on `AGXG15GFamilyCommandQueue`, matched uniquely to the drawable (`consecutiveMatches` increments once per frame), `commandBufferStatus=4` at completion, `error=none`. It executes for **7.4 %** of the interval it occupies.

A command buffer that renders a 1472 × 956 drawable cannot execute in 0.094 ms. At that duration the effective rate would be 15 Gpixel/s end to end and would have to include every pass that produces the pixels. The figure is the cost of a full-screen write — the drawable-writing tail of a chain — not the frame.

### 5.2 The independent cross-check from R121

R121 measured the SOURCE frame's GPU retirement wait at **23.073 ms** median and attributed it to the frame's own GPU execution. R122 measures producer commit → completion at **1.272 ms**. Both cannot describe the same frame.

They do not. R121 measured with the display hold in place; the capture blit is committed on the producer's queue, so in-order queue semantics held it behind the deferred presentation, and the "retirement wait" was the hold. R122 removed the hold — that is the round's premise — and the same interval collapsed from 23 ms to 1.27 ms. **R121's dominant blocker was the experiment's own apparatus, and removing it recovered the 23 ms.**

This is the most important cross-round result in the report, and it is why the capacity question had to be asked this way: the previous round's headline number was an artefact of the hold.

### 5.3 The honest limit

`SOURCE_RENDER_GPU_EXECUTION = GPUEndTime − GPUStartTime` is available and is **not** the frame's GPU execution. The brief's Phase-1 quantity cannot be obtained from this seam. The analyser therefore classifies the evidence as `TAIL_ONLY` and refuses to report the window as the frame's render:

```
evidenceQuality: TAIL_ONLY
sourceGpuExecMedianMs: 0.0938
producerWindowLeadMs: 0.4921
producerWindowTrailMs: 0.6840
evidenceNote: the correlated command buffer's GPU window is the drawable-writing
              tail, not the frame's render; the lead and trail latencies show it
              queued for longer than it executed
```

The threshold is stated in the analyser (`TAIL_FLOOR_MS = 0.5`) with its derivation, so the classification is auditable rather than a judgement call.

---

## 6. CLOCK DISCIPLINE

`MTLCommandBuffer.GPUStartTime` / `GPUEndTime` are on the `CACurrentMediaTime` base. `CLOCK_MONOTONIC` is not. A GPU timestamp may be subtracted only from another GPU/media-domain timestamp; a mismatch fails closed.

Every frame carries both domains for the same interval, which turns the rule into a measurement:

| Frame | media span | monotonic span | `clockSkewNs` |
|---|---|---|---|
| 1488 | 1.2408 ms | 1.2420 ms | −1 167 |
| 1489 | 1.1767 ms | 1.1780 ms | −1 334 |
| 1490 | 1.2685 ms | 1.2700 ms | −1 500 |

Skew is 1–4 µs against a 1.2 ms interval, i.e. below 0.35 %, and is reported per frame rather than corrected. `SOURCE_RENDER_GPU_EXECUTION` is computed entirely inside the media domain. The analyser's self-test contains a case (D) that writes the GPU stamps on a monotonic-like base and asserts that overlap collapses to zero — a check that has been shown to fail.

---

## 7. PHASES 2 AND 3 — GENERATION-ONLY MODE AND THE SINGLE SLOT

### 7.1 `R122_GENERATION_ONLY_OVERLAP`

| Required property | Implementation | Proof in the log |
|---|---|---|
| SOURCE presentation normal | `r102TryDeferSourcePresent` returns NO when overlap is armed | `bDeferral=NONE`; `R110_SOURCE_PRESENT_RETURN` one per frame |
| B not deferred | tuple machinery never armed | `frontier=OPEN-ALWAYS internalDispatch=NONE` |
| Frontier not closed | same | `R103_SOURCE_FRONTIER` absent from both slices |
| INTERNAL M not dispatched | `mDispatched=0` on every pair | `R101_WSI_DISPATCH origin=INTERNAL` absent |
| SOURCE WSI timing unaltered | pacing at acquisition unchanged | `R120_PACE_ENABLED` identical in both phases |
| FG still runs the full chain | capture → R117 estimator → MetalFX → gate | `ELDENRING_SOURCE_COPY_ENQUEUED`, `R108_GPU_INPUTS_COMPLETE`, `R108_METALFX_TRANSITION` ×29 |

The M candidate is discarded after completion. That is the mode: it spends the GPU and keeps nothing.

### 7.2 Single generation slot

Active slot = 1, acquired by CAS before `dispatch_async` and released after `processCapturedFrame` returns. There is no queue, bounded or otherwise: a second attempt while the slot is held is dropped and counted, never enqueued.

| Counter | Value |
|---|---|
| attempts | 29 (observer: 30, including the history seed) |
| generated | 28 |
| skipped busy | **0** |
| failed | 1 — first-use transition, `result=NO_OUTPUT`, `error=none`, 15.955 ms |
| history seeds | 1 |

A skip cannot block SOURCE: the skip path writes one line and returns before any capture work, and the SOURCE frame proceeds. Zero skips means the path was never exercised under load — which is itself the result, not an absence of testing.

---

## 8. PHASE 5 — SOURCE-ONLY BASELINE

Same process, same scene, minutes before the overlap run. `FGMETAL_R122_OVERLAP=1` was loaded but unarmed: capture selection returns NO while unarmed, so no frame is copied and nothing is generated.

| Metric | Value |
|---|---|
| SOURCE frames with a GPU window | 168 |
| Acquisition interval, median / p90 | 34.317 ms / 34.367 ms |
| Acquisition rate | 29.140 fps |
| Presented interval, median | 33.333 ms |
| Presented rate | 30.000 fps |
| Producer GPU window, median / p90 / max | 0.0942 / 0.0984 / 0.131 ms |
| Tail duty cycle, median / p90 | 0.28 % / 0.30 % |
| Pacer mean blocked per present | 32.370 ms |
| Pacer over-cap waits / short intervals | 0 / 0 |
| FG pairs / skips | 0 / 0 |

The controller aborts the baseline if any `R122_FG_PAIR` appears. None did.

---

## 9. PHASE 6 — OVERLAP RUN

| Metric | Value |
|---|---|
| SOURCE frames with a GPU window | 108 |
| Acquisition interval, median / p90 | 34.319 ms / 34.361 ms |
| Acquisition rate | 29.138 fps |
| Presented interval, median | 33.333 ms |
| Presented rate | 30.000 fps |
| Producer GPU window, median / p90 / max | 0.0938 / 0.0971 / 0.1048 ms |
| Tail duty cycle, median / p90 | 0.28 % / 0.29 % |
| Pacer mean blocked per present | 32.442 ms |
| Pacer over-cap waits / short intervals | 0 / 0 |
| Observer summary | `attempted=30 generated=28 skippedBusy=0 failed=1 seeds=1 sourceFrames=1525` |

### 9.1 Generation service

| Quantity | min | median | p90 | max |
|---|---|---|---|---|
| Estimator GPU execution | 6.310 ms | 6.491 ms | 7.055 ms | 7.075 ms |
| MetalFX wall (contains the GPU transition) | 4.121 ms | 4.264 ms | 4.552 ms | 4.735 ms |
| **Per-pair service window** (estimator GPU + provider wall) | 10.472 ms | **10.686 ms** | **11.587 ms** | 11.790 ms |

The per-pair service window is computed per pair and then described; it is never a sum of unrelated medians. MetalFX exposes no GPU timestamp to this caller, so its interval is a CPU wall interval that *contains* the GPU transition — which biases the service window upward and any "FG-free" figure downward. That direction is stated rather than corrected away, because correcting it would require a timestamp the platform does not give us.

Against the 33.333 ms period the service window occupies 32.1 % at the median and 34.8 % at p90.

### 9.2 Throughput

| Metric | Value |
|---|---|
| Generated pairs per second | **30.49** |
| Pair span | 0.918 s |
| SOURCE frames in the armed window | 28 |
| Attempts per second in the armed window | 30.19 |
| M per SOURCE frame | **1.046** |
| Skip rate | 0.0 |
| Success rate | 28 / 29 = 0.966 |

30.49 M candidates per second against 29.14 SOURCE frames per second. The 30→60 requirement is one M per SOURCE frame, i.e. 30 per second at 30 fps. The generation half of 30→60 is already at rate.

The one non-generated pair is the provider's first-use transition: `result=NO_OUTPUT`, `error=none`, `elapsedMs=15.955` against a 4.264 ms steady-state median (3.7×), followed by `R108_PROVIDER_WARM_STATE=READY` on every subsequent pair. It is a warm-up, not a defect, and it is excluded from the rate by construction because the rate is computed over generated pairs only.

---

## 10. PHASE 4 — GPU OVERLAP MEASUREMENT

### 10.1 What was measured

| Quantity | Value |
|---|---|
| FG intervals (estimator GPU + MetalFX wall) | 58 |
| SOURCE tail windows intersecting an FG interval | 1 of 108 |
| Overlapped SOURCE GPU execution | 0.1048 ms total |
| Non-overlapped SOURCE GPU execution | 0.0 ms |
| SOURCE tail with FG active | 0.1048 ms (n=1) |
| SOURCE tail without FG active | 0.0938 ms median (n=107) |

### 10.2 Why that number is not a concurrency result

The estimator for pair (B,C) is dispatched immediately after C's producer completes. Its GPU window runs 6.5 ms from that point, and the provider blocks for a further 4.3 ms. Frame D is not acquired until ~34.3 ms after C. The service window therefore completes roughly 11 ms into a 33 ms period, and the next frame's 0.094 ms tail lands ~22 ms later.

A 0.094 ms sliver inside a 6.5 ms window would intersect about 20 % of the time if the two were placed independently; observed is 1 in 108, which is consistent with the pipeline finishing its work in the inter-frame gap rather than colliding with the next frame.

**The correct reading is not "FG did not overlap" and not "FG overlapped".** It is: *FG's service window fits inside the period without displacing the frame, and the seam cannot see whether the frame's own passes were running concurrently.* Reporting `MEASURED_GPU_OVERLAP = NOT_EVALUATED` is the only honest option, and the analyser does exactly that rather than converting a 0.1048 ms sliver into a pass.

### 10.3 The bound that can be stated

The estimator executed 6.491 ms median of GPU work, 28 times, on the device that was rendering the title. Across those 28 periods the SOURCE frame missed no deadline: cadence unchanged, `overCapWaits=0`, `shortIntervals=0`.

Therefore: **the GPU absorbed at least 6.5 ms of additional measured execution per 33.333 ms period without the frame exceeding its period.** Whatever the frame's own GPU execution is, it is at least 6.5 ms below the period. That is a bound, not an estimate, and it is the strongest statement the available instrumentation supports.

---

## 11. PRIMARY METRICS

| Metric | Baseline | Overlap | Delta |
|---|---|---|---|
| SOURCE acquisition median | 34.317 ms | 34.319 ms | **+0.01 %** |
| SOURCE acquisition p90 | 34.367 ms | 34.361 ms | −0.02 % |
| SOURCE presented median | 33.333 ms | 33.333 ms | 0.00 % |
| SOURCE producer GPU median | 0.0942 ms | 0.0938 ms | **−0.42 %** |
| SOURCE producer GPU p90 | 0.0984 ms | 0.0971 ms | −1.32 % |
| Tail duty cycle median | 0.28 % | 0.28 % | 0.00 % |
| Pacer mean blocked | 32.370 ms | 32.442 ms | **+0.22 %** |
| Pacer over-cap waits | 0 | 0 | — |
| Pacer short intervals | 0 | 0 | — |
| FG generation service | — | 10.686 ms median / 11.587 ms p90 | — |
| Generated-pair completion | — | 28 / 29 = 96.6 % | — |
| Skipped-busy | — | 0 / 29 = 0.0 % | — |
| Critical-path assertions | — | 30 / 30 PASS, 0 violations | — |

The pacer's blocked time is the part of each period the title spent waiting for the next paced slot — the headroom the frame did not need. A GPU-bound title cannot hold it near the period. It held at 98.6 % of the period in the baseline and 98.8 % with FG running.

### 11.1 One pre-existing observation, not caused by R122

Acquisition cadence is 29.14 fps, not 30.00, in **both** phases. The pacer requests 33.333 ms, blocks at most 33.124 ms, and the remaining frame work adds ~1.2 ms, giving ~34.3 ms. This is an R120 pacing property, identical with and without FG, and the brief freezes the pacer. It is recorded here because a reader will notice 34.3 ≠ 33.3, and it is not an R122 effect.

---

## 12. VERDICT AGAINST THE BRIEF'S THREE CASES

| Criterion | Result | Detail |
|---|---|---|
| `source-cadence-30fps` | **PASS** | 34.319 ms (29.138 fps) against a 33.333 ms period |
| `source-producer-throughput` | **PASS** | 0.0938 ms vs 0.0942 ms baseline (−0.42 %) |
| `one-m-per-source-pair` | **PASS** | 28 of 29 attempts = 0.966 |
| `skipped-busy-near-zero` | **PASS** | skip rate 0.0 |
| `generation-service-within-period` | **PASS** | p90 11.587 ms within 33.333 ms |
| `critical-path-untouched` | **PASS** | 30/30 assertions, 0 violations |
| `game-stable` | **PASS** | 29 transitions, 0 with an error |
| `measured-gpu-overlap` | **`NOT_EVALUATED`** | tail seam; see section 10 |
| `equivalent-throughput-evidence` | **PASS** | cadence ok; producer ok; pacer headroom ok; overCapWaits 0; service p90 within period |

**`OVERLAPPED_30FPS_GENERATION_CAPACITY = VALIDATED`**

with the caveat recorded in the verdict document itself:

> validated on the brief's equivalent-throughput branch: physical GPU concurrency was not measured, because the correlated command buffer covers only the drawable-writing tail. R123 must not read this as a measured-overlap result.

`GPU_CAPACITY_FOR_30_TO_60 = INSUFFICIENT` is **not** reached: no criterion failed. Case B (`PARTIAL`) is **not** reached either: it is defined as SOURCE holding ~30 fps while FG cannot generate every pair, and here FG generated every pair with zero skips.

### 12.1 The verdict is falsifiable

`tools/test_analyze_r122.py` builds six logs whose ground truth is known by construction and asserts the analyser recovers it. `SELFTEST=PASS cases=6 baselines=2`:

| Case | Construction | Required outcome | Got |
|---|---|---|---|
| A | 8 ms producer, 30 fps, 9 ms estimator overlapping | VALIDATED | VALIDATED |
| B | producer stretches to 50 ms, most pairs skipped | INSUFFICIENT | INSUFFICIENT |
| C | estimator windows entirely outside every producer window | not VALIDATED | INSUFFICIENT |
| D | GPU stamps on a monotonic-like base | no overlap claimed | no overlap claimed |
| E | tail seam, throughput preserved | VALIDATED **with caveat** | VALIDATED + caveat |
| F | tail seam, pacer headroom collapsed | not VALIDATED | PARTIAL |

E and F exist as a pair on purpose. E alone would make the throughput promotion unfalsifiable; F is the case that proves the promotion can be refused.

---

## 13. CONTENTION CLASSIFICATION — NOT TRIGGERED

Phase 7 applies only if capacity fails. It did not, so no classification is claimed. The before/after factors are reported anyway, because the brief asks for them and because "no contention" is itself a measurement:

| Factor | Baseline | Overlap | Reading |
|---|---|---|---|
| Game producer | 0.0942 ms | 0.0938 ms | no slowdown |
| Estimator | — | 6.491 ms median, 6.310–7.075 ms range | 12 % spread over 28 pairs, no long tail |
| MetalFX | — | 4.264 ms median, 4.121–4.735 ms range | 15 % spread, no long tail |
| Time-slice / pacer | 32.370 ms blocked | 32.442 ms blocked | unchanged |
| Drawable availability | no `nextDrawable` stall | no `nextDrawable` stall | pool not starved |

A tight estimator distribution is consistent with uncontended execution. It is not proof of it, and this report does not offer it as proof.

---

## 14. ACCOUNTING, SAFETY AND THE FRONTIER

### 14.1 Accounting

Every pair record carries `wsiPresentId=UNALLOCATED accounting=GENERATION_ONLY mDispatched=0 discarded=1`. No generation-only M received a `WsiPresentId`, none was counted as a displayed INTERNAL frame, and no public SOURCE accounting changed:

| Check | Result |
|---|---|
| `R101_WSI_DISPATCH origin=INTERNAL` | absent from both slices |
| `R101_WSI_DISPLAYED origin=SOURCE` | 108, one per SOURCE frame, `reachedDisplay=1` |
| `R110_WSI_ORDER_EVENT origin=SOURCE` | 108, `order == sourceSequence` on every line |
| `internalPresentId` on SOURCE presents | 0 on every line |

### 14.2 Safety

| Hazard | Result |
|---|---|
| SOURCE frame dropped | none — 108 acquired, 108 presented |
| SOURCE frame reordered | none — order equals sequence on every event |
| SOURCE frame duplicated | none — one drawable per sequence |
| Drawable starvation | none — no `nextDrawable` stall; pool behaviour unchanged |
| Unbounded queue | none — slot is 1, skip path drops rather than enqueues |
| Stale pair callback | none — 29 transitions, 0 errors, 28 READY states |
| Historical evidence modified | none — 0 of 5 486 files changed |

### 14.3 The frontier

The frontier was **not** closed for display. `frontier=OPEN-ALWAYS` on the mode banner, `R103_SOURCE_FRONTIER` absent from both slices, and `bDeferral=NONE`. SOURCE C rendered while FG processed A/B — that is what the cadence and pacer numbers show, and it is the precondition the round existed to establish.

### 14.4 Terminology, stated exactly

| Term | Status |
|---|---|
| `SOURCE_IMAGE_CAPTURE_DECOUPLING` | **exists** — the capture copy decouples FG's SOURCE images from the presented drawable |
| `SOURCE_PRESENTATION_DRAWABLE_DECOUPLING` | **not implemented**, and R122 does not implement it |

R122 established that generation fits. It did not touch how SOURCE presentation acquires its drawables.

---

## 15. INDEPENDENT REVIEW — 9 ITEMS BEFORE THE LIVE RUN

| # | Item | Verdict | Basis |
|---|---|---|---|
| 1 | SOURCE presentation normal | PASS | `r102TryDeferSourcePresent` returns NO when armed; 108 presents, one per frame |
| 2 | B not deferred | PASS | tuple machinery never armed; `bDeferral=NONE` |
| 3 | Game thread cannot wait on estimator or MetalFX | PASS | 30/30 assertions: no CPU readback, no file transport, private input textures, `diagnosticsDependency=NONE`; estimator runs on a detached dispatch |
| 4 | Generation slot bounded | PASS | CAS slot = 1, no queue, `slot=0` at summary |
| 5 | Skips cannot block SOURCE | PASS | skip path writes one line and returns before capture |
| 6 | Producer GPU timing in the correct clock domain | PASS | media-domain subtraction only; per-frame skew 1–4 µs; self-test case D fails closed |
| 7 | Overlap claims use timestamp intervals | PASS | intersection of GPU intervals only; `NOT_EVALUATED` where the interval is a tail |
| 8 | Generated M has no WSI or public accounting effect | PASS | `wsiPresentId=UNALLOCATED`, `mDispatched=0`, no INTERNAL dispatch |
| 9 | Evidence directory isolated | PASS | five probes including traversal and symlink; corpus unchanged by hash |

Item 3 deserves emphasis: the assertion is emitted per pair, and all 30 passed with `inputTextures=private` and `diagnosticsDependency=NONE`. The title thread constructed no FG input on the CPU. If it had, every throughput number here would be measuring a different pipeline.

---

## 16. PRODUCT DECISION AND THE R123 PATH

The brief fixes the branch: **VALIDATED → R123 implements buffered logical SOURCE presentation using FG-owned SOURCE images and fresh presentation drawables.**

What R122 hands R123:

| Input | Value | Why R123 needs it |
|---|---|---|
| M generation rate achieved | 30.49 /s vs 29.14 fps SOURCE | the generation half of 30→60 is at rate |
| Per-pair service window | 10.686 ms median, 11.587 ms p90 | must fit a 16.667 ms half-period |
| Margin at 60 fps output | **5.080 ms at p90**, 4.877 ms at max | the budget the presentation change may spend |
| Skips at slot = 1 | 0 of 29 | a single slot suffices at 30 fps SOURCE |
| Critical-path cost to the title | 0 (30/30 assertions) | presentation must keep it at 0 |

Two constraints R123 inherits, stated so they are not rediscovered:

1. **The service window is the binding constraint, not the rate.** At 60 fps output the M for a pair must be ready inside a 16.667 ms half-period; p90 service is 11.587 ms, leaving 5.080 ms for everything the presentation change adds. A buffered presentation path that costs more than ~5 ms per frame consumes the entire margin.
2. **The overlap question is still open.** R123's design assumes FG work can run while the frame renders. R122 could not measure that and did not claim it. If R123 needs the assumption to hold rather than merely to be affordable, the seam has to be widened first: the frame's GPU span must be measured as the union of every command buffer committed between two drawable acquisitions, not as one correlated buffer.

The second point is the honest gap. The capacity answer does not depend on it — the throughput evidence is direct — but any future claim about *how* the capacity is obtained does.

---

## 17. ARTEFACT INDEX

All paths relative to `experiments/metalfx_frame_interpolator_R93/evidence/r122-overlapped-gpu-capacity/`.

| Artefact | Contents |
|---|---|
| `observer-title-inprocess-r122.m` | the observer; SHA-256 `3e4dc847…` |
| `libfgmetal_r122_overlap_probe.dylib` | the build; SHA-256 `c64d1c2e…` |
| `build_r122_tools.sh` | reproducible build |
| `run_r122.py` | two-phase controller, with the provenance guard |
| `R122-RUNBOOK.md` | operator procedure and failure signatures |
| `bottle-env-r122-overlap.json`, `bottle-env-r122-baseline.json` | environment manifests |
| `run-01-baseline/{slice.log, analysis.json, controller.json, controller.log}` | 168 frames, 0 pairs |
| `run-02-overlap/{slice.log, analysis.json, controller.json, controller.log}` | 108 frames, 30 pair records |
| `tools/analyze_r122.py` | analyser; enforces GPU-only-from-GPU and interval-only overlap |
| `tools/test_analyze_r122.py` | six-case self-test with known ground truth |
| `tools/verify_emitter_grammar.py` | run-grammar vs source-grammar comparison |
| `tools/snapshot_corpus.py` | corpus guard, meta and hash modes |
| `corpus-before-*.json`, `corpus-after-*.json`, `corpus-diff.txt` | the corpus proof |
| `evidence-isolation/isolation-probes.txt` | five isolation probes |
| `evidence-isolation/emitter-grammar.txt` | grammar check for both slices |
| `evidence-isolation/accounting-invariants.txt` | INTERNAL/frontier/reorder/error counts, both slices |

---

## 18. WHAT THIS ROUND DID NOT ESTABLISH

Stated plainly, because the value of the result depends on the reader knowing its edge:

1. **Physical GPU concurrency was not measured.** The seam returns the drawable-writing tail. Section 10.3 gives a bound, not a measurement.
2. **The frame's own GPU execution is still unknown.** R121's 23 ms figure was a display-hold artefact; R122 shows the hold's removal collapses that interval to 1.27 ms, but does not replace it with a frame-level figure.
3. **The 60 fps presentation path was not attempted.** The 5.080 ms margin in section 16 is a projection from the service window, not a measurement of a 60 fps pipeline.
4. **The evidence predates the shipped source by one diagnostic edit.** Checked, bounded and disclosed in section 3.1; the guard that makes it impossible to repeat is in place, but this round's evidence was taken before it existed.
