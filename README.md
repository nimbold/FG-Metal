# FG-Metal

**GPU-native frame generation research for macOS & Apple silicon — zero screen capture, strictly evidence-gated.**

[![License](https://img.shields.io/badge/License-Apache--2.0-blue.svg)](LICENSE)
[![Platform](https://img.shields.io/badge/Platform-macOS%20%7C%20Apple%20Silicon-lightgrey.svg)](#requirements)
[![C++23](https://img.shields.io/badge/C%2B%2B-23-00599C.svg)](CMakeLists.txt)
[![Alpha Status](https://img.shields.io/badge/Alpha-R189%20Internal%20Baseline-orange.svg)](#status-at-a-glance)

FG-Metal pairs a renderer-independent C++23/Metal frame-generation library (`Framegen`) with an in-process MetalFX live integration (`R189`) that interpolates 30→60 fps inside a running game on Apple silicon. Every claim in this repo traces to a falsifiable measurement; failed experiments are published alongside passes.

> [!IMPORTANT]
> **Experimental research project — not a drop-in mod.**
> The supervised internal alpha (`R189`) is narrowly scoped to one title (Elden Ring, offline), one translation stack (Highball / D3DMetal), and one host class (Apple M3, 60 Hz display, 30→60 fps). There is **no** general adapter for arbitrary games, 60→120 Hz is not attempted, and external alpha testing is currently held (`HOLD_FOR_FREEZE_ROOT_CAUSE`).

---

## Contents

- [Status at a Glance](#status-at-a-glance)
- [Quick Start](#quick-start)
- [Architecture](#architecture)
- [Supervised Internal Alpha (`R189`) & Base-M3 Closures (`R193–R208`)](#supervised-internal-alpha-r189--base-m3-closures-r193r208)
- [Repository Layout](#repository-layout)
- [Documentation](#documentation)
- [Project Boundaries & License](#project-boundaries--license)

---

## Status at a Glance

| Track / Capability | Status | Key Evidence |
| --- | --- | --- |
| **Core Library & C ABI** (`src/`, `include/framegen/`) | **Implemented & Tested** | `ctest` (6/6), [ARCHITECTURE.md](ARCHITECTURE.md) |
| **Metal RIFE v4.26 Backend** (`src/metal/`) | **Offline / Synthetic Only** | [TEMPORAL_QUALITY.md](docs/TEMPORAL_QUALITY.md) |
| **Live 30→60 Alpha (`R189`)** (`experiments/.../r127-alpha-runtime/`) | **Promoted Internal Alpha** (20.4-min soak, `0` source drops) | [DECISIONS.md](DECISIONS.md) (D-019–D-020) |
| **External Alpha Test Gate** | **HELD (`HOLD_FOR_FREEZE_ROOT_CAUSE`)** | [TASKS.md](TASKS.md) |
| **Base-M3 Alternative Backends (`R193–R208`)** | **Closed / Frozen on `R189`** (8 paths falsified) | [DECISIONS.md](DECISIONS.md) (D-021–D-022) |
| **60→120 Hz & Arbitrary-Game Adapter** | **Not Attempted** | [R117](docs/reports/r117-lookahead-2x-feasibility.md), [TASKS.md](TASKS.md) |

---

## Quick Start

### Requirements

- **OS / Hardware:** macOS on Apple silicon (for Metal host & benchmarks); core library builds on any C++23 host
- **Toolchain:** Clang, CMake `>= 3.25`, Ninja, Python 3 (stdlib only for metric tests)

### Build & Run Tests

```sh
# 1. Build core library, Metal backend, and CTest suite
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure

# 2. Run Python metric & benchmark unit tests (stdlib only)
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_benchmark_metrics.py
```

<details>
<summary>Optional: Core-only build (no Metal), Metal preview app, and synthetic benchmarks</summary>

**Core-only build (cross-platform, no Metal):**
```sh
cmake -S . -B build-core -G Ninja -DFRAMEGEN_BUILD_METAL=OFF -DFRAMEGEN_BUILD_TEST_METAL=OFF
cmake --build build-core
ctest --test-dir build-core --output-on-failure
```

**Metal preview host** (requires local weights at `models/local/rife-v4.26/rife-v4.26.fgweights` or `FRAMEGEN_MODEL_WEIGHTS`):
```sh
open build/adapters/test-metal/framegen-test-metal.app
# Optional precision: FRAMEGEN_METAL_MODEL_VARIANT=QUALITY|BALANCED (default: QUALITY)
```

**Synthetic benchmark & pacing simulation:**
```sh
cmake -S . -B build -G Ninja -DFRAMEGEN_BUILD_TOOLS=ON
cmake --build build --target framegen-benchmark-metal
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --output test-results/benchmarks/rife-v4.26-raw-synthetic-motion.json

build/tests/framegen-pacing-host
```

</details>

---

## Architecture

FG-Metal keeps its reusable SDK strictly separate from title-specific live experiments:

```text
Renderer / Host App
  │  (imports GPU textures — zero CPU pixel readback)
  ▼
include/framegen/          Public, versioned C ABI & C++ backend SPI
  │
  ├── src/core/            Validation, submission contract, resource lifecycle
  ├── src/pacing/          PresentationScheduler (lock-free policy, real-frame fallback)
  ├── src/quality/         HUD preservation & quality policies
  ├── src/diagnostics/     Separated GPU vs. wall-clock telemetry
  └── src/metal/           Metal execution path (synthetic RIFE v4.26 backend)
```

- **Zero CPU Readback:** Producers submit GPU texture handles; the backend schedules asynchronous Metal work and returns a distinct output texture plus completion fence.
- **Bounded Pacing:** `PresentationScheduler` permits at most one active generated frame, enforces presentation deadlines, and falls back to real source frames when generation is late.
- **HUD Protection:** Supports explicit UI-plane compositing and conservative screen-space confidence masking ([docs/HUD_PRESERVATION.md](docs/HUD_PRESERVATION.md)).

---

## Supervised Internal Alpha (`R189`) & Base-M3 Closures (`R193–R208`)

The live integration in [`experiments/metalfx_frame_interpolator_R93/`](experiments/metalfx_frame_interpolator_R93/) runs in-process under Highball/D3DMetal, captures source frames on the GPU, computes coarse-to-fine motion vectors, and interpolates via `MTLFXFrameInterpolator` directly into `CAMetalLayer`.

### Promoted Baseline (`R189`)

- **Live Performance (`1470×956`, Apple M3):** `8.75–9.07 ms` cold / `12.17 ms` 20.4-min thermally soaked `estimatorGpuExec` median; separate `metalFXStartToEnd` `6.22–6.30 ms` cold / `8.54 ms` soaked *(disjoint clock domains — never summed)*.
- **Stability:** `0` SOURCE drops, `0` ordering violations, `0` protection/GPU errors, `0` `nextDrawableNil` events, and `0.19%–0.27%` natural misses (`R189` promoted; `R185` previous validated; `R172` immutable rollback).
- **Protected Regions:** `R165` status bars, `R170` letterbox borders, and opt-in `R185` Souls counter ROI.
- **Open Blocker (`HOLD_FOR_FREEZE_ROOT_CAUSE`):** External alpha testing remains held until opportunistic live natural-stall witness stacks are captured via `r195_freeze_witness_candidate.py`.

### Base-M3 Interpolation Closures (`R193–R208`)

Eight alternative interpolation and HUD-masking paths were tested on base Apple M3 and formally closed in [DECISIONS.md](DECISIONS.md) (D-021–D-022):

| Rounds | Investigated Path | Outcome | Primary Falsification / Rejection Reason |
| --- | --- | --- | --- |
| **R193–R194** | Single-scale `R108`/`R189` coarse block tuning | **Closed** | Could not fix fast foreground ghosting without breaking `2–4 px` slow camera pans |
| **R196–R197** | Apple `VTFrameProcessor` (`x86_64`) & `Vision` flow | **Closed** | `VT` unsupported under Rosetta; `Vision` `18.3 ms` + `0.365` foreground error; `arm64` `VT` `13.14 ms` idle / `23.23 ms` contended |
| **R199** | Post-MetalFX 6-hypothesis spatial MV repair | **Closed** | Improved `R176` sword/helmet slightly but regressed `tarnished_torso_tabard` (`0.812 < 0.820`) |
| **R200** | Full-res `1470×956` Metal FP16 RIFE (`rife-v4.26`) | **Closed** | `91.95 ms` GPU median and `+771 MiB` working set on Apple M3 |
| **R204–R205** | `0.25×` flow-only RIFE + `MTLFX` / warp | **Closed** | `29.7%–94.8%` slow-pan collapse at `2–4 px` + `12.6×` contended GPU scaling (`31.83 ms`) |
| **R206** | Path 1: Stateless relative-contrast HUD mask | **Parked** | `0.00%` dark-torso false-lock, but `ov=0` on single-frame `MTLFX` (`ROUTE_E` insufficient evidence) |
| **R207** | Path 2: 3-level analytical pyramid flow + custom warp | **Closed** | Gate 1 early-exit: `12.271 ms` idle GPU (`> 6.5 ms` stop) and worse `R176` EPE than `R189` |
| **R208** | Path 3: Native `arm64 <= 720p` ANE `VTFrameProcessor` | **Closed** | `3.34 ms` ANE fast cliff, but failed `R176` anti-blur, `R205` slow-pan (30–63×), and `4:2:0` HUD bleed (`151/255`) |

<details>
<summary>Historical Context: Live Alpha Milestones (R121–R139) & Prior Renderer Investigations</summary>

- **R121–R122:** Proved overlapped GPU execution lets interpolation run alongside source rendering with `+0.01%` source cadence impact.
- **R123–R135:** Built decoupled display pump, transactional source ownership, and capture-anchored fixed-latency 30→60 scheduling (`~29–30 fps` source → `~56 fps` displayed).
- **R136–R139:** Fixed 1.27 s pump starvation (`183 ms` max gap post-fix) and assembled the first reversible D3DMetal alpha candidate.
- **Prior Renderer Routes (Closed):** GPTK4/D3DMetal transparent adapter (`PARTIAL PASS` — no public per-swapchain hook), DXMT + `CAMetalDisplayLink` (`STOP`), DXVK-MacOS Step 11 (`11E NOT READY`; native MoltenVK control proved starvation is upstream in Wine/DXVK). See [Step 11 Closeout](docs/reports/step11-closeout/reconciliation.md).

</details>

---

## Repository Layout

```text
include/framegen/    Public C ABI and C++ backend SPI
src/                 Core, Metal backend, pacing, quality, diagnostics
backends/            Reserved algorithm candidates
adapters/            test-metal preview host; d3dmetal, dxmt, dxvk reserved
tools/               Benchmark runner, synthetic corpus, RIFE conversion tools
tests/               CTest suite and Python metric checks
docs/                Architecture, benchmark, HUD, temporal quality, R105–R139 reports
experiments/         Live R127/R189 alpha runtime & local R&D harnesses (evidence gitignored)
```

> [!NOTE]
> Large binary weights (`*.fgweights`), personal Highball `bottle*.json` configs/backups, live witness dumps, game-frame PNG captures, raw logs, and compiled offline probe executables are excluded via [`.gitignore`](.gitignore) and remain strictly local.

---

## Documentation

| Document | Purpose |
| --- | --- |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Module boundaries, resource ownership, synchronization, clock domains |
| [DECISIONS.md](DECISIONS.md) | Accepted engineering constraints, `R189` promotion, and `R193–R208` closures |
| [TASKS.md](TASKS.md) | Open external-alpha freeze gate (`HOLD_FOR_FREEZE_ROOT_CAUSE`) & completed work |
| [ROADMAP.md](ROADMAP.md) | Phase status, active freeze on `R189`, and reopen criteria |
| [docs/BENCHMARK.md](docs/BENCHMARK.md) | Synthetic corpus format, metrics, and evaluation limits |
| [docs/HUD_PRESERVATION.md](docs/HUD_PRESERVATION.md) | Explicit UI-plane and automatic HUD confidence masking |
| [docs/TEMPORAL_QUALITY.md](docs/TEMPORAL_QUALITY.md) | Synthetic temporal quality benchmarks |
| [PROVENANCE.md](PROVENANCE.md) | Third-party licenses, model weights, and rights records |

---

## Project Boundaries & License

- **GPU-Native:** No screen capture, desktop duplication, or CPU pixel readbacks in the live path.
- **Core Independence:** `src/` and `include/` never depend on Highball, Wine, D3DMetal, DXMT, or DXVK.
- **Clean-Room:** Zero inspection, copying, or reconstruction of proprietary or GPL-3.0 third-party frame-generation code.
- **Contributing:** See [CONTRIBUTING.md](CONTRIBUTING.md) and [PROVENANCE.md](PROVENANCE.md). Commits follow [Conventional Commits](https://www.conventionalcommits.org/).
- **License:** Original project code is licensed under the [Apache License 2.0](LICENSE).
