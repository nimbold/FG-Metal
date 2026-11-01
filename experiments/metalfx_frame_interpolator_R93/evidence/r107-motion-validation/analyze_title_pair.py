#!/usr/bin/env python3
"""Bounded pairwise motion diagnostics for R107 Elden Ring SOURCE captures.

Inputs are tightly packed BGRA8 SOURCE A (previous), SOURCE B (current), and
full-resolution RG16Float vectors in current-to-previous pixel units (R=dx,
G=dy). The diagnostic compares B(x,y) against A(x+dx,y+dy), bilinearly sampled.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class Region:
    name: str
    box: tuple[int, int, int, int] | None  # x, y, width, height; None means full frame.
    pixels: int = 0
    finite: int = 0
    bounded: int = 0
    below_half: int = 0
    below_one: int = 0
    xs: list[float] = field(default_factory=list)
    ys: list[float] = field(default_factory=list)
    magnitudes: list[float] = field(default_factory=list)
    warp_valid: int = 0
    zero_error_sum: float = 0.0
    warp_error_sum: float = 0.0

    def contains(self, x: int, y: int) -> bool:
        if self.box is None:
            return True
        left, top, width, height = self.box
        return left <= x < left + width and top <= y < top + height

    def add_vector(self, dx: float, dy: float, magnitude: float, bounded: bool) -> None:
        self.pixels += 1
        if not (math.isfinite(dx) and math.isfinite(dy)):
            return
        self.finite += 1
        self.xs.append(dx)
        self.ys.append(dy)
        self.magnitudes.append(magnitude)
        self.below_half += magnitude < 0.5
        self.below_one += magnitude < 1.0
        self.bounded += bounded

    def add_warp(self, zero_error: float, warp_error: float) -> None:
        self.warp_valid += 1
        self.zero_error_sum += zero_error
        self.warp_error_sum += warp_error


def percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    rank = (len(sorted_values) - 1) * p
    low = int(rank)
    high = min(low + 1, len(sorted_values) - 1)
    weight = rank - low
    return sorted_values[low] * (1.0 - weight) + sorted_values[high] * weight


def summarize_region(region: Region, max_component_px: float) -> dict[str, Any]:
    xs = sorted(region.xs)
    ys = sorted(region.ys)
    mags = sorted(region.magnitudes)
    finite = region.finite
    zero_mae = (region.zero_error_sum / (region.warp_valid * 3 * 255.0)
                if region.warp_valid else None)
    warp_mae = (region.warp_error_sum / (region.warp_valid * 3 * 255.0)
                if region.warp_valid else None)
    return {
        "box_xywh": list(region.box) if region.box is not None else None,
        "total_vectors": region.pixels,
        "finite_vectors": finite,
        "nonfinite_vectors": region.pixels - finite,
        "bounded_vectors": region.bounded,
        "bounded_component_limit_px": max_component_px,
        "finite_percent": 100.0 * finite / region.pixels if region.pixels else None,
        "bounded_percent_of_finite": 100.0 * region.bounded / finite if finite else None,
        "median_x_px": percentile(xs, 0.5),
        "median_y_px": percentile(ys, 0.5),
        "median_magnitude_px": percentile(mags, 0.5),
        "p90_magnitude_px": percentile(mags, 0.90),
        "p99_magnitude_px": percentile(mags, 0.99),
        "max_magnitude_px": mags[-1] if mags else None,
        "below_0_5_px_count": region.below_half,
        "below_0_5_px_percent_of_finite": 100.0 * region.below_half / finite if finite else None,
        "below_1_px_count": region.below_one,
        "below_1_px_percent_of_finite": 100.0 * region.below_one / finite if finite else None,
        "warp_valid_pixels": region.warp_valid,
        "zero_motion_mae_rgb_0_1": zero_mae,
        "derived_warp_mae_rgb_0_1": warp_mae,
        "mae_improvement_zero_minus_warp": (zero_mae - warp_mae) if zero_mae is not None and warp_mae is not None else None,
    }


def parse_roi(value: str, width: int, height: int) -> Region:
    try:
        name, coords = value.split("=", 1)
        x, y, w, h = (int(part) for part in coords.split(","))
    except (ValueError, TypeError):
        raise argparse.ArgumentTypeError("ROI must be NAME=x,y,width,height")
    if not name or any(ch.isspace() for ch in name):
        raise argparse.ArgumentTypeError("ROI name must be non-empty and contain no spaces")
    if x < 0 or y < 0 or w <= 0 or h <= 0 or x + w > width or y + h > height:
        raise argparse.ArgumentTypeError(f"ROI {name!r} must be a positive box inside {width}x{height}")
    return Region(name, (x, y, w, h))


def rgb_at(data: bytes, x: int, y: int, width: int) -> tuple[int, int, int]:
    offset = (y * width + x) * 4
    # BGRA8 source files are tightly packed; ignore alpha.
    return data[offset + 2], data[offset + 1], data[offset]


def bilinear_rgb(data: bytes, sx: float, sy: float, width: int, height: int) -> tuple[float, float, float] | None:
    if sx < 0.0 or sy < 0.0 or sx > width - 1 or sy > height - 1:
        return None
    x0 = int(math.floor(sx))
    y0 = int(math.floor(sy))
    x1 = min(x0 + 1, width - 1)
    y1 = min(y0 + 1, height - 1)
    fx = sx - x0
    fy = sy - y0
    c00 = rgb_at(data, x0, y0, width)
    c10 = rgb_at(data, x1, y0, width)
    c01 = rgb_at(data, x0, y1, width)
    c11 = rgb_at(data, x1, y1, width)
    return tuple(
        (c00[c] * (1.0 - fx) + c10[c] * fx) * (1.0 - fy)
        + (c01[c] * (1.0 - fx) + c11[c] * fx) * fy
        for c in range(3)
    )


def write_output(path_text: str | None, content: str) -> None:
    if path_text is None:
        return
    if path_text == "-":
        print(content)
        return
    path = Path(path_text)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def render_markdown(report: dict[str, Any]) -> str:
    pair = report["pair"]
    lines = [
        f"# Elden Ring motion pair {pair['source_a_sequence']}→{pair['source_b_sequence']}",
        "",
        f"Condition: `{pair['condition'] or 'UNSPECIFIED'}`. Motion convention: `{report['motion']['convention']}`.",
        "",
        "| Region | Median X (px) | Median Y (px) | Median magnitude (px) | P90 | P99 | Max | <0.5 px | <1 px | Bounded / finite | Zero MAE | Warp MAE | Improvement |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, values in report["regions"].items():
        def fmt(key: str, digits: int = 4) -> str:
            value = values.get(key)
            return "n/a" if value is None else f"{value:.{digits}f}"
        bounded = f"{values['bounded_vectors']}/{values['finite_vectors']}"
        lines.append(
            f"| {name} | {fmt('median_x_px')} | {fmt('median_y_px')} | {fmt('median_magnitude_px')} | "
            f"{fmt('p90_magnitude_px')} | {fmt('p99_magnitude_px')} | {fmt('max_magnitude_px')} | "
            f"{fmt('below_0_5_px_percent_of_finite', 2)}% | {fmt('below_1_px_percent_of_finite', 2)}% | {bounded} | "
            f"{fmt('zero_motion_mae_rgb_0_1', 6)} | {fmt('derived_warp_mae_rgb_0_1', 6)} | "
            f"{fmt('mae_improvement_zero_minus_warp', 6)} |"
        )
    lines.extend([
        "",
        f"Warp-valid pixels use the same bounded-vector interior mask for both MAEs. `warp_valid_pixels` global: {report['regions']['global']['warp_valid_pixels']}.",
        "MAE is RGB code-value absolute error normalized by 255; alpha is ignored and the input is not linearized.",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-a", required=True, help="Previous SOURCE A raw tightly packed BGRA8 file")
    parser.add_argument("--source-b", required=True, help="Current SOURCE B raw tightly packed BGRA8 file")
    parser.add_argument("--motion", required=True, help="Full-resolution little-endian RG16Float raw file, R=dx and G=dy")
    parser.add_argument("--width", required=True, type=int)
    parser.add_argument("--height", required=True, type=int)
    parser.add_argument("--source-a-sequence", required=True, type=int)
    parser.add_argument("--source-b-sequence", required=True, type=int)
    parser.add_argument("--condition", default="", help="STILL, CAMERA, CHARACTER, COMBINED, or other short label")
    parser.add_argument("--max-component-px", type=float, default=256.0,
                        help="Absolute per-component bound for warp eligibility (default: 256 px)")
    parser.add_argument("--roi", action="append", default=[], metavar="NAME=x,y,width,height",
                        help="Optional pixel-space ROI; repeat for character/world/HUD regions")
    parser.add_argument("--json-out", help="Write machine-readable JSON to this path; use - for stdout")
    parser.add_argument("--markdown-out", help="Write Markdown summary to this path; use - for stdout")
    args = parser.parse_args()

    if args.width <= 0 or args.height <= 0 or args.width * args.height > 16_777_216:
        parser.error("dimensions must be positive and no more than 16,777,216 pixels")
    if args.source_a_sequence < 0 or args.source_b_sequence != args.source_a_sequence + 1:
        parser.error("SOURCE B sequence must equal SOURCE A sequence + 1")
    if not math.isfinite(args.max_component_px) or args.max_component_px <= 0:
        parser.error("--max-component-px must be finite and positive")

    regions = [Region("global", None)]
    names = {"global"}
    for roi_text in args.roi:
        roi = parse_roi(roi_text, args.width, args.height)
        if roi.name in names:
            parser.error(f"duplicate ROI name: {roi.name}")
        names.add(roi.name)
        regions.append(roi)

    expected_frame_bytes = args.width * args.height * 4
    expected_motion_bytes = args.width * args.height * 4
    source_a = Path(args.source_a).read_bytes()
    source_b = Path(args.source_b).read_bytes()
    motion = Path(args.motion).read_bytes()
    if len(source_a) != expected_frame_bytes:
        parser.error(f"SOURCE A size {len(source_a)} != expected {expected_frame_bytes} bytes for BGRA8 {args.width}x{args.height}")
    if len(source_b) != expected_frame_bytes:
        parser.error(f"SOURCE B size {len(source_b)} != expected {expected_frame_bytes} bytes for BGRA8 {args.width}x{args.height}")
    if len(motion) != expected_motion_bytes:
        parser.error(f"motion size {len(motion)} != expected {expected_motion_bytes} bytes for RG16Float {args.width}x{args.height}")

    width, height = args.width, args.height
    for y in range(height):
        for x in range(width):
            index = y * width + x
            dx, dy = struct.unpack_from("<ee", motion, index * 4)
            finite = math.isfinite(dx) and math.isfinite(dy)
            magnitude = math.hypot(dx, dy) if finite else math.nan
            bounded = finite and abs(dx) <= args.max_component_px and abs(dy) <= args.max_component_px
            active = [region for region in regions if region.contains(x, y)]
            for region in active:
                region.add_vector(dx, dy, magnitude, bounded)
            if not bounded:
                continue

            sample = bilinear_rgb(source_a, x + dx, y + dy, width, height)
            if sample is None:
                continue
            current_rgb = rgb_at(source_b, x, y, width)
            previous_same = rgb_at(source_a, x, y, width)
            zero_error = sum(abs(current_rgb[c] - previous_same[c]) for c in range(3))
            warp_error = sum(abs(current_rgb[c] - sample[c]) for c in range(3))
            for region in active:
                region.add_warp(zero_error, warp_error)

    report = {
        "schema": "fgmetal-r107-title-motion-analysis-v1",
        "pair": {
            "source_a_sequence": args.source_a_sequence,
            "source_b_sequence": args.source_b_sequence,
            "condition": args.condition,
            "source_a_path": os.path.abspath(args.source_a),
            "source_b_path": os.path.abspath(args.source_b),
            "motion_path": os.path.abspath(args.motion),
            "width": width,
            "height": height,
            "pixel_count": width * height,
        },
        "formats": {
            "source": "tightly-packed BGRA8 raw; ignored alpha",
            "motion": "tightly-packed little-endian RG16Float raw; 4 bytes/pixel",
        },
        "motion": {
            "convention": "current SOURCE B pixel p maps to previous SOURCE A sample p+(R,G); R=dx pixels, G=dy pixels; positive Y follows increasing image rows",
            "component_bound_px": args.max_component_px,
            "warp": "bilinear SOURCE A sampling at current pixel coordinate plus motion; zero and derived MAE share the bounded-vector interior mask",
            "mae": "mean absolute RGB code-value error divided by 255; alpha ignored; no transfer-function linearization",
        },
        "regions": {region.name: summarize_region(region, args.max_component_px) for region in regions},
    }

    json_text = json.dumps(report, indent=2, sort_keys=True) + "\n"
    markdown_text = render_markdown(report)
    if args.json_out:
        write_output(args.json_out, json_text)
    if args.markdown_out:
        write_output(args.markdown_out, markdown_text)
    if not args.json_out and not args.markdown_out:
        sys.stdout.write(json_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
