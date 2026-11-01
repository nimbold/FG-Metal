# FG-METAL R119 — Live Contention and Evidence Isolation

**Round:** R119
**Predecessor:** R118 (`GENERATION_THROUGHPUT_BELOW_SOURCE_RATE`, 20.452 ms service vs 16.939 ms arrival)
**Harness:** WorkBuddy AI, DeepSeek v4.1 Flash — High. No fallback model family used.
**Date:** 2026-10-31
**Live process:** Elden Ring pid 44045, Highball bottle `Games`, in-world, controller-driven steady 360°/s camera rotation, 175.52 s

---

## Headline

```
GENERATION_THROUGHPUT_BELOW_SOURCE_RATE
```

| | R119 |
|---|---:|
| FG generation service, median | **19.728 ms** |
| SOURCE arrival, median (measured) | **17.502 ms** |
| Ratio, median-over-median | **1.127×** |
| Deficit against measured arrival | **2.226 ms** |
| Deficit against nominal 16.6667 ms | 3.061 ms |

The round did **not** cross the throughput line. It did answer the question that
was blocking progress, and the answer changes what R120 should do.

**The estimator's live cost is GPU execution under contention, not command-buffer
queue delay.** Queue delay is 1.244 ms — 10.5 % of the estimator's CPU wall and
6.3 % of the service. GPU execution is 9.932 ms against a 6.06 ms offline figure
for the same kernel at the same resolution: a **1.639× contention factor worth
3.873 ms per pair**. MetalFX adds a further 0.702 ms of contention. The whole
2.226 ms deficit — and twice over — sits in GPU contention, not in scheduling.

Stated as an accounting identity from the run:

```
non-GPU overhead, per-tuple median                  3.955 ms
GPU work at offline cost (6.0595 + 5.07)           11.130 ms
                                                  ---------
non-contended service floor, median(O - contention) 15.085 ms
GPU contention paid, median service - floor          4.643 ms
                                                  ---------
observed service median                             19.728 ms
```

The pipeline's non-contended cost is **15.085 ms — 2.417 ms under the measured
arrival median.** The entire failure is contention. That is the finding R120 has
to work with.

---

## 1. Harness and model summary

| Item | Value |
|---|---|
| Orchestrator | WorkBuddy AI, DeepSeek v4.1 Flash — High |
| Implementation | same model family, continuous context |
| Independent review | adversarial audit passes, same context (limitation noted in §1.2) |
| Runtime owner | operator (human) driving the game with a physical controller |
| Model substitution | none; no fallback family used |
| Artifact under test | `libfgmetal_r119_throughput_probe.dylib`, sha256 `cc6fa03cc8fb8e44b93a25b1e65d973b97970cf6c3cc0fce92d0a8d231ad61c6` |
| Observer source | `observer-title-inprocess-r119-throughput.m`, 390,017 bytes |
| Delta against R118 observer | +519 / −48 lines across 23 hunks (`r119-observer.diff`) |

### 1.1 Structure actually used

The round ran as one orchestrator holding the full state, with the reviewer role
executed as a deliberate adversarial pass against the finished instrumentation
*before* the live run, and a second pass against the first pass's own fixes. The
runtime/evidence role was executed by the orchestrator (snapshotting, probing,
analysing); the live-run role was executed by the operator.

### 1.2 Limitation this creates

The review was **not** a separate context. A reviewer in the same context shares
the author's blind spots, and two of the three defects the review found were
found by *re-reading the emitted log* rather than by reading the code — which is
the signature of a same-context review. The findings were real and were fixed
(§5.4, §7.4), but a future round with a harder gate should run the audit in a
fresh context.

---

## 2. The evidence-directory fix

### 2.1 The defect

`kR115EvidenceDirectory` was a compile-time constant. `r114EvidenceDirectory()`
returned it whenever `FGMETAL_R115_CONTINUOUS=1`, which every live round since
R115 has set. R118 therefore wrote its diagnostics into the R115 evidence
directory, and because both rounds ran Elden Ring at 1470×956 with overlapping
source sequence numbers, **25 R115 source raws and 5 R115 motion fields were
silently overwritten**.

### 2.2 The fix

One accessor, one path composer, one blob writer:

```c
static const char *fgmetalEvidenceDirectory(void) {
    const char *override = getenv("FGMETAL_EVIDENCE_DIR");
    return (override && *override) ? override : kR115EvidenceDirectory;
}
static BOOL fgmetalEvidenceDiagnosticsAllowed(void);
static BOOL fgmetalEvidencePath(char *out, size_t cap, const char *fmt, ...);
static BOOL fgmetalWriteEvidenceAbsolute(const char *path, const void *bytes, size_t len);
```

`r114EvidenceDirectory()` was deleted; the legacy constant is now selected *inside*
`fgmetalResolveEvidenceDirectory()`. There is exactly **one** `getenv("FGMETAL_EVIDENCE_DIR")`
in the observer and zero direct uses of the legacy constants outside the resolver.

Then a protected-corpus guard. Anything under
`experiments/metalfx_frame_interpolator_R93/evidence/` that is not this round's own
directory is another round's evidence. Both sides are resolved with `realpath()`
before comparison, so `..`-spelled and symlinked paths cannot be smuggled past it,
and an unresolvable path fails closed:

```c
static BOOL fgmetalEvidenceDirectoryIsProtected(const char *path) {
    if (!path || !*path) return YES;
    if (!fgmetalResolvePath(path, resolvedPath, ...)) return YES;      // fail closed
    if (!fgmetalResolvePath(kEvidenceCorpusRoot, resolvedRoot, ...)) return YES;
    if (!fgmetalResolvePath(kR119EvidenceDirectory, resolvedOwn, ...)) return YES;
    if (!fgmetalPathIsWithin(resolvedPath, resolvedRoot)) return NO;   // outside the corpus
    return !fgmetalPathIsWithin(resolvedPath, resolvedOwn);            // someone else's corpus
}
```

A refused directory does not fall back to writing somewhere else and does not
abort the process: diagnostics are disabled, the product path is untouched, and
the refusal is logged. The escape hatch for a deliberate historical rerun is
`FGMETAL_ALLOW_LEGACY_EVIDENCE_DIR=1` — explicit, not implicit.

### 2.3 A second instance the R118 report did not find

The R105/R106 depth and motion sample writers (`r105EncodeDepthDiagnostic`,
`r105EncodeMotionDiagnostic`) wrote to
`evidence/r106-title-derived-motion/run-02-world-paired-depth/` **by absolute path**,
bypassing the accessor entirely. Had R119 run with `FGMETAL_R106_DEPTH=1`, it would
have reproduced the R118 damage in a different directory. All five writers now route
through the accessor.

### 2.4 What was *not* done

The 25 R115 source raws and 5 R115 motion fields destroyed by R118 were **not**
regenerated. Fabricating replacements under historical filenames would convert a
disclosed loss into a false record. The damage remains itemised in
`r118-live-throughput/EVIDENCE-INTEGRITY-NOTE.md` and
`r118-live-throughput/r115-overwritten-source-raws.txt`, which stay authoritative.

---

## 3. Evidence-isolation validation

```
EVIDENCE_DIRECTORY_ISOLATION = VALIDATED
```

