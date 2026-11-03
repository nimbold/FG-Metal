# FG-Metal — R132 — Presentation-Ordered Estimator Ingestion

**Round:** R132 — estimator history follows `SourcePresentOrdinal`, not acquisition order
**Date:** 2026-11-02
**Scope:** one ordering/admission layer between capture completion and estimator history. No estimator mathematics, no MetalFX mathematics, no ownership redesign.
**Corpus:** `experiments/metalfx_frame_interpolator_R93/r132-evidence/`

---

## 1. Executive verdicts

```
SOURCE_PRESENTATION_ORDER_SEMANTICS = VALIDATED
EXACTLY_ONCE_SOURCE_OWNERSHIP       = VALIDATED
LIVE_TITLE_GATE                     = CLOSED — every live SOURCE capture is rejected by the
                                      R130/R131 acquisition-ticket admission gate because the
                                      title's present hook terminalises the ticket before the
                                      capture copy completes, so no ordinal can ever be
                                      CAPTURE_READY and the R123 pump receives no SOURCE work.
RUNTIME_FG_ENABLE_DISABLE           = PARTIAL
FG_FAILURE_FALLBACK                 = NOT_RUN
D3DMETAL_FG_ALPHA_SOAK              = NOT_RUN
FG_DISPLAY_LATENCY                  = NOT_MEASURED
BOUNDED_ALPHA_DIAGNOSTICS           = PARTIAL
D3DMETAL_FG_ALPHA_RUNTIME           = PARTIAL — ordering defect fixed and reviewed; live
                                      pipeline starved upstream of R132.
```

The R132 defect named in the round prompt — estimator adjacency following capture order — is fixed, proven deterministically, and independently reviewed. The live gate stays closed for a **different, pre-existing** reason that R132 neither causes nor is scoped to fix (§26).

---

## 2. Model and agent assignments

| Role | Configuration |
|---|---|
| Root | DeepSeek V4.1 Flash, high reasoning |
| Implementation, analysis, live drive | Root agent (no delegation; the change is one bounded layer) |
| Independent reviewer | Fresh general-purpose agent, read-only, 6 m 05 s, `REVIEW = PASS` |
| Delta re-reviewer | Fresh general-purpose agent, read-only, 1 m 27 s, `DELTA_REVIEW = PASS` |
| Max escalation | **None used.** No ambiguous blocker arose that required arbitration. |

Runtime model identity was not introspected; the configured selection is treated as authoritative per the round's harness rule.

---

## 3. Max escalation

Not used. The two questions that could have warranted it — whether publication order is monotonic, and whether the reclamation pass can steal a live entry — were both resolved by direct lock-order analysis and confirmed by the reviewers (§18). No additional reasoning pass was likely to change the outcome.

---

## 4. Files changed

| File | Change |
|---|---|
| `r127-alpha-runtime/observer-title-inprocess-r127.m` | **+718 / −99 lines.** R132 reorder stage, publication from the three present hooks, estimator adjacency on ordinals, staging reclamation, bounded diagnostics, estimator fields in the periodic summary. |
| `r127-alpha-runtime/r130_observer_pre_capture_integration.m` | **+~330 lines.** Six R132 deterministic cases wired into `main`; pressure cases assert staging peak stays in bound. |
| `r132-evidence/live-tools/run_r132.py` | New. Live transition controller (R127 runtime control surface). |
| `r132-evidence/live-tools/analyze_r132.py` | New. Per-phase log reducer. |
| `r132-evidence/bottle-env-r132.json` | New. Bottle environment for the live run. |

Unchanged and deliberately not reopened: `R123DisplayPump.{h,m}`, `R127SourceOwnership.h`, `MetalFXFrameInterpolationProvider.{h,m}`, the estimator, the MetalFX call, the drawable pool depth.

`git diff --check` clean. R122–R131 reports and evidence untouched.

---

## 5. Final identity and order model

