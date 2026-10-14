#!/usr/bin/env python3
"""Build deterministic event-aligned metrics and window-set diffs for one Z9 replay."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

import z9_fast_zero_detector as detector

WINDOWS_MS = [(-1000, -500), (-500, 0), (0, 100), (100, 250),
              (250, 500), (500, 1000), (1000, 2000)]
VISIBLE_BIT = 1
FASTZERO_STATE_MAX_AGE_NS = 1_500_000_000


def read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"refusing symlink input: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if path.is_symlink():
        raise ValueError(f"refusing symlink input: {path}")
    result: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"malformed JSON at {path}:{line_no}: {error}") from error
            if isinstance(row, dict):
                result.append(row)
            else:
                raise ValueError(f"non-object JSON at {path}:{line_no}")
    return result


def stamp(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    return value if type(value) is int and value >= 0 else None


def rect(row: dict[str, Any] | None) -> dict[str, float] | None:
    raw = row.get("bounds_cg") if isinstance(row, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        return {key: float(raw[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None


def contains(outer: dict[str, float] | None,
             inner: dict[str, float] | None) -> bool:
    if outer is None or inner is None:
        return False
    return (outer["x"] <= inner["x"] and outer["y"] <= inner["y"]
            and outer["x"] + outer["width"] >= inner["x"] + inner["width"]
            and outer["y"] + outer["height"] >= inner["y"] + inner["height"])


def finder_cover_state(sample: dict[str, Any], target_id: int, finder_id: int,
                       expected_title: str | None = None,
                       expected_layer: int | None = None
                       ) -> tuple[bool, dict[str, Any] | None, dict[str, Any] | None]:
    targets = [row for row in sample.get("onscreen_windows", [])
               if isinstance(row, dict) and row.get("cg_window_id") == target_id]
    finders = [row for row in sample.get("finder_onscreen_windows", [])
               if isinstance(row, dict) and row.get("cg_window_id") == finder_id]
    if len(targets) != 1 or len(finders) != 1:
        return False, None, None
    target, finder = targets[0], finders[0]
    if ((expected_title is not None and finder.get("title") != expected_title)
            or (expected_layer is not None and finder.get("layer") != expected_layer)):
        return False, finder, target
    front = (type(target.get("rank_onscreen_windows")) is int
             and type(finder.get("rank_onscreen_windows")) is int
             and finder["rank_onscreen_windows"] < target["rank_onscreen_windows"])
    alpha = finder.get("alpha")
    full = (sample.get("onscreen_window_query_succeeded") is True
            and target.get("onscreen") is True and finder.get("onscreen") is True
            and isinstance(alpha, (int, float)) and alpha >= 0.999 and front
            and contains(rect(finder), rect(target)))
    return bool(full), finder, target


def geometry_interval(samples: list[dict[str, Any]], target_id: int, finder_id: int,
                      first_proof: dict[str, Any] | None, expected_title: str,
                      expected_layer: int, restored: bool = False) -> dict[str, Any] | None:
    if not isinstance(first_proof, dict):
        return None
    sample_index = first_proof.get("sample_index")
    if type(sample_index) is not int:
        return None
    ordered = sorted((row for row in samples if row.get("event") == "sample"),
                     key=lambda row: row.get("sample_index", -1))
    proof_position = next((i for i, row in enumerate(ordered)
                           if row.get("sample_index") == sample_index), None)
    if proof_position is None:
        return None
    proof_sample = ordered[proof_position]
    proof_cover, proof_finder, proof_target = finder_cover_state(
        proof_sample, target_id, finder_id, expected_title, expected_layer)
    if (proof_sample.get("onscreen_window_query_succeeded") is not True
            or proof_finder is None or proof_target is None or proof_cover == restored):
        return None
    desired_before = restored
    prior = None
    for row in reversed(ordered[:proof_position]):
        covered, prior_finder, prior_target = finder_cover_state(
            row, target_id, finder_id, expected_title, expected_layer)
        if (row.get("onscreen_window_query_succeeded") is True
                and prior_finder is not None and prior_target is not None
                and covered == desired_before):
            prior = row
            break
    if prior is None:
        return None
    begin_key = "onscreen_query_started_mach_absolute_ns"
    end_key = "onscreen_query_completed_mach_absolute_ns"
    lower, upper = stamp(prior, begin_key), stamp(proof_sample, end_key)
    if lower is None or upper is None or lower > upper:
        return None
    return {
        "lower_mach_ns": lower,
        "upper_mach_ns": upper,
        "uncertainty_width_ns": upper - lower,
        "last_opposite_geometry_sample_index": prior.get("sample_index"),
        "first_proof_sample_index": sample_index,
        "last_opposite_query_started_mach_ns": stamp(prior, begin_key),
        "last_opposite_query_completed_mach_ns": stamp(prior, end_key),
        "first_proof_query_started_mach_ns": stamp(proof_sample, begin_key),
        "first_proof_query_completed_mach_ns": stamp(proof_sample, end_key),
        "first_proof_finder_row": proof_finder,
        "first_proof_target_row": proof_target,
        "sample_gap_ns": (stamp(proof_sample, "sample_started_monotonic_ns")
                           - stamp(prior, "sample_started_monotonic_ns")
                           if stamp(proof_sample, "sample_started_monotonic_ns") is not None
                           and stamp(prior, "sample_started_monotonic_ns") is not None else None),
    }


def appkit_state_rows(events: list[dict[str, Any]], start: int, end: int) -> list[dict[str, Any]]:
    keys = ("ts_mach_ns", "notification_name", "trigger", "app_active", "window_key",
            "window_main", "window_visible", "window_miniaturized",
            "window_occlusion_state", "window_occluded", "window_lookup_path",
            "layer_id", "window_view_id", "window_id", "window_number",
            "window_frame_x", "window_frame_y", "window_frame_width", "window_frame_height",
            "screen_id", "window_level", "window_alpha")
    return [{key: row.get(key) for key in keys} for row in events
            if row.get("event") == "window_telemetry"
            and type(row.get("ts_mach_ns")) is int
            and start <= row["ts_mach_ns"] < end]


def fastzero_visible_state(detector_row: dict[str, Any] | None,
                           state_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Resolve whether Visible remained clear or set over the detector evidence span."""
    if not isinstance(detector_row, dict):
        return {"status": "UNAVAILABLE_NO_COVERED_FAST_ZERO_CANDIDATE"}
    candidate_start = detector_row.get("candidate_window_start_mach_ns")
    confirmation_end = detector_row.get("confirmation_edge_mach_ns")
    if (type(candidate_start) is not int or type(confirmation_end) is not int
            or confirmation_end < candidate_start):
        return {"status": "UNAVAILABLE_INVALID_DETECTOR_SPAN"}
    valid_rows = [row for row in state_rows
                  if type(row.get("ts_mach_ns")) is int
                  and type(row.get("window_occlusion_state")) is int
                  and row["ts_mach_ns"] <= confirmation_end]
    before = [row for row in valid_rows if row["ts_mach_ns"] < candidate_start]
    preceding = max(before, key=lambda row: row["ts_mach_ns"], default=None)
    preceding_age = (candidate_start - preceding["ts_mach_ns"]
                     if preceding is not None else None)
    preceding_is_fresh = (type(preceding_age) is int
                          and preceding_age <= FASTZERO_STATE_MAX_AGE_NS)
    response_rows = [row for row in valid_rows
                     if candidate_start <= row["ts_mach_ns"] <= confirmation_end]
    evidence_rows = ([preceding] if preceding_is_fresh and preceding is not None else [])
    evidence_rows.extend(response_rows)
    evidence_rows.sort(key=lambda row: (
        row["ts_mach_ns"], row.get("event_id") if type(row.get("event_id")) is int else -1))
    if not evidence_rows:
        return {
            "status": "UNAVAILABLE_NO_FRESH_STATE_EVIDENCE",
            "candidate_window_start_mach_ns": candidate_start,
            "confirmation_edge_mach_ns": confirmation_end,
            "preceding_state_sample_age_ns": preceding_age,
            "freshness_limit_ns": FASTZERO_STATE_MAX_AGE_NS,
            "response_state_rows": response_rows,
        }

    bits = [bool(row["window_occlusion_state"] & VISIBLE_BIT) for row in evidence_rows]
    transitions = [(left, right) for left, right in zip(bits, bits[1:]) if left != right]
    same_time_conflicts = any(
        left["ts_mach_ns"] == right["ts_mach_ns"]
        and bool(left["window_occlusion_state"] & VISIBLE_BIT)
        != bool(right["window_occlusion_state"] & VISIBLE_BIT)
        for left, right in zip(evidence_rows, evidence_rows[1:]))
    if same_time_conflicts or len(transitions) > 1:
        status = "UNRESOLVED_STATE_TRANSITIONS_DURING_RESPONSE"
    elif transitions == [(True, False)] or (not transitions and bits[-1] is False):
        status = "VISIBLE_CLEAR_CONFIRMED_THROUGH_CONFIRMATION"
    elif transitions == [(False, True)] or (not transitions and bits[-1] is True):
        status = "VISIBLE_SET_CONFIRMED_THROUGH_CONFIRMATION"
    else:
        status = "UNRESOLVED_STATE_TRANSITIONS_DURING_RESPONSE"
    return {
        "status": status,
        "candidate_window_start_mach_ns": candidate_start,
        "confirmation_edge_mach_ns": confirmation_end,
        "preceding_state_sample_mach_ns": (preceding.get("ts_mach_ns")
                                            if preceding_is_fresh and preceding is not None else None),
        "preceding_state_sample_age_ns": preceding_age,
        "freshness_limit_ns": FASTZERO_STATE_MAX_AGE_NS,
        "fresh_preceding_state_used": preceding_is_fresh,
        "response_state_rows": response_rows,
        "state_evidence_rows": evidence_rows,
        "visible_bit_sequence": bits,
        "visible_bit_transition_count": len(transitions),
        "same_timestamp_conflicting_state": same_time_conflicts,
    }


