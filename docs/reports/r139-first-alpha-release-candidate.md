# R139 — FIRST D3DMETAL ALPHA RELEASE CANDIDATE

Evidence snapshot: 2026-11-03/04 (Asia/Baku). Host: M3 MacBook, macOS 27.x,
Highball + D3DMetal, Elden Ring offline, 60 Hz display, 30→60 target.

---

## 1. Executive verdicts

| Verdict | Value |
|---|---|
| `D3DMETAL_FG_ALPHA_RUNTIME` | **VALIDATED** (unchanged from R138; no contradictory evidence) |
| `ALPHA_RC1_BUILD` | **PASS** |
| `ALPHA_RC1_INSTALL_RESTORE` | **PASS** |
| `ALPHA_RC1_LIVE_SMOKE` | **PASS** |
| `PUMP_QOS_EXPERIMENT` | **KEPT** (env-gated, default absent = R138-identical) |
| `EDGE_HAZE` | **INCONCLUSIVE** (edge-localized haze not reproduced; new global photometric INTERNAL-capture finding documented, likely capture-path) |
| `TARGET_RATE_RETEST` | **NOT_AVAILABLE** (no ≥28 fps SOURCE scene occurred; heavy areas 13.7–25.4 fps) |
| `TARGET_RATE_LATENCY` | **NOT_MEASURED** (no target-rate window; non-target-rate latency values documented in §8) |
| `ALPHA_RC1_INDEPENDENT_REVIEW` | **PASS** (§19) |
| `FGMETAL_D3DMETAL_ALPHA_RC1` | **READY_FOR_CONTROLLED_TESTING** |

All R138 invariants held in every live window: droppedSource = 0,
orderViolations = 0, FG-only outage > 250 ms = 0, zero GPU submission failures,
5 clean ON→OFF cycles.

## 2. Zcode/GLM assignments

- **Root / release owner — GLM-5.3 Flash Max** (this session): all integration
  decisions, runtime edit, suites, live orchestration, packaging, report.
- **Pump timing analyst — GLM-5.3 Flash Max (subagent, read-only):** located the
  QoS mechanism (pump thread created `observer-title-inprocess-r127.m:4355`,
  attributes only set detach-state → inherited DEFAULT; readback record
  confirmed `actual=DEFAULT` in all R138 logs); quantified R138 wake lateness
  (p50 1.5–2.8 ms steady; +16–28 ms events 100% confined to outage catch-up
  windows; the +28 ms incident repeated across ~12 consecutive ticks inside
  outage 91); risk-assessed the raise (single-purpose thread; present path
  shares it; drawable-pool contention is the watch item); recommended RUN.
- **Edge-haze analyst — GLM-5.3 Flash Max (subagent, read-only):** image-level
  characterization of the 3 captured events; verdict and findings in §6.
- **Final independent reviewer — GLM-5.3 Flash Max (subagent, read-only):** §19.

## 3. R138 baseline provenance (Phase A)

- Branch `main` @ `87e8e768ea5b790dcb96cabb8111d40a5af21870`.
- Tracked diffs = exactly the R138 fix (provider `.h/.m` + `provider_smoke.m`),
  verified line-by-line; observer and pump sources byte-identical to
  `r138-evidence/r138-build-postfix/` copies. Details:
  `phase-a/PROVENANCE.md`.
- Full deterministic suite re-run green twice (baseline rebuild and QoS-gate
  rebuild): diagnostics 39 PASS, fence/ownership 25 cases 0 failures, pump 19
  PASS, integration row set byte-identical to the R138 postfix log (exit 0),
  trace-selection 34 PASS, provider smoke PASS (establishment frame yielded).
- **Old-binary injection check:** the Highball bottle's *resting* environment
  was found to be a **stale R136-staged state** (`DYLD_INSERT_LIBRARIES` →
  `r136-evidence/live-build/…`, file present). Pre-existing host state,
  discovered via a wrong-key-case check earlier in the round; left as-is by
  user decision; every R139 run stages/restores explicitly over it. This is a
  real footgun for bare Highball launches on this host (see §18).
- **Build reproducibility finding:** the dylib recipe is NOT byte-reproducible
  (two identical-source rebuilds → `82cb9b83…` / `1b8d570c…`). Source identity
  is hash-proven; the shipped RC1 dylib is byte-exact the artifact every suite
  and live window ran against.

