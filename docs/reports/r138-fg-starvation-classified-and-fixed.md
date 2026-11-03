# R138 — The 1.27 s FG-only starvation burst: classified, fixed, and alpha-validated

Evidence snapshot: 2026-11-03 (Asia/Baku). R138 loaded the R137 final10 diagnostic build
live, captured a severe FG-only outage with the full final10 trace, identified the exact
failed transition, fixed it with the smallest measured change, and passed the full
deterministic + live battery. This report supersedes nothing; R135–R137 reports remain
valid as history.

## 1. Executive verdicts

```text
PUMP_STARVATION_CAUSE        = S1 GENERATION-CHAIN CASCADE — generation-slot-busy skip of a
                               capture's generation appends a history gap reset; the next
                               real pair seeds (previousCaptureSequence=0) and the provider
                               seeded MetalFX history and then discarded its own valid A->B
                               interpolation output (NO-OUTPUT by design). Each skip thus
                               destroyed TWO midpoints, and under capture-cadence jitter the
                               pattern chained (skip -> reset -> seed -> NO-OUTPUT holds the
                               slot ~22 ms -> next capture skips again). Live severe event:
                               3 seeds + 3 NO-OUTPUTs, 5 consecutive unproducible midpoints,
                               216.663583 ms of SOURCE-only display.
PUMP_STARVATION_RECOVERY     = VALIDATED — the fix makes the seed pair yield a real frame;
                               recovery after any history reset is now 1 pair, automatic.
INTERPOLATION_OUTAGE_BURSTS  = VALIDATED — post-fix 12-min soak: FG-only p50 66.7 ms,
                               p90 100.0 ms, p99 116.7 ms, max 183.3 ms, >100 ms = 13,
                               >250 ms = 0, >500 ms = 0, max 2 consecutive missing slots.
R126_CLASS_CADENCE           = NOT_MEASURED — the whole round stayed in the heavy area;
                               measured source period oscillated 30–67 ms (~22 fps average),
                               no clean 29–30 fps window occurred.
RUNTIME_FG_ENABLE_DISABLE    = VALIDATED — three ON cycles (epochs 2/5/8) in the post-fix
                               session, every drain completed, droppedSource=0,
                               orderViolations=0.
FG_FAILURE_FALLBACK          = VALIDATED — 60-pair bounded INTERNAL-loss injection: 60/60
                               refused, R127_GENERATION_RECOVERED recovery=automatic,
                               droppedSource=0.
D3DMETAL_FG_ALPHA_SOAK       = VALIDATED — 12-minute post-fix heavy-area soak, zero GPU
                               failures, zero severe-trace promotions, bounded queues.
FG_DISPLAY_LATENCY           = NOT_MEASURED — no clean ~34 ms source-period window existed
                               in any R138 window; the measurement remains open.
EDGE_HAZE                    = NOT_CHARACTERIZED — explicitly out of scope this round.
D3DMETAL_FG_ALPHA_RUNTIME    = VALIDATED
```

The R138 live A/B is at the dylib level: pre-fix window loaded the R137 final10 build
(`ad735b53…`), post-fix windows loaded the R138 build (`6e09ce65…`); both hashes are
verified in the observer `LOADED` lines.

## 2–3. Model assignments and rationale

Executed on Zcode with GLM-5.3 Flash (Max reasoning) as the root orchestrator: live
starvation classification, concurrency/state interpretation, trace reconstruction, fix
selection, live-gate decisions, and this report. Mechanical work (log extraction scripts,
bottle staging, drive windows, suite runs) was done directly by the root using the shell —
no High worker agents were spawned, because the analysis required the full trace-schema
context in one head and the mechanical steps were each single commands. One fresh
GLM-5.3 Flash (Max) read-only reviewer ran after live validation (§29); its corrections
were applied before this report was finalized. No second Max arbitration was needed:
the final10 trace discriminated the causes directly, as designed.

## 4. Files changed

