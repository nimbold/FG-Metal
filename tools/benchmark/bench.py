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
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ANALYZER_VERSION = "1.4"
METRIC_CONTRACT_VERSION = "7"
REGIONS = ("all", "hud", "text", "scene", "occlusion")
MASK_LABELS = REGIONS[1:]
MANDATORY_STRICT_PIXEL_LABELS = ("hud", "text")
MAX_CORPUS_MASK_LABELS = 64
MASK_MIN_PIXELS_DEFAULT = 8
DEFAULT_MAX_ANALYSIS_MEMORY_MIB = 6144
MAX_MANIFEST_BYTES = 32 * 1024 * 1024
MAX_PROVIDER_SOURCE_BYTES = 4 * 1024 * 1024
NETPBM_HEADER_LIMIT_BYTES = 64 * 1024
BACKEND_JSON_LINE_LIMIT_BYTES = 1024 * 1024
MAX_BACKEND_SERVER_JOBS = 100_000
MAX_BACKEND_SESSION_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_RESULT_BYTES = 64 * 1024 * 1024
MAX_STRICT_PIXEL_COMPARISONS = 250_000
# The current pure-Python SSIM and edge routines create several pixel-sized
# object arrays at once. This conservative scratch allowance is part of the
# corpus preflight model, not a measured RSS guarantee.
ANALYSIS_SCRATCH_BYTES_PER_PIXEL = 1024
MAX_SEQUENCE_NUMBER = (1 << 64) - 1
MAX_TIMESTAMP_NS = (1 << 63) - 1
SSIM_RADIUS = 3
EDGE_THRESHOLD = 48.0
EDGE_CHAMFER_CAP_PX = 6.0
MS_SSIM_WEIGHTS = (0.4, 0.3, 0.2, 0.1)


def _parse_netpbm(data: bytes, path: Path, magic: bytes,
                  channels: int) -> tuple[int, int, bytes]:
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


def _netpbm(path: Path, magic: bytes, channels: int,
            maximum_bytes: int = MAX_MANIFEST_BYTES) -> tuple[int, int, bytes]:
    data = _read_bounded(path, maximum_bytes, "Netpbm file")
    return _parse_netpbm(data, path, magic, channels)


def read_image(path: Path, width: int | None = None,
               height: int | None = None) -> tuple[int, int, bytes]:
    maximum = (width * height * 3 + NETPBM_HEADER_LIMIT_BYTES
               if width is not None and height is not None else MAX_MANIFEST_BYTES)
    return _netpbm(path, b"P6", 3, maximum)


def read_mask(path: Path, width: int, height: int, label: str,
              minimum_pixels: int = MASK_MIN_PIXELS_DEFAULT) -> bytes:
    data = _read_bounded(
        path, width * height + NETPBM_HEADER_LIMIT_BYTES, f"{label} mask")
    return _validate_mask_data(data, path, width, height, label, minimum_pixels)


def _validate_mask_data(data: bytes, path: Path, width: int, height: int,
                        label: str, minimum_pixels: int) -> bytes:
    mw, mh, raw = _parse_netpbm(data, path, b"P5", 1)
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


def _reject_json_constant(value: str):
    raise ValueError(f"non-standard JSON numeric constant: {value}")


def _corpus_file(root: Path, relative: Any, label: str) -> Path:
    if not isinstance(relative, str) or not relative or Path(relative).is_absolute():
        raise ValueError(f"{label} must be a non-empty relative path")
    root = root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as error:
        raise ValueError(f"{label} must stay inside the corpus directory") from error
    if not path.is_file():
        raise ValueError(f"{label} does not name a corpus file: {relative}")
    return path


def _read_bounded(path: Path, maximum_bytes: int, label: str) -> bytes:
    with path.open("rb") as source:
        data = source.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise ValueError(f"{label} exceeds the configured size limit: {path}")
    return data


def _provider_bytes(value: Any, expected_size: int, label: str) -> bytes:
    """Validate provider buffer size before copying or invoking bytes(value)."""
    if isinstance(value, bytes):
        data = value
    elif isinstance(value, bytearray):
        if len(value) != expected_size:
            raise ValueError(f"analytic provider returned an incorrectly sized {label}")
        data = bytes(value)
    elif isinstance(value, memoryview):
        if value.nbytes != expected_size:
            raise ValueError(f"analytic provider returned an incorrectly sized {label}")
        data = value.tobytes()
    else:
        raise ValueError(f"analytic provider {label} must be a bytes-like object")
    if len(data) != expected_size:
        raise ValueError(f"analytic provider returned an incorrectly sized {label}")
    return data


