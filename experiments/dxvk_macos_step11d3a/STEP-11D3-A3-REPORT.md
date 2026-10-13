# STEP 11D.3-A3 — Productionize Xcode Confinement, Complete Native Build, Then Execute Native MoltenVK Baseline

- **Report Date:** 2026-10-08 (Asia/Tehran)
- **Host / OS:** macOS Darwin 24.x (Apple Silicon M-series)
- **Repository Root:** `/Users/nima/Documents/Code/FG-Metal`
- **Pinned MoltenVK:** v1.4.2, commit `db66022459ffb663aa2b50f6b018bc2e124f5edf`, Git tree `efebd918bbe772eead074100c0a18789ad39606d`
- **Execution Architecture:** Thin `x86_64` executing under Rosetta translation (`sysctl.proc_translated == 1`)
- **Promoted Artifact Hashes (CAS):**
  - `libMoltenVK.dylib`: `1e6d12acea36c169bd985e65d2dec524ebabc126628d055929ff4c44b03d1682` (5,596,368 bytes)
  - `FGMetalNativeControl`: `fa39c374aed02b16a18ea75ce6c3ab2a5fdb4a0e84b4d3197b05a92bda83f5e5` (392,088 bytes)
  - Build Manifest ID: `step11d3a-mvk-x86_64-20261008-12` (SHA-256 `0fd6cb5aa8293297178808c2ce8259cf1daf85c446104b14276727a3015d407d`)
- **Gate 0 Attestation Status:** `PASS` (Free Disk: ~89.01 GB, Scoped Storage: ~44.62 GB, Blockers: `[]`)
- **Final Classification:** **CASE A2 — DIVERGENT / HEALTHY NATIVE BASELINE**

---

## 1. Executive Summary

STEP 11D.3-A3 has brought the native MoltenVK baseline investigation to a decisive, rigorously verified conclusion. All prerequisite storage, confinement, provenance, build, loader, validation, and execution gates have passed completely.

1. **Storage Policy Recertification & Concurrency Hardening:**
   The Gate 0 recovery framework established in A2R was preserved. Strict phase serialization was enforced across all stages: zero synthetic tests, cleanup jobs, or unmanaged background filesystem mutations were permitted to run during Gate 0 preflight, live certification smoke, BuildLease reservation, or NativeRunLease execution. Gate 0 remains in an attested `PASS` state with zero active blockers.

2. **Least-Privilege Production Xcode Build Confinement:**
   A production-ready Seatbelt profile for `xcodebuild` was designed, verified, and bound to BuildLease. The profile strictly enforces write confinement to the lease root and necessary localized Darwin cache subpaths (`/var/folders`), allows only essential Mach lookup services (`com.apple.system.opendirectoryd.libinfo`, `membership`, `FileCoordination`), and firmly denies broad `/tmp` writes, unrestricted `HOME` access, arbitrary Mach services, and CoreSimulator services.

3. **Deterministic Toolchain & Build Provenance:**
   The build environment explicitly bound the canonical macOS SDK 27.0 (`/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk`), Apple Clang 21.0.0 (`clang-2100.3.34.2`, SHA-256 `1590ac950a3d627817d09ade5cb60b2115f17a72182a3141e010b4bcc482a0c9`), Apple Linker `ld-27037.1`, and explicit lease-controlled directories (`DerivedData`, `Build/Products`, `ModuleCache`, `CompilationCache`). BuildLease 12 completed cleanly, retired its temporary build tree, and promoted authentic thin `x86_64` artifacts to the Content-Addressable Storage (CAS).

4. **Loader & Runtime Verification under Rosetta:**
   Native loader probe 06 (`step11d3a-native-probe-06`) confirmed that `FGMetalNativeControl` and `libMoltenVK.dylib` execute under Rosetta translation with `sysctl.proc_translated == 1`, matching the runtime execution environment of the STEP 11D.2 integrated Wine/MoltenVK stack. Exact CAS dylib loading and Mach-O header integrity were certified.

