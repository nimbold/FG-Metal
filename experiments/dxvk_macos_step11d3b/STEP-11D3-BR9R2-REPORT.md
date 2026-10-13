# STEP 11D.3-BR9R2 — Neutral External Coverer Differential

**Execution state: PREPARATION COMPLETE; EXPERIMENTS BLOCKED AT INDEPENDENT REVIEW / GATE 0**

No graphics experiment was admitted. The fresh Gate 0 preflight stops because its mandatory independent review record does not bind the amended storage policy source. This report records completed policy, test, and inventory work and preserves the accepted BR9R sequence for resumption.

## 1. Storage-policy amendment

Implemented the project owner's amended policy in the shared storage controls. Admission uses projected peak scope and projected free space. BuildLease, PrefixLease, and NativeRunLease paths use the shared projected-admission calculation; PrefixLease rechecks the remaining peak after materialization.

## 2. Exact constants

| Control | Value |
| --- | ---: |
| Emergency free-space hard stop | 25 GiB |
| Normal projected free-space minimum | 50 GiB |
| Preferred free-space target | 60 GiB |
| Project-scope warning threshold | 52 GiB |
| FG-Metal project-scope ceiling | 55 GiB |
| Minimum uncertainty margin | 256 MiB |
| Proportional uncertainty margin | 10% |

## 3. Regression tests

The complete storage suite passed: 399 tests, no skips or expected failures. The added admission cases cover 54 GiB current scope, exact projected limits of 55 GiB scope and 50 GiB free space, a 45+ GiB current allocation, projected scope above 55 GiB, projected free below 50 GiB, and the current-pass/projected-fail cases.

Evidence: [storage-suite.log](evidence/br9r2/storage-suite.log), SHA-256 4b6da9e68615da959c306fce51467d8d9000267737c1ad1f7d9dbe602d1ca11f.

The executable Python storage and BR9R runner sources contain no 45 GiB scope ceiling. Remaining source references describe the former limit in regression-test wording; older reports remain historical.

## 4. Independent review

**BLOCKED.** The existing review record says PASS with no findings, but it reviewed storage_policy.py SHA-256 057a24154ceb1e8fe714158e0867962940fbda60d288c7febc79900a6386e718. The amended source is 7b0e6e7ae23271c2f298345da9bfa907e9600ebeba7797b3a5e9c14b9ac19004. Its test-file hash still matches, but the source hash does not. I did a primary source read-through of the admission constants and lease call paths; that does not satisfy the independent-review requirement. Agy was not used after the owner's instruction.

## 5. Fresh Gate 0

**BLOCKED before certification.** The fresh preflight returned: “Gate 0 refresh blocked: launcher independent review is failed or does not bind the current source”. It stopped in the completion-evidence check, before Gate 0's own suite/inventory refresh and before writing a fresh attestation. The full suite and inventory below were run independently for this report; they do not substitute for a Gate 0 PASS.

Evidence: [gate0-preflight.log](evidence/br9r2/gate0-preflight.log).

## 6. Fresh inventory

Inventory scan completed on 2026-10-11 across 94 roots. It measured:

- Available free space: 66,458,628,096 bytes (61.894 GiB).
- FG-Metal inode-deduplicated scope: 48,213,647,360 bytes (44.902 GiB).
- Scoped unlinked mounted-image allowance: 1,436,106,752 bytes (1.337 GiB).
- Conservative projected starting scope used below: 49,649,754,112 bytes (46.240 GiB).
- Active builds: 0; preserved builds: 0; worktrees: 3.

The inventory file SHA-256 is 00bce839300f7193a519e9621064397318030108c5f99236d78edfb2fec899b4.

## 7. Projected peak under the 55/50 GiB policy

The estimates below use the conservative starting scope and fresh free-space measurement above. Wine modes reserve a 3 GiB PrefixLease; Wine smoke reserves 100 MiB persistent output and state 5 reserves 300 MiB. Native modes reserve 512 MiB persistent output. Each lease adds the source-defined uncertainty margin. For the planned coverer build, the planning estimate is 128 MiB temporary allocation, 8 MiB binary/provenance output, and the 256 MiB minimum uncertainty margin. The same 8 MiB coverer binary is retained across all coverer runs. The table assumes each completed run retains its full declared persistent output; controller records/logs are included in those persistent allocations. A further 1 MiB is reserved for the final report.

| Planned phase peak | Projected scope | Projected free | Admission |
| --- | ---: | ---: | --- |
| Fresh Wine authorization smoke | 49.647 GiB | 58.487 GiB | PASS |
| Coverer compile after Wine smoke | 46.720 GiB | 61.414 GiB | PASS |
| Wine state 5 calibration, with smoke output and coverer retained | 49.968 GiB | 58.167 GiB | PASS |
| Native foreground control, with Wine outputs and coverer retained | 47.388 GiB | 60.746 GiB | PASS |
| Native full-occlusion run 1 | 47.888 GiB | 60.246 GiB | PASS |
| Native full-occlusion run 2 | 48.388 GiB | 59.746 GiB | PASS |
| Estimated final retained outputs and report | 48.139 GiB | 59.995 GiB | PASS |

All modeled peaks remain below 55 GiB scope and above 50 GiB free. Projected free space enters the below-60 GiB preferred-target advisory during runs; no modeled phase enters the below-50 GiB normal-admission blocker or the above-52 GiB scope-warning region. These are estimates only; each actual lease must repeat live admission checks after Gate 0.

## 8. Neutral coverer build and provenance

The accepted public-AppKit coverer source is [neutral_coverer.swift](neutral_coverer.swift), SHA-256 8618bfa139df18d7da6c4db46518b8cf690c8c42bb568aa8470d118c8c6c0b82. The controller source is [coverer_sequence.py](evidence/br9r2/coverer_sequence.py), SHA-256 f650bb05d5d152fa37d97c2c4d246eaa71a3facc41dcf8c16aa2f6037a0b57f5. Swift AppKit/CoreGraphics type-check passed with swiftc -parse-as-library -typecheck; no coverer binary was built because fresh Gate 0 did not pass. Binary provenance and identical-binary verification remain pending.

## 9. Wine calibration

**NOT RUN.** Gate 0 is blocked. The prior smoke authorization binds older source hashes and cannot authorize this amended run. After fresh certification, create a new Wine smoke authorization, then run Wine state 5 calibration with the coverer.

## 10. Native foreground control

**NOT RUN.** It remains the first Native run after the Wine calibration.

## 11. Native full-occlusion run 1

**NOT RUN.** The accepted 80-second Native occlusion mode and controller are prepared. Admission remains gated on fresh Gate 0.

## 12. Native full-occlusion run 2

**NOT RUN.** It must use the same coverer binary and sequence as run 1.

## 13. Optional long confirmation

**NOT RUN.** Run only if the two Native occlusion results leave an ambiguity that a longer confirmation can resolve.

## 14. Wine-versus-Native differential

**PENDING DATA.** The fresh differential has not run. The existing STEP 11D.3-B classification remains B1: bare Wine Vulkan reproduces drawable instability.

## 15. BR9R causal classification

BR9R remains **BR9R-N3 — INCONCLUSIVE** pending the neutral-coverer differential. No causal conclusion is drawn from the unrun phases.

## 16. Final storage

No BR9R experiment output was created. The latest fresh inventory remains at 61.894 GiB free and 46.240 GiB conservative FG-Metal scope. Gate 0's prior attestation is not treated as current certification.

## 17. Diff validation

Final git diff --check is recorded after this report and its evidence files are written.

## 18. Exact next step

Obtain an independent read-only review bound to amended storage_policy.py SHA-256 7b0e6e7ae23271c2f298345da9bfa907e9600ebeba7797b3a5e9c14b9ac19004 and the current policy-test hash. Then rerun the serialized Gate 0 chain: preflight, its permitted storage-only PrefixLease smoke, and finalization. Only after fresh PASS, create a new Wine smoke authorization, run Wine calibration, run the fresh Native foreground control, and then execute the two Native occlusion runs.

## 19. Backend-strategy boundary

The experiment remains limited to the accepted external opaque AppKit coverer above Native A's floating level. Native target rendering stays untouched. STEP 11D.3-C remains gated.

**No commit was created.**

## 20. Execution update — 2026-10-11

This append-only execution record supersedes the preparation-time status above. It preserves that original preparation block as a historical snapshot.

### Independent policy review and Gate 0

The independent GPT-6 Luna Max review completed **PASS** with no findings at 2026-10-11T12:21:45Z. The review artifact SHA-256 is `461d0e5ea470b8cb55a97d6e38d21ad6c2ab7abf3a718790d44268499f0e0066`. It binds the then-current `storage_policy.py` SHA-256 `97ee17a5ae6a4b81496f03f6cf6e4c630a4239eaa08a5e1bcbeb65fc2a2b7ec7`, policy-test SHA-256 `4eff8bcfa34eac88c4bdd0aa0bd535aefa6484f1d469bc6526d1a2675084bc20`, Wine runner/test, BuildLease, PrefixLease, NativeRunLease, Gate 0, inventory, Native runner, and BR9R2 controller/build evidence. The reviewer confirmed the full projected-peak policy, the post-materialization APFS COW recheck, the screenshot-readiness fix, and the bounded coverer-build admission. Full answers and reviewed hashes are in [g0c-launcher-independent-review.json](../storage/g0c-launcher-independent-review.json).

The canonical thresholds in this review were: emergency free-space hard stop 25 GiB; normal projected free-space minimum 50 GiB; preferred free-space target 60 GiB; project-scope warning above 52 GiB; project-scope ceiling 55 GiB; uncertainty `max(256 MiB, ceiling(10%))`.

After the screenshot-readiness change, the fresh serialized Gate 0 chain passed. Preflight measured 69,604,216,832 bytes free and 48,288,055,296 bytes of scoped allocation; the permitted storage-only PrefixLease smoke created and deleted one temporary prefix, removed its marker after a durable signed cleanup receipt, and passed storage hygiene/free-space checks. Finalization measured 69,608,488,960 bytes free and 48,288,296,960 bytes scoped; active builds and PrefixLeases were zero, uncertified prefixes were zero, storage PrefixLease certification was PASS, and blockers were empty. Logs: [preflight](evidence/br9r2/gate0-post-screenshot-fix-preflight.log), [PrefixLease smoke](evidence/br9r2/gate0-post-screenshot-fix-prefixlease-smoke.log), [finalization](evidence/br9r2/gate0-post-screenshot-fix-finalize.log).

### Coverer build

