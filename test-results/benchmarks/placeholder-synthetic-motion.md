# Frame-generation benchmark: placeholder-metal-synthetic-motion-v1

- Backend: metal_placeholder_blend (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel error max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.2013 / 0.6575 | 0.3671 / 1.3150 | 0.0598 / 0.0780 | 0.8648 | 0.6575 |
| hud | 0.0000 / 0.6471 | 0.1078 / 1.2941 | 0.0079 / 0.0200 | 0.9983 | 0.6471 |
| text | 0.6471 / 0.6471 | 1.1515 / 1.2941 | 0.0009 / 0.0022 | 1.0000 | 0.6471 |
| scene | 0.2039 / 0.6575 | 0.3817 / 1.3150 | 0.0870 / 0.1073 | 0.7990 | 0.6575 |
| occlusion | 0.2104 / 0.5765 | 0.4214 / 1.1529 | 0.5396 / 1.0000 | 0.0000 | 0.5765 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only. Exact-match PSNR is capped at 120 dB for finite JSON.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 23.849 | 0.82473 | 0.88741 | 3.5114 |
| hud | 24.911 | 0.96638 | 0.96775 | 1.4719 |
| text | 16.959 | 0.74029 | 0.82949 | 5.9147 |
| scene | 23.677 | 0.79344 | 0.86933 | 3.9000 |
| occlusion | 20.984 | 0.22031 | 0.36916 | 14.2302 |

## Runtime

- GPU time p50/p95/p99: 0.0109 / 0.0130 / 0.0216 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.0115 / 0.0161 / 0.0232 ms
- Completion latency p50/p95/p99: 0.3515 / 0.4172 / 0.4309 ms
- GPU allocated peak: 770048 bytes; host RSS peak: 13959168 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.28% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
