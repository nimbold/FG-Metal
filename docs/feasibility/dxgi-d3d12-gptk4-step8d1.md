# Step 8D.1 — Transparent DXGI attachment and swapchain progression

- **Date:** 2026-10-02
- **Decision:** **PARTIAL PASS**
- **Runtime:** Highball 0.10.1, `x64-sikarugir10.0_6-r14`, Wine 10.0 (Sikarugir), macOS on Apple M3, 60 Hz display mode
- **Scope:** Public DXGI/D3D12/Win32 interfaces and controlled applications. No RIFE, HUD detection, D3DMetal inspection, game patching, screen capture, or CPU pixel readback.

This is the Step 8D.1 follow-up to [Step 8D](dxgi-d3d12-gptk4-step8d.md). It fixes the reproduced factory/adapter identity routes for the tested provider and validates GPU-work failure forwarding. It also proves that an extra Present on the application swapchain breaks common application-owned buffer progression. It does not establish a generally transparent application adapter.

## Result

An unmodified controlled D3D12 executable, with no Framegen import or call, loaded an app-local `dxgi.dll` proxy through the selected Highball/Wine setup. The proxy forwarded factory creation to the separate Wine DXGI provider, wrapped the tested factory/adapter/output/swapchain graph, found the public D3D12 command queue/device, observed `Present`/`Present1`, and copied backbuffers on-GPU. All baseline, pass-through, and copy-only A–E buffer-bookkeeping cases completed successfully.

The added magenta G request is not safe on the application's swapchain in general. Case A passes because it asks DXGI for the current buffer index each frame. Cases B–E use application-owned index and fence/allocator progression; all four fail after G advances the provider's index once more. In each, the application's next source frame is rendered against the wrong backbuffer or allocator state. The synthetic Present uses the inner provider object and does not recurse, but `DXGI_PRESENT_DO_NOT_SEQUENCE` does not prevent the tested provider from advancing `GetCurrentBackBufferIndex`.

The proxy now clears the tested `GetParent`/adapter-enumeration bypass, and the rebuilt lifecycle test also passes a wrapped non-null output into fullscreen transitions. The unknown-IID and decode-swapchain routes remain raw public-interface escapes. A DirectComposition probe tried the public Device3 and Device2 entry points; both returned `E_NOTIMPL` on this Highball runtime, so a separate composition surface was not established. No provenance-clean third-party app was available, so the real-app phase remains **NOT RUN**.

## 1. Architecture choice

| Candidate | Decision |
| --- | --- |
| App-local `dxgi.dll` proxy forwarding to the Wine/GPTK DXGI provider, plus typed COM wrappers | Selected for this experiment. It intercepts documented factory exports before the application creates a factory and wraps objects returned through the tested public methods. |
| Typed wrappers without a proxy at factory creation | Rejected as a complete attachment route. It has no supported bootstrap path for an application that never gives Framegen its factory/device/queue/swapchain. |
| Wine override or another documented loader mechanism without an app-local proxy | Not sufficient for transparent wrapping. `WINEDLLOVERRIDES` selects native/builtin loading; this run found no public Wine callback that inserts typed COM wrappers around objects created by the builtin provider. |
| Wine-private hooks, generic process injection, D3DMetal modification | Rejected by scope and boundary. None was used. |

The selected proxy and wrappers are test-only. This experiment does not provide a redistributable DXGI replacement, general game compatibility, or a Highball UI integration.

## 2. Provider forwarding and DLL configuration

The proxy resolves the provider with `LoadLibraryW(L"C:\\windows\\system32\\dxgi.dll")`, rejects a result whose module handle is its own module, and resolves exports with `GetProcAddress` ([LoadLibraryW](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryw), [GetProcAddress](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-getprocaddress)). On the tested Highball engine, the loader trace shows the app-local proxy and a different system32 Wine builtin provider in the same process. This is observed behavior for this engine/configuration, not a documented general Wine proxy-forwarding contract. Installing this proxy into `system32` would hit the recursion guard.

