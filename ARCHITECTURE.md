# Architecture

## Goals and constraints

Framegen is a reusable, renderer-agnostic library whose live path consumes and produces GPU textures. It aims to improve image quality first, then temporal stability, latency, HUD/text handling, frame pacing, and eventually compatibility with renderer stacks. These are priorities, not claims that the current placeholder achieves them.

The core has no dependency on Highball, Wine, D3DMetal, DXMT, or DXVK. Integrations for those environments live in adapters and communicate through the public GPU-resource contract. No screen-capture architecture is permitted. The implementation is independently designed and must not use or reproduce private Lossless Scaling/LSFG interfaces or behavior.

## Layering

```text
Renderer / test application
        │
        ├── renderer adapter (future: D3DMetal, DXMT, DXVK)
        │       │ imports or wraps renderer-owned GPU textures
        ▼
include/framegen  ── public, opaque GPU-resource API
        │
src/core          ── validation, submission contract, lifecycle
        ├── src/pacing      ── timing and presentation policy
        ├── src/quality     ── quality policy and future controls
        ├── src/diagnostics ── metadata and performance diagnostics
        └── backend contract
                ├── src/metal       ── Metal resource and execution path
                └── backends/*      ── separately evaluated algorithms/runtime adapters
```

The core deals in opaque GPU resource references and explicit metadata such as dimensions, format, usage, frame timing, and synchronization state. Metal types and Apple framework imports belong in the Metal layer or a Metal-specific API header. This keeps the general contract independent of renderer and operating-system integration details while allowing the macOS path to process `MTLTexture` resources directly.

The backend boundary owns processing-specific GPU work. Core code validates requests, manages the public session/submission contract, and coordinates timing/diagnostics without assuming a particular interpolation algorithm. The Metal implementation may use Metal-specific APIs and shader code. Backend candidates under `backends/` are not selected merely by reserving a directory: RIFE, Core ML, and MetalFX require separate technical, quality, licensing, and redistribution evaluation before adoption.

## Resource flow and ownership

1. The producer renders or otherwise supplies two temporally ordered source textures and their timing/metadata.
2. A caller or adapter wraps/imports those existing GPU resources without routing pixels through CPU memory.
3. The API validates device/backend compatibility, texture descriptors, ordering, and synchronization preconditions. Before submission, the caller must ensure producer writes to both inputs are complete or provide GPU dependencies for every outstanding write; omitting a dependency asserts the inputs are ready for backend reads.
4. The selected backend schedules GPU work and returns a handle to the generated output texture plus the completion/presentation synchronization information required by the caller.
5. The consumer waits or schedules presentation using that synchronization contract; it retains resource ownership according to documented handle lifetime rules.

The API must make texture lifetime, queue/command-buffer coordination, resource ownership, and completion explicit. It must not hide an implicit GPU-to-CPU image transfer. CPU-side descriptors and timing values are metadata, not image data. CPU readback may be used by an isolated offline test/analyzer if specifically needed, but it is never part of the live frame-generation path and must not be used to conceal transfer costs in measurements.

The bootstrap implementation uses a simple GPU blend only to prove the resource contract. The Metal test host creates the Metal device and test input textures; the API yields a third texture for display. The host displays it through a Metal-backed view. The blend is not an interpolation-quality baseline and does not establish temporal quality or latency results.

## Public API shape

The public contract exposes a C++23 façade around opaque device and texture handles rather than CPU pixel buffers. The core-facing types are backend-neutral. The Metal interop header is Objective-C++-only and wraps/unpacks native `MTLTexture` objects; renderer adapters provide color-space and alpha semantics that are not stored on a Metal texture. A C ABI can be considered later if stable binary/plugin compatibility across toolchains becomes a requirement.

