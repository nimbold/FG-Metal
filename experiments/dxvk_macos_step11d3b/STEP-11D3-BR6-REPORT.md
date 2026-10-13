# STEP 11D.3-BR6 — Transparent GDPA, Gate 0, and Bare Wine Vulkan B

- **Report date:** 2026-10-10 (Asia/Tehran)
- **Workspace:** `/Users/nima/Documents/Code/FG-Metal`
- **Final classification:** `STEP 11D.3-B RESULT: CASE B3 — INCONCLUSIVE`
- **Scope:** direct delegate GDPA initialization, formal 45/45 Gate 0, official smoke, and three accepted 300-second B baselines. No STEP 11D.3-C work was started.

## 1. Executive summary

BR6 removed the app-order dependency that caused BR5: the proxy now resolves `vkGetDeviceProcAddr` directly from the exact MoltenVK delegate during proxy initialization. The device-dispatch suite passed 15/15; the full storage test discovery passed 380/380. The original late-query application order was restored.

The fresh official 30-second smoke passed and produced a signed authorization. All three accepted 300-second baselines passed trace validation, Gate 0 PRE/POST, process exit, and PrefixLease cleanup. Their presentation results conflict: Baselines 1 and 3 displayed 59.75 and 59.84 FPS, while Baseline 2 displayed 5.16 FPS with 95.44% of acquired frames unpresented and one 1.010-second `nextDrawable` wait. The smoke itself measured 31.33 displayed FPS, and the user observed fast flicker with dark/white frames most visible.

Because severe instability did not repeat across the accepted baselines and the run-to-run behavior differs materially, the evidence supports **B3 — INCONCLUSIVE**. It does not support a stable healthy-B2 conclusion or a reproducible B1 conclusion. The Baseline 2 trace is valid and remains part of the evidence; it is not discarded as an outlier.

## 2. B classification

**STEP 11D.3-B RESULT: CASE B3 — INCONCLUSIVE**

Accepted displayed-FPS range: **5.163–59.843 FPS**. Baselines 1 and 3 are near native cadence; Baseline 2 is pathological. The single severe baseline and the user's visual report warrant follow-up, while the two healthy baselines prevent calling the behavior reproducible. No causal root cause is established by these runs.

## 3. BR5 accepted crash proof

BR5 identified the primary fault as device proc-address resolution and the manifestation as Wine `win32u.so` device dispatch-table construction. The proxy constructor called GIPA with `VK_NULL_HANDLE` for `vkGetDeviceProcAddr`; exact MoltenVK 1.4.2 correctly returned `NULL`. Wine then stored a null `vkGetDeviceQueue` pointer and faulted at `callq *0x1048(%r14)` with `ip=0`, `addr=0x0`, and `0xc0000005` inside `win32u_vkCreateDevice`.

BR5's early-GDPA app workaround proved the diagnosis but is not retained as the instrumentation contract. See the accepted evidence in [BR5 report](STEP-11D3-BR5-REPORT.md), especially its crash trace and disassembly.

## 4. Proxy transparency issue

The earlier proxy depended on the PE application requesting GDPA through a valid-instance GIPA call before Wine's internal GDPA lookup. That call-order dependency was not transparent: Wine directly resolves the proxy's exported GDPA with `dlsym`, and may need device functions before the PE application's later query.

## 5. Direct delegate GDPA resolution

`initialize_proxy()` now resolves `g_real_gdpa` with `dlsym(original_handle, "vkGetDeviceProcAddr")` while loading the delegate. On the baseline-3 delegate, `nm -arch x86_64 -gU` found the exported `_vkGetDeviceProcAddr` at `0x10780`; `otool -arch x86_64 -tvV` showed its function body and `otool -l` showed `LC_DYLD_EXPORTS_TRIE`. The runtime dlsym probe resolved the symbol and `dladdr` attributed it to the exact delegate. The x86_64 slice UUID was `C178CB05-A1CA-34F9-8ADD-7671DC15A86B`; SHA-256 was `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f`.

