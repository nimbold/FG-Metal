# HUD preservation

HUD handling is a host-visible part of the frame-generation contract. The default is **no HUD knowledge**. Hosts can select explicit scene/UI input or automatic protection for composited input. The modes and UI-source policy are exposed by `framegen_context_set_hud_options`; debug visualization is disabled by default.

## Modes

| Mode | Host input | Placeholder behavior |
| --- | --- | --- |
| No HUD knowledge | One composited color image | Blend the complete image at the requested interpolation fraction. |
| Explicit UI plane | Scene-only color plus a matching UI texture for each source | Blend scene color, then composite the selected endpoint UI using its alpha mode. |
| Automatic protection | One already-composited color image | Estimate a soft screen-space confidence mask, stabilize it on the GPU, and mix protected pixels from the selected endpoint into the generated image. |

Explicit mode is preferred. Configure the context with `FRAMEGEN_CAP_UI_PLANE` in `preferred_capabilities` and `available_input_capabilities`, then set the HUD mode before submitting source frames. Attach a `ui_texture` to every explicit-mode source frame. The placeholder currently accepts an opaque scene plane and matching RGBA8 SDR UI planes. Scene and UI may each declare sRGB or linear-sRGB transfer, and each plane is decoded from its own metadata for linear-light composition. Straight, premultiplied, and opaque UI alpha are supported; alpha supplies the soft coverage. The explicit mode composites after scene interpolation, so the UI pixels are not themselves interpolated.

Automatic mode is a per-pixel heuristic, not a general motion estimator. It combines source-pair screen-position stability, sharp-edge and local high-frequency structure, disagreement between a pixel's change and its four-neighbor change, and recurring change at a previously confident fixed position. The neighborhood signal is only a scene-motion proxy; it does not estimate optical flow. The mask uses a soft confidence ramp, faster engagement than release, time-aware smoothing, and a one-pixel confidence feather. History advances only for a new chronological source pair; repeated requests for the same pair are evaluated idempotently, and an older pair cannot overwrite the forward mask history. A mode change, history reset, resolution change, clock-domain change, or source color-metadata discontinuity starts fresh mask history. The host must request a reset after a cut or seek.

## UI timing and debug views

Protected pixels can come from the previous source, current source, or the source nearest the requested presentation timestamp. An exact nearest-time tie selects the current source. The choice affects dynamic UI: previous can show a stale state, current can show the next state early, and nearest follows the requested presentation time. It does not remove the half-frame tradeoff for moving UI when only endpoint samples are available.

The optional debug views are raw HUD confidence, stabilized confidence, protected regions, interpolation confidence, and final composite. Automatic mode displays its raw or stabilized detector mask. Explicit UI-plane mode displays the supplied UI alpha for both raw and stabilized mask views. In the placeholder automatic mode, “interpolation confidence” is the inverse of HUD protection confidence; it is not an optical-flow or motion-model confidence. A debug view replaces the generated image while enabled; disable it for normal output. This keeps readback optional and lets a host display the selected internal view without adding CPU mask transfers.

The mask itself has a separate temporal diagnostic. `measure_mask_stability.py` runs the five consecutive synthetic midpoint pairs in one backend session, compares adjacent raw-mask and stabilized-mask debug images, and measures detection precision/recall against the authored midpoint HUD ROI at a confidence threshold of 0.5. That binary ROI is an evaluation proxy for intended protected pixels, not ground-truth confidence. Lower mask transition difference can mean smoothing or slower response, so the detector metrics and coverage must be considered alongside it.

## Placeholder benchmark

The committed synthetic fixture includes a static crosshair and exact crosshair-stroke mask, static `CLEAR` text and exact glyph-pixel mask, minimap, health bar, subtitle, counter, timer, scrolling text, flashing UI, transparent UI, and a moving menu, alongside scene motion, foliage, particles, thin geometry, and an occluder. UI detection does not consume authored masks; masks are evaluation-only.

The checked-in runs use 128×72 synthetic frames at 60 Hz, five low-rate source pairs, requested `t=0.5` targets, three warmups, and 100 measured iterations per target on an Apple M3. Target timestamps are quantized to integer nanoseconds and passed from the analyzer to the runner so endpoint selection uses the same target time scored by the reference. The table uses the benchmark report's per-region values. Pixel-error p95 is the aggregate p95 of the five per-target spatial p95 values; residual and flicker p95 cover the report's temporal records. These are synthetic placeholder results, not game footage or renderer performance.

| Region and metric | Raw blend | Automatic, current | Change |
| --- | ---: | ---: | ---: |
| HUD pixel-error p95 | 0.2858 | 0.2690 | 5.9% lower |
| HUD temporal-residual p95 | 0.2876 | 0.2766 | 3.8% lower |
| HUD flicker p95 | 0.5752 | 0.5144 | 10.6% lower |
| All-image pixel-error p95 | 0.2107 | 0.2201 | 4.5% higher |
| All-image temporal-residual p95 | 0.2131 | 0.2222 | 4.3% higher |
| All-image flicker p95 | 0.4167 | 0.4361 | 4.7% higher |

