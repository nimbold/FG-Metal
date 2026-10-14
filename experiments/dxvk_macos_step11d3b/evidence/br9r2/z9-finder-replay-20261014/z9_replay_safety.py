"""Fail-closed PRE and public-window evidence checks for Z9 replay."""
from __future__ import annotations

import math
import statistics
from typing import Any

MAX_SAMPLE_GAP_NS = 100_000_000
MIN_COVERED_SAMPLES = 350
MAX_IDENTITY_SAMPLE_GAP_NS = 1_500_000_000
MAX_HEALTHY_RENDERER_EVENT_GAP_NS = 100_000_000

_RENDERER_EVENTS = {
    ("WINE_SOURCE", "vkAcquireNextImageKHR_begin"),
    ("WINE_SOURCE", "vkAcquireNextImageKHR_end"),
    ("WINE_SOURCE", "vkQueueSubmit2_end"),
    ("LAYER_SOURCE", "presented_callback"),
    ("LAYER_SOURCE", "next_drawable_begin"),
}


def replay_execution_path_eligible(record: dict[str, Any]) -> bool:
    """Reject replay records that stopped or errored before normal completion."""
    if (record.get("status") != "RUNNER_COMPLETED"
            or record.get("controller_error") is not None):
        return False
    if "transient_output_budget_guard" not in record:
        return True
    guard = record.get("transient_output_budget_guard")
    return isinstance(guard, dict) and guard.get("triggered") is False


def _stamp(row: dict[str, Any], *, drawable: bool = False) -> int | None:
    value = row.get("call_begin_mach_ns") if drawable else row.get("ts_mach_ns")
    return value if type(value) is int and value > 0 else None


def _renderer_stream_coverage(stamps: list[int], start_ns: int,
                              end_ns: int) -> dict[str, Any]:
    ordered = sorted(stamps)
    gaps = [right - left for left, right in zip(ordered, ordered[1:])]
    first_offset = ordered[0] - start_ns if ordered else None
    last_lag = end_ns - ordered[-1] if ordered else None
    maximum_gap = max(gaps) if gaps else None
    continuous = bool(
        ordered
        and first_offset is not None and 0 <= first_offset <= MAX_HEALTHY_RENDERER_EVENT_GAP_NS
        and last_lag is not None and 0 <= last_lag <= MAX_HEALTHY_RENDERER_EVENT_GAP_NS
        and maximum_gap is not None and maximum_gap <= MAX_HEALTHY_RENDERER_EVENT_GAP_NS
    )
    return {
        "event_count": len(ordered),
        "first_event_offset_from_window_start_ns": first_offset,
        "last_event_lag_to_window_end_ns": last_lag,
        "maximum_inter_event_gap_ns": maximum_gap,
        "maximum_allowed_gap_ns": MAX_HEALTHY_RENDERER_EVENT_GAP_NS,
        "continuous": continuous,
    }


