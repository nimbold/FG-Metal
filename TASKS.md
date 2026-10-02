# Tasks

Task states: **Done** means the repository contains the work and it has passed the stated check; **In progress** means implementation is underway; **Open** means it remains future work. Verification claims belong with their evidence, not merely with a task marked done.

## Bootstrap step

- [x] **Done** — Create the initial project layout and CMake/Ninja targets using C++23 and the newest available macOS SDK/toolchain.
- [x] **Done** — Implement a small opaque GPU-resource public API with two ordered inputs and a returned output texture.
- [x] **Done** — Implement the Metal placeholder blend with GPU-only texture processing and explicit synchronization/lifetime behavior.
- [x] **Done** — Implement the `test-metal` host: create a Metal device and two input textures, submit them through the API, and display the returned third texture.
- [x] **Done** — Add focused API/core tests that do not require image readback in the live path.
- [x] **Done** — Complete `CONTRIBUTING.md`, `PROVENANCE.md`, and `LICENSE`; prohibit proprietary LSFG implementation material and track third-party/model/data/runtime rights separately.
- [x] **Done** — Review every initial diff for renderer coupling, hidden CPU copies, boundary compliance, and licensing/provenance issues.
- [x] **Done** — Build from a clean checkout; run all tests and the Metal host; record exact toolchain versions and evidence.
- [x] **Done** — Confirm the core has no Highball, Wine, LSFG, or renderer-specific dependency, and inspect the submission path for CPU image transfers.

### Bootstrap verification record

- Clean verification used a temporary Git source snapshot and clone outside the project directory; the cloned worktree was clean before configuration.
- Default macOS configure/build succeeded, CTest passed (1/1), and the native Metal test host launched and displayed `FrameGen Metal blend preview`.
- A separate core-only configure/build and CTest run also passed (1/1) with Metal disabled.
- Toolchain: macOS 27.0.1 (build 26A434), arm64, Apple Command Line Tools at `/Library/Developer/CommandLineTools`, macOS SDK 27.0, AppleClang 21.0.0.21000334, CMake 4.4.3, Ninja 1.13.2.
- Full Xcode, `xcodebuild`, the standalone `metal` compiler, and GPTK were not used. Runtime shader compilation through Metal succeeded. No neural backend or renderer adapter was started.

## Benchmark step

- [x] **Done** — Define a versioned high-rate corpus format, lower-rate source selection, ground-truth targets, arbitrary analytic-provider timestamps, and independently overlapping HUD/text/scene/occlusion masks.
- [x] **Done** — Implement image, temporal, flicker, sparse-pixel, and edge metrics with per-region, per-target, and strict critical-ROI pixel regression gates; keep PSNR diagnostic only.
- [x] **Done** — Add a rights-safe synthetic sequence, offline Metal placeholder runner, machine-readable JSON, human-readable Markdown, and compatible-run comparison.
- [x] **Done** — Capture a placeholder-backend baseline and document that its output and offline throughput are not interpolation-quality or game-performance claims.
- [ ] **Open** — Expand corpus coverage for racing, scene-cut and loading transitions, and richer subtitle cases; add real footage only when redistribution rights are documented. Fast camera rotation and moving menus are now in the synthetic fixture.

### Benchmark verification record

- The Python analyzer and metric tests require only the standard library. The Metal runner is an optional macOS-only build target.
- The checked-in fixture is project-authored synthetic content. No copyrighted game capture, third-party metric code, model, or weights are included.
- Results measure a serial offline backend path and must not be represented as renderer-integrated FPS, presentation pacing, or observed dropped frames.

## Temporal quality step

- [x] **Done** — Add an opt-in, model-independent post-interpolation confidence controller with endpoint/source fallback policies, temporal history isolation, GPU scene-cut detection, debug views, and reset handling.
- [x] **Done** — Benchmark raw RIFE, automatic HUD, each temporal policy, and each HUD-plus-temporal combination on the fast-rotation synthetic fixture; inspect the generated contact sheet and document both gains and regressions.
- [x] **Done** — Record the limited measured improvements and keep the controller disabled by default because strict comparisons still report regressions.

### Temporal quality verification record

- See [temporal quality results](docs/TEMPORAL_QUALITY.md) and the linked raw JSON/Markdown run and comparison artifacts.
- Apple M3, Practical-RIFE v4.26 QUALITY, 128×72; five targets, three warmups and 100 measured iterations per target for each of eight configurations. All six strict policy comparisons report `REGRESSION`.
- Endpoint fallback reduced HUD-region flicker p95 23.3% and weapon-sight flicker p95 16.6% versus raw RIFE, but whole-frame flicker p95 rose 3.7% and minimum thin-edge recall fell 0.7055→0.6090. HUD-plus-temporal was worse than HUD alone for HUD flicker. These results justify keeping the feature experimental and off by default, not a general quality claim.
- GPU same-histogram cut, no-false-positive rotation, real-frame cut/reset fallback, duplicate-target stability, and stale-request isolation were probed. Final verification: CMake build succeeded; CTest passed 4/4, including Metal C API smoke on Apple M3; benchmark metric tests passed 43/43.