def geometry_state_rows(samples: list[dict[str, Any]], target_id: int, finder_id: int,
                        start: int, end: int, expected_title: str,
                        expected_layer: int) -> list[dict[str, Any]]:
    output = []
    for sample in samples:
        if sample.get("event") != "sample":
            continue
        query_start = stamp(sample, "onscreen_query_started_mach_absolute_ns")
        query_end = stamp(sample, "onscreen_query_completed_mach_absolute_ns")
        if query_start is None or query_end is None or query_end < start or query_start >= end:
            continue
        covered, finder, target = finder_cover_state(
            sample, target_id, finder_id, expected_title, expected_layer)
        output.append({
            "sample_index": sample.get("sample_index"),
            "query_started_mach_ns": query_start,
            "query_completed_mach_ns": query_end,
            "query_succeeded": sample.get("onscreen_window_query_succeeded"),
            "finder_fully_contains_and_precedes_target": covered,
            "finder_row": finder,
            "target_row": target,
        })
    return output


def metrics(start: int, end: int, sequences: dict[str, Any],
            events: list[dict[str, Any]]) -> dict[str, Any]:
    detector.events_by_sequence = sequences
    result = detector.window_metrics(start, end)
    duration = end - start
    present_requests = [row for row in events
                        if row.get("origin") == "WINE_SOURCE"
                        and row.get("event") == "vkQueuePresentKHR_begin"
                        and start <= (stamp(row, "ts_mach_ns") or -1) < end]
    result["present_request_count"] = len(present_requests)
    result["present_request_rate_per_second"] = (
        len(present_requests) * 1_000_000_000 / duration if duration > 0 else None)
    return result


