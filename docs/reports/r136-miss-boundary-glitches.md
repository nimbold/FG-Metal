# R136 — ELIMINATE MISS-BOUNDARY GLITCHES AND CLOSE THE D3DMETAL ALPHA

Evidence root: `experiments/metalfx_frame_interpolator_R93/r136-evidence/`

Every number is labelled **[MEASURED]** (a record in this evidence tree),
**[DERIVED]** (arithmetic on measured values), **[INTERPRETATION]**,
**[HYPOTHESIS]**, or **[USER OBSERVATION]**. Logs: `live-r136/observer.22022.log`
(session A, pre-fix characterization), `live-r136/observer.24835.log`
(session B, post-fix battery). Captures: `live-r136/miss-captures/`
(18 + 17 event windows, PNG + JSON sidecar each).

---

## 1. Executive verdicts

```
MISS_BOUNDARY_ARTIFACT_CAUSE  = CAUSE_1_PURE_CADENCE_HOLE — dominant;
                                sub-populations measured (§9)
MISS_BOUNDARY_RECOVERY        = VALIDATED (§10, §11)
R126_CLASS_CADENCE            = VALIDATED (lifetime 97.0 % INTERNAL/SOURCE
                                across a 19-minute multi-leg battery; §16)
RUNTIME_FG_ENABLE_DISABLE     = VALIDATED (3 × OFF→ON→OFF, §20)
FG_FAILURE_FALLBACK           = VALIDATED (60-pair injection, §21)
D3DMETAL_FG_ALPHA_SOAK        = VALIDATED (12 min 25 s heavy gameplay, §22)
FG_DISPLAY_LATENCY            = MEASURED (§23; no regression vs R135 at
                                comparable periods; per-window latency
                                instrumentation gap disclosed)
BOUNDED_ALPHA_DIAGNOSTICS     = VALIDATED (§25; two facility defects found
                                and fixed during the round, disclosed)

D3DMETAL_FG_ALPHA_RUNTIME     = PARTIAL — the R136 blocker (miss-boundary
                                glitch characterization + structural fix)
                                is closed and validated, but the user
                                still reports non-fluid stutter in heavy
                                areas and interpolation edge-haze; the
                                remaining blockers are pump commit-starvation
                                under GPU backlog, interpolation edge
                                quality, and host GPU fragility. §29.
```

**FINAL-REVIEW = PASS** (fresh Max reviewer; 0 HIGH / 3 MEDIUM / 6 LOW
findings; F1/F5/F6/F7 fixed in code post-review, F2/F3/F4/F8 disclosed and
corrected in this report; delta review §27).

---

## 2. Model assignments

- **Interactive harness:** Zcode, model `GLM-5.3 Flash` throughout.
- **Root orchestrator — GLM-5.3 Flash (Max):** taxonomy design, capture
  facility design and implementation, gate classification, fix design and
  implementation, verdicts. Implemented directly (single-file surgical
  edits + one pump hunk; the R135 report's "root does small surgical work"
  clause applies).
- **Bounded implementation / tooling — GLM-5.3 Flash (Max), same session:**
  analyzer extension, drivers, PNG smoke harness. No separate High worker
  was spawned: every code change was small, inside two known files, and
  gate-checked after each step; a worker hand-off would have added risk.
- **Final reviewer — fresh GLM-5.3 Flash (Max), read-only:** §27
  (FINAL-REVIEW = PASS, 9 findings; delta review of the four applied fixes).
- **Live evidence reduction:** existing analyzer extended; no worker
  (strictly serial GPU host).

---

## 3. Files changed