## 6. Recursion and ownership proof

Proxy initialization checks `dladdr` ownership and the resolved image path/hash against the selected delegate. It rejects a GDPA pointer that equals the proxy export or whose image/hash does not match the exact delegate. `proxy_gdpa` treats a non-null `VkDevice` as opaque and passes it to the delegate without inspecting or dereferencing it. The 15-test suite passed direct resolution, delegate ownership, pointer distinction, no-recursion, and fail-closed wrong/absent-delegate cases. Runtime traces also bound the proxy and delegate paths and hashes for all accepted baselines.

## 7. Late-query test

The PE source has the canonical late GDPA query order again; it no longer makes the BR5 pre-device query. Test 5 in `test_proxy_device_dispatch` exercises late client GDPA resolution and device creation/proc lookup and passed. The official smoke and all three baselines then created the device and queue with this source and the exact proxy.

## 8. Early-query test

Test 6 in `test_proxy_device_dispatch` exercised early client GDPA resolution and device creation/proc lookup and passed. Early GIPA-based discovery remains supported, but is no longer needed to initialize the proxy delegate.

## 9. Bypass control

BR5's diagnostic matrix passed all four controls: proxy plus MoltenVK 1.4.2 headless, proxy plus 1.4.2 with surface, bypass plus 1.4.2 headless, and bypass plus 1.4.2 with surface. These are device/surface controls, not B performance baselines; all accepted smoke/baseline runs used the tracing proxy.

## 10. Gate 0 policy verification

Live source constants remain `FREE_MINIMUM = 45 * GIB` and `GATE0_PROJECT_BUDGET = 45 * GIB` (48,318,382,080 bytes each). `FREE_HARD_STOP = 25 * GIB` is the emergency stop, not the normal free-space threshold. No 1.4.3 runtime or presentation-setting change was introduced.

## 11. Tests and diff checks

- Focused proxy/device dispatch binary: **15/15 passed**.
- Focused trace, PrefixLease, runner, and authorization module group: **118 tests passed**.
- `python3.14 -m unittest discover -s experiments/storage -p 'test_*.py'`: **380 tests passed**.
- The discovery includes the trace validator/process, PrefixLease contract, runner failure/process, and smoke authorization test modules. The five new runner failure tests passed as part of the full suite.
- `git diff --check`: **passed**. It was rerun after the report was written; the report file was additionally checked for trailing whitespace.

## 12. Independent review

No usable independent opinion was obtained. The bounded Agy consultation returned malformed JSON. The OpenCode shared consultant wrapper failed before provider dispatch with a `TypeError` in `build_payload`; the direct OpenCode adapter failed at the same call site. No consultant output is treated as review evidence. The requested `STEP-11D3-BR4-REPORT.md` was not present in this checkout; the available BR5, prior B, and A3 reports were inspected.

## 13. Fresh certification

After the runner error-path change, Gate 0 was refreshed through the storage-only PrefixLease smoke `step11d3-storage-policy-smoke-20261010-75fa41147ed1401c` (**PASS**), then finalized to a signed Gate 0 PASS. That smoke reclaimed 2,303,463,424 bytes, passed 78 synthetic storage-policy tests and 15 focused Gate 0 checks, and left no active lease. The smoke authorization binds the current runner/proxy/validator inputs.

## 14. Official smoke ID

Accepted fresh official smoke: `step11d3b-smoke-20261010-053718-a78894d0`. It generated `smoke-authorization.json`, which records PASS, `is_valid=true`, `quantitative_ready=true`, Gate 0 POST PASS, and automatic PrefixLease POST PASS. Two screenshots were captured 10.001 seconds apart and both passed the screenshot/content checks.

## 15. Runtime identities

