# Step 11 Closeout Reconciliation — 2026-10-15

## Decision

**Step 11 closeout: CONDITIONAL.** The BR9R engineering investigation is closed/deferred by diminishing returns, with its scientific result unchanged at **BR9R-N3 — INCONCLUSIVE**. H6-A and H6-B are READY, and the fresh lifecycle-bound Wine authorization passed. Replay 1 stopped at fresh storage admission before launch; it produced no replay PRE, Finder action, detector result, treatment, restoration, or causal update. Preserve **R5 — INVALID / INCOMPLETE**, **T8 — INSUFFICIENT TIMING EVIDENCE**, and replay 2 ineligible/not run.

**11E_READY = NO. 11E_EXECUTION = NOT RUN.** The public Framegen API and Metal backend accept and retain previous/current GPU textures, and the accepted R5 path proves a single-source Vulkan→Metal→Vulkan job. The accepted R5 integrated DXVK/provider source is not present as buildable source in this checkout, and the retained single-source bridge does not establish app-backbuffer identity, cross-frame A/B retention, two-source reset handling, or a closed release ledger. Step 11E cannot be represented as a valid integrated A/B run until those dependencies are implemented and bound to exact build/runtime identities and the target-resolution scope is explicitly settled. A diagnostic-size probe may provide partial bridge evidence, but cannot count as a valid/completed 11E run or change readiness.

This reconciliation uses the current H6 facts supplied for this task, the repository’s latest BR9R2 append and retained reports, and the three read-only audits in [A — roadmap](a-roadmap.md), [B — invariants](b-invariants.md), and [C — 11E plan](c-11e-plan.md). This record does not alter historical classifications or the H6 storage policy.

## Current status and evidence boundary

- **H6:** H6-A READY; H6-B READY; environment guard unchanged; H3/H4/H5 remain closed; correction-2 remains immutable. Fresh Gate 0 and the fresh lifecycle authorization passed. The latter used `step11d3b-smoke-20261015-084645-a9298721`, PID `44601`, public CGWindow `3314`, a 32.110 s qualifying uncovered streak, `HEALTHY_REFRESH`, and approximately 59.847 displayed FPS.
- **Replay 1:** admission stopped before launch because projected scope was 59,075,344,794 bytes, 19,544,474 bytes above the unchanged 55-GiB ceiling. No replay directory, replay PRE, Finder action, detector, treatment, or restoration followed. No replay 2. Do not weaken storage policy or remove evidence to make room.
- **BR9R scientific status:** **N3 — INCONCLUSIVE.** The exact external inducer and cross-backend causal question remain unresolved.
- **BR9R engineering status:** **INVESTIGATION CLOSED / DEFERRED BY DIMINISHING RETURNS.** No further dedicated Finder replay or Native differential is justified before the frame-generation transport work.
- **Source/build boundary:** this checkout tracks only `adapters/dxvk/.gitkeep`; R5 reports, binaries, and run evidence are retained, but the integrated DXVK capture/provider implementation is not available here as a buildable source tree. The standalone native bridge proof in the historical sibling checkout is not an integrated DXVK/AppFrame path and must not be treated as one.
- **Implementation-owner result:** the single authorized implementation owner independently confirmed that `adapters/dxvk/` contains only `.gitkeep`, the in-tree R5 directory contains reports/evidence rather than bridge/provider source, and the public FrameSubmission API is not wired to DXVK/AppFrame/flip backbuffers. The preserved diagnostic manifest explicitly does not assert source/configuration completeness or claim rebuildability. No code changes, tests, Wine launch, or runtime experiments were made. The exact pinned integrated source/build inputs listed below are required to resume safely.
- **Resolution/performance boundary:** the project has no declared Step 11E product resolution matrix or pass thresholds. Historical 64×64 bridge and 640×360 controlled-app runs are diagnostic sizes, not product targets. Report measurements without importing old single-source WSI thresholds or inventing a new performance PASS line.

## Reconciled unfinished-work matrix

