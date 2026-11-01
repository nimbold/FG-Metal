# Round reports

The dated record of the title-integration line, from the first deterministic harness to the present blocker. Each report states what it set out to prove, what the measurement returned, and what it did not establish.

Newest first.

| Report | Subject | Outcome |
| --- | --- | --- |
| [R121 — Presentation occupancy](r121-presentation-occupancy.md) | Decompose the ~25 ms presentation-side occupancy | Dominant blocker measured and classified as `GPU_CAPACITY_FOR_INTERPOLATION`; no fix promoted, with reasons |
| [R120 — Continuous 30→60](r120-continuous-30-to-60.md) | Reach a continuous 30→60 output | `CONTINUOUS_30_TO_60_TITLE_FG = NOT_VALIDATED`; source cadence established, output 30.64 fps |
| [R119 — Live contention](r119-live-contention-and-evidence-isolation.md) | Separate GPU contention from scheduling cost | `GENERATION_THROUGHPUT_BELOW_SOURCE_RATE`; 19.728 ms service against 17.502 ms arrival, deficit is contention |
| [R118 — Live throughput](r118-live-generation-throughput.md) | Confirm the optimised estimator under live load | `GENERATION_THROUGHPUT_BELOW_SOURCE_RATE`; 20.452 ms against 16.939 ms |
| [R117 — Look-ahead feasibility](r117-lookahead-2x-feasibility.md) | Half-interval A→M→B pacing and estimator cost | `HALF_INTERVAL_PRESENTATION_BLOCKED`; estimator 2.8× faster and bit-identical; 60→120 not attempted |
| [R116 — Critical-path pacing](r116-critical-path-pacing.md) | Remove CPU cost from the generation path | Fast gate 31.28 ms → 0.128 ms; 51/51 tuples ordered; continuous 2× still not validated |
| [R115 — Re-armable continuous](r115-rearmable-continuous-title.md) | Repeat A→M→B across many pairs | `REARMABLE_REAL_TITLE_A_TO_M_TO_B = VALIDATED`; 60 tuples, zero leaks; 2× not claimed |
| [R114 — In-process target latch](r114-inprocess-target-latch.md) | Display a generated frame through the game's own layer | `FIRST_REAL_TITLE_D3DMETAL_A_TO_M_TO_B = VALIDATED`; one pair, game stable for 9,864 further frames |
| [R106 — Title-derived motion](r106-title-derived-motion.md) | Validate the derived-motion fallback and inspect a real title texture | Bounded fallback validated on the harness; first Elden Ring MetalFX texture generated |
| [R105 — Title inputs](r105-title-inputs.md) | Find real depth and motion inputs at the title's producer | `ELDEN_RING_RENDER = VALIDATED`; `REAL_TITLE_DEPTH = NOT VALIDATED`; depth remains `CONSTANT_DEPTH_PROTOTYPE` |

## Step 11 closeout

| Document | Contents |
| --- | --- |
| [Reconciliation](step11-closeout/reconciliation.md) | Current BR9R/H6 disposition, essential Step 11 work, and 11E readiness |
| [A — Roadmap](step11-closeout/a-roadmap.md) | Remaining Step 11 sequence |
| [B — Invariants](step11-closeout/b-invariants.md) | Invariants the two-source transport must satisfy |
| [C — 11E plan](step11-closeout/c-11e-plan.md) | Experiment design for the 11E transport proof |

## Scope

These reports are the published subset of the round log. Earlier rounds (R65–R113) and the storage-gate bookkeeping are retained locally, not tracked; see the `.gitignore` header for the boundary and the reasoning. Evidence directories are likewise local — each report names the harness, run parameters, and build hashes needed to reproduce its measurement.
