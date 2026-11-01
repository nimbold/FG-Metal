# Roadmap

This roadmap is ordered by prerequisites. Dates and performance targets are intentionally omitted until there is a measured baseline. Completion of a phase does not authorize starting a later phase automatically.

## Current status — 2026-10-15

Framegen remains an experimental library with a draft C ABI and no supported adapter for an unmodified game. The Metal preview and synthetic evaluation tools work, but they do not establish in-game quality or physical display cadence.

- DXMT Step 10B.6.1: **STOP** the current CAMetalDisplayLink architecture; matched windowed and fullscreen runs failed presentation health. RIFE is not cleared. See the [report](experiments/dxmt_framegen/evidence/step10b6_1/report.md).
- DXVK-MacOS Step 11C.1R2: **FULL PASS** for its WSI cadence/lifecycle gates. Step 11D.1 accepted the GPU-only single-source Vulkan→Metal→Vulkan transport. Step 11E is **NOT READY**: integrated AppFrame/backbuffer identity, cross-frame A/B ownership, two-source reset rejection, and release-ledger closure remain unproven; the rebuildable R5 integration source is absent from this checkout. See the [Step 11 closeout](docs/reports/step11-closeout/reconciliation.md).
- Native MoltenVK Step 11D.3-A: **CASE A2 — DIVERGENT / HEALTHY NATIVE BASELINE**. Thin x86_64 MoltenVK under Rosetta achieves 59.99–60.01 FPS with >99.79% of nextDrawable calls locked in 14–18 ms and 0 waits >= 50 ms. The presentation starvation of STEP 11D.2 does not reproduce in native control. See the [Step 11D.3-A3 report](experiments/dxvk_macos_step11d3a/STEP-11D3-A3-REPORT.md).
- Bare Wine Vulkan Step 11D.3-B: **CASE B1**; BR9R scientific status remains **N3 — INCONCLUSIVE**, while the engineering investigation is **CLOSED / DEFERRED BY DIMINISHING RETURNS**. H6-A/H6-B and fresh lifecycle authorization passed; replay 1 stopped before launch at storage admission, preserving R5/T8 and replay-2 ineligibility. Further Finder/Native work and Step 11D.3-C are deferred. See the [BR9R2 report](experiments/dxvk_macos_step11d3b/STEP-11D3-BR9R2-REPORT.md).

## Roadmap checklist

- [x] Implement the renderer-independent GPU texture contract, Metal placeholder backend, preview host, and core tests.
- [x] Add synthetic image/temporal metrics and a bounded presentation scheduler with simulation coverage.
- [x] Run cooperative GPTK/D3DMetal and public DXGI feasibility experiments; document the partial and blocked routes.
- [x] Run the current DXMT and DXVK-MacOS feasibility investigations to their evidence-backed stop points; BR9R engineering work is closed while its scientific classification remains inconclusive.
- [ ] Complete essential Step 11 A/B source identity, cross-frame ownership, reset, and bounded-release proof; run Step 11E before selecting an integration strategy.
- [ ] Validate the public API, resource lifetime, and synchronization contract with an independent adapter consumer.
- [ ] Expand quality evaluation with documented, redistribution-cleared material and supported-hardware measurements.
- [ ] Select an inference backend after quality, latency, licensing, and redistribution review.
- [ ] Pass lifecycle, failure, sustained-soak, and clean-machine packaging checks before declaring any adapter supported.

The [Step 11 closeout record](docs/reports/step11-closeout/reconciliation.md) is authoritative for current Step 11 status. BR9R remains scientifically inconclusive and engineering-deferred; Step 11D.3-C is deferred, not an 11E prerequisite. RIFE implementation remains gated. The detailed phases below describe later project prerequisites and boundaries.

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
