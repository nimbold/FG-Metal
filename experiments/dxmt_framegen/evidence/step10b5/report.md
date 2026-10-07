# Step 10B.5 — Independent Display/VSync Evidence and Final Synthetic Timing Gate

- Captured: 2026-10-04, Asia/Tehran
- Overall result: **FAIL**
- RIFE decision: **RIFE NOT CLEARED**

## Gate result

The measured callback, submission, and GPU-completion paths remain near 60 Hz, and accepted sources stayed safe in the qualified primary runs. Apple Display tracing also shows 60 Hz VSync opportunities. However, a separate motion probe changed the clear color on every application Present and still produced only about 8.0–8.6 consecutive Wine-attributed displayed-surface gaps per trace second, with a median surface hold around 65 ms and a median Display frame-label step of 6. The same surface cadence appeared with source-only presentation and with F-equivalent generation enabled.

This supports **CASE B at the compositor surface-update level**: updates are being coalesced or skipped after the app changes content on every Present and submits work. It does not establish that exactly 57 drawable attempts per second reached scanout, and it does not prove that every zero `presentedTime` was dropped. The Display rows cannot be joined one-to-one to the native drawable attempt IDs. Physical panel scanout therefore remains **unverified**, but the independent surface evidence is already unfavorable enough to fail the gate.

Labels used below:

- **VERIFIED** — directly present in frozen manifests, run output, native logs, or exported trace tables.
- **INFERRED** — interpretation supported by the measured evidence, with the stated limits.
- **NOT TESTED** — not run or not resolvable with this trace.

## Frozen identities

The machine-readable manifest is [runtime-identity.txt](runtime-identity.txt). Its SHA-256 is `58d6be95e8d7c4f8fe24ba031420da03762a1528dbcdae56d45da713ea6626c0`.

| Artifact | Frozen identity |
|---|---|
| Candidate | `step10b5-local-f-equivalent-signpost-v3` |
| DXMT source commit | `fb4515681daefb789a4d0f403c4bdbca88f3b3de` |
| Downstream source tree | `664aaad0d4611f8272d9d2857e236cce2101dd012fb2daea08741765e6608551` (478 files) |
| Downstream diff stream | `c1dc834baba26b7d3f54daf9f18d8c98fb06b287effc17490947ea389db1b96e` (no patch artifact was created) |
| Synthetic Framegen library | `dbed8452d8458641aee9ca6b58188ba5a3b4873ff2e0dfd357143fafade19d54` |
| D3D11 harness | `6c919596ba659493e8d66fe5c9197a67ae0671424eab746f92c5a71a2de37c71` |
| d3d10core.dll | `786d42842609a07bd63409def87ddeb1ef29471ddc5853352b5f6040f320e2be` |
| d3d11.dll | `a8a80faac6231fc3e997702c05f838d69752c05739087b8d2ad07a00b072fecc` |
| dxgi.dll | `086f0c433a191cc20967682ed437e62b0f1a5c2d6c641bd41732808c06c3bc7f` |
| winemetal.dll / winemetal.so | `abd5ff1e9c0873513c778736596d848037e057784b081ccd430c6070fca7aa75` / `8079c6779f401727d3ec22b51b4b2837d786363b910f61ee77077b7d28f0f68f` |
| Analyzer / runner / stager / path-safety script | `c7e48a8b566798d87fd096b96be8f94f7a469f9a33cc294944c36113950c402c` / `15c09029c9f33f24b74de4c0ce5589dd827ccd0c5d57e1882930caae5f9994e0` / `988d4a7260662d9e1ba33635999d0fb1a696466158eea7befec4bc584460878b` / `aab1e95d36d40606dce12583866c5bf9f940976fcdcc70d06c803ac86efe546f` |
| Harness build script / regression-test source | `35ab5503fdeffba5ebec5dfcc40f338a3e5025f89fb769c142b01329e3dd9a65` / `bede9e5ea51a003de04a17e7204b84118d9f630619ee7680253e95cecc7eaedb` |
| Base Wine / wineserver / required libinotify | `1b992a3e0bc5f2a058a24f923832aaa6e464d44766fb0ad13054797e02060d10` / `6dfe1f9d2d8a67cc6a09a57966f5ef88fd461abe7321d6fb0d4a1672e8ff0350` / `0565014c42c3506fb5635aeb12f09bad3a4eef8f7ff3e6c9cf9d9e9e6b339df8` |
| Workspace baseline commit | `c30e2d19b26b1611cea328b0bd551c63c82af9b1` |

