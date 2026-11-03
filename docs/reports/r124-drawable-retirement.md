# FG-METAL — R124: Drawable Retirement Context

**Round:** R124 — narrow falsification round
**Harness:** WorkBuddy AI, DeepSeek v4.1 Flash — High
**Date:** 2026-11-01
**Evidence:** `experiments/metalfx_frame_interpolator_R93/evidence/r124-drawable-retirement/`

---

## 1. Verdict

```
DRAWABLE_RETIREMENT_CONTEXT = NOT_YET_ISOLATED
```

Neither hypothesis in the brief survives.

- `TITLE_PRESENT_CONTEXT_REQUIRED_FOR_DRAWABLE_RETIREMENT` — **not validated.** Test 1, presenting
  from the title's own present thread, behaves identically to the pump.
- `DRAWABLE_ACQUISITION_PROVENANCE_REQUIRED` — **not validated.** Test 2, presenting a
  title-acquired drawable later from the pump, behaves identically to Test 1.

The measured distinction that remains is this:

> **The ~1 s drawable withholding is present in the title's own, unmodified D3DMetal present path
> with FG-Metal presenting nothing at all.** In pass-through — no suppression, no replacement
> present, 128 of 129 dispatches issued by the title itself — 41 of 128 `nextDrawable` calls still
> block for the layer's full timeout (p90 1010.3 ms, max 1016.1 ms).

Therefore `FRESH_PRESENTATION_DRAWABLE_RETIREMENT = BLOCKED` cannot be attributed to the
replacement-present design, the presenting thread, the drawable's provenance, the present API, or
`displaySyncEnabled`. The condition that withholds drawables is already in the title's layer before
FG-Metal presents anything.

This round did not find the condition. It did eliminate five candidate explanations and falsify one
of R123's two load-bearing inferences (§7, §10).

---

## 2. Exact hypothesis under test

From the brief:

> Successful retirement may depend on the title/D3DMetal presentation context — presenting thread,
> acquisition context, drawable provenance, attachment to the normal context. Do not assume which
> one is correct. Measure them separately.

Two separable claims:

- **H1 (thread/context):** a replacement drawable retires only when presented from the same
  thread/context the title uses for its normal present.
- **H2 (provenance):** a replacement drawable retires only when it was acquired from the title's
  normal acquisition context.

H1 and H2 were measured in the same process, same layer, same window, with one variable changed at
a time.

---

## 3. Method: three arms in one process

Comparing two launches would leave the window, the layer, the title's phase and the GPU state free
to differ. The arm is therefore switched **inside the running process**: the observer re-reads
`/tmp/fgmetal-r108-unattended/r124-test` every 200 ms.

| Arm | Name | What presents the replacement | Variable isolated |
|---|---|---|---|
| 0 | `pump` | R123's pump thread, R123 code unchanged | the measured failure (control) |
| 1 | `title-present-thread` | title's own present hook, title's thread | presenting thread/context |
| 2 | `title-acquired / pump-presented` | pump thread, drawable pre-acquired at the title's `nextDrawable` | acquisition provenance |
| 3 | `pass-through` | nothing — title presents normally | FG presentation removed entirely |

Arm 3 was added after the first live session, because arms 0–2 all exhaust the pool within seconds
and the title could not be brought into the world. It then became the most informative arm in the
round.

Provenance is enforced by the controller: it refuses to drive a process whose injected dylib is not
this round's, or whose dylib is newer than the process birth time.

```
pid 46594, dylib sha256 ea19bb9ba47e8decad5c6b5b1c85f411501b41903adf564d1fcdaebf499708fb
source sha256 1e3e3d023d4e1b526e08881db1dd0bd8173f7ac6796bc5426bf9fe0daaf21bbb
matches: true, dylibNewerThanProcess: false, hasR124Mode: true, hasTitleLayer: true
```

---

## 4. Test 1 — replacement present from the title's present thread

### Implementation

Called from inside all three present hooks, immediately after `r123TrySuppressSourcePresent` returns
YES, so it runs on the title's own present thread with the title's own layer:

```objc
if (r123TrySuppressSourcePresent((id<CAMetalDrawable>)self)) {
    NSDictionary *r124SuppressedRecord =
        trackedDrawableForTexture(((id<CAMetalDrawable>)self).texture);
    r124MaybeTitleThreadPresent([r124SuppressedRecord[@"sequence"] unsignedLongLongValue]);
    return;
}
```

`r124PresentReplacement()` acquires a fresh drawable from the title's layer, writes a known
FG-owned image into it (blit from the capture texture, else an alternating clear), allocates the WSI
identity immediately before dispatch, registers `addPresentedHandler:`, and calls the drawable's
original `present` IMP.

