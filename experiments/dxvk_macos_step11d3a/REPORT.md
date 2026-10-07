# STEP 11D.3-A — Native MoltenVK Drawable-Starvation Baseline

Status: preparation and read-only admission audit only. No native graphics process, smoke, baseline, or experimental build has run. This document does not establish native presentation health.

## Source and binary identity

The existing pristine source is `/private/tmp/fgmetal-step11b/MoltenVK`, version v1.4.2, HEAD `db66022459ffb663aa2b50f6b018bc2e124f5edf`. Its Git status was empty. The full storage-controller source-tree hash (excluding root `.git`, including dependencies and retained package paths) is `2a51f319d33f6e554a2d5e073ac86a1968f35a5746d298d46f6614b6dcd3f3f5`, 7,631 files. This is not an instrumented source hash or a Git tree ID. Exact Git tree and attestation hashes are in `preflight-audit.json`.

The previous integrated diagnostic pinned dylib was SHA-256 `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f`, as recorded in its runner. No new native executable or instrumented MoltenVK binary exists; their binary hashes are UNAVAILABLE, rather than inferred from source.

## Configuration inherited from 11D.2

`../dxvk_macos_step11dR/evidence/step11d2-diagnostic/diag-60s-07-submit-reclaim-split/slow-visual_d3d11.log`, lines 287–292, records BGRA8 UNORM, FIFO with dynamic support, 640×360 buffer extent, and three images. These are the native control's requested swapchain parameters. The earlier full display identity, effective refresh, layer properties, contents scale, and window dimensions in points were not established by this audit. A future native run must record actual values and report mismatches; the requested values are not measured native configuration.

The native control must leave MoltenVK's normal swapchain setup in place. Pinned `MVKSwapchain.mm` itself assigns maximumDrawableCount from image count and displaySyncEnabled from present mode. The experiment must not introduce overrides to those assignments, nextDrawable timeouts, synchronous queue submission, acquire placement, queue count, or presentation scheduling.

## Public interface basis

