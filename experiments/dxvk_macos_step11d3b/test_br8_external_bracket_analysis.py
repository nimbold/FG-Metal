import unittest

from br8_external_bracket_analysis import (
    NS,
    summarize_inter_action_steady_hold,
    summarize_interval,
)


class ExternalBracketSummaryTests(unittest.TestCase):
    def test_separates_acquisition_and_request_slices_and_counts_stalls(self):
        frames = [
            {
                "native_acquire_begin_mach_ns": 100,
                "present_request_mach_ns": 100,
                "presented_callback_mach_ns": 50_000_100,
                "presented_time_mach_ns": 1,
                "is_displayed": True,
                "next_drawable_duration_ns": 28_000_000,
                "native_submit_duration_ns": 12_000_000,
                "request_phase_fraction": 0.10,
            },
            {
                "native_acquire_begin_mach_ns": 500_000_100,
                "present_request_mach_ns": 1_100_000_100,
                "presented_callback_mach_ns": 2_000_000_100,
                "presented_time_mach_ns": 0,
                "is_displayed": False,
                "next_drawable_duration_ns": 900_000_000,
                "native_submit_duration_ns": 14_000_000,
                "request_phase_fraction": 0.80,
            },
        ]

        summary = summarize_interval(frames, 0, 1_000_000_000, 0, 2_000_000_000)

        acquisition = summary["acquisition_time_cohort"]
        self.assertEqual(acquisition["count"], 2)
        self.assertEqual(acquisition["displayed_count"], 1)
        self.assertEqual(acquisition["raw_zero_callback_count"], 1)
        self.assertEqual(acquisition["next_drawable_ge_100ms_count"], 1)
        self.assertEqual(acquisition["next_drawable_ge_900ms_count"], 1)
        request = summary["request_time_cohort"]
        self.assertEqual(request["unique_request_count"], 1)
        self.assertEqual(request["callback_latency_ge_100ms_count"], 0)
        self.assertEqual(request["request_phase_fraction"]["quartiles"], [1, 0, 0, 0])

    def test_marks_trace_clipped_pre_action_window_incomplete(self):
        summary = summarize_interval([], -1_000_000_000, 0, 0, 2_000_000_000)
        self.assertFalse(summary["complete"])
        self.assertEqual(summary["observed_coverage_ns"], 0)
        self.assertIsNone(summary["acquisition_time_cohort"]["acquired_fps"])

    def test_inter_action_hold_uses_exact_half_open_wrapper_boundaries(self):
        frames = [
            {
                "native_acquire_begin_mach_ns": 10 * NS + 100,
                "present_request_mach_ns": 10 * NS + 100,
                "presented_callback_mach_ns": 10 * NS + 200,
                "presented_time_mach_ns": 0,
                "is_displayed": False,
                "next_drawable_duration_ns": 15_000_000,
                "native_submit_duration_ns": 2_000_000,
                "request_phase_fraction": 0.25,
            },
            {
                "native_acquire_begin_mach_ns": 20 * NS + 100,
                "present_request_mach_ns": 20 * NS + 100,
                "presented_callback_mach_ns": 20 * NS + 200,
                "presented_time_mach_ns": 1,
                "is_displayed": True,
                "next_drawable_duration_ns": 16_000_000,
                "native_submit_duration_ns": 3_000_000,
                "request_phase_fraction": 0.75,
            },
        ]

        result = summarize_inter_action_steady_hold(
            frames,
            entry_wrapper_end_ns=100,
            restoration_wrapper_start_ns=20 * NS + 100,
            run_start_ns=0,
            run_end_ns=30 * NS,
        )

        metrics = result["metrics"]
        self.assertEqual(result["interval_rule"],
                         "[entry_wrapper_end + 10s, restoration_wrapper_start)")
        self.assertEqual(metrics["requested_interval_mach_ns"],
                         {"start": 10 * NS + 100, "end": 20 * NS + 100})
        self.assertEqual(metrics["acquisition_time_cohort"]["count"], 1)
        self.assertEqual(metrics["acquisition_time_cohort"]["raw_zero_callback_count"], 1)
        self.assertTrue(metrics["complete"])
        self.assertIn("does not prove continuous geometry",
                      result["boundary_semantics"])


if __name__ == "__main__":
    unittest.main()
