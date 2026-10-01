# Temporal quality controller

`TemporalQualityController` is a model-independent, opt-in post-process around a backend's generated output. The Metal implementation runs entirely on the GPU and does not read frame pixels back to the CPU. The policy defaults to `disabled`; enable it only after evaluating the target renderer and content.

## Confidence and fallback

For each pixel, the Metal pass estimates confidence from generated-frame support in the two sources and their immediate neighborhoods, local source motion, disocclusion evidence, high-frequency structure, thin-edge structure, temporal excess against the previous generated frame, and bright transient/particle evidence. A supplied HUD mask marks protected pixels as fully trusted. The `confidence`, `disocclusion`, `unstable_thin_features`, `high_frequency_texture`, `specular_or_particles`, `scene_cut`, and `confidence_classes` debug views expose the estimates; the class view uses green above 0.76, amber above 0.42, and red below 0.42, with separate colors for disocclusion, unstable thin structure, and transient outliers.

The history comparison is deliberately attenuated where source motion makes same-pixel correspondence unreliable. Motion vectors are not required or consumed by this first implementation. A moving fence is not treated as unstable merely because it moves; its generated temporal change must also exceed the source-pair evidence.

Three policies were implemented and benchmarked:

- `continuous_endpoint_blend`: blend the generated output toward the temporally nearest real endpoint as confidence falls.
- `continuous_source_blend`: blend toward the source A/B interpolation (linear-light for sRGB inputs) as confidence falls.
- `nearest_endpoint_fallback`: use the generated result except below a low-confidence threshold, where it selects the nearest real endpoint.

Two independent GPT-6 Luna (Extra High) design passes proposed confidence-driven source fallback families; endpoint, source-blend, and nearest-endpoint variants were all measured in both HUD stacks. No policy won every region, so the selected implementation is the opt-in controller with all three explicitly selectable, and no default policy.

The first two map confidence through a smooth ramp from 0.20 to 0.76. The nearest fallback threshold is 0.34. With valid history, repeated requests for an identical source pair and target reuse the same result. Older requests render without replacing the newest history. Cold startup without a reset keeps the initial generated result while seeding history.

Current Metal processing accepts matching opaque RGBA8 sRGB or linear-sRGB SDR inputs. Unsupported formats, HDR, invalid timing, or asynchronous backend failure invalidate temporal state. This support limit is explicit; the controller does not claim HDR stabilization.

## Scene cuts and history resets

A tiled GPU pass samples a downscaled grid from source A, source B, and the generated frame. It combines a 16-bin luminance histogram distance, downsampled RGB difference, changed-sample fraction, and generated residual. Several OR-ed thresholds cover both histogram-changing cuts and same-histogram structural changes. The detector does not rely on one signal alone. On a detected cut, the current output is the temporally nearest real source frame; the next valid source pair is also held to a real frame while it seeds fresh history. A focused probe verified the equal-histogram structural-change path and the following real-source recovery frame. The committed camera-rotation sequence produced no false cut flags in its five target probes.

The controller also invalidates history on changed input dimensions/format/color metadata, policy or HUD-mode changes, source-timeline discontinuities, and a source cadence change greater than 4x or below one quarter of the established cadence. The host must call the explicit reset API for known seeks, device resets, and swapchain recreation, including recreation where the texture descriptor happens to stay the same. An explicit reset presents a selected real endpoint before history is trusted again. Backend rejection/failure also schedules a reset before the next chronological request. Reset state is per `FrameGenerator` stream, so generators sharing a Metal device do not share confidence or output history.

## Benchmark and visual review

The policy matrix compared raw RIFE, RIFE with automatic HUD protection, temporal stabilization alone, and HUD protection combined with each temporal policy. Runs used Practical-RIFE v4.26 QUALITY on an Apple M3 at 128×72. Each of five interpolation targets had 3 warmups and 100 measured iterations. The synthetic fixture includes foliage, fences/thin geometry, particles, moving highlights, weapon sights, HUD/text, occlusion, and the new fast-camera-rotation layer. These results are synthetic evidence, not game footage or renderer-integrated pacing.

