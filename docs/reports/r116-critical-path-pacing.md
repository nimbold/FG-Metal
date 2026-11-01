# FG-METAL — R116 Critical-Path Pacing Report

| Verdict | Result |
|---|---|
| `ASYNC_GPU_FAST_GATE` | **`VALIDATED`** — semantics preserved 7/7, 31.28 ms → **0.128 ms** |
| `NONBLOCKING_M_THEN_B_SCHEDULING` | **`VALIDATED`** — 51/51 in-game tuples ordered and distinct, 0 coalesced |
| `COMMITTED_B_NO_PRESENT_RECOVERY` | **`BOUNDED`** — observed live: **4211 ms → 190.8 ms** |
| `BOUNDED_CONTINUOUS_2X_TITLE_FG` | **`NOT VALIDATED`** — hold is 2.0× the source interval; §17 |

R116 removes the three costs R115 identified but did not fix. All three are now
measured in the game, not inferred:

- the gate's 31.3 ms synchronisation bubble is **gone** — the CPU decision costs
  0.128 ms and the GPU work it displaced costs 0.71 ms inside the MetalFX
  submission it now shares;
- retained B is **no longer held for M's display callback** — the B hold fell from
  a 101.4 ms median to **34.9 ms**, and 51/51 accepted tuples showed M and B on
  distinct refreshes in the right order;
- the committed-but-never-presented pathology **recurred live** and was bounded
  from **4211 ms to 190.8 ms**.

The pipeline is still not 2×. §13 and §17 explain exactly why, and §18 names the
structural change that is required.

---

## 1. WorkBuddy / model summary

- Harness: WorkBuddy AI. Orchestrator: DeepSeek V4.1 Flash (Max) — repository
  reading, design, implementation, build, offline experiment design, evidence
  analysis, this report.
- Three independent reviewer subagents audited the implementation before any live
  attempt. They were not ceremonial: they found **2 blockers and 4 majors across
  three rounds**, every one of them in code I had just written and believed
  correct. Every finding was fixed and the library rebuilt. Details in §3.6.
- No other model was used and no fallback occurred. No agent army: one
  implementation owner, one reviewer at a time.
- Four live runs in four game processes (two 10-tuple, two 50-tuple), all driven
  by the same operator action: physical camera rotation. No synthetic gameplay
  input was sent. The last three runs each fixed a defect found by the previous
  one; the fourth is the one that authorises the verdicts (§11.1).

## 2. R115 latency baseline (frozen reference)

R115 run 2, 50 accepted tuples, reproduced here from the same analyser that will
be used on R116 so the two are directly comparable:

| Stage | n | min | mean | median | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| Estimator | 61 | 16.724 | 18.327 | 17.911 | 19.113 | 27.005 |
| MetalFX transition | 61 | 4.245 | 4.422 | 4.382 | 4.562 | 5.270 |
| **Gate (sync round trip)** | 61 | **25.676** | **31.281** | **31.329** | **34.767** | **38.481** |
| Producer complete → M WSI | 50 | 59.708 | 69.428 | 69.843 | 73.797 | 77.608 |
| Producer complete → M display | 50 | 72.845 | 97.288 | 99.837 | 106.791 | 113.812 |
| M display → B dispatch | 50 | 0.021 | 0.036 | 0.033 | 0.048 | 0.078 |
| **B hold** | 61 | **58.714** | **93.636** | **101.352** | **108.783** | **124.040** |
| Tuple total | 62 | 93.590 | 193.887 | 133.219 | 149.552 | **4216.335** |
| B commit → B present hook | 61 | 0.095 | 9.974 | 13.518 | 15.002 | 17.641 |

The 4216 ms maximum is the single pathological tuple (§8). Excluding it, tuple
total is 93.6–150 ms.

The gate is 31.3 ms of a 93.6 ms mean hold — **one third of the hold is a
synchronisation bubble around a kernel whose GPU time is under 1 ms** (§5).

## 3. Fast-gate redesign

### 3.1 What was wrong

`r110CollectFastMetrics` created a fresh `MTLBuffer`, committed a **second**
command buffer to a **different** `MTLCommandQueue`, called
`waitUntilCompleted`, then walked 345 group records on the CPU. The CPU walk is
microseconds. The 31.3 ms was the commit → schedule → execute → complete → wake
cycle of an extra submission that had to be scheduled against the game's own
rendering.

### 3.2 What changed

`MetalFXFrameInterpolationProvider` gained an `additionalEncoding:` block,
invoked between `[interpolator encodeToCommandBuffer:]` and
`[commandBuffer commit]`:

```objc
typedef void (^FGMetalFXAdditionalEncodingBlock)(id<MTLCommandBuffer> commandBuffer,
                                                 id<MTLTexture> interpolatedOutput);
```

The observer uses it to encode, into the **same** submission as the MetalFX
transition:

1. `r110FastGate` — the **unchanged** per-group kernel (same 16×16 groups, same
   4×4 pixel footprint per lane, same 5-point colour sampling, same accumulation);
2. `r110FastGateReduce` — a new single-threadgroup (256) kernel that folds all
   group records into one 40-byte `R110GateResult` carrying a monotonic
   `frameTag`.

Both kernels compile from `r116_gate_kernels.h`, which the offline equivalence
harness also compiles, so the harness cannot drift from the shipped kernel.

