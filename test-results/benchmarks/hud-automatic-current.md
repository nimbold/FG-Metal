# Frame-generation benchmark: 20261001T123557Z

- Backend: metal_placeholder_blend (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.2222 / 0.7438 | 0.4361 / 1.4876 | 0.0543 / 0.0738 | 0.8769 | 0.0257 / 0.2201 / 0.7438 |
| hud | 0.2766 / 0.7438 | 0.5144 / 1.4876 | 0.0231 / 0.0293 | 0.9606 | 0.0292 / 0.2690 / 0.7438 |
| text | 0.7059 / 0.7438 | 1.4114 / 1.4876 | 0.0143 / 0.0168 | 0.9663 | 0.1402 / 0.7017 / 0.7438 |
| scene | 0.2200 / 0.7098 | 0.4289 / 1.4196 | 0.0814 / 0.1113 | 0.8017 | 0.0231 / 0.2154 / 0.6575 |
| occlusion | 0.2713 / 0.6275 | 0.5532 / 1.2549 | 0.5771 / 1.0000 | 0.0000 | 0.0708 / 0.1663 / 0.5765 |
| thin_geometry | 0.4235 / 0.6680 | 0.8295 / 1.3359 | 0.2334 / 0.2680 | 0.4815 | 0.0707 / 0.4154 / 0.6680 |
| crosshair | 0.4013 / 0.5778 | 0.7911 / 1.1556 | 0.0200 / 0.0287 | 0.9875 | 0.0393 / 0.3984 / 0.5778 |
| crosshair_pixels | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0246 / 0.0545 | 0.8889 | 0.0000 / 0.0000 / 0.0000 |
| weapon_sight | 0.4013 / 0.5817 | 0.7880 / 1.1634 | 0.0327 / 0.0519 | 0.8912 | 0.0525 / 0.3976 / 0.5817 |
| minimap | 0.0000 / 0.3961 | 0.0000 / 0.7922 | 0.0023 / 0.0044 | 1.0000 | 0.0019 / 0.0000 / 0.3961 |
| static_text_pixels | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 1.0000 | 0.0000 / 0.0000 / 0.0000 |
| health_bar | 0.2654 / 0.2654 | 0.5307 / 0.5307 | 0.0164 / 0.0648 | 0.9565 | 0.0201 / 0.2654 / 0.2654 |
| subtitle | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 1.0000 | 0.0000 / 0.0000 / 0.0000 |
| hud_counter | 0.6471 / 0.6471 | 1.2941 / 1.2941 | 0.0123 / 0.0158 | 1.0000 | 0.0544 / 0.6471 / 0.6471 |
| timer | 0.0000 / 0.7438 | 0.0000 / 1.4876 | 0.0081 / 0.0119 | 1.0000 | 0.0142 / 0.0000 / 0.7438 |
| scrolling_text | 0.4961 / 0.6680 | 0.9699 / 1.3359 | 0.0690 / 0.0797 | 0.8543 | 0.0481 / 0.4905 / 0.6680 |
| flashing_ui | 0.1778 / 0.2222 | 0.3822 / 0.4444 | 0.0000 / 0.0000 | 1.0000 | 0.0755 / 0.1778 / 0.2222 |
| transparent_ui | 0.0915 / 0.1791 | 0.1830 / 0.3582 | 0.0167 / 0.0546 | 0.8908 | 0.0081 / 0.0915 / 0.1791 |
| moving_menu | 0.6884 / 0.7098 | 1.3700 / 1.4196 | 0.0536 / 0.1204 | 0.9245 | 0.0949 / 0.6867 / 0.7098 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 20.886 | 0.79137 | 0.85792 | 4.6102 |
| hud | 18.659 | 0.84358 | 0.87701 | 4.8755 |
| text | 11.188 | 0.47104 | 0.59262 | 17.9039 |
| scene | 23.006 | 0.77293 | 0.85287 | 4.3075 |
| occlusion | 21.106 | 0.22432 | 0.37880 | 14.0272 |
| thin_geometry | 18.004 | 0.27703 | 0.51599 | 9.2469 |
| crosshair | 20.989 | 0.89883 | 0.90980 | 5.0811 |
| crosshair_pixels | 120.000 | 1.00000 | 0.97914 | 0.0000 |
| weapon_sight | 18.920 | 0.78320 | 0.84403 | 7.5344 |
| minimap | 65.947 | 0.99073 | 0.99111 | 0.4404 |
| static_text_pixels | 120.000 | 1.00000 | 1.00000 | 0.0000 |
| health_bar | 23.107 | 0.82235 | 0.84611 | 5.8421 |
| subtitle | 120.000 | 1.00000 | 0.99998 | 0.0000 |
| hud_counter | 14.658 | 0.74315 | 0.78912 | 8.5837 |
| timer | 21.551 | 0.93700 | 0.94704 | 1.5914 |
| scrolling_text | 16.470 | 0.64034 | 0.72822 | 7.9713 |
| flashing_ui | 23.092 | 0.64206 | 0.63710 | 25.6184 |
| transparent_ui | 28.518 | 0.89117 | 0.94057 | 1.3332 |
| moving_menu | 13.327 | 0.50198 | 0.65136 | 12.0709 |

## Runtime

- GPU time p50/p95/p99: 0.0189 / 0.0200 / 0.0214 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.0119 / 0.0161 / 0.0237 ms
- Completion latency p50/p95/p99: 0.4647 / 0.5371 / 0.6508 ms
- GPU allocated peak: 753664 bytes; host RSS peak: 14450688 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.42% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
