# Decisions

This log records decisions that constrain implementation. Pending items are not implicit approvals. When a choice is made, record its rationale, consequences, and evidence here.

## Accepted constraints

### D-001 — GPU textures are the live processing contract

**Status:** Accepted<br>
**Decision:** The public library processes opaque GPU resources, with two temporally ordered input textures and a GPU output texture. CPU image buffers and live CPU readback are not part of the API or frame-generation path.<br>
**Rationale:** The project is renderer-integrated and latency-sensitive; direct GPU resource flow is a foundational constraint.<br>
**Consequence:** Texture ownership, device identity, format/usage metadata, synchronization, and completion must be expressible through the API.

### D-002 — Core stays independent of renderers and host products

**Status:** Accepted<br>
**Decision:** The core does not depend on Highball, Wine, D3DMetal, DXMT, or DXVK. Renderer integrations are adapters outside the core.<br>
**Rationale:** A reusable frame-generation library must remain usable independently of any one renderer or host application.<br>
**Consequence:** Renderer-specific resource import and synchronization translation belong in adapter modules.

### D-003 — No desktop capture architecture

**Status:** Accepted<br>
**Decision:** No screen-capture or desktop-duplication path is built for live frame generation.<br>
**Rationale:** Inputs must come from renderer/GPU resources.<br>
**Consequence:** A missing renderer hook cannot be worked around by capturing the displayed screen.

### D-004 — Independent design; proprietary LSFG material excluded

**Status:** Accepted<br>
**Decision:** Do not use, copy, depend on, inspect, reconstruct, or imitate private interfaces, binaries, models, or reverse-engineered LSFG behavior, including old Highball LSFG integration as an implementation specification. Lossless Scaling may be referenced only for high-level product UX.<br>
**Rationale:** This project requires an independent implementation and provenance.<br>
**Consequence:** Contributions and test methodology must not encode proprietary implementation knowledge as an oracle or specification.

### D-005 — Bootstrap behavior is a GPU blend only

**Status:** Accepted<br>
**Decision:** The first output uses a simple Metal GPU blend to exercise input, output, and display plumbing. No neural implementation begins in the bootstrap step.<br>
**Rationale:** Validate architecture and data flow before selecting an algorithm.<br>
**Consequence:** The blend is not evidence of interpolation quality and must not be described as such.

### D-006 — Apple platform/toolchain direction

**Status:** Accepted<br>
**Decision:** Target the newest available macOS SDK and Apple toolchain, C++23, CMake + Ninja, and modern Metal APIs, preferring Metal 4 where applicable. Use Objective-C++ only for Apple framework interop.<br>
**Rationale:** The project targets cutting-edge Apple platforms and does not need broad old-SDK compatibility.<br>
**Consequence:** Actual versions and availability must be recorded from the development environment; feature use is capability-checked where needed.

### D-007 — Apache-2.0 for original project code

**Status:** Accepted<br>
**Decision:** Original project code is Apache-2.0 unless a concrete dependency requires a documented exception. Third-party source, models, weights, datasets, and runtime dependencies receive separate provenance and rights review.<br>
**Rationale:** Code licenses do not settle model, dataset, or binary redistribution rights.<br>
**Consequence:** See `PROVENANCE.md` for the required records.

### D-008 — Frame timing and history reset

**Status:** Accepted<br>
**Decision:** Each source frame carries a sequence number, signed nanosecond timestamp, and non-zero producer-defined clock-domain token. The two inputs in one submission must share a domain and, unless `reset_history` is set, have increasing sequence and timestamp values.<br>
**Rationale:** Temporal backends need frame order and elapsed time; renderer clocks may have different origins.<br>
**Consequence:** The domain token identifies both the clock and its epoch. A reset permits sequence/time discontinuity and tells a future temporal backend to discard history.

### D-009 — Backend-owned submission with GPU synchronization points

**Status:** Accepted<br>
**Decision:** The backend owns its command queue and encodes GPU dependencies supplied with a submission. Callers must supply a dependency for every outstanding producer write to either input; no dependency asserts that input is already ready for backend reads. It returns an opaque completion point; adapters can compose that point into consumer GPU work. CPU `wait()` is an explicit convenience and is not the renderer synchronization contract. The Metal adapter maps these points to `MTLSharedEvent` plus a value.<br>
**Rationale:** Renderer integration must order producer, frame-generation, and consumer work without a CPU wait or pixel readback.<br>
**Consequence:** The core sees only backend/device identity and generic synchronization objects. Metal-specific event import/export stays in `framegen/metal.hpp`. Callers must provide an event usable on the selected Metal device; `MTLSharedEvent.device == nil` is expected for shared events. The backend does not infer producer readiness from a texture wrapper or queue ownership.