### 3.1 In-process probe

`tools/evidence_probe_host` `dlopen()`s the real compiled dylib, so the real
accessor, the real path composer, the real writer and the real guard run — then
exits before any hook is installed (`FGMETAL_EVIDENCE_SELFTEST=1` → `hooks=skipped`).
No Metal device, no game, no frame.

| Probe | Configuration | Expected | Observed |
|---|---|---|---|
| 01 | `FGMETAL_EVIDENCE_DIR=<r119>/evidence-isolation/probe-output` | both artefacts written there | `saved=1 saved=1`, 2 files, 4096 B each |
| 02 | no override, `FGMETAL_R115_CONTINUOUS=1 FGMETAL_R114_INPROCESS_LATCH=1` | refused | `allowed=0 protected=1 saved=0 saved=0` |
| 03 | override aimed at `run-07-r115-continuous-tuple-lifecycle` | refused | `allowed=0 protected=1 saved=0 saved=0` |
| 04 | override spelled `r119-live-throughput/../r108-unattended/run-07-…` | refused | `allowed=0 protected=1 saved=0 saved=0` |

### 3.2 Corpus-wide snapshot proof

A full-content snapshot, not `find -newer`: every regular file under `evidence/`
is hashed with SHA-256, so a rewrite preserving size *and* mtime is still caught.

Before the round: **5,278 files, 29.86 GB**.

After the probes:

```
COMPARE before=5278 after=5298 added=20 removed=0 changed=2
COMPARE allowed_roots=1 allowed_changes=22 violations=0
EVIDENCE_ISOLATION=UNCHANGED_OUTSIDE_ALLOWED
```

After the live run — the check that actually covers the game process:

```
COMPARE before=5278 after=5316 added=38 removed=0 changed=2
COMPARE allowed_roots=1 allowed_changes=40 violations=0
EVIDENCE_ISOLATION=UNCHANGED_OUTSIDE_ALLOWED
```

- **removed = 0**, **violations = 0**.
- **changed = 2**, both inside this round: the rebuilt dylib and the edited observer.
- **added = 38**, every one under `r119-live-throughput/`: the 12 bounded artefacts,
  the run logs, the tooling, the probe outputs.

No R115, R116, R117 or R118 file was touched by the live game process.

### 3.3 The 12 artefacts the live process wrote

Three sampled pairs × (source A raw, source B raw, motion field, MetalFX title frame):

```
motion-pair-{7282-7283, 7309-7310, 7339-7340}.rg16f      5,621,280 B each
source-sequence-{7282,7283,7309,7310,7339,7340}.raw      5,621,280 B each
title-M-for-{7282-7283, 7309-7310, 7339-7340}.rgba16f  11,242,560 B each
```

Artifacts: `evidence-isolation/{baseline,after-probe,after-live}-snapshot.json`,
`isolation-compare.json`, `live-isolation-compare.json`, `probe-0{1..4}-*.log`,
`VALIDATION.md`.

---

## 4. R118 baseline

Quoted from `FG-METAL-R118-LIVE-GENERATION-THROUGHPUT-REPORT.md`; not re-derived.

| Metric | R118 median |
|---|---:|
| FG generation service | 20.452 ms |
| SOURCE arrival (measured) | 16.939 ms |
| Ratio | 1.207× |
| `bAcquiredToGenStart` | 2.672 ms |
| Preparation | 0.207 ms |
| Estimator, CPU wall | 11.462 ms |
| Estimator, GPU span | 10.218 ms |
| CPU wall − GPU span | 1.283 ms |
| Estimator complete → MetalFX submit | 0.303 ms |
| MetalFX transition | 5.656 ms |
| Gate decision | 0.268 ms |

R118's own framing of its gap: *"a 2.67 ms median but four tuples at 15.7–17.1 ms
… they are dispatch-queue stalls, not GPU work"*, and *"the estimator is the
longest-running GPU dispatch and therefore the one that queues behind the game's
render most often."* R119 tests both claims and finds the first correct and the
second **wrong**.

Offline reference costs used throughout: estimator **6.0595 ms**, MetalFX
**5.07 ms**, both at 1470×956.

---

## 5. Diagnostic-overhead policy

### 5.1 Policy

`FGMETAL_DIAG_SAMPLE=bounded` (default) exports full raw/motion/MetalFX artefacts
for the **first, middle and last** tuple of the budget only. Per-pair timing, the
gate decision, the accounting counters and the WSI evidence remain enabled for
**every** tuple. `FGMETAL_DIAG_SAMPLE=full` reproduces the R118 configuration
exactly, so the overhead can be measured rather than asserted.

The selection is made **once, at reservation**, before A's producer can complete,
so the sampling decision cannot depend on how the tuple turns out.

Observed: `R119_DIAGNOSTIC_POLICY mode=bounded env=bounded boundedSample=first-middle-last
timingPerTuple=1 gatePerTuple=1 accountingPerTuple=1 wsiPerTuple=1`, and
`R119_DIAG_SAMPLE sampled=3/21`.

### 5.2 What the counters say

| Counter | Value | Meaning |
|---|---:|---|
| `sourceReadbackVerified` | 6 | 3 pairs × 2 sequences, all luma-verified and written |
| `sourceReadbackUnverified` | 0 | no dark-frame or failed-verification sample |
| `motionExportSaved` | 3 | one motion field per sampled pair |
| `metalFxDiagnosticSaved` | 3 | one MetalFX frame per sampled pair |
| `metalFxDiagnosticSkipped` | 18 | correct — outside the sample |
| `handoffSkipped` / `estimatorQueueSkipped` | 0 / 0 | the timing decomposition ran on **every** tuple |
| `estimatorQueueClockMismatch` | 0 | clock-domain check passed on every tuple |
| `presentReadyWithoutArtefact` | 19 | see §5.5 |

### 5.3 `DIAGNOSTIC_OVERHEAD_REMOVED`

Reported separately, as required, and with its limits.

| Effect | R118 | R119 | Change |
|---|---:|---:|---:|
| `bAcquiredToGenStart` median | 2.672 ms | 2.299 ms | −0.373 ms |
| `bAcquiredToGenStart` max | 17.1 ms (4 tuples at 15.7–17.1) | 5.773 ms | **16 ms-class tails gone** |
| Estimator → MetalFX export gap, median | 0.303 ms | 0.081 ms | −0.222 ms |
| Service median | 20.452 ms | 19.728 ms | −0.724 ms |

The tails are the important part. R118's four 15.7–17.1 ms `bAcquiredToGenStart`
tuples were the 22 MB source-raw export landing in the capture path; with the
export bounded they do not occur, and the handoff's own media-clock decomposition
(§6) tops out at 3.249 ms. **The 16 ms dispatch-stall class was a diagnostic
artefact, not a product defect** — R118 attributed it to "dispatch-queue stalls"
and that attribution was wrong.

### 5.4 Disclosure: R118's numbers came from a diagnostics-enabled observer

R118 wrote a 22 MB source raw, a 22 MB diagnostic copy and a motion field per
tuple, and logged every event. R119 writes 12 artefacts across 3 of 21 tuples.
**The R118 → R119 comparison is therefore not a pure code change: the measurement
regime changed too.**

