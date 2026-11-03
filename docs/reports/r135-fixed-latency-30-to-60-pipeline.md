# R135 — PROVE AND IMPLEMENT THE FIXED-LATENCY 30→60 PIPELINE

Evidence root: `experiments/metalfx_frame_interpolator_R93/r135-evidence/`

Every number below is labelled **[MEASURED]**, **[DERIVED]** (arithmetic on
measured values), **[INTERPRETATION]** or **[HYPOTHESIS]**. Nothing is asserted
as measured unless a record in the evidence tree contains it.

---

## 1. Executive verdicts

```
MIDPOINT_DEADLINE_MODEL      = VALIDATED
                             (steady-state W, realized slot budget and miss
                              measured directly for the first time; §5–§6)

FIXED_LATENCY_PIPELINE       = VALIDATED
                             (capture-anchored two-rule schedule; 94.4 % of
                              midpoints presented at ~34 ms source; fixed
                              per-pair latency; no accumulation; §7–§9)

CROSS_QUEUE_CAPTURE_SYNC     = VALIDATED
                             (shared-event edge; 29 158 waits, 0 skips,
                              0 violations, min encode margin 75 µs; §12;
                              cadence-neutrality comparison in §11)

R126_CLASS_CADENCE           = VALIDATED
                             (93.8–94.4 % INTERNAL/SOURCE at 34.4 ms source,
                              ≈56 fps total output ≈ 1.94× source, 0 SOURCE
                              drops, 0 order violations, 0 drawable/present
                              failures; §9; visually confirmed by the user)

RUNTIME_FG_ENABLE_DISABLE    = VALIDATED (OFF→ON×3→OFF, §13)
FG_FAILURE_FALLBACK          = VALIDATED (60-pair injection, §14)
D3DMETAL_FG_ALPHA_SOAK       = VALIDATED (12 min 28 s, 89.9 % share, §15)
FG_DISPLAY_LATENCY           = MEASURED (§16)
BOUNDED_ALPHA_DIAGNOSTICS    = VALIDATED (§17)

D3DMETAL_FG_ALPHA_RUNTIME    = PARTIAL — the cadence is R126-class and every
                             correctness invariant holds, but the product
                             still shows visible artifacts at the ~5–10 % of
                             pairs whose midpoint misses (user-reported
                             glitches) and the host GPU remains fragile
                             (SubmissionsIgnored recurred in 1 of 2 later
                             sessions). §17.
```

Option C **was** implemented — twice. The first form (the R134 §21 shape:
rule-2 anchor delayed, rule 1 untouched) was implemented, reviewed STABLE, and
**falsified in the field** (§8): it can never enter its own steady state. The
amended form (both slot rules capture-anchored) is the validated pipeline.

---

## 2. Model assignments

- **Interactive harness:** Zcode, model `GLM-5.3 Flash` throughout. No other
  model family was used.
- **Root orchestrator — GLM-5.3 Flash (Max):** architecture, schedule
  derivation and its two amendments, instrumentation design, integration,
  verdicts. Also executed the instrumentation and pump edits directly: the
  edits are small and surgical inside one 13.7k-line file, and the entry-defect
  root-cause analysis was deadline-model reasoning, not mechanical work —
  spawning a High worker would have added hand-off risk without reducing any
  risk that matters. This is the "do not launch a worker if the root can
  efficiently do that scope itself" clause.
- **Scheduling analyst — GLM-5.3 Flash (Max, fresh subagent):** adversarial
  design review of the original derivation (verdict: DESIGN = STABLE with five
  corrections) and a delta review of the capture-anchored amendment (verdict:
  DESIGN-DELTA = STABLE; both preserved in the agent transcript and summarised
  in §7/§8). Note: the delta review's proposed correction to the rule-1
  comment was itself arithmetically wrong (it mis-attached the measured 25 ms
  to the wrong capture); the comment was clarified instead and the mechanism
  numbers in §8 are the authoritative reading.
- **Instrumentation / implementation worker — not spawned.** See above.
- **Performance / evidence worker — not spawned.** Live windows are strictly
  serial on one GPU host; parallelism is impossible, and the reduction scripts
  already existed from R134.
- **Final reviewer — fresh Max:** §15 (dispatched after implementation
  stabilized; result recorded there).

