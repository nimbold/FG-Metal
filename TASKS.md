# Tasks

Task states: **Done** means the repository contains the work and it has passed the stated check; **In progress** means implementation is underway; **Open** means it remains future work. Verification claims belong with their evidence, not merely with a task marked done.

## Bootstrap step

- [x] **Done** — Create the initial project layout and CMake/Ninja targets using C++23 and the newest available macOS SDK/toolchain.
- [x] **Done** — Implement a small opaque GPU-resource public API with two ordered inputs and a returned output texture.
- [x] **Done** — Implement the Metal placeholder blend with GPU-only texture processing and explicit synchronization/lifetime behavior.
- [x] **Done** — Implement the `test-metal` host: create a Metal device and two input textures, submit them through the API, and display the returned third texture.
- [x] **Done** — Add focused API/core tests that do not require image readback in the live path.
- [x] **Done** — Complete `CONTRIBUTING.md`, `PROVENANCE.md`, and `LICENSE`; prohibit proprietary LSFG implementation material and track third-party/model/data/runtime rights separately.
- [x] **Done** — Review every initial diff for renderer coupling, hidden CPU copies, boundary compliance, and licensing/provenance issues.
- [x] **Done** — Build from a clean checkout; run all tests and the Metal host; record exact toolchain versions and evidence.
- [x] **Done** — Confirm the core has no Highball, Wine, LSFG, or renderer-specific dependency, and inspect the submission path for CPU image transfers.

### Bootstrap verification record

- Clean verification used a temporary Git source snapshot and clone outside the project directory; the cloned worktree was clean before configuration.
- Default macOS configure/build succeeded, CTest passed (1/1), and the native Metal test host launched and displayed `FrameGen Metal blend preview`.
- A separate core-only configure/build and CTest run also passed (1/1) with Metal disabled.
- Toolchain: macOS 27.0.1 (build 26A434), arm64, Apple Command Line Tools at `/Library/Developer/CommandLineTools`, macOS SDK 27.0, AppleClang 21.0.0.21000334, CMake 4.4.3, Ninja 1.13.2.
- Full Xcode, `xcodebuild`, the standalone `metal` compiler, and GPTK were not used. Runtime shader compilation through Metal succeeded. No neural backend or renderer adapter was started.

## Benchmark step

- [x] **Done** — Define a versioned high-rate corpus format, lower-rate source selection, ground-truth targets, arbitrary analytic-provider timestamps, and independently overlapping HUD/text/scene/occlusion masks.
- [x] **Done** — Implement image, temporal, flicker, sparse-pixel, and edge metrics with per-region, per-target, and strict critical-ROI pixel regression gates; keep PSNR diagnostic only.
- [x] **Done** — Add a rights-safe synthetic sequence, offline Metal placeholder runner, machine-readable JSON, human-readable Markdown, and compatible-run comparison.
- [x] **Done** — Capture a placeholder-backend baseline and document that its output and offline throughput are not interpolation-quality or game-performance claims.
- [ ] **Open** — Expand coverage for fast rotation, racing, menus, scene cuts, loading transitions, and richer subtitle cases; add real footage only when redistribution rights are documented.

### Benchmark verification record

- The Python analyzer and metric tests require only the standard library. The Metal runner is an optional macOS-only build target.
- The checked-in fixture is project-authored synthetic content. No copyrighted game capture, third-party metric code, model, or weights are included.
- Results measure a serial offline backend path and must not be represented as renderer-integrated FPS, presentation pacing, or observed dropped frames.

## Follow-on work (not authorized by completion of bootstrap)

- [ ] **Open** — Resolve the API and synchronization decisions listed in `DECISIONS.md`.
- [ ] **Open** — Identify a broader set of redistribution-cleared visual sequences and record their provenance and rights.
- [ ] **Open** — Evaluate algorithm/backend candidates without treating exploratory folders as approved dependencies.
- [ ] **Open** — Evaluate renderer adapters independently, starting only when authorized; use documented/public interfaces.
- [ ] **Open** — Consider a Highball consumer adapter only after the public API and core are stable.
