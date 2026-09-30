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

## Build and run

Prerequisites are macOS with an Apple SDK/Clang toolchain, CMake 3.25 or newer, and Ninja. On macOS, the Metal backend and test app are enabled by default.

```sh
cmake -S . -B build -G Ninja
cmake --build build
ctest --test-dir build --output-on-failure
```

Open the native Metal preview with:

```sh
open build/adapters/test-metal/framegen-test-metal.app
```

The Metal test host requires a Metal-capable device. A core-only build can disable both Metal targets with `-DFRAMEGEN_BUILD_METAL=OFF -DFRAMEGEN_BUILD_TEST_METAL=OFF`. Benchmark and analyzer targets are reserved but not implemented; `FRAMEGEN_BUILD_TOOLS=ON` is unavailable until those targets land.

## Current scope

The bootstrap establishes project structure, a GPU-texture public API, tests, and a placeholder Metal blend path. Neural network implementation, model selection, renderer integration, and performance claims remain out of scope until the corresponding roadmap work is explicitly started. RIFE, Core ML, and MetalFX are research/evaluation options only, not selected dependencies or promised backends.

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) and [PROVENANCE.md](PROVENANCE.md) before proposing dependencies, model assets, or implementation material. In particular, proprietary LSFG implementation material must not be incorporated.
