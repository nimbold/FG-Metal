# FG-METAL — R133 — RESTORE LIVE CAPTURE AVAILABILITY WITHOUT BREAKING TRANSACTIONAL OWNERSHIP

Round directory: `experiments/metalfx_frame_interpolator_R93/r133-evidence/`
Supersedes nothing. R122–R132 reports and evidence are untouched.

---

## 1. Executive verdicts

| Verdict | Value |
|---|---|
| `LATE_CAPTURE_ESTIMATOR_ADMISSION` | **VALIDATED** — a TITLE-owned SOURCE whose private copy is committed but not yet GPU-complete is admitted as `PENDING_CAPTURE`, resolves to `CAPTURE_READY` inside a one-ordinal grace, and is consumed at its own ordinal without a second SOURCE WSI. |
| `FG_SOURCE_COMMIT_PREREQUISITE` | **`CAPTURE_COPY_COMMITTED_IN_PRODUCER_CB`** — measured `copyBeforePresent=14319`, `copyAfterPresent=0`, `prerequisiteAbsent=0` over 14 319 live SOURCE frames. |
| `LIVE_CAPTURE_AVAILABILITY` | **RESTORED** — 14 319/14 319 captures committed and completed, `captureFailed=0`, `estimatorLate=0`, `estimatorNoCapture=110` (0.77 %, 94 of which are the deliberately injected fault). R132's signature (`CAPTURE_READY 0`, `NO_CAPTURE 3 491`) is gone. |
| `LIVE_TITLE_GATE` | **OPEN** — all three gate conditions hold live: `CAPTURE_READY 14209 > 0`, `SOURCE enqueued 8201 > 0`, `INTERNAL presented 52 > 0`. The 30→60 claim is **not** made: INTERNAL presentation is 0.63 % (see §16). |
| `RUNTIME_FG_ENABLE_DISABLE` | **PASS** — `OFF→ON→OFF→ON→OFF→ON` (epochs 2, 5, 8, 11): no deadlock, `discardedSource=0`, `discardedInternal=0`, `queueDepthBefore/After=0`, no SOURCE loss or duplicate, `orderViolations=0`. |
| `FG_FAILURE_FALLBACK` | **PASS (bounded)** — 200 injected INTERNAL pair losses, `refusedPairs=200`, `recovery=automatic`, `droppedSource=0` throughout, `estimatorNoCapture` bounded at 110 and static afterwards. |
| `D3DMETAL_FG_ALPHA_SOAK` | **PARTIAL** — 5 min 05 s of continuous ON (17:44:11 → 17:49:1x), then the title exited on its own. Below the required 10–15 min. No observer-side fault is implicated (the log ends with a clean `R127_GENERATION_RECOVERED`). |
| `FG_DISPLAY_LATENCY` | **MEASURED** — present-hook→physical display: median 36.95 ms, p90 47.00 ms, p99 54.10 ms (n = 6 951). |
| `BOUNDED_ALPHA_DIAGNOSTICS` | **PASS** — 381 log lines for 14 319 captures; `healthyLogSuppressed=240441`, `unclassifiedLogSuppressed=42764`; no per-frame logging. Known bound: the 64-sample anomaly budget saturates during long fault campaigns, so totals stay exact but per-pair detail does not (§18, §21). |
| `D3DMETAL_FG_ALPHA_RUNTIME` | **NOT_VALIDATED** — INTERNAL reaches the screen for 0.63 % of SOURCE presentations, so the runtime is a correct 30 fps SOURCE path with an almost-never-firing midpoint, not a 30→60 path. |

---

## 2. Model and agent assignments

Orchestration and architecture: Max. Implementation and test work: High. Final safety review: a separate read-only Max reviewer that did not write any of the changed code. Model identity was not self-verified; model metadata availability is not an acceptance criterion for this round.

---

## 3. Max escalation

None. A bounded original-drawable lease was never needed and was never implemented (§11), so the narrow arbitration trigger in the round prompt did not fire.

---

## 4. Files changed

