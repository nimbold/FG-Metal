# FG-METAL — R118 Live Generation Throughput Report

| Verdict | Result |
|---|---|
| `GENERATION_THROUGHPUT >= SOURCE_PAIR_RATE` | **`GENERATION_THROUGHPUT_BELOW_SOURCE_RATE`** (§15) |
| Optimised R117 estimator loaded live | **`VALIDATED`** — binary marker scan + live bit-identity (§2, §13) |
| Live generation service, median | **20.452 ms** against a measured **16.939 ms** source arrival |
| Ratio | **1.207×** median-over-median, **1.227×** against the nominal 16.6667 ms period |
| Backlog growth | **none** — service is uniformly above arrival, not diverging (§12) |

R118 asked one question: does the optimised generation pipeline stay under the
source-pair arrival interval while Elden Ring is actually rendering?

**It does not.** Across 21 gate-accepted in-world tuples in a single game process,
the median FG generation service is **20.452 ms**. Not one of the 21 came in under
the 16.6667 ms refresh period; the fastest was 18.091 ms. The measured source
arrival during the same run is 16.939 ms median, so the pipeline is **1.207×** too
slow to consume a 60 fps source without accumulating backlog.

The R117 optimisation is real and it is present — the motion field produced live is
byte-identical to the reference estimator (§13). It is simply not enough. R117
offered two projections for this number, 13.9 ms and 18.8 ms; the measurement is
**worse than both**, which is why it needed measuring rather than modelling.

---

## 1. WorkBuddy / model assignment verification

**Harness:** WorkBuddy AI. **Orchestrator:** DeepSeek V4.1 Flash.

**What is verifiable and what is not.** The harness reports the model family. It
does not expose a reasoning-effort tier to the model itself, so no claim of the form
"Max verified" can be made honestly from inside the run. Stating the tier as
confirmed would be an assertion without evidence.

**A contradiction in the round brief, recorded rather than resolved silently.** The
brief's assignment block specifies *"Primary orchestrator: DeepSeek V4.1 Flash —
High"*, while its fail-closed policy says *"Do NOT silently downgrade the orchestrator
from Max to High."* Those two instructions cannot both be satisfied. The run
proceeded on the explicit assignment (Flash), which is also the tier every prior
round in this series recorded, and did **not** stop with
`REQUIRED_ORCHESTRATOR_UNAVAILABLE` — that condition is for the model being
*unavailable*, and it was available. If the intent was Max-only, the brief's own
assignment line is wrong and should be corrected before R119.

No other model family was used and no fallback occurred.

**Agent structure:** kept deliberately small, as instructed — one implementation
owner (the orchestrator), one independent review commission, no agent army.

**The independent review did not land, and that is a gap.** One reviewer subagent was
commissioned against the instrumentation, the metric definitions and the build
verification. It ran to completion but returned an empty result to this session; no
finding from it is incorporated here and none is claimed. The defects listed below
were found by the orchestrator's own audit, and three of them were serious:

1. The build-verification script was pointed at the **R117** library while the run
   would load the **R118** one. Fixed; the shipped library is now the one verified.
2. The decomposition self-check summed a term that sits *before* the service window
   and so could never reconcile. Fixed; it now reconciles to 0.000000 ms.
3. The first analysis pass ran against an **observer log that was still being
   written**, understating the source-frame count by 27 %. Every number in this
   report is from the complete log.

A fourth defect was environmental and cost one full run: the bottle environment was
missing `FGMETAL_R100_PRESENT=1`, which left `internalPresentationPipeline` nil so
every accepted tuple fell back at the internal copy. That run is retained as
`run-01-generation-only` and is used only as corroboration (§5.3), never as the
authoritative measurement.

## 2. Optimised estimator build verification

The brief's warning — *"Do not accidentally run the R116 estimator"* — is the
failure mode this section exists to exclude. A stale kernel compiles, produces
correct motion, and silently invalidates every timing number.

`tools/verify_r118_estimator.py` performs three checks and is run against the exact
library the launcher pins by hash.

| Check | Method | Result |
|---|---|---|
| Source literal | Decode the C string literal passed to `newLibraryWithSource:` in the observer | R117 form present, R110 form absent |
| **Binary literal** | Scan the compiled dylib for **every** embedded Metal source, not just the largest | R117 form present, R110 form absent in **all** literals |
| Prefix identity | The source literal must be an exact prefix of the binary literal, with only the R116 gate source appended | Match, +5,662 bytes of gate kernels |

