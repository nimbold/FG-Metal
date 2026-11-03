# FG-METAL — R123 — buffered SOURCE presentation

**Round:** R123
**Title:** `SOURCE_PRESENTATION_DRAWABLE_DECOUPLING` — display the SOURCE→M→SOURCE→M chain at one physical refresh per frame while the title keeps producing genuine SOURCE frames at ~30 fps
**Harness:** WorkBuddy AI, DeepSeek v4.1 Flash — High
**Evidence:** `experiments/metalfx_frame_interpolator_R93/evidence/r123-buffered-source-presentation/`

---

## 1. Objective

R122 proved the throughput question and closed it. R123 was asked to spend that
throughput on the display: take the SOURCE image the title produces at ~30 fps,
keep it alive in an FG-owned texture, release the title's own drawable
immediately, and present a bounded `SOURCE A → INTERNAL M(A,B) → SOURCE B →
INTERNAL M(B,C)` sequence at one physical 60 Hz refresh per displayed frame.

The brief was explicit that this is not another instrumentation round. The
deliverable is an architecture that either displays at 60 Hz or names, with
measurement, the thing that stops it.

## 2. What R122 handed over

| Quantity | R122 value |
|---|---|
| SOURCE acquisition | 34.317 ms (with FG 34.319 ms) |
| SOURCE rate | 29.14 fps |
| Generated M rate | 30.49 /s |
| Skipped-busy | 0 |
| FG service median / p90 / max | 10.686 / 11.587 / 11.790 ms |
| p90 margin against the 16.667 ms half-period | **5.080 ms** |
| `SOURCE_IMAGE_CAPTURE_DECOUPLING` | implemented (FG-owned private texture) |
| `SOURCE_PRESENTATION_DRAWABLE_DECOUPLING` | **not implemented** |

R122 also corrected R121: the old ~23 ms SOURCE-retirement figure was the
experimental display hold, not intrinsic game-render dependency. R123 does not
design around 23 ms, and does not re-measure it.

## 3. What was implemented

One architectural change and the instrumentation needed to prove it happened.

**3.1 `SOURCE_PRESENTATION_DRAWABLE_DECOUPLING`.** All three `MTLDrawable`
present hooks (`present`, `presentAtTime:`, `presentAfterMinimumDuration:`) no
longer dispatch the title's own drawable. They record that the SOURCE frame
reached its present point and return. The drawable is released by D3DMetal at
the moment the title expects to release it, and never survives to a later display
slot. Measured on the first live frame:

```
R123_SOURCE_PRESENT=SUPPRESSED seq=1 appFrameId=1 drawableID=0
  wsiPresentId=UNALLOCATED originalDrawableRetained=0 action=return-without-dispatch
```

`originalDrawableRetained=0` is the round's principal claim, and it is recorded
per frame rather than asserted.

**3.2 The display pump.** `R123DisplayPump` is a bounded FIFO of display items
that decides, at each deadline, whether to show SOURCE k or INTERNAL M(k,k+1),
and hands the winner to a presenter that acquires a fresh drawable, copies the
buffered logical image into it, and presents. It holds no Metal object, no
`CAMetalLayer` and no clock, so its decisions can be tested against known ground
truth without a GPU.

Two anchoring rules replace the free-running 16.667 ms grid:

- after SOURCE k: `next = now + halfPeriod`
- after M(k,k+1): `next = capture(k+1) + sourcePeriod`

The second rule exists because a run of missed midpoints would otherwise shrink
the logical display delay and pull the SOURCE stream forward. The source period
is measured at capture instants, not at presentation instants, for the same
reason.

**3.3 Capture selection.** In pump mode every SOURCE frame is captured. Thinning
capture would silently thin the display, which is the one thing this round must
not do.

**3.4 Explicitly not done.** No estimator redesign, no R110/R117 motion-maths
reopening, no MetalFX redesign, no DXMT investigation, no 60→120 attempt, no
generalised swapchain abstraction, no large buffering framework, no
`maximumDrawableCount` change. `maximumDrawableCount` remains 3 and the
architecture was not allowed to require a fourth.

## 4. The decision unit and its self-test

`r123_pump_selftest.m` exercises six cases with known ground truth, event-driven
rather than on a fixed slot grid. `SELFTEST=PASS cases=6`.