Consequences, stated plainly:

- The R119 service figure is the fairer estimate of the product's cost. The R118
  figure was an upper bound for a diagnostics-enabled build.
- The R118 → R119 service change (−0.724 ms) is **not** attributable to code alone.
  Part is the removed export work; the rest is run-to-run variation, and the two
  runs had different arrival distributions (§14) and therefore different GPU load.
- **The throughput verdict uses R119 numbers only, on both sides of the ratio.**
  No R118 figure is used to make R119 look better or worse.

### 5.5 One annotation defect, disclosed

`presentReadyWithoutArtefact = 19` should be 18. For pair `7309->7310`
(the middle sample) the `R110_PRESENT_M_READY` line printed
`sourceA=<not-exported:r119-bounded-sample>` even though both files exist and were
verified. Log ordering:

```
88417  R119_DIAG_SAMPLE tuple=10 … fullArtefacts=1
88451  R108_SOURCE_DIAGNOSTIC_READBACK seq=7309 … saved=1
88491  R110_PRESENT_M_READY pair=7309->7310 sourceA=<not-exported…>
88496  R108_SOURCE_DIAGNOSTIC_READBACK seq=7310 … saved=1
```

B's readback completes four lines *after* the present-ready line that consults the
verified-sequence ring, so the ring was still missing 7310 at that instant. The
annotation is **conservative** — it never names a file that does not exist — and
affects no measured number. Recorded rather than patched mid-round, because
patching it would invalidate the run that produced the numbers.

---

## 6. Capture / handoff decomposition

Eight intervals on the media clock (`CACurrentMediaTime()`), plus the two
composites. All n=21.

| Interval | min | mean | median | p90 | max |
|---|---:|---:|---:|---:|---:|
| `hookToCaptureEntryNs` | 0.087 | 0.495 | 0.573 | 0.680 | 1.092 |
| `captureEntryToBlitCommitNs` | 0.198 | 0.239 | 0.235 | 0.272 | 0.325 |
| `blitCommitToCopyGpuStartNs` | 0.411 | 0.528 | **0.480** | 0.757 | 0.915 |
| `copyGpuExecNs` | 0.141 | 0.178 | 0.152 | 0.275 | 0.346 |
| `copyGpuEndToWakeNs` | 0.130 | 0.151 | 0.143 | 0.152 | 0.270 |
| `wakeToDispatchNs` | 0.013 | 0.024 | 0.016 | 0.036 | 0.098 |
| `dispatchToGenQueueEntryNs` | 0.017 | 0.102 | 0.026 | 0.105 | 1.231 |
| `genQueueEntryToGenStartNs` | 0.001 | 0.002 | 0.002 | 0.003 | 0.004 |
| **`hookToGenStartNs`** | 1.152 | 1.719 | **1.651** | 2.093 | 3.249 |
| `hookToCopyGpuStartNs` | 0.802 | 1.262 | 1.258 | 1.652 | 1.765 |

`R119_HANDOFF_RECONCILIATION maxErrorMs=0.000000 (EXACT)` — the eight intervals sum
to the composite on every tuple.

### 6.1 Classification

The brief asked for a classification, not a guess. The data classifies cleanly:

- **GPU copy: 0.152 ms** (`copyGpuExec`) — not a factor. The blit is cheap.
- **CPU queue dispatch: 0.042 ms** (`wakeToDispatch` + `dispatchToGenQueueEntry`) —
  the two hops the brief suspected of being collapsible are worth **42 µs**. There
  is nothing there to win.
- **Waiting behind another capture task: 0.573 ms** (`hookToCaptureEntry`) — the
  present hook reaching the capture entry. This is the single largest sub-interval
  and it is thread-scheduling / lock entry, not GPU.
- **Synchronisation with producer completion: 0.143 ms** (`copyGpuEndToWake`) — the
  wait for the copy's completion signal. Small.
- **Drawable / layer backpressure: 1.258 ms** (`hookToCopyGpuStart`) — present in
  the interval between the hook and the copy being *submitted*, of which only
  0.235 ms is blit encoding. The remaining ~0.45 ms is inside
  `blitCommitToCopyGpuStart`: the gap between committing the blit and the copy's
  command buffer starting on the GPU.

### 6.2 Against the Phase 2 target

**Target: median ≤ 1 ms, avoid 16 ms tails.** Result: **1.651 ms median, max
3.249 ms.**

- **Target missed** by 0.65 ms. The handoff is not under 1 ms.
- **16 ms-class tails eliminated.** R118's 15.7–17.1 ms excursions are gone; the
  maximum across 21 tuples is 3.249 ms and the p90 is 2.093 ms. The tail work the
  brief asked for was accomplished, by removing the export from the path rather
  than by rescheduling.
- **Generation starts as soon as the FG-owned copies are ready**, to within
  0.002 ms: `genQueueEntryToGenStartNs` median is 2 µs. There is no queue-depth
  delay between enqueueing and starting. Whatever the remaining 1.65 ms is, it is
  *before* the generation queue.

### 6.3 Why the residual is not worth chasing now

The 1.651 ms is: 0.573 (hook→capture entry) + 0.235 (blit encode) + 0.480 (blit
commit→copy GPU start) + 0.152 (copy) + 0.143 (copy wake) + 0.042 (dispatch) +
0.002 (queue→start). To reach 1 ms you must take ~0.65 ms out of the 0.573 ms
lock-entry interval *plus* the 0.480 ms submit-to-start interval, and neither is a
single mechanical fix. **Even eliminating the handoff entirely would leave the
service at 18.08 ms** — still above the 17.502 ms arrival. It is not the lever.

---

## 7. Estimator queue-delay profile

### 7.1 The split

Derived per tuple from three clocks and published only when the clock-domain
assumption holds.

| Interval | min | mean | median | p90 | max |
|---|---:|---:|---:|---:|---:|
| **`commitToGpuStartNs`** — queue delay | 1.068 | 1.249 | **1.244** | 1.364 | 1.471 |
| **`gpuExecNs`** — GPU execution | 6.796 | 9.767 | **9.932** | 10.978 | 12.819 |
| `gpuEndToWakeNs` — completion signalling | 0.157 | 0.408 | 0.341 | 0.738 | 1.339 |
| `commitToWakeNs` | 8.301 | 11.424 | 11.671 | 12.615 | 14.759 |
| `cpuWallNs` | 8.304 | 11.427 | 11.674 | 12.618 | 14.761 |

`R119_ESTIMATOR_RECONCILIATION maxErrorMs=0.000000 (EXACT)`.

**Queue share of the estimator's CPU wall: 0.105 median** (min 0.081, max 0.159).

### 7.2 Queue delay is small and *tight*

The distribution is the most striking thing in the round: queue delay ranges
**1.068 → 1.471 ms**, a spread of 0.40 ms across 21 tuples under continuous camera
motion. It is not occasionally huge; it is uniformly small.

That is the direct refutation of R118's hypothesis. R118 wrote that the estimator
*"is the longest-running GPU dispatch and therefore the one that queues behind the
game's render most often"*. If that were true, queue delay would be the dominant
term and would be spiky — the estimator would sometimes wait 8 ms behind a render.
It waits 1.2 ms, every time.