```
R118_BUILD_CHECK verdict=R117_OPTIMIZED_ESTIMATOR_PRESENT
R118_BUILD_CHECK dylibSha256=ccc0035d1602eeefded4003b2050724d7b35ca43cc6a3694efec0c3707a5ccbe
R118_BUILD_CHECK binaryLiteral r117Form=True r110Form=False parabolaProbes=5 bytes=17338
R118_BUILD_CHECK sourceLiteral r117Form=True r110Form=False bytes=11676
R118_BUILD_CHECK coreMatch=True embeddedOffset=201552
```

The discriminating markers are the R117 sample-outer accumulation arrays
(`int2 shifts[81]`, `float costSum[81]`, `costSum[k] += weight * abs(sampleY - b)`)
against the R110 candidate-outer call
`r110FinePatchCost(previous, current, center, target, shift)`. The five sub-pixel
parabola probes are present in both versions by design and are reported, never used
as a discriminator.

Scanning *every* literal rather than the largest is deliberate: a tree containing
both an optimised and an unoptimised estimator would pass a largest-literal check
while shipping whichever the pipeline actually selected.

**Launcher guard.** `launch_r118_game.sh` refuses to start if the dylib hash differs
from `ccc0035d…`, so the library that was verified is the library that runs.

**Focused preflight, as instructed — one confirmation, not a new research phase.**
The shader literal extracted from the shipped binary was compiled through the same
runtime path the product uses and run on the accepted, actually-displayed R114 pair:

```
R118_PHASE4 pair=2749->2750
R118_PHASE4 live-vs-reference=BIT_IDENTICAL bytes=5621280
```

## 3. Live title process

| Requirement | Observed |
|---|---|
| Exact executable | `/Users/nima/Games/Elden Ring/Game/eldenring.exe` |
| Exact Wine/game PID | **26228** (parent 26223) |
| `ELDEN RING™` window | present, `CAMetalLayer` selected |
| Actual loaded world | in-world; world-scene confirmation accepted by the gate on every tuple |
| Apple M3 layer/device | `TITLE_LAYER_SELECTED ... deviceName=Apple M3`, registryID `0x1b020209` |
| Optimised R117 library loaded | `DYLD_INSERT_LIBRARIES` contains `libfgmetal_r118_throughput_probe.dylib`, hash `ccc0035d…` |
| Display | 60.000 Hz, all five advertised modes (re-confirmed, §3.1 of R117 unchanged) |

**Launch route — a deviation that must be recorded.** The round specified the
"accepted Highball R29 D3DMetal route". The project's own `launch_r116_game.sh`
does **not** work on this engine build: invoking `wine` directly aborts with

```
tsshim: cannot load the real D3DMetal d3d12.dll (d3d12_d3dmetal.dll, error 126)
err:module:loader_init "d3d12.dll" failed to initialize, aborting
```

The engine manifest explains why: the D3DMetal timestamp shim requires two artefacts
that *Highball creates at launch* — a hard link `d3d12_d3dmetal.dll` beside the shim
and a symlink to `x86_64-unix/d3d12.so`. Reconstructing both by hand was attempted
and still failed (error 126 persisted, including with `HB_D3D12_REAL` set to a full
path), so the direct route was abandoned rather than forced.

The run therefore used the **Highball launcher itself** with the probe injected
through the bottle's `environment` map — which is the route the earlier live rounds
actually used. R116's own reproduction section specifies the bottle environment and
never claims the shell script worked; the script is nonetheless misleading, because
it looks like the supported entry point and has never produced a live log (the R116
launch log contains the same d3d12 abort). It should be deleted or repaired before
R119 rather than copied forward again.

Bottle configuration used for the run is preserved verbatim in
`bottle-env-r118.json`; the bottle was restored to its pre-run state afterwards.

## 4. Physical-motion test

All camera movement was the operator's. No synthetic gameplay input was sent, no
pair was written externally, and the controller only observes.

| Signal | Value |
|---|---|
| Gate-accepted pairs | **20** display-confirmed of 21 gate-accepted |
| Rejected pairs | 4, all `motion-below-frozen-pair-derived-threshold` |
| Acceptance ratio | 0.800 |
| Tuple budget | 25 attempts against a 20-tuple target (3× cap not reached) |