## 4. Source files changed

| File | Change |
|---|---|
| `r127-alpha-runtime/observer-title-inprocess-r127.m` | R139 Phase B only: env-gated pump-QoS gate in `r123ConfigurePumpThreadAttributes` + `r123ConfiguredPumpQos()` + honest `R127_PUMP_THREAD_QOS configured=` readback. Unset env → byte-identical attribute configuration to R138. Fail-open: a rejected QoS request logs and falls back; never refuses launch. |
| `r139-evidence/tools/*` | Round tooling (bottle env, driver, analyzers, wake-lateness extractor). No runtime code. |
| `r139-evidence/FGMETAL_D3DMETAL_ALPHA_RC1/*` | Release bundle (§10). No runtime code. |

No estimator, MetalFX mathematics, ownership, ordering, pacing, drop-guard, or
control-wiring changes. R120/R125/R126/R131/R132/R133/R135/R136/R138 protected
surfaces untouched.

## 5. QoS experiment and measured decision (Phase B)

Gate: `FGMETAL_R139_PUMP_QOS=USER_INITIATED`, read once at pump-thread
creation, default absent.

Wake lateness (PUMP_SLEEP_RETURN, return − slot target, live):

| Window | QoS | n | p50 | p90 | p99 | max | >15 ms |
|---|---|---|---|---|---|---|---|
| S1 target-rate (120 s) | DEFAULT | 122 | 2.62 ms | 5.52 | 10.86 | 13.82 | 0 |
| S1 soak (709 s) | DEFAULT | 183 | 3.02 ms | 6.03 | 16.91 | 23.20 | 2 (outage-confined) |
| S2 candidate (240 s) | USER_INITIATED | 234 | 2.61 ms | 4.75 | **5.08** | **7.23** | **0** |

Steady state is unchanged (p50 identical); the tail is compressed ~3× under
equal-or-heavier load. No regressions: drops 0, violations 0, FG-only >250 ms
0, INTERNAL/SOURCE ≥ 99.19 % (per-session lifetime), GPU failures 0, epochs
clean in both configurations.

**Decision: KEPT — as an environment gate, default absent.** The RC1 release
configuration therefore remains byte-for-byte R138 behavior; flipping the
default ON is deferred until a QoS-ON soak (R140 candidate), per the safety
policy that the release default must equal the long-soak-validated
configuration.

## 6. Edge-haze characterization (Phase C)

Set: the S1 capture facility armed with budget 8 wrote **8 events / 42 PNGs**
into `live-r139/miss-captures/`. Per the brief's "small representative set",
**3 events (01–03, 16 PNGs) were analyzed** — the first three events, captured
during early S1 FG-ON gameplay (the ON ramp / target-rate window period).
Events 04–08 were not analyzed (no exclusion was needed: the
analysis budget was consumed by the 01–03 set); they are preserved raw with
their metas for follow-up. Findings below therefore describe the analyzed set
only.

**Verdict: `EDGE_HAZE = INCONCLUSIVE`** for the user-reported edge-localized
haze. Measured findings (analyst, tool-tracked in /tmp, outputs summarized in
the bundle evidence):

1. SOURCE frames A/B/B2/B3/C_next: clean, near-static, no border defect.
2. INTERNAL M frames: geometry pixel-aligned with neighbors (Laplacian variance
   center 2196–2476 vs sources 2159–2487; border band equivalent); no ghosting,
   disocclusion halo, duplicated geometry, or HUD smear; UI black bars stay
   black. **No edge-concentrated spatial defect found anywhere.**
3. **New measured finding:** every analyzed INTERNAL frame carries a global
   photometric lift matching an exact per-pixel sRGB-encode curve (median
   exponent 0.45 ≈ 1/2.2, black-preserving, per-channel slopes 1.15/1.32/1.53,
   LUT-inversion residual MAD 57.8 → 1.9/255, identical across the analyzed
   events). Two explanations the PNGs cannot separate: (a) capture-path
   colorspace difference (INTERNAL texture linear, PNG written as sRGB;
   display unaffected) — considered more likely given five rounds of visual
   use without a reported global wash; (b) a real double-encode on INTERNAL
   presentation. Motion association and post-reset worsening were untestable
   (static scenes). All three analyzed events were recovery frames with
   `historyValid=false` at event-open; event 01 — the seed pair — measured
   identical to 02/03, incidentally re-confirming the R138 one-pair recovery
   live (`recoveryInternalPresented=true` ×3).