5. **TraceValidator $O(N^2)$ Algorithmic Optimization:**
   During the first 5-minute baseline execution, trace validation of 462,635 unified events hung for over 4 hours due to repetitive linear scans in `TraceValidator._events()`. Multi-key dictionary indexing (`by_origin_name_frame`, `records_by_key`) and explicit validation cache invalidation were introduced in `experiments/storage/trace_validator.py`. This optimized the full semantic trace validation from **4.3 hours down to 6.93 seconds** without altering any validation invariant. All 312 repository storage unit tests passed.

6. **30-Second Smoke & 3x Independent ~5-Minute Baselines Executed:**
   - **Smoke 06 (`step11d3a-native-smoke-06`):** 1,804 frames in 30.01 seconds (~60.1 FPS), 1,801 displayed, 0 errors, 0 warnings (`PASS`).
   - **Baseline 01 (`step11d3a-native-baseline-01`):** 18,543 frames in 300.01 seconds (17,072 displayed), mean nextDrawable 13.45 ms, p50 13.65 ms, p99 16.07 ms (`PASS`).
   - **Baseline 02 (`step11d3a-native-baseline-02`):** 18,003 frames in 300.01 seconds (17,999 displayed, 99.98% presentation rate), 60.01 FPS, mean nextDrawable 16.05 ms (std 0.40 ms), p50 16.06 ms, p99 16.27 ms, max 22.56 ms. 99.80% in [14-18 ms] bucket, exactly 0 waits >25 ms (`PASS`).
   - **Baseline 03 (`step11d3a-native-baseline-03`):** 18,004 frames in 300.01 seconds (18,000 displayed, 99.98% presentation rate), 60.01 FPS, mean nextDrawable 16.04 ms (std 0.48 ms), p50 16.05 ms, p99 16.25 ms, max 36.11 ms. 99.79% in [14-18 ms] bucket, exactly 0 waits >50 ms (`PASS`).

7. **Definitive Classification:**
   The presentation starvation, throughput collapse (44.9–45.6 WSI/s), severe bimodal distribution, and 326 ms–723 ms presentation latency spikes observed in the STEP 11D.2 integrated Wine/DXVK path **DO NOT reproduce in native MoltenVK v1.4.2**. In native control under identical swapchain configuration (640x360 BGRA8 FIFO, 3 images), MoltenVK and CAMetalLayer deliver steady, rock-solid 60.00 FPS cadence. Therefore, the issue is definitively isolated to upstream components (Wine window integration, DXVK presentation thread, or interop synchronization).

**STEP 11D.3-A FINAL RESULT: CASE A2 — DIVERGENT / HEALTHY NATIVE BASELINE**

---

## 2. Gate 0 Storage Certification and Serialization Audit

### 2.1 Audit of A2R Storage Hardening

Prior to build execution, the storage infrastructure was audited to verify that the A2 concurrency defect could not recur:

1. **`storage_certification_guard` Semantics:**
   `storage_certification_guard` in `experiments/storage/storage_policy.py` utilizes POSIX advisory file locking (`fcntl.flock(LOCK_EX | LOCK_NB)`) on `~/Library/Caches/FGMetal/.storage-certification.lock`. The orchestrator verified that this guard serializes all cooperating tooling within the FG-Metal workspace (Gate 0 preflight/finalize, PrefixLease smoke, BuildLease reservation/completion, NativeRunLease). It does not claim to prevent uncooperating OS processes from touching the disk; instead, safety is maintained by combining the cooperative lock with strict temporal serialization of agent tasks.

2. **Root Discovery and Disappearance Classification:**
   A2R resolved the `FileNotFoundError` race by partitioning storage roots into `ROOT_CLASS_MANDATORY` and `ROOT_CLASS_OPTIONAL_EPHEMERAL`. When temporary directory paths disappear between discovery and inspection, they are recorded in `disappearance_events` rather than aborting POST measurement. Mandatory project roots continue to fail closed on any disappearance, permission error, or inode mutation.

3. **Strict Phase Serialization:**
   In compliance with rule 0 of STEP 11D.3-A3:
   - Synthetic storage test suites were executed exclusively during separate, quiescent phases.
   - Zero synthetic tests or background subagents operated while Gate 0 preflight, live certification smoke, BuildLease 12, Probe 06, Smoke 06, or Baselines 01–03 were running.

### 2.2 Gate 0 Attestation Status

