# FG-METAL R106 — Title Derived Motion and First Elden Ring MetalFX Texture

Date: 2026-10-28

Scope: Complete one final R105 depth-access check, validate the bounded GPU-derived-motion fallback on the deterministic harness, and generate and inspect an internal MetalFX texture from real Elden Ring source frames. No title presentation path was used.

R104 remains the accepted GPU-resident harness baseline. R105's native/direct title-motion result remains `NOT FOUND`; R106 did not resume broad motion-resource hunting.

## 1. Final depth-access correctness check

The R105 candidate contract was rechecked against the installed macOS 27.0 Metal SDK at `/Applications/Xcode.app/Contents/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX27.0.sdk`. The candidate reported by R105 was `Depth32Float_Stencil8`, `MTLTextureType2DArray`, array length 1, mip 0, slice 0, single-sample, shader-readable, and stored. The SDK defines `MTLTextureType2DArray = 3` and exposes `newTextureViewWithPixelFormat:textureType:levels:slices:`. R106 implements a one-level, one-slice `MTLTextureType2D` view using the source pixel format, then samples it as `depth2d<float>`.

The storage-mode value in the R105 report needs correction: `storageMode=0` is `MTLStorageModeShared`; `MTLStorageModePrivate` is `2`. The report called value 0 private. This correction is based on `MTLResource.h` lines 58–86 in the installed SDK.

The corrected array-view sampler did not execute in the R106 window. Across the selected world window, the drawable color render passes had `depthAttachment.texture == nil` in all 112 attachment diagnostics; all 72 selected producer records reported `sameCommandBufferPasses=0`. No same-producer depth candidate was available to view or sample. Therefore this run neither validates real title depth nor semantically rejects the R105 candidate. It does close the bounded reacquisition attempt without broadening the search.

## 2. Paired depth/color semantic result

The one-shot R106 window selected 72 producer frames from source sequence 4677 through 4788. The selected producer command buffers completed successfully. No depth grid was encoded, so no `R106_DEPTH_COLOR_PAIR=CAPTURED` record exists. The matching full-resolution color capture path was ready and saved 16 consecutive BGRA8 frames, sequences 4677–4692, at 1470×956. Those images establish the observed scene, but they do not pair with a depth grid because no depth grid was sampled.

The scene in sequence 4677 is a loaded world view with the player character, foreground rocks, a large boulder, sky, and HUD. The first attempt, retained separately under `run-01-paired-depth`, shows the Bandai Namco startup splash and is excluded from world-scene evidence.

## 3. Constant-depth harness control

The deterministic harness used the accepted real color and correct motion inputs for pair 3→4 and replaced only geometric depth with a constant 0.25 field. The pair includes the moving main shape, faster marker, static background, and overlap/disocclusion behavior. The constant-depth result remained finite and opaque, with 72,450 meaningful pixels out of 921,600. MAE was 0.00585287 versus the previous source and 0.00536840 versus the current source. R104 baseline MAE was 0.00602260 and 0.00522749 respectively, so the constant-depth deltas were −0.00016973 and +0.00014091. The midpoint inspection showed no severe artifacts.

The harness also retained `sourceGetBytesCritical=0`, zero CPU color/depth/motion pixel inputs, and a GPU-resident A→M→B path. Verdict: `CONSTANT_DEPTH_TITLE_PROTOTYPE = ALLOWED` for a first title-only proof; this is not a production-depth claim.

## 4. Derived-motion implementation and Apple reference

The custom fallback is a bounded Metal block matcher, not a general optical-flow framework. It searches independent X and Y offsets, gates patch cost by color, rejects ambiguous axes using a confidence margin, propagates the coarse field, and upsamples to a full-resolution `RG16Float` texture. Its grid is `ceil(width/40) × ceil(height/40)`: 32×18 for the 1280×720 harness and 37×24 for the 1470×956 title pair.

The output stores R=X and G=Y displacement in pixel units using the calibrated current-to-previous convention. Positive texture X moves right and positive texture Y follows increasing texture row (down); the harness ground truth exercises X only, so Y sign is code-convention evidence rather than an independent Y-motion calibration.

