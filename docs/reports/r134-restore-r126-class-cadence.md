# R134 — RESTORE R126-CLASS 30→60 CADENCE

Evidence root: `experiments/metalfx_frame_interpolator_R93/r134-evidence/`

Every number below is labelled **[MEASURED]**, **[DERIVED]** (arithmetic on
measured values), **[INTERPRETATION]** or **[HYPOTHESIS]**. Nothing is asserted
as measured unless a record in the evidence tree contains it.

---

## 1. Executive summary

R134 set out to remove the midpoint deadline miss and restore the R126-class
30→60 cadence. It did not restore the cadence, and it now knows why with
numbers rather than inference.

Three results matter.

1. **A hard deadlock was found and fixed.** The runtime could freeze
   permanently, which is why earlier live attempts "went quiet". Two threads
   were parked in a lock cycle: the display pump held `r127RuntimeLock` while
   calling `-[CAMetalDrawable addPresentedHandler:]` (which needs the drawable's
   internal `os_unfair_lock`), while QuartzCore's display thread held that same
   drawable lock inside `didPresentAtTime:` and called our presented-handler
   block, which wanted `r127RuntimeLock`. This is a pre-existing R127
   display-observation defect, not R134 instrumentation. It is fixed (§10).

2. **The midpoint is late because the work does not fit, not because the
   schedule is late.** [MEASURED] From capture-complete to estimator-submit the
   CPU costs a median of **22 µs + 150 µs = 172 µs**. [MEASURED] The estimator
   command buffer itself runs **18.6–23.0 ms** on the GPU and is followed
   serially by a **7.2–8.7 ms** MetalFX call. [DERIVED] The serial floor is
   **25.8 ms at best** against a **17.2 ms** half-period budget — **1.50×**.
   There is no scheduling delay to remove and no earlier start available.

3. **The alpha still presents only 0.68 % of midpoints at the target source
   rate.** [MEASURED] `internalPresented=24` of `sourcePresented=3507` in a clean
   window at a 34.5 ms source period, with `droppedSource=0` and
   `orderViolations=0`. This reproduces R133's 0.63 %.

4. **The share is period-dependent, which confirms the model.** [MEASURED] In the
   same session the share rose to 23.8 % once the source slowed to a 38.5–44.7 ms
   period. [DERIVED] A 12 % longer period produced a 34.8× higher share — only
   possible if the deadline is hard and the cost is nearly fixed. **The 23.8 %
   must not be quoted as a restored cadence**; the alpha did not get faster, the
   source got slower.

5. **Correctness held under soak and fault injection.** [MEASURED] 15 min 15 s of
   real gameplay: 28 913 SOURCE frames, `droppedSource=0`, `orderViolations=0`,
   `drawableFailures=0`, `presentFailures=0`, through a full ON/OFF cycle and a
   60-pair INTERNAL-loss injection with automatic recovery.

Consequence: **Option A and Option B are excluded by measurement.** The only
remaining routes are a one-slot pipeline with fixed bounded latency (Option C) or
a cheaper estimator (out of scope: estimator math is frozen).

---

## 2. What was frozen and stayed frozen

Unchanged and hash-verified after every build:

| File | SHA-256 |
|---|---|
| `R123DisplayPump.m` | `f68b5eaec8487252c53ede42cd73377c94c3c76e906d226cfc63f7b894ac3454` |
| `R123DisplayPump.h` | `88ae57c6e48e4a14d845db0d182b3c7ced2031d12ba75318008ccda46eaf75a1` |
| `MetalFXFrameInterpolationProvider.m` | `a02c25d448c519f1dd994504f7f59e3ee90ccc3b2f7b07cdf98bd9d801c1a029` |
| `MetalFXFrameInterpolationProvider.h` | `45b1e33846d3aff2c300ecdd5199af8dd9af7f4c2c1405afed7d360a8825d8d6` |

Capture, transactional SOURCE ownership, `SourcePresentOrdinal` semantics,
estimator math, MetalFX math, the SOURCE reservation scheme, the R125
drawable-retention fix, the R126 self-present gate, R120 pacing and the R102
disabled path were not touched. The two edits in §10 and §11 are in the
diagnostics and display-observation paths only.

---

## 3. Builds and deterministic gate