| Identity | Meaning | Not used for |
|---|---|---|
| `AcquisitionId` (`sequence`) | drawable/capture correlation, acquisition ticket, capture readiness, `AppFrameId` | adjacency, ordering |
| `AppFrameId` | unchanged | — |
| `SourcePresentOrdinal` | logical SOURCE presentation order, SOURCE adjacency, estimator history order, INTERNAL pair endpoints | WSI identity |
| `WsiPresentId` | actual WSI dispatch identity only | ordering |

The four identities remain distinct. `SourcePresentOrdinal` is allocated only at a SOURCE presentation boundary, under `[r110WsiDispatchLock lock]`, and published inside that same critical section. R132 adds no identity.

---

## 6. Estimator reorder-stage design

A capture is no longer handed to the estimator at capture completion. Instead:

1. **Stage.** `r123EnqueueSourceDetailed` calls `r132StageCapture`, which places the completed private capture in a fixed 8-slot array keyed by `AcquisitionId`, with `sourcePresentOrdinal = 0`.
2. **Bind.** When the presentation decision assigns an ordinal, `r132PublishPresentation` looks the entry up by `AcquisitionId` and binds the ordinal.
3. **Release.** The bound entry is dispatched to the serial `captureProcessingQueue` as `processCapturedFrame(..., sourcePresentOrdinal)` and cleared by `r132ReleaseStage` after consumption.

Adjacency is decided by `r132HistoryIsAdjacent`, which compares `SourcePresentOrdinal` values (`current == previous + 1`) whenever the pump is enabled, and falls back to acquisition adjacency only on the pre-R132 non-pump experiment path. Every downstream decision that previously read `previousCaptureSequence + 1` now reads this predicate: the gap check, `hasPrevious`, the R116 target-pair gate, the R103 frontier-published gate, and the quality-rejection path.

---

## 7. Staging bound and observed occupancy

**Bound: 8 entries**, equal to the pump's `kR123MaxDepth`. Each entry holds `id<MTLTexture>` (the FG-owned private copy), `id<MTLDevice>`, a condition string, and scalars. **No `CAMetalDrawable` is stored** — only the scalar `drawableID`.

Observed maxima:

| Schedule | Peak occupancy |
|---|---|
| 9-SOURCE capacity | 1 / 8 |
| 80-SOURCE pressure | 1 / 8 |
| Staging capacity case | 8 / 8 (deliberate) |
| Mixed ownership | 1 / 8 |
| Live, 3 491 ordinals | 0 / 8 (nothing was ever admitted — §26) |

The stage is bounded in both count and lifetime. Two reclamation rules free a slot when `r132FindFreeStageLocked()` returns NULL:

- **ticket-terminal** — the acquisition's ticket is inactive, so no ordinal can ever be assigned;
- **frontier-passed** — `r132NewestPublishedSequence > entry->sequence + capacity`, which catches the acquired-but-never-presented acquisition whose ticket is never finished.

Both are exercised deterministically (`R132_STAGE_RECLAMATION`, `R132_FRONTIER_RECLAIM`). An `inFlight` entry is exempt because the serial block that consumed it owns its lifetime.

**Why this was added:** the first R132 implementation had no reclamation. Eight captures whose presentation never arrived would pin their slots permanently and silently disable interpolation while presentation stayed correct. The round prompt requires release on "stale/terminal rejection"; the original code had no such path.

---

## 8. `CAPTURE_READY` / `NO_CAPTURE` semantics

Every ordinal receives exactly one disposition, and the frontier advances through both:

- **`CAPTURE_READY`** — a staged private image exists and was bound; the entry is dispatched to estimator history.
- **`NO_CAPTURE`** — no usable image at the decision boundary: present-before-capture, preflight/allocation/copy failure, stale epoch, staging refusal, or any other case where the image is unavailable when the ordinal becomes terminal.

`NO_CAPTURE` clears any staged entry, advances `r132NextEstimatorOrdinal`, and queues a history reset on the same serial queue that processes frames, so the next frame seeds rather than pairs across the gap. `processCapturedFrame` re-checks adjacency independently, so a lost reset cannot produce a pre-gap/post-gap pair.

