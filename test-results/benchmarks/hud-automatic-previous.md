# Frame-generation benchmark: 20261001T123623Z

- Backend: metal_placeholder_blend (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.2771 / 0.7438 | 0.5292 / 1.4876 | 0.0588 / 0.0843 | 0.8807 | 0.0278 / 0.2708 / 0.7438 |
| hud | 0.2809 / 0.7438 | 0.5537 / 1.4876 | 0.0235 / 0.0333 | 0.9602 | 0.0298 / 0.2818 / 0.7438 |
| text | 0.7059 / 0.7438 | 1.4110 / 1.4876 | 0.0148 / 0.0175 | 0.9476 | 0.1382 / 0.6943 / 0.7438 |
| scene | 0.2785 / 0.7150 | 0.5178 / 1.4301 | 0.0897 / 0.1290 | 0.8165 | 0.0259 / 0.2650 / 0.6810 |
| occlusion | 0.2867 / 0.5804 | 0.5424 / 1.1608 | 0.5577 / 1.0000 | 0.0000 | 0.0713 / 0.1901 / 0.5804 |
| thin_geometry | 0.4235 / 0.6771 | 0.8345 / 1.3542 | 0.2374 / 0.2758 | 0.4815 | 0.0884 / 0.4183 / 0.6575 |
| crosshair | 0.4013 / 0.4235 | 0.8026 / 0.8471 | 0.0275 / 0.0410 | 0.9747 | 0.0577 / 0.4013 / 0.4235 |
| crosshair_pixels | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0404 / 0.0878 | 0.7500 | 0.0000 / 0.0000 / 0.0000 |
| weapon_sight | 0.4013 / 0.5817 | 0.7932 / 1.1634 | 0.0449 / 0.0656 | 0.8503 | 0.0593 / 0.3990 / 0.5817 |
| minimap | 0.0000 / 0.3961 | 0.0000 / 0.7922 | 0.0035 / 0.0084 | 0.9947 | 0.0027 / 0.0000 / 0.3961 |
| static_text_pixels | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 1.0000 | 0.0000 / 0.0000 / 0.0000 |
| health_bar | 0.1320 / 0.2641 | 0.2421 / 0.5281 | 0.0133 / 0.0600 | 1.0000 | 0.0074 / 0.1265 / 0.2641 |
| subtitle | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 1.0000 | 0.0000 / 0.0000 / 0.0000 |
| hud_counter | 0.6471 / 0.6471 | 1.2941 / 1.2941 | 0.0131 / 0.0177 | 1.0000 | 0.0513 / 0.6471 / 0.6471 |
| timer | 0.0000 / 0.7438 | 0.0000 / 1.4876 | 0.0055 / 0.0119 | 1.0000 | 0.0093 / 0.0000 / 0.7438 |
| scrolling_text | 0.5010 / 0.6771 | 0.9876 / 1.3542 | 0.0659 / 0.0763 | 0.8976 | 0.0505 / 0.4974 / 0.6771 |
| flashing_ui | 0.1778 / 0.2222 | 0.3822 / 0.4444 | 0.0000 / 0.0000 | 1.0000 | 0.0755 / 0.1778 / 0.2222 |
| transparent_ui | 0.0915 / 0.1791 | 0.1830 / 0.3582 | 0.0164 / 0.0546 | 0.8908 | 0.0085 / 0.0915 / 0.1791 |
| moving_menu | 0.6813 / 0.7150 | 1.3460 / 1.4301 | 0.0714 / 0.2096 | 0.7547 | 0.0992 / 0.6772 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 20.420 | 0.77054 | 0.84563 | 4.8344 |
| hud | 18.583 | 0.84116 | 0.87422 | 4.9468 |
| text | 11.293 | 0.48167 | 0.61479 | 17.6126 |
| scene | 22.113 | 0.74516 | 0.83693 | 4.5981 |
| occlusion | 20.878 | 0.22976 | 0.36740 | 13.9429 |
| thin_geometry | 16.045 | 0.15738 | 0.42401 | 11.2729 |
| crosshair | 19.617 | 0.84914 | 0.86631 | 7.1252 |
| crosshair_pixels | 120.000 | 1.00000 | 0.98063 | 0.0000 |
| weapon_sight | 18.051 | 0.71721 | 0.80727 | 8.2845 |
| minimap | 64.520 | 0.98418 | 0.98360 | 0.6346 |
| static_text_pixels | 120.000 | 1.00000 | 1.00000 | 0.0000 |
| health_bar | 45.387 | 0.91186 | 0.93895 | 2.1733 |
| subtitle | 120.000 | 1.00000 | 0.99997 | 0.0000 |
| hud_counter | 15.437 | 0.75992 | 0.80418 | 8.1131 |
| timer | 24.596 | 0.96305 | 0.96437 | 1.0375 |
| scrolling_text | 16.189 | 0.63081 | 0.72001 | 8.3803 |
| flashing_ui | 23.092 | 0.64206 | 0.63699 | 25.6184 |
| transparent_ui | 28.228 | 0.89345 | 0.94026 | 1.4034 |
| moving_menu | 13.073 | 0.47585 | 0.63010 | 12.6086 |

## Runtime

- GPU time p50/p95/p99: 0.0191 / 0.0195 / 0.0200 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.0121 / 0.0149 / 0.0177 ms
- Completion latency p50/p95/p99: 0.4728 / 0.5256 / 0.5659 ms
- GPU allocated peak: 753664 bytes; host RSS peak: 14434304 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.44% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