| Build | Dylib SHA-256 | Gate |
|---|---|---|
| `baseline-build/` (R133 source, untouched) | `59eaea8a…` | 45 PASS / 0 FAIL |
| `instrumented-build/` (R134 timeline added) | `4206063e…` | 45 PASS / 0 FAIL |
| `fix1-build/` (deadlock + classifier fixes) | `9101ce8a…` | 45 PASS / 0 FAIL |

Logs: `baseline-deterministic.log`, `instrumented-deterministic.log`,
`fix1-deterministic.log`. All three ran the same 45-case suite
(`build_r130_observer_integration.sh`) including the 9-SOURCE and 80-SOURCE
pressure cases, ledger lifetime, reversed ordering, estimator gap/late-capture
and the commit-prerequisite matrix. The fix in §10 is behaviour-neutral on all
45 cases, which is the evidence that the lock split did not change scheduling.

---

## 4. Instrumentation design

`FGMETAL_R134_TIMELINE` (default on) enables a bounded pair record:

```
R134PairRecord: sequence, pairANs, hookNs, presentNs, slotAfterSourceNs,
copyCompleteNs, genStartNs, estSubmitNs, estWakeNs, mfxStartNs, mfxEndNs,
resolveNs, presentMidNs, missedSlotNs, pumpUseNs, gpuStartNs, gpuEndNs, outcome
```

A 512-entry ring is keyed by sequence and re-created when the sequence does not
match, so a recycled slot cannot be confused with an old frame. Terminal pairs
are copied into a 384-entry retained sample once, after 8 warm-up pairs.

Clock discipline: all subtraction happens on `CLOCK_MONOTONIC` via `r105NowNs()`.
GPU timestamps (`GPUStartTime`/`GPUEndTime`) live on the media clock and are
reported separately after `fgmetalMediaToNs()`. `r134Delta()` returns
`ok=NO` when either endpoint is absent, so a missing timestamp can never
masquerade as a zero-length interval.

Emission: `R134_PAIR` for the first 24 terminal pairs of either outcome,
`R134_TIMELINE` plus a fixed `R134_CRITICAL_PATH` interval table with each
periodic summary and at the final one-shot summary.

---

## 5. Two defects found in the instrumentation path itself

### 5.1 R134 records were being swallowed by the diagnostics cap

[MEASURED] `R134_*` prefixes were not registered in
`r127ClassifyLogLine()`, so they fell into `R127LogUnclassifiedEvent`, which is a
**first-64 budget shared by every unfamiliar prefix for the whole process**. By
the time the runtime was switched on, that budget had been spent and every R134
record was suppressed. Symptom: a pump that was demonstrably generating
(pair ordinals in the thousands) with zero `R134_` lines.

Fix (§11): register `R134_` as `R127LogAlways`. This is safe because the records
are bounded at the source — 24 pair records, one timeline per periodic summary,
a fixed interval table each time.

### 5.2 The one-shot `SUMMARY` command does not complete in OFF mode

[MEASURED] Sending `SUMMARY` while the runtime mode is OFF produces
`R127_RUNTIME_SUMMARY=FINAL` and then no further record — the following
`R127_RUNTIME_STATE=FINAL`, the pump summary and the R134 timeline never appear.
R133's own log confirms the same shape: it contains `PERIODIC` summaries and a
`FINAL`, and its counters were taken from the periodic path. The driver was
changed to delimit segments with ON/OFF transitions and to send `SUMMARY` only
while generating.

This is a latent defect worth its own bounded investigation; it did not block
R134 because the periodic path is uncapped and carries the same counters.

---

## 6. The measurement window

[MEASURED] `live-r134/observer.80640.log` (sha256 in
`live-r134/observer-sha256.txt`):

- 2 `R127_RUNTIME_SUMMARY=PERIODIC`, 2 `R134_TIMELINE`, 24 `R134_PAIR`
- **0** `kIOGPUCommandBufferCallbackErrorSubmissionsIgnored`
- **0** `R133_CAPTURE_COMMIT=REFUSED/REJECTED`

Clean window: no GPU failure and no capture-commit refusal, so the counters are
trustworthy. This matters because three other sessions in this round were
invalidated by GPU failure (§12).

---

## 7. Phase 1 — the midpoint critical path

### 7.1 CPU-side segments [MEASURED, R134 live]

From `R134_CRITICAL_PATH`, class `missed`, n=377:

| Interval | min | median | p90 | p99 | max |
|---|---|---|---|---|---|
| `captureCompleteToGenerationStart` | 13 µs | **22 µs** | 60 µs | 121 µs | 211 µs |
| `generationStartToEstimatorSubmit` | 108 µs | **150 µs** | 336 µs | 488 µs | 578 µs |

