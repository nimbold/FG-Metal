# FG-METAL — R120

## CONTINUOUS 30→60 TITLE FRAME GENERATION

`CONTINUOUS_30_TO_60_TITLE_FG = NOT_VALIDATED`

The round established a genuine 30 fps SOURCE cadence, found and fixed a
pacer-induced deadlock, produced real interpolation frames with correct A/M/B
identity and a perfect one-refresh M→B interval, and measured precisely why
continuous 60 fps output is not yet reached: tuple occupancy is 47.8 ms against
the 33.3 ms per-pair budget, of which 25.1 ms is non-GPU overhead in the
interpolation presentation path.

---

## 1. WorkBuddy / model summary

| item | value |
|---|---|
| harness | WorkBuddy AI |
| model | DeepSeek v4.1 Flash — High, orchestrator and all subagent roles |
| model-family fallback | none |
| agents used | one implementation thread; independent review performed inline against the brief's nine review items |
| display | built-in 60 Hz panel, 2560×1664 (Apple M3), `maximumDrawableCount=3` |

No 60→120 work was attempted. No estimator redesign, no GPU-contention research,
no sub-refresh experiments, no depth research, no UI work.

## 2. Evidence isolation

Phase 0 preserved the R119 resolver and corpus guard unchanged; only the "own
round" constant moved from `r119-live-throughput` to `r120-continuous-30-60`, so
R119 is now protected by the same rule that protected R115–R118 from R119.

Four in-process probes: R120 override writable; legacy R115 directory refused;
an override aimed at R119 refused; a `..`-spelled path into R119 refused.
`priorRoundWritable=0` is logged at every startup.

Corpus snapshots, all content-hashed:

| point | files | bytes | added | removed | violations |
|---|---|---|---|---|---|
| baseline | 5,324 | 29.98 GB | — | — | — |
| pre-live | 5,362 | — | 38 | 0 | 0 |
| pre-pairs | 5,362 | — | 38 | 0 | 0 |
| post-pairs | 5,395 | 30.08 GB | 71 | 0 | **0** |

**Disclosed incident (pre-existing in this round, not hidden).** Running
`verify_r119_estimator.py` from the R120 directory rewrote R119's
`embedded-estimator-r119.metal` with byte-identical content (`d1120f88…` before
and after); only mtime moved, and the compare tool flags mtime before hash. The
root cause was fixed — the verifier now refuses to write outside its own round —
the recorded mtime was restored, and the event is documented in
`evidence-isolation/VALIDATION.md`. The general lesson: the corpus guard protects
the observer, not the Python tooling.

## 3. 30 fps SOURCE pacing mechanism

`FGMETAL_R120_PACE_HZ=30` → a bounded rate limiter that blocks the game's frame
production until the paced deadline. It is back-pressure, not frame thinning:
every present still produces one present, nothing is dropped, relabelled or
suppressed, and accounting is untouched. The wait is capped at two periods and
the schedule is rebased on resume, so there is no drift and no catch-up burst.

The wait's position moved twice during the round, each move forced by a
measurement:

| build | wait location | outcome |
|---|---|---|
| run-02 | top of `present()` | **deadlock**, `accepted=0` of 30 attempts |
| run-03 | end of `present()` | works, but +26.2 ms dead time before every generation |
| run-04 | `nextDrawable` (acquisition) | works, dead time 3.2 ms |

Root cause of the run-02 deadlock, from the event trace: with the wait before
admission, SOURCE A sat inside `present()` while SOURCE B's producer committed.
The R103 frontier closed for pair (A,B) ahead of A's admission; A blocked waiting
for the frontier to reopen; the frontier reopens only when B's present is
deferred; B's present cannot begin until A's present returns. Circular wait,
broken only by the 150 ms watchdog (`hookEnteredForPair=0`, 29 of 30 tuples).

Pacing acquisition instead bounds the SOURCE rate at its source and touches no
present, no frontier and no tuple state, leaving the R116-validated display path
unchanged.

## 4. Measured SOURCE cadence

Run-01, present-paced, 25 s window, 744 SOURCE frames:

| measurement | value | target |
|---|---|---|
| acquisition median | **33.457 ms** | 33.333 |
| public Present median | 34.340 ms | 33.333 |
| displayed (`presentedTime`) median | **33.333 ms** | 33.333 |
| presents actually blocked | 738 of 744 | — |
| short-interval fraction | 0.0054 | ~0 |
| origins | `{SOURCE: 744}` | SOURCE only |

`TRUE_30FPS_SOURCE_CADENCE = ESTABLISHED`.

The `presentedTime` median of exactly 33.333 ms is the decisive number: the
display settled on every second 60 Hz refresh. 738 of 744 presents were actually
blocked by the limiter, which is what rules out the forbidden cheat of letting
the game run at 60 fps and discarding half the frames. The analyser is
independently proven to reject that cheat: a synthetic log with 60 fps
acquisition and presents thinned to 30 fps returns `NOT_ESTABLISHED` on the
*acquisition* criterion.

In the pair runs the cadence held: run-04 acquisition median 34.33 ms.

## 5. One-frame look-ahead architecture

The frozen R115/R116 pipeline already implements the look-ahead for B: it defers
SOURCE B's present, generates M(A,B) on a background queue, then dispatches M and
B back-to-back onto consecutive refreshes (R116 early-B). That is the A→M→B
display order, and it works — measured M→B = **16.666 ms**, one refresh, in 10 of
11 pairs.

It does **not** hold A. A is presented at its natural deadline, so the gap before
M is pure generation latency. Measured **A→M = 50.000 ms**, median, every run.

This is the brief's own premise, confirmed by measurement: *"A cannot be
displayed at its original immediate deadline if M(A,B) is to appear halfway
between A and B."* Holding A is the remaining architectural work, and it is
coupled to the throughput problem in §8 — holding A alone would yield at most
~40–45 fps because only one pair in three is interpolated.

## 6. Buffer ownership / lifetime

| property | value |
|---|---|
| drawable pool | `maximumDrawableCount=3`, `allowsNextDrawableTimeout=1` |
| concurrent demand | deferred B + INTERNAL M + next SOURCE = 3 |
| pool sufficient | yes (`poolSufficient=1`) |
| drawable acquisition failures | **0** across all runs |
| starvation / pool exhaustion | none observed |

A guard was added: the controller refuses to arm if `maximumDrawableCount < 3`,
because with fewer the render thread would block inside `nextDrawable` and freeze
rather than fall back. No source texture copying was introduced; retaining the
original drawables proved adequate, which is what the brief permitted if
measurement supported it.

## 7. Pipeline priming

**NOT MEASURED.** The prime latency (start request → first M presented) was not
instrumented in this round, so no figure is claimed. What is known is that the
first tuple of each run shows the largest occupancy (64.5 ms vs a 47.8 ms
median), consistent with a cold first pair, and that provider warm-up gated the
first arm (`providerWarmCount=0` on attempt 1).

## 8. Generation throughput

Per-tuple decomposition, run-04, median of 11 accepted tuples:

| component | ms | share |
|---|---|---|
| estimator (R117 optimized) | 17.9 | — |
| MetalFX | 11.1 | — |
| fast gate (R116) | 0.08 | — |
| **GPU subtotal** | **22.0** | 46% |
| queue + M drawable acquisition + internal copy render + dispatch | **25.1** | 54% |
| **total service (`bPresentHookToMWsiNs`)** | **47.8** | 100% |
| **budget for one pair per source period** | **33.3** | — |

`bHoldNs` equals service (47.8 ms) because early-B forwards B as soon as M is
dispatched, so the deferred drawable is held for exactly the service time.

The estimator is **not** the blocker. R119's 60 fps service was 19.7 ms; here the
GPU work is 22.0 ms, essentially unchanged. The round's throughput finding is
that the interpolation *presentation* path costs ~25 ms on top of the GPU work,
and that is what puts the pipeline 1.44× over the 33.3 ms budget.

Consequence: at most ~21 M per second, against the 30 per second needed for
continuous 30→60. Observed arming was one pair in three (frames 1720, 1723, 1726,
1729 …), i.e. ~10 M/s.