Exports are provided for `CreateDXGIFactory`, `CreateDXGIFactory1`, `CreateDXGIFactory2`, `DXGIDeclareAdapterRemovalSupport`, `DXGIDisableVBlankVirtualization`, `DXGIGetDebugInterface`, and `DXGIGetDebugInterface1`. The controlled app calls all three factory-creation functions; the other four exports compile and forward but were not runtime exercised. If the provider or a required export cannot be loaded, factory creation returns `DXGI_ERROR_UNSUPPORTED`. That startup failure cannot fall back to the displaced provider and is **not fail-open**.

The tested disposable-prefix configuration uses an app-local native DLL beside the controlled executable and:

```text
WINEDLLOVERRIDES=dxgi=n,b;d3d11,d3d10core,d3d12,d3d12core=n,b;winemenubuilder.exe=d
```

[`run_highball.sh`](../../experiments/dxgi_public_attachment/run_highball.sh) also supplies the selected engine's renderer paths, `HB_D3D12_REAL`, D3DMetal framework/library paths, and `D3DM_MTL4=0`. No Highball code or package was changed. This is a test runner configuration, not confirmation that every variable is a public Highball product setting.

Eventual packaging would need a per-prefix opt-in, architecture-matched proxy placement, preserving any application-bundled `dxgi.dll`, provider/version checks, and rollback. 32-bit applications, arbitrary prefixes, and app-local DXGI conflicts were not tested.

## 3. Wrapped interfaces and COM identity

For the tested provider, the proxy wraps `IDXGIFactory7` (and offers `IDXGIFactoryMedia` only if the provider supports it), `IDXGIAdapter4`, `IDXGIOutput6`, and `IDXGISwapChain4` (plus media interfaces only if supported). Explicit forwarding covers the implemented methods on those interfaces, including factory enumeration and swapchain creation routes (`CreateSwapChain`, `CreateSwapChainForHwnd`, `CreateSwapChainForCoreWindow`, `CreateSwapChainForComposition`, and factory-media composition-surface calls where available), output/adapter parent routes, `GetContainingOutput`, fullscreen output routes, `ResizeBuffers`, `ResizeBuffers1`, `Present`, and `Present1`.

The proxy keeps a process-wide registry keyed by the provider's canonical inner `IUnknown`; the outward primary interface address is separately indexed for safe output unwrapping. Registry lookup/insertion and final wrapper release/removal use the same SRW lock. Wrapper `AddRef`/`Release` own the underlying references. A caller-supplied output is matched by its public wrapper pointer; no RTTI cast is made on foreign COM objects. A non-wrapper pointer passes through unchanged.

Runtime identity checks passed for repeated factory QI, adapter `GetParent(IID_IDXGIFactory*)`, output `GetParent(IID_IDXGIAdapter*)`, and swapchain `GetParent(IID_IDXGIFactory*)`. Adapter0–4, Factory1–7, SwapChain0–4, and Output6 were supported by this provider. The fullscreen lifecycle passes a wrapped `GetContainingOutput` result back to `SetFullscreenState`; the proxy maps it to the provider output. FactoryMedia and SwapChainMedia QI returned `E_NOINTERFACE` in this runtime.

Coverage remains intentionally incomplete:

- A successful unknown `QueryInterface` result is returned raw, with an explicit log marker. Implementing an arbitrary future/private/provider IID by guessing its vtable is unsafe.
- `IDXGIFactoryMedia::CreateDecodeSwapChainForCompositionSurfaceHandle` returns a raw `IDXGIDecodeSwapChain` if that route exists. Its `PresentBuffer` route is not intercepted. The tested provider did not expose FactoryMedia, so this was not exercised.
- `IDXGIOutputDuplication` is returned raw and logged as a non-swapchain route. It was not treated as a Present path in this test.
- Wrapper creation requires Factory7, Adapter4, Output6, or SwapChain4 on the provider. A provider lacking these interfaces falls back to a raw object, so older providers are unsupported for full interception.

Thus the tested graph is identity-preserving for the enumerated versions and routes, not for every public or future DXGI interface.

## 4. D3D12 queue/device discovery and resource states

At swapchain creation, the proxy QIs the public device argument for `ID3D12CommandQueue`, requires a direct queue, obtains its `ID3D12Device` through `GetDevice`, and accepts only a one-node device with node mask 0 or 1. D3D11-device/context and unknown DXGI device paths remain pass-through with adapter work disabled.

