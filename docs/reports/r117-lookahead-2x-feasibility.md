# FG-METAL — R117 Look-Ahead / 2× Feasibility Report

| Verdict | Result |
|---|---|
| `HALF_INTERVAL_A_TO_M_TO_B_PACING` | **`NOT VALIDATED` — `HALF_INTERVAL_PRESENTATION_BLOCKED`** (§5) |
| `GAME_RENDERING_DECOUPLED_FROM_FG_DISPLAY` | **`NOT ATTEMPTED`** — gated by §5, per the round's own stop rule |
| `GENERATION_THROUGHPUT >= SOURCE_PAIR_RATE` | **`NOT ESTABLISHED`** — offline estimator 2.8× faster and bit-identical, live budget unresolved (§10.4) |
| `BOUNDED_CONTINUOUS_2X_TITLE_FG` | **`NOT VALIDATED`** — requires 120 Hz; this panel offers 60 Hz in all modes (§16) |

R117 was scoped as the first bounded display-pipeline architecture phase: decouple
game rendering from the display timeline, prove half-interval A→M→B pacing, and get
generation throughput below source arrival.

Two of the three were answered. **The display answer kills the premise on this
host:** the panel runs at 60.000 Hz in every advertised mode, the title already
presents on every refresh, and a 13.33 ms change in a requested presentation offset
moves the realised presentation time by **0.6 µs**. There is no display slot for an
INTERNAL frame, and no scheduling API can create one.

The throughput answer is a genuine win but not a closed one: the estimator was
2.8× slower than it needed to be for a mechanical reason, and is now **6.06 ms**
bit-identically. Whether that lands under the live budget depends on a contention
model this round could not measure (§10.4).

---

## 1. WorkBuddy / model summary

- Harness: WorkBuddy AI. Orchestrator: DeepSeek V4.1 Flash (High) — measurement
  design, experiment implementation, estimator optimisation, evidence analysis,
  this report.
- No other model family was used and no fallback occurred.
- **One independent reviewer audited this round's evidence before publication, and
  it found four blockers in claims the orchestrator had already written down as
  facts.** §4.3 and §10.4 are materially different because of it; §19 records what
  it caught and what was rejected. The first version of the sub-refresh experiment
  measured the probe's own pacing, not the compositor, and its table did not match
  its own log. Both are corrected here.
- No live game process was run this round (§17). Every number comes from a
  reproducible tool or the R116 live log, except where explicitly labelled a
  projection.

## 2. R116 baseline (frozen reference)

| Claim | Status |
|---|---|
| `ASYNC_GPU_FAST_GATE` | `VALIDATED` — 31.28 ms → 0.128 ms CPU, +0.71 ms GPU |
| `NONBLOCKING_M_THEN_B_SCHEDULING` | `VALIDATED` — 51/51 ordered, 0 coalesced |
| `COMMITTED_B_NO_PRESENT_RECOVERY` | `BOUNDED` — 4211 ms → 190.8 ms |
| `REARMABLE_REAL_TITLE_A_TO_M_TO_B` | `VALIDATED (CONSTANT_DEPTH_PROTOTYPE)` |
| `BOUNDED_CONTINUOUS_2X_TITLE_FG` | `NOT VALIDATED` — hold 34.9 ms vs 17.1 ms interval |

R116 named the next blocker as "M is produced inside B's hold", and predicted a
look-ahead buffer would help **only if** generation throughput also came under the
source interval. R117 tests both halves independently, as instructed.

## 3. Display refresh / source cadence measurement

### 3.1 Hardware, measured not assumed

`r117_display_cadence info`:

```
R117_DISPLAY cgRefreshHz=60.0000 width=1470 height=956 pixelWidth=2940 pixelHeight=1912
R117_DISPLAY nsScreenMaxFPS=60 backingScale=2.00
R117_DISPLAY_MODE_COUNT count=5
R117_DISPLAY_MODE index=0 refreshHz=60.0000 periodMs=16.6667 ...
R117_DISPLAY_MODE index=4 refreshHz=60.0000 periodMs=16.6667 w=2560 h=1664
R117_DISPLAY cvDisplayLinkNominalHz=60.0024 nominalPeriodMs=16.6660
```