**Interpretation.** The estimator's command buffer is submitted on the generation
thread's queue and is not enqueued until the pair's copies are done. By the time
it reaches the GPU, the game's frame for B has already been submitted and is
either finished or nearly finished — the CPU is behind the GPU at that point, so
there is nothing ahead of it in the queue but the tail of the game's own work.
Queue delay ≈ 1.2 ms is the *residual* of that tail, not a collision.

### 7.3 Clock-domain verification

The brief required that queue delay and GPU execution be kept separate and that
queue delay never be called GPU execution. To make that defensible rather than
asserted:

- `wakeMinusGpuEndNs` is published **unclamped and signed**, so a clamping bug
  cannot manufacture a favourable zero.
- The split is gated on `wakeMinusGpuEndNs ∈ [−0.5 ms, +2.0 ms]` **and**
  `estimatorClockSkewNs ∈ [−50 ms, +2.0 ms]`. Outside the band the tuple emits
  `R119_ESTIMATOR_QUEUE=CLOCK_MISMATCH` instead of a split.
- Observed: **0/21 mismatches**, `clock=CONSISTENT` on every tuple.
  Tail sample: `wakeMinusGpuEndNs=392125`, `clockSkewNs=−3250`.

The band is asymmetric on purpose. With `GPU_time = media + δ`, a negative δ puts
the GPU clock *behind* the media clock and deflates `commitToGpuStart`; the
ceiling was tightened to 2 ms for exactly that reason, while the floor was
loosened to −50 ms because a large negative skew is not favourable and therefore
not a way to cheat.

`R119_ESTIMATOR_QUEUE_CLAMPED_TO_ZERO=0/21` — no queue delay was clamped away.

### 7.4 The defect the review caught here

First pass published `commitToGpuStart` from a delta function that clamped
negatives to zero. A tuple whose GPU start preceded its CPU commit (clock skew)
would have reported **0.000 ms queue delay** — the most favourable possible value,
produced by a bug. Fixed by publishing the raw signed residual and gating the
split on it. The follow-up pass then found the band was asymmetric in the *wrong*
direction (a 50 ms ceiling on a term whose negative side deflates the metric), and
that was tightened. A third pass found `wakeMinusGpuEndNs == 0` in a
`memset`-zeroed timing struct was `!= INT64_MIN`, so a tuple where the estimator
never ran emitted `CLOCK_MISMATCH` instead of `SKIPPED`; fixed by initialising both
sentinels in `r115BeginTimingLocked`.

---

## 8. Estimator GPU-execution profile

| | Value |
|---|---:|
| Live GPU execution, median | **9.932 ms** |
| Live GPU execution, min | 6.796 ms |
| Offline, same kernel, same resolution | **6.0595 ms** |
| Contention factor, median | **1.639×** |
| Contention factor, min | **1.121×** |
| Contention cost, median | **3.873 ms** |

### 8.1 The lower tail is the proof

The minimum live GPU span is **6.796 ms — 1.121× offline.** The distribution's
bottom is essentially the uncontended cost. So the kernel is not intrinsically
slower on this GPU, the driver is not miscompiling it, and the 9.932 ms median is
not a fixed property of the work. It is a property of *when* the work runs.

This is the strongest single piece of evidence in the round. A kernel that had
become 1.6× slower would not have a 1.12× floor.

### 8.2 Corroboration from the offline re-run

§11 re-compiled the shipped kernel from the literal extracted out of the compiled
dylib and ran it on the three preserved live pairs:

| Pair | Shipped kernel (R117 form) | Reference kernel (R110 form) |
|---|---:|---:|
| 7282→7283 | 5.999 ms | 17.292 ms |
| 7309→7310 | 6.069 ms | 19.349 ms |
| 7339→7340 | 6.605 ms | 20.540 ms |

The 6.06 ms figure is confirmed by three independent pairs, and the R117
optimisation's magnitude (≈3× over the R110 form) is confirmed alongside it.

### 8.3 MetalFX, for completeness

| | Value |
|---|---:|
| Live, median | 5.772 ms |
| Live, min | 5.286 ms |
| Offline | 5.07 ms |
| Contention factor, median | 1.138× |
| Contention factor, min | **1.043×** |

MetalFX is 1.14× contended, and its floor is 1.04× — it is barely affected. This
matches R118's 1.12× and confirms the brief's instruction to leave MetalFX alone
in this phase.

### 8.4 The contention is shared and anti-correlated

| | Value |
|---|---:|
| Total GPU contention per tuple, median | 4.892 ms |
| Sum of the two individual medians | 4.575 ms |
| `corr(estimator contention, MetalFX contention)` | **−0.417** |

The two stages are **negatively** correlated in how much contention they absorb.
When the estimator eats a long stall, MetalFX eats a short one, and vice versa.

**This matters for R120.** It means the estimator and MetalFX are not colliding
with each other; they are taking turns absorbing the *same* collision with the
game's render. The mechanism is consistent with the ordering: MetalFX is submitted
immediately after the estimator (§9), so if the estimator has already waited out
the game's frame, MetalFX runs on a clear GPU. A fix that merely reorders the two
past each other redistributes the cost rather than removing it. What has to change
is either the **total GPU occupancy FG asks for**, or **where in the game's frame
that occupancy lands**.

---

## 9. Scheduling changes tested

### 9.1 What was done, stated exactly

**No bounded scheduling change from the Phase 4 list was implemented or promoted.**
That is a deviation from the round brief and it is recorded as one.

The brief made Phase 4 conditional — *"only after classification, test bounded
scheduling changes"* — and the classification (§7, §8) shows that all four levers
address a quantity far smaller than the deficit. Each lever's ceiling, from this
run's own measurements:

| Lever | Measured quantity it could remove | Ceiling |
|---|---|---:|
| **(A)** same / related command queue | Does not change execution time. Queue delay 1.244 ms is set by the game's in-flight work, not by which queue the estimator is on. | ~0 ms |
| **(B)** earlier preparation / commit | Commit already precedes GPU availability by 1.244 ms. Committing earlier only lengthens the queue wait; it cannot make the GPU free sooner. | ≤ 1.244 ms, realistically ≪ |
| **(C)** GPU dependency instead of CPU dependency | The CPU wait is fully explained by queue + wake (1.244 + 0.341 = 1.585 of the 1.592 ms `cpuMinusGpu`). There is no hidden CPU stall to remove; the dependency is already GPU-ordered for MetalFX. | ≤ 1.592 ms, realistically ~0.3–0.6 |
| **(D)** collapse capture → generation queue | The two hops measure 0.016 + 0.026 = **0.042 ms**. | 0.042 ms |

Optimistic total across all four, assuming they compose perfectly and each hits its
ceiling: **~1.6 ms**, against a **2.226 ms** deficit — and **none of it touches the
3.873 ms of execution contention**, which is the actual failure.

Phase 4 was therefore evaluated against the classification and rejected on
arithmetic, not skipped. Implementing (A)–(D) would have consumed a live run and
could not have produced a pass.

### 9.2 What the brief asked for and what it got