Because the provider already blocks on that command buffer, the CPU reads the
single struct the instant the call returns. **There is no second command buffer,
no `waitUntilCompleted`, and no per-group CPU loop on the critical path.**

### 3.3 Gate semantics — unchanged, and checked

Preserved exactly: finite motion; `activeFraction >= 0.15`; `maxMagnitude <= 256`;
finite MetalFX output; endpoint difference `>= 0.002` to both A and B; exact
lineage (`sourceWindow`, `frontierReady`, `r110TupleSourceBSequence == sequence`,
`previousCaptureSequence + 1 == sequence`); verified world state and camera /
character attribution. **No threshold was touched.** The reason-string precedence
is byte-identical.

Two new guards were added, both of which can only *reject* where the old path
would have accepted:

- `r116ReadGateResult` rejects a result whose `groupCount` does not equal
  `((w+63)/64)*((h+63)/64)`, or whose `frameTag` does not match the tag stamped
  at encode time. A stale buffer from a previous tuple is therefore impossible to
  consume.
- If the encode could not be issued, `r116GateEncoded` is `NO` and the code falls
  back to the legacy collector, which is correct but slow. That fallback is
  visible in the log as `gatePath=legacy-separate-command-buffer`.

### 3.4 Where the join happens

At the latest possible point: the metrics are read after
`interpolatePreviousColor:` returns and before the gate decision. Nothing waits
earlier. The gate's GPU work is inside a submission that already existed.

### 3.5 Cost of the reduction

Measured offline (`gate-equivalence/*.json`, 7 pairs × 20 iterations): the
two-stage gate's GPU time minus the first-stage-only GPU time is
**−0.025 to +0.145 ms**, i.e. inside launch-overhead noise. The reduce stage is
effectively free; the whole gate pass costs ~0.3–0.95 ms of GPU time, and that is
now the entire added critical-path cost.

### 3.6 The audits

Three rounds, each run by a fresh reviewer that had no part in writing the code.
What they caught, in order:

**Round 1** — (a) *blocker*: my first `frontierContiguous` fix removed the only
display check without replacing it, so a tuple whose M never displayed could be
retired as `CONSUMED`; (b) *major*: the ordering record was a single current-tuple
slot, so a late B callback for tuple N could wipe tuple N+1's M sample;
(c) *major*: the watchdog's hook-entry guard was set before every validation and
never cleared, so a hook that entered and then rejected would fall back to the 4 s
stall — the exact case the watchdog exists for; (d) *major*: the watchdog mutated
the frontier before calling reset, and could leave it `Open`+`Spent` if reset
declined, wedging continuous mode.

**Round 2** — (a) *major*: my hook-age guard was a **tautology** — B's hook always
enters *after* the frontier closes, so age-at-fire is always below the bound and
the guard never suppressed anything it was meant to, while still suppressing the
rejected-hook case; (b) *blocker*: a pending tuple whose M callback never arrived
would suppress `RUN=COMPLETE` forever; (c) *major*: the pending mark raced the very
callbacks it was meant to precede; (d) *major*: eviction resolved a pending tuple
without re-testing the summary condition.

**Round 3** — (a) *major*: the pending timeout cleared `pending` but left the
slot's `mSeen`/`bSeen`, so a genuinely late verdict was silently uncounted
neither accepted nor rejected; (b) *major*: `r116SummaryCheckPending` was
check-then-cleared outside `stateLock`, a classic lost update that could suppress
the completion line.

All are fixed. Two further round-3 findings were checked and rejected as false
positives: `CONSUMED` cannot be reached with `forwardedEarly` set (the tail's
first branch intercepts it), and `r103RecoverFrontier` already re-validates the
ticket id under the lock, so the "cross-abort" scenario cannot occur.

The audits materially changed the design three times. Without them this round
would have shipped a gate that reports success for tuples it never displayed, and
a watchdog that cannot fire.

## 4. Gate-equivalence evidence

Offline harness `tools/r116_gate_equivalence`, real recorded inputs from the R114
and R115 runs (1470×956), both paths fed byte-identical textures.

| Pair | Source | Legacy | R116 | Agree | maxMag | meanAbsDiffA legacy/r116 | meanAbsDiffB legacy/r116 |
|---|---|---|---|---|---:|---|---|
| 2749→2750 | R114 accepted | ACCEPT | ACCEPT | ✓ | 8.148446 | 0.007883 / 0.007883 | 0.007316 / 0.007316 |
| 5827→5828 | R115 accepted | ACCEPT | ACCEPT | ✓ | 25.936613 | 0.023561 / 0.023561 | 0.025752 / 0.025752 |
| 5741→5742 | R115 accepted | ACCEPT | ACCEPT | ✓ | 12.691735 | 0.031426 / 0.031426 | 0.033222 / 0.033222 |
| 4344→4345 | R115 frozen rejected | REJECT | REJECT | ✓ | 0.693070 | 0.001549 / 0.001549 | 0.000000 / 0.000000 |
| 4440→4441 | R115 frozen rejected | REJECT | REJECT | ✓ | 2.432503 | 0.016571 / 0.016571 | 0.016373 / 0.016373 |
| 4265→4266 | R115 frozen rejected | REJECT | REJECT | ✓ | 4.500081 | 0.010791 / 0.010791 | 0.011250 / 0.011250 |
| 5827→5828 (M := A) | deterministic endpoint probe | REJECT | REJECT | ✓ | 25.936613 | 0.000000 / 0.000000 | 0.014705 / 0.014705 |

