# Step 11D.1 — Metal Roundtrip Acceptance Closure

**Disposition: PARTIAL PASS. Step 11E remains blocked.**

The zero-copy Vulkan → Metal → Vulkan design, source-derived transform, GPU-only synchronization, DXGI/D3D11 isolation, and absence of a CPU-pixel path remain accepted from the frozen R5 baseline. This closure adds repeat cadence, timing, lifecycle, process-termination, and regression evidence. It does not authorize two-frame work.

## Candidate identity and scope

All new runtime checks used frozen R5. The source-tree identity is `044d03510e5b1c57fead0aed1a31f03105c78b17dcc303d7053b4811ab270c0b`; D3D11 is `e747ca5a5c2087d1f788762a5c4bae13939593a2cb89dedf2f7c04999ab94de5`; DXGI is `58e972688c739ee8444c8efa7a175503ff8dd0774ceb63e9b8526168054d9132`; PE bridge is `44cdaa3ff503a1beb3afd735620c3b33eac4bca17377fa78e5c3be5f852c18cf`; native provider is `033e3a8cbd957b1e16131fc690256aba8684d51b17688514a5ff7eaa351c2fe2`. Run manifests bind the runtime DLL/bridge/provider hashes to these R5 component identities. No R6 binaries or slot-count changes were used.

## 1. Cadence repeatability and monitor overhead

Six matched five-minute trials alternated unmonitored and 1 Hz RSS-monitor runs. They used the same R5 build, 30 Hz source, window/resolution, logging, interop settings, and test application. Prefixes were freshly constructed per run.

| Run | Monitor | Source Presents | Captures / Metal commits | Consumer attempts | Internal WSI successes | NO_FREE_HISTORY | Queue full | Deadline | Other explicit | Combined WSI/s |
|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| A1 | none | 9,001 | 7,046 / 7,046 | 4,600 | 4,470 | 1,955 | 2,389 | 8 | 1 | 44.8983 |
| B1 | RSS 1 Hz | 8,999 | 7,012 / 7,012 | 4,749 | 4,647 | 1,987 | 2,203 | 1 | 2 | 45.4777 |
| A2 | none | 8,999 | 7,158 / 7,158 | 4,816 | 4,678 | 1,841 | 2,263 | 14 | 0 | 45.5901 |
| B2 | RSS 1 Hz | 9,001 | 8,931 / 8,931 | 8,790 | 8,699 | 70 | 98 | 9 | 0 | 58.9934 |
| A3 | none | 9,001 | 8,840 / 8,840 | 8,482 | 8,431 | 161 | 287 | 9 | 0 | 58.0994 |
| B3 | RSS 1 Hz | 9,001 | 8,962 / 8,962 | 8,858 | 8,680 | 39 | 45 | 2 | 0 | 58.9293 |

Across all six runs, cadence mean was **51.9980 WSI/s**, median **51.8448**, range **44.8983–58.9934**, and sample standard deviation **7.3238**. The unmonitored arm had mean **49.5293**, median **45.5901**, range **44.8983–58.0994**, and sample standard deviation **7.4300**; two of its three runs were below 52 WSI/s. The monitored arm had mean **54.4668**, median **58.9293**, range **45.4777–58.9934**, and sample standard deviation **7.7849**; one of its three runs was below 52.

The cadence gate **fails**: the unmonitored median is below the required 55 WSI/s and two unmonitored runs are below 52 WSI/s. The RSS monitor used `ps -axo pid=,ppid=,rss=,command=` once per second, traversed the process tree, and scanned about 190,000–195,000 process rows per run. It consumed 14.255–14.697 CPU seconds over about 319–320 seconds (roughly 4.5% of a CPU) and made 318–320 timed wakeups. That is measurable observer work, but the alternating results do not show a consistent rate penalty, so monitoring does not explain the low unmonitored trials. The earlier 56.396 versus 50.794 WSI/s discrepancy remains unresolved.

The low-rate group also had 1,841–1,987 NO_FREE_HISTORY drops per run, compared with 39–161 in the high-rate group. The evidence correlates poor cadence with two-slot pressure and longer capture-to-retirement tails. It does not isolate whether scheduler timing, host load, or Metal queue behavior caused that pressure. The reference Vulkan-only R5 result was 58.983 WSI/s; a prior 60-second Metal result was 57.947 WSI/s, but neither replaces the failed repeated five-minute acceptance median.

Evidence: [cadence summary](cadence-repeatability-r5-20261006/cadence-summary.json), [run sequence](cadence-repeatability-r5-20261006/sequence-results.json), per-run ledgers and strict audits under `cadence-repeatability-r5-20261006/`.