Every accepted static, source-only, and primary F run used a fresh disposable engine copy and Wine prefix. The staged DLL hashes matched this manifest. The motion probe was a separate candidate with one named variable: color changed every app Present instead of every 60 Presents. Its identity is recorded in [display-motion-probe-identity.txt](display-motion-probe-identity.txt); source SHA-256 `e315dd2c7c5aeaa734ec550902fe376624b17d0ffc95e4791b8d767a403d4185`, binary SHA-256 `1f6147376d786ec7cc9e86faa66c87e2a1bafc2b591ef655ac5b9bf917eef5e9`.

## Qualification and rejected runs

**VERIFIED qualification checks:** foreground and visible app window; built-in display ID 1 at 2940×1912 and 60 Hz; expected app source cadence near 30 Hz; callback ledger near 60 Hz; matching frozen runtime hashes; fresh prefix and staged engine for primary comparisons; one stable owner epoch; no app Present errors; no lost accepted sources, unsafe terminals, source regressions, malformed G pairs, or ordinary `nextDrawable` ownership assertion.

Only the qualified runs below contribute to primary comparisons. Rejected or exploratory attempts were retained and excluded:

| Attempt | Exclusion reason |
|---|---|
| Initial runtime smoke | Required sibling `frameworks/libinotify.0.dylib` was missing; rejected before launch. |
| Early fullscreen/hidden-window trials | App surface was not visible/foreground and cadence/source safety was abnormal. |
| Focus-loss static trial | Only 122/901 Presents were foreground; retained as exploratory. |
| Trace export/attach failures | One trace failed with Instruments “Data stream: Time Mapping”; other captures attached after the app closed or used a mismatched process basename. Native-only output was not promoted to trace acceptance. |
| Long capture attempts v18 and v22 | Xcode could not save/trim the trace because the disk was full. Native output was retained but not counted as independent display evidence. |
| First source-motion trace attempt | Wrong process matcher; run was not trace-qualified and was repeated with the corrected matcher. |

The saved primary run outputs, manifests, and staged hashes are under [runs](runs/). Full raw `.trace` packages remain in their `/private/tmp/dxmt-step10b5-run-v*` capture directories; the programmatically exported TOCs and selected XML tables are preserved in this evidence directory. Their SHA-256 values are in [trace-exports.sha256](trace-exports.sha256).

## Xcode facilities, signposts, and correlation method

**VERIFIED:** Xcode and `xctrace` are 27.0 (27A266a), macOS 27.0.1 (26A434). The exact output of `xcrun xctrace version`, `list templates`, and `list instruments` is saved in [xctrace-capabilities.txt](xctrace-capabilities.txt).

Available standard templates were Activity Monitor, Allocations, Animation Hitches, App Launch, Audio System Trace, CPU Counters, CPU Profiler, Core AI, Data Persistence, File Activity, Foundation Models, Game Memory, Game Performance, Game Performance Overview, Leaks, Logging, Metal System Trace, Network, Power Profiler, Processor Trace, RealityKit Trace, Swift Concurrency, SwiftUI, System Trace, and Time Profiler.

The relevant installed instruments included Display, Core Animation Activity/Commits/FPS/Server, GPU, Metal Application, Metal GPU Counters, Points of Interest, `os_signpost`, and `os_log`. Captures used Metal System Trace with Display and Points of Interest, attached to the controlled process and exported with `xctrace`. The Display export included the `display-vsyncs-interval`, `displayed-surfaces-interval`, `ca-client-present-request`, and `ca-client-presented-handler` tables. XML TOCs and selected tables are preserved for the 45-second static controls, source-only run, F run, and both motion probes.