---

## 3. Files changed

| File | Change |
|---|---|
| `r127-alpha-runtime/R123DisplayPump.m` | Rule 2 anchor → `capture + 2·period`; rule 1 → capture-anchored `capture + 2·period + halfPeriod` (fallback `now + H` while period unknown); `_measuredSourceDelayNs` clamp widened `[H,3H] → [H,6H]`; rationale comments |
| `r127-alpha-runtime/R123DisplayPump.h` | Contract documentation for the capture-anchored two-rule schedule |
| `r127-alpha-runtime/r123_pump_selftest.m` | 2 contract assertions updated, 5 new pipeline cases (K–N), dispatch-trace recorder, two-phase-period and slow-pair scenario support, horizon fix |
| `r127-alpha-runtime/observer-title-inprocess-r127.m` | R134 retain-by-reference + per-ON window reset + `internalReadyToDispatch` interval + warmup counters; R135 shared-event capture sync (env-gated); sync counters in `R134_TIMELINE` |
| `r135-evidence/tools/*` | bottle env, drive scripts, analyzer, verified key-tap tool (input blocked by TCC; unused) |
| `r135-evidence/R135-SCHEDULE-DERIVATION.md` | derivation + field amendment record |

Untouched and hash-verified: SOURCE ownership, capture staging, estimator
math, MetalFX math, reservation machinery, R125 retention, R126 self-present
gate, R120 pacing, R102 disabled path.

---

## 4. Builds and deterministic gates

| Build | Content | Pump selftest | 45-case suite |
|---|---|---|---|
| `instrumented-build/` | Phase-1 instrumentation only | 10/10 | 45/45 |
| `pipeline-build/` | capture-anchored pipeline + sync edge | 15/15 | 45/45 |
| `sync-build/` | same, pre-env-gate | 15/15 | 45/45 |
| `pipeline-only-build/` | env-gated sync (default ON) | 15/15 | 45/45 |

Logs: `r135-evidence/*.log`. The two contract-assertion updates in the pump
selftest are contract *changes* (two-period delay; 70 ms miss case), not
weakenings; case E, G, H, H2, I, J are byte-identical in their assertions.

Phase-1 instrumentation (all three R134 gaps closed):

1. **Retain-by-reference:** every `r134Note*` re-copies a retained record in
   place (`r134RefreshRetainedLocked`), so late timestamps (estimator wake,
   MetalFX end, resolve) survive into the emission snapshot. Ring-recycle is
   detected and the retained copy stands.
2. **Steady-state window:** the retained sample resets on every runtime ON
   (`windowStarts` counts resets; `warmupExcluded` counts the excluded pairs —
   7 per window, matching `kR134WarmupPairs`).
3. **New interval:** `internalReadyToDispatch` (presented class only).

---

## 5. Completed deadline measurements (Phase 1 + decision gate)

Window 1 (`live-r135/observer.2840.log`, open world, ~29.5 fps source,
pre-Option-C schedule, missed class, n = 376–384) — **[MEASURED]**:

| Interval | median | p90 | p99 | max |
|---|---|---|---|---|
| captureComplete → generationStart | 57 µs | 64 µs | 195 µs | 273 µs |
| generationStart → estimatorSubmit | 160 µs | 368 µs | 505 µs | 654 µs |
| estimatorSubmit → GPU complete | 21.50 ms | 22.87 | 23.80 | 26.54 |
| GPU complete → MetalFX start | 23 µs | 33 | 54 | 153 |
| MetalFX start → end | 7.74 ms | 8.37 | 9.05 | 11.86 |
| **captureComplete → INTERNALReady (W)** | **29.56 ms** | **31.09** | **32.02** | **35.74** |
| captureComplete → intended slot (budget) | 25.92 ms | 33.13 | 36.12 | 36.89 |
| intended slot → INTERNALReady (miss) | **+3.56 ms** | **+11.60** | **+14.55** | **+15.03** |

Decision-gate answers:

1. Steady-state compute **W ≈ 29.6 ms median, 32.0 p99** (serial; the CPU path
   is 0.22 ms of it).
2. Budget from B's capture-complete to the realized midpoint slot: **25.9 ms
   median** (the realized slot is later than the idealized half-period because
   of the early-present cascade, see §8).