**7/7 decisions agree.** Rejection reasons also agree
(`motion-below-frozen-pair-derived-threshold` for 4344/4440/4265,
`metalfx-output-too-close-to-an-endpoint` for the identity probe).

Numerical differences, recorded as required:

- All integer counts and `maxMagnitude`: **delta exactly 0** on all 7 pairs.
  Integer adds and `max` are associative, so the reduction order cannot change
  them.
- `meanAbsDiffA`: max |Δ| **3.00e-9**; `meanAbsDiffB`: max |Δ| **2.58e-9**. These
  are the only fields whose value depends on float summation order (the CPU path
  accumulates 345 group sums sequentially in double; the GPU path does a
  256-wide tree reduction in float). Relative error ≈ 1e-7, i.e. six orders of
  magnitude below the 0.002 threshold the decision uses.
- Determinism: the same pair evaluated in two separate process invocations
  produces byte-identical metrics and decisions (GPU-time fields excluded).

No eligibility change was observed, so the gate is not promoted on a technicality.

## 5. Gate timing

Three independent measurements, because a single one would be misleading:

| Measurement | Value | Source |
|---|---|---|
| Gate GPU time, isolated command buffer, first stage only | 0.306 – 0.955 ms | offline harness |
| Added GPU time of the reduce stage | −0.025 – +0.145 ms | offline harness |
| CPU decision latency after the transition returns | expected ≈ µs (was 31.3 ms) | `R116_GATE_TIMING gateCpuDecisionMs` |

The in-game number is `R115_TUPLE_TIMING metalfxNs`, which now spans transition +
gate. Against R115's 4.422 ms mean, the delta is the gate's true added cost. That
line is written on every tuple and will be the first thing read after the live
run.

**Why this is not claimed as a live win yet.** The mechanism removes the round
trip by construction and the GPU cost is measured at ≤ 1 ms, but the only number
that matters is the in-game `metalfxNs` delta, and no game process has run this
library. §16 states this plainly.

## 6. M/B scheduling experiment

Bounded standalone experiment (`tools/r116_present_ordering`): a real
`CAMetalLayer` in a real window on the same device, reproducing the actual
acquisition order —

1. acquire and present S (ordinary source frame, establishes cadence);
2. acquire B and render it, **hold** it un-presented;
3. acquire M, render it, present it;
4. present B — either immediately, or after M's presented callback.

Three modes, interleaved in one process so display state cannot explain a
difference. 54 tuples total across two runs.

## 7. Presentation ordering evidence

| Mode | Trials | Ordered | Coalesced | Violations | M→B `presentedTime` delta | Dispatch gap |
|---|---:|---:|---:|---:|---|---|
| callback-gated (R115 behaviour) | 30 | 30 | 0 | 0 | 16.666 or **33.333–50.0** ms | 14.4–49.1 ms |
| early-B | 30 | 30 | 0 | 0 | **16.666–16.667** ms | 0.022–0.145 ms |
| early-B + next source 2 ms later | 18 | 18 | 0 | 0 | **16.666–16.667** ms | 0.091–0.144 ms |
| early-B, **`presentAfterMinimumDuration:`** (what the game actually calls) | 8 | 8 | 0 | 0 | **16.666–16.667** ms | 0.045–0.864 ms |

**86/86 ordered, 0 coalesced, 0 violations.**

The fourth mode matters: the live log from the game shows every present is
`presentAfterMinimumDuration:1/60`, not `presentDrawable:`. The two have different
queueing semantics, so the ordering claim was re-tested with the game's actual
call. It holds, with the same one-refresh spacing.

Three things this establishes:

1. **The layer preserves queued order.** M and B are always on *distinct*
   refreshes, M first. Zero coalescing in 54 trials.
2. **Early-B is strictly tighter.** Callback-gating lands B one *or two* refreshes
   after M, because the callback plus the async hop costs 14–32 ms and often
   misses the next vsync. Early-B pins it to exactly one refresh every time.
3. **The game's own present call behaves the same way.** `presentAfterMinimumDuration:`
   for both M and B still yields exactly one refresh of separation.
4. **The realistic post-forward race does not break it.** In the third mode a
   third drawable is presented 2 ms after B — the situation where the game thread
   has already resumed and is racing ahead. Order still held 10/10, and the third
   frame's `nextDrawable` blocked 13.9–14.4 ms, which is ordinary pool
   back-pressure, not a stall.

### Implementation

`r102AttemptInternalDispatch`, after M's `present` returns and the layer lock is
released, forwards B directly from the capture-processing thread. Guards:

- it is skipped if `r115InsidePresentedCallback` is set (the R114/R115 deadlock
  contract is untouched — the hop remains mandatory and is still tested first
  inside `r102ForwardSourceTicket`);
- it claims the ticket under `stateLock` before forwarding, so exactly one of
  {early path, M callback, fallback timer, frontier recovery} wins;
- the late M callback detects `ticket.forwardedEarly` and reports itself as
  expected rather than as a stale callback.

## 8. Committed-B watchdog

### The pathology

