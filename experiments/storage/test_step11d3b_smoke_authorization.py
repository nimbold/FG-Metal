#!/usr/bin/env python3
"""Offline tests for the authenticated STEP 11D.3-B baseline admission gate."""
from __future__ import annotations

import hashlib
import json
import copy
import os
import re
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from experiments.dxvk_macos_step11d3b import runner_b
from experiments.dxvk_macos_step11d3b.evidence.br9r2 import (
    authorization_smoke_controller,
    coverer_sequence,
)
from experiments.dxvk_macos_step11d3b.evidence.br9r2.lifecycle_window_observer import (
    current_canonical_uncovered_streak,
)
from experiments.storage import storage_policy


SMOKE_ID = "step11d3b-smoke-20261009-001122-aabbccdd"


class SmokeAuthorizationTests(unittest.TestCase):
    def test_transient_controller_evidence_root_is_private_and_outside_measured_repo(self) -> None:
        root = Path("/private/tmp") / f"codex-br9r2-test-{uuid.uuid4().hex[:12]}"
        with patch.dict(os.environ, {"FGMETAL_BR9R_EVIDENCE_ROOT": str(root)}):
            resolved = authorization_smoke_controller._configured_evidence_root()
            coverer_root = coverer_sequence._configured_evidence_root()
            runner_root = runner_b.observer_evidence_root()
        self.assertEqual(resolved, root)
        self.assertEqual(coverer_root, root)
        self.assertEqual(runner_root, root)
        self.assertEqual(resolved.stat().st_mode & 0o777, 0o700)
        self.assertFalse(resolved.is_symlink())
        self.assertNotIn(storage_policy.REPO, resolved.parents)
        resolved.rmdir()

    def test_transient_controller_evidence_root_rejects_repo_paths_and_symlinks(self) -> None:
        with patch.dict(os.environ, {
                "FGMETAL_BR9R_EVIDENCE_ROOT": str(storage_policy.REPO / "experiments/codex-br9r2-test-12345678")}):
            with self.assertRaisesRegex(ValueError, "canonical private"):
                authorization_smoke_controller._configured_evidence_root()

        target = Path("/private/tmp") / f"codex-br9r2-target-{uuid.uuid4().hex[:12]}"
        link = Path("/private/tmp") / f"codex-br9r2-link-{uuid.uuid4().hex[:12]}"
        target.mkdir(mode=0o700)
        link.symlink_to(target, target_is_directory=True)
        try:
            with patch.dict(os.environ, {"FGMETAL_BR9R_EVIDENCE_ROOT": str(link)}):
                with self.assertRaisesRegex(ValueError, "canonical private"):
                    authorization_smoke_controller._configured_evidence_root()
        finally:
            link.unlink()
            target.rmdir()

    def test_runner_observer_paths_accept_only_configured_private_root(self) -> None:
        root = Path("/private/tmp") / f"codex-br9r2-runner-{uuid.uuid4().hex[:12]}"
        root.mkdir(mode=0o700)
        try:
            with patch.dict(os.environ, {"FGMETAL_BR9R_EVIDENCE_ROOT": str(root)}):
                self.assertEqual(runner_b.observer_evidence_root(), root)
            with patch.dict(os.environ, {
                    "FGMETAL_BR9R_EVIDENCE_ROOT": str(storage_policy.REPO / "experiments/observer")}):
                with self.assertRaisesRegex(RuntimeError, "canonical /private/tmp"):
                    runner_b.observer_evidence_root()
        finally:
            root.rmdir()

    def test_start_gate_signal_is_published_only_after_complete_write(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            signal_path = Path(temporary) / "authorization-start-signal.json"
            signal = {"status": "CANONICAL_UNCOVERED_ATTAINED", "run_id": "run-1"}
            encoded = (json.dumps(signal, sort_keys=True, separators=(",", ":")) + "\n").encode()
            real_link = authorization_smoke_controller.os.link

            def verify_complete_temp_before_publish(source, destination, *, follow_symlinks=True):
                self.assertFalse(Path(destination).exists())
                self.assertEqual(Path(source).read_bytes(), encoded)
                return real_link(source, destination, follow_symlinks=follow_symlinks)

            with patch.object(authorization_smoke_controller.os, "link",
                              side_effect=verify_complete_temp_before_publish):
                authorization_smoke_controller.write_start_gate_signal(signal_path, signal)

            self.assertEqual(json.loads(signal_path.read_text(encoding="utf-8")), signal)
            self.assertEqual(list(Path(temporary).iterdir()), [signal_path])
            with self.assertRaises(FileExistsError):
                authorization_smoke_controller.write_start_gate_signal(signal_path, signal)

    @staticmethod
    def _canonical_uncovered_samples(target_pid: int = 2201) -> list[dict]:
        samples = []
        for index in range(5):
            sample_ns = 1_000_000_000 + index * 500_000_000
            samples.append({
                "event": "sample",
                "targetKind": "wine",
                "windowTelemetryExpected": True,
                "sampledAtMonotonicNS": sample_ns,
                "canonicalUncoveredEligible": True,
                "allWindowListQuerySucceeded": True,
                "onScreenWindowListQuerySucceeded": True,
                "aboveTargetWindowDetailsComplete": True,
                "covererProcessEnumerationSucceeded": True,
                "alphaOpaqueFullCoverCandidatePresent": False,
                "neutralCovererProcessPresent": False,
                "neutralCovererWindowPresent": False,
                "neutralCovererProcesses": [],
                "neutralCovererWindows": [],
                "target": {
                    "ownerPID": target_pid,
                    "windowID": 91,
                    "rankFrontToBack": 0,
                    "rankSource": "onScreenWindowList",
                    "titleMatched": True,
                    "onScreen": True,
                    "processAlive": True,
                    "processIdentityMatches": True,
                    "windowIdentityAmbiguous": False,
                    "accessibilityMinimized": False,
                    "accessibility": {"accessibilityTrusted": True, "focused": True},
                    "displayRelationship": {
                        "uniqueExpectedDisplayMatch": True,
                        "targetFullyOnExpectedDisplay": True,
                    },
                },
                "app": {"isActive": True},
                "inProcessTelemetry": {
                    "available": True,
                    "fresh": True,
                    "windowVisible": True,
                    "windowOccluded": False,
                    "visibleBitSet": True,
                    "windowMiniaturized": False,
                    "windowKey": True,
                    "windowMain": True,
                    "appActive": True,
                    "telemetrySequence": index + 1,
                    "timestampMachNS": sample_ns - 1_000_000,
                },
            })
        return samples

    def test_start_gate_requires_a_current_stable_uncovered_streak(self) -> None:
        samples = self._canonical_uncovered_samples()
        latest = {"latestSample": samples[-1],
                  "canonicalUncovered": {"attained": True}}
        proof = current_canonical_uncovered_streak(
            samples, target_pid=2201, latest_state=latest)
        self.assertIsNotNone(proof)
        self.assertEqual(proof["stable_duration_ns"], 2_000_000_000)
        self.assertEqual(proof["sample_count"], 5)
        self.assertTrue(proof["telemetry_progress_verified"])

        samples[-1]["target"]["titleMatched"] = False
        self.assertIsNone(current_canonical_uncovered_streak(
            samples, target_pid=2201,
            latest_state={"latestSample": samples[-1],
                          "canonicalUncovered": {"attained": True}}))

    def test_start_gate_rejects_stale_latest_or_nonadvancing_telemetry(self) -> None:
        samples = self._canonical_uncovered_samples()
        stale_latest = {"latestSample": {**samples[-1], "sampledAtMonotonicNS": 9},
                        "canonicalUncovered": {"attained": True}}
        self.assertIsNone(current_canonical_uncovered_streak(
            samples, target_pid=2201, latest_state=stale_latest))

        for sample in samples:
            sample["inProcessTelemetry"]["telemetrySequence"] = 7
            sample["inProcessTelemetry"]["timestampMachNS"] = 999_000_000
        self.assertIsNone(current_canonical_uncovered_streak(
            samples, target_pid=2201,
            latest_state={"latestSample": samples[-1],
                          "canonicalUncovered": {"attained": True}}))

    def test_start_gate_requires_first_app_and_host_acquires_after_release(self) -> None:
        run_id = "step11d3b-smoke-20261011-010203-aabbccdd"
        target_pid = 2201
        token = "a" * 32
        signal_path = Path("/private/tmp/br9r2/authorization-start-signal.json")
        signal = {"released_monotonic_ns": 1_000}
        app_events = [{
            "origin": "APP_CONFIG", "event": "app_configuration",
            "run_id": run_id, "target_unix_pid": target_pid,
            "experiment_token": token,
            "public_window_start_gate_required": True,
            "public_window_start_gate_passed": True,
            "public_window_start_gate_wait_begin_qpc_ticks": 200,
            "public_window_start_gate_passed_qpc_ticks": 300,
            "public_window_start_gate_released_monotonic_ns": 1_000,
            "public_window_start_gate_path": runner_b.windows_path(signal_path),
        }, {
            "origin": "APP_SOURCE", "event": "app_vkAcquireNextImageKHR_begin",
            "call_begin_qpc_ticks": 301,
        }]
        host_events = [{
            "origin": "MVK_BOUNDARY", "event": "vkAcquireNextImageKHR_begin",
            "trace_role": "target", "host_pid": target_pid,
            "target_unix_pid": target_pid, "experiment_token": token,
            "ts_mach_ns": 1_001,
        }]
        result = runner_b.validate_start_gate_order(
            signal, experiment_id=run_id, target_pid=target_pid, token=token,
            signal_path=signal_path, app_events=app_events, host_events=host_events)
        self.assertEqual(result["status"], "PASS")

        host_events[0]["ts_mach_ns"] = 999
        result = runner_b.validate_start_gate_order(
            signal, experiment_id=run_id, target_pid=target_pid, token=token,
            signal_path=signal_path, app_events=app_events, host_events=host_events)
        self.assertEqual(result["status"], "FAIL")
        self.assertTrue(any("first target vkAcquire" in error for error in result["errors"]))

    def test_canonical_smoke_signature_requires_healthy_refresh(self) -> None:
        healthy_frames = [{"presented_time_mach_ns": 0 if i == 0 else i,
                           "is_displayed": i != 0} for i in range(60)]
        healthy = runner_b.summarize_smoke_signature({
            "quantitative_ready": True, "acquired_fps": 60.0,
            "displayed_frames": 59, "frame_count": 60,
            "app_configuration": {"actual_run_ns": 1_000_000_000},
            "frames": healthy_frames,
        })
        self.assertEqual(healthy["status"], "HEALTHY_REFRESH")
        self.assertTrue(healthy["healthy_refresh"])
        self.assertAlmostEqual(healthy["raw_zero_fraction"], 1 / 60)

        fast_zero_frames = [{"presented_time_mach_ns": 1 if i == 0 else 0,
                             "is_displayed": i == 0} for i in range(120)]
        fast_zero = runner_b.summarize_smoke_signature({
            "quantitative_ready": True, "acquired_fps": 120.0,
            "displayed_frames": 1, "frame_count": 120,
            "app_configuration": {"actual_run_ns": 1_000_000_000},
            "frames": fast_zero_frames,
        })
        self.assertEqual(fast_zero["status"], "FAST_ZERO")
        self.assertFalse(fast_zero["healthy_refresh"])

    def test_black_window_warmup_is_not_screenshot_ready(self) -> None:
        sample = {"sampled_pixels": 19728, "nonblack_ratio": 0.0585,
                  "distinct_quantized_colors": 43, "mean_rgb": [0.014, 0.014, 0.014]}
        result = runner_b.classify_screenshot_samples([sample])
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["reason"], "smoke window was black or lacked spatial color content")

    def test_one_colored_frame_is_acceptable_only_as_warmup_readiness(self) -> None:
        sample = {"sampled_pixels": 19728, "nonblack_ratio": 0.65,
                  "distinct_quantized_colors": 45, "mean_rgb": [0.16, 0.13, 0.35]}
        result = runner_b.classify_screenshot_samples([sample])
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(result["content_visible"])
        self.assertEqual(len(result["samples"]), 1)

    def test_two_colored_frames_still_require_visible_change(self) -> None:
        first = {"sampled_pixels": 100, "nonblack_ratio": 0.8,
                 "distinct_quantized_colors": 8, "mean_rgb": [0.1, 0.2, 0.3]}
        changed = {**first, "mean_rgb": [0.2, 0.3, 0.4]}
        static = {**first, "mean_rgb": [0.1, 0.2, 0.3]}
        self.assertEqual(runner_b.classify_screenshot_samples([first, changed])["status"], "PASS")
        unchanged_result = runner_b.classify_screenshot_samples([first, static])
        self.assertEqual(unchanged_result["status"], "FAIL")
        self.assertEqual(unchanged_result["reason"], "separated smoke screenshots did not change visibly")

    def _write_valid_auth(self, root: Path, bindings: dict) -> None:
        run_dir = root / SMOKE_ID
        run_dir.mkdir(parents=True)
        result_path = run_dir / "run-result.json"
        prefix = storage_policy.PREFIX_ROOT / f"{SMOKE_ID}-prefix"
        manifest_sha = "c" * 64
        identity = {"experiment_id": SMOKE_ID, "manifest_sha256": manifest_sha,
                    **{key: str(prefix) for key in (
                        "canonical_absolute_path", "launch_WINEPREFIX", "cleanup_WINEPREFIX")}}
        cleanup = storage_policy._seal_storage_evidence({
            "experiment_id": SMOKE_ID, "prefix_path": str(prefix), "status": "PASS",
            "storage_hygiene_status": "PASS", "free_space_status": "PASS",
            "prefix_deleted": True, "active_lease_marker_removed_after_receipt": True,
            "wineprefix_identity": identity,
            "wineserver_shutdown": [{"flag": flag, "returncode": 0,
                "WINEPREFIX": str(prefix), "prefix_identity": identity} for flag in ("-k", "-w")],
        }, "cleanup_receipt_hmac_sha256")
        result = {
            "experiment_id": SMOKE_ID, "phase": "smoke", "status": "PASS",
            "is_valid": True, "process_returncode": 0, "timed_out": False,
            "duration_seconds_requested": 30, "prefix_cleanup_pass": True,
            "screenshot_check_pass": True, "control_binding_snapshot": bindings,
            "gate0_post": {"gate0_status": "PASS"},
            "validation": {"is_valid": True, "quantitative_ready": True},
            "smoke_signature": {
                "status": "HEALTHY_REFRESH", "healthy_refresh": True,
                "acquired_fps": 60.0, "displayed_fps": 59.0,
                "raw_zero_fraction": 1 / 60,
            },
            "start_gate_signal": {"synthetic_offline_start_gate": True},
            "start_gate_validation": {"status": "PASS", "errors": []},
            "prefix_cleanup": cleanup, "storage_manifest_sha256": manifest_sha,
        }
        traces = run_dir / "host-traces"
        traces.mkdir(mode=0o700)
        observer_path = run_dir / "observer-result.json"
        observer_path.write_text(json.dumps({"synthetic_offline_observer": True}) + "\n")
        observer_path.chmod(0o600)
        trace_artifacts = {}
        for role, path in (("target_host_trace", traces / "host-trace.12345.jsonl"),
                           ("trace_index", run_dir / "trace-index.json"),
                           ("app_trace", run_dir / "app-trace.jsonl")):
            path.write_text('{"synthetic_offline_binding":true}\n')
            path.chmod(0o600)
            trace_artifacts[role] = {"path": str(path.resolve()),
                                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        observer_evidence = tempfile.TemporaryDirectory(
            prefix="smoke-auth-test-", suffix="-observer",
            dir=runner_b.BR9R2_EVIDENCE)
        self.addCleanup(observer_evidence.cleanup)
        observer_evidence_dir = Path(observer_evidence.name)
        for role, name in (
            ("public_window_start_signal", "authorization-start-signal.json"),
            ("public_window_start_state", "authorization-start-state-123.json"),
            ("public_window_start_streak", "authorization-start-streak-123.jsonl"),
        ):
            path = observer_evidence_dir / name
            path.write_text('{"synthetic_offline_start_gate":true}\n')
            trace_artifacts[role] = {"path": str(path.resolve()),
                                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        result["trace_artifacts"] = trace_artifacts
        result["public_window_observer"] = {
            "status": "PASS",
            "path": str(observer_path.resolve()),
            "sha256": hashlib.sha256(observer_path.read_bytes()).hexdigest(),
            "canonical_uncovered_state": {"attained": True},
        }
        result_bytes = (json.dumps(result, sort_keys=True) + "\n").encode()
        result_path.write_bytes(result_bytes)
        authorization = {
            "experiment_id": SMOKE_ID, "phase": "smoke", "status": "PASS",
            "is_valid": True, "quantitative_ready": True,
            "smoke_signature": result["smoke_signature"],
            "prefixlease_automatic_post": "PASS", "gate0_post": "PASS",
            "run_result_path": str(result_path.resolve()),
            "run_result_sha256": hashlib.sha256(result_bytes).hexdigest(),
            "bindings": bindings, "trace_artifacts": trace_artifacts,
            "start_gate_signal": result["start_gate_signal"],
            "start_gate_validation": result["start_gate_validation"],
        }
        sealed = storage_policy._seal_storage_evidence(
            authorization, runner_b.SMOKE_AUTH_FIELD)
        (run_dir / "smoke-authorization.json").write_text(
            json.dumps(sealed, sort_keys=True) + "\n", encoding="utf-8")

    def test_launcher_and_host_mapping_remain_independent(self) -> None:
        inputs = runner_b.candidate_files()
        hashes = runner_b.wine_candidate_hashes(inputs)
        binding = runner_b.wine_execution_binding(inputs)
        launcher = "946a7032484d02981182d59f82edda03f40f38c510e0208d48482c0830ef20e9"
        host = "38525a48b11217238977eff6fa1f573ffed88a8534975c9477827e886ec0ec94"
        self.assertEqual(hashes["wine_binary"], launcher)
        self.assertEqual(hashes["wine_host_image"], host)
        self.assertEqual(binding["wine_launcher_sha256"], launcher)
        self.assertEqual(binding["wine_host_image_sha256"], host)
        self.assertNotEqual(hashes["wine_binary"], hashes["wine_host_image"])
        self.assertEqual(binding["wine_launcher_path"], str(runner_b.WINE))
        self.assertEqual(binding["wine_host_image_path"], str(runner_b.WINE_HOST_EXECUTABLE))

    def test_observer_completion_uses_the_explicit_target_token(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            completion = Path(temporary) / "experiment-complete.json"
            paths = {"completion": completion}
            token = "a" * 32
            arguments = {
                "experiment_id": "step11d3b-smoke-20261011-010203-aabbccdd",
                "target_pid": 12345,
                "runner_returncode": 0,
                "experiment_token": token,
                "target_process_exit_monotonic_ns": 6_000_000_000,
            }
            runner_b.signal_observer_complete(paths, **arguments)
            self.assertEqual(json.loads(completion.read_text())["experiment_token"], token)
            self.assertEqual(json.loads(completion.read_text())[
                "target_process_exit_monotonic_ns"], 6_000_000_000)
            runner_b.signal_observer_complete(paths, **arguments)
            with self.assertRaisesRegex(RuntimeError, "different target identity"):
                runner_b.signal_observer_complete(
                    paths, **{**arguments, "experiment_token": "b" * 32})
            with self.assertRaisesRegex(RuntimeError, "requires the exact experiment token"):
                runner_b.signal_observer_complete(
                    {"completion": Path(temporary) / "other-completion.json"},
                    **{**arguments, "experiment_token": None})

    def test_smoke_observer_binds_runner_pid_and_same_pid_wine_trace_identity(self) -> None:
        run_id = "step11d3b-smoke-20261011-010203-aabbccdd"
        token = "d" * 32
        target_pid = 2201
        runner_pid = 2200
        completion_runner_pid = 2199
        app_path = str(runner_b.APP.resolve())
        app_sha = runner_b.sha256(runner_b.APP)
        process_path = str(runner_b.WINE_HOST_EXECUTABLE.resolve())
        process_sha = runner_b.sha256(runner_b.WINE_HOST_EXECUTABLE)
        process_row = {
            "origin": "INSTRUMENTATION", "event": "process_identity",
            "trace_role": "target", "host_pid": target_pid,
            "target_unix_pid": target_pid, "pid": target_pid,
            "experiment_token": token, "startup_id": f"pid-{target_pid}",
            "process_start_monotonic_ns": 123456789,
            "main_executable_path": process_path,
            "main_executable_sha256": process_sha,
        }
        module_row = {
            "origin": "INSTRUMENTATION", "event": "startup_module_identity",
            "trace_role": "target", "host_pid": target_pid,
            "target_unix_pid": target_pid, "experiment_token": token,
            "startup_id": f"pid-{target_pid}", "role": "wine",
            "identity_kind": "dyld-main-executable", "loaded_in_process": True,
            "identity_match": True, "path": process_path, "sha256": process_sha,
            "observed_path": process_path, "observed_sha256": process_sha,
        }
        source = "same-PID same-token process_identity and startup_module_identity trace"
        observer = {
            "targetRunID": run_id, "experimentToken": token, "targetPID": target_pid,
            "targetWindowID": 91,
            "targetProcessImagePath": process_path,
            "targetProcessImageSHA256": process_sha,
            "targetProcessIdentityVerified": True,
            "targetProcessImageVerificationSource": source,
            "targetProcessIdentityEvidence": {
                "source": source, "verified": True, "path": process_path,
                "sha256": process_sha, "processIdentity": process_row,
                "mainExecutableModuleIdentity": module_row,
            },
            "targetIdentityHandoff": {
                "schema_version": 2, "target_kind": "wine",
                "window_telemetry_expected": True, "run_id": run_id,
                "experiment_token": token, "target_unix_pid": target_pid,
                "runner_pid": runner_pid,
                "target_trace_path": str(
                    (runner_b.HERE / "runs" / run_id / "host-traces"
                     / f"host-trace.{target_pid}.jsonl").resolve()),
                "expected_pe_identity": {"path": app_path, "sha256": app_sha},
                "expected_target_process_identity": {
                    "role": "wine_host_image", "path": process_path,
                    "sha256": process_sha,
                },
            },
            "experimentCompleteSignal": {
                "run_id": run_id, "target_unix_pid": target_pid,
                "experiment_token": token, "status": "RUNNER_FINISHED",
                "process_runner_returncode": 0,
                "runner_pid": completion_runner_pid,
                "runner_monotonic_ns": 6_100_000_000,
                "target_process_exit_monotonic_ns": 6_000_000_000,
            },
            "endedAtMonotonicNS": 6_200_000_000,
            "canonicalUncoveredState": {
                "attained": True, "stableDurationNS": 2_000_000_000,
                "sampleCountInLongestQualifyingStreak": 5,
                "stabilityProvenMonotonicNS": 5_000_000_000,
                "telemetryProgressRequired": True,
                "telemetryProgressVerified": True,
                "telemetrySequenceAdvanceCountInLongestQualifyingStreak": 2,
            },
            "watchdogTriggered": False, "error": None,
            "covererProcessesAtObserverStart": [],
            "covererWindowsAtObserverStart": [],
            "covererProcessEnumerationSucceededAtObserverStart": True,
            "observerStartWindowQuerySucceeded": True,
            "observerQueriesBeforeTargetHandoff": [],
            "samples": [{
                "target": {
                    "ownerPID": target_pid, "windowID": 91,
                    "processAlive": True, "processIdentityMatches": True,
                    "processImagePath": process_path,
                    "processImageSHA256": process_sha,
                },
                "sampledAtMonotonicNS": 5_000_000_000,
                "canonicalUncoveredEligible": True,
                "neutralCovererProcesses": [], "neutralCovererWindows": [],
                "alphaOpaqueFullCoverCandidatePresent": False,
            }],
        }
        expected_process = {"role": "wine_host_image", "path": process_path,
                            "sha256": process_sha}
        execution_result = {
            "experiment_id": run_id, "experiment_token": token,
            "target_unix_pid": target_pid, "process_returncode": 0,
            "timed_out": False,
            "target_process_exit_monotonic_ns": 6_000_000_000,
        }
        expected_trace_path = observer["targetIdentityHandoff"]["target_trace_path"]
        trace_index = {
            "status": "PASS", "target_unix_pid": target_pid,
            "experiment_token": token, "target_trace_path": expected_trace_path,
            "processes": [{
                "trace_role": "target", "host_pid": target_pid,
                "target_unix_pid": target_pid, "path": expected_trace_path,
                "ownership_valid": True, "record_count": 5,
            }],
        }
        valid = runner_b.validate_smoke_observer(
            observer, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token,
            expected_process_identity=expected_process, trace_index=trace_index,
            execution_result=execution_result,
            target_run_end_monotonic_ns=5_000_000_000)
        self.assertEqual(valid["status"], "PASS", valid["errors"])

        terminal_observer = copy.deepcopy(observer)
        terminal_sample = copy.deepcopy(observer["samples"][0])
        terminal_sample["sampledAtMonotonicNS"] = 5_050_000_000
        terminal_sample["canonicalUncoveredEligible"] = False
        terminal_sample["aboveTargetWindowDetailsComplete"] = True
        terminal_sample["query"] = {"status": "TARGET_WINDOW_FOUND"}
        terminal_sample["target"].update({
            "ownerPID": target_pid, "processAlive": True,
            "processIdentityMatches": True,
            "processImagePath": process_path, "processImageSHA256": process_sha,
            "titleMatched": True, "onScreen": True, "cgWindowIsOnscreen": True,
            "rankFrontToBack": 1, "rankSource": "onScreenWindowList",
            "windowIdentityAmbiguous": False, "windowIdentityError": None,
            "accessibility": {}, "accessibilityMinimized": None,
            "appKitMiniaturized": False, "alpha": 1,
            "displayRelationship": {
                "uniqueExpectedDisplayMatch": True,
                "targetFullyOnExpectedDisplay": True,
            },
        })
        terminal_sample["app"] = {"isActive": True}
        terminal_sample["inProcessTelemetry"] = {
            "available": True, "fresh": True, "windowVisible": True,
            "windowOccluded": False, "visibleBitSet": True,
            "windowMiniaturized": False, "windowKey": True,
            "windowMain": True, "appActive": True,
            "timestampMachNS": 5_049_000_000, "ageNS": 1_000_000,
        }
        terminal_sample.update({
            "allWindowListQuerySucceeded": True,
            "onScreenWindowListQuerySucceeded": True,
            "covererProcessEnumerationSucceeded": True,
            "alphaOpaqueFullCoverCandidatePresent": False,
            "neutralCovererProcessPresent": False,
            "neutralCovererWindowPresent": False,
            "neutralCovererCoveringWindowPresent": False,
            "neutralCovererProcesses": [], "neutralCovererWindows": [],
        })
        terminal_observer["samples"].append(terminal_sample)
        post_exit_sample = {
            "sampledAtMonotonicNS": 5_800_000_000,
            "canonicalUncoveredEligible": False,
            "target": {"ownerPID": target_pid, "processAlive": False,
                       "windowID": None},
            "neutralCovererProcesses": [], "neutralCovererWindows": [],
        }
        terminal_observer["samples"].append(post_exit_sample)
        terminal_observer["sampleCount"] = 3
        terminal_observer["experimentCompleteSignal"][
            "target_process_exit_monotonic_ns"] = 5_200_000_000
        terminal_execution = {
            **execution_result, "target_process_exit_monotonic_ns": 5_200_000_000,
        }
        terminal_observer["experimentCompleteSignal"][
            "runner_monotonic_ns"] = 5_300_000_000
        terminal_observer["endedAtMonotonicNS"] = 6_200_000_000
        terminal = runner_b.validate_smoke_observer(
            terminal_observer, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token,
            expected_process_identity=expected_process, trace_index=trace_index,
            execution_result=terminal_execution,
            target_run_end_monotonic_ns=5_100_000_000)
        self.assertEqual(terminal["status"], "PASS", terminal["errors"])
        self.assertEqual(terminal["terminal_accessibility_gap_samples_accepted"], 1)

        recovered_after_gap = copy.deepcopy(terminal_observer)
        recovered_sample = copy.deepcopy(terminal_observer["samples"][0])
        recovered_sample["sampledAtMonotonicNS"] = 5_075_000_000
        recovered_after_gap["samples"].insert(2, recovered_sample)
        rejected_recovery = runner_b.validate_smoke_observer(
            recovered_after_gap, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token,
            expected_process_identity=expected_process, trace_index=trace_index,
            execution_result=terminal_execution,
            target_run_end_monotonic_ns=5_100_000_000)
        self.assertEqual(rejected_recovery["status"], "FAIL")
        self.assertTrue(any("followed by target exit" in error
                            for error in rejected_recovery["errors"]))

        unsafe_terminal = copy.deepcopy(terminal_observer)
        unsafe_terminal["samples"][-2]["inProcessTelemetry"]["windowOccluded"] = True
        rejected_terminal = runner_b.validate_smoke_observer(
            unsafe_terminal, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token,
            expected_process_identity=expected_process, trace_index=trace_index,
            execution_result=terminal_execution,
            target_run_end_monotonic_ns=5_100_000_000)
        self.assertEqual(rejected_terminal["status"], "FAIL")
        self.assertTrue(any("during its measured run" in error
                            for error in rejected_terminal["errors"]))

        early_terminal = runner_b.validate_smoke_observer(
            terminal_observer, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token,
            expected_process_identity=expected_process, trace_index=trace_index,
            execution_result=terminal_execution,
            target_run_end_monotonic_ns=7_000_000_000)
        self.assertEqual(early_terminal["status"], "FAIL")

        for changed_identity in ("ownerPID", "windowID", "processImageSHA256"):
            with self.subTest(canonical_identity=changed_identity):
                switched_window = copy.deepcopy(terminal_observer)
                swapped = copy.deepcopy(terminal_sample)
                swapped["sampledAtMonotonicNS"] = 5_060_000_000
                swapped["canonicalUncoveredEligible"] = True
                if changed_identity == "ownerPID":
                    swapped["target"]["ownerPID"] = target_pid + 1
                elif changed_identity == "windowID":
                    swapped["target"]["windowID"] = 92
                else:
                    swapped["target"]["processImageSHA256"] = "0" * 64
                switched_window["samples"].insert(-1, swapped)
                rejected_identity = runner_b.validate_smoke_observer(
                    switched_window, experiment_id=run_id, target_pid=target_pid,
                    runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
                    token=token, expected_process_identity=expected_process,
                    trace_index=trace_index, execution_result=terminal_execution,
                    target_run_end_monotonic_ns=5_100_000_000)
                self.assertEqual(rejected_identity["status"], "FAIL")
                self.assertTrue(any("exact handed-off PID, window, and image" in error
                                    for error in rejected_identity["errors"]))

        for malformed_completion, expected_error in (
                ({"runner_pid": completion_runner_pid + 1}, "written by this runner"),
                ({"runner_monotonic_ns": 5_199_999_999}, "after the verified target exit"),
                ({"runner_monotonic_ns": 6_300_000_000}, "observer result does not end"),
                ({"process_runner_returncode": False}, "does not bind this run, target PID, and token"),
                ({"target_process_exit_monotonic_ns": 5_200_000_001}, "does not bind the target process exit")):
            with self.subTest(completion=malformed_completion):
                malformed = copy.deepcopy(terminal_observer)
                malformed["experimentCompleteSignal"].update(malformed_completion)
                rejected_completion = runner_b.validate_smoke_observer(
                    malformed, experiment_id=run_id, target_pid=target_pid,
                    runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
                    token=token, expected_process_identity=expected_process,
                    trace_index=trace_index, execution_result=terminal_execution,
                    target_run_end_monotonic_ns=5_100_000_000)
                self.assertEqual(rejected_completion["status"], "FAIL")
                self.assertTrue(any(expected_error in error
                                    for error in rejected_completion["errors"]))

        for bad_execution, expected_error in (
                ({"experiment_id": "other-run"}, "execution result does not bind"),
                ({"experiment_token": "e" * 32}, "execution result does not bind"),
                ({"target_unix_pid": target_pid + 1}, "execution result does not bind"),
                ({"process_returncode": 1}, "execution result does not bind"),
                ({"process_returncode": False}, "execution result does not bind"),
                ({"timed_out": True}, "execution result does not bind"),
                ({"target_process_exit_monotonic_ns": None}, "execution result does not bind")):
            with self.subTest(execution=bad_execution):
                malformed = {**terminal_execution, **bad_execution}
                rejected_execution = runner_b.validate_smoke_observer(
                    terminal_observer, experiment_id=run_id, target_pid=target_pid,
                    runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
                    token=token, expected_process_identity=expected_process,
                    trace_index=trace_index, execution_result=malformed,
                    target_run_end_monotonic_ns=5_100_000_000)
                self.assertEqual(rejected_execution["status"], "FAIL")
                self.assertTrue(any(expected_error in error
                                    for error in rejected_execution["errors"]))

        two_ax_misses = copy.deepcopy(terminal_observer)
        second_miss = copy.deepcopy(terminal_sample)
        second_miss["sampledAtMonotonicNS"] = 5_075_000_000
        two_ax_misses["samples"].insert(-1, second_miss)
        rejected_two_misses = runner_b.validate_smoke_observer(
            two_ax_misses, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token, expected_process_identity=expected_process,
            trace_index=trace_index, execution_result=terminal_execution,
            target_run_end_monotonic_ns=5_100_000_000)
        self.assertEqual(rejected_two_misses["status"], "FAIL")
        self.assertTrue(any("more than one terminal accessibility" in error
                            for error in rejected_two_misses["errors"]))

        for field, value in (("focused", False), ("accessibilityTrusted", False),
                             ("minimized", True)):
            with self.subTest(accessibility_field=field):
                bad_accessibility = copy.deepcopy(terminal_observer)
                ax_sample = bad_accessibility["samples"][-2]
                ax_sample["target"]["accessibility"][field] = value
                rejected_ax = runner_b.validate_smoke_observer(
                    bad_accessibility, experiment_id=run_id, target_pid=target_pid,
                    runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
                    token=token, expected_process_identity=expected_process,
                    trace_index=trace_index, execution_result=terminal_execution,
                    target_run_end_monotonic_ns=5_100_000_000)
                self.assertEqual(rejected_ax["status"], "FAIL")

        nonzero_exit = {**terminal_execution, "process_returncode": 1}
        timeout_exit = {**terminal_execution, "timed_out": True}
        for bad_result in (nonzero_exit, timeout_exit):
            with self.subTest(process_result=bad_result):
                rejected_exit = runner_b.validate_smoke_observer(
                    terminal_observer, experiment_id=run_id, target_pid=target_pid,
                    runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
                    token=token, expected_process_identity=expected_process,
                    trace_index=trace_index, execution_result=bad_result,
                    target_run_end_monotonic_ns=5_100_000_000)
                self.assertEqual(rejected_exit["status"], "FAIL")

        too_late_exit = {**terminal_execution,
                         "target_process_exit_monotonic_ns": 6_700_000_000}
        too_late_observer = copy.deepcopy(terminal_observer)
        too_late_observer["experimentCompleteSignal"][
            "target_process_exit_monotonic_ns"] = 6_700_000_000
        too_late_observer["experimentCompleteSignal"]["runner_monotonic_ns"] = 6_800_000_000
        too_late_observer["endedAtMonotonicNS"] = 6_900_000_000
        rejected_late_exit = runner_b.validate_smoke_observer(
            too_late_observer, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token, expected_process_identity=expected_process,
            trace_index=trace_index, execution_result=too_late_exit,
            target_run_end_monotonic_ns=5_100_000_000)
        self.assertEqual(rejected_late_exit["status"], "FAIL")
        self.assertTrue(any("within one observer interval" in error
                            for error in rejected_late_exit["errors"]))

        observer["targetIdentityHandoff"]["runner_pid"] = runner_pid + 1
        observer["targetProcessIdentityEvidence"]["processIdentity"]["pid"] = target_pid + 1
        rejected = runner_b.validate_smoke_observer(
            observer, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token,
            expected_process_identity=expected_process, trace_index=trace_index,
            execution_result=execution_result,
            target_run_end_monotonic_ns=5_000_000_000)
        self.assertEqual(rejected["status"], "FAIL")
        self.assertTrue(any("actual PrefixLease process runner" in error
                            for error in rejected["errors"]))
        self.assertTrue(any("process_identity row is incomplete or mismatched" in error
                            for error in rejected["errors"]))

        observer["targetIdentityHandoff"]["target_trace_path"] = "/tmp/unrelated-trace.jsonl"
        rejected_trace = runner_b.validate_smoke_observer(
            observer, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token,
            expected_process_identity=expected_process, trace_index=trace_index,
            execution_result=execution_result,
            target_run_end_monotonic_ns=5_000_000_000)
        self.assertEqual(rejected_trace["status"], "FAIL")
        self.assertTrue(any("trace path" in error for error in rejected_trace["errors"]))

        observer["targetIdentityHandoff"]["target_trace_path"] = expected_trace_path
        observer["canonicalUncoveredState"]["telemetryProgressVerified"] = False
        rejected_stale_telemetry = runner_b.validate_smoke_observer(
            observer, experiment_id=run_id, target_pid=target_pid,
            runner_pid=runner_pid, completion_runner_pid=completion_runner_pid,
            token=token,
            expected_process_identity=expected_process, trace_index=trace_index,
            execution_result=execution_result,
            target_run_end_monotonic_ns=5_000_000_000)
        self.assertEqual(rejected_stale_telemetry["status"], "FAIL")
        self.assertTrue(any("advancing Wine AppKit telemetry" in error
                            for error in rejected_stale_telemetry["errors"]))

    def test_every_relevant_identity_change_invalidates_authorization(self) -> None:
        identities = {name: "a" * 64 for name in (
            "app", "wine_launcher", "wine_host_image", "winevulkan_dll",
            "winevulkan_so", "wine_host_loader", "trace_proxy", "moltenvk",
            "trace_validator")}
        original = {"candidate_identities": identities, "architecture": "x86_64-under-Rosetta",
                    "gate0_control_hashes": {"policy": "a" * 64}}
        for name in identities:
            with self.subTest(identity=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                self._write_valid_auth(root, original)
                changed = {**original, "candidate_identities": {**identities, name: "b" * 64}}
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    runner_b.verify_smoke_authorization(SMOKE_ID, runs_root=root, current_bindings=changed)
        for name, value in (("architecture", "arm64"), ("gate0_control_hashes", {})):
            with self.subTest(control=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                self._write_valid_auth(root, original)
                with self.assertRaisesRegex(RuntimeError, "stale"):
                    runner_b.verify_smoke_authorization(SMOKE_ID, runs_root=root,
                                                       current_bindings={**original, name: value})

    def test_other_smoke_pass_receipt_cannot_authorize_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._write_valid_auth(root, {})
            result = json.loads((root / SMOKE_ID / "run-result.json").read_text())
            receipt = result["prefix_cleanup"]
            self.assertTrue(runner_b.automatic_post_matches(receipt, SMOKE_ID,
                storage_policy.PREFIX_ROOT / f"{SMOKE_ID}-prefix", result["storage_manifest_sha256"]))
            for field, value in (("experiment_id", "other-smoke"), ("prefix_path", "/other-prefix")):
                altered = storage_policy._seal_storage_evidence({**receipt, field: value},
                                                              "cleanup_receipt_hmac_sha256")
                self.assertFalse(runner_b.automatic_post_matches(altered, SMOKE_ID,
                    storage_policy.PREFIX_ROOT / f"{SMOKE_ID}-prefix", result["storage_manifest_sha256"]))
            self.assertFalse(runner_b.automatic_post_matches(receipt, SMOKE_ID,
                storage_policy.PREFIX_ROOT / f"{SMOKE_ID}-prefix", "d" * 64))

    def test_baseline_cannot_be_invoked_without_smoke_authorization(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "requires a matching authenticated prior smoke ID"):
            runner_b.run_one("baseline", 1)
        with patch.object(sys, "argv", ["runner_b.py", "baseline", "1"]):
            self.assertEqual(runner_b.main(), 2)

    def test_smoke_environment_binds_certified_icd_manifest_for_prefixlease(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary).resolve()
            traces = root / "host-traces"
            traces.mkdir(mode=0o700)
            with patch.dict(runner_b.os.environ, {}, clear=True):
                environment = runner_b.clean_environment(root / "prefix", root,
                    root / "app-trace.jsonl", traces, 30, experiment_token="a" * 32)
            self.assertEqual(environment["FG_B_TRACE_DIRECTORY"], str(traces))
            self.assertNotIn("FG_B_TARGET_UNIX_PID", environment)
            self.assertNotIn("FG_B_MVK_TRACE_PATH", environment)
        expected_path = str(runner_b.ICD)
        for key in ("VK_DRIVER_FILES", "VK_ICD_FILENAMES"):
            self.assertEqual(environment[key], expected_path)
            self.assertEqual(hashlib.sha256(Path(environment[key]).read_bytes()).hexdigest(),
                "578ff08cd0d8734619357541771a5abc9c3470ca300030219a971a9e9dbbe466")

    def test_changed_target_index_or_app_trace_invalidates_authorization(self) -> None:
        for role in ("target_host_trace", "trace_index", "app_trace",
                     "public_window_start_signal"):
            with self.subTest(role=role), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                self._write_valid_auth(root, {})
                result = json.loads((root / SMOKE_ID / "run-result.json").read_text())
                Path(result["trace_artifacts"][role]["path"]).write_text("replacement")
                with self.assertRaisesRegex(RuntimeError, "artifact.*changed"):
                    runner_b.verify_smoke_authorization(SMOKE_ID, runs_root=root, current_bindings={})

    def test_missing_target_artifacts_cannot_authorize_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._write_valid_auth(root, {})
            result = json.loads((root / SMOKE_ID / "run-result.json").read_text())
            Path(result["trace_artifacts"]["target_host_trace"]["path"]).unlink()
            with self.assertRaises((RuntimeError, FileNotFoundError)):
                runner_b.verify_smoke_authorization(SMOKE_ID, runs_root=root, current_bindings={})

    def test_pinned_smoke_pe_has_no_unshipped_mingw_runtime_imports(self) -> None:
        identities = runner_b.candidate_files()
        self.assertEqual(
            identities["app"]["sha256"],
            "31fd01f5b59c408053a3f82146427fae9d1b5b515ce9fe2ed681358aed8ad088",
        )
        inspection = subprocess.run(
            ["x86_64-w64-mingw32-objdump", "-p", str(runner_b.APP)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        )
        imported_dlls = {
            match.casefold() for match in re.findall(
                r"^\s*DLL Name: (.+)$", inspection.stdout, re.MULTILINE)
        }
        self.assertFalse(imported_dlls & {
            "libstdc++-6.dll", "libgcc_s_seh-1.dll", "libwinpthread-1.dll",
        })

    def test_matching_authenticated_smoke_is_accepted_offline(self) -> None:
        bindings = {"control_version": "test-v1", "candidate_identities": {"app": "a" * 64}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._write_valid_auth(root, bindings)
            authorization, path = runner_b.verify_smoke_authorization(
                SMOKE_ID, runs_root=root, current_bindings=bindings)
            self.assertEqual(authorization["status"], "PASS")
            self.assertEqual(path.name, "smoke-authorization.json")

    def test_changed_runtime_hash_invalidates_prior_smoke(self) -> None:
        original = {"candidate_identities": {"moltenvk": "a" * 64}}
        changed = {"candidate_identities": {"moltenvk": "b" * 64}}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._write_valid_auth(root, original)
            with self.assertRaisesRegex(RuntimeError, "stale because a runtime or control hash changed"):
                runner_b.verify_smoke_authorization(
                    SMOKE_ID, runs_root=root, current_bindings=changed)

    def test_mutated_smoke_result_invalidates_its_authenticated_binding(self) -> None:
        bindings = {"control_version": "test-v1"}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            self._write_valid_auth(root, bindings)
            result_path = root / SMOKE_ID / "run-result.json"
            result_path.write_text(result_path.read_text() + " ", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "does not bind a complete passing smoke"):
                runner_b.verify_smoke_authorization(
                    SMOKE_ID, runs_root=root, current_bindings=bindings)


if __name__ == "__main__":
    unittest.main()
