# R130 — Transactional SOURCE ownership implementation and gate report

## Executive verdicts

- `EXACTLY_ONCE_SOURCE_OWNERSHIP = NOT_VALIDATED — the required 29-case actual-path gate and sustained 80-SOURCE pressure test are incomplete; a direct/direct ordering counterexample is measured.`
- `LIVE_TITLE_GATE = CLOSED — deterministic gate failed/incomplete and the required fresh post-gate independent review has not run.`
- `D3DMETAL_FG_ALPHA_RUNTIME = PARTIAL — ordering under overlapping title acquisitions, full transition/pressure testing, live transitions, failure injection and soak remain unvalidated.`

**Elden Ring was not launched in R130.** No live transition, deliberate INTERNAL-loss run, soak, display-latency distribution or runtime diagnostics-growth measurement was performed. R126's previous `CONTINUOUS_30_TO_60_TITLE_FG = VALIDATED` is the supplied historical verdict, not a new R130 measurement.

This round implements substantial observer and pump changes; they are **experimental and not live-gate validated**. The measurements below deliberately separate actual observer/pump paths with fake platform objects from real Metal, a real drawable pool, and live title behavior.

## Assignments and working tree

The user requested an externally selected root model and GPT-6 Luna xHigh child workers. The root model was not introspected. Three child tasks were explicitly routed to `gpt-6-luna`, `xhigh`: pump implementation/tests, observer analysis and pre-gate adversarial inspection, and the observer integration harness. A later pump task built a separate acquisition-order probe. Runtime model identity beyond requested routing was not exposed. The harness worker later hit a usage limit; the root continued the harness work and ran the subsequent capture-failure and transition tests. These agents are **not** the required fresh independent post-gate reviewer; that reviewer was not launched because the deterministic gate did not pass.

The R130 implementation changed the already-untracked `r127-alpha-runtime` tree:

- `observer-title-inprocess-r127.m`: readiness publication, acquisition tickets, title/FG ownership commit, WSI boundary and disable changes.
- `R123DisplayPump.h`, `R123DisplayPump.m`: reservation, dispatch and retry changes.
- `R127SourceOwnership.h`, `r127_source_ack_selftest.c`, `r123_pump_selftest.m`: bounded ownership lifecycle and focused tests.
- `r130_observer_pre_capture_integration.m`, `build_r130_observer_integration.sh`: actual observer/pump harness with fake platform objects.
- `r130-evidence/acquisition-order/`: separate acquisition-order harness, runner and results; this report and other R130 evidence are in this directory's parent.

The provider files, historical reports and prior R127/R129 tree were already modified or untracked at the start. They were not reset or staged. The runtime tree is untracked, so ordinary `git diff --check` does not inspect its files; build and individual test evidence are listed below.

## Implemented transaction and remaining boundary

The bounded 256-slot ledger records `Unknown`, `Direct`, `Suppressed` (FG committed), `Dispatched` (FG WSI issued) and `TitleDispatched`. It refuses reuse while a different sequence is unresolved and refuses an older sequence from replacing a newer entry. The helper asserts `overwriteNonTerminal == 0`. A terminal slot can be reused. Slot refusal and stale callback semantics still require broader actual-path coverage, particularly when more than 256 title acquisitions remain outstanding or ticket identity is ambiguous.

Successful private-copy completion publishes a single cached private texture with its sequence and the runtime epoch recorded at acquisition. The title hook checks that identity and epoch, current mode and worker state, a sole outstanding acquisition ticket, and the pump reservation. Under the runtime lock it reserves a provisional SOURCE, records FG ownership, commits the reservation and then suppresses the title presentation. An unsuccessful provisional commit is undone before suppression. If these checks fail, the intended result is title-direct dispatch; no waiting for capture occurs in the present hook. Copy failure and late capture do not themselves grant ownership.

The pump counts provisional, committed queued and claimed/recovering SOURCE reservations in `activeSourceReservations`; the configured SOURCE capacity is the existing queue `maxDepth` (8 in the runtime), without increasing it. The pump drops INTERNAL work first and refuses a new reservation before commitment if the bounded limit cannot be met. Claimed SOURCE retains its reservation and payload. A rejected/aborted pre-WSI SOURCE is requeued at the front rather than handed back or counted dropped. Pump tests cover the queue/claim accounting and retry; the full observer capacity-pressure matrix remains incomplete.