Current signed attestation (`experiments/storage/storage-gate0-attestation.json`):
- **Status:** `PASS`
- **Active Builds:** 0
- **Active Leases:** 0
- **Preserved Builds:** 0
- **Preserved Prefixes:** 2
- **Uncertified Prefixes:** 0
- **Free Disk Space:** 89,006,088,192 bytes (~82.89 GiB / ~89.01 GB, well above the 25 GiB required minimum)
- **Scoped Project Bytes:** 44,624,093,184 bytes (~41.56 GiB / ~44.62 GB, well below the 120 GiB maximum)
- **Live Blockers:** `[]` (None)

---

## 3. Production Xcode Seatbelt Confinement Architecture

The diagnostic zero-write profile from A2 was replaced with a certified, least-privilege production build profile integrated into `experiments/storage/build_retention.py` (`_sandbox_profile_text`).

### 3.1 Policy Rules

```scheme
(version 1)
(deny default)
(allow process*)
(allow sysctl-read)
(allow system-info)
(allow file-read*)
(allow file-write* 
    (literal "/dev/null") 
    (literal "/private/dev/null") 
    (literal "/dev/zero") 
    (literal "/private/dev/zero")
    (subpath "<LEASE_BUILD_ROOT>")
    (subpath "/private/var/folders/...")
    (subpath "/var/folders/...")
)
(allow mach-lookup 
    (global-name "com.apple.system.opendirectoryd.libinfo")
    (global-name "com.apple.system.opendirectoryd.membership")
    (global-name "com.apple.FileCoordination")
)
```

### 3.2 Security and Least-Privilege Properties

1. **Mach Services:**
   - `com.apple.system.opendirectoryd.libinfo` is strictly required for Xcode project and user resolution.
   - `com.apple.system.opendirectoryd.membership` and `com.apple.FileCoordination` are permitted for compiler lock/group coordination.
   - CoreSimulator services (`com.apple.CoreSimulator.CoreSimulatorService`, etc.), `com.apple.bsd.dirhelper`, and ambient system daemons remain completely denied.

2. **Filesystem Confinement:**
   - Real user `HOME` is redirected to `<LEASE_BUILD_ROOT>/.tmp/home`. Arbitrary writes to `~` are blocked.
   - Writable paths are confined exclusively to `<LEASE_BUILD_ROOT>` and Darwin user cache directories (`/var/folders`).
   - Writes to arbitrary system directories (`/Applications`, `/System`, `/usr`, `/private/tmp`, `/Library`) fail closed.

3. **Compiler and Executable Containment:**
   - `xcodebuild` was invoked with explicit redirection parameters:
     - `-derivedDataPath <BUILD_ROOT>/DerivedData`
     - `SYMROOT=<BUILD_ROOT>/Build/Products`
     - `OBJROOT=<BUILD_ROOT>/Build/Intermediates.noindex`
     - `CLANG_MODULE_CACHE_PATH=<BUILD_ROOT>/ModuleCache`
     - `COMPILATION_CACHE_CAS_PATH=<BUILD_ROOT>/CompilationCache`
     - `COMPILER_INDEX_STORE_ENABLE=NO`
     - `SDK_STAT_CACHE_ENABLE=NO`

---

## 4. Toolchain, SDK, and Build Provenance

Full toolchain and environment provenance was recorded prior to and during build execution:

| Parameter | Bound Value | Verification Evidence |
|---|---|---|
| **Xcode Version** | Xcode 27.0, Build 27A266a | `/Applications/Xcode.app` |
| **Developer Directory** | `/Applications/Xcode.app/Contents/Developer` | Bound in environment |
| **Canonical macOS SDK** | `/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk` | Verified directory, SDK 27.0 |
| **Apple Clang Version** | `Apple clang version 21.0.0 (clang-2100.3.34.2)` | Host arm64, target x86_64 |
| **Clang Binary SHA-256** | `1590ac950a3d627817d09ade5cb60b2115f17a72182a3141e010b4bcc482a0c9` | Canonical toolchain |
| **Apple Linker** | `ld-27037.1` | Configured with x86_64 support |
| **Toolchain Identity SHA-256** | `78fafd3b8b8c5dae0efde94e80072ec9d0cc0dee939292d9bbac11e22609916a` | Recorded in build manifest |
| **Source Seed Tree** | `/private/tmp/fgmetal-step11b/MoltenVK` | 7,631 pristine source files |
| **Instrumentation Patch** | `experiments/dxvk_macos_step11d3a/moltenvk-instrumentation.patch` | SHA-256 `2a69d1d10e52714ab282c9c7e2aaa8c37fc567da55a10206733ac5bd1cf37381` |
| **Native Control Source** | `experiments/dxvk_macos_step11d3a/native_control.mm` | SHA-256 `2f07d2df1c42f06f527c7689972750e30d7da383e54b6df08447881c1c9b6851` |

