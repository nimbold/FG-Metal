# Frame-generation benchmark: 20261001T133844Z

- Backend: practical_rife_v4_26_metal (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.3268 / 0.7438 | 0.5495 / 1.4876 | 0.0418 / 0.0515 | 0.9308 | 0.0249 / 0.2899 / 0.7438 |
| hud | 0.2644 / 0.7438 | 0.5275 / 1.4876 | 0.0231 / 0.0367 | 0.9591 | 0.0329 / 0.2609 / 0.7438 |
| text | 0.7117 / 0.7438 | 1.3947 / 1.4876 | 0.0131 / 0.0261 | 0.9275 | 0.1805 / 0.7054 / 0.7438 |
| scene | 0.3215 / 0.7150 | 0.5386 / 1.4301 | 0.0599 / 0.0782 | 0.9029 | 0.0208 / 0.2863 / 0.6915 |
| occlusion | 0.1634 / 0.6588 | 0.3268 / 1.3176 | 0.1051 / 0.1717 | 0.6105 | 0.0392 / 0.1634 / 0.6588 |
| thin_geometry | 0.4135 / 0.6837 | 0.8190 / 1.3673 | 0.1889 / 0.2253 | 0.5696 | 0.0742 / 0.4097 / 0.6837 |
| crosshair | 0.4118 / 0.4196 | 0.8120 / 0.8392 | 0.0164 / 0.0367 | 0.9878 | 0.0366 / 0.4089 / 0.4196 |
| crosshair_pixels | 0.1310 / 0.1373 | 0.2490 / 0.2745 | 0.0152 / 0.0328 | 0.8889 | 0.0310 / 0.1245 / 0.1373 |
| weapon_sight | 0.4055 / 0.5817 | 0.8076 / 1.1634 | 0.0344 / 0.0627 | 0.9388 | 0.0495 / 0.4047 / 0.5817 |
| minimap | 0.0157 / 0.3961 | 0.0303 / 0.7922 | 0.0044 / 0.0093 | 0.9894 | 0.0048 / 0.0154 / 0.3961 |
| static_text_pixels | 0.0562 / 0.0745 | 0.1093 / 0.1490 | 0.0000 / 0.0000 | 1.0000 | 0.0209 / 0.0554 / 0.0745 |
| health_bar | 0.2215 / 0.2536 | 0.3581 / 0.5072 | 0.0191 / 0.0833 | 1.0000 | 0.0211 / 0.1914 / 0.2536 |
| subtitle | 0.0407 / 0.0784 | 0.0794 / 0.1569 | 0.0000 / 0.0000 | 1.0000 | 0.0071 / 0.0402 / 0.0784 |
| hud_counter | 0.6369 / 0.6471 | 1.2516 / 1.2941 | 0.0133 / 0.0173 | 1.0000 | 0.0634 / 0.6314 / 0.6471 |
| timer | 0.0551 / 0.7438 | 0.1109 / 1.4876 | 0.0067 / 0.0159 | 0.9828 | 0.0116 / 0.0522 / 0.7438 |
| scrolling_text | 0.5408 / 0.6837 | 1.0783 / 1.3673 | 0.0631 / 0.0752 | 0.8732 | 0.0603 / 0.5400 / 0.6837 |
| flashing_ui | 0.0438 / 0.1529 | 0.0803 / 0.3059 | 0.0000 / 0.0000 | 1.0000 | 0.0034 / 0.0382 / 0.1529 |
| transparent_ui | 0.1791 / 0.4627 | 0.3467 / 0.9255 | 0.0209 / 0.0729 | 0.8908 | 0.0210 / 0.1762 / 0.4627 |
| moving_menu | 0.7059 / 0.7150 | 1.3908 / 1.4301 | 0.0534 / 0.1696 | 0.7358 | 0.1124 / 0.7007 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 20.150 | 0.80940 | 0.86960 | 3.9048 |
| hud | 17.779 | 0.83222 | 0.87680 | 4.6754 |
| text | 9.838 | 0.30809 | 0.50320 | 23.0018 |
| scene | 22.440 | 0.80212 | 0.86859 | 3.4617 |
| occlusion | 22.370 | 0.55508 | 0.63610 | 7.5667 |
| thin_geometry | 16.566 | 0.41588 | 0.60839 | 9.4128 |
| crosshair | 24.462 | 0.91285 | 0.92031 | 4.5447 |
| crosshair_pixels | 29.908 | 0.72706 | 0.87412 | 2.7877 |
| weapon_sight | 19.614 | 0.78928 | 0.84913 | 6.9329 |
| minimap | 34.538 | 0.98278 | 0.97977 | 0.9312 |
| static_text_pixels | 31.391 | 0.78301 | 0.91055 | 1.9808 |
| health_bar | 24.449 | 0.83627 | 0.85577 | 5.9163 |
| subtitle | 36.361 | 0.99475 | 0.98804 | 0.9855 |
| hud_counter | 14.094 | 0.71078 | 0.75531 | 10.0155 |
| timer | 23.540 | 0.95305 | 0.95790 | 1.3714 |
| scrolling_text | 15.222 | 0.60889 | 0.71498 | 9.5056 |
| flashing_ui | 34.462 | 0.93186 | 0.95688 | 1.4200 |
| transparent_ui | 25.294 | 0.88456 | 0.92590 | 2.6288 |
| moving_menu | 11.518 | 0.40703 | 0.60608 | 14.0169 |

## Runtime

- GPU time p50/p95/p99: 2.5858 / 3.0369 / 3.9180 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.9104 / 0.9895 / 1.1012 ms
- Completion latency p50/p95/p99: 3.0915 / 3.5360 / 4.4197 ms
- GPU allocated peak: 42205184 bytes; host RSS peak: 91684864 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.92% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