## 9. 10-pair run

Four live runs were required. Only the last two produced interpolation.

| run | pacer | attempts | accepted | fallback | watchdog | verdict |
|---|---|---|---|---|---|---|
| 02 | top of present | 30 | **0** | 30 | 29 | deadlock, no M ever produced |
| 03 | end of present | 14 | 10 | 3 | 1 | pipeline works, cadence fails |
| 04 | acquisition | 13 | 10 | 2 | **0** | pipeline works, cadence fails |

Run-04 (`run-04-pairs10-acqpace`) is the authoritative one: 10 accepted, 10 M
displayed, 0 ordering violations, 0 coalesced presentations, 0 missing
callbacks, 0 source leaks, 0 stale callbacks, 0 drawable acquisition failures.

## 10. 50-pair run

**NOT RUN.** The brief gates it on a clean 10-pair run; 10 pairs was not clean.
Running 50 would have produced a longer record of the same defect, not new
information.

## 11. Actual A/M/B display order

Run-04, 11 tuples with display evidence:

| property | value |
|---|---|
| A → M → B ordered | **11 of 11** |
| ordering violations | **0** |
| distinct M and B | 11 of 11 |
| coalesced | **0** |
| duplicate INTERNAL | 0 |
| duplicate SOURCE | 0 |
| SOURCE reorder or unexplained drop | 0 (13 gaps, all explained by forwarded fallback B) |

The pipeline's own `R116_PRESENTATION_ORDER` flags agree with the independent
analysis: `ordered=1 distinct=1 coalesced=0` for every accepted tuple.

## 12. Presented-time cadence

All cadence figures come from `presentedTime`, never from WSI dispatch
timestamps.

| interval | median | target |
|---|---|---|
| A → M | **50.000 ms** | 16.667 |
| M → B | **16.667 ms** | 16.667 |
| overall output | **30.64 fps** | 60 |
| intervals within ±4 ms of 16.667 | **18.8%** | ~100% |

So the output is not a 60 Hz stream. A appears at its natural refresh, then a gap
of 3 refreshes, then M, then B one refresh later. Between tuples the SOURCE
frames continue at 33.3 ms spacing. The result is ~30 fps with occasional
interpolation, not 60 fps.

## 13. Queue depth / backlog

| property | value |
|---|---|
| deferred SOURCE records | fixed capacity 1, never exceeded |
| INTERNAL slot | single fixed slot, states Empty/Claimed/Presented |
| interval drift, first vs second half | **0.000 ms** |
| monotonic backlog growth | none |
| overflow events | 0 |

The structure is bounded by construction rather than by policy: one deferred
ticket, one internal slot, no dynamic queue. Nothing in the measurements shows
queue growth.

## 14. SOURCE / INTERNAL accounting

| property | value |
|---|---|
| SOURCE AppFrameId preserved | yes |
| INTERNAL InternalPresentId, no AppFrameId | yes |
| unique WsiPresentId at dispatch | yes |
| `sourceLeaksWhileClosed` | **0** |
| `staleCallbacks` | 0 |
| `duplicateMDispatch` | 0 |
| `resets` | 12 (run-04), all rejected attempts, not accepted tuples |

No display delay was used to rewrite identity.

## 15. Fallback behaviour

2 fallbacks in run-04, 3 in run-03. In every case SOURCE B was forwarded through
its original implementation and displayed as a normal SOURCE frame; the
displayed sequence remained A → B with no corruption, no reorder and no
duplication. The pipeline re-armed on the next eligible pair. A fallback tuple's
B records no `presentedTime`, because the forward path installs the R116 display
handler only once M has been dispatched — so those frames are unobservable rather
than lost, and the analyser reports them as such instead of counting them as
drops.

## 16. Added latency

| measure | value |
|---|---|
| A → B presented span | **66.666 ms** |
| natural consecutive-SOURCE span | 33.333 ms |
| added display latency | **+33.333 ms (+2 refreshes)** |

This is honest cost, not a low-latency claim. A one-frame look-ahead is expected
to increase latency, and it does. Note the span improved from run-03's 83.332 ms
after the pace wait left the present path.

