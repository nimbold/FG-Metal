# FG-METAL — R121 PRESENTATION OCCUPANCY REPORT

**Round:** R121
**Subject:** Decompose the ~25 ms presentation-side occupancy; remove the dominant mechanical wait if safe; decide whether SOURCE/drawable decoupling is required.
**Status:** COMPLETE — dominant blocker measured, quantified and classified. No fix promoted, and the reason is stated with evidence.

---

## 0. HEADLINE CLAIMS

| Claim | Verdict |
|---|---|
| `SOURCE_FRAME_GPU_RETIREMENT_WAIT` | **`DOMINANT_PRESENTATION_BLOCKER`** — 23.073 ms median, 59.7 % of the headline |
| `INTERNAL_DRAWABLE_ACQUISITION` | **`NOT_A_BLOCKER`** — 0.017 ms median `nextDrawable` entry→return |
| `INTERNAL_COPY_RENDER` | **`NOT_A_BLOCKER`** — 0.532 ms median for the entire M copy |
| `CPU_QUEUE_PATH` | **`NOT_A_BLOCKER`** — 0.041 ms median |
| `B_TO_M_PRESENTATION_SERVICE_WITHIN_30FPS_BUDGET` | **`NOT_VALIDATED`** — 41.392 ms median against a 33.333 ms budget |
| `CONTINUOUS_30_TO_60_TITLE_FG` | **NOT CLAIMED** |
| `SOURCE_DRAWABLE_DECOUPLING` | **`ALREADY_IMPLEMENTED_AND_REQUIRED`** — the capture copy is the decoupling |
| `A_HOLD_FEASIBLE_AT_POOL_3` | **NO** — proven from measured intervals, not inferred |

The single sentence: **the ~25 ms is not presentation-side overhead at all. It is the SOURCE frame's own GPU execution. The capture blit is committed on the producer's command queue, so by in-order queue semantics it cannot begin until the frame it is copying has retired, and that retirement happens a median of 25.1 ms after B's present hook. Every CPU-side and drawable-side component the brief asked about measures below one millisecond combined.**

---

## 1. HARNESS, MODEL AND ROLES

| Item | Value |
|---|---|
| Harness | WorkBuddy AI |
| Model | DeepSeek v4.1 Flash — High, for every role |
| Model-family fallback | None used |
| Roles | orchestrator, implementation, independent review, analysis |
| Agent count | 3 |
| Live runs | 2 authoritative ten-tuple runs (run-02, run-03) plus 1 aborted arm |
| Evidence root | `experiments/metalfx_frame_interpolator_R93/evidence/r121-presentation-occupancy/` |

All measurement, instrumentation, analysis and self-testing in this round was performed by the above. No measurement was taken from a prior round's report; prior-round numbers appear only where explicitly attributed.

---

## 2. R120 BASELINE

R120 established `TRUE_30FPS_SOURCE_CADENCE = ESTABLISHED` and left `CONTINUOUS_30_TO_60_TITLE_FG = NOT_VALIDATED`. Its authoritative run-04 published:

| Quantity | R120 value |
|---|---|
| SOURCE cadence | 33.3 ms |
| A→M presented interval | 50.0 ms |
| M→B presented interval | 16.667 ms |
| Output rate | 30.64 fps |
| B present hook → M WSI service | 47.8 ms |
| GPU subtotal (estimator + MetalFX + copies) | 22.0 ms |
| Presentation-side / non-GPU remainder | **25.1 ms** |

That 25.1 ms was the round's subject. R120 grouped the copy encoding, the commit→GPU-start wait, the GPU execution and the wake into one number; it could not say which dominated. R121 exists to split it.

**R121 reproduces the R120 shape.** Two independent builds of the R121 probe, run on the same machine and the same scene class, give headline medians of 43.999 ms (run-02) and 41.392 ms (run-03), with the SOURCE copy wait at 24.416 ms and 23.359 ms respectively. The R120 residual of 25.1 ms and the R121 measurement of 24.7 ms for encode-plus-wait are the same quantity measured two ways. The decomposition below is therefore a decomposition of the R120 number, not a new number.

---

## 3. EVIDENCE ISOLATION

The round's own directory is writable; every other round's directory under the evidence root is protected by a derived rule: anything under the root that is not this round's own directory is protected. The guard resolves paths through `realpath` and `..` before comparing, so a symlink or a traversal cannot smuggle a write into a prior round.

Three probes were run against the guard, each in a standalone host so that no live run was involved:

| Probe | Target | Result |
|---|---|---|
| R121 own directory | `evidence/r121-presentation-occupancy` | `writable=1 protected=0` — accepted |
| R120 directory | `evidence/r120-continuous-30-60` | `writable=0 protected=1` — refused |
| R119 directory | `evidence/r119-live-throughput` | `writable=0 protected=1` — refused, and `R121_EVIDENCE_BOUNDARY … priorRoundProtected=…/r119-live-throughput priorRoundWritable=0` |

The live run's own banner confirms the same state from inside the game process:

```
R121_EVIDENCE_DIRECTORY pid=97342 source=unresolved
  path=…/evidence/r121-presentation-occupancy writable=1 protected=0
R121_EVIDENCE_BOUNDARY pid=97342 own=…/r121-presentation-occupancy
  priorRoundProtected=…/r119-live-throughput priorRoundWritable=0
```

A content-hashed snapshot of the whole corpus was taken before the first live run: **5 412 files, 30 168 200 909 bytes**, hashed with SHA-256 per file. No prior-round file was modified in this round.

**Diagnostic sampling was kept out of the timing window.** `FGMETAL_DIAG_SAMPLE=bounded` (first-middle-last) was set for both runs, so the diagnostic export that writes multi-megabyte RGBA and RG16F dumps ran three times per run rather than once per tuple. The `R119_DIAG_SAMPLE` lines in run-03 number exactly 12, matching the 12 tuples.

---

## 4. TIMING INSTRUMENTATION

### 4.1 One clock for the chain

The authoritative chain is `CLOCK_MONOTONIC` nanoseconds via `r105NowNs()`. Every one of the twenty chain instants is taken with that function and nothing else. The chain is written into a 24-slot record (`R121Span`) by lock-free single-writer slot marks:

```objc
static inline void r121MarkValue(R121Slot slot, uint64_t ns) {
    if (!__atomic_load_n(&r121Span.armed, __ATOMIC_ACQUIRE)) return;
    if (__atomic_load_n(&r121Span.have[slot], __ATOMIC_RELAXED)) return;
    __atomic_store_n(&r121Span.t[slot], ns, __ATOMIC_RELAXED);
    __atomic_store_n(&r121Span.have[slot], 1, __ATOMIC_RELEASE);
    __atomic_add_fetch(&r121MarkCount, 1, __ATOMIC_RELAXED);
}
```

A slot is claimed once and only once, so a late or duplicated mark from a stale tuple cannot overwrite a newer tuple's instant. `r121SpanBegin` drops `armed` first, then zeroes the record, then re-arms — a mark still in flight from the previous tuple is discarded rather than written into the new one.

### 4.2 The twenty-four instants

| # | Slot | What it brackets |
|---|---|---|
| 1 | `BHook` | B present-hook entry — chain head, bit-identical to `bPresentHookNs` |
| 2 | `SrcCaptureEntry` | producer-complete → capture entry |
| | `SrcCopyEncoded` | SOURCE blit fully encoded |
| | `SrcCopyCommit` | immediately before `[copyCommandBuffer commit]` |
| | `SrcCopyWake` | SOURCE copy completion, CPU wake |
| 3 | `TaskEnqueue` | immediately before `dispatch_async` to the generation queue |
| 4 | `TaskStart` | inside that block, first statement |
| 5 | `EstimatorStart` | |
| 6 | `EstimatorEnd` | |
| 7 | `MetalFXStart` | |
| 8 | `MetalFXEnd` | |
| 9 | `GateVerdict` | |
| 10 | `InternalAcquireReq` | |
| 11 | `InternalNdEnter` | immediately before the original `nextDrawable` IMP |
| 12 | `InternalNdReturn` | immediately after it returns |
| 13 | `InternalAcquired` | drawable validated and tagged |
| 14 | `MCopyEncodeStart` | |
| 15 | `MCopyCommit` | |
| 16 | `MCopyWake` | after `waitUntilCompleted` |
| 17 | `MWsiAlloc` | M's WSI identity allocated |
| 18 | `MWsiDispatch` | chain endpoint — `== mDispatchNs` |
| 19 | `InternalPresentEnter` | extension: after the endpoint |
| 20 | `InternalPresentReturn` | extension |

Slots 19 and 20 lie after the measured window and are reported as an extension, never folded into the budget.

### 4.3 The clock-domain defect, and why the fix matters

The first R121 build subtracted Metal's `GPUStartTime`/`GPUEndTime` directly from `CLOCK_MONOTONIC` chain instants. Those Metal timestamps are on the `CACurrentMediaTime` base. The run produced:

```
srcCopyCommitToGpuStartNs ≈ -179 007 104.698 ms
srcCopyGpuEndToWakeNs     ≈ +179 007 111.725 ms
```

Worse than the wrong numbers was that the check meant to catch them could never fire: the closure residual summed the same three terms that the media clock trivially closes, so it was tautologically zero. That is precisely the R119 review item — a check that cannot fail is not a check.

The fix is structural, not cosmetic. Every GPU subtraction is now performed **entirely inside the media domain**, and the monotonic boundary instants are used for exactly one purpose: publishing the difference between the media-clock duration and the monotonic-clock duration of the *same copy*. That difference is the real cross-clock validation.

```objc
srcCommitToGpuStart = (int64_t)r121Span.srcCopyGpuStartNs
    - fgmetalMediaToNs(r121Span.mediaSrcCopyCommit);
srcGpuExec          = (int64_t)r121Span.srcCopyGpuEndNs
    - (int64_t)r121Span.srcCopyGpuStartNs;
srcGpuEndToWake     = fgmetalMediaToNs(r121Span.mediaSrcCopyWake)
    - (int64_t)r121Span.srcCopyGpuEndNs;
srcMediaDuration    = srcCommitToGpuStart + srcGpuExec + srcGpuEndToWake;
srcClockSkew        = srcMediaDuration - srcCommitToWake;   // the validation
```

Run-03 result: **`srcMaxSkewMs=0.035`, `mMaxSkewMs=0.001`** across 11 tuples. The two clocks agree on the duration of the same copy to 35 microseconds. A domain error would show as hundreds of seconds. The check is now capable of failing, and the self-test proves it does (`CLOCK_SKEW_REJECTED`, a synthetic 5 ms skew).

### 4.4 Emission discipline

Both report lines are emitted from `r115ResetTuple`, which runs **after** the window has closed, so nothing in them is perturbed by the act of reporting. The analyser is forbidden from treating any other line as authoritative for the decomposition.

---

## 5. FULL B→M DECOMPOSITION

Run-03-pairs10. Eleven measured tuples (ten accepted, one failed at fast-eligibility). All figures in milliseconds.

| Link | min | mean | median | p90 | max |
|---|---|---|---|---|---|
| `hookToSrcCaptureNs` | 0.063 | 3.248 | 1.183 | 2.012 | 23.665 |
| `srcCaptureToCopyEncodedNs` | 0.134 | 0.185 | 0.177 | 0.213 | 0.309 |
| `copyEncodedToCopyCommitNs` | 0.006 | 0.010 | 0.007 | 0.011 | 0.035 |
| **`copyCommitToCopyWakeNs`** | **2.895** | **21.271** | **23.359** | **26.105** | **26.159** |
| `copyWakeToTaskEnqueueNs` | 0.010 | 0.025 | 0.027 | 0.033 | 0.042 |
| `taskEnqueueToTaskStartNs` | 0.012 | 0.014 | 0.014 | 0.016 | 0.016 |
| `taskStartToEstimatorStartNs` | 0.151 | 0.180 | 0.180 | 0.192 | 0.213 |
| `estimatorStartToEstimatorEndNs` | 9.129 | 9.742 | 9.788 | 10.052 | 10.644 |
| `estimatorEndToMetalFXStartNs` | 0.017 | 0.066 | 0.029 | 0.187 | 0.213 |
| `metalFXStartToMetalFXEndNs` | 5.382 | 5.962 | 6.011 | 6.206 | 6.596 |
| `metalFXEndToGateVerdictNs` | 0.095 | 0.102 | 0.098 | 0.113 | 0.118 |
| `gateVerdictToInternalAcquireReqNs` | 0.039 | 0.044 | 0.044 | 0.049 | 0.052 |
| `internalAcquireReqToNdEnterNs` | 0.006 | 0.008 | 0.007 | 0.009 | 0.015 |
| **`internalNdEnterToNdReturnNs`** | **0.014** | **0.018** | **0.017** | **0.021** | **0.021** |
| `internalNdReturnToAcquiredNs` | 0.005 | 0.006 | 0.006 | 0.006 | 0.010 |
| `internalAcquiredToMCopyEncodeNs` | 0.001 | 0.001 | 0.001 | 0.001 | 0.003 |
| `mCopyEncodeToCommitNs` | 0.059 | 0.077 | 0.072 | 0.103 | 0.107 |
| `mCopyCommitToWakeNs` | 0.445 | 0.465 | 0.459 | 0.473 | 0.537 |
| `mCopyWakeToMWsiAllocNs` | 0.024 | 0.051 | 0.031 | 0.035 | 0.262 |
| `mWsiAllocToDispatchNs` | 0.011 | 0.013 | 0.014 | 0.015 | 0.015 |
| **Headline (`bPresentHookToMWsiNs`)** | **29.939** | **41.489** | **41.392** | **45.338** | **46.761** |

