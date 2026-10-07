# Step 11D.2 — R5 Bimodal History-Slot Stall Diagnosis

**Disposition: measured proximal cause; Step 11E remains blocked.** The long READY-record-to-Metal-commit intervals are caused by a synchronous source `vkQueueSubmit2` call blocking in MoltenVK while it waits for a `CAMetalLayer` drawable. That stalls the shared DXVK submit worker, delays READY publication and Metal dispatch, and leaves the claimed slot owned. The diagnostic evidence identifies this mechanism and its downstream waits. It does not establish why QuartzCore's drawable semaphore is available promptly in some launches and remains unavailable much longer in others, nor does it contain a fully instrumented 45 WSI/s run.

## Frozen candidate and diagnostic builds

The R5 baseline remains source SHA-256 `044d03510e5b1c57fead0aed1a31f03105c78b17dcc303d7053b4811ab270c0b`, with the D3D11, DXGI, PE bridge, and native-provider hashes recorded in the Step 11D.1 report. All diagnostic runs used 30 Hz source and retained two HISTORY plus two OUTPUT resources. No R6 build or additional slot was used.

The deep-timeline binary is an instrumented diagnostic build, not the frozen R5 candidate: its D3D11/DXGI DLLs hash to `cc09178473bef9b6af9fec5821d9d56ad089d61b9daf0af139c132b379f46297` and `2c90d3fc3f364a13d003c2dfe1a08ccdffe5bca9a9d6cde8408b65d6a5450f88`; its bridge/provider hashes are in each run manifest. The source base is R5, but the added tracing and enlarged bridge packet mean these timings are diagnostic evidence, not exact-R5 acceptance measurements.

## Existing exact-R5 low/high comparison

The six frozen-R5 five-minute trials already show the bimodality. READY-record-to-commit distributions were extracted from the exact-run logs:

| Run | WSI/s | p50 ms | p95 ms | p99 ms | Max ms | READY-to-commit >20 ms | NO_FREE_HISTORY | Internal queue full |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| A1 low | 44.898 | 2.902 | 10.341 | 13.434 | 36.604 | 12 | 1,955 | 2,389 |
| B1 low | 45.478 | 2.416 | 12.831 | 17.726 | 39.667 | 37 | 1,987 | 2,203 |
| A2 low | 45.590 | 3.358 | 15.714 | 20.504 | 1,175.889 | 78 | 1,841 | 2,263 |
| B2 high | 58.993 | 2.346 | 3.810 | 4.857 | 31.482 | 2 | 70 | 98 |
| A3 high | 58.099 | 2.504 | 4.039 | 7.402 | 18.948 | 0 | 161 | 287 |
| B3 high | 58.929 | 2.494 | 3.812 | 4.349 | 44.226 | 2 | 39 | 45 |

The monitor/no-monitor alternation does not explain the regimes: low A1 and A2 were unmonitored, low B1 was monitored, high A3 was unmonitored, and high B2/B3 were monitored. A2's largest event was job 3264 at 1.175889 s; the following source Present lasted 1.144259 s. Its own job 3265 reached commit in 3.873 ms. This links the long READY-to-commit event to source-side blocking, but the exact-R5 A2 log did not split time inside the Vulkan call.

## First measured stage divergence

The diagnostic timeline comparison below uses the 60-second slow-tail run 07 (54.283 WSI/s) and the clearly high run 08 (58.283 WSI/s). Run 07 is an intermediate-cadence run, not the 45 WSI/s regime; its per-job tails nevertheless reproduce the same long synchronous source-submit stall. Full p50/p95/p99/max distributions for every recorded stage are in each linked `timeline_summary.json`.

| Stage | Run 07 p50 / p95 / p99 / max ms | Run 08 p50 / p95 / p99 / max ms |
|---|---|---|
| Copy submit → READY available | 1.023 / 14.040 / 15.992 / 722.911 | 1.059 / 1.613 / 14.384 / 16.164 |
| Source-ready `vkQueueSubmit2` | 0.136 / 1.237 / 15.751 / 722.783 | 0.129 / 0.962 / 1.470 / 15.936 |
| READY available → provider entry | 0.683 / 2.550 / 3.186 / 7.991 | 0.805 / 2.193 / 2.860 / 4.513 |
| READY available → Metal commit | 0.744 / 2.670 / 3.304 / 8.031 | 0.877 / 2.309 / 2.973 / 4.678 |
| Source-present queue wait | 1.158 / 14.823 / 16.195 / 723.115 | 1.199 / 1.989 / 15.119 / 16.320 |
| `m_presentPending` condition wait | 0.002 / 15.751 / 16.682 / 326.783 | 0.002 / 0.710 / 15.784 / 21.090 |
| Metal commit → completion callback | 0.485 / 0.771 / 0.921 / 297.743 | 0.494 / 0.727 / 0.812 / 1.189 |
| HISTORY/OUTPUT slot claim → joint release | 33.204 / 59.003 / 68.976 / 749.779 | 32.945 / 37.464 / 55.437 / 85.791 |
| Terminal → slot release | 7.535 / 18.734 / 31.320 / 37.157 | 9.297 / 17.653 / 19.919 / 52.609 |