The pump enforces monotonic item position when claiming and again at WSI terminalization. The observer allocates an FG WSI identity and marks the pump and ownership ledger dispatched while holding the shared WSI lock, before the original present call. The reservation is released at this dispatch frontier; presented-handler feedback is telemetry and does not grant redispatch authority. The title path now claims once and marks its ledger record under the WSI lock; title WSI identity allocation also occurs under that lock. Physical callback absence did not block the tested OFF-after-dispatch case. Failure and timing around every instruction at the terminal boundary are not yet exhaustively tested.

ON attempts ownership only at a later safe ready/reserved SOURCE. OFF enters source-only drain, and the direct cutover checks zero pending FG dispatches, zero SOURCE reservations and no claimed item; it does not wait for positive `presentedTime`. Existing SOURCE work is intended to survive epoch changes and retry. The earlier reject/hand-back mechanisms are no longer used to return a committed SOURCE to title; the pump's legacy hand-back method returns NO. No full multi-epoch actual-path transition matrix or live transition test has passed.

The acquisition-side fence counts title acquisitions before the original `nextDrawable`. A committed FG SOURCE is permitted only when its ticket is the sole outstanding title acquisition. While an FG dispatch is pending, later acquisitions wait before calling the original acquisition, and wake on the WSI dispatch frontier. Tickets contain non-owning drawable address and drawableID scalars, so they do not strongly retain a title drawable; they also permit lookup after the separate R125 diagnostic registry evicts a record. A missing/ambiguous ticket disables further FG admission. How abandoned/reused tickets behave under all real drawable lifetimes remains unvalidated. This design serializes a new title acquisition behind each committed FG SOURCE WSI dispatch, so the normal observer path cannot build the prior multi-SOURCE queue and may alter the validated continuous cadence. The fence may stall acquisition while a persistent presentation failure remains unresolved. Neither cadence nor stall behavior has been tested in the title.

### Measured ordering blocker

The actual observer acquisition-order harness allows two overlapping title acquisitions, as the current title API does. Both cannot satisfy the sole-ticket condition and therefore correctly take the required DIRECT fallback. Presenting the newer acquired SOURCE first produced:

`acquired=5,6; WSI dispatch order=6,5; both title dispatch counts=1; ambiguity detected only afterward`.

The runner exits 1 (`FAIL-DIRECT-ORDER`): [acquisition-order.log](acquisition-order/acquisition-order.log). It also passes its separate checks that overlapping acquisitions fall back direct and that a pending FG SOURCE blocks a new acquisition until FG WSI, without a physical callback. This is an actual hook/real pump path using fake platform drawables, not a live display measurement.

The measured case makes the remaining constraint concrete: if SOURCE 6 is already acquired and its title present arrives while SOURCE 5 has not yet been presented, strict acquisition-sequence WSI order cannot be restored by immediately dispatching SOURCE 6. Holding/waiting inside that present conflicts with the supplied no-wait/no-held-drawable rule. Serializing every acquisition until the prior title present can itself wait while the caller holds an already-acquired title drawable, including a same-thread acquire-twice-before-present schedule; a timeout cannot safely order a later present. No title-specific assumption excluding that schedule has been established. The current code therefore detects the inversion after dispatch rather than mechanically preventing it. Resolving this requires a clarified supported title acquisition contract or a safe ownership/ordering mechanism that is compatible with those constraints; changing the scheduler or silently relaxing the rule was not attempted.

## Deterministic evidence

Final x86_64 runtime build and existing self-tests: [final-build.log](final-build.log). Diagnostics helper: 37 cases pass; source acknowledgment/ownership helper: 25 pass; pump suite: 10 cases pass. The build produces the observer dylib with compiler warnings, including an existing R116 retain-cycle warning. These are not the 29 integration cases. The observer and pump tested in the acquisition and 80-frame logs have SHA-256 `041b24e99588b1ba636398f577a66369d40124b3d6893f39e31dbe1ab0f6af69` and `fac85900439431ab8ea83786a145eca6bdbf53f5c61b0ca00d75e17519e979a4`, respectively.

The initial frozen R129 actual observer probe reproduced the original present-before-capture failure: TITLE dispatches 0 and accepted FG SOURCE 0: [baseline-pre-capture-failure.log](baseline-pre-capture-failure.log). The updated fake-platform harness exercises the real hooked title present, real pump claim and `r123PresentItem`, with a simulated original WSI call: [transition-path.log](transition-path.log). It passes the individually named direct, ready, late-capture, simulated acquisition/copy/pre-WSI failure and OFF-without-physical-callback scenarios in that log. Fake objects do not prove real GPU capture, drawable lifetime or display behavior. Production `captureRealDrawable` preflight, allocation, and failed blit-completion branches were also exercised using simulated Metal objects; all produced one TITLE WSI and no FG WSI. The continued run also removed a live fake drawable from the R125 correlation registry and observed one TITLE WSI, zero outstanding tickets and no accounting ambiguity; it directly removed the record rather than driving natural registry eviction.