Classification discipline: not `FG_INPUT_DEFECT` (no evidence anything reaches
the display), not `NORMAL_INTERPOLATION_LIMITATION` (the reported edge haze was
not reproduced in this set). **Follow-up (R140):** one on-screen A/B
(photo/screenshot vs runtime capture during known INTERNAL display) plus an
audit of drawable/texture colorspace tagging on the INTERNAL presentation path.
No MetalFX mathematics were changed.

## 7. Target-rate test (Phase D)

`TARGET_RATE_RETEST = NOT_AVAILABLE`. Both live sessions ran heavy-area
gameplay: per-minute presented-SOURCE fps ranged 20.2–24.5 in S1 (window-A
delta 24.1; soak minutes 20.2/23.6/24.5/23.5) and 13.7–25.4 in S2. No 60–120 s
stretch at ≈29–30 fps SOURCE occurred; none was fabricated. Historical R135/R136
target-rate evidence (~29–30 fps SOURCE, ~56 fps displayed) remains the
reference. Window health in the achieved cadence: INTERNAL/SOURCE presented
99.58 % (S1 window A) / 99.71 % (S2, strict in-window deltas; 99.60 % by
lifetime counters at the S2 OFF), max 3 consecutive missing INTERNAL slots
(S1 soak) / 0 (S2 window), queue maxima depth 8 / reservations ≤6 / backlog
≤6 — all R138-class.

## 8. Display latency (Phase E)

`TARGET_RATE_LATENCY = NOT_MEASURED` (no target-rate window; R135's 101.60 ms
reference at ≈34.45 ms SOURCE period is not comparable to slower-cadence
windows and was not re-measured).

Measured at the achieved cadences (same record kind as R135,
`original-source-present-hook-to-physical-display`):

| Window | n | median | p90 | p99 | max |
|---|---|---|---|---|---|
| S1 (QoS off, lifetime-to-OFF, heavy ~21–24 fps stretches) | 3 586 | 137.07 ms | 147.88 | 164.53 | 194.94 |
| S2 (QoS on, lifetime-to-OFF, heavy 13.7–25.4 fps) | 6 034 | 145.52 ms | 152.50 | 184.86 | 1 086.4* |

\* one title-side stall on the SOURCE path; INTERNAL dispatch→display stayed
44.2–44.8 ms median in both sessions.

Schedule decomposition (R134 critical path) is QoS-invariant as designed:
`hookToIntendedSlot` median 83.35 ms (S1) vs 83.41 ms (S2). **The QoS change
does not alter end-to-end latency**; its measured effect is confined to the
wake-lateness tail (§5). Diagnostics caveat: the `sourcePeriodNs` field in
`R127_LATENCY_SUMMARY` disagrees with per-minute presented counts (definition
skew, present in both sessions); presented-count deltas are used as ground
truth for all cadence claims.

## 9. Deterministic regression

All green on the exact shipped binary (`3cd5e8ee…`), twice: diagnostics 39 /
fence 25 / pump 19 / integration (row set identical to R138 postfix; exit 0) /
trace-selection 34 / provider smoke. Counts unchanged from R138; no assertion
weakened; protected-surfaces list untouched.

## 10. Release-candidate binary and bundle

Bundle: `r139-evidence/FGMETAL_D3DMETAL_ALPHA_RC1/`

- `libfgmetal_r127_alpha_runtime.dylib` — SHA-256
  `3cd5e8ee7c0e12a0fd3166de104352f8a3298b5c97ac2e0efcfa32ac50278d45` (x86_64)
- `MANIFEST.json` — provenance: git head, source SHA-256 set, build command,
  reproducibility finding, environment manifest, suite results
- `check_prerequisites.sh` — 9-point host/bundle verification (all PASS on this
  host)
- `bottle_env.py` — self-locating prepare/status/restore; refuses missing or
  hash-mismatched dylib; snapshots original bottle bytes per label
- `fg_ctl.sh` — ON/OFF/SUMMARY with a no-title refusal guard
- `README.md`, `KNOWN-LIMITATIONS.md`