def _write_json_bounded(path: Path, value: Any, label: str) -> None:
    """Stream JSON through a bounded temporary file and atomically replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    written = 0
    encoder = json.JSONEncoder(indent=2, allow_nan=False)
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp",
                                         delete=False) as output:
            temporary_path = Path(output.name)
            for chunk in encoder.iterencode(value):
                encoded = chunk.encode("utf-8")
                written += len(encoded)
                if written + 1 > MAX_RESULT_BYTES:
                    raise ValueError(
                        f"{label} exceeds the {MAX_RESULT_BYTES} byte result limit")
                output.write(encoded)
            output.write(b"\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _estimated_analysis_bytes(width: int, height: int, frame_count: int,
                              mask_count: int) -> int:
    pixels = width * height
    # Corpus RGB plus every declared mask, timeline RGB outputs, two retained
    # edge maps per timeline sample, temporal error distributions, and
    # single-target image-metric scratch. This intentionally errs high for
    # short clips while still accounting for arbitrary named ROI masks.
    retained_bytes_per_pixel = frame_count * (3 + mask_count + 3 + 64 + 96 + 7)
    return pixels * (retained_bytes_per_pixel + ANALYSIS_SCRATCH_BYTES_PER_PIXEL)


def validate_corpus(path: Path, max_analysis_memory_mib: int = DEFAULT_MAX_ANALYSIS_MEMORY_MIB) -> dict[str, Any]:
    """Read and validate every corpus frame and mask before invoking a backend."""
    manifest_path = path.resolve()
    manifest_bytes = _read_bounded(manifest_path, MAX_MANIFEST_BYTES, "corpus manifest")
    manifest = json.loads(manifest_bytes.decode("utf-8"), parse_constant=_reject_json_constant)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise ValueError("corpus manifest must be a schema_version 1 object")
    for key in ("sequence_id", "license", "width", "height", "high_rate_fps",
                "low_rate_stride_frames", "frames"):
        if key not in manifest:
            raise ValueError(f"corpus manifest is missing {key}")
    for key in ("sequence_id", "license"):
        if not isinstance(manifest[key], str) or not manifest[key].strip():
            raise ValueError(f"corpus manifest {key} must be a non-empty string")
    width = _positive_int(manifest["width"], "width")
    height = _positive_int(manifest["height"], "height")
    fps = manifest["high_rate_fps"]
    if isinstance(fps, bool) or not isinstance(fps, (int, float)) or not math.isfinite(fps) or fps <= 0:
        raise ValueError("high_rate_fps must be a finite positive number")
    stride = _positive_int(manifest["low_rate_stride_frames"], "low_rate_stride_frames")
    mask_labels = manifest.get("mask_labels", list(MASK_LABELS))
    if (not isinstance(mask_labels, list) or len(mask_labels) < len(MASK_LABELS)
            or len(mask_labels) > MAX_CORPUS_MASK_LABELS
            or any(not isinstance(label, str) or not label or len(label) > 64
                   or not label[0].islower()
                   or any(not (char.islower() or char.isdigit() or char in "_-") for char in label)
                   for label in mask_labels)
            or len(set(mask_labels)) != len(mask_labels)
            or not set(MASK_LABELS).issubset(mask_labels)):
        raise ValueError("mask_labels must contain the required regions and at most 64 unique valid labels")
    strict_pixel_labels = manifest.get(
        "strict_pixel_labels", list(MANDATORY_STRICT_PIXEL_LABELS))
    if (not isinstance(strict_pixel_labels, list)
            or any(not isinstance(label, str) or label not in mask_labels
                   for label in strict_pixel_labels)
            or len(set(strict_pixel_labels)) != len(strict_pixel_labels)):
        raise ValueError("strict_pixel_labels must be a unique subset of mask_labels")
    if not set(MANDATORY_STRICT_PIXEL_LABELS).issubset(strict_pixel_labels):
        raise ValueError("strict_pixel_labels must include the mandatory hud and text regions")
    frames_raw = manifest["frames"]
    if not isinstance(frames_raw, list) or len(frames_raw) < stride + 1:
        raise ValueError("corpus must contain at least one complete source interval")
    _positive_int(max_analysis_memory_mib, "max_analysis_memory_mib")
    estimated_analysis_bytes = _estimated_analysis_bytes(
        width, height, len(frames_raw), len(mask_labels))
    analysis_budget_bytes = max_analysis_memory_mib * 1024 * 1024
    if estimated_analysis_bytes > analysis_budget_bytes:
        estimated_mib = estimated_analysis_bytes / (1024 * 1024)
        raise ValueError(
            f"estimated analyzer working set is {estimated_mib:.0f} MiB, above the "
            f"{max_analysis_memory_mib} MiB limit; use a shorter/lower-resolution corpus "
            "or raise --max-analysis-memory-mib on a host with sufficient memory"
        )
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
        if label not in mask_labels:
            raise ValueError(f"unknown mask_minimum_pixels label: {label}")
        _positive_int(value, f"mask_minimum_pixels.{label}")
    corpus_hash = hashlib.sha256()
    corpus_hash.update(manifest_bytes)
    normalized: list[dict[str, Any]] = []
    for position, entry in enumerate(frames_raw):
        if not isinstance(entry, dict):
            raise ValueError(f"frame record {position} must be an object")
        index = entry.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0:
            raise ValueError(f"frames[{position}].index must be a non-negative integer")
        if index > MAX_SEQUENCE_NUMBER:
            raise ValueError(f"frames[{position}].index exceeds the backend sequence-number range")
        timestamp = entry.get("timestamp_ns")
        if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
            raise ValueError(f"frame {index} timestamp_ns must be a non-negative integer")
        if timestamp > MAX_TIMESTAMP_NS:
            raise ValueError(f"frame {index} timestamp_ns exceeds the signed 64-bit backend range")
        if index in index_seen:
            raise ValueError(f"duplicate frame index {index}")
        if previous_index is not None and index != previous_index + 1:
            raise ValueError("high-rate frame indices must be contiguous and increasing")
        if previous_time is not None and timestamp <= previous_time:
            raise ValueError("frame timestamps must be strictly increasing")
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            raise ValueError(f"frame {index} has no path")
        image_path = _corpus_file(root, rel, f"frame {index} path")
        image_limit = width * height * 3 + NETPBM_HEADER_LIMIT_BYTES
        image_bytes = _read_bounded(image_path, image_limit, f"frame {index} image")
        iw, ih, rgb = _parse_netpbm(image_bytes, image_path, b"P6", 3)
        if (iw, ih) != (width, height):
            raise ValueError(f"frame {index} dimensions do not match the manifest")
        masks_raw = entry.get("masks")
        if not isinstance(masks_raw, dict) or set(masks_raw) != set(mask_labels):
            raise ValueError(f"frame {index} must provide exactly these masks: {', '.join(mask_labels)}")
        expected_mask_counts = entry.get("mask_pixel_counts")
        if expected_mask_counts is not None:
            if not isinstance(expected_mask_counts, dict) or set(expected_mask_counts) != set(mask_labels):
                raise ValueError(f"frame {index} mask_pixel_counts must declare every required mask")
            for label, expected_count in expected_mask_counts.items():
                if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
                    raise ValueError(f"frame {index} mask_pixel_counts.{label} must be a non-negative integer")
        masks: dict[str, bytes] = {}
        mask_counts: dict[str, int] = {}
        for label in mask_labels:
            mask_rel = masks_raw[label]
            if not isinstance(mask_rel, str) or not mask_rel:
                raise ValueError(f"frame {index} has invalid {label} mask path")
            mask_path = _corpus_file(root, mask_rel, f"frame {index} {label} mask path")
            mask_bytes = _read_bounded(
                mask_path, width * height + NETPBM_HEADER_LIMIT_BYTES,
                f"frame {index} {label} mask")
            data = _validate_mask_data(mask_bytes, mask_path, width, height, label,
                                       min_pixels.get(label, MASK_MIN_PIXELS_DEFAULT))
            masks[label] = data
            mask_counts[label] = data.count(255)
            if expected_mask_counts is not None and expected_mask_counts[label] != mask_counts[label]:
                raise ValueError(
                    f"frame {index} {label} mask has {mask_counts[label]} active pixels; "
                    f"manifest declares {expected_mask_counts[label]}"
                )
            corpus_hash.update(mask_rel.encode("utf-8") + b"\0")
            corpus_hash.update(mask_bytes)
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
        corpus_hash.update(image_bytes)
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
    provider, provider_path, provider_bytes = _load_provider(root, manifest)
    if provider_bytes is not None:
        if any(frame["index"] > (1 << 53) for frame in normalized):
            raise ValueError("analytic provider frame coordinates must be exactly representable as float64")
        origin = normalized[0]["timestamp_ns"]
        for position, frame in enumerate(normalized):
            expected_time = origin + round(position * 1_000_000_000 / float(fps))
            if frame["timestamp_ns"] != expected_time:
                raise ValueError(
                    "analytic provider corpora must use the nominal uniform high-rate "
                    f"timestamp cadence; frame {frame['index']} has {frame['timestamp_ns']}, "
                    f"expected {expected_time}"
                )
            try:
                rendered = provider(float(frame["index"]))
            except Exception as error:
                raise ValueError(
                    f"analytic provider failed at stored high-rate frame {frame['index']}"
                ) from error
            if not isinstance(rendered, dict) or "rgb" not in rendered or "masks" not in rendered:
                raise ValueError(
                    f"analytic provider omitted RGB or masks at stored frame {frame['index']}"
                )
            rendered_rgb = _provider_bytes(
                rendered["rgb"], width * height * 3,
                f"RGB data at stored frame {frame['index']}")
            if rendered_rgb != frame["rgb"]:
                raise ValueError(
                    f"analytic provider RGB does not match stored frame {frame['index']}"
                )
            rendered_masks = rendered["masks"]
            if not isinstance(rendered_masks, dict) or set(rendered_masks) != set(mask_labels):
                raise ValueError(
                    f"analytic provider masks do not match stored frame {frame['index']} labels"
                )
            for label in mask_labels:
                provider_mask = _provider_bytes(
                    rendered_masks[label], width * height,
                    f"{label} mask at stored frame {frame['index']}")
                if provider_mask != frame["masks"][label]:
                    raise ValueError(
                        f"analytic provider {label} mask does not match stored frame {frame['index']}"
                    )
        corpus_hash.update(str(manifest["analytic_provider"]).encode("utf-8") + b"\0")
        corpus_hash.update(provider_bytes)
    return {
        "path": manifest_path, "root": root, "manifest": manifest, "width": width, "height": height,
        "fps": float(fps), "stride": stride, "frames": normalized, "by_index": frame_by_index,
        "source_indices": source_indices, "provider": provider, "provider_path": provider_path,
        "mask_labels": mask_labels, "strict_pixel_labels": strict_pixel_labels,
        "content_sha256": corpus_hash.hexdigest(),
        "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "estimated_analysis_bytes": estimated_analysis_bytes,
    }


def _load_provider(root: Path, manifest: dict) -> tuple[Any, Path | None, bytes | None]:
    if "analytic_provider" not in manifest:
        return None, None, None
    relative = manifest["analytic_provider"]
    if not isinstance(relative, str) or not relative:
        raise ValueError("analytic_provider must be a non-empty relative path")
    path = _corpus_file(root, relative, "analytic_provider")
    source = _read_bounded(path, MAX_PROVIDER_SOURCE_BYTES, "analytic provider source")
    # Corpus providers are executable project-authored code. Loading from source
    # avoids leaving bytecode artifacts in a clean checkout.
    namespace: dict[str, Any] = {"__name__": "framegen_analytic_ground_truth"}
    exec(compile(source, str(path), "exec"), namespace)
    function = namespace.get(manifest.get("analytic_provider_function", "render"))
    if not callable(function):
        raise ValueError("analytic ground-truth provider function is missing")
    return function, path, source


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
    # Keep PSNR finite and bounded for JSON; the perfect-match flag distinguishes
    # an exact match from a finite score that reaches the same cap.
    psnr = 120.0 if mse == 0 else min(120.0, 10 * math.log10(255.0 * 255.0 / mse))
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


def _critical_pixel_error_map(reference: bytes, candidate: bytes,
                              mask: bytes) -> list[list[int]]:
    """Retain per-channel pixel error for explicitly critical ROI masks."""
    errors = []
    for pixel, enabled in enumerate(mask):
        if not enabled:
            continue
        offset = pixel * 3
        error = [abs(reference[offset + channel] - candidate[offset + channel])
                 for channel in range(3)]
        if any(error):
            errors.append([pixel, *error])
    return errors


def _union_mask(*masks: bytes) -> bytes:
    return bytes(255 if any(mask[i] for mask in masks) else 0 for i in range(len(masks[0])))


def _temporal_pixel_errors(before: bytes, after: bytes, gt_before: bytes, gt_after: bytes,
                           mask: bytes, dt_frames: float,
                           retain_map: bool = False) -> tuple[list[float], list[list[int]]]:
    values = []
    error_map = []
    for pixel, enabled in enumerate(mask):
        if not enabled:
            continue
        off = pixel * 3
        difference = sum(abs((after[off + c] - before[off + c]) -
                             (gt_after[off + c] - gt_before[off + c])) for c in range(3))
        values.append(difference / (3 * 255.0 * dt_frames))
        if retain_map and difference:
            error_map.append([pixel, difference])
    return values, error_map


def _acceleration_pixel_errors(previous: list[float], middle: list[float], following: list[float],
                               gt_previous: list[float], gt_middle: list[float],
                               gt_following: list[float], mask: bytes,
                               dt_left: float, dt_right: float,
                               retain_map: bool = False) -> tuple[list[float], list[list[float]]]:
    divisor = (dt_left + dt_right) / 2.0
    values = []
    error_map = []
    for pixel, enabled in enumerate(mask):
        if not enabled:
            continue
        pred = ((following[pixel] - middle[pixel]) / dt_right
                - (middle[pixel] - previous[pixel]) / dt_left) / divisor
        truth = ((gt_following[pixel] - gt_middle[pixel]) / dt_right
                 - (gt_middle[pixel] - gt_previous[pixel]) / dt_left) / divisor
        error = abs(pred - truth) / 255.0
        values.append(error)
        if retain_map and error:
            error_map.append([pixel, error])
    return values, error_map


def temporal_metrics(timeline: list[dict[str, Any]], width: int, height: int,
                     high_rate_fps: float,
                     strict_pixel_labels: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
    """Compare reconstructed and reference derivatives on the complete timeline."""
    if len(timeline) < 2:
        raise ValueError("temporal metrics need at least two timeline samples")
    mask_labels = tuple(timeline[0]["masks"])
    if any(set(item.get("masks", {})) != set(mask_labels) for item in timeline):
        raise ValueError("timeline samples must use the same evaluation mask labels")
    regions = ("all", *mask_labels)
    unit_ns = 1e9 / high_rate_fps
    edge_reconstructed = [_edge_map(item["rgb"], width, height) for item in timeline]
    edge_reference = [_edge_map(item["reference_rgb"], width, height) for item in timeline]
    residual: dict[str, dict[str, list[float]]] = {
        region: {"mean": [], "p95": [], "max": []} for region in regions
    }
    residual_rows: dict[str, list[dict[str, Any]]] = {region: [] for region in regions}
    flicker: dict[str, dict[str, list[float]]] = {
        region: {"mean": [], "p95": [], "max": []} for region in regions
    }
    edge_flicker: dict[str, dict[str, list[float]]] = {
        region: {"mean": [], "p95": [], "max": []} for region in regions
    }
    flicker_rows: dict[str, list[dict[str, Any]]] = {region: [] for region in regions}
    for i in range(1, len(timeline)):
        left, right = timeline[i - 1], timeline[i]
        if left.get("continuity_group", 0) != right.get("continuity_group", 0):
            continue
        dt = (right["time_ns"] - left["time_ns"]) / unit_ns
        if dt <= 0:
            raise ValueError("timeline timestamps must be strictly increasing")
        for region in regions:
            mask = (bytes([255]) * (width * height) if region == "all"
                    else _union_mask(left["masks"][region], right["masks"][region]))
            values, strict_error_map = _temporal_pixel_errors(
                left["rgb"], right["rgb"], left["reference_rgb"],
                right["reference_rgb"], mask, dt, region in strict_pixel_labels)
            stats = distribution(values)
            residual[region]["mean"].append(stats["mean"] or 0.0)
            residual[region]["p95"].append(stats["p95"] or 0.0)
            residual[region]["max"].append(stats["max"] or 0.0)
            row = {
                "timestamps_ns": [left["time_ns"], right["time_ns"]],
                "continuity_group": left.get("continuity_group", 0),
                "mean": stats["mean"], "p95": stats["p95"], "max": stats["max"],
                "active_pixel_count": len(values),
            }
            if region in strict_pixel_labels:
                row["critical_pixel_errors"] = strict_error_map
            residual_rows[region].append(row)
    for i in range(1, len(timeline) - 1):
        before, middle, after = timeline[i - 1], timeline[i], timeline[i + 1]
        if not (before.get("continuity_group", 0) == middle.get("continuity_group", 0)
                == after.get("continuity_group", 0)):
            continue
        dt_left = (middle["time_ns"] - before["time_ns"]) / unit_ns
        dt_right = (after["time_ns"] - middle["time_ns"]) / unit_ns
        if dt_left <= 0 or dt_right <= 0:
            raise ValueError("timeline timestamps must be strictly increasing")
        for region in regions:
            mask = (bytes([255]) * (width * height) if region == "all"
                    else _union_mask(before["masks"][region], middle["masks"][region],
                                     after["masks"][region]))
            edge_values, strict_edge_error_map = _acceleration_pixel_errors(
                edge_reconstructed[i - 1], edge_reconstructed[i], edge_reconstructed[i + 1],
                edge_reference[i - 1], edge_reference[i], edge_reference[i + 1], mask,
                dt_left, dt_right, region in strict_pixel_labels,
            )
            # Color flicker uses per-pixel RGB acceleration, including exact
            # source frames that surround generated frames.
            color_values = []
            strict_color_error_map = []
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
                error = total / (3 * 255.0)
                color_values.append(error)
                if region in strict_pixel_labels and error:
                    strict_color_error_map.append([pixel, error])
            color_stats, edge_stats = distribution(color_values), distribution(edge_values)
            flicker[region]["mean"].append(color_stats["mean"] or 0.0)
            flicker[region]["p95"].append(color_stats["p95"] or 0.0)
            flicker[region]["max"].append(color_stats["max"] or 0.0)
            edge_flicker[region]["mean"].append(edge_stats["mean"] or 0.0)
            edge_flicker[region]["p95"].append(edge_stats["p95"] or 0.0)
            edge_flicker[region]["max"].append(edge_stats["max"] or 0.0)
            row = {
                "timestamps_ns": [before["time_ns"], middle["time_ns"], after["time_ns"]],
                "continuity_group": middle.get("continuity_group", 0),
                "mean": color_stats["mean"], "p95": color_stats["p95"], "max": color_stats["max"],
                "edge_mean": edge_stats["mean"], "edge_p95": edge_stats["p95"],
                "edge_max": edge_stats["max"], "active_pixel_count": len(color_values),
            }
            if region in strict_pixel_labels:
                row["critical_pixel_errors"] = strict_color_error_map
                row["critical_edge_errors"] = strict_edge_error_map
            flicker_rows[region].append(row)
    result = {}
    for region in regions:
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


def _pair_skip_reasons(ordered_frames: list[dict[str, Any]], left_position: int,
                       right_position: int) -> list[str]:
    left, right = ordered_frames[left_position], ordered_frames[right_position]
    interval_frames = ordered_frames[left_position + 1:right_position + 1]
    reasons = []
    if left["segment_id"] != right["segment_id"]:
        reasons.append("segment_id_mismatch")
    if any(frame["boundary_before"] is not None for frame in interval_frames):
        reasons.append("boundary_before")
    if any(not frame["interpolable"] for frame in [left, *interval_frames]):
        reasons.append("non_interpolable")
    return reasons


def _source_pair_plan(corpus: dict[str, Any], target_plan: list[dict[str, Any]]) -> list[dict[str, Any]]:
    targets_by_pair: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for target in target_plan:
        targets_by_pair.setdefault(tuple(target["source_pair"]), []).append(target)
    frames = corpus["frames"]
    by_index = corpus["by_index"]
    first_index = frames[0]["index"]
    interval_plan = []
    continuity_group = 0
    for left_index, right_index in zip(corpus["source_indices"], corpus["source_indices"][1:]):
        left, right = by_index[left_index], by_index[right_index]
        left_position, right_position = left_index - first_index, right_index - first_index
        reasons = _pair_skip_reasons(frames, left_position, right_position)
        if reasons:
            continuity_group += 1
            interval_plan.append({
                "source_pair": [left_index, right_index],
                "source_timestamps_ns": [left["timestamp_ns"], right["timestamp_ns"]],
                "status": "skipped", "skip_reasons": reasons,
                "continuity_group_after_skip": continuity_group,
            })
            continue
        targets = targets_by_pair.get((left_index, right_index), [])
        if not targets or any(target["continuity_group"] != continuity_group for target in targets):
            raise ValueError(f"eligible source pair {left_index},{right_index} has an incomplete target plan")
        interval_plan.append({
            "source_pair": [left_index, right_index],
            "source_timestamps_ns": [left["timestamp_ns"], right["timestamp_ns"]],
            "status": "selected", "continuity_group": continuity_group,
            "targets": [
                {"timestamp_ns": target["time_ns"], "t": target["t"],
                 "target_index": target["target_index"]}
                for target in targets
            ],
        })
    return interval_plan


def _target_plan(corpus: dict[str, Any], requested_t: float | None) -> list[dict[str, Any]]:
    if requested_t is not None and (not math.isfinite(requested_t) or not 0 < requested_t < 1):
        raise ValueError("--t must be finite and strictly between 0 and 1")
    frames = corpus["by_index"]
    ordered_frames = corpus["frames"]
    first_frame_index = ordered_frames[0]["index"]
    provider = corpus["provider"]
    plan = []
    source = corpus["source_indices"]
    continuity_group = 0
    for left_index, right_index in zip(source, source[1:]):
        left, right = frames[left_index], frames[right_index]
        left_position, right_position = left_index - first_frame_index, right_index - first_frame_index
        interior = ordered_frames[left_position + 1:right_position]
        if _pair_skip_reasons(ordered_frames, left_position, right_position):
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
            # Preserve exact epoch-based nanosecond timestamps. Float conversion
            # loses low bits near 1e18 ns and can skew temporal derivatives.
            timestamp = (target_frame["timestamp_ns"] if target_frame else
                         left["timestamp_ns"] + round(
                             (right["timestamp_ns"] - left["timestamp_ns"]) * t))
            if not left["timestamp_ns"] < timestamp < right["timestamp_ns"]:
                raise ValueError(
                    f"requested t={t:g} for source pair {left_index},{right_index} "
                    "does not resolve to a distinct interior nanosecond timestamp"
                )
            # Use the timestamp-quantized position consistently for the
            # analytic reference, benchmark metadata, and backend request.
            # For very short intervals, integer-nanosecond rounding can move
            # the effective fraction by much more than float precision.
            effective_t = ((timestamp - left["timestamp_ns"]) /
                           (right["timestamp_ns"] - left["timestamp_ns"]))
            coordinate = left_index + (right_index - left_index) * effective_t
            plan.append({
                "source_pair": [left_index, right_index],
                "t": float(effective_t), "time_ns": timestamp,
                "target_index": target_frame["index"] if target_frame else None,
                "coordinate": float(coordinate), "continuity_group": continuity_group,
            })
    if not plan:
        raise ValueError("no exact target-time ground truth was selected")
    return plan


def _timestamp_matches_interpolation(source_times: list[int], timestamp: int,
                                     interpolation_t: float) -> bool:
    span = source_times[1] - source_times[0]
    expected = source_times[0] + round(span * interpolation_t)
    # A float64 fraction cannot represent every integer ratio for large spans.
    # Allow only its arithmetic uncertainty when validating the quantized ns
    # timestamp; the target plan itself is also checked against corpus-derived
    # metadata exactly.
    tolerance = max(1, math.ceil(2.0 * span * math.ulp(float(interpolation_t)) + 1.0))
    return abs(timestamp - expected) <= tolerance


def _read_backend_json(stdout: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line, parse_constant=_reject_json_constant)
            if isinstance(value, dict):
                return value
        except json.JSONDecodeError:
            pass
    raise ValueError("backend did not write a JSON object to stdout")


def _read_backend_json_lines(stdout: str, expected: int) -> list[dict[str, Any]]:
    results = []
    for line_number, line in enumerate(stdout.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError(f"backend server wrote invalid JSON on output line {line_number}") from error
        if not isinstance(value, dict):
            raise ValueError(f"backend server output line {line_number} is not a JSON object")
        results.append(value)
    if len(results) != expected:
        raise ValueError(f"backend server returned {len(results)} result rows; expected {expected}")
    return results


def _iter_backend_json_lines(path: Path, expected: int):
    count = 0
    with path.open("rb") as output:
        line_number = 0
        while True:
            raw_line = output.readline(BACKEND_JSON_LINE_LIMIT_BYTES + 1)
            if not raw_line:
                break
            line_number += 1
            if not raw_line.strip():
                continue
            if len(raw_line) > BACKEND_JSON_LINE_LIMIT_BYTES:
                raise ValueError(f"backend server output line {line_number} exceeds the size limit")
            try:
                value = json.loads(raw_line.decode("utf-8"), parse_constant=_reject_json_constant)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError(f"backend server wrote invalid JSON on output line {line_number}") from error
            if not isinstance(value, dict):
                raise ValueError(f"backend server output line {line_number} is not a JSON object")
            count += 1
            if count > expected:
                raise ValueError(f"backend server returned more than {expected} result rows")
            yield value
    if count != expected:
        raise ValueError(f"backend server returned {count} result rows; expected {expected}")


def _run_backend_session(backend: Path, request_path: Path, stdout_path: Path,
                         stderr_path: Path, timeout_seconds: float) -> None:
    """Run the adapter while bounding captured output bytes and memory use."""
    state = {"bytes": 0, "exceeded": False, "error": None}
    state_lock = threading.Lock()
    with request_path.open("rb") as requests, stdout_path.open("wb") as stdout, \
            stderr_path.open("wb") as stderr:
        process = subprocess.Popen(
            [str(backend.resolve()), "--server"], stdin=requests,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        )

        def terminate_group() -> None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                if process.poll() is None:
                    process.kill()

        def drain(pipe, destination) -> None:
            try:
                while True:
                    chunk = pipe.read(64 * 1024)
                    if not chunk:
                        return
                    with state_lock:
                        if state["bytes"] + len(chunk) > MAX_BACKEND_SESSION_OUTPUT_BYTES:
                            state["exceeded"] = True
                            terminate_group()
                            return
                        state["bytes"] += len(chunk)
                    destination.write(chunk)
            except OSError as error:
                state["error"] = error
                terminate_group()
            finally:
                pipe.close()

        stdout_thread = threading.Thread(target=drain, args=(process.stdout, stdout), daemon=True)
        stderr_thread = threading.Thread(target=drain, args=(process.stderr, stderr), daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            try:
                process.wait(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                terminate_group()
                process.wait()
            stdout_thread.join(timeout=2.0)
            stderr_thread.join(timeout=2.0)
            if stdout_thread.is_alive() or stderr_thread.is_alive():
                terminate_group()
                process.wait()
                stdout_thread.join(timeout=5.0)
                stderr_thread.join(timeout=5.0)
                if stdout_thread.is_alive() or stderr_thread.is_alive():
                    raise ValueError("backend descendants kept output pipes open after session termination")
                state["orphan_descendant"] = True
        finally:
            # The backend owns a detached process group. Always kill and reap
            # it on Ctrl-C, SystemExit, or any unexpected exception so neither
            # the backend nor pipe-drainer threads outlive this run.
            if (process.poll() is None or stdout_thread.is_alive()
                    or stderr_thread.is_alive()):
                terminate_group()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    terminate_group()
                    process.wait()
                stdout_thread.join(timeout=5.0)
                stderr_thread.join(timeout=5.0)
    if timed_out:
        raise ValueError(f"backend sequence session exceeded timeout of {timeout_seconds:g} seconds")
    if state["exceeded"]:
        raise ValueError(
            f"backend sequence output exceeded the {MAX_BACKEND_SESSION_OUTPUT_BYTES} byte limit"
        )
    if state["error"] is not None:
        raise ValueError(f"failed while capturing backend output: {state['error']}")
    if state.get("orphan_descendant"):
        raise ValueError("backend left descendant processes running after the sequence session")
    if process.returncode != 0:
        with stderr_path.open("rb") as error_output:
            error_output.seek(0, os.SEEK_END)
            error_size = error_output.tell()
            error_output.seek(max(0, error_size - 65536))
            error_tail = error_output.read().decode("utf-8", errors="replace")
        raise subprocess.CalledProcessError(
            process.returncode, [str(backend.resolve()), "--server"], stderr=error_tail
        )


def _server_jobs(contexts: list[dict[str, Any]], pass_count: int):
    """Yield jobs in sequence order and reset temporal history at each pass/cut."""
    for pass_index in range(pass_count):
        previous_group = None
        for context in contexts:
            target = context["target"]
            reset_history = (previous_group is None
                             or target["continuity_group"] != previous_group)
            previous_group = target["continuity_group"]
            yield pass_index, context, reset_history


def _mask_union_pixel_count(*masks: bytes) -> int:
    combined = 0
    for mask in masks:
        combined |= int.from_bytes(mask, "little")
    # Authored masks are binary 0/255, so each selected pixel sets eight bits.
    return combined.bit_count() // 8


def _strict_pixel_map_comparison_count(contexts: list[dict[str, Any]],
                                       strict_labels: tuple[str, ...] | list[str]) -> int:
    """Count worst-case spatial and temporal strict-ROI map values before backend work."""
    timeline_samples: dict[int, tuple[int, dict[str, bytes]]] = {}
    target_pixel_count = 0

    def add_sample(timestamp: int, group: int, masks: dict[str, bytes]) -> None:
        previous = timeline_samples.get(timestamp)
        if previous is not None:
            if previous[0] != group:
                raise ValueError("source frame belongs to incompatible continuity segments")
            if previous[1] != masks:
                raise ValueError("two timeline samples at one timestamp have different masks")
            return
        timeline_samples[timestamp] = (group, masks)

    for context in contexts:
        target = context["target"]
        target_masks = context["masks"]
        target_pixel_count += sum(target_masks[label].count(255) for label in strict_labels)
        add_sample(context["left"]["timestamp_ns"], target["continuity_group"],
                   context["left"]["masks"])
        add_sample(context["right"]["timestamp_ns"], target["continuity_group"],
                   context["right"]["masks"])
        add_sample(target["time_ns"], target["continuity_group"], target_masks)

    ordered = [
        (timestamp, group, masks)
        for timestamp, (group, masks) in sorted(timeline_samples.items())
    ]
    comparisons = target_pixel_count
    for index in range(1, len(ordered)):
        _, left_group, left_masks = ordered[index - 1]
        _, right_group, right_masks = ordered[index]
        if left_group == right_group:
            for label in strict_labels:
                comparisons += _mask_union_pixel_count(left_masks[label], right_masks[label])
    for index in range(1, len(ordered) - 1):
        _, before_group, before_masks = ordered[index - 1]
        _, middle_group, middle_masks = ordered[index]
        _, after_group, after_masks = ordered[index + 1]
        if before_group == middle_group == after_group:
            for label in strict_labels:
                active_pixels = _mask_union_pixel_count(
                    before_masks[label], middle_masks[label], after_masks[label])
                comparisons += active_pixels * 2  # color and edge flicker maps
    return comparisons


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
        "expected_target_runs": expected_target_runs,
        "interpolation_gpu_time_ms": {
            **distribution([v / 1e6 for v in gpu]), "missing_samples": gpu_missing,
            "availability": "available" if gpu else "unavailable",
        },
        "cpu_submit_overhead_ms": distribution([v / 1e6 for v in cpu]),
        "completion_latency_ms": distribution([v / 1e6 for v in latency]),
        "gpu_allocated_bytes_current_max": max(gpu_current) if gpu_current else None,
        "gpu_allocated_bytes_sampled_max": max(gpu_peak) if gpu_peak else None,
        "runner_peak_resident_bytes_max": max(resident) if resident else None,
        "gpu_memory_current_sample_count": len(gpu_current),
        "gpu_memory_current_missing_target_runs": max(0, expected_target_runs - len(gpu_current)),
        "gpu_memory_sampled_max_sample_count": len(gpu_peak),
        "gpu_memory_sampled_max_missing_target_runs": max(0, expected_target_runs - len(gpu_peak)),
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
    corpus = validate_corpus(Path(args.corpus), args.max_analysis_memory_mib)
    plan = _target_plan(corpus, args.t)
    pass_count = args.warmup + args.iterations
    server_job_count = pass_count * len(plan)
    if server_job_count > MAX_BACKEND_SERVER_JOBS:
        raise ValueError(
            f"benchmark requires {server_job_count} backend jobs, above the "
            f"{MAX_BACKEND_SERVER_JOBS} job limit"
        )
    width, height = corpus["width"], corpus["height"]
    regions = ("all", *corpus["mask_labels"])
    manifest, frames = corpus["manifest"], corpus["by_index"]
    deadline_ms = args.deadline_ms if args.deadline_ms is not None else 1000.0 / corpus["fps"]
    if not math.isfinite(deadline_ms) or deadline_ms <= 0:
        raise ValueError("deadline must be finite and positive")
    if not math.isfinite(args.backend_timeout_seconds) or args.backend_timeout_seconds <= 0:
        raise ValueError("backend timeout must be finite and positive")
    timeline_by_time: dict[int, dict[str, Any]] = {}
    quality_samples: dict[str, dict[str, list[float]]] = {
        region: {name: [] for name in (
            "psnr_db", "ssim", "perceptual", "delta_e", "pixel_mean", "pixel_p95", "pixel_max",
            "pixel_over_1pct", "pixel_over_5pct", "pixel_over_5pct_count",
            "pixel_over_1pct_count",
            "edge_chamfer", "edge_precision", "edge_recall")}
        for region in regions
    }
    quality_frames = []
    gpu_ms_ns, gpu_missing = [], 0
    cpu_ns, latency_ns = [], []
    gpu_current, gpu_peak, resident = [], [], []
    source_fps, source_with_generation_fps = [], []
    throughput_methods: list[str] = []
    throughput_method_presence: bool | None = None
    throughput_sample_presence: tuple[bool, bool] | None = None
    backend_identity = None
    target_metadata = []
    expected_samples = len(plan) * args.iterations
    gpu_current_by_target: dict[int, int] = {}
    gpu_peak_by_target: dict[int, int] = {}
    resident_by_target: dict[int, int] = {}
    with tempfile.TemporaryDirectory(prefix="framegen-bench-") as temp_dir:
        temp = Path(temp_dir)
        contexts = []
        source_paths: dict[int, Path] = {}
        for ordinal, target in enumerate(plan):
            left_index, right_index = target["source_pair"]
            left, right = frames[left_index], frames[right_index]
            target_index = target["target_index"]
            if target_index is None:
                rendered = corpus["provider"](target["coordinate"])
                if not isinstance(rendered, dict) or "rgb" not in rendered or "masks" not in rendered:
                    raise ValueError("analytic ground-truth provider must return rgb and masks")
                reference = _provider_bytes(
                    rendered["rgb"], width * height * 3, "RGB data for generated timestamp")
                rendered_masks = rendered["masks"]
                if (not isinstance(rendered_masks, dict)
                        or set(rendered_masks) != set(corpus["mask_labels"])):
                    raise ValueError("analytic ground-truth provider returned unexpected mask labels")
                masks = {}
                for label in corpus["mask_labels"]:
                    value = _provider_bytes(
                        rendered_masks[label], width * height,
                        f"{label} mask for generated timestamp")
                    if set(value) - {0, 255}:
                        raise ValueError(f"analytic {label} mask has invalid dimensions or values")
                    if value.count(255) < int(manifest.get("mask_minimum_pixels", {}).get(
                            label, MASK_MIN_PIXELS_DEFAULT)):
                        raise ValueError(f"analytic {label} mask is underfilled")
                    masks[label] = value
            else:
                target_frame = frames[target_index]
                reference, masks = target_frame["rgb"], target_frame["masks"]
            for frame in (left, right):
                if frame["index"] not in source_paths:
                    source_path = temp / f"source-{frame['index']}.ppm"
                    _write_ppm(source_path, width, height, frame["rgb"])
                    source_paths[frame["index"]] = source_path
            contexts.append({
                "ordinal": ordinal, "target": target, "left": left, "right": right,
                "reference": reference, "masks": masks,
                "previous_path": source_paths[left["index"]],
                "current_path": source_paths[right["index"]],
                "output_path": temp / f"generated-t{ordinal}.ppm",
            })
            target_metadata.append({
                "source_pair": target["source_pair"], "target_index": target_index,
                "t": target["t"], "timestamp_ns": target["time_ns"],
                "source_timestamps_ns": [left["timestamp_ns"], right["timestamp_ns"]],
                "continuity_group": target["continuity_group"],
            })

        strict_pixel_map_comparisons = _strict_pixel_map_comparison_count(
            contexts, corpus["strict_pixel_labels"])
        if strict_pixel_map_comparisons > MAX_STRICT_PIXEL_COMPARISONS:
            raise ValueError(
                f"strict pixel-map budget exceeds {MAX_STRICT_PIXEL_COMPARISONS} "
                "pixel comparisons; tighten ROI masks or reduce target count"
            )
        timeline_entry_bound = len(plan) * 3 + 1
        estimated_result_bytes = (
            64 * 1024 + len(plan) * (8192 + len(regions) * 4096)
            + timeline_entry_bound * len(regions) * 768
            + strict_pixel_map_comparisons * 64
        )
        estimated_retained_result_bytes = (
            estimated_result_bytes * 4 + strict_pixel_map_comparisons * 192)
        if estimated_result_bytes > MAX_RESULT_BYTES:
            raise ValueError(
                f"estimated result size {estimated_result_bytes} exceeds the "
                f"{MAX_RESULT_BYTES} byte result limit; use fewer targets or regions"
            )
        analysis_budget_bytes = args.max_analysis_memory_mib * 1024 * 1024
        estimated_working_set = corpus["estimated_analysis_bytes"] + estimated_retained_result_bytes
        if estimated_working_set > analysis_budget_bytes:
            raise ValueError(
                "estimated corpus and retained result working set is "
                f"{estimated_working_set / (1024 * 1024):.0f} MiB, above the "
                f"{args.max_analysis_memory_mib} MiB --max-analysis-memory-mib budget"
            )

        # Run the backend once per ordered sequence. The same process and
        # FrameGenerator session can therefore retain temporal state between
        # generated targets. Warmup and measured passes replay the whole plan;
        # each pass starts with a history reset, and cut/loading boundaries
        # reset history before the first target in the following segment.
        request_path = temp / "backend-requests.tsv"
        stdout_path, stderr_path = temp / "backend.stdout", temp / "backend.stderr"
        with request_path.open("w+", encoding="utf-8", newline="\n") as requests:
            for pass_index, context, reset_history in _server_jobs(contexts, pass_count):
                target = context["target"]
                left, right = context["left"], context["right"]
                output_path = (context["output_path"] if pass_index == pass_count - 1
                               else "-")
                fields = [
                    context["previous_path"], context["current_path"], output_path,
                    format(target["t"], ".17g"), "0", "1",
                    str(left["index"]), str(right["index"]),
                    str(left["timestamp_ns"]), str(right["timestamp_ns"]),
                    "1" if reset_history else "0",
                    args.hud_mode, args.ui_source, args.hud_debug,
                    str(target["time_ns"]),
                ]
                if any("\t" in str(field) or "\n" in str(field) for field in fields):
                    raise ValueError("backend server request paths cannot contain tabs or newlines")
                requests.write("\t".join(str(field) for field in fields) + "\n")
            requests.flush()
            requests.seek(0)
            _run_backend_session(
                Path(args.backend), request_path, stdout_path, stderr_path,
                args.backend_timeout_seconds,
            )
        backend_results = _iter_backend_json_lines(stdout_path, server_job_count)
        for pass_index, context, reset_history in _server_jobs(contexts, pass_count):
            backend_run = next(backend_results)
            target = context["target"]
            left, right = context["left"], context["right"]
            ordinal = context["ordinal"]
            reported_width, reported_height = backend_run.get("width"), backend_run.get("height")
            if (isinstance(reported_width, bool) or not isinstance(reported_width, int)
                    or isinstance(reported_height, bool) or not isinstance(reported_height, int)
                    or reported_width != width or reported_height != height):
                raise ValueError("backend JSON dimensions do not match the corpus")
            reported_t = backend_run.get("interpolation_t", backend_run.get("t"))
            if (isinstance(reported_t, bool) or not isinstance(reported_t, (int, float))
                    or not math.isfinite(reported_t)):
                raise ValueError("backend JSON must report a finite interpolation timestamp")
            if abs(float(reported_t) - target["t"]) > 1e-5:
                raise ValueError("backend JSON reports a different interpolation timestamp")
            expected_timing = {
                "previous_sequence": left["index"],
                "current_sequence": right["index"],
                "previous_timestamp_ns": left["timestamp_ns"],
                "current_timestamp_ns": right["timestamp_ns"],
                "interpolation_timestamp_ns": target["time_ns"],
                "desired_presentation_timestamp_ns": target["time_ns"],
            }
            for key, expected in expected_timing.items():
                value = backend_run.get(key)
                if isinstance(value, bool) or not isinstance(value, int) or value != expected:
                    raise ValueError(f"backend JSON {key} does not match the corpus source pair")
            if backend_run.get("reset_history") is not reset_history:
                raise ValueError("backend JSON reset_history does not match the sequence plan")
            identity = {
                "id": backend_run.get("backend_id", "unspecified"),
                "kind": backend_run.get("backend_kind", "unspecified"),
                "metadata": backend_run.get("backend_metadata"),
            }
            if identity["metadata"] is not None and not isinstance(identity["metadata"], dict):
                raise ValueError("backend_metadata must be a JSON object")
            device_id = backend_run.get("device_id")
            device_name = backend_run.get("device_name")
            if not isinstance(device_id, str) or not device_id.strip():
                raise ValueError("backend device_id must be a non-empty stable accelerator identity")
            if not isinstance(device_name, str) or not device_name.strip():
                raise ValueError("backend device_name must be a non-empty accelerator name")
            identity["device"] = {"id": device_id, "name": device_name}
            if backend_identity is None:
                backend_identity = identity
            elif identity != backend_identity:
                raise ValueError("backend identity changed between target timestamps")
            measure = pass_index >= args.warmup
            if not measure:
                continue
            gpu_values, missing = _positive_samples(backend_run, "gpu_execution_time_ns", 1)
            if missing:
                raise ValueError(
                    f"backend GPU timing is unavailable for {missing} samples at "
                    f"source pair {left['index']},{right['index']}; complete GPU timing coverage is required"
                )
            cpu_values, cpu_missing = _positive_samples(
                backend_run, "cpu_submit_overhead_ns", 1)
            latency_values, latency_missing = _positive_samples(
                backend_run, "completion_latency_ns", 1)
            if cpu_missing or latency_missing:
                raise ValueError(
                    "backend CPU submit and completion latency measurements must be positive "
                    "for every measured sample"
                )
            gpu_ms_ns.extend(gpu_values)
            gpu_missing += missing
            cpu_ns.extend(cpu_values)
            latency_ns.extend(latency_values)
            for key, target_values in (
                ("gpu_allocated_bytes_current", gpu_current_by_target),
                ("gpu_allocated_bytes_sampled_max", gpu_peak_by_target),
                ("peak_resident_bytes", resident_by_target),
            ):
                value = backend_run.get(key)
                if value is not None:
                    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                        raise ValueError(f"backend {key} must be a positive integer when supplied")
                    target_values[ordinal] = max(value, target_values.get(ordinal, 0))
            throughput_values = []
            for key in ("source_only_input_fps", "source_with_generation_input_fps"):
                value = backend_run.get(key)
                if value is not None and (
                        isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value <= 0):
                    raise ValueError(f"backend {key} must be finite and positive when supplied")
                throughput_values.append(None if value is None else float(value))
            sample_presence = tuple(value is not None for value in throughput_values)
            if sample_presence not in ((False, False), (True, True)):
                raise ValueError("backend must report both source throughput measurements or neither")
            if throughput_sample_presence is not None and sample_presence != throughput_sample_presence:
                raise ValueError("backend throughput sample availability changed between targets")
            throughput_sample_presence = sample_presence
            if sample_presence == (True, True):
                source_fps.append(throughput_values[0])
                source_with_generation_fps.append(throughput_values[1])
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
            if sample_presence[1] != method_present:
                raise ValueError("backend throughput method must be supplied exactly when throughput is reported")
            if pass_index != args.warmup + args.iterations - 1:
                continue
            gw, gh, generated = read_image(context["output_path"], width, height)
            if (gw, gh) != (width, height):
                raise ValueError("backend output image dimensions changed")
            reference, masks = context["reference"], context["masks"]
            target_index = target["target_index"]
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
            for region in regions:
                mask = bytes([255]) * (width * height) if region == "all" else masks[region]
                values = evaluate_frame(reference, generated, mask, width, height, ref_edges, generated_edges)
                region_metrics[region] = values
                bucket = quality_samples[region]
                bucket["psnr_db"].append(values["psnr_db"])
                bucket["ssim"].append(values["ssim_7x7_masked_luminance"])
                bucket["perceptual"].append(values["perceptual_similarity"])
                bucket["delta_e"].append(values["cielab_delta_e76_mean"])
                bucket["pixel_mean"].append(values["pixel_abs_error_norm_mean"])
                bucket["pixel_p95"].append(values["pixel_abs_error_norm_p95"])
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
                "t": target["t"], "timestamp_ns": time_ns,
                "continuity_group": target["continuity_group"], "regions": region_metrics,
                "mask_pixel_counts": {
                    label: masks[label].count(255) for label in corpus["mask_labels"]
                },
                "critical_pixel_errors": {
                    label: _critical_pixel_error_map(reference, generated, masks[label])
                    for label in corpus["strict_pixel_labels"]
                },
            })
        try:
            next(backend_results)
        except StopIteration:
            pass
        else:
            raise ValueError("backend server returned more result rows than requested")
        gpu_current.extend(gpu_current_by_target[index] for index in sorted(gpu_current_by_target))
        gpu_peak.extend(gpu_peak_by_target[index] for index in sorted(gpu_peak_by_target))
        resident.extend(resident_by_target[index] for index in sorted(resident_by_target))
    for target in plan:
        for source_index in target["source_pair"]:
            frame = frames[source_index]
            stamp = frame["timestamp_ns"]
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
    temporal = temporal_metrics(
        timeline, width, height, corpus["fps"], corpus["strict_pixel_labels"])
    quality = {}
    for region, values in quality_samples.items():
        quality[region] = {
            "sample_count": len(quality_frames),
            "psnr_db": distribution(values["psnr_db"]),
            "ssim_7x7_masked_luminance": distribution(values["ssim"]),
            "perceptual_similarity": distribution(values["perceptual"]),
            "cielab_delta_e76_mean": distribution(values["delta_e"]),
            "pixel_abs_error_norm_mean_per_target": distribution(values["pixel_mean"]),
            "pixel_abs_error_norm_p95_per_target": distribution(values["pixel_p95"]),
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
    compatibility_host = {
        "platform": platform.platform(), "system": platform.system(),
        "machine": platform.machine(),
        "gpu_device": (backend_identity or {}).get("device"),
    }
    frame_selection_metadata = [
        {"index": frame["index"], "timestamp_ns": frame["timestamp_ns"],
         "segment_id": frame["segment_id"], "boundary_before": frame["boundary_before"],
         "interpolable": frame["interpolable"]}
        for frame in corpus["frames"]
    ]
    source_pair_plan = _source_pair_plan(corpus, plan)
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
        "host": {**compatibility_host,
                 "python": platform.python_version()},
        "corpus": {
            "sequence_id": manifest["sequence_id"], "title": manifest.get("title"),
            "manifest": str(corpus["path"]), "manifest_sha256": corpus["manifest_sha256"],
            "content_sha256": corpus["content_sha256"], "width": width, "height": height,
            "high_rate_fps": corpus["fps"], "low_rate_stride_frames": corpus["stride"],
            "source_indices": corpus["source_indices"],
            "mask_labels": corpus["mask_labels"],
            "strict_pixel_labels": corpus["strict_pixel_labels"],
            "analytic_provider": manifest.get("analytic_provider"),
            "frame_selection_metadata": frame_selection_metadata,
            "license": manifest["license"],
            "source": manifest.get("source"), "fixture_coverage": manifest.get("fixture_coverage", []),
            "mask_coverage_pixels_by_frame": {
                str(frame["index"]): frame["mask_counts"] for frame in corpus["frames"]
            },
            "mask_overlap_policy": "Masks are evaluated independently and may overlap; HUD/text and scene/occlusion are not forced into a partition.",
        },
        "configuration": {
            "warmup_iterations": args.warmup, "measured_iterations_per_target": args.iterations,
            "deadline_ms": deadline_ms, "backend_timeout_seconds": args.backend_timeout_seconds,
            "max_analysis_memory_mib": args.max_analysis_memory_mib,
            "estimated_analysis_memory_bytes": corpus["estimated_analysis_bytes"],
            "estimated_result_bytes": estimated_result_bytes,
            "strict_pixel_map_comparisons": strict_pixel_map_comparisons,
            "requested_t": args.t,
            "target_timeline": target_metadata,
            "source_pair_plan": source_pair_plan,
            "temporal_timeline": [
                {"timestamp_ns": item["time_ns"],
                 "continuity_group": item.get("continuity_group", 0)}
                for item in timeline
            ],
            "runner_mode": "offline serial submit and completion; final measured output is read back for quality analysis",
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
        "compatibility": _compatibility_signature(result_host=compatibility_host, corpus={
            "content_sha256": corpus["content_sha256"], "sequence_id": manifest["sequence_id"],
            "width": width, "height": height, "high_rate_fps": corpus["fps"],
            "low_rate_stride_frames": corpus["stride"], "source_indices": corpus["source_indices"],
            "mask_labels": corpus["mask_labels"],
            "strict_pixel_labels": corpus["strict_pixel_labels"],
            "analytic_provider": manifest.get("analytic_provider"),
            "frame_selection_metadata": frame_selection_metadata,
        }, configuration={
            "warmup_iterations": args.warmup, "measured_iterations_per_target": args.iterations,
            "deadline_ms": deadline_ms, "backend_timeout_seconds": args.backend_timeout_seconds,
            "max_analysis_memory_mib": args.max_analysis_memory_mib,
            "estimated_analysis_memory_bytes": corpus["estimated_analysis_bytes"],
            "estimated_result_bytes": estimated_result_bytes,
            "strict_pixel_map_comparisons": strict_pixel_map_comparisons,
            "target_timeline": target_metadata,
            "source_pair_plan": source_pair_plan,
            "temporal_timeline": [
                {"timestamp_ns": item["time_ns"],
                 "continuity_group": item.get("continuity_group", 0)}
                for item in timeline
            ],
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
    _write_json_bounded(output, result, "benchmark result")
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
        "| Region | Residual p95 / max | Flicker p95 / max | Edge chamfer mean / worst | Edge recall min | Pixel mean / p95 / max |",
        "| --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for region, quality in result["quality_by_region"].items():
        temporal = quality["temporal"]
        residual = temporal["frame_to_frame_residual"]
        flicker = temporal["high_frequency_temporal_flicker"]
        edge = quality["edge_location"]
        pixel_mean = quality["pixel_abs_error_norm_mean_per_target"]
        pixel_p95 = quality["pixel_abs_error_norm_p95_per_target"]
        pixel_max = quality["frame_max_pixel_error_norm"]
        lines.append(
            f"| {region} | {_fmt(residual['per_transition_p95']['p95'])} / {_fmt(residual['worst_pixel_per_transition']['max'])} "
            f"| {_fmt(flicker['per_window_p95']['p95'])} / {_fmt(flicker['worst_pixel_per_window']['max'])} "
            f"| {_fmt(edge['symmetric_chamfer_norm']['mean'])} / {_fmt(edge['worst_target_chamfer_norm'])} "
            f"| {_fmt(edge['minimum_target_recall_at_1px'])} "
            f"| {_fmt(pixel_mean['mean'])} / {_fmt(pixel_p95['p95'])} / {_fmt(pixel_max['max'])} |"
        )
    lines += [
        "", "Temporal residual and flicker are evaluated on the reconstructed timeline, including exact unchanged source frames. "
        "HUD, text, scene, and occlusion masks are scored independently and may overlap.",
        "", "## Image quality diagnostics", "",
        "PSNR is diagnostic only and is capped at 120 dB; the perfect-match flag distinguishes exact matches from finite scores at the cap.",
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
    mem = perf["gpu_allocated_bytes_sampled_max"]
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
    ("completion_latency_ms", "max", "lower", 0.1),
    ("interpolation_gpu_time_ms", "p50", "lower", 0.1),
    ("interpolation_gpu_time_ms", "p95", "lower", 0.1),
    ("interpolation_gpu_time_ms", "p99", "lower", 0.1),
    ("interpolation_gpu_time_ms", "max", "lower", 0.1),
    ("cpu_submit_overhead_ms", "p95", "lower", 0.1),
    ("cpu_submit_overhead_ms", "p99", "lower", 0.1),
    ("cpu_submit_overhead_ms", "max", "lower", 0.1),
]


def _nested(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"result is missing required metric field {path}")
        current = current[part]
    return current


def _require_distribution_match(actual: Any, expected: dict[str, Any], label: str) -> None:
    if not isinstance(actual, dict) or set(actual) != set(expected):
        raise ValueError(f"{label} has an invalid aggregate distribution")
    for key, expected_value in expected.items():
        actual_value = actual[key]
        if expected_value is None:
            if actual_value is not None:
                raise ValueError(f"{label} aggregate {key} does not match its source records")
        elif (isinstance(actual_value, bool) or not isinstance(actual_value, (int, float))
              or not math.isfinite(actual_value)
              or not math.isclose(actual_value, expected_value, rel_tol=1e-12, abs_tol=1e-12)):
            raise ValueError(f"{label} aggregate {key} does not match its source records")


def _validate_sparse_scalar_pixel_map(rows: Any, pixel_count: int, label: str,
                                     integer_max: int | None = None) -> None:
    if not isinstance(rows, list):
        raise ValueError(f"{label} is missing a sparse per-pixel error map")
    previous_pixel = -1
    for row in rows:
        if (not isinstance(row, list) or len(row) != 2
                or isinstance(row[0], bool) or not isinstance(row[0], int)
                or not previous_pixel < row[0] < pixel_count):
            raise ValueError(f"{label} has an invalid sparse per-pixel error record")
        value = row[1]
        if integer_max is not None:
            if (isinstance(value, bool) or not isinstance(value, int)
                    or not 0 < value <= integer_max):
                raise ValueError(f"{label} has an invalid integer pixel error")
        elif (isinstance(value, bool) or not isinstance(value, (int, float))
              or not math.isfinite(value) or value <= 0):
            raise ValueError(f"{label} has an invalid floating-point pixel error")
        previous_pixel = row[0]


def _sparse_pixel_distribution(nonzero_values: list[float], active_count: int) -> dict[str, Any]:
    """Compute dense-pixel distribution statistics from positive sparse values."""
    if active_count <= 0 or len(nonzero_values) > active_count:
        raise ValueError("sparse pixel map has an invalid active-pixel count")
    ordered = sorted(nonzero_values)
    if any(not math.isfinite(value) or value <= 0 for value in ordered):
        raise ValueError("sparse pixel map contains an invalid error value")
    zero_count = active_count - len(ordered)

    def value_at(index: int) -> float:
        return 0.0 if index < zero_count else ordered[index - zero_count]

    def percentile_at(percent: float) -> float:
        position = (active_count - 1) * percent / 100.0
        low, high = math.floor(position), math.ceil(position)
        low_value, high_value = value_at(low), value_at(high)
        return (low_value * (high - position) + high_value * (position - low)
                if low != high else low_value)

    return {
        "count": active_count,
        "mean": math.fsum(ordered) / active_count,
        "min": 0.0 if zero_count else ordered[0],
        "p50": percentile_at(50),
        "p95": percentile_at(95),
        "p99": percentile_at(99),
        "max": ordered[-1] if ordered else 0.0,
    }


def _require_sparse_pixel_statistics(actual: dict[str, Any], values: list[float],
                                     active_count: int, label: str,
                                     fields: tuple[str, ...] = ("mean", "p95", "max")) -> None:
    expected = _sparse_pixel_distribution(values, active_count)
    for field in fields:
        value = actual.get(field)
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or not math.isclose(value, expected[field], rel_tol=1e-12, abs_tol=1e-12)):
            raise ValueError(f"{label} {field} does not match its sparse per-pixel map")


def _sparse_rgb_map_psnr(rows: list[list[int]], active_count: int) -> float:
    """Recompute the capped RGB PSNR from sparse per-channel absolute errors."""
    squared_error_sum = sum(channel * channel for row in rows for channel in row[1:])
    if squared_error_sum == 0:
        return 120.0
    mse = squared_error_sum / (3 * active_count)
    return min(120.0, 10.0 * math.log10(255.0 * 255.0 / mse))


def _validate_result_impl(result: dict[str, Any], label: str) -> None:
    if not isinstance(result, dict) or result.get("schema_version") != 1:
        raise ValueError(f"{label} is not a supported benchmark result")
    if result.get("metric_contract_version") != METRIC_CONTRACT_VERSION:
        raise ValueError(f"{label} uses an incompatible metric contract")
    compatibility = result.get("compatibility")
    host, corpus, configuration = (result.get("host"), result.get("corpus"),
                                   result.get("configuration"))
    if not all(isinstance(value, dict) for value in (compatibility, host, corpus, configuration)):
        raise ValueError(f"{label} is missing compatibility metadata or run identity")
    mask_labels = corpus.get("mask_labels")
    strict_pixel_labels = corpus.get("strict_pixel_labels")
    if (not isinstance(mask_labels, list) or len(mask_labels) < len(MASK_LABELS)
            or len(mask_labels) > MAX_CORPUS_MASK_LABELS
            or any(not isinstance(name, str) or not name or len(name) > 64
                   or not name[0].islower()
                   or any(not (char.islower() or char.isdigit() or char in "_-") for char in name)
                   for name in mask_labels)
            or len(set(mask_labels)) != len(mask_labels)
            or not set(MASK_LABELS).issubset(mask_labels)
            or not isinstance(strict_pixel_labels, list)
            or any(not isinstance(name, str) or name not in mask_labels
                   for name in strict_pixel_labels)
            or len(set(strict_pixel_labels)) != len(strict_pixel_labels)
            or not set(MANDATORY_STRICT_PIXEL_LABELS).issubset(strict_pixel_labels)):
        raise ValueError(f"{label} has invalid mask or strict-pixel label metadata")
    for field in ("content_sha256", "manifest_sha256"):
        digest = corpus.get(field)
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdefABCDEF" for character in digest)):
            raise ValueError(f"{label} has an invalid corpus {field}")
    for field in ("sequence_id", "license"):
        value = corpus.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{label} has no corpus {field}")
    region_labels = ("all", *mask_labels)
    expected_compatibility = {
        "host": {key: host.get(key) for key in ("platform", "system", "machine", "gpu_device")},
        "corpus": {key: corpus.get(key) for key in (
            "content_sha256", "sequence_id", "width", "height", "high_rate_fps",
            "low_rate_stride_frames", "source_indices", "mask_labels", "strict_pixel_labels", "analytic_provider",
            "frame_selection_metadata")},
        "configuration": {key: configuration.get(key) for key in (
            "warmup_iterations", "measured_iterations_per_target", "deadline_ms",
            "backend_timeout_seconds", "max_analysis_memory_mib",
            "estimated_analysis_memory_bytes", "estimated_result_bytes",
            "strict_pixel_map_comparisons",
            "target_timeline", "source_pair_plan",
            "temporal_timeline", "metric_parameters")},
    }
    if compatibility != expected_compatibility:
        raise ValueError(f"{label} compatibility metadata does not match its reported run identity")
    device = host.get("gpu_device")
    backend = result.get("backend")
    if (not isinstance(device, dict) or set(device) != {"id", "name"}
            or not isinstance(device.get("id"), str) or not device["id"].strip()
            or not isinstance(device.get("name"), str) or not device["name"].strip()
            or not isinstance(backend, dict) or backend.get("device") != device):
        raise ValueError(f"{label} is missing a stable backend device identity")
    configured_targets = configuration.get("target_timeline")
    if not isinstance(configured_targets, list):
        raise ValueError(f"{label} has invalid target timeline configuration")
    expected_target_runs_from_configuration = len(configured_targets)
    expected_target_runs = expected_target_runs_from_configuration
    iterations = configuration.get("measured_iterations_per_target")
    if (isinstance(iterations, bool) or not isinstance(iterations, int) or iterations <= 0
            or expected_target_runs <= 0):
        raise ValueError(f"{label} has invalid target or iteration configuration")
    expected_samples_from_configuration = expected_target_runs * iterations
    estimated_result_bytes = configuration.get("estimated_result_bytes")
    if (isinstance(estimated_result_bytes, bool) or not isinstance(estimated_result_bytes, int)
            or not 0 < estimated_result_bytes <= MAX_RESULT_BYTES):
        raise ValueError(f"{label} has an invalid estimated result size")
    strict_pixel_map_comparisons = configuration.get("strict_pixel_map_comparisons")
    if (isinstance(strict_pixel_map_comparisons, bool)
            or not isinstance(strict_pixel_map_comparisons, int)
            or not 0 <= strict_pixel_map_comparisons <= MAX_STRICT_PIXEL_COMPARISONS):
        raise ValueError(f"{label} has an invalid strict pixel-map comparison count")
    actual_strict_map_entries = 0

    def account_strict_map(rows: list, description: str) -> None:
        nonlocal actual_strict_map_entries
        actual_strict_map_entries += len(rows)
        if (actual_strict_map_entries > strict_pixel_map_comparisons
                or actual_strict_map_entries > MAX_STRICT_PIXEL_COMPARISONS):
            raise ValueError(f"{label} {description} exceed the declared strict pixel-map budget")
    source_indices = corpus.get("source_indices")
    stride = corpus.get("low_rate_stride_frames")
    frame_metadata = corpus.get("frame_selection_metadata")
    provider = corpus.get("analytic_provider")
    if (isinstance(stride, bool) or not isinstance(stride, int) or stride <= 0
            or not isinstance(source_indices, list)
            or any(isinstance(index, bool) or not isinstance(index, int) for index in source_indices)
            or not isinstance(frame_metadata, list) or len(frame_metadata) < stride + 1
            or (provider is not None and (not isinstance(provider, str) or not provider.strip()))):
        raise ValueError(f"{label} is missing corpus frame-selection metadata")
    width, height = corpus.get("width"), corpus.get("height")
    high_rate_fps = corpus.get("high_rate_fps")
    if (isinstance(width, bool) or not isinstance(width, int) or not 0 < width <= 32768
            or isinstance(height, bool) or not isinstance(height, int) or not 0 < height <= 32768
            or width * height > 268_435_456
            or isinstance(high_rate_fps, bool)
            or not isinstance(high_rate_fps, (int, float))
            or not math.isfinite(high_rate_fps) or high_rate_fps <= 0):
        raise ValueError(f"{label} has invalid corpus dimensions")
    normalized_metadata = []
    prior_index = prior_timestamp = None
    for item in frame_metadata:
        if not isinstance(item, dict):
            raise ValueError(f"{label} has invalid corpus frame-selection metadata")
        index, timestamp = item.get("index"), item.get("timestamp_ns")
        segment, boundary, interpolable = (item.get("segment_id"), item.get("boundary_before"),
                                           item.get("interpolable"))
        if (isinstance(index, bool) or not isinstance(index, int) or index < 0
                or isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0
                or not isinstance(segment, str) or not segment
                or boundary not in (None, "scene_cut", "loading_transition")
                or not isinstance(interpolable, bool)
                or (prior_index is not None and index != prior_index + 1)
                or (prior_timestamp is not None and timestamp <= prior_timestamp)):
            raise ValueError(f"{label} has invalid corpus frame-selection fields")
        normalized_metadata.append({
            "index": index, "timestamp_ns": timestamp, "segment_id": segment,
            "boundary_before": boundary, "interpolable": interpolable,
        })
        prior_index, prior_timestamp = index, timestamp
    source_mask_coverage = corpus.get("mask_coverage_pixels_by_frame")
    expected_mask_coverage_keys = {str(item["index"]) for item in normalized_metadata}
    if (not isinstance(source_mask_coverage, dict)
            or set(source_mask_coverage) != expected_mask_coverage_keys):
        raise ValueError(f"{label} is missing per-source-frame mask coverage")
    for frame_index, counts in source_mask_coverage.items():
        if (not isinstance(counts, dict) or set(counts) != set(mask_labels)
                or any(isinstance(count, bool) or not isinstance(count, int)
                       or not 0 < count <= width * height for count in counts.values())):
            raise ValueError(f"{label} has invalid mask coverage for source frame {frame_index}")
    expected_sources = [item["index"] for item in normalized_metadata[::stride]]
    if source_indices != expected_sources or (len(normalized_metadata) - 1) % stride != 0:
        raise ValueError(f"{label} source indices do not cover its frame-selection metadata")
    requested_t = configuration.get("requested_t")
    if requested_t is not None and (
            isinstance(requested_t, bool) or not isinstance(requested_t, (int, float))
            or not math.isfinite(requested_t) or not 0 < requested_t < 1):
        raise ValueError(f"{label} has an invalid requested interpolation timestamp")
    selection_corpus = {
        "frames": normalized_metadata,
        "by_index": {item["index"]: item for item in normalized_metadata},
        "source_indices": source_indices,
        "provider": provider,
    }
    expected_target_plan = _target_plan(selection_corpus, requested_t)
    expected_target_metadata = []
    for target in expected_target_plan:
        left, right = (selection_corpus["by_index"][index]
                       for index in target["source_pair"])
        expected_target_metadata.append({
            "source_pair": target["source_pair"], "target_index": target["target_index"],
            "t": target["t"], "timestamp_ns": target["time_ns"],
            "source_timestamps_ns": [left["timestamp_ns"], right["timestamp_ns"]],
            "continuity_group": target["continuity_group"],
        })
    if configured_targets != expected_target_metadata:
        raise ValueError(f"{label} target list does not cover every eligible source pair")
    expected_pair_plan = _source_pair_plan(selection_corpus, expected_target_plan)
    if configuration.get("source_pair_plan") != expected_pair_plan:
        raise ValueError(f"{label} source-pair plan omits or misclassifies an interval")
    expected_temporal_groups: dict[int, int] = {}
    for target in configured_targets:
        if not isinstance(target, dict):
            raise ValueError(f"{label} has invalid target timeline metadata")
        pair, source_times = target.get("source_pair"), target.get("source_timestamps_ns")
        target_time, group = target.get("timestamp_ns"), target.get("continuity_group")
        interpolation_t = target.get("t")
        source_indices = corpus.get("source_indices")
        if (not isinstance(pair, list) or len(pair) != 2
                or any(isinstance(index, bool) or not isinstance(index, int) for index in pair)
                or pair[0] >= pair[1]
                or not isinstance(source_indices, list)
                or any(isinstance(index, bool) or not isinstance(index, int) for index in source_indices)
                or not any(source_indices[index:index + 2] == pair
                           for index in range(len(source_indices) - 1))
                or not isinstance(source_times, list) or len(source_times) != 2
                or any(isinstance(stamp, bool) or not isinstance(stamp, int) or stamp < 0
                       for stamp in source_times)
                or source_times[0] >= source_times[1]
                or isinstance(target_time, bool) or not isinstance(target_time, int)
                or not source_times[0] < target_time < source_times[1]
                or isinstance(interpolation_t, bool)
                or not isinstance(interpolation_t, (int, float))
                or not math.isfinite(interpolation_t) or not 0 < interpolation_t < 1
                or isinstance(group, bool) or not isinstance(group, int) or group < 0):
            raise ValueError(f"{label} has invalid target/source timeline metadata")
        if not _timestamp_matches_interpolation(source_times, target_time,
                                                float(interpolation_t)):
            raise ValueError(f"{label} target timestamp disagrees with its interpolation fraction")
        for timestamp in (*source_times, target_time):
            prior_group = expected_temporal_groups.setdefault(timestamp, group)
            if prior_group != group:
                raise ValueError(f"{label} source/target timestamp belongs to multiple continuity groups")
    temporal_timeline = configuration.get("temporal_timeline")
    if not isinstance(temporal_timeline, list) or len(temporal_timeline) < 3:
        raise ValueError(f"{label} is missing its temporal timeline identity")
    temporal_times, temporal_groups = [], []
    for item in temporal_timeline:
        if not isinstance(item, dict):
            raise ValueError(f"{label} has invalid temporal timeline metadata")
        timestamp, group = item.get("timestamp_ns"), item.get("continuity_group")
        if (isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0
                or isinstance(group, bool) or not isinstance(group, int) or group < 0):
            raise ValueError(f"{label} has invalid temporal timeline timestamp or segment")
        temporal_times.append(timestamp)
        temporal_groups.append(group)
    if any(a >= b for a, b in zip(temporal_times, temporal_times[1:])):
        raise ValueError(f"{label} temporal timeline timestamps are not strictly increasing")
    actual_temporal_groups = dict(zip(temporal_times, temporal_groups))
    if actual_temporal_groups != expected_temporal_groups:
        raise ValueError(f"{label} temporal timeline omits or adds source/target samples")
    expected_transitions = [
        ((temporal_times[index], temporal_times[index + 1]), temporal_groups[index])
        for index in range(len(temporal_times) - 1)
        if temporal_groups[index] == temporal_groups[index + 1]
    ]
    expected_windows = [
        ((temporal_times[index], temporal_times[index + 1], temporal_times[index + 2]),
         temporal_groups[index])
        for index in range(len(temporal_times) - 2)
        if temporal_groups[index] == temporal_groups[index + 1] == temporal_groups[index + 2]
    ]
    if not expected_transitions or not expected_windows:
        raise ValueError(f"{label} has no complete temporal transitions or flicker windows")
    quality_by_region = result.get("quality_by_region")
    if not isinstance(quality_by_region, dict) or set(quality_by_region) != set(region_labels):
        raise ValueError(f"{label} must contain every required quality region")
    for region in region_labels:
        quality = quality_by_region[region]
        psnr = quality.get("psnr_db")
        psnr_count = psnr.get("count") if isinstance(psnr, dict) else None
        if (isinstance(psnr_count, bool) or not isinstance(psnr_count, int)
                or psnr_count != expected_target_runs_from_configuration):
            raise ValueError(f"{label} is missing diagnostic PSNR samples in region {region}")
        for statistic in ("mean", "min", "p50", "p95", "p99", "max"):
            value = psnr.get(statistic)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or not 0 <= value <= 120):
                raise ValueError(f"{label} has invalid diagnostic PSNR {statistic} in region {region}")
        for group, path, _, _ in QUALITY_RULES:
            base = quality["temporal"] if group == "temporal" else quality
            value = _nested(base, path)
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{label} has unavailable or invalid {group} metric in region {region}: {path}")
        residual_summary = quality["temporal"].get("frame_to_frame_residual", {})
        flicker_summary = quality["temporal"].get("high_frequency_temporal_flicker", {})
        residual_rows = residual_summary.get("transitions")
        flicker_rows = flicker_summary.get("windows")
        if not isinstance(residual_rows, list) or not isinstance(flicker_rows, list):
            raise ValueError(f"{label} is missing per-transition or per-window temporal metrics for {region}")
        for rows, expected_rows, collection in (
                (residual_rows, expected_transitions, "transitions"),
                (flicker_rows, expected_windows, "windows")):
            identities = []
            for row in rows:
                stamps, group = row.get("timestamps_ns"), row.get("continuity_group")
                if (not isinstance(stamps, list)
                        or any(isinstance(stamp, bool) or not isinstance(stamp, int) for stamp in stamps)
                        or isinstance(group, bool) or not isinstance(group, int)):
                    raise ValueError(f"{label} has invalid temporal row identity in {region}.{collection}")
                identities.append((tuple(stamps), group))
            if identities != expected_rows:
                raise ValueError(f"{label} has incomplete or reordered temporal {collection} for {region}")
        for row in residual_rows:
            for metric in ("mean", "p95", "max"):
                value = row.get(metric)
                if (value is None or isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value < 0):
                    raise ValueError(f"{label} has an invalid temporal transition metric in {region}")
            active_count = row.get("active_pixel_count")
            if (isinstance(active_count, bool) or not isinstance(active_count, int)
                    or not 0 < active_count <= width * height):
                raise ValueError(f"{label} has an invalid temporal active pixel count in {region}")
            if region in strict_pixel_labels:
                pixel_errors = row.get("critical_pixel_errors")
                _validate_sparse_scalar_pixel_map(
                    pixel_errors, width * height,
                    f"{label} {region} transition", integer_max=765)
                if len(pixel_errors) > active_count:
                    raise ValueError(f"{label} {region} transition pixel map exceeds its mask area")
                account_strict_map(pixel_errors, f"{region} transition maps")
                interval = (row["timestamps_ns"][1] - row["timestamps_ns"][0]) / (
                    1e9 / high_rate_fps)
                pixel_values = [value / (3 * 255.0 * interval)
                                for _, value in pixel_errors]
                _require_sparse_pixel_statistics(
                    row, pixel_values, active_count,
                    f"{label} {region} transition")
        for row in flicker_rows:
            for metric in ("mean", "p95", "max", "edge_mean", "edge_p95", "edge_max"):
                value = row.get(metric)
                if (value is None or isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value < 0):
                    raise ValueError(f"{label} has an invalid temporal window metric in {region}")
            active_count = row.get("active_pixel_count")
            if (isinstance(active_count, bool) or not isinstance(active_count, int)
                    or not 0 < active_count <= width * height):
                raise ValueError(f"{label} has an invalid temporal active pixel count in {region}")
            if region in strict_pixel_labels:
                color_errors = row.get("critical_pixel_errors")
                edge_errors = row.get("critical_edge_errors")
                _validate_sparse_scalar_pixel_map(
                    color_errors, width * height,
                    f"{label} {region} color-flicker window")
                _validate_sparse_scalar_pixel_map(
                    edge_errors, width * height,
                    f"{label} {region} edge-flicker window")
                if (len(color_errors) > active_count
                        or len(edge_errors) > active_count):
                    raise ValueError(f"{label} {region} temporal pixel map exceeds its mask area")
                account_strict_map(color_errors, f"{region} color-flicker maps")
                account_strict_map(edge_errors, f"{region} edge-flicker maps")
                _require_sparse_pixel_statistics(
                    row, [value for _, value in color_errors], active_count,
                    f"{label} {region} color-flicker window")
                edge_statistics = {
                    "mean": row["edge_mean"], "p95": row["edge_p95"],
                    "max": row["edge_max"],
                }
                _require_sparse_pixel_statistics(
                    edge_statistics, [value for _, value in edge_errors], active_count,
                    f"{label} {region} edge-flicker window")
        temporal_distributions = {
            "frame_to_frame_residual.per_transition_mean": [row["mean"] for row in residual_rows],
            "frame_to_frame_residual.per_transition_p95": [row["p95"] for row in residual_rows],
            "frame_to_frame_residual.worst_pixel_per_transition": [row["max"] for row in residual_rows],
            "high_frequency_temporal_flicker.per_window_mean": [row["mean"] for row in flicker_rows],
            "high_frequency_temporal_flicker.per_window_p95": [row["p95"] for row in flicker_rows],
            "high_frequency_temporal_flicker.worst_pixel_per_window": [row["max"] for row in flicker_rows],
            "edge_flicker.per_window_mean": [row["edge_mean"] for row in flicker_rows],
            "edge_flicker.per_window_p95": [row["edge_p95"] for row in flicker_rows],
            "edge_flicker.worst_pixel_per_window": [row["edge_max"] for row in flicker_rows],
        }
        for path, values in temporal_distributions.items():
            _require_distribution_match(
                _nested(quality["temporal"], path), distribution(values),
                f"{label} {region}.{path}")
        expected_consistency = max(0.0, 1.0 - statistics.fmean(
            row["mean"] for row in residual_rows))
        consistency = quality["temporal"].get("temporal_consistency_score")
        if (isinstance(consistency, bool) or not isinstance(consistency, (int, float))
                or not math.isfinite(consistency)
                or not math.isclose(consistency, expected_consistency,
                                    rel_tol=1e-12, abs_tol=1e-12)):
            raise ValueError(f"{label} temporal consistency score does not match its transition records")
    perf = result.get("performance")
    if not isinstance(perf, dict):
        raise ValueError(f"{label} is missing performance metrics")
    measurement_samples = perf.get("measurement_samples")
    expected_samples = perf.get("expected_measurement_samples")
    expected_target_runs = perf.get("expected_target_runs")
    for name, value in (("measurement_samples", measurement_samples),
                        ("expected_measurement_samples", expected_samples),
                        ("expected_target_runs", expected_target_runs)):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{label} has invalid performance.{name}")
    if measurement_samples != expected_samples:
        raise ValueError(f"{label} has incomplete performance samples")
    if (expected_target_runs != expected_target_runs_from_configuration
            or expected_samples != expected_samples_from_configuration):
        raise ValueError(f"{label} performance sample counts do not match its run configuration")
    gpu = perf.get("interpolation_gpu_time_ms")
    if not isinstance(gpu, dict):
        raise ValueError(f"{label} is missing GPU timing coverage")
    gpu_count, gpu_missing = gpu.get("count"), gpu.get("missing_samples")
    if (isinstance(gpu_count, bool) or not isinstance(gpu_count, int) or gpu_count < 0
            or isinstance(gpu_missing, bool) or not isinstance(gpu_missing, int) or gpu_missing < 0
            or gpu_count + gpu_missing != expected_samples):
        raise ValueError(f"{label} has invalid GPU timing sample coverage")
    if gpu_count != expected_samples or gpu_missing != 0:
        raise ValueError(f"{label} has incomplete GPU timing sample coverage")
    for metric in ("completion_latency_ms", "interpolation_gpu_time_ms", "cpu_submit_overhead_ms"):
        distribution_value = perf.get(metric)
        if (not isinstance(distribution_value, dict)
                or isinstance(distribution_value.get("count"), bool)
                or distribution_value.get("count") != expected_samples):
            raise ValueError(f"{label} has incomplete performance.{metric} samples")
        for statistic in ("mean", "min", "p50", "p95", "p99", "max"):
            value = distribution_value.get(statistic)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value) or value <= 0):
                raise ValueError(f"{label} has invalid performance.{metric}.{statistic}")
    deadline = perf.get("deadline_ms")
    if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
            or not math.isfinite(deadline) or deadline <= 0
            or deadline != configuration.get("deadline_ms")):
        raise ValueError(f"{label} has invalid or inconsistent deadline")
    for field in ("gpu_memory_current_sample_count", "gpu_memory_sampled_max_sample_count",
                  "runner_resident_memory_sample_count"):
        count = perf.get(field)
        if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= expected_target_runs:
            raise ValueError(f"{label} has invalid performance.{field}")
    for count_field, missing_field, value_field in (
        ("gpu_memory_current_sample_count", "gpu_memory_current_missing_target_runs",
         "gpu_allocated_bytes_current_max"),
        ("gpu_memory_sampled_max_sample_count", "gpu_memory_sampled_max_missing_target_runs",
         "gpu_allocated_bytes_sampled_max"),
        ("runner_resident_memory_sample_count", "runner_resident_memory_missing_target_runs",
         "runner_peak_resident_bytes_max"),
    ):
        count, missing, value = perf[count_field], perf.get(missing_field), perf.get(value_field)
        if (isinstance(missing, bool) or not isinstance(missing, int)
                or missing != expected_target_runs - count):
            raise ValueError(f"{label} has inconsistent performance.{missing_field}")
        if ((count == 0) != (value is None)
                or (value is not None and
                    (isinstance(value, bool) or not isinstance(value, int) or value <= 0))):
            raise ValueError(f"{label} has inconsistent performance.{value_field}")
    source_only = perf.get("source_only_input_throughput_fps")
    source_with_generation = perf.get("source_with_generation_input_throughput_fps")
    if not isinstance(source_only, dict) or not isinstance(source_with_generation, dict):
        raise ValueError(f"{label} is missing source throughput sample coverage")
    source_only_count, generated_count = source_only.get("count"), source_with_generation.get("count")
    if (source_only_count != generated_count
            or source_only_count not in (0, expected_samples)):
        raise ValueError(f"{label} has inconsistent source throughput sample coverage")
    if (source_only_count == 0) != (perf.get("source_frame_fps_impact_percent") is None):
        raise ValueError(f"{label} has inconsistent source throughput impact availability")
    impact = perf.get("source_frame_fps_impact_percent")
    if impact is not None and (
            isinstance(impact, bool) or not isinstance(impact, (int, float))
            or not math.isfinite(impact)):
        raise ValueError(f"{label} has invalid source throughput impact")
    for distribution_value in (source_only, source_with_generation):
        count = distribution_value.get("count")
        if isinstance(count, bool) or not isinstance(count, int) or count != source_only_count:
            raise ValueError(f"{label} has invalid source throughput sample count")
        statistic_fields = ("mean", "p50", "p95", "p99", "max")
        if count == 0:
            if any(distribution_value.get(key) is not None for key in statistic_fields):
                raise ValueError(f"{label} has values for unavailable source throughput")
        else:
            for key in statistic_fields:
                value = distribution_value.get(key)
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or value <= 0):
                    raise ValueError(f"{label} has invalid source throughput {key}")
    misses, miss_rate = (perf.get("generated_frame_deadline_miss_count"),
                         perf.get("generated_frame_deadline_miss_rate"))
    if (isinstance(misses, bool) or not isinstance(misses, int)
            or not 0 <= misses <= measurement_samples
            or isinstance(miss_rate, bool) or not isinstance(miss_rate, (int, float))
            or not math.isfinite(miss_rate) or not 0 <= miss_rate <= 1
            or not math.isclose(miss_rate, misses / measurement_samples, rel_tol=1e-9, abs_tol=1e-12)):
        raise ValueError(f"{label} has inconsistent deadline-miss metrics")
    if source_only_count:
        expected_impact = (
            100.0 * (source_only["mean"] - source_with_generation["mean"])
            / source_only["mean"]
        )
        if not math.isclose(impact, expected_impact, rel_tol=1e-9, abs_tol=1e-9):
            raise ValueError(f"{label} has inconsistent source throughput impact")
    target_plan = result.get("configuration", {}).get("target_timeline")
    quality_frames = result.get("quality_frames")
    if not isinstance(target_plan, list) or not isinstance(quality_frames, list) or len(target_plan) != len(quality_frames):
        raise ValueError(f"{label} is missing per-target quality records")
    for frame, target in zip(quality_frames, target_plan):
        if (frame.get("source_pair"), frame.get("target_index"),
                frame.get("timestamp_ns"), frame.get("t"),
                frame.get("continuity_group")) != (
                target.get("source_pair"), target.get("target_index"),
                target.get("timestamp_ns"), target.get("t"),
                target.get("continuity_group")):
            raise ValueError(f"{label} per-target quality records do not match target timestamps")
        frame_regions = frame.get("regions")
        if not isinstance(frame_regions, dict) or set(frame_regions) != set(region_labels):
            raise ValueError(f"{label} target frame is missing a required quality region")
        pixel_count = width * height
        target_mask_counts = frame.get("mask_pixel_counts")
        if (not isinstance(target_mask_counts, dict)
                or set(target_mask_counts) != set(mask_labels)
                or any(isinstance(count, bool) or not isinstance(count, int)
                       or not 0 < count <= pixel_count
                       for count in target_mask_counts.values())):
            raise ValueError(f"{label} target frame has invalid mask pixel counts")
        target_index = target.get("target_index")
        if (target_index is not None
                and target_mask_counts != source_mask_coverage.get(str(target_index))):
            raise ValueError(f"{label} target mask pixel counts do not match source-frame coverage")
        critical_errors = frame.get("critical_pixel_errors")
        if not isinstance(critical_errors, dict) or set(critical_errors) != set(strict_pixel_labels):
            raise ValueError(f"{label} target frame is missing critical ROI pixel-error records")
        for roi_label, rows in critical_errors.items():
            if not isinstance(rows, list):
                raise ValueError(f"{label} has invalid critical pixel-error records for {roi_label}")
            previous_pixel = -1
            for row in rows:
                if (not isinstance(row, list) or len(row) != 4
                        or isinstance(row[0], bool) or not isinstance(row[0], int)
                        or not previous_pixel < row[0] < pixel_count
                        or any(isinstance(channel, bool) or not isinstance(channel, int)
                               or not 0 <= channel <= 255 for channel in row[1:])
                        or not any(row[1:])):
                    raise ValueError(f"{label} has invalid critical pixel-error record for {roi_label}")
                previous_pixel = row[0]
            account_strict_map(rows, f"{roi_label} target maps")
        for region in region_labels:
            values = frame_regions[region]
            for metric in ("frame_max_pixel_error_norm", "pixels_over_1pct_error_count",
                           "pixels_over_5pct_error_count", "psnr_db",
                           "pixel_abs_error_norm_mean", "pixel_abs_error_norm_p95",
                           "pixels_over_1pct_error_fraction", "pixels_over_5pct_error_fraction",
                           "ssim_7x7_masked_luminance", "perceptual_similarity",
                           "cielab_delta_e76_mean"):
                value = values.get(metric)
                if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"{label} target frame is missing {metric} for {region}")
                if metric in ("ssim_7x7_masked_luminance", "perceptual_similarity",
                              "pixels_over_1pct_error_fraction", "pixels_over_5pct_error_fraction") and not 0 <= value <= 1:
                    raise ValueError(f"{label} target frame has out-of-range {metric} for {region}")
                if metric == "psnr_db" and not 0 <= value <= 120:
                    raise ValueError(f"{label} target frame has out-of-range PSNR for {region}")
                if metric == "cielab_delta_e76_mean" and value < 0:
                    raise ValueError(f"{label} target frame has negative {metric} for {region}")
                if metric == "frame_max_pixel_error_norm" and not 0 <= value <= 1:
                    raise ValueError(f"{label} target frame has out-of-range pixel error for {region}")
                if metric in ("pixel_abs_error_norm_mean", "pixel_abs_error_norm_p95") and not 0 <= value <= 1:
                    raise ValueError(f"{label} target frame has out-of-range {metric} for {region}")
                if metric.endswith("_count") and (not isinstance(value, int) or value < 0):
                    raise ValueError(f"{label} target frame has invalid sparse-error count for {region}")
            active_count = values.get("active_pixel_count")
            expected_active_count = (
                pixel_count if region == "all" else target_mask_counts[region])
            if (isinstance(active_count, bool) or not isinstance(active_count, int)
                    or active_count != expected_active_count):
                raise ValueError(f"{label} target frame has invalid active pixel count for {region}")
            perfect_match = values.get("psnr_perfect_match")
            if not isinstance(perfect_match, bool):
                raise ValueError(f"{label} target frame has invalid perfect-match flag for {region}")
            if any(values[count_metric] > active_count for count_metric in (
                    "pixels_over_1pct_error_count", "pixels_over_5pct_error_count")):
                raise ValueError(f"{label} target frame sparse-error count exceeds mask area for {region}")
            edge = values.get("edge_location")
            if not isinstance(edge, dict):
                raise ValueError(f"{label} target frame is missing edge-location metrics for {region}")
            for metric in ("symmetric_chamfer_norm", "precision_at_1px", "recall_at_1px"):
                value = edge.get(metric)
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value) or not 0 <= value <= 1):
                    raise ValueError(f"{label} target frame has invalid {metric} for {region}")
        for roi_label, rows in critical_errors.items():
            roi_values = frame_regions[roi_label]
            active_count = roi_values["active_pixel_count"]
            if len(rows) > active_count:
                raise ValueError(f"{label} critical ROI error map exceeds its mask area for {roi_label}")
            errors = [sum(row[1:]) / (3 * 255.0) for row in rows]
            expected = _sparse_pixel_distribution(errors, active_count)
            target_fields = {
                "pixel_abs_error_norm_mean": expected["mean"],
                "pixel_abs_error_norm_p95": expected["p95"],
                "frame_max_pixel_error_norm": expected["max"],
                "pixels_over_1pct_error_count": sum(value >= 0.01 for value in errors),
                "pixels_over_5pct_error_count": sum(value >= 0.05 for value in errors),
            }
            target_fields["pixels_over_1pct_error_fraction"] = (
                target_fields["pixels_over_1pct_error_count"] / active_count)
            target_fields["pixels_over_5pct_error_fraction"] = (
                target_fields["pixels_over_5pct_error_count"] / active_count)
            for metric, expected_value in target_fields.items():
                actual_value = roi_values[metric]
                if (isinstance(actual_value, bool)
                        or not isinstance(actual_value, (int, float))
                        or not math.isfinite(actual_value)
                        or not math.isclose(actual_value, expected_value,
                                            rel_tol=1e-12, abs_tol=1e-12)):
                    raise ValueError(
                        f"{label} {roi_label} target {metric} does not match its sparse per-pixel map")
            perfect_match = roi_values["psnr_perfect_match"]
            expected_psnr = _sparse_rgb_map_psnr(rows, active_count)
            if (perfect_match != (not rows)
                    or not math.isclose(roi_values["psnr_db"], expected_psnr,
                                        rel_tol=1e-12, abs_tol=1e-12)):
                raise ValueError(
                    f"{label} {roi_label} target PSNR does not match its sparse per-pixel map")
    for region in region_labels:
        summary = quality_by_region[region]
        samples = [frame["regions"][region] for frame in quality_frames]
        if summary.get("sample_count") != len(samples):
            raise ValueError(f"{label} has an inconsistent quality sample count for {region}")
        for path, key in (
            ("psnr_db", "psnr_db"),
            ("ssim_7x7_masked_luminance", "ssim_7x7_masked_luminance"),
            ("perceptual_similarity", "perceptual_similarity"),
            ("cielab_delta_e76_mean", "cielab_delta_e76_mean"),
            ("pixel_abs_error_norm_mean_per_target", "pixel_abs_error_norm_mean"),
            ("pixel_abs_error_norm_p95_per_target", "pixel_abs_error_norm_p95"),
            ("frame_max_pixel_error_norm", "frame_max_pixel_error_norm"),
            ("pixels_over_1pct_error_fraction_per_target", "pixels_over_1pct_error_fraction"),
            ("pixels_over_1pct_error_count_per_target", "pixels_over_1pct_error_count"),
            ("pixels_over_5pct_error_fraction_per_target", "pixels_over_5pct_error_fraction"),
            ("pixels_over_5pct_error_count_per_target", "pixels_over_5pct_error_count"),
        ):
            _require_distribution_match(
                summary.get(path), distribution([frame[key] for frame in samples]),
                f"{label} {region}.{path}")
        for edge_path, edge_key in (
            ("symmetric_chamfer_norm", "symmetric_chamfer_norm"),
            ("precision_at_1px", "precision_at_1px"),
            ("recall_at_1px", "recall_at_1px"),
        ):
            _require_distribution_match(
                _nested(summary, f"edge_location.{edge_path}"),
                distribution([frame["edge_location"][edge_key] for frame in samples]),
                f"{label} {region}.edge_location.{edge_path}")
        for key, values in (
            ("worst_target_chamfer_norm", [frame["edge_location"]["symmetric_chamfer_norm"]
                                            for frame in samples]),
            ("minimum_target_precision_at_1px", [frame["edge_location"]["precision_at_1px"]
                                                   for frame in samples]),
            ("minimum_target_recall_at_1px", [frame["edge_location"]["recall_at_1px"]
                                                for frame in samples]),
        ):
            value = _nested(summary, f"edge_location.{key}")
            expected_value = (max(values) if key == "worst_target_chamfer_norm" else min(values))
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or not math.isclose(value, expected_value, rel_tol=1e-12, abs_tol=1e-12)):
                raise ValueError(f"{label} {region}.{key} does not match its target records")
    for metric, statistic, _, _ in RUNTIME_RULES:
        value = _nested(perf, f"{metric}.{statistic}")
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)):
            raise ValueError(f"{label} has invalid runtime metric {metric}.{statistic}")


def _validate_result(result: dict[str, Any], label: str) -> None:
    try:
        _validate_result_impl(result, label)
    except (AttributeError, IndexError, KeyError, TypeError, OverflowError,
            ZeroDivisionError, RecursionError) as error:
        raise ValueError(f"{label} contains a malformed result structure: {error}") from error


def _compare_values(base: float, candidate: float, direction: str, tolerance: float) -> bool:
    return candidate < base - tolerance if direction == "higher" else candidate > base + tolerance


def _sparse_map_union(baseline_rows: list, candidate_rows: list):
    """Merge two validated, sorted sparse maps without materializing dictionaries."""
    base_index = candidate_index = 0
    while base_index < len(baseline_rows) or candidate_index < len(candidate_rows):
        base_row = (baseline_rows[base_index]
                    if base_index < len(baseline_rows) else None)
        candidate_row = (candidate_rows[candidate_index]
                         if candidate_index < len(candidate_rows) else None)
        if candidate_row is None or (base_row is not None and base_row[0] < candidate_row[0]):
            pixel = base_row[0]
            old_values, new_values = tuple(base_row[1:]), (0,) * (len(base_row) - 1)
            base_index += 1
        elif base_row is None or candidate_row[0] < base_row[0]:
            pixel = candidate_row[0]
            old_values, new_values = (0,) * (len(candidate_row) - 1), tuple(candidate_row[1:])
            candidate_index += 1
        else:
            pixel = base_row[0]
            old_values, new_values = tuple(base_row[1:]), tuple(candidate_row[1:])
            base_index += 1
            candidate_index += 1
        yield pixel, old_values, new_values


def require_compatible(baseline: dict[str, Any], candidate: dict[str, Any]) -> None:
    if baseline.get("compatibility") != candidate.get("compatibility"):
        raise ValueError(
            "runs are incompatible: corpus content, target timestamps, host, "
            "iteration/deadline configuration, or metric parameters differ"
        )


def _compare(args: argparse.Namespace) -> int:
    baseline_path, candidate_path = Path(args.baseline), Path(args.candidate)
    baseline = json.loads(_read_bounded(baseline_path, MAX_RESULT_BYTES, "baseline result").decode("utf-8"),
                          parse_constant=_reject_json_constant)
    candidate = json.loads(_read_bounded(candidate_path, MAX_RESULT_BYTES, "candidate result").decode("utf-8"),
                           parse_constant=_reject_json_constant)
    _validate_result(baseline, "baseline")
    _validate_result(candidate, "candidate")
    require_compatible(baseline, candidate)
    region_labels = ("all", *baseline["corpus"]["mask_labels"])
    regressions, unavailable = [], []
    for region in region_labels:
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
        for region in region_labels:
            old_values = baseline_frame["regions"][region]
            new_values = candidate_frame["regions"][region]
            per_target_metrics = (
                ("frame_max_pixel_error_norm", "lower", 0.0),
                ("pixel_abs_error_norm_mean", "lower", 0.0),
                ("pixel_abs_error_norm_p95", "lower", 0.0),
                ("pixels_over_1pct_error_count", "lower", 0.0),
                ("pixels_over_5pct_error_count", "lower", 0.0),
                ("ssim_7x7_masked_luminance", "higher", 0.005),
                ("perceptual_similarity", "higher", 0.005),
                ("cielab_delta_e76_mean", "lower", 0.5),
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
        # Critical ROIs are compared pointwise. Improving one panel cannot
        # offset a new defect at a crosshair, sight, counter, or text glyph.
        for roi_label in baseline["corpus"]["strict_pixel_labels"]:
            worsened_pixel_count = worsened_channel_count = 0
            maximum_error_increase = 0
            worst = None
            locations_sample = []
            for pixel, baseline_channels, candidate_channels in _sparse_map_union(
                    baseline_frame["critical_pixel_errors"][roi_label],
                    candidate_frame["critical_pixel_errors"][roi_label]):
                channels = [(channel, baseline_channels[channel], candidate_channels[channel])
                            for channel in range(3)
                            if candidate_channels[channel] > baseline_channels[channel]]
                if channels:
                    worsened_pixel_count += 1
                    worsened_channel_count += len(channels)
                    local_worst = max(channels, key=lambda item: item[2] - item[1])
                    if (worst is None
                            or local_worst[2] - local_worst[1] > worst[2] - worst[1]):
                        worst = local_worst
                    maximum_error_increase = max(
                        maximum_error_increase,
                        *(new_value - old_value for _, old_value, new_value in channels))
                    if len(locations_sample) < 16:
                        locations_sample.append({
                            "x": pixel % baseline["corpus"]["width"],
                            "y": pixel // baseline["corpus"]["width"],
                            "channels": [
                                {"channel": ("red", "green", "blue")[channel],
                                 "baseline": old_value / 255.0,
                                 "candidate": new_value / 255.0}
                                for channel, old_value, new_value in channels
                            ],
                        })
            if worst is not None:
                worst_channel, worst_old, worst_new = worst
                regressions.append({
                    "scope": "critical_roi_pixel", "region": roi_label,
                    "target": target_id, "metric": "per_pixel_per_channel_absolute_rgb_error",
                    "direction": "lower", "tolerance": 0,
                    "channel": ("red", "green", "blue")[worst_channel],
                    "baseline": worst_old / 255.0, "candidate": worst_new / 255.0,
                    "worsened_pixel_count": worsened_pixel_count,
                    "worsened_channel_count": worsened_channel_count,
                    "maximum_error_increase": maximum_error_increase / 255.0,
                    "locations_sample": locations_sample,
                })
    # Temporal tails are also matched by actual timestamp windows. This catches
    # a newly shimmering interval even if another interval was already worse.
    for region in region_labels:
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
                if region in baseline["corpus"]["strict_pixel_labels"]:
                    map_fields = (
                        (("critical_pixel_errors", "first_difference_residual"),)
                        if collection == "frame_to_frame_residual" else
                        (("critical_pixel_errors", "color_flicker"),
                         ("critical_edge_errors", "edge_flicker"))
                    )
                    for field, metric_name in map_fields:
                        worsened_pixel_count = 0
                        maximum_error_increase = 0.0
                        worst = None
                        locations_sample = []
                        for pixel, old_values, new_values in _sparse_map_union(
                                old_row[field], new_row[field]):
                            old_value, new_value = old_values[0], new_values[0]
                            if new_value > old_value:
                                worsened_pixel_count += 1
                                if (worst is None
                                        or new_value - old_value > worst[2] - worst[1]):
                                    worst = (pixel, old_value, new_value)
                                maximum_error_increase = max(
                                    maximum_error_increase, new_value - old_value)
                                if len(locations_sample) < 16:
                                    locations_sample.append({
                                        "x": pixel % baseline["corpus"]["width"],
                                        "y": pixel // baseline["corpus"]["width"],
                                        "baseline": old_value, "candidate": new_value,
                                    })
                        if worst is not None:
                            regressions.append({
                                "scope": "critical_roi_temporal",
                                "region": region, "timestamps_ns": list(key[0]),
                                "metric": metric_name, "direction": "lower",
                                "tolerance": 0.0, "baseline": worst[1],
                                "candidate": worst[2],
                                "worsened_pixel_count": worsened_pixel_count,
                                "maximum_error_increase": maximum_error_increase,
                                "locations_sample": locations_sample,
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
    for field in ("gpu_allocated_bytes_current_max", "gpu_allocated_bytes_sampled_max",
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
    base_gpu = br["interpolation_gpu_time_ms"]
    candidate_gpu = cr["interpolation_gpu_time_ms"]
    if candidate_gpu["missing_samples"] > base_gpu["missing_samples"]:
        regressions.append({
            "scope": "runtime", "metric": "interpolation_gpu_time_missing_samples",
            "direction": "lower", "baseline": base_gpu["missing_samples"],
            "candidate": candidate_gpu["missing_samples"], "tolerance": 0,
        })
    for field in ("gpu_memory_current_sample_count", "gpu_memory_sampled_max_sample_count",
                  "runner_resident_memory_sample_count"):
        old, new = br[field], cr[field]
        if new < old:
            regressions.append({
                "scope": "runtime", "metric": field, "direction": "higher",
                "baseline": old, "candidate": new, "tolerance": 0,
            })
    old_misses = br["generated_frame_deadline_miss_count"]
    new_misses = cr["generated_frame_deadline_miss_count"]
    if new_misses > old_misses:
        regressions.append({
            "scope": "runtime", "metric": "generated_frame_deadline_miss_count",
            "direction": "lower", "baseline": old_misses,
            "candidate": new_misses, "tolerance": 0,
        })
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
    _write_json_bounded(output, result, "benchmark comparison")
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
    run.add_argument("--backend-timeout-seconds", type=float, default=600.0,
                     help="fail the full backend sequence session after this wall time (default: 600)")
    run.add_argument("--hud-mode", choices=("none", "explicit", "automatic"),
                     default="none", help="host-visible HUD handling mode")
    run.add_argument("--ui-source", choices=("previous", "current", "nearest"),
                     default="nearest",
                     help="endpoint UI source; nearest ties select the current frame")
    run.add_argument("--hud-debug", choices=(
        "disabled", "raw-mask", "stabilized-mask", "protected-regions",
        "interpolation-confidence", "final-composite"), default="disabled",
        help="optional debug output visualization")
    run.add_argument("--max-analysis-memory-mib", type=int,
                     default=DEFAULT_MAX_ANALYSIS_MEMORY_MIB,
                     help="reject corpora whose estimated analyzer working set exceeds this MiB budget")
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
    except (ValueError, OSError, KeyError, TypeError, OverflowError, RecursionError,
            subprocess.CalledProcessError, json.JSONDecodeError) as error:
        print(f"framegen benchmark: {error}", file=sys.stderr)
        if isinstance(error, subprocess.CalledProcessError) and error.stderr:
            print(error.stderr, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