An FG-committed SOURCE that reaches ordinal publication without its expected capture increments `r132EstimatorFgMissingCaptureCount` rather than being treated as normal.

---

## 9. Reversed acquisition/presentation test

`R132_REVERSED_ESTIMATOR_ORDER PASS`

```
AcquisitionIdX=105 AcquisitionIdY=106
SourcePresentOrdinalY=1 SourcePresentOrdinalX=2
ingestion=106,105
ESTIMATOR_PAIR=106->105
METALFX_PAIR=106->105
WRONG_PAIR_X_TO_Y=0
```

Both private captures complete in acquisition order (X then Y); presentation order is Y then X. The estimator receives `Y → X`, and the pair handed toward MetalFX is the same pair. The wrong pair count is zero.

---

## 10. Gap test

`R132_ESTIMATOR_GAP PASS`

```
dispositions=100:CAPTURE_READY,101:NO_CAPTURE,102:CAPTURE_READY,103:CAPTURE_READY
ingestion=100,102,103
falsePair100_to_102=0
nextPair=102->103
noCapture=1 stagePeak=1/8
```

Ordinal 101 has no image. 100 is seeded, 101 advances the frontier and invalidates adjacency, 102 becomes a new seed, and the only pair formed afterwards is `102 → 103`. `A → C` is never treated as adjacent.

---

## 11. Late-capture test

`R132_LATE_CAPTURE PASS`

```
AcquisitionId=701 SourcePresentOrdinal=1 disposition=NO_CAPTURE frontier=1
retroactiveIngestion=0 oldPairGenerated=0 ownerUnchanged=1
stageOccupancy=0 released=1 lateCapture=1
```

A capture arriving after its ordinal was published `NO_CAPTURE` and consumed does not insert itself, does not generate an old pair, does not mutate the presentation owner, and releases its texture (the weak reference is nil after the autorelease pool drains).

---

## 12. Mixed ownership test

`R132_MIXED_OWNERSHIP PASS`

```
sequence=FG_READY,TITLE_READY,TITLE_NO_CAPTURE,FG_READY,TITLE_READY
ordinals=1,2,3,4,5
ingestion=400,401,403,404
pairs=400->401,SEED,403->404
falsePair401_to_403=0 totalWsi=5 stagePeak=1/8
```

Estimator ordering depends only on the ordinal, not on the presenter. The `NO_CAPTURE` TITLE source breaks adjacency between 401 and 403; 403 re-seeds and pairs with 404. A TITLE-owned source with a ready image participates normally, confirming that presentation ownership and estimator-image availability are separate facts.

---

## 13. Complete 29-case regression

All 29 R131 cases remain PASS, with the assertions unchanged. `verified4.log`, exit 0, **40 PASS** total: the 29 R131 cases, 7 R132 cases (reversed order, gap, late capture, staging capacity, stage reclamation, frontier reclaim, mixed ownership), and the supporting R130 actual-observer cases.

The 29 cases are the R131 table reproduced in `r131-evidence/R131-REPORT.md` §"Deterministic actual-path matrix": capture-before-present, present-before-capture, ON/OFF transition captures, lookup/transition race, late capture after TITLE/FG terminal, capacity available/unavailable, INTERNAL eviction, all-capacity-protected, queued+claimed boundary, preflight/allocation/copy failure, epoch mismatch, pump claim disturbance, drawable-acquisition failure, presentation-copy failure, pre-WSI abort, ON during unresolved capture, OFF with queued/claimed SOURCE, OFF after WSI without display confirmation, repeated ON/OFF, stale callback after ledger reuse, sustained 80-source pressure, INTERNAL pressure, refusal before suppression.

No ownership, capacity, transition, or dispatch assertion was weakened to accommodate staging.

---

## 14. 9-SOURCE capacity pressure

`R131_SOURCE_CAPACITY_PRESSURE PASS`