| Item | Classification | Disposition |
|---|---|---|
| Step 11C.1R2 WSI cadence, lifecycle, failure isolation, and soak gates | **SUPERSEDED** | The later C.1R2 report is FULL PASS for these gates. Do not repeat them before 11E. It does not prove Metal A/B slot ownership. |
| Step 11D.1 single-source Vulkan→Metal→Vulkan | **PROVEN BASELINE** | Accepted as GPU-only transport with GPU synchronization and Vulkan output consumption for the captured single-source workload. It does not authorize an A/B claim. |
| AppFrame-to-real flip backbuffer/allocation identity under `FLIP_SEQUENTIAL`, `BufferCount=2` | **ESSENTIAL / UNPROVEN** | The app-visible accounting passed, but the retained trace had zero backbuffer-rotation events and did not prove the two-slot allocation exchange. Bind the copied source to its actual AppFrame and allocation before naming A/B consecutive. |
| Cross-frame A/B lifetime and output ownership | **ESSENTIAL / UNPROVEN** | The existing provider job takes one input; its per-job HISTORY/OUTPUT pair does not prove A remains immutable and retained while B’s job reads both. Keep both inputs through Metal completion and OUTPUT through Vulkan consumption. |
| Two-source reset/discontinuity rejection and complete drain | **ESSENTIAL / UNPROVEN** | Reset while work is outstanding must invalidate old history, reject stale-generation pairs/consumes, drain submitted work safely, and end with a bounded, closed release ledger. Existing generic WSI shutdown evidence is not a substitute. |
| R5 cadence and source-Present latency tails | **OPTIONAL / DIAGNOSTIC** | Measure and report them in 11E, including long tails and drops. They are not a separate entry gate unless they invalidate pairing, synchronization, bounded lifetime, or measurement validity. Do not reopen AppKit/Finder archaeology just to explain them. |
| Generic D1 failure matrix beyond paths exercised by 11E | **OPTIONAL / DIAGNOSTIC** | Do not require every unrelated failpoint before starting. Any failure on the chosen A/B path must fail closed and retire safely. |
| D3D9 Metal-interoperability parity | **DEFERRED** | The current accepted interop path is D3D11. Do not generalize its results to D3D9 or block the D3D11 transport experiment on D3D9 parity. |
| Step 11D.3-A Native baseline | **COMPLETE / SUPERSEDES EARLIER A ATTEMPT** | Preserve CASE A2 — DIVERGENT / HEALTHY NATIVE BASELINE. No new Native run is needed for 11E. |
| Step 11D.3-B BR8/BR9R causal branch; Finder replays; Native BR9R differential; H7/H8 | **DEFERRED** | Keep historical evidence and N3/R5/T8 labels. No replay, H7/H8, Native differential, storage-policy relaxation, or automatic 11D.3-C run. |
| Step 11D.3-C | **DEFERRED** | The project only marks it gated in the Native/Wine diagnostic branch and defines no independent acceptance criteria or 11E invariant. It is not architecturally required before 11E. Revisit only if later 11E evidence makes that diagnosis decision-relevant. |
| MetalFX, GPTK/D3DMetal reintegration, RIFE/models, quality/backend selection | **DEFERRED UNTIL STRATEGY REVIEW** | Do not start them here. The target remains modern Apple Silicon M-series; DXVK-MacOS remains the research/reference backend through 11E. |

## Answers to the orchestration questions

### 1. Exact remaining Step 11 critical path

Recover or otherwise make available the pinned, buildable R5 integrated DXVK capture/provider source and build inputs. Then prove the exact D3D11 AppFrame-to-backbuffer mapping and two-slot flip rotation; implement the narrow A/B packet and shared-history retain/release rule; reject cross-generation or discontinuous pairs; and close the exercised job/source/output ledger at drain. Bind the source, binaries, runtime, device, and resolution to the run. After these preconditions, run one bounded real A/B Vulkan→Metal→Vulkan workload and measure its transport/performance fields. Review the evidence and stop at the backend/provider strategy gate.

### 2. Is Step 11D.3-C essential before 11E?

**No.** Current project materials define it only as a gated Native/Wine/BR9R diagnostic branch and provide no acceptance contract tying it to A/B history, GPU synchronization, output ownership, or presentation accounting. H6 closes that branch for active engineering work. The 11E objective can be reached without it.

### 3. Which BR9R/Native tasks are deferred?

Further Finder replay, replay 2, H7/H8, Native BR9R foreground/cover differential, dedicated coverer fixes, external-inducer search, and 11D.3-C are deferred. Keep BR9R scientific N3 and R5/T8 as recorded. The already-completed Native A2 baseline remains available as reference evidence.

### 4. Which cadence/lifetime gates remain?

No C.1R2 cadence/lifecycle gate needs repeating. The 11E-specific gates still required are: true consecutive source allocation identity, A retained through B’s Metal read, distinct output retained until Vulkan consume, GPU-side READY/DONE ordering for both inputs, reset/discontinuity stale-pair rejection, bounded in-flight resources, one terminal/release for each exercised source/output/job, and zero outstanding ownership after normal drain. Measure current cadence and latency tails; no old threshold is a new 11E performance threshold.

### 5. Can 11E begin with the current architecture?

**The public Framegen API can express an ordered previous/current texture pair, so bounded design and implementation work can begin. A valid integrated 11E run cannot begin or pass with the current checked-in R5 source/build state.** The current root lacks the buildable integrated DXVK/provider source path, the retained bridge accepts one input per job, flip backbuffer identity is unproven, and no project-owned real-resolution matrix exists. Do not substitute a standalone bridge probe or the 64×64/640×360 historical diagnostics for integrated real-source evidence. Even an integrated diagnostic-resolution probe is partial only and cannot complete 11E or change readiness until its resolution scope is explicitly approved.

