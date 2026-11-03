# R137 — Pump starvation and D3DMetal alpha status

Evidence snapshot: 2026-11-03 (Asia/Baku). This is the authoritative R137 handoff. The captured live run used the final9 observer. The final10 diagnostic build and deterministic integration run were completed afterward; final10 has not been loaded into Elden Ring.

## 1. Executive verdicts

```text
PUMP_STARVATION_CAUSE        = NOT_VALIDATED — one 1.266652 s FG-only outage is measured; persistent S3 commit backlog is unsupported by its bounded queue/age data, but final9 cannot distinguish S1 no output from S2 missing placeholder or an unobserved presentation-stage failure.
PUMP_STARVATION_RECOVERY     = NOT_VALIDATED — no pipeline fix was made and the 1.266652 s outage remains unexplained.
INTERPOLATION_OUTAGE_BURSTS  = PARTIAL — 299 FG-no-INTERNAL intervals; p50 83.332 ms, p90 116.665 ms, p99 199.998 ms, max 1.266652 s; one exceeded 500 ms.
R126_CLASS_CADENCE           = NOT_VALIDATED — this heavy-area window averaged about 22.1 SOURCE fps, not a clean 29–30 fps target-rate window.
EDGE_HAZE                     = NOT_CHARACTERIZED.
RUNTIME_FG_ENABLE_DISABLE    = PARTIAL — one live ON→OFF drain completed.
FG_FAILURE_FALLBACK          = PARTIAL — R136's 60-pair injection passed historically; it was not rerun in R137.
D3DMETAL_FG_ALPHA_SOAK       = PARTIAL — an 11m06s pre-fix heavy-area run exists, but it contains the unexplained 1.266652 s FG-only outage; no post-fix soak exists.
FG_DISPLAY_LATENCY            = NOT_MEASURED — no clean target-rate R137 window or per-window hook-to-display p50/p90/p99.
BOUNDED_ALPHA_DIAGNOSTICS    = PARTIAL — final10 build and deterministic tests pass; its new frame/error/stage classification has not been exercised live.
D3DMETAL_FG_ALPHA_RUNTIME    = PARTIAL — the single concrete blocker is the unexplained 1.266652 s FG-only no-INTERNAL interval under continuing SOURCE cadence; cause and recovery remain unproven.
```

The strict heavy-area window separates **299 FG-NO-INTERNAL intervals** from **511 TITLE-LOW-FPS intervals**. The largest FG-only interval had a maximum SOURCE gap of 49.9995 ms, while the separate 1.383317 s title hitch included a 1.116654 s SOURCE gap. Those are different event classes and are not merged when judging FG starvation.

## 2–3. Codex / GPT-6 Luna assignments and rationale

The supplied execution policy assigns the root to overall starvation analysis and live-gate decisions at GPT-6 Luna xHigh; instrumentation, build and evidence mechanics to GPT-6 Luna High; and a fresh read-only GPT-6 Luna xHigh reviewer after live validation. Max is reserved for narrow unresolved arbitration. Model metadata was not inspected, as the request directs. No R137 Max arbitration is recorded. The R136 event-2 evidence note is read-only analysis, not the required R137 final review.

The R137 final independent review has **not** been completed: final10 is not live, and there is no measured-cause fix to review. The prior R136 report's reviewer is historical and does not satisfy the fresh R137 reviewer requirement.

## 4. Files changed and artifacts produced

R137-specific final10 source changes:

- `r127-alpha-runtime/observer-title-inprocess-r127.m`: diagnostics only. MetalFX completion now distinguishes provider skipped, frame returned, and error; `generatedInternals` increments only when a frame is returned; `PRESENTATION_STAGE` records capture/copy/readiness disposition; non-direct records no longer receive a misleading direct reason; the detail budget reserves two slots for severe FG bursts.
- `r127-alpha-runtime/r130_observer_pre_capture_integration.m`: expands trace-selection and event-classification deterministic cases to 34.

The R137 changes do not alter the presentation schedule, source ownership, estimator, MetalFX inputs, shared-event synchronization, or pump/drop policy. `MetalFXFrameInterpolationProvider.h/.m` have existing unstaged R136 history-validity changes in this worktree; they were not changed for R137.