| File | Change |
|---|---|
| `r127-alpha-runtime/observer-title-inprocess-r127.m` | R133 capture scheduling, capture-availability record, bounded `PENDING_CAPTURE`, FG prerequisite, diagnostics. |
| `r127-alpha-runtime/r130_observer_pre_capture_integration.m` | Five new R133 deterministic cases wired into `main()`; R133 helpers. |
| `r133-evidence/bottle-env-r133.json` | New. Live manifest. |
| `r133-evidence/tools/r133_bottle_env.py` | New. Reversible bottle staging (prepare/restore/status). |
| `r133-evidence/` | New round directory. Nothing else in the tree was modified. |

`R123DisplayPump.{h,m}`, `R127SourceOwnership.h`, `R127SourceAck.h`, `MetalFXFrameInterpolationProvider.{h,m}` are byte-identical to R132 (hashes in `live-build.log`).

---

## 5. Root cause, from measurement

R132's live log established the order inside one frame:

```
R110_SOURCE_PRODUCER_COMMIT → R110_SOURCE_PRESENT_ENTRY → R101_WSI_DISPATCH
→ DRAWABLE_PRODUCER_COMPLETED → ELDENRING_SOURCE_COPY_ENQUEUED → R132_CAPTURE_COMPLETED
```

The private copy was encoded from the producer command buffer's **completion handler**, which runs after `present` was entered. By the time the present hook asked for an image, the copy had not even been scheduled, so every ordinal was finalised `NO_CAPTURE` and no SOURCE could ever be FG-owned. The R132 report's suggested next step ("relax `acquisitionStillActive`") would have relaxed *ownership* to fix an *estimator* problem; it was not applied.

---

## 6. The three independent facts (Part 1)

| Fact | Store | Question it answers |
|---|---|---|
| Presentation ownership | `r127SourceOwnership` (256-slot ledger) + `r130AcquireTickets` | Who presents this SOURCE: TITLE or FG? |
| Capture availability | `r133CaptureRecords` (256 slots, exact-sequence match, leaf mutex) | Does a private copy exist, is it committed, did it complete or fail? |
| Estimator disposition | `r132EstimatorStages` (8 slots) + `r132EstimatorTerminals` | Was this ordinal `CAPTURE_READY`, `PENDING_CAPTURE` or `NO_CAPTURE`? |

Capture availability was deliberately **not** stored in the estimator's 8-slot reorder table. The first implementation did store it there and the 80-acquisition-backlog case exposed the consequence: with 80 acquisitions outstanding, only 8 can hold a stage slot, so the 80th frame silently lost its FG eligibility even though its copy existed. Availability now has its own bounded record, sized to the ownership ledger, so estimator reorder depth can never withdraw the prerequisite.

---

## 7. Capture scheduling (Option B)

`r133CommitCaptureForCommandBuffer` is called from `hookCommandBufferCommit` after the title's last render pass and **before** the command buffer is committed:

1. look up the SOURCE record for this command buffer (`r133SourceRecordForCommandBuffer`),
2. read the drawable texture from the weak table (`r133TrackedTexture`, `__weak`, filled from `reportCorrelation`),
3. preflight (device, `framebufferOnly`, `ShaderRead`, 64…4096 × 64…2160, BGRA8/RGBA8),
4. allocate a private `MTLStorageModePrivate` texture, `ShaderRead|ShaderWrite`,
5. encode the blit **into the producer command buffer**,
6. stage it (`copyCommitted=YES`, `copyComplete=NO`) and record availability,
7. `addCompletedHandler` → `r133CaptureDidComplete`.

No extra command buffer, no drawable retention, no title-thread wait. The legacy path (`captureRealDrawable`) detects the in-command-buffer copy and skips itself: `R133_LEGACY_CAPTURE=SKIPPED … reason=in-producer-command-buffer-copy-present`.

---

## 8. Why `CAPTURE_COPY_COMMITTED_IN_PRODUCER_CB` is the right prerequisite