The exact prepared source SHA-256 remained `8618bfa139df18d7da6c4db46518b8cf690c8c42bb568aa8470d118c8c6c0b82`; the prepared controller SHA-256 at build time was `f650bb05d5d152fa37d97c2c4d246eaa71a3facc41dcf8c16aa2f6037a0b57f5`. The projected build admission was ALLOW at 411,041,792 bytes including the 256 MiB uncertainty margin. The artifactized executable SHA-256 is `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc`, at `/Users/nima/Library/Caches/FGMetal/artifacts/sha256/1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc/neutral-coverer`; it is a 147,888-byte arm64 Mach-O. Toolchain: Swift 6.4, macOS SDK 27.0. The window is opaque with alpha 1.0 at `NSModalPanelWindowLevel` (AppKit raw value 8), above Native A's floating level (raw value 3). It is standalone, borderless, black, and nonanimated, with no Metal, Vulkan, screen capture, or target modification. Build manifest and provenance are bound in the independent-review artifact.

### Fresh Wine authorization

The first post-review Wine smoke (`step11d3b-smoke-20261011-120105-aaf32430`) did not authorize calibration: its first capture was nearly black (`nonblack_ratio=0.0585`), while a later capture showed content. The runner was changed to retry black warm-up captures while retaining the requirement for two content-qualified frames at least 10 seconds apart with visible change. The expanded storage suite then passed 404 tests, with no skips; evidence is [storage-suite-post-screenshot-fix.log](evidence/br9r2/storage-suite-post-screenshot-fix.log).

Following the new independent review and fresh Gate 0 chain, Wine authorization `step11d3b-smoke-20261011-122913-9bb97862` passed. It bound the current storage policy and Wine runner hashes, exact x86_64-under-Rosetta target/runtime, passive trace proxy and MoltenVK 1.4.2. Target and trace validation passed; `quantitative_ready=true`; Gate PRE and POST were PASS with empty blockers; PrefixLease cleanup and signed receipt were PASS. Both accepted screenshots showed visible color content and the second was captured 10.0009 seconds after the first. The run acquired 58.908 FPS and reported 58.475 presented FPS. Evidence: [run result](runs/step11d3b-smoke-20261011-122913-9bb97862/run-result.json), [authorization](runs/step11d3b-smoke-20261011-122913-9bb97862/smoke-authorization.json), and [smoke log](evidence/br9r2/wine-authorization-smoke-retry.log).

### Wine calibration attempt and controller deadline correction

The first calibration attempt used the verified artifactized coverer and the prepared `coverer_sequence.py`. The coverer process started, but the controller's 90-second target-acquisition deadline expired while the fresh state-5 Wine runner was completing its own Gate PRE and PrefixLease setup. It never bound a target PID and never issued `show`; therefore no coverage was attained and no covered/restore metrics are claimed. The unmanaged state-5 run was allowed to finish through its managed cleanup: Gate PRE/POST were PASS, the PrefixLease marker was removed after receipt, and the prefix was deleted. Its trace validation failed because a concurrent `dyld[...]` log line interleaved with a Vulkan extension line in `runtime.log`. This run is invalid and is not calibration evidence.

The observed startup sequence demonstrates that the controller's fixed 90-second acquisition deadline was too short for the required live Gate/PrefixLease work on this host. The controller now uses a bounded 360-second target-acquisition deadline and records that bound in its evidence. Current controller SHA-256 is `0d5c7870948df33a4df1630bf6132ddd44bdfd6bcc0893a96c356a806077611c`. Python compilation passed; final diff validation is pending. Because this control source changed after the preceding independent review and Gate 0, a new independent review and a fresh serialized Gate 0 chain are required before retrying the Wine calibration.

No Native foreground or occlusion run has started. The failed, uncovered state-5 run does not change STEP 11D.3-B's B1 result and does not establish any BR9R differential. STEP 11D.3-C remains gated.

## 21. Execution update — fresh independent review, Gate 0, and authorization stop

This append-only update records the resumed BR9R2 run. Sections 1–20 remain intact as the preparation and earlier execution history.

### Independent storage-policy review

The first fresh audit artifact was **FAIL** because Gate 0 checked only the policy module and Wine launcher test hashes, not the reviewed storage-policy and BuildLease test hashes. Gate 0 now requires exact repo-relative paths and current no-follow SHA-256 values for nine reviewed files: `storage_policy.py`, `test_wine_launcher_policy.py`, `test_storage_policy.py`, `build_retention.py`, `test_build_retention.py`, `prefix_preflight.py`, `test_prefixlease_wineprefix_contract.py`, `storage_gate0.py`, and `test_g0c_checks.py`. Regression tests reject both missing and stale required bindings.

After that concrete fix, the focused Gate 0 checks passed 17/17 and the full storage suite passed **408/408**, with no skips. Evidence: [storage-suite-post-gate0-binding-fix.log](evidence/br9r2/storage-suite-post-gate0-binding-fix.log).

A fresh independent **GPT-6 Luna (Max)** review completed **PASS, no findings** at `2026-10-11T13:22:20Z`. It answered all 12 questions and bound all nine required current source/test hashes. Artifact SHA-256: `0b7447b92ccb8b42e3d9887ed26344cbcb4a14753c320bec86dc97778f129149`. See [g0c-launcher-independent-review.json](../storage/g0c-launcher-independent-review.json).

The reviewed canonical thresholds are: emergency free-space hard stop 25 GiB; normal projected free-space minimum 50 GiB; preferred free-space target 60 GiB; project-scope warning above 52 GiB; project-scope ceiling 55 GiB; uncertainty `max(256 MiB, ceil(10% of raw peak))`. Equality at projected 50 GiB free and 55 GiB scope is admitted.

### Fresh Gate 0

The serialized chain passed. Fresh preflight returned `PREFLIGHT_PASS` with 64.269 GiB free and 45.153 GiB conservative scoped allocation. It reported zero active builds, zero active PrefixLease markers, and zero uncertified prefixes. The permitted storage-only PrefixLease smoke was `step11d3-storage-policy-smoke-20261011-c82ebb071b954af1`; its projected peak was admitted at 52,062,389,863 scoped bytes and 65,437,116,825 projected free bytes. The signed cleanup receipt passed, the temporary prefix was deleted, its marker was removed after receipt finalization, and the prefix inventory returned to baseline.

Finalization returned **PASS** with 64.274 GiB free, 45.153 GiB conservative scope, zero active builds, zero active PrefixLeases, zero uncertified prefixes, and no storage warnings. `storage_policy.py blockers` returned `[]`. Gate 0 is **PASS**. Logs: [preflight](evidence/br9r2/gate0-post-review-binding-preflight.log), [PrefixLease smoke](evidence/br9r2/gate0-post-review-binding-prefixlease-smoke.log), [finalization](evidence/br9r2/gate0-post-review-binding-finalize.log), and [blockers](evidence/br9r2/gate0-post-review-binding-blockers.log).

### Existing neutral coverer provenance

The prepared Swift source remains SHA-256 `8618bfa139df18d7da6c4db46518b8cf690c8c42bb568aa8470d118c8c6c0b82`. The existing immutable artifact was reverified at SHA-256 `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc`; it is a 147,888-byte arm64 Mach-O. Toolchain: Swift 6.4, macOS SDK 27.0. AppKit level: `NSModalPanelWindowLevel` raw value 8, above Native A floating raw value 3. Opacity is `isOpaque=true`, alpha 1.0, black content; the binary is standalone, borderless, and nonanimated, with no Metal, Vulkan, screen capture, or target modification.

The current controller SHA-256 is `0d5c7870948df33a4df1630bf6132ddd44bdfd6bcc0893a96c356a806077611c`. It changed from the preparation-time `f650bb05d5d152fa37d97c2c4d246eaa71a3facc41dcf8c16aa2f6037a0b57f5` when the target-acquisition deadline was raised from 90 to 360 seconds after the earlier setup timeout. The fresh review binds the current controller. The verified coverer binary was reused; it was not rebuilt against the stale controller pin in the old build helper.

### Fresh Wine authorization attempts

Two fresh smoke attempts were admitted under current hashes. Both had Gate PRE and POST **PASS**, valid target/runtime identity, successful screenshot-content checks, clean process exit, and signed PrefixLease cleanup **PASS**. Neither produced an authorization because trace validation failed with `valid presentedTime records are insufficient or non-monotonic`; `quantitative_ready` was not issued.

| Smoke ID | Target PID | callbacks | positive displayed callbacks | Result |
| --- | ---: | ---: | ---: | --- |
| `step11d3b-smoke-20261011-133102-4ff65117` | 96525 | 3,570 | 0 | Trace validation FAIL |
| `step11d3b-smoke-20261011-133849-6696d906` | 96887 | 3,582 | 0 | Trace validation FAIL |

Every callback in both traces had `presentedTime=0` and `is_displayed=false`. Each run showed changing target-window screenshot content, and its exact Wine/MoltenVK runtime identity and trace index validated, but the presentation trace did not satisfy the required monotonic displayed-time contract. Both prefixes were deleted and their signed cleanup receipts verified. Evidence: [first smoke result](runs/step11d3b-smoke-20261011-133102-4ff65117/run-result.json), [first smoke log](evidence/br9r2/wine-authorization-smoke-post-gate0-binding.log), [retry result](runs/step11d3b-smoke-20261011-133849-6696d906/run-result.json), and [retry log](evidence/br9r2/wine-authorization-smoke-fresh-retry.log).

These invalid authorization runs are not coverer calibration or Wine-versus-Native differential results. No coverer command was issued. Wine calibration, Native foreground control, and both Native occlusion runs remain **NOT RUN**. Native coverage was not attained; valid Native occlusion repeats: **0/2**. Native covered FPS, raw-zero fraction, drawable/submit timings, and restoration metrics are **N/A**.

### Classification and current storage

STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. BR9R2 is **BR9R-N3 — INCONCLUSIVE** because the required fresh Wine authorization did not pass trace validation; no new causal conclusion is drawn. D2 remains `VISIBLE_SUPPORTED`, with exact occlusion at its long stall `UNKNOWN`. STEP 11D.3-C remains gated and was not started.

The most recent run's Gate POST measured 64.069 GiB free and 45.327 GiB conservative scoped allocation, with Gate blockers empty, no active builds or PrefixLease markers, and no uncertified prefixes. The minimum free-space sample across the two Wine smoke leases was 68,789,026,816 bytes (64.065 GiB). The maximum conservative scoped allocation actually measured was 48,669,958,144 bytes (45.327 GiB); prefix `st_blocks` deletion figures are estimates because APFS clone sharing is not observable. The two preserved prefixes remain the existing pristine Wine template and the preserved Step 11D-R diagnostic reproduction. No NativeRunLease marker was created.

The final diff check and a direct current storage snapshot are recorded after this report update. No commit was created.

### Exact next step

