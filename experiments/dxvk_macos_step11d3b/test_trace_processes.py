#!/usr/bin/env python3
"""Offline process-ownership matrix for the STEP 11D.3-B host trace index."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from experiments.dxvk_macos_step11d3b.trace_processes import (
    TraceProcessError,
    build_trace_index,
    validate_target_association,
)


TOKEN = "a" * 32
TARGET_PID = 100
HELPER_PID = 200
MODULE_ROLES = (
    "wine", "wine_launcher", "wineserver", "winevulkan_dll", "winevulkan_so",
    "wine_host_loader", "winemac_so", "trace_proxy", "moltenvk",
)


def _record(file_pid: int, target_pid: int, token: str, event: str, origin: str,
            event_id: int, **fields) -> dict:
    return {
        "origin": origin,
        "event_id": event_id,
        "event": event,
        "host_pid": file_pid,
        "target_unix_pid": target_pid,
        "experiment_token": token,
        "trace_role": "target" if file_pid == target_pid else "helper",
        **fields,
    }


def _target_rows(pid: int = TARGET_PID, *, token: str = TOKEN,
                 moltenvk_version: str = "MoltenVK v1.4.2",
                 include_summary: bool = True, stamp: int = 10) -> list[dict]:
    rows: list[dict] = []

    def add(event: str, origin: str, **fields) -> dict:
        row = _record(pid, pid, token, event, origin, len(rows) + 1, **fields)
        rows.append(row)
        return row

    add("clock_metadata", "INSTRUMENTATION",
        event_clock_source="mach_absolute_time", event_clock_domain="mach_absolute_time_ns")
    startup_id = f"pid-{pid}"
    add("process_identity", "INSTRUMENTATION", startup_id=startup_id,
        pid=pid, process_architecture="x86_64", process_translated=1,
        main_executable_path="/wine", main_executable_sha256="1" * 64,
        wine_launcher_path="/wine-launcher", wine_launcher_sha256="2" * 64,
        process_start_monotonic_ns=1)
    add("vulkan_proxy_identity", "INSTRUMENTATION", startup_id=startup_id,
        trace_proxy_path="/run/trace-loader/libMoltenVK.dylib",
        trace_proxy_sha256="3" * 64,
        loaded_proxy_path="/run/trace-loader/libMoltenVK.dylib",
        loaded_proxy_sha256="3" * 64,
        loaded_delegate_path="/run/mvk/libMoltenVK.1.dylib",
        loaded_delegate_sha256="4" * 64)
    for role in MODULE_ROLES:
        add("startup_module_identity", "INSTRUMENTATION", startup_id=startup_id,
            role=role, path=f"/pinned/{role}", sha256="5" * 64,
            identity_kind="dyld-loaded-image", loaded_in_process=True,
            observed_path=f"/pinned/{role}", observed_sha256="5" * 64,
            identity_match=True, startup_phase=True)
    add("host_runtime_startup", "INSTRUMENTATION", startup_id=startup_id,
        startup_valid=True, process_architecture="x86_64", process_translated=1,
        moltenvk_path="/run/mvk/libMoltenVK.1.dylib", moltenvk_sha256="4" * 64,
        moltenvk_version=moltenvk_version,
        vulkan_runtime_version=("1.4.357" if moltenvk_version == "MoltenVK v1.4.2" else "1.4.400"),
        host_loader_role="win32u.so::_vulkan_init_once direct dlopen libMoltenVK.dylib; no Khronos libvulkan",
        loaded_moltenvk_count=1, loaded_proxy_count=1, loaded_winevulkan_count=1,
        loaded_host_loader_count=1, loaded_libvulkan_dylib_count=0)
    add("vkCreateMetalSurfaceEXT_end", "MVK_BOUNDARY", ts_mach_ns=stamp,
        vk_result=0, surface_id=71, layer_id=72)
    add("vkAcquireNextImageKHR_end", "WINE_SOURCE", ts_mach_ns=stamp + 1,
        vk_result=0, device_id=81, swapchain_id=91, image_id=101)
    add("vkQueueSubmit2_end", "WINE_SOURCE", ts_mach_ns=stamp + 2,
        vk_result=0, queue_id=82, device_id=81, swapchain_id=91)
    add("vkQueuePresentKHR_end", "MVK_BOUNDARY", ts_mach_ns=stamp + 3,
        vk_result=0, queue_id=82, swapchain_id=91, surface_id=71)
    add("next_drawable_end", "LAYER_SOURCE", ts_mach_ns=stamp + 4,
        drawable_id=111, texture_id=112, swapchain_id=91)
    add("layer_configuration", "LAYER_SOURCE", ts_mach_ns=stamp + 5,
        layer_id=72, layer_is_cametal_layer=True)
    if include_summary:
        add("trace_summary", "LOGGER", ts_mach_ns=stamp + 6,
            dropped_count=0, write_failure_count=0, ring_capacity=32768)
    return rows


def _helper_rows(pid: int, *, target_pid: int = TARGET_PID, token: str = TOKEN,
                 version: str = "1.4.2", include_summary: bool = True,
                 stamp: int = 10) -> list[dict]:
    rows = [
        _record(pid, target_pid, token, "clock_metadata", "INSTRUMENTATION", 1,
                event_clock_source="mach_absolute_time", event_clock_domain="mach_absolute_time_ns"),
        _record(pid, target_pid, token, "helper_startup_skipped", "INSTRUMENTATION", 2,
                startup_authorization=False),
        _record(pid, target_pid, token, "helper_delegate_identity", "INSTRUMENTATION", 3,
                startup_authorization=False,
                moltenvk_path="/run/mvk/libMoltenVK.1.dylib", moltenvk_sha256="4" * 64,
                loaded_delegate_path="/run/mvk/libMoltenVK.1.dylib",
                loaded_delegate_sha256="4" * 64,
                moltenvk_version=f"MoltenVK v{version}",
                moltenvk_runtime_version=version,
                vulkan_runtime_version="1.4.357" if version == "1.4.2" else "1.4.400",
                identity_match=True),
        _record(pid, target_pid, token, "vkQueuePresentKHR_end", "MVK_BOUNDARY", 4,
                ts_mach_ns=stamp, vk_result=0, queue_id=2, swapchain_id=3),
    ]
    if include_summary:
        rows.append(_record(pid, target_pid, token, "trace_summary", "LOGGER", 5,
                            ts_mach_ns=stamp + 1, dropped_count=0,
                            write_failure_count=0, ring_capacity=32768))
    return rows


class TraceProcessMatrixTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.run_dir = Path(self.temp.name).resolve(strict=True)
        self.trace_dir = self.run_dir / "host-traces"
        self.trace_dir.mkdir(mode=0o700)
        os.chmod(self.trace_dir, 0o700)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write(self, pid: int, rows: list[dict], *, name: str | None = None) -> Path:
        path = self.trace_dir / (name or f"host-trace.{pid}.jsonl")
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        os.chmod(path, 0o600)
        return path

    def _config(self, index: dict) -> dict:
        return {
            "origin": "APP_CONFIG", "event": "app_configuration",
            "process_id": 501, "target_unix_pid": TARGET_PID,
            "experiment_token": TOKEN,
            "host_trace_path": index["target_trace_windows_path"],
            "device_id": 81, "queue_id": 82, "swapchain_id": 91,
            "app_frames_started": 1, "app_submits": 1,
            "app_present_requests": 1,
        }

    def _index_pass(self) -> dict:
        index = build_trace_index(self.run_dir, TARGET_PID, TOKEN)
        self.assertEqual(index["status"], "PASS")
        return index

    def _index_fail(self) -> dict:
        with self.assertRaises(TraceProcessError):
            build_trace_index(self.run_dir, TARGET_PID, TOKEN)
        path = self.run_dir / "trace-index.json"
        self.assertTrue(path.is_file())
        self.assertFalse(path.is_symlink())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        index = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(index["status"], "FAIL")
        self.assertTrue(index["errors"])
        return index

    def _with_target(self, *, include_summary: bool = True,
                     version: str = "MoltenVK v1.4.2", stamp: int = 10) -> None:
        self._write(TARGET_PID, _target_rows(include_summary=include_summary,
                                            moltenvk_version=version, stamp=stamp))

    def test_A_helper_starts_first_target_second(self):
        self._write(HELPER_PID, _helper_rows(HELPER_PID, stamp=1))
        self._with_target(stamp=2)
        index = self._index_pass()
        self.assertEqual(Path(index["target_trace_path"]).name, "host-trace.100.jsonl")
        validate_target_association([self._config(index)], index)

    def test_B_target_starts_first_helper_second(self):
        self._with_target(stamp=1)
        self._write(HELPER_PID, _helper_rows(HELPER_PID, stamp=2))
        index = self._index_pass()
        validate_target_association([self._config(index)], index)

    def test_C_two_helpers_and_one_target(self):
        self._write(HELPER_PID, _helper_rows(HELPER_PID, stamp=1))
        self._write(HELPER_PID + 1, _helper_rows(HELPER_PID + 1, stamp=2))
        self._with_target(stamp=3)
        index = self._index_pass()
        self.assertEqual([row["trace_role"] for row in index["processes"]],
                         ["target", "helper", "helper"])
        validate_target_association([self._config(index)], index)

    def test_D_helper_exits_before_target_without_summary(self):
        self._write(HELPER_PID, _helper_rows(HELPER_PID, include_summary=False, stamp=1))
        self._with_target(stamp=2)
        index = self._index_pass()
        helper = next(row for row in index["processes"] if row["host_pid"] == HELPER_PID)
        self.assertEqual(helper["summary"]["state"], "missing")

    def test_E_target_exits_before_helper(self):
        self._with_target(stamp=1)
        self._write(HELPER_PID, _helper_rows(HELPER_PID, stamp=2))
        index = self._index_pass()
        validate_target_association([self._config(index)], index)

    def test_F_simultaneous_proxy_initialization(self):
        self._write(HELPER_PID, _helper_rows(HELPER_PID, stamp=10))
        self._with_target(stamp=10)
        index = self._index_pass()
        self.assertEqual(index["target_unix_pid"], TARGET_PID)
        validate_target_association([self._config(index)], index)

    def test_G_stale_process_trace_exists(self):
        self._write(HELPER_PID, _helper_rows(HELPER_PID, token="b" * 32))
        self._with_target()
        index = self._index_fail()
        self.assertTrue(any("conflicting ownership" in error for error in index["errors"]))

    def test_H_duplicate_PID_named_simulation(self):
        self._with_target()
        self._write(TARGET_PID, _target_rows(), name="host-trace.0100.jsonl")
        index = self._index_fail()
        self.assertTrue(any("noncanonical entry" in error for error in index["errors"]))

    def test_I_target_missing_shutdown_summary(self):
        self._with_target(include_summary=False)
        index = self._index_fail()
        self.assertEqual(index["target_summary"]["state"], "missing")
        self.assertTrue(any("exactly one trace_summary" in error for error in index["errors"]))

    def test_J_malformed_helper(self):
        self._write(HELPER_PID, _helper_rows(HELPER_PID))
        malformed = self.trace_dir / f"host-trace.{HELPER_PID + 1}.jsonl"
        malformed.write_text('{"event":"broken"\n', encoding="utf-8")
        os.chmod(malformed, 0o600)
        self._with_target()
        index = self._index_fail()
        self.assertTrue(any("malformed JSON" in error for error in index["errors"]))

    def test_K_helper_different_MoltenVK_version(self):
        self._write(HELPER_PID, _helper_rows(HELPER_PID, version="1.4.3"))
        self._with_target()
        index = self._index_fail()
        self.assertTrue(any("helper PID 200 delegate identity" in error
                            for error in index["errors"]))

    def test_L_target_correct_MoltenVK(self):
        self._with_target(version="MoltenVK v1.4.2")
        index = self._index_pass()
        self.assertEqual(index["target_host"]["moltenvk_version"], "MoltenVK v1.4.2")
        result = validate_target_association([self._config(index)], index)
        self.assertEqual(result["status"], "PASS")

    def test_M_helper_correct_MoltenVK_target_wrong(self):
        self._write(HELPER_PID, _helper_rows(HELPER_PID, version="1.4.2"))
        self._with_target(version="MoltenVK v1.4.3")
        index = self._index_fail()
        self.assertTrue(any("target PID 100 runtime startup" in error
                            for error in index["errors"]))

    def test_N_target_absent(self):
        self._write(HELPER_PID, _helper_rows(HELPER_PID))
        index = self._index_fail()
        self.assertTrue(any("target host trace" in error for error in index["errors"]))

    def test_private_trace_rejects_symlink_and_wrong_mode(self):
        self._with_target()
        original = self.trace_dir / "host-trace.100.jsonl"
        original.chmod(0o644)
        self._index_fail()

    def test_concurrent_enqueue_reordering_accepts_unique_event_ids(self):
        rows = _target_rows()
        rows[6]["event_id"], rows[7]["event_id"] = rows[7]["event_id"], rows[6]["event_id"]
        self._write(TARGET_PID, rows)
        index = self._index_pass()
        validate_target_association([self._config(index)], index)

    def test_duplicate_event_ids_fail_closed(self):
        rows = _target_rows()
        rows[7]["event_id"] = rows[6]["event_id"]
        self._write(TARGET_PID, rows)
        index = self._index_fail()
        self.assertTrue(any("duplicate event_id" in error for error in index["errors"]))


if __name__ == "__main__":
    unittest.main()
