# FG-METAL — R125 — Retention Control

**Round:** R125 — remove one uncontrolled measurement variable
**Date:** 2026-11-01
**Harness:** WorkBuddy AI, DeepSeek v4.1 Flash — High, throughout. No model-family switching.
No sub-agent performed analysis, implementation or review; the desktop-automation tool was used
only to launch the title.
**Corpus:** `experiments/metalfx_frame_interpolator_R93/evidence/r125-retention-control/`
(R123 and R124 corpora untouched — verified: no file in either tree has an mtime after 16:00.)

---

## 0. Verdict

> ### `INSTRUMENTATION_DRAWABLE_RETENTION_CONTAMINATION = VALIDATED`

The observer's correlation registry held strong references to `CAMetalDrawable` objects — up to 32
of them on a layer whose pool is 3 deep. Removing that retention, and changing nothing else in the
frame path, removes the ~1 s `nextDrawable` withholding completely:

| | R124 (retention active) | R125 (retention removed) |
|---|---|---|
| Title acquisitions | 128 | 2 199 / 2 279 |
| Blocked > 500 ms | 41 | **0 / 0** |
| p90 | 1 010.3 ms | 0.060 ms / 0.036 ms |
| Wall-clock time blocked | 91.8 % | **0.0 %** |
| Effective frame rate | 2.85 fps | 29.3 fps |

The same holds on the two arms that present from FG-Metal:

| Arm | R124 | R125 |
|---|---|---|
| 1 — replacement from the title's present thread | 48 acquisitions, 19 blocked (40 %), 6/22 replacements displayed | 1 409 acquisitions, **0 blocked**, **24/24 displayed** |
| 0 — R123 display pump | 198 acquisitions, 81 blocked (41 %) | 1 855 acquisitions, **0 blocked**, `drawableFailures=0` |

Two further results are reported below and are *not* part of the verdict: R124's pass-through was
not one variable wide (§5), and the bounded continuous 30→60 attempt runs without any drawable stall
but does not yet produce interpolated frames (§6).

The strongest single piece of supporting evidence is in §4.6: in R124 the layer minted a **new
drawable object every 2.5 acquisitions and a new texture almost every acquisition**; in R125 it
recycles 10–21 objects and 2–6 textures across thousands of frames. The pool was not merely slow, it
was being rebuilt continuously.

---

## 1. The question

R124 concluded that the ~1 s withholding was "present in the title's own, unmodified D3DMetal
present path with FG-Metal presenting nothing at all", and excluded `displaySyncEnabled` from the
causes. Its §12 flagged the one thing it had not controlled: the observer's own registry, which
stored `@"drawable": drawable` — a strong reference — capped at 32 entries.

On a 3-deep layer, retaining 32 drawables is not a diagnostic detail. If it mattered, then R124's
"the title stalls by itself" was measuring the observer. This round removes exactly that and asks
whether the stall survives.

---

## 2. What was changed

One function, and the call sites that depended on it.

`retainDrawableForCorrelation()` now stores scalars only:

```objc
drawablesByTexture[textureKey] = @{
    @"sequence":     @(sequence),
    @"drawableID":   @(drawable.drawableID),
    @"drawablePtr":  @((uintptr_t)(__bridge void *)drawable),
    @"texturePtr":   @((uintptr_t)(__bridge void *)texture),
    @"layer":        @((uintptr_t)(__bridge void *)drawable.layer),
    @"device":       @((uintptr_t)(__bridge void *)layerDevice),
    @"acquiredAtNs": @(acquiredAtNs),
};
```

`kRetainedDrawableLimit` 32 → 8 (the dictionary is now a bounded lookup table, not a hold list).

Three consequences had to be handled, and only these:

- **Consumers that used to read the object back.** `reportCorrelation()`, `r105AssociateProducer()`
  and the capture path used `record[@"drawable"]`. They now read `drawablePtr`, `texturePtr`,
  `layer`, `device` through `r125PointerField()` and cast, or take the live object from the call
  site. `trackedRecordForDrawable()` validates the drawable's address against `drawablePtr` before
  trusting a record, so a recycled address cannot inherit a stale correlation.