```
logical=9 peakReservations=8 capacity=8 internalEvictions=6
refusedBeforeSuppression=1 queuedAndClaimed=1 refusedOwner=TITLE
totalWsi=9 duplicates=0 zeroDispatch=0 committedDropped=0
orderViolations=0 estimatorStagePeak=1/8
```

---

## 15. 80-SOURCE pressure

`R131_80_SOURCE_CAPACITY_PRESSURE PASS`

```
logical=80 titleWsi=8 fgWsi=72 capacityRefusals=8
peakReservations=8 capacity=8 internalEvictionBatches=8
totalWsi=80 duplicates=0 zeroDispatch=0 committedDropped=0
orderViolations=0 overwriteNonTerminal=0 estimatorStagePeak=1/8
```

One record per `AcquisitionId`/`SourcePresentOrdinal` with `totalWsi=1` throughout.

---

## 16. Ledger lifetime

```
R131_UNRESOLVED_LEDGER_COLLISION PASS
  oldAcquisition=1 newAcquisition=257 fgRefusedBeforeSuppression=1
  oldSourceRetained=1 oldFgWsi=1 newTitleWsi=1 staleCallbackRejected=1
  slotRefusals=2 overwriteNonTerminal=0 orderViolations=0

R131_OWNERSHIP_LEDGER_LIFETIME PASS
  logical=300 capacity=256 terminalSlotReuse=PASS
  staleCallbackRejected=1 lastOrdinal=300 orderViolations=0
  overwriteNonTerminal=0 outstandingTickets=0
```

---

## 17. Exactly-once SOURCE WSI accounting

**Deterministic (measured):** every schedule above observes exactly one SOURCE WSI per logical SOURCE — 9/9, 80/80, and the reverse-order schedule 2/2 — with `duplicates=0`, `zeroDispatch=0`, `committedDropped=0`, `orderViolations=0`.

**Live:** not exercised. With zero SOURCE admissions the live run produced no SOURCE WSI at all, so it neither confirms nor contradicts exactly-once.

---

## 18. Independent review

A fresh reviewer with no implementation role read the final diff and the production paths and returned `REVIEW = PASS`, with all 16 required items PASS.

Notable confirmations:
- **Publication order is monotonic** because ordinal allocation and publication occur inside the same `[r110WsiDispatchLock]` region on all three present paths — not because of the condvar. The `sourcePresentOrdinal > r132NextEstimatorOrdinal` branch is therefore unreachable in production.
- **No double consumption:** `r130ClaimTitleDispatch` plus the Suppressed early-return prevent two WSI issues for one acquisition.
- **The 256-entry terminal ring cannot misclassify a legitimate sequence:** a slot collision (`S ± 256`) fails the exact-equality test and is caught by the owner/ticket gate instead.
- **No lock inversion:** the reclamation ticket read always happens under `r127RuntimeLock`, and no path takes `r127RuntimeLock` while holding `r132EstimatorLock`.

Two non-blocking findings were raised and both were fixed before the live run:

1. Reclamation could not see an acquired-but-never-presented acquisition (ticket never finished). → added the frontier-passed rule (§7).
2. Diagnostics performed file I/O inside `r132EstimatorLock`, which the present thread takes while holding the WSI dispatch lock. → all logging moved outside the lock; `r132ReclaimDeadStagesLocked` now performs no I/O.

A second, targeted re-review of exactly those two changes returned `DELTA_REVIEW = PASS` and confirmed no live entry can be reclaimed, no entry can be stolen mid-commit, the frontier counter is lock-consistent and correctly not reset per epoch, and no new race was introduced.

**The reviewer's own caveat:** the production history bookkeeping in `processCapturedFrame` is verified by inspection rather than by a deterministic test, because the test sink short-circuits before it. This is recorded as a coverage limitation, not a defect (§25).

---

## 19. Live transition results

**Configuration.** Elden Ring, Highball `x64-sikarugir10.0_6-r29`, D3DMetal, R126 continuous 30→60 slice. Injected dylib SHA-256 `4bd5ca6d…`, source SHA-256 `ec935d50…`, verified to be the injected entry and not newer than the process.

