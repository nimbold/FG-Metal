# Frame generation benchmark

This benchmark evaluates generated frames against known high-rate ground truth and measures the isolated backend runner. It is designed to expose temporal shimmer, edge movement, HUD/text damage, and errors around occlusion. It does not capture a desktop, integrate with Wine, or claim game performance.

The analyzer is tools/benchmark/bench.py and uses only Python's standard library. The Metal command line runner is an offline adapter: it loads two PPM inputs, submits the backend request, waits for completion, reads back the generated texture, and emits timing/memory JSON. Readback is confined to this benchmark path.

## Build and run

Run the standard-library adversarial metric checks from a clean checkout:

~~~sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/test_benchmark_metrics.py
~~~

On a Metal capable macOS host:

~~~sh
cmake -S . -B build -G Ninja -DFRAMEGEN_BUILD_TOOLS=ON
cmake --build build --target framegen-benchmark-metal
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --output test-results/benchmarks/placeholder-synthetic-motion.json
~~~

The default run uses three warmup iterations and 100 measured iterations for each target timestamp. The default deadline is one high-rate frame period, 1000 / high_rate_fps milliseconds. The JSON result is accompanied by a Markdown summary at the same path with a .md suffix.

Regenerate the small, project-authored synthetic fixture with:

~~~sh
python3 tools/benchmark/synthetic/generate_fixture.py
~~~

The fixture contains no game footage, copied artwork, or third-party model/data assets. Its manifest records the Apache-2.0 project-authored provenance. Do not add copyrighted game captures unless their license expressly permits redistribution; private or locally licensed footage must remain outside the repository.

To request one normalized interpolation time:

~~~sh
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --t 0.5 \
  --output test-results/benchmarks/placeholder-t050.json
~~~

Without --t, the analyzer requests every available ground-truth frame between consecutive low-rate source frames. For ordinary image sequences, t is derived from source and target timestamps; a requested t must match a supplied target timestamp in each eligible interval. An analytic provider may render a target at any normalized coordinate, including values other than 0.5. Each generated target is paired with its own timestamp and mask.

## Corpus format

A version 1 manifest contains:

- sequence_id, license, positive width and height, high_rate_fps, and low_rate_stride_frames.
- Contiguous high-rate frame records with strictly increasing timestamp_ns, index, a binary P6 RGB path, and exactly four P5 masks: hud, text, scene, and occlusion.
- source_indices, which must equal every low_rate_stride_frames-th frame, beginning at the first frame. The last frame must land on a selected source frame so there is no silently ignored tail.
- Optional analytic_provider and analytic_provider_function for project-authored continuous ground truth.
- Optional per-frame segment metadata described below.

All source images and masks are validated before the backend runs. Dimensions must exactly match the manifest. Masks may contain only 0 and 255, must have at least eight active pixels by default, and may declare higher per-label minimums with mask_minimum_pixels. The result reports each input mask's pixel coverage. The labels are scored separately and may overlap: text can be inside HUD, and occlusion can be inside scene content. They are not treated as a disjoint partition.

For corpora with fixed authored masks, a frame may include mask_pixel_counts with the declared active-pixel count for all four labels. The analyzer rejects any mask whose actual count differs. This catches accidental mask erosion or truncation when regenerating fixed fixtures; mask counts and the corpus content digest remain recorded in each run.

The manifest's fixture_coverage is a descriptive inventory. A representative corpus should contain clips or synthetic scenes for slow camera pans, fast camera rotations, third-person character movement, racing, foliage, thin geometry, fences, particles, transparency, reflections/specular highlights, weapon sights, crosshairs, subtitles, minimaps, health bars, menus, rapidly changing HUD counters, scene cuts, and loading transitions. The committed fixture covers a subset using deterministic synthetic geometry, particles, a moving occluder, and a changing HUD.

### Scene cuts and loading transitions

Frame records can carry:

~~~json
{
  "segment_id": "level-2",
  "boundary_before": "scene_cut",
  "interpolable": true
}
~~~

segment_id defaults to segment-0. boundary_before is omitted except on a transition frame and accepts scene_cut or loading_transition. interpolable defaults to true; set it to false on every frame within a loading transition or another interval where interpolation is not meaningful.

The analyzer skips a low-rate source pair if its endpoints have different segment IDs, a boundary_before marker occurs inside the pair, or any frame in the pair is marked non-interpolable. Temporal history is split at each skipped pair. Frame-to-frame residuals, high-frequency flicker, and edge flicker never compare across that boundary. For a multi-frame loading transition, mark the frames as non-interpolable and give the transition its own segment ID; mark the boundary entering and leaving it. At least one eligible source pair must remain for a run.

## Metrics

Every generated frame is compared with its exact reference frame. Metrics are computed over the full image and independently over each required mask. PSNR is diagnostic only and cannot compensate for a temporal, edge, HUD, text, or occlusion regression.

