# FG-Metal

**Renderer-integrated frame generation for macOS — GPU-native, evidence-gated, no screen capture.**

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Apple%20silicon-lightgrey.svg)](#requirements)
[![C++](https://img.shields.io/badge/C%2B%2B-23-00599C.svg)](CMakeLists.txt)
[![Status](https://img.shields.io/badge/status-alpha%20rc-orange.svg)](#status-at-a-glance)

FG-Metal is a frame-generation research project built around one rule: **no claim survives without a measurement that could have falsified it.** It pairs a renderer-independent GPU-texture library with a live in-process integration that generates and displays real interpolated frames inside a running game on Apple silicon — continuously, at 30→60, through the game's own presentation layer.

Every headline number in this repository traces to a dated report naming the harness, the run parameters, and the build hashes that produced it. The raw evidence stays on the machine that generated it; the report states what was measured. Results that failed are published with the same prominence as results that passed.

> **Naming.** The reusable library is `Framegen` in the source tree and in [ARCHITECTURE.md](ARCHITECTURE.md); `FG-Metal` is the repository and the research programme around it.

> **Not a product.** The first alpha release candidate is narrowly scoped — one title, one translation stack, one host class — and requires manual setup. There is no supported adapter for an unmodified game, and 60→120 Hz is not attempted. See [Status at a glance](#status-at-a-glance) for exactly what is and is not established.

---

## Contents

- [Status at a glance](#status-at-a-glance)
- [What has actually been demonstrated](#what-has-actually-been-demonstrated)
- [From blocker to alpha](#from-blocker-to-alpha)
- [Open items](#open-items)
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
| Generation overlapping the game's own rendering at 30 fps | **Validated** | [R122](docs/reports/r122-overlapped-gpu-capacity.md) |
| Continuous 30→60 through the title's layer | **Validated** | [R126](docs/reports/r126-self-present-gate.md), [R134](docs/reports/r134-restore-r126-class-cadence.md) |
| Exactly-once SOURCE ownership and ordering | **Validated** | [R129](docs/reports/r129-transactional-source-ownership.md)–[R132](docs/reports/r132-presentation-ordered-estimator-ingestion.md) |
| Fixed-latency capture-anchored 30→60 pipeline | **Validated** (~29–30 fps source → ~56 fps displayed) | [R135](docs/reports/r135-fixed-latency-30-to-60-pipeline.md) |
| D3DMetal alpha runtime | **Validated** (12-min soak, zero source drops) | [R138](docs/reports/r138-fg-starvation-classified-and-fixed.md) |
| First alpha release candidate | **Ready for controlled testing** | [R139](docs/reports/r139-first-alpha-release-candidate.md), [independent review](docs/reports/r139-independent-review.md) |
| Continuous 60→120 | **Not attempted** | [R117](docs/reports/r117-lookahead-2x-feasibility.md) |
| Supported adapter for an unmodified game | **None exists** | [TASKS.md](TASKS.md) |

---

## What has actually been demonstrated

The project has two tracks that are deliberately kept separate.

**1. A reusable library.** `src/`, `include/`, `backends/`, and `adapters/` build a renderer-independent frame-generation core: opaque GPU resource import, asynchronous interpolation tickets, presentation feedback, history invalidation, HUD preservation modes, and a model-independent pacing scheduler. It builds clean and passes its full test suite on Apple silicon. Its quality numbers come from a project-authored synthetic corpus, and they are not in-game quality claims.

**2. A live in-process integration.** [`experiments/metalfx_frame_interpolator_R93/`](experiments/metalfx_frame_interpolator_R93/) is where the product question is actually being answered. It hooks a real title running under Highball/D3DMetal, captures source frames on the GPU, derives a motion field, interpolates with MetalFX, and presents the generated frame through the game's own `CAMetalLayer`.

What that integration has proven, in a real game process:

- **Continuous 30→60 frame generation** through the title's own layer, with the game rendering normally underneath ([R126](docs/reports/r126-self-present-gate.md), re-established on the fixed-latency pipeline in [R135](docs/reports/r135-fixed-latency-30-to-60-pipeline.md)).
- A **fixed-latency display schedule**: every source and every interpolated midpoint is presented at a capture-anchored slot, at ~29–30 fps source → ~56 fps displayed in qualifying windows ([R135](docs/reports/r135-fixed-latency-30-to-60-pipeline.md)).
- **Exactly-once source ownership**: zero dropped sources and zero ordering violations across validation windows and a 12-minute soak ([R129](docs/reports/r129-transactional-source-ownership.md)–[R132](docs/reports/r132-presentation-ordered-estimator-ingestion.md), [R138](docs/reports/r138-fg-starvation-classified-and-fixed.md)).
- A measured 1.27 s frame-generation starvation burst, **root-caused and fixed** with a one-assignment provider change; post-fix, the longest generation-only gap in the soak was 183 ms ([R138](docs/reports/r138-fg-starvation-classified-and-fixed.md)).
- A **release candidate**: reproducible bundle, reversible bottle install/restore, ON/OFF control, five clean live enable/disable cycles, and an independent 15-point review ([R139](docs/reports/r139-first-alpha-release-candidate.md)).

---

## From blocker to alpha

R121 measured the era-defining deficit: the source frame occupied the GPU ~25 ms of every 33.3 ms period, leaving ~8 ms short for interpolation. The conclusion then was `GPU_CAPACITY_FOR_INTERPOLATION` — no presentation-side reordering could reach it.

R122 reframed the question. Instead of squeezing interpolation into the same period, let the game render the next source frame **while** generation works on the previous pair. Measured answer: generation fits, and the source cadence does not move (+0.01 %). The deficit was a scheduling assumption, not a hardware limit.

The rounds that turned that result into an alpha:

| Round | What it established |
| --- | --- |
| [R123](docs/reports/r123-buffered-source-presentation.md) | Buffered source presentation — the display chain decoupled from title drawables |
| [R125](docs/reports/r125-retention-control.md) | Instrumentation retention contamination found and removed |
| [R126](docs/reports/r126-self-present-gate.md) | Self-present gate; continuous 30→60 title FG validated |
| [R129](docs/reports/r129-transactional-source-ownership.md)–[R133](docs/reports/r133-restore-live-capture-availability.md) | Transactional source ownership, presentation ordering, producer-side capture |
| [R135](docs/reports/r135-fixed-latency-30-to-60-pipeline.md) | Capture-anchored fixed-latency schedule; 30→60 proven at target rate |
| [R136](docs/reports/r136-miss-boundary-glitches.md) | Capture-aware drop guard for miss boundaries |
| [R138](docs/reports/r138-fg-starvation-classified-and-fixed.md) | The starvation burst classified and fixed; alpha validated |
| [R139](docs/reports/r139-first-alpha-release-candidate.md) | First alpha release candidate; independent review PASS |

The alpha runtime is deliberately narrow: Apple silicon (M3 host), Highball, D3DMetal translation, one title (Elden Ring, offline), 60 Hz display, 30→60 target. It defaults to OFF and installs reversibly. See the [R139 report](docs/reports/r139-first-alpha-release-candidate.md) for scope, limits, and verdicts.

---

## Open items

Tracked, measured, and unsolved — detailed in the [R139 report](docs/reports/r139-first-alpha-release-candidate.md):

- **Edge haze.** A reported softening near screen edges in heavy gameplay. Bounded capture analysis found no edge-concentrated spatial defect; classified INCONCLUSIVE, with a measured photometric difference on captured internal frames queued for disambiguation.
- **Target-rate retest.** No R139 window reached the ~29–30 fps source class; historical R135 numbers stand until one does.
- **Display latency.** ~101.6 ms source-hook-to-display median at target rate (R135). Latency at slower cadences measured higher, as the fixed schedule predicts.
- **Pump QoS default.** An opt-in thread-priority gate compresses the wake-lateness tail (p99 16.9 → 5.1 ms); it ships default-off until a soak validates it as the default.
- **60→120 Hz, more titles, other stacks.** Not attempted.

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

### Live alpha runtime harness (R127 line)

The in-process runtime that rounds R122–R139 validated lives in [`experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/`](experiments/metalfx_frame_interpolator_R93/r127-alpha-runtime/). Its build scripts compile the injected observer, the display pump, and the deterministic suites (diagnostics, pump, integration, trace selection). Operating the alpha against a real title requires the local Highball setup described in the [R139 report](docs/reports/r139-first-alpha-release-candidate.md); the evidence corpus it produces stays local.

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
docs/reports/        Round reports cited by this README (R105–R139), Step 11 closeout
experiments/         Investigation harnesses; evidence corpora are retained locally
```

**What is published, and what is not.** This repository tracks the library, its documentation, the round reports the documentation cites, and the harness source of the experiment line — including the alpha runtime observer and its test suites. It does not track the accumulated evidence: raw frame dumps, instrument traces, capture PNGs, per-round logs, and built binaries ran to gigabytes against a few megabytes of source, which made the repository slow to clone without making any claim more verifiable. Those artefacts are retained on the machine that produced them, and the reports record the harness, run parameters, and build hashes needed to reproduce a measurement. `.gitignore` documents the boundary; `git add -f <path>` publishes an excluded artefact deliberately.

---

## Prior renderer investigations

Four integration routes were investigated before the current title work. Three are closed, and one produced the baseline that redirected the effort.

| Route | Outcome | Why it stopped |
| --- | --- | --- |
| GPTK 4 / D3DMetal transparent adapter | **PARTIAL PASS** | No supported public per-swapchain Metal hook; same-chain generated presents break common application buffer progression |
| DXMT + `CAMetalDisplayLink` | **STOP** | Matched windowed and fullscreen runs failed presentation health; physical scanout unverifiable |
| DXVK-MacOS Step 11 | **11E NOT READY** | AppFrame/backbuffer identity, cross-frame A/B ownership, and release-ledger closure unproven |
| Native MoltenVK control | **Healthy baseline** | 59.99–60.01 FPS with 0 waits ≥ 50 ms — proving the starvation is upstream, in the Wine/DXVK stack |

The native control is the most useful result in that set: it converted a suspected platform limitation into a bounded, upstream-specific defect. Details are in the [Step 11 closeout](docs/reports/step11-closeout/reconciliation.md) and [decisions](DECISIONS.md).

---

## Documentation

| Document | Contents |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Modules, resource ownership, synchronization, adapter boundary |
| [Roadmap](ROADMAP.md) | Phase structure and prerequisites |
| [Tasks](TASKS.md) | Completed, blocked, and open work with verification records |
| [Decisions](DECISIONS.md) | Accepted constraints and open questions |
| [Step 11 closeout](docs/reports/step11-closeout/reconciliation.md) | DXVK disposition and 11E readiness |
| [Round reports](docs/reports/) | R105–R139 title-integration reports and the Step 11 closeout series |
| [Benchmark guidance](docs/BENCHMARK.md) | Corpus format, metrics, and limits |
| [Temporal quality](docs/TEMPORAL_QUALITY.md) | Synthetic evaluation and regressions |
| [HUD preservation](docs/HUD_PRESERVATION.md) | UI-plane and automatic protection |
| [Renderer feasibility](docs/feasibility/) | GPTK, D3DMetal, DXGI, DXMT reports |
| [Model provenance](docs/provenance/rife-v4.26.md) | Source, conversion, license, evidence |

Recent title-integration rounds, newest first: [R139](docs/reports/r139-first-alpha-release-candidate.md) · [R138](docs/reports/r138-fg-starvation-classified-and-fixed.md) · [R137](docs/reports/r137-pump-starvation-alpha-status.md) · [R136](docs/reports/r136-miss-boundary-glitches.md) · [R135](docs/reports/r135-fixed-latency-30-to-60-pipeline.md) · [R134](docs/reports/r134-restore-r126-class-cadence.md) · [R133](docs/reports/r133-restore-live-capture-availability.md) · [R132](docs/reports/r132-presentation-ordered-estimator-ingestion.md) · [R131](docs/reports/r131-transactional-ownership-report.md) · [R130](docs/reports/r130-transactional-ownership-gate.md) · [R129](docs/reports/r129-transactional-source-ownership.md) · [R128](docs/reports/r128-source-ownership-planner-handoff.md) · [R126](docs/reports/r126-self-present-gate.md) · [R125](docs/reports/r125-retention-control.md) · [R124](docs/reports/r124-drawable-retirement.md) · [R123](docs/reports/r123-buffered-source-presentation.md) · [R122](docs/reports/r122-overlapped-gpu-capacity.md) · [R121](docs/reports/r121-presentation-occupancy.md)

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