The installed Vision path was tested as a reference with `VNGenerateOpticalFlowRequest`, CPU `BGRA` pixel-buffer inputs, and `TwoComponent32Float` output. It completed, but took 47.097 seconds at low accuracy and 45.667 seconds at medium accuracy on the harness pair. Its measured vectors did not match the harness displacement: the main-region estimate was +1.369 px against −4.3868 px expected, and the marker estimate was +2.867 px against −15 px expected. The CPU pixel-buffer path and latency make it unsuitable for the product critical path. No usable MPS optical-flow API was found in the installed SDK. Vision remains a rejected correctness reference for this pair, not the implementation.

## 5. Harness motion calibration

On the deterministic 1280×720 pair 3→4, the custom GPU probe returned status 4 with no error and a 32×18 motion grid. The known current-to-previous displacement was −4.3868 px for the main shape and −15 px for the fast marker; the static background was 0. The estimated medians were (−4.5, 0), (−12.5, 0), and (0, 0), respectively. Median vector errors were 0.1132 px for the main shape, 2.5 px for the marker, and 0 for the static background. This establishes component order, horizontal sign, pixel scale, and a zero-motion response for the calibrated harness regions.

## 6. Elden Ring source pairs

The final process was PID 53895 on Apple M3 device registry ID 4294968554. The one-shot trigger began at source sequence 4677 with stride 1 and a 72-producer bound; the last selected producer was sequence 4788. All 72 selected producer command buffers completed with Metal status 4 and no error. The 16 retained color files are the exact consecutive sequences 4677–4692, each 1470×956 BGRA8 (`pixelFormat=80`). They were captured after producer completion from the correlated drawable path.

The title motion pair was sequences 4678→4679, with sequence 4677 supplying the preceding provider-history frame. The operator-confirmed condition for this run was “world loaded; hold still.” The images show the player and world geometry; this is a still interval. No slow camera rotation or character movement interval was captured in this run.

Evidence: `/tmp/fgmetal-r106-title-derived-motion/observer.53895.log` and `experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/run-02-world-paired-depth/`.

## 7. Title derived-motion semantic validation

The GPU estimator ran on the exact consecutive still pair 4678→4679. Across 1,405,320 full-resolution vectors, the median was (−0.040680, +0.048798) px; median magnitude was 0.089309 px, p90 0.283109 px, p99 0.465379 px, and maximum 0.508986 px. 99.8861% of vectors were below 0.5 px. This is consistent with the confirmed still scene and gives a credible still-interval response.

Verdict: `REAL_TITLE_DERIVED_MOTION = VALIDATED` for this still interval only. It does not validate camera-pan or character-motion semantics. `REAL_TITLE_NATIVE_MOTION = NOT FOUND` remains unchanged.

## 8. Title depth choice

R106 did not validate real title depth and did not acquire an R105 depth grid in the paired world window. The constant-depth harness control explicitly allowed a first title prototype, so the title experiment used a full-resolution R32Float depth field of 0.25. This is qualified as `CONSTANT_DEPTH_PROTOTYPE`; it must not be read as real scene depth or production-ready occlusion information.

## 9. MetalFX title input preparation

The exact captured BGRA8 triplet 4677, 4678, 4679 was loaded by the bounded standalone probe. A Metal compute preparation pass converted each color to private `RGBA16Float` and wrote the constant 0.25 depth field as `R32Float`. The derived RG16Float field for 4678→4679 was passed to the accepted `MetalFXFrameInterpolationProvider`; the initial 4677→4678 history seed used zero motion, then the provider generated the 4678→4679 midpoint.

This is an offline evidence path. Captured color passes through CPU readback and files; the standalone estimator uploads it, reads the derived field back to a file, and the provider uploads that field. It does not establish an in-process GPU-resident derived-motion path.

## 10. Title MetalFX output

MetalFX returned a private 1470×956 `RGBA16Float` internal texture (`internalPresentId=1`) for source sequences 4678→4679. All 5,621,280 output channel values were finite; none were non-finite. There were 1,183,144 meaningful pixels out of 1,405,320. Alpha was exactly 1.0 throughout, with all pixels opaque. MAE was 0.00377813 versus the previous source and 0.00366801 versus the current source. The provider interpolation call took 77.718 ms.