Three independent APIs agree, and **all five advertised display modes are 60 Hz**.
There is no 120 Hz mode to switch to.

| Quantity | Value | Source |
|---|---|---|
| `DISPLAY_REFRESH_PERIOD` | **16.6667 ms** (60.000 Hz) | CGDisplayMode, NSScreen, CVDisplayLink |
| `SOURCE_PRESENT_PERIOD` | **16.6665 ms** median (60.001 Hz) | 4585 in-game presented intervals |

### 3.2 Source cadence, from presented-time evidence

`analyze_r117_cadence.py` over the R116 final live log (pid 4811, 4586 SOURCE
presentations, 51 INTERNAL):

| Metric | Value |
|---|---|
| SOURCE presented interval, median | **16.6665 ms** |
| Modal presented delta | 16.666 ms (3160 of 4585) |
| Quantisation residual — deviation from an integer multiple of the quantum | median 0.0005 ms, **p99 0.0023 ms**, max 2.46 ms |
| Intervals exactly 1 refresh apart | **4021 / 4585 (87.7 %)** |
| Intervals with **0 free refreshes** | **4021 (87.7 %)** — the same 4021 |
| Intervals with 1 free refresh | 424 (9.2 %) |
| A→M presented delta, median | 33.33 ms (2 refreshes) |
| M→B presented delta, median | 16.666 ms (1 refresh) |

The quantisation residual is the load-bearing statistic: every presented delta in
the log is an integer multiple of 16.6667 ms to within 2.3 µs at p99. That is a
60 Hz compositor and nothing else. (The 2.46 ms maximum is a single outlier across
4585 samples, not a second mode.)

### 3.3 What this means before any experiment

`SOURCE_PRESENT_PERIOD / DISPLAY_REFRESH_PERIOD = 1.000`. The source already uses
**every** display refresh. A 2× display stream needs 120 refreshes per second.

Two caveats, stated because they matter:

- **The median is not the delivered rate.** The same run delivered 38.5 fps
  overall (mean interval 66.85 ms) because the R116 pipeline was stalling. 87.7 %
  is an interval statistic over the run, not a sustained claim. It is the right
  statistic for "is there a free refresh between two consecutive source frames",
  which is the question.
- The current pipeline's displayed pattern is A at *t*, M at *t+2q*, B at *t+3q* —
  M is **two** refreshes after A, not half of one.

## 4. Half-interval scheduling experiment

### 4.1 Design

`r117_half_interval` reproduces the real acquisition order validated in R116
(acquire A and present, acquire B and hold, acquire M and present, then present B)
on a real `CAMetalLayer` in a real window, varying **only** the presentation call.
Evidence is `presentedTime` read through `addPresentedHandler:`; dispatch order is
never treated as evidence.

### 4.2 Minimum-duration test — and what it does and does not prove

| Mode | n | A→M median | **M→B median** | A→B median |
|---|---:|---:|---:|---:|
| `baseline` — M and B use `presentAfterMinimumDuration:1/60` | 11 | 16.6665 ms | **16.6665 ms** | 33.333 ms |
| `minduration-half` — M and B use `presentAfterMinimumDuration:1/120` | 11 | 16.6664 ms | **16.6665 ms** | 33.333 ms |

The meaningful column is **M→B**, and it is unchanged to 0.1 µs when the requested
minimum duration is halved. M and B are 16.6665 ms apart whether you ask for
16.667 ms or 8.333 ms between them.

**A→M in this table proves nothing and is not used as evidence.** A is presented
with `presentAfterMinimumDuration:q` in both modes, so A occupies a full refresh by
construction and A→M cannot be less than one refresh regardless of M's request. The
independent reviewer flagged this; the row is reported for completeness, not as
support.

### 4.3 Sub-refresh scheduling sweep — the decisive experiment