| File | Change |
|---|---|
| `r127-alpha-runtime/R123DisplayPump.m` | **The fix**: capture-aware drop guard in `takeForSlotAtNs`'s miss branch (defer unfilled-head drop to `capture(B) + measuredPeriod` when the slot fires earlier); `_dropGuardDeferrals` counter + accessor; F6 comment correction (post-review) |
| `r127-alpha-runtime/R123DisplayPump.h` | Guard contract in the buffer-shape comment + `dropGuardDeferrals` property doc (F6-corrected wording) |
| `r127-alpha-runtime/observer-title-inprocess-r127.m` | R136 section (~5844–6700): miss taxonomy classifier, bounded visual capture (rings, event windows, PNG/JSON writer), hooks in the tick/present/resolve paths, `R136_MISS_SUMMARY` in both summaries, ON-reset, 16F-aware capture reader + `r136HalfToFloat`; post-review: role-by-claim-order (F1), capture-queue init under lock (F5), miss-record logged after open (F7) |
| `r127-alpha-runtime/R127LatencyStats.h` | `R136_` prefix classified `R127LogAlways` (the shared unclassified 64-line budget had swallowed the records in the first session-A attempt) |
| `r127-alpha-runtime/r123_pump_selftest.m` | Scenario extension (`gapAtSeq`/`gapNs` capture hitch), `dropGuardDeferrals` in results, cases **O/P/Q**; 15 → 18 cases |
| `MetalFXFrameInterpolationProvider.{h,m}` | Read-only `historyValid` diagnostic accessor (the reviewer notes this predates R136 as an uncommitted R135 leftover; R136 only documents it) |
| `r136-evidence/tools/*` | `r136_bottle_env.py`, `drive_r136_miss_capture.sh` (auto game-log resolution), `drive_r136_cycle.sh` (auto resolution), `analyze_r136.py` (R136 taxonomy + **fixes the R135 analyzer's record-prefix bug** — it parsed `R135_*` while the observer emits `R134_*`, so R135's pair/critical-path parsing silently matched nothing), `r136_png_smoke.m` (BGRA + RGBA16Float encode paths) |
| Build scripts | `-framework CoreGraphics -framework ImageIO` added to both (`build_r127_tools.sh`, `build_r130_observer_integration.sh`) |

Untouched: estimator math, MetalFX math, SOURCE ownership, capture staging,
reservation machinery, R125 retention, R126 self-present gate, R120 pacing,
R102 disabled path, R135 capture-anchored slot rules and shared-event sync.

---

## 4. Baseline R135 cadence (authoritative, unchanged)

At ~29 fps SOURCE (R135 report §9): INTERNAL/SOURCE 93.8 %, output ≈ 56.2 fps
(≈ 1.94× source), SOURCE drops 0, order violations 0, drawable/present
failures 0; capture-anchored schedule `SOURCE n @ capture(n)+2P`,
`M(n,n+1) @ capture(n)+2P+H`; display latency median 101.60 ms @ 34.45 ms
source period (2.95 periods incl. photon tail).

---

## 5. Miss-event taxonomy (Phase 1, implemented)

`R136_MISS_EVENT` (always on, one bounded line per deadline drop, emitted
from the pump tick after `R123_SLOT_MISSED`):

| Class | Rule (from the R134 pair records, monotonic domain) |
|---|---|
| **A** — generated, not ready at slot | `genStartNs != 0`; sub-states `in-flight-at-slot` / `completed-late` / `completed-before-slot` |
| **B** — never generated | `genStartNs == 0`; sub-reasons `capture-incomplete` / `no-generation-entry` |
| **C** — source/capture hitch (flag) | `captureInterval > max(2×period, 3H)` |
| **D** — recovery after miss (flag) | previous pair outcome == 2; run length |
| **E** — host GPU failure | excluded at analysis by the SubmissionsIgnored rule; each event carries `recentCopyAbort` (copy abort ≤ 250 ms before the miss) |

Plus per-event: pair endpoints + present ordinals, slotNs, readyNs,
ready−slot, source period, queue depth, provider `historyValid`, injected
flag, miss-run length, capture disposition. `R136_MISS_RECOVERY` closes each
window; `R136_MISS_SUMMARY` rides on both pump summaries. ON resets the
per-window counters.

---

## 6. Bounded visual-capture method

Env-gated (`FGMETAL_R136_MISS_CAPTURE=1`; budgets 16 natural / 6 injected
per process; hard caps 8 images/event, writes only inside
`FGMETAL_EVIDENCE_DIR/miss-captures/`):

