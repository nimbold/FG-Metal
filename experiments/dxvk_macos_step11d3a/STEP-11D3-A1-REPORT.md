# STEP 11D.3-A1 — Infrastructure Audit and Native MoltenVK Baseline

- **Report date:** 2026-10-08
- **Repository:** `/Users/nima/Documents/Code/FG-Metal`
- **Pinned MoltenVK:** v1.4.2, commit `db66022459ffb663aa2b50f6b018bc2e124f5edf`
- **Primary control architecture:** x86_64, expected to run translated under Rosetta
- **Graphics execution:** none

## 1. Executive summary

The A0 infrastructure was audited against source and retained STEP 11D.2 evidence. That evidence shows the integrated Wine/MoltenVK path used an x86_64 process under Rosetta, so x86_64 is the correct primary native control architecture. The MPSC ring uses per-slot generation publication with release/acquire ordering; a two-million-event contention run passed normally and under ThreadSanitizer. The validator joins the acquired swapchain/image identity through the drawable and presentation lifecycle, and the runner requires a valid quantitative trace before returning PASS.

The first fresh BuildLease attempt exposed incorrect patch hunk counts; an exact prepared-source identity test now catches truncation. A second attempt passed patch/source identity checks and launched Xcode, but Xcode failed while loading the project under the confined build environment before compilation. Its retained log reports CoreSimulator service initialization failures and an empty-string filesystem representation error. The original BuildLease failure capture also masked the child exit because its own log remained open; that cleanup ordering is fixed and covered by a test using the real liveness check. Both build trees were removed. Gate 0 was refreshed after this storage-control fix, and its required PrefixLease certification smoke passed. No native executable or MoltenVK dylib exists, so NativeRunLease, graphics smoke, and five-minute baselines were not run.

## 2. Final classification

**STEP 11D.3-A RESULT: CASE A3 — INCONCLUSIVE**

The required real build lifecycle did not complete. There is no native trace from which to classify drawable behavior. This is an infrastructure failure, not evidence for either reproduction or native health.

## 3. STEP 11D.2 architecture-chain audit

The retained STEP 11D.2 diagnostic was `diag-60s-07-submit-reclaim-split`. Its process sample reports `Code Type: X86-64 (translated)`. The sampled stack includes `thunk64_vkQueueSubmit2 (winevulkan.so)`, `vkQueueSubmit2 (libMoltenVK.1.dylib)`, `MVKQueue::submit`, `MVKPresentableSwapchainImage::getCAMetalDrawable()`, `-[WineMetalLayer nextDrawable]`, and `-[CAMetalLayer nextDrawable]`, followed by a semaphore wait. The MoltenVK call and the in-process `CAMetalLayer` caller were therefore executing in the translated x86_64 Wine process.

| Layer | Observed architecture and evidence |
|---|---|
| Application | `slow-visual.exe` is PE32+ x86-64. It ran inside the sampled x86_64-translated Wine process. |
| Wine process | x86_64 translated under Rosetta, from `os-sample-01.txt`. |
| DXVK PE context | `d3d11.dll` and `dxgi.dll` are x86-64 PE DLLs loaded as native Wine PE modules in that process. |
| `winevulkan` PE module | x86-64 PE DLL. |
| `winevulkan` Unix module | x86_64 Mach-O `winevulkan.so`, loaded from Wine's `x86_64-unix` directory. |
| Integrated MoltenVK | Actual loaded path was `/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib`. It is a universal x86_64/arm64 Mach-O. The translated x86_64 process selected its x86_64 slice. |
| `vkQueueSubmit2` / `nextDrawable` caller | Wine PID 83220 in the retained sample, x86_64 translated. The stack shows the direct MoltenVK and `CAMetalLayer nextDrawable` calls in that process. |
| CoreAnimation boundary | The in-process caller is x86_64 translated. The architecture of any out-of-process CoreAnimation/window-server service was not separately measured. |
| 11D.2 interop provider | `FGMetalBridge.dll` is x86-64 PE and `fgmetalbridge.so` is x86_64 Mach-O. These were present in the integrated path and are deliberately absent from the native control. |
| Planned native control | x86_64, with `sysctl.proc_translated == 1` required by the runner and validator. The executable was not built, so runtime architecture remains unobserved. |

The direct MoltenVK control removes Wine window integration, DXVK, D3D11, frame generation, and the Vulkan-to-Metal interop provider while preserving the x86_64 MoltenVK execution architecture. STEP 11D.2 did not record a complete comparable set of native display/layer settings, so environment equivalence beyond architecture and the intended swapchain target remains unverified.