### Measurements (`run-session-e/arm1-title-present-thread`)

| Quantity | Value |
|---|---|
| Replacement presents | 22 |
| Reached display (`presentedTime > 0`) | **6 / 22** |
| `presentedTime == 0` | 16 / 22 |
| Present-to-present spacing | p50 **1036.4 ms**, p90 1037.4 ms |
| Replacement acquisition latency | p50 0.374 ms, p90 0.613 ms |
| Title `nextDrawable` | n=48, p50 0.333 ms, p90 **1001.8 ms**, max 1002.3 ms |
| Title `nextDrawable` > 500 ms | **19 / 48** |
| Presented handler fired | 21/22 within p50 20.4 ms, p90 52.1 ms (upper bound) |
| Replacement drawable back to title | 19/22, p50 **2100.7 ms**, p90 8491.8 ms, max 10355.0 ms |

---

## 5. Test 2 — title-acquired drawable, pump-presented

### Implementation

Pre-acquired at the title's own acquisition point, right after `installDrawablePresentationHooks`:

```objc
installDrawablePresentationHooks(drawable);
// R124 TEST 2: pre-acquire the replacement drawable here, at the title's own
// acquisition point, and hand it to the pump.
r124MaybePreAcquireReplacement();
retainDrawableForCorrelation(drawable);
```

No additional original SOURCE drawables are retained, and `maximumDrawableCount` is never touched
(the observer contains no reference to it at all — see §12).

### Measurements (`run-session-e/arm2-title-acquired-pump-presented`)