def renderer_healthy(events: list[dict[str, Any]], now_mach_ns: int, *,
                     expected_pid: int, expected_token: str) -> dict[str, Any]:
    """Validate event identity/uniqueness, then apply the established 2 s PRE gate."""
    start = now_mach_ns - 2_000_000_000
    errors: list[str] = []
    selected: list[dict[str, Any]] = []
    for row in events:
        if (not isinstance(row, dict)
                or (row.get("origin"), row.get("event")) not in _RENDERER_EVENTS):
            continue
        stamp = _stamp(row, drawable=row.get("event") == "next_drawable_begin")
        if stamp is None:
            errors.append("renderer event lacks a valid Mach timestamp")
        elif start <= stamp <= now_mach_ns:
            selected.append(row)
    ids: set[int] = set()
    for row in selected:
        event_id = row.get("event_id")
        if type(event_id) is not int or event_id <= 0:
            errors.append("renderer event lacks a positive event_id")
        elif event_id in ids:
            errors.append("renderer event_id is duplicated")
        else:
            ids.add(event_id)
        if (row.get("target_unix_pid") != expected_pid
                or row.get("host_pid") != expected_pid
                or row.get("trace_role") != "target"
                or row.get("experiment_token") != expected_token
                or row.get("ts_clock_domain") != "mach_absolute_time_ns"):
            errors.append("renderer event has a wrong or incomplete PID, role, token, or clock domain")

    begin_rows = [row for row in selected if row.get("event") == "vkAcquireNextImageKHR_begin"]
    all_begins: dict[int, list[dict[str, Any]]] = {}
    all_ends: dict[int, list[dict[str, Any]]] = {}
    for row in events:
        if not isinstance(row, dict) or row.get("origin") != "WINE_SOURCE":
            continue
        sequence = row.get("wine_call_seq")
        if type(sequence) is not int or sequence <= 0:
            if row.get("event") in {"vkAcquireNextImageKHR_begin", "vkAcquireNextImageKHR_end"}:
                errors.append("acquire event lacks a positive wine_call_seq")
            continue
        if row.get("event") == "vkAcquireNextImageKHR_begin":
            all_begins.setdefault(sequence, []).append(row)
        elif row.get("event") == "vkAcquireNextImageKHR_end":
            all_ends.setdefault(sequence, []).append(row)
    for sequence, rows in all_begins.items():
        if len(rows) != 1:
            errors.append("acquire sequence is duplicated")
    for sequence, rows in all_ends.items():
        if len(rows) != 1:
            errors.append("acquire completion sequence is duplicated")
    for row in selected:
        sequence = row.get("wine_call_seq")
        if (row.get("event") == "vkAcquireNextImageKHR_end"
                and type(sequence) is int and len(all_begins.get(sequence, [])) != 1):
            errors.append("in-window acquire completion has no unique begin")

    acquired: list[dict[str, Any]] = []
    for begin in begin_rows:
        sequence = begin.get("wine_call_seq")
        ends = all_ends.get(sequence, []) if type(sequence) is int else []
        if len(ends) != 1:
            errors.append("in-window acquire has no unique completion")
            continue
        begin_stamp = _stamp(begin)
        end_stamp = _stamp(ends[0])
        if begin_stamp is None or end_stamp is None or end_stamp < begin_stamp or end_stamp > now_mach_ns:
            errors.append("acquire begin/end timestamps are invalid or out of order")
            continue
        end = ends[0]
        if (end.get("target_unix_pid") != expected_pid
                or end.get("host_pid") != expected_pid
                or end.get("trace_role") != "target"
                or end.get("experiment_token") != expected_token
                or end.get("ts_clock_domain") != "mach_absolute_time_ns"):
            errors.append("acquire completion identity differs from its target begin")
        if type(end.get("vk_result")) is not int:
            errors.append("acquire completion result is not an integer")
        if type(end.get("vk_result")) is int and end.get("vk_result") == 0:
            acquired.append(begin)

    submits_by_sequence: dict[int, list[dict[str, Any]]] = {}
    submits = [row for row in selected if row.get("event") == "vkQueueSubmit2_end"]
    for row in submits:
        sequence = row.get("wine_call_seq")
        if type(sequence) is not int or sequence <= 0:
            errors.append("submit lacks a positive wine_call_seq")
        else:
            submits_by_sequence.setdefault(sequence, []).append(row)
        if type(row.get("vk_result")) is not int:
            errors.append("submit result is not an integer")
    if any(len(rows) != 1 for rows in submits_by_sequence.values()):
        errors.append("submit sequence is duplicated")
    successful_submits = [row for rows in submits_by_sequence.values()
                          for row in rows if type(row.get("vk_result")) is int
                          and row.get("vk_result") == 0]

    callbacks = [row for row in selected if row.get("event") == "presented_callback"]
    callbacks_by_present: dict[int, list[dict[str, Any]]] = {}
    displayed: list[dict[str, Any]] = []
    zero: list[dict[str, Any]] = []
    for row in callbacks:
        if (row.get("presentation_clock_domain") != "mach_absolute_time_ns"
                or row.get("callback_delivery_clock_domain") != "mach_absolute_time_ns"):
            errors.append("callback presentation or delivery clock domain is invalid")
        present_sequence = row.get("driver_present_seq")
        if type(present_sequence) is not int or present_sequence <= 0:
            errors.append("callback lacks a positive driver_present_seq")
        else:
            callbacks_by_present.setdefault(present_sequence, []).append(row)
        presented_time = row.get("presented_time_mach_ns")
        is_displayed = row.get("is_displayed")
        if type(presented_time) is not int or type(is_displayed) is not bool:
            errors.append("callback outcome is unknown")
        elif presented_time == 0 and is_displayed is False:
            zero.append(row)
        elif presented_time > 0 and is_displayed is True:
            displayed.append(row)
        else:
            errors.append("callback presentation timestamp and display flag disagree")
    if any(len(rows) != 1 for rows in callbacks_by_present.values()):
        errors.append("present callback sequence is duplicated")

    drawable_rows = [row for row in selected if row.get("event") == "next_drawable_begin"]
    drawable_sequences: set[int] = set()
    drawable_starts: list[int] = []
    for row in drawable_rows:
        sequence = row.get("next_drawable_sequence")
        stamp = _stamp(row, drawable=True)
        if type(sequence) is not int or sequence <= 0 or stamp is None:
            errors.append("nextDrawable start lacks a unique sequence or valid call-begin timestamp")
            continue
        if sequence in drawable_sequences:
            errors.append("nextDrawable sequence is duplicated")
        drawable_sequences.add(sequence)
        drawable_starts.append(stamp)

    gaps = [right - left for left, right in zip(sorted(drawable_starts), sorted(drawable_starts)[1:])]
    median = statistics.median(gaps) if gaps else None
    p90 = sorted(gaps)[math.ceil(0.9 * len(gaps)) - 1] if gaps else None
    renderer_stream_coverage = {
        "successful_acquisitions": _renderer_stream_coverage(
            [stamp for row in acquired if (stamp := _stamp(row)) is not None], start, now_mach_ns),
        "successful_submits": _renderer_stream_coverage(
            [stamp for row in successful_submits if (stamp := _stamp(row)) is not None],
            start, now_mach_ns),
        "displayed_callbacks": _renderer_stream_coverage(
            [stamp for row in displayed if (stamp := _stamp(row)) is not None],
            start, now_mach_ns),
        "next_drawable_starts": _renderer_stream_coverage(
            drawable_starts, start, now_mach_ns),
    }
    acquire_rate = len(acquired) / 2
    submit_rate = len(successful_submits) / 2
    display_rate = len(displayed) / 2
    zero_fraction = len(zero) / len(callbacks) if callbacks else 1.0
    integrity_pass = not errors
    continuity_pass = all(row["continuous"] for row in renderer_stream_coverage.values())
    healthy = (integrity_pass and continuity_pass
               and 55 <= acquire_rate <= 65 and 55 <= submit_rate <= 65
               and display_rate >= 55 and zero_fraction <= 0.05
               and median is not None and 14_000_000 <= median <= 18_000_000
               and p90 is not None and p90 <= 22_000_000)
    return {
        "healthy": healthy,
        "integrity_pass": integrity_pass,
        "integrity_errors": sorted(set(errors)),
        "stream_coverage_pass": continuity_pass,
        "renderer_stream_coverage": renderer_stream_coverage,
        "window_ns": [start, now_mach_ns],
        "acquire_rate_per_second": acquire_rate,
        "submit_rate_per_second": submit_rate,
        "display_rate_per_second": display_rate,
        "raw_zero_fraction": zero_fraction,
        "callback_count": len(callbacks),
        "unique_acquisition_count": len(acquired),
        "successful_submit_count": len(successful_submits),
        "next_drawable_gap_median_ns": median,
        "next_drawable_gap_p90_ns": p90,
    }