- **`captureRealDrawable()`** no longer receives the drawable. It takes
  `(queue, id<MTLTexture> source, uint64_t drawableID, sequence, producerCommandBuffer)`.
- **A runtime audit** (`r125AuditRegistry()`), once per 64 acquisitions, walks the live dictionary
  and logs the class of every value. "No objects in the registry" is a measurement, not a comment.

Nothing else moved: no pool depth, no pacing, no display-sync policy, no pump behaviour, no present
API, no estimator. The full inventory is `audit/static-retention-audit.txt`, regenerated from the
built source by `audit/generate_audit.py`.

---

## 3. The control design

R124's "pass-through" was not a control. Two things were true of it that the round neither varied
nor reported (see §5). A single slice therefore cannot decide this question, so the round runs two
slices **in the same process**, switched at runtime through
`/tmp/fgmetal-r108-unattended/r125-test` (`<arm> <pristine> <capture>`):

| Slice | Arm | Pump | `displaySyncEnabled` | Capture | What it is |
|---|---|---|---|---|---|
| **pristine** | 3 (pass-through) | not started² | never written (stays 0) | off | the title's own path, observed only |
| **parity** | 3 (pass-through) | allowed, idles | forced 0→1 | on | R124's exact configuration minus retention |
| replacement | 1 | allowed, idles | inherited 1 | on | one minimal replacement present |
| continuous | 0 | presenting | inherited 1 | on | bounded 30→60 attempt |

`FGMETAL_R125_PRISTINE=1` makes `r123EnsureStarted()` refuse before it can touch the layer, and
makes `captureRealDrawable()` return before copying. The pump is never started in the pristine
slice, so `displaySyncEnabled` is never written.

² Not merely refused — never asked. `r123EnsureStarted()` has one caller, `r123EnqueueSource()`, and
that sits behind the capture gate; with capture off the function is unreachable. The
`R125_PUMP=REFUSED` line in the process log appears at line 164 675, long after the pristine window
closed, when a copy from the previous slice completed across a switch. It is not part of that slice's
evidence, and the pristine slice's claim rests on the *absence* of `R123_PUMP=STARTED` and
`R123_LAYER_DISPLAY_SYNC=FORCED` inside its window, not on that line.

The pristine slice is what R124 could not produce: the title's own present path with FG-Metal not
merely "not presenting" but *not running*.

---

## 4. Measurements

### 4.1 Primary — the title's own `nextDrawable`

Latency is measured around the original `nextDrawable` IMP only (`return − entry`), on the title's
acquisition thread. The R120 pacer runs *before* the entry instant, so the 30 Hz pacing block is
outside the measured interval.

| Slice | n | p50 | p90 | p99 | max | >100 ms | >500 ms | >900 ms | 990–1030 ms |
|---|---|---|---|---|---|---|---|---|---|
| **R124 arm 3** (reference) | 128 | 0.565 | **1 010.273** | 1 014.830 | 1 016.080 | 43 | **41** | 41 | 41 |
| **R125 pristine** | 2 199 | 0.032 | 0.060 | 0.080 | 0.661 | 0 | **0** | 0 | 0 |
| **R125 parity** | 2 279 | 0.024 | 0.036 | 0.061 | 228.774 | 1 | **0** | 0 | 0 |
| R125 replacement (arm 1) | 1 409 | 0.037 | 0.042 | 0.049 | 0.060 | 0 | **0** | 0 | 0 |
| R125 continuous (arm 0) | 1 855 | 0.035 | 0.076 | 8.035 | 74.478 | 0 | **0** | 0 | 0 |

The R124 reference row is not quoted from R124's report: the same analyser, pointed at
`r124-drawable-retirement/run-session-h/arm3-pass-through/passthrough-clean.log` through its
`R110_ENTRY_RETURN_FALLBACK` path, reproduces 128 / 41 / 0.3203 / p90 1 010.273 / max 1 016.080
exactly. The analyser is therefore not the difference.

All 41 blocked acquisitions are ≥ 900 ms and all 41 fall inside 990–1030 ms: R124's stalls were not a
slow tail, they were exactly the layer's timeout, 41 times. R125's four slices contain none of them
in 7 742 acquisitions.

