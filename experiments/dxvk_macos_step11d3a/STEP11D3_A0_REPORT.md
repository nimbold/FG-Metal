# STEP 11D.3-A0: Native Experiment Lifecycle & Trace Readiness Report

**Date:** 2026-10-07T19:42:15+03:30  
**Status:** **NATIVE EXPERIMENT INFRASTRUCTURE READY — RERUN STEP 11D.3-A**  
**Gate 0 Status:** **PASS** (Blockers: `[]`)  
**Scope:** Infrastructure-only. Zero graphics processes executed. Zero Wine prefixes allocated (`expected_prefix_bytes = 0`). Presentation behavior unchanged. Uncommitted.

---

## 1. Executive Summary

STEP 11D.3-A previously terminated with **CASE A3 — INCONCLUSIVE** because the native experiment path was not prebindable under the repository's storage and provenance model, lacked a non-Wine supervision lifecycle, and possessed instrumentation with unacceptable synchronous file I/O perturbation on critical presentation threads.

**STEP 11D.3-A0** establishes a fully verified, auditable, and sandboxed execution and analysis infrastructure for native macOS Vulkan graphics experiments without weakening any storage, provenance, lifecycle, or cleanup guarantees.

All six core goals have been achieved and verified:
1. **Native Fresh-Build Contract:** Prebindable native MoltenVK build identity derived strictly from declared immutable inputs prior to execution, verified by `BuildLease.reserve`, canonicalized into the CAS (`experiments/storage/canonical-artifacts`), and auto-retired.
2. **Native Process Supervision Lifecycle:** Implemented `NativeRunLease` in `experiments/storage/storage_policy.py` providing descriptor-pinned Mach-O ARM64 validation, environment sterilization, 4-phase termination hierarchy, descriptor-isolated scratch execution directories, and 64KB HMAC-authenticated cleanup receipts (`native-run-cleanup.json`).
3. **Low-Perturbation Trace Pipeline:** Lock-free, non-blocking 131,072-slot POD ring buffer in `native_control.mm` and `moltenvk-instrumentation.patch` capturing monotonic timestamps (`mach_absolute_time`) and atomic sequence IDs at event sites, with asynchronous background drain thread logging without holding Vulkan or CAMetalLayer locks.
4. **Deterministic Frame Correlation:** Strict 6-tuple cascade frame join algorithm tracking `app_frame_id`, `acquire_seq`, `image_index`, `submit_seq`, `present_seq`, and `drawable` pointers.
5. **Presentation Observation & Validation:** Formalized `presentedTime == 0.0` as dropped/unpresented; implemented offline trace validator (`experiments/storage/trace_validator.py`) detecting ambiguous joins, sequence/timestamp reordering, and buffer drops.
6. **Gate 0 Admission Integration:** Seamlessly integrated `expected_prefix_bytes = 0` admission into `begin_experiment`, refreshed independent review bindings, ran complete preflight/smoke/finalization cycles, and proved Gate 0 **PASS** with zero active blockers.

---

## 2. Artifact and Source Provenance

### 2.1 Pinned Pristine MoltenVK
* **Pristine Source Location:** `/private/tmp/fgmetal-step11b/MoltenVK`
* **Release Version:** `v1.4.2`
* **Pristine Commit:** `db66022459ffb663aa2b50f6b018bc2e124f5edf`
* **Pristine Tree SHA-256:** `2a51f319d33f6e554a2d5e073ac86a1968f35a5746d298d46f6614b6dcd3f3f5` (7,631 files)

### 2.2 Prepared Experiment Assets
* **Instrumentation Patch:** `experiments/dxvk_macos_step11d3a/moltenvk-instrumentation.patch`
  * Size: 34,812 bytes
  * SHA-256: `df0771ff6ad60bd81c8fea82e06fa0fc4f34d4d00558d99ae0d15af8c0309506`
  * Git Apply Check: `git -C /private/tmp/fgmetal-step11b/MoltenVK apply --check --whitespace=error` (Exit Code 0, clean apply)