Investigate why both fresh, on-screen Wine smoke attempts produced only zero `presentedTime` callbacks under the current validated runtime. Obtain a new smoke with trace validation PASS and `quantitative_ready=true`; do not treat either failed smoke as authorization. Only then run the accepted 20 s uncovered / 40 s full coverage / 20 s restored Wine calibration with the existing immutable coverer. Native work remains gated on successful calibration, beginning with a fresh Native foreground control and then two independent covered runs.


## 22. Final post-smoke storage refresh — 2026-10-11

This direct snapshot supersedes the earlier point-in-time values in section 21. At `2026-10-11T13:47:44Z`, the final Gate 0 attestation remained `PASS` in `final` mode and a live blocker check returned `[]`. Current measured free space was 68,778,196,992 bytes (64.054688 GiB); current inode-deduplicated allocated scope was 47,234,007,040 bytes (43.990097 GiB); and current conservative scope, including the fresh inventory mounted-image upper bound, was 48,670,113,792 bytes (45.327576 GiB). The conservative maximum measured over the final post-smoke snapshots was 48,670,113,792 bytes (45.327576 GiB). The minimum free-space sample over the final post-smoke snapshots and both Wine PrefixLease watches was 68,774,277,120 bytes (64.051037 GiB).

The snapshot found zero active builds, zero active PrefixLease markers, zero active NativeRunLease markers, zero preserved builds, and zero uncertified prefixes. The two preserved prefixes are `/Users/nima/Library/Caches/FGMetal/prefixes/wine-11.17-mvk142-pristine` and `/Users/nima/Library/Caches/FGMetalStep11D-R/step11d2-diag-60s-07-submit-reclaim-split-prefix`. APFS clone sharing is not observable in the allocated-byte scan. Full byte counts and the live Gate 0 blocker result are in [final-storage-snapshot-post-smokes.json](evidence/br9r2/final-storage-snapshot-post-smokes.json).

After this report and snapshot evidence were written, `git diff --check` passed. No commit was created.

## 23. All-zero diagnosis and live Gate 0 freshness stop — 2026-10-11

This append-only diagnostic preserves sections 1–22. The retained evidence answers the cause question as far as it can: both all-zero runs occurred with AppKit reporting the Wine target fully occluded and the application inactive; the successful run was active/key/main and not occluded. This is a strong run-level association and moderate causal support for the window-state explanation, not proof that occlusion caused the zero timestamps. A spontaneous fast-zero event under a verified uncovered/active state has **not** been established.

### Three-run counts and durations

All three requests were 30-second FIFO runs using three 640×360 drawables. The application frame-loop durations below come from `APP_CONFIG.actual_run_ns`; process elapsed includes launch and shutdown. Acquisition, submit, present-request, and callback counts matched within each run.

| Smoke | Result | Frame loop (s) | Process elapsed (s) | Acquires / submits / requests / callbacks | Positive callbacks | Zero callbacks | Acquire / request / callback FPS | Valid displayed FPS | Zero fraction |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `122913-9bb97862` | PASS | 30.012709600 | 87.6817312 | 1,768 each | 1,755 | 13 | 58.908 / 58.908 / 58.908 | 58.475 | 0.7353% |
| `133102-4ff65117` | FAIL | 30.000194800 | 88.9607057 | 3,570 each | 0 | 3,570 | 118.999 / 118.999 / 118.999 | 0 | 100.0000% |
| `133849-6696d906` | FAIL | 30.003005700 | 88.3378768 | 3,582 each | 0 | 3,582 | 119.388 / 119.388 / 119.388 | 0 | 100.0000% |

Both failed smokes therefore match the accepted BR8 fast-zero envelope: **MATCH** for `133102` (118.999 acquire FPS, 0 displayed, 100% zero, 6.790 ms nextDrawable median / 7.299 ms submit median) and **MATCH** for `133849` (119.388 acquire FPS, 0 displayed, 100% zero, 6.411 ms / 7.061 ms medians). They are faithful all-zero callback-time observations, not a parser-generated missing-data result.

### Window telemetry differential

Each run retained 30 passive AppKit telemetry samples spanning about 29.1 seconds; every callback had a prior sample no more than 1.1 seconds old. No window/app notification record or sampled state transition occurred.

| Field | PASS `122913` | FAIL `133102` | FAIL `133849` |
| --- | --- | --- | --- |
| Window visible | true | true | true |
| AppKit occluded / `occlusionState` | false / 8194 | true / 8192 | true / 8192 |
| Key / main | true / true | false / false | false / false |
| Application active | true | false | false |
| Miniaturized | false | false | false |
| Frame | `(68, 502, 640, 392)` | same | same |
| Screen ID / visible frame | `1 / (0, 0, 1470, 923)` | same | same |
| Screen and window backing scale | `2 / 2` | same | same |
| Layer bounds / drawableSize / contentsScale | `(0,0,640,360) / 640×360 / 1` | same | same |
| Layer hidden / max drawables | `false / 3` | same | same |
| `allowsNextDrawableTimeout` / `displaySyncEnabled` | `true / true` | same | same |

The reported `window_visible_intersection_area=250880` is frame geometry intersected with the display's visible frame; it is not an unobscured-pixel measurement. Apple defines a clear `NSWindowOcclusionStateVisible` bit as the window being fully occluded, but that state does not identify the occluder or its bounds. Since occlusion, app-active, key, and main all co-vary, this comparison cannot isolate which part of the state explains the timestamp change.

### Startup and cadence

All-zero mode begins at callback sequence 1 in both failures; neither has a positive callback or a retained NORMAL→ZERO transition bracket. Times are Mach absolute nanoseconds.

| Smoke | First acquire begin → end | First request | First callback / zero | First positive callback |
| --- | --- | --- | --- | --- |
| PASS `122913` | `931837541510959 → 931837541519625` | `931837555221625` | `931837560333625` | Sequence 11, timestamp `931837628792541`, delivered `931837629140625` |
| FAIL `133102` | `935547330881917 → 935547330886209` | `935547345964834` | `935547355361667` | none |
| FAIL `133849` | `936014649374709 → 936014649378834` | `936014660229750` | `936014670408375` | none |

The PASS run also had 13 zeros distributed through the run, so its first positive callback was not simply the end of a one-time warm-up interval.

| Smoke | nextDrawable ms (mean / median / p90 / p99 / max) | submit ms (mean / median / p90 / p99 / max) | request spacing ms (median / p99) | callback latency ms (median / p99) |
| --- | --- | --- | --- | --- |
| PASS `122913` | 14.7973 / 15.2424 / 16.2047 / 16.6663 / 32.2276 | 15.3053 / 15.7891 / 16.3989 / 17.3708 / 32.6486 | 16.6656 / 20.2849 | 48.4323 / 49.2242 |
| FAIL `133102` | 6.5057 / 6.7904 / 8.3564 / 9.0898 / 21.0984 | 7.0287 / 7.2987 / 8.7257 / 10.7745 / 23.6459 | 8.3165 / 13.5740 | 0.6886 / 9.2695 |
| FAIL `133849` | 6.3470 / 6.4108 / 8.0769 / 9.1613 / 19.8268 | 6.9122 / 7.0607 / 8.6333 / 10.6997 / 22.1804 | 8.3392 / 13.0962 | 0.6410 / 9.2073 |

Only the PASS run has a valid display-time reference for request phase. Its four request-phase quartile shares were 97.06%, 2.15%, 0.57%, and 0.23%; phase fractions were median 0.12349, p90 0.16514, and p99 0.40886. No phase values are assigned to either all-zero run.

### Trace integrity and screenshot boundary

All three trace indexes pass with the exact target PID, complete target host trace, correct Wine/MoltenVK/proxy identities, stable per-run control bindings, and zero target-host trace drops. The two failed host traces contain all 3,570 and 3,582 raw callback records with `presented_time_mach_ns: 0` and `is_displayed: false`; the PASS trace contains 1,755 positive and 13 zero callbacks. The callback parser reads `CAMetalDrawable.presentedTime` in the declared Mach time domain, converts positive seconds to nanoseconds, and derives `is_displayed` from `presented_time > 0`. Handler/request/callback/release counts remain complete. This rules out a changed callback field, target/helper PID selection, proxy/MoltenVK mismatch, truncation, and recorded ring drops in these retained runs.

Both failed runs passed the content-qualified target-window screenshot check, but **content-qualified target capture != CoreAnimation valid displayed presentation**. The screenshot helper selects an on-screen CGWindow and captures it by ID without changing focus; that does not prove unobscured display. Positive monotonic `presentedTime` remains the quantitative criterion; its validator was not weakened.

### Known coverer and other-window attribution

The known neutral-coverer process was PID `89364`. Its controller record has `actions: []`, with no target binding and no `show` command. Unified logs place its `running-NotVisible` state at local 16:04:10 on the earlier calibration attempt, then record `proc_exit` at 16:09:36.732 and removal at 16:09:36.840 (UTC 12:39:36.732–12:39:36.840). The failed smoke windows begin at approximately UTC 13:31:52 and 13:39:41, over 52 minutes later. `pgrep -alf neutral-coverer` currently returns no process. Thus contamination from the **known PID 89364 coverer is NO** for both failures.

Historical CGWindow rank, owner, alpha, and bounds for the failed-run moments were not retained. The target screenshot records a window ID but not a historical stacking snapshot. Therefore the identity/geometry of any other covering window is **UNKNOWN**; the window-state correlation cannot be upgraded to a proven coverer cause. System-log excerpt: [coverer-pid-89364-unified-log.log](evidence/br9r2/coverer-pid-89364-unified-log.log).

### Fresh baseline and authorization were blocked before launch

The optional 80-second state-5 preflight was rejected before output/prefix creation because the passing `122913` authorization no longer matches current `gate0_control_hashes` (the current set differs in `build_retention.py`, `storage_gate0.py`, `test_build_retention.py`, and `test_g0c_checks.py`). The two later failed smoke bindings do match the current set. No stale authorization was reused.

A direct fresh `python3 runner_b.py smoke` then stopped at Gate 0 PRE before creating a run or launching Wine. Its blockers were: Gate 0 attestation older than one hour; storage inventory without a measurement from the last hour; and latest cleanup phase older than one hour. The last retained storage sample remains 64.051037 GiB minimum free and 45.327576 GiB maximum conservative scoped allocation, but it is over an hour old and is not presented as a fresh measurement. The historical Gate 0 attestation said PASS; **the current runner gate is BLOCKED on freshness**.

The public observer [public_window_observer.swift](evidence/br9r2/public_window_observer.swift) was added as a read-only CGWindow-list/normal-activation instrument and compiled successfully. It did not capture a target because Gate PRE prevented launch. No canonical uncovered baseline, new authorization smoke, Wine coverer calibration, or Native foreground/occlusion run was produced. Native valid occlusion repeats remain **0/2**. STEP 11D.3-C remains gated.

