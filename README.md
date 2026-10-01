# Framegen

Framegen is an experimental macOS library for renderer-integrated frame generation. Its live processing contract is built around GPU textures: renderer adapters provide frames as GPU resources, a backend processes them on the GPU, and the caller receives an output texture. The project does not capture the desktop or copy image pixels through CPU memory.

The first implementation is deliberately small: a Metal test host submits two textures through a placeholder API and displays a GPU-blended result. It establishes resource ownership, synchronization, and presentation boundaries before any frame-interpolation algorithm is selected.

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

The bootstrap, benchmark framework, provisional Step 3 host API, and Step 4 HUD preservation paths are implemented. The host API is a versioned C ABI with backend/capability discovery, GPU-resource import, source-frame submission, asynchronous interpolation tickets, presentation feedback, history invalidation, HUD mode/source/debug controls, and statistics. HUD controls are a separate top-level v1 extension. The Metal backend is registered explicitly with `framegen_metal_register_backend()`. The ABI remains a draft; it has not been validated by an external renderer adapter or declared stable.

The only processing backend is still the placeholder Metal path. It supports three host-visible HUD modes: whole-frame blending with no HUD knowledge, preferred explicit scene/UI-plane composition (straight, premultiplied, or opaque UI alpha), and heuristic protection for already-composited frames. Automatic protection produces a soft confidence mask, stabilizes it over chronologically ordered source pairs, and lets the host choose previous, current, or nearest-presentation UI. Debug views are optional and disabled by default. This is a first-class API path, but it is not a general scene-flow estimator or motion-compensated interpolator.

The current synthetic results are mixed. At timestamp-quantized midpoint targets, automatic-current reduced HUD pixel-error p95 from 0.2858 to 0.2690, temporal-residual p95 from 0.2876 to 0.2766, and flicker p95 from 0.5752 to 0.5144 versus raw blending. All-image pixel-error p95 increased from 0.2107 to 0.2201; moving-menu p95 increased from 0.3616 to 0.6867. Exact static crosshair and text pixels remain stable, but those regions are also exact in the raw baseline. The separate five-pair mask check reduced transition MAE by 37.3%, while 0.5-threshold HUD-ROI proxy recall was only 0.3119 and stabilized mask coverage rose across the sequence. Treat this as synthetic placeholder evidence, not proof of general HUD detection quality.

No RIFE implementation, model weights, Core ML or MPSGraph backend, D3DMetal integration, Wine/game integration, renderer adapter, or in-game FPS evidence exists. The benchmark's source-throughput value is an offline input-upload proxy, not game FPS or presented-frame pacing. CTest covers the placeholder and fake-provider contracts, not renderer integration or ABI stability. See [HUD preservation](docs/HUD_PRESERVATION.md) for per-policy measurements, mask diagnostics, and documented quality regressions.

The committed synthetic fixture covers slow panning, third-person motion, foliage, thin geometry and fences, particles, translucent scene and UI layers, moving highlights, sights and static crosshairs, subtitles, minimap, health bar, timer, scrolling text, flashing UI, moving menu, and changing HUD counters. HUD and text are mandatory pointwise ROIs; named UI guards include an exact crosshair-stroke mask. Those guards compare per-target RGB errors and per-pixel temporal residual, color flicker, and edge flicker. Fast rotation, racing, scene cuts, loading transitions, and richer subtitle cases remain planned. See [the corpus catalog](tools/benchmark/corpus/catalog.json) for the per-category status. No copyrighted game footage is included.

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

The Metal test host requires a Metal-capable device. A core-only build can disable both Metal targets with `-DFRAMEGEN_BUILD_METAL=OFF -DFRAMEGEN_BUILD_TEST_METAL=OFF`. The Python analyzer uses only the standard library and can validate/compare results on other hosts. The Metal benchmark runner is an optional macOS target; configure it with `-DFRAMEGEN_BUILD_TOOLS=ON`.

Run the committed synthetic fixture after building the runner:

```sh
cmake -S . -B build -G Ninja -DFRAMEGEN_BUILD_TOOLS=ON
cmake --build build --target framegen-benchmark-metal
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --output test-results/benchmarks/candidate-synthetic-motion.json
```

Compare a candidate with the checked-in placeholder baseline:

```sh
python3 tools/benchmark/bench.py compare \
  --baseline test-results/benchmarks/placeholder-synthetic-motion.json \
  --candidate test-results/benchmarks/candidate-synthetic-motion.json \
  --output test-results/benchmarks/placeholder-vs-candidate.json \
  --summary test-results/benchmarks/placeholder-vs-candidate.md
```

See [BENCHMARK.md](docs/BENCHMARK.md) for corpus authoring, arbitrary interpolation timestamps, masked temporal/edge metrics, runtime fields, comparison rules, and limitations. Analytic ground-truth providers are Python code loaded from the corpus; run only providers you trust.

## Project boundaries and next work

The benchmark is a quality-evaluation framework, not evidence that a frame-generation algorithm has been selected or validated. Next work is to validate the provisional ABI and synchronization contract with a real adapter, broaden the synthetic corpus, identify redistribution-cleared sequences, and evaluate algorithm and backend candidates before implementation. RIFE-family approaches, Core ML, and MetalFX remain research options, not selected dependencies or promised backends.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [PROVENANCE.md](PROVENANCE.md) before proposing dependencies, model assets, or implementation material. In particular, proprietary LSFG implementation material must not be incorporated.
