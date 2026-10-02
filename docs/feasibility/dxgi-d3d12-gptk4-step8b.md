# Step 8B — Public DXGI/D3D12 feasibility above D3DMetal

**Result: PARTIAL PASS for D3D12 resource access and successful synthetic Present requests in a cooperative Wine test app. NOT VALIDATED for an unmodified game's DXGI interception, actual display cadence, or GPTK 4/D3DMetal runtime.** No D3DMetal implementation material was inspected. No neural backend was connected.

**Retrospective attribution correction from Step 8C:** this report incorrectly labeled the `com.gamemac.www` Wine tree as Highball. It was a different GameHub/Proton tree. The Step 8B resource/present and `E_NOTIMPL` observations remain valid for that specific run, but Step 8B did not use Highball’s GPTK 4 runtime. See [the Step 8C report](dxgi-d3d12-gptk4-step8c.md) for the actual Highball GPTK 4 validation.

The test proves that an app which explicitly hands a public D3D12 swapchain, device, and direct queue to an in-process adapter can let that adapter retrieve the swapchain's `ID3D12Resource` backbuffers, copy them to adapter-owned D3D12 resources, issue GPU commands, and make `Present`/`Present1` calls on that same swapchain. The controlled app also made one successful synthetic `G` Present1 request between source Presents. This is not a transparent interposer: the cooperative app explicitly loads and calls the adapter. The Wine provider accepted the calls, but its `GetFrameStatistics` implementation is a logged stub, and interval-zero presents can be discarded. Therefore the test does **not** prove that G reached the display or that 30→60/60→120 display cadence was achieved.

## Runtime and package identity — Step 8B evidence

The Step 8B test used the Wine runner and prefix under the `com.gamemac.www` support tree. The original attribution to Highball was incorrect; the tree was GameHub/Proton. Step 8B’s Wine loader trace identified its `dxgi.dll` and `d3d12.dll` as Wine builtins, and its `GetFrameStatistics` calls returned `E_NOTIMPL`. There was no D3DMetal framework load in that process, so Step 8B did not exercise GPTK 4.

The GPTK 4 / D3DMetal 4.0b2 package metadata referred to a separate installed runtime and did not establish which runtime Step 8B had loaded. Step 8C subsequently exercised the actual Highball engine `x64-sikarugir10.0_6-r14` with its `D3DMetal-4.0b2` component; the package identity, effective Highball configuration, loaded framework paths, and display evidence are documented separately in [Step 8C](dxgi-d3d12-gptk4-step8c.md).

## Experimental architecture and interception boundary

```text
Cooperative test executable
  ├─ LoadLibraryW("fg_probe.dll")
  ├─ CreateDXGIFactory2 → IDXGIFactory2
  ├─ D3D12CreateDevice → ID3D12Device
  ├─ ID3D12Device::CreateCommandQueue → direct ID3D12CommandQueue
  ├─ IDXGIFactory2::CreateSwapChainForHwnd → IDXGISwapChain1
  └─ FG_Attach(factory, device, queue, swapchain)
       ├─ IDXGISwapChain1::GetBuffer → ID3D12Resource backbuffers
       ├─ public D3D12 copy/clear/barrier/fence work
       └─ IDXGISwapChain1::Present / Present1
```

`fg_probe.dll` is an application-loaded adapter API, not a `dxgi.dll` proxy, a COM vtable patch, or an injected module. The test application passes the live public COM interfaces directly to exported adapter functions. It owns swapchain creation and calls `FG_RenderSource`/`FG_PresentSource` in place of a game renderer's loop. This deliberate cooperation validates the resource and command path but does not intercept an unmodified game's factory creation or Present calls.

The standard Wine route investigated was the documented native/builtin DLL override, with the adapter loaded by the cooperative app. `WINEDLLOVERRIDES=dxgi=n,b;d3d12=n,b;d3d12core=n,b` selects native-first/builtin-fallback load order. It does not insert a wrapper around an already-created COM interface or give a native DXGI proxy a supported entry point to call the Wine builtin provider. `WINEDLLPATH` supplies Wine library search paths; the GPTK package manifest also provides the two environment values listed above. A per-prefix native DLL installation and override would be required for a future proxy experiment. This test did not build or validate such a proxy.

The exact public hook missing for transparent use is a supported Wine integration point that lets an adapter observe the game's returned factory/swapchain interfaces and wrap their documented methods **while retaining a callable underlying Wine/GPTK DXGI implementation**. The normal DLL override provides selection/fallback, not that forwarding contract. Without it, the app itself must load and call the adapter, or the renderer must be integrated directly. A native proxy that substitutes DXGI without a supported forwarding route was not attempted.

## Public calls and resource access exercised

The harness called or queried these documented interfaces:

- `CreateDXGIFactory2` → `IDXGIFactory2`, then `QueryInterface` for `IDXGIFactory4`.
- `D3D12CreateDevice` → `ID3D12Device`; `CreateCommandQueue` created a direct `ID3D12CommandQueue`.
- `IDXGIFactory2::CreateSwapChainForHwnd` created a three-buffer, 960×540, `DXGI_FORMAT_R8G8B8A8_UNORM`, flip-discard swapchain.
- The same object supported `IDXGISwapChain1`, `IDXGISwapChain3`, and `IDXGISwapChain4`. The adapter used `GetDesc1`, `GetCurrentBackBufferIndex`, `GetBuffer`, `Present`, `Present1`, `GetLastPresentCount`, `GetFrameStatistics`, `ResizeBuffers`, `SetFullscreenState`, and `GetFullscreenState`.
- `GetBuffer` returned `ID3D12Resource` objects. `GetDesc` reported 960×540, format 28 (`R8G8B8A8_UNORM`), 2D resources. The harness queried canonical `IUnknown` identity and logged each buffer identity. The adapter allocated two same-format, same-size library-owned committed D3D12 textures; these are D3D12 resources, not Metal textures.
- `ID3D12Device` created render-target views, command allocators, a direct command list, a fence, and a fence event. The adapter recorded `CopyResource`, `ClearRenderTargetView`, and documented transition barriers. It submitted work on the supplied direct queue, signaled a fence, and waited before allocator reuse and buffer resize.

The copy path captures the current source backbuffer to an adapter-owned texture, then restores the saved B source into the next swapchain backbuffer before its source Present. The synthetic test uses full-frame constant-color A and B images: CPU code computes the four scalar midpoint channels, and a GPU `ClearRenderTargetView` writes G. The adapter also performs GPU-only `CopyResource` operations. It does not read pixels back to CPU memory, call screen capture, or create an `MTLTexture`. G is a cheap presentation probe, not a general pixel-sampling or interpolation kernel.

Before `ResizeBuffers`, the adapter signals/waits the queue fence, resets and closes its command list to drop recorded backbuffer references, releases its backbuffer and owned-texture references, then re-fetches buffers and rebuilds views/resources after the resize. The exercised fence and barrier path is for the controlled app's known PRESENT/RENDER_TARGET/COPY_SOURCE/COPY_DEST states; an external adapter cannot safely guess arbitrary state or queue ownership for an unmodified application. The harness uses one swapchain and one direct queue.

## Results

Each cadence run lasted five seconds and used absolute QPC deadlines for source production and output Present requests. `Present(0, ...)` was used so the harness could schedule calls itself; Microsoft documents interval-zero behavior as eligible to discard older queued frames. The counts below are DXGI call counts, not monitor-present counts.

| Run | Requested source → output | Source frames | G requests | Present/Present1 calls | Failed Present HRESULTs | Actual call-gap median / p95 / max | Display result |
|---|---:|---:|---:|---:|---:|---:|---|
| Cadence | 30→60 | 150 | 149 | 299 | 0 | 16.673 / 19.209 / 26.164 ms | **UNVERIFIED**; `GetFrameStatistics` returned `E_NOTIMPL` |
| Cadence | 60→120 | 300 | 299 | 599 | 0 | 8.334 / 15.190 / 25.583 ms | **UNVERIFIED**; 60 Hz output cannot establish 120 Hz |
| Lifecycle | 30→60 | 150 | 149 | 299 | 0 | not used as cadence evidence | **UNVERIFIED**; lifecycle test only |

The timings are QPC intervals between successful Present API calls in the final build. The cooperative process requested interleaved source/G calls, but the p95 and maximum call gaps exceed the target intervals, especially in the 60→120 mode. These are not refresh-aligned presentation measurements: Wine's `GetFrameStatistics` returned `0x80004001` on every sample, and Wine's `+fixme` log explicitly labels its D3D12 swapchain `GetFrameStatistics` implementation a stub. `GetLastPresentCount` reached 299 and 599, matching API calls. Microsoft explicitly documents that this counter measures Present/Present1 calls and that monitor-present counts need not equal call counts. Accordingly no claim is made that every G was displayed, that G reached the physical panel, or that the resulting display actually ran at 60/120 Hz.

The lifecycle run requested a window resize to 1024×576 and back to 960×540, then `SetFullscreenState(TRUE)` and `SetFullscreenState(FALSE)` with `GetFullscreenState` checks and `ResizeBuffers` calls. All calls returned `S_OK`, and the process exited normally. This was a short controlled harness run. It did not cover `ResizeBuffers1`, repeated recreation, manually triggered focus loss/recovery, concurrent swapchains/windows, variable source rates, long-term memory growth, a 30-minute soak, or a GPTK/D3DMetal app.

## Wine loading and attachment boundary