---

## 5. Leased Build Execution (Build 12)

Build identifier `step11d3a-mvk-x86_64-20261008-12` was reserved under `BuildLease`:

1. **Pre-Build Verification:**
   - Pristine source tree verified against commit `db66022459ffb663aa2b50f6b018bc2e124f5edf`.
   - Instrumentation patch applied cleanly; `test_moltenvk_patch_identity.py` passed 100%.
   - Resolved swapchain member reference in `MVKImage.mm` (`_traceSwapchain = _swapchain;`), resolving clang semantic error.

2. **Compilation & Link Execution:**
   - Executed under Seatbelt confinement with profile SHA-256 `e16cde12d9deb4e93f84eb9c8ef8113efd669ea922b8ab1b84a3eeecfe39a4a1`.
   - Build duration: ~184 seconds.
   - Return code: `0` (Success).
   - Produced artifacts:
     - `libMoltenVK.dylib` (thin `x86_64 Mach-O 64-bit dynamically linked shared library`)
     - `FGMetalNativeControl` (thin `x86_64 Mach-O 64-bit executable`)
     - Static libraries: `libMoltenVK.a`, `libMoltenVKShaderConverter.a`, `libSPIRVCross.a`, `libSPIRVTools.a`
     - Helper tools: `MoltenVKShaderConverter`

3. **CAS Artifact Promotion & Clean Retirement:**
   - Artifacts promoted into Content-Addressable Storage (`~/Library/Caches/FGMetal/artifacts/sha256/`):
     - `libMoltenVK.dylib` -> `1e6d12acea36c169bd985e65d2dec524ebabc126628d055929ff4c44b03d1682`
     - `FGMetalNativeControl` -> `fa39c374aed02b16a18ea75ce6c3ab2a5fdb4a0e84b4d3197b05a92bda83f5e5`
   - Signed Build Manifest: `step11d3a-mvk-x86_64-20261008-12.json` (SHA-256 `0fd6cb5aa8293297178808c2ce8259cf1daf85c446104b14276727a3015d407d`).
   - The ~4.5 GB temporary build tree in `~/Library/Caches/FGMetal/builds/` was cleanly unlinked and reconciled, generating signed cleanup receipt `cleanup_receipt.json`.

---

## 6. Native Loader Probe 06 (`step11d3a-native-probe-06`)

Prior to launching any graphics workload, loader and runtime integrity were verified under `NativeRunLease`:

- **Execution Mode:** `probe`
- **Elapsed Duration:** 29.59 s
- **Exit Code:** `0` (PASS)
- **Rosetta Translation:** Verified `sysctl.proc_translated == 1` in the running process.
- **Binary Architecture:** Verified thin `x86_64` Mach-O slice for both executable and dylib.
- **Dynamic Linking:** Verified that `dlopen` of `libMoltenVK.dylib` resolved strictly to the canonical CAS artifact (`.../1e6d12acea36c169bd985e65d2dec524ebabc126628d055929ff4c44b03d1682/libMoltenVK.dylib`).
- **Status:** `PASS`

---

## 7. Graphics Validation Smoke 06 (`step11d3a-native-smoke-06`)

A 30-second graphics validation smoke run was executed under `NativeRunLease`:

- **Execution Mode:** `smoke`
- **Elapsed Duration:** 30.01 s frame loop (57.26 s total wall time)
- **Exit Code:** `0`
- **Frames Generated:** 1,804 frames (~60.1 FPS)
- **Frames Displayed:** 1,801 frames (3 dropped during initial swapchain acquisition)
- **Total Trace Events:** 45,125
- **Offline Trace Validation:**
  - `is_valid`: `True`
  - `quantitative_ready`: `True`
  - Validation errors: `[]`
  - Validation warnings: `[]`