## 2. Job/drop classification and ledger invariants

The six cadence runs contain 54,002 source Presents: 47,949 captured jobs plus 6,053 `NO_FREE_HISTORY` drops. Every captured job in those logs has exactly one terminal event. Aggregate terminal classes are:

| Terminal | Count | Raw R5 reason / interpretation |
|---|---:|---|
| PRESENTED | 39,605 | Consumer submitted and internal WSI retirement succeeded |
| SCHEDULER_QUEUE_FULL | 7,285 | `queue-full` |
| SOURCE_PRIORITY | 392 | `source-pending`, `source-queue-nonempty`, or source/swapchain conflict before internal submit |
| DEADLINE_EXPIRED | 43 | Deadline elapsed before claim, during scheduling, or before submit |
| WSI_FAILURE | 621 | Lease validation failed before internal submit; logged retirement has `wsi=0` |
| OTHER_EXPLICIT | 3 | Raw terminal reason was `presenter-busy` |

There were 40,295 consumer attempts and 39,605 successful submitted consumers. Every consumer attempt joined to exactly one WSI retirement record, including pre-submit failures with no WSI submit. The audited READY/DONE/CONSUMED ordering, two-slot maximum ownership, and terminal uniqueness checks pass for captured cadence jobs. The measured paths did not leave a DONE wait dependent on a failed Metal signal.

The ledger is **not closed at shutdown**: five of six cadence logs lack one `job-retired` marker at EOF and report its slot still owned. A2 is the only strict cadence audit that fully passes. This is a retirement-marker/ownership evidence failure; the log alone cannot distinguish a physically retained resource from a final release omitted from the ledger.

The full requested taxonomy is not exercised. `NO_FREE_OUTPUT` was not observed or injected. `GENERATION_INVALIDATED` occurs once in the lifecycle run but was not forced in each required state. Shutdown cancellation and Metal/Vulkan failure terminals were not comprehensively injected. Natural queue-full, deadline, source-priority, and lease-validation cases are classified above; the failure matrix below remains incomplete.

Evidence: per-run `synchronization-ledger-strict-audit.json` files and CSVs in [the cadence evidence folder](cadence-repeatability-r5-20261006/).

## 3. Source Present latency tails

The trace-only correlation includes every source Present longer than 5 ms and joins its AppFrameId, WSI acquire/image/generation, same-frame job and slot/resource/timeline IDs, nearby job events, and available partial durations. Tail counts above 5 / 10 / 25 / 50 / 100 ms were:

| Run | >5 ms | >10 ms | >25 ms | >50 ms | >100 ms |
|---|---:|---:|---:|---:|---:|
| A1 | 343 | 83 | 3 | 1 | 0 |
| B1 | 422 | 313 | 30 | 0 | 0 |
| A2 | 1,282 | 784 | 54 | 1 | 1 |
| B2 | 40 | 37 | 1 | 0 | 0 |
| A3 | 55 | 35 | 2 | 0 | 0 |
| B3 | 6 | 5 | 2 | 0 | 0 |

`SOURCE ONLY`, synthetic Vulkan internal-WSI output, and Metal roundtrip were each measured once for 60 seconds. Their Present p50/p95/p99/p99.9/max values were respectively **208/1,456/2,182/13,214/39,124 µs**, **184/442/16,478/43,960/44,744 µs**, and **231/571/15,482/31,258/142,880 µs**. These single-run samples do not establish a systematic latency regression. The synthetic Vulkan case emitted experimental magenta/cyan internal WSI output; it should not be described as a literal clear-image workload.

The largest tail was A2 AppFrameId 4106: source Present lasted **1.144259 s**, WSI present 6301/acquire 6372 on generation 2, image 0. Its own job 3265 had a 46 µs copy record, 3.873 ms ready-to-commit interval, and retired after 12.864 ms. Immediately preceding job 3264 had a **1.175889 s** ready-to-commit interval, remained `source-pending`, and terminalized after 1.211286 s. This is a strong temporal correlation, not proof that Metal caused the Present delay.

The trace does not measure WSI acquire duration, queue/lock waits, source command recording/submission, bridge-call duration, contemporaneous slot/scheduler state, swapchain lifecycle timestamps, Wine/OS scheduling, or per-stage logging cost. Static R5 review found no direct CPU wait for Metal completion in the normal application Present path. Indirect contention is not ruled out, so the source-latency hard invariant is **partially supported but not fully proven**, and tail causality remains unresolved.

Evidence: [latency comparison](source-latency-comparison-20261006.json), [tail join data](source-present-tail-correlation-20261006.json), [tail CSV](source-present-tail-correlation-20261006.csv), [static lifetime audit](source-latency-lifetime-audit.md).

