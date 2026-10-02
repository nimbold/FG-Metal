# Step 8 — GPTK 4 / D3DMetal synthetic-presentation feasibility

**Result: BLOCKED pending a public integration contract for a transparent D3DMetal adapter using Framegen's Metal texture contract.** Here, “transparent” means added without integrating the game renderer; it may run in-process or in a helper process. The public Apple APIs let an app process and present its own Metal frames, but the public documentation reviewed does not define a hook for an adapter to obtain an arbitrary game's final D3DMetal frame or take ownership of its presentation schedule. This is a boundary of the reviewed public contract, not a claim that every possible implementation approach has been experimentally ruled out.

This is a public-interface feasibility finding, not a runtime failure measurement. At the time of the original Step 8 review, the direct D3DMetal application test and 30-minute soak were not run because no supported source-frame/presentation contract was identified in the public material reviewed, and no GPTK 4 evaluation runtime had been verified. In-process AppKit/DXGI interposition was not experimentally ruled out. No private D3DMetal implementation, symbol, object layout, or binary was inspected. No RIFE path was used for this spike.

**Later runtime update:** Step 8B incorrectly attributed a GameHub/Proton runtime tree to Highball; its test did not load GPTK 4. Step 8C then exercised Highball’s actual `x64-sikarugir10.0_6-r14` engine, whose public manifest identifies D3DMetal 4.0b2, and verified the framework loaded in the cooperative D3D12 harness. That proves the cooperative DXGI/D3D12 route can present a synthetic G through the GPTK 4 runtime, but it does not add a public hook for a transparent Metal-texture adapter or an unmodified game. The original transparent-adapter finding remains blocked; see [Step 8C](dxgi-d3d12-gptk4-step8c.md) and the [corrected Step 8B report](dxgi-d3d12-gptk4-step8b.md).

## Scope and environment

The experiment requested a synthetic midpoint, `G = 0.5 * A + 0.5 * B`, presented between source frames at 30→60 and 60→120. That specific transparent-adapter experiment requires both the rendered source frames and authority over the same swapchain's presents. No documented external D3DMetal hook providing them was identified.

Environment checks on 2026-10-02:

- macOS 27.0.1 (build 26A434), arm64; Apple M3 with Metal 4 support.
- CrossOver 26.3 is installed.
- The only standalone GPTK package located was Heroic's `Game-Porting-Toolkit-latest`, whose bundle metadata reports version 3.0. No GPTK 4 evaluation package was verified.
- Apple's GPTK 4 page is public, but its download link redirected to Apple's developer sign-in page in this environment.
- The active developer directory is Command Line Tools, not full Xcode: `xcodebuild` is unavailable and `xcrun --find metal` found no Metal compiler.