Wine was pinned to Wine 11.17 / WineForge 0.6.0.5. The observed Wine host loader was the pinned x86_64 `win32u.so` (`b43893430e929e17086653d9c2266a0d6dd4863b8739c005c3c9a9bdb2a86f1a`); builtin `winevulkan.dll` and `winevulkan.so` loaded with hashes `a18992aca34493aae325a5c1c6ddc78b905876fb65b2ca91fe7b1882b33fd2dc` and `6362dd64890738166dda79bdd9fcc94df55857c1f9756a3268ae91b8d3c38d88`. The proxy SHA-256 was `1b3dda059905bbacb6dfb63718c78eb1505682fb6d493d5be2ef4dec51a38688`. No forbidden module or Khronos loader was observed.

## 16. Target architecture and Rosetta

The PE target and Wine process were x86_64. In-process identity telemetry recorded `process_translated = 1` (`sysctl.proc_translated`) in the smoke and all three baselines. Each baseline ran approximately 300 seconds in a single target process; app/host traces reported zero dropped events.

## 17. Exact MoltenVK

Every accepted smoke/baseline loaded MoltenVK v1.4.2, SHA-256 `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f`, x86_64 LC_UUID `C178CB05-A1CA-34F9-8ADD-7671DC15A86B`. No MoltenVK 1.4.3 runtime was selected.

## 18. Layer and swapchain

Requested and observed configuration: windowed 640×360, `VK_FORMAT_B8G8R8A8_UNORM`, `VK_PRESENT_MODE_FIFO_KHR`, three swapchain images, Apple M3. The layer record identified `WineMetalLayer` backed by `CAMetalLayer`; `displaySyncEnabled=true`, `maximumDrawableCount=3`, `allowsNextDrawableTimeout=true`, drawable size 640×360.

## 19. Smoke result

The official smoke rendered for 30.002 seconds and passed, with 31.331 displayed FPS. Its maximum `nextDrawable` wait was 1.005481 seconds. The screenshots were nonblack and changed between captures; this does not establish frame-by-frame visual quality. During the open smoke window, the user reported fast flicker, with dark/white coloration most recognizable. That visual report is preserved as qualitative evidence alongside the trace.

## 20. Trace validation

The accepted smoke and all three baselines had `is_valid=true` and `quantitative_ready=true`; target app and host traces were present, clock metadata and loader identities validated, and trace-drop counts were zero. Baseline frame/event totals and timings below come from the accepted `run-result.json` files and their joined app/host traces.

## 21. PrefixLease POST

Smoke and all baselines recorded PrefixLease cleanup PASS, storage hygiene PASS, free-space status PASS, wineserver shutdown return code 0, disposable prefix deletion, and active-marker removal after receipt. Each graphics run also recorded Gate 0 PRE and POST PASS.

## 22. Smoke authorization

The accepted smoke authorization is HMAC-signed and binds the smoke run-result hash, trace artifacts, x86_64-under-Rosetta architecture, proxy/delegate hashes, runner, validator, and storage-policy hashes. Baselines 2 and 3 used this fresh authorization. Baseline 1 is the previously accepted valid run under its earlier passing smoke authorization, as allowed by the handoff; it predates the runner's error-reporting-only correction.

## 23. Baseline 01

Run `step11d3b-baseline-01-20261009-223854-cfea0560`: **PASS**, valid and quantitative-ready; 300.012 seconds, process exit 0, 17,941 acquired/present requests, 17,925 displayed, 16 unpresented (0.08918%), 59.8009 acquired FPS, 59.7476 displayed FPS. `nextDrawable` maximum was 50.7495 ms; 95.6747% of waits were 14–18 ms. Gate 0 PRE/POST and cleanup passed.

## 24. Baseline 02

Run `step11d3b-baseline-02-20261010-054210-3a16e5aa`: **PASS**, valid and quantitative-ready; 300.018 seconds, process exit 0, 33,990 acquired/present requests, 1,549 displayed, 32,441 unpresented (95.4428%), 113.2934 acquired FPS, 5.1630 displayed FPS. `nextDrawable` maximum was 1,009.6541 ms. The trace recorded 271,922 events with zero dropped trace events; Gate 0 PRE/POST and cleanup passed.

