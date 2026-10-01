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
- [ ] **Open** — Expand corpus coverage for racing, scene-cut and loading transitions, and richer subtitle cases; add real footage only when redistribution rights are documented. Fast camera rotation and moving menus are now in the synthetic fixture.

### Benchmark verification record

- The Python analyzer and metric tests require only the standard library. The Metal runner is an optional macOS-only build target.
- The checked-in fixture is project-authored synthetic content. No copyrighted game capture, third-party metric code, model, or weights are included.
- Results measure a serial offline backend path and must not be represented as renderer-integrated FPS, presentation pacing, or observed dropped frames.

## Temporal quality step

- [x] **Done** — Add an opt-in, model-independent post-interpolation confidence controller with endpoint/source fallback policies, temporal history isolation, GPU scene-cut detection, debug views, and reset handling.
- [x] **Done** — Benchmark raw RIFE, automatic HUD, each temporal policy, and each HUD-plus-temporal combination on the fast-rotation synthetic fixture; inspect the generated contact sheet and document both gains and regressions.
- [x] **Done** — Record the limited measured improvements and keep the controller disabled by default because strict comparisons still report regressions.

### Temporal quality verification record

- See [temporal quality results](docs/TEMPORAL_QUALITY.md) and the linked raw JSON/Markdown run and comparison artifacts.
- Apple M3, Practical-RIFE v4.26 QUALITY, 128×72; five targets, three warmups and 100 measured iterations per target for each of eight configurations. All six strict policy comparisons report `REGRESSION`.
- Endpoint fallback reduced HUD-region flicker p95 23.3% and weapon-sight flicker p95 16.6% versus raw RIFE, but whole-frame flicker p95 rose 3.7% and minimum thin-edge recall fell 0.7055→0.6090. HUD-plus-temporal was worse than HUD alone for HUD flicker. These results justify keeping the feature experimental and off by default, not a general quality claim.
- GPU same-histogram cut, no-false-positive rotation, real-frame cut/reset fallback, duplicate-target stability, and stale-request isolation were probed. Final verification: CMake build succeeded; CTest passed 4/4, including Metal C API smoke on Apple M3; benchmark metric tests passed 43/43.

## Follow-on work (not authorized by completion of bootstrap)

- [ ] **Open** — Resolve the API and synchronization decisions listed in `DECISIONS.md`.
- [ ] **Open** — Identify a broader set of redistribution-cleared visual sequences and record their provenance and rights.
- [ ] **Open** — Evaluate algorithm/backend candidates without treating exploratory folders as approved dependencies.
- [ ] **Open** — Evaluate renderer adapters independently, starting only when authorized; use documented/public interfaces.
- [ ] **Open** — Consider a Highball consumer adapter only after the public API and core are stable.