The four rejections are the gate doing its job: `activeFraction` fell below the
frozen 0.15 threshold when the camera sweep slowed. They are not measurement
failures and their service times are reported separately (§9.4).

An earlier attempt in the same session lost 16 of 60 pairs to the same threshold
(activeFraction 0.141). That is why the operator was asked for a brisker sweep.

## 5. 20-tuple measurement run

### 5.1 Configuration

R116 presentation path frozen and **running**, deliberately, so the measurement is
taken under real game and display contention:

```
FGMETAL_R100_PRESENT=1   FGMETAL_R108_CAPTURE=1   FGMETAL_R108_FRAMEBUFFER_ONLY=1
FGMETAL_R110_PRESENT=1   FGMETAL_R114_INPROCESS_LATCH=1
FGMETAL_R115_CONTINUOUS=1  FGMETAL_R115_MAX_TUPLES=20
FGMETAL_R116_B_EARLY=1     FGMETAL_R116_WATCHDOG_MS=150
```

No presentation, scheduling, gate, frontier or accounting code was changed. The
only product change is additive instrumentation: one clock read at the generation
entry point, one assignment at the gate verdict, and one log line.

### 5.2 Result

| | Value |
|---|---:|
| Source frames observed | 8,045 |
| Tuples attempted | 25 |
| Gate-accepted | 21 (20 display-confirmed) |
| Fallback | 4 |
| Service records emitted | 25 (0 skipped) |
| Decomposition reconciliation error | **0.000000 ms** |

### 5.3 Corroboration from the earlier run

`run-01-generation-only` (60 attempts, 44 gate-accepted, display path inert) gives a
service median of **21.024 ms** against an arrival median of 18.427 ms — ratio
**1.141**. Different run, different process, different display-path state, same
conclusion. It is corroboration only; the authoritative numbers are §5.2.

## 6. Estimator timing

| Metric | n | min | mean | median | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| Estimator, CPU wall (commit → complete) | 21 | 7.948 | 11.110 | **11.462** | 12.503 | 16.072 |
| Estimator, GPU (`GPUEndTime − GPUStartTime`) | 21 | 6.777 | 9.740 | **10.218** | 11.082 | 13.771 |
| CPU wall minus GPU span | 21 | 1.128 | 1.370 | 1.283 | 1.552 | 2.301 |

**Against the R117 offline figure.** The same kernel measures **6.0595 ms** offline
at 1470×956. In-game the GPU span is **10.218 ms** — a contention factor of
**1.69×**. The R117 optimisation survived intact; the GPU it runs on is busier.

**What the 1.283 ms gap is and is not.** It is the difference between the CPU
blocking on `waitUntilCompleted` and the command buffer's own GPU execution window:
commit, scheduling behind the game's work, and wake-up. It is **not** a queue-depth
measurement and this round does not claim it as one. It does bound the scheduling
overhead at roughly 1.3 ms per pair, which is small relative to the estimator itself.

**What this does not establish.** The stage split (downsample / coarse / refine /
propagate / upsample) is not re-measured live. R117 measured it offline at 3.34 ms
coarse and 2.46 ms refine after optimisation; R118 measures only the composite. The
live composite is consistent with those stages under 1.69× contention, but that is
an inference, not a measurement, and is labelled as such.

## 7. MetalFX / gate timing

| Metric | n | min | mean | median | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| MetalFX transition (blocking call) | 21 | 4.962 | 5.906 | **5.656** | 7.162 | 7.467 |
| Estimator complete → MetalFX submit | 21 | 0.149 | 0.326 | 0.303 | 0.462 | 0.564 |
| Gate CPU decision | 21 | 0.118 | 0.474 | 0.268 | 0.303 | 4.898 |

MetalFX is **1.12×** its 5.07 ms offline figure — barely contended. The gate remains
at the R116 level: **0.268 ms median**, with the 4.898 ms maximum a single outlier
rather than a return of the 31 ms synchronisation bubble. `gatePath` was
`transition-command-buffer` on every tuple; the legacy separate-command-buffer path
never ran.

The 0.303 ms gap between estimator completion and MetalFX submission is the motion
diagnostic export and its logging — see §12.4 for why it is disclosed rather than
optimised away mid-round.

## 8. Generation-service definition

Stated once, applied everywhere, and chosen so that neither side is charged for the
other's work.

