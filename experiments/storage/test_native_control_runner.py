#!/usr/bin/env python3
"""Tests that the native graphics runner gates PASS on offline trace validation."""
from __future__ import annotations

import copy
import unittest
from pathlib import Path

from experiments.dxvk_macos_step11d3a.run_native_control import (
    validate_graphics_trace_events,
)
from experiments.storage.test_trace_validator import valid_trace


class TestNativeControlRunnerValidation(unittest.TestCase):
    def setUp(self):
        self.dylib_path = Path("/tmp/libMoltenVK.dylib")

    def test_valid_complete_trace_allows_runner_pass(self):
        result = validate_graphics_trace_events(
            valid_trace(), requested_seconds=30, dylib_path=self.dylib_path)
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["is_valid"])
        self.assertTrue(result["quantitative_ready"])
        self.assertEqual(result["requested_run_seconds"], 30)
        self.assertEqual(result["frame_count"], 1)

    def test_early_window_close_cannot_receive_runner_pass(self):
        events = copy.deepcopy(valid_trace())
        stop = next(event for event in events if event["event"] == "stop_requested")
        stop["reason"] = "window_closed"
        result = validate_graphics_trace_events(
            events, requested_seconds=30, dylib_path=self.dylib_path)
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["is_valid"])
        self.assertIn("frame loop", result["errors"][0])

    def test_trace_duration_must_match_runner_mode(self):
        result = validate_graphics_trace_events(
            valid_trace(), requested_seconds=300, dylib_path=self.dylib_path)
        self.assertEqual(result["status"], "FAIL")
        self.assertFalse(result["quantitative_ready"])
        self.assertTrue(any("duration differs" in error for error in result["errors"]))


if __name__ == "__main__":
    unittest.main()