**The first version of this experiment was wrong and is retained only as a
corrective record.** `r117_schedule_granularity` recomputed its anchor with
`ceil((anchor+q)/q)*q` and slept 25 ms between samples, so every requested time was
already in the past when the present was issued and every frame resolved to "the
next refresh after now" regardless of offset. Its flat output was the probe's
pacing, not the compositor's behaviour. Its realised offsets also drifted 0.72 →
1.72 → 2.72 refreshes across passes, contradicting the "constant to ±0.0003" claim
in the first draft of this report. That claim is withdrawn.

`r117_subrefresh_probe` removes the confound. For each offset it computes a base on
a refresh boundary **two refreshes in the future**, presents A at that base,
immediately presents M at `base + offset*q` with nothing sleeping in between, and
reads both presented times. Both targets are in the future when issued.

Result (`subrefresh-sweep.log`, 27 usable samples, 3 passes, direction alternating):

| Requested M offset | 1.667 ms | 3.333 | 5.000 | 6.667 | 8.333 | 10.000 | 11.667 | 13.333 | 15.000 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **Realised A→M** | 16.6665 | 16.6667 | 16.6664 | 16.6665 | 16.6665 | 16.6665 | 16.6662 | 16.6665 | 16.6666 |

```
R117_SUB_VERDICT aToMminMs=16.6661 aToMmaxMs=16.6668 aToMspreadMs=0.0006
                 requestedOffsetRangeMs=15.0000 subRefreshRepresentable=0
```

A **13.33 ms** change in the requested offset produces **0.6 µs** of change in the
realised spacing. The sub-refresh component of a presentation time has no effect
whatsoever on this display.

One incidental observation worth recording: targeting a presentation time at
exactly a refresh boundary (offset 0.000) was dropped in 3 of 3 attempts, while
every non-boundary offset succeeded. `presentAtTime:` on a boundary appears to race
the compositor's own scheduling.

### 4.4 Absolute-time scheduling is fragile

Separately, in isolation: `presentAtTime:` with a 200 ms lead yields
`presentedTime = 0` for every frame — the drawable is **dropped**, not delayed.
With a 50 ms lead it is honoured but quantised to a refresh boundary (error
0.5–10 ms). A mechanism that silently discards frames whose target is too far
ahead is not a usable foundation for a look-ahead queue.

## 5. Half-interval verdict

### `HALF_INTERVAL_A_TO_M_TO_B_PACING = NOT VALIDATED`

### `HALF_INTERVAL_PRESENTATION_BLOCKED`

| Required condition | Measured |
|---|---|
| M at ≈ T/2 after A | **16.666 ms** where T/2 = 8.333 ms — 2.0× the target |
| B at ≈ the other half | 16.6665 ms — correct only because both halves are one refresh |
| M and B distinct | 11/11 in `baseline`, 11/11 in `minduration-half` |
| No reversal | 0 reversals in those 22 trials |

Coalescing: 0 in the 22 usable trials above. One coalescence **was** observed in
`attime-half` (`a = m = 113444.211888`), and the first draft's "0 coalesced in 22
trials" silently excluded it. It is not excluded here: absolute-time scheduling
produced both drops and one coalescence, which is a further reason it is unsuitable.

**Root cause:** the SOURCE already presents on every display refresh (87.7 % of
intervals have zero free refreshes). A half-interval requires a display cadence of
2× the source cadence. `DISPLAY_REFRESH_PERIOD = 16.6667 ms`,
`SOURCE_PRESENT_PERIOD = 16.6665 ms`, ratio 1.000, required display rate
**120.001 Hz**, available **60.000 Hz**.

Per the round's own rule — *"IF HALF-INTERVAL DISPLAY TIMING FAILS: do NOT continue
building the full look-ahead loop"* — Phases 3, 4, 7, 8 and 9 were **not built**.
§6 records the design so the work is not lost, but no queue, buffer or scheduler
was added to the product.

### 5.1 What is *not* blocked