**First attempt — invalid configuration (preserved in `live-invalid-r115-config/`).** The bottle carried R115-era knobs including `FGMETAL_R115_CONTINUOUS=1`. With that set, `r108SequenceSelectedForCapture` routes to `r115PairCaptureSelectedLocked`, which captures only a reserved pair, so the R123 pump path receives no continuous images:

```
sourceEnqueued=0 estimatorReady=0 estimatorNoCapture=3309 estimatorLate=3308
```

This is a harness configuration error, not a product defect. Recorded because it is exactly the failure signature a starved capture path produces.

**Second attempt — corrected configuration (`live-transition/starved-run/`).** `R120_CONTINUOUS_ENABLED=NO … continuous=0`, pump enabled, capture selected for every frame.

| Measurement | Value |
|---|---|
| Runtime ON accepted | `requestId=1 epoch=2 from=OFF to=ON boundarySequence=63` |
| SOURCE enqueued into pump | **0** |
| SOURCE presented / INTERNAL presented | 0 / 0 |
| Estimator frontier | 3 491 |
| `CAPTURE_READY` dispositions | **0** |
| `NO_CAPTURE` dispositions | **3 491** |
| Late captures | 3 490 |
| Estimator order violations | **0** |
| Staging peak | 0 / 8 |
| `R130_SOURCE_PRESENT_ORDER=VIOLATION` | 0 |
| `overwriteNonTerminal` | 0 |
| Title stalls / deadlocks | none observed; title presented continuously at ~30 Hz |
| Drawable retention (`R125_REGISTRY_AUDIT`) | `drawableObjects=0 textureObjects=0` |

**What this measures.** The R132 frontier invariant held exactly on live data: `ready + noCapture = frontier` (0 + 3 491 = 3 491) with zero order violations, and the stage never exceeded 0/8 because nothing was ever admitted. The reorder stage behaved exactly as specified under a total absence of captures. It did not mis-pair, did not insert retroactively, and did not retain a drawable.

The `OFF` half of the sequence was not reached: the controller aborted when the first `OFF` did not reach `R130_RUNTIME_DISABLE=COMPLETE` within 25 s, because the drain waits on pump state that a starved pump never advances. One `OFF` attempt is recorded in `live-invalid-r115-config/observer.log` (`R127_RUNTIME_DISABLE=FLUSH_STARTED requestId=2 epoch=3 … policy=source-only-60hz-drain`, no COMPLETE). **This is an observation, not a confirmed defect** — it occurred only under starvation and has not been reproduced with a working pipeline.

---

## 20. Failure injection

**Not run.** The bounded deliberate INTERNAL loss path requires INTERNAL frames to exist. With zero SOURCE admissions there were no pairs, no INTERNAL frames, and nothing to drop. Running the injection would have produced a trivially "passing" result that measured nothing.

---

## 21. Soak

**Not run.** A 10–15 minute soak measures cadence stability, queue depth, midpoint misses, duplicate/drop counts, stale callbacks and memory growth **of the frame-generation pipeline**. With `sourceEnqueued=0` the pipeline is inert; a soak would have measured the title's own presentation and nothing about FG-Metal. Reporting it as a soak result would have been misleading.

---

## 22. Display latency

**Not measured.** `R127_LATENCY_SUMMARY` is emitted at summary time from the SOURCE/INTERNAL display callbacks. With no SOURCE or INTERNAL presentations there are no samples, so median/p90/p99 are undefined rather than zero. No percentile is reported.

---

## 23. Bounded diagnostics

**Partial — validated as a policy, not as an alpha behaviour.**

Measured on the live run:

- `R127_DIAGNOSTICS_POLICY mode=bounded healthyPerEvent=first-8 residualMissOrIdle=first-16 anomalySamples=first-64 unknownEvents=first-64 periodicSummarySeconds=60`
- `healthyLogSuppressed=45 448` against `unclassifiedLogSamples=10 507` in a ~3-minute window: the healthy per-frame records were suppressed as designed, and the log grew to 198 lines rather than hundreds of thousands.
- Per-event healthy records (`R132_CAPTURE_COMPLETED`) stopped after 10, confirming the first-8 style bound.
- The 60 s periodic summary now carries `estimatorStage / estimatorPeak / estimatorFrontier / estimatorReady / estimatorNoCapture / estimatorLate / estimatorReclaimed / estimatorOrderViolations`, so estimator state is observable without per-frame logging.

Not validated: behaviour over a multi-hour alpha session, and the absence of any slow leak in the suppression counters. Not tested.

---

## 24. R125 / R126 / R102 regression status

| Item | Status |
|---|---|
| R125 non-owning drawable instrumentation | **Intact.** `R125_REGISTRY_AUDIT … drawableObjects=0 textureObjects=0 retention=DRAWABLE-AND-TEXTURE-NONE` on the live title. No R132 structure stores a `CAMetalDrawable`; the stage stores only the private copy and a scalar id. |
| R126 self-present queue gate | **Intact by inspection.** All three hooks still return before SOURCE capture when the completion's queue is `r123Queue`. Live `R126_SELF_PRESENT_COMPLETION=IGNORED` count was 0 in the starved run because the gate is reached only once the pump is presenting; the gate's presence is confirmed by code, its live exercise is not. |
| R102 deferred-B | **Disabled and untouched.** All three hooks still gate `r102TryDeferSourcePresent` on `!r123PumpEnabled()`; the R132 diff touches only the ordinal/publish lines. |

---

## 25. What was NOT tested

1. **Production estimator history bookkeeping.** `processCapturedFrame`'s adjacency, gap reset and history update are verified by inspection and by the frontier counters, not by a deterministic test that drives the production path with a real provider. The test sink short-circuits before it. This is the single largest coverage gap in R132.
2. **Live `CAPTURE_READY` ingestion.** No live capture was ever admitted, so no live pair was ever formed, presented, or measured.
3. **Live exactly-once SOURCE WSI.** No SOURCE WSI occurred live.
4. **ON→OFF→ON alternation.** Only one ON transition completed; the OFF drain never reached COMPLETE under starvation.
5. **Failure injection, soak, latency percentiles.** See §20–22.
6. **Gameplay motion.** The title was on a static screen. Whether capture/present ordering differs under real camera motion was not tested — see the hypothesis in §28.

---

## 26. Exact remaining blocker

```
LIVE_TITLE_GATE = CLOSED

The R130/R131 acquisition-ticket admission gate rejects every live SOURCE capture,
so no SourcePresentOrdinal can ever be CAPTURE_READY.
```

**Mechanism.** `r123EnqueueSourceDetailed` admits a completed capture only when all three hold:

```objc
owner == R127SourceOwnerUnknown &&
r130HasActiveTicketForSequenceLocked(sequence) &&
captureEpoch == r127RuntimeEpochSnapshot()
```

On this title, the present hook for sequence *N* runs and completes before the capture copy for *N* finishes. The present hook calls `r130FinishTitleAcquisitionLocked`, which sets `ticket->active = NO`, and `r127RecordSourceOwnerIfUnknown` / the TITLE dispatch sets the owner away from `Unknown`. When the copy then completes, both the owner test and the ticket test fail, the capture is counted as a stale callback, and no image ever reaches the stage.

Measured signature: `estimatorReady=0`, `estimatorNoCapture=3491`, `estimatorLate=3490`, `sourceEnqueued=0`, `staleCallbackRejections` ≈ one per frame.

**Why this is not an R132 regression.** R126's `r123EnqueueSource` admitted every capture unconditionally:

```objc
static void r123EnqueueSource(uint64_t sequence, uint64_t appFrameId, id<MTLTexture> texture) {
    if (!r123PumpEnabled() || !texture) return;
    r124NoteCapture(sequence, texture);
    r123EnsureStarted();
    ...
```