- PSNR: RGB peak signal-to-noise ratio over 8-bit channels. A perfect match is represented as 120 dB with a psnr_perfect_match flag so JSON remains finite.
- SSIM: region-weighted local luminance SSIM using overlapping 7x7 box windows. Windows retain their image positions; only mask-selected pixels contribute to the local statistics and center-weighted average.
- Perceptual similarity: a project-authored score in [0,1]. It is the weighted geometric mean of masked local luminance SSIM over average-pooled image scales 1, 2, 4, and 8, with weights 0.4, 0.3, 0.2, and 0.1. That structural score is raised to 0.75 and multiplied by exp(-mean CIE76 DeltaE / 25) raised to 0.25. Higher is better. This is not LPIPS and is not claimed to be canonical MS-SSIM; no third-party metric implementation is copied.
- CIE76 color difference: mean Euclidean Delta E in CIELAB after sRGB-to-XYZ D65 conversion. Lower is better. It is a color-difference diagnostic, separate from the spatial multiscale score.
- Frame-to-frame residual: absolute RGB first-difference error between reconstructed and reference timelines, divided by actual elapsed high-rate frame periods. It includes exact source frames, which have zero image error, and uses the union of masks at both endpoints.
- High-frequency temporal flicker: reconstructed versus reference second temporal derivative over three adjacent timeline samples. Actual timestamp spacing is used for nonuniform samples. The HUD/text/scene/occlusion mask union spans all three frames. Per-window mean, p95, and worst-pixel values are retained.
- Edge stability: per-target Sobel edge maps are compared with a symmetric chamfer distance capped at six pixels, plus edge precision and recall within one pixel. A steady shifted or missing contour therefore remains visible even when its intensity does not flicker. A second derivative residual over Sobel edge magnitude reports temporal edge flicker.
- Sparse damage guards: each target and region records worst-pixel error and counts/fractions over error thresholds. The comparison gate checks the maximum per-target count of pixels with at least 1% and 5% normalized RGB error with zero tolerance, so added HUD/text pixels cannot disappear in a region-wide percentile.

Temporal residual and flicker distributions are kept per transition/window. Target-frame image metrics are kept per target before summary aggregation, and the comparison checks worst target values as well as averages. This prevents one bad frame, crosshair stroke, or subtitle pixel from being hidden by a long clean clip or by a worse error at another timestamp.

## Runtime fields and limits

The result stores p50, p95, and p99 for GPU execution time, CPU submit overhead, and completion latency. Zero GPU timing samples are counted as unavailable, rather than as zero time. The analyzer reports current and peak GPU allocation plus the runner's peak resident memory, each with target-run coverage.

Deadline misses count measured completion samples above the deadline. dropped_generated_frames_estimated repeats that count as a serial-runner estimate; it is not an observed presentation drop. source_only_input_throughput_fps measures wall time for uploading the same two source textures per cycle. source_with_generation_input_throughput_fps reuploads those two textures and includes backend submit-to-completion in wall time. Their percentage difference is labeled an offline input-throughput proxy. It excludes renderer work, game FPS, and presentation. If either sample is missing, impact is null and the result explains why.

The runner serializes submissions and waits for completion, so its timing does not model a renderer's queue overlap, display cadence, pacing policy, or game-frame FPS. Keep results on the same host and with matching warmup, iteration, deadline, corpus, target timestamps, and metric settings for meaningful comparisons.

## Compare runs

Create a candidate with the same corpus and measurement configuration, then compare:

~~~sh
python3 tools/benchmark/bench.py compare \
  --baseline test-results/benchmarks/placeholder-synthetic-motion.json \
  --candidate test-results/benchmarks/candidate-synthetic-motion.json \
  --output test-results/benchmarks/placeholder-vs-candidate.json \
  --summary test-results/benchmarks/placeholder-vs-candidate.md
~~~

The comparison rejects differences in corpus content, target timestamps, host, warmup/iteration/deadline configuration, or metric parameters. It also rejects results that omit any required region or quality metric. Backend identity may differ, allowing comparisons such as raw RIFE versus stabilized RIFE, HUD-protected variants, Core ML, MPSGraph, future models, and renderer adapters.

Adapters may provide a JSON object named backend_metadata in their runner result for model names, revisions, stabilization/HUD options, or adapter versions. The analyzer requires that object to remain stable across target timestamps and carries it into run and comparison reports. Backend metadata may differ between candidate and baseline.

Regression rules are explicit in QUALITY_RULES and RUNTIME_RULES in bench.py. Temporal residual, flicker, mask-specific image errors, edge location, and runtime tail percentiles are checked independently for every region. Per-target image metrics, every temporal transition, and every three-frame flicker window are also compared independently. Quality gains in one region or metric cannot cancel losses elsewhere. PSNR remains visible in reports but is not a gate. A comparison exits 1 if a regression is detected and 2 for invalid or incompatible inputs.

## Baseline

The initial result should be captured from a clean checkout after building the Metal runner. Use a stable, descriptive run ID and store the JSON and Markdown under test-results/benchmarks/. The placeholder blend is only a plumbing baseline; it is not an interpolation quality claim and does not establish RIFE or game performance.

The baseline should preserve its corpus content digest, manifest digest, backend identity, host details, configuration, and per-target records. Do not distribute third-party footage with the result unless its license permits redistribution.