- Two 2-slot shared-copy rings (SOURCE / INTERNAL payloads) written by async
  blits on a dedicated `r136CaptureQueue` — the frame *before* a miss stays
  inspectable without retaining any pump payload or drawable. No
  `CAMetalDrawable` is retained anywhere.
- Event window: opened at the drop; claims A (ring), B/B2/… (each present
  while open, role by claim order post-F1), `lateM` (a resolve arriving
  after its drop), the recovery midpoint `M_next`, and the closing SOURCE
  `C_next`; closes after the recovery internal + following source or a 4 s
  timeout; writes one PNG per frame (lossless, full res) + one JSON sidecar
  with the full temporal record and image manifest.
- INTERNAL payloads are MetalFX outputs in **RGBA16Float**: the reader
  converts per-format (8 bytes/pixel, half→float, linear→sRGB) — the
  original 4-byte assumption silently produced black evidence PNGs
  (§7 caveat). Validated by `r136_png_smoke` (`PNG-SMOKE=PASS`,
  `PNG-SMOKE-16F=PASS`).

---

## 7. Natural miss evidence (session A, pre-fix; 200 s volatile gameplay)

**[MEASURED]** 126 classified drops (123 natural / 6 injection-refused
pairs; class A 114 = 90.5 %, class B 12 = 9.5 %); 18 captured windows,
128 images, 18 sidecars; zero host GPU errors.