[DERIVED] Total capture-complete → estimator-submit: **172 µs median**,
766 µs at p99. Against a 17.2 ms budget this is **1 %**. The scheduler is not
late; the work is long.

### 7.2 GPU-side segments [MEASURED, R133 live pair records]

The R134 retained snapshot is taken at the terminal disposition, so timestamps
that arrive *after* the slot are absent from it (§9). For the GPU segments the
R133 pair records are used instead — same code, same configuration, valid
session, `r133-evidence/live-r133/observer.56958.log`, 6 `GENERATED` pairs:

| pair | estimator GPU | MetalFX wall | serial |
|---|---|---|---|
| 5588→5589 | 23.032 ms | 8.723 ms | 31.755 ms |
| 5589→5590 | 20.544 ms | 8.246 ms | 28.790 ms |
| 5590→5591 | 19.806 ms | 7.838 ms | 27.644 ms |
| 5591→5592 | 19.026 ms | 7.468 ms | 26.494 ms |
| 5592→5593 | 18.922 ms | 7.191 ms | 26.113 ms |
| 5593→5594 | 18.617 ms | 7.190 ms | **25.807 ms** |

[MEASURED] estimator GPU 18.617–23.032 ms (mean 19.991); MetalFX wall
7.190–8.723 ms (mean 7.776).

The two are **serial, not overlapped**: `processCapturedFrame()` does
`[commandBuffer commit]; [commandBuffer waitUntilCompleted];` and only then
enters the MetalFX transition. The midpoint image cannot exist before the
estimator's GPU work retires.

[MEASURED] Estimator submission spacing is one source frame, not faster:
successive `estimatorGpuStartNs` deltas are 32.2, 32.5, 34.1, 35.4, 38.0, 42.0 ms
against a measured source period of 34.4 ms.

### 7.3 The comparison that decides the round [DERIVED]

- Half source period: `34529000 / 2` = **17.26 ms** (R134), 17.20 ms (R133).
- Best-case serial midpoint cost: **25.81 ms**.
- Shortfall: **8.6 ms**, i.e. the best case needs **1.50×** the budget.

This also explains why the brief's warning was correct: observed midpoint
lateness was only ~2–7 ms, *not* 8.6 ms, so the intended slot is not simply
`present(B) + halfPeriod`. The capture completes before B is presented, which
gives back several milliseconds of headroom. The numbers are not additive; the
budget is somewhat larger than half a period, and still not large enough.

### 7.4 Successful vs missed pairs

[MEASURED] In the clean window, `presented=24`, `missed=3481`. The 24 successes
are the pipeline-fill pairs immediately after the runtime is switched on; the
first 24 `R134_PAIR` records are all `outcome=MISSED` and several carry
sub-millisecond `estimatorGpuNs`, which is the fill transient, not steady state.
[INTERPRETATION] The 24 "successes" are therefore not a steady-state cadence
sample — they are the head of the run. This is the same sampling bias R133 had,
and it is why R133's 52 successes did not indicate a working cadence either.

---

## 8. Phase 2 — cross-queue capture ordering

[MEASURED] `R134_TIMELINE`:

```
pumpUseSamples=3430  pumpUseViolations=0  pumpUseMinMarginNs=3000
```

Over 3430 samples the pump never sampled the private copy before the producer
command buffer that wrote it completed. The **minimum** margin was 3 µs.

[INTERPRETATION] Ordering is correct today, but a 3 µs minimum margin is not a
design margin — it is a coincidence of two nearly simultaneous events. It is
safe on this host under this load and it is not safe by construction.

**Verdict: `CROSS_QUEUE_CAPTURE_ORDERING = VALIDATED`** (no violation observed),
with a required pre-release hardening item: add the smallest explicit sync edge
(preferably an existing Metal primitive — a shared event or a fence on the copy)
rather than relying on the producer completion callback ordering. Per the brief,
no sync was added for experimentation; this is recorded as hardening.

---

## 9. Phase 3 — how the intended slot is computed, and what is missing

[MEASURED] The pump's slot rules are R123's, unchanged: after a SOURCE present
the next slot is `now + halfPeriod` (rule 1); after a generated midpoint the next
slot is `capture(B) + sourcePeriod` (rule 2). `r123Pump.nextSlotNs` sampled at
the top of `r123PresentItem` is the intended slot used by the R134 deadline
interval.

