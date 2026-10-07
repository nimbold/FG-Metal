#!/usr/bin/env python3
"""Summarize Step 10B.6 Display tables without equating rows to frames."""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any


def parse_table(path: Path) -> list[dict[str, dict[str, str] | None]]:
    root = ET.parse(path).getroot()
    schema = next(root.iter("schema"), None)
    if schema is None:
        raise ValueError(f"missing schema in {path}")
    columns = [column.findtext("mnemonic") for column in schema.findall("col")]
    ids = {node.get("id"): node for node in root.iter() if node.get("id")}
    result = []
    for row in root.iter("row"):
        cells: dict[str, dict[str, str] | None] = {}
        for column, cell in zip(columns, row):
            if cell.tag == "sentinel":
                cells[column] = None
                continue
            value = ids.get(cell.get("ref")) if cell.get("ref") else cell
            cells[column] = {
                "fmt": value.get("fmt", "") if value is not None else "",
                "text": (value.text or "") if value is not None else "",
            }
        result.append(cells)
    return result


def value(row: dict[str, Any], key: str) -> str:
    cell = row.get(key)
    return "" if cell is None else str(cell.get("fmt", ""))


def integer(row: dict[str, Any], key: str) -> int | None:
    cell = row.get(key)
    if cell is None:
        return None
    raw = str(cell.get("text", "")).strip()
    if not raw:
        return None
    try:
        return int(raw, 0)
    except ValueError:
        try:
            return int(raw)
        except ValueError:
            return None


def format_integer(text: str) -> int | None:
    try:
        return int(text.strip(), 0)
    except ValueError:
        return None


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def stats(values: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(values),
        "p50": percentile(values, 0.50),
        "p95": percentile(values, 0.95),
        "p99": percentile(values, 0.99),
        "max": max(values) if values else None,
    }


def gap_rate(times_ns: list[int]) -> tuple[float | None, float | None]:
    if len(times_ns) < 2:
        return None, None
    span = (times_ns[-1] - times_ns[0]) / 1e9
    return span, (len(times_ns) - 1) / span if span > 0 else None


def app_process(rows: list[dict[str, Any]], process_substring: str) -> list[dict[str, Any]]:
    if not process_substring:
        return rows
    return [row for row in rows if process_substring in value(row, "process")]