## Presentation pacing step

- [x] **Done** — Add a model-independent 2x presentation scheduler with configurable interpolation fraction, QUALITY/BALANCED latency policies, bounded work/ready state, real-source fallback, and deadline-miss cooldown/recovery.
- [x] **Done** — Keep source production, backend generation, and display scheduling on separate paths; expose bounded event traces and queue/latency diagnostics.
- [x] **Done** — Use `CAMetalDisplayLink` target timing and `MTLDrawable` presentation feedback in the standalone Metal host; keep QuartzCore types out of the reusable scheduler.
- [x] **Done** — Add a standalone simulation host for 30→60, 40→80, and 60→120 under stable/jittery timing, long source frames, a slow generation backend, display timing changes, and late render-submit callbacks.
- [x] **Done** — Verify artificially slow generation drops generated work, continues real-frame presentation, recovers after cooldown, and keeps queue depth bounded.

### Presentation pacing verification record

- `framegen-pacing-host` completed all 18 cadence/disturbance scenarios plus interpolation-fraction, output-retirement, and concurrent source/presentation checks.
- The deliberately slow backend recorded deadline misses and generated-frame drops in all three cadence pairs, continued presenting real frames, attempted recovery after cooldown, and never exceeded queue depth 1.
- The host is a deterministic scheduler simulation. It does not establish physical panel cadence, renderer integration, neural interpolation performance, or zero added latency.

## GPTK 4 / D3DMetal feasibility (Step 8)

- [x] **Done** — Audit public Metal, QuartzCore, GPTK, and Wine/D3D12 boundaries; check the local tool/package inventory; document the transparent-adapter decision without inspecting private D3DMetal material.
- [ ] **Blocked** — Demonstrate a transparent Metal-texture adapter for an unmodified GPTK 4 D3DMetal game and run the 30-minute lifecycle soak. Step 8D partially validates a separate public DXGI/D3D12 proxy on a controlled app, but does not expose D3DMetal's final Metal texture or establish complete third-party-game interception. The missing public Metal per-swapchain hook and soak remain unresolved.

### Step 8 evidence record

- See [GPTK 4 / D3DMetal synthetic-presentation feasibility](docs/feasibility/gptk4-d3dmetal-synthetic-presentation.md) and decision D-013 in `DECISIONS.md`.
- The original Step 8 inventory found a standalone GPTK 3.0 package and no verified GPTK 4 runtime. Step 8B’s `com.gamemac.www` runner was later identified as GameHub/Proton rather than Highball; its package attribution was incorrect. Step 8C verifies the actual Highball 0.10.1 engine and D3DMetal 4.0b2 runtime in-process.
- Step 8C demonstrated cooperative D3D12 backbuffer access and credible 30→60 display delivery using synthetic GPU patterns. It did not run a transparent game adapter, RIFE, or the 30-minute soak. Drawable starvation and long-term memory growth remain **UNVERIFIED**; 60-second timing, errors, and memory observations are recorded in the Step 8C report.
- GPTK 4 and the app-owned MetalFX integration are publicly documented. The materials reviewed did not identify a supported per-swapchain frame and presentation hook for Framegen's current Metal adapter contract. The separate cooperative D3D12 experiment is recorded below and does not change this Metal-path decision.

## Public DXGI/D3D12 feasibility (Step 8B)

- [x] **Done** — Build a cooperative Win32 test app and explicit in-process D3D12 adapter using documented DXGI/D3D12 interfaces; inspect Wine module-load traces and GPTK package metadata only.
- [x] **Done** — Experimentally retrieve D3D12 backbuffers, copy to adapter-owned D3D12 resources, issue GPU-only work with barriers/fences, submit interleaved synthetic G Present requests at paced 30→60 and 60→120 rates, and exercise one resize/fullscreen sequence.
- [x] **Done** — Validate the cooperative path with Highball’s actual GPTK 4/D3DMetal runtime and public display-side presentation evidence; do not proceed to a D3D12 RIFE backend on this result alone.

### Step 8B evidence record