The parity slice's single 228.774 ms acquisition sits at index 175 of 2 279, with the next largest at
0.154 ms — a 1 500× gap. That is one discrete event (a window/hitch), not a distribution tail. It is
reported rather than smoothed away, and it is not near the layer's timeout.

### 4.2 Wall-clock impact

The stall *rate* understates R124. A stalled acquisition costs ~1 010 ms; a healthy one costs
0.03 ms. R124's window is 45 s long and contains 128 acquisitions:

| Slice | n | span | effective fps | time blocked > 500 ms | share of window |
|---|---|---|---|---|---|
| R124 arm 3 | 128 | 44.94 s | **2.85** | 41.24 s | **91.8 %** |
| R125 pristine | 2 199 | 75.10 s | 29.28 | 0.00 s | 0.0 % |
| R125 parity | 2 279 | 78.09 s | 29.18 | 0.00 s | 0.0 % |
| R125 replacement | 1 409 | 48.10 s | 29.30 | 0.00 s | 0.0 % |
| R125 continuous | 1 855 | 63.50 s | 29.21 | 0.00 s | 0.0 % |

The title was not "stalling 32 % of the time" in R124. It was stalled 92 % of the time and managed
2.85 frames per second.

### 4.3 Degradation over the window

R124's rate degraded 6 % → 38 % → 50 % across 128 acquisitions. R125's parity slice runs 2 279
acquisitions in 143 buckets of 16, and **every bucket is 0.0 %**. There is no onset and no
degradation. Whatever accumulated in R124 accumulated within the first few dozen frames; nothing
accumulates now.

### 4.4 Registry, measured at runtime

`R125_REGISTRY_AUDIT` fires once per 64 acquisitions:

| Slice | audit lines | `drawableObjects` | `textureObjects` | other objects |
|---|---|---|---|---|
| pristine | 35 | 0 | 0 | 10 (`producerQueue`) |
| parity | 35 | 0 | 0 | 12 (`producerQueue`) |
| replacement | 22 | 0 | 0 | 14 (`producerQueue`) |
| continuous | 29 | 0 | 0 | 14 (`producerQueue`) |

121 audits, zero drawables, zero textures. The "other objects" are the `MTLCommandQueue` the
records are keyed against, bounded by the same 8-entry cap.

### 4.5 Cross-checks

- `R125_TITLE_ACQUISITION` vs an independent recomputation from `R110_SOURCE_NEXT_DRAWABLE_ENTRY` /
  `_RETURN`: 2 199/2 199, 2 279/2 279, 1 409/1 409, 1 855/1 855 — **0 mismatched latencies**.
- `drawableID` is an acquisition counter, not a pool slot: `distinctDrawableIDs == n` in all four
  slices (2 199, 2 279, 1 409, 1 855). Pool identity is the object address: 14, 13, 10 and 21
  distinct pointers respectively.
- The layer at session start:
  `displaySyncEnabled=0 presentsWithTransaction=0 allowsNextDrawableTimeout=1 framebufferOnly=1 maximumDrawableCount=3 drawableSize=1472x956`.
  `allowsNextDrawableTimeout=1` is what makes a ~1 s block observable at all; the pool is 3 deep and
  was never changed.
- `maximumDrawableCount` and `allowsNextDrawableTimeout` are never assigned anywhere in the file
  (audit §F). The only layer write is `displaySyncEnabled`, inside the pump start gate a pristine
  slice refuses.

### 4.6 The layer stopped minting new drawables

Counting the distinct objects the title actually received, per slice:

| Slice | acquisitions | distinct drawable objects | distinct textures | new drawable per acquisition |
|---|---|---|---|---|
| R124 arm 3 | 128 | **52** | **123** | **0.41** |
| R125 pristine | 2 199 | 14 | 2 | 0.006 |
| R125 parity | 2 279 | 13 | 4 | 0.006 |
| R125 replacement | 1 409 | 10 | 3 | 0.007 |
| R125 continuous | 1 855 | 21 | 6 | 0.011 |

In R124 the layer produced a new drawable object every 2.5 acquisitions and a new texture almost
every acquisition. In R125 it recycles 10–21 objects and 2–6 textures across thousands of frames.