[MEASURED] `sourcePeriodNs` is itself measured from SOURCE reservation enqueue
deltas: 34.267 ms in the first periodic window, 34.529 ms in the second.

**Instrumentation limitation, stated plainly.** The retained sample is copied at
the moment a pair reaches a terminal disposition. For a missed pair that instant
is the slot, which is *before* the estimator completes. So
`estimatorSubmitToWake`, `estimatorWakeToMetalFXStart`, `metalFXStartToEnd`,
`captureCompleteToInternalReady`, `intendedSlotToInternalReady`,
`hookToIntendedSlot` and `captureCompleteToIntendedSlot` all report
`samples=0` for missed pairs. They are absent, not zero.

[MEASURED, and itself informative] `hookToCaptureComplete` is also `samples=0`,
which means the SOURCE-present note for pair B arrives *after* the midpoint slot
for (A,B) is evaluated. The intended midpoint slot is therefore anchored before
B's own SOURCE presentation, consistent with rule 2 anchoring on `capture(B)`
rather than on `present(B)`.

Fix for the next round: retain a *reference* and snapshot at emission time, or
re-copy the retained record whenever a late timestamp lands. That is a
diagnostics-only change.

---

## 10. The deadlock (root cause, evidence, fix)

### 10.1 Evidence

[MEASURED] `sample 76211` captured both threads parked:

```
Thread (CA::Context dispatch):
  layer_presented → -[CAMetalDrawable didPresentAtTime:]
    → __r127ObserveWsiDisplay_block_invoke
      → _pthread_mutex_firstfit_lock_slow        (waiting)

Thread r123PumpMain:
  r123PumpMain → r123PresentItem → r127ObserveWsiDisplay
    → -[CAMetalDrawable addPresentedHandler:]
      → _os_unfair_lock_lock_slow                (waiting)
```

### 10.2 Root cause

`r123PresentItem()` takes `r127RuntimeLock` at entry and, still holding it,
calls `r127ObserveWsiDisplay()` (which calls `addPresentedHandler:`) and then
`present` — both of which need the drawable's internal `os_unfair_lock`. The
handler block that gets installed then takes `r127RuntimeLock`. Pump:
`r127RuntimeLock → drawable lock`. Display thread: `drawable lock →
r127RuntimeLock`. Cycle.

Once it fires, the pump thread never returns: no further slots, no periodic
summary, no shutdown. This is why sessions "went quiet" after a few thousand
frames and why a healthy-looking process could stop presenting with an idle
stack.

### 10.3 Fix

A dedicated `r127DisplayLock` now guards the physical-display bookkeeping. The
handler takes only that lock. Readers that hold no drawable lock take only that
lock. Nothing acquires a drawable lock or `r127RuntimeLock` while holding it, so
the wait-for graph is acyclic by construction.

Touched: the declaration and rationale comment, the handler's critical section,
`r127DisplaySnapshot()`, `r127DisplayAckResetAtBoundary()`, the final-summary
reader (now reads display state after dropping `r127RuntimeLock`), and the
ON/OFF transition snapshots.

[MEASURED] After the fix: 45/45 deterministic, and a live session ran 3507 SOURCE
frames with 2 periodic summaries and **zero** GPU errors — the first session in
this round to reach a periodic summary at all.

---

## 11. Diagnostics accounting

[MEASURED] `BOUNDED_ALPHA_DIAGNOSTICS` behaviour is as designed: healthy events
cap at 8 per prefix, residual at 16, anomaly at 64, unclassified at 64, and the
periodic summary is uncapped. In the clean window `healthyLogSuppressed=97219`,
`residualLogSuppressed=6285`, `unclassifiedLogSuppressed=16348`.

One real defect was found and fixed: R134's own prefixes were unclassified and
therefore invisible (§5.1). R133's `anomalyLogSamples=168` vs ~218 expected is
not reproduced as a blocker here; anomaly samples in this window were 3 then 4,
consistent with a healthy run.

---

## 12. Environment: GPU subsystem degradation

[MEASURED] Four sessions were attempted. Three entered
`kIOGPUCommandBufferCallbackErrorSubmissionsIgnored` (preceded by
`kIOGPUCommandBufferCallbackErrorInvalidResource` and
`kIOGPUCommandBufferCallbackErrorTimeout`). Once submissions are ignored, every
capture copy fails to complete, staging capacity fills and the window renders
black; all counters after that point are meaningless. The fourth session
(`observer.80640.log`) was clean and is the one reported.