* **Native Control Application:** `experiments/dxvk_macos_step11d3a/native_control.mm`
  * Size: 81,304 bytes
  * SHA-256: `82c9d717734ff9257cdc4047cd3e3e4fafb26a015fc7f763a900c0b9bc63e06c`
  * Syntax & Type Check: `xcrun clang++ -std=c++17 -fobjc-arc -fno-modules -fsyntax-only` (Exit Code 0, clean)
* **Prepared Source Identity Record:** `experiments/dxvk_macos_step11d3a/prepared-source-identity.json`
  * SHA-256: `546bc4780f50798cf0aeb1c07a3b6ff73ed4d5a131390dcbd3be8b04c78e3494`
* **Verification Record:** `experiments/dxvk_macos_step11d3a/verification.json`
  * SHA-256: `79dba2bbf5fc80000387c62f573e80b53a2ff2841e874824b83a73aca699d0b6`
* **Artifact Hashes Manifest:** `experiments/dxvk_macos_step11d3a/artifact-hashes.json`
  * Binds all current patch, source, and audit hashes.

---

## 3. Native Build Contract Implementation

### 3.1 Architecture & Design Decisions
In legacy workflows, Xcode builds produced configuration keys dependent on post-build artifacts (e.g. `Info.plist`, intermediate XCBuildData logs) and defaulted to `unknown-legacy`. 

Under the fresh native MoltenVK contract in `experiments/storage/build_retention.py`:
1. **Prebindable Identity (`NativeMoltenVKBuildRequest`):** The configuration key (`compute_native_moltenvk_config_key`) is computed strictly from declared, immutable inputs known *prior* to build invocation:
   * Pristine MoltenVK commit (`db66022459ffb663aa2b50f6b018bc2e124f5edf`)
   * Pristine Git tree SHA-256 (`2a51f319d33f6e554a2d5e073ac86a1968f35a5746d298d46f6614b6dcd3f3f5`)
   * Instrumentation patch SHA-256 (`df0771ff6ad60bd81c8fea82e06fa0fc4f34d4d00558d99ae0d15af8c0309506`)
   * Source files modified list (`MoltenVK/MoltenVK/Commands/MVKCommandBuffer.mm`, `MoltenVK/MoltenVK/GPUObjects/MVKQueue.mm`, `MoltenVK/MoltenVK/Vulkan/vulkan.mm`)
   * Build system identity: `xcode_native` (explicitly representing Xcode, never `unknown-legacy`)
   * Target: `MoltenVK Package (macOS only)`
   * Build configuration: `Release`
   * Target architecture: `arm64`
   * Deployment target: `11.0`
   * Compiler / toolchain identity (`xcrun clang --version`, Xcode version)
   * Normalized build command and arguments
   * Declared output roles (`DeclaredOutputRole` mapping `libMoltenVK.dylib` as `PRIMARY_DYLIB`)
2. **Pre-Build Validation:** `BuildLease.reserve` validates the native request against the active storage manifest, verifies zero active build collisions, and asserts disk headroom before any compilation starts.
3. **Role Reconciliation & Output Verification:** Upon build completion, `BuildLease.complete`:
   * Hashes exact binary outputs.
   * Verifies outputs match declared expected output roles.
   * Scans for unexpected/undeclared binaries and fails closed if foreign dylibs exist.
   * Canonicalizes required artifacts into the Content Addressable Store (`experiments/storage/canonical-artifacts`).
   * Durably writes the build manifest.
   * Verifies canonical copies match manifest records.
   * Marks state `COMPLETED`, releases the build lease, and auto-retires the temporary build tree unless debug preservation was pre-approved.

