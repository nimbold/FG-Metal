# Frame-generation benchmark: 20261001T172706Z

- Backend: practical_rife_v4_26_metal (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.3163 / 0.7438 | 0.6193 / 1.4876 | 0.0684 / 0.0759 | 0.8500 | 0.0375 / 0.3130 / 0.7438 |
| hud | 0.3394 / 0.7438 | 0.6255 / 1.4876 | 0.0292 / 0.0378 | 0.9354 | 0.0356 / 0.3284 / 0.7438 |
| text | 0.7041 / 0.7438 | 1.4032 / 1.4876 | 0.0206 / 0.0237 | 0.9461 | 0.1510 / 0.6695 / 0.7399 |
| scene | 0.3216 / 0.7150 | 0.6285 / 1.4301 | 0.1155 / 0.1291 | 0.7367 | 0.0379 / 0.3179 / 0.6837 |
| occlusion | 0.2201 / 0.6549 | 0.3964 / 1.3098 | 0.1926 / 0.4302 | 0.0690 | 0.0400 / 0.2102 / 0.6549 |
| thin_geometry | 0.4218 / 0.6837 | 0.8212 / 1.3673 | 0.1752 / 0.2382 | 0.6867 | 0.1654 / 0.4220 / 0.6837 |
| crosshair | 0.4235 / 0.7242 | 0.8418 / 1.4484 | 0.0380 / 0.0463 | 0.9189 | 0.0815 / 0.4222 / 0.7242 |
| crosshair_pixels | 0.7231 / 0.7242 | 1.1711 / 1.4484 | 0.0447 / 0.1246 | 0.6667 | 0.0573 / 0.5881 / 0.7242 |
| weapon_sight | 0.4165 / 0.7242 | 0.8208 / 1.4484 | 0.0582 / 0.0742 | 0.9007 | 0.0834 / 0.4134 / 0.7242 |
| minimap | 0.0140 / 0.3961 | 0.0259 / 0.7922 | 0.0020 / 0.0058 | 1.0000 | 0.0037 / 0.0135 / 0.3961 |
| static_text_pixels | 0.0516 / 0.0614 | 0.0946 / 0.1229 | 0.0000 / 0.0000 | 1.0000 | 0.0105 / 0.0495 / 0.0614 |
| health_bar | 0.2654 / 0.3294 | 0.4246 / 0.6588 | 0.0127 / 0.0500 | 1.0000 | 0.0210 / 0.2317 / 0.3294 |
| subtitle | 0.0340 / 0.0641 | 0.0648 / 0.1281 | 0.0000 / 0.0000 | 1.0000 | 0.0040 / 0.0332 / 0.0641 |
| hud_counter | 0.6439 / 0.6471 | 1.2620 / 1.2941 | 0.0161 / 0.0234 | 0.9831 | 0.0508 / 0.6374 / 0.6471 |
| timer | 0.0170 / 0.7438 | 0.0424 / 1.4876 | 0.0085 / 0.0151 | 0.9828 | 0.0133 / 0.0162 / 0.7438 |
| scrolling_text | 0.5167 / 0.6575 | 0.9790 / 1.3150 | 0.0962 / 0.1189 | 0.6957 | 0.0709 / 0.5031 / 0.6575 |
| flashing_ui | 0.1899 / 0.3033 | 0.3316 / 0.6065 | 0.2668 / 1.0000 | 0.0000 | 0.0129 / 0.1708 / 0.3033 |
| transparent_ui | 0.4984 / 0.6719 | 0.8121 / 1.3438 | 0.0114 / 0.0202 | 0.9558 | 0.0276 / 0.4317 / 0.6719 |
| moving_menu | 0.6994 / 0.7150 | 1.3800 / 1.4301 | 0.0422 / 0.0979 | 0.8962 | 0.0941 / 0.6947 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 19.179 | 0.70637 | 0.79989 | 5.6803 |
| hud | 18.115 | 0.81189 | 0.86174 | 5.0293 |
| text | 10.827 | 0.29803 | 0.50235 | 19.2307 |
| scene | 19.995 | 0.65502 | 0.76705 | 5.8902 |
| occlusion | 22.228 | 0.53868 | 0.66428 | 7.5309 |
| thin_geometry | 12.825 | 0.25376 | 0.43892 | 19.7414 |
| crosshair | 15.794 | 0.74144 | 0.78793 | 9.8825 |
| crosshair_pixels | 29.868 | 0.67950 | 0.76995 | 5.4633 |
| weapon_sight | 15.754 | 0.57224 | 0.71260 | 10.5970 |
| minimap | 38.154 | 0.98622 | 0.98609 | 0.7456 |
| static_text_pixels | 35.178 | 0.83549 | 0.93753 | 0.9996 |
| health_bar | 23.306 | 0.82797 | 0.85227 | 5.6297 |
| subtitle | 39.579 | 0.99537 | 0.99078 | 0.7218 |
| hud_counter | 15.429 | 0.78524 | 0.80236 | 8.0942 |
| timer | 21.291 | 0.94057 | 0.95248 | 1.5422 |
| scrolling_text | 15.645 | 0.51962 | 0.65660 | 10.4466 |
| flashing_ui | 28.630 | 0.85303 | 0.89620 | 3.8387 |
| transparent_ui | 23.461 | 0.85536 | 0.90365 | 3.6525 |
| moving_menu | 12.749 | 0.52131 | 0.66065 | 11.9068 |

## Runtime

- GPU time p50/p95/p99: 2.6090 / 7.9658 / 8.1445 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.9599 / 1.1499 / 1.2574 ms
- Completion latency p50/p95/p99: 3.1496 / 8.5244 / 8.7668 ms
- GPU allocated peak: 42319872 bytes; host RSS peak: 91570176 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.92% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