## 4. A0 audit findings and A1 changes

The previous A0 report was not accepted without source inspection. This audit found and corrected the following issues:

1. **Architecture:** the A0 report described arm64 as the native architecture. STEP 11D.2 evidence instead shows x86_64 translated Wine and an x86_64 MoltenVK slice. The current experiment plan, build request, native runner, signed NativeRunLease architecture field, and validator target x86_64; the lease rejects architecture mismatches, unknown values, malformed Mach-O, and unsupported fat binaries. This is the correct primary control architecture.
2. **Ring capacity discrepancy:** the A0 report claimed 131,072 entries. The implementation and logger summary use 32,768 entries. The validator now requires the trace to report 32,768; this discrepancy is documented here.
3. **Trace identity joins:** the initial validator did not anchor every Metal/presentation/lifetime record to the acquired swapchain and image. The validator now enforces exact `swapchain_id`, `image_id`, `image_index`, and `acquisition_sequence` equality along those edges, including `make_available`, and requires the app's `VkImage` handle to equal MoltenVK's acquired image handle.
4. **Runner PASS semantics:** the runner previously allowed a nonempty trace and zero process exit to yield PASS before offline validation. It now runs the validator with the expected architecture, Rosetta state, exact dylib path, and mode duration, records a compact validation summary, and reports PASS only for a valid, quantitative-ready trace and a clean Gate 0 POST check. A window-close before the requested deadline fails validation.
5. **Patch integrity:** the first real attempt showed that unified-diff hunk counts can silently truncate added files. The hunk counts were corrected, `prepared-source-identity.json` was rebound to the patch hash, and `test_moltenvk_patch_identity.py` now applies the exact patch in a temporary Git checkout and checks every prepared-file hash.
6. **Build failure cleanup:** on a nonzero child exit, `BuildLease.run()` previously called failure retirement while its own `build.log` descriptor was still open. The liveness guard mistook the supervising process for a live build and masked the child result. Failure capture now runs after the log closes; `test_failed_build_closes_supervisor_log_before_liveness_check` exercises the real `lsof` path and confirms the tree retires and the actual synthetic return code is retained.
7. **Gate 0 freshness:** changing `build_retention.py` and its tests correctly blocked stale admission. The full 279-test suite and required PrefixLease certification smoke were rerun, and a fresh signed final attestation now passes.

The A0 report's earlier storage figures (about 42.45 GiB scoped usage and 76.24 GiB free) differ from the final Gate 0 inventory (40.98 GiB scoped usage and 81.05 GiB free). These are time-specific measurements; the final signed inventory and later read-only check are recorded below.

## 5. Ring-buffer concurrency audit

The implementation is a bounded multi-producer/single-consumer ring with 32,768 slots. A producer reads the enqueue position, checks the slot generation, and claims the enqueue ticket with a bounded compare/exchange loop. A failed full/contention attempt does not advance the enqueue position and cannot leave a queue hole. After writing the ordinary payload fields, the producer release-stores the published sequence. The sole consumer acquire-loads that sequence before copying the payload, then release-stores the next reusable generation. The producer acquire-loads the generation before reusing a slot. The counter stops before unsigned wrap could make an old generation appear current.

`ProducerGate` combines a closed bit and an active producer count in one atomic word. Closing prevents new producers from publishing and allows shutdown to wait for admitted producers. Closed-gate attempts balance their temporary count but do not mutate the final rejection summary. The drain thread remains the only consumer, drains published records through FIFO order, exits after the gate is closed and the ring is empty, and is joined before the summary is written and the descriptor is synced and closed.

The hot producer path uses fixed stack buffers and bounded atomic publication. Source inspection found no direct file I/O, flush, Objective-C logging, heap allocation, or blocking mutex acquisition in event emission. The formatter calls `vsnprintf` and obtains the thread ID with `pthread_threadid_np`; the internal allocation/locking behavior of those library calls was not established. Therefore the source supports a low-overhead design, but a strict claim that every helper is allocation-free and lock-free is unverified.

### Stress and informational emission benchmark

The stress payload includes a process-wide event ID, Mach timestamp, thread ID, producer/ordinal, and four integrity words. Four producers make two million attempts while one consumer is deliberately delayed; the test checks exact payloads, no torn or fabricated records, no duplicate IDs, and reconciles all accepted events and counted drops. A separate shutdown test closes admission while producers are active and checks full-ring drops, late closed-gate calls, drain completion, and stable final rejection accounting.