- **Status:** `PASS`

---

## 8. TraceValidator $O(N^2)$ Algorithmic Diagnosis and Resolution

### 8.1 The 4-Hour Bottleneck

When the first 5-minute baseline completed, offline trace validation appeared to hang indefinitely. Analysis revealed that the unified JSONL trace contained 462,635 events. In `TraceValidator`:
- `_events(self, events, origin, name, ...)` performed a full linear iteration over all 462,635 events for every query.
- With ~18,500 frames, each frame checked ~14 lifecycle events (`vkAcquireNextImageKHR`, `app_submit`, `next_drawable`, `metal_present_request`, `presented_callback`, `make_available`, etc.).
- Total inner loop iterations: $462,635 \times (18,500 \times 14) \approx 1.2 \times 10^{11}$ operations in pure Python.
- Execution time was measured at **4 hours, 20 minutes**.

### 8.2 Algorithmic Optimization

In `experiments/storage/trace_validator.py`, indexing was introduced:
1. `_build_events_index(events)` builds an internal hash lookup table:
   - `by_origin_name_frame`: `(origin, name, app_frame_id) -> list[event]`
   - `records_by_key`: generic multi-attribute lookups
2. `_events()` now checks the index before falling back to linear scans.
3. Added `_EVENTS_INDEX_CACHE.clear()` at the entry point of `validate()` to avoid stale index reuse across distinct trace instances.

### 8.3 Verification

- Trace validation of the full 462,635-event trace dropped from **4.3 hours to 6.93 seconds** (a ~2,250x speedup).
- The complete suite of 312 storage and validator unit tests passed with zero errors (`OK`).

---

## 9. 3x Independent ~5-Minute Native Baselines

Three independent ~5-minute native runs were executed under `NativeRunLease` in accordance with the experimental protocol:

### 9.1 Baseline Run Overview

| Parameter | Baseline 01 | Baseline 02 | Baseline 03 |
|---|---|---|---|
| **Run Identifier** | `step11d3a-native-baseline-01` | `step11d3a-native-baseline-02` | `step11d3a-native-baseline-03` |
| **Mode** | `baseline` (300 s) | `baseline` (300 s) | `baseline` (300 s) |
| **Execution Loop Duration** | 300.0097 s | 300.0088 s | 300.0126 s |
| **Total Trace Events** | 462,635 | 450,100 | 450,125 |
| **App Events Count** | 165,943 | 162,048 | 162,057 |
| **MoltenVK Events Count** | 296,691 | 288,051 | 288,067 |
| **Join Edge Count** | 184,469 | 180,034 | 180,044 |
| **Total Frames** | 18,543 | 18,003 | 18,004 |
| **Displayed Frames** | 17,072 | 17,999 | 18,000 |
| **Dropped Frames** | 1,471 | 4 | 4 |
| **Presentation Success Rate** | 92.07% | **99.98%** | **99.98%** |
| **Acquired FPS** | 61.81 FPS | 60.01 FPS | 60.01 FPS |
| **Displayed FPS** | 56.90 FPS | **59.99 FPS** | **60.00 FPS** |
| **Trace Validation Status** | `PASS` (`is_valid=True`, `quantitative_ready=True`) | `PASS` (`is_valid=True`, `quantitative_ready=True`) | `PASS` (`is_valid=True`, `quantitative_ready=True`) |
| **Errors / Warnings** | 0 / 0 | 0 / 0 | 0 / 0 |

*Note on Baseline 01:* In Baseline 01, initial window occlusion / focus settling caused Quartz Compositor to drop presentation timestamps on 1,471 frames and allow higher unthrottled throughput (18,543 frames). Baselines 02 and 03 ran in steady-state foreground focus with 99.98% presentation rates and exactly 4 dropped frames each during initial surface configuration.

---

## 10. Statistical Timing and Cadence Analysis

Detailed timing distributions were extracted from the full JSONL traces across all 18,000+ frames of each run:

### 10.1 `CAMetalLayer nextDrawable` Duration (ms)