This is a property of **this display at this title's frame rate**, not of the
architecture. On the same panel with a 30 fps source, T = 33.33 ms, T/2 = 16.67 ms
= exactly one refresh, and A→M→B fits at one refresh each — the classic 30→60 case.
The design is sound; the deployment target has no headroom.

## 6. Bounded look-ahead design (specified, not built)

Recorded so the next round does not re-derive it. Gated out by §5.

- **Capacity 3 pair records.** Justification: one source interval of display delay
  plus one generation slot, with headroom for a single generation overrun. Nothing
  larger is justifiable — a deeper buffer converts a throughput failure into
  growing latency, which §16 rules out.
- **Ownership:** SOURCE content is blitted into FG-owned private textures at
  capture time; the game's drawable is released immediately. R116 measured
  `nextDrawable` back-pressure of 13.9–14.4 ms when a third drawable was queued, so
  retaining game drawables across a display delay is unacceptable.
- **Overflow policy:** skip interpolation for the pair, present SOURCE-only. Never
  drop or duplicate a SOURCE frame; never delay a SOURCE presentation to protect an
  INTERNAL one.
- **Accounting:** unchanged — `AppFrameId` SOURCE-only, `InternalPresentId`
  INTERNAL-only, `WsiPresentId` unique, exactly-once terminal state.

## 7. Game-thread decoupling evidence

**Not measured — not attempted.** Phase 4's criterion is only meaningful if M has
somewhere to go. On a display with zero free refreshes, decoupling the timeline
would let the game render ahead while the display stream stayed at 60 fps —
producing *lower* delivered frame rate than passthrough, which is exactly the R116
failure mode (38.5 fps delivered vs 58.5 fps with FG off) plus extra latency.

R116's arithmetic bounds this: parity with interpolation off needs hold ≤ 17.1 ms;
2× needs hold ≈ 0. A look-ahead buffer does not reduce hold, it moves it.

## 8. Buffer / queue policy

Not implemented (§5, §7). No queue exists in the shipped library, so there is no
overflow behaviour to report and no backlog risk to measure.

## 9. Generation throughput profile

### 9.1 Why the live estimator is 24.5 ms when R110 published 10.4 ms

**(a) Resolution.** R110's 10.4 ms was measured at 1280×720. The title runs at
1470×956 — 1.52× the pixels. The unchanged R110 shader on real in-world frames:

| Resolution | Estimator composite median |
|---|---:|
| 1280×720 (R110 published) | 10.62 ms |
| **1470×956 (title resolution)** | **16.98 ms** |

**(b) GPU contention with the game.** 16.98 ms isolated → 24.50 ms live median
(n=51 accepted tuples). The estimator shares the GPU with the game's rendering;
the ratio is 1.43×.

### 9.2 Where the 16.98 ms goes

`r117_estimator_stages` encodes each kernel in its own command buffer so
`GPUEndTime - GPUStartTime` attributes cost to a single stage. At 1470×956:

| Stage | Isolated median | Share |
|---|---:|---:|
| downsample | 0.5006 ms | 2.9 % |
| coarse match | 3.2756 ms | 19.1 % |
| **fine refine** | **13.3562 ms** | **77.8 %** |
| propagate | 0.0030 ms | 0.02 % |
| upsample | 0.0442 ms | 0.3 % |
| sum of stages | 17.1798 ms | |
| **composite (as the product runs it)** | **16.9803 ms** | |

The composite is *below* the sum of isolated stages, so there is no hidden
serialisation.

**Refine is the entire problem.** It evaluates 81 candidate shifts per cell × 888
cells, each candidate re-running a 169-sample patch cost at **full resolution** —
12.2 M full-resolution sample evaluations, 24.3 M texture reads, in 13.36 ms.

### 9.3 Live service time against arrival

| Component | Live median (n=51) |
|---|---:|
| Estimator | 24.50 ms |
| MetalFX transition | 5.07 ms |
| Gate (CPU) | 0.128 ms |
| **Generation service** | **29.69 ms** |
| **Source pair arrival** | **16.666 ms** |
| **Ratio** | **1.78×** |