def event_aligned(anchor_name: str, anchor: dict[str, Any] | None,
                  point: bool, trace_start: int, trace_end: int,
                  sequences: dict[str, Any], events: list[dict[str, Any]],
                  samples: list[dict[str, Any]], target_id: int, finder_id: int,
                  finder_title: str, finder_layer: int) -> dict[str, Any]:
    if not isinstance(anchor, dict):
        return {"anchor": anchor_name, "status": "UNAVAILABLE_NO_ANCHOR", "windows": []}
    lower, upper = anchor.get("lower_mach_ns"), anchor.get("upper_mach_ns")
    if point:
        lower = upper = anchor.get("mach_ns")
    if type(lower) is not int or type(upper) is not int or lower > upper:
        return {"anchor": anchor_name, "status": "UNAVAILABLE_INVALID_ANCHOR", "anchor_data": anchor,
                "windows": []}
    output = []
    for offset_start_ms, offset_end_ms in WINDOWS_MS:
        offset_start, offset_end = offset_start_ms * 1_000_000, offset_end_ms * 1_000_000
        endpoints = [lower] if point else sorted(set((lower, upper)))
        endpoint_rows = []
        for endpoint in endpoints:
            start, end = endpoint + offset_start, endpoint + offset_end
            if start < trace_start or end > trace_end:
                endpoint_rows.append({"anchor_endpoint_mach_ns": endpoint,
                                      "status": "UNAVAILABLE_INCOMPLETE_TRACE_COVERAGE"})
                continue
            if not point and start < upper and end > lower:
                endpoint_rows.append({"anchor_endpoint_mach_ns": endpoint,
                                      "status": "UNAVAILABLE_OVERLAPS_ANCHOR_QUERY_UNCERTAINTY"})
                continue
            endpoint_rows.append({
                "anchor_endpoint_mach_ns": endpoint,
                "status": "AVAILABLE",
                "metrics": metrics(start, end, sequences, events),
                "appkit_state_rows": appkit_state_rows(events, start, end),
                "external_geometry_query_rows": geometry_state_rows(
                    samples, target_id, finder_id, start, end,
                    finder_title, finder_layer),
            })
        available = [row["metrics"] for row in endpoint_rows if row.get("status") == "AVAILABLE"]
        range_metrics: dict[str, Any] | None = None
        if len(available) == 2:
            metric_keys = ("successful_acquire_count", "successful_acquire_rate_per_second",
                           "successful_submit_count", "successful_submit_rate_per_second",
                           "present_request_count",
                           "unique_callback_count", "displayed_callback_count",
                           "displayed_callback_rate_per_second", "raw_zero_callback_count",
                           "callback_outcome_unknown_count", "raw_zero_fraction",
                           "next_drawable_gap_count", "next_drawable_gap_median_ns",
                           "next_drawable_gap_p90_ns")
            range_metrics = {key: [available[0].get(key), available[1].get(key)]
                             for key in metric_keys}
        output.append({
            "relative_window_ms": [offset_start_ms, offset_end_ms],
            "anchor_uncertainty_overlaps_window": any(
                row.get("status") == "UNAVAILABLE_OVERLAPS_ANCHOR_QUERY_UNCERTAINTY"
                for row in endpoint_rows),
            "endpoint_metrics_range": range_metrics,
            "endpoint_results": endpoint_rows,
        })
    return {"anchor": anchor_name, "status": "AVAILABLE", "anchor_data": anchor,
            "windows": output}