- `experiments/metalfx_frame_interpolator_R93/MetalFXFrameInterpolationProvider.m` — THE FIX.
  After a successful history-priming commit, `self.hasValidHistory = YES` (one assignment
  + comment); the now-dead post-encode discard branch (`if (!self.hasValidHistory) { …
  return nil; }`) removed with its stale rationale. Nothing else in the file changed.
- `experiments/metalfx_frame_interpolator_R93/MetalFXFrameInterpolationProvider.h` —
  doc comments only (`invalidateHistory` contract and the class-level contract updated to
  the post-fix behavior).
- `experiments/metalfx_frame_interpolator_R93/provider_smoke.m` — expectation updated:
  the first call after creation now yields a frame (id 1), the second id 2.
- `experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/r130_observer_pre_capture_integration.m`
  — new deterministic test `r138RunProviderSeedWarmupProducesFrame` wired into `main`.
- `r138-evidence/tools/{r138_bottle_env.py,drive_r138_window.sh,analyze_r138_window.py}` —
  round tooling (R137-derived; log-side record prefixes remain `R137_*` as emitted by the
  final10/138 build). A provenance-record bug in the first `r138_bottle_env.py` prepare
  (recorded the default dylib instead of the staged one) was fixed after r138-live-02;
  authoritative verification used the staged `environment` object plus the actual file hash.

Untouched: observer, pump, estimator stage, ownership, scheduling, shared-event path.

## 5. Final10 live provenance

| Phase | Process | Dylib (from `LOADED` line) | SHA-256 |
|---|---|---|---|
| Pre-fix heavy-area capture (heavy-01) | pid 70959 | `r137-evidence/r137-build-final10/libfgmetal_r127_alpha_runtime.dylib` | `ad735b53aa7813854a396be6e45b6297bedcb48501a17c4977315475ccd27fdd` |
| Post-fix gate (heavy-02) | pid 73109 | `r138-evidence/r138-build-postfix/libfgmetal_r127_alpha_runtime.dylib` | `6e09ce65075ea4ee40e56b43115ba4d5a07dac5e99200a93a7818040655ae6af` |
| Post-fix battery + soak (soak-01) | pid 74435 | `r138-evidence/r138-build-postfix/…` (same) | `6e09ce65…` |

Phase-0 checks passed for both sessions: `R137_DIAGNOSTICS=ARMED` (budgets
`6-FG+2-severe+4-title`, traceLimit=1024), `R127_RUNTIME_CONTROL=READY`, pump mode
confirmed, FG ON at epoch 2 in each session, SOURCE/INTERNAL presentation active. The
user navigated the game into the heavy area manually (Highball launches only). Bottle
staging: `launch-preparation/r138-live-0{1,2}/preparation.json`; the bottle was restored
to its pre-round bytes after the title exited (sha-verified).

## 6. R137 baseline (carried)

R137's 11m06s final9 heavy-area run: 299 FG-NO-INTERNAL intervals, p50 83.332 ms,
p90 116.665 ms, p99 199.998 ms, max **1.266652 s**, >250 ms = 1, >500 ms = 1; the event
(35 missing slots) could not be classified because final9 could not distinguish provider
no-output from placeholder loss from a presentation-stage failure. R138's job was to
capture that class with final10 and identify the exact failed transition.

## 7. The final10 severe-event trace (heavy-01, outage index 91)

4-minute focused heavy-area window, 240 s, no GPU errors. 218 closed intervals: 27
FG-NO-INTERNAL, 191 TITLE-LOW-FPS. The severe event (per the R137 severe-burst criteria:
>2 consecutive missing INTERNAL slots) triggered the reserved FG-SEVERE trace budget
(`detailTrigger=sustained-outage-100ms detailBudget=FG-SEVERE`) and flushed 570 records
(512-record ring context + the outage window). Outage record: `durationNs=216663583`,
`missingInternalSlots=5`, `sourceFrames=7`, `generationAttempts=6`,
`generatedInternals=3`, `internalEnqueued=7`, `internalResolved=3`,
`internalDropped=5`, `deadlineMisses=5`, `titleDirectFrames=0`, `maxSourceGapNs`
≈ 50 ms, max queue depth 8, max reservations 4, oldest committed age ≈ 104 ms.