### 3.2 Synthetic Test Coverage
Implemented in `experiments/storage/test_build_retention.py` (11 new tests, 98 total tests passing):
* `test_native_moltenvk_config_key_invariance_to_generated_metadata`: Proves intermediate build files and generated `Info.plist` do not alter the prebound configuration key.
* `test_native_moltenvk_altered_toolchain_changes_key`: Proves compiler/Xcode identity shifts produce distinct build keys.
* `test_native_moltenvk_altered_patch_hash_fails_reservation`: Proves altered patch digests are rejected at reservation.
* `test_native_moltenvk_altered_source_tree_fails_reservation`: Proves pristine source tree changes fail reservation.
* `test_native_moltenvk_altered_build_args_changes_key`: Proves build argument changes alter the configuration identity.
* `test_native_moltenvk_missing_mandatory_output_fails_completion`: Proves absence of primary dylib fails completion.
* `test_native_moltenvk_unexpected_binary_fails_completion`: Proves unexpected binary output aborts completion and flags hygiene violation.
* `test_native_moltenvk_cleanup_failure_records_recovery_intent`: Proves unremovable build directories trigger recovery intent markers without masking errors.
* `test_native_moltenvk_output_hash_mismatch_refuses_retirement`: Proves tampered build outputs refuse CAS promotion.
* `test_native_moltenvk_idempotent_completion_replay`: Proves duplicate completion calls handle re-retirement idempotently.
* `test_native_moltenvk_canonical_equivalent_reuse`: Proves verified canonical equivalents can satisfy requirements without recompilation.

---

## 4. Native Process Supervision Lifecycle (`NativeRunLease`)

### 4.1 Architecture & Design Decisions
Native graphics processes must **never** route through `PrefixLease.start_wine_process`, which clones a 3 GiB Wine prefix, starts wineserver daemons, requires Rosetta/x86_64, and initializes Windows registries.

Implemented `NativeRunLease` in `experiments/storage/storage_policy.py`:
1. **Zero-Prefix Enforcement:** Rejects manifests with `expected_prefix_bytes != 0` or `expected_prefix_requirement == True`. Zero prefix bytes are allocated on disk.
2. **Binary Integrity & Mach-O ARM64 Verification:**
   * Descriptor-pinned `open(O_RDONLY | O_NOFOLLOW)` inspects file identity.
   * `_verify_macho_arm64_header` validates Mach-O 64-bit header magic (`MH_MAGIC_64 = 0xfeedfacf`) and ARM64 architecture (`CPU_TYPE_ARM64 = 0x0100000C`). Rejects non-Mach-O binaries and x86_64 binaries.
   * Enforces canonical non-symlink paths and dev/ino stat stability during hashing.
   * Matches content SHA-256 against declared expected hashes for both executable and candidate dylibs.
   * Prohibits unapproved binaries from coexisting in candidate library directories.
3. **Sterile Environment Construction:**
   * Strips ambient environment variables starting with `DYLD_`, `LD_`, `__XPC_DYLD_`, `PYTHON*`, `PERL*`, `RUBY*`, `NODE_*`.
   * Strips execution hooks: `BASH_ENV`, `ENV`, `SHELLOPTS`, `CDPATH`, `IFS`, etc.
   * Rejects explicitly passed forbidden variables with `ValueError`.
   * Builds synthetic `DYLD_LIBRARY_PATH` strictly bound to verified candidate roots (and `/usr/lib`).
   * Freezes environment; post-sealing environment mutation is strictly forbidden.
4. **Isolated Scratch Execution Directory:**
   * Creates randomized scratch directory `.run_scratch_<exp_id>_<token>` inside `output_dir` via parent descriptor.
   * Launches child process with `cwd` set to scratch directory.
5. **Supervision and 4-Phase Termination Hierarchy:**
   * Tracks child PID and supervises execution against `timeout_seconds`.
   * Periodically samples disk free space during execution; if free space crosses the 25 GiB hard-stop threshold (`FREE_HARD_STOP`), immediately triggers emergency termination.
   * 4-phase process reap:
     1. `process.terminate()` (`SIGTERM`)
     2. Wait grace period (5.0s)
     3. `process.kill()` (`SIGKILL`)
     4. Wait grace period (5.0s) and reap child.
   * If a process remains unkillable, sets `_unsafe_live_process = True`, retains the scratch directory as quarantined, flags hygiene status `FAIL`, and sets active marker to `RECONCILIATION_REQUIRED`.