**Primary — `presentHookToGateNs`.** From the B present hook, which is the first
instant FG touches the pair (the source blit is encoded there), to the gate verdict,
which is the last instant FG needs it. It contains, in order: blit encode and commit,
source copy on the GPU, the dispatch hop to the capture queue, preparation, the
estimator, the diagnostic export, the MetalFX transition, and the gate decision.
It **excludes** the game's own render time for B.

**Secondary — `genThreadServiceNs`.** The same window minus the blit and the
dispatch hop, i.e. from `processCapturedFrame` entry to gate verdict. Reported so the
copy/hop component is visible instead of buried in the total.

**Context only — `endToEndServiceNs`.** From B's `nextDrawable` acquisition to the
gate verdict. **This is not an FG cost**: it includes whatever the game did between
acquiring the drawable and presenting it. It is reported only to show how much of
the wall clock is not FG's.

**Why not the estimator alone.** 11.462 ms against 16.939 ms would read as 1.48×
headroom and would be wrong — it is exactly the framing error R117 corrected in its
own §10.4. The pipeline cannot produce an INTERNAL frame by running the estimator;
it must also copy, transition and decide.

**Why not `endToEndServiceNs` as the primary.** It would inflate the service by
charging the game's frame time to FG, making the failure look larger than it is.
The primary metric deliberately under-charges FG rather than over-charging it.

## 9. Generation-service distribution

### 9.1 Primary

| Metric | n | min | mean | median | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| **FG generation service** | 21 | **18.091** | 21.297 | **20.452** | 24.751 | 26.520 |

**Every accepted tuple is above the 16.6667 ms refresh period.** The distribution is
tight — 18.1 to 26.5 ms, standard deviation **2.26 ms**, interquartile range
**19.51–23.00 ms** — so this is a uniform deficit, not a tail problem. Zero of 21
tuples met the strong target (< 16.67 ms); 8 of 21 were under 20 ms.

### 9.2 Secondary and context

| Metric | median |
|---|---:|
| Generation-thread service | 17.905 ms |
| End-to-end from drawable acquisition (context) | 21.401 ms |

The difference between primary and generation-thread — about **2.55 ms** — is the
source blit and the dispatch hop, measured independently as a 2.672 ms median
`bAcquiredToGenStart`. That component alone is 12 % of the service and is larger
than the entire margin the pipeline would need.

### 9.3 Decomposition

Medians, and they reconcile to `genThreadServiceNs` exactly on every tuple
(max error **0.000000 ms** across all 21):

| Component | median | share of 20.452 ms |
|---|---:|---:|
| Copy + dispatch hop | ~2.55 | 12.5 % |
| Preparation (alloc, encode) | 0.207 | 1.0 % |
| **Estimator** | **11.462** | **56.0 %** |
| Diagnostic export gap | 0.303 | 1.5 % |
| **MetalFX transition** | **5.656** | **27.7 %** |
| Gate decision | 0.268 | 1.3 % |

### 9.4 Rejected tuples

The 4 gate-rejected tuples have a service median of **21.029 ms** — statistically
indistinguishable from the accepted ones. Rejection does not avoid the generation
cost; the estimator and the transition run either way. Throughput is therefore a
property of the pipeline, not of gate selectivity.

## 10. SOURCE-pair arrival distribution

Measured from `R108_SOURCE_FRAME_ACQUIRED` monotonic timestamps for consecutive
source sequences in the same process. The nominal 16.6667 ms period is **not**
substituted anywhere.

| Metric | n | min | mean | median | p90 | max |
|---|---:|---:|---:|---:|---:|---:|
| Source arrival interval | 8,044 | 0.502 | 19.369 | **16.939** | 21.449 | 1833.708 |

| Derived | Value |
|---|---:|
| Effective arrival rate, median-implied | **59.0 Hz** |
| Effective arrival rate, mean-implied | 51.6 Hz |
| Display refresh period (reference only) | 16.6667 ms |

The median sits **1.6 % above** the refresh period, so the title really is a ~60 Hz
source and the display really is the cadence it presents at. The mean is dragged
down by stalls; the median and p90 are the honest description.

**Two caveats that must travel with this number.**

1. *It is acquisition, not presentation.* `nextDrawable` returns before the game has
   drawn the frame, so this is "when the game started frame N". It is the correct
   measure of when a pair becomes available to FG's capture path.
2. *It is endogenous.* FG's own per-frame cost is part of what sets it. That is
   unavoidable in a single-process measurement and is exactly why the in-tuple
   figure below must not be used as the denominator.

