# Frame-generation benchmark: 20261001T172714Z

- Backend: practical_rife_v4_26_metal (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.3438 / 0.7948 | 0.6562 / 1.5895 | 0.0701 / 0.1046 | 0.8472 | 0.0403 / 0.3359 / 0.7948 |
| hud | 0.3664 / 0.7948 | 0.7108 / 1.5895 | 0.0288 / 0.0384 | 0.9348 | 0.0398 / 0.3626 / 0.7948 |
| text | 0.7106 / 0.7438 | 1.4128 / 1.4876 | 0.0186 / 0.0256 | 0.9314 | 0.1736 / 0.7090 / 0.7438 |
| scene | 0.3438 / 0.7150 | 0.6562 / 1.4301 | 0.1149 / 0.1675 | 0.7453 | 0.0397 / 0.3362 / 0.6889 |
| occlusion | 0.2401 / 0.6549 | 0.3964 / 1.3098 | 0.2191 / 0.3947 | 0.0690 | 0.0503 / 0.2102 / 0.6549 |
| thin_geometry | 0.4235 / 0.6837 | 0.8293 / 1.3673 | 0.1714 / 0.2970 | 0.6090 | 0.1679 / 0.4227 / 0.6837 |
| crosshair | 0.4235 / 0.7948 | 0.8439 / 1.5895 | 0.0367 / 0.0471 | 0.9583 | 0.0856 / 0.4227 / 0.7948 |
| crosshair_pixels | 0.2395 / 0.7948 | 0.4002 / 1.5895 | 0.0252 / 0.0606 | 1.0000 | 0.0320 / 0.2106 / 0.7948 |
| weapon_sight | 0.4235 / 0.7948 | 0.8370 / 1.5895 | 0.0594 / 0.0766 | 0.9237 | 0.0857 / 0.4210 / 0.7948 |
| minimap | 0.0144 / 0.3961 | 0.0277 / 0.7922 | 0.0024 / 0.0062 | 1.0000 | 0.0043 / 0.0140 / 0.3961 |
| static_text_pixels | 0.0683 / 0.0941 | 0.1298 / 0.1882 | 0.0000 / 0.0000 | 1.0000 | 0.0195 / 0.0652 / 0.0941 |
| health_bar | 0.3605 / 0.3804 | 0.6246 / 0.7608 | 0.0051 / 0.0114 | 1.0000 | 0.0352 / 0.3125 / 0.3804 |
| subtitle | 0.0539 / 0.0941 | 0.1007 / 0.1882 | 0.0001 / 0.0007 | 1.0000 | 0.0067 / 0.0515 / 0.0941 |
| hud_counter | 0.6471 / 0.6471 | 1.2941 / 1.2941 | 0.0172 / 0.0288 | 0.9683 | 0.0712 / 0.6471 / 0.6471 |
| timer | 0.0370 / 0.7438 | 0.0947 / 1.4876 | 0.0073 / 0.0108 | 1.0000 | 0.0115 / 0.0365 / 0.7438 |
| scrolling_text | 0.5529 / 0.6549 | 1.0667 / 1.3098 | 0.0915 / 0.1235 | 0.8214 | 0.0786 / 0.5431 / 0.6549 |
| flashing_ui | 0.1845 / 0.3098 | 0.3241 / 0.6196 | 0.2668 / 1.0000 | 0.0000 | 0.0116 / 0.1644 / 0.3098 |
| transparent_ui | 0.2108 / 0.6418 | 0.3518 / 1.2837 | 0.0097 / 0.0202 | 0.9558 | 0.0221 / 0.1914 / 0.6418 |
| moving_menu | 0.7059 / 0.7150 | 1.4118 / 1.4301 | 0.0495 / 0.1557 | 0.7358 | 0.1126 / 0.7059 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 18.643 | 0.68405 | 0.77963 | 6.1501 |
| hud | 17.297 | 0.78714 | 0.84415 | 5.6202 |
| text | 9.818 | 0.32257 | 0.50728 | 22.2191 |
| scene | 19.785 | 0.63459 | 0.74569 | 6.2448 |
| occlusion | 21.285 | 0.42852 | 0.54613 | 9.5252 |
| thin_geometry | 12.759 | 0.25097 | 0.42743 | 20.3581 |
| crosshair | 15.910 | 0.73888 | 0.78804 | 10.3381 |
| crosshair_pixels | 44.035 | 0.68811 | 0.81990 | 2.9728 |
| weapon_sight | 15.730 | 0.56724 | 0.71272 | 10.7858 |
| minimap | 36.097 | 0.98428 | 0.98247 | 0.8547 |
| static_text_pixels | 48.332 | 0.80609 | 0.91890 | 1.8309 |
| health_bar | 22.023 | 0.55490 | 0.72976 | 7.5569 |
| subtitle | 52.162 | 0.99816 | 0.99058 | 0.8709 |
| hud_counter | 14.130 | 0.66985 | 0.72197 | 11.3330 |
| timer | 22.767 | 0.95415 | 0.95859 | 1.3526 |
| scrolling_text | 14.771 | 0.47485 | 0.60168 | 11.7292 |
| flashing_ui | 30.098 | 0.83655 | 0.89583 | 3.4089 |
| transparent_ui | 25.042 | 0.86693 | 0.91533 | 3.0594 |
| moving_menu | 11.414 | 0.41990 | 0.61801 | 13.8468 |

## Runtime

- GPU time p50/p95/p99: 2.6136 / 8.0198 / 8.2761 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.9555 / 1.1198 / 1.2548 ms
- Completion latency p50/p95/p99: 3.1461 / 8.5625 / 8.8424 ms
- GPU allocated peak: 42401792 bytes; host RSS peak: 91324416 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.92% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