Build/test artifacts are in `r137-evidence/r127-build-final10.log`, `r137-evidence/r137-build-final10/libfgmetal_r127_alpha_runtime.dylib`, and `r137-evidence/r137-observer-integration-final10.log` plus its immutable source snapshot directory. The dylib SHA-256 is `ad735b53aa7813854a396be6e45b6297bedcb48501a17c4977315475ccd27fdd`. It is staged, not loaded into the game.

## 5. R136 baseline

The authoritative prior report is `../r136-evidence/R136-REPORT.md`. R136 validated the capture-anchored fixed-latency schedule and drop guard, 37/37 diagnostics, 18/18 pump, 45/45 observer cases, three OFF→ON→OFF cycles, 60-pair INTERNAL-loss fallback, shared-event synchronization, and a 12m25s heavy-gameplay soak. It reported lifetime INTERNAL/SOURCE of 97.0%, zero SOURCE drops and order violations, with target-rate windows at 94.9–97.3%.

R136 also contained one approximately 560 ms SOURCE-to-recovery-INTERNAL stretch (called about 550 ms in the report). Its follow-up note, `r137-evidence/r136-event2-review.md`, found pump idle/tick events during part of the interval and concluded that a continuous pump-thread stall was not established. R136 did not sample the commit frontier throughout the interval, so the prior “commit-backlog starvation” explanation was a hypothesis, not a measured root cause.

## 6. Interpolation-outage definition and burst threshold

R137 opens an interval after a physical INTERNAL and tracks subsequent physical output while runtime FG is generating. It closes when the next INTERNAL is physically presented or when the runtime boundary censors the interval. SOURCE frames, missing INTERNAL slots, generation attempts, pump source progress, direct-source frames, queue/commit snapshots and the runtime epoch are accumulated. Durations use `MTLDrawable.presentedTime` mapped to host nanoseconds.

The observer calls an interval `TITLE-LOW-FPS` when the largest observed SOURCE gap, or the current SOURCE age, exceeds **two times the interval's threshold period**. Otherwise, a completed interval is `FG-NO-INTERNAL`. This is the classification used for the split below.

The R137 severe-burst trace rule is more than two missing INTERNAL slots or at least 250 ms of FG outage, unless the interval meets the title-hitch rule. A 100 ms sustained interval, title-direct activity after 50 ms, and committed SOURCE age of 100 ms can promote a bounded trace. Final10 reserves six routine FG traces, two severe FG traces, and four title-hitch traces. The separate `CADENCE_HOLE_EVENT_RATE` remains a midpoint cadence metric; it is not called a visual-glitch rate.

## 7. Pre-fix live outage distribution

The final9 live heavy-area marker spans 16:55:54–17:07:00 local time (11m06s). The analyzer uses only log lines inside those timeline markers. The strict window contains 810 completed intervals and no censored intervals:

| Classification | Count | p50 | p90 | p99 | Maximum | >50 ms | >100 ms | >250 ms | >500 ms | Total time |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| FG-NO-INTERNAL | 299 | 83.332 ms | 116.665 ms | 199.998 ms | 1,266.652 ms | 298 | 33 | 1 | 1 | 26.200 s |
| TITLE-LOW-FPS | 511 | 83.332 ms | 116.665 ms | 1,216.650 ms | 1,383.317 ms | 511 | 54 | 6 | 6 | 52.616 s |
| All completed | 810 | 83.332 ms | 116.665 ms | 233.331 ms | 1,383.317 ms | 809 | 87 | 7 | 7 | 78.816 s |

The FG-only maximum missed 35 consecutive INTERNAL slots. The interval count and all percentiles above are measured from the marked run using nearest-rank p90/p99 and the analyzer's even-safe p50. The source is `live-r137/heavy-07.analysis.json`; the analyzer is `tools/analyze_r137_window.py`.

## 8–9. Starvation timeline and measured cause

The longest FG-only outage is event index 634, observer log line 7182:

```text
duration=1,266,652,000 ns; threshold period=33,297,000 ns
missing INTERNAL slots=35; SOURCE frames=37; pump SOURCE frames=37
maximum SOURCE gap=49,999,542 ns; TITLE-direct frames=0
generation attempts=25; final9 generatedInternals counter=14 (invalid; see below)
INTERNAL enqueued=37; resolved=2; dropped=35; deadline misses=35
max committed backlog=3; max queue depth=5; max reservations=3
oldest committed SOURCE age=80,172,000 ns
```