**The denominator that must not be used.** Measured across only the frames bounding
the tuples, the arrival median is **25.516 ms** — because the pipeline is stalling
the game while it generates. Dividing 20.452 by 25.516 gives 0.80 and would "pass".
That number is circular: the stall is the cost being measured. It is reported here
only so that a reader who computes it knows why it is not the answer.

**Robustness.** The verdict does not depend on the choice among defensible
denominators: all frames (16.939 ms) and all frames excluding stalls over 100 ms
(16.938 ms) both give a ratio of **1.207**.

## 11. Throughput ratio

| Comparison | Ratio |
|---|---:|
| **median service / median arrival** (primary) | **1.207×** |
| mean service / mean arrival | 1.100× |
| p90 service / p90 arrival | 1.154× |
| median service / p10 arrival | 1.276× |
| median service / nominal 16.6667 ms | 1.227× |

The pipeline must get **17.2 % faster** to break even at the median (20.452 → 16.939
ms, a 3.513 ms reduction), and **18.5 % faster** to hold 60 fps against the nominal
period (→ 16.6667 ms, a 3.785 ms reduction). Because the minimum observed service
(18.091 ms) is itself above the arrival median, there is no operating point in this
distribution that keeps up.

## 12. Backlog / contention analysis

### 12.1 No backlog — and that matters

| Metric | Value |
|---|---:|
| Service slope over the run | **−0.061 ms/tuple** |
| Estimator slope | −0.046 ms/tuple |
| MetalFX slope | −0.003 ms/tuple |
| First-half median service | 21.804 ms |
| Second-half median service | 19.885 ms |
| Monotonic growth | **No** |

Service *improves* slightly across the run. There is no command-buffer backlog, no
increasing estimator time, no increasing MetalFX time.

This distinction is the round's most useful structural finding:

> The generation path is **uniformly too slow**, not **progressively saturating**.

A look-ahead queue would therefore accumulate backlog at a roughly constant
**3.5 ms per pair** (20.45 − 16.94) — linear growth in latency at about 210 ms per
second of gameplay. A deeper buffer buys time; it cannot fix a rate deficit. This is
the arithmetic R116 §18 predicted and R118 now measures.

### 12.2 Where the contention lands

| Stage | Offline | Live | Factor |
|---|---:|---:|---:|
| Estimator GPU | 6.06 ms | 10.218 ms | **1.69×** |
| MetalFX | 5.07 ms | 5.656 ms | 1.12× |

The estimator absorbs nearly all of the contention. That is expected — it is the
longest-running GPU dispatch and therefore the one that queues behind the game's
render most often.

### 12.3 The copy/dispatch tail

`bAcquiredToGenStart` is bimodal: a 2.67 ms median but four tuples at 15.7–17.1 ms.
Those four inflate the mean to 4.918 ms and produce the run's only 400 ms-class
outlier in the earlier pass. They are dispatch-queue stalls, not GPU work, and they
are the one component with an obvious mechanical fix available in a later round.
They are reported rather than smoothed.

### 12.4 What the instrumented build costs

The observer writes a 22 MB source raw, a 22 MB diagnostic copy and a motion field
per tuple, and logs a line per event. Some of that lands inside the measured window
(the 0.303 ms diagnostic-export gap is visible in the decomposition). The published
service is therefore an **upper bound for a diagnostics-enabled build**, not a floor.
That direction is conservative: it cannot make a failure look like a pass.

## 13. Regression equivalence check

One live pair was preserved with its source A/B and resulting motion field, and
compared against the accepted reference implementation.

```
R118_PHASE4 pair=1895->1896 resolution=1470x956
R110_ESTIMATOR_MEDIAN_GPU_MS=17.948   (reference, R110 candidate-outer)
R110_ESTIMATOR_MEDIAN_GPU_MS=7.602    (R117 sample-outer, same pair)
R118_PHASE4 live-vs-reference=BIT_IDENTICAL bytes=5621280
R118_PHASE4 live-vs-embedded=BIT_IDENTICAL
R118_PHASE4 reference-vs-embedded=BIT_IDENTICAL
```

Three independent statements, all byte-exact:

1. The motion field the shipped library **actually produced in the game** is
   identical to the reference estimator re-run on the same sources.
2. The library's own extracted kernel text, recompiled, reproduces what it did live.
3. Reference and optimised agree with each other.

