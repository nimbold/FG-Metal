# Framegen

Framegen is an experimental macOS library for renderer-integrated frame generation. Its live processing contract is built around GPU textures: renderer adapters provide frames as GPU resources, a backend processes them on the GPU, and the caller receives an output texture. The project does not capture the desktop or copy image pixels through CPU memory.

The first implementation is a standalone Metal host that submits two textures through the generic color-only API and displays Practical-RIFE v4.26 interpolation. It establishes resource ownership, synchronization, and presentation boundaries without tying the host API to a particular model.

## Project boundaries

- The reusable core must not depend on Highball, Wine, D3DMetal, DXMT, or DXVK.
- Renderer integrations are adapters outside the core. An adapter may import or wrap renderer textures using documented/public mechanisms and pass GPU resources to the library.
- The live path operates directly on GPU textures. CPU work may describe resources, schedule work, and collect metadata; it must not read back or copy frame pixels.
- This project is independently designed. Do not use, copy, depend on, inspect, reconstruct, or imitate private interfaces, binaries, models, or reverse-engineered behavior from lsfg-vk, lsfg-metal, Lossless Scaling, or old Highball LSFG integration. Lossless Scaling is only a high-level product and UX reference.
- Original project code is intended to use Apache-2.0. Third-party source, models, weights, datasets, and runtime dependencies require separate provenance and redistribution review.

See [ARCHITECTURE.md](ARCHITECTURE.md) for module boundaries and [DECISIONS.md](DECISIONS.md) for current decisions and open questions.

## Development direction

Target the newest macOS SDK and Apple toolchain available in the development environment, C++23, CMake, Ninja, and modern Metal APIs, including Metal 4 where applicable. Objective-C++ is limited to Apple framework interop. The initial host is a Metal-only test application; D3DMetal, DXMT, and DXVK adapters are later work and are not part of this bootstrap.

## Current status

The bootstrap, benchmark framework, provisional host API, HUD preservation paths, Metal RIFE v4.26 backend, and Step 6 temporal quality controller are implemented. The versioned C ABI supports backend/capability discovery, GPU-resource import, source-frame submission, asynchronous interpolation tickets, presentation feedback, history invalidation, HUD and temporal-quality controls, and statistics. The Metal backend advertises `COLOR_ONLY`, `UI_PLANE`, `AUTOMATIC_HUD_PROTECTION`, `HUD_DEBUG_VISUALIZATION`, `ARBITRARY_INTERPOLATION_TIME`, and `TEMPORAL_QUALITY_CONTROLLER`, and is registered explicitly with `framegen_metal_register_backend()`. QUALITY output matches the pinned CPU reference within one 8-bit channel value on the 64×64 and 65×63 fixtures. The ABI remains a draft; it has not been validated by an external renderer adapter or declared stable.

The Metal backend runs the complete Practical-RIFE v4.26 `IFNet_HDv3` color model with MPSGraph and purpose-built Metal kernels. It accepts matching opaque RGBA8 sRGB or linear-sRGB SDR textures, pads inputs to RIFE's mod-64 requirement, and writes an output texture without CPU pixel readback. The internal `QUALITY` and `BALANCED` variants use full-resolution float32 and float16 inference, respectively. HUD handling runs after scene interpolation: explicit scene/UI-plane composition uses the supplied UI plane, and automatic protection uses a soft confidence mask over already-composited frames. The model's intermediate scratch pool is capped at 2 GiB, with a 1280 MiB per-request limit checked before temporal or inference allocations; requests above the limit fail explicitly. These limits exclude MPSGraph workspaces and input/output/HUD textures. The RIFE implementation and checkpoint are MIT licensed; exact source, checkpoint, conversion provenance and reference results are recorded in [RIFE provenance](docs/provenance/rife-v4.26.md). The converted weights remain local and outside Git.

The model-independent `TemporalQualityController` adds GPU confidence estimates, endpoint/source fallback, scene-cut detection, and per-stream temporal history. It remains disabled by default. On the Apple M3 synthetic matrix, endpoint fallback lowered HUD flicker p95 by 23.3% and weapon-sight flicker p95 by 16.6%, while whole-frame flicker p95 rose 3.7% and minimum thin-edge recall fell from 0.7055 to 0.6090. All six strict policy comparisons report `REGRESSION`, including temporal stabilization combined with automatic HUD protection. Keep it opt-in and evaluate the target renderer and content; see [temporal quality results, visual inspection, and limitations](docs/TEMPORAL_QUALITY.md).