The first large divergence is before READY becomes available: the source-ready queue submission. READY-available-to-provider and provider/Metal enqueue stages remain close. In run 07, PE-to-Unix bridge entry was p95 0.003 ms (max 0.195 ms), Unix-to-provider entry p95 0.002 ms (max 0.119 ms), command-buffer creation p95 0.025 ms (max 0.109 ms), encoding p95 0.032 ms (max 0.085 ms), and the Metal commit call p95 0.024 ms (max 0.135 ms). The native provider runs inline; it has no worker thread, condition wait, or hidden work queue. Its recorded in-flight depth never exceeded two.

Run 07 also had one separate Metal completion-callback delay of 297.743 ms (job 1174). That job's slot stayed owned for 659.880 ms, but terminal-to-release was only 0.045 ms after the terminal event. Its callback delay is a real secondary tail and is not explained by the source-submit sample. The high run's maximum callback interval was 1.189 ms.

## Matched stall evidence

Run 07 job 1202 / AppFrameId 1254 shows where a 723 ms slot hold was spent:

1. HISTORY slot 1 was claimed at monotonic `675636215092500` ns.
2. The source-ready graphics `vkQueueSubmit2` began at `675636215596600` ns and returned at `675636938380000` ns: **722.783 ms** in that call.
3. The Vulkan READY signal-submit completed at `675636938487300` ns. Metal provider entry and command-buffer creation followed promptly; Metal committed in microseconds.
4. The next source frame, AppFrameId 1255, waited on `m_presentPending` from `675636215529100` to `675636938834300` ns: **723.305 ms**, overlapping the same queue-submit stall.
5. Job 1202 terminalized at `675636939331100` ns and the shared slot released at `675636939400500` ns. Terminal-to-release was **0.069 ms**; the slot's 724.308 ms hold was overwhelmingly before Metal dispatch/terminalization, not a delayed reclaim after terminal.

The same overlap occurred for job 1169 (325.904 ms source-ready submit and a 326.783 ms next-frame `m_presentPending` wait) and job 1173 (403.784 ms submit and a 404.344 ms pending wait).

The one-second macOS `sample` captured process `wine` PID 83220 at 20:06:29.545 +0330, overlapping job 1169. In the `dxvk-submit` thread (TID 7265845), 28 of 37 samples show the stack through `vkQueueSubmit2` → MoltenVK command-buffer/render-pass encoding → `MVKPresentableSwapchainImage::getCAMetalDrawable` → `WineMetalLayer nextDrawable` → `CAMetalLayer nextDrawable` → `_dispatch_semaphore_wait_slow`. This is direct thread-state evidence of a QuartzCore drawable semaphore wait, not an inference from generic OS scheduling. See `step11d2-diagnostic/diag-60s-07-submit-reclaim-split/os-sample-01.txt`.

The sampled stack explains where the matched 326 ms wait occurs. The 723 ms event is measured in the same source-ready `vkQueueSubmit2` call, but that event did not receive its own simultaneous stack sample. The diagnostic data therefore identifies the blocking mechanism and exact API stage, while the reason QuartzCore releases drawable availability at different times remains unproven.

## Ownership and shutdown ledger

R5 stores HISTORY and OUTPUT in the same interop slot and logs `historyAndOutputReleasedTogether=true`. For a successful consumer, terminal/reclaim is the Vulkan `CONSUMED` timeline value; `pollSlots()` releases both resources after the provider reports a terminal Metal state and Vulkan's semaphore counter reaches that reclaim value. A committed job dropped for queue/deadline reasons reclaims at DONE; a confirmed Metal failure reclaims at READY so it does not wait for a DONE signal that may never arrive. The slot is **not** held until WSI retirement: consumer-submit-to-WSI-retirement is measured separately, and every diagnostic run's submitted consumer count equals its retirement count.