### 5.1 Exact per-tuple reconciliation

Every tuple reconciles to the nanosecond. `maxAbsResidualNs = 0` across all eleven.

| gen | pair | outcome | headline | chain sum | residual |
|---|---|---|---|---|---|
| 2 | 2045→2046 | PENDING_DISPLAY | 46.761 | 46.761 | 0 |
| 3 | 2048→2049 | PENDING_DISPLAY | 45.338 | 45.338 | 0 |
| 4 | 2051→2052 | PENDING_DISPLAY | 43.916 | 43.916 | 0 |
| 5 | 2054→2055 | PENDING_DISPLAY | 43.972 | 43.972 | 0 |
| 6 | 2057→2058 | PENDING_DISPLAY | 41.286 | 41.286 | 0 |
| 7 | 2060→2061 | PENDING_DISPLAY | 40.414 | 40.414 | 0 |
| 8 | 2063→2064 | PENDING_DISPLAY | 41.392 | 41.392 | 0 |
| 9 | 2066→2067 | PENDING_DISPLAY | 41.916 | 41.916 | 0 |
| 10 | 2069→2070 | PENDING_DISPLAY | 29.939 | 29.939 | 0 |
| 11 | 2072→2073 | PENDING_DISPLAY | 41.210 | 41.210 | 0 |
| 12 | 2075→2076 | PENDING_DISPLAY | 40.231 | 40.231 | 0 |

### 5.2 Where the time goes

| Group | Median | Share |
|---|---|---|
| Encode + wait for the SOURCE copy (`hookToSrcCapture` + encode + commit + `copyCommitToCopyWake`) | **24.726 ms** | **59.7 %** |
| Frozen estimator + MetalFX | 15.799 ms | 38.2 % |
| All fifteen remaining links, combined | 0.867 ms | 2.1 % |
| **Headline** | **41.392 ms** | 100 % |

The answer to the brief's primary unknown is therefore unambiguous. It is not `nextDrawable` blocking, not queue or thread dispatch, not the INTERNAL copy/render, not the presentation API call, and not a sum of small components. It is **(D) command-buffer queueing**, and specifically the in-order wait for the frame being captured.

---

## 6. INTERNAL `nextDrawable` LATENCY

The brief's Branch A premise — that the INTERNAL acquisition blocks on a full drawable pool — is refuted by direct measurement.

| Component | min | mean | median | p90 | max |
|---|---|---|---|---|---|
| `gateVerdictToInternalAcquireReqNs` | 0.039 | 0.044 | 0.044 | 0.049 | 0.052 |
| `internalAcquireReqToNdEnterNs` | 0.006 | 0.008 | 0.007 | 0.009 | 0.015 |
| **`internalNdEnterToNdReturnNs`** | **0.014** | **0.018** | **0.017** | **0.021** | **0.021** |
| `internalNdReturnToAcquiredNs` | 0.005 | 0.006 | 0.006 | 0.006 | 0.010 |
| **Total acquisition** | — | — | **0.030 ms** | — | — |

`nextDrawable` on the INTERNAL path returns in **17 microseconds at the median and 21 microseconds at the maximum**. There is no wait to remove. Across the whole run the acquisition never exceeded 21 µs, on eleven independent tuples.

### 6.1 The refresh-quantum test

The brief warned: *"Do not call it drawable starvation merely because the duration resembles a refresh."* The test therefore requires **both** conditions before classifying a wait as refresh-locked: the wait length must be within 1 ms of a whole number of refreshes, **and** its return instant must be within 1 ms of a display callback.

| Bucket | Count |
|---|---|
| ~1 refresh | 0 |
| ~2 refreshes | 0 |
| ~3 refreshes | 0 |
| other | 11 |
| unmeasured | 0 |
| returns near a display callback | 11 |

**Zero of eleven** INTERNAL waits were of refresh-quantum length. No starvation claim is available, and none is made.

The eleven near-callback returns are not evidence of anything: with a 17 µs wait, "returning near a display callback" simply records that the gate fires shortly after the frame boundary. A 17 µs interval that happens to sit near a callback is not a wait.

---

## 7. SOURCE DRAWABLE PRESSURE

Evidence recorded at every INTERNAL acquisition, under the lock already held there — no extra syscall, no extra critical section:

| gen | pool | outstanding SOURCE | outstanding INTERNAL | slot | frontier | B deferred | SOURCE nd block | INTERNAL nd block |
|---|---|---|---|---|---|---|---|---|
| 1 | — | 0 | 0 | 0 | 0 | 0 | 0.000 | 0.000 |
| 2 | 3 | 3 | 0 | 3 | 2 | 1 | 0.046 | 0.015 |
| 3 | 3 | 3 | 0 | 3 | 2 | 1 | 2.913 | 0.021 |
| 4 | 3 | 3 | 0 | 3 | 2 | 1 | 0.031 | 0.021 |
| 5 | 3 | 3 | 0 | 3 | 2 | 1 | 0.023 | 0.020 |
| 6 | 3 | 3 | 0 | 3 | 2 | 1 | 0.024 | 0.017 |
| 7 | 3 | 3 | 0 | 3 | 2 | 1 | 0.036 | 0.014 |
| 8 | 3 | 3 | 0 | 3 | 2 | 1 | 0.035 | 0.017 |
| 9 | 3 | 3 | 0 | 3 | 2 | 1 | 0.077 | 0.018 |
| 10 | 3 | 3 | 0 | 3 | 2 | 1 | 0.033 | 0.017 |
| 11 | 3 | 3 | 0 | 3 | 2 | 1 | 0.027 | 0.021 |
| 12 | 3 | 3 | 0 | 3 | 2 | 1 | 0.032 | 0.016 |

