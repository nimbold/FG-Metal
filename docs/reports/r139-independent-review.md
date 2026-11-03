# R139 independent review (fresh read-only GLM-5.3 Flash Max subagent, 2026-11-04)

Verdict: ALPHA_RC1_INDEPENDENT_REVIEW = PASS
(15/15 points CONFIRMED; 4 documentation corrections required, all applied;
binary and safety behavior untouched. Full findings summarized below; the
reviewer independently recomputed hashes, re-ran the wake-lateness extractor
on the stated line ranges, and diffed the runtime source against the R138
postfix copy.)

- 1 Provider fix intact — CONFIRMED (provider .m warmup commit sets
  hasValidHistory; discard branch gone; smoke contract + suite row PASS).
- 2 Packaged == tested binary — CONFIRMED (bundle = build-dir = MANIFEST sha
  3cd5e8ee…). Correction applied: MANIFEST listed the Phase-A pre-gate
  observer hash; now records the RC1 build input 48bd01af… with the baseline
  annotated.
- 3 Minimal config — CONFIRMED (env key-for-key vs R138 validated set; QoS
  absent by default). Nit applied: FGMETAL_EVIDENCE_DIR added to MANIFEST.
- 4 Prepare/restore — CONFIRMED (only `environment` replaced; snapshots;
  three refusal paths; STAGED→ORIGINAL cycles recorded).
- 5 ON/OFF — CONFIRMED (5 clean cycles; no-title refusal; atomic writes).
- 6 SOURCE exactly-once — CONFIRMED (droppedSource=0; enqueued==presented S1;
  orderViolations=0 in all 26 records).
- 7 Ownership/ordering — CONFIRMED (runtime diff vs R138 = exactly 4 QoS-gate
  hunks; orderViolations=0; pumpUseViolations=0).
- 8 Cross-queue sync — CONFIRMED (R135_CAPTURE_SYNC=1; syncWaitSkips=0).
- 9 QoS evidence — CONFIRMED (reviewer re-ran the extractor; numbers match
  §5 exactly; KEPT-as-gate-default-off honestly described).
- 10 No starvation regression — CONFIRMED (FG-only max 166.7/233.3 ms,
  >250=0; ratios 99.62%/99.19% lifetime; zero GPU failures).
- 11 Edge haze — CONFIRMED verdict; disclosure gap fixed: 8 events/42 PNGs
  captured, 3 (01–03) analyzed, 04–08 preserved unanalyzed and now disclosed.
- 12 Numbers vs logs — CONFIRMED; nits fixed: S1 fps floor 20.2; S2 strict
  in-window ratio 99.71% (99.60% counter-based annotated); S2 window 258 s;
  1 283 ms title stall marked session-lifetime (in-soak max 416.7 ms).
- 13 Scope accuracy — CONFIRMED.
- 14 No undocumented system changes — CONFIRMED.
- 15 Alpha suitability — CONFIRMED; gap fixed: README step 2b (fully quit and
  relaunch Highball after prepare) + KNOWN-LIMITATIONS latency/haze entries
  now match report §16 claims.
- Path disclosure and stale-R136 resting-bottle finding — CONFIRMED adequately
  disclosed (report §3/§17, PROVENANCE, replacedEnvironment records).

## Delta review (same reviewer, 2026-11-04)

DELTA_VERDICT: corrections 1–4 CONFIRMED — required-corrections list closed;
ALPHA_RC1_INDEPENDENT_REVIEW stands at PASS. Hash gates unaffected (dylib
still 3cd5e8ee…). One optional reword (event-timing phrase in §6) applied:
"captured during early S1 FG-ON gameplay".
