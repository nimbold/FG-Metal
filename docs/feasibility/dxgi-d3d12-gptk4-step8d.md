# Step 8D — Transparent DXGI/D3D12 attachment feasibility

- **Date:** 2026-10-02
- **Decision:** **PARTIAL PASS**
- **Runtime:** Highball 0.10.1, `x64-sikarugir10.0_6-r14`, Wine 10.0 (Sikarugir), macOS on Apple M3, 2940×1912 at 60 Hz
- **Scope:** Public DXGI/D3D12 interception and diagnostic GPU work only. No RIFE, HUD detection, D3DMetal inspection, application patching, screen capture, or CPU pixel readback.

> **Historical snapshot:** This report records the original Step 8D state. The adapter/GetParent and canonical-identity findings below are superseded for the tested interfaces by [Step 8D.1](dxgi-d3d12-gptk4-step8d1.md). The later report records the remaining unknown-IID/decode-swapchain escapes, local-index G failure, failpoint results, and final decision.

## Result

An ordinary controlled D3D12 executable with no Framegen imports or calls loaded an app-local `dxgi.dll` proxy through the tested Highball/Wine configuration. The proxy forwarded all three `CreateDXGIFactory*` entry points to a separate system32 Wine DXGI provider, returned a wrapped factory and swapchain on the exercised route, found the public D3D12 queue/device, copied all 90 presented buffers on-GPU, and submitted 90 extra magenta diagnostic Presents. The controlled application completed its 90 source Presents normally. DXGI counters reached 180 total Presents, and the Metal HUD reported mostly 16.67 ms present intervals at the display's 60 Hz mode.

This proves feasibility for a **narrow direct-factory controlled path on this runtime**. It does not prove that the adapter transparently captures general applications. An adapter-enumeration/`GetParent` route returns a raw factory and bypasses observation; unknown `QueryInterface` results can also escape wrapping. The proxy does not yet preserve COM identity across all returned interface paths. No third-party unmodified D3D12 application/game was available, so the real-application phase was not run. These are material gaps; the result is not PASS.

The experiment used only public DXGI/D3D12/Win32 interfaces. It did not inspect private D3DMetal or Wine symbols/layouts, modify an application or renderer, or use a protected game.

## Architecture selection

| Candidate | Assessment |
| --- | --- |
| App-local DXGI proxy DLL plus typed COM wrappers | Selected for the experiment. DXGI exports can be intercepted before application factory creation while returned factory and swapchain methods are forwarded to their provider objects. Installation can be scoped beside an executable and enabled by a prefix override. |
| Wrapping only objects returned from factory exports | Needed as the object-level part of the selected design, but insufficient alone: it misses factories obtained by other public paths and does not cover direct imports if there is no proxy. |
| Wine-wide builtin replacement, private Wine hooks, generic process injection | Not selected. A replacement of the system provider would need a supported way to reach the displaced provider, and generic injection/private loader hooks are outside the intended boundary. No such mechanism was needed for the tested app-local path. |

The prototype combines the first two: a native app-local `dxgi.dll` exports documented DXGI entry points, and typed wrappers intercept the returned factory/swapchain interfaces. This is an experiment, not a drop-in or redistributable DXGI implementation.

## Provider forwarding and loading

The proxy resolves the provider with public Win32 `LoadLibraryW(L"C:\\windows\\system32\\dxgi.dll")` and `GetProcAddress`. It compares the returned module with its own module and rejects recursion. In the tested configuration, the loader trace shows the app-local proxy and a distinct system32 Wine builtin provider in the same process. See [loader evidence](../../experiments/dxgi_public_attachment/evidence/step8d-synthetic-g-loader-hud.log) and [build/runtime identity](../../experiments/dxgi_public_attachment/evidence/step8d-build-manifest.txt).