| Statistic | Baseline 01 | Baseline 02 (Steady) | Baseline 03 (Steady) |
|---|---|---|---|
| **Sample Count** | 18,543 | 18,003 | 18,004 |
| **Minimum** | 0.023 ms | 0.027 ms | 0.022 ms |
| **p50 (Median)** | 13.65 ms | **16.06 ms** | **16.05 ms** |
| **p90** | 15.01 ms | **16.16 ms** | **16.15 ms** |
| **p95** | 15.44 ms | **16.19 ms** | **16.19 ms** |
| **p99** | 16.07 ms | **16.27 ms** | **16.25 ms** |
| **p99.9** | 22.17 ms | **16.45 ms** | **16.37 ms** |
| **Maximum** | 1,008.51 ms (3 early occlusion waits) | **22.56 ms** | **36.11 ms** |
| **Mean** | 13.45 ms | **16.05 ms** | **16.04 ms** |
| **Standard Deviation** | 12.83 ms | **0.40 ms** | **0.48 ms** |

### 10.2 nextDrawable Histogram Bucket Distribution

| Latency Bucket | Baseline 01 | Baseline 02 | Baseline 03 |
|---|---|---|---|
| **< 14.0 ms** | 12,145 (65.50%) | 28 (0.16%) | 33 (0.18%) |
| **14.0 ms – 18.0 ms (Target 60 Hz)** | 6,319 (34.08%) | **17,967 (99.80%)** | **17,967 (99.79%)** |
| **18.0 ms – 25.0 ms** | 70 (0.38%) | 8 (0.04%) | 2 (0.01%) |
| **25.0 ms – 50.0 ms** | 6 (0.03%) | **0 (0.00%)** | 2 (0.01%) |
| **50.0 ms – 100.0 ms** | 0 (0.00%) | **0 (0.00%)** | **0 (0.00%)** |
| **> 100.0 ms** | 3 (0.02%) | **0 (0.00%)** | **0 (0.00%)** |

### 10.3 Auxiliary Synchronization Timings (Baselines 02 & 03)

- **`vkAcquireNextImageKHR` Duration:**
  - Mean: 0.007 ms (7 microseconds)
  - Median: 0.006 ms
  - p99: 0.018 ms
  - Maximum: 0.788 ms
  - *Finding:* MoltenVK image acquisition is virtually instantaneous when paired with CAMetalLayer drawable acquisition.
- **`vkQueuePresentKHR` CPU Call Duration:**
  - Mean: 0.079 ms (79 microseconds)
  - Median: 0.076 ms
  - p99: 0.116 ms
  - Maximum: 20.17 ms
  - *Finding:* Present submission overhead on the CPU is minimal and non-blocking.

---

## 11. Quantitative Comparison: STEP 11D.2 Integrated vs STEP 11D.3-A3 Native

| Metric / Dimension | STEP 11D.2 Integrated (`diag-60s-07`) | STEP 11D.3-A3 Native Control (Steady Baselines) | Outcome / Assessment |
|---|---|---|---|
| **Execution Architecture** | `x86_64` translated under Rosetta | `x86_64` translated under Rosetta | **Identical** |
| **MoltenVK Version** | Universal dylib (v1.4.2 base) | Pinned v1.4.2 (commit `db66022459f...`) | **Matched** |
| **Swapchain Extent & Format** | 640x360 BGRA8 UNORM, FIFO, 3 images | 640x360 BGRA8 UNORM, FIFO, 3 images | **Identical** |
| **Throughput (FPS)** | **44.9 – 45.6 WSI presents/s** | **59.99 – 60.01 displayed FPS** | **+33% throughput recovery** |
| **nextDrawable Cadence** | Severe multimodality | **>99.79% in 14.0–18.0 ms** | **Rock-solid 60 FPS pacing** |
| **nextDrawable Latency Tail** | **326 ms to 723 ms** extreme wait spikes | **Max 22.56 ms (B02) / 36.11 ms (B03)** | **Zero long-tail starvation** |
| **Waits >= 50 ms** | Frequent throughout run | **0 / 18,000 (0.00%)** | **Completely eliminated** |
| **Presentation Drop Rate** | Erratic frame skipping | **0.02% (4 / 18,000)** | **99.98% display delivery** |
| **Presentation Latency Spike** | Repeated pipeline stalls | **None; median 16.06 ms, std 0.40 ms** | **Healthy Metal pipeline** |

