# FG-METAL — R115 Re-armable Continuous Title Report

**Primary verdict:** `REARMABLE_REAL_TITLE_A_TO_M_TO_B = VALIDATED (CONSTANT_DEPTH_PROTOTYPE)`

**Not claimed:** `BOUNDED_CONTINUOUS_2X_TITLE_FG` — see §17. The lifecycle is re-armable and stable, but the measured cadence is one interpolated frame per ~3 source frames with a ~100 ms retained-B hold. The pipeline is not operating at 2×; it currently costs more wall time than the frames it adds.

Two bounded runs in two game processes produced **60 accepted A→M→B tuples** with zero SOURCE leaks, zero stale callbacks, zero duplicate dispatches, and a flat latency profile over 50 consecutive generations.

---

## 1. WorkBuddy/model execution summary

- Harness: WorkBuddy AI. Orchestrator: DeepSeek V4.1 Flash (Max) — repository reading, lifecycle design, implementation, build, live-run control, evidence analysis, this report.
- Two independent reviewer subagents (high-reasoning tier) audited the implementation before the live runs. The first audit found **2 blockers and 5 majors** in the first implementation; the second audit confirmed 5 of 7 fixes and found 2 still broken plus one new latent issue of the same class. All were fixed and rebuilt before any live run. No other models were used and no fallback occurred.
- No agent army: one implementation owner (orchestrator), one reviewer at a time, orchestrator as runtime/evidence owner.
- The audit materially changed the outcome: without it, one defect (§4) would have rebuilt the MetalFX provider inside every retained-B hold, and another (§13) would have converted the known deadlock into a permanently dropped frame.
- Two live runs, both bounded, both driven by the same operator action (physical camera rotation). No synthetic gameplay input was sent in either run.

## 2. Reusable tuple state machine

R114 was terminal after one tuple: `r103FrontierSpent` stayed set and the latch stayed `CONSUMED`, so nothing could reserve again. R115 adds the smallest explicit lifecycle that returns the *same* accepted seams to `IDLE`:

```
IDLE → ARMED → PAIR_RESERVED → FRONTIER_CLOSING → B_DEFERRED
     → PROCESSING → M_PRESENTING → B_FORWARDING → RESETTING → IDLE
any state → FALLBACK → RESETTING → IDLE
```

No speculative states. Every transition is driven by an existing R114 seam, not by new scheduling:

| Transition | Driven by |
|---|---|
| IDLE → ARMED | `r114MaybeArm` (game thread, after acquisition lease release) |
| ARMED → PAIR_RESERVED | `r115ReserveAtArmLocked` (same call, under `stateLock`) |
| PAIR_RESERVED → FRONTIER_CLOSING | `r103CloseFrontierAfterProducerCommit` (B's producer commit) |
| FRONTIER_CLOSING → B_DEFERRED | `r102TryDeferSourcePresent` (B's present hook) |
| B_DEFERRED → PROCESSING | gate evaluation for the reserved pair |
| PROCESSING → M_PRESENTING | `r102AttemptInternalDispatch` (INTERNAL WSI dispatch) |
| M_PRESENTING → B_FORWARDING | `r102InternalPresented` (`reachedDisplay=1`) |
| B_FORWARDING → RESETTING → IDLE | end of `r102ForwardSourceTicket` (frontier reopened) |
| any → FALLBACK | gate rejection, seed miss, frontier recovery |

`r115ResetTuple()` is the single place that implements the reset contract. It retires, under one lock hold: pair identity (`r114ReservedPairA/B`), SOURCE A/B references (`r110TupleSourceBSequence`, `r110ExpectedA/BSourceWsiPresentId/Order`, `r110InternalWsiPresentId/Order`), the retained drawable and producer (via the ticket), the `InternalPresentId` association and candidate M (`r102ClearPendingInternalLocked`), the ticket pointer, gate state (`r110QualityReviewed/Rejected/WaitStarted`), and frontier ownership (`r103FrontierSpent = NO`, state `Open`, identity cleared). It then advances the generation and broadcasts `presentationReadyCondition` so blocked acquisitions and dispatches re-test immediately.

The reset refuses to run while a deferred ticket is live (§7); the ticket's own terminal path owns the reset in that case.

## 3. Generation / stale-callback protection

A monotonically increasing `r115Generation` is captured when a tuple arms, stamped on the ticket at publication, and advanced only inside the reset critical section that requires `r102DeferredTicket == nil`.

**Honest statement of what enforces what.** Pointer identity (`r102TicketIsCurrentLocked`) is the load-bearing guard: every callback tests that the ticket it carries is still the live ticket. The generation nonce is defence-in-depth and its only *reachable* use is rejecting a late M at the gate (`gateGenerationCurrent`, folded into `fastEligible`). A generation mismatch on a live ticket is structurally impossible because the generation cannot advance while a ticket exists — the reviewers established this and the live runs agree: **0 mismatches observed across 111 WSI dispatches and 61 forwards**.

This is reported as an invariant that held, not as measured evidence of stale-callback defence. The report does not claim the nonce caught anything.

## 4. Automatic re-arm behavior

Re-arming requires no process restart and no external trigger after the first start file. At each acquisition the observer re-tests, in-process: start requested, budget left, world scene confirmed, capture enabled, no deferred ticket, frontier open and unspent, zero operations in flight, internal slot empty, gate flags clear, and (after a rejection) an 8-frame cooldown.

Two design points worth recording, both found during review or live:

- **Provider warm-up.** R115 captures only the reserved pair, so no estimator runs before the first reservation and the provider would never warm. A one-off warm-up pair is captured at start-up (run 1: `5677..5678`, `R115_PROVIDER_WARMUP=ARMED reason=provider-not-warm`), after which arming proceeds.
- **Provider teardown.** The inherited `R108_SOURCE_SEQUENCE_GAP` handler tore down `realFrameProvider` on any sequence gap — and R115 gaps by design. That would have rebuilt MetalFX once per tuple *inside* the B hold. Continuous mode now keeps the provider and drops only the history textures. This was the single largest correction the audit produced.

Re-arm spacing is naturally bounded by the hold itself: the game thread is blocked while B is retained, so tuples cannot arrive faster than one per hold.

## 5. Physical-motion test setup

- Fresh Highball process, engine `x64-sikarugir10.0_6-r29`, executable `/Users/nima/Games/Elden Ring/Game/eldenring.exe`.
- Observer injected through the Games bottle environment; both runs verified `R115_CONTINUOUS=ENABLED`, `mode=R114-inprocess-target-latch-title-presentation presentation=1`, and the exact layer/device (`deviceName=Apple M3`, `registryID=4294968554`, `1472×956`, `framebufferOnly` cleared).
- Run 1: pid `77553`, layer `0x600001400ea0`, device `0x7fbdeb04f400`.
- Run 2: pid `82126`, layer `0x600002078780`, device `0x7f8086056e00`.
- The operator confirmed the open world, then performed a steady physical camera rotation with the touchpad. **No synthetic gameplay input was sent in either run.** Motion is evidenced by the accepted R110 estimator field itself (e.g. `activeFraction=0.918`, `maxMagnitude=8.148` on the R114 pair) plus the operator's attested action.
- Controller wrote `r112-world-scene-confirmed = VISIBLE_WORLD` and `capture-condition = CAMERA`, then the start file `r115-continuous-start`. It never named a pair.

## 6. 10-tuple bounded run

Process `77553`. Target: 10 accepted tuples.

```
R115_CONTINUOUS_RUN_READY generation=1 firstPair=5710->5711 acceptedTarget=10
R115_CONTINUOUS_RUN=COMPLETE attempts=19 accepted=10 fallback=9
  sourceLeaksWhileClosed=0 staleCallbacks=0 duplicateMDispatch=0 resets=19
  acceptanceRatio=0.526
```

19 candidate tuples were attempted; 10 were accepted and displayed, 9 were rejected by the frozen gate. Every rejection was `motion-below-frozen-pair-derived-threshold` — the operator's rotation had slowed on those pairs. The gate was not modified or relaxed.

## 7. Accepted tuple results

All ten accepted tuples, run 1:

| # | pair | A wsi | M wsi | B wsi | InternalPresentId | contiguous |
|---:|---|---:|---:|---:|---:|---|
| 1 | 5710→5711 | 5710 | 5711 | 5712 | 1 | yes |
| 2 | 5713→5714 | 5714 | 5715 | 5716 | 2 | yes |
| 3 | 5716→5717 | 5718 | 5719 | 5720 | 3 | yes |
| 4 | 5719→5720 | 5722 | 5723 | 5724 | 4 | yes |
| 5 | 5722→5723 | 5726 | 5727 | 5728 | 5 | yes |
| 6 | 5725→5726 | 5730 | 5731 | 5732 | 6 | yes |
| 7 | 5728→5729 | 5734 | 5735 | 5736 | 7 | yes |
| 8 | 5741→5742 | 5748 | 5749 | 5750 | 9 | yes |
| 9 | 5744→5745 | 5752 | 5753 | 5754 | 10 | yes |
| 10 | 5827→5828 | 5836 | 5837 | 5838 | 19 | yes |

In every row `B = M + 1 = A + 2`, i.e. exactly one INTERNAL dispatch between A and B and no SOURCE dispatch between. `InternalPresentId` is unique per tuple; the gaps (8, 11–18) are rejected attempts that produced an estimator result but never dispatched M.

## 8. Fallback tuple results

| Run | Attempts | Accepted | Fallback | Reasons |
|---|---:|---:|---:|---|
| 1 | 19 | 10 | 9 | 9 × `motion-below-frozen-pair-derived-threshold` |
| 2 | 62 | 50 | 12 | 11 × `motion-below-frozen-pair-derived-threshold`, 1 × `source-frontier-recovery-bound` |

Fallback order held in every case: SOURCE A → retained SOURCE B → next SOURCE. No INTERNAL WSI dispatch was emitted for any rejected pair (verified by set difference over all failed pairs: `failed pairs that still got an INTERNAL dispatch: []`). No ticket leaked and no frontier was left stuck.

The one `source-frontier-recovery-bound` fallback (run 2, generation 31, pair `4334→4335`) is a genuine defect and is quantified in §10.

## 9. A/M/B ordering

Across both runs:

- `R110_TITLE_WSI_SEQUENCE=VALIDATED` = 60 (10 + 50), `REJECTED` = 0.
- All 60 satisfied `noSourceDispatchBetween=1`.
- `InternalPresentId` unique across each run (50/50 in run 2); `WsiPresentId` unique across all 111 dispatches in run 2.
- No INTERNAL dispatch carried an `AppFrameId` (0 occurrences); every SOURCE dispatch carried one.

Representative accepted tuple (run 1, generation 19):

```
R103_SOURCE_FRONTIER=CLOSING_FOR_PAIR pair=5827->5828
R103_SOURCE_FRONTIER=CLOSED operationsInFlight=0
R102_SOURCE_TICKET=DEFERRED appFrameId=5828 pair=5827->5828 wsiPresentId=UNALLOCATED
R110_FAST_PRESENT_ELIGIBILITY=ACCEPTED pair=5827->5828 internalPresentId=19 activeFraction≈0.92
R102_WSI_DISPATCH origin=INTERNAL internalPresentId=19 wsiPresentId=5837 appFrameId=NONE
R102_INTERNAL_PRESENTED_CALLBACK internalPresentId=19 wsiPresentId=5837 reachedDisplay=1
R102_WSI_DISPATCH origin=SOURCE appFrameId=5828 wsiPresentId=5838
R103_SOURCE_FRONTIER=OPEN result=FORWARDED
```

## 10. Latency / pacing measurements

All values in ms. `B hold` = B forward dispatch − B original present-hook entry.

| Stage | Run 1 (10 tuples) | Run 2 (50 tuples) |
|---|---:|---:|
| Estimator (A→B) | 20.00 (17.16–27.19) | 18.04 (16.72–19.94) |
| MetalFX transition | 4.55 (4.33–4.93) | 4.40 (4.25–4.81) |
| Gate (frozen R110 metrics) | 31.41 (28.94–36.23) | 31.46 (27.80–37.25) |
| Producer complete → M WSI | 67.79 (60.19–76.47) | 69.43 (59.71–77.61) |
| Producer complete → M display | 105.80 (93.81–130.39) | 97.29 (72.84–113.81) |
| M display → B dispatch | 0.04 (0.03–0.07) | 0.04 (0.02–0.08) |
| **B hold** | **108.53 (94.78–131.24)** | **99.95 (75.28–124.04)** |
| Tuple total (arm → reopen) | 143.59 | 133.51 |

Run 2 distribution: min 75.3, p50 101.7, p90 110.2, max 124.0.

**Against the R114 baseline:** 409.9 ms → 100.0 ms mean, a **4.1× reduction**, and no longer backlog-driven. R114's hold was almost entirely capture-queue lag (the pipeline had fallen ~11 frames behind, blocking frame 2751's `nextDrawable` for 416 ms). R115's hold is the actual cost of producing and displaying M.

**Composition of the ~100 ms.** ≈69 ms to produce M and dispatch it (estimator 18 + MetalFX 4.4 + gate 31.5 + capture/copy and queue ≈15), ≈28 ms for M to reach the display, 0.04 ms to forward B.

**Pacing correctness.** The hold is flat, not accumulating: linear fit over 50 consecutive generations gives **−0.12 ms/tuple** (total drift −6.0 ms); first-10 mean 104.8 ms, last-10 mean 99.7 ms. There is no burst presentation, no queued SOURCE accumulation and no alternating-interval pattern. Median source frame interval across the run was 16.69 ms (p99 89 ms, consistent with the hold).

**The one non-bounded path.** Run 2, generation 31: B committed (`reserveToBCommitNs=16.85 ms`) but **never presented** — zero `R102_SOURCE_TICKET=DEFERRED` lines for sequence 4335. The frontier therefore stayed closed until the close-recovery bound fired, producing a **4,211 ms** stall (`seq 4335 → 4336`). This is 1 of 62 attempts (1.6 %) and it is the only unbounded event in either run. The recovery itself worked correctly (frontier reopened, tuple reset, run continued to 50), but a 4.2 s freeze is not acceptable product behaviour. Fix identified in §19.

A 1,196 ms gap at seq 4824 in run 2 has no R115 events nearby and occurs after the run completed; it is not attributable to this code.

## 11. Frontier / leak accounting

| Class | Run 1 | Run 2 |
|---|---:|---:|
| `SOURCE_LEAK_WHILE_FRONTIER_CLOSED` | **0** | **0** |
| `MISSED_TARGET_ADMISSION` | 0 | 0 |
| `R115_RESERVATION_WATCHDOG=EXPIRED` | 0 | 0 |
| `R115_SEED_MISSING` | 0 | 0 |
| Frontier recovery events | 0 | 1 (§10) |
| `PRE_FRONTIER_ADMISSION` | normal pre-reservation traffic, counted separately | same |

Leaks are now judged per generation (`r103FutureSourceLeaks − r115Timing.leaksAtArm`) so a single leak in tuple N cannot mark every later tuple failed; the cumulative counter still gates the run verdict, which requires zero for the whole run. Both runs satisfy the cumulative requirement.

## 12. Callback / accounting safety

| Assertion | Result |
|---|---|
| SOURCE `AppFrameId` strictly SOURCE-only | pass — 0 INTERNAL dispatches carried one |
| INTERNAL has no `AppFrameId` | pass |
| Unique `InternalPresentId` | pass — 50/50 run 2 |
| Unique `WsiPresentId` | pass — 111/111 run 2 |
| No duplicate B forwarding | pass — 0 |
| No double M dispatch | pass — 0 |
| No stale callback crossing a generation | pass — 0, see §3 for what this does and does not prove |
| Exactly-once tuple terminal state | pass — 62 resets for 62 attempts, no double reset |

The reset/publish race the reviewer found (a ticket published between the reset's probe and its critical section would be orphaned, and pointer identity would then reject it forever) is closed by a second `r102DeferredTicket` re-check inside the critical section, returning `R115_RESET_DEFERRED`. It did not trigger in either run.

## 13. Deadlock regression

R114 attempt 9 froze the game because forwarding retained B synchronously inside `didPresentAtTime:` re-enters `CAMetalLayer`'s present lock. R115 keeps the async forward and makes the regression structural rather than incidental:

- `r115InsidePresentedCallback` is a thread-local set for the whole body of `r102InternalPresented`.
- `r102ForwardSourceTicket` tests it as its **first statement**, before `stateLock` and before claiming `ticket.state = ForwardingSource`. If set, it logs `R115_DEADLOCK_REGRESSION=HOPPED` and re-dispatches asynchronously.
- The ordering matters: claiming the ticket state before the hop would make the re-entrant call fail `state != ForwardingSource` and drop B permanently, with the frontier stuck in `Releasing` — a state `r103RecoverFrontier` explicitly refuses to recover. The first audit caught exactly this in the first implementation.
- The `!reachedDisplay` fallback also hops asynchronously instead of forwarding inline.

Observed: **0 hops, 0 deadlocks, 61 forwards, all completing.** `M display → B dispatch` = 0.04 ms mean, confirming the hop itself is not on the critical path.

## 14. Game stability

| Check | Run 1 | Run 2 |
|---|---|---|
| Game responsive throughout | yes | yes |
| Persistent black screen | none | none |
| Layer starvation | none | none |
| Stuck key/input | none | none |
| Wine/Highball crash | none | none |
| SOURCE frames after run completed | 5,509 | 1,692 at analysis time; process still rendering at seq 7913 |
| Post-run frame rate | ~59 fps | ~59 fps (median interval 16.69 ms) |

After the run each process returned to ordinary SOURCE presentation with no interpolation, no frontier and no capture, and continued rendering for thousands of frames.

## 15. Optional 50-tuple run

Executed because the 10-tuple run passed cleanly. Process `82126`.

```
R115_CONTINUOUS_RUN=COMPLETE attempts=62 accepted=50 fallback=12
  sourceLeaksWhileClosed=0 staleCallbacks=0 duplicateMDispatch=0
  resets=62 acceptanceRatio=0.806 postRunStartSequence=4484
```

- 50 consecutive accepted generations, 50 unique `InternalPresentId`, all contiguous A→M→B.
- Hold 75.3–124.0 ms, p50 101.7, p90 110.2, mean 100.0; **slope −0.12 ms/tuple**, no monotonic growth.
- 11 gate rejections (motion below the frozen threshold), 1 recovery-bound fallback (§10).
- 0 leaks, 0 stale callbacks, 0 duplicate dispatches, 0 deadlocks.
- Game stable and still rendering after the run.

The extended run therefore confirms stable repeated operation. It does **not** confirm 2× (§17).

## 16. Re-armable verdict

`REARMABLE_REAL_TITLE_A_TO_M_TO_B = VALIDATED (CONSTANT_DEPTH_PROTOTYPE)`

All ten conditions hold:

1. One process — two separate single-process runs, no restart within a run.
2. Multiple tuple generations — 10 and 50.
3. ≥5 accepted/displayed INTERNAL tuples — 10 and 50.
4. Correct A→M→B order for each accepted tuple — 60/60 `VALIDATED`, `noSourceDispatchBetween=1`.
5. Safe fallback for rejected tuples — 21 fallbacks, correct order, no INTERNAL WSI, no stuck frontier, no leaked ticket.
6. Zero SOURCE leaks while the frontier was closed — 0 and 0.
7. No stale callback crossing generations — 0.
8. No duplicate INTERNAL/B dispatch — 0.
9. Bounded B-hold latency — 75–131 ms on the accepted path, flat over 50 tuples. **Caveat:** one fallback path produced a 4.2 s stall (§10); that path is not bounded.
10. Game remains stable — pass in both runs.

## 17. Bounded continuous-2× verdict

`BOUNDED_CONTINUOUS_2X_TITLE_FG = NOT VALIDATED`

This is a deliberate non-claim, and the arithmetic is unambiguous:

- Source frame interval: 16.69 ms median.
- Cost per tuple: one M produced, retained B held ~100 ms.
- Accepted tuples arrived roughly every 3 source frames (~50 ms).

So each interpolation adds one displayed frame while blocking the game for ~100 ms — roughly six frame intervals. During interpolation the game runs at about one third speed. The pipeline is re-armable and stable, but it is **slowing the game down** rather than doubling its frame rate. Claiming 2× on this evidence would be wrong.

The blocker is structural, not a tuning issue: M is produced *inside* B's hold, so its production latency is charged directly to the display stream. 2× becomes arithmetically possible only when that latency is below one frame interval.

## 18. Alpha readiness

**Not alpha ready.** R115 removes the "one tuple per process" blocker and replaces it with a measured, bounded, flat-latency lifecycle. Remaining blockers:

- Pacing: ~100 ms hold per tuple; not 2× (§17).
- Latency composition: the gate's synchronous GPU round-trip is 31.5 ms of every hold.
- One unbounded fallback path: 4.2 s stall when a committed frame is never presented (§10).
- Constant depth: `CONSTANT_DEPTH_PROTOTYPE` unchanged; visual quality was not assessed beyond R114's `NONE_OBVIOUS` at modest motion.
- MetalFX temporal history carries across tuples (see §19 note).
- No user enable/disable control and no graceful in-game recovery UI.

## 19. Exact next PRODUCT blocker

**Pipeline the M production instead of charging it to B's hold.** Concretely, in order:

1. **Remove the gate's synchronous round-trip (31.5 ms/tuple, ~31 % of the hold).** `r110CollectFastMetrics` commits a compute pass, calls `waitUntilCompleted`, then aggregates ~5.5 k groups on the CPU, all on the critical path. It can be encoded into the MetalFX command buffer or dispatched asynchronously and joined only before M dispatch. This is the largest single removable wait and was left untouched in R115 because the gate is frozen.
2. **Account for M's display latency (~28 ms).** M is dispatched from a non-game thread while the game thread is blocked; the layer's transaction appears to wait for a later vsync than R114's 20 ms. This needs measurement before it is optimised.
3. **Then restructure:** produce M for pair N while frame N+1 is being presented, so B never needs to be held at all. Only that removes the ~100 ms charge and makes 2× arithmetically reachable.
4. **Bound the committed-but-never-presented case.** A B that commits but never presents currently stalls the frontier until the 4 s recovery bound. The frontier being `Closed` with no deferred ticket for more than a few frame intervals is detectable; a short watchdog (e.g. 150 ms) would cut the worst case from 4.2 s to ~0.15 s.

Also recorded, not fixed by design: **MetalFX temporal history is not reset between tuples.** The provider primes history on the first transition after a reset and **returns no internal frame** on that transition (`MetalFXFrameInterpolationProvider.m:215-220`), so resetting per tuple would make every tuple fall back. R115 therefore keeps history across tuples and accepts a stale-history quality risk (ghosting) rather than a guaranteed zero. A proper history-reset path that still yields an M per tuple is a later quality blocker, not a stability one.

### Evidence

- Run 1 (10 tuples): `experiments/metalfx_frame_interpolator_R93/evidence/r115-continuous/run-01-10tuples/observer.log`, controller `../r115-controller.json`
- Run 2 (50 tuples): `experiments/metalfx_frame_interpolator_R93/evidence/r115-continuous/run-02-50tuples/observer.log`, `controller.json`
- Implementation: `experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/observer-title-inprocess-r115-continuous.m` (sha256 `b28610da…` at review; rebuilt after review fixes to dylib `ed409caa…`)
- Build: `experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/build_r115_tools.sh` → `libfgmetal_r115_continuous_probe.dylib`
- Runner: `experiments/metalfx_frame_interpolator_R93/evidence/r115-continuous/run_r115_continuous.py`
- Bottle environment used: Games bottle, `FGMETAL_R115_CONTINUOUS=1`, `FGMETAL_R115_MAX_TUPLES=10` then `50`; pre-R115 backup at `/tmp/bottle.json.r115-backup`. The bottle was restored to its pre-R115 state after the runs, so no R115 probe remains injected.
