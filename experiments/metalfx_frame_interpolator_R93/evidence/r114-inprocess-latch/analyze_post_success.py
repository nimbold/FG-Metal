#!/usr/bin/env python3
"""R114 post-success visual diagnostics for the first displayed Elden Ring M.

Reads the GPU-diagnostic raws for the validated pair (A, M, B, motion field),
writes A/M/B PNGs plus a three-panel composite, and computes the quantitative
comparison used for the visual-quality classification. Pure stdlib only.
"""

from __future__ import annotations

import json
import math
import struct
import sys
import zlib
from pathlib import Path

W, H = 1470, 956
EVIDENCE = Path("/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/run-06-r114-inprocess-latch-title-display")
OUT = Path("/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence/r114-inprocess-latch/post-success")
PAIR_A, PAIR_B = 2749, 2750


def write_png(path: Path, width: int, height: int, rgb: bytes) -> None:
    raw = bytearray()
    stride = width * 3
    for y in range(height):
        raw.append(0)
        raw += rgb[y * stride:(y + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data +
                struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 6))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def bgra_to_rgb(data: bytes) -> bytearray:
    out = bytearray(W * H * 3)
    # BGRA -> RGB in place pattern; alpha dropped.
    out[0::3] = data[2::4]
    out[1::3] = data[1::4]
    out[2::3] = data[0::4]
    return out


def rgba16f_to_rgb(data: bytes) -> bytearray:
    vals = struct.unpack("<%de" % (len(data) // 2), data)
    out = bytearray(W * H * 3)
    j = 0
    for i in range(0, len(vals), 4):
        for c in range(3):
            v = vals[i + c]
            if not (v == v):      # NaN
                v = 0.0
            if v < 0.0:
                v = 0.0
            elif v > 1.0:
                v = 1.0
            out[j] = int(v * 255.0 + 0.5)
            j += 1
    return out


def motion_stats(path: Path) -> dict:
    data = path.read_bytes()
    vals = struct.unpack("<%de" % (len(data) // 2), data)
    n = len(vals) // 2
    step = 4
    total = active = 0
    mx = 0.0
    acc = 0.0
    for i in range(0, n, step):
        m = math.hypot(vals[2 * i], vals[2 * i + 1])
        total += 1
        acc += m
        if m > 0.5:
            active += 1
        if m > mx:
            mx = m
    return {
        "samples": total,
        "activeOver0_5pxFraction": active / total if total else 0.0,
        "maxMagnitude": mx,
        "meanMagnitude": acc / total if total else 0.0,
    }


def region_mean_abs_diff(a: bytearray, b: bytearray, x0: int, y0: int, x1: int, y1: int) -> float:
    total = 0.0
    count = 0
    for y in range(y0, y1, 2):
        base = y * W * 3
        for x in range(x0, x1, 2):
            i = base + x * 3
            total += abs(a[i] - b[i]) + abs(a[i + 1] - b[i + 1]) + abs(a[i + 2] - b[i + 2])
            count += 3
    return (total / count / 255.0) if count else 0.0


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    a_raw = (EVIDENCE / f"source-sequence-{PAIR_A}.raw").read_bytes()
    b_raw = (EVIDENCE / f"source-sequence-{PAIR_B}.raw").read_bytes()
    m_raw = (EVIDENCE / f"title-M-for-{PAIR_A}-{PAIR_B}.rgba16f").read_bytes()
    motion = EVIDENCE / f"motion-pair-{PAIR_A}-{PAIR_B}.rg16f"

    a = bgra_to_rgb(a_raw)
    b = bgra_to_rgb(b_raw)
    m = rgba16f_to_rgb(m_raw)

    write_png(OUT / f"A-{PAIR_A}.png", W, H, bytes(a))
    write_png(OUT / f"B-{PAIR_B}.png", W, H, bytes(b))
    write_png(OUT / f"M-{PAIR_A}-{PAIR_B}.png", W, H, bytes(m))

    # Three-panel composite, half resolution, thin white separators.
    half_w, half_h = W // 2, H // 2
    panel_gap = 6
    comp_w = half_w * 3 + panel_gap * 2
    comp = bytearray(comp_w * half_h * 3)
    for idx, src in enumerate((a, m, b)):
        x_off = idx * (half_w + panel_gap)
        for y in range(half_h):
            sy = y * 2
            row = sy * W * 3
            dst = (y * comp_w + x_off) * 3
            for x in range(half_w):
                si = row + x * 2 * 3
                comp[dst] = src[si]
                comp[dst + 1] = src[si + 1]
                comp[dst + 2] = src[si + 2]
                dst += 3
    for y in range(half_h):
        for g in range(panel_gap):
            for idx in (0, 1):
                x = half_w + g + idx * (half_w + panel_gap)
                i = (y * comp_w + x) * 3
                comp[i] = comp[i + 1] = comp[i + 2] = 255
    write_png(OUT / f"composite-A-M-B-{PAIR_A}-{PAIR_B}.png", comp_w, half_h, bytes(comp))

    stats = motion_stats(motion)
    endpoint_ab = region_mean_abs_diff(a, b, 0, 0, W, H)
    m_vs_a = region_mean_abs_diff(m, a, 0, 0, W, H)
    m_vs_b = region_mean_abs_diff(m, b, 0, 0, W, H)

    # Zero-motion reference: plain 50/50 blend of the endpoints.
    zero = bytearray(len(a))
    for i in range(0, len(a), 3):
        zero[i] = (a[i] + b[i]) // 2
        zero[i + 1] = (a[i + 1] + b[i + 1]) // 2
        zero[i + 2] = (a[i + 2] + b[i + 2]) // 2
    zero_vs_m = region_mean_abs_diff(zero, m, 0, 0, W, H)
    write_png(OUT / "zero-motion-blend.png", W, H, bytes(zero))

    # Player ROI (centre-bottom) and world ROI (upper third) on the derived warp.
    roi_player = (W // 2 - 220, int(H * 0.45), W // 2 + 220, int(H * 0.92))
    roi_world = (int(W * 0.1), int(H * 0.08), int(W * 0.9), int(H * 0.40))
    result = {
        "pair": f"{PAIR_A}->{PAIR_B}",
        "dimensions": f"{W}x{H}",
        "motion": stats,
        "endpointMeanAbsDiffAtoB": endpoint_ab,
        "MvsA": m_vs_a,
        "MvsB": m_vs_b,
        "zeroMotionBlendVsM": zero_vs_m,
        "playerROI": {
            "rect": roi_player,
            "MvsA": region_mean_abs_diff(m, a, *roi_player),
            "MvsB": region_mean_abs_diff(m, b, *roi_player),
            "zeroVsM": region_mean_abs_diff(zero, m, *roi_player),
        },
        "worldROI": {
            "rect": roi_world,
            "MvsA": region_mean_abs_diff(m, a, *roi_world),
            "MvsB": region_mean_abs_diff(m, b, *roi_world),
            "zeroVsM": region_mean_abs_diff(zero, m, *roi_world),
        },
        "artifacts": {
            "A": str(OUT / f"A-{PAIR_A}.png"),
            "M": str(OUT / f"M-{PAIR_A}-{PAIR_B}.png"),
            "B": str(OUT / f"B-{PAIR_B}.png"),
            "composite": str(OUT / f"composite-A-M-B-{PAIR_A}-{PAIR_B}.png"),
            "zeroMotionBlend": str(OUT / "zero-motion-blend.png"),
        },
    }
    (OUT / "r114-post-success-visual.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