The ownership and ticket conditions were introduced in R130/R131 as part of the transactional ownership work. R131 never ran live, so this ordering assumption was never exercised. R132 changed *when a capture is admitted to estimator history*, not *whether it is admitted to the ownership transaction*.

Fixing it requires changing when the acquisition ticket is terminalised relative to capture admission — an R130/R131 ownership-architecture change. The round prompt forbids reopening that architecture unless R132 demonstrates a specific regression, and it does not.

---

## 27. Smallest next product step

**Decouple capture admission from ticket terminalisation for one bounded window.** Specifically: admit a completed capture when the acquisition was *acquired under the current epoch* and has not been superseded, rather than requiring the ticket to still be `active` at copy completion. The ticket exists to bound ownership accounting, not to gate estimator-image availability; those are the two separate facts R132 already distinguishes.

The minimal shape:

1. Record the acquisition epoch on the ticket at `nextDrawable`, and keep that record readable after `active` is cleared.
2. In `r123EnqueueSourceDetailed`, replace the `acquisitionStillActive` term with "the sequence was acquired in the current epoch and has not been reused by the ledger".
3. Keep the owner test as-is: a SOURCE already dispatched as TITLE must still not be re-admitted to the pump.
4. Add one deterministic case: capture completes *after* the present hook for the same sequence, and must still stage and publish `CAPTURE_READY` for the ordinal that the present already assigned.

That is a bounded, testable change to one predicate plus one ledger field. It does not touch estimator mathematics, MetalFX, the reservation scheme, or the pump.

Only after that lands and the live pipeline produces `CAPTURE_READY > 0` do the transition, failure-injection, soak and latency items in this round become measurable.

---

## 28. Fact, interpretation, hypothesis

**Measured fact**

- All 40 deterministic cases pass, exit 0; the 29 R131 cases are unchanged.
- Reversed acquire/present yields `ESTIMATOR_PAIR = Y→X` with `WRONG_PAIR_X_TO_Y = 0`.
- A `NO_CAPTURE` ordinal breaks adjacency; the next valid pair is `C→D`.
- A late capture produces no retroactive insertion, no old pair, no ownership mutation, and releases its texture.
- Mixed FG/TITLE ownership orders by ordinal only.
- On 3 491 live ordinals: `ready + noCapture = frontier`, zero order violations, staging peak 0/8, zero drawable retention.
- Two independent reviews returned PASS.

**Interpretation**

- R132's ordering layer is correct and its invariants hold under both synthetic and live conditions, including a total absence of captures.
- The live pipeline is starved upstream of R132, at the R130/R131 ticket gate.
- The `NO_CAPTURE`-only live profile is consistent with present-before-capture on every frame, not with a fault in the reorder stage.

**Hypothesis (not tested)**

- Capture completion may precede the present hook under real gameplay motion, in which case the starvation is an artefact of the static title screen rather than a general defect. The static screen re-presents a small set of drawables and may invert the ordering. This is why §27 is framed as a bounded admission change plus one deterministic case, not as a confirmed architectural fault: the first thing to do is re-run the same live probe in gameplay and see whether `estimatorReady` becomes non-zero without any code change.

---

## 29. Evidence index

| Artefact | Contents |
|---|---|
| `verified4.log` | Final deterministic gate: 40 PASS, exit 0 |
| `verified4/` | Immutable source snapshot + `source-sha256.txt` for that binary |
| `repro-unit.log`, `repro-integration.log` | Independent rebuild reproducing the pre-fix gate |
| `observer-r131-to-r132-final.diff` | Authoritative observer change set |
| `integration-r131-to-r132-final.diff` | Harness change set |
| `live-runtime-build.log` | Live dylib build + source hashes |
| `live-invalid-r115-config/` | First live attempt, R115 pair-capture configuration |
| `live-transition/starved-run/` | Corrected-configuration live run, 3 491 ordinals |
| `live-tools/run_r132.py`, `analyze_r132.py` | Controller and reducer |
| `bottle-env-r132.json` | Bottle environment used |