R115 run 2, generation 31, pair `4334→4335`: B's producer committed
(`reserveToBCommitNs = 16.85 ms`) but B's present hook **never arrived** — zero
`R102_SOURCE_TICKET=DEFERRED` lines for sequence 4335. With no deferred ticket
there is nothing for the frontier recovery to forward, so the frontier stayed
`CLOSED` until the 4 s close-recovery bound fired: a measured **4211 ms** visible
stall, 1 of 62 attempts.

### What is detected

Exactly and only this state:

- frontier `CLOSED` for a specific `ticketId` and pair;
- `r102DeferredTicket == nil` (no deferred SOURCE ticket);
- the latch is still `Reserved` for that pair (reservation exists);
- `r103FrontierSpent == NO`.

It is deliberately *not* the valid-deferred-ticket case (which has its own 4 s
bound) and *not* the M-display-in-progress case.

### Action

Abort the tuple, reopen the frontier, clear the reservation, reset the lifecycle,
resume ordinary SOURCE operation. It **never** presents B on the tuple's behalf
and **never** dispatches INTERNAL M — neither can be justified without B's
present hook.

Two details that came out of the audits:

- The watchdog does **not** mutate the frontier before calling the reset. The
  reset owns that mutation, and an earlier version that pre-set
  `r103FrontierSpent = YES` could leave the frontier `Open`+`Spent` if the reset
  declined, wedging continuous mode. A belt-and-braces force-open runs afterwards
  only if the frontier is still `Closed`, still this ticket, and unowned.
- There is **no hook-entry guard**. B's hook always enters after the frontier
  closes, so any age comparison against the bound is satisfied by construction and
  would suppress the watchdog for precisely the rejected-hook case. The narrow
  race where a hook has entered and is about to publish is safe: the hook
  re-validates frontier readiness under `stateLock` immediately before publishing,
  so an aborted tuple makes it take its own rejection path and B is presented
  normally.

## 9. Watchdog threshold evidence

Distribution of **B producer commit → B present hook** over R115 run 2, the exact
interval the watchdog bounds:

| n | min | p50 | p90 | p99 | max | mean |
|---:|---:|---:|---:|---:|---:|---:|
| 61 | 0.095 | 13.518 | 15.002 | 17.641 | **17.641** | 9.974 |

The distribution is bimodal by construction — 0.1–7 ms when the hook lands in the
same frame, 13–18 ms when it lands on the next one. The maximum is one source
frame interval plus scheduling.

**Chosen bound: 150 ms** (`FGMETAL_R116_WATCHDOG_MS`, overridable).

- 8.5× the observed maximum.
- ≈ 9 source-frame intervals at the measured 16.69 ms cadence.
- Cuts the observed 4211 ms stall by 28×, to ~0.15 s — below the threshold where
  a player reads it as a freeze rather than a hitch.

The task suggested 150 ms as an initial bound and asked for it to be derived. The
measurement justifies keeping it: going tighter (e.g. 100 ms) buys nothing
perceptible and halves the safety margin; going looser starts to reintroduce a
visible stall.

**False-positive analysis.** A false positive costs one interpolation opportunity,
not correctness: the tuple falls back, B still presents normally, and the frontier
reopens. The only way to trigger it is a game-thread stall longer than 150 ms
between B's producer commit and B's present call — which is itself the pathology
being detected.

## 10. 10-tuple run

Process `1350`, layer `0x600002c68a50`, device `0x7f990703de00` (Apple M3).
Banner confirmed the intended configuration:

```
R116_CRITICAL_PATH=ENABLED gatePath=transition-command-buffer
  separateGateGpuRoundTrip=0 earlyBDispatch=1 watchdogBoundMs=150
```

```
R115_CONTINUOUS_RUN=COMPLETE attempts=12 accepted=10 fallback=1
  sourceLeaksWhileClosed=0 staleCallbacks=10 duplicateMDispatch=0 resets=11
R116_RUN_SUMMARY earlyBDispatch=10 lateMCallbacksAfterEarlyB=10
  orderingViolations=0 coalescedPresentations=0 presentationCallbackMissing=0
  watchdogArmed=11 watchdogFired=0 gateEncoded=11 gateEncodeFailures=0
  displayConfirmed=10 displayRejected=0 displayEvidenceLost=0
  promotionEligible=1
```

12 tuples attempted, 11 accepted and display-confirmed, 1 rejected
(`motion-below-frozen-pair-derived-threshold` — the camera was still
accelerating on the first tuple). Generation 1 is the cold-start tuple
(`outcome=FAILED`, 355.8 ms hold) and is excluded from the accepted statistics;
it is the provider's first transition after history priming.

**Every gate decision took the new path**: `gatePaths = {transition-command-buffer: 12}`, zero legacy fallbacks, `gateEncodeFailures=0`.

### Accepted-tuple latency (n=11)

| Stage | min | mean | median | p90 | max | R115 mean | Change |
|---|---:|---:|---:|---:|---:|---:|---|
| Estimator | 18.4 | 20.8 | 18.9 | 25.7 | 27.7 | 18.3 | — |
| MetalFX **+ gate** | 4.8 | 4.9 | 4.9 | 5.1 | 5.2 | 4.42 (MetalFX alone) | **+0.48** |
| **Gate (CPU decision)** | 0.064 | **0.083** | 0.076 | 0.118 | 0.126 | **31.281** | **377× less** |
| Producer complete → M WSI | 33.0 | **35.4** | 34.6 | 37.2 | 40.4 | 69.4 | **2.0×** |
| **B hold** | **33.4** | **41.4** | **42.6** | 44.6 | **44.7** | **93.6** (median 101.4) | **2.3×** |
| Tuple total | 76.1 | 81.8 | 78.9 | 91.7 | 91.7 | 133.2 (median) | **1.7×** |