The requested cases are assessed at the specified **actual-path** standard below. `PARTIAL` means relevant evidence exists but does not cover all scheduling/failure conditions named by the case. `NOT RUN` does not imply failure or success.

| # | Required case | Result and scope |
|---|---|---|
| 1 | Capture before present | PARTIAL — fake completed image committed and reached one simulated FG WSI; real copy readiness not tested. |
| 2 | Present before capture | PASS in actual observer/fake-platform probe: one TITLE WSI, no FG reservation. |
| 3 | Capture during ON | PARTIAL — the real observer epoch transition rejects a prior-epoch copy and a later ready frame dispatches via FG; the control-file poll itself was bypassed. |
| 4 | Capture during OFF | PARTIAL — ready capture in an OFF or source-only-drain fixture takes TITLE; a racing OFF control transition was not exercised. |
| 5 | Lookup before transition, commit afterward | PARTIAL — lookup before the real observer epoch transition followed by stale capture and title fallback was exercised; not a concurrent control-file race. |
| 6 | Late capture after TITLE terminal | PASS in fake-platform actual observer probe. |
| 7 | Late capture after FG terminal | PASS in fake-platform actual observer/pump probe. |
| 8 | Capacity available | PARTIAL — actual observer reserves one; complete capacity boundary not exercised there. |
| 9 | Capacity unavailable | PARTIAL — pump-only refusal; actual hook fallback under full protected reservations not proven. |
| 10 | INTERNAL eviction creates room | PARTIAL — pump tests only; not end-to-end. |
| 11 | All capacity protected by SOURCE | PARTIAL — pump tests only; not end-to-end. |
| 12 | Queued + claimed boundary | PARTIAL — pump tests cover accounting; not the complete observer transition. |
| 13 | Preflight failure | PASS through production capture preflight with a simulated framebuffer-only texture, then one TITLE WSI and no FG WSI. |
| 14 | Allocation failure | PASS through production allocation-failure branch with a simulated device returning no texture, then one TITLE WSI and no FG WSI. |
| 15 | Capture copy failure | PASS through production capture-copy completion with a simulated failed command buffer, then one TITLE WSI and no FG WSI. |
| 16 | Epoch mismatch | PARTIAL — actual capture publication with an older supplied epoch is rejected and the title dispatches once; the epoch was set up by the fixture, not raced through acquisition and control. |
| 17 | Pump claim disturbance | PARTIAL — pump-only retry; full observer race not covered. |
| 18 | Presentation drawable acquisition failure | PASS in actual presenter with fake nil acquisition, retry and one FG WSI. |
| 19 | Presentation copy failure | PASS in actual presenter with fake failed command buffer, retry and one FG WSI. |
| 20 | Pre-WSI abort | PASS for fake missing original present IMP, retry and one FG WSI; other aborts not covered. |
| 21 | ON during unresolved capture | PASS for a forced fake-platform transition: the unresolved old frame dispatched TITLE once and a subsequent ready frame dispatched FG once; transition function invoked directly. |
| 22 | OFF with queued FG SOURCE | PASS for a fake-platform queued SOURCE through observer drain and FG WSI; transition function invoked directly and no physical callback. |
| 23 | OFF with claimed FG SOURCE | PASS for a fake-platform claim requeued by observer epoch transition, followed by one FG WSI and no physical callback; not a concurrent GPU-copy/control race. |
| 24 | OFF after dispatch, physically unconfirmed | PASS for fake-platform actual observer with no presented callback. |
| 25 | Repeated ON/OFF epochs | PARTIAL — six direct calls to observer epoch-transition logic keep pump and runtime epochs aligned with an empty queue; no control files or frames during transitions. |
| 26 | Stale callback from previous epoch | PARTIAL — older-epoch capture callback is rejected in an actual observer fixture; the preceding live control transition and stale physical callback were not exercised. |
| 27 | Sustained all-SOURCE pressure | PARTIAL — 80-acquisition backlog ran end-to-end, but only one SOURCE reservation was active and pump capacity pressure was not reached. |
| 28 | INTERNAL pressure | PARTIAL — pump-only coverage, not observer end-to-end. |
| 29 | Capacity refusal before suppression | PARTIAL — pump-only capacity refusal, not actual hook under full SOURCE reservations. |