This is the strongest available evidence that the numbers in §6–§9 belong to the
optimised kernel. A wrong-kernel regression is excluded.

**Evidence-integrity regression — reported, not buried.** The observer's evidence
directory is a compile-time constant pointing at the R115 run folder, so this round
wrote its diagnostics into it and **overwrote 25 R115 source raws and 5 R115 motion
fields**. Consequence: the R117 pairs 2519→2520 and 2749→2750 can no longer be
re-run from disk, because their B-side source raws are now R118 frames. Their
conclusions are unaffected (R118 re-established the same equivalence live), but
reproducibility of that specific R117 check is lost.

Contained: all 345 R118 artefacts were moved into `run-08-r118-throughput/`, and the
damage is itemised in `EVIDENCE-INTEGRITY-NOTE.md` and
`r115-overwritten-source-raws.txt`. The fix — making the directory
`FGMETAL_EVIDENCE_DIR`-configurable — was deliberately **not** applied mid-round,
because rebuilding would have changed the dylib hash after the measurement and the
report's numbers must belong to one exact binary. It is the first item of R119.

## 14. Accounting / stability

| Requirement | Observed |
|---|---|
| Game responsive | Yes — 6,051 source frames acquired after run completion |
| No crash | Clean exit, `# exit=0 after 172s`; no crash report |
| No black screen | 20 display-confirmed INTERNAL presentations |
| No stuck frontier | `sourceLeaksWhileClosed=0`, `R103_SOURCE_FRONTIER=OPEN` on every terminal |
| No stale tuple | `staleCallbacks=0`, `duplicateMDispatch=0` |
| No persistent capture state | `resets=24`, all tuples reached IDLE |
| Watchdog | `watchdogArmed=24`, **`watchdogFired=0`** |
| Ordering | `orderingViolations=0`, `coalescedPresentations=0`, `presentationCallbackMissing=0` |
| Early-B dispatch | 20 of 20 |
| Promotion eligibility | **`promotionEligible=1`** |

The R116 accounting contract is untouched: `AppFrameId` SOURCE-only,
`InternalPresentId` INTERNAL-only, `WsiPresentId` unique, exactly-once terminal
state. The instrumentation reads no accounting variable and writes none.

**Display behaviour.** The displayed stream stayed on the 60 Hz contract established
in R117. Nothing here re-opens 60→120, and the delivered output rate is **not**
interpreted as a 2× validation — the display remains the known bottleneck.

## 15. Throughput verdict

### `GENERATION_THROUGHPUT_BELOW_SOURCE_RATE`

The success condition required all of: a live Elden Ring workload, the optimised
R117 estimator loaded, multiple accepted real moving pairs, end-to-end generation
service measured, service median below arrival median, no monotonic backlog, no
worsening service time, and a stable game.

Seven of eight hold. The sixth does not.

| Requirement | Status |
|---|---|
| Live Elden Ring workload | **Met** — pid 26228, in-world |
| Optimised R117 estimator loaded | **Met** — marker scan + live bit-identity |
| Multiple accepted real moving pairs | **Met** — 21 gate-accepted, 20 display-confirmed |
| Generation service measured end-to-end | **Met** — decomposition reconciles to 0.000000 ms |
| Service median below arrival median | **NOT MET** — 20.452 vs 16.939 ms, 1.207× |
| No monotonic queue/work backlog | **Met** — slope negative |
| No worsening service time | **Met** — second half faster than first |
| Stable game | **Met** — clean exit, no watchdog, no crash |

**Dominant remaining cost, identified and not redesigned.** The estimator is
**11.462 ms of the 20.452 ms service (56 %)**, of which 10.218 ms is GPU execution
under a 1.69× contention factor. MetalFX contributes a further 5.656 ms (28 %) and
the copy/dispatch hop ~2.55 ms (12 %).

No mechanical regression was found that would justify touching the estimator
mid-round. The kernel is the right one (§2, §13), there is exactly one estimator
command buffer per pair, and there is no duplicate source conversion. The
serialisation is by design — the gate needs the result — and costs 1.283 ms, not the
4.2 ms that is missing.

**R117's projections are superseded, and both were optimistic.** The multiplicative
model said 13.9 ms, the additive-steal model said 18.8 ms; the measurement is
20.45 ms. Neither is cited further.

## 16. Next product path

### `PATH B` — throughput fails; R119 remains a software-performance phase

Per the round's own decision gate, and because the measured blocker is a rate deficit
rather than a display or scheduling artefact.