### D-010 — Initial Metal texture scope

**Status:** Accepted<br>
**Decision:** The placeholder generator accepts matching, single-sample 2D `rgba8_unorm` textures. Imported Metal textures carry renderer-supplied color-space and alpha metadata; Metal texture objects do not encode those semantics.<br>
**Rationale:** A narrow validated format keeps the initial GPU blend simple while preserving the metadata contract needed for later quality work.<br>
**Consequence:** Other formats may be represented by the public descriptor and wrapped, but the placeholder backend rejects formats it does not process. Unknown color/alpha metadata is allowed at wrapping time and may be rejected by future algorithms.

### D-011 — Provisional renderer-independent C host ABI

**Status:** Accepted for Step 3; ABI remains provisional<br>
**Decision:** Expose the host lifecycle through a versioned C ABI with opaque context/ticket and GPU-resource handles. Keep C++ backend implementations and platform GPU types behind that boundary. Negotiate backend capabilities and supported formats before submission, separate interpolation sample time from desired presentation time/deadline, and report producer/consumer completion through nonblocking synchronization points. The current ABI is a draft, not a permanent freeze.<br>
**Rationale:** Renderer adapters need a stable language boundary across toolchains while the host contract still needs validation through a real placeholder backend.<br>
**Consequence:** Fixed-width versioned structures are used at the boundary. Embedded-by-value structures are frozen for ABI v1; future evolution must use top-level extensions or a new major ABI. D3D12 and Vulkan transport values remain reserved until resource-layout and queue-ownership import contracts exist.

### D-012 — Bounded model-independent presentation pacing

**Status:** Accepted for Step 7<br>
**Decision:** Keep presentation policy in a renderer/model-independent scheduler. Source production submits timing metadata and receives at most one generation request; the caller runs backend work on a separate worker. The initial schedule is 2x with one configurable interior interpolation sample per source pair, defaulting to 0.5. QUALITY permits more source-to-presentation headroom; BALANCED uses a shorter delay and a more conservative readiness cutoff. The scheduler retains at most one active or ready generated output, falls back to real source frames at a missed slot, and temporarily disables generation after repeated misses before allowing recovery probes. It does not predict or extrapolate frames.
**Rationale:** Deadline misses must not block source production or turn GPU work into an unbounded presentation queue. A model-independent policy allows the same tests and timing contract to serve different backends.
**Consequence:** Added latency is measured and reported, never described as zero. The scheduler's generation-readiness deadline is separate from the host's render-submit deadline and from the C ABI's later presentation expiry. QuartzCore/Metal timing values stay in the standalone host adapter; all times are translated into one monotonic clock domain before entering Framegen. QUALITY/BALANCED pacing policy does not select the backend's f32/f16 precision variant.

## Open decisions

### O-002 — Public API ABI and language boundary

**Status:** Resolved for the provisional Step 3 contract; long-term ABI stability remains open<br>
**Decision:** Use the provisional C host ABI in D-011 and keep the implementation SPI in C++.<br>
**Need:** Validate the draft through adapter consumers before declaring any ABI frozen.

### O-004 — Backend candidates

**Question:** Which interpolation implementation, if any, best satisfies quality, stability, latency, license, and redistribution requirements?<br>
**Need:** RIFE-family approaches, Core ML, and MetalFX are exploratory candidates only. No dependency or model is selected.

### O-005 — Renderer adapter feasibility and order

**Question:** Which public/documented integration route and renderer should be validated first?<br>
**Need:** Evaluate adapter feasibility separately for D3DMetal/GPTK 4+, DXMT, and DXVK-MacOS. D3DMetal must not be reverse engineered; no renderer integration begins in the bootstrap step.

### O-006 — Quality datasets and redistribution

**Question:** Which test images/sequences can be used for internal evaluation and redistributed with the project?<br>
**Need:** Track source, dataset license, consent/rights, transformations, and redistribution permissions independently before adding assets.