3. INTERNAL misses the current slot by **3.6 ms median / 11.6 ms p90**.
4. Recoverable scheduling delay: **none** (0.22 ms total CPU path).
5. Sufficient delayed slot: **L = 2P** — midpoint budget `2P − H ≈ 51.3 ms`
   against W p99 32.0 ms → margin ≥ 19 ms. L = 1.5P (budget 34.5 ms, worst-case
   margin 2.7 ms) rejected as fragile; matches the analyst's independent
   recommendation.

Cross-queue ordering before the sync edge (same window): 0 violations by the
R134 counter, but the counter is blind when `copyComplete` lands after
`pumpUse` (it cannot compare what it does not yet have) — the sync edge was
added on the analyst's original recommendation and the min encode-time margin
improved from 8 µs (R134) to ≥ 75 µs (§12).

---

## 6. Steady-state sample design

`FGMETAL_R134_TIMELINE` retained sample: 384 terminal pairs after a 7-pair
warm-up, window reset at each runtime ON, records refreshed in place by every
late timestamp. Emission snapshots the latest complete state at each periodic
and final summary. With the locked pipeline the retained sample is
presented-dominated (335–356 presented of 384), which is the steady state the
round needed and R134 could not see.

---

## 7. The exact pipeline schedule (as implemented)

Both slot rules are **capture-anchored** (`R123DisplayPump.m
takeForSlotAtNs`):

```
after presenting SOURCE n        → next slot = capture(n) + 2·period + H
after presenting M(n, n+1)       → next slot = capture(n+1) + 2·period
(period == 0 → both fall back to now + H; only the first SOURCE after ON)
```

Steady state **[DERIVED, verified field-exact]**: SOURCE n presents at
`capture(n) + 2P`; `M(n,n+1)` at `capture(n) + 2P + H = capture(n+1) + P + H`;
slot spacing is `H` then `P_true − H` (H here is the pump's fixed 16.667 ms
60 Hz half-slot quantum, not the measured half-period — at the field period the
two spacings are 16.67 and ~17.8 ms); midpoint budget `P + H − W ≈ 50.9 − 28.5
≈ 22.4 ms` at the field period. With `_sourcePeriodNs` clamped to `[H, 4H]` per
update, every slot lies in `[capture + 3H, capture + 9H]`.

---

## 8. Why the reviewed first form failed, and the fixed-latency proof

The originally reviewed form (rule 2 delayed, rule 1 = `present + H`) was
**falsified by the field** (`live-r135/observer.4381.log`, cadence window 1:
174/2717 = 6.4 % presented): in the pre-entry cascade the realized SOURCE
present is `p_n ≈ capture(n) + 8.4 ms` (the midpoint drop tick precedes B's
commit; B waits one idle tick), so the present-anchored midpoint slot lands at
`capture(n) + 25 ms = capture(n+1) − 9 ms` — **before the pair's own B is
captured** and ~38 ms before the midpoint can be ready, at any source period.
The slot for every pair is structurally unpresentable, so no rule-2 anchor ever
fires: the cascade is a closed feedback loop (slot ← present ← drop ← previous
slot) and the "first hit locks the schedule" entry mechanism is unreachable.
`R123_INTERNAL_ENQUEUED … reason=placeholder-already-dropped` records show the
resolve arriving after the drop. The analyst's delta review verified the
amendment restores reachability: **entry is deterministic** (period==0
fallback for `M(1,2)` at most; from SOURCE 2 the capture-anchored slot ≈
`capture(B) + 51 ms` ≥ ready → presents → rule 2 locks from pair 3) and the
cascade **cannot recur** because no slot is derived from a present time.

Required properties, all re-verified by the delta review against the code and
by the field:

- **Fixed latency** — every SOURCE at exactly `capture + 2·period`
  (selftest case K asserts the realized dispatch time per frame over 240
  frames; the field evidence is `R127_LATENCY_SUMMARY`
  `medianSourcePeriods=2.949` / 2.887 and the `captureCompleteToIntendedSlot`
  slot records — the pump's own `measuredSourceDelayNs` is a diagnostic the
  observer never logs).
