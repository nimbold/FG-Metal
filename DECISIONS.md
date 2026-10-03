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

### D-013 — GPTK 4 / D3DMetal external-adapter feasibility

**Status:** Blocked for a transparent adapter using Framegen's Metal texture contract<br>
**Decision:** Keep the transparent D3DMetal adapter blocked until a public per-swapchain resource-access and presentation-control mechanism is identified and validated. Do not inspect D3DMetal internals or substitute screen capture/CPU readback.<br>
**Rationale:** Public Metal sharing requires the producer to create and export a Metal resource. In the public material reviewed, no supported mapping from a D3DMetal swapchain to its final `MTLTexture`, nor a mechanism to defer/replace GPTK presents, was identified. AppKit can expose in-process windows, layers, and lifecycle notifications, but their mapping to the final D3DMetal frame and presentation owner was not tested. A cooperative D3D12-only application or backend is a separate design and does not validate this Metal contract.<br>
**Consequence:** The cooperative D3D12 route now has a GPTK 4 / D3DMetal 4.0b2 30→60 result, but that does not expose a D3DMetal swapchain as a Metal texture or grant control over an unmodified game. The transparent Metal adapter and 30-minute soak remain blocked/unrun. See [the Step 8 feasibility record](docs/feasibility/gptk4-d3dmetal-synthetic-presentation.md) and [Step 8C](docs/feasibility/dxgi-d3d12-gptk4-step8c.md).

### D-014 — Public DXGI/D3D12 adapter feasibility (Steps 8B–8C)

**Status:** Cooperative path passes on Highball GPTK 4; Step 8D later reached PARTIAL PASS on a controlled direct-factory path<br>
**Decision:** Keep D3D12 resources separate from Metal resources. Do not create a D3D12 inference backend or product adapter from this result. A separately scoped public DXGI wrapping experiment is justified.<br>
**Rationale:** The cooperative harness ran through Highball’s public engine configuration with D3DMetal 4.0b2 loaded, retrieved `ID3D12Resource` backbuffers, submitted GPU-only work, and produced 1,799 interleaved G presentations in a 30→60 run. `DXGI_FRAME_STATISTICS` monitor/refresh counters advanced after each output, and Apple’s Metal HUD reported present intervals with a 16.67 ms median/p95/p99 on a 60-Hz-only display. This is credible display evidence for a cooperative app, not proof of a transparent adapter or pixel-level G identity. Timing outliers, 60→120 unavailability, and the unrun soak are recorded in Step 8C.<br>
**Consequence:** The GPTK 4 cooperative runtime/display-statistics follow-up is complete. Step 8D later tested transparent DXGI attachment and produced a partial pass for a controlled direct-factory route; it did not establish general interception or a product adapter. Keep RIFE disconnected and preserve the public-interface boundary. See [Step 8B](docs/feasibility/dxgi-d3d12-gptk4-step8b.md), [Step 8C](docs/feasibility/dxgi-d3d12-gptk4-step8c.md), and [Step 8D](docs/feasibility/dxgi-d3d12-gptk4-step8d.md).

### D-015 — Transparent DXGI attachment feasibility (Step 8D)

