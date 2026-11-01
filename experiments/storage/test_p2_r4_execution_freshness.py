#!/usr/bin/env python3
"""Focused R4 execution-start lease, ordering, and R3 supersession tests."""

from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
import uuid
import argparse
import builtins
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))

import p2_base_orchestrator as p2
import test_vm_capacity_domain as vm_tests
import vm_capacity_domain as vm


class ExecutionStartAgeTests(unittest.TestCase):
    def test_lease_boundary_is_inclusive_and_future_or_old_time_fails(self) -> None:
        now_wall = 1_800_000_000.0
        now_monotonic = 400.0
        cases = (
            (30.0, 30.0, True),
            (30.0001, 30.0001, False),
            (0.0, 0.0, True),
            (-0.001, -0.001, False),
        )
        for wall_age, monotonic_age, expected in cases:
            with self.subTest(wall_age=wall_age, monotonic_age=monotonic_age):
                valid, _ = p2._execution_start_age_valid(
                    now_wall - wall_age, now_unix=now_wall,
                    issued_at_monotonic=now_monotonic - monotonic_age,
                    now_monotonic=now_monotonic)
                self.assertEqual(valid, expected)


class ExecutionStartLeaseValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p2-r4-lease-", dir=HERE)
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.key = vm.policy._manifest_signing_key()
        self.session = uuid.uuid4().hex
        self.run_id = "r4-lease-fixture-" + uuid.uuid4().hex[:8]
        self.admission_path = self.root / "admission.json"
        self.admission_path.write_text("synthetic admission path\n", encoding="utf-8")
        self.manifest_path = self.root / "manifest.json"
        self.manifest_path.write_text("synthetic manifest path\n", encoding="utf-8")
        self.review_path = self.root / "review.json"
        self.review_path.write_text("synthetic review path\n", encoding="utf-8")
        self.restore_path = self.root / "restore.ipsw"
        self.restore_path.write_bytes(b"synthetic immutable restore identity")
        self.restore_sha, self.restore_size, self.restore_identity = p2.sha256_file(self.restore_path)
        self.marker_path = self.root / "active-session.json"
        self.marker = {"session_id": self.session, "state": "ACTIVE",
                       "phase_id": "P2_AUTHORIZED", "volume_uuid": p2.VM_VOLUME_UUID}
        p2.signed_write(self.marker_path, self.marker, "vm_session_hmac_sha256", self.key)
        self.marker_sha = vm._file_sha256(self.marker_path)
        self.source_sha = "a" * 64
        self.adapter_sha = "b" * 64
        self.review_sha = "c" * 64
        self.manifest_sha = "d" * 64
        self.capability_identity = {"device": 11, "inode": 12, "owner_uid": os.geteuid(),
                                    "mode": 0o600, "link_count": 1, "length": 32}
        self.g0c_identity = {"snapshot_sha256": "e" * 64}
        self.admission = {
            "record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
            "run_id": self.run_id,
            "session_id": self.session,
            "created_at_unix": time.time() - 1_258,
            "helper_control_token_sha256": "f" * 64,
            "p2_identity_plan_sha256": p2.P2_ACCEPTED_IDENTITY_PLAN_SHA256,
            "restore_sha256": self.restore_sha,
            "restore_file_identity": self.restore_identity,
            "provisioner_source_sha256": self.source_sha,
            "provisioner_orchestration_source_sha256": self.adapter_sha,
            "g0c_identity": self.g0c_identity,
            "system_raw_path": str(self.root / "guest-base" / "system.raw"),
        }
        self.admission_sha = "1" * 64
        self.evidence = {
            "admission": self.admission,
            "admission_path": self.admission_path,
            "admission_sha256": self.admission_sha,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha,
            "review_path": self.review_path,
            "review_sha256": self.review_sha,
            "active_marker_sha256": self.marker_sha,
            "launch_capability": {"status": "READY",
                                  "commitment_sha256": self.admission["helper_control_token_sha256"],
                                  "file_identity": self.capability_identity},
        }
        self.vm_mount = {
            "volume_uuid": p2.VM_VOLUME_UUID,
            "device_identifier": "disk4s2",
            "container_uuid": p2.CONTAINER_UUID,
            "container_reference": "disk4",
            "mount_point": "/Volumes/VM",
            "mount_state": "MOUNTED_VERIFIED",
            "mounted": True,
            "mount_identity": {
                "mount_table_device": "/dev/disk4s2",
                "mount_table_filesystem_id": [1, 2],
            },
        }
        self.storage_measurement = {
            "safety_volume_uuid": p2.SAFETY_VOLUME_UUID,
            "safety_mount_state": "UNMOUNTED_VERIFIED",
            "safety_volume_capacity_in_use_bytes": 16_106_127_360,
            "safety_volume_reserve_bytes": 15 * p2.GIB,
            "vm_volume_uuid": p2.VM_VOLUME_UUID,
            "vm_volume_quota_bytes": 45 * p2.GIB,
            "vm_volume_usage_bytes": p2.GIB,
            "vm_mount_identity": self.vm_mount["mount_identity"],
            "vm_device_identifier": self.vm_mount["device_identifier"],
            "vm_mount": self.vm_mount,
            "apfs_container_uuid": p2.CONTAINER_UUID,
            "physical_store_uuid": p2.PHYSICAL_STORE_UUID,
        }
        self.storage_ids = {
            "safety_volume_uuid": self.storage_measurement["safety_volume_uuid"],
            "safety_volume_state": self.storage_measurement["safety_mount_state"],
            "safety_volume_capacity_in_use_bytes": self.storage_measurement["safety_volume_capacity_in_use_bytes"],
            "safety_reserve_bytes": self.storage_measurement["safety_volume_reserve_bytes"],
            "vm_volume_uuid": self.storage_measurement["vm_volume_uuid"],
            "vm_volume_quota_bytes": self.storage_measurement["vm_volume_quota_bytes"],
            "vm_volume_usage_bytes": self.storage_measurement["vm_volume_usage_bytes"],
            "vm_mount_identity": self.storage_measurement["vm_mount_identity"],
            "vm_device_identifier": self.storage_measurement["vm_device_identifier"],
            "apfs_container_uuid": self.storage_measurement["apfs_container_uuid"],
            "physical_store_uuid": self.storage_measurement["physical_store_uuid"],
        }
        self.storage_observation = {"apfs_container": {"APFSContainerUUID": p2.CONTAINER_UUID}}
        self.lease = self._lease()
        self.lease_path = self.root / "execution-start.json"
        p2.signed_write(self.lease_path, self.lease,
                        p2.P2_EXECUTION_START_SIGNATURE_FIELD, self.key)
        self.lease_sha = vm._file_sha256(self.lease_path)
        self.issued_monotonic = time.monotonic()

    def _lease(self) -> dict[str, object]:
        return {
            "schema_version": p2.P2_EXECUTION_START_LEASE_VERSION,
            "record_type": "R6_VM_P2_EXECUTION_START_LEASE",
            "protocol_version": "P2_R4_EXECUTION_START_LEASE_V1",
            "authority": p2.P2_EXECUTION_START_AUTHORITY,
            "authority_scope": "EXACT_ADMISSION_STARTUP_ONLY",
            "status": "ISSUED",
            "single_use": True,
            "created_at_unix": time.time(),
            "issued_at_unix": time.time(),
            "maximum_age_seconds": 30,
            "freshness_semantics": "issuance through initial monitor gate and claim",
            "claim_boundary": "after_live_monitor_initial_gate_before_helper_authorization",
            "static_admission_age_limited": False,
            "admission_path": str(self.admission_path.relative_to(REPO)),
            "admission_sha256": self.admission_sha,
            "run_id": self.run_id,
            "session_id": self.session,
            "provenance": {
                "manifest_path": str(self.manifest_path.relative_to(REPO)),
                "manifest_sha256": self.manifest_sha,
                "review_path": str(self.review_path.relative_to(REPO)),
                "review_sha256": self.review_sha,
                "source_sha256": self.source_sha,
                "adapter_sha256": self.adapter_sha,
            },
            "identity_plan_sha256": p2.P2_ACCEPTED_IDENTITY_PLAN_SHA256,
            "active_marker_sha256": self.marker_sha,
            "g0c_identity": self.g0c_identity,
            "capability": {
                "state": "READY",
                "commitment_sha256": self.admission["helper_control_token_sha256"],
                "file_identity": self.capability_identity,
            },
            "restore": {"path": str(self.restore_path), "sha256": self.restore_sha,
                        "logical_bytes": self.restore_size,
                        "file_identity": self.restore_identity},
            "storage_identities": self.storage_ids,
            "storage_measurement": self.storage_measurement,
            "storage_observation": self.storage_observation,
            "p2_demand": {"p2_growth_bytes": p2.GIB},
            "r4_gate": {"decision": "PASS"},
            "guest_assets": {"present": False, "assets": []},
            "process_scan": {"blocking_processes": []},
        }

    def validate(self, lease: dict[str, object] | None = None) -> tuple[dict[str, object], str]:
        value = self.lease if lease is None else lease
        if self.lease_path.exists():
            self.lease_path.unlink()
        p2.signed_write(self.lease_path, value,
                        p2.P2_EXECUTION_START_SIGNATURE_FIELD, self.key)
        self.lease_sha = vm._file_sha256(self.lease_path)
        with (mock.patch.object(p2, "RESTORE_PATH", self.restore_path),
              mock.patch.object(p2, "RESTORE_SIZE", self.restore_size),
              mock.patch.object(p2, "RESTORE_SHA256", self.restore_sha),
              mock.patch.object(p2.vm, "VM_ACTIVE_MARKER_PATH", self.marker_path),
              mock.patch.object(p2.vm, "verify_fresh_g0c_review", return_value=self.g0c_identity),
              mock.patch.object(p2, "_p2_process_scan_evidence",
                               return_value={"blocking_processes": []}),
              mock.patch.object(p2, "_validate_launch_capability_contract",
                               return_value={"status": "READY",
                                             "file_identity": self.capability_identity})):
            return p2._validate_execution_start_lease(
                self.lease_path, self.evidence, signing_key=self.key,
                lease_sha256=self.lease_sha, issued_at_monotonic=self.issued_monotonic)

    def test_old_static_admission_with_fresh_lease_passes(self) -> None:
        self.assertGreater(time.time() - self.admission["created_at_unix"], 1_200)
        lease, digest = self.validate()
        self.assertEqual(digest, self.lease_sha)
        self.assertEqual(lease["r4_gate"]["decision"], "PASS")

    def test_expired_lease_and_exact_30_second_lease_boundary(self) -> None:
        expired = self._lease()
        expired["issued_at_unix"] = time.time() - 30.01
        expired["created_at_unix"] = expired["issued_at_unix"]
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self.validate(expired)
        valid, _ = p2._execution_start_age_valid(
            970.0, now_unix=1_000.0, issued_at_monotonic=500.0,
            now_monotonic=530.0)
        self.assertTrue(valid)

    def test_different_admission_capability_marker_or_provenance_fails(self) -> None:
        mutations = {
            "admission": lambda row: row.update(admission_sha256="9" * 64),
            "capability": lambda row: row["capability"].update(commitment_sha256="9" * 64),
            "marker": lambda row: row.update(active_marker_sha256="9" * 64),
            "provenance": lambda row: row["provenance"].update(adapter_sha256="9" * 64),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                lease = self._lease()
                mutate(lease)
                with self.assertRaises(RuntimeError):
                    self.validate(lease)

    def test_changed_restore_and_storage_identity_fail(self) -> None:
        bad_restore = self._lease()
        bad_restore["restore"]["sha256"] = "9" * 64
        with self.assertRaises(RuntimeError):
            self.validate(bad_restore)
        bad_storage = self._lease()
        bad_storage["storage_identities"]["vm_volume_uuid"] = "9" * 36
        with self.assertRaisesRegex(RuntimeError, "storage identity"):
            self.validate(bad_storage)

    def test_changed_restore_file_after_lease_fails(self) -> None:
        self.restore_path.write_bytes(b"changed restore bytes after lease")
        with self.assertRaisesRegex(RuntimeError, "restore identity changed"):
            self.validate()


class PreclaimClaimOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p2-r4-claim-", dir=HERE)
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.control_root = self.root / "control-root"
        self.control_root.mkdir(mode=0o700)
        self.key = vm.policy._manifest_signing_key()
        self.session = uuid.uuid4().hex
        self.token = os.urandom(32)
        self.commitment = hashlib.sha256(self.token).hexdigest()
        self.admission_path = self.root / "R6-VM-P2-ADMISSION-synthetic.json"
        self.record = {
            "record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
            "run_id": "synthetic-r4-" + uuid.uuid4().hex[:8],
            "session_id": self.session,
            "created_at_unix": time.time() - 1_258,
            "decision": "PASS", "authorized_phase": "P2",
            "authority_scope": "PROVISION_MACOS_GUEST_BASE_ONLY",
            "p2_authorized": True,
            "vm_start_authorized_for_installation_only": True,
            "runtime_feasibility_authorized": False,
            "serial_runtime_testing_authorized": False,
            "r6_execution_authorized": False,
            "execution_freshness_protocol": {
                "version": "P2_R4_EXECUTION_START_LEASE_V1",
                "static_admission_age_limited": False,
                "age_subject": "execution_start_lease",
                "maximum_start_age_seconds": 30,
                "claim_boundary": "after_live_monitor_initial_gate_before_helper_authorization",
            },
            "helper_control_token_sha256": self.commitment,
            "launch_capability": {
                "format_version": p2.P2_LAUNCH_CAPABILITY_FORMAT_VERSION,
                "commitment_sha256": self.commitment,
                "session_id": self.session,
                "private_store": p2.P2_LAUNCH_CAPABILITY_STORE_ID,
                "path_derivation": "<session_id>/<admission_sha256>.ready",
                "state": "READY", "single_use": True,
            },
        }
        self.record = p2.signed_write(
            self.admission_path, self.record, "vm_admission_hmac_sha256", self.key)
        self.admission_sha = vm._file_sha256(self.admission_path)
        p2._write_launch_capability_ready(
            self.session, self.admission_sha, self.token, control_root=self.control_root)
        self.capability = p2._validate_launch_capability_contract(
            self.record, self.admission_sha, control_root=self.control_root)
        self.marker_path = self.root / "active-marker.json"
        self.marker = {"session_id": self.session, "state": "ACTIVE",
                       "phase_id": "P2_AUTHORIZED"}
        p2.signed_write(self.marker_path, self.marker, "vm_session_hmac_sha256", self.key)
        self.marker_sha = vm._file_sha256(self.marker_path)
        self.evidence = {
            "admission": self.record, "admission_path": self.admission_path,
            "admission_sha256": self.admission_sha,
            "manifest_path": self.root / "manifest.json", "manifest_sha256": "a" * 64,
            "review_path": self.root / "review.json", "review_sha256": "b" * 64,
            "active_marker_sha256": self.marker_sha,
            "launch_capability": self.capability,
        }
        for name in ("manifest.json", "review.json"):
            (self.root / name).write_text("synthetic\n", encoding="utf-8")
        self.vm_mount = {
            "volume_uuid": p2.VM_VOLUME_UUID,
            "device_identifier": "disk3s7",
            "container_uuid": p2.CONTAINER_UUID,
            "container_reference": "disk3",
            "mount_point": "/Volumes/FGMetal-VM",
            "mount_state": "MOUNTED_VERIFIED",
            "mounted": True,
            "mount_identity": {
                "mount_table_device": "/dev/disk3s7",
                "mount_table_filesystem_id": [73, 19],
            },
        }
        self.lease = {"issued_at_unix": time.time(),
                      "storage_measurement": {"vm_volume_usage_bytes": 0,
                                               "vm_mount": self.vm_mount},
                      "p2_demand": {"p2_growth_bytes": p2.GIB},
                      "capability": {"file_identity": self.capability["file_identity"]}}
        self.lease_path = self.root / "execution-start.json"
        p2.signed_write(self.lease_path, self.lease,
                        p2.P2_EXECUTION_START_SIGNATURE_FIELD, self.key)
        self.lease_sha = vm._file_sha256(self.lease_path)
        p2._write_execution_start_lease_ready(
            self.session, self.lease_sha, control_root=self.control_root)
        self.issued_monotonic = time.monotonic()
        self.execution_claimed = {"value": False}

    def _claim(self, *, ready: bool = True, error: object = None,
               clock=time, issued_at_monotonic: float | None = None):
        host_gate = threading.Event()
        if ready:
            host_gate.set()
        thread = mock.Mock()
        thread.is_alive.return_value = True
        evidence = dict(self.evidence)
        with (mock.patch.object(p2, "_validate_execution_entry", return_value=evidence),
              mock.patch.object(p2, "_validate_execution_start_lease",
                               return_value=(self.lease, self.lease_sha)),
              mock.patch.object(p2.vm, "VM_ACTIVE_MARKER_PATH", self.marker_path),
              mock.patch.object(p2.vm, "_check_runtime_phase_sample",
                                return_value={"admitted": True, "blockers": []}),
              mock.patch.object(p2, "time", clock)):
            state = {"error": error}
            return p2._claim_p2_after_initial_monitor_gate(
                admission_path=self.admission_path,
                lease_path=self.root / "execution-start.json",
                lease_sha256=self.lease_sha,
                issued_at_monotonic=(self.issued_monotonic if issued_at_monotonic is None
                                     else issued_at_monotonic), signing_key=self.key,
                control_root=self.control_root, host_monitor_ready=host_gate,
                monitor_thread=thread, monitor_state=state,
                monitor_ready_evidence={"validated_sample_count": 3,
                                        "last_validated_at_monotonic": clock.monotonic()},
                boundary_lock=threading.Lock(),
                boundary_sample=lambda: {"vm_volume_mount": self.vm_mount},
                execution_claimed_state=self.execution_claimed)

    def test_initial_monitor_failure_stops_before_any_claim(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "initial gate"):
            self._claim(error=RuntimeError("capacity failed"))
        self.assertEqual(p2._validate_launch_capability_contract(
            self.record, self.admission_sha, control_root=self.control_root)["status"], "READY")
        p2._verify_execution_start_lease_ready(
            self.session, self.lease_sha, control_root=self.control_root)

    def test_monitor_startup_failure_leaves_capability_ready(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "initial gate"):
            self._claim(ready=False)
        self.assertEqual(p2._validate_launch_capability_contract(
            self.record, self.admission_sha, control_root=self.control_root)["status"], "READY")

    def test_execution_start_lease_state_fsync_failure_leaves_capability_ready(self) -> None:
        snapshot = {
            "provenance": {"manifest_path": str(self.evidence["manifest_path"].relative_to(REPO)),
                           "manifest_sha256": self.evidence["manifest_sha256"],
                           "review_path": str(self.evidence["review_path"].relative_to(REPO)),
                           "review_sha256": self.evidence["review_sha256"],
                           "source_sha256": "a" * 64, "adapter_sha256": "b" * 64},
            "identity_plan_sha256": p2.P2_ACCEPTED_IDENTITY_PLAN_SHA256,
            "active_marker_sha256": self.marker_sha,
            "g0c_identity": {},
            "capability": {"file_identity": self.capability["file_identity"]},
            "restore": {"path": str(p2.RESTORE_PATH), "sha256": p2.RESTORE_SHA256,
                        "logical_bytes": p2.RESTORE_SIZE,
                        "file_identity": {"device": 1, "inode": 2}},
            "storage_identities": {}, "storage_observation": {},
            "storage_measurement": {}, "metadata_inventory": {},
            "p2_demand": {}, "r4_gate": {}, "guest_assets": {}, "process_scan": {},
        }
        real_fsync = os.fsync
        fsync_calls = 0

        def fail_lease_state_fsync(fd: int) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 3:
                raise OSError("injected execution-start READY-state fsync failure")
            real_fsync(fd)

        with (mock.patch.object(p2, "REPO", self.root),
              mock.patch.object(p2, "unique_path", return_value=self.root / "lease.json"),
              mock.patch.object(p2, "_fresh_p2_execution_start_snapshot", return_value=snapshot),
              mock.patch.object(p2.os, "fsync", side_effect=fail_lease_state_fsync)):
            with self.assertRaisesRegex(OSError, "READY-state fsync"):
                p2._create_execution_start_lease(
                    self.evidence, signing_key=self.key, control_root=self.control_root)
        self.assertEqual(fsync_calls, 3)
        self.assertEqual(p2._validate_launch_capability_contract(
            self.record, self.admission_sha, control_root=self.control_root)["status"], "READY")
        self.assertIsNone(next(self.control_root.rglob("*.claimed"), None))

    def test_non_consuming_validation_requires_future_lease_without_issuing_it(self) -> None:
        args = argparse.Namespace(admission=str(self.admission_path), validate_only=True)
        with (mock.patch.object(p2, "_validate_execution_entry", return_value=self.evidence),
              mock.patch.object(p2, "_create_execution_start_lease") as create_lease,
              mock.patch.object(p2, "_claim_launch_capability") as claim_capability,
              mock.patch.object(builtins, "print")):
            result = p2._resume_existing_admission(
                args, signing_key=self.key, control_root=self.control_root)
        self.assertEqual(result["execution_entry"], "READY_BEFORE_ATOMIC_CLAIM")
        self.assertEqual(result["execution_start_lease"], "REQUIRED_NOT_ISSUED")
        self.assertEqual(result["launch_capability_state"], "READY")
        create_lease.assert_not_called()
        claim_capability.assert_not_called()

    def test_marker_or_capability_change_fails_before_claim(self) -> None:
        changed = dict(self.marker, phase_id="P1_VERIFIED")
        self.marker_path.unlink()
        p2.signed_write(self.marker_path, changed, "vm_session_hmac_sha256", self.key)
        with self.assertRaisesRegex(RuntimeError, "marker changed"):
            self._claim()
        self.marker_path.unlink()
        p2.signed_write(self.marker_path, self.marker, "vm_session_hmac_sha256", self.key)
        cap_path = pathlib.Path(self.capability["ready_path"])
        cap_path.write_bytes(b"x" * 32)
        with self.assertRaises(RuntimeError):
            self._claim()

    def test_successful_initial_gate_claims_each_single_use_state_once(self) -> None:
        result = self._claim()
        self.assertEqual(result["launch_claim"]["status"], "CLAIMED")
        self.assertEqual(result["lease_claim"]["status"], "CLAIMED")
        self.assertTrue(self.execution_claimed["value"])
        self.assertFalse(next(self.control_root.rglob("*.ready"), None))
        self.assertEqual(len(list(self.control_root.rglob("*.claimed"))), 2)
        with self.assertRaises(RuntimeError):
            self._claim()

    def test_competing_capability_claim_is_not_misattributed_to_this_attempt(self) -> None:
        actual_claim = p2._claim_launch_capability

        def competing_claim(*args, **kwargs):
            # Model another provisioner winning the rename between this
            # attempt's READY validation and its own exclusive rename.
            kwargs.pop("claim_state", None)
            actual_claim(*args, **kwargs)
            raise FileExistsError("another provisioner won the exclusive claim")

        with mock.patch.object(p2, "_claim_launch_capability", side_effect=competing_claim):
            with self.assertRaisesRegex(FileExistsError, "another provisioner"):
                self._claim()
        self.assertFalse(self.execution_claimed["value"])
        state_fd = p2._open_launch_capability_session_directory(
            self.session, control_root=self.control_root, create=False)
        try:
            status = p2._capability_file_state(
                state_fd, self.session, self.admission_sha)["status"]
        finally:
            os.close(state_fd)
        self.assertEqual(status, "CLAIMED")

    def test_successful_rename_owns_claim_even_if_directory_fsync_fails(self) -> None:
        actual_fsync = os.fsync
        fsync_calls = 0

        def fail_capability_claim_fsync(descriptor: int) -> None:
            nonlocal fsync_calls
            fsync_calls += 1
            if fsync_calls == 2:
                raise OSError("injected post-rename directory fsync failure")
            actual_fsync(descriptor)

        with mock.patch.object(p2.os, "fsync", side_effect=fail_capability_claim_fsync):
            with self.assertRaisesRegex(OSError, "post-rename"):
                self._claim()
        self.assertEqual(fsync_calls, 2)
        self.assertTrue(self.execution_claimed["value"])
        state_fd = p2._open_launch_capability_session_directory(
            self.session, control_root=self.control_root, create=False)
        try:
            status = p2._capability_file_state(
                state_fd, self.session, self.admission_sha)["status"]
        finally:
            os.close(state_fd)
        self.assertEqual(status, "CLAIMED")

    def test_lease_age_is_rechecked_after_capability_io_immediately_before_rename(self) -> None:
        class ControlledClock:
            def __init__(self, issued_unix: float, issued_monotonic: float) -> None:
                self.wall = issued_unix + 29.99
                self.monotonic_value = issued_monotonic + 29.99

            def time(self) -> float:
                return self.wall

            def monotonic(self) -> float:
                return self.monotonic_value

            def advance(self, seconds: float) -> None:
                self.wall += seconds
                self.monotonic_value += seconds

        clock = ControlledClock(float(self.lease["issued_at_unix"]), self.issued_monotonic)
        actual_rename = p2._rename_launch_capability_exclusive
        rename_calls = 0

        def delay_before_capability_rename(*args, **kwargs) -> None:
            nonlocal rename_calls
            rename_calls += 1
            if rename_calls == 2:
                clock.advance(0.02)
            actual_rename(*args, **kwargs)

        with mock.patch.object(p2, "_rename_launch_capability_exclusive",
                               side_effect=delay_before_capability_rename):
            with self.assertRaisesRegex(RuntimeError, "expired at the atomic capability claim boundary"):
                self._claim(clock=clock)
        self.assertEqual(rename_calls, 2)
        self.assertFalse(self.execution_claimed["value"])
        state_fd = p2._open_launch_capability_session_directory(
            self.session, control_root=self.control_root, create=False)
        try:
            status = p2._capability_file_state(
                state_fd, self.session, self.admission_sha)["status"]
        finally:
            os.close(state_fd)
        self.assertEqual(status, "READY")

    def test_monitor_cadence_is_rechecked_immediately_before_atomic_rename(self) -> None:
        class ControlledClock:
            def __init__(self, issued_unix: float, issued_monotonic: float) -> None:
                self.wall = issued_unix + 1.0
                self.monotonic_value = issued_monotonic + 1.0

            def time(self) -> float:
                return self.wall

            def monotonic(self) -> float:
                return self.monotonic_value

            def advance(self, seconds: float) -> None:
                self.wall += seconds
                self.monotonic_value += seconds

        clock = ControlledClock(float(self.lease["issued_at_unix"]), self.issued_monotonic)
        actual_rename = p2._rename_launch_capability_exclusive
        rename_calls = 0

        def delay_before_capability_rename(*args, **kwargs) -> None:
            nonlocal rename_calls
            rename_calls += 1
            if rename_calls == 2:
                clock.advance(p2.vm.VM_PHASE_MONITOR_MAX_INTERVAL_SECONDS + 0.01)
            actual_rename(*args, **kwargs)

        with mock.patch.object(p2, "_rename_launch_capability_exclusive",
                               side_effect=delay_before_capability_rename):
            with self.assertRaisesRegex(RuntimeError, "monitor sample exceeded its cadence"):
                self._claim(clock=clock)
        self.assertEqual(rename_calls, 2)
        self.assertFalse(self.execution_claimed["value"])
        state_fd = p2._open_launch_capability_session_directory(
            self.session, control_root=self.control_root, create=False)
        try:
            status = p2._capability_file_state(
                state_fd, self.session, self.admission_sha)["status"]
        finally:
            os.close(state_fd)
        self.assertEqual(status, "READY")

    def test_lease_expiry_after_successful_rename_is_postclaim_and_prestart(self) -> None:
        class ControlledClock:
            def __init__(self, issued_unix: float, issued_monotonic: float) -> None:
                self.wall = issued_unix + 29.99
                self.monotonic_value = issued_monotonic + 29.99

            def time(self) -> float:
                return self.wall

            def monotonic(self) -> float:
                return self.monotonic_value

            def advance(self, seconds: float) -> None:
                self.wall += seconds
                self.monotonic_value += seconds

        clock = ControlledClock(float(self.lease["issued_at_unix"]), self.issued_monotonic)
        actual_claim = p2._claim_launch_capability

        def expire_after_rename(*args, **kwargs):
            after_claim = kwargs["after_claim"]

            def delayed_postclaim_check() -> None:
                clock.advance(0.02)
                after_claim()

            kwargs["after_claim"] = delayed_postclaim_check
            return actual_claim(*args, **kwargs)

        with mock.patch.object(p2, "_claim_launch_capability", side_effect=expire_after_rename):
            with self.assertRaisesRegex(RuntimeError, "expired during the atomic capability claim"):
                self._claim(clock=clock)
        self.assertTrue(self.execution_claimed["value"])
        state_fd = p2._open_launch_capability_session_directory(
            self.session, control_root=self.control_root, create=False)
        try:
            status = p2._capability_file_state(
                state_fd, self.session, self.admission_sha)["status"]
        finally:
            os.close(state_fd)
        self.assertEqual(status, "CLAIMED")


class CapabilitySupersessionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p2-r4-supersession-", dir=HERE)
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.control_root = self.root / "control-root"
        self.control_root.mkdir(mode=0o700)
        self.key = vm.policy._manifest_signing_key()
        self.session = uuid.uuid4().hex
        self.token = os.urandom(32)
        self.commitment = hashlib.sha256(self.token).hexdigest()
        self.admission_path = self.root / "R6-VM-P2-ADMISSION-old.json"
        self.record = {
            "record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
            "run_id": "old-r3-" + uuid.uuid4().hex[:8],
            "session_id": self.session,
            "decision": "PASS", "authorized_phase": "P2",
            "authority_scope": "PROVISION_MACOS_GUEST_BASE_ONLY",
            "helper_control_token_sha256": self.commitment,
            "provisioner_source_manifest_sha256": "1" * 64,
            "provisioner_review_sha256": "2" * 64,
            "p2_identity_plan_sha256": "3" * 64,
            "p2_preflight_sha256": "4" * 64,
            "restore_sha256": p2.RESTORE_SHA256,
            "restore_file_identity": {"device": 1, "inode": 2, "logical_bytes": p2.RESTORE_SIZE},
            "launch_capability": {
                "format_version": p2.P2_LAUNCH_CAPABILITY_FORMAT_VERSION,
                "commitment_sha256": self.commitment,
                "session_id": self.session,
                "private_store": p2.P2_LAUNCH_CAPABILITY_STORE_ID,
                "path_derivation": "<session_id>/<admission_sha256>.ready",
                "state": "READY", "single_use": True,
            },
        }
        self.record = p2.signed_write(
            self.admission_path, self.record, "vm_admission_hmac_sha256", self.key)
        self.admission_sha = vm._file_sha256(self.admission_path)
        p2._write_launch_capability_ready(
            self.session, self.admission_sha, self.token, control_root=self.control_root)
        self.implementation_review = self.root / "implementation-review.md"
        self.implementation_review.write_text(
            "PASS — P2 R4 EXECUTION FRESHNESS IMPLEMENTATION ACCEPTED\n", encoding="utf-8")
        self.manifest_path = self.root / "r4-manifest.json"
        self.manifest_path.write_text("synthetic R4 source manifest\n", encoding="utf-8")
        self.provenance_review_path = self.root / "r4-provenance-review.json"
        self.provenance_review_path.write_text("synthetic signed review placeholder\n", encoding="utf-8")

    def test_ready_capability_is_atomically_superseded_and_commitment_preserved(self) -> None:
        result = p2._supersede_launch_capability(
            self.session, self.admission_sha, self.commitment, control_root=self.control_root)
        self.assertEqual(result["status"], "SUPERSEDED")
        self.assertEqual(result["commitment_sha256"], self.commitment)
        self.assertTrue(pathlib.Path(result["superseded_path"]).exists())
        self.assertFalse(pathlib.Path(result["superseded_path"].replace(".superseded", ".ready")).exists())

    def test_wrong_admission_and_already_claimed_capability_cannot_be_superseded(self) -> None:
        with self.assertRaises(RuntimeError):
            p2._supersede_launch_capability(
                self.session, hashlib.sha256(b"wrong admission").hexdigest(),
                self.commitment, control_root=self.control_root)
        p2._claim_launch_capability(
            self.session, self.admission_sha, self.commitment, control_root=self.control_root)
        with self.assertRaisesRegex(RuntimeError, "READY"):
            p2._supersede_launch_capability(
                self.session, self.admission_sha, self.commitment, control_root=self.control_root)

    def test_supersession_receipt_rejects_bad_binding_and_never_contains_token(self) -> None:
        state = p2._supersede_launch_capability(
            self.session, self.admission_sha, self.commitment, control_root=self.control_root)
        with mock.patch.object(p2, "REPO", self.root):
            receipt_path = self.root / "R6-VM-P2-R3-EXECUTION-PACKET-SUPERSESSION-r4-test.json"
            historical_report = self.root / (
                f"R6-VM-P2-R3-EXECUTION-RESUMABILITY-REPAIR-FINAL-{p2.P2_R3_RUN_ID}.md")
            historical_report.write_text("preserved R3 report fixture\n", encoding="utf-8")
            receipt = {
                "record_type": "R6_VM_P2_R3_EXECUTION_PACKET_SUPERSESSION",
                "schema_version": 1,
                "protocol_version": "P2_R4_EXECUTION_START_LEASE_V1",
                "status": "PASS",
                "classification": "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM",
                "authority_scope": "NONE", "execution_authority": "NONE", "authority": "NONE",
                "r4_run_id": "r4-test",
                "r3_admission_path": str(self.admission_path.relative_to(self.root)),
                "r3_admission_sha256": self.admission_sha,
                "r3_run_id": self.record["run_id"], "r3_session_id": self.session,
                "r3_admission_signature_valid": True,
                "r3_source_manifest_sha256": self.record["provisioner_source_manifest_sha256"],
                "r3_provisioner_review_sha256": self.record["provisioner_review_sha256"],
                "r3_identity_plan_sha256": self.record["p2_identity_plan_sha256"],
                "r3_preflight_sha256": self.record["p2_preflight_sha256"],
                "r3_capability_commitment_sha256": self.commitment,
                "r4_implementation_review": {
                    "path": str(self.implementation_review.relative_to(self.root)),
                    "sha256": vm._file_sha256(self.implementation_review),
                    "accepted_decision": "PASS — P2 R4 EXECUTION FRESHNESS IMPLEMENTATION ACCEPTED",
                },
                "r4_provenance": {
                    "manifest_path": str(self.manifest_path.relative_to(self.root)),
                    "manifest_sha256": vm._file_sha256(self.manifest_path),
                    "review_path": str(self.provenance_review_path.relative_to(self.root)),
                    "review_sha256": vm._file_sha256(self.provenance_review_path),
                    "unchanged_helper_relationship_status": "PASS",
                },
                "capability_state_before": "READY", "claimed_state_absent_before": True,
                "capability_state_after": "SUPERSEDED", "capability_commitment_verified_after": True,
                "observed_source_ordering_defect": True,
                "source_ordering_defect": (
                    "R3 consumed the private READY capability before the controller rejected the stale static admission at monitor initialization."),
                "stale_admission_blocker": {
                    "admission_age_seconds_at_retirement": 1_258,
                    "monitor_maximum_age_seconds": 30,
                    "static_admission_remains_signed_pass": True,
                },
                "private_capability_state": {
                    "path_suffix": ".superseded", "status": "SUPERSEDED",
                    "commitment_sha256": self.commitment, "file_identity": state["file_identity"],
                },
                "guest_mutation_occurred": False,
                "guest_assets_present_before": False, "guest_assets_present_after": False,
                "active_marker_sha256_before": p2.P2_R3_ACTIVE_MARKER_SHA256,
                "active_marker_sha256_after": p2.P2_R3_ACTIVE_MARKER_SHA256,
                "blocking_processes_before": [],
                "restore": {"path": str(p2.RESTORE_PATH),
                            "sha256_before": p2.RESTORE_SHA256,
                            "sha256_after": p2.RESTORE_SHA256,
                            "logical_bytes": p2.RESTORE_SIZE,
                            "file_identity": self.record["restore_file_identity"]},
                "r3_final_report": {"path": str(historical_report.relative_to(self.root)),
                                    "sha256": vm._file_sha256(historical_report)},
                "cross_session_marker_transition": False,
                "raw_capability_bytes_included": False,
            }
            p2.signed_write(receipt_path, receipt,
                            p2.P2_EXECUTION_PACKET_SUPERSESSION_SIGNATURE_FIELD, self.key)
            verified, _ = p2._validate_supersession_receipt_for_admission(
                receipt_path, self.admission_path, self.record, self.admission_sha,
                signing_key=self.key, control_root=self.control_root)
            raw = receipt_path.read_bytes()
            self.assertNotIn(self.token, raw)
            self.assertNotIn(self.token.hex().encode(), raw)
            self.assertEqual(verified["authority"], "NONE")
            self.assertEqual(vm._file_sha256(self.admission_path), self.admission_sha)
            with self.assertRaisesRegex(RuntimeError, "superseded before claim"):
                p2._assert_admission_not_superseded(
                    self.admission_path, self.record, self.admission_sha,
                    signing_key=self.key, control_root=self.control_root)

            bad_receipt = dict(receipt, r3_admission_sha256="9" * 64)
            receipt_path.unlink()
            p2.signed_write(receipt_path, bad_receipt,
                            p2.P2_EXECUTION_PACKET_SUPERSESSION_SIGNATURE_FIELD, self.key)
            with self.assertRaisesRegex(RuntimeError, "malformed"):
                p2._validate_supersession_receipt_for_admission(
                    receipt_path, self.admission_path, self.record, self.admission_sha,
                    signing_key=self.key, control_root=self.control_root)


class SupersessionUniquenessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="p2-r4-uniqueness-", dir=HERE)
        self.addCleanup(self.temporary.cleanup)
        self.root = pathlib.Path(self.temporary.name)
        self.control_root = self.root / "control-root"
        self.control_root.mkdir(mode=0o700)
        self.key = vm.policy._manifest_signing_key()
        self.session = uuid.uuid4().hex
        self.old_path = self.root / f"R6-VM-P2-ADMISSION-{p2.P2_R3_RUN_ID}.json"
        self.old_token = os.urandom(32)
        self.old_commitment = hashlib.sha256(self.old_token).hexdigest()
        self.old_record = {
            "record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
            "run_id": p2.P2_R3_RUN_ID,
            "session_id": self.session,
            "decision": "PASS", "authorized_phase": "P2",
            "helper_control_token_sha256": self.old_commitment,
            "provisioner_source_manifest_sha256": "1" * 64,
            "provisioner_review_sha256": "2" * 64,
            "p2_identity_plan_sha256": "3" * 64,
            "p2_preflight_sha256": "4" * 64,
            "restore_sha256": p2.RESTORE_SHA256,
            "restore_file_identity": {"device": 1, "inode": 2,
                                      "logical_bytes": p2.RESTORE_SIZE},
        }
        self.old_record = p2.signed_write(
            self.old_path, self.old_record, "vm_admission_hmac_sha256", self.key)
        self.old_sha = vm._file_sha256(self.old_path)
        p2._write_launch_capability_ready(
            self.session, self.old_sha, self.old_token, control_root=self.control_root)
        self.new_path = self.root / "R6-VM-P2-ADMISSION-r4-current.json"
        self.new_record = p2.signed_write(
            self.new_path, {"record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
                            "run_id": "r4-current", "session_id": self.session,
                            "decision": "PASS", "authorized_phase": "P2"},
            "vm_admission_hmac_sha256", self.key)
        self.new_sha = vm._file_sha256(self.new_path)
        self.report_path = self.root / (
            f"R6-VM-P2-R3-EXECUTION-RESUMABILITY-REPAIR-FINAL-{p2.P2_R3_RUN_ID}.md")
        self.report_path.write_text("preserved R3 report fixture\n", encoding="utf-8")
        self.implementation_review_path = self.root / "r4-implementation-review.md"
        self.implementation_review_path.write_text(
            "PASS — P2 R4 EXECUTION FRESHNESS IMPLEMENTATION ACCEPTED\n", encoding="utf-8")
        self.manifest_path = self.root / "r4-manifest.json"
        self.manifest_path.write_text("R4 manifest fixture\n", encoding="utf-8")
        self.review_path = self.root / "r4-provenance-review.json"
        self.review_path.write_text("R4 review fixture\n", encoding="utf-8")

    def _write_valid_supersession(self) -> tuple[pathlib.Path, dict[str, object]]:
        state = p2._supersede_launch_capability(
            self.session, self.old_sha, self.old_commitment,
            control_root=self.control_root)
        receipt_path = self.root / (
            "R6-VM-P2-R3-EXECUTION-PACKET-SUPERSESSION-r4-current.json")
        receipt: dict[str, object] = {
            "record_type": "R6_VM_P2_R3_EXECUTION_PACKET_SUPERSESSION",
            "schema_version": 1,
            "protocol_version": "P2_R4_EXECUTION_START_LEASE_V1",
            "status": "PASS",
            "classification": "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM",
            "authority": "NONE", "authority_scope": "NONE", "execution_authority": "NONE",
            "r4_run_id": "r4-current",
            "r3_admission_path": str(self.old_path.relative_to(self.root)),
            "r3_admission_sha256": self.old_sha,
            "r3_run_id": self.old_record["run_id"], "r3_session_id": self.session,
            "r3_admission_signature_valid": True,
            "r3_source_manifest_sha256": self.old_record["provisioner_source_manifest_sha256"],
            "r3_provisioner_review_sha256": self.old_record["provisioner_review_sha256"],
            "r3_identity_plan_sha256": self.old_record["p2_identity_plan_sha256"],
            "r3_preflight_sha256": self.old_record["p2_preflight_sha256"],
            "r3_capability_commitment_sha256": self.old_commitment,
            "capability_state_before": "READY", "claimed_state_absent_before": True,
            "capability_state_after": "SUPERSEDED", "capability_commitment_verified_after": True,
            "observed_source_ordering_defect": True,
            "source_ordering_defect": (
                "R3 consumed the private READY capability before the controller rejected the stale static admission at monitor initialization."),
            "stale_admission_blocker": {"monitor_maximum_age_seconds": 30,
                                        "static_admission_remains_signed_pass": True},
            "r4_implementation_review": {
                "path": str(self.implementation_review_path.relative_to(self.root)),
                "sha256": vm._file_sha256(self.implementation_review_path),
                "accepted_decision": "PASS — P2 R4 EXECUTION FRESHNESS IMPLEMENTATION ACCEPTED",
            },
            "r4_provenance": {
                "manifest_path": str(self.manifest_path.relative_to(self.root)),
                "manifest_sha256": vm._file_sha256(self.manifest_path),
                "review_path": str(self.review_path.relative_to(self.root)),
                "review_sha256": vm._file_sha256(self.review_path),
                "unchanged_helper_relationship_status": "PASS",
            },
            "private_capability_state": {
                "status": "SUPERSEDED", "path_suffix": ".superseded",
                "commitment_sha256": self.old_commitment,
                "file_identity": state["file_identity"],
            },
            "guest_mutation_occurred": False,
            "guest_assets_present_before": False, "guest_assets_present_after": False,
            "active_marker_sha256_before": p2.P2_R3_ACTIVE_MARKER_SHA256,
            "active_marker_sha256_after": p2.P2_R3_ACTIVE_MARKER_SHA256,
            "blocking_processes_before": [],
            "restore": {"path": str(p2.RESTORE_PATH),
                        "sha256_before": p2.RESTORE_SHA256,
                        "sha256_after": p2.RESTORE_SHA256,
                        "logical_bytes": p2.RESTORE_SIZE,
                        "file_identity": self.old_record["restore_file_identity"]},
            "r3_final_report": {"path": str(self.report_path.relative_to(self.root)),
                                "sha256": vm._file_sha256(self.report_path)},
            "cross_session_marker_transition": False,
            "raw_capability_bytes_included": False,
        }
        p2.signed_write(receipt_path, receipt,
                        p2.P2_EXECUTION_PACKET_SUPERSESSION_SIGNATURE_FIELD, self.key)
        return receipt_path, receipt

    def test_unsuperseded_pass_still_blocks_same_session_duplicate(self) -> None:
        with mock.patch.object(p2, "REPO", self.root):
            with self.assertRaisesRegex(RuntimeError, "unsuperseded PASS"):
                p2._assert_single_r3_pass_admission(self.session, signing_key=self.key,
                                                    control_root=self.control_root)

    def test_superseded_r3_no_longer_blocks_r4_and_r2_lineage_is_ignored(self) -> None:
        receipt_path, receipt = self._write_valid_supersession()
        r2_path = self.root / "R6-VM-P2-ADMISSION-historical-r2.json"
        r2_record = p2.signed_write(
            r2_path, {"record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
                      "run_id": "historical-r2", "session_id": self.session,
                      "decision": "PASS", "authorized_phase": "P2"},
            "vm_admission_hmac_sha256", self.key)
        with (mock.patch.object(p2, "REPO", self.root),
              mock.patch.object(p2, "P2_R2_ADMISSION_PATH", r2_path),
              mock.patch.object(p2, "P2_R2_ADMISSION_SHA256", vm._file_sha256(r2_path))):
            p2._assert_single_r3_pass_admission(
                self.session, current_admission_path=self.new_path,
                current_admission_sha256=self.new_sha, signing_key=self.key,
                control_root=self.control_root)
            self.assertEqual(receipt["r4_run_id"], "r4-current")
            self.assertEqual(r2_record["run_id"], "historical-r2")
            self.assertTrue(receipt_path.exists())

    def test_malformed_supersession_evidence_fails_closed_in_uniqueness_scan(self) -> None:
        receipt_path = self.root / "R6-VM-P2-R3-EXECUTION-PACKET-SUPERSESSION-bad.json"
        malformed = {"record_type": "R6_VM_P2_R3_EXECUTION_PACKET_SUPERSESSION",
                     "r4_run_id": "bad", "r3_admission_sha256": self.old_sha,
                     "r3_admission_path": str(self.old_path.relative_to(self.root)),
                     "r3_session_id": self.session, "r3_run_id": self.old_record["run_id"]}
        p2.signed_write(receipt_path, malformed,
                        p2.P2_EXECUTION_PACKET_SUPERSESSION_SIGNATURE_FIELD, self.key)
        with mock.patch.object(p2, "REPO", self.root):
            with self.assertRaisesRegex(RuntimeError, "malformed"):
                p2._assert_single_r3_pass_admission(
                    self.session, current_admission_path=self.new_path,
                    current_admission_sha256=self.new_sha, signing_key=self.key,
                    control_root=self.control_root)