- **No accumulation** — both anchors re-derive per pair from capture times;
  late ticks shift only their own present.
- **Stable cadence** — SOURCE→INTERNAL→SOURCE interleave at H spacing (case K
  trace assertions; field idleSlots = 8 vs 2107 pre-amendment).
- **SOURCE cadence preserved** — 33.8–34.5 ms measured periods, unchanged.
- **No reordering / exactly once** — orderViolations = 0 in every window;
  positions are ordinal-derived, tracker untouched.
- **Midpoint pair correctness** — `M(A,B)` is queued before B and claimed
  FIFO-first; a dropped M is a gap, never a violation.
- **Bounded queueing** — maxDepth ≤ 8 (bound), in-flight ≈ 2 sources + placeholders.

Hitch behaviour **[DERIVED, delta review]**: a capture hitch > ~56 ms drops
that pair's midpoint; the recovery pair re-anchors with a 66–139 ms budget and
the pump drains past-due slots FIFO at CPU speed (ordering intact, cadence
restored by the next capture-anchored pair). One-frame latency spike after a
hitch is bounded by `2·clamp(hitch)`.

---

## 9. Short live cadence at ~30 fps SOURCE (target rate)

`live-r135/observer.5823.log`, cadence window (94 s generating, open world,
sync edge active):

```
sourceEnqueued=2801  sourcePresented=2799  internalEnqueued=2799
internalPresented=2624  deadlineMisses=156  droppedSource=0
drawableFailures=0  presentFailures=0  orderViolations=0  idleSlots=8
sourcePeriodNs=34448000  maxDepth=8  currentDepth=2
```

**[MEASURED]** INTERNAL/SOURCE = 2624/2799 = **93.8 %** at a 34.45 ms source
period. **[DERIVED]** source rate 29.03 fps; total displayed = 5423 items over
the ~96 s generating window ≈ **56.2 fps**, ≈ 1.94× the source, ≈ 97 % of the
ideal doubled-source ceiling. The user independently reported the game visibly
running at ~60 fps (with glitches — §17).

Presented-class intervals in the locked state (n = 335) **[MEASURED]**: W
median 22.67 ms (p99 34.91); `captureCompleteToIntendedSlot` median 84.55 ms
(the record's slot is the *next* pair's midpoint slot, `capture(B) + 2P + H` —
exact); ready-minus-slot median −61.7 ms (midpoints ready ~62 ms before their
slot — the fixed-latency margin); `internalReadyToDispatch` median 32.5 ms
(the midpoint waits at its slot for its scheduled turn).

---

## 10. Deterministic pipeline tests

Pump selftest grew 10 → **15 cases**; additions:

- **K** — steady alternation + fixed latency: per-frame exact-time assertions
  over 240 frames at the measured title cadence (every SOURCE at
  `capture + 2·period`, every midpoint at `capture + 2P + H`, zero misses,
  zero idle, bound respected).
- **L** — one missing INTERNAL: exact early-present of SOURCE 10 at the missed
  slot (`capture + P + H`), recovery midpoint back on the fixed anchor, SOURCE
  11 re-anchored, exactly one miss, accounting closes.
- **M** — dynamic source period (33.33 → 40.00 ms mid-run): phase-1 delay
  2·P1, phase-2 delay 2·P2 from the first post-step slot, no drops, bound
  respected, offered = accepted + refused.
- **N** — OFF/ON restart: epoch-1 anchor, OFF drain at the rule-2 anchor,
  re-enabled epoch resolves only its own epoch, SOURCE 21 re-anchors at
  `capture + 2·period`, no drops, accounting closes.

Plus the dispatch-trace recorder and the two-phase/slow-pair scenario
machinery. Full 45-case suite: **45/45 on all four builds** (§4).

---

## 11. Cross-queue synchronization

