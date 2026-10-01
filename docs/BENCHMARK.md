# Frame generation benchmark

This benchmark evaluates generated frames against known high-rate ground truth and measures the isolated backend runner. It is designed to expose temporal shimmer, edge movement, HUD/text damage, and errors around occlusion. It does not capture a desktop, integrate with Wine, or claim game performance.

The analyzer is `tools/benchmark/bench.py` and uses only Python's standard library. The Metal command-line runner is an offline adapter: it loads two PPM inputs, submits the backend request, waits for completion, reads back the final measured generated texture, and emits timing/memory JSON. Readback is confined to this benchmark path. One persistent server process and Metal context serve all jobs in a run. The default 600-second timeout applies to the complete backend session; `--backend-timeout-seconds` changes it. A timed-out session is killed as a process group. The protocol caps each input line at 64 KiB, a session at 100,000 jobs and 1,000,000 total warmup/measured samples, each job at 100,000 samples, and combined stdout/stderr at 256 MiB. The session accepts the original 11-column request, a 14-column extension carrying HUD mode/UI source/debug view, and a 15-column form that also carries the analyzer's exact integer target timestamp. The analyzer uses the 15-column form so the scene interpolation fraction and endpoint UI selection share the timestamp used to evaluate the reference. The runner echoes both target timestamp fields and the analyzer checks them against the planned sample.

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

The runner can measure the placeholder HUD paths and each endpoint UI policy. For example, collect a raw baseline and an automatic-protection run at the same midpoint targets:

~~~sh
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --t 0.5 --hud-mode none --ui-source nearest \
  --output test-results/benchmarks/hud-none.json
python3 tools/benchmark/bench.py run \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --t 0.5 --hud-mode automatic --ui-source previous \
  --output test-results/benchmarks/hud-automatic-previous.json
~~~

Repeat with `--ui-source current` and `--ui-source nearest`. Nearest presentation compares the requested presentation timestamp with each endpoint's presentation timestamp; an exact tie selects the current frame. Every policy uses the same per-target HUD/text/scene quality, temporal residual, and flicker metrics. The backend options are recorded in run metadata. `--hud-debug` can select `raw-mask`, `stabilized-mask`, `protected-regions`, `interpolation-confidence`, or `final-composite`; leave it `disabled` for quality runs because a debug view replaces the generated image.

Measure temporal mask changes separately from generated-image quality:

~~~sh
python3 tools/benchmark/measure_mask_stability.py \
  --corpus tools/benchmark/corpus/synthetic-motion/manifest.json \
  --backend build/tools/benchmark/framegen-benchmark-metal \
  --output test-results/benchmarks/hud-mask-stability.json
~~~

This runs raw and stabilized mask debug views over one consecutive source-pair timeline in a persistent backend session. The report also thresholds the confidence at 0.5 and compares it with the authored binary `hud` ROI, providing a detection proxy's precision, recall, and false-positive rate alongside coverage. The authored ROI labels intended pixels for protection; it is not ground-truth confidence. Transition MAE measures temporal change and can improve simply because smoothing slows response, so review it with the proxy scores and coverage.

The default run uses three warmup iterations and 100 measured iterations for each target timestamp. For stateful, temporal comparisons, the persistent Metal server accepts exactly one sample per job and the analyzer replays the full chronological source-pair sequence for each warmup and measured pass. This lets mask history follow the same timeline on every pass; the runner does not repeatedly submit one source pair inside a server job. The default deadline is one high-rate frame period, 1000 / `high_rate_fps` milliseconds. The JSON result is accompanied by a Markdown summary at the same path with a `.md` suffix. The corpus and its assets are validated before launching the backend. Paths must resolve inside the manifest's corpus directory. Manifest, provider code, Netpbm payloads, backend output, result files, and server sessions have size bounds. The analyzer estimates its working set and rejects corpora above the default 6144 MiB `--max-analysis-memory-mib` budget before loading image assets. The estimate is deliberately conservative but is not an operating-system RSS limit; large custom corpora should be run on a host with sufficient memory. Analytic providers are executable Python source and must be trusted before running the benchmark.

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

Without `--t`, the analyzer requests every available ground-truth frame between consecutive low-rate source frames. For ordinary image sequences, t is derived from source and target timestamps; a requested t must match a supplied target timestamp in each eligible interval. An analytic provider may render a target at any normalized coordinate, including values other than 0.5. Its corpus must follow the nominal uniform cadence implied by `high_rate_fps`, because the provider receives high-rate frame coordinates. Each generated target is paired with its own integer nanosecond timestamp and mask. Interpolated provider timestamps are quantized to the nearest nanosecond; the reference coordinate and reported interpolation fraction are derived from that quantized timestamp. This keeps the scene blend, nearest-UI choice, and reference on one timeline. The placeholder accepts float32 interpolation fractions, so a target that rounds to an endpoint float is clamped to the nearest representable interior value. For very short intervals, nanosecond quantization can materially shift the effective fraction from the requested `--t`; reports record the effective fraction. Timestamp arithmetic remains integer-valued to preserve epoch-based timestamps.