def same_query_cover_state(sample: dict[str, Any], *,
                           allowed_cover_identity: tuple[int, int] | None = None) -> dict[str, Any]:
    evidence = sample.get("same_query_cover_evidence")
    if (not isinstance(evidence, dict)
            or evidence.get("cover_evidence_complete") is not True
            or evidence.get("target_join_status") != "PASS"):
        return {"status": "UNKNOWN", "cover_rows": []}
    target_pid = sample.get("target_unix_pid")
    target_id = evidence.get("target_cg_window_id")
    target_rank = evidence.get("target_onscreen_rank")
    onscreen_rows = sample.get("onscreen_windows")
    if (type(target_pid) is not int or target_pid <= 1
            or type(target_id) is not int or target_id <= 0
            or type(target_rank) is not int or target_rank < 0
            or not isinstance(onscreen_rows, list)):
        return {"status": "UNKNOWN", "cover_rows": []}
    target_rows = [row for row in onscreen_rows if isinstance(row, dict)
                   and row.get("owner_pid") == target_pid
                   and row.get("cg_window_id") == target_id
                   and row.get("onscreen") is True
                   and row.get("rank_onscreen_windows") == target_rank]
    if len(target_rows) != 1:
        return {"status": "UNKNOWN", "cover_rows": []}
    target_bounds = target_rows[0].get("bounds_cg")
    if (not isinstance(target_bounds, dict)
            or not all(type(target_bounds.get(key)) in {int, float}
                       for key in ("x", "y", "width", "height"))
            or target_bounds["width"] <= 0 or target_bounds["height"] <= 0):
        return {"status": "UNKNOWN", "cover_rows": []}
    rows = evidence.get("opaque_full_cover_rows")
    if not isinstance(rows, list):
        return {"status": "UNKNOWN", "cover_rows": []}
    parsed: list[dict[str, Any]] = []
    for row in rows:
        if (not isinstance(row, list) or len(row) != 9
                or type(row[0]) is not int or type(row[1]) is not int
                or not all(type(value) in {int, float} for value in row[2:6])
                or type(row[7]) not in {int, float} or type(row[8]) is not int):
            return {"status": "UNKNOWN", "cover_rows": []}
        x, y, width, height = row[2:6]
        if (row[0] <= 0 or row[1] <= 1 or row[8] < 0 or width <= 0 or height <= 0
                or row[7] < 0.999 or row[8] >= target_rank
                or x > target_bounds["x"] or y > target_bounds["y"]
                or x + width < target_bounds["x"] + target_bounds["width"]
                or y + height < target_bounds["y"] + target_bounds["height"]):
            return {"status": "UNKNOWN", "cover_rows": []}
        parsed.append({"cg_window_id": row[0], "owner_pid": row[1],
                       "bounds_cg": row[2:6], "layer": row[6],
                       "alpha": row[7], "rank_onscreen_windows": row[8]})
    if not parsed:
        if evidence.get("opaque_full_cover_state") != "CLEAR":
            return {"status": "UNKNOWN", "cover_rows": []}
        return {"status": "CLEAR", "cover_rows": []}
    if evidence.get("opaque_full_cover_state") != "COVERED":
        return {"status": "UNKNOWN", "cover_rows": []}
    if allowed_cover_identity is not None and len(parsed) == 1:
        row = parsed[0]
        if (row["owner_pid"], row["cg_window_id"]) == allowed_cover_identity:
            return {"status": "EXPECTED_COVER", "cover_rows": parsed}
    return {"status": "UNEXPECTED_COVER", "cover_rows": parsed}


