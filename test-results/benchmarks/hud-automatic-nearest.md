# Frame-generation benchmark: 20261001T123629Z

- Backend: metal_placeholder_blend (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.2771 / 0.7438 | 0.5292 / 1.4876 | 0.0601 / 0.0843 | 0.8807 | 0.0278 / 0.2708 / 0.7438 |
| hud | 0.2809 / 0.7438 | 0.5537 / 1.4876 | 0.0237 / 0.0333 | 0.9602 | 0.0302 / 0.2818 / 0.7438 |
| text | 0.7059 / 0.7438 | 1.4110 / 1.4876 | 0.0148 / 0.0175 | 0.9476 | 0.1427 / 0.7023 / 0.7438 |
| scene | 0.2785 / 0.7150 | 0.5178 / 1.4301 | 0.0917 / 0.1290 | 0.8165 | 0.0256 / 0.2650 / 0.6810 |
| occlusion | 0.2713 / 0.6275 | 0.5352 / 1.2549 | 0.5873 / 1.0000 | 0.0000 | 0.0703 / 0.1653 / 0.5765 |
| thin_geometry | 0.4235 / 0.6680 | 0.8345 / 1.3359 | 0.2544 / 0.2743 | 0.4815 | 0.0798 / 0.4167 / 0.6680 |
| crosshair | 0.4013 / 0.5778 | 0.8026 / 1.1556 | 0.0290 / 0.0410 | 0.9747 | 0.0576 / 0.4013 / 0.5778 |
| crosshair_pixels | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0451 / 0.0878 | 0.7500 | 0.0000 / 0.0000 / 0.0000 |
| weapon_sight | 0.4013 / 0.5817 | 0.7932 / 1.1634 | 0.0425 / 0.0656 | 0.8503 | 0.0583 / 0.3990 / 0.5817 |
| minimap | 0.0000 / 0.3961 | 0.0000 / 0.7922 | 0.0023 / 0.0044 | 1.0000 | 0.0019 / 0.0000 / 0.3961 |
| static_text_pixels | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 1.0000 | 0.0000 / 0.0000 / 0.0000 |
| health_bar | 0.1320 / 0.2654 | 0.2599 / 0.5307 | 0.0133 / 0.0600 | 1.0000 | 0.0078 / 0.1310 / 0.2654 |
| subtitle | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 1.0000 | 0.0000 / 0.0000 / 0.0000 |
| hud_counter | 0.6471 / 0.6471 | 1.2941 / 1.2941 | 0.0118 / 0.0177 | 1.0000 | 0.0491 / 0.6471 / 0.6471 |
| timer | 0.0000 / 0.7438 | 0.0000 / 1.4876 | 0.0072 / 0.0119 | 1.0000 | 0.0125 / 0.0000 / 0.7438 |
| scrolling_text | 0.4961 / 0.6680 | 0.9817 / 1.3359 | 0.0684 / 0.0763 | 0.8732 | 0.0491 / 0.4935 / 0.6680 |
| flashing_ui | 0.1778 / 0.2222 | 0.3822 / 0.4444 | 0.0000 / 0.0000 | 1.0000 | 0.0755 / 0.1778 / 0.2222 |
| transparent_ui | 0.0915 / 0.1791 | 0.1830 / 0.3582 | 0.0164 / 0.0546 | 0.8908 | 0.0086 / 0.0915 / 0.1791 |
| moving_menu | 0.6813 / 0.7150 | 1.3616 / 1.4301 | 0.0693 / 0.2096 | 0.7547 | 0.1030 / 0.6810 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 20.450 | 0.77444 | 0.84862 | 4.8089 |
| hud | 18.521 | 0.84046 | 0.87500 | 4.9438 |
| text | 11.125 | 0.47641 | 0.60070 | 18.0648 |
| scene | 22.200 | 0.75112 | 0.84070 | 4.5637 |
| occlusion | 21.160 | 0.23007 | 0.37943 | 13.9359 |
| thin_geometry | 17.010 | 0.21137 | 0.45019 | 10.2856 |
| crosshair | 18.584 | 0.84682 | 0.86442 | 7.1358 |
| crosshair_pixels | 120.000 | 1.00000 | 0.96769 | 0.0000 |
| weapon_sight | 18.175 | 0.73772 | 0.81713 | 8.1452 |
| minimap | 65.947 | 0.99073 | 0.99115 | 0.4404 |
| static_text_pixels | 120.000 | 1.00000 | 1.00000 | 0.0000 |
| health_bar | 45.095 | 0.90826 | 0.93349 | 2.2734 |
| subtitle | 120.000 | 1.00000 | 0.99998 | 0.0000 |
| hud_counter | 15.641 | 0.77218 | 0.81604 | 7.7586 |
| timer | 22.422 | 0.94578 | 0.95326 | 1.3936 |
| scrolling_text | 16.340 | 0.63617 | 0.72423 | 8.1392 |
| flashing_ui | 23.092 | 0.64206 | 0.63711 | 25.6184 |
| transparent_ui | 28.153 | 0.89326 | 0.94030 | 1.4165 |
| moving_menu | 12.844 | 0.46089 | 0.62153 | 13.0047 |

## Runtime

- GPU time p50/p95/p99: 0.0186 / 0.0203 / 0.0273 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.0125 / 0.0192 / 0.0288 ms
- Completion latency p50/p95/p99: 0.4320 / 0.5433 / 0.6379 ms
- GPU allocated peak: 753664 bytes; host RSS peak: 14303232 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.38% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
