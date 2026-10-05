#!/usr/bin/env python3
"""Compare one disabled DXVK source run with one internal-WSI run."""

import argparse
import csv
import json
import re
import statistics
from collections import Counter
from pathlib import Path


PRESENT_RE = re.compile(
    r"WSI: present origin=(APPLICATION_SOURCE|INTERNAL_OUTPUT) "
    r"wsi=(\d+) app=(\d+) internal=(\d+) acquire=(\d+) "
    r"image=(\d+) generation=(\d+)"
)
SOURCE_DURATION_RE = re.compile(
    r"InternalWSI: source-present app=\d+ duration-us=(\d+) hr=0x([0-9a-fA-F]+)"
)
APP_LATENCY_SIGNAL_RE = re.compile(
    r"WSI: app-frame-latency-signal app=(\d+) via=(source-submit-completion|source-present-retirement)"
)
INTERNAL_RETIREMENT_RE = re.compile(
  r"InternalWSI: retirement outcome=(presented|failed|dropped) "
  r"wsi=(\d+) internal=(\d+) status=([A-Za-z0-9_-]+)"
)
INTERNAL_DISPATCH_RE = re.compile(
    r"InternalWSI: queued origin=INTERNAL_OUTPUT wsi=(\d+) internal=(\d+) "
    r"app=none pattern=(?:magenta|cyan) vkPresent=([A-Za-z0-9_]+)"
)
WSI_SUCCESS_RESULTS = {"VK_SUCCESS", "VK_SUBOPTIMAL_KHR"}


def read_csv(path):
    with path.open(newline="") as stream:
        return list(csv.DictReader(stream))


def rank_percentile(values, percentile):
    if not values:
        return None
    values = sorted(values)
    index = max(0, min(len(values) - 1, int((len(values) - 1) * percentile + 0.5)))
    return values[index]


def load_run(path):
    path = Path(path)
    meta = json.loads((path / "result.json").read_text())
    sem_rows = read_csv(path / "semantics.csv")
    app_rows = read_csv(path / "d3d11_clear_window_app.csv")
    log_files = list(path.glob("*_d3d11.log"))
    if len(log_files) != 1:
        raise ValueError(f"expected one DXVK D3D11 log in {path}, found {len(log_files)}")
    log = log_files[0].read_text(errors="replace")

    observations = [row for row in sem_rows if row["event"] == "observe"]
    setup_rows = [row for row in sem_rows if row["event"] == "setup"]
    if len(setup_rows) != 1:
        raise ValueError(f"expected one waitable setup row in {path}")
    setup = setup_rows[0]
    presents = [row for row in app_rows if row["event"] == "present"]
    successful_presents = [row for row in presents if int(row["present_hr"], 16) == 0]
    last_present = [int(row["count"]) for row in observations if int(row["count_hr"], 16) == 0]
    stats = [(int(row["stats_hr"], 16), int(row["stats_present"])) for row in observations]
    stats_lags = [
        int(row["count"]) - int(row["stats_present"])
        for row in observations
        if int(row["count_hr"], 16) == 0 and int(row["stats_hr"], 16) == 0
    ]
    stats_timing = [
        (int(row["stats_refresh"]), int(row["sync_refresh"]), int(row["sync_qpc"]))
        for row in observations if int(row["stats_hr"], 16) == 0
    ]
    backbuffer_indices = [int(row["index"]) for row in observations]
    buffer_identities = [row["buffer_identity"].lower() for row in observations]
    removed = [int(row["device_removed"], 16) for row in observations]
    waits = [int(row["wait"]) for row in observations]

    presents_wsi = [
        {
            "origin": origin,
            "wsi": int(wsi),
            "app": int(app),
            "internal": int(internal),
            "acquire": int(acquire),
            "image": int(image),
            "generation": int(generation),
        }
        for origin, wsi, app, internal, acquire, image, generation in PRESENT_RE.findall(log)
    ]
    source = [entry for entry in presents_wsi if entry["origin"] == "APPLICATION_SOURCE"]
    internal = [entry for entry in presents_wsi if entry["origin"] == "INTERNAL_OUTPUT"]
    source_durations = [
        int(duration)
        for duration, result in SOURCE_DURATION_RE.findall(log)
        if int(result, 16) == 0
    ]
    app_latency_signals = [
        {"app": int(app), "via": via}
        for app, via in APP_LATENCY_SIGNAL_RE.findall(log)
    ]
    internal_retirements = [
        {"outcome": outcome, "wsi": int(wsi), "internal": int(internal), "status": status}
        for outcome, wsi, internal, status in INTERNAL_RETIREMENT_RE.findall(log)
    ]
    internal_dispatches = [
        {"wsi": int(wsi), "internal": int(internal), "result": result}
        for wsi, internal, result in INTERNAL_DISPATCH_RE.findall(log)
    ]

    return {
        "path": str(path),
        "meta": meta,
        "observations": observations,
        "present_rows": presents,
        "successful_presents": successful_presents,
        "last_present": last_present,
        "stats": stats,
        "stats_lags": stats_lags,
        "stats_timing": stats_timing,
        "backbuffer_indices": backbuffer_indices,
        "buffer_identities": buffer_identities,
        "device_removed": removed,
        "waits": waits,
        "setup": {
            "query_interface_hr": int(setup["count_hr"], 16),
            "set_maximum_latency_hr": int(setup["stats_hr"], 16),
            "get_maximum_latency_hr": int(setup["stats_present"], 16),
            "maximum_latency": int(setup["stats_refresh"]),
        },
        "wsi": presents_wsi,
        "source_wsi": source,
        "internal_wsi": internal,
        "source_durations_us": source_durations,
        "app_latency_signals": app_latency_signals,
        "internal_retirements": internal_retirements,
        "internal_dispatches": internal_dispatches,
        "log": log,
    }