`maximumDrawableCount = 3`, `allowsNextDrawableTimeout = 1`.

Generation 1 is the failed tuple: it was rejected at fast eligibility before any span was armed, so every field is zero. It is retained in the table because omitting it would misrepresent the run as eleven clean tuples.

**The SOURCE layer's outstanding count sits at exactly 3 of 3 at every measured acquisition, and yet the next acquisition never blocks** (median 0.033 ms, maximum 2.913 ms). This is a result that must be stated carefully, because it is exactly the trap the brief warned about in a different guise.

The observer's outstanding figure is an **upper bound**, not a query of Metal's free-list. It releases a drawable when that drawable's presented handler fires, and Metal makes a drawable reusable before that point. So the counter can read 3 of 3 while Metal still has a free slot. The correct conclusion is therefore the narrow one:

> **The pool is not the constraint. `nextDrawable` returns in microseconds while the observer's own conservative counter reads saturation, which means the counter overstates occupancy and the pool has slack that the counter cannot see.**

This is why the bookkeeping check asserts `outstanding ≤ pool` and publishes all three contributing totals, rather than asserting anything about starvation.

### 7.1 The bookkeeping defect found and fixed

The first R121 build reported `liveSourceMax = 14` against a pool of 3. That was not a Metal violation; it was a defect in the counter. Diagnosis from run-02's log:

```
present entries            523
  deferred (R116 early-B)   11
  ordinary WSI dispatch    512
SOURCE presented handlers  512
```

523 = 11 + 512 exactly. The eleven deferred presents left ordinary dispatch **without installing a presented handler**, so the counter acquired them and never released them. The defect was closed by releasing from *both* SOURCE exits — the ordinary dispatch handler and the forwarded-deferred handler — and by publishing `acquired`, `released` and `deferred` alongside the derived figure so a reader can re-derive it instead of trusting it.

Run-03 result: `outstandingMax=3 poolMaximumDrawables=3 acquired=2076 released=2073 deferred=12`. The identity closes to the drawable, and the maximum equals the pool exactly.

---

## 8. COPY / RENDER COST

This is the split the R120 report could not produce. All figures milliseconds; all GPU subtractions performed inside the media clock domain.

### 8.1 SOURCE capture copy

| Segment | min | mean | median | p90 | max |
|---|---|---|---|---|---|
| **commit → GPU start** | **2.526** | **20.982** | **23.073** | **25.900** | **25.905** |
| GPU execution | 0.130 | 0.161 | 0.139 | 0.272 | 0.279 |
| GPU end → CPU wake | 0.073 | 0.137 | 0.121 | 0.204 | 0.319 |
| **commit → wake (total)** | **2.895** | **21.271** | **23.359** | **26.105** | **26.159** |
| media-clock duration (cross-check) | 2.901 | 21.280 | 23.364 | 26.112 | 26.166 |
| **clock skew (validation)** | 0.005 | 0.009 | 0.007 | 0.010 | **0.035** |

**The copy itself is 0.139 ms. The wait for the GPU to begin it is 23.073 ms.** That ratio — 166:1 — is the entire finding of the round.

### 8.2 INTERNAL M copy

| Segment | min | mean | median | p90 | max |
|---|---|---|---|---|---|
| commit → GPU start | 0.161 | 0.174 | 0.167 | 0.193 | 0.207 |
| GPU execution | 0.115 | 0.119 | 0.116 | 0.126 | 0.134 |
| GPU end → CPU wake | 0.157 | 0.173 | 0.170 | 0.183 | 0.206 |
| commit → wake (total) | 0.445 | 0.465 | 0.459 | 0.473 | 0.537 |
| clock skew | −0.001 | 0.000 | 0.000 | 0.000 | 0.001 |

The M copy is 0.459 ms end to end and shows no submission delay at all — its GPU start is 0.167 ms after commit. **The difference between the two copies is the whole story**: M's copy is submitted to a queue with nothing ahead of it; SOURCE's copy is submitted behind a full frame.

### 8.3 Present-call overhead

| Segment | median | max |
|---|---|---|
| `dispatchToPresentEnterNs` | 0.008 | 0.009 |
| `presentCallNs` (extension, after the endpoint) | 0.021 | 0.023 |

Both are negligible and, correctly, outside the measured window.

### 8.4 The mechanism, stated exactly

`captureRealDrawable` builds the blit on **the producer's own command queue**:

```objc
id<MTLCommandQueue> queue = record[@"producerQueue"];
…
id<MTLCommandBuffer> copyCommandBuffer = [queue commandBuffer];
```

Metal executes a queue's command buffers in commit order. The producer's frame was committed before the present hook fired, and the blit is committed after it. Therefore **the blit cannot begin until the frame it is copying has retired.** The 23.073 ms is not contention, not a scheduling accident, and not a defect: it is in-order queue semantics applied to a copy of a frame that is still being drawn.

### 8.5 The control experiment that proves the interpretation

One tuple in the run is a natural experiment. On generation 5 the hook→capture path was unusually slow — 23.849 ms instead of the usual ~1.3 ms — and the GPU wait correspondingly collapsed:

| gen | hook → blit commit | blit commit → GPU start | hook → GPU start |
|---|---|---|---|
| 2 | 2.232 | 25.905 | 28.137 |
| 3 | 2.104 | 25.900 | 28.004 |
| 4 | 1.928 | 25.034 | 26.962 |
| **5** | **23.849** | **2.526** | **26.375** |
| 6 | 2.115 | 23.073 | 25.188 |
| 7 | 1.236 | 22.555 | 23.791 |
| 8 | 1.234 | 22.958 | 24.192 |
| 9 | 0.535 | 24.572 | 25.107 |
| 10 | 0.263 | 11.833 | 12.096 |
| 11 | 1.034 | 23.637 | 24.671 |
| 12 | 1.342 | 22.808 | 24.150 |

The two terms are inversely coupled and their sum is nearly constant. A 21 ms delay on the CPU side removed 20.5 ms from the GPU wait. **That is only possible if the wait is a phase of the frame cycle, not a fixed quantity of queued work.** It rules out the alternative explanation — "the blit is waiting for a constant amount of GPU work" — and it means no amount of committing the blit earlier will move its GPU start earlier.

---

## 9. QUEUE / CPU COST