For supported chains, the proxy retains the queue, device, swapchain, per-buffer adapter allocator/resources, and fence state only as needed. `GetBuffer` retrieves each public `ID3D12Resource`. The diagnostic copy is queued on the swapchain's own direct queue. At the documented D3D12 Present boundary the application must leave the presented buffer in `D3D12_RESOURCE_STATE_PRESENT` (alias of `COMMON`); the copy uses `PRESENT → COPY_SOURCE → PRESENT`, and synthetic G uses `PRESENT → RENDER_TARGET → PRESENT` ([D3D12 swap-chain contract](https://learn.microsoft.com/en-us/windows/win32/direct3d12/swap-chains), [resource barriers](https://learn.microsoft.com/en-us/windows/win32/direct3d12/using-resource-barriers-to-synchronize-resource-states-in-direct3d-12)). No CPU readback occurs. A per-slot fence protects resource/allocator reuse. Other queues, unknown states, protected chains, non-flip chains, non-RTV buffers for G, and unsupported node arrangements are not adapted.

`ResizeBuffers` and `ResizeBuffers1` drain the adapter queue and release retained backbuffer references before forwarding resize. An unresolved resize drain returns `DXGI_ERROR_WAS_STILL_DRAWING` without calling the provider resize. Destruction that cannot prove completion deliberately retains GPU references until process exit instead of releasing potentially in-flight resources. Normal completed-fence behavior is tested; actual device removal, operating-system wait failure, and unresolved-drain lifecycle were not induced. These paths remain a compatibility risk.

## 5. Present observation and GPU-only copy

The wrapper records swapchain ID, current index, width, height, format, sync interval, flags, `Present1` dirty/scroll metadata, queue/device identity, QPC entry/duration, and HRESULT. It forwards the application's original `Present`/`Present1` call. Log-only, GPU-copy, and baseline A–E cases each finished 15 frames without index mismatch. GPU-copy cases used no CPU image readback and passed all application bookkeeping variants.

The source copy happens before forwarding the application Present, on the same queue as its rendering. The `PRESENT` state assumption follows the D3D12 swapchain contract at that call boundary; the proxy does not infer states for unrelated queues or nonconforming application work.

## 6. Synthetic G and application-visible progression

After an ordinary successful source Present (`S_OK`, flags 0), the diagnostic path clears the then-current backbuffer magenta, restores it to `PRESENT`, and calls `Present` directly on the inner swapchain with `DXGI_PRESENT_DO_NOT_SEQUENCE` ([Present flags](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/dxgi-present)). This prevents recursive wrapper entry. The application issues only its normal source Presents. `Present1` G is limited to an empty dirty/scroll update. G is experimental only.

After the follow-up code review, the normal proxy build disables this diagnostic. Reproducing it now requires compiling with `FG_DXGI_ENABLE_UNSAFE_CONTROLLED_TEST_G=1` and setting `FG_DXGI_G=1`; the build guard prevents accidental use through the normal test proxy. The guard does not make same-chain G safe.

The final same-binary A–E matrix used 15 uncapped source calls per run:

| Mode | Baseline | Proxy pass-through | GPU copy | Synthetic G |
| --- | --- | --- | --- | --- |
| A: query `GetCurrentBackBufferIndex` every source frame | PASS | PASS | PASS | PASS |
| B: app-local index, one reused allocator | PASS | PASS | PASS | FAIL |
| C: app-local index, three allocator/fence contexts | PASS | PASS | PASS | FAIL |
| D: app-local index, per-backbuffer allocator/fence | PASS | PASS | PASS | FAIL |
| E: app-local index, independent three-frame context ring | PASS | PASS | PASS | FAIL |

Each failed G run records 20 expected/observed index mismatches over the 15 source calls. The first B source Present expects the next index to be 1 and observes 2 after G. A passes because it re-queries DXGI every frame. This is a concrete application-semantics failure, not a copy-barrier failure.

Step 8D previously recorded a controlled **30 source FPS → 60 Hz-mode** run: 90 source calls in about 3.0009 seconds and 90 successful G requests; DXGI counters reported 180 Present/refresh requests, public CoreGraphics mode query reported 60.0 Hz, and the Metal HUD log contained mostly 16.67 ms intervals. This is cadence evidence for the earlier dynamic-index controlled harness. It does not identify image pixels, prove each magenta G reached scanout, or fix the local-index failures found here. The Step 8D.1 uncapped matrix is not display-cadence evidence. The current rebuilt lifecycle test also completed 90 source Presents with G enabled, but its post-recreation `GetLastPresentCount` is per-chain/reset-sensitive and is not used as whole-run display evidence.

