# FG-Metal R131 Report

**Date:** 2026-11-02  
**Scope:** Presentation-order semantics, SOURCE ownership/capacity, and deterministic gate completion.

## Executive verdicts

- `SOURCE_PRESENTATION_ORDER_SEMANTICS = NOT_VALIDATED — WSI issue ordering now follows SourcePresentOrdinal, but estimator adjacency still uses acquisition/capture sequence.`
- `EXACTLY_ONCE_SOURCE_OWNERSHIP = NOT_VALIDATED — all synthetic observer/pump dispatch matrices pass, but the complete product gate is blocked by interpolation adjacency and has not received the required fresh independent review.`
- `LIVE_TITLE_GATE = CLOSED — interpolation adjacency is not presentation-ordered; fresh review is therefore not eligible yet.`
- `D3DMETAL_FG_ALPHA_RUNTIME = PARTIAL — synthetic integration passed; no live title run or alpha hardening was performed.`
- Elden Ring was not launched. No live transitions, soak, latency percentile collection, or bounded-diagnostics run occurred.

## Model and agent assignments

- Requested root and substantive worker configuration: GPT-6 Luna xHigh. Runtime model metadata was not exposed or introspected.
- One early read-only audit worker examined acquisition versus presentation order and the estimator adjacency path.
- No Luna Max escalation was used.
- The required fresh independent reviewer was not launched because the deterministic product gate did not pass the interpolation-adjacency requirement.

## Changed files

R131 changed these files under the untracked R127 runtime/test directory:

- `experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/R127SourceOwnership.h`
- `experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/R123DisplayPump.h`
- `experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/R123DisplayPump.m`
- `experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/observer-title-inprocess-r127.m`
- `experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/r130_observer_pre_capture_integration.m`

The pre-existing worktree edits to `MetalFXFrameInterpolationProvider.h/.m` were not changed. Existing R122–R130 reports and evidence were preserved.

## Order and identity model

**Measured:** Acquisition order is not a valid substitute for application presentation order. The R130 schedule `acquire 105, acquire 106, present 106, present 105` dispatched both TITLE sources exactly once in WSI order `106,105`. Their `SourcePresentOrdinal` values were `1,2`; duplicate, zero-dispatch, ambiguity, and order-violation counts were all zero. See `final-integration-build.log` (`R131_REVERSE_ACQUISITION_PRESENTATION_ORDER`).

**Implementation:** The existing acquisition `sequence` remains the drawable/capture identity and the existing `AppFrameId` value. A separate `sourcePresentOrdinal` is carried through SOURCE pump items and INTERNAL endpoints. DIRECT TITLE ordinals are allocated inside the short WSI dispatch lock. FG ordinals are committed with the bounded SOURCE reservation before title suppression; a later TITLE dispatch waits for earlier committed FG WSI dispatches. `WsiPresentId` remains a separate WSI identity.

**Pump evidence:** The order tracker uses SOURCE presentation ordinals; INTERNAL items carry endpoint ordinals. An invalid SOURCE order remains pending at the pump head in the 10-case pump self-test. The reverse-acquisition test above establishes the direct TITLE path's actual issue order.

**Interpolation blocker (source fact):** `processCapturedFrame` still uses `previousCaptureSequence + 1` for adjacency, including the `hasPrevious` decision. No bounded capture staging/reorder step keyed by acquisition ID and released by presentation ordinal was added. Therefore the required logical `Y → X` estimator pair after acquire `X,Y` / present `Y,X` is not demonstrated. The implication that the estimator may pair in capture/acquisition order is an inference from this code path; no reversed-order MetalFX estimator run was performed.

## Transactional ownership and capacity

The R130 transaction remains three-stage:

1. Before commit, FG requires a ready private image, a matching active acquisition ticket, the current epoch, a bounded reservation, and a successful ownership commit. Otherwise the title WSI path remains available.
2. A committed SOURCE retains its reservation through queued, claimed, retry, and OFF drain states. INTERNAL items remain evictable.
3. WSI dispatch terminalizes the SOURCE independent of a physical-display callback.