## 25. Baseline 03

Run `step11d3b-baseline-03-20261010-062321-91bb8f4d`: **PASS**, valid and quantitative-ready; 300.001 seconds, process exit 0, 17,967 acquired/present requests, 17,953 displayed, 14 unpresented (0.07792%), 59.8899 acquired FPS, 59.8432 displayed FPS. `nextDrawable` maximum was 67.1707 ms and there were no waits ≥100 ms. Gate 0 PRE/POST and cleanup passed.

## 26. Pooled 13-bucket `nextDrawable` histogram

The following histogram pools all 69,898 `next_drawable_end` samples from the three accepted baselines. Percentages use the pooled sample count; accumulated wait is the sum of measured event durations.

| Required bucket | Count | Percentage | Accumulated wait |
|---|---:|---:|---:|
| <0.5 ms | 386 | 0.55223% | 58,996,459 ns |
| 0.5–2 ms | 142 | 0.20315% | 202,454,831 ns |
| 2–5 ms | 3,904 | 5.58528% | 15,930,888,827 ns |
| 5–10 ms | 26,600 | 38.05545% | 194,675,137,916 ns |
| 10–14 ms | 3,580 | 5.12175% | 42,483,314,004 ns |
| 14–18 ms | 34,780 | 49.75822% | 549,448,057,497 ns |
| 18–25 ms | 476 | 0.68099% | 9,487,888,591 ns |
| 25–50 ms | 27 | 0.03863% | 730,363,588 ns |
| 50–100 ms | 2 | 0.00286% | 117,920,209 ns |
| 100–250 ms | 0 | 0% | 0 ns |
| 250–500 ms | 0 | 0% | 0 ns |
| 500–900 ms | 0 | 0% | 0 ns |
| ≥900 ms | 1 | 0.00143% | 1,009,654,125 ns |

## 27. Timing statistics

All `nextDrawable` statistics are in milliseconds. Percentiles use linear interpolation on the sorted sample at `(n−1)×q`.

| Run | n | Min | Mean | p50 | p90 | p95 | p99 | p99.9 | Max |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| Baseline 01 | 17,941 | 0.0746 | 16.0786 | 16.1579 | 16.4724 | 16.6056 | 18.8952 | 24.0716 | 50.7495 |
| Baseline 02 | 33,990 | 0.0297 | 7.4742 | 7.4387 | 9.9877 | 12.1829 | 16.1880 | 20.4792 | 1,009.6541 |
| Baseline 03 | 17,967 | 0.0230 | 15.1183 | 15.4271 | 16.1740 | 16.3707 | 16.8855 | 21.4213 | 67.1707 |
| Pooled | 69,898 | 0.0230 | 11.6476 | 14.1478 | 16.2783 | 16.4132 | 17.2420 | 22.8727 | 1,009.6541 |

Native-boundary API durations, milliseconds:

| Run | Acquire mean / median / p99 / max | Submit mean / median / p90 / p95 / p99 / p99.9 / max | Present CPU mean / median / p99 / max |
|---|---|---|---|
| Baseline 01 | 0.0043 / 0.0028 / 0.0229 / 0.8588 | 16.3130 / 16.3605 / 16.7292 / 16.9201 / 19.6257 / 24.8164 / 52.2006 | 0.1187 / 0.0998 / 0.3534 / 7.2456 |
| Baseline 02 | 0.0718 / 0.0640 / 0.2778 / 10.7580 | 7.9519 / 7.7678 / 10.4704 / 13.8878 / 16.5582 / 22.9751 / 1,009.9326 | 0.3847 / 0.3410 / 1.4495 / 32.6043 |
| Baseline 03 | 0.0095 / 0.0068 / 0.0781 / 1.1990 | 15.6176 / 15.9010 / 16.496 / 16.6500 / 18.2421 / 24.4835 / 67.7862 | 0.2965 / 0.2533 / 1.2880 / 5.6385 |