This is the clearest statement of what the retention did: it is not that the pool got slightly
slower, it is that the pool was being rebuilt continuously. A three-deep pool behaves like a
three-deep pool only in R125. Which way the causation runs — a drawable the layer cannot reuse
forces a new allocation, or an allocation follows a timeout — is not resolved here and is not
needed for the verdict; either way the two configurations do not have the same layer, and the
difference tracks the retention exactly.

### 4.7 Pass-through is genuinely pass-through

| Slice | suppressed presents | replacement presents | FG dispatches | pump started | `displaySync` forced | dispatches by origin |
|---|---|---|---|---|---|---|
| pristine | 0 | 0 | 0 | 0 | 0 | 2 199 SOURCE |
| parity | 0 | 0 | 0 | 1 | 1 | 2 280 SOURCE |
| replacement | 1 409 | 24 | 24 | 0¹ | 0¹ | 24 SOURCE |
| continuous | 1 855 | 0 | 3 362 | 0¹ | 0¹ | 33 INTERNAL, 3 362 SOURCE |

¹ inherited from the parity slice in the same process — see §5.3.

In the pristine slice every acquisition produced exactly one SOURCE dispatch and one
`R101_WSI_DISPLAYED`: 2 199 : 2 199 : 2 199. Nothing was withheld, deferred or duplicated. In the
parity slice the pump thread exists but presents nothing (`0` internal dispatches, `0` slot events),
which is the arm-3 contract.

---

## 5. The second uncontrolled variable R124 missed

This is not a re-investigation of R124. It is the reason the round needed two slices, and it changes
how one sentence of R124 should be read.

### 5.1 The layer was modified

R124's report excludes `displaySyncEnabled` as a cause. R124's own live log contains:

```
R123_LAYER_DISPLAY_SYNC=FORCED layer=0x600000718f60 before=0 after=1 reason=pump-requires-one-present-per-refresh
```

`r123EnsureStarted()` is called from the capture path (`r123EnqueueSource` → `r123EnsureStarted`), and
it forces `displaySyncEnabled` from NO to YES before starting the pump. With `FGMETAL_R123_PUMP=1`
and capture on, merely having the instrumentation loaded changed the layer's presentation contract.
The pass-through window then ran on a layer that was no longer the title's layer.

### 5.2 The window's own counters could not see it

R124's decisive window, `run-session-h/arm3-pass-through/passthrough-clean.log`, shows
`pumpStarted=0` and `displaySyncForced=0` — because both happened *before* the window opened. A
window-scoped counter cannot observe inherited state. The window does show `captureCopies=129` for
128 acquisitions, so the capture path was live throughout.

The window's `pid=46594` and `layer=0x600000718f60` match the process and layer in the `FORCED`
line. It is the same layer.

### 5.3 What R125 does about it

The pristine slice never enables capture, so `r123EnqueueSource` is never called and the pump is
never even asked to start. The layer stays at `displaySyncEnabled=0` for the whole slice, and the
title runs 2 199 acquisitions with zero stalls.

The parity slice deliberately reproduces R124's configuration — pump available, `displaySync`
forced, capture on — and also runs 2 279 acquisitions with zero stalls.

So `displaySyncEnabled` is excluded by measurement now, not by assertion: both states are stall-free
once the retention is gone. R124's conclusion happened to be right; its control did not establish it.

For completeness: the replacement and continuous slices inherit `displaySyncEnabled=1` and a running
pump thread from the parity slice, because they run later in the same process. Their
`pumpStarted=0` / `displaySyncForced=0` counters mean "already done", not "not done". Neither slice
is used to make a claim about `displaySyncEnabled`.

---

## 6. Bounded continuous 30→60 attempt

The brief authorises one bounded attempt once pass-through is clean. It was run: arm 0 (the R123
display pump as the only presenter), capture on, 60 s, then the controller returned the process to
pristine.

### 6.1 The drawable problem is gone