| Run | Shutdown test | 2,000,000-event stress | Emission cost, 100,000 samples |
|---|---|---|---|
| Normal | PASS; 80,329 attempted, 1,257 accepted, 79,072 full drops, 4 shutdown rejects, 80,000 late rejects, 1,257 drained | PASS; 1,856,826 accepted, 143,174 counted drops, 247,939,375 ns, 7,489,032 accepted events/s | min 291 ns; median 375 ns; p95 416 ns; p99 500 ns; max 8,083 ns; 2,597,622 emissions/s |
| ThreadSanitizer | PASS; 10,020 attempted, 6,949 accepted, 3,071 full drops, 4 shutdown rejects, 80,000 late rejects, 6,949 drained | PASS; 633,682 accepted, 1,366,318 counted drops, 537,451,167 ns, 1,179,050 accepted events/s | min 1,291 ns; median 1,541 ns; p95 2,000 ns; p99 2,583 ns; max 41,000 ns; 531,178 emissions/s |

No TSan diagnostics were emitted. These measurements are informational synthetic buffer costs; they were not used to alter presentation scheduling and do not measure MoltenVK's actual graphics-path overhead.

## 6. Trace-correlation graph and validation model

The validator reconstructs a graph of event identities rather than assuming every event shares a single tuple:

```text
APP acquire (app_frame_id, app acquire sequence, swapchain, VkImage, image index)
  └─ unique same-thread API interval + swapchain/image index + exact VkImage handle
     → MVK vkAcquireNextImageKHR (acquisition_sequence, swapchain, image, image index)
        └─ explicit acquisition_sequence + image index
           → acquire_image_assigned
              └─ explicit frame/image identity
                 → command recording
                    └─ unique nested same-thread API interval + queue identity
                       → MVK vkQueueSubmit2 (driver_submit_seq)
                          └─ explicit TLS app_frame_id + driver_submit_seq +
                             acquisition/image identity + request_id/attempt_index
                             → nextDrawable begin/end (drawable_id, texture_id)
                                └─ unique nested same-thread API interval + queue identity
                                   → MVK vkQueuePresentKHR (driver_present_seq)
                                      └─ explicit app/acquisition/image/present identity
                                         → present_request
                                            └─ explicit drawable + texture + image identity
                                               → Metal present request / handler registration
                                                  → presented callback + normalized presentedTime
                                                     → command completion / release /
                                                        availability signal / make_available
```

The application-to-driver API edges use unique same-thread interval containment plus queue identity. Acquire-to-image and drawable-to-present/lifetime edges use propagated identifiers. Any edge without a unique explicit or deterministic association is rejected as invalid/ambiguous. The validator does not join records by nearest timestamp alone.

In pristine MoltenVK v1.4.2, `MVKSwapchain::getImages()` returns each presentable-image pointer cast as the Vulkan `VkImage` handle. That source relationship supports the validator's exact equality check between the app acquire handle and MoltenVK's acquired image identity. Source hashes for the relevant pristine files are listed in Section 8.

Every record carries an event type, process-wide atomic event ID, thread ID, and monotonic timestamp captured at the event site. File/drain order is not treated as event order. Event IDs are checked for uniqueness and unexplained gaps; per-thread Mach timestamps and API intervals are checked for regressions and ordering, with event ID as a tie-break/debug identity. `presentedTime` is normalized to Mach absolute nanoseconds and is checked against callback delivery in that clock domain. The separate `VK_GOOGLE_display_timing` nanosecond fields stay in their declared Vulkan timing domain and are never subtracted from Mach timestamps.

No native trace was produced in this attempt, so these runtime trace properties have only synthetic/source validation, not a live MoltenVK trace validation result.

## 7. Source, binary, and evidence hashes

All hashes below are SHA-256. The complete A1 working-source hash list is also saved in `evidence/a1-infrastructure/a1-hashes.txt`.

### A1 control and validator source

