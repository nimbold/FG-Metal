#!/usr/bin/env python3
"""Generate deterministic divisible and edge-padded RIFE validation inputs."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path

from ppm_utils import write_ppm


WIDTH = 64
HEIGHT = 64


def clamp8(value: int) -> int:
    return max(0, min(255, value))


def pixel(x: int, y: int, frame: int) -> tuple[int, int, int]:
    """Build a textured background and two moving, high-contrast shapes."""
    checker = ((x // 8) ^ (y // 8)) & 1
    red = (25 + 3 * x + 2 * y + (x * y) % 23 + checker * 18) % 256
    green = (35 + 2 * x + 3 * y + (3 * x + y * y) % 29 + checker * 9) % 256
    blue = (55 + x + 4 * y + (x * x + 2 * y) % 31 + checker * 14) % 256

    # A moving outlined disk with a striped interior.
    disk_x = x - (23 + frame * 4)
    disk_y = y - (31 - frame * 2)
    radius2 = disk_x * disk_x + disk_y * disk_y
    if radius2 <= 11 * 11:
        if radius2 >= 8 * 8:
            red, green, blue = 248, 238, 72
        else:
            stripe = ((disk_x + 2 * disk_y) // 3) & 1
            red, green, blue = (224, 58 + stripe * 45, 40 + stripe * 24)

    # A thinner cyan bar moves the other way and crosses the disk near its edge.
    bar_x = 43 - frame * 3
    if bar_x <= x < bar_x + 5 and 8 <= y < 57:
        edge = x == bar_x or x == bar_x + 4
        red, green, blue = (18, 242, 238) if not edge else (235, 255, 255)

    # A small dark square creates a second motion direction and a sharp corner.
    square_x = 43 - frame * 2
    square_y = 16 + frame * 2
    if square_x <= x < square_x + 7 and square_y <= y < square_y + 7:
        red, green, blue = 22, 24, 34

    return clamp8(red), clamp8(green), clamp8(blue)


def frame_bytes(width: int, height: int, frame: int) -> bytes:
    return bytes(
        channel for y in range(height) for x in range(width)
        for channel in pixel(x, y, frame)
    )


def default_directory() -> Path:
    root = Path(__file__).resolve().parents[2]
    return root / "tests" / "fixtures" / "rife-v4.26"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=default_directory())
    args = parser.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    for width, height, names in (
        (WIDTH, HEIGHT, ("frame_a.ppm", "frame_b.ppm")),
        (65, 63, ("frame_a_edge.ppm", "frame_b_edge.ppm")),
    ):
        for frame, name in enumerate(names):
            data = frame_bytes(width, height, frame)
            path = args.out_dir / name
            write_ppm(path, width, height, data)
            print(f"wrote {path} sha256={hashlib.sha256(path.read_bytes()).hexdigest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
