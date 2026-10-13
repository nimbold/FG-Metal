#!/usr/bin/env python3
"""Focused tests for STEP 11D.3-B trace lifecycle validation.

These fixtures exercise exact frame/image/drawable joins without starting Wine,
loading Vulkan, or touching PrefixLease/storage state.
"""
from __future__ import annotations

import copy
import re
import unittest
from pathlib import Path

from experiments.storage.trace_validator import BareWineTraceValidator, ValidationError


WINE = "/pinned/wine/lib/wine/x86_64-unix/wine"
WINE_LAUNCHER = "/pinned/wine/bin/wine"
WINESERVER = "/pinned/wine/bin/wineserver"
WINEVULKAN_DLL = "/pinned/prefix/drive_c/windows/system32/winevulkan.dll"
WINEVULKAN_SO = "/pinned/wine/lib/wine/x86_64-unix/winevulkan.so"
HOST_LOADER = "/pinned/wine/lib/wine/x86_64-unix/win32u.so"
WINEMAC_SO = "/pinned/wine/lib/wine/x86_64-unix/winemac.so"
TRACE_PROXY = "/pinned/run/trace-loader/libMoltenVK.dylib"
MOLTENVK = "/pinned/run/mvk/libMoltenVK.1.dylib"
APP = "/pinned/control/bare_vulkan_app.exe"
MODULE_HASHES = {
    "wine": "1" * 64, "wine_launcher": "a" * 64, "wineserver": "2" * 64,
    "winevulkan_dll": "3" * 64, "winevulkan_so": "4" * 64,
    "wine_host_loader": "5" * 64, "winemac_so": "6" * 64,
    "trace_proxy": "7" * 64, "moltenvk": "8" * 64,
}
SURFACE = 7001
LAYER = 6001
SWAPCHAIN = 8001
QUEUE = 9001
IMAGES = {0: 2001, 1: 2002, 2: 2003}


class BareWineTraceValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.validator = BareWineTraceValidator(
            expected_original_vulkan_path=MOLTENVK,
            expected_moltenvk_paths=[MOLTENVK],
            expected_startup_identities={
                "wine": {"path": WINE, "sha256": MODULE_HASHES["wine"]},
                "wine_launcher": {"path": WINE_LAUNCHER,
                                  "sha256": MODULE_HASHES["wine_launcher"]},
                "wineserver": {"path": WINESERVER, "sha256": MODULE_HASHES["wineserver"]},
                "winevulkan_dll": {"path": WINEVULKAN_DLL, "sha256": MODULE_HASHES["winevulkan_dll"]},
                "winevulkan_so": {"path": WINEVULKAN_SO, "sha256": MODULE_HASHES["winevulkan_so"]},
                "wine_host_loader": {"path": HOST_LOADER, "sha256": MODULE_HASHES["wine_host_loader"]},
                "winemac_so": {"path": WINEMAC_SO, "sha256": MODULE_HASHES["winemac_so"]},
                "trace_proxy": {"path": TRACE_PROXY, "sha256": MODULE_HASHES["trace_proxy"]},
                "moltenvk": {"path": MOLTENVK, "sha256": MODULE_HASHES["moltenvk"]},
            },
            expected_app_path=APP, expected_app_sha256="9" * 64,
            expected_moltenvk_version="MoltenVK v1.4.2",
            expected_duration_seconds=3,
        )

    def test_helper_or_unbound_trace_cannot_satisfy_target_validation(self):
        app, host, loader = self._valid_traces()
        self.validator.expected_target_unix_pid = 12345
        self.validator.expected_experiment_token = "a" * 32
        for row in host:
            row.update(host_pid=12345, target_unix_pid=12345,
                       experiment_token="a" * 32, trace_role="target")
        app[0].update(target_unix_pid=12345, experiment_token="a" * 32)
        self.assertTrue(self.validator.validate(app, host, loader_evidence=loader)["quantitative_ready"])
        for field, value in (("host_pid", 54321), ("target_unix_pid", 54321),
                             ("experiment_token", "b" * 32), ("trace_role", "helper")):
            with self.subTest(field=field):
                original = host[0][field]
                host[0][field] = value
                with self.assertRaisesRegex(ValidationError, "helper or unbound"):
                    self.validator.validate(app, host, loader_evidence=loader)
                host[0][field] = original

    def test_pe_app_must_bind_supervised_target(self):
        app, host, loader = self._valid_traces()
        self.validator.expected_target_unix_pid = 12345
        self.validator.expected_experiment_token = "a" * 32
        for row in host:
            row.update(host_pid=12345, target_unix_pid=12345,
                       experiment_token="a" * 32, trace_role="target")
        app[0].update(target_unix_pid=54321, experiment_token="a" * 32)
        with self.assertRaisesRegex(ValidationError, "PE app does not bind"):
            self.validator.validate(app, host, loader_evidence=loader)

    def _valid_traces(self):
        app: list[dict] = []
        host: list[dict] = []
        next_app_id = 1
        next_host_id = 1

        def app_event(name: str, **fields):
            nonlocal next_app_id
            timestamp_ticks = fields.get("call_begin_qpc_ticks", 1)
            qpc_frequency = 999_999_999
            row = {"origin": "APP_SOURCE", "event_id": next_app_id,
                   "event": name, "ts_clock_domain": "windows_qpc_ticks",
                   "timestamp_clock_source": "QueryPerformanceCounter",
                   "ts_qpc_ticks": timestamp_ticks,
                   "ts_qpc_ns": timestamp_ticks * 1_000_000_000 // qpc_frequency,
                   "qpc_frequency": qpc_frequency,
                   "tid": 41, **fields}
            for tick_field, ns_field in (
                    ("call_begin_qpc_ticks", "call_begin_qpc_ns"),
                    ("call_end_qpc_ticks", "call_end_qpc_ns"),
                    ("duration_qpc_ticks", "duration_ns")):
                if tick_field in fields:
                    row[ns_field] = fields[tick_field] * 1_000_000_000 // qpc_frequency
            next_app_id += 1
            app.append(row)
            return row

        def host_event(origin: str, name: str, ts: int, tid: int = 51, **fields):
            nonlocal next_host_id
            row = {"origin": origin, "event_id": next_host_id, "event": name,
                   "ts_mach_ns": ts, "ts_clock_domain": "mach_absolute_time_ns",
                   "tid": tid, **fields}
            next_host_id += 1
            host.append(row)
            return row

        config = {
            "origin": "APP_CONFIG", "event_id": 0, "event": "app_configuration",
            "timestamp_clock_domain": "windows_qpc_ticks",
            "event_timestamp_clock_source": "QueryPerformanceCounter",
            "timestamp_conversion": "floor(ticks*1e9/qpc_frequency)",
            "process_architecture": "x86_64", "requested_window_client_width": 640,
            "requested_window_client_height": 360, "actual_window_client_width": 640,
            "actual_window_client_height": 360, "window_mode": "windowed",
            "window_dpi_aware": False, "dpi_x": 96, "dpi_y": 96,
            "window_logical_dpi_scale_x": 1.0, "window_logical_dpi_scale_y": 1.0,
            "display_name": "DISPLAY1", "display_refresh_hz": 60,
            "requested_format": "VK_FORMAT_B8G8R8A8_UNORM", "actual_format": 44,
            "actual_format_name": "VK_FORMAT_B8G8R8A8_UNORM",
            "requested_present_mode": "VK_PRESENT_MODE_FIFO_KHR", "actual_present_mode": 2,
            "actual_present_mode_name": "VK_PRESENT_MODE_FIFO_KHR", "actual_color_space": 0,
            "requested_swapchain_width": 640, "requested_swapchain_height": 360,
            "actual_swapchain_width": 640, "actual_swapchain_height": 360,
            "requested_image_count": 3, "actual_image_count": 3,
            "submit2_entrypoint": "vkQueueSubmit2", "exit_code": 0,
            "stop_reason": "duration_elapsed", "fatal_error": "",
            "window_visible_at_start": True,
            "host_runtime_startup_gate_passed": True,
            "winevulkan_dll_module_loaded": True,
            "winevulkan_dll_before_frame_loop": True,
            "winevulkan_dll_module_path": "C:\\windows\\system32\\winevulkan.dll",
            "winevulkan_dll_host_path": WINEVULKAN_DLL,
            "winevulkan_dll_sha256": MODULE_HASHES["winevulkan_dll"],
            "actual_run_ns": 3_000_000_000,
            "qpc_frequency": 999_999_999, "run_seconds_requested": 3,
            "trace_events_dropped": 0, "app_frames_started": 3,
            "app_submits": 3, "app_present_requests": 3,
        }
        app.insert(0, config)

        host_event("INSTRUMENTATION", "clock_metadata", 1,
                   event_clock_source="mach_absolute_time",
                   event_clock_domain="mach_absolute_time_ns",
                   mach_timebase_numer=1, mach_timebase_denom=1,
                   event_conversion="floor(ticks*numer/denom)",
                   presented_time_source="CAMetalDrawable.presentedTime",
                   presented_time_basis="mach_absolute_time_seconds",
                   presented_time_clock_domain="mach_absolute_time_ns",
                   presented_time_conversion="floor(seconds*1e9)",
                   presented_time_clock_contract=(
                       "Apple Metal host-time seconds; present(at:) is Mach absolute seconds"),
                   callback_delivery_clock_source="mach_absolute_time",
                   callback_delivery_clock_domain="mach_absolute_time_ns")
        startup_id = "pid-100"
        process_start_ns = 2
        host_event("INSTRUMENTATION", "process_identity", 2,
                   startup_id=startup_id, compiled_architecture="x86_64",
                   process_architecture="x86_64", process_translated=1,
                   process_start_monotonic_ns=process_start_ns,
                   process_start_clock_domain="mach_absolute_time_ns",
                   main_executable_path=WINE, main_executable_sha256=MODULE_HASHES["wine"],
                   wine_launcher_path=WINE_LAUNCHER,
                   wine_launcher_sha256=MODULE_HASHES["wine_launcher"])
        for role in ("wine", "wine_launcher", "winevulkan_dll", "winevulkan_so",
                     "wine_host_loader", "winemac_so", "trace_proxy", "moltenvk",
                     "wineserver"):
            identity = self.validator.expected_startup_identities[role]
            loaded = role not in {"wine_launcher", "wineserver"}
            identity_kind = (
                "prefixlease-bound-executable" if role == "wineserver" else
                "prefixlease-spawn-executable" if role == "wine_launcher" else
                "win32-hmodule-proof-in-paired-app-trace" if role == "winevulkan_dll" else
                "dyld-main-executable" if role == "wine" else
                "dyld-loaded-image")
            host_event("INSTRUMENTATION", "startup_module_identity", 3,
                       startup_id=startup_id, startup_phase=True, role=role,
                       path=identity["path"], sha256=identity["sha256"],
                       identity_kind=identity_kind,
                       loaded_in_process=loaded, observed_path=identity["path"],
                       observed_sha256=identity["sha256"], identity_match=True)
        host_event("INSTRUMENTATION", "vulkan_proxy_identity", 4,
                   startup_id=startup_id, real_gipa_available=True,
                   metal_surface_dlsym_available=True,
                   original_vulkan_library=MOLTENVK,
                   original_vulkan_library_sha256=MODULE_HASHES["moltenvk"],
                   loaded_delegate_path=MOLTENVK,
                   loaded_delegate_sha256=MODULE_HASHES["moltenvk"],
                   trace_proxy_path=TRACE_PROXY,
                   trace_proxy_sha256=MODULE_HASHES["trace_proxy"],
                   loaded_proxy_path=TRACE_PROXY,
                   loaded_proxy_sha256=MODULE_HASHES["trace_proxy"],
                   process_start_monotonic_ns=process_start_ns,
                   process_start_clock_domain="mach_absolute_time_ns")
        host_event("INSTRUMENTATION", "host_runtime_startup", 5,
                   startup_id=startup_id, startup_valid=True,
                   process_architecture="x86_64", process_translated=1,
                   process_start_monotonic_ns=process_start_ns,
                   process_start_clock_domain="mach_absolute_time_ns",
                   winevulkan_so_path=WINEVULKAN_SO,
                   winevulkan_so_sha256=MODULE_HASHES["winevulkan_so"],
                   wine_host_loader_path=HOST_LOADER,
                   wine_host_loader_sha256=MODULE_HASHES["wine_host_loader"],
                   trace_proxy_path=TRACE_PROXY,
                   trace_proxy_sha256=MODULE_HASHES["trace_proxy"],
                   host_loader_role=("win32u.so::_vulkan_init_once direct dlopen "
                                     "libMoltenVK.dylib; no Khronos libvulkan"),
                   moltenvk_path=MOLTENVK,
                   moltenvk_sha256=MODULE_HASHES["moltenvk"],
                   moltenvk_version="MoltenVK v1.4.2",
                   moltenvk_version_source="vkGetVersionStringsMVK from loaded delegate",
                   vulkan_runtime_version="1.4.357",
                   vulkan_api_version=1_004_357,
                   loaded_moltenvk_count=1, loaded_proxy_count=1,
                   loaded_winevulkan_count=1,
                   loaded_host_loader_count=1, loaded_libvulkan_dylib_count=0)
        host_event("LAYER_SOURCE", "layer_configuration", 6,
                   layer_id=LAYER, layer_class="WineMetalLayer",
                   layer_is_cametal_layer=True,
                   layer_class_image_path=WINEMAC_SO,
                   layer_class_image_sha256=MODULE_HASHES["winemac_so"],
                   maximum_drawable_count=3, allows_next_drawable_timeout=True,
                   display_sync_enabled=True, drawable_width=640.0, drawable_height=360.0,
                   contents_scale=2.0, pixel_format=80, framebuffer_only=True,
                   layer_bounds_width_points=320.0, layer_bounds_height_points=180.0)
        host_event("LOGGER", "trace_summary", 7, dropped_count=0,
                   write_failure_count=0, ring_capacity=8192)
        for index, function in enumerate(("vkAcquireNextImageKHR", "vkQueueSubmit2",
                                          "vkQueuePresentKHR", "vkGetSwapchainImagesKHR")):
            host_event("INSTRUMENTATION", "native_dispatch_source", 5 + index,
                       function=function, resolved=True, library_path=MOLTENVK,
                       symbol_name=function)

        # The pinned Wine macOS path creates one EXT_metal_surface and one swapchain.
        surface_start = 100
        host_event("MVK_BOUNDARY", "vkCreateMetalSurfaceEXT_begin", surface_start,
                   wine_call_seq=1, layer_id=LAYER, call_begin_mach_ns=surface_start)
        host_event("MVK_BOUNDARY", "vkCreateMetalSurfaceEXT_end", surface_start + 1,
                   wine_call_seq=1, layer_id=LAYER, surface_id=SURFACE, vk_result=0,
                   call_begin_mach_ns=surface_start, call_end_mach_ns=surface_start + 1,
                   duration_ns=1)
        swap_start = 110
        swap_common = {"wine_call_seq": 1, "surface_id": SURFACE,
                       "surface_handle": SURFACE, "requested_image_count": 3,
                       "format": 44, "color_space": 0, "width": 640,
                       "height": 360, "present_mode": 2}
        host_event("MVK_BOUNDARY", "vkCreateSwapchainKHR_begin", swap_start,
                   device_id=1, **swap_common, call_begin_mach_ns=swap_start)
        host_event("MVK_BOUNDARY", "vkCreateSwapchainKHR_end", swap_start + 1,
                   device_id=1, swapchain_id=SWAPCHAIN, **swap_common, vk_result=0,
                   call_begin_mach_ns=swap_start, call_end_mach_ns=swap_start + 1,
                   duration_ns=1)
        for image_index, image_id in IMAGES.items():
            host_event("MVK_BOUNDARY", "swapchain_image_identity", 120 + image_index,
                       swapchain_id=SWAPCHAIN, surface_id=SURFACE,
                       image_index=image_index, image_id=image_id)

        for ordinal, image_index in enumerate((0, 1, 2), 1):
            frame_id = ordinal
            image_id = IMAGES[image_index]
            base = 1_000_000_000 + (ordinal - 1) * 16_666_667
            app_image_id = 30_000 + ordinal

            def app_pair(api: str, sequence_field: str, sequence: int,
                         extra_begin: dict, extra_end: dict, start: int, duration: int = 1):
                common = {"app_frame_id": frame_id, sequence_field: sequence, **extra_begin}
                app_event(api + "_begin", call_begin_qpc_ticks=start, **common)
                app_event(api + "_end", call_begin_qpc_ticks=start,
                          call_end_qpc_ticks=start + duration,
                          duration_qpc_ticks=duration, duration_ns=duration, vk_result=0,
                          **{**common, **extra_end})

            app_pair("app_vkAcquireNextImageKHR", "app_acquire_seq", ordinal,
                     {}, {"image_index": image_index, "image_id": app_image_id}, base + 1)
            app_pair("app_vkQueueSubmit2", "app_submit_seq", ordinal,
                     {"app_acquire_seq": ordinal},
                     {"app_acquire_seq": ordinal, "image_index": image_index,
                      "image_id": app_image_id}, base + 4)
            app_pair("app_vkQueuePresentKHR", "app_present_seq", ordinal,
                     {"app_acquire_seq": ordinal, "app_submit_seq": ordinal,
                      "image_index": image_index, "image_id": app_image_id},
                     {"app_acquire_seq": ordinal, "app_submit_seq": ordinal,
                      "image_index": image_index, "image_id": app_image_id}, base + 13)

            host_common = {"swapchain_id": SWAPCHAIN, "surface_id": SURFACE,
                           "image_index": image_index, "image_id": image_id}
            acq_fields = {**host_common, "device_id": 2}
            for origin in ("WINE_SOURCE", "MVK_BOUNDARY"):
                host_event(origin, "vkAcquireNextImageKHR_begin", base + 1,
                           wine_call_seq=ordinal, call_begin_mach_ns=base + 1,
                           **acq_fields)
                host_event(origin, "vkAcquireNextImageKHR_end", base + 2,
                           wine_call_seq=ordinal,
                           **({"acquisition_sequence": ordinal} if origin == "MVK_BOUNDARY" else {}),
                           **host_common, vk_result=0,
                           call_begin_mach_ns=base + 1, call_end_mach_ns=base + 2,
                           duration_ns=1)

            submit_fields = {**host_common, "queue_id": QUEUE,
                             "acquisition_sequence": ordinal}
            for origin in ("WINE_SOURCE", "MVK_BOUNDARY"):
                host_event(origin, "vkQueueSubmit2_begin", base + 4,
                           wine_call_seq=ordinal, call_begin_mach_ns=base + 4,
                           **submit_fields)
                host_event(origin, "vkQueueSubmit2_end", base + 12,
                           wine_call_seq=ordinal,
                           **({"driver_submit_seq": ordinal} if origin == "MVK_BOUNDARY" else {}),
                           **submit_fields, vk_result=0,
                           call_begin_mach_ns=base + 4, call_end_mach_ns=base + 12,
                           duration_ns=8)

            next_sequence = ordinal
            drawable_id = 10_000 + ordinal
            drawable_identity = 20_000 + ordinal
            texture_id = 40_000 + ordinal
            next_common = {"next_drawable_sequence": next_sequence, "layer_id": LAYER,
                           "surface_id": SURFACE, "acquisition_sequence": ordinal,
                           "swapchain_id": SWAPCHAIN, "image_index": image_index,
                           "image_id": image_id, "driver_submit_seq": ordinal}
            host_event("LAYER_SOURCE", "next_drawable_begin", base + 5,
                       call_begin_mach_ns=base + 5, **next_common)
            host_event("LAYER_SOURCE", "next_drawable_end", base + 10,
                       **next_common, drawable_id=drawable_id,
                       drawable_identity=drawable_identity, texture_id=texture_id,
                       texture_pixel_format=80, call_begin_mach_ns=base + 5,
                       call_end_mach_ns=base + 10, duration_ns=5, detail="acquired")

            present_start = base + 13
            for origin in ("WINE_SOURCE", "MVK_BOUNDARY"):
                host_event(origin, "vkQueuePresentKHR_begin", present_start,
                           wine_call_seq=ordinal, present_seq=ordinal,
                           queue_id=QUEUE, call_begin_mach_ns=present_start,
                           **host_common, acquisition_sequence=ordinal,
                           driver_submit_seq=ordinal)
                host_event(origin, "vkQueuePresentKHR_end", present_start + 1,
                           wine_call_seq=ordinal, present_seq=ordinal,
                           **({"driver_present_seq": ordinal} if origin == "MVK_BOUNDARY" else {}),
                           queue_id=QUEUE,
                           **host_common, acquisition_sequence=ordinal,
                           driver_submit_seq=ordinal, vk_result=0,
                           call_begin_mach_ns=present_start,
                           call_end_mach_ns=present_start + 1, duration_ns=1)

            lifecycle = {
                "acquisition_sequence": ordinal, "swapchain_id": SWAPCHAIN,
                "surface_id": SURFACE, "image_index": image_index,
                "image_id": image_id, "driver_submit_seq": ordinal,
                "driver_present_seq": ordinal, "present_queue_id": QUEUE,
                "present_swapchain_id": SWAPCHAIN,
                "present_image_index": image_index,
                "present_image_id": image_id, "drawable_id": drawable_id,
                "drawable_identity": drawable_identity, "texture_id": texture_id,
            }
            host_event("LAYER_SOURCE", "present_drawable_join", present_start,
                       present_seq=ordinal, acquisition_sequence=ordinal,
                       submit_sequence=ordinal, surface_id=SURFACE,
                       swapchain_id=SWAPCHAIN, image_index=image_index,
                       image_id=image_id, drawable_id=drawable_id,
                       texture_id=texture_id, texture_pixel_format=80,
                       matched=True, swapchain_count=1, queue_id=QUEUE,
                       call_begin_mach_ns=present_start)
            host_event("LAYER_SOURCE", "presented_handler_registered", base + 15,
                       **lifecycle)
            presented_time = base + 15_000_000
            host_event("LAYER_SOURCE", "presented_callback", base + 16_000_000,
                       **lifecycle, presented_time_mach_ns=presented_time,
                       presentation_clock_domain="mach_absolute_time_ns",
                       presented_time_basis="mach_absolute_time_seconds",
                       presented_time_conversion="floor(seconds*1e9)",
                       callback_delivery_clock_domain="mach_absolute_time_ns",
                       is_displayed=True)
            host_event("LAYER_SOURCE", "availability_after_presented_handler", base + 16_000_001,
                       **lifecycle)
            host_event("LAYER_SOURCE", "drawable_release", base + 16_000_002,
                       **lifecycle)

        runtime_identities = {
            role: dict(identity)
            for role, identity in self.validator.expected_startup_identities.items()
        }
        runtime_identities["winevulkan_dll"]["load_kind"] = "builtin"
        runtime_identities["wine_launcher"]["load_kind"] = "prefixlease-launcher"
        runtime_identities["moltenvk"]["version"] = "MoltenVK v1.4.2"
        runtime_identities["app"] = {"path": APP, "sha256": "9" * 64}
        loader_evidence = {
            "winevulkan_dll_loaded": True, "winevulkan_so_loaded": True,
            "winevulkan_dll_reported_path": "C:\\windows\\system32\\winevulkan.dll",
            "moltenvk_loaded_path": MOLTENVK,
            "moltenvk_sha256": MODULE_HASHES["moltenvk"],
            "moltenvk_version": "MoltenVK v1.4.2",
            "moltenvk_runtime_banners": [{"moltenvk_version": "1.4.2",
                                           "vulkan_version": "1.4.357"}],
            "trace_proxy_loaded_path": TRACE_PROXY,
            "trace_proxy_sha256": MODULE_HASHES["trace_proxy"],
            "wine_host_loader_path": HOST_LOADER,
            "wine_host_loader_sha256": MODULE_HASHES["wine_host_loader"],
            "wine_host_loader_role": "win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib",
            "khronos_loader_loaded": False,
            "runtime_identities": runtime_identities,
            "forbidden_module_names": [],
        }
        return app, host, loader_evidence

    def test_complete_three_frame_lifecycle_is_quantitative_ready(self):
        app, host, loader = self._valid_traces()
        report = self.validator.validate(app, host, loader_evidence=loader)
        self.assertTrue(report["is_valid"])
        self.assertTrue(report["quantitative_ready"])
        self.assertEqual(report["frame_count"], 3)
        self.assertEqual(report["next_drawable"]["count"], 3)

    def test_positive_wrong_drawable_size_fails_closed(self):
        app, host, loader = self._valid_traces()
        layer = next(row for row in host if row.get("event") == "layer_configuration")
        layer["drawable_width"] = 1280.0
        with self.assertRaisesRegex(ValidationError, "fixed 640x360"):
            self.validator.validate(app, host, loader_evidence=loader)

    def _split_layer_traces(self):
        app, host, loader = self._valid_traces()
        early = next(row for row in host if row.get("event") == "layer_configuration")
        configured = dict(early)
        configured["event"] = "layer_presentation_configuration"
        configured["event_id"] = max(row["event_id"] for row in host) + 1
        configured["ts_mach_ns"] = max(row["ts_mach_ns"] for row in host) + 1
        early["observation_stage"] = "surface_identity_before_swapchain"
        early["drawable_width"] = early["drawable_height"] = 0.0
        host.append(configured)
        return app, host, loader, early, configured

    def test_split_identity_zero_extent_and_late_configuration_pass(self):
        app, host, loader, _, _ = self._split_layer_traces()
        self.assertTrue(self.validator.validate(app, host, loader_evidence=loader)["is_valid"])

    def test_split_configuration_missing_mismatch_duplicate_fail(self):
        for mutation in ("missing", "duplicate", "identity", "extent"):
            app, host, loader, _, configured = self._split_layer_traces()
            if mutation == "missing": host.remove(configured)
            elif mutation == "duplicate": host.append(dict(configured))
            elif mutation == "identity": configured["layer_id"] += 1
            else: configured["drawable_width"] = 0.0
            with self.subTest(mutation=mutation), self.assertRaises(ValidationError):
                self.validator.validate(app, host, loader_evidence=loader)

    def test_missing_app_configuration_fails_closed(self):
        app, host, loader = self._valid_traces()
        app[:] = [row for row in app if row.get("origin") != "APP_CONFIG"]
        with self.assertRaisesRegex(ValidationError, "app configuration: expected one row"):
            self.validator.validate(app, host, loader_evidence=loader)

    def test_pre_frame_runtime_identity_gate_is_mandatory(self):
        for field in ("host_runtime_startup_gate_passed", "winevulkan_dll_module_loaded",
                      "winevulkan_dll_before_frame_loop"):
            app, host, loader = self._valid_traces()
            app[0][field] = False
            with self.subTest(field=field), self.assertRaisesRegex(
                    ValidationError, "app window/swapchain configuration mismatch"):
                self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        app[0]["winevulkan_dll_module_path"] = "C:\\windows\\system32\\vulkan-1.dll"
        with self.assertRaisesRegex(ValidationError, "pre-frame app identity"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        loader["winevulkan_dll_reported_path"] = "C:\\windows\\system32\\winevulkan32.dll"
        with self.assertRaisesRegex(ValidationError, "differs from the Wine loader record"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        app[0]["winevulkan_dll_host_path"] = "/wrong/prefix/drive_c/windows/system32/winevulkan.dll"
        with self.assertRaisesRegex(ValidationError, "not bound to the exact PrefixLease"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        app[0]["winevulkan_dll_sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "not bound to the exact PrefixLease"):
            self.validator.validate(app, host, loader_evidence=loader)

    def test_qpc_timestamp_conversion_must_match_the_declared_floor_contract(self):
        app, host, loader = self._valid_traces()
        app[1]["ts_qpc_ns"] += 1
        with self.assertRaisesRegex(ValidationError, "QPC timestamp conversion is inconsistent"):
            self.validator.validate(app, host, loader_evidence=loader)

    def test_app_or_host_trace_drops_fail_closed(self):
        app, host, loader = self._valid_traces()
        app[0]["trace_events_dropped"] = 1
        with self.assertRaisesRegex(ValidationError, "app trace dropped"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        summary = next(row for row in host if row["event"] == "trace_summary")
        summary["dropped_count"] = 1
        with self.assertRaisesRegex(ValidationError, "host asynchronous trace ring"):
            self.validator.validate(app, host, loader_evidence=loader)

    def test_startup_identity_and_clock_metadata_are_mandatory(self):
        app, host, loader = self._valid_traces()
        host[:] = [row for row in host if row.get("event") != "host_runtime_startup"]
        with self.assertRaisesRegex(ValidationError, "host runtime startup identity"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        host[:] = [row for row in host if row.get("event") != "clock_metadata"]
        with self.assertRaisesRegex(ValidationError, "clock metadata"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        next(row for row in app if row.get("origin") == "APP_SOURCE").pop("ts_clock_domain")
        with self.assertRaisesRegex(ValidationError, "QueryPerformanceCounter timestamp clock domain"):
            self.validator.validate(app, host, loader_evidence=loader)

    def test_rosetta_and_exact_runtime_module_hashes_are_mandatory(self):
        app, host, loader = self._valid_traces()
        next(row for row in host if row.get("event") == "process_identity")["process_translated"] = 0
        with self.assertRaisesRegex(ValidationError, "not x86_64 under Rosetta"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        module = next(row for row in host if row.get("role") == "winevulkan_so")
        module["sha256"] = "f" * 64
        with self.assertRaisesRegex(ValidationError, "winevulkan_so path or SHA-256"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        module = next(row for row in host if row.get("role") == "wine_host_loader")
        module["path"] = "/wrong/libvulkan.dylib"
        with self.assertRaisesRegex(ValidationError, "wine_host_loader path or SHA-256"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        module = next(row for row in host if row.get("role") == "winevulkan_dll")
        module["loaded_in_process"] = False
        with self.assertRaisesRegex(ValidationError, "winevulkan_dll load state or identity kind"):
            self.validator.validate(app, host, loader_evidence=loader)

    def test_wrong_moltenvk_version_or_duplicate_candidate_fails(self):
        app, host, loader = self._valid_traces()
        next(row for row in host if row.get("event") == "host_runtime_startup")["moltenvk_version"] = "MoltenVK v1.4.3"
        with self.assertRaisesRegex(ValidationError, "startup identity block"):
            self.validator.validate(app, host, loader_evidence=loader)

        app, host, loader = self._valid_traces()
        duplicate = copy.deepcopy(next(row for row in host if row.get("role") == "winevulkan_so"))
        duplicate["event_id"] = max(row["event_id"] for row in host) + 1
        host.append(duplicate)
        with self.assertRaisesRegex(ValidationError, "duplicate startup module identity"):
            self.validator.validate(app, host, loader_evidence=loader)

    def test_missing_host_trace_and_changed_loader_evidence_fail_closed(self):
        app, host, loader = self._valid_traces()
        with self.assertRaisesRegex(ValidationError, "clock metadata"):
            self.validator.validate(app, [], loader_evidence=loader)

        app, host, loader = self._valid_traces()
        loader["moltenvk_runtime_banners"][0]["moltenvk_version"] = "1.4.3"
        with self.assertRaisesRegex(ValidationError, "runtime loader path, MoltenVK version"):
            self.validator.validate(app, host, loader_evidence=loader)

    def test_duplicate_callback_for_one_drawable_is_ambiguous(self):
        app, host, loader = self._valid_traces()
        callback = next(row for row in host if row["event"] == "presented_callback")
        duplicate = copy.deepcopy(callback)
        duplicate["event_id"] = max(row["event_id"] for row in host) + 1
        duplicate["ts_mach_ns"] += 1
        host.append(duplicate)
        with self.assertRaisesRegex(ValidationError, "duplicate present callback identity"):
            self.validator.validate(app, host, loader_evidence=loader)

    def test_multiple_successful_drawables_for_one_app_frame_are_ambiguous(self):
        app, host, loader = self._valid_traces()
        first = next(row for row in host if row["event"] == "next_drawable_end")
        begin = next(row for row in host if row["event"] == "next_drawable_begin"
                     and row["next_drawable_sequence"] == first["next_drawable_sequence"])
        sequence = 4
        duplicate_begin = copy.deepcopy(begin)
        duplicate_end = copy.deepcopy(first)
        duplicate_begin.update(event_id=max(row["event_id"] for row in host) + 1,
                               next_drawable_sequence=sequence)
        duplicate_end.update(event_id=max(row["event_id"] for row in host) + 2,
                             next_drawable_sequence=sequence,
                             drawable_id=99_999, drawable_identity=98_888,
                             texture_id=97_777)
        host.extend((duplicate_begin, duplicate_end))
        with self.assertRaisesRegex(ValidationError, "multiple valid drawable attempts"):
            self.validator.validate(app, host, loader_evidence=loader)


class BareWineTraceSourceContractTests(unittest.TestCase):
    def test_proxy_startup_roles_match_validator_contract(self):
        source = (Path(__file__).resolve().parents[1]
                  / "dxvk_macos_step11d3b" / "trace_loader.mm").read_text(encoding="utf-8")
        emitted_roles = set(re.findall(r'emit_startup_module\("([^"]+)"', source))
        self.assertEqual(
            emitted_roles,
            {"wine", "wine_launcher", "winevulkan_dll", "winevulkan_so",
             "wine_host_loader", "winemac_so", "trace_proxy", "moltenvk", "wineserver"},
        )
        self.assertIn("layer_is_cametal_layer", source)

    def test_app_requires_runtime_identity_before_device_and_swapchain(self):
        source = (Path(__file__).resolve().parents[1]
                  / "dxvk_macos_step11d3b" / "bare_vulkan_app.cpp").read_text(encoding="utf-8")
        win_main = source[source.index("int WINAPI WinMain"):]
        self.assertIn("GetModuleHandleA(\"winevulkan.dll\")", source)
        self.assertIn("FG_B_WINEVULKAN_DLL_PATH", source)
        self.assertIn("FG_B_WINEVULKAN_DLL_SHA256", source)
        self.assertIn("WINEPREFIX", source)
        self.assertIn("win32-hmodule-proof-in-paired-app-trace", source)
        self.assertIn("loaded_in_process\\\":true", source)
        self.assertLess(win_main.index("WaitForHostRuntimeStartupIdentity()"),
                        win_main.index("SelectPhysicalDeviceAndCreateLogicalDevice()"))
        self.assertLess(win_main.index("WaitForHostRuntimeStartupIdentity()"),
                        win_main.index("CreateSwapchainAndRenderResources()"))

    def test_app_qpc_nanoseconds_use_the_declared_floor_conversion(self):
        source = (Path(__file__).resolve().parents[1]
                  / "dxvk_macos_step11d3b" / "bare_vulkan_app.cpp").read_text(encoding="utf-8")
        helper = source[source.index("uint64_t QpcTicksToNs"):
                        source.index("const char *EventName")]
        self.assertIn("static_cast<unsigned __int128>(ticks) * 1000000000ULL", helper)
        self.assertIn("scaled_ticks / gConfig.qpcFrequency", helper)
        self.assertNotIn("long double", helper)


if __name__ == "__main__":
    unittest.main()