Capture readiness now additionally requires a live acquisition ticket. A late callback cannot repopulate cache after the ticket is terminal or after a 256-slot ledger reuse. If an unresolved slot collides, FG admission is refused and the older SOURCE remains protected; the title fallback waits for earlier committed WSI work.

The pressure test held eight FG SOURCE reservations, claimed one while seven remained queued, and kept the total at `8/8`. A ninth ready SOURCE was refused before suppression and stayed TITLE-owned. INTERNAL items were evicted first. The 80-source schedule reached peak reservations `8/8`, had eight capacity refusals, dispatched all 80 exactly once, and recorded zero source drops, duplicates, zero-dispatch sources, order violations, or nonterminal overwrites.

## Deterministic actual-path matrix

“Actual path” here means the production observer hook and production R123 pump ran in the integration executable. Metal devices, command buffers, layers, and drawables were fakes; these are not live D3DMetal/Metal measurements. Every row below is `PASS` for the exercised deterministic schedule.

| # | Case | Result and evidence |
|---:|---|---|
| 1 | Capture before present | **PASS** — `capture-before-present-commits`; one FG WSI dispatch. |
| 2 | Present before capture | **PASS** — `present-before-capture` remained TITLE-owned; late callback after TITLE terminal was rejected. |
| 3 | Capture during ON transition | **PASS** — real ON control poll advanced to epoch 2; a pending epoch-1 callback was rejected and did not populate cache. |
| 4 | Capture during OFF transition | **PASS** — real OFF control poll entered source-only drain; an epoch-2 callback delivered during drain was rejected. |
| 5 | Lookup/transition race | **PASS** — controlled lock schedule made the present decision linearize before a real OFF control poll; committed FG SOURCE drained once. |
| 6 | Late capture after TITLE terminal | **PASS** — cache unchanged; one TITLE WSI, no FG WSI. |
| 7 | Late capture after FG terminal | **PASS** — late callback rejected after FG WSI; no second dispatch. |
| 8 | Capacity available | **PASS** — ready image admitted and FG WSI dispatched once. |
| 9 | Capacity unavailable | **PASS** — at `8/8`, next SOURCE remained TITLE-owned. |
| 10 | INTERNAL eviction | **PASS** — INTERNAL eviction preceded SOURCE refusal; 80-source run recorded eight eviction batches. |
| 11 | All capacity protected by SOURCE | **PASS** — eight committed SOURCE reservations occupied the full capacity; no committed SOURCE was evicted. |
| 12 | Queued plus claimed/in-flight boundary | **PASS** — one claimed plus seven queued remained counted as eight reservations; a ninth was refused. |
| 13 | Preflight failure | **PASS** — real capture path, zero allocation attempts, TITLE WSI once. |
| 14 | Allocation failure | **PASS** — one allocation attempt failed; TITLE WSI once. |
| 15 | Capture-copy failure | **PASS** — failing copy path reached; TITLE WSI once, no FG ownership. |
| 16 | Epoch mismatch | **PASS** — stale callback rejected after runtime epoch changed; no cache admission. |
| 17 | Pump claim disturbance | **PASS** — OFF drain requeued a claimed SOURCE, retained its reservation, then dispatched it once. |
| 18 | Presentation drawable acquisition failure | **PASS** — FG SOURCE retained and retried; one eventual FG WSI. |
| 19 | Presentation-copy failure | **PASS** — pre-WSI retry preserved the SOURCE; one eventual FG WSI. |
| 20 | Pre-WSI abort | **PASS** — abort/retry preserved the SOURCE; one eventual FG WSI. |
| 21 | ON during unresolved capture | **PASS** — the old unresolved callback was rejected across the ON epoch boundary; the next current-epoch image committed FG. |
| 22 | OFF with queued FG SOURCE | **PASS** — real OFF control poll retained queued SOURCE work through drain. |
| 23 | OFF with claimed FG SOURCE | **PASS** — real OFF control poll requeued the claimed SOURCE; queued and claimed sources drained in ordinal order. |
| 24 | OFF after WSI, physical display unconfirmed | **PASS** — OFF was requested after FG WSI with no presented callback; dispatch remained terminal and was not repeated. |
| 25 | Repeated ON/OFF | **PASS** — repeated control-file ON/OFF cycles completed with monotonic epochs and no pending reservations. |
| 26 | Stale callback after ledger reuse | **PASS** — after 300 identities reused the 256-slot ledger, a callback for identity 1 was rejected. |
| 27 | Sustained all-SOURCE pressure | **PASS** — 80 ready SOURCE attempts, peak `8/8`, eight TITLE refusals, 80 total WSI dispatches. |
| 28 | INTERNAL pressure | **PASS** — each of eight full batches evicted INTERNAL work before refusing the ninth SOURCE. |
| 29 | Capacity refusal before suppression | **PASS** — refusal was observed before title suppression; the refused source received one TITLE WSI. |