Implementation (preferred direction from the brief): one **MTLSharedEvent**
ring, 64 slots, per-slot generation = `sequence/64 + 1`. The producer's capture
blit (inside the title's command buffer, `r133CommitCaptureForCommandBuffer`)
encodes a **signal** on its slot after the blit; the pump's presentation
command buffer (`r123PresentItem`, SOURCE items only) encodes
`encodeWaitForEvent:value:` **before** its render encoder samples the private
copy. Metadata carried: the event ring only (the generation is derived from the
sequence). No CPU blocking, no display wait, no drawable retention.
Env-gated: `FGMETAL_R135_CAPTURE_SYNC=0` disables both encodes (pipeline-only
measurement).

Evidence (session A2, whole battery) **[MEASURED]**:
`syncSignals=38941 syncWaits=29158 syncWaitSkips=0 pumpUseViolations=0
pumpUseMinMarginNs=75000` — every SOURCE presentation waited on its capture's
signal; min encode-time margin 75 µs across 29 158 samples (R134: 8 µs). The
cadence result in §9 was produced **with** the edge active; the isolation
window (edge disabled) is §11.1.

**§11.1 — sync-off comparison window:** recorded after this report's session B
(see the addendum block at the end of this file for the measured numbers and
verdict).

---

## 12. ON/OFF regression, fault injection, soak (session A2)

`live-r135/observer.5823.log` — one process, four ON legs, zero
`kIOGPUCommandBufferCallbackErrorSubmissionsIgnored`:

| Leg | Pairs | Presented | Share |
|---|---|---|---|
| ON 1 (cadence gate) | 2 780 | 2 624 | **94.4 %** |
| ON 2 (cycle) | 3 402 | 3 214 | **94.5 %** |
| ON 3 (cycle) | 2 444 | 2 339 | **95.7 %** |
| ON 4 (soak, 12 min 28 s) | 19 342 | 17 384 | **89.9 %** |

- **ON/OFF ×3** — every transition completed (driver timeline
  `cycle-soak-timeline.txt`), periodic summaries flowed through every leg, no
  deadlock, phase re-locked each ON (each window ≥ 94 % except the soak).
- **Fault injection** — `R127_GENERATION_LOSS=ARMED … pairs=60` at soak+5 min;
  the two post-injection minutes show miss deltas of +370 and +509 against a
  ~80–90/min ambient (≈ +710 excess over ambient, comfortably absorbing the 60
  injected pairs); `droppedSource=0` and `orderViolations=0` across the whole
  session; share recovered to 94.8 % in the post-injection periodic delta.
- **Soak** — 12 min 28 s of real gameplay with the title's own rate swinging
  30–66 ms (clamped): 19 342 pairs, 17 384 midpoints presented (89.9 %),
  `droppedSource=0`, `orderViolations=0`, `drawableFailures=0`,
  `presentFailures=0`, `maxDepth=8`, process stable, estimator
  `captureFailed=0`, `estimatorLate=0`, `pendingExpired=0` throughout.
- **Slow-source behaviour** — the share tracks volatility, not the period: the
  capture-anchored schedule keeps presenting through 54 ms and 66 ms windows
  (which the pre-R135 schedule could not: R134's 23.8 % at 38–45 ms).

---

## 13. Display latency (required)

`R127_LATENCY_SUMMARY`, cadence window, n = 2 797 SOURCE / 2 622 INTERNAL
**[MEASURED]**:

| Interval | median | p90 | p99 |
|---|---|---|---|
| SOURCE present-hook → physical display | **101.60 ms** | 109.56 | 143.26 |
| INTERNAL WSI dispatch → physical display | 29.26 ms | 37.18 | 46.65 |

R133 baseline (present-hook → display, title-direct SOURCE): 36.95 / 47.00 /
54.10 ms — 1.073 source periods, a *title-direct* path, not the capture+P
schedule. **[DERIVED]** delta = **+64.6 ms median ≈ one source period + photon
lag** (2P pipeline position + pump-path dispatch→photon − the title-direct
photon tail). `[INTERPRETATION]` this is the priced tradeoff the brief
required: the pipeline buys ~94 % interpolation for one source period of added
SOURCE display latency. INTERNAL frames carry ~29 ms of dispatch→photon latency
on top of their ~51 ms pipeline position.

---

## 14. Bounded diagnostics

Caps behaved as designed (`healthyLogSuppressed=853k/122k per window`,
`anomalyLogSamples` growing slowly with session length, periodic summaries
uncapped and complete). The R134 prefix-registration defect stayed fixed; the
R135 records (`R135_CAPTURE_SYNC`, R135 driver lines) are bounded. The
`SUMMARY`-while-OFF defect was **not** touched (per the brief); one related
driver-side observation: a `SUMMARY` sent with no sleep before `OFF` truncates
the final emission (the soak leg's final pump summary was cut by the immediate
OFF) — recorded for later hardening, drivers now avoid it or accept periodic
coverage.

---

## 15. Final independent review

Dispatched after the soak: fresh GLM-5.3 Flash (Max), read-only. It re-derived
the schedule from the shipped code, diffed both changed files against the R134
snapshot, and recomputed every load-bearing number from the logs. Answered all
14 brief questions YES (stability, fixed latency, ordering incl. catch-up and
period-swing inversions, SOURCE cadence, exactly-once, miss/ON-OFF phase
safety, bounded queueing, dynamic period, sync correctness incl. the env gate
and allocation-failure path, no new latency blocker, invariants by diff,
R126-class cadence recomputed per window, latency acceptable). Both leaned-on
claims verified: the session-B share difference is estimator-production-, not
sync-driven (schedule lock identical at 84 ms anchors; W +10.6 ms from scene
load, all upstream of the edge), and the retain-by-reference snapshot cannot
be corrupted at pipeline depth ≤ 8.

**FINAL-REVIEW = PASS.** Findings (all LOW/report-hygiene, all applied):
archive `observer.4381.log` into the evidence tree (done); relabel the
`measuredSourceDelayNs` figure as selftest-asserted with the latency records
as the field evidence (done); drop "exactly" from the §13 latency-delta
sentence (done); clarify H as the fixed 60 Hz half-slot quantum (done);
correct the fault-injection minute attribution (done). Monitor-items recorded:
the 3 s-bounded `copy-timeout` path under host GPU failure (fails safe, 0
occurrences), post-hitch one-frame latency spike, steady-state `orderGaps ≈ 0`
as a health signal, and the theoretical ≥512-pair-late-note ring-reset path
(unreachable at pipeline depth ≤ 8).

---

## 16. What was NOT tested

- Frame-capture characterization of the user-visible glitches (which of miss
  boundary / estimator history reseed / hitch recovery each artifact is).
- INTERNAL hook→photon latency (only dispatch→photon exists; INTERNAL frames
  have no original hook).
- 22–26 fps slow-source cadence as a *target* (the soak covered it; the brief
  forbids using it as the gate).
- Memory growth across the soak (no cheap reliable instrument, unchanged from
  R134).
- Window resize / display change / multi-monitor; game restart mid-session;
  ON during a title hitch worse than 4H (clamp behaviour untested beyond the
  selftest).
- The `SUMMARY`-while-OFF defect (recorded, not fixed).
- GPU-host fragility (host-level, outside the runtime).

## 17. Exact remaining blocker

**Alpha-blocker:** user-visible artifacts at interpolation miss boundaries and
after capture hitches (~5–10 % of pairs in volatile windows; the miss itself is
handled safely, but the visual transition — estimator history reseed, one-frame
latency spike — is not yet characterized or smoothed).

**Host-blocker:** `kIOGPUCommandBufferCallbackErrorSubmissionsIgnored`
recurred in 1 of 2 post-soak sessions (invalidated; preserved as
`observer.10431.gpu-failure.log`). This is a host/driver state, not an FG
scheduling failure — R134 saw it on 3 of 4 sessions — but it gates any long
measurement and, if it reaches players, the product.

## 18. Smallest next product step

1. Finish the §11.1 sync-off comparison window (in flight).
2. Characterize the miss-boundary artifact with frame captures (bounded
   diagnostics run), then decide whether the fix is estimator history handling
   (likely) or presentation (unlikely).

---

## 19. Reproduction

```sh
# build + gates
cd experiments/metalfx_frame_interpolator_R93
FGMETAL_R127_BUILD_DIR=r135-evidence/pipeline-only-build \
  r127-alpha-runtime/build_r127_tools.sh
FGMETAL_R130_BUILD_DIR=r135-evidence/pipeline-only-deterministic \
FGMETAL_EVIDENCE_SELFTEST=1 FGMETAL_R123_PUMP=1 FGMETAL_R124_TEST=0 \
  r127-alpha-runtime/build_r130_observer_integration.sh

# live (Highball; the user navigates into the open world)
FGMETAL_R135_CAPTURE_SYNC=0 python3 r135-evidence/tools/r135_bottle_env.py \
  prepare --label r135-live-02-syncoff
# Highball → eldenring → Play, then:
r135-evidence/tools/drive_r135_window.sh \
  /tmp/fgmetal-r108-unattended/observer.<pid>.log \
  r135-evidence/live-r135/<name>-timeline.txt 90
python3 r135-evidence/tools/analyze_r135.py \
  r135-evidence/live-r135/observer.<pid>.log
# restore
python3 r135-evidence/tools/r135_bottle_env.py restore \
  --label r135-live-02-syncoff
```

## 20. Clock domains

Unchanged from R134 §25: all pump/slot arithmetic is `CLOCK_MONOTONIC` ns;
`R134_CRITICAL_PATH` intervals are monotonic-to-monotonic;
`estimatorGpuExec` is media-to-media (`GPUStartTime/GPUEndTime`);
`R127_LATENCY_SUMMARY` is host-seconds (`CACurrentMediaTime` vs
`presentedTime`). No cross-domain subtraction is reported as an interval.

---

## ADDENDUM — §11.1 sync-off comparison window (measured)

Session B (`live-r135/observer.11212.log`, clean host, 0 GPU errors, 90 s
generating, `FGMETAL_R135_CAPTURE_SYNC=0`, syncSignals=0 syncWaits=0 — the
gate verifiably disabled both encodes):

```
sourceEnqueued=2740  sourcePresented=2738  internalEnqueued=2734
internalPresented=1829  deadlineMisses=878  droppedSource=0
orderViolations=0  drawableFailures=0  presentFailures=0  idleSlots=18
sourcePeriodNs=34341000  pumpUseMinMarginNs=188000
```

INTERNAL/SOURCE = 1829/2734 = **66.9 %** (vs 93.8–94.4 % in the sync-on
cadence windows).

**[MEASURED]** The schedule locked identically without the edge:
`captureCompleteToIntendedSlot` median 83.97 ms (= `capture + 2P + H`, the
same slot structure as the sync-on windows), SOURCE latency and period
unchanged, zero violations, zero drops, encode-time cross-queue margin
≥ 188 µs. **[MEASURED]** The share difference is W-driven, not sync-driven:
in this window the title renders a far heavier scene — MetalFX median
11.68 ms (vs 6.22–7.74 ms in the sync-on windows) and
`captureCompleteToInternalReady` median 33.3–38.6 ms with p99 50.1 ms,
i.e. the serial compute approaches the 51.3 ms budget, so ~32 % of midpoints
miss. **[INTERPRETATION]** No mechanism exists by which the pump-side wait
could reduce estimator or MetalFX GPU time (it sits strictly downstream, on
`r123Queue`, satisfied ~67 ms before the pump's present in steady state); the
two windows differ in scene load, which is the confound.

**Verdict:** the pipeline's cadence margin survives without the edge
(graceful degradation to 2/3 interpolation under W pressure, schedule lock and
all invariants intact), and the edge is cadence-neutral by structure and by
the identical lock signature. **`CROSS_QUEUE_CAPTURE_SYNC = VALIDATED`** —
with the honest caveat that a same-scene A/B was not run (the scene changed
between sessions and cannot be reproduced on demand); the residual risk it
closes (GPU-side ordering of the capture copy) is the reason it ships enabled.
The final review additionally verified: the missing third of midpoints in the
sync-off window never existed (estimator non-production — ~97 of 292 sampled
missed pairs never started generation), while resolved midpoints presented at
1829/1831 — the pump presented essentially every image it was given. One new
bounded failure mode is documented: if the host's capture command buffer never
completes (the SubmissionsIgnored state), the presentation buffer's GPU wait
converts to the pump's existing 3 s-bounded `copy-timeout` drop — fails safe,
0 occurrences in either session.

Host-health note: sessions B attempt 1 and 2 entered
`kIOGPUCommandBufferCallbackErrorSubmissionsIgnored` during loading
(preserved as `observer.10431.gpu-failure.log`, `observer.10839.gpu-failure.log` —
the R134 §12 host failure mode recurring); attempt 3 after a 2-minute
cooldown was clean and is the reported window.
