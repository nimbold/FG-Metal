#!/usr/bin/env python3
"""Observation-only analysis of accepted STEP 11D.3-BR6 traces.

This module reads run evidence and writes derived CSV/JSON/Markdown files. It
does not launch a process, alter a trace, validate storage, or certify a run.
The default CLI analyzes only the smoke and baselines accepted in the BR6
report. ``analyze_run(run_dir)`` is reusable for later, independently accepted
runs that use the same trace schema.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import hashlib
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


HERE = Path(__file__).resolve().parent
REPOSITORY_ROOT = HERE.parents[1]
RUNS_ROOT = HERE / "runs"
DEFAULT_OUTPUT = HERE / "evidence" / "br7" / "analysis"
BR6_REPORT = HERE / "STEP-11D3-BR6-REPORT.md"

ACCEPTED_RUNS = (
    ("step11d3b-smoke-20261010-053718-a78894d0", "official_smoke"),
    ("step11d3b-baseline-01-20261009-223854-cfea0560", "baseline"),
    ("step11d3b-baseline-02-20261010-054210-3a16e5aa", "baseline"),
    ("step11d3b-baseline-03-20261010-062321-91bb8f4d", "baseline"),
)

WINDOWS_NS = (1_000_000_000, 5_000_000_000)
TELEMETRY_STALE_AFTER_NS = 2_000_000_000
TERMINAL_CALLBACK_CENSOR_NS = 1_000_000_000
OUTCOME_ENUMS = (
    "DISPLAYED_VALID_PRESENTED_TIME", "CALLBACK_PRESENTED_TIME_ZERO",
    "SUPERSEDED_BEFORE_DISPLAY", "NO_PRESENT_CALLBACK", "SWAPCHAIN_REPLACED",
    "WINDOW_NOT_PRESENTABLE", "UNKNOWN",
)
HISTOGRAM_BOUNDS_NS = (
    (0, 500_000, "<0.5 ms"),
    (500_000, 2_000_000, "0.5–2 ms"),
    (2_000_000, 5_000_000, "2–5 ms"),
    (5_000_000, 10_000_000, "5–10 ms"),
    (10_000_000, 14_000_000, "10–14 ms"),
    (14_000_000, 18_000_000, "14–18 ms"),
    (18_000_000, 25_000_000, "18–25 ms"),
    (25_000_000, 50_000_000, "25–50 ms"),
    (50_000_000, 100_000_000, "50–100 ms"),
    (100_000_000, 250_000_000, "100–250 ms"),
    (250_000_000, 500_000_000, "250–500 ms"),
    (500_000_000, 900_000_000, "500–900 ms"),
    (900_000_000, None, "≥900 ms"),
)

HOST_RATE_EVENTS = {
    "acquisition": ("vkAcquireNextImageKHR_end", "WINE_SOURCE", "acquire_success"),
    "failed_acquisition_attempt": ("vkAcquireNextImageKHR_end", "WINE_SOURCE", "failure"),
    "submit": ("vkQueueSubmit2_end", "WINE_SOURCE", "success"),
    "request": ("present_request", "MVK_BOUNDARY", "all"),
    "callback": ("presented_callback", "LAYER_SOURCE", "all"),
    "displayed_callback": ("presented_callback", "LAYER_SOURCE", "displayed"),
    "zero": ("presented_callback", "LAYER_SOURCE", "zero"),
    "availability": ("availability_after_presented_handler", "LAYER_SOURCE", "all"),
    "release": ("drawable_release", "LAYER_SOURCE", "all"),
}
APP_RATE_EVENTS = {
    "acquisition": ("app_vkAcquireNextImageKHR_end", "acquire_success"),
    "failed_acquisition_attempt": ("app_vkAcquireNextImageKHR_end", "failure"),
    "submit": ("app_vkQueueSubmit2_end", "success"),
    "request": ("app_vkQueuePresentKHR_end", "success"),
}

TELEMETRY_FIELDS = (
    "trigger", "notification_name", "telemetry_sequence", "layer_id", "window_id", "window_associated",
    "window_visible", "window_key", "window_main", "window_occluded",
    "window_occlusion_state", "window_miniaturized", "app_active", "window_alpha",
    "window_ordered", "window_level", "window_frame_x", "window_frame_y",
    "window_frame_width", "window_frame_height", "screen_id", "screen_visible_frame_x",
    "screen_visible_frame_y", "screen_visible_frame_width", "screen_visible_frame_height",
    "screen_backing_scale", "window_backing_scale_factor", "screen_refresh_hz", "screen_refresh_available",
    "window_visible_intersection_x", "window_visible_intersection_y",
    "window_visible_intersection_width", "window_visible_intersection_height",
    "window_visible_intersection_area", "layer_bounds_x", "layer_bounds_y",
    "layer_bounds_width", "layer_bounds_height", "drawable_width", "drawable_height",
    "contents_scale", "maximum_drawable_count", "allows_next_drawable_timeout",
    "display_sync_enabled", "pixel_format", "framebuffer_only", "layer_opaque",
    "layer_hidden", "layer_masks_to_bounds",
)


class AnalysisError(ValueError):
    """Raised when evidence ownership, completeness, or identity is ambiguous."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"cannot read JSON evidence {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise AnalysisError(f"expected a JSON object in {path}")
    return value


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    try:
        with path.open(encoding="utf-8") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise AnalysisError(f"malformed JSONL at {path}:{line_no}: {exc}") from exc
                if not isinstance(row, dict):
                    raise AnalysisError(f"non-object JSONL row at {path}:{line_no}")
                rows.append(row)
    except OSError as exc:
        raise AnalysisError(f"cannot read trace {path}: {exc}") from exc
    return rows


def _event_rows(rows: Iterable[Mapping[str, Any]], event: str,
                origin: str | None = None) -> list[Mapping[str, Any]]:
    return [row for row in rows
            if row.get("event") == event and (origin is None or row.get("origin") == origin)]


def _num(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _success(row: Mapping[str, Any]) -> bool:
    result = row.get("vk_result")
    return result == 0 or result == "VK_SUCCESS"


def _acquire_success(row: Mapping[str, Any]) -> bool:
    """Vulkan acquire can return an image with SUCCESS or SUBOPTIMAL_KHR."""
    result = row.get("vk_result")
    return result in (0, 1000001003, "VK_SUCCESS", "VK_SUBOPTIMAL_KHR")


def _acquire_failed(row: Mapping[str, Any]) -> bool:
    result = row.get("vk_result")
    if isinstance(result, int) and not isinstance(result, bool):
        return not _acquire_success(row)
    return isinstance(result, str) and result.startswith("VK_ERROR_")


def _api_success(event: str, row: Mapping[str, Any]) -> bool:
    return _acquire_success(row) if event in ("vkAcquireNextImageKHR_end", "app_vkAcquireNextImageKHR_end") \
        else _success(row)


def _median(values: Sequence[int | float]) -> float | None:
    if not values:
        return None
    return float(statistics.median(values))


def _percentile(values: Sequence[int | float], quantile: float) -> float | None:
    """Linear interpolation at (n-1)*q, matching BR6's reported convention."""
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    if len(ordered) == 1:
        return ordered[0]
    offset = (len(ordered) - 1) * quantile
    lower = math.floor(offset)
    upper = math.ceil(offset)
    if lower == upper:
        return ordered[lower]
    fraction = offset - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def duration_stats(values: Sequence[int | float]) -> dict[str, Any]:
    vals = [float(value) for value in values]
    if not vals:
        return {"count": 0, "min_ns": None, "mean_ns": None, "p50_ns": None,
                "p90_ns": None, "p95_ns": None, "p99_ns": None,
                "p99_9_ns": None, "max_ns": None}
    return {
        "count": len(vals), "min_ns": min(vals), "mean_ns": sum(vals) / len(vals),
        "p50_ns": _percentile(vals, 0.5), "p90_ns": _percentile(vals, 0.9),
        "p95_ns": _percentile(vals, 0.95), "p99_ns": _percentile(vals, 0.99),
        "p99_9_ns": _percentile(vals, 0.999), "max_ns": max(vals),
    }


def value_stats(values: Sequence[int | float], unit: str) -> dict[str, Any]:
    vals = [float(value) for value in values]
    if not vals:
        return {"count": 0, f"min_{unit}": None, f"mean_{unit}": None,
                f"p50_{unit}": None, f"p90_{unit}": None, f"p95_{unit}": None,
                f"p99_{unit}": None, f"p99_9_{unit}": None, f"max_{unit}": None}
    return {
        "count": len(vals), f"min_{unit}": min(vals), f"mean_{unit}": sum(vals) / len(vals),
        f"p50_{unit}": _percentile(vals, 0.5), f"p90_{unit}": _percentile(vals, 0.9),
        f"p95_{unit}": _percentile(vals, 0.95), f"p99_{unit}": _percentile(vals, 0.99),
        f"p99_9_{unit}": _percentile(vals, 0.999), f"max_{unit}": max(vals),
    }


def duration_histogram(values: Sequence[int | float]) -> list[dict[str, Any]]:
    total = len(values)
    result: list[dict[str, Any]] = []
    for lower, upper, label in HISTOGRAM_BOUNDS_NS:
        selected = [float(value) for value in values
                    if value >= lower and (upper is None or value < upper)]
        result.append({
            "bucket": label, "lower_ns": lower, "upper_exclusive_ns": upper,
            "count": len(selected), "percent": (100.0 * len(selected) / total) if total else None,
            "accumulated_ns": sum(selected),
        })
    return result


def _resolve_trace_path(run_dir: Path, raw_path: Any, description: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path:
        raise AnalysisError(f"{description} path is missing")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = run_dir / candidate
    try:
        resolved = candidate.resolve(strict=True)
        run_root = run_dir.resolve(strict=True)
    except OSError as exc:
        raise AnalysisError(f"cannot resolve {description} path {candidate}: {exc}") from exc
    if not resolved.is_relative_to(run_root):
        raise AnalysisError(f"{description} path escapes run directory: {resolved}")
    if not resolved.is_file():
        raise AnalysisError(f"{description} path is not a file: {resolved}")
    return resolved


def _load_owned_inputs(run_dir: Path) -> tuple[dict[str, Any], dict[str, Any],
                                                   list[dict[str, Any]], list[dict[str, Any]],
                                                   Path, Path, int]:
    run_dir = run_dir.resolve(strict=True)
    result_path = run_dir / "run-result.json"
    index_path = run_dir / "trace-index.json"
    manifest_path = run_dir / "run-manifest.json"
    result = _read_json(result_path)
    index = _read_json(index_path)
    manifest = _read_json(manifest_path)

    if result.get("status") != "PASS" or result.get("is_valid") is not True:
        raise AnalysisError("run-result must report status=PASS and is_valid=true")
    if index.get("errors") != []:
        raise AnalysisError("trace-index contains errors or has no explicit empty error list")
    processes = index.get("processes")
    if not isinstance(processes, list):
        raise AnalysisError("trace-index.processes is missing")
    target_processes = [p for p in processes if isinstance(p, dict) and p.get("trace_role") == "target"]
    if len(target_processes) != 1:
        raise AnalysisError(f"expected exactly one target process record, found {len(target_processes)}")
    target = target_processes[0]
    target_pid = _num(target.get("target_unix_pid"))
    host_pid = _num(target.get("host_pid"))
    if target_pid is None or host_pid != target_pid:
        raise AnalysisError("target trace host_pid and target_unix_pid must identify the same process")
    if target.get("parse_status") != "valid" or target.get("ownership_valid") is not True:
        raise AnalysisError("target trace is not marked valid and ownership-checked")
    if target.get("summary", {}).get("dropped_count") != 0:
        raise AnalysisError("target host trace reports dropped events")

    app_path = _resolve_trace_path(run_dir, result.get("app_trace_path"), "app trace")
    indexed_host_path = _resolve_trace_path(run_dir, target.get("path"), "indexed target host trace")
    host_path = _resolve_trace_path(run_dir, result.get("host_trace_path"), "run-result host trace")
    if indexed_host_path != host_path:
        raise AnalysisError("run-result host trace path does not match the owned target trace-index path")
    if manifest.get("app_trace_path_host"):
        manifest_app_path = _resolve_trace_path(run_dir, manifest.get("app_trace_path_host"), "manifest app trace")
        if manifest_app_path != app_path:
            raise AnalysisError("run-manifest app trace path does not match run-result")

    app = _read_jsonl(app_path)
    host = _read_jsonl(host_path)
    app_configs = _event_rows(app, "app_configuration", "APP_CONFIG")
    if len(app_configs) != 1:
        raise AnalysisError(f"expected exactly one APP_CONFIG row, found {len(app_configs)}")
    config = app_configs[0]
    if _num(config.get("target_unix_pid")) != target_pid:
        raise AnalysisError("app_configuration target_unix_pid does not match owned host trace PID")
    for line_no, row in enumerate(host, 1):
        if row.get("trace_role") != "target":
            raise AnalysisError(f"non-target row found in owned target trace at line {line_no}")
        if _num(row.get("host_pid")) != target_pid or _num(row.get("target_unix_pid")) != target_pid:
            raise AnalysisError(f"PID ownership mismatch in target trace at line {line_no}")
    clock_rows = _event_rows(host, "clock_metadata", "INSTRUMENTATION")
    summary_rows = _event_rows(host, "trace_summary", "LOGGER")
    if len(clock_rows) != 1 or len(summary_rows) != 1:
        raise AnalysisError("target host trace must contain exactly one clock_metadata and trace_summary")
    if summary_rows[0].get("dropped_count") != 0 or summary_rows[0].get("write_failure_count") != 0:
        raise AnalysisError("target host trace summary reports drops or write failures")
    app_summary_rows = _event_rows(app, "app_configuration", "APP_CONFIG")
    if app_summary_rows[0].get("trace_events_dropped") not in (0, None):
        raise AnalysisError("app trace configuration reports dropped events")

    expected_target_path = result.get("host_trace_path")
    if isinstance(expected_target_path, str) and Path(expected_target_path).name != host_path.name:
        raise AnalysisError("host trace path basename differs from run-result")
    return result, index, app, host, app_path, host_path, target_pid


def _rows_by_sequence(rows: Iterable[Mapping[str, Any]], event: str, origin: str,
                      sequence_field: str) -> dict[int, list[Mapping[str, Any]]]:
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in _event_rows(rows, event, origin):
        sequence = _num(row.get(sequence_field))
        if sequence is not None and sequence > 0:
            grouped[sequence].append(row)
    return grouped


def _same_value(left: Mapping[str, Any], right: Mapping[str, Any], key: str) -> bool:
    return key in left and key in right and left[key] is not None and left[key] == right[key]


def _same_image(left: Mapping[str, Any], right: Mapping[str, Any],
                *, left_swapchain: str = "swapchain_id",
                right_swapchain: str = "swapchain_id") -> bool:
    return all((left.get(a) is not None and right.get(b) is not None and left[a] == right[b])
               for a, b in ((left_swapchain, right_swapchain), ("image_index", "image_index"),
                            ("image_id", "image_id")))


def _callback_image_matches_request(callback: Mapping[str, Any], request: Mapping[str, Any]) -> bool:
    return all((callback.get(left) is not None and request.get(right) is not None
                and callback[left] == request[right])
               for left, right in (("present_swapchain_id", "swapchain_id"),
                                   ("present_image_index", "image_index"),
                                   ("present_image_id", "image_id"),
                                   ("driver_submit_seq", "driver_submit_seq"),
                                   ("driver_present_seq", "driver_present_seq")))


def _lifecycle_identity_matches(row: Mapping[str, Any], callback: Mapping[str, Any]) -> bool:
    keys = ("drawable_id", "drawable_identity", "texture_id", "acquisition_sequence",
            "swapchain_id", "image_index", "image_id", "driver_submit_seq", "driver_present_seq")
    return all(key in row and key in callback and row[key] is not None and row[key] == callback[key]
               for key in keys)


def _app_event_groups(app: Sequence[Mapping[str, Any]], event: str,
                      result_required: bool = True) -> dict[int, list[Mapping[str, Any]]]:
    grouped: dict[int, list[Mapping[str, Any]]] = defaultdict(list)
    for row in _event_rows(app, event, "APP_SOURCE"):
        sequence = _num(row.get("app_acquire_seq"))
        if sequence is not None and sequence > 0 and (not result_required or _success(row)):
            grouped[sequence].append(row)
    return grouped


def _extract_frames(app: Sequence[Mapping[str, Any]], host: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    acquisition_all = _rows_by_sequence(host, "vkAcquireNextImageKHR_end", "MVK_BOUNDARY", "acquisition_sequence")
    acquisition = {sequence: [row for row in rows if _acquire_success(row)]
                   for sequence, rows in acquisition_all.items()}
    acquisition = {sequence: rows for sequence, rows in acquisition.items() if rows}
    submit = _rows_by_sequence(host, "vkQueueSubmit2_end", "MVK_BOUNDARY", "acquisition_sequence")
    next_drawable = _rows_by_sequence(host, "next_drawable_end", "LAYER_SOURCE", "acquisition_sequence")
    requests = _rows_by_sequence(host, "present_request", "MVK_BOUNDARY", "acquisition_sequence")
    handlers = _rows_by_sequence(host, "presented_handler_registered", "LAYER_SOURCE", "acquisition_sequence")
    callbacks = _rows_by_sequence(host, "presented_callback", "LAYER_SOURCE", "acquisition_sequence")
    availability = _rows_by_sequence(host, "availability_after_presented_handler", "LAYER_SOURCE", "acquisition_sequence")
    releases = _rows_by_sequence(host, "drawable_release", "LAYER_SOURCE", "acquisition_sequence")
    app_acquires = _app_event_groups(app, "app_vkAcquireNextImageKHR_end", result_required=False)
    app_acquires = {sequence: [row for row in rows if _acquire_success(row)]
                    for sequence, rows in app_acquires.items()}
    app_acquires = {sequence: rows for sequence, rows in app_acquires.items() if rows}
    app_submits = _app_event_groups(app, "app_vkQueueSubmit2_end")
    app_presents = _app_event_groups(app, "app_vkQueuePresentKHR_end")
    duration_maps = {
        event: _host_duration_map(host, event)
        for event in ("vkAcquireNextImageKHR_end", "vkQueueSubmit2_end", "vkQueuePresentKHR_end")
    }
    summaries = _event_rows(host, "trace_summary", "LOGGER")
    trace_end_ns = _num(summaries[0].get("ts_mach_ns")) if len(summaries) == 1 \
        and summaries[0].get("dropped_count") == 0 \
        and summaries[0].get("write_failure_count") == 0 else None

    sequence_ids = sorted(acquisition)
    frames: list[dict[str, Any]] = []
    for sequence in sequence_ids:
        acquire_rows = acquisition[sequence]
        frame_acquire = acquire_rows[0] if len(acquire_rows) == 1 else {}
        submit_rows = submit.get(sequence, [])
        drawable_rows = [row for row in next_drawable.get(sequence, []) if row.get("detail") == "acquired"]
        request_rows = requests.get(sequence, [])
        handler_rows = handlers.get(sequence, [])
        callback_rows = callbacks.get(sequence, [])
        availability_rows = availability.get(sequence, [])
        release_rows = releases.get(sequence, [])
        app_acquire_rows = app_acquires.get(sequence, [])
        app_submit_rows = app_submits.get(sequence, [])
        app_present_rows = app_presents.get(sequence, [])
        reasons: list[str] = []

        cardinalities = {
            "acquire": len(acquire_rows), "submit": len(submit_rows),
            "next_drawable": len(drawable_rows), "request": len(request_rows),
            "handler": len(handler_rows), "callback": len(callback_rows),
            "availability": len(availability_rows), "release": len(release_rows),
            "app_acquire": len(app_acquire_rows), "app_submit": len(app_submit_rows),
            "app_request": len(app_present_rows),
        }
        for label, count in cardinalities.items():
            if count != 1:
                reasons.append(f"{label}_count_{count}")

        one = lambda rows: rows[0] if len(rows) == 1 else {}
        sub = one(submit_rows)
        draw = one(drawable_rows)
        request = one(request_rows)
        handler = one(handler_rows)
        callback = one(callback_rows)
        available = one(availability_rows)
        release = one(release_rows)
        app_acquire = one(app_acquire_rows)
        app_submit = one(app_submit_rows)
        app_present = one(app_present_rows)

        if acquire_rows and not all(_acquire_success(row) for row in acquire_rows):
            reasons.append("acquire_not_success")
        if submit_rows and not all(_success(row) for row in submit_rows):
            reasons.append("submit_not_success")
        if app_acquire_rows and not all(_acquire_success(row) for row in app_acquire_rows):
            reasons.append("app_acquire_not_success")
        if app_submit_rows and not all(_success(row) for row in app_submit_rows):
            reasons.append("app_submit_not_success")
        if app_present_rows and not all(_success(row) for row in app_present_rows):
            reasons.append("app_request_not_success")

        if frame_acquire and sub and not _same_image(frame_acquire, sub):
            reasons.append("acquire_submit_identity_mismatch")
        if frame_acquire and draw and not _same_image(frame_acquire, draw):
            reasons.append("acquire_drawable_identity_mismatch")
        if frame_acquire and request and not _same_image(frame_acquire, request):
            reasons.append("acquire_request_identity_mismatch")
        if sub and request:
            if sub.get("driver_submit_seq") is None or sub.get("driver_submit_seq") != request.get("driver_submit_seq"):
                reasons.append("submit_request_sequence_mismatch")
        if callback and request and not _callback_image_matches_request(callback, request):
            reasons.append("request_callback_identity_mismatch")
        if callback and handler and not _lifecycle_identity_matches(handler, callback):
            reasons.append("handler_callback_identity_mismatch")
        if callback and available and not _lifecycle_identity_matches(available, callback):
            reasons.append("callback_availability_identity_mismatch")
        if callback and release and not _lifecycle_identity_matches(release, callback):
            reasons.append("callback_release_identity_mismatch")
        if frame_acquire and app_acquire:
            if frame_acquire.get("image_index") != app_acquire.get("image_index"):
                reasons.append("app_native_image_index_mismatch")
            if frame_acquire.get("image_id") != app_acquire.get("image_id"):
                reasons.append("app_native_image_id_mismatch")
        if app_acquire and app_submit:
            if app_acquire.get("app_frame_id") != app_submit.get("app_frame_id"):
                reasons.append("app_acquire_submit_frame_mismatch")
            if app_acquire.get("image_id") != app_submit.get("image_id"):
                reasons.append("app_acquire_submit_image_mismatch")
        if app_acquire and app_present:
            if app_acquire.get("app_frame_id") != app_present.get("app_frame_id"):
                reasons.append("app_acquire_request_frame_mismatch")
            if app_acquire.get("image_id") != app_present.get("image_id"):
                reasons.append("app_acquire_request_image_mismatch")

        exact_chain = not reasons
        outcome = "ambiguous"
        terminal_censored = False
        if exact_chain:
            presented_ns = _num(callback.get("presented_time_mach_ns"))
            displayed = callback.get("is_displayed")
            if presented_ns == 0 and displayed is False:
                outcome = "zero"
            elif presented_ns is not None and presented_ns > 0 and displayed is True:
                outcome = "presented"
            else:
                reasons.append("callback_display_state_inconsistent_or_missing")
                exact_chain = False

        # A missing callback is reportable only when the acquisition through
        # request is itself a unique successful chain and the trace is known to
        # have remained open for at least one second after the request. A
        # request near trace termination is terminally censored and UNKNOWN.
        request_prefix_labels = ("acquire", "submit", "next_drawable", "request",
                                 "app_acquire", "app_submit", "app_request")
        request_prefix_counts_ok = all(cardinalities[label] == 1 for label in request_prefix_labels)
        request_prefix_identity_ok = not any(reason in {
            "acquire_submit_identity_mismatch", "acquire_drawable_identity_mismatch",
            "acquire_request_identity_mismatch", "submit_request_sequence_mismatch",
            "app_native_image_index_mismatch", "app_native_image_id_mismatch",
            "app_acquire_submit_frame_mismatch", "app_acquire_submit_image_mismatch",
            "app_acquire_request_frame_mismatch", "app_acquire_request_image_mismatch",
        } for reason in reasons)
        request_prefix_success = (_acquire_success(frame_acquire) and _success(sub)
                                  and _acquire_success(app_acquire) and _success(app_submit)
                                  and _success(app_present))
        request_stamp = _num(request.get("ts_mach_ns"))
        trace_tail_ns = trace_end_ns - request_stamp if trace_end_ns is not None \
            and request_stamp is not None and trace_end_ns >= request_stamp else None
        no_callback_candidate = (not callback_rows and request_prefix_counts_ok
                                 and request_prefix_identity_ok and request_prefix_success
                                 and trace_tail_ns is not None)
        if not exact_chain and no_callback_candidate:
            reasons = [reason for reason in reasons if reason not in (
                "handler_count_0", "callback_count_0", "availability_count_0", "release_count_0")]
            if trace_tail_ns >= TERMINAL_CALLBACK_CENSOR_NS:
                outcome = "no_present_callback"
            else:
                outcome = "unknown"
                terminal_censored = True

        frame: dict[str, Any] = {
            "acquisition_sequence": sequence,
            "app_frame_id": app_acquire.get("app_frame_id"),
            "image_index": frame_acquire.get("image_index"),
            "image_id": frame_acquire.get("image_id"),
            "native_swapchain_id": frame_acquire.get("swapchain_id"),
            "drawable_id": callback.get("drawable_id"),
            "drawable_identity": callback.get("drawable_identity"),
            "texture_id": callback.get("texture_id"),
            "host_acquire_ts_mach_ns": frame_acquire.get("ts_mach_ns"),
            "host_submit_ts_mach_ns": sub.get("ts_mach_ns"),
            "request_ts_mach_ns": request.get("ts_mach_ns"),
            "callback_ts_mach_ns": callback.get("ts_mach_ns"),
            "presented_time_mach_ns": callback.get("presented_time_mach_ns"),
            "availability_ts_mach_ns": available.get("ts_mach_ns"),
            "release_ts_mach_ns": release.get("ts_mach_ns"),
            "next_drawable_duration_ns": draw.get("duration_ns"),
            "host_acquire_duration_ns": duration_maps["vkAcquireNextImageKHR_end"].get(sequence),
            "host_submit_duration_ns": duration_maps["vkQueueSubmit2_end"].get(sequence),
            "host_present_duration_ns": duration_maps["vkQueuePresentKHR_end"].get(sequence),
            "app_acquire_duration_ns": app_acquire.get("duration_ns"),
            "app_submit_duration_ns": app_submit.get("duration_ns"),
            "app_present_duration_ns": app_present.get("duration_ns"),
            "event_counts": cardinalities,
            "exact_chain": exact_chain,
            "outcome_category": outcome if exact_chain or outcome in ("no_present_callback", "unknown") else "ambiguous",
            "outcome_enum": {
                "presented": "DISPLAYED_VALID_PRESENTED_TIME",
                "zero": "CALLBACK_PRESENTED_TIME_ZERO",
                "no_present_callback": "NO_PRESENT_CALLBACK",
                "unknown": "UNKNOWN",
            }.get(outcome, "UNKNOWN"),
            "terminal_censored": terminal_censored,
            "ambiguity_reasons": reasons,
            "state_correlation": None,
        }
        frames.append(frame)
    return frames


def _host_duration_map(host: Sequence[Mapping[str, Any]], event: str) -> dict[int, int]:
    boundaries = _rows_by_sequence(host, event, "MVK_BOUNDARY", "acquisition_sequence")
    wrappers_by_key: dict[tuple[Any, ...], list[Mapping[str, Any]]] = defaultdict(list)
    for row in _event_rows(host, event, "WINE_SOURCE"):
        if _api_success(event, row):
            key = (row.get("call_begin_mach_ns"), row.get("call_end_mach_ns"),
                   row.get("image_index"), row.get("image_id"))
            wrappers_by_key[key].append(row)
    result: dict[int, int] = {}
    for sequence, rows in boundaries.items():
        if len(rows) != 1 or not _api_success(event, rows[0]):
            continue
        row = rows[0]
        key = (row.get("call_begin_mach_ns"), row.get("call_end_mach_ns"),
               row.get("image_index"), row.get("image_id"))
        matches = wrappers_by_key.get(key, [])
        if len(matches) == 1 and _num(matches[0].get("duration_ns")) is not None:
            result[sequence] = int(matches[0]["duration_ns"])
    return result


def _app_duration_by_sequence(app: Sequence[Mapping[str, Any]], event: str) -> dict[int, int]:
    # Added as a separate mapping because the APP_SOURCE API end event always
    # carries app_acquire_seq; it is the authoritative per-frame app duration.
    grouped = _app_event_groups(app, event)
    return {seq: _num(rows[0].get("duration_ns")) for seq, rows in grouped.items()
            if len(rows) == 1 and _num(rows[0].get("duration_ns")) is not None}


def _host_api_durations(host: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    return [row for row in _event_rows(host, event, "WINE_SOURCE")
            if _api_success(event, row) and _num(row.get("duration_ns")) is not None]


def _event_time(row: Mapping[str, Any], clock: str) -> int | None:
    return _num(row.get("ts_mach_ns" if clock == "host" else "ts_qpc_ns"))


def _accepted_count(row: Mapping[str, Any], accepted: str) -> bool:
    if accepted == "all":
        return True
    if accepted == "success":
        return _success(row)
    if accepted == "acquire_success":
        return _acquire_success(row)
    if accepted == "failure":
        return _acquire_failed(row)
    if accepted == "zero":
        return _num(row.get("presented_time_mach_ns")) == 0
    if accepted == "displayed":
        return row.get("is_displayed") is True and (_num(row.get("presented_time_mach_ns")) or 0) > 0
    return False


def _make_windows(rows: Sequence[Mapping[str, Any]], origin: int, end: int, window_ns: int,
                  *, stream: str) -> list[dict[str, Any]]:
    duration_ns = max(0, end - origin)
    metrics: dict[str, list[Mapping[str, Any]]] = {name: [] for name in
        (HOST_RATE_EVENTS if stream == "host" else APP_RATE_EVENTS)}
    event_specs = HOST_RATE_EVENTS if stream == "host" else APP_RATE_EVENTS
    for name, spec in event_specs.items():
        event, *rest = spec
        origin_name = rest[0] if stream == "host" else "APP_SOURCE"
        accepted = rest[1] if stream == "host" else rest[0]
        for row in _event_rows(rows, event, origin_name):
            if _accepted_count(row, accepted):
                metrics[name].append(row)

    measured_stamps = [_event_time(row, "host" if stream == "host" else "app")
                       for metric_rows in metrics.values() for row in metric_rows]
    max_delta = max((max(0, stamp - origin) for stamp in measured_stamps if stamp is not None), default=0)
    window_count = max(1, max_delta // window_ns + 1)

    counts_by_window: dict[str, Counter[int]] = {name: Counter() for name in event_specs}
    timing_by_window: dict[str, dict[int, list[int]]] = defaultdict(lambda: defaultdict(list))
    timing_specs = ([("next_drawable", "next_drawable_end", "LAYER_SOURCE"),
                     ("host_submit", "vkQueueSubmit2_end", "WINE_SOURCE")]
                    if stream == "host" else [])
    for label, event, source in timing_specs:
        for row in _event_rows(rows, event, source):
            duration = _num(row.get("duration_ns"))
            stamp = _event_time(row, "host")
            if duration is None or stamp is None:
                continue
            if event == "next_drawable_end" and row.get("detail") != "acquired":
                continue
            if event == "vkQueueSubmit2_end" and not _success(row):
                continue
            index = max(0, stamp - origin) // window_ns
            timing_by_window[label][index].append(duration)
    clock_name = "host" if stream == "host" else "app"
    for name, metric_rows in metrics.items():
        for row in metric_rows:
            stamp = _event_time(row, clock_name)
            if stamp is None:
                continue
            delta = max(0, stamp - origin)
            row_index = delta // window_ns
            if row_index >= window_count:
                row_index = window_count - 1
            counts_by_window[name][row_index] += 1

    bins: list[dict[str, Any]] = []
    for index in range(window_count):
        offset = index * window_ns
        # A trace's final event is an observed endpoint. If it falls exactly
        # on a window boundary, retain a full nominal window for the endpoint.
        exposure_ns = min(window_ns, max(0, duration_ns - offset))
        if exposure_ns == 0 and index == window_count - 1:
            exposure_ns = window_ns
        item: dict[str, Any] = {
            "stream": stream, "window_ns": window_ns, "window_index": index,
            "start_elapsed_ns": offset, "end_elapsed_ns": offset + exposure_ns,
            "observed_exposure_ns": exposure_ns,
        }
        for name in event_specs:
            count = counts_by_window[name][index]
            item[f"{name}_count"] = count
            item[f"{name}_rate_per_observed_second"] = (
                count * 1_000_000_000 / exposure_ns if exposure_ns else None)
        if stream == "host":
            callback_count = item.get("callback_count", 0)
            item["displayed_callback_count"] = item.get("displayed_callback_count", 0)
            item["displayed_callback_rate_per_observed_second"] = (
                item["displayed_callback_count"] * 1_000_000_000 / exposure_ns if exposure_ns else None)
            item["zero_percent_of_callbacks"] = (
                100.0 * item.get("zero_count", 0) / callback_count if callback_count else None)
            item["callback_observed_count"] = callback_count
            for label, _event, _source in timing_specs:
                samples = timing_by_window[label].get(index, [])
                item[f"{label}_p50_ns"] = _percentile(samples, 0.5)
                item[f"{label}_p99_ns"] = _percentile(samples, 0.99)
                item[f"{label}_max_ns"] = max(samples) if samples else None
        bins.append(item)
    return bins


def _enrich_outcome_windows(windows: Sequence[dict[str, Any]], frames: Sequence[Mapping[str, Any]],
                            host_origin: int, run_duration_ns: int) -> None:
    """Add exact frame outcomes to a host-clock window series in place."""
    by_key: dict[tuple[int, int], Counter[str]] = defaultdict(Counter)
    sizes = {int(window["window_ns"]) for window in windows}
    for frame in frames:
        stamp = _num(frame.get("callback_ts_mach_ns"))
        if stamp is None:
            stamp = _num(frame.get("host_acquire_ts_mach_ns"))
        if stamp is None:
            continue
        category = frame.get("outcome_category")
        for size in sizes:
            index = max(0, stamp - host_origin) // size
            if category == "presented":
                by_key[(size, index)]["presented"] += 1
            elif category == "zero":
                by_key[(size, index)]["zero"] += 1
            else:
                by_key[(size, index)]["ambiguous"] += 1
            by_key[(size, index)]["candidate"] += 1
    for window in windows:
        size = int(window["window_ns"])
        index = int(window["window_index"])
        exposure = int(window["observed_exposure_ns"])
        counts = by_key[(size, index)]
        frame_count = counts["candidate"]
        presented = counts["presented"]
        zero = counts["zero"]
        ambiguous = counts["ambiguous"]
        exact = presented + zero
        window.update({
            "candidate_frame_count": frame_count,
            "exact_presented_count": presented,
            "exact_zero_count": zero,
            "ambiguous_frame_count": ambiguous,
            "exact_outcome_count": exact,
            "zero_share_of_exact_outcomes": zero / exact if exact else None,
        "outcome_window_edge": ("initial_and_last_partial" if index == 0 and
                                    exposure < size and index == len(windows) - 1 else
                                    "initial_partial" if index == 0 and exposure < size else
                                    "last_partial" if index == len(windows) - 1 and exposure < size else
                                    "full"),
        })
        # A window with incomplete frame chains is not eligible for a regime
        # label because the exact outcomes may be a selected subset.
        complete = frame_count == exact and ambiguous == 0
        sufficiently_long = exposure >= size * 9 // 10
        minimum_count = 15 if size == 1_000_000_000 else 30
        if not sufficiently_long or not complete or exact < minimum_count:
            label = "insufficient"
        elif zero / exact <= 0.10:
            label = "healthy_candidate"
        elif zero / exact >= 0.50:
            label = "pathological_candidate"
        else:
            label = "mixed"
        window["candidate_regime"] = label


def _sustained_regimes(windows_1s: Sequence[Mapping[str, Any]],
                       minimum_consecutive_seconds: int = 5) -> dict[str, Any]:
    """Find persistent low/high-zero regimes in complete one-second windows."""
    labels = [str(row.get("candidate_regime")) for row in windows_1s]
    stable: list[dict[str, Any]] = []
    cursor = 0
    while cursor < len(labels):
        label = labels[cursor]
        end = cursor + 1
        while end < len(labels) and labels[end] == label:
            end += 1
        length = end - cursor
        if label in ("healthy_candidate", "pathological_candidate") and length >= minimum_consecutive_seconds:
            stable.append({
                "regime": "HEALTHY" if label == "healthy_candidate" else "PATHOLOGICAL",
                "first_window_index": cursor, "last_window_index": end - 1,
                "start_elapsed_ns": int(windows_1s[cursor]["start_elapsed_ns"]),
                "end_elapsed_ns": int(windows_1s[end - 1]["end_elapsed_ns"]),
                "window_count": length,
                "minimum_exact_outcomes": min(int(windows_1s[i]["exact_outcome_count"])
                                               for i in range(cursor, end)),
                "maximum_zero_share": max(float(windows_1s[i]["zero_share_of_exact_outcomes"] or 0)
                                           for i in range(cursor, end)),
                "minimum_zero_share": min(float(windows_1s[i]["zero_share_of_exact_outcomes"] or 0)
                                           for i in range(cursor, end)),
                "left_censored": cursor == 0,
                "right_censored": end == len(labels),
            })
        cursor = end

    regimes = {row["regime"] for row in stable}
    if regimes == {"HEALTHY", "PATHOLOGICAL"}:
        overall = "MIXED"
    elif regimes == {"HEALTHY"}:
        overall = "HEALTHY"
    elif regimes == {"PATHOLOGICAL"}:
        overall = "PATHOLOGICAL"
    else:
        overall = "INDETERMINATE"

    transitions: list[dict[str, Any]] = []
    for previous, current in zip(stable, stable[1:]):
        if previous["regime"] == current["regime"]:
            continue
        transitions.append({
            "from_regime": previous["regime"], "to_regime": current["regime"],
            # Each label applies to an entire one-second bin. Include the
            # prior stable bin's start and next stable bin's end so a plotted
            # boundary cannot imply timestamp-level precision.
            "lower_elapsed_ns": max(0, previous["end_elapsed_ns"] - 1_000_000_000),
            "upper_elapsed_ns": current["start_elapsed_ns"] + 1_000_000_000,
            "lower_window_index": previous["last_window_index"],
            "upper_window_index": current["first_window_index"],
            "boundary_semantics": "conservative one-second-bin bracket from the start of the last prior stable bin through the end of the first next stable bin; the underlying transition may be inside mixed/insufficient windows",
            "timestamp_precision_ns": 1,
            "classifier_resolution_ns": 1_000_000_000,
        })
    return {
        "overall_label": overall,
        "thresholds": {"healthy_max_zero_share": 0.10, "pathological_min_zero_share": 0.50,
                        "minimum_exact_outcomes_per_1s": 15,
                        "minimum_complete_consecutive_windows": minimum_consecutive_seconds,
                        "minimum_sustained_duration_ns": minimum_consecutive_seconds * 1_000_000_000},
        "stable_segments": stable,
        "transitions": transitions,
        "candidate_window_counts": dict(Counter(labels)),
        "caution": "Labels are descriptive analysis thresholds. Mixed or insufficient one-second windows break a sustained segment. A transition bracket is limited by window semantics, even though event timestamps have nanosecond precision.",
    }


def _phase_quartiles(frames: Sequence[Mapping[str, Any]], period_ns: int | None) -> dict[str, Any]:
    quartiles = [{"quartile": i + 1, "count": 0, "percent": None} for i in range(4)]
    phase_values: list[float] = []
    displayed_times = sorted({
        int(frame["presented_time_mach_ns"]) for frame in frames
        if frame.get("exact_chain") and _num(frame.get("presented_time_mach_ns"))
        and int(frame["presented_time_mach_ns"]) > 0
    })
    request_rows = [frame for frame in frames if frame.get("exact_chain")
                    and _num(frame.get("request_ts_mach_ns")) is not None]
    if period_ns is None or period_ns <= 0 or not displayed_times:
        return {"reference_period_ns": period_ns, "reference_presented_time_mach_ns": None,
                "sample_count": 0, "quartiles": quartiles,
                "phase_fraction_stats": duration_stats([])}
    reference = displayed_times[0]
    for frame in request_rows:
        phase = ((int(frame["request_ts_mach_ns"]) - reference) % period_ns) / period_ns
        phase_values.append(phase)
        quartile = min(3, int(phase * 4))
        quartiles[quartile]["count"] += 1
    total = len(phase_values)
    for item in quartiles:
        item["percent"] = (100.0 * item["count"] / total) if total else None
    return {"reference_period_ns": period_ns, "reference_presented_time_mach_ns": reference,
            "sample_count": total, "quartiles": quartiles,
            "phase_fraction_stats": {
                "count": total, "min": min(phase_values) if phase_values else None,
                "mean": sum(phase_values) / total if total else None,
                "p50": _percentile(phase_values, 0.5), "p90": _percentile(phase_values, 0.9),
                "p99": _percentile(phase_values, 0.99), "max": max(phase_values) if phase_values else None,
            }}


def _zero_bursts(frames: Sequence[Mapping[str, Any]],
                 failed_acquisition_sequences: set[int] | None = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    ordered = sorted(frames, key=lambda frame: int(frame.get("acquisition_sequence", 0)))
    failed_acquisition_sequences = failed_acquisition_sequences or set()
    bursts: list[dict[str, Any]] = []
    current: list[Mapping[str, Any]] = []
    previous_zero: Mapping[str, Any] | None = None
    zero_spacings: list[int] = []
    previous_sequence: int | None = None
    for frame in ordered:
        sequence = int(frame.get("acquisition_sequence", 0))
        if previous_sequence is not None and (sequence != previous_sequence + 1 or
                any(previous_sequence < gap < sequence for gap in failed_acquisition_sequences)):
            if current:
                bursts.append(_make_burst(current, len(bursts), bursts[-1] if bursts else None))
                current = []
            previous_zero = None
        previous_sequence = sequence
        if not frame.get("exact_chain") or frame.get("outcome_category") not in ("zero", "presented"):
            if current:
                bursts.append(_make_burst(current, len(bursts), bursts[-1] if bursts else None))
                current = []
            previous_zero = None
            continue
        if frame.get("outcome_category") == "zero":
            if previous_zero is not None:
                left = _num(previous_zero.get("callback_ts_mach_ns"))
                right = _num(frame.get("callback_ts_mach_ns"))
                if left is not None and right is not None and right >= left:
                    zero_spacings.append(right - left)
            current.append(frame)
            previous_zero = frame
        else:
            if current:
                bursts.append(_make_burst(current, len(bursts), bursts[-1] if bursts else None))
                current = []
    if current:
        bursts.append(_make_burst(current, len(bursts), bursts[-1] if bursts else None))

    lengths = [int(burst["frame_count"]) for burst in bursts]
    return ({
        "zero_callback_count": sum(lengths), "burst_count": len(bursts),
        "run_length_stats": value_stats(lengths, "frames"),
        "run_length_histogram": _integer_histogram(lengths, ((1, 1, "1"), (2, 2, "2"),
            (3, 5, "3–5"), (6, 10, "6–10"), (11, 50, "11–50"), (51, None, ">50"))),
        "zero_to_zero_callback_spacing_ns": duration_stats(zero_spacings),
        "longest_bursts": sorted(bursts, key=lambda burst: burst["frame_count"], reverse=True)[:20],
    }, bursts)


def _make_burst(frames: Sequence[Mapping[str, Any]], index: int,
                previous_burst: Mapping[str, Any] | None) -> dict[str, Any]:
    first, last = frames[0], frames[-1]
    start_seq = int(first["acquisition_sequence"])
    end_seq = int(last["acquisition_sequence"])
    start_ts = _num(first.get("callback_ts_mach_ns"))
    end_ts = _num(last.get("callback_ts_mach_ns"))
    previous_end_seq = _num(previous_burst.get("end_acquisition_sequence")) if previous_burst else None
    previous_end_ts = _num(previous_burst.get("end_callback_ts_mach_ns")) if previous_burst else None
    return {
        "burst_index": index + 1, "frame_count": len(frames),
        "start_acquisition_sequence": start_seq, "end_acquisition_sequence": end_seq,
        "start_callback_ts_mach_ns": start_ts, "end_callback_ts_mach_ns": end_ts,
        "duration_ns": (end_ts - start_ts) if start_ts is not None and end_ts is not None else None,
        "image_indices": ";".join(str(x) for x in sorted({f.get("image_index") for f in frames
                                                              if f.get("image_index") is not None})),
        "unique_image_ids": len({f.get("image_id") for f in frames if f.get("image_id") is not None}),
        "unique_drawable_identities": len({f.get("drawable_identity") for f in frames
                                             if f.get("drawable_identity") is not None}),
        "frames_between_previous_zero_burst": (start_seq - previous_end_seq - 1)
        if previous_end_seq is not None else None,
        "callback_spacing_from_previous_zero_burst_ns": (start_ts - previous_end_ts)
        if start_ts is not None and previous_end_ts is not None else None,
    }


def _integer_histogram(values: Sequence[int], buckets: Sequence[tuple[int, int | None, str]]) -> list[dict[str, Any]]:
    total = len(values)
    return [{"bucket": label, "count": sum(1 for value in values
            if value >= lower and (upper is None or value <= upper)),
            "percent": (100.0 * sum(1 for value in values
            if value >= lower and (upper is None or value <= upper)) / total) if total else None}
            for lower, upper, label in buckets]


def _identity_reuse(frames: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    ordered = sorted((frame for frame in frames if frame.get("exact_chain")),
                     key=lambda frame: int(frame.get("acquisition_sequence", 0)))
    result: dict[str, Any] = {}
    for field in ("image_index", "image_id", "drawable_id", "drawable_identity", "texture_id"):
        previous: dict[Any, Mapping[str, Any]] = {}
        values: Counter[Any] = Counter()
        gaps: list[int] = []
        time_gaps: list[int] = []
        immediate = 0
        repeat_rows = 0
        for frame in ordered:
            value = frame.get(field)
            if value is None:
                continue
            values[value] += 1
            if value in previous:
                repeat_rows += 1
                gap = int(frame["acquisition_sequence"]) - int(previous[value]["acquisition_sequence"])
                gaps.append(gap)
                if int(frame["acquisition_sequence"]) - int(previous[value]["acquisition_sequence"]) == 1:
                    immediate += 1
                prev_time = _num(previous[value].get("callback_ts_mach_ns"))
                this_time = _num(frame.get("callback_ts_mach_ns"))
                if prev_time is not None and this_time is not None and this_time >= prev_time:
                    time_gaps.append(this_time - prev_time)
            previous[value] = frame
        result[field] = {
            "unique_identity_count": len(values), "reused_identity_rows": repeat_rows,
            "immediate_previous_frame_reuse_count": immediate,
            "uses_per_identity_histogram": _integer_histogram(list(values.values()),
                ((1, 1, "1 use"), (2, 2, "2 uses"), (3, 3, "3 uses"), (4, 10, "4–10 uses"),
                 (11, None, ">10 uses"))),
            "reuse_acquisition_spacing": value_stats(gaps, "frames"),
            "reuse_callback_time_spacing_ns": duration_stats(time_gaps),
        }
    return result


def _extreme_stalls(frames: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    stalls: list[dict[str, Any]] = []
    for frame in frames:
        for metric in ("next_drawable_duration_ns", "host_submit_duration_ns"):
            duration = _num(frame.get(metric))
            if duration is not None and duration >= 25_000_000:
                stalls.append({
                    "acquisition_sequence": frame["acquisition_sequence"],
                    "app_frame_id": frame.get("app_frame_id"), "metric": metric,
                    "duration_ns": duration, "duration_ms": duration / 1_000_000,
                    "image_index": frame.get("image_index"), "image_id": frame.get("image_id"),
                    "drawable_id": frame.get("drawable_id"),
                    "outcome_category": frame.get("outcome_category"),
                    "request_ts_mach_ns": frame.get("request_ts_mach_ns"),
                    "callback_ts_mach_ns": frame.get("callback_ts_mach_ns"),
                })
    return sorted(stalls, key=lambda item: item["duration_ns"], reverse=True)


def _segments(frames: Sequence[Mapping[str, Any]], origin: int, end: int,
              host: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    span = max(1, end - origin)
    result: list[dict[str, Any]] = []
    for quartile in range(4):
        start_delta = span * quartile // 4
        end_delta = span * (quartile + 1) // 4
        selected = [frame for frame in frames
                    if _num(frame.get("host_acquire_ts_mach_ns")) is not None
                    and start_delta <= int(frame["host_acquire_ts_mach_ns"]) - origin
                    < (end_delta if quartile < 3 else span + 1)]
        waits = [_num(frame.get("next_drawable_duration_ns")) for frame in selected
                 if _num(frame.get("next_drawable_duration_ns")) is not None]
        outcomes = Counter(frame.get("outcome_category") for frame in selected)
        first_ts = origin + start_delta
        last_ts = origin + end_delta
        elapsed = max(0, last_ts - first_ts)
        result.append({
            "segment": f"time_quartile_{quartile + 1}", "start_elapsed_ns": start_delta,
            "end_elapsed_ns": end_delta, "frame_count": len(selected),
            "presented_count": outcomes.get("presented", 0), "zero_count": outcomes.get("zero", 0),
            "ambiguous_count": outcomes.get("ambiguous", 0),
            "zero_percent_of_exact_outcomes": (100 * outcomes.get("zero", 0) /
                (outcomes.get("presented", 0) + outcomes.get("zero", 0)))
                if outcomes.get("presented", 0) + outcomes.get("zero", 0) else None,
            "acquired_rate_per_second": len(selected) * 1_000_000_000 / elapsed if elapsed else None,
            "next_drawable_stats_ns": duration_stats(waits),
        })
    return result


def _enrich_regime_segments(regime: dict[str, Any], frames: Sequence[Mapping[str, Any]],
                            host_origin: int) -> None:
    """Summarize timings and outcomes within each sustained mode segment."""
    for segment in regime["stable_segments"]:
        start = int(segment["start_elapsed_ns"])
        end = int(segment["end_elapsed_ns"])
        duration = max(0, end - start)
        selected = [frame for frame in frames
                    if _num(frame.get("host_acquire_ts_mach_ns")) is not None
                    and start <= int(frame["host_acquire_ts_mach_ns"]) - host_origin < end]
        exact = [frame for frame in selected if frame.get("exact_chain")]
        outcomes = Counter(frame.get("outcome_category") for frame in selected)
        for metric, name in (("next_drawable_duration_ns", "next_drawable"),
                             ("host_submit_duration_ns", "host_submit")):
            values = [_num(frame.get(metric)) for frame in selected if _num(frame.get(metric)) is not None]
            stats = duration_stats(values)
            for percentile in ("p50", "p99"):
                segment[f"{name}_{percentile}_ns"] = stats[f"{percentile}_ns"]
            segment[f"{name}_max_ns"] = stats["max_ns"]
            segment[f"{name}_sample_count"] = len(values)
        segment.update({
            "candidate_frame_count": len(selected),
            "exact_outcome_count": len(exact),
            "presented_count": outcomes.get("presented", 0),
            "zero_count": outcomes.get("zero", 0),
            "no_present_callback_count": outcomes.get("no_present_callback", 0),
            "unknown_count": outcomes.get("ambiguous", 0) + outcomes.get("unknown", 0),
            "acquisition_rate_per_second": len(selected) * 1_000_000_000 / duration if duration else None,
            "callback_rate_per_second": len(exact) * 1_000_000_000 / duration if duration else None,
            "zero_percent_of_exact_outcomes": (100.0 * outcomes.get("zero", 0) / len(exact))
                if exact else None,
        })


def _validate_telemetry(host: Sequence[Mapping[str, Any]], target_pid: int,
                        configured_layer_id: int | None) -> tuple[list[dict[str, Any]], list[str]]:
    raw_rows = _event_rows(host, "window_telemetry", "LAYER_SOURCE")
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    seen_sequences: set[int] = set()
    for ordinal, row in enumerate(raw_rows, 1):
        if row.get("trace_role") != "target" or _num(row.get("host_pid")) != target_pid \
                or _num(row.get("target_unix_pid")) != target_pid:
            errors.append(f"telemetry row {ordinal} is not owned by target PID {target_pid}")
            continue
        sequence = _num(row.get("telemetry_sequence"))
        stamp = _num(row.get("ts_mach_ns"))
        layer_id = _num(row.get("layer_id"))
        if sequence is None or sequence <= 0:
            errors.append(f"telemetry row {ordinal} has no positive telemetry_sequence")
            continue
        if sequence in seen_sequences:
            errors.append(f"duplicate telemetry_sequence {sequence}")
            continue
        seen_sequences.add(sequence)
        if stamp is None or stamp <= 0:
            errors.append(f"telemetry sequence {sequence} has no positive ts_mach_ns")
            continue
        if configured_layer_id is not None and layer_id != configured_layer_id:
            errors.append(f"telemetry sequence {sequence} layer_id does not match layer_configuration")
            continue
        if row.get("trigger") not in ("initial", "periodic", "window_notification", "app_notification"):
            errors.append(f"telemetry sequence {sequence} has an unknown trigger")
            continue
        rows.append(dict(row))
    rows.sort(key=lambda row: (int(row["ts_mach_ns"]), int(row["telemetry_sequence"])))
    for left, right in zip(rows, rows[1:]):
        if int(right["telemetry_sequence"]) <= int(left["telemetry_sequence"]):
            errors.append("telemetry_sequence is not strictly increasing with sampled time")
            break
    return rows, errors


def _correlate_telemetry(frames: Sequence[dict[str, Any]], telemetry: Sequence[Mapping[str, Any]]) -> None:
    if not telemetry:
        return
    stamps = [int(row["ts_mach_ns"]) for row in telemetry]
    for frame in frames:
        frame_stamp = _num(frame.get("callback_ts_mach_ns"))
        if frame_stamp is None:
            frame["state_correlation"] = {"status": "unknown", "reason": "callback_missing"}
            continue
        index = bisect.bisect_right(stamps, frame_stamp) - 1
        if index < 0:
            frame["state_correlation"] = {"status": "unknown", "reason": "no_prior_snapshot"}
            continue
        row = telemetry[index]
        age = frame_stamp - stamps[index]
        if row.get("window_associated") is not True or _num(row.get("window_id")) in (None, 0):
            frame["state_correlation"] = {
                "status": "unknown", "reason": "snapshot_has_no_associated_window",
                "telemetry_sequence": row.get("telemetry_sequence"), "age_ns": age,
                "window_associated": row.get("window_associated"),
                "window_id": row.get("window_id"), "layer_id": row.get("layer_id"),
            }
            continue
        if age > TELEMETRY_STALE_AFTER_NS:
            frame["state_correlation"] = {
                "status": "unknown", "reason": "prior_snapshot_stale",
                "telemetry_sequence": row.get("telemetry_sequence"), "age_ns": age,
            }
            continue
        frame["state_correlation"] = {
            "status": "observed_as_of_prior_snapshot",
            "telemetry_sequence": row.get("telemetry_sequence"),
            "trigger": row.get("trigger"), "sample_ts_mach_ns": row.get("ts_mach_ns"),
            "age_ns": age,
            **{field: row.get(field) for field in TELEMETRY_FIELDS},
        }


def _telemetry_outcome_groups(frames: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counts: dict[tuple[Any, ...], Counter[str]] = defaultdict(Counter)
    for frame in frames:
        state = frame.get("state_correlation")
        if isinstance(state, Mapping):
            key = ("observed", state.get("window_visible"), state.get("window_occluded"),
                   state.get("window_miniaturized"), state.get("app_active"),
                   state.get("window_visible_intersection_area"), state.get("window_associated"),
                   state.get("window_id"), state.get("layer_id"), state.get("trigger"))
            if state.get("status") != "observed_as_of_prior_snapshot":
                key = ("unknown", state.get("window_visible"), state.get("window_occluded"),
                       state.get("window_miniaturized"), state.get("app_active"),
                       state.get("window_visible_intersection_area"), state.get("window_associated"),
                       state.get("window_id"), state.get("layer_id"), state.get("trigger"))
        else:
            key = ("unknown", None, None, None, None, None, None, None, None, None)
        counts[key][str(frame.get("outcome_category", "ambiguous"))] += 1
    result = []
    for key, outcomes in sorted(counts.items(), key=lambda item: repr(item[0])):
        result.append({
            "state_observation": key[0], "window_visible": key[1], "window_occluded": key[2],
            "window_miniaturized": key[3], "app_active": key[4],
            "window_visible_intersection_area": key[5], "window_associated": key[6],
            "window_id": key[7], "layer_id": key[8], "trigger": key[9],
            "presented_count": outcomes.get("presented", 0), "zero_count": outcomes.get("zero", 0),
            "ambiguous_count": outcomes.get("ambiguous", 0),
        })
    return result


def _summary_rate(count: int, duration_ns: int) -> float | None:
    return count * 1_000_000_000 / duration_ns if duration_ns > 0 else None


def analyze_run(run_dir: str | Path) -> dict[str, Any]:
    """Analyze one already accepted run directory using its raw traces.

    Trace-index ownership and run-result validity are strict inputs. Helper
    processes are never read or pooled with the target process.
    """
    run_dir = Path(run_dir).resolve(strict=True)
    result, index, app, host, app_path, host_path, target_pid = _load_owned_inputs(run_dir)
    run_id = str(result.get("experiment_id") or run_dir.name)
    config = _event_rows(app, "app_configuration", "APP_CONFIG")[0]
    target_layer_rows = _event_rows(host, "layer_configuration", "LAYER_SOURCE")
    layer_id = _num(target_layer_rows[0].get("layer_id")) if len(target_layer_rows) == 1 else None
    telemetry, telemetry_errors = _validate_telemetry(host, target_pid, layer_id)

    frames = _extract_frames(app, host)
    _correlate_telemetry(frames, telemetry)

    host_begins = [row for row in _event_rows(host, "vkAcquireNextImageKHR_begin", "MVK_BOUNDARY")
                   if _num(row.get("call_begin_mach_ns")) is not None]
    if not host_begins:
        host_begins = [row for row in _event_rows(host, "vkAcquireNextImageKHR_begin", "WINE_SOURCE")
                       if _num(row.get("call_begin_mach_ns")) is not None]
    host_origin = min((_num(row.get("call_begin_mach_ns")) for row in host_begins), default=None)
    host_end_candidates = [_num(row.get("ts_mach_ns")) for row in
                           _event_rows(host, "drawable_release", "LAYER_SOURCE")
                           if _num(row.get("ts_mach_ns")) is not None]
    if not host_end_candidates:
        host_end_candidates = [_num(row.get("ts_mach_ns")) for row in
                               _event_rows(host, "present_request", "MVK_BOUNDARY")
                               if _num(row.get("ts_mach_ns")) is not None]
    host_end = max(host_end_candidates, default=host_origin)
    if host_origin is None or host_end is None:
        raise AnalysisError("target trace has no host frame interval")
    host_duration = max(0, host_end - host_origin)

    app_begins = [_num(row.get("call_begin_qpc_ns")) for row in
                  _event_rows(app, "app_vkAcquireNextImageKHR_begin", "APP_SOURCE")
                  if _num(row.get("call_begin_qpc_ns")) is not None]
    app_ends = [_num(row.get("ts_qpc_ns")) for row in
                _event_rows(app, "app_vkQueuePresentKHR_end", "APP_SOURCE")
                if _num(row.get("ts_qpc_ns")) is not None]
    app_origin = min(app_begins, default=None)
    app_end = max(app_ends, default=app_origin)
    if app_origin is None or app_end is None:
        raise AnalysisError("app trace has no app frame interval")
    app_duration = max(0, app_end - app_origin)

    period_ns = _num(result.get("validation", {}).get("effective_refresh_period_ns"))
    phase = _phase_quartiles(frames, period_ns)
    failed_host_acquires = _rows_by_sequence(
        host, "vkAcquireNextImageKHR_end", "MVK_BOUNDARY", "acquisition_sequence")
    failed_acquisition_sequences = {sequence for sequence, rows in failed_host_acquires.items()
                                    if any(_acquire_failed(row) for row in rows)}
    zero_summary, bursts = _zero_bursts(frames, failed_acquisition_sequences)
    identity_reuse = _identity_reuse(frames)
    stalls = _extreme_stalls(frames)

    host_windows: dict[str, list[dict[str, Any]]] = {}
    app_windows: dict[str, list[dict[str, Any]]] = {}
    for window_ns in WINDOWS_NS:
        host_windows[str(window_ns // 1_000_000_000)] = _make_windows(
            host, host_origin, host_end, window_ns, stream="host")
        app_windows[str(window_ns // 1_000_000_000)] = _make_windows(
            app, app_origin, app_end, window_ns, stream="app")
    all_host_window_rows = [row for series in host_windows.values() for row in series]
    _enrich_outcome_windows(all_host_window_rows, frames, host_origin, host_duration)
    regime = _sustained_regimes(host_windows["1"])
    _enrich_regime_segments(regime, frames, host_origin)

    host_event_counts: dict[str, int] = {}
    for name, (event, origin, accepted) in HOST_RATE_EVENTS.items():
        host_event_counts[name] = sum(1 for row in _event_rows(host, event, origin)
                                      if _accepted_count(row, accepted))
    app_event_counts: dict[str, int] = {}
    for name, (event, accepted) in APP_RATE_EVENTS.items():
        app_event_counts[name] = sum(1 for row in _event_rows(app, event, "APP_SOURCE")
                                     if _accepted_count(row, accepted))

    host_duration_samples: dict[str, list[int]] = {
        "next_drawable": [_num(row.get("duration_ns")) for row in _event_rows(host, "next_drawable_end", "LAYER_SOURCE")
                          if row.get("detail") == "acquired" and _num(row.get("duration_ns")) is not None],
    }
    for label, event in (("host_acquire", "vkAcquireNextImageKHR_end"),
                         ("host_submit", "vkQueueSubmit2_end"),
                         ("host_present", "vkQueuePresentKHR_end")):
        host_duration_samples[label] = [_num(row.get("duration_ns")) for row in
                                        _host_api_durations(host, event)
                                        if _num(row.get("duration_ns")) is not None]
    app_duration_samples: dict[str, list[int]] = {}
    for label, event in (("app_acquire", "app_vkAcquireNextImageKHR_end"),
                         ("app_submit", "app_vkQueueSubmit2_end"),
                         ("app_present", "app_vkQueuePresentKHR_end")):
        app_duration_samples[label] = [_num(row.get("duration_ns")) for row in
                                       _event_rows(app, event, "APP_SOURCE")
                                       if _api_success(event, row) and _num(row.get("duration_ns")) is not None]

    segments = _segments(frames, host_origin, host_end, host)
    telemetry_groups = _telemetry_outcome_groups(frames)
    phase_rows: list[dict[str, Any]] = []
    for item in phase["quartiles"]:
        phase_rows.append({"run_id": run_id, **item, "reference_period_ns": phase["reference_period_ns"]})
    segment_rows = [{"run_id": run_id, **segment} for segment in segments]
    telemetry_rows: list[dict[str, Any]] = []
    for row in telemetry:
        telemetry_rows.append({"run_id": run_id, **{key: row.get(key) for key in
                              ("ts_mach_ns",) + TELEMETRY_FIELDS}})
    telemetry_group_rows = [{"run_id": run_id, **row} for row in telemetry_groups]

    source_files = [
        {"role": "run_result", "path": "run-result.json", "sha256": _sha256(run_dir / "run-result.json")},
        {"role": "trace_index", "path": "trace-index.json", "sha256": _sha256(run_dir / "trace-index.json")},
        {"role": "run_manifest", "path": "run-manifest.json", "sha256": _sha256(run_dir / "run-manifest.json")},
        {"role": "app_trace", "path": str(app_path.relative_to(run_dir)), "sha256": _sha256(app_path)},
        {"role": "target_host_trace", "path": str(host_path.relative_to(run_dir)), "sha256": _sha256(host_path)},
    ]
    report_candidate = REPOSITORY_ROOT / "experiments" / "dxvk_macos_step11d3b" / "STEP-11D3-BR6-REPORT.md"
    if run_dir.parent == RUNS_ROOT and report_candidate.is_file():
        source_files.append({"role": "accepted_set_report", "path": str(report_candidate.relative_to(REPOSITORY_ROOT)),
                             "sha256": _sha256(report_candidate)})

    exact_outcomes = Counter(frame["outcome_category"] for frame in frames)
    enum_outcomes = Counter(frame.get("outcome_enum", "UNKNOWN") for frame in frames)
    actual_run_ns = _num(config.get("actual_run_ns"))
    rate_denominator_ns = actual_run_ns if actual_run_ns is not None and actual_run_ns > 0 else None
    summary = {
        "run_id": run_id,
        "run_directory": str(run_dir),
        "phase": result.get("phase"),
        "process_returncode": result.get("process_returncode"),
        "run_status": result.get("status"),
        "run_valid": result.get("is_valid"),
        "target_unix_pid": target_pid,
        "app_clock": config.get("timestamp_clock_domain"),
        "app_qpc_frequency": config.get("qpc_frequency"),
        "requested_seconds": config.get("run_seconds_requested"),
        "host_interval": {"clock": "mach_absolute_time_ns", "start_ns": host_origin,
                          "end_ns": host_end, "duration_ns": host_duration},
        "app_interval": {"clock": "windows_qpc_ns", "start_ns": app_origin,
                         "end_ns": app_end, "duration_ns": app_duration},
        "rendered_run_duration": {"duration_ns": actual_run_ns,
                                   "source": "app_configuration.actual_run_ns" if actual_run_ns else "unavailable",
                                   "rate_denominator_ns": rate_denominator_ns},
        "host_event_counts": host_event_counts,
        "app_event_counts": app_event_counts,
        "host_rates_per_second": {key: _summary_rate(value, rate_denominator_ns or 0)
                                   for key, value in host_event_counts.items()},
        "app_rates_per_second": {key: _summary_rate(value, rate_denominator_ns or 0)
                                  for key, value in app_event_counts.items()},
        "host_observed_interval_rates_per_second": {key: _summary_rate(value, host_duration)
                                                    for key, value in host_event_counts.items()},
        "app_observed_interval_rates_per_second": {key: _summary_rate(value, app_duration)
                                                   for key, value in app_event_counts.items()},
        "frame_outcomes": {
            "candidate_acquisitions": len(frames), "exact_one_complete_chains": sum(
                1 for frame in frames if frame.get("exact_chain")),
            "presented": exact_outcomes.get("presented", 0), "zero": exact_outcomes.get("zero", 0),
            "no_present_callback": enum_outcomes.get("NO_PRESENT_CALLBACK", 0),
            "ambiguous": enum_outcomes.get("UNKNOWN", 0),
            "outcome_enum_counts": {name: enum_outcomes.get(name, 0) for name in OUTCOME_ENUMS},
            "zero_percent_of_exact_outcomes": (100.0 * exact_outcomes.get("zero", 0) /
                (exact_outcomes.get("zero", 0) + exact_outcomes.get("presented", 0)))
                if exact_outcomes.get("zero", 0) + exact_outcomes.get("presented", 0) else None,
            "causal_categories_note": "SUPERSEDED_BEFORE_DISPLAY, SWAPCHAIN_REPLACED, and WINDOW_NOT_PRESENTABLE are left at zero because these traces do not prove them. UNKNOWN includes incomplete or contradictory chains and terminally censored requests. NO_PRESENT_CALLBACK is used only for a unique successful acquisition-to-request prefix with a valid trace tail of at least one second.",
        },
        "timing_stats_ns": {key: duration_stats(values) for key, values in
                             {**host_duration_samples, **app_duration_samples}.items()},
        "histograms_ns": {key: duration_histogram(values) for key, values in
                          {**host_duration_samples, **app_duration_samples}.items()},
        "zero_bursts": zero_summary,
        "identity_reuse": identity_reuse,
        "refresh_phase": phase,
        "segments": segments,
        "regime_analysis": regime,
        "extreme_stall_count_ge_25ms": len(stalls),
        "extreme_stall_count_ge_100ms": sum(1 for item in stalls if item["duration_ns"] >= 100_000_000),
        "telemetry": {
            "sample_count": len(telemetry), "invalid_row_errors": telemetry_errors,
            "stale_after_ns": TELEMETRY_STALE_AFTER_NS,
            "state_outcome_groups": telemetry_groups,
            "correlation_rule": "latest target-owned snapshot at or before callback timestamp; no future/backward match; older than stale_after_ns is unknown",
            "outcome_rule": "window telemetry is context only and never changes displayed/zero/ambiguous frame classification",
        },
        "lifecycle_interpretation": "availability_after_presented_handler is emitted after the presented-handler callback returns. It is a callback-return timing witness, not independent proof that a drawable or GPU resource was recycled or that GPU use completed.",
        "window_definition": {
            "host_clock": "mach_absolute_time_ns; event timestamps are binned relative to the first target acquire call",
            "app_clock": "windows QPC converted to ns; binned relative to first app acquire call",
            "cross_clock_alignment": "none; host and app windows are reported separately because their recorded origins differ",
            "windows_seconds": [1, 5],
        },
        "source_files": source_files,
    }
    return {
        "summary": summary, "frames": [{"run_id": run_id, **frame} for frame in frames],
        "windows": [{"run_id": run_id, **row} for name, series in host_windows.items()
                    for row in series] + [{"run_id": run_id, **row}
                    for name, series in app_windows.items() for row in series],
        "zero_bursts": [{"run_id": run_id, **burst} for burst in bursts],
        "extreme_stalls": [{"run_id": run_id, **stall} for stall in stalls],
        "phase_quartiles": phase_rows, "segments": segment_rows,
        "regime_segments": [{"run_id": run_id, **row} for row in regime["stable_segments"]],
        "regime_transitions": [{"run_id": run_id, **row} for row in regime["transitions"]],
        "window_telemetry": telemetry_rows,
        "telemetry_outcome_groups": telemetry_group_rows,
    }


def _csv_value(value: Any) -> Any:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return value


def _write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    columns: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})


def _render_readme(summaries: Sequence[Mapping[str, Any]]) -> str:
    lines = [
        "# BR7 offline presentation analysis",
        "",
        "This directory contains derived, observation-only analysis of accepted raw app and target-host traces. The analyzer reads no live display state and starts no process. It does not modify source traces, run Gate 0, certify storage, or establish visual quality.",
        "",
        "Frame outcomes are conservative: only a complete, unique app-to-host acquisition → submit → drawable → request → handler → callback → availability → release chain with consistent identities is classified. A unique callback with `presented_time_mach_ns == 0` and `is_displayed == false` is `CALLBACK_PRESENTED_TIME_ZERO`; a positive presented time plus `is_displayed == true` is `DISPLAYED_VALID_PRESENTED_TIME`. A unique successful acquisition-to-request prefix with no callback and at least one second of valid trace tail is `NO_PRESENT_CALLBACK`; requests within the final second are terminally censored as `UNKNOWN`. Duplicated, contradictory, or mismatched rows are `UNKNOWN`. `SUPERSEDED_BEFORE_DISPLAY`, `SWAPCHAIN_REPLACED`, and `WINDOW_NOT_PRESENTABLE` stay unassigned because the evidence cannot establish those causes.",
        "",
        "The target-host and app traces use separate recorded clock origins. One- and five-second windows are binned independently in each clock domain, relative to each stream's first acquire call. No app/host timestamp alignment is assumed. The host trace is selected only from the single target-owned `trace-index.json` process record; helper traces are never pooled.",
        "",
        "`window_telemetry` rows, when present, are correlated as context using the latest target-owned sample at or before the frame callback timestamp. A prior sample older than 2 seconds, no prior sample, or malformed ownership is unknown. The association is temporal and asynchronous, not a frame identity join; state never changes the frame outcome.",
        "The `availability_after_presented_handler` row is emitted after the presented-handler callback returns. It provides callback-return timing, not independent proof of drawable/resource recycling or GPU completion.",
        "",
        "Regime candidates are classified from exact callback outcomes in one-second windows: healthy candidate means zero share ≤10%, pathological candidate means zero share ≥50%, and the interval between is mixed. A window is eligible only when it has at least 15 exact outcomes, at least 90% exposure, and every acquisition candidate in the window has an exact callback chain. A stable regime requires five consecutive eligible one-second windows with the same candidate label. `MIXED` means both a sustained healthy and a sustained pathological segment were observed in the same run. Transition brackets span from the beginning of the last prior stable bin through the end of the first next stable bin; timestamp precision does not narrow this one-second classifier resolution.",
        "",
        "## Per-run observations",
        "",
        "| Run | App-reported duration (s) | Host observed coverage (s) | Acquired/s | Submitted/s | Requests/s | Callbacks/s | Zero callbacks | Presented / zero / no callback / unknown | Regime | nextDrawable p99 / max (ms) | Submit p99 / max (ms) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---|---|---:|---:|",
    ]
    for item in summaries:
        run_duration_ns = item["rendered_run_duration"]["rate_denominator_ns"]
        duration = run_duration_ns / 1e9 if run_duration_ns else 0.0
        coverage = item["host_interval"]["duration_ns"] / 1e9
        stats = item["timing_stats_ns"]
        outcomes = item["frame_outcomes"]
        rate = item["host_rates_per_second"]
        nd = stats["next_drawable"]
        sub = stats["host_submit"]
        lines.append(
            f"| {item['run_id']} | {duration:.3f} | {coverage:.3f} | {rate['acquisition']:.3f} | {rate['submit']:.3f} | "
            f"{rate['request']:.3f} | {rate['callback']:.3f} | {item['zero_bursts']['zero_callback_count']} | "
            f"{outcomes['presented']} / {outcomes['zero']} / {outcomes['no_present_callback']} / {outcomes['ambiguous']} | "
            f"{item['regime_analysis']['overall_label']} | "
            f"{nd['p99_ns'] / 1e6:.3f} / {nd['max_ns'] / 1e6:.3f} | "
            f"{sub['p99_ns'] / 1e6:.3f} / {sub['max_ns'] / 1e6:.3f} |"
        )
    lines.extend([
        "",
        "## Artifact map",
        "",
        "- `summary.json`: per-run rates, distribution statistics, histograms, refresh-phase quartiles, segmentation, zero-burst and identity-reuse summaries, telemetry correlation, and source hashes.",
        "- `per_run.csv`: compact per-run metrics.",
        "- `windows.csv`: per-window counts and rates, separately labeled `host` and `app` clocks. Host rows include displayed-callback rate, zero percent of observed callbacks, nextDrawable p50/p99/max, and submit p50/p99/max over events ending inside each one- or five-second window.",
        "- `frames.csv`: one compact row per target acquisition candidate, with frame identity, key event timestamps/durations, conservative outcome enum, terminal-censor flag, and any state-correlation summary.",
        "- `zero_bursts.csv`: contiguous runs of exact-zero callback outcomes with sequence and callback-time spacing.",
        "- `extreme_stalls.csv`: each `nextDrawable` or host submit duration ≥25 ms, with frame identity and outcome.",
        "- `phase_quartiles.csv`, `segments.csv`, `regime_segments.csv`, and `regime_transitions.csv`: refresh phase, run-time quartiles, sustained mode segments with their own outcome/timing distributions, and transition brackets.",
        "- `window_telemetry.csv` and `telemetry_outcomes.csv`: raw target-owned snapshots and outcome counts by the prior observed state when telemetry exists.",
        "",
        "All durations in raw JSON/CSV fields are nanoseconds unless a field explicitly ends in `_ms`. Histogram buckets use `[lower, upper)` boundaries. Percentiles use linear interpolation at `(n−1)×q`.",
        "",
    ])
    return "\n".join(lines)


def write_analysis(results: Sequence[Mapping[str, Any]], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    summaries = [result["summary"] for result in results]
    payload = {
        "schema_version": 1,
        "analysis_kind": "observation_only",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_script_sha256": _sha256(Path(__file__).resolve()),
        "method_limits": [
            "Raw evidence is accepted only when run-result says PASS/is_valid and the trace index has one valid, explicitly owned target process.",
            "No helpers are included in target event counts.",
            "App and host clocks are analyzed separately; no cross-clock offset is estimated.",
            "Incomplete or non-unique chains are ambiguous; superseded is never inferred.",
            "Vulkan acquire results VK_SUCCESS and VK_SUBOPTIMAL_KHR both return an acquired image; explicit failed attempts are counted separately and excluded from frame candidates.",
            "Window telemetry is asynchronous, temporal context only and not causal proof.",
            "availability_after_presented_handler marks callback-return timing, not independent resource-recycle or GPU-completion proof.",
            "No live run, visual inspection, storage certification, or presentation-setting change is performed by this analysis.",
        ],
        "runs": summaries,
    }
    (output_dir / "summary.json").write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                                               encoding="utf-8")
    frame_columns = (
        "run_id", "acquisition_sequence", "app_frame_id", "image_index", "image_id",
        "native_swapchain_id", "drawable_id", "drawable_identity", "texture_id",
        "host_acquire_ts_mach_ns", "host_submit_ts_mach_ns", "request_ts_mach_ns",
        "callback_ts_mach_ns", "presented_time_mach_ns", "availability_ts_mach_ns",
        "release_ts_mach_ns", "next_drawable_duration_ns", "host_acquire_duration_ns",
        "host_submit_duration_ns", "host_present_duration_ns", "app_acquire_duration_ns",
        "app_submit_duration_ns", "app_present_duration_ns", "exact_chain",
        "outcome_category", "outcome_enum", "terminal_censored", "ambiguity_reasons",
    )
    def compact_frame(row: Mapping[str, Any]) -> dict[str, Any]:
        state = row.get("state_correlation")
        compact = {key: row.get(key) for key in frame_columns}
        if isinstance(state, Mapping):
            compact.update({
                "state_status": state.get("status"),
                "state_reason": state.get("reason"),
                "telemetry_sequence": state.get("telemetry_sequence"),
                "telemetry_age_ns": state.get("age_ns"),
                "window_visible": state.get("window_visible"),
                "window_occluded": state.get("window_occluded"),
                "window_miniaturized": state.get("window_miniaturized"),
                "app_active": state.get("app_active"),
                "window_visible_intersection_area": state.get("window_visible_intersection_area"),
            })
        return compact

    for name, key in (("frames.csv", "frames"), ("windows.csv", "windows"),
                      ("zero_bursts.csv", "zero_bursts"), ("extreme_stalls.csv", "extreme_stalls"),
                      ("phase_quartiles.csv", "phase_quartiles"), ("segments.csv", "segments"),
                      ("window_telemetry.csv", "window_telemetry"),
                      ("telemetry_outcomes.csv", "telemetry_outcome_groups"),
                      ("regime_segments.csv", "regime_segments"),
                      ("regime_transitions.csv", "regime_transitions")):
        rows = [row for result in results for row in result[key]]
        if name == "frames.csv":
            rows = [compact_frame(row) for row in rows]
        _write_csv(output_dir / name, rows)
    compact_rows = []
    for item in summaries:
        outcomes = item["frame_outcomes"]
        nd = item["timing_stats_ns"]["next_drawable"]
        submit = item["timing_stats_ns"]["host_submit"]
        compact_rows.append({
            "run_id": item["run_id"], "phase": item["phase"], "status": item["run_status"],
            "target_unix_pid": item["target_unix_pid"],
            "app_reported_run_duration_ns": item["rendered_run_duration"]["duration_ns"],
            "host_observed_coverage_ns": item["host_interval"]["duration_ns"],
            "host_acquired_per_s": item["host_rates_per_second"]["acquisition"],
            "host_submitted_per_s": item["host_rates_per_second"]["submit"],
            "host_requests_per_s": item["host_rates_per_second"]["request"],
            "host_callbacks_per_s": item["host_rates_per_second"]["callback"],
            "host_zero_callbacks_per_s": item["host_rates_per_second"]["zero"],
            "host_availability_per_s": item["host_rates_per_second"]["availability"],
            "host_release_per_s": item["host_rates_per_second"]["release"],
            "app_acquired_per_s": item["app_rates_per_second"]["acquisition"],
            "app_submitted_per_s": item["app_rates_per_second"]["submit"],
            "app_requests_per_s": item["app_rates_per_second"]["request"],
            "presented_count": outcomes["presented"], "zero_count": outcomes["zero"],
            "no_present_callback_count": outcomes["no_present_callback"],
            "unknown_count": outcomes["ambiguous"], "overall_regime": item["regime_analysis"]["overall_label"],
            **{f"outcome_{name.lower()}_count": count for name, count in outcomes["outcome_enum_counts"].items()},
            "sustained_segments": len(item["regime_analysis"]["stable_segments"]),
            "zero_burst_count": item["zero_bursts"]["burst_count"],
            "longest_zero_burst_frames": item["zero_bursts"]["run_length_stats"]["max_frames"],
            "next_drawable_p99_ns": nd["p99_ns"], "next_drawable_max_ns": nd["max_ns"],
            "host_submit_p99_ns": submit["p99_ns"], "host_submit_max_ns": submit["max_ns"],
            "extreme_stall_count_ge_25ms": item["extreme_stall_count_ge_25ms"],
            "extreme_stall_count_ge_100ms": item["extreme_stall_count_ge_100ms"],
            "telemetry_samples": item["telemetry"]["sample_count"],
            "source_hashes": {source["role"]: source["sha256"] for source in item["source_files"]},
        })
    _write_csv(output_dir / "per_run.csv", compact_rows)
    (output_dir / "README.md").write_text(_render_readme(summaries), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", action="append", type=Path,
                        help="run directory to analyze; may be repeated for later accepted runs")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT,
                        help=f"derived artifact directory (default: {DEFAULT_OUTPUT})")
    args = parser.parse_args(argv)
    run_dirs = args.run_dir or [RUNS_ROOT / name for name, _role in ACCEPTED_RUNS]
    try:
        results = [analyze_run(path) for path in run_dirs]
        write_analysis(results, args.output_dir)
    except (AnalysisError, OSError, KeyError, TypeError, ValueError) as exc:
        print(f"BR7 analysis failed: {exc}", file=sys.stderr)
        return 2
    print(f"Analyzed {len(results)} run(s); wrote observation-only artifacts to {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
