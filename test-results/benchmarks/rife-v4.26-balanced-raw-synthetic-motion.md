# Frame-generation benchmark: 20261001T133935Z

- Backend: practical_rife_v4_26_metal (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.3268 / 0.7438 | 0.5495 / 1.4876 | 0.0418 / 0.0515 | 0.9308 | 0.0249 / 0.2899 / 0.7438 |
| hud | 0.2644 / 0.7438 | 0.5281 / 1.4876 | 0.0231 / 0.0367 | 0.9591 | 0.0329 / 0.2608 / 0.7438 |
| text | 0.7117 / 0.7438 | 1.3947 / 1.4876 | 0.0131 / 0.0261 | 0.9275 | 0.1804 / 0.7054 / 0.7438 |
| scene | 0.3215 / 0.7150 | 0.5386 / 1.4301 | 0.0599 / 0.0782 | 0.9029 | 0.0208 / 0.2863 / 0.6915 |
| occlusion | 0.1634 / 0.6588 | 0.3268 / 1.3176 | 0.1053 / 0.1717 | 0.6105 | 0.0393 / 0.1634 / 0.6588 |
| thin_geometry | 0.4132 / 0.6837 | 0.8183 / 1.3673 | 0.1875 / 0.2257 | 0.5633 | 0.0742 / 0.4097 / 0.6837 |
| crosshair | 0.4118 / 0.4196 | 0.8120 / 0.8392 | 0.0164 / 0.0367 | 0.9878 | 0.0367 / 0.4089 / 0.4196 |
| crosshair_pixels | 0.1310 / 0.1373 | 0.2490 / 0.2745 | 0.0152 / 0.0328 | 0.8889 | 0.0312 / 0.1245 / 0.1373 |
| weapon_sight | 0.4055 / 0.5817 | 0.8076 / 1.1634 | 0.0343 / 0.0627 | 0.9388 | 0.0495 / 0.4047 / 0.5817 |
| minimap | 0.0157 / 0.3961 | 0.0303 / 0.7922 | 0.0044 / 0.0093 | 0.9894 | 0.0049 / 0.0154 / 0.3961 |
| static_text_pixels | 0.0562 / 0.0745 | 0.1093 / 0.1490 | 0.0000 / 0.0000 | 1.0000 | 0.0208 / 0.0554 / 0.0745 |
| health_bar | 0.2215 / 0.2523 | 0.3584 / 0.5046 | 0.0191 / 0.0833 | 1.0000 | 0.0211 / 0.1914 / 0.2523 |
| subtitle | 0.0407 / 0.0771 | 0.0794 / 0.1542 | 0.0000 / 0.0000 | 1.0000 | 0.0071 / 0.0402 / 0.0771 |
| hud_counter | 0.6369 / 0.6471 | 1.2516 / 1.2941 | 0.0133 / 0.0173 | 1.0000 | 0.0634 / 0.6314 / 0.6471 |
| timer | 0.0551 / 0.7438 | 0.1116 / 1.4876 | 0.0067 / 0.0159 | 0.9828 | 0.0116 / 0.0522 / 0.7438 |
| scrolling_text | 0.5418 / 0.6837 | 1.0795 / 1.3673 | 0.0629 / 0.0752 | 0.8732 | 0.0603 / 0.5408 / 0.6837 |
| flashing_ui | 0.0438 / 0.1529 | 0.0798 / 0.3059 | 0.0000 / 0.0000 | 1.0000 | 0.0034 / 0.0379 / 0.1529 |
| transparent_ui | 0.1791 / 0.4641 | 0.3467 / 0.9281 | 0.0209 / 0.0729 | 0.8908 | 0.0210 / 0.1762 / 0.4641 |
| moving_menu | 0.7059 / 0.7150 | 1.3908 / 1.4301 | 0.0534 / 0.1696 | 0.7358 | 0.1124 / 0.7007 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 20.150 | 0.80937 | 0.86957 | 3.9054 |
| hud | 17.779 | 0.83225 | 0.87681 | 4.6749 |
| text | 9.839 | 0.30832 | 0.50337 | 22.9987 |
| scene | 22.438 | 0.80205 | 0.86854 | 3.4632 |
| occlusion | 22.354 | 0.55391 | 0.63512 | 7.5814 |
| thin_geometry | 16.565 | 0.41619 | 0.60848 | 9.4185 |
| crosshair | 24.455 | 0.91278 | 0.92023 | 4.5514 |
| crosshair_pixels | 29.813 | 0.72680 | 0.87369 | 2.8240 |
| weapon_sight | 19.614 | 0.78922 | 0.84907 | 6.9378 |
| minimap | 34.542 | 0.98280 | 0.97984 | 0.9271 |
| static_text_pixels | 31.394 | 0.78347 | 0.91057 | 1.9959 |
| health_bar | 24.458 | 0.83629 | 0.85566 | 5.9376 |
| subtitle | 36.362 | 0.99476 | 0.98800 | 0.9906 |
| hud_counter | 14.095 | 0.71095 | 0.75549 | 10.0092 |
| timer | 23.540 | 0.95303 | 0.95789 | 1.3715 |
| scrolling_text | 15.225 | 0.60912 | 0.71507 | 9.4968 |
| flashing_ui | 34.441 | 0.93167 | 0.95675 | 1.4231 |
| transparent_ui | 25.285 | 0.88443 | 0.92579 | 2.6357 |
| moving_menu | 11.518 | 0.40707 | 0.60608 | 14.0149 |

## Runtime

- GPU time p50/p95/p99: 2.2168 / 2.6702 / 3.3819 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.9189 / 1.0071 / 1.1075 ms
- Completion latency p50/p95/p99: 2.7434 / 3.1734 / 3.8700 ms
- GPU allocated peak: 22052864 bytes; host RSS peak: 84819968 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.90% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
