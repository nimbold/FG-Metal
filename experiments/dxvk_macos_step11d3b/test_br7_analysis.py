#!/usr/bin/env python3
"""Offline tests for BR7 raw-trace analysis; no renderer or storage runs."""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from experiments.dxvk_macos_step11d3b import br7_analysis as analysis


class BR7AnalysisTests(unittest.TestCase):
    def _joined_traces(self):
        from experiments.storage.test_bare_wine_trace_validator import BareWineTraceValidatorTests

        fixture = BareWineTraceValidatorTests()
        fixture.setUp()
        app, host, _loader = fixture._valid_traces()
        host = copy.deepcopy(host)
        app = copy.deepcopy(app)
        # Adapt the validator's earlier present-end event to BR7's request row.
        for row in host:
            if row.get("origin") == "MVK_BOUNDARY" and row.get("event") == "vkQueuePresentKHR_end":
                row["event"] = "present_request"
        host_acquires = {row.get("acquisition_sequence"): row for row in host
                         if row.get("origin") == "MVK_BOUNDARY"
                         and row.get("event") == "vkAcquireNextImageKHR_end"}
        for row in app:
            sequence = row.get("app_acquire_seq")
            native = host_acquires.get(sequence)
            if native and row.get("event") in {
                    "app_vkAcquireNextImageKHR_end", "app_vkQueueSubmit2_end",
                    "app_vkQueuePresentKHR_end"}:
                row["image_index"] = native["image_index"]
                row["image_id"] = native["image_id"]
        # End marker follows the synthetic trace events and has no drops.
        for row in host:
            if row.get("event") == "trace_summary":
                row.update(ts_mach_ns=2_000_000_000, dropped_count=0, write_failure_count=0)
        return app, host

    def test_full_chain_classifies_exact_zero_and_positive_callbacks(self):
        app, host = self._joined_traces()
        callbacks = [row for row in host if row.get("event") == "presented_callback"]
        callbacks[1]["presented_time_mach_ns"] = 0
        callbacks[1]["is_displayed"] = False
        frames = analysis._extract_frames(app, host)
        self.assertEqual([frame["exact_chain"] for frame in frames], [True, True, True])
        self.assertEqual([frame["outcome_enum"] for frame in frames], [
            "DISPLAYED_VALID_PRESENTED_TIME", "CALLBACK_PRESENTED_TIME_ZERO",
            "DISPLAYED_VALID_PRESENTED_TIME",
        ])
        self.assertEqual(frames[0]["outcome_category"], "presented")
        self.assertEqual(frames[1]["outcome_category"], "zero")

    def test_duplicate_callback_is_unknown_and_never_double_classified(self):
        app, host = self._joined_traces()
        callback = next(row for row in host if row.get("event") == "presented_callback"
                        and row.get("acquisition_sequence") == 1)
        duplicate = copy.deepcopy(callback)
        duplicate["event_id"] += 1000
        host.append(duplicate)
        first = analysis._extract_frames(app, host)[0]
        self.assertFalse(first["exact_chain"])
        self.assertEqual(first["outcome_enum"], "UNKNOWN")
        self.assertIn("callback_count_2", first["ambiguity_reasons"])

    def test_missing_callback_needs_valid_trace_tail_and_marks_terminal_censor(self):
        app, host = self._joined_traces()
        callback = next(row for row in host if row.get("event") == "presented_callback"
                        and row.get("acquisition_sequence") == 1)
        host.remove(callback)
        next(row for row in host if row.get("event") == "trace_summary")["ts_mach_ns"] = 3_000_000_000
        first = analysis._extract_frames(app, host)[0]
        self.assertFalse(first["exact_chain"])
        self.assertEqual(first["outcome_enum"], "NO_PRESENT_CALLBACK")
        self.assertFalse(first["terminal_censored"])

        app, host = self._joined_traces()
        callback = next(row for row in host if row.get("event") == "presented_callback"
                        and row.get("acquisition_sequence") == 1)
        host.remove(callback)
        request = next(row for row in host if row.get("event") == "present_request"
                       and row.get("acquisition_sequence") == 1)
        summary = next(row for row in host if row.get("event") == "trace_summary")
        summary["ts_mach_ns"] = request["ts_mach_ns"] + 500_000_000
        first = analysis._extract_frames(app, host)[0]
        self.assertEqual(first["outcome_enum"], "UNKNOWN")
        self.assertTrue(first["terminal_censored"])

    def test_acquisition_accepts_suboptimal_image_and_excludes_failed_result(self):
        app, host = self._joined_traces()
        host_acquire = next(row for row in host if row.get("origin") == "MVK_BOUNDARY"
                            and row.get("event") == "vkAcquireNextImageKHR_end"
                            and row.get("acquisition_sequence") == 1)
        self.assertTrue(analysis._acquire_success({**host_acquire, "vk_result": 1000001003}))
        host_acquire["vk_result"] = -1
        frames = analysis._extract_frames(app, host)
        self.assertEqual([frame["acquisition_sequence"] for frame in frames], [2, 3])

    def test_zero_bursts_break_at_ambiguous_or_missing_acquisition(self):
        def frame(sequence, outcome, exact=True):
            return {"acquisition_sequence": sequence, "outcome_category": outcome,
                    "exact_chain": exact, "callback_ts_mach_ns": sequence * 10}
        summary, bursts = analysis._zero_bursts([
            frame(1, "zero"), frame(2, "ambiguous", exact=False), frame(3, "zero")])
        self.assertEqual(summary["burst_count"], 2)
        self.assertEqual([row["frame_count"] for row in bursts], [1, 1])
        summary, bursts = analysis._zero_bursts([frame(1, "zero"), frame(3, "zero")])
        self.assertEqual(summary["burst_count"], 2)

    def test_sustained_regimes_are_mixed_and_transition_is_bin_bracketed(self):
        windows = []
        for index in range(10):
            healthy = index < 5
            windows.append({
                "window_index": index,
                "start_elapsed_ns": index * 1_000_000_000,
                "end_elapsed_ns": (index + 1) * 1_000_000_000,
                "exact_outcome_count": 20,
                "zero_share_of_exact_outcomes": 0.0 if healthy else 0.75,
                "candidate_regime": "healthy_candidate" if healthy else "pathological_candidate",
            })
        result = analysis._sustained_regimes(windows)
        self.assertEqual(result["overall_label"], "MIXED")
        self.assertEqual(len(result["stable_segments"]), 2)
        transition = result["transitions"][0]
        self.assertEqual(transition["lower_elapsed_ns"], 4_000_000_000)
        self.assertEqual(transition["upper_elapsed_ns"], 6_000_000_000)
        self.assertEqual(transition["classifier_resolution_ns"], 1_000_000_000)

    def test_window_telemetry_ownership_association_asof_and_staleness(self):
        def telemetry(sequence, stamp, **fields):
            return {"origin": "LAYER_SOURCE", "event": "window_telemetry",
                    "trace_role": "target", "host_pid": 77, "target_unix_pid": 77,
                    "telemetry_sequence": sequence, "ts_mach_ns": stamp,
                    "layer_id": 7, "window_id": 22, "window_associated": True,
                    "trigger": "periodic", "window_visible": True, **fields}
        rows = [telemetry(1, 100), telemetry(2, 200)]
        accepted, errors = analysis._validate_telemetry(rows, 77, 7)
        self.assertEqual(errors, [])
        frames = [{"callback_ts_mach_ns": 150, "outcome_category": "zero"},
                  {"callback_ts_mach_ns": 2_000_000_201, "outcome_category": "presented"}]
        analysis._correlate_telemetry(frames, accepted)
        self.assertEqual(frames[0]["state_correlation"]["telemetry_sequence"], 1)
        self.assertEqual(frames[0]["state_correlation"]["age_ns"], 50)
        self.assertEqual(frames[1]["state_correlation"]["reason"], "prior_snapshot_stale")

        wrong_owner = telemetry(3, 300, host_pid=88)
        accepted, errors = analysis._validate_telemetry([wrong_owner], 77, 7)
        self.assertEqual(accepted, [])
        self.assertIn("not owned by target PID", errors[0])
        unassociated = telemetry(4, 300, window_id=0, window_associated=False)
        accepted, errors = analysis._validate_telemetry([unassociated], 77, 7)
        frame = {"callback_ts_mach_ns": 301, "outcome_category": "zero"}
        analysis._correlate_telemetry([frame], accepted)
        self.assertEqual(frame["state_correlation"]["status"], "unknown")
        self.assertEqual(frame["state_correlation"]["reason"], "snapshot_has_no_associated_window")

    def test_trace_index_selects_only_owned_target_and_rejects_pid_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            run = Path(temp)
            pid = 4242
            app_path = run / "app.jsonl"
            host_path = run / "host.jsonl"
            app_path.write_text(json.dumps({"origin": "APP_CONFIG", "event": "app_configuration",
                                            "target_unix_pid": pid}) + "\n")
            target_rows = [
                {"origin": "INSTRUMENTATION", "event": "clock_metadata", "trace_role": "target",
                 "host_pid": pid, "target_unix_pid": pid},
                {"origin": "LOGGER", "event": "trace_summary", "trace_role": "target",
                 "host_pid": pid, "target_unix_pid": pid, "dropped_count": 0,
                 "write_failure_count": 0},
            ]
            host_path.write_text("".join(json.dumps(row) + "\n" for row in target_rows))
            helper_path = run / "helper.jsonl"
            helper_path.write_text("{}\n")
            (run / "run-result.json").write_text(json.dumps({
                "status": "PASS", "is_valid": True, "app_trace_path": str(app_path),
                "host_trace_path": str(host_path),
            }))
            (run / "trace-index.json").write_text(json.dumps({"errors": [], "processes": [
                {"trace_role": "target", "target_unix_pid": pid, "host_pid": pid,
                 "parse_status": "valid", "ownership_valid": True,
                 "summary": {"dropped_count": 0}, "path": str(host_path)},
                {"trace_role": "helper", "target_unix_pid": 9999, "host_pid": 9999,
                 "parse_status": "valid", "ownership_valid": True,
                 "summary": {"dropped_count": 0}, "path": str(helper_path)},
            ]}))
            (run / "run-manifest.json").write_text(json.dumps({"app_trace_path_host": str(app_path)}))
            loaded = analysis._load_owned_inputs(run)
            self.assertEqual(loaded[-1], pid)
            self.assertEqual(len(loaded[3]), 2)
            target_rows[0]["target_unix_pid"] = pid + 1
            host_path.write_text("".join(json.dumps(row) + "\n" for row in target_rows))
            with self.assertRaisesRegex(analysis.AnalysisError, "PID ownership mismatch"):
                analysis._load_owned_inputs(run)

    def test_per_window_timing_and_callback_rates_use_event_end_bins(self):
        rows = [
            {"origin": "LAYER_SOURCE", "event": "next_drawable_end", "detail": "acquired",
             "ts_mach_ns": 100, "duration_ns": 500},
            {"origin": "LAYER_SOURCE", "event": "next_drawable_end", "detail": "acquired",
             "ts_mach_ns": 1_000_000_100, "duration_ns": 900},
            {"origin": "WINE_SOURCE", "event": "vkQueueSubmit2_end", "ts_mach_ns": 100,
             "duration_ns": 300, "vk_result": 0},
            {"origin": "WINE_SOURCE", "event": "vkQueueSubmit2_end", "ts_mach_ns": 1_000_000_100,
             "duration_ns": 700, "vk_result": 0},
            {"origin": "LAYER_SOURCE", "event": "presented_callback", "ts_mach_ns": 100,
             "presented_time_mach_ns": 0, "is_displayed": False},
            {"origin": "LAYER_SOURCE", "event": "presented_callback", "ts_mach_ns": 1_000_000_100,
             "presented_time_mach_ns": 1, "is_displayed": True},
        ]
        windows = analysis._make_windows(rows, 100, 2_000_000_100, 1_000_000_000, stream="host")
        self.assertEqual(windows[0]["next_drawable_p99_ns"], 500)
        self.assertEqual(windows[1]["host_submit_max_ns"], 700)
        self.assertEqual(windows[0]["zero_percent_of_callbacks"], 100.0)
        self.assertEqual(windows[1]["displayed_callback_count"], 1)

    def test_host_phase_rates_count_wine_api_calls_once_from_wine_source(self):
        rows = [
            {"origin": "WINE_SOURCE", "event": "vkAcquireNextImageKHR_end",
             "ts_mach_ns": 100, "vk_result": 0},
            {"origin": "MVK_BOUNDARY", "event": "vkAcquireNextImageKHR_end",
             "ts_mach_ns": 101, "vk_result": 0},
            {"origin": "WINE_SOURCE", "event": "vkQueueSubmit2_end",
             "ts_mach_ns": 100, "duration_ns": 300, "vk_result": 0},
            {"origin": "MVK_BOUNDARY", "event": "vkQueueSubmit2_end",
             "ts_mach_ns": 101, "duration_ns": 900, "vk_result": 0},
            {"origin": "LAYER_SOURCE", "event": "next_drawable_end", "detail": "acquired",
             "ts_mach_ns": 100, "duration_ns": 500},
            {"origin": "LAYER_SOURCE", "event": "presented_callback", "ts_mach_ns": 100,
             "presented_time_mach_ns": 100, "is_displayed": True},
        ]
        windows = analysis._make_windows(
            rows, 100, 1_000_000_100, 1_000_000_000, stream="host")
        self.assertEqual(windows[0]["acquisition_count"], 1)
        self.assertEqual(windows[0]["submit_count"], 1)
        self.assertEqual(windows[0]["callback_count"], 1)
        self.assertEqual(windows[0]["next_drawable_p50_ns"], 500)
        self.assertEqual(windows[0]["host_submit_p50_ns"], 300)

    def test_sustained_mode_segments_receive_separate_timing_stats(self):
        regime = {"stable_segments": [{"regime": "HEALTHY", "start_elapsed_ns": 0,
                                      "end_elapsed_ns": 1_000_000_000}]}
        frames = [{"host_acquire_ts_mach_ns": 100, "exact_chain": True,
                   "outcome_category": "presented", "next_drawable_duration_ns": 1000,
                   "host_submit_duration_ns": 500}]
        analysis._enrich_regime_segments(regime, frames, 100)
        self.assertEqual(regime["stable_segments"][0]["next_drawable_p99_ns"], 1000)
        self.assertEqual(regime["stable_segments"][0]["host_submit_p50_ns"], 500)


if __name__ == "__main__":
    unittest.main()
