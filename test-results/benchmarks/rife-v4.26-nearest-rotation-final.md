# Frame-generation benchmark: 20261001T172732Z

- Backend: practical_rife_v4_26_metal (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.3438 / 0.7948 | 0.6565 / 1.5895 | 0.0703 / 0.1046 | 0.8472 | 0.0404 / 0.3360 / 0.7948 |
| hud | 0.3708 / 0.7948 | 0.7180 / 1.5895 | 0.0289 / 0.0389 | 0.9348 | 0.0400 / 0.3668 / 0.7948 |
| text | 0.7115 / 0.7438 | 1.4148 / 1.4876 | 0.0187 / 0.0256 | 0.9314 | 0.1751 / 0.7101 / 0.7438 |
| scene | 0.3438 / 0.7150 | 0.6562 / 1.4301 | 0.1152 / 0.1675 | 0.7453 | 0.0397 / 0.3362 / 0.6889 |
| occlusion | 0.2401 / 0.6549 | 0.3964 / 1.3098 | 0.2180 / 0.3947 | 0.0690 | 0.0502 / 0.2102 / 0.6549 |
| thin_geometry | 0.4235 / 0.6837 | 0.8293 / 1.3673 | 0.1729 / 0.2970 | 0.6090 | 0.1676 / 0.4227 / 0.6837 |
| crosshair | 0.4235 / 0.7948 | 0.8439 / 1.5895 | 0.0374 / 0.0473 | 0.9583 | 0.0859 / 0.4227 / 0.7948 |
| crosshair_pixels | 0.2573 / 0.7948 | 0.4215 / 1.5895 | 0.0252 / 0.0606 | 1.0000 | 0.0337 / 0.2309 / 0.7948 |
| weapon_sight | 0.4235 / 0.7948 | 0.8370 / 1.5895 | 0.0600 / 0.0795 | 0.9237 | 0.0859 / 0.4210 / 0.7948 |
| minimap | 0.0144 / 0.3961 | 0.0277 / 0.7922 | 0.0024 / 0.0062 | 1.0000 | 0.0043 / 0.0140 / 0.3961 |
| static_text_pixels | 0.0683 / 0.0941 | 0.1298 / 0.1882 | 0.0000 / 0.0000 | 1.0000 | 0.0195 / 0.0652 / 0.0941 |
| health_bar | 0.3605 / 0.3804 | 0.6246 / 0.7608 | 0.0051 / 0.0114 | 1.0000 | 0.0370 / 0.3131 / 0.3804 |
| subtitle | 0.0539 / 0.0941 | 0.1007 / 0.1882 | 0.0001 / 0.0007 | 1.0000 | 0.0067 / 0.0515 / 0.0941 |
| hud_counter | 0.6471 / 0.6471 | 1.2941 / 1.2941 | 0.0172 / 0.0288 | 0.9683 | 0.0715 / 0.6471 / 0.6471 |
| timer | 0.0370 / 0.7438 | 0.0947 / 1.4876 | 0.0073 / 0.0108 | 1.0000 | 0.0115 / 0.0365 / 0.7438 |
| scrolling_text | 0.5529 / 0.6549 | 1.0667 / 1.3098 | 0.0913 / 0.1235 | 0.8214 | 0.0786 / 0.5431 / 0.6549 |
| flashing_ui | 0.1889 / 0.3098 | 0.3329 / 0.6196 | 0.2668 / 1.0000 | 0.0000 | 0.0118 / 0.1694 / 0.3098 |
| transparent_ui | 0.2813 / 0.6641 | 0.4706 / 1.3281 | 0.0096 / 0.0202 | 0.9558 | 0.0231 / 0.2478 / 0.6641 |
| moving_menu | 0.7059 / 0.7150 | 1.4118 / 1.4301 | 0.0499 / 0.1557 | 0.7358 | 0.1127 / 0.7059 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 18.625 | 0.68376 | 0.77934 | 6.1581 |
| hud | 17.268 | 0.78599 | 0.84335 | 5.6477 |
| text | 9.776 | 0.31484 | 0.50221 | 22.3662 |
| scene | 19.758 | 0.63433 | 0.74548 | 6.2480 |
| occlusion | 21.303 | 0.42964 | 0.54664 | 9.5100 |
| thin_geometry | 12.763 | 0.24950 | 0.42616 | 20.3315 |
| crosshair | 15.901 | 0.73812 | 0.78705 | 10.3620 |
| crosshair_pixels | 43.868 | 0.67194 | 0.81044 | 3.1347 |
| weapon_sight | 15.720 | 0.56603 | 0.71164 | 10.8106 |
| minimap | 36.097 | 0.98428 | 0.98247 | 0.8547 |
| static_text_pixels | 48.301 | 0.80364 | 0.91799 | 1.8340 |
| health_bar | 21.739 | 0.53666 | 0.71564 | 7.9223 |
| subtitle | 52.146 | 0.99815 | 0.99057 | 0.8715 |
| hud_counter | 14.086 | 0.66732 | 0.72093 | 11.3808 |
| timer | 22.746 | 0.95400 | 0.95851 | 1.3526 |
| scrolling_text | 14.769 | 0.47480 | 0.60147 | 11.7329 |
| flashing_ui | 29.761 | 0.83749 | 0.89565 | 3.4555 |
| transparent_ui | 24.830 | 0.85951 | 0.91122 | 3.1581 |
| moving_menu | 11.405 | 0.41925 | 0.61756 | 13.8599 |

## Runtime

- GPU time p50/p95/p99: 2.6187 / 8.0066 / 8.4264 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.9581 / 1.1373 / 1.2718 ms
- Completion latency p50/p95/p99: 3.1557 / 8.5739 / 8.9796 ms
- GPU allocated peak: 42401792 bytes; host RSS peak: 91389952 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.92% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