A 1.78× service-to-arrival ratio is a guaranteed monotonic backlog at any buffer
depth. This is the `GENERATION_THROUGHPUT_BELOW_SOURCE_RATE` condition, and it was
true **before** any display consideration.

## 10. Estimator throughput changes

### 10.1 The observation

`r110FinePatchCost` re-reads 169 positions of `current` **for every one of the 81
candidate shifts**. Those 169 positions are identical across all candidates — only
the `previous` sample moves with the shift. R110 therefore performs 81× redundant
work on the `current` side: 13,689 reads where 169 suffice, and 13,689
recomputations of `luma(a)` and `length(a - targetColor)` where 169 suffice.

### 10.2 The change

Invert the loops: the **sample** loop is outermost and the candidate loop innermost,
accumulating 81 partial sums and 81 weight sums.

The claim of equivalence is not that these live in registers — 162 floats plus 81
`int2` cannot, and the compiler spills. The claim is that the arithmetic is
unchanged: for any given candidate the samples are still accumulated in the same
order (0…168), with the same weights, the same skip predicate, the same epsilon
tie-break, the same `clamp(shift, ±28)`, and the same first-wins comparison. The
five sub-pixel parabola probes are untouched.

**This is an empirical result, not a structural proof.** The shader is compiled with
`options:nil`, so fast-math is on; a compiler is free to reassociate. What is
established is that on 22 independent real inputs the two versions produce
byte-identical output (§11).

### 10.3 Measured effect

| Stage | Before | After | Speedup |
|---|---:|---:|---:|
| downsample | 0.5006 | 0.4911 | — |
| coarse | 3.2756 | 3.3428 | 0.98× (noise, slightly worse) |
| **refine** | **13.3562** | **2.4627** | **5.42×** |
| propagate | 0.0030 | 0.0031 | — |
| upsample | 0.0442 | 0.0444 | — |
| **composite** | **16.9803** | **6.0595** | **2.80×** |

At 1280×720 the same change takes the deterministic harness from 10.62 → 5.10 ms.
On the R114 accepted pair at 1470×956: 17.12 → 6.05 ms.

### 10.4 Service time against the budget — what is and is not established

**Offline, measured:**

| | Value |
|---|---:|
| Estimator | 6.06 ms |
| MetalFX | 5.07 ms |
| Gate | 0.128 ms |
| **Offline generation service** | **11.26 ms** |
| Source arrival | 16.666 ms |
| Headroom | **1.48×** |

11.26 ms is the number that goes against the 16.666 ms budget. **Not 6.06 ms** —
the first draft of this report framed 6.06 against the budget and implied 2.75×
headroom. That was wrong; 6.06 ms is the estimator alone.

**Live, unresolved.** The contention model decides whether 11.26 ms survives, and
the two plausible models disagree:

| Model | Reasoning | Projected live service |
|---|---|---:|
| Multiplicative | the game slows the estimator proportionally (measured 24.50 / 16.98 = 1.43×) | 6.06 × 1.43 + 5.2 = **13.9 ms** — fits |
| Additive steal | the game takes a fixed 7.5 ms of GPU time regardless of estimator length | 6.06 + 7.5 + 5.2 = **18.8 ms** — does not fit |

Both are consistent with the single live datapoint this round has. **The
projection is therefore unsafe in both directions and is not claimed.** This is the
most important unresolved item in the round: the optimisation is real and large, but
whether it clears the budget in-game is unmeasured.

### 10.5 Status of the change

The optimised kernel is embedded in `observer-title-inprocess-r116-criticalpath.m`
(the shader string inside `initializeR108Pipelines`), the library rebuilds cleanly,
and the embedded text was extracted back out and compiled through the **same runtime
path the product uses** (`newLibraryWithSource`) to prove it is valid and produces
identical output (§11.3). Build: `build_r117_tools.sh` →
`libfgmetal_r117_lookahead_probe.dylib` (sha256 `f9d3bb0c…`). No live process has
loaded it.