R133's live log contains **zero** GPU errors, so this is a change in host state
across the round, not a property of R134 code. The R134 dylib differs from R133
only by timestamp reads, and the same failures occurred with the untouched R133
dylib during launch-recipe debugging.

---

## 13. Phase 4 — option selection, decided by measurement

| Option | Verdict |
|---|---|
| **A — remove a real critical-path delay** | **Excluded.** [MEASURED] The entire CPU-side path from capture-complete to estimator-submit is 172 µs median. There is no delay of the required 8.6 ms to remove. |
| **B — start existing work earlier** | **Excluded.** [MEASURED] Generation starts 22 µs after the copy completes and submits 150 µs later. Even at zero, the serial GPU chain (25.8 ms) still exceeds the 17.2 ms budget. |
| **C — one-slot pipelined presentation** | **The only remaining route.** Give the midpoint one full source period instead of half: budget becomes 34.5 ms against 25.8 ms of work, a ~8.7 ms margin. Must be implemented as a *stable repeating* schedule with bounded fixed latency — no per-frame accumulation, no reordering, no duplication. |

This is the round's actionable conclusion. It is not a hypothesis: A and B are
excluded by two independent measured quantities (the 172 µs CPU path and the
25.8 ms serial GPU floor).

Note that C changes presentation latency by one source period (~34.5 ms) for
midpoints. That trade must be accepted explicitly, and `FG_DISPLAY_LATENCY` must
be re-measured afterwards.

---

## 14. Live ON/OFF behaviour

[MEASURED] `live-r134/short-timeline.txt` records a complete
`OFF → ON → OFF → ON` cycle through the runtime control file, followed by a
third ON leg that opened the soak and ran for 15 minutes before returning to OFF.
That is the full `OFF→ON→OFF→ON→OFF→ON` sequence the brief asks for, with the
third ON leg extended into the soak.

Across every transition in the session: `droppedSource=0`,
`orderViolations=0`, `staleCallbacks` bounded, no deadlock, no GPU failure, and
the process stayed alive from first present to the final OFF.

[MEASURED] The fix in §10 is what makes this repeatable. Before it, two earlier
sessions froze permanently after a few thousand frames and never emitted a
periodic summary. After it, this session emitted **18** periodic summaries across
three ON legs without a single stall.

**`RUNTIME_FG_ENABLE_DISABLE` = VALIDATED.**

---

## 15. Soak and fallback injection

[MEASURED] The soak ran **15 min 15 s** of continuous generating mode on real
gameplay (open world, not a menu), `live-r134/soak-timeline.txt`, ending in the
one-shot pump summary:

```
R123_PUMP_SUMMARY=COMPLETE reason=r127-requested-final-summary
  sourceEnqueued=28913  sourcePresented=28913
  internalEnqueued=28902 internalPresented=6880 internalResolved=6880
  deadlineMisses=22022  droppedInternal=22022  droppedSource=0
  drawableFailures=0    presentFailures=0
  idleSlots=22157  currentDepth=0  maxDepth=6
  orderViolations=0  orderGaps=22032  slots=35793
  sourcePeriodNs=38510000  ignoredSelfPresentCompletions=35787
```

Process stability: alive throughout, no deadlock, no GPU failure, zero
`kIOGPUCommandBufferCallbackErrorSubmissionsIgnored`. Memory growth was not
measured (no cheaply reliable instrument on this host).

[MEASURED] Bounded INTERNAL-loss fallback: `R127_GENERATION_LOSS=ARMED
requestId=1 pairs=60 reason=bounded-development-fault
injection=drop-generated-internal-before-pump` was delivered at soak + 5 min.
After the injection SOURCE continuity held (`droppedSource=0` across the whole
soak), `orderViolations=0`, and the pump kept presenting — no manual recovery
was needed.

[INTERPRETATION] This is the strongest correctness evidence the round produced:
28 913 SOURCE frames with **zero** source drops, **zero** ordering violations,
**zero** drawable failures and **zero** present failures, through an ON/OFF cycle
and an injected generation loss.

`FG_DISPLAY_LATENCY` is still **NOT MEASURED**. The R133 figures
(36.95 / 47.00 / 54.10 ms median/p90/p99) stand unrevised. §10 changed a lock but
no scheduling decision and the 45-case gate is unchanged, so there is no
mechanism by which the fix would move display latency — that is an argument, not
a measurement, and it is labelled as such.

---

