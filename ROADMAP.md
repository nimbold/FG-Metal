# Roadmap

This roadmap is ordered by prerequisites. Dates and performance targets are intentionally omitted until there is a measured baseline. Completion of a phase does not authorize starting a later phase automatically.

## Current status — 2026-10-08

Framegen remains an experimental library with a draft C ABI and no supported adapter for an unmodified game. The Metal preview and synthetic evaluation tools work, but they do not establish in-game quality or physical display cadence.

- DXMT Step 10B.6.1: **STOP** the current CAMetalDisplayLink architecture; matched windowed and fullscreen runs failed presentation health. RIFE is not cleared. See the [report](experiments/dxmt_framegen/evidence/step10b6_1/report.md).
- DXVK-MacOS Step 11C: **PARTIAL PASS** for a low-rate windowed experiment; user-observed magenta/cyan flashes remain a defect, and doubled cadence plus lifecycle coverage are incomplete. See the [report](experiments/dxvk_macos_step11c/REPORT.md).
- Native MoltenVK Step 11D.3-A: **CASE A3 — INCONCLUSIVE**. x86_64 is the architecture-matched primary control, but the leased Xcode build failed before compilation. Gate 0 is current and passing; no native executable, loader probe, or graphics trace exists. See the [A1 report](experiments/dxvk_macos_step11d3a/STEP-11D3-A1-REPORT.md).

## Roadmap checklist

- [x] Implement the renderer-independent GPU texture contract, Metal placeholder backend, preview host, and core tests.
- [x] Add synthetic image/temporal metrics and a bounded presentation scheduler with simulation coverage.
- [x] Run cooperative GPTK/D3DMetal and public DXGI feasibility experiments; document the partial and blocked routes.
- [x] Run the current DXMT and DXVK-MacOS feasibility investigations to their evidence-backed stop points.
- [ ] Resolve renderer access and presentation questions before selecting an integration strategy.
- [ ] Validate the public API, resource lifetime, and synchronization contract with an independent adapter consumer.
- [ ] Expand quality evaluation with documented, redistribution-cleared material and supported-hardware measurements.
- [ ] Select an inference backend after quality, latency, licensing, and redistribution review.
- [ ] Pass lifecycle, failure, sustained-soak, and clean-machine packaging checks before declaring any adapter supported.

The detailed phases below describe prerequisites and boundaries. Step 11D.3-B and RIFE implementation remain gated; the current A3 result does not authorize either.

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