In Step 8B, the cooperative executable called `LoadLibraryW("fg_probe.dll")`; the run log recorded the path beside the executable. The native/builtin override trials applied only to the GameHub/Proton runner used in that step. They did not validate Highball’s actual Wine configuration. Step 8C later used Highball’s public renderer environment and proved that the cooperative harness can operate through that GPTK 4 runtime.

Neither step attached to an unmodified game. A game that does not load and call the adapter will not use it. A normal Wine DLL override selects DLL loading order; Step 8B did not prove a supported transparent wrapper/forwarding contract for the builtin DXGI implementation. Step 8C leaves that question to the narrowly scoped transparent-attachment follow-up.

Do not target anti-cheat or protected games without explicit support from the game and its protection vendor. No anti-cheat path was tested. The controlled standalone harness is the appropriate scope for this probe.

## Framegen API implication

The test supports keeping resource ownership backend-specific. A future renderer-facing resource descriptor could carry a resource kind (`METAL_TEXTURE`, `D3D12_RESOURCE`, `VULKAN_IMAGE`), dimensions/format, owning device/queue, producer completion primitive, resource state/layout, and lifetime rules. Its output side must also define the queue/state that the renderer can consume and who owns Present cadence. A D3D12-native Framegen backend would be a separate backend; a D3D12 resource must not be represented as a fake `MTLTexture`.

This controlled D3D12 evidence is enough to justify preserving an abstract-resource API option, but not enough to start a D3D12 inference backend or add a product adapter. Step 8C now establishes the cooperative D3D12 path on Highball GPTK 4; transparent game attachment remains untested. Keep RIFE disconnected from this route.

## Phase answers and recommendation

| Phase / criterion | Finding |
|---|---|
| Public DXGI/D3D12 call path | **Observed in the cooperative Wine harness**, not in a D3DMetal application or an unmodified game. |
| In-process adapter attachment | **Cooperative only:** explicit `LoadLibraryW` and explicit COM-interface handoff. Transparent loading/wrapping was not established. |
| Backbuffer retrieval and GPU work | **PASS in the cooperative harness:** `GetBuffer`, `ID3D12Resource`, GPU copies/clears, fences and state transitions all succeeded. |
| Observe/control real game's Present/Present1 | **NOT ESTABLISHED:** both calls were made on the harness-owned swapchain; calls from a separate game's loop were not observed or wrapped. |
| Extra synthetic Present between source Presents | **PASS at API-submission level:** 149/299 G requests for 30→60 and 299/599 for 60→120 succeeded and were QPC scheduled between source Present requests. Display delivery/cadence remains unverified. |
| Resize/fullscreen | **PASS for the short single-window harness sequence.** |
| GPTK 4/D3DMetal validation | **NOT RUN in Step 8B:** the successful Step 8B tests used the GameHub/Proton Wine builtins. Step 8C separately validates the cooperative path on Highball GPTK 4. |
| Screen capture / CPU pixel readback | **Not used.** |
| Multiple windows, focus loss, varying FPS, long soak | **NOT TESTED.** |

**Historical Step 8B recommendation — superseded by Step 8C.** The proposed Highball runtime/display-statistics follow-up was completed using Highball’s actual public environment. The cooperative path passed; the next experiment, if separately authorized, is Step 8D for transparent attachment/wrapping only. Do not compensate for missing hooks with private D3DMetal knowledge, screen capture, CPU readback, or game binary patching.

Build/run source is in [`experiments/dxgi_public_probe`](../../experiments/dxgi_public_probe/README.md). Primary API references: [D3D12 swap chains](https://learn.microsoft.com/en-us/windows/win32/direct3d12/swap-chains), [`GetBuffer`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/nf-dxgi-idxgiswapchain-getbuffer), [`Present`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/nf-dxgi-idxgiswapchain-present), [`Present1`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi1_2/nf-dxgi-idxgiswapchain1-present1), [`GetLastPresentCount`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/nf-dxgi-idxgiswapchain-getlastpresentcount), [`DXGI_FRAME_STATISTICS`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/ns-dxgi-dxgi_frame_statistics), [resource barriers](https://learn.microsoft.com/en-us/windows/win32/direct3d12/using-resource-barriers-to-synchronize-resource-states-in-direct3d-12), and the [Wine DLL override format](https://goma.googlesource.com/wine/%2B/d937dc2963a8a1686bbbbb0a5ae1b1cf00b07f2d/documentation/wine.conf.man).

## Independent boundary review

An independent GPT-6 Luna reviewer at Max reasoning audited the final Step 8B source, report, and public runtime evidence. It found no material issues, no accidental reliance on private D3DMetal details, and agreed that the conclusions remain limited to the cooperative Wine builtin path; the call counters and QPC timing do not establish display cadence. The review did not inspect D3DMetal/GPTK binaries or implementation internals.
