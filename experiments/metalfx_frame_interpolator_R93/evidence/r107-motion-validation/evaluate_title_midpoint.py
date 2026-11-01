#!/usr/bin/env python3
"""Measure one R107 MetalFX midpoint against its exact SOURCE endpoints."""

from __future__ import annotations

import argparse
import json
import math
import struct
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-a", required=True, help="Previous tightly packed BGRA8 SOURCE raw")
    parser.add_argument("--source-b", required=True, help="Current tightly packed BGRA8 SOURCE raw")
    parser.add_argument("--midpoint", required=True, help="Post-generation RGBA16Float raw")
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--source-a-sequence", required=True, type=int)
    parser.add_argument("--source-b-sequence", required=True, type=int)
    parser.add_argument("--json-out", required=True)
    args = parser.parse_args()

    if args.width <= 0 or args.height <= 0 or args.source_b_sequence != args.source_a_sequence + 1:
        parser.error("positive dimensions and consecutive SOURCE A/B sequences are required")

    source_a = Path(args.source_a).read_bytes()
    source_b = Path(args.source_b).read_bytes()
    midpoint = Path(args.midpoint).read_bytes()
    pixel_count = args.width * args.height
    if len(source_a) != pixel_count * 4 or len(source_b) != pixel_count * 4:
        parser.error("SOURCE raw byte size does not match tightly packed BGRA8 dimensions")
    if len(midpoint) != pixel_count * 8:
        parser.error("midpoint byte size does not match tightly packed RGBA16Float dimensions")

    errors_a = 0.0
    errors_b = 0.0
    finite_values = 0
    nonfinite_values = 0
    meaningful_pixels = 0
    valid_rgb_pixels = 0
    alpha_min = math.inf
    alpha_max = -math.inf
    for i, rgba in enumerate(struct.iter_unpack("<4e", midpoint)):
        rgb = rgba[:3]
        alpha = rgba[3]
        finite_values += sum(math.isfinite(value) for value in rgba)
        nonfinite_values += sum(not math.isfinite(value) for value in rgba)
        if math.isfinite(alpha):
            alpha_min = min(alpha_min, alpha)
            alpha_max = max(alpha_max, alpha)
        if all(math.isfinite(value) for value in rgb):
            valid_rgb_pixels += 1
            meaningful_pixels += any(abs(value) > 0.01 for value in rgb)
            byte_offset = i * 4
            # SOURCE files are BGRA8; compare MetalFX RGB against normalized RGB.
            a = (source_a[byte_offset + 2], source_a[byte_offset + 1], source_a[byte_offset])
            b = (source_b[byte_offset + 2], source_b[byte_offset + 1], source_b[byte_offset])
            errors_a += sum(abs(rgb[c] - a[c] / 255.0) for c in range(3))
            errors_b += sum(abs(rgb[c] - b[c] / 255.0) for c in range(3))

    denom = max(valid_rgb_pixels * 3, 1)
    mae_a = errors_a / denom
    mae_b = errors_b / denom
    result = {
        "schema": "fgmetal-r107-title-midpoint-evaluation-v1",
        "pair": {
            "source_a_sequence": args.source_a_sequence,
            "source_b_sequence": args.source_b_sequence,
            "width": args.width,
            "height": args.height,
        },
        "source_endpoints_byte_identical": source_a == source_b,
        "midpoint": {
            "finite_values": finite_values,
            "nonfinite_values": nonfinite_values,
            "meaningful_pixels": meaningful_pixels,
            "valid_rgb_pixels": valid_rgb_pixels,
            "alpha_min": alpha_min if math.isfinite(alpha_min) else None,
            "alpha_max": alpha_max if math.isfinite(alpha_max) else None,
            "mae_rgb_0_1_vs_source_a": mae_a,
            "mae_rgb_0_1_vs_source_b": mae_b,
            "distinct_from_source_a": mae_a > 0.0001,
            "distinct_from_source_b": mae_b > 0.0001,
        },
    }
    Path(args.json_out).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