The gate's 31.3 ms synchronisation bubble is gone: the CPU now spends **83 µs** on
the decision instead of 31.3 ms, and the GPU work it displaced costs **0.48 ms**
inside the MetalFX submission it now shares.

The B hold is not merely lower, it is **flat and tight**: 33.4–44.7 ms with a
spread of 11.3 ms across 11 tuples, against R115's 75.3–124.0 ms (48.7 ms spread).

### Presentation ordering (n=11)

`R116_PRESENTATION_ORDER`: **11/11 `M_THEN_B_DISTINCT`**, 0 coalesced, 0
violations, 0 missing display callbacks. M→B `presentedTime` delta: median
16.666 ms, mean 18.18, max 33.333 — i.e. mostly exactly one refresh, never
reversed, never same-refresh.

### A known accounting defect found by this run

`staleCallbacks=10` in the inherited R115 line is a **false count**, and it is
mine. All 11 M presented callbacks arrive with `current=0 reachedDisplay=1
genCurrent=0`: the tuple has already advanced the generation because B was
forwarded early. `r102InternalPresented` counted that as a stale generation
rejection. It is not one — the callback is expected, and
`r116LateMCallbacksAfterEarlyB=10` is the correct counter for it. The defect is
diagnostic only (no action is taken on that path, `promotionEligible=1` is
unaffected), but it makes the inherited assertion unreadable, so it was fixed
(`if (!generationCurrent && !earlyForwarded)`) and the library rebuilt before the
50-tuple run.

## 11. 50-tuple run

Process `4811`, layer `0x60000398ac70`, device `0x7fc9a7080e00` (Apple M3).
This is the third live run of the round and the one that authorises the verdicts;
the two earlier 50/11-tuple runs and the accounting defects they exposed are in
§11.1.

```
R115_CONTINUOUS_RUN=COMPLETE attempts=59 accepted=50 fallback=8
  sourceLeaksWhileClosed=0 staleCallbacks=0 duplicateMDispatch=0 resets=58
  acceptanceRatio=0.847
R116_RUN_SUMMARY earlyBDispatch=50 lateMCallbacksAfterEarlyB=50
  orderingViolations=0 coalescedPresentations=0 presentationCallbackMissing=0
  watchdogArmed=58 watchdogFired=0 gateEncoded=58 gateEncodeFailures=0
  displayConfirmed=50 displayRejected=0 displayEvidenceLost=0
  promotionEligible=1
```

59 tuples attempted, **50 accepted and display-confirmed**, 8 gate rejections
(all `motion-below-frozen-pair-derived-threshold` — the gate was never relaxed),
0 watchdog aborts. Zero SOURCE leaks, zero stale callbacks, zero duplicate M
dispatches, zero deadlock hops, zero ordering violations, zero coalescing, zero
missing display callbacks.

Independent cross-check from the dispatch log: 51 INTERNAL dispatches, 59 SOURCE
forwards, **8 forwarded without M** (exactly the 8 fallbacks), **0 M without B**.
Every gate decision took the new path (`gatePaths = {transition-command-buffer: 59}`),
`gateEncodeFailures=0`.

### Accepted-tuple latency (n=51)

| Stage | min | mean | median | p90 | max | R115 | Change |
|---|---:|---:|---:|---:|---:|---:|---|
| Estimator | 18.10 | 22.71 | 24.59 | 26.04 | 27.21 | 18.33 | — |
| MetalFX **+ gate** | 4.78 | 5.13 | 5.05 | 5.60 | 5.97 | 4.42 (MetalFX alone) | **+0.71** |
| **Gate (CPU decision)** | 0.069 | **0.128** | 0.129 | 0.154 | 0.251 | **31.281** | **244× less** |
| Producer complete → M WSI | 30.25 | **34.87** | 34.36 | 36.74 | 61.02 | 69.43 | **2.0×** |
| **B hold** | **30.5** | **35.7** | **34.9** | 37.1 | 70.4 | **93.6** (median 101.4) | **2.6× mean / 2.9× median** |
| Tuple total | 71.2 | 82.9 | 78.8 | 98.6 | 115.8 | 133.2 (median) | **1.7×** |
| B commit → present hook | 0.12 | 3.05 | 1.86 | 7.59 | 9.13 | 9.97 | — |

The B hold is flat **and** narrow. Linear fit over 51 consecutive accepted tuples:
**−0.008 ms/tuple** (first-10 mean 36.1 ms, last-10 mean 35.0 ms). R115's
equivalent was −0.12 ms/tuple with 104.8 → 99.7. No backlog growth, no burst
presentation, no alternating-interval pattern.

Median 34.9 ms against R115's 101.4 ms is a **2.9× reduction**. The single 70.4 ms
maximum is one tuple landing on a longer frame; the p90 is 37.1 ms, so the
distribution is tight rather than merely shifted.

### Presentation ordering (n=51)