The copy lives in the title's own producer command buffer. Its completion is therefore implied by that command buffer's completion and does **not** depend on the title present being issued. That is exactly the property R132 lacked: the old copy depended on a completion handler that ran after `present`.

Measured live: `copyBeforePresent=14319`, `copyAfterPresent=0`. In 14 319 consecutive frames the copy was always already committed when the present hook ran. `prerequisiteSatisfied=14319`, `prerequisiteAbsent=0`.

Failure behaviour is explicit and bounded: a preflight, allocation, blit-encoder or copy failure sets `failed` on the record, `r133CaptureCommittedForSequence` immediately returns NO for every later frame, and the frame that was already FG-committed is presented exactly once from the reservation's own strong payload (§14, case 4). The residual cost of a GPU copy failure after commit is one SOURCE frame sampled from an unwritten private texture; that is strictly better than R132's 100 % loss and is recorded as a known limitation in §24.

---

## 9. Bounded `PENDING_CAPTURE` (Part 2)

- Capacity `kR133MaxPendingOrdinals = 4`; grace default **1 SOURCE interval**, env `FGMETAL_R133_PENDING_GRACE`, clamped to `[1, 3]`.
- An ordinal enters pending only when `copyCommitted && !copyComplete && !copyFailed` and the stage entry holds a texture and device.
- Release is strictly in ordinal order: an unresolved head blocks later ordinals (`r133DrainPendingLocked`).
- The window closes when `head.ordinal + grace <= publishFrontier`; the ordinal is then finalised with the disposition it earned.
- Hard bound reached (only possible if grace were misconfigured): fail closed to `NO_CAPTURE`.
- Pending never takes `r127RuntimeLock`, never waits, never retains a drawable, never reserves FG capacity, and never changes the present hook's return value — the WSI decision is made from the separate commit fact.

Measured live: `pendingPeak=1`, `pendingNow=0`, `pendingExpired=0`, `pendingReady=14209`. The pending state exists, is entered, and resolves inside one interval. The one-ordinal grace was sufficient; no extension was needed.

---

## 10. Late capture, expired capture, known failures

| Situation | Behaviour |
|---|---|
| Matching AcquisitionId, epoch, valid ordinal, still `PENDING_CAPTURE`, unexpired | `CAPTURE_READY` for the estimator only. Owner, reservation and WSI identity untouched. |
| Capture after the window closed | Rejected (`r133LateExpiredCaptures`), stage slot released, record cleared, history never rewritten, no retroactive insertion. |
| Preflight / allocation / copy / stale-epoch failure | `copyFailed` → prerequisite withdrawn immediately → TITLE direct. |

---

## 11. Drawable retention

Not reintroduced. `R133TrackedTexture.texture` is `__weak`; the capture record is scalars only. Live audit: `R125_REGISTRY_AUDIT … drawableObjects=0 textureObjects=0 otherObjects=10 retention=DRAWABLE-AND-TEXTURE-NONE`.

---

## 12. Deterministic estimator tests 1–5

All from `r133-evidence/deterministic.log`:

```
R133_PENDING_CAPTURE_WITHIN_GRACE PASS AcquisitionId=460 ordinal=1 pendingBefore=1 pendingAfter=0
    ingestion=1 ownerAfterPresent=2 ownerAfterWsi=3 fgWsi=1 titleWsi=0 secondSourceWsi=0
R133_PENDING_CAPTURE_EXPIRY PASS ordinal=1 grace=1 expired=1 noCapture=2 frontier=2
    lateRejected=1 retroactiveIngestion=0
R133_EXPIRED_GAP_NOT_ADJACENT PASS ordinals=1:NO_CAPTURE,2:NO_CAPTURE,3:472,4:473
    ingestion=472,473 pair=472->473 falsePair471_to_472=0 noCapture=2
R133_MULTIPLE_PENDING_BOUNDED PASS grace=3 pendingPeak=3 boundedBy=1
    ingestionOrder=480,481,482 pairs=480->481,481->482 orderViolations=0
```