Display anatomy (presentedTime, outage-relative): one INTERNAL, then SEVEN consecutive
pump-presented SOURCE frames at 16.7/33.3/16.7/16.7/50.0/33.3 ms spacing, then the closing
INTERNAL. The pump presented every slot correctly — S3/S4/S5 exonerated by the trace.

Generation anatomy (all times outage-relative, presentedTime clock):

| pair | capture B complete | generation entry | estimator wake | MetalFX | outcome |
|---|---|---|---|---|---|
| (0,8670) | −30.95 ms | −30.81 (flags=0, SEED) | — | — | no interpolation attempted |
| (8670,8671) | +2.91 | +3.02 (flags=1) | +18.45 | start +18.48 | **NO-OUTPUT** +25.72 |
| (8671,8672) | +25.71 | **NONE** (skipped) | — | — | generation slot still held by (8670,8671) |
| (0,8673) | +60.26 | +60.38 (flags=0, SEED) | — | — | no interpolation attempted |
| (8673,8674) | +94.70 | +94.82 (flags=1) | +110.61 | start +110.64 | **NO-OUTPUT** +117.08 |
| (8674,8675) | +128.63 | +128.76 (flags=1) | +144.77 | start +144.85 | FRAME +149.92 → resolved 3652 +149.95 |
| (8675,8676) | +162.40 | +162.52 (flags=1) | +178.15 | start +178.18 | FRAME +183.36 → resolved 3653 |
| (8676,8677) | +195.86 | +195.98 (flags=1) | +211.22 | start +211.25 | FRAME +216.37 → resolved 3654 |

The recovered M(8674,8675) was ready at +149.95, its (compressed-period) slot at
≈ +154.5, but the pump's sleep returned at +182.6 — a **+28 ms wake overshoot**
(pump thread runs at DEFAULT QOS, `R127_PUMP_THREAD_QOS configured=UNSPECIFIED
actual=DEFAULT relativePriority=0`); the internal was then displayed at +216.66.
Not fixed this round (see §29 finding c).

## 8. Provider-result classification (final10's discriminator)