The implemented request carries two textures, source sequence numbers, nanosecond timestamps, a producer-defined non-zero clock-domain token, an interpolation fraction, a history-reset flag, and GPU dependency points. The core validates matching backend/device and texture metadata, a shared clock domain, and increasing sequence/timestamps unless history is reset. For each input, callers provide a GPU dependency for every outstanding producer write; no dependency means the input is already ready for backend reads. A backend returns a distinct output texture and a completion point. `TextureResource::resource_identity()` lets adapters identify distinct wrappers of the same underlying resource.

The Metal `Device` owns the Metal command queue and backend. Its `wrap_texture` method accepts renderer-supplied color-space and alpha metadata; wrapping does not synchronize producer writes. `EventPoint` wraps an `MTLSharedEvent` and value for producer dependencies; `event_point()` extracts the output event/value so a consumer queue can wait before sampling the generated texture. The core forwards generic GPU sync points and never blocks on them. CPU `wait()` remains an explicit test/tool convenience. The current Metal host uses `MTLCommandQueue`, `MTLSharedEvent`, and `MTKView`; Metal 4 command queues remain a future evaluation item.

## Synchronization and pacing

Frame generation is on the critical rendering/presentation path, so submissions must avoid unnecessary CPU waits and queue round trips. Integrations should be able to express producer completion and consumer dependencies with GPU-native synchronization. A blocking CPU completion wait can remain a convenience for tests and tools, but cannot be the only way for a renderer to sequence generated output. The library must document which component owns command queues/buffers and where synchronization is inserted. Pacing policy is isolated under `src/pacing/` and cannot assume a particular renderer's swapchain or presentation hooks.

Metal 4 and modern QuartzCore presentation/synchronization facilities are preferred when supported by the active SDK and device. The placeholder currently uses shared-event synchronization and an `MTKView` presentation path; it does not yet use Metal 4 command queues. Avoid old-SDK compatibility scaffolding unless it is effectively free. Specific APIs are chosen after checking the installed SDK/toolchain; no older API requirement is implied by this architecture.

## Adapter boundaries

- `adapters/test-metal/` is a standalone validation host, not part of the reusable core.
- `adapters/d3dmetal/`, `adapters/dxmt/`, and `adapters/dxvk/` are reserved for future renderer adapters. They are not implemented in the bootstrap step.
- Future D3DMetal work must use documented/public Metal, QuartzCore, Wine, GPTK, or renderer integration mechanisms. Do not reverse engineer D3DMetal.
- Adapters translate renderer resource handles, timing, and synchronization to the library contract. They do not move core algorithms into renderer-specific modules.
- No adapter may introduce screen capture as the live input path.

## Initial repository map

```text
framegen/
  CMakeLists.txt
  include/framegen/
  src/
    core/
    metal/
    pacing/
    quality/
    diagnostics/
  backends/
    rife/       # exploratory placeholder; no implementation commitment
    coreml/     # exploratory placeholder; no implementation commitment
    metalfx/    # exploratory placeholder; no implementation commitment
  adapters/
    test-metal/
    d3dmetal/   # future only
    dxmt/       # future only
    dxvk/       # future only
  tools/
    benchmark/
    analyzer/
  tests/
  models/provenance/
  docs/
```

Documentation and repository metadata live at the project root. Reserved directories do not imply that their implementation or dependencies have been approved.

## Quality and measurement boundaries

Quality evaluation must distinguish generated-image quality, temporal stability, HUD/text preservation, added latency, and frame pacing. Benchmarks should identify the device, SDK/toolchain, input format, resolution, synchronization mode, and whether timing includes presentation. Metrics and visual samples must identify algorithm/backend versions and asset provenance. The placeholder blend cannot be used to support claims about a future neural or system-provided backend.

## Dependency and provenance boundary

Project-authored code is Apache-2.0 unless a documented exception is approved. Third-party code, model implementations, weights, datasets, and runtime dependencies are tracked independently with license and redistribution-rights status in `PROVENANCE.md`. Do not add model assets or third-party code without recording the source and terms. No proprietary LSFG material may be used as code, model, interface, test oracle, or implementation specification.