The exports implemented and forwarded are `CreateDXGIFactory`, `CreateDXGIFactory1`, `CreateDXGIFactory2`, `DXGIDeclareAdapterRemovalSupport`, `DXGIDisableVBlankVirtualization`, `DXGIGetDebugInterface`, and `DXGIGetDebugInterface1`. The controlled executable calls each of the three factory-creation exports. The other four exports are present but were not exercised; if the runtime lacks an export, the proxy reports `DXGI_ERROR_UNSUPPORTED`.

This forwarding route is **empirically verified on the selected Highball/Wine engine, not a general documented Wine proxy-forwarding contract**. A system32-installed proxy would resolve the same path back to itself and fail the recursion check. If loading the provider or resolving an export fails, factory creation returns failure; startup/provider failure is not fail-open. The proxy should therefore remain app-local and be limited to a controlled test directory until broader Wine behavior is established. The relevant documented Windows loader API is [LoadLibraryW](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryw); Wine's override syntax is described in its [`wine.conf` documentation](https://goma.googlesource.com/wine/%2B/d937dc2963a8a1686bbbbb0a5ae1b1cf00b07f2d/documentation/wine.conf.man).

## Factory/swapchain coverage and COM behavior

The proxy wraps provider factories as `IDXGIFactory7`; when supported it separately exposes `IDXGIFactoryMedia`. The tested provider returned Factory1 through Factory7 and returned `E_NOINTERFACE` for FactoryMedia. The swapchain proxy is backed by `IDXGISwapChain4`, exposing SwapChain1 through SwapChain4. The exercised creation route is `CreateSwapChainForHwnd`; the prototype has forwarding methods for legacy `CreateSwapChain`, CoreWindow, composition, and composition-surface-handle creation, but those routes were not exercised. Decode swapchains and interfaces not implemented by the proxy are not comprehensively intercepted.

The wrapper owns a reference to its inner COM object, uses an atomic wrapper reference count, and keeps queue/device references for a wrapped D3D12 swapchain. `QueryInterface` returns the wrapper for explicitly supported IIDs. It returns the provider's raw interface for other IIDs. Factory enumeration methods also return raw adapters. A `GetParent` call made on an already wrapped factory or swapchain wraps a returned factory, but creates a new wrapper without looking it up in a canonical-identity registry. If the provider cannot supply Factory7 or SwapChain4, factory/swapchain wrapping falls back to the raw object.

The bypass is reproduced in [adapter-parent app output](../../experiments/dxgi_public_attachment/evidence/step8d-adapter-parent-app.log) and [proxy output](../../experiments/dxgi_public_attachment/evidence/step8d-adapter-parent-proxy.log): after `EnumAdapters1` and adapter `GetParent(IID_IDXGIFactory2)`, `same_wrapper=0`; creating/presenting through that factory produces no proxy swapchain or Present log entries. Unknown-IID escape and lack of global canonical-identity tracking are additional known risks. COM requires stable interface-set and identity behavior; see Microsoft's [QueryInterface rules](https://learn.microsoft.com/en-us/windows/win32/com/rules-for-implementing-queryinterface). Until every public factory/adapter/swapchain path is wrapped and identity-preserving, this proxy cannot claim general COM-transparent behavior.

## D3D12 discovery and resource-state strategy

For swapchain creation, the proxy queries the supplied `IUnknown` for `ID3D12CommandQueue`, requires a direct queue, gets its device through the public D3D12 child interface, and checks that the device has one node and the queue/node mask is compatible with node 0/1. The selected runtime reported one node and queue mask 1. D3D11 and unsupported queue/device forms remain pass-through without adapter GPU work. Microsoft's [D3D12 swap-chain contract](https://learn.microsoft.com/en-us/windows/win32/direct3d12/swap-chains) documents the command queue and device relationship for a D3D12 swapchain.

