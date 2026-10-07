#!/usr/bin/env python3
"""Fail-closed offline validator for STEP 11D.3-A native MoltenVK traces.

The JSONL file is an asynchronous drain log. Line order is never treated as
causal order. Joins use propagated frame/acquisition/drawable/present IDs or a
unique same-thread API interval containment check. Ambiguous and incomplete
quantitative chains are rejected.
"""
from __future__ import annotations

import dataclasses
import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


class ValidationError(Exception):
    """Raised when trace identity, clock, lifecycle, or join invariants fail."""


@dataclass
class FrameRecord:
    app_frame_id: int
    app_acquire_sequence: int
    image_index: int
    acquisition_sequence: int
    app_submit_sequence: int
    driver_submit_sequence: int
    app_present_sequence: int
    driver_present_sequence: int
    drawable_id: int
    texture_id: int
    next_drawable_duration_ns: int
    present_callback_delivery_mach_ns: int
    presented_time_mach_ns: int
    is_displayed: bool
    lifecycle_released: bool


@dataclass
class ValidationReport:
    is_valid: bool = False
    quantitative_ready: bool = False
    total_events: int = 0
    app_events_count: int = 0
    moltenvk_events_count: int = 0
    frame_count: int = 0
    complete_frame_count: int = 0
    displayed_frame_count: int = 0
    dropped_frame_count: int = 0
    requested_run_seconds: int = 0
    actual_run_duration_ns: int = 0
    frame_loop_elapsed_ns: int = 0
    frame_recorded_count: int = 0
    frame_fence_wait_count: int = 0
    acquired_image_fence_wait_count: int = 0
    presentation_timing_extension_available: bool = False
    presentation_completion_records: int = 0
    unobserved_presentation_timing_records: int = 0
    buffer_dropped_count: int = 0
    logger_producer_gate_rejected_count: int = 0
    logger_write_failure_count: int = 0
    logger_serialization_failure_count: int = 0
    execution_architecture: dict[str, Any] = field(default_factory=dict)
    configured_layer_state: dict[str, Any] = field(default_factory=dict)
    swapchain_configuration: dict[str, Any] = field(default_factory=dict)
    frames: list[FrameRecord] = field(default_factory=list)
    join_edges: list[dict[str, Any]] = field(default_factory=list)
    unscoped_lifecycle_events: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class TraceValidator:
    """Validate one unified APP_SOURCE/MVK_SOURCE/LOGGER JSONL trace."""

    # The trace is a versioned experiment artifact. Validate JSON value types
    # before any semantic joins so Python's bool-is-an-int behavior cannot turn
    # `true` into a valid Vulkan result, sequence, or handle.
    _BOOLEAN_FIELDS = {
        "allows_next_drawable_timeout", "changed", "complete", "cpu_readback",
        "display_sync_enabled", "extent_matches_target", "framebuffer_only",
        "google_display_timing_available", "has_ext_metal_surface", "has_khr_surface",
        "image_count_matches_target", "is_displayed", "loader_path_resolved",
        "presentation_timing_extension_available", "resizable", "screen_capture",
        "swapchain_transfer_usage", "trace_healthy", "windowed",
    }
    _STRING_FIELDS = {
        "acquisition_trace_correlation", "clock", "compiled_architecture", "detail",
        "device_name", "display_name", "display_uuid", "driver_info", "driver_name",
        "duration_clock", "event", "frames_in_flight_policy", "function", "instance",
        "layer_change_observation", "message", "moltenvk_loader_path", "new_signature",
        "operation", "origin", "pixel_format_name", "present_mode_name",
        "presentation_clock_domain", "reason", "refresh_rate_source", "rendering_method",
        "requested_pixel_format", "requested_present_mode", "required_features",
        "rosetta_query", "step", "submit_api", "surface", "swapchain",
        "timestamp_domain", "trace_clock", "trace_path", "vk_format_name",
        "vk_result_name",
    }
    _RAW_FIELDS = {
        "advertised_present_modes", "advertised_vk_formats", "assigned_layer_properties",
        "available_device_extensions", "available_extensions", "comparison_reference",
        "enabled_instance_extensions", "image_handles", "known_comparison_mismatches",
        "queue", "untouched_layer_properties",
    }
    _NULLABLE_FIELDS = {"frame_id", "image_id", "image_index"}
    _FLOAT_FIELDS = {
        "clear_a", "clear_b", "clear_g", "clear_r", "contents_scale",
        "display_nominal_refresh_hz", "drawable_height", "drawable_width",
        "layer_bounds_height_points", "layer_bounds_width_points",
        "screen_backing_scale", "window_backing_scale",
    }
    _INTEGER_FIELDS = {
        "actual_height", "actual_image_count", "actual_present_time_ns", "actual_width",
        "advertised_format_count", "api_version", "app_acquire_calls",
        "app_acquire_sequence", "app_format_failure_count", "app_frame_id",
        "app_present_seq", "app_submit_seq", "begin_mach_ns", "call_begin_mach_ns",
        "call_end_mach_ns",
        "color_space", "completion_records_observed", "composite_alpha",
        "device_id", "display_id", "display_mode_id", "display_pixel_height",
        "display_pixel_width", "driver_version",
        "current_extent_height", "current_extent_width", "current_transform",
        "duration_ns", "earliest_present_time_ns", "end_mach_ns", "event_id",
        "exit_code", "expected_height", "expected_width", "fence_id",
        "frame_resources", "frame_slot", "frames_in_flight", "image_height",
        "image_usage", "image_width", "max_image_count",
        "maximum_drawable_count", "min_image_count", "observation_mach_ns",
        "palette_index", "pending_callbacks", "pid", "pixel_format", "present_calls",
        "present_id", "present_id_google", "present_ids_submitted", "present_margin_ns",
        "present_mode", "presentation_completion_records", "process_translated",
        "queue_family_index", "queue_id", "queue_index", "record_count",
        "requested_api_version", "requested_color_space", "requested_drawable_height",
        "requested_drawable_height_pixels", "requested_drawable_width",
        "requested_drawable_width_pixels", "requested_image_count", "requested_seconds",
        "requested_swapchain_image_count", "requested_vk_format",
        "pre_transform",
        "requested_window_content_height_points", "requested_window_content_width_points",
        "run_end_mach_ns", "run_start_mach_ns", "screen_maximum_frames_per_second",
        "selected_extent_height", "selected_extent_width", "submitted_frames",
        "successful_present_calls", "supported_usage_flags", "swapchain_id",
        "thread_id", "tid", "ts_mach_ns", "unobserved_present_ids", "vendor_id",
        "vk_format", "vk_result", "acquisition_sequence", "attempt_index",
        "drawable_identity", "drawable_id", "driver_present_seq", "driver_submit_seq",
        "present_seq", "request_id", "texture_id", "presented_time_mach_ns",
        "dropped_count", "producer_gate_rejected_count", "serialization_failure_count",
        "write_failure_count_before_summary", "ring_capacity",
        "logger_drop_count_before_stop", "logger_write_failure_count_before_stop",
    }
    _KNOWN_FIELDS = (_BOOLEAN_FIELDS | _STRING_FIELDS | _RAW_FIELDS | _NULLABLE_FIELDS
                     | _FLOAT_FIELDS | _INTEGER_FIELDS)

    def __init__(self, *, expected_architecture: str | None = None,
                 expected_process_translated: int | None = None,
                 expected_library_path: str | None = None) -> None:
        if expected_architecture not in (None, "arm64", "x86_64"):
            raise ValueError("expected_architecture must be arm64, x86_64, or None")
        if expected_process_translated not in (None, 0, 1):
            raise ValueError("expected_process_translated must be 0, 1, or None")
        self.expected_architecture = expected_architecture
        self.expected_process_translated = expected_process_translated
        self.expected_library_path = expected_library_path

    def parse_lines(self, lines: Iterable[str]) -> list[dict[str, Any]]:
        parsed: list[dict[str, Any]] = []
        for line_no, raw in enumerate(lines, 1):
            if not raw.strip():
                continue
            try:
                record = json.loads(raw)
            except json.JSONDecodeError as error:
                raise ValidationError(f"line {line_no}: malformed JSON: {error}") from error
            if not isinstance(record, dict):
                raise ValidationError(f"line {line_no}: JSON record is not an object")
            record["_line_no"] = line_no
            parsed.append(record)
        return parsed

    def load_file(self, path: Path | str) -> list[dict[str, Any]]:
        source = Path(path)
        if not source.is_file():
            raise ValidationError(f"trace file does not exist: {source}")
        return self.parse_lines(source.read_text(encoding="utf-8").splitlines())

    @staticmethod
    def _integer(event: dict[str, Any], key: str, *, optional: bool = False) -> int | None:
        value = event.get(key)
        if value is None and optional:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(
                f"line {event.get('_line_no', '?')} event {event.get('event')!r}: {key} must be an integer")
        return value

    @classmethod
    def _validate_field_types(cls, event: dict[str, Any], line_no: int) -> None:
        name = event.get("event")
        for key, value in event.items():
            if key == "_line_no":
                continue
            if key not in cls._KNOWN_FIELDS:
                raise ValidationError(f"line {line_no}: unknown trace field {key!r}")
            if key in cls._BOOLEAN_FIELDS:
                if type(value) is not bool:
                    raise ValidationError(f"line {line_no} event {name!r}: {key} must be a boolean")
            elif key in cls._STRING_FIELDS:
                if not isinstance(value, str):
                    raise ValidationError(f"line {line_no} event {name!r}: {key} must be a string")
            elif key in cls._RAW_FIELDS:
                if value is not None and not isinstance(value, (dict, list)):
                    raise ValidationError(f"line {line_no} event {name!r}: {key} must be a raw JSON object or array")
            elif key in cls._NULLABLE_FIELDS and name == "presentation_completion_observed" and value is None:
                continue
            elif key in cls._FLOAT_FIELDS:
                if (isinstance(value, bool) or not isinstance(value, (int, float))
                        or not math.isfinite(value)):
                    raise ValidationError(f"line {line_no} event {name!r}: {key} must be a finite number")
            elif key in cls._INTEGER_FIELDS or key in cls._NULLABLE_FIELDS:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise ValidationError(f"line {line_no} event {name!r}: {key} must be an integer")

    @staticmethod
    def _one(rows: list[dict[str, Any]], description: str) -> dict[str, Any]:
        if len(rows) != 1:
            raise ValidationError(f"{description}: expected exactly one event, found {len(rows)}")
        return rows[0]

    def _require_driver_image_identity(self, event: dict[str, Any], acquired: dict[str, Any],
                                       description: str) -> None:
        """Require a MoltenVK event to retain its exact acquired WSI object identity."""
        for key in ("swapchain_id", "image_id", "image_index", "acquisition_sequence"):
            actual = self._integer(event, key)
            expected = self._integer(acquired, key)
            if actual != expected:
                raise ValidationError(
                    f"{description} event_id={event.get('event_id')}: {key} differs from its acquired image")

    @staticmethod
    def _events(events: list[dict[str, Any]], *, origin: str | None = None,
                name: str | None = None, **fields: Any) -> list[dict[str, Any]]:
        result = []
        for event in events:
            if origin is not None and event.get("origin") != origin:
                continue
            if name is not None and event.get("event") != name:
                continue
            if all(event.get(key) == value for key, value in fields.items()):
                result.append(event)
        return result

    def _validate_drawable_request_ids(self, events: list[dict[str, Any]]) -> None:
        owners: dict[int, int] = {}
        for event in self._events(events, origin="MVK_SOURCE", name="next_drawable_end"):
            request_id = self._integer(event, "request_id")
            app_frame_id = self._integer(event, "app_frame_id")
            if request_id is None or request_id <= 0 or app_frame_id is None or app_frame_id <= 0:
                raise ValidationError("nextDrawable request IDs and app frame IDs must be positive integers")
            owner = owners.setdefault(request_id, app_frame_id)
            if owner != app_frame_id:
                raise ValidationError(
                    f"nextDrawable request_id={request_id} is reused across app frames {owner} and {app_frame_id}")

    def _validate_records(self, events: list[dict[str, Any]], report: ValidationReport) -> None:
        if not events:
            raise ValidationError("trace is empty")
        seen_ids: dict[int, dict[str, Any]] = {}
        summaries: list[dict[str, Any]] = []
        callback_drains: list[dict[str, Any]] = []
        per_thread: dict[int, list[tuple[int, int, int]]] = {}
        for event in events:
            line_no = event.get("_line_no", "?")
            for key in ("origin", "event_id", "event", "ts_mach_ns", "tid"):
                if key not in event:
                    raise ValidationError(f"line {line_no}: required common field {key!r} is missing")
            origin = event["origin"]
            name = event["event"]
            if origin not in {"APP_SOURCE", "MVK_SOURCE", "LOGGER"}:
                raise ValidationError(f"line {line_no}: unsupported origin {origin!r}")
            if not isinstance(name, str) or not name:
                raise ValidationError(f"line {line_no}: event name is empty or invalid")
            self._validate_field_types(event, line_no if isinstance(line_no, int) else 0)
            app_event_names = {
                "acquire_begin", "acquire_end", "actual_swapchain_image_count_mismatch",
                "color_attachment_swapchain_usage_unavailable", "compatible_physical_device_not_found",
                "device_wait_idle", "drawable_extent_mismatch", "execution_architecture",
                "experiment_metadata", "frame_fence_wait", "frame_recorded", "acquired_image_fence_wait",
                "graphics_present_queue_selected", "layer_state", "layer_state_changed",
                "optional_presentation_timing_function_missing", "physical_device_selected",
                "presentation_callback_drain", "presentation_completion_observed",
                "presentation_timing_drain", "presentation_timing_query", "process_start",
                "renderer_resources_created", "renderer_thread_started", "requested_surface_format_unavailable",
                "required_instance_extension_missing", "run_end", "run_start", "startup_error",
                "stop_requested", "surface_capabilities", "swapchain_configuration",
                "swapchain_image_count_unsupported", "swapchain_suboptimal", "trace_sink_stop_request",
                "vulkan_error", "vulkan_instance_surface", "wait_timeout",
                "app_vkQueueSubmit2_begin", "app_vkQueueSubmit2_end",
                "app_vkQueuePresentKHR_begin", "app_vkQueuePresentKHR_end",
            }
            moltenvk_event_names = {
                "acquire_begin", "acquire_end", "acquire_image_assigned", "drawable_release",
                "make_available", "metal_present_request", "next_drawable_begin", "next_drawable_end",
                "present_command_buffer_complete", "present_request", "presentation_availability_signal",
                "presentation_completion_assumed", "presented_callback", "presented_handler_registered",
                "vkAcquireNextImageKHR_begin", "vkAcquireNextImageKHR_end", "vkQueuePresentKHR_begin",
                "vkQueuePresentKHR_end", "vkQueueSubmit_begin", "vkQueueSubmit_end",
                "vkQueueSubmit2_begin", "vkQueueSubmit2_end",
            }
            if ((origin == "APP_SOURCE" and name not in app_event_names)
                    or (origin == "MVK_SOURCE" and name not in moltenvk_event_names)):
                raise ValidationError(f"line {line_no}: unrecognized {origin} event {name!r}")
            event_id = self._integer(event, "event_id")
            ts = self._integer(event, "ts_mach_ns")
            tid = self._integer(event, "tid")
            assert event_id is not None and ts is not None and tid is not None
            if event_id <= 0 or ts <= 0 or tid <= 0:
                raise ValidationError(f"line {line_no}: event_id, ts_mach_ns, and tid must be positive")
            if event_id in seen_ids:
                raise ValidationError(
                    f"duplicate process-wide event_id {event_id} at lines {seen_ids[event_id].get('_line_no')} and {line_no}")
            seen_ids[event_id] = event
            if origin == "LOGGER":
                if name != "trace_dropped_summary":
                    raise ValidationError(f"line {line_no}: unknown LOGGER record {name!r}")
                summaries.append(event)
            elif origin == "APP_SOURCE":
                report.app_events_count += 1
                if name == "presentation_callback_drain":
                    callback_drains.append(event)
            else:
                report.moltenvk_events_count += 1
            per_thread.setdefault(tid, []).append((event_id, ts, line_no if isinstance(line_no, int) else 0))

            for key, value in event.items():
                if key.endswith("_mach_ns") and value is not None:
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ValidationError(f"line {line_no}: {key} must be a non-negative Mach nanosecond integer")
            if "call_begin_mach_ns" in event or "call_end_mach_ns" in event:
                call_begin = self._integer(event, "call_begin_mach_ns")
                call_end = self._integer(event, "call_end_mach_ns")
                duration = self._integer(event, "duration_ns")
                if call_begin <= 0 or call_end < call_begin or call_end > ts:
                    raise ValidationError(
                        f"line {line_no}: API call interval is invalid or ends after its event timestamp")
                if duration != call_end - call_begin:
                    raise ValidationError(f"line {line_no}: API duration does not match its Mach interval")
            if "begin_mach_ns" in event or "end_mach_ns" in event:
                begin = self._integer(event, "begin_mach_ns")
                end = self._integer(event, "end_mach_ns")
                duration = self._integer(event, "duration_ns")
                if begin <= 0 or end < begin or end > ts:
                    raise ValidationError(
                        f"line {line_no}: measured interval is invalid or ends after its event timestamp")
                if duration != end - begin:
                    raise ValidationError(f"line {line_no}: measured interval duration does not match its bounds")
            if "presented_time_mach_ns" in event:
                if event.get("presentation_clock_domain") != "mach_absolute_time_ns":
                    raise ValidationError(f"line {line_no}: presentedTime clock domain is not normalized to Mach absolute nanoseconds")
                if event["presented_time_mach_ns"] == 0 and event.get("is_displayed") is not False:
                    raise ValidationError(f"line {line_no}: presentedTime == 0 must classify as unpresented")
                if event["presented_time_mach_ns"] > 0 and event.get("is_displayed") is not True:
                    raise ValidationError(f"line {line_no}: positive presentedTime must classify as displayed")
                if event["presented_time_mach_ns"] > ts:
                    raise ValidationError(
                        f"line {line_no}: normalized presentedTime is later than callback delivery in the same Mach clock domain")
        if len(summaries) != 1:
            raise ValidationError(f"trace must contain exactly one LOGGER drop summary; found {len(summaries)}")
        if len(callback_drains) != 1:
            raise ValidationError(
                f"trace must contain exactly one presentation callback drain record; found {len(callback_drains)}")
        callback_drain = callback_drains[0]
        if (callback_drain.get("complete") is not True
                or self._integer(callback_drain, "pending_callbacks") != 0):
            raise ValidationError("presentation callbacks were still pending when trace shutdown began")
        architecture = self._one(
            self._events(events, origin="APP_SOURCE", name="execution_architecture"),
            "native execution architecture")
        compiled_arch = architecture.get("compiled_architecture")
        translated = self._integer(architecture, "process_translated")
        library_path = architecture.get("moltenvk_loader_path")
        if compiled_arch not in {"arm64", "x86_64"}:
            raise ValidationError("native execution architecture is unknown or malformed")
        if translated not in {-1, 0, 1}:
            raise ValidationError("sysctl.proc_translated result is malformed")
        if architecture.get("loader_path_resolved") is not True or not isinstance(library_path, str) or not Path(library_path).is_absolute():
            raise ValidationError("MoltenVK loader path was not resolved to an absolute path")
        if self.expected_architecture is not None and compiled_arch != self.expected_architecture:
            raise ValidationError(
                f"native architecture mismatch: expected {self.expected_architecture}, actual {compiled_arch}")
        if (self.expected_process_translated is not None
                and translated != self.expected_process_translated):
            raise ValidationError(
                "native process translation mismatch: "
                f"expected {self.expected_process_translated}, actual {translated}")
        if self.expected_library_path is not None and Path(library_path).resolve() != Path(self.expected_library_path).resolve():
            raise ValidationError("loaded MoltenVK path differs from the provenance-bound artifact")
        report.execution_architecture = {
            "compiled_architecture": compiled_arch,
            "process_translated": translated,
            "moltenvk_loader_path": library_path,
        }

        configured_layer = self._one(
            self._events(events, origin="APP_SOURCE", name="layer_state",
                         reason="startup_after_control_configuration"),
            "post-configuration CAMetalLayer state")
        layer_keys = (
            "maximum_drawable_count", "allows_next_drawable_timeout", "display_sync_enabled",
            "drawable_width", "drawable_height", "contents_scale", "pixel_format",
            "pixel_format_name", "framebuffer_only", "display_id", "display_uuid",
            "display_name", "display_pixel_width", "display_pixel_height",
            "display_mode_id", "display_nominal_refresh_hz",
            "screen_maximum_frames_per_second", "refresh_rate_source",
        )
        missing_layer_keys = [key for key in layer_keys if key not in configured_layer]
        if missing_layer_keys:
            raise ValidationError("CAMetalLayer state is missing fields: " + ", ".join(missing_layer_keys))
        if (configured_layer.get("drawable_width") != 640
                or configured_layer.get("drawable_height") != 360
                or configured_layer.get("pixel_format_name") != "MTLPixelFormatBGRA8Unorm"):
            raise ValidationError("actual CAMetalLayer drawable size or pixel format differs from the target configuration")
        if not isinstance(configured_layer.get("display_name"), str) or not configured_layer.get("display_name"):
            raise ValidationError("CAMetalLayer state does not identify the active display")
        stable_layer_keys = (
            "maximum_drawable_count", "allows_next_drawable_timeout", "display_sync_enabled",
            "drawable_width", "drawable_height", "contents_scale", "pixel_format",
            "pixel_format_name", "framebuffer_only", "display_id", "display_uuid",
            "display_mode_id", "display_nominal_refresh_hz",
            "screen_maximum_frames_per_second",
        )
        for later in self._events(events, origin="APP_SOURCE", name="layer_state"):
            if later is configured_layer or later.get("reason") == "startup_before_control_configuration":
                continue
            changed = [key for key in stable_layer_keys
                       if later.get(key) != configured_layer.get(key)]
            if changed:
                raise ValidationError("CAMetalLayer configuration changed during the run: " + ", ".join(changed))
        report.configured_layer_state = {key: configured_layer[key] for key in layer_keys}

        swapchain = self._one(
            self._events(events, origin="APP_SOURCE", name="swapchain_configuration"),
            "actual Vulkan swapchain configuration")
        swapchain_fields = ("requested_image_count", "actual_image_count", "image_width", "image_height",
                            "vk_format_name", "present_mode_name", "extent_matches_target",
                            "image_count_matches_target")
        missing_swapchain = [key for key in swapchain_fields if key not in swapchain]
        if missing_swapchain:
            raise ValidationError("swapchain configuration is missing fields: " + ", ".join(missing_swapchain))
        if (swapchain.get("requested_image_count") != 3
                or swapchain.get("actual_image_count") != 3
                or swapchain.get("image_width") != 640
                or swapchain.get("image_height") != 360
                or swapchain.get("vk_format_name") != "VK_FORMAT_B8G8R8A8_UNORM"
                or swapchain.get("present_mode_name") != "VK_PRESENT_MODE_FIFO_KHR"
                or swapchain.get("extent_matches_target") is not True
                or swapchain.get("image_count_matches_target") is not True):
            raise ValidationError("actual Vulkan swapchain configuration differs from requested baseline")
        report.swapchain_configuration = {key: swapchain[key] for key in swapchain_fields}
        summary = summaries[0]
        summary_counts = {
            "dropped_count": self._integer(summary, "dropped_count"),
            "producer_gate_rejected_count": self._integer(summary, "producer_gate_rejected_count"),
            "serialization_failure_count": self._integer(summary, "serialization_failure_count"),
            "write_failure_count_before_summary": self._integer(summary, "write_failure_count_before_summary"),
            "ring_capacity": self._integer(summary, "ring_capacity"),
        }
        if any(value is None or value < 0 for value in summary_counts.values()):
            raise ValidationError("LOGGER drop summary is missing a non-negative required counter")
        report.buffer_dropped_count = int(summary_counts["dropped_count"])
        report.logger_producer_gate_rejected_count = int(
            summary_counts["producer_gate_rejected_count"])
        report.logger_serialization_failure_count = int(summary_counts["serialization_failure_count"])
        report.logger_write_failure_count = int(summary_counts["write_failure_count_before_summary"])
        if summary_counts["ring_capacity"] != 32768:
            raise ValidationError("LOGGER ring capacity differs from the attested 32768-slot trace buffer")
        if report.buffer_dropped_count != 0:
            raise ValidationError(f"logger reported {report.buffer_dropped_count} dropped events; trace is not quantitative-ready")
        if report.logger_producer_gate_rejected_count != 0:
            raise ValidationError(
                "logger producer gate rejected "
                f"{report.logger_producer_gate_rejected_count} event admissions during shutdown")
        if report.logger_serialization_failure_count or report.logger_write_failure_count:
            raise ValidationError(
                "logger serialization/write failures were reported: "
                f"serialization={report.logger_serialization_failure_count}, writes={report.logger_write_failure_count}")
        all_ids = sorted(seen_ids)
        if not all_ids or all_ids[0] != 1 or all_ids[-1] != len(all_ids):
            raise ValidationError("process-wide event IDs contain unexplained gaps or do not start at 1")
        if summary["event_id"] != all_ids[-1]:
            raise ValidationError("LOGGER drop summary is not the final process-wide event ID")
        # Event IDs are not assumed to be file order. Within one thread, however,
        # event creation and timestamp capture are sequenced and must not regress.
        for tid, rows in per_thread.items():
            rows.sort(key=lambda row: row[0])
            previous = 0
            for event_id, ts, line_no in rows:
                if ts < previous:
                    raise ValidationError(
                        f"thread {tid}: Mach timestamp regresses in event-id order at line {line_no} ({ts} < {previous})")
                previous = ts
        report.total_events = len(events)

    def _validate_join_edges(self, events: list[dict[str, Any]], report: ValidationReport) -> None:
        run_start = self._one(
            self._events(events, origin="APP_SOURCE", name="run_start"),
            "native control run_start")
        run_end = self._one(
            self._events(events, origin="APP_SOURCE", name="run_end"),
            "native control run_end")
        stop_requested = self._one(
            self._events(events, origin="APP_SOURCE", name="stop_requested"),
            "APP_SOURCE:stop_requested")
        trace_sink_stop = self._one(
            self._events(events, origin="APP_SOURCE", name="trace_sink_stop_request"),
            "APP_SOURCE:trace_sink_stop_request")
        run_start_ns = self._integer(run_start, "run_start_mach_ns")
        run_end_ns = self._integer(run_end, "run_end_mach_ns")
        requested_seconds = self._integer(run_start, "requested_seconds")
        if (run_start_ns is None or run_end_ns is None or requested_seconds is None
                or requested_seconds <= 0 or run_end_ns < run_start_ns
                or run_start["ts_mach_ns"] < run_start_ns
                or run_end["ts_mach_ns"] > run_end_ns
                or run_end_ns <= run_start_ns):
            raise ValidationError("native run start/end Mach timestamps are missing or invalid")
        actual_run_duration_ns = run_end_ns - run_start_ns
        if actual_run_duration_ns < requested_seconds * 1_000_000_000:
            raise ValidationError(
                "native run ended before its requested duration: "
                f"requested={requested_seconds}s actual={actual_run_duration_ns}ns")
        report.requested_run_seconds = requested_seconds
        report.actual_run_duration_ns = actual_run_duration_ns
        if not (run_start["event_id"] < stop_requested["event_id"] < run_end["event_id"]):
            raise ValidationError("stop_requested must be emitted once after run_start and before run_end")
        frame_loop_end_ns = self._integer(stop_requested, "ts_mach_ns")
        expected_frame_deadline_ns = run_start_ns + requested_seconds * 1_000_000_000
        if (stop_requested.get("reason") != "frame_loop_completed"
                or frame_loop_end_ns is None
                or frame_loop_end_ns < expected_frame_deadline_ns):
            raise ValidationError(
                "native frame loop stopped before completing the requested interval or without its completion reason")
        report.frame_loop_elapsed_ns = frame_loop_end_ns - run_start_ns
        if trace_sink_stop["event_id"] <= run_end["event_id"]:
            raise ValidationError("trace_sink_stop_request must follow run_end")
        summaries = self._events(events, origin="LOGGER", name="trace_dropped_summary")
        if len(summaries) != 1 or trace_sink_stop["event_id"] >= summaries[0]["event_id"]:
            raise ValidationError("trace sink stop request must precede the final logger summary")
        if run_end.get("exit_code") != 0 or run_end.get("trace_healthy") is not True:
            raise ValidationError("native control run_end reports failure or an unhealthy trace sink")
        error_events = self._events(events, origin="APP_SOURCE", name="vulkan_error")
        if error_events:
            raise ValidationError(f"native control recorded {len(error_events)} Vulkan error event(s)")
        configuration_failures = {
            "actual_swapchain_image_count_mismatch", "color_attachment_swapchain_usage_unavailable",
            "compatible_physical_device_not_found", "drawable_extent_mismatch",
            "required_instance_extension_missing", "requested_surface_format_unavailable",
            "startup_error", "swapchain_image_count_unsupported", "swapchain_suboptimal", "wait_timeout",
        }
        for name in configuration_failures:
            failures = self._events(events, origin="APP_SOURCE", name=name)
            if failures:
                raise ValidationError(
                    f"native control recorded baseline-invalid event {name!r} ({len(failures)} occurrence(s))")
        if self._events(events, origin="APP_SOURCE", name="layer_state_changed"):
            raise ValidationError("CAMetalLayer reported a state change during the measured run")
        idle_events = self._events(events, origin="APP_SOURCE", name="device_wait_idle")
        self._one(idle_events, "native device idle cleanup")
        if any(self._integer(event, "vk_result") != 0 for event in idle_events):
            raise ValidationError("native device did not reach a successful idle state during cleanup")
        if self._events(events, origin="APP_SOURCE", name="optional_presentation_timing_function_missing"):
            report.warnings.append("VK_GOOGLE_display_timing query is unavailable; Apple presentedTime callbacks remain authoritative")
        if self._events(events, origin="MVK_SOURCE", name="presentation_completion_assumed"):
            raise ValidationError("native macOS trace contains a simulated presentation-completion event")
        if any(self._events(events, origin="MVK_SOURCE", name=name) for name in (
                "vkQueueSubmit_begin", "vkQueueSubmit_end", "acquire_begin", "acquire_end")):
            raise ValidationError("native control used an unmodeled Vulkan submit/acquire entry point")

        renderer_resources = self._one(
            self._events(events, origin="APP_SOURCE", name="renderer_resources_created"),
            "renderer resource configuration")
        frame_resources = self._integer(renderer_resources, "frame_resources")
        if (frame_resources != 3 or renderer_resources.get("cpu_readback") is not False
                or renderer_resources.get("screen_capture") is not False
                or renderer_resources.get("swapchain_transfer_usage") is not False
                or renderer_resources["ts_mach_ns"] >= run_start["ts_mach_ns"]):
            raise ValidationError("renderer resource configuration differs from the no-readback baseline")
        initialization_events = (
            self._one(self._events(events, origin="APP_SOURCE", name="execution_architecture"),
                      "native execution architecture"),
            self._one(self._events(events, origin="APP_SOURCE", name="layer_state",
                                   reason="startup_after_control_configuration"),
                      "configured CAMetalLayer state"),
            self._one(self._events(events, origin="APP_SOURCE", name="swapchain_configuration"),
                      "actual Vulkan swapchain configuration"),
        )
        if any(event["ts_mach_ns"] >= run_start["ts_mach_ns"] for event in initialization_events):
            raise ValidationError("native architecture/display/swapchain configuration was not captured before the run")

        app_frames = sorted({int(event["app_frame_id"]) for event in events
                             if event.get("origin") == "APP_SOURCE"
                             and isinstance(event.get("app_frame_id"), int)
                             and not isinstance(event.get("app_frame_id"), bool)
                             and event.get("event") == "acquire_end"
                             and event.get("vk_result") in (0, 1000001003)})
        if not app_frames:
            raise ValidationError("no successfully acquired app frames were recorded")
        self._validate_drawable_request_ids(events)
        unsuccessful_acquires = [event for event in self._events(
            events, origin="APP_SOURCE", name="acquire_end")
            if event.get("vk_result") not in (0, 1000001003)]
        if unsuccessful_acquires:
            raise ValidationError("native control recorded an unsuccessful image acquisition")

        frame_lifecycle_names = {
            ("APP_SOURCE", name) for name in (
                "acquire_begin", "acquire_end",
                "app_vkQueueSubmit2_begin", "app_vkQueueSubmit2_end",
                "app_vkQueuePresentKHR_begin", "app_vkQueuePresentKHR_end",
                "frame_fence_wait", "frame_recorded", "acquired_image_fence_wait",
                "presentation_completion_observed", "presentation_timing_drain")
        } | {
            ("MVK_SOURCE", name) for name in (
                "vkAcquireNextImageKHR_begin", "vkAcquireNextImageKHR_end",
                "acquire_image_assigned", "vkQueueSubmit2_begin", "vkQueueSubmit2_end",
                "next_drawable_begin", "next_drawable_end",
                "vkQueuePresentKHR_begin", "vkQueuePresentKHR_end", "present_request",
                "metal_present_request", "presented_handler_registered", "presented_callback",
                "present_command_buffer_complete", "drawable_release",
                "make_available", "presentation_availability_signal")
        }
        for event in events:
            if (event.get("origin"), event.get("event")) in frame_lifecycle_names:
                ts = int(event["ts_mach_ns"])
                if ts < int(run_start["ts_mach_ns"]) or ts > int(run_end["ts_mach_ns"]):
                    raise ValidationError(
                        f"frame lifecycle event {event.get('event')!r} is outside the run interval")

        callback_drain = self._one(
            self._events(events, origin="APP_SOURCE", name="presentation_callback_drain"),
            "native presentation callback drain")
        if not (idle_events[0]["event_id"] < callback_drain["event_id"]
                < run_end["event_id"]):
            raise ValidationError("device_wait_idle, callback drain, and run_end are out of shutdown order")

        def check_ordered_interval(start: dict[str, Any], end: dict[str, Any],
                                   description: str) -> tuple[int, int]:
            begin_ns = self._integer(end, "call_begin_mach_ns")
            finish_ns = self._integer(end, "call_end_mach_ns")
            duration_ns = self._integer(end, "duration_ns")
            if (begin_ns is None or finish_ns is None or duration_ns is None
                    or begin_ns < int(start["ts_mach_ns"])
                    or finish_ns < begin_ns
                    or finish_ns > int(end["ts_mach_ns"])
                    or duration_ns != finish_ns - begin_ns):
                raise ValidationError(f"{description}: invalid event/API timestamp ordering")
            return begin_ns, finish_ns

        records: list[FrameRecord] = []
        frame_fence_waits = sorted(
            self._events(events, origin="APP_SOURCE", name="frame_fence_wait"),
            key=lambda event: (int(event["ts_mach_ns"]), int(event["event_id"])))
        if len(frame_fence_waits) != len(app_frames):
            raise ValidationError(
                "frame fence wait count does not reconcile with successfully acquired frames")
        previous_app_present_end: dict[str, Any] | None = None
        for frame_ordinal, frame_id in enumerate(app_frames):
            app_acquire_end = self._one(
                self._events(events, origin="APP_SOURCE", name="acquire_end", app_frame_id=frame_id),
                f"app_frame_id={frame_id} acquire_end")
            app_acquire_start = self._one(
                self._events(events, origin="APP_SOURCE", name="acquire_begin", app_frame_id=frame_id),
                f"app_frame_id={frame_id} acquire_begin")
            app_acquire_seq = self._integer(app_acquire_end, "app_acquire_sequence")
            image_index = self._integer(app_acquire_end, "image_index")
            if app_acquire_seq is None or image_index is None:
                raise ValidationError(f"app_frame_id={frame_id}: successful acquire lacks app acquire sequence/image index")
            if (app_acquire_seq != frame_id or app_acquire_seq != frame_ordinal + 1
                    or frame_id != frame_ordinal + 1):
                raise ValidationError("app frame IDs and acquire sequences are not a unique contiguous chain")
            if (app_acquire_start.get("app_acquire_sequence") != app_acquire_seq
                    or app_acquire_start.get("frame_id") != frame_id
                    or app_acquire_start.get("swapchain_id") != app_acquire_end.get("swapchain_id")):
                raise ValidationError(f"app_frame_id={frame_id}: acquire begin/end identity changed")
            if app_acquire_start.get("tid") != app_acquire_end.get("tid"):
                raise ValidationError(f"app_frame_id={frame_id}: acquire begin/end changed thread")
            acquire_call_begin, acquire_call_end = check_ordered_interval(
                app_acquire_start, app_acquire_end,
                f"app_frame_id={frame_id} application acquire")

            frame_fence_wait = frame_fence_waits[frame_ordinal]
            fence_wait_begin = self._integer(frame_fence_wait, "begin_mach_ns")
            fence_wait_end = self._integer(frame_fence_wait, "end_mach_ns")
            if (frame_fence_wait.get("tid") != app_acquire_start.get("tid")
                    or frame_fence_wait.get("vk_result") != 0
                    or frame_fence_wait.get("frame_slot") != (app_acquire_seq - 1) % frame_resources
                    or frame_fence_wait.get("frame_slot") != app_acquire_start.get("frame_slot")
                    or not self._integer(frame_fence_wait, "fence_id")
                    or fence_wait_begin is None or fence_wait_end is None
                    or fence_wait_begin < (run_start_ns if previous_app_present_end is None
                                           else int(previous_app_present_end["ts_mach_ns"]))
                    or fence_wait_end > frame_fence_wait["ts_mach_ns"]
                    or frame_fence_wait["ts_mach_ns"] >= app_acquire_start["ts_mach_ns"]):
                raise ValidationError(
                    f"app_frame_id={frame_id}: frame fence wait does not join by renderer-loop order and frame slot")
            report.join_edges.append({
                "from": f"renderer loop ordinal={frame_ordinal + 1}, frame_slot={(app_acquire_seq - 1) % frame_resources}",
                "to": f"app_frame_id={frame_id} acquire",
                "method": "deterministic renderer-loop order plus frame-slot formula (sequence minus one modulo frame_resources)",
                "event_ids": [frame_fence_wait["event_id"], app_acquire_start["event_id"]],
            })

            driver_acquire = self._one(
                self._events(events, origin="MVK_SOURCE", name="vkAcquireNextImageKHR_end", app_frame_id=frame_id),
                f"app_frame_id={frame_id} driver acquire API")
            driver_acquire_begin = self._one(
                self._events(events, origin="MVK_SOURCE", name="vkAcquireNextImageKHR_begin", app_frame_id=frame_id,
                             tid=driver_acquire.get("tid")),
                f"app_frame_id={frame_id} driver acquire begin")
            if driver_acquire_begin.get("swapchain_id") != app_acquire_end.get("swapchain_id"):
                raise ValidationError(f"app_frame_id={frame_id}: driver acquire begin changed swapchain identity")
            call_begin = self._integer(driver_acquire, "call_begin_mach_ns")
            call_end = self._integer(driver_acquire, "call_end_mach_ns")
            if (driver_acquire.get("image_index") != image_index
                    or driver_acquire.get("swapchain_id") != app_acquire_end.get("swapchain_id")
                    or call_begin is None or call_end is None
                    or call_begin < app_acquire_start["ts_mach_ns"]
                    or call_end > app_acquire_end["ts_mach_ns"]
                    or driver_acquire["tid"] != app_acquire_end["tid"]
                    or driver_acquire_begin["ts_mach_ns"] > call_begin
                    or driver_acquire["ts_mach_ns"] < call_end
                    or call_begin < acquire_call_begin
                    or call_end > acquire_call_end
                    or driver_acquire.get("vk_result") != app_acquire_end.get("vk_result")
                    or image_index < 0 or image_index >= int(report.swapchain_configuration["actual_image_count"])):
                raise ValidationError(
                    f"app_frame_id={frame_id}: acquire join is not uniquely contained in the same-thread app API interval")
            if driver_acquire.get("image_id") != app_acquire_end.get("image_id"):
                raise ValidationError(
                    f"app_frame_id={frame_id}: application VkImage handle differs from the acquired MoltenVK image")
            acquisition_sequence = self._integer(driver_acquire, "acquisition_sequence")
            if not acquisition_sequence:
                raise ValidationError(f"app_frame_id={frame_id}: driver acquire lacks acquisition_sequence")
            report.join_edges.append({
                "from": f"APP_SOURCE acquire app_frame_id={frame_id}",
                "to": f"MVK_SOURCE acquire acquisition_sequence={acquisition_sequence}",
                "method": "same-thread API interval containment plus swapchain/image index",
                "event_ids": [app_acquire_start["event_id"], driver_acquire_begin["event_id"],
                              driver_acquire["event_id"], app_acquire_end["event_id"]],
            })

            assigned = self._one(
                self._events(events, origin="MVK_SOURCE", name="acquire_image_assigned",
                             app_frame_id=frame_id, acquisition_sequence=acquisition_sequence,
                             image_index=image_index),
                f"app_frame_id={frame_id} image acquisition assignment")
            self._require_driver_image_identity(
                assigned, driver_acquire, f"app_frame_id={frame_id} acquisition assignment")
            if not (call_begin <= int(assigned["ts_mach_ns"]) <= call_end):
                raise ValidationError(f"app_frame_id={frame_id}: image assignment is outside the acquire API interval")
            report.join_edges.append({
                "from": f"acquisition_sequence={acquisition_sequence}",
                "to": f"swapchain image_index={image_index}",
                "method": "explicit propagated acquisition_sequence and image_index",
                "event_ids": [assigned["event_id"], driver_acquire["event_id"]],
            })

            frame_recorded = self._one(
                self._events(events, origin="APP_SOURCE", name="frame_recorded",
                             app_frame_id=frame_id),
                f"app_frame_id={frame_id} command buffer recording")
            if (frame_recorded.get("frame_id") != frame_id
                    or frame_recorded.get("image_index") != image_index
                    or frame_recorded.get("image_id") != app_acquire_end.get("image_id")
                    or frame_recorded.get("tid") != app_acquire_end.get("tid")
                    or frame_recorded["ts_mach_ns"] <= app_acquire_end["ts_mach_ns"]):
                raise ValidationError(f"app_frame_id={frame_id}: recorded command buffer identity/order differs from acquire")
            report.join_edges.append({
                "from": f"app_frame_id={frame_id}, swapchain image_index={image_index}",
                "to": "recorded Vulkan command buffer",
                "method": "explicit propagated app_frame_id, frame_id, image_index, and application VkImage handle",
                "event_ids": [app_acquire_end["event_id"], frame_recorded["event_id"]],
            })

            image_fence_waits = self._events(
                events, origin="APP_SOURCE", name="acquired_image_fence_wait", frame_id=frame_id)
            if len(image_fence_waits) > 1:
                raise ValidationError(f"app_frame_id={frame_id}: duplicate acquired-image fence waits")
            if image_fence_waits:
                image_fence_wait = image_fence_waits[0]
                wait_begin = self._integer(image_fence_wait, "begin_mach_ns")
                wait_end = self._integer(image_fence_wait, "end_mach_ns")
                if (image_fence_wait.get("image_index") != image_index
                        or image_fence_wait.get("image_id") != app_acquire_end.get("image_id")
                        or image_fence_wait.get("vk_result") != 0
                        or not self._integer(image_fence_wait, "fence_id")
                        or image_fence_wait.get("tid") != app_acquire_end.get("tid")
                        or wait_begin is None or wait_end is None
                        or wait_begin < app_acquire_end["ts_mach_ns"]
                        or wait_end > image_fence_wait["ts_mach_ns"]
                        or image_fence_wait["ts_mach_ns"] >= frame_recorded["ts_mach_ns"]):
                    raise ValidationError(f"app_frame_id={frame_id}: acquired-image fence wait is invalid or misjoined")
                report.join_edges.append({
                    "from": f"app_frame_id={frame_id}, image_index={image_index}",
                    "to": "acquired image fence completion",
                    "method": "explicit frame_id, image_index, and application VkImage handle",
                    "event_ids": [app_acquire_end["event_id"], image_fence_wait["event_id"]],
                })

            app_submit_end = self._one(
                self._events(events, origin="APP_SOURCE", name="app_vkQueueSubmit2_end", app_frame_id=frame_id),
                f"app_frame_id={frame_id} app submit end")
            app_submit_begin = self._one(
                self._events(events, origin="APP_SOURCE", name="app_vkQueueSubmit2_begin", app_frame_id=frame_id,
                             app_submit_seq=app_submit_end.get("app_submit_seq")),
                f"app_frame_id={frame_id} app submit begin")
            app_submit_seq = self._integer(app_submit_end, "app_submit_seq")
            if (app_submit_seq is None or app_submit_seq != frame_ordinal + 1
                    or app_submit_end.get("image_index") != image_index
                    or app_submit_end.get("image_id") != app_acquire_end.get("image_id")
                    or app_submit_end.get("swapchain_id") != app_acquire_end.get("swapchain_id")
                    or app_submit_begin.get("image_id") != app_acquire_end.get("image_id")
                    or app_submit_begin.get("swapchain_id") != app_acquire_end.get("swapchain_id")
                    or app_submit_begin.get("queue_id") != app_submit_end.get("queue_id")):
                raise ValidationError(f"app_frame_id={frame_id}: app submit identity does not match acquired image")
            app_submit_call_begin, app_submit_call_end = check_ordered_interval(
                app_submit_begin, app_submit_end,
                f"app_frame_id={frame_id} application submit")
            if frame_recorded["ts_mach_ns"] >= app_submit_begin["ts_mach_ns"]:
                raise ValidationError(f"app_frame_id={frame_id}: command recording was not complete before submit begin")
            driver_submit_candidates = []
            for candidate in self._events(events, origin="MVK_SOURCE", name="vkQueueSubmit2_end", app_frame_id=frame_id):
                begin_ns = self._integer(candidate, "call_begin_mach_ns")
                end_ns = self._integer(candidate, "call_end_mach_ns")
                if (begin_ns is not None and end_ns is not None
                        and begin_ns >= app_submit_begin["ts_mach_ns"]
                        and end_ns <= app_submit_end["ts_mach_ns"]
                        and candidate.get("tid") == app_submit_end.get("tid")
                        and candidate.get("queue_id") == app_submit_end.get("queue_id")):
                    driver_submit_candidates.append(candidate)
            driver_submit = self._one(driver_submit_candidates,
                                      f"app_frame_id={frame_id} driver submit interval join")
            driver_submit_seq = self._integer(driver_submit, "driver_submit_seq")
            if not driver_submit_seq:
                raise ValidationError(f"app_frame_id={frame_id}: driver submit sequence is absent")
            driver_submit_begin = self._one(
                self._events(events, origin="MVK_SOURCE", name="vkQueueSubmit2_begin",
                             driver_submit_seq=driver_submit_seq, app_frame_id=frame_id,
                             tid=driver_submit.get("tid")),
                f"app_frame_id={frame_id} driver submit begin pair")
            driver_submit_call_begin, driver_submit_call_end = check_ordered_interval(
                driver_submit_begin, driver_submit,
                f"app_frame_id={frame_id} driver submit")
            if (driver_submit_call_begin < app_submit_call_begin
                    or driver_submit_call_end > app_submit_call_end
                    or driver_submit_begin["tid"] != app_submit_begin["tid"]
                    or driver_submit["tid"] != app_submit_end["tid"]
                    or driver_submit_begin.get("queue_id") != driver_submit.get("queue_id")
                    or driver_submit.get("queue_id") != app_submit_end.get("queue_id")):
                raise ValidationError(f"app_frame_id={frame_id}: driver submit is outside its application interval")
            if driver_submit.get("vk_result") not in (0, 1000001003):
                raise ValidationError(f"app_frame_id={frame_id}: driver vkQueueSubmit2 returned failure")
            report.join_edges.append({
                "from": f"app_frame_id={frame_id}, app_submit_seq={app_submit_seq}",
                "to": f"driver_submit_seq={driver_submit_seq}",
                "method": "unique same-thread nested API interval and queue identity",
                "event_ids": [app_submit_begin["event_id"], driver_submit_begin["event_id"],
                              driver_submit["event_id"], app_submit_end["event_id"]],
            })

            drawable_ends = []
            for end in self._events(events, origin="MVK_SOURCE", name="next_drawable_end",
                                    app_frame_id=frame_id, acquisition_sequence=acquisition_sequence,
                                    image_index=image_index):
                request_id = self._integer(end, "request_id")
                attempt_index = self._integer(end, "attempt_index")
                drawable_submit_sequence = self._integer(end, "driver_submit_seq")
                if request_id is None or attempt_index is None:
                    raise ValidationError(f"app_frame_id={frame_id}: nextDrawable end lacks request/attempt identity")
                if drawable_submit_sequence != driver_submit_seq:
                    raise ValidationError(
                        f"app_frame_id={frame_id}: nextDrawable request lacks the current driver submit sequence")
                self._require_driver_image_identity(
                    end, driver_acquire, f"app_frame_id={frame_id} nextDrawable end")
                begin = self._one(self._events(events, origin="MVK_SOURCE", name="next_drawable_begin",
                                                app_frame_id=frame_id, acquisition_sequence=acquisition_sequence,
                                                image_index=image_index, request_id=request_id,
                                                attempt_index=attempt_index,
                                                driver_submit_seq=driver_submit_seq),
                                  f"drawable request={request_id} attempt={attempt_index} begin")
                self._require_driver_image_identity(
                    begin, driver_acquire, f"app_frame_id={frame_id} nextDrawable begin")
                duration = self._integer(end, "duration_ns")
                begin_ns = self._integer(end, "call_begin_mach_ns")
                end_ns = self._integer(end, "call_end_mach_ns")
                if (duration is None or begin_ns is None or end_ns is None or duration < 0
                        or end_ns < begin_ns or duration != end_ns - begin_ns
                        or begin["tid"] != end["tid"]
                        or begin["tid"] != driver_submit_begin["tid"]
                        or begin["ts_mach_ns"] > begin_ns
                        or end["ts_mach_ns"] < end_ns
                        or begin_ns < driver_submit_call_begin
                        or end_ns > driver_submit_call_end):
                    raise ValidationError(f"drawable request={request_id} attempt={attempt_index}: invalid call interval/duration")
                drawable_ends.append((end, begin, duration))
            if not drawable_ends:
                raise ValidationError(f"app_frame_id={frame_id}: no nextDrawable attempt joined by explicit acquire/image identity")
            request_ids = {self._integer(end, "request_id") for end, _, _ in drawable_ends}
            attempt_indices = sorted(self._integer(end, "attempt_index") for end, _, _ in drawable_ends)
            if (len(request_ids) != 1 or None in request_ids
                    or next(iter(request_ids)) <= 0
                    or attempt_indices != list(range(len(attempt_indices)))):
                raise ValidationError(
                    f"app_frame_id={frame_id}: nextDrawable request/attempt IDs are duplicated or noncontiguous")
            request_id = next(iter(request_ids))
            successful_drawables = [(end, begin, duration) for end, begin, duration in drawable_ends
                                     if end.get("detail") == "acquired"]
            if len(successful_drawables) != 1:
                raise ValidationError(
                    f"app_frame_id={frame_id}: expected one acquired drawable identity, found {len(successful_drawables)}")
            drawable_end, drawable_begin, drawable_duration = successful_drawables[0]
            drawable_id = self._integer(drawable_end, "drawable_id")
            texture_id = self._integer(drawable_end, "texture_id")
            if not drawable_id or not texture_id:
                raise ValidationError(f"app_frame_id={frame_id}: successful nextDrawable lacks drawable/texture identity")
            for end, begin, duration in drawable_ends:
                report.join_edges.append({
                    "from": f"app_frame_id={frame_id}, acquisition_sequence={acquisition_sequence}, image_index={image_index}",
                    "to": f"driver_submit_seq={driver_submit_seq}, drawable request_id={end['request_id']} attempt_index={end['attempt_index']}",
                    "method": "explicit propagated app/acquisition/image/request/attempt IDs and submit-sequence TLS",
                    "event_ids": [begin["event_id"], end["event_id"]],
                })

            app_present_end = self._one(
                self._events(events, origin="APP_SOURCE", name="app_vkQueuePresentKHR_end", app_frame_id=frame_id),
                f"app_frame_id={frame_id} app present end")
            app_present_seq = self._integer(app_present_end, "app_present_seq")
            app_present_begin = self._one(
                self._events(events, origin="APP_SOURCE", name="app_vkQueuePresentKHR_begin", app_frame_id=frame_id,
                             app_present_seq=app_present_seq),
                f"app_frame_id={frame_id} app present begin")
            if app_present_seq is None:
                raise ValidationError(f"app_frame_id={frame_id}: app present sequence is absent")
            if (app_present_seq != frame_ordinal + 1
                    or app_present_end.get("image_index") != image_index
                    or app_present_end.get("image_id") != app_acquire_end.get("image_id")
                    or app_present_end.get("swapchain_id") != app_acquire_end.get("swapchain_id")
                    or app_present_begin.get("image_id") != app_acquire_end.get("image_id")
                    or app_present_begin.get("swapchain_id") != app_acquire_end.get("swapchain_id")
                    or app_present_begin.get("queue_id") != app_present_end.get("queue_id")
                    or app_present_end.get("queue_id") != app_submit_end.get("queue_id")):
                raise ValidationError(f"app_frame_id={frame_id}: app present identity does not match its acquired image/queue")
            app_present_call_begin, app_present_call_end = check_ordered_interval(
                app_present_begin, app_present_end,
                f"app_frame_id={frame_id} application present")
            if app_submit_end["ts_mach_ns"] >= app_present_begin["ts_mach_ns"]:
                raise ValidationError(f"app_frame_id={frame_id}: present began before application submit returned")
            driver_present_candidates = []
            for candidate in self._events(events, origin="MVK_SOURCE", name="vkQueuePresentKHR_end", app_frame_id=frame_id):
                begin_ns = self._integer(candidate, "call_begin_mach_ns")
                end_ns = self._integer(candidate, "call_end_mach_ns")
                if (begin_ns is not None and end_ns is not None
                        and begin_ns >= app_present_begin["ts_mach_ns"]
                        and end_ns <= app_present_end["ts_mach_ns"]
                        and candidate.get("tid") == app_present_end.get("tid")
                        and candidate.get("queue_id") == app_present_end.get("queue_id")):
                    driver_present_candidates.append(candidate)
            driver_present = self._one(driver_present_candidates,
                                       f"app_frame_id={frame_id} driver present API interval join")
            driver_present_seq = self._integer(driver_present, "driver_present_seq")
            if not driver_present_seq:
                raise ValidationError(f"app_frame_id={frame_id}: driver present sequence is absent")
            driver_present_begin = self._one(
                self._events(events, origin="MVK_SOURCE", name="vkQueuePresentKHR_begin",
                             app_frame_id=frame_id, driver_present_seq=driver_present_seq,
                             tid=driver_present.get("tid")),
                f"app_frame_id={frame_id} driver present begin pair")
            driver_present_call_begin, driver_present_call_end = check_ordered_interval(
                driver_present_begin, driver_present,
                f"app_frame_id={frame_id} driver present")
            if (driver_present_call_begin < app_present_call_begin
                    or driver_present_call_end > app_present_call_end
                    or driver_present_begin["tid"] != app_present_begin["tid"]
                    or driver_present["tid"] != app_present_end["tid"]
                    or driver_present_begin.get("queue_id") != driver_present.get("queue_id")
                    or driver_present.get("queue_id") != app_present_end.get("queue_id")
                    or driver_present.get("vk_result") != app_present_end.get("vk_result")):
                raise ValidationError(f"app_frame_id={frame_id}: driver present is outside its application interval")
            for event_name in ("vkQueuePresentKHR_begin", "present_request", "vkQueuePresentKHR_end",
                               "metal_present_request", "presented_handler_registered",
                               "presented_callback", "present_command_buffer_complete",
                               "drawable_release", "make_available",
                               "presentation_availability_signal"):
                for event in self._events(events, origin="MVK_SOURCE", name=event_name,
                                          app_frame_id=frame_id, driver_present_seq=driver_present_seq):
                    if event.get("present_seq") != driver_present_seq:
                        raise ValidationError(
                            f"app_frame_id={frame_id}: {event_name} present_seq does not equal driver_present_seq")
            if driver_present.get("vk_result") not in (0, 1000001003):
                raise ValidationError(f"app_frame_id={frame_id}: driver vkQueuePresentKHR returned failure")
            present_request = self._one(
                self._events(events, origin="MVK_SOURCE", name="present_request",
                             app_frame_id=frame_id, driver_present_seq=driver_present_seq,
                             acquisition_sequence=acquisition_sequence, image_index=image_index),
                f"app_frame_id={frame_id} Vulkan present request")
            self._require_driver_image_identity(
                present_request, driver_acquire, f"app_frame_id={frame_id} Vulkan present request")
            if (present_request["tid"] != driver_present_begin["tid"]
                    or not (driver_present_begin["ts_mach_ns"]
                            <= present_request["ts_mach_ns"]
                            <= driver_present_call_begin)):
                raise ValidationError(f"app_frame_id={frame_id}: Vulkan present request is outside its causal interval")
            if (app_present_end.get("present_id_google") is not None
                    and present_request.get("present_id_google") != app_present_end.get("present_id_google")):
                raise ValidationError(f"app_frame_id={frame_id}: VK_GOOGLE present ID does not match propagated request")
            report.join_edges.append({
                "from": f"app_frame_id={frame_id}, app_present_seq={app_present_seq}",
                "to": f"driver_present_seq={driver_present_seq}",
                "method": "unique same-thread app/driver API interval containment plus queue identity",
                "event_ids": [app_present_begin["event_id"], driver_present_begin["event_id"],
                              present_request["event_id"],
                              driver_present["event_id"], app_present_end["event_id"]],
            })

            metal_request = self._one(
                self._events(events, origin="MVK_SOURCE", name="metal_present_request",
                             app_frame_id=frame_id, driver_present_seq=driver_present_seq,
                             acquisition_sequence=acquisition_sequence, image_index=image_index,
                             drawable_id=drawable_id),
                f"app_frame_id={frame_id} Metal present request")
            self._require_driver_image_identity(
                metal_request, driver_acquire, f"app_frame_id={frame_id} Metal present request")
            if metal_request.get("texture_id") != texture_id:
                raise ValidationError(f"app_frame_id={frame_id}: drawable texture identity changed before presentation")
            if (metal_request["tid"] != driver_present_begin["tid"]
                    or metal_request["ts_mach_ns"] < driver_present_call_begin
                    or metal_request["ts_mach_ns"] > driver_present_call_end):
                raise ValidationError(f"app_frame_id={frame_id}: Metal present request is outside the driver present interval")
            handler_registered = self._one(
                self._events(events, origin="MVK_SOURCE", name="presented_handler_registered",
                             app_frame_id=frame_id, driver_present_seq=driver_present_seq,
                             acquisition_sequence=acquisition_sequence, image_index=image_index,
                             drawable_id=drawable_id),
                f"app_frame_id={frame_id} presented handler registration attempt")
            self._require_driver_image_identity(
                handler_registered, driver_acquire,
                f"app_frame_id={frame_id} presented handler registration")
            callback = self._one(
                self._events(events, origin="MVK_SOURCE", name="presented_callback",
                             app_frame_id=frame_id, driver_present_seq=driver_present_seq,
                             acquisition_sequence=acquisition_sequence, image_index=image_index,
                             drawable_id=drawable_id),
                f"app_frame_id={frame_id} presented callback")
            self._require_driver_image_identity(
                callback, driver_acquire, f"app_frame_id={frame_id} presented callback")
            presented_time = self._integer(callback, "presented_time_mach_ns")
            if presented_time is None or callback.get("presentation_clock_domain") != "mach_absolute_time_ns":
                raise ValidationError(f"app_frame_id={frame_id}: callback lacks normalized Apple presentedTime")
            is_displayed = callback.get("is_displayed") is True and presented_time > 0
            if (callback.get("texture_id") != texture_id
                    or callback.get("present_id_google") != present_request.get("present_id_google")):
                raise ValidationError(f"app_frame_id={frame_id}: callback identity differs from its present request")
            if callback["ts_mach_ns"] < handler_registered["ts_mach_ns"]:
                raise ValidationError(f"app_frame_id={frame_id}: presented callback preceded handler registration")
            if handler_registered["ts_mach_ns"] < metal_request["ts_mach_ns"]:
                raise ValidationError(f"app_frame_id={frame_id}: presented handler registration preceded Metal present request")
            completion = self._one(
                self._events(events, origin="MVK_SOURCE", name="present_command_buffer_complete",
                             app_frame_id=frame_id, driver_present_seq=driver_present_seq,
                             acquisition_sequence=acquisition_sequence, drawable_id=drawable_id),
                f"app_frame_id={frame_id} command buffer completion")
            self._require_driver_image_identity(
                completion, driver_acquire, f"app_frame_id={frame_id} command buffer completion")
            release = self._one(
                self._events(events, origin="MVK_SOURCE", name="drawable_release",
                             app_frame_id=frame_id, driver_present_seq=driver_present_seq,
                             acquisition_sequence=acquisition_sequence, image_index=image_index,
                             drawable_id=drawable_id, present_seq=driver_present_seq),
                f"app_frame_id={frame_id} drawable reference release")
            self._require_driver_image_identity(
                release, driver_acquire, f"app_frame_id={frame_id} drawable reference release")
            if release.get("texture_id") != texture_id:
                raise ValidationError(f"app_frame_id={frame_id}: released drawable texture identity changed")
            available = self._one(
                self._events(events, origin="MVK_SOURCE", name="presentation_availability_signal",
                             app_frame_id=frame_id, detail="presentation_availability_signal_completed",
                             driver_present_seq=driver_present_seq, drawable_id=drawable_id,
                             acquisition_sequence=acquisition_sequence,
                             image_index=image_index),
                f"app_frame_id={frame_id} presentation-completion availability signal")
            self._require_driver_image_identity(
                available, driver_acquire,
                f"app_frame_id={frame_id} presentation-completion availability signal")
            if available.get("texture_id") != texture_id:
                raise ValidationError(f"app_frame_id={frame_id}: availability signal texture identity changed")
            if available["ts_mach_ns"] < callback["ts_mach_ns"]:
                raise ValidationError(f"app_frame_id={frame_id}: availability signal preceded the presented callback")
            report.join_edges.extend([
                {
                    "from": f"driver_present_seq={driver_present_seq}, drawable_id={drawable_id}",
                    "to": "Metal presented callback / presentedTime",
                    "method": "explicit propagated driver present, acquisition, image, and drawable IDs",
                    "event_ids": [metal_request["event_id"], handler_registered["event_id"],
                                  callback["event_id"]],
                },
                {
                    "from": f"drawable_id={drawable_id}",
                    "to": "presentation completion availability signal and drawable reference release",
                    "method": "explicit propagated app frame, driver present, acquisition, image, and drawable IDs",
                    "event_ids": [completion["event_id"], release["event_id"], available["event_id"]],
                },
            ])
            records.append(FrameRecord(
                app_frame_id=frame_id,
                app_acquire_sequence=app_acquire_seq,
                image_index=image_index,
                acquisition_sequence=acquisition_sequence,
                app_submit_sequence=app_submit_seq,
                driver_submit_sequence=driver_submit_seq,
                app_present_sequence=app_present_seq,
                driver_present_sequence=driver_present_seq,
                drawable_id=drawable_id,
                texture_id=texture_id,
                next_drawable_duration_ns=drawable_duration,
                present_callback_delivery_mach_ns=int(callback["ts_mach_ns"]),
                presented_time_mach_ns=presented_time,
                is_displayed=is_displayed,
                lifecycle_released=True,
            ))
            previous_app_present_end = app_present_end
        report.frames = records
        report.frame_count = len(records)
        report.complete_frame_count = len(records)
        report.displayed_frame_count = sum(frame.is_displayed for frame in records)
        report.dropped_frame_count = len(records) - report.displayed_frame_count
        report.frame_recorded_count = len(self._events(events, origin="APP_SOURCE", name="frame_recorded"))
        report.frame_fence_wait_count = len(frame_fence_waits)
        report.acquired_image_fence_wait_count = len(self._events(
            events, origin="APP_SOURCE", name="acquired_image_fence_wait"))

        unique_sequence_fields = (
            ("app acquire", [frame.app_acquire_sequence for frame in records]),
            ("MoltenVK acquisition", [frame.acquisition_sequence for frame in records]),
            ("app submit", [frame.app_submit_sequence for frame in records]),
            ("MoltenVK submit", [frame.driver_submit_sequence for frame in records]),
            ("app present", [frame.app_present_sequence for frame in records]),
            ("MoltenVK present", [frame.driver_present_sequence for frame in records]),
            ("drawable", [frame.drawable_id for frame in records]),
        )
        for description, values in unique_sequence_fields:
            if any(value <= 0 for value in values) or len(values) != len(set(values)):
                raise ValidationError(f"{description} IDs are not unique positive identities across frames")

        app_acquire_count = len(self._events(events, origin="APP_SOURCE", name="acquire_end"))
        app_submit_count = len(self._events(events, origin="APP_SOURCE", name="app_vkQueueSubmit2_end"))
        app_present_count = len(self._events(events, origin="APP_SOURCE", name="app_vkQueuePresentKHR_end"))
        if (self._integer(run_end, "app_acquire_calls") != app_acquire_count
                or self._integer(run_end, "submitted_frames") != len(records)
                or self._integer(run_end, "present_calls") != app_present_count
                or self._integer(run_end, "successful_present_calls") != len(records)
                or app_acquire_count != len(records) or app_submit_count != len(records)
                or app_present_count != len(records)):
            raise ValidationError("run_end counters do not reconcile with the complete frame chains")

        extension_available = run_end.get("presentation_timing_extension_available")
        completion_count = self._integer(run_end, "presentation_completion_records")
        if not isinstance(extension_available, bool) or completion_count is None or completion_count < 0:
            raise ValidationError("run_end presentation timing availability/counter is malformed")
        completion_events = self._events(
            events, origin="APP_SOURCE", name="presentation_completion_observed")
        timing_drains = self._events(events, origin="APP_SOURCE", name="presentation_timing_drain")
        timing_queries = self._events(events, origin="APP_SOURCE", name="presentation_timing_query")
        if extension_available:
            timing_drain_for_order = self._one(timing_drains, "VK_GOOGLE_display_timing drain")
            if any(query["event_id"] >= timing_drain_for_order["event_id"]
                   for query in timing_queries):
                raise ValidationError("presentation_timing_query must precede presentation_timing_drain")
            if any(completion["event_id"] >= timing_drain_for_order["event_id"]
                   for completion in completion_events):
                raise ValidationError("presentation completion records must precede presentation_timing_drain")
            if timing_drain_for_order["event_id"] >= idle_events[0]["event_id"]:
                raise ValidationError("presentation_timing_drain must precede device_wait_idle")
        report.presentation_timing_extension_available = extension_available
        report.presentation_completion_records = len(completion_events)
        if completion_count != len(completion_events):
            raise ValidationError("run_end presentation completion counter does not match emitted timing records")
        if extension_available:
            timing_drain = self._one(timing_drains, "VK_GOOGLE_display_timing drain")
            expected_present_ids: dict[int, tuple[int, int, int]] = {}
            for frame_id in app_frames:
                present_end = self._one(
                    self._events(events, origin="APP_SOURCE", name="app_vkQueuePresentKHR_end",
                                 app_frame_id=frame_id),
                    f"app_frame_id={frame_id} present end for VK_GOOGLE identity")
                present_id = self._integer(present_end, "present_id_google")
                if present_id is None or present_id <= 0 or present_id in expected_present_ids:
                    raise ValidationError("VK_GOOGLE present IDs are missing or duplicated")
                acquired = self._one(
                    self._events(events, origin="APP_SOURCE", name="acquire_end", app_frame_id=frame_id),
                    f"app_frame_id={frame_id} acquire end for VK_GOOGLE identity")
                expected_present_ids[present_id] = (
                    frame_id, int(acquired["image_index"]), int(acquired["image_id"]))
            observed_ids: set[int] = set()
            for event in completion_events:
                present_id = self._integer(event, "present_id")
                if (present_id is None or present_id not in expected_present_ids
                        or present_id in observed_ids
                        or event.get("timestamp_domain") != "VK_GOOGLE_display_timing nanoseconds"):
                    raise ValidationError("VK_GOOGLE completion record has an unknown/duplicate ID or clock domain")
                for key in ("actual_present_time_ns", "earliest_present_time_ns", "present_margin_ns"):
                    value = self._integer(event, key)
                    if value is None or value < 0:
                        raise ValidationError(f"VK_GOOGLE completion record {key} is invalid")
                observation_mach_ns = self._integer(event, "observation_mach_ns")
                if (observation_mach_ns is None
                        or observation_mach_ns < int(event["ts_mach_ns"])
                        or observation_mach_ns > run_end_ns):
                    raise ValidationError("VK_GOOGLE observation Mach timestamp is outside its event/run interval")
                expected_frame, expected_image_index, expected_image_id = expected_present_ids[present_id]
                if (event.get("frame_id") != expected_frame
                        or event.get("image_index") != expected_image_index
                        or event.get("image_id") != expected_image_id):
                    raise ValidationError("VK_GOOGLE present ID maps to a different app frame or swapchain image")
                observed_ids.add(present_id)
                report.join_edges.append({
                    "from": f"VK_GOOGLE present_id={present_id}",
                    "to": f"app_frame_id={expected_frame}, image_index={expected_image_index}",
                    "method": "explicit VK_GOOGLE present ID; its timestamp fields remain in their declared VK clock domain",
                    "event_ids": [event["event_id"]],
                })
            submitted = self._integer(timing_drain, "present_ids_submitted")
            observed = self._integer(timing_drain, "completion_records_observed")
            unobserved = self._integer(timing_drain, "unobserved_present_ids")
            if (submitted != len(records) or observed != len(completion_events)
                    or unobserved != submitted - observed or unobserved < 0):
                raise ValidationError("presentation_timing_drain counters do not reconcile with frame identities")
            report.unobserved_presentation_timing_records = unobserved
            report.join_edges.append({
                "from": f"VK_GOOGLE submitted={submitted}",
                "to": f"observed={observed}, unobserved={unobserved}",
                "method": "presentation_timing_drain counters reconciled against explicit present IDs",
                "event_ids": [timing_drain["event_id"]],
            })
            for event in timing_queries:
                result = self._integer(event, "vk_result")
                record_count = self._integer(event, "record_count")
                if result in (0, 5) or record_count is None or record_count < 0:
                    raise ValidationError("presentation_timing_query logged a success/incomplete result or malformed count")
                report.warnings.append(
                    f"presentation timing query reported VkResult {result}; Apple Metal callback chains remain authoritative")
        else:
            if timing_drains or completion_events or timing_queries or completion_count != 0:
                raise ValidationError("presentation timing records exist although VK_GOOGLE_display_timing is unavailable")
            report.unobserved_presentation_timing_records = 0

        for event in events:
            if (event.get("origin") in {"APP_SOURCE", "MVK_SOURCE"}
                    and event.get("event") not in {"run_end", "stop_requested", "trace_sink_stop_request"}
                    and int(event["ts_mach_ns"]) > int(run_end["ts_mach_ns"])):
                raise ValidationError(
                    f"application or MoltenVK event {event.get('event')!r} occurred after run_end")

        accounted = {event_id for edge in report.join_edges
                     for event_id in edge.get("event_ids", [])}
        for event in self._events(events, origin="MVK_SOURCE", name="make_available"):
            app_frame_id = self._integer(event, "app_frame_id")
            acquisition = self._integer(event, "acquisition_sequence")
            image_index = self._integer(event, "image_index")
            present_sequence = self._integer(event, "driver_present_seq")
            present_seq = self._integer(event, "present_seq")
            swapchain_id = self._integer(event, "swapchain_id")
            image_id = self._integer(event, "image_id")
            if (app_frame_id <= 0 or acquisition <= 0 or image_index < 0
                    or present_sequence <= 0 or present_seq != present_sequence
                    or swapchain_id <= 0 or image_id <= 0):
                raise ValidationError(
                    f"make_available event_id={event['event_id']} lacks positive frame, acquisition, present, swapchain, or image identity")
            candidates = [frame for frame in records
                         if frame.app_frame_id == app_frame_id
                         and frame.acquisition_sequence == acquisition
                         and frame.image_index == image_index
                         and frame.driver_present_sequence == present_sequence]
            present_requests = self._events(
                events, origin="MVK_SOURCE", name="present_request",
                app_frame_id=app_frame_id, acquisition_sequence=acquisition,
                image_index=image_index, driver_present_seq=present_sequence,
                present_seq=present_sequence, swapchain_id=swapchain_id,
                image_id=image_id)
            if len(candidates) != 1 or len(present_requests) != 1:
                raise ValidationError(f"make_available event_id={event['event_id']} has an ambiguous frame join")
            frame = candidates[0]
            present_request = present_requests[0]
            acquired = self._one(
                self._events(events, origin="MVK_SOURCE", name="vkAcquireNextImageKHR_end",
                             app_frame_id=app_frame_id, acquisition_sequence=acquisition,
                             image_index=image_index),
                f"make_available event_id={event['event_id']} acquired image")
            self._require_driver_image_identity(
                event, acquired, f"make_available event_id={event['event_id']}")
            self._require_driver_image_identity(
                present_request, acquired, f"make_available event_id={event['event_id']} present request")
            report.join_edges.append({
                "from": f"app_frame_id={frame.app_frame_id}, swapchain_id={swapchain_id}, image_id={image_id}, acquisition_sequence={acquisition}, image_index={image_index}, driver_present_seq={present_sequence}",
                "to": f"present_request event_id={present_request['event_id']} -> make_available event_id={event['event_id']}",
                "method": "exact explicit propagated app/acquisition/image/present/swapchain identities",
                "event_ids": [present_request["event_id"], event["event_id"]],
            })
            accounted.add(int(event["event_id"]))

        for event in self._events(events, origin="MVK_SOURCE", name="drawable_release"):
            event_id = int(event["event_id"])
            if event_id in accounted:
                continue
            raise ValidationError(
                f"drawable_release event_id={event_id} is an extra or unscoped reference release")

        # Every WSI lifecycle row must belong to one complete reconstructed frame.
        lifecycle_ids = {int(event["event_id"]) for event in events
                         if (event.get("origin"), event.get("event")) in frame_lifecycle_names}
        unaccounted = sorted(lifecycle_ids - accounted)
        if unaccounted:
            first = next(event for event in events if int(event["event_id"]) == unaccounted[0])
            raise ValidationError(
                f"unaccounted lifecycle event {first.get('origin')}:{first.get('event')} "
                f"event_id={unaccounted[0]}; trace contains an orphan or partial frame record")

    def validate(self, events: list[dict[str, Any]]) -> ValidationReport:
        report = ValidationReport()
        try:
            self._validate_records(events, report)
            self._validate_join_edges(events, report)
            report.is_valid = True
            report.quantitative_ready = report.complete_frame_count > 0 and report.buffer_dropped_count == 0
        except ValidationError as error:
            report.errors.append(str(error))
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("trace", type=Path)
    parser.add_argument("--expected-architecture", choices=("arm64", "x86_64"), required=True)
    parser.add_argument("--expected-process-translated", type=int, choices=(0, 1), required=True)
    parser.add_argument("--expected-library-path", type=Path, required=True)
    args = parser.parse_args()
    validator = TraceValidator(
        expected_architecture=args.expected_architecture,
        expected_process_translated=args.expected_process_translated,
        expected_library_path=str(args.expected_library_path) if args.expected_library_path else None,
    )
    try:
        events = validator.load_file(args.trace)
        # File/drain order is explicitly non-authoritative. The validator keeps
        # identity joins and checks clock/event IDs independently of line order.
        report = validator.validate(events)
    except ValidationError as error:
        print(json.dumps({"is_valid": False, "quantitative_ready": False,
                          "errors": [str(error)]}, indent=2, sort_keys=True))
        raise SystemExit(1)
    print(json.dumps(dataclasses.asdict(report), indent=2, sort_keys=True))
    if not report.is_valid or not report.quantitative_ready:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