def desktop_preflight_eligible(evidence: dict[str, Any], expected_uid: int) -> bool:
    frontmost_name = evidence.get("frontmost_application_name")
    frontmost_lower = frontmost_name.lower() if isinstance(frontmost_name, str) else ""
    return bool(
        evidence.get("status") == "PASS"
        and evidence.get("evidence_complete") is True
        and evidence.get("all_window_query_succeeded") is True
        and evidence.get("onscreen_window_query_succeeded") is True
        and evidence.get("session_login_complete") is True
        and evidence.get("session_on_console") is True
        and evidence.get("session_user_id") == expected_uid
        and evidence.get("expected_user_id") == expected_uid
        and isinstance(frontmost_name, str) and bool(frontmost_name.strip())
        and evidence.get("expected_target_inside_display") is True
        and evidence.get("lock_screen_name_suspected") is False
        and evidence.get("lock_screen_owner_suspected") is False
        and "loginwindow" not in frontmost_lower
        and "screensaver" not in frontmost_lower
        and "screen saver" not in frontmost_lower
        and evidence.get("opaque_full_cover_candidates") == []
        and evidence.get("unclassifiable_onscreen_window_count") == 0
        and evidence.get("candidate_overflow") is False)


def covered_sampling_summary(samples: list[dict[str, Any]], *,
                             start_monotonic_ns: int,
                             end_monotonic_ns: int,
                             required_cover_identity: tuple[int, int],
                             expected_target_pid: int,
                             expected_run_id: str,
                             expected_token: str,
                             minimum_samples: int = MIN_COVERED_SAMPLES,
                             maximum_gap_ns: int = MAX_SAMPLE_GAP_NS) -> dict[str, Any]:
    if (type(start_monotonic_ns) is not int or type(end_monotonic_ns) is not int
            or end_monotonic_ns <= start_monotonic_ns):
        return {"status": "FAIL", "sample_count": 0, "maximum_sample_start_gap_ns": None,
                "query_failures": 0}
    rows = sorted(
        (row for row in samples if isinstance(row, dict)
         and row.get("event") == "sample"
         and type(row.get("sample_started_monotonic_ns")) is int
         and start_monotonic_ns <= row["sample_started_monotonic_ns"] < end_monotonic_ns),
        key=lambda row: row["sample_started_monotonic_ns"],
    )
    starts = [row["sample_started_monotonic_ns"] for row in rows]
    gaps = ([starts[0] - start_monotonic_ns] if starts else [])
    gaps.extend(right - left for left, right in zip(starts, starts[1:]))
    if starts:
        gaps.append(end_monotonic_ns - starts[-1])
    query_failures = sum(
        row.get("all_window_query_succeeded") is not True
        or row.get("onscreen_window_query_succeeded") is not True
        or row.get("target_unix_pid") != expected_target_pid
        or row.get("run_id") != expected_run_id
        or row.get("experiment_token") != expected_token
        or (row.get("same_query_cover_evidence", {}).get("target_join_status") != "PASS"
            if isinstance(row.get("same_query_cover_evidence"), dict) else True)
        or (row.get("same_query_cover_evidence", {}).get("cover_evidence_complete") is not True
            if isinstance(row.get("same_query_cover_evidence"), dict) else True)
        or same_query_cover_state(
            row, allowed_cover_identity=required_cover_identity).get("status") != "EXPECTED_COVER"
        for row in rows
    )
    maximum_gap = max(gaps) if gaps else None
    passed = (len(rows) >= minimum_samples and query_failures == 0
              and maximum_gap is not None and maximum_gap <= maximum_gap_ns)
    return {
        "status": "PASS" if passed else "FAIL",
        "sample_count": len(rows),
        "maximum_sample_start_gap_ns": maximum_gap,
        "query_failures": query_failures,
        "minimum_samples": minimum_samples,
        "maximum_allowed_gap_ns": maximum_gap_ns,
        "covered_start_monotonic_ns": start_monotonic_ns,
        "covered_end_monotonic_ns": end_monotonic_ns,
    }