| Case | Ground truth | Result |
|---|---|---|
| A | 30.000 fps, midpoints always ready | `src=60/60 int=59 miss=0 idle=0 depth=3 period=33.333ms` |
| B | 29.14 fps (R122's measured rate) | `src=120/120 int=119 miss=0 period=34.317ms` |
| C | 20 ms generation, midpoints cannot make their slot | 59 midpoints missed, every SOURCE still shown exactly once |
| D | no midpoint ever resolves | 39 missed, 40 SOURCE shown exactly once, no fabricated M |
| E | producer outruns the display | bound respected, overload drops frames and says which |
| F | negative control on the order tracker | correct chain accepted; repeated, backwards and dropped frames classified correctly |

Two real defects were found here, before any title run:

1. **Phase drift.** With a free-running 16.667 ms grid the consumer is 2.9 %
   faster than the producer, so a SOURCE frame's presentation phase slides ~1 ms
   per pair until a midpoint's slot arrives before the midpoint exists. Case B
   lost 33 of 59 midpoints. Fixed by the two anchoring rules.
2. **Overflow policy.** Preferring placeholders under overload displayed 40
   interpolations of 3 frames the viewer never saw. Fixed: overflow drops from
   the head regardless of kind and records which kind.

Both accounting identities are exact: `presented + dropped == enqueued` for
SOURCE and for INTERNAL.

## 5. Accounting contract as implemented

| Rule | Implementation |
|---|---|
| SOURCE keeps `AppFrameId` and SOURCE identity | `R101_WSI_DISPATCH origin=SOURCE appFrameId=…` |
| SOURCE receives exactly one `WsiPresentId`, immediately before dispatch | `allocateWsiPresentId()` is called only after the copy is verified; the suppressed present logs `wsiPresentId=UNALLOCATED` |
| INTERNAL owns `InternalPresentId`, no `AppFrameId` | `origin=INTERNAL internalPresentId=… appFrameId=NONE` |
| No INTERNAL becomes SOURCE | origin is fixed at enqueue and never rewritten |
| No WSI ID for a frame not dispatched | every abort path returns before allocation |

## 6. Live evidence — three runs

The title is Elden Ring through Highball/Wine, layer 1472×956, `pixelFormat=80`,
`framebufferOnly=1`, `maximumDrawableCount=3`, Apple M3. Camera motion was
supplied by posting CoreGraphics mouse-move events through the window server into
the title's own input handling — real input events, not memory writes and not
fabricated frames. **It was not a human hand**, and no claim in this report
depends on it being one.

### Run 00 — first attempt (superseded, kept as evidence of the defect)

1974 slot records, of which **1856 were idle** and only 63 were real
presentations. Fresh `nextDrawable` latency p90 1.0056 s. Two defects visible at
once: the pump's stale-clock crawl (§8, item 6) and drawable starvation.

### Run 01 — isolation: suppression on, presentation off

`FGMETAL_R123_NO_PRESENT=1`. Every SOURCE present suppressed, no replacement
drawable ever acquired.

| Quantity | Value |
|---|---|
| SOURCE acquisitions | 1293 |
| SOURCE acquisition period | median **34.250 ms**, p10 33.658, p90 **34.424**, max 1778 |
| Implied SOURCE rate | **29.20 fps** |
| Stalls > 500 ms | **0.3 %** |
| Title's own `nextDrawable` wait | median 0.058 ms, p90 0.165 ms, p99 0.674 ms |

**Conclusion: suppression alone does not starve the title.** Releasing the
title's drawable un-presented is safe. This is the single most important
positive result of the round, and it is the thing R123 was actually asked to
establish.

### Run 03 — full pump, with display sync forced

The layer was found with `displaySyncEnabled=0`:

```
R123_LAYER_PROPERTIES displaySyncEnabled=0 presentsWithTransaction=0
  allowsNextDrawableTimeout=1 framebufferOnly=0 maximumDrawableCount=3
R123_LAYER_DISPLAY_SYNC=FORCED before=0 after=1
```

Forcing it to 1 did not change the outcome, which rules out the presentation
mode as the cause.

## 7. The required measurements

| # | Required quantity | Measured |
|---|---|---|
| 1 | fresh `nextDrawable` latency | **p50 989.072 ms, p90 1001.541 ms, max 2006.747 ms** |
| 2 | private SOURCE→drawable copy execution | **p50 1.038 ms, p90 2.109 ms, max 8.622 ms** |
| 3 | M→drawable copy, if separate | same path and same pipeline as (2) |
| 4 | scheduling delay to WSI dispatch | **p50 994.619 ms, p90 1007.202 ms** — dominated by (1) |
| 5 | actual display interval where observable | **median 1037.625 ms, p90 2074.997 ms → 0.96 Hz** |
| 6 | M deadline misses | 254 of 260 slots |
| 7 | SOURCE acquisition cadence | median 34.412 ms (29.06 fps) but **p90 1002.695 ms** |
| 8 | SOURCE display cadence | 243 SOURCE slots presented; display 0.96 Hz |
| 9 | INTERNAL display cadence | **3 INTERNAL slots presented** |

The budget question has a clean answer. R122 left 5.080 ms of p90 margin for
presentation-side serial work. The copy costs **2.109 ms at p90**. The
presentation-side serial work fits the budget with ~3 ms to spare. What does not
fit is the wait for a drawable, which is three orders of magnitude larger.

The display interval in row 5 is not a property of the pump. It is the layer
retiring roughly one drawable per second, which is also why row 1 is ~1 s: the
`allowsNextDrawableTimeout` of 1 s is the only thing freeing drawables at all.

Drawable identity confirms the mechanism rather than a slow GPU: over ~550
presents the `drawableID` climbed monotonically 305 → 1116. A three-deep pool
reuses three identifiers forever. These were new drawables, not recycled ones.

## 8. Independent review — the ten items

Answered from the code and the logs. Where a run did not reach the condition, the
item is marked unexercised rather than passed.

| # | Item | Finding |
|---|---|---|
| 1 | SOURCE identity survives drawable replacement | **Holds.** SOURCE keeps `AppFrameId` and origin across the drawable swap; the suppressed present logs `wsiPresentId=UNALLOCATED` and the delayed one allocates exactly once. |
| 2 | No unnecessary original drawable retention | **Holds.** `originalDrawableRetained=0` on every suppressed present; the hook returns without retaining. |
| 3 | Drawable count cannot exceed the real pool | **Holds.** `maximumDrawableCount` unchanged at 3; the pump acquires one drawable at a time and holds it only across the copy. |
| 4 | WSI IDs allocated exactly once, immediately before dispatch | **Holds.** Allocation is after the copy is verified; all abort paths return before it. |
| 5 | Delayed SOURCE presentation cannot duplicate the original | **Holds, and this is the round's core result.** The title's present never dispatches, so no frame can appear twice. |
| 6 | Warm-up / missed-M fallback preserves order | **Holds in the unit, unexercised at scale in the title.** Case C and D prove SOURCE is shown exactly once with midpoints dropped; run 03 reached only 3 INTERNAL slots. |
| 7 | No title thread waits synchronously on estimator or MetalFX | **Holds.** Generation is dispatched from the producer completion handler; the pump runs on its own thread; the only waits are bounded. |
| 8 | No unbounded buffer | **Holds.** `maxDepth=8`, observed depth ≤ 4, overflow drops from the head and records the kind. |
| 9 | Evidence only to the R123 evidence directory | **Holds.** `R123_EVIDENCE_BOUNDARY own=…/r123-buffered-source-presentation priorRoundProtected=…/r122-overlapped-gpu-capacity`. |
| 10 | Milestone language matches what timestamps prove | **Holds by construction of this report.** No 60 Hz claim is made; §7 row 5 states 0.96 Hz. |

Two defects in the round's own work were found and fixed rather than explained
away:

- **Stale-clock crawl.** A late slot was handed its own stale deadline, so every
  catch-up tick advanced the schedule by one half-period while real time advanced
  by microseconds. 1856 idle ticks for 63 presentations. Fixed by handing the
  tick the actual wake instant; lateness is still reported as `wakeNs - slotNs`.
- **Backup-path defect in the round's tooling.** `apply_bottle_env.py` restored
  from `bottle.json.pre-r122`, returning the bottle to the wrong generation.
  Fixed to `bottle.json.pre-r123`.

## 9. Product-level consequence

With presentation suppressed, the title's own watchdog fires:

> Unable to start in online mode due to a detected frame rate issue.
> Please restart the game after resolving the issue to play online.

The title measures its own presented frame rate and refuses online mode when it
disagrees. Any shipping form of this architecture has to present a frame rate the
title believes in, or the title will refuse to run online. This is recorded
because it is a real product constraint, not a harness artefact.

## 10. Verdict

R123 established the thing it was principally asked to establish:
`SOURCE_PRESENTATION_DRAWABLE_DECOUPLING` works, the title's drawable is released
immediately and never retained, and the title keeps 29.20 fps at p90 34.424 ms
while every one of its presents is suppressed. The presentation-side serial work
fits the R122 budget with ~3 ms to spare at p90.

R123 did **not** display at 60 Hz, and did not observe a continuous
A→M→B→M→C sequence longer than one tuple. It is blocked on drawable
availability, and the blocker is measured rather than inferred: fresh
`nextDrawable` latency p50 989 ms against a 5.080 ms budget, `presentedTime=0`
on ~72 % of presents, display 0.96 Hz, drawable identities climbing instead of
recycling, unchanged by forcing `displaySyncEnabled`.

```
CONTINUOUS_30_TO_60_TITLE_FG = PARTIAL — fresh CAMetalDrawable availability
```

### The measured blocker, stated precisely

On this layer, a `CAMetalDrawable` acquired by `nextDrawable` and presented from
the pump thread is accepted, reports no error, and is then not retired by the
display: `presentedTime` comes back 0 and the drawable is not returned to the
pool. After three such presents the pool is empty and `nextDrawable` blocks for
its full 1 s timeout, which is the only event that frees a drawable. The pool is
therefore being recycled by timeout rather than by presentation.

This is distinct from, and not explained by, any of: suppression (run 01 shows
suppression alone is healthy), copy cost (2.109 ms at p90), the scheduler (fixed
and self-tested), display sync (forced, no change), or the title's own
throughput (29.20 fps in run 01).

### What R124 should test first, in this order

1. Whether the same fresh-drawable presentation retires correctly when issued
   from the title's own render thread instead of the pump thread. This is the one
   hypothesis the current evidence most supports and has not been tested.
2. Whether a drawable acquired before suppression and presented after one
   source period retires correctly — i.e. whether the failure is the drawable's
   *provenance* rather than its *timing*.
3. Only then, whether pool depth is the binding constraint. The brief forbids
   solving this by adding a drawable, and nothing in this round's evidence
   justifies revisiting that.
