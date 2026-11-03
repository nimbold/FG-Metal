# FG-Metal R128: SOURCE ownership planner handoff

Date: 2026-11-02  
Repository: FG-Metal (local working tree)

## Current verdict and safety gate

```text
EXACTLY_ONCE_SOURCE_OWNERSHIP = NOT_VALIDATED
D3DMETAL_FG_ALPHA_RUNTIME = PARTIAL
LIVE_TITLE_GATE = CLOSED
```

The title present hook can still suppress a SOURCE before a ready private image and bounded pump admission have been committed. A committed SOURCE can also be dropped after suppression. Do not launch Elden Ring for R128 validation until the complete deterministic safety gate and fresh independent review requested in the original brief pass. No R128 live transition test, fault-injection run, soak, or latency validation was performed.

The full user brief is a local attachment retained offline (not part of this repository). Its requirements, including the exact test cases, live sequence, milestones, and frozen contracts, remain authoritative. R126 continuous 30-to-60 title FG is the prior validated baseline; this handoff does not revalidate it.

## Worktree and what changed

The R127 experimental directory `experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/` was already untracked at the start of R128 work. The provider `.h` and `.m` were already modified, and the R122–R126 reports were already untracked. Preserve all pre-existing work. Because the R127 directory is untracked, ordinary `git diff` does not show its contents or the changes made within it; inspect the files directly.

The R128 attempt changed only these files inside that directory:

- `R123DisplayPump.m` and `.h`: queue admission refuses a new SOURCE without incrementing accepted/dropped accounting; if one slot remains, it can admit SOURCE without the optional INTERNAL placeholder. `setRuntimeEnabled:` and `endSourceOnlyEpochForTitleDirect:` refuse to clear queued SOURCE items, and head requeue is allowed in an enabled current epoch, not only in source-only mode.
- `r123_pump_selftest.m`: Case E accounts for refused offers separately; Cases G and H check the new queued-SOURCE drain behavior and head requeue.

These are **partial, experimental pump changes**, not an integrated ownership fix. In particular, the observer still follows R127 semantics. The pump's transition refusal needs an explicit observer-side protocol: `r127AdvanceRuntimeEpochLocked` and `r127FinishDisableToTitleLocked` currently advance mode/epoch before, or without verifying acceptance of, the pump transition. A refusal can therefore leave observer and pump state inconsistent. The pump's claimed item is outside queue depth and the existing H2 test permits a transient `maxDepth + 1` requeue; the planner must account for in-flight capacity instead of treating this as proof of the requested bound. The old handback and reject APIs also remain available and can discard ownership in the existing observer.

An unfinished observer patch was attempted and then removed. It used a physical-ack wait after title drawable acquisition; that can deadlock if the title holds the last available drawable while an older FG SOURCE needs a fresh drawable, or if display feedback never arrives. The observer was restored to its pre-attempt R127 state. Do not assume the abandoned patch is present or validated.

## Source evidence: unresolved defects

Paths below are relative to `experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/`; line numbers reflect this handoff's worktree and may move.

1. **Suppression precedes ownership:** `observer-title-inprocess-r127.m:4180` (`r123TrySuppressSourcePresent`) records `Suppressed` using runtime mode and worker readiness, before confirming a ready image and successful pump admission. It can return YES even if no SOURCE is queued.
2. **Capture/present race:** `observer-title-inprocess-r127.m:3485` (`r123EnqueueSource`) snapshots owner separately from runtime state. Capture while ownership is unknown can be neither retained for the hook nor enqueued; a later hook can still suppress. Capture preflight, allocation, or copy can fail before any usable private texture exists, including around lines 10045, 10091, and 10148.
3. **Pre-WSI failure loses committed SOURCE:** `observer-title-inprocess-r127.m:2259` calls `rejectUndispatchedItem:` through `r127AbandonPumpItem`; presentation failures call it. `R123DisplayPump.m:258` counts a rejected SOURCE as dropped rather than preserving its private texture for retry.
4. **Transitions can lose or reorder:** inspect `observer-title-inprocess-r127.m:3672`, `:3702`, and `:3995` together with the pump transition APIs. Older claimed, queued, dispatched, and physically unacknowledged SOURCE work must be settled before direct ownership of a newer SOURCE can safely pass it. Current epoch transitions and handback paths do not establish that proof.
5. **Ownership ring lifetime:** `R127SourceOwnership.h` uses a 256-entry ring and can overwrite an entry without proving it terminal. Delayed work and stale epochs need a bounded, explicit lifetime rule.
6. **Dispatch is not physical display:** the WSI present call returns before physical feedback; the callback advances the source acknowledgement only for positive `presentedTime` (`observer-title-inprocess-r127.m:2195`). Missing or zero-time feedback cannot be treated as successful display or safely replayed without a proof that duplication is impossible. Define the exact recovery and liveness assumptions and keep unresolved work visible.
7. **Order tracking is diagnostic:** `R123OrderTrackerObserve` reports a violation, but the pump's take path does not use its result to prevent dispatch. A passing counter is not a mechanism that enforces order.

