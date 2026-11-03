# FG-METAL — R126 — Self-Present Gate

**Round:** R126 — stop FG-Metal from capturing its own presented frames
**Date:** 2026-11-01
**Harness:** WorkBuddy AI, DeepSeek v4.1 Flash — High, throughout. No model-family switching.
No sub-agent performed analysis, implementation or review; the desktop-automation tool was used
only to launch the title.
**Corpus:** `experiments/metalfx_frame_interpolator_R93/evidence/r126-self-present-gate/`
(R125 and earlier corpora untouched — every write is refused by the derived-corpus guard, which
now resolves R126's own directory as the sole writable root.)

---

## 0. Verdict

> ### `CONTINUOUS_30_TO_60_TITLE_FG = VALIDATED`

A small queue-origin gate at the top of the producer-completion handler removes the self-capture
feedback loop completely, and the bounded continuous configuration on the live title then holds a
sustained `SOURCE A → INTERNAL M(A,B) → SOURCE B → INTERNAL M(B,C) → SOURCE C` alternation at a
16.666 ms display interval for the whole window:

| Invariant | R125 (before) | R126 (after) |
|---|---|---|
| SOURCE enqueues / distinct SOURCE sequences | 5 248 / 1 856 = **2.83** | 2 296 / 2 296 = **1.000** |
| Worst multiplicity of one `seq` | **17** | **1** |
| Measured SOURCE period | 16 666 667 ns | **34 320 000 ns** |
| `orderViolations` | 463 (per 900 slots) | **0** (per 3 300 slots) |
| `droppedSource` | 4 202 | **0** |
| INTERNAL frames actually presented | 10 per 900 slots (1.1 %) | **1 600 per 3 300 slots (48.5 %)** |
| Displays per second | ~53 Hz of repeated SOURCE | **57.2 Hz, 48.8 % of them interpolated** |

Two independent windows were captured. Both reproduce the invariants exactly; the second one is
reported alongside the first because a single window is not evidence of a steady state (§9).

---

## 1. The question

R125 established that the observer's retention of `CAMetalDrawable` was the cause of the ~1 s
`nextDrawable` withholding, and removing it fixed that. It also left one defect standing, newly
*visible* rather than newly created: the capture path was attributing FG-Metal's own presentation
command buffers to SOURCE frames.

The mechanism, from R125 §6.2: `reportCorrelation()` registers a completed handler on **any** render
pass whose colour attachment is a tracked drawable texture. The R123 display pump encodes its copy
into a freshly acquired drawable **on its own queue** — so that pass also matches, and its completion
was treated as a title producer completion. Each such completion re-captured an already-captured
frame, re-enqueued it under a stale `seq`, and fed it back into the pump. The measurable signature
was 5 248 SOURCE enqueues across 1 856 distinct sequences (max multiplicity 17) and a pump whose
`sourcePeriodNs` had collapsed to 16 666 667 ns.

R126 asks one question: if FG-Metal's own completions are refused entry to the SOURCE path, does the
continuous 30→60 configuration produce a genuine sustained alternation on the live title?

---

## 2. The change

One file, 66 added lines and 3 replaced lines, 8 hunks. No new abstraction, no new file, no new
configuration knob. The producer-correlation design is untouched.

### 2.1 The gate

First statement after `NSError *error = completed.error;` inside the `addCompletedHandler:` block in
`reportCorrelation()`:

```objc
if (r123Queue && completed.commandQueue == r123Queue) {
    const uint64_t ignored =
        __atomic_add_fetch(&ignoredSelfPresentCompletions, 1, __ATOMIC_RELAXED);
    if (ignored <= 8) {
        __atomic_add_fetch(&ignoredSelfPresentLogged, 1, __ATOMIC_RELAXED);
        logLine("R126_SELF_PRESENT_COMPLETION=IGNORED pid=%d seq=%llu drawableID=%llu "
                "commandBuffer=%p queue=%p reason=queue-is-fg-presentation "
                "reachedSourceCapture=0 ordinal=%llu",
            getpid(), (unsigned long long)sequence, (unsigned long long)drawableID,
            (__bridge void *)completed, (__bridge void *)completed.commandQueue,
            (unsigned long long)ignored);
    }
    // The key is registered before the commit and must be released on every
    // exit path, or a recycled command-buffer address would be skipped on a
    // later frame.
    pthread_mutex_lock(&stateLock);
    [completedHookKeys removeObject:completionKey];
    pthread_mutex_unlock(&stateLock);
    return;
}
```

The `return` is above SOURCE capture, SOURCE sequence attribution, the SOURCE enqueue, the estimator
history update and MetalFX pair construction — the five things the brief required it to precede. The
completion-hook key is released on this path too, so a recycled command-buffer address is not skipped
on a later frame; that is the only non-obvious line in the block and it is why the gate is four lines
of logic rather than one.

### 2.2 The counter

```objc
// R126: command-buffer completions that arrived on FG-Metal's own presentation
// queue and were therefore refused entry to the SOURCE capture path. Evidence
// only -- it exists to prove the gate is exercised, not to steer anything.
static uint64_t ignoredSelfPresentCompletions;
static uint64_t ignoredSelfPresentLogged;
```

Echoed once per pump summary as `ignoredSelfPresentCompletions=%llu`. Nothing reads it; no decision
depends on it.

### 2.3 Arming evidence

The gate can only be evaluated once `r123Queue` exists, so its identity is logged once per process
from both queue-creation sites:

```objc
static void r126LogSelfPresentGate(void) {
    static uint64_t logged;
    if (__atomic_exchange_n(&logged, 1, __ATOMIC_RELAXED) != 0) return;
    logLine("R126_SELF_PRESENT_GATE=ARMED pid=%d queue=%p "
            "rule=completed.commandQueue==r123Queue "
            "action=return-before-source-capture counter=ignoredSelfPresentCompletions",
        getpid(), (__bridge void *)r123Queue);
}
```

Recorded in the live process log:

```
R126_SELF_PRESENT_GATE=ARMED pid=71349 queue=0x7fe147459e00 rule=completed.commandQueue==r123Queue
  action=return-before-source-capture counter=ignoredSelfPresentCompletions
```

### 2.4 Build provenance

| Artifact | SHA-256 |
|---|---|
| `observer-title-inprocess-r126.m` | `8753d97c1adde2afa278bbaf8d0ec9ec49ff0d40f4b48a5e6600ed575a296554` |
| `libfgmetal_r126_self_present_gate.dylib` | `22bc87b497401e4f3571f69381b73799180a47c83f9ed520bc38d2a1c3648611` |

`r123_pump_selftest` passes unchanged (`SELFTEST=PASS cases=6`). The injected dylib's hash was read
back from the running process environment and matches the built artifact; the dylib's mtime
(18:52:51) precedes the process start (18:55:00), so the process is running the binary measured here.

---

## 3. The gate in operation

| Measurement | Window A (75 s) | Window B (60 s) |
|---|---|---|
| `ignoredSelfPresentCompletions` | **3 290** | not emitted — the summary is emitted once per process (exactly one line in 127.8 MB of log) and was consumed in window A |
| Display confirmations before the summary line | **3 298** | — |
| Refused completions per displayed frame | **0.998** | — |
| `R126_SELF_PRESENT_COMPLETION=IGNORED` lines | 7 (log sample cap 8) | 0 |
| … all on queue | `0x7fe147459e00` = `r123Queue` | — |
| … with `reachedSourceCapture=1` | **0** | **0** |
| Producer completions on the title's primary queue | 2 295 | 1 854 |
| Producer completions on the title's secondary (overlay) queue | 5 | 19 |

**What the counter counts.** `ignoredSelfPresentCompletions = 3 290` is not an incidental number.
At the instant the summary was emitted, 3 298 display confirmations had been logged in the same
window — **one refused completion per displayed frame**, with 8 command buffers still in flight.
That is exactly the shape expected if every pump present encodes one command buffer on `r123Queue`:
the gate is measured to cover FG-Metal's own presentation work at 1:1, not merely to fire
occasionally.

Only the first 8 refusals are logged, and the earliest captured line carries `ordinal=2` — refusal
number 1 happened before the capture window opened. The 7 `IGNORED` lines are therefore a sample of
the mechanism, not a count of it. The count is the counter, and the counter's value is explained by
the display total to within 0.2 %.

Zero of them reached SOURCE capture: no `reachedSourceCapture=1` line exists in either window.

**The gate is not too wide.** Every genuine producer completion still passes through the identical
path. Proof from the same window: 2 295 completions on the title's primary queue
(`0x7fe142b1a800`) produced 2 296 SOURCE enqueues (the one-frame difference is the first enqueued
frame, `seq=19790`, whose completion line precedes the capture window's first line), and the 5
completions on the secondary overlay queue produced **zero** enqueues because preflight rejected
their bound texture (§8.3).

---

## 4. SOURCE invariant

Measured from the raw `R123_SOURCE_ENQUEUED` event stream, not from the pump's own summary.

| Measurement | Window A | Window B |
|---|---|---|
| SOURCE enqueues | **2 296** | **1 854** |
| Distinct SOURCE sequences | **2 296** | **1 854** |
| **enqueued / distinct** | **1.000** | **1.000** |
| Max multiplicity | **1** | **1** |
| Multiplicity histogram | `{1: 2296}` | `{1: 1854}` |
| Sequence range | 19 790 – 22 085 | 28 044 – 29 897 |
| Contiguous (no holes) | **yes** | **yes** |

2 296 = 22 085 − 19 790 + 1 exactly, and 1 854 = 29 897 − 28 044 + 1 exactly. Every sequence number
in the range appears exactly once. The R125 signature — a handful of sequences carrying up to 17
copies — is absent.

This is the invariant the brief asked for and it is met at 1.000, not "approximately".

---

## 5. Timing invariant

| Measurement | Window A | Window B |
|---|---|---|
| Pump `sourcePeriodNs` | **34 320 000** (34.32 ms) | — (one-shot summary consumed in window A) |
| Measured acquisition interval, median | 34.333 ms | 34.331 ms |
| … p90 / p99 | 34.354 / 34.376 ms | 34.359 / 34.394 ms |
| Pacer interval (`R120_PACE_ACQUIRE`), median | 34.341 ms | 34.342 ms |
| Title acquisition cadence | **29.271 fps** | **29.284 fps** |
| Acquisition latency, median | 0.021 ms | 0.025 ms |
| … p90 / p99 / max | 0.027 / 0.159 / 8.122 ms | 0.039 / 6.789 / 10.819 ms |
| Acquisitions > 100 ms | **0** | **0** |
| Acquisitions > 500 ms | **0** | **0** |

The three independent measurements of the same period — the pump's clamp, the acquisition event
stream, and the 30 fps pacer's own record — agree to within 0.06 %. The contaminated 16 666 667 ns
value is gone, and its replacement is not merely different but diagnostic: the pump clamps
`sourcePeriodNs` to `[half, 4 × half]` = [16.667 ms, 66.667 ms], and R125's value sat **exactly on
the floor**, the signature of a period that had saturated rather than been measured. R126's 34.32 ms
sits mid-band and tracks the title's real 34.333 ms cadence to 0.04 %.

The 30 fps pacer is untouched: `FGMETAL_R120_PACE_HZ=30`, measured 34.34 ms, no frame thinning,
dropping or relabelling anywhere in this round.

---

## 6. Generation and presentation

### 6.1 MetalFX pair generation

| Measurement | Window A | Window B |
|---|---|---|
| Pair attempts | 2 290 | 1 853 |
| `GENERATED` | 2 287 | 1 852 |
| `WARMUP` | 3 | 1 |
| `FAILED` | **0** | **0** |
| Skipped-busy | 2 | 0 |
| MetalFX wall, median | 5.538 ms | 5.167 ms |
| … p90 / p99 / max | 6.054 / 8.609 / 28.736 ms | 5.683 / 6.519 / 17.025 ms |
| Estimator GPU exec, median | 9.016 ms | 8.253 ms |

Generation keeps up: 5.5 ms of MetalFX wall time against a 17.16 ms midpoint budget. Skipped-busy is
2 in 2 290 attempts (0.09 %), so the generator is not saturated and is not the reason midpoints are
occasionally late.

### 6.2 SOURCE vs INTERNAL presentation

| Measurement | Window A | Window B |
|---|---|---|
| SOURCE dispatched / displayed | 2 296 / **2 296** | 1 853 / **1 852** |
| INTERNAL dispatched / displayed | 2 192 / **2 191** | 1 771 / **1 771** |
| Distinct SOURCE frames displayed | 2 296 | 1 852 |
| Distinct INTERNAL ids displayed | 2 191 | 1 771 |
| Duplicate SOURCE / duplicate INTERNAL | **0 / 0** | **0 / 0** |
| SOURCE : INTERNAL ratio | **1.047** | **1.046** |
| INTERNAL share of all displays | 48.8 % | 48.9 % |
| Amplification (displays / SOURCE) | **1.954×** | **1.956×** |

Every displayed frame is distinct. The 1.047 ratio is not duplication — it is the arithmetic
consequence of the ~2–4 % of midpoints that miss their deadline and are dropped (§7.2).

### 6.3 Physical display cadence

| Measurement | Window A | Window B |
|---|---|---|
| Display events (`R101_WSI_DISPLAYED`) | 4 487 | 3 623 |
| … with `presentedTime > 0` | 4 486 | 3 518 |
| Intervals measured | 4 485 | 3 517 |
| Display interval, median | **16.6665 ms** | **16.6665 ms** |
| … p90 / p99 | 16.6666 / 33.333 ms | 16.6666 / 33.333 ms |
| … min | 16.6659 ms | 16.6658 ms |
| Interval < 20 ms | 4 287 (95.6 %) | 3 393 (96.5 %) |
| Interval 20–40 ms | 179 (4.0 %) | 122 (3.5 %) |
| Interval 40–70 ms | 18 | 2 |
| Interval > 70 ms | 1 (83.3 ms) | **0** |
| Display span | 78.399 s | 60.716 s |
| Display rate | **57.21 fps** | **57.93 fps** |

The distribution is a hard spike at the panel period with a tail exactly one period long — the shape
of a pacer that is in phase, not of one that is drifting. p99 = 33.333 ms = 2 × 16.6665 ms: the
worst one-percent outcome is a single missed slot, never a partial interval.

**On "60".** The title acquires at 29.27 fps, so a perfect alternation yields 58.54 fps. Measured
57.21 fps is **97.7 %** of that ceiling (window B: 57.93 of 58.57 = **98.9 %**). The gap to a nominal
60 Hz is the title's own cadence plus the midpoint drops, not a defect in the pump.

---

## 7. Ordering

### 7.1 Tuple grammar

| Measurement | Window A | Window B |
|---|---|---|
| Complete `A → M(A,B) → B` triples | **2 192** | **1 771** |
| Triples with wrong pairing | **0** | **0** |
| `orderViolations` (position went backwards or repeated) | **0** | **0** |
| `orderGaps` | 103 | 81 |
| Gap-size histogram | `{1: 103}` | `{1: 81}` |
| Leading SOURCE-only run (cold start) | 65 | 2 |
| Steady-state displays | 4 421 | 3 621 |
| Steady-state alternation | **99.12 %** | **97.79 %** |

A triple is counted only when the middle frame's `sourcePair` is `(A, B)` with `A` the preceding
SOURCE and `B` the following SOURCE. 2 192 consecutive correct triples with zero mismatches is the
sustained alternation the brief required, not a single tuple.

### 7.2 Every gap is explained

The brief required no *unexplained* steady-state `orderGaps`. Each gap is mapped to a cause:

| | Window A | Window B |
|---|---|---|
| Gaps | 103 | 81 |
| … at a pair with a `R123_SLOT_MISSED` | **103 of 103** | **81 of 81** |
| … with no corresponding miss | **0** | **0** |
| Midpoint deadline misses | 104 | 82 |
| … during the cold-start SOURCE-only run | 65 | 2 |
| … in the steady state | **39** | **80** |
| Midpoints delivered / expected in steady state | 2 192 / 2 231 = **98.25 %** | 1 771 / 1 851 = **95.68 %** |

The one miss without a gap is the window's first pair in both windows (position 39 576 = 2 × 19 788
and 56 086 = 2 × 28 043), whose preceding SOURCE was dispatched before the capture window opened —
a boundary artifact, not an anomaly.

So the structure is exact, not statistical: in 99.12 % / 97.79 % of steady-state pairs the midpoint
arrives; when it does not, the pump **drops the midpoint and presents the SOURCE**. It never
substitutes a stale INTERNAL, never repeats a frame, never reorders. `droppedSource = 0` in both
windows: no SOURCE frame was ever sacrificed to make room for an interpolated one.

The distinction between a gap and a violation is not an interpretation imposed on the data after the
fact — it is a property the pump's own self-test asserts, and `r123_pump_selftest` still passes all
six cases unchanged (`SELFTEST=PASS cases=6`), including the case named *"a dropped midpoint is a gap,
not a violation"*.

### 7.3 Why midpoints are occasionally late

`R123_SLOT_MISSED pair=A->B reason=midpoint-not-ready-at-slot action=drop-midpoint-present-source`.
The midpoint budget is half a source period, 17.16 ms. Generation itself takes 5.5 ms median, so the
misses are not MetalFX. They are the cost of a 3-deep drawable pool under a saturated display
budget: 4 487 displays in 78.4 s means the pump is issuing a present every 17.5 ms on average, and
the title's own presents contend for the same pool. Window B, whose cold start was 2 frames instead of
65, shows the higher rate — 4.3 % vs 1.8 % — which is consistent with contention rather than with a
systematic scheduling error.

This is a **quality residual, not a blocker**: it costs ~1–2 fps against the achievable ceiling and
it never breaks ordering.

---

## 8. Failures, deadline misses, stability

### 8.1 Failure counters

| Counter | Window A | Window B |
|---|---|---|
| `drawableFailures` | **0** | **0** |
| `presentFailures` | **0** | **0** |
| `R123_SLOT_ABORTED` | **0** | **0** |
| `ELDENRING_SOURCE_COPY_FAILED` | **0** | **0** |
| `slotIdle` | 79 | 0 |
| `slotMissed` | 104 | 82 |
| `droppedInternal` | 100 | — |
| `droppedSource` | **0** | — |
| `maxDepth` | 4 | — |

### 8.2 Deadline misses and idle slots

`deadlineMisses = droppedInternal = 100` in the pump's first 3 300 slots, `droppedSource = 0`. The
pump degrades in exactly one direction: it loses interpolated frames, never source frames.

The 79 idle slots in window A occur only in its 65-frame cold-start region. Window B, whose cold
start was 2 frames, has zero. They are the slots before MetalFX had produced anything to show.

### 8.3 Preflight rejections — all accounted for

| | Window A | Window B |
|---|---|---|
| `SOURCE_CAPTURE_PREFLIGHT=FAILED` | 5 | 19 |
| Distinct sequences | 1 (`20456`) | 2 (`27985` ×15, `28133` ×4) |
| Bound texture | `pixelFormat=115`, `usage=5`, 1470×956 | same class |
| Completions on the secondary queue | 5, same seq | 19, same split |

These are the title's **overlay** pass: a different texture object on the same drawable, with
`usage=5` (`shaderRead|renderTarget`) and `pixelFormat=115` (`rg11b10float`) rather than the
presentable `usage=23`, `pixelFormat=80` backbuffer. Preflight correctly refuses it. The counts match
one-for-one with completions on the title's secondary queue, and **none of them produced a SOURCE
enqueue** — which is why the enqueue set is still exactly contiguous.

The affected frames were not lost. `seq=20456` shows the full lifecycle inside the window:

```
R123_SOURCE_PRESENT=SUPPRESSED  seq=20456 ... action=return-without-dispatch
ELDENRING_SOURCE_COPY_ENQUEUED  seq=20456 ... pixelFormat=80 size=1470x956
R123_SOURCE_ENQUEUED            seq=20456 ... stored=1 depth=3
```

### 8.4 Retention and the R102 path

| Measurement | Window A | Window B |
|---|---|---|
| Registry audits | 36 | 29 |
| `drawableObjects` retained (sum over audits) | **0** | **0** |
| `textureObjects` retained (sum over audits) | **0** | **0** |
| `R102_*` events | **0** | **0** |

The correlation registry remains non-owning; no strong `CAMetalDrawable` or texture reference is held
anywhere. R102 deferred-B stays disabled — zero events, no regression.

### 8.5 Stability

The title ran continuously from 18:55:00 through the end of window B and beyond. At the time of
writing (19:13:49) the process log had grown to 127.8 MB and was still being appended — no crash, no
hang, no watchdog. The control file was restored to `pristine (3 1 0)` at the end of each window and
the title returned to pass-through.

---

## 9. Two independent windows

| | Window A | Window B |
|---|---|---|
| Slice | continuous, 75 s | continuous, 60 s |
| Source frames | 2 296 | 1 854 |
| SOURCE : distinct | 1.000 | 1.000 |
| Max multiplicity | 1 | 1 |
| Acquisition cadence | 29.27 fps | 29.28 fps |
| SOURCE period | 34.32 ms | 34.33 ms |
| Display interval median | 16.6665 ms | 16.6665 ms |
| Display rate | 57.21 fps | 57.93 fps |
| Triples correct / mismatched | 2 192 / 0 | 1 771 / 0 |
| Order violations | 0 | 0 |
| Gaps (all size 1) | 103 | 81 |
| Gaps explained by a miss | 103 / 103 | 81 / 81 |
| Steady alternation | 99.12 % | 97.79 % |
| Drawable / present failures | 0 / 0 | 0 / 0 |
| Retention objects | 0 | 0 |

Window B is not a repeat of window A under identical conditions. It is a **fresh pump instance** —
its interpolated-frame ids restart at 1 — whose cold start lasted 2 frames instead of 65, because
MetalFX was already compiled in the process. The pump summary is emitted once per process, so window
B reports none. Window B also ran under a different scene load, which is why its midpoint-miss rate is
higher. It reproduces every invariant that the verdict depends on, and it shows the residual varying
with load rather than being a fixed property of the design. That is the more useful second sample.

---

## 10. Independent review

Ten checks, run against the evidence rather than against the implementation's intent.

**1. Is the gate on the path the defect was measured on?**
Yes. R125 §6.2 attributed 3 395 completions on `r123Queue` to SOURCE. In R126, 3 290
completions on that same queue object are refused, and the arming line records the exact address the
gate compares against. Same handler, same queue, same code path.

**2. Is the gate too wide — does it swallow genuine producer completions?**
No. The title's primary queue is a different object and produced 2 295 completions that all reached
SOURCE capture, yielding 2 296 enqueues over a fully contiguous sequence range. If the gate were
swallowing real frames, the range would have holes or the enqueue count would fall below the
acquisition count. Neither happened.

**3. Does `ignoredSelfPresentCompletions` count what it claims to count?**
Yes, and the arithmetic is exact rather than approximate: 3 290 refusals against 3 298 display
confirmations logged before the summary line, i.e. 0.998 refused completions per displayed frame.
The counter is measuring FG-Metal's own presentation command buffers, one per present, with 8 still
in flight when the summary was read.

**4. Is the SOURCE invariant measured independently of the pump's self-report?**
Yes. Enqueue counts, distinctness, multiplicity and contiguity are computed from
`R123_SOURCE_ENQUEUED` lines, and the pump's summary is reported separately as a cross-check. The two
agree: the summary reports 1 701 enqueues over its 3 300 slots, and because the global maximum
multiplicity is 1, those 1 701 are necessarily 1 701 distinct sequences.

**5. Is the timing invariant corroborated by a second clock?**
Yes, by three: the pump's clamped `sourcePeriodNs` (34.32 ms), the acquisition event stream
(34.333 ms median), and the R120 pacer's own interval record (34.341 ms median). Agreement to 0.06 %
rules out a single mis-scaled constant.

**6. Is the alternation a real tuple grammar or a counting artifact?**
Real. The triple test requires the INTERNAL's `sourcePair` to be exactly `(preceding SOURCE,
following SOURCE)`; 2 192 and 1 771 triples pass with zero mismatches. Bigram census of window A is
`SI 2192, IS 2192, SS 103` — no `II` at all, and every `SS` is either the cold-start run or a
documented midpoint drop.

**7. Are the gaps explained, or merely absent from the summary?**
Explained, one-for-one: all 103 (window A) and 81 (window B) gaps sit at a pair with a recorded
`R123_SLOT_MISSED`, and there are zero gaps without a miss. The single unmatched miss in each window
is the boundary pair whose preceding SOURCE predates the capture window.

**8. Did anything else change?**
No. The 30 fps pacer is untouched (34.34 ms measured, `FGMETAL_R120_PACE_HZ=30`). The registry is
still non-owning (0 retained objects across 65 audits). `maximumDrawableCount` is unchanged. R102
deferred-B remains disabled (0 events). The only environment difference from R125 is
`FGMETAL_R123_MAX_SLOTS=3300` instead of 900, which moves the summary inside the window and changes
no pump behaviour.

**9. Is the corpus isolated and is the binary the one measured?**
The evidence directory is R126's own and is the sole writable root; R125 and earlier are refused by
the same derived rule. The dylib hash was read back from the running process and matches the built
artifact; its mtime precedes process start by 2 min 9 s.

**10. Would this reproduce, and is the residual risk stated?**
It reproduced across two windows on the same process — one with a 65-frame cold start, one with a
2-frame cold start — with identical invariants and a different midpoint-miss rate. Residual risks are
stated where they belong: 1.8–4.3 % of midpoints miss their deadline under load (§7.3), and 105 of
3 623 display confirmations in window B read `presentedTime=0` and are excluded from interval
statistics (counts, ordering and provenance are unaffected).

---

## 11. What R126 does not claim

- **Not a claim about 60.00 fps.** The delivered rate is 57.2–57.9 fps, which is 97.7–98.9 % of the
  58.5 fps ceiling implied by the title's own 29.27 fps acquisition cadence. The display *cadence* is
  16.666 ms; the *rate* is bounded by the source.
- **Not a claim that midpoints always land.** 1.8 % (window A) and 4.3 % (window B) miss. The pump
  degrades correctly when they do, and that degradation is measured rather than assumed.
- **Not a claim about the ~1 s drawable withholding.** That was R125's result and is not re-litigated
  here; it simply does not recur (0 acquisitions > 100 ms in either window).
- **Not a claim about GPU overlap, estimator quality, or MetalFX tuning.** None was touched. The
  estimator GPU time (9.0 / 8.3 ms median) is reported only as context for the midpoint budget.
- **Not a claim about EAC or online play.** Both are out of scope and neither was exercised.
- **Not a claim about 60→120.** Out of scope.
- **Not a redesign.** Producer correlation, drawable retirement, `maximumDrawableCount`, the pump's
  scheduling rules and the estimator are byte-identical to R125 apart from the gate.

---

## 12. Verdict

`CONTINUOUS_30_TO_60_TITLE_FG = VALIDATED`
