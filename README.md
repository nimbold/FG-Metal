# Framegen

Framegen is an experimental renderer-independent frame-generation library. Its processing contract uses GPU textures: a renderer adapter supplies source frames, a backend generates an output texture, and the renderer decides when to present it. Frame pixels are not read back to the CPU, and the project does not capture the desktop.

> **Status: experimental; not a drop-in game mod or overlay.** The C ABI is a draft, and there is no supported adapter for an unmodified game.

> **Current renderer-feasibility status (2026-10-08): Step 10B.6.1 — STOP the current DXMT CAMetalDisplayLink Framegen architecture; RIFE is not cleared.** Matched windowed and fullscreen F runs preserved accepted-source safety but failed presentation health. A public contentsScale-only probe improved the short trace, then failed the fresh 60-second validation. Native calibration confirms Display surface rows are not FPS or drawable IDs; physical scanout remains unverified. See the [Step 10B.6.1 report](experiments/dxmt_framegen/evidence/step10b6_1/report.md).

> **DXVK-MacOS Step 11C: PARTIAL PASS; Step 11D is not cleared.** A rebuilt matched 20-second, 15 Hz run preserved measured DXGI accounting while issuing 42 extra WSI presents for 301 application Presents. The user directly confirmed brief magenta and cyan flashes in the post-audit candidate; the newest matched build has a separate API run. The scheduler remains far below doubled cadence, and fullscreen, recreation, failure, and pending-work shutdown checks remain unverified. See the [Step 11C report](experiments/dxvk_macos_step11c/REPORT.md).

> **Step 11D.3-A: CASE A3 — INCONCLUSIVE.** The primary native control architecture is x86_64 to match the translated STEP 11D.2 MoltenVK process. Architecture, trace publication, and correlation checks are in place, but the fresh leased Xcode build did not produce a MoltenVK dylib or native executable. Xcode failed while loading the project under the confined build environment. No native loader probe, 30-second graphics smoke, or five-minute baseline ran. See the [Step 11D.3-A1 report](experiments/dxvk_macos_step11d3a/STEP-11D3-A1-REPORT.md). Step 11D.3-B has not started.

## What works today

| Area | Current status |
| --- | --- |
| Core library and C ABI | Implemented; ABI is not stable and has not been validated by an external renderer. |
| Metal backend | Practical-RIFE v4.26 GPU inference and a standalone macOS preview host. Model weights are prepared separately and are not checked in. |
| Quality and pacing | Optional HUD/temporal controls and a model-independent pacing scheduler; results are synthetic and do not establish in-game quality or display pacing. |
| GPTK / D3DMetal | Cooperative D3D12 tests passed on Highball GPTK 4. Transparent DXGI attachment reached **PARTIAL PASS** on a controlled app; same-chain generated presents break common application buffer progression. It is not a product adapter. |
| DXMT | Step 10B.6.1: **WINDOWED FAIL / NOT CLEARED; FULLSCREEN FAIL; RIFE NOT CLEARED; architecture STOP.** Qualified matched runs passed source safety but retained abnormal Display durations and all Direct-to-Display No. Two public-property probes did not establish a durable fullscreen lane. Return to renderer-strategy selection; see the [report and evidence](experiments/dxmt_framegen/evidence/step10b6_1/report.md). |
| DXVK-MacOS | Step 11C: **PARTIAL PASS.** Separate AppFrameId/WSI/internal identity and measured DXGI accounting invariants pass in a matched low-rate windowed run; the user confirmed brief magenta and cyan flashes. The scheduler misses doubled cadence. Fullscreen/recreation/failure/pending-work shutdown checks remain unverified. See the [report and evidence](experiments/dxvk_macos_step11c/REPORT.md). |
| Native MoltenVK control | Step 11D.3-A: **A3 / INCONCLUSIVE.** The build lifecycle is leased and Gate 0 is current, but Xcode project loading failed under the build sandbox before compilation. No native runtime or graphics result exists. See the [A1 infrastructure report](experiments/dxvk_macos_step11d3a/STEP-11D3-A1-REPORT.md). |

See [the current decisions](DECISIONS.md), [work status](TASKS.md), and the [Step 8D.1 feasibility report](docs/feasibility/dxgi-d3d12-gptk4-step8d1.md) for evidence and limits.

## Features

- GPU texture import, asynchronous interpolation tickets, presentation feedback, history invalidation, and statistics through a provisional C ABI.
- A Metal backend using Practical-RIFE v4.26 with `QUALITY` (float32) and `BALANCED` (float16) inference modes.
- Optional explicit UI-plane composition and automatic HUD protection.
- An opt-in temporal quality controller and a model-independent presentation scheduler.
- A synthetic motion corpus, benchmark tools, and strict visual-quality comparisons.

The benchmark fixtures do not contain copyrighted game footage. Synthetic results are not substitutes for licensed game captures or real-game validation.

## Roadmap

The roadmap follows evidence and public interface availability. A completed feasibility experiment is not a supported renderer integration.