| File | SHA-256 |
|---|---|
| `run_native_control.py` | `0e979317101bcdc79c061f0903c12eefd9219bae6a820cf1b70c950408b982b6` |
| `build_native_moltenvk.py` | `44f4290134ea1a3bd9427d5c0e402b6f958eb33f183f2a489beaee343fd72e36` |
| `native_control.mm` | `f4e8e87cc2653360021b5b802ced745748d081d9b08065bb2deaa2592a15d501` |
| `moltenvk-instrumentation.patch` | `64645b52ecbaf4be617533b01ea794b4f7ecb13c1a54bcd57b3c0701b1264f47` |
| `prepared-source-identity.json` | `2b79d797a556cbec663f40a1bc39f03b94b9a56c6f527b534fd2693914851c51` |
| `experiments/storage/storage_policy.py` | `5885dd97e4e5fb7fa767a43a4063465ca354ffc18724a5f0846638086507dc9f` |
| `experiments/storage/build_retention.py` | `db8021636849860a1cc70b07f22037531301798628f7bbf141c34478fb5ca257` |
| `trace_ring.hpp` | `fd51bd23c088f109f046d3b5dff2c6d7499781fb986e992111b16070f52825d8` |
| `mvk_native_trace_runtime.inc` | `8db51a96f8fd297c5c40f14e89a42f5da4921d9051a63f50db1afadc2dd43aed` |
| `mvk_native_trace.hpp` | `a42bb7eea1aa6e485dcc77ae7b1cbe5a9bff89951cf2ad254001f7dd774baeb3` |
| `native_trace_api.h` | `00804a69f85ecab9370f708546bff04595999eea013db9f41b8e45ec6c183c87` |
| `trace_validator.py` | `9cd02835185dbdc65c0559a57cc784adcc9aa481a25f88ec0ba8e2309ea21b89` |
| `test_storage_policy.py` | `b98b43c885c6cf127ebb68b17d3071d9fd1d0118a1fba02b8f4b3b3d10d095af` |
| `test_build_retention.py` | `4f821d3bab2fa75baae007f868fdcbdd4268611eeb1d30b879ad83eb9d7fbc72` |
| `test_trace_validator.py` | `4cb269d82c7270b780938e9d6e044279ee403ccf01d2c27fc67db06dd27dc81e` |
| `test_native_control_runner.py` | `433ce46f953571ade089745b9f72fa42d369e62031778fe9959bdf779e50bdf0` |
| `test_moltenvk_patch_identity.py` | `ebd45a4d5637ffae6afe17ce5130741db2693bc654b1dd34efff1c3cddcb76e2` |
| `trace_ring_stress.cpp` | `dc88d8161944512309865a302c8fd20a5e8c73b30d4d8998dc345b6ceba0f2ce` |
| `experiment-plan.json` | `ef94b4612da0ba2b4d653c15b6b8d102336ae2d627057c9b8b40789749222315` |
| `experiments/storage/storage_gate0.py` | `791e539862214610c65bdd0453eae8fcd96de37693f87d681246d15822eb4b8a` |
| `experiments/storage/smoke_prefix_lease.py` | `2793ba4b952d284f741681e33b443b3dda55b3178d86ea0659831d214722e406` |
| `experiments/storage/prefix_preflight.py` | `888afd7ac36aa528bdb1548bee5449a688b25824ba7625101f10aea76562233b` |
| `experiments/storage/storage_inventory.py` | `245e6b7ce314791d3542c1f439dbd3ca7a44bca8a12f7684e4ad249d6c747f02` |

### STEP 11D.2 integrated binary identities

| Binary | SHA-256 | Observed format/architecture |
|---|---|---|
| Wine | `946a7032484d02981182d59f82edda03f40f38c510e0208d48482c0830ef20e9` | Executing process x86_64 translated |
| Application `slow-visual.exe` | `f9e581029c215edc2ecad46f5c22cc1b0b0cfc8d17fbaec5f7d96667fc556169` | PE32+ x86-64 |
| `winevulkan.dll` | `a18992aca34493aae325a5c1c6ddc78b905876fb65b2ca91fe7b1882b33fd2dc` | PE32+ x86-64 |
| `winevulkan.so` | `6362dd64890738166dda79bdd9fcc94df55857c1f9756a3268ae91b8d3c38d88` | Mach-O x86_64 |
| Loaded `libMoltenVK.1.dylib` | `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f` | Universal x86_64 + arm64; x86_64 slice used |
| `d3d11.dll` | `cc09178473bef9b6af9fec5821d9d56ad089d61b9daf0af139c132b379f46297` | PE32+ x86-64 |
| `dxgi.dll` | `2c90d3fc3f364a13d003c2dfe1a08ccdffe5bca9a9d6cde8408b65d6a5450f88` | PE32+ x86-64 |
| `FGMetalBridge.dll` | `7a4d53fbf6e4730c93119759d521d91bbf5bfa419bde6df7d63d2ca7b0d500e3` | PE32+ x86-64 |
| `fgmetalbridge.so` | `ecc9ce4c552f093a501ed1ac1186bbd086b2db4465830a07d119a6c4b49dd5d2` | Mach-O x86_64 |

The 11D.2 run directories also contain local symlink materializations of 14 unique DXVK build outputs in the user's external CAS, totaling about 4.22 GB. Those links point to an absolute `/Users/.../Library/Caches/FGMetal` path and are not portable, so `.gitignore` excludes those `d3d11.dll`/`dxgi.dll` outputs from the repository. Their hashes remain in the retained manifests and reports; the binaries remain available in the local CAS for this workspace.

