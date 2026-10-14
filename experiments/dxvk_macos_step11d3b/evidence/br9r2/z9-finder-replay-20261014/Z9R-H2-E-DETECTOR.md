# Z9R-H2-E — Trace / detector audit

**Result: NO-GO for replay with the current protocol timing.** This was a read-only source audit; I did not launch Wine/Finder, run tests, or execute an experiment.

## Source identity and frozen thresholds

- Current `z9_fast_zero_detector.py` SHA-256: `af6c2b69893e6c59fb453f5e140901a711411ba0e75c68747f11eaef1fadd582`. This exactly matches the detector hash bound in the retained controller record (`historical-closure-review/source-controller/controller.json`, lines 106–107): **PASS, byte identity matches the prior binding**.
- Detector source: `experiments/dxvk_macos_step11d3b/evidence/br9r2/z9-finder-replay-20261014/z9_fast_zero_detector.py`.
- It uses a 50 ms evaluation grid and 500 ms primary windows (lines 13–15, 375–376). The frozen primary gate is two adjacent passing windows; each window uses **counts**, not per-second rates: successful acquire count >=45, submit count >=45, callbacks >=40, displayed callbacks <=1, zero fraction >=0.95, >=20 drawable gaps, median <=12 ms, nearest-rank p90 <=14 ms (lines 195–209, 413–423). Component gates are 250 ms windows (lines 212–229, 424–434). I found no threshold change relative to the hash-bound source.
- Report accuracy issue: the prior report at `STEP-11D3-BR9R2-REPORT.md:853` paraphrases acquire/submit and displayed thresholds as rates per second. That is not what `primary_pass` implements: these are counts in each 500 ms window. H2 reporting should preserve count semantics per the request.

## Actual replay healthy-renderer gate

- `z9_replay_safety.py` SHA-256: `e42a5391a31d59e82c18ff83658b173f8d3fd82733894b86182a99430b7bf002`.
- `renderer_healthy` evaluates the trailing 2 seconds (line 64), validates PID/token/clock/event identity and sequence integrity (lines 66–182), and requires continuous coverage for each successful-acquisition, successful-submit, displayed-callback, and next-drawable stream: first and last event within 100 ms of window boundaries and no inter-event gap above 100 ms (lines 38–58, 202–224). It additionally requires 55–65 successful acquire and submit events/s, >=55 displayed callbacks/s, zero fraction <=5%, drawable median 14–18 ms and p90 <=22 ms (lines 214–224). This is a distinct renderer-health gate, not the fast-zero detector thresholds.
- The replay runner SHA-256 is `b4d1778f57ba928fe908e555aba84579fc15642a4fcd0a159464b494090bf734`.
- The runner checks `renderer_healthy` in `canonical_precondition` before accepting the actual replay process (runner lines 1419–1518, especially 1485–1496). It checks it again inside the measured PRE loop before each scheduled Finder action and raises before proceeding if unhealthy (lines 1711–1722). Therefore a healthy 2-second rolling renderer state is required on the same target PID before the first Finder action: **PASS for the pre-action health gate**.

## Coverage and timing blockers

- Burst-then-idle pseudo-health is rejected by the renderer PRE gate: each renderer stream must be continuous, so burst counts cannot substitute for recent first/last events and <=100 ms gaps. The source test explicitly constructs a 60/s burst followed by an idle tail and asserts coverage and health fail (`test_z9_replay_safety.py:310–335`). Static source evidence: **PASS**; test was not run.
- The fast-zero detector itself is count/window based and only requires two adjacent 500 ms passing windows on the 50 ms grid (`z9_fast_zero_detector.py:269–282`). It has no separate max-inter-event-gap/first-last continuity predicate. Therefore do not use a detector-positive result alone as proof of continuous renderer coverage; raw trace and the independent coverage gate remain necessary. Its semantics are frozen and must not be retuned.
- The current runner's first Finder activation is scheduled at PRE +3 s, followed by new Finder window creation at +4 s, raise at +5 s and staging at +6 s (`z9_finder_replay.py:1694–1704`, 1731–1749, 1877–1885). The loop rechecks renderer health immediately before those actions, but the requested 20-second PRE is not action-free. **FAIL against the H2 sequence requirement** to complete canonical PRE before treatment. Combined with the pre-action health check, the code proves healthy state before treatment only at the current action boundary; it does not provide a full 20-second no-treatment baseline.

**Disposition:** Do not start this replay with the current runner. Preserve the frozen detector and its count-based thresholds. A reviewed runner/protocol reconciliation is needed so the full required PRE elapses before any Finder action; then Gate 0 and lifecycle authorization must be refreshed by the top-level procedure.