`overwriteNonTerminal` is zero in the measured helper and sequential mixed-readiness run; this is not a proof of all production interleavings. Pump-focused tests report no committed SOURCE drops and one-time terminal dispatch; the actual-path full matrix and duplicate-path audit have not passed. The pump implements an order guard, but the direct/direct counterexample above means overall order enforcement **FAILS** under the supplied broad ordering criterion.

## 80-SOURCE accounting and pressure limitation

The actual observer/pump fake-platform mixed-readiness run reports: 80 logical SOURCE; 52 TITLE-owned; 28 FG-owned; 52 TITLE WSI; 28 FG WSI; 80 total WSI; duplicates 0; zero-dispatch 0; ordered 1; droppedSource 0; peak reservations 1 of capacity 8. It calls each title hook twice to check the tested duplicate path. These 52 are **not capacity refusals**: their private image was deliberately absent. Each FG source was drained before the next acquisition. Thus this is a useful end-to-end accounting test for that serial schedule, **not** the requested sustained all-SOURCE pressure/overload scenario and not proof that 52 capacity-refused frames dispatch correctly. See [extended-ownership-overload.log](extended-ownership-overload.log) and its source snapshot under `extended-ownership-overload-build/source/`.

A second fake-platform run registers 80 acquisition tickets and completes the 80 capture callbacks before presenting the frames in sequence. It measured 79 TITLE-owned, 1 FG-owned, 80 total WSI calls, zero duplicates, zero zero-dispatch frames, zero dropped SOURCE and ordered dispatch. Peak SOURCE reservations were 1 of 8; no capacity refusal occurred. This exercises acquisition backlog and fallback, not sustained full pump capacity. See [acquisition-backlog-run.log](acquisition-backlog-run.log) and its immutable source snapshot under `acquisition-backlog-build/source/`.

## Required fresh independent review

Not run: the user required a fresh GPT-6 Luna xHigh review **after** all deterministic gates pass. The preliminary analysis agent was used to challenge the evolving implementation and exposed the direct/direct counterexample; it was not that post-gate review. Questions 1–13 therefore have no fresh-review verdict:

| Question | Fresh review verdict |
|---|---|
| 1. Suppression without ready private image? | NOT RUN |
| 2. Suppression without bounded reservation? | NOT RUN |
| 3. Suppression before FG commit? | NOT RUN |
| 4. Committed SOURCE evicted, dropped or handed back? | NOT RUN |
| 5. Zero WSI dispatch possible? | NOT RUN |
| 6. Multiple WSI dispatches possible? | NOT RUN |
| 7. Capture races mutate terminal ownership? | NOT RUN |
| 8. ON/OFF reorders SOURCE? | NOT RUN |
| 9. Capacity includes queued and claimed? | NOT RUN |
| 10. Ambiguous physical feedback causes redispatch? | NOT RUN |
| 11. Order mechanically enforced? | NOT RUN; separate pre-gate direct/direct failure measured. |
| 12. Unresolved ownership record overwritten? | NOT RUN |
| 13. R125/R126 fixes preserved? | NOT RUN |

## Runtime and regressions not validated

Live transition sequence, `RUNTIME_FG_ENABLE_DISABLE`, `FG_FAILURE_FALLBACK`, `D3DMETAL_FG_ALPHA_SOAK`, `FG_DISPLAY_LATENCY`, and `BOUNDED_ALPHA_DIAGNOSTICS` are **NOT RUN**. No median/p90/p99, 10–15 minute soak, stall/deadlock rate, memory-growth or self-capture counters were collected from the title. The R125 non-owning registry and R126 self-present gate remain in the source, and R130 tickets store scalar drawable identity only; those are static observations, not a new live regression validation. R102 deferred-B is explicitly bypassed when the R123 pump is enabled. No claim is made that continuous FG remains performant or even active under ordinary producer/present timing: present-before-copy must now be direct by design.

## Exact remaining blocker and next product step

The deterministic gate is not satisfied: the real observer has a reproducible reverse-order title dispatch with two already-acquired direct sources, and the remaining cases/pressure test above are incomplete. The strict ordering, immediate direct dispatch and no-wait-with-acquired-drawable requirements conflict for the demonstrated schedule unless an earlier safe acquisition/lifetime contract can be established. This is a measured failing schedule, not a claim that Metal or the repository makes every solution impossible. The smallest next step is to establish a supported acquisition ordering/lifetime contract or implement a bounded, non-retaining mechanism that handles that schedule without waiting on an acquired title drawable, then rerun the actual-path acquisition counterexample, all 29 cases and sustained 80-SOURCE pressure test. Only after those pass should a fresh independent Luna review be requested, followed by the specified live and alpha-hardening gates.