### Pinned-source, Gate 0, and failure evidence

| Evidence | SHA-256 |
|---|---|
| Pristine `MVKSwapchain.mm` | `3615c9417827b1777e5dd0dc7391950065af657155887793f8b31358a770582` |
| Pristine `MVKImage.h` | `24e4fdaf440642c0fe4d9d04d436c89f603411349cf84288dd54878a34cb5c4b` |
| Pristine `MVKImage.mm` | `55639a1dd7c55a58f26c3ed4cab6f461dd12f8ef18fee2954712361f7c428adf` |
| Final Gate 0 attestation | `0b3e55c7655feb616f21ca2a7097052e36445bb3b78a7a6d7d815f2d8c4b631b` |
| Final Gate 0 inventory | `d6473cfbb2810fa8b6bc09ed9b6c93d225b26aaa0a6ceeab9a1588d6323aebfd` |
| Gate 0 full synthetic-suite log | `082b4edf6a99ab2e6718098f0706b626a2abe889e7ffa9022c224f94080942ea` |
| Successful PrefixLease smoke report | `6f6244d0afeba36a4ac0c7d397130276060810677b504581e372ce48de6b9487` |
| Successful PrefixLease cleanup receipt | `4faf0d9f190f54357c12c1468a25e6b099ab1b55ca83800f51366bec607fd443` |
| Attempt `20261008-01` failure manifest | `2d1aab802e18fd88d918d60f993ceef40e8f80afc874a3b8985104640499b312` |
| Attempt `20261008-01` build log | `92f3a0aef7b9c9a5b4ee3f72a5f100641eecead74147ce30cc501b7b0eb9d701` |
| Attempt `20261008-01` cleanup receipt | `ec96e41cc47a1fc4a20b314392d76f7db9b16efe2d982b616d029f95099bb37a` |
| Attempt `20261008-02` failure manifest | `4d0021082f0ee6059d5073725de8618ed1aaacdf7a97eabea22b0cee12bfefc1` |
| Attempt `20261008-02` build log | `205fd0b40f75d83ab0fda314b6e93b5f929866e3200decbe78ff45a6e1abafe5` |
| Attempt `20261008-02` cleanup receipt | `b875de271508591e1383e98c6a8fbc622715e09ec285b370438b10a1a4933dbc` |
| Focused trace validator tests | `230955faea668c6c27a66828d094ab80adf5f47afb7ddeaccc74ea6d6ba1ea5e` |
| Focused native runner tests | `94fbf053c138626905075345829956a328a70d4df94c814f9a2c82100fd18d3a` |
| Storage suite evidence log (279 tests) | `55230f94f542075348e4f33c4870d662607a025066979ded429be6e70b5f3f94` |
| First smoke refusal log (environment sanitizer) | `703dbc32cefee3893a6c1fc38d2a99c0e1d0c4dff4409a249c367c2f938bd9e2` |
| Successful smoke invocation log | `dd3f47ab858140a6642d3576d3828d6f6f4502eb2acafc0e35fd1af05071cfbd` |

The instrumented MoltenVK dylib and native control executable have no output hashes because Xcode failed while loading the project, before compilation or linking.

The oversized STEP 11D.2 raw logs are tracked as lossless gzip archives to satisfy GitHub's per-file limit. The [archive index](../dxvk_macos_step11dR/evidence/STEP11D2-LARGE-LOG-ARCHIVE.md) records both compressed and uncompressed SHA-256 values and the restoration procedure.

## 8. Real build-contract result

Two fresh leased attempts were made after the source audit:

1. **Build ID `step11d3a-mvk-x86_64-20261008-01`:** Gate 0 PRE admission returned `ALLOW`. Source materialization succeeded, but the exact prepared-file hash check found that the unified patch had truncated `MVKNativeTraceRing.hpp`. The patch hunk declared 173 lines where the added content had 177; another hunk undercounted `native_control.mm` by one closing brace. The patch counts and prepared patch hash were corrected. `test_moltenvk_patch_identity.py` now applies the retained patch and verifies every prepared file against the identity manifest. BuildLease captured evidence and removed the root (`REMOVED`). No compiler was started.
2. **Build ID `step11d3a-mvk-x86_64-20261008-02`:** A fresh Gate 0 PRE admission and BuildLease reservation passed. Source materialization, patch application, and prepared-source identity verification passed. The declared `xcodebuild` command launched under the BuildLease Seatbelt profile, then failed while loading `MoltenVK.xcodeproj`, before compilation. Its log reports denied/invalid CoreSimulator service initialization, an empty-string filesystem representation error, and Xcode's generic “project is damaged” message. `plutil -lint`, `xcodebuild -list`, and `-showBuildSettings` succeed outside that profile; a separate `xcodebuild -list` against the pristine pinned project fails under the same profile. This points to Xcode's service access under Seatbelt rather than a malformed instrumentation edit, but the exact failing Xcode dependency is not proven.