Apple describes GPTK 4 as an evaluation environment, porting samples and skills, and Metal debugging/profiling tools. It also documents MetalFX frame interpolation as an app integration: the game supplies its rendered frames and uses its own presentation loop. The published material reviewed does not document a third-party D3DMetal swapchain callback or a present-replacement API. See [Apple's GPTK page](https://developer.apple.com/games/game-porting-toolkit/), [Apple's GPTK 4 repository](https://github.com/apple/game-porting-toolkit), and the [MetalFX frame-interpolator API](https://developer.apple.com/documentation/metalfx/mtlfxframeinterpolatordescriptor).

## Public-interface findings

Metal and QuartzCore have the necessary primitives after a participating renderer supplies resource access and presentation ownership:

- A Metal app asks its own `CAMetalLayer` for a drawable, reads that drawable's texture, and presents it through a command buffer. QuartzCore's drawable pool is finite; Apple says to release drawable references as soon as the work using them is submitted. Keeping a drawable through an interpolation interval can therefore starve that pool. See [`CAMetalLayer`](https://developer.apple.com/documentation/quartzcore/cametallayer), [`nextDrawable()`](https://developer.apple.com/documentation/quartzcore/cametallayer/1478172-nextdrawable), and [onscreen presentation](https://developer.apple.com/documentation/metal/onscreen-presentation).
- A layer's `framebufferOnly` property defaults to `true`; in that mode its textures are render-target-only and cannot be sampled, read, or written. The layer owner can configure a readable path or copy earlier from an owned render target. See [`framebufferOnly`](https://developer.apple.com/documentation/quartzcore/cametallayer/framebufferonly).
- `MTLSharedTextureHandle`, IOSurface, and `MTLSharedEvent` provide explicit cross-process resource and synchronization mechanisms. The producer must create/share the resource and pass its handle to the consumer. These APIs do not discover or export an arbitrary game's drawable. See [`MTLSharedTextureHandle`](https://developer.apple.com/documentation/metal/mtlsharedtexturehandle), [`makeSharedTexture(descriptor:)`](https://developer.apple.com/documentation/metal/mtldevice/makesharedtexture(descriptor:)), [IOSurface](https://developer.apple.com/documentation/iosurface), and [Metal synchronization events](https://developer.apple.com/documentation/metal/about-synchronization-events).
- `CAMetalDisplayLink` and timed drawable presentation coordinate frames for the app that owns the layer and drawables. They do not give an outside observer control of a D3DMetal present queue. See [CAMetalDisplayLink](https://developer.apple.com/documentation/quartzcore/cametaldisplaylink) and [`present(_:atTime:)`](https://developer.apple.com/documentation/metal/mtlcommandbuffer/present(_:attime:)).

For an in-process library, public AppKit APIs can enumerate the current app's `NSWindow` objects, access an `NSView`'s layer, and observe window focus, resize, and fullscreen notifications. These generic window APIs do not establish which view/layer contains the final D3DMetal output, map it to a particular DXGI swapchain, or grant control of its present calls. That mapping and any in-process interposition were not tested. See [`NSApplication.windows`](https://developer.apple.com/documentation/appkit/nsapplication/windows), [`NSView.layer`](https://developer.apple.com/documentation/appkit/nsview/layer), and [`NSWindow`](https://developer.apple.com/documentation/appkit/nswindow).

Wine/DXGI/D3D12 integration can access public swapchain backbuffers and schedule work through public D3D12 interfaces. For example, DXGI exposes [`IDXGISwapChain::GetBuffer`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/nf-dxgi-idxgiswapchain-getbuffer) and [`Present`](https://learn.microsoft.com/en-us/windows/win32/api/dxgi/nf-dxgi-idxgiswapchain-present); D3D12 exposes [shared handles for D3D12 resources and fences](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-createsharedhandle). That is a different resource contract: a D3D12 resource/shared handle is not a Metal texture or `MTLSharedTextureHandle`. In the public interfaces reviewed, we found no documented conversion from a D3DMetal swapchain resource to an `MTLTexture`, no D3DMetal callback returning the matching Metal drawable, and no GPTK/D3DMetal API for an adapter to cancel/reorder an unmodified game's presents. Step 8C separately validates a cooperative D3D12 resource/presentation path on GPTK 4, but neither a D3D12 inference backend nor an adapter for an unmodified Wine game was validated; this result does not validate this repository's Metal texture adapter.

This distinction matters for a controlled harness: a D3D12 app that owns its own swapchain can use public D3D12 compute and `Present` calls to request A/G/B without any D3DMetal-specific knowledge. Step 8C demonstrates that cooperative resource, GPU-work, and presentation path through Highball GPTK 4, with monitor counters and HUD timing. It does not prove that Framegen can attach to or replace presentation in an unmodified GPTK app. A transparent Wine DLL override/interposer was not built or tested; the existence of public COM interfaces alone does not establish a robust interception contract across all games.

## Answers to the requested questions

| # | Question | Finding for an external adapter on an unmodified GPTK/D3DMetal app |
|---|---|---|
| 1 | Identify the game's relevant Metal presentation surface | **UNVERIFIED for a D3DMetal app.** In-process AppKit can enumerate generic app windows and view layers, but the reviewed public material does not map those objects to the final D3DMetal frame or its DXGI swapchain. In-process mapping/interposition was not tested. |
| 2 | Copy the final image to a library-owned `MTLTexture` | **Conditional only.** With renderer-provided texture access and a producer completion dependency, a GPU copy into an owned texture is viable. Explicit Metal sharing also works when the source creates and exports the resource. Neither path supplies an arbitrary game's frame. |
| 3 | Release renderer-owned drawables promptly | **Conditional only.** Once a source drawable is accessible, encode the copy, establish GPU ordering, and release the drawable promptly. No external route to acquire that drawable was found. |
| 4 | Coordinate presentation timing | **Only for an owned layer.** Display-link callbacks and timed presentation are public. They do not control the game's D3DMetal presentation schedule. |
| 5 | Insert an extra generated frame | **Not established for a transparent adapter by the reviewed public APIs.** A cooperative renderer can schedule an additional texture through its own presentation loop. |
| 6 | Avoid double-presenting source frames | **No documented adapter control was identified.** This needs authority to defer, suppress, or replace the source present. |
| 7 | Survive resize | **UNVERIFIED for a D3DMetal swapchain.** AppKit exposes generic window resize notifications in-process. The required mapping to swapchain recreation, GPU resources, and safe rebinding was not tested or found in the reviewed public contract. |
| 8 | Survive fullscreen/windowed transitions | **UNVERIFIED for a D3DMetal swapchain.** AppKit exposes generic fullscreen notifications in-process; mapping them to D3DMetal presentation/resource ownership is untested. |
| 9 | Survive focus loss/recovery | **UNVERIFIED for a D3DMetal swapchain.** AppKit exposes generic key-window/focus state in-process; pairing it with the relevant swapchain and presentation state is untested. |
| 10 | Support multiple swapchains/windows safely | **UNVERIFIED.** AppKit can enumerate app windows in-process, but the reviewed public material does not associate each window with the correct D3DMetal swapchain, source resource, and presentation owner. |
| 11 | Avoid screen capture | **Yes, with cooperation.** Renderer-supplied GPU resources provide a non-capture path. Without that access, the allowed API set supplies no live source image. |
| 12 | Avoid CPU image readback | **Yes, with cooperation.** Metal copies, compute, shared textures, and shared events are GPU-native. Explicit cross-process sharing requires the producer to participate. |

## Decision and missing public contract

The direct adapter route is **BLOCKED pending a public hook**. The exact missing contract is a supported per-application/per-swapchain integration interface that provides all of the following:

1. A stable swapchain/window identity and the final rendered source as a Metal-readable or explicitly importable GPU resource, together with producer completion/synchronization and texture metadata.
2. Presentation authority for that same swapchain: defer or suppress the original present, submit source/generated output at chosen times, and receive actual presentation completion so original frames are not double-presented.
3. Lifecycle notifications for swapchain recreation, resize, fullscreen/windowed changes, focus loss/recovery, and multiple windows, with a safe way to rebind resources.

No supported transparent implementation using the reviewed public material was identified. In-process AppKit/DXGI interposition was not built or tested in Step 8, so this report does not rule out every such technique; it leaves the direct Metal adapter blocked pending a supported resource/presentation contract or a separate public-API experiment that validates one. Step 8B evaluates a cooperative D3D12 harness but not a transparent Wine adapter; either D3D12 route would remain separate from this Metal texture contract. The D3DMetal adapter boundary remains unimplemented, and RIFE remains disconnected from this route.

## Runtime and soak evidence

The original direct Metal-texture adapter path was **NOT RUN**. Step 8C separately ran synthetic GPU-generated G frames through a cooperative D3D12 swapchain on GPTK 4 at 30→60. The following remain **NOT RUN** or unverified:

- A direct `MTLTexture` interpolation/output path or RIFE backend under GPTK 4.
- 60→120, because the tested display exposes only 60-Hz modes.
- The 30-minute resize, fullscreen/windowed, focus loss/recovery, and varying-source-FPS soak.
- Long-term drawable starvation and memory-growth behavior. Step 8C observed no Present/fence failures or crash in its short run, but the harness exposes no drawable-starvation counter.

The existing standalone Metal host and scheduler simulation exercise library-owned resources and policy; they do not substitute for the cooperative D3D12 display evidence in Step 8C or establish a transparent Metal adapter.

## Independent boundary review

An independent GPT-6 Astra Max reviewer audited the Step 8 documentation diff against the public-interface boundary, before the separate Step 8B work. It found no accidental reliance on private D3DMetal implementation details and approved the distinction between the not-yet-tested cooperative D3D12 design at that time and the blocked current Metal adapter. The reviewer also confirmed that this audit did not establish runtime feasibility or complete the requested soak.