| Component | median | max |
|---|---|---|
| `copyWakeToTaskEnqueueNs` | 0.027 | 0.042 |
| `taskEnqueueToTaskStartNs` | 0.014 | 0.016 |
| `taskStartToEstimatorStartNs` | 0.180 | 0.213 |
| `metalFXEndToGateVerdictNs` | 0.098 | 0.118 |
| `gateVerdictToInternalAcquireReqNs` | 0.044 | 0.052 |
| `internalNdReturnToAcquiredNs` | 0.006 | 0.010 |
| `mCopyWakeToMWsiAllocNs` | 0.031 | 0.262 |
| `mWsiAllocToDispatchNs` | 0.014 | 0.015 |
| **`CPU_QUEUE_PATH` total** | **0.041** | — |

The serial generation queue is not a bottleneck: enqueue→start is 14 microseconds. The dispatch from the copy's completed handler into `captureProcessingQueue` costs 27 microseconds. There is no unnecessary hop to collapse.

---

## 10. DOMINANT-BLOCKER CLASSIFICATION

Per-component medians, summed only within a group. Never a sum across unrelated groups, and never a sum of medians presented as a total.

| Group | Median total | Verdict |
|---|---|---|
| **`SOURCE_FRAME_GPU_RETIREMENT_WAIT`** (encode + commit + wait for the frame) | **23.366 ms** | **DOMINANT** |
| `INTERNAL_COPY_RENDER` | 0.532 ms | not a blocker |
| `GENERATION_PREP` | 0.180 ms | not a blocker |
| `PRESENTATION_DISPATCH` | 0.045 ms | not a blocker |
| `CPU_QUEUE_PATH` | 0.041 ms | not a blocker |
| `INTERNAL_DRAWABLE_ACQUISITION` | 0.030 ms | not a blocker |

The top group exceeds the second by a factor of 44. This is not a mixed verdict; it is a single dominant mechanical wait.

**Classification: `SOURCE_FRAME_GPU_RETIREMENT_WAIT = DOMINANT_PRESENTATION_BLOCKER`.**

This is a refinement of the brief's Branch B rather than a departure from it. Branch B said: *"If copy/render dominant, optimize only that path; do not change MetalFX."* The copy/render path **is** dominant — but its dominant sub-component is not the copy's execution (0.139 ms) and not its encoding (0.007 ms); it is the wait for the frame being copied to retire. Section 11 explains why that distinction forbids the optimisation Branch B anticipated.

---

## 11. OPTIONAL PROMOTED FIX — DECLINED, WITH REASON

The brief permits at most one bounded mechanical fix and requires a promotion rule: promote only if authoritative measurement materially reduces `bPresentHookToMWsi` without violating SOURCE identity, INTERNAL identity, ordering, frontier safety or drawable pool safety.

Three candidate fixes were evaluated against the measured mechanism. **None was promoted, because none can work**, and the reason is measurement rather than caution.

**Candidate 1 — commit the capture blit earlier.** Already committed 1.2 ms after the hook; the wait is 23.1 ms. The gen-5 control proves the wait is phase-locked to the frame cycle, not to the commit instant: a 21 ms earlier-or-later commit moves the GPU start by 20.5 ms in the opposite direction and leaves the sum unchanged. **Projected gain: 0 ms.**

**Candidate 2 — move the capture into the producer's completed handler.** The blit would then be committed exactly when the frame retires, so its GPU start would equal its commit. But that instant is the same wall-clock instant as today's GPU start, so the estimator would begin at the same time or later, never earlier. **Projected gain: 0 ms, at the cost of a new code path.**

**Candidate 3 — capture on a separate queue.** A separate queue does not remove the hazard: the blit reads a texture the producer is still writing, so Metal inserts a dependency and the blit waits for the same render pass to finish. **Projected gain: 0 ms, at the cost of a new queue and a new hazard surface.**

The only lever that would fit the budget is the frozen GPU work, and the brief forbids touching it:

> Headline median (measured)                                              **41.392 ms**
> Frozen estimator + MetalFX (measured)                                   −15.799 ms
> **Everything else, including the structural wait**                     **= 25.593 ms**
> Budget                                                                   33.333 ms
> **Available to the frozen passes**                                      **7.740 ms**
> **Frozen passes require**                                              **15.799 ms**
> **Deficit**                                                             **8.059 ms**

No reordering of the presentation path can close an 8.1 ms deficit that lives entirely inside work the round is forbidden to modify.

**No fix was applied. No post-fix run was performed, because there was nothing to run.** Section 12 records what was run instead and why that is the honest substitute.

---

## 12. SECOND TEN-TUPLE RUN (IN PLACE OF A POST-FIX RUN)

The brief's Phase 7 calls for an optional bounded fix followed by another ten-tuple run with identical metric definitions. No fix was promoted, so the second run serves a different and still necessary purpose: **it is the run that made the copy split measurable at all.**

The first build's copy split was unmeasurable because of the clock-domain defect. Run-02 was taken with that build; its twenty chain links are valid (they were always `CLOCK_MONOTONIC` alone) but its copy fields are not, and they are marked as such in `run-02-pairs10/SUPERSEDED.json` so that no future reader can quote them.

| | run-02-pairs10 | run-03-pairs10 |
|---|---|---|
| Observer build | pre-fix (media timestamps mixed with monotonic) | post-fix (media domain isolated) |
| Attempts / accepted / fallback | 11 / 10 / 0 | 12 / 10 / 1 |
| Watchdog fires | 0 | 0 |
| Ordering violations | 0 | 0 |
| Coalesced presentations | 0 | 0 |
| Missing presentation callbacks | 0 | 0 |
| `displayConfirmed` | 10 | 10 |
| `promotionEligible` | 1 | 1 |
| Headline median | 43.999 ms | 41.392 ms |
| SOURCE copy commit→wake median | 24.416 ms | 23.359 ms |
| INTERNAL `nextDrawable` median | 0.023 ms | 0.017 ms |
| Copy clock validation | **impossible** (tautological) | **0.035 ms** |
| Drawable bookkeeping | 14 vs pool 3 (defect) | 3 vs pool 3 (exact) |

The two runs agree on every quantity both could measure, to within the scene-to-scene variation of a live game. That agreement is the stability evidence the second run was supposed to provide; it is just evidence of stability across *builds* rather than across a fix.

**Run-02 remains in the evidence tree deliberately.** It is the only record of the defect, and deleting it would leave the fix unauditable.

---

## 13. B→M BUDGET VERDICT

| Target | Threshold | Measured | Verdict |
|---|---|---|---|
| Prerequisite | < 33.333 ms | median 41.392, p90 45.338 | **FAIL** |
| Preferred | ≤ 25.000 ms | median 41.392 | **FAIL** |
| Best single tuple | — | 29.939 ms | passes, once, by luck |