class StaleStaticAdmissionMonitorTests(unittest.TestCase):
    """Exercise the P2 adapter monitor without changing the accepted controller."""

    def setUp(self) -> None:
        self.fixture = vm_tests.InternalVMCapacityPolicyTests("test_phase_monitor_polls_and_records_final_sample")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.addCleanup(self.fixture.tearDown)
        self.volume_uuid = p2.VM_VOLUME_UUID
        self.g0c, self.profile, self.profile_path, self._old_admission_path = (
            self.fixture._prepare_p2_monitor(self.volume_uuid))
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="p2-r4-monitor-", dir=HERE))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.root, ignore_errors=True))
        self.key = vm.policy._manifest_signing_key()
        self.session_id = "a" * 32
        self.admission_path = self.root / "R6-VM-P2-ADMISSION-stale-static.json"
        self.vm_mount = self._vm_mount()
        self.token = os.urandom(32)
        self.commitment = hashlib.sha256(self.token).hexdigest()
        self.admission = {
            "record_type": "R6_VM_P2_PROVISIONING_ADMISSION",
            "run_id": "r4-stale-static-" + uuid.uuid4().hex[:8],
            "decision": "PASS", "authorized_phase": "P2",
            "session_id": self.session_id, "vm_volume_uuid": self.volume_uuid,
            "p2_growth_bytes": vm.GIB, "vm_volume_usage_bytes": 0,
            "created_at_unix": time.time() - 1_258,
            "controller_identity": vm._controller_identity(),
            "g0c_identity": self.g0c,
            "stop_safety_profile_mode": vm.VM_STOP_SAFETY_PROFILE_MODE_MEASURED,
            "stop_safety_profile_sha256": vm._file_sha256(self.profile_path),
            "stop_safety_profile": self.profile,
            "p2_concurrent_growth_bound_bytes": 0,
            "system_raw_path": str(self.root / "guest-base" / "system.raw"),
            "helper_control_token_sha256": self.commitment,
            "launch_capability": {
                "format_version": p2.P2_LAUNCH_CAPABILITY_FORMAT_VERSION,
                "commitment_sha256": self.commitment,
                "session_id": self.session_id,
                "private_store": p2.P2_LAUNCH_CAPABILITY_STORE_ID,
                "path_derivation": "<session_id>/<admission_sha256>.ready",
                "state": "READY", "single_use": True,
            },
        }
        self.admission = p2.signed_write(
            self.admission_path, self.admission, "vm_admission_hmac_sha256", self.key)
        self.admission_sha = vm._file_sha256(self.admission_path)
        self.control_root = self.root / "control-root"
        self.control_root.mkdir(mode=0o700)
        p2._write_launch_capability_ready(
            self.session_id, self.admission_sha, self.token,
            control_root=self.control_root)
        self.lease_path = self.root / "execution-start.json"
        self.lease = p2.signed_write(
            self.lease_path, self._lease(),
            p2.P2_EXECUTION_START_SIGNATURE_FIELD, self.key)
        self.lease_sha = vm._file_sha256(self.lease_path)
        self.issued_monotonic = time.monotonic()

    def _vm_mount(self, *, device: str = "disk3s7",
                  filesystem_id: list[int] | None = None) -> dict[str, object]:
        return {
            "volume_uuid": p2.VM_VOLUME_UUID,
            "device_identifier": device,
            "container_uuid": p2.CONTAINER_UUID,
            "container_reference": "disk3",
            "mount_point": "/Volumes/FGMetal-VM",
            "mount_state": "MOUNTED_VERIFIED",
            "mounted": True,
            "mount_identity": {
                "mount_table_device": f"/dev/{device}",
                "mount_table_filesystem_id": filesystem_id or [73, 19],
            },
        }

    def _lease(self) -> dict[str, object]:
        return {
            "schema_version": p2.P2_EXECUTION_START_LEASE_VERSION,
            "record_type": "R6_VM_P2_EXECUTION_START_LEASE",
            "protocol_version": "P2_R4_EXECUTION_START_LEASE_V1",
            "authority": p2.P2_EXECUTION_START_AUTHORITY,
            "authority_scope": "EXACT_ADMISSION_STARTUP_ONLY",
            "status": "ISSUED", "single_use": True,
            "issued_at_unix": time.time(), "maximum_age_seconds": 30,
            "static_admission_age_limited": False,
            "admission_path": str(self.admission_path.relative_to(REPO)),
            "admission_sha256": self.admission_sha,
            "session_id": self.session_id,
            "active_marker_sha256": vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH),
            "g0c_identity": self.g0c,
            "storage_measurement": {
                "vm_volume_usage_bytes": 0,
                "vm_mount": self.vm_mount,
            },
            "p2_demand": {"p2_growth_bytes": vm.GIB},
        }

    def _observation(self, *, free_bytes: int = 70 * vm.GIB,
                     mount: dict[str, object] | None = None) -> dict[str, object]:
        observation = vm_tests._session_observation(
            free_bytes=free_bytes, vm_volume_uuid=self.volume_uuid,
            vm_used_bytes=0, quota_bytes=50 * vm.GIB)
        observation["vm_volume_mount"] = self.vm_mount if mount is None else mount
        return observation

    def _runtime_patches(self):
        from contextlib import ExitStack

        stack = ExitStack()
        stack.enter_context(mock.patch.object(p2, "REPO", REPO))
        stack.enter_context(mock.patch.object(vm, "VM_STOP_SAFETY_PROFILE_PATH", self.profile_path))
        stack.enter_context(mock.patch.object(vm, "verify_fresh_g0c_review", return_value=self.g0c))
        stack.enter_context(mock.patch.object(
            vm, "_verify_stop_safety_profile", return_value=(self.profile, [])))
        return stack

    def _run_monitor(self, *, lease: object = ...,
                     sample_observation=None, operation_done=None,
                     claimed: dict[str, bool] | None = None,
                     issued_at_monotonic: float | None = None,
                     ready_event: threading.Event | None = None,
                     ready_evidence: dict[str, object] | None = None,
                     monitor_state: dict[str, object] | None = None):
        if lease is ...:
            lease = self.lease
        if claimed is None:
            claimed = {"value": False}
        if sample_observation is None:
            sample_observation = self._observation
        if operation_done is None:
            operation_done = lambda: bool(claimed["value"])
        if ready_event is None:
            ready_event = threading.Event()
        if ready_evidence is None:
            ready_evidence = {"validated_sample_count": 0}
        if monitor_state is None:
            monitor_state = {"error": None}
        return p2._run_p2_live_monitor(
            admission_path=self.admission_path, admission_sha256=self.admission_sha,
            lease_path=self.lease_path, lease=lease,
            lease_sha256=self.lease_sha,
            issued_at_monotonic=(self.issued_monotonic if issued_at_monotonic is None
                                 else issued_at_monotonic),
            signing_key=self.key, session_id=self.session_id,
            initial_quota_used_bytes=0, maximum_phase_growth_bytes=vm.GIB,
            sample_observation=sample_observation,
            operation_done=operation_done,
            cancel_operation=lambda: None,
            wait_operation_quiescent=lambda _timeout: True,
            stop_vm=lambda: None,
            wait_vm_stopped_and_handles_released=lambda _timeout: True,
            host_monitor_ready=ready_event,
            monitor_ready_evidence=ready_evidence,
            monitor_state=monitor_state,
            execution_claimed_state=claimed,
            boundary_lock=threading.Lock(), poll_interval_seconds=0.001)

    def _capability_state(self) -> str:
        capability = p2._validate_launch_capability_contract(
            self.admission, self.admission_sha, control_root=self.control_root)
        return str(capability["status"])

    def _run_in_thread(self, *, sample_observation=None, operation_done=None,
                       claimed: dict[str, bool] | None = None,
                       issued_at_monotonic: float | None = None):
        if claimed is None:
            claimed = {"value": False}
        ready_event = threading.Event()
        ready_evidence: dict[str, object] = {"validated_sample_count": 0}
        monitor_state: dict[str, object] = {"error": None}
        result: list[object] = []

        def target() -> None:
            try:
                result.append(self._run_monitor(
                    sample_observation=sample_observation,
                    operation_done=operation_done, claimed=claimed,
                    issued_at_monotonic=issued_at_monotonic,
                    ready_event=ready_event, ready_evidence=ready_evidence,
                    monitor_state=monitor_state))
            except BaseException as error:
                result.append(error)

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread, result, ready_event, ready_evidence, monitor_state, claimed

    def test_stale_static_admission_and_fresh_lease_reach_monitor_gate_before_claim(self) -> None:
        before_marker_sha = vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH)
        with self._runtime_patches():
            thread, result, ready, evidence, _state, claimed = self._run_in_thread()
            self.assertTrue(ready.wait(2), "three passing monitor samples should open the pre-claim gate")
            self.assertGreater(time.time() - self.admission["created_at_unix"], 1_200)
            self.assertGreaterEqual(evidence["validated_sample_count"], 3)
            self.assertEqual(self._capability_state(), "READY")
            self.assertEqual(vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH), before_marker_sha)
            claimed["value"] = True
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["status"], "PASS_PHASE_COMPLETE")

    def test_stale_static_admission_without_lease_fails_before_first_sample(self) -> None:
        samples = 0

        def sample() -> dict[str, object]:
            nonlocal samples
            samples += 1
            return self._observation()

        with self._runtime_patches(), self.assertRaisesRegex(
                RuntimeError, "signed execution-start lease"):
            self._run_monitor(lease=None, sample_observation=sample)
        self.assertEqual(samples, 0)
        self.assertEqual(self._capability_state(), "READY")

    def test_initial_capacity_failure_is_before_claim_and_preserves_marker(self) -> None:
        before_marker_sha = vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH)
        low_capacity = lambda: self._observation(free_bytes=23 * vm.GIB)
        with self._runtime_patches():
            thread, result, _ready, _evidence, _state, _claimed = self._run_in_thread(
                sample_observation=low_capacity)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result[0], vm.VMPhaseViolation)
        self.assertEqual(result[0].record["status"], "FAILED_STOPPED_BEFORE_CLAIM")
        self.assertEqual(self._capability_state(), "READY")
        self.assertEqual(vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH), before_marker_sha)

    def test_vm_mount_change_after_lease_fails_before_claim(self) -> None:
        before_marker_sha = vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH)
        changed_mount = self._vm_mount(device="disk3s8", filesystem_id=[73, 20])
        sample = lambda: self._observation(mount=changed_mount)
        with self._runtime_patches():
            thread, result, _ready, _evidence, _state, _claimed = self._run_in_thread(
                sample_observation=sample)
            thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertIsInstance(result[0], vm.VMPhaseViolation)
        self.assertIn("mount identity changed", str(result[0]))
        self.assertEqual(result[0].record["status"], "FAILED_STOPPED_BEFORE_CLAIM")
        self.assertEqual(self._capability_state(), "READY")
        self.assertEqual(vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH), before_marker_sha)

    def test_preclaim_monitor_failure_does_not_mutate_marker_after_shared_claim(self) -> None:
        before_marker_sha = vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH)
        p2._claim_launch_capability(
            self.session_id, self.admission_sha,
            self.admission["helper_control_token_sha256"], control_root=self.control_root)

        def competing_start_state() -> bool:
            raise RuntimeError("this provisioner lost the shared single-use claim")

        with self._runtime_patches(), self.assertRaises(vm.VMPhaseViolation) as raised:
            self._run_monitor(operation_done=competing_start_state, claimed={"value": False})
        self.assertFalse(raised.exception.record["capability_claimed"])
        self.assertEqual(raised.exception.record["status"], "FAILED_STOPPED_BEFORE_CLAIM")
        state_fd = p2._open_launch_capability_session_directory(
            self.session_id, control_root=self.control_root, create=False)
        try:
            status = p2._capability_file_state(
                state_fd, self.session_id, self.admission_sha)["status"]
        finally:
            os.close(state_fd)
        self.assertEqual(status, "CLAIMED")
        self.assertEqual(vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH), before_marker_sha)

    def test_claimed_monitor_continues_after_lease_expires(self) -> None:
        real_time = time
        base_unix = real_time.time()
        base_monotonic = real_time.monotonic()
        elapsed = 0.0

        class AdvancingClock:
            @staticmethod
            def time() -> float:
                return base_unix + elapsed

            @staticmethod
            def monotonic() -> float:
                return base_monotonic + elapsed

            @staticmethod
            def sleep(seconds: float) -> None:
                nonlocal elapsed
                elapsed += max(float(seconds), 0.02)
                real_time.sleep(0)

        self.lease_path.unlink()
        self.lease["issued_at_unix"] = base_unix
        self.lease = p2.signed_write(
            self.lease_path, self.lease,
            p2.P2_EXECUTION_START_SIGNATURE_FIELD, self.key)
        self.lease_sha = vm._file_sha256(self.lease_path)
        claimed = {"value": False}

        def complete_after_expiry() -> bool:
            return bool(claimed["value"] and elapsed > 30.2)

        with self._runtime_patches(), mock.patch.object(p2, "time", AdvancingClock):
            thread, result, ready, _evidence, _state, _claimed = self._run_in_thread(
                operation_done=complete_after_expiry, claimed=claimed,
                issued_at_monotonic=base_monotonic)
            self.assertTrue(ready.wait(2))
            claimed["value"] = True
            thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertGreater(elapsed, 30.2)
        self.assertEqual(result[0]["status"], "PASS_PHASE_COMPLETE")


if __name__ == "__main__":
    unittest.main()