def identity_stability_summary(rows: list[dict[str, Any]], *, now_mach_ns: int,
                               minimum_duration_ns: int = 2_000_000_000,
                               maximum_gap_ns: int = MAX_IDENTITY_SAMPLE_GAP_NS) -> dict[str, Any]:
    """Require one exact tracked identity across a fresh, contiguous time span."""
    if (type(now_mach_ns) is not int or now_mach_ns <= 0
            or type(minimum_duration_ns) is not int or minimum_duration_ns <= 0
            or type(maximum_gap_ns) is not int or maximum_gap_ns <= 0):
        return {"stable": False, "reason": "invalid interval input", "point_count": 0}
    identity_keys = ("layer_id", "window_view_id", "window_lookup_path", "window_id", "window_number")
    points = []
    for row in rows:
        if not isinstance(row, dict) or row.get("window_associated") is not True:
            continue
        stamp = row.get("ts_mach_ns")
        identity = tuple(row.get(key) for key in identity_keys)
        if (type(stamp) is not int or stamp <= 0 or stamp > now_mach_ns
                or any(type(value) is not int or value <= 0 for value in
                       (identity[0], identity[1], identity[3], identity[4]))
                or identity[2] != "direct_delegate"):
            continue
        points.append((stamp, identity))
    points.sort(key=lambda item: item[0])
    if not points or now_mach_ns - points[-1][0] > maximum_gap_ns:
        return {"stable": False, "reason": "latest identity point is missing or stale",
                "point_count": len(points), "interval_ns": 0}
    latest_identity = points[-1][1]
    suffix = [points[-1]]
    for point in reversed(points[:-1]):
        gap = suffix[-1][0] - point[0]
        if (point[1] != latest_identity or gap <= 0 or gap > maximum_gap_ns):
            break
        suffix.append(point)
    suffix.reverse()
    duration = suffix[-1][0] - suffix[0][0]
    stable = duration >= minimum_duration_ns
    return {
        "stable": stable,
        "reason": None if stable else "same identity has not been observed across the required interval",
        "point_count": len(suffix),
        "interval_ns": duration,
        "maximum_sample_gap_ns": max((right[0] - left[0]
                                       for left, right in zip(suffix, suffix[1:])), default=0),
        "identity": list(latest_identity),
        "first_mach_ns": suffix[0][0],
        "last_mach_ns": suffix[-1][0],
        "maximum_allowed_gap_ns": maximum_gap_ns,
        "minimum_duration_ns": minimum_duration_ns,
    }