Automatic current therefore improves the measured HUD tails, but not every image metric. Scene-region pixel-error p95 rises from 0.2094 to 0.2154, residual p95 from 0.2125 to 0.2200, and flicker p95 from 0.4157 to 0.4289. Text pixel-error p95 rises from 0.6797 to 0.7017. Moving-menu pixel-error p95 rises from 0.3616 to 0.6867, and scrolling-text p95 rises from 0.3310 to 0.4905. Thin-geometry pixel-error/flicker p95 rises from 0.3814/0.6787 to 0.4154/0.8295; weapon-sight pixel-error/flicker p95 rises from 0.3644/0.6635 to 0.3976/0.7880. Occlusion pixel-error p95 improves from 0.1869 to 0.1663, but its residual/flicker p95 worsens from 0.2104/0.4214 to 0.2713/0.5532. The translucent-UI ROI's pixel-error p95, residual p95, and flicker p95 are unchanged at 0.0915, 0.0915, and 0.1830: this fixture exposes a moving underlay, but the automatic mask does not improve it. The comparator flags the run as a regression because it applies independent gates to each region and tail metric; HUD gains do not cancel losses elsewhere.

The two static pixel guards remain exact in all four runs: crosshair-stroke and static-text pixel MAE, p95 error, residual, and flicker are all zero. Raw interpolation is already exact at those fixed pixels, so this proves stability in the controlled fixture rather than a gain over the baseline. The broader crosshair ROI includes nearby scene pixels and its p95 rises from 0.3637 to 0.3984 under automatic-current; exact stroke stability must not be read as an improvement across that larger region.

The midpoint UI-source comparison shows different choices for changing UI:

| ROI and metric | Raw blend | Previous UI | Current UI | Nearest UI |
| --- | ---: | ---: | ---: | ---: |
| Health bar pixel-error p95 | 0.1331 | 0.1265 | 0.2654 | 0.1310 |
| Scrolling text pixel-error p95 | 0.3310 | 0.4974 | 0.4905 | 0.4935 |
| Moving menu pixel-error p95 | 0.3616 | 0.6772 | 0.6867 | 0.6810 |

For this fixture, source endpoints are about 33.3 ms apart and the requested midpoint is about 16.7 ms from either source. Previous UI can show a stale state and current UI can show a later state. Nearest compares the desired presentation timestamp to endpoint presentation timestamps and resolves an exact tie to current; integer-nanosecond quantization can make a nominal midpoint a one-nanosecond near-tie. These measurements show that no one endpoint policy suits every element, and they do not measure input-to-display latency.

Endpoint selection also changes whole-image tails: raw/current/previous/nearest pixel-error p95 is `0.2107 / 0.2201 / 0.2708 / 0.2708`, temporal-residual p95 is `0.2131 / 0.2222 / 0.2771 / 0.2771`, and flicker p95 is `0.4167 / 0.4361 / 0.5292 / 0.5292`. Previous and nearest therefore preserve some HUD states while increasing error across the whole frame on this fixture. P95 also hides brief changes: the timer ROI has zero pixel-error p95 in all runs even though its maximum per-target error reaches `0.7438`.

## Known failure cases

- Stationary scenery, signs, thin geometry, fences, foliage, or world-space text can resemble a fixed HUD. False protection can select a scene endpoint and worsen temporal tails.
- Scrolling text and moving menus can snap or stutter because endpoint selection does not move glyphs between source positions. The synthetic moving-menu p95 regresses sharply under every automatic endpoint policy.
- A counter, timer, health bar, flash, fade, or newly appearing overlay can use a stale mask or the wrong endpoint state. The best endpoint differs by element; the health bar favors previous in this fixture while scrolling text has lower mean error with current.
- Temporal engagement and release constants are normalized around a 33.3 ms pair-center interval. Other cadences are scaled by elapsed time, but may need different tuning for equally responsive protection.
- An already-composited protected pixel includes the scene beneath translucent UI. Automatic protection can freeze or ghost that underlay; it cannot reconstruct the missing clean scene. The synthetic translucent panel now overlays moving scene content, but this still does not represent the variety of real compositors and materials.
- Camera jitter, exposure changes, temporal antialiasing, reflections, or cuts can change the detector's confidence. The backend does not infer scene cuts; the host must request history reset or invalidate history after cuts and seeks. At confidence `0.5`, raw-to-stabilized HUD-ROI proxy precision is `0.5239 → 0.5278` and recall is `0.2853 → 0.3119`. Stabilized coverage rises from `0.1419` on the first transition to `0.2226` on the fifth while raw coverage stays near `0.14–0.16`. The `37.3%` transition-MAE reduction can reflect smoothing or slower response; these proxy scores are not detector ground truth.

The current implementation is limited to the placeholder Metal backend. It does not integrate D3DMetal, include a renderer adapter, or establish performance or quality on real games.