def api_invariants(run):
    n = len(run["successful_presents"])
    assert len(run["present_rows"]) == n, "DXGI Present returned a failing HRESULT"
    assert run["last_present"] == list(range(1, n + 1)), "GetLastPresentCount is not app-Present-only 1..N"
    successful_stats = [(hr, count) for hr, count in run["stats"] if hr == 0]
    successful_stats_counts = [count for _, count in successful_stats]
    assert all(a <= b for a, b in zip(successful_stats_counts, successful_stats_counts[1:])), "GetFrameStatistics PresentCount regressed"
    assert all(0 <= lag <= 2 for lag in run["stats_lags"]), "GetFrameStatistics moved outside frozen baseline's 0-2 frame lag envelope"
    assert all(count <= last for count, last in zip(successful_stats_counts, run["last_present"][1:])), "GetFrameStatistics counted beyond app LastPresentCount"
    assert len(set(run["buffer_identities"])) <= 1, "GetBuffer(0) identity changed within this run"
    assert all(value == 0 for value in run["device_removed"]), "device-removed status was not S_OK"
    assert run["setup"]["query_interface_hr"] == 0, "IDXGISwapChain2 query failed"
    assert run["setup"]["set_maximum_latency_hr"] == 0, "SetMaximumFrameLatency failed"
    assert run["setup"]["get_maximum_latency_hr"] == 0, "GetMaximumFrameLatency failed"
    assert run["setup"]["maximum_latency"] == 1, "maximum frame latency did not remain 1"
    return n


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("baseline", type=Path, help="internal-disabled result directory")
    parser.add_argument("experiment", type=Path, help="internal-enabled result directory")
    parser.add_argument("--output", type=Path, help="write JSON summary here")
    args = parser.parse_args()

    baseline = load_run(args.baseline)
    experiment = load_run(args.experiment)
    n_base = api_invariants(baseline)
    n_exp = api_invariants(experiment)

    # The harness runs to a monotonic wall-clock deadline. Two independent runs
    # can therefore straddle the final 15 Hz tick differently; allow only that
    # single boundary frame, then compare every shared application Present.
    assert abs(n_base - n_exp) <= 1, "application Present totals differ by more than the final timed-run boundary frame"

    assert baseline["meta"]["exit_status"] == 0 and experiment["meta"]["exit_status"] == 0
    assert baseline["meta"].get("source_cadence_variant") == experiment["meta"].get("source_cadence_variant"), "source cadence harness differs"
    assert baseline["meta"]["sha256"]["dxgi.dll"] == experiment["meta"]["sha256"]["dxgi.dll"], "DXGI binary differs"
    assert baseline["meta"]["sha256"]["d3d11.dll"] == experiment["meta"]["sha256"]["d3d11.dll"], "DXVK candidate binary differs"
    baseline_exes = {key: value for key, value in baseline["meta"]["sha256"].items() if key.endswith(".exe")}
    experiment_exes = {key: value for key, value in experiment["meta"]["sha256"].items() if key.endswith(".exe")}
    assert baseline_exes == experiment_exes, "application binary differs"
    common = min(n_base, n_exp)
    assert [row["present_hr"] for row in baseline["present_rows"][:common]] == [row["present_hr"] for row in experiment["present_rows"][:common]], "Present HRESULT sequence differs"
    assert baseline["last_present"][:common] == experiment["last_present"][:common], "GetLastPresentCount sequence changed"
    assert [status for status, _ in baseline["stats"][:common]] == [status for status, _ in experiment["stats"][:common]], "GetFrameStatistics HRESULT sequence changed"
    assert baseline["backbuffer_indices"][:common] == experiment["backbuffer_indices"][:common], "backbuffer index sequence changed"
    assert baseline["setup"] == experiment["setup"], "frame-latency waitable configuration changed"
    assert len(set(baseline["buffer_identities"])) <= 1 and len(set(experiment["buffer_identities"])) <= 1, "buffer identity was unstable within a run"
    assert baseline["device_removed"][:common] == experiment["device_removed"][:common], "device-removed state sequence changed"

    source = experiment["source_wsi"]
    internal = experiment["internal_wsi"]
    all_wsi = experiment["wsi"]
    assert len(source) == n_exp, "application source WSI count differs from successful app Presents"
    assert len(all_wsi) == len(source) + len(internal), "WSI count does not equal source plus internal"
    wsi_ids = [entry["wsi"] for entry in all_wsi]
    assert wsi_ids == list(range(1, len(wsi_ids) + 1)), "WSI IDs are not unique contiguous dispatch-order IDs"
    app_ids = [entry["app"] for entry in source]
    assert all(entry["app"] == 0 and entry["internal"] != 0 for entry in internal), "internal WSI output has an app frame identity"
    assert app_ids and app_ids == list(range(app_ids[0], app_ids[0] + len(app_ids))), "source AppFrameIds are not consecutive"
    internal_ids = [entry["internal"] for entry in internal]
    assert all(value > 0 for value in internal_ids) and all(
        a < b for a, b in zip(internal_ids, internal_ids[1:])
    ), "InternalPresentIds are not unique and increasing"
    assert all(entry["acquire"] > 0 and entry["generation"] > 0 for entry in all_wsi), "WSI presentation lacks acquire/generation identity"
    acquire_ids = [entry["acquire"] for entry in all_wsi]
    assert all(value > 0 for value in acquire_ids)
    assert len(acquire_ids) == len(set(acquire_ids)) and all(
        a < b for a, b in zip(acquire_ids, acquire_ids[1:])
    ), "WSI presents reused or reordered an acquire serial"
    terminals = experiment["internal_retirements"]
    terminal_internal_ids = [entry["internal"] for entry in terminals]
    assert terminal_internal_ids == list(range(1, len(terminal_internal_ids) + 1)), "internal attempt terminal records are missing, duplicated, or out of order"
    assert len(terminal_internal_ids) == len(set(terminal_internal_ids)), "an internal attempt retired more than once"
    pre_wsi_terminals = [entry for entry in terminals if entry["wsi"] == 0]
    assert acquire_ids[0] == 1 and (
        max(acquire_ids) - min(acquire_ids) + 1 - len(acquire_ids)
        == len(pre_wsi_terminals)
    ), "acquire serial gaps do not match internally retired acquired images with no WSI present"
    dispatch_terminal = [entry for entry in terminals if entry["wsi"] != 0]
    assert [entry["wsi"] for entry in dispatch_terminal] == [entry["wsi"] for entry in internal], "internal WSI terminal retirement count/order differs from dispatched presents"
    assert [entry["internal"] for entry in dispatch_terminal] == [entry["internal"] for entry in internal], "internal retirement identity mismatch"
    dispatches = [entry for entry in experiment["internal_dispatches"] if entry["wsi"] != 0]
    assert [entry["wsi"] for entry in dispatches] == [entry["wsi"] for entry in internal], "internal dispatch result records do not match WSI dispatches"
    assert [entry["internal"] for entry in dispatches] == [entry["internal"] for entry in internal], "internal dispatch result IDs do not match WSI identities"
    assert any(entry["result"] in WSI_SUCCESS_RESULTS for entry in dispatches), "no internal WSI present succeeded"
    assert "WSI: duplicate/out-of-order image retirement" not in experiment["log"], "WSI image retirement order/uniqueness assertion fired"
    assert "WSI: stale image retired" not in experiment["log"], "a WSI image retired from a stale swapchain generation"

    for run in (baseline, experiment):
        signal_ids = [entry["app"] for entry in run["app_latency_signals"]]
        source_ids = [entry["app"] for entry in run["source_wsi"]]
        assert len(source_ids) == len(run["successful_presents"]), "source WSI count differs from successful application Presents"
        assert source_ids and source_ids == list(range(source_ids[0], source_ids[0] + len(source_ids))), "source AppFrameIds are not consecutive"
        assert len(signal_ids) == len(set(signal_ids)), "an AppFrameId signaled frame latency more than once"
        assert signal_ids == source_ids, "app frame-latency signals do not match the application source frames exactly"

    baseline_source_ids = [entry["app"] for entry in baseline["source_wsi"]]
    experiment_source_ids = [entry["app"] for entry in experiment["source_wsi"]]
    assert baseline_source_ids[:common] == experiment_source_ids[:common], "source AppFrameId prefix differs between runs"

    source_durations = experiment["source_durations_us"]
    baseline_durations = baseline["source_durations_us"]
    def timing_summary(values):
        return {
            "samples": len(values),
            "p50_us": rank_percentile(values, .50),
            "p95_us": rank_percentile(values, .95),
            "p99_us": rank_percentile(values, .99),
            "max_us": max(values) if values else None,
        }

    def timing_offsets(run):
        values = run["stats_timing"]
        if not values:
            return None
        first = values[0]
        last = values[-1]
        return {
            "sample_count": len(values),
            "present_refresh_delta": last[0] - first[0],
            "sync_refresh_delta": last[1] - first[1],
            "sync_qpc_delta": last[2] - first[2],
        }

    waitable_poll_order_equal = baseline["waits"][:common] == experiment["waits"][:common]
    app_signal_prefix_equal = (
        [entry["app"] for entry in baseline["app_latency_signals"]][:common]
        == [entry["app"] for entry in experiment["app_latency_signals"]][:common]
    )
    report = {
        "verdict": (
            "PASS_API_ID_SEPARATION_WITH_INTERNAL_FAILURES"
            if internal and any(entry["result"] not in WSI_SUCCESS_RESULTS for entry in dispatches)
            else "PASS_API_ID_SEPARATION_WITH_ONE_FRAME_RUN_BOUNDARY_DELTA"
            if internal and n_base != n_exp
            else "PASS_API_ID_SEPARATION_WITH_ASYNC_WAITABLE_POLL_VARIATION"
            if internal and not waitable_poll_order_equal
            else "PASS_API_ID_SEPARATION" if internal else "FAIL_NO_INTERNAL_WSI_OUTPUT"
        ),
        "baseline": {
            "name": baseline["meta"]["name"],
            "application_present_count": n_base,
            "last_present_count_final": baseline["last_present"][-1],
            "frame_statistics_success_count": sum(status == 0 for status, _ in baseline["stats"]),
            "frame_statistics_timing_span": timing_offsets(baseline),
            "frame_statistics_lag_min_max": [
                min(baseline["stats_lags"]), max(baseline["stats_lags"]),
            ],
            "waitable_status_counts": dict(Counter(map(str, baseline["waits"]))),
            "app_frame_latency_signal_count": len(baseline["app_latency_signals"]),
            "source_present_cpu_us": timing_summary(baseline_durations),
        },
        "experiment": {
            "name": experiment["meta"]["name"],
            "application_present_count": n_exp,
            "last_present_count_final": experiment["last_present"][-1],
            "frame_statistics_success_count": sum(status == 0 for status, _ in experiment["stats"]),
            "frame_statistics_lag_min_max": [min(experiment["stats_lags"]), max(experiment["stats_lags"])],
            "frame_statistics_timing_span": timing_offsets(experiment),
            "waitable_status_counts": dict(Counter(map(str, experiment["waits"]))),
            "app_frame_latency_signal_count": len(experiment["app_latency_signals"]),
            "source_wsi": len(source),
            "internal_wsi": len(internal),
            "internal_wsi_present_status_counts": dict(Counter(
                entry["result"] for entry in dispatches
            )),
            "internal_wsi_success_count": sum(
                entry["result"] in WSI_SUCCESS_RESULTS for entry in dispatches
            ),
            "internal_wsi_failure_count": sum(
                entry["result"] not in WSI_SUCCESS_RESULTS for entry in dispatches
            ),
            "internal_terminal_attempt_count": len(terminals),
            "internal_terminal_drops_before_wsi": sum(
                entry["wsi"] == 0 and entry["outcome"] == "dropped"
                for entry in terminals
            ),
            "internal_terminal_failures_before_wsi": sum(
                entry["wsi"] == 0 and entry["outcome"] == "failed"
                for entry in terminals
            ),
            "total_wsi": len(all_wsi),
            "internal_to_source_ratio": len(internal) / max(1, len(source)),
            "internal_drop_reasons": dict(Counter(re.findall(r"InternalWSI: dropped (?:opportunity|request)[^\r\n]*?reason=([a-z-]+)", experiment["log"]))),
            "source_present_cpu_us": timing_summary(source_durations),
        },
        "checks": {
            "compared_common_application_presents": common,
            "application_present_count_delta_experiment_minus_baseline": n_exp - n_base,
            "present_hresult_prefix_equal": True,
            "last_present_count_prefix_equal": True,
            "frame_statistics_hresult_prefix_equal": True,
            "frame_statistics_app_count_stays_within_baseline_lag_envelope": True,
            "timed_run_present_delta_within_one_boundary_frame": abs(n_base - n_exp) <= 1,
            "waitable_poll_common_prefix_equal": waitable_poll_order_equal,
            "app_frame_latency_signals_match_each_run_source_ids": True,
            "app_frame_latency_signal_prefix_equal": app_signal_prefix_equal,
            "app_frame_latency_signal_count_matches_each_run_app_present_total": (
                len(baseline["app_latency_signals"]) == n_base
                and len(experiment["app_latency_signals"]) == n_exp
            ),
            "maximum_frame_latency_equal": True,
            "backbuffer_index_sequence_equal": True,
            "getbuffer_identity_stable_within_each_run": True,
            "device_removed_prefix_equal_and_ok": True,
            "app_source_present_matches_successful_app_present_count": True,
            "wsi_count_equals_source_plus_internal": True,
            "wsi_ids_unique_and_monotonic": True,
            "internal_has_no_app_frame_id": True,
            "present_acquire_serials_unique_and_ordered": True,
            "acquire_serial_gaps_accounted_by_pre_wsi_internal_retirements": True,
            "internal_terminal_retirement_exactly_once": True,
            "no_duplicate_or_stale_image_retirement_assertion": True,
        },
        "visual_gate": "DIRECT HUMAN OBSERVATION REQUIRED",
        "timing_note": "GetFrameStatistics HRESULTs are compared; PresentCount must be monotonic, never exceed app LastPresentCount, and remain within the frozen baseline 0-2 frame lag envelope. Zero-timeout waitable poll results are timing observations and may shift with asynchronous completion; actual app-frame latency signal calls are logged and checked for uniqueness and source-only identity. Refresh and QPC fields are timing observations because added output changes actual display scheduling.",
        "display_timing": "VK_GOOGLE_display_timing not integrated; present submission/retirement is not actual display timing",
    }
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered)
    print(rendered, end="")


if __name__ == "__main__":
    main()