The texture is saved at `experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/run-02-world-paired-depth/title-M-for-4678-4679.rgba16f`. No title presentation path was invoked.

## 11. Visual artifact assessment

The saved previous–M–current composite is `experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/run-02-world-paired-depth/title-4678-M-4679-previous-M-current.png`. The midpoint is finite, opaque, and visually close to both nearly identical still sources. The character silhouette, sword, boulder, terrain, sky, and HUD remain coherent. No visible double edges, ghosting, deformation, tearing, or HUD/text artifact appeared in this still interval. Moving-scene artifacts remain unassessed because this pair contains no camera or character motion.

## 12. Timing

| Stage | Measured time | Boundary |
|---|---:|---|
| Custom harness motion probe | 151.805 ms GPU; 156.479 ms elapsed | 1280×720, including command-buffer wait |
| Custom title still-pair motion probe | 132.727 ms GPU; 154.647 ms elapsed | 1470×956, including command-buffer wait |
| Vision low / medium reference | 47.097 s / 45.667 s | Harness pair, CPU BGRA pixel buffers |
| MetalFX title interpolation call | 77.718 ms | Excludes capture, file transport, and input preparation |

The custom estimator is an unoptimized prototype and is too slow for gameplay pacing. R104 waits were left unchanged.

## 13. Verdicts

- `REAL_TITLE_MOTION = NOT FOUND` for native/direct motion resources, unchanged from R105.
- `REAL_TITLE_DERIVED_MOTION = VALIDATED` for the still interval only; camera and character motion remain unvalidated.
- `REAL_TITLE_DEPTH = NOT VALIDATED`; the R105 array candidate was not reacquired, so the corrected array-view sampler was not exercised in this run.
- `R105_DEPTH_CANDIDATE = NOT REACQUIRED IN R106 WINDOW`; do not call it semantically rejected based on this run.
- `CONSTANT_DEPTH_TITLE_PROTOTYPE = ALLOWED` for first-title proof only.
- `REAL_TITLE_METALFX_INTERNAL_TEXTURE = VALIDATED (CONSTANT_DEPTH_PROTOTYPE; STILL INTERVAL ONLY)`. MetalFX returned a meaningful, finite, opaque game-derived internal texture and the visual review found no still-scene artifact.
- `REAL_TITLE_PRESENTATION = NOT ATTEMPTED`, as requested.

## 14. Exact next product blocker

`NEXT_PRODUCT_BLOCKER = IN_PROCESS_GPU_DERIVED_MOTION_FOR_MOVING_TITLE_FRAMES`. The current title result proves only a still-scene midpoint through an offline CPU/file transport path. Before advancing the product path, capture exact consecutive source pairs during a slow camera pan and character movement, validate coherent local vectors and moving-scene artifacts, then move the estimator into the in-process GPU path. Real depth remains unvalidated; constant depth is sufficient only for this first prototype.

Evidence and implementation files:

- Observer source: `experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/observer-title-derived-motion-r106.m`
- Observer build: `experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/build_r106_title_observer.sh`
- Observer SHA-256: `5906c8f813249bd9ce7df77848e265bc61ec82ed7daaf9440ac2487f951b5a17`
- GPU motion implementation and calibrated log: `experiments/metalfx_frame_interpolator_R93/evidence/r106-constant-depth/r106_gpu_motion_probe.m` and `experiments/metalfx_frame_interpolator_R93/evidence/r106-constant-depth/run-02/gpu-motion-harness-validated.log`
- Title provider probe: `experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/r106_title_metafx_probe.m`
- Title output and visual evidence: `experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion/run-02-world-paired-depth/`
- Constant-depth harness image: `experiments/metalfx_frame_interpolator_R93/evidence/r106-constant-depth/run-02/constant-depth-pair-3-4-visual.png`
- Highball's temporary `DYLD_INSERT_LIBRARIES`, `FGMETAL_R106_CAPTURE`, and `FGMETAL_R106_FRAMEBUFFER_ONLY` environment variables were cleared after capture. The running game was left open; the current process retains its already-loaded observer until it exits.