## 11. Quality regression evidence

Byte-identical output, not metric similarity — the strongest available evidence.
Where frozen inputs were not on disk, the argument is by construction and is
labelled as such.

### 11.1 Deterministic harness 3→4 (1280×720)

| | Result |
|---|---|
| Flow output | **BIT_IDENTICAL** (`cmp` clean, 3,686,400 bytes) |
| Estimator GPU | 10.62 → 5.10 ms |

Because the flow is byte-identical, the recorded region metrics are preserved
exactly: static background median (0,0) with 404,301/404,301 within 1 px; main-shape
median (−4.00, 0) magnitude 4.00; fast-marker median (−12.5, 0) magnitude 12.5. No
metric moved, because no value moved.

### 11.2 Frozen Elden Ring pair 17796→17797

The source raws for this pair are **not retained on disk** — only the resulting flow
and quality record. It could not be re-run, and this is a gap. Its recorded metrics
(global median magnitude 1.7447 px, warp improvement 0.004677, player region
2.0422 px) are preserved by construction, on the strength of 22 other inputs being
byte-identical. That is an inference, not a measurement, and is stated as one.

### 11.3 R114 displayed pair 2749→2750 (1470×956)

| | Result |
|---|---|
| New flow vs R110 flow | **BIT_IDENTICAL** |
| New flow vs the **accepted, actually-displayed** R114 flow | **BIT_IDENTICAL** |
| Embedded-shader extraction, runtime-compiled, same pair | **BIT_IDENTICAL** to the accepted R114 flow |
| Estimator GPU | 17.12 → 6.05 ms |

The optimised kernel reproduces byte for byte the motion field that the R110 gate
accepted and that was displayed on screen in the real R114 run. The gate sees
identical inputs and reaches an identical decision.

### 11.4 In-world sweep

20 consecutive real pairs from the R115 continuous run (1470×956): **20/20
bit-identical.**

## 12. Live 20-pair run

**Not performed.** Two reasons:

1. §5 blocks the architecture this run existed to validate. A 20-pair run of a
   look-ahead pipeline that was never built would measure nothing.
2. The run requires the operator to physically rotate the in-world camera; no
   synthetic gameplay input is sent by this project.

What a live run *would* now settle is narrow and material: whether live generation
service lands near 13.9 ms or near 18.8 ms (§10.4).

## 13. Queue-depth / backlog analysis

No queue was built, so there is no queue depth to report. The backlog question was
answered arithmetically from live data (§9.3):

```
generation service  29.69 ms/pair
source arrival      16.666 ms/pair
→ backlog growth    13.02 ms per pair, monotonic, at any buffer depth
```

After §10 the offline service is 11.26 ms, i.e. 1.48× faster than arrival — stable
if the contention model is favourable, marginal-to-negative if it is additive. Both
are reported; neither is inflated into a claim.

## 14. Displayed cadence analysis

From the R116 live log, during accepted tuples:

| Transition | Median | In refreshes |
|---|---:|---:|
| A → M | 33.33 ms | **2.0** |
| M → B | 16.666 ms | 1.0 |
| A → B | 50.0 ms | 3.0 |

The intended pattern is A → M at 0.5 and M → B at 0.5. Measured is 2.0 and 1.0. M is
not merely late; it is on the far side of a full extra refresh, because there was no
free slot and the only way to show it was to push B out.

Displayed stream interval overall: median 16.6665 ms, p90 33.33 ms, p99 66.67 ms — a
60 Hz stream with gaps, not a 120 Hz stream. Delivered rate during the R116 run was
38.5 fps against 58.5 fps with interpolation off.

## 15. Accounting / recovery safety

Nothing in the accounting contract was modified, because nothing in the dispatch
path was modified. The only product change is the text of one Metal kernel inside
the estimator; it has no access to `AppFrameId`, `InternalPresentId`,
`WsiPresentId`, the frontier, the latch or the watchdog.

