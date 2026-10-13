#!/usr/bin/env python3
"""Lightweight offline tests for BR8 notification-transition analysis."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from experiments.dxvk_macos_step11d3b import br8_analysis as analysis


NS = 1_000_000_000


def frame(acquire_ns: int, *, outcome: str = "presented",
          latency_ns: int = 100_000_000, duration_ns: int = 2_000_000,
          callback_count: int = 1) -> dict:
    exact = outcome in ("presented", "zero")
    request_ns = acquire_ns + 1_000
    callback_ns = request_ns + latency_ns if callback_count == 1 else None
    return {
        "acquisition_sequence": acquire_ns,
        "host_acquire_ts_mach_ns": acquire_ns,
        "host_submit_ts_mach_ns": acquire_ns + 500,
        "request_ts_mach_ns": request_ns,
        "callback_ts_mach_ns": callback_ns,
        "presented_time_mach_ns": 0 if outcome == "zero" else 1 if outcome == "presented" else None,
        "next_drawable_duration_ns": duration_ns,
        "host_submit_duration_ns": duration_ns + 500,
        "exact_chain": exact,
        "outcome_category": outcome,
        "outcome_enum": "CALLBACK_PRESENTED_TIME_ZERO" if outcome == "zero" else
                        "DISPLAYED_VALID_PRESENTED_TIME" if outcome == "presented" else "UNKNOWN",
        "event_counts": {"acquire": 1, "next_drawable": 1, "submit": 1,
                          "request": 1, "callback": callback_count},
    }


def telemetry(stamp: int, trigger: str, *, name: str | None = None,
              active: bool = True, key: bool = True, sequence: int = 1) -> dict:
    return {
        "ts_mach_ns": stamp,
        "telemetry_sequence": sequence,
        "trigger": trigger,
        "notification_name": name,
        "window_associated": True,
        "window_id": 9,
        "window_visible": True,
        "window_key": key,
        "window_main": True,
        "window_occluded": False,
        "window_miniaturized": False,
        "app_active": active,
    }


class BR8AnalysisTests(unittest.TestCase):
    def _transition_result(self) -> dict:
        frames = [frame(second * NS + NS // 2, latency_ns=10_000_000)
                  for second in range(1, 70)]
        rows = [
            telemetry(100_000_000, "initial", sequence=1),
            telemetry(19 * NS + NS // 2, "periodic", sequence=2),
            telemetry(20 * NS, "window_notification",
                      name="NSWindowDidResignKeyNotification", active=False,
                      key=False, sequence=3),
            telemetry(30 * NS, "periodic", active=False, key=False, sequence=4),
            telemetry(44 * NS + NS // 2, "periodic", active=False, key=False, sequence=5),
            telemetry(45 * NS, "app_notification",
                      name="NSApplicationDidBecomeActiveNotification", sequence=6),
        ]
        return {
            "summary": {"run_id": "synthetic-transition", "run_directory": "/synthetic",
                        "host_interval": {"start_ns": 0, "end_ns": 70 * NS}},
            "frames": frames,
            "window_telemetry": rows,
        }

    def test_notification_phases_use_exact_half_open_intervals(self):
        source = self._transition_result()
        source["summary"]["refresh_phase"] = {
            "reference_period_ns": 16_666_500,
            "reference_presented_time_mach_ns": 1_000_000_000,
        }
        result = analysis.analyze_result(source)
        transitions = result["notification_transitions"]
        self.assertEqual([row["timestamp_ns"] for row in transitions], [20 * NS, 45 * NS])
        first = transitions[0]
        self.assertEqual(first["transition_direction"], "change_away_from_initial_observed_state")
        self.assertEqual(first["phases"]["before_10s"]["acquisition_cohort"]["acquired_count"], 10)
        self.assertEqual(first["phases"]["first_10s_after"]["acquisition_cohort"]["acquired_count"], 10)
        phase_reference = first["phases"]["first_10s_after"]["request_phase"]["refresh_phase"]
        self.assertEqual(phase_reference["status"], "valid_run_wide_reference")
        self.assertEqual(phase_reference["request_count"], 10)
        self.assertIn("run-wide", phase_reference["reference_uncertainty"])
        hold = first["phases"]["steady_hold"]
        self.assertEqual(hold["interval"]["requested_start_ns"], 30 * NS)
        self.assertEqual(hold["interval"]["requested_end_ns"], 45 * NS)
        self.assertEqual(hold["acquisition_cohort"]["acquired_count"], 15)
        restored = transitions[1]
        self.assertEqual(restored["transition_direction"], "restoration_to_initial_observed_state")
        self.assertEqual(
            restored["phases"]["restoration_10s_after"]["acquisition_cohort"]["acquired_count"], 10)
        self.assertFalse(result["causal_inference"])

    def test_request_phase_counts_callback_latency_thresholds(self):
        frames = [
            frame(NS, latency_ns=100_000_000),
            frame(2 * NS, latency_ns=900_000_000),
            frame(3 * NS, latency_ns=899_000_000),
        ]
        metrics = analysis._phase_metrics(frames, 0, 10 * NS, 0, 10 * NS)
        request = metrics["request_phase"]
        self.assertEqual(request["unique_request_count"], 3)
        self.assertEqual(request["callback_latency_ge_100ms_count"], 3)
        self.assertEqual(request["callback_latency_ge_900ms_count"], 1)
        self.assertEqual(request["request_to_callback_latency_ns"]["count"], 3)

    def test_all_zero_phase_uses_only_an_independently_supplied_run_reference(self):
        frames = [frame(NS, outcome="zero"), frame(2 * NS, outcome="zero")]
        without_reference = analysis._phase_metrics(frames, 0, 10 * NS, 0, 10 * NS)
        no_phase = without_reference["request_phase"]["refresh_phase"]
        self.assertEqual(no_phase["status"], "reference_unavailable")
        self.assertEqual(no_phase["request_count"], 0)
        self.assertEqual(no_phase["phase_fraction_stats"]["count"], 0)
        self.assertTrue(all(item["percent"] is None for item in no_phase["quartiles"]))

        independent_run_reference = {
            "reference_period_ns": NS,
            "reference_presented_time_mach_ns": NS // 2,
        }
        with_reference = analysis._phase_metrics(
            frames, 0, 10 * NS, 0, 10 * NS,
            refresh_reference=independent_run_reference)
        phase = with_reference["request_phase"]["refresh_phase"]
        self.assertEqual(phase["status"], "valid_run_wide_reference")
        self.assertEqual(phase["request_count"], 2)
        self.assertEqual(phase["phase_fraction_stats"]["count"], 2)
        self.assertAlmostEqual(phase["phase_fraction_stats"]["mean"], 0.500001)
        self.assertEqual(sum(item["count"] for item in phase["quartiles"]), 2)

    def test_next_drawable_stall_counts_include_exact_thresholds(self):
        frames = [
            frame(NS, duration_ns=100_000_000),
            frame(2 * NS, duration_ns=900_000_000),
            frame(3 * NS, duration_ns=99_999_999),
        ]
        metrics = analysis._phase_metrics(frames, 0, 10 * NS, 0, 10 * NS)
        cohort = metrics["acquisition_cohort"]
        self.assertEqual(cohort["next_drawable_ge_100ms_count"], 2)
        self.assertEqual(cohort["next_drawable_ge_900ms_count"], 1)

    def test_pathology_requires_five_seconds_and_high_acquisition(self):
        frames = []
        for second in range(6):
            for ordinal in range(80):
                stamp = second * NS + ordinal * (NS // 80)
                if ordinal < 50:
                    outcome = "zero"
                elif ordinal < 75:
                    outcome = "presented"
                else:
                    outcome = "ambiguous"
                frames.append(frame(stamp, outcome=outcome))
        summary = {"host_interval": {"start_ns": 0, "end_ns": 6 * NS}}
        assessed = analysis._pathology_assessment({
            "summary": summary,
            "frames": frames,
            "window_telemetry": [telemetry(0, "initial") | {
                "screen_refresh_available": True, "screen_refresh_hz": 60.0}],
        })
        self.assertTrue(assessed["sustained_pathology_observed"])
        self.assertEqual(assessed["sustained_segments"][0]["consecutive_seconds"], 6)
        self.assertEqual(assessed["criteria"]["acquired_fps_at_least"], 75.0)
        self.assertEqual(assessed["bins"][0]["exact_zero_share_of_exact_outcomes"], 2 / 3)

        slow_frames = []
        for second in range(5):
            for ordinal in range(10):
                outcome = "zero" if ordinal < 8 else "presented"
                slow_frames.append(frame(second * NS + ordinal * 50_000_000, outcome=outcome))
        slow = analysis._pathology_assessment({
            "summary": {"host_interval": {"start_ns": 0, "end_ns": 5 * NS}},
            "frames": slow_frames,
        })
        self.assertFalse(slow["sustained_pathology_observed"])
        self.assertEqual(slow["low_acquisition_zero_bins_excluded"], 5)
        self.assertTrue(all(row["qualification_reason"] == "acquisition_rate_below_above_refresh_floor"
                            for row in slow["bins"]))

    def test_visible_bit_does_not_claim_physical_occlusion_or_coverage_extent(self):
        frames = [frame(NS, outcome="zero"), frame(2 * NS, outcome="zero")]
        no_visible_pixels = [telemetry(NS, "periodic", sequence=1),
                             telemetry(2 * NS, "periodic", sequence=2)]
        for row in no_visible_pixels:
            row["window_occluded"] = True
        metrics = analysis._phase_metrics(frames, 0, 10 * NS, 0, 10 * NS, no_visible_pixels)
        state = metrics["window_state_observation"]
        self.assertEqual(state["classification"], "NO_VISIBLE_PIXELS_REPORTED")
        self.assertTrue(state["observed_display_suppression_state"])
        self.assertIn("does not identify a physical coverage extent", state["occlusion_semantics"])
        self.assertEqual(metrics["window_state_observation"]["acquisition_context"],
                         "no_fast_acquisition; report minimized/no-visible-pixels state separately")

        # Hidden and offscreen windows can also lack the visible-pixels bit;
        # the telemetry does not establish that another window covered them.
        for case, changes in (
            ("hidden", {"window_visible": False, "window_occluded": True}),
            ("offscreen", {"window_visible": True, "window_occluded": True,
                            "screen_id": None, "window_frame_x": 100_000.0}),
        ):
            samples = [telemetry(NS, "periodic", sequence=3),
                       telemetry(2 * NS, "periodic", sequence=4)]
            for row in samples:
                row.update(changes)
            with self.subTest(case=case):
                state = analysis._state_profile(samples, 0, 10 * NS)
                self.assertEqual(state["classification"], "NO_VISIBLE_PIXELS_REPORTED")
                self.assertNotIn("FULLY_OCCLUDED", state["classification"])

        # The visible bit only says that AppKit reports some visible pixels;
        # visible-frame geometry is not a physical overlap measurement.
        partial = [telemetry(NS, "periodic", sequence=5),
                   telemetry(2 * NS, "periodic", sequence=6)]
        for row in partial:
            row["window_occluded"] = False
            row["window_visible_intersection_area"] = 100.0
        partial_state = analysis._state_profile(partial, 0, 10 * NS)
        self.assertEqual(partial_state["classification"], "AT_LEAST_SOME_VISIBLE_PIXELS_REPORTED")
        self.assertNotIn("PARTIALLY_OCCLUDED", partial_state["classification"])
        self.assertNotIn("FULLY_OCCLUDED", partial_state["classification"])

        minimized = [telemetry(NS, "periodic", sequence=3),
                     telemetry(2 * NS, "periodic", sequence=4)]
        for row in minimized:
            row["window_miniaturized"] = True
        metrics = analysis._phase_metrics(frames, 0, 10 * NS, 0, 10 * NS, minimized)
        self.assertEqual(metrics["window_state_observation"]["classification"], "MINIMIZED")

    def test_phase_exposes_duration_percentiles_and_marks_clipped_windows(self):
        metrics = analysis._phase_metrics([frame(4 * NS, duration_ns=7_000_000)],
                                          -5 * NS, 5 * NS, 0, 20 * NS)
        self.assertFalse(metrics["interval"]["complete"])
        self.assertEqual(metrics["interval"]["observed_coverage_ns"], 5 * NS)
        self.assertEqual(metrics["acquisition_cohort"]["acquired_fps"], 0.2)
        self.assertEqual(metrics["acquisition_cohort"]["next_drawable_duration_ns"]["median_ns"],
                         7_000_000.0)
        self.assertEqual(metrics["acquisition_cohort"]["host_submit_duration_ns"]["max_ns"],
                         7_000_500)

    def test_json_artifact_keeps_action_log_unjoined(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "br8" / "transition-analysis.json"
            actions = {"records": [{"action": "restore", "timestamp": "controller-clock"}],
                       "used_as_notification_anchor": False, "clock_join_performed": False}
            analysis.write_analysis([{"run_id": "synthetic"}], output, actions)
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["analysis_kind"], "offline_notification_transition_analysis")
        self.assertFalse(payload["controller_action_log"]["clock_join_performed"])
        self.assertEqual(payload["runs"][0]["run_id"], "synthetic")


if __name__ == "__main__":
    unittest.main()