def summarize_inventory(samples: list[dict[str, Any]], controller: dict[str, Any],
                        phase_anchors: dict[str, tuple[int, int] | None],
                        target_id: int, finder_id: int) -> dict[str, Any]:
    ordered = sorted((row for row in samples if row.get("event") == "sample"),
                     key=lambda row: row.get("sample_index", -1))
    pre_begin = phase_anchors.get("PRE")
    baseline_set: dict[int, dict[str, Any]] = {}
    if pre_begin is not None:
        before = [row for row in ordered
                  if (stamp(row, "onscreen_query_completed_mach_absolute_ns") or 0) <= pre_begin[0]
                  and row.get("all_window_query_succeeded") is True]
        if before:
            for row in before[-1].get("windows", []):
                if isinstance(row, dict) and type(row.get("cg_window_id")) is int:
                    baseline_set[row["cg_window_id"]] = row

    phases: dict[str, Any] = {}
    any_set_or_attribute_mutation = False
    for name, bounds in phase_anchors.items():
        if bounds is None:
            phases[name] = {"status": "UNAVAILABLE_NO_PHASE_BOUNDS"}
            continue
        start, end = bounds
        phase_samples = [row for row in ordered
                         if (stamp(row, "onscreen_query_completed_mach_absolute_ns") or -1) >= start
                         and (stamp(row, "onscreen_query_started_mach_absolute_ns") or (1 << 63)) < end]
        by_id: dict[int, list[dict[str, Any]]] = {}
        for sample in phase_samples:
            for row in sample.get("windows", []):
                if isinstance(row, dict) and type(row.get("cg_window_id")) is int:
                    by_id.setdefault(row["cg_window_id"], []).append(row)
        phase_ids = set(by_id)
        union_rows = {}
        attribute_changed_vs_pre = []
        for cg_id, rows in by_id.items():
            variants = []
            by_fingerprint: dict[str, dict[str, Any]] = {}
            for row in rows:
                attrs = {key: row.get(key) for key in ("bounds_cg", "layer", "alpha", "title",
                                                       "onscreen", "rank_all_windows",
                                                       "rank_onscreen_windows", "on_screen_joined")}
                fingerprint = json.dumps(attrs, sort_keys=True, separators=(",", ":"))
                variant = by_fingerprint.setdefault(fingerprint, {"attributes": attrs,
                                                                   "sample_indices": []})
                variant["sample_indices"].append(row.get("sample_index"))
            variants.extend(by_fingerprint.values())
            baseline = baseline_set.get(cg_id)
            if baseline is not None:
                baseline_attrs = {key: baseline.get(key) for key in ("bounds_cg", "layer", "alpha", "title",
                                                                      "onscreen", "rank_all_windows",
                                                                      "rank_onscreen_windows", "on_screen_joined")}
                if any(variant["attributes"] != baseline_attrs for variant in variants):
                    attribute_changed_vs_pre.append(cg_id)
            union_rows[str(cg_id)] = {
                "observation_count": len(rows),
                "absent_sample_count": len(phase_samples) - len(rows),
                "first_observation_sample_index": min(
                    (row.get("sample_index", -1) for row in rows), default=None),
                "last_observation_sample_index": max(
                    (row.get("sample_index", -1) for row in rows), default=None),
                "attribute_variants": variants,
            }
        new_ids = sorted(phase_ids - set(baseline_set))
        missing_ids = sorted(set(baseline_set) - phase_ids)
        within_phase_attribute_change = any(
            len(row["attribute_variants"]) > 1 for row in union_rows.values())
        any_set_or_attribute_mutation |= bool(
            new_ids or missing_ids or attribute_changed_vs_pre or within_phase_attribute_change)
        phases[name] = {
            "mach_interval_ns": [start, end],
            "sample_count": len(phase_samples),
            "query_failure_count": sum(1 for row in phase_samples
                                       if row.get("all_window_query_succeeded") is not True
                                       or row.get("onscreen_window_query_succeeded") is not True),
            "window_ids": sorted(phase_ids),
            "new_window_ids_vs_pre": new_ids,
            "missing_pre_window_ids": missing_ids,
            "attribute_changed_window_ids_vs_pre": sorted(attribute_changed_vs_pre),
            "within_phase_attribute_changes": within_phase_attribute_change,
            "window_rows": union_rows,
        }
    return {
        "target_cg_window_id": target_id,
        "controlled_finder_cg_window_id": finder_id,
        "pre_window_ids": sorted(baseline_set),
        "phases": phases,
        "wine_window_set_mutation_observed": any_set_or_attribute_mutation,
        "identity_semantics": "CGWindow ID and row attributes only; no helper/child meaning inferred",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("controller", type=Path)
    parser.add_argument("poller", type=Path)
    parser.add_argument("detector", type=Path)
    parser.add_argument("target_pid", type=int)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    controller = read_json(args.controller)
    detector_result = read_json(args.detector)
    poller = read_jsonl(args.poller)
    events = detector.read_events(args.trace, args.target_pid)
    current_trace_sha = hashlib.sha256(args.trace.read_bytes()).hexdigest()
    if (detector_result.get("target_pid") != args.target_pid
            or detector_result.get("trace_sha256") != current_trace_sha
            or controller.get("target_pid") != args.target_pid
            or Path(controller.get("target_trace_path", "")).resolve() != args.trace.resolve()):
        raise SystemExit("trace, detector, controller, and exact target PID bindings disagree")
    detector_source_path = Path(detector_result.get("detector_source", ""))
    if (not detector_source_path.is_file() or detector_source_path.is_symlink()
            or hashlib.sha256(detector_source_path.read_bytes()).hexdigest()
            != detector_result.get("detector_source_sha256")):
        raise SystemExit("detector source does not match the source hash recorded in detector output")
    sequences, integrity, integrity_ok = detector.prepare_sequences(events)
    detector.events_by_sequence = sequences
    trace_stamps = [stamp(row, "ts_mach_ns") for row in events]
    trace_stamps = [value for value in trace_stamps if value is not None]
    if not trace_stamps:
        raise SystemExit("no target trace timestamps")
    trace_start, trace_end = min(trace_stamps), max(trace_stamps)

    target_id = controller.get("canonical_precondition", {}).get(
        "external_target_correlation", {}).get("cg_window_id")
    continuity = controller.get("target_telemetry_identity_continuity", {})
    accepted_tuple = controller.get("accepted_i1_identity_tuple")
    accepted_public_id = controller.get("accepted_public_cg_window_id")
    accepted_start = controller.get("accepted_i1_identity_start_mach_ns")
    identity_keys = ("layer_id", "window_view_id", "window_lookup_path", "window_id", "window_number")
    identity_ok = bool(
        continuity.get("identity_continuous") is True
        and isinstance(accepted_tuple, list) and len(accepted_tuple) == len(identity_keys)
        and type(target_id) is int and target_id == accepted_public_id
        and type(accepted_start) is int)
    if identity_ok:
        identity_ok = all(
            tuple(row.get("telemetry_identity_tuple", [])) == tuple(accepted_tuple)
            and row.get("cg_window_id") == accepted_public_id
            and row.get("status") == "PASS"
            for row in controller.get("target_telemetry_identity_continuity", {}).get(
                "public_cg_identity_samples", [])
            if type(row.get("query_completed_mach_ns")) is int
            and row["query_completed_mach_ns"] >= accepted_start)
    analysis_integrity_ok = bool(integrity_ok and identity_ok)
    finder_identity = controller.get("finder_identity", {})
    finder_id = finder_identity.get("cg_window_id")
    finder_title, finder_layer = finder_identity.get("title"), finder_identity.get("layer")
    if (type(target_id) is not int or type(finder_id) is not int
            or not isinstance(finder_title, str) or type(finder_layer) is not int):
        raise SystemExit("controller lacks exact target or dedicated Finder CGWindow ID")
    all_samples = [row for row in poller if row.get("event") == "sample"]
    cover_interval = geometry_interval(
        all_samples, target_id, finder_id,
        controller.get("finder_full_cover_external_first_proof"),
        finder_title, finder_layer)
    restored_interval = geometry_interval(
        all_samples, target_id, finder_id,
        controller.get("finder_restored_external_first_proof"),
        finder_title, finder_layer, restored=True)

    phase_timeline = controller.get("measured_phases", {})
    pre_begin = stamp(phase_timeline.get("PRE", {}).get("begin", {}), "mach_absolute_time_ns")
    restore_action = next((row for row in controller.get("finder_actions", [])
                           if row.get("action") == "finder_restore_staged_bounds_request"), None)
    restore_action_begin = (restore_action.get("begin", {}).get("mach_absolute_time_ns")
                            if isinstance(restore_action, dict) else None)
    restore_action_begin = restore_action_begin if type(restore_action_begin) is int else None
    measured_end = restore_action_begin
    if measured_end is None:
        fallback_stamps = [
            stamp(phase_timeline.get("RESTORED", {}).get("end", {}), "mach_absolute_time_ns"),
            stamp(phase_timeline.get("COVERED", {}).get("end_request_begin", {}),
                  "mach_absolute_time_ns"),
            stamp(phase_timeline.get("PRE", {}).get("end", {}), "mach_absolute_time_ns"),
        ]
        measured_end = next((value for value in fallback_stamps if type(value) is int), None)
    identity_tuple = tuple(accepted_tuple) if isinstance(accepted_tuple, list) else None

    all_telemetry = [row for row in events if row.get("event") == "window_telemetry"
                     and type(row.get("ts_mach_ns")) is int]
    telemetry = [row for row in all_telemetry
                 if row.get("window_associated") is True
                 and type(accepted_start) is int and row["ts_mach_ns"] >= accepted_start
                 and identity_tuple is not None
                 and tuple(row.get(key) for key in identity_keys) == identity_tuple]
    measured_state_rows = [row for row in telemetry
                           if type(pre_begin) is int
                           and type(measured_end) is int
                           and row["ts_mach_ns"] >= pre_begin
                           and row["ts_mach_ns"] <= measured_end]
    visible_clears = [row for row in measured_state_rows
                      if type(row.get("window_occlusion_state")) is int
                      and not (row["window_occlusion_state"] & VISIBLE_BIT)]
    visible_clear_notifications = [row for row in visible_clears
                                   if row.get("notification_name") ==
                                   "NSWindowDidChangeOcclusionStateNotification"]
    visible_sets = [row for row in telemetry
                    if type(row.get("window_occlusion_state")) is int
                    and row["window_occlusion_state"] & VISIBLE_BIT]
    first_clear = min(visible_clears, key=lambda row: row["ts_mach_ns"]) if visible_clears else None
    first_clear_notification = (min(visible_clear_notifications,
                                    key=lambda row: row["ts_mach_ns"])
                                if visible_clear_notifications else None)
    clear_time = stamp(first_clear_notification, "ts_mach_ns")
    first_clear_state_time = stamp(first_clear, "ts_mach_ns")
    clear_ordering_time = clear_time if clear_time is not None else first_clear_state_time
    ordering_time = clear_ordering_time
    restore_notification = next((row for row in sorted(visible_sets,
                                                       key=lambda item: item["ts_mach_ns"])
                                 if row.get("notification_name") ==
                                 "NSWindowDidChangeOcclusionStateNotification"
                                 and clear_ordering_time is not None
                                 and type(restore_action_begin) is int
                                 and row["ts_mach_ns"] >= restore_action_begin
                                 and row["ts_mach_ns"] > clear_ordering_time), None)
    telemetry_ordered = sorted(telemetry, key=lambda row: row.get("ts_mach_ns", -1))
    appkit_notification_timeline = [{
        "ts_mach_ns": row.get("ts_mach_ns"),
        "notification_name": row.get("notification_name"),
        "trigger": row.get("trigger"),
        "app_active": row.get("app_active"),
        "window_key": row.get("window_key"),
        "window_main": row.get("window_main"),
        "window_visible": row.get("window_visible"),
        "window_miniaturized": row.get("window_miniaturized"),
        "window_occlusion_state": row.get("window_occlusion_state"),
        "window_lookup_path": row.get("window_lookup_path"),
        "layer_id": row.get("layer_id"),
        "window_view_id": row.get("window_view_id"),
        "window_id": row.get("window_id"),
        "window_number": row.get("window_number"),
        "accepted_i1_identity": (type(accepted_start) is int
                                  and row.get("ts_mach_ns", -1) >= accepted_start
                                  and identity_tuple is not None
                                  and tuple(row.get(key) for key in identity_keys) == identity_tuple),
    } for row in sorted(all_telemetry, key=lambda item: item.get("ts_mach_ns", -1))
      if row.get("notification_name") is not None]
    cover_action = next((row for row in controller.get("finder_actions", [])
                         if row.get("action") == "finder_full_cover_position_request"), None)
    cover_action_begin = (cover_action.get("begin", {}).get("mach_absolute_time_ns")
                          if isinstance(cover_action, dict) else None)
    cover_action_end = (cover_action.get("end", {}).get("mach_absolute_time_ns")
                        if isinstance(cover_action, dict) else None)
    covered_lower = cover_interval.get("lower_mach_ns") if cover_interval else None
    covered_upper = restored_interval.get("upper_mach_ns") if restored_interval else None
    state_fields = ("app_active", "window_key", "window_main")
    pre_action_rows = [row for row in telemetry_ordered
                       if type(cover_action_begin) is int
                       and row.get("ts_mach_ns", -1) <= cover_action_begin]
    post_action_rows = [row for row in telemetry_ordered
                        if type(cover_action_begin) is int
                        and row.get("ts_mach_ns", -1) >= cover_action_begin]
    pre_action_state = ({field: pre_action_rows[-1].get(field) for field in state_fields}
                        if pre_action_rows else None)
    active_key_main_effect: dict[str, Any] = {
        "finder_cover_action_begin_mach_ns": cover_action_begin,
        "finder_cover_action_end_mach_ns": cover_action_end,
        "last_observed_state_before_request": pre_action_state,
        "first_observed_state_rows_after_request": [
            {"ts_mach_ns": row.get("ts_mach_ns"),
             "notification_name": row.get("notification_name"),
             **{field: row.get(field) for field in state_fields}}
            for row in post_action_rows[:4]],
        "finder_state_action_timestamps": [
            {"action": row.get("action"),
             "begin_mach_ns": row.get("begin", {}).get("mach_absolute_time_ns"),
             "end_mach_ns": row.get("end", {}).get("mach_absolute_time_ns")}
            for row in controller.get("finder_actions", [])
            if row.get("action") in {"finder_activate_request",
                                      "finder_exact_window_raise_request",
                                      "finder_disjoint_stage_request"}],
    }
    if type(pre_begin) is int:
        measured_ordered = [row for row in telemetry_ordered
                            if row.get("ts_mach_ns", -1) >= pre_begin
                            and type(measured_end) is int
                            and row.get("ts_mach_ns", 1 << 63) <= measured_end]
        active_key_main_effect["first_true_to_false_transitions_from_pre"] = {
            field: next(({
                "ts_mach_ns": row.get("ts_mach_ns"),
                "notification_name": row.get("notification_name"),
                "trigger": row.get("trigger"),
            } for previous, row in zip(measured_ordered, measured_ordered[1:])
                if previous.get(field) is True and row.get(field) is False), None)
            for field in state_fields
        }
    if pre_action_state is not None:
        active_key_main_effect["first_true_to_false_transitions_during_cover"] = {
            field: next(({
                "ts_mach_ns": row.get("ts_mach_ns"),
                "request_offset_ns": row.get("ts_mach_ns") - cover_action_begin,
                "notification_name": row.get("notification_name"),
                "trigger": row.get("trigger"),
            } for previous, row in zip(pre_action_rows[-1:] + post_action_rows[:-1],
                                       post_action_rows)
                if previous.get(field) is True and row.get(field) is False
                and (type(covered_upper) is not int
                     or row.get("ts_mach_ns", 1 << 63) <= covered_upper)), None)
            for field in state_fields
        }
    detector_global_primary = detector_result.get("primary_transition")
    detector_runs = detector_result.get("primary_transition_runs")
    detector_runs = detector_runs if isinstance(detector_runs, list) else []
    covered_transitions = [row for row in detector_runs
                           if isinstance(row, dict)
                           and isinstance(cover_interval, dict)
                           and type(row.get("candidate_window_start_mach_ns")) is int
                           and type(row.get("candidate_window_end_mach_ns")) is int
                           and type(row.get("confirmation_edge_mach_ns")) is int
                           and type(restore_action_begin) is int
                           and row["candidate_window_start_mach_ns"] >= cover_interval["upper_mach_ns"]
                           and row["candidate_window_end_mach_ns"] <= restore_action_begin
                           and row["confirmation_edge_mach_ns"] <= restore_action_begin]
    detector_primary = covered_transitions[0] if covered_transitions else None
    detector_visible_state = fastzero_visible_state(detector_primary, measured_state_rows)
    component_runs = detector_result.get("component_transition_runs", {})
    component_runs = component_runs if isinstance(component_runs, dict) else {}
    component_runs_covered = {
        name: [row for row in rows if isinstance(row, dict)
               and isinstance(cover_interval, dict)
               and type(row.get("candidate_window_start_mach_ns")) is int
               and row["candidate_window_start_mach_ns"] >= cover_interval["upper_mach_ns"]
               and type(restore_action_begin) is int
               and type(row.get("candidate_window_end_mach_ns")) is int
               and row["candidate_window_end_mach_ns"] <= restore_action_begin
               and type(row.get("confirmation_edge_mach_ns")) is int
               and row["confirmation_edge_mach_ns"] <= restore_action_begin]
        for name, rows in component_runs.items() if isinstance(rows, list)
    }
    detector_interval = (detector_primary.get("detector_transition_interval_mach_ns")
                         if isinstance(detector_primary, dict) else None)
    detector_anchor = None
    if isinstance(detector_interval, list) and len(detector_interval) == 2:
        detector_anchor = {"lower_mach_ns": detector_interval[0],
                           "upper_mach_ns": detector_interval[1],
                           "candidate_window_start_mach_ns": detector_primary.get("candidate_window_start_mach_ns"),
                           "candidate_window_end_mach_ns": detector_primary.get("candidate_window_end_mach_ns"),
                           "confirmation_edge_mach_ns": detector_primary.get("confirmation_edge_mach_ns")}

    anchors: dict[str, tuple[dict[str, Any] | None, bool]] = {
        "A_full_cover_geometry": (cover_interval, False),
        "B_visible_clear_notification": (({"mach_ns": clear_time,
            "notification_name": (first_clear_notification or {}).get("notification_name"),
            "first_clear_any_row_mach_ns": first_clear_state_time}
            if clear_time is not None else None), True),
        "B2_first_visible_clear_state_sample": (({"mach_ns": first_clear_state_time,
            "notification_name": (first_clear or {}).get("notification_name"),
            "state_row_mach_ns": first_clear_state_time}
            if first_clear_state_time is not None else None), True),
        "C_sustained_fast_zero_transition": (detector_anchor, False),
        "D_visible_restore_notification": (({"mach_ns": stamp(restore_notification, "ts_mach_ns"),
            "notification_name": restore_notification.get("notification_name")}
            if restore_notification else None), True),
        "E_restored_geometry": (restored_interval, False),
    }
    event_windows = [event_aligned(name, anchor, point, trace_start, trace_end,
                                   sequences, events, all_samples, target_id, finder_id,
                                   finder_title, finder_layer)
                     for name, (anchor, point) in anchors.items()]

    pre = phase_timeline.get("PRE", {})
    covered = phase_timeline.get("COVERED", {})
    restored = phase_timeline.get("RESTORED", {})
    pre_bounds = (stamp(pre.get("begin", {}), "mach_absolute_time_ns"),
                  stamp(pre.get("end", {}), "mach_absolute_time_ns"))
    covered_bounds = (stamp(covered.get("begin", {}), "mach_absolute_time_ns"),
                      (restored_interval or {}).get("upper_mach_ns"))
    restored_bounds = (stamp(restored.get("begin", {}), "mach_absolute_time_ns"),
                       stamp(restored.get("end", {}), "mach_absolute_time_ns"))
    fast_candidate_bounds = (
        detector_primary.get("candidate_window_start_mach_ns"),
        detector_primary.get("confirmation_edge_mach_ns")) if isinstance(detector_primary, dict) else None
    phase_anchors = {
        "PRE": pre_bounds if all(type(value) is int for value in pre_bounds) else None,
        "TRANSITION": ((cover_interval or {}).get("lower_mach_ns"),
                       (cover_interval or {}).get("upper_mach_ns"))
        if cover_interval else None,
        "FAST_ZERO_CANDIDATE": (fast_candidate_bounds
                                if isinstance(fast_candidate_bounds, tuple)
                                and all(type(value) is int for value in fast_candidate_bounds)
                                else None),
        "COVERED": covered_bounds if all(type(value) is int for value in covered_bounds) else None,
        "RESTORED": restored_bounds if all(type(value) is int for value in restored_bounds) else None,
    }
    phase_metrics = {name: metrics(bounds[0], bounds[1], sequences, events)
                     for name, bounds in phase_anchors.items() if bounds is not None}

    restore_start = (restored_interval or {}).get("upper_mach_ns")
    restore_health: dict[str, Any] = {"status": "UNAVAILABLE"}
    if type(restore_start) is int:
        health_end = restore_start + 10_000_000_000
        health_metrics = metrics(restore_start, health_end, sequences, events)
        health_source_rows = [row for row in telemetry
                              if restore_start <= row["ts_mach_ns"] < health_end]
        health_rows = [{key: row.get(key) for key in (
            "ts_mach_ns", "notification_name", "trigger", "app_active", "window_key",
            "window_main", "window_visible", "window_miniaturized", "window_occlusion_state",
            "window_occluded", "window_lookup_path", "layer_id", "window_view_id", "window_id",
            "window_number", "window_frame_x", "window_frame_y", "window_frame_width",
            "window_frame_height", "screen_id", "window_level", "window_alpha")}
                       for row in health_source_rows]
        health_stamps = [row["ts_mach_ns"] for row in health_source_rows]
        health_stamps = [value for value in health_stamps if value is not None]
        visible_throughout_observed = bool(health_stamps) and all(
            type(row.get("window_occlusion_state")) is int
            and (row["window_occlusion_state"] & VISIBLE_BIT)
            for row in health_source_rows)
        health_coverage = (len(health_stamps) >= 8
                           and health_stamps[0] - restore_start <= 1_100_000_000
                           and health_end - health_stamps[-1] <= 1_500_000_000
                           and max((b - a for a, b in zip(health_stamps, health_stamps[1:])),
                                   default=0) <= 1_500_000_000)
        restore_healthy = bool(
            health_metrics["successful_acquire_rate_per_second"] >= 55
            and health_metrics["successful_acquire_rate_per_second"] <= 65
            and health_metrics["unique_callback_count"] >= 550
            and health_metrics["displayed_callback_rate_per_second"] >= 55
            and health_metrics["callback_outcome_unknown_count"] == 0
            and health_metrics["raw_zero_fraction"] is not None
            and health_metrics["raw_zero_fraction"] <= 0.05
            and health_metrics["next_drawable_gap_median_ns"] is not None
            and 14_000_000 <= health_metrics["next_drawable_gap_median_ns"] <= 18_000_000
            and health_metrics["next_drawable_gap_p90_ns"] is not None
            and health_metrics["next_drawable_gap_p90_ns"] <= 22_000_000
            and visible_throughout_observed and health_coverage)
        restore_health = {
            "status": "PASS" if restore_healthy else "FAIL",
            "interval_mach_ns": [restore_start, health_end],
            "metrics": health_metrics,
            "appkit_state_rows": health_rows,
            "visible_bit_set_in_all_observed_rows": visible_throughout_observed,
            "telemetry_coverage_pass": health_coverage,
            "telemetry_row_count": len(health_rows),
        }

    cover_sample_index = (cover_interval or {}).get("first_proof_sample_index")
    restored_sample_index = (restored_interval or {}).get("first_proof_sample_index")
    covered_samples = [row for row in all_samples
                       if type(cover_sample_index) is int and type(restored_sample_index) is int
                       and type(row.get("sample_index")) is int
                       and cover_sample_index <= row["sample_index"] <= restored_sample_index]
    held_samples = [row for row in covered_samples
                    if type(restored_sample_index) is int
                    and row.get("sample_index") < restored_sample_index]
    held_gaps = [stamp(right, "sample_started_monotonic_ns")
                 - stamp(left, "sample_started_monotonic_ns")
                 for left, right in zip(covered_samples, covered_samples[1:])
                 if stamp(left, "sample_started_monotonic_ns") is not None
                 and stamp(right, "sample_started_monotonic_ns") is not None]
    held_fullcover_failures = [row.get("sample_index") for row in held_samples
                               if row.get("all_window_query_succeeded") is not True
                               or row.get("onscreen_window_query_succeeded") is not True
                               or not finder_cover_state(row, target_id, finder_id,
                                                         finder_title, finder_layer)[0]]
    full_cover_samples_observed = controller.get("measured_phases", {}).get(
        "COVERED", {}).get("observed_full_cover_sample_count")
    complete_hold = bool(
        isinstance(cover_interval, dict) and isinstance(restored_interval, dict)
        and controller.get("measured_sequence_completed") is True
        and controller.get("windowserver_collector_summary", {}).get("sample_query_failures") == 0
        and type(full_cover_samples_observed) is int and full_cover_samples_observed >= 350
        and len(held_samples) >= 350 and not held_fullcover_failures
        and max(held_gaps, default=1 << 62) <= 100_000_000
        and type(cover_interval.get("sample_gap_ns")) is int
        and cover_interval["sample_gap_ns"] <= 100_000_000
        and type(restored_interval.get("sample_gap_ns")) is int
        and restored_interval["sample_gap_ns"] <= 100_000_000)
    visible_cleared = first_clear is not None
    fastzero_detected = isinstance(detector_primary, dict)
    complete_hold = bool(complete_hold and controller.get("technical_valid") is True)
    covered_start = (controller.get("measured_phases", {}).get("COVERED", {})
                     .get("begin", {}).get("mach_absolute_time_ns"))
    full_cover_hold_ns = (restore_action_begin - covered_start
                          if type(restore_action_begin) is int and type(covered_start) is int else None)
    complete_hold = bool(complete_hold and type(full_cover_hold_ns) is int
                         and full_cover_hold_ns >= 40_000_000_000)

    if not analysis_integrity_ok or not complete_hold:
        replay_class = "R5"
    elif visible_cleared and not fastzero_detected:
        replay_class = "R2"
    elif (fastzero_detected
          and detector_visible_state.get("status") ==
          "VISIBLE_CLEAR_CONFIRMED_THROUGH_CONFIRMATION"):
        replay_class = "R1" if restore_health.get("status") == "PASS" else "R5"
    elif (fastzero_detected
          and detector_visible_state.get("status") ==
          "VISIBLE_SET_CONFIRMED_THROUGH_CONFIRMATION"):
        replay_class = "R3"
    elif not visible_cleared and not fastzero_detected:
        replay_class = "R4"
    else:
        # A transient clear that is no longer current, or missing/mixed state
        # evidence, does not satisfy any reproducible R1-R4 state pairing.
        replay_class = "R5"

    if not analysis_integrity_ok or not complete_hold:
        timing_class = "T8"
    elif (fastzero_detected and detector_visible_state.get("status") not in {
            "VISIBLE_CLEAR_CONFIRMED_THROUGH_CONFIRMATION",
            "VISIBLE_SET_CONFIRMED_THROUGH_CONFIRMATION"}):
        timing_class = "T8"
    elif visible_cleared and fastzero_detected and type(ordering_time) is int:
        assert isinstance(cover_interval, dict) and isinstance(detector_interval, list)
        assert type(ordering_time) is int
        geometry_upper = cover_interval["upper_mach_ns"]
        fast_lower, fast_upper = detector_interval
        if geometry_upper < ordering_time < fast_lower:
            timing_class = "T1"
        elif geometry_upper < ordering_time and fast_lower <= ordering_time <= fast_upper:
            timing_class = "T2"
        elif fast_upper < ordering_time:
            timing_class = "T3"
        elif ordering_time < fast_lower and geometry_upper >= ordering_time:
            timing_class = "T4"
        else:
            timing_class = "T8"
    elif visible_cleared and fastzero_detected:
        timing_class = "T8"
    elif fastzero_detected and not visible_cleared:
        timing_class = "T5"
    elif visible_cleared and not fastzero_detected:
        timing_class = "T6"
    else:
        timing_class = "T7"

    relative_timing: dict[str, Any] = {
        "geometry_to_visible_clear_ns": None,
        "geometry_to_fast_zero_ns": None,
        "visible_clear_to_fast_zero_ns": None,
    }
    if isinstance(cover_interval, dict) and type(ordering_time) is int:
        relative_timing["geometry_to_visible_clear_ns"] = [
            ordering_time - cover_interval["upper_mach_ns"],
            ordering_time - cover_interval["lower_mach_ns"],
        ]
    if (isinstance(cover_interval, dict) and isinstance(detector_interval, list)
            and len(detector_interval) == 2 and all(type(value) is int for value in detector_interval)):
        relative_timing["geometry_to_fast_zero_ns"] = [
            detector_interval[0] - cover_interval["upper_mach_ns"],
            detector_interval[1] - cover_interval["lower_mach_ns"],
        ]
    if type(ordering_time) is int and isinstance(detector_interval, list) and len(detector_interval) == 2:
        relative_timing["visible_clear_to_fast_zero_ns"] = [
            detector_interval[0] - ordering_time,
            detector_interval[1] - ordering_time,
        ]

    output = {
        "schema_version": 1,
        "analysis_source": str(Path(__file__).resolve()),
        "analysis_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "trace_path": str(args.trace.resolve()),
        "trace_sha256": hashlib.sha256(args.trace.read_bytes()).hexdigest(),
        "controller_path": str(args.controller.resolve()),
        "poller_path": str(args.poller.resolve()),
        "detector_path": str(args.detector.resolve()),
        "target_pid": args.target_pid,
        "target_cg_window_id": target_id,
        "finder_cg_window_id": finder_id,
        "trace_coverage_mach_ns": [trace_start, trace_end],
        "sequence_integrity": integrity,
        "sequence_integrity_ok": integrity_ok,
        "accepted_identity_continuity_ok": identity_ok,
        "geometry_transition": cover_interval,
        "restored_geometry_transition": restored_interval,
        "first_visible_clear_state_row": first_clear,
        "first_visible_clear_notification": first_clear_notification,
        "visible_restore_notification": restore_notification,
        "appkit_notification_timeline": appkit_notification_timeline,
        "active_key_main_request_alignment": active_key_main_effect,
        "fast_zero_transition_global_first": detector_global_primary,
        "fast_zero_transition_runs": detector_runs,
        "fast_zero_transition_in_covered_interval": detector_primary,
        "fast_zero_visible_state": detector_visible_state,
        "component_transition_runs": component_runs,
        "component_transition_runs_in_covered_interval": component_runs_covered,
        "fast_zero_transition": detector_primary,
        "event_aligned_windows": event_windows,
        "phase_metrics": phase_metrics,
        "restoration_healthy_refresh": restore_health,
        "replay_classification": replay_class,
        "timing_classification": timing_class,
        "relative_timing_ns": relative_timing,
        "visible_bit_cleared": visible_cleared,
        "visible_bit_clear_confirmed_through_fast_zero_confirmation": (
            detector_visible_state.get("status") ==
            "VISIBLE_CLEAR_CONFIRMED_THROUGH_CONFIRMATION"),
        "visible_clear_ordering_timestamp_source": (
            "appkit_notification" if clear_time is not None else
            "first_clear_state_sample" if first_clear_state_time is not None else None),
        "sustained_fast_zero_detected": fastzero_detected,
        "finder_full_cover_hold_complete": complete_hold,
        "finder_full_cover_hold_duration_ns": full_cover_hold_ns,
        "finder_full_cover_sample_count": len(held_samples),
        "finder_full_cover_sample_max_gap_ns": max(held_gaps, default=None),
        "finder_full_cover_sample_failures": held_fullcover_failures,
        "controller_technical_valid": controller.get("technical_valid"),
        "window_inventory_diff": summarize_inventory(
            all_samples, controller, phase_anchors, target_id, finder_id),
    }
    rendered = json.dumps(output, indent=2, sort_keys=True) + "\n"
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(rendered)
    return 0 if analysis_integrity_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