1. **Present then late capture inside grace** → `CAPTURE_READY`, consumed at ordinal 1, exactly one FG WSI, no title WSI, duplicate completion inert (`secondSourceWsi=0`).
2. **Present then no capture** → grace expires → `NO_CAPTURE`, frontier advances to 2.
3. **Capture after expiry** → rejected, no retroactive ingestion, stage empty, texture released.
4. **Gap behaviour** → ordinals 1 and 2 are `NO_CAPTURE`; 472/473 ingest with pair `472→473` only; `471` is never a pair endpoint.
5. **Multiple pending ordinals** → three pending, out-of-order completions held until the head arrives, ingestion strictly 480, 481, 482 with pairs `480→481`, `481→482`, `orderViolations=0`, no deadlock.

---

## 13. Presentation-ownership matrix

```
R133_COMMIT_PREREQUISITE_MATRIX PASS satisfied=1 fgWsi=1 titleWsi=0 absentTitleWsi=1
    failedTitleWsi=1 failureAfterCommitFgWsi=1 failureAfterCommitTitleWsi=0
    estimatorIngestion=1 ownerImmutable=1 prerequisiteSatisfied=2 prerequisiteAbsent=2
```

| Case | Result |
|---|---|
| Prerequisite satisfied → FG commit | Exactly one FG SOURCE WSI, zero TITLE |
| Prerequisite absent | TITLE direct exactly once |
| Prerequisite fails before commit | TITLE direct exactly once |
| Failure after FG commit, before WSI | R131 retry contract holds; exactly one FG WSI; estimator rejected |
| Owner after WSI | Immutable — `R127SourceOwnershipMarkTitleDispatched` returns 0 |
| Late estimator capture | Never becomes a second SOURCE presentation |

---

## 14. Regression gate

`45 PASS / 0 FAIL`, exit 0 (`r133-evidence/deterministic.log`). The 40 pre-existing cases are unchanged and still PASS, including `R131_REVERSE_ACQUISITION_PRESENTATION_ORDER`, `R132_REVERSED_ESTIMATOR_ORDER`, `R132_ESTIMATOR_GAP`, `R132_LATE_CAPTURE`, `R132_STAGING_CAPACITY`, `R132_STAGE_RECLAMATION`, `R132_FRONTIER_RECLAIM`, `R132_MIXED_OWNERSHIP`, `R131_SOURCE_CAPACITY_PRESSURE`, `R131_80_SOURCE_CAPACITY_PRESSURE`, `R131_80_SOURCE_RECORD`, `R131_OWNERSHIP_LEDGER_LIFETIME`, `R131_UNRESOLVED_LEDGER_COLLISION`, `R131_LOOKUP_TRANSITION_RACE`, `R130_80_SOURCE_END_TO_END`, `R130_80_ACQUISITION_BACKLOG`, all nine `R130_ACTUAL_OBSERVER_CASE` rows and the three `R130_CAPTURE_FAILURE` rows.

No assertion was weakened. No test setup was changed to make a case pass. `R130_80_ACQUISITION_BACKLOG` keeps its 80 up-front enqueues and still reports `titleOwned=79 fgOwned=1 … pressure=ACQUISITION-TICKET-BACKPRESSURE`.

---

## 15. Fresh independent read-only review

A separate Max reviewer, given only the round's 12 questions and the file list, returned: Q1 SAFE, Q2 SAFE, Q3 SAFE, Q4 SAFE (bounded count, wall-clock unbounded if the title stops presenting), Q5 SAFE, Q6 SAFE, Q7 **RISK**, Q8 SAFE, Q9 SAFE, Q10 SAFE, Q11 SAFE, Q12 SAFE on ownership.

Findings and disposition:

| Finding | Disposition |
|---|---|
| **Q7** Presentation is gated on *commit*, not *completion*, and the pump samples the private texture on `r123Queue` with no `MTLSharedEvent` cross-queue fence. | **Accepted as the round's top residual risk.** In practice the SOURCE slot is at least one half-period after the present hook and the producer command buffer also carries the title's own frame, so the copy is complete long before the sample; but there is no formal fence. Recorded in §24 with the concrete mitigation. Not fixable inside the R123 freeze without adding an event to `R123DisplayItem`. |
| **Lock scope** `r133StageCommittedCapture` logged a rejection while the caller held `r127RuntimeLock`. | **Fixed.** The function now reports a reason code through an out-parameter and the caller logs after unlocking. Deterministic gate re-run: 45 PASS / 0 FAIL. |
| **Conflation** an FG-owned ordinal that expires is counted as an ordinary `NO_CAPTURE`. | **Accepted, diagnostic only.** `r132EstimatorFgMissingCaptureCount` now consults the capture record rather than the stage entry, so a genuinely missing image is still distinguished from a stage-capacity refusal. |
| **Test coverage** the 9-SOURCE and 80-SOURCE cases reach admission through the legacy staging path, and the R133 matrix stubs the commit hook. | **Accepted.** Those cases exist to prove ownership and capacity, which they do; the R133 matrix proves the prerequisite. The real blit and hook ordering are covered by the live run (`copyBeforePresent=14319`), not by the harness. |

---

## 16. Live validation

Environment: `r133-evidence/bottle-env-r133.json`, R126's validated continuous 30→60 slice plus `FGMETAL_R133_IN_CB_CAPTURE=1`; `FGMETAL_R115_CONTINUOUS` deliberately absent for the reason recorded in R132. Title: Elden Ring, offline, D3DMetal, sikarugir r29. Log: `r133-evidence/live-r133/observer.56958.log` (sha256 in `observer-sha256.txt`).

Two pump summaries matter, and they are not the same snapshot. The `COMPLETE` summary is emitted at a runtime **OFF** boundary, so it reads the totals up to that transition; the last `PERIODIC` summary is the true end of run.

`R123_PUMP_SUMMARY=COMPLETE reason=r127-requested-final-summary` (at the third OFF boundary):

```
sourceEnqueued=7297 sourcePresented=7297 droppedSource=0 presentFailures=0 drawableFailures=0
internalEnqueued=7289 internalPresented=52 internalResolved=52 deadlineMisses=7237 droppedInternal=7237
orderViolations=0 orderGaps=7244 sourcePeriodNs=34433000 slots=7349
```

Last `R127_RUNTIME_SUMMARY=PERIODIC` before the title exited (the end-of-run totals):

```
sourceEnqueued=8201 sourcePresented=8200 internalEnqueued=8193 internalPresented=52
deadlineMisses=8140 droppedInternal=8140 droppedSource=0 idleSlots=6598 currentDepth=2 maxDepth=4
orderViolations=0
```

Final `R127_RUNTIME_SUMMARY=PERIODIC` (capture and estimator facts):

```
estimatorFrontier=14319 estimatorReady=14209 estimatorNoCapture=110 estimatorLate=0
estimatorOrderViolations=0 estimatorStagePeak=3
pendingNow=0 pendingPeak=1 pendingGrace=1 pendingReady=14209 pendingExpired=0
captureCommitted=14319 captureComplete=14319 captureFailed=0
copyBeforePresent=14319 copyAfterPresent=0
prerequisiteSatisfied=14319 prerequisiteAbsent=0
```

Gate: `CAPTURE_READY 14209 > 0` ✓, `SOURCE enqueued 8201 > 0` ✓, `INTERNAL presented 52 > 0` ✓ → **OPEN**. The gate is evaluated on the end-of-run totals, which are also the more conservative reading: the later the run, the lower the INTERNAL fraction.

`sourceEnqueued=8201` against `sourcePresented=8200` is the run ending mid-pair, not a drop: the title exited between a SOURCE enqueue and its presentation. `droppedSource=0` confirms nothing was discarded.

Against R132's starved run (`estimatorReady=0`, `estimatorNoCapture=3491`, `sourceEnqueued=0`): capture admission is fixed. Against the product target: `internalPresented / sourcePresented = 52 / 8200 = 0.63 %`, so the runtime is **not** a 30→60 path.