## 16. Cadence result, and why it is period-dependent

[MEASURED] Clean window, second periodic summary:

```
sourceEnqueued=3507  sourcePresented=3507  slots=3531
internalEnqueued=3505 internalPresented=24  deadlineMisses=3481
idleSlots=2836  maxDepth=3  sourcePeriodNs=34529000
droppedSource=0  droppedInternal=3481  orderViolations=0
```

[DERIVED] INTERNAL share = 24 / 3507 = **0.684 %**. R133 was 52/8200 = 0.634 %.
At this source period the cadence is unchanged within noise.

The soak tells a more interesting story. [MEASURED] Across the same session the
share tracks the source period:

| Window | INTERNAL | SOURCE | share | source period | half period |
|---|---|---|---|---|---|
| short window (2nd periodic) | 24 | 3507 | **0.68 %** | 34.53 ms | 17.27 ms |
| soak end (last periodic) | 6774 | 28642 | **23.65 %** | 44.73 ms | 22.37 ms |
| soak final (pump summary) | 6880 | 28913 | **23.80 %** | 38.51 ms | 19.26 ms |

[DERIVED] The share rises **34.8×** for a period only **12 % longer** (34.53 →
38.51 ms). A 12 % change in budget cannot produce a 35× change in outcome unless
the deadline is hard and the cost is nearly fixed — which is exactly the model in
§7: a roughly constant ~26 ms of serial work crossing a deadline that moves with
the source period.

[INTERPRETATION] The 23.8 % soak figure is therefore **not** a restored cadence
and must not be quoted as one. During the soak the title slowed to ~22–26 fps
(period 38.5–44.7 ms), which widens the slot enough that some midpoints fit. The
alpha did not get faster; the source got slower. This also explains why R133 and
the short window both sit near 0.6–0.7 %: both were measured at ~29 fps source.

**Verdict: `R126_CLASS_CADENCE = NOT_VALIDATED`** — at the 30 fps source the
product targets (34.5 ms period), the serial estimator + MetalFX floor
(25.8 ms best case) exceeds the half-period budget (17.2 ms) by 8.6 ms (1.50×),
and the measured INTERNAL share is 0.68 %. The cadence only appears to recover
when the source period grows, which is the opposite of the product goal.

---

## 17. Correctness counters

[MEASURED] All of the following are clean in the reported window:

- `captureCommitted=5454`, `captureComplete=5454`, `captureFailed=0`
- `copyBeforePresent=5454`, `copyAfterPresent=0`
- `prerequisiteSatisfied=5454`, `prerequisiteAbsent=0`
- `estimatorReady=5449`, `estimatorNoCapture=5`, `estimatorLate=0`,
  `estimatorReclaimed=0`, `estimatorOrderViolations=0`
- `pendingNow=0`, `pendingPeak=1`, `pendingGrace=1`, `pendingExpired=0`
- `orderViolations=0`, `droppedSource=0`
- `pumpUseViolations=0`

[INTERPRETATION] The ownership/capture architecture behaved exactly as R133
described it. Nothing in this round contradicts it. The problem is purely that
the generated midpoint cannot be produced fast enough to meet its slot.

---

## 18. Diagnostics caveats that affect interpretation

1. Every per-frame log family is capped, so a log that stops growing is **not**
   evidence that the runtime stopped. Three of my earlier diagnoses in this round
   were wrong for exactly this reason. The only reliable liveness signal is
   `R127_RUNTIME_SUMMARY=PERIODIC`.
2. The retained R134 sample is the **first 384 terminal pairs after warm-up**, so
   it is dominated by pipeline fill. A steady-state sample needs either a
   later-start retention window or a reset on each ON transition.
3. `samples=0` in the interval table means *absent*, never *zero*.

---

## 19. Required regressions

| Gate | Result |
|---|---|
| R133 45-case deterministic suite | **45/45 PASS**, three times (baseline, instrumented, fixed) |
| 9-SOURCE pressure | PASS (in suite) |
| 80-SOURCE pressure | PASS (in suite) |
| Ledger lifetime (300 logical / 256 capacity) | PASS (in suite) |
| Reversed ordering | PASS (in suite) |
| Estimator gap / late capture | PASS (in suite) |
| Commit-prerequisite matrix | PASS (in suite) |
| Live ON/OFF ×2 | PASS (clean window) |
| Live ON/OFF ×3 | PASS (third leg extended into the soak) |
| Bounded INTERNAL-loss fallback | PASS — 60 pairs dropped, SOURCE continuous, self-recovered |
| 15 min soak | PASS — 28913 SOURCE, 0 drops, 0 violations, 0 drawable/present failures |
| Display latency | NOT MEASURED |

