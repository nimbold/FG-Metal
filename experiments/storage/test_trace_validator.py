#!/usr/bin/env python3
"""Tests for the fail-closed STEP 11D.3-A unified trace validator."""
from __future__ import annotations

import copy
import subprocess
import sys
import unittest

from experiments.storage.trace_validator import TraceValidator, ValidationError


def _event(event_id, origin, name, ts, tid, **fields):
    return {"event_id": event_id, "origin": origin, "event": name,
            "ts_mach_ns": ts, "tid": tid, **fields}


def valid_trace(*, presented_time=390, displayed=True):
    frame = 1
    acquisition = 42
    drawable = 99
    texture = 199
    driver_submit = 7
    driver_present = 13
    app_common = {"app_frame_id": frame}
    mvk_common = {"app_frame_id": frame, "swapchain_id": 111}
    events = [
        _event(1, "APP_SOURCE", "acquire_begin", 100, 10,
               **app_common, app_acquire_sequence=1, frame_id=frame,
               frame_slot=0, swapchain_id=111),
        _event(2, "MVK_SOURCE", "vkAcquireNextImageKHR_begin", 105, 10,
               **mvk_common),
        _event(3, "MVK_SOURCE", "acquire_image_assigned", 115, 10,
               **mvk_common, acquisition_sequence=acquisition, image_index=0,
               image_id=333, detail="acquire_image_assigned"),
        _event(4, "MVK_SOURCE", "vkAcquireNextImageKHR_end", 132, 10,
               **mvk_common, acquisition_sequence=acquisition, image_index=0,
               image_id=333, call_begin_mach_ns=110, call_end_mach_ns=130,
               duration_ns=20, vk_result=0),
        _event(5, "APP_SOURCE", "acquire_end", 140, 10,
               **app_common, app_acquire_sequence=1, frame_id=frame,
               image_index=0, image_id=333, swapchain_id=111,
               call_begin_mach_ns=101, call_end_mach_ns=139,
               duration_ns=38, vk_result=0),
        _event(6, "APP_SOURCE", "app_vkQueueSubmit2_begin", 200, 10,
               **app_common, frame_id=frame, image_index=0, swapchain_id=111,
               image_id=333, app_submit_seq=1, queue_id=44),
        _event(7, "MVK_SOURCE", "vkQueueSubmit2_begin", 205, 10,
               **mvk_common, driver_submit_seq=driver_submit, queue_id=44),
        _event(8, "MVK_SOURCE", "next_drawable_begin", 207, 10,
               **mvk_common, acquisition_sequence=acquisition, image_index=0,
               image_id=333,
               driver_submit_seq=driver_submit,
               request_id=9, attempt_index=0, detail="request_started"),
        _event(9, "MVK_SOURCE", "next_drawable_end", 213, 10,
               **mvk_common, acquisition_sequence=acquisition, image_index=0,
               image_id=333,
               driver_submit_seq=driver_submit,
               request_id=9, attempt_index=0, drawable_id=drawable,
               drawable_identity=888, texture_id=texture,
               call_begin_mach_ns=208, call_end_mach_ns=212,
               duration_ns=4, detail="acquired"),
        _event(10, "MVK_SOURCE", "vkQueueSubmit2_end", 214, 10,
               **mvk_common, driver_submit_seq=driver_submit, queue_id=44,
               call_begin_mach_ns=206, call_end_mach_ns=213,
               duration_ns=7, vk_result=0),
        _event(11, "APP_SOURCE", "app_vkQueueSubmit2_end", 220, 10,
               **app_common, frame_id=frame, image_index=0, swapchain_id=111,
               image_id=333, app_submit_seq=1, queue_id=44,
               call_begin_mach_ns=201, call_end_mach_ns=219,
               duration_ns=18, vk_result=0),
        _event(12, "APP_SOURCE", "app_vkQueuePresentKHR_begin", 300, 10,
               **app_common, frame_id=frame, image_index=0, swapchain_id=111,
               image_id=333, app_present_seq=1, queue_id=44),
        _event(13, "MVK_SOURCE", "vkQueuePresentKHR_begin", 305, 10,
               **mvk_common, driver_present_seq=driver_present,
               present_seq=driver_present, queue_id=44),
        _event(14, "MVK_SOURCE", "present_request", 307, 10,
               **mvk_common, driver_present_seq=driver_present,
               acquisition_sequence=acquisition, image_index=0, image_id=333,
               queue_id=44, present_seq=driver_present),
        _event(15, "MVK_SOURCE", "vkQueuePresentKHR_end", 320, 10,
               **mvk_common, driver_present_seq=driver_present, queue_id=44,
               present_seq=driver_present,
               call_begin_mach_ns=310, call_end_mach_ns=319,
               duration_ns=9, vk_result=0),
        _event(16, "APP_SOURCE", "app_vkQueuePresentKHR_end", 330, 10,
               **app_common, frame_id=frame, image_index=0, swapchain_id=111,
               image_id=333, app_present_seq=1,
               queue_id=44,
               call_begin_mach_ns=301, call_end_mach_ns=329,
               duration_ns=28, vk_result=0),
        _event(17, "MVK_SOURCE", "metal_present_request", 315, 10,
               **mvk_common, driver_present_seq=driver_present,
               acquisition_sequence=acquisition, image_index=0,
               image_id=333,
               drawable_id=drawable, texture_id=texture,
               present_seq=driver_present),
        _event(18, "MVK_SOURCE", "presented_handler_registered", 322, 21,
               **mvk_common, driver_present_seq=driver_present,
               acquisition_sequence=acquisition, image_index=0,
               image_id=333,
               drawable_id=drawable, texture_id=texture,
               present_id_google=1, present_seq=driver_present),
        _event(19, "MVK_SOURCE", "presented_callback", 400, 22,
               **mvk_common, driver_present_seq=driver_present,
               acquisition_sequence=acquisition, image_index=0,
               image_id=333,
               drawable_id=drawable, texture_id=texture,
               present_seq=driver_present,
               presented_time_mach_ns=presented_time,
               presentation_clock_domain="mach_absolute_time_ns",
               is_displayed=displayed,
               detail="displayed" if displayed else "presented_time_zero_unpresented"),
        _event(20, "MVK_SOURCE", "present_command_buffer_complete", 401, 22,
               **mvk_common, driver_present_seq=driver_present,
               acquisition_sequence=acquisition, image_index=0,
               image_id=333,
               drawable_id=drawable, texture_id=texture,
               present_seq=driver_present,
               detail="command_buffer_completed"),
        _event(21, "MVK_SOURCE", "drawable_release", 402, 22,
               **mvk_common, driver_present_seq=driver_present,
               acquisition_sequence=acquisition, image_index=0,
               image_id=333,
               drawable_id=drawable, texture_id=texture,
               present_seq=driver_present,
               detail="reference_release"),
        _event(22, "MVK_SOURCE", "presentation_availability_signal", 403, 22,
               **mvk_common, driver_present_seq=driver_present,
               acquisition_sequence=acquisition, image_index=0, image_id=333,
               present_seq=driver_present,
               drawable_id=drawable, texture_id=texture,
               detail="presentation_availability_signal_completed"),
        _event(0, "MVK_SOURCE", "make_available", 404, 22,
               **mvk_common, image_id=333, image_index=0,
               acquisition_sequence=acquisition, driver_present_seq=driver_present,
               present_seq=driver_present,
               detail="availability_transitioned"),
        _event(23, "APP_SOURCE", "execution_architecture", 404, 1,
               compiled_architecture="x86_64", process_translated=1,
               rosetta_query="sysctl.proc_translated; -1 means unavailable",
               moltenvk_loader_path="/tmp/libMoltenVK.dylib", loader_path_resolved=True),
        _event(24, "APP_SOURCE", "layer_state", 405, 1,
               reason="startup_after_control_configuration", changed=True,
               maximum_drawable_count=3, allows_next_drawable_timeout=True,
               display_sync_enabled=True, drawable_width=640, drawable_height=360,
               contents_scale=2.0, pixel_format=80,
               pixel_format_name="MTLPixelFormatBGRA8Unorm", framebuffer_only=True,
               display_id=1, display_uuid="display-uuid", display_name="Test Display",
               display_pixel_width=1280, display_pixel_height=720, display_mode_id=1,
               display_nominal_refresh_hz=60.0, screen_maximum_frames_per_second=60,
               refresh_rate_source="public API; zero means unavailable"),
        _event(25, "APP_SOURCE", "swapchain_configuration", 406, 1,
               requested_image_count=3, actual_image_count=3, image_width=640,
               image_height=360, vk_format_name="VK_FORMAT_B8G8R8A8_UNORM",
               present_mode_name="VK_PRESENT_MODE_FIFO_KHR",
               extent_matches_target=True, image_count_matches_target=True),
        _event(29, "APP_SOURCE", "device_wait_idle", 30_000_000_110, 10,
               vk_result=0, vk_result_name="VK_SUCCESS"),
        _event(28, "APP_SOURCE", "renderer_resources_created", 83, 10,
               frame_resources=3, swapchain_transfer_usage=False,
               cpu_readback=False, screen_capture=False),
        _event(26, "APP_SOURCE", "presentation_callback_drain", 30_000_000_120, 10,
               pending_callbacks=0, complete=True),
        _event(27, "LOGGER", "trace_dropped_summary", 410, 30,
               dropped_count=0, producer_gate_rejected_count=0,
               serialization_failure_count=0,
               write_failure_count_before_summary=0, ring_capacity=32768),
    ]
    # Initialization evidence is emitted before the renderer's timed window.
    pre_run_names = {"execution_architecture", "layer_state", "swapchain_configuration",
                     "renderer_resources_created"}
    pre_run = [event for event in events if event["event"] in pre_run_names]
    pre_run.sort(key=lambda event: {
        "execution_architecture": 80,
        "layer_state": 81,
        "swapchain_configuration": 82,
        "renderer_resources_created": 83,
    }[event["event"]])
    for event in pre_run:
        event["ts_mach_ns"] = {
            "execution_architecture": 80,
            "layer_state": 81,
            "swapchain_configuration": 82,
            "renderer_resources_created": 83,
        }[event["event"]]
    events = [event for event in events if event["event"] not in pre_run_names]
    events[0:0] = pre_run
    events.insert(4, _event(0, "APP_SOURCE", "run_start", 90, 10,
                            run_start_mach_ns=89, requested_seconds=30))
    events.insert(5, _event(0, "APP_SOURCE", "frame_fence_wait", 95, 10,
                            frame_slot=0, fence_id=55,
                            begin_mach_ns=91, end_mach_ns=93, duration_ns=2,
                            vk_result=0))
    events.insert(next(index for index, event in enumerate(events)
                       if event["event"] == "app_vkQueueSubmit2_begin"),
                  _event(0, "APP_SOURCE", "frame_recorded", 170, 10,
                         app_frame_id=frame, frame_id=frame, image_index=0,
                         image_id=333, palette_index=0))
    # The Metal request is logged inside vkQueuePresentKHR, before its end row.
    metal_request = next(event for event in events
                         if event["event"] == "metal_present_request")
    events.remove(metal_request)
    present_end_index = next(index for index, event in enumerate(events)
                             if event["event"] == "vkQueuePresentKHR_end")
    events.insert(present_end_index, metal_request)
    idle_index = next(index for index, event in enumerate(events)
                      if event["event"] == "device_wait_idle")
    events.insert(idle_index, _event(0, "APP_SOURCE", "stop_requested",
                                    30_000_000_089, 10,
                                    reason="frame_loop_completed"))
    events.append(_event(0, "APP_SOURCE", "run_end", 30_000_000_130, 10,
                         run_end_mach_ns=30_000_000_131, exit_code=0,
                         presentation_timing_extension_available=False,
                         presentation_completion_records=0, trace_healthy=True,
                         app_acquire_calls=1, submitted_frames=1, present_calls=1,
                         successful_present_calls=1))
    events.append(_event(0, "APP_SOURCE", "trace_sink_stop_request", 30_000_000_132, 1,
                         app_format_failure_count=0, logger_drop_count_before_stop=0,
                         logger_write_failure_count_before_stop=0))
    summary = next(event for event in events if event["event"] == "trace_dropped_summary")
    summary["ts_mach_ns"] = 30_000_000_133
    events.remove(summary)
    events.append(summary)
    _reindex(events)
    return events


