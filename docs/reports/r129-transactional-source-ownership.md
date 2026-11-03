# R129 — Transactional SOURCE ownership report

Date: 2026-11-02. Repository: FG-Metal (local working tree).

## Executive verdict

```text
EXACTLY_ONCE_SOURCE_OWNERSHIP = NOT_VALIDATED — the observer suppresses title presentation before a ready image and bounded FG admission commit, and committed work can be dropped before WSI dispatch.
LIVE_TITLE_GATE = CLOSED
D3DMETAL_FG_ALPHA_RUNTIME = PARTIAL — transactional SOURCE ownership and its deterministic safety gate remain unimplemented and unvalidated.
```

No Elden Ring session was launched. The required live transition test, failure injection, soak, latency, and diagnostics validation did not run. R126's earlier continuous 30-to-60 result is historical evidence, not a new R129 validation.

## Work and evidence

No runtime implementation or test source was changed in this round. `r127-alpha-runtime/build_r127_tools.sh` now accepts `FGMETAL_R127_BUILD_DIR` so future builds can isolate generated evidence; its default remains unchanged. A second build using the R129 directory passed; see [isolated-build.log](isolated-build.log). The working tree already contained modified provider files, untracked R122–R126 reports, the R128 handoff, and the untracked R127 runtime tree. Three work streams examined implementation, tests, and the acquisition lifecycle; none is the required fresh post-gate review. A draft reservation protocol and pump changes were explored but not integrated or tested; they were removed, and the pump header, implementation, and observer were verified byte-identical to the R129 isolated baseline copies. An unexecuted draft test harness was also removed. The current observer and pump were inspected directly, and the existing build and self-tests were run. The captured output is [baseline-build.log](baseline-build.log). The build completed with nine compiler warnings, including an existing retain-cycle warning, and built the x86_64 observer. R127 diagnostics self-test: 37 cases pass; source fence and ownership helper: 20 cases pass; pump: nine cases pass. These are baseline tests, not the R129 integration gate.

**Evidence isolation exception:** `build_r127_tools.sh` has a hard-coded output path under `experiments/metalfx_frame_interpolator_R93/evidence/r127-alpha-runtime`. Running it refreshed copied sources, executables, and the generated dylib in that existing R127 evidence directory. Their prior contents were not captured before the build, so historical generated artifacts there cannot be claimed unchanged or restored from this run. The new log and this report are in the separate `r129-evidence` directory. No R122–R128 report was overwritten. Future runs can set `FGMETAL_R127_BUILD_DIR` to a round-specific directory before execution. The second build used that override successfully.

## Current ownership contract and defects

There is **no implemented R129 transactional lifecycle**. `R127SourceOwnership.h` stores UNKNOWN, DIRECT, or SUPPRESSED in a 256-slot modulo table. It has no reserved, committed, or dispatched state and overwrites a different sequence without checking terminality. `r123TrySuppressSourcePresent` in `r127-alpha-runtime/observer-title-inprocess-r127.m` records SUPPRESSED based on mode and worker readiness, before confirming private-image readiness or admission. It may suppress without an enqueue. Private capture is produced asynchronously after producer completion and only reaches `r123EnqueueSource` after a successful private copy. Preflight, allocation, copy, worker startup, capacity, epoch, and ordering failures can therefore leave a suppressed SOURCE without a dispatch. The ownership lookup in enqueue is separate from the later runtime lock.

`r127AbandonPumpItem` calls `rejectUndispatchedItem:` on pre-WSI failures; the pump increments `droppedSource`. Transition code also uses reject and `handBackUndispatchedSourceToTitle:` even though a suppressed title drawable has already been released. Observer mode and epoch advance before checking whether a pump transition succeeds. These paths violate the required post-commit ownership and transition contract. They have not been redesigned here.

The WSI present call has an identity allocated shortly before dispatch, but there is no per-SOURCE atomic dispatched terminal state. Positive `presentedTime` updates display acknowledgement; missing or zero feedback is not proof that dispatch did not happen. The current disable path waits on the physical acknowledgement frontier and does not establish the required bounded, non-deadlocking policy. No claim is made that missing feedback causes a replay in the present code; the requested independent dispatch-terminal proof is absent.