## Pressure, 80-source accounting, and ledger lifetime

**Nine-source capacity schedule:** `R131_SOURCE_CAPACITY_PRESSURE PASS`, `logical=9`, `peakReservations=8`, `capacity=8`, `queuedAndClaimed=1`, `internalEvictions=6`, `refusedBeforeSuppression=1`, total WSI `9`, duplicates `0`, zero dispatch `0`, committed drops `0`, order violations `0`.

**Sustained 80-source schedule:** `R131_80_SOURCE_CAPACITY_PRESSURE PASS`: `logical=80`, TITLE WSI `8`, FG WSI `72`, capacity refusals `8`, peak `8/8`, INTERNAL eviction batches `8`, total WSI `80`, duplicates `0`, zero dispatch `0`, committed drops `0`, order violations `0`, `overwriteNonTerminal=0`. The log contains one row for each AcquisitionId and SourcePresentOrdinal, each with total WSI count `1`.

**Ledger:** A live unresolved collision at identities `1` and `257` refused the newer FG commit, kept identity 1 protected until its FG WSI, then dispatched identity 257 through TITLE once. The stale callback for 1 was rejected after reuse; `slotRefusals=2`, `overwriteNonTerminal=0`. A separate 300-identity hook run demonstrated terminal slot reuse and monotonic ordinals with zero order violations and zero outstanding tickets.

## Exactly-once and regression status

**Measured:** The 9-source pressure, 80-source pressure, reverse-order schedule, unresolved-slot collision, and 300-identity ledger run all observed exactly one SOURCE WSI per logical SOURCE in their schedules.

**R125/R126:** Source review confirms the R125 correlation registry remains scalar/non-owning and reports `retainedSourceDrawables=0`; the R126 self-present completion gate still returns before SOURCE capture and counts ignored self-present completions. These files compiled in the final builds. No live title regression run was performed for R125/R126.

**R102:** R131 did not arm or alter the deferred-B path. It remains outside the active R123 pump path in these harness runs; no independent R102 runtime test was performed.

## Build and evidence

- `final-unit-build.log`: `build_r127_tools.sh` exited 0; latency/source-ack self-tests passed and `r123_pump_selftest` reported `SELFTEST=PASS cases=10`.
- `final-integration-build.log`: `build_r130_observer_integration.sh` exited 0; actual observer/pump matrix, reverse-order, control-transition, pressure, and ledger tests all passed.
- `final-integration-build/source-sha256.txt`: immutable source snapshot hashes for the final integration executable.
- `baseline-build.log` and `baseline/`: pre-R131 observer baseline.
- `git diff --check` passed.
- Compiler emitted the existing strong-capture/retain-cycle warning in the presented-handler path; the build had no errors.

## Independent review and live hardening

The final independent review was not launched because presentation/interpolation semantics are not fully validated. This is a gate dependency, not a reviewer finding. No Elden Ring process was started. Live OFF/ON transitions, title soak, fault-loss file injection, latency median/p90/p99, and bounded-diagnostics behavior were not tested.

## Remaining blocker and smallest next step

**Blocker:** Estimator pairing still follows `previousCaptureSequence + 1` instead of `SourcePresentOrdinal`; WSI ordering alone does not establish correct interpolation adjacency under reversed acquisition/presentation.

**Smallest next product step:** Add bounded capture staging keyed by AcquisitionId until its SourcePresentOrdinal is assigned, then submit completed captures to estimator history in ordinal order. Add a reversed acquire/present integration test that proves the estimator receives `Y → X`, then rerun the deterministic gate and obtain the required fresh independent review before any live title run.
