#!/usr/bin/env python3
"""Offline BR8 metrics around separately logged external UI action brackets.

This supplements br8_analysis.py without changing its target-notification
anchors or any live runner/controller path. Action-call windows are descriptive
time slices; they do not assert an exact target state transition instant.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

NS = 1_000_000_000
EVIDENCE = Path(__file__).resolve().parent / "evidence" / "br8"


def _quantile(values: list[int | float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lo = math.floor(position)
    hi = math.ceil(position)
    return float(ordered[lo] if lo == hi else
                 ordered[lo] + (ordered[hi] - ordered[lo]) * (position - lo))


def _distribution(values: list[int | float]) -> dict[str, Any]:
    if not values:
        return {"count": 0, "p50_ns": None, "p99_ns": None, "max_ns": None}
    return {
        "count": len(values),
        "p50_ns": _quantile(values, 0.50),
        "p99_ns": _quantile(values, 0.99),
        "max_ns": max(values),
    }


def summarize_interval(frames: list[dict[str, Any]], start_ns: int, end_ns: int,
                       run_start_ns: int, run_end_ns: int) -> dict[str, Any]:
    """Summarize validated frame rows in acquisition-time and request-time slices."""
    if end_ns <= start_ns:
        raise ValueError("interval end must follow start")
    observed_start = max(start_ns, run_start_ns)
    observed_end = min(end_ns, run_end_ns)
    coverage_ns = max(0, observed_end - observed_start)
    complete = coverage_ns == end_ns - start_ns

    acquired = [f for f in frames
                if observed_start <= int(f["native_acquire_begin_mach_ns"]) < observed_end]
    outcomes = [f for f in acquired if f.get("presented_callback_mach_ns") is not None]
    zeros = [f for f in outcomes if f.get("presented_time_mach_ns") is not None
             and int(f["presented_time_mach_ns"]) == 0]
    displayed = [f for f in outcomes
                 if f.get("is_displayed") is True
                 and int(f.get("presented_time_mach_ns") or 0) > 0]
    seconds = coverage_ns / NS if coverage_ns else 0.0

    drawable = [int(f["next_drawable_duration_ns"]) for f in acquired
                if f.get("next_drawable_duration_ns") is not None]
    submit = [int(f["native_submit_duration_ns"]) for f in acquired
              if f.get("native_submit_duration_ns") is not None]

    requested = [f for f in frames
                 if observed_start <= int(f["present_request_mach_ns"]) < observed_end]
    callback_latency = [int(f["presented_callback_mach_ns"])
                        - int(f["present_request_mach_ns"])
                        for f in requested
                        if f.get("presented_callback_mach_ns") is not None]
    fractions = [float(f["request_phase_fraction"]) for f in requested
                 if f.get("request_phase_fraction") is not None]
    phase_quartiles = [0, 0, 0, 0]
    for value in fractions:
        phase_quartiles[min(3, max(0, int(value * 4)))] += 1

    return {
        "requested_interval_mach_ns": {"start": start_ns, "end": end_ns},
        "requested_interval_elapsed_s": {
            "start": (start_ns - run_start_ns) / NS,
            "end": (end_ns - run_start_ns) / NS,
            "duration": (end_ns - start_ns) / NS,
        },
        "observed_interval_mach_ns": {"start": observed_start, "end": observed_end},
        "observed_coverage_ns": coverage_ns,
        "complete": complete,
        "acquisition_time_cohort": {
            "count": len(acquired),
            "acquired_fps": len(acquired) / seconds if seconds else None,
            "exact_outcome_count": len(outcomes),
            "exact_outcome_coverage": len(outcomes) / len(acquired) if acquired else None,
            "displayed_count": len(displayed),
            "displayed_cohort_fps": len(displayed) / seconds if seconds else None,
            "raw_zero_callback_count": len(zeros),
            "raw_zero_share_of_acquisitions": len(zeros) / len(acquired) if acquired else None,
            "next_drawable_duration_ns": _distribution(drawable),
            "next_drawable_ge_100ms_count": sum(d >= 100_000_000 for d in drawable),
            "next_drawable_ge_900ms_count": sum(d >= 900_000_000 for d in drawable),
            "host_submit_duration_ns": _distribution(submit),
        },
        "request_time_cohort": {
            "unique_request_count": len(requested),
            "request_to_callback_latency_ns": _distribution(callback_latency),
            "callback_latency_ge_100ms_count": sum(v >= 100_000_000 for v in callback_latency),
            "callback_latency_ge_900ms_count": sum(v >= 900_000_000 for v in callback_latency),
            "request_phase_fraction": {
                "count": len(fractions),
                "quartiles": phase_quartiles,
                "p50": _quantile(fractions, 0.50),
                "p90": _quantile(fractions, 0.90),
                "p99": _quantile(fractions, 0.99),
                "max": max(fractions) if fractions else None,
            },
        },
    }


def summarize_inter_action_steady_hold(
        frames: list[dict[str, Any]], entry_wrapper_end_ns: int,
        restoration_wrapper_start_ns: int, run_start_ns: int,
        run_end_ns: int) -> dict[str, Any]:
    """Describe the post-entry/post-settle interval up to the restore action."""
    start_ns = entry_wrapper_end_ns + 10 * NS
    end_ns = restoration_wrapper_start_ns
    if end_ns <= start_ns:
        raise ValueError("restoration wrapper must follow the steady-hold start")
    return {
        "label": "inter_action_steady_hold",
        "interval_rule": "[entry_wrapper_end + 10s, restoration_wrapper_start)",
        "boundary_semantics": (
            "This is a descriptive frame cohort between external controller action brackets. "
            "It has no target notification anchor and does not prove continuous geometry or "
            "window-stack state during the interval."
        ),
        "metrics": summarize_interval(frames, start_ns, end_ns,
                                      run_start_ns, run_end_ns),
    }


def _read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inner_helper_record(action: dict[str, Any]) -> dict[str, Any] | None:
    try:
        value = json.loads(action.get("stdout", ""))
    except (json.JSONDecodeError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _action_bracket(action: dict[str, Any], origin_ns: int) -> dict[str, Any]:
    start = int(action["start_mach_ns"])
    end = int(action["end_mach_ns"])
    if end < start:
        raise ValueError(f"controller action clock regressed: {action.get('label')}")
    inner = _inner_helper_record(action)
    helper_bracket = None
    if inner and inner.get("start_mach_ns") is not None and inner.get("end_mach_ns") is not None:
        helper_bracket = {
            "start_mach_ns": int(inner["start_mach_ns"]),
            "end_mach_ns": int(inner["end_mach_ns"]),
            "start_elapsed_s": (int(inner["start_mach_ns"]) - origin_ns) / NS,
            "end_elapsed_s": (int(inner["end_mach_ns"]) - origin_ns) / NS,
            "duration_ns": int(inner["end_mach_ns"]) - int(inner["start_mach_ns"]),
            "helper_mode": inner.get("mode"),
            "requested_position": inner.get("requested_position"),
            "action_error": inner.get("action_error"),
        }
    return {
        "label": action.get("label"),
        "requested_loop_second": action.get("requested_loop_second"),
        "controller_wrapper_start_mach_ns": start,
        "controller_wrapper_end_mach_ns": end,
        "controller_wrapper_start_elapsed_s": (start - origin_ns) / NS,
        "controller_wrapper_end_elapsed_s": (end - origin_ns) / NS,
        "controller_wrapper_duration_ns": end - start,
        "returncode": action.get("returncode"),
        "helper_call_envelope": helper_bracket,
        "boundary_semantics": (
            "The wrapper brackets launching and collecting the normal UI helper. "
            "It is an external action interval, not a target notification or exact "
            "window-stack transition timestamp; OS/compositor effect may lag return."
        ),
    }


def _snapshot_summary(path: Path, origin_ns: int, target_pid: int) -> dict[str, Any] | None:
    if not path.exists():
        return None
    doc = _read_json(path)
    target = next((w for w in doc.get("windows", [])
                   if int(w.get("kCGWindowOwnerPID", -1)) == target_pid), None)
    finder = next((w for w in doc.get("windows", [])
                   if w.get("kCGWindowOwnerName") == "Finder"), None)
    if target is None or finder is None:
        return {
            "source": path.name,
            "complete_target_and_finder_pair": False,
            "start_elapsed_s": (int(doc["start_mach_ns"]) - origin_ns) / NS,
            "end_elapsed_s": (int(doc["end_mach_ns"]) - origin_ns) / NS,
        }
    tb = target["kCGWindowBounds"]
    fb = finder["kCGWindowBounds"]
    left = max(float(tb["X"]), float(fb["X"]))
    right = min(float(tb["X"]) + float(tb["Width"]),
                float(fb["X"]) + float(fb["Width"]))
    top = max(float(tb["Y"]), float(fb["Y"]))
    bottom = min(float(tb["Y"]) + float(tb["Height"]),
                 float(fb["Y"]) + float(fb["Height"]))
    overlap_w = max(0.0, right - left)
    overlap_h = max(0.0, bottom - top)
    target_area = float(tb["Width"]) * float(tb["Height"])
    return {
        "source": path.name,
        "start_mach_ns": doc["start_mach_ns"],
        "end_mach_ns": doc["end_mach_ns"],
        "start_elapsed_s": (int(doc["start_mach_ns"]) - origin_ns) / NS,
        "end_elapsed_s": (int(doc["end_mach_ns"]) - origin_ns) / NS,
        "finder": {"front_to_back_rank": finder.get("front_to_back_rank"),
                   "bounds": fb, "alpha": finder.get("kCGWindowAlpha"),
                   "on_screen": finder.get("kCGWindowIsOnscreen")},
        "target": {"front_to_back_rank": target.get("front_to_back_rank"),
                   "bounds": tb, "alpha": target.get("kCGWindowAlpha"),
                   "on_screen": target.get("kCGWindowIsOnscreen")},
        "nominal_rectangular_overlap": {
            "width_points": overlap_w,
            "height_points": overlap_h,
            "target_area_fraction": overlap_w * overlap_h / target_area if target_area else None,
            "opaque_per_pixel_coverage_proven": False,
        },
        "observation_limit": (
            "CoreGraphics geometry and order are a sampled external observation; "
            "they do not prove opaque per-pixel coverage between samples."
        ),
    }


def analyze(analysis_path: Path, controller_path: Path, prefix: str) -> dict[str, Any]:
    analysis = _read_json(analysis_path)
    if len(analysis.get("runs", [])) != 1:
        raise ValueError("expected one analyzed run")
    analyzed_run = analysis["runs"][0]
    run_id = analyzed_run["run_id"]
    run_dir = Path(analyzed_run["run_directory"])
    result_path = run_dir / "run-result.json"
    result = _read_json(result_path)
    if result.get("status") != "PASS" or result.get("is_valid") is not True \
            or result.get("validation", {}).get("is_valid") is not True:
        raise ValueError("the supplemental analysis requires a valid PASS run")
    frames = result["validation"].get("frames", [])
    if not frames or len(frames) != result["validation"].get("frame_count"):
        raise ValueError("validated frame rows are missing or incomplete")
    target_pid = int(result["trace_index"]["target_unix_pid"])
    controller = _read_json(controller_path)
    if not controller or controller[0].get("run_id") != run_id \
            or int(controller[0].get("target_pid", -1)) != target_pid:
        raise ValueError("controller log does not belong to the analyzed target run")
    origin_ns = int(controller[0]["origin_mach_ns"])
    run_start_ns = int(analyzed_run["host_interval"]["start_ns"])
    run_end_ns = int(analyzed_run["host_interval"]["end_ns"])
    if origin_ns != run_start_ns:
        raise ValueError("controller origin and validated target host interval do not match")

    clock = time.get_clock_info("monotonic")
    if clock.implementation != "mach_absolute_time()":
        raise ValueError("run this analysis with the same Mach-based CPython clock family")

    host_path = Path(result["trace_index"]["target_trace_path"])
    metadata: list[dict[str, Any]] = []
    target_notifications: list[dict[str, Any]] = []
    with host_path.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row.get("event") == "clock_metadata":
                metadata.append(row)
            if row.get("event") == "window_telemetry" and row.get("notification_name"):
                target_notifications.append(row)
    if not metadata:
        raise ValueError("target host trace does not expose clock metadata")
    trace_clock = metadata[0]
    same_mach_source = (trace_clock.get("event_clock_source") == "mach_absolute_time"
                        and trace_clock.get("event_clock_domain") == "mach_absolute_time_ns")
    if not same_mach_source:
        raise ValueError("target host trace clock is not verified Mach absolute time")

    actions = {row.get("label"): row for row in controller if row.get("start_mach_ns") is not None
               and row.get("end_mach_ns") is not None}
    entry = actions.get("coverage entry")
    restore = actions.get("coverage restoration")
    if entry is None or restore is None:
        raise ValueError("controller log lacks coverage entry or restoration action bracket")
    entry_bracket = _action_bracket(entry, origin_ns)
    restore_bracket = _action_bracket(restore, origin_ns)
    steady_hold = summarize_inter_action_steady_hold(
        frames,
        int(entry_bracket["controller_wrapper_end_mach_ns"]),
        int(restore_bracket["controller_wrapper_start_mach_ns"]),
        run_start_ns,
        run_end_ns,
    )
    intervals = []
    for bracket in (entry_bracket, restore_bracket):
        start = int(bracket["controller_wrapper_start_mach_ns"])
        end = int(bracket["controller_wrapper_end_mach_ns"])
        intervals.append({
            "action": bracket,
            "pre_action_10s": summarize_interval(frames, start - 10 * NS, start,
                                                  run_start_ns, run_end_ns),
            "action_wrapper_bracket": summarize_interval(frames, start, end,
                                                          run_start_ns, run_end_ns),
            "post_action_10s": summarize_interval(frames, end, end + 10 * NS,
                                                   run_start_ns, run_end_ns),
        })

    snapshots = {
        label: _snapshot_summary(EVIDENCE / f"{prefix}-{file_tag}-coverage.json",
                                 origin_ns, target_pid)
        for label, file_tag in (("pre_entry", "pre"), ("held", "hold"),
                                ("restored", "restored"))
    }
    before = snapshots["pre_entry"]
    held = snapshots["held"]
    restored = snapshots["restored"]
    transition_evidence = {}
    if before and held:
        transition_evidence["entry_sample_bracket"] = {
            "last_baseline_observation_interval": [before["start_mach_ns"], before["end_mach_ns"]],
            "external_action_wrapper_interval": [entry_bracket["controller_wrapper_start_mach_ns"],
                                                  entry_bracket["controller_wrapper_end_mach_ns"]],
            "first_partial_geometry_observation_interval": [held["start_mach_ns"], held["end_mach_ns"]],
            "exact_state_change_timestamp_available": False,
        }
    if held and restored:
        transition_evidence["restoration_sample_bracket"] = {
            "last_partial_geometry_observation_interval": [held["start_mach_ns"], held["end_mach_ns"]],
            "external_action_wrapper_interval": [restore_bracket["controller_wrapper_start_mach_ns"],
                                                  restore_bracket["controller_wrapper_end_mach_ns"]],
            "first_restored_geometry_observation_interval": [restored["start_mach_ns"],
                                                              restored["end_mach_ns"]],
            "exact_state_change_timestamp_available": False,
        }

    first_positive_time = min((int(f["presented_time_mach_ns"]) for f in frames
                               if int(f.get("presented_time_mach_ns") or 0) > 0), default=None)
    analysis_note = (
        "These intervals are anchored to ordinary external controller call brackets and sampled "
        "CoreGraphics geometry because the target emitted no window-state notifications. "
        "Window state changes are not assigned notification timestamps and the interval metrics "
        "must not be read as exact-state or causal estimates."
    )
    return {
        "schema_version": 1,
        "analysis_kind": "BR8_EXTERNALLY_BRACKETED_DESCRIPTIVE_PHASES",
        "analysis_note": analysis_note,
        "run_id": run_id,
        "target_pid": target_pid,
        "run_status": result["status"],
        "valid_target_frame_count": len(frames),
        "source_files": {
            "generator_code": {"path": str(Path(__file__).resolve()),
                                "sha256": _sha256(Path(__file__).resolve())},
            "state_analysis": {"path": str(analysis_path), "sha256": _sha256(analysis_path)},
            "controller_log": {"path": str(controller_path), "sha256": _sha256(controller_path)},
            "run_result": {"path": str(result_path), "sha256": _sha256(result_path)},
            "target_host_trace": {"path": str(host_path), "sha256": _sha256(host_path)},
        },
        "clock_lineage": {
            "controller_source_expression": "time.monotonic_ns() in window_sequence.py",
            "analysis_interpreter": {
                "executable": sys.executable,
                "version": sys.version.split()[0],
                "monotonic_implementation": clock.implementation,
                "monotonic": clock.monotonic,
                "adjustable": clock.adjustable,
                "resolution_ns": clock.resolution * NS,
            },
            "target_host_trace_source": trace_clock.get("event_clock_source"),
            "target_host_trace_domain": trace_clock.get("event_clock_domain"),
            "target_host_trace_timebase_numerator": trace_clock.get("mach_timebase_numer"),
            "target_host_trace_timebase_denominator": trace_clock.get("mach_timebase_denom"),
            "controller_origin_mach_ns": origin_ns,
            "validated_host_interval_start_mach_ns": run_start_ns,
            "controller_origin_matches_validated_host_start": origin_ns == run_start_ns,
            "shared_mach_absolute_time_lineage_supported": True,
            "same_source_limit": (
                "The analysis interpreter reports the same Mach absolute clock family as the "
                "target trace. The archived controller JSON does not record sys.executable; "
                "the installed Homebrew CPython 3.14.8 runtime was checked read-only."
            ),
        },
        "run_interval": {
            "start_mach_ns": run_start_ns,
            "end_mach_ns": run_end_ns,
            "duration_ns": run_end_ns - run_start_ns,
            "effective_refresh_hz": result["validation"].get("effective_refresh_hz"),
            "refresh_phase_period_ns": result["validation"].get("refresh_phase", {}).get("period_ns"),
            "first_positive_presented_time_mach_ns": first_positive_time,
        },
        "target_notification_count": len(target_notifications),
        "target_notifications": [
            {"notification_name": row.get("notification_name"),
             "ts_mach_ns": row.get("ts_mach_ns"),
             "elapsed_s": (int(row["ts_mach_ns"]) - origin_ns) / NS}
            for row in target_notifications
        ],
        "external_action_windows": intervals,
        "inter_action_steady_hold": steady_hold,
        "geometry_samples": snapshots,
        "transition_sample_brackets": transition_evidence,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", default="partial-frontmost",
                        help="evidence file prefix (default: partial-frontmost)")
    parser.add_argument("--output", type=Path,
                        default=EVIDENCE / "partial-frontmost-external-bracket-analysis.json")
    args = parser.parse_args()
    evidence = EVIDENCE
    report = analyze(evidence / f"{args.prefix}-analysis.json",
                     evidence / f"{args.prefix}-controller.json", args.prefix)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