## 4. Failure matrix and async Metal failure distinction

| Failure case | Evidence / result | Status |
|---|---|---|
| Provider unavailable | Provider binary omitted; short 1 Hz source run continued with no Metal commits | PASS for this case |
| Provider initialization failure | Not deterministically injected | NOT RUN |
| HISTORY texture export failure | Not injected | NOT RUN |
| OUTPUT texture export failure | Not injected | NOT RUN |
| Shared-event export failure | Not injected | NOT RUN |
| Metal pipeline creation failure | Not injected | NOT RUN |
| Metal pre-commit rejection | One injected rejection; source continued through 451 Presents, but the old strict ledger did not close all retirement markers | PARTIAL |
| Metal post-commit asynchronous failure | Test provider returned a simulated asynchronous error after commit; job failed and retired without a Vulkan DONE consumer wait; source continued | PASS as simulated provider failure only |
| Genuine Metal command-buffer NSError | Not safely or reliably induced with a real GPU failure | NOT RUN |
| Vulkan consumer submission failure | Not injected | NOT RUN |
| Forced stale-generation invalidation in four requested states | Not injected | NOT RUN |
| Deterministic deadline expiry | Natural expiries occurred; dedicated injection absent | NOT RUN |
| `VK_ERROR_DEVICE_LOST` fatal path | Not tested | NOT RUN |

The post-commit injection is explicitly **simulated at the provider callback**. It is not evidence of a real Metal command-buffer error and is not labeled as an NSError. The tested failure case did not leave a Vulkan wait pending on a Metal signal that could never arrive. Optional-failure source preservation across the untested cases remains unverified; device loss is expected to stay fatal.

Prior inventory: [failure matrix status](../../../../../../.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/evidence/ledger-audits/failure-matrix-status.md) and [strict async injection audit](../../../../../../.codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/evidence/ledger-audits/current-r5-async-injection-strict.json).

## 5. Shutdown, final-job drain, and process termination

The requested deterministic pause hooks for all seven states—FREE, VULKAN_WRITING_HISTORY, READY_FOR_METAL, METAL_IN_FLIGHT, READY_FOR_VULKAN, VULKAN_CONSUMING, and RETIRING—were not implemented. The existing normal shutdown sequence has one useful R5 lifecycle run, but it does not establish deterministic teardown from each state.

The 60-second 30 Hz lifecycle run completed explicit renderer/device teardown without an arbitrary sleep. It recorded 1,800 source Presents, 1,720 Metal commits, 1,619 consumer attempts, 1,541 submitted consumers/internal WSI Presents, one `DeviceShutdown: normal`, one provider-resource release, and two Presenter retirement markers. The last committed job reached terminal state, consumer WSI retirement, and interop retirement. However, job 724 ended READY-only with a precommit `source-present-or-metal-commit-failed` terminal and no `job-retired` event; the strict audit reports that slot owned at EOF. Thus the normal teardown path ran, but the complete job/slot ledger and “all committed jobs terminalized plus every submitted consumer retired” shutdown contract are **not fully proven**.

Forced process termination used Wine `taskkill /F` at 1, 5, and 10 seconds after the first active commit. At 1 s, counts were 37 captured / 37 committed / 36 retired; at 5 s, 157 / 157 / 156. Both were active-work terminations, returned successfully, skipped normal device/provider shutdown, and had no hang or self-join symptom. At 10 s, counts were 303 / 303 / 303, so the app was between committed jobs; it is not a third active-Metal sample. D1 process-detach behavior is supported at two active offsets, not fully sampled at three.

Evidence: [30 Hz lifecycle result](lifecycle-30hz-r5-20261006/runtime/result.json), [strict lifecycle ledger](lifecycle-30hz-r5-20261006/runtime/synchronization-ledger-strict-audit.json), [process termination results](process-termination-r5-20261006/sequence-results.json).

## 6. Generation invalidation and 30 Hz lifecycle

The R5 lifecycle run exercised resize/recreation at 3 and 5 seconds, minimize/restore at 25 and 26 seconds, fullscreen/windowed transitions at 35 and 36.5 seconds, and another swapchain recreation at 45 seconds. Source Presents continued through the 60-second run. Its ledger saw six swapchain generations, respected the two-slot maximum per generation, and recorded one generation-invalidated job; it did not flag a stale-generation consumer.

This is useful 30 Hz lifecycle evidence, but it is not the requested forced resize/recreation while jobs are specifically in HISTORY-writing, METAL_IN_FLIGHT, READY_FOR_VULKAN, and VULKAN_CONSUMING. The one READY-only retirement gap also prevents a complete all-slot release claim. Generation invalidation is therefore **PARTIAL**, not a pass for all four states.

