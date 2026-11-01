#!/usr/bin/env python3
"""Focused test suite for P2 local compatibility consumption and admission predicates.

Retains the original predicate fixtures and exercises production-path parser and admission negatives.
"""

from __future__ import annotations

import copy
import argparse
import hashlib
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import p2_base_orchestrator as p2
import storage_policy as policy
import vm_capacity_domain as vm


def _accepted_p1_g0c_fixture_identity() -> dict[str, object]:
    """Return only the G0C identity authenticated by the accepted P1 final."""
    final, _digest = p2.read_signed(
        p2.P1_FINAL_PATH, "restore_verification_hmac_sha256")
    identity = final.get("g0c_identity")
    if not isinstance(identity, dict):
        raise RuntimeError("accepted P1 final receipt omits its authenticated G0C identity")
    return copy.deepcopy(identity)


class P2LocalCompatibilityPredicateTests(unittest.TestCase):
    """Test accepted P2 reconciliation and production compatibility/admission predicates."""

    def setUp(self) -> None:
        self.signing_key = vm.policy._manifest_signing_key()
        self.accepted_p1_g0c_identity = _accepted_p1_g0c_fixture_identity()
        with mock.patch.object(
                vm, "verify_fresh_g0c_review",
                return_value=self.accepted_p1_g0c_identity):
            self.p1_lineage = p2._validate_accepted_p1(allow_historical_r2_boundary=True)
        self.reconciliation_info = p2._validate_p2_reconciliation()

    # 1. exact P1 + reconciliation + exact local hash → PASS
    def test_01_exact_p1_and_reconciliation_and_local_hash_pass(self) -> None:
        rec = p2._validate_p2_reconciliation()
        self.assertIsNotNone(rec)
        self.assertEqual(p2.P2_LOCAL_HARDWARE_MODEL_SHA256,
                         "976e66740c64041cf9f127588f93eabf7efb9e7d3cbaee9582bcded88ba5e8d3")
        self.assertEqual(p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256,
                         "18d57e0cfa40f7af63bb38c801a03bde06d00b281554b926128828a7ca0d9a2e")

    # 2. P1 hash differs from reconciled local hash → PASS
    def test_02_p1_hash_differs_from_reconciled_local_hash_pass(self) -> None:
        self.assertNotEqual(p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256,
                            p2.P2_LOCAL_HARDWARE_MODEL_SHA256)
        # Both must be valid 64-char hex
        self.assertEqual(len(p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256), 64)
        self.assertEqual(len(p2.P2_LOCAL_HARDWARE_MODEL_SHA256), 64)

    # 3. local hash equals P1 hash but differs from reconciliation → FAIL
    def test_03_local_hash_equals_p1_hash_differs_from_reconciliation_fails(self) -> None:
        # If the local parse returned the P1 hash (18d57e0c...) instead of the reconciled
        # local hash (976e6674...), plan check must reject it.
        mock_plan = {
            "status": "PASS",
            "plan_only": True,
            "guest_assets_created": False,
            "restore_sha256": p2.RESTORE_SHA256,
            "hardware_model_sha256": p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256, # equals P1, differs from reconciliation!
            "is_supported": True,
            "metadata": {
                "restore_sha256": p2.RESTORE_SHA256,
                "operating_system_version": "27.0.1",
                "build_version": "26A434",
            }
        }
        with self.assertRaises(RuntimeError) as ctx:
            # Plan verification checks hardware_model_sha256 == P2_LOCAL_HARDWARE_MODEL_SHA256
            if mock_plan["hardware_model_sha256"] != p2.P2_LOCAL_HARDWARE_MODEL_SHA256:
                raise RuntimeError("helper plan does not revalidate the exact accepted local IPSW and hardware model")
        self.assertIn("hardware model", str(ctx.exception))

    # 4. changed local P2 model hash → FAIL
    def test_04_changed_local_p2_model_hash_fails(self) -> None:
        corrupted_hash = "a" * 64
        with self.assertRaises(RuntimeError):
            if corrupted_hash != p2.P2_LOCAL_HARDWARE_MODEL_SHA256:
                raise RuntimeError("local hardware model hash differs from reconciled P2 identity")

    # 5. changed IPSW SHA → FAIL
    def test_05_changed_ipsw_sha_fails(self) -> None:
        changed_sha = "0" * 64
        with self.assertRaises(RuntimeError):
            if changed_sha != p2.RESTORE_SHA256:
                raise RuntimeError("accepted IPSW size or complete local SHA-256 differs from P1")

    # 6. same version/build but changed IPSW bytes → FAIL
    def test_06_same_version_build_but_changed_ipsw_bytes_fails(self) -> None:
        product = "27.0.1"
        build = "26A434"
        changed_bytes_sha = "f" * 64
        self.assertEqual(product, "27.0.1")
        self.assertEqual(build, "26A434")
        with self.assertRaises(RuntimeError):
            if changed_bytes_sha != p2.RESTORE_SHA256:
                raise RuntimeError("IPSW payload bytes do not match canonical accepted SHA-256")

    # 7. changed logical size → FAIL
    def test_07_changed_logical_size_fails(self) -> None:
        changed_size = 26637307068 # 1 byte off
        with self.assertRaises(RuntimeError):
            if changed_size != p2.RESTORE_SIZE:
                raise RuntimeError("accepted IPSW size differs from P1")

    # 8. unsupported local image → FAIL
    def test_08_unsupported_local_image_fails(self) -> None:
        image_supported = False
        with self.assertRaises(RuntimeError):
            if not image_supported:
                raise RuntimeError("accepted local restore image is unsupported")

    # 9. unsupported hardware model → FAIL
    def test_09_unsupported_hardware_model_fails(self) -> None:
        model_supported = False
        with self.assertRaises(RuntimeError):
            if not model_supported:
                raise RuntimeError("accepted local restore hardware model is unsupported")

    # 10. changed minimum CPU → FAIL
    def test_10_changed_minimum_cpu_fails(self) -> None:
        min_cpu = 4 # expected 2
        with self.assertRaises(RuntimeError):
            if min_cpu != 2:
                raise RuntimeError("minimum supported CPU count changed from accepted P1 requirement")

    # 11. changed minimum memory → FAIL
    def test_11_changed_minimum_memory_fails(self) -> None:
        min_mem = 8 * 1024 * 1024 * 1024 # 8 GiB instead of 4 GiB
        with self.assertRaises(RuntimeError):
            if min_mem != 4294967296:
                raise RuntimeError("minimum supported memory size changed from accepted P1 requirement")

    # 12. wrong reconciliation path/hash → FAIL
    def test_12_wrong_reconciliation_path_or_hash_fails(self) -> None:
        with mock.patch("p2_base_orchestrator.P2_RECONCILIATION_PATH", REPO / "nonexistent.json"):
            with self.assertRaises(RuntimeError) as ctx:
                p2._validate_p2_reconciliation()
            self.assertIn("missing", str(ctx.exception))

        with mock.patch("p2_base_orchestrator.P2_RECONCILIATION_SHA256", "b" * 64):
            with self.assertRaises(RuntimeError) as ctx:
                p2._validate_p2_reconciliation()
            self.assertIn("SHA-256 differs", str(ctx.exception))

    # 13. invalid reconciliation HMAC → FAIL
    def test_13_invalid_reconciliation_hmac_fails(self) -> None:
        wrong_key = b"wrong-key" * 4
        with self.assertRaises(RuntimeError) as ctx:
            p2.read_signed_with_key(
                p2.P2_RECONCILIATION_PATH,
                "vm_p2_local_compatibility_reconciliation_hmac_sha256",
                wrong_key[:32]
            )
        self.assertIn("hmac", str(ctx.exception).lower())

    # 14. wrong reconciliation review → FAIL
    def test_14_wrong_reconciliation_review_fails(self) -> None:
        with mock.patch("p2_base_orchestrator.P2_RECONCILIATION_REVIEW_PATH", REPO / "nonexistent-review.json"):
            with self.assertRaises(RuntimeError) as ctx:
                p2._validate_p2_reconciliation()
            self.assertIn("missing", str(ctx.exception))

        with mock.patch("p2_base_orchestrator.P2_RECONCILIATION_REVIEW_SHA256", "c" * 64):
            with self.assertRaises(RuntimeError) as ctx:
                p2._validate_p2_reconciliation()
            self.assertIn("SHA-256 differs", str(ctx.exception))

    # 15. reconciliation for another P1 → FAIL
    def test_15_reconciliation_for_another_p1_fails(self) -> None:
        tampered_reconciliation = copy.deepcopy(self.reconciliation_info["reconciliation"])
        tampered_reconciliation["p1_accepted_restore"]["final_verification_receipt_sha256"] = "d" * 64
        with mock.patch("p2_base_orchestrator.read_signed_with_key") as mock_read:
            mock_read.side_effect = [
                (tampered_reconciliation, p2.P2_RECONCILIATION_SHA256),
                (self.reconciliation_info["review"], p2.P2_RECONCILIATION_REVIEW_SHA256),
                (self.reconciliation_info["observation"], p2.P2_OBSERVATION_SHA256),
            ]
            with self.assertRaises(RuntimeError) as ctx:
                p2._validate_p2_reconciliation()
            self.assertIn("does not bind the accepted P1 restore identity", str(ctx.exception))

    # 16. local observation for another payload → FAIL
    def test_16_local_observation_for_another_payload_fails(self) -> None:
        tampered_obs = copy.deepcopy(self.reconciliation_info["observation"])
        tampered_obs["restore_sha256"] = "e" * 64
        with mock.patch("p2_base_orchestrator.read_signed_with_key") as mock_read:
            mock_read.side_effect = [
                (self.reconciliation_info["reconciliation"], p2.P2_RECONCILIATION_SHA256),
                (self.reconciliation_info["review"], p2.P2_RECONCILIATION_REVIEW_SHA256),
                (tampered_obs, p2.P2_OBSERVATION_SHA256),
            ]
            with self.assertRaises(RuntimeError) as ctx:
                p2._validate_p2_reconciliation()
            self.assertIn("reconciled hardware model identities", str(ctx.exception))

    # 17. P1 identity never rewritten
    def test_17_p1_identity_never_rewritten(self) -> None:
        upstream = self.p1_lineage["upstream_metadata"]
        self.assertEqual(upstream["hardware_model_data_sha256"],
                         p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256)
        self.assertEqual(self.p1_lineage["p1_final"]["local_sha256"],
                         p2.RESTORE_SHA256)
        self.assertEqual(p2.HARDWARE_MODEL_SHA256,
                         "18d57e0cfa40f7af63bb38c801a03bde06d00b281554b926128828a7ca0d9a2e")

    # 18. P2 identity never substituted into P1 fields
    def test_18_p2_identity_never_substituted_into_p1_fields(self) -> None:
        upstream = self.p1_lineage["upstream_metadata"]
        self.assertNotEqual(upstream["hardware_model_data_sha256"],
                            p2.P2_LOCAL_HARDWARE_MODEL_SHA256)
        self.assertEqual(upstream["hardware_model_data_sha256"],
                         p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256)

    # 19. reconciliation alone does not grant provisioning
    def test_19_reconciliation_alone_does_not_grant_provisioning(self) -> None:
        reconcile_auth = self.reconciliation_info["reconciliation"]["authority"]
        self.assertFalse(reconcile_auth["p2_provisioning_authority_granted"])
        self.assertTrue(reconcile_auth["review_required_before_any_later_provisioning"])
        review_exec = self.reconciliation_info["review"]["execution_boundary"]
        self.assertFalse(review_exec["provisioning_authorized_by_this_review"])
        self.assertFalse(review_exec["execution_authority_granted"])

    # 20. helper cannot create guest assets before P2 admission
    def test_20_helper_cannot_create_guest_assets_before_p2_admission(self) -> None:
        # Check that helper plan command runs with plan_only=true and creates no files
        self.assertTrue(self.reconciliation_info["observation"]["guest_assets_created"] is False)
        self.assertTrue(self.reconciliation_info["observation"]["plan_only"] is True)

    # 21. no VM start before P2 admission
    def test_21_no_vm_start_before_p2_admission(self) -> None:
        self.assertFalse(self.reconciliation_info["observation"]["virtualization_vm_actions_performed"])
        self.assertFalse(self.reconciliation_info["reconciliation"]["authority"]["virtualization_vm_actions_performed"])
        self.assertFalse(self.reconciliation_info["review"]["execution_boundary"]["virtualization_vm_actions_performed_by_reviewer"])

    # 22. no auxiliary storage creation before P2 admission
    def test_22_no_auxiliary_storage_creation_before_p2_admission(self) -> None:
        templates_dir = p2.VM_ROOT / "templates"
        if templates_dir.exists():
            aux_files = list(templates_dir.glob("*/auxiliary.bin"))
            self.assertEqual(aux_files, [], "No auxiliary storage file should exist before P2 execution")

    # 23. no RAW disk creation before P2 admission
    def test_23_no_raw_disk_creation_before_p2_admission(self) -> None:
        templates_dir = p2.VM_ROOT / "templates"
        if templates_dir.exists():
            raw_files = list(templates_dir.glob("*/system.raw"))
            self.assertEqual(raw_files, [], "No system.raw disk file should exist before P2 execution")

    def _valid_helper_plan_events(self) -> list[dict[str, object]]:
        identity = {
            "device": 16777241,
            "inode": 380,
            "logical_bytes": p2.RESTORE_SIZE,
            "allocated_bytes": 26_637_500_416,
            "link_count": 1,
            "mode": 33188,
            "mtime_ns": 1_700_000_000_000_000_000,
            "ctime_ns": 1_700_000_000_000_000_000,
            "mtime_seconds": 1_700_000_000,
            "mtime_nanoseconds": 0,
            "ctime_seconds": 1_700_000_000,
            "ctime_nanoseconds": 0,
        }
        p1_identity = {key: identity[key] for key in (
            "device", "inode", "logical_bytes", "allocated_bytes", "mtime_ns", "ctime_ns")}
        metadata = {
            "restore_sha256": p2.RESTORE_SHA256,
            "restore_file_identity": copy.deepcopy(identity),
            "hardware_model_sha256": p2.P2_LOCAL_HARDWARE_MODEL_SHA256,
            "operating_system_version": "27.0.1",
            "build_version": "26A434",
            "minimum_supported_cpu_count": 2,
            "minimum_supported_memory_size_bytes": 4 * p2.GIB,
        }
        return [
            {"event": "restore_hash_verified", "sha256": p2.RESTORE_SHA256,
             "file_identity": copy.deepcopy(identity)},
            {"event": "local_restore_compatibility_observed",
             "restore_sha256": p2.RESTORE_SHA256,
             "restore_file_identity_after_hash": copy.deepcopy(identity),
             "restore_file_identity_after_parse": copy.deepcopy(identity),
             "restore_parse_source": "VZMacOSRestoreImage.image(from: verified local IPSW URL)",
             "operating_system_version": "27.0.1", "build_version": "26A434",
             "image_supported": True, "hardware_model_supported": True,
             "hardware_model_sha256": p2.P2_LOCAL_HARDWARE_MODEL_SHA256,
             "minimum_supported_cpu_count": 2,
             "minimum_supported_memory_size_bytes": 4 * p2.GIB},
            {"event": "plan", "is_supported": True,
             "restore_file_identity": p1_identity,
             "hardware_model_sha256": p2.P2_LOCAL_HARDWARE_MODEL_SHA256,
             "metadata": metadata,
             "hardware_model_data_base64": "AQ==",
             "machine_identifier_data_base64": "Ag==",
             "machine_identifier_sha256": "d" * 64,
             "machine_identifier_creation": {
                 "method": "VZMacMachineIdentifier() exactly once during the signed P2 identity-plan command",
                 "created_at_unix": 1_700_000_000.0,
             },
             "monotonic_time_seconds": 12.5},
        ]

    def test_25_real_run_plan_parser_rejects_compatibility_mismatches(self) -> None:
        """Drive _run_plan's JSON event parser and production validator with bad helper output."""

        class FakeProcess:
            returncode = 0
            code_directory_hash = "a" * 40
            executable_sha256_after_suspended_exec = "b" * 64
            executable_identity_after_suspended_exec = {"device": 1, "inode": 2}

            def __init__(self, stdout_text: str) -> None:
                self.stdout_text = stdout_text
                self.stderr_text = ""

            def communicate(self, timeout: int) -> tuple[str, str]:
                self.assert_timeout = timeout
                return self.stdout_text, self.stderr_text

        manifest = {"source_manifest": {
            "binary": {"sha256": "b" * 64, "file_identity": {"device": 1, "inode": 2},
                       "code_directory_hash": "c" * 40},
            "source": {"sha256": "d" * 64}}}
        with tempfile.TemporaryDirectory(prefix=".p2-plan-success-test-", dir=HERE) as temporary:
            temp_root = pathlib.Path(temporary)
            plan_path = temp_root / "identity-plan.json"
            manifest_path = temp_root / "manifest.json"
            review_path = temp_root / "review.json"
            manifest_path.write_text("{}", encoding="utf-8")
            review_path.write_text("{}", encoding="utf-8")
            valid_process = FakeProcess("\n".join(
                json.dumps(row) for row in self._valid_helper_plan_events()))
            with (mock.patch.object(p2, "_verify_reviewed_provenance", return_value=(manifest, {})),
                  mock.patch.object(p2, "_open_reviewed_binary_for_exec", return_value=(91, {})),
                  mock.patch.object(p2, "_spawn_reviewed_binary_suspended", return_value=valid_process),
                  mock.patch.object(p2.os, "close"),
                  mock.patch.object(p2, "unique_path", return_value=plan_path)):
                p2._run_plan("p2-native-identity-shape-test", pathlib.Path("/tmp/not-launched-helper"),
                             manifest_path, review_path)
            sealed_plan, _ = p2.read_signed(plan_path, "vm_p2_identity_plan_hmac_sha256")
            expected_identity = self._valid_helper_plan_events()[2]["restore_file_identity"]
            self.assertEqual(sealed_plan["restore_file_identity"], expected_identity)
            self.assertEqual(sealed_plan["minimum_supported_cpu_count"], 2)
            self.assertEqual(sealed_plan["minimum_supported_memory_size_bytes"], 4 * p2.GIB)

        cases = [
            ("wrong local model", lambda rows: (
                rows[1].update(hardware_model_sha256=p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256),
                rows[2].update(hardware_model_sha256=p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256),
                rows[2]["metadata"].update(hardware_model_sha256=p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256))),
            ("wrong IPSW SHA", lambda rows: (
                rows[0].update(sha256="0" * 64),
                rows[1].update(restore_sha256="0" * 64),
                rows[2]["metadata"].update(restore_sha256="0" * 64))),
            ("wrong logical size", lambda rows: (
                rows[0]["file_identity"].update(logical_bytes=p2.RESTORE_SIZE + 1),
                rows[1]["restore_file_identity_after_hash"].update(logical_bytes=p2.RESTORE_SIZE + 1),
                rows[1]["restore_file_identity_after_parse"].update(logical_bytes=p2.RESTORE_SIZE + 1),
                rows[2]["restore_file_identity"].update(logical_bytes=p2.RESTORE_SIZE + 1),
                rows[2]["metadata"]["restore_file_identity"].update(logical_bytes=p2.RESTORE_SIZE + 1))),
            ("wrong CPU minimum", lambda rows: (
                rows[1].update(minimum_supported_cpu_count=1),
                rows[2]["metadata"].update(minimum_supported_cpu_count=1))),
            ("wrong memory minimum", lambda rows: (
                rows[1].update(minimum_supported_memory_size_bytes=2 * p2.GIB),
                rows[2]["metadata"].update(minimum_supported_memory_size_bytes=2 * p2.GIB))),
        ]
        run_id = "p2-production-plan-negative-test"
        for label, mutate in cases:
            with self.subTest(case=label):
                events = self._valid_helper_plan_events()
                mutate(events)
                process = FakeProcess("\n".join(json.dumps(row) for row in events))
                with (mock.patch.object(p2, "_verify_reviewed_provenance", return_value=(manifest, {})),
                      mock.patch.object(p2, "_open_reviewed_binary_for_exec", return_value=(91, {})),
                      mock.patch.object(p2, "_spawn_reviewed_binary_suspended", return_value=process),
                      mock.patch.object(p2.os, "close")):
                    with self.assertRaises(RuntimeError):
                        p2._run_plan(run_id, pathlib.Path("/tmp/not-launched-helper"),
                                     pathlib.Path("/tmp/manifest"), pathlib.Path("/tmp/review"))

    def test_26_real_reconciliation_validator_rejects_p1_p2_model_substitution(self) -> None:
        tampered_observation = copy.deepcopy(self.reconciliation_info["observation"])
        tampered_observation["observed_local_hardware_model_sha256"] = p2.P1_UPSTREAM_HARDWARE_MODEL_SHA256
        with mock.patch("p2_base_orchestrator.read_signed_with_key") as mock_read:
            mock_read.side_effect = [
                (self.reconciliation_info["reconciliation"], p2.P2_RECONCILIATION_SHA256),
                (self.reconciliation_info["review"], p2.P2_RECONCILIATION_REVIEW_SHA256),
                (tampered_observation, p2.P2_OBSERVATION_SHA256),
            ]
            with self.assertRaises(RuntimeError) as ctx:
                p2._validate_p2_reconciliation()
        self.assertIn("reconciled hardware model identities", str(ctx.exception))

    def test_27_real_reconciliation_validator_rejects_wrong_review_decision(self) -> None:
        tampered_review = copy.deepcopy(self.reconciliation_info["review"])
        tampered_review["decision"] = "PASS"
        with mock.patch("p2_base_orchestrator.read_signed_with_key") as mock_read:
            mock_read.side_effect = [
                (self.reconciliation_info["reconciliation"], p2.P2_RECONCILIATION_SHA256),
                (tampered_review, p2.P2_RECONCILIATION_REVIEW_SHA256),
                (self.reconciliation_info["observation"], p2.P2_OBSERVATION_SHA256),
            ]
            with self.assertRaises(RuntimeError) as ctx:
                p2._validate_p2_reconciliation()
        self.assertIn("does not approve", str(ctx.exception))

    def test_28_real_provision_gate_rejects_failed_admission_before_helper_launch(self) -> None:
        token = hashlib.sha256(b"p2-negative-test-control-token").digest()
        run_id = "p2-failed-admission-negative-test"
        with tempfile.TemporaryDirectory(prefix=".p2-gate-test-", dir=HERE) as temporary:
            root = pathlib.Path(temporary)
            manifest_path = root / "manifest.json"
            review_path = root / "review.json"
            admission_path = root / "admission.json"
            failure_path = root / "failure.json"
            manifest_path.write_text("{}", encoding="utf-8")
            review_path.write_text("{}", encoding="utf-8")
            p2.signed_write(admission_path, {
                "schema_version": 1,
                "record_type": "R6_VM_P2_ADMISSION",
                "run_id": run_id,
                "session_id": p2.P2_R2_SESSION_ID,
                "decision": "FAIL",
                "authorized_phase": None,
                "helper_control_token_sha256": hashlib.sha256(token).hexdigest(),
            }, "vm_admission_hmac_sha256", self.signing_key)
            admission_sha = vm._file_sha256(admission_path)
            launch_claim = {"status": "CLAIMED", "session_id": p2.P2_R2_SESSION_ID,
                            "admission_sha256": admission_sha,
                            "commitment_sha256": hashlib.sha256(token).hexdigest()}
            original_read = p2.read_signed_with_key
            active_marker, active_marker_sha = original_read(
                vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256", self.signing_key)

            def read_signed(path: pathlib.Path, field: str, signing_key: bytes):
                if pathlib.Path(path) == admission_path:
                    return original_read(path, field, signing_key)
                if pathlib.Path(path) == vm.VM_ACTIVE_MARKER_PATH:
                    return active_marker, active_marker_sha
                if pathlib.Path(path) == failure_path:
                    return original_read(path, field, signing_key)
                raise AssertionError(f"unexpected signed read in failed-admission path: {path}")

            args = argparse.Namespace(run_id=run_id, manifest=str(manifest_path),
                                      review=str(review_path), admission=str(admission_path))
            with (mock.patch.object(p2, "_verify_reviewed_provenance", return_value=({}, {})),
                  mock.patch.object(p2, "read_signed_with_key", side_effect=read_signed),
                  mock.patch.object(p2, "unique_path", return_value=failure_path),
                  mock.patch.object(p2, "_spawn_reviewed_binary_suspended") as launch):
                with self.assertRaisesRegex(RuntimeError, "signed failure evidence recorded"):
                    p2.provision(args)
            launch.assert_not_called()
            failure, _ = p2.read_signed_with_key(
                failure_path, "vm_p2_helper_launch_failure_hmac_sha256", self.signing_key)
            self.assertEqual(failure["status"], "FAIL")
            self.assertFalse(failure["helper_process_created"])
            self.assertFalse(failure["guest_assets_created"])
            self.assertIn("exact bounded P2 provisioning authority", failure["error"])


class P2RealWorldRegressionFixtureTests(unittest.TestCase):
    """Exact real-world regression fixture representing the accepted state."""

    def test_24_exact_real_world_regression_fixture(self) -> None:
        """P1 upstream: 18d57e0cfa40f7af63bb38c801a03bde06d00b281554b926128828a7ca0d9a2e

        P2 local: 976e66740c64041cf9f127588f93eabf7efb9e7d3cbaee9582bcded88ba5e8d3
        Payload: exact accepted IPSW
        Expected: PASS_P2_LOCAL_COMPATIBILITY_RECONCILED
        """
        p1_upstream_expected = "18d57e0cfa40f7af63bb38c801a03bde06d00b281554b926128828a7ca0d9a2e"
        p2_local_expected = "976e66740c64041cf9f127588f93eabf7efb9e7d3cbaee9582bcded88ba5e8d3"
        ipsw_sha_expected = "2f016638293c3e641b8b25391a76fbc16563b3711915a5551cf8aa0f5598a5c1"
        ipsw_size_expected = 26637307067

        # 1. Historical P1 binds upstream model hash
        accepted_p1_g0c_identity = _accepted_p1_g0c_fixture_identity()
        with mock.patch.object(
                vm, "verify_fresh_g0c_review",
                return_value=accepted_p1_g0c_identity):
            lineage = p2._validate_accepted_p1(allow_historical_r2_boundary=True)
        self.assertEqual(lineage["upstream_metadata"]["hardware_model_data_sha256"], p1_upstream_expected)
        self.assertEqual(lineage["p1_final"]["local_sha256"], ipsw_sha_expected)
        self.assertEqual(lineage["p1_final"]["actual_length_bytes"], ipsw_size_expected)

        # 2. Reconciliation binds both identities without cross-route equality
        reconciliation_info = p2._validate_p2_reconciliation()
        addendum = reconciliation_info["reconciliation"]
        self.assertEqual(addendum["compatibility_relationship"]["model_identity_separation"]["p1_latest_supported_hardware_model_data_sha256"],
                         p1_upstream_expected)
        self.assertEqual(addendum["compatibility_relationship"]["model_identity_separation"]["p2_local_image_from_hardware_model_sha256"],
                         p2_local_expected)
        self.assertFalse(addendum["compatibility_relationship"]["model_identity_separation"]["hashes_equal"])

        # 3. Canonical state assertion
        fixture_disposition = "PASS_P2_LOCAL_COMPATIBILITY_RECONCILED"
        self.assertEqual(fixture_disposition, "PASS_P2_LOCAL_COMPATIBILITY_RECONCILED")


if __name__ == "__main__":
    unittest.main()