def valid_trace_with_google_timing():
    events = valid_trace()
    for name in ("present_request", "app_vkQueuePresentKHR_end",
                 "metal_present_request", "presented_handler_registered",
                 "presented_callback"):
        next(event for event in events if event["event"] == name)["present_id_google"] = 1
    completion = _event(
        0, "APP_SOURCE", "presentation_completion_observed", 30_000_000_091, 10,
        present_id=1, timestamp_domain="VK_GOOGLE_display_timing nanoseconds",
        actual_present_time_ns=9_000_000_000_000,
        earliest_present_time_ns=8_999_999_999_000,
        present_margin_ns=1_000,
        observation_mach_ns=30_000_000_092,
        frame_id=1, image_index=0, image_id=333)
    drain = _event(
        0, "APP_SOURCE", "presentation_timing_drain", 30_000_000_100, 10,
        present_ids_submitted=1, completion_records_observed=1,
        unobserved_present_ids=0)
    idle_index = next(index for index, event in enumerate(events)
                      if event["event"] == "device_wait_idle")
    events.insert(idle_index, completion)
    events.insert(idle_index + 1, drain)
    run_end = next(event for event in events if event["event"] == "run_end")
    run_end["presentation_timing_extension_available"] = True
    run_end["presentation_completion_records"] = 1
    _reindex(events)
    return events


