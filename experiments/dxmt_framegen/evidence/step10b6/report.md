# Step 10B.6 — Apple Display calibration and DXMT/Wine presentation path

Captured 2026-10-04 on Xcode 27.0 (27A266a), macOS 27.0.1 (26A434), MacBook Air with Apple M3 and a 2940×1912 built-in display at 60 Hz.

## Result

**Step 10B.6: PARTIAL PASS. RIFE: NOT CLEARED.** The native controls establish that `displayed-surfaces-interval` rows and `Frame N` labels do not map one-to-one to application drawables. The fresh, windowed F-policy run still differs substantially from both native controls in the Display instrument: 196 Wine-attributed surface interval rows (9.6 rows/s), longer exported `Duration` values, and no direct-to-display rows. Renderer feedback also includes 44 zero `presentedTime` values and uneven positive timestamp spacing. This supports a presentation-path concern, but does not prove a count of dropped physical frames. The fullscreen F-policy case remains untested because the frozen candidate refuses to enable Framegen on fullscreen swapchains.

The DXGI fullscreen transition succeeded and stayed visible and foreground. The frozen candidate then declined Framegen in fullscreen by design: `framegenSourceSupported` returns false when the swapchain is not windowed. The fullscreen trace is therefore an ordinary 30 fps DXMT/Wine run, **not** a matched fullscreen F-policy test. Fullscreen F behavior remains **NOT TESTED**.

Keep the F scheduler and runtime source frozen. Do not implement RIFE or start the 15-minute soak. The current windowed F path remains a significant red flag; the experiment does not meet the user's architecture-wide stop condition because fullscreen F could not run. Do not create Step 10B.7 just to extend this measurement.

Claim labels below mean:

- **VERIFIED** — directly present in run logs, identities, source, or exported tables.
- **SUPPORTED INFERENCE** — evidence supports the interpretation, but does not establish it directly.
- **NOT TESTED** — no qualified measurement was made.
- **UNSUPPORTED** — the evidence cannot establish the claim.

## Frozen identity and scope

The runtime was copied from the preserved Step 10B.5 F-equivalent candidate. DXMT source commit, source-tree and downstream-diff hashes, Wine/runtime files, and app hashes are in each run's `run-identity.txt`. The qualified windowed run used a new engine copy and a new Wine prefix. The fullscreen attempt used a separate fresh copy and the same app binary and runtime hashes.

| Item | Identity |
|---|---|
| DXMT commit | `fb4515681daefb789a4d0f403c4bdbca88f3b3de` |
| Downstream source tree SHA-256 | `664aaad0d4611f8272d9d2857e236cce2101dd012fb2daea08741765e6608551` |
| Downstream diff stream SHA-256 | `c1dc834baba26b7d3f54daf9f18d8c98fb06b287effc17490947ea389db1b96e` |
| Policy | F-equivalent midpoint policy; no Candidate-I early-B advancement |
| DXMT D3D11 motion probe source / binary | `44cb20795fd162df0fb8e1fa058ea8db4c6496b8f555a2c0335fb93750a6736d` / `a9979b0521a5bf49c47cd7e027408dcadcd3e7f1cd3167cc1dd45e2a3b31bc49` |
| Native control source / built binary | `4143b77b0425c053822fcec67bb78a521c4e1359e83c5a094578e0bc0c76f60f` / `55d58f177d9ffe05935288e98ca6ea479bd55f8fff8962095a5e4492e731a376` |
| `d3d10core.dll` / `d3d11.dll` / `dxgi.dll` | `786d42842609a07bd63409def87ddeb1ef29471ddc5853352b5f6040f320e2be` / `a8a80faac6231fc3e997702c05f838d69752c05739087b8d2ad07a00b072fecc` / `086f0c433a191cc20967682ed437e62b0f1a5c2d6c641bd41732808c06c3bc7f` |
| `winemetal.dll` / `winemetal.so` | `abd5ff1e9c0873513c778736596d848037e057784b081ccd430c6070fca7aa75` / `8079c6779f401727d3ec22b51b4b2837d786363b910f61ee77077b7d28f0f68f` |
| Wine / wineserver / external `libinotify` | `1b992a3e0bc5f2a058a24f923832aaa6e464d44766fb0ad13054797e02060d10` / `6dfe1f9d2d8a67cc6a09a57966f5ef88fd461abe7321d6fb0d4a1672e8ff0350` / `0565014c42c3506fb5635aeb12f09bad3a4eef8f7ff3e6c9cf9d9e9e6b339df8` |

