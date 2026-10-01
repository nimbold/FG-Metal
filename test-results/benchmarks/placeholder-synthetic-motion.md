# Frame-generation benchmark: 20261001T121312Z

- Backend: metal_placeholder_blend (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.2131 / 0.7438 | 0.4167 / 1.4876 | 0.0563 / 0.0771 | 0.8749 | 0.0268 / 0.2107 / 0.7438 |
| hud | 0.2876 / 0.7438 | 0.5752 / 1.4876 | 0.0235 / 0.0316 | 0.9651 | 0.0295 / 0.2858 / 0.7438 |
| text | 0.7059 / 0.7438 | 1.4110 / 1.4876 | 0.0162 / 0.0168 | 0.9663 | 0.1393 / 0.6797 / 0.7438 |
| scene | 0.2125 / 0.7098 | 0.4157 / 1.4196 | 0.0842 / 0.1151 | 0.8017 | 0.0244 / 0.2094 / 0.6575 |
| occlusion | 0.2104 / 0.5765 | 0.4214 / 1.1529 | 0.5761 / 1.0000 | 0.0000 | 0.0711 / 0.1869 / 0.5765 |
| thin_geometry | 0.4235 / 0.6575 | 0.6787 / 1.3150 | 0.2710 / 0.2907 | 0.3964 | 0.0792 / 0.3814 / 0.6575 |
| crosshair | 0.4013 / 0.4013 | 0.6520 / 0.8026 | 0.0222 / 0.0317 | 0.9875 | 0.0485 / 0.3637 / 0.4013 |
| crosshair_pixels | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0345 / 0.0521 | 0.7500 | 0.0000 / 0.0000 / 0.0000 |
| weapon_sight | 0.3971 / 0.5817 | 0.6635 / 1.1634 | 0.0365 / 0.0615 | 0.8299 | 0.0558 / 0.3644 / 0.5817 |
| minimap | 0.0000 / 0.3961 | 0.0000 / 0.7922 | 0.0024 / 0.0046 | 0.9947 | 0.0023 / 0.0000 / 0.3961 |
| static_text_pixels | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 1.0000 | 0.0000 / 0.0000 / 0.0000 |
| health_bar | 0.1333 / 0.1333 | 0.2656 / 0.2667 | 0.0142 / 0.0648 | 1.0000 | 0.0138 / 0.1331 / 0.1333 |
| subtitle | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 1.0000 | 0.0000 / 0.0000 / 0.0000 |
| hud_counter | 0.6471 / 0.6471 | 1.2941 / 1.2941 | 0.0112 / 0.0161 | 1.0000 | 0.0529 / 0.6471 / 0.6471 |
| timer | 0.0000 / 0.7438 | 0.0000 / 1.4876 | 0.0069 / 0.0119 | 1.0000 | 0.0118 / 0.0000 / 0.7438 |
| scrolling_text | 0.3333 / 0.6575 | 0.6573 / 1.3150 | 0.0665 / 0.0824 | 0.8693 | 0.0493 / 0.3310 / 0.6575 |
| flashing_ui | 0.1778 / 0.2222 | 0.3822 / 0.4444 | 0.0000 / 0.0000 | 1.0000 | 0.0755 / 0.1778 / 0.2222 |
| transparent_ui | 0.0915 / 0.1791 | 0.1830 / 0.3582 | 0.0165 / 0.0546 | 0.8908 | 0.0083 / 0.0915 / 0.1791 |
| moving_menu | 0.3634 / 0.7098 | 0.7210 / 1.4196 | 0.0641 / 0.1365 | 0.8585 | 0.0971 / 0.3616 / 0.7098 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 21.204 | 0.79188 | 0.85945 | 4.7174 |
| hud | 19.192 | 0.84991 | 0.88134 | 4.9079 |
| text | 11.765 | 0.48000 | 0.61122 | 17.5386 |
| scene | 23.028 | 0.77013 | 0.85237 | 4.4475 |
| occlusion | 21.112 | 0.23151 | 0.37690 | 13.9940 |
| thin_geometry | 17.319 | 0.14014 | 0.44233 | 10.1420 |
| crosshair | 20.072 | 0.89700 | 0.89961 | 6.1229 |
| crosshair_pixels | 120.000 | 1.00000 | 0.98827 | 0.0000 |
| weapon_sight | 19.231 | 0.77768 | 0.84052 | 7.9068 |
| minimap | 65.504 | 0.98952 | 0.98926 | 0.5386 |
| static_text_pixels | 120.000 | 1.00000 | 1.00000 | 0.0000 |
| health_bar | 26.040 | 0.88211 | 0.90330 | 4.0505 |
| subtitle | 120.000 | 1.00000 | 0.99998 | 0.0000 |
| hud_counter | 15.602 | 0.77853 | 0.81152 | 8.2753 |
| timer | 22.943 | 0.95456 | 0.95803 | 1.3158 |
| scrolling_text | 16.993 | 0.64696 | 0.73896 | 8.1718 |
| flashing_ui | 23.092 | 0.64206 | 0.63706 | 25.6184 |
| transparent_ui | 28.752 | 0.89296 | 0.94094 | 1.3697 |
| moving_menu | 13.663 | 0.50785 | 0.65695 | 12.3320 |

## Runtime

- GPU time p50/p95/p99: 0.0109 / 0.0145 / 0.0167 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.0110 / 0.0155 / 0.0246 ms
- Completion latency p50/p95/p99: 0.3717 / 0.4660 / 0.6177 ms
- GPU allocated peak: 704512 bytes; host RSS peak: 14467072 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.31% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
