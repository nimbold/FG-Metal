#!/usr/bin/env python3
"""Apply the frozen Z9 rolling-window detector to one target Wine host trace."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

GRID_NS = 50_000_000
PRIMARY_NS = 500_000_000
COMPONENT_NS = 250_000_000
ALLOWED_ORIGINS = {"WINE_SOURCE", "LAYER_SOURCE"}


def read_events(path: Path, pid: int) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"malformed JSON at line {line_number}: {error}") from error
            if not isinstance(row, dict):
                raise ValueError(f"non-object trace row at line {line_number}")
            if row.get("target_unix_pid") != pid or row.get("trace_role") != "target":
                continue
            if row.get("origin") in ALLOWED_ORIGINS:
                events.append(row)
    return events


def required_int(row: dict[str, Any], key: str) -> int | None:
    value = row.get(key)
    return value if type(value) is int and value >= 0 else None


def unique_by(rows: list[dict[str, Any]], key: str
             ) -> tuple[dict[int, dict[str, Any]], list[int], int]:
    result: dict[int, dict[str, Any]] = {}
    duplicates: list[int] = []
    invalid = 0
    for row in rows:
        value = required_int(row, key)
        if value is None:
            invalid += 1
            continue
        if value in result:
            duplicates.append(value)
        else:
            result[value] = row
    return result, duplicates, invalid


def nearest_rank(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile * len(ordered)) - 1)]


def in_window(stamp: int | None, start: int, end: int) -> bool:
    return stamp is not None and start <= stamp < end


def window_metrics(start: int, end: int) -> dict[str, Any]:
    acquire_begins = {
        key: row for key, row in events_by_sequence["successful_acquire_begins"].items()
        if in_window(required_int(row, "ts_mach_ns"), start, end)
    }
    submits = [row for row in events_by_sequence["successful_submits"].values()
               if in_window(required_int(row, "ts_mach_ns"), start, end)]
    callbacks = [row for row in events_by_sequence["callbacks"].values()
                 if in_window(required_int(row, "ts_mach_ns"), start, end)]
    zero = [row for row in callbacks
            if required_int(row, "presented_time_mach_ns") == 0
            and row.get("is_displayed") is False]
    displayed = [row for row in callbacks
                 if (required_int(row, "presented_time_mach_ns") or 0) > 0
                 and row.get("is_displayed") is True]
    callback_outcome_unknown = len(callbacks) - len(zero) - len(displayed)
    drawable_starts = sorted(
        required_int(row, "call_begin_mach_ns")
        for row in events_by_sequence["drawable_starts"].values()
        if in_window(required_int(row, "call_begin_mach_ns"), start, end)
    )
    gaps = [right - left for left, right in zip(drawable_starts, drawable_starts[1:])]
    duration = (end - start) / 1_000_000_000
    return {
        "window_start_mach_ns": start,
        "window_end_mach_ns": end,
        "window_duration_seconds": duration,
        "successful_acquire_count": len(acquire_begins),
        "successful_acquire_rate_per_second": len(acquire_begins) / duration,
        "successful_submit_count": len(submits),
        "successful_submit_rate_per_second": len(submits) / duration,
        "unique_callback_count": len(callbacks),
        "displayed_callback_count": len(displayed),
        "displayed_callback_rate_per_second": len(displayed) / duration,
        "raw_zero_callback_count": len(zero),
        "raw_zero_fraction": len(zero) / len(callbacks) if callbacks else None,
        "callback_outcome_unknown_count": callback_outcome_unknown,
        "next_drawable_start_count": len(drawable_starts),
        "next_drawable_gap_count": len(gaps),
        "next_drawable_gap_median_ns": statistics.median(gaps) if gaps else None,
        "next_drawable_gap_p90_ns": nearest_rank(gaps, 0.9),
    }


events_by_sequence: dict[str, dict[int, dict[str, Any]]]


def prepare_sequences(events: list[dict[str, Any]]) -> tuple[
        dict[str, dict[int, dict[str, Any]]], dict[str, Any], bool]:
    acquire_begins_raw = [row for row in events
                          if row.get("origin") == "WINE_SOURCE"
                          and row.get("event") == "vkAcquireNextImageKHR_begin"]
    acquire_ends_raw = [row for row in events
                        if row.get("origin") == "WINE_SOURCE"
                        and row.get("event") == "vkAcquireNextImageKHR_end"]
    submits_raw = [row for row in events
                   if row.get("origin") == "WINE_SOURCE"
                   and row.get("event") == "vkQueueSubmit2_end"]
    callbacks_raw = [row for row in events
                     if row.get("origin") == "LAYER_SOURCE"
                     and row.get("event") == "presented_callback"]
    drawable_raw = [row for row in events
                    if row.get("origin") == "LAYER_SOURCE"
                    and row.get("event") == "next_drawable_begin"]

    begins, begin_duplicates, invalid_begin_keys = unique_by(acquire_begins_raw, "wine_call_seq")
    ends, end_duplicates, invalid_end_keys = unique_by(acquire_ends_raw, "wine_call_seq")
    successful_acquires = {
        sequence: begins[sequence] for sequence, end in ends.items()
        if sequence in begins and end.get("vk_result") == 0
    }
    successful_submits, submit_duplicates, invalid_submit_keys = unique_by(
        submits_raw, "wine_call_seq")
    successful_submits = {seq: row for seq, row in successful_submits.items()
                          if row.get("vk_result") == 0}
    callbacks, callback_duplicates, invalid_callback_keys = unique_by(
        callbacks_raw, "acquisition_sequence")
    drawable_starts, drawable_duplicates, invalid_drawable_keys = unique_by(
        drawable_raw, "next_drawable_sequence")
    missing_acquire_ends = sorted(set(begins) - set(ends))
    unmatched_acquire_ends = sorted(set(ends) - set(begins))
    unknown_outcomes = [sequence for sequence, row in callbacks.items()
                        if required_int(row, "presented_time_mach_ns") is None
                        or type(row.get("is_displayed")) is not bool]
    diagnostics = {
        "acquire_begin_count": len(acquire_begins_raw),
        "acquire_end_count": len(acquire_ends_raw),
        "successful_acquire_count": len(successful_acquires),
        "successful_submit_count": len(successful_submits),
        "callback_count": len(callbacks_raw),
        "unique_callback_count": len(callbacks),
        "next_drawable_begin_count": len(drawable_raw),
        "duplicate_acquire_begin_sequences": sorted(set(begin_duplicates)),
        "duplicate_acquire_end_sequences": sorted(set(end_duplicates)),
        "duplicate_submit_sequences": sorted(set(submit_duplicates)),
        "duplicate_callback_acquisition_sequences": sorted(set(callback_duplicates)),
        "duplicate_next_drawable_sequences": sorted(set(drawable_duplicates)),
        "missing_acquire_end_sequences": missing_acquire_ends,
        "unmatched_acquire_end_sequences": unmatched_acquire_ends,
        "callback_unknown_outcome_sequences": unknown_outcomes,
        "unkeyed_acquire_begin_count": invalid_begin_keys,
        "unkeyed_acquire_end_count": invalid_end_keys,
        "unkeyed_submit_count": invalid_submit_keys,
        "unkeyed_callback_count": invalid_callback_keys,
        "unkeyed_next_drawable_count": invalid_drawable_keys,
    }
    integrity_ok = not any((begin_duplicates, end_duplicates, submit_duplicates,
                            callback_duplicates, drawable_duplicates,
                            missing_acquire_ends, unmatched_acquire_ends, unknown_outcomes,
                            invalid_begin_keys, invalid_end_keys, invalid_submit_keys,
                            invalid_callback_keys, invalid_drawable_keys))
    return {
        "successful_acquire_begins": successful_acquires,
        "successful_submits": successful_submits,
        "callbacks": callbacks,
        "drawable_starts": drawable_starts,
    }, diagnostics | {"integrity_status": "PASS" if integrity_ok else "FAIL"}, integrity_ok


def evaluation_edges(first_ns: int, last_ns: int, width_ns: int) -> range:
    first_grid = ((first_ns + GRID_NS - 1) // GRID_NS) * GRID_NS
    first_edge = first_grid + width_ns
    last_edge = (last_ns // GRID_NS) * GRID_NS
    return range(first_edge, last_edge + GRID_NS, GRID_NS)


def primary_pass(metrics: dict[str, Any]) -> bool:
    return bool(
        metrics["successful_acquire_count"] >= 45
        and metrics["successful_submit_count"] >= 45
        and metrics["unique_callback_count"] >= 40
        and metrics["displayed_callback_count"] <= 1
        and metrics["callback_outcome_unknown_count"] == 0
        and metrics["raw_zero_fraction"] is not None
        and metrics["raw_zero_fraction"] >= 0.95
        and metrics["next_drawable_gap_count"] >= 20
        and metrics["next_drawable_gap_median_ns"] is not None
        and metrics["next_drawable_gap_median_ns"] <= 12_000_000
        and metrics["next_drawable_gap_p90_ns"] is not None
        and metrics["next_drawable_gap_p90_ns"] <= 14_000_000
    )


def component_pass(metrics: dict[str, Any], component: str) -> bool:
    if component == "acquisition_acceleration":
        return metrics["successful_acquire_count"] >= 24
    if component == "raw_zero_transition":
        return (metrics["unique_callback_count"] >= 20
                and metrics["callback_outcome_unknown_count"] == 0
                and metrics["raw_zero_fraction"] is not None
                and metrics["raw_zero_fraction"] >= 0.95)
    if component == "displayed_collapse":
        return (metrics["unique_callback_count"] >= 20
                and metrics["displayed_callback_count"] <= 1
                and metrics["callback_outcome_unknown_count"] == 0)
    if component == "fast_next_drawable_cadence":
        return (metrics["next_drawable_gap_count"] >= 10
                and metrics["next_drawable_gap_median_ns"] is not None
                and metrics["next_drawable_gap_median_ns"] <= 12_000_000
                and metrics["next_drawable_gap_p90_ns"] is not None
                and metrics["next_drawable_gap_p90_ns"] <= 14_000_000)
    raise ValueError(component)


def transition_for_pair(windows: list[dict[str, Any]], passes: list[bool],
                        width_ns: int, index: int) -> dict[str, Any]:
    candidate = windows[index]
    confirmation = windows[index + 1]
    previous_edge_index = index - 1
    previous_edge_passed = (passes[previous_edge_index]
                            if previous_edge_index >= 0 else None)
    last_nonpassing_edge = next((windows[prior]["window_end_mach_ns"]
                                 for prior in range(index - 1, -1, -1)
                                 if not passes[prior]), None)
    run_end = index + 2
    while run_end < len(passes) and passes[run_end]:
        run_end += 1
    return {
        "immediately_previous_evaluation_edge_mach_ns": (
            windows[previous_edge_index]["window_end_mach_ns"]
            if previous_edge_index >= 0 else None),
        "immediately_previous_evaluation_passed": previous_edge_passed,
        "last_nonpassing_evaluation_edge_mach_ns": last_nonpassing_edge,
        "detector_transition_interval_mach_ns": (
            [last_nonpassing_edge, candidate["window_end_mach_ns"]]
            if last_nonpassing_edge is not None else None),
        "candidate_window_start_mach_ns": candidate["window_start_mach_ns"],
        "candidate_window_end_mach_ns": candidate["window_end_mach_ns"],
        "candidate_metrics": candidate,
        "confirmation_edge_mach_ns": confirmation["window_end_mach_ns"],
        "confirmation_lag_ns": confirmation["window_end_mach_ns"]
        - candidate["window_end_mach_ns"],
        "confirmation_metrics": confirmation,
        "candidate_window_width_ns": width_ns,
        "passing_run_start_edge_mach_ns": candidate["window_end_mach_ns"],
        "passing_run_end_edge_mach_ns": windows[run_end - 1]["window_end_mach_ns"],
        "passing_evaluation_count": run_end - index,
    }


def sustained_transition_runs(windows: list[dict[str, Any]], passes: list[bool],
                              width_ns: int) -> list[dict[str, Any]]:
    transitions = []
    index = 0
    while index < len(passes):
        if not passes[index]:
            index += 1
            continue
        run_start = index
        while index < len(passes) and passes[index]:
            index += 1
        if index - run_start >= 2:
            transitions.append(transition_for_pair(windows, passes, width_ns, run_start))
    return transitions


def first_adjacent_transition(windows: list[dict[str, Any]], passes: list[bool],
                              width_ns: int) -> dict[str, Any] | None:
    transitions = sustained_transition_runs(windows, passes, width_ns)
    return transitions[0] if transitions else None


def event_landmarks(candidate: dict[str, Any] | None) -> dict[str, Any] | None:
    if candidate is None:
        return None
    start = candidate["candidate_window_start_mach_ns"]
    end = candidate["confirmation_edge_mach_ns"]

    def stamps(rows: list[dict[str, Any]], field: str) -> list[int]:
        return sorted(value for row in rows
                      if (value := required_int(row, field)) is not None)

    acquire_rows = list(events_by_sequence["successful_acquire_begins"].values())
    submit_rows = list(events_by_sequence["successful_submits"].values())
    callback_rows = list(events_by_sequence["callbacks"].values())
    drawable_rows = list(events_by_sequence["drawable_starts"].values())
    acquire_times = stamps(acquire_rows, "ts_mach_ns")
    submit_times = stamps(submit_rows, "ts_mach_ns")
    callback_times = stamps(callback_rows, "ts_mach_ns")
    drawable_times = stamps(drawable_rows, "call_begin_mach_ns")
    zero_times = sorted(required_int(row, "ts_mach_ns") for row in callback_rows
                        if required_int(row, "presented_time_mach_ns") == 0
                        and row.get("is_displayed") is False
                        and required_int(row, "ts_mach_ns") is not None)
    displayed_times = sorted(required_int(row, "ts_mach_ns") for row in callback_rows
                             if (required_int(row, "presented_time_mach_ns") or 0) > 0
                             and row.get("is_displayed") is True
                             and required_int(row, "ts_mach_ns") is not None)

    def within(values: list[int]) -> list[int]:
        return [stamp for stamp in values if start <= stamp < end]

    acquire_in_span = within(acquire_times)
    submit_in_span = within(submit_times)
    callback_in_span = within(callback_times)
    zero_in_span = within(zero_times)
    displayed_in_span = within(displayed_times)
    drawable_in_span = within(drawable_times)
    displayed_before = [stamp for stamp in displayed_times if stamp < start]
    return {
        "candidate_and_confirmation_span_mach_ns": [start, end],
        "successful_acquire_begin_first_last_mach_ns": [
            acquire_in_span[0] if acquire_in_span else None,
            acquire_in_span[-1] if acquire_in_span else None],
        "successful_submit_end_first_last_mach_ns": [
            submit_in_span[0] if submit_in_span else None,
            submit_in_span[-1] if submit_in_span else None],
        "callback_delivery_first_last_mach_ns": [
            callback_in_span[0] if callback_in_span else None,
            callback_in_span[-1] if callback_in_span else None],
        "raw_zero_callback_delivery_first_last_mach_ns": [
            zero_in_span[0] if zero_in_span else None,
            zero_in_span[-1] if zero_in_span else None],
        "displayed_callback_delivery_first_last_mach_ns": [
            displayed_in_span[0] if displayed_in_span else None,
            displayed_in_span[-1] if displayed_in_span else None],
        "last_displayed_callback_before_span_mach_ns": (
            displayed_before[-1] if displayed_before else None),
        "first_next_drawable_start_in_span_mach_ns": next(
            iter(drawable_in_span), None),
        "last_next_drawable_start_in_span_mach_ns": (
            drawable_in_span[-1] if drawable_in_span else None),
        "first_raw_zero_callback_in_trace_mach_ns": (
            zero_times[0] if zero_times else None),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("target_pid", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.target_pid <= 1:
        parser.error("target_pid must be an exact positive Wine target PID")
    raw = args.trace.read_bytes()
    events = read_events(args.trace, args.target_pid)
    sequences, diagnostics, integrity_ok = prepare_sequences(events)
    global events_by_sequence
    events_by_sequence = sequences

    timestamps = [required_int(row, "ts_mach_ns") for row in events]
    valid_timestamps = [stamp for stamp in timestamps if stamp is not None]
    if not valid_timestamps:
        raise SystemExit("no target-origin events with valid ts_mach_ns")
    first_ns, last_ns = min(valid_timestamps), max(valid_timestamps)
    primary_windows = [window_metrics(edge - PRIMARY_NS, edge)
                       for edge in evaluation_edges(first_ns, last_ns, PRIMARY_NS)]
    primary_transitions = (sustained_transition_runs(
        primary_windows, [primary_pass(row) for row in primary_windows], PRIMARY_NS)
        if integrity_ok else [])
    primary_transition = primary_transitions[0] if primary_transitions else None
    components: dict[str, Any] = {}
    component_transition_runs: dict[str, Any] = {}
    for name in ("acquisition_acceleration", "raw_zero_transition",
                 "displayed_collapse", "fast_next_drawable_cadence"):
        rows = [window_metrics(edge - COMPONENT_NS, edge)
                for edge in evaluation_edges(first_ns, last_ns, COMPONENT_NS)]
        transitions = (sustained_transition_runs(
            rows, [component_pass(row, name) for row in rows], COMPONENT_NS)
            if integrity_ok else [])
        for transition_row in transitions:
            transition_row["event_landmarks"] = event_landmarks(transition_row)
        transition = transitions[0] if transitions else None
        components[name] = transition
        component_transition_runs[name] = transitions
    for transition_row in primary_transitions:
        transition_row["event_landmarks"] = event_landmarks(transition_row)

    output = {
        "schema_version": 1,
        "detector_source": str(Path(__file__).resolve()),
        "detector_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "trace_path": str(args.trace.resolve()),
        "trace_sha256": hashlib.sha256(raw).hexdigest(),
        "target_pid": args.target_pid,
        "canonical_origins": sorted(ALLOWED_ORIGINS),
        "excluded_origin": "MVK_BOUNDARY",
        "event_count_after_origin_pid_filter": len(events),
        "trace_start_mach_ns": first_ns,
        "trace_end_mach_ns": last_ns,
        "evaluation_grid_ns": GRID_NS,
        "primary_window_ns": PRIMARY_NS,
        "component_window_ns": COMPONENT_NS,
        "primary_thresholds": {
            "successful_acquires_min": 45,
            "successful_submits_min": 45,
            "unique_callbacks_min": 40,
            "displayed_callbacks_max": 1,
            "raw_zero_fraction_min": 0.95,
            "next_drawable_gaps_min": 20,
            "next_drawable_median_max_ns": 12_000_000,
            "next_drawable_nearest_rank_p90_max_ns": 14_000_000,
            "adjacent_passing_windows_required": 2,
        },
        "component_thresholds": {
            "acquisition_acceleration": {"window_ns": COMPONENT_NS, "acquires_min": 24},
            "raw_zero_transition": {"window_ns": COMPONENT_NS, "callbacks_min": 20,
                                     "raw_zero_fraction_min": 0.95},
            "displayed_collapse": {"window_ns": COMPONENT_NS, "callbacks_min": 20,
                                   "displayed_callbacks_max": 1},
            "fast_next_drawable_cadence": {"window_ns": COMPONENT_NS, "gaps_min": 10,
                                            "median_max_ns": 12_000_000,
                                            "nearest_rank_p90_max_ns": 14_000_000},
            "adjacent_passing_windows_required": 2,
        },
        "sequence_integrity": diagnostics,
        "detector_eligible": integrity_ok,
        "primary_transition": primary_transition,
        "primary_transition_runs": primary_transitions,
        "primary_transition_event_landmarks": event_landmarks(primary_transition),
        "component_transitions": components,
        "component_transition_runs": component_transition_runs,
    }
    rendered = json.dumps(output, indent=2, sort_keys=True) + "\n"
    if args.output:
        descriptor = args.output.open("x", encoding="utf-8")
        with descriptor:
            descriptor.write(rendered)
    else:
        print(rendered, end="")
    return 0 if integrity_ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
