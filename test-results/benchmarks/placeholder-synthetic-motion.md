# Frame-generation benchmark: placeholder-synthetic-motion-v4

- Backend: metal_placeholder_blend (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.2013 / 0.6575 | 0.3671 / 1.3150 | 0.0598 / 0.0780 | 0.8648 | 0.0190 / 0.1924 / 0.6575 |
| hud | 0.0000 / 0.6471 | 0.1078 / 1.2941 | 0.0079 / 0.0200 | 0.9983 | 0.0092 / 0.0000 / 0.6471 |
| text | 0.6471 / 0.6471 | 1.1515 / 1.2941 | 0.0009 / 0.0022 | 1.0000 | 0.0394 / 0.3229 / 0.6471 |
| scene | 0.2039 / 0.6575 | 0.3817 / 1.3150 | 0.0870 / 0.1073 | 0.7990 | 0.0208 / 0.1974 / 0.6575 |
| occlusion | 0.2104 / 0.5765 | 0.4214 / 1.1529 | 0.5396 / 1.0000 | 0.0000 | 0.0731 / 0.2052 / 0.5765 |
| thin_geometry | 0.4235 / 0.6575 | 0.6787 / 1.3150 | 0.2887 / 0.3083 | 0.3701 | 0.0758 / 0.3814 / 0.6575 |
| crosshair | 0.4013 / 0.4013 | 0.6520 / 0.8026 | 0.0222 / 0.0317 | 0.9875 | 0.0485 / 0.3637 / 0.4013 |
| weapon_sight | 0.3971 / 0.5817 | 0.6639 / 1.1634 | 0.0369 / 0.0615 | 0.8299 | 0.0558 / 0.3645 / 0.5817 |
| minimap | 0.0000 / 0.3961 | 0.0000 / 0.7922 | 0.0024 / 0.0046 | 0.9947 | 0.0023 / 0.0000 / 0.3961 |
| health_bar | 0.1333 / 0.2654 | 0.2661 / 0.5307 | 0.0193 / 0.0833 | 1.0000 | 0.0158 / 0.1331 / 0.2654 |
| subtitle | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 0.0000 / 0.0000 | 1.0000 | 0.0000 / 0.0000 / 0.0000 |
| hud_counter | 0.6471 / 0.6471 | 1.0358 / 1.2941 | 0.0109 / 0.0206 | 1.0000 | 0.0516 / 0.5825 / 0.6471 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 23.848 | 0.82473 | 0.88741 | 3.5113 |
| hud | 24.910 | 0.96638 | 0.96775 | 1.4719 |
| text | 16.959 | 0.74029 | 0.82949 | 5.9147 |
| scene | 23.676 | 0.79343 | 0.86933 | 3.8999 |
| occlusion | 20.984 | 0.22031 | 0.36916 | 14.2302 |
| thin_geometry | 17.701 | 0.16106 | 0.46871 | 9.3791 |
| crosshair | 20.069 | 0.89699 | 0.89960 | 6.1226 |
| weapon_sight | 19.229 | 0.77767 | 0.84047 | 7.9060 |
| minimap | 65.504 | 0.98952 | 0.98926 | 0.5386 |
| health_bar | 25.594 | 0.85968 | 0.88451 | 4.7097 |
| subtitle | 120.000 | 1.00000 | 0.99999 | 0.0000 |
| hud_counter | 15.693 | 0.78792 | 0.81412 | 8.0598 |

## Runtime

- GPU time p50/p95/p99: 0.0109 / 0.0135 / 0.0211 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.0117 / 0.0260 / 0.0383 ms
- Completion latency p50/p95/p99: 0.3739 / 0.4705 / 0.7943 ms
- GPU allocated peak: 704512 bytes; host RSS peak: 14204928 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.29% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