The full identities and per-run SHA-256 manifests are in [native-motion60](native-motion60/), [native-source30-to60](native-source30-to60/), [dxmt-windowed](dxmt-windowed/), and [dxmt-fullscreen-unqualified](dxmt-fullscreen-unqualified/). The fullscreen run's runtime hashes were checked against the same frozen identity before its temporary engine was removed. There were no DXMT runtime, scheduler, Highball, or RIFE source changes. The only code changes are this step's diagnostic controls and trace analysis helper.

## Native Metal control

The standalone [native control](native_display_control.mm) uses public APIs only. A buffered `NSWindow` owns a layer-backed parent `NSView` and a child view whose backing layer is `CAMetalLayer`. It targets the Apple M3 device, uses a 640×360 drawable with BGRA8Unorm, opaque content, `framebufferOnly=NO`, `displaySyncEnabled=NO`, `preferredFrameLatency=1`, and a fixed 60/60/60 Hz range. The callback renders to `CAMetalDisplayLinkUpdate.drawable`; it does not call `nextDrawable`. Each tick clears a deterministic, changed GPU color, registers a `presentedTime` handler and a GPU completion handler, commits the command buffer, then calls `drawable.present()`.

The 60-motion mode changed to a distinct color on every callback. The 30→60 mode emitted the deterministic `A, G, B, G, …` sequence: 30 source colors and 30 midpoint colors per second, all rendered by Metal. The control logged callback and native frame IDs, public drawable ID, callback and target timestamps, commit and present-call times, GPU completion, presented-handler time, `presentedTime`, and source/output IDs. Full configuration is in each `native-config.txt`; per-callback records are in `native-timing.csv`.

| Property | Captured native value |
|---|---|
| Window/view | 640×360 content; titled, closable, miniaturizable, resizable; parent layer-backed view and child CAMetalLayer |
| Device / display | Apple M3 / built-in display ID 1, 60 Hz |
| Layer | BGRA8Unorm, 640×360 drawable, contents scale 2.0, opaque, framebuffer-only false, display-sync false |
| Other public getters | maximumDrawableCount 3; allowsNextDrawableTimeout true; presentsWithTransaction false; sRGB color space; EDR false |
| Display link | preferred latency 1 requested; min/max/preferred frame rate 60/60/60 |

The configuration file reports numeric `window_backing_type=3`, while the control requested `NSBackingStoreBuffered`; that numeric getter value is recorded without interpreting it. The native temporary executable was removed after the trace tables and logs were exported and hashed.

## Native calibration results

The trace analyzer uses linear-interpolated percentiles at rank `(n - 1) * p`. Rates named `gap_rate_hz` are `(N - 1) / span`, not physical FPS. VSync is grouped by the `display-name` associated with the app's surface rows; this avoids summing two display streams in the fullscreen trace.

| Run / exact request window | Callbacks / GPU complete / positive `presentedTime` | CA requests / CA handlers | Built-in VSync | App-attributed Display rows | Exported `Duration` p50/p95/p99/max | Frame-label steps | Direct to Display |
|---|---|---|---:|---:|---|---|---|
| Native, 60-changing frames; 20.666 s | 1,241 / 1,241 / 1,241; all matched feedback positive | 1,241 (~60.000/s) / 1,240 (~59.999/s) | 1,237 (~60.001/s) | 773, 37.525 rows/s | 33.333 / 33.333 / 33.333 / 33.333 ms | 1:306, 2:465; median 2 | Yes:772, No:1 |
| Native, 30-source→60-output; 20.583 s | 1,236 / 1,236 / 1,236; all matched feedback positive | 1,236 (~60.001/s) / 1,235 (~60.001/s) | 1,230 (~59.952/s) | 683, 33.317 rows/s | 33.333 / 33.333 / 33.333 / 83.332 ms | 1:136, 2:544, 4:1; median 2 | Yes:682, No:1 |