## 7. Separate composition-surface experiment

[`dcomp_overlay_probe.cpp`](../../experiments/dxgi_public_attachment/dcomp_overlay_probe.cpp) uses public `DCompositionCreateDevice3` and `DCompositionCreateDevice2`, asks for `IDCompositionDesktopDevice`, and would create a composition swapchain, attach it to a visual/desktop target, commit, and present diagnostic frames. On this Highball runtime, both public device-creation exports returned `E_NOTIMPL`. The test therefore did not reach visual/target creation or composition-chain presentation. No separate composition surface is established here. A separate surface could preserve the application's own swapchain index in principle, but the public composition bootstrap required for that approach is unavailable in this tested engine.

## 8. Lifecycle and multiple swapchains

The final G-enabled lifecycle run completed 90 source Presents, including:

- window resize followed by `ResizeBuffers` and resource rebinding;
- fullscreen enter/leave with a wrapped non-null output, state query, resize, and resource rebinding;
- `ResizeBuffers1` with one compatible same-identity direct queue set;
- helper-window focus loss/recovery;
- destruction of swapchain ID 1 and recreation as ID 2, with selection released and reacquired.

All checked calls returned success and no stale resource was used in this controlled dynamic-index app. `ResizeBuffers1` runtime coverage uses node mask 0; the code accepts masks 0 and 1 on one-node devices. Application-specific recreation and failed-drain behavior remain unverified.

The two-swapchain run created IDs 1 and 2, continued source Presents through both, and assigned synthetic G to the first eligible chain only. The app completed 90 iterations, with 90 source calls per chain and 90 synthetic-G requests on ID 1. This confirms sequential bookkeeping separation for two controlled HWND chains. It does not test concurrent creation/presents, deterministic selection of a particular game window, or local-index application behavior.

## 9. Failure behavior

Fourteen injected failure runs cover fence/event/allocator/list creation, `GetBuffer`, resource allocation, descriptor heap allocation, allocator/list reset/close, queue signal, fence event/timeout handling, and synthetic Present. Every run reports app exit 0, A-case bookkeeping PASS, and all 15 original source Presents. The proxy logs adapter-work disablement. The synthetic-Present failure occurs after the source Present succeeded, releases the selection, and leaves following source presents operating normally. Queue-signal injection happens after command submission; references are retained until the queue can be drained or the process exits.

The proxy contains C++ exceptions from optional GPU operations so allocation failures do not cross the COM Present boundary. This is exercised for HRESULT-style failpoints, not an actual allocator exhaustion. Fence timeout/event-error failpoints exercise slot-wait handling, not a real OS wait failure or a real stalled GPU. Device-removal sentinel behavior and unresolved resize/destruction were reviewed in source but not induced. Synthetic-only descriptor-heap and synthetic-Present errors were injected with G enabled; shared allocator/list/fence errors were injected on copy, not separately on G. Provider loading remains fail-closed as described above.

## 10. Real application status

**Phase 14: NOT RUN.** The only local candidate identified was `/Users/nima/Games/Deep Rock Glactic Survivor`. Its `DRG Survivor_Data/Plugins/x86_64/steam_emu.ini` contains a RUNE banner, `[Crack]` section, and RUNE data path. Its active `steam_api64.dll` SHA-256 is `5e215ed9fca1b95f1acbac37b9e3284ae727ac5128bd7196497c1e74781cbc59`, differing from neighboring `steam_api64.dll.valve` at `1db3fd414039d3e5815a5721925dd2e0a3a9f2549603c6cab7c49b84966a1af3`; `steam_api64.rne` and `steam_emu.ini` are present. No checked local Steam library had app manifest 2321470. This tree is not a provenance-clean unmodified install, so it was not launched. The actual-app requirement is unsatisfied; this is not a game failure or a D3D12 runtime result.

## 11. Pass-through overhead