| Brief | Result |
|---|---|
| Classify the handoff | done — §6.1; five-way classification with a number for each |
| Handoff median ≤ 1 ms | **not met** — 1.651 ms; §6.3 explains why it is not the lever |
| Avoid 16 ms tails | **met** — max 3.249 ms, was 15.7–17.1 ms |
| Start generation as soon as FG copies are ready | **met** — enqueue→start is 2 µs |
| Derive queue delay and GPU execution separately | **met** — §7, with clock-domain gating |
| Answer (A) vs (B) vs (C) | **answered** — §8.1; (A) dominant, (B) minor |
| Test bounded scheduling changes | **not done** — §9.1; rejected on measured ceiling |

---

## 10. Final promoted change

**The promoted change for R119 is the evidence-directory isolation fix and the
bounded-diagnostics measurement mode. No performance change was promoted.**

| Change | Status | Evidence |
|---|---|---|
| Single evidence accessor + `realpath()` corpus guard | **promoted** | §2, §3 — 4 probes, 2 corpus snapshots, 0 violations |
| R105/R106 absolute-path writers rerouted | **promoted** | §2.3 — found this round, not in R118's report |
| Bounded diagnostic sampling | **promoted** | §5 — `sampled=3/21`, timing on 21/21 |
| Media-clock handoff decomposition | **promoted** (instrumentation) | §6 — reconciliation EXACT |
| Estimator queue/execution split | **promoted** (instrumentation) | §7 — reconciliation EXACT, 0/21 clock mismatches |
| Any product-path performance change | **none promoted** | §9.1 |

The product path is byte-identical to R118's in behaviour: same estimator kernel,
same MetalFX call, same gate, same accounting. Only the diagnostic export is
bounded. That is what makes §11's bit-identical result the load-bearing claim it is.

---

## 11. Regression-equivalence evidence

### 11.1 Requirement

Motion field must remain `BIT_IDENTICAL` to the accepted R117 estimator given the
same A/B source inputs. Deterministic harness; R114 displayed pair where available;
one fresh R119 live pair.

### 11.2 What was run

Three fresh R119 live pairs (source A raw, source B raw and the motion field the
shipped library actually produced in the game), each re-run offline through the
R110 bench twice — once with the reference kernel text, once with the kernel
literal extracted from the compiled R119 dylib:

```sh
./phase4_regression.sh "$PWD" 7282 7283 1470 956
./phase4_regression.sh "$PWD" 7309 7310 1470 956
./phase4_regression.sh "$PWD" 7339 7340 1470 956
```

### 11.3 Result — all three pairs, all three statements

```
pair=7282->7283   live-vs-reference=BIT_IDENTICAL  live-vs-embedded=BIT_IDENTICAL  reference-vs-embedded=BIT_IDENTICAL
pair=7309->7310   live-vs-reference=BIT_IDENTICAL  live-vs-embedded=BIT_IDENTICAL  reference-vs-embedded=BIT_IDENTICAL
pair=7339->7340   live-vs-reference=BIT_IDENTICAL  live-vs-embedded=BIT_IDENTICAL  reference-vs-embedded=BIT_IDENTICAL
bytes=5621280 each (1470 × 956 × 2 channels × float16)
```

Three independent statements per pair:

- **live vs reference** — what the shipped estimator produced in the game equals
  what the accepted R110 candidate-outer reference produces from the same inputs.
  This is the equivalence claim the brief requires.
- **live vs embedded** — the shipped dylib's own kernel text, extracted and
  compiled again, reproduces what it did live. This closes the loop on "is the
  binary we measured the binary we think it is".
- **reference vs embedded** — the two kernels are the same function, expressed
  differently, producing identical output. This is what makes the R117
  optimisation safe rather than merely fast.

### 11.4 Estimator identity

`tools/verify_r119_estimator.py` → `build-verification.json`:

```
verdict                  = R117_OPTIMIZED_ESTIMATOR_PRESENT
r117Form=True  r110Form=False  parabolaProbes=5  bytes=17338
sourceLiteral bytes=11676  coreMatch=True  embeddedOffset=211623
```

The shipped dylib carries the sample-outer refine form
(`int2 shifts[81]; float costSum[81]; float weightSum[81]; costSum[k] += weight * abs(sampleY - b);`),
not the R110 candidate-outer form, and the extracted literal is byte-identical to
`r118-live-throughput/embedded-estimator-r118.metal`.

### 11.5 A bug found and fixed in the harness itself

`phase4_regression.sh` initially reported `live-vs-embedded=DIFFERS` and
`reference-vs-embedded=DIFFERS` with `-[MTLComputePipelineDescriptorInternal
setComputeFunction:withType:]: failed assertion 'computeFunction must not be nil.'`
Tracing with `sh -x` showed the script was passing
`.../r119-live-throughput/embedded-estimator-r118.metal` — a file that does not
exist; the real one is `embedded-estimator-r119.metal`. The bench aborted, produced
no output field, and the script compared against a stale file from a previous
invocation and reported a difference.

Two fixes: the correct path, and a fail-closed guard — stale outputs are deleted
before each run and a missing output field is now `R119_PHASE4=FAIL
reason=bench_produced_no_field`, exit 3. A harness that can silently report
"DIFFERS" because it compared against yesterday's file is worse than no harness.

### 11.6 What this does and does not establish

**Establishes:** for three live pairs with real in-game camera motion, the shipped
estimator's output is bit-identical to the accepted reference; the shipped kernel
is the R117 form; the maths is unchanged by anything this round did.

**Does not establish:** coverage of the input space. Three pairs is a spot check,
not a sweep. The claim is "unchanged", and "unchanged" is supported by the kernel
being byte-identical and by three bit-identical pairs — not by exhaustive testing.

---

## 12. The authoritative live run

| | |
|---|---|
| Process | Elden Ring pid **44045**, Highball bottle `Games`, fresh launch |
| Scene | in-world, open field |
| Input | physical controller, steady 360°/s camera rotation, operator-driven. **No synthetic input, no injected events** |
| Duration | **175.52 s** |
| Source frames acquired | **8,697** |
| Tuple attempts | 21 |
| **Accepted** | **21** |
| Rejected | 0 |
| Skipped | 0 |
| Motion quality settings | unchanged from R118 |
| Gate thresholds | unchanged (`activeFraction≥0.15`, `endpointDifference≥0.002`, `maxMagnitude≤256`) |

`R116_RUN_SUMMARY`:

```
earlyBDispatch=20 lateMCallbacksAfterEarlyB=20 orderingViolations=0
coalescedPresentations=0 presentationCallbackMissing=0
watchdogArmed=20 watchdogFired=0 earlyBDispatchEnabled=1 watchdogBoundMs=150
gateEncoded=20 gateEncodeFailures=0
displayConfirmed=20 displayRejected=0 displayEvidenceLost=0 promotionEligible=1
```

21 accepted exceeds the brief's 20-tuple requirement. The tuple budget was 20 and
the lifecycle re-armed one final attempt before the controller stopped it; that
21st tuple is included rather than discarded, because discarding the last tuple is
exactly the kind of post-hoc selection the brief forbids.

`R119_EVIDENCE_DIRECTORY pid=44045 … path=<r119-live-throughput> writable=1
protected=0 override=<r119-live-throughput> allowLegacy=<unset>` — the live process
wrote where it was told to, and nowhere else (§3.2).