The intended contract remains: before suppression, atomically establish ready private image, bounded SOURCE reservation, and FG ownership; any pre-commit failure leaves TITLE to dispatch. After FG commit, retain image and ordered reservation through pre-WSI failures and retry only before dispatch. Allocate identity and terminalize dispatch immediately before issuing WSI once; later physical feedback only updates separate telemetry. A title-direct fallback or OFF transition must not overtake older committed FG work. This is a target, not implemented behavior.

## Capacity and ordering

The production pump is initialized with `kR123MaxDepth = 8`, a queue-depth limit shared with INTERNAL items, not a proven SOURCE reservation bound. Claimed items leave the linked queue and are held separately by the observer; admission does not count them. Requeue explicitly allows one claimed SOURCE back when the queue is full. Pump self-test H2 deliberately reaches depth 4 with configured maxDepth 3. The maximum queued-plus-in-flight SOURCE reservations in a production run was not measured. Optional INTERNAL placeholders can be evicted first, but accepted SOURCE can still be rejected/dropped or handed back through the paths above. The order tracker records violations; its boolean result is not used to prevent the pump take/dispatch. There is no demonstrated cross-owner enforcement for TITLE fallback against earlier FG work.

The title `nextDrawable` hook invokes the original acquisition before the present hooks run. The latter therefore hold a title drawable when deciding suppression. Waiting there for an older FG acquisition risks drawable-pool exhaustion. Pre-acquisition gating is a possible design direction, not an established solution: sequence allocation, concurrent acquisitions, R120 pacing and title producer/commit order must be tested. A still-live acquired drawable may never present, and the current hooks provide no definitive abandonment signal. Timing it out can let a delayed present overtake newer work; waiting with a later drawable can exhaust the pool. A bounded speculative lease associated without retaining the drawable, released on nil acquisition or proven object deallocation, is a hypothesis requiring retention and concurrency tests. An indefinitely retained drawable can exhaust such leases, so its liveness assumptions remain unresolved. The existing eight-entry scalar correlation registry is evictable and cannot itself prove lease lifetime.

## Required deterministic matrix

**NOT RUN / NOT IMPLEMENTED** below means no actual observer + pump integration test establishes the specified exactly-once outcome. Passing pump/helper cases do not substitute for it. No per-logical-SOURCE end-to-end `titleDispatchCount + fgDispatchCount == 1` assertion was established for these cases.

| Required case | Result |
|---|---|
| 1 capture before present; 2 present before capture; 3 capture/ON race; 4 capture/OFF race | NOT RUN — observer integration harness absent |
| 5 lookup before transition and commit afterward; 6 late capture after TITLE terminal; 7 late capture after FG terminal | NOT RUN — terminal lifecycle absent |
| 8 FG capacity available; 9 capacity unavailable; 10 INTERNAL eviction; 11 all SOURCE protected; 12 claimed plus queued boundary | NOT RUN end-to-end; baseline pump pressure cases exist, and H2 demonstrates the bound is insufficient |
| 13 preflight failure; 14 allocation failure; 15 copy failure; 16 epoch mismatch | NOT RUN end-to-end; current suppression can precede these outcomes |
| 17 pump claim failure; 18 drawable failure; 19 presentation copy failure; 20 pre-WSI abort | NOT RUN for ordered preserved retry; source code currently rejects SOURCE on several of these paths |
| 21 ON during unresolved capture; 22 OFF queued; 23 OFF claimed; 24 OFF dispatched but unconfirmed; 25 repeated ON/OFF; 26 stale callback | NOT RUN end-to-end; pump-only drain tests do not prove observer ownership or title dispatch |
| 27 sustained SOURCE pressure; 28 INTERNAL pressure; 29 capacity refusal before suppression | NOT RUN end-to-end; pump-only overload is described below |
| Ownership-record lifetime; exactly-once WSI terminality; order enforcement; epoch terminal immutability | FAIL static safety gate / NOT VALIDATED by integration tests |

## Synthetic overload