Acquired/displayed FPS and presentation success were 59.8009/59.7476 FPS and 99.9108% for Baseline 01; 113.2934/5.1630 FPS and 4.5572% for Baseline 02; 59.8899/59.8432 FPS and 99.9221% for Baseline 03. Present CPU is the host `vkQueuePresentKHR` call duration; the app-side wrappers are separately available in each validation record.

## 28. Long-wait enumeration

All target waits ≥25 ms, listed as `app_frame_id: duration_ms` (helper processes excluded):

- **Baseline 01:** 1187:25.2145, 3946:26.7510, 7857:50.7495, 8286:25.9858, 12742:25.4128, 14835:26.3500, 17024:26.6603.
- **Baseline 02:** 301:31.7244, 662:26.1770, 668:1009.6541, 3961:26.3427, 3962:31.9898, 8553:25.2812, 11601:25.8834, 11890:28.8150, 15208:27.0323, 15561:26.4446, 18185:26.5272, 24492:25.0120, 25083:25.3180, 25440:26.2170, 26387:26.2575, 33872:28.7480.
- **Baseline 03:** 812:25.4205, 902:67.1707, 1546:25.1524, 7651:31.0021, 10786:28.6789, 15286:28.4761, 15346:27.4893.

Across the three runs: 30 waits ≥25 ms; 3 ≥50 ms; 1 ≥100 ms; 1 ≥250 ms; and 1 ≥500 ms. The sole ≥100 ms event is Baseline 02 frame 668.

## 29. ≥100 ms frame-668 timeline

At approximately 12.0 seconds after target process start, PE frame 668 acquired image index 1 and submitted it. The app-side acquire took 57.1 µs. `vkQueueSubmit2` began at mach time 821034324945959 and ended at 821035334878584 (1.009932625 s). The correlated `nextDrawable` began at 821034324961000 and ended at 821035334615125 (1.009654125 s), matching image ID `140430760209120` and image index 1.

The `present_request` followed at 821035334960042; host `vkQueuePresentKHR` ended at 821035335016625 (56.583 µs). The presented handler was registered at 821035335191250. The callback arrived at 821035344848334 with `presented_time_mach_ns=0`; availability was observed at 821035344858542 and drawable release at 821035344863459. The validator marked this frame not displayed. The roughly one-second host `nextDrawable` interval accounts for nearly all of the submit call duration.

`WINE_SOURCE` and `MVK_BOUNDARY` trace rows annotate the same interposed host Vulkan call; they are not independent measurements of Wine thunk time versus MoltenVK time. The process-scoped trace joined the target PID and exact image/drawable identities; no helper event was used to explain the wait.

## 30. Refresh phase and modality

The measured effective period was 16,666,459 ns for all three baselines; this comes from valid target `presentedTime` intervals and does not assume exactly 60.000 Hz. The 10–18 ms wait counts were 17,503/17,941 (Baseline 01), 3,322/33,990 (Baseline 02), and 17,535/17,967 (Baseline 03). Counts >25 ms were 7, 16, and 7; counts >100 ms were 0, 1, and 0.

Baselines 01 and 03 were strongly phase-clustered in the first quarter of request phase modulo the measured period: 97.49% and 87.20% of requests, with 95.67% and 89.34% respectively in the 14–18 ms duration bucket. Baseline 02 was different: request-phase quartiles were approximately even (27.68%, 23.37%, 25.96%, 22.99%), its dominant duration mode was 5–10 ms (77.50%), and only 4.60% fell in 14–18 ms. The three-run set therefore does not show one stable presentation modality.

## 31. Native A comparison