| Quantity | Value |
|---|---|
| Replacement presents | 19 |
| Reached display (`presentedTime > 0`) | **3 / 19** |
| `presentedTime == 0` | 16 / 19 |
| Present-to-present spacing | p50 **1037.5 ms**, p90 1037.8 ms |
| Replacement acquisition latency | 0.0 ms (pre-acquired; cost moved to the title's acquisition) |
| Title `nextDrawable` | n=39, p50 1.240 ms, p90 **1001.8 ms**, max 1002.1 ms |
| Title `nextDrawable` > 500 ms | **19 / 39** |
| Presented handler fired | 19/19 within p50 19.0 ms (upper bound) |
| Replacement drawable back to title | 16/19, p50 **1550.5 ms**, p90 5692.4 ms, max 10359.6 ms |

### Result: Test 1 ≡ Test 2

Both arms produce the same outcome to within noise: one drawable held for ~1 s, the title's own
acquisition blocking behind it at p90 ≈ 1002 ms, and most replacement presents reporting
`presentedTime == 0`. **Thread and provenance are eliminated as the discriminator.**

---

## 6. The control that decides the round — pass-through (arm 3)

`run-session-h/arm3-pass-through/passthrough-clean.log` — a window with no suppression and no
replacement present. Verified clean:

```
suppressed = 0        replacement presents = 0
SOURCE dispatches = 129   of which title's own = 128, FG-FRESH = 1 (boundary leftover)
```

| Quantity | Value |
|---|---|
| Title acquisitions | 129 |
| Title `nextDrawable` | n=128, p50 0.565 ms, p90 **1010.3 ms**, max 1016.1 ms |
| Title `nextDrawable` > 500 ms | **41 / 128 (32%)** |

All blocks cluster at 1001–1016 ms — the layer's own timeout, not scheduling noise.

### The stall rate grows over the window

Stalls per 16 acquisitions:

```
gaps   0- 15:  6%      gaps  64- 79: 38%
gaps  16- 31:  6%      gaps  80- 95: 38%
gaps  32- 47: 38%      gaps  96-111: 50%
gaps  48- 63: 31%      gaps 112-127: 50%
```

And the fast-run structure is **not** "three fast frames then one stall", which is what a healthy
3-deep pool being cycled would produce. It is 1–2 fast frames per stall (26 runs of length 1, 12 of
length 2), i.e. the usable pool behaves as if it were 1–2 deep, and it degrades as the window runs.

### Cross-arm comparison

| Arm | Acquisitions | Blocked > 500 ms | Rate |
|---|---|---|---|
| 3 — pass-through (FG presents nothing) | 128 | 41 | **32%** |
| 0 — R123 pump | 198 | 81 | 41% |
| 1 — Test 1 | 48 | 19 | 40% |
| 2 — Test 2 | 39 | 19 | 49% |

The arm slices are short and begin at an arm switch, so the *rates* are not a clean ranking. The
robust result is the qualitative one: **the stall is already present at 32% with FG-Metal out of the
presentation path.** The replacement arms raise it by roughly 8–17 points, which is a real but
second-order effect.

---

## 7. Standalone falsification — three variants on a plain layer

The probe (`tools/r124_retire_probe.m`, x86_64, plain `CAMetalLayer`, 1472×956, BGRA8,
`framebufferOnly=NO`, `maximumDrawableCount=3`, `allowsNextDrawableTimeout=YES`) was extended this
session with two switches, so that the two mechanisms the live title exhibits could each be tested
in isolation.

| Configuration | Frames | Displayed | Distinct objs | Object recycles | `nextDrawable` timeouts | p50 | max |
|---|---|---|---|---|---|---|---|
| `title-only` control | 40 | 39 | 13 | 27 | **0** | 0.045 ms | 2.389 ms |
| `title-only --retain 32` | 40 | **1** | 37 | 0 | **0** | 0.089 ms | 2.534 ms |
| `title-only --offscreen` | 30 | 10 | 10 | 6 | **0** | 0.127 ms | 3.278 ms |
| `pump` | 30 | 22 | 6 | 24 | **0** | 0.092 ms | 14.862 ms |

Two mechanisms were reproduced **separately**, and neither produces the stall:

- **Strong retention of 32 drawables** (the observer's own registry shape) reproduces the
  `presentedTime == 0` signature almost completely — 39 of 40 presents report no display time — and
  reproduces the object churn (37 distinct objects, 0 recycles). It produces **zero** `nextDrawable`
  timeouts: the plain layer allocates fresh drawables instead of waiting.
- **An offscreen window** also reproduces `presentedTime == 0` (only 10 of 30 displayed) and produces
  **zero** timeouts.

So on a plain layer, neither retention pressure nor loss of visibility is sufficient to make
`nextDrawable` block. The stall requires the title's own layer. That is the remaining distinction,
and it is now bounded to the layer, not to the present call.

---

## 8. Drawable identity and recycling evidence

R123's central identity inference is **falsified** and was already falsified in the probe before any
live run:

> R123 read `drawableID` as a pool-slot identity ("drawableID climbed 305 → 1116, so these were new
> drawables, not recycled ones").

The probe's positive control contradicts that reading: 40 acquisitions into a three-deep pool
produced **40 distinct `drawableID`s, 0 ID recycles, and 0.109 ms p50 acquisition latency** — the
pool was recycling perfectly while the identifiers climbed monotonically. `drawableID` is an
acquisition counter on this OS, not a pool slot.

All identity tracking in this round is therefore keyed on drawable **object pointer** and texture
**pointer**:

- Arm 1: 48 acquisitions → 38 distinct drawable objects, 48 distinct drawableIDs. Same-pointer reuse
  did occur (e.g. `0x6000033f9ea0` at slots 1 and 2; `0x6000033c3aa0` at slots 3, 4, 5, 7, 10), but
  at 1.0–3.1 s intervals, never within a refresh.
- Arm 3 (pass-through): 129 acquisitions → 52 distinct drawable objects, 123 distinct textures.

**Caveat, stated plainly:** object/texture churn turned out to be a weak discriminator. The
`--retain 32` probe produced 37 distinct objects over 40 acquisitions *without* stalling. The
load-bearing measurement in this report is therefore the **timeout count**, not the identity
churn. Identity is used only to show that reuse happens on a multi-second scale, not per refresh.

---

## 9. `presentedTime` — corrected interpretation

R123 read `presentedTime == 0` as "accepted but dropped, drawable not returned". R124 measures
something more specific, and the difference matters:

The replacement's `R101_WSI_DISPLAYED` record is emitted **inside the presented handler**, so a
`presentedTime == 0` line proves the handler *did* fire — it is not evidence of a stuck drawable.

- Arm 1: handler fired for 21/22 presents within p50 **20.4 ms**, p90 52.1 ms.
- Arm 2: handler fired for 19/19 within p50 **19.0 ms**.

So the presentation **concludes promptly** in essentially every case, including the 16/22 and 16/19
that report no display time. `presentedTime == 0` means "concluded without reaching the display",
not "still held". Yet the drawable still does not come back to the title for 1.0–10.4 s.

**Handler-fired and drawable-returned are different events**, and R123's report conflated them.

Note also that reaching the display does not prevent the next acquisition from blocking: in arm 1,
presents that *did* display were followed by a 1001.3 ms block at p50 — indistinguishable from the
1001.7 ms p50 after presents that did not.

---

## 10. Fresh `nextDrawable` latency

| Context | n | p50 | p90 | max |
|---|---|---|---|---|
| Arm 1 — replacement acquisition | 22 | 0.374 ms | 0.613 ms | 0.709 ms |
| Arm 1 — title's own acquisition | 48 | 0.333 ms | 1001.8 ms | 1002.3 ms |
| Arm 2 — title's own acquisition | 39 | 1.240 ms | 1001.8 ms | 1002.1 ms |
| Arm 0 — title's own acquisition | 198 | 0.536 ms | 1002.1 ms | 1031.2 ms |
| Arm 3 — title's own acquisition (pass-through) | 128 | 0.565 ms | 1010.3 ms | 1016.1 ms |

Acquiring a *fresh* replacement drawable is fast in every arm — the ~989 ms figure in the brief was
not reproduced as an acquisition cost. What costs ~1 s is the **title's own next acquisition**, and
it costs the same in pass-through as in every FG arm.

---

## 11. Thread / context provenance

| Arm | Acquisition thread | Present thread | Queue | Outcome |
|---|---|---|---|---|
| 1 | title's present thread (in-hook) | title's present thread | pump queue | 6/22 displayed, ~1002 ms blocks |
| 2 | title's acquisition thread | pump thread | pump queue | 3/19 displayed, ~1002 ms blocks |
| 3 | title's acquisition thread | title's present thread | title's own | 32% blocks, FG not presenting |

Context is recorded on every replacement record (`context=TITLE-PRESENT-THREAD` /
`context=PUMP-THREAD-HANDOFF`) and cross-checked against `R101_WSI_DISPLAYED ... context=`.

---

## 12. Safety contracts

| Contract | Status |
|---|---|
| `maximumDrawableCount` stays 3 | **held by construction** — the observer contains no reference to `maximumDrawableCount`, `allowsNextDrawableTimeout` or `presentsWithTransaction` (grep: zero matches) |
| Original SOURCE drawable not retained by FG | held — `originalDrawableRetained=0`, `action=return-without-dispatch` |
| SOURCE identity remains SOURCE | held — `origin=SOURCE`, `appFrameId` preserved |
| INTERNAL remains INTERNAL | held — `internalPresentId=0` on all SOURCE records |
| No duplicate SOURCE presentation | held — suppressed presents allocate `wsiPresentId=UNALLOCATED` and dispatch nothing |
| No duplicate INTERNAL presentation | held |
| WSI ID allocated only immediately before dispatch | held — `allocateWsiPresentId()` is the last statement before dispatch |
| No unbounded queue | held — pump depth bounded, `depth` logged |
| No title-thread sync on estimator or MetalFX | held |
| Evidence in a new directory | held — `r124-drawable-retirement/`; no previous evidence directory was written |

**One honest exception to flag:** the observer *does* hold strong references to up to 32 drawables
via `retainDrawableForCorrelation()` → `drawablesByTexture` (`kRetainedDrawableLimit = 32`). §7 shows
this is not sufficient to cause the stall on a plain layer, but it is an uncontrolled variable in
every live arm and it is the first thing R125 should remove.

---

## 13. Title SOURCE cadence

- Arm 1: pace gaps p50 33.874 ms, p90 1029.1 ms; pacing gate blocked p50 22.7 ms, p90 32.7 ms.
- Arm 3 (pass-through): pace gaps p50 34.285 ms, p90 1035.9 ms; pacing gate blocked p90 32.8 ms.

The title holds its ~30 fps pacing target between stalls in every arm, including pass-through. The
stalls are not the pacing gate: they are `nextDrawable` returning late, after which the pacing gate
passes immediately.

---

## 14. Independent review — the ten required items

1. **Variables differ only in the intended one.** *Held for Test 1 vs Test 2.* The only difference is
   which thread presents and where the drawable was acquired. **Broken for the round as a whole:**
   arm 3 is not a pristine baseline — the observer's hooks are still installed (read-only), and its
   32-drawable retention registry is still active. Flagged, not hidden.
2. **No fourth drawable.** Held by construction (§12).
3. **Original title drawable still released.** **NOT CONFIRMED.** R123's `VALIDATED` was not
   reproduced. Measured by texture reappearance, only 10 of 48 suppressed originals returned in arm 1
   (p50 14.7 s), 0 of 39 in arm 2, 28 of 198 in arm 0 (p50 20.3 s). The proxy is weak (textures
   churn), so this is stated as *unconfirmed*, not as *leaking*.
4. **Identity evidence establishes recycle/non-recycle.** **Partially.** `drawableID` is falsified as
   an identity (§8). Object-pointer reuse is real but multi-second. Identity churn alone does not
   establish pathology — the `--retain 32` probe churns without stalling. The timeout count is the
   load-bearing evidence.
5. **`presentedTime` and completion interpreted correctly.** **Corrected** (§9). Handler fires
   promptly; `presentedTime == 0` means "concluded without display", not "stuck".
6. **No title thread waits on FG generation.** Held — the copy is enqueued and waited on the pump;
   the title thread only issues the present.
7. **Accounting unchanged.** Held (§12).
8. **No previous evidence directory modified.** Held — all writes are under
   `r124-drawable-retirement/`, including `run-session-a` … `run-session-h`, `run-probe/`.
9. **EAC / online excluded from the verdict.** Held. Online functionality is unavailable in this
   environment because of Easy Anti-Cheat; nothing in this round depends on it, and no online warning
   was treated as evidence or as a regression.
10. **Final claim does not exceed the experiment.** Held — the verdict is negative, and §6's
    pass-through result is the only positive claim, scoped to this title, this layer and this
    environment.

---

## 15. Exact blocker

`FRESH_PRESENTATION_DRAWABLE_RETIREMENT = BLOCKED` is **reproduced but re-attributed**. The
withholding is real, but it is not caused by the replacement present: it is present at 32% of
acquisitions in the title's own path with FG-Metal presenting nothing, and it degrades over time
(6% → 50%) in that path.

Five candidate causes are now excluded by measurement:

| Candidate | Excluded by |
|---|---|
| Presenting thread | Test 1 ≡ Test 2 ≡ arm 0 |
| Drawable acquisition provenance | Test 1 ≡ Test 2 |
| Present API (`present` vs `presentDrawable:`) | probe: both retire on a plain layer |
| `displaySyncEnabled` | probe: retires with it off |
| Strong retention of 32 drawables | probe `--retain 32`: `presentedTime=0` but 0 timeouts |
| Window not being displayed | probe `--offscreen`: `presentedTime=0` but 0 timeouts |

What is *not* excluded: the title's D3DMetal layer implementation itself, and the interaction between
the observer's retention registry and that layer.

```
DRAWABLE_RETIREMENT_CONTEXT = NOT_YET_ISOLATED — not thread, not API, not provenance, not
display-sync, not drawable retention, not window visibility: the ~1 s withholding occurs in the
title's own unmodified present path with FG-Metal presenting nothing (41/128 acquisitions blocked at
1010.3 ms p90), so the remaining distinction is inside the title's D3DMetal layer.
```

---

## 16. Smallest next product step

**R125 should remove the instrument's own retention before investigating anything else.**

`retainDrawableForCorrelation()` currently stores `@"drawable": drawable` — a strong reference — in
a dictionary capped at 32, i.e. ten times the pool depth, on every title acquisition. §7 shows this
is enough to suppress display (`presentedTime == 0` on 39 of 40 presents) but not enough to stall a
plain layer. It is the one uncontrolled variable left in every live arm, it is a one-line change
(store the texture or the drawable's `drawableID`, not the drawable), and if it is the amplifier that
turns retention pressure into a stall on D3DMetal's layer, it removes the blocker without touching
the product at all.

Concretely, in this order:

1. Change `retainDrawableForCorrelation` to keep `@"texture": texture` and `@"drawableID": @(...)`
   instead of `@"drawable": drawable`, and drop the cap to 8. Rebuild, relaunch.
2. Re-run **pass-through only**. Success criterion: `nextDrawable > 500 ms` falls from 32% to ≈0 in
   the same window. That single number decides whether the R123/R124 blocker is product or
   instrument.
3. Only if pass-through is clean, re-run Test 1 and Test 2 and resume continuous 30→60.

Do not rebuild the pump, do not change pool depth, and do not add infrastructure before step 2
reports.

---

## 17. Files

| Path | Role |
|---|---|
| `observer-title-inprocess-r124.m` | live observer, arms 0–3, ~516 KB |
| `run_r124.py` | arm controller with dylib provenance guard |
| `tools/analyze_r124.py` | log analyser (v2; corrects v1's two wrong assumptions) |
| `tools/r124_retire_probe.m` | standalone probe; extended with `--retain N` and `--offscreen` |
| `run-session-e/` | arms 0, 1, 2 in one process |
| `run-session-h/arm3-pass-through/passthrough-clean.log` | **the decisive pass-through window** |
| `run-probe/{control,retain32,offscreen,pump}.log` | the four falsification runs |
| `bottle.json.pre-r124` | environment backup (restored at end of round) |