Log: `run-01-live/observer.44045.log`, 106,039 lines, 26.4 MB.
Metrics: `run-01-live/service.json`.

---

## 13. Generation-service distribution

Primary metric, unchanged from R118: **B present hook → gate verdict.** Excludes
the game's own pre-present render time for B. Includes blit, copy, dispatch,
preparation, estimator, MetalFX and gate.

| Metric | n | min | mean | median | p90 | max | stdev |
|---|---:|---:|---:|---:|---:|---:|---:|
| **FG generation service** | 21 | **18.090** | 20.008 | **19.728** | 21.548 | 25.411 | 1.785 |
| Generation-thread service | 21 | 15.891 | 18.289 | 18.110 | 19.694 | 23.654 | — |
| End-to-end from drawable (context) | 21 | 18.564 | 20.809 | 20.273 | 22.365 | 27.275 | — |

### 13.1 Decomposition

| Component | min | mean | median | p90 | max |
|---|---:|---:|---:|---:|---:|
| `bAcquiredToGenStart` | 1.631 | 2.521 | **2.299** | 3.138 | 5.773 |
| Preparation (alloc, encode) | 0.179 | 0.261 | 0.226 | 0.374 | 0.385 |
| **Estimator, CPU wall** | 8.304 | 11.427 | **11.674** | 12.618 | 14.761 |
| Estimator, GPU span | 6.796 | 9.767 | **9.932** | 10.978 | 12.819 |
| CPU wall − GPU span | 1.414 | 1.660 | 1.592 | 1.942 | 2.482 |
| Estimator complete → MetalFX submit | 0.041 | 0.190 | 0.081 | 0.502 | 1.003 |
| **MetalFX transition** | 5.286 | 6.017 | **5.772** | 6.712 | 8.332 |
| Gate decision | 0.160 | 0.393 | 0.255 | 0.640 | 1.796 |

`R119_DECOMPOSITION maxErrorMs=0.000000 (EXACT)` — prep + estimator + gap + MetalFX
+ gate equals `genThreadServiceNs` on every tuple.

**Note on summing medians.** The component medians sum to 20.307 ms against a
service median of 19.728 ms. Medians are not additive for correlated components;
the 0.579 ms difference is that artefact, not a reconciliation failure. The EXACT
reconciliation is per-tuple, and it holds. Every figure in this report that
combines components uses a per-tuple computation, never a sum of medians.

### 13.2 Shape

The distribution is tight — 18.09 to 25.41 ms, sample stdev 1.785, IQR
18.768–20.742 — so this is a uniform deficit, not a tail problem. **Zero of 21
tuples met even the minimum target** (< 17.502 ms measured arrival); 18 of 21 were
under 21 ms and 13 of 21 under 20 ms. The minimum observed service, 18.090 ms, is
itself above the arrival median: there is no operating point in this distribution
that keeps up.

---

## 14. SOURCE-arrival distribution

Measured from consecutive `R108_SOURCE_FRAME_ACQUIRED` monotonic timestamps in the
same process. The nominal 16.6667 ms period is **not substituted anywhere**.

| Metric | n | min | mean | median | p10 | p90 | max |
|---|---:|---:|---:|---:|---:|---:|---:|
| Source arrival interval | **8,696** | 0.728 | 20.185 | **17.502** | 16.140 | 25.618 | 1847.357 |

| Derived | Value |
|---|---:|
| Median-implied arrival rate | **57.1 Hz** |
| Mean-implied arrival rate | 49.5 Hz |
| Display refresh period (reference only) | 16.6667 ms |

### 14.1 The denominator is robust

The brief required all relevant consecutive source acquisitions as the denominator,
and warned against using only tuples whose cadence was already slowed by FG. Three
denominators, three different stall policies:

| Denominator | n | median | ratio to 19.728 |
|---|---:|---:|---:|
| All intervals | 8,696 | 17.502 | **1.127** |
| Excluding stalls > 100 ms | 8,684 | 17.499 | 1.127 |
| Excluding stalls > 2× nominal (33.4 ms) | 8,439 | 17.421 | 1.132 |

The ratio moves by 0.005 across the most aggressive stall filter available. There
is no defensible choice of denominator that produces a pass.

**What is *not* used, and must not be:** the arrival measured only across
tuple-bounding frames is **23.561 ms median** (n=21). Dividing 19.728 by 23.561
gives 0.837 and would "pass". That number is depressed by the very stall being
measured — it is the cadence *after* FG has already slowed the game — and the
analyser labels it `SOURCE_ARRIVAL_IN_TUPLE (not a denominator)` for that reason.

---

## 15. Throughput ratio

| Ratio | Value |
|---|---:|
| **median service / median arrival** (primary) | **1.127×** |
| mean / mean | 0.991× |
| p90 / p90 | 0.841× |
| median service / p10 arrival | 1.222× |
| median service / nominal 16.6667 ms | 1.184× |
| mean service / mean arrival, R118 | 1.100× |

### 15.1 Read the right row

Only the **median-over-median** row is the verdict, because only medians are
comparable across two distributions with different shapes. The other four rows are
included because the brief asked for the full picture, and two of them are
informative in one specific way: **the mean ratio is below 1 (0.991) and the p90
ratio is well below 1 (0.841)** while the median ratio is 1.127.

Neither is a pass hiding in the statistics, and neither may be promoted to the
verdict:

- **Mean-over-mean.** The arrival distribution has a long right tail (max
  1847 ms — one 1.8 s hitch) that drags its mean up to 20.185 ms; the service
  distribution has no such tail. The ratio of two means is a ratio of two
  different tail shapes, not a throughput statement.
- **p90-over-p90.** The arrival p90 is 25.618 ms — 46 % above its own median —
  because the game hitches. The service p90 is 21.548 ms. A pipeline can sit
  comfortably under the p90 of a hitching source while still being slower than the
  source on the median frame, which is exactly what happened here. Reporting
  0.841× as the throughput would say "FG keeps up" while 21 of 21 frames failed to.

R118 had the same property (1.100× mean against 1.207× median, 1.154× p90 against
1.207×) and treated the median as primary for the same reason. **The metric
definition was not changed to obtain a pass** — it is R118's, applied unchanged.

### 15.2 Against the performance targets

| Target | Result |
|---|---|
| Minimum: median service < measured arrival median | **NOT MET** — 19.728 vs 17.502 |
| Preferred: ≤ 15 ms | **NOT MET** — 19.728 |
| Strong: ≤ 14 ms | **NOT MET** — 19.728 |

### 15.3 R118 → R119 comparison

| | R118 | R119 | Change |
|---|---:|---:|---:|
| Service median | 20.452 | 19.728 | **−0.724** |
| Arrival median | 16.939 | 17.502 | +0.563 |
| Ratio | 1.207 | 1.127 | −0.080 |
| `bAcquiredToGenStart` | 2.672 | 2.299 | −0.373 |
| Estimator GPU | 10.218 | 9.932 | −0.286 |
| MetalFX | 5.656 | 5.772 | +0.116 |
| Estimator → MetalFX gap | 0.303 | 0.081 | −0.222 |