The final overhead run used the controlled A case, three 2,000-frame trials per condition, `Present(1, 0)`, and logging/copy/G disabled. Baseline and app-local-proxy pass-through were run on the same Highball engine/prefix. All six trials exited successfully. Summary data are in [`summary.csv`](../../experiments/dxgi_public_attachment/evidence/step8d1-final-overhead/summary.csv), with run identity in [`run-manifest.txt`](../../experiments/dxgi_public_attachment/evidence/step8d1-final-overhead/run-manifest.txt).

| Measure | Baseline | Proxy pass-through |
| --- | ---: | ---: |
| Three-trial source-loop FPS range | 60.30–60.72 | 60.72–62.05 |
| Median of per-trial Present medians | 16.2147 ms | 16.2855 ms |
| Median of per-trial Present p95 | 16.7603 ms | 16.9348 ms |
| Maximum sampled working-set range | 86.5–94.4 MB | 90.0–97.3 MB |

Median Present deltas were +70.8 μs and +174.5 μs at p95. Because each Present waits with sync interval 1, these timings include display waiting and do not isolate proxy CPU cost. FPS and working-set ranges overlap; this run shows no gross regression in the controlled 60-Hz path but does not prove negligible or representative product overhead. Wine's private-memory counter reported roughly 44 GB and is not treated as valid memory evidence. G-path timing is separate and is dominated by its additional Present/synchronization behavior; no product performance claim is made.

## 12. Independent audits

Six read-only GPT-6 Luna Max audits covered: (1) COM identity and lifetime; (2) public DXGI wrapper coverage; (3) D3D12 queue synchronization and resource states; (4) presentation semantics/evidence; (5) Wine forwarding and real-app evidence; and (6) failure behavior/private-boundary compliance. Audits found no blocker in the tested canonical identity routes, normal source-copy barriers/fences, or private-interface boundaries. They confirmed the local-index synthetic-G blocker, unknown-IID/decode-swapchain public escapes, provider-load fail-closed behavior, untested real-app phase, and the limits on current failpoint evidence. The source was revised for the RTTI cast, optional-work exception containment, successful-Present G gating, selection clearing on GPU object initialization failure, and node-mask 1 handling before the final build and controlled evidence runs.

## Post-review hardening

A second worst-case review found and corrected additional wrapper hazards:

- `Present` and `Present1` with `DXGI_PRESENT_TEST` now forward without queuing adapter GPU work. A current-source Highball run returned `S_OK` for both test calls, kept the current backbuffer index at 1, and logged `skipped_test_present` for each; see the [application log](../../experiments/dxgi_public_attachment/evidence/step8d1-review-present-test/app.log) and [proxy log](../../experiments/dxgi_public_attachment/evidence/step8d1-review-present-test/proxy.log).
- The wrapper no longer holds its per-swapchain state lock while calling the provider's source `Present`, `Present1`, `ResizeBuffers`, or `ResizeBuffers1`. Resize generations and an active-resize count prevent adapter work during overlapping resize calls and suppress G when a resize crosses a source Present. Concurrent Present calls pass through and disable adapter work for that chain.
- `ResizeBuffers1` now verifies that a supported direct queue's normalized single-node mask agrees with the backbuffer creation node mask. Failed public identity/queue/device queries release any non-null returned references. Unsupported queue sets still go to the provider unchanged and disable adapter work only after a successful resize.
- The controlled-only G build retains the original experiment for reproducibility but requires an explicit unsafe-test compile define. It is not enabled by the normal proxy build.
- The progression and failure runners now check their expected exits, bookkeeping, source-present counts, and failpoint logs. They require the controlled-only G build and cannot silently produce a successful-looking matrix with G compiled out.

The revised source and both proxy configurations compiled cleanly with MinGW-w64. The repository CTest suite passed 4/4. The current-source 90-frame Highball lifecycle run passed resize, fullscreen enter/leave, `ResizeBuffers1`, focus recovery, and recreation with GPU copying enabled; all drains completed. The current-source failure matrix passed 14/14 failpoints. The current-source A–E matrix passed all baseline, proxy pass-through, and GPU-copy cases; G case A passed and G cases B–E failed as expected. The focused G rerun also confirmed 20 local-index mismatches in case B, confirming that the build guard is necessary. See the [review progression matrix](../../experiments/dxgi_public_attachment/evidence/step8d1-review-matrix/), [review lifecycle evidence](../../experiments/dxgi_public_attachment/evidence/step8d1-review-lifecycle/), [review failpoints](../../experiments/dxgi_public_attachment/evidence/step8d1-review-failpoints/), and [review G cases](../../experiments/dxgi_public_attachment/evidence/step8d1-review-synthetic/).