`METALFX_COMPLETE` eventDetail over detailed traces: pre-fix heavy-01 = 98 completions,
5 NO-OUTPUT + 93 FRAME (reviewer-corrected count; my first pass said 6), 3 of the
NO-OUTPUTs inside outage 91; every NO-OUTPUT carried **no error** — matching the
provider's by-design discard path. Post-fix = 358 completions across heavy-02 + soak-01,
**358 FRAME, 0 NO-OUTPUT**. The R137 open question ("provider returned no frame vs
error") is answered: no frame, no error, on the first pair after every history reset.

## 9. Placeholder / pairing classification

S2 is excluded: in outage 91, `INTERNAL_RESOLVED` succeeded for every generated frame
(presentIds 3652/3653/3654), placeholders existed and resolved, and the closing INTERNAL
presented normally. Post-fix, `internalResolved` continued to match `generatedInternals`.
No endpoint mismatch was observed in any R138 trace.

## 10. Presentation-stage classification

S4 is excluded: 39 WSI presents in outage 91 all issued and completed (present ids
12313–12320 contiguous), `PRESENTATION_STAGE` dispositions were uniformly `PENDING-COPY`
(the normal pre-copy state at capture commit), `drawableFailures=0`, `presentFailures=0`
in all periodic summaries, and no `WSI_ISSUE` carried a failure disposition.

## 11. Pump progress classification

S3 is excluded: 8 pump claims in the outage window, every slot produced a present, queue
depth stayed ≤ 6, head/claimed advanced in order. The pump presented SOURCE for every
slot that lacked a ready internal (correct R136 drop-guard behavior, including one
deferred re-fire 2.07 ms after its base slot).

## 12. First divergent transition

```text
EXPECTED: capture B completes -> estimator stage dispatches generation for (A,B)
          -> provider interpolates -> INTERNAL resolved -> pump presents M at its
          capture-anchored slot.
ACTUAL:   capture 8672 completed 22.8 ms after capture 8671 while the serial generation
          slot was still held by 8671's generation (estimator ~15.5 ms + MetalFX ~7 ms)
          -> generation SKIPPED and a history gap reset queued
          -> capture 8673 entered as a SEED (previousCaptureSequence=0, flags=0)
          -> pair (8673,8674) reached MetalFX, which seeded history and returned
             NO-OUTPUT for the pair that should have produced M(8673,8674).
FIRST DIVERGENCE: capture-complete(8672) -> generation dispatch. The dispatch was
          refused by the busy generation slot; everything downstream (seed, NO-OUTPUT)
          followed deterministically from that refusal plus the provider's discard rule.
```

## 13. Measured starvation root cause

Code-confirmed chain (all sites read in the post-fix tree):

1. `r133DrainPendingLocked` (observer `r127-alpha-runtime/observer-title-inprocess-r127.m`
   ≈ :4824-4837): when `r122TryAcquireGenerationSlot()` fails, the generation is skipped,
   `r132PairsSkippedBusy` increments, and the action list gets `gapReset = YES`.
2. `r132QueueHistoryGapReset` (:4637) → `r132ResetEstimatorHistoryOnQueue` (:4624):
   `[realFrameProvider invalidateHistory]` + `previousCaptureSequence = 0`.
3. Pre-fix `MetalFXFrameInterpolationProvider.m interpolatePreviousColor:…`: with
   `hasValidHistory == NO`, a warmup pass seeds MetalFX history with A; the real A→B
   pass then encodes and completes successfully; the code then executed
   `if (!self.hasValidHistory) { self.hasValidHistory = YES; return nil; }` — discarding
   the valid interpolation output of the seed pair.
4. Net effect: every slot-busy skip cost TWO midpoints, and because the NO-OUTPUT
   generation still held the serial slot ~22 ms, a capture cadence below that
   (measured 22.8 ms interval in the heavy area, vs ~33.5 ms nominal) repeated the skip.
   Outage 91 shows the chain running three times (skips of 8664/8669/8672 per the
   independent reviewer's reconstruction; seeds 8665/8670/8673; NO-OUTPUTs
   8666/8671/8674).

Classification against the brief's classes: **S1** (no INTERNAL output produced), with a
precise three-step measured mechanism; not S2/S3/S4 (§9–11), not S5 (titleDirectFrames=0
in the event), not S6 (no long shared-event wait), not S7 (SOURCE stream gaps of 33–50 ms
contributed the trigger cadence but the pump presented sources throughout), not S8 (no
host GPU failure).

## 14. Deterministic pre-fix reproduction

New test `r138RunProviderSeedWarmupProducesFrame` (in the actual-observer integration
suite): real MetalFX provider on a real MTLDevice, textures built per the provider's
usage probe, then `invalidateHistory` → first interpolation attempt. The pre-fix build
FAILS exactly as the live cascade:

```text
r138-evidence/r138-observer-integration-prefix.log (regenerated from the preserved
sha-pinned pre-fix binary in r138-observer-integration-prefix/):
R138_PROVIDER_SEED_WARMUP FAIL reason=first-pair-after-reset-no-output frame=nil error=none
exit 1
```

The test also asserts history-validity transitions, monotonic internalPresentId
continuation across the pairs, and a SECOND reset (the live cascade chained through
repeated resets).

## 15. Fix implemented

`MetalFXFrameInterpolationProvider.m`, inside `interpolatePreviousColor:…`: immediately
after the history-priming command buffer completes successfully, `self.hasValidHistory =
YES`. The same call's real A→B pass — already encoded unconditionally right after — then
returns a genuine INTERNAL frame. The post-encode discard branch was deleted (it became
unreachable; the only remaining `hasValidHistory = NO` paths are the documented failure
paths). No estimator, MetalFX descriptor, anchor, ownership, or scheduling change.
Pairs-to-recovery after any history reset: 1 (the seed pair itself now yields).

## 16. Deterministic post-fix result

```text
r138-evidence/r138-observer-integration-postfix.log:
R138_PROVIDER_SEED_WARMUP PASS first-pair-yields-frame identity=1->2->3 historyValid=1
47/47 result rows PASS, exit 0
```

## 17. Current regression suite results

| Suite | Result |
|---|---|
| R127 diagnostics selftest | PASS cases=39 |
| R123 pump selftest | PASS cases=19 |
| R130 actual-observer integration (incl. new R138 case) | 47/47 rows PASS |
| R137 trace-selection selftest | PASS cases=34 |
| provider_smoke (new contract) | PASS (establishment frame yielded, readback valid) |

All R136-era invariants re-verified in-suite (capacity pressure, backlog, ownership
ledger lifetime, lookup race, OFF-with-queued/claimed SOURCE, stale epoch, repeated
transitions). Counts grew by the one new case; no assertion was weakened.

## 18. Short post-fix heavy-area result (heavy-02)

4-minute focused window in the same heavy-area class, same drive script and analyzer:
246 closed intervals (25 FG-NO-INTERNAL, 221 TITLE-LOW-FPS). FG-only p50 66.7 ms,
p90 100.0 ms, p99 100.0 ms, **max 100.0 ms**, >100 ms = 0. Max consecutive missing
INTERNAL slots 2. Queue maxima: backlog 4, depth 8, reservations 4. One detailed FG
burst captured; its trace contains no NO-OUTPUT and no seed-chain.

## 19–20. Before/after outage distribution (>100/250/500 ms)

| Window | Build | FG-only count | p50 | p90 | p99 | max | >100 ms | >250 ms | >500 ms | max missing slots |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| R137 heavy-07 (11m06s) | final9 | 299 | 83.3 | 116.7 | 200.0 | **1266.7** | 33 | 1 | 1 | 35 |
| R138 heavy-01 (4m, pre-fix) | final10 | 27 | 66.7 | 83.3 | 216.7 | **216.7** | 1 | 0 | 0 | 5 |
| R138 heavy-02 (4m, post-fix) | r138 | 25 | 66.7 | 100.0 | 100.0 | **100.0** | 0 | 0 | 0 | 2 |
| R138 soak-01 (12m, post-fix) | r138 | 276 | 66.7 | 100.0 | 116.7 | **183.3** | 13 | 0 | 0 | 2 |

Primary acceptance target met: the unexplained long FG-caused burst is gone (no ≥250 ms
event in any post-fix window; the 1.27 s class has no surviving mechanism — §29 Q4).
The 13 soak events >100 ms were individually verified: all have a stretched threshold
period (43–67 ms, the title's own capture stream slowing), `missingInternalSlots=0`,
`deadlineMisses=0`, and successful generation — title-slowness boundary intervals, not
FG starvation. The residual 66–100 ms cadence holes are the known R136-class deadline
misses (deadlineMisses=137 over the soak, ≈0.54 % of 25390 enqueued internals).

## 21. Target-rate cadence

NOT_MEASURED. Every R138 window was heavy-area; measured source period oscillated
30–67 ms (~22 fps average, 25395 sources presented / 25209 internals presented over the
post-fix session = 99.27 % INTERNAL/SOURCE lifetime share). No clean 29–30 fps window
occurred; none was manufactured.

## 22. ON/OFF ×3

Post-fix session (pid 74435): ON epoch 2 → OFF (drain, source-only-60hz epoch) → ON
epoch 5 → OFF → ON epoch 8, each ON verified by its `R127_RUNTIME_TRANSITION` record and
each state read back via SUMMARY. Every drain completed; droppedSource=0;
orderViolations=0 across the session; stale-capture rejection remained armed
(staleCallbackRejections 0→6 across transitions). Matches R136's battery shape exactly
(epochs 2/5/8). A first battery attempt was aborted when the user quit the game after
the heavy-02 gate; it was rerun cleanly in the pid-74435 session.

## 23. Failure-injection regression

`/tmp/fgmetal-r127/fault-pairs` ← `60` while generating (epoch 8):
`R127_GENERATION_LOSS=ARMED requestId=1 pairs=60`; 60/60 generated internals refused
before the pump (`R127_GENERATION_REFUSED`, 50 per-pair lines logged, the remainder
bounded-sampled — the summary counter `refusedPairs=60` is authoritative), then
`R127_GENERATION_RECOVERED requestId=1 … refusedPairs=60 recovery=automatic`.
SOURCE continuity: sourcePresented advanced monotonically through the injection,
droppedSource=0, no endpoint corruption, no persistent starvation. Pairs-to-recovery: 1
pair after the 60th refusal.

## 24. Full post-fix soak (12 minutes, heavy area)

1020 closed intervals: 276 FG-NO-INTERNAL, 744 TITLE-LOW-FPS. FG-only: p50 66.7,
p90 100.0, p99 116.7, max 183.3 ms; >100/250/500 ms = 13/0/0; max 2 consecutive missing
slots; total FG-only outage time 21.7 s over 12 min. TITLE-LOW-FPS kept separate: 744
intervals, p50 100.0 ms, max 1283.3 ms (title hitches; includes the largest SOURCE gap
class). Zero severe-trace promotions (no event met >2-slots/≥250 ms). Zero
`captureFailed`, zero GPU failures. Queue maxima: backlog 5, depth 8, reservations 5;
oldest committed age max 149 ms (belongs to title-hitch intervals).

## 25. Target-rate display latency

NOT_MEASURED — no clean ~34 ms source-period window existed in any R138 window. The
measurement remains open; R135's reference (101.60 / 109.56 / 143.26 ms) still stands.

## 26. Cross-queue synchronization

Unchanged and active. `FGMETAL_R135_CAPTURE_SYNC=1` throughout; no
`SHARED_EVENT_WAIT_ENCODED` anomalies in any trace; the provider's warmup runs on the
provider's own private command queue inside an already-blocking call, adding no new wait
class and never touching the pump's shared-event ring (independent review, Q9 PASS).

## 27. Edge haze

`EDGE_HAZE = NOT_CHARACTERIZED`. No image-quality work was mixed into the starvation fix.
One bounded consequence of the fix is deliberate: the first frame after a history reset
is now a real MetalFX interpolation rather than nothing; if MetalFX's first post-reset
interpolation shows slightly elevated ghosting, that is a next-round image-quality
characterization item, and it trades against a previously guaranteed missing frame.

## 28. Host GPU failure observations

Zero `kIOGPUCommandBufferCallbackErrorSubmissionsIgnored` in all three R138 logs. No
window was invalidated.

## 29. Fresh independent review

A fresh GLM-5.3 Flash (Max) read-only reviewer audited the mechanism, the diff, the
evidence, and the 16 required questions: **R138_INDEPENDENT_REVIEW = PASS** (16/16).
Selected findings and their disposition:

- **MEDIUM (evidence hygiene):** the pre-fix integration log had not been saved to disk.
  Fixed: `r138-observer-integration-prefix.log` regenerated by running the preserved
  sha-pinned pre-fix binary (exit 1, FAIL line present).
- **LOW counts corrected:** pre-fix NO-OUTPUT total is 5 (not 6); post-fix trace
  completions are 358 all-FRAME (not 79 — my count covered only heavy-02's four detailed
  outages). Both corrected here.
- **LOW stale doc:** `MetalFXFrameInterpolationProvider.h` still described the old
  discard contract; fixed (comment-only; dylib hash unchanged, no gate rerun needed).
- **LOW strays:** duplicate analysis files at the repo root removed; canonical copies
  live in `live-r138/`.
- **CONCERN (accepted for alpha):** the +28 ms pump wake overshoot (DEFAULT-QOS pump
  thread) was observed but not fixed; with >250 ms = 0 in every post-fix window
  including this overshoot, it does not independently threaten the burst class. QOS
  elevation is a cheap, well-scoped follow-up.
- **LOW:** 10 of 60 injection refusal lines were budget-sampled out; counters
  authoritative. **LOW:** the generation-slot skip has no dedicated trace point (the
  skip is identified structurally: capture-complete without a following
  GENERATION_ENTRY); an explicit skip trace point is a next-round diagnostics nicety.
- The reviewer's Q4 residual: a sustained capture cadence below the ~22 ms serial
  generation hold would still seed every pair (observer-level seeds never call
  MetalFX); observed capture intervals only transiently dipped to ~17.9–22.8 ms, and
  no post-fix window produced >2 consecutive missing slots. Bounded and monitored.

## 30. What was NOT tested

- Clean target-rate cadence and hook→physical-display latency (no qualifying window).
- Edge-haze image characterization.
- A capture-stream collapse comparable to R137's 1.1 s SOURCE gap while FG is ON
  (title hitches occurred but always classified TITLE-LOW-FPS; no deliberate
  worst-case load was staged).
- Sustained sub-22 ms capture cadence (the reviewer's Q4 residual).
- Offline/EAC, thermal-controlled A/B (user reported throttling suspicion in R137 —
  untested, user observation only).
- macOS releases / GPUs other than this M3 host.

## 31. Alpha blockers vs future optimizations

**Alpha blockers: none.** `D3DMETAL_FG_ALPHA_RUNTIME = VALIDATED` under the brief's
acceptance: mechanism proven and fixed; no FG-caused ≥250 ms (hence ≥500 ms) event in
the post-fix soak; outages bounded and self-recovering; SOURCE exactly-once intact;
ON/OFF ×3 pass; failure injection recovers automatically; 12-minute soak clean;
p50/p90 unchanged (no new cadence regression).

Known limitations carried (per the brief, non-blocking): edge haze not characterized;
target-rate latency not measured in R138; isolated 66–100 ms cadence holes remain
(0.5 % class); host-GPU fragility unchanged; pump wake overshoot (~25–28 ms tail) and
the missing skip trace point are cheap follow-ups; sustained sub-22 ms capture cadence
would still seed every pair (monitored, bounded).

## 32. Smallest next product step

Elevate the pump thread QOS (one-line, removes the measured +28 ms wake overshoot tail),
add an explicit generation-slot-skip trace point, and characterize edge haze with the
bounded image set — in that order. A clean target-rate scene should be captured when
naturally available to close `R126_CLASS_CADENCE` and `FG_DISPLAY_LATENCY`.

## Evidence index

- Pre-fix live log + timeline + analysis: `live-r138/observer.70959.heavy-01.log`,
  `heavy-01.timeline`, `heavy-01.analysis.json`
- Post-fix gate: `live-r138/observer.73109.heavy-02.log`, `heavy-02.timeline`,
  `heavy-02.analysis.json`
- Post-fix battery + 12-min soak: `live-r138/observer.74435.soak-01.log`,
  `soak-01.timeline`, `soak-01.analysis.json`
- Deterministic pre-fix FAIL: `r138-observer-integration-prefix.log` (from the preserved
  pre-fix binary `r138-observer-integration-prefix/`, sha-pinned sources)
- Deterministic post-fix PASS: `r138-observer-integration-postfix.log` (+ immutable
  source snapshot directory)
- Pump/diagnostics suites + post-fix dylib: `r138-build-postfix.log`,
  `r138-build-postfix/libfgmetal_r127_alpha_runtime.dylib`
  (SHA-256 `6e09ce65075ea4ee40e56b43115ba4d5a07dac5e99200a93a7818040655ae6af`)
- Bottle staging/restore records: `launch-preparation/r138-live-0{1,2}/`
- R137 baseline: `../r137-evidence/R137-REPORT.md`