The second attempt's retained failure manifest records `return_code=-1` and a liveness error because the then-current BuildLease implementation tried to retire the tree while its supervisor still held `build.log` open. It therefore does **not** preserve Xcode's actual exit code. The build log is retained and shows the project-load failure. The BuildLease failure ordering was fixed afterward, and a focused regression test using real `lsof` plus a synthetic child exit verifies that the log closes before the liveness check and that the true child code is retained.

Both BuildLease attempts auto-retired their temporary roots and have `REMOVED` cleanup receipts. The second root had 211,016,768 logical bytes / 232,185,856 allocated bytes; no declared output was built, hashed, reconciled, or canonicalized, and no successful build manifest exists. This failure triggers the stop rule: no NativeRunLease probe or graphics process was started.

## 9. NativeRunLease result

NativeRunLease was not started because the native executable and instrumented MoltenVK dylib were not built. There was no real loader probe, so loader resolution, translated runtime execution, scratch lifecycle, stdout/stderr supervision, timeout supervision, and native-run POST receipt remain unverified. Source-level and synthetic tests cover signed architecture binding, exact Mach-O/hash checks, x86_64 success, arm64/x86_64 mismatches, unknown architecture, malformed/fat Mach-O rejection, environment sealing, timeout supervision, and cleanup receipts.

After the failed build, the shared active-lease directory contained only its lock file; there were zero active prefix or native run lease markers. The normal Gate 0 PrefixLease certification smoke was separate and is described below.

## 10. Test and independent-review results

| Check | Result |
|---|---|
| Full storage synthetic suite | **279/279 PASS**, `python3 -m unittest discover -v -s experiments/storage -p 'test_*.py'`; includes the BuildLease log-close regression; captured in `evidence/a1-infrastructure/storage-suite-final.log`. |
| Trace validator focused suite | **50/50 PASS**; includes identity mutations, ambiguous joins, shutdown/order checks, timestamp rules, and file-order independence. |
| Native runner focused suite | **3/3 PASS**; valid trace allows PASS, early window-close fails, and mode-duration mismatch fails. |
| Patch/source identity test | **1/1 PASS**; applies the full patch and checks all prepared-file hashes. |
| BuildLease failure cleanup regression | **PASS** within the full suite; return code 41 was preserved and the active build root was removed after `lsof` liveness checks. |
| Ring stress normal | **PASS**, two million producer attempts and shutdown/full-ring checks. |
| Ring stress ThreadSanitizer | **PASS**, two million producer attempts and shutdown/full-ring checks; no diagnostics. |
| Gate 0 PrefixLease certification smoke | **PASS** on clean retry; exactly one temporary prefix was created and removed; no Wine application or graphics process launched. |

An independent GPT-6 Luna Max reviewer inspected the current architecture evidence, ring implementation/stress records, current validator joins, and latest real-build failure. The reviewer passed the source-level x86_64 target, ring publication/shutdown, and swapchain/image joins; actual native runtime architecture remains unobserved because no executable was produced. The reviewer confirmed graphics must not run while the build is unresolved. The reviewer also noted that `vsnprintf` and `pthread_threadid_np` do not establish that every helper is allocation-free or lock-free. The source contains no direct file I/O, Objective-C logging, heap construction, or blocking mutex acquisition in the event path, but strict helper behavior remains unverified. Reviewers did not independently execute the test suites.

The reviewer also noted that partial/unpresented image teardown may emit `make_available` records that fail closed in the validator. That could invalidate a trace, but cannot produce a false quantitative PASS.

A separate earlier review reported that contradictory swapchain identities could pass validation and that Gate 0 storage-control hashes were stale. The old hashes were against superseded files. Current validator `9cd02835…` anchors lifecycle joins to acquired swapchain/image identity, and the 50-test suite includes wrong-swapchain mutations. Current storage-control hashes are bound by the fresh attestation. The remaining A3 result follows from the second real build failing before compilation; this is not a graphics-health result.

## 11. Gate 0 evidence