| Measurement | Result |
|---|---|
| Title `nextDrawable` | n=1 855, p50 0.035 ms, p90 0.076 ms, p99 8.035 ms, max 74.478 ms, **0 > 500 ms** |
| Pump `drawableFailures` (first 900 slots) | **0** |
| Pump `presentFailures` | **0** |
| Pump `idleSlots` | **0** |
| Displays confirmed (`presentedTime > 0`) | 3 167 of 3 393 |

In R124's arm 0 the same pump blocked the title on 81 of 198 acquisitions (41 %). Here it never
blocks the title and never fails to obtain a drawable. That is the result the round was looking for.

### 6.2 But it is not 30→60

The pump's own summary for its first 900 slots:

```
R123_PUMP_SUMMARY=COMPLETE reason=slot-budget-reached
  sourceEnqueued=5072 sourcePresented=866 internalEnqueued=4411 internalPresented=10
  internalResolved=3670 deadlineMisses=264 droppedInternal=4399 droppedSource=4202
  sourceSuppressedPresents=1892 drawableFailures=0 presentFailures=0 idleSlots=0
  orderViolations=463 orderGaps=396 sourcePeriodNs=16666667 slots=900
```

866 source frames and **10** interpolated frames in 900 slots. The output is ~53 Hz of *repeated
source frames* (3 393 presents in 63.5 s), not 30→60. And the reason is measurable:

| Measurement | Value |
|---|---|
| `R123_SOURCE_ENQUEUED` in the window | 5 248 |
| distinct `seq` among them | **1 856** |
| worst multiplicity of a single `seq` | **17** |
| `DRAWABLE_PRODUCER_COMPLETED` in the window | 5 263 |
| … of which on the pump's own present queue | **3 395** |
| … on the title's queue | 1 868 |

The title acquired 1 855 frames and completed 1 868 producer command buffers — one per frame, as it
should. The extra 3 395 completions are FG-Metal's own present command buffers on `r123Queue`, and
the capture path attributes each of them to a SOURCE frame, copying it and enqueuing it with a
*stale* sequence number. The pump is being fed its own output.

This is a capture-attribution defect, not a drawable defect, and it is independent of retention.
It is newly *observable* here because R124's arm 0 never presented enough frames to expose it
(R124's arm-0 windows show producer completions == acquisitions, all on the title's queue). R125
does not isolate its cause further; that would be a different round.

---

## 7. What R125 does not claim

- It does **not** claim the unmodified title never stalls. Every slice still runs under the observer.
  What is claimed is narrower: with the observer no longer retaining drawables, the withholding does
  not appear in 7 742 acquisitions across four configurations.
- It does **not** claim a mechanism inside `CAMetalLayer` / D3DMetal. How a strong external reference
  to a drawable delays the next `nextDrawable` is not established here and was explicitly out of
  scope. The round establishes that removing it removes the effect.
- It does **not** claim `ORIGINAL_SOURCE_DRAWABLE_RELEASE = VALIDATED`. The correct statement remains
  `PRODUCT_PATH_DOES_NOT_INTENTIONALLY_RETAIN_ORIGINAL_SOURCE`: on the suppression path the hook
  returns without dispatching and the drawable is released by its owner. Nothing in R125 measures
  D3DMetal's internal release.
- It does **not** claim `CONTINUOUS_30_TO_60_TITLE_FG = VALIDATED`. §6 shows the opposite.
- It does **not** interpret `presentedTime == 0` as a stuck drawable. A zero means the presentation
  concluded without a confirmed physical display; 226 of 3 393 continuous-slice presentations
  returned zero and none is described as stuck.
- It does **not** use `drawableID` as a pool slot. `drawableID` counts acquisitions (§4.5).
- It says **nothing** about EAC or online play. No slice depends on either.

---

## 8. Independent review

Ten items, each answered from the evidence rather than from intent.

**1. No strong drawable reference remains in the diagnostic registry.**
`audit/static-retention-audit.txt` §A prints the complete value written per acquisition — seven
`NSNumber` fields. §B lists all three mutation sites, each a dictionary of scalars. Runtime:
121 `R125_REGISTRY_AUDIT` lines across four slices, every one `drawableObjects=0 textureObjects=0`,
`retention=DRAWABLE-AND-TEXTURE-NONE`.