## Corpus format

A version 1 manifest contains:

- sequence_id, license, positive width and height, high_rate_fps, and low_rate_stride_frames.
- Contiguous high-rate frame records with strictly increasing timestamp_ns, index, a binary P6 RGB path, and P5 masks for every label in `mask_labels`. The required labels are `hud`, `text`, `scene`, and `occlusion`; up to 60 additional named ROI labels may be added.
- `strict_pixel_labels` defaults to the mandatory `hud` and `text` regions and must include both. Add focused named ROIs such as crosshairs, weapon sights, minimaps, health bars, counters, and subtitles when those components need pointwise guards.
- source_indices, which must equal every low_rate_stride_frames-th frame, beginning at the first frame. The last frame must land on a selected source frame so there is no silently ignored tail.
- Optional analytic_provider and analytic_provider_function for project-authored continuous ground truth.
- Optional per-frame segment metadata described below.

All source images and masks are validated before the backend runs. Dimensions must exactly match the manifest. Masks may contain only 0 and 255 and must have at least eight active pixels per frame by default; `mask_minimum_pixels` can set a higher threshold by label. Masks describe evaluation regions of interest, so for intermittent subtitles or occlusions, include the screen area where the content appears instead of making the mask empty in other frames. The result reports each input mask's pixel coverage. The labels are scored separately and may overlap: text can be inside HUD, and occlusion can be inside scene content. They are not treated as a disjoint partition.

Each label in `strict_pixel_labels` retains sparse per-pixel absolute RGB channel errors for generated targets, per-pixel first-difference residuals for each timestamped transition, and per-pixel color and edge flicker for each timestamped window. Missing entries mean zero error. The comparator requires every channel and temporal error at every pixel in each strict ROI to be no worse than the baseline, with zero tolerance. This catches equal-size damage or shimmer moving from one pixel to another while regional means, p95, and maxima stay unchanged. This exact localization guarantee applies only to strict labels; equal-severity scene or occlusion errors can still move within a non-strict region while preserving regional summaries. Add focused named strict ROIs for important foliage, thin geometry, or disocclusion edges. It can flag small tradeoffs inside a strict ROI, so keep mandatory HUD/text masks focused on meaningful UI areas and add tight named ROIs for especially sensitive elements. The analyzer preflights the combined strict-map work at 250,000 pixel comparisons and caps each serialized result at 64 MiB; reduce ROI area or target count if the limit is exceeded.

For corpora with fixed authored masks, a frame may include `mask_pixel_counts` with the declared active-pixel count for every label. The analyzer rejects any mask whose actual count differs. This catches accidental mask erosion or truncation when regenerating fixed fixtures; mask counts and the corpus content digest remain recorded in each run. The memory preflight estimate includes storage for every declared mask and named ROI.

The manifest's fixture_coverage is a descriptive inventory. A representative corpus should contain clips or synthetic scenes for slow camera pans, fast camera rotations, third-person character movement, racing, foliage, thin geometry, fences, particles, transparency, reflections/specular highlights, weapon sights, crosshairs, subtitles, minimaps, health bars, menus, rapidly changing HUD counters, scene cuts, and loading transitions. The committed fixture currently covers deterministic slow panning, third-person motion, foliage and thin geometry, particles, translucent layers, highlights, weapon sights and crosshairs, subtitles, minimap, health bar, counter, timer, scrolling text, flashing and transparent UI, a moving menu, and a moving occluder. Fast rotations, racing, cuts, loading transitions, and richer subtitle behavior remain untested. See [HUD preservation results](HUD_PRESERVATION.md) for the per-policy measurements and failure cases.

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

- PSNR: RGB peak signal-to-noise ratio over 8-bit channels, capped at 120 dB. The `psnr_perfect_match` flag distinguishes exact matches from finite near-perfect scores at the cap.
- SSIM: region-weighted local luminance SSIM using overlapping 7x7 box windows. Windows retain their image positions; only mask-selected pixels contribute to the local statistics and center-weighted average.
- Perceptual similarity: a project-authored score in [0,1]. It is the weighted geometric mean of masked local luminance SSIM over average-pooled image scales 1, 2, 4, and 8, with weights 0.4, 0.3, 0.2, and 0.1. That structural score is raised to 0.75 and multiplied by exp(-mean CIE76 DeltaE / 25) raised to 0.25. Higher is better. This is not LPIPS and is not claimed to be canonical MS-SSIM; no third-party metric implementation is copied.
- CIE76 color difference: mean Euclidean Delta E in CIELAB after sRGB-to-XYZ D65 conversion. Lower is better. It is a color-difference diagnostic, separate from the spatial multiscale score.
- Frame-to-frame residual: absolute RGB first-difference error between reconstructed and reference timelines, divided by actual elapsed high-rate frame periods. It includes exact source frames, which have zero image error, and uses the union of masks at both endpoints.
- High-frequency temporal flicker: reconstructed versus reference second temporal derivative over three adjacent timeline samples. Actual timestamp spacing is used for nonuniform samples. The HUD/text/scene/occlusion mask union spans all three frames. Per-window mean, p95, and worst-pixel values are retained.
- Edge stability: per-target Sobel edge maps are compared with a symmetric chamfer distance capped at six pixels, plus edge precision and recall within one pixel. A steady shifted or missing contour therefore remains visible even when its intensity does not flicker. A second derivative residual over Sobel edge magnitude reports temporal edge flicker.
- Sparse damage guards: each target and region records worst-pixel error, normalized RGB absolute-error mean and p95, and counts/fractions over error thresholds. The comparison checks mean and p95 severity for every target with zero tolerance, plus the maximum per-target count of pixels with at least 1% and 5% normalized RGB error with zero tolerance. A target-local error increase cannot be hidden by another target's worse maximum or by remaining inside the same sparse-error bucket.