- See [the corrected Step 8B report](docs/feasibility/dxgi-d3d12-gptk4-step8b.md), [Step 8C report](docs/feasibility/dxgi-d3d12-gptk4-step8c.md), [Step 8D report](docs/feasibility/dxgi-d3d12-gptk4-step8d.md), [probe source and run instructions](experiments/dxgi_public_probe/README.md), and decisions D-014/D-015.
- In Wine Proton 10.0 (x86-64), the cooperative probe requested 30→60 with 150 source frames, 149 G frames, and 299 successful Present/Present1 calls; 60→120 requested 300 source frames, 299 G frames, and 599 successful calls. `GetLastPresentCount` matched those call counts. These are submission counts, not proof of display delivery.
- In the final build, QPC call-gap median/p95/max were 16.673/19.209/26.164 ms for the 60 Hz request and 8.334/15.190/25.583 ms for the 120 Hz request. Wine logged its D3D12 `GetFrameStatistics` implementation as a stub and returned `E_NOTIMPL`; the available display is 60 Hz.
- Step 8B itself used the GameHub/Proton Wine builtins, had `GetFrameStatistics=E_NOTIMPL`, and did not validate Highball. Step 8C supersedes its runtime recommendation: on Highball GPTK 4, 1,799 G frames were presented within 3,599 source/G presentations, with one-image/one-refresh DXGI counter increments and Apple HUD present-interval timing.
- The Step 8C short lifecycle sequence resized, switched fullscreen/windowed, and moved focus to a helper window and back; all checked operations returned `S_OK`. Multiple swapchains, OS-level alt-tab, variable source FPS, `ResizeBuffers1`, long-term memory behavior, and the 30-minute soak remain **UNVERIFIED**.

## Highball GPTK 4 cooperative validation (Step 8C)

- [x] **Done** — Reproduce Highball’s public engine configuration in a disposable prefix and verify GPTK 4.0 beta 2 / D3DMetal 4.0b2 loaded in-process.
- [x] **Done** — Demonstrate GPU-only D3D12 backbuffer operations and synthetic 30→60 G presentations with DXGI monitor counters plus Apple Metal HUD timing.
- [x] **Done** — Exercise a short resize/fullscreen/focus lifecycle and record the limits; 60→120 was correctly not run on the available 60-Hz-only display.
- See [the Step 8C report](docs/feasibility/dxgi-d3d12-gptk4-step8c.md) and preserved [run evidence](experiments/dxgi_public_probe/evidence/).
- **Decision at Step 8C:** PASS for the cooperative DXGI/D3D12 path. Step 8D was subsequently run and returned PARTIAL PASS for the controlled direct-factory transparent path. Do not implement RIFE or a product adapter based on either result alone.

## Transparent DXGI attachment (Steps 8D–8D.1)

- [x] **Done** — Build an app-local `dxgi.dll` proxy and typed public COM wrappers. On the tested Highball/Wine engine, provider forwarding works through public Win32 loader APIs; this remains engine-specific.
- [x] **Done** — Wrap the tested factory/adapter/output/swapchain identity graph, exercise all three factory creation exports, discover the D3D12 queue/device, and copy backbuffers GPU-only.
- [x] **Done** — Run A–E pass-through/copy/synthetic progression cases, fourteen GPU-work failpoints, current-source resize/fullscreen/`ResizeBuffers1`/focus/recreation, two swapchains, and a three-trial-per-condition pass-through overhead comparison.
- [x] **Done** — Test a public DirectComposition bootstrap; Device3 and Device2 both return `E_NOTIMPL` on the selected Highball runtime. Test and reject the local Deep Rock candidate without launching it because its tree contains a RUNE/Steam-emulator layer.
- **Unresolved limits:** Successful unknown `QueryInterface` interfaces and the decode-swapchain route may escape raw; provider-load/export failure is not fail-open; no provenance-clean third-party app was tested. Same-chain G passes only when the app queries its index each frame and breaks four local-index/fence/allocator policies.
- **Decision:** PARTIAL PASS. Stop this Step 8D experiment on the current runtime; do not modify Highball or implement RIFE. A targeted follow-up is not justified without a newly supported public composition route or another engine that exposes one. Use the planned DXMT reference integration as the next renderer-integration investigation. See [Step 8D.1 result](docs/feasibility/dxgi-d3d12-gptk4-step8d1.md), [earlier Step 8D result](docs/feasibility/dxgi-d3d12-gptk4-step8d.md), [prototype and run instructions](experiments/dxgi_public_attachment/README.md), and decision D-015.

## Follow-on work (not authorized by completion of bootstrap)

- [ ] **Open** — Resolve the API and synchronization decisions listed in `DECISIONS.md`.
- [ ] **Open** — Identify a broader set of redistribution-cleared visual sequences and record their provenance and rights.
- [ ] **Open** — Evaluate algorithm/backend candidates without treating exploratory folders as approved dependencies.
- [ ] **Open** — Evaluate renderer adapters independently, starting only when authorized; use documented/public interfaces.
- [ ] **Open** — Consider a Highball consumer adapter only after the public API and core are stable.