Existing pump-only Case E offers 80 SOURCE items: 28 accepted and consumed, 52 refused, zero accepted-source drops, zero pump-tracker violations, and maximum queue depth 4 under its configured depth of 4. Three INTERNAL items were dropped. TITLE fallback dispatches: **not modeled / unknown**. FG WSI dispatches: **not measured**; the synthetic pump consumption count is not an actual WSI dispatch count. End-to-end drops, duplicates, ordering, and total reservations including claims: **unknown**. The 52 refusals have not been proven to produce 52 ordered title dispatches. Therefore the required overload gate fails despite the pump-only self-test passing.

## Independent review

A fresh independent GPT-6 Luna xHigh review of a completed implementation was not launched because the deterministic gate did not pass and there is no completed implementation to review. The independent analyses in this run are diagnostic, not that review. Required questions remain as follows:

| Review question | Status |
|---|---|
| 1 Suppression before ready image, capacity and commit? | FAIL — possible |
| 2 Can committed FG SOURCE be evicted? | FAIL — reject/drop paths exist |
| 3 Can committed FG SOURCE be refused later? | FAIL — post-suppression enqueue can refuse |
| 4 Can a SOURCE receive zero WSI dispatches? | FAIL — suppression followed by copy/admission/presentation failure permits it |
| 5 Can a SOURCE receive two WSI dispatches? | NOT VALIDATED — no per-SOURCE dispatch terminal proof |
| 6 Can capture/present races alter terminal ownership? | FAIL — stale lookup and overwriteable record; terminal lifecycle absent |
| 7 Can ON/OFF reorder SOURCE? | FAIL — ordering across owners is not enforced and transition acceptance is not atomic |
| 8 Can stale epoch callbacks mutate current ownership? | NOT VALIDATED — bounded record lifetime and stale-callback proof absent |
| 9 Does capacity include claimed/in-flight SOURCE? | FAIL — queue depth excludes claim |
| 10 Can missing display feedback cause duplicate redispatch? | NOT VALIDATED — no dispatch terminality proof; no claim of an observed replay |
| 11 Is order enforced mechanically? | FAIL — tracker result is diagnostic |
| 12 Are R125/R126 correctness fixes preserved? | Present in source on inspection; full regression NOT RUN |

## Live and alpha continuation

The `OFF → ON → OFF → ON → OFF → ON` title test did not run. No live ownership counts, unresolved count, stale callback count, queue maxima, self-capture, drawable retention, or deadlock evidence was collected. Failure injection, 10–15 minute soak, SOURCE/INTERNAL cadence and accounting, midpoint misses, memory growth, latency median/p90/p99, and runtime log-growth validation likewise did not run. Existing diagnostics helper tests only exercise their local math and policy; they do not validate runtime alpha diagnostics.

## Regression status

R125 non-owning registry instrumentation and the R126 queue-origin self-present gate remain visible in current source; no live regression check was performed. R120 acquisition pacing was not intentionally modified or revalidated. The prior R126 report records R102 deferred-B disabled in its measured run; it was not revalidated here. `maximumDrawableCount` is read for diagnostics in the inspected acquisition path; no new pool change was made or measured. The estimator, MetalFX and provider files were not edited in this round, although the provider files were already modified on entry. Historical report files were not overwritten; the generated R127 evidence exception is described above.

## Remaining blocker and smallest next step

**Proven product blocker:** there is no integrated transactional handoff that binds image readiness, bounded queued-plus-claimed capacity, ownership and ordered single WSI dispatch before title suppression. The individual unsafe paths above are evidence of that missing contract. A pre-acquisition boundary and non-retaining drawable lease are design hypotheses whose interaction with pacing, concurrent acquisition, late presentation, deallocation and bounded liveness requires deterministic validation. The inability to prove that lifecycle in the current hooks is an unresolved engineering issue, not proof that a solution is impossible. Performance tuning is future work.

Implement the bounded observer/pump ownership transaction together with an actual-path deterministic harness, starting at the acquisition/present boundary and preserving ordered committed SOURCE recovery. Run all 29 required cases and end-to-end overload accounting, then obtain the specified fresh independent review before opening the live-title gate.