The checked-in early synthetic benchmark results were captured against the earlier blend placeholder and remain historical. Current RIFE raw, HUD, temporal, and combined results use the rotation fixture and are documented with their strict comparisons in [the temporal quality report](docs/TEMPORAL_QUALITY.md). Synthetic scenes do not establish performance or quality on real games.

Backend availability reports whether a Metal device and converted weights are present; the runtime parses the weights at context creation and validates their architecture and graph shapes when it builds the first inference plan. CTest covers core, fake-provider, timestamp-contract, oversized-resolution-preflight, and Metal C API smoke checks when a Metal device and converted RIFE weights are present; the Metal test is skipped when either prerequisite is absent. The benchmark's source-throughput value is an offline input-upload proxy, not game FPS or presented-frame pacing. The standalone Metal host is the current GPU runtime boundary; D3DMetal integration, full-resolution performance evidence, and in-game FPS evidence remain future work. See [HUD preservation](docs/HUD_PRESERVATION.md) for the host controls and [benchmark guidance](docs/BENCHMARK.md) for measurement limits.

The committed synthetic fixture covers slow panning, fast camera rotation, third-person motion, foliage, thin geometry and fences, particles, translucent scene and UI layers, moving highlights, sights and static crosshairs, subtitles, minimap, health bar, timer, scrolling text, flashing UI, moving menu, and changing HUD counters. HUD and text are mandatory pointwise ROIs; named UI guards include an exact crosshair-stroke mask. Those guards compare per-target RGB errors and per-pixel temporal residual, color flicker, and edge flicker. Racing, scene-cut and loading-transition sequences, and richer subtitle cases remain planned for corpus coverage. A separate same-histogram cut probe is recorded in the temporal quality report. See [the corpus catalog](tools/benchmark/corpus/catalog.json) for the per-category status. No copyrighted game footage is included.

## Build and run

Prerequisites are macOS with an Apple SDK/Clang toolchain, CMake 3.25 or newer, and Ninja. On macOS, the Metal backend and test app are enabled by default.

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
```

On macOS, run the analyzer's standard-library metric and regression checks with:

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_benchmark_metrics.py
```

Open the native Metal preview with:

```sh
open build/adapters/test-metal/framegen-test-metal.app
```

The Metal test host requires a Metal-capable device and the separately converted weights at `models/local/rife-v4.26/rife-v4.26.fgweights` (or `FRAMEGEN_MODEL_WEIGHTS`). Prepare the checkpoint with [the offline conversion steps](tools/rife/README.md). Set `FRAMEGEN_METAL_MODEL_VARIANT=QUALITY` or `BALANCED` to select an internal precision mode; the default is `QUALITY`. A core-only build can disable both Metal targets with `-DFRAMEGEN_BUILD_METAL=OFF -DFRAMEGEN_BUILD_TEST_METAL=OFF`. The Python analyzer uses only the standard library and can validate/compare results on other hosts. The Metal benchmark runner is an optional macOS target; configure it with `-DFRAMEGEN_BUILD_TOOLS=ON`.

Run the committed synthetic fixture after building the runner:

```sh
cmake -S . -B build -G Ninja -DFRAMEGEN_BUILD_TOOLS=ON
cmake --build build --target framegen-benchmark-metal
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --output test-results/benchmarks/rife-v4.26-raw-synthetic-motion.json
```

Run automatic HUD protection against the same corpus and target times with `--hud-mode automatic`; see [BENCHMARK.md](docs/BENCHMARK.md) for raw and protected commands and the metrics each run records.

Compare the checked-in raw and automatic-HUD RIFE runs:

```sh
python3 tools/benchmark/bench.py compare \
  --baseline test-results/benchmarks/rife-v4.26-raw-synthetic-motion.json \
  --candidate test-results/benchmarks/rife-v4.26-automatic-synthetic-motion.json \
  --output test-results/benchmarks/rife-v4.26-raw-vs-automatic.json \
  --summary test-results/benchmarks/rife-v4.26-raw-vs-automatic.md
```

See [BENCHMARK.md](docs/BENCHMARK.md) for corpus authoring, arbitrary interpolation timestamps, masked temporal/edge metrics, runtime fields, comparison rules, and limitations. Analytic ground-truth providers are Python code loaded from the corpus; run only providers you trust.

## Project boundaries and next work

The standalone Metal host demonstrates GPU-native inference, and the deterministic 64×64 fixture provides a reference-parity check. The provisional ABI still needs validation with an external renderer adapter, and the synthetic benchmark does not substitute for real-game quality or display-pacing evidence. Next work can address renderer integration and broader licensed evaluation footage.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [PROVENANCE.md](PROVENANCE.md) before proposing dependencies, model assets, or implementation material. In particular, proprietary LSFG implementation material must not be incorporated.