Why midpoints miss: `R123_FG_PAIR … estimatorGpuExecNs≈19–24 ms, metalFXWallNs≈7–9 ms` against a half-period of 16.67 ms. Every midpoint is generated ~2–7 ms after its slot and is dropped with `reason=midpoint-not-ready-at-slot`. This is a latency-budget problem in the estimator/MetalFX path, not a capture-availability problem, and it is outside R133's scope.

---

## 17. Runtime transition test

```
requestId=1 epoch=2  from=OFF to=ON boundarySequence=5583  discardedInternal=0 discardedSource=0 queueDepthBefore=0 queueDepthAfter=0 staleCallbackRejections=0
requestId=3 epoch=5  from=OFF to=ON boundarySequence=12328 discardedInternal=0 discardedSource=0 queueDepthBefore=0 queueDepthAfter=0 staleCallbackRejections=2
requestId=5 epoch=8  from=OFF to=ON boundarySequence=12681 discardedInternal=0 discardedSource=0 queueDepthBefore=0 queueDepthAfter=0 staleCallbackRejections=4
requestId=7 epoch=11 from=OFF to=ON boundarySequence=13033 discardedInternal=0 discardedSource=0 queueDepthBefore=0 queueDepthAfter=0 staleCallbackRejections=6
```

Three complete cycles. No deadlock, no SOURCE loss or duplicate, no ownership or ordering violation. The `staleCallbackRejections` increments are exactly two per cycle: captures whose epoch no longer matches at the boundary are refused rather than applied, which is the intended behaviour and is bounded by the transition count, not by frame count.

R132's starvation-OFF timeout is therefore resolved: with an active pipeline the OFF transition completes at the next SOURCE boundary and does not need a timeout.

---

## 18. Failure injection

`printf '200\n' > /tmp/fgmetal-r127/fault-pairs` while generating:

```
R127_GENERATION_LOSS=ARMED requestId=1 pairs=200 reason=bounded-development-fault injection=drop-generated-internal-before-pump
R127_GENERATION_REFUSED requestId=1 pair=13937->13938 internalPresentId=855 remaining=199 action=drop-before-pump sourceFallback=preserved
R127_GENERATION_REFUSED requestId=1 pair=14114->14115 internalPresentId=900 remaining=154 action=drop-before-pump sourceFallback=preserved
R127_GENERATION_RECOVERED requestId=1 pair=14407->14408 internalPresentId=1055 refusedPairs=200 recovery=automatic
```

Through the whole injection: `droppedSource=0`, `orderViolations=0`, ownership unchanged. `estimatorNoCapture` rose from 16 to 110 and then stayed flat. The SOURCE fallback is preserved for every dropped midpoint.

**Measured:** 46 `REFUSED` lines appear for 200 refused pairs — `remaining` runs 199 down to 154 and then logging stops, so the 46 are the *first* 46 pairs, not a sample. `R127_GENERATION_REFUSED` classifies as `R127LogAnomaly` (verified by compiling `R127LatencyStats.h` and probing the exact live line), and the anomaly class prints only while its sample counter is ≤ 64. So the truncation is the R132 bounded-diagnostics policy doing its job, not lost refusals: 18 anomaly-class lines were printed earlier in the run (3 `ELDENRING_SOURCE_CAPTURE_PREFLIGHT=FAILED`, 7 `R127_GENERATION_CALLBACK=REJECTED`, 8 further anomaly lines carrying a marker further along the line), 18 + 46 = 64, and the budget closed exactly at the 47th refusal. The remaining 154 pairs are counted but not printed.

**Unresolved accounting detail.** The end-of-run summary reports `anomalyLogSamples=168`, `anomalyLogSuppressed=104`. Those imply 64 printed and 104 suppressed, which is consistent with the cap, but 168 attempts is 50 fewer than the 18 + 200 = 218 this run should have produced if every refused pair reached `logLine`. Two readings are possible and I did not distinguish them: the periodic summary sampled the counters at a moment before the last refusals were folded in, or 50 refusals took a path that bypasses `logLine`. Neither changes the verdict — `refusedPairs=200` and the pump counters are authoritative and agree — but the discrepancy is recorded rather than smoothed over. Resolving it needs a run with `FGMETAL_DIAG_SAMPLE=full`, where the cap is disabled and the printed count equals the attempted count.