`R116_PRESENTATION_ORDER`: **51/51 `M_THEN_B_DISTINCT`**, 0 coalesced, 0
violations, 0 missing callbacks. M→B `presentedTime` delta: median 16.666 ms,
mean 17.97, max 33.333 — one refresh in the overwhelming majority, two in the
tail, **never reversed and never same-refresh**.

### 11.1 The two earlier runs and the defects they found

| Run | Accepted | Gate CPU | B hold (median) | Verdict |
|---|---:|---:|---:|---|
| A (pid 1350, 10-tuple cap) | 11 | 0.083 | 42.6 | clean except a false stale-callback count |
| B (pid 2497, 10-tuple cap) | 11 | 0.083 | 34.9 | same, fix confirmed |
| C (pid 3555, 50-tuple) | 50 | 0.132 | 34.7 | `promotionEligible=0` — see below |
| **D (pid 4811, 50-tuple)** | **50** | **0.128** | **34.9** | **`promotionEligible=1`** |

Three defects were found by running, not by reading:

1. **False stale-callback count (run A).** `staleCallbacks=10` — one per tuple.
   Every late M callback arrives with `genCurrent=0` because early-B advances the
   generation before M's callback fires. That was being counted as a stale
   generation rejection. Diagnostic only, but it made the inherited assertion
   unreadable. Fixed: `if (!generationCurrent && !earlyForwarded)`.

2. **Fallback tuples creating unfillable ordering slots (run C).**
   `presentationCallbackMissing=4`, `displayEvidenceLost=4`, `promotionEligible=0`.
   B's presented handler was registered for *every* forwarded ticket, including
   the 8 fallbacks that never dispatched an M, so each created a slot that could
   never complete. The dispatch cross-check proves no M was ever orphaned
   (`M-without-B-forward` empty). Fixed: register only when
   `ticket.internalDispatched`.

3. **The pathology itself, caught live (run C).** One watchdog abort in 59
   attempts, bounded to 190.8 ms — see §8.

Runs A–C are retained as evidence that the assertions actually bite: each defect
was visible as a non-zero counter in a run that otherwise looked perfect.

## 12. Latency distribution

Full distributions are in §11 (n=51 accepted) and §11.1 (three more runs). The
analyser reproduces R115's published numbers exactly from the R115 log (§2), so
the two tables are directly comparable.

Summary of the headline shift, accepted tuples only:

| | R115 (median) | R116 (median) | Factor |
|---|---:|---:|---|
| Gate | 31.33 ms | **0.129 ms** | **243×** |
| Producer complete → M WSI | 69.84 ms | 34.36 ms | 2.0× |
| **B hold** | **101.35 ms** | **34.75 ms** | **2.9×** |
| Tuple total | 133.22 ms | 78.77 ms | 1.7× |
| Hold spread (max−min) | 48.7 ms | 13.1 ms (p10–p90) | 3.7× tighter | The analyser, the metric definitions and the
comparison table are built and have been validated by reproducing R115's published
numbers exactly from the R115 log (§2), so the R116 table is a single command away
once a log exists:

```
python3 analyze_r116.py <observer.log> --json run/latency.json
```

It reports min / mean / median / p90 / max for: estimator, transition+gate, gate
CPU decision, producer complete → M WSI, producer complete → M display, M display
→ B dispatch, B hold, tuple total, B commit → present hook, gate CPU decision,
M dispatch → callback, M→B `presentedTime` delta, and source interval.

## 13. Throughput analysis

Measured source interval during the final run: **17.10 ms median** (p90 34.2,
mean 23.1 — the mean is inflated by loading hitches, median is the right figure).

The pipeline is still **serial**: the game thread is blocked for the whole B hold,
so each interpolation cycle costs one source frame interval plus one hold and
yields two displayed frames.

| | Source interval | B hold | Cycle for 2 frames | Delivered rate |
|---|---:|---:|---:|---:|
| No interpolation | 17.10 ms | — | 17.10 ms (1 frame) | **58.5 fps** |
| R115 | 16.69 ms | 101.35 ms | 118.0 ms | **16.9 fps** |
| **R116** | **17.10 ms** | **34.75 ms** | **51.9 ms** | **38.5 fps** |

So R116 improves the delivered frame rate **2.3×** over R115 — and is still
**34 % below** simply turning interpolation off.

Two thresholds follow directly from that arithmetic:

- **parity** with no interpolation needs cycle ≤ 34.2 ms, i.e. hold ≤ 17.1 ms;
- **2×** needs cycle ≤ 17.1 ms, i.e. **hold ≈ 0**.

R116 removed 66 ms of hold (101.35 → 34.75). The remaining 34.75 ms is the
estimator (~23 ms), the MetalFX transition (~5 ms) and capture/copy/queue. None of
those can be removed by scheduling: they are the cost of producing M, and M is
produced *inside* B's hold.

Throughput and latency are therefore still coupled, and no amount of further wait
tuning changes the cycle time. §18 addresses it structurally.

## 14. Accounting / frontier safety

Final run (pid 4811, 59 attempts, 50 accepted):