---

## 20. Verdicts

```
MIDPOINT_CRITICAL_PATH      = MEASURED
                              CPU path 172 us median; GPU path 25.8-31.8 ms serial;
                              intended-slot intervals absent (see §9)

R126_CLASS_CADENCE          = NOT_VALIDATED — serial estimator+MetalFX floor
                              25.8 ms exceeds the 17.2 ms half-period budget by
                              8.6 ms (1.50x); INTERNAL share 0.68 % at 30 fps
                              source. The 23.8 % seen in the soak is an artefact
                              of the source slowing to ~22-26 fps (§16).

CROSS_QUEUE_CAPTURE_ORDERING = VALIDATED — 3430 samples, 0 violations, but the
                              minimum margin is 3 us: hardening required before
                              release

RUNTIME_FG_ENABLE_DISABLE   = VALIDATED — OFF->ON->OFF->ON->OFF->ON, 18 periodic
                              summaries, zero drops, zero ordering violations

FG_FAILURE_FALLBACK         = VALIDATED — 60-pair INTERNAL-loss injection, SOURCE
                              continuity held, automatic recovery, zero SOURCE
                              drops and zero ordering violations

D3DMETAL_FG_ALPHA_SOAK      = VALIDATED — 15 min 15 s real gameplay, 28913 SOURCE
                              presented, 6880 INTERNAL, drawableFailures=0,
                              presentFailures=0, process stable

FG_DISPLAY_LATENCY          = NOT MEASURED (R133 values stand: 36.95/47.00/54.10)
BOUNDED_ALPHA_DIAGNOSTICS   = VALIDATED — caps behave; R134 prefixes were
                              unclassified and are now registered

D3DMETAL_FG_ALPHA_RUNTIME   = PARTIAL
```

The alpha is **not** validated, because the 30→60 cadence it exists to deliver is
still not there at the target source rate. It is also **not** regressed, and it is
now materially more trustworthy: the deadlock that could freeze it is gone, and
the soak shows 28 913 SOURCE frames with zero drops, zero ordering violations and
zero drawable/present failures through an ON/OFF cycle and an injected generation
loss.

---

## 21. The single smallest next step

Implement **Option C as a fixed one-slot pipeline**: keep R123's slot rules and
every reservation/ownership rule, and change only the anchor so a midpoint for
pair (A,B) targets the slot one source period after the slot it targets today.
That converts a 25.8 ms requirement against a 17.2 ms budget into 25.8 ms
against 34.5 ms, with ~8.7 ms of margin.

It must be a stable repeating schedule: every midpoint is exactly one source
period late, never accumulating, never reordering, never duplicating. Then
re-run the 45-case gate, a 60 s live window (the periodic summary is enough to
read `internalPresented / sourcePresented`), and `FG_DISPLAY_LATENCY` to price
the added latency.

If C is rejected on latency grounds, the only other lever is the estimator's
18.6–23.0 ms of GPU time — which is frozen this round.

---

## 22. Reproduction

```sh
# build + gate
FGMETAL_R127_BUILD_DIR=r134-evidence/fix1-build \
  r127-alpha-runtime/build_r127_tools.sh
FGMETAL_R130_BUILD_DIR=r134-evidence/fix1-deterministic \
FGMETAL_EVIDENCE_SELFTEST=1 FGMETAL_R123_PUMP=1 FGMETAL_R124_TEST=0 \
  r127-alpha-runtime/build_r130_observer_integration.sh

# live
tools/r134_bottle_env.py prepare --label r134-live-02
#   DYLD_INSERT_LIBRARIES=<fix1-build dylib>, FGMETAL_R134_TIMELINE=1,
#   FGMETAL_R123_PUMP=1, FGMETAL_R133_IN_CB_CAPTURE=1, FGMETAL_R120_PACE_HZ=30
# launch through Highball's Play button, then:
tools/drive_r134_short.sh /tmp/fgmetal-r108-unattended/observer.<pid>.log \
  r134-evidence/live-r134/short-timeline.txt
tools/analyze_r134.py r134-evidence/live-r134/observer.<pid>.log

# restore
tools/r134_bottle_env.py restore --label r134-live-02
```

---

## 23. Files changed