The COM registry still relies on the caller retaining a valid interface reference while a wrapper method consumes that pointer; concurrent final `Release` against a call using the same reference is outside normal COM lifetime rules and was not stress-tested. Actual device removal, unresolved queue drain, and concurrent resize/present behavior remain uninduced. The proxy remains a test-only experiment.

## Decision and recommended next step

**PARTIAL PASS.** On this named Highball runtime, an unmodified controlled app can load the app-local proxy; the enumerated public factory/adapter/output/swapchain graph preserves tested identity paths; D3D12 backbuffers are accessible; GPU-only copies work; the lifecycle and two-chain controlled cases complete; and injected adapter GPU-work failures preserve original source Presents. This is not a general transparent DXGI adapter: unknown/decode interfaces can bypass observation, provider startup is not fail-open, and no provenance-clean third-party app was tested.

The precise missing capability is a generated presentation path that does not mutate application-visible swapchain progression. Same-chain G fails four common local-index/fence/allocator policies. A separate DirectComposition device/target could not be created on the tested Highball runtime (`E_NOTIMPL` from both public creation APIs). A targeted Step 8D.2 on this same runtime is **not justified** without a newly available supported public composition route or a different Highball engine with working public DirectComposition. Do not implement RIFE or a product adapter from this result. Stop this Step 8D experiment here; use the separately planned DXMT reference integration as the next renderer-integration investigation. If a supported composition path later becomes available, validate that independent surface before attempting any frame-generation backend.

## Evidence index

- Final A–E matrix and exact binary hashes: [`step8d1-final-matrix`](../../experiments/dxgi_public_attachment/evidence/step8d1-final-matrix/).
- Fourteen failure injections: [`step8d1-final-failpoints`](../../experiments/dxgi_public_attachment/evidence/step8d1-final-failpoints/).
- Current-source lifecycle and wrapped fullscreen output: [`step8d1-final-lifecycle-app.log`](../../experiments/dxgi_public_attachment/evidence/step8d1-final-lifecycle-app.log), [`step8d1-final-lifecycle-proxy.log`](../../experiments/dxgi_public_attachment/evidence/step8d1-final-lifecycle-proxy.log).
- Current-source two-chain selection: [`step8d1-final-two-swapchains-app.log`](../../experiments/dxgi_public_attachment/evidence/step8d1-final-two-swapchains-app.log), [`step8d1-final-two-swapchains-proxy.log`](../../experiments/dxgi_public_attachment/evidence/step8d1-final-two-swapchains-proxy.log).
- DirectComposition probe result: [`step8d1-dcomp-overlay.log`](../../experiments/dxgi_public_attachment/evidence/step8d1-dcomp-overlay.log).
- Baseline/pass-through timing: [`step8d1-final-overhead`](../../experiments/dxgi_public_attachment/evidence/step8d1-final-overhead/).
- Follow-up Present-test, lifecycle, failpoint, and synthetic-G review runs: [`step8d1-review-present-test`](../../experiments/dxgi_public_attachment/evidence/step8d1-review-present-test/), [`step8d1-review-lifecycle`](../../experiments/dxgi_public_attachment/evidence/step8d1-review-lifecycle/), [`step8d1-review-failpoints`](../../experiments/dxgi_public_attachment/evidence/step8d1-review-failpoints/), and [`step8d1-review-synthetic`](../../experiments/dxgi_public_attachment/evidence/step8d1-review-synthetic/).
- Follow-up current-source progression matrix: [`step8d1-review-matrix`](../../experiments/dxgi_public_attachment/evidence/step8d1-review-matrix/).
- Historical controlled display-cadence experiment: [Step 8D app](../../experiments/dxgi_public_attachment/evidence/step8d-synthetic-g-app.log), [proxy](../../experiments/dxgi_public_attachment/evidence/step8d-synthetic-g-proxy.log), [display mode](../../experiments/dxgi_public_attachment/evidence/step8d-display-refresh.txt), and [Metal HUD](../../experiments/dxgi_public_attachment/evidence/step8d-synthetic-g-metal-hud.log).