def align_native_csv(
    native_csv: Path,
    request_times: list[int],
    process_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    if not native_csv.exists() or not request_times:
        return None
    with native_csv.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or "present_call_return_mach_ns" not in rows[0]:
        return None
    app_rows = [row for row in rows if int(row.get("present_call_return_mach_ns", "0") or 0) > 0]
    if len(app_rows) < len(request_times):
        return {"status": "INSUFFICIENT_NATIVE_ROWS", "native_rows": len(app_rows), "trace_requests": len(request_times)}
    trace_times = request_times
    candidates = []
    last_offset = len(app_rows) - len(trace_times)
    for start in range(last_offset + 1):
        diffs = [int(app_rows[start + i]["present_call_return_mach_ns"]) - trace_times[i] for i in range(len(trace_times))]
        offset = statistics.median(diffs)
        residuals = sorted(abs(diff - offset) for diff in diffs)
        p95_index = min(len(residuals) - 1, math.floor(0.95 * (len(residuals) - 1)))
        candidates.append((statistics.median(residuals), start, offset, residuals[p95_index], residuals[-1]))
    best = min(candidates)
    _, start, offset, residual_p95, residual_max = best
    matched = app_rows[start : start + len(trace_times)]
    start_ns = int(matched[0]["callback_mach_ns"])
    end_ns = int(matched[-1]["callback_mach_ns"])
    span = (end_ns - start_ns) / 1e9
    positive = sum(float(row.get("presented_time_seconds", "0") or 0) > 0 for row in matched)
    feedback = sum(int(row.get("presented_handler_seen", "0") or 0) == 1 for row in matched)
    gpu = sum(int(row.get("gpu_completion_seen", "0") or 0) == 1 for row in matched)
    outputs = Counter(row.get("output_kind", "") for row in matched)
    colors = [
        (row.get("color_red", ""), row.get("color_green", ""), row.get("color_blue", ""))
        for row in matched
    ]
    return {
        "status": "ALIGNED" if residual_p95 <= 2_000_000 else "LOW_CONFIDENCE",
        "native_csv_rows": len(rows),
        "matched_rows": len(matched),
        "native_start_index": start,
        "trace_to_mach_offset_ns": int(offset),
        "alignment_residual_p95_ns": residual_p95,
        "alignment_residual_max_ns": residual_max,
        "window_seconds": span,
        "callback_rate_hz": (len(matched) - 1) / span if span > 0 else None,
        "gpu_completion_count": gpu,
        "presented_handler_count": feedback,
        "positive_presented_time_count": positive,
        "zero_presented_time_count": feedback - positive,
        "output_kind_counts": dict(outputs),
        "unique_colors": len(set(colors)),
        "adjacent_color_changes": sum(a != b for a, b in zip(colors, colors[1:])),
    }


def align_dxmt_csv(native_csv: Path, request_times: list[int]) -> dict[str, Any] | None:
    """Align CA present requests with DXMT drawable.present() return timestamps."""
    if not native_csv.exists() or not request_times:
        return None
    with native_csv.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    if not rows or "present_call_ns" not in rows[0] or "event" not in rows[0]:
        return None
    submissions = [
        row for row in rows
        if row.get("event") == "presentation_stages"
        and int(row.get("present_call_ns", "0") or 0) > 0
    ]
    submissions.sort(key=lambda row: int(row["present_call_ns"]))
    if len(submissions) < len(request_times):
        return {
            "status": "INSUFFICIENT_DXMT_ROWS",
            "dxmt_present_calls": len(submissions),
            "trace_requests": len(request_times),
        }

    candidates = []
    last_offset = len(submissions) - len(request_times)
    for start in range(last_offset + 1):
        diffs = [
            int(submissions[start + i]["present_call_ns"]) - request_times[i]
            for i in range(len(request_times))
        ]
        offset = statistics.median(diffs)
        residuals = sorted(abs(diff - offset) for diff in diffs)
        p95_index = min(len(residuals) - 1, math.floor(0.95 * (len(residuals) - 1)))
        candidates.append((statistics.median(residuals), start, offset, residuals[p95_index], residuals[-1]))
    _, start, offset, residual_p95, residual_max = min(candidates)
    matched = submissions[start : start + len(request_times)]
    span = (request_times[-1] - request_times[0]) / 1e9
    kinds = Counter(row.get("kind", "") for row in matched)
    feedback_by_call = {
        int(row["present_call_ns"]): row for row in rows
        if row.get("event") == "presentation_feedback_result"
        and int(row.get("present_call_ns", "0") or 0) > 0
    }
    matched_feedback = [
        feedback_by_call[int(row["present_call_ns"])]
        for row in matched if int(row["present_call_ns"]) in feedback_by_call
    ]
    callback_start = request_times[0] + offset
    callback_end = request_times[-1] + offset
    callback_rows = [
        row for row in rows
        if row.get("event") == "presentation_stages"
        and callback_start <= int(row.get("callback_ns", "0") or 0) <= callback_end
        and int(row.get("callback_ns", "0") or 0) > 0
    ]
    callbacks_by_tick = {int(row["tick_id"]): int(row["callback_ns"]) for row in callback_rows if row.get("tick_id", "0") != "0"}
    callback_times = sorted(callbacks_by_tick.values())
    callback_span = (callback_times[-1] - callback_times[0]) / 1e9 if len(callback_times) > 1 else 0.0
    positive_time = sum(int(row.get("actual_presented_ns", "0") or 0) > 0 for row in matched_feedback)
    zero_time = sum(int(row.get("actual_presented_ns", "0") or 0) == 0 for row in matched_feedback)
    gpu_status = Counter(row.get("gpu_status", "(empty)") or "(empty)" for row in matched_feedback)
    return {
        "status": "ALIGNED" if residual_p95 <= 2_000_000 else "LOW_CONFIDENCE",
        "dxmt_csv_rows": len(rows),
        "present_call_rows": len(submissions),
        "matched_requests": len(matched),
        "dxmt_start_index": start,
        "trace_to_dxmt_mach_offset_ns": int(offset),
        "alignment_residual_p95_ns": residual_p95,
        "alignment_residual_max_ns": residual_max,
        "window_seconds": span,
        "trace_request_rate_hz": (len(request_times) - 1) / span if span > 0 else None,
        "callback_count_in_request_window": len(callback_times),
        "callback_rate_hz_in_request_window": (len(callback_times) - 1) / callback_span if callback_span > 0 else None,
        "matched_source_submissions": sum(kind == "1" for kind in (row.get("kind", "") for row in matched)),
        "matched_generated_submissions": sum(kind == "2" for kind in (row.get("kind", "") for row in matched)),
        "matched_kind_values": dict(kinds),
        "matched_tick_ids": len({row.get("tick_id", "") for row in matched}),
        "matched_terminal_classes": dict(Counter(row.get("terminal_class", "") for row in matched)),
        "matched_feedback_records": len(matched_feedback),
        "positive_presented_time_count": positive_time,
        "zero_presented_time_count": zero_time,
        "missing_feedback_record_count": len(matched) - len(matched_feedback),
        "gpu_status": dict(gpu_status),
    }


def summarize(
    directory: Path,
    process_name: str,
    native_csv: Path | None,
    dxmt_native_csv: Path | None,
) -> dict[str, Any]:
    vsync_rows = parse_table(directory / "display-vsyncs-interval.xml")
    all_surface_rows = parse_table(directory / "displayed-surfaces-interval.xml")
    request_rows = app_process(parse_table(directory / "ca-client-present-request.xml"), process_name)
    handler_rows = app_process(parse_table(directory / "ca-client-presented-handler.xml"), process_name)

    request_surface_ids = {
        format_integer(value(row, "surface-id"))
        for row in request_rows
        if format_integer(value(row, "surface-id")) is not None
    }
    surface_rows = [
        row
        for row in all_surface_rows
        if format_integer(value(row, "surface-id")) in request_surface_ids
        or process_name and process_name in value(row, "event-label")
    ]
    surface_rows.sort(key=lambda row: integer(row, "start") or 0)
    surface_times = [integer(row, "start") for row in surface_rows]
    surface_times = [time for time in surface_times if time is not None]
    surface_span, surface_gap_rate = gap_rate(surface_times)

    durations = [integer(row, "duration") / 1e6 for row in surface_rows if integer(row, "duration") is not None]
    cpu_latencies = [
        integer(row, "cpu-to-display-latency") / 1e6
        for row in surface_rows
        if integer(row, "cpu-to-display-latency") is not None
    ]
    row_gaps = [(b - a) / 1e6 for a, b in zip(surface_times, surface_times[1:])]
    direct_counts = Counter(value(row, "direct-to-display") or "(empty)" for row in surface_rows)
    reason_counts = Counter(value(row, "detachment-reason") or "(empty)" for row in surface_rows)
    labels = []
    for row in surface_rows:
        match = re.search(r"Frame ([0-9,]+)", value(row, "event-label"))
        if match:
            labels.append(int(match.group(1).replace(",", "")))
    frame_steps = [b - a for a, b in zip(labels, labels[1:])]

    surface_display_names = sorted({value(row, "display-name") for row in surface_rows if value(row, "display-name")})
    vsync_by_display: dict[str, dict[str, Any]] = {}
    for display_name in surface_display_names:
        display_times = [
            integer(row, "timestamp") for row in vsync_rows
            if value(row, "display-name") == display_name
        ]
        display_times = [
            time for time in display_times
            if time is not None and surface_times and surface_times[0] <= time <= surface_times[-1]
        ]
        display_times.sort()
        display_span, display_gap_rate = gap_rate(display_times)
        vsync_by_display[display_name] = {
            "count": len(display_times),
            "span_seconds": display_span,
            "gap_rate_hz": display_gap_rate,
        }
    request_times = [integer(row, "timestamp") for row in request_rows]
    request_times = [time for time in request_times if time is not None]
    request_times.sort()
    handler_times = [integer(row, "timestamp") for row in handler_rows]
    handler_times = [time for time in handler_times if time is not None]
    request_span, request_gap_rate = gap_rate(request_times)
    handler_span, handler_gap_rate = gap_rate(handler_times)

    aligned = align_native_csv(native_csv, request_times, request_rows) if native_csv else None
    dxmt_aligned = align_dxmt_csv(dxmt_native_csv, request_times) if dxmt_native_csv else None
    toc_root = ET.parse(directory / "xctrace-toc.xml").getroot()
    run_duration_text = toc_root.findtext("./run[1]/info/summary/duration")
    return {
        "process_name": process_name,
        "trace_duration_seconds": float(run_duration_text) if run_duration_text else None,
        "application_surface_ids": sorted(request_surface_ids),
        "ca_present_request": {"count": len(request_rows), "span_seconds": request_span, "gap_rate_hz": request_gap_rate},
        "ca_presented_handler": {"count": len(handler_rows), "span_seconds": handler_span, "gap_rate_hz": handler_gap_rate},
        "display_surface_intervals": {
            "count": len(surface_rows),
            "span_seconds": surface_span,
            "row_rate_hz": len(surface_rows) / surface_span if surface_span and surface_span > 0 else None,
            "adjacent_start_gap_rate_hz": surface_gap_rate,
            "duration_ms": stats(durations),
            "adjacent_start_gap_ms": stats(row_gaps),
            "cpu_to_display_latency_ms": stats(cpu_latencies),
            "direct_to_display": dict(direct_counts),
            "direct_to_display_failure_reason": dict(reason_counts),
            "frame_label_count": len(labels),
            "frame_label_step": dict(Counter(frame_steps)),
            "frame_label_step_stats": stats([float(step) for step in frame_steps]),
        },
        "surface_display_names": surface_display_names,
        "vsync_by_surface_display_in_surface_window": vsync_by_display,
        "native_callback_alignment": aligned,
        "dxmt_present_call_alignment": dxmt_aligned,
        "quantile_method": "linear interpolation at rank (n - 1) * p",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("evidence_directory", type=Path)
    parser.add_argument("--process", default="")
    parser.add_argument("--native-csv", type=Path)
    parser.add_argument("--dxmt-native-csv", type=Path)
    parser.add_argument("--json-output", type=Path)
    args = parser.parse_args()
    result = summarize(args.evidence_directory, args.process, args.native_csv, args.dxmt_native_csv)
    rendered = json.dumps(result, indent=2, sort_keys=True)
    print(rendered)
    if args.json_output:
        args.json_output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