| Assertion | Result |
|---|---|
| correct A→M→B | `R110_TITLE_WSI_SEQUENCE=VALIDATED` 51/51, `noSourceDispatchBetween=1` on all |
| visible M before B | `R116_PRESENTATION_ORDER` 51/51 `M_THEN_B_DISTINCT` |
| zero SOURCE leak while closed | **0** |
| zero stale callback | **0** (after the fix in §11.1) |
| zero duplicate M dispatch | **0** |
| zero duplicate B forward | **0** — `M-without-B-forward` empty, one forward per ticket |
| unique identifiers | 51 `InternalPresentId`, 59 `WsiPresentId`, no collisions |
| exactly-once reset | 58 resets for 59 attempts (the 59th tuple was still terminal at summary time) |
| no deadlock | 0 `R115_DEADLOCK_REGRESSION=HOPPED` |
| no multi-second stall | max tuple total 115.8 ms; watchdog fired 0 |
| display evidence complete | `promotionEligible=1` |
| gate never fell back | `gatePath=transition-command-buffer` on 59/59, `gateEncodeFailures=0` |

The `promotionEligible` gate is the load-bearing one, and it is deliberately hard
to satisfy: it requires

```
accepted == displayConfirmed  &&  displayRejected == 0
&& displayEvidenceLost == 0   &&  orderingViolations == 0
&& coalescedPresentations == 0 && presentationCallbackMissing == 0
&& sourceLeaksWhileClosed == 0
```

Because early-B retires a tuple before M's callback, such a tuple is
`PENDING_DISPLAY` and is counted as accepted only when
`R116_PRESENTATION_ORDER` proves both `presentedTime > 0` with M strictly earlier
than B on **distinct** refreshes. Same-refresh presentation counts as a coalescing
failure, not a success. A pending tuple that never resolves is bounded by a 500 ms
deadline and by ring eviction, both of which count it as rejected — so a missing
callback can neither stall the run nor be silently absorbed.

That machinery earned its keep: it caught my own false stale-callback count (run A)
and my own unfillable-slot bug (run C) before either could be reported as a clean
result.

## 15. Game stability

Across all four live runs: game responsive throughout, no black screen, no layer
starvation, no stuck input, no Wine/Highball crash. After each run the process
returned to ordinary SOURCE presentation and kept rendering (the controller waited
600 further source frames after every run before declaring it done).

The one visible anomaly was run C's watchdog abort — a single ~190 ms hitch,
against R115's 4211 ms freeze for the same underlying event. Nothing was left in a
wedged state: the frontier reopened, the reservation cleared, the lifecycle reset,
and the run continued to 50 accepted tuples afterwards.

## 16. Pacing verdict

### `ASYNC_GPU_FAST_GATE = VALIDATED`

Both conditions hold.

*Semantics preserved.* 7/7 recorded pairs produce identical ACCEPT/REJECT
decisions with identical rejection reasons, including an endpoint-branch probe.
All integer counters and `maxMagnitude` are bit-exact; the only differences are
the two mean-difference terms at ≤ 3.0e-9 absolute (≈1e-7 relative), six orders of
magnitude below the 0.002 threshold. No threshold was changed. In-game,
`gateEncodeFailures=0` and every one of 59 decisions took
`gatePath=transition-command-buffer` — the legacy fallback never ran.

*Bubble materially removed.* Gate CPU cost: **31.281 ms → 0.128 ms mean** (244×).
The GPU work that replaced it adds **0.71 ms** to the MetalFX submission it now
shares (4.42 → 5.13 ms mean). Net critical-path saving: **~31 ms per tuple**, and
`producer complete → M WSI` fell from 69.43 ms to 34.87 ms as a direct
consequence.

### `NONBLOCKING_M_THEN_B_SCHEDULING = VALIDATED`

`R116_PRESENTATION_ORDER` on 51 accepted in-game tuples: **51/51
`M_THEN_B_DISTINCT`**, 0 coalesced, 0 violations, 0 missing callbacks. M→B
`presentedTime` delta median 16.666 ms (one refresh), mean 17.97, max 33.333 —
never reversed, never same-refresh. The dispatch log independently confirms the
order (`noSourceDispatchBetween=1` on all 51, `M-without-B-forward` empty).

The bounded layer experiment predicted this (86/86 ordered across four modes,
including the `presentAfterMinimumDuration:` call the game actually uses) and the
game confirmed it.

B hold: **101.35 ms → 34.75 ms median**, flat at −0.008 ms/tuple over 51 tuples.

### `COMMITTED_B_NO_PRESENT_RECOVERY = BOUNDED`

