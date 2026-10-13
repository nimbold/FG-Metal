#!/usr/bin/env python3
"""Tests that the native graphics runner gates PASS on offline trace validation."""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from experiments.dxvk_macos_step11d3a.run_native_control import (
    dyld_moltenvk_images,
    run_parameters,
    validate_loader_probe,
    validate_graphics_trace_events,
    verify_child_environment,
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

    def test_occlusion_mode_uses_bounded_eighty_second_loop(self):
        self.assertEqual(run_parameters("occlusion"), (80, 180, 512 * 1024 * 1024))

    def test_existing_native_run_durations_remain_unchanged(self):
        self.assertEqual(run_parameters("probe"), (0, 60, 32 * 1024 * 1024))
        self.assertEqual(run_parameters("smoke"), (30, 120, 512 * 1024 * 1024))
        self.assertEqual(run_parameters("baseline"), (300, 420, 512 * 1024 * 1024))


class TestNativeControlRuntimeEvidence(unittest.TestCase):
    def test_child_environment_evidence_hashes_only_the_sealed_allowlist(self):
        ambient = {
            "DYLD_INSERT_LIBRARIES": "/private/secret/ambient-injection.dylib",
            "DYLD_LIBRARY_PATH": "/private/secret/ambient-library-path",
            "API_TOKEN": "private-ambient-token",
        }
        with tempfile.TemporaryDirectory() as temporary:
            dylib_directory = Path(temporary) / "cas"
            dylib_directory.mkdir()
            sealed = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "en_US.UTF-8",
                "HOME": "/private/tmp/native-run-scratch",
                "TMPDIR": "/private/tmp/native-run-scratch",
                "DYLD_LIBRARY_PATH": f"{dylib_directory.resolve(strict=True)}:/usr/lib",
                "DYLD_PRINT_LIBRARIES": "1",
            }

            evidence = verify_child_environment(
                sealed, expected_dylib_directory=dylib_directory, ambient_environment=ambient)
            serialized = json.dumps(evidence, sort_keys=True)
            expected_hash = hashlib.sha256(
                json.dumps(sealed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            ).hexdigest()

            self.assertEqual(evidence["sealed_child_environment"], sealed)
            self.assertEqual(evidence["sealed_child_environment_sha256"], expected_hash)
            self.assertEqual(evidence["ambient_dyld_key_names_in_supervisor"], [
                "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH",
            ])
            self.assertEqual(evidence["sealed_child_dyld_key_names"], [
                "DYLD_LIBRARY_PATH", "DYLD_PRINT_LIBRARIES",
            ])
            self.assertEqual(evidence["ambient_dyld_injection_key_names_in_child"], [])
            self.assertEqual(evidence["unexpected_child_dyld_key_names"], [])
            self.assertNotIn("ambient-injection.dylib", serialized)
            self.assertNotIn("ambient-library-path", serialized)
            self.assertNotIn("private-ambient-token", serialized)

    def test_child_environment_rejects_an_actual_unapproved_dyld_key(self):
        sealed = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8",
            "HOME": "/private/tmp/native-run-scratch",
            "TMPDIR": "/private/tmp/native-run-scratch",
            "DYLD_LIBRARY_PATH": "/private/cache/artifacts/sha256/abc/lib:/usr/lib",
            "DYLD_PRINT_LIBRARIES": "1",
            "DYLD_INSERT_LIBRARIES": "/private/secret/injected.dylib",
        }
        with self.assertRaisesRegex(RuntimeError, "evidence allowlist"):
            verify_child_environment(
                sealed, expected_dylib_directory=Path(tempfile.gettempdir()), ambient_environment={})

    def test_child_environment_rejects_extra_dyld_search_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            dylib_directory = Path(temporary) / "cas"
            dylib_directory.mkdir()
            sealed = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                "LANG": "en_US.UTF-8",
                "HOME": "/private/tmp/native-run-scratch",
                "TMPDIR": "/private/tmp/native-run-scratch",
                "DYLD_LIBRARY_PATH": f"{dylib_directory.resolve(strict=True)}:/private/tmp/alternate:/usr/lib",
                "DYLD_PRINT_LIBRARIES": "1",
            }
            with self.assertRaisesRegex(RuntimeError, "exact candidate and system library roots"):
                verify_child_environment(sealed, expected_dylib_directory=dylib_directory,
                                         ambient_environment={})

    def test_loader_probe_accepts_only_the_exact_dyld_reported_cas_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            dylib = Path(temporary) / "libMoltenVK.dylib"
            dylib.write_bytes(b"synthetic exact CAS image")
            canonical_dylib = dylib.resolve(strict=True)
            image_uuid = "C178CB05-A1CA-34F9-8ADD-7671DC15A86B"
            stdout = (
                f"LOADER_PROBE=PASS symbol=vkCreateInstance path={canonical_dylib} "
                "compiled_arch=x86_64 process_translated=1\n"
            )
            stderr = f"dyld[1234]: <{image_uuid}> {canonical_dylib}\n"

            result = validate_loader_probe(stdout, stderr, dylib_path=canonical_dylib)

            self.assertEqual(result["status"], "PASS")
            self.assertEqual(result["loaded_moltenvk_path"], str(canonical_dylib))
            self.assertEqual(result["compiled_architecture"], "x86_64")
            self.assertEqual(result["process_translated"], 1)
            self.assertEqual(
                result["dyld_printed_moltenvk_images"]["unique_resolved_paths"], [str(canonical_dylib)])
            self.assertEqual(
                result["dyld_printed_moltenvk_images"]["images"][0]["reported_uuid"], image_uuid)

    def test_loader_probe_rejects_any_alternate_moltenvk_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dylib = root / "cas" / "libMoltenVK.dylib"
            alternate = root / "system" / "libMoltenVK.1.dylib"
            dylib.parent.mkdir()
            alternate.parent.mkdir()
            dylib.write_bytes(b"exact CAS image")
            alternate.write_bytes(b"alternate image")
            stdout = (
                f"LOADER_PROBE=PASS symbol=vkCreateInstance path={dylib} "
                "compiled_arch=x86_64 process_translated=1\n"
            )
            stderr = f"dyld[1234]: {dylib}\ndyld[1234]: {alternate}\n"

            with self.assertRaisesRegex(RuntimeError, "different MoltenVK path than the manifest"):
                validate_loader_probe(stdout, stderr, dylib_path=dylib)

    def test_loader_probe_rejects_a_symlink_alias_to_the_cas_image(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            dylib = root / "cas" / "libMoltenVK.dylib"
            alias = root / "alias" / "libMoltenVK.dylib"
            dylib.parent.mkdir()
            alias.parent.mkdir()
            dylib.write_bytes(b"exact CAS image")
            alias.symlink_to(dylib)
            stdout = (
                f"LOADER_PROBE=PASS symbol=vkCreateInstance path={dylib} "
                "compiled_arch=x86_64 process_translated=1\n"
            )
            stderr = f"dyld[1234]: {alias}\n"

            with self.assertRaisesRegex(RuntimeError, "different MoltenVK path than the manifest"):
                validate_loader_probe(stdout, stderr, dylib_path=dylib)

    def test_loader_probe_requires_a_dyld_moltenvk_image_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            dylib = Path(temporary) / "libMoltenVK.dylib"
            dylib.write_bytes(b"synthetic exact CAS image")
            stdout = (
                f"LOADER_PROBE=PASS symbol=vkCreateInstance path={dylib} "
                "compiled_arch=x86_64 process_translated=1\n"
            )
            with self.assertRaisesRegex(RuntimeError, "did not report a MoltenVK image"):
                validate_loader_probe(stdout, "dyld[1234]: /usr/lib/libSystem.B.dylib\n", dylib_path=dylib)

    def test_loader_probe_requires_rosetta_translation(self):
        with tempfile.TemporaryDirectory() as temporary:
            dylib = Path(temporary) / "libMoltenVK.dylib"
            dylib.write_bytes(b"synthetic exact CAS image")
            stdout = (
                f"LOADER_PROBE=PASS symbol=vkCreateInstance path={dylib} "
                "compiled_arch=x86_64 process_translated=0\n"
            )
            with self.assertRaisesRegex(RuntimeError, "architecture/translation"):
                validate_loader_probe(stdout, f"dyld[1234]: {dylib}\n", dylib_path=dylib)

    def test_dyld_parser_rejects_non_dyld_moltenvk_mentions(self):
        with tempfile.TemporaryDirectory() as temporary:
            dylib = Path(temporary) / "libMoltenVK.dylib"
            dylib.write_bytes(b"synthetic exact CAS image")
            with self.assertRaisesRegex(RuntimeError, "not a DYLD_PRINT_LIBRARIES record"):
                dyld_moltenvk_images(f"loader diagnostic mentions {dylib}\n", dylib)


if __name__ == "__main__":
    unittest.main()