**2. No indirect block / container / associated-object retention replaces it.**
`objc_setAssociatedObject` / `objc_getAssociatedObject`: zero occurrences. File-scope drawable
storage: exactly one variable, `r124HandoffDrawable`, assigned and drained only when
`FGMETAL_R124_TEST=2`; arms 0, 1 and 3 never touch it. Three residual object retentions exist, and
all three are stated rather than hidden:

- The pump's copy-completion block names `drawable` in a discarded expression. Pump path only,
  bounded by the copy wait; the pump's own counters show the pool never ran dry
  (`drawableFailures=0`, `idleSlots=0` over 900 slots).
- The R105 attachment inventory holds `MTLTexture` objects for a 72-frame window at process start and
  is never cleared. Textures, not drawables, and inactive in these slices (zero `R105_` lines).
- **Two of the five `addPresentedHandler:` blocks capture the R102 ticket, and the ticket owns its
  drawable strongly** (`@property(nonatomic, strong) id<CAMetalDrawable> drawable`). That is a
  drawable → block → ticket → drawable cycle, and it would hold a drawable for the life of the
  process. It is not the registry and it is pre-existing (R102-era), and it is **not exercised in
  this round** — `r102TryDeferSourcePresent()` produced zero tickets in all four slices (zero `R102_`
  events), because the deferred-B path is off. It is recorded here because a future slice that
  enables deferred-B would reintroduce exactly the contamination this round removed.

The other three handlers use the `presented` argument and scalar captures only.

**3. Pass-through truly issues no FG presentations.**
pristine: 0 suppressed, 0 replacements, 0 FG dispatches, pump never started, 2 199 acquisitions →
2 199 SOURCE dispatches → 2 199 displays. parity: 0 suppressed, 0 replacements, 0 FG dispatches,
0 internal dispatches, 2 280 → 2 280. Both are 1:1:1.

**4. The title's normal present path is otherwise unchanged.**
`r123TrySuppressSourcePresent()` returns `NO` on its second line for arm 3, before any record
lookup. `r102TryDeferSourcePresent()` never fires (zero `R102_` events, and every present reached
WSI). No `presentAtTime:` or `presentAfterMinimumDuration:` was introduced. The hooks observe and
call through.

**5. `maximumDrawableCount` unchanged.**
`R123_LAYER_PROPERTIES … maximumDrawableCount=3` at session start; no assignment to it anywhere in
the file (audit §F); no code path writes `allowsNextDrawableTimeout` either.

**6. `nextDrawable` timeout counts computed correctly.**
Thresholds are strict `>` at 100 / 500 / 900 ms, and the ~1 s bucket is the closed interval
990–1030 ms. Every latency is recomputed independently from the `R110_ENTRY`/`RETURN` pair:
0 mismatches in 7 742 acquisitions. The analyser reproduces R124's published reference
(128 / 41 / 0.3203 / p90 1 010.273) from R124's own log, so the comparison is not an artefact of a
changed analyser.

**7. `drawableID` not used as a pool-slot identity.**
`distinctDrawableIDs == n` in every slice; pool identity is tracked on the drawable address
(14 / 13 / 10 / 21 distinct pointers), and `r124NoteReplacementDrawable()` hashes `uintptr_t`.

**8. `presentedTime == 0` not interpreted as a stuck drawable.**
The replacement slice reports 24/24 replacements with `presentedTime > 0`. The continuous slice
reports 226 zero-time presentations out of 3 393 and treats them as "not confirmed on a physical
display", which is what the field means.

**9. EAC / online excluded.**
No slice references either; no claim depends on either.

**10. Claims stay inside the measured evidence.**
§7 enumerates the claims the round does *not* make, including the two the brief specifically warned
about (`ORIGINAL_SOURCE_DRAWABLE_RELEASE`, and the natural-stall claim).

---

## 9. Smallest next product step

Not a research round. One gate, one rebuild, one 60-second run.

**Gate the producer-completion capture on the command buffer's queue.** The handler that logs
`DRAWABLE_PRODUCER_COMPLETED` (line 9763) already reads `completed.commandQueue`. If that queue is
`r123Queue`, the completion belongs to FG-Metal's own present and must not be attributed to a SOURCE
frame: return before `captureRealDrawable()`. That removes the feedback loop in §6.2 at its source,
without touching the pump, the estimator, the pool or the pacing.