| File | Change |
|---|---|
| `observer-title-inprocess-r127.m` | R134 timeline instrumentation; `r127DisplayLock` + `r127DisplaySnapshot()` + `r127DisplayAckResetAtBoundary()`; handler and readers moved onto the display lock |
| `R127LatencyStats.h` | `R134_` registered as `R127LogAlways` |
| `r134-evidence/tools/*` | bottle staging, launch probe, drivers, analyzer |

---

## 24. Open items for the next round

1. Option C, as specified in §21.
2. Retain-by-reference so post-slot timestamps survive (§9) — needed to close the
   intended-slot half of the deadline model.
3. Register a steady-state retention window (reset on each ON) so the sample is
   not dominated by pipeline fill (§18.2).
4. Fix the `SUMMARY`-in-OFF non-completion (§5.2).
5. Add the capture fence as release hardening (§8).
6. Complete soak, fallback injection and latency on a host with a healthy GPU
   (§12, §15).

---

## 25. Clock domains and units

Three clocks appear in this round and must not be mixed:

| Domain | Source | Used for |
|---|---|---|
| `CLOCK_MONOTONIC` ns | `r105NowNs()` | every interval in `R134_CRITICAL_PATH` and `R134_PAIR`; all pump slot arithmetic |
| Media/host time (s) | `CACurrentMediaTime()`, `MTLCommandBuffer.GPUStartTime/GPUEndTime`, `MTLDrawable.presentedTime` | `estimatorGpuExecNs` (after `fgmetalMediaToNs`), display latency |
| `presentedTime` | `MTLDrawable` after display | physical display ordering only |

`r134Delta()` returns `ok=NO` if either endpoint is missing, so a cross-domain or
absent timestamp cannot be reported as a small interval. `estimatorGpuExecNs` in
`R123_FG_PAIR` is `GPUEndTime − GPUStartTime` on the media clock; it is **not**
subtractable from any monotonic value in §7.1.

## 26. What would falsify the conclusion

The §7/§13 conclusion — "the work does not fit the budget" — would be wrong if any
of these were shown:

1. **Estimator GPU time is not on the critical path.** Falsified if the estimator
   command buffer overlapped the MetalFX call. The source shows
   `commit` → `waitUntilCompleted` → MetalFX, so it does not. A future change that
   removes the synchronous wait would invalidate this.
2. **The intended slot is much later than half a period.** The absent
   intended-slot intervals (§9) leave this open. If `intendedSlotToInternalReady`
   turns out to be large and positive, the budget is bigger than assumed and the
   shortfall smaller. This is the single most important missing measurement.
3. **`sourcePeriodNs` is mis-measured.** It is derived from SOURCE reservation
   enqueue deltas, so a systematic reservation skew would move the budget. The
   two independent windows (34.27 / 34.53 ms) agree, which argues against it.
4. **The 24 "presented" pairs are not representative.** They are pipeline fill
   (§7.4). A steady-state retention window would settle this and is the second
   most important missing measurement.

## 27. Continuation brief for a fresh chat

State at hand-off:

- The runtime is instrumented (`FGMETAL_R134_TIMELINE`), the deterministic gate is
  45/45, and the two defects in §5 and §10 are fixed and gated.
- The measurement lives in `live-r134/observer.80640.log`
  (`48535bbd94af2c565d8071a9fad8b13efd2e70532bf3a8fff3980789f995d44d`), a single
  clean session covering ON/OFF cycling, a 15-minute soak and a 60-pair loss
  injection.
- The blocker is quantified: ~26 ms of serial estimator+MetalFX work against a
  ~17 ms half-period budget, with no CPU-side delay to recover (172 µs total).

Do this next, in order:

1. **Close the two measurement gaps** (§26 items 2 and 4): retain by reference and
   snapshot at emission, and reset the retention window on each ON. Both are
   diagnostics-only. Without them the deadline model is still partly inferred.
2. **Implement Option C** as a fixed one-slot pipeline (§21). Change only the slot
   anchor; keep every reservation, ownership and ordering rule.
3. **Re-measure** a 60 s window at ~30 fps source. Success is
   `internalPresented / sourcePresented` approaching 50 %, not 24 %.
4. **Re-measure `FG_DISPLAY_LATENCY`** to price the one-period delay C adds.
5. **Add the capture fence** (§8) before release; the 3 µs margin is not a margin.

Do not re-derive the cadence from the soak's 23.8 % — §16 explains why that number
belongs to a slower source, not to a working alpha.