This is measured FG-NO-INTERNAL behavior while pump-owned SOURCE output continues at a maximum gap of about 50 ms. It is not a title hitch and the event reports no TITLE-direct SOURCE frame. The persistent S3 backlog hypothesis is unsupported for this event: the maximum observed committed backlog was 3 and oldest committed age was about 80 ms. The data do **not** prove S1 or S2, because the final9 provider callback counter did not distinguish “provider returned no frame” from “provider returned an error,” and the largest event exhausted the bounded detail quota before its full pair/stage sequence was retained. It also does not rule out a short unobserved presentation-stage failure. No exact root class is claimed.

Final9's `generatedInternals=14` is not a valid output count: it incremented on provider completion even when no frame was returned. Final10 corrects this and records `FRAME`, `NO-OUTPUT`, `ERROR`, `FRAME-AND-ERROR`, or `PROVIDER-SKIPPED`, plus presentation-stage disposition. Those distinctions are deterministic-tested but not yet live-measured.

The largest separate TITLE-LOW-FPS interval is index 646, line 7220: 1.383317 s total duration, 1.116654 s maximum SOURCE gap, four missing INTERNAL slots, max queue depth 8, max committed backlog 5, and max committed age 1.097417 s. The long SOURCE gap makes this title-caused/low-FPS evidence; it is not counted as an FG-caused 1.383 s stall.

## 10. TITLE-direct-while-ON accounting

The strict analysis sums one TITLE-direct frame inside outage intervals. The final request summary also says `titleDirectWhileOn=1`. However, the marked log contains seven `R137_TITLE_DIRECT_SAMPLE` records (sample indices 2–8), each tagged mode 1 / epoch 2 and reason `capture-image-unavailable`. The bounded sample records and aggregate field do not reconcile into a unique count of physical bypass presentations. Treat the aggregate as unverified until final10 resolves the event-to-WSI relation. None of the direct samples is attributed to the longest outage: index 634 explicitly reports `titleDirectFrames=0`.

The recorded samples show no queued or claimed item in their instantaneous snapshots, so they do not demonstrate committed backlog stranded by TITLE-direct fallback. The direct-path accounting discrepancy itself remains an instrumentation gap.

## 11. Committed-SOURCE backlog accounting

For the longest FG-only event, committed backlog peaked at 3, queue depth at 5, active reservations at 3, and oldest committed age at 80.172 ms. Across all strict-window intervals, the maxima were backlog 5, queue depth 8, reservations 5, and age 1.143122 s. The all-window age maximum belongs to the title-hitch interval, whose SOURCE stream itself had a 1.116654 s gap; it must not be assigned to the FG-only event.

The periodic pump summary reports `sourceDropped=0`, `drawableFailures=0`, `presentFailures=0`, and `orderViolations=0`; `orderGaps=165` is reported separately and is not an order violation. The final periodic snapshot had `captureCommitted=18156`, `captureComplete=18156`, `captureFailed=0`. These counters establish bounded observed queue shape and SOURCE continuity for this run, not the internal cause of index 634.

## 12. Thread and lock evidence

Two bounded `THREAD_STATE_SAMPLE` pump-state records survived in the saved detailed traces, for earlier outage indices 10 and 174. They record recent pump progress and worker-state values; they are not OS thread stacks. The maximum event index 634 has `detailedTrace=0`, so it has no retained thread-state, lock, drawable, shared-event, or presentation-stage sequence. No conclusion that a hard lock or shared-event wait caused the maximum event is supported. No host GPU submission error was observed.

## 13. Fix implemented

No pipeline or scheduling fix was implemented because the cause was not proven. Final10 is a diagnostics-only change. R135/R136 anchors (`capture(n)+2P` and `capture(n)+2P+H`), capture-aware drop guard, SOURCE ownership, bounded pump, and cross-queue shared-event synchronization remain unchanged. No queue was enlarged and no retry or wait behavior was changed.

## 14. Deterministic starvation-specific tests