### 6. What is the smallest implementation required?

This is a scoped adapter/bridge change, not a new algorithm or broad WSI rewrite:

1. Restore/pin the exact integrated source/build inputs and add traceable AppFrame→backbuffer/history allocation identity for the actual two-buffer swapchain.
2. Extend the current provider job contract to carry two distinct source handles and IDs, keep both GPU resources immutable/alive through Metal completion, and keep a distinct output alive through the existing Vulkan INTERNAL consumer.
3. Add generation/sequence/clock validation that seeds after startup/reset, forms only an eligible same-generation consecutive pair, and clears history on resize/recreate or discontinuity.
4. Close the exercised path’s completion/reclaim/release ledger and assert bounded in-flight resources and zero ownership after drain; retain the existing no-readback GPU event path.
5. Add deterministic focused tests for pair eligibility, reset/stale rejection, and exactly-once lease retirement. Do not broaden into the unrelated D1 failure matrix.

The implementation-owner check found no safe active source/build input to patch. No minimal code patch can be made in this checkout without reconstructing or copying an unrelated historical integration; obtain the pinned source inputs before code implementation. Do not claim partial harness work as 11E.

### 7. What evidence would actually change backend strategy?

One trustworthy, source-bound A/B run can change the strategy discussion if it shows actual app backbuffer allocations forming the intended consecutive pair; no premature overwrite; Metal reading both sources and producing a separate output; READY/DONE/CONSUMED ordering on GPU; correct Vulkan INTERNAL consumption without an extra app-visible Present; reset rejecting stale history; bounded release and clean drain; no CPU pixel readback; and complete per-stage GPU, handoff, latency, CPU, memory, drop, and reset measurements at the declared target resolution(s). An explicit diagnostic resolution may produce partial transport evidence, but absent an agreed target-resolution scope it cannot count as a completed 11E run or change readiness. Strategy can then compare DXVK→Apple-native provider, GPTK/D3DMetal→Apple frame generation/MetalFX, and renderer-independent providers. It cannot decide image quality or infer performance outside measured resolution/cadence.

## Required before an 11E run

- Pinned, buildable integrated R5 DXVK capture/provider source, bridge, build recipe, app fixture, and runtime identity available in the active checkout or a safely isolated owned worktree.
- Trace proving the actual FLIP_SEQUENTIAL two-buffer allocation rotation and AppFrame-to-copied-source association.
- Implemented two-source bridge path with explicit A/B IDs and GPU lifetime through completion.
- Forced reset/recreation/discontinuity with outstanding work; proof old-generation pairs and consumes are rejected and resources later retire safely.
- Complete exercised-path release ledger; bounded resource high-water; clean drain to zero ownership.
- Owner-specified target resolution scope before any run can qualify as completed 11E. Without it, any sample is diagnostic-only and cannot change `11E_READY` or close 11E. Do not silently designate historical 640×360 as product target.
- Exact source/build/runtime hashes and timing clock domains captured. Performance remains descriptive until a project threshold is explicitly adopted.

## Status record

| Field | Current result |
|---|---|
| BR9R scientific status | N3 — INCONCLUSIVE |
| BR9R engineering status | INVESTIGATION CLOSED / DEFERRED BY DIMINISHING RETURNS |
| H6 status | H6-A READY; H6-B READY; fresh Gate 0 and lifecycle authorization PASS; replay 1 stopped before launch at storage admission |
| Step 11D.3-C | DEFERRED; not an 11E dependency |
| Step 11 closeout | CONDITIONAL |
| Essential remaining Step 11 items | Source/backbuffer binding; cross-frame A/B transport/lifetime; two-source reset and stale rejection; closed bounded release ledger |
| Deferred items | BR9R/Finder/Native differential, H7/H8, 11D.3-C, broad backend/model/quality work |
| 11E readiness | NO |
| 11E execution | NOT RUN |
| A/B history | NOT PROVEN on integrated path |
| Synchronization | Single-source READY/DONE/CONSUMED proven in retained runs; A/B two-source ordering not measured |
| Lifetime/reset | Per-job single-source evidence exists; cross-frame A/B lease, forced in-flight reset, and final zero-owned ledger unproven |
| Real-resolution performance | NOT MEASURED; product target matrix and thresholds undefined |
| Independent review | Three read-only audits and one implementation-owner feasibility check completed; fresh independent review initially returned PASS WITH NON-BLOCKING CAVEATS, then confirmed the requested corrections and returned PASS. No 11E run reviewers apply until a valid run exists |
| Exact next step | Obtain the pinned integrated R5 source/build inputs, settle the intended resolution scope, implement and test the minimal A/B/rotation/reset/release blockers, then fresh-review before any runtime run |

No commit or push was made.