Fresh preflight returned `PREFLIGHT_PASS`, then the required PrefixLease smoke completed and Gate 0 finalization returned `PASS`. The final attestation SHA-256 is `0b3e55c7655feb616f21ca2a7097052e36445bb3b78a7a6d7d815f2d8c4b631b`. After the README/report changes, live `_gate0_blockers()` returned `[]`; the attestation HMAC and all current retention-control hashes were independently verified.

Certification smoke ID: `step11d3-storage-policy-smoke-20261007-f1a36698bb5a4e3c`. It created one temporary certified-template clone, ran the supervised `/usr/bin/true` control command, and removed the clone. The report states `wine_app_launched=false` and `graphics_experiments_run=false`; baseline and post-cleanup prefix state match. An earlier smoke invocation accidentally inherited `PYTHONDONTWRITEBYTECODE`, which the Wine child environment validator correctly rejected before materialization; that attempt is retained as a failed smoke diagnostic and was followed by a new preflight nonce and successful clean retry.

The final Gate 0 inventory immediately before the native build recorded:

- Free bytes: `86,996,877,312` (81.05 GiB).
- Scoped FG-Metal allocation: `44,006,158,336` bytes (40.98 GiB).
- Active builds: 0; retained builds: 0; preserved builds: 0; unknown roots: 0.
- Preserved prefixes: 2; uncertified prefixes: 0; active leases: 0.

The signed inventory captured at Gate 0 finalization records `86,996,877,312` bytes free (**81.05 GiB**) and `44,006,158,336` scoped bytes (**40.98 GiB**); this remains below the project cap and above the free-space minimum. The later live check is recorded in Section 19. No safety threshold or admission control was bypassed.

## 12. 30-second validation smoke

**NOT RUN.** The native MoltenVK library and executable were not produced. The build failure occurred before compilation, and the stop rule prohibited proceeding to a loader probe or graphics process.

No native trace exists to validate. Thus smoke trace validity, actual CAMetalLayer state, actual Vulkan swapchain state, display identity, effective refresh, and presentation cadence are all unobserved.

## 13. Three five-minute baseline runs

**0 of 3 runs executed.** No baseline admission or graphics process was started because the real build contract failed. There are no per-run hashes, POST receipts, traces, or timing summaries.

## 14. nextDrawable wait histogram

No native `nextDrawable` events exist. Each requested bucket is therefore **not measured**, rather than zero:

| Wait bucket | Count | Percentage | Accumulated wait |
|---|---:|---:|---:|
| `<0.5 ms` | N/A | N/A | N/A |
| `0.5–2 ms` | N/A | N/A | N/A |
| `2–5 ms` | N/A | N/A | N/A |
| `5–10 ms` | N/A | N/A | N/A |
| `10–14 ms` | N/A | N/A | N/A |
| `14–18 ms` | N/A | N/A | N/A |
| `18–25 ms` | N/A | N/A | N/A |
| `25–50 ms` | N/A | N/A | N/A |
| `50–100 ms` | N/A | N/A | N/A |
| `100–250 ms` | N/A | N/A | N/A |
| `250–500 ms` | N/A | N/A | N/A |
| `500–900 ms` | N/A | N/A | N/A |
| `>=900 ms` | N/A | N/A | N/A |

## 15. Percentiles and submit durations

Native nextDrawable min/median/mean/p90/p95/p99/p99.9/max and accumulated-wait statistics: **not available**. Native `vkQueueSubmit2` duration statistics: **not available**. The synthetic emission benchmark in Section 5 measures logger/buffer work only and is not a substitute for either timing distribution.

## 16. Long-wait events and local timelines

No native trace was produced. There are no native events at or above 25, 50, 100, 250, or 500 ms to enumerate, and no app-frame/acquire/submit/drawable/present/callback/availability/release timelines to reconstruct.

## 17. Refresh-phase analysis

No native display or presentation data was collected. Nominal/effective refresh, fitted period, residual/error, request phase modulo the period, phase clustering, and modality are **not measured**. No 60.000 Hz assumption or callback-delivery timestamp was used to infer presentation time.

## 18. Comparison to STEP 11D.2

The integrated STEP 11D.2 evidence recorded LOW WSI throughput of about 44.9–45.6/s and HIGH throughput of about 58.1–59.0/s. One source-ready `vkQueueSubmit2` measurement was 722.783 ms and the following `m_presentPending` observation was 723.305 ms. Separate stack evidence showed roughly 326 ms blocking along `vkQueueSubmit2 → MoltenVK → getCAMetalDrawable → nextDrawable → CAMetalLayer`, including a semaphore wait.

There is no native throughput, wait-tail, sustained cadence, phase, or pathological-stall result to compare. The native experiment neither reproduced nor refuted the integrated symptom. A one-refresh-sized wait would not have been sufficient by itself to claim reproduction; no such native observation exists here.