Final10's deterministic work covers diagnostic selection and classification: event-frame/error states, presentation-stage dispositions, pump events, trace-budget precedence, severe-burst reservation, and direct-path classification. `R137_TRACE_SELECTION_SELFTEST=PASS cases=34`. The integration log also contains pump snapshot tests for reservation, commit, resolve, claim, requeue, dispatch, and drop accounting.

These are instrumentation and state-accounting tests, not a deterministic reproduction/fix test for the measured 1.266652 s runtime event. Since the runtime cause is unknown, the requested cause-specific pump recovery tests and automated ON/OFF-under-that-backlog test remain open.

## 15. Full 37/18/45 regression history and current final10 results

The exact 37 diagnostics / 18 pump / 45 observer suite passed in R136 (`r136-evidence/R136-REPORT.md`). Current final10 updated counts are `R127_DIAGNOSTICS_SELFTEST=PASS cases=39`, pump `SELFTEST=PASS cases=19`, 46 top-level integration results passed, 14 actual-observer cases passed, and the 34-case R137 trace-selection selftest passed. The final10 integration script exited 0; there are no failing test result lines. This is the current updated suite; the historical 37/18/45 counts were not rerun as separate unchanged-count binaries.

## 16–18. Short heavy-area result, cadence, and outage thresholds

The available live run is an 11m06s final9 heavy-area capture, longer than the requested 2–3 minute short gate, but it is a **pre-fix** diagnostic run. In the full-ON summary just after the marked window, physical counts were 14,888 SOURCE and 14,733 INTERNAL. Derived over the approximately 673 seconds to that summary: about 22.1 SOURCE fps, 21.9 INTERNAL fps, and 44.0 total output fps; INTERNAL/SOURCE was 98.96%. These are coarse whole-window derived averages, not a clean target-rate window. `cadenceHoleEvents=150`, `CADENCE_HOLE_EVENT_RATE=150/14888=0.01007523` (about 1.01%). This midpoint metric is separate from the outage distribution in §7.

There is no valid before/after maximum comparison because no pipeline fix was applied. The historic R136 event was about 560 ms SOURCE-to-recovery-INTERNAL; the current final9 maximum is 1,266.652 ms. The measured maximum is therefore larger in R137, but run conditions and instrumentation differ and this is not a controlled regression comparison.

## 19. User fluidity observation

After the run, the user reported that performance was clearly better than a couple of builds earlier and that they did not see the prior hard locks. They also reported more deformation/glitches as frame rate falls and could not distinguish MacBook Air thermal throttling from additional FG load. This is a user observation, not a measured causal attribution. No coolpad/thermal-controlled A/B was run.

## 20–21. ON/OFF and failure fallback

The live final9 session performed one FG ON→OFF transition: request 137401 enabled FG at epoch 2; request 137004 drained to source-only epoch 4. The 11m06s window closed, OFF completed, and no pump fatal error was reported. This is one transition, not the required three-cycle OFF→ON→OFF→ON→OFF→ON battery.

R137 did not rerun INTERNAL-loss injection. R136's bounded 60-pair loss injection preserved SOURCE continuity and recovered automatically; that historical result remains useful baseline evidence but does not validate the final10 runtime or a R137 fix.

## 22. 10–15 minute soak

The 11m06s final9 live run qualifies as a long real-gameplay observation, not post-fix acceptance. It contains one >500 ms FG-only outage, so the soak gate is **PARTIAL / not passed**. There is no post-fix 10–15 minute run. The captured run reported no SOURCE drops, drawable failures, present failures, or order violations, and no host `kIOGPUCommandBufferCallbackErrorSubmissionsIgnored` event.

## 23. Target-rate display latency

R137 did not produce a clean approximately 34 ms SOURCE window or per-window SOURCE present-hook→physical-display p50/p90/p99. Therefore `FG_DISPLAY_LATENCY=NOT_MEASURED` for R137; `internalReadyToDispatch` is not substituted for display latency.

Historical R136 reported a single lifetime SOURCE-hook→display record of 127.80 / 154.81 / 184.86 ms and INTERNAL-dispatch→display 33.38 / 46.05 / 60.84 ms. R135's comparable approximately 34.45 ms SOURCE-period reference was 101.60 / 109.56 / 143.26 ms. R136 disclosed that its record was lifetime-normalized and not a same-window target-rate comparison. These historical figures are not R137 measurements.

## 24. Edge haze