Config/environment manifest = the R138-validated set, `FGMETAL_R136_MISS_CAPTURE=0`,
`FGMETAL_R139_PUMP_QOS` absent. No experimental tracing or visual capture in
the release configuration; FG defaults OFF.

## 11. Prepare/restore verification (`ALPHA_RC1_INSTALL_RESTORE = PASS`)

Offline: prepare → `STATE=STAGED` → restore → `STATE=ORIGINAL`; repeated
cycles (labels `rc1-verify-01/02/03`); missing-dylib refusal (exit 2, no label
written); sha-mismatch refusal (exit 2); `fg_ctl.sh` refuses with no title
running (exit 2). Live: `r139-live-01` and `r139-live-02` both restored to the
byte-exact pre-session bottle (`STATE=ORIGINAL`, sha `c6b38dcf…`). Restore
returns only the `environment` object; no other bottle field touched; no
system-wide components; no elevation.

## 12. ON/OFF control verification

5 ON→OFF cycles across two live sessions (S1 epochs 2–9: 3 cycles; S2 epochs
2–6: 2 cycles), all via the release bundle paths with `fg_ctl.sh`-equivalent
atomic control writes. Transitions recorded with clean flushes
(`discardedSource=0` everywhere; `discardedInternal ≤1` per OFF, the designed
source-only drain), epoch resets correct, `SUMMARY` one-shot completed while
generating. `SUMMARY`/control behavior matches the documented contracts
including the no-title refusal.

## 13. Packaged-runtime smoke test (`ALPHA_RC1_LIVE_SMOKE = PASS`)