The current slot contract still does not release HISTORY independently at Metal completion. HISTORY remains busy until OUTPUT's Vulkan consumer work is complete (or, for a dropped output, until Metal DONE). This can add ordinary slot hold time and should be considered separately from the measured 723 ms source-submit stall. The large source-submit hold does not become a terminal/reclaim delay: job 1202 released 69 microseconds after terminal.

The five-minute instrumented run 10 produced 8,958 captured/committed/completion-callback/terminal records, 7,758 submitted consumers and 7,758 WSI retirements, but only 8,957 slot-release markers. Job 8958 has a terminal marker and no `job-retired` marker at EOF. EOF is not counted as retirement. Therefore ledger closure and normal teardown are **not proven**. Its 55.863 WSI/s result is an instrumented intermediate sample, not the frozen-R5 acceptance run and not the required repeatability matrix.

## Wait, provider, and diagnostic-accounting checks

The native provider has no timed wait or worker wake to malfunction. The R5 Windows `dxvk::condition_variable` does use `SleepConditionVariableSRW` and the QPC-backed monotonic `high_resolution_clock`. A targeted test compiled against the R5 source header and passed under the pinned Wine 11.17 runtime for expired and future deadlines, timeout sign, predicate wake, spurious wake, a 50-day timeout conversion interrupted by notification, and negative `wait_for`. See `custom_wait_until_test.cpp` and its run output in this evidence directory. No wait-conversion defect was found.

The analyzer was corrected to interpret `internal_queue_insert.queueDepth` as the pre-push size; accepted entries therefore count as one pending request. The internal-present pending queue capacity is one. The analyzer also now reports copy-submit-to-READY and READY-available-to-provider durations separately. It excludes `InteropJobId=0` and negative timestamp pairs. These changes are diagnostic only.

## Disposition and remaining blockers

**Root mechanism:** intermittent `CAMetalLayer nextDrawable` semaphore waits inside the synchronous source-ready `vkQueueSubmit2` call. This delays READY publication and the following Metal dispatch, blocks the single source submit/present worker, holds the claimed paired slot, and overlaps the next frame's `m_presentPending` wait. The exact-R5 low/high `ready-to-commit` distributions, the instrumented high/slow-tail comparison, and the matched thread sample all agree on that mechanism.

**Not established:** a fully instrumented 45 WSI/s run; the exact display/compositor state causing QuartzCore drawable availability to vary; why the separate 297 ms Metal callback tail occurred; independent HISTORY release; strict normal-shutdown closure; deterministic pause-state shutdown; full optional-failure matrix; forced generation invalidation in all requested states; restored FLIP backing-rotation trace; latency acceptance matrix; five independent five-minute unmonitored trials; final soak and regressions.

No scheduler or WSI ownership change was made: the measured long wait is inside MoltenVK/QuartzCore, and changing WSI acquisition/presentation semantics is outside the frozen constraints. The existing README was not changed, and no commit or push was made because the result remains blocked.

**Final status: STEP 11E STILL BLOCKED.**

Evidence: [Step 11D.1 report](STEP11D1_REPORT.md), [diagnostic runs](step11d2-diagnostic/), and [original cadence summary](cadence-repeatability-r5-20261006/cadence-summary.json).

## Storage retention reconciliation — 2026-10-07

The separate 145 KiB bridge/provider overlay used by the diagnostic runs was adopted under lease `legacy-fgmetal-step11d2-bridge-diagnostic-build-c-20261006-91603f7d`. Its short-lived PRESERVE_DEBUG manifest is [66e8867390644bbfd7ad21322c5a0baa0135b4d399ecc2c775b480f5f0cf74a7](../../storage/build-manifests/legacy-fgmetal-step11d2-bridge-diagnostic-build-c-20261006-91603f7d.json), manifest SHA-256 `5b20bbba0784e242473c357b59975fe1839695635fd10f2ed600379a1376e44e`. The run manifests and report are retained with the manifest; the two output hashes resolve to the canonical store: `FGMetalBridge.dll` `7a4d53fbf6e4730c93119759d521d91bbf5bfa419bde6df7d63d2ca7b0d500e3` and `fgmetalbridge.so` `ecc9ce4c552f093a501ed1ac1186bbd086b2db4465830a07d119a6c4b49dd5d2`. The three top-level object files were captured as hash-indexed debug-symbol evidence. Historical build-time source/configuration completeness remains explicitly unasserted by this manifest; it is a debug-preservation record, not a rebuild claim.

After the diagnostic runner was switched to those canonical artifact paths, the debug lease was closed and the temporary build tree retired. The preserved run records and object-symbol evidence remain available through the manifest and evidence index.
