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

## 32. BR9R2-Z6 — Frozen-coverer attainment diagnosis and Wine retry — 2026-10-13

### Frozen binary and SHOW contract

The diagnostic and Wine retry kept the frozen coverer binary SHA-256 `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc`. Its matching source SHA-256 remains `8618bfa139df18d7da6c4db46518b8cf690c8c42bb568aa8470d118c8c6c0b82`; neither source nor binary was changed or rebuilt.

Source inspection of `experiments/dxvk_macos_step11d3b/neutral_coverer.swift` establishes the command contract:

- `DispatchSourceRead` is registered on `.main` and invokes `readCommands`; the normal app event loop remains in `application.run()` (`56–70`). `handle` runs synchronously from that handler (`72–92`). The exact main-thread identity is not emitted in telemetry.
- `SHOW` sets `isShown`, computes the target frame, calls `orderFrontRegardless()` and `displayIfNeeded()`, then writes `snapshot` (`108–116`). The timer retries target placement every 250 ms while shown (`61–64`). The window is retained in a strong `let`; `isReleasedWhenClosed` is false (`26–33`, `51`).
- Initialization sets accessory activation policy, modal-panel level raw 8, opacity and alpha 1, ignores mouse events, and prevents key/main status (`40–52`). It does not call `makeKeyAndOrderFront` or activate the target or coverer app.
- `snapshot` reads `CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], ...)` from inside the coverer process (`173–203`). Its `status` is `PASS` when the command handler supplied no internal failure (`199–203`). That is an acknowledgement of completed handler work, not a stable external state guarantee.
- The binary exposes PID/window number, `isVisible`, alpha, opacity, level, app-active/key/main flags, public CG bounds/layer/rank, and coverage. It does not expose `NSWindow.frame`, `screen`, `isMiniaturized`, `occlusionState`, or main-thread identity. `isVisible=true` alone cannot establish external attainment.

The controller already changed to record `COMMAND_ACCEPTED` separately from `PENDING_EXTERNAL_WINDOWSERVER_ATTAINMENT` and to require the independent lifecycle observer's exact, stable state before accepting coverage. Its 10-second wait remains fail-closed. The current `coverer_sequence.py` source hash is `e19593f6037b0732ecfa526c84d71365bd97d3549521b3bb98ec3f003c07d8d8`; independent GPT-6 Luna Max review of that exact controller source passed. The authorization watchdog correction described below was also independently reviewed at its exact source hash. No further controller change was made in this retry.

### C1 and geometry round-trip

The frozen binary is **C1 — COVERER CAN MATERIALIZE ONSCREEN IN SIMPLE GEOMETRY**. In the simple non-Wine probe, the independent public window observer found the exact coverer PID/window on-screen in 281 of 290 samples. Its CG bounds were `(220,514,245,271)` and it covered 70.017% of the probe target. This proved on-screen materialization, but not full containment in that simple geometry. `HIDE` removed it from all 20 subsequent on-screen samples. The retained artifacts are `evidence/br9r2/coverer-self-visibility-20261013-01/` and `evidence/br9r2/coverer-self-visibility-20261013-02/`.

The controlled Wine-equivalent rectangle was CG `(68,62,640,392)`. On this single-display test, the screen's AppKit frame was `(0,0,1470,956)`, visible frame `(0,0,1470,923)`, and CG display bounds `(0,0,1470,956)`; backing scale was 2. The conversion in source lines `158–170` gives `x=68`, `y=956-(62+392)=502`, `width=640`, `height=392`. The frozen binary does not report its `NSWindow.frame`, so that frame is inferred from the conversion and captured screen geometry. In two controlled repeats, independent CGWindow evidence showed exact coverer bounds, layer 8/rank 1 ahead of target layer 0/rank 2, and full containment. First on-screen/full-cover samples were 24.528/116.079 ms in repeat 1 and 12.427/91.724 ms in repeat 2; 273 and 277 samples respectively retained exact full/front geometry. `HIDE` removed the window. This controlled target was not a Wine PID.

The actual Wine run below independently confirms the same round-trip: target CG bounds and coverer CG bounds both resolved to `(68,62,640,392)` on expected display 1. This rules out a demonstrated Y inversion, geometry mismatch, or wrong-display assignment for this one-display target. It does not test multi-display origins. No direct coverer `NSWindow.frame` was available.

### Gate 0 and fresh Wine authorization

After correcting the auth controller's deadline handling, a fresh Python 3.14 Gate 0 chain passed using storage smoke `step11d3-storage-policy-smoke-20261013-44a239deeae54f55`. The real PrefixLease smoke ran 111 storage-policy tests and 24 focused Gate 0 checks with no skips; cleanup deleted the prefix and reclaimed 2,303,463,424 bytes. Finalization passed with free space 76,873,392,128 bytes (71.594 GiB), conservative scope 48,736,886,784 bytes (45.374 GiB), and explicit blockers `[]`.

The first fresh authorization attempt, `wine-auth-20261013-160612-c79c0e0e` / runner `step11d3b-smoke-20261013-160619-7f0921d4`, reached a valid start gate but failed to produce a passing authorization: its 1,200-second watchdog continued from runner launch and expired about 59 seconds after gate release. The controller was corrected to retain the bounded gate-acquisition deadline and start one fresh bounded completion deadline after the qualifying signal and controller record are durable; a watchdog trigger also remains a sticky PASS disqualifier. This controller's source hash is `4ae1eaba2f8f589de3e506a2a22d15639139d5fed4cccd6cb80c87d6e65d2e88`. Independent GPT-6 Luna Max review passed that exact hash and confirmed the gate predicates were not weakened. Review caveats remain: failure of the post-kill wait can prevent proof of reaping, persistent record-write failure can prevent the final failure record, and `runner_b.py` children are not terminated as a process group; the direct runner is terminated/reaped when possible and PrefixLease retains its cleanup guard.

The fresh retry `wine-auth-20261013-165932-cd7d76cf` / runner `step11d3b-smoke-20261013-165939-2fd34a1d` passed Gate PRE/POST, PrefixLease cleanup, canonical uncovered handoff, trace validation, and `quantitative_ready=true`. The observer bound PID 13909/window 1317 and proved a 31.782478-second canonical uncovered streak across 50 samples before release. Its 30.002561-second run measured 60.428174 acquired FPS, 59.828225 displayed FPS, and 18 raw-zero callbacks of 1,813 (0.992830%). Gate PRE was 76,857,028,608 bytes free and 48,736,886,784 bytes scoped; POST was 76,783,439,872 bytes free and 48,803,184,640 bytes scoped. PrefixLease cleanup passed and reclaimed 2,116,657,152 bytes.

The first calibration invocation, tag `z6-20261013-1716`, stopped before launching Wine because it used a different private evidence root from the successful authorization; the runner correctly rejected the authorization's `public_window_start_signal` path binding. It had no target PID handoff and no calibration metrics. The retry reused the authorization's owned mode-0700 evidence root, `/private/tmp/codex-br9r2-20261013-wineauth-02`, and passed the binding check. The failed invocation's diagnostic is retained at `/private/tmp/codex-br9r2-20261013-winecal-01/calibration.log`.

### Actual Wine 20/40/20 retry

The bound Wine run is `step11d3b-br9r-calibration-20261013-171838-46e1a8b2`, target PID **16109**, CGWindow **1330**. Its 90-second runner trace and quantitative validation passed (`run-result.json` SHA-256 `b19e8aeb5a906d9bfdbbb41c5dd2c56c1b41b319ac72ce3a3d75b0271653ca28`); the outer coverer sequence failed its state gate. The 90-second trace contained 5,377 frames, 5,354 displayed frames, 23 presentation drops, 59.734691 acquired FPS, and 59.479178 presented FPS. These whole-run numbers are not covered-phase results.

| State | Result and valid measurements |
| --- | --- |
| UNCOVERED / PRE, 20 seconds | **PASS**: 1,200 acquisitions, callbacks, and displayed callbacks; 60.000 acquired/displayed FPS; 0 raw-zero callbacks; `HEALTHY_REFRESH`. Next-drawable median/p99/max: 15.986854 / 16.483124 / 21.052125 ms. Submit median/p99/max: 16.229896 / 16.646764 / 21.573792 ms. Observer: 31 samples, 22 telemetry sequence advances, 20-second measured interval, maximum sample gap 686.169 ms. |
| COVERED / requested 40 seconds | **NOT ATTAINED under the accepted combined predicate; no valid covered renderer metrics.** The first SHOW response arrived 136.487 ms after command handling began with `status=PASS`, `isVisible=true`, but null coverer CG bounds, no coverer rank, `front_of_target=false`, and coverage 0. The controller recorded `COMMAND_ACCEPTED` and kept the window state pending. The first independent lifecycle sample with the exact coverer appeared 3.417660 seconds after SHOW began. It then showed coverer PID **16185**/window **1337**, alpha 1, layer 8, bounds `(68,62,640,392)`, expected display, full containment, and on-screen rank 1 ahead of target PID 16109/window 1330 at rank 2. This geometric state held for 16 consecutive samples spanning 9.985064 seconds, with a maximum sample gap of 681.189 ms. |
| Target-owned telemetry during those 16 covered-geometry samples | **Contradicted the covered-state requirement**: all 16 telemetry records were fresh (age 145.579–881.026 ms) and advanced sequence 24→34, yet all reported `windowOccluded=false`, `visibleBitSet=true`, and `windowOcclusionState=8194`. The observer's `isCovered` predicate explicitly requires fresh `windowOccluded=true` and `visibleBitSet=false` when Wine telemetry is expected (`public_window_observer.swift:709–714`). Consequently its stable `coveredState` attained 0 qualifying samples, even though the independent CGWindow geometry predicate was true for the 16-sample streak. After the 10-second wait failed, the controller sent HIDE; the external observer next recorded the coverer off-screen at 17:33:20.313 UTC. |
| RESTORED / requested 20 seconds | **NOT RUN / not measured.** The controller did not enter a valid covered state and therefore did not measure a restoration phase. |

The exact cause of the *current calibration failure* is established: the test's strict covered predicate combines public full-cover geometry with target-owned AppKit occlusion telemetry, and the latter stayed visible while fresh. The current replay also proves the SHOW `PASS` is an acknowledgement rather than attainment and measures the first independent exact/on-screen sample 3.417660 seconds later. The original earlier Wine attempt's persistent 10-second absence is **not fully explained** by this replay; its exact trigger remains unproven. Current evidence does not prove a coverer binary defect: the frozen binary passed C1, attained exact external full-cover geometry against the actual Wine window, and was later removed by HIDE. The telemetry mismatch's lower-level Wine/AppKit cause remains unresolved.