6. **POST-Run Cleanup & Receipt:**
   * Reaps all managed processes.
   * Closes open directory descriptors.
   * Recursively unlinks scratch directory via descriptor `_remove_child_directory_nofollow`.
   * Audits output directory log bounds (`max_log_bytes = 100 MiB`).
   * Measures final disk free space and project budget usage.
   * Persists an authenticated 64KB padded receipt (`native-run-cleanup.json`) signed with HMAC-SHA256 (`_storage_evidence_signature`).
   * Unlinks active lease marker on PASS; preserves `RECONCILIATION_REQUIRED` marker and invalidates Gate 0 attestation on any cleanup or storage failure.

### 4.2 Synthetic Test Coverage
Implemented in `experiments/storage/test_storage_policy.py` (`NativeRunLeasePolicyTests`, 21 tests, 73 total tests passing):
* `test_native_lease_normal_exit_and_receipt_verification`: Clean exit code 0, 64KB padded receipt verified with HMAC, scratch dir removed, active marker removed.
* `test_native_lease_nonzero_exit`: Child exit code 42 correctly captured, hygiene PASS.
* `test_native_lease_supervisor_exception`: Supervisor body exception recorded in receipt, POST cleanup completes, active marker removed.
* `test_native_lease_process_timeout_handling`: Process timeout expires, triggers termination hierarchy, records `TIMEOUT_EXPIRED` event.
* `test_native_lease_sigterm_handling`: SIGTERM signal terminates child processes and writes cleanup receipt.
* `test_native_lease_rejects_path_substitution`: Launching different executable path than bound raises `ValueError`.
* `test_native_lease_rejects_non_macho_or_wrong_arch`: Non-Mach-O and x86_64 binaries fail admission.
* `test_native_lease_rejects_sha256_mismatch`: Modified executable binary hash fails admission.
* `test_native_lease_rejects_symlink_executable_and_dylibs`: Symlinked executables and dylibs fail admission.
* `test_native_lease_rejects_unapproved_binary_in_candidate_dir`: Foreign dylib alongside candidate fails admission.
* `test_native_lease_rejects_forbidden_env_variables`: Passing `DYLD_INSERT_LIBRARIES`, `PYTHONPATH`, or `BASH_ENV` raises `ValueError`.
* `test_native_lease_rejects_env_mutation_after_sealing`: Mutating environment at `start_native_process` raises `ValueError`.
* `test_native_lease_rejects_wine_prefix_manifest`: Manifest requesting `expected_prefix_bytes > 0` fails admission.
* `test_native_lease_rejects_tampered_manifest`: Tampered manifest HMAC fails admission.
* `test_native_lease_rejects_stale_gate0_attestation`: Mismatched Gate 0 attestation hash fails admission.
* `test_native_lease_active_marker_collision`: Pre-existing marker collision fails admission.
* `test_native_lease_orphan_process_quarantines_scratch_and_invalidates_gate0`: Unreaped process quarantines scratch dir, marks `RECONCILIATION_REQUIRED`, and calls Gate 0 invalidation.
* `test_native_lease_cleanup_failure_invalidates_gate0`: Scratch directory deletion error marks `RECONCILIATION_REQUIRED` and invalidates Gate 0.
* `test_native_lease_log_size_exceeded`: Log file exceeding 100 MiB fails hygiene and invalidates Gate 0.
* `test_assert_native_lease_helper`: `assert_native_lease` helper validates active marker and rejects path discrepancies.
* `test_scoped_lease_with_native_run_lease`: `scoped_lease` context manager properly nests `NativeRunLease` and sets `current_lease()`.

---

## 5. Low-Perturbation Trace Pipeline & Frame Correlation

### 5.1 Instrumentation Perturbation Fix
* **Elimination of Synchronous File I/O:** The legacy instrumentation performed synchronous `fprintf` and `fflush` inside hot presentation paths while MoltenVK internal mutexes were held, introducing artificial latency and timing perturbation.
* **Preallocated Ring Buffer:**
  * Fixed-size ring buffer with 131,072 entries (`kRingCapacity`).
  * Compact POD event struct (`TraceEvent`) recording: event type, `mach_absolute_time` timestamp, thread ID, process-wide atomic monotonic sequence number, and integer/pointer identifiers.
  * Atomic monotonic ticket allocation via `std::atomic<uint64_t>::fetch_add(1, std::memory_order_relaxed)`.
  * Zero heap allocations (`malloc`/`free`) and zero filesystem syscalls in the event emission path.