| Configuration | Whole-frame flicker p95 | HUD flicker p95 | Weapon-sight flicker p95 | Thin-edge recall minimum |
| --- | ---: | ---: | ---: | ---: |
| Raw RIFE | 0.6329 | 0.9271 | 1.0033 | 0.7055 |
| RIFE + endpoint blend | 0.6562 | 0.7108 | 0.8370 | 0.6090 |
| RIFE + source blend | 0.6554 | 0.7033 | 0.8370 | 0.6090 |
| RIFE + nearest fallback | 0.6565 | 0.7180 | 0.8370 | 0.6090 |
| RIFE + automatic HUD | 0.6193 | 0.6255 | 0.8208 | 0.6867 |
| HUD + endpoint blend | 0.6520 | 0.6623 | 0.8261 | 0.6090 |
| HUD + source blend | 0.6520 | 0.6573 | 0.8261 | 0.6090 |
| HUD + nearest fallback | 0.6520 | 0.6623 | 0.8261 | 0.6090 |

Lower flicker is better; higher edge recall is better. Temporal stabilization alone lowered HUD flicker p95 by 23.3% and weapon-sight flicker p95 by 16.6% for the endpoint policy. Whole-frame flicker p95 rose 3.7%, and minimum thin-edge recall fell from 0.7055 to 0.6090. Automatic HUD protection lowered whole-frame flicker p95 to 0.6193 and HUD flicker p95 to 0.6255. Adding endpoint stabilization on top raised whole-frame flicker by 5.3% (0.6193 to 0.6520) and HUD flicker by 5.9% (0.6255 to 0.6623), and reduced minimum thin-edge recall from 0.6867 to 0.6090. The six strict policy comparisons report `REGRESSION`; those results are kept as-is rather than averaged away. The selected regional gains are measurable, but they do not support a general image-quality claim.

The refreshed five-target contact sheet was inspected across reference, raw RIFE, HUD-protected RIFE, temporal endpoint fallback, and combined HUD/temporal output. Some generated variants show broken or speckled fence segments and distorted or doubled HUD text, much of which is already present with HUD protection alone. No broad additional blur or accumulated multi-frame trails were apparent, and the first temporal target visibly steps to a real endpoint after reset. A still contact sheet does not establish response delay. The separate GPT-6 Max review of the refreshed sheet reached the same visual limits and called out the combined-mode flicker and thin-edge regressions above. The direct scene-cut and reset probes verified real-frame output after a cut, explicit reset, and timestamp/timeline invalidation. These checks do not substitute for licensed real-game captures.

![Temporal quality contact sheet: reference, raw RIFE, HUD, temporal endpoint fallback, and combined output](/Users/nima/Documents/Code/FG-Metal/test-results/benchmarks/rife-v4.26-temporal-quality-rotation-contact.png)

### Result artifacts

Each policy has a full run report and a strict comparison report:

- [Raw RIFE](../test-results/benchmarks/rife-v4.26-raw-rotation-final.md)
- [RIFE + automatic HUD](../test-results/benchmarks/rife-v4.26-automatic-rotation-final.md)
- [RIFE + endpoint blend](../test-results/benchmarks/rife-v4.26-endpoint-rotation-final.md) and [comparison with raw](../test-results/benchmarks/rife-v4.26-endpoint-vs-raw-rotation-final.md)
- [RIFE + source blend](../test-results/benchmarks/rife-v4.26-source-rotation-final.md) and [comparison with raw](../test-results/benchmarks/rife-v4.26-source-vs-raw-rotation-final.md)
- [RIFE + nearest fallback](../test-results/benchmarks/rife-v4.26-nearest-rotation-final.md) and [comparison with raw](../test-results/benchmarks/rife-v4.26-nearest-vs-raw-rotation-final.md)
- [HUD + endpoint blend](../test-results/benchmarks/rife-v4.26-automatic-endpoint-rotation-final.md) and [comparison with HUD](../test-results/benchmarks/rife-v4.26-automatic-endpoint-vs-automatic-rotation-final.md)
- [HUD + source blend](../test-results/benchmarks/rife-v4.26-automatic-source-rotation-final.md) and [comparison with HUD](../test-results/benchmarks/rife-v4.26-automatic-source-vs-automatic-rotation-final.md)
- [HUD + nearest fallback](../test-results/benchmarks/rife-v4.26-automatic-nearest-rotation-final.md) and [comparison with HUD](../test-results/benchmarks/rife-v4.26-automatic-nearest-vs-automatic-rotation-final.md)

Re-run a matrix cell with the standard analyzer, changing `--hud-mode` and `--temporal-policy` for the selected cell:

```sh
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --output test-results/benchmarks/temporal-rerun.json \
  --warmup 3 --iterations 100 \
  --hud-mode none --temporal-policy endpoint-blend
```

See [benchmark methodology](BENCHMARK.md) for metric definitions and comparison gates.