- [x] Build the renderer-independent GPU texture contract, Metal placeholder backend, preview host, and synthetic test harness.
- [x] Add offline image/temporal metrics and a model-independent presentation scheduler; keep quality claims limited to synthetic evidence.
- [x] Complete cooperative GPTK/D3DMetal and public DXGI experiments with their current limits recorded.
- [x] Record the current DXMT and DXVK-MacOS feasibility results, including unresolved native-baseline blockers.
- [ ] Select a renderer strategy only after the unresolved presentation and public resource-access questions have an evidence-backed answer.
- [ ] Stabilize the C ABI, device/format contract, synchronization rules, and adapter boundary through independent consumers.
- [ ] Expand evaluation using material with documented rights; benchmark quality and pacing on supported hardware.
- [ ] Select and validate an inference backend only after quality, latency, licensing, and redistribution review.
- [ ] Validate any chosen adapter through lifecycle/failure coverage, a sustained soak, and clean-machine packaging before describing it as supported.

See the detailed [roadmap](ROADMAP.md), current [tasks](TASKS.md), and [decisions](DECISIONS.md). Step 11D.3-B and RIFE implementation remain gated; this README does not authorize either.

## Build and test

### macOS with Metal

Requirements: macOS, Apple SDK and Clang toolchain, CMake 3.25 or newer, and Ninja.

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
```

Run the Python benchmark-metric checks with the standard library:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_benchmark_metrics.py
```

### Core-only build

The renderer-independent core and tests can also be built without the Metal targets:

```sh
cmake -S . -B build-core -G Ninja \
  -DFRAMEGEN_BUILD_METAL=OFF \
  -DFRAMEGEN_BUILD_TEST_METAL=OFF
cmake --build build-core
ctest --test-dir build-core --output-on-failure
```

### Metal preview and model weights

The preview requires a Metal-capable Mac and converted weights at `models/local/rife-v4.26/rife-v4.26.fgweights`, or a path provided with `FRAMEGEN_MODEL_WEIGHTS`. Prepare the weights using the [offline conversion guide](tools/rife/README.md). The weights are local and are not included in Git.

```sh
open build/adapters/test-metal/framegen-test-metal.app
```

Set `FRAMEGEN_METAL_MODEL_VARIANT=QUALITY` or `BALANCED` to select an inference precision mode. The default is `QUALITY`.

## Benchmarks

Enable the optional Metal benchmark runner with `-DFRAMEGEN_BUILD_TOOLS=ON`, then run the synthetic motion corpus:

```sh
cmake -S . -B build -G Ninja -DFRAMEGEN_BUILD_TOOLS=ON
cmake --build build --target framegen-benchmark-metal
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --output test-results/benchmarks/rife-v4.26-raw-synthetic-motion.json
```

For HUD-protected runs, metric definitions, comparisons, and known limitations, see [benchmark guidance](docs/BENCHMARK.md) and [temporal quality results](docs/TEMPORAL_QUALITY.md). The scheduler host is a simulation, not an in-game or physical-display test:

```sh
build/tests/framegen-pacing-host
```

## Documentation

| Document | Description |
| --- | --- |
| [Architecture](ARCHITECTURE.md) | Library modules, resource ownership, and adapter boundary. |
| [Roadmap](ROADMAP.md) | Planned project direction. |
| [Decisions](DECISIONS.md) | Accepted constraints and open questions. |
| [Tasks](TASKS.md) | Completed, blocked, and follow-up work. |
| [HUD preservation](docs/HUD_PRESERVATION.md) | UI-plane and automatic HUD controls. |
| [Temporal quality](docs/TEMPORAL_QUALITY.md) | Synthetic evaluation and regressions. |
| [Renderer feasibility](docs/feasibility/) | GPTK, D3DMetal, and DXGI experiment reports. |
| [Model provenance](docs/provenance/rife-v4.26.md) | Model source, conversion, license, and reference evidence. |
| [Contributing](CONTRIBUTING.md) and [provenance policy](PROVENANCE.md) | Contribution and third-party material requirements. |

## Project boundaries

- The core must not depend on Highball, Wine, D3DMetal, DXMT, or DXVK.
- Renderer integrations belong in separate adapters and must use documented/public interfaces.
- The live frame path must remain GPU-native; CPU code may schedule work and handle metadata, but must not read back frame pixels.
- Do not inspect, copy, depend on, reconstruct, or imitate private Lossless Scaling or LSFG interfaces, binaries, models, or behavior.
- Original project code is intended to use Apache-2.0. Third-party code, models, weights, datasets, and runtime dependencies have separate provenance and redistribution requirements; see [LICENSE](LICENSE) and [PROVENANCE.md](PROVENANCE.md).

## Contributing and support

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [PROVENANCE.md](PROVENANCE.md) before proposing code, dependencies, model assets, or evaluation data. For current priorities and integration limits, start with [TASKS.md](TASKS.md) and [DECISIONS.md](DECISIONS.md).