The R116 watchdog (`FGMETAL_R116_WATCHDOG_MS=150`), early-B forwarding, the deadlock
hop and the exactly-once terminal-state machinery are untouched and remain as
validated. Since no look-ahead queue exists, there is no new stale-generation,
overflow or ordering surface.

## 16. Bounded-2× verdict

### `BOUNDED_CONTINUOUS_2X_TITLE_FG = NOT VALIDATED`

Not because of throughput, and not because of scheduling, but because **the display
cannot show 120 frames per second**.

| Quantity | Value |
|---|---:|
| Display refresh period | 16.6667 ms (60.000 Hz, all 5 modes) |
| SOURCE present period | 16.6665 ms (60.001 Hz) |
| Free refreshes per source interval | **0** in 87.7 % of intervals |
| Display rate required for 2× | **120.001 Hz** |
| Display rate available | **60.000 Hz** |
| Realised spacing response to a 13.33 ms offset request | **0.6 µs** |

A 2× display stream on a 60 Hz panel fed by a 60 fps source is not a scheduling,
buffering or throughput problem. It is a contradiction. The source already occupies
every refresh; an INTERNAL frame can only appear by displacing a SOURCE frame, which
is substitution, not generation — and substitution lowers delivered rate, which is
precisely the R116 result (38.5 fps delivered vs 58.5 fps with FG off).

The claim is bounded to what was measured: this display, this title's frame rate,
this platform's presentation APIs. It is not a claim that FG-Metal cannot do 2×.

### 16.1 What R117 did establish

- The estimator is **2.80× faster** (16.98 → 6.06 ms) at the title's real
  resolution, **bit-identically** on 22 inputs including the accepted displayed R114
  flow.
- Half-interval presentation is **unrepresentable** on this display: a 13.33 ms
  offset request moves the realised presentation time by 0.6 µs.
- The look-ahead architecture is **specified** (§6) and remains the right next step
  on hardware with a free display slot.

## 17. Exact next PRODUCT blocker

> **The title already presents on every display refresh, so there is no display slot
> for an INTERNAL frame. 2× requires 2× the source cadence; this panel offers 1.0×.**

| Fact | Value | Source |
|---|---|---|
| Display refresh | 60.000 Hz, all 5 modes | CGDisplayMode / NSScreen / CVDisplayLink |
| Source present rate | 60.001 Hz median | 4585 in-game presented intervals |
| Intervals with zero free refreshes | 87.7 % | same |
| Presented-delta quantisation residual | p99 2.3 µs | same |
| 13.33 ms offset request → realised change | 0.6 µs | `r117_subrefresh_probe`, n=27 |
| 8.333 ms minimum duration → realised spacing | 16.666 ms | `minduration-half`, n=11 |

Three ways to resolve it, only one of them software:

1. **A 120 Hz display.** Not available here — every advertised mode is 60 Hz.
2. **A title running at ≤ half the refresh rate.** At 30 fps on this panel,
   T/2 = 16.67 ms = exactly one refresh and the design works unchanged. This is the
   realistic near-term target.
3. **A compositing path that can present sub-refresh.** Not offered by CAMetalLayer
   here: `presentAtTime:` either quantises to a boundary or drops the drawable.

**The next step is therefore not more R117.** It is two things, in order:

1. **One operator-driven live run** to settle §10.4 — whether live generation
   service is ~13.9 ms or ~18.8 ms. This is the only open software question.
2. **Re-target the 2× claim at a workload where the display is not already
   saturated**, because on this host, at this title's frame rate, no amount of
   FG-Metal engineering can produce a second frame per display refresh.

## 18. Reproducing this round