Gate 0 freshness cannot be restored without the accepted preflight/finalization path. The preflight source calls `reconcile_abandoned_storage_smoke_lease_markers`, `build_retention.reconcile_pending`, and may detach legacy CAS hardlinks; the permitted storage-only PrefixLease smoke creates and removes its own temporary prefix after a signed receipt. This refresh was not run because the request explicitly prohibited storage cleanup. No retained prefix/build/evidence was cleaned, and no coverer was rebuilt.

### Current classification and next step

STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. BR9R2 remains **BR9R-N3 — INCONCLUSIVE**. The leading explanation for the two later all-zero runs is the sampled inactive/occluded target-window state, with moderate causal support; an uncovered spontaneous fast-zero reproduction is not established. Calibration: **NOT RUN**. Native runs: **0/2**. Current Gate: **BLOCKED, stale evidence**. Minimum retained free-space sample: **64.051037 GiB**. Maximum retained conservative scope: **45.327576 GiB**. STEP 11D.3-C: **gated, yes**.

Exact next step: either provide a newly refreshed Gate 0 attestation/inventory/cleanup phase or authorize only the existing Gate 0 refresh chain, including its signed storage-only temporary PrefixLease smoke; then run one fresh Wine authorization smoke with the public-window observer active. No commit was created.

## 24. Authorized Gate 0 refresh and fresh authorization smoke — 2026-10-11

This append-only update supersedes section 23's stale-Gate stop: the user authorized the exact Gate 0 refresh chain, it passed, and one fresh Wine smoke ran. Sections 1–23 remain intact.

### Gate 0 refresh

The first refresh invocation used the system `python3` 3.9 and stopped in the preflight suite because that interpreter does not support `Path.stat(follow_symlinks=...)`; it completed no reconciliation or PrefixLease operation. The same authorized chain then ran under `/opt/homebrew/bin/python3.14` (Python 3.14.8).

Fresh preflight passed at 65.093780 GiB free and 45.359924 GiB scoped. The preflight receipt listed no build reconciliation, abandoned lease reconciliation, artifact hardlink action, or copy estimate. Its required storage-only PrefixLease smoke, `step11d3-storage-policy-smoke-20261011-13d326b62d7a4713`, passed: it created and removed its own temporary prefix, reclaimed an estimated 2,303,463,424 bytes, finalized the signed receipt before removing its active marker, and returned the prefix inventory to baseline. The 89-test synthetic policy suite and 17 focused Gate checks passed with no skips. Finalization passed at 65.091900 GiB free and 45.360153 GiB scoped; the live blocker check returned `[]`.

Evidence: [Python 3.14 preflight](evidence/br9r2/gate0-refresh-20261011-preflight-python314.log), [PrefixLease smoke](evidence/br9r2/gate0-refresh-20261011-prefixlease-python314.log), [finalization](evidence/br9r2/gate0-refresh-20261011-finalize-python314.log), and [initial Python 3.9 attempt](evidence/br9r2/gate0-refresh-20261011-preflight.log).

### Fresh Wine authorization result

