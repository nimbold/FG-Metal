# Framegen

Framegen is an experimental renderer-independent frame-generation library. Its processing contract uses GPU textures: a renderer adapter supplies source frames, a backend generates an output texture, and the renderer decides when to present it. Frame pixels are not read back to the CPU, and the project does not capture the desktop.

> **Status: experimental; not a drop-in game mod or overlay.** The C ABI is a draft, and there is no supported adapter for an unmodified game.

## What works today

| Area | Current status |
| --- | --- |
| Core library and C ABI | Implemented; ABI is not stable and has not been validated by an external renderer. |
| Metal backend | Practical-RIFE v4.26 GPU inference and a standalone macOS preview host. Model weights are prepared separately and are not checked in. |
| Quality and pacing | Optional HUD/temporal controls and a model-independent pacing scheduler; results are synthetic and do not establish in-game quality or display pacing. |
| GPTK / D3DMetal | Cooperative D3D12 tests passed on Highball GPTK 4. Transparent DXGI attachment reached **PARTIAL PASS** on a controlled app; same-chain generated presents break common application buffer progression. It is not a product adapter. |
| DXMT | Step 10B.4 **PARTIAL PASS**: the final advance-B candidate retained all 1,796 accepted sources safely. It reduced no-output ticks 26→19; callbacks/submissions/positive feedback were 60.045/59.726/57.079 Hz, while the p95 positive interval remained 45.183 ms. Of 1,638 positive G timestamps, 269 followed B's display target and 56 lacked a complete positive A/G/B endpoint bracket. B's early selection also returned positive timestamps before its scheduled source time in 125 of 152 advance cases. No RIFE or Highball change is part of this result. See the [Step 10B.4 report](docs/feasibility/dxmt-framegen-step10b4.md) and [retained evidence](experiments/dxmt_framegen/evidence/step10b4/). |

See [the current decisions](DECISIONS.md), [work status](TASKS.md), and the [Step 8D.1 feasibility report](docs/feasibility/dxgi-d3d12-gptk4-step8d1.md) for evidence and limits.

## Features

- GPU texture import, asynchronous interpolation tickets, presentation feedback, history invalidation, and statistics through a provisional C ABI.
- A Metal backend using Practical-RIFE v4.26 with `QUALITY` (float32) and `BALANCED` (float16) inference modes.
- Optional explicit UI-plane composition and automatic HUD protection.
- An opt-in temporal quality controller and a model-independent presentation scheduler.
- A synthetic motion corpus, benchmark tools, and strict visual-quality comparisons.

The benchmark fixtures do not contain copyrighted game footage. Synthetic results are not substitutes for licensed game captures or real-game validation.

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