**Do not build display look-ahead.** R116 §18 and R118 §12.1 now agree on measured
grounds: a bounded buffer converts a 3.5 ms/pair rate deficit into linearly growing
latency. That is worse than the current behaviour, not better.

**R119 scope, in priority order:**

1. **Evidence-directory fix** (`FGMETAL_EVIDENCE_DIR`), before anything else. One
   round of measurement must not be able to destroy another round's corpus.
2. **Attack the estimator's live contention, not its offline speed.** The offline
   kernel is 6.06 ms; the live GPU span is 10.218 ms. The 4.2 ms difference is
   scheduling, not arithmetic, and it is now the single largest addressable block.
   Concretely worth measuring: whether the estimator's command buffer can be kept
   out of the queue behind the game's render, and whether `waitUntilCompleted` on the
   generation thread is the reason the dispatch waits.
3. **The copy/dispatch tail.** Four tuples at ~16 ms against a 2.67 ms median. If
   that tail is a queue-starvation artefact it is cheap to remove; if it is the
   display hold, it is not, and measuring which is the first task.
4. **MetalFX (5.656 ms, 28 %)** is the second-largest block and the least contended
   (1.12×). It is not the priority, but the arithmetic says estimator-only
   improvements cannot close a 3.5 ms gap that is 56 % estimator.

**Explicitly out of scope for R119** unless throughput is solved: look-ahead,
generalised scheduling, display timing experiments, 60→120 claims, depth research,
DXMT, HDR, UI, installer, storage.

**On 30→60.** R118 establishes generation throughput only, and it establishes that
throughput is insufficient at 60 fps source. A 30 fps source would halve the arrival
rate to 33.3 ms and make the current 20.45 ms service comfortably sustainable — but
that is a **30→60 validation**, not native 60→120, and it must be labelled as such
whenever it is run. It is not a fix for this blocker; it is a different product
claim on the same hardware.

---

## Reproducing this round

```sh
cd experiments/metalfx_frame_interpolator_R93/evidence/r118-live-throughput

# Phase 2 — prove the shipped binary carries the R117 estimator
python3 tools/verify_r118_estimator.py --json build-verification.json
#   expects verdict=R117_OPTIMIZED_ESTIMATOR_PRESENT and dylibSha256=ccc0035d…

# Phase 1/2 — launch (Highball route; see §3 for why the standalone script aborts)
#   bottle environment: see bottle-env-r118.json
./run_r118_throughput.py 20 600 20tuples

# Phase 3 — measurement
python3 tools/analyze_r118.py /tmp/fgmetal-r108-unattended/observer.26228.log \
        --json run-20tuples/service.json

# Phase 4 — regression equivalence on a live pair
./phase4_regression.sh ../r108-unattended/run-08-r118-throughput 1895 1896
```

Read, in this order:

1. `R118_DECOMPOSITION ... EXACT` — the decomposition is self-consistent, so the
   shares in §9.3 are real.
2. `FG_GENERATION_SERVICE (primary) median` against `SOURCE_ARRIVAL median`.
3. `R118_THROUGHPUT_RATIO median_over_median`.
4. `R118_BACKLOG ... monotonicGrowth=False` — a rate deficit, not saturation.
5. `R118_PHASE4 live-vs-reference=BIT_IDENTICAL` — the numbers belong to the
   optimised kernel.

### Evidence

- Build verification: `build-verification.json`, `embedded-estimator-r118.metal`
- Display contract: `display-contract.log`
- Authoritative run: `run-20tuples/{controller.log,controller.json,service.json}`
- Corroborating run: `run-01-generation-only/{controller.log,service.json}`
- Phase 4: `phase4-live-pair.log`
- Live artefacts: `../r108-unattended/run-08-r118-throughput/`
- Instrumentation: `../r108-unattended/observer-title-inprocess-r118-throughput.m`
  (4 hunks, +102 / −1 lines against the R117 observer; one statement replaced, the
  rest additive)
- Build: `../r108-unattended/build_r118_tools.sh` →
  `libfgmetal_r118_throughput_probe.dylib` (sha256 `ccc0035d…`)
- Bottle environment: `bottle-env-r118.json`
- Evidence-integrity note: `EVIDENCE-INTEGRITY-NOTE.md`,
  `r115-overwritten-source-raws.txt`
- Observer log: `/tmp/fgmetal-r108-unattended/observer.26228.log`