The single smoke was `step11d3b-smoke-20261011-152647-ce7ab1ee`, requested for 30 seconds. `APP_CONFIG.actual_run_ns` was 30,000,991,000 ns (30.000991 s); process elapsed was 88.741792 s. Counts were 3,589 acquisitions, submits, present requests, and callbacks, or 119.629 per second for each over the measured frame loop. There were zero positive displayed callbacks and 3,589 raw-zero callbacks (100% zero; valid displayed FPS 0). The first callback, sequence 1, was already zero, and every raw `CAMetalDrawable.presentedTime` record contained `presented_time_mach_ns: 0` and `is_displayed: false`; no NORMAL-to-ZERO transition occurred. Across the 29 complete one-second Mach-time bins, all acquisitions had a matching callback, each bin contained 119–120 acquisitions, and every callback was zero. `nextDrawable` median/p99/max was 6.441/8.840/19.773 ms and submit median/p99/max was 6.939/9.192/20.396 ms (p99 uses the report's linear-interpolation convention). This is a **MATCH** to the BR8 fast-zero signature at its high end: sustained above-refresh acquisition, no valid displayed times, 100% zeros, and about 6–7 ms median drawable/submit latency.

Trace index validation passed for target PID 2030. The target trace had 71,841 records, zero ring drops and zero write failures; target ownership, Wine/MoltenVK 1.4.2, and trace-proxy identities matched the run binding. The callback data are present and complete, so this all-zero result is not explained by helper selection, a missing callback field, a proxy/runtime mismatch, or recorded trace loss. The validator correctly failed with `valid presentedTime records are insufficient or non-monotonic`; `quantitative_ready` was not issued. See [run result](runs/step11d3b-smoke-20261011-152647-ce7ab1ee/run-result.json), [trace index](runs/step11d3b-smoke-20261011-152647-ce7ab1ee/trace-index.json), and [raw target trace](runs/step11d3b-smoke-20261011-152647-ce7ab1ee/host-traces/host-trace.2030.jsonl).

### Window state and uncovered-state boundary

All 30 in-process AppKit telemetry samples report `window_visible=true` and occluded (`occlusionState=8192`), with the application inactive and the window neither key nor main. The recorded `window_ordered=true` mirrors the visibility flag in the tracer; it is not an independent stacking-order observation. The window was not miniaturized. Geometry and layer configuration matched the prior runs: frame `(68,502,640,392)`, screen 1, 2× screen/window scale, 640×360 drawable, contents scale 1, layer visible, three drawables, timeout allowed, and display sync enabled. These measurements correlate the all-zero run with the same inactive/occluded state seen in the two prior failures; they do not identify the occluder or prove causation.

The external [public window observer result](evidence/br9r2/public-window-observer-next-smoke.json) has zero samples: its exact-title query found no target before the configured 150-second deadline, and activation was not attempted. Although the JSON key is named `startedAtUTC`, the observer source writes it when constructing the result after the wait loop; it is an end timestamp, not a start timestamp. It records 2026-10-11 15:29:15 UTC. Mapping the target host trace's `process_start_monotonic_ns` to the contemporaneous wall/monotonic clock places Wine PID 2030's process start at approximately 15:30:01.8 UTC, about 47 seconds later. The observer therefore expired before the Wine target process launched; its title/query behavior against a live target remains untested. Its zero-valued coverer/window sample counters are not evidence of absence. Thus this was **not a verified canonical uncovered run**. The previously known coverer PID 89364 had exited before this smoke and was not launched by this runner, so contamination from PID 89364 is **NO**. Any replacement neutral-coverer process/window or other overlapping window is **UNKNOWN**; no external stacking, owner, rank, alpha, bounds, or overlap rows were captured. The two accepted content-qualified screenshots show changing target-window pixels and did not mutate focus, but content capture does not prove unobscured CoreAnimation presentation.

### Cleanup, Gate, and disposition

The target process exited 0 without timeout. Its PrefixLease cleanup passed, deleted the temporary Wine prefix, finalized the signed receipt before active-marker removal, and reported no unexpected persistent bytes. Gate PRE and POST both passed with no blockers, active builds, active PrefixLeases, or uncertified prefixes. The lowest measured free-space sample in this smoke's cleanup watch was 69,772,382,208 bytes (64.980595 GiB); the smoke's conservative Gate POST scope was 48,795,459,584 bytes (45.444313 GiB). Including earlier retained section 22 evidence, the minimum measured free space remains 64.051037 GiB and the maximum measured conservative scope is now 45.444313 GiB. The projected 61.687 GiB free-space admission floor is a reservation estimate, not a measured free-space minimum.

Fresh Wine authorization smoke: **FAIL**. The required externally verified uncovered-state precondition was **NOT ESTABLISHED**. Wine coverer calibration is **NOT RUN**; Native foreground/occlusion runs are **0/2**; STEP 11D.3-C remains gated. The all-zero observation is another BR8-like reproduction under an AppKit-reported occluded/inactive state, not a spontaneous fast-zero reproduction under a verified uncovered state. STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**; BR9R2 remains **BR9R-N3 — INCONCLUSIVE**. Exact next step: extend/sequence the external observer to remain active through the measured prefix-materialization delay, retain its actual start/deadline/query attempts, then verify the live target's public CGWindow row and stacking state before any further authorization attempt. The current observer expired about 47 seconds before target process launch, so its live title/query behavior remains untested. Do not proceed to coverer or Native calibration from this failed authorization.

After this append, `git diff --check` passed and the appended report text was checked for trailing whitespace. No commit was created.

## 25. Lifecycle-bound observer and final BR9R2 outcome — 2026-10-11

This append supersedes the active run status in §§23–24. It does not reopen the accepted STEP 11D.3-B classification, coverer design, storage policy, or prior B1 evidence. STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. BR9R2 remains **BR9R-N3 — INCONCLUSIVE**.

### Lifecycle observer and canonical uncovered Wine state

The public observer was started before the runner and stayed active through the explicit experiment-complete signal. It received the authenticated handoff for the actual Wine Unix PID, bound it to the run/token and expected executable identity, and discovered the target by owner PID with the title as a secondary discriminator. The observer did not use a prelaunch fixed deadline. Its safety watchdog did not fire.

For the passing authorization `step11d3b-smoke-20261011-191025-c35f36a6`, observer start was 2026-10-11 19:10:24.994 UTC, target handoff arrived at 19:13:39.285 UTC, and the first target window was found at 19:13:44.971 UTC. The target was Wine PID 38870 / CGWindow ID 28910. The observer captured 100 samples through 19:14:41.905 UTC and ended without error. Canonical uncovered state was established: the target was onscreen on display 1, not miniaturized or AppKit-occluded, active/key/main, with advancing fresh target-owned telemetry and no neutral-coverer process/window or opaque full-cover candidate. The qualifying streak contained 51 samples over 32.301 seconds and 32 telemetry sequence advances.

For calibration run `step11d3b-br9r-calibration-20261011-191535-62b08a17`, observer start was 19:15:35.373 UTC, handoff arrived at 19:18:48.705 UTC, and the first target window was found at 19:18:54.573 UTC. The exact target was Wine PID 40750 / CGWindow ID 28924. The observer received the run-complete signal and ended at 19:20:50.376 UTC after 188 samples, with no error or watchdog. It again proved canonical uncovered state: 136 samples across 89.247 seconds, 89 telemetry sequence advances, active/key/main and visible/unoccluded target, and no neutral-coverer process/window during observed samples. The complete handoff, query, and sample records are retained under [the calibration observer evidence](evidence/br9r2/br9r2-wine-calibration-20261011-1915-observer/observer-result.json) and [controller record](evidence/br9r2/br9r2-wine-calibration-20261011-1915-controller.json).

### Fresh authorization result

The authorization start gate released at 19:13:47.976600 UTC after a further 2.327692-second stable interval. Runner, trace identity, Gate PRE/POST, and PrefixLease cleanup validation passed. The measured smoke lasted 30.000561 seconds and reported `quantitative_ready=true`: **60.165542 acquired FPS, 59.632219 displayed FPS, 16 raw-zero callbacks out of 1,805 (0.8864%)**, and a healthy-refresh signature. The maximum `nextDrawable` wait was 77.413 ms. This is the canonical uncovered Wine authorization baseline.

### Wine neutral-coverer calibration and strict stop

The immutable neutral coverer SHA remained `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc`. The requested sequence was 20 seconds uncovered, 40 seconds covered, and 20 seconds restored. The calibration controller did not advance beyond the uncovered precondition: its 20-second timeline had 30 valid public-window samples, no invalid state samples, a maximum sample gap of 787.238 ms, and fresh advancing Wine telemetry, but the trailing two-second telemetry comparison had no qualifying in-phase sample pair. The sample at monotonic `956246355317625` and the final in-range sample at `956248342266083` were 1.986948458 seconds apart, 13.051542 ms short of the required two seconds. The controller recorded 26 complete advancing comparisons and failed closed on the uncovered timeline; the managed runner and observer then completed their lifecycles. It did not issue SHOW: the controller `actions` array is empty, no coverer PID was created, and no coverer process/window appeared in observer samples.

The controller's original uncovered phase-rate summary double-counted Wine acquisitions and submits because it counted both `WINE_SOURCE` and `MVK_BOUNDARY` records. A read-only reduction of the retained PID/token-bound raw trace counted each acquire/submit once from `WINE_SOURCE` and callbacks/`nextDrawable` once from `LAYER_SOURCE`. The actual 20-second uncovered trace contains **1,198 acquisitions (59.9 FPS), 1,198 callbacks, 1,198 positive displayed times (59.9 FPS), and zero raw-zero callbacks (0%)**. `nextDrawable` median/p99/max was 16.0890/16.6302/40.3860 ms; submit median/p99/max was 16.2568/16.8077/40.6807 ms. These trace metrics resemble the expected uncovered frame signature, but they do not satisfy the controller's failed observer-timeline acceptance gate and do not establish a completed calibration.

No covered Wine phase, full-cover attainment, or restoration transition was measured. The observer result's `restoredState.attained=true` is not restoration evidence: the independent review found that this state predicate can be true without any preceding covered state. The same run records `coveredState.attained=false` and no coverer actions. Covered and restored Wine metrics are **NOT RUN**. Under the requested stop rule, the calibration is **FAIL / INCOMPLETE**, not a demonstrated failure of the immutable coverer. Native foreground, Native cover runs 1 and 2, and Native metrics are **NOT RUN**. STEP 11D.3-C remains **gated: YES**.

### Independent review and controller findings

The existing independent GPT-6 Luna Max reviewer checked only observer correctness and the final Wine-versus-Native classification. The reviewer agreed that **BR9R-N3 — INCONCLUSIVE** is correct: the Wine authorization passed, but the required calibration uncovered timeline did not, no coverer was shown, and no Native phase ran.

The review confirmed the lifecycle start, real target-PID handoff, target-owned telemetry binding, canonical uncovered evidence, explicit completion, and fail-closed calibration stop. It also identified two controller/reporting defects to resolve before another calibration: (1) restored-state attainment is not gated on a preceding covered state; and (2) phase-rate aggregation double-counts Wine API records unless it filters `origin`. The raw trace can be reduced without ambiguity as described above, but the persisted controller phase summary remains incorrect. No code changes or further experiments were made after the stop.

### Final Gate 0, validation, and next step

The authorized Gate 0 refresh chain ran with `/opt/homebrew/bin/python3.14`: preflight passed, storage-only PrefixLease smoke `step11d3-storage-policy-smoke-20261011-40dc639a02cd46f6` passed and removed its temporary prefix after signed cleanup, and finalization passed. Evidence: [z5 preflight](evidence/br9r2/gate0-refresh-z5-preflight-python314.log), [storage-only PrefixLease smoke and cleanup](evidence/br9r2/gate0-refresh-z5-prefixlease-python314.log), and [z5 finalization](evidence/br9r2/gate0-refresh-z5-finalize-python314.log).

The calibration run's Gate PRE and POST also passed without blockers. PRE measured 59.408913 GiB free and 45.982426 GiB scoped; POST measured 59.275013 GiB free and 46.109550 GiB scoped. The lowest measured free-space sample across the authorization/calibration PrefixLease cleanup watchers was 59.268421 GiB; the maximum conservative scoped Gate reading was 46.109550 GiB. A fresh read-only `storage_policy.py blockers` check returned `[]`.

Focused existing source tests passed before these runs: 8/8 AppKit telemetry source tests and 21/21 smoke-authorization tests. `git diff --check` and the no-index whitespace check for this untracked report pass. No commit was created, and STEP 11D.3-C was not started.

Exact next step: keep BR9R-N3 and Native gated. Correct and independently review the calibration timeline's trailing-boundary handling without weakening the two-second telemetry requirement, fix origin filtering in Wine phase metrics, and require a covered phase before reporting restoration. Then rerun Wine calibration with the same immutable coverer; only a passing Wine calibration can authorize fresh Native foreground and the two Native cover runs.

## 26. BR9R2-Z3 controller fixes, independent review, Gate 0 refresh, and authorization stop — 2026-10-12

This append records the requested calibration-boundary and Wine metric-origin fixes. It preserves STEP 11D.3-B as **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. The frozen neutral coverer was not rebuilt; its SHA-256 remains `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc`.

### Controller and reducer changes

The calibration controller now evaluates the latest contiguous qualifying observer interval. It preserves the exact `2,000,000,000 ns` threshold, requires at least three samples, resets on an invalid state or target/window identity change, and rejects gaps above `1,500,000,000 ns`. Wine telemetry must remain fresh, no more than two seconds old, and advance in both sequence and timestamp. A retained `1.986948458 s` interval still fails; the next valid sample at `2.001 s` proves the required duration. The action phase remains exactly 20/40/20 seconds. A bounded final observation sample may arrive within the recorded 1.5-second grace, while trace metrics stay in the nominal half-open phase interval.

Restoration now follows the explicit `WAIT_UNCOVERED → UNCOVERED_ATTAINED → COVER_ACTION_ISSUED → COVERED_ATTAINED → RESTORE_ACTION_ISSUED → RESTORED_ATTAINED → COMPLETE` state graph. The controller requires a successful covered phase and HIDE response, then evaluates restored samples strictly after HIDE completion. The restored phase is HIDE-completion anchored at 20 seconds; stable restoration must be proven by its nominal end. A grace-only proof after that end fails.

Wine phase metrics now count acquire and submit calls only from `WINE_SOURCE`; `nextDrawable` and presentation callbacks come only from `LAYER_SOURCE`. `MVK_BOUNDARY` records remain available for correlation but are not counted as additional API calls.

The previous incomplete calibration remains **FAIL / INCOMPLETE**. Its read-only reducer artifact is [the corrected Z3 v2 metrics](evidence/br9r2/br9r2-wine-calibration-20261011-1915-corrected-metrics-z3-v2.json), SHA-256 `33e213283819dd952b011dcbaad7e8f9321718324ed89a63ac27d9478cc6d127`. The bound trace SHA is `41cf8107f34885a523f872b462d9e99fc77c64a3c9a48d49f96d92b682744659`; the source controller record SHA is `04c7cb6e6e02718c6caf99ff9ee0f84b2e7d8d6573604b2803ca68e68863afb3`. Corrected uncovered metrics are **1,198 acquires, 1,198 callbacks, 1,198 positive displayed times, 0 zero times, 59.9 acquired FPS, and 59.9 displayed FPS**. The original controller record is FAIL and has no coverer actions or SHOW; these metrics do not turn it into a completed calibration.

### Boundary and origin tests

Focused results: lifecycle/controller **20/20 PASS**, BR7 analyzer **11/11 PASS**, and smoke authorization binding **21/21 PASS**. Python syntax compilation and Swift observer typecheck passed. The independent reviewer also ran the focused set as **52/52 PASS**. The tests cover 2.001-second success, exact 1.999-second rejection, the 1.986948458-second regression followed by later proof, invalid-state reset, stale and nonadvancing telemetry, excessive gaps, PID/window mismatches, allowed boundary observation grace without phase extension, restore ordering, valid uncovered→covered→restored completion, and duplicate Wine/MoltenVK origin rows counting once.

### Independent GPT-6 Luna Max review

Overall review: **PASS**. The reviewer hash-bound the current controller, observer, analyzer/reducer, tests, native foreground controller, native runner, Wine runner, authorization controller, and immutable coverer. It confirmed the exact two-second threshold, fail-closed intervals and restoration order, phase/grace separation, correct metric origins, no Wine API double-counting, and that the previous authorization hashes are stale. The reviewer could not compare the current controller to an exact pre-change source snapshot because that predecessor is not retained; therefore the review confirms the measurement contract and current implementation, while an “only these source lines changed” predecessor-diff claim is unavailable.

Exact reviewed SHA-256 values:

- `evidence/br9r2/coverer_sequence.py` — `3207555ab573e1c53c24c6b04b8dedd3459facee4bb2d78a98cd2405adec6b51`
- `evidence/br9r2/lifecycle_window_observer.py` — `70cf9e9340c8ad14c5ad7182dfff31a8afacdf0deae734a0b3427600be441316`
- `evidence/br9r2/public_window_observer.swift` — `1a3985e4f7478cfe8799a2d239f000578004bf8bad01a960524afb84c87a95c9`
- `br7_analysis.py` — `e7afe1740d81c4d984222a681181e6221d6e3038aa35619955a0d851e75f08a0`
- `trace_processes.py` — `9faa1b81a1e672521de64c044ea19f9ccdd771169a717e7be0fd0bf9e631f29a`
- `evidence/br9r2/native_foreground_controller.py` — `f26a1febae1ec4d955b12c9d8c6da435f7286b99cf5395c98f7fdd90871ebaac`
- `../dxvk_macos_step11d3a/run_native_control.py` — `379dc06a921a49c50e561009d23f249239141d108c7db068140f2e0af175b9f5`
- `evidence/br9r2/authorization_smoke_controller.py` — `5274f4655bf40c725da4c200893d7fbf0575dfbf1e0f18b0760aa61d4a30b542`
- `runner_b.py` — `fd72407136580fa40d30d8e376cf4d68ca62226ad4e33d3fe0b7cf0a2a1c152f`
- `evidence/br9r2/test_lifecycle_controller.py` — `958d1093cf3621ea89fb4a90d0bea2a91c1584fd2fbb1381c32a7cbfad565387`
- `test_br7_analysis.py` — `4a87083d5fd16b892639c202104b69b192ce001a2ed6a7278f994753dde1aa11`
- `../storage/test_step11d3b_smoke_authorization.py` — `f7c85b75c989f001c733156cf03644b6ef9976181fdeb58a3b5086cbf78f79fc`
- Immutable neutral coverer — `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc`

### Fresh Gate 0 chain

The authorized Python 3.14 chain completed: preflight **PASS**, storage-only PrefixLease smoke `step11d3-storage-policy-smoke-20261012-3bb68fb55f244363` **PASS**, signed cleanup and prefix baseline restoration **PASS**, finalization **PASS**, and the post-finalization blocker list was `[]`. The smoke ran 89 synthetic storage tests and 17 focused Gate 0 checks, with no skips. Dedicated logs: [preflight](evidence/br9r2/gate0-refresh-br9r2-z3-preflight-python314.log), [PrefixLease smoke and signed cleanup](evidence/br9r2/gate0-refresh-br9r2-z3-prefixlease-python314.log), [finalization](evidence/br9r2/gate0-refresh-br9r2-z3-finalize-python314.log), and [blocker check](evidence/br9r2/gate0-refresh-br9r2-z3-blockers-python314.log).

### Fresh Wine authorization attempt and stop

The accepted authorization `step11d3b-smoke-20261011-191025-c35f36a6` is invalidated for a new run because the controller and public observer hashes differ from its accepted binding. A fresh lifecycle-bound authorization was therefore attempted as `step11d3b-smoke-20261012-163223-083d11d1`. Its runner Gate 0 PRE and POST checks both reported **PASS**, and the temporary prefix cleanup receipt reported **PASS** with the signed receipt finalized before active-marker removal. However, after PrefixLease materialization, `assert_prefix_lease` failed closed with `Gate 0 blocks Wine runner preflight: current FG-Metal allocated size changed beyond the unlinked-image estimate allowance`. The run exited before starting the Wine target: there is no execution result, no valid target PID handoff, and the lifecycle observer captured zero target samples. No renderer measurements or new canonical uncovered authorization were produced. The managed prefix was removed; free-space and scope remained inside the configured run limits.

Because the required fresh authorization did not pass, the corrected Wine calibration was **NOT RUN**. No SHOW, covered state, restoration, Native foreground, or Native occlusion run occurred. The previous accepted canonical uncovered Wine baseline remains retained evidence (60.165542 acquired FPS, 59.632219 displayed FPS, 16/1,805 raw-zero callbacks, 0.8864%), but its old authorization cannot be reused after the binding changes.

### Classification, storage, and next step

STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. BR9R remains **BR9R-N3 — INCONCLUSIVE**. The Wine calibration retry did not complete because the new authorization failed before target launch; covered and restored metrics are **N/A**. Native foreground and Native runs 1 and 2 are **NOT RUN**. STEP 11D.3-C remains gated: **YES**.

For this Z3 Gate 0 and authorization window, the minimum measured free space was **64.105137 GiB** and the maximum conservative scoped usage was **44.575005 GiB**. The retained earlier BR9R record remains the lower/higher experiment-wide extrema: **59.268421 GiB minimum free** and **46.109550 GiB maximum conservative scope**. No discretionary evidence cleanup or coverer rebuild was performed.

Exact next step: resolve the fresh runner’s post-materialization Gate 0 allocation guard failure under the existing storage policy, then obtain a passing lifecycle-bound Wine authorization. Only after that authorization passes may the 20/40/20 Wine calibration run; Native foreground and the two Native cover runs remain gated on a passing Wine calibration. Do not start STEP 11D.3-C automatically. No commit was created.

## 27. Final workspace validation — 2026-10-12

`git diff --check` passed. A trailing-whitespace scan passed for the report, controller, observer, analyzer, tests, and corrected metrics artifact. The report and evidence remain append-only; no commit was created.

## 28. Z4 PrefixLease post-materialization accounting correction and BR9R2 resume — 2026-10-12

### Z3 failure diagnosis and byte-level reconstruction

The rejected Wine preflight came from the old Gate 0 mutation check, which compared the **whole live project allocation** against the whole-project Gate 0 allocation and allowed only `1 GiB + the unlinked mounted-image upper bound`. `assert_prefix_lease` reached this check after the current PrefixLease had materialized, so its legitimate allocation was charged as unexplained global growth. The old comparison did not subtract an authenticated active PrefixLease or distinguish the current run's signed output from unrelated scope.

The historical assertion-time raw measurement and materialization receipt were not retained. The byte-exact Z3 regression therefore proves the old guard arithmetic and the corrected lease model, but cannot independently establish every byte's provenance at that historical assertion instant. Its recorded fixture is:

| Component | Bytes | GiB | Attribution |
| --- | ---: | ---: | --- |
| Gate 0 whole-scope baseline | 47,831,126,016 | 44.546 | Historical baseline |
| Preclone nonlease scope | 47,865,233,408 | 44.578 | Baseline plus 34,107,392 bytes of preclone drift |
| Materialized PrefixLease | 2,303,463,424 | 2.145 | Measured `st_blocks` allocation, charged in full |
| Marker plus unclassified residual | 8,192 | 0.000008 | 4,096-byte marker plus 4,096 bytes with no retained provenance |
| Reconstructed postclone whole scope | 50,168,705,024 | 46.723 | Preclone scope plus prefix and residual |
| Old guard ceiling with the fixture's zero unlinked-image allowance | 48,904,867,840 | 45.546 | Gate 0 baseline plus 1 GiB |
| Excess over old guard ceiling | 1,263,837,184 | 1.177 | Rejected by the obsolete whole-scope invariant |

Thus the reconstructed whole-scope change was 2,337,579,008 bytes (2.177 GiB): 34,107,392 bytes of preclone nonlease drift, 2,303,463,424 bytes of current prefix allocation, and 8,192 bytes residual. The fixture's output allocation was zero. APFS shared extents cannot be independently apportioned from these inode measurements; no clone-sharing credit is assumed. The actual failed assertion's exact raw scope was unavailable, so the fixture is the evidence boundary for this historical calculation.

### Corrected contract, identity binding, APFS, and concurrency

Gate 0 now retains both the full observed allocation and the exact nonlease baseline. PrefixLease resolves its exclusions from the signed current-run manifest and authenticated materialization receipt, binding the run ID, prefix path and device/inode, active marker and parent identity, lease token, Gate 0 and manifest hashes, reservation, output directory, and current process. It checks exact equality of Gate 0, pre-materialization, post-materialization, and current nonlease scope. The lease-owned prefix and output remain charged in the whole-scope policy measurement; they are excluded only from the unrelated-growth comparison.

The measured materialized prefix must match the signed materialization receipt and remain within its manifest reservation. Current-run output remains within its signed persistent-output reservation. The post-materialization admission continues charging the full projected peak, including prefix reservation and uncertainty margin, and requires scope at or below 55 GiB and projected free space at or above 50 GiB. No APFS shared-extent credit is taken: `st_blocks` and inode de-duplication provide observed allocation, while APFS clone/COW sharing does not expose reliable physical ownership. The existing 256 MiB / 10% uncertainty rule is unchanged.

The storage allocation lock covers the final lease assertion and bound identity check through Wine process creation. Materialization compares exact nonlease scope before and after the clone, and the Gate 0 check rebinds the manifest, output, active marker, prefix identity, and exclusion set after its scan. Concurrent unrelated growth, a second active lease, stale identity, or reservation overflow fails closed. A 64 MiB nonlease increase is rejected; no arbitrary sub-55-GiB growth allowance was introduced.

The Z3 fixture and failure matrix passed: A no materialization delta; B materialization within reservation; C materialization at the reservation bound when policy allows; D over-reservation; E unrelated growth; F second active lease; G stale/wrong binding; H projected free below 50 GiB; and I scope/projection above 55 GiB. The fixture fails under the old 1 GiB whole-scope allowance and passes under the corrected model when Gate 0 binds the exact pre-materialization baseline. The full storage suite passed **449 tests**; Python compilation and `git diff --check` passed.

### Independent review

The independent GPT-6 Luna Max review in [the storage review artifact](../storage/g0c-launcher-independent-review.json) is **PASS**, with no remaining code findings. It reviewed the old guard arithmetic, exact nonlease comparisons, lease identity and reservation binding, pre/post materialization concurrency, external-growth rejection, 55/50 GiB policy, APFS uncertainty, and the Z3 regression. It also explicitly records that the historical failed assertion-time raw scope was not retained. Review answers: the obsolete whole-scope guard explains the reported failure; the fixture proves its byte arithmetic but not historical per-byte provenance; only the authenticated current lease/output can be excluded from nonlease scope; unrelated growth, >55 GiB, <50 GiB projected free, stale identity, and over-reservation still block; APFS remains conservatively charged; the corrected path is safe to retry after a passing Gate 0.

### Interrupted Wine authorization and current-state recovery

The fresh Wine authorization `step11d3b-smoke-20261012-190615-07dbd67c` passed Gate 0 PRE, materialized PrefixLease, passed `assert_prefix_lease`, launched the pinned Wine target, handed off PID 22715, and bound the expected target window. Its materialization receipt measured 2,303,463,424 bytes (2.145 GiB); pre/post nonlease scope was exactly 46,625,316,864 bytes, so the unexpected external nonlease delta was **0 bytes (0 GiB)**. Authorized current-run output grew by 20,426,752 bytes and remained below its 100 MiB reservation. Full scope and projected storage limits remained satisfied.

The screen stayed locked and the Wine window remained occluded by `CUALockScreenGuardian` and ChatGPT. Three activation attempts did not establish the required current two-second uncovered interval. The 20-minute watchdog stopped the runner at `start_gate=NOT_RELEASED`; the controller result is **FAIL / incomplete authorization**, not a graphics result. There is no valid uncovered state, POST/cleanup PASS, trace validation, or `quantitative_ready=true`. No calibration or Native run was started.

After fresh process and `lsof` checks found no process using the leased prefix, a locked recovery transaction revalidated the signed marker, in-prefix marker, manifest, Gate 0, materialization receipt, build inventory, output, and exact nonlease scope. It removed only prefix device/inode `16777229/97553889` through the no-follow descriptor-bound remover. At recovery, prefix allocation was 2,116,657,152 bytes, 186,806,272 bytes below the materialization snapshot and still below the signed 3 GiB reservation. Post-removal nonlease scope remained exactly 46,625,316,864 bytes. The signed `CURRENT_STATE_STORAGE_RECOVERY` receipt and retired marker are recorded under `experiments/storage/prefix-recovery-receipts/` and `~/Library/Caches/FGMetal/prefixes/.retired-leases/`. The exact final nonlease increase equals the 20,480 bytes of authenticated recovery artifacts; unexpected delta is **0 bytes (0 GiB)**. This is current-state reconciliation only: **historical cleanup is not certified, and no normal `prefix-cleanup.json` was fabricated**.

### Fresh Gate 0 after recovery

The post-recovery Gate 0 chain completed with Python 3.14:

- Preflight: **PREFLIGHT_PASS**, no warnings; scoped allocation 47,918,469,120 bytes (44.627552 GiB), free 67,679,768,576 bytes (63.031696 GiB).
- Storage-only smoke `step11d3-storage-policy-smoke-20261012-b2494f0eb2df4fc3`: **PASS**. It exercised the real `PrefixLease.materialize` → `assert_prefix_lease` → controlled process → cleanup path; 111 storage-policy tests and 24 focused Gate 0 checks passed with no skips. The real prefix was deleted, 2,303,463,424 bytes were reclaimed, and the prefix baseline was restored. No Wine app or graphics experiment ran.
- Finalization: **PASS**, `prefixlease_smoke_status=PASS`, no warnings; scoped allocation 47,918,686,208 bytes (44.627754 GiB), free 67,648,147,456 bytes (63.002247 GiB).
- Explicit blocker check: `[]`.

`git diff --check` passed after the append-only report update. No commit was created.

### Classification and exact next step

STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. BR9R remains **BR9R-N3 — INCONCLUSIVE**. Wine calibration is **NOT RUN**; Native foreground and Native runs 1 and 2 are **NOT RUN**. STEP 11D.3-C remains gated: **YES**.

The next step is for the user to unlock the Mac and bring the Wine target window to the foreground. Then repeat a fresh lifecycle-bound Wine authorization (refresh Gate 0 again if its one-hour validity expires). If it passes, continue directly into the approved 20 s uncovered / 40 s covered / 20 s restored calibration with the frozen coverer. Run Native foreground and the two Native cover runs only if that calibration passes. Do not start STEP 11D.3-C automatically.

### Gate 0 recheck after report append

The append-only report update occurred after the prior finalization. The following read-only blocker check returned `unrelated non-lease storage changed since Gate 0 certification`, correctly treating the new report allocation as post-attestation growth. The report append was the only project write after that finalization. Gate 0 is being refreshed again with the current report included before the next Wine authorization; no authorization may use the stale attestation.

### Resumed-turn status and Gate 0 freshness — 2026-10-13

The Gate 0 refresh described above completed on 2026-10-12. Its preflight was **PREFLIGHT_PASS** at 47,918,833,664 scoped bytes and 67,658,047,488 free bytes. Storage-only smoke `step11d3-storage-policy-smoke-20261012-923eaa067d2c4d16` was **PASS**: the real materialize/assert/controlled-process/cleanup path completed, 111 storage-policy tests and 24 focused Gate 0 checks passed with no skips, and cleanup reclaimed 2,303,463,424 bytes. Finalization was **PASS**, with scoped allocation 47,919,046,656 bytes, free space 67,664,027,648 bytes, and explicit blockers `[]`. The signed final attestation was created at 2026-10-12 20:44:47 UTC.

By the resumed turn at 2026-10-13 05:41 UTC, that attestation had exceeded the one-hour validity window. A desktop inventory showed CrossOver not running; selecting CrossOver for inspection timed out. No fresh Wine authorization, calibration, or Native run was started. Because this report revision postdates the 20:44 UTC attestation, that attestation cannot authorize a later Wine run; use a new complete Gate 0 chain after this report revision.

### Gate 0 refresh attempt after resumed turn — 2026-10-13

The canonical preflight was attempted with `/opt/homebrew/bin/python3.14` after the report update. It stopped before issuing a new preflight PASS because the synthetic build-retention suite returned **6 failures and 3 errors** across 449 tests. All nine affected storage-recovery tests were short-circuited by the real-system guard `a wineserver process is currently running on the system`; their expected synthetic blocker diagnostics were therefore unavailable. The storage-only smoke and Gate 0 finalization were not run, and no new PASS attestation was issued.

The first desktop inventory listed CrossOver as stopped. A CUA request to inspect CrossOver timed out; the subsequent inventory and process check showed CrossOver with `wineserver` and `winedevice` processes, but no Wine target was identified. Since the app window and active bottle could not be inspected, those processes were left running. No graphics runner or storage-only smoke was started by this failed preflight.

Gate 0 remains **expired / not recertified** for this resumed turn. Before another Wine authorization, close or otherwise safely stop the active CrossOver/Wine services, then rerun the complete authorized sequence: preflight, storage-only PrefixLease smoke, signed cleanup, finalization, and explicit blockers `[]`. The Wine authorization also requires the target window to be visible in a canonical uncovered state.

### Gate 0 recovery after desktop became ready — 2026-10-13

After the user reported that the desktop was ready, CrossOver was stopped and no `wineserver` process remained. The canonical Gate 0 preflight passed at 2026-10-13 05:48:53 UTC with no preflight warnings; it issued smoke ID `step11d3-storage-policy-smoke-20261013-4563454d577b484b`. Preflight measured 47,922,987,008 scoped bytes and 67,245,694,976 free bytes.

The authorized storage-only smoke **PASS** completed the actual `PrefixLease.materialize` → `assert_prefix_lease` → controlled process → cleanup path. All 111 storage-policy tests and 24 focused Gate 0 checks passed with no skips. The lease assertion passed, the prefix was deleted, baseline prefix state was restored, and cleanup reclaimed 2,303,463,424 bytes. No Wine application or graphics experiment ran. Its projected free-space admission was 63,665,346,969 bytes, above the unchanged 50 GiB minimum; the admission warning only noted that it fell below the 60 GiB preferred target.

This entry records the completed preflight and smoke before finalization. Since report bytes are part of measured project scope, a complete Gate 0 chain must pass after this report append before a Wine authorization can use it. The signed attestation is authoritative for the finalization and blocker result.

### Resumed Wine authorization blocked at Gate 0 PRE — 2026-10-13

The post-append Gate 0 chain completed with preflight **PREFLIGHT_PASS**, storage-only PrefixLease smoke **PASS** (111 storage-policy tests and 24 focused Gate 0 checks, no skips), signed cleanup and baseline restoration **PASS**, finalization **PASS**, and blocker list `[]`. Finalization measured 47,923,421,184 scoped bytes and 67,224,907,776 free bytes with no warnings.

A fresh lifecycle-bound authorization controller run, `wine-auth-20261013-062833-6a8c8502`, then stopped before creating a run manifest or PrefixLease. The runner's exact Gate 0 PRE blockers were: `unrelated non-lease storage changed since Gate 0 certification`; `signed experiment output paths changed while Gate 0 accounting was measured`; and `current free space changed by more than 2 GiB since Gate 0 attestation`. The observer had zero target samples and reported completion before a valid target PID handoff. No Wine target, graphics execution, cleanup receipt, calibration, or Native run resulted.

The controller's observer binary and its controller/observer records were written to the persistent BR9R2 evidence directory before `runner_b.py` took its PRE snapshot. Those setup files are outside the runner's signed current-run output binding, so Gate 0 correctly treated their project allocation as unrelated and the persistent output binding set as changed. The runner did not persist the numeric PRE free-space sample or its signed-output comparison inputs; the exact contemporaneous >2 GiB free-space delta and allocation-by-file are therefore **not recoverable from this attempt**. No cause is assigned to that separate free-space-drift blocker without a saved measurement.

The authorization controller and runner support `FGMETAL_BR9R_EVIDENCE_ROOT` under an owned mode-0700 `/private/tmp/codex-br9r2-*` directory. The retry will use that existing transient-root path, then rely on the run's normal signed manifest and Gate POST accounting for persistent experiment outputs. No storage threshold or mutation guard is relaxed. This report append requires another complete Gate 0 chain before retrying Wine.

## 29. Fresh Wine authorization passed; calibration blocked before target launch — 2026-10-13

### Gate 0 and lifecycle-bound Wine authorization

A fresh Python 3.14 Gate 0 chain passed before the Wine authorization. Preflight and the actual storage-only `PrefixLease.materialize` → `assert_prefix_lease` → controlled process → cleanup smoke passed for `step11d3-storage-policy-smoke-20261013-9f5a48a953fb4d91`. The smoke ran 111 storage-policy tests and 24 focused Gate 0 checks, with no skips; cleanup deleted the prefix and reclaimed 2,303,463,424 bytes. Finalization passed with scoped usage 48,311,001,088 bytes (44.993126 GiB), free space 59,152,982,016 bytes (55.090508 GiB), and the explicit blocker list `[]`.

Fresh lifecycle-bound authorization `wine-auth-20261013-092244-e3496b11` passed. Runner `step11d3b-smoke-20261013-092251-8bb784ca` had Gate 0 PRE and POST **PASS**, PrefixLease assertion and cleanup **PASS**, trace validation **PASS**, and `quantitative_ready=true`. The exact target was PID 74219, window 874; the public observer verified its image identity, visible/unoccluded window, and a 32.390283-second canonical uncovered streak with advancing telemetry before releasing the start gate. The 30.013216-second baseline measured 60.573316 acquired FPS, 59.840306 displayed FPS, and 22 raw-zero callbacks of 1,818 (1.210121%). PRE measured free 59,152,936,960 bytes and scope 48,311,001,088 bytes; POST measured free 56,310,898,688 bytes (52.443611 GiB) and scope 48,377,327,616 bytes (45.054897 GiB), with no Gate 0 blockers. No foregrounding receipt is claimed; the observer evidence establishes the exact eligible target state.

### Calibration attempt and current storage admission

The authorized 20/40/20 Wine calibration was started as `br9r2-wine-calibration-20261013-1309` with the frozen coverer hash `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc`. It stopped at runner Gate 0 PRE with `current free space changed by more than 2 GiB since Gate 0 attestation`. No target PID handoff occurred, the lifecycle observer ended before a valid handoff, no coverer action occurred, and `phase_metrics` is empty. The uncovered, covered, and restored intervals therefore have **no measurements**; this is a storage admission failure, not a graphics result. No Native foreground or Native cover run was started.

A subsequent read-only live admission calculation measured free space at 55,923,728,384 bytes (52.083031 GiB) and conservative scoped usage at 48,386,732,032 bytes (45.063656 GiB). The canonical storage-only smoke manifest reserves a 3,580,257,895-byte projected peak: 3,254,779,904 bytes peak demand plus 325,477,991 bytes uncertainty. Projected free space is 52,343,470,489 bytes (48.748656 GiB), short of the fixed 50 GiB minimum by 1,343,620,711 bytes (1.251344 GiB). Projected scope is 51,966,989,927 bytes (48.398031 GiB), within the unchanged 55 GiB ceiling. Thus a new full Gate 0 chain cannot be admitted at this measurement; the prior attestation is stale after free space drifted down 3,229,253,632 bytes from its finalization measurement. No threshold was relaxed and no replacement Gate 0 PASS is claimed.

During desktop inspection, CrossOver was not initially running. Selecting it opened its Home screen without a Bare Vulkan target; that empty CrossOver instance was then quit. Finder was frontmost on recheck; no Wine target PID or `wineserver` remained, while two older `winedevice.exe` processes (PIDs 32333 and 32334, started at 09:10) were left untouched. The lowest free-space sample in this resumed sequence was 55,672,627,200 bytes (51.849175 GiB) while the empty CrossOver Home instance was open. The highest live conservative scope in this sequence was 48,386,732,032 bytes (45.063656 GiB); the earlier report's larger experiment-wide maximum of 46.109550 GiB remains the recorded maximum.

### Classification and next step

STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. BR9R remains **BR9R-N3 — INCONCLUSIVE**. Fresh Wine authorization is **PASS**; Wine calibration is **NOT RUN / blocked at Gate 0 PRE**; Native foreground and Native runs 1 and 2 are **NOT RUN**. STEP 11D.3-C remains gated: **YES**.

Exact next step: make at least 1,343,620,711 bytes (1.251344 GiB) of additional safe free capacity available, with margin for live variation. Then rerun the complete Gate 0 preflight → storage-only PrefixLease smoke → signed cleanup → finalization → explicit blockers `[]` chain. After it passes, perform a fresh lifecycle-bound Wine authorization; if that passes, continue immediately into the approved 20-second uncovered / 40-second covered / 20-second restored calibration. Run Native foreground and the two Native cover runs only if the calibration passes. The Mac must be unlocked and the actual Wine test window must be in front for the calibration observer. No STEP 11D.3-C or commit was started.

### Post-append storage disposition and workspace check — 2026-10-13

After section 29 was appended, the final read-only admission measurement was free 55,922,057,216 bytes (52.081474 GiB) and conservative scope 48,386,797,568 bytes (45.063717 GiB). Reserving the smoke's 3,580,257,895-byte projected peak leaves 52,341,799,321 bytes (48.747100 GiB) projected free, a 1,345,291,879-byte (1.252899 GiB) shortfall against the 50 GiB minimum. This supersedes section 29's earlier point-in-time shortfall. The final full Gate 0 chain was not run because that canonical smoke admission is denied; the existing attestation remains stale. `git diff --check` passed, and the append-only report scan found no trailing whitespace. No commit was created.

For a retry, make at least 1.26 GiB of additional safe free capacity available, plus margin for live variation, then verify admission from a fresh measurement before starting Gate 0.

Rounding correction for the preceding post-append disposition: 1,345,291,879 bytes equals 1.252900696 GiB, which rounds to **1.252901 GiB** at six decimal places. The byte count and 1.26 GiB retry recommendation are unchanged.

## 30. Fresh BR9R2 admission blocked — 2026-10-13

A fresh storage inventory was taken before starting the requested Gate 0 or desktop sequence. The live inventory reported free space of **55,950,696,448 bytes (52.108147 GiB)** and conservative FG-Metal scope of **48,416,415,744 bytes (45.091301 GiB)**; the unlinked mounted-image upper bound was zero. The canonical storage-only PrefixLease smoke reservation remains **3,580,257,895 bytes (3.334375 GiB)**.

Admission calculation:

| Measure | Bytes | GiB |
| --- | ---: | ---: |
| Current free | 55,950,696,448 | 52.108147 |
| Current conservative scope | 48,416,415,744 | 45.091301 |
| Projected peak reservation | 3,580,257,895 | 3.334375 |
| Projected free | 52,370,438,553 | 48.773772 |
| Projected scope | 51,996,673,639 | 48.425676 |

Projected scope is below the unchanged 55 GiB ceiling. Projected free is below the unchanged 50 GiB minimum by **1,316,652,647 bytes (1.226228 GiB)**. This is the additional free capacity required to reach the mathematical minimum at this measurement and reservation; the requested several-GiB margin requires additional headroom beyond that amount. Admission therefore **FAILS**.

The sequence stopped at admission. No new Gate 0 chain, desktop precondition check, Wine authorization, Wine calibration, Native foreground, or Native occlusion run was started. No prior attestation or authorization was reused. STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY** and BR9R remains **BR9R-N3 — INCONCLUSIVE**. The accepted earlier Wine authorization remains historical evidence only; this attempt has no new phase metrics or coverage evidence. STEP 11D.3-C remains gated: **YES**.

Exact next step: make additional safe capacity available, targeting several GiB of projected-free margin, then repeat fresh admission before starting Gate 0. No threshold was changed and no discretionary deletion was performed.

### Later read-only remeasurement at stop

After the blocked result was documented, a second live inventory found free space had changed to **55,483,334,656 bytes (51.672882 GiB)**. Conservative FG-Metal scope remained **48,416,415,744 bytes (45.091301 GiB)** and the projected peak reservation remained **3,580,257,895 bytes**. At this later snapshot, projected free was **51,903,076,761 bytes (48.338507 GiB)** and projected scope was **51,996,673,639 bytes (48.425676 GiB)**. The 50 GiB projected-free shortfall is therefore **1,784,014,439 bytes (1.661493 GiB)** at the latest measurement. The scope ceiling still passes; free-space admission still fails. This later snapshot supersedes the initial admission snapshot for the next retry estimate. No Gate 0 or graphics step was started.

## 31. Fresh headroom, Gate 0 and Wine authorization pass; coverer calibration incomplete — 2026-10-13

### Fresh storage admission and Gate 0

After additional storage headroom was provided, a fresh read-only in-memory inventory measured **77,576,040,448 bytes (72.248318 GiB)** free and **48,416,481,280 bytes (45.091362 GiB)** conservative FG-Metal scope. The current canonical reservation is **3,580,257,895 bytes**: **3,254,779,904 bytes** projected peak demand plus **325,477,991 bytes** uncertainty. Projected free is **73,995,782,553 bytes (68.913943 GiB)** and projected scope is **51,996,739,175 bytes (48.425737 GiB)**. Both admission thresholds passed.

An initial `storage_inventory.py --help` invocation unintentionally ran that script's default inventory write. The admission values above came from the subsequent fresh scan captured in memory without writing or reusing that inventory file.

The Python 3.14 Gate 0 chain passed. Preflight first identified two empty, unleased, reclaimable build directories under `/private/tmp/firelink-win-api-check.oNqymm/target`; only those two empty directories were removed. The storage-only PrefixLease smoke `step11d3-storage-policy-smoke-20261013-73abf8010cb94d2b` passed materialization, assertion, controlled-process, signed cleanup, and finalization. It ran 111 storage-policy tests and 24 focused Gate 0 checks, with no skips; cleanup deleted the prefix and reclaimed **2,303,463,424 bytes**. The final Gate 0 attestation was **PASS**, and the explicit blocker list was `[]`. CrossOver and `wineserver` were not running; two unrelated older Steam/Battle.net `winedevice.exe` processes were left untouched.

### Fresh lifecycle-bound Wine authorization

Authorization `wine-auth-20261013-122609-c8b02a05` passed for run `step11d3b-smoke-20261013-122616-f8eae88a`. Gate PRE and POST, PrefixLease cleanup, PID/window handoff, canonical uncovered state, trace validation, and `quantitative_ready=true` all passed. The observer bound PID **97415** to window **1053**, verified the current Wine image identity, and proved **31.915997 seconds** of canonical uncovered stability across 50 qualifying samples with telemetry advancing. The 30.001702-second baseline had **1,815** callbacks, **60.496569 acquired FPS**, **59.829940 displayed FPS**, and **20 raw-zero callbacks (1.101928%)**. Its Gate PRE measured **77,546,151,936 bytes** free and **48,416,776,192 bytes** scoped; POST measured **77,475,422,208 bytes** free and **48,483,086,336 bytes** scoped.

### Frozen-coverer Wine 20/40/20 calibration

Calibration run `step11d3b-br9r-calibration-20261013-124203-9f4d1985` used the required immutable neutral coverer SHA-256 `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc`. Its Gate PRE and POST, PrefixLease cleanup, run validation, and quantitative trace readiness passed. The sequence itself **failed / was incomplete** because the coverer window never achieved verified full coverage.

| Phase | Result and measured metrics |
| --- | --- |
| PRE / uncovered, 20 seconds | **PASS**: 1,199 callbacks/acquisitions/displayed callbacks; 59.95 acquired and displayed FPS; zero raw-zero callbacks; `HEALTHY_REFRESH`. Next-drawable median/p99/max: 16.035459 / 16.696309 / 24.708667 ms. |
| COVERED, 40 seconds | **Not attained; no valid covered-phase metrics.** The coverer's `show` action returned `PASS`, but public window evidence reported `front_of_target=false`, no coverer CG bounds, `target_area_coverage_fraction=0`, and `target_fully_covered_by_geometry=false`. No stable COVERED streak was proven. |
| RESTORED, 20 seconds | **Not run / not measured.** A valid covered state never preceded restoration, so no RESTORED streak or phase metrics are claimed. |

The coverer process and its window were detected, but the window was not on-screen in front of the Wine target. This attempt does not demonstrate a defect in the immutable coverer. Native foreground and Native full-cover runs 1 and 2 were not started because calibration did not pass.

### Classification and next step

STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. BR9R remains **BR9R-N3 — INCONCLUSIVE**; the required Wine-versus-Native differential is still incomplete. The minimum measured free space in this resumed sequence was **77,300,002,816 bytes (71.991238 GiB)**, and the maximum conservative scoped usage was **48,621,400,064 bytes (45.282207 GiB)**, both at calibration Gate POST. STEP 11D.3-C remains gated: **YES** and was not started.

Exact next step: determine why the frozen coverer's window was not on-screen or geometrically in front of the Wine target. Then take a fresh storage admission and complete a new Gate 0 chain and lifecycle-bound Wine authorization before retrying the same 20/40/20 calibration. Start Native foreground and its two full-cover runs only after that calibration passes. No storage policy, Gate 0, PrefixLease, controller, observer, trace-validation, or coverer semantics were changed.