**[MEASURED]** 83 of the events ran with the pump period clamped at the 4H
ceiling (66.67 ms — the title's capture stream at ≤ 15 fps), with A→B
capture gaps of 72–215 ms; 66 natural events had gaps > 83 ms, the interval
beyond which the capture-anchored slot fires **before the pair's own B can
plausibly be generated** — a structurally unpresentable slot.
**[MEASURED]** 14 sampled late midpoints arrived 12–31 ms after their slot
(all within one period). **[MEASURED]** one natural 4-consecutive-miss run;
27/126 events followed another miss.

**[MEASURED]** Display intervals around misses (from the capture metadata):
event 1 (4-run): B,B2,B3,B4 at +4/+82/+136/+156 ms — multiple held frames;
recovery M_next then C_next back on the normal anchors. Events 2–16: single
miss, B at the slot, `lateM` 12–31 ms late (discarded), recovery internal
presented; event 2 additionally shows a **~550 ms stretch with no INTERNAL
presented** (the pump starved while the title presented directly — commit
backlog under GPU pressure; `internalPresentId` of the recovery midpoint was
the 29th of the run).

**Facility caveats (disclosed):** (a) session-A INTERNAL PNGs are black —
the 16F reader bug above, fixed and re-proven in session B; (b) the first
build's records were swallowed by the unclassified log budget
(`R136_` now `R127LogAlways`) and `captureArmed` was never set at open —
both fixed; the defective attempt is archived
(`observer.17307.sessionA-instrumentation-defect.log` + note); (c) final
review F1: 14/18 session-A windows overwrote their B-role PNG
(role collision; manifests carry the true sequences — the all-SOURCE-clean
conclusion is unaffected).

---

## 8. Controlled miss evidence (R127_GENERATION_LOSS, drop-before-pump)

**[MEASURED]** The validated injector drops *already-generated* INTERNALs
before the pump (estimator + MetalFX run, temporal history advances; next
pair interpolates exactly as after a natural miss). Session A armed 1, 2, 3
consecutive drops with gaps: refused pairs 3836 / 4016–4017 / 4265–4267;
capture windows 17 (missRun=2) and 18 (missRun=3) recorded A → B → B2(→B3) →
M_next → C_next with the pure-miss signature (no wrong frame, clean
re-entry). Caveat **[MEASURED]**: both injection windows landed during
clamped-period (≤ 15 fps) source stretches — the arming delay put them
there — so their display gaps (48–125 ms) are wider than a nominal miss.
Session B's 60-pair injection: pairs 16841–16900 refused consecutively; only
15 `R127_GENERATION_REFUSED` lines logged (the shared 64-line anomaly budget
was at 48 when the injection fired; the remaining 45 suppressed — counts
reconciled via `R136_MISS_SUMMARY injected=59` and the event span).

---

## 9. Exact visual-glitch classification (the decision gate)

**`MISS_BOUNDARY_ARTIFACT_CAUSE = CAUSE_1_PURE_CADENCE_HOLE`** **[MEASURED]**

1. At every captured miss the displayed sequence is exactly
   `… A → B(at the missed slot) → [hold] → M(B,C) → C …`. **All 35 captured
   SOURCE frames across both sessions are clean, correct, uncorrupted game
   renders** (session-B INTERNAL captures decode as valid interpolated
   frames with the fixed reader; session-A INTERNAL captures were black from
   the reader bug, not the product). No wrong pairing, no corruption, no
   phase break → CAUSEs 2/3/4 excluded for the dominant population.
2. Sub-populations **[MEASURED, session A natural]**:
   - **Source-collapse structural drops (~2/3 of events):** period clamped
     at 66.67 ms while the true capture gap ran 72–215 ms → slot before
     `capture(B)` → guaranteed drop. This is a pump defect (clamp
     underestimates the true interval), not a title-hitch to accept.
   - **Fast-window W-tail (~20 %):** midpoint 12–31 ms late; presenting it
     would be display-equivalent (one held frame either way) — accepted.
   - **Placeholder pairing mismatch (`completed-before-slot`, ~14 %):** after
     a title-direct SOURCE, the pump's placeholder endpoints track the
     commit chain (`priorSourceSequence`) while generation pairs the capture
     chain → the generated midpoint cannot fill it and is discarded.
     Future optimization (needs a commit-path endpoint change).
   - **Estimator non-production (9.5 % session A; 27 % of session B's
     residual):** capture gaps → seeds; the hole is content-inherent.
3. **[USER OBSERVATION, pre-round]** the user reported visible glitches
   corresponding to misses/hitches.

---

## 10. Estimator-history behavior at misses

**[MEASURED]** The guard never touches history; the injection (which drops
generated frames after history advances) produced correct recovery
midpoints in every window. **[MEASURED, corrected per final review F2]**
`historyValid=0` on 10/126 session-A and 261/613 session-B events —
dominated by `no-generation-entry` pairs (the pre-existing R132
capture-gap reset: a *reseed*, by design, never interpolation across a
missing SOURCE), plus 109 session-B class-A events with history invalid
around the miss. No event shows a wrong-endpoint recovery; the distinction
missing-INTERNAL vs missing-SOURCE is preserved (SOURCE never drops).

---

## 11. Pipeline recovery behavior

**[MEASURED]** After any miss the next pair re-anchors capture-based (rule
1/rule 2 untouched): every captured window closes with the recovery
midpoint and following SOURCE on their normal anchors
(`R136_MISS_RECOVERY recoveryInternal=1` in all 35 windows). The displayed
intervals after a single miss are `H, P, H, …` — one held frame, no phase
shift. Consecutive runs (natural 4-run; injected 2/3-runs; session-B runs to
6) close the same way. **[INTERPRETATION]** the one visible anomaly the
guard cannot address is the commit-backlog stretch (§7 event 2) where the
pump starves and the title presents directly for hundreds of ms — a
separate, now-measured mechanism (future step, §29/§30).

---

## 12. Fix implemented (the smallest justified change)

**`R123DisplayPump.m takeForSlotAtNs` — capture-aware drop guard** (the
exceptional miss branch only; ~15 lines):

```
if head is an unfilled INTERNAL placeholder AND
   now < head.enqueueNs + measuredPeriod:      # enqueueNs = capture(B)
       nextSlot = capture(B) + measuredPeriod  # defer the drop, once
       (counted: dropGuardDeferrals)
else: drop + early-present B (the R135 behavior, unchanged)
```

- Steady state never enters the guard (slots are `capture(B)+P+H`);
  proven by selftest case Q and byte-identical cases A–N.
- Recovery path: midpoint resolves within the deferral → presents; rule 2
  re-anchors SOURCE B at `capture(B)+2P` — the no-miss schedule (case O).
- Miss path: drop at `capture(B)+P`; B presents there — **earlier** than
  its two-period anchor, never later (case P). A later wake plus a raised
  period can re-defer; the [H, 4H] clamp bounds every target at
  `capture(B)+4H` (final-review F6 wording).
- **[DERIVED]** effect on the measured population: recovers the
  large-capture-gap pairs (gap > clamp + H ≈ 83 ms) whose W fits one
  clamped period — the dominant natural class. W-tail and pairing-mismatch
  classes are display-equivalent or deferred to future work.

---

## 13. Deterministic recovery tests

Pump selftest **15 → 18 cases**; additions (all new, none weakened):

- **O** — capture hitch (gap 100 ms at seq 30, 28 ms generation): guard
  defers once, midpoint presents at `capture(30)+clamped period`, SOURCE 30
  back on the two-period anchor, zero misses, accounting closes.
- **P** — same hitch, gap-pair generation 70 ms: exactly one miss, exactly
  one gap, guard deferred once, SOURCE 30 presented at
  `capture(30)+clamped period` (early vs its anchor), all other midpoints
  presented.
- **Q** — steady-grid 70 ms overrun regression: guard silent
  (`dropGuardDeferrals == 0`), miss shape identical to pre-R136.

Cases A–N byte-identical (verified by the final reviewer against the R135
snapshot). 45-case observer suite unchanged and green. **[MEASURED]**
37/37 diagnostics, 18/18 pump, 45/45 observer — on the final build
(`r136-build.log`, `r136-deterministic.log`).

---

## 14. Full historical regression results (session B battery)

**[MEASURED]** One process, 19 min 12 s (00:07:18–00:25:51), zero
`kIOGPUCommandBufferCallbackError*` lines:

| Leg | Window | INTERNAL/SOURCE |
|---|---|---|
| ON 1 (gate, 133 s) | 3 periodic windows | 94.9 % / 97.3 % / 96.6 % |
| ON 2 (cycle, 92 s) | 2 windows | 96.6 % / 91.7 % |
| ON 3 (soak, 12 min 25 s) | 12 windows | 95.0 / 96.2 / 99.7 / 96.8 / 94.0 / 92.2 / 99.0 / 99.7 / 98.7 / 99.3 / 99.2 / 98.3 / 98.5 % |

Lifetime: `sourceEnqueued=20881 sourcePresented=20879 droppedSource=0
internalEnqueued=20849 internalPresented=20219 deadlineMisses=613
droppedInternal=629 dropGuardDeferrals=463 maxDepth=8 orderViolations=0
orderGaps=660`. **[DERIVED]** lifetime INTERNAL/SOURCE = **97.0 %**; the 2-source
enqueued/presented gap is the final 20 s tail (post-summary runtime
summaries close at presented == enqueued, depth 0 — final-review verified).

---

## 15. Short live before/after comparison

Same host, same title area type (open world), capture instrumentation on in
both. **[MEASURED]** pre-fix session A: 126 misses over ≈ 4 400 pairs in a
200 s window (≈ 2.8 % lifetime, up to 8 % per volatile window) with the
structural class unrecovered. **[MEASURED]** post-fix session B: 613 misses
over 20 849 pairs = **2.94 % lifetime**, with the heavy-scene windows at
0.2–1.3 % (the guard absorbing the collapse windows) and worst windows at
7.5 % during double-hitch minutes. **[INTERPRETATION]** the comparison is
scene-confounded (the soak entered heavier areas than session A), but the
mechanism-level evidence — 463 guard deferrals, case O/P determinism, and
the post-guard heavy-window shares of 95–99 % — supports the structural
class being recovered rather than the rate being scene-luck.

## 16. Target-rate cadence (~29–30 fps SOURCE)

**[MEASURED]** ON-1 gate leg (source period ≈ 34–40 ms): share 94.9–97.3 %
across its windows — R126-class (R135: 93.8–94.4 %). Output cadence ≈ 2×
source with alternating H / P−H spacing as designed. The soak legs swing
the title 35–67 ms (clamped); shares hold 92–99.7 % — the share tracks
volatility, not the period, as in R135.

## 17. Miss rate

**[MEASURED]** session B lifetime: 613 / 20 849 pairs = **2.94 %** (vs
R135's ≈ 5–10 % in volatile windows). Guard deferrals: 463 (2.2 % of all
pairs). **[DERIVED]** residual decomposition (of 613): pairing mismatch 240
(39 %), W-tail in-flight 205 (33 %), non-production 164 (27 %), completed-late 4.

## 18. Objective glitch rate

**[DERIVED]** visible glitch = a held frame (one or more) at a miss:
≤ 613 held-frame events per 20 879 SOURCE frames = **≤ 29.4 per 1000 SOURCE
frames lifetime**, concentrated in the collapse windows; the guard's
recovered class displays the no-miss pattern. The ~550 ms pump-starvation
stretch (§7) is a separate, rarer event class (observed once in session A's
window sample; not separately counter-instrumented — see §28).

## 19. User-visible observation

**[USER OBSERVATION, post-fix session B, verbatim]**: "There was lots of
stutters here and there and it definitely wasn't fluid experience, and most
of the glitches and haziness (that happens on all frame generations to a
degree) was happening on the edges of the screen."
**[INTERPRETATION]** (i) the session was played deliberately into heavy
areas where the title's own capture stream collapsed to ≤ 15 fps
(measured) — part of the stutter is the title's, not the pipeline's; (ii)
no black frames were reported this session (the pre-round complaint class);
(iii) the *edge haziness* is an interpolation-quality artifact (MetalFX
temporal edges), orthogonal to cadence — a quality item, not a correctness
item. Both the stutter and the haze keep the alpha honest at PARTIAL.

## 20. ON/OFF regression

**[MEASURED]** 3 × OFF→ON→OFF (requestIds 4001/4002/4003/4004/4005/4007):
every transition completed (epochs 2/5/8 for the ONs; disable flushes at
epochs 3/6/9), all legs summarized, no deadlock, phase re-locked each ON
(per-window shares ≥ 91.7 %), zero SOURCE drops, zero duplicates, zero
order violations lifetime.

## 21. Failure-injection regression

**[MEASURED]** 60-pair `R127_GENERATION_LOSS` armed at soak+6 min: pairs
16841–16900 refused consecutively (drop-before-pump, history advanced);
SOURCE continuity intact (`droppedSource=0`); the immediately following
pairs present normally; post-injection periodic windows show miss deltas of
+9/+2/+11/+8/+5/+16/+14 per ~1100–1300-pair window (ambient) — no elevated
residual. **[MEASURED]** only 15/60 refusal lines survived the shared
anomaly log budget (disclosed; counts reconciled via the miss-record span).

## 22. 10–15 minute soak

**[MEASURED]** 12 min 25 s continuous ON (00:13:06–00:25:31), real
gameplay, title rate 35–67 ms (clamped): 12 periodic windows at 92.2–99.7 %
share, `droppedSource=0`, `orderViolations=0`, `maxDepth=8`, process
stable, zero GPU errors, estimator/capture counters clean. **VALIDATED.**

## 23. Display latency median/p90/p99 and delta vs R135

**[MEASURED]** `R127_LATENCY_SUMMARY` (single, LIFETIME record — final
review F3; no per-window emission or reset exists in this build):
SOURCE present-hook → display **median 127.80 ms / p90 154.81 / p99
184.86**, `sourcePeriodNs=61754000` (the soak tail's period), = **2.07 ×
the final-window period**; INTERNAL dispatch → display median 33.38 / p90
46.05 / p99 60.84 ms.

**[DERIVED, corrected per F3]** the record normalizes a lifetime median by
the final window's period. On the session's measured period mix (17 windows
at 35–67 ms, weighted ≈ 50 ms) the lifetime median is ≈ **2.55 periods incl.
photon tail** vs R135's 2.95 at 34.45 ms — the 2-period anchor scales with
the measured period and no added latency is present (the guard only defers
*drops*; SOURCE B presents at `capture+2P` or earlier — case O/P assert
this). **[MEASURED GAP]** no per-window latency was instrumented this
round, so a same-scene A/B at ~34 ms was not captured; the mechanism-level
claim (fixed 2-period delay, non-accumulating) is covered by the selftest
and by the period-normalized numbers.

## 24. Cross-queue sync status

**[MEASURED]** enabled by default and active the whole battery:
`syncSignals=30279 syncWaits=20994 syncWaitSkips=0 pumpUseViolations=0`
(final R134_TIMELINE). **[MEASURED, monitor]** `pumpUseMinMarginNs` fell to
3 000 ns (R135: ≥ 75 000) — consistent with the pump presenting closer to
capture under the guard/early-present paths; zero violations, health-signal
only.

## 25. Bounded diagnostics

**[MEASURED]** Caps behaved: `healthyLogSuppressed` large as designed;
periodic/final summaries uncapped and complete; all `R136_` records always
logged by policy (bounded by construction: ≤ 1 line per miss/window). Two
round defects were found *by* the diagnostics discipline and fixed: the
unclassified-budget swallow (session A attempt 1) and the never-armed
capture writer (session A attempt 2) — both archived with notes. A shared
anomaly-budget suppression (15/60 refusal lines) is disclosed in §21.
**VALIDATED** with those disclosures.

## 26. Host GPU failure observations

**[MEASURED]** One launch (pid 21347) entered the host failure mode during
loading: 33 × `kIOGPUCommandBufferCallbackErrorSubmissionsIgnored` + 1
InvalidResource + 2 Timeout; black screen; the window was marked INVALID,
preserved (`observer.21347.gpu-failure.log`), and the 2-minute cooldown +
relaunch protocol (from R135) produced a clean session. The measurement
sessions themselves (22022, 24835) ran with **zero** GPU errors. The
fail-closed surfaces were not exercised live this round (0 occurrences);
the 3 s-bounded copy-timeout drop path is unchanged from R135 and the
round made no GPU-recovery changes.

## 27. Final independent Max review

**FINAL-REVIEW = PASS.** Fresh read-only Max reviewer; re-derived the
schedule, diffed both changed files against the R135 snapshot, recomputed
the load-bearing numbers, decoded the capture PNGs itself. All 14 brief
questions answered YES (cause demonstrated from evidence; fix matches the
cause; R135 cadence preserved — guard unreachable in steady state; latency
non-accumulating; SOURCE exactly-once incl. the 2-source tail explanation;
endpoints correct; consecutive-miss runs safe incl. the injected 60-run; no
reorder; sync preserved default-on; ON/OFF clean at epochs 2/5/8; fallback
clean; no material latency regression; invariants intact by diff; residual
acceptable-for-alpha with honest disclosure). Findings: **0 HIGH,
3 MEDIUM (F1 B-role PNG collision; F2 historyValid claim false → corrected;
F3 latency record is lifetime → corrected), 6 LOW** (F4 session-A provenance
hash gap — the session-A conclusions rest on log records whose taxonomy code
is identical in the archived source; F5 capture-facility races — fixed; F6
guard re-fire wording — corrected; F7 idx=0 on window openers — fixed; F8
gate numeric nits — corrected; F9 margin monitor — recorded §24).
**Delta review** of the four applied fixes: **PASS** — all four verified
minimal and correct, the guard's scheduling behavior token-identical to
what was reviewed, the gate chain proven genuine on the shipped source
(hashes `f953692d`/`b6a6bde3`/`b263022c` match the tree). Residuals, all
LOW and non-blocking: **R1** a redundant unlocked count re-check (fixed
after the delta: the cap is enforced only inside the locked role
assignment); **R2** a theoretical duplicate-`lateM` filename collision
(pre-existing shape; requires a duplicated generation for one pair; 0
collisions across 235 session files); **R3** the capture-facility fixes are
inspection-verified, not gate-covered (the 18/45-case suites do not drive
the env-gated default-off capture path); **R4** a pre-existing microsecond
ON-reset window in `r136OnMiss`'s two lock acquisitions (one-event summary
undercount at worst). Post-delta, R1's one-line fix landed and all gates
were re-run on the shipped source: 37/37 + 18/18 + 45/45 PASS; final
provenance hashes in `observer-sha256.txt` (dylib `04df6092…`, observer
`1f68e1d4…`, pump `b263022c…`).

## 28. What was NOT tested

- Per-window display latency (single lifetime record only — F3).
- The pump-starvation (commit-backlog) stretch: characterized in session A
  (§7) but not counter-instrumented or fixed.
- Memory growth across the soak (no cheap reliable instrument — unchanged
  since R134).
- 22–26 fps source as a target rate; window resize / display change;
  game restart mid-session.
- Fail-closed GPU surfaces live (0 host errors in the measured sessions).
- Same-scene A/B of the guard (scene cannot be reproduced on demand).
- Edge-haze interpolation quality (user-observed; no automated metric).

## 29. Exact remaining blocker(s) / future optimizations

**Alpha blockers (remaining):**
1. **Perceived fluidity under source collapse** — during title hitch
   windows (≤ 15 fps capture stream) the output necessarily holds frames;
   the guard recovered the structural drops, but the pump commit-starvation
   stretch (title-direct display for hundreds of ms after a backlog) and
   the title's own stutter remain user-visible. **[USER OBSERVATION]**.
2. **Interpolation edge haziness** — MetalFX temporal artifacts at frame
   borders, reported by the user as present "on all frame generations to a
   degree" and here on screen edges; quality item, not cadence.
3. **Host GPU fragility** (`SubmissionsIgnored` during loading) —
   host/driver level, unchanged, gates long sessions.

**Future optimizations (not blockers):**
- Placeholder pairing after title-direct SOURCEs (~39 % of session-B's
  residual drops): seed placeholder endpoints from the capture chain.
- Per-window latency instrumentation (restore R135's per-summary latency
  emission or add a window reset).
- W-tail late-present (display-equivalent today; only worth revisiting if
  the hold-energy calculus changes).

## 30. Smallest next product step

1. Instrument + fix the commit-backlog pump-starvation stretch (the §7
   event-2 mechanism): it is the largest remaining *pipeline-controllable*
   stutter source under load (R137 candidate).
2. Evaluate MetalFX edge handling (motion-vector border clamp /
   `shouldResetHistory` policy) against the user's edge-haze report.
3. Re-run the 2-minute latency instrument at a ~34 ms window to close the
   F3 measurement gap.

---

## Reproduction

```sh
cd experiments/metalfx_frame_interpolator_R93
# deterministic gates
FGMETAL_R127_BUILD_DIR=r136-evidence/r136-build \
  r127-alpha-runtime/build_r127_tools.sh
FGMETAL_R130_BUILD_DIR=r136-evidence/r136-deterministic \
FGMETAL_EVIDENCE_SELFTEST=1 FGMETAL_R123_PUMP=1 FGMETAL_R124_TEST=0 \
  r127-alpha-runtime/build_r130_observer_integration.sh
# PNG evidence-path smoke
clang -fobjc-arc -framework Metal -framework CoreGraphics -framework ImageIO \
  -framework Foundation r136-evidence/tools/r136_png_smoke.m -o /tmp/smoke \
  && /tmp/smoke /tmp/r136-png
# live (Highball; the user navigates into the open world)
python3 r136-evidence/tools/r136_bottle_env.py prepare --label r136-live-0N
# Highball → eldenring → Play, then:
r136-evidence/tools/drive_r136_miss_capture.sh auto <timeline> 70   # session A shape
r136-evidence/tools/drive_r136_cycle.sh auto <timeline>             # session B battery
python3 r136-evidence/tools/analyze_r136.py <observer-log>
python3 r136-evidence/tools/r136_bottle_env.py restore --label r136-live-0N
```

## Clock domains

Unchanged from R134/R135: pump/slot arithmetic is CLOCK_MONOTONIC ns;
R134/R136 event records are monotonic-to-monotonic; estimator GPU windows
are media-to-media; `R127_LATENCY_SUMMARY` is host-seconds. No cross-domain
subtraction is reported as an interval.
