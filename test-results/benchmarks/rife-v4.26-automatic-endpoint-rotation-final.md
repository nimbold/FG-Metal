# Frame-generation benchmark: 20261001T172740Z

- Backend: practical_rife_v4_26_metal (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.3438 / 0.7438 | 0.6520 / 1.4876 | 0.0767 / 0.1046 | 0.8394 | 0.0397 / 0.3349 / 0.7438 |
| hud | 0.3438 / 0.7438 | 0.6623 / 1.4876 | 0.0293 / 0.0368 | 0.9467 | 0.0356 / 0.3378 / 0.7438 |
| text | 0.7059 / 0.7438 | 1.4086 / 1.4876 | 0.0177 / 0.0199 | 0.9559 | 0.1471 / 0.6992 / 0.7438 |
| scene | 0.3438 / 0.7150 | 0.6552 / 1.4301 | 0.1310 / 0.1675 | 0.7156 | 0.0404 / 0.3357 / 0.6837 |
| occlusion | 0.2263 / 0.6549 | 0.3964 / 1.3098 | 0.2311 / 0.4368 | 0.0690 | 0.0500 / 0.2102 / 0.6549 |
| thin_geometry | 0.4235 / 0.6837 | 0.8293 / 1.3673 | 0.1863 / 0.2970 | 0.6090 | 0.1760 / 0.4220 / 0.6837 |
| crosshair | 0.4235 / 0.4235 | 0.8282 / 0.8471 | 0.0358 / 0.0471 | 0.9189 | 0.0743 / 0.4188 / 0.4235 |
| crosshair_pixels | 0.0667 / 0.0719 | 0.1108 / 0.1438 | 0.0270 / 0.0357 | 0.8750 | 0.0084 / 0.0599 / 0.0719 |
| weapon_sight | 0.4235 / 0.6314 | 0.8261 / 1.2627 | 0.0566 / 0.0729 | 0.9007 | 0.0801 / 0.4183 / 0.6314 |
| minimap | 0.0114 / 0.3961 | 0.0220 / 0.7922 | 0.0021 / 0.0062 | 1.0000 | 0.0034 / 0.0112 / 0.3961 |
| static_text_pixels | 0.0516 / 0.0614 | 0.0946 / 0.1229 | 0.0000 / 0.0000 | 1.0000 | 0.0085 / 0.0495 / 0.0614 |
| health_bar | 0.2654 / 0.3294 | 0.4246 / 0.6588 | 0.0127 / 0.0500 | 1.0000 | 0.0208 / 0.2277 / 0.3294 |
| subtitle | 0.0340 / 0.0641 | 0.0648 / 0.1281 | 0.0000 / 0.0000 | 1.0000 | 0.0030 / 0.0332 / 0.0641 |
| hud_counter | 0.6471 / 0.6471 | 1.2941 / 1.2941 | 0.0158 / 0.0234 | 0.9831 | 0.0498 / 0.6471 / 0.6471 |
| timer | 0.0170 / 0.7438 | 0.0425 / 1.4876 | 0.0087 / 0.0151 | 1.0000 | 0.0141 / 0.0162 / 0.7438 |
| scrolling_text | 0.5529 / 0.6549 | 1.0225 / 1.3098 | 0.0977 / 0.1235 | 0.7857 | 0.0755 / 0.5321 / 0.6549 |
| flashing_ui | 0.1883 / 0.3033 | 0.3285 / 0.6065 | 0.2668 / 1.0000 | 0.0000 | 0.0113 / 0.1674 / 0.3033 |
| transparent_ui | 0.1284 / 0.6314 | 0.2294 / 1.2627 | 0.0088 / 0.0202 | 0.9558 | 0.0173 / 0.1216 / 0.6314 |
| moving_menu | 0.7059 / 0.7150 | 1.4066 / 1.4301 | 0.0429 / 0.0979 | 0.8962 | 0.1036 / 0.7046 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 18.884 | 0.67925 | 0.77676 | 6.0577 |
| hud | 17.878 | 0.80050 | 0.85633 | 5.0131 |
| text | 10.636 | 0.37335 | 0.54347 | 18.8570 |
| scene | 19.746 | 0.62264 | 0.73582 | 6.3861 |
| occlusion | 21.330 | 0.43236 | 0.54873 | 9.4667 |
| thin_geometry | 12.523 | 0.25115 | 0.42013 | 21.1490 |
| crosshair | 16.389 | 0.77551 | 0.81746 | 9.1695 |
| crosshair_pixels | 52.012 | 0.84075 | 0.90696 | 0.7468 |
| weapon_sight | 15.985 | 0.58648 | 0.72875 | 10.2537 |
| minimap | 38.011 | 0.98538 | 0.98634 | 0.6961 |
| static_text_pixels | 52.121 | 0.86534 | 0.94887 | 0.8072 |
| health_bar | 22.480 | 0.82350 | 0.84781 | 5.6170 |
| subtitle | 55.761 | 0.99887 | 0.99487 | 0.4619 |
| hud_counter | 15.220 | 0.77220 | 0.79599 | 7.9537 |
| timer | 20.685 | 0.93432 | 0.94833 | 1.6326 |
| scrolling_text | 15.366 | 0.46823 | 0.61577 | 11.0616 |
| flashing_ui | 30.244 | 0.85034 | 0.89860 | 3.3763 |
| transparent_ui | 25.836 | 0.88245 | 0.92453 | 2.6436 |
| moving_menu | 12.028 | 0.46997 | 0.63460 | 12.8289 |

## Runtime

- GPU time p50/p95/p99: 2.6214 / 8.0305 / 8.2350 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.9632 / 1.1470 / 1.3336 ms
- Completion latency p50/p95/p99: 3.1618 / 8.5966 / 8.8596 ms
- GPU allocated peak: 42516480 bytes; host RSS peak: 92307456 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.92% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