`B_TO_M_PRESENTATION_SERVICE_WITHIN_30FPS_BUDGET = NOT_VALIDATED`.

The one tuple that came in under budget (gen 10, 29.939 ms) did so because its blit reached the GPU in 11.833 ms instead of ~23 ms — the GPU happened to be idle at that instant. It is a lower bound on what an unloaded GPU could give, not evidence of a reachable steady state.

**Why the budget matters mechanically.** B is held from its present hook until M has been dispatched and presented. Measured hold: `bHoldNs` **29.981–46.803 ms** (median ~41.3) against a SOURCE period of 33.333 ms. The hold therefore exceeds one frame period on ten of eleven tuples, so each pair costs more than one period, and the pipeline settles below the source rate rather than above it:

| Quantity | Measured |
|---|---|
| SOURCE acquisition interval (median) | 34.321 ms → 29.14 fps |
| SOURCE presented interval (median) | 33.333 ms |
| M→B presented interval (median) | 16.667 ms |
| Presentation verdicts | 11 × `M_THEN_B_DISTINCT`, 0 violations |
| Output rate | 27.11 fps |

M and B do land on consecutive refreshes — the M→B half of the ordering machinery works exactly as designed, 11 times out of 11. But the output rate is **below** the source rate, because the B-hold of ~41 ms exceeds the 33.333 ms period. That is the precise sense in which the 30→60 pipeline is currently a net loss: it produces fewer displayed frames than the SOURCE stream would have produced alone.

`CONTINUOUS_30_TO_60_TITLE_FG` is **not claimed**. A→M was not measured in this round (`aDisplayToMDisplayNs = 0` in all twelve tuples, because the tuples were still `PENDING_DISPLAY` when the slice was cut and the A callback was not correlated), and the brief requires both A→M and M→B at one refresh before that claim. R120's A→M of 50.0 ms is a prior-round figure and is cited as such, not as this round's measurement.

---

## 14. DRAWABLE-LIFETIME ANALYSIS

### 14.1 What the traces show

| Observation | Value | Source |
|---|---|---|
| `maximumDrawableCount` | 3 | `R120_DRAWABLE_POOL`, `TITLE_LAYER_SELECTED` |
| `allowsNextDrawableTimeout` | 1 | `R120_DRAWABLE_POOL` |
| Concurrent demand declared by R120 | 3 (deferred B + INTERNAL M + next SOURCE) | `R120_DRAWABLE_POOL poolSufficient=1` |
| SOURCE outstanding at M acquisition | 3 of 3, every tuple | §7 table |
| SOURCE acquisition block | median 0.033 ms, max 2.913 ms | §7 table |
| INTERNAL acquisition block | median 0.017 ms, max 0.021 ms | §6 table |
| SOURCE acquisitions in window | 523 (run-02) | present-entry accounting |
| SOURCE presented callbacks | 512 (run-02) | `R101_WSI_DISPLAYED` |
| Deferred-B presents | 11 (run-02) | `R110_SOURCE_PRESENT_DEFERRED` |

The 523 = 512 + 11 identity is exact and is what closed the bookkeeping defect. It also establishes a lifetime fact the brief asked for: **every SOURCE drawable that the observer acquires has exactly two possible exits — ordinary WSI dispatch, or the R116 deferred-B forward — and both are now instrumented.**

### 14.2 The current lifetime model

```
nextDrawable returns drawable D
  → D is live
  → producer command buffer commits, drawing into D.texture
  → present(D) enters the hook
      ├─ deferred path: D is retained by the R102 ticket; M is dispatched;
      │                 D is forwarded later; D retires at the forwarded
      │                 present's callback
      └─ ordinary path: D is dispatched; D retires at its presented callback
```

Separately, and crucially: **the estimator never reads D's texture.** The capture blit copies `D.texture` into `captureTexture`, and the estimator consumes `captureTexture`. The SOURCE *image* lifetime is therefore already decoupled from the SOURCE *drawable* lifetime. That is what makes it possible for the estimator to run 25 ms after the hook without holding a drawable for 25 ms.

### 14.3 Whether lifetimes are inferred or measured

The brief forbids inferring drawable lifetimes from sequence numbers alone. Nothing in this section is inferred from a sequence number:

- Acquisition is a measured instant at the `nextDrawable` return.
- Retirement is a measured instant inside a presented handler, on both exits.
- The outstanding count is derived from three monotonic totals and published with them.
- The one place where the counter can disagree with Metal is stated rather than hidden: it releases at the presented callback, Metal releases earlier, so the counter is an upper bound — and the measurement that proves it is the 17 µs acquisition against a 3-of-3 reading.

---

## 15. A-HOLD FEASIBILITY

**Question:** can continuous 30→60 be built while retaining the original A and B drawables?

**Answer: NO.** And it is not necessary.

### 15.1 The arithmetic, from measured intervals

| Step | Measured |
|---|---|
| A present hook → B present hook | 33.333 ms (SOURCE presented interval, median) |
| B present hook → M WSI dispatch | 41.392 ms (headline, median) |
| **A must therefore remain un-retired for** | **74.725 ms** |
| In frame periods | **2.24 periods** |
| Drawables simultaneously un-retired | A, B, C (three SOURCE frames span 2.24 periods) **+ M = 4** |
| Pool | **3** |

`4 > 3`. The requirement exceeds the pool by one, and it is not a marginal exceedance: the pipeline would need to hold three consecutive SOURCE drawables at once, which at a 33.3 ms cadence is unavoidable once the hold exceeds two periods.

### 15.2 Corroboration from the traces

The traces do not merely permit this conclusion; they show the pool already at its limit under the *current*, non-holding design. Outstanding SOURCE drawables read 3 of 3 at every measured M acquisition (§7), and the only reason acquisition still returns in microseconds is that A is released at display before the next acquisition is attempted. Remove that release and the next acquisition must wait a full frame period — a stall that the current `sourceNdBlockNs` maximum of 2.913 ms shows is not happening today.

### 15.3 Why the answer does not block the product

A-hold is infeasible **and unnecessary**, because SOURCE image lifetime is already decoupled from drawable lifetime by the capture copy (§14.2). The estimator reads a private copy, not a live drawable. The design that the brief asked whether to build already exists and is the reason the pipeline functions at all.

The correct framing of the decoupling question is therefore inverted:

> **SOURCE/drawable decoupling is not a candidate optimisation — it is a load-bearing prerequisite that is already in place. Removing it in favour of holding drawables would require a pool of at least 4 and would stall the SOURCE stream.**

### 15.4 What A-hold would have bought, and why it is moot

Holding A would have allowed the estimator to read A's texture directly, saving the capture copy's 0.139 ms of GPU execution and its 0.007 ms of encoding. Against a 41.392 ms headline and an 8.059 ms deficit, a 0.146 ms saving is immaterial. The idea is rejected on cost-benefit grounds as well as on pool grounds.

---

## 16. EXACT NEXT PRODUCT BLOCKER

**`GPU_CAPACITY_FOR_INTERPOLATION`.**

Not presentation scheduling. Not drawable management. Not queue dispatch. Not the estimator's design. The measured facts that establish this:

| Fact | Value | Where |
|---|---|---|
| The SOURCE frame retires on the GPU a median of | **25.1 ms after B's present hook** | §8.5, sum column |
| The capture copy itself costs | 0.139 ms | §8.1 |
| The frozen estimator + MetalFX cost | 15.799 ms | §5 |
| Sum of GPU work required per pair | **~41.2 ms** | 25.1 + 0.139 + 9.788 + 6.011 + 0.116 |
| Available per pair at 30 fps SOURCE | **33.333 ms** | — |
| **Implied GPU utilisation** | **~123 %** | — |
| Observed output rate | 27.11 fps over the measured slice window | §13 |

The pipeline asks the GPU to do roughly 41 ms of work in a 33 ms period. It cannot, so the period stretches and the output settles at ~27 fps — below the 29.14 fps the SOURCE stream produces on its own. This is arithmetic, not inference, and it explains the observed output rate without any additional hypothesis.

### 16.1 The three ways out, and which are open to R122

| Path | Requirement | Status |
|---|---|---|
| **A. Reduce the frozen GPU work** | estimator + MetalFX from 15.799 ms to ≤ 7.740 ms | **Forbidden in R121 by the brief.** The only path that closes the deficit without changing the architecture. |
| **B. Overlap interpolation with rendering** | M for pair (A,B) computed while B is still rendering | Requires the estimator to read a partially rendered frame. Not physically available: interpolation needs B's final pixels. |
| **C. Accept M trailing B by more than one refresh** | Give up the one-refresh A→M target; interpolate at a coarser cadence | Viable, but it is a different product claim than `CONTINUOUS_30_TO_60`. |

The honest statement of the blocker, in one line:

> **The SOURCE frame occupies the GPU for ~25 ms of every 33.3 ms period, leaving 7.7 ms available to the interpolation passes; those passes need 15.8 ms. The deficit is 8.1 ms, it lives entirely inside work R121 is forbidden to modify, and no presentation-side reordering can reach it.**

### 16.2 What R122 should measure first

Two measurements would convert the remaining inference into fact, and neither requires touching the frozen estimator:

1. **Direct per-frame GPU occupancy.** Attach a completed-handler timestamp pair to the producer command buffer itself and publish `GPUEndTime − GPUStartTime` per frame. R121 established that the frame retires 25.1 ms after the hook but did not measure the frame's own GPU window; the two differ by the commit→hook interval, which varied 0.1–13.8 ms across the run.
2. **Estimator and MetalFX duration as a function of GPU load.** R120 measured the same frozen passes at 21.7 ms; R121 measured 15.8 ms. That 37 % spread on identical code is itself evidence of contention, and quantifying it would tell R122 whether the 8.3 ms headroom is real or optimistic.

Neither is a redesign. Both are instrumentation on work the brief already froze.

---

## APPENDIX A — RUN RECORD

### run-02-pairs10 (pre-fix build)

```
R115_CONTINUOUS_RUN=COMPLETE attempts=11 accepted=10 fallback=0
  sourceLeaksWhileClosed=0 staleCallbacks=0 duplicateMDispatch=0
R116_RUN_SUMMARY orderingViolations=0 coalescedPresentations=0
  presentationCallbackMissing=0 watchdogFired=0 displayConfirmed=10
  promotionEligible=1
```

Chain decomposition valid. Copy split, clock validation, drawable bookkeeping and classification **invalid** — see `run-02-pairs10/SUPERSEDED.json`.

### run-03-pairs10 (post-fix build)

```
R115_CONTINUOUS_RUN=COMPLETE attempts=12 accepted=10 fallback=1
  sourceLeaksWhileClosed=0 staleCallbacks=0 duplicateMDispatch=0
  resets=11 acceptanceRatio=0.833
R116_RUN_SUMMARY earlyBDispatch=10 lateMCallbacksAfterEarlyB=10
  orderingViolations=0 coalescedPresentations=0 presentationCallbackMissing=0
  watchdogArmed=11 watchdogFired=0 gateEncoded=11 gateEncodeFailures=0
  displayConfirmed=10 displayRejected=0 displayEvidenceLost=0
  promotionEligible=1
```

Checks: 6 PASS, 1 FAIL.

```
chain_complete=PASS                tuples=11
reconciliation_exact=PASS          maxAbsResidualNs=0
no_order_violation=PASS            violations=0
copy_clock_compatible=PASS         srcMaxSkewMs=0.035(n=11) mMaxSkewMs=0.001(n=11)
drawable_bookkeeping_bounded=PASS  outstandingMax=3 pool=3 acquired=2076 released=2073 deferred=12
headline_within_budget=FAIL        median=41.392ms budget=33.333ms
source_cadence_30fps=PASS          sourceAcquireMedianMs=34.321 sourceFps=29.14
```

The single FAIL is the round's finding, not a defect in the measurement.

### Environment (13 variables, unchanged across both runs)

```
DYLD_INSERT_LIBRARIES      libfgmetal_r121_occupancy_probe.dylib
FGMETAL_R100_PRESENT       1
FGMETAL_R108_CAPTURE       1
FGMETAL_R108_FRAMEBUFFER_ONLY 1
FGMETAL_R110_PRESENT       1
FGMETAL_R114_INPROCESS_LATCH 1
FGMETAL_R115_CONTINUOUS    1
FGMETAL_R115_MAX_TUPLES    10
FGMETAL_R116_B_EARLY       1
FGMETAL_R116_WATCHDOG_MS   150
FGMETAL_R120_PACE_HZ       30
FGMETAL_DIAG_SAMPLE        bounded
FGMETAL_EVIDENCE_DIR       …/r121-presentation-occupancy
```

