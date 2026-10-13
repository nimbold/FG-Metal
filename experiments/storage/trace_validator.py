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
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping


class ValidationError(Exception):
    """Raised when trace identity, clock, lifecycle, or join invariants fail."""


_EVENTS_INDEX_CACHE: dict[int, tuple[int, dict[Any, list[dict[str, Any]]]]] = {}


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
            elif key in cls._NULLABLE_FIELDS and value is None:
                if name in ("presentation_completion_observed", "acquire_begin"):
                    continue
                raise ValidationError(f"line {line_no} event {name!r}: {key} cannot be null for this event")
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
        cache_key = id(events)
        entry = _EVENTS_INDEX_CACHE.get(cache_key)
        if entry is None or entry[0] != len(events):
            index: dict[Any, list[dict[str, Any]]] = defaultdict(list)
            for e in events:
                orig = e.get("origin")
                ev = e.get("event")
                index[(orig, ev)].append(e)
                index[(orig, None)].append(e)
                index[(None, ev)].append(e)
                index[(None, None)].append(e)
                frame_id = e.get("app_frame_id")
                if isinstance(frame_id, int):
                    index[(orig, ev, "app_frame_id", frame_id)].append(e)
                    index[(orig, None, "app_frame_id", frame_id)].append(e)
                    index[(None, ev, "app_frame_id", frame_id)].append(e)
                    index[(None, None, "app_frame_id", frame_id)].append(e)
            entry = (len(events), index)
            _EVENTS_INDEX_CACHE[cache_key] = entry
        else:
            index = entry[1]

        if "app_frame_id" in fields and isinstance(fields["app_frame_id"], int):
            candidates = index.get((origin, name, "app_frame_id", fields["app_frame_id"]), [])
        else:
            candidates = index.get((origin, name), [])

        if not fields:
            return list(candidates)
        return [e for e in candidates if all(e.get(k) == v for k, v in fields.items())]

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
            if later is configured_layer or later.get("ts_mach_ns", 0) <= configured_layer.get("ts_mach_ns", 0):
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
        records_by_key = {
            (frame.app_frame_id, frame.acquisition_sequence, frame.image_index, frame.driver_present_sequence): frame
            for frame in records
        }
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
            matched_frame = records_by_key.get((app_frame_id, acquisition, image_index, present_sequence))
            candidates = [matched_frame] if matched_frame is not None else []
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
            events_by_id = {int(event["event_id"]): event for event in events if "event_id" in event}
            first = events_by_id.get(unaccounted[0], {})
            raise ValidationError(
                f"unaccounted lifecycle event {first.get('origin')}:{first.get('event')} "
                f"event_id={unaccounted[0]}; trace contains an orphan or partial frame record")

    def validate(self, events: list[dict[str, Any]]) -> ValidationReport:
        _EVENTS_INDEX_CACHE.clear()
        report = ValidationReport()
        try:
            self._validate_records(events, report)
            self._validate_join_edges(events, report)
            report.is_valid = True
            report.quantitative_ready = report.complete_frame_count > 0 and report.buffer_dropped_count == 0
        except ValidationError as error:
            report.errors.append(str(error))
        return report