```sh
cd experiments/metalfx_frame_interpolator_R93/evidence/r117-lookahead

# Phase 1 — display contract and source cadence
tools/r117_display_cadence info
python3 tools/analyze_r117_cadence.py /tmp/fgmetal-r108-unattended/observer.4811.log \
        --json run-01-cadence-r116.json

# Phase 2 — half-interval scheduling
tools/r117_half_interval baseline 12
tools/r117_half_interval minduration-half 12       # asks 8.333 ms, gets 16.666
tools/r117_subrefresh_probe                        # the decisive experiment

# Phase 5 — estimator profile and the optimisation
R=../r108-unattended/run-07-r115-continuous-tuple-lifecycle
tools/r117_estimator_stages $R/source-sequence-2314.raw $R/source-sequence-2315.raw \
        1470 956 ../r110-motion-redesign/r110-coarse-to-fine-balanced.metal
tools/r117_estimator_stages $R/source-sequence-2314.raw $R/source-sequence-2315.raw \
        1470 956 r117-coarse-to-fine-sampled.metal      # 16.98 -> 6.06 ms

# bit-identity, the actual quality gate
B=../r110-motion-redesign/r110_coarse_to_fine_bench
$B $R/source-sequence-2519.raw $R/source-sequence-2520.raw 1470 956 \
   ../r110-motion-redesign/r110-coarse-to-fine-balanced.metal /tmp/ref.rg16f
$B $R/source-sequence-2519.raw $R/source-sequence-2520.raw 1470 956 \
   r117-coarse-to-fine-sampled.metal /tmp/new.rg16f
cmp /tmp/ref.rg16f /tmp/new.rg16f          # silent == identical

# build the library that carries the optimised kernel
cd ../r108-unattended && ./build_r117_tools.sh
```

Read, in this order:

1. `R117_SUB_VERDICT ... subRefreshRepresentable=0` — the display blocker.
2. `R117_DISPLAY_MODE` lines — *why* it is blocked.
3. `R117_EST_COMPOSITE medianGpuMs` before/after — the throughput win.
4. `cmp` on the R114 pair against `motion-pair-2749-2750.rg16f` — the quality win.

### Evidence

- Display contract: `r117_display_cadence info` output, §3.1
- Cadence analysis: `run-01-cadence-r116.json`
- Half-interval: `half-interval/{baseline,minduration-half,attime-half,attime-same}.log`
- Sub-refresh: `subrefresh-sweep.log` (decisive); `granularity-sweep.log` (confounded,
  retained as a corrective record — see §4.3)
- Estimator profile: `estimator-stages-1470x956.log`,
  `estimator-stages-1470x956-optimized.log`
- Optimised kernel: `r117-coarse-to-fine-sampled.metal`
- Embedded kernel extracted from the built observer: `embedded-refine-r117.metal`
- Implementation: `../r108-unattended/observer-title-inprocess-r116-criticalpath.m`
- Build: `../r108-unattended/build_r117_tools.sh` →
  `libfgmetal_r117_lookahead_probe.dylib` (sha256 `f9d3bb0c…`)
- Prior observer backed up at `/tmp/observer-r116-backup.m`

## 19. Independent review

One reviewer audited the evidence before publication. It found four blockers, all in
claims the orchestrator had already written as facts:

1. **The §4.3 table did not match its own log** — realised offsets were transcribed
   as 1.72 when the log said 0.72, and two of three passes were omitted. The
   "constant to ±0.0003" claim was false.
2. **That experiment measured the probe, not the compositor** — a `ceil()` anchor and
   a 25 ms inter-sample sleep put every requested time in the past.
3. **§4.2's A→M row was forced** by A's own minimum duration and tested nothing.
4. **"0 coalesced in 22 trials" excluded an observed coalescence** in `attime-half`.

It also caught four majors: the budget framing (6.06 ms is the estimator, not the
service), the untested multiplicative contention assumption, the "60.001 Hz"
median-vs-rate conflation, and that fast-math makes bit-identity empirical rather
than structural. Minor numeric errors were corrected throughout.

**All findings were accepted and are reflected in this version.** §4.3 was rebuilt
as a new experiment with the confound removed, and it produced a *stronger* result
than the flawed one. §10.4 now reports both contention models instead of the
favourable one. Nothing was rejected as a false positive.

Without the review this report would have published a confounded experiment as a
proof and a 2.75× headroom figure that was really 1.48×.