The downstream instrumentation emits public OS signposts in subsystem `org.fgmetal.dxmt`, category `PointsOfInterest`: `DISPLAY_CALLBACK`, `TICK_ENQUEUED`, `WORKER_WAKE`, `OUTPUT_SELECTED`, `PRESENTER_BEGIN`, `PRESENTER_END`, `COMMAND_COMMITTED`, `DRAWABLE_PRESENT_CALLED`, `PRESENT_GPU_COMPLETED`, and `DRAWABLE_FEEDBACK`. Events carry compact tick/drawable/attempt/source/pair/epoch/output identifiers. The callback path uses a fixed event macro without formatting or allocation.

**VERIFIED smoke/cost check:** the short static export is [smoke-v6-selected-trace.xml](smoke-v6-selected-trace.xml), with its TOC and signpost-off control TOC beside it. It contains VSync, Metal, and signpost rows and correlates `ca-client-present-request` to `DRAWABLE_PRESENT_CALLED` and handler rows to `DRAWABLE_FEEDBACK`. The signposts-on/off control retained about 60.29 versus 60.30 callbacks/s. Callback-duration p95 was 44.17 µs with signposts and 23.63 µs without; the observed approximately 20.5 µs increase did not move callback cadence. Native smoke outputs are under [signpost-on-smoke](runs/signpost-on-smoke/) and [signpost-off-control](runs/signpost-off-control/).

Correlation uses monotonic nanosecond timestamps and the compact IDs in native CSV/signpost rows. The Display and Core Animation rows are independently inspected by process, surface, frame label, direct-to-display status, and interval. In static trace controls, the Core Animation present-request rows match the app’s present-call signposts within 1 ms, while presented-handler rows match the app feedback signpost within about 0.01 ms. Thus those Core Animation rows confirm the same request/feedback chain; they are not an independent count of physical scanout.

