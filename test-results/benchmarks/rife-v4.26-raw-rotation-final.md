# Frame-generation benchmark: 20261001T172657Z

- Backend: practical_rife_v4_26_metal (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.3216 / 0.7948 | 0.6329 / 1.5895 | 0.0622 / 0.0701 | 0.9033 | 0.0403 / 0.3190 / 0.7948 |
| hud | 0.5342 / 0.7948 | 0.9271 / 1.5895 | 0.0293 / 0.0427 | 0.9329 | 0.0458 / 0.5012 / 0.7948 |
| text | 0.7150 / 0.7856 | 1.4075 / 1.5712 | 0.0186 / 0.0264 | 0.9314 | 0.2022 / 0.7074 / 0.7856 |
| scene | 0.3230 / 0.7150 | 0.6322 / 1.4301 | 0.0989 / 0.1068 | 0.8576 | 0.0381 / 0.3208 / 0.6889 |
| occlusion | 0.2345 / 0.6549 | 0.3964 / 1.3098 | 0.1734 / 0.3680 | 0.1034 | 0.0404 / 0.2102 / 0.6549 |
| thin_geometry | 0.4218 / 0.7856 | 0.8231 / 1.5712 | 0.1623 / 0.2377 | 0.7055 | 0.1582 / 0.4227 / 0.7856 |
| crosshair | 0.7725 / 0.7948 | 1.2596 / 1.5895 | 0.0403 / 0.0631 | 0.9722 | 0.1035 / 0.7012 / 0.7948 |
| crosshair_pixels | 0.7948 / 0.7948 | 1.2901 / 1.5895 | 0.0340 / 0.1047 | 1.0000 | 0.1102 / 0.6609 / 0.7948 |
| weapon_sight | 0.5590 / 0.7948 | 1.0033 / 1.5895 | 0.0635 / 0.0958 | 0.8931 | 0.0937 / 0.5303 / 0.7948 |
| minimap | 0.0157 / 0.3922 | 0.0303 / 0.7843 | 0.0024 / 0.0062 | 1.0000 | 0.0049 / 0.0154 / 0.3922 |
| static_text_pixels | 0.0683 / 0.0941 | 0.1298 / 0.1882 | 0.0000 / 0.0000 | 1.0000 | 0.0238 / 0.0652 / 0.0941 |
| health_bar | 0.3843 / 0.3856 | 0.7503 / 0.7712 | 0.0074 / 0.0164 | 1.0000 | 0.0910 / 0.3797 / 0.3856 |
| subtitle | 0.0539 / 0.0941 | 0.1007 / 0.1882 | 0.0001 / 0.0007 | 1.0000 | 0.0084 / 0.0515 / 0.0941 |
| hud_counter | 0.6416 / 0.6471 | 1.2831 / 1.2941 | 0.0173 / 0.0320 | 0.9831 | 0.0889 / 0.6416 / 0.6471 |
| timer | 0.0444 / 0.7438 | 0.1008 / 1.4876 | 0.0068 / 0.0108 | 0.9828 | 0.0110 / 0.0430 / 0.7438 |
| scrolling_text | 0.5438 / 0.6575 | 1.0533 / 1.3150 | 0.0866 / 0.0994 | 0.7488 | 0.0771 / 0.5352 / 0.6575 |
| flashing_ui | 0.1889 / 0.3098 | 0.3329 / 0.6196 | 0.2829 / 1.0000 | 0.0000 | 0.0132 / 0.1699 / 0.3098 |
| transparent_ui | 0.6485 / 0.6758 | 1.0466 / 1.3516 | 0.0128 / 0.0214 | 0.9558 | 0.0485 / 0.5774 / 0.6758 |
| moving_menu | 0.7059 / 0.7150 | 1.3908 / 1.4301 | 0.0489 / 0.1557 | 0.7358 | 0.1044 / 0.7007 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 18.563 | 0.69974 | 0.79408 | 6.0918 |
| hud | 16.901 | 0.76840 | 0.82876 | 6.5464 |
| text | 9.488 | 0.24377 | 0.44824 | 25.3441 |
| scene | 19.770 | 0.66319 | 0.77421 | 5.8441 |
| occlusion | 22.171 | 0.53560 | 0.66131 | 7.6081 |
| thin_geometry | 12.961 | 0.24340 | 0.43499 | 19.3316 |
| crosshair | 15.264 | 0.65158 | 0.73661 | 12.1313 |
| crosshair_pixels | 23.758 | 0.56827 | 0.72111 | 10.6729 |
| weapon_sight | 15.340 | 0.52093 | 0.68438 | 11.6320 |
| minimap | 36.233 | 0.98482 | 0.98173 | 0.9389 |
| static_text_pixels | 30.495 | 0.76222 | 0.90067 | 2.2414 |
| health_bar | 19.060 | 0.33927 | 0.54088 | 19.8978 |
| subtitle | 35.187 | 0.99451 | 0.98566 | 1.2107 |
| hud_counter | 13.993 | 0.60294 | 0.67225 | 14.1720 |
| timer | 23.305 | 0.96046 | 0.96275 | 1.3064 |
| scrolling_text | 14.783 | 0.51225 | 0.63625 | 11.5885 |
| flashing_ui | 28.485 | 0.83924 | 0.89337 | 3.8713 |
| transparent_ui | 21.074 | 0.76519 | 0.85006 | 5.5979 |
| moving_menu | 12.044 | 0.46551 | 0.64146 | 13.0784 |

## Runtime

- GPU time p50/p95/p99: 2.5877 / 2.7858 / 3.3306 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.9427 / 1.0453 / 1.0911 ms
- Completion latency p50/p95/p99: 3.1033 / 3.3881 / 3.8601 ms
- GPU allocated peak: 42205184 bytes; host RSS peak: 91684864 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.91% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