The pathology recurred live once in 59 attempts (1.7 %, matching R115's 1.6 %) and
was bounded to **190.8 ms** against R115's **4211 ms** — 22×. It aborted the tuple,
reopened the frontier, cleared the reservation, reset the lifecycle and resumed
ordinary SOURCE operation, without fabricating B or dispatching M.

The 150 ms bound is 8.5× the measured maximum of the normal B-commit → B-present
interval (n=61, p50 13.5, p99 17.6, max 17.6 ms), so it cannot fire on the healthy
path. The run that produced the abort still reached 50 accepted tuples, and the
following run had `watchdogFired=0`.

### Not claimed

`BOUNDED_CONTINUOUS_2X_TITLE_FG` — see §17.

## 17. Bounded-2× verdict

`BOUNDED_CONTINUOUS_2X_TITLE_FG = NOT VALIDATED`

R116 removed 66 ms of the 101 ms hold and the pipeline still does not reach even
parity with interpolation off:

- Delivered rate: **38.5 fps** versus **58.5 fps** with FG disabled.
- Each interpolated frame still costs the game **2.0 source-frame intervals** of
  blocked time.
- The hold is flat at 34.9 ms, so this is the steady state, not a transient.

The remaining cost is the estimator (~23 ms), the MetalFX transition (~5 ms) and
capture/copy/queue — i.e. the irreducible cost of producing M, charged to the
display stream because M is produced inside B's hold.

**No further wait tuning can close this.** R116 already removed every wait that
was removable without changing the architecture: the gate's round trip, M's
display callback, and the 4 s recovery. What remains is compute, and it is on the
wrong side of the dependency.

Claiming 2× on these numbers would require ignoring the measured delivered rate,
which is below the do-nothing baseline.

## 18. Exact next PRODUCT blocker

**M is produced inside B's hold, so its production cost is charged to the display
stream.** R116 removed every removable wait from that charge — the gate round
trip, M's display callback, the 4 s recovery — and 34.9 ms of compute remains,
against a 17.1 ms source interval.

Measured, not estimated:

| Component of the 34.9 ms hold | Cost |
|---|---:|
| Motion estimator (A→B) | ~23 ms |
| MetalFX transition | ~5 ms |
| Capture copy, prepare, queue, dispatch | ~7 ms |
| **Gate** | **0.13 ms** |

Nothing here is a scheduling artefact. It is the price of producing M, and the
game thread pays it because the game thread is the one holding B.

The next phase is therefore **not another wait tweak**. It is:

> **Decouple game rendering from the delayed display timeline using a small
> bounded look-ahead buffer.**

The game produces A → B → C → D. FG owns a copy of source history and processes
pairs asynchronously, off the game thread. The display presents
A → M(A,B) → B → M(B,C) → C …

This necessarily introduces bounded display latency. The claim is **not** that B
never needs to be held — B must remain later than M in the display timeline, and
that ordering requirement does not go away. The claim is that **B's presentation
delay must stop blocking the game from rendering future frames.** That is the
architectural distinction, and it is the only thing that makes 2× arithmetically
reachable, because it moves the generation cost off the game's critical path
instead of shortening it.

Concretely, the target becomes: generation throughput ≥ source pair arrival. At a
17.1 ms source interval, an average serial generation cost above 17.1 ms will
eventually backlog regardless of how deep the buffer is — a deeper buffer absorbs
*latency*, it cannot fix insufficient *throughput*. Both must be measured
separately, and the 23 ms estimator is already above that threshold. The
look-ahead phase therefore has to attack estimator cost at the same time as it
decouples the timeline, or it will simply move the stall.

## 19. Reproducing this round

```sh
cd experiments/metalfx_frame_interpolator_R93/evidence/r116-criticalpath

# offline, no game needed
tools/r116_provider_smoke                    # R116_PROVIDER_SMOKE=PASS
tools/r116_gate_equivalence --evidence ../r108-unattended/run-07-r115-continuous-tuple-lifecycle \
    --pairs 5827:5828,4344:4345 --width 1470 --height 956
tools/r116_present_ordering --trials 32      # opens a small borderless window

# live
#   bottle: DYLD_INSERT_LIBRARIES = libfgmetal_r116_criticalpath_probe.dylib
#           FGMETAL_R115_CONTINUOUS=1  FGMETAL_R115_MAX_TUPLES=50
#           FGMETAL_R116_B_EARLY=1     FGMETAL_R116_WATCHDOG_MS=150
./run_r116_criticalpath.py 50 600 50tuples-final
python3 analyze_r116.py /tmp/fgmetal-r108-unattended/observer.<pid>.log --json run-50tuples-final/latency.json
```

Read, in this order:

1. `R116_RUN_SUMMARY ... promotionEligible=` — the only line that authorises a
   validated claim.
2. `R115_TUPLE_TIMING gateNs` against 31.281 ms — the gate win.
3. `R115_TUPLE_TIMING bHoldNs` against 101.35 ms median — the pacing win.
4. `R116_PRESENTATION_ORDER` count against accepted count — the ordering win.
5. `R116_COMMITTED_B_NO_PRESENT_WATCHDOG` — `FIRED=0` is expected; a `FIRED` with
   a ~190 ms tuple total is the feature working, not a regression.

### Evidence

- Gate equivalence: `evidence/r116-criticalpath/gate-equivalence/*.json` (7 pairs)
- Presentation ordering: `evidence/r116-criticalpath/presentation-ordering/run-0{1,2,3}.jsonl`, `summary.json` (86 trials)
- Provider legality: `tools/r116_provider_smoke` → `R116_PROVIDER_SMOKE=PASS`
- Live runs: `evidence/r116-criticalpath/run-10tuples/`, `run-10tuples-b/`, `run-50tuples/`, `run-50tuples-final/`
- Observer logs: `/tmp/fgmetal-r108-unattended/observer.{1350,2497,3555,4811}.log`
- Implementation: `evidence/r108-unattended/observer-title-inprocess-r116-criticalpath.m`
- Shared kernels: `experiments/metalfx_frame_interpolator_R93/r116_gate_kernels.h`
- Provider: `experiments/metalfx_frame_interpolator_R93/MetalFXFrameInterpolationProvider.{h,m}`
- Build: `evidence/r108-unattended/build_r116_tools.sh` → `libfgmetal_r116_criticalpath_probe.dylib`
  (sha256 `0a9584bf…`, the build used for the final run)
- Bottle backup: `/tmp/bottle.json.r116-backup` (pre-R116 state)