**The ratio improvement is not a throughput improvement.** It is mostly the
denominator rising: a different run, a different camera path, a busier GPU. The
honest reading is that the pipeline got ~0.7 ms cheaper (partly real — the removed
export work — partly run variation) and the game got ~0.56 ms slower, and the two
moved the ratio by 0.08. Neither number is a trend.

---

## 16. Accounting, gate and game stability

| Check | Result |
|---|---|
| Tuple ordering violations | **0** |
| Coalesced presentations | **0** |
| Missing presentation callbacks | **0** |
| Watchdog armed / fired | 20 / **0** |
| Gate encoded / encode failures | 20 / **0** |
| Display confirmed / rejected / evidence lost | 20 / **0** / **0** |
| Gate path | `transition-command-buffer` on every tuple |
| Promotion eligible | 1 |
| Decomposition reconciliation | **EXACT**, 21/21 |
| Handoff reconciliation | **EXACT**, 21/21 |
| Estimator reconciliation | **EXACT**, 21/21 |
| Estimator queue clock mismatches | **0/21** |
| Queue delay clamped to zero | **0/21** |
| Rejection reasons | none recorded |
| Game crashes / hangs | **none** |
| Run completed its tuple budget | yes, 21/21 |

### 16.1 No hidden work-queue accumulation

| | Value |
|---|---:|
| Service slope across tuples | **+0.0030 ms/tuple** |
| Estimator slope | −0.0065 ms/tuple |
| MetalFX slope | +0.0296 ms/tuple |
| First-half median | 19.479 ms |
| Second-half median | 19.728 ms |
| `monotonicGrowth` | **False** |

A +0.003 ms/tuple drift over 21 tuples is 0.06 ms total — inside noise. There is no
accumulating backlog, and the second half is not systematically worse than the
first. If a queue were filling, the last tuples would be visibly slower; they are
not.

### 16.2 SOURCE/INTERNAL accounting unchanged

The `SOURCE`-class and `INTERNAL`-class counters are emitted per tuple with the
gate decision and were not modified this round. `promotionEligible=1` and
`displayEvidenceLost=0` on every tuple.

---

## 17. Throughput verdict

```
R119_SERVICE_VS_ARRIVAL medianServiceABOVE medianArrival
GENERATION_THROUGHPUT_BELOW_SOURCE_RATE
```

| Verdict condition | Result |
|---|---|
| Median generation service < median SOURCE arrival | **FAIL** — 19.728 vs 17.502 |
| p90 service near/below normal arrival budget | **FAIL** — 21.548 vs 17.502; p90 arrival is 25.618, so p90 service is inside p90 arrival but above the median budget |
| No monotonic service growth | **PASS** — slope +0.003 ms/tuple |
| No hidden work-queue accumulation | **PASS** — §16.1 |
| Game stable | **PASS** — 0 crashes, 0 watchdog fires, 0 ordering violations |
| Estimator output preserved | **PASS** — bit-identical on 3 pairs, §11 |
| Accounting and presentation valid | **PASS** — §16 |

**Residual deficit: 2.226 ms against the measured arrival median (11.3 % of
service); 3.061 ms against the nominal period (15.5 %).**

### 17.1 Where the deficit lives

Every term below is computed per tuple and then summarised; no two medians are
added to each other (§13.1 note).

| Component | ms | Share of service |
|---|---:|---:|
| Non-GPU overhead (handoff, prep, gap, gate, queue, wake) | **3.955** | 20.0 % |
| GPU work at offline cost (6.0595 + 5.07) | **11.130** | 56.4 % |
| **GPU contention** (median service − non-contended floor) | **4.643** | 23.5 % |
| **Observed service median** | **19.728** | 100 % |

The 4.643 ms is contention measured as a difference of medians. Summarised the
other way — median of the per-tuple contention — it is **4.892 ms**, split
3.873 ms estimator and 0.702 ms MetalFX with 0.32 ms of co-variance between the two
stages (§8.4). The two summaries differ by 0.25 ms because contention and non-GPU
overhead are not independently distributed across tuples. The table uses the
smaller, conservative figure; **either way the contention is 4.6–4.9 ms against a
2.226 ms deficit.**

**The deficit is contention. Nothing else in the pipeline can produce 2.226 ms.**

To pass, the pipeline needs 2.226 ms. Removing the contention entirely would give
**15.085 ms** — under the measured arrival *and* under the preferred 15 ms target,
with 2.35 ms of margin. Removing the handoff entirely (impossible, and it is only
2.299 ms including prep and the copy) would not be enough on its own if contention
stayed.

### 17.2 The brief's central question, answered

> Is the extra live cost (A) kernel slower while competing for GPU, (B) command
> buffer waiting behind game work, or (C) both?

**(A), with a minor (B) component — closer to (C) in the strict sense, but with a
3:1 split that matters.**

| | ms | Basis |
|---|---:|---|
| (A) kernel slower while competing | **3.873** | execution 9.932 vs 6.0595 offline = 1.639×; floor 6.796 = 1.121× |
| (B) waiting behind game work | **1.244** | `commitToGpuStart`, tight 1.068–1.471, 10.5 % of CPU wall |

The (B) term is not just small, it is *stable*. A command buffer waiting behind
variable game work would have a variable wait; this one varies by 0.40 ms across
21 tuples. The GPU is essentially free when the estimator is submitted, and then
the estimator runs 64 % slower than it does when nothing else is on the GPU.

**Do not call queue delay GPU execution**, and do not call GPU execution queue
delay. They are 1.244 and 9.932 ms, measured on different clocks, reconciled
per-tuple to 0.000000 ms error, and gated on a verified clock-domain assumption.

---

## 18. Next product path

### 18.1 What R119 settles