class BareWineTraceValidator:
    """Strict deterministic validator for STEP 11D.3-B's two-clock traces.

    The Windows app clock and host Mach clock are deliberately not joined by
    timestamp. Calls are paired by their explicit per-function sequence and
    image index; host lifecycle edges use propagated acquisition, submit,
    present, drawable, texture, and native image identities.
    """

    BUCKETS_MS = (
        (0.0, 0.5, "<0.5 ms"), (0.5, 2.0, "0.5–2 ms"),
        (2.0, 5.0, "2–5 ms"), (5.0, 10.0, "5–10 ms"),
        (10.0, 14.0, "10–14 ms"), (14.0, 18.0, "14–18 ms"),
        (18.0, 25.0, "18–25 ms"), (25.0, 50.0, "25–50 ms"),
        (50.0, 100.0, "50–100 ms"), (100.0, 250.0, "100–250 ms"),
        (250.0, 500.0, "250–500 ms"), (500.0, 900.0, "500–900 ms"),
        (900.0, math.inf, ">=900 ms"),
    )

    def __init__(self, *, expected_original_vulkan_path: str,
                 expected_moltenvk_paths: Iterable[str],
                 expected_startup_identities: Mapping[str, Mapping[str, str]],
                 expected_app_path: str, expected_app_sha256: str,
                 expected_moltenvk_version: str,
                 expected_duration_seconds: int | None = None,
                 expected_target_unix_pid: int | None = None,
                 expected_experiment_token: str | None = None) -> None:
        self.expected_original_vulkan_path = str(Path(expected_original_vulkan_path).resolve())
        self.expected_moltenvk_paths = {
            str(Path(path).resolve()) for path in expected_moltenvk_paths
        }
        if not self.expected_moltenvk_paths:
            raise ValueError("at least one exact MoltenVK path is required")
        self.expected_startup_identities = {
            role: {"path": str(Path(identity["path"]).resolve()),
                   "sha256": str(identity["sha256"])}
            for role, identity in expected_startup_identities.items()
        }
        self.expected_moltenvk_version = expected_moltenvk_version
        self.expected_app_path = str(Path(expected_app_path).resolve())
        self.expected_app_sha256 = expected_app_sha256
        required_identity_roles = {
            "wine", "wine_launcher", "winevulkan_dll", "winevulkan_so", "wine_host_loader",
            "winemac_so", "trace_proxy", "moltenvk", "wineserver",
        }
        if set(self.expected_startup_identities) != required_identity_roles:
            raise ValueError("startup identity binding must include every exact BR2 runtime role")
        if any(not re.fullmatch(r"[0-9a-f]{64}", identity["sha256"])
               for identity in self.expected_startup_identities.values()):
            raise ValueError("startup identity binding contains a malformed SHA-256")
        if not re.fullmatch(r"[0-9a-f]{64}", self.expected_app_sha256):
            raise ValueError("expected app SHA-256 is malformed")
        if expected_duration_seconds is not None and expected_duration_seconds <= 0:
            raise ValueError("expected duration must be positive")
        self.expected_duration_seconds = expected_duration_seconds
        if ((expected_target_unix_pid is None) != (expected_experiment_token is None)
                or expected_target_unix_pid is not None and (
                    isinstance(expected_target_unix_pid, bool)
                    or not isinstance(expected_target_unix_pid, int) or expected_target_unix_pid <= 0
                    or not isinstance(expected_experiment_token, str)
                    or not re.fullmatch(r"[0-9a-f]{32}", expected_experiment_token))):
            raise ValueError("process-scoped validation requires exact target PID and experiment token")
        self.expected_target_unix_pid = expected_target_unix_pid
        self.expected_experiment_token = expected_experiment_token

    @staticmethod
    def _rows(events: list[dict[str, Any]], origin: str,
              name: str | None = None) -> list[dict[str, Any]]:
        return [row for row in events if row.get("origin") == origin
                and (name is None or row.get("event") == name)]

    @staticmethod
    def _one(rows: list[dict[str, Any]], what: str) -> dict[str, Any]:
        if len(rows) != 1:
            raise ValidationError(f"{what}: expected one row, found {len(rows)}")
        return rows[0]

    @staticmethod
    def _int(row: dict[str, Any], field: str, *, positive: bool = False) -> int:
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"{row.get('origin')}:{row.get('event')} {field} is not an integer")
        if positive and value <= 0:
            raise ValidationError(f"{row.get('origin')}:{row.get('event')} {field} must be positive")
        return value

    @staticmethod
    def _number(row: dict[str, Any], field: str) -> float:
        value = row.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValidationError(f"{row.get('origin')}:{row.get('event')} {field} is not finite")
        return float(value)

    @classmethod
    def _call_pairs(cls, events: list[dict[str, Any]], origin: str,
                    api_name: str, sequence_field: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
        prefix = f"{api_name}_"
        begins: dict[int, dict[str, Any]] = {}
        ends: dict[int, dict[str, Any]] = {}
        for row in cls._rows(events, origin):
            event_name = row.get("event")
            if event_name not in {prefix + "begin", prefix + "end"}:
                continue
            sequence = cls._int(row, sequence_field, positive=True)
            target = begins if event_name.endswith("_begin") else ends
            if sequence in target:
                raise ValidationError(f"duplicate {origin}:{event_name} sequence {sequence}")
            target[sequence] = row
        if set(begins) != set(ends) or not begins:
            raise ValidationError(
                f"{origin}:{api_name} begin/end sequences differ or are empty: "
                f"begins={len(begins)} ends={len(ends)}")
        sequences = sorted(begins)
        if sequences != list(range(1, len(sequences) + 1)):
            raise ValidationError(f"{origin}:{api_name} sequence has a gap or does not start at one")
        pairs = []
        for sequence in sequences:
            begin, end = begins[sequence], ends[sequence]
            if origin == "APP_SOURCE":
                if (begin.get("app_frame_id") != end.get("app_frame_id")
                        or begin.get("call_begin_qpc_ticks") != end.get("call_begin_qpc_ticks")
                        or end.get("call_end_qpc_ticks", 0) < begin.get("call_begin_qpc_ticks", 0)):
                    raise ValidationError(f"{origin}:{api_name} sequence {sequence} has a mismatched interval")
            else:
                if (begin.get("call_begin_mach_ns") != end.get("call_begin_mach_ns")
                        or end.get("call_end_mach_ns", 0) < begin.get("call_begin_mach_ns", 0)):
                    raise ValidationError(f"{origin}:{api_name} sequence {sequence} has a mismatched interval")
            pairs.append((begin, end))
        return pairs

    @classmethod
    def duration_histogram(cls, durations_ns: list[int]) -> dict[str, Any]:
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0
               for value in durations_ns):
            raise ValidationError("duration series contains a negative or non-integer value")
        rows = []
        for lower, upper, label in cls.BUCKETS_MS:
            values = [value for value in durations_ns
                      if value / 1_000_000 >= lower and value / 1_000_000 < upper]
            rows.append({"bucket": label, "count": len(values),
                         "percentage": (100.0 * len(values) / len(durations_ns)) if durations_ns else 0.0,
                         "accumulated_wait_ns": sum(values)})
        ordered = sorted(durations_ns)

        def percentile(q: float) -> float | None:
            if not ordered:
                return None
            rank = (len(ordered) - 1) * q
            lo = math.floor(rank)
            hi = math.ceil(rank)
            return ordered[lo] + (ordered[hi] - ordered[lo]) * (rank - lo)

        return {
            "count": len(ordered), "histogram": rows,
            "accumulated_wait_ns": sum(ordered),
            "minimum_ns": ordered[0] if ordered else None,
            "mean_ns": (sum(ordered) / len(ordered)) if ordered else None,
            "median_ns": percentile(0.5), "p90_ns": percentile(0.90),
            "p95_ns": percentile(0.95), "p99_ns": percentile(0.99),
            "p99_9_ns": percentile(0.999), "maximum_ns": ordered[-1] if ordered else None,
            "percentile_method": "linear interpolation on sorted samples at (n-1)*q",
        }

    def validate(self, app_events: list[dict[str, Any]], host_events: list[dict[str, Any]],
                 *, loader_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.expected_target_unix_pid is not None:
            if not host_events or any(
                    row.get("host_pid") != self.expected_target_unix_pid
                    or row.get("target_unix_pid") != self.expected_target_unix_pid
                    or row.get("experiment_token") != self.expected_experiment_token
                    or row.get("trace_role") != "target" for row in host_events):
                raise ValidationError("helper or unbound process evidence cannot satisfy target trace validation")
            config = self._one(self._rows(app_events, "APP_CONFIG", "app_configuration"), "target app config")
            if (config.get("target_unix_pid") != self.expected_target_unix_pid
                    or config.get("experiment_token") != self.expected_experiment_token):
                raise ValidationError("PE app does not bind the supervised target process")
        allowed_app_origins = {"APP_CONFIG", "APP_SOURCE"}
        allowed_host_origins = {"INSTRUMENTATION", "WINE_SOURCE", "MVK_BOUNDARY",
                                "LAYER_SOURCE", "LOGGER"}
        if any(row.get("origin") not in allowed_app_origins for row in app_events):
            raise ValidationError("app trace contains an unknown origin")
        if any(row.get("origin") not in allowed_host_origins for row in host_events):
            raise ValidationError("host trace contains an unknown origin")
        if any(row.get("ts_clock_domain") != "windows_qpc_ticks" for row in app_events
               if row.get("origin") == "APP_SOURCE"):
            raise ValidationError("app trace is missing its QueryPerformanceCounter timestamp clock domain")
        if any(row.get("ts_clock_domain") != "mach_absolute_time_ns" for row in host_events):
            raise ValidationError("host trace is missing its Mach absolute timestamp clock domain")

        clock = self._one(self._rows(host_events, "INSTRUMENTATION", "clock_metadata"),
                          "host trace clock metadata")
        if (clock.get("event_clock_source") != "mach_absolute_time"
                or clock.get("event_clock_domain") != "mach_absolute_time_ns"
                or clock.get("event_conversion") != "floor(ticks*numer/denom)"
                or self._int(clock, "mach_timebase_numer", positive=True) <= 0
                or self._int(clock, "mach_timebase_denom", positive=True) <= 0
                or clock.get("presented_time_source") != "CAMetalDrawable.presentedTime"
                or clock.get("presented_time_basis") != "mach_absolute_time_seconds"
                or clock.get("presented_time_clock_domain") != "mach_absolute_time_ns"
                or clock.get("presented_time_conversion") != "floor(seconds*1e9)"
                or clock.get("presented_time_clock_contract")
                    != "Apple Metal host-time seconds; present(at:) is Mach absolute seconds"
                or clock.get("callback_delivery_clock_source") != "mach_absolute_time"
                or clock.get("callback_delivery_clock_domain") != "mach_absolute_time_ns"):
            raise ValidationError("host or presentedTime clock metadata is incomplete or uses an unidentified basis")

        config = self._one(self._rows(app_events, "APP_CONFIG", "app_configuration"),
                           "app configuration")
        if (config.get("timestamp_clock_domain") != "windows_qpc_ticks"
                or config.get("event_timestamp_clock_source") != "QueryPerformanceCounter"
                or config.get("timestamp_conversion") != "floor(ticks*1e9/qpc_frequency)"):
            raise ValidationError("app configuration lacks its QPC clock source, domain, or conversion")
        required_config = {
            "process_architecture": "x86_64", "requested_window_client_width": 640,
            "requested_window_client_height": 360, "actual_window_client_width": 640,
            "actual_window_client_height": 360, "window_mode": "windowed",
            "window_dpi_aware": False, "dpi_x": 96, "dpi_y": 96,
            "window_logical_dpi_scale_x": 1.0, "window_logical_dpi_scale_y": 1.0,
            "display_refresh_hz": 60,
            "requested_format": "VK_FORMAT_B8G8R8A8_UNORM",
            "actual_format": 44, "actual_format_name": "VK_FORMAT_B8G8R8A8_UNORM",
            "requested_present_mode": "VK_PRESENT_MODE_FIFO_KHR", "actual_present_mode": 2,
            "actual_present_mode_name": "VK_PRESENT_MODE_FIFO_KHR",
            "actual_color_space": 0,
            "requested_swapchain_width": 640, "requested_swapchain_height": 360,
            "actual_swapchain_width": 640, "actual_swapchain_height": 360,
            "requested_image_count": 3, "actual_image_count": 3,
            "submit2_entrypoint": "vkQueueSubmit2", "exit_code": 0,
            "stop_reason": "duration_elapsed", "fatal_error": "",
            "window_visible_at_start": True,
            "host_runtime_startup_gate_passed": True,
            "winevulkan_dll_module_loaded": True,
            "winevulkan_dll_before_frame_loop": True,
        }
        mismatches = {key: {"expected": value, "actual": config.get(key)}
                      for key, value in required_config.items() if config.get(key) != value}
        if mismatches:
            raise ValidationError(f"app window/swapchain configuration mismatch: {mismatches}")
        if not isinstance(config.get("display_name"), str) or not config["display_name"].strip():
            raise ValidationError("app trace does not identify the display containing its window")
        app_winevulkan_path = config.get("winevulkan_dll_module_path")
        if (not isinstance(app_winevulkan_path, str)
                or app_winevulkan_path.replace("\\", "/").casefold()
                    != "c:/windows/system32/winevulkan.dll"):
            raise ValidationError("pre-frame app identity does not name the loaded builtin winevulkan.dll")
        expected_dll_identity = self.expected_startup_identities["winevulkan_dll"]
        app_winevulkan_host_path = config.get("winevulkan_dll_host_path")
        app_winevulkan_sha256 = config.get("winevulkan_dll_sha256")
        if (not isinstance(app_winevulkan_host_path, str)
                or str(Path(app_winevulkan_host_path).resolve()) != expected_dll_identity["path"]
                or app_winevulkan_sha256 != expected_dll_identity["sha256"]):
            raise ValidationError(
                "pre-frame app HMODULE path is not bound to the exact PrefixLease winevulkan.dll path and SHA-256")
        run_ns = self._int(config, "actual_run_ns", positive=True)
        qpc_frequency = self._int(config, "qpc_frequency", positive=True)
        for row in self._rows(app_events, "APP_SOURCE"):
            if (row.get("timestamp_clock_source") != "QueryPerformanceCounter"
                    or row.get("ts_clock_domain") != "windows_qpc_ticks"
                    or self._int(row, "qpc_frequency", positive=True) != qpc_frequency):
                raise ValidationError("app event clock source, domain, or QPC frequency differs from its config")
            timestamp_ticks = self._int(row, "ts_qpc_ticks", positive=True)
            if self._int(row, "ts_qpc_ns", positive=True) != timestamp_ticks * 1_000_000_000 // qpc_frequency:
                raise ValidationError("app event QPC timestamp conversion is inconsistent with its frequency")
            for tick_field, ns_field in (
                    ("call_begin_qpc_ticks", "call_begin_qpc_ns"),
                    ("call_end_qpc_ticks", "call_end_qpc_ns"),
                    ("duration_qpc_ticks", "duration_ns")):
                if row.get(tick_field) is not None:
                    ticks = self._int(row, tick_field)
                    expected_ns = ticks * 1_000_000_000 // qpc_frequency
                    if self._int(row, ns_field) != expected_ns:
                        raise ValidationError(f"app event {tick_field} conversion is inconsistent with QPC frequency")
        requested_seconds = self._int(config, "run_seconds_requested", positive=True)
        if self.expected_duration_seconds is not None and requested_seconds != self.expected_duration_seconds:
            raise ValidationError(
                f"app requested {requested_seconds}s but this run requires {self.expected_duration_seconds}s")
        if run_ns < requested_seconds * 950_000_000 or run_ns > (requested_seconds + 2) * 1_000_000_000:
            raise ValidationError("app frame-loop duration differs materially from its requested duration")
        if self._int(config, "trace_events_dropped") != 0:
            raise ValidationError("app trace dropped one or more events")

        process_identity = self._one(
            self._rows(host_events, "INSTRUMENTATION", "process_identity"), "host process identity")
        if (process_identity.get("compiled_architecture") != "x86_64"
                or process_identity.get("process_architecture") != "x86_64"
                or self._int(process_identity, "process_translated") != 1
                or process_identity.get("process_start_clock_domain") != "mach_absolute_time_ns"
                or self._int(process_identity, "process_start_monotonic_ns", positive=True) <= 0):
            raise ValidationError("host trace process is not x86_64 under Rosetta")
        expected_wine = self.expected_startup_identities["wine"]
        expected_wine_launcher = self.expected_startup_identities["wine_launcher"]
        if (str(Path(str(process_identity.get("main_executable_path", ""))).resolve())
                != expected_wine["path"]
                or process_identity.get("main_executable_sha256") != expected_wine["sha256"]
                or str(Path(str(process_identity.get("wine_launcher_path", ""))).resolve())
                    != expected_wine_launcher["path"]
                or process_identity.get("wine_launcher_sha256") != expected_wine_launcher["sha256"]):
            raise ValidationError("in-process Wine executable path or SHA-256 differs from the bound runtime")
        proxy_identity = self._one(
            self._rows(host_events, "INSTRUMENTATION", "vulkan_proxy_identity"), "Vulkan proxy identity")
        if (proxy_identity.get("real_gipa_available") is not True
                or proxy_identity.get("metal_surface_dlsym_available") is not True
                or str(Path(str(proxy_identity.get("original_vulkan_library", ""))).resolve())
                    != self.expected_original_vulkan_path
                or proxy_identity.get("original_vulkan_library_sha256")
                    != self.expected_startup_identities["moltenvk"]["sha256"]
                or str(Path(str(proxy_identity.get("trace_proxy_path", ""))).resolve())
                    != self.expected_startup_identities["trace_proxy"]["path"]
                or proxy_identity.get("trace_proxy_sha256")
                    != self.expected_startup_identities["trace_proxy"]["sha256"]
                or str(Path(str(proxy_identity.get("loaded_proxy_path", ""))).resolve())
                    != self.expected_startup_identities["trace_proxy"]["path"]
                or proxy_identity.get("loaded_proxy_sha256")
                    != self.expected_startup_identities["trace_proxy"]["sha256"]
                or str(Path(str(proxy_identity.get("loaded_delegate_path", ""))).resolve())
                    != self.expected_startup_identities["moltenvk"]["path"]
                or proxy_identity.get("loaded_delegate_sha256")
                    != self.expected_startup_identities["moltenvk"]["sha256"]
                or proxy_identity.get("process_start_clock_domain") != "mach_absolute_time_ns"
                or self._int(proxy_identity, "process_start_monotonic_ns", positive=True)
                    != self._int(process_identity, "process_start_monotonic_ns", positive=True)):
            raise ValidationError("proxy did not delegate to the exact pinned Wine Vulkan library")
        startup = self._one(
            self._rows(host_events, "INSTRUMENTATION", "host_runtime_startup"),
            "in-process host runtime startup identity")
        startup_id = startup.get("startup_id")
        if (startup.get("startup_valid") is not True
                or startup_id != process_identity.get("startup_id")
                or startup_id != proxy_identity.get("startup_id")
                or startup.get("process_architecture") != "x86_64"
                or self._int(startup, "process_translated") != 1
                or startup.get("process_start_clock_domain") != "mach_absolute_time_ns"
                or self._int(startup, "process_start_monotonic_ns", positive=True)
                    != self._int(process_identity, "process_start_monotonic_ns", positive=True)
                or startup.get("moltenvk_version") != self.expected_moltenvk_version
                or startup.get("moltenvk_version_source") != "vkGetVersionStringsMVK from loaded delegate"
                or startup.get("vulkan_runtime_version") != "1.4.357"
                or startup.get("moltenvk_sha256") != self.expected_startup_identities["moltenvk"]["sha256"]
                or str(Path(str(startup.get("moltenvk_path", ""))).resolve())
                    != self.expected_startup_identities["moltenvk"]["path"]
                or startup.get("trace_proxy_sha256") != self.expected_startup_identities["trace_proxy"]["sha256"]
                or str(Path(str(startup.get("trace_proxy_path", ""))).resolve())
                    != self.expected_startup_identities["trace_proxy"]["path"]
                or startup.get("winevulkan_so_sha256") != self.expected_startup_identities["winevulkan_so"]["sha256"]
                or str(Path(str(startup.get("winevulkan_so_path", ""))).resolve())
                    != self.expected_startup_identities["winevulkan_so"]["path"]
                or startup.get("wine_host_loader_sha256") != self.expected_startup_identities["wine_host_loader"]["sha256"]
                or str(Path(str(startup.get("wine_host_loader_path", ""))).resolve())
                    != self.expected_startup_identities["wine_host_loader"]["path"]
                or startup.get("host_loader_role")
                    != "win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib; no Khronos libvulkan"
                or self._int(startup, "loaded_moltenvk_count") != 1
                or self._int(startup, "loaded_proxy_count") != 1
                or self._int(startup, "loaded_winevulkan_count") != 1
                or self._int(startup, "loaded_host_loader_count") != 1
                or self._int(startup, "loaded_libvulkan_dylib_count") != 0):
            raise ValidationError("in-process runtime startup identity block is absent, stale, duplicated, or incorrect")
        startup_modules = self._rows(host_events, "INSTRUMENTATION", "startup_module_identity")
        startup_by_role: dict[str, dict[str, Any]] = {}
        for row in startup_modules:
            if row.get("startup_phase") is not True or row.get("startup_id") != startup_id:
                raise ValidationError("startup module identity is outside the authenticated startup block")
            role = row.get("role")
            if role in startup_by_role:
                raise ValidationError(f"duplicate startup module identity role: {role}")
            startup_by_role[str(role)] = row
        expected_module_roles = {"wine", "wine_launcher", "winevulkan_dll", "winevulkan_so",
                                 "wine_host_loader", "winemac_so", "trace_proxy", "moltenvk",
                                 "wineserver"}
        if set(startup_by_role) != expected_module_roles:
            raise ValidationError("startup module identity block is missing or duplicates a required runtime module")
        for role, row in startup_by_role.items():
            expected = self.expected_startup_identities[role]
            if (str(Path(str(row.get("path", ""))).resolve()) != expected["path"]
                    or row.get("sha256") != expected["sha256"]
                    or str(Path(str(row.get("observed_path", ""))).resolve()) != expected["path"]
                    or row.get("observed_sha256") != expected["sha256"]
                    or row.get("identity_match") is not True):
                raise ValidationError(f"startup {role} path or SHA-256 differs from the bound runtime")
            expected_loaded = role not in {"wineserver", "wine_launcher"}
            expected_kind = (
                "prefixlease-bound-executable" if role == "wineserver" else
                "prefixlease-spawn-executable" if role == "wine_launcher" else
                "win32-hmodule-proof-in-paired-app-trace" if role == "winevulkan_dll" else
                "dyld-main-executable" if role == "wine" else
                "dyld-loaded-image")
            if (row.get("loaded_in_process") is not expected_loaded
                    or row.get("identity_kind") != expected_kind):
                raise ValidationError(f"startup {role} load state or identity kind is ambiguous")
        runtime_identities = loader_evidence.get("runtime_identities") if loader_evidence else None
        if not isinstance(runtime_identities, dict):
            raise ValidationError("loader evidence lacks runtime module identities")
        loader_dll_path = loader_evidence.get("winevulkan_dll_reported_path")
        if (not isinstance(loader_dll_path, str)
                or loader_dll_path.replace("\\", "/").casefold()
                    != app_winevulkan_path.replace("\\", "/").casefold()):
            raise ValidationError("pre-frame loaded winevulkan.dll path differs from the Wine loader record")
        if set(runtime_identities) != set(self.expected_startup_identities) | {"app"}:
            raise ValidationError("loader evidence has missing or duplicate runtime identity roles")
        for role, expected in self.expected_startup_identities.items():
            runtime = runtime_identities.get(role)
            if not isinstance(runtime, dict):
                raise ValidationError(f"runtime identity evidence is missing {role}")
            if (str(Path(str(runtime.get("path", ""))).resolve()) != expected["path"]
                    or runtime.get("sha256") != expected["sha256"]):
                raise ValidationError(f"runtime identity evidence differs from the bound {role} binary")
        app_runtime = runtime_identities["app"]
        if (str(Path(str(app_runtime.get("path", ""))).resolve()) != self.expected_app_path
                or app_runtime.get("sha256") != self.expected_app_sha256):
            raise ValidationError("runtime evidence differs from the bound native app path or SHA-256")
        if (runtime_identities["winevulkan_dll"].get("load_kind") != "builtin"
                or runtime_identities["moltenvk"].get("version") != self.expected_moltenvk_version
                or loader_evidence.get("khronos_loader_loaded") is not False
                or loader_evidence.get("moltenvk_version") != self.expected_moltenvk_version
                or not loader_evidence.get("moltenvk_runtime_banners")
                or any(row.get("moltenvk_version") != "1.4.2"
                       or row.get("vulkan_version") != "1.4.357"
                       for row in loader_evidence["moltenvk_runtime_banners"])):
            raise ValidationError("runtime loader path, MoltenVK version, or direct-loader selection is incorrect")
        layer = self._one(self._rows(host_events, "LAYER_SOURCE", "layer_configuration"),
                          "WineMetalLayer configuration")
        if (layer.get("layer_class") != "WineMetalLayer"
                or layer.get("layer_is_cametal_layer") is not True
                or str(Path(str(layer.get("layer_class_image_path", ""))).resolve())
                    != self.expected_startup_identities["winemac_so"]["path"]
                or layer.get("layer_class_image_sha256")
                    != self.expected_startup_identities["winemac_so"]["sha256"]):
            raise ValidationError(f"observed layer is not WineMetalLayer: {layer.get('layer_class')}")
        layer_id = self._int(layer, "layer_id", positive=True)
        identity_layer = layer
        presentation_layers = self._rows(host_events, "LAYER_SOURCE", "layer_presentation_configuration")
        if presentation_layers:
            configured = self._one(presentation_layers, "WineMetalLayer presentation configuration")
            for identity_key in ("layer_id", "layer_class", "layer_is_cametal_layer",
                                 "layer_class_image_path", "layer_class_image_sha256"):
                if configured.get(identity_key) != layer.get(identity_key):
                    raise ValidationError("early and presentation layer identities differ")
            if layer.get("observation_stage") != "surface_identity_before_swapchain":
                raise ValidationError("split layer observation lacks an explicit early identity stage")
            layer = configured
        elif layer.get("observation_stage") == "surface_identity_before_swapchain":
            raise ValidationError("split layer observation requires presentation configuration")
        for key in ("maximum_drawable_count", "pixel_format"):
            self._int(layer, key)
        if self._int(layer, "maximum_drawable_count") != 3:
            raise ValidationError("WineMetalLayer maximumDrawableCount differs from the requested three images")
        for key in ("drawable_width", "drawable_height", "contents_scale",
                    "layer_bounds_width_points", "layer_bounds_height_points"):
            if self._number(layer, key) <= 0:
                raise ValidationError(f"WineMetalLayer {key} must be positive")
        for key in ("allows_next_drawable_timeout", "display_sync_enabled", "framebuffer_only"):
            if type(layer.get(key)) is not bool:
                raise ValidationError(f"WineMetalLayer {key} is not a boolean")
        if (layer.get("drawable_width") != 640 or layer.get("drawable_height") != 360):
            raise ValidationError("WineMetalLayer drawable size differs from the fixed 640x360 experiment")

        trace_summary = self._one(self._rows(host_events, "LOGGER", "trace_summary"),
                                  "host trace summary")
        if (self._int(trace_summary, "dropped_count") != 0
                or self._int(trace_summary, "write_failure_count") != 0
                or self._int(trace_summary, "ring_capacity") != 8192):
            raise ValidationError("host asynchronous trace ring reported drops, write failures, or a changed capacity")
        frame_start_events = [row for row in host_events
                              if row.get("event") in {
                                  "vkAcquireNextImageKHR_begin", "vkQueueSubmit2_begin",
                                  "vkQueuePresentKHR_begin", "next_drawable_begin",
                              } and row.get("origin") in {"WINE_SOURCE", "MVK_BOUNDARY", "LAYER_SOURCE"}]
        if not frame_start_events:
            raise ValidationError("host trace contains no identified Vulkan frame events")
        first_frame_event_id = min(self._int(row, "event_id", positive=True)
                                   for row in frame_start_events)
        startup_block = [clock, process_identity, proxy_identity, startup,
                         *startup_modules, identity_layer]
        if any(self._int(row, "event_id", positive=True) >= first_frame_event_id
               for row in startup_block):
            raise ValidationError("runtime, clock, or WineMetalLayer startup identity arrived after frame events")
        dispatch = self._rows(host_events, "INSTRUMENTATION", "native_dispatch_source")
        if len(dispatch) != 4:
            raise ValidationError(f"expected four native Vulkan dispatch identities, found {len(dispatch)}")
        dispatch_paths = []
        for row in dispatch:
            if row.get("resolved") is not True:
                raise ValidationError("one or more native Vulkan entry points lack a dladdr identity")
            path = str(Path(str(row.get("library_path", ""))).resolve())
            if path not in self.expected_moltenvk_paths | {self.expected_original_vulkan_path}:
                raise ValidationError(f"Vulkan dispatch resolved through an unexpected library: {path}")
            dispatch_paths.append({"function": row.get("function"), "library_path": path,
                                   "symbol_name": row.get("symbol_name")})

        app_acquire = self._call_pairs(app_events, "APP_SOURCE", "app_vkAcquireNextImageKHR",
                                       "app_acquire_seq")
        app_submit = self._call_pairs(app_events, "APP_SOURCE", "app_vkQueueSubmit2", "app_submit_seq")
        app_present = self._call_pairs(app_events, "APP_SOURCE", "app_vkQueuePresentKHR", "app_present_seq")
        host_acquire = self._call_pairs(host_events, "WINE_SOURCE", "vkAcquireNextImageKHR", "wine_call_seq")
        host_submit = self._call_pairs(host_events, "WINE_SOURCE", "vkQueueSubmit2", "wine_call_seq")
        host_present = self._call_pairs(host_events, "WINE_SOURCE", "vkQueuePresentKHR", "wine_call_seq")
        mvk_acquire = self._call_pairs(host_events, "MVK_BOUNDARY", "vkAcquireNextImageKHR", "wine_call_seq")
        mvk_submit = self._call_pairs(host_events, "MVK_BOUNDARY", "vkQueueSubmit2", "wine_call_seq")
        mvk_present = self._call_pairs(host_events, "MVK_BOUNDARY", "vkQueuePresentKHR", "wine_call_seq")
        if not (len(app_acquire) == len(app_submit) == len(app_present)
                == len(host_acquire) == len(host_submit) == len(host_present)
                == len(mvk_acquire) == len(mvk_submit) == len(mvk_present)):
            raise ValidationError("app and winevulkan/MoltenVK API counts differ; ordinal joins are ambiguous")
        count = len(app_acquire)
        if count != self._int(config, "app_frames_started", positive=True):
            raise ValidationError("app frame count does not equal successful acquire call count")
        if any(self._int(config, field, positive=True) != count for field in
               ("app_submits", "app_present_requests")):
            raise ValidationError("app acquire/submit/present totals differ")

        surface_calls = self._call_pairs(
            host_events, "MVK_BOUNDARY", "vkCreateMetalSurfaceEXT", "wine_call_seq")
        fallback_surface_rows = self._rows(
            host_events, "MVK_BOUNDARY", "vkCreateMacOSSurfaceMVK_begin")
        fallback_surface_calls = self._call_pairs(
            host_events, "MVK_BOUNDARY", "vkCreateMacOSSurfaceMVK", "wine_call_seq") \
            if fallback_surface_rows else []
        swapchain_calls = self._call_pairs(
            host_events, "MVK_BOUNDARY", "vkCreateSwapchainKHR", "wine_call_seq")
        if len(surface_calls) != 1 or fallback_surface_calls or len(swapchain_calls) != 1:
            raise ValidationError(
                "the pinned Wine path must create exactly one traced VK_EXT_metal_surface and swapchain")
        surface_begin, surface_end = surface_calls[0]
        swapchain_begin, swapchain_end = swapchain_calls[0]
        if self._int(identity_layer, "event_id", positive=True) >= self._int(swapchain_begin, "event_id", positive=True):
            raise ValidationError("WineMetalLayer identity must precede swapchain creation")
        native_surface = self._int(surface_end, "surface_id", positive=True)
        surface_layer = self._int(surface_end, "layer_id", positive=True)
        if (self._int(surface_end, "vk_result") != 0
                or self._int(surface_begin, "layer_id", positive=True) != surface_layer):
            raise ValidationError("Wine vkCreateMetalSurfaceEXT did not succeed with one layer identity")
        if surface_layer != layer_id:
            raise ValidationError("WineMetalLayer nextDrawable identity differs from the surface pLayer")
        if (self._int(swapchain_end, "surface_id", positive=True) != native_surface
                or self._int(swapchain_begin, "surface_id", positive=True) != native_surface
                or self._int(swapchain_end, "vk_result") != 0):
            raise ValidationError("Wine swapchain does not belong to the unique created surface")
        if (self._int(swapchain_end, "requested_image_count") != 3
                or self._int(swapchain_end, "format") != 44
                or self._int(swapchain_end, "width") != 640
                or self._int(swapchain_end, "height") != 360
                or self._int(swapchain_end, "present_mode") != 2):
            raise ValidationError("native swapchain creation request differs from A's target configuration")
        native_swapchain_from_create = self._int(swapchain_end, "swapchain_id", positive=True)

        native_images: dict[tuple[int, int], int] = {}
        image_rows = self._rows(host_events, "MVK_BOUNDARY", "swapchain_image_identity")
        for row in image_rows:
            key = (self._int(row, "swapchain_id", positive=True), self._int(row, "image_index"))
            image_id = self._int(row, "image_id", positive=True)
            if self._int(row, "surface_id", positive=True) != native_surface:
                raise ValidationError("native swapchain image identity lost its surface identity")
            if key in native_images:
                raise ValidationError(f"duplicate native swapchain image identity: {key}")
            native_images[key] = image_id
        swapchain_ids = {key[0] for key in native_images}
        if (len(swapchain_ids) != 1 or len(native_images) != 3
                or {key[1] for key in native_images} != {0, 1, 2}):
            raise ValidationError("native MoltenVK trace does not identify exactly three swapchain images")
        native_swapchain = next(iter(swapchain_ids))
        if native_swapchain != native_swapchain_from_create:
            raise ValidationError("swapchain creation and acquired-image records identify different swapchains")

        next_begin: dict[int, dict[str, Any]] = {}
        next_end: dict[int, dict[str, Any]] = {}
        for row in self._rows(host_events, "LAYER_SOURCE", "next_drawable_begin"):
            sequence = self._int(row, "next_drawable_sequence", positive=True)
            if sequence in next_begin:
                raise ValidationError(f"duplicate nextDrawable begin sequence {sequence}")
            next_begin[sequence] = row
        for row in self._rows(host_events, "LAYER_SOURCE", "next_drawable_end"):
            sequence = self._int(row, "next_drawable_sequence", positive=True)
            if sequence in next_end:
                raise ValidationError(f"duplicate nextDrawable end sequence {sequence}")
            next_end[sequence] = row
        if (set(next_begin) != set(next_end) or not next_end
                or sorted(next_end) != list(range(1, len(next_end) + 1))):
            raise ValidationError("nextDrawable begin/end sequence is incomplete or gapped")
        next_drawable_events: list[dict[str, Any]] = []
        next_by_frame: dict[tuple[int, int, int, int, int], list[dict[str, Any]]] = defaultdict(list)
        for sequence in sorted(next_end):
            begin, end = next_begin[sequence], next_end[sequence]
            if (self._int(begin, "call_begin_mach_ns", positive=True)
                    != self._int(end, "call_begin_mach_ns", positive=True)
                    or self._int(end, "call_end_mach_ns", positive=True)
                        < self._int(begin, "call_begin_mach_ns", positive=True)
                    or self._int(begin, "tid", positive=True) != self._int(end, "tid", positive=True)
                    or self._int(end, "duration_ns") != self._int(end, "call_end_mach_ns")
                        - self._int(end, "call_begin_mach_ns")):
                raise ValidationError(f"nextDrawable sequence {sequence} has an inconsistent interval")
            row = dict(end)
            row["next_drawable_sequence"] = sequence
            row["next_drawable_begin_mach_ns"] = self._int(begin, "call_begin_mach_ns")
            row["next_drawable_end_mach_ns"] = self._int(end, "call_end_mach_ns")
            row["duration_ns"] = self._int(end, "duration_ns")
            row["request_phase_fraction"] = None
            next_drawable_events.append(row)
            acquisition_value = self._int(end, "acquisition_sequence")
            submit_value = self._int(end, "driver_submit_seq")
            swapchain_value = self._int(end, "swapchain_id")
            image_index_value = self._int(end, "image_index")
            image_value = self._int(end, "image_id")
            if (acquisition_value > 0 and submit_value > 0 and swapchain_value > 0
                    and image_index_value in (0, 1, 2) and image_value > 0):
                key = (acquisition_value, submit_value, swapchain_value,
                       image_index_value, image_value)
                next_by_frame[key].append(row)

        present_join_rows = self._rows(host_events, "LAYER_SOURCE", "present_drawable_join")
        request_rows = self._rows(host_events, "LAYER_SOURCE", "presented_handler_registered")
        callback_rows = self._rows(host_events, "LAYER_SOURCE", "presented_callback")
        availability_rows = self._rows(host_events, "LAYER_SOURCE", "availability_after_presented_handler")
        release_rows = self._rows(host_events, "LAYER_SOURCE", "drawable_release")
        def by_key(rows: list[dict[str, Any]], key_name: str, what: str) -> dict[int, dict[str, Any]]:
            indexed: dict[int, dict[str, Any]] = {}
            for row in rows:
                key = self._int(row, key_name, positive=True)
                if key in indexed:
                    raise ValidationError(f"duplicate {what} identity {key}")
                indexed[key] = row
            return indexed
        present_joins = by_key(present_join_rows, "present_seq", "present/drawable join")
        if len(present_joins) != count:
            raise ValidationError("present/drawable join rows do not cover each app present exactly once")
        requests_by_drawable = by_key(request_rows, "drawable_id", "presented-handler registration")
        callbacks_by_drawable = by_key(callback_rows, "drawable_id", "present callback")
        availability_by_drawable = by_key(availability_rows, "drawable_id", "availability signal")
        releases_by_drawable = by_key(release_rows, "drawable_id", "drawable release")
        if not (len(requests_by_drawable) == len(callbacks_by_drawable)
                == len(availability_by_drawable) == count):
            raise ValidationError("drawable handler/callback/availability counts differ from frame count")
        known_drawable_ids = {self._int(row, "drawable_id") for row in next_drawable_events
                              if self._int(row, "drawable_id") > 0}
        if any(drawable_id not in known_drawable_ids for drawable_id in releases_by_drawable):
            raise ValidationError("drawable release event lacks a matching nextDrawable identity")

        frames = []
        for ordinal in range(count):
            app_a_begin, app_a_end = app_acquire[ordinal]
            app_s_begin, app_s_end = app_submit[ordinal]
            app_p_begin, app_p_end = app_present[ordinal]
            host_a_begin, host_a_end = host_acquire[ordinal]
            host_s_begin, host_s_end = host_submit[ordinal]
            host_p_begin, host_p_end = host_present[ordinal]
            mvk_a_begin, mvk_a_end = mvk_acquire[ordinal]
            mvk_s_begin, mvk_s_end = mvk_submit[ordinal]
            mvk_p_begin, mvk_p_end = mvk_present[ordinal]
            acquisition = ordinal + 1
            app_frame_id = self._int(app_a_end, "app_frame_id", positive=True)
            image_index = self._int(app_a_end, "image_index")
            if image_index not in (0, 1, 2):
                raise ValidationError(f"frame {app_frame_id} acquired invalid image index {image_index}")
            native_image = native_images[(native_swapchain, image_index)]
            api_ends = (app_a_end, app_s_end, app_p_end)
            if any(self._int(row, "vk_result") not in (0, 1000001003) for row in api_ends):
                raise ValidationError(f"frame {app_frame_id} has a failed app Vulkan call")
            if any(self._int(row, "image_index") != image_index
                   or self._int(row, "image_id", positive=True) <= 0 for row in api_ends):
                raise ValidationError(f"frame {app_frame_id} app call image identity changed")
            if any(self._int(row, "app_frame_id", positive=True) != app_frame_id
                   for row in (app_s_end, app_p_end)):
                raise ValidationError(f"frame {app_frame_id} app acquire/submit/present identities differ")
            if any(self._int(row, "app_acquire_seq", positive=True) != acquisition
                   for row in (app_a_end, app_s_end, app_p_end)):
                raise ValidationError(f"frame {app_frame_id} app acquire sequence is not deterministic")
            if any(self._int(row, "surface_id", positive=True) != native_surface
                   or self._int(row, "image_index") != image_index
                   or self._int(row, "image_id", positive=True) != native_image
                   or self._int(row, "swapchain_id", positive=True) != native_swapchain
                   or self._int(row, "vk_result") not in (0, 1000001003)
                   for row in (host_a_end, host_s_end, host_p_end)):
                raise ValidationError(f"frame {app_frame_id} host/native Vulkan image identity differs")
            # WINE_SOURCE records the per-API call ordinal. The same producer
            # counter is named acquisition/driver sequence in MVK_BOUNDARY.
            # Check both labels explicitly; no timestamp-derived join is used.
            if (self._int(host_a_end, "wine_call_seq", positive=True) != acquisition
                    or self._int(mvk_a_end, "acquisition_sequence", positive=True) != acquisition
                    or self._int(host_s_end, "acquisition_sequence", positive=True) != acquisition
                    or self._int(host_p_end, "acquisition_sequence", positive=True) != acquisition
                    or self._int(mvk_s_end, "acquisition_sequence", positive=True) != acquisition
                    or self._int(mvk_p_end, "acquisition_sequence", positive=True) != acquisition
                    or self._int(host_s_end, "wine_call_seq", positive=True) != ordinal + 1
                    or self._int(mvk_s_end, "driver_submit_seq", positive=True) != ordinal + 1
                    or self._int(host_p_end, "driver_submit_seq", positive=True) != ordinal + 1
                    or self._int(mvk_p_end, "driver_submit_seq", positive=True) != ordinal + 1
                    or self._int(mvk_p_end, "present_seq", positive=True) != ordinal + 1
                    or self._int(host_p_begin, "present_seq", positive=True) != ordinal + 1
                    or self._int(host_p_end, "present_seq", positive=True) != ordinal + 1
                    or self._int(host_p_end, "wine_call_seq", positive=True) != ordinal + 1
                    or self._int(mvk_p_end, "driver_present_seq", positive=True) != ordinal + 1):
                raise ValidationError(f"frame {app_frame_id} lacks exact host acquisition/submit/present correlation")
            for host_end, mvk_end in ((host_a_end, mvk_a_end), (host_s_end, mvk_s_end),
                                      (host_p_end, mvk_p_end)):
                for field in ("swapchain_id", "image_index", "image_id", "vk_result"):
                    if host_end.get(field) != mvk_end.get(field):
                        raise ValidationError(
                            f"frame {app_frame_id} winevulkan/MoltenVK {field} identity differs")
            if (self._int(app_s_end, "app_acquire_seq", positive=True) != acquisition
                    or self._int(app_p_end, "app_acquire_seq", positive=True) != acquisition
                    or self._int(app_p_end, "app_submit_seq", positive=True) != ordinal + 1
                    or self._int(app_p_end, "app_present_seq", positive=True) != ordinal + 1):
                raise ValidationError(f"frame {app_frame_id} lacks exact app frame/submit/present correlation")

            draw_key = (acquisition, ordinal + 1, native_swapchain, image_index, native_image)
            attempts = next_by_frame.get(draw_key, [])
            if not attempts:
                raise ValidationError(f"frame {app_frame_id} has no nextDrawable call for its acquired native image")
            join = present_joins.get(ordinal + 1)
            if (not join or join.get("matched") is not True
                    or self._int(join, "swapchain_count", positive=True) != 1
                    or self._int(join, "present_seq", positive=True) != ordinal + 1
                    or self._int(join, "acquisition_sequence", positive=True) != acquisition
                    or self._int(join, "submit_sequence", positive=True) != ordinal + 1
                    or self._int(join, "surface_id", positive=True) != native_surface
                    or self._int(join, "swapchain_id", positive=True) != native_swapchain
                    or self._int(join, "image_index") != image_index
                    or self._int(join, "image_id", positive=True) != native_image
                    or self._int(join, "queue_id", positive=True)
                        != self._int(host_p_begin, "queue_id", positive=True)
                    or self._int(join, "call_begin_mach_ns", positive=True)
                        != self._int(host_p_begin, "call_begin_mach_ns", positive=True)
                    or self._int(join, "tid", positive=True)
                        != self._int(host_p_begin, "tid", positive=True)):
                raise ValidationError(f"frame {app_frame_id} present is not joined to its drawable by exact identity")
            drawable_id = self._int(join, "drawable_id", positive=True)
            selected_attempts = [row for row in attempts
                                 if self._int(row, "drawable_id") == drawable_id]
            if len(selected_attempts) != 1:
                raise ValidationError(f"frame {app_frame_id} present drawable does not identify one nextDrawable result")
            draw_end = selected_attempts[0]
            if (self._int(draw_end, "texture_pixel_format", positive=True) == 0
                    or self._int(draw_end, "layer_id", positive=True) != layer_id
                    or self._int(draw_end, "surface_id", positive=True) != native_surface
                    or self._int(draw_end, "texture_id", positive=True) <= 0):
                raise ValidationError(f"frame {app_frame_id} selected drawable does not match its WineMetalLayer/native image")
            if any(self._int(row, "texture_pixel_format") > 0 and row is not draw_end
                   for row in attempts):
                raise ValidationError(f"frame {app_frame_id} has multiple valid drawable attempts; association is ambiguous")
            draw_sequence = self._int(draw_end, "next_drawable_sequence", positive=True)
            draw_begin = next_begin[draw_sequence]
            submit_begin_ns = self._int(host_s_begin, "call_begin_mach_ns", positive=True)
            submit_end_ns = self._int(host_s_end, "call_end_mach_ns", positive=True)
            draw_begin_ns = self._int(draw_begin, "call_begin_mach_ns", positive=True)
            draw_end_ns = self._int(draw_end, "call_end_mach_ns", positive=True)
            for attempt in attempts:
                attempt_sequence = self._int(attempt, "next_drawable_sequence", positive=True)
                attempt_begin = next_begin[attempt_sequence]
                attempt_begin_ns = self._int(attempt_begin, "call_begin_mach_ns", positive=True)
                attempt_end_ns = self._int(attempt, "call_end_mach_ns", positive=True)
                if (self._int(attempt, "layer_id", positive=True) != layer_id
                        or self._int(attempt, "surface_id", positive=True) != native_surface
                        or self._int(attempt, "tid", positive=True)
                            != self._int(host_s_begin, "tid", positive=True)
                        or self._int(attempt_begin, "tid", positive=True)
                            != self._int(host_s_begin, "tid", positive=True)
                        or attempt_begin_ns < submit_begin_ns or attempt_end_ns > submit_end_ns):
                    raise ValidationError(f"frame {app_frame_id} nextDrawable attempt lacks exact in-submit context")
                attempt["app_frame_id"] = app_frame_id
                attempt["selected_for_present"] = attempt is draw_end
            if (self._int(draw_begin, "acquisition_sequence", positive=True) != acquisition
                    or self._int(draw_end, "acquisition_sequence", positive=True) != acquisition
                    or self._int(draw_begin, "driver_submit_seq", positive=True) != ordinal + 1
                    or self._int(draw_end, "driver_submit_seq", positive=True) != ordinal + 1
                    or self._int(draw_begin, "swapchain_id", positive=True) != native_swapchain
                    or self._int(draw_end, "swapchain_id", positive=True) != native_swapchain
                    or self._int(draw_begin, "image_index") != image_index
                    or self._int(draw_end, "image_index") != image_index
                    or self._int(draw_begin, "image_id", positive=True) != native_image
                    or self._int(draw_end, "image_id", positive=True) != native_image
                    or self._int(draw_begin, "tid", positive=True) != self._int(host_s_begin, "tid", positive=True)
                    or self._int(draw_end, "tid", positive=True) != self._int(host_s_begin, "tid", positive=True)
                    or draw_begin_ns < submit_begin_ns or draw_end_ns > submit_end_ns):
                raise ValidationError(f"frame {app_frame_id} nextDrawable lacks same-thread in-submit image identity")
            if (self._int(draw_end, "drawable_id", positive=True) != drawable_id
                    or self._int(join, "texture_id", positive=True) != self._int(draw_end, "texture_id", positive=True)):
                raise ValidationError(f"frame {app_frame_id} present/drawable texture identity differs")
            request = requests_by_drawable.get(drawable_id)
            callback = callbacks_by_drawable.get(drawable_id)
            availability = availability_by_drawable.get(drawable_id)
            release = releases_by_drawable.get(drawable_id)
            if not all((request, callback, availability, release)):
                raise ValidationError(f"frame {app_frame_id} has an incomplete drawable lifecycle")
            for row in (request, callback, availability, release):
                if (self._int(row, "surface_id", positive=True) != native_surface
                        or self._int(row, "acquisition_sequence", positive=True) != acquisition
                        or self._int(row, "image_id", positive=True) != native_image
                        or self._int(row, "driver_present_seq", positive=True) != ordinal + 1
                        or self._int(row, "present_queue_id", positive=True)
                            != self._int(host_p_begin, "queue_id", positive=True)
                        or self._int(row, "present_swapchain_id", positive=True) != native_swapchain
                        or self._int(row, "present_image_index") != image_index
                        or self._int(row, "present_image_id", positive=True) != native_image
                        ):
                    raise ValidationError(f"frame {app_frame_id} drawable lifecycle identity is ambiguous")
            if (self._int(callback, "presented_time_mach_ns") < 0
                    or type(callback.get("is_displayed")) is not bool
                    or self._int(availability, "event_id", positive=True) == 0):
                raise ValidationError(f"frame {app_frame_id} has malformed presented/availability data")
            if (callback.get("presentation_clock_domain") != "mach_absolute_time_ns"
                    or callback.get("presented_time_basis") != "mach_absolute_time_seconds"
                    or callback.get("presented_time_conversion") != "floor(seconds*1e9)"
                    or callback.get("callback_delivery_clock_domain") != "mach_absolute_time_ns"):
                raise ValidationError(f"frame {app_frame_id}: callback timestamp has an unidentified clock basis")
            if (self._int(draw_end, "duration_ns")
                    != self._int(draw_end, "call_end_mach_ns") - self._int(draw_end, "call_begin_mach_ns")):
                raise ValidationError(f"frame {app_frame_id} nextDrawable duration is inconsistent")

            frames.append({
                "app_frame_id": app_frame_id, "acquisition_sequence": acquisition,
                "app_image_handle": self._int(app_a_end, "image_id", positive=True),
                "image_index": image_index, "native_swapchain_id": native_swapchain,
                "native_surface_id": native_surface,
                "native_image_id": native_image,
                "app_acquire_duration_ns": self._int(app_a_end, "duration_ns"),
                "app_submit_duration_ns": self._int(app_s_end, "duration_ns"),
                "app_present_duration_ns": self._int(app_p_end, "duration_ns"),
                "native_acquire_duration_ns": self._int(host_a_end, "duration_ns"),
                "native_acquire_begin_mach_ns": self._int(host_a_begin, "call_begin_mach_ns"),
                "native_acquire_end_mach_ns": self._int(host_a_end, "call_end_mach_ns"),
                "native_submit_duration_ns": self._int(host_s_end, "duration_ns"),
                "native_submit_begin_mach_ns": submit_begin_ns,
                "native_submit_end_mach_ns": submit_end_ns,
                "native_present_duration_ns": self._int(host_p_end, "duration_ns"),
                "next_drawable_begin_mach_ns": self._int(draw_begin, "call_begin_mach_ns"),
                "next_drawable_end_mach_ns": self._int(draw_end, "call_end_mach_ns"),
                "next_drawable_duration_ns": self._int(draw_end, "duration_ns"),
                "drawable_id": drawable_id, "texture_id": self._int(draw_end, "texture_id", positive=True),
                "present_request_mach_ns": self._int(host_p_begin, "call_begin_mach_ns", positive=True),
                "presented_handler_registered_mach_ns": self._int(request, "ts_mach_ns"),
                "presented_callback_mach_ns": self._int(callback, "ts_mach_ns"),
                "presented_time_mach_ns": self._int(callback, "presented_time_mach_ns"),
                "is_displayed": callback["is_displayed"],
                "availability_observed_mach_ns": self._int(availability, "ts_mach_ns"),
                "drawable_release_mach_ns": self._int(release, "ts_mach_ns"),
                "next_drawable_attempts": [
                    {"sequence": self._int(row, "next_drawable_sequence", positive=True),
                     "drawable_id": self._int(row, "drawable_id"),
                     "texture_pixel_format": self._int(row, "texture_pixel_format"),
                     "duration_ns": self._int(row, "duration_ns"),
                     "selected_for_present": row["selected_for_present"]}
                    for row in attempts
                ],
                "app_event_ids": [self._int(row, "event_id") for row in
                                   (app_a_begin, app_a_end, app_s_begin, app_s_end,
                                    app_p_begin, app_p_end)],
                "host_event_ids": [self._int(row, "event_id") for row in
                                   (host_a_begin, host_a_end, host_s_begin, host_s_end,
                                    host_p_begin, host_p_end, draw_begin, draw_end,
                                    join, request, callback, availability, release)],
                "mvk_event_ids": [self._int(row, "event_id") for row in
                                  (mvk_a_begin, mvk_a_end, mvk_s_begin, mvk_s_end,
                                   mvk_p_begin, mvk_p_end)],
                "deterministic_join": "app API ordinal + native swapchain/image/acquisition/submit tuple -> WineMetalLayer drawable; exact present tuple -> drawable ID; callback and release keyed by drawable ID",
            })

        displayed = [row for row in frames if row["is_displayed"] and row["presented_time_mach_ns"] > 0]
        if any(row["is_displayed"] != (row["presented_time_mach_ns"] > 0) for row in frames):
            raise ValidationError("presented callback display flag disagrees with presentedTime")
        displayed_times = sorted(row["presented_time_mach_ns"] for row in displayed)
        intervals = [right - left for left, right in zip(displayed_times, displayed_times[1:])]
        if not intervals or any(value <= 0 for value in intervals):
            raise ValidationError("valid presentedTime records are insufficient or non-monotonic")
        period = sorted(intervals)[(len(intervals) - 1) // 2]
        if period <= 0:
            raise ValidationError("effective presentation period is not positive")
        for row in frames:
            relative = row["next_drawable_begin_mach_ns"] - displayed_times[0]
            row["request_phase_fraction"] = (relative % period) / period
            row["effective_period_ns"] = period
        for event in next_drawable_events:
            relative = event["next_drawable_begin_mach_ns"] - displayed_times[0]
            event["request_phase_fraction"] = (relative % period) / period
            event["effective_period_ns"] = period
        if any(event["duration_ns"] >= 100_000_000 and event.get("app_frame_id") is None
               for event in next_drawable_events):
            raise ValidationError("a >=100 ms nextDrawable wait cannot be tied to an app frame/native image")



        wait_series = [row["duration_ns"] for row in next_drawable_events]
        submit_series = [row["native_submit_duration_ns"] for row in frames]
        present_series = [row["native_present_duration_ns"] for row in frames]
        acquire_series = [row["native_acquire_duration_ns"] for row in frames]
        app_submit_series = [row["app_submit_duration_ns"] for row in frames]
        app_present_series = [row["app_present_duration_ns"] for row in frames]
        app_acquire_series = [row["app_acquire_duration_ns"] for row in frames]
        duration_seconds = run_ns / 1_000_000_000
        loader = loader_evidence or {}
        if loader.get("winevulkan_dll_loaded") is not True or loader.get("winevulkan_so_loaded") is not True:
            raise ValidationError("loader log does not establish the exact winevulkan DLL and Unix module")
        loaded_mvk = str(Path(str(loader.get("moltenvk_loaded_path", ""))).resolve())
        if loaded_mvk not in self.expected_moltenvk_paths:
            raise ValidationError("loader log does not establish the exact pinned MoltenVK path")
        if loader.get("forbidden_module_names"):
            raise ValidationError(f"forbidden D3D/DXVK/bridge module(s) loaded: {loader['forbidden_module_names']}")

        return {
            "is_valid": True, "quantitative_ready": True, "frame_count": len(frames),
            "acquired_fps": len(app_acquire) / duration_seconds,
            "presented_fps": len(displayed) / duration_seconds,
            "wsi_present_rate": len(host_present) / duration_seconds,
            "displayed_frames": len(displayed), "presentation_drop_count": len(frames) - len(displayed),
            "presentation_drop_rate": (len(frames) - len(displayed)) / len(frames),
            "effective_refresh_period_ns": period,
            "effective_refresh_hz": 1_000_000_000 / period,
            "layer_configuration": layer, "app_configuration": config,
            "process_identity": process_identity, "proxy_identity": proxy_identity,
            "native_dispatch_sources": dispatch_paths, "loader_evidence": loader,
            "trace_summary": trace_summary,
            "next_drawable": self.duration_histogram(wait_series),
            "vkQueueSubmit2": self.duration_histogram(submit_series),
            "vkQueueSubmit2_app": self.duration_histogram(app_submit_series),
            "vkQueuePresentKHR_cpu": self.duration_histogram(present_series),
            "vkQueuePresentKHR_app_cpu": self.duration_histogram(app_present_series),
            "vkAcquireNextImageKHR": self.duration_histogram(acquire_series),
            "vkAcquireNextImageKHR_app": self.duration_histogram(app_acquire_series),
            "refresh_phase": {
                "method": "request start modulo the measured median displayed-presentedTime interval; no 60.000 Hz assumption",
                "period_ns": period,
                "10_to_18_ms": [row for row in next_drawable_events
                                if 10_000_000 <= row["duration_ns"] <= 18_000_000],
                "over_25_ms": [row for row in next_drawable_events
                               if row["duration_ns"] > 25_000_000],
                "over_100_ms": [row for row in next_drawable_events
                                if row["duration_ns"] > 100_000_000],
            },
            "next_drawable_events": next_drawable_events,
            "frames": frames,
            "availability_semantics": (
                "availability_after_presented_handler is emitted after MoltenVK's original CAMetalDrawable handler returns. "
                "In the pinned v1.4.2 source, normal presentation uses endPresentation -> signalAndUntrack/release; "
                "makeAvailable is the explicit-release/teardown helper, not the normal present callback. "
                "The next successful acquire of the same image_index is checked as the subsequent availability witness."
            ),
        }


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