The control uses [VK_EXT_metal_surface](https://docs.vulkan.org/refpages/latest/refpages/source/VK_EXT_metal_surface.html), which creates a public Vulkan surface from CAMetalLayer. Public presentation observation uses the existing MoltenVK [MTLDrawable presented callback/time](https://developer.apple.com/documentation/metal/mtldrawable/presentedtime). A callback timestamp is not the actual display timestamp; presentedTime equal to zero means unpresented or dropped, and must not count as a successful displayed frame.

## Per-run results and quantitative evidence

| Run | Requested duration | Execution | Measurements |
| --- | ---: | --- | --- |
| Validation smoke | 30 seconds | NOT RUN | UNAVAILABLE |
| Baseline 1 | 300 seconds | NOT RUN | UNAVAILABLE |
| Baseline 2 | 300 seconds | NOT RUN | UNAVAILABLE |
| Baseline 3 | 300 seconds | NOT RUN | UNAVAILABLE |

Elapsed time, submitted/presented frame counts, WSI presents/sec, acquire/submit/drawable durations, presentation completion intervals, abnormal gaps, percentiles (including p99.9), and the longest 20 waits are UNAVAILABLE. There are no measured samples. Missing samples must not be interpreted as zero long waits.

| nextDrawable bucket | Count | Percentage | Accumulated time |
| --- | --- | --- | --- |
| <0.5 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 0.5–2 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 2–5 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 5–10 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 10–14 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 14–18 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 18–25 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 25–50 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 50–100 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 100–250 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 250–500 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| 500–900 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |
| >=900 ms | UNAVAILABLE | UNAVAILABLE | UNAVAILABLE |

No native >=25, >=50, >=100, >=250, or >=500 ms event can be identified or excluded. No >=100 ms local timeline exists.

## Refresh phase and comparison

Native nominal/effective refresh, observed presentation cadence, request phase modulo the effective period, multimodality, and phase correlations for 10–18, >25, and >100 ms waits are UNAVAILABLE. Do not assume 60.000 Hz or infer phase from callback delivery time alone. The future analysis must fit the effective period to valid actual presentation times, report residuals/clock basis, use fine phase bins, and distinguish callback delivery delay from display gaps.

The 11D.2 report records frozen integrated low runs at 44.898–45.590 WSI/s and high runs at 58.099–58.993 WSI/s, alongside much larger NO_FREE_HISTORY counts in the low runs. Its instrumented source-ready submit tail reached 722.783 ms, overlapped by a 723.305 ms following present-pending wait. Its direct stack sample establishes nextDrawable blocking for a separate ~326 ms event; it does not provide a simultaneous stack sample for the 723 ms event or a fully instrumented 45 WSI/s run.

No native comparison is possible yet. An ordinary single-refresh drawable wait would not independently support instability; throughput, distribution, frequency and long tails remain required.

## Storage evidence

Read-only live Gate 0 check: PASS, blockers `[]`; build retention PASS; active, retained and preserved experimental builds all zero. Live free disk was 75.33247 GiB and scoped storage 41.09268 GiB, with a 45 GiB cap. The signed attestation remains the existing one; this check did not create a signed run admission, refresh the gate, or allocate a build/prefix. See `preflight-audit.json` for exact bytes and timestamp.

Per-run PRE admission and POST cleanup receipts: NOT RUN / NOT APPLICABLE. No completed build exists to artifactize or retire. Final live storage and repository checks will be recorded after source preparation.

## Evidence still required

A policy-compliant native build and managed native launch with automatic POST cleanup and next-run Gate 0 validity; exact instrumented source and executable hashes; successful rendering and complete 30-second smoke traces; three independent approximately five-minute native traces; observed layer/display configuration; per-frame acquire/submit/drawable/presentation/availability/release correlation; histograms, percentiles, worst-event timelines, and actual-presentation phase analysis. Until these exist, A1/A2 would be unsupported.

No prohibited presentation setting was overridden, no Wine/DXVK/bridge code was changed, no STEP 11D.3-B was launched, and no commit was made.

## Native lifecycle blocker and independent assessment

The independent GPT-6 Luna (Max) audit found that `begin_experiment(expected_prefix_bytes=0)` can admit a newly hash-bound native runner without modifying the attested policy. The existing diagnostic runner, SHA-256 `852327da8bef1e417de1f5a11f3b31415c8bf3662c8e57ffa261fc9f3449e67a`, is a Wine/DXVK/bridge launch and cannot execute this native control as written.

`PrefixLease.start_wine_process` is restricted to the certified Wine loader after materialization and environment sealing. Its cleanup owns the supervised-process, prefix, CAS, signed receipt and threshold checks. There is no corresponding native runtime lifecycle in the current controls. Routing a native process through the Wine launcher or disguising it as a build would not satisfy the requested baseline contract.

The native MoltenVK build also lacks a demonstrated fresh-build contract: `BuildLease.reserve` demands a frozen source and configuration key, while Xcode completion recomputes that key using generated `Info.plist` and complete retained build-request records. The previous MoltenVK build manifest is an adopted legacy record, rather than proof of that fresh path. The Make capture assigns `unknown-legacy` build type and completion rejects unsupported build systems. These are unresolved infrastructure requirements, not a measured storage threshold rejection, and not evidence of a MoltenVK graphics defect.

To make the experiment executable, the controls need a verified native process/POST lifecycle and a fresh, prebindable MoltenVK build contract, with any modified attested controls tested and Gate 0 refreshed. This step leaves those controls unchanged and does not start an unleased or unsupervised run. See `storage-review.md` for the independent source references and exact limitation.

## Prepared control and instrumentation

`native_control.mm` prepares an AppKit window and CAMetalLayer, creates a public VK_EXT_metal_surface Vulkan surface, chooses one graphics/presentation queue, requests FIFO/BGRA8 UNORM/640×360/three swapchain images, and clears renderer-owned swapchain images through a Vulkan render pass. It uses three frame slots, per-image render-finished semaphores, vkQueueSubmit2, and ordinary vkQueuePresentKHR. A renderer worker submits frames while the main thread services AppKit and observes layer/window/display state; there is no frame-generation scheduler, bridge, capture or pixel readback. Optional VK_GOOGLE_display_timing queries observe public presentation records with desired presentation times left unset. These describe prepared source, not an observed native run.

The app records layer properties at startup, delegate notifications and 100 ms polling. Polling can miss intermediate state transitions; it has not been demonstrated to satisfy complete change tracking. Windowed native AppKit and three frame slots are explicit control choices; the integrated layer/display/window-in-points and queue-depth state remains unverified. App and driver acquisition sequence numbers are distinct and require an explicit cross-trace mapping.

`moltenvk-instrumentation.patch` changes five upstream files (383 additions, 14 removals): MVKImage.h/mm, MVKSwapchain.h/mm, and Vulkan/vulkan.mm. It adds opt-in JSONL acquire/submit/present boundaries, image assignment, exact nextDrawable bounds, drawable/texture identities, public presented callbacks, command-buffer completion/reference-release observation, and availability transitions. Pointer identities are process-local; `event_id` is file order rather than chronological order across threads. Explicit call bounds are required for phase analysis.

The patch SHA-256 is `a9795dae571d4ea50cede0c50973b6f2298c1e7caeb19be638697114936d1cef`. `prepared-source-identity.json` binds the pristine complete tree, exact Git commit/tree, patch, and original/prepared SHA-256 for all five changed files. Patch application was simulated in memory only. The pristine source was not edited and no instrumented tree was materialized.

Static review found synchronous file logging can perturb scheduling, including while a driver availability lock is held; this overhead is unmeasured. Generic driver submit events lack direct application frame IDs. A future analyzer must join app frame IDs through the same-process thread/API intervals, image identity and acquire ordinal, then carry the driver's acquisition sequence. Image/reference lifetime and trace completeness still need the smoke. The instrumentation is not baseline-ready simply because apply-check passes.

Independent GPT-6 Luna (Max) reviews are retained in `storage-review.md` and `independent-review.md`. The reviewer supported preparation-only/A3 evidence boundaries, identified the acquisition-error output-read issue (now guarded on SUCCESS/SUBOPTIMAL and index range), and documented logging and frame-join limitations. No reviewer ran a native experiment.

## Preparation verification and final result

The native app source passed `xcrun clang++ -std=c++17 -fobjc-arc -fno-modules -fsyntax-only` with the pinned Vulkan-Headers include path, exit 0 with no diagnostics. This produced no object or executable. Draft namespace/header/overload and metadata defects were corrected before this final check. The MoltenVK patch passed `git apply --check --whitespace=error` against the pristine pinned checkout, whose Git status remains empty. The patch itself is UNCOMPILED and UNRUN. `git diff --check` passed; because these preparation artifacts are untracked, their trailing whitespace was checked separately. See `verification.json`.

Final sampled storage at 2026-10-07T17:54:58.206569+03:30: free **75.32 GiB**, scoped **41.09 GiB**, cap **45 GiB**. Gate 0 and retention status PASS; blockers []; active/retained/preserved builds **0/0/0**; preserved/uncertified prefixes **2/0**. The five pre-existing tracked modifications remain present. `final-storage-audit.json` retains exact byte values and identities. This is a read-only ending snapshot, not a per-run POST cleanup receipt.

Native app source SHA-256: `06afa905af4c694546b238e5bc03263b768a7eb610b8bc8e8df4a1495754a816`. Source/preparation hashes are in `artifact-hashes.json`; native executable/instrumented dylib hashes remain UNAVAILABLE.

The missing execution lifecycle and fresh-build provenance support prevented a compliant native smoke and baseline. No native wait distribution, sustained throughput, long-tail or refresh-phase evidence exists. This result says nothing about whether native MoltenVK reproduces the integrated symptoms. Storage-control infrastructure must first provide a verified build/run/POST path; the graphics experiment must then be executed under fresh admissions. No performance fix, setting sweep, Wine branch, STEP 11D.3-B, commit, or push was performed.

STEP 11D.3-A RESULT: CASE A3 — INCONCLUSIVE