The authoritative totals live in the `RECOVERED` line (`refusedPairs=200`) and the pump counters, which is why the injection verdict does not depend on per-pair log volume.

---

## 19. Soak

5 min 05 s of continuous ON (17:44:11 → 17:49:1x), 14 319 SOURCE frames, 8 200 SOURCE presentations (8 201 enqueued), zero SOURCE drops, zero present failures, zero drawable failures, zero ordering violations, no unbounded growth in any counter (`estimatorStagePeak=3`, `pendingPeak=1`, `maxDepth=4`).

The title exited on its own at the end of that window; the observer log ends with a clean `R127_GENERATION_RECOVERED` line and shows no fault on the observer side. A relaunch was attempted and the launcher did not come back with a window, so the 10–15 min requirement is **not met**. `D3DMETAL_FG_ALPHA_SOAK = PARTIAL`.

---

## 20. Display latency

`R127_LATENCY_CLOCK_CHECK`: `domain=CACurrentMediaTime-host-seconds-and-MTLDrawable-presentedTime-host-seconds`, `invalidHostTimePairs=0` — both ends are host seconds, so the numbers are comparable.

| Kind | n | min | median | p90 | p99 | max |
|---|---|---|---|---|---|---|
| present-hook → physical display | 6 951 | 17.47 ms | 36.95 ms | 47.00 ms | 54.10 ms | 96.09 ms |
| internal WSI dispatch → physical display | 47 | 15.88 ms | 22.96 ms | 30.63 ms | 33.44 ms | 33.44 ms |

In source periods the present-hook path is 1.073 / 1.365 / 1.571 (median / p90 / p99) against `sourcePeriodNs=34433000`. `source-availability-to-physical-display` reports `count=0`, so the availability-to-display interval is **not** measured.

---

## 21. Bounded diagnostics

381 log lines for 14 319 captures. End-of-run: `healthyLogSuppressed=240441`, `residualLogSuppressed=10764`, `anomalyLogSamples=168`, `anomalyLogSuppressed=104`, `unclassifiedLogSamples=42828`, `unclassifiedLogSuppressed=42764`. Per-event caps (`first-8` / `first-16` / `first-64`) held for every R133 tag: 9 `R133_CAPTURE_COPY_COMMITTED` lines for 14 319 commits, 8 `R133_LEGACY_CAPTURE=SKIPPED` lines. No per-frame logging. The R132 policy is preserved.

The one operational consequence is measured in §18: `R127_GENERATION_REFUSED` is anomaly-class, so the 64-sample anomaly budget is spent long before a 200-pair injection finishes, and only the first 46 refusals are printed. Per-pair detail therefore saturates on any fault campaign that needs more than 64 anomaly lines. The totals stay exact (`refusedPairs=200`, pump counters), so the verdict is unaffected; a campaign that needs the per-pair trace must run with `FGMETAL_DIAG_SAMPLE=full`, which bypasses the filter entirely. Raising the cap was rejected: it would trade a bounded log for per-frame logging, which is the thing this policy exists to prevent.

---

## 22. Frozen work

`SourcePresentOrdinal` semantics, estimator mathematics, MetalFX mathematics, R131 reservation/terminality, R120 pacing, R125 non-owning instrumentation, R126 self-present queue gate and drawable pool depth are unchanged. `R123DisplayPump.{h,m}` and `MetalFXFrameInterpolationProvider.{h,m}` are byte-identical to R132. R102 deferred-B stays disabled; no DXMT, no 60→120, no visual-quality or depth work, no EAC or online work.

---

## 23. Fact, interpretation, hypothesis

**Measured facts.** `copyBeforePresent=14319`, `copyAfterPresent=0`; `prerequisiteAbsent=0`; `estimatorReady=14209` vs `estimatorNoCapture=110`; `pendingPeak=1`, `pendingExpired=0`; `internalPresented=52` of `sourcePresented=8200`; `droppedSource=0`; `orderViolations=0`; 45/45 deterministic PASS; three transition cycles clean; 200 injected losses recovered automatically.