**Status:** Partial pass for tested public DXGI paths; general COM-transparent attachment and safe same-chain extra presentation remain unresolved<br>
**Decision:** Keep the app-local proxy as a test-only experiment. Do not treat it as a product adapter, change Highball, or begin a D3D12 RIFE backend. Step 8D.1 closed the reproduced adapter/parent identity route on the tested provider, exercised GPU-work fail-open cases, and tested a separate DirectComposition bootstrap; another Step 8D iteration on this runtime is not justified unless a supported public composition route becomes available.<br>
**Rationale:** On Highball 0.10.1's Sikarugir Wine 10 engine, the controlled unmodified DXGI/D3D12 app loads an app-local proxy and reaches a separate system32 provider through public Win32 loading APIs. Typed Factory7/Adapter4/Output6/SwapChain4 wrappers preserve tested identity routes, and D3D12 backbuffer copies succeed GPU-only. The A–E matrix passes baseline, proxy pass-through, and copy cases; synthetic G passes only when the app queries `GetCurrentBackBufferIndex` every frame and breaks four local-index/fence/allocator policies because the extra inner Present advances that index. Both public DCompositionCreateDevice3 and Device2 calls return `E_NOTIMPL` on this engine. Fourteen injected GPU-work failures preserve 15 normal source Presents, but provider startup is fail-closed, unknown QI/decode-swapchain routes remain raw, and no provenance-clean third-party app was tested. The local Deep Rock candidate is a RUNE/Steam-emulator tree and was not launched. Six independent GPT-6 Luna Max audits found no private-interface boundary violation and confirmed these limits. See [Step 8D.1](docs/feasibility/dxgi-d3d12-gptk4-step8d1.md), [Step 8D](docs/feasibility/dxgi-d3d12-gptk4-step8d.md), and retained [runtime evidence](experiments/dxgi_public_attachment/evidence/).
**Consequence:** The result is **PARTIAL PASS**, not proof of general application transparency or safe generated presentation. A follow-up code audit skipped GPU work for `DXGI_PRESENT_TEST`, moved provider Present/resize calls outside the wrapper state lock, tightened `ResizeBuffers1` node/queue matching, and restricted same-chain G to an explicit controlled-test build. Keep D-013's Metal-texture adapter blocked. Stop this Step 8D experiment here; do not implement RIFE or modify Highball. Use the separately planned DXMT reference integration as the next renderer-integration investigation.

### D-016 — DXMT Metal presentation integration (Step 10)

**Status:** Step 10A PASS; Steps 10B.3 and 10B.4 **PARTIAL PASS**; 15-minute soak complete for 10B.3 only<br>
**Decision:** Keep the local downstream DXMT presenter as a viable source-safe route. Continue cadence and feedback work. Do not implement RIFE. Keep the D3DMetal/DXGI experiments as historical negative evidence; this decision does not reopen them.<br>
**Rationale:** The pinned DXMT build uses a single layer owner and mandatory same-command-buffer SourceEscrow before ownership transfer. The Step 10B.3 soak retained all 27,000 accepted sources safely. Step 10B.4's final advance-B run retained all 1,796 accepted sources and delivered 60.045 callbacks/s, 59.726 submissions/s, and 57.079 positive feedback/s, with a 45.183 ms p95 positive interval. It reduced no-output ticks from 26 to 19, but 269 positive G timestamps followed B's display target and 56 of 1,638 positive G results lacked one or both positive endpoint timestamps. Among 152 B advances before B's scheduled source time, 125 positive B timestamps were early. The advance-B policy did not meet the cadence and feedback gate; the conditional 15-minute soak was skipped. See the [Step 10B.4 report](docs/feasibility/dxmt-framegen-step10b4.md), [Step 10B.3 report](docs/feasibility/dxmt-framegen-step10b3.md), [Step 10 report](docs/feasibility/dxmt-framegen-step10.md), and retained [Step 10B.4 evidence](experiments/dxmt_framegen/evidence/step10b4/).<br>
**Consequence:** Keep the downstream candidate local; do not modify Highball or prepare an upstream DXMT contribution. The conditional 15-minute soak was not run because final I did not meet the synthetic correctness gate. Cadence and public feedback interpretation remain unresolved, and this is not a physical-scanout claim. No RIFE implementation was made.

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
**Status:** The transparent D3DMetal route remains blocked. Step 10B.4 reached **PARTIAL PASS** with a DXMT-native presenter, safe accepted-source outcomes, balanced callback accounting, and near-60 submission cadence; positive drawable feedback and strict A/G/B timing remain unresolved.<br>
**Need:** Keep the DXGI/D3DMetal negative results in D-013–D-015. Continue DXMT cadence and feedback validation within the single-owner/source-safety architecture. Do not begin RIFE until the remaining timing and feedback limits have an evidence-backed disposition. Do not modify Highball or reverse engineer D3DMetal.

### O-006 — Quality datasets and redistribution

**Question:** Which test images/sequences can be used for internal evaluation and redistributed with the project?<br>
**Need:** Track source, dataset license, consent/rights, transformations, and redistribution permissions independently before adding assets.