At `Present`/eligible `Present1`, the prototype gets the current `ID3D12Resource` backbuffer and records its dimensions/format/index. It allocates an adapter-owned default-heap resource per backbuffer and records a GPU copy on the swapchain's direct queue. It uses the documented D3D12 swapchain Present boundary: the presented buffer is in `D3D12_RESOURCE_STATE_PRESENT` (an alias of `COMMON`), then transitions `PRESENT → COPY_SOURCE → PRESENT`. The copy destination remains in its matching copy state. No CPU pixels are read back. See Microsoft's [swapchain resource-state contract](https://learn.microsoft.com/en-us/windows/win32/direct3d12/swap-chains) and [resource barrier guidance](https://learn.microsoft.com/en-us/windows/win32/direct3d12/using-resource-barriers-to-synchronize-resource-states-in-direct3d-12).

On the normal path, adapter queue work is fenced, allocators/resources are not reused until their fence completes, and `ResizeBuffers`/`ResizeBuffers1` drain queue work before dropping retained backbuffer references; resources are recreated lazily after resize. This is validated only when fences complete successfully on the observed single direct queue/node configuration and supported flip-model, non-protected chains. The failure path is not yet safe to generalize: `wait_for_fence` currently treats `GetCompletedValue() == UINT64_MAX` as completed, although D3D12 documents that value as the device-removed sentinel. That branch is logged as complete; a separate signal/wait failure logs `degraded_release`, and resize/destruction still releases retained GPU resources. Device removal and synchronization failure were not injected. `WaitForSingleObject` also uses an unbounded wait while the swapchain mutex is held, so a stalled fence can block the caller. See Microsoft's [`GetCompletedValue` contract](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12fence-getcompletedvalue). The prototype does not infer arbitrary resource state or synchronize unrelated queues.

## Present observation and synthetic G

The wrapper observes and forwards `Present` and `Present1`, recording swapchain identity, current buffer index, extent/format, sync interval/flags, QPC timing, queue/device identity, and HRESULT. Copy-only and fail-open runs show normal source Present calls. No generated frame is inserted in those runs.

For the synthetic-G run, after a successful eligible application Present the proxy clears the current backbuffer to magenta on the same queue, transitions `PRESENT → RENDER_TARGET → PRESENT`, fences the work, then calls the inner swapchain's Present with `DXGI_PRESENT_DO_NOT_SEQUENCE`. The call goes directly to the inner object, so it does not recursively enter the wrapper. The application issues only its regular source Presents. In this Wine provider, the extra Present advances `GetCurrentBackBufferIndex`; the controlled app queries the index again each frame and completes. This is a deliberate diagnostic, not a generic coordination scheme. An application that advances its own backbuffer/fence slot once per source frame can become inconsistent, and dirty-rect/flip-sequential apps need separate validation. Microsoft's [Present documentation](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/dxgi-present) defines the public flags, but does not establish compatibility with every application's buffer bookkeeping.

Current [application logs](../../experiments/dxgi_public_attachment/evidence/step8d-synthetic-g-app.log) record 90 source Presents in 3.0008803 s, all successful; `DXGI_FRAME_STATISTICS` and `GetLastPresentCount` report 180 after the 90 extra present requests. [Proxy logs](../../experiments/dxgi_public_attachment/evidence/step8d-synthetic-g-proxy.log) record 90 queued copies and 90 successful synthetic-G Present requests. The 60 Hz display mode is recorded in [display evidence](../../experiments/dxgi_public_attachment/evidence/step8d-display-refresh.txt).

The [Metal HUD log](../../experiments/dxgi_public_attachment/evidence/step8d-synthetic-g-metal-hud.log) contains 119 per-frame present-interval samples: 118 were 16.67 ms; one initial interval was 80.65 ms. Median, p95, and p99 were 16.67 ms. These observations support approximately 60 Hz present cadence while the process requested source plus G. DXGI counters and HUD timing do **not** identify each displayed image or prove exact A/G/B pixel order. No exact displayed-pixel claim is made. Step 8D's loader trace does not independently identify the D3DMetal framework binary; Step 8C established D3DMetal 4.0b2 in-process for the same Highball engine/configuration.

## Lifecycle, multiple swapchains, failure, and overhead

| Area | Result and limit |
| --- | --- |
| `ResizeBuffers` | Frame-30 resize succeeded after adapter fence drain/reference release; resources were reacquired. |
| Fullscreen transitions | Enter and leave calls, state queries, `ResizeBuffers`, and resource rebinding succeeded. The test follows the documented requirement to resize after fullscreen transitions. See [SetFullscreenState](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/nf-dxgi-idxgiswapchain-setfullscreenstate). |
| `ResizeBuffers1` | Succeeded with same-identity compatible queue set; GPU copy resumed. The adapter only re-enables work when the first `nodeMask` is 0; nonzero masks and other queue/node arrangements are forwarded but leave adapter work disabled. |
| Focus | An in-process helper window caused the app to receive `WM_KILLFOCUS` and `WM_SETFOCUS`; this was not an OS-level app switch/alt-tab test. |
| Destruction/recreation | First chain drained and was destroyed; a second wrapper identity was created and observed. DXGI counters reset/aggregate in ways that make them unsuitable as per-chain evidence here. |
| Two swapchains | Two independent wrapper IDs received 90 Presents and GPU copies each; only the first was selected for 90 G requests. See [app](../../experiments/dxgi_public_attachment/evidence/step8d-two-swapchains-app.log) and [proxy](../../experiments/dxgi_public_attachment/evidence/step8d-two-swapchains-proxy.log). Selection is process-global and is not reset to a replacement chain after destruction; G-after-recreation was not tested. This does not fix factory/adapter escape paths. |
| Injected GPU failure | The injected pre-work failure disabled adapter work and preserved 90 successful source Presents with no G. See [app](../../experiments/dxgi_public_attachment/evidence/step8d-fail-open-app.log) and [proxy](../../experiments/dxgi_public_attachment/evidence/step8d-fail-open-proxy.log). |
| Other GPU failures | Several `GetBuffer`/resource/recording failures preserve the current source Present but return without disabling adapter work, so later Presents can retry. A failed synthetic inner Present is logged but does not disable G. Full per-swapchain fail-open behavior is not established. |
| Provider/export failure | Provider load or export lookup returns `DXGI_ERROR_UNSUPPORTED`; because the proxy occupies the imported DXGI name, it cannot fall back to the displaced provider. The other four exports were not exercised, and unexported imports may fail during loader resolution. |
| Fence/device failure | **Unresolved robustness gap:** fence waits are unbounded; `GetCompletedValue() == UINT64_MAX` is incorrectly treated as successful completion; and signal/wait failures do not prevent resize/destruction from releasing retained resources. Device-removal behavior was not tested; do not claim lifetime safety for this failure path. |

The lifecycle run completed 90 frames in approximately 3.30 s (including the focus interval) and logged successful resize/recreation calls in [lifecycle app](../../experiments/dxgi_public_attachment/evidence/step8d-lifecycle-app.log), [proxy](../../experiments/dxgi_public_attachment/evidence/step8d-lifecycle-proxy.log), and [loader trace](../../experiments/dxgi_public_attachment/evidence/step8d-lifecycle-loader.log).

One paired 30-fps run compared the app without the proxy against proxy-loaded pass-through with copy/G/logging disabled. Baseline mean/median/p95 Present-call time was 4.89/4.50/8.10 μs, 29.990 FPS, and max sampled working set 79.16 MiB. With the proxy, values were 13.37/15.15/18.90 μs, 29.997 FPS, and 90.84 MiB. The observed median overhead was about 10.65 μs and p95 about 10.80 μs. This is one run per condition with a 30-fps limiter, so it is suggestive only; it is not a repeated or high-FPS overhead characterization. Wine's private-memory counter was implausible and is excluded. See [baseline](../../experiments/dxgi_public_attachment/evidence/step8d-perf-baseline-app.log) and [proxy pass-through](../../experiments/dxgi_public_attachment/evidence/step8d-perf-pass-through-app.log).

## Highball configuration and real application status

The disposable-prefix test uses an app-local `dxgi.dll` next to the controlled executable and:

```text
WINEDLLOVERRIDES=dxgi=n,b;d3d11,d3d10core,d3d12,d3d12core=n,b;winemenubuilder.exe=d
```

The run script configures the selected Highball engine's renderer paths and D3DMetal-related public environment. No Highball source or package was changed. This establishes a temporary test configuration only. Eventual integration would need a per-prefix opt-in, an app-local install location that preserves any game-provided `dxgi.dll`, explicit 32/64-bit DLL selection, runtime/provider compatibility checks, and a rollback path. Highball support is not implemented.

No third-party unmodified non-protected D3D12 application/game was available, so Phase 14 is **NOT RUN / UNVERIFIED**. The controlled application is unmodified with respect to Framegen: it imports ordinary DXGI/D3D12 only and never links to or calls Framegen. It validates the loader/interception mechanism for its direct creation path, not actual-game compatibility.

## Independent review and remaining blocker findings

Independent reviews of the current COM and Wine/Highball paths confirmed the material gaps: raw adapter and unknown-IID results can bypass the proxy; a wrapper registry is absent, so canonical identity across independently acquired wrappers is unverified; and provider-load/export failure returns `DXGI_ERROR_UNSUPPORTED` instead of recovering to the provider. The D3D12/failure review found the normal queue/barrier path coherent, but confirmed the false fence-complete sentinel, unbounded waits, and resource release after degraded drains. It also found that some GPU failures do not disable later attempts and that synthetic selection does not move to a replacement chain. These failure branches remain untested. The tested G app re-queries its current buffer index and completes, but the extra Present does advance that index. Presentation counters/HUD remain cadence evidence rather than per-image proof. Earlier node-mask and fullscreen-resize concerns were corrected and the affected controlled runs were repeated. Other initial review notes predated the final runtime trace and are not treated as independent confirmation of that trace.

No blocker-level private-interface boundary violation was identified. The unresolved product blocker is general interception/COM transparency plus absence of a real-app result, not access to D3D12 resources on the tested route.

## Decision and next step

**PARTIAL PASS.** The unmodified controlled app loaded the app-local adapter under the tested Highball configuration; the direct factory/swapchain route was wrapped; D3D12 resources were observed and copied GPU-only; the test submitted extra G requests; and the tested lifecycle and two-chain path completed. General COM transparency is disproven by the reproduced adapter-parent bypass, G changes index progression, broad fail-open behavior is incomplete, and no third-party application was run.

The targeted follow-up is to fix wrapper identity and close all public factory/adapter/`QueryInterface` escape paths; make all GPU failures disable adapter work while preserving source Presents; bound waits and preserve resource lifetime on fence/device failure; reset selection on swapchain recreation; rerun the exact-path and controlled lifecycle/multi-chain cases; and test one unmodified non-protected D3D12 application under Highball. Do not build a product adapter or proceed to RIFE on this result. If the interception gaps cannot be closed using supported/public interfaces, stop D3DMetal work and proceed to the DXMT reference integration. The separate Metal-texture adapter remains blocked under D-013.

### Evidence index

- [Proxy source](../../experiments/dxgi_public_attachment/dxgi_proxy.cpp), [exports](../../experiments/dxgi_public_attachment/dxgi_proxy.def), [controlled app](../../experiments/dxgi_public_attachment/unmodified_d3d12_app.cpp), [lifecycle app](../../experiments/dxgi_public_attachment/lifecycle_app.cpp), [two-swapchain app](../../experiments/dxgi_public_attachment/two_swapchains_app.cpp)
- [Run/build instructions](../../experiments/dxgi_public_attachment/README.md), [Highball runner](../../experiments/dxgi_public_attachment/run_highball.sh), [build manifest and hashes](../../experiments/dxgi_public_attachment/evidence/step8d-build-manifest.txt)
- All retained runtime logs: [`experiments/dxgi_public_attachment/evidence/`](../../experiments/dxgi_public_attachment/evidence/)