`EDGE_HAZE=NOT_CHARACTERIZED`. R137 collected no bounded SOURCE/INTERNAL image set, center-versus-border comparison, or reset/motion comparison. The user's deformation/glitch observation during falling FPS does not establish an edge-specific MetalFX defect. Keep this separate from the starvation finding.

## 25. Cross-queue synchronization

The shared-event path remained enabled. The final R134 timeline records 18,553 signals, 15,053 waits, zero skipped waits, zero pump-use violations, and a minimum pump-use margin of 2.403 ms. No synchronization change was made. This supports that the mechanism remained active; it does not identify the maximum outage's cause because that event's detail trace was exhausted.

## 26. Host GPU failures

There were zero `kIOGPUCommandBufferCallbackErrorSubmissionsIgnored` records in the final9 observer log. No measured R137 performance interval was excluded under the host-GPU-failure policy.

## 27–28. Independent review and Max arbitration

No fresh R137 GPT-6 Luna xHigh read-only final review has been performed. The current R137 changes have build and deterministic-test evidence, but the required review is deferred until final10 is live and the measured event has either been fixed or conclusively classified. No Max arbitration was used; the next discriminating step is the final10 live trace, which should resolve the frame-versus-placeholder question directly.

## 29. What was not tested

- Final10 live capture with provider no-output/error and presentation-stage events.
- A proven starvation fix, short post-fix heavy-area acceptance, and post-fix 10–15 minute soak.
- Three ON/OFF cycles and R137 failure-injection recovery.
- Clean target-rate R126 cadence and R137 hook-to-physical-display latency percentiles.
- Edge-haze image characterization.
- Full OS thread stacks for the maximum event; the retained pump-state samples concern earlier intervals.
- A fresh independent R137 Luna xHigh review.
- A controlled thermal-versus-FG-load comparison.

## 30–31. Alpha blockers and future optimizations

**Alpha blocker:** one unexplained 1.266652 s FG-only no-INTERNAL interval while the pump continued presenting SOURCE. Until the exact no-output/placeholder/presentation-stage transition is measured and recovery is validated, the alpha cannot be closed. The one live ON/OFF cycle and pre-fix soak do not satisfy the requested post-fix gates.

**Future characterization after the blocker is resolved:** target-rate latency percentiles; edge haze as a bounded image-quality comparison; thermal-controlled long-session comparison. Do not change the working R135/R136 schedule or shared-event path without contradictory event evidence.

## 32. Smallest next product step

Use the staged final10 diagnostics in the same heavy area for a focused 3–5 minute capture. The current final9 game process has exited and FG is OFF; final10 has not been loaded. Restart Highball and Elden Ring under the final10 bottle environment, then collect the trace while the user moves in the heavy area. Prioritize any FG outage exceeding 250 ms so one of the two reserved severe traces records provider result, placeholder/stage disposition, pump claim/progress, direct-source reason, drawable result, and shared-event state. Keep the pipeline unchanged until that event identifies a specific failed transition; then implement the smallest supported fix and rerun the applicable deterministic and live gates.

## Evidence index and hashes

- R136 baseline: `../r136-evidence/R136-REPORT.md`.
- R136 event-2 reassessment: `r136-event2-review.md`.
- R137 live log: `live-r137/observer.66189.heavy-07.log` — SHA-256 `49dd66fc47276cce3db5503f78b01b04329391688f5ac19a0a71c193bde3999d`.
- Marked interval timeline: `live-r137/heavy-07.timeline` — SHA-256 `f970a28d15bd9f97cf526f6b168e24d436f6a610d446eb1fe3a9a017aeec4d5b`.
- Marked interval analysis: `live-r137/heavy-07.analysis.json` — SHA-256 `52ea0eda53f7c2292bf67cbd708d5cec87cf412234f87e2ee8729f71d116224c`.
- Final10 build log: `r127-build-final10.log`.
- Final10 integration log and immutable inputs: `r137-observer-integration-final10.log` (SHA-256 `fda64ca978fd2c0ac6b73047dfc9045d4ea8b7e7b2b8b1c815d49d007bbf2c56`) and `r137-observer-integration-final10/`.

The live log and analysis are intentionally not described as a post-fix comparison. They are the evidence that motivates the next final10 capture.
