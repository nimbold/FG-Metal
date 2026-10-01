#!/usr/bin/env python3
"""Regenerate the committed, entirely synthetic high-rate benchmark fixture."""

from __future__ import annotations

import json
from pathlib import Path

from scene import HEIGHT, WIDTH, render

ROOT = Path(__file__).resolve().parents[1] / "corpus" / "synthetic-motion"
FRAMES = ROOT / "frames"
MASKS = ROOT / "masks"


def write_ppm(path: Path, rgb: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"P6\n{WIDTH} {HEIGHT}\n255\n".encode("ascii") + rgb)


def write_pgm(path: Path, mask: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(f"P5\n{WIDTH} {HEIGHT}\n255\n".encode("ascii") + mask)


def main() -> None:
    frames = []
    for index in range(11):
        rendered = render(float(index))
        name = f"f{index:03d}.ppm"
        write_ppm(FRAMES / name, rendered["rgb"])
        masks = {}
        for label, data in rendered["masks"].items():
            filename = f"f{index:03d}-{label}.pgm"
            write_pgm(MASKS / filename, data)
            masks[label] = f"masks/{filename}"
        frames.append({
            "index": index,
            "timestamp_ns": index * 16_666_667,
            "path": f"frames/{name}",
            "masks": masks,
        })

    manifest = {
        "schema_version": 1,
        "sequence_id": "synthetic-motion-v1",
        "title": "Synthetic motion, thin geometry, occlusion, and HUD fixture",
        "source": "Project-authored analytic scene; no third-party footage or assets.",
        "license": "Apache-2.0 (project-authored code and generated fixture)",
        "width": WIDTH,
        "height": HEIGHT,
        "high_rate_fps": 60.0,
        "low_rate_stride_frames": 2,
        "source_indices": [0, 2, 4, 6, 8, 10],
        "analytic_provider": "../../synthetic/scene.py",
        "analytic_provider_function": "render",
        "frame_time_coordinate": "high-rate frame units; endpoints are two units apart",
        "mask_labels": ["hud", "text", "scene", "occlusion"],
        "category_catalog": "../catalog.json",
        "fixture_coverage": [
            "slow_camera_pan", "third_person_character_movement", "foliage",
            "thin_geometry", "fences", "particles", "transparency",
            "reflections_specular_highlights", "weapon_sights", "crosshairs",
            "subtitles", "minimaps", "health_bars", "rapidly_changing_hud_counters"
        ],
        "frames": frames,
    }
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(frames)} synthetic frames and masks to {ROOT}")


if __name__ == "__main__":
    main()