Temporal residual and flicker distributions are kept per transition/window. Target-frame image metrics are kept per target before summary aggregation, and the comparison checks severity, sparse errors, perceptual quality, and edge location independently. HUD, text, scene, occlusion, and any named ROI masks should be drawn around the content whose damage matters; masks can overlap. Strict ROIs compare per-pixel image error, first-difference residual, color flicker, and edge flicker at matching timestamps. This prevents damage or shimmer from moving to another pixel while aggregate statistics remain unchanged.

## Runtime fields and limits

The result stores p50, p95, and p99 for GPU execution time, CPU submit overhead, and completion latency. Every measured sample must have positive GPU, CPU-submit, and completion timing; a missing or zero timing invalidates the run instead of being reported as zero. It reports current GPU allocation and the highest post-completion allocation sample (not a guaranteed allocation peak), plus the runner's peak resident memory. Memory values include sample coverage per target run. Comparisons require complete timing sample coverage. If a backend reports source-only and source-with-generation throughput, it must provide both consistently for every measured job and state the measurement method; both may be omitted, in which case impact remains unavailable.

Deadline misses count measured completion samples above the deadline. `dropped_generated_frames_estimated` repeats that count as a serial-runner estimate; it is not an observed presentation drop. `source_only_input_throughput_fps` measures wall time for uploading the same two source textures per cycle. `source_with_generation_input_throughput_fps` reuploads those textures and includes backend submit-to-completion in wall time. Their percentage difference is labeled an offline input-throughput proxy. It excludes renderer work, game FPS, and presentation. The comparator treats an increase of more than one percentage point in source-frame FPS impact and any new deadline miss as runtime regressions.

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

The comparison rejects differences in corpus content, target timestamps, host and reported Metal device, warmup/iteration/deadline/timeout/memory-budget configuration, or metric parameters. It validates result structure, timing coverage, target and temporal records, and required per-region/per-target quality data before comparing. Backend identity may differ, allowing comparisons such as raw RIFE versus stabilized RIFE, HUD-protected variants, Core ML, MPSGraph, future models, and renderer adapters.

Every backend adapter must report non-empty `device_id` and `device_name` fields for the accelerator used by the run. The Metal runner uses the Metal registry ID plus the device name. A missing device identity makes a run invalid. Adapters may provide a JSON object named `backend_metadata` for model names, revisions, stabilization/HUD options, or adapter versions. The analyzer requires device and backend identity to remain stable across target timestamps and carries them into run and comparison reports. Backend identity and metadata may differ between candidate and baseline; device identity may not.

Regression rules are explicit in `QUALITY_RULES` and `RUNTIME_RULES` in `bench.py`. Temporal residual, flicker, mask-specific image errors, edge location, and runtime tail percentiles are checked independently for every region. Per-target severity metrics, every temporal transition, and every three-frame flicker window are compared independently. Each result includes a complete source-pair plan: all eligible intervals must have targets, and every skipped interval must carry reasons derived from the corpus boundary/interpolability metadata. The analyzer verifies that the full timeline contains every configured target and source endpoint, and recomputes temporal and spatial distributions from their retained records before comparison. Quality gains in one region or metric cannot cancel losses elsewhere. PSNR remains visible in reports but is not a gate. A comparison exits 1 if a regression is detected and 2 for invalid or incompatible inputs. Results are structurally validated but not signed; preserve the corpus and result artifacts as evidence if provenance matters.

## Baseline

The initial result should be captured from a clean checkout after building the Metal runner. Use a stable, descriptive run ID and store the JSON and Markdown under test-results/benchmarks/. The placeholder blend is only a plumbing baseline; it is not an interpolation quality claim and does not establish RIFE or game performance.

The baseline should preserve its corpus content digest, manifest digest, backend identity, host details, configuration, and per-target records. Do not distribute third-party footage with the result unless its license permits redistribution.