Independent read-only analyses confirmed these defects. They were reviews of the R127/partial pump state, **not** the required fresh review of a completed R128 implementation.

## Verification performed

The build script was run from the repository root:

```sh
./experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/build_r127_tools.sh
```

It completed successfully, including the R127 diagnostics and ownership-helper tests, the pump self-test, and building the x86_64 observer dylib. The compiler emitted nine warnings, including an ARC retain-cycle warning in an existing deferred-ticket path and unused declarations. The captured output was `/tmp/fgmetal-r128-build.log` at handoff time; `/tmp` is ephemeral.

The synthetic pump overload Case E reported:

```text
80 SOURCE offers: 28 accepted and consumed by the synthetic pump, 52 refused
droppedSource = 0 for accepted pump items
maximum observed queue depth = 4
pump order violations = 0
```

This is **not** an end-to-end zero-loss or exactly-once result. The test does not display the 52 refused offers through the title, enforce their ordering against older FG work, inject pre-WSI failures, or prove physical display. It also does not account for the separate in-flight work bound in the full runtime. The old baseline test expected dropped SOURCE under overload; the revised test measures pre-acceptance refusal, not successful fallback.

The R126 self-present queue gate remains visible at `observer-title-inprocess-r127.m:11253`, and the R125 scalar-only display correlation comments and callback are present. Their full regression gates, R102 disabled state, and actual runtime behavior have not been revalidated in R128.

## Planning priorities and acceptance criteria

Plan the smallest bounded integrated handoff, with implementation and tests tied to the actual runtime path:

1. Separate per-SOURCE image readiness from ownership. Commit FG ownership and a bounded queue claim atomically before returning from the title hook without original presentation. Before commit, any failure or lack of capacity leaves TITLE responsible. A late capture or old epoch must not change a terminal decision.
2. Count queued and in-flight claims in the capacity invariant. Make accepted SOURCE impossible to evict or refuse later; discard optional INTERNAL work first. Reconcile the current partial pump changes and all observer callers, including transition failure reporting.
3. Preserve a committed private SOURCE through pre-WSI failures, with ordered head retry or an equivalent bounded recovery. Clarify what constitutes a definitive failed dispatch versus ambiguous/missing display feedback, and how physical exactly-once and liveness are established under the actual platform semantics.
4. Design an ordering boundary for direct fallback and OFF that cannot overtake older FG-owned work and does not deadlock by holding title drawables needed by the pump. Consider whether gating before title drawable acquisition is feasible, but verify it against actual hook ordering, pacing, callbacks, and drawable-pool behavior; it is an idea, not an established solution.
5. Add deterministic **integration** tests for all ON/capture/hook orderings, capacity refusal, capture/preflight/copy failures, post-commit pre-WSI failure, all-SOURCE pressure, OFF with outstanding ownership, stale callbacks, and exactly-once terminal accounting. Tests must prove actual DIRECT or FG dispatch/display behavior and ordering, not just count refusal or report `sourceContinuity=UNRECOVERED`.
6. Preserve R125 non-owning drawable instrumentation, R126 self-present queue attribution, and disabled R102 deferred-B. Do not redesign the frozen estimator, MetalFX, pacing, drawable retirement/pool, DXMT, or 60-to-120 work. EAC/online is out of scope.
7. Only after the static/synthetic gate passes, obtain a fresh independent GPT-6 Luna xHigh review answering all 12 questions in the brief. Fix any item 1–10 failure before live testing. Then run `OFF → ON → OFF → ON → OFF → ON`, and only after that passes continue in the same run to bounded INTERNAL loss/recovery, the 10–15 minute title soak, latency percentiles, and bounded diagnostics.

The planner should explicitly distinguish a guaranteed single **dispatch** from a guaranteed single **physical display**, and should state what happens when the platform never provides conclusive display feedback. Neither retrying an ambiguous dispatch nor allowing later DIRECT presentation to overtake an unresolved SOURCE is proven safe by the current code.