Both live sessions loaded the runtime **from the bundle path** (verified in
each session's `LOADED` record: `…/r139-evidence/FGMETAL_D3DMETAL_ALPHA_RC1/
libfgmetal_r127_alpha_runtime.dylib`); correct configuration (bounded
diagnostics armed; `R127_PUMP_THREAD_QOS` honest in both configurations —
`UNSPECIFIED/DEFAULT` and `USER_INITIATED/USER_INITIATED`); capture and
generation active; SOURCE and INTERNAL presentation active (S1 20 022 /
19 947 presented lifetime; S2 6 392 / 6 340); cross-queue shared-event sync on
every INTERNAL (`syncWaits` = INTERNAL count; `syncWaitSkips=0`); zero
duplicate/zero-dropped SOURCE; zero order violations; `pumpUseViolations=0`.
Because the runtime code differs from R138 by the QoS gate, the full
deterministic suite was re-run on the shipped binary (§9) **and** a 10–15 min
gameplay soak was run in the release configuration (S1 soak: 709 s ON ≈ 11.8
min, §14). Bounded failure injection was **not repeated**: control wiring and
ownership/ordering paths are byte-identical to the R138 injection-validated
code; the only delta is thread-attribute configuration (verified live in both
states); R138's 60-pair injection evidence is preserved and applicable. One
bounded `ELDENRING_SOURCE_CAPTURE_PREFLIGHT=FAILED` record appeared during an
S2 OFF flush and was absorbed by the tested bounded capture-failure path with
zero source loss.

## 14. Live soak (release configuration)

S1 soak window (QoS absent): 709 s FG-ON heavy-area gameplay.

- FG-only outages: 241; p50 83.3 ms, p90 100.0, p99 133.3, **max 166.7 ms,
  >250 ms = 0**
- Max consecutive missing INTERNAL slots: 3 (R138: 2)
- droppedSource 0, orderViolations 0, deadlineMisses +10 in-window (~1.4/min,
  R136-class), INTERNAL drops 10 (miss-guard class)
- INTERNAL/SOURCE presented (lifetime at close): 19 947 / 20 022 = 99.62 %
- Title-side (not FG): 616 low-fps intervals; the session-lifetime maximum
  title stall is 1 283 ms (a fast-travel/load stall outside the soak mark
  pair); the largest in-soak title interval was 416.7 ms — the game's own
  heavy-area hitching; SOURCE presented throughout
- GPU submission failures: 0

S2 candidate window (258 s wall from ON boundary to close mark, 240 s
commanded; QoS on): FG-only max 233.3 ms (>250 = 0), max consecutive missing
slots 0, drops/violations 0, in-window ratio 99.71 %.

## 15. Supported platforms and exclusions

Validated: Apple Silicon (M3 host), Highball `Games` bottle, D3DMetal
translation, Elden Ring offline path, 60 Hz display, 30→60 target, this exact
dylib hash. Excluded/unsupported: Intel Macs, Vulkan/DXVK shipping path,
CrossOver installs, other games, 60→120 Hz, EAC/online, multi-display or other
refresh configurations, other macOS/GPU generations. The bundle states this
scope verbatim.

## 16. Known limitations

See `FGMETAL_D3DMETAL_ALPHA_RC1/KNOWN-LIMITATIONS.md`: fixed added latency
(~83 ms schedule anchor + slot alignment; ~137–146 ms present-hook→display at
heavy-area cadences), reduced midpoint density under ~22 fps SOURCE, R136-class
66–100 ms deadline misses (~0.5–0.7 %), edge-haze report unresolved
(INCONCLUSIVE) with the photometric capture finding documented, launch-only-
through-Highball rule, control commands only while the title runs, GPU-abort
window invalidation, non-byte-reproducible build (hash-pinned artifacts).

## 17. Facility/ops lessons (R139)

1. **Highball caches the launch environment per app instance:** a Stop→Play
   relaunch inside one Highball instance reuses the env captured at that
   instance's first launch. After re-staging the bottle, QUIT HIGHBALL
   COMPLETELY and relaunch it, or the title silently loads the previous env.
   This bit twice at session-1 start (two launches came up on the stale R136
   dylib before the full-restart procedure was enforced).
2. **The bottle's resting env on this host is stale-R136-staged** (pre-existing;
   user chose to keep for now). Bare launches inject the old pre-R138 runtime.
   Recommended separate cleanup: set `environment` to `{}` after confirming no
   round needs it.
3. `drive_r13N_window.sh` **truncates** its timeline file at start — one
   timeline file per window, or reconstruct marks from driver stdout (done for
   the target-rate window; reconstruction is marked in-file).
4. The `R127_RUNTIME_SUMMARY=FINAL` one-shot emits a multi-record burst
   (OUTAGE/MISS/PUMP/LATENCY/CLOCK_CHECK + R134 paths) — read the burst, not
   the header line.
5. Bounded log budget suppresses per-slot boundary records; cadence comes from
   PERIODIC-summary deltas of presented counters (ground truth), not from
   boundary records or `sourcePeriodNs` (definition skew noted in §8).
6. Analyzer label parsing: window marks carry labels (`-target-rate`, `-soak`,
   `-qos-candidate`); strip known labels only — naive suffix strips break on
   hyphenated labels.

## 18. Host GPU failure policy

No `kIOGPUCommandBufferCallbackErrorSubmissionsIgnored` (or any unrecoverable
Metal submission condition) occurred in any R139 window; no window was
invalidated. The policy remains armed in the driver (1 s polling, abort +
SUMMARY + OFF).

## 19. Final independent review

Fresh read-only GLM-5.3 Flash Max review over the bundle, report, sources,
suite logs, and live evidence: **PASS** — all 15 review points confirmed
(provider fix intact; packaged == tested binary hash; minimal reproducible
config; prepare/restore correct incl. refusals; ON/OFF verified; SOURCE
exactly-once; no ownership/ordering regression; cross-queue sync active; QoS
gate justified and honestly bounded; no starvation regression — FG-only
>250 ms = 0 and ratio ≥ 99.19 % in all windows; edge haze classified without
overclaiming; performance/latency claims match the windows; scope stated
accurately; no undocumented system changes; suitable for controlled alpha
testing). Reviewer-flagged nits (report wording only) were applied before
finalizing. Full text: `r139-independent-review.md`.

## 20. Remaining release blockers

None. Deferred (non-blocking, R140 candidates): QoS-default flip after a
QoS-ON soak; edge-haze on-screen A/B + colorspace-tag audit; target-rate window
in a qualifying scene; `sourcePeriodNs` definition fix; host bottle cleanup.

## 21. Smallest next step toward external alpha testing

Hand `FGMETAL_D3DMETAL_ALPHA_RC1/` to one technically capable tester on an
Apple Silicon Mac with the same Highball/Elden Ring setup, with the README
workflow (prerequisites → prepare → launch via Highball → `fg_ctl.sh on`) and
one feedback question set: FG ON/OFF perceivability, edge-haze visibility, and
thermal/stability over a 30-minute session.