Native A steady controls reported 59.99–60.00 displayed FPS, mean `nextDrawable` about 16.04–16.05 ms, p99 about 16.25–16.27 ms, and maxima 22.56 and 36.11 ms. Baselines 01 and 03 are close in displayed rate and dominant cadence, though their maxima (50.75 and 67.17 ms) exceed those A controls. Baseline 02 is far from A in displayed rate, presentation success, and its one-second wait. The observed B variance prevents a general parity or instability claim.

## 32. Integrated D2 comparison

Integrated D2 reported 44.898–45.590 WSI/s in LOW and 58.099–58.993 WSI/s in HIGH, with approximately 326 ms `nextDrawable`, 722.783 ms submit, and 723.305 ms `presentPending` tails. Baselines 01 and 03 look much closer to healthy native A. Baseline 02 is more severe in displayed rate and has a longer single `nextDrawable`/submit wait than the D2 tail values. The instrumentation labels are not identical measures across those experiments, so the comparison is descriptive rather than a causal attribution.

## 33. Final storage state

Formal thresholds are free space ≥45 GiB and scoped FG-Metal use ≤45 GiB; emergency hard stop is 25 GiB. The signed final attestation is PASS with no blockers, zero active builds, zero active leases, two preserved prefixes, and zero uncertified prefixes. Its recorded measurements were 72,168,886,272 free bytes (67.22 GiB) and 46,863,388,672 scoped bytes (43.65 GiB).

The latest graphics POST, Baseline 03, was also PASS: 66.98 GiB free and 42.64 GiB scoped, with empty active-build and active-PrefixLease lists and no uncertified prefixes. A subsequent read-only live snapshot measured 71,813,103,616 free bytes (66.88 GiB) and 45,878,902,784 scoped bytes (42.73 GiB); a live blocker check returned `[]`, active lease markers were empty, preserved prefixes remained two, and uncertified prefixes were empty. The active marker root covers PrefixLease and NativeRunLease markers; none were present.

## 34. Git and workspace state

`git diff --check` passed. The BR6 experiment directory, including the report and runner regression test, is untracked in this checkout; there is no commit. Existing unrelated modified and untracked workspace content was preserved. The BR6-specific runner change makes missing/malformed execution-result handling report the original process failure instead of masking it; `test_bare_wine_runner_failures.py` covers five failure cases.

Three failed/superseded run directories were retained as compressed archives before their expanded copies were removed: `failed-step11d3b-smoke-20261010-053051-91b038e1.tar.gz` (SHA-256 `8c033d32680a0278b8438b74c1c20f1f06d301ffbc0ac0dd8fa7a7086cd0de1b`), `failed-step11d3b-baseline-03-20261010-055156-2726942d.tar.gz` (`4d5674022660dfdb838d8b98f020ca6aee3432490a64093a9711b65afd1ae8ec`), and `failed-step11d3b-baseline-03-20261010-060943-bd8c8e10.tar.gz` (`60cab85a5e63f7cc3b93962cc99facc3c4810460e4c3ad337e4dcf7c55d822ee`).

## 35. Remaining blockers

The B stability result remains unresolved because one valid baseline diverges sharply from the other two, and the official smoke/user observation also indicate visible flicker. No root cause is established. Independent review was unavailable due consultant-output/runtime failures, and the BR4 report named in the handoff was absent from the checkout. These limitations do not invalidate the accepted run evidence or the current Gate 0 PASS.

## 36. Exact next step

Keep STEP 11D.3-C gated. The next step is a separately scoped, serialized B follow-up that repeats the Baseline 02 conditions and correlates a fresh visible-window observation with frame-level `presentedTime`, drawable acquisition, and submit traces, starting with the frame-668 pattern. Reassess B only after that controlled result; do not change presentation settings as part of the comparison.

## 37. Backend-strategy boundary

DXVK-MacOS remains the research/reference backend through STEP 11 and STEP 11E. After STEP 11E, stop at the backend-strategy gate before major RIFE/model/product integration. This BR6 work does not reopen DXMT, D3DMetal/GPTK, RIFE, or shipping-backend selection.
