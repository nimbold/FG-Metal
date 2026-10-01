# Frame-generation benchmark: 20261001T133901Z

- Backend: practical_rife_v4_26_metal (org.framegen.metal)
- Corpus: synthetic-motion-v1 at 128x72
- Interpolation targets: 5
- License: Apache-2.0 (project-authored code and generated fixture)

## Temporal and edge quality

| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |
| --- | ---: | ---: | ---: | ---: | ---: |
| all | 0.2993 / 0.7438 | 0.5041 / 1.4876 | 0.0404 / 0.0520 | 0.9317 | 0.0231 / 0.2742 / 0.7438 |
| hud | 0.1699 / 0.7438 | 0.4013 / 1.4876 | 0.0233 / 0.0319 | 0.9669 | 0.0277 / 0.1695 / 0.7438 |
| text | 0.7041 / 0.7438 | 1.3958 / 1.4876 | 0.0170 / 0.0237 | 0.9434 | 0.1517 / 0.6795 / 0.7438 |
| scene | 0.3168 / 0.7150 | 0.5208 / 1.4301 | 0.0570 / 0.0779 | 0.8985 | 0.0204 / 0.2896 / 0.6784 |
| occlusion | 0.1634 / 0.6588 | 0.3268 / 1.3176 | 0.1080 / 0.1716 | 0.6105 | 0.0414 / 0.1634 / 0.6588 |
| thin_geometry | 0.4135 / 0.6837 | 0.8221 / 1.3673 | 0.1671 / 0.2149 | 0.7151 | 0.0703 / 0.4097 / 0.6837 |
| crosshair | 0.4118 / 0.5778 | 0.8152 / 1.1556 | 0.0185 / 0.0360 | 1.0000 | 0.0371 / 0.4097 / 0.5778 |
| crosshair_pixels | 0.0750 / 0.0771 | 0.1335 / 0.1542 | 0.0240 / 0.0545 | 0.8889 | 0.0154 / 0.0709 / 0.0771 |
| weapon_sight | 0.4055 / 0.5817 | 0.8108 / 1.1634 | 0.0339 / 0.0623 | 0.9653 | 0.0505 / 0.4054 / 0.5817 |
| minimap | 0.0118 / 0.3961 | 0.0225 / 0.7922 | 0.0031 / 0.0062 | 0.9894 | 0.0034 / 0.0115 / 0.3961 |
| static_text_pixels | 0.0438 / 0.0614 | 0.0850 / 0.1229 | 0.0000 / 0.0000 | 1.0000 | 0.0095 / 0.0431 / 0.0614 |
| health_bar | 0.2654 / 0.2654 | 0.4246 / 0.5307 | 0.0191 / 0.0833 | 1.0000 | 0.0204 / 0.2257 / 0.2654 |
| subtitle | 0.0261 / 0.0745 | 0.0523 / 0.1490 | 0.0000 / 0.0000 | 1.0000 | 0.0034 / 0.0261 / 0.0745 |
| hud_counter | 0.6467 / 0.6471 | 1.2902 / 1.2941 | 0.0133 / 0.0173 | 1.0000 | 0.0578 / 0.6459 / 0.6471 |
| timer | 0.0170 / 0.7438 | 0.0470 / 1.4876 | 0.0067 / 0.0115 | 0.9828 | 0.0117 / 0.0159 / 0.7438 |
| scrolling_text | 0.5085 / 0.6837 | 0.9767 / 1.3673 | 0.0707 / 0.0879 | 0.8247 | 0.0467 / 0.4984 / 0.6837 |
| flashing_ui | 0.0438 / 0.1529 | 0.0803 / 0.3059 | 0.0000 / 0.0000 | 1.0000 | 0.0034 / 0.0382 / 0.1529 |
| transparent_ui | 0.1609 / 0.2771 | 0.2906 / 0.5542 | 0.0211 / 0.0735 | 0.8908 | 0.0140 / 0.1531 / 0.2771 |
| moving_menu | 0.6929 / 0.7150 | 1.3742 / 1.4301 | 0.0425 / 0.1146 | 0.8962 | 0.1011 / 0.6900 / 0.7150 |

Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. HUD, text, scene, and occlusion masks are scored independently and may overlap.

## Image quality diagnostics

PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.
The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.

| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |
| --- | ---: | ---: | ---: | ---: |
| all | 20.537 | 0.81495 | 0.87292 | 3.7030 |
| hud | 18.433 | 0.85049 | 0.88903 | 3.9708 |
| text | 10.489 | 0.37696 | 0.54541 | 19.5523 |
| scene | 22.506 | 0.80179 | 0.86793 | 3.4816 |
| occlusion | 22.086 | 0.53341 | 0.62467 | 8.0124 |
| thin_geometry | 17.353 | 0.38743 | 0.58361 | 8.9445 |
| crosshair | 23.437 | 0.88682 | 0.90819 | 4.6285 |
| crosshair_pixels | 34.014 | 0.80376 | 0.89926 | 1.3827 |
| weapon_sight | 18.991 | 0.77161 | 0.83915 | 7.1239 |
| minimap | 36.317 | 0.98811 | 0.98783 | 0.6677 |
| static_text_pixels | 35.947 | 0.85217 | 0.94415 | 0.9097 |
| health_bar | 23.618 | 0.83895 | 0.84879 | 5.8790 |
| subtitle | 40.487 | 0.99554 | 0.99239 | 0.5732 |
| hud_counter | 14.175 | 0.72036 | 0.76538 | 9.1553 |
| timer | 22.337 | 0.94699 | 0.95614 | 1.3688 |
| scrolling_text | 16.351 | 0.67085 | 0.74771 | 7.5063 |
| flashing_ui | 34.462 | 0.93186 | 0.95690 | 1.4200 |
| transparent_ui | 27.045 | 0.89313 | 0.93427 | 1.9865 |
| moving_menu | 12.180 | 0.46401 | 0.63710 | 12.6806 |

## Runtime

- GPU time p50/p95/p99: 2.5878 / 2.6722 / 3.3447 ms (missing samples: 0)
- CPU submit p50/p95/p99: 0.9287 / 1.0006 / 1.0895 ms
- Completion latency p50/p95/p99: 3.1007 / 3.2423 / 3.8890 ms
- GPU allocated peak: 42319872 bytes; host RSS peak: 90783744 bytes
- Deadline misses: 0 / 500 at 16.6667 ms; estimated generated drops: 0
- Source-frame FPS impact proxy: 99.91% (measured)
- Throughput method: 100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; two source-frame texture uploads plus serial submit-to-completion per cycle, divided by measured wall time from before uploads through completion; offline benchmark host only; excludes renderer work and presentation
- Impact scope: offline input-throughput proxy only; excludes renderer work, game FPS, and presentation

Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.
Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation.
