#!/usr/bin/env python3
"""Compare two 8-bit RGB P6 PPM images with NumPy metrics."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

from ppm_utils import read_ppm


def global_errors(reference: np.ndarray, candidate: np.ndarray) -> dict[str, float | None]:
    delta = np.abs(reference.astype(np.float64) - candidate.astype(np.float64))
    mse = float(np.mean(delta * delta))
    psnr = None if mse == 0.0 else 20.0 * math.log10(255.0 / math.sqrt(mse))
    return {
        "mae_8bit": float(np.mean(delta)),
        "p95_abs_error_8bit": float(np.percentile(delta, 95.0)),
        "max_abs_error_8bit": float(np.max(delta)),
        "psnr_db": psnr,
    }


def ssim_wang_11x11(reference: np.ndarray, candidate: np.ndarray) -> float:
    """Wang-style SSIM with an 11x11, sigma=1.5 Gaussian window, valid region."""
    if reference.shape[0] < 11 or reference.shape[1] < 11:
        raise ValueError("SSIM requires images at least 11x11")
    coordinates = np.arange(-5, 6, dtype=np.float64)
    one_d = np.exp(-(coordinates * coordinates) / (2.0 * 1.5 * 1.5))
    one_d /= np.sum(one_d)
    kernel = np.outer(one_d, one_d)

    x = reference.astype(np.float64)
    y = candidate.astype(np.float64)
    x_windows = np.lib.stride_tricks.sliding_window_view(x, (11, 11), axis=(0, 1))
    y_windows = np.lib.stride_tricks.sliding_window_view(y, (11, 11), axis=(0, 1))
    weights = kernel[None, None, None, :, :]

    mean_x = np.sum(x_windows * weights, axis=(-1, -2))
    mean_y = np.sum(y_windows * weights, axis=(-1, -2))
    centered_x = x_windows - mean_x[..., None, None]
    centered_y = y_windows - mean_y[..., None, None]
    variance_x = np.sum(weights * centered_x * centered_x, axis=(-1, -2))
    variance_y = np.sum(weights * centered_y * centered_y, axis=(-1, -2))
    covariance = np.sum(weights * centered_x * centered_y, axis=(-1, -2))

    c1 = (0.01 * 255.0) ** 2
    c2 = (0.03 * 255.0) ** 2
    numerator = (2.0 * mean_x * mean_y + c1) * (2.0 * covariance + c2)
    denominator = (mean_x * mean_x + mean_y * mean_y + c1) * (variance_x + variance_y + c2)
    return float(np.mean(numerator / denominator))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", type=Path, help="official PyTorch reference PPM")
    parser.add_argument("candidate", type=Path, help="Metal host output PPM")
    parser.add_argument("--json-out", type=Path, help="optional path to write the metric JSON")
    args = parser.parse_args()

    try:
        reference_width, reference_height, reference_raw = read_ppm(args.reference)
        candidate_width, candidate_height, candidate_raw = read_ppm(args.candidate)
        if (reference_width, reference_height) != (candidate_width, candidate_height):
            raise ValueError(
                f"image dimensions differ: reference={reference_width}x{reference_height}, "
                f"candidate={candidate_width}x{candidate_height}"
            )
        shape = (reference_height, reference_width, 3)
        reference = np.frombuffer(reference_raw, dtype=np.uint8).reshape(shape)
        candidate = np.frombuffer(candidate_raw, dtype=np.uint8).reshape(shape)
        metrics = global_errors(reference, candidate)
        metrics["ssim_wang_11x11_gaussian_sigma_1_5"] = ssim_wang_11x11(reference, candidate)
        result = {
            "reference": str(args.reference),
            "candidate": str(args.candidate),
            "width": reference_width,
            "height": reference_height,
            "channels": 3,
            "metrics": metrics,
            "metric_definition": {
                "mae_p95_max": "absolute error over all 8-bit RGB channel samples; p95 uses NumPy linear percentile",
                "psnr": "20*log10(255/sqrt(MSE)); MSE over all RGB samples; null means identical images and infinite PSNR",
                "ssim": "channel-averaged Wang-style SSIM; 11x11 sigma-1.5 Gaussian, valid windows, population moments, C1=(0.01*255)^2, C2=(0.03*255)^2",
            },
        }
        print(json.dumps(result, indent=2, sort_keys=True, allow_nan=False))
        if args.json_out is not None:
            args.json_out.parent.mkdir(parents=True, exist_ok=True)
            args.json_out.write_text(json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n")
        return 0
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