Apple documents the Display instrument as a way to analyze display events, and describes long-lived displayed surfaces as a source of visible stutter in [Analyzing performance of your Metal app](https://developer.apple.com/documentation/xcode/analyzing-the-performance-of-your-metal-app/). `MTLDrawable.presentedTime` can be zero when a drawable was not presented or was dropped, so zero alone cannot distinguish those cases ([API reference](https://developer.apple.com/documentation/metal/mtldrawable/presentedtime)). The feedback handler and drawable timestamp are separate from GPU completion ([presented handler](https://developer.apple.com/documentation/metal/mtldrawable/addpresentedhandler%28_%3A%29)). `targetTimestamp` and `targetPresentationTimestamp` are separate callback values ([CAMetalDisplayLink update](https://developer.apple.com/documentation/quartzcore/cametaldisplaylink/update?changes=latest_ma_10_8_8)).

## Timestamp semantics

All internal event fields use one monotonic nanosecond epoch derived from `mach_absolute_time()`. Core Animation time values from the display-link callback are converted into that epoch using the recorded clock bridge.

| Field | Meaning in this run |
|---|---|
| Source arrival | CPU monotonic time when the D3D11 Present path admits/captures a source for escrow; it is not a source-media timestamp. |
| SourceEscrow command submission | The ordinary app-render command buffer, with its mandatory escrow copy encoded in the same buffer, is committed. |
| SourceEscrow completion | Completion-handler time after that same command buffer’s GPU work and immutable escrow copy finish. |
| Pair midpoint | Integer midpoint of A and B source-arrival timestamps, rounded down by integer division for an odd-nanosecond interval. |
| G requested | Time the generation worker has the exact A/B escrow pair and requests a generated frame. |
| G submitted | Time immediately after the Framegen submit call returns; distinct from the later ready/completion time. |
| G ready | Time the generated-frame completion is reported to the bridge. |
| Display-link callback | Monotonic timestamp at callback entry. |
| `targetTimestamp` | Converted display-link render deadline, not the callback time or presentation target. |
| `targetPresentationTimestamp` | Converted system estimate of when the next frame will display; it is not a guarantee of display or physical scanout. |
| Worker selection | Monotonic timestamp immediately before output selection for the callback tick. |
| `drawable.present()` call | CPU timestamp immediately after the public present call returns, after command-buffer commit. It records a request to present, not proof of display. |
| Presentation GPU completion | Command-buffer completion-handler time; proves GPU completion only. |
| `MTLDrawable.presentedTime` | Public host timestamp for when the drawable was displayed onscreen, delivered by the presented handler. Apple documents zero when the drawable has not been presented or its frame was dropped; zero alone does not distinguish those cases. |

## Static controls and preferred frame latency

Apple Display event rates below use adjacent-row gaps: for `N` event rows spanning `T` seconds, the rate is `(N - 1) / T`. Row counts and gap rates are both shown where the distinction matters.

Both controls used the same app binary, one owner epoch, and a static texture whose color stayed unchanged for 60 app Presents. The full native summaries and Display exports are preserved as [latency 1](runs/static-latency1/) and [latency 2](runs/static-latency2/).

| Run | App Presents/s | Callbacks/s | Submissions/s | GPU completions/s | Positive `presentedTime`/s | VSync/s | Wine surface gap rate/s |
|---|---:|---:|---:|---:|---:|---:|---:|
| 45 s, preferred latency 1 | 30.038 | 59.987 | 59.654 | 59.654 | 57.986 (75 zero results) | 59.976 | 7.625 |
| 45 s, preferred latency 2 | 30.035 | 59.936 | 59.781 | 59.781 | 57.202 (116 zero results) | 59.903 | 8.240 |

For latency 2, positive-feedback spacing was p50/p95/p99/max **16.666/44.731/46.046/50.936 ms**. For latency 1 it was **16.666/34.001/46.385/96.594 ms**; another native-only latency-1 repeat measured a 45.162 ms p95 tail. No meaningful stability improvement from latency 2 was demonstrated, so the low-latency default of 1 remains.

VSync intervals were near 16.667 ms: latency 2 p50/p95/p99/max **16.666/16.667/16.667/33.333 ms**; latency 1 **16.666/16.667/16.667/33.333 ms**. Core Animation present requests were approximately 59.713/s and 59.842/s; handler rows were approximately 59.903/s and 59.929/s. The handler-to-app-feedback match was within 1 ms for every matched row.

Wine-attributed surface intervals in the static traces had p50/p95/p99/max gaps of **133.332/333.330/479.661/666.659 ms** for latency 2 and **133.332/322.496/649.326/1049.988 ms** for latency 1. Their durations clustered near 65–66 ms. Since the harness deliberately reused identical content, these rows do not by themselves establish dropped changed frames.

## Source-only 60-second control

**VERIFIED**, fresh engine/prefix, visible foreground window, one owner epoch:

- 1,801 app Presents over 59.968 s: **30.033/s**.
- 3,601 callbacks: **60.049/s**; 3,585 source submissions and GPU completions: **59.782/s**.
- 3,478 positive `presentedTime` results: **57.998/s**; 107 zero feedback attempts.
- 1,800 accepted source IDs; all 1,800 safe; zero lost, unsafe, regressed, or malformed.
- VSync **59.965/s**. Core Animation requests/handlers were **59.849/s / 59.918/s**.
- The trace showed 287 Wine-attributed surface rows spanning about 39.45 s (**286 adjacent gaps, 7.25 gaps/s**), with p50/p95/p99/max gaps **133.332/366.662/638.326/933.323 ms** and median displayed duration **65.596 ms**.

The source-only harness changed color only once per 60 app Presents, so these surface rows are not a motion-calibrated loss count.

## Primary F-equivalent 60-second run

**VERIFIED**, same frozen identity as the static/source runs, fresh engine/prefix, visible foreground window, 60 Hz display, one owner epoch. Full output is in [F-synthetic-60](runs/F-synthetic-60/); exported trace is [F-synthetic60-selected-trace.xml](F-synthetic60-selected-trace.xml).

| Stage | Count/rate |
|---|---:|
| App Presents | 1,801 / 59.979 s = **30.027/s** |
| Display-link callbacks | 3,601 = **60.038/s** |
| Source / generated submissions | 1,807 / 1,783 |
| Total submissions / GPU completions | 3,590 / 59.979 s = **59.854/s** |
| Positive `presentedTime` | 3,482 = **58.054/s** |
| Zero feedback results | 108: 93 generated, 15 source |

The callback ledger passed with 3,601 unique tick terminals and no callback/terminal delta. The 1,800 accepted source IDs all ended safely; lost=0, unsafe=0, source regressions=0, malformed G pairs=0, GPU failures=0. The run had one owner claim/release and drained generation submissions, presentation feedback, GPU work, worker drawables, and ticks.

Native stage latency p50/p95/p99/max in milliseconds:

| Stage | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|
| Callback → worker wake | 0.037 | 0.195 | 3.018 | 33.436 |
| Callback → output selection | 0.061 | 0.280 | 3.390 | 42.920 |
| Presenter encode | 0.109 | 0.402 | 0.739 | 15.180 |
| Callback → command-buffer submit | 0.321 | 1.397 | 5.238 | 16.219 |
| GPU submit → GPU completion | 0.827 | 1.467 | 1.706 | 2.613 |
| Callback → `drawable.present()` | 0.363 | 1.644 | 5.695 | 17.562 |
| Callback → presented feedback | 55.298 | 71.644 | 75.673 | 79.014 |
| Positive `presentedTime` interval | 16.666 | 34.472 | 47.385 | 50.466 |

The trace recorded 3,044 VSync samples spanning 50.716 s (**3,043 adjacent gaps, 60.001/s**); p50/p95/p99/max gap was **16.666/16.667/16.667/16.667 ms**. It also recorded 371 Wine-attributed surface rows over about 37.7 s (**370 adjacent gaps, 9.814 gaps/s**): gap p50/p95/p99/max **116.665/316.663/416.662/516.661 ms**, duration **49.794/66.467/66.540/66.563 ms**, with 347/371 marked “display rejected” and median frame-label step 5. The content was nearly static in this harness, so this primary F trace alone is not the decisive motion result. Its Core Animation request/handler export covered only a short portion and was excluded from full-run rate estimates.

## Escrow-to-G budget and A/G/B chronology

The F-equivalent policy used the exact source pair A/B and submitted G at the integer midpoint. No Candidate-I early-B policy was enabled.

| F timing interval | n | p50 ms | p95 ms | p99 ms | max ms |
|---|---:|---:|---:|---:|---:|
| B escrow complete → G submit | 1,800 | 0.193 | 1.132 | 2.315 | 5.788 |
| G submit → G ready | 1,800 | 13.910 | 15.251 | 19.708 | 29.368 |
| G ready → selected tick | 1,785 | 16.674 | 17.008 | 19.300 | 32.705 |
| G ready → G target | 1,800 | 51.149 | 52.841 | 56.177 | 65.576 |
| G ready → B/display target | 1,785 | 66.516 | 66.608 | 66.616 | 76.592 |

The native records tie each generated request to its exact A and B source IDs. There were 1,800 G requests and ready frames, 1,785 selected, 1,783 submitted, 1,690 with positive feedback, 93 zero-feedback results, and 17 unique pairs dropped before submit.

Renderer-feedback chronology:

- 1,657 displayed G records had an immediate A/G/B triple; 33 were unbracketed.
- The immediate-triple analysis reported missing A=13, missing B=20, missing both=0.
- Positive timestamp ordering separately confirmed A<G<B for 1,663 generated results; among positive G results, A feedback was missing for 13 and B feedback for 14.
- Across the 1,800 distinct B endpoints, 1,785 had positive source feedback and 15 did not. Of those positive B source confirmations, 39 occurred before that B source’s scheduled time (median 3.36 ms early). This is renderer feedback, not independent display evidence.
- For G, native feedback was after B’s scheduled source time in 725 records and after B’s display target in 643; 1,047 G records were before B’s display target. Among cases with positive B feedback, no G feedback was later than B’s positive feedback; B feedback was unavailable in 14 G comparisons.

No exact A/G/B set could be matched to Apple’s per-surface Display rows by attempt ID. Therefore independent-trace A/G/B order is **TRACE_INSUFFICIENT**, even where native positive timestamps establish order.

## Independent display-motion probe

The separate probe changed only the harness cadence for its clear color from one color per 60 app Presents to a new color on every app Present. It did not issue additional Presents and did not read pixels back on the CPU. Source and binary identities are in [display-motion-probe-identity.txt](display-motion-probe-identity.txt), with the probe source in [display-motion-probe.cpp](display-motion-probe.cpp).

| Probe | App content / native path | Independent Display result |
|---|---|---|
| Source-only, 30 s | 901 app Presents; color ID changed on all 901 Presents with zero adjacent repeats (four-color palette); 30.055 app Presents/s, 59.910 callbacks/s, 59.744 submissions and GPU completions/s, 58.176 positive feedback/s. All 900 accepted sources safe. | VSync **60.001/s**. 132 Wine surface rows spanning 15.183 s (**131 adjacent gaps, 8.628 gaps/s**). Gap p50/p95/p99/max **116.665/333.330/478.328/533.327 ms**; displayed-duration p50/p95/p99 **65.557/66.530/66.595 ms**. All 132 were not direct-to-display; 121 were marked “display rejected”; median Display frame-label step 6. |
| F with generation, 20 s | 601 app Presents; color ID changed on all 601 Presents with zero adjacent repeats (four-color palette); 30.082 app Presents/s, 59.914 callbacks/s, 1,191 submissions/GPU completions = **59.630/s**, 58.112 positive feedback/s. All 600 accepted sources safe. G: 600 requested/ready, 581 selected, 580 submitted, 578 positive. | VSync **60.001/s**. 59 Wine surface rows spanning 7.233 s (**58 adjacent gaps, 8.019 gaps/s**). Gap p50/p95/p99/max **116.665/321.663/480.995/499.994 ms**; displayed-duration p50/p95/p99 **64.353/66.428/70.448 ms**. All 59 were not direct-to-display; 52 were marked “display rejected”; median frame-label step 6. |

The F motion trace used the same verified staged DLL hashes as the primary candidate but reused that already-staged disposable engine after a failed long trace save; it had a fresh prefix and is secondary corroboration, not a fresh-engine primary comparison. Both traces have shorter Display overlap than the app run, and the F-motion Display trace covered only 7.233 s of surface rows.

**INFERRED:** the repeated ~8/s surface-gap rate, ~65 ms holds, and skipped Display frame labels while the source color changes on every app Present show recurring compositor/surface update coalescing beyond what static pixel reuse can explain. VSync remained at 60 Hz. This does not name every drawable or prove physical scanout.

## Zero `presentedTime` classification

Counts below are feedback results/attempts, not raw native log rows (the native log can emit more than one diagnostic row for one zero result).

| Run | Zero feedback attempts | Independent per-attempt classification |
|---|---:|---|
| Static, latency 2 | 116 | 116 `TRACE_INSUFFICIENT` |
| Static, latency 1 | 75 | 75 `TRACE_INSUFFICIENT` |
| Source-only 60 s | 107 | 107 `TRACE_INSUFFICIENT` |
| F synthetic 60 s | 108: 93 generated, 15 source | 108 `TRACE_INSUFFICIENT` |

The exported Display trace provides app-attributed surface intervals but not a unique drawable/attempt ID that can be joined to each zero result. No zero was forced into `DISPLAY_EVENT_PRESENT`, `DISPLAY_DROP_REPORTED`, `COALESCED_OR_REPLACED`, or `NO_CORRESPONDING_DISPLAY_EVENT`. The motion probes support a system-level coalescing interpretation but do not change the per-attempt classification.

## Interpolation timestamp/fraction regression

The bridge computes the integer midpoint from source timestamps and derives the interpolation fraction from that exact rounded midpoint offset divided by the exact source interval. It no longer assumes that every rounded midpoint is represented by exactly `0.5F`.

The regression in [core_tests.cpp](../../../../tests/core_tests.cpp) uses A=`294470926456000 ns`, B=`294470926573833 ns`, midpoint=`294470926514916 ns`, and a 117,833 ns interval. It verifies that the ratio-derived fraction is accepted and literal `0.5F` is rejected for this pair. The Framegen test, pacing host, and C API test suites passed: **3/3 CTest tests**. `git diff --check` passed. The F run logged zero generated submission failures.

## Lifecycle and failure regression

Full logs and summaries are under [regressions](regressions/).

**Lifecycle, 65 s — VERIFIED PASS for safety/drain:** resize, Framegen disable/re-enable, minimize/restore, fullscreen enter/exit, swapchain recreation, and shutdown all completed successfully. The app was visible for every Present; 30 presents during intentional minimization were marked iconic/occluded. Across the expected eight owner epochs, all 1,722 accepted sources ended safely; lost=0, unsafe=0, regressions=0, malformed G pairs=0. Callback and terminal counts matched at 3,437; presentation feedback, worker drawables/ticks, generation submissions, and synthetic GPU work drained. App exit was 0.

**Failure injections — VERIFIED with one fail-closed caveat:**

| Injection | Result |
|---|---|
| Escrow preflight | No source transferred ownership, so accepted-source safety is vacuous; 132 reserved candidates were cancelled before transfer. Callback ledger passed (544 callbacks/terminals), no unsafe or lost accepted sources, and GPU/worker drain records completed. Persistent injection caused 128 owner claims, 129 release events, and three quiesce failures. The logs state “accepted source still needs display feedback; layer claim retained” and “ordinary nextDrawable is forbidden.” This is a safe fail-closed result, but not a clean quiesce pass. |
| Generator submission | 360 sources accepted and safe; 719 source submissions/GPU completions; callback ledger passed at 60.160/s; no lost or unsafe sources. Generation failure opened the circuit and generated outputs were dropped. |
| Generated completion | 360 sources accepted and safe; 719 source submissions/GPU completions; callback ledger passed at 60.326/s. Three G requests failed to become ready; generated output was dropped and source presentation continued. |
| Generation deadline | 360 sources accepted and safe; 719 source submissions/GPU completions; callback ledger passed at 60.248/s. All 360 ready G pairs missed the injected deadline and were not selected; source output continued. |
| Worker exception after `present()` and before `reportPresentationSubmitted` | The injection fired on 719 ticks. Recovery finalized presentation feedback; 360 accepted sources all ended safely, no lost/unsafe/regressed sources; 719 GPU completions and feedback results drained; callback ledger passed at 60.163/s; one owner claim/release; app exit 0. |

The escrow preflight run also exercised quiesce-failure handling: the 3-second path retained the layer claim and refused ordinary `nextDrawable`. **NOT TESTED as a separate deterministic timeout case:** there is no dedicated safe environment hook for forcing only the quiesce timeout, and no GPU hang/device-wide failure was simulated. The observed preflight-failure path is supporting fail-closed evidence, not a purpose-built forced-timeout trial.

## Other phase results

- **Windowed/fullscreen A/B — NOT TESTED as a matched static comparison.** The lifecycle harness entered and exited fullscreen successfully for about 1.58 s, but did not hold a fullscreen state for a qualified static trace. All primary cadence runs were windowed.
- **Candidate I — NOT TESTED.** The primary F result and independent motion traces are unfavorable; no scheduler-policy comparison was run.
- **`presentedTime` delayed re-read — NOT TESTED.** Public documentation does not provide a safe lifetime guarantee for retaining/re-reading drawables at 1, 2, and 4 display intervals without risking drawable pressure. No drawables were retained for this experiment.
- **15-minute soak — NOT RUN.** The soak eligibility condition failed because the motion-calibrated Display traces show recurring surface update loss/coalescing.
- **Staging/rollback — VERIFIED carried forward from the audited Step 10B.4 checks.** Fresh-copy refusal, symlink-target validation, rollback after injected install failure, and rollback after SIGTERM after rename had already passed. Step 10B.5 did not alter the runner, stager, or path-safety scripts; their hashes remained frozen. Every new run verified staged DLL hashes. All engines and prefixes were disposable under `/private/tmp`; no installed Highball engine was changed.
- No screen capture, CPU pixel readback, private API, D3DMetal internal inspection, extra DXGI Present, RIFE implementation, Highball modification, source-format broadening, or upstream contribution was made.

## Exploratory anomaly comparison

These remain excluded from acceptance evidence, and their cause is **UNEXPLAINED** because more than one run variable differs and no exact-binary rerun isolated one factor.

- The static B-v2 anomaly used harness SHA-256 `125061f3552938624a0aa37c21237c6d0f0cb78a55d9f92ff07647d224d55729`, a static-texture run, and the older staged engine path `/private/tmp/dxmt-step10b3-run-v8/engine`. It had 30.039 app Presents/s, 60.079 callbacks/s, 59.945 source submissions/s, one owner epoch, and only 70 positive feedback events over 30 s (about 2.34/s). It had no matching independent Display trace. The current candidate uses a different harness hash and a fresh engine/prefix.
- The earlier foreground source-only anomaly used harness SHA-256 `9a88448479f0ae34eea9f093ec311308f0b10925cdd5d69d920e9f61b36e682a`, the same older engine path, and a different prefix. It was visible on all 1,495 Presents and foreground on 1,491, yet callbacks were 45.486/s with 18 owner epochs, 33 escrow setup failures, and 7 quiesce failures. Four accepted sources (`1:1451`–`1:1454`) were lost/unsafe at shutdown. The qualified Step 10B.5 source-only run used the current harness hash, stayed in one owner epoch, and had zero source-safety violations.

The anomaly logs show that foreground loss alone does not explain the earlier source run. The different harness binaries, older staging path, prefixes, and lifecycle/error behavior prevent a causal attribution. Strict qualification remains necessary.

## Final decision and next recommendation

**FAIL — RIFE NOT CLEARED.** Callback/submission/GPU cadence and source safety passed in the qualified primary runs. The independent 60 Hz VSync trace did not establish stable app-surface cadence: per-Present color-motion probes repeatedly showed roughly 8–9 consecutive Wine surface gaps/s and ~65 ms holds, with skipped Display frame labels. Per-attempt Display association and physical scanout remain unverified; the per-attempt zero-feedback classes remain `TRACE_INSUFFICIENT`.

Next, keep the F policy and binaries frozen while running a dedicated, matched windowed-versus-fullscreen motion probe with the public Display and Core Animation instruments. Record the source color IDs and Display surface/frame labels together, and only claim per-drawable outcomes if the public export provides a defensible ID mapping. Investigate the shared presentation/compositor path before changing timing policy or starting RIFE.

## Erratum — Xcode 27 Display export field order

Added after the Step 10B.6 native-control calibration. The selected Xcode 27 `displayed-surfaces-interval` schema places `Duration` before the separate `CPU to Display Latency` field. The source-only and F-motion rows above label values near 65 ms as displayed duration; those values were read from the CPU-to-display-latency column. The original selected XML exports are unchanged.

Re-reading the motion traces with the corrected field mapping gives actual `Duration` p50/p95/p99/max of **116.665/333.330/483.328/533.327 ms** for source-only motion, and **116.665/316.663/466.661/499.994 ms** for F motion. The approximately 65 ms values remain CPU-to-display latency, not surface duration. This corrects the field interpretation only; it does not create a per-drawable Display-row mapping or establish physical panel scanout.