* **Asynchronous Drain Thread:**
  * Dedicated background worker (`trace_drain`) polls the ring buffer with sleep throttling, formats events into structured JSONL, and flushes to disk outside driver locks.
  * Overflow tracking: atomic `gDroppedEvents` counter records and reports dropped events if the ring buffer becomes full.
* **Shutdown Flushing:** Graceful drain upon application shutdown ensures all queued events are durably persisted before process exit.

### 5.2 Deterministic Frame Correlation & Event Ordering
* **Event Identifiers:** Explicitly propagates and logs `app_frame_id`, `acquire_seq`, `image_index`, `submit_seq`, `present_seq`, `drawable_id` (pointer), and driver sequence numbers.
* **Deterministic Join Rules:** Traces are correlated through a strict 6-tuple cascade:
  $$\text{Frame Join} = (\text{app\_frame\_id}, \text{thread\_id}, \text{image\_index}, \text{acquire\_seq}, \text{submit\_seq}, \text{present\_seq})$$
* **Monotonicity Guarantees:** Every event contains an atomic monotonic process-wide sequence ID (`event_id`) and a monotonic timestamp (`timestamp_ns`), enabling unambiguous sorting across multi-threaded queue submit and presentation paths.

### 5.3 Presentation Observation & Display Timing
* **Public Interface Basis:** Uses public `CAMetalDrawable` `presentedHandler` callback.
* **Drop / Unpresented Classification:** Formally enforces that `presentedTime == 0.0` represents an unpresented or dropped drawable; it is strictly categorized as dropped and never counted as a successful display presentation.
* **Scheduling Preservation:** No presentation scheduling parameters, queue counts, or nextDrawable timeouts are altered. FIFO present mode, `maximumDrawableCount`, and `displaySyncEnabled` remain identical to MoltenVK defaults.
* **Display Timing Query:** `VK_GOOGLE_display_timing` querying is supported passively without setting desired presentation times.

### 5.4 Control Application (`native_control.mm`) Hardening
* **Fence Timeouts:** Replaced infinite `vkWaitForFences` with a 5.0-second timeout (`kFenceTimeoutNs = 5000000000ULL`), logging `wait_timeout` diagnostics on deadlock rather than hanging indefinitely.
* **Suboptimal Swapchain Recovery:** Captures `VK_SUBOPTIMAL_KHR` and logs `swapchain_suboptimal` informational events without crashing.
* **Layer Changes & Occlusion:** Added `windowDidChangeOcclusionState:` callback. Converted CAMetalLayer property modifications to informational `layer_state_changed` logs instead of triggering fatal assertion exits.

### 5.5 Offline Trace Validator (`trace_validator.py`)
Implemented `experiments/storage/trace_validator.py` and unit tests in `experiments/storage/test_trace_validator.py` (8/8 tests pass):
* Validates strict JSON syntax and required event fields.
* Enforces process-wide monotonic sequence ID order.
* Enforces monotonic timestamps per thread ID.
* Validates legal frame lifecycle transitions: `acquire` $\rightarrow$ `submit` $\rightarrow$ `present` $\rightarrow$ `drawable_presented`.
* Enforces `presentedTime == 0.0` drop classification.
* Detects ambiguous, missing, duplicate, or reordered joins and fails validation closed.
* Computes buffer drop and overflow accounting summaries.

---

## 6. Storage Gate 0 Verification & Final Attestation

### 6.1 Admission Integration
* `storage_policy.begin_experiment` accepts `expected_prefix_bytes = 0` when `authorized_runner_paths` is provided and includes `NativeRunLease` execution statements in the signed manifest.
* Descriptor-pinned audit in `g0c-launcher-independent-review.json` updated with the SHA-256 of `storage_policy.py` (`b70bca09308bdba30831c4c8022a4fd85ba54c2ddb4c82c1ec882686a9f5dd3e`).