## 7. High-rate visual criterion

The corrected criterion is applied: high-rate acceptance does not require the observer to distinguish each complementary-color frame. Across the three reported observations, the user consistently saw fast flicker with mostly white recognizable. No observation reported sustained black output, a frozen image, or persistent texture corruption. The rapid complementary-color pattern is expected at the tested rate. The existing 1 Hz direct visual inversion proof remains the content/order visual check; high-rate content order is established by resource/job IDs and the ledger. **The revised high-rate human visual check passes.**

No screen capture or CPU pixel readback was used. A separate slow-bar diagnostic transform was unnecessary and was not run.

## 8. Final soak and controls

The distinct phase-19 five-minute final soak with minimal instrumentation, completed teardown ledger, and bounded host-memory trend was **not run**. The earlier five-minute cadence trials cannot substitute: their cadence fails, and five strict ledgers end with one retirement marker missing. No increasing-memory conclusion is claimed from the RSS monitor samples.

Control results:

- Source-only 30 Hz R5: 1,801 Presents, approximately 30 Hz; source accounting passed.
- Synthetic Vulkan internal-WSI R5 control: 1,740 successful internal retirements and 58.9832 combined WSI/s. This used an experimental magenta/cyan diagnostic pattern, not a literal clear image.
- D3D9 smoke: passed, 300 frames in 2.52 seconds, no reported errors.
- Exact-R5 FLIP_SEQUENTIAL: both source-only and internal arms recorded 901 contiguous app Presents, matching `GetLastPresentCount`; waitable signaling counts matched (900 signaled, one timeout), with 802 internal WSI presents in the internal arm. The DLLs emitted zero backbuffer trace events, so the two-slot rotation invariant is unverified and the analyzer reports `p0_2_pass=false`.
- Resize and other lifecycle transitions ran in the 30 Hz R5 lifecycle case above. DXGI/D3D11 isolation remains accepted from the R5 baseline; this closure did not alter the architecture.

Evidence: [exact-R5 FLIP analysis](flipseq-r5-exact-20261006/runs-fixed/analysis.json), [lifecycle artifacts](lifecycle-30hz-r5-20261006/runtime/).

## 9. Adversarial review and acceptance decision

The independent adversarial reviewer concluded that Step 11E is **not authorized**. The reviewer agreed the corrected visual criterion passes, but cadence misses its target; the RSS monitor does not explain the variance; the source tail is correlated but not causally diagnosed; five cadence runs and one lifecycle job lack retirement markers; deterministic failure injections and seven-state shutdown gates are incomplete; process termination has only two qualifying active-work offsets; and exact-R5 FLIP accounting passes while two-slot rotation remains unverified.

| Acceptance criterion | Result |
|---|---|
| Repeated five-minute cadence stable or observer effect explains variance | **FAIL** |
| Corrected high-rate visual check | **PASS** |
| Every opportunity has complete explicit classification | **PARTIAL** |
| Source latency tails explained; optional work not systematically blocking source | **PARTIAL / UNVERIFIED** |
| All optional failure cases preserve source | **PARTIAL** |
| No impossible Metal-dependent Vulkan wait | **PASS for observed/injected cases; full matrix unverified** |
| Deterministic shutdown from all seven states | **NOT RUN** |
| Explicit teardown drains every final job and consumer | **PARTIAL** |
| Generation invalidation safe in all four requested states | **PARTIAL** |
| 30 Hz lifecycle | **PARTIAL** (transitions passed; strict ledger gap remains) |
| Final five-minute soak | **NOT RUN** |
| Vulkan/source/D3D9/DXGI/FLIP/resize regressions | **PARTIAL** (source, D3D9, and app accounting pass; FLIP rotation is unverified) |

**Overall: PARTIAL PASS. Step 11D acceptance is not closed. Step 11E is not authorized.** No R6, third resource slot, RIFE, or two-frame interpolation work was started.

## Evidence index

- [Repeated cadence and per-run ledgers](cadence-repeatability-r5-20261006/)
- [Source latency comparison](source-latency-comparison-20261006.json)
- [Source Present tail correlation](source-present-tail-correlation-20261006.json)
- [30 Hz lifecycle and shutdown](lifecycle-30hz-r5-20261006/runtime/)
- [Process termination offsets](process-termination-r5-20261006/sequence-results.json)
- [Exact-R5 FLIP_SEQUENTIAL control](flipseq-r5-exact-20261006/runs-fixed/analysis.json)
- [Source latency and lifetime audit](source-latency-lifetime-audit.md)

STEP 11E STILL BLOCKED.