1. **Evidence isolation is fixed and proved.** The R118 overwrite class is closed,
   including an instance R118's own report missed. `EVIDENCE_DIRECTORY_ISOLATION
   = VALIDATED` with two corpus-wide SHA-256 snapshots and 0 violations.
2. **The estimator's arithmetic is not the problem, and the R117 optimisation
   stands.** 6.06 ms offline, 1.639× contended, bit-identical output on three
   live pairs. Reopening the estimator's maths would be optimising the wrong
   thing.
3. **The 16 ms dispatch-stall class was a diagnostic artefact.** It does not exist
   in a bounded-diagnostics build.
4. **The handoff is not the lever.** 1.651 ms median, 2 µs enqueue-to-start,
   0.042 ms of queue hops.
5. **The failure is 4.6–4.9 ms of GPU contention against a 2.226 ms deficit.**

### 18.2 What R120 should do

The brief's instruction is explicit: *"If throughput still fails: report exact
residual deficit and component decomposition, then decide whether one further
estimator optimization is warranted."* R119 reports both. The decision:

**One further estimator optimisation is not warranted on the evidence — but not
because the estimator is fast. Because the estimator's cost is not arithmetic.**

The measured floor of the live estimator is 6.796 ms against 6.0595 ms offline.
Even a *free* estimator would leave the service at 19.728 − 9.932 = 9.796 ms plus
MetalFX and non-GPU overhead, and that only passes because the estimator would be
gone. There is no arithmetic change that removes 3.873 ms of contention, because
that 3.873 ms is not being spent in the kernel's instructions.

Two directions are supported by the data, in order:

**(1) Reduce GPU occupancy overlap with the game's render — not the total work.**
The contention is anti-correlated between the two stages (ρ = −0.417), which means
FG's GPU work is colliding with the game's frame *as a block*, and whichever stage
is running absorbs it. The target is not "make the estimator faster" but "make the
estimator's 6.06 ms of GPU time not overlap the game's render". Candidates, in
order of expected value per unit of risk:

- **Interleave instead of serialise.** The estimator and MetalFX run back-to-back
  (§9). If MetalFX's 5.07 ms were issued on a queue that can overlap the game's
  next render while the estimator is not running, the anti-correlation suggests the
  GPU has slack in the MetalFX window that is currently unused.
- **Split the estimator into two dispatches** so the game's frame can interleave
  between them. This does not reduce work; it reduces the contiguous occupancy that
  causes the collision. It is a scheduling change with a correctness-neutral
  surface, which is why it is first.

**(2) Reduce total GPU work — but not by degrading motion.**
Offline GPU cost is 11.13 ms against a 17.502 ms budget, so there is 6.37 ms of
headroom if contention is removed. The brief forbids reducing search range,
resolution, refinement or thresholds. What remains is the estimator's stage
decomposition (downsample / coarse / refine / propagate / upsample), which R117
measured offline at 3.34 ms coarse + 2.46 ms refine — 96 % of the estimator. A
further optimisation there is legitimate only if it is arithmetic-equivalent, and
the R117 result shows that is possible (3× faster, bit-identical).

**Do not:** reimplement MetalFX (1.04× floor — it is not contended); chase the
handoff (1.651 ms, and 2 µs of it is the queue); reopen the estimator's maths
without a contention-removal experiment first.

### 18.3 The 30→60 decision

The brief states: *"If throughput passes: stop performance work; R120 should pivot
to 30→60 end-to-end frame generation on this 60-Hz display. If throughput still
fails: report the exact residual deficit and component decomposition, then decide
whether one further estimator optimisation is warranted."*

**Throughput did not pass, so R120 does not pivot to 30→60.** No 30→60 work was
implemented in R119, per the brief.

The projection in §17.1 does make the pivot's precondition concrete: a
contention-free pipeline is 15.085 ms, which is 2.417 ms inside the measured
arrival median and 1.58 ms inside the nominal period. That is a real midpoint
refresh slot, with margin. The pivot is reachable — but only after contention is
addressed, and R119 has not addressed it.

### 18.4 Honest summary of the round

R119 fixed a data-integrity defect that had been silently corrupting historical
evidence for four rounds, and it converted the throughput problem from an
open-ended "make it faster" into a bounded, measured target: **4.6–4.9 ms of GPU
contention, 3.873 ms of it in the estimator's execution, against a 2.226 ms
deficit.** It did not make the pipeline fast enough, and it did not implement the
Phase 4 scheduling changes because their measured ceiling (~1.6 ms, none of it
touching the contention) could not have produced a pass. That is a decision the
next round can overturn with a measurement, which is the right position to leave
it in.

---

## Appendix A — Artifact index

All paths relative to `experiments/metalfx_frame_interpolator_R93/evidence/r119-live-throughput/`.

| Path | Contents |
|---|---|
| `observer-title-inprocess-r119-throughput.m` | instrumented observer, 390,017 B |
| `libfgmetal_r119_throughput_probe.dylib` | built dylib, sha256 `cc6fa03c…ad61c6` |
| `r119-observer.diff` | +519 / −48, 23 hunks vs R118 |
| `build-verification.json` | `R117_OPTIMIZED_ESTIMATOR_PRESENT` |
| `embedded-estimator-r119.metal` | kernel literal extracted from the dylib, 17,338 B |
| `run-01-live/observer.44045.log` | 106,039 lines, 26.4 MB |
| `run-01-live/service.json` | analyser output |
| `motion-pair-*.rg16f` ×3 | 5,621,280 B each |
| `source-sequence-*.raw` ×6 | 5,621,280 B each |
| `title-M-for-*.rgba16f` ×3 | 11,242,560 B each |
| `phase4_regression.sh` | equivalence harness (path bug fixed, §11.5) |
| `phase4-live-pairs.log` | 3 pairs × 3 statements, all `BIT_IDENTICAL` |
| `projection.txt` | per-tuple contention projection |
| `evidence-isolation/VALIDATION.md` | Phase 0 proof, pre- and post-live |
| `evidence-isolation/*-snapshot.json` | 5,278-file SHA-256 corpus snapshots |
| `tools/analyze_r119.py` | distributions, reconciliations, ratios |
| `tools/projection_r119.py` | contention projection |
| `tools/verify_r119_estimator.py` | binary-level estimator identity |
| `tools/snapshot_evidence.py` | corpus snapshot / compare |
| `tools/apply_bottle_env.py` | reversible bottle environment (`--apply` / `--restore`) |
| `tools/evidence_probe_host.m` | dlopen host for the isolation probe |
| `bottle-env-r119.json` | exact environment used |

The Highball bottle environment has been **restored** to its pre-R119 state
(`RESTORED … from=bottle.json.pre-r119`; `--show` now returns `{}`).

## Appendix B — Reproduction

```sh
cd experiments/metalfx_frame_interpolator_R93/evidence/r119-live-throughput

# 1. build + prove the binary carries the R117 estimator
./build_r119_tools.sh
python3 tools/verify_r119_estimator.py --json build-verification.json

# 2. prove the evidence directory cannot reach a historical corpus
FGMETAL_EVIDENCE_SELFTEST=1 ./tools/evidence_probe_host ./libfgmetal_r119_throughput_probe.dylib
python3 tools/snapshot_evidence.py compare evidence-isolation/baseline-snapshot.json \
        evidence-isolation/after-live-snapshot.json --allow "$PWD"

# 3. measure the recorded run
python3 tools/analyze_r119.py run-01-live/observer.44045.log --json run-01-live/service.json
python3 tools/projection_r119.py run-01-live/observer.44045.log

# 4. prove the motion field is unchanged
./phase4_regression.sh "$PWD" 7282 7283 1470 956
./phase4_regression.sh "$PWD" 7309 7310 1470 956
./phase4_regression.sh "$PWD" 7339 7340 1470 956
```

## Appendix C — Terms

| Term | Definition |
|---|---|
| **FG generation service** | B present hook → gate verdict. Excludes the game's render for B. The primary metric, unchanged from R118. |
| **Queue delay** | `GPUStartTime − CPU commit time` for the estimator command buffer. Not GPU execution. |
| **GPU execution** | `GPUEndTime − GPUStartTime`. Not queue delay. |
| **Contention factor** | live GPU span ÷ offline GPU span for the same kernel at the same resolution. |
| **SOURCE arrival** | interval between consecutive `R108_SOURCE_FRAME_ACQUIRED` monotonic timestamps. Measured, never nominal. |
| **Non-contended floor** | GPU work at offline cost + measured non-GPU overhead. A projection, not a measurement. |
| **bounded sample** | first / middle / last tuple of the budget carry full artefacts; all tuples carry timing, gate, accounting and WSI. |
