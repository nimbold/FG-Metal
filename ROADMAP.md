# Roadmap

This roadmap is ordered by prerequisites. Dates and performance targets are intentionally omitted until there is a measured baseline. Completion of a phase does not authorize starting a later phase automatically.

## Phase 0 — Bootstrap and boundaries

- Establish the repository, Apache-2.0 project-code licensing, contribution rules, and separate provenance records.
- Define an opaque GPU-resource submission/output contract.
- Build a minimal macOS Metal host that submits two textures, executes a GPU-only placeholder blend, and displays the returned output texture.
- Verify a clean checkout build, tests, host launch, and absence of CPU image readback in the live path.
- Record toolchain versions and any unavailable verification in the task report.

## Phase 1 — Contract and runtime foundations

- Finalize texture formats, device identity, ownership/lifetime, synchronization, command-buffer/queue ownership, and error semantics.
- Add deterministic API/descriptor validation and lifecycle tests.
- Establish GPU timing and diagnostics that measure submission and completion without adding hidden readbacks or avoidable waits.
- Define a reproducible visual and temporal quality evaluation corpus with independently documented sources and rights.

## Phase 2 — Quality research and backend evaluation

- Compare suitable interpolation approaches against the project's quality, temporal stability, latency, and HUD/text priorities.
- Evaluate RIFE-family implementations, Core ML deployment, and MetalFX only as candidates; document feasibility, quality, licensing, runtime, and redistribution findings before selecting any.
- Do not begin neural implementation or acquire/model weights until there is an explicit backend decision and provenance review.
- Measure on supported Apple hardware and report limitations across motion types, UI/text overlays, and frame pacing.

## Phase 3 — Renderer adapters

- Design an adapter contract and isolated validation fixtures.
- Evaluate D3DMetal/GPTK 4+, DXMT, and DXVK-MacOS integration feasibility one at a time through documented/public interfaces.
- Keep renderer-specific texture import, format translation, and synchronization in adapters. Preserve the core's independence.
- Do not reverse engineer D3DMetal or use screen capture.

## Phase 4 — Stabilization and later consumer integration

- Harden API/versioning, failure recovery, pacing, observability, and supported-device documentation.
- Validate clean-machine packaging and runtime dependency distribution rights.
- Consider Highball integration only through the clean public API, as a separate consumer of the library.

## Out of scope for Phase 0

- Neural-network implementation or weights.
- Selecting RIFE, Core ML, or MetalFX as a dependency.
- D3DMetal, DXMT, or DXVK integration.
- Highball integration.
- Screen capture, desktop duplication, or CPU image-buffer processing.
- Claims of production-quality frame interpolation.