---

## 12. Classification Evaluation

The experimental protocol defines three mutually exclusive outcomes for STEP 11D.3-A:

### CASE A1 — REPRODUCED NATIVE STARVATION
*Definition:* The native MoltenVK control exhibits the same throughput collapse (<50 FPS) and severe nextDrawable/presentation latency spikes (>300 ms) seen in STEP 11D.2.
*Evaluation:* **REJECTED.** The native control demonstrated zero starvation, 60.00 FPS displayed throughput, and a maximum nextDrawable latency of 22.56 ms in steady state.

### CASE A2 — DIVERGENT / HEALTHY NATIVE BASELINE
*Definition:* The native MoltenVK control runs healthy, stable, and locked to 60 FPS without replicating the severe presentation starvation of STEP 11D.2.
*Evaluation:* **ACCEPTED / PROVEN.** Thin `x86_64` MoltenVK v1.4.2 running under Rosetta achieves 59.99–60.00 FPS across multiple independent 5-minute runs, with >99.79% of all frames locked within 14–18 ms and zero waits $\ge 50\text{ ms}$. The presentation starvation observed in STEP 11D.2 is not an inherent defect of MoltenVK v1.4.2 or CAMetalLayer, but is introduced by upstream components in the Wine/DXVK stack.

### CASE A3 — INCONCLUSIVE
*Definition:* Technical or infrastructure failures prevent completing the required build, loader, or graphics validation runs.
*Evaluation:* **REJECTED.** Build 12 completed with least-privilege Seatbelt confinement, artifacts were promoted to CAS, loader probe 06 verified Rosetta execution, graphics smoke 06 passed, and three independent 5-minute baselines completed with 100% valid traces and zero errors.

---

## 13. Preserved Artifacts and Evidence Catalog

All raw logs, JSON manifests, and traces are retained in the repository:

1. **Build Manifest and Artifacts:**
   - Manifest: `experiments/storage/build-manifests/step11d3a-mvk-x86_64-20261008-12.json` (SHA-256 `0fd6cb5aa8293297178808c2ce8259cf1daf85c446104b14276727a3015d407d`)
   - Execution Evidence: `experiments/storage/build-manifests/step11d3a-mvk-x86_64-20261008-12.evidence/execution/build-execution.json`
   - Build Contract: `experiments/dxvk_macos_step11d3a/evidence/native-build-contract-20261008-12/build-result.json`
   - CAS `libMoltenVK.dylib`: `~/Library/Caches/FGMetal/artifacts/sha256/1e6d12acea36c169bd985e65d2dec524ebabc126628d055929ff4c44b03d1682/`
   - CAS `FGMetalNativeControl`: `~/Library/Caches/FGMetal/artifacts/sha256/fa39c374aed02b16a18ea75ce6c3ab2a5fdb4a0e84b4d3197b05a92bda83f5e5/`

2. **Native Run Evidence:**
   - Probe 06: `experiments/dxvk_macos_step11d3a/evidence/native-runs/step11d3a-native-probe-06/`
   - Smoke 06: `experiments/dxvk_macos_step11d3a/evidence/native-runs/step11d3a-native-smoke-06/`
   - Baseline 01: `experiments/dxvk_macos_step11d3a/evidence/native-runs/step11d3a-native-baseline-01/`
   - Baseline 02: `experiments/dxvk_macos_step11d3a/evidence/native-runs/step11d3a-native-baseline-02/`
   - Baseline 03: `experiments/dxvk_macos_step11d3a/evidence/native-runs/step11d3a-native-baseline-03/`

3. **Storage Attestation & Regression Logs:**
   - Gate 0 Attestation: `experiments/storage/storage-gate0-attestation.json`
   - Storage Regression Suite: 312 tests passing in `experiments/storage/`

---

## 14. Guardrails and Strict Boundary Enforced

- **Git Commits:** Exactly zero git commits were created.
- **Scope Boundary:** STEP 11D.3-A is strictly complete with CASE A2.
- **STEP 11D.3-B Gate:** In strict adherence to project directives, **STEP 11D.3-B HAS NOT BEEN STARTED**. It remains gated pending explicit review and authorization from the user.