## 19. Storage PRE/POST and cleanup state

The Gate 0 PRE admission for the fresh build was `ALLOW`. BuildLease failure capture and POST retirement completed successfully. The temporary build root is absent, the cleanup receipt authenticates removal, and the final build-retention snapshot reports zero active, retained, preserved, failed/retiring, and unknown builds.

The Gate 0 final inventory reports 86,996,877,312 bytes free (**81.05 GiB**) and 44,006,158,336 scoped bytes (**40.98 GiB**). The latest read-only inventory check, run after staging the repository changes and written to a temporary path outside the checkout so the signed Gate 0 inventory stayed untouched, reported 86,261,248,000 bytes free (**80.34 GiB**) and 44,053,737,472 scoped bytes (**41.03 GiB**). The live Gate 0 blocker list was `[]`. The retention snapshot has zero active, retained, and preserved builds, Gate 0 status `PASS`, and no retention blockers. There are zero active prefix/build lease markers or native-run leases, two preserved Wine prefixes, and zero uncertified prefixes. Both build roots were retired; the attempt `20261008-02` cleanup receipt measures a 236,130,304-byte free-space increase.

Final state:

- Active builds: 0.
- Retained builds: 0.
- Preserved builds: 0.
- Failed/retiring or unknown build roots: 0.
- Active native run leases: 0.
- Preserved Wine prefixes: 2.
- Uncertified Wine prefixes: 0.
- BuildLease POST cleanup: **PASS** (`REMOVED` / `REMOVED`).
- NativeRunLease POST cleanup: **not applicable; no run started**.
- Gate 0 blockers: `[]`.

The mandatory Gate 0 PrefixLease smoke did create and then remove a temporary prefix; the native experiment itself created no Wine prefix and launched no Wine application.

## 20. Repository check

`git diff --check` and `git diff --cached --check`: **PASS** (exit 0). `.gitattributes` keeps byte-preserved experiment captures and historical patches out of whitespace normalization/checking; project source and the current documentation remain checked. The repository was at `main`, aligned with `origin/main` before this requested commit. The 34 absolute symlink entries to 14 external CAS binaries (4.22 GB unique payload) are excluded from Git. Two raw logs above GitHub's 100 MiB per-file limit remain local and are represented by byte-verified gzip archives with a restoration index. No unrelated worktree changes were discarded.

## 21. Remaining blockers

1. The exact Xcode service/project-load dependency under Seatbelt is unresolved; no successful leased MoltenVK build exists. Preserve write confinement when addressing this.
2. No instrumented MoltenVK binary, native control executable, CAS artifact, or successful build manifest exists.
3. NativeRunLease, actual loader binding, 30-second trace, and all three baseline runs remain unexecuted.
4. Native trace validity, actual display/layer/swapchain configuration, wait histograms, submit statistics, long-wait timelines, and refresh-phase analysis remain unobserved.

## 22. Exact next recommended step

Resolve Xcode's project-load failure under the BuildLease Seatbelt profile without weakening Gate 0 or escaping the leased write root. Then use a fresh PRE admission and build ID to complete the entire BuildLease/CAS lifecycle, followed by a NativeRunLease loader probe. Only after those pass may the 30-second smoke run; valid smoke traces are required before the three five-minute baselines. Do not start STEP 11D.3-B from this A3 result.

## Evidence files

- Infrastructure test and stress logs: `experiments/dxvk_macos_step11d3a/evidence/a1-infrastructure/`
- Attempt `20261008-01` failure manifest/log/receipt: `experiments/storage/build-manifests/step11d3a-mvk-x86_64-20261008-01.failure/` and `experiments/storage/build-cleanup-receipts/step11d3a-mvk-x86_64-20261008-01.json`.
- Attempt `20261008-02` failure manifest/log/receipt: `experiments/storage/build-manifests/step11d3a-mvk-x86_64-20261008-02.failure/` and `experiments/storage/build-cleanup-receipts/step11d3a-mvk-x86_64-20261008-02.json`.
- Gate 0 successful smoke report: `experiments/storage/smoke-runs/step11d3-storage-policy-smoke-20261007-f1a36698bb5a4e3c/prefix-smoke-report.json`.
- Failed smoke environment diagnostic: `experiments/storage/prefix-smoke-run-20261008.log`; clean successful retry: `experiments/storage/prefix-smoke-run-20261008-retry.log`.
- STEP 11D.2 large-log archive index and gzip restoration instructions: `experiments/dxvk_macos_step11dR/evidence/STEP11D2-LARGE-LOG-ARCHIVE.md`.
