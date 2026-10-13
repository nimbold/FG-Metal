#!/usr/bin/env python3
"""Offline source-contract tests for target AppKit window telemetry."""
from __future__ import annotations

import unittest
from pathlib import Path
import re


SOURCE = Path(__file__).with_name("trace_loader.mm").read_text(encoding="utf-8")


def function_body(signature: str) -> str:
    match = re.search(re.escape(signature) + r"[^;{]*\{", SOURCE)
    if not match:
        raise AssertionError(f"function not found: {signature}")
    opening = match.end() - 1
    depth = 0
    for position in range(opening, len(SOURCE)):
        if SOURCE[position] == "{":
            depth += 1
        elif SOURCE[position] == "}":
            depth -= 1
            if depth == 0:
                return SOURCE[opening + 1:position]
    raise AssertionError(f"unterminated function: {signature}")


class AppKitTelemetrySourceTests(unittest.TestCase):
    def test_target_ownership_is_checked_before_capture(self) -> None:
        allowed = function_body("bool target_window_telemetry_allowed()")
        self.assertIn("g_enabled.load(std::memory_order_acquire)", allowed)
        self.assertIn("g_target_process", allowed)
        self.assertIn("g_target_unix_pid == getpid()", allowed)
        self.assertIn('std::strcmp(g_trace_role, "target") == 0', allowed)
        self.assertIn("target_window_telemetry_allowed()", function_body(
            "void emit_layer_configuration(CAMetalLayer* layer)"))
        self.assertIn("target_window_telemetry_allowed()", function_body(
            "void emit_window_telemetry("))

    def test_capture_runs_on_main_thread_and_timer_is_at_most_one_hz(self) -> None:
        capture = function_body("void emit_window_telemetry(")
        self.assertIn("[NSThread isMainThread]", capture)
        self.assertIn("dispatch_get_main_queue()", capture)
        setup = function_body("void start_window_observation(CAMetalLayer* layer)")
        self.assertIn("dispatch_get_main_queue()", setup)
        self.assertIn("timerWithTimeInterval:1.0 repeats:YES", setup)
        self.assertIn("g_window_telemetry_timer.tolerance = 0.1", setup)
        self.assertIn("NSRunLoopCommonModes", setup)

    def test_window_telemetry_starts_at_surface_creation_before_drawable_acquisition(self) -> None:
        surface = function_body(
            "VkResult VKAPI_CALL wrapped_create_metal_surface(")
        self.assertIn(
            "start_window_observation((CAMetalLayer*)create_info->pLayer)", surface)
        self.assertLess(surface.index("start_window_observation("),
                        surface.index("record_surface_creation("))
        drawable = function_body("id<CAMetalDrawable> layer_next_drawable(")
        self.assertIn("start_window_observation(layer)", drawable)
        self.assertIn("emit_layer_presentation_configuration(layer)", drawable)

    def test_presentation_configuration_waits_for_positive_layer_dimensions(self) -> None:
        setup = function_body("void start_window_observation(CAMetalLayer* layer)")
        self.assertNotIn("layer_presentation_configuration", setup)
        configuration = function_body(
            "void emit_layer_presentation_configuration(CAMetalLayer* layer)")
        self.assertIn("snapshot.drawable_width <= 0", configuration)
        self.assertIn("snapshot.drawable_height <= 0", configuration)
        self.assertIn("snapshot.layer_bounds_width <= 0", configuration)
        self.assertIn("snapshot.layer_bounds_height <= 0", configuration)
        self.assertIn('"layer_presentation_configuration"', configuration)

    def test_layer_and_window_reads_have_no_presentation_or_geometry_writes(self) -> None:
        helpers_start = SOURCE.index("bool target_window_telemetry_allowed()")
        helpers_end = SOURCE.index("void patch_wine_layer()", helpers_start)
        telemetry_helpers = SOURCE[helpers_start:helpers_end]
        forbidden = (
            "drawableSize =", "setActivationPolicy", "activateIgnoringOtherApps",
            "makeKeyAndOrderFront", "orderFrontRegardless",
            "setHidesOnDeactivate", ".level =",
        )
        for operation in forbidden:
            with self.subTest(operation=operation):
                self.assertNotIn(operation, telemetry_helpers)
        self.assertNotIn("640.0", telemetry_helpers)
        self.assertNotIn("360.0", telemetry_helpers)

    def test_notifications_cover_window_and_application_state(self) -> None:
        source = function_body("void emit_window_telemetry(")
        for notification in (
            "NSApplicationDidBecomeActiveNotification",
            "NSApplicationDidResignActiveNotification",
            "NSWindowDidMoveNotification", "NSWindowDidResizeNotification",
            "NSWindowDidChangeScreenNotification",
            "NSWindowDidChangeBackingPropertiesNotification",
            "NSWindowDidChangeOcclusionStateNotification",
            "NSWindowDidMiniaturizeNotification",
            "NSWindowDidDeminiaturizeNotification",
            "NSWindowDidBecomeKeyNotification", "NSWindowDidResignKeyNotification",
            "NSWindowDidBecomeMainNotification", "NSWindowDidResignMainNotification",
            "NSWindowWillCloseNotification",
        ):
            with self.subTest(notification=notification):
                self.assertIn(notification, source)
        self.assertIn("note.name, true", source)

    def test_snapshot_schema_contains_visibility_display_and_layer_metrics(self) -> None:
        source = function_body("void emit_window_telemetry(")
        for field in (
            "window_associated", "window_visible", "window_key", "window_main",
            "window_occluded", "window_occlusion_state", "window_miniaturized",
            "app_active", "window_frame_x", "window_frame_width", "screen_id",
            "screen_visible_frame_x", "screen_backing_scale",
            "window_backing_scale_factor", "screen_refresh_hz",
            "window_visible_intersection_area", "layer_bounds_width",
            "drawable_width", "drawable_height", "contents_scale",
            "maximum_drawable_count", "allows_next_drawable_timeout",
            "display_sync_enabled", "pixel_format", "framebuffer_only",
        ):
            with self.subTest(field=field):
                self.assertIn(f'\\"{field}\\"', source)

    def test_display_refresh_lookup_is_cached_by_screen(self) -> None:
        refresh = function_body("double cached_screen_refresh_hz(")
        self.assertIn("g_cached_refresh_screen_id != screen_id || !g_cached_refresh_known", refresh)
        self.assertIn("CGDisplayCopyDisplayMode", refresh)
        self.assertIn("CGDisplayModeRelease", refresh)


if __name__ == "__main__":
    unittest.main()