### 6.2 Full Gate 0 Attestation Cycle
Executed the complete Gate 0 lifecycle:
1. **Preflight Execution:**
   * Command: `python3 experiments/storage/storage_gate0.py preflight`
   * Result: **`PREFLIGHT_PASS`**
   * Output: `prefixlease_smoke_status: PENDING`, `preferred_free_space_met: true`
2. **Fixed Storage Smoke Execution:**
   * Command: `python3 experiments/storage/smoke_prefix_lease.py`
   * Result: **`PASS`**
   * Verification: Clean temporary prefix creation, zero wine apps launched, 100% baseline restoration, 73 synthetic tests pass.
3. **Gate 0 Finalization:**
   * Command: `python3 experiments/storage/storage_gate0.py finalize --smoke-report experiments/storage/smoke-runs/step11d3-storage-policy-smoke-20261007-844f7f672db1489a/prefix-smoke-report.json`
   * Result: **`PASS`**
4. **Active Gate Blockers Audit:**
   * Query: `storage_policy._gate0_blockers()`
   * Result: **`[]` (Zero blockers)**

### 6.3 Current Storage Metrics
* **Attestation File:** `experiments/storage/storage-gate0-attestation.json`
* **Status:** `PASS`
* **Free Disk Space:** 81,865,023,488 bytes (~76.24 GiB $\ge$ 45.0 GiB Gate 0 minimum)
* **Scoped Project Storage:** 45,585,035,264 bytes (~42.45 GiB $\le$ 45.0 GiB Gate 0 budget)
* **Active Experimental Builds:** 0
* **Retained Experimental Builds:** 0
* **Preserved Experimental Builds:** 0
* **Allocated Wine Prefix Bytes:** **0 bytes**
* **Graphics Processes Executed:** **0**

---

## 7. Comprehensive Test Suite Results

All synthetic policy, retention, launcher, and validator test suites run and pass cleanly:

| Test Suite File | Test Scope | Tests Run | Result | Duration |
| :--- | :--- | :---: | :---: | :---: |
| [`test_storage_policy.py`](file:///Users/nima/Documents/Code/FG-Metal/experiments/storage/test_storage_policy.py) | PrefixLease, NativeRunLease, CAS, Admission AST | 73 | **PASS** | 2.31s |
| [`test_build_retention.py`](file:///Users/nima/Documents/Code/FG-Metal/experiments/storage/test_build_retention.py) | BuildLease, CAS artifacts, Native MoltenVK build | 98 | **PASS** | 1.74s |
| [`test_wine_launcher_policy.py`](file:///Users/nima/Documents/Code/FG-Metal/experiments/storage/test_wine_launcher_policy.py) | Wine loader canonicalization, DYLD search | 17 | **PASS** | 0.12s |
| [`test_g0c_checks.py`](file:///Users/nima/Documents/Code/FG-Metal/experiments/storage/test_g0c_checks.py) | Evidence binding, Gate 0 threshold checks | 15 | **PASS** | 0.08s |
| [`test_evidence_consolidation.py`](file:///Users/nima/Documents/Code/FG-Metal/experiments/storage/test_evidence_consolidation.py) | Zero-copy evidence consolidation transactions | 10 | **PASS** | 0.01s |
| [`test_trace_validator.py`](file:///Users/nima/Documents/Code/FG-Metal/experiments/storage/test_trace_validator.py) | Ring buffer traces, frame join, drop detection | 8 | **PASS** | 0.00s |
| **Complete Discovery (`test_*.py`)** | **Full repository storage & verification tests** | **221** | **PASS** | **4.25s** |

* Git diff check: `git diff --check` exited with code 0 (zero whitespace or formatting errors).

---

## 8. Conclusion and Final Determination

All infrastructure, contracts, supervision lifecycles, and trace validation tooling required to safely execute the native MoltenVK drawable-starvation baseline are in place, verified, and audited. Gate 0 is in a signed PASS state. No graphics baseline has run yet, and no presentation scheduling modifications have been introduced.

**Final Determination:**
```
NATIVE EXPERIMENT INFRASTRUCTURE READY — RERUN STEP 11D.3-A
```