Across each approximately 28-second app run, the native log recorded 1,677 / 1,678 callbacks, 1,677 / 1,678 GPU completions and handlers, and 1,676 / 1,677 positive `presentedTime` results (one zero in each full app run). Every frame in the 60-changing trace window had a unique color and adjacent frames changed color. The 30→60 trace window contained 309 A, 618 G, and 309 B outputs, with 1,236 unique colors and 1,235 adjacent changes.

**VERIFIED calibration:** 1,241 requests do not produce 1,241 Display surface rows in the 60-motion control; nor do 1,236 requests produce 1,236 rows in the 30→60 control. Both controls changed content on every callback/output. Thus the exported `displayed-surfaces-interval` table is not a one-row-per-submitted-drawable ledger. The label increments are not an application drawable counter: native controls mostly advance by two while callbacks occur at 60 Hz. The instrument schema provides no public drawable-attempt ID that joins these rows to `drawableID` or a DXMT presentation sequence.

This is an empirical limitation, not a claim that the instrument is useless. Apple describes the Display instrument as tracking display and vertical-synchronization events, and shows how display instances and skipped VSync events can help identify stutter. Its public documentation does not define this exported row or the text `Frame N` as one physical scanout or one app drawable. See [Apple's Metal performance guide](https://developer.apple.com/documentation/xcode/analyzing-the-performance-of-your-metal-app).

## Matched windowed F-policy DXMT trace

This is the qualified primary DXMT measurement. It used a fresh engine copy and prefix, the frozen runtime hashes above, a visible foreground 640×360 window, generation enabled, source content changing on every D3D Present, and one stable owner epoch. A synchronized supervisor waited for the app's `measurement_ready` marker, then recorded a full 20-second trace while the 50-second app run remained active.

**VERIFIED run ledger:** 1,501 app Presents over 49.975 s (30.035/s); all 1,501 were visible and foreground. The callback-tick ledger passed with 2,999 unique ticks/terminals (60.009/s) and no delta. There was one layer-owner claim and one release. The summary records 1,500 accepted source IDs and 1,500 safe terminal IDs, with lost=0, unsafe=0, source regressions=0, malformed requested/displayed pairs=0, and GPU failures=0. Separately, 47 accepted source presentations had unconfirmed `presentedTime` feedback and one terminal record was orphaned; those caveats limit this to a bounded steady-state accounting result, not lifecycle qualification. The full run had 1,509 source submissions and 1,482 generated submissions/completions; generated outputs were dropped before submit for 18 unique pairs, but there were no generated submission failures. Full ledger: [summary.txt](dxmt-windowed/summary.txt).

The trace window has a strong ordered timestamp alignment between 1,237 CA requests and 1,237 DXMT present-call feedback records (median clock offset; p95 residual 0.302 ms, max 11.748 ms). The maximum is one timing outlier, so this is not a perfect per-request match. The window contained 623 source and 614 generated submissions; all 1,237 had GPU-completion feedback. Within that window, 1,193 `presentedTime` values were positive and 44 were zero. Callbacks counted within the request window were 1,238 (~59.920/s). The near-60/s CA request/handler chain is useful to verify the path, not an independent physical-display count.

Positive `presentedTime` spacing is measured between successive positive timestamps within each listed sequence; it is not a physical scanout interval. The long tails are additional renderer-feedback evidence, while zero remains ambiguous under Apple's API contract.

| Sequence | Submissions | Positive / zero | Positive timestamp gap p50/p95/p99/max | Gaps >25 ms |
|---|---:|---:|---|---:|
| All feedback, ordered by timestamp | 1,237 | 1,193 / 44 | 16.666 / 45.166 / 46.216 / 63.187 ms | 167 of 1,192 |
| Source | 623 | 604 / 19 | 33.333 / 62.269 / 63.825 / 79.649 ms | 461 of 603 |
| Generated | 614 | 589 / 25 | 33.333 / 62.723 / 71.260 / 113.186 ms | 463 of 588 |

| Measurement | Result |
|---|---:|
| CA present requests / handlers | 1,237 (~59.808/s) / 1,237 (~59.891/s) |
| DXMT feedback in aligned request window | 1,237 records; GPU completed 1,237; `presentedTime` positive 1,193, zero 44 |
| Built-in display VSync | 1,225 over the app surface window, ~59.952/s |
| Wine-attributed `displayed-surfaces-interval` rows | 196 over 20.416 s; 9.600 rows/s |
| `Duration` p50/p95/p99/max | 116.665 / 316.663 / 434.162 / 466.661 ms |
| Separate CPU-to-display-latency p50/p95/p99/max | 65.845 / 66.519 / 66.562 / 99.897 ms |
| Frame-label steps | median 6; p95 19; max 29 |
| Direct to Display | No:196; Direct-to-Display failing-reason text `display rejected`:181, empty:15 |

**SUPPORTED INFERENCE:** the windowed DXMT path differs materially from these native controls at the compositor/surface-instrument level: 196 app-attributed surface interval rows (9.600 rows/s), all `Direct to Display=No`, versus 773 and 683 rows in the native controls, most native rows direct-to-display. This is evidence to investigate the Wine/DXMT view/layer path. It does not prove that 57 submitted drawables were dropped per second, that the 44 zero `presentedTime` values are all drops, or that a particular physical scanout was missed. The text `display rejected` belongs to `Direct to Display Failing Reason`; it does not mean that Metal's application `present()` call was rejected.

## Fullscreen qualification attempt — not a matched F run

The same app binary and frozen runtime were used with `DXMT_FRAMEGEN_START_FULLSCREEN=1`. The harness called `IDXGISwapChain::SetFullscreenState(TRUE)`. It logged `GetFullscreenState=1`, visible=1, iconic=0, foreground=1 at `measurement_ready` and before process exit. The mode-set log reports 1470×956 at 60 Hz. The 20-second trace itself is valid, but the F workload is not.

The run log says `requested=1 occluded=0 source_supported=0 layer_owner=0`; no DXMT `native.csv` was created. In the downstream candidate, [`framegenSourceSupported`](/private/tmp/fg-metal-step10b5-downstream/src/d3d11/d3d11_swapchain.cpp:1455) returns false when `!fullscreen_desc_.Windowed` (the gate is at lines 1460–1463). The runner therefore exited 1 after correctly refusing to certify a run without the native Framegen log; the D3D app itself exited 0. No source-safety or owner-epoch claim applies to this mode because Framegen never started.

For context only, the ordinary fullscreen run made 1,501 app Presents in 49.975 s. In the exported 20-second trace, CA requests were ~29.994/s, app-attributed surface rows were 621 (~30.049/s), their `Duration` p50/p95/p99 was 33.333 ms, all 621 were `Direct to Display=Yes`, and all adjacent label steps were 1. Built-in display VSync was ~59.952/s. These are **NOT** fullscreen F-policy results and are excluded from the matched windowed/fullscreen decision. They only establish how this ordinary, no-Framegen fullscreen run was reported.

The DXMT fullscreen path changes Win32 style/geometry and requests a display mode; it does not call AppKit `toggleFullScreen:` in the audited source. The absence of a qualified fullscreen F run leaves the user's windowed-versus-fullscreen F question **NOT TESTED**; no new source candidate was built to bypass the explicit support gate.

## Table semantics and evidence hierarchy

The selected Xcode 27 schemas are retained in each run's `xctrace-toc.xml` and XML exports.

| Export / field | What this evidence supports | What it does not support |
|---|---|---|
| `display-vsyncs-interval` | VSync request samples on the named display; count/rate must be grouped by display name | That the app's surface updated or was physically scanned on every VSync |
| `displayed-surfaces-interval` | Start, **Duration**, separate **CPU to Display Latency**, surface ID/name, and Direct-to-Display state/reason | A unique drawable/frame attempt or physical scanout record; native controls disprove one row per drawable |
| Label text `Frame N` | Empirical sequence labels; native controls mostly step 2 while presenting on every callback | A documented app frame counter or proof that step 6 means six dropped frames |
| `Direct to Display Failing Reason` text `display rejected` | Direct-to-display path was not used/available for that instrument row | That Metal/Wine rejected the app's `present()` request |
| `ca-client-present-request` | Core Animation request chain; timestamps align to app presentation calls | Independent evidence that the panel displayed each frame |
| `ca-client-presented-handler` | Core Animation presented-handler chain | A per-drawable physical-scanout counter |
| `MTLDrawable.presentedTime` | Apple's public framework feedback; positive values are framework-reported onscreen times | A physical panel sensor measurement. Apple documents zero as “not presented or associated frame dropped,” so zero is ambiguous. |

For the native traces, every callback-aligned `presentedTime` in the selected trace window was positive. For the matched DXMT windowed trace, 44/1,237 were zero. This is a framework-feedback difference and a useful warning; it is not by itself a calibrated physical-drop count. Apple documents the zero behavior and presented-handler API in [`MTLDrawable.presentedTime`](https://developer.apple.com/documentation/metal/mtldrawable/presentedtime). The display link's requested latency may also be realized at greater latency in windowed macOS mode ([`preferredFrameLatency`](https://developer.apple.com/documentation/quartzcore/cametaldisplaylink/preferredframelatency)).

## Public layer/window comparison

| Property | Native control | DXMT/Wine test path | Evidence status |
|---|---|---|---|
| View/layer hierarchy | Layer-backed parent NSView with child CAMetalLayer | Pinned Wine source uses layer-backed `WineContentView` and child `WineMetalView` with its CAMetalLayer | Source-verified; installed Sikarugir Wine 10.0 was not byte-matched to the pinned Wine source |
| Device | Apple M3 | Presenter sets the D3D device on its CAMetalLayer | Native getter verified; DXMT value is source-level, no live getter recorded |
| Pixel format | BGRA8Unorm | 640×360 D3D11 swapchain uses DXGI RGBA8; pinned DXMT mapping produces Metal BGRA8 | Source/config verified; live DXMT layer getter not captured |
| Drawable size / contents scale | 640×360 / 2.0 | Set from swapchain dimensions; Wine Retina scale is multiplied by DXMT scale factor | Native getter verified; DXMT actual values not queried |
| Opaque | Window, parent, and Metal layer are opaque | DXMT sets CAMetalLayer opaque=YES | Source/config verified |
| `framebufferOnly` | NO | Wine initially sets YES in the audited source; DXMT changes it to NO | Source/config verified |
| `displaySyncEnabled` | NO | DXMT sets NO | Source/config verified |
| Display link | requested latency 1, fixed 60/60/60 range | downstream Framegen implementation requests latency 1 and 60 Hz | Source-level DXMT; ~60 Hz callbacks verified, live link-property getter not available |
| maximum drawable count / timeout / transaction | 3 / true / false | Not set or measured through the public DXMT bridge | NOT TESTED on DXMT |
| Color space / EDR | sRGB / EDR false | SDR DXGI color space is propagated; DXMT has HDR/EDR paths | Native getter verified; live DXMT values not queried |
| Window backing | requested buffered; getter returned raw numeric 3 | Pinned Wine source creates a buffered NSWindow; runtime getter not captured | Native numeric recorded; DXMT runtime NOT TESTED |
| Fullscreen | native control remained a normal titled window | DXGI mode-set changes Win32 style/geometry; it is not AppKit `toggleFullScreen:` | Source and run log verified |

Apple's macOS Metal-window guidance describes full-screen, opaque RGB content on Apple silicon as the common direct-to-display route and recommends verifying it in Instruments. The native control was **not** AppKit fullscreen yet mostly reported `Direct to Display=Yes`; therefore these conditions are guidance, not a substitute for the observed per-surface flag. See [Managing your game window for Metal in macOS](https://developer.apple.com/documentation/metal/managing-your-game-window-for-metal-in-macos). The window/layer hierarchy and property differences remain plausible investigation targets, not proven causes.

Source references: [DXMT presenter properties](https://github.com/3Shain/dxmt/blob/fb4515681daefb789a4d0f403c4bdbca88f3b3de/src/dxmt/dxmt_presenter.cpp#L11-L53), [WineMetal CAMetalLayer property bridge](https://github.com/3Shain/dxmt/blob/fb4515681daefb789a4d0f403c4bdbca88f3b3de/src/winemetal/unix/winemetal_unix.c#L1623-L1653), [DXMT HWND-to-Metal-view bridge](https://github.com/3Shain/dxmt/blob/fb4515681daefb789a4d0f403c4bdbca88f3b3de/src/winemetal/unix/winemetal_unix.c#L1561-L1618), [pinned Wine view hierarchy](https://github.com/3Shain/wine/blob/4dd8a2d6dca39a116d0911f247bbaf1ad5e29471/dlls/winemac.drv/cocoa_window.m#L615-L626), and [pinned Wine Metal view](https://github.com/3Shain/wine/blob/4dd8a2d6dca39a116d0911f247bbaf1ad5e29471/dlls/winemac.drv/cocoa_window.m#L871-L908).

## Source safety, experiments not run, and decision

**VERIFIED for this bounded steady-state windowed run:** the F-policy callback ledger passes; one layer-owner epoch; no accepted source IDs were classified as lost or unsafe; malformed pairs, source regression, GPU failures, and generated submission failures were zero. The app remained visible and foreground. The ledger also reports 47 accepted source presentations with unconfirmed `presentedTime` feedback, zero lost IDs among them, and one orphan terminal record. These results describe this normal run only; they do not establish lifecycle or failure-path safety.

**NOT TESTED:** a fullscreen F-policy source-safety run, public runtime getters for the DXMT layer, individual physical-scanout correlation, any one-variable DXMT layer/window change, and failure injection/lifecycle regressions. The run manifest records `lifecycle_test=0`, `skip_resize_test=1`, and `failure_injection=none`. No renderer or layer setting was changed in the tested candidate, so the failure matrix was not rerun. The failed fullscreen eligibility check is recorded separately; it is not a failure-injection regression.

**Adversarial review reconciled:** the ordered CA-to-DXMT alignment has one 11.748 ms maximum residual; positive feedback timing is uneven for both source and generated submissions; 47 accepted sources lack positive feedback in this run; and one orphan terminal record exists. The reviewer also confirmed that the ordinary fullscreen trace is not an F-policy run, the Display label is not a drawable ID, DXMT maximum-drawable-count/timeout/transaction values were not recorded, and physical scanout remains untested. These caveats are included in the claims and tables above.

**Decision:** native calibration weakens the Step 10B.5 claim that Wine Display rows directly count dropped output frames, but the matched windowed DXMT measurements remain materially worse than both native controls at the surface-instrument level and in `presentedTime` feedback. The explicit fullscreen support gate prevents a matched fullscreen F comparison. Therefore Step 10B.6 is a **PARTIAL PASS as a calibration experiment**, the currently tested windowed F presentation gate is **FAIL/NOT CLEARED**, and **RIFE remains NOT CLEARED**. Physical panel scanout and the fullscreen F compositor outcome remain **UNSUPPORTED/NOT TESTED**.

The [Step 10B.5 report](../step10b5/report.md) received a short erratum correcting its `Duration` versus CPU-latency labels; it remains otherwise preserved. Disposable failed/partial captures and run engine/prefix copies were removed only after retained evidence was exported and hashed. The frozen engine seed, source checkouts, checked-in harnesses, and evidence remain; free space after cleanup was approximately 34 GiB.