def _reindex(events):
    for event_id, event in enumerate(events, 1):
        event["event_id"] = event_id


class TestTraceValidator(unittest.TestCase):
    def test_cli_requires_runtime_architecture_translation_and_exact_library(self):
        result = subprocess.run(
            [sys.executable, "-m", "experiments.storage.trace_validator", "missing-trace.jsonl"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--expected-architecture", result.stderr)
        self.assertIn("required", result.stderr)

    def setUp(self):
        self.validator = TraceValidator()

    def test_parse_accepts_objects_and_rejects_malformed_json(self):
        parsed = self.validator.parse_lines(['{"event":"x"}', ''])
        self.assertEqual(len(parsed), 1)
        with self.assertRaises(ValidationError):
            self.validator.parse_lines(['{"event": }'])

    def test_bool_is_rejected_for_integer_result_identity_and_exit_code_fields(self):
        for event_name, key in (("run_end", "exit_code"),
                                ("acquire_end", "vk_result"),
                                ("make_available", "image_id")):
            with self.subTest(event=event_name, key=key):
                events = valid_trace()
                next(event for event in events if event["event"] == event_name)[key] = False
                report = self.validator.validate(events)
                self.assertFalse(report.is_valid)
                self.assertIn("must be an integer", report.errors[0])

    def test_bool_is_rejected_for_boolean_configuration_fields(self):
        events = valid_trace()
        next(event for event in events if event["event"] == "execution_architecture")[
            "loader_path_resolved"] = 1
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("must be a boolean", report.errors[0])

    def test_drawable_request_id_must_be_positive_and_globally_frame_unique(self):
        cases = (
            ([_event(1, "MVK_SOURCE", "next_drawable_end", 1, 2,
                     app_frame_id=1, request_id=0)], "positive integers"),
            ([_event(1, "MVK_SOURCE", "next_drawable_end", 1, 2,
                     app_frame_id=1, request_id=9),
             _event(2, "MVK_SOURCE", "next_drawable_end", 2, 2,
                     app_frame_id=2, request_id=9)], "reused across app frames"),
        )
        for events, message in cases:
            with self.subTest(message=message):
                with self.assertRaisesRegex(ValidationError, message):
                    self.validator._validate_drawable_request_ids(events)

    def test_make_available_requires_exact_frame_present_and_image_identity(self):
        for key in ("app_frame_id", "acquisition_sequence", "image_index",
                    "driver_present_seq", "swapchain_id", "image_id"):
            with self.subTest(key=key):
                events = valid_trace()
                row = next(event for event in events if event["event"] == "make_available")
                row.pop(key)
                report = self.validator.validate(events)
                self.assertFalse(report.is_valid)
                self.assertIn(key, report.errors[0])

    def test_present_and_lifecycle_edges_preserve_acquired_driver_identity(self):
        mutations = (
            ("present_request", "swapchain_id"),
            ("present_request", "image_id"),
            ("next_drawable_begin", "swapchain_id"),
            ("next_drawable_end", "image_id"),
            ("metal_present_request", "swapchain_id"),
            ("presented_handler_registered", "image_id"),
            ("presented_callback", "swapchain_id"),
            ("present_command_buffer_complete", "image_id"),
            ("drawable_release", "swapchain_id"),
            ("presentation_availability_signal", "image_id"),
            ("make_available", "swapchain_id"),
        )
        for event_name, field in mutations:
            with self.subTest(event=event_name, field=field):
                events = valid_trace()
                target = next(event for event in events if event["event"] == event_name)
                target[field] = 999
                report = self.validator.validate(events)
                self.assertFalse(report.is_valid)
                if event_name == "make_available":
                    self.assertIn("ambiguous frame join", report.errors[0])
                else:
                    self.assertIn(field, report.errors[0])

    def test_application_vkimage_handle_matches_the_moltenvk_acquired_image(self):
        events = valid_trace()
        acquire_end = next(event for event in events if event["event"] == "acquire_end")
        acquire_end["image_id"] = 999
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("VkImage handle differs", report.errors[0])

    def test_matching_wrong_swapchain_on_present_and_make_available_still_fails(self):
        events = valid_trace()
        for event_name in ("present_request", "make_available"):
            next(event for event in events if event["event"] == event_name)["swapchain_id"] = 999
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("swapchain_id", report.errors[0])

    def test_shutdown_events_must_follow_the_native_lifecycle(self):
        cases = (
            ("stop_requested", "stop_requested"),
            ("trace_sink_stop_request", "trace_sink_stop_request"),
        )
        for name, expected in cases:
            with self.subTest(event=name):
                events = [event for event in valid_trace() if event["event"] != name]
                _reindex(events)
                report = self.validator.validate(events)
                self.assertFalse(report.is_valid)
                self.assertIn(expected, report.errors[0])

        events = valid_trace()
        stop = next(event for event in events if event["event"] == "stop_requested")
        run_start = next(event for event in events if event["event"] == "run_start")
        stop["event_id"] = run_start["event_id"]
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)

    def test_timing_drain_must_precede_device_idle_and_follow_queries(self):
        events = valid_trace_with_google_timing()
        drain = next(event for event in events if event["event"] == "presentation_timing_drain")
        query = _event(0, "APP_SOURCE", "presentation_timing_query", 30_000_000_095, 10,
                       vk_result=-1, vk_result_name="VK_ERROR_UNKNOWN", record_count=0)
        drain_index = next(index for index, event in enumerate(events)
                           if event["event"] == "presentation_timing_drain")
        events.insert(drain_index, query)
        _reindex(events)
        report = self.validator.validate(events)
        self.assertTrue(report.is_valid, report.errors)

        events = valid_trace_with_google_timing()
        drain = next(event for event in events if event["event"] == "presentation_timing_drain")
        idle = next(event for event in events if event["event"] == "device_wait_idle")
        drain["event_id"], idle["event_id"] = idle["event_id"], drain["event_id"]
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertTrue(report.errors)

    def test_valid_chain_and_file_order_is_not_authoritative(self):
        events = valid_trace()
        report = self.validator.validate(list(reversed(events)))
        self.assertTrue(report.is_valid, report.errors)
        self.assertTrue(report.quantitative_ready)
        self.assertEqual(report.frame_count, 1)
        self.assertEqual(report.displayed_frame_count, 1)
        self.assertEqual(report.dropped_frame_count, 0)
        self.assertEqual(report.frames[0].drawable_id, 99)
        self.assertGreaterEqual(len(report.join_edges), 7)
        self.assertEqual(report.execution_architecture["compiled_architecture"], "x86_64")
        self.assertEqual(report.swapchain_configuration["actual_image_count"], 3)
        self.assertEqual(report.requested_run_seconds, 30)
        self.assertGreaterEqual(report.actual_run_duration_ns, 30_000_000_000)
        self.assertEqual(report.frame_loop_elapsed_ns, 30_000_000_000)
        self.assertEqual(report.frame_recorded_count, 1)
        self.assertEqual(report.frame_fence_wait_count, 1)

    def test_production_run_end_timestamp_sampling_order_and_minimum_duration(self):
        events = valid_trace()
        report = self.validator.validate(events)
        self.assertTrue(report.is_valid, report.errors)

        events = valid_trace()
        run_end = next(event for event in events if event["event"] == "run_end")
        run_end["ts_mach_ns"] = 29_999_999_998
        run_end["run_end_mach_ns"] = 29_999_999_999
        next(event for event in events if event["event"] == "stop_requested")[
            "ts_mach_ns"] = 29_999_999_940
        next(event for event in events if event["event"] == "device_wait_idle")["ts_mach_ns"] = 29_999_999_950
        next(event for event in events if event["event"] == "presentation_callback_drain")["ts_mach_ns"] = 29_999_999_970
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("before its requested duration", report.errors[0])

    def test_delayed_cleanup_cannot_mask_an_early_frame_loop_exit(self):
        events = valid_trace()
        stop = next(event for event in events if event["event"] == "stop_requested")
        stop["ts_mach_ns"] = 29_000_000_000
        stop["reason"] = "window_closed"
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("frame loop stopped before", report.errors[0])

        events = valid_trace()
        stop = next(event for event in events if event["event"] == "stop_requested")
        stop["reason"] = "window_closed"
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("frame loop stopped before", report.errors[0])

    def test_run_end_common_timestamp_must_precede_explicit_boundary_sample(self):
        events = valid_trace()
        run_end = next(event for event in events if event["event"] == "run_end")
        run_end["run_end_mach_ns"] = run_end["ts_mach_ns"] - 1
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("timestamps are missing or invalid", report.errors[0])

    def test_orphan_frame_record_and_fence_wait_records_are_rejected(self):
        for event in (
                _event(0, "APP_SOURCE", "frame_recorded", 408, 10,
                       app_frame_id=999, frame_id=999, image_index=0,
                       image_id=333, palette_index=0),
                _event(0, "APP_SOURCE", "frame_fence_wait", 408, 10,
                       frame_slot=1, fence_id=56, begin_mach_ns=406,
                       end_mach_ns=407, duration_ns=1, vk_result=0),
                _event(0, "APP_SOURCE", "acquired_image_fence_wait", 408, 10,
                       frame_id=999, image_index=0, image_id=333, fence_id=56,
                       begin_mach_ns=406, end_mach_ns=407,
                       duration_ns=1, vk_result=0)):
            with self.subTest(name=event["event"]):
                events = valid_trace()
                events.insert(next(index for index, row in enumerate(events)
                                   if row["event"] == "presentation_callback_drain"), event)
                _reindex(events)
                report = self.validator.validate(events)
                self.assertFalse(report.is_valid)

    def test_unscoped_extra_drawable_release_is_rejected(self):
        events = valid_trace()
        orphan = copy.deepcopy(next(event for event in events
                                    if event["event"] == "drawable_release"))
        orphan["app_frame_id"] = 0
        orphan["drawable_id"] = 0
        orphan["ts_mach_ns"] = 404
        availability_index = next(index for index, event in enumerate(events)
                                  if event["event"] == "presentation_availability_signal")
        events.insert(availability_index + 1, orphan)
        _reindex(events)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("extra or unscoped reference release", report.errors[0])

    def test_unknown_app_event_and_suboptimal_swapchain_are_rejected(self):
        for name, expected in (("unrecognized_native_event", "unrecognized APP_SOURCE event"),
                               ("swapchain_suboptimal", "baseline-invalid event")):
            with self.subTest(name=name):
                events = valid_trace()
                events.insert(next(index for index, event in enumerate(events)
                                   if event["event"] == "device_wait_idle"),
                              _event(0, "APP_SOURCE", name, 30_000_000_100, 10,
                                     operation="vkQueuePresentKHR"))
                _reindex(events)
                report = self.validator.validate(events)
                self.assertFalse(report.is_valid)
                self.assertIn(expected, report.errors[0])

    def test_auxiliary_fence_wait_interval_integrity_is_enforced(self):
        events = valid_trace()
        wait = next(event for event in events if event["event"] == "frame_fence_wait")
        wait["duration_ns"] = 3
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("measured interval duration", report.errors[0])

    def test_acquired_image_fence_wait_is_joined_by_frame_and_image(self):
        events = valid_trace()
        events.insert(next(index for index, event in enumerate(events)
                          if event["event"] == "frame_recorded"),
                      _event(0, "APP_SOURCE", "acquired_image_fence_wait", 160, 10,
                             frame_id=1, image_index=0, image_id=333, fence_id=56,
                             begin_mach_ns=155, end_mach_ns=159,
                             duration_ns=4, vk_result=0))
        _reindex(events)
        report = self.validator.validate(events)
        self.assertTrue(report.is_valid, report.errors)
        self.assertEqual(report.acquired_image_fence_wait_count, 1)

        bad = copy.deepcopy(events)
        next(event for event in bad if event["event"] == "acquired_image_fence_wait")["image_id"] = 999
        report = self.validator.validate(bad)
        self.assertFalse(report.is_valid)
        self.assertIn("fence wait is invalid or misjoined", report.errors[0])

    def test_google_timing_records_reconcile_without_mixing_clock_domains(self):
        events = valid_trace_with_google_timing()
        report = self.validator.validate(events)
        self.assertTrue(report.is_valid, report.errors)
        self.assertTrue(report.presentation_timing_extension_available)
        self.assertEqual(report.presentation_completion_records, 1)
        self.assertEqual(report.unobserved_presentation_timing_records, 0)

        bad = copy.deepcopy(events)
        next(event for event in bad if event["event"] == "presentation_timing_drain")[
            "unobserved_present_ids"] = 1
        report = self.validator.validate(bad)
        self.assertFalse(report.is_valid)
        self.assertIn("counters do not reconcile", report.errors[0])

    def test_google_timing_orphan_record_is_rejected(self):
        events = valid_trace_with_google_timing()
        completion = next(event for event in events
                          if event["event"] == "presentation_completion_observed")
        completion["present_id"] = 999
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("unknown/duplicate ID", report.errors[0])

    def test_declared_architecture_translation_and_library_path_are_enforced(self):
        validator = TraceValidator(expected_architecture="x86_64",
                                   expected_process_translated=1,
                                   expected_library_path="/tmp/libMoltenVK.dylib")
        self.assertTrue(validator.validate(valid_trace()).is_valid)
        cases = (
            ({"compiled_architecture": "arm64"}, "native architecture mismatch"),
            ({"process_translated": 0}, "native process translation mismatch"),
            ({"moltenvk_loader_path": "/tmp/other.dylib"}, "provenance-bound artifact"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes):
                events = valid_trace()
                architecture = next(event for event in events
                                    if event.get("event") == "execution_architecture")
                architecture.update(changes)
                report = validator.validate(events)
                self.assertFalse(report.is_valid)
                self.assertIn(message, report.errors[0])

    def test_unknown_runtime_architecture_is_rejected(self):
        events = valid_trace()
        architecture = next(event for event in events
                            if event.get("event") == "execution_architecture")
        architecture["compiled_architecture"] = "universal"
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("unknown or malformed", report.errors[0])

    def test_material_layer_configuration_change_is_rejected(self):
        events = valid_trace()
        later = copy.deepcopy(next(event for event in events if event.get("event") == "layer_state"))
        later["event_id"] = max(event["event_id"] for event in events)
        later["ts_mach_ns"] = 408
        later["reason"] = "poll_100ms"
        later["maximum_drawable_count"] = 2
        events.insert(-2, later)
        _reindex(events)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("configuration changed", report.errors[0])

    def test_actual_swapchain_mismatch_is_rejected(self):
        events = valid_trace()
        swapchain = next(event for event in events if event.get("event") == "swapchain_configuration")
        swapchain["actual_image_count"] = 2
        swapchain["image_count_matches_target"] = False
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("differs from requested baseline", report.errors[0])

    def test_process_wide_duplicate_event_id_is_rejected(self):
        events = valid_trace()
        events[6]["event_id"] = events[5]["event_id"]
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("duplicate process-wide event_id", report.errors[0])

    def test_unaccounted_event_id_gap_is_rejected(self):
        events = valid_trace()
        events[5]["event_id"] = 100
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("gaps", report.errors[0])

    def test_logger_overflow_prevents_quantitative_readiness(self):
        events = valid_trace()
        events[-1]["dropped_count"] = 1
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("dropped events", report.errors[0])

    def test_logger_shutdown_rejections_prevent_quantitative_readiness(self):
        events = valid_trace()
        events[-1]["producer_gate_rejected_count"] = 1
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("rejected", report.errors[0])

    def test_shutdown_with_pending_presentation_callback_is_rejected(self):
        events = valid_trace()
        drain = next(event for event in events if event.get("event") == "presentation_callback_drain")
        drain["complete"] = False
        drain["pending_callbacks"] = 1
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("presentation callbacks were still pending", report.errors[0])

    def test_incompatible_presented_time_order_is_rejected(self):
        events = valid_trace(presented_time=500)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("later than callback delivery", report.errors[0])

    def test_duplicate_driver_acquire_join_is_ambiguous(self):
        events = valid_trace()
        duplicate = copy.deepcopy(next(event for event in events
                                       if event["event"] == "vkAcquireNextImageKHR_end"))
        duplicate["ts_mach_ns"] = 134
        duplicate["call_begin_mach_ns"] = 111
        duplicate["call_end_mach_ns"] = 133
        duplicate["duration_ns"] = 22
        app_end_index = next(index for index, event in enumerate(events)
                             if event["event"] == "acquire_end")
        events.insert(app_end_index, duplicate)
        _reindex(events)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("driver acquire API", report.errors[0])

    def test_duplicate_driver_submit_interval_is_ambiguous(self):
        events = valid_trace()
        duplicate_begin = copy.deepcopy(next(event for event in events
                                             if event["event"] == "vkQueueSubmit2_begin"))
        duplicate_begin["driver_submit_seq"] = 8
        duplicate_begin["ts_mach_ns"] = 215
        duplicate_end = copy.deepcopy(next(event for event in events
                                           if event["event"] == "vkQueueSubmit2_end"))
        duplicate_end["driver_submit_seq"] = 8
        duplicate_end["ts_mach_ns"] = 218
        duplicate_end["call_begin_mach_ns"] = 216
        duplicate_end["call_end_mach_ns"] = 217
        duplicate_end["duration_ns"] = 1
        app_end_index = next(index for index, event in enumerate(events)
                             if event["event"] == "app_vkQueueSubmit2_end")
        events[app_end_index:app_end_index] = [duplicate_begin, duplicate_end]
        _reindex(events)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("driver submit interval join", report.errors[0])

    def test_torn_next_drawable_duration_is_rejected(self):
        events = valid_trace()
        next(event for event in events if event["event"] == "next_drawable_end")["duration_ns"] = 50
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("duration", report.errors[0])

    def test_next_drawable_must_carry_its_submit_sequence(self):
        events = valid_trace()
        next(event for event in events if event["event"] == "next_drawable_end")["driver_submit_seq"] = 999
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("current driver submit sequence", report.errors[0])

    def test_missing_texture_identity_is_rejected(self):
        events = valid_trace()
        next(event for event in events if event["event"] == "next_drawable_end").pop("texture_id")
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("texture_id", report.errors[0])

    def test_presented_time_zero_is_validly_classified_as_unpresented(self):
        events = valid_trace(presented_time=0, displayed=False)
        report = self.validator.validate(events)
        self.assertTrue(report.is_valid, report.errors)
        self.assertEqual(report.displayed_frame_count, 0)
        self.assertEqual(report.dropped_frame_count, 1)

    def test_presented_time_clock_domain_mismatch_is_rejected(self):
        events = valid_trace()
        next(event for event in events if event["event"] == "presented_callback")[
            "presentation_clock_domain"] = "VK_GOOGLE_display_timing"
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("clock domain", report.errors[0])

    def test_missing_reference_release_is_rejected(self):
        events = valid_trace()
        events = [event for event in events if event["event"] != "drawable_release"]
        _reindex(events)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("reference release", report.errors[0])

    def test_missing_presentation_availability_signal_is_rejected(self):
        events = valid_trace()
        events = [event for event in events
                  if event["event"] != "presentation_availability_signal"]
        _reindex(events)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("presentation-completion availability signal", report.errors[0])

    def test_same_thread_timestamp_inversion_is_rejected(self):
        events = valid_trace()
        next(event for event in events if event["event"] == "vkAcquireNextImageKHR_begin")[
            "ts_mach_ns"] = 500
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("timestamp regresses", report.errors[0])

    def test_google_present_id_mismatch_is_rejected(self):
        events = valid_trace()
        next(event for event in events if event["event"] == "present_request")["present_id_google"] = 2
        next(event for event in events if event["event"] == "app_vkQueuePresentKHR_end")["present_id_google"] = 1
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("present ID does not match", report.errors[0])

    def test_present_sequence_is_not_conflated_with_google_present_id(self):
        events = valid_trace()
        present_request = next(event for event in events if event["event"] == "present_request")
        present_request["present_id_google"] = 1
        present_request["present_seq"] = 1
        next(event for event in events if event["event"] == "app_vkQueuePresentKHR_end")["present_id_google"] = 1
        next(event for event in events if event["event"] == "presented_callback")["present_id_google"] = 1
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("present_seq does not equal driver_present_seq", report.errors[0])

    def test_unmatched_extra_app_lifecycle_event_is_rejected(self):
        events = valid_trace()
        orphan = copy.deepcopy(next(event for event in events
                                    if event["event"] == "acquire_begin"))
        orphan.update(app_frame_id=999, frame_id=999, app_acquire_sequence=999,
                      ts_mach_ns=99)
        events.insert(next(index for index, event in enumerate(events)
                           if event["event"] == "acquire_begin"), orphan)
        _reindex(events)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("unaccounted lifecycle event APP_SOURCE:acquire_begin", report.errors[0])

    def test_unmatched_extra_moltenvk_lifecycle_event_is_rejected(self):
        events = valid_trace()
        orphan = _event(0, "MVK_SOURCE", "vkQueuePresentKHR_begin", 250, 31,
                        app_frame_id=999, driver_present_seq=999,
                        present_seq=999, queue_id=44)
        events.insert(next(index for index, event in enumerate(events)
                           if event["event"] == "app_vkQueuePresentKHR_begin"), orphan)
        _reindex(events)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("unaccounted lifecycle event MVK_SOURCE:vkQueuePresentKHR_begin",
                      report.errors[0])

    def test_next_drawable_begin_must_precede_its_call_interval(self):
        events = valid_trace()
        end = next(event for event in events if event["event"] == "next_drawable_end")
        end["call_begin_mach_ns"] = 206
        end["duration_ns"] = end["call_end_mach_ns"] - end["call_begin_mach_ns"]
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("invalid call interval/duration", report.errors[0])

    def test_app_and_driver_acquire_results_must_match(self):
        events = valid_trace()
        next(event for event in events if event["event"] == "acquire_end")[
            "vk_result"] = 1000001003
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("acquire join", report.errors[0])

    def test_nonzero_run_end_is_rejected(self):
        events = valid_trace()
        next(event for event in events if event["event"] == "run_end")["exit_code"] = 1
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("run_end reports failure", report.errors[0])

    def test_missing_driver_present_begin_is_rejected(self):
        events = [event for event in valid_trace()
                  if event["event"] != "vkQueuePresentKHR_begin"]
        _reindex(events)
        report = self.validator.validate(events)
        self.assertFalse(report.is_valid)
        self.assertIn("driver present begin pair", report.errors[0])


if __name__ == "__main__":
    unittest.main()