## 17. Visual assessment

**NOT PERFORMED.** No bounded visual sample was reviewed in this round, so no
NONE_OBVIOUS / MINOR / MATERIAL / CATASTROPHIC classification is issued.
Constant depth remains a prototype and no depth research was started. Structural
success was not reached, so the brief's own sequencing defers the visual check.

## 18. Game stability

Across four live sessions the game did not crash, hang or freeze. The one
watchdog firing in run-03 and the 29 in run-02 were pipeline-level aborts that
correctly reopened the frontier and forwarded the SOURCE frame; the render thread
was never left parked. The 150 ms bound fired only when the pipeline could not
reach its own display evidence.

## 19. 30→60 verdict

`CONTINUOUS_30_TO_60_TITLE_FG = NOT_VALIDATED (CONSTANT_DEPTH_PROTOTYPE)`

Against the brief's eleven criteria:

| # | criterion | result |
|---|---|---|
| 1 | actual SOURCE cadence ≈30 fps | **PASS** (33.46 / 34.33 ms) |
| 2 | actual output cadence ≈60 fps | **FAIL** (30.64 fps) |
| 3 | multiple consecutive pairs interpolate | **PASS** (11 ordered) |
| 4 | displayed sequence A→M→B→M→C | **PASS** in order; cadence not continuous |
| 5 | presented spacing ≈ one refresh | **FAIL** (A→M 50.00 ms) |
| 6 | generation keeps up with 30 fps arrival | **FAIL** (47.8 ms vs 33.3 ms) |
| 7 | bounded queue does not grow | **PASS** |
| 8 | no SOURCE silently dropped or duplicated | **PASS** |
| 9 | accounting remains correct | **PASS** |
| 10 | fallback works | **PASS** |
| 11 | game remains stable | **PASS** |

Six of eleven pass. The headline claim is not made.

## 20. Exact next PRODUCT blocker

**Tuple occupancy is 47.8 ms against a 33.3 ms budget, and the majority of the
excess is not GPU work.**

Two changes are required, in this order:

1. **Cut the ~25.1 ms non-GPU cost of the interpolation presentation path.**
   This is queue + INTERNAL drawable acquisition + internal copy render +
   dispatch. It is larger than the entire estimator. With `maximumDrawableCount=3`
   and a deferred B occupying one, M's `nextDrawable` plausibly waits a refresh
   or more; that hypothesis is stated, not proven, and is the first thing to
   instrument.
2. **Hold A as well as B**, then dispatch A → M → B back-to-back, so A→M becomes
   one refresh instead of 50 ms. Without change 1 this alone caps output near
   40–45 fps, so it must not be done first.

Only when both are in place does continuous 30→60 become reachable. Until then
the correct claim for this hardware is: *real interpolation frames with correct
identity and a proven one-refresh M→B interval, delivered at roughly one pair in
three.*

Recorded for future 120 Hz hardware, unchanged: R119's 60 fps generation service
of ~19.7 ms against ~17.5 ms SOURCE arrival remains a performance blocker. It is
not R120's blocker.

---

## Evidence index

| path | content |
|---|---|
| `evidence/r120-continuous-30-60/run-01-pace/` | 30 fps SOURCE validation |
| `run-02-pairs10/DIAGNOSIS.md` | pacer deadlock, root cause and trace |
| `run-03-pairs10-fixed/DIAGNOSIS.md` | first working interpolation; dead-time measurement |
| `run-04-pairs10-acqpace/` | authoritative run: `analysis.json`, `slice.log` |
| `evidence-isolation/` | baseline and per-phase corpus snapshots, `VALIDATION.md` |
| `tools/analyze_r120_pairs.py` | 18-check analyser, `presentedTime` only |
| `tools/test_analyze_r120_pairs.py` | 5-case self-test, 5 defect classes rejected |
| `tools/pace_model_test.c` | pacer arithmetic model, 0.000 ms drift over 3,600 presents |
| `build-verification.json` | `R117_OPTIMIZED_ESTIMATOR_PRESENT`, dylib `1bc2c603…` |
