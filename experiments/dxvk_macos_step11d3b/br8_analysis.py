#!/usr/bin/env python3
"""Offline transition analysis for accepted BR7-compatible presentation runs.

BR8 consumes only the owned, validated frame and telemetry records returned by
``br7_analysis.analyze_run``. Notification timing is descriptive: the analyzer
does not infer that a notification or controller action caused a frame change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

try:  # Support both ``python br8_analysis.py`` and package imports.
    from . import br7_analysis
except ImportError:  # pragma: no cover - exercised by the script entry point
    import br7_analysis  # type: ignore[no-redef]


HERE = Path(__file__).resolve().parent
DEFAULT_OUTPUT = HERE / "evidence" / "br8" / "transition-analysis.json"
PHASE_WINDOW_NS = 10_000_000_000
REGIME_BIN_NS = 1_000_000_000
PATHOLOGY_MIN_CONSECUTIVE_SECONDS = 5
PATHOLOGY_MIN_ACQUISITION_FPS = 75.0
PATHOLOGY_ZERO_SHARE = 0.50
PATHOLOGY_MAX_DISPLAYED_FPS = 30.0
PATHOLOGY_MIN_EXACT_OUTCOME_COVERAGE = 0.90
REFERENCE_HIGH_ACQUISITION_FPS = 75.0
STATE_FIELDS = (
    "window_visible", "window_key", "window_main", "window_occluded",
    "window_miniaturized", "app_active",
)
NOTIFICATION_TRIGGERS = frozenset(("window_notification", "app_notification"))


class AnalysisError(ValueError):
    """Raised when an already-analyzed run cannot support this analysis."""


def _integer(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _state_signature(row: Mapping[str, Any]) -> dict[str, bool]:
    if row.get("window_associated") is not True or _integer(row.get("window_id")) in (None, 0):
        return {}
    return {field: row[field] for field in STATE_FIELDS if isinstance(row.get(field), bool)}


def _changed_fields(left: Mapping[str, Any] | None,
                    right: Mapping[str, Any]) -> list[str] | None:
    if left is None:
        return None
    left_state = _state_signature(left)
    right_state = _state_signature(right)
    comparable = [field for field in STATE_FIELDS
                  if field in left_state and field in right_state]
    if not comparable:
        return None
    return [field for field in comparable if left_state[field] != right_state[field]]


def _event_count(frame: Mapping[str, Any], name: str) -> int | None:
    counts = frame.get("event_counts")
    if not isinstance(counts, Mapping):
        return None
    return _integer(counts.get(name))


def _latency_stats(values: Sequence[int]) -> dict[str, int | float | None]:
    if not values:
        return {"count": 0, "median_ns": None, "p99_ns": None, "max_ns": None}
    return {
        "count": len(values),
        "median_ns": float(statistics.median(values)),
        "p99_ns": br7_analysis._percentile(values, 0.99),
        "max_ns": max(values),
    }


def _request_refresh_phase(frames: Sequence[Mapping[str, Any]],
                           reference: Mapping[str, Any] | None) -> dict[str, Any]:
    period_ns = _integer(reference.get("reference_period_ns")) if reference else None
    presented_ns = (_integer(reference.get("reference_presented_time_mach_ns"))
                    if reference else None)
    valid_reference = period_ns is not None and period_ns > 0 \
        and presented_ns is not None and presented_ns > 0
    request_stamps = [stamp for frame in frames if frame.get("exact_chain") is True
                      and (stamp := _integer(frame.get("request_ts_mach_ns"))) is not None]
    fractions = [((stamp - presented_ns) % period_ns) / period_ns
                 for stamp in request_stamps] if valid_reference else []
    quartiles = [{"quartile": index + 1, "count": 0, "percent": None}
                 for index in range(4)]
    for fraction in fractions:
        quartiles[min(3, int(fraction * 4))]["count"] += 1
    for item in quartiles:
        item["percent"] = 100.0 * item["count"] / len(fractions) if fractions else None
    fraction_stats = {
        "count": len(fractions),
        "min": min(fractions) if fractions else None,
        "mean": statistics.mean(fractions) if fractions else None,
        "p50": br7_analysis._percentile(fractions, 0.5),
        "p90": br7_analysis._percentile(fractions, 0.9),
        "p99": br7_analysis._percentile(fractions, 0.99),
        "max": max(fractions) if fractions else None,
    }
    return {
        "status": "valid_run_wide_reference" if valid_reference else "reference_unavailable",
        "request_count": len(fractions),
        "reference_period_ns": period_ns,
        "reference_presented_time_mach_ns": presented_ns,
        "quartiles": quartiles,
        "phase_fraction_stats": fraction_stats,
        "reference_scope": "fixed full-run BR7 reference; never re-estimated within a notification phase",
        "reference_uncertainty": (
            "Descriptive only: a run-wide first displayed presentedTime may not represent "
            "local refresh phase during this notification interval or state transition."
            if valid_reference else
            "No valid full-run reference period and positive presentedTime are available; "
            "no phase origin was inferred from this interval."
        ),
    }


def _state_profile(telemetry: Sequence[Mapping[str, Any]], start_ns: int,
                   end_ns: int) -> dict[str, Any]:
    samples = [row for row in telemetry
               if (stamp := _integer(row.get("ts_mach_ns"))) is not None
               and start_ns <= stamp < end_ns]
    associated = [row for row in samples if _state_signature(row)]
    unassociated_count = len(samples) - len(associated)
    mini_count = sum(row.get("window_miniaturized") is True for row in associated)
    no_visible_pixels_count = sum(row.get("window_occluded") is True for row in associated)
    visible_pixels_reported_count = sum(row.get("window_occluded") is False for row in associated)
    window_not_visible_count = sum(row.get("window_visible") is False for row in associated)
    if len(associated) < 2 or unassociated_count:
        classification = "INSUFFICIENT_OR_UNASSOCIATED_STATE_SAMPLES"
    elif mini_count == len(associated):
        classification = "MINIMIZED"
    elif no_visible_pixels_count == len(associated):
        classification = "NO_VISIBLE_PIXELS_REPORTED"
    elif mini_count + no_visible_pixels_count == len(associated):
        classification = "MINIMIZED_OR_NO_VISIBLE_PIXELS_REPORTED"
    elif (visible_pixels_reported_count == len(associated) and not mini_count
          and not window_not_visible_count):
        classification = "AT_LEAST_SOME_VISIBLE_PIXELS_REPORTED"
    else:
        classification = "MIXED_STATE_COVERAGE_EXTENT_UNKNOWN"
    suppression_state = classification in {
        "MINIMIZED", "NO_VISIBLE_PIXELS_REPORTED",
        "MINIMIZED_OR_NO_VISIBLE_PIXELS_REPORTED",
    }
    return {
        "classification": classification,
        "sampling_interpretation": "discrete target-owned telemetry samples; not continuous state proof",
        "occlusion_semantics": (
            "window_occluded is the inverse of NSWindowOcclusionStateVisible. "
            "The bit reports whether AppKit sees any visible pixels; it does not "
            "identify a physical coverage extent or the reason pixels are not visible."
        ),
        "sample_count": len(samples),
        "associated_sample_count": len(associated),
        "unassociated_sample_count": unassociated_count,
        "miniaturized_sample_count": mini_count,
        "no_visible_pixels_reported_sample_count": no_visible_pixels_count,
        "visible_pixels_reported_sample_count": visible_pixels_reported_count,
        "window_not_visible_sample_count": window_not_visible_count,
        "observed_display_suppression_state": suppression_state,
    }


def _host_interval(summary: Mapping[str, Any]) -> tuple[int, int]:
    interval = summary.get("host_interval")
    if not isinstance(interval, Mapping):
        raise AnalysisError("BR7 result has no host_interval object")
    start = _integer(interval.get("start_ns"))
    end = _integer(interval.get("end_ns"))
    if start is None or end is None or end < start:
        raise AnalysisError("BR7 result has an invalid host interval")
    return start, end


def _frames_in_interval(frames: Sequence[Mapping[str, Any]], start_ns: int,
                        end_ns: int, timestamp_field: str) -> list[Mapping[str, Any]]:
    return [frame for frame in frames
            if (stamp := _integer(frame.get(timestamp_field))) is not None
            and start_ns <= stamp < end_ns]


def _phase_metrics(frames: Sequence[Mapping[str, Any]], requested_start_ns: int,
                   requested_end_ns: int, observed_start_ns: int,
                   observed_end_ns: int,
                   telemetry: Sequence[Mapping[str, Any]] = (),
                   refresh_reference: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if requested_end_ns < requested_start_ns:
        raise AnalysisError("phase interval ends before it starts")
    start_ns = max(requested_start_ns, observed_start_ns)
    end_ns = min(requested_end_ns, observed_end_ns)
    coverage_ns = max(0, end_ns - start_ns)
    requested_ns = requested_end_ns - requested_start_ns
    acquisition_frames = _frames_in_interval(
        frames, start_ns, end_ns, "host_acquire_ts_mach_ns") if coverage_ns else []
    request_frames = _frames_in_interval(
        frames, start_ns, end_ns, "request_ts_mach_ns") if coverage_ns else []

    displayed = [frame for frame in acquisition_frames
                 if frame.get("exact_chain") is True
                 and frame.get("outcome_category") == "presented"]
    exact_zero = [frame for frame in acquisition_frames
                  if frame.get("exact_chain") is True
                  and frame.get("outcome_category") == "zero"]
    raw_zero = [frame for frame in acquisition_frames
                if _event_count(frame, "callback") == 1
                and _integer(frame.get("callback_ts_mach_ns")) is not None
                and _integer(frame.get("presented_time_mach_ns")) == 0]
    raw_zero_not_displayed = [frame for frame in raw_zero
                              if frame.get("exact_chain") is True
                              and frame.get("outcome_category") == "zero"]
    callback_latency_ns = []
    for frame in request_frames:
        if frame.get("exact_chain") is not True:
            continue
        request_ns = _integer(frame.get("request_ts_mach_ns"))
        callback_ns = _integer(frame.get("callback_ts_mach_ns"))
        if request_ns is not None and callback_ns is not None and callback_ns >= request_ns:
            callback_latency_ns.append(callback_ns - request_ns)

    next_drawable_ns = [int(frame["next_drawable_duration_ns"])
                        for frame in acquisition_frames
                        if _event_count(frame, "next_drawable") == 1
                        and _integer(frame.get("next_drawable_duration_ns")) is not None]
    submit_ns = [int(frame["host_submit_duration_ns"])
                 for frame in acquisition_frames
                 if _event_count(frame, "submit") == 1
                 and _integer(frame.get("host_submit_duration_ns")) is not None]

    exact_outcome_count = len(displayed) + len(exact_zero)
    rate_duration_ns = coverage_ns
    acquired_fps = (len(acquisition_frames) * 1_000_000_000 / rate_duration_ns
                    if rate_duration_ns else None)
    displayed_fps = (len(displayed) * 1_000_000_000 / rate_duration_ns
                     if rate_duration_ns else None)
    request_fps = (len(request_frames) * 1_000_000_000 / rate_duration_ns
                   if rate_duration_ns else None)
    state_profile = _state_profile(telemetry, start_ns, end_ns) if coverage_ns else {
        "classification": "NO_OBSERVED_PHASE_COVERAGE",
        "sampling_interpretation": "no target telemetry inside observed phase coverage",
        "occlusion_semantics": "no samples",
        "sample_count": 0, "associated_sample_count": 0, "unassociated_sample_count": 0,
        "miniaturized_sample_count": 0, "no_visible_pixels_reported_sample_count": 0,
        "visible_pixels_reported_sample_count": 0, "window_not_visible_sample_count": 0,
        "observed_display_suppression_state": False,
    }
    if state_profile["observed_display_suppression_state"]:
        state_profile["acquisition_context"] = (
            "fast_acquisition_present; assess historical-pathology criteria separately"
            if acquired_fps is not None and acquired_fps >= PATHOLOGY_MIN_ACQUISITION_FPS
            else "no_fast_acquisition; report minimized/no-visible-pixels state separately"
        )
    else:
        state_profile["acquisition_context"] = "no consistent minimized/no-visible-pixels sample state"
    return {
        "interval": {
            "requested_start_ns": requested_start_ns,
            "requested_end_ns": requested_end_ns,
            "observed_start_ns": start_ns if coverage_ns else None,
            "observed_end_ns": end_ns if coverage_ns else None,
            "requested_duration_ns": requested_ns,
            "observed_coverage_ns": coverage_ns,
            "complete": coverage_ns == requested_ns,
        },
        "acquisition_cohort": {
            "basis": "successful target acquisitions timestamped in the phase",
            "acquired_count": len(acquisition_frames),
            "acquired_fps": acquired_fps,
            "exact_displayed_count": len(displayed),
            "displayed_cohort_fps": displayed_fps,
            "exact_zero_count": len(exact_zero),
            "raw_zero_callback_count": len(raw_zero),
            "raw_zero_not_displayed_count": len(raw_zero_not_displayed),
            "raw_zero_share_of_acquisitions": (len(raw_zero) / len(acquisition_frames)
                                                if acquisition_frames else None),
            "exact_outcome_coverage_of_acquisitions": (exact_outcome_count / len(acquisition_frames)
                                                        if acquisition_frames else None),
            "next_drawable_duration_ns": _latency_stats(next_drawable_ns),
            "next_drawable_ge_100ms_count": sum(value >= 100_000_000
                                                 for value in next_drawable_ns),
            "next_drawable_ge_900ms_count": sum(value >= 900_000_000
                                                 for value in next_drawable_ns),
            "host_submit_duration_ns": _latency_stats(submit_ns),
        },
        "window_state_observation": state_profile,
        "request_phase": {
            "basis": "unique target request timestamps in the phase; latency requires an exact BR7 chain",
            "unique_request_count": len(request_frames),
            "request_fps": request_fps,
            "request_to_callback_latency_ns": _latency_stats(callback_latency_ns),
            "callback_latency_ge_100ms_count": sum(value >= 100_000_000
                                                     for value in callback_latency_ns),
            "callback_latency_ge_900ms_count": sum(value >= 900_000_000
                                                     for value in callback_latency_ns),
            "refresh_phase": _request_refresh_phase(request_frames, refresh_reference),
        },
    }


def _notification_transitions(run_analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    summary = run_analysis.get("summary")
    frames = run_analysis.get("frames")
    telemetry = run_analysis.get("window_telemetry")
    if not isinstance(summary, Mapping) or not isinstance(frames, list) or not isinstance(telemetry, list):
        raise AnalysisError("BR7 result is missing summary, frames, or window_telemetry records")
    observed_start_ns, observed_end_ns = _host_interval(summary)
    refresh_reference = summary.get("refresh_phase")
    if not isinstance(refresh_reference, Mapping):
        refresh_reference = None
    valid_telemetry = [row for row in telemetry if isinstance(row, Mapping)
                       and _integer(row.get("ts_mach_ns")) is not None]
    valid_telemetry.sort(key=lambda row: (_integer(row.get("ts_mach_ns")) or 0,
                                          _integer(row.get("telemetry_sequence")) or 0))
    baseline_row = next((row for row in valid_telemetry
                         if row.get("trigger") not in NOTIFICATION_TRIGGERS
                         and _state_signature(row)), None)
    if baseline_row is None:
        baseline_row = next((row for row in valid_telemetry if _state_signature(row)), None)
    baseline = _state_signature(baseline_row) if baseline_row else {}

    notification_rows = [(index, row) for index, row in enumerate(valid_telemetry)
                         if row.get("trigger") in NOTIFICATION_TRIGGERS
                         and isinstance(row.get("notification_name"), str)
                         and row.get("notification_name")]
    annotated: list[dict[str, Any]] = []
    for index, row in notification_rows:
        timestamp = _integer(row.get("ts_mach_ns"))
        assert timestamp is not None
        previous = valid_telemetry[index - 1] if index > 0 else None
        changed = _changed_fields(previous, row)
        state = _state_signature(row)
        previous_state = _state_signature(previous) if previous else {}
        baseline_comparable = bool(baseline) and all(
            field in state and state[field] == baseline[field] for field in baseline)
        previous_matches_baseline = bool(baseline) and all(
            field in previous_state and previous_state[field] == baseline[field]
            for field in baseline)
        if changed and baseline_comparable and not previous_matches_baseline:
            direction = "restoration_to_initial_observed_state"
        elif changed and previous_matches_baseline and state and not baseline_comparable:
            direction = "change_away_from_initial_observed_state"
        elif changed:
            direction = "other_observed_state_change"
        elif changed == []:
            direction = "notification_without_observed_state_change"
        else:
            direction = "state_change_unknown"
        annotated.append({
            "telemetry_index": index,
            "timestamp_ns": timestamp,
            "telemetry_sequence": row.get("telemetry_sequence"),
            "trigger": row.get("trigger"),
            "notification_name": row.get("notification_name"),
            "state_changed_fields": changed,
            "transition_direction": direction,
            "state_signature": state,
        })

    state_change_timestamps = [item["timestamp_ns"] for item in annotated
                               if item["state_changed_fields"]]
    results: list[dict[str, Any]] = []
    for item in annotated:
        stamp = item["timestamp_ns"]
        next_state_change = next((candidate for candidate in state_change_timestamps
                                  if candidate > stamp), None)
        phases: dict[str, Any] = {
            "before_10s": _phase_metrics(frames, stamp - PHASE_WINDOW_NS, stamp,
                                          observed_start_ns, observed_end_ns, valid_telemetry,
                                          refresh_reference),
            "first_10s_after": _phase_metrics(frames, stamp, stamp + PHASE_WINDOW_NS,
                                              observed_start_ns, observed_end_ns, valid_telemetry,
                                              refresh_reference),
            "steady_hold": None,
            "restoration_10s_after": None,
        }
        if item["state_changed_fields"]:
            hold_end = min(next_state_change if next_state_change is not None else observed_end_ns,
                           observed_end_ns)
            hold_start = stamp + PHASE_WINDOW_NS
            if hold_end > hold_start:
                phases["steady_hold"] = _phase_metrics(
                    frames, hold_start, hold_end, observed_start_ns, observed_end_ns,
                    valid_telemetry, refresh_reference)
        if item["transition_direction"] == "restoration_to_initial_observed_state":
            phases["restoration_10s_after"] = _phase_metrics(
                frames, stamp, stamp + PHASE_WINDOW_NS, observed_start_ns, observed_end_ns,
                valid_telemetry, refresh_reference)
        results.append({
            "timestamp_ns": stamp,
            "elapsed_from_host_start_ns": stamp - observed_start_ns,
            "telemetry_sequence": item["telemetry_sequence"],
            "trigger": item["trigger"],
            "notification_name": item["notification_name"],
            "state_changed_fields": item["state_changed_fields"],
            "transition_direction": item["transition_direction"],
            "state_signature": item["state_signature"],
            "next_state_change_timestamp_ns": next_state_change,
            "phases": phases,
        })
    return results


def _raw_zero_callback(frame: Mapping[str, Any]) -> bool:
    return (_event_count(frame, "callback") == 1
            and _integer(frame.get("callback_ts_mach_ns")) is not None
            and _integer(frame.get("presented_time_mach_ns")) == 0)


def _pathology_assessment(run_analysis: Mapping[str, Any]) -> dict[str, Any]:
    summary = run_analysis.get("summary")
    frames = run_analysis.get("frames")
    if not isinstance(summary, Mapping) or not isinstance(frames, list):
        raise AnalysisError("BR7 result is missing summary or frames")
    start_ns, end_ns = _host_interval(summary)
    telemetry = run_analysis.get("window_telemetry", [])
    telemetry = telemetry if isinstance(telemetry, list) else []
    refresh_rates = sorted({
        float(row["screen_refresh_hz"])
        for row in telemetry if isinstance(row, Mapping)
        and row.get("screen_refresh_available") is True
        and isinstance(row.get("screen_refresh_hz"), (int, float))
        and not isinstance(row.get("screen_refresh_hz"), bool)
        and row["screen_refresh_hz"] > 0
    })
    acquisition_floor_fps = max(
        PATHOLOGY_MIN_ACQUISITION_FPS,
        (max(refresh_rates) * 1.25) if refresh_rates else PATHOLOGY_MIN_ACQUISITION_FPS,
    )
    full_seconds = max(0, (end_ns - start_ns) // REGIME_BIN_NS)
    frames_by_bin: list[list[Mapping[str, Any]]] = [[] for _ in range(full_seconds)]
    for frame in frames:
        stamp = _integer(frame.get("host_acquire_ts_mach_ns"))
        if stamp is None or stamp < start_ns:
            continue
        bin_index = (stamp - start_ns) // REGIME_BIN_NS
        if bin_index < full_seconds:
            frames_by_bin[bin_index].append(frame)
    bins: list[dict[str, Any]] = []
    for index in range(full_seconds):
        cohort = frames_by_bin[index]
        acquired_count = len(cohort)
        zero_count = sum(_raw_zero_callback(frame) for frame in cohort)
        displayed_count = sum(frame.get("exact_chain") is True
                              and frame.get("outcome_category") == "presented"
                              for frame in cohort)
        exact_zero_count = sum(frame.get("exact_chain") is True
                               and frame.get("outcome_category") == "zero"
                               for frame in cohort)
        exact_outcome_count = displayed_count + exact_zero_count
        exact_coverage = (exact_outcome_count / acquired_count
                          if acquired_count else 0.0)
        exact_zero_share = (exact_zero_count / exact_outcome_count
                            if exact_outcome_count else 0.0)
        acquired_fps = acquired_count
        zero_share = zero_count / acquired_count if acquired_count else 0.0
        high_enough = acquired_fps >= acquisition_floor_fps
        outcome_pattern = (zero_share >= PATHOLOGY_ZERO_SHARE
                           and exact_zero_share >= PATHOLOGY_ZERO_SHARE
                           and displayed_count <= PATHOLOGY_MAX_DISPLAYED_FPS)
        coverage_sufficient = exact_coverage >= PATHOLOGY_MIN_EXACT_OUTCOME_COVERAGE
        qualifies = high_enough and outcome_pattern and coverage_sufficient
        if not acquired_count:
            reason = "no_acquisitions"
        elif not high_enough:
            reason = "acquisition_rate_below_above_refresh_floor"
        elif not outcome_pattern:
            reason = "zero_share_or_displayed_rate_outside_threshold"
        elif not coverage_sufficient:
            reason = "exact_outcome_coverage_below_90_percent"
        else:
            reason = "qualifying_pathological_second"
        bins.append({
            "bin_index": index,
            "start_elapsed_ns": index * REGIME_BIN_NS,
            "end_elapsed_ns": (index + 1) * REGIME_BIN_NS,
            "acquired_count": acquired_count,
            "acquired_fps": float(acquired_fps),
            "raw_zero_callback_count": zero_count,
            "raw_zero_share_of_acquisitions": zero_share,
            "exact_displayed_count": displayed_count,
            "exact_zero_outcome_count": exact_zero_count,
            "exact_zero_share_of_exact_outcomes": exact_zero_share,
            "displayed_cohort_fps": float(displayed_count),
            "exact_outcome_coverage_of_acquisitions": exact_coverage,
            "qualifies": qualifies,
            "qualification_reason": reason,
        })

    qualifying = [row for row in bins if row["qualifies"]]
    clusters: list[list[dict[str, Any]]] = []
    for row in qualifying:
        if not clusters or row["bin_index"] != clusters[-1][-1]["bin_index"] + 1:
            clusters.append([row])
        else:
            clusters[-1].append(row)
    sustained = [cluster for cluster in clusters
                 if len(cluster) >= PATHOLOGY_MIN_CONSECUTIVE_SECONDS]
    segments = [{
        "start_elapsed_ns": cluster[0]["start_elapsed_ns"],
        "end_elapsed_ns": cluster[-1]["end_elapsed_ns"],
        "duration_ns": len(cluster) * REGIME_BIN_NS,
        "consecutive_seconds": len(cluster),
        "minimum_acquired_fps": min(row["acquired_fps"] for row in cluster),
        "mean_acquired_fps": statistics.mean(row["acquired_fps"] for row in cluster),
        "maximum_displayed_cohort_fps": max(row["displayed_cohort_fps"] for row in cluster),
        "minimum_raw_zero_share_of_acquisitions": min(
            row["raw_zero_share_of_acquisitions"] for row in cluster),
        "minimum_exact_outcome_coverage": min(
            row["exact_outcome_coverage_of_acquisitions"] for row in cluster),
    } for cluster in sustained]
    low_acquisition_zero_bins = sum(
        row["acquired_count"] > 0
        and row["acquired_fps"] < acquisition_floor_fps
        and row["raw_zero_share_of_acquisitions"] >= PATHOLOGY_ZERO_SHARE
        and row["displayed_cohort_fps"] <= PATHOLOGY_MAX_DISPLAYED_FPS
        for row in bins)
    return {
        "classification": "SUSTAINED_PATHOLOGY_OBSERVED" if segments
                         else "NO_SUSTAINED_PATHOLOGY_OBSERVED",
        "evidence_status": "evaluated" if full_seconds else "no_full_second_bins",
        "sustained_pathology_observed": bool(segments),
        "criteria": {
            "whole_bin_duration_ns": REGIME_BIN_NS,
            "minimum_consecutive_seconds": PATHOLOGY_MIN_CONSECUTIVE_SECONDS,
            "acquired_fps_at_least": acquisition_floor_fps,
            "minimum_above_refresh_ratio": 1.25,
            "minimum_reference_fps": REFERENCE_HIGH_ACQUISITION_FPS,
            "observed_available_screen_refresh_rates_hz": refresh_rates,
            "raw_zero_share_of_acquisitions_at_least": PATHOLOGY_ZERO_SHARE,
            "exact_zero_share_of_exact_outcomes_at_least": PATHOLOGY_ZERO_SHARE,
            "displayed_cohort_fps_at_most": PATHOLOGY_MAX_DISPLAYED_FPS,
            "exact_outcome_coverage_at_least": PATHOLOGY_MIN_EXACT_OUTCOME_COVERAGE,
            "zero_definition": "unique target callback in the acquisition cohort with raw presented_time_mach_ns equal to zero",
            "displayed_definition": "exact BR7 chain classified as presented",
        },
        "qualifying_second_count": len(qualifying),
        "sustained_segments": segments,
        "low_acquisition_zero_bins_excluded": low_acquisition_zero_bins,
        "bins": bins,
        "interpretation": (
            "Low or near-zero acquisition cannot qualify. A sustained segment requires "
            "at least max(75 FPS, 1.25 times the highest target-reported screen refresh) "
            "and at least half of acquisitions with "
            "a unique raw-zero callback, at most 30 exact displayed acquisitions per "
            "second, at least half of exact outcomes classified as zero, at least 90 "
            "percent exact-outcome coverage, and five contiguous "
            "full one-second bins. This is a run-local descriptive classifier."
        ),
    }


def analyze_result(run_analysis: Mapping[str, Any]) -> dict[str, Any]:
    """Derive transition and sustained-state metrics from a BR7 result object."""
    summary = run_analysis.get("summary")
    if not isinstance(summary, Mapping):
        raise AnalysisError("BR7 result has no summary object")
    return {
        "run_id": summary.get("run_id"),
        "run_directory": summary.get("run_directory"),
        "source_files": summary.get("source_files", []),
        "host_interval": summary.get("host_interval"),
        "notification_transitions": _notification_transitions(run_analysis),
        "pathology_assessment": _pathology_assessment(run_analysis),
        "causal_inference": False,
        "method_note": (
            "Notification timestamps and controller actions are descriptive anchors. "
            "No causal relationship is inferred; host and app clocks are not aligned. "
            "Outcome and timing metrics reuse BR7's validated target-owned frame chains."
        ),
    }


def analyze_run(run_dir: str | Path) -> dict[str, Any]:
    """Analyze a valid, already-accepted run through the strict BR7 loader."""
    return analyze_result(br7_analysis.analyze_run(run_dir))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _read_action_log(path: Path) -> dict[str, Any]:
    """Retain action-log records separately; do not interpret or time-join them."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AnalysisError(f"cannot read controller action log {path}: {exc}") from exc
    records: Any
    try:
        records = json.loads(raw)
    except json.JSONDecodeError:
        records = []
        for line_no, line in enumerate(raw.splitlines(), 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                records.append({"unparsed_line_number": line_no, "text": line})
    return {
        "path": str(path.resolve()),
        "sha256": _sha256(path),
        "records": records,
        "used_as_notification_anchor": False,
        "clock_join_performed": False,
    }


def write_analysis(results: Sequence[Mapping[str, Any]], output_path: Path,
                   controller_action_log: Mapping[str, Any] | None = None) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "analysis_kind": "offline_notification_transition_analysis",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "analysis_script_sha256": _sha256(Path(__file__).resolve()),
        "method_limits": [
            "Each raw run is accepted through br7_analysis.analyze_run, which requires PASS/is_valid and one explicitly owned, complete target trace.",
            "Frame outcomes and durations use BR7's unique acquisition and lifecycle joins; ambiguous chains do not count as displayed or latency samples.",
            "Acquisition-cohort outcome rates use the successful target acquisition timestamp; request-phase latency uses the unique request timestamp and a complete exact chain.",
            "Per-phase request refresh quartiles use only BR7's full-run reference period and first positive presentedTime; zero-only phase data never supplies a local phase origin.",
            "The refresh phase reference is descriptive and run-wide; it may not represent local refresh phase during a notification transition.",
            "Per-phase nextDrawable stall counts use unique acquisition-cohort events and inclusive >=100ms and >=900ms thresholds.",
            "Phase intervals are half-open [start,end); rates use observed coverage and incomplete edge windows are marked.",
            "Target notifications are exact target telemetry timestamps. Controller action logs, when supplied, are retained separately and never time-joined.",
            "No causal relationship is inferred from temporal proximity, and app and host clock domains are not aligned.",
            "A pathology segment requires five contiguous full seconds at >=max(75 FPS, 1.25x highest target-reported refresh), at least 50% raw-zero callbacks and exact-zero share, at most 30 displayed cohort FPS, and at least 90% exact outcome coverage.",
            "No process is launched, no live display is read, and no source trace is changed by this analysis.",
        ],
        "controller_action_log": controller_action_log,
        "runs": list(results),
    }
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+", type=Path,
                        help="valid accepted BR7-compatible run directories")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help=f"JSON artifact path (default: {DEFAULT_OUTPUT})")
    parser.add_argument("--controller-action-log", type=Path,
                        help="optional JSON/JSONL/text action log retained separately without clock joining")
    args = parser.parse_args(argv)
    try:
        results = [analyze_run(run_dir) for run_dir in args.run_dirs]
        action_log = _read_action_log(args.controller_action_log) if args.controller_action_log else None
        write_analysis(results, args.output, action_log)
    except (AnalysisError, br7_analysis.AnalysisError, OSError) as exc:
        print(f"BR8 analysis failed: {exc}", file=sys.stderr)
        return 2
    print(args.output)
    return 0


if __name__ == "__main__":  # pragma: no cover - direct CLI is covered by smoke use
    raise SystemExit(main())
