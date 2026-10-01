# Frame-generation benchmark: 20261001T172749Z

- Backend: practical_rife_v4_26_metal (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.3438 / 0.7438 | 0.6520 / 1.4876 | 0.0767 / 0.1046 | 0.8383 | 0.0397 / 0.3349 / 0.7438 |
| hud | 0.3438 / 0.7438 | 0.6573 / 1.4876 | 0.0292 / 0.0368 | 0.9467 | 0.0356 / 0.3373 / 0.7438 |
| text | 0.7059 / 0.7438 | 1.4086 / 1.4876 | 0.0179 / 0.0199 | 0.9559 | 0.1463 / 0.6992 / 0.7438 |
| scene | 0.3438 / 0.7150 | 0.6552 / 1.4301 | 0.1311 / 0.1675 | 0.7133 | 0.0405 / 0.3357 / 0.6837 |
| occlusion | 0.2263 / 0.6549 | 0.3964 / 1.3098 | 0.2311 / 0.4368 | 0.0690 | 0.0500 / 0.2102 / 0.6549 |
| thin_geometry | 0.4235 / 0.6837 | 0.8293 / 1.3673 | 0.1878 / 0.2970 | 0.6090 | 0.1758 / 0.4220 / 0.6837 |
| crosshair | 0.4235 / 0.4235 | 0.8282 / 0.8471 | 0.0361 / 0.0471 | 0.9189 | 0.0743 / 0.4188 / 0.4235 |
| crosshair_pixels | 0.0667 / 0.0719 | 0.1108 / 0.1438 | 0.0270 / 0.0357 | 0.8750 | 0.0084 / 0.0599 / 0.0719 |
| weapon_sight | 0.4235 / 0.6314 | 0.8261 / 1.2627 | 0.0568 / 0.0729 | 0.9007 | 0.0803 / 0.4183 / 0.6314 |
| minimap | 0.0114 / 0.3961 | 0.0220 / 0.7922 | 0.0021 / 0.0062 | 1.0000 | 0.0034 / 0.0112 / 0.3961 |
| static_text_pixels | 0.0516 / 0.0614 | 0.0946 / 0.1229 | 0.0000 / 0.0000 | 1.0000 | 0.0085 / 0.0495 / 0.0614 |
| health_bar | 0.2654 / 0.3294 | 0.4246 / 0.6588 | 0.0127 / 0.0500 | 1.0000 | 0.0208 / 0.2277 / 0.3294 |
| subtitle | 0.0340 / 0.0641 | 0.0648 / 0.1281 | 0.0000 / 0.0000 | 1.0000 | 0.0030 / 0.0332 / 0.0641 |
| hud_counter | 0.6471 / 0.6471 | 1.2941 / 1.2941 | 0.0161 / 0.0234 | 0.9831 | 0.0485 / 0.6471 / 0.6471 |
| timer | 0.0170 / 0.7438 | 0.0424 / 1.4876 | 0.0081 / 0.0151 | 1.0000 | 0.0140 / 0.0162 / 0.7438 |
| scrolling_text | 0.5529 / 0.6549 | 1.0225 / 1.3098 | 0.0973 / 0.1235 | 0.7857 | 0.0754 / 0.5321 / 0.6549 |
| flashing_ui | 0.1883 / 0.3033 | 0.3285 / 0.6065 | 0.2668 / 1.0000 | 0.0000 | 0.0113 / 0.1674 / 0.3033 |
| transparent_ui | 0.1284 / 0.6275 | 0.2294 / 1.2549 | 0.0088 / 0.0202 | 0.9558 | 0.0173 / 0.1216 / 0.6275 |
| moving_menu | 0.7059 / 0.7150 | 1.4066 / 1.4301 | 0.0429 / 0.0979 | 0.8962 | 0.1036 / 0.7046 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 18.895 | 0.67960 | 0.77690 | 6.0554 |
| hud | 17.904 | 0.80131 | 0.85664 | 5.0012 |
| text | 10.690 | 0.37449 | 0.54507 | 18.7243 |
| scene | 19.747 | 0.62274 | 0.73586 | 6.3885 |
| occlusion | 21.333 | 0.43255 | 0.54887 | 9.4565 |
| thin_geometry | 12.529 | 0.25009 | 0.41937 | 21.1356 |
| crosshair | 16.388 | 0.77550 | 0.81713 | 9.1864 |
| crosshair_pixels | 52.012 | 0.84075 | 0.90678 | 0.7468 |
| weapon_sight | 15.977 | 0.58560 | 0.72808 | 10.2798 |
| minimap | 38.011 | 0.98538 | 0.98634 | 0.6961 |
| static_text_pixels | 52.121 | 0.86534 | 0.94887 | 0.8072 |
| health_bar | 22.480 | 0.82350 | 0.84762 | 5.6170 |
| subtitle | 55.761 | 0.99887 | 0.99487 | 0.4619 |
| hud_counter | 15.425 | 0.78575 | 0.80243 | 7.7161 |
| timer | 20.769 | 0.93526 | 0.94839 | 1.6145 |
| scrolling_text | 15.380 | 0.46980 | 0.61647 | 11.0490 |
| flashing_ui | 30.244 | 0.85037 | 0.89864 | 3.3750 |
| transparent_ui | 25.841 | 0.88250 | 0.92456 | 2.6422 |
| moving_menu | 12.028 | 0.46996 | 0.63456 | 12.8304 |

## Runtime

- GPU time p50/p95/p99: 2.6225 / 8.0265 / 8.2589 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.9681 / 1.1328 / 1.2414 ms
- Completion latency p50/p95/p99: 3.1621 / 8.5819 / 8.8492 ms
- GPU allocated peak: 42516480 bytes; host RSS peak: 91996160 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.92% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