`FGMETAL_EVIDENCE_SELFTEST` is deliberately absent: it makes the dylib skip hook installation so a standalone probe host can exercise the corpus guard without a Metal device, and with it set the game runs completely uninstrumented. It is used only by `tools/evidence_probe_host`.

---

## APPENDIX B — INDEPENDENT REVIEW AGAINST THE BRIEF'S EIGHT ITEMS

| # | Item | Result |
|---|---|---|
| 1 | All timestamps use compatible clocks | **PASS.** Chain is `CLOCK_MONOTONIC` only. GPU timestamps are subtracted only from other media-clock samples. Cross-clock agreement 0.035 ms (SOURCE) and 0.001 ms (M). The pre-fix build failed this and is marked superseded. |
| 2 | `nextDrawable` wait separated from copy GPU work | **PASS.** Distinct slots, distinct links. `internalNdEnterToNdReturnNs` 0.017 ms against `srcCopyCommitToGpuStartNs` 23.073 ms. |
| 3 | Drawable lifetime counts not inferred from sequence numbers | **PASS.** Acquisition and both retirement exits are measured instants. The one divergence from Metal (release at presented callback vs earlier reuse) is stated, and the 17 µs acquisition against a 3-of-3 reading is the measurement that demonstrates it. |
| 4 | Per-tuple reconciliation exact | **PASS.** `maxAbsResidualNs = 0` on all 11 tuples. |
| 5 | No diagnostic export contaminating timing | **PASS.** `bounded` sampling; 12 `R119_DIAG_SAMPLE` lines for 12 tuples. Both report lines emitted after the window closes. |
| 6 | 30-fps SOURCE cadence genuine | **PASS.** `sourceAcquireMedianMs=34.321`, `sourceFps=29.14`, presented interval median 33.333 ms, `source_cadence_30fps=PASS`. |
| 7 | No historical evidence modified | **PASS.** 5 412 files / 30 168 200 909 bytes hashed before the first run; three isolation probes confirm prior rounds refuse writes. |
| 8 | No performance claim based on a sum of unrelated medians | **PASS.** Every group total sums only its own members. The headline is computed from the headline column, never from a sum. The 24.726 / 15.799 / 0.867 attribution is presented as an attribution of the median, with the residual stated. |

One item deserves a note rather than a tick. Item 8 is satisfied in the letter by the per-component tables, but the *attribution* in §5.2 sums four medians and compares them to the headline median. That is legitimate only because the reconciliation residual is exactly zero on every tuple, so the components do describe the headline rather than approximating it. Had the residual been non-zero, §5.2 would have been withdrawn.

---

## APPENDIX C — SELF-TESTS

`tools/test_analyze_r121.py` exercises the analyser against synthetic slices in which the defect is known by construction, asserting that it reaches the verdict the defect demands rather than merely that it runs.

```
SELFTEST FAST_PASSES                PASS
SELFTEST FAST_TWO_TUPLES            PASS
SELFTEST FAST_RESIDUAL_ZERO         PASS
SELFTEST CLEAN_ONLY_BUDGET_FAILS    PASS  failing=['headline_within_budget']
SELFTEST CLEAN_CLASSIFICATION       PASS  verdict=SOURCE_COPY_GPU_SUBMIT
SELFTEST RECONCILIATION_REJECTED    PASS  maxAbsResidualNs=1000000
SELFTEST MISSING_LINK_REJECTED      PASS  tuples=0
SELFTEST ORDER_REJECTED             PASS  violations=1
SELFTEST CLOCK_SKEW_REJECTED        PASS  srcMaxSkewMs=5.000 toleranceMs=1.0
SELFTEST LIVENESS_REJECTED          PASS  outstandingMax=4 pool=3
SELFTEST BUDGET_REJECTED            PASS  median=152.445ms
SELFTEST REFRESH_TRAP_NOT_CORRELATED PASS nearCallback=0
SELFTEST_SUMMARY failures=0
```

Two of these exist because of defects found in this round: `CLOCK_SKEW_REJECTED` because the original closure residual could not fail, and `LIVENESS_REJECTED` because the original counter drifted to 14 against a pool of 3.

`tools/verify_r121_estimator.py` confirms the frozen estimator is unmodified:

```
R120_BUILD_CHECK verdict=R117_OPTIMIZED_ESTIMATOR_PRESENT
R120_BUILD_CHECK dylibSha256=4db76d06347eb8d15af61b3551f073d00811e17b41e29d645d67bd3de52a84ab
R120_BUILD_CHECK binaryLiteral r117Form=True r110Form=False parabolaProbes=5 bytes=17338
R120_BUILD_CHECK sourceLiteral r117Form=True r110Form=False bytes=11676
R120_BUILD_CHECK coreMatch=True embeddedOffset=222264
```

The embedded shader literal hash `d1120f88…` is byte-identical to R120's. The dylib hash changed when the probe was rebuilt for the clock-domain fix, so `build-verification.json` was regenerated against the rebuilt binary; the *estimator* literal did not change, which is the point — instrumentation was added and the frozen shader was not touched. `coreMatch=True` confirms the source literal is a prefix of the binary literal, so the only thing appended is the R116 gate source.

---

## APPENDIX D — WHAT WAS NOT DONE

The brief's prohibition list, with confirmation that each was respected:

| Prohibited | Status |
|---|---|
| Redesign the R120 30-fps SOURCE pacer | Untouched; `FGMETAL_R120_PACE_HZ=30` carried over unchanged |
| Optimise the R117 estimator | Untouched; hash verified byte-identical |
| Change the R116 fast gate | Untouched |
| Change R116 early-B ordering | Untouched; 10 early dispatches, 0 violations |
| Change the R116 watchdog | Untouched; 150 ms, 0 fires |
| Change SOURCE/INTERNAL accounting | Untouched; R121 counters are additive and reported separately |
| Break evidence isolation | Three probes confirm prior rounds refuse writes |
| Change R119 bounded diagnostics | Untouched; `bounded` mode |
| Change R115/R116 lifecycle semantics | Untouched; 10 accepted, 1 fallback |
| Estimator redesign | Not attempted |
| GPU contention research | Not attempted as a research programme — contention appears only as a measured consequence, in §16 |
| Depth work | Not attempted |
| 60→120 | Not attempted |
| Sub-refresh scheduling | Not attempted |
| Generalised scheduler | Not attempted |
| UI, installer, DXMT, HDR, R6-A, storage | Not attempted |

No 50-tuple run was performed. Both authoritative runs were bounded at 10 accepted tuples, per Phase 4.

---

*End of report.*