**Interpretation.** The R132 blocker was scheduling, not admission. Moving the same bounded copy into the producer command buffer makes the prerequisite available on 100 % of frames without touching ownership, reservations, the pump or the estimator. The estimator now sees almost every SOURCE; the remaining gap is a latency budget, not a policy.

**Hypothesis (untested).** If the estimator's GPU prep (`≈19–24 ms`) were reduced below the 16.67 ms half-period, INTERNAL presentation would rise toward R126's 48.8 % without any further change to R133. This is the next round's subject and is not claimed here.

---

## 24. Known limitations and residual risks

1. **No cross-queue fence.** The pump samples the private capture texture on `r123Queue`; the blit was encoded on the title's queue. Ordering currently rests on the half-period gap plus Metal's resource hazard tracking, not on an explicit `MTLSharedEvent`. Mitigation: signal an event at the end of the producer command buffer, carry it on `R123DisplayItem`, and `encodeWaitForEvent` before the pump's sampling blit. This is the single change most likely to matter in the field.
2. **One-frame loss on a post-commit copy failure.** If the blit fails after FG ownership was committed, that SOURCE frame is presented from an unwritten private texture. `copyFailed` withdraws the prerequisite for every later frame immediately, so the loss is bounded to one frame per failure. Observed live: zero.
3. **Pending is bounded in count, not in wall clock.** If the title stops presenting while an ordinal is pending, the entry and its private texture persist until presentation resumes. Bounded memory (≤4 textures), unbounded time.
4. **Soak is 5 minutes, not 10–15.** See §19.
5. **Availability-to-display latency is not measured** (`count=0`).
6. **Anomaly log accounting does not reconcile.** `anomalyLogSamples=168` versus the 218 this run should have produced (§18). Verdict-neutral — the refusal count and pump counters agree — but unresolved until a `FGMETAL_DIAG_SAMPLE=full` run.

---

## 25. Exact remaining blocker and smallest next step

**Remaining blocker.** INTERNAL presentation is 0.63 %. Every midpoint is generated after its slot: `estimatorGpuExecNs≈19–24 ms` + `metalFXWallNs≈7–9 ms` against a 16.67 ms half-period, so `takeForSlotAtNs` finds the head INTERNAL not ready and drops it with `reason=midpoint-not-ready-at-slot`.

**Smallest next step.** Measure the estimator's per-pair GPU interval and the MetalFX wall interval against the slot they must hit, over a bounded run, and decide whether the fix is (a) reducing estimator prep, (b) allowing the midpoint to land on the following slot without breaking cadence, or (c) both. Do not change capture availability, ownership or the prerequisite: those are measured correct on 14 319 frames.

---

## 26. Evidence index

| Path | Contents |
|---|---|
| `r133-evidence/R133-REPORT.md` | This report. |
| `r133-evidence/deterministic.log` | 45-case deterministic gate, exit 0. |
| `r133-evidence/deterministic/` | Immutable source snapshot, `source-sha256.txt`, `source-commit.txt`, binary. |
| `r133-evidence/live-build.log` | Live dylib build, hashes of every input. |
| `r133-evidence/live-build/libfgmetal_r127_alpha_runtime.dylib` | The binary that produced the live log. |
| `r133-evidence/live-r133/observer.56958.log` | Live observer log, 381 lines. |
| `r133-evidence/live-r133/observer-sha256.txt` | Its hash. |
| `r133-evidence/live-r133/highball-eldenring.log` | Highball's log for the same session. |
| `r133-evidence/bottle-env-r133.json` | Live manifest with rationale. |
| `r133-evidence/launch-preparation/r133-live-01/` | Bottle before/after bytes, hashes, staged environment. |
| `r133-evidence/tools/r133_bottle_env.py` | Reversible staging tool. |
| `r133-evidence/highball-bottle-before.json` | Bottle as found before staging. |
