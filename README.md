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

The bootstrap, benchmark framework, provisional Step 3 host API, Step 4 HUD preservation paths, and Step 5 Metal RIFE v4.26 backend are implemented. The host API is a versioned C ABI with backend/capability discovery, GPU-resource import, source-frame submission, asynchronous interpolation tickets, presentation feedback, history invalidation, HUD mode/source/debug controls, and statistics. HUD controls are a separate top-level v1 extension. The Metal backend advertises `COLOR_ONLY` and is registered explicitly with `framegen_metal_register_backend()`. QUALITY output matches the pinned CPU reference within one 8-bit channel value on the 64×64 and 65×63 fixtures. The ABI remains a draft; it has not been validated by an external renderer adapter or declared stable.

The Metal backend runs the complete Practical-RIFE v4.26 `IFNet_HDv3` color model with MPSGraph and purpose-built Metal kernels. It accepts matching opaque RGBA8 sRGB or linear-sRGB SDR textures, pads inputs to RIFE's mod-64 requirement, and writes an output texture without CPU pixel readback. The internal `QUALITY` and `BALANCED` variants use full-resolution float32 and float16 inference, respectively. HUD handling runs after scene interpolation: explicit scene/UI-plane composition uses the supplied UI plane, and automatic protection uses a soft confidence mask over already-composited frames. The model's intermediate scratch pool is capped at 2 GiB, with a 1280 MiB per-request limit checked before graph compilation; requests above the limit fail explicitly. These limits exclude MPSGraph workspaces and input/output/HUD textures. The RIFE implementation and checkpoint are MIT licensed; exact source, checkpoint, conversion provenance and reference results are recorded in [RIFE provenance](docs/provenance/rife-v4.26.md). The converted weights remain local and outside Git.

The checked-in synthetic benchmark results were captured against the earlier blend placeholder and remain historical. RIFE-specific raw and HUD-protected results are reported in the current benchmark artifacts. These synthetic scenes do not establish performance or quality on real games.

On an Apple M3, the serial 128×72 synthetic run measured QUALITY raw RIFE GPU p50/p95/p99 at 2.59/3.04/3.92 ms and QUALITY with automatic HUD protection at 2.59/2.67/3.34 ms, with no misses across 500 measured samples in either run. Automatic protection improves aggregate HUD tails on this corpus, while the strict comparison still reports regressions in some ROIs and temporal/edge tails. The runs sampled about 42 MB of GPU allocation and 91 MB of runner RSS. The runner samples GPU allocation after completion, so transient MPSGraph workspace peaks may be missed.

Backend availability reports whether a Metal device and converted weights are present; the runtime parses the weights at context creation and validates their architecture and graph shapes when it builds the first inference plan. CTest covers core and fake-provider contracts plus a Metal C API smoke test when a Metal device and converted RIFE weights are present, and reports the Metal test as skipped when either prerequisite is absent. The benchmark's source-throughput value is an offline input-upload proxy, not game FPS or presented-frame pacing. The standalone Metal host is the current GPU runtime boundary; D3DMetal integration, full-resolution performance evidence, and in-game FPS evidence remain future work. See [HUD preservation](docs/HUD_PRESERVATION.md) for the host controls and [benchmark guidance](docs/BENCHMARK.md) for measurement limits.

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