Then rerun the same bounded continuous slice and check three things in this order:

1. `R123_SOURCE_ENQUEUED` distinct-`seq` ratio returns to ≈1.0 and `internalPresented` rises from 10.
2. `drawableFailures` and `idleSlots` stay at 0.
3. Title `nextDrawable` stays below 100 ms at p99.

If those hold, `CONTINUOUS_30_TO_60_TITLE_FG` is a measurement away rather than a design away — and
`SOURCE_PRESENTATION_DRAWABLE_DECOUPLING` can be restored on the pipeline the brief describes
(`A → M(A,B) → B → M(B,C) → C`) with the retention-free observer as the permanent instrumentation.

One precondition, from review item 2: do not re-enable the R102 deferred-B path before its
`addPresentedHandler:` retain cycle is broken. That cycle (drawable → block → ticket → drawable) is a
stronger retention than the registry this round removed, and it would bring the same contamination
back on a path that currently has none.

---

## 10. Evidence index

| Artefact | What it is |
|---|---|
| `observer-title-inprocess-r125.m` | the observer; SHA-256 `6faf3936…b0325bc2e` |
| `libfgmetal_r125_retention_control.dylib` | x86_64, built from the above; SHA-256 `7c8a5f1a…4cc05b5e` |
| `build_r125_tools.sh` | build + `r123_pump_selftest`; prints both hashes |
| `bottle-env-r125.json`, `tools/apply_bottle_env.py` | bottle environment, `--apply/--restore/--show` |
| `run_r125.py` | slice controller; refuses a process running a different or newer dylib |
| `tools/analyze_r125.py` | the analyser; reproduces R124's reference through its fallback path |
| `audit/generate_audit.py`, `audit/static-retention-audit.txt` | static retention audit, regenerated from the built source |
| `run-session-a/pristine`, `run-session-a/parity` | the control (75 s each, one process, pid 60652) |
| `run-session-b/replacement` | minimal replacement present (45 s) |
| `run-session-c/continuous` | bounded 30→60 attempt (60 s) |
| `run-session-*/analysis.json` | analyser output for every slice |

Reproduce:

```sh
cd experiments/metalfx_frame_interpolator_R93/evidence/r125-retention-control
./build_r125_tools.sh
python3 tools/apply_bottle_env.py --apply      # then quit and relaunch Highball
python3 run_r125.py --label session-a          # pristine, then parity
python3 tools/analyze_r125.py --json run-session-a/analysis.json \
        run-session-a/pristine/raw.log run-session-a/parity/raw.log
python3 tools/apply_bottle_env.py --restore
```

---

## 11. Closing

R124 eliminated five candidate causes and was left with "the condition is in the title's layer". It
was not. The condition was in the observer: a diagnostic registry that held every drawable the title
owned, on a layer that only has three.

The cost of that mistake was not a wrong number. It was a wrong *shape*: 41 stalls in 128
acquisitions reads like "32 % of frames hitch". The same data read as wall-clock says the title ran
at 2.85 fps. A round that had reported the second number would not have spent two rounds looking for
a D3DMetal behaviour to explain it.

The instrumentation is now non-owning, and it is measured to be non-owning rather than asserted to
be. The next blocker is a capture bug that the healthy pool made visible — which is the right kind
of problem to have.

---

## 12. Verdict

```
INSTRUMENTATION_DRAWABLE_RETENTION_CONTAMINATION = VALIDATED
```

**Smallest next product step.** One early return in the producer-completion handler that logs
`DRAWABLE_PRODUCER_COMPLETED`: if `completed.commandQueue == r123Queue`, the completion is FG-Metal's
own present and must not be attributed to a SOURCE frame. Rebuild, rerun the same 60 s continuous
slice, and check that the distinct-`seq` ratio returns to ≈1.0 while `drawableFailures` and
`idleSlots` stay at 0. No new research round is required to reach
`CONTINUOUS_30_TO_60_TITLE_FG`.