Classification across the requested causes: **A is proven** (acknowledgement precedes attainment); **G is measured** (the independent observer's first exact/on-screen sample was 3.417660 seconds after SHOW began); geometry and display are correct for this single-display run; activation is not required for the observed geometry (the coverer app stayed inactive); **H is not proven**. The strict telemetry mismatch is the proven reason this replay could not pass calibration. No code or binary change was needed in this turn to record that result.

The raw controller record, observer result, and sample stream remain in the private transient root. The observer result SHA-256 is `72083deee879a36ee4a56dd7cbd6d7af550e23820bafd9fc2caea49dd82642ae`; the sample stream SHA-256 is `b27785c47313dae3f5a0a342f3e78ee5bd7bdd060c9687bb8d3259ab9ecdac80`. The outer controller status is **FAIL** with `coveredState.attained=false`; the underlying 90-second Wine runner status is **PASS**. Do not conflate those results.

### Storage bounds, classification, and next step

Across the fresh Gate 0/auth/calibration Gate snapshots, minimum actual free space was **76,630,392,832 bytes (71.367615 GiB)** at calibration POST. Maximum conservative scoped usage was **48,941,543,424 bytes (45.580364 GiB)**, also at calibration POST. Calibration Gate PRE/POST, PrefixLease cleanup, trace validation, and run `quantitative_ready` all passed; the cleanup deleted the prefix, reclaimed 2,116,657,152 bytes, and reported no Gate 0 blockers. The lower `free_peak_or_estimate_bytes` in the cleanup record is the policy's projected peak estimate, not an observed free-space sample.

STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. BR9R remains **BR9R-N3 — INCONCLUSIVE**. Frozen-coverer simple-window classification is **C1**. Additional coverer source/binary change required: **NO**. Frozen coverer SHA remains `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc`. Independent review: **PASS** for the exact changed controller sources listed above. Fresh Gate 0: **PASS**. Fresh Wine authorization: **PASS**. Wine calibration: **FAIL / incomplete** under the covered-state gate. Native foreground, Native cover run 1, and Native cover run 2: **NOT RUN**, gated by calibration failure. STEP 11D.3-C remains gated: **YES**.

Exact next step: trace why Wine's target-owned `[NSWindow occlusionState]` telemetry, sampled fresh from the target process, remained `8194`/visible during independently verified full on-screen coverage. Keep the existing covered predicate unchanged until that source path is understood. After that diagnosis, take a fresh storage admission and complete Gate 0 after this report append, then run a new lifecycle-bound Wine authorization and the same 20/40/20 calibration. Start Native foreground and its two cover runs only if the full Wine calibration passes. Do not start STEP 11D.3-C automatically.

`git diff --check` passed after this report append. No commit was created.

## 33. BR9R2-Z7 — Retrospective covered-geometry reduction and AppKit occlusion source diagnosis — 2026-10-13

### Retrospective answer and interval

The retained Z6 Wine/MoltenVK presentation pipeline **did not enter the BR8 fast-zero regime** during the externally verified full-cover geometry streak. The retrospective diagnostic classification is **G2 — HEALTHY-REFRESH**. This does not repair or pass the 20/40/20 calibration: the accepted combined covered predicate still attained zero samples because Wine's fresh target-owned AppKit telemetry kept its Visible bit set.

The geometry boundary comes from zero-based entries 45 and 60 in `observer-result.json`'s `samples` array (that JSON has no ordinal field). They are the first and final samples of 16 consecutive qualifying observations for coverer PID 16185/window 1337: on-screen, alpha 1, display 1, fully containing target PID 16109/window 1330, and ranked ahead. The first was `2026-10-13T17:33:09.648Z` / monotonic `93667076617291`; the final was `17:33:19.631Z` / `93677061681458`. Their first-to-final sample span is **9.985064167 seconds** and the largest adjacent observation gap is **681.189458 ms**. These are consecutive positive observations, not proof of the unobserved compositor state between polls.

The observer and trace clocks align without a fitted offset. The observer records `DispatchTime.now().uptimeNanoseconds`; Apple documents the Apple-platform `DispatchTime` clock as Mach absolute time and its uptime value as Mach absolute time converted to nanoseconds ([Apple DispatchTime](https://developer.apple.com/documentation/dispatch/dispatchtime/init%28uptimenanoseconds%3A%29)). The trace declares `mach_absolute_time`, nanosecond domain, timebase 1/1, and floor conversion. At sample 45 the WindowServer query completed at `93667076615291`, 2 microseconds before the recorded sample timestamp. At sample 60 it started at `93677059062958` and completed at `93677061679125`, 2.333 microseconds before the recorded timestamp. The conservative query-interior interval `[93667076615291, 93677059062958)` is **9.982447667 seconds**; endpoint query durations were 2.743333 ms and 2.616167 ms. The sample gap, rather than clock conversion, is the larger continuity limitation. The renderer remains near 60 Hz even on this conservative interior.

The selected half-open trace intervals are PRE `[93658300684000, 93666414605166)` (8.113921166 s), geometry `[93667076617291, 93677061681458)` (9.985064167 s), and POST `[93677749147250, 93686465980916)` (8.716833666 s). PRE and POST are the nearest 8–10-second controls with canonical uncovered state; POST begins at the observer's first post-HIDE/off-screen sample, `17:33:20.313Z`. The gaps between cohorts are excluded.

### Retrospective renderer metrics

The reduction binds target PID and Unix PID 16109, token `98506e94c1790cf60ffb5960e21d64e1`, and `trace_role=target`. Acquire, submit, and present requests use `WINE_SOURCE`; next-drawable and presented callbacks use `LAYER_SOURCE`. `MVK_BOUNDARY` duplicates are excluded. Present-to-callback latency is paired one-to-one by present sequence; acquisition, swapchain, image, and present identities have no missing, duplicate, or mismatched pairs.

| Cohort | Acquire / successful submit end / present request / callback | Positive / raw-zero callback | Acquired / displayed FPS | Zero fraction |
| --- | ---: | ---: | ---: | ---: |
| PRE | 487 / 487 / 487 / 487 | 487 / 0 | 60.020302 / 60.020302 | 0 / 487 (0%) |
| Geometry | 599 / 599 / 599 / 599 | 599 / 0 | 59.989599 / 59.989599 | 0 / 599 (0%) |
| POST | 523 / 523 / 523 / 523 | 523 / 0 | 59.998851 / 59.998851 | 0 / 523 (0%) |

Latency values are milliseconds in `p50 / p90 / p99 / max` order:

| Cohort | `nextDrawable` | Successful submit | Present-to-callback |
| --- | --- | --- | --- |
| PRE | 15.971583 / 16.285233 / 16.496415 / 21.052125 | 16.206500 / 16.413333 / 16.900682 / 21.573792 | 48.987167 / 49.257467 / 49.323022 / 49.355417 |
| Geometry | 16.055542 / 16.291383 / 16.415355 / 21.337958 | 16.246250 / 16.416142 / 16.610644 / 21.992083 | 49.020834 / 49.229526 / 49.323327 / 49.419125 |
| POST | 16.042083 / 16.298358 / 16.406443 / 24.915583 | 16.231042 / 16.433167 / 16.597879 / 25.265458 | 48.995666 / 49.270625 / 49.357540 / 49.413125 |

All ten full one-second geometry bins remained at 59–61 acquisitions with positive callbacks. This is incompatible with the observed BR8 pattern of roughly 117–120 acquisitions, near-zero displayed frames, very high raw-zero share, and 6–8 ms cadence. G2 is diagnostic only; the accepted covered predicate did not pass, and no valid covered calibration phase is claimed.

### Target state and comparison captures

In all 16 Z6 geometry samples, fresh telemetry advanced from sequence 24 to 34 and reported `appActive=true`, `windowKey=true`, `windowMain=true`, `windowVisible=true`, `windowMiniaturized=false`, `windowOcclusionState=8194`, `windowOccluded=false`, and `visibleBitSet=true` (telemetry age 145.579–881.026 ms). PRE and POST also had active/key/main true and Visible set. The independent healthy uncovered authorization baseline was 60.496569 acquired FPS, 59.829940 displayed FPS, and 20 raw-zero callbacks of 1,815 (1.101928%).

The accepted BR8 full-Finder capture differs: active/key/main were lost about three seconds after startup, well before the actual full-cover event; refresh stayed near 60/60 during the following roughly 17 seconds. Fast-zero began only after the AppKit Visible bit cleared around 20.272 seconds. Under full coverage the capture measured about 119.8 acquired FPS, zero displayed FPS, and 100% raw-zero callbacks, then returned to about 60/60 after restoration. Thus active/key/main loss alone did not track the onset; the Visible-bit transition co-varied more closely in BR8. Historical failed-authorization captures 133102 and 133849 showed about 119 acquire / 0 display / 100% zero, with 30 telemetry samples reporting Visible set false while inactive/non-key/non-main and not miniaturized; their external cover geometry and window identity are unknown. These are comparisons, not causal attribution.

### AppKit telemetry source and identity limit

`trace_loader.mm` selects the tracked `CAMetalLayer` and calls `owning_window_for_metal_layer`. It first accepts the delegate only when it is an `NSView` whose `view.layer` is that exact layer and `view.window` is non-null; otherwise it searches `NSApp.windows` content-view subtrees for that exact layer and verifies `match.window == window` (`1412–1426`). On the main thread, `read_window_telemetry_snapshot` reads live `[window isVisible]`, key/main, miniaturized, `[window occlusionState]`, alpha, level, frame, and screen; `window_occluded` is derived from the Visible bit (`1532–1571`). Off-main requests are dispatched to the main queue; AppKit notifications are delivered on the main queue, and a one-second main-run-loop timer emits periodic snapshots (`1574–1644`, `1801–1822`). The external observer reads the latest target-owned `LAYER_SOURCE window_telemetry` row with an age bound; it does not query AppKit cross-process.

Retained Z6 logs `window_id` as an `NSWindow` object pointer (`140389955705344`), not a WindowServer identifier. Its AppKit frame `(68,502,640,392)` maps to CG bounds `(68,62,640,392)` on the 956-point display, and the exact layer/window association is source-supported. Neither proves that this object is CGWindow 1330. Wine's telemetry did not contain `NSWindow.windowNumber`, so the Z6 NSWindow-to-CGWindow identity remains **unproven**.

The narrow source addition now records `NSInteger window_number = [window windowNumber]`, includes it in unchanged-snapshot comparison, and emits it as `window_number` while preserving the old pointer field. It is a source-only patch; it was not built into or emitted by Z6. Independent GPT-6 Luna Max source review **PASS** at `trace_loader.mm` SHA-256 `4abefe8a625f232995e45771a9b104f4c146a4487ec3f859da8481209a899bf9`; the review confirmed format alignment, explicit `-1` when no NSWindow is associated, and no rendering, controller, predicate, coverer, Wine, or MoltenVK behavior change. Interpretation caveat: Apple defines `NSWindow.windowNumber` as the app's window-device number and explicitly says it is not the global number assigned by WindowServer; a non-null window with no window device can also return `<=0` ([Apple NSWindow.windowNumber](https://developer.apple.com/documentation/appkit/nswindow/windownumber)). Therefore a future numeric equality is useful runtime evidence but is not, by itself, a documented identity bridge to CGWindow 1330. The diagnostic AppKit runs below happened to observe equal numeric values for each target and its CG row; that instance-specific result is not substituted for Wine evidence.

Retained WineForge 0.6.0.5 source describes a top-level `WineWindow` NSWindow, a `WineMetalView` returning `WineMetalLayer`, `nextDrawable` obtaining the view's top-level `window`, and support for child/latent child windows. Runtime telemetry identifies the layer class as `WineMetalLayer` in `winemac.so` SHA-256 `e39ec04fbe3db971e272e13209bffc5ccb059232015b565e5082e161069df199`. The exact retained-source-to-loaded-binary lineage is not proven, so this does not prove the number or structure of Z6 CG windows; the six windows owned by Wine are not classified as children or helpers from this evidence.

### Pure AppKit control — A2

The corrected standalone AppKit control used an ordinary opaque active/key/main NSWindow and the frozen coverer, with direct target-owned telemetry and an independent public CGWindow poller. Target PID 18590, AppKit window number / observed CG ID 1374, and coverer PID 18599/window 1379 were bound. The target stayed at `(68,62,640,392)`, display 1. **849 consecutive poller samples** over **9.904327500 seconds** satisfied full containment, alpha 1, on-screen geometry, and front-of-target ordering; maximum poller gap was 20.196417 ms. In seven samples the coverer was 2 pixels wider than the target but still fully contained it; the other 842 had identical rectangles. All 198 direct target telemetry samples aligned to this interval reported active/key/main true, visible true, not miniaturized, state 8194, and Visible set (maximum direct telemetry gap 51.987708 ms). This is **A2 — NORMAL APPKIT TARGET ALSO REMAINS VISIBLE** under the frozen neutral coverer. HIDE removed the coverer; subsequent polling confirmed it off-screen.

The controller's final `controller.json` says `FAIL` only because the frozen coverer remained alive after its `quit` response and was then SIGTERM'd. HIDE had succeeded and the independent poller had already captured the full-coverage interval and removal, so that process-cleanup failure does not change the geometry or direct telemetry result. The frozen coverer binary was not rebuilt or modified. The earlier 18:18Z attempt had no valid active/key/main baseline and partial geometry; the 18:26Z attempt stopped at a warmup-comparison precondition before SHOW. Neither contributes to A2.

### Optional Finder comparison

Because A2 occurred, a separate public Finder window was created, positioned over the same diagnostic NSWindow, and closed by the exact Finder window ID created in that run. Finder PID 632/window 1387 was at alpha 1, layer 0, rank 1 with exact bounds `(68,62,640,392)`; target PID 18895/window 1381 was rank 2 at the same bounds and layer. **1,200 consecutive samples** proved full containment for **14.222818417 seconds**, with maximum external sample gap 51.630208 ms. The 285 direct target AppKit samples aligned inside the interval all reported inactive/non-key/non-main, `isVisible=true`, not miniaturized, `occlusionState=8194`, and Visible set. After the test window was closed, 380 subsequent poller samples showed no Finder full cover and kept the target on-screen. No screenshot was used as evidence, and no pre-existing Finder window was closed.

This Finder comparison also retained the Visible bit on the standalone diagnostic target. It therefore found no AppKit-state difference between the neutral coverer and an ordinary full-size Finder window for this target. It does not reproduce the BR8 Wine-target transition, and it does not resolve whether Z6's layer-associated NSWindow is CGWindow 1330. The run controller recorded `result=CAPTURED`, poller exit 0, target exit 0, and Finder close `closed`. The launched Python wrapper nevertheless returned exit 1 because its `except BaseException` handler caught `SystemExit(0)` after capture; the exact at-run source is retained as `run_finder_control.at-run.py` (SHA-256 `0548c4f59c1463f8ec575bda9cb54b62e31a04d79957a89de6e02e766a5a5e06`). The wrapper source was corrected afterward but not rerun; this exit-status wrapper issue did not alter the captured data.

### Evidence, decision, and next step

Key retained hashes:

- Z6 observer result `72083deee879a36ee4a56dd7cbd6d7af550e23820bafd9fc2caea49dd82642ae`; observer sample stream `b27785c47313dae3f5a0a342f3e78ee5bd7bdd060c9687bb8d3259ab9ecdac80`.
- Z6 target trace `6917172beb7a407f9c309c68835fdd63bd83e65b6a21173ba6feca84debabd6f`; quantitative run result `b19e8aeb5a906d9bfdbbb41c5dd2c56c1b41b319ac72ce3a3d75b0271653ca28`.
- AppKit A2 public samples `43920bf3856d1a34d501e4f43ea7c2f53bfc4f568cf30ee22d24e8e3f3a9bdd3`; direct telemetry `cf1eb9edb794d442b0d70b70755062d77e2a03cc0c833474388774cd58cfbfe1`.
- Finder public samples `47a87c97ab726584682e6219a944d131832de02a4af5d3bc54713be0ce495421`; direct telemetry `ef70d529c73310a16273f2eadb18ebce6d4600aeef2d71bf23817ce8e5609d7a`.

The original retrospective interval methodology was independently reviewed by GPT-6 Luna Max and passed, including origin filters, query/clock alignment, and present-sequence pairing. The `window_number` source patch was separately reviewed and passed with the API identity caveat above. No covered-predicate change is proposed and no coverer change is required; neither was rebuilt. No Native run, Wine process, Wine prefix, or Gate 0 chain was started for these standalone AppKit/Finder controls. A fresh read-only admission preceded each live control; the second measured 76,553,289,728 bytes free and 48,980,123,648 bytes of conservative project scope with no blocker at the 16 MiB allowance.

Final status remains **STEP 11D.3-B CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**, **BR9R-N3 — INCONCLUSIVE**, and **STEP 11D.3-C gated: YES**. Native remains gated. Z6's retrospective G2 remains diagnostic and does not pass calibration. The next step is a fresh admission and Gate 0 before any Wine/prefix diagnostic, then a newly pinned proxy/runner binding that contains the reviewed telemetry field and a fresh lifecycle-bound Wine capture. Correlate the sampled AppKit object with the target WindowServer record without assuming the AppKit `windowNumber` is the global CGWindow ID. Keep the accepted covered predicate and frozen coverer unchanged until that source/identity path is understood; only start Native if a later full Wine calibration passes. D2 remains distinct. No commit was created.

Post-append `git diff --check`: PASS.

## 34. BR9R2-Z8 — WineMetalLayer-owning NSWindow identity and BR8 reassessment — 2026-10-13

### Accepted status and decision

STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**. BR9R remains **BR9R-N3 — INCONCLUSIVE**. Native remains gated, and STEP 11D.3-C remains gated. This identity capture does not change those accepted statuses.

The fresh capture is **I1 — STRONG UNIQUE RUNTIME CORRELATION**: the tracked WineMetalLayer resolves directly through its owning NSView to NSWindow number 1401, and only same-PID public CGWindow row 1401 matches its converted frame, lifecycle, level, alpha, title, and on-screen observation. Its numerical equality to CGWindow ID 1401 is supporting runtime evidence, not an API contract. The matched row is the Wine presentation target in this fresh run.

Per the requested I1 interpretation, the prior Z6 telemetry/geometry mismatch is now treated as genuine for the tracked presentation-window association: Z6's externally proven full-coverage interval had healthy rendering while the tracked NSWindow telemetry retained its Visible bit. Z6 itself did not record a historical `windowNumber` or a complete same-PID WindowServer inventory, so this conclusion does not claim a retrospective numerical join to Z6 CGWindow 1330. The new capture was uncovered and did not run a coverer.

### Telemetry build and independent review

The final telemetry source SHA-256 is `08d721e9871c9715594a2ec5e6c140ce2bfd26e9beeb4febc6109247f1ea596a`. It records the matched NSView pointer, lookup path, NSWindow object pointer, and `window_number`, alongside the requested frame, level, alpha, visibility, key/main, miniaturization, occlusion-state, and screen fields. The exact built proxy SHA-256 is `04bbfc075f6ab999d0538f4a5371f3abd65fb0f0e307be0dea56e47aaefd5157`; its build receipt is `7241a71e28197f5b889d8e255a655f06a6f46128dbf704044f663d589d979cb2`. The run manifest confirms that this proxy and source were loaded for the target process, rather than relying on source-only evidence.

The runner SHA-256 is `5c5e6f46475a4fbd45a453b9c29ccb5e164d994432edd589d58c1793e066892e`; the process-runner SHA-256 is `3f06ed3dc8b0d5a33a3bc2a92496756c07cf829ca6716da5e999b5a546469c`. The Wine runtime tree SHA-256 is `2fea825b205073bf011ce948ec4cbe6560d9d90e26982233ad05822a17a92bfe`, and the loaded MoltenVK SHA-256 is `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f`. The proxy was built for x86_64 with Apple clang 21.0.0 and the macOS 27.0 SDK. No rendering/presentation behavior, controller predicate, coverer, Wine, or MoltenVK binary was changed.

Independent GPT-6 Luna Max review passed for the exact telemetry source and observed identity/correlation. The reviewer verified the source, proxy, run manifest, trace, collector source/binary, and complete same-PID comparison. It confirmed the direct-delegate identity path and I1 classification. The separate WindowServer collector review also passed for full `.optionAll` target-PID retention, a separate on-screen query joined by `(owner PID, CGWindow ID)`, monotonic query bounds, and poller timing bounds. Review caveats retained here: the `.optionAll` query excludes desktop elements; the two WindowServer queries are sequential rather than atomic; and an on-screen row does not mean its pixels are unobscured. No covered-predicate revision is proposed, so there is no predicate change for review.

### Fresh storage admission, Gate 0, and lifecycle-bound capture

Fresh admission passed before the run using Python 3.14.8. It measured 76,522,369,024 bytes free and 48,991,813,632 bytes in conservative project scope. With the unchanged 3,658,691,380-byte projected reservation, projected free was 72,863,677,644 bytes (67.89 GiB) and projected scope was 52,650,505,012 bytes (49.02 GiB); both canonical thresholds passed. The fresh complete Gate 0 chain passed: preflight, the storage-only PrefixLease smoke (111 storage-policy tests and 24 focused Gate 0 checks), and final attestation. No storage threshold, policy, or Gate 0 architecture was changed.

The one new uncovered Wine run was `step11d3b-smoke-20261013-194952-8a6a0487`, target Unix PID 24642, experiment token `96c0d14fddba4e1ce65f8660cebbd583`. The run result SHA-256 is `8ada18a74867cfe59221398e2cab7e684f990db46ceffe40cba6fec7c00b65dc`. It is valid and PASS, with `quantitative_ready=true` and trace validation PASS. Gate PRE passed at 19:51:41.726888Z with 76,536,176,640 bytes free and 48,992,137,216 scoped bytes; Gate POST passed at 20:07:22.954078Z with 76,471,947,264 bytes free and 49,058,451,456 scoped bytes. Both had no blockers, warnings, or active PrefixLeases. PrefixLease cleanup passed, deleted the run prefix, and reported no cleanup error. The lifecycle observer and target PID/window handoff passed; it bound PID 24642 to public window 1401 and proved 32.407354666 seconds of canonical uncovered state over 50 samples with telemetry advancing. The start gate released after a 2.384733708-second stable streak. No coverer process or window was present at the gate.

The 30.013946-second target interval contained 1,805 callbacks, 1,795 displayed callbacks, and 10 raw-zero callbacks (0.554%). Acquired and displayed rates were 60.138710 and 59.805531 FPS. These are capture-health figures, not a new B1 or covered-phase result.

The WindowServer poller started from the experiment's exact MATERIALIZED lease marker at 20:00:44.182932Z, before target handoff, and finished PASS with 245 samples at 20:05:31.334Z. One initial wrapper did not attach it because its PID filter compared the PrefixLease process-runner PID with the controller runner PID. The attachment was corrected during this same live run by matching the unique experiment ID and MATERIALIZED marker; the reviewed collector was running before handoff, the final attachment result records return code 0, and no second Wine run was started. The original wrapper result with a null poller launch remains retained as evidence of that correction. Capture material was copied into the repository only after Gate POST passed.

### Layer → view → window proof and target-owned telemetry

The target trace contains 34 window-telemetry rows. Every row reports the same tracked CAMetalLayer pointer `105553120169632`, matched NSView pointer `140444097921328`, NSWindow object pointer `140444096852352`, `window_number=1401`, and lookup path `direct_delegate`. The source at `trace_loader.mm:1414–1426` accepts the delegate only when it is an NSView, `view.layer` is the exact tracked layer, and `view.window` exists; it returns that `view.window`. The subtree fallback is separately labeled and verifies the matched view's window. The emitted fields are at `trace_loader.mm:1548–1562` and `1686–1710`.

Across the 34 rows, the AppKit frame was `(68,502,640,392)` points on screen 1; level 0; alpha 1; visible, key, main, ordered, and not miniaturized; `occlusionState=8194` with the Visible bit set; and app active. The screen frame was `(0,0,1470,956)` with backing scale 2. The AppKit-to-CG conversion is `x=68`, `y=956-(502+392)=62`, width 640, height 392. Thus the converted rectangle is `(68,62,640,392)`. The telemetry layer, NSView, and NSWindow identities remained stable across all rows.

### Complete same-PID public window inventory and correlation

The collector retained both the complete target-PID rows returned by `.optionAll` (excluding desktop elements) and a separate `.optionOnScreenOnly` result. It retained all six Wine-owned rows through samples 21–152 (132 samples); the on-screen query returned only row 1401 in samples 22–152 (131 samples). The first target-active `.optionAll` query contained 70 global rows, including all six Wine rows. The `.optionAll` rows do not independently carry an on-screen boolean in this collector; “not returned” below means absent from the separate successful on-screen query during the active interval, not a claim about every lifecycle moment.

| CGWindow ID | Title and observed CG bounds | Layer / alpha | On-screen query and rank | Correlation |
| --- | --- | --- | --- | --- |
| 1400 | Empty title; `(0,456,500,500)` | 0 / 1 | Not returned; all-window rank 51–53 | **REJECTED** — incompatible bounds and no on-screen return. |
| 1401 | `FG-Metal Step 11D.3-B Bare Vulkan`; `(68,62,640,392)` during the stable interval | 0 / 1 | Returned in 131 samples; all-window rank 48–50 and on-screen rank 1 or 3 | **MATCH** — exact converted frame, title, level/layer, alpha, visibility, and temporal lifecycle. `windowNumber` is also numerically 1401. |
| 1402 | Empty title; initially zero bounds, then `(0,0,1470,33)` | 0 / 1 | Not returned; all-window rank 59–63 | **REJECTED** — incompatible bounds and no on-screen return. |
| 1403 | Empty title; initially zero bounds, then `(0,0,1470,33)` | 0 / 1 | Not returned; all-window rank 58–62 | **REJECTED** — incompatible bounds and no on-screen return. |
| 1404 | Empty title; initially zero bounds, then `(0,0,1470,33)` | 0 / 1 | Not returned; all-window rank 67–71 | **REJECTED** — incompatible bounds and no on-screen return. |
| 1405 | Empty title; initially zero bounds, then `(0,0,1470,33)` | 0 / 1 | Not returned; all-window rank 64–68 | **REJECTED** — incompatible bounds and no on-screen return. |

The four full-width 33-pixel rows and the 500×500 row are recorded as Wine-owned public rows; no child, helper, or other role is inferred. Candidate 1401 first appeared with zero bounds during creation, then reached the exact AppKit-converted rectangle and remained the only returned on-screen Wine row. In the final near-exit row it shifted to `(73,65,630,386)` with alpha approximately 0.18014 before disappearing. Its all-window and on-screen ranks fluctuated; no hierarchy meaning is inferred from those ranks. The complete public inventory JSONL SHA-256 is `1f73860ef7635e4e61008e1c832d2cc1f070dfefec31a82918851b7a0965c59b`; the exact collector source and binary hashes are `bf818f5513c0ecbbd573c18e296ac6b286961de678df68467ccceaa5feefd92a` and `630e28516d57d1bc99aa7b3e17154f39957b8da694f422c1df100d9fdf692bd3`.

The evidence supports **I1**, not I2, I3, or I4. The numerical `NSWindow.windowNumber == CGWindow ID` match strengthens this runtime correlation only; it is not treated as an API identity guarantee. The inventory confirms six public same-PID rows for this fresh run and one unique on-screen match.

### BR8 identity reconstruction — historical identity remains UNKNOWN

For accepted BR8 run `step11d3b-br8-state-05-20261011-010918-133301ef`, the retained telemetry kept a stable layer pointer and NSWindow object pointer with frame `(68,502,640,392)`, level 0, alpha 1, visible state, and screen 1. The retained Finder coverage snapshot lists Wine PID 10353 / CGWindow 27938 at `(68,62,640,392)`, layer 0, alpha 1, on-screen, rank 2, behind Finder PID 867 / CGWindow 27754 at rank 1. The converted AppKit frame, level, screen, alpha, and lifecycle therefore match the candidate. This is strong historical geometry/attribute consistency, not an exact identity join.

BR8 did not record the matched NSView pointer, NSWindow `windowNumber`, or direct-delegate-versus-subtree lookup path. The retained public snapshot contains selected target/coverer rows rather than the complete set of Wine-owned WindowServer rows. It cannot establish whether CGWindow 27938 was the telemetry window, recover a historical numerical bridge, or determine whether other Wine-owned windows were present. Current-run PID/window values cannot be projected across runs. **BR8 same-window identity: UNKNOWN.**

### Accepted BR8 transition chronology and uncertainty

The strict accepted E-run chronology is relative to its validated target host interval start, `origin_mach_ns=891044669644625`. At `+3.163756125 s`, the target resigned key; at `+3.163827292 s`, it resigned main; and at `+3.167127334 s`, the app resigned active. The target was inactive, non-key/non-main, visible, not miniaturized, and still had the Visible bit set. The first 10 seconds after key resignation contained 600 acquisitions and 600 displayed outcomes at 60/60 FPS with no raw-zero callbacks; refresh remained roughly 60/60 for about another 17 seconds. Key/main/activity loss alone therefore did not track fast-zero onset.

The retained Finder positioning action was requested at about `+20.073425 s` and returned at `+20.113744750 s`. At `+20.276346292 s`, the target emitted `NSWindowDidChangeOcclusionStateNotification`: the app remained inactive/non-key/non-main, `isVisible` remained true, the window was not miniaturized, and state changed to 8192 with the Visible bit clear. The first retained full-cover CG query interval starts at `+25.077187916 s` and ends at about `+25.115915 s`; in it Finder was rank 1 and Wine CGWindow 27938 rank 2 with the exact target rectangle. The action timing and first retained geometry sample bracket the event imperfectly; no continuous WindowServer snapshot proves the exact compositor transition time. The accepted analysis marks the controller-action clock join as not performed, so action times are chronology context rather than frame-level causal ordering.

Validated raw-zero callback chains occur immediately on both sides of the AppKit event: the last callback before it was at `+20.274253084 s` (2.093 ms before), and the first at/after it was at `+20.289399459 s` (13.053 ms after). These are not first-zero events. The first run-wide raw-zero callback occurred at startup (`+0.014336459 s`) and was sparse. The classifier's first sustained fast-zero segment is the one-second-bin interval `[20 s,60 s)`; its first qualifying bin begins 276.346 ms before the exact notification, and that bin boundary is not a frame-level onset. Across the 40-second segment the run acquired about 119.8 FPS, displayed zero frames, had 100% raw-zero callbacks, and showed roughly 6–8 ms `nextDrawable` cadence. The sustained symptom onset cannot be ordered precisely against the notification from these bins.

At `+60.145306917 s`, the next occlusion-state notification restored `window_occluded=false` and state 8194 with the Visible bit present. The Finder restoration request began at about `+60.068765291 s` and returned at `+60.109012083 s`; the first retained restored-geometry proof starts at `+65.057243166 s`. The complete first 10 seconds after the AppKit restoration notification contain 600 acquired and 600 displayed frames at 60/60 FPS with zero raw-zero callbacks. These event and callback timings are association evidence; they do not establish that clearing or restoring the Visible bit caused the renderer transition.

### Hypothesis assessment and next step

| Hypothesis | Evidence-weighted assessment |
| --- | --- |
| H1 — BR8 sampled the same presentation NSWindow and its Visible bit genuinely cleared | **Plausible, not proven.** The fresh I1 capture proves the current tracker resolves to the presentation target. BR8's frame/level/screen and stable tracked-layer/window telemetry fit its CG candidate, but the historic view/window-number/path and full PID inventory are absent. |
| H2 — BR8 and Z6 sampled different Wine NSWindow roles | **Less supported by the fresh I1 result, but not excluded for BR8.** No positive evidence identifies a different role; BR8's historical candidate set is incomplete. |
| H3 — a BR8-specific Wine/AppKit/WindowServer ordering or occlusion transition was involved | **Leading specific causal hypothesis, still unproven.** Z6's actual Wine full-geometry interval stayed G2 healthy, and Z7 neutral-coverer and Finder controls on the standalone AppKit target retained the Visible bit. BR8 uniquely aligns a Finder-associated transition, the Visible-bit change, and the fast-zero regime. No retained evidence isolates a hierarchy/order mutation as the cause. |
| H4 — another desktop/AppKit state caused both Visible-bit clearing and fast-zero | **Plausible and unisolated.** The captures do not isolate all desktop or AppKit state. |
| H5 — Visible-bit clearing was a co-symptom rather than the trigger | **Plausible.** Zero callbacks bracket the notification, and one-second classification bins cannot order sustained onset against the state change. |

The next step is one lifecycle-bound BR8-type Wine comparison that deliberately reproduces the Finder-associated ordering/occlusion transition, recording the same direct layer→view→window→number telemetry, the complete same-PID inventory, and high-resolution acquisition/present/callback and `nextDrawable` timing around the transition. Preserve the frozen coverer SHA-256 `1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc` and the covered predicate `windowOccluded=true && visibleBitSet=false`; do not repeat generic AppKit controls. Require independent GPT-6 Luna Max review before any future predicate proposal. Native remains gated until the B1 state is deliberately reproducible; STEP 11D.3-C remains gated.

### Retained evidence and final requested fields

Capture artifacts, including the full public inventory, exact poller source/binary, controller/observer records, and wrapper correction receipts, are under `experiments/dxvk_macos_step11d3b/evidence/br9r2/z8-identity-20261013/capture/`. The run result and trace are under `experiments/dxvk_macos_step11d3b/runs/step11d3b-smoke-20261013-194952-8a6a0487/`; the trace SHA-256 is `e9c31f353eb3c5b56240ac4d49ba6b44545754b58d607cb41146a39a2d912a29`. No second Wine run, coverer run, Native run, or predicate experiment was performed. No commit was created.

- STEP 11D.3-B: **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**
- BR9R: **N3 — INCONCLUSIVE**
- Telemetry instrumentation built and loaded: **YES**; reviewed source SHA-256 `08d721e9871c9715594a2ec5e6c140ce2bfd26e9beeb4febc6109247f1ea596a`
- Fresh Wine identity capture: **PASS**
- Tracked `NSWindow.windowNumber`: **1401**
- Same-PID public WindowServer windows: **6**
- Identity classification: **I1 — STRONG UNIQUE RUNTIME CORRELATION**; matched CGWindow ID: **1401**
- Window lookup path: **direct delegate**
- BR8 same-window identity: **UNKNOWN**
- Strongest remaining causal hypothesis: **H3, a BR8-specific Finder-associated AppKit/WindowServer ordering or occlusion transition; association only, no cause established**
- Covered-predicate change proposed: **NO**
- Coverer change required: **NO**; frozen hash preserved
- Native gated: **YES**
- STEP 11D.3-C gated: **YES**
- Exact next step: **a lifecycle-bound BR8-type Wine replay of the Finder-associated transition with full same-PID inventory and high-resolution renderer timing, preserving the current predicate and coverer**

## STEP 11D.3-BR9R2-Z9 — controlled Finder replay 1 disposition

### Result and scope

Replay 1 is **R5 — INVALID / INCOMPLETE**. The controller never proved the canonical uncovered PRE state, so the measured 20/40/20 sequence did not start. It did not create a dedicated Finder window, did not request Finder activation/position/restoration, and did not attain Finder full cover. This is **not R4**: Finder full cover was never attempted. Replay 2 was not run because replay 1 was invalid and its Gate 0 POST was blocked.

The timing classification is **T8 — insufficient timing evidence**. No Finder geometry, Visible-bit transition, detector onset, or restoration event can be ordered. In particular, the fast-zero-like renderer signature below was already present while the controller was still waiting for PRE; it is not a Finder-triggered transition and does not establish T5. Z9 therefore makes no causal update to BR8 and does not change the accepted STEP 11D.3-B / BR9R states.

### Predeclared protocol and independent review

The reviewed protocol remained unchanged: canonical uncovered baseline 20 s, exact Finder full cover 40 s, restored state 20 s; target rectangle `(68,62,640,392)`; no neutral coverer. The public WindowServer collector ran with a 50 ms requested cadence, retaining `.optionAll` same-PID Wine rows and a separate `.optionOnScreenOnly` join. Raw traces remain authoritative.

Before execution, the fixed diagnostic detector was declared as two adjacent qualifying trailing 500 ms windows. Each window requires acquire rate >=45/s, successful-submit rate >=45/s, callback count >=40, displayed rate <=1/s, raw-zero fraction >=95%, at least 20 `nextDrawable` gaps, median gap <=12 ms, and nearest-rank p90 <=14 ms. The detector was not retuned. No detector onset is assigned to this invalid run because no healthy canonical PRE was established and no Finder action occurred.

Independent GPT-6 Luna Max review passed for the pre-run protocol/bindings and for this failure classification. The reviewer independently classified R5/T8, confirmed no Finder action or measured PRE, rejected any trigger inference from the pre-existing fast-zero-like state, and advised fresh admission, Gate 0, and lifecycle authorization before any retry. No protocol or predicate change is recommended.

### Admission, authorization, and bound artifacts

The run-level fresh Gate 0 PRE passed with 82,179,457,024 bytes free and 49,216,278,528 scoped bytes. The current lifecycle-bound smoke authorization was `step11d3b-smoke-20261014-112011-087f5dc3`, status PASS; the controller recorded `required_hashes_match=true` and `smoke_authorization_quantitative_ready=true`.

The loaded bindings matched the accepted telemetry source/proxy, runner, process runner, Wine runtime tree, and MoltenVK identities: telemetry source `08d721e9871c9715594a2ec5e6c140ce2bfd26e9beeb4febc6109247f1ea596a`; proxy `04bbfc075f6ab999d0538f4a5371f3abd65fb0f0e307be0dea56e47aaefd5157`; runner `5c5e6f46475a4fbd45a453b9c29ccb5e164d994432edd589d58c1793e066892e`; process runner `3f06ed3dc8b0d5a33a3bc2a92496756c07cf829ca6716da5e999b5a546469c`; Wine runtime tree `2fea825b205073bf011ce948ec4cbe6560d9d90e26982233ad05822a17a92bfe`; MoltenVK `aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f`. The replay controller was SHA-256 `718152eb63f8215118549373aa10966caabf69395045ec197d7951978464b211`. The collector source/binary were `b2640c7b764e2592646cc8f2893abc86144656b91ec8bbf4ad89675520509d07` / `70711cb2e5fc7870280ffff14a6b22cecaaf82d267426c2b0059f846d612b7f8`.

The baseline experiment was `step11d3b-baseline-01-20261014-130823-44892b9b`, Wine PID 58921, target image SHA-256 `38525a48b11217238977eff6fa1f573ffed88a8534975c9477827e886ec0ec94`. Its requested runner duration was 300 s; recorded Wine-process elapsed time was 452.61023075 s. The controller's canonical PRE wait ended with an error before any measured phase or Finder action.

### Target identity, AppKit state, and public WindowServer evidence

The in-process identity tuple stayed stable in the failure evidence: tracked WineMetalLayer `105553143913888` -> matched NSView `140628519379200` by `direct_delegate` -> NSWindow object `140628519331808` -> candidate CGWindow 2405. This is internal identity continuity only. At the stop, the public target failed the required on-screen join (`UNIQUE_ONSCREEN_JOIN_FAILED`, zero joined candidates in the final two seconds); therefore I1-quality public correlation across replay phases was **not established**.

At the failed PRE check the latest in-process sample had `appActive=false`, `windowKey=false`, `windowMain=false`, `windowVisible=true`, `windowMiniaturized=false`, `occlusionState=8192`, and `visibleBitSet=false`. The exact-title `.optionAll` row was Wine PID 58921 / CGWindow 2405, bounds `(68,62,640,392)`, layer 0, alpha 1, rank 61, but it was not joined to the separate on-screen query. The observer's canonical-uncovered certificate was unattained; its longest qualifying streak was zero. Some earlier samples were transiently eligible, but no stable 2 s canonical state was certified.

The full collector JSONL is preserved at `/private/tmp/codex-br9r2-z9auth-retry-20261014-a31f4e79/z9-replay-1-20261014-130814-7dafc87e/public-cgwindow-inventory.jsonl` (90,296,534 bytes; 26,547 samples). The requested cadence was 50 ms; maximum observed sample gap was 209.769375 ms and maximum query duration was 155.210708 ms. Across startup/shutdown, the complete Wine same-PID row-ID set had five observed forms: no rows; `{2404}`; `{2404,2405}`; `{2404,2405,2413,2414,2416,2417}`; then `{2404,2413,2414,2416,2417}` after the target row disappeared. The target row joined the separate on-screen list in three disjoint periods, but the observer never certified a stable canonical PRE. These are public row-set/bounds observations only; no child/helper semantics are assigned. The inventory, including all rows and both query results, is retained without reducing it to a presumed target row.

The observer also recorded a fully opaque Firefox PID 37317 / CGWindow 665 row (rank 5, bounds `(0,33,1470,923)`) fully containing the target in an early sample at 13:25:07.733Z. Later samples no longer showed that row as an on-screen overlap. No Finder window was involved. Near controller failure the overlap list was empty while Wine itself was no longer joined to the on-screen list. A post-run public WindowServer snapshot showed the ChatGPT and Notes windows on the current Space; this diagnostic snapshot is not used as replay transition evidence.

The controller timeline contains only `controller_error` and `controller_finished`. No Finder creation/position/full-cover/restoration timestamps exist. No AppKit notification is attributed to a Finder action, no frame-level renderer onset can be aligned to Finder geometry or Visible clearing, and restoration was not exercised.

### Renderer evidence and absent event-aligned windows

At the controller's failed PRE check, the raw-trace-derived trailing 2 s diagnostic reported about 97.5 acquisitions/s, 97.5 submits/s, 0 displayed callbacks/s, raw-zero fraction 1.0, 195 callbacks, `nextDrawable` median gap 8.313916 ms, and p90 10.675708 ms. This is a fast-zero-like wait-state signature, not a detector-certified onset. The renderer-health precondition was false before any Finder action. The target host trace and validation artifacts remain in `experiments/dxvk_macos_step11d3b/runs/step11d3b-baseline-01-20261014-130823-44892b9b/`.

The requested event-aligned windows around (A) Finder full-cover attainment, (B) AppKit Visible-bit-clear notification, (C) sustained detector onset, (D) Visible restoration, and (E) restored external geometry are **not applicable / insufficient data**: none of those replay events occurred. PRE, pathological treatment, and RESTORED interval acquire/display/zero metrics are likewise **not valid measured phases**. The wait-state diagnostic above must not be relabeled as PRE or as a Finder-induced pathological interval.

| Relative ordering | Result |
| --- | --- |
| Finder full cover -> Visible transition | Not observed |
| Finder full cover -> fast-zero onset | Not observed |
| Visible transition -> fast-zero onset | Not orderable |
| Timing class | **T8** |

### Storage result, hypothesis, and next step

PrefixLease cleanup passed and removed the temporary prefix. Its receipt recorded expected persistent output of 314,572,800 bytes versus 575,672,320 bytes retained allocation estimate (261,099,520 unexpected persistent bytes). Gate 0 POST was **BLOCKED** with these recorded blockers: signed experiment output exceeded its persistent reservation; unrelated non-lease storage changed since Gate 0 certification; signed experiment output exceeded its persistent reservation. POST free space was 81,306,374,144 bytes and scoped usage was 49,825,538,048 bytes, both within the numerical thresholds, but the blockers still prohibit another launch. The receipt's estimated low free-space point during the lease was 78,242,884,812 bytes. No storage policy or PrefixLease accounting change was made.

Z9 supplies no evidence that Finder caused this renderer state: the attempt began in an already unhealthy wait-state and never exercised Finder. The strongest supported Z9 statement is limited to that invalid-run observation; it does not strengthen or weaken whether Visible-bit clearing triggers BR8. Covered predicate change proposed: **NO**. Future cross-backend inducer: **unresolved**; this run cannot select Finder over the frozen neutral coverer or justify a methodology change. Native remains gated: **YES**. STEP 11D.3-C remains gated: **YES**.

Exact next step: after the user leaves a regular desktop active, resolve the existing Gate 0 POST reservation/non-lease blockers through the unchanged storage policy and obtain a fresh passing admission/Gate 0 plus lifecycle-bound authorization. Then retry replay 1 with the same reviewed protocol and current bindings. Do not run replay 2, Native, or STEP 11D.3-C until a technically valid replay 1 and the required independent review permit them. No commit was created.

## STEP 11D.3-BR9R2-Z9R reconciliation and execution update

### Required parallel reviews and reconciled conclusions

All six required GPT-6 Luna Max workstreams completed before implementation, with standalone reports at the repository root: A storage/Gate POST, B evidence budget, C actual-process PRE, D WindowServer/AppKit identity, E trace/detector, and F adversarial safety. The primary reconciliation is:

| Topic | Reconciled conclusion |
|---|---|
| Gate POST | The signed run-output tree was 598,573,056 allocated bytes at POST against 314,572,800 reserved: 284,000,256 bytes over. The receipt's 575,672,320-byte figure is a whole-project retained-allocation estimate, not signed-tree allocation. A later 127,115,264-byte `run-result.json` write raised the current signed tree to 725,688,320 bytes, 411,115,520 over the old reservation. The storage guard correctly failed closed. |
| Unrelated non-lease change | The blocker is real, but retained evidence lacks path/inode PRE-vs-POST inventories needed to decompose it. The 22,900,736-byte difference between the two overrun arithmetic values is not attributable from the saved records. No path is guessed and the guard is unchanged. |
| New output reservation | Subagent B independently recommended 4,155,506,688 bytes using measured byte rates and lifetime projections, but explicitly described it as a planning envelope, not producer-enforced. The implemented 4,974,444,544-byte reservation is higher because it also gives the collector a distinct 64-row-per-sample bound, 192-byte serialized-row bound, 512-byte sample overhead, 20 Hz cadence, and 2,900 s horizon (742,400,000 candidate bytes plus the base inventory); it separates signed and transient caps and carries a 10% model margin. This is a reproducible code-derived ceiling, not an arbitrary uplift. |
| Actual replay PRE | The prior authorization process could not establish PRE for PID 58921. At the actual replay's failed gate, the on-screen public join was absent and the stable canonical streak was zero. The fast-zero-like wait state was already present before any Finder action. The PRE failure was correct and must not be treated as a treatment result. |
| I1 and desktop state | Internal layer→view→window identity remained stable, but CGWindow 2405 did not pass the contemporaneous on-screen join; replay-phase I1 was not established. The earlier opaque Firefox overlap proves desktop coverage can change, but does not prove it caused the later renderer state. The requested 50 ms collector cadence remains; its 209.769 ms observed maximum gap shows cadence is not a coverage guarantee. |
| Renderer and detector | The wait state is BR8-like in phenotype only (about 97.5 acquisitions/s and submits/s, zero displayed callbacks, all 195 callbacks raw-zero, 8.313916 ms drawable median). It identifies neither onset nor cause. Per the user's explicit clarification, the frozen protocol and detector count thresholds per 500 ms window and govern unchanged; the task's rate paraphrase does not retune or redefine them. |
| Required implementation and tests | Implement the strict two-second renderer evidence-coverage check, covered-sampling and transition-bracket validity blockers, final signed-output accounting after durable run-result write, bounded signed/transient evidence enforcement, and replay-2 eligibility interlock. Add deterministic budget, cadence, malformed/burst-idle PRE, boundary, and replay-2 regressions. Keep Finder order/protocol, covered predicate, neutral coverer, detector, and all storage thresholds unchanged. |
| Live-run decision | NO-GO until a fresh complete Gate 0 passes. Fresh numerical admission alone cannot override a failed historical signed-output accounting check. |

The primary evidence resolves stale subagent claims that current code had no trace-integrity guard, no opaque full-cover handling, or no final storage linkage: those descriptions did not match the files reviewed in the current working tree. Conversely, Subagent D's cadence distinction and Subagent F's coverage concern were borne out and addressed in current technical-validity gates. The initial reviewer also identified a PRE burst/idle coverage hole; a regression now rejects a two-second window whose events are concentrated in an initial burst followed by an unobserved idle interval. No detector threshold, canonical PRE state, Finder sequence, covered predicate, or storage policy threshold was altered.

### Signed output, stream growth, and bounded reservation

At Gate POST, the signed manifest covered the complete run-output directory. Its reconstructed allocation was:

| Output class | Logical bytes | Allocated bytes | Manifest / timing / evidence status |
|---|---:|---:|---|
| Target host trace | 364,651,052 | 369,160,192 | Signed run tree; produced during target run; primary renderer evidence. |
| App trace | 197,310,536 | 197,312,512 | Signed run tree; produced during target run; primary app/renderer evidence. |
| Helper trace | 1,823 | 4,096 | Signed run tree; produced during run; supporting evidence. |
| Runtime/process logs | 1,179,674 | 2,105,344 | Signed run tree; produced during run; supporting evidence. |
| Copied runtime libraries | 29,865,008 | 29,872,128 | Signed run tree; copied before/during launch and required to reproduce the run. |
| Index, metadata, cleanup artifacts | 110,910 | 118,784 | Signed run tree; generated during/after run; required for identity and cleanup provenance. |
| **Gate POST signed tree** | **593,119,003** | **598,573,056** | **284,000,256 bytes above the old reservation.** |
| `run-result.json` | 127,112,105 | 127,115,264 | Signed run tree, written after Gate POST; required final result and validation evidence. |
| **Current signed tree** | — | **725,688,320** | **411,115,520 bytes above the old reservation.** |

The raw 90,296,534-byte WindowServer JSONL, controller record/timeline, and lifecycle observer files were captured in the owned transient evidence root, outside the signed run directory; they were not part of the Gate POST signed-tree bytes. Screenshots and offline analysis artifacts were absent (zero bytes). The report and six review deliverables were created after the old Gate POST and are non-lease project writes, not signed run contents and not the cause of the already-recorded POST blocker.

Measured logical growth rates from Subagent B's independent normalization were: target host trace 1,204,900 B/s; app trace 651,964 B/s; run result 420,011 B/s; runtime log 3,896 B/s; WindowServer JSONL 67,800 B/s; controller JSON 3,629 B/s; controller timeline 2.57 B/s; observer samples 2,655 B/s; observer result and latest files 4,027 B/s each; observer stdout 61 B/s; runner log 0.70 B/s. The separate collector candidate cap bounds its added cover rows at 256,000 B/s for at most 2,900 s, in addition to the observed-rate base inventory. These are final-file-normalized rates, not claims that arbitrary bursts cannot occur.

The bounded lifecycle model uses up to 1,540 s for target/run outputs; 2,900 s for the collector; 5,000 s for controller/timeline/runner logs; 1,801.5 s for observer outputs; a 1,200 s startup/handoff allowance; 60 s PrefixLease setup; 180 s canonical PRE acquisition; the declared 20/40/20 s measured phases; and 45 s cleanup. The conservative controller phase sum is 4,906.5 s, below its 5,000 s cap. Fixed allocation is 31,020,843 bytes, split into 29,972,267 signed bytes and 1 MiB transient control evidence. A 10% model margin is applied separately to signed and transient subtotals; the storage policy's own uncertainty margin remains separate and unchanged.

The resulting persistent-output reservation is **4,974,444,544 bytes**, split into **3,897,556,992 signed run-directory bytes** and **1,076,887,552 transient evidence bytes**. The separately enforced result-payload ceiling is 646,971,392 bytes; if the payload or aggregate tree would exceed its allowance, the oversized validation payload is replaced by a bounded summary carrying its digest and original byte count, while raw traces remain intact. The collector JSONL ceiling is 939,524,096 bytes. The controller admits only the derived reservation, runner/storage guards stop before the signed output boundary, transient evidence has a budget guard, and finalization remeasures durable signed output after the final result write. The measured-rate model remains empirical and supported by explicit producer/runtime caps and fail-closed monitoring, not a mathematical guarantee against arbitrary producer misbehavior.

### Implementation review and verification

Implementation Gate A was reviewed by fresh independent GPT-6 Luna Max reviewer Kepler (agent `01a13b5a-af53-70a1-bfcd-1e93f4af98a1`), who returned **PASS** after inspecting the reservation, manifest binding, unrelated-growth rejection, tests, and unchanged 55/50 policy. This was not the final post-execution independent review. That final review and the three post-run analysis agents were not reached because no valid treatment run occurred.

The Z9 replay-safety suite has **20 passing tests**, including exact reservation and one-byte overflow/admission boundaries, maximum lifecycle and collector bounds, two-second renderer coverage including burst/idle rejection, 100 ms sampling boundaries, and replay-2 rejection for failed/invalid replay 1. The storage policy suite (113 tests), process runner suite (4 tests), Swift type checks, Python compilation, and `git diff --check` passed. The 55 GiB scope ceiling, 50 GiB projected-free minimum, 60 GiB preferred target, 52 GiB warning, 25 GiB emergency floor, and uncertainty policy were not changed.

### Fresh admission and Gate 0 disposition

Read-only admission with the derived reservation passed before the Gate 0 attempt: free 71,024,947,200 bytes; scoped usage 49,938,821,120 bytes; 3 GiB prefix; 4,974,444,544-byte output reservation; 819,567,002-byte unchanged-policy uncertainty; projected free 62,009,710,182 bytes (57.751 GiB) and projected scope 58,954,058,138 bytes (54.905 GiB). It was above the 50 GiB projected-free minimum and below the 55 GiB scope ceiling; the 60 GiB preferred-free and 52 GiB scope-warning advisories were not blockers.

The first fresh Gate 0 preflight exposed stale independent-review hashes; those bindings were refreshed only after the current storage policy and tests were independently reviewed. The retried preflight then **BLOCKED** on exact historical signed-output accounting for `step11d3b-baseline-01-20261014-130823-44892b9b`, whose signed tree exceeds its original 314,572,800-byte manifest reservation. Gate 0 attestation is BLOCKED, PrefixLease smoke is PENDING/not run, signed cleanup and finalization were not run, and there is no fresh complete Gate 0 PASS. The storage snapshot at that blocked preflight had 71,028,445,184 free bytes and 49,938,898,944 scoped bytes. The previous run's PrefixLease cleanup PASS remains true but does not authorize a retry.

No current lifecycle authorization was attempted after this Gate 0 block. The earlier lifecycle authorization PASS belongs to the earlier, now-stale sequence and is not replay PRE. No actual retry Wine process, activation, canonical PRE, or Finder action was started. In the historical failed replay, PRE remained unattained; its latest telemetry sampled `visibleBitSet=false`, but no Finder-aligned transition occurred. I1 public correlation was **NO**. Healthy refresh PRE was **NO**. Finder full cover and restoration were **NO**; sustained fast-zero after Finder was **NO / not applicable**. The observed same-PID public Wine row-ID set did change over the historical collector interval, but no child/helper semantics are assigned. The old replay remains **R5 / T8**; replay 2 was not run.

### Final scope and exact next step

The strongest causal statement is unchanged: the invalid run began in a fast-zero-like wait state before Finder and contributes no trigger evidence. There is no causal update to BR9R. The future cross-backend inducer remains **unresolved**; do not select Finder or the neutral coverer from this run. STEP 11D.3-B remains **CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY**; BR9R remains **N3 — INCONCLUSIVE**. Native and STEP 11D.3-C remain gated. The six required work agents completed (6/6); post-run analysis agents are 0/3; final independent review is **not reached**. No commit was created.

Exact next step: resolve the old run's under-sized signed-output reservation through an explicitly approved, auditable history-preserving reauthorization or artifact archival path, without deleting, hiding, or rewriting its recorded R5/T8 or Gate POST failure. Then repeat current-hash read-only admission and the full preflight → storage-only PrefixLease smoke → finalization Gate 0 chain. Do not run lifecycle authorization or graphics until that complete Gate 0 passes. Replay 2, Native, and STEP 11D.3-C remain prohibited/gated.



## STEP 11D.3-BR9R2-Z9R-H1 — History-Preserving Terminal Closure

### A–F workstreams and reconciled findings

All six required GPT-6 Luna Max workstreams completed before implementation (6/6). Their standalone reports and copied source-controller evidence are retained under evidence/br9r2/historical-closure-review/.

| Workstream | Finding and disposition |
|---|---|
| A — Historical provenance | Confirmed the immutable run, reservation, embedded Gate PRE/POST, cleanup, result, controller, R5/T8, and current-tree anchors. The POST allocation of 598,573,056 bytes is a report reconstruction, not a numeric field in the embedded POST object. The current tree includes the later run-result write and measures 725,688,320 allocated bytes. |
| B — Gate 0 semantics | The signed-output snapshot scans authenticated ALLOW manifests without a terminal-run state. An over-reservation blocks and is not excluded from the nonlease comparison; its bytes remain in the whole-project total. The blocker is an integrity/accounting stop independent of the numeric 50/55 GiB limits. |
| C — Archive/retirement | No existing run-level terminal closure exists. Build and PrefixLease retirement concern other lifecycle objects. Physical archive/move would alter the manifest-bound path, inode/tree binding, and charged allocation, so no archive was used. |
| D — Safety | The initial design was NO-GO until exact identity, no-follow content inventory, live process/lease checks, race rechecks, and non-authorization were enforced. Those controls are implemented and bound into Gate checks. |
| E — Accounting | The old reservation stays fixed and the entire historical tree remains charged. Only this exact verified run's known reservation overrun is accepted as closed historical evidence; unrelated growth and the 50/55 GiB guards remain active. |
| F — Adversarial | The initial sketch was NO-GO pending regressions for copied certificates, stale/wrong hashes, active run/lease, symlinks, races, future mutation, authorization isolation, and threshold preservation. The implemented adversarial suite covers those fail-closed paths, and the independent post-implementation review returned PASS. |

Reconciliation: the original Gate POST failure and R5/T8 remain immutable; current Gate 0 had no run-level terminal-state concept; physical archival was not compatible with preserving the signed output tree; a narrowly scoped append-only closure was necessary. The accepted closure binds exact historical evidence and current tree contents, is non-authorizing, and leaves every output byte in project accounting. A–F report links: [A provenance](evidence/br9r2/historical-closure-review/Z9R-H1-A-PROVENANCE.md), [B Gate 0](evidence/br9r2/historical-closure-review/Z9R-H1-B-GATE0.md), [C archive](evidence/br9r2/historical-closure-review/Z9R-H1-C-ARCHIVE.md), [D safety](evidence/br9r2/historical-closure-review/Z9R-H1-D-SAFETY.md), [E accounting](evidence/br9r2/historical-closure-review/Z9R-H1-E-ACCOUNTING.md), [F adversarial](evidence/br9r2/historical-closure-review/Z9R-H1-F-ADVERSARIAL.md).

### Historical anchors and immutability

Historical run ID: step11d3b-baseline-01-20261014-130823-44892b9b.

| Record | SHA-256 before | SHA-256 after |
|---|---|---|
| Signed storage manifest | 11732b65ef09689e0cac94f3e0579e9c45f5af24def1129f8e7bfa910efc9d59 | 11732b65ef09689e0cac94f3e0579e9c45f5af24def1129f8e7bfa910efc9d59 |
| Original run manifest | e7e77a3185ea9d5c9a4e8fa1880e9ef4420d17c9bd675fd32f3d86eeb2b1e365 | e7e77a3185ea9d5c9a4e8fa1880e9ef4420d17c9bd675fd32f3d86eeb2b1e365 |
| run-result.json, containing PRE and POST | a321d23588ded6c6d1c1f23fa9d8093a544d85e81aec3810be0aa2d1dbfa7589 | a321d23588ded6c6d1c1f23fa9d8093a544d85e81aec3810be0aa2d1dbfa7589 |
| Embedded Gate PRE evidence | 147cdbca6047990992a6b30d3ee26430c70102f7208c7f06e53cef2b85cb3e0c | 147cdbca6047990992a6b30d3ee26430c70102f7208c7f06e53cef2b85cb3e0c |
| Embedded Gate POST evidence | 200aca7cd2edd190546342a0525fb6f7ca3076519a2937b3386049a2a6678c88 | 200aca7cd2edd190546342a0525fb6f7ca3076519a2937b3386049a2a6678c88 |
| Prefix cleanup receipt | 0e80c241e6f0b72cc7432d76a5182a3cc9c7faf91d2fc7db6ec31ac718013308 | 0e80c241e6f0b72cc7432d76a5182a3cc9c7faf91d2fc7db6ec31ac718013308 |
| execution-result.json | dca9ab61f70d36a2790243bd2a7b058c7dbda7e09f365f1f89d1f208aab3b4f6 | dca9ab61f70d36a2790243bd2a7b058c7dbda7e09f365f1f89d1f208aab3b4f6 |
| trace-index.json | 1e03ffab47bf76a1d3a440b639b232cf15a9238955e4c64cfd23de2ba3694582 | 1e03ffab47bf76a1d3a440b639b232cf15a9238955e4c64cfd23de2ba3694582 |
| Original report prefix, 160,968 bytes | 9012ccacc5a64dc9a8d0f5198059f791a10d514a7857c5dda3fdd5e6c337e00d | 9012ccacc5a64dc9a8d0f5198059f791a10d514a7857c5dda3fdd5e6c337e00d |

The exact current controller evidence is also bound: controller.json 1a7590578476931ceb9e816f7a1a15c507d63d6ec9abd36bcdedfec3771e638b; observer-controller f82e9e985108281595f56ec81f833321611ae9dedcdaa1278026bd0727d97da2; observer-result 17fb45ab4077ad13c642d0a8cb0032441f4a4797898e7245f962ae5f9f00b0aa; observer-samples d4787506c269a74ee0d7f1099ab4234e83737df63feb53bef407121c24930603; timeline 680fcb513c01e2e56e442c100b33ed0df16a386c68e2f5f16669bcbe56eb63f8.

Original historical facts remain: run result FAIL; original reservation 314,572,800 bytes; Gate POST BLOCKED; recorded POST allocation 598,573,056 bytes; current surviving allocation 725,688,320 bytes and logical size 720,231,108 bytes. The 598,573,056-byte POST value is reconstructed from the preserved report and category accounting; it is not a numeric field in the embedded POST object. R5 remains INVALID / INCOMPLETE; T8 remains INSUFFICIENT TIMING EVIDENCE; Finder remains NOT EXECUTED. The historical report prefix was exactly 160,968 bytes and its SHA remained unchanged before this appended section.

### Closure semantics, inventory, and liveness

The first append-only schema-v2 certificate is preserved unchanged at experiments/storage/historical-terminal-closures/step11d3b-baseline-01-20261014-130823-44892b9b.json, SHA-256 8b591c60099965513a7485890928f96af242a6ca3422a28ed20eb3440c140bcf. It was rejected because its writer omitted the required closure_path field. It was not edited, deleted, or treated as a valid closure.

The schema-v3 correction is revision 1 at experiments/storage/historical-terminal-closures/step11d3b-baseline-01-20261014-130823-44892b9b.correction-1.json, SHA-256 8201c356ce00f8b2577df90c4e3ed5e6bb229bacef4f1785eac0a6edb5628710. It pins the exact prior certificate SHA, exact run ID and original signed manifest hash/reservation, failed Gate POST and classifications, the immutable report prefix, no-follow per-entry content inventory, root identity, source/control hashes, and liveness evidence. Creation remeasured evidence/tree/liveness twice, used append-only O_EXCL plus O_NOFOLLOW and fsync, and revalidated after the durable write. The record explicitly says historical_failure_preserved=true, authorizes_execution=false, changes_original_gate_result=false, and counts_toward_project_scope=true. The closure is consumed only by historical storage accounting.

The closed tree has 20 entries (16 regular files), 720,231,108 logical bytes, and 725,688,320 allocated bytes. Its content/tree digest is 6b2c295fbce4e089de2ecd6bc133b9e06c22b80b2cd9e530cc5722cfff98eb57; root device/inode is 16777229/98341479. The current signed-output binding reports the same allocation and closure/tree digest. Read-only liveness revalidation found zero active PrefixLease markers, BuildRetention QUIESCENT, historical PIDs 58921 and 58942 GONE, no open prefix/output handles, and the prefix deleted. The stable trusted lsof identity was rechecked. Signed-output snapshot blockers after closure were [].

### Implementation, regressions, and independent review

Implementation is limited to experiments/storage/storage_policy.py and experiments/storage/test_storage_policy.py. The correction path accepts only the exact pinned legacy certificate after fresh matching historical evidence, liveness, tree inventory, root/path identity, metadata digest, logical/allocated totals, and report-prefix checks. Gate 0 still binds the closed tree on every read and blocks any changed, missing, symlinked, or substituted entry. The 314,572,800-byte reservation was not enlarged; closure does not create an execution authorization or project-wide allowance.

The focused H1 suite passed 19 tests, including successful exact-run closure, wrong run/manifest hash rejection, preservation of the original certificate, no correction on a wrong pinned hash, copied-certificate rejection, symlink/path substitution, tree races, unchanged-allocation content mutation, unexpected 64 MiB tree growth, active process/unknown liveness rejection, and non-authorization. The full Python 3.14 storage suite passed 471 tests. The smoke's embedded focused suites also passed: 132 storage-policy tests and 24 Gate0 checks, with zero skips. Python compilation and git diff --check passed.

The fresh independent GPT-6 Luna Max reviewer, not an A–F author and not the implementation author, returned PASS with no blocking findings at 2026-10-14 18:21:14 UTC. It confirmed the wrong-legacy-hash regression prevents correction creation and leaves the original certificate byte-identical. The G0C review now binds storage_policy.py SHA-256 c2feb4c3a607ad4bf92a1a023feffd431aa13fc2b620fc7e1cf59e819b72efc1 and test_storage_policy.py SHA-256 0cc610bd9988ab14348d3b32e288af4c405a381c6348285f6382f5cafa0ba0a7; all nine required G0C source/test bindings verified current. The 55 GiB project ceiling and 50 GiB projected-free minimum, as well as the 60 GiB preferred target and 25 GiB emergency floor, are unchanged.

### Fresh admission and first complete post-closure Gate 0

Read-only current-hash admission for the FUTURE-only reservation of 4,974,444,544 bytes passed with no blockers or warnings:

| Measurement | Bytes | GiB |
|---|---:|---:|
| Current free | 71,003,688,960 | 66.15 |
| Current full scoped allocation | 49,957,203,968 | 46.52 |
| Future-run peak reservation | 4,974,444,544 | 4.63 |
| Projected free | 66,029,244,416 | 61.50 |
| Projected scoped allocation | 54,931,648,512 | 51.17 |

The reservation is for future run(s) only and was not applied to the historical manifest. Both projections remain within the unchanged 50 GiB free-space minimum and 55 GiB project ceiling.

First post-closure Gate 0 sequence completed under Python 3.14:

1. Preflight PASS: free space 71,000,272,896 bytes; scoped allocation 49,957,351,424 bytes; smoke status PENDING; no warnings.
2. Storage-only PrefixLease smoke PASS: temporary prefix created and deleted; 2,303,463,424 bytes reclaimed; one baseline-restored prefix state; supervised command /usr/bin/true returned 0. No Wine application, graphics experiment, Native run, or lifecycle authorization was launched.
3. Finalization PASS: free space 71,011,725,312 bytes; scoped allocation 49,957,605,376 bytes; smoke status PASS; no warnings.
4. experiments/storage/storage_policy.py blockers returned [].

Smoke peak admission was 3,580,257,895 projected new bytes, with projected free 67,425,864,089 bytes and projected scoped use 53,537,609,319 bytes. Across post-closure admission and this Gate 0 cycle, observed free-space extrema were 71,000,272,896–71,011,725,312 bytes. The largest projected scoped figure for the declared future-run reservation was 54,931,648,512 bytes. Historical tree bytes remained charged throughout.

Because this report append changes the project inventory and Gate 0 attests to the current artifact set, a second complete preflight → storage-only PrefixLease smoke → signed cleanup → finalization sequence is run after this append. The final post-append attestation and blockers output are retained in the canonical storage Gate 0 artifacts. This section records the first completed cycle; the second cycle's canonical result is supplied in the task completion message. Do not edit this report after that finalization.

### Scope stop and exact next step

STEP 11D.3-B remains CASE B1 — BARE WINE VULKAN REPRODUCES DRAWABLE INSTABILITY. BR9R remains N3 — INCONCLUSIVE. The closure does not change those findings or the historical R5/T8 classifications.

This task stops at clean Gate 0. Lifecycle authorization was NOT RUN; no Wine application or graphics experiment was launched; Native remains gated YES; STEP 11D.3-C remains gated YES. Exact next step: separately review the final post-append Gate 0 artifacts and, only under a new scoped authorization, perform lifecycle authorization for a future run. Do not resume graphics from the historical run or treat the closure as run authorization. No commit was created.


## STEP 11D.3-BR9R2-Z9R-H2 — Fresh Gate and controlled-replay disposition — 2026-10-14 UTC

**Disposition: NO-GO for Finder replay with the reviewed current controller.** All six requested independent roles completed. The desktop tool limit permits at most three child workers alongside the orchestrator, so the six distinct roles ran in overlapping batches rather than six simultaneously. Their standalone reports are retained beside the frozen Z9 controller:

| Role | Report | SHA-256 | Reconciled result |
|---|---|---|---|
| A — H1 / Gate 0 | [Z9R-H2-A-GATE0.md](evidence/br9r2/z9-finder-replay-20261014/Z9R-H2-A-GATE0.md) | `f15a280fc5caaf4ec91d262c88db698f6246e1c6b4ad28e8ba5e97e3cc3a55ee` | Prior finalized snapshot was PASS after H1, but later `.git` writes make it stale. |
| B — storage | [Z9R-H2-B-STORAGE.md](evidence/br9r2/z9-finder-replay-20261014/Z9R-H2-B-STORAGE.md) | `c05a90bbe14d5e9368626fc15857cfd205b8bb17612292143deb2b5c3b6f876d` | Fresh numerical admission passes with two advisories. |
| C — actual replay PRE | [Z9R-H2-C-PRE.md](evidence/br9r2/z9-finder-replay-20261014/Z9R-H2-C-PRE.md) | `25aac522f40a7258c88c8cf77e03d2ba8624283f556ffd9f4810728181e98b58` | Required Visible-state predicate is impossible as implemented; prior replay never attained PRE. |
| D — WindowServer / I1 | [Z9R-H2-D-I1.md](evidence/br9r2/z9-finder-replay-20261014/Z9R-H2-D-I1.md) | `a4fb5388aeedece5624ad2fc8708f16ae01fa292d783c06e86282ed5c8b22af3` | Public on-screen join is correctly fail-closed; Finder starts during measured PRE. |
| E — trace / detector | [Z9R-H2-E-DETECTOR.md](evidence/br9r2/z9-finder-replay-20261014/Z9R-H2-E-DETECTOR.md) | `bf2e8c9a8a09d0ebba0d7ffb6d5eaed3405b704b784e28c7320d60665736f121` | Frozen detector is unchanged; healthy-PRE guard rejects burst/idle pseudo-coverage; current action order fails H2. |
| F — adversarial | [Z9R-H2-F-ADVERSARIAL.md](evidence/br9r2/z9-finder-replay-20261014/Z9R-H2-F-ADVERSARIAL.md) | `fc139d01d921febf26436799dc84607112eca1f19f7dc5b97541457190d59322` | NO-GO from stale Gate, impossible visibility predicate, and early Finder creation. |

### Reconciliation and current storage admission

The final H1 attestation was signed PASS at `2026-10-14T19:26:51.590472Z`, after the H1 report append. It recognized the schema-v3 correction-1 closure, kept the original historical tree charged at 725,688,320 allocated bytes, retained the original 314,572,800-byte reservation, and preserved the historical Gate POST failure. The original signed storage manifest remains SHA-256 `11732b65ef09689e0cac94f3e0579e9c45f5af24def1129f8e7bfa910efc9d59`; the valid closure remains SHA-256 `8201c356ce00f8b2577df90c4e3ed5e6bb229bacef4f1785eac0a6edb5628710`.

It is **not a current Gate 0**. After that finalization, 241 regular files under `.git` were written, including about 1.35 GB allocated. The storage policy counts `.git` within the project tree. Therefore item A.10 fails and the canonical H1 result cannot authorize this task. A complete new Gate 0 chain is required after these H2 reports and this report append.

The fresh read-only admission at `2026-10-14T20:15:47.359534Z` used the unchanged future-only 4,974,444,544-byte output reservation, a 3 GiB PrefixLease, and the policy's 10% uncertainty margin:

| Measurement | Bytes | GiB |
|---|---:|---:|
| Current free | 69,378,482,176 | 64.614 |
| Current conservative scope | 49,962,729,472 | 46.531 |
| Future-run output reservation | 4,974,444,544 | 4.633 |
| Prefix plus uncertainty-adjusted peak | 9,015,237,018 | 8.396 |
| Projected free | 60,363,245,158 | 56.218 |
| Projected conservative scope | 58,977,966,490 | 54.928 |

The hard admission bounds pass (projected free at least 50 GiB and scope at most 55 GiB). The below-60-GiB preferred-free and above-52-GiB scope warnings apply. Historical closed-tree bytes remain charged; no reservation was applied retroactively.

### Actual replay PRE, identity, renderer, and ordering

The current source hash for `z9_finder_replay.py` is `b4d1778f57ba928fe908e555aba84579fc15642a4fcd0a159464b494090bf734`. It computes `visible_bit_set` with `window_occlusion_state & 1` (lines 484–485) while canonical PRE requires both exact state `8194` and that bit to be true (lines 1480–1481). The installed AppKit SDK defines `NSWindowOcclusionStateVisible = 1UL << 1`, numeric value 2, in `NSWindow.h:188–189`. Since `8194 & 1 == 0`, the unchanged controller cannot satisfy its own required state. This is a source contradiction, not missing runtime evidence.

The same controller begins measured PRE for 20 seconds but schedules Finder activation at +3 seconds, creation of the treatment window at +4, raising at +5, and staging at +6 (lines 1694–1704 and subsequent action branches). H2 requires a full action-free 20-second canonical PRE and says to create the dedicated Finder window only after PRE. The checked-in predeclared protocol retains the earlier schedule. The role reviews agree that changing or running this sequence as-is would violate one of the user's frozen requirements; execution stops pending a reviewed reconciliation.

Static I1 safeguards require the unique same-PID public target row to join the successful separate on-screen query, and same-query cover classification fails closed on incomplete identity. The collector's 50 ms value is a requested cadence, not a sampling guarantee; actual gaps and query timings must be used. Renderer health is checked on the actual replay PID before treatment. Its independent coverage gate requires continuous acquisition, submit, displayed-callback, and nextDrawable streams. The focused regression `test_renderer_pre_gate_rejects_refresh_burst_followed_by_idle_tail` passed (one test); it confirms that high counts concentrated in an early burst do not pass the healthy-PRE coverage gate. The fast-zero detector SHA remains `af6c2b69893e6c59fb453f5e140901a711411ba0e75c68747f11eaef1fadd582`; its primary semantics remain count-based per 500 ms window and were not retuned.

The retained earlier replay remains `INVALID_OR_INCOMPLETE` / R5 and T8: no measured PRE, no Finder actions, failed public on-screen join, no accepted public CGWindow ID, zero canonical streak at the decision boundary, and unhealthy renderer (about 97.5 acquire/submit events per second, zero displayed callbacks, 100% raw-zero callbacks). Later observer samples cannot retroactively validate it. This is historical evidence only; it is not a new H2 replay.

### H2 execution fields at this report append

- H1 closure valid: **YES**. Historical manifest and original failure unchanged: **YES**.
- Final H1 post-append Gate currently verified: **NO** (the prior signed PASS is stale after post-finalization `.git` writes).
- Fresh storage admission: **PASS numerically**, with the two advisories above.
- Fresh lifecycle authorization: **not run at this report append**; it is permitted only after a fresh Gate 0 passes.
- New actual replay target / canonical PRE / I1 runtime correlation / healthy PRE: **not reached**.
- Finder full cover / Visible-bit clearing / fast-zero after Finder / healthy restoration: **not run**.
- H2 replay 1: **not run**; the earlier accepted Z9 replay remains R5. Replay 2: **not run and ineligible**.
- New timing classification: **none**; historical timing remains T8. New post-run analysis agents: **0/3**. Final independent reviewer: **not reached**.
- Future cross-backend inducer: **unresolved**. Native: **gated**. STEP 11D.3-C: **gated**.
- Evidence-budget source guards: **present**; no new run output budget was consumed.
- The required fresh Gate 0 chain will be run only after all H2 report writes and this append. Its final attestation and the post-run storage extrema are authoritative; no report edit will follow finalization.

No detector threshold, covered predicate, Finder sequence, neutral coverer, Wine, MoltenVK, or DXVK was changed. No commit was created.

## STEP 11D.3-BR9R2-Z9R-H2 — Authorization stop and final disposition update — 2026-10-14 UTC

### Gate 0 and admission used for the authorization attempt

The fresh post-H2-report Gate 0 chain passed before the lifecycle authorization. Preflight passed at `2026-10-14T20:19:07.519705Z`; storage-only PrefixLease smoke `step11d3-storage-policy-smoke-20261014-a60425043d42485e` passed, including signed cleanup, baseline restoration, and zero Wine/graphics launches; finalization passed at `2026-10-14T20:49:19.205070Z`. The canonical attestation is `experiments/storage/storage-gate0-attestation.json`, with smoke PASS and no storage warnings. Finalization internally re-read the attestation and returned PASS with no blockers.

H1 closure correction-1 remains SHA-256 `8201c356ce00f8b2577df90c4e3ed5e6bb229bacef4f1785eac0a6edb5628710`. The historical manifest remains SHA-256 `11732b65ef09689e0cac94f3e0579e9c45f5af24def1129f8e7bfa910efc9d59`; its original 314,572,800-byte reservation and Gate POST BLOCKED result are unchanged. Historical allocated tree bytes remain charged.

The post-finalization inventory measured 69,366,353,920 bytes free and 49,963,122,688 bytes of conservative project scope. For the future-only 4,974,444,544-byte output reservation and 3 GiB PrefixLease, unchanged policy uncertainty is 819,567,002 bytes. Total projected peak is 9,015,237,018 bytes, giving projected free space of 60,351,116,902 bytes (56.206 GiB) and projected scope of 58,978,359,706 bytes (54.928 GiB). The 50 GiB minimum-free and 55 GiB project ceiling pass. The below-60-GiB preferred-free and above-52-GiB scope advisories apply.

### Fresh lifecycle authorization

One lifecycle authorization was attempted with tag `wine-auth-20261014-210108-e03e2db6` and finished at `2026-10-14T21:03:32.938021Z`: **FAIL before target launch**. The runner's current source-authorization check rejected `experiments/dxvk_macos_step11d3b/wine_process_runner.py`: its `main()` did not satisfy the authorized PrefixLease process-ownership contract requiring one scoped lease block and `PrefixLease.start_wine_process` / `PrefixLease.wait_process`. The runner exited 1 before creating a run result or launching Wine. Consequently there was no target PID handoff, no released start gate, no Gate PRE/POST, no `quantitative_ready=true`, and no signed cleanup result. The observer recorded no accepted target PID and zero canonical uncovered samples. This is a prelaunch authorization failure, not a renderer result.

The raw authorization controller, runner log, and complete observer directory are retained byte-for-byte under `experiments/dxvk_macos_step11d3b/evidence/br9r2/` with the tag above. Controller SHA-256 is `487b8badeee04aba712d5535062f6fb63a5bf0390810bc8ea7b0aba2ba90abcf`; runner-log SHA-256 is `b003ba21a9fb9d85f5a5491465be0c0bec54b49a923f07571f634645b1e8fb22`; observer result SHA-256 is `7b8ec4c12d3a49c17f31a29654635285a95637267425c200ef813416d388541d`; observer samples SHA-256 is `dde01cbcb666b3bd3a69a260c957d4bd75bc968742872d4fa93e1a760631878c`. The recorded observer binary SHA-256 is `76352453c7ad6f596de4d98c9b47f6f5048309a4a1da1d5ca8e90f6f592e8281`.

### H2 phase disposition and next step

The lifecycle authorization failure requires STOP before any replay. Independently, all six reviews found the replay controller's `occlusionState == 8194` / Visible-mask contradiction and Finder actions scheduled inside the required 20-second action-free PRE. Therefore actual replay PRE, runtime I1 correlation, healthy replay PRE, Finder full cover, Visible-bit transition, fast-zero-after-Finder, and restored health were **not reached**. No replay 1 or replay 2 ran; historical Z9 remains R5 / T8. Post-run analysis agents remain 0/3, and the final independent reviewer was not reached. Future cross-backend inducer remains unresolved; Native and STEP 11D.3-C remain gated.

Before any new Wine authorization, separately review and reconcile the runner's PrefixLease source contract, the AppKit Visible mask for state 8194, and the conflict between the required action-free PRE and frozen Finder schedule. Then obtain a new scoped authorization. Do not start Finder replay 2 or change the frozen detector, treatment predicate, or historical evidence.

`git diff --check` passed after the README update and authorization evidence append.

This report is append-only. After this section and the README update are committed and published, a final complete post-append Gate 0 chain is run. Its canonical attestation, inventory, cleanup receipt, smoke, blockers, and storage extrema are the final authority; no report edit follows that finalization.