def transition_bracket_summary(samples: list[dict[str, Any]], *,
                               from_state: str,
                               to_state: str,
                               action_end_mach_ns: int,
                               expected_target_pid: int,
                               expected_run_id: str,
                               expected_token: str,
                               allowed_cover_identity: tuple[int, int] | None = None,
                               maximum_gap_ns: int = MAX_SAMPLE_GAP_NS) -> dict[str, Any]:
    """Require adjacent successful 50 ms same-query samples on each transition."""
    if (type(action_end_mach_ns) is not int or action_end_mach_ns <= 0
            or from_state not in {"CLEAR", "EXPECTED_COVER"}
            or to_state not in {"CLEAR", "EXPECTED_COVER"}
            or from_state == to_state):
        return {"status": "FAIL", "reason": "invalid transition inputs"}
    rows = sorted(
        (row for row in samples if isinstance(row, dict)
         and row.get("event") == "sample"
         and type(row.get("sample_started_mach_absolute_ns")) is int),
        key=lambda row: row["sample_started_mach_absolute_ns"],
    )
    classified = [
        (row, same_query_cover_state(
            row, allowed_cover_identity=allowed_cover_identity).get("status"))
        for row in rows
    ]
    destination_index = next((index for index, (row, state) in enumerate(classified)
                              if state == to_state
                              and type(row.get("onscreen_query_started_mach_absolute_ns")) is int
                              and row["onscreen_query_started_mach_absolute_ns"] >= action_end_mach_ns),
                             None)
    if destination_index is None or destination_index == 0:
        return {"status": "FAIL", "reason": "no bounded destination proof after action"}
    source_row, source_state = classified[destination_index - 1]
    destination_row, _ = classified[destination_index]
    source_start = source_row.get("sample_started_monotonic_ns")
    destination_start = destination_row.get("sample_started_monotonic_ns")
    gap = (destination_start - source_start
           if type(source_start) is int and type(destination_start) is int else None)
    query_rows_ok = all(
        row.get("all_window_query_succeeded") is True
        and row.get("onscreen_window_query_succeeded") is True
        and row.get("target_unix_pid") == expected_target_pid
        and row.get("run_id") == expected_run_id
        and row.get("experiment_token") == expected_token
        and isinstance(row.get("same_query_cover_evidence"), dict)
        and row["same_query_cover_evidence"].get("target_join_status") == "PASS"
        and row["same_query_cover_evidence"].get("cover_evidence_complete") is True
        for row, _state in classified[destination_index - 1:destination_index + 1]
    )
    passed = (source_state == from_state and query_rows_ok and gap is not None
              and 0 < gap <= maximum_gap_ns)
    return {
        "status": "PASS" if passed else "FAIL",
        "reason": None if passed else "source/query identity/gap proof failed",
        "from_state": from_state,
        "to_state": to_state,
        "action_end_mach_ns": action_end_mach_ns,
        "source_sample_index": source_row.get("sample_index"),
        "source_query_started_mach_ns": source_row.get("onscreen_query_started_mach_absolute_ns"),
        "source_query_completed_mach_ns": source_row.get("onscreen_query_completed_mach_absolute_ns"),
        "destination_sample_index": destination_row.get("sample_index"),
        "destination_query_started_mach_ns": destination_row.get("onscreen_query_started_mach_absolute_ns"),
        "destination_query_completed_mach_ns": destination_row.get("onscreen_query_completed_mach_absolute_ns"),
        "sample_start_gap_ns": gap,
        "maximum_allowed_gap_ns": maximum_gap_ns,
        "source_state": source_state,
        "queries_successful_and_target_joined": query_rows_ok,
    }
