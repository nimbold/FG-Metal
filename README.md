# FG-Metal

**Renderer-integrated frame generation for macOS — GPU-native, evidence-gated, no screen capture.**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Apple%20silicon-lightgrey.svg)](#requirements)
[![C++](https://img.shields.io/badge/C%2B%2B-23-00599C.svg)](CMakeLists.txt)
[![Status](https://img.shields.io/badge/status-experimental-orange.svg)](#status-at-a-glance)

FG-Metal is a frame-generation research project built around one rule: **no claim survives without a measurement that could have falsified it.** It pairs a renderer-independent GPU-texture library with a live in-process integration that has generated and displayed real interpolated frames inside a running game on Apple silicon.

Every headline number in this repository traces to a dated report and a content-hashed evidence directory. Results that failed are published with the same prominence as results that passed.

> **Not a product.** There is no supported adapter for an unmodified game, the C ABI is a draft, and continuous 2× frame generation is **not** achieved. See [Status at a glance](#status-at-a-glance) for exactly what is and is not established.

---

## Contents

- [Status at a glance](#status-at-a-glance)
- [What has actually been demonstrated](#what-has-actually-been-demonstrated)
- [The current blocker](#the-current-blocker)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Build and test](#build-and-test)
- [Repository layout](#repository-layout)
- [Prior renderer investigations](#prior-renderer-investigations)
- [Documentation](#documentation)
- [Project boundaries](#project-boundaries)
- [Contributing](#contributing)
- [License](#license)

---

## Status at a glance

| Capability | State | Evidence |
| --- | --- | --- |
| Core library, GPU-texture contract, draft C ABI | **Implemented, tested** | `tests/`, [`include/framegen/api.h`](include/framegen/api.h) |
| Metal backend (Practical-RIFE v4.26) | **Offline / synthetic only** | [Temporal quality](docs/TEMPORAL_QUALITY.md) |
| Presentation scheduler | **Simulated only** | [`framegen-pacing-host`](tests/pacing_host.cpp) |
| Real in-world A→M→B on the game's own layer | **Validated** (one pair) | [R114](FG-METAL-R114-INPROCESS-TARGET-LATCH-TITLE-DISPLAY-REPORT.md) |
| Re-armable continuous A→M→B | **Validated** (60 tuples) | [R115](FG-METAL-R115-REARMABLE-CONTINUOUS-TITLE-REPORT.md) |
| Async GPU fast gate, non-blocking M-then-B | **Validated** (51/51 ordered) | [R116](FG-METAL-R116-CRITICAL-PATH-PACING-REPORT.md) |
| Generation throughput ≥ source rate | **Not validated** | [R118](FG-METAL-R118-LIVE-GENERATION-THROUGHPUT-REPORT.md), [R119](FG-METAL-R119-LIVE-CONTENTION-AND-EVIDENCE-ISOLATION-REPORT.md) |
| Continuous 30→60 | **Not validated** | [R120](FG-METAL-R120-CONTINUOUS-30-TO-60-TITLE-REPORT.md), [R121](FG-METAL-R121-PRESENTATION-OCCUPANCY-REPORT.md) |
| Continuous 60→120 | **Not attempted** | [R117](FG-METAL-R117-LOOKAHEAD-2X-FEASIBILITY-REPORT.md) |
| Real title depth and motion vectors | **Not validated** (constant-depth prototype) | [R105](FG-METAL-R105-TITLE-INPUTS-CONSOLIDATED-REPORT.md), [R106](FG-METAL-R106-TITLE-DERIVED-MOTION-CONSOLIDATED-REPORT.md) |
| Supported adapter for an unmodified game | **None exists** | [TASKS.md](TASKS.md) |

---

## What has actually been demonstrated

The project has two tracks that are deliberately kept separate.

**1. A reusable library.** `src/`, `include/`, `backends/`, and `adapters/` build a renderer-independent frame-generation core: opaque GPU resource import, asynchronous interpolation tickets, presentation feedback, history invalidation, HUD preservation modes, and a model-independent pacing scheduler. It builds clean and passes its full test suite on Apple silicon. Its quality numbers come from a project-authored synthetic corpus, and they are not in-game quality claims.

**2. A live in-process integration.** [`experiments/metalfx_frame_interpolator_R93/`](experiments/metalfx_frame_interpolator_R93/) is where the product question is actually being answered. It hooks a real title running under Highball/D3DMetal, captures source frames on the GPU, derives a motion field, interpolates with MetalFX, and presents the generated frame through the game's own `CAMetalLayer`.

What that integration has proven, in a real game process:

- A real in-world frame pair was captured, interpolated, and displayed as **A → M → B** with `reachedDisplay=1`, and the game remained stable for 9,864 further frames ([R114](FG-METAL-R114-INPROCESS-TARGET-LATCH-TITLE-DISPLAY-REPORT.md)).
- The lifecycle is **re-armable**: 60 accepted tuples across two processes with zero source leaks, zero stale callbacks, zero duplicate dispatches ([R115](FG-METAL-R115-REARMABLE-CONTINUOUS-TITLE-REPORT.md)).
- The critical-path gate moved from **31.28 ms to 0.128 ms** of CPU time, and M-then-B scheduling held ordering and distinctness in 51 of 51 in-game tuples ([R116](FG-METAL-R116-CRITICAL-PATH-PACING-REPORT.md)).
- A genuine **30 fps source cadence** was established by back-pressure, not frame thinning: 738 of 744 presents were actually blocked by the limiter ([R120](FG-METAL-R120-CONTINUOUS-30-TO-60-TITLE-REPORT.md)).

What it has not proven: that the output is a continuous 60 fps stream. It is not. See below.

---

## The current blocker

R121 decomposed the interpolation presentation path end to end. The result is not where it was expected to be.

| Quantity | Measured | Budget |
| --- | --- | --- |
| Present hook → M dispatched to WSI (median) | **41.392 ms** | 33.333 ms |
| of which: wait for the SOURCE frame to retire on the GPU | **23.073 ms** (59.7 %) | — |
| of which: frozen estimator + MetalFX | 15.799 ms (38.2 %) | — |
| of which: all fifteen remaining links combined | 0.867 ms (2.1 %) | — |
| Internal `nextDrawable` acquisition (median) | 0.017 ms | — |
| Observed output rate | **27.11 fps** | 60 fps |
| Source rate in the same window | 29.14 fps | 30 fps |

The 25 ms that earlier rounds attributed to "presentation overhead" is not presentation overhead. The capture blit is committed on the producer's command queue, so by in-order queue semantics it cannot begin until the frame it is copying has retired. Every CPU-side and drawable-side component measures below one millisecond combined.

That leaves one open problem, and it is arithmetic rather than scheduling:

> The SOURCE frame occupies the GPU for ~25 ms of every 33.3 ms period, leaving ~7.7 ms for interpolation passes that need ~15.8 ms. **The deficit is ~8.1 ms, and it lives entirely inside GPU work.** No presentation-side reordering can reach it.

Three candidate fixes were evaluated and **all were declined with measurement**, not caution: committing the blit earlier moves the GPU start by the same amount in the opposite direction; capturing in the completion handler gains nothing; a separate queue inherits the same hazard. The honest classification is `GPU_CAPACITY_FOR_INTERPOLATION` — see [R121 §11](FG-METAL-R121-PRESENTATION-OCCUPANCY-REPORT.md).

---

## Architecture

The core deals in opaque GPU resource references and explicit metadata. Renderer-specific resource import and synchronization translation belong in adapters, never in the core.

```text
Renderer / test application
        │
        │  imports renderer-owned GPU textures (never CPU readback)
        ▼
include/framegen ── public, opaque GPU-resource C ABI (draft, versioned)
        │
src/core ─────────── validation, submission contract, lifecycle
        ├── src/pacing ────── timing and presentation policy
        ├── src/quality ───── quality policy and controls
        ├── src/diagnostics ─ metadata and performance diagnostics
        └── backend contract
                ├── src/metal ──── Metal resource and execution path
                └── backends/* ─── separately evaluated algorithm candidates
```

**Resource flow.** The producer supplies two temporally ordered source textures. The adapter wraps them without routing pixels through CPU memory. The backend schedules GPU work and returns a distinct output texture plus the completion point the consumer must wait on. The consumer presents it and reports the outcome; resources are retained through both producer and consumer completion.

**Pacing.** `PresentationScheduler` is a thread-safe policy state machine. Source submissions update bounded metadata and return at most one generation request; callers run backend submission on their own worker. It allows at most one active or ready generated output, applies per-output readiness and presentation deadlines, and falls back to a real source frame when a generated result is late or unavailable. It never blocks on the backend while holding its lock and retains no FIFO.

**HUD preservation.** Explicit UI-plane mode interpolates scene colour and composites the declared endpoint UI in linear light. Automatic protection is a GPU confidence field over screen-position stability, local change disagreement, sharp edges, and short-range high-frequency structure, with a stateful per-pixel mask history. It is a conservative heuristic for screen-space UI, not a motion estimator — its failure modes are documented in [HUD preservation](docs/HUD_PRESERVATION.md).

The full contract — ownership, synchronization, clock domains, and adapter boundaries — is in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## Requirements

- macOS on Apple silicon, with the Apple SDK and Clang toolchain
- CMake 3.25 or newer
- Ninja
- A Metal-capable device for the preview host and benchmark runner

The core and its tests build without Metal, so a non-Apple host can still compile and run the renderer-independent path.

---

## Build and test

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
```

The default build produces five CTest targets: core, pacing simulation, C API, C API header smoke, and Metal C API.

Core-only build, with no Metal targets:

```sh
cmake -S . -B build-core -G Ninja \
  -DFRAMEGEN_BUILD_METAL=OFF \
  -DFRAMEGEN_BUILD_TEST_METAL=OFF
cmake --build build-core
ctest --test-dir build-core --output-on-failure
```

Python metric checks need only the standard library:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_benchmark_metrics.py
```

### Metal preview host

The preview requires converted weights at `models/local/rife-v4.26/rife-v4.26.fgweights`, or a path given by `FRAMEGEN_MODEL_WEIGHTS`. Weights are prepared locally and are not committed.

```sh
open build/adapters/test-metal/framegen-test-metal.app
```

Set `FRAMEGEN_METAL_MODEL_VARIANT=QUALITY` or `BALANCED` to select inference precision. The default is `QUALITY`.

### Benchmarks

```sh
cmake -S . -B build -G Ninja -DFRAMEGEN_BUILD_TOOLS=ON
cmake --build build --target framegen-benchmark-metal
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --output test-results/benchmarks/rife-v4.26-raw-synthetic-motion.json
```

Metric definitions, HUD-protected runs, and known limitations are in [benchmark guidance](docs/BENCHMARK.md). The scheduler host is a deterministic simulation, not a display-cadence test:

```sh
build/tests/framegen-pacing-host
```

---

## Repository layout

```text
include/framegen/    Public C ABI and C++ backend SPI
src/                 Core, Metal backend, pacing, quality, diagnostics
backends/            Candidate algorithms — reserved, not approved dependencies
adapters/            test-metal host; d3dmetal, dxmt, dxvk reserved
tools/               Benchmark runner, corpus, analyzer, RIFE conversion
tests/               CTest suite and Python metric checks
docs/                Benchmark, HUD, temporal quality, feasibility, provenance
experiments/         Dated investigation trees with evidence and reports
*.md (root)          Append-only engineering log, R65–R121 and Step 8–11 closeouts
```

The root-level `FG-METAL-R*.md` files are the project's primary record. They are kept at the root deliberately so that report-to-report references and evidence paths stay stable; a future documentation consolidation is tracked in [TASKS.md](TASKS.md).

---

## Prior renderer investigations

Four integration routes were investigated before the current title work. Three are closed, and one produced the baseline that redirected the effort.

| Route | Outcome | Why it stopped |
| --- | --- | --- |
| GPTK 4 / D3DMetal transparent adapter | **PARTIAL PASS** | No supported public per-swapchain Metal hook; same-chain generated presents break common application buffer progression |
| DXMT + `CAMetalDisplayLink` | **STOP** | Matched windowed and fullscreen runs failed presentation health; physical scanout unverifiable |
| DXVK-MacOS Step 11 | **11E NOT READY** | AppFrame/backbuffer identity, cross-frame A/B ownership, and release-ledger closure unproven |
| Native MoltenVK control | **Healthy baseline** | 59.99–60.01 FPS with 0 waits ≥ 50 ms — proving the starvation is upstream, in the Wine/DXVK stack |

The native control is the most useful result in that set: it converted a suspected platform limitation into a bounded, upstream-specific defect. Details are in the [Step 11 closeout](STEP11-CLOSEOUT-RECONCILIATION.md) and [decisions](DECISIONS.md).

---

## Documentation

| Document | Contents |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Modules, resource ownership, synchronization, adapter boundary |
| [Roadmap](ROADMAP.md) | Phase structure and prerequisites |
| [Tasks](TASKS.md) | Completed, blocked, and open work with verification records |
| [Decisions](DECISIONS.md) | Accepted constraints and open questions |
| [Step 11 closeout](STEP11-CLOSEOUT-RECONCILIATION.md) | DXVK disposition and 11E readiness |
| [Benchmark guidance](docs/BENCHMARK.md) | Corpus format, metrics, and limits |
| [Temporal quality](docs/TEMPORAL_QUALITY.md) | Synthetic evaluation and regressions |
| [HUD preservation](docs/HUD_PRESERVATION.md) | UI-plane and automatic protection |
| [Renderer feasibility](docs/feasibility/) | GPTK, D3DMetal, DXGI, DXMT reports |
| [Model provenance](docs/provenance/rife-v4.26.md) | Source, conversion, license, evidence |

Recent title-integration rounds, newest first: [R121](FG-METAL-R121-PRESENTATION-OCCUPANCY-REPORT.md) · [R120](FG-METAL-R120-CONTINUOUS-30-TO-60-TITLE-REPORT.md) · [R119](FG-METAL-R119-LIVE-CONTENTION-AND-EVIDENCE-ISOLATION-REPORT.md) · [R118](FG-METAL-R118-LIVE-GENERATION-THROUGHPUT-REPORT.md) · [R117](FG-METAL-R117-LOOKAHEAD-2X-FEASIBILITY-REPORT.md) · [R116](FG-METAL-R116-CRITICAL-PATH-PACING-REPORT.md) · [R115](FG-METAL-R115-REARMABLE-CONTINUOUS-TITLE-REPORT.md) · [R114](FG-METAL-R114-INPROCESS-TARGET-LATCH-TITLE-DISPLAY-REPORT.md)

---

## Project boundaries

These are constraints on the implementation, not aspirations.

- **GPU-native live path.** CPU code may schedule work and handle metadata; it must not read back frame pixels. No screen capture, desktop duplication, or CPU image-buffer processing.
- **Core independence.** The core must not depend on Highball, Wine, D3DMetal, DXMT, or DXVK. Integrations live in adapters and use documented public interfaces only.
- **Clean-room design.** No inspection, copying, reconstruction, or imitation of private Lossless Scaling / LSFG interfaces, binaries, models, or behaviour.
- **Evidence before claims.** A completed feasibility experiment is not a supported integration. Unverified means unverified in the README, the reports, and the commit messages.
- **Separate provenance.** Original code is Apache-2.0. Third-party code, models, weights, datasets, and runtime dependencies carry independent rights records in [PROVENANCE.md](PROVENANCE.md).

---

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [PROVENANCE.md](PROVENANCE.md) before proposing code, dependencies, model assets, or evaluation data. Contributions are expected to state what was verified and what was not, and to keep renderer-specific code in adapters.

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/); the scope names the subsystem (`api`, `metal`, `pacing`, `hud`, `temporal`, `dxmt`, `dxvk`, `experiments`, `docs`).

---

## License

Original project code is licensed under the [Apache License 2.0](LICENSE). Third-party components, models, weights, and datasets retain their own terms; see [PROVENANCE.md](PROVENANCE.md).
