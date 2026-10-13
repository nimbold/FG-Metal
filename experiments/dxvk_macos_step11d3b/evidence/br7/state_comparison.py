#!/usr/bin/env python3
"""Describe target-window telemetry by sustained mode in an analysis directory.

This is a standard-library-only, offline presentation-state comparison tool.
It consumes the derived ``summary.json``, ``frames.csv``,
``window_telemetry.csv`` and ``regime_segments.csv`` files produced by
``br7_analysis.py``. Each run is summarized independently; it never combines
legacy BR6 and revised BR7 runs into a pooled result. Telemetry is correlated
to a frame only with the latest sample at or before its callback timestamp,
and samples older than the configured two-second limit are reported stale.
The report is descriptive temporal context, not causal evidence.

Usage::

    python3 state_comparison.py path/to/analysis-directory

The JSON and Markdown reports are written to that directory unless
``--output-dir`` is supplied.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


STALE_AFTER_NS = 2_000_000_000

# These fields define the observed AppKit/window/layer state tuple. Keep the
# complete geometry and configuration snapshot so reports can distinguish
# window placement from backing scales and drawable extent.
STATE_FIELDS = (
    "window_associated", "window_id", "layer_id",
    "window_key", "window_main", "app_active", "window_occluded",
    "window_visible", "window_miniaturized", "screen_id",
    "window_occlusion_state", "window_alpha", "window_ordered", "window_level",
    "window_frame_x", "window_frame_y", "window_frame_width", "window_frame_height",
    "screen_visible_frame_x", "screen_visible_frame_y",
    "screen_visible_frame_width", "screen_visible_frame_height",
    "window_visible_intersection_x", "window_visible_intersection_y",
    "window_visible_intersection_width", "window_visible_intersection_height",
    "window_visible_intersection_area",
    "screen_backing_scale", "window_backing_scale_factor", "screen_refresh_hz",
    "screen_refresh_available",
    "layer_bounds_x", "layer_bounds_y", "layer_bounds_width", "layer_bounds_height",
    "drawable_width", "drawable_height", "contents_scale",
    "maximum_drawable_count", "allows_next_drawable_timeout", "display_sync_enabled",
    "pixel_format", "framebuffer_only", "layer_opaque", "layer_hidden",
    "layer_masks_to_bounds",
)

NUMERIC_FIELDS = tuple(
    field for field in STATE_FIELDS
    if field not in {
        "window_associated", "window_key", "window_main", "app_active",
        "window_occluded", "window_visible", "window_miniaturized",
        "screen_refresh_available", "window_ordered", "framebuffer_only",
        "allows_next_drawable_timeout", "display_sync_enabled", "layer_opaque",
        "layer_hidden", "layer_masks_to_bounds",
    }
)

BOOLEAN_FIELDS = (
    "window_associated", "window_key", "window_main", "app_active",
    "window_occluded", "window_visible", "window_miniaturized",
    "screen_refresh_available", "window_ordered", "framebuffer_only",
    "allows_next_drawable_timeout", "display_sync_enabled", "layer_opaque",
    "layer_hidden", "layer_masks_to_bounds",
)

GEOMETRY_FIELDS = (
    "window_frame_x", "window_frame_y", "window_frame_width", "window_frame_height",
    "screen_visible_frame_x", "screen_visible_frame_y",
    "screen_visible_frame_width", "screen_visible_frame_height",
    "window_visible_intersection_x", "window_visible_intersection_y",
    "window_visible_intersection_width", "window_visible_intersection_height",
    "window_visible_intersection_area",
    "layer_bounds_x", "layer_bounds_y", "layer_bounds_width", "layer_bounds_height",
    "drawable_width", "drawable_height",
)

SCALE_FIELDS = (
    "screen_backing_scale", "window_backing_scale_factor", "contents_scale",
    "screen_refresh_hz",
)

TRIGGER_FIELDS = ("trigger", "notification_name", "telemetry_sequence", "ts_mach_ns")
NOTIFICATION_TRIGGERS = {"window_notification", "app_notification"}


class ComparisonError(ValueError):
    """Raised when the analysis directory cannot be read coherently."""


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ComparisonError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ComparisonError(f"expected a JSON object in {path}")
    return value


def _read_csv(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.exists():
        return [], [f"missing input: {path.name}"]
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as stream:
            return [dict(row) for row in csv.DictReader(stream)], []
    except (OSError, csv.Error) as exc:
        raise ComparisonError(f"cannot read {path}: {exc}") from exc


def _value(value: Any) -> Any:
    """Convert common CSV scalar spellings without treating empty as zero."""
    if value is None:
        return None
    if isinstance(value, (bool, int, float)):
        return value
    text = str(value).strip()
    if not text:
        return None
    folded = text.casefold()
    if folded in {"true", "yes"}:
        return True
    if folded in {"false", "no"}:
        return False
    try:
        return int(text, 10)
    except ValueError:
        pass
    try:
        number = float(text)
    except ValueError:
        return text
    return number if math.isfinite(number) else None


def _number(value: Any) -> int | float | None:
    value = _value(value)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _integer(value: Any) -> int | None:
    number = _number(value)
    if number is None:
        return None
    try:
        integer = int(number)
    except (OverflowError, ValueError):
        return None
    return integer if integer == number else None


def _state(row: Mapping[str, Any]) -> dict[str, Any]:
    return {field: _value(row.get(field)) for field in STATE_FIELDS}


def _stable_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _mode_revision(run: Mapping[str, Any], analysis_dir: Path) -> dict[str, str]:
    """Label instrumentation lineage without pooling or guessing from outcome."""
    run_id = str(run.get("run_id") or "")
    phase = str(run.get("phase") or "")
    manifest_note = ""
    manifest_path = None
    raw_run_dir = run.get("run_directory")
    if isinstance(raw_run_dir, str) and raw_run_dir:
        candidate = Path(raw_run_dir)
        if not candidate.is_absolute():
            candidate = analysis_dir / candidate
        manifest_path = candidate / "run-manifest.json"
    if manifest_path is not None and manifest_path.is_file():
        try:
            manifest = _read_json(manifest_path)
            manifest_note = str(manifest.get("br7_instrumentation_difference") or "")
        except ComparisonError:
            manifest_note = ""
    combined = " ".join((run_id, phase, manifest_note)).casefold()
    if "revised" in combined or "br7" in combined:
        label = "revised_BR7"
    elif "br6" in combined:
        label = "legacy_BR6"
    else:
        label = "unknown"
    return {"label": label, "basis": "run_id_phase_or_run_manifest", "manifest_note": manifest_note}


def _state_status(callback_ns: int | None, stamps: Sequence[int], telemetry: Sequence[Mapping[str, Any]],
                  stale_after_ns: int) -> tuple[dict[str, Any], Mapping[str, Any] | None]:
    if callback_ns is None or callback_ns <= 0:
        return {"status": "unknown", "reason": "callback_missing", "age_ns": None}, None
    index = bisect.bisect_right(stamps, callback_ns) - 1
    if index < 0:
        return {"status": "unknown", "reason": "no_prior_snapshot", "age_ns": None}, None
    row = telemetry[index]
    age = callback_ns - stamps[index]
    details = {
        "telemetry_sequence": _integer(row.get("telemetry_sequence")),
        "trigger": _value(row.get("trigger")),
        "notification_name": _value(row.get("notification_name")),
        "sample_ts_mach_ns": stamps[index],
        "age_ns": age,
        "age_status": "stale" if age > stale_after_ns else "fresh",
        "window_associated": _value(row.get("window_associated")),
        "window_id": _integer(row.get("window_id")),
        "layer_id": _integer(row.get("layer_id")),
    }
    associated = details["window_associated"] is True and details["window_id"] not in (None, 0)
    details["association_status"] = "associated" if associated else "unassociated"
    if not associated:
        reason = "unassociated_window_and_stale_snapshot" if age > stale_after_ns else "unassociated_window"
        return {"status": "unknown", "reason": reason, **details}, row
    if age > stale_after_ns:
        return {"status": "unknown", "reason": "stale_snapshot", **details}, row
    return {"status": "observed_as_of_prior_snapshot", **details,
            "state": _state(row)}, row


def _outcome(frame: Mapping[str, Any]) -> str:
    enum = _value(frame.get("outcome_enum"))
    category = _value(frame.get("outcome_category"))
    return str(enum or category or "UNKNOWN")


def _callback_time(frame: Mapping[str, Any]) -> int | None:
    value = _integer(frame.get("callback_ts_mach_ns"))
    return value if value is not None and value > 0 else None


def _host_origin(run: Mapping[str, Any]) -> int | None:
    interval = run.get("host_interval")
    if isinstance(interval, Mapping):
        value = _integer(interval.get("start_ns"))
        if value is not None and value > 0:
            return value
    return None


def _field_stats(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in STATE_FIELDS:
        values = [_value(row.get(field)) for row in rows]
        present = [item for item in values if item is not None]
        key_values = sorted({_stable_key(item): item for item in present}.items())
        entry: dict[str, Any] = {
            "sample_count": len(rows),
            "observed_count": len(present),
            "missing_count": len(rows) - len(present),
            "distinct_count": len(key_values),
            "distinct_values": [item for _, item in key_values[:20]],
            "distinct_values_truncated": len(key_values) > 20,
            "single_observed_value": len(key_values) == 1,
        }
        if field in NUMERIC_FIELDS:
            numbers = [item for item in present
                       if isinstance(item, (int, float)) and not isinstance(item, bool)]
            entry["numeric_count"] = len(numbers)
            entry["min"] = min(numbers) if numbers else None
            entry["max"] = max(numbers) if numbers else None
        result[field] = entry
    return result


def _prepare_regimes(regime_rows: Sequence[Mapping[str, Any]], run_id: str,
                     origin: int | None) -> list[dict[str, Any]]:
    prepared = []
    for row in regime_rows:
        if str(row.get("run_id") or "") != run_id:
            continue
        start_elapsed = _integer(row.get("start_elapsed_ns"))
        end_elapsed = _integer(row.get("end_elapsed_ns"))
        start_ns = origin + start_elapsed if origin is not None and start_elapsed is not None else None
        end_ns = origin + end_elapsed if origin is not None and end_elapsed is not None else None
        prepared.append({
            "regime": str(row.get("regime") or "UNKNOWN"),
            "start_elapsed_ns": start_elapsed,
            "end_elapsed_ns": end_elapsed,
            "start_ts_mach_ns": start_ns,
            "end_ts_mach_ns": end_ns,
            "left_censored": _value(row.get("left_censored")),
            "right_censored": _value(row.get("right_censored")),
            "window_count": _integer(row.get("window_count")),
            "source_row": dict(row),
        })
    prepared.sort(key=lambda item: (
        item["start_ts_mach_ns"] if item["start_ts_mach_ns"] is not None else 2**63,
        item["regime"],
    ))
    return prepared


def _regime_for_callback(callback_ns: int | None,
                          regimes: Sequence[Mapping[str, Any]]) -> tuple[int | None, str]:
    if callback_ns is None:
        return None, "callback_missing"
    matches = [index for index, segment in enumerate(regimes)
               if segment.get("start_ts_mach_ns") is not None
               and segment.get("end_ts_mach_ns") is not None
               and int(segment["start_ts_mach_ns"]) <= callback_ns < int(segment["end_ts_mach_ns"])]
    if len(matches) == 1:
        return matches[0], "matched_sustained_segment"
    if len(matches) > 1:
        return None, "overlapping_sustained_segments"
    if not regimes:
        return None, "no_sustained_segments"
    if any(segment.get("start_ts_mach_ns") is None or segment.get("end_ts_mach_ns") is None
           for segment in regimes):
        return None, "host_interval_origin_unavailable"
    return None, "outside_sustained_segments"


def _group_frames(frames: Sequence[Mapping[str, Any]],
                  telemetry: Sequence[Mapping[str, Any]], stamps: Sequence[int],
                  regimes: Sequence[Mapping[str, Any]], stale_after_ns: int) -> tuple[
                      list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    per_regime: list[dict[str, Any]] = []
    for segment in regimes:
        per_regime.append({
            "regime": segment["regime"],
            "start_elapsed_ns": segment["start_elapsed_ns"],
            "end_elapsed_ns": segment["end_elapsed_ns"],
            "start_ts_mach_ns": segment["start_ts_mach_ns"],
            "end_ts_mach_ns": segment["end_ts_mach_ns"],
            "left_censored": segment["left_censored"],
            "right_censored": segment["right_censored"],
            "window_count": segment["window_count"],
            "callback_count": 0,
            "outcome_counts": {},
            "state_status_counts": {},
            "state_tuples": [],
        })
    unassigned: Counter[str] = Counter()
    unassigned_frames: list[dict[str, Any]] = []
    buckets: list[dict[tuple[str, str], dict[str, Any]]] = [defaultdict(dict) for _ in regimes]

    for frame in frames:
        callback = _callback_time(frame)
        segment_index, segment_reason = _regime_for_callback(callback, regimes)
        if segment_index is None:
            unassigned[segment_reason] += 1
            unassigned_frames.append({
                "acquisition_sequence": _integer(frame.get("acquisition_sequence")),
                "app_frame_id": _integer(frame.get("app_frame_id")),
                "callback_ts_mach_ns": callback,
                "outcome": _outcome(frame),
                "reason": segment_reason,
            })
            continue

        correlation, _sample = _state_status(callback, stamps, telemetry, stale_after_ns)
        status_key = str(correlation["status"])
        if status_key == "unknown":
            status_key = "unknown:" + str(correlation.get("reason"))
        outcome = _outcome(frame)
        group_state = correlation.get("state") if correlation.get("status") == "observed_as_of_prior_snapshot" else None
        group_key = (status_key, _stable_key(group_state))
        bucket = buckets[segment_index].get(group_key)
        if bucket is None:
            bucket = {
                "observation_status": correlation["status"],
                "unknown_reason": correlation.get("reason"),
                "state": group_state,
                "callback_count": 0,
                "outcome_counts": Counter(),
                "telemetry_trigger_counts": Counter(),
                "age_ns": [],
                "first_callback_ts_mach_ns": callback,
                "last_callback_ts_mach_ns": callback,
                "example_acquisition_sequences": [],
            }
            buckets[segment_index][group_key] = bucket
        bucket["callback_count"] += 1
        bucket["outcome_counts"][outcome] += 1
        bucket["first_callback_ts_mach_ns"] = min(bucket["first_callback_ts_mach_ns"], callback)
        bucket["last_callback_ts_mach_ns"] = max(bucket["last_callback_ts_mach_ns"], callback)
        age = _integer(correlation.get("age_ns"))
        if age is not None:
            bucket["age_ns"].append(age)
        trigger = _value(correlation.get("trigger"))
        bucket["telemetry_trigger_counts"][str(trigger or "unknown")] += 1
        frame_seq = _integer(frame.get("acquisition_sequence"))
        if frame_seq is not None and len(bucket["example_acquisition_sequences"]) < 8:
            bucket["example_acquisition_sequences"].append(frame_seq)
        current = per_regime[segment_index]
        current["callback_count"] += 1
        current["outcome_counts"][outcome] = current["outcome_counts"].get(outcome, 0) + 1
        current["state_status_counts"][status_key] = current["state_status_counts"].get(status_key, 0) + 1

    for index, groups in enumerate(buckets):
        result_groups = []
        for key in sorted(groups):
            group = groups[key]
            ages = group.pop("age_ns")
            group["outcome_counts"] = dict(sorted(group["outcome_counts"].items()))
            group["telemetry_trigger_counts"] = dict(sorted(group["telemetry_trigger_counts"].items()))
            group["age_ns_stats"] = {
                "count": len(ages),
                "min": min(ages) if ages else None,
                "median": statistics.median(ages) if ages else None,
                "max": max(ages) if ages else None,
            }
            result_groups.append(group)
        per_regime[index]["state_tuples"] = result_groups
        per_regime[index]["outcome_counts"] = dict(sorted(per_regime[index]["outcome_counts"].items()))
        per_regime[index]["state_status_counts"] = dict(sorted(per_regime[index]["state_status_counts"].items()))
    return per_regime, dict(sorted(unassigned.items())), unassigned_frames


def _all_frame_correlation_counts(frames: Sequence[Mapping[str, Any]],
                                  telemetry: Sequence[Mapping[str, Any]],
                                  stamps: Sequence[int], stale_after_ns: int) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    for frame in frames:
        callback = _callback_time(frame)
        correlation, _sample = _state_status(callback, stamps, telemetry, stale_after_ns)
        key = str(correlation["status"])
        if key == "unknown":
            key += ":" + str(correlation.get("reason"))
        counts[key] += 1
    return {
        "frame_count": len(frames),
        "status_counts": dict(sorted(counts.items())),
        "rule": "latest prior sample for every frame with callback_ts_mach_ns, independent of sustained-mode assignment",
    }


def _notification_transitions(rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: (
        _integer(row.get("ts_mach_ns")) if _integer(row.get("ts_mach_ns")) is not None else 2**63,
        _integer(row.get("telemetry_sequence")) if _integer(row.get("telemetry_sequence")) is not None else 2**63,
    ))
    notification_rows = [row for row in ordered if _value(row.get("trigger")) in NOTIFICATION_TRIGGERS]
    events: list[dict[str, Any]] = []
    changed_count = 0
    unchanged_count = 0
    no_prior_count = 0
    prior: Mapping[str, Any] | None = None
    prior_sequence: int | None = None
    for row in ordered:
        state = _state(row)
        trigger = str(_value(row.get("trigger")) or "")
        if trigger in NOTIFICATION_TRIGGERS:
            if prior is None:
                changed_fields = None
                transition = "no_prior_sample"
                no_prior_count += 1
            else:
                old_state = _state(prior)
                changed_fields = [field for field in STATE_FIELDS if old_state[field] != state[field]]
                transition = "changed" if changed_fields else "unchanged"
                if changed_fields:
                    changed_count += 1
                else:
                    unchanged_count += 1
            events.append({
                "telemetry_sequence": _integer(row.get("telemetry_sequence")),
                "ts_mach_ns": _integer(row.get("ts_mach_ns")),
                "trigger": trigger,
                "notification_name": _value(row.get("notification_name")),
                "previous_telemetry_sequence": prior_sequence,
                "transition": transition,
                "changed_fields": changed_fields,
                "window_associated": _value(row.get("window_associated")),
                "window_id": _integer(row.get("window_id")),
                "layer_id": _integer(row.get("layer_id")),
                "state": state,
            })
        prior = row
        prior_sequence = _integer(row.get("telemetry_sequence"))
    return events, {
        "notification_row_count": len(notification_rows),
        "notification_rows_with_state_change": changed_count,
        "notification_rows_without_state_change": unchanged_count,
        "notification_rows_without_prior_sample": no_prior_count,
    }


def _run_revision(analysis_dir: Path, run: Mapping[str, Any]) -> dict[str, str]:
    return _mode_revision(run, analysis_dir)


def _compare_run(run: Mapping[str, Any], frames: Sequence[Mapping[str, Any]],
                 telemetry_rows: Sequence[Mapping[str, Any]],
                 regime_rows: Sequence[Mapping[str, Any]], analysis_dir: Path,
                 warnings: Sequence[str]) -> dict[str, Any]:
    run_id = str(run.get("run_id") or "unknown_run")
    target_frames = [row for row in frames if str(row.get("run_id") or "") == run_id]
    target_telemetry = [row for row in telemetry_rows if str(row.get("run_id") or "") == run_id]
    parsed_telemetry = []
    invalid_telemetry_rows = []
    for index, row in enumerate(target_telemetry, 1):
        stamp = _integer(row.get("ts_mach_ns"))
        if stamp is None or stamp <= 0:
            invalid_telemetry_rows.append({"row_index": index, "reason": "missing_or_invalid_ts_mach_ns"})
            continue
        row = dict(row)
        row["_parsed_ts_mach_ns"] = stamp
        parsed_telemetry.append(row)
    parsed_telemetry.sort(key=lambda row: (
        int(row["_parsed_ts_mach_ns"]),
        _integer(row.get("telemetry_sequence")) or 0,
    ))
    stamps = [int(row["_parsed_ts_mach_ns"]) for row in parsed_telemetry]
    stale_after_ns = STALE_AFTER_NS
    telemetry_info = run.get("telemetry")
    if isinstance(telemetry_info, Mapping):
        configured_stale = _integer(telemetry_info.get("stale_after_ns"))
        if configured_stale is not None and configured_stale >= 0:
            stale_after_ns = configured_stale

    origin = _host_origin(run)
    regimes = _prepare_regimes(regime_rows, run_id, origin)
    per_regime, unassigned_mode_counts, unassigned_frames = _group_frames(
        target_frames, parsed_telemetry, stamps, regimes, stale_after_ns)
    all_frame_status_counts = _all_frame_correlation_counts(
        target_frames, parsed_telemetry, stamps, stale_after_ns)
    notification_events, notification_counts = _notification_transitions(parsed_telemetry)

    trigger_counts = Counter(str(_value(row.get("trigger")) or "unknown") for row in parsed_telemetry)
    sequence_values = [_integer(row.get("telemetry_sequence")) for row in parsed_telemetry]
    sequence_values = [value for value in sequence_values if value is not None]
    sequence_order_valid = all(right > left for left, right in zip(sequence_values, sequence_values[1:]))
    sample_range = {
        "start_ts_mach_ns": min(stamps) if stamps else None,
        "end_ts_mach_ns": max(stamps) if stamps else None,
        "start_sequence": min(sequence_values) if sequence_values else None,
        "end_sequence": max(sequence_values) if sequence_values else None,
    }

    # The per-run field report describes observed invariants/cardinality and
    # numeric ranges; it deliberately does not label variation as a cause.
    field_stats = _field_stats(parsed_telemetry)
    state_change_count = 0
    last_state = None
    for row in parsed_telemetry:
        current = _state(row)
        if last_state is not None and current != last_state:
            state_change_count += 1
        last_state = current

    return {
        "run_id": run_id,
        "phase": run.get("phase"),
        "run_status": run.get("run_status"),
        "run_valid": run.get("run_valid"),
        "target_unix_pid": run.get("target_unix_pid"),
        "instrumentation_revision": _run_revision(analysis_dir, run),
        "pooling": "none; each run and revision is reported independently",
        "host_clock_origin_ns": origin,
        "callback_assignment_basis": "callback_ts_mach_ns against host-origin-relative sustained regime segment bounds",
        "telemetry_correlation": {
            "rule": "latest telemetry row with ts_mach_ns <= callback_ts_mach_ns",
            "maximum_age_ns": stale_after_ns,
            "no_future_sample": True,
            "telemetry_rows": len(target_telemetry),
            "usable_timestamp_rows": len(parsed_telemetry),
            "invalid_timestamp_rows": invalid_telemetry_rows,
            "sample_range": sample_range,
            "trigger_counts": dict(sorted(trigger_counts.items())),
            "sequence_strictly_increasing_in_timestamp_order": sequence_order_valid,
            "notification_transition_counts": notification_counts,
            "state_change_between_any_consecutive_samples": state_change_count,
        },
        "sustained_mode_state_tuples": per_regime,
        "all_frame_telemetry_status_counts": all_frame_status_counts,
        "callbacks_without_mode_assignment": {
            "reason_counts": unassigned_mode_counts,
            "examples": unassigned_frames[:50],
            "examples_truncated": len(unassigned_frames) > 50,
        },
        "notification_transitions": notification_events,
        "per_run_state_invariants_and_ranges": {
            "interpretation": "Observed cardinality and ranges only; no invariant violation or causal conclusion is inferred.",
            "snapshot_count": len(parsed_telemetry),
            "fields": field_stats,
        },
        "input_warnings": list(warnings),
    }


def analyze_directory(analysis_dir: str | Path, output_dir: str | Path | None = None) -> dict[str, Any]:
    """Read one existing analysis directory and write JSON/Markdown reports."""
    analysis_dir = Path(analysis_dir).resolve(strict=True)
    summary_path = analysis_dir / "summary.json"
    if not summary_path.is_file():
        raise ComparisonError(f"missing required input: {summary_path}")
    summary = _read_json(summary_path)
    runs = summary.get("runs")
    if not isinstance(runs, list):
        raise ComparisonError("summary.json must contain a runs array")
    warnings: list[str] = []
    frames, frame_warnings = _read_csv(analysis_dir / "frames.csv")
    telemetry, telemetry_warnings = _read_csv(analysis_dir / "window_telemetry.csv")
    regimes, regime_warnings = _read_csv(analysis_dir / "regime_segments.csv")
    warnings.extend(frame_warnings)
    warnings.extend(telemetry_warnings)
    warnings.extend(regime_warnings)

    comparisons = []
    for run in runs:
        if not isinstance(run, Mapping):
            warnings.append("summary.json contains a non-object run entry")
            continue
        comparisons.append(_compare_run(run, frames, telemetry, regimes, analysis_dir, warnings))

    report = {
        "schema_version": 1,
        "report_kind": "observation_only_window_state_comparison",
        "analysis_directory": str(analysis_dir),
        "source_summary_schema_version": summary.get("schema_version"),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "method_limits": [
            "Every run remains independent; no rates, state tuples, or outcomes are pooled across runs or instrumentation revisions.",
            "Window state is selected only from the latest sample at or before callback_ts_mach_ns; no future sample is used.",
            "Samples older than the per-run stale limit, missing callbacks, missing prior samples, and unassociated windows are explicitly unknown.",
            "Mode membership uses callback timestamp against the analyzer's sustained-segment intervals; callbacks outside segments stay unassigned.",
            "Notification rows are retained even when state is unchanged; changed_fields compare against the immediately preceding telemetry sample.",
            "This is descriptive temporal context and does not infer causality, presentation eligibility, visual quality, or state mutation.",
            "Telemetry rows are read from the target-filtered analysis CSV; raw trace ownership is not revalidated by this utility.",
        ],
        "runs": comparisons,
        "input_warnings": warnings,
    }

    destination = Path(output_dir).resolve() if output_dir is not None else analysis_dir
    destination.mkdir(parents=True, exist_ok=True)
    json_path = destination / "window-state-comparison.json"
    markdown_path = destination / "window-state-comparison.md"
    report["output_paths"] = {"json": str(json_path), "markdown": str(markdown_path)}
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                         encoding="utf-8")
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    return report


def _short_state_label(state: Mapping[str, Any] | None) -> str:
    if state is None:
        return "unknown"
    return ", ".join(
        f"{name}={state.get(name)}" for name in (
            "window_visible", "window_key", "window_main", "app_active",
            "window_occluded", "window_miniaturized", "screen_id",
            "drawable_width", "drawable_height", "window_backing_scale_factor",
            "contents_scale",
        )
    )


def _render_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Window state comparison",
        "",
        "Observation-only summary of target-owned AppKit/window and layer telemetry. "
        "Each run and instrumentation revision remains separate; no cross-run pooling or causal inference is performed.",
        "",
        "Correlation uses the latest telemetry row at or before each callback timestamp, with a two-second stale limit. "
        "Callbacks without a fresh associated window remain explicitly unknown.",
        "",
    ]
    for run in report.get("runs", []):
        lines.extend([
            f"## {run.get('run_id', 'unknown run')}",
            "",
            f"- Phase: `{run.get('phase')}`; status: `{run.get('run_status')}`; valid: `{run.get('run_valid')}`.",
            f"- Instrumentation revision: `{run.get('instrumentation_revision', {}).get('label', 'unknown')}`.",
            f"- Telemetry samples: {run.get('telemetry_correlation', {}).get('usable_timestamp_rows', 0)}; "
            f"notification rows: {run.get('telemetry_correlation', {}).get('notification_transition_counts', {}).get('notification_row_count', 0)}.",
            "",
            "| Sustained mode | Callback frames | Fresh associated | Unknown status counts | State tuples |",
            "|---|---:|---:|---|---:|",
        ])
        segments = run.get("sustained_mode_state_tuples", [])
        if not segments:
            lines.append("| No sustained segment in input | 0 | 0 | — | 0 |")
        for segment in segments:
            status_counts = segment.get("state_status_counts", {})
            fresh = status_counts.get("observed_as_of_prior_snapshot", 0)
            unknown = {key: value for key, value in status_counts.items() if key != "observed_as_of_prior_snapshot"}
            unknown_text = json.dumps(unknown, sort_keys=True) if unknown else "{}"
            lines.append(
                f"| {segment.get('regime')} | {segment.get('callback_count', 0)} | {fresh} | "
                f"`{unknown_text}` | {len(segment.get('state_tuples', []))} |"
            )
        lines.append("")
        for segment in segments:
            lines.extend([f"### {segment.get('regime')} state tuples", ""])
            groups = segment.get("state_tuples", [])
            if not groups:
                lines.extend(["No callback frame was assigned to this segment.", ""])
                continue
            lines.extend(["| Frames | Observation | Outcomes | State |", "|---:|---|---|---|"])
            for group in groups:
                state_label = _short_state_label(group.get("state"))
                unknown = group.get("unknown_reason")
                if unknown:
                    state_label = f"{state_label} ({unknown})"
                lines.append(
                    f"| {group.get('callback_count', 0)} | {group.get('observation_status')} | "
                    f"`{json.dumps(group.get('outcome_counts', {}), sort_keys=True)}` | {state_label} |"
                )
            lines.append("")
        lines.extend([
            "### Notification transitions",
            "",
            "Every notification-triggered sample is retained, including unchanged snapshots. `changed_fields` is relative to the immediately preceding telemetry sample.",
            "",
        ])
        notifications = run.get("notification_transitions", [])
        if not notifications:
            lines.extend(["No notification-triggered telemetry rows were present.", ""])
        else:
            lines.extend(["| Sequence | Trigger / notification | Timestamp (mach ns) | Transition | Changed fields |",
                          "|---:|---|---:|---|---|"])
            for event in notifications:
                label = str(event.get("trigger"))
                if event.get("notification_name"):
                    label += " / " + str(event["notification_name"])
                changed = event.get("changed_fields")
                lines.append(
                    f"| {event.get('telemetry_sequence')} | {label} | {event.get('ts_mach_ns')} | "
                    f"{event.get('transition')} | {', '.join(changed) if changed else ('—' if changed == [] else 'unknown')} |"
                )
            lines.append("")
        fields = run.get("per_run_state_invariants_and_ranges", {}).get("fields", {})
        lines.extend([
            "### Per-run state ranges and observed invariants",
            "",
            "Single-valued fields below are observations within this run, not assertions about behavior outside its telemetry interval.",
            "",
            "| Field | Samples | Distinct values | Range |",
            "|---|---:|---:|---|",
        ])
        for field, details in fields.items():
            values = details.get("distinct_values", [])
            distinct = json.dumps(values, sort_keys=True)
            bounds = "—"
            if details.get("numeric_count", 0):
                bounds = f"{details.get('min')} .. {details.get('max')}"
            lines.append(f"| `{field}` | {details.get('observed_count', 0)} | `{distinct}` | {bounds} |")
        unassigned = run.get("callbacks_without_mode_assignment", {}).get("reason_counts", {})
        lines.extend(["", f"Callbacks without sustained-mode assignment: `{json.dumps(unassigned, sort_keys=True)}`.", ""])
    if report.get("input_warnings"):
        lines.extend(["## Input warnings", ""])
        lines.extend(f"- {warning}" for warning in report["input_warnings"])
        lines.append("")
    lines.append("Unknown, stale, or unassociated telemetry is not used to label a state tuple. No causal claim is made from temporal association.")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("analysis_dir", type=Path, help="directory with summary.json and derived CSV files")
    parser.add_argument("--output-dir", type=Path, help="report destination (default: analysis directory)")
    args = parser.parse_args(argv)
    try:
        report = analyze_directory(args.analysis_dir, args.output_dir)
    except (ComparisonError, OSError) as exc:
        print(f"state comparison failed: {exc}", file=sys.stderr)
        return 2
    outputs = report.get("output_paths", {})
    print(f"runs={len(report.get('runs', []))}")
    print(f"json={outputs.get('json')}")
    print(f"markdown={outputs.get('markdown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
