#!/usr/bin/env python3
"""Run a rights-safe frame-generation quality and runtime benchmark.

Only the Python standard library is required. The Metal runner is an isolated
offline measurement adapter; this program never captures a desktop or game.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ANALYZER_VERSION = "1.0"
METRIC_CONTRACT_VERSION = "2"
REGIONS = ("all", "hud", "text", "scene", "occlusion")
MASK_LABELS = REGIONS[1:]
MASK_MIN_PIXELS_DEFAULT = 8
SSIM_RADIUS = 3
EDGE_THRESHOLD = 48.0
EDGE_CHAMFER_CAP_PX = 6.0
MS_SSIM_WEIGHTS = (0.4, 0.3, 0.2, 0.1)


def _netpbm(path: Path, magic: bytes, channels: int) -> tuple[int, int, bytes]:
    data = path.read_bytes()
    cursor = 0

    def token() -> bytes:
        nonlocal cursor
        while cursor < len(data):
            if data[cursor] == 35:
                while cursor < len(data) and data[cursor] not in (10, 13):
                    cursor += 1
            elif data[cursor] in b" \t\r\n":
                cursor += 1
            else:
                break
        start = cursor
        while cursor < len(data) and data[cursor] not in b" \t\r\n#":
            cursor += 1
        if start == cursor:
            raise ValueError(f"invalid or truncated Netpbm header: {path}")
        return data[start:cursor]

    if token() != magic:
        raise ValueError(f"expected {magic.decode('ascii')} in {path}")
    try:
        width, height, maximum = int(token()), int(token()), int(token())
    except ValueError as exc:
        raise ValueError(f"invalid Netpbm dimensions: {path}") from exc
    if width <= 0 or height <= 0 or maximum != 255:
        raise ValueError(f"unsupported Netpbm dimensions or max value: {path}")
    if cursor >= len(data) or data[cursor] not in b" \t\r\n":
        raise ValueError(f"missing Netpbm pixel separator: {path}")
    cursor += 2 if data[cursor:cursor + 2] == b"\r\n" else 1
    pixels = data[cursor:]
    if len(pixels) != width * height * channels:
        raise ValueError(f"pixel count does not match image dimensions: {path}")
    return width, height, pixels


def read_image(path: Path) -> tuple[int, int, bytes]:
    return _netpbm(path, b"P6", 3)


def read_mask(path: Path, width: int, height: int, label: str,
              minimum_pixels: int = MASK_MIN_PIXELS_DEFAULT) -> bytes:
    mw, mh, raw = _netpbm(path, b"P5", 1)
    if (mw, mh) != (width, height):
        raise ValueError(f"{label} mask dimensions {mw}x{mh} do not match {width}x{height}: {path}")
    values = set(raw)
    if not values <= {0, 255}:
        raise ValueError(f"{label} mask must contain only 0 and 255 values: {path}")
    active = raw.count(255)
    if active < minimum_pixels:
        raise ValueError(
            f"{label} mask has only {active} active pixels (minimum {minimum_pixels}): {path}")
    return raw


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def validate_corpus(path: Path) -> dict[str, Any]:
    """Read and validate every corpus frame and mask before invoking a backend."""
    manifest_path = path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("corpus manifest must be a schema_version 1 object")
    for key in ("sequence_id", "license", "width", "height", "high_rate_fps",
                "low_rate_stride_frames", "frames"):
        if key not in manifest:
            raise ValueError(f"corpus manifest is missing {key}")
    width = _positive_int(manifest["width"], "width")
    height = _positive_int(manifest["height"], "height")
    fps = manifest["high_rate_fps"]
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("high_rate_fps must be a finite positive number")
    stride = _positive_int(manifest["low_rate_stride_frames"], "low_rate_stride_frames")
    frames_raw = manifest["frames"]
    if not isinstance(frames_raw, list) or len(frames_raw) < stride + 1:
        raise ValueError("corpus must contain at least one complete source interval")
    if (len(frames_raw) - 1) % stride != 0:
        raise ValueError("corpus must end on a selected low-rate source frame")
    root = manifest_path.parent
    frames: list[dict[str, Any]] = []
    index_seen: set[int] = set()
    previous_index = previous_time = None
    min_pixels = manifest.get("mask_minimum_pixels", {})
    if not isinstance(min_pixels, dict):
        raise ValueError("mask_minimum_pixels must be an object when supplied")
    for label, value in min_pixels.items():
        if label not in MASK_LABELS:
            raise ValueError(f"unknown mask_minimum_pixels label: {label}")
        _positive_int(value, f"mask_minimum_pixels.{label}")
    corpus_hash = hashlib.sha256()
    corpus_hash.update(manifest_path.read_bytes())
    normalized: list[dict[str, Any]] = []
    for position, entry in enumerate(frames_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"frame record {position} must be an object")
        index = entry.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError(f"frames[{position}].index must be a non-negative integer")
        timestamp = entry.get("timestamp_ns")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError(f"frame {index} timestamp_ns must be a non-negative integer")
        if index in index_seen:
            raise ValueError(f"duplicate frame index {index}")
        if previous_index is not None and index != previous_index + 1:
            raise ValueError("high-rate frame indices must be contiguous and increasing")
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError("frame timestamps must be strictly increasing")
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            raise ValueError(f"frame {index} has no path")
        image_path = (root / rel).resolve()
        iw, ih, rgb = read_image(image_path)
        if (iw, ih) != (width, height):
            raise ValueError(f"frame {index} dimensions do not match the manifest")
        masks_raw = entry.get("masks")
        if not isinstance(masks_raw, dict) or set(masks_raw) != set(MASK_LABELS):
            raise ValueError(f"frame {index} must provide exactly these masks: {', '.join(MASK_LABELS)}")
        expected_mask_counts = entry.get("mask_pixel_counts")
        if expected_mask_counts is not None:
            if not isinstance(expected_mask_counts, dict) or set(expected_mask_counts) != set(MASK_LABELS):
                raise ValueError(f"frame {index} mask_pixel_counts must declare every required mask")
            for label, expected_count in expected_mask_counts.items():
                if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
                    raise ValueError(f"frame {index} mask_pixel_counts.{label} must be a non-negative integer")
        masks: dict[str, bytes] = {}
        mask_counts: dict[str, int] = {}
        for label in MASK_LABELS:
            mask_rel = masks_raw[label]
            if not isinstance(mask_rel, str) or not mask_rel:
                raise ValueError(f"frame {index} has invalid {label} mask path")
            mask_path = (root / mask_rel).resolve()
            data = read_mask(mask_path, width, height, label,
                             int(min_pixels.get(label, MASK_MIN_PIXELS_DEFAULT)))
            masks[label] = data
            mask_counts[label] = data.count(255)
            if expected_mask_counts is not None and expected_mask_counts[label] != mask_counts[label]:
                raise ValueError(
                    f"frame {index} {label} mask has {mask_counts[label]} active pixels; "
                    f"manifest declares {expected_mask_counts[label]}"
                )
            corpus_hash.update(mask_rel.encode("utf-8") + b"\0")
            corpus_hash.update(mask_path.read_bytes())
        normalized_entry = {
            "index": index, "timestamp_ns": timestamp, "path": rel,
            "rgb": rgb, "masks": masks, "mask_counts": mask_counts,
            "segment_id": entry.get("segment_id", "segment-0"),
            "boundary_before": entry.get("boundary_before"),
            "interpolable": entry.get("interpolable", True),
        }
        if not isinstance(normalized_entry["segment_id"], str) or not normalized_entry["segment_id"]:
            raise ValueError(f"frame {index} segment_id must be a non-empty string")
        if normalized_entry["boundary_before"] not in (None, "scene_cut", "loading_transition"):
            raise ValueError(f"frame {index} boundary_before must be scene_cut or loading_transition")
        if not isinstance(normalized_entry["interpolable"], bool):
            raise ValueError(f"frame {index} interpolable must be a boolean")
        normalized.append(normalized_entry)
        index_seen.add(index)
        corpus_hash.update(rel.encode("utf-8") + b"\0")
        corpus_hash.update(image_path.read_bytes())
        previous_index, previous_time = index, timestamp
    expected_sources = [frame["index"] for frame in normalized[::stride]]
    source_indices = manifest.get("source_indices", expected_sources)
    if not isinstance(source_indices, list) or source_indices != expected_sources:
        raise ValueError("source_indices must equal the high-rate frames sampled at low_rate_stride_frames")
    frame_by_index = {frame["index"]: frame for frame in normalized}
    for left, right in zip(source_indices, source_indices[1:]):
        if right - left != stride:
            raise ValueError("source frame intervals must match low_rate_stride_frames")
        if not any(left < frame["index"] < right for frame in normalized):
            raise ValueError(f"source pair {left},{right} has no ground-truth interior frame")
    provider = _load_provider(root, manifest)
    provider_path = None
    if manifest.get("analytic_provider"):
        provider_path = (root / manifest["analytic_provider"]).resolve()
        corpus_hash.update(str(manifest["analytic_provider"]).encode("utf-8") + b"\0")
        corpus_hash.update(provider_path.read_bytes())
    return {
        "path": manifest_path, "root": root, "manifest": manifest, "width": width, "height": height,
        "fps": float(fps), "stride": stride, "frames": normalized, "by_index": frame_by_index,
        "source_indices": source_indices, "provider": provider, "provider_path": provider_path,
        "content_sha256": corpus_hash.hexdigest(),
    }


def _load_provider(root: Path, manifest: dict) -> Any:
    relative = manifest.get("analytic_provider")
    if not relative:
        return None
    if not isinstance(relative, str):
        raise ValueError("analytic_provider must be a relative path")
    path = (root / relative).resolve()
    if not path.is_file():
        raise ValueError(f"analytic ground-truth provider not found: {path}")
    # Corpus providers are executable project-authored code. Loading from source
    # avoids leaving bytecode artifacts in a clean checkout.
    namespace: dict[str, Any] = {"__name__": "framegen_analytic_ground_truth"}
    exec(compile(path.read_text(encoding="utf-8"), str(path), "exec"), namespace)
    function = namespace.get(manifest.get("analytic_provider_function", "render"))
    if not callable(function):
        raise ValueError("analytic ground-truth provider function is missing")
    return function


def _write_ppm(path: Path, width: int, height: int, rgb: bytes) -> None:
    path.write_bytes(f"P6\n{width} {height}\n255\n".encode("ascii") + rgb)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * p / 100.0
    low, high = math.floor(position), math.ceil(position)
    return (ordered[low] * (high - position) + ordered[high] * (position - low)
            if low != high else ordered[low])


def distribution(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "mean": None, "p50": None, "p95": None, "p99": None, "max": None}
    if not all(math.isfinite(value) for value in values):
        raise ValueError("metric calculation produced a non-finite value")
    return {
        "count": len(values), "mean": statistics.fmean(values), "min": min(values),
        "p50": percentile(values, 50), "p95": percentile(values, 95),
        "p99": percentile(values, 99), "max": max(values),
    }


def _luminance(rgb: bytes, pixel: int) -> float:
    offset = pixel * 3
    return 0.2126 * rgb[offset] + 0.7152 * rgb[offset + 1] + 0.0722 * rgb[offset + 2]


def _integral(values: list[float], width: int, height: int) -> list[float]:
    stride = width + 1
    result = [0.0] * (stride * (height + 1))
    for y in range(height):
        row_sum = 0.0
        src = y * width
        dst = (y + 1) * stride
        above = y * stride
        for x in range(width):
            row_sum += values[src + x]
            result[dst + x + 1] = result[above + x + 1] + row_sum
    return result


def _rect_sum(integral: list[float], width: int, x0: int, y0: int, x1: int, y1: int) -> float:
    stride = width + 1
    return (integral[y1 * stride + x1] - integral[y0 * stride + x1]
            - integral[y1 * stride + x0] + integral[y0 * stride + x0])


def _ssim_map_mean(reference_luma: list[float], candidate_luma: list[float],
                   mask: list[float], width: int, height: int) -> float:
    count = width * height
    weighted = [[0.0] * count for _ in range(5)]
    for i, weight in enumerate(mask):
        if weight <= 0:
            continue
        x, y = reference_luma[i], candidate_luma[i]
        weighted[0][i] = weight
        weighted[1][i] = weight * x
        weighted[2][i] = weight * y
        weighted[3][i] = weight * x * x
        weighted[4][i] = weight * y * y
    # Cross products use a sixth integral image.
    cross = [mask[i] * reference_luma[i] * candidate_luma[i] for i in range(count)]
    integrals = [_integral(item, width, height) for item in weighted]
    cross_integral = _integral(cross, width, height)
    c1, c2 = (0.01 * 255) ** 2, (0.03 * 255) ** 2
    scores, score_weights = [], []
    for index, center_weight in enumerate(mask):
        if center_weight <= 0:
            continue
        x, y = index % width, index // width
        x0, x1 = max(0, x - SSIM_RADIUS), min(width, x + SSIM_RADIUS + 1)
        y0, y1 = max(0, y - SSIM_RADIUS), min(height, y + SSIM_RADIUS + 1)
        n = _rect_sum(integrals[0], width, x0, y0, x1, y1)
        if n <= 0:
            continue
        mx = _rect_sum(integrals[1], width, x0, y0, x1, y1) / n
        my = _rect_sum(integrals[2], width, x0, y0, x1, y1) / n
        vx = max(0.0, _rect_sum(integrals[3], width, x0, y0, x1, y1) / n - mx * mx)
        vy = max(0.0, _rect_sum(integrals[4], width, x0, y0, x1, y1) / n - my * my)
        cov = _rect_sum(cross_integral, width, x0, y0, x1, y1) / n - mx * my
        denominator = (mx * mx + my * my + c1) * (vx + vy + c2)
        score = ((2 * mx * my + c1) * (2 * cov + c2) / denominator
                 if denominator else 1.0)
        scores.append(max(-1.0, min(1.0, score)))
        score_weights.append(center_weight)
    if not scores:
        raise ValueError("SSIM region mask has no active centers")
    return sum(a * b for a, b in zip(scores, score_weights)) / sum(score_weights)


def _downsample(values: list[float], width: int, height: int) -> tuple[list[float], int, int]:
    nw, nh = max(1, (width + 1) // 2), max(1, (height + 1) // 2)
    result = [0.0] * (nw * nh)
    for y in range(nh):
        for x in range(nw):
            points = [values[sy * width + sx]
                      for sy in range(2 * y, min(height, 2 * y + 2))
                      for sx in range(2 * x, min(width, 2 * x + 2))]
            result[y * nw + x] = sum(points) / len(points)
    return result, nw, nh


def _lab(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    def linear(channel: int) -> float:
        value = channel / 255.0
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    r, g, b = (linear(v) for v in rgb)
    x = (0.4124564 * r + 0.3575761 * g + 0.1804375 * b) / 0.95047
    y = (0.2126729 * r + 0.7151522 * g + 0.0721750 * b)
    z = (0.0193339 * r + 0.1191920 * g + 0.9503041 * b) / 1.08883
    delta = 6 / 29
    def f(value: float) -> float:
        return value ** (1 / 3) if value > delta ** 3 else value / (3 * delta * delta) + 4 / 29
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _edge_map(rgb: bytes, width: int, height: int) -> list[float]:
    gray = [_luminance(rgb, i) for i in range(width * height)]
    edges = [0.0] * (width * height)
    for y in range(1, height - 1):
        for x in range(1, width - 1):
            i = y * width + x
            gx = (-gray[i - width - 1] + gray[i - width + 1] - 2 * gray[i - 1]
                  + 2 * gray[i + 1] - gray[i + width - 1] + gray[i + width + 1]) / 4
            gy = (-gray[i - width - 1] - 2 * gray[i - width] - gray[i - width + 1]
                  + gray[i + width - 1] + 2 * gray[i + width] + gray[i + width + 1]) / 4
            edges[i] = min(255.0, math.hypot(gx, gy))
    return edges


def _edge_distance(edge: list[bool], width: int, height: int) -> list[float]:
    """Two-pass 3x3 chamfer distance transform with diagonal cost sqrt(2)."""
    inf = EDGE_CHAMFER_CAP_PX + 2
    dist = [0.0 if value else inf for value in edge]
    diagonal = math.sqrt(2.0)
    for y in range(height):
        for x in range(width):
            i = y * width + x
            best = dist[i]
            if x: best = min(best, dist[i - 1] + 1.0)
            if y: best = min(best, dist[i - width] + 1.0)
            if x and y: best = min(best, dist[i - width - 1] + diagonal)
            if x + 1 < width and y: best = min(best, dist[i - width + 1] + diagonal)
            dist[i] = best
    for y in range(height - 1, -1, -1):
        for x in range(width - 1, -1, -1):
            i = y * width + x
            best = dist[i]
            if x + 1 < width: best = min(best, dist[i + 1] + 1.0)
            if y + 1 < height: best = min(best, dist[i + width] + 1.0)
            if x + 1 < width and y + 1 < height: best = min(best, dist[i + width + 1] + diagonal)
            if x and y + 1 < height: best = min(best, dist[i + width - 1] + diagonal)
            dist[i] = best
    return dist


def edge_similarity(reference_edges: list[float], candidate_edges: list[float],
                    mask: bytes, width: int, height: int) -> dict[str, float]:
    ref = [reference_edges[i] >= EDGE_THRESHOLD and bool(mask[i]) for i in range(width * height)]
    cand = [candidate_edges[i] >= EDGE_THRESHOLD and bool(mask[i]) for i in range(width * height)]
    ref_ids = [i for i, value in enumerate(ref) if value]
    cand_ids = [i for i, value in enumerate(cand) if value]
    ref_to_cand = _edge_distance(cand, width, height)
    cand_to_ref = _edge_distance(ref, width, height)
    if not ref_ids and not cand_ids:
        chamfer = 0.0
        precision = recall = 1.0
    elif not ref_ids or not cand_ids:
        chamfer = EDGE_CHAMFER_CAP_PX
        precision = 1.0 if not cand_ids else 0.0
        recall = 1.0 if not ref_ids else 0.0
    else:
        mean_candidate = statistics.fmean(min(EDGE_CHAMFER_CAP_PX, cand_to_ref[i]) for i in cand_ids)
        mean_reference = statistics.fmean(min(EDGE_CHAMFER_CAP_PX, ref_to_cand[i]) for i in ref_ids)
        chamfer = (mean_candidate + mean_reference) / 2
        precision = sum(cand_to_ref[i] <= 1.0 for i in cand_ids) / len(cand_ids)
        recall = sum(ref_to_cand[i] <= 1.0 for i in ref_ids) / len(ref_ids)
    return {
        "symmetric_chamfer_px": chamfer,
        "symmetric_chamfer_norm": chamfer / EDGE_CHAMFER_CAP_PX,
        "precision_at_1px": precision,
        "recall_at_1px": recall,
        "reference_edge_pixels": float(len(ref_ids)),
        "candidate_edge_pixels": float(len(cand_ids)),
    }


def evaluate_frame(reference: bytes, candidate: bytes, mask: bytes,
                   width: int, height: int, ref_edges: list[float] | None = None,
                   cand_edges: list[float] | None = None) -> dict[str, Any]:
    """Compute masked pixel, SSIM, multiscale perceptual, and edge metrics."""
    selected = [i for i, enabled in enumerate(mask) if enabled]
    if not selected:
        raise ValueError("evaluation mask has no active pixels")
    abs_errors: list[float] = []
    delta_es: list[float] = []
    squared_sum = 0
    ref_luma = [_luminance(reference, i) for i in range(width * height)]
    cand_luma = [_luminance(candidate, i) for i in range(width * height)]
    for pixel in selected:
        off = pixel * 3
        a = tuple(reference[off:off + 3])
        b = tuple(candidate[off:off + 3])
        differences = [abs(a[c] - b[c]) for c in range(3)]
        abs_errors.append(sum(differences) / (3 * 255.0))
        squared_sum += sum(d * d for d in differences)
        lab_a, lab_b = _lab(a), _lab(b)
        delta_es.append(math.sqrt(sum((lab_a[c] - lab_b[c]) ** 2 for c in range(3))))
    mse = squared_sum / (len(selected) * 3)
    # Exact-match PSNR is mathematically infinite. A documented 120 dB cap
    # keeps JSON standards-compliant while preserving the perfect-match flag.
    psnr = 120.0 if mse == 0 else 10 * math.log10(255.0 * 255.0 / mse)
    base_mask = [1.0 if value else 0.0 for value in mask]
    ref_pyr, cand_pyr, masks_pyr = [], [], []
    rw, rh = width, height
    r, c, m = ref_luma, cand_luma, base_mask
    for _ in range(len(MS_SSIM_WEIGHTS)):
        ref_pyr.append((r, rw, rh))
        cand_pyr.append((c, rw, rh))
        masks_pyr.append(m)
        if rw == 1 and rh == 1:
            break
        r, rw2, rh2 = _downsample(r, rw, rh)
        c, _, _ = _downsample(c, rw, rh)
        m, _, _ = _downsample(m, rw, rh)
        rw, rh = rw2, rh2
    level_scores = [
        _ssim_map_mean(ref_pyr[i][0], cand_pyr[i][0], masks_pyr[i],
                       ref_pyr[i][1], ref_pyr[i][2])
        for i in range(len(ref_pyr))
    ]
    ssim_score = max(0.0, min(1.0, level_scores[0]))
    weights = MS_SSIM_WEIGHTS[:len(level_scores)]
    weight_sum = sum(weights)
    weights = tuple(value / weight_sum for value in weights)
    ms_ssim = math.exp(sum(weight * math.log(max(1e-12, max(0.0, min(1.0, score))))
                           for weight, score in zip(weights, level_scores)))
    mean_delta_e = statistics.fmean(delta_es)
    color_similarity = math.exp(-mean_delta_e / 25.0)
    perceptual_similarity = (ms_ssim ** 0.75) * (color_similarity ** 0.25)
    if ref_edges is None:
        ref_edges = _edge_map(reference, width, height)
    if cand_edges is None:
        cand_edges = _edge_map(candidate, width, height)
    edges = edge_similarity(ref_edges, cand_edges, mask, width, height)
    return {
        "psnr_db": psnr,
        "psnr_perfect_match": mse == 0,
        "ssim_7x7_masked_luminance": ssim_score,
        "perceptual_similarity": perceptual_similarity,
        "perceptual_similarity_definition": (
            "project-authored score: weighted geometric mean of masked 7x7 luminance SSIM "
            "over average-pooled scales 1,2,4,8 (weights 0.4,0.3,0.2,0.1), raised to 0.75, "
            "times exp(-mean CIE76 DeltaE/25) raised to 0.25; higher is better; not LPIPS "
            "or canonical MS-SSIM"
        ),
        "cielab_delta_e76_mean": mean_delta_e,
        "pixel_abs_error_norm_mean": statistics.fmean(abs_errors),
        "pixel_abs_error_norm_p95": percentile(abs_errors, 95),
        "frame_max_pixel_error_norm": max(abs_errors),
        "pixels_over_1pct_error_fraction": sum(value >= 0.01 for value in abs_errors) / len(abs_errors),
        "pixels_over_1pct_error_count": sum(value >= 0.01 for value in abs_errors),
        "pixels_over_5pct_error_fraction": sum(value >= 0.05 for value in abs_errors) / len(abs_errors),
        "pixels_over_5pct_error_count": sum(value >= 0.05 for value in abs_errors),
        "active_pixel_count": len(selected),
        "edge_location": edges,
    }


def _union_mask(*masks: bytes) -> bytes:
    return bytes(255 if any(mask[i] for mask in masks) else 0 for i in range(len(masks[0])))


def _temporal_pixel_errors(before: bytes, after: bytes, gt_before: bytes, gt_after: bytes,
                           mask: bytes, dt_frames: float) -> list[float]:
    values = []
    for pixel, enabled in enumerate(mask):
        if not enabled:
            continue
        off = pixel * 3
        difference = sum(abs((after[off + c] - before[off + c]) -
                             (gt_after[off + c] - gt_before[off + c])) for c in range(3))
        values.append(difference / (3 * 255.0 * dt_frames))
    return values


def _acceleration_pixel_errors(previous: list[float], middle: list[float], following: list[float],
                               gt_previous: list[float], gt_middle: list[float],
                               gt_following: list[float], mask: bytes,
                               dt_left: float, dt_right: float) -> list[float]:
    divisor = (dt_left + dt_right) / 2.0
    values = []
    for pixel, enabled in enumerate(mask):
        if not enabled:
            continue
        pred = ((following[pixel] - middle[pixel]) / dt_right
                - (middle[pixel] - previous[pixel]) / dt_left) / divisor
        truth = ((gt_following[pixel] - gt_middle[pixel]) / dt_right
                 - (gt_middle[pixel] - gt_previous[pixel]) / dt_left) / divisor
        values.append(abs(pred - truth) / 255.0)
    return values


def temporal_metrics(timeline: list[dict[str, Any]], width: int, height: int,
                     high_rate_fps: float) -> dict[str, Any]:
    """Compare reconstructed and reference derivatives on the complete timeline."""
    if len(timeline) < 2:
        raise ValueError("temporal metrics need at least two timeline samples")
    unit_ns = 1e9 / high_rate_fps
    edge_reconstructed = [_edge_map(item["rgb"], width, height) for item in timeline]
    edge_reference = [_edge_map(item["reference_rgb"], width, height) for item in timeline]
    residual: dict[str, dict[str, list[float]]] = {
        region: {"mean": [], "p95": [], "max": []} for region in REGIONS
    }
    residual_rows: dict[str, list[dict[str, Any]]] = {region: [] for region in REGIONS}
    flicker: dict[str, dict[str, list[float]]] = {
        region: {"mean": [], "p95": [], "max": []} for region in REGIONS
    }
    edge_flicker: dict[str, dict[str, list[float]]] = {
        region: {"mean": [], "p95": [], "max": []} for region in REGIONS
    }
    flicker_rows: dict[str, list[dict[str, Any]]] = {region: [] for region in REGIONS}
    for i in range(1, len(timeline)):
        left, right = timeline[i - 1], timeline[i]
        if left.get("continuity_group", 0) != right.get("continuity_group", 0):
            continue
        dt = (right["time_ns"] - left["time_ns"]) / unit_ns
        if dt <= 0:
            raise ValueError("timeline timestamps must be strictly increasing")
        for region in REGIONS:
            mask = (bytes([255]) * (width * height) if region == "all"
                    else _union_mask(left["masks"][region], right["masks"][region]))
            values = _temporal_pixel_errors(left["rgb"], right["rgb"],
                                            left["reference_rgb"], right["reference_rgb"], mask, dt)
            stats = distribution(values)
            residual[region]["mean"].append(stats["mean"] or 0.0)
            residual[region]["p95"].append(stats["p95"] or 0.0)
            residual[region]["max"].append(stats["max"] or 0.0)
            residual_rows[region].append({
                "timestamps_ns": [left["time_ns"], right["time_ns"]],
                "continuity_group": left.get("continuity_group", 0),
                "mean": stats["mean"], "p95": stats["p95"], "max": stats["max"],
            })
    for i in range(1, len(timeline) - 1):
        before, middle, after = timeline[i - 1], timeline[i], timeline[i + 1]
        if not (before.get("continuity_group", 0) == middle.get("continuity_group", 0)
                == after.get("continuity_group", 0)):
            continue
        dt_left = (middle["time_ns"] - before["time_ns"]) / unit_ns
        dt_right = (after["time_ns"] - middle["time_ns"]) / unit_ns
        if dt_left <= 0 or dt_right <= 0:
            raise ValueError("timeline timestamps must be strictly increasing")
        for region in REGIONS:
            mask = (bytes([255]) * (width * height) if region == "all"
                    else _union_mask(before["masks"][region], middle["masks"][region],
                                     after["masks"][region]))
            edge_values = _acceleration_pixel_errors(
                edge_reconstructed[i - 1], edge_reconstructed[i], edge_reconstructed[i + 1],
                edge_reference[i - 1], edge_reference[i], edge_reference[i + 1], mask,
                dt_left, dt_right,
            )
            # Color flicker uses per-pixel RGB acceleration, including exact
            # source frames that surround generated frames.
            color_values = []
            for pixel, enabled in enumerate(mask):
                if not enabled:
                    continue
                off = pixel * 3
                total = 0.0
                for channel in range(3):
                    pred = (((after["rgb"][off + channel] - middle["rgb"][off + channel]) / dt_right)
                            - ((middle["rgb"][off + channel] - before["rgb"][off + channel]) / dt_left)) / ((dt_left + dt_right) / 2)
                    truth = (((after["reference_rgb"][off + channel] - middle["reference_rgb"][off + channel]) / dt_right)
                             - ((middle["reference_rgb"][off + channel] - before["reference_rgb"][off + channel]) / dt_left)) / ((dt_left + dt_right) / 2)
                    total += abs(pred - truth)
                color_values.append(total / (3 * 255.0))
            color_stats, edge_stats = distribution(color_values), distribution(edge_values)
            flicker[region]["mean"].append(color_stats["mean"] or 0.0)
            flicker[region]["p95"].append(color_stats["p95"] or 0.0)
            flicker[region]["max"].append(color_stats["max"] or 0.0)
            edge_flicker[region]["mean"].append(edge_stats["mean"] or 0.0)
            edge_flicker[region]["p95"].append(edge_stats["p95"] or 0.0)
            edge_flicker[region]["max"].append(edge_stats["max"] or 0.0)
            flicker_rows[region].append({
                "timestamps_ns": [before["time_ns"], middle["time_ns"], after["time_ns"]],
                "continuity_group": middle.get("continuity_group", 0),
                "mean": color_stats["mean"], "p95": color_stats["p95"], "max": color_stats["max"],
                "edge_mean": edge_stats["mean"], "edge_p95": edge_stats["p95"],
                "edge_max": edge_stats["max"],
            })
    result = {}
    for region in REGIONS:
        result[region] = {
            "frame_to_frame_residual": {
                "per_transition_mean": distribution(residual[region]["mean"]),
                "per_transition_p95": distribution(residual[region]["p95"]),
                "worst_pixel_per_transition": distribution(residual[region]["max"]),
                "definition": "absolute RGB first-difference residual against ground truth, normalized by actual elapsed high-rate frame units",
                "transitions": residual_rows[region],
            },
            "temporal_consistency_score": max(0.0, 1.0 - (statistics.fmean(residual[region]["mean"])
                                                              if residual[region]["mean"] else 0.0)),
            "high_frequency_temporal_flicker": {
                "per_window_mean": distribution(flicker[region]["mean"]),
                "per_window_p95": distribution(flicker[region]["p95"]),
                "worst_pixel_per_window": distribution(flicker[region]["max"]),
                "definition": "absolute RGB second-derivative residual against ground truth, with actual timestamp spacing",
                "windows": flicker_rows[region],
            },
            "edge_flicker": {
                "per_window_mean": distribution(edge_flicker[region]["mean"]),
                "per_window_p95": distribution(edge_flicker[region]["p95"]),
                "worst_pixel_per_window": distribution(edge_flicker[region]["max"]),
                "definition": "Sobel magnitude second-derivative residual against ground truth",
            },
        }
    return result


def _target_plan(corpus: dict[str, Any], requested_t: float | None) -> list[dict[str, Any]]:
    if requested_t is not None and (not math.isfinite(requested_t) or not 0 < requested_t < 1):
        raise ValueError("--t must be finite and strictly between 0 and 1")
    manifest, frames = corpus["manifest"], corpus["by_index"]
    provider = corpus["provider"]
    plan = []
    source = corpus["source_indices"]
    continuity_group = 0
    for left_index, right_index in zip(source, source[1:]):
        left, right = frames[left_index], frames[right_index]
        interior = [frame for frame in corpus["frames"]
                    if left["timestamp_ns"] < frame["timestamp_ns"] < right["timestamp_ns"]]
        interval_frames = [frame for frame in corpus["frames"]
                           if left_index < frame["index"] <= right_index]
        crosses_boundary = (
            left["segment_id"] != right["segment_id"]
            or any(frame["boundary_before"] is not None for frame in interval_frames)
            or any(not frame["interpolable"] for frame in [left, *interval_frames])
        )
        if crosses_boundary:
            continuity_group += 1
            continue
        if requested_t is None:
            targets = [((frame["timestamp_ns"] - left["timestamp_ns"]) /
                        (right["timestamp_ns"] - left["timestamp_ns"]), frame)
                       for frame in interior]
        elif provider:
            targets = [(requested_t, None)]
        else:
            targets = []
            for frame in interior:
                t = ((frame["timestamp_ns"] - left["timestamp_ns"]) /
                     (right["timestamp_ns"] - left["timestamp_ns"]))
                if abs(t - requested_t) <= 1e-9:
                    targets.append((t, frame))
            if not targets:
                raise ValueError(
                    f"requested t={requested_t} has no exact ground-truth timestamp in eligible pair {left_index},{right_index}"
                )
        for t, target_frame in targets:
            timestamp = (target_frame["timestamp_ns"] if target_frame else
                         left["timestamp_ns"] + (right["timestamp_ns"] - left["timestamp_ns"]) * t)
            coordinate = left_index + (right_index - left_index) * t
            plan.append({
                "source_pair": [left_index, right_index],
                "t": float(t), "time_ns": float(timestamp),
                "target_index": target_frame["index"] if target_frame else None,
                "coordinate": float(coordinate), "continuity_group": continuity_group,
            })
    if not plan:
        raise ValueError("no exact target-time ground truth was selected")
    return plan


def _read_backend_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    raise ValueError("backend did not write a JSON object to stdout")


def _positive_samples(run: dict[str, Any], key: str, iterations: int,
                      required: bool = True) -> tuple[list[float], int]:
    raw = run.get(key)
    if raw is None:
        if required:
            raise ValueError(f"backend JSON is missing {key}")
        return [], iterations
    if not isinstance(raw, list) or len(raw) != iterations:
        raise ValueError(f"backend {key} must contain exactly {iterations} samples")
    known = []
    missing = 0
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"backend {key} contains an invalid sample")
        if value == 0:
            missing += 1
        else:
            known.append(float(value))
    return known, missing


def _performance_summary(gpu: list[float], gpu_missing: int, cpu: list[float],
                         latency: list[float], gpu_current: list[int], gpu_peak: list[int],
                         resident: list[int], source_fps: list[float],
                         source_with_generation_fps: list[float],
                         throughput_methods: list[str], deadline_ms: float,
                         expected_samples: int, expected_target_runs: int) -> dict[str, Any]:
    misses = sum(value / 1e6 > deadline_ms for value in latency)
    source_only_mean = statistics.fmean(source_fps) if source_fps else None
    source_generated_mean = statistics.fmean(source_with_generation_fps) if source_with_generation_fps else None
    impact = (100.0 * (source_only_mean - source_generated_mean) / source_only_mean
              if source_only_mean and source_generated_mean is not None else None)
    return {
        "measurement_samples": len(latency),
        "expected_measurement_samples": expected_samples,
        "interpolation_gpu_time_ms": {
            **distribution([v / 1e6 for v in gpu]), "missing_samples": gpu_missing,
            "availability": "available" if gpu else "unavailable",
        },
        "cpu_submit_overhead_ms": distribution([v / 1e6 for v in cpu]),
        "completion_latency_ms": distribution([v / 1e6 for v in latency]),
        "gpu_allocated_bytes_current_max": max(gpu_current) if gpu_current else None,
        "gpu_allocated_bytes_peak_max": max(gpu_peak) if gpu_peak else None,
        "runner_peak_resident_bytes_max": max(resident) if resident else None,
        "gpu_memory_sample_count": len(gpu_current),
        "gpu_memory_missing_target_runs": max(0, expected_target_runs - len(gpu_current)),
        "runner_resident_memory_sample_count": len(resident),
        "runner_resident_memory_missing_target_runs": max(0, expected_target_runs - len(resident)),
        "deadline_ms": deadline_ms,
        "generated_frame_deadline_miss_count": misses,
        "generated_frame_deadline_miss_rate": misses / len(latency) if latency else None,
        "dropped_generated_frames_estimated": misses,
        "dropped_generated_frames_method": "count of measured serial generation completions above deadline; not observed presentation drops",
        "source_only_input_throughput_fps": distribution(source_fps),
        "source_with_generation_input_throughput_fps": distribution(source_with_generation_fps),
        "source_frame_fps_impact_percent": impact,
        "source_frame_fps_impact_availability": "measured" if impact is not None else
        "unavailable: source-only and with-generation throughput samples are both required",
        "source_frame_fps_impact_method": (
            "100 * (source_only_input_fps - source_with_generation_input_fps) / source_only_input_fps; "
            + (throughput_methods[0] if throughput_methods else
               "unavailable because the runner did not report throughput during generation")
        ),
        "source_frame_fps_impact_scope": (
            "offline input-throughput proxy only; excludes renderer work, game FPS, and presentation"
        ),
    }


def _run(args: argparse.Namespace) -> int:
    corpus = validate_corpus(Path(args.corpus))
    plan = _target_plan(corpus, args.t)
    width, height = corpus["width"], corpus["height"]
    manifest, frames = corpus["manifest"], corpus["by_index"]
    deadline_ms = args.deadline_ms if args.deadline_ms is not None else 1000.0 / corpus["fps"]
    if not math.isfinite(deadline_ms) or deadline_ms <= 0:
        raise ValueError("deadline must be finite and positive")
    timeline_by_time: dict[float, dict[str, Any]] = {}
    quality_samples: dict[str, dict[str, list[float]]] = {
        region: {name: [] for name in (
            "psnr_db", "ssim", "perceptual", "delta_e", "pixel_mean", "pixel_max",
            "pixel_over_1pct", "pixel_over_5pct", "pixel_over_5pct_count",
            "pixel_over_1pct_count",
            "edge_chamfer", "edge_precision", "edge_recall")}
        for region in REGIONS
    }
    quality_frames = []
    gpu_ms_ns, gpu_missing = [], 0
    cpu_ns, latency_ns = [], []
    gpu_current, gpu_peak, resident = [], [], []
    source_fps, source_with_generation_fps = [], []
    throughput_methods: list[str] = []
    throughput_method_presence: bool | None = None
    backend_identity = None
    target_metadata = []
    expected_samples = len(plan) * args.iterations
    with tempfile.TemporaryDirectory(prefix="framegen-bench-") as temp_dir:
        temp = Path(temp_dir)
        for ordinal, target in enumerate(plan):
            left_index, right_index = target["source_pair"]
            left, right = frames[left_index], frames[right_index]
            target_index = target["target_index"]
            if target_index is None:
                rendered = corpus["provider"](target["coordinate"])
                if not isinstance(rendered, dict) or "rgb" not in rendered or "masks" not in rendered:
                    raise ValueError("analytic ground-truth provider must return rgb and masks")
                reference = bytes(rendered["rgb"])
                if len(reference) != width * height * 3:
                    raise ValueError("analytic provider returned incorrectly sized RGB data")
                masks = {}
                for label in MASK_LABELS:
                    if label not in rendered["masks"]:
                        raise ValueError(f"analytic target has no {label} mask")
                    value = bytes(rendered["masks"][label])
                    if len(value) != width * height or set(value) - {0, 255}:
                        raise ValueError(f"analytic {label} mask has invalid dimensions or values")
                    if value.count(255) < int(manifest.get("mask_minimum_pixels", {}).get(
                            label, MASK_MIN_PIXELS_DEFAULT)):
                        raise ValueError(f"analytic {label} mask is underfilled")
                    masks[label] = value
            else:
                target_frame = frames[target_index]
                reference, masks = target_frame["rgb"], target_frame["masks"]
            previous_path, current_path = temp / f"left-{ordinal}.ppm", temp / f"right-{ordinal}.ppm"
            output_path = temp / f"generated-{ordinal}.ppm"
            _write_ppm(previous_path, width, height, left["rgb"])
            _write_ppm(current_path, width, height, right["rgb"])
            command = [
                str(Path(args.backend).resolve()), "--previous", str(previous_path),
                "--current", str(current_path), "--output", str(output_path),
                "--t", format(target["t"], ".17g"), "--warmup", str(args.warmup),
                "--iterations", str(args.iterations),
            ]
            completed = subprocess.run(command, check=True, capture_output=True, text=True)
            backend_run = _read_backend_json(completed.stdout)
            if backend_run.get("width") != width or backend_run.get("height") != height:
                raise ValueError("backend JSON dimensions do not match the corpus")
            reported_t = backend_run.get("interpolation_t", backend_run.get("t"))
            if reported_t is not None and abs(float(reported_t) - target["t"]) > 1e-5:
                raise ValueError("backend JSON reports a different interpolation timestamp")
            identity = {
                "id": backend_run.get("backend_id", "unspecified"),
                "kind": backend_run.get("backend_kind", "unspecified"),
                "metadata": backend_run.get("backend_metadata"),
            }
            if identity["metadata"] is not None and not isinstance(identity["metadata"], dict):
                raise ValueError("backend_metadata must be a JSON object")
            if backend_identity is None:
                backend_identity = identity
            elif identity != backend_identity:
                raise ValueError("backend identity changed between target timestamps")
            gw, gh, generated = read_image(output_path)
            if (gw, gh) != (width, height):
                raise ValueError("backend output image dimensions changed")
            gpu_values, missing = _positive_samples(backend_run, "gpu_execution_time_ns", args.iterations)
            cpu_values, _ = _positive_samples(backend_run, "cpu_submit_overhead_ns", args.iterations)
            latency_values, _ = _positive_samples(backend_run, "completion_latency_ns", args.iterations)
            gpu_ms_ns.extend(gpu_values)
            gpu_missing += missing
            cpu_ns.extend(cpu_values)
            latency_ns.extend(latency_values)
            for key, target_values in (
                ("gpu_allocated_bytes_current", gpu_current),
                ("gpu_allocated_bytes_peak", gpu_peak),
                ("peak_resident_bytes", resident),
            ):
                value = backend_run.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
                    target_values.append(int(value))
            for key, target_values in (
                ("source_only_input_fps", source_fps),
                ("source_with_generation_input_fps", source_with_generation_fps),
            ):
                value = backend_run.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
                    target_values.append(float(value))
            method = backend_run.get("source_with_generation_throughput_method")
            method_present = method is not None
            if throughput_method_presence is not None and method_present != throughput_method_presence:
                raise ValueError("backend throughput method availability changed between targets")
            throughput_method_presence = method_present
            if method is not None:
                if not isinstance(method, str) or not method:
                    raise ValueError("backend source_with_generation_throughput_method must be a non-empty string")
                if throughput_methods and method != throughput_methods[0]:
                    raise ValueError("backend throughput measurement method changed between targets")
                throughput_methods.append(method)
            if backend_run.get("source_with_generation_input_fps") is not None and method is None:
                raise ValueError("backend must explain source_with_generation_input_fps measurement method")
            time_ns = target["time_ns"]
            item = {
                "time_ns": time_ns, "rgb": generated, "reference_rgb": reference,
                "masks": masks, "kind": "generated", "source_pair": target["source_pair"],
                "t": target["t"], "target_index": target_index,
                "continuity_group": target["continuity_group"],
            }
            if time_ns in timeline_by_time:
                raise ValueError("two generated targets resolve to the same timestamp")
            timeline_by_time[time_ns] = item
            ref_edges, generated_edges = _edge_map(reference, width, height), _edge_map(generated, width, height)
            region_metrics = {}
            for region in REGIONS:
                mask = bytes([255]) * (width * height) if region == "all" else masks[region]
                values = evaluate_frame(reference, generated, mask, width, height, ref_edges, generated_edges)
                region_metrics[region] = values
                bucket = quality_samples[region]
                bucket["psnr_db"].append(values["psnr_db"])
                bucket["ssim"].append(values["ssim_7x7_masked_luminance"])
                bucket["perceptual"].append(values["perceptual_similarity"])
                bucket["delta_e"].append(values["cielab_delta_e76_mean"])
                bucket["pixel_mean"].append(values["pixel_abs_error_norm_mean"])
                bucket["pixel_max"].append(values["frame_max_pixel_error_norm"])
                bucket["pixel_over_1pct"].append(values["pixels_over_1pct_error_fraction"])
                bucket["pixel_over_1pct_count"].append(values["pixels_over_1pct_error_count"])
                bucket["pixel_over_5pct"].append(values["pixels_over_5pct_error_fraction"])
                bucket["pixel_over_5pct_count"].append(values["pixels_over_5pct_error_count"])
                bucket["edge_chamfer"].append(values["edge_location"]["symmetric_chamfer_norm"])
                bucket["edge_precision"].append(values["edge_location"]["precision_at_1px"])
                bucket["edge_recall"].append(values["edge_location"]["recall_at_1px"])
            quality_frames.append({
                "source_pair": target["source_pair"], "target_index": target_index,
                "t": target["t"], "timestamp_ns": time_ns, "regions": region_metrics,
            })
            target_metadata.append({
                "source_pair": target["source_pair"], "target_index": target_index,
                "t": target["t"], "timestamp_ns": time_ns,
            })
    for target in plan:
        for source_index in target["source_pair"]:
            frame = frames[source_index]
            stamp = float(frame["timestamp_ns"])
            existing = timeline_by_time.get(stamp)
            if existing and existing["continuity_group"] != target["continuity_group"]:
                raise ValueError("source frame belongs to incompatible continuity segments")
            if not existing:
                timeline_by_time[stamp] = {
                    "time_ns": stamp, "rgb": frame["rgb"],
                    "reference_rgb": frame["rgb"], "masks": frame["masks"],
                    "kind": "source", "source_index": source_index,
                    "continuity_group": target["continuity_group"],
                }
    timeline = sorted(timeline_by_time.values(), key=lambda item: item["time_ns"])
    if any(timeline[i]["time_ns"] >= timeline[i + 1]["time_ns"] for i in range(len(timeline) - 1)):
        raise ValueError("reconstructed timeline is not strictly increasing")
    temporal = temporal_metrics(timeline, width, height, corpus["fps"])
    quality = {}
    for region, values in quality_samples.items():
        quality[region] = {
            "sample_count": len(quality_frames),
            "psnr_db": distribution(values["psnr_db"]),
            "ssim_7x7_masked_luminance": distribution(values["ssim"]),
            "perceptual_similarity": distribution(values["perceptual"]),
            "cielab_delta_e76_mean": distribution(values["delta_e"]),
            "pixel_abs_error_norm_mean_per_target": distribution(values["pixel_mean"]),
            "frame_max_pixel_error_norm": distribution(values["pixel_max"]),
            "pixels_over_1pct_error_fraction_per_target": distribution(values["pixel_over_1pct"]),
            "pixels_over_1pct_error_count_per_target": distribution(values["pixel_over_1pct_count"]),
            "pixels_over_5pct_error_fraction_per_target": distribution(values["pixel_over_5pct"]),
            "pixels_over_5pct_error_count_per_target": distribution(values["pixel_over_5pct_count"]),
            "edge_location": {
                "symmetric_chamfer_norm": distribution(values["edge_chamfer"]),
                "precision_at_1px": distribution(values["edge_precision"]),
                "recall_at_1px": distribution(values["edge_recall"]),
                "worst_target_chamfer_norm": max(values["edge_chamfer"]) if values["edge_chamfer"] else None,
                "minimum_target_precision_at_1px": min(values["edge_precision"]) if values["edge_precision"] else None,
                "minimum_target_recall_at_1px": min(values["edge_recall"]) if values["edge_recall"] else None,
            },
            "temporal": temporal[region],
        }
    perf = _performance_summary(
        gpu_ms_ns, gpu_missing, cpu_ns, latency_ns, gpu_current, gpu_peak, resident,
        source_fps, source_with_generation_fps, throughput_methods,
        deadline_ms, expected_samples, len(plan),
    )
    now = datetime.now(timezone.utc)
    result = {
        "schema_version": 1,
        "analyzer_version": ANALYZER_VERSION,
        "metric_contract_version": METRIC_CONTRACT_VERSION,
        "run_id": args.run_id or now.strftime("%Y%m%dT%H%M%SZ"),
        "created_utc": now.isoformat(),
        "backend": {
            **(backend_identity or {"id": "unspecified", "kind": "unspecified"}),
            "binary": str(Path(args.backend).resolve()),
        },
        "host": {"platform": platform.platform(), "system": platform.system(),
                 "machine": platform.machine(), "python": platform.python_version()},
        "corpus": {
            "sequence_id": manifest["sequence_id"], "title": manifest.get("title"),
            "manifest": str(corpus["path"]), "manifest_sha256": hashlib.sha256(corpus["path"].read_bytes()).hexdigest(),
            "content_sha256": corpus["content_sha256"], "width": width, "height": height,
            "high_rate_fps": corpus["fps"], "low_rate_stride_frames": corpus["stride"],
            "source_indices": corpus["source_indices"], "license": manifest["license"],
            "source": manifest.get("source"), "fixture_coverage": manifest.get("fixture_coverage", []),
            "mask_coverage_pixels_by_frame": {
                str(frame["index"]): frame["mask_counts"] for frame in corpus["frames"]
            },
            "mask_overlap_policy": "Masks are evaluated independently and may overlap; HUD/text and scene/occlusion are not forced into a partition.",
        },
        "configuration": {
            "warmup_iterations": args.warmup, "measured_iterations_per_target": args.iterations,
            "deadline_ms": deadline_ms, "requested_t": args.t,
            "target_timeline": target_metadata,
            "runner_mode": "offline serial submit, completion wait, and readback",
            "metric_parameters": {
                "metric_contract_version": METRIC_CONTRACT_VERSION,
                "psnr_perfect_cap_db": 120.0, "ssim_window": "7x7 box, mask-weighted local neighborhoods",
                "perceptual_metric": "project-authored masked multiscale luminance SSIM plus CIE76 color score",
                "multiscale_weights": list(MS_SSIM_WEIGHTS), "perceptual_color_scale_delta_e": 25.0,
                "edge_threshold_luma_gradient": EDGE_THRESHOLD,
                "edge_chamfer_cap_px": EDGE_CHAMFER_CAP_PX,
                "temporal_time_unit": "actual elapsed high-rate frame periods from timestamps",
                "source_with_generation_throughput_method": (
                    throughput_methods[0] if throughput_methods else None
                ),
            },
        },
        "quality_by_region": quality,
        "quality_frames": quality_frames,
        "performance": perf,
        "compatibility": _compatibility_signature(result_host={
            "platform": platform.platform(), "system": platform.system(), "machine": platform.machine(),
        }, corpus={
            "content_sha256": corpus["content_sha256"], "sequence_id": manifest["sequence_id"],
            "width": width, "height": height, "high_rate_fps": corpus["fps"],
            "low_rate_stride_frames": corpus["stride"], "source_indices": corpus["source_indices"],
        }, configuration={
            "warmup_iterations": args.warmup, "measured_iterations_per_target": args.iterations,
            "deadline_ms": deadline_ms, "target_timeline": target_metadata,
            "metric_parameters": {
                "metric_contract_version": METRIC_CONTRACT_VERSION,
                "psnr_perfect_cap_db": 120.0, "ssim_window": "7x7 box, mask-weighted local neighborhoods",
                "perceptual_metric": "project-authored masked multiscale luminance SSIM plus CIE76 color score",
                "multiscale_weights": list(MS_SSIM_WEIGHTS), "perceptual_color_scale_delta_e": 25.0,
                "edge_threshold_luma_gradient": EDGE_THRESHOLD,
                "edge_chamfer_cap_px": EDGE_CHAMFER_CAP_PX,
                "temporal_time_unit": "actual elapsed high-rate frame periods from timestamps",
                "source_with_generation_throughput_method": (
                    throughput_methods[0] if throughput_methods else None
                ),
            },
        }),
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    summary = Path(args.summary).resolve() if args.summary else output.with_suffix(".md")
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text(render_summary(result), encoding="utf-8")
    print(f"JSON: {output}\nSummary: {summary}")
    return 0


def _compatibility_signature(result_host: dict, corpus: dict, configuration: dict) -> dict:
    return {"host": result_host, "corpus": corpus, "configuration": configuration}


def _fmt(value: Any, digits: int = 4) -> str:
    return "unavailable" if value is None else f"{value:.{digits}f}"


def render_summary(result: dict[str, Any]) -> str:
    perf = result["performance"]
    lines = [
        f"# Frame-generation benchmark: {result['run_id']}", "",
        f"- Backend: {result['backend']['kind']} ({result['backend']['id']})",
        f"- Corpus: {result['corpus']['sequence_id']} at {result['corpus']['width']}x{result['corpus']['height']}",
        f"- Interpolation targets: {len(result['configuration']['target_timeline'])}",
        f"- License: {result['corpus']['license']}",
        "", "## Temporal and edge quality", "",
        "| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel error max |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for region, quality in result["quality_by_region"].items():
        temporal = quality["temporal"]
        residual = temporal["frame_to_frame_residual"]
        flicker = temporal["high_frequency_temporal_flicker"]
        edge = quality["edge_location"]
        lines.append(
            f"| {region} | {_fmt(residual['per_transition_p95']['p95'])} / {_fmt(residual['worst_pixel_per_transition']['max'])} "
            f"| {_fmt(flicker['per_window_p95']['p95'])} / {_fmt(flicker['worst_pixel_per_window']['max'])} "
            f"| {_fmt(edge['symmetric_chamfer_norm']['mean'])} / {_fmt(edge['worst_target_chamfer_norm'])} "
            f"| {_fmt(edge['minimum_target_recall_at_1px'])} "
            f"| {_fmt(quality['frame_max_pixel_error_norm']['max'])} |"
        )
    lines += [
        "", "Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. "
        "HUD, text, scene, and occlusion masks are scored independently and may overlap.",
        "", "## Image quality diagnostics", "",
        "PSNR is diagnostic only. Exact-match PSNR is capped at 120 dB for finite JSON.",
        "The perceptual score is project-authored and combines multiscale masked luminance structure with CIE76 color similarity; it is not LPIPS.",
        "", "| Region | PSNR mean dB | SSIM mean | Perceptual score mean | CIE76 Delta E mean |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for region, quality in result["quality_by_region"].items():
        lines.append(
            f"| {region} | {_fmt(quality['psnr_db']['mean'], 3)} "
            f"| {_fmt(quality['ssim_7x7_masked_luminance']['mean'], 5)} "
            f"| {_fmt(quality['perceptual_similarity']['mean'], 5)} "
            f"| {_fmt(quality['cielab_delta_e76_mean']['mean'], 4)} |"
        )
    gpu = perf["interpolation_gpu_time_ms"]
    latency = perf["completion_latency_ms"]
    cpu = perf["cpu_submit_overhead_ms"]
    mem = perf["gpu_allocated_bytes_peak_max"]
    lines += [
        "", "## Runtime", "",
        f"- GPU time p50/p95/p99: {_fmt(gpu['p50'])} / {_fmt(gpu['p95'])} / {_fmt(gpu['p99'])} ms "
        f"(missing samples: {gpu['missing_samples']})",
        f"- CPU submit p50/p95/p99: {_fmt(cpu['p50'])} / {_fmt(cpu['p95'])} / {_fmt(cpu['p99'])} ms",
        f"- Completion latency p50/p95/p99: {_fmt(latency['p50'])} / {_fmt(latency['p95'])} / {_fmt(latency['p99'])} ms",
        f"- GPU allocated peak: {mem if mem is not None else 'unavailable'} bytes; host RSS peak: "
        f"{perf['runner_peak_resident_bytes_max'] if perf['runner_peak_resident_bytes_max'] is not None else 'unavailable'} bytes",
        f"- Deadline misses: {perf['generated_frame_deadline_miss_count']} / {perf['measurement_samples']} "
        f"at {perf['deadline_ms']:.4f} ms; estimated generated drops: {perf['dropped_generated_frames_estimated']}",
        f"- Source-frame FPS impact proxy: {_fmt(perf['source_frame_fps_impact_percent'], 2)}% "
        f"({perf['source_frame_fps_impact_availability']})",
        f"- Throughput method: {perf['source_frame_fps_impact_method']}",
        f"- Impact scope: {perf['source_frame_fps_impact_scope']}",
        "", "Percentiles describe serial offline runner samples. Deadline misses and dropped-frame counts are estimates, not observed presentation events.",
        ("Source throughput impact is unavailable because the runner did not provide both measurements."
         if perf["source_frame_fps_impact_percent"] is None
         else "Source throughput impact is an offline input-throughput proxy and excludes renderer work, game FPS, and presentation."),
        "",
    ]
    return "\n".join(lines)


QUALITY_RULES = [
    ("temporal", "frame_to_frame_residual.per_transition_p95.p95", "lower", 0.002),
    ("temporal", "frame_to_frame_residual.worst_pixel_per_transition.max", "lower", 0.01),
    ("temporal", "high_frequency_temporal_flicker.per_window_p95.p95", "lower", 0.002),
    ("temporal", "high_frequency_temporal_flicker.worst_pixel_per_window.max", "lower", 0.01),
    ("quality", "frame_max_pixel_error_norm.max", "lower", 0.005),
    ("quality", "pixels_over_5pct_error_fraction_per_target.max", "lower", 0.001),
    ("quality", "pixels_over_1pct_error_count_per_target.max", "lower", 0.0),
    ("quality", "pixels_over_5pct_error_count_per_target.max", "lower", 0.0),
    ("quality", "ssim_7x7_masked_luminance.mean", "higher", 0.005),
    ("quality", "ssim_7x7_masked_luminance.p50", "higher", 0.01),
    ("quality", "perceptual_similarity.mean", "higher", 0.005),
    ("quality", "perceptual_similarity.min", "higher", 0.01),
    ("quality", "cielab_delta_e76_mean.p95", "lower", 0.5),
    ("edge", "edge_location.symmetric_chamfer_norm.mean", "lower", 0.01),
    ("edge", "edge_location.worst_target_chamfer_norm", "lower", 0.03),
    ("edge", "edge_location.minimum_target_precision_at_1px", "higher", 0.01),
    ("edge", "edge_location.minimum_target_recall_at_1px", "higher", 0.01),
    ("temporal", "edge_flicker.per_window_p95.p95", "lower", 0.002),
    ("temporal", "edge_flicker.worst_pixel_per_window.max", "lower", 0.01),
]
RUNTIME_RULES = [
    ("completion_latency_ms", "p50", "lower", 0.1),
    ("completion_latency_ms", "p95", "lower", 0.1),
    ("completion_latency_ms", "p99", "lower", 0.1),
    ("interpolation_gpu_time_ms", "p50", "lower", 0.1),
    ("interpolation_gpu_time_ms", "p95", "lower", 0.1),
    ("interpolation_gpu_time_ms", "p99", "lower", 0.1),
    ("cpu_submit_overhead_ms", "p95", "lower", 0.1),
    ("cpu_submit_overhead_ms", "p99", "lower", 0.1),
]


def _nested(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"result is missing required metric field {path}")
        current = current[part]
    return current


def _validate_result(result: dict[str, Any], label: str) -> None:
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise ValueError(f"{label} is not a supported benchmark result")
    if result.get("metric_contract_version") != METRIC_CONTRACT_VERSION:
        raise ValueError(f"{label} uses an incompatible metric contract")
    if not isinstance(result.get("compatibility"), dict):
        raise ValueError(f"{label} is missing compatibility metadata")
    regions = result.get("quality_by_region")
    if not isinstance(regions, dict) or set(regions) != set(REGIONS):
        raise ValueError(f"{label} must contain every required quality region")
    for region in REGIONS:
        quality = regions[region]
        for group, path, _, _ in QUALITY_RULES:
            base = quality["temporal"] if group == "temporal" else quality
            value = _nested(base, path)
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{label} has unavailable or invalid {group} metric in region {region}: {path}")
        residual_rows = quality["temporal"]["frame_to_frame_residual"].get("transitions")
        flicker_rows = quality["temporal"]["high_frequency_temporal_flicker"].get("windows")
        if not isinstance(residual_rows, list) or not isinstance(flicker_rows, list):
            raise ValueError(f"{label} is missing per-transition or per-window temporal metrics for {region}")
        for row in residual_rows:
            for metric in ("mean", "p95", "max"):
                value = row.get(metric)
                if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{label} has an invalid temporal transition metric in {region}")
        for row in flicker_rows:
            for metric in ("mean", "p95", "max", "edge_mean", "edge_p95", "edge_max"):
                value = row.get(metric)
                if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{label} has an invalid temporal window metric in {region}")
    perf = result.get("performance")
    if not isinstance(perf, dict):
        raise ValueError(f"{label} is missing performance metrics")
    target_plan = result.get("configuration", {}).get("target_timeline")
    quality_frames = result.get("quality_frames")
    if not isinstance(target_plan, list) or not isinstance(quality_frames, list) or len(target_plan) != len(quality_frames):
        raise ValueError(f"{label} is missing per-target quality records")
    for frame, target in zip(quality_frames, target_plan):
        if (frame.get("source_pair"), frame.get("timestamp_ns"), frame.get("t")) != (
                target.get("source_pair"), target.get("timestamp_ns"), target.get("t")):
            raise ValueError(f"{label} per-target quality records do not match target timestamps")
        regions = frame.get("regions")
        if not isinstance(regions, dict) or set(regions) != set(REGIONS):
            raise ValueError(f"{label} target frame is missing a required quality region")
        for region in REGIONS:
            values = regions[region]
            for metric in ("frame_max_pixel_error_norm", "pixels_over_1pct_error_count",
                           "pixels_over_5pct_error_count"):
                value = values.get(metric)
                if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{label} target frame is missing {metric} for {region}")
            edge = values.get("edge_location")
            if not isinstance(edge, dict) or edge.get("symmetric_chamfer_norm") is None:
                raise ValueError(f"{label} target frame is missing edge-location metrics for {region}")
    for metric, statistic, _, _ in RUNTIME_RULES:
        value = _nested(perf, f"{metric}.{statistic}")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError(f"{label} has invalid runtime metric {metric}.{statistic}")


def _compare_values(base: float, candidate: float, direction: str, tolerance: float) -> bool:
    return candidate < base - tolerance if direction == "higher" else candidate > base + tolerance


def require_compatible(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    if baseline.get("compatibility") != candidate.get("compatibility"):
        raise ValueError(
            "runs are incompatible: corpus content, target timestamps, host, "
            "iteration/deadline configuration, or metric parameters differ"
        )


def _compare(args: argparse.Namespace) -> int:
    baseline_path, candidate_path = Path(args.baseline), Path(args.candidate)
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    _validate_result(baseline, "baseline")
    _validate_result(candidate, "candidate")
    require_compatible(baseline, candidate)
    regressions, unavailable = [], []
    for region in REGIONS:
        bq, cq = baseline["quality_by_region"][region], candidate["quality_by_region"][region]
        for group, path, direction, tolerance in QUALITY_RULES:
            base_obj = bq["temporal"] if group == "temporal" else bq
            cand_obj = cq["temporal"] if group == "temporal" else cq
            old, new = _nested(base_obj, path), _nested(cand_obj, path)
            if _compare_values(old, new, direction, tolerance):
                regressions.append({
                    "scope": group, "region": region, "metric": path,
                    "direction": direction, "baseline": old, "candidate": new,
                    "tolerance": tolerance,
                })
    # Compare every generated timestamp independently. A worse target frame
    # cannot hide behind another target's already larger maximum.
    for baseline_frame, candidate_frame in zip(baseline["quality_frames"], candidate["quality_frames"]):
        target_id = {
            "source_pair": baseline_frame["source_pair"],
            "timestamp_ns": baseline_frame["timestamp_ns"],
            "t": baseline_frame["t"],
        }
        for region in REGIONS:
            old_values = baseline_frame["regions"][region]
            new_values = candidate_frame["regions"][region]
            per_target_metrics = (
                ("frame_max_pixel_error_norm", "lower", 0.0),
                ("pixels_over_1pct_error_count", "lower", 0.0),
                ("pixels_over_5pct_error_count", "lower", 0.0),
                ("edge_location.symmetric_chamfer_norm", "lower", 0.0),
                ("edge_location.precision_at_1px", "higher", 0.0),
                ("edge_location.recall_at_1px", "higher", 0.0),
            )
            for metric, direction, tolerance in per_target_metrics:
                old, new = _nested(old_values, metric), _nested(new_values, metric)
                if _compare_values(old, new, direction, tolerance):
                    regressions.append({
                        "scope": "quality_target", "region": region, "target": target_id,
                        "metric": metric, "direction": direction, "baseline": old,
                        "candidate": new, "tolerance": tolerance,
                    })
    # Temporal tails are also matched by actual timestamp windows. This catches
    # a newly shimmering interval even if another interval was already worse.
    for region in REGIONS:
        base_temporal = baseline["quality_by_region"][region]["temporal"]
        cand_temporal = candidate["quality_by_region"][region]["temporal"]
        for collection, metric_names in (
            ("frame_to_frame_residual", ("mean", "p95", "max")),
            ("high_frequency_temporal_flicker", ("mean", "p95", "max")),
        ):
            row_key = "transitions" if collection == "frame_to_frame_residual" else "windows"
            base_rows = base_temporal[collection][row_key]
            cand_rows = cand_temporal[collection][row_key]
            def row_identity(row: dict[str, Any]) -> tuple[Any, ...]:
                return (tuple(row["timestamps_ns"]), row["continuity_group"])
            base_map = {row_identity(row): row for row in base_rows}
            cand_map = {row_identity(row): row for row in cand_rows}
            if set(base_map) != set(cand_map):
                raise ValueError(f"runs have different temporal windows for {region}.{collection}")
            for key, old_row in base_map.items():
                new_row = cand_map[key]
                fields = metric_names
                if collection == "high_frequency_temporal_flicker":
                    fields = (*metric_names, "edge_mean", "edge_p95", "edge_max")
                for metric in fields:
                    old, new = old_row[metric], new_row[metric]
                    if _compare_values(old, new, "lower", 0.0):
                        regressions.append({
                            "scope": "temporal_window", "region": region,
                            "timestamps_ns": list(key[0]), "metric": metric,
                            "baseline": old, "candidate": new, "tolerance": 0.0,
                        })
    br, cr = baseline["performance"], candidate["performance"]
    for metric, statistic, direction, tolerance in RUNTIME_RULES:
        old, new = _nested(br, f"{metric}.{statistic}"), _nested(cr, f"{metric}.{statistic}")
        if old is None and new is None:
            unavailable.append(f"{metric}.{statistic}")
        elif old is None or new is None:
            raise ValueError(f"runs have different availability for runtime metric {metric}.{statistic}")
        elif _compare_values(old, new, direction, tolerance):
            regressions.append({
                "scope": "runtime", "metric": f"{metric}.{statistic}",
                "direction": direction, "baseline": old, "candidate": new,
                "tolerance": tolerance,
            })
    for field in ("gpu_allocated_bytes_current_max", "gpu_allocated_bytes_peak_max",
                  "runner_peak_resident_bytes_max",
                  "generated_frame_deadline_miss_rate", "source_frame_fps_impact_percent"):
        old, new = br.get(field), cr.get(field)
        if old is None and new is None:
            unavailable.append(field)
        elif old is None or new is None:
            raise ValueError(f"runs have different availability for runtime metric {field}")
        elif field == "generated_frame_deadline_miss_rate":
            if _compare_values(old, new, "lower", 0.01):
                regressions.append({"scope": "runtime", "metric": field, "direction": "lower",
                                    "baseline": old, "candidate": new, "tolerance": 0.01})
        elif field == "source_frame_fps_impact_percent":
            if _compare_values(old, new, "lower", 1.0):
                regressions.append({"scope": "runtime", "metric": field, "direction": "lower",
                                    "baseline": old, "candidate": new, "tolerance": 1.0})
        elif _compare_values(old, new, "lower", 1024 * 1024):
            regressions.append({"scope": "runtime", "metric": field, "direction": "lower",
                                "baseline": old, "candidate": new, "tolerance": 1024 * 1024})
    result = {
        "schema_version": 1, "metric_contract_version": METRIC_CONTRACT_VERSION,
        "baseline": str(baseline_path.resolve()), "candidate": str(candidate_path.resolve()),
        "baseline_backend": baseline.get("backend", {}),
        "candidate_backend": candidate.get("backend", {}),
        "quality_regression_count": sum(item["scope"] != "runtime" for item in regressions),
        "runtime_regression_count": sum(item["scope"] == "runtime" for item in regressions),
        "regressions": regressions, "unavailable_runtime_comparisons": sorted(set(unavailable)),
        "status": "REGRESSION" if regressions else "NO_REGRESSION_DETECTED",
        "gate_policy": "all regions and thresholds are independent; gains in one metric or region cannot offset regressions in another",
    }
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    summary = Path(args.summary).resolve()
    summary.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# Benchmark comparison: {result['status']}", "",
             f"Baseline: {result['baseline']}", f"Candidate: {result['candidate']}", "",
             f"Baseline backend: {json.dumps(result['baseline_backend'], sort_keys=True)}",
             f"Candidate backend: {json.dumps(result['candidate_backend'], sort_keys=True)}", "",
             result["gate_policy"], ""]
    if regressions:
        lines += ["| Scope | Region | Metric | Baseline | Candidate |",
                  "| --- | --- | --- | ---: | ---: |"]
        for item in regressions:
            lines.append(f"| {item['scope']} | {item.get('region', '—')} | {item['metric']} "
                         f"| {item['baseline']:.6g} | {item['candidate']:.6g} |")
    else:
        lines.append("No thresholded regressions were detected.")
    if unavailable:
        lines += ["", "Unavailable runtime comparisons: " + ", ".join(sorted(set(unavailable)))]
    summary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"JSON: {output}\nSummary: {summary}\n{result['status']}")
    return 1 if regressions else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run", help="run corpus quality and performance evaluation")
    run.add_argument("--corpus", required=True, help="path to a schema_version 1 manifest")
    run.add_argument("--backend", required=True, help="framegen-benchmark-metal executable or compatible adapter")
    run.add_argument("--output", required=True, help="machine-readable JSON result")
    run.add_argument("--summary", help="Markdown summary (defaults to output path with .md suffix)")
    run.add_argument("--run-id")
    run.add_argument("--t", type=float, help="normalized interpolation timestamp; default evaluates every exact interior corpus timestamp")
    run.add_argument("--warmup", type=int, default=3)
    run.add_argument("--iterations", type=int, default=100)
    run.add_argument("--deadline-ms", type=float, help="default is one high-rate frame period")
    compare = subparsers.add_parser("compare", help="compare compatible quality and runtime runs")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--output", required=True)
    compare.add_argument("--summary", required=True)
    args = parser.parse_args()
    try:
        if args.command == "run":
            backend = Path(args.backend)
            if not backend.is_file() or not os.access(backend, os.X_OK):
                raise ValueError(f"backend executable is missing or not executable: {args.backend}")
            if args.warmup < 1 or args.iterations < 100:
                raise ValueError("warmup must be positive and measured iterations must be at least 100 for p99 reporting")
            return _run(args)
        return _compare(args)
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"framegen benchmark: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
