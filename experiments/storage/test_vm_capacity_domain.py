#!/usr/bin/env python3
"""Focused synthetic policy tests for the isolated internal VM APFS domain."""
from __future__ import annotations

import builtins
import hashlib
import io
import json
import os
import stat
import tempfile
import threading
import time
import uuid
import unittest
from contextlib import ExitStack, contextmanager, nullcontext
from pathlib import Path
from unittest import mock

import build_retention
import storage_policy as policy
import storage_gate0
import vm_capacity_domain as vm


def _projected_without_vm_commitment(**inputs):
    empty_commitment = {"committed_growth_bytes": 0, "marker_sha256": None,
                        "marker_identity": None}
    with mock.patch.object(policy, "_vm_capacity_commitment_snapshot",
                           return_value=empty_commitment):
        return policy.projected_storage_admission(**inputs)


def _observation(*, free_bytes: int = 70 * vm.GIB,
                 vm_volume_uuid: str | None = None,
                 vm_used_bytes: int = 0,
                 quota_bytes: int = vm.VM_REVIEWED_QUOTA_MAX_BYTES,
                 reserve_bytes: int = 0,
                 safety_volume_uuid: str | None = None,
                 safety_used_bytes: int = 0,
                 safety_reserve_bytes: int = vm.VM_PROTECTED_CAPACITY_RESERVE_BYTES,
                 safety_quota_bytes: int = 0,
                 safety_mounted: bool = False,
                 safety_container_reference: str = "disk3") -> dict[str, object]:
    volumes: list[dict[str, object]] = [{
        "APFSVolumeUUID": vm.EXPECTED_DATA_VOLUME_UUID,
        "DeviceIdentifier": "disk3s1",
        "Name": "Macintosh HD - Data",
        "CapacityInUse": 380 * vm.GIB,
        "CapacityQuota": 0,
        "CapacityReserve": 0,
        "Roles": ["Data"],
    }, {
        "APFSVolumeUUID": "B6A4A3CC-B83E-40D5-892F-C6419D06E71B",
        "DeviceIdentifier": "disk3s3",
        "Name": "Macintosh HD",
        "CapacityInUse": 13 * vm.GIB,
        "CapacityQuota": 0,
        "CapacityReserve": 0,
        "Roles": ["System"],
    }]
    if vm_volume_uuid is not None:
        volumes.append({
            "APFSVolumeUUID": vm_volume_uuid,
            "DeviceIdentifier": "disk3s7",
            "Name": vm.VM_VOLUME_NAME,
            "CapacityInUse": vm_used_bytes,
            "CapacityQuota": quota_bytes,
            "CapacityReserve": reserve_bytes,
            "Roles": [],
        })
    if safety_volume_uuid is not None:
        volumes.append({
            "APFSVolumeUUID": safety_volume_uuid,
            "DeviceIdentifier": "disk3s9",
            "Name": vm.VM_SAFETY_VOLUME_NAME,
            "CapacityInUse": safety_used_bytes,
            "CapacityQuota": safety_quota_bytes,
            "CapacityReserve": safety_reserve_bytes,
            "Roles": [],
        })
    container = {
        "APFSContainerUUID": vm.EXPECTED_CONTAINER_UUID,
        "ContainerReference": "disk3",
        "CapacityCeiling": 494_384_795_648,
        "CapacityFree": free_bytes,
        "DesignatedPhysicalStore": "disk0s2",
        "PhysicalStores": [{
            "DeviceIdentifier": "disk0s2",
            "DiskUUID": vm.EXPECTED_PHYSICAL_STORE_UUID,
            "Size": 494_384_795_648,
        }],
        "Volumes": volumes,
    }
    return {
        "schema_version": 1,
        "captured_at_unix": time.time(),
        "apfs_container": container,
        "physical_store_info": {
            "DeviceIdentifier": "disk0s2",
            "ParentWholeDisk": "disk0",
            "DiskUUID": vm.EXPECTED_PHYSICAL_STORE_UUID,
            "Internal": True,
            "Removable": False,
        },
        "physical_device_info": {
            "Internal": True,
            "Removable": False,
            "MediaName": vm.EXPECTED_INTERNAL_MEDIA_NAME,
            "SolidState": True,
            "selected_parent_whole_disk": "disk0",
        },
        "data_volume_info": {
            "VolumeUUID": vm.EXPECTED_DATA_VOLUME_UUID,
            "DeviceIdentifier": "disk3s1",
            "MountPoint": vm.EXPECTED_DATA_MOUNT,
            "FilesystemType": "apfs",
            "APFSContainerReference": "disk3",
            "canonical_mount_state": "MOUNTED_VERIFIED",
            "canonical_device_identifier": "disk3s1",
        },
        "crosschecks": {
            "apfs_container_free_bytes": free_bytes,
            "data_info_container_free_bytes": free_bytes,
            "data_statfs_available_bytes": free_bytes,
            "tolerance_bytes": vm.VM_SHARED_FREE_CROSSCHECK_TOLERANCE,
        },
        "safety_volume_info": ({
            "VolumeUUID": safety_volume_uuid,
            "DeviceIdentifier": "disk3s9",
            "APFSContainerReference": safety_container_reference,
            "FilesystemType": "apfs",
            "Mounted": safety_mounted,
            "MountPoint": "/Volumes/FGMetal-VM-Safety" if safety_mounted else None,
            "mount_state": "MOUNTED_VERIFIED" if safety_mounted else "UNMOUNTED_VERIFIED",
        } if safety_volume_uuid is not None else None),
    }


def _session_observation(*, free_bytes: int = 70 * vm.GIB,
                        vm_volume_uuid: str = "11111111-1111-1111-1111-111111111111",
                        vm_used_bytes: int = 0,
                        quota_bytes: int = 50 * vm.GIB,
                        reserve_bytes: int = 0,
                        safety_volume_uuid: str = "33333333-3333-3333-3333-333333333333",
                        safety_used_bytes: int = 0,
                        safety_reserve_bytes: int = vm.VM_PROTECTED_CAPACITY_RESERVE_BYTES,
                        safety_quota_bytes: int = 0,
                        safety_mounted: bool = False,
                        safety_container_reference: str = "disk3") -> dict[str, object]:
    return _observation(
        free_bytes=free_bytes, vm_volume_uuid=vm_volume_uuid,
        vm_used_bytes=vm_used_bytes, quota_bytes=quota_bytes,
        reserve_bytes=reserve_bytes, safety_volume_uuid=safety_volume_uuid,
        safety_used_bytes=safety_used_bytes,
        safety_reserve_bytes=safety_reserve_bytes,
        safety_quota_bytes=safety_quota_bytes, safety_mounted=safety_mounted,
        safety_container_reference=safety_container_reference)


class APFSMountIdentityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="apfs-mount-identity-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.mount_point = Path(self.temp.name) / "expected-mount"
        self.mount_point.mkdir()
        self.other_mount_point = Path(self.temp.name) / "other-mount"
        self.other_mount_point.mkdir()
        self.identity = {
            "volume_uuid": "86CAA82D-4005-4342-B779-CEFE80A7309B",
            "device_identifier": "disk3s7",
            "container_uuid": vm.EXPECTED_CONTAINER_UUID,
            "container_reference": "disk3",
            "volume_name": vm.VM_QUOTA_PROBE_VOLUME_NAME,
            "expected_mount_location": str(self.mount_point),
        }

    def _diskutil_info(self, **overrides: object) -> dict[str, object]:
        result: dict[str, object] = {
            "DeviceIdentifier": self.identity["device_identifier"],
            "VolumeUUID": self.identity["volume_uuid"],
            "APFSContainerReference": self.identity["container_reference"],
            "APFSContainerUUID": self.identity["container_uuid"],
            "FilesystemType": "apfs",
            "VolumeName": self.identity["volume_name"],
        }
        result.update(overrides)
        return result

    def _mount_record(self, *, source: str | None = None,
                      mount_point: str | None = None,
                      filesystem_id: list[int] | None = None) -> dict[str, object]:
        return {
            "source": source or f"/dev/{self.identity['device_identifier']}",
            "mount_point": mount_point or str(self.mount_point),
            "filesystem_type": "apfs",
            "filesystem_id": filesystem_id or [73, 19],
        }

    def _resolve(self, info: dict[str, object] | None = None,
                 records: list[dict[str, object]] | None = None) -> dict[str, object]:
        return vm._resolve_apfs_mount_state(
            **self.identity,
            diskutil_info=info if info is not None else self._diskutil_info(),
            mount_records=records if records is not None else [self._mount_record()])

    def test_mounted_true_and_matching_mountpoint_are_corroborated(self) -> None:
        result = self._resolve(self._diskutil_info(
            Mounted=True, MountPoint=str(self.mount_point)))
        self.assertEqual(result["mount_state"], "MOUNTED_VERIFIED")
        self.assertEqual(result["volume_uuid"], self.identity["volume_uuid"])
        self.assertEqual(result["mount_table_device"], f"/dev/{self.identity['device_identifier']}")
        self.assertEqual(result["mount_table_filesystem_id"], [73, 19])
        self.assertEqual(result["resolved_mount_point"], str(self.mount_point))

    def test_missing_mounted_and_mountpoint_keys_use_exact_mount_table_identity(self) -> None:
        result = self._resolve()
        self.assertEqual(result["mount_state"], "MOUNTED_VERIFIED")
        self.assertIsNone(result["diskutil_mounted"])
        self.assertFalse(result["diskutil_mount_point_present"])

    def test_missing_keys_and_no_mount_record_establish_unmounted_state(self) -> None:
        result = self._resolve(records=[])
        self.assertEqual(result["mount_state"], "UNMOUNTED_VERIFIED")
        self.assertIsNone(result["mount_table_device"])
        self.assertIsNone(result["resolved_mount_point"])

    def test_stale_mount_directory_does_not_establish_mounted_state(self) -> None:
        self.assertTrue(self.mount_point.is_dir())
        result = self._resolve(records=[])
        self.assertEqual(result["mount_state"], "UNMOUNTED_VERIFIED")

    def test_mounted_false_contradicting_kernel_table_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Mounted=false contradicts"):
            self._resolve(self._diskutil_info(Mounted=False))

    def test_diskutil_mountpoint_contradicting_kernel_table_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "MountPoint contradicts"):
            self._resolve(self._diskutil_info(
                Mounted=True, MountPoint=str(self.other_mount_point)))

    def test_mountpoint_omitted_but_kernel_mount_record_matches(self) -> None:
        result = self._resolve(self._diskutil_info(Mounted=True))
        self.assertEqual(result["mount_state"], "MOUNTED_VERIFIED")
        self.assertEqual(result["resolved_mount_point"], str(self.mount_point))

    def test_wrong_device_at_expected_path_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "occupied by another"):
            self._resolve(records=[self._mount_record(source="/dev/disk3s8")])

    def test_correct_device_at_wrong_path_fails_closed(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "unexpected mount point"):
            self._resolve(records=[self._mount_record(mount_point=str(self.other_mount_point))])

    def test_volume_uuid_or_device_mismatch_fails_closed(self) -> None:
        with self.subTest("volume UUID"):
            with self.assertRaisesRegex(RuntimeError, "UUID/device/container/name"):
                self._resolve(self._diskutil_info(
                    VolumeUUID="11111111-1111-1111-1111-111111111111"))
        with self.subTest("device"):
            with self.assertRaisesRegex(RuntimeError, "UUID/device/container/name"):
                self._resolve(self._diskutil_info(DeviceIdentifier="disk3s8"))
        with self.subTest("container UUID"):
            with self.assertRaisesRegex(RuntimeError, "UUID/device/container/name"):
                self._resolve(self._diskutil_info(
                    APFSContainerUUID="11111111-1111-1111-1111-111111111111"))

    def test_duplicate_or_conflicting_mount_records_fail_closed(self) -> None:
        with self.subTest("duplicate device"):
            with self.assertRaisesRegex(RuntimeError, "duplicate mounted-filesystem records"):
                self._resolve(records=[self._mount_record(), self._mount_record()])
        with self.subTest("conflicting expected path"):
            with self.assertRaisesRegex(RuntimeError, "occupied by another"):
                self._resolve(records=[self._mount_record(source="/dev/disk3s8")])

    def test_device_disappearance_during_diskutil_readback_fails_closed(self) -> None:
        with mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm, "_run_plist", side_effect=FileNotFoundError("deleted volume")), \
                mock.patch.object(vm, "_mounted_filesystem_table", return_value=[]):
            with self.assertRaises(FileNotFoundError):
                vm._read_apfs_mount_state(**self.identity)

    def test_previous_signed_probe_recovery_remains_failure_only_and_authentic(self) -> None:
        recovery_path = policy.REPO / "R6-VM-APFS-QUOTA-PROBE-RECOVERY.json"
        recovery = vm._read_signed_record(
            recovery_path, "vm_quota_probe_recovery_hmac_sha256")
        self.assertEqual(recovery.get("status"), "FAILED / RECOVERY")
        self.assertEqual(
            recovery.get("probe_volume_uuid"), "86CAA82D-4005-4342-B779-CEFE80A7309B")
        self.assertFalse(recovery.get("probe_file_opened"))
        self.assertFalse(recovery.get("probe_file_present"))
        self.assertTrue(recovery.get("writer_process_exited_before_cleanup"))
        self.assertTrue(vm.verify_signed_record(
            recovery["stop_proof"], "vm_stop_proof_hmac_sha256"))

    def test_failed_prewrite_probe_can_delete_only_its_exact_bounded_volume(self) -> None:
        volume_uuid = self.identity["volume_uuid"]
        before = _observation(
            vm_volume_uuid=volume_uuid,
            quota_bytes=vm.VM_QUOTA_PROBE_QUOTA_BYTES)
        before["apfs_container"]["Volumes"][-1]["Name"] = vm.VM_QUOTA_PROBE_VOLUME_NAME
        after = _observation()
        expected_mount = {
            "volume_uuid": volume_uuid,
            "device_identifier": self.identity["device_identifier"],
            "mount_state": "MOUNTED_VERIFIED",
            "mount_identity": {"mount_table_device": "/dev/disk3s7"},
        }
        command_result = mock.Mock(returncode=0, stdout="deleted", stderr="")
        with mock.patch.object(vm, "capture_host_observation",
                               side_effect=[before, after]), \
                mock.patch.object(vm, "_revalidate_volume_mount", return_value=expected_mount), \
                mock.patch.object(vm, "_mounted_filesystem_table", return_value=[]), \
                mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm.subprocess, "run", return_value=command_result) as run:
            evidence = vm._delete_exact_probe_volume(volume_uuid, expected_mount)
        self.assertEqual(evidence["after_observation"], after)
        run.assert_called_once_with(
            ["diskutil", "apfs", "deleteVolume", "/dev/disk3s7"],
            check=False, stdout=vm.subprocess.PIPE, stderr=vm.subprocess.PIPE,
            timeout=120.0, text=True)

    def test_exact_quota_probe_mount_readback_accepts_missing_mounted_key(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        observation = _observation(
            vm_volume_uuid=volume_uuid,
            quota_bytes=vm.VM_QUOTA_PROBE_QUOTA_BYTES)
        observation["apfs_container"]["Volumes"][-1]["Name"] = vm.VM_QUOTA_PROBE_VOLUME_NAME
        expected_location = f"/Volumes/{vm.VM_QUOTA_PROBE_VOLUME_NAME}"
        info = {
            "DeviceIdentifier": "disk3s7",
            "VolumeUUID": volume_uuid,
            "APFSContainerReference": "disk3",
            "APFSContainerUUID": vm.EXPECTED_CONTAINER_UUID,
            "FilesystemType": "apfs",
            "VolumeName": vm.VM_QUOTA_PROBE_VOLUME_NAME,
        }
        records = [{
            "source": "/dev/disk3s7",
            "mount_point": expected_location,
            "filesystem_type": "apfs",
            "filesystem_id": [73, 19],
        }]
        fake_stat = mock.Mock(st_mode=stat.S_IFDIR | 0o755)
        root_fd = os.open(self.mount_point, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        file_path = self.mount_point / "quota-enforcement-probe.bin"
        with mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm, "_run_plist", return_value=info), \
                mock.patch.object(vm, "_mounted_filesystem_table", return_value=records), \
                mock.patch.object(vm.os, "lstat", return_value=fake_stat), \
                mock.patch.object(vm.os.path, "realpath", return_value=expected_location), \
                mock.patch.object(vm, "capture_host_observation", return_value=observation), \
                mock.patch.object(vm, "_fd_mount_record", return_value={
                    "source": "/dev/disk3s7",
                    "mount_point": expected_location,
                    "filesystem_type": "apfs",
                    "filesystem_id": [73, 19],
                }):
            result = vm._volume_mount_info(observation, volume_uuid)
            file_fd = vm._open_verified_probe_payload(root_fd, volume_uuid, result)
            try:
                self.assertEqual(os.fstat(file_fd).st_nlink, 1)
            finally:
                os.close(file_fd)
        self.assertEqual(result["mount_state"], "MOUNTED_VERIFIED")
        self.assertTrue(result["mounted"])
        self.assertTrue(file_path.is_file())
        file_path.unlink()

    def test_device_change_before_payload_open_prevents_file_open(self) -> None:
        root_fd = os.open(self.mount_point, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        observation = _observation(
            vm_volume_uuid=volume_uuid,
            quota_bytes=vm.VM_QUOTA_PROBE_QUOTA_BYTES)
        observation["apfs_container"]["Volumes"][-1]["Name"] = vm.VM_QUOTA_PROBE_VOLUME_NAME
        expected = {
            "device_identifier": "disk3s7",
            "volume_uuid": volume_uuid,
            "container_uuid": vm.EXPECTED_CONTAINER_UUID,
            "container_reference": "disk3",
            "mount_point": str(self.mount_point),
            "mount_state": "MOUNTED_VERIFIED",
            "mount_identity": {"mount_table_device": "/dev/disk3s7"},
        }
        with mock.patch.object(vm, "capture_host_observation", return_value=observation), \
                mock.patch.object(vm, "_container_blockers", return_value=[]), \
                mock.patch.object(vm, "_revalidate_volume_mount",
                                  side_effect=RuntimeError("device identity changed")), \
                mock.patch.object(vm.os, "open") as open_file:
            with self.assertRaisesRegex(RuntimeError, "device identity changed"):
                vm._open_verified_probe_payload(root_fd, volume_uuid, expected)
        open_file.assert_not_called()

    def test_same_device_and_path_with_reused_mount_id_prevents_payload_open(self) -> None:
        root_fd = os.open(self.mount_point, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        observation = _observation(
            vm_volume_uuid=volume_uuid,
            quota_bytes=vm.VM_QUOTA_PROBE_QUOTA_BYTES)
        observation["apfs_container"]["Volumes"][-1]["Name"] = vm.VM_QUOTA_PROBE_VOLUME_NAME
        expected = {
            "device_identifier": "disk3s7",
            "volume_uuid": volume_uuid,
            "container_uuid": vm.EXPECTED_CONTAINER_UUID,
            "container_reference": "disk3",
            "mount_point": str(self.mount_point),
            "mount_state": "MOUNTED_VERIFIED",
            "mount_identity": {
                "mount_table_device": "/dev/disk3s7",
                "mount_table_filesystem_id": [73, 19],
            },
        }
        substituted_filesystem = {
            "source": "/dev/disk3s7",
            "mount_point": str(self.mount_point),
            "filesystem_type": "apfs",
            "filesystem_id": [73, 20],
        }
        with mock.patch.object(vm, "capture_host_observation", return_value=observation), \
                mock.patch.object(vm, "_container_blockers", return_value=[]), \
                mock.patch.object(vm, "_revalidate_volume_mount", return_value=expected), \
                mock.patch.object(vm, "_fd_mount_record", return_value=substituted_filesystem), \
                mock.patch.object(vm.os, "open") as open_file:
            with self.assertRaisesRegex(RuntimeError, "APFS volume instance"):
                vm._open_verified_probe_payload(root_fd, volume_uuid, expected)
        open_file.assert_not_called()


class InternalVMCapacityPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="vm-capacity-policy-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name) / "capacity-domain"
        marker = root / "active-session.json"
        deferred_inactivation = root / "deferred-capacity-inactivation.json"
        self.patches = [
            mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", Path(self.temp.name) / "signing-key"),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ROOT", root),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ACTIVE_MARKER", marker),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION",
                              deferred_inactivation),
            mock.patch.object(vm, "VM_ACTIVE_MARKER_PATH", marker),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        policy._manifest_signing_key(create=True)

    def _write_session_marker(self, session_id: str, volume_uuid: str,
                              *, quota_bytes: int = vm.VM_REVIEWED_QUOTA_MAX_BYTES,
                              pre_reserve_free_bytes: int = 70 * vm.GIB,
                              committed_growth_bytes: int = 0,
                              phase_id: str = "P1_VERIFIED",
                              state: str = "ACTIVE") -> dict[str, object]:
        return vm.write_active_session_marker(
            session_id=session_id, phase_id=phase_id,
            container_uuid=vm.EXPECTED_CONTAINER_UUID,
            volume_uuid=volume_uuid, committed_growth_bytes=committed_growth_bytes,
            plan_sha256="b" * 64, quota_bytes=quota_bytes,
            safety_volume_uuid="33333333-3333-3333-3333-333333333333",
            safety_volume_in_use_bytes=0,
            safety_container_uuid=vm.EXPECTED_CONTAINER_UUID,
            safety_physical_store_uuid=vm.EXPECTED_PHYSICAL_STORE_UUID,
            pre_reserve_shared_free_bytes=pre_reserve_free_bytes,
            unreserved_free_ceiling_bytes=(
                pre_reserve_free_bytes - vm.VM_PROTECTED_CAPACITY_RESERVE_BYTES),
            state=state)

    def _prepare_p2_monitor(self, volume_uuid: str, *,
                            committed_growth_bytes: int = 0
                            ) -> tuple[dict[str, object], dict[str, object], Path, Path]:
        g0c_identity: dict[str, object] = {"snapshot_sha256": "e" * 64}
        profile: dict[str, object] = {
            "volume_uuid": volume_uuid,
            "g0c_identity": g0c_identity,
            "host_build_version": "26A434",
            "maximum_write_rate_bytes_per_second": vm.MIB,
            "maximum_monitor_gap_microseconds": 250_000,
            "maximum_stop_latency_microseconds": 5_000_000,
        }
        profile_path = Path(self.temp.name) / "synthetic-stop-profile.json"
        profile_path.write_text("synthetic profile fixture\n", encoding="utf-8")
        profile_path.chmod(0o600)
        admission_path = Path(self.temp.name) / "synthetic-p2-admission.json"
        vm._write_signed_json(admission_path, {
            "decision": "PASS",
            "authorized_phase": "P2",
            "session_id": "a" * 32,
            "vm_volume_uuid": volume_uuid,
            "p2_growth_bytes": vm.GIB,
            "vm_volume_usage_bytes": 0,
            "created_at_unix": time.time(),
            "controller_identity": vm._controller_identity(),
            "g0c_identity": g0c_identity,
            "host_build_version": profile["host_build_version"],
            "stop_safety_profile_mode": vm.VM_STOP_SAFETY_PROFILE_MODE_MEASURED,
            "stop_safety_profile_sha256": vm._file_sha256(profile_path),
            "stop_safety_profile": profile,
            "p2_concurrent_growth_bound_bytes": 0,
        }, "vm_admission_hmac_sha256")
        session_id = "a" * 32
        self._write_session_marker(
            session_id, volume_uuid, phase_id="P1_ADMISSION",
            committed_growth_bytes=committed_growth_bytes)
        p1_path = Path(self.temp.name) / "synthetic-p1-admission.json"
        vm._write_signed_json(p1_path, {
            "record_type": "R6_VM_P1_RESTORE_DOWNLOAD_ADMISSION",
            "mode": vm.VM_MODE,
            "decision": "PASS",
            "authorization": "DOWNLOAD_AND_VERIFY_RESTORE_ONLY",
            "authorized_phase": "P1",
            "p1_authorized": True,
            "restore_download_authorized": True,
            "p2_authorized": False,
            "vm_start_authorized": False,
            "session_id": session_id,
            "vm_volume_uuid": volume_uuid,
            "receipt_path": "synthetic-p1-admission.json",
        }, "vm_admission_hmac_sha256")
        marker_sha = vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH)
        with mock.patch.object(
                vm, "_repo_artifact_path",
                return_value=(p1_path, "synthetic-p1-admission.json")):
            vm.update_active_session_marker(
                session_id, phase_id="P1", p1_admission_path=p1_path,
                p1_admission_sha256=vm._file_sha256(p1_path),
                expected_marker_sha256=marker_sha)
        vm.update_active_session_marker(session_id, phase_id="P1_VERIFIED")
        vm.update_active_session_marker(session_id, phase_id="P2_AUTHORIZED")
        return g0c_identity, profile, profile_path, admission_path

    def _assert_capacity_only_commitment(self, expected_bytes: int) -> None:
        marker = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        self.assertTrue(vm.verify_signed_record(marker, "vm_session_hmac_sha256"))
        self.assertEqual(marker["committed_growth_bytes"], expected_bytes)
        self.assertEqual(policy._active_vm_capacity_session_blockers(), [])
        projection = policy.projected_storage_admission(
            current_free_bytes=70 * vm.GIB,
            current_scoped_bytes=0,
            peak_additional_bytes=0,
        )
        self.assertEqual(projection["shared_capacity_commitment_bytes"], expected_bytes)
        self.assertEqual(projection["projected_free_bytes"], 70 * vm.GIB - expected_bytes)
        self.assertTrue(projection["admitted"])

    def test_synthetic_capacity_domain_does_not_read_live_inactivation_record(self) -> None:
        expected_commitment = 3 * vm.GIB
        self._write_session_marker(
            "a" * 32, "11111111-1111-1111-1111-111111111111",
            committed_growth_bytes=expected_commitment,
        )
        synthetic_receipt = policy.VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION
        self.assertEqual(
            synthetic_receipt,
            policy.VM_CAPACITY_DOMAIN_ROOT / "deferred-capacity-inactivation.json",
        )
        self.assertFalse(synthetic_receipt.exists())

        with mock.patch.object(policy, "_stat_entry_nofollow",
                               wraps=policy._stat_entry_nofollow) as stat_entry:
            snapshot = policy._vm_capacity_commitment_snapshot()

        self.assertEqual(snapshot["committed_growth_bytes"], expected_commitment)
        self.assertEqual(snapshot["historical_committed_growth_bytes"], expected_commitment)
        self.assertIsNone(snapshot["capacity_inactivation_sha256"])
        self.assertEqual(
            [call.args[0] for call in stat_entry.call_args_list], [synthetic_receipt],
        )

    def _run_p2_admission_fixture(self, *, free_bytes: int,
                                  profile_bytes: bytes | None = None,
                                  quota_bytes: int = 45 * vm.GIB,
                                  vm_used_bytes: int = 25 * vm.GIB,
                                  concurrent_failure: bool = False,
                                  host_lease_state: str | None = None) -> dict[str, object]:
        g0c_identity: dict[str, object] = {"snapshot_sha256": "f" * 64}
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        session_id = "a" * 32
        safety_uuid = "33333333-3333-3333-3333-333333333333"
        lease_suffix = f"-{host_lease_state.lower()}" if host_lease_state is not None else ""
        root = Path(self.temp.name) / f"p2-admission-{free_bytes}{lease_suffix}"
        root.mkdir()
        # Each arithmetic/admission case uses an explicit fresh synthetic marker.
        vm.VM_ACTIVE_MARKER_PATH.unlink(missing_ok=True)
        mount = root / "vm-volume"
        mount.mkdir()
        restore_file = mount / "restore.ipsw"
        restore_file.write_bytes(b"verified restore")
        info = restore_file.stat()
        restore_length = info.st_size
        lease_root = root / "build-leases"
        lease_root.mkdir(mode=0o700)
        retention_lock_fd = os.open(
            lease_root / build_retention.BUILD_LOCK,
            os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(retention_lock_fd)
        with mock.patch.object(build_retention, "LEASE_ROOT", lease_root):
            if host_lease_state == "MALFORMED":
                malformed_lease = lease_root / "unknown.json"
                malformed_lease.write_bytes(b"not a signed BuildLease record\n")
                malformed_lease.chmod(0o600)
            elif host_lease_state is not None:
                build_retention._store_lease({
                    "schema_version": 1,
                    "build_id": "synthetic-host-build",
                    "path": str(root / "synthetic-host-build"),
                    "owner_experiment": "synthetic-test",
                    "kind": "reference",
                    "created_at_unix": time.time(),
                    "state": host_lease_state,
                    "session_v3": {
                        "schema_version": build_retention.BUILD_SESSION_SCHEMA_VERSION,
                        "executor_mode": build_retention.BUILD_SESSION_EXECUTOR_MODE,
                        "state": "ACTIVE",
                    },
                })
        creation_path = root / "creation.json"
        p1_path = root / "p1.json"
        restore_path = root / "restore-verification.json"
        profile_path = root / "stop-profile.json"
        receipt_path = root / "p2-admission.json"
        vm._write_signed_json(creation_path, {
            "status": "PASS", "mode": vm.VM_MODE,
            "session_id": session_id, "volume_uuid": volume_uuid,
            "g0c_identity": g0c_identity,
        }, "vm_creation_hmac_sha256")
        vm._write_signed_json(p1_path, {
            "decision": "PASS", "authorized_phase": "P1",
            "session_id": session_id, "vm_volume_uuid": volume_uuid,
            "controller_identity": vm._controller_identity(),
            "g0c_identity": g0c_identity,
        }, "vm_admission_hmac_sha256")
        vm._write_signed_json(restore_path, {
            "status": "PASS", "phase_id": "P1",
            "local_sha256_state": "VERIFIED",
            "local_sha256": hashlib.sha256(restore_file.read_bytes()).hexdigest(),
            "session_id": session_id, "volume_uuid": volume_uuid,
            "restore_path": str(restore_file),
            "expected_length_bytes": restore_length,
            "file_identity": {
                "device": info.st_dev,
                "inode": info.st_ino,
                "logical_bytes": info.st_size,
                "mtime_ns": info.st_mtime_ns,
                "ctime_ns": info.st_ctime_ns,
            },
            "p1_admission_sha256": vm._file_sha256(p1_path),
            "g0c_identity": g0c_identity,
        }, "restore_verification_hmac_sha256")
        if profile_bytes is not None:
            profile_path.write_bytes(profile_bytes)
            profile_path.chmod(0o600)
        self._write_session_marker(session_id, volume_uuid,
                                   quota_bytes=quota_bytes,
                                   phase_id="P1_VERIFIED")
        measurement_patch = mock.patch.object(
            vm, "measure_concurrent_growth_bound",
            side_effect=RuntimeError("synthetic concurrent sampling failure")
            if concurrent_failure else None,
            return_value={"status": "PASS", "observed_max_interval_growth_bytes": 0})
        lock_trace: dict[str, bool] = {
            "held": False,
            "host_scan_under_lock": False,
            "retention_guard_held": False,
            "marker_update_under_guard": False,
        }

        @contextmanager
        def allocation_lock_fixture():
            if lock_trace["held"]:
                raise AssertionError("nested synthetic allocation lock")
            lock_trace["held"] = True
            try:
                yield
            finally:
                lock_trace["held"] = False

        original_host_lease_guard = build_retention.host_buildlease_p2_admission_guard

        @contextmanager
        def checked_host_lease_guard():
            if not lock_trace["held"]:
                raise AssertionError("P2 checked Host BuildLease state outside the shared allocation lock")
            lock_trace["host_scan_under_lock"] = True
            with original_host_lease_guard() as blockers:
                lock_trace["retention_guard_held"] = True
                try:
                    yield blockers
                finally:
                    lock_trace["retention_guard_held"] = False

        def checked_marker_update(*_args, **_kwargs):
            if not lock_trace["retention_guard_held"]:
                raise AssertionError("P2 marker commit escaped the Host BuildLease retention lock")
            lock_trace["marker_update_under_guard"] = True

        with mock.patch.object(vm, "VM_STOP_SAFETY_PROFILE_PATH", profile_path), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity), \
                mock.patch.object(vm, "_host_build_version", return_value="26A434"), \
                mock.patch.object(vm, "capture_host_observation",
                                  return_value=_observation(
                                      free_bytes=free_bytes,
                                      vm_volume_uuid=volume_uuid,
                                      vm_used_bytes=vm_used_bytes,
                                      quota_bytes=quota_bytes,
                                      safety_volume_uuid=safety_uuid)), \
                mock.patch.object(vm, "_volume_mount_info", return_value={
                    "mounted": True, "mount_point": str(mount)}), \
                measurement_patch, \
                mock.patch.object(vm, "update_active_session_marker",
                                  side_effect=checked_marker_update), \
                mock.patch.object(policy, "_acquire_storage_allocation_lock",
                                  side_effect=allocation_lock_fixture), \
                mock.patch.object(policy, "measure_project_usage", return_value={"rows": []}), \
                mock.patch.object(policy, "conservative_project_usage_bytes", return_value=0), \
                mock.patch.object(build_retention, "LEASE_ROOT", lease_root), \
                mock.patch.object(build_retention, "host_buildlease_p2_admission_guard",
                                  side_effect=checked_host_lease_guard):
            result = vm.create_p2_admission(
                stop_safety_profile_path=profile_path,
                creation_receipt_path=creation_path,
                p1_admission_path=p1_path,
                restore_verification_path=restore_path,
                receipt_path=receipt_path)
        self._last_p2_lock_trace = lock_trace
        return result

    def test_normal_project_ceiling_matches_current_policy(self) -> None:
        result = _projected_without_vm_commitment(
            current_free_bytes=70 * vm.GIB,
            current_scoped_bytes=59 * vm.GIB,
            peak_additional_bytes=vm.GIB + 1,
        )
        self.assertFalse(result["admitted"])
        self.assertEqual(result["projected_scoped_bytes"], policy.PROJECT_BUDGET + 1)

    def test_normal_projected_free_floor_stays_50_gib(self) -> None:
        result = _projected_without_vm_commitment(
            current_free_bytes=51 * vm.GIB,
            current_scoped_bytes=40 * vm.GIB,
            peak_additional_bytes=vm.GIB + 1,
        )
        self.assertFalse(result["admitted"])
        self.assertEqual(result["projected_free_bytes"], 50 * vm.GIB - 1)

    def test_normal_preferred_free_and_emergency_limits_remain_60_and_25_gib(self) -> None:
        self.assertEqual(policy.FREE_PREFERRED, 60 * vm.GIB)
        self.assertEqual(policy.FREE_HARD_STOP, 25 * vm.GIB)
        result = _projected_without_vm_commitment(
            current_free_bytes=26 * vm.GIB,
            current_scoped_bytes=10 * vm.GIB,
            peak_additional_bytes=2 * vm.GIB,
        )
        self.assertEqual(result["projected_free_bytes"], 24 * vm.GIB)
        self.assertFalse(result["admitted"])

    def test_g0c_review_contract_binds_vm_controller_and_tests(self) -> None:
        self.assertEqual(storage_gate0.G0C_REVIEW_REQUIRED_FILES["vm_capacity_controller"],
                         "vm_capacity_domain.py")
        self.assertEqual(storage_gate0.G0C_REVIEW_REQUIRED_FILES["vm_capacity_tests"],
                         "test_vm_capacity_domain.py")
        self.assertEqual(storage_gate0.G0C_REVIEW_REQUIRED_FILES["vm_restore_metadata_probe"],
                         "vm_restore_metadata_probe.swift")
        self.assertEqual(vm.G0C_SNAPSHOT_PATH.name,
                         "G0C-INTERNAL-VM-CONTROLLER-SNAPSHOT-P1-BODY-START-REPAIR-R6-20261017.json")
        self.assertEqual(vm.G0C_REVIEW_PATH.name,
                         "g0c-internal-vm-controller-independent-review-p1-body-start-repair-r6-20261017.json")

    def test_active_marker_locator_supports_configured_cache_path_outside_repo(self) -> None:
        marker_path = Path(self.temp.name) / "cache" / "vm-capacity-domain" / "active-session.json"
        with mock.patch.object(vm, "VM_ACTIVE_MARKER_PATH", marker_path):
            self.assertEqual(vm._active_marker_locator(), str(marker_path))

    def test_vm_mode_requires_explicit_authorization(self) -> None:
        result = vm.calculate_vm_phase_admission(
            mode="NORMAL_PROJECT", container_uuid=vm.EXPECTED_CONTAINER_UUID,
            free_bytes=70 * vm.GIB, quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            quota_used_bytes=0, growth_bytes=vm.GIB,
            uncertainty_bytes=vm.MIB, concurrent_growth_bytes=0,
        )
        self.assertFalse(result["admitted"])
        self.assertTrue(any("explicit INTERNAL_VM_CAPACITY_DOMAIN" in item
                            for item in result["blockers"]))

    def test_vm_quota_never_becomes_normal_project_credit(self) -> None:
        # VM bytes are excluded from project usage, but they cannot subtract from
        # or otherwise credit the existing normal project ledger.
        commitment = 10 * vm.GIB
        with mock.patch.object(policy, "_vm_capacity_commitment_snapshot", return_value={
                "committed_growth_bytes": commitment,
                "marker_sha256": "a" * 64,
                "marker_identity": {"device": 1, "inode": 2},
        }):
            result = policy.projected_storage_admission(
                current_free_bytes=80 * vm.GIB,
                current_scoped_bytes=59 * vm.GIB,
                peak_additional_bytes=2 * vm.GIB,
            )
        without_commitment = _projected_without_vm_commitment(
            current_free_bytes=80 * vm.GIB,
            current_scoped_bytes=59 * vm.GIB,
            peak_additional_bytes=2 * vm.GIB)
        self.assertFalse(result["admitted"])
        self.assertEqual(result["shared_capacity_commitment_bytes"], commitment)
        self.assertEqual(result["projected_free_bytes"], without_commitment["projected_free_bytes"] - commitment)
        self.assertEqual(result["projected_scoped_bytes"], 61 * vm.GIB)
        self.assertEqual(result["projected_scoped_bytes"], without_commitment["projected_scoped_bytes"])

    def test_shared_apfs_capacity_is_counted_once_by_container_uuid(self) -> None:
        sample = _observation(free_bytes=70 * vm.GIB)
        result = vm.deduplicate_container_free_measurements([sample, sample])
        self.assertEqual(result["container_count"], 1)
        self.assertEqual(result["total_unique_shared_free_bytes"], 70 * vm.GIB)
        self.assertEqual(result["capacity_domains"][vm.EXPECTED_CONTAINER_UUID]["observation_count"], 2)

    def test_physical_store_identity_substitution_fails_closed(self) -> None:
        sample = _observation()
        sample["apfs_container"]["PhysicalStores"][0]["DiskUUID"] = "11111111-1111-1111-1111-111111111111"
        blockers = vm._container_blockers(sample)
        self.assertTrue(any("physical-store UUID" in item for item in blockers))

    def test_vm_volume_uuid_substitution_fails_closed(self) -> None:
        sample = _observation(vm_volume_uuid="11111111-1111-1111-1111-111111111111")
        blockers = vm._container_blockers(
            sample, expected_vm_volume_uuid="22222222-2222-2222-2222-222222222222")
        self.assertTrue(any("bound VM volume UUID" in item for item in blockers))

    def test_quota_mismatch_fails_closed(self) -> None:
        sample = _observation(
            vm_volume_uuid="11111111-1111-1111-1111-111111111111",
            quota_bytes=49 * vm.GIB)
        blockers = vm._container_blockers(
            sample, expected_vm_volume_uuid="11111111-1111-1111-1111-111111111111")
        self.assertTrue(any("quota" in item and "differs" in item for item in blockers))

    def test_exact_empty_safety_volume_contract_passes(self) -> None:
        sample = _session_observation()
        blockers = vm._container_blockers(
            sample, expected_vm_volume_uuid="11111111-1111-1111-1111-111111111111",
            expected_vm_quota_bytes=50 * vm.GIB,
            expected_safety_volume_uuid="33333333-3333-3333-3333-333333333333",
            expected_safety_volume_in_use_bytes=0)
        self.assertEqual(blockers, [])

    def test_missing_safety_volume_blocks_the_live_session(self) -> None:
        self._write_session_marker("a" * 32, "11111111-1111-1111-1111-111111111111")
        blockers = vm._session_container_blockers(
            _observation(vm_volume_uuid="11111111-1111-1111-1111-111111111111"),
            volume_uuid="11111111-1111-1111-1111-111111111111")
        self.assertTrue(any("safety-volume UUID" in item for item in blockers))

    def test_safety_reserve_mismatch_fails_closed(self) -> None:
        self._write_session_marker(
            "a" * 32, "11111111-1111-1111-1111-111111111111",
            committed_growth_bytes=vm.GIB)
        blockers = vm._session_container_blockers(
            _session_observation(safety_reserve_bytes=14 * vm.GIB),
            volume_uuid="11111111-1111-1111-1111-111111111111")
        self.assertTrue(any("exact reserve" in item for item in blockers))
        self._assert_capacity_only_commitment(vm.GIB)

    def test_safety_reserve_cannot_be_borrowed_or_rebound_during_a_session(self) -> None:
        self._write_session_marker("a" * 32, "11111111-1111-1111-1111-111111111111")
        with self.assertRaises(TypeError):
            vm.update_active_session_marker(
                "a" * 32, safety_reserve_bytes=14 * vm.GIB)  # type: ignore[call-arg]
        blockers = vm._session_container_blockers(
            _session_observation(safety_reserve_bytes=14 * vm.GIB),
            volume_uuid="11111111-1111-1111-1111-111111111111")
        self.assertTrue(any("exact reserve" in item for item in blockers))

    def test_safety_volume_uuid_substitution_fails_closed(self) -> None:
        self._write_session_marker("a" * 32, "11111111-1111-1111-1111-111111111111")
        blockers = vm._session_container_blockers(
            _session_observation(safety_volume_uuid="44444444-4444-4444-4444-444444444444"),
            volume_uuid="11111111-1111-1111-1111-111111111111")
        self.assertTrue(any("bound safety-volume UUID" in item for item in blockers))

    def test_safety_volume_must_stay_unmounted(self) -> None:
        self._write_session_marker("a" * 32, "11111111-1111-1111-1111-111111111111")
        blockers = vm._session_container_blockers(
            _session_observation(safety_mounted=True),
            volume_uuid="11111111-1111-1111-1111-111111111111")
        self.assertTrue(any("unmounted state" in item for item in blockers))

    def test_safety_volume_empty_accounting_must_not_change(self) -> None:
        self._write_session_marker("a" * 32, "11111111-1111-1111-1111-111111111111")
        blockers = vm._session_container_blockers(
            _session_observation(safety_used_bytes=vm.MIB),
            volume_uuid="11111111-1111-1111-1111-111111111111")
        self.assertTrue(any("empty accounting" in item for item in blockers))

    def test_safety_volume_has_zero_quota(self) -> None:
        sample = _session_observation(safety_quota_bytes=vm.MIB)
        blockers = vm._container_blockers(
            sample, expected_vm_volume_uuid="11111111-1111-1111-1111-111111111111",
            expected_vm_quota_bytes=50 * vm.GIB,
            expected_safety_volume_uuid="33333333-3333-3333-3333-333333333333",
            expected_safety_volume_in_use_bytes=0)
        self.assertTrue(any("safety-volume name, empty accounting, quota" in item
                            for item in blockers))

    def test_safety_volume_container_identity_mismatch_fails_closed(self) -> None:
        self._write_session_marker("a" * 32, "11111111-1111-1111-1111-111111111111")
        blockers = vm._session_container_blockers(
            _session_observation(safety_container_reference="disk9"),
            volume_uuid="11111111-1111-1111-1111-111111111111")
        self.assertTrue(any("container, filesystem" in item for item in blockers))

    def test_safety_volume_store_identity_is_explicitly_bound(self) -> None:
        with self.assertRaisesRegex(ValueError, "container/store identities"):
            vm.write_active_session_marker(
                session_id="a" * 32, phase_id="P0", container_uuid=vm.EXPECTED_CONTAINER_UUID,
                volume_uuid="11111111-1111-1111-1111-111111111111",
                committed_growth_bytes=0, plan_sha256="b" * 64,
                safety_volume_uuid="33333333-3333-3333-3333-333333333333",
                safety_volume_in_use_bytes=0,
                safety_container_uuid=vm.EXPECTED_CONTAINER_UUID,
                safety_physical_store_uuid="44444444-4444-4444-4444-444444444444")

    def test_active_contract_rejects_missing_or_malformed_unreserved_free_ceiling(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        marker = self._write_session_marker("a" * 32, volume_uuid)
        self.assertEqual(vm._active_capacity_contract(volume_uuid=volume_uuid)[
            "unreserved_free_ceiling_bytes"], 55 * vm.GIB)
        unsigned = {key: value for key, value in marker.items()
                    if key != "vm_session_hmac_sha256"}
        unsigned["unreserved_free_ceiling_bytes"] = 56 * vm.GIB
        vm._write_signed_json(
            vm.VM_ACTIVE_MARKER_PATH, unsigned, "vm_session_hmac_sha256")
        with self.assertRaisesRegex(RuntimeError, "valid safety reserve and dynamic quota"):
            vm._active_capacity_contract(volume_uuid=volume_uuid)

    def test_marker_rejects_inconsistent_pre_reserve_capacity_ceiling(self) -> None:
        with self.assertRaisesRegex(ValueError, "pre-reserve free capacity"):
            vm.write_active_session_marker(
                session_id="a" * 32, phase_id="P0",
                container_uuid=vm.EXPECTED_CONTAINER_UUID,
                volume_uuid="11111111-1111-1111-1111-111111111111",
                committed_growth_bytes=0, plan_sha256="b" * 64,
                safety_volume_uuid="33333333-3333-3333-3333-333333333333",
                safety_volume_in_use_bytes=0,
                safety_container_uuid=vm.EXPECTED_CONTAINER_UUID,
                safety_physical_store_uuid=vm.EXPECTED_PHYSICAL_STORE_UUID,
                pre_reserve_shared_free_bytes=70 * vm.GIB,
                unreserved_free_ceiling_bytes=56 * vm.GIB)

    def test_live_free_at_8_gib_early_stop_threshold_blocks(self) -> None:
        result = vm.calculate_vm_phase_admission(
            mode=vm.VM_MODE, container_uuid=vm.EXPECTED_CONTAINER_UUID,
            free_bytes=8 * vm.GIB, quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            quota_used_bytes=0, growth_bytes=0,
            uncertainty_bytes=0, concurrent_growth_bytes=1,
        )
        self.assertFalse(result["admitted"])
        self.assertTrue(any("8 GiB" in item for item in result["blockers"]))

    def test_live_free_at_5_gib_hard_floor_blocks(self) -> None:
        result = vm.calculate_vm_phase_admission(
            mode=vm.VM_MODE, container_uuid=vm.EXPECTED_CONTAINER_UUID,
            free_bytes=5 * vm.GIB, quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            quota_used_bytes=0, growth_bytes=0,
            uncertainty_bytes=0, concurrent_growth_bytes=0,
        )
        self.assertFalse(result["admitted"])
        self.assertTrue(any("5 GiB" in item for item in result["blockers"]))

    def test_dynamic_quota_pre_reserve_subtracts_exact_reserve_once(self) -> None:
        result = vm.calculate_dynamic_vm_quota(
            free_bytes=40 * vm.GIB,
            deterministic_uncertainty_bytes=256 * vm.MIB,
            admitted_coexistence_bytes=2 * vm.GIB,
            minimum_required_quota_bytes=10 * vm.GIB,
            maximum_quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            reserve_already_reflected=False)
        self.assertTrue(result["admitted"])
        self.assertEqual(result["safety_reserve_subtracted_bytes"], 15 * vm.GIB)
        self.assertEqual(result["quota_bytes"], 17 * vm.GIB + 768 * vm.MIB)

    def test_dynamic_quota_post_reserve_does_not_subtract_reserve_twice(self) -> None:
        result = vm.calculate_dynamic_vm_quota(
            free_bytes=25 * vm.GIB,
            deterministic_uncertainty_bytes=256 * vm.MIB,
            admitted_coexistence_bytes=2 * vm.GIB,
            minimum_required_quota_bytes=10 * vm.GIB,
            maximum_quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            reserve_already_reflected=True)
        self.assertTrue(result["admitted"])
        self.assertEqual(result["safety_reserve_subtracted_bytes"], 0)
        self.assertEqual(result["quota_bytes"], 17 * vm.GIB + 768 * vm.MIB)

    def test_post_reserve_quota_is_capped_even_when_readback_may_include_reserve(self) -> None:
        pre_reserve_free = 65 * vm.GIB
        post_reserve = vm.calculate_post_reserve_free_capacity(
            pre_reserve_free_bytes=pre_reserve_free,
            post_reserve_free_bytes=pre_reserve_free)
        self.assertTrue(post_reserve["admitted"])
        self.assertEqual(post_reserve["unreserved_free_ceiling_bytes"], 50 * vm.GIB)
        self.assertEqual(post_reserve["effective_unreserved_free_bytes"], 50 * vm.GIB)
        quota = vm.calculate_dynamic_vm_quota(
            free_bytes=post_reserve["effective_unreserved_free_bytes"],
            deterministic_uncertainty_bytes=0,
            admitted_coexistence_bytes=0,
            minimum_required_quota_bytes=1,
            reserve_already_reflected=True)
        pre_reserve_quota = vm.calculate_dynamic_vm_quota(
            free_bytes=pre_reserve_free,
            deterministic_uncertainty_bytes=0,
            admitted_coexistence_bytes=0,
            minimum_required_quota_bytes=1,
            reserve_already_reflected=False)
        unbounded_post_quota = vm.calculate_dynamic_vm_quota(
            free_bytes=pre_reserve_free,
            deterministic_uncertainty_bytes=0,
            admitted_coexistence_bytes=0,
            minimum_required_quota_bytes=1,
            reserve_already_reflected=True)
        self.assertEqual(quota["quota_bytes"], 45 * vm.GIB)
        self.assertEqual(quota["quota_bytes"], pre_reserve_quota["quota_bytes"])
        self.assertGreater(unbounded_post_quota["quota_bytes"], quota["quota_bytes"])

    def test_post_reserve_free_subtracts_reserve_from_lower_live_value(self) -> None:
        result = vm.calculate_post_reserve_free_capacity(
            pre_reserve_free_bytes=70 * vm.GIB,
            post_reserve_free_bytes=50 * vm.GIB)
        self.assertEqual(result["effective_unreserved_free_bytes"], 35 * vm.GIB)

    def test_create_vm_volume_caps_quota_when_post_reserve_readback_is_unchanged(self) -> None:
        pre_reserve_free = 65 * vm.GIB
        expected_capacity = vm.calculate_post_reserve_free_capacity(
            pre_reserve_free_bytes=pre_reserve_free,
            post_reserve_free_bytes=pre_reserve_free)
        expected_quota = vm.calculate_dynamic_vm_quota(
            free_bytes=expected_capacity["effective_unreserved_free_bytes"],
            deterministic_uncertainty_bytes=vm.VM_DYNAMIC_QUOTA_UNCERTAINTY_BYTES,
            admitted_coexistence_bytes=0,
            minimum_required_quota_bytes=20 * vm.GIB,
            maximum_quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            reserve_already_reflected=True)["quota_bytes"]
        self.assertEqual(expected_quota, 44 * vm.GIB + 768 * vm.MIB)

        root = Path(self.temp.name)
        precreation_path = root / "precreation-admission.json"
        probe_path = root / "quota-probe.json"
        historical_probe_path = root / "historical-failed-quota-probe.json"
        metadata_path = root / "restore-metadata.json"
        creation_path = root / "vm-creation.json"
        historical_probe_bytes = b"historical failed probe receipt remains byte-for-byte unchanged\n"
        historical_probe_path.write_bytes(historical_probe_bytes)
        metadata_path.write_text("synthetic signed metadata fixture\n", encoding="utf-8")
        controller_identity = {"vm_capacity_controller_sha256": "c" * 64}
        g0c_identity = {"snapshot_sha256": "d" * 64}
        probe = vm._write_signed_json(probe_path, {
            "status": "PASS",
            "probe_volume_deleted": True,
            "controller_identity": controller_identity,
            "g0c_identity": g0c_identity,
        }, "vm_quota_probe_hmac_sha256")
        vm._write_signed_json(precreation_path, {
            "decision": "PASS",
            "authorization": "CREATE_VM_VOLUME_ONLY",
            "mode": vm.VM_MODE,
            "created_at_unix": time.time(),
            "controller_identity": controller_identity,
            "g0c_identity": g0c_identity,
            "quota_probe_receipt_sha256": vm._file_sha256(probe_path),
            "phase_evaluation": {"plan": {"restore_expected_bytes": 1 * vm.GIB}},
            "dynamic_vm_quota_pre_reserve": {
                "minimum_required_quota_bytes": 20 * vm.GIB,
            },
            "restore_upstream_metadata": {
                "status": "PASS",
                "expected_length_bytes": 1 * vm.GIB,
                "metadata_sha256": "e" * 64,
            },
            "restore_metadata_receipt_sha256": vm._file_sha256(metadata_path),
        }, "vm_admission_hmac_sha256")

        safety_uuid = "33333333-3333-3333-3333-333333333333"
        vm_uuid = "11111111-1111-1111-1111-111111111111"
        before = _observation(free_bytes=pre_reserve_free)
        after_safety = _observation(
            free_bytes=pre_reserve_free, safety_volume_uuid=safety_uuid)
        after_vm = _observation(
            free_bytes=pre_reserve_free, vm_volume_uuid=vm_uuid,
            quota_bytes=expected_quota, safety_volume_uuid=safety_uuid)
        command_result = mock.Mock(returncode=0, stdout="created", stderr="")
        with mock.patch.object(policy, "_acquire_storage_allocation_lock",
                               return_value=mock.MagicMock()), \
                mock.patch.object(vm, "VM_QUOTA_PROBE_RECEIPT_PATH", historical_probe_path), \
                mock.patch.object(vm, "VM_RESTORE_METADATA_PATH", metadata_path), \
                mock.patch.object(vm, "VM_VOLUME_CREATION_RECEIPT_PATH", creation_path), \
                mock.patch.object(vm, "_read_latest_restore_metadata", return_value={
                    "metadata_sha256": "e" * 64,
                    "expected_length_bytes": 1 * vm.GIB,
                }), \
                mock.patch.object(vm, "_controller_identity", return_value=controller_identity), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity), \
                mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm, "_volume_mount_info", return_value={
                    "mounted": False, "mount_point": None,
                }), \
                mock.patch.object(vm, "capture_host_observation",
                                  side_effect=[before, after_safety, after_safety, after_vm]), \
                mock.patch.object(vm.subprocess, "run", return_value=command_result) as run:
            result = vm.create_vm_volume(
                precreation_path, quota_probe_receipt_path=probe_path,
                receipt_path=creation_path)
        self.assertEqual(result["quota_bytes"], expected_quota)
        self.assertEqual(result["quota_probe_receipt_sha256"], vm._file_sha256(probe_path))
        self.assertEqual(result["post_reserve_effective_unreserved_free_bytes"], 50 * vm.GIB)
        self.assertEqual(result["post_reserve_observed_shared_free_bytes"], pre_reserve_free)
        self.assertEqual(historical_probe_path.read_bytes(), historical_probe_bytes)
        self.assertTrue(creation_path.is_file())
        self.assertEqual(run.call_count, 2)
        self.assertIn(f"{expected_quota // vm.MIB}MiB", run.call_args.args[0])

    def test_active_session_capacity_is_capped_by_immutable_pre_reserve_ceiling(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        self._write_session_marker(
            "a" * 32, volume_uuid, pre_reserve_free_bytes=22 * vm.GIB)
        sample = _session_observation(free_bytes=70 * vm.GIB, vm_volume_uuid=volume_uuid)
        self.assertEqual(vm.conservative_session_unreserved_free_bytes(
            sample, volume_uuid=volume_uuid), 7 * vm.GIB)
        result = vm._check_runtime_phase_sample(
            sample, volume_uuid=volume_uuid, initial_quota_used_bytes=0,
            maximum_phase_growth_bytes=vm.GIB, concurrent_growth_bytes=0)
        self.assertFalse(result["admitted"])
        self.assertTrue(any("8 GiB" in item for item in result["blockers"]))

    def test_active_session_subtracts_reserve_from_falling_live_gross_free(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        self._write_session_marker(
            "a" * 32, volume_uuid, pre_reserve_free_bytes=70 * vm.GIB)
        sample = _session_observation(free_bytes=60 * vm.GIB, vm_volume_uuid=volume_uuid)
        self.assertEqual(vm.conservative_session_unreserved_free_bytes(
            sample, volume_uuid=volume_uuid), 45 * vm.GIB)

    def test_dynamic_quota_blocks_when_reviewed_phase_minimum_does_not_fit(self) -> None:
        result = vm.calculate_dynamic_vm_quota(
            free_bytes=30 * vm.GIB,
            deterministic_uncertainty_bytes=256 * vm.MIB,
            admitted_coexistence_bytes=0,
            minimum_required_quota_bytes=30 * vm.GIB,
            maximum_quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES)
        self.assertFalse(result["admitted"])
        self.assertIsNone(result["quota_bytes"])
        self.assertTrue(any("minimum VM quota" in item for item in result["blockers"]))

    def test_dynamic_quota_never_exceeds_reviewed_maximum(self) -> None:
        result = vm.calculate_dynamic_vm_quota(
            free_bytes=100 * vm.GIB,
            deterministic_uncertainty_bytes=0,
            admitted_coexistence_bytes=0,
            minimum_required_quota_bytes=10 * vm.GIB,
            maximum_quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES)
        self.assertTrue(result["admitted"])
        self.assertEqual(result["quota_bytes"], vm.VM_REVIEWED_QUOTA_MAX_BYTES)

    def test_concurrent_growth_values_never_change_phase_authority(self) -> None:
        common = {
            "mode": vm.VM_MODE,
            "container_uuid": vm.EXPECTED_CONTAINER_UUID,
            "free_bytes": 20 * vm.GIB,
            "quota_bytes": 10 * vm.GIB,
            "quota_used_bytes": vm.GIB,
            "growth_bytes": vm.GIB,
            "uncertainty_bytes": 0,
            "expected_quota_bytes": 10 * vm.GIB,
        }
        results = [vm.calculate_vm_phase_admission(
            **common, concurrent_growth_bytes=sample)
            for sample in (None, 0, 10**18)]
        self.assertTrue(all(result["admitted"] for result in results))
        self.assertEqual({result["quota_demand_bytes"] for result in results}, {vm.GIB})
        self.assertEqual({result["concurrent_growth_authority"] for result in results},
                         {"DIAGNOSTIC_ONLY"})

    def test_statfs_failure_is_diagnostic_and_does_not_block_shared_capacity(self) -> None:
        sample = _observation()
        sample["crosschecks"]["data_statfs_available_bytes"] = None
        sample["crosschecks"]["data_statfs_failure"] = "synthetic statfs failure"
        self.assertEqual(vm._container_blockers(sample), [])

    def test_p2_direct_live_admission_has_no_bootstrap_envelope_or_growth_authority(self) -> None:
        admission = self._run_p2_admission_fixture(free_bytes=45 * vm.GIB,
                                                   concurrent_failure=True)
        self.assertEqual(admission["decision"], "PASS")
        self.assertTrue(self._last_p2_lock_trace["host_scan_under_lock"])
        self.assertFalse(self._last_p2_lock_trace["held"])
        self.assertFalse(self._last_p2_lock_trace["retention_guard_held"])
        self.assertTrue(self._last_p2_lock_trace["marker_update_under_guard"])
        self.assertEqual(admission["stop_safety_profile_mode"],
                         vm.VM_STOP_SAFETY_PROFILE_MODE_LIVE)
        self.assertIsNone(admission["stop_safety_profile_sha256"])
        self.assertNotIn("quota_bootstrap_envelope", admission)
        self.assertEqual(admission["p2_uncertainty_bytes"], 0)
        self.assertEqual(admission["p2_concurrent_growth_authority"], "DIAGNOSTIC_ONLY")
        self.assertEqual(admission["p2_concurrent_growth_measurement"]["status"], "UNKNOWN")
        self.assertEqual(set(admission["p2_component_maxima_bytes"]), {
            "guest_system_user_file_data", "auxiliary_storage_user_file_data"})

    def test_p2_fails_closed_for_active_or_unknown_host_buildlease(self) -> None:
        for host_lease_state in ("ACTIVE", "MALFORMED"):
            with self.subTest(host_lease_state=host_lease_state):
                admission = self._run_p2_admission_fixture(
                    free_bytes=45 * vm.GIB, host_lease_state=host_lease_state)
                self.assertEqual(admission["decision"], "FAIL")
                self.assertTrue(self._last_p2_lock_trace["host_scan_under_lock"])
                self.assertTrue(self._last_p2_lock_trace["marker_update_under_guard"])
                expected = "active Host BuildLease prevents P2 admission" \
                    if host_lease_state == "ACTIVE" else "Host BuildLease state is unknown"
                self.assertTrue(any(expected in item for item in admission["blockers"]))

    def test_p2_blocks_only_when_identified_file_data_exceeds_live_quota_headroom(self) -> None:
        admission = self._run_p2_admission_fixture(
            free_bytes=40 * vm.GIB, quota_bytes=42 * vm.GIB)
        self.assertEqual(admission["decision"], "FAIL")
        self.assertTrue(any("file data exceeds remaining APFS quota" in item
                            for item in admission["blockers"]))

    def test_invalid_present_stop_profile_cannot_fall_back_to_live_monitor_mode(self) -> None:
        admission = self._run_p2_admission_fixture(
            free_bytes=45 * vm.GIB, profile_bytes=b"not a signed stop profile\n")
        self.assertEqual(admission["decision"], "FAIL")
        self.assertEqual(admission["stop_safety_profile_mode"],
                         vm.VM_STOP_SAFETY_PROFILE_MODE_MEASURED)
        self.assertTrue(any("profile is missing or invalid" in item
                            for item in admission["blockers"]))

    def test_stop_response_uses_vm_latency_and_treats_unrelated_growth_as_telemetry(self) -> None:
        safe = vm.calculate_stop_cushion(
            maximum_write_rate_bytes_per_second=64 * vm.MIB,
            maximum_monitor_gap_microseconds=250_000,
            maximum_stop_latency_microseconds=1_000_000,
            concurrent_growth_during_stop_bytes=10**18,
        )
        self.assertTrue(safe["admitted"])
        self.assertEqual(safe["required_cushion_bytes"], 80 * vm.MIB)
        self.assertEqual(safe["concurrent_growth_authority"], "DIAGNOSTIC_ONLY")
        self.assertEqual(safe["response_estimate_scope"],
                         "OPERATIONAL_ONLY_NOT_A_FUTURE_GROWTH_BOUND")
        without_telemetry = vm.calculate_stop_cushion(
            maximum_write_rate_bytes_per_second=64 * vm.MIB,
            maximum_monitor_gap_microseconds=250_000,
            maximum_stop_latency_microseconds=1_000_000,
            concurrent_growth_during_stop_bytes=None,
        )
        self.assertEqual(safe["admitted"], without_telemetry["admitted"])
        self.assertEqual(safe["required_cushion_bytes"],
                         without_telemetry["required_cushion_bytes"])
        unsafe = vm.calculate_stop_cushion(
            maximum_write_rate_bytes_per_second=2 * vm.GIB,
            maximum_monitor_gap_microseconds=250_000,
            maximum_stop_latency_microseconds=2_000_000,
            concurrent_growth_during_stop_bytes=None,
        )
        self.assertFalse(unsafe["admitted"])

    def test_vm_quota_exhaustion_blocks_further_growth(self) -> None:
        result = vm.calculate_vm_phase_admission(
            mode=vm.VM_MODE, container_uuid=vm.EXPECTED_CONTAINER_UUID,
            free_bytes=70 * vm.GIB, quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            quota_used_bytes=50 * vm.GIB, growth_bytes=1,
            uncertainty_bytes=0, concurrent_growth_bytes=0,
        )
        self.assertFalse(result["admitted"])
        self.assertEqual(result["projected_quota_headroom_bytes"], -1)

    def test_sparse_image_physical_cap_is_independent_of_dynamic_quota(self) -> None:
        result = vm.sparse_image_accounting(
            logical_capacity_bytes=64 * vm.GIB,
            maximum_logical_capacity_bytes=64 * vm.GIB,
            physical_allocated_bytes=49 * vm.GIB,
            maximum_physical_allocation_bytes=48 * vm.GIB,
        )
        self.assertFalse(result["admitted"])
        self.assertTrue(any("signed per-image physical cap" in item for item in result["blockers"]))

    def test_sparse_image_logical_limit_is_independent_of_its_physical_cap(self) -> None:
        result = vm.sparse_image_accounting(
            logical_capacity_bytes=9 * vm.GIB,
            maximum_logical_capacity_bytes=8 * vm.GIB,
            physical_allocated_bytes=vm.MIB,
            maximum_physical_allocation_bytes=vm.GIB,
        )
        self.assertFalse(result["admitted"])
        self.assertTrue(any("logical capacity" in item for item in result["blockers"]))

    def test_restore_cleanup_requires_and_preserves_local_hash_and_base_provenance(self) -> None:
        restore_hash = hashlib.sha256(b"local restore payload").hexdigest()
        sealed_base = policy._seal_storage_evidence({
            "hardware_model_sha256": "a" * 64,
            "auxiliary_storage_sha256": "b" * 64,
            "system_image_sha256": "c" * 64,
            "bootstrap_identity_sha256": "d" * 64,
        }, "sealed_base_hmac_sha256")
        restore_record = {
            "phase_id": "P1", "local_sha256_state": "VERIFIED",
            "local_sha256": restore_hash,
        }
        cleanup = policy._seal_storage_evidence({
            "restore_sha256": restore_hash,
            "sealed_base_sha256": hashlib.sha256(
                policy._canonical_pretty_json_bytes(sealed_base)).hexdigest(),
        }, "restore_cleanup_hmac_sha256")
        self.assertEqual(vm.validate_restore_cleanup(
            restore_record=restore_record, sealed_base_record=sealed_base,
            cleanup_action=cleanup), [])
        cleanup["restore_sha256"] = "0" * 64
        self.assertTrue(vm.validate_restore_cleanup(
            restore_record=restore_record, sealed_base_record=sealed_base,
            cleanup_action=cleanup))
        deferred_identity = {
            "device": 16_777_241, "inode": 380,
            "logical_bytes": 26_637_307_067, "allocated_bytes": 26_638_647_296,
            "mtime_ns": 1_700_000_000_000_000_000,
            "ctime_ns": 1_700_000_000_000_000_000,
            "owner_uid": 501, "owner_gid": 20, "mode": 0o600, "link_count": 1,
        }
        deferred_p1 = {
            "status": "PASS", "phase_id": "P1",
            "restore_path": "/Volumes/FGMetal-VM/restore.ipsw",
            "local_sha256_state": "VERIFIED",
            "local_sha256": vm.VM_DEFERRED_RESTORE_SHA256,
            "expected_length_bytes": vm.VM_DEFERRED_RESTORE_LOGICAL_BYTES,
            "actual_length_bytes": vm.VM_DEFERRED_RESTORE_LOGICAL_BYTES,
            "file_identity": {
                key: deferred_identity[key] for key in (
                    "device", "inode", "logical_bytes", "allocated_bytes",
                    "mtime_ns", "ctime_ns")
            },
        }
        vm._assert_deferred_restore_matches_p1(deferred_identity, deferred_p1)
        current_allocation = {
            **deferred_identity,
            "allocated_bytes": deferred_identity["allocated_bytes"] - 4_911_104,
        }
        vm._assert_deferred_restore_matches_p1(current_allocation, deferred_p1)
        self.assertEqual(vm._deferred_restore_allocation_measurement(
            deferred_p1, current_allocation), {
                "p1_historical_allocated_bytes": deferred_identity["allocated_bytes"],
                "current_allocated_bytes": current_allocation["allocated_bytes"],
                "current_minus_p1_bytes": -4_911_104,
                "method": (
                    "st_blocks * 512; filesystem-dependent time-specific allocation measurement"),
            })
        with self.assertRaisesRegex(RuntimeError, "identity differs"):
            vm._assert_deferred_restore_matches_p1(
                {**deferred_identity, "inode": deferred_identity["inode"] + 1}, deferred_p1)
        with self.assertRaisesRegex(RuntimeError, "allocation measurement is invalid"):
            vm._assert_deferred_restore_matches_p1(
                {**deferred_identity, "allocated_bytes": -1}, deferred_p1)
        with self.assertRaisesRegex(RuntimeError, "exact restore payload"):
            vm._assert_deferred_restore_matches_p1(
                deferred_identity, {**deferred_p1, "local_sha256": "0" * 64})

        marker = {
            "session_id": "a" * 32,
            "committed_growth_bytes": 19_350_155_264,
        }
        branch_sha = "b" * 64
        p1_admission_sha = "c" * 64
        p1_verification_sha = "d" * 64
        root_identity = {
            "device": deferred_identity["device"], "inode": 2,
            "owner_uid": 501, "mode": 0o775,
        }
        old_authorization = {
            "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT_AUTHORIZATION",
            "status": "AUTHORIZED",
            "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
            "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
            "vm_containment": "DEFERRED / OPTIONAL",
            "session_id": marker["session_id"],
            "active_marker_path": str(vm.VM_ACTIVE_MARKER_PATH),
            "active_marker_sha256": vm.VM_DEFERRED_ACTIVE_MARKER_SHA256,
            "committed_growth_bytes": marker["committed_growth_bytes"],
            "branch_retirement_receipt_sha256": branch_sha,
            "p1_admission_sha256": p1_admission_sha,
            "p1_verification_sha256": p1_verification_sha,
            "p1_restore_acquisition_historically_accepted": True,
            "p1_upstream_content_digest_validated": False,
            "restore_path": str(vm.VM_DEFERRED_RESTORE_PATH),
            "restore_sha256": vm.VM_DEFERRED_RESTORE_SHA256,
            "restore_logical_bytes": vm.VM_DEFERRED_RESTORE_LOGICAL_BYTES,
            "vm_volume_uuid": vm.VM_DEFERRED_VM_VOLUME_UUID,
            "container_uuid": vm.VM_DEFERRED_CONTAINER_UUID,
            "physical_store_uuid": vm.VM_DEFERRED_PHYSICAL_STORE_UUID,
            "vm_volume_quota_bytes": vm.VM_DEFERRED_QUOTA_BYTES,
            "safety_volume_uuid": vm.VM_DEFERRED_SAFETY_VOLUME_UUID,
            "safety_reserve_bytes": vm.VM_DEFERRED_SAFETY_RESERVE_BYTES,
            "local_restore_payload_retired": True,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "vm_guest_base_sealed": False,
            "p2_cleanup_success_claimed": False,
            "future_vm_work_requires_fresh_p1": True,
            "restore_file_identity": dict(deferred_identity),
            "vm_volume_root_identity": root_identity,
            "restore_allocation_measurement": vm._deferred_restore_allocation_measurement(
                deferred_p1, deferred_identity),
        }
        vm._assert_deferred_restore_authorization_supersedable(
            old_authorization, branch_sha=branch_sha, marker=marker,
            p1_admission_sha=p1_admission_sha,
            p1_verification_sha=p1_verification_sha,
            p1_verification=deferred_p1, identity=current_allocation,
            root_identity=root_identity)
        with self.assertRaisesRegex(RuntimeError, "not the exact replaceable record"):
            vm._assert_deferred_restore_authorization_supersedable(
                {**old_authorization, "local_restore_payload_retired": False},
                branch_sha=branch_sha, marker=marker,
                p1_admission_sha=p1_admission_sha,
                p1_verification_sha=p1_verification_sha,
                p1_verification=deferred_p1, identity=current_allocation,
                root_identity=root_identity)
        with self.assertRaisesRegex(RuntimeError, "identity changed"):
            vm._assert_deferred_restore_authorization_supersedable(
                old_authorization, branch_sha=branch_sha, marker=marker,
                p1_admission_sha=p1_admission_sha,
                p1_verification_sha=p1_verification_sha,
                p1_verification=deferred_p1,
                identity={**current_allocation, "inode": current_allocation["inode"] + 1},
                root_identity=root_identity)

        executable_authorization = {
            "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT_AUTHORIZATION",
            "status": "AUTHORIZED",
            "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
            "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
            "authorization_scope_state": "LOCAL_RESTORE_PRESENT_AWAITING_DELETE",
            "local_restore_payload_retired": False,
            "restore_path": str(vm.VM_DEFERRED_RESTORE_PATH),
            "restore_sha256": vm.VM_DEFERRED_RESTORE_SHA256,
            "restore_logical_bytes": vm.VM_DEFERRED_RESTORE_LOGICAL_BYTES,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "vm_guest_base_sealed": False,
            "p2_cleanup_success_claimed": False,
        }
        vm._assert_deferred_restore_authorization_executable(executable_authorization)
        for rejected_authorization in (
                old_authorization,
                {**executable_authorization, "local_restore_payload_retired": True}):
            with self.assertRaisesRegex(RuntimeError, "not the exact executable"):
                vm._assert_deferred_restore_authorization_executable(rejected_authorization)

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            repo = Path(temp_dir)
            authorization = repo / "R6-VM-DEFERRED-RESTORE-RETIREMENT-AUTHORIZATION-old.json"
            pending = repo / "R6-VM-DEFERRED-RESTORE-RETIREMENT-PENDING-old.json"
            terminal = repo / "R6-VM-DEFERRED-RESTORE-RETIREMENT-old.json"
            for path in (authorization, pending, terminal):
                path.write_text("{}", encoding="utf-8")
            with mock.patch.object(vm.policy, "REPO", repo):
                self.assertEqual(
                    set(vm._deferred_restore_retirement_receipts()), {pending, terminal})

        expected_review_fields = {"reviewed_authorization_sha256": "1" * 64}
        valid_review = {
            "schema_version": 1,
            "status": "PASS",
            "decision": "PASS — DEFERRED VM RESTORE PREDELETE REVIEW",
            "reviewer_identity": "GPT-6 Luna Max independent predelete reviewer",
            "model_identity": "GPT-6 Luna Max",
            "reviewed_at_unix": time.time(),
            "findings": [],
            **expected_review_fields,
        }
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            review_path = Path(temp_dir) / "review.json"
            review_path.write_text(json.dumps(valid_review), encoding="utf-8")
            accepted_review, _ = vm._deferred_fresh_review_json(
                review_path,
                decision="PASS — DEFERRED VM RESTORE PREDELETE REVIEW",
                expected_fields=expected_review_fields)
            self.assertEqual(accepted_review["status"], "PASS")
            vm._require_deferred_review_postdates(
                accepted_review, accepted_review["reviewed_at_unix"] - 1)
            with self.assertRaisesRegex(RuntimeError, "must postdate"):
                vm._require_deferred_review_postdates(
                    accepted_review, accepted_review["reviewed_at_unix"])
            for rejected_fields in (
                    {"schema_version": True}, {"status": "FAIL"}, {"status": None},
                    {"model_identity": "unverified model"},
                    {"reviewed_at_unix": time.time() - vm.VM_DEFERRED_REVIEW_MAX_AGE_SECONDS - 1}):
                review_path.write_text(
                    json.dumps({**valid_review, **rejected_fields}), encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "does not PASS"):
                    vm._deferred_fresh_review_json(
                        review_path,
                        decision="PASS — DEFERRED VM RESTORE PREDELETE REVIEW",
                        expected_fields=expected_review_fields)

        # The signed-record reader returns (record, raw, digest, identity).
        # Authorization must preserve the receipt path separately so its later
        # repository binding never mistakes the returned raw bytes for a path.
        branch_path = Path(__file__).resolve().parent / "branch-retirement.json"
        with mock.patch.object(
                vm, "_read_signed_record_snapshot",
                return_value=({}, b"signed bytes", "a" * 64, (1, 2, 3, 4, 5))) as reader:
            with self.assertRaisesRegex(
                    RuntimeError, "restore retirement requires a signed pre-claim"):
                vm.authorize_deferred_vm_restore_retirement(branch_path)
        self.assertEqual(reader.call_args.args[0], branch_path)

    def test_protected_roots_remain_charged_to_normal_project_scope(self) -> None:
        # Protected evidence roots are already included by the existing scanner;
        # the VM capacity mode must not subtract them as a credit.
        other_scope = 58 * vm.GIB
        protected_root_bytes = 2 * vm.GIB
        charged = _projected_without_vm_commitment(
            current_free_bytes=70 * vm.GIB,
            current_scoped_bytes=other_scope + protected_root_bytes,
            peak_additional_bytes=1,
        )
        hypothetical_uncharged = _projected_without_vm_commitment(
            current_free_bytes=70 * vm.GIB,
            current_scoped_bytes=other_scope,
            peak_additional_bytes=1,
        )
        self.assertFalse(charged["admitted"])
        self.assertEqual(charged["projected_scoped_bytes"], policy.PROJECT_BUDGET + 1)
        self.assertTrue(hypothetical_uncharged["admitted"])
        self.assertEqual(hypothetical_uncharged["projected_scoped_bytes"], other_scope + 1)

    def test_unrelated_project_growth_remains_visible_to_normal_admission(self) -> None:
        before = _projected_without_vm_commitment(
            current_free_bytes=70 * vm.GIB,
            current_scoped_bytes=55 * vm.GIB,
            peak_additional_bytes=5 * vm.GIB,
        )
        after = _projected_without_vm_commitment(
            current_free_bytes=70 * vm.GIB,
            current_scoped_bytes=55 * vm.GIB + 1,
            peak_additional_bytes=5 * vm.GIB,
        )
        self.assertTrue(before["admitted"])
        self.assertEqual(before["projected_scoped_bytes"], policy.PROJECT_BUDGET)
        self.assertFalse(after["admitted"])
        self.assertEqual(after["projected_scoped_bytes"], policy.PROJECT_BUDGET + 1)

    def test_vm_reservation_cannot_authorize_normal_build_or_gate(self) -> None:
        marker = vm.write_active_session_marker(
            session_id="a" * 32, phase_id="P1",
            container_uuid=vm.EXPECTED_CONTAINER_UUID,
            volume_uuid=None, committed_growth_bytes=10 * vm.GIB,
            plan_sha256="b" * 64)
        self.assertTrue(vm.verify_signed_record(marker, "vm_session_hmac_sha256"))
        missing_attestation = Path(self.temp.name) / "missing-gate-attestation.json"
        with mock.patch.object(policy, "GATE0_ATTESTATION_PATH", missing_attestation):
            gate_blockers = policy._gate0_blockers()
        self.assertIn(
            "Gate 0 attestation is missing; graphics experiments remain disabled",
            gate_blockers,
        )
        self.assertFalse(any("normal storage and Gate admission" in item
                             for item in gate_blockers))
        self._assert_capacity_only_commitment(10 * vm.GIB)

    def test_failed_or_unknown_provisioning_inputs_remain_fail_closed(self) -> None:
        result = vm.calculate_vm_phase_admission(
            mode=vm.VM_MODE, container_uuid=vm.EXPECTED_CONTAINER_UUID,
            free_bytes=None, quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            quota_used_bytes=0, growth_bytes=1,
            uncertainty_bytes=0, concurrent_growth_bytes=0,
        )
        self.assertFalse(result["admitted"])
        self.assertTrue(any("free_bytes" in item for item in result["blockers"]))

    def test_restore_metadata_must_be_https_apple_with_finite_length_and_hash(self) -> None:
        valid = vm.validate_restore_metadata(
            "https://updates.cdn-apple.com/restore/image.ipsw",
            26_637_307_067, "a" * 64)
        self.assertEqual(valid["expected_length_bytes"], 26_637_307_067)
        self.assertEqual(vm.validate_restore_metadata(
            "https://updates.cdn-apple.com/restore/source.ipsw",
            26_637_307_067, "a" * 64,
            "HTTPS://UPDATES.CDN-APPLE.COM:443/restore/final.ipsw?token=A%2fB")["final_url"],
            "https://updates.cdn-apple.com/restore/final.ipsw?token=A%2fB")
        for url in (
                "http://updates.cdn-apple.com/image.ipsw",
                "https://apple.com.evil.test/image.ipsw",
                "https://user@updates.cdn-apple.com/image.ipsw",
                "https://updates.cdn-apple.com/image%2.ipsw",
                "https://updates.cdn-apple.com\\@evil.test/image.ipsw"):
            with self.assertRaises(ValueError):
                vm.validate_restore_metadata(url, 26_637_307_067, "a" * 64)

    def _subject_fixture(self) -> tuple[dict[str, object], dict[str, object]]:
        metadata: dict[str, object] = {
            "source_url": "https://updates.cdn-apple.com/restore/image.ipsw?token=A%2fB",
            "final_url": "https://updates.cdn-apple.com/restore/image.ipsw?token=A%2fB",
            "expected_length_bytes": 26_637_307_067,
            "operating_system_version": "27.0.1",
            "build_version": "26A434",
            "is_supported": True,
            "hardware_model_data_sha256": "a" * 64,
            "hardware_model_supported": True,
            "minimum_supported_cpu_count": 2,
            "minimum_supported_memory_size_bytes": 4_294_967_296,
        }
        host: dict[str, object] = {
            "architecture": "arm64",
            "product_version": "27.0.1",
            "build_version": "26A434",
        }
        return metadata, host

    def test_restore_subject_identity_is_domain_separated_and_uses_stable_fields(self) -> None:
        metadata, host = self._subject_fixture()
        legacy = vm._legacy_restore_subject(metadata, host)
        complete = vm._complete_restore_subject(legacy, metadata)
        canonical = vm._canonical_subject_bytes(complete)
        self.assertEqual(canonical, json.dumps(
            complete, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii"))
        digest = vm._restore_subject_digest(complete)
        self.assertRegex(digest, r"^rsid-v1:sha256:[0-9a-f]{64}$")
        self.assertEqual(digest,
                         "rsid-v1:sha256:ff1d88865674c258fa130104a1a34d1bf4cddcabd6e727a778aaa36367f82f8f")
        self.assertNotEqual(digest, vm._restore_subject_digest(complete, legacy=True))
        self.assertEqual(vm._restore_subject_digest(complete),
                         vm._restore_subject_digest(dict(reversed(list(complete.items())))))

    def test_restore_subject_rejects_unknown_missing_and_unsupported_fields(self) -> None:
        metadata, host = self._subject_fixture()
        legacy = vm._legacy_restore_subject(metadata, host)
        complete = vm._complete_restore_subject(legacy, metadata)
        unknown = dict(complete)
        unknown["unreviewed_field"] = "ignored?"
        with self.assertRaisesRegex(ValueError, "unknown"):
            vm._canonical_subject_bytes(unknown)
        incomplete = dict(complete)
        incomplete.pop("compatibility")
        with self.assertRaises(ValueError):
            vm._canonical_subject_bytes(incomplete)
        unknown_hardware = json.loads(json.dumps(complete))
        unknown_hardware["compatibility"]["hardware"]["model_name"] = "unreviewed"
        with self.assertRaisesRegex(ValueError, "missing or unknown"):
            vm._canonical_subject_bytes(unknown_hardware)
        boolean_length = json.loads(json.dumps(complete))
        boolean_length["restore"]["content_length_bytes"] = True
        with self.assertRaises(ValueError):
            vm._canonical_subject_bytes(boolean_length)
        unsupported = dict(metadata)
        unsupported["hardware_model_supported"] = False
        with self.assertRaisesRegex(ValueError, "unsupported"):
            vm._complete_restore_subject(legacy, unsupported)
        unsupported_restore = dict(metadata)
        unsupported_restore["is_supported"] = False
        with self.assertRaises(ValueError):
            vm._legacy_restore_subject(unsupported_restore, host)
        for missing in ("hardware_model_data_sha256", "hardware_model_supported",
                        "minimum_supported_cpu_count", "minimum_supported_memory_size_bytes"):
            incomplete_hardware = dict(metadata)
            incomplete_hardware.pop(missing)
            with self.subTest(missing=missing), self.assertRaisesRegex(ValueError, "missing"):
                vm._hardware_compatibility_tuple(incomplete_hardware)

    def test_legacy_subject_covers_url_length_os_build_host_and_support(self) -> None:
        metadata, host = self._subject_fixture()
        original = vm._legacy_restore_subject(metadata, host)
        identity = vm._restore_subject_digest(original, legacy=True)
        changes = (
            ("source_url", "https://updates.cdn-apple.com/restore/other.ipsw?token=A%2fB", host),
            ("final_url", "https://updates.cdn-apple.com/restore/image.ipsw?token=A%2fC", host),
            ("expected_length_bytes", 26_637_307_068, host),
            ("operating_system_version", "27.0.2", {**host, "product_version": "27.0.2"}),
            ("build_version", "26A435", {**host, "build_version": "26A435"}),
        )
        for field, changed, changed_host in changes:
            with self.subTest(field=field):
                changed_metadata = dict(metadata)
                changed_metadata[field] = changed
                changed_subject = vm._legacy_restore_subject(changed_metadata, changed_host)
                self.assertNotEqual(identity, vm._restore_subject_digest(changed_subject, legacy=True))
        for changed_host in (
                {**host, "architecture": "x86_64"},
                {**host, "product_version": "27.0.2"},
                {**host, "build_version": "26A435"},):
            with self.subTest(host=changed_host):
                if (changed_host["product_version"] != metadata["operating_system_version"]
                        or changed_host["build_version"] != metadata["build_version"]):
                    with self.assertRaises(ValueError):
                        vm._legacy_restore_subject(metadata, changed_host)
                else:
                    changed_subject = vm._legacy_restore_subject(metadata, changed_host)
                    self.assertNotEqual(identity, vm._restore_subject_digest(changed_subject, legacy=True))

    def test_observation_changes_do_not_change_subject_identity(self) -> None:
        metadata, host = self._subject_fixture()
        first = {**metadata, "captured_at_unix": 100.0, "headers": {"etag": "one"}}
        second = {**metadata, "captured_at_unix": 200.0, "headers": {"etag": "two"}}
        first_subject = vm._legacy_restore_subject(first, host)
        second_subject = vm._legacy_restore_subject(second, host)
        self.assertEqual(first_subject, second_subject)
        self.assertEqual(vm._restore_subject_digest(first_subject, legacy=True),
                         vm._restore_subject_digest(second_subject, legacy=True))
        self.assertNotEqual(vm._sha256(json.dumps(first, sort_keys=True).encode()),
                            vm._sha256(json.dumps(second, sort_keys=True).encode()))

    def test_complete_subject_hardware_tuple_changes_identity(self) -> None:
        metadata, host = self._subject_fixture()
        legacy = vm._legacy_restore_subject(metadata, host)
        identity = vm._restore_subject_digest(vm._complete_restore_subject(legacy, metadata))
        for field, value in (("hardware_model_data_sha256", "b" * 64),
                             ("minimum_supported_cpu_count", 4),
                             ("minimum_supported_memory_size_bytes", 8_589_934_592)):
            with self.subTest(field=field):
                changed = dict(metadata)
                changed[field] = value
                changed_subject = vm._complete_restore_subject(legacy, changed)
                self.assertNotEqual(identity, vm._restore_subject_digest(changed_subject))

    def test_url_canonicalization_preserves_path_query_and_escape_case(self) -> None:
        self.assertEqual(vm._canonical_apple_restore_url(
            "HTTPS://UPDATES.CDN-APPLE.COM:443/restore/%2f.ipsw?B=2&A=1"),
            "https://updates.cdn-apple.com/restore/%2f.ipsw?B=2&A=1")
        for changed in (
                "https://updates.cdn-apple.com/restore/%2F.ipsw?B=2&A=1",
                "https://updates.cdn-apple.com/restore/%2f.ipsw?A=1&B=2",
                "https://updates.cdn-apple.com/restore/%2f.ipsw?B=2&A=1&x=1"):
            self.assertNotEqual(vm._canonical_apple_restore_url(
                "https://updates.cdn-apple.com/restore/%2f.ipsw?B=2&A=1"),
                vm._canonical_apple_restore_url(changed))

    def test_freshness_rejects_nan_infinity_boolean_and_future_values(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf"), True, None,
                      101.0, 10 ** 1000):
            with self.subTest(value=value), self.assertRaises(RuntimeError):
                vm._require_fresh_timestamp(value, label="fixture", maximum_age_seconds=10,
                                            now=100.0)

    def test_historical_p0_age_is_not_current_freshness_but_fresh_leaf_is(self) -> None:
        old_p0_timestamp = 1_700_000_000.0
        self.assertEqual(vm._require_nonfuture_timestamp(
            old_p0_timestamp, label="historical P0 metadata"), old_p0_timestamp)
        with self.assertRaisesRegex(RuntimeError, "stale"):
            vm._require_fresh_timestamp(
                old_p0_timestamp, label="fresh observation", maximum_age_seconds=300,
                now=old_p0_timestamp + 301)
        _timestamp, age = vm._require_fresh_timestamp(
            old_p0_timestamp, label="fresh observation", maximum_age_seconds=300,
            now=old_p0_timestamp + 300)
        self.assertEqual(age, 300)

    def test_refresh_lineage_must_point_directly_to_exact_p0_metadata_leaf(self) -> None:
        ancestry: dict[str, object] = {
            "metadata_receipt_path": "R6-VM-RESTORE-UPSTREAM-METADATA-p0.json",
            "metadata_receipt_sha256": "a" * 64,
        }
        lineage: dict[str, object] = {
            "record_type": "R6_VM_RESTORE_METADATA_REFRESH_LINEAGE",
            "status": "PASS",
            "receipt_path": "R6-VM-RESTORE-METADATA-REFRESH-LINEAGE-current.json",
            "p0_ancestry": ancestry,
            "predecessor": {"type": "IMMUTABLE_P0_METADATA_RECEIPT", "sha256": "a" * 64},
            "comparison": {"legacy_subject_equal": True, "changed_fields": []},
        }
        vm._require_direct_p0_metadata_lineage(
            lineage, path_text=str(lineage["receipt_path"]), p0_ancestry=ancestry)
        for changed in (
                {**lineage, "predecessor": {"type": "PRIOR_REFRESH", "sha256": "b" * 64}},
                {**lineage, "receipt_path": "substituted-lineage.json"},
                {**lineage, "comparison": {"legacy_subject_equal": False, "changed_fields": ["build"]}},
        ):
            with self.subTest(changed=changed), self.assertRaisesRegex(RuntimeError, "direct-P0"):
                vm._require_direct_p0_metadata_lineage(
                    changed, path_text=str(lineage["receipt_path"]), p0_ancestry=ancestry)

    def test_restore_payload_inventory_catches_any_session_partial_and_symlink(self) -> None:
        root = Path(self.temp.name) / "payload-inventory"
        root.mkdir()
        descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            self.assertEqual(vm._restore_payload_entries(descriptor), [])
            (root / ".restore.ipsw.partial-prior-session").write_bytes(b"partial")
            self.assertEqual(vm._restore_payload_entries(descriptor),
                             [".restore.ipsw.partial-prior-session"])
            (root / ".restore.ipsw.partial-prior-session").unlink()
            (root / "restore.ipsw").symlink_to(root / "missing-target")
            self.assertEqual(vm._restore_payload_entries(descriptor), ["restore.ipsw"])
        finally:
            os.close(descriptor)

    def test_unique_signed_receipt_write_is_no_overwrite_and_readback_hash_is_exact(self) -> None:
        receipt_path = Path(self.temp.name) / "unique-lineage.json"
        first = vm._write_signed_json_unique(
            receipt_path, {"record_type": "FIXTURE", "status": "PASS"},
            "vm_metadata_refresh_lineage_hmac_sha256")
        record, raw, receipt_sha, _identity = vm._read_signed_record_snapshot(
            receipt_path, "vm_metadata_refresh_lineage_hmac_sha256")
        self.assertEqual(record, first)
        self.assertEqual(receipt_sha, vm._sha256(raw))
        with self.assertRaises(FileExistsError):
            vm._write_signed_json_unique(
                receipt_path, {"record_type": "REPLACEMENT", "status": "PASS"},
                "vm_metadata_refresh_lineage_hmac_sha256")
        reread, reread_raw, reread_sha, _identity = vm._read_signed_record_snapshot(
            receipt_path, "vm_metadata_refresh_lineage_hmac_sha256")
        self.assertEqual(reread, first)
        self.assertEqual(reread_raw, raw)
        self.assertEqual(reread_sha, receipt_sha)

    def test_signed_receipt_tampering_is_rejected_before_use(self) -> None:
        receipt_path = Path(self.temp.name) / "tampered-metadata.json"
        vm._write_signed_json(receipt_path, {
            "record_type": "R6_VM_RESTORE_METADATA_REFRESH_LINEAGE",
            "status": "PASS",
        }, "vm_metadata_refresh_lineage_hmac_sha256")
        tampered = json.loads(receipt_path.read_text(encoding="utf-8"))
        tampered["status"] = "FAIL"
        receipt_path.write_bytes(vm.policy._canonical_pretty_json_bytes(tampered))
        with self.assertRaisesRegex(RuntimeError, "unauthenticated"):
            vm._read_signed_record_snapshot(
                receipt_path, "vm_metadata_refresh_lineage_hmac_sha256")

    def test_swift_probe_binds_vz_hardware_requirements(self) -> None:
        source = vm.VM_RESTORE_METADATA_PROBE_SOURCE.read_text(encoding="utf-8")
        for required in (
                "mostFeaturefulSupportedConfiguration",
                "hardwareModel.dataRepresentation",
                "hardwareModel.isSupported",
                "minimumSupportedCPUCount",
                "minimumSupportedMemorySize"):
            with self.subTest(required=required):
                self.assertIn(required, source)

    def test_metadata_preflight_fails_before_compiler_or_probe_without_fresh_g0c(self) -> None:
        with mock.patch.object(
                vm, "verify_fresh_g0c_review",
                side_effect=RuntimeError("synthetic missing fresh G0C PASS")) as verify_g0c, \
                mock.patch.object(vm.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "missing fresh G0C PASS"):
                vm.fetch_latest_restore_metadata(
                    receipt_path=Path(self.temp.name) / "must-not-be-written.json")
        verify_g0c.assert_called_once_with()
        run.assert_not_called()
        self.assertFalse((Path(self.temp.name) / "must-not-be-written.json").exists())

    def test_fresh_metadata_receipt_must_bind_the_current_g0c_review(self) -> None:
        g0c_at_fetch = {"snapshot_sha256": "a" * 64}
        current_g0c = {"snapshot_sha256": "b" * 64}
        record: dict[str, object] = {
            "schema_version": 1,
            "mode": vm.VM_MODE,
            "status": "PASS",
            "probe": "VZMacOSRestoreImage.latestSupported+HTTPS_HEAD",
            "source_url": "https://updates.cdn-apple.com/restore/image.ipsw",
            "final_url": "https://updates.cdn-apple.com/restore/image.ipsw",
            "expected_length_bytes": 26_637_307_067,
            "operating_system_version": "27.0.1",
            "build_version": "26A434",
            "is_supported": True,
            "host": {"architecture": "arm64", "product_version": "27.0.1",
                     "build_version": "26A434"},
            "hardware_model_data_sha256": "c" * 64,
            "hardware_model_supported": True,
            "minimum_supported_cpu_count": 2,
            "minimum_supported_memory_size_bytes": 4_294_967_296,
            "http_status": 200,
            "headers": {key: None for key in (
                "etag", "last_modified", "digest", "content_md5",
                "x_apple_content_sha256", "x_apple_signature")},
            "probe_source_sha256": "d" * 64,
            "probe_executable_sha256": "e" * 64,
            "probe_entitlements_sha256": "f" * 64,
            "probe_entitlements": {"com.apple.security.virtualization": True},
            "probe_entitlement_readback_sha256": "1" * 64,
            "probe_compile_stdout_sha256": "2" * 64,
            "probe_compile_stderr_sha256": "3" * 64,
            "captured_at_unix": time.time(),
            "receipt_path": None,
            "g0c_identity": g0c_at_fetch,
        }
        record["metadata_sha256"] = vm._sha256(
            vm.policy._canonical_pretty_json_bytes(record))
        record["restore_metadata_hmac_sha256"] = "synthetic-signature"
        snapshot = (record, vm.policy._canonical_pretty_json_bytes(record),
                    "4" * 64, (1, 2, 3, 4, 5))
        with mock.patch.object(vm, "_read_signed_record_snapshot", return_value=snapshot), \
                mock.patch.object(vm, "_require_fresh_timestamp", return_value=(time.time(), 0.0)), \
                mock.patch.object(vm, "_controller_identity", return_value={
                    "restore_metadata_probe_sha256": "d" * 64}), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=current_g0c):
            with self.assertRaisesRegex(RuntimeError, "current fresh G0C review"):
                vm._read_latest_restore_metadata_snapshot(Path(self.temp.name) / "fresh.json")

    def test_get_redirect_must_be_the_exact_admitted_final_url(self) -> None:
        source = "https://updates.cdn-apple.com/restore/source.ipsw?x=1"
        final = "https://updates.cdn-apple.com/restore/final.ipsw?x=1"
        handler = vm._AppleOnlyRedirectHandler(1234, "a" * 64, source, final)
        accepted = handler.redirect_request(
            vm.Request(source), None, 302, "Found", {}, final)
        self.assertIsNotNone(accepted)
        self.assertEqual(handler.redirect_target, final)
        for changed in (
                "https://updates.cdn-apple.com/restore/other.ipsw?x=1",
                "https://updates.cdn-apple.com/restore/final.ipsw?x=2",
                "https://other.cdn-apple.com/restore/final.ipsw?x=1"):
            handler = vm._AppleOnlyRedirectHandler(1234, "a" * 64, source, final)
            with self.subTest(target=changed), self.assertRaises(RuntimeError):
                handler.redirect_request(vm.Request(source), None, 302, "Found", {}, changed)
        handler = vm._AppleOnlyRedirectHandler(1234, "a" * 64, source, final)
        with self.assertRaises(RuntimeError):
            handler.validate_final_url("https://updates.cdn-apple.com/restore/final.ipsw?x=2")

    def test_pending_marker_label_is_not_download_authority(self) -> None:
        self._write_session_marker("a" * 32, "11111111-1111-1111-1111-111111111111",
                                   phase_id="P1_ADMISSION", state="ACTIVE")
        with self.assertRaisesRegex(RuntimeError, "pending marker"):
            vm._verify_p1_download_authority()

    def test_transition_or_nested_phase_cannot_substitute_for_p1_outer_authority(self) -> None:
        session_id = "a" * 32
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        p1_path = repo / "p1.json"
        with mock.patch.object(vm.policy, "REPO", repo):
            receipt = vm._write_signed_json(p1_path, {
                "record_type": "R6_VM_P0_REVALIDATED_FOR_P1",
                "decision": "PASS",
                "authorized_phase": "P1",
                "phase_evaluation": {"authorized_phase": "P1"},
                "session_id": session_id,
                "vm_volume_uuid": volume_uuid,
            }, "vm_admission_hmac_sha256")
            p1_sha = vm._sha256(vm.policy._canonical_pretty_json_bytes(receipt))
            vm._write_signed_json(vm.VM_ACTIVE_MARKER_PATH, {
                "session_id": session_id,
                "state": "ACTIVE",
                "phase_id": "P1",
                "volume_uuid": volume_uuid,
                "p1_admission_path": "p1.json",
                "p1_admission_sha256": p1_sha,
            }, "vm_session_hmac_sha256")
            with self.assertRaisesRegex(RuntimeError, "outer authority"):
                vm._verify_p1_download_authority()

    def test_marker_requires_exact_p1_receipt_and_keeps_failure_sticky(self) -> None:
        session_id = "a" * 32
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        self._write_session_marker(session_id, volume_uuid, phase_id="P1_ADMISSION")
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        p1_path = repo / "p1.json"
        with mock.patch.object(vm.policy, "REPO", repo):
            receipt = vm._write_signed_json(p1_path, {
                "record_type": "R6_VM_P1_RESTORE_DOWNLOAD_ADMISSION",
                "mode": vm.VM_MODE,
                "decision": "PASS",
                "authorization": "DOWNLOAD_AND_VERIFY_RESTORE_ONLY",
                "authorized_phase": "P1",
                "p1_authorized": True,
                "restore_download_authorized": True,
                "p2_authorized": False,
                "vm_start_authorized": False,
                "session_id": session_id,
                "vm_volume_uuid": volume_uuid,
                "receipt_path": "p1.json",
            }, "vm_admission_hmac_sha256")
            p1_sha = vm._sha256(vm.policy._canonical_pretty_json_bytes(receipt))
            marker_sha = vm._file_sha256(vm.VM_ACTIVE_MARKER_PATH)
            vm.update_active_session_marker(
                session_id, phase_id="P1", p1_admission_path=p1_path,
                p1_admission_sha256=p1_sha, expected_marker_sha256=marker_sha)
            marker = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
            self.assertEqual(marker["p1_admission_sha256"], p1_sha)
            self.assertEqual(marker["p1_admission_path"], "p1.json")
            with self.assertRaisesRegex(RuntimeError, "backwards"):
                vm.update_active_session_marker(session_id, phase_id="P1_ADMISSION")
            vm.update_active_session_marker(session_id, state="FAILED", phase_id="P1_DOWNLOAD_FAILED")
            with self.assertRaisesRegex(RuntimeError, "sticky"):
                vm.update_active_session_marker(session_id, state="ACTIVE", phase_id="P1")

    def test_pending_p1_marker_cannot_jump_to_a_later_phase(self) -> None:
        self._write_session_marker(
            "a" * 32, "11111111-1111-1111-1111-111111111111", phase_id="P1_ADMISSION")
        with self.assertRaisesRegex(RuntimeError, "pass P1"):
            vm.update_active_session_marker("a" * 32, phase_id="P2_AUTHORIZED")

    def test_pending_p0_marker_is_bound_to_creation_contract_not_initial_marker_hash(self) -> None:
        now = time.time()
        session_id = "a" * 32
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        safety_uuid = "22222222-2222-2222-2222-222222222222"
        phase_plan = {"phases": {"P1": {"growth_bytes": 1234}}}
        creation = {
            "active_marker_sha256": "a" * 64,
            "initial_phase_plan": phase_plan,
            "session_id": session_id,
            "volume_uuid": volume_uuid,
            "volume_name": vm.VM_VOLUME_NAME,
            "quota_bytes": 50 * vm.GIB,
            "safety_volume_uuid": safety_uuid,
            "safety_volume_in_use_bytes": 0,
            "safety_volume_reserve_bytes": vm.VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "safety_container_uuid": vm.EXPECTED_CONTAINER_UUID,
            "safety_physical_store_uuid": vm.EXPECTED_PHYSICAL_STORE_UUID,
            "pre_reserve_shared_free_bytes": 70 * vm.GIB,
            "unreserved_free_ceiling_bytes": 55 * vm.GIB,
        }
        marker = {
            "created_at_unix": now - 10,
            "updated_at_unix": now,
            "state": "ACTIVE",
            "phase_id": "P1_ADMISSION",
            "session_id": session_id,
            "container_uuid": vm.EXPECTED_CONTAINER_UUID,
            "volume_uuid": volume_uuid,
            "volume_name": vm.VM_VOLUME_NAME,
            "quota_bytes": 50 * vm.GIB,
            "safety_volume_uuid": safety_uuid,
            "safety_volume_in_use_bytes": 0,
            "safety_reserve_bytes": vm.VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "safety_container_uuid": vm.EXPECTED_CONTAINER_UUID,
            "safety_physical_store_uuid": vm.EXPECTED_PHYSICAL_STORE_UUID,
            "pre_reserve_shared_free_bytes": 70 * vm.GIB,
            "unreserved_free_ceiling_bytes": 55 * vm.GIB,
            "plan_sha256": vm._sha256(vm.policy._canonical_pretty_json_bytes(phase_plan)),
            "committed_growth_bytes": 1234,
            "p1_admission_path": None,
            "p1_admission_sha256": None,
        }
        vm._require_unchanged_pending_p0_marker(marker, creation)
        for changed in ({**marker, "phase_id": "P1"},
                        {**marker, "p1_admission_sha256": "b" * 64},
                        {**marker, "quota_bytes": 49 * vm.GIB}):
            with self.subTest(changed=changed), self.assertRaises(RuntimeError):
                vm._require_unchanged_pending_p0_marker(changed, creation)

    def test_precreation_plan_must_fit_quota_and_shared_free_floor(self) -> None:
        decision = vm.evaluate_initial_plan(
            _observation(), 26_637_307_067, concurrent_growth_bound_bytes=0)
        self.assertTrue(decision["admitted_for_p1"])
        too_small = vm.evaluate_initial_plan(
            _observation(free_bytes=40 * vm.GIB), 26_637_307_067,
            concurrent_growth_bound_bytes=0)
        self.assertFalse(too_small["admitted_for_p1"])
        self.assertIsNone(too_small["authorized_phase"])

    def test_concurrent_growth_bound_comes_from_live_shared_container_samples(self) -> None:
        observations = [
            _observation(free_bytes=70 * vm.GIB),
            _observation(free_bytes=70 * vm.GIB - 24 * vm.MIB),
        ]
        result = vm.measure_concurrent_growth_bound(
            sample_observation=lambda: observations.pop(0), sample_count=2,
            sample_interval_seconds=0.001)
        self.assertEqual(result["observed_max_interval_growth_bytes"], 24 * vm.MIB)
        self.assertEqual(result["concurrent_growth_bound_bytes"], 24 * vm.MIB)
        self.assertNotIn("safety_pad_bytes", result)

    def test_stop_profile_metrics_are_derived_from_signed_monitor_evidence(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        g0c_identity = {"snapshot_sha256": "c" * 64}
        build = "26A434"
        controller = vm._controller_identity()
        now = time.time()
        run = vm._seal({
            "schema_version": 1,
            "mode": vm.VM_MODE,
            "session_id": "d" * 32,
            "phase_id": "P2",
            "status": "PASS_PHASE_COMPLETE",
            "host_build_version": build,
            "volume_uuid": volume_uuid,
            "controller_identity": controller,
            "g0c_identity": g0c_identity,
            "phase_quiescent": True,
            "vm_state": "STOPPED",
            "all_writable_handles_released": True,
            "serial_and_attachment_handles_released": True,
            "samples": [
                {"monitor_sampled_at_unix": now - 1.0,
                 "vm_volume_used_bytes": 1 * vm.GIB,
                 "shared_container_free_bytes": 50 * vm.GIB},
                {"monitor_sampled_at_unix": now - 0.75,
                 "vm_volume_used_bytes": 1 * vm.GIB + 256 * vm.MIB,
                 "shared_container_free_bytes": 50 * vm.GIB - 256 * vm.MIB},
            ],
            "stop_measurement": {
                "phase_quiescent": True,
                "vm_stopped_and_handles_released": True,
                "stop_latency_microseconds": 100_000,
                "concurrent_growth_during_stop_bytes": 0,
            },
        }, "vm_phase_monitor_hmac_sha256")
        evidence_path = Path(self.temp.name) / "stop-evidence.json"
        evidence_path.write_text(json.dumps({"runs": [run]}), encoding="utf-8")
        evidence_path.chmod(0o600)
        with mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity), \
                mock.patch.object(vm.subprocess, "run", return_value=mock.Mock(stdout=build + "\n")):
            profile_path = Path(self.temp.name) / "stop-profile.json"
            with self.assertRaises(TypeError):
                vm.create_stop_safety_profile(
                    measurement_evidence_path=evidence_path,
                    maximum_write_rate_bytes_per_second=0)
            profile = vm.write_stop_safety_profile(
                measurement_evidence_path=evidence_path, profile_path=profile_path)
            self.assertEqual(profile["maximum_write_rate_bytes_per_second"], 1_024 * vm.MIB)
            verified, blockers = vm._verify_stop_safety_profile(profile_path)
        self.assertEqual(blockers, [])
        self.assertEqual(verified["maximum_stop_latency_microseconds"], 100_000)

    def test_active_marker_survives_malformed_state_as_a_blocker(self) -> None:
        marker = vm.VM_ACTIVE_MARKER_PATH
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{"broken":true}\n', encoding="utf-8")
        marker.chmod(0o600)
        blockers = policy._active_vm_capacity_session_blockers()
        self.assertTrue(any("malformed or unknown" in item for item in blockers))

    def test_diskutil_creation_help_requires_quota_semantics(self) -> None:
        help_text = "-quota maximum file data usage limit\n-reserve optional minimum guaranteed file data capacity"
        fake = mock.Mock(returncode=0, stdout=help_text)
        with mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm.subprocess, "run", return_value=fake) as run:
            info = vm.verify_quota_cli_support()
        self.assertEqual(info["reviewed_quota_max_bytes"], 50 * vm.GIB)
        self.assertEqual(info["safety_volume_reserve_bytes"], 15 * vm.GIB)
        self.assertEqual(info["vm_volume_reserve_bytes"], 0)
        run.assert_called_once()

    def test_phase_monitor_polls_and_records_final_sample(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        g0c_identity, profile, profile_path, admission_path = self._prepare_p2_monitor(volume_uuid)
        done_calls = 0

        def done() -> bool:
            nonlocal done_calls
            done_calls += 1
            return True

        with mock.patch.object(vm, "_verify_stop_safety_profile", return_value=(profile, [])), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity):
            result = vm.monitor_vm_phase(
                session_id="a" * 32, volume_uuid=volume_uuid, phase_id="P2",
                initial_quota_used_bytes=0, maximum_phase_growth_bytes=vm.GIB,
                operation_done=done,
                cancel_operation=lambda: self.fail("successful phase must not be cancelled"),
                wait_operation_quiescent=lambda _timeout: True,
                stop_vm=lambda: None,
                wait_vm_stopped_and_handles_released=lambda _timeout: True,
                stop_safety_profile_path=profile_path,
                p2_admission_path=admission_path,
                sample_observation=lambda: _session_observation(vm_volume_uuid=volume_uuid),
                concurrent_growth_bytes=0,
                poll_interval_seconds=0.01)
        self.assertEqual(result["status"], "PASS_PHASE_COMPLETE")
        self.assertEqual(result["sample_count"], 3)
        self.assertEqual(result["vm_state"], "STOPPED")
        self.assertTrue(result["all_writable_handles_released"])
        self.assertEqual(done_calls, 1)

    def test_p2_live_monitor_accepts_missing_growth_telemetry_and_keeps_direct_checks(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        g0c_identity, profile, profile_path, admission_path = self._prepare_p2_monitor(volume_uuid)
        evidence_path = Path(self.temp.name) / "live-p2-monitor.json"
        done_calls = 0

        def done_after_first_sample() -> bool:
            nonlocal done_calls
            done_calls += 1
            return done_calls > 1

        with mock.patch.object(vm, "_verify_stop_safety_profile", return_value=(profile, [])), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity):
            result = vm.monitor_vm_phase(
                session_id="a" * 32, volume_uuid=volume_uuid, phase_id="P2",
                initial_quota_used_bytes=0, maximum_phase_growth_bytes=vm.GIB,
                operation_done=done_after_first_sample,
                cancel_operation=lambda: self.fail("live P2 should complete without cancellation"),
                wait_operation_quiescent=lambda _timeout: True,
                stop_vm=lambda: None,
                wait_vm_stopped_and_handles_released=lambda _timeout: True,
                stop_safety_profile_path=profile_path,
                p2_admission_path=admission_path,
                sample_observation=lambda: _session_observation(vm_volume_uuid=volume_uuid),
                evidence_receipt_path=evidence_path,
                concurrent_growth_bytes=None,
                poll_interval_seconds=0.01)
        self.assertEqual(result["status"], "PASS_PHASE_COMPLETE")
        self.assertNotIn("bootstrap_stop_cushion", result)
        self.assertEqual(result["safety_volume_uuid"], "33333333-3333-3333-3333-333333333333")
        self.assertEqual(
            result["samples"][0]["live_capacity_and_quota_check"]["concurrent_growth_authority"],
            "DIAGNOSTIC_ONLY")
        self.assertTrue(vm.verify_signed_record(result, "vm_phase_monitor_hmac_sha256"))
        self.assertTrue(vm.verify_signed_record(
            json.loads(evidence_path.read_text(encoding="utf-8")),
            "vm_phase_monitor_hmac_sha256"))

    def test_phase_monitor_cancels_and_keeps_sticky_marker_at_8_gib(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        g0c_identity, profile, profile_path, admission_path = self._prepare_p2_monitor(
            volume_uuid, committed_growth_bytes=vm.GIB)
        callbacks: list[str] = []
        with mock.patch.object(vm, "_verify_stop_safety_profile", return_value=(profile, [])), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity):
            with self.assertRaises(vm.VMPhaseViolation) as raised:
                vm.monitor_vm_phase(
                    session_id="a" * 32, volume_uuid=volume_uuid, phase_id="P2",
                    initial_quota_used_bytes=0, maximum_phase_growth_bytes=vm.GIB,
                    operation_done=lambda: False,
                    cancel_operation=lambda: callbacks.append("cancel"),
                    wait_operation_quiescent=lambda _timeout: True,
                    sample_observation=lambda: _session_observation(
                        free_bytes=23 * vm.GIB, vm_volume_uuid=volume_uuid),
                    stop_vm=lambda: callbacks.append("stop"),
                    wait_vm_stopped_and_handles_released=lambda _timeout: True,
                    stop_safety_profile_path=profile_path,
                    p2_admission_path=admission_path,
                    concurrent_growth_bytes=0)
        self.assertEqual(callbacks, ["cancel", "stop"])
        self.assertEqual(raised.exception.record["status"], "FAILED_STOPPED")
        self.assertTrue(any("8 GiB" in item for item in raised.exception.record["failure"]["blockers"]))
        marker = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        self.assertEqual(marker["state"], "FAILED")
        self.assertEqual(marker["phase_id"], "P2")
        self._assert_capacity_only_commitment(vm.GIB)

    def test_live_vm_writer_requires_stop_callbacks_and_does_not_claim_stopped_without_proof(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        g0c_identity, profile, profile_path, admission_path = self._prepare_p2_monitor(volume_uuid)
        with mock.patch.object(vm, "_verify_stop_safety_profile", return_value=(profile, [])), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity):
            with self.assertRaisesRegex(RuntimeError, "explicit VM-stop"):
                vm.monitor_vm_phase(
                    session_id="a" * 32, volume_uuid=volume_uuid, phase_id="P2",
                    initial_quota_used_bytes=0, maximum_phase_growth_bytes=vm.GIB,
                    operation_done=lambda: True,
                    cancel_operation=lambda: None,
                    wait_operation_quiescent=lambda _timeout: True,
                    stop_safety_profile_path=profile_path,
                    p2_admission_path=admission_path,
                    concurrent_growth_bytes=0)
        with mock.patch.object(vm, "_verify_stop_safety_profile", return_value=(profile, [])), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity):
            with self.assertRaises(vm.VMPhaseViolation) as raised:
                vm.monitor_vm_phase(
                    session_id="a" * 32, volume_uuid=volume_uuid, phase_id="P2",
                    initial_quota_used_bytes=0, maximum_phase_growth_bytes=vm.GIB,
                    operation_done=lambda: True,
                    cancel_operation=lambda: None,
                    wait_operation_quiescent=lambda _timeout: False,
                    stop_vm=lambda: None,
                    wait_vm_stopped_and_handles_released=lambda _timeout: False,
                    stop_safety_profile_path=profile_path,
                    p2_admission_path=admission_path,
                    sample_observation=lambda: _session_observation(
                        free_bytes=23 * vm.GIB, vm_volume_uuid=volume_uuid),
                    concurrent_growth_bytes=0)
        self.assertEqual(raised.exception.record["status"], "UNKNOWN_BLOCKING")
        self.assertEqual(raised.exception.record["vm_state"], "UNKNOWN")
        self.assertFalse(raised.exception.record["all_writable_handles_released"])

    def test_live_vm_monitor_rejects_unauthorized_phase_aliases(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        g0c_identity, profile, profile_path, admission_path = self._prepare_p2_monitor(volume_uuid)
        with mock.patch.object(vm, "_verify_stop_safety_profile", return_value=(profile, [])), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity):
            with self.assertRaisesRegex(RuntimeError, "not an authorized live-writer phase"):
                vm.monitor_vm_phase(
                    session_id="a" * 32, volume_uuid=volume_uuid, phase_id="P2_TEST",
                    initial_quota_used_bytes=0, maximum_phase_growth_bytes=vm.GIB,
                    operation_done=lambda: True,
                    cancel_operation=lambda: None,
                    wait_operation_quiescent=lambda _timeout: True,
                    stop_vm=lambda: None,
                    wait_vm_stopped_and_handles_released=lambda _timeout: True,
                    stop_safety_profile_path=profile_path,
                    p2_admission_path=admission_path,
                    concurrent_growth_bytes=0)

    def test_live_monitor_fails_when_safety_volume_becomes_mounted(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        g0c_identity, profile, profile_path, admission_path = self._prepare_p2_monitor(
            volume_uuid, committed_growth_bytes=vm.GIB)
        callbacks: list[str] = []
        with mock.patch.object(vm, "_verify_stop_safety_profile", return_value=(profile, [])), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=g0c_identity):
            with self.assertRaises(vm.VMPhaseViolation) as raised:
                vm.monitor_vm_phase(
                    session_id="a" * 32, volume_uuid=volume_uuid, phase_id="P2",
                    initial_quota_used_bytes=0, maximum_phase_growth_bytes=vm.GIB,
                    operation_done=lambda: False,
                    cancel_operation=lambda: callbacks.append("cancel"),
                    wait_operation_quiescent=lambda _timeout: True,
                    stop_vm=lambda: callbacks.append("stop"),
                    wait_vm_stopped_and_handles_released=lambda _timeout: True,
                    stop_safety_profile_path=profile_path,
                    p2_admission_path=admission_path,
                    sample_observation=lambda: _session_observation(safety_mounted=True),
                    concurrent_growth_bytes=None,
                    poll_interval_seconds=0.01)
        self.assertEqual(raised.exception.record["status"], "FAILED_STOPPED")
        self.assertTrue(any("unmounted state" in item
                            for item in raised.exception.record["failure"]["blockers"]))
        self.assertEqual(callbacks, ["cancel", "stop"])
        marker = vm._read_signed_record(vm.VM_ACTIVE_MARKER_PATH, "vm_session_hmac_sha256")
        self.assertEqual(marker["state"], "FAILED")
        self.assertEqual(marker["phase_id"], "P2")
        self._assert_capacity_only_commitment(vm.GIB)

    def test_recovery_release_rejects_active_or_successful_session_proofs(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        marker = self._write_session_marker("a" * 32, volume_uuid)
        failure_proof = vm._seal({
            "status": "FAILED_STOPPED", "session_id": "a" * 32,
            "volume_uuid": volume_uuid,
            "safety_volume_uuid": "33333333-3333-3333-3333-333333333333",
            "phase_quiescent": True, "vm_state": "STOPPED",
            "all_writable_handles_released": True,
            "serial_and_attachment_handles_released": True,
        }, "vm_phase_monitor_hmac_sha256")
        blockers = vm._safety_reserve_recovery_release_blockers(marker, failure_proof)
        self.assertTrue(any("failed VM session" in item for item in blockers))
        success_proof = vm._seal({
            **{key: value for key, value in failure_proof.items()
               if key != "vm_phase_monitor_hmac_sha256"},
            "status": "PASS_PHASE_COMPLETE",
        }, "vm_phase_monitor_hmac_sha256")
        failed_marker = dict(marker, state="FAILED", phase_id="P2")
        self.assertTrue(vm._safety_reserve_recovery_release_blockers(
            failed_marker, success_proof))

    def _write_failed_p0_marker(self, *, phase_id: str = "SAFETY_RESERVE_CREATE_FAILED",
                                 state: str = "FAILED",
                                 safety_uuid: str | None = None,
                                 safety_used: int | None = None) -> dict[str, object]:
        return vm.write_active_session_marker(
            session_id="a" * 32, phase_id=phase_id,
            container_uuid=vm.EXPECTED_CONTAINER_UUID,
            volume_uuid=None, committed_growth_bytes=0,
            plan_sha256="b" * 64, quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            safety_volume_uuid=safety_uuid,
            safety_volume_in_use_bytes=safety_used,
            safety_container_uuid=(vm.EXPECTED_CONTAINER_UUID if safety_uuid else None),
            safety_physical_store_uuid=(vm.EXPECTED_PHYSICAL_STORE_UUID if safety_uuid else None),
            pre_reserve_shared_free_bytes=70 * vm.GIB,
            unreserved_free_ceiling_bytes=55 * vm.GIB,
            state=state)

    def test_partial_p0_recovery_releases_exact_unmounted_reserve_and_clears_marker(self) -> None:
        safety_uuid = "33333333-3333-3333-3333-333333333333"
        self._write_failed_p0_marker(
            phase_id="POST_RESERVE_QUOTA_FAILED", safety_uuid=safety_uuid, safety_used=0)
        before = _observation(safety_volume_uuid=safety_uuid)
        after = _observation()
        receipt_path = Path(self.temp.name) / "p0-recovery.json"
        completed = mock.Mock(returncode=0, stdout="deleted", stderr="")
        with mock.patch.object(policy, "_acquire_storage_allocation_lock",
                               return_value=mock.MagicMock()), \
                mock.patch.object(vm, "capture_host_observation",
                                  side_effect=[before, before, before, after]), \
                mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm.subprocess, "run", return_value=completed) as run:
            result = vm.recover_partial_vm_volume_creation(receipt_path=receipt_path)
        self.assertEqual(result["status"], "FAILED / RECOVERY")
        self.assertTrue(result["pre_vm_creation_recovery"])
        self.assertTrue(result["safety_reserve_released"])
        self.assertFalse(vm.VM_ACTIVE_MARKER_PATH.exists())
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-1], "/dev/disk3s9")

    def test_partial_p0_recovery_releases_bound_reserve_after_failed_vm_volume_create(self) -> None:
        safety_uuid = "33333333-3333-3333-3333-333333333333"
        self._write_failed_p0_marker(
            phase_id="VM_VOLUME_CREATE_FAILED", safety_uuid=safety_uuid, safety_used=0)
        before = _observation(safety_volume_uuid=safety_uuid)
        after = _observation()
        receipt_path = Path(self.temp.name) / "vm-create-failed-p0-recovery.json"
        completed = mock.Mock(returncode=0, stdout="deleted", stderr="")
        with mock.patch.object(policy, "_acquire_storage_allocation_lock",
                               return_value=mock.MagicMock()), \
                mock.patch.object(vm, "capture_host_observation",
                                  side_effect=[before, before, before, after]), \
                mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm.subprocess, "run", return_value=completed) as run:
            result = vm.recover_partial_vm_volume_creation(receipt_path=receipt_path)
        self.assertTrue(result["safety_reserve_released"])
        self.assertEqual(result["status"], "FAILED / RECOVERY")
        run.assert_called_once()

    def test_partial_p0_recovery_keeps_reserve_if_vm_volume_exists_after_create_error(self) -> None:
        safety_uuid = "33333333-3333-3333-3333-333333333333"
        vm_uuid = "11111111-1111-1111-1111-111111111111"
        self._write_failed_p0_marker(
            phase_id="VM_VOLUME_CREATE_FAILED", safety_uuid=safety_uuid, safety_used=0)
        observation = _observation(
            vm_volume_uuid=vm_uuid, safety_volume_uuid=safety_uuid)
        with mock.patch.object(policy, "_acquire_storage_allocation_lock",
                               return_value=mock.MagicMock()), \
                mock.patch.object(vm, "capture_host_observation", return_value=observation), \
                mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "safety-volume recovery preflight failed"):
                vm.recover_partial_vm_volume_creation(
                    receipt_path=Path(self.temp.name) / "vm-present-p0-recovery.json")
        self.assertTrue(vm.VM_ACTIVE_MARKER_PATH.exists())
        run.assert_not_called()

    def test_partial_p0_recovery_clears_failed_marker_when_create_left_no_volume(self) -> None:
        self._write_failed_p0_marker()
        receipt_path = Path(self.temp.name) / "p0-no-reserve-recovery.json"
        with mock.patch.object(policy, "_acquire_storage_allocation_lock",
                               return_value=mock.MagicMock()), \
                mock.patch.object(vm, "capture_host_observation", return_value=_observation()):
            result = vm.recover_partial_vm_volume_creation(receipt_path=receipt_path)
        self.assertEqual(result["status"], "FAILED / RECOVERY")
        self.assertTrue(result["pre_vm_creation_recovery"])
        self.assertFalse(result["safety_reserve_released"])
        self.assertTrue(result["no_safety_volume_present"])
        self.assertFalse(vm.VM_ACTIVE_MARKER_PATH.exists())

    def test_partial_p0_recovery_keeps_unbound_same_name_safety_volume_for_manual_adjudication(self) -> None:
        self._write_failed_p0_marker()
        safety_uuid = "33333333-3333-3333-3333-333333333333"
        populated = _observation(
            safety_volume_uuid=safety_uuid, safety_used_bytes=vm.MIB)
        receipt_path = Path(self.temp.name) / "unbound-p0-recovery.json"
        with mock.patch.object(policy, "_acquire_storage_allocation_lock",
                               return_value=mock.MagicMock()), \
                mock.patch.object(vm, "capture_host_observation", return_value=populated), \
                mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "UUID and empty-accounting baseline captured"):
                vm.recover_partial_vm_volume_creation(receipt_path=receipt_path)
        self.assertTrue(vm.VM_ACTIVE_MARKER_PATH.exists())
        run.assert_not_called()

    def test_partial_p0_recovery_blocks_active_sessions_and_mounted_safety_volume(self) -> None:
        self._write_failed_p0_marker(state="ACTIVE")
        with mock.patch.object(policy, "_acquire_storage_allocation_lock",
                               return_value=mock.MagicMock()):
            with self.assertRaisesRegex(RuntimeError, "failed pre-VM marker"):
                vm.recover_partial_vm_volume_creation(
                    receipt_path=Path(self.temp.name) / "active-p0.json")
        self.assertTrue(vm.VM_ACTIVE_MARKER_PATH.exists())
        safety_uuid = "33333333-3333-3333-3333-333333333333"
        vm.update_active_session_marker(
            "a" * 32, state="FAILED", phase_id="SAFETY_RESERVE_BOUND",
            safety_volume_uuid=safety_uuid, safety_volume_in_use_bytes=0)
        mounted = _observation(safety_volume_uuid=safety_uuid, safety_mounted=True)
        with mock.patch.object(policy, "_acquire_storage_allocation_lock",
                               return_value=mock.MagicMock()), \
                mock.patch.object(vm, "capture_host_observation", return_value=mounted):
            with self.assertRaisesRegex(RuntimeError, "safety-volume recovery preflight failed"):
                vm.recover_partial_vm_volume_creation(
                    receipt_path=Path(self.temp.name) / "mounted-p0.json")
        self.assertTrue(vm.VM_ACTIVE_MARKER_PATH.exists())

    def test_recovery_release_deletes_exact_reserve_and_records_permanent_failure(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        self._write_session_marker("a" * 32, volume_uuid,
                                   phase_id="P2", state="FAILED")
        stopped_proof = vm._seal({
            "status": "FAILED_STOPPED", "session_id": "a" * 32,
            "volume_uuid": volume_uuid,
            "safety_volume_uuid": "33333333-3333-3333-3333-333333333333",
            "phase_quiescent": True, "vm_state": "STOPPED",
            "all_writable_handles_released": True,
            "serial_and_attachment_handles_released": True,
        }, "vm_phase_monitor_hmac_sha256")
        before = _session_observation(vm_volume_uuid=volume_uuid)
        after = _observation(vm_volume_uuid=volume_uuid, quota_bytes=50 * vm.GIB)
        receipt_path = Path(self.temp.name) / "recovery-release.json"
        command_result = mock.Mock(returncode=0, stdout="deleted", stderr="")
        with mock.patch.object(policy, "_acquire_storage_allocation_lock",
                               return_value=mock.MagicMock()), \
                mock.patch.object(vm, "capture_host_observation",
                                  side_effect=[before, before, after]), \
                mock.patch.object(vm, "_diskutil", return_value="diskutil"), \
                mock.patch.object(vm.subprocess, "run", return_value=command_result) as run:
            result = vm.release_safety_reserve_for_recovery(
                stopped_proof, receipt_path=receipt_path)
        self.assertEqual(result["status"], "FAILED / RECOVERY")
        self.assertTrue(result["safety_reserve_released"])
        self.assertEqual(result["safety_volume_uuid"], "33333333-3333-3333-3333-333333333333")
        self.assertTrue(vm.verify_signed_record(result, "vm_recovery_reserve_release_hmac_sha256"))
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][-1], "/dev/disk3s9")
        self.assertFalse(vm.VM_ACTIVE_MARKER_PATH.exists())
        blocked = vm._session_container_blockers(after, volume_uuid=volume_uuid)
        self.assertTrue(any("active VM capacity contract is unavailable" in item
                            for item in blocked))

    def test_final_p1_capacity_sample_blocks_after_last_write_at_stop_trigger(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        self._write_session_marker("a" * 32, volume_uuid)
        result = vm._check_runtime_phase_sample(
            _session_observation(free_bytes=23 * vm.GIB, vm_volume_uuid=volume_uuid),
            volume_uuid=volume_uuid,
            initial_quota_used_bytes=0,
            maximum_phase_growth_bytes=26_637_307_067 + vm.VM_P1_METADATA_MAX_BYTES,
            concurrent_growth_bytes=0)
        self.assertFalse(result["admitted"])
        self.assertTrue(any("8 GiB" in item for item in result["blockers"]))

    def test_p5_runtime_sample_blocks_a_raw_image_over_its_signed_physical_cap(self) -> None:
        volume_uuid = "11111111-1111-1111-1111-111111111111"
        fake_repo = Path(self.temp.name) / "repo"
        receipt_root = fake_repo / "R6-VM-P5-COMPONENTS"
        mount_path = Path(self.temp.name) / "vm-volume"
        receipt_root.mkdir(parents=True)
        mount_path.mkdir()
        image_path = mount_path / "workspace.raw"
        image_path.write_bytes(b"X" * (1024 * 1024))
        image_info = image_path.stat()
        physical = image_info.st_blocks * 512
        self.assertGreater(physical, 0)
        receipt_path = receipt_root / "session-workspace-raw.json"
        receipt = {
            "status": "PASS",
            "phase_id": "P5",
            "session_id": "a" * 32,
            "volume_uuid": volume_uuid,
            "container_uuid": vm.EXPECTED_CONTAINER_UUID,
            "role": "workspace_raw",
            "basename": "workspace.raw",
            "logical_capacity_bytes": image_info.st_size,
            "maximum_physical_allocation_bytes": physical - 1,
            "file_identity": {"device": image_info.st_dev, "inode": image_info.st_ino},
        }
        vm._write_signed_json(receipt_path, receipt, "vm_component_hmac_sha256")
        with mock.patch.object(policy, "REPO", fake_repo), \
                mock.patch.object(vm, "_volume_mount_info",
                                  return_value={"mounted": True, "mount_point": str(mount_path)}):
            result = vm._sample_tracked_raw_images(
                observation=_observation(vm_volume_uuid=volume_uuid),
                session_id="a" * 32, volume_uuid=volume_uuid,
                receipt_paths=[receipt_path])
        self.assertTrue(any("exceeded its signed per-image cap" in item
                            for item in result["blockers"]))


class DeferredRestoreIdentityRepairTests(unittest.TestCase):
    """Synthetic-only coverage for the deferred restore rename/delete boundary."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="deferred-restore-identity-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "vm-volume"
        self.root.mkdir()

    def _identity(self, data: bytes = b"synthetic authorized restore") -> dict[str, int]:
        path = self.root / "restore.ipsw"
        path.write_bytes(data)
        info = path.stat()
        return vm._deferred_restore_file_identity(info)

    def test_ctime_changes_across_rename_while_open_descriptor_and_object_identity_hold(self) -> None:
        canonical = self.root / "restore.ipsw"
        canonical.write_bytes(b"synthetic authorized restore")
        root_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        restore_fd = os.open(canonical, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        self.addCleanup(os.close, restore_fd)
        before = vm._deferred_restore_file_identity(os.fstat(restore_fd))
        self.assertEqual(vm._deferred_restore_stable_object_identity(os.fstat(restore_fd)),
                         (before["device"], before["inode"], stat.S_IFREG))
        quarantine_name = vm._DEFERRED_RESTORE_QUARANTINE_PREFIX + "synthetic"
        quarantine = self.root / quarantine_name

        os.rename(canonical.name, quarantine_name,
                  src_dir_fd=root_fd, dst_dir_fd=root_fd)
        after_fd = vm._deferred_restore_file_identity(os.fstat(restore_fd))
        after_path = vm._deferred_restore_file_identity_at(root_fd, quarantine_name)

        self.assertEqual((before["device"], before["inode"]),
                         (after_fd["device"], after_fd["inode"]))
        self.assertNotEqual(before["ctime_ns"], after_fd["ctime_ns"])
        vm._assert_deferred_restore_same_object(before, after_fd, allow_ctime_change=True)
        vm._assert_deferred_restore_same_object(after_fd, after_path)
        with self.assertRaisesRegex(RuntimeError, "ctime changed"):
            vm._assert_deferred_restore_same_object(before, after_fd)

        canonical.write_bytes(b"substituted canonical file")
        replacement = vm._deferred_restore_file_identity_at(root_fd, canonical.name)
        descriptor_after_substitution = vm._deferred_restore_file_identity(os.fstat(restore_fd))
        self.assertEqual((before["device"], before["inode"]),
                         (descriptor_after_substitution["device"],
                          descriptor_after_substitution["inode"]))
        with self.assertRaisesRegex(RuntimeError, "object identity changed"):
            vm._assert_deferred_restore_same_object(before, replacement,
                                                    allow_ctime_change=True)
        self.assertTrue(quarantine.exists())

    def test_device_inode_and_every_stability_field_are_checked_independently(self) -> None:
        baseline = {
            "device": 11, "inode": 12, "logical_bytes": 13, "allocated_bytes": 14,
            "mtime_ns": 15, "ctime_ns": 16, "owner_uid": 17, "owner_gid": 18,
            "mode": 0o600, "link_count": 1,
        }
        for field in ("device", "inode", *vm._DEFERRED_RESTORE_STABILITY_FIELDS):
            with self.subTest(field=field):
                mutated = {**baseline, field: baseline[field] + 1}
                with self.assertRaises(RuntimeError):
                    vm._assert_deferred_restore_same_object(
                        baseline, mutated, allow_ctime_change=True)
        ctime_only = {**baseline, "ctime_ns": baseline["ctime_ns"] + 1}
        vm._assert_deferred_restore_same_object(
            baseline, ctime_only, allow_ctime_change=True)
        with self.assertRaisesRegex(RuntimeError, "ctime changed"):
            vm._assert_deferred_restore_same_object(baseline, ctime_only)

    def test_nonregular_descriptor_and_nofollow_path_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "not a regular file"):
            vm._deferred_restore_stable_object_identity(self.root.stat())
        root_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        canonical = self.root / "restore.ipsw"
        canonical.write_bytes(b"expected regular file")
        original = vm._deferred_restore_file_identity_at(root_fd, canonical.name)
        canonical.unlink()
        canonical.symlink_to(self.root)
        with self.assertRaisesRegex(RuntimeError, "single-link regular file"):
            vm._deferred_restore_file_identity_at(root_fd, canonical.name)
        self.assertEqual(original["logical_bytes"], len(b"expected regular file"))

    def test_namespace_rejects_multiple_quarantines_and_canonical_quarantine_ambiguity(self) -> None:
        canonical = self.root / "restore.ipsw"
        first = self.root / (vm._DEFERRED_RESTORE_QUARANTINE_PREFIX + "one")
        second = self.root / (vm._DEFERRED_RESTORE_QUARANTINE_PREFIX + "two")
        canonical.write_bytes(b"canonical")
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        root_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        self.assertEqual(vm._deferred_restore_namespace_candidates(root_fd),
                         sorted([canonical.name, first.name, second.name]))
        with self.assertRaisesRegex(RuntimeError, "namespace is ambiguous"):
            vm._assert_deferred_restore_namespace(root_fd, [first.name])
        canonical.unlink()
        with self.assertRaisesRegex(RuntimeError, "namespace is ambiguous"):
            vm._assert_deferred_restore_namespace(root_fd, [first.name])

    def test_namespace_rejects_one_canonical_and_one_quarantine_candidate(self) -> None:
        canonical = self.root / "restore.ipsw"
        quarantine = self.root / (vm._DEFERRED_RESTORE_QUARANTINE_PREFIX + "single")
        canonical.write_bytes(b"canonical")
        quarantine.write_bytes(b"quarantine")
        root_fd = os.open(self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        with self.assertRaisesRegex(RuntimeError, "namespace is ambiguous"):
            vm._assert_deferred_restore_namespace(root_fd, [quarantine.name])

    def _perform_synthetic_retirement_failure(self, *, substitution: bool) -> tuple[
            Path, Path, Path, str, str]:
        repo = Path(self.temp.name) / "repo"
        repo.mkdir()
        root = repo / "vm-volume"
        root.mkdir()
        restore_path = root / "restore.ipsw"
        restore_bytes = b"synthetic restore payload only"
        restore_path.write_bytes(restore_bytes)
        expected_sha = hashlib.sha256(restore_bytes).hexdigest()
        identity = vm._deferred_restore_file_identity(restore_path.stat())
        root_stat = root.stat()
        root_identity = {
            "device": int(root_stat.st_dev), "inode": int(root_stat.st_ino),
            "owner_uid": int(root_stat.st_uid), "mode": int(stat.S_IMODE(root_stat.st_mode)),
        }
        marker = {"session_id": "a" * 32}
        marker_sha = "1" * 64
        branch_sha = "2" * 64
        p1_admission_sha = "3" * 64
        p1_verification_sha = "4" * 64
        branch = {
            "status": "PASS",
            "decision": "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM",
        }
        p1_verification = {
            "status": "PASS", "phase_id": "P1",
            "restore_path": str(restore_path),
            "local_sha256_state": "VERIFIED", "local_sha256": expected_sha,
            "expected_length_bytes": len(restore_bytes),
            "actual_length_bytes": len(restore_bytes),
            "file_identity": {
                key: identity[key] for key in (
                    "device", "inode", "logical_bytes", "allocated_bytes",
                    "mtime_ns", "ctime_ns")
            },
        }
        allocation = {
            "p1_historical_allocated_bytes": identity["allocated_bytes"],
            "current_allocated_bytes": identity["allocated_bytes"],
            "current_minus_p1_bytes": 0,
            "method": "st_blocks * 512; filesystem-dependent time-specific allocation measurement",
        }
        action = {
            "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT_AUTHORIZATION",
            "status": "AUTHORIZED",
            "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
            "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
            "authorization_scope_state": "LOCAL_RESTORE_PRESENT_AWAITING_DELETE",
            "local_restore_payload_retired": False,
            "restore_path": str(restore_path),
            "restore_sha256": expected_sha,
            "restore_logical_bytes": len(restore_bytes),
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "vm_guest_base_sealed": False,
            "p2_cleanup_success_claimed": False,
            "action_id": "synthetic",
            "created_at_unix": 1.0,
            "branch_retirement_receipt_path": "branch-retirement.json",
            "branch_retirement_receipt_sha256": branch_sha,
            "active_marker_sha256": marker_sha,
            "session_id": marker["session_id"],
            "p1_admission_sha256": p1_admission_sha,
            "p1_verification_sha256": p1_verification_sha,
            "restore_file_identity": identity,
            "vm_volume_root_identity": root_identity,
            "restore_allocation_measurement": allocation,
            "synthetic_authorization_secret": "r12-test-secret-must-never-be-logged",
        }
        authorization_path = repo / "authorization.json"
        review_path = repo / "review.json"
        final_path = repo / "R6-VM-DEFERRED-RESTORE-RETIREMENT-synthetic.json"
        pending_path = repo / "R6-VM-DEFERRED-RESTORE-RETIREMENT-PENDING-synthetic.json"
        review = {"status": "PASS", "created_at_unix": 2.0}
        process_calls = 0
        action_sha = "5" * 64

        def signed_snapshot(path: Path, _signature: str) -> tuple[Any, bytes, str, tuple[int, ...]]:
            if Path(path) == authorization_path:
                return action, b"", action_sha, (1, 2, 3, 4, 5)
            return branch, b"", branch_sha, (1, 2, 3, 4, 5)

        def process_scan() -> dict[str, Any]:
            nonlocal process_calls
            process_calls += 1
            if not substitution and process_calls == 3:
                return {"blocking_processes": [{"pid": 99}]}
            return {"blocking_processes": []}

        def descriptor_scan(_paths: list[Path]) -> dict[str, Any]:
            return {
                "descriptor_identity_mismatches": [],
                "exact_target_descriptor_matches": [{"pid": os.getpid()}],
                "open_file_descriptors": [{"files": [{"descriptor": 5}]}],
                "open_process_ids": [str(os.getpid())],
            }

        def write_pending(path: Path, record: dict[str, Any], _signature: str) -> dict[str, Any]:
            path.write_text(json.dumps(record), encoding="utf-8")
            return dict(record)

        def rename_synthetic(source_fd: int, source_name: str,
                             destination_fd: int, destination_name: str) -> None:
            try:
                os.stat(destination_name, dir_fd=destination_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(destination_name)
            os.rename(source_name, destination_name,
                      src_dir_fd=source_fd, dst_dir_fd=destination_fd)
            if substitution and source_name == "restore.ipsw":
                os.unlink(destination_name, dir_fd=destination_fd)
                substitute_fd = os.open(
                    destination_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600, dir_fd=destination_fd)
                try:
                    os.write(substitute_fd, b"replacement payload")
                finally:
                    os.close(substitute_fd)

        import p2_base_orchestrator as p2

        inactivate = mock.Mock()
        patchers = [
            mock.patch.object(policy, "REPO", repo),
            mock.patch.object(vm, "VM_DEFERRED_RESTORE_PATH", restore_path),
            mock.patch.object(vm, "VM_DEFERRED_RESTORE_SHA256", expected_sha),
            mock.patch.object(vm, "VM_DEFERRED_RESTORE_LOGICAL_BYTES", len(restore_bytes)),
            mock.patch.object(policy, "_acquire_storage_allocation_lock", return_value=nullcontext()),
            mock.patch.object(vm, "_read_signed_record_snapshot", side_effect=signed_snapshot),
            mock.patch.object(vm, "_deferred_read_repo_signed",
                              return_value=(branch, repo / "branch-retirement.json", branch_sha)),
            mock.patch.object(vm, "_deferred_fresh_review_json", return_value=(review, "6" * 64)),
            mock.patch.object(vm, "_require_deferred_review_postdates"),
            mock.patch.object(vm, "_deferred_read_exact_active_marker",
                              return_value=(marker, marker_sha)),
            mock.patch.object(vm, "_deferred_p1_evidence",
                              return_value=({}, p1_admission_sha,
                                            p1_verification, p1_verification_sha)),
            mock.patch.object(vm, "_assert_deferred_restore_authorization_history"),
            mock.patch.object(vm, "capture_host_observation", return_value={}),
            mock.patch.object(vm, "_deferred_storage_summary",
                              return_value={"conservative_shared_free_bytes": 123}),
            mock.patch.object(vm, "_deferred_restore_tree_evidence",
                              return_value={"blockers": [], "partial_paths": [],
                                            "restore_paths": []}),
            mock.patch.object(vm, "_deferred_process_scan", side_effect=process_scan),
            mock.patch.object(vm, "_volume_mount_info", return_value={}),
            mock.patch.object(vm, "_verify_open_mount_fd"),
            mock.patch.object(vm, "_volume_row", return_value={"CapacityInUse": 456}),
            mock.patch.object(vm, "_write_signed_json_unique", side_effect=write_pending),
            mock.patch.object(p2, "_acquire_p2_cleanup_lock",
                              side_effect=lambda: (os.open(root, os.O_RDONLY), {})),
            mock.patch.object(p2, "_descriptor_scan_evidence", side_effect=descriptor_scan),
            mock.patch.object(p2, "_renameatx_exclusive", side_effect=rename_synthetic),
            mock.patch.object(vm, "_create_deferred_capacity_inactivation_locked",
                              new=inactivate),
        ]
        entered = []
        try:
            for patcher in patchers:
                entered.append(patcher)
                patcher.start()
            error_text = ""
            try:
                vm.perform_deferred_vm_restore_retirement(
                    authorization_path, review_path,
                    retirement_receipt_path=final_path,
                    pending_receipt_path=pending_path)
            except RuntimeError as error:
                error_text = str(error)
            else:
                self.fail("synthetic pre-unlink failure unexpectedly completed")
            self.assertTrue(pending_path.exists(), error_text)
            pending = json.loads(pending_path.read_text(encoding="utf-8"))
            self.assertEqual(pending["status"], "PENDING")
            self.assertEqual(pending["record_type"],
                             "R6_VM_DEFERRED_RESTORE_RETIREMENT_PENDING")
            self.assertFalse(final_path.exists())
            inactivate.assert_not_called()
            self.assertNotIn(str(restore_path), error_text)
            self.assertNotIn(expected_sha, error_text)
            self.assertNotIn(action["synthetic_authorization_secret"], error_text)
            return root, restore_path, pending_path, expected_sha, error_text
        finally:
            for patcher in reversed(entered):
                patcher.stop()

    def test_preunlink_error_rolls_back_same_object_across_ctime_delta_and_leaves_pending(self) -> None:
        root, restore_path, pending_path, _expected_sha, error_text = (
            self._perform_synthetic_retirement_failure(substitution=False))
        self.assertIn("appeared before restore unlink", error_text)
        self.assertTrue(restore_path.is_file())
        self.assertEqual(restore_path.read_bytes(), b"synthetic restore payload only")
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            self.assertEqual(vm._deferred_restore_namespace_candidates(root_fd), ["restore.ipsw"])
            pending_identity = json.loads(
                pending_path.read_text(encoding="utf-8"))["restore_file_identity"]
            restored_identity = vm._deferred_restore_file_identity_at(root_fd, "restore.ipsw")
            self.assertNotEqual(pending_identity["ctime_ns"], restored_identity["ctime_ns"])
            vm._assert_deferred_restore_same_object(
                pending_identity, restored_identity, allow_ctime_change=True)
        finally:
            os.close(root_fd)
        self.assertEqual(json.loads(pending_path.read_text(encoding="utf-8"))["status"], "PENDING")

    def test_quarantine_path_substitution_stops_without_unlink_or_success_receipt(self) -> None:
        root, restore_path, pending_path, _expected_sha, error_text = (
            self._perform_synthetic_retirement_failure(substitution=True))
        self.assertIn("deferred restore", error_text)
        self.assertIn("changed", error_text)
        self.assertFalse(restore_path.exists())
        candidates = sorted(root.glob(vm._DEFERRED_RESTORE_QUARANTINE_PREFIX + "*"))
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].read_bytes(), b"replacement payload")
        self.assertEqual(json.loads(pending_path.read_text(encoding="utf-8"))["status"], "PENDING")

    def test_pending_restore_result_cannot_inactivate_capacity_commitment(self) -> None:
        branch = {"status": "PASS",
                  "decision": "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM"}
        pending_restore = {
            "status": "PENDING", "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
        }
        with mock.patch.object(vm, "_read_signed_record_snapshot",
                               side_effect=[(branch, Path("branch.json"), "1" * 64, ()),
                                            (pending_restore, Path("pending.json"),
                                             "2" * 64, ()),
                                            ({"status": "PASS"}, Path("inventory.json"),
                                             "3" * 64, ())]), \
                mock.patch.object(vm, "_write_signed_json_unique") as write:
            with self.assertRaisesRegex(RuntimeError, "capacity inactivation requires exact"):
                vm._create_deferred_capacity_inactivation_locked(
                    Path("branch.json"), Path("pending.json"),
                    Path("commitment-review.json"), Path("inventory.json"),
                    inactivation_path=Path(self.temp.name) / "inactivation.json")
        write.assert_not_called()


class HistoricalG0CAncestryCompatibilityTests(unittest.TestCase):
    """Exact-pair compatibility tests; every artifact is isolated under /private/tmp."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p1-historical-g0c-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        self.marker_path = self.repo / "active-session.json"
        self.issuance_controller = {
            "vm_capacity_controller_sha256": "1" * 64,
            "storage_policy_sha256": "2" * 64,
            "restore_metadata_probe_sha256": "3" * 64,
        }
        self.current_controller = {
            "vm_capacity_controller_sha256": "a" * 64,
            "storage_policy_sha256": "b" * 64,
            "restore_metadata_probe_sha256": "c" * 64,
        }
        self.receipt_sha256 = "0" * 64
        self.completion_review_sha256 = "0" * 64
        self.current_test_bytes = b"synthetic current controller tests B\n"
        self.issuance_snapshot_path = self.repo / "experiments/storage/issuance-r4-snapshot.json"
        self.issuance_review_path = self.repo / "experiments/storage/issuance-r4-review.json"
        self.current_snapshot_path = self.repo / "experiments/storage/current-snapshot.json"
        self.current_review_path = self.repo / "experiments/storage/current-review.json"
        self.transition_path = self.repo / "R6-VM-HISTORICAL-ANCESTRY-COMPATIBILITY-TRANSITION-test-a.json"
        self.patchers = [
            mock.patch.object(policy, "REPO", self.repo),
            mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", self.repo / ".signing-key"),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ROOT", self.repo / "vm-state"),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ACTIVE_MARKER", self.marker_path),
            mock.patch.object(vm, "VM_ACTIVE_MARKER_PATH", self.marker_path),
            mock.patch.object(vm, "G0C_SNAPSHOT_PATH", self.current_snapshot_path),
            mock.patch.object(vm, "G0C_REVIEW_PATH", self.current_review_path),
            mock.patch.object(vm, "VM_P1_G0C_ANCESTRY_TRANSITION_PATH", self.transition_path),
            mock.patch.object(vm, "VM_P1_G0C_ANCESTRY_TRANSITION_PREDECESSOR_PATH", None),
        ]
        for name in (
                "VM_P1_ACCEPTED_ADMISSION_PATH", "VM_P1_ACCEPTED_REVIEW_PATH",
                "VM_P1_R2_G0C_SNAPSHOT_PATH", "VM_P1_R2_G0C_REVIEW_PATH",
                "VM_P1_G0C_ANCESTRY_RECEIPT_PATH", "VM_P1_G0C_ANCESTRY_REVIEW_PATH"):
            old = getattr(vm, name)
            relative = old.relative_to(Path(__file__).resolve().parents[2])
            self.patchers.append(mock.patch.object(vm, name, self.repo / relative))
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        policy._manifest_signing_key(create=True)
        self.real_repo = Path(__file__).resolve().parents[2]
        self.real_historical_paths = [
            self.real_repo / "R6-VM-INTERNAL-PROVISIONING-ADMISSION-P1-20261017T152047Z-fc8ad609.json",
            self.real_repo / "R6-VM-INTERNAL-PROVISIONING-ADMISSION-P1-REVIEW-20261017T152047Z-fc8ad609.json",
            self.real_repo / "experiments/storage/G0C-INTERNAL-VM-CONTROLLER-SNAPSHOT-METADATA-P1-TRANSITION-R2-20261017.json",
            self.real_repo / "experiments/storage/g0c-internal-vm-controller-independent-review-metadata-p1-transition-r2-20261017.json",
        ]
        self.real_historical_bytes = [path.read_bytes() for path in self.real_historical_paths]
        self.snapshot, self.review, self.identity, self.test_binding = self._write_pair(legacy=True)
        self.controller_identity = dict(vm.VM_P1_R2_CONTROLLER_IDENTITY)
        self.p1_path = vm.VM_P1_ACCEPTED_ADMISSION_PATH
        self.p1_review_path = vm.VM_P1_ACCEPTED_REVIEW_PATH
        self._write_issuance_g0c()
        self._write_current_g0c()
        for name, value in (
                ("VM_P1_G0C_ANCESTRY_ISSUANCE_CONTROLLER_IDENTITY", self.issuance_controller),
                ("VM_P1_G0C_ANCESTRY_ISSUANCE_G0C_IDENTITY", self.issuance_g0c),
                ("VM_P1_G0C_ANCESTRY_ISSUANCE_TEST_BINDING", self.issuance_test_binding),
                ("VM_P1_G0C_ANCESTRY_RECEIPT_SHA256", "0" * 64),
                ("VM_P1_G0C_ANCESTRY_REVIEW_SHA256", "0" * 64)):
            patcher = mock.patch.object(vm, name, value)
            patcher.start()
            self.patchers.append(patcher)
            self.addCleanup(patcher.stop)
        self._write_p1()

    @staticmethod
    def _write_private_json(path: Path, value: object) -> bytes:
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = policy._canonical_pretty_json_bytes(value)
        path.write_bytes(raw)
        path.chmod(0o600)
        return raw

    def _write_issuance_g0c(self) -> None:
        test_path = "experiments/storage/test_vm_capacity_domain.py"
        self.issuance_test_binding = {
            "path": test_path,
            "sha256": hashlib.sha256(b"synthetic issuance-time tests\n").hexdigest(),
            "size_bytes": len(b"synthetic issuance-time tests\n"),
        }
        paths = {
            "build_retention_control": "experiments/storage/build_retention.py",
            "build_retention_tests": "experiments/storage/test_build_retention.py",
            "gate0_checks_tests": "experiments/storage/test_g0c_checks.py",
            "gate0_control": "experiments/storage/storage_gate0.py",
            "prefix_preflight_control": "experiments/storage/prefix_preflight.py",
            "prefixlease_contract_tests": "experiments/storage/test_prefixlease_wineprefix_contract.py",
            "source": "experiments/storage/storage_policy.py",
            "storage_policy_tests": "experiments/storage/test_storage_policy.py",
            "tests": "experiments/storage/test_wine_launcher_policy.py",
            "vm_capacity_controller": "experiments/storage/vm_capacity_domain.py",
            "vm_capacity_tests": test_path,
            "vm_restore_metadata_probe": "experiments/storage/vm_restore_metadata_probe.swift",
        }
        controller_hashes = {
            "vm_capacity_controller": self.issuance_controller["vm_capacity_controller_sha256"],
            "source": self.issuance_controller["storage_policy_sha256"],
            "vm_restore_metadata_probe": self.issuance_controller["restore_metadata_probe_sha256"],
        }
        reviewed: dict[str, object] = {}
        for index, member in enumerate(sorted(vm.VM_P1_R2_REVIEWED_FILE_KEYS)):
            if member == "vm_capacity_tests":
                reviewed[member] = dict(self.issuance_test_binding)
            else:
                reviewed[member] = {
                    "path": paths[member],
                    "sha256": controller_hashes.get(member, f"{index + 1:064x}"[-64:]),
                    "size_bytes": 1000 + index,
                }
        snapshot = {
            "created_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 60)),
            "direct_full_suite_diagnostic": {},
            "focused_vm_policy_suite_evidence": {},
            "full_suite_evidence": {},
            "historical_ancestry_focused_suite_evidence": {},
            "isolation_note": "synthetic isolated fixture",
            "p1_freshness_renewal_source_review": {},
            "p1_renewal_suite_evidence": {},
            "prior_p1_renewal_review": {},
            "review_contract": {},
            "reviewed_controller_and_test_files": reviewed,
            "schema_version": 1,
            "separately_bound_files": {},
            "snapshot_id": "G0C-INTERNAL-VM-CONTROLLER",
        }
        snapshot_raw = self._write_private_json(self.issuance_snapshot_path, snapshot)
        snapshot_sha256 = hashlib.sha256(snapshot_raw).hexdigest()
        review = {
            "findings": [],
            "model_identity": "GPT-6 Luna Max",
            "record_type": "R6_VM_P1_HISTORICAL_ANCESTRY_FRESH_G0C_REVIEW",
            "reviewed_files": dict(reviewed),
            "reviewer_identity": "GPT-6 Luna Max G0C Storage/Gate Controller Reviewer",
            "reviewer_status": "PASS",
            "schema_version": 1,
            "snapshot_path": self.issuance_snapshot_path.relative_to(self.repo).as_posix(),
            "snapshot_sha256": snapshot_sha256,
            "status": "PASS",
        }
        review_raw = self._write_private_json(self.issuance_review_path, review)
        self.issuance_g0c = {
            "model_identity": "GPT-6 Luna Max",
            "review_path": self.issuance_review_path.relative_to(self.repo).as_posix(),
            "review_sha256": hashlib.sha256(review_raw).hexdigest(),
            "reviewed_file_count": len(reviewed),
            "reviewer_identity": "GPT-6 Luna Max G0C Storage/Gate Controller Reviewer",
            "snapshot_path": self.issuance_snapshot_path.relative_to(self.repo).as_posix(),
            "snapshot_sha256": snapshot_sha256,
        }

    def _write_current_g0c(self, *, controller: dict[str, object] | None = None,
                           test_bytes: bytes | None = None) -> None:
        if controller is not None:
            self.current_controller = dict(controller)
        if test_bytes is not None:
            self.current_test_bytes = test_bytes
        test_path = self.repo / "experiments/storage/test_vm_capacity_domain.py"
        test_path.parent.mkdir(parents=True, exist_ok=True)
        test_path.write_bytes(self.current_test_bytes)
        test_path.chmod(0o600)
        paths = {
            "vm_capacity_controller": "experiments/storage/vm_capacity_domain.py",
            "source": "experiments/storage/storage_policy.py",
            "vm_restore_metadata_probe": "experiments/storage/vm_restore_metadata_probe.swift",
        }
        fields = {
            "vm_capacity_controller": "vm_capacity_controller_sha256",
            "source": "storage_policy_sha256",
            "vm_restore_metadata_probe": "restore_metadata_probe_sha256",
        }
        reviewed: dict[str, object] = {}
        for index, member in enumerate(paths):
            reviewed[member] = {
                "path": paths[member],
                "sha256": self.current_controller[fields[member]],
                "size_bytes": 2000 + index,
            }
        self.current_test_binding = {
            "path": "experiments/storage/test_vm_capacity_domain.py",
            "sha256": hashlib.sha256(self.current_test_bytes).hexdigest(),
            "size_bytes": len(self.current_test_bytes),
        }
        reviewed["vm_capacity_tests"] = dict(self.current_test_binding)
        snapshot = {
            "schema_version": 1,
            "snapshot_id": "G0C-INTERNAL-VM-CONTROLLER",
            "reviewed_controller_and_test_files": reviewed,
        }
        snapshot_raw = self._write_private_json(vm.G0C_SNAPSHOT_PATH, snapshot)
        snapshot_sha256 = hashlib.sha256(snapshot_raw).hexdigest()
        review = {
            "status": "PASS",
            "reviewer_status": "PASS",
            "findings": [],
            "reviewer_identity": "GPT-6 Luna Max current G0C reviewer",
            "model_identity": "GPT-6 Luna Max",
            "snapshot_path": vm.G0C_SNAPSHOT_PATH.relative_to(self.repo).as_posix(),
            "snapshot_sha256": snapshot_sha256,
            "reviewed_files": dict(reviewed),
        }
        review_raw = self._write_private_json(vm.G0C_REVIEW_PATH, review)
        self.current_controller_files = {
            key: dict(reviewed[key])
            for key in ("vm_capacity_controller", "source", "vm_restore_metadata_probe")}
        self.current_g0c = {
            "snapshot_path": vm.G0C_SNAPSHOT_PATH.relative_to(self.repo).as_posix(),
            "snapshot_sha256": snapshot_sha256,
            "review_path": vm.G0C_REVIEW_PATH.relative_to(self.repo).as_posix(),
            "review_sha256": hashlib.sha256(review_raw).hexdigest(),
            "reviewer_identity": review["reviewer_identity"],
            "model_identity": review["model_identity"],
            "reviewed_file_count": len(reviewed),
        }

    def _write_transition(self, receipt: dict[str, object], receipt_sha256: str,
                          review_sha256: str) -> None:
        self.assertEqual(receipt_sha256, vm.VM_P1_G0C_ANCESTRY_RECEIPT_SHA256)
        self.assertEqual(review_sha256, vm.VM_P1_G0C_ANCESTRY_REVIEW_SHA256)
        with self._runtime_pins(), \
                mock.patch.object(vm, "_controller_identity", return_value=self.current_controller), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=self.current_g0c):
            vm.create_historical_g0c_ancestry_compatibility_transition()

    def _write_pair(self, *, legacy: bool, test_binding: dict[str, object] | None = None,
                    missing_review_fields: tuple[str, ...] = ()) -> tuple[
                        dict[str, object], dict[str, object], dict[str, object], dict[str, object]]:
        snapshot_path = vm.VM_P1_R2_G0C_SNAPSHOT_PATH if legacy else (
            self.repo / "experiments/storage/nonlegacy-snapshot.json")
        review_path = vm.VM_P1_R2_G0C_REVIEW_PATH if legacy else (
            self.repo / "experiments/storage/nonlegacy-review.json")
        test = dict(test_binding or vm.VM_P1_R2_TEST_BINDING)
        paths = {
            "build_retention_control": "experiments/storage/build_retention.py",
            "build_retention_tests": "experiments/storage/test_build_retention.py",
            "gate0_checks_tests": "experiments/storage/test_g0c_checks.py",
            "gate0_control": "experiments/storage/storage_gate0.py",
            "prefix_preflight_control": "experiments/storage/prefix_preflight.py",
            "prefixlease_contract_tests": "experiments/storage/test_prefixlease_wineprefix_contract.py",
            "source": "experiments/storage/storage_policy.py",
            "storage_policy_tests": "experiments/storage/test_storage_policy.py",
            "tests": "experiments/storage/test_wine_launcher_policy.py",
            "vm_capacity_controller": "experiments/storage/vm_capacity_domain.py",
            "vm_capacity_tests": str(test["path"]),
            "vm_restore_metadata_probe": "experiments/storage/vm_restore_metadata_probe.swift",
        }
        reviewed: dict[str, object] = {}
        for index, member in enumerate(sorted(vm.VM_P1_R2_REVIEWED_FILE_KEYS)):
            if member == "vm_capacity_tests":
                reviewed[member] = dict(test)
            else:
                reviewed[member] = {
                    "path": paths[member],
                    "sha256": vm.VM_P1_R2_CONTROLLER_IDENTITY.get(
                        {"vm_capacity_controller": "vm_capacity_controller_sha256",
                         "source": "storage_policy_sha256",
                         "vm_restore_metadata_probe": "restore_metadata_probe_sha256"}.get(member, ""),
                        f"{index + 1:064x}"[-64:]),
                    "size_bytes": 1000 + index,
                }
        snapshot: dict[str, object] = {
            "created_at_utc": "2026-10-17T15:10:08Z",
            "focused_vm_policy_suite_evidence": {"result": "PASS"},
            "full_suite_evidence": {"result": "PASS"},
            "metadata_p1_transition_source_review": {"status": "PASS"},
            "review_contract": {
                "path": "experiments/storage/storage_gate0.py",
                "required_fields": {key: paths[key].split("experiments/storage/", 1)[-1]
                                    for key in sorted(paths)},
                "sha256": "f" * 64,
                "size_bytes": 100,
            },
            "reviewed_controller_and_test_files": reviewed,
            "schema_version": 1,
            "separately_bound_files": {"storage_smoke_runner": {"path": "experiments/storage/smoke_prefix_lease.py"}},
            "snapshot_id": "G0C-INTERNAL-VM-CONTROLLER",
        }
        snapshot_raw = self._write_private_json(snapshot_path, snapshot)
        snapshot_sha = hashlib.sha256(snapshot_raw).hexdigest()
        review: dict[str, object] = {
            "findings": [],
            "model_identity": "GPT-6 Luna Max",
            "reviewed_files": dict(reviewed),
            "reviewer_identity": vm.VM_P1_R2_G0C_REVIEWER_IDENTITY,
            "reviewer_status": "PASS",
            "snapshot_path": snapshot_path.relative_to(self.repo).as_posix(),
            "snapshot_sha256": snapshot_sha,
            "status": "PASS",
        }
        if not legacy:
            review["source_identity"] = {"controller_test": dict(test)}
        for field in missing_review_fields:
            review.pop(field, None)
        review_raw = self._write_private_json(review_path, review)
        identity = {
            "snapshot_path": snapshot_path.relative_to(self.repo).as_posix(),
            "snapshot_sha256": snapshot_sha,
            "review_path": review_path.relative_to(self.repo).as_posix(),
            "review_sha256": hashlib.sha256(review_raw).hexdigest(),
            "reviewer_identity": vm.VM_P1_R2_G0C_REVIEWER_IDENTITY,
            "model_identity": "GPT-6 Luna Max",
            "reviewed_file_count": len(reviewed),
        }
        if legacy:
            with mock.patch.object(vm, "VM_P1_R2_G0C_SNAPSHOT_SHA256", snapshot_sha), \
                    mock.patch.object(vm, "VM_P1_R2_G0C_REVIEW_SHA256", identity["review_sha256"]):
                self.legacy_hashes = (snapshot_sha, identity["review_sha256"])
        return snapshot, review, identity, test

    def _write_p1(self) -> None:
        p1_relative = self.p1_path.relative_to(self.repo).as_posix()
        review_relative = self.p1_review_path.relative_to(self.repo).as_posix()
        admission = {
            "schema_version": 1,
            "record_type": "R6_VM_P1_RESTORE_DOWNLOAD_ADMISSION",
            "receipt_path": p1_relative,
            "decision": "PASS",
            "authorization": "DOWNLOAD_AND_VERIFY_RESTORE_ONLY",
            "authorized_phase": "P1",
            "p1_authorized": True,
            "restore_download_authorized": True,
            "p2_authorized": False,
            "vm_start_authorized": False,
            "created_at_unix": time.time() - 2,
            "required_independent_review_path": review_relative,
            "controller_identity": self.controller_identity,
            "g0c_identity": self.identity,
        }
        sealed = vm._write_signed_json_unique(
            self.p1_path, admission, "vm_admission_hmac_sha256")
        self.p1_sha256 = hashlib.sha256(
            policy._canonical_pretty_json_bytes(sealed)).hexdigest()
        p1_review = {
            "status": "PASS",
            "reviewer_identity": "GPT-6 Luna Max independent reviewer for R6 VM P1 admission, 2026-10-17",
            "reviewed_artifact_path": p1_relative,
            "reviewed_artifact_sha256": self.p1_sha256,
            "reviewed_at_unix": time.time() - 1,
            "findings": [],
        }
        p1_review_raw = self._write_private_json(self.p1_review_path, p1_review)
        self.p1_review_sha256 = hashlib.sha256(p1_review_raw).hexdigest()
        with mock.patch.object(vm, "VM_P1_ACCEPTED_ADMISSION_SHA256", self.p1_sha256), \
                mock.patch.object(vm, "VM_P1_ACCEPTED_REVIEW_SHA256", self.p1_review_sha256):
            self.pinned_hashes = (self.p1_sha256, self.p1_review_sha256)

    def _runtime_pins(self):
        return mock.patch.multiple(
            vm,
            VM_P1_ACCEPTED_ADMISSION_SHA256=self.p1_sha256,
            VM_P1_ACCEPTED_REVIEW_SHA256=self.p1_review_sha256,
            VM_P1_R2_G0C_SNAPSHOT_SHA256=self.legacy_hashes[0],
            VM_P1_R2_G0C_REVIEW_SHA256=self.legacy_hashes[1],
            VM_P1_G0C_ANCESTRY_RECEIPT_SHA256=self.receipt_sha256,
            VM_P1_G0C_ANCESTRY_REVIEW_SHA256=self.completion_review_sha256,
        )

    def _write_completion(self) -> tuple[dict[str, object], str]:
        p1_path_text = self.p1_path.relative_to(self.repo).as_posix()
        with self._runtime_pins():
            base = vm._legacy_ancestry_receipt_base(
                p1_path_text=p1_path_text, p1_sha256=self.p1_sha256,
                p1_review_sha256=self.p1_review_sha256, identity=self.identity,
                historical_controller_identity=self.controller_identity,
                historical_test_binding=self.test_binding,
                issuance_controller_identity=self.issuance_controller,
                issuance_g0c_identity=self.issuance_g0c,
                created_at_unix=time.time() - 20,
            )
            receipt = vm._write_signed_json_unique(
                vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH, base,
                vm.VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
        _record, _raw, digest, _file_id = vm._read_signed_record_snapshot(
            vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH, vm.VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
        self.receipt_sha256 = digest
        vm.VM_P1_G0C_ANCESTRY_RECEIPT_SHA256 = digest
        self._write_completion_review(digest)
        self._write_transition(receipt, digest, self.completion_review_sha256)
        return receipt, digest

    def _write_completion_review(self, receipt_sha256: str, *, changes: dict[str, object] | None = None) -> None:
        try:
            existing, _raw, existing_sha, _ = vm._read_json_file_snapshot(
                vm.VM_P1_G0C_ANCESTRY_REVIEW_PATH)
            if (changes is None
                    and existing.get("reviewed_artifact_sha256") == receipt_sha256):
                self.completion_review_sha256 = existing_sha
                vm.VM_P1_G0C_ANCESTRY_REVIEW_SHA256 = existing_sha
                return
        except OSError:
            pass
        review = {
            "schema_version": 1,
            "record_type": "R6_VM_P1_HISTORICAL_G0C_ANCESTRY_COMPLETION_REVIEW",
            "scope": vm.VM_P1_G0C_ANCESTRY_REVIEW_SCOPE,
            "status": "PASS",
            "reviewer_identity": vm.VM_P1_G0C_ANCESTRY_REVIEWER_IDENTITY,
            "model_identity": "GPT-6 Luna Max",
            "reviewed_at_unix": time.time(),
            "reviewed_artifact_path": vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH.relative_to(self.repo).as_posix(),
            "reviewed_artifact_sha256": receipt_sha256,
            "findings": [],
        }
        review.update(changes or {})
        raw = self._write_private_json(vm.VM_P1_G0C_ANCESTRY_REVIEW_PATH, review)
        self.completion_review_sha256 = hashlib.sha256(raw).hexdigest()
        vm.VM_P1_G0C_ANCESTRY_REVIEW_SHA256 = self.completion_review_sha256

    def _verify(self, *, current_controller: dict[str, object] | None = None,
                current_g0c: dict[str, object] | None = None) -> None:
        effective_controller = current_controller or self.current_controller
        effective_g0c = current_g0c or self.current_g0c
        with self._runtime_pins(), \
                mock.patch.object(vm, "_controller_identity", return_value=self.current_controller), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=self.current_g0c):
            vm._verify_historical_g0c_identity(
                self.identity, self.controller_identity,
                p1_path_text=self.p1_path.relative_to(self.repo).as_posix(),
                p1_sha256=self.p1_sha256,
                p1_review_sha256=self.p1_review_sha256,
                current_controller_identity=effective_controller,
                current_g0c_identity=effective_g0c)

    def test_01_exact_legacy_omission_passes_with_signed_reviewed_receipt(self) -> None:
        _receipt, digest = self._write_completion()
        self._write_completion_review(digest)
        self._verify()

    def test_02_nonlegacy_review_missing_controller_test_fails(self) -> None:
        _snapshot, _review, identity, _test = self._write_pair(
            legacy=False, missing_review_fields=("source_identity",))
        with self.assertRaisesRegex(RuntimeError, "does not bind its accepted controller tests"):
            vm._verify_historical_g0c_identity(identity, self.controller_identity)

    def test_03_nonlegacy_review_with_correct_controller_test_uses_normal_path(self) -> None:
        _snapshot, _review, identity, _test = self._write_pair(legacy=False)
        vm._verify_historical_g0c_identity(identity, self.controller_identity)

    def test_04_nonlegacy_review_with_wrong_controller_test_fails(self) -> None:
        _snapshot, review, identity, _test = self._write_pair(legacy=False)
        wrong = dict(vm.VM_P1_R2_TEST_BINDING, sha256="0" * 64)
        review["source_identity"] = {"controller_test": wrong}
        review_raw = self._write_private_json(
            self.repo / identity["review_path"], review)
        identity["review_sha256"] = hashlib.sha256(review_raw).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "does not bind its accepted controller tests"):
            vm._verify_historical_g0c_identity(identity, self.controller_identity)

    def test_05_snapshot_missing_vm_test_binding_fails(self) -> None:
        snapshot = dict(self.snapshot)
        reviewed = dict(snapshot["reviewed_controller_and_test_files"])
        reviewed.pop("vm_capacity_tests")
        snapshot["reviewed_controller_and_test_files"] = reviewed
        snapshot_path = vm.VM_P1_R2_G0C_SNAPSHOT_PATH
        raw = self._write_private_json(snapshot_path, snapshot)
        self.identity["snapshot_sha256"] = hashlib.sha256(raw).hexdigest()
        review = dict(self.review)
        review["reviewed_files"] = reviewed
        review["snapshot_sha256"] = self.identity["snapshot_sha256"]
        review_raw = self._write_private_json(vm.VM_P1_R2_G0C_REVIEW_PATH, review)
        self.identity["review_sha256"] = hashlib.sha256(review_raw).hexdigest()
        with self._runtime_pins(), self.assertRaisesRegex(RuntimeError, "snapshot inventory"):
            vm._historical_g0c_pair(self.identity, self.controller_identity)

    def test_06_snapshot_with_wrong_historical_test_hash_fails(self) -> None:
        snapshot = dict(self.snapshot)
        reviewed = dict(snapshot["reviewed_controller_and_test_files"])
        reviewed["vm_capacity_tests"] = dict(vm.VM_P1_R2_TEST_BINDING, sha256="0" * 64)
        snapshot["reviewed_controller_and_test_files"] = reviewed
        self._write_private_json(vm.VM_P1_R2_G0C_SNAPSHOT_PATH, snapshot)
        with self._runtime_pins(), self.assertRaisesRegex(RuntimeError, "exact accepted PASS pair"):
            vm._historical_g0c_pair(self.identity, self.controller_identity)

    def test_07_snapshot_path_substitution_fails(self) -> None:
        alternate = self.repo / "experiments/storage/alternate-snapshot.json"
        self._write_private_json(alternate, self.snapshot)
        identity = dict(self.identity, snapshot_path=alternate.relative_to(self.repo).as_posix())
        with self._runtime_pins(), self.assertRaisesRegex(RuntimeError, "exact accepted PASS pair"):
            vm._historical_g0c_pair(identity, self.controller_identity)

    def test_08_snapshot_hash_substitution_fails(self) -> None:
        identity = dict(self.identity, snapshot_sha256="0" * 64)
        with self._runtime_pins(), self.assertRaisesRegex(RuntimeError, "exact accepted PASS pair"):
            vm._historical_g0c_pair(identity, self.controller_identity)

    def test_09_review_path_or_hash_substitution_fails(self) -> None:
        changed_path = dict(self.identity, review_path="experiments/storage/alternate-review.json")
        changed_hash = dict(self.identity, review_sha256="0" * 64)
        self._write_private_json(self.repo / changed_path["review_path"], self.review)
        with self._runtime_pins():
            with self.assertRaises((RuntimeError, OSError)):
                vm._historical_g0c_pair(changed_path, self.controller_identity)
            with self.assertRaisesRegex(RuntimeError, "exact accepted PASS pair"):
                vm._historical_g0c_pair(changed_hash, self.controller_identity)

    def test_10_absent_completion_receipt_fails(self) -> None:
        with self._runtime_pins(), self.assertRaisesRegex(RuntimeError, "receipt is unavailable"):
            vm._verify_historical_g0c_identity(
                self.identity, self.controller_identity,
                p1_path_text=self.p1_path.relative_to(self.repo).as_posix(),
                p1_sha256=self.p1_sha256, p1_review_sha256=self.p1_review_sha256,
                current_controller_identity=self.current_controller,
                current_g0c_identity=self.current_g0c)

    def test_11_invalid_completion_signature_fails(self) -> None:
        receipt, _digest = self._write_completion()
        receipt[vm.VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD] = "0" * 64
        raw = policy._canonical_pretty_json_bytes(receipt)
        vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH.write_bytes(raw)
        with self._runtime_pins(), self.assertRaisesRegex(RuntimeError, "unauthenticated"):
            self._verify()

    def test_12_receipt_with_wrong_historical_g0c_review_binding_fails(self) -> None:
        receipt, _digest = self._write_completion()
        receipt["g0c_binding"]["review_sha256"] = "0" * 64
        self._replace_signed_completion(receipt)
        _record, _raw, digest, _file_id = vm._read_signed_record_snapshot(
            vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH, vm.VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
        self._write_completion_review(digest)
        with self._runtime_pins(), self.assertRaisesRegex(RuntimeError, "receipt bytes differ from the pinned anchor"):
            self._verify()

    def test_13_receipt_with_wrong_snapshot_binding_fails(self) -> None:
        receipt, _digest = self._write_completion()
        receipt["g0c_binding"]["snapshot_sha256"] = "0" * 64
        self._replace_signed_completion(receipt)
        _record, _raw, digest, _file_id = vm._read_signed_record_snapshot(
            vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH, vm.VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
        self._write_completion_review(digest)
        with self._runtime_pins(), self.assertRaisesRegex(RuntimeError, "receipt bytes differ from the pinned anchor"):
            self._verify()

    def test_14_another_historical_review_field_missing_fails(self) -> None:
        _snapshot, review, identity, _test = self._write_pair(legacy=False)
        review.pop("reviewer_identity")
        review_raw = self._write_private_json(
            self.repo / identity["review_path"], review)
        identity["review_sha256"] = hashlib.sha256(review_raw).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "exact accepted PASS pair"):
            vm._verify_historical_g0c_identity(identity, self.controller_identity)

    def test_15_two_historical_inventory_fields_missing_fails(self) -> None:
        snapshot = dict(self.snapshot)
        reviewed = dict(snapshot["reviewed_controller_and_test_files"])
        reviewed.pop("vm_capacity_tests")
        reviewed.pop("vm_capacity_controller")
        snapshot["reviewed_controller_and_test_files"] = reviewed
        raw = self._write_private_json(vm.VM_P1_R2_G0C_SNAPSHOT_PATH, snapshot)
        identity = dict(self.identity, snapshot_sha256=hashlib.sha256(raw).hexdigest())
        review = dict(self.review, reviewed_files=reviewed,
                      snapshot_sha256=identity["snapshot_sha256"])
        review_raw = self._write_private_json(vm.VM_P1_R2_G0C_REVIEW_PATH, review)
        identity["review_sha256"] = hashlib.sha256(review_raw).hexdigest()
        with self._runtime_pins(), self.assertRaises(RuntimeError):
            vm._historical_g0c_pair(identity, self.controller_identity)

    def test_16_current_test_hash_cannot_replace_historical_hash(self) -> None:
        receipt, _digest = self._write_completion()
        receipt["legacy_test_binding"]["sha256"] = self.current_controller["vm_capacity_controller_sha256"]
        self._replace_signed_completion(receipt)
        _record, _raw, digest, _file_id = vm._read_signed_record_snapshot(
            vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH, vm.VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
        self._write_completion_review(digest)
        with self._runtime_pins(), self.assertRaisesRegex(
                RuntimeError, "receipt bytes differ from the pinned anchor"):
            self._verify()

    def test_17_current_g0c_identity_cannot_replace_historical_g0c(self) -> None:
        _receipt, digest = self._write_completion()
        self._write_completion_review(digest)
        substituted = dict(self.current_g0c, snapshot_sha256="f" * 64)
        with self.assertRaisesRegex(RuntimeError, "present reviewed identities"):
            self._verify(current_g0c=substituted)
        current_as_historical = dict(self.identity,
                                     snapshot_path=self.current_g0c["snapshot_path"],
                                     snapshot_sha256=self.current_g0c["snapshot_sha256"],
                                     review_path=self.current_g0c["review_path"],
                                     review_sha256=self.current_g0c["review_sha256"])
        with self.assertRaises((RuntimeError, OSError)):
            vm._historical_g0c_pair(current_as_historical, self.controller_identity)

    def test_18_ancestry_receipt_cannot_bypass_metadata_current_g0c_gate(self) -> None:
        _receipt, digest = self._write_completion()
        self._write_completion_review(digest)
        self.assertNotIn("ancestry_receipt", __import__("inspect").signature(
            vm.fetch_latest_restore_metadata).parameters)
        output = self.repo / "must-not-be-written.json"
        with mock.patch.object(vm, "verify_fresh_g0c_review",
                               side_effect=RuntimeError("current G0C required")) as current_g0c, \
                mock.patch.object(vm.subprocess, "run") as run:
            with self.assertRaisesRegex(RuntimeError, "current G0C required"):
                vm.fetch_latest_restore_metadata(receipt_path=output)
        current_g0c.assert_called_once_with()
        run.assert_not_called()
        self.assertFalse(output.exists())

    def test_19_ancestry_receipt_cannot_replace_marker_or_download_renewal(self) -> None:
        _receipt, digest = self._write_completion()
        self._write_completion_review(digest)
        with mock.patch.object(vm, "VM_ACTIVE_MARKER_PATH", self.marker_path), \
                mock.patch.object(vm, "build_opener") as opener, \
                mock.patch.object(vm, "capture_host_observation") as observation:
            with self.assertRaises(OSError):
                vm.download_and_verify_restore()
        opener.assert_not_called()
        observation.assert_not_called()

    def test_20_source_historical_artifacts_remain_byte_identical(self) -> None:
        before = list(self.real_historical_bytes)
        _receipt, digest = self._write_completion()
        self._write_completion_review(digest)
        self._verify()
        self.assertEqual([path.read_bytes() for path in self.real_historical_paths], before)

    def test_21_receipt_with_wrong_p1_review_binding_fails(self) -> None:
        receipt, _digest = self._write_completion()
        receipt["p1_binding"]["review_sha256"] = "0" * 64
        self._replace_signed_completion(receipt)
        _record, _raw, digest, _file_id = vm._read_signed_record_snapshot(
            vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH, vm.VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
        self._write_completion_review(digest)
        with self._runtime_pins(), self.assertRaisesRegex(
                RuntimeError, "receipt bytes differ from the pinned anchor"):
            self._verify()

    def test_22_receipt_cannot_bind_a_different_current_controller(self) -> None:
        _receipt, digest = self._write_completion()
        self._write_completion_review(digest)
        changed = dict(self.current_controller, storage_policy_sha256="f" * 64)
        with self.assertRaisesRegex(RuntimeError, "present reviewed identities"):
            self._verify(current_controller=changed)

    def test_23_missing_exact_artifact_independent_review_fails(self) -> None:
        _receipt, _digest = self._write_completion()
        vm.VM_P1_G0C_ANCESTRY_REVIEW_PATH.unlink()
        with self._runtime_pins(), self.assertRaisesRegex(RuntimeError, "independent review is unavailable"):
            self._verify()

    def test_23a_changed_completion_review_fails(self) -> None:
        _receipt, _digest = self._write_completion()
        review, _raw, _digest, _identity = vm._read_json_file_snapshot(
            vm.VM_P1_G0C_ANCESTRY_REVIEW_PATH)
        review["findings"] = ["changed completion review"]
        self._write_private_json(vm.VM_P1_G0C_ANCESTRY_REVIEW_PATH, review)
        with self.assertRaisesRegex(RuntimeError, "exact independent PASS review"):
            self._verify()

    def test_23b_receipt_cannot_reuse_another_p1_or_omission(self) -> None:
        receipt, _digest = self._write_completion()
        for member, replacement in (
                ("p1_binding", dict(receipt["p1_binding"], admission_sha256="0" * 64)),
                ("omission", dict(receipt["omission"], scope="ANY_HISTORICAL_P1"))):
            with self.subTest(member=member):
                changed = json.loads(json.dumps(receipt))
                changed[member] = replacement
                self._replace_signed_completion(changed)
                with self.assertRaisesRegex(RuntimeError, "receipt bytes differ from the pinned anchor"):
                    self._verify()
                self._replace_signed_completion(json.loads(json.dumps(receipt)))

    def test_24_transition_has_no_execution_authority(self) -> None:
        _receipt, _digest = self._write_completion()
        record, _raw, _digest, _identity = vm._read_signed_record_snapshot(
            vm.VM_P1_G0C_ANCESTRY_TRANSITION_PATH,
            vm.VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD)
        self.assertEqual(record["transition_scope"], "HISTORICAL_G0C_ANCESTRY_COMPATIBILITY_ONLY")
        self.assertEqual(record["authority"], {
            "scope": "NONE", "p1_authorized": False,
            "restore_download_authorized": False, "p2_authorized": False,
            "vm_start_authorized": False,
        })
        with mock.patch.object(vm.subprocess, "run") as run, \
                mock.patch.object(vm, "build_opener") as opener, \
                mock.patch.object(vm, "capture_host_observation") as observation:
            self._verify()
        run.assert_not_called()
        opener.assert_not_called()
        observation.assert_not_called()

    def test_25_current_test_bytes_must_match_the_transition(self) -> None:
        _receipt, _digest = self._write_completion()
        (self.repo / "experiments/storage/test_vm_capacity_domain.py").write_bytes(
            b"changed after current G0C review\n")
        with self.assertRaisesRegex(RuntimeError, "current transition test identity"):
            self._verify()

    def test_26_current_independent_review_bytes_must_match(self) -> None:
        _receipt, _digest = self._write_completion()
        review, _raw, _digest, _identity = vm._read_json_file_snapshot(self.current_review_path)
        review["findings"] = ["tampered current review"]
        self._write_private_json(self.current_review_path, review)
        with self.assertRaisesRegex(RuntimeError, "current compatibility transition G0C bytes"):
            self._verify()

    def test_27_missing_current_g0c_fails_closed(self) -> None:
        _receipt, _digest = self._write_completion()
        self.current_review_path.unlink()
        with self.assertRaises((RuntimeError, OSError)):
            self._verify()

    def test_28_transition_wrong_predecessor_fails(self) -> None:
        _receipt, _digest = self._write_completion()
        record, _raw, _digest, _identity = vm._read_signed_record_snapshot(
            vm.VM_P1_G0C_ANCESTRY_TRANSITION_PATH,
            vm.VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD)
        record["predecessor"]["path"] = "R6-VM-HISTORICAL-ANCESTRY-COMPATIBILITY-TRANSITION-replay.json"
        self._replace_signed_transition(record)
        with self.assertRaisesRegex(RuntimeError, "does not start at the exact historical issuance"):
            self._verify()

    def test_29_transition_schema_authority_and_anchor_are_closed(self) -> None:
        _receipt, _digest = self._write_completion()
        record, _raw, _digest, _identity = vm._read_signed_record_snapshot(
            vm.VM_P1_G0C_ANCESTRY_TRANSITION_PATH,
            vm.VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD)
        original = json.loads(json.dumps(record))
        variants = []
        changed = json.loads(json.dumps(original))
        changed["schema_version"] = 2
        variants.append(changed)
        changed = json.loads(json.dumps(original))
        changed["authority"]["p1_authorized"] = True
        variants.append(changed)
        changed = json.loads(json.dumps(original))
        changed["historical_anchor"]["completion_receipt"]["sha256"] = "0" * 64
        variants.append(changed)
        changed = json.loads(json.dumps(original))
        changed["compatibility_rule_version"] = "UNREVIEWED_FALLBACK"
        variants.append(changed)
        changed = json.loads(json.dumps(original))
        changed["predecessor"]["sha256"] = "0" * 64
        variants.append(changed)
        changed = json.loads(json.dumps(original))
        changed["current_test_identity"]["path"] = "experiments/storage/other-tests.py"
        variants.append(changed)
        for changed in variants:
            with self.subTest(change=changed):
                self._replace_signed_transition(changed)
                with self.assertRaisesRegex(RuntimeError, "transition"):
                    self._verify()

    def test_30_invalid_transition_signature_fails(self) -> None:
        _receipt, _digest = self._write_completion()
        record, _raw, _digest, _identity = vm._read_signed_record_snapshot(
            vm.VM_P1_G0C_ANCESTRY_TRANSITION_PATH,
            vm.VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD)
        record[vm.VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD] = "0" * 64
        self._write_private_json(vm.VM_P1_G0C_ANCESTRY_TRANSITION_PATH, record)
        with self.assertRaisesRegex(RuntimeError, "unauthenticated"):
            self._verify()

    def test_30a_stale_transition_tip_fails(self) -> None:
        _receipt, _digest = self._write_completion()
        stale_tip = self.repo / (
            "R6-VM-HISTORICAL-ANCESTRY-COMPATIBILITY-TRANSITION-stale.json")
        stale_tip.write_bytes(vm.VM_P1_G0C_ANCESTRY_TRANSITION_PATH.read_bytes())
        stale_tip.chmod(0o600)
        with mock.patch.object(vm, "VM_P1_G0C_ANCESTRY_TRANSITION_PATH", stale_tip):
            with self.assertRaisesRegex(RuntimeError, "unsupported schema, anchor, or authority"):
                self._verify()

    def test_31_future_controller_c_evolves_from_b_without_rewriting_anchor(self) -> None:
        _receipt, receipt_sha256 = self._write_completion()
        receipt_path = vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH
        review_path = vm.VM_P1_G0C_ANCESTRY_REVIEW_PATH
        anchor_bytes = (receipt_path.read_bytes(), review_path.read_bytes())
        transition_b_path = vm.VM_P1_G0C_ANCESTRY_TRANSITION_PATH
        transition_b_bytes = transition_b_path.read_bytes()
        transition_b, _raw, transition_b_sha256, _identity = vm._read_signed_record_snapshot(
            transition_b_path, vm.VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD)
        anchor_b = dict(transition_b["historical_anchor"])
        controller_b = dict(self.current_controller)

        controller_c = {
            "vm_capacity_controller_sha256": "d" * 64,
            "storage_policy_sha256": "e" * 64,
            "restore_metadata_probe_sha256": "f" * 64,
        }
        snapshot_c = self.repo / "experiments/storage/current-c-snapshot.json"
        review_c = self.repo / "experiments/storage/current-c-review.json"
        transition_c_path = self.repo / (
            "R6-VM-HISTORICAL-ANCESTRY-COMPATIBILITY-TRANSITION-test-c.json")
        self.current_snapshot_path = snapshot_c
        self.current_review_path = review_c
        for name, value in (
                ("G0C_SNAPSHOT_PATH", snapshot_c),
                ("G0C_REVIEW_PATH", review_c),
                ("VM_P1_G0C_ANCESTRY_TRANSITION_PATH", transition_c_path),
                ("VM_P1_G0C_ANCESTRY_TRANSITION_PREDECESSOR_PATH", transition_b_path)):
            patcher = mock.patch.object(vm, name, value)
            patcher.start()
            self.patchers.append(patcher)
            self.addCleanup(patcher.stop)
        self._write_current_g0c(controller=controller_c, test_bytes=b"synthetic current controller tests C\n")

        with self._runtime_pins(), \
                mock.patch.object(vm, "_controller_identity", return_value=controller_c), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=self.current_g0c):
            transition_c = vm.create_historical_g0c_ancestry_compatibility_transition()
            self.assertEqual(transition_c["sequence"], 2)
            self.assertEqual(transition_c["predecessor"], {
                "kind": "HISTORICAL_ANCESTRY_COMPATIBILITY_TRANSITION",
                "path": transition_b_path.relative_to(self.repo).as_posix(),
                "sha256": transition_b_sha256,
            })
            self.assertEqual(transition_c["previous_controller_identity"], controller_b)
            self.assertEqual(transition_c["current_controller_identity"], controller_c)
            self.assertEqual(transition_c["previous_g0c_identity"], transition_b["current_g0c_identity"])
            self.assertEqual(transition_c["historical_anchor"], anchor_b)
            self.assertEqual(transition_c["historical_anchor"]["issuance_controller_identity"],
                             self.issuance_controller)
            vm._verify_historical_g0c_identity(
                self.identity, self.controller_identity,
                p1_path_text=self.p1_path.relative_to(self.repo).as_posix(),
                p1_sha256=self.p1_sha256,
                p1_review_sha256=self.p1_review_sha256,
                current_controller_identity=controller_c,
                current_g0c_identity=self.current_g0c)
        self.assertEqual((receipt_path.read_bytes(), review_path.read_bytes()), anchor_bytes)
        self.assertEqual(transition_b_path.read_bytes(), transition_b_bytes)
        self.assertEqual(transition_c["historical_anchor"]["completion_receipt"]["sha256"],
                         receipt_sha256)

        bad_gap = json.loads(json.dumps(transition_c))
        bad_gap["sequence"] = 3
        self._replace_signed_transition(bad_gap)
        with self._runtime_pins(), \
                mock.patch.object(vm, "_controller_identity", return_value=controller_c), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=self.current_g0c), \
                self.assertRaisesRegex(RuntimeError, "sequence gap or rollback"):
            vm._verify_historical_g0c_identity(
                self.identity, self.controller_identity,
                p1_path_text=self.p1_path.relative_to(self.repo).as_posix(),
                p1_sha256=self.p1_sha256,
                p1_review_sha256=self.p1_review_sha256,
                current_controller_identity=controller_c,
                current_g0c_identity=self.current_g0c)

        bad_cycle = json.loads(json.dumps(transition_c))
        bad_cycle["sequence"] = 2
        bad_cycle["predecessor"] = {
            "kind": "HISTORICAL_ANCESTRY_COMPATIBILITY_TRANSITION",
            "path": transition_c_path.relative_to(self.repo).as_posix(),
            "sha256": "0" * 64,
        }
        self._replace_signed_transition(bad_cycle)
        with self._runtime_pins(), \
                mock.patch.object(vm, "_controller_identity", return_value=controller_c), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=self.current_g0c), \
                self.assertRaisesRegex(RuntimeError, "cyclic"):
            vm._verify_historical_g0c_identity(
                self.identity, self.controller_identity,
                p1_path_text=self.p1_path.relative_to(self.repo).as_posix(),
                p1_sha256=self.p1_sha256,
                p1_review_sha256=self.p1_review_sha256,
                current_controller_identity=controller_c,
                current_g0c_identity=self.current_g0c)

    def _replace_signed_completion(self, record: dict[str, object]) -> None:
        record.pop(vm.VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD, None)
        sealed = vm._seal(record, vm.VM_P1_G0C_ANCESTRY_SIGNATURE_FIELD)
        raw = policy._canonical_pretty_json_bytes(sealed)
        vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH.write_bytes(raw)
        vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH.chmod(0o600)

    def _replace_signed_transition(self, record: dict[str, object]) -> None:
        record.pop(vm.VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD, None)
        sealed = vm._seal(record, vm.VM_P1_G0C_ANCESTRY_TRANSITION_SIGNATURE_FIELD)
        self._write_private_json(vm.VM_P1_G0C_ANCESTRY_TRANSITION_PATH, sealed)


class HistoricalG0CRealAnchorTests(unittest.TestCase):
    """Read-only check of the exact existing historical receipt and review."""

    def test_exact_completion_receipt_and_review_remain_historical_anchor(self) -> None:
        paths = (
            vm.VM_P1_ACCEPTED_ADMISSION_PATH,
            vm.VM_P1_ACCEPTED_REVIEW_PATH,
            vm.VM_P1_R2_G0C_SNAPSHOT_PATH,
            vm.VM_P1_R2_G0C_REVIEW_PATH,
            vm.VM_P1_G0C_ANCESTRY_RECEIPT_PATH,
            vm.VM_P1_G0C_ANCESTRY_REVIEW_PATH,
            policy.REPO / vm.VM_P1_G0C_ANCESTRY_ISSUANCE_G0C_IDENTITY["snapshot_path"],
            policy.REPO / vm.VM_P1_G0C_ANCESTRY_ISSUANCE_G0C_IDENTITY["review_path"],
        )
        before = tuple(path.read_bytes() for path in paths)
        anchor = vm._accepted_historical_ancestry_anchor()
        self.assertEqual(anchor["completion_receipt"]["sha256"],
                         vm.VM_P1_G0C_ANCESTRY_RECEIPT_SHA256)
        self.assertEqual(anchor["completion_review"]["sha256"],
                         vm.VM_P1_G0C_ANCESTRY_REVIEW_SHA256)
        self.assertEqual(anchor["issuance_controller_identity"],
                         vm.VM_P1_G0C_ANCESTRY_ISSUANCE_CONTROLLER_IDENTITY)
        self.assertEqual(anchor["issuance_g0c_identity"],
                         vm.VM_P1_G0C_ANCESTRY_ISSUANCE_G0C_IDENTITY)
        self.assertEqual(tuple(path.read_bytes() for path in paths), before)


class P1FreshnessRenewalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="p1-renewal-", dir=policy.REPO)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.marker_path = self.root / "active-session.json"
        original_fresh_artifact_path = vm._fresh_artifact_path

        def isolated_fresh_artifact_path(prefix: str, run_id: str) -> Path:
            if prefix == vm.VM_P1_FRESHNESS_START_PREFIX:
                return self.root / f"{prefix}-{run_id}.json"
            return original_fresh_artifact_path(prefix, run_id)

        artifact_path_patcher = mock.patch.object(
            vm, "_fresh_artifact_path", side_effect=isolated_fresh_artifact_path)
        artifact_path_patcher.start()
        self.addCleanup(artifact_path_patcher.stop)
        for patcher in (
                mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", self.root / "key"),
                mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ROOT", self.root / "domain"),
                mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ACTIVE_MARKER", self.marker_path),
                mock.patch.object(vm, "VM_ACTIVE_MARKER_PATH", self.marker_path)):
            patcher.start()
            self.addCleanup(patcher.stop)
        policy._manifest_signing_key(create=True)
        self.session_id = "a" * 32
        self.volume_uuid = "11111111-1111-1111-1111-111111111111"
        self.p1_path = self.root / "p1.json"
        self.p1_path_text = self.p1_path.relative_to(policy.REPO).as_posix()
        vm.write_active_session_marker(
            session_id=self.session_id, phase_id="P1_ADMISSION",
            container_uuid=vm.EXPECTED_CONTAINER_UUID, volume_uuid=self.volume_uuid,
            committed_growth_bytes=0, plan_sha256="b" * 64,
            quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            safety_volume_uuid="33333333-3333-3333-3333-333333333333",
            safety_volume_in_use_bytes=0, safety_container_uuid=vm.EXPECTED_CONTAINER_UUID,
            safety_physical_store_uuid=vm.EXPECTED_PHYSICAL_STORE_UUID,
            pre_reserve_shared_free_bytes=70 * vm.GIB,
            unreserved_free_ceiling_bytes=70 * vm.GIB - vm.VM_PROTECTED_CAPACITY_RESERVE_BYTES)
        self.p1_record = vm._write_signed_json(self.p1_path, {
            "record_type": "R6_VM_P1_RESTORE_DOWNLOAD_ADMISSION", "mode": vm.VM_MODE,
            "decision": "PASS", "authorization": "DOWNLOAD_AND_VERIFY_RESTORE_ONLY",
            "authorized_phase": "P1", "p1_authorized": True,
            "restore_download_authorized": True, "p2_authorized": False,
            "vm_start_authorized": False, "session_id": self.session_id,
            "vm_volume_uuid": self.volume_uuid, "receipt_path": self.p1_path_text,
            "created_at_unix": time.time()}, "vm_admission_hmac_sha256")
        self.p1_sha = vm._file_sha256(self.p1_path)
        vm.update_active_session_marker(
            self.session_id, phase_id="P1", p1_admission_path=self.p1_path,
            p1_admission_sha256=self.p1_sha,
            expected_marker_sha256=vm._file_sha256(self.marker_path))
        self.p1_bytes = self.p1_path.read_bytes()

        self.metadata = {
            "source_url": "https://updates.cdn-apple.com/restore/image.ipsw?token=A%2fB",
            "final_url": "https://updates.cdn-apple.com/restore/image.ipsw?token=A%2fB",
            "expected_length_bytes": 26_637_307_067,
            "operating_system_version": "27.0.1", "build_version": "26A434",
            "is_supported": True, "hardware_model_data_sha256": "c" * 64,
            "hardware_model_supported": True, "minimum_supported_cpu_count": 2,
            "minimum_supported_memory_size_bytes": 4_294_967_296,
        }
        self.host = {"architecture": "arm64", "product_version": "27.0.1",
                     "build_version": "26A434"}
        self.fresh = self._fresh()
        self.subject = vm._complete_restore_subject(
            vm._legacy_restore_subject(self.fresh, self.host), self.fresh)
        self.subject_id = vm._restore_subject_digest(self.subject)
        self.hardware = vm._hardware_compatibility_tuple(self.fresh)
        self.p0 = {"p0_admission_path": "experiments/storage/p0.json",
                   "p0_admission_sha256": "1" * 64,
                   "p0_review_path": "experiments/storage/p0-review.json",
                   "p0_review_sha256": "2" * 64,
                   "creation_receipt_path": "experiments/storage/creation.json",
                   "creation_receipt_sha256": "3" * 64,
                   "metadata_receipt_path": "experiments/storage/metadata.json",
                   "metadata_receipt_sha256": "4" * 64}
        self.controller = {"controller_sha256": "5" * 64,
                           "restore_metadata_probe_sha256": "6" * 64}
        self.g0c = {"snapshot_sha256": "7" * 64, "review_sha256": "8" * 64}
        self.historical_controller = {"controller_sha256": "9" * 64}
        self.historical_g0c = {"snapshot_sha256": "a" * 64}
        self.paths = {
            "lineage": "experiments/storage/lineage.json",
            "basis": "experiments/storage/basis.json",
            "basis_review": "experiments/storage/basis-review.json",
            "transition": "experiments/storage/transition.json",
            "transition_review": "experiments/storage/transition-review.json",
            "creation": "experiments/storage/creation.json",
            "p1_review": "experiments/storage/p1-review.json",
        }
        self.admission = {
            "authorization": "DOWNLOAD_AND_VERIFY_RESTORE_ONLY",
            "authorized_phase": "P1",
            "p1_authorized": True,
            "restore_download_authorized": True,
            "p2_authorized": False,
            "vm_start_authorized": False,
            "session_id": self.session_id, "vm_volume_uuid": self.volume_uuid,
            "vm_volume_quota_bytes": vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            "safety_volume_uuid": "33333333-3333-3333-3333-333333333333",
            "restore_expected_bytes": self.fresh["expected_length_bytes"],
            "concurrent_growth_bound_bytes": 0, "p0_ancestry": self.p0,
            "restore_lineage_path": self.paths["lineage"], "restore_lineage_sha256": "b" * 64,
            "compatibility_basis_path": self.paths["basis"],
            "compatibility_basis_sha256": "c" * 64,
            "compatibility_basis_review_path": self.paths["basis_review"],
            "compatibility_basis_review_sha256": "d" * 64,
            "p0_transition_path": self.paths["transition"], "p0_transition_sha256": "e" * 64,
            "p0_transition_review_path": self.paths["transition_review"],
            "p0_transition_review_sha256": "f" * 64,
            "volume_creation_receipt_path": self.paths["creation"],
            "volume_creation_receipt_sha256": "3" * 64, "metadata_ancestor_sha256": "4" * 64,
            "restore_subject_identity": self.subject_id, "restore_subject": self.subject,
            "restore_upstream_metadata": vm.validate_restore_metadata(
                self.fresh["source_url"], self.fresh["expected_length_bytes"],
                self.fresh["metadata_sha256"], self.fresh["final_url"]),
        }
        self.basis = {"restore_subject_identity": self.subject_id,
                      "restore_subject": self.subject,
                      "adopted_hardware_compatibility": self.hardware}
        self.fresh_sha = "d" * 64
        self.fresh_identity = (1, 2, 3, 4, 5)
        self.metadata_path = self.root / "fresh-metadata.json"
        self.renewal_path = self.root / "renewal.json"

    def _fresh(self, metadata: dict[str, object] | None = None,
               host: dict[str, object] | None = None, *, captured_at: float | None = None
               ) -> dict[str, object]:
        return {**(metadata or self.metadata), "host": host or self.host,
                "metadata_sha256": "b" * 64,
                "captured_at_unix": time.time() if captured_at is None else captured_at}

    def _authority(self) -> dict[str, object]:
        marker, _raw, marker_sha, _identity = vm._read_signed_record_snapshot(
            self.marker_path, "vm_session_hmac_sha256")
        return {
            "admission": self.admission, "compatibility_basis": self.basis,
            "p1_path": self.p1_path, "p1_path_text": self.p1_path_text,
            "p1_sha256": self.p1_sha, "p1_review_path_text": self.paths["p1_review"],
            "p1_review_sha256": "c" * 64, "creation_path_text": self.paths["creation"],
            "lineage_sha256": self.admission["restore_lineage_sha256"],
            "basis_sha256": self.admission["compatibility_basis_sha256"],
            "basis_review_sha256": self.admission["compatibility_basis_review_sha256"],
            "transition_sha256": self.admission["p0_transition_sha256"],
            "transition_review_sha256": self.admission["p0_transition_review_sha256"],
            "historical_controller_identity": self.historical_controller,
            "historical_g0c_identity": self.historical_g0c,
            "current_controller_identity": self.controller, "g0c_identity": self.g0c,
            "marker": marker, "marker_sha256": marker_sha,
        }

    def _live_storage(self) -> dict[str, object]:
        captured_at = time.time()
        observation = _session_observation(
            free_bytes=64 * vm.GIB, vm_volume_uuid=self.volume_uuid,
            quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            safety_volume_uuid=self.admission["safety_volume_uuid"],
            safety_reserve_bytes=vm.VM_PROTECTED_CAPACITY_RESERVE_BYTES)
        observation["captured_at_unix"] = captured_at
        mount_point = self.root / "synthetic-vm-volume"
        return {
            "captured_at_unix": captured_at,
            "session_id": self.session_id,
            "vm_volume_uuid": self.volume_uuid,
            "safety_volume_uuid": self.admission["safety_volume_uuid"],
            "container_uuid": vm.EXPECTED_CONTAINER_UUID,
            "physical_store_uuid": vm.EXPECTED_PHYSICAL_STORE_UUID,
            "quota_bytes": vm.VM_REVIEWED_QUOTA_MAX_BYTES,
            "safety_reserve_bytes": vm.VM_PROTECTED_CAPACITY_RESERVE_BYTES,
            "unreserved_free_bytes": 60 * vm.GIB,
            "vm_volume_used_bytes": 0,
            "mount": {
                "mounted": True,
                "mount_state": "MOUNTED_VERIFIED",
                "volume_uuid": self.volume_uuid.upper(),
                "container_uuid": vm.EXPECTED_CONTAINER_UUID.upper(),
                "filesystem_type": "apfs",
                "mount_point": str(mount_point),
            },
            "observation": observation,
            "capacity_sample": {"admitted": True, "blockers": []},
            "restore_payload_absent": True,
        }

    def _create(self, fresh: dict[str, object] | None = None,
                receipt_path: Path | None = None) -> dict[str, object]:
        chosen = fresh or self.fresh
        with mock.patch.object(vm, "_verify_p1_download_authority",
                               return_value=self._authority()), \
                mock.patch.object(vm, "_read_latest_restore_metadata_snapshot",
                                  return_value=(chosen, self.fresh_sha, self.fresh_identity)), \
                mock.patch.object(vm, "_controller_identity", return_value=self.controller), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=self.g0c), \
                mock.patch.object(vm, "_capture_p1_renewal_live_storage",
                                  return_value={"session_id": self.session_id,
                                                "vm_volume_uuid": self.volume_uuid,
                                                "safety_volume_uuid": self.admission["safety_volume_uuid"],
                                                "container_uuid": vm.EXPECTED_CONTAINER_UUID,
                                                "physical_store_uuid": vm.EXPECTED_PHYSICAL_STORE_UUID,
                                                "unreserved_free_bytes": 60 * vm.GIB,
                                                "restore_payload_absent": True}):
            return vm.create_p1_freshness_renewal(
                self.metadata_path, receipt_path=receipt_path or self.renewal_path)

    def _verify(self, created: dict[str, object], *, fresh: dict[str, object] | None = None,
                authority: dict[str, object] | None = None,
                attempt_id: str | None = None) -> dict[str, object]:
        current = self._authority()
        if authority:
            current.update(authority)
        chosen = fresh or self.fresh
        with mock.patch.object(vm, "_read_latest_restore_metadata_snapshot",
                               return_value=(chosen, self.fresh_sha, self.fresh_identity)), \
                mock.patch.object(vm, "_controller_identity", return_value=self.controller), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=self.g0c):
            return vm._verify_p1_freshness_renewal(
                current, allow_consumed_attempt_id=attempt_id)

    def test_active_p1_same_subject_fresh_observation_renews_successfully(self) -> None:
        created = self._create()
        self.assertEqual(self._verify(created)["renewal_sha256"], created["renewal_sha256"])
        marker, _raw, _sha, _identity = vm._read_signed_record_snapshot(
            self.marker_path, "vm_session_hmac_sha256")
        self.assertEqual((marker["state"], marker["phase_id"]), ("ACTIVE", "P1"))
        self.assertEqual(marker["p1_admission_sha256"], self.p1_sha)

    def test_combined_path_validates_existing_p1_before_fetching_metadata(self) -> None:
        review_path = self.root / "synthetic-review.json"
        with mock.patch.object(
                vm, "_verify_p1_download_authority",
                side_effect=RuntimeError("synthetic missing or invalid P1")) as verify_authority, \
                mock.patch.object(vm, "fetch_latest_restore_metadata") as fetch_metadata:
            with self.assertRaisesRegex(RuntimeError, "synthetic missing or invalid P1"):
                vm.renew_p1_and_download(
                    p1_admission_path=self.p1_path, p1_review_path=review_path)
        verify_authority.assert_called_once_with(
            self.p1_path, review_path, require_freshness_renewal=False)
        fetch_metadata.assert_not_called()

    def test_stale_fresh_observation_fails(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self._create(self._fresh(captured_at=time.time() - 301))

    def test_wrong_original_p1_admission_fails(self) -> None:
        other = self.root / "other-p1.json"
        other_text = other.relative_to(policy.REPO).as_posix()
        vm._write_signed_json(other, {**self.p1_record, "receipt_path": other_text},
                              "vm_admission_hmac_sha256")
        with self.assertRaisesRegex(RuntimeError, "exact marker-bound admission"):
            vm._verify_p1_download_authority(other)

    def test_wrong_original_p0_lineage_fails(self) -> None:
        created = self._create()
        wrong = self._authority()
        wrong["admission"] = {**self.admission, "p0_ancestry": {**self.p0, "p0_review_sha256": "0" * 64}}
        with self.assertRaisesRegex(RuntimeError, "immutable authority bindings"):
            self._verify(created, authority=wrong)

    def test_changed_full_subject_identity_fails(self) -> None:
        wrong = self._authority()
        wrong["admission"] = {**self.admission, "restore_subject_identity": "rsid-v1:sha256:" + "0" * 64}
        with self.assertRaisesRegex(RuntimeError, "RESTORE SUBJECT CHANGED"):
            vm._require_same_p1_restore_subject(self.fresh, wrong)

    def test_changed_content_length_fails(self) -> None:
        changed = self._fresh({**self.metadata, "expected_length_bytes": self.metadata["expected_length_bytes"] + 1})
        with self.assertRaisesRegex(RuntimeError, "RESTORE SUBJECT CHANGED"):
            vm._require_same_p1_restore_subject(changed, self._authority())

    def test_changed_macos_version_and_build_fail(self) -> None:
        for field, value, host_field in (("operating_system_version", "27.0.2", "product_version"),
                                         ("build_version", "26A435", "build_version")):
            changed = dict(self.metadata)
            changed[field] = value
            host = dict(self.host)
            host[host_field] = value
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, "RESTORE SUBJECT CHANGED"):
                vm._require_same_p1_restore_subject(self._fresh(changed, host), self._authority())

    def test_changed_compatibility_tuple_fails(self) -> None:
        changed = self._fresh({**self.metadata, "hardware_model_data_sha256": "0" * 64})
        with self.assertRaisesRegex(RuntimeError, "RESTORE SUBJECT CHANGED"):
            vm._require_same_p1_restore_subject(changed, self._authority())

    def test_p1_admission_marker_fails_renewal(self) -> None:
        marker = {**self._authority()["marker"], "phase_id": "P1_ADMISSION"}
        with self.assertRaisesRegex(RuntimeError, "ACTIVE/P1"):
            vm._validate_p1_freshness_renewal_marker_update(marker, {}, session_id=self.session_id)

    def test_p2_or_another_phase_fails_renewal(self) -> None:
        for phase in ("P1_VERIFIED", "P2_AUTHORIZED", "P3_SEALED"):
            marker = {**self._authority()["marker"], "phase_id": phase}
            with self.subTest(phase=phase), self.assertRaisesRegex(RuntimeError, "ACTIVE/P1"):
                vm._validate_p1_freshness_renewal_marker_update(marker, {}, session_id=self.session_id)

    def test_renewal_cannot_broaden_p1_authority(self) -> None:
        record = self._create()["renewal"]
        self.assertEqual(record["retained_authority"], "DOWNLOAD_AND_VERIFY_RESTORE_ONLY")
        self.assertFalse(record["p1_authorized"])
        self.assertFalse(record["restore_download_authorized"])

    def test_renewal_cannot_authorize_vm_start(self) -> None:
        self.assertIs(self._create()["renewal"]["vm_start_authorized"], False)

    def test_renewal_cannot_authorize_p2(self) -> None:
        self.assertIs(self._create()["renewal"]["p2_authorized"], False)

    def test_renewal_replay_for_another_session_fails(self) -> None:
        created = self._create()
        wrong = self._authority()
        wrong["marker"] = {**wrong["marker"], "session_id": "b" * 32}
        with self.assertRaisesRegex(RuntimeError, "invalid or exceeds"):
            self._verify(created, authority=wrong)

    def test_single_use_renewal_cannot_be_consumed_twice(self) -> None:
        self._create()
        marker, _raw, digest, _identity = vm._read_signed_record_snapshot(self.marker_path, "vm_session_hmac_sha256")
        claim = dict(marker["p1_freshness_renewal"])
        claim.update(use_state="CONSUMED", attempt_id="1" * 32,
                     claimed_at_unix=time.time(), body_start_evidence=None)
        vm.update_active_session_marker(self.session_id, p1_freshness_renewal=claim,
                                        expected_marker_sha256=digest)
        marker, _raw, digest, _identity = vm._read_signed_record_snapshot(self.marker_path, "vm_session_hmac_sha256")
        replay = dict(marker["p1_freshness_renewal"])
        replay.update(attempt_id="2" * 32, claimed_at_unix=time.time())
        with self.assertRaisesRegex(RuntimeError, "same terminally consumed attempt"):
            vm.update_active_session_marker(self.session_id, p1_freshness_renewal=replay,
                                            expected_marker_sha256=digest)

    def test_download_verifier_rejects_expired_renewal(self) -> None:
        created = self._create()
        with self.assertRaisesRegex(RuntimeError, "stale"):
            self._verify(created, fresh=self._fresh(captured_at=time.time() - 301))

    def test_verifier_rejects_wrong_renewal_hash(self) -> None:
        self._create()
        current = self._authority()
        current["marker"] = {**current["marker"],
                             "p1_freshness_renewal": {
                                 **current["marker"]["p1_freshness_renewal"], "sha256": "0" * 64}}
        with self.assertRaisesRegex(RuntimeError, "invalid or exceeds"):
            self._verify({}, authority=current)

    def test_body_download_cannot_start_before_renewal(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "marker-bound freshness renewal"):
            vm._verify_p1_freshness_renewal(self._authority())

    def test_whole_body_may_finish_after_start_window(self) -> None:
        created = self._create()
        self.assertEqual(vm._check_p1_renewal_freshness_now(
            created["renewal"], self.fresh, now=self.fresh["captured_at_unix"] + 300,
            monotonic_at_seal=10.0, monotonic_now=10.0), 300)

    def test_historical_p1_receipt_is_byte_for_byte_unchanged(self) -> None:
        original = self.p1_path.read_bytes()
        self._create()
        self.assertEqual(self.p1_path.read_bytes(), original)
        self.assertEqual(original, self.p1_bytes)

    def test_exact_300_is_accepted_and_future_timestamp_rejected(self) -> None:
        created = self._create()
        self.assertEqual(vm._check_p1_renewal_freshness_now(
            created["renewal"], self.fresh, now=self.fresh["captured_at_unix"] + 300,
            monotonic_at_seal=10.0, monotonic_now=10.0), 300)
        future = self._fresh(captured_at=time.time() + 1)
        with self.assertRaisesRegex(RuntimeError, "future-dated or stale"):
            vm._check_p1_renewal_freshness_now(created["renewal"], future)

    def test_monotonic_elapsed_cannot_be_extended_by_wall_clock_rollback(self) -> None:
        created = self._create()
        renewal = {**created["renewal"],
                   "fresh_metadata": {**created["renewal"]["fresh_metadata"],
                                      "age_at_renewal_seconds": 299.0}}
        recent = self._fresh(captured_at=900.0)
        self.assertEqual(vm._check_p1_renewal_freshness_now(
            renewal, recent, now=1_000.0, monotonic_at_seal=10.0,
            monotonic_now=11.0), 300.0)
        with self.assertRaisesRegex(RuntimeError, "expired before first body"):
            vm._check_p1_renewal_freshness_now(
                renewal, recent, now=1_000.0, monotonic_at_seal=10.0,
                monotonic_now=11.01)

    def test_delayed_redirect_rechecks_freshness(self) -> None:
        stale = self._fresh(captured_at=100.0)
        renewal = {"fresh_metadata": {"age_at_renewal_seconds": 299.0}}
        handler = vm._AppleOnlyRedirectHandler(
            self.fresh["expected_length_bytes"], self.fresh["metadata_sha256"],
            self.fresh["source_url"], self.fresh["final_url"],
            before_redirect=lambda: vm._check_p1_renewal_freshness_now(renewal, stale, now=401.0))
        with self.assertRaisesRegex(RuntimeError, "stale"):
            handler.redirect_request(vm.Request(self.fresh["source_url"]), None, 302,
                                     "redirect", {}, self.fresh["final_url"])
        self.assertEqual(handler.redirect_count, 0)

    def test_final_url_and_query_are_bound_exactly(self) -> None:
        handler = vm._AppleOnlyRedirectHandler(
            self.fresh["expected_length_bytes"], self.fresh["metadata_sha256"],
            self.fresh["source_url"], self.fresh["final_url"])
        self.assertEqual(handler.validate_final_url(self.fresh["final_url"]), self.fresh["final_url"])
        with self.assertRaisesRegex(RuntimeError, "differs from the signed metadata"):
            handler.validate_final_url("https://updates.cdn-apple.com/restore/image.ipsw?token=other")

    def test_sequence_and_predecessor_advance_after_consumption(self) -> None:
        first = self._create()
        self.assertIsNone(first["renewal"]["predecessor_path"])
        self.assertIsNone(first["renewal"]["predecessor_sha256"])
        marker, _raw, digest, _identity = vm._read_signed_record_snapshot(self.marker_path, "vm_session_hmac_sha256")
        claim = dict(marker["p1_freshness_renewal"])
        claim.update(use_state="CONSUMED", attempt_id="3" * 32,
                     claimed_at_unix=time.time(), body_start_evidence=None)
        vm.update_active_session_marker(self.session_id, p1_freshness_renewal=claim,
                                        expected_marker_sha256=digest)
        second = self._create(receipt_path=self.root / "renewal-2.json")
        self.assertEqual(second["sequence"], 2)
        self.assertEqual(second["renewal"]["predecessor_path"], first["renewal_path_text"])
        self.assertEqual(second["renewal"]["predecessor_sha256"], first["renewal_sha256"])

    def test_predecessor_path_mismatch_is_rejected_by_verifier_and_marker_cas(self) -> None:
        first = self._create()
        marker, _raw, digest, _identity = vm._read_signed_record_snapshot(
            self.marker_path, "vm_session_hmac_sha256")
        claim = dict(marker["p1_freshness_renewal"])
        claim.update(use_state="CONSUMED", attempt_id="6" * 32,
                     claimed_at_unix=time.time(), body_start_evidence=None)
        vm.update_active_session_marker(
            self.session_id, p1_freshness_renewal=claim,
            expected_marker_sha256=digest)
        second = self._create(receipt_path=self.root / "renewal-2.json")
        self.assertEqual(second["renewal"]["predecessor_path"], first["renewal_path_text"])

        wrong_authority = self._authority()
        binding = dict(wrong_authority["marker"]["p1_freshness_renewal"])
        binding["predecessor_path"] = "wrong-predecessor.json"
        wrong_authority["marker"] = {
            **wrong_authority["marker"], "p1_freshness_renewal": binding}
        with self.assertRaisesRegex(RuntimeError, "invalid or exceeds"):
            self._verify(second, authority=wrong_authority)

        current, _raw, current_sha, _identity = vm._read_signed_record_snapshot(
            self.marker_path, "vm_session_hmac_sha256")
        forged_binding = dict(current["p1_freshness_renewal"])
        forged_binding["predecessor_path"] = "wrong-predecessor.json"
        with self.assertRaisesRegex(RuntimeError, "does not bind a valid inert receipt"):
            vm.update_active_session_marker(
                self.session_id, p1_freshness_renewal=forged_binding,
                expected_marker_sha256=current_sha)

    def test_concurrent_consumers_allow_only_one_claim(self) -> None:
        self._create()
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        lock = threading.Lock()

        def claim(attempt: str) -> None:
            barrier.wait()
            try:
                with policy._acquire_storage_allocation_lock():
                    marker, _raw, digest, _identity = vm._read_signed_record_snapshot(
                        self.marker_path, "vm_session_hmac_sha256")
                    binding = dict(marker["p1_freshness_renewal"])
                    binding.update(use_state="CONSUMED", attempt_id=attempt,
                                   claimed_at_unix=time.time(), body_start_evidence=None)
                    vm.update_active_session_marker(self.session_id, p1_freshness_renewal=binding,
                                                    expected_marker_sha256=digest)
                result = "claimed"
            except RuntimeError:
                result = "rejected"
            with lock:
                outcomes.append(result)

        threads = [threading.Thread(target=claim, args=(digit * 32,)) for digit in ("4", "5")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertCountEqual(outcomes, ["claimed", "rejected"])

    def _run_synthetic_download(self, *, status: int = 200,
                                content_length: str | None = None,
                                content_length_values: list[str] | None = None,
                                first_chunk: bytes = b"abc",
                                stale_after_read: bool = False,
                                stale_before_read: bool = False,
                                live_state_changes: bool = False,
                                final_url: str | None = None,
                                output_open_failure: bool = False,
                                stream_read_failure_after_first: bool = False,
                                stream_write_fail_after_bytes: int | None = None,
                                recorder_hook: object | None = None
                                ) -> tuple[dict[str, object], object, Path, list[float]]:
        metadata = {**self.metadata, "expected_length_bytes": 3}
        fresh = self._fresh(metadata)
        self.fresh = fresh
        self.subject = vm._complete_restore_subject(
            vm._legacy_restore_subject(fresh, self.host), fresh)
        self.subject_id = vm._restore_subject_digest(self.subject)
        self.hardware = vm._hardware_compatibility_tuple(fresh)
        self.admission.update({
            "restore_expected_bytes": 3,
            "restore_subject_identity": self.subject_id,
            "restore_subject": self.subject,
            "restore_upstream_metadata": vm.validate_restore_metadata(
                fresh["source_url"], 3, fresh["metadata_sha256"], fresh["final_url"]),
        })
        self.basis.update({"restore_subject_identity": self.subject_id,
                           "restore_subject": self.subject,
                           "adopted_hardware_compatibility": self.hardware})
        created = self._create(fresh=fresh)
        mount = self.root / "synthetic-vm-volume"
        mount.mkdir()
        metadata_path_text = self.metadata_path.relative_to(policy.REPO).as_posix()
        elapsed = {"seconds": 0.0}

        class Headers:
            def get_all(_self, name: str, default: object = None) -> list[str]:
                if name.lower() == "content-length":
                    if content_length_values is not None:
                        return list(content_length_values)
                    return [content_length if content_length is not None else "3"]
                return []

            def get(_self, name: str, default: object = None) -> object:
                return default

        class Response:
            def __init__(_self) -> None:
                _self.status = status
                _self.headers = Headers()
                _self.read_calls = 0

            def __enter__(_self):
                return _self

            def __exit__(_self, *_args: object) -> None:
                return None

            def geturl(_self) -> str:
                return final_url if final_url is not None else str(fresh["final_url"])

            def read(_self, _size: int) -> bytes:
                _self.read_calls += 1
                if _self.read_calls == 1:
                    elapsed["seconds"] = 301.0 if stale_after_read else 300.0
                    return first_chunk
                if stream_read_failure_after_first:
                    raise OSError("synthetic stream read failure after first accepted block")
                elapsed["seconds"] = 301.0
                return b""

        response = Response()
        clock_checks: list[float] = []
        output_open_state: dict[str, object] = {"opened": False}
        real_os_open = os.open

        def check_freshness(*_args: object, **_kwargs: object) -> float:
            clock_checks.append(elapsed["seconds"])
            if stale_before_read and len(clock_checks) >= 2:
                raise RuntimeError("P1 renewal expired before first body read")
            if stale_after_read and len(clock_checks) >= 4:
                raise RuntimeError("P1 renewal expired after first body read")
            return min(elapsed["seconds"], 300.0)

        def live_boundary(_authority: object) -> dict[str, object]:
            if live_state_changes:
                return {"status": "FAIL", "blockers": ["synthetic live session state changed"]}
            return {"status": "PASS", "blockers": [], **self._live_storage()}

        def authority_reader(*_args: object, **_kwargs: object) -> dict[str, object]:
            authority = self._authority()
            if _kwargs.get("require_freshness_renewal") is False:
                return authority
            authority.update({
                "renewal": created["renewal"],
                "renewal_sha256": created["renewal_sha256"],
                "renewal_path_text": created["renewal_path_text"],
                "renewal_fresh_metadata": fresh,
                "renewal_fresh_metadata_path_text": metadata_path_text,
                "renewal_fresh_metadata_receipt_sha256": self.fresh_sha,
                "renewal_fresh_metadata_age_seconds": 0.0,
                "fresh_metadata": fresh,
                "fresh_metadata_receipt_sha256": self.fresh_sha,
                "fresh_metadata_age_seconds": 0.0,
                "renewal_fresh_metadata_verifier_entry_at_unix": time.time(),
                "renewal_fresh_metadata_verifier_entry_age_seconds": 0.0,
                "metadata": self.admission["restore_upstream_metadata"],
            })
            return authority

        def renewal_reader(authority: dict[str, object], **_kwargs: object) -> dict[str, object]:
            marker = authority["marker"]
            binding = marker["p1_freshness_renewal"]
            verified = {
                "renewal": created["renewal"],
                "renewal_path": created["renewal_path"],
                "renewal_path_text": created["renewal_path_text"],
                "renewal_sha256": created["renewal_sha256"],
                "renewal_binding": binding,
                "renewal_fresh_metadata": fresh,
                "renewal_fresh_metadata_path": self.metadata_path,
                "renewal_fresh_metadata_path_text": metadata_path_text,
                "renewal_fresh_metadata_receipt_sha256": self.fresh_sha,
                "renewal_fresh_metadata_age_seconds": 0.0,
                "renewal_fresh_metadata_verifier_entry_at_unix": time.time(),
                "renewal_fresh_metadata_verifier_entry_age_seconds": 0.0,
                "renewal_subject": self.subject,
            }
            self.last_consumed_verifier_result = verified
            return verified

        real_recorder = vm._record_p1_freshness_start
        recorder_calls: list[tuple[object, dict[str, object]]] = []

        def observed_recorder(authority: object, **kwargs: object) -> dict[str, object]:
            recorder_calls.append((authority, dict(kwargs)))
            context = None
            if callable(recorder_hook):
                context = recorder_hook(authority, kwargs)
            if context is not None:
                with context:
                    return real_recorder(authority, **kwargs)
            return real_recorder(authority, **kwargs)

        def observed_open(path: object, *args: object, **kwargs: object) -> int:
            if isinstance(path, str) and path.startswith(".restore.ipsw.partial-"):
                if output_open_failure:
                    raise OSError("synthetic output open failure")
                current, _raw, _digest, _identity = vm._read_signed_record_snapshot(
                    self.marker_path, "vm_session_hmac_sha256")
                binding = current.get("p1_freshness_renewal")
                output_open_state.update({
                    "opened": True,
                    "state": current.get("state"),
                    "phase_id": current.get("phase_id"),
                    "use_state": binding.get("use_state") if isinstance(binding, dict) else None,
                    "attempt_id": binding.get("attempt_id") if isinstance(binding, dict) else None,
                    "body_start_evidence": (binding.get("body_start_evidence")
                                             if isinstance(binding, dict) else None),
                })
            opened_fd = real_os_open(path, *args, **kwargs)
            if isinstance(path, str) and path.startswith(".restore.ipsw.partial-"):
                output_open_state["fd"] = opened_fd
            return opened_fd

        real_os_write = os.write
        output_write_state: dict[str, object] = {"written": 0, "calls": 0, "chunks": []}

        def observed_write(descriptor: int, data: object) -> int:
            if descriptor == output_open_state.get("fd"):
                if stream_write_fail_after_bytes is not None:
                    remaining = stream_write_fail_after_bytes - int(output_write_state["written"])
                    if remaining <= 0:
                        raise OSError("synthetic output write failure after partial acceptance")
                    chunk = memoryview(data)[:remaining]
                else:
                    chunk = memoryview(data)
                count = real_os_write(descriptor, chunk)
                output_write_state["written"] = int(output_write_state["written"]) + count
                output_write_state["calls"] = int(output_write_state["calls"]) + 1
                cast_chunks = output_write_state["chunks"]
                assert isinstance(cast_chunks, list)
                cast_chunks.append(bytes(chunk[:count]))
                return count
            return real_os_write(descriptor, data)

        observation = lambda: _session_observation(
            vm_volume_uuid=self.volume_uuid, quota_bytes=vm.VM_REVIEWED_QUOTA_MAX_BYTES)
        real_volume_row = vm._volume_row
        with mock.patch.object(vm, "_verify_p1_download_authority", side_effect=authority_reader), \
                mock.patch.object(vm, "_verify_p1_freshness_renewal", side_effect=renewal_reader), \
                mock.patch.object(vm, "_check_p1_renewal_freshness_now", side_effect=check_freshness), \
                mock.patch.object(vm, "_renewal_live_boundary", side_effect=live_boundary), \
                mock.patch.object(vm, "_controller_identity", return_value=self.controller), \
                mock.patch.object(vm, "verify_fresh_g0c_review", return_value=self.g0c), \
                mock.patch.object(vm, "capture_host_observation", side_effect=observation), \
                mock.patch.object(vm, "_session_container_blockers", return_value=[]), \
                mock.patch.object(vm, "_volume_mount_info",
                                  return_value={"mounted": True, "mount_point": str(mount)}), \
                mock.patch.object(vm, "_volume_row", side_effect=real_volume_row), \
                mock.patch.object(vm, "conservative_session_unreserved_free_bytes",
                                  return_value=60 * vm.GIB), \
                mock.patch.object(vm, "_check_runtime_phase_sample",
                                  return_value={"blockers": []}), \
                mock.patch.object(vm, "_record_p1_freshness_start",
                                  side_effect=observed_recorder), \
                mock.patch.object(vm.os, "open", side_effect=observed_open), \
                mock.patch.object(vm.os, "write", side_effect=observed_write), \
                mock.patch.object(vm, "build_opener",
                                  return_value=mock.Mock(open=mock.Mock(return_value=response))):
            result = vm.download_and_verify_restore(
                self.p1_path, p1_review_path=self.root / "synthetic-review.json",
                receipt_path=self.root / "download-result.json",
                _renewal_monotonic_at_seal=1.0)
        self.last_output_open_state = output_open_state
        self.last_recorder_calls = recorder_calls
        self.last_output_write_state = output_write_state
        return result, response, mount, clock_checks

    def test_stale_first_body_chunk_is_discarded_without_write_or_hash(self) -> None:
        result, response, mount, _checks = self._run_synthetic_download(stale_after_read=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["restore_bytes_written"], 0)
        self.assertEqual(result["restore_sha256_partial"], hashlib.sha256(b"").hexdigest())
        self.assertEqual(response.read_calls, 1)
        self.assertEqual(list(mount.iterdir()), [])
        marker, _raw, _sha, _identity = vm._read_signed_record_snapshot(
            self.marker_path, "vm_session_hmac_sha256")
        evidence_ref = marker["p1_freshness_renewal"]["body_start_evidence"]
        evidence = vm._read_signed_record(Path(policy.REPO / evidence_ref["path"]),
                                           vm.VM_P1_FRESHNESS_START_SIGNATURE_FIELD)
        self.assertFalse(evidence["body_bytes_accepted"])

    def test_live_state_change_at_first_body_boundary_accepts_zero_bytes(self) -> None:
        result, response, mount, _checks = self._run_synthetic_download(live_state_changes=True)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["restore_bytes_written"], 0)
        self.assertEqual(response.read_calls, 1)
        self.assertEqual(list(mount.iterdir()), [])

    def test_http_status_mismatch_stops_before_body_read(self) -> None:
        result, response, mount, _checks = self._run_synthetic_download(status=206)
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["restore_bytes_written"], 0)
        self.assertEqual(response.read_calls, 0)
        self.assertEqual(list(mount.iterdir()), [])

    def test_content_length_mismatch_stops_before_body_read(self) -> None:
        result, response, mount, _checks = self._run_synthetic_download(content_length="2")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["restore_bytes_written"], 0)
        self.assertEqual(response.read_calls, 0)
        self.assertEqual(list(mount.iterdir()), [])

    def test_duplicate_content_length_stops_before_body_read(self) -> None:
        result, response, mount, _checks = self._run_synthetic_download(content_length="3, 3")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["restore_bytes_written"], 0)
        self.assertEqual(response.read_calls, 0)
        self.assertEqual(list(mount.iterdir()), [])

    def test_oversized_first_chunk_is_recorded_and_discarded(self) -> None:
        result, response, mount, _checks = self._run_synthetic_download(first_chunk=b"abcd")
        self.assertEqual(result["status"], "FAIL")
        self.assertEqual(result["restore_bytes_written"], 0)
        self.assertEqual(response.read_calls, 1)
        self.assertEqual(list(mount.iterdir()), [])
        marker, _raw, _sha, _identity = vm._read_signed_record_snapshot(
            self.marker_path, "vm_session_hmac_sha256")
        evidence = marker["p1_freshness_renewal"]["body_start_evidence"]
        record = vm._read_signed_record(Path(policy.REPO / evidence["path"]),
                                         vm.VM_P1_FRESHNESS_START_SIGNATURE_FIELD)
        self.assertFalse(record["body_bytes_accepted"])

    def test_transfer_can_finish_after_300_seconds_without_rechecking_age(self) -> None:
        result, response, _mount, checks = self._run_synthetic_download()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(response.read_calls, 2)
        self.assertEqual(checks, [0.0, 0.0, 0.0, 300.0])

    def test_start_event_and_marker_claim_are_durable_before_first_file_open(self) -> None:
        result, _response, _mount, _checks = self._run_synthetic_download()
        state = self.last_output_open_state
        self.assertEqual(result["status"], "PASS")
        self.assertTrue(state["opened"])
        self.assertEqual(state["state"], "ACTIVE")
        self.assertEqual(state["phase_id"], "P1")
        self.assertEqual(state["use_state"], "CONSUMED")
        self.assertIsInstance(state["body_start_evidence"], dict)

    def test_exact_one_hop_redirect_is_allowed_and_second_hop_rejected(self) -> None:
        handler = vm._AppleOnlyRedirectHandler(
            3, self.fresh["metadata_sha256"], self.fresh["source_url"], self.fresh["final_url"])
        request = vm.Request(self.fresh["source_url"])
        redirected = handler.redirect_request(request, None, 302, "redirect", {}, self.fresh["final_url"])
        self.assertIsNotNone(redirected)
        self.assertEqual(handler.redirect_count, 1)
        with self.assertRaisesRegex(RuntimeError, "one exact metadata-bound final URL"):
            handler.redirect_request(redirected, None, 302, "redirect", {}, self.fresh["final_url"])


class P1BodyStartRepairTests(unittest.TestCase):
    """Focused synthetic checks for the consumed-renewal/body-start repair."""

    def setUp(self) -> None:
        self.baseline_inventory = self._synthetic_artifact_inventory()
        self.fixtures: list[P1FreshnessRenewalTests] = []

    def tearDown(self) -> None:
        roots = [fixture.root for fixture in self.fixtures]
        for fixture in reversed(self.fixtures):
            fixture.doCleanups()
        for root in roots:
            self.assertFalse(root.exists(), f"synthetic test root remained after cleanup: {root}")
        self.assertEqual(self._synthetic_artifact_inventory(), self.baseline_inventory)

    @staticmethod
    def _synthetic_artifact_inventory() -> set[str]:
        return {
            item.name for item in policy.REPO.iterdir()
            if item.name.startswith(("p1-renewal-", "R6-VM-P1-FRESHNESS-START-",
                                    "R6-VM-P1-FRESHNESS-RENEWAL-",
                                    "R6-VM-RESTORE-VERIFICATION-"))
        }

    def _fixture(self) -> P1FreshnessRenewalTests:
        fixture = P1FreshnessRenewalTests(
            "test_active_p1_same_subject_fresh_observation_renews_successfully")
        fixture.setUp()
        self.fixtures.append(fixture)
        return fixture

    def _signed_failure(self, fixture: P1FreshnessRenewalTests,
                        result: dict[str, object]) -> dict[str, object]:
        receipt_path = fixture.root / "download-result.json"
        signed = vm._read_signed_record(
            receipt_path, "restore_verification_hmac_sha256")
        self.assertEqual(signed, result)
        self.assertTrue(vm.verify_signed_record(signed, "restore_verification_hmac_sha256"))
        evidence = result.get("failure_evidence")
        self.assertIsInstance(evidence, dict)
        self.assertEqual(evidence.get("schema_version"), 1)
        return evidence

    def test_historical_detached_consumed_renewal_is_passed_explicitly(self) -> None:
        fixture = self._fixture()
        result, _response, mount, _checks = fixture._run_synthetic_download()
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(fixture.last_recorder_calls), 1)
        base_authority, recorder_args = fixture.last_recorder_calls[0]
        self.assertNotIn("renewal", base_authority)
        consumed = fixture.last_consumed_verifier_result
        self.assertIs(recorder_args["renewal"], consumed["renewal"])
        self.assertEqual(recorder_args["renewal_path"], consumed["renewal_path"])
        self.assertEqual(recorder_args["renewal_path_text"], consumed["renewal_path_text"])
        self.assertEqual(recorder_args["renewal_sha256"], consumed["renewal_sha256"])
        self.assertEqual(recorder_args["renewal_binding"], consumed["renewal_binding"])
        self.assertEqual(recorder_args["renewal_subject"], consumed["renewal_subject"])
        self.assertTrue(fixture.last_output_open_state["opened"])
        self.assertIsInstance(fixture.last_output_open_state["body_start_evidence"], dict)
        restore_path = mount / "restore.ipsw"
        self.assertEqual(restore_path.read_bytes(), b"abc")
        self.assertEqual(result["local_sha256"], hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(result["actual_length_bytes"], 3)
        self.assertEqual(fixture.last_output_write_state["calls"], 1)
        self.assertEqual(fixture.last_output_write_state["written"], 3)
        self.assertEqual(fixture.last_output_write_state["chunks"], [b"abc"])

    def test_recorder_failure_is_signed_with_the_historical_zero_byte_stage_pair(self) -> None:
        fixture = self._fixture()

        def omit_consumed_renewal(_authority: object, args: dict[str, object]) -> None:
            args["renewal"] = None

        result, response, mount, _checks = fixture._run_synthetic_download(
            recorder_hook=omit_consumed_renewal)
        evidence = self._signed_failure(fixture, result)
        boundary = evidence["response_boundary"]
        self.assertEqual(evidence["stage"], "FIRST_BODY_RETURNED_UNACCEPTED")
        self.assertEqual(evidence["failure_stage"], "START_EVIDENCE_FAILED")
        self.assertEqual(boundary["http_status"], 200)
        self.assertEqual(boundary["raw_final_url"], fixture.fresh["final_url"])
        self.assertEqual(boundary["raw_content_length_values"], ["3"])
        self.assertEqual(boundary["first_body_chunk_length_bytes"], 3)
        self.assertIsInstance(boundary["first_body_read_at_unix"], (int, float))
        self.assertFalse(evidence["first_body_bytes_accepted"])
        self.assertFalse(evidence["start_evidence_created"])
        self.assertEqual(evidence["start_evidence_creation_state"], "NOT_ATTEMPTED")
        self.assertEqual(result["restore_bytes_written"], 0)
        self.assertEqual(result["restore_sha256_partial"], hashlib.sha256(b"").hexdigest())
        self.assertIsNone(result["partial_path"])
        self.assertEqual(response.read_calls, 1)
        self.assertEqual(list(mount.iterdir()), [])
        self.assertNotEqual(evidence["renewal"]["path"], "unknown")
        self.assertNotEqual(evidence["renewal"]["sequence"], "unknown")
        self.assertEqual(evidence["metadata_ages"]["after_headers"]["check_result"], "PASS")
        self.assertEqual(evidence["metadata_ages"][
            "immediately_after_first_nonempty_read"]["check_result"], "PASS")

    def test_consumed_renewal_authority_substitution_matrix_fails_before_evidence_write(self) -> None:
        def missing_renewal(_authority: object, args: dict[str, object]) -> None:
            args["renewal"] = None

        def wrong_path(_authority: object, args: dict[str, object]) -> None:
            args["renewal_path_text"] = "experiments/storage/other-renewal.json"

        def wrong_hash(_authority: object, args: dict[str, object]) -> None:
            args["renewal_sha256"] = "0" * 64

        def wrong_sequence(_authority: object, args: dict[str, object]) -> None:
            args["renewal_binding"]["sequence"] = 2

        def wrong_predecessor(_authority: object, args: dict[str, object]) -> None:
            args["renewal_binding"]["predecessor_path"] = "experiments/storage/prior.json"

        def wrong_renewal_object(_authority: object, args: dict[str, object]) -> None:
            args["renewal"] = {**args["renewal"], "sequence": 2}

        def wrong_p1(_authority: dict[str, object], _args: dict[str, object]) -> None:
            _authority["p1_sha256"] = "0" * 64

        def wrong_review(_authority: dict[str, object], _args: dict[str, object]) -> None:
            _authority["p1_review_sha256"] = "0" * 64

        def wrong_review_path(_authority: dict[str, object], _args: dict[str, object]) -> None:
            _authority["p1_review_path_text"] = "experiments/storage/other-review.json"

        def wrong_session(_authority: dict[str, object], _args: dict[str, object]) -> None:
            _authority["admission"]["session_id"] = "f" * 32

        def wrong_volume(_authority: dict[str, object], _args: dict[str, object]) -> None:
            _authority["admission"]["vm_volume_uuid"] = "22222222-2222-2222-2222-222222222222"

        def wrong_attempt(_authority: object, args: dict[str, object]) -> None:
            args["attempt_id"] = "e" * 32

        def ready_state(_authority: object, args: dict[str, object]) -> None:
            args["renewal_binding"]["use_state"] = "READY"

        def prior_attempt(_authority: object, args: dict[str, object]) -> None:
            args["renewal_binding"]["attempt_id"] = "d" * 32

        def wrong_subject(_authority: object, args: dict[str, object]) -> None:
            args["renewal_subject"] = {"different": "restore subject"}

        def wrong_metadata_path(_authority: object, args: dict[str, object]) -> None:
            args["fresh_metadata_path_text"] = "experiments/storage/other-metadata.json"

        def wrong_metadata_hash(_authority: object, args: dict[str, object]) -> None:
            args["fresh_metadata_receipt_sha256"] = "0" * 64

        def wrong_metadata_digest(_authority: object, args: dict[str, object]) -> None:
            args["fresh_metadata"]["metadata_sha256"] = "0" * 64

        def changed_controller(_authority: dict[str, object], _args: dict[str, object]) -> None:
            _authority["current_controller_identity"] = {"controller_sha256": "0" * 64}

        def changed_g0c(_authority: dict[str, object], _args: dict[str, object]) -> None:
            _authority["g0c_identity"] = {"snapshot_sha256": "0" * 64}

        def live_storage_mismatch(_authority: object, args: dict[str, object]) -> None:
            args["live_storage"]["session_id"] = "f" * 32

        def marker_drift(authority: dict[str, object], _args: dict[str, object]) -> None:
            marker, _raw, digest, _identity = vm._read_signed_record_snapshot(
                authority["marker_path"] if "marker_path" in authority else vm.VM_ACTIVE_MARKER_PATH,
                "vm_session_hmac_sha256")
            vm.update_active_session_marker(
                marker["session_id"], state="FAILED", expected_marker_sha256=digest)

        cases = [
            ("missing renewal", missing_renewal, {}),
            ("wrong renewal path", wrong_path, {}),
            ("wrong renewal hash", wrong_hash, {}),
            ("wrong renewal object", wrong_renewal_object, {}),
            ("wrong sequence", wrong_sequence, {}),
            ("wrong predecessor", wrong_predecessor, {}),
            ("wrong P1 admission", wrong_p1, {}),
            ("wrong P1 review", wrong_review, {}),
            ("wrong review path", wrong_review_path, {}),
            ("wrong session", wrong_session, {}),
            ("wrong VM volume", wrong_volume, {}),
            ("wrong attempt", wrong_attempt, {}),
            ("READY instead of CONSUMED", ready_state, {}),
            ("previous attempt", prior_attempt, {}),
            ("different stable subject", wrong_subject, {}),
            ("wrong metadata path", wrong_metadata_path, {}),
            ("wrong metadata receipt hash", wrong_metadata_hash, {}),
            ("wrong metadata observation digest", wrong_metadata_digest, {}),
            ("changed controller", changed_controller, {}),
            ("changed current G0C", changed_g0c, {}),
            ("mismatched live storage", live_storage_mismatch, {}),
            ("marker drift", marker_drift, {}),
        ]
        for label, hook, options in cases:
            with self.subTest(case=label):
                fixture = self._fixture()
                result, response, mount, _checks = fixture._run_synthetic_download(
                    recorder_hook=hook, **options)
                evidence = self._signed_failure(fixture, result)
                self.assertEqual(evidence["stage"], "FIRST_BODY_RETURNED_UNACCEPTED")
                self.assertEqual(evidence["failure_stage"], "START_EVIDENCE_FAILED")
                self.assertEqual(evidence["start_evidence_creation_state"], "NOT_ATTEMPTED")
                self.assertFalse(evidence["start_evidence_created"])
                self.assertEqual(result["restore_bytes_written"], 0)
                self.assertEqual(result["restore_sha256_partial"], hashlib.sha256(b"").hexdigest())
                self.assertIsNone(result["partial_path"])
                self.assertEqual(response.read_calls, 1)
                self.assertEqual(list(mount.iterdir()), [])

    def test_signed_response_boundary_receipts_keep_observed_values_and_unknowns(self) -> None:
        cases = [
            ("invalid status", {"status": 206}, 0, "RESPONSE_HEADERS_VALIDATION"),
            ("wrong final URL", {"final_url": "https://updates.cdn-apple.com/restore/other.ipsw"},
             0, "RESPONSE_HEADERS_VALIDATION"),
            ("wrong Content-Length", {"content_length": "2"}, 0, "RESPONSE_HEADERS_VALIDATION"),
            ("duplicate Content-Length", {"content_length_values": ["3", "3"]},
             0, "RESPONSE_HEADERS_VALIDATION"),
        ]
        for label, options, expected_reads, expected_failure_stage in cases:
            with self.subTest(case=label):
                fixture = self._fixture()
                result, response, mount, _checks = fixture._run_synthetic_download(**options)
                evidence = self._signed_failure(fixture, result)
                boundary = evidence["response_boundary"]
                self.assertEqual(evidence["stage"], "RESPONSE_HEADERS_RECEIVED")
                self.assertEqual(evidence["failure_stage"], expected_failure_stage)
                self.assertEqual(response.read_calls, expected_reads)
                self.assertEqual(result["restore_bytes_written"], 0)
                self.assertEqual(result["restore_sha256_partial"], hashlib.sha256(b"").hexdigest())
                self.assertIsNone(result["partial_path"])
                self.assertEqual(boundary["http_status"], options.get("status", 200))
                self.assertNotEqual(boundary["raw_final_url"], "unknown")
                self.assertNotEqual(boundary["raw_content_length_values"], "unknown")
                self.assertEqual(boundary["first_body_read_at_unix"], "unknown")
                self.assertEqual(boundary["first_body_chunk_length_bytes"], "unknown")
                self.assertEqual(list(mount.iterdir()), [])

    def test_signed_stale_and_live_storage_failure_receipts_preserve_first_read(self) -> None:
        cases = [
            ("stale before read", {"stale_before_read": True}, "FRESHNESS_AFTER_HEADERS", 0),
            ("stale after read", {"stale_after_read": True}, "POST_READ_VALIDATION", 1),
            ("live storage fail", {"live_state_changes": True}, "POST_READ_VALIDATION", 1),
        ]
        for label, options, failure_stage, reads in cases:
            with self.subTest(case=label):
                fixture = self._fixture()
                result, response, mount, _checks = fixture._run_synthetic_download(**options)
                evidence = self._signed_failure(fixture, result)
                self.assertEqual(evidence["failure_stage"], failure_stage)
                self.assertEqual(response.read_calls, reads)
                self.assertEqual(result["restore_bytes_written"], 0)
                self.assertEqual(result["restore_sha256_partial"], hashlib.sha256(b"").hexdigest())
                self.assertIsNone(result["partial_path"])
                if reads:
                    boundary = evidence["response_boundary"]
                    self.assertEqual(boundary["first_body_chunk_length_bytes"], 3)
                    self.assertIsInstance(boundary["first_body_read_at_unix"], (int, float))
                    self.assertFalse(evidence["first_body_bytes_accepted"])
                else:
                    self.assertEqual(evidence["response_boundary"][
                        "first_body_chunk_length_bytes"], "unknown")
                self.assertEqual(list(mount.iterdir()), [])

    def test_start_receipt_write_readback_and_marker_cas_failures_are_distinguished(self) -> None:
        def fail_unique_write(_authority: object, _args: dict[str, object]) -> object:
            return mock.patch.object(
                vm, "_write_signed_json_unique",
                side_effect=OSError("synthetic unique start-receipt write failure"))

        def fail_start_readback(_authority: object, _args: dict[str, object]) -> object:
            real_read = vm._read_signed_record_snapshot

            def read_or_fail(path: object, *args: object, **kwargs: object):
                if Path(path).name.startswith(vm.VM_P1_FRESHNESS_START_PREFIX):
                    raise RuntimeError("synthetic signed start-receipt read-back failure")
                return real_read(path, *args, **kwargs)

            return mock.patch.object(vm, "_read_signed_record_snapshot", side_effect=read_or_fail)

        def fail_marker_cas(_authority: object, _args: dict[str, object]) -> object:
            real_update = vm.update_active_session_marker

            def update_or_fail(session_id: str, **kwargs: object):
                binding = kwargs.get("p1_freshness_renewal")
                if isinstance(binding, dict) and binding.get("body_start_evidence") is not None:
                    raise RuntimeError("synthetic first-body marker CAS failure")
                return real_update(session_id, **kwargs)

            return mock.patch.object(vm, "update_active_session_marker", side_effect=update_or_fail)

        cases = [
            ("unique write", fail_unique_write, "NOT_CREATED", False),
            ("signed read-back", fail_start_readback, "READBACK_FAILED_UNBOUND", True),
            ("marker CAS", fail_marker_cas, "MARKER_CAS_ATTEMPTED", True),
        ]
        for label, hook, state, created in cases:
            with self.subTest(case=label):
                fixture = self._fixture()
                result, response, mount, _checks = fixture._run_synthetic_download(
                    recorder_hook=hook)
                evidence = self._signed_failure(fixture, result)
                self.assertEqual(evidence["stage"], "FIRST_BODY_RETURNED_UNACCEPTED")
                self.assertEqual(evidence["failure_stage"], "START_EVIDENCE_FAILED")
                self.assertEqual(evidence["start_evidence_creation_state"], state)
                self.assertEqual(evidence["start_evidence_created"], created)
                self.assertFalse(evidence["start_evidence_marker_bound"])
                self.assertEqual(result["restore_bytes_written"], 0)
                self.assertEqual(result["restore_sha256_partial"], hashlib.sha256(b"").hexdigest())
                self.assertIsNone(result["partial_path"])
                self.assertEqual(response.read_calls, 1)
                self.assertEqual(list(mount.iterdir()), [])

    def test_output_open_and_stream_failures_report_exact_partial_file_state(self) -> None:
        fixture = self._fixture()
        result, response, mount, _checks = fixture._run_synthetic_download(
            output_open_failure=True)
        evidence = self._signed_failure(fixture, result)
        self.assertEqual(evidence["stage"], "FIRST_BODY_ACCEPTED")
        self.assertEqual(evidence["failure_stage"], "OUTPUT_OPEN")
        self.assertTrue(evidence["start_evidence_created"])
        self.assertTrue(evidence["start_evidence_marker_bound"])
        self.assertEqual(result["restore_bytes_written"], 0)
        self.assertEqual(result["restore_sha256_partial"], hashlib.sha256(b"").hexdigest())
        self.assertIsNone(result["partial_path"])
        self.assertEqual(evidence["output_file_presence"], {"temporary": False, "restore": False})
        self.assertEqual(response.read_calls, 1)
        self.assertEqual(list(mount.iterdir()), [])

        fixture = self._fixture()
        result, response, mount, _checks = fixture._run_synthetic_download(
            stream_read_failure_after_first=True)
        evidence = self._signed_failure(fixture, result)
        self.assertEqual(evidence["stage"], "STREAMING")
        self.assertEqual(evidence["failure_stage"], "STREAM_READ")
        self.assertEqual(result["restore_bytes_written"], 3)
        self.assertEqual(result["restore_sha256_partial"], hashlib.sha256(b"abc").hexdigest())
        self.assertEqual(Path(result["partial_path"]).read_bytes(), b"abc")
        self.assertEqual(response.read_calls, 2)
        self.assertEqual(len(list(mount.iterdir())), 1)

        fixture = self._fixture()
        result, response, mount, _checks = fixture._run_synthetic_download(
            stream_write_fail_after_bytes=1)
        evidence = self._signed_failure(fixture, result)
        self.assertEqual(evidence["stage"], "STREAMING")
        self.assertEqual(evidence["failure_stage"], "STREAM_WRITE")
        self.assertEqual(result["restore_bytes_written"], 1)
        self.assertEqual(result["restore_sha256_partial"], hashlib.sha256(b"a").hexdigest())
        self.assertEqual(Path(result["partial_path"]).read_bytes(), b"a")
        self.assertEqual(response.read_calls, 1)
        self.assertEqual(len(list(mount.iterdir())), 1)


class DeferredRestoreRecoveryTests(unittest.TestCase):
    """Synthetic-only tests for the additive R13 pending-restore recovery path."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="deferred-restore-recovery-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def _recovery_identity(self, path: Path) -> dict[str, object]:
        os.chmod(path, 0o600)
        return vm._deferred_restore_recovery_file_identity(path.stat())

    def test_real_old_pending_ancestry_matches_anchors_without_payload_access_or_mutation(self) -> None:
        pending, _raw, _digest, _identity = vm._read_signed_record_snapshot(
            vm.VM_R13_PENDING_RECEIPT_PATH,
            vm.VM_DEFERRED_RESTORE_PENDING_SIGNATURE_FIELD)
        branch_receipt_path = policy.REPO / pending["branch_retirement_receipt_path"]
        preserved_evidence_paths = (
            vm.VM_R13_PENDING_RECEIPT_PATH,
            vm.VM_R13_OLD_AUTHORIZATION_PATH,
            vm.VM_R13_OLD_PREDELETE_REVIEW_PATH,
            branch_receipt_path,
            vm.VM_R13_R12_IDENTITY_REVIEW_PATH,
            vm.VM_R13_R12_QUARANTINE_REVIEW_PATH,
        )
        before = {path: path.read_bytes() for path in preserved_evidence_paths}
        with ExitStack() as mutations:
            for operation in ("unlink", "remove", "rename", "replace", "mkdir",
                              "makedirs", "rmdir", "chmod", "chown", "truncate"):
                if hasattr(os, operation):
                    mutations.enter_context(mock.patch.object(
                        os, operation,
                        side_effect=AssertionError("historical ancestry check must not mutate state")))
            evidence = vm._deferred_restore_recovery_ancestry()
        self.assertEqual(evidence["pending_receipt_sha256"], vm.VM_R13_PENDING_RECEIPT_SHA256)
        self.assertEqual(evidence["old_authorization_sha256"],
                         vm.VM_R13_OLD_AUTHORIZATION_SHA256)
        self.assertEqual(evidence["old_predelete_review_sha256"],
                         vm.VM_R13_OLD_PREDELETE_REVIEW_SHA256)
        self.assertEqual(evidence["r11_failure_sha256"], vm.VM_R13_R11_FAILURE_SHA256)
        self.assertEqual(evidence["r11_failure_anchor_source_path"],
                         vm.VM_R13_R12_QUARANTINE_REVIEW_PATH.relative_to(policy.REPO).as_posix())
        self.assertEqual(evidence["expected_quarantine_path"],
                         "/Volumes/FGMetal-VM/" + vm.VM_R13_EXPECTED_QUARANTINE_NAME)
        self.assertEqual({path: path.read_bytes() for path in preserved_evidence_paths}, before)

    def test_real_old_pending_ancestry_does_not_read_the_vanished_r11_tmp_file(self) -> None:
        r11_path = vm.VM_R13_R11_FAILURE_PATH
        original_r11_exists = r11_path.exists()
        original_read_bytes = Path.read_bytes
        original_path_open = Path.open
        original_builtin_open = builtins.open
        original_io_open = io.open
        original_os_open = os.open
        original_stat = os.stat
        original_lstat = os.lstat
        original_read_file_nofollow = policy._read_file_nofollow

        def reject_r11_path(path, *args, **kwargs) -> None:
            if isinstance(path, int):
                return
            candidate = Path(os.fsdecode(os.fspath(path)))
            if candidate == r11_path or candidate.name == r11_path.name:
                raise AssertionError("the vanished R11 /tmp artifact must not be accessed")

        def guarded_read_bytes(path: Path) -> bytes:
            reject_r11_path(path)
            return original_read_bytes(path)

        def guarded_path_open(path: Path, *args, **kwargs):
            reject_r11_path(path)
            return original_path_open(path, *args, **kwargs)

        def guarded_builtin_open(path, *args, **kwargs):
            reject_r11_path(path)
            return original_builtin_open(path, *args, **kwargs)

        def guarded_io_open(path, *args, **kwargs):
            reject_r11_path(path)
            return original_io_open(path, *args, **kwargs)

        def guarded_os_open(path, *args, **kwargs):
            reject_r11_path(path)
            return original_os_open(path, *args, **kwargs)

        def guarded_stat(path, *args, **kwargs):
            reject_r11_path(path)
            return original_stat(path, *args, **kwargs)

        def guarded_lstat(path, *args, **kwargs):
            reject_r11_path(path)
            return original_lstat(path, *args, **kwargs)

        def guarded_read_file_nofollow(path, *args, **kwargs):
            reject_r11_path(path)
            return original_read_file_nofollow(path, *args, **kwargs)

        with mock.patch.object(Path, "read_bytes", new=guarded_read_bytes), \
                mock.patch.object(Path, "open", new=guarded_path_open), \
                mock.patch.object(builtins, "open", new=guarded_builtin_open), \
                mock.patch.object(io, "open", new=guarded_io_open), \
                mock.patch.object(os, "open", new=guarded_os_open), \
                mock.patch.object(os, "stat", new=guarded_stat), \
                mock.patch.object(os, "lstat", new=guarded_lstat), \
                mock.patch.object(policy, "_read_file_nofollow", new=guarded_read_file_nofollow):
            evidence = vm._deferred_restore_recovery_ancestry()
        self.assertEqual(evidence["r11_failure_sha256"], vm.VM_R13_R11_FAILURE_SHA256)
        self.assertEqual(r11_path.exists(), original_r11_exists)

    def test_missing_preserved_r11_anchor_evidence_fails_closed(self) -> None:
        missing_review = self.base / "missing-r12-quarantine-review.md"
        with mock.patch.object(vm, "VM_R13_R12_QUARANTINE_REVIEW_PATH", missing_review):
            with self.assertRaisesRegex(
                    RuntimeError, "R12 quarantine identity provenance evidence is unavailable"):
                vm._deferred_restore_recovery_ancestry()

    def test_tampered_preserved_r11_anchor_evidence_fails_closed(self) -> None:
        source = vm.VM_R13_R12_QUARANTINE_REVIEW_PATH.read_bytes()
        tampered = self.base / "tampered-r12-quarantine-review.md"
        tampered.write_bytes(source.replace(b'"status":"FAIL"', b'"status":"PASS"', 1))
        with mock.patch.object(vm, "VM_R13_R12_QUARANTINE_REVIEW_PATH", tampered):
            with self.assertRaisesRegex(RuntimeError, "R12 quarantine identity review evidence changed"):
                vm._deferred_restore_recovery_ancestry()

    def test_wrong_pending_or_r11_digest_anchor_fails_closed(self) -> None:
        with mock.patch.object(vm, "VM_R13_PENDING_RECEIPT_SHA256", "0" * 64):
            with self.assertRaisesRegex(RuntimeError, "immutable old PENDING/authentication ancestry"):
                vm._deferred_restore_recovery_ancestry()
        with mock.patch.object(vm, "VM_R13_R11_FAILURE_SHA256", "0" * 64):
            with self.assertRaisesRegex(RuntimeError, "R11 failure evidence differs from the preserved provenance anchor"):
                vm._deferred_restore_recovery_ancestry()

    def test_real_old_pending_ancestry_never_accesses_historical_payload(self) -> None:
        payload_paths = {
            Path(vm.VM_DEFERRED_RESTORE_PATH),
            Path(vm.VM_DEFERRED_RESTORE_PATH.parent / vm.VM_R13_EXPECTED_QUARANTINE_NAME),
        }
        payload_names = {path.name for path in payload_paths}
        original_path_open = Path.open
        original_builtin_open = builtins.open
        original_io_open = io.open
        original_os_open = os.open
        original_read_file_nofollow = policy._read_file_nofollow
        original_stat = os.stat
        original_lstat = os.lstat

        def reject_payload_path(path, *args, **kwargs):
            if isinstance(path, int):
                return
            candidate = Path(os.fsdecode(os.fspath(path)))
            if candidate in payload_paths or candidate.name in payload_names:
                raise AssertionError("historical restore payload path must not be accessed")

        def guarded_path_open(path, *args, **kwargs):
            reject_payload_path(path)
            return original_path_open(path, *args, **kwargs)

        def guarded_builtin_open(path, *args, **kwargs):
            reject_payload_path(path)
            return original_builtin_open(path, *args, **kwargs)

        def guarded_io_open(path, *args, **kwargs):
            reject_payload_path(path)
            return original_io_open(path, *args, **kwargs)

        def guarded_os_open(path, *args, **kwargs):
            reject_payload_path(path)
            return original_os_open(path, *args, **kwargs)

        def guarded_read_file_nofollow(path, *args, **kwargs):
            reject_payload_path(path)
            return original_read_file_nofollow(path, *args, **kwargs)

        def guarded_stat(path, *args, **kwargs):
            reject_payload_path(path)
            return original_stat(path, *args, **kwargs)

        def guarded_lstat(path, *args, **kwargs):
            reject_payload_path(path)
            return original_lstat(path, *args, **kwargs)

        with mock.patch.object(Path, "open", new=guarded_path_open), \
                mock.patch.object(builtins, "open", new=guarded_builtin_open), \
                mock.patch.object(io, "open", new=guarded_io_open), \
                mock.patch.object(os, "open", new=guarded_os_open), \
                mock.patch.object(policy, "_read_file_nofollow", new=guarded_read_file_nofollow), \
                mock.patch.object(os, "stat", new=guarded_stat), \
                mock.patch.object(os, "lstat", new=guarded_lstat):
            evidence = vm._deferred_restore_recovery_ancestry()
        self.assertEqual(evidence["r11_failure_sha256"], vm.VM_R13_R11_FAILURE_SHA256)

    def test_consumed_old_authorization_is_not_a_recovery_authority(self) -> None:
        old, _raw, _digest, _identity = vm._read_signed_record_snapshot(
            vm.VM_R13_OLD_AUTHORIZATION_PATH,
            vm.VM_DEFERRED_RESTORE_AUTHORIZATION_SIGNATURE_FIELD)
        with self.assertRaisesRegex(RuntimeError, "recovery authorization"):
            vm._assert_deferred_restore_recovery_authorization(old)

    def test_canonical_restore_reappearance_fails_closed(self) -> None:
        canonical = self.base / "restore.ipsw"
        quarantine = self.base / vm.VM_R13_EXPECTED_QUARANTINE_NAME
        canonical.write_bytes(b"unexpected canonical")
        quarantine.write_bytes(b"quarantine")
        root_fd = os.open(self.base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        with self.assertRaisesRegex(RuntimeError, "canonical restore path unexpectedly reappeared"):
            vm._assert_deferred_restore_recovery_namespace(
                root_fd, vm.VM_R13_EXPECTED_QUARANTINE_NAME)

    def test_multiple_quarantine_candidates_fail_closed(self) -> None:
        first = self.base / vm.VM_R13_EXPECTED_QUARANTINE_NAME
        second = self.base / (vm.VM_R13_EXPECTED_QUARANTINE_NAME + ".second")
        first.write_bytes(b"first")
        second.write_bytes(b"second")
        root_fd = os.open(self.base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        with self.assertRaisesRegex(RuntimeError, "namespace is ambiguous"):
            vm._assert_deferred_restore_recovery_namespace(
                root_fd, vm.VM_R13_EXPECTED_QUARANTINE_NAME)

    def test_quarantine_symlink_fails_no_follow_regular_file_check(self) -> None:
        target = self.base / "target"
        target.write_bytes(b"target")
        quarantine = self.base / vm.VM_R13_EXPECTED_QUARANTINE_NAME
        quarantine.symlink_to(target)
        root_fd = os.open(self.base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        with self.assertRaisesRegex(RuntimeError, "not a regular file|link count"):
            vm._deferred_restore_recovery_file_identity_at(
                root_fd, vm.VM_R13_EXPECTED_QUARANTINE_NAME)

    def test_quarantine_directory_and_fifo_are_rejected_as_nonregular(self) -> None:
        directory = self.base / "directory"
        directory.mkdir()
        root_fd = os.open(self.base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, root_fd)
        with self.assertRaisesRegex(RuntimeError, "not a regular file"):
            vm._deferred_restore_recovery_file_identity_at(root_fd, "directory")
        fifo = self.base / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaisesRegex(RuntimeError, "not a regular file"):
            vm._deferred_restore_recovery_file_identity_at(root_fd, "fifo")

    def test_device_and_inode_mismatch_are_rejected(self) -> None:
        baseline = {
            "device": 11, "inode": 12, "logical_bytes": 13, "allocated_bytes": 14,
            "mtime_ns": 15, "ctime_ns": 16, "owner_uid": 17, "owner_gid": 18,
            "mode": 0o600, "link_count": 1, "birthtime_ns": 19,
            "file_type": "regular",
        }
        for field in ("device", "inode"):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, field):
                vm._assert_deferred_restore_recovery_identity(
                    baseline, {**baseline, field: baseline[field] + 1})

    def test_size_mismatch_is_rejected(self) -> None:
        baseline = {
            "device": 11, "inode": 12, "logical_bytes": 13, "allocated_bytes": 14,
            "mtime_ns": 15, "ctime_ns": 16, "owner_uid": 17, "owner_gid": 18,
            "mode": 0o600, "link_count": 1, "birthtime_ns": 19,
            "file_type": "regular",
        }
        with self.assertRaisesRegex(RuntimeError, "logical_bytes"):
            vm._assert_deferred_restore_recovery_identity(
                baseline, {**baseline, "logical_bytes": 14})

    def test_mtime_and_allocated_size_stability_mismatches_are_rejected(self) -> None:
        baseline = {
            "device": 11, "inode": 12, "logical_bytes": 13, "allocated_bytes": 14,
            "mtime_ns": 15, "ctime_ns": 16, "owner_uid": 17, "owner_gid": 18,
            "mode": 0o600, "link_count": 1, "birthtime_ns": 19,
            "file_type": "regular",
        }
        for field in ("mtime_ns", "allocated_bytes"):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, field):
                vm._assert_deferred_restore_recovery_identity(
                    baseline, {**baseline, field: baseline[field] + 1})

    def test_owner_mode_and_link_count_mismatches_are_rejected(self) -> None:
        baseline = {
            "device": 11, "inode": 12, "logical_bytes": 13, "allocated_bytes": 14,
            "mtime_ns": 15, "ctime_ns": 16, "owner_uid": 17, "owner_gid": 18,
            "mode": 0o600, "link_count": 1, "birthtime_ns": 19,
            "file_type": "regular",
        }
        for field in ("owner_uid", "owner_gid", "mode", "link_count"):
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, field):
                vm._assert_deferred_restore_recovery_identity(
                    baseline, {**baseline, field: baseline[field] + 1})

    def test_birthtime_mismatch_or_unavailability_is_rejected(self) -> None:
        baseline = {
            "device": 11, "inode": 12, "logical_bytes": 13, "allocated_bytes": 14,
            "mtime_ns": 15, "ctime_ns": 16, "owner_uid": 17, "owner_gid": 18,
            "mode": 0o600, "link_count": 1, "birthtime_ns": 19,
            "file_type": "regular",
        }
        with self.assertRaisesRegex(RuntimeError, "birth time"):
            vm._assert_deferred_restore_recovery_identity(
                baseline, {**baseline, "birthtime_ns": 19 + vm.VM_R13_BIRTHTIME_TOLERANCE_NS + 1})
        with self.assertRaisesRegex(RuntimeError, "birth time"):
            vm._assert_deferred_restore_recovery_identity(
                baseline, {**baseline, "birthtime_ns": None})

    def test_pre_rename_ctime_is_not_compared_but_stable_phase_ctime_is(self) -> None:
        baseline = {
            "device": 11, "inode": 12, "logical_bytes": 13, "allocated_bytes": 14,
            "mtime_ns": 15, "ctime_ns": 16, "owner_uid": 17, "owner_gid": 18,
            "mode": 0o600, "link_count": 1, "birthtime_ns": 19,
            "file_type": "regular",
        }
        vm._assert_deferred_restore_recovery_identity(
            baseline, {**baseline, "ctime_ns": 99}, compare_ctime=False)
        with self.assertRaisesRegex(RuntimeError, "ctime"):
            vm._assert_deferred_restore_recovery_identity(
                baseline, {**baseline, "ctime_ns": 99})

    def test_current_digest_mismatch_and_wrong_exact_byte_count_fail(self) -> None:
        payload = b"synthetic restore payload"
        quarantine = self.base / vm.VM_R13_EXPECTED_QUARANTINE_NAME
        quarantine.write_bytes(payload)
        os.chmod(quarantine, 0o600)
        root_fd = os.open(self.base, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        descriptor = os.open(quarantine.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                             dir_fd=root_fd)
        self.addCleanup(os.close, descriptor)
        self.addCleanup(os.close, root_fd)
        identity = vm._deferred_restore_recovery_file_identity(os.fstat(descriptor))
        for digest, byte_count in (("0" * 64, len(payload)),
                                   (hashlib.sha256(payload).hexdigest(), len(payload) + 1)):
            os.lseek(descriptor, 0, os.SEEK_SET)
            with self.subTest(digest=digest, byte_count=byte_count), self.assertRaises(RuntimeError):
                vm._hash_deferred_restore_recovery_descriptor(
                    root_fd, quarantine.name, descriptor,
                    expected_identity=identity, expected_sha256=digest,
                    expected_bytes=byte_count)

    def test_process_scan_blocker_fails(self) -> None:
        with mock.patch.object(vm, "_deferred_process_scan", return_value={
                "scan_status": "BLOCKED", "blocking_processes": [{"pid": 99}]}):
            with self.assertRaisesRegex(RuntimeError, "process scan blocks"):
                vm._deferred_recovery_require_process_scan()

    def test_descriptor_scan_blocker_fails(self) -> None:
        import p2_base_orchestrator as p2
        evidence = {"scan_clear": False, "open_file_descriptors": [{"files": [{"fd": "7"}]}],
                    "exact_target_descriptor_matches": [],
                    "descriptor_identity_mismatches": [], "open_process_ids": ["99"]}
        with mock.patch.object(p2, "_descriptor_scan_evidence", return_value=evidence):
            with self.assertRaisesRegex(RuntimeError, "another process or descriptor"):
                vm._deferred_recovery_require_descriptor_scan([self.base / "restore.ipsw"])

    def test_missing_fresh_recovery_review_blocks_new_authorization(self) -> None:
        repo = self.base / "repo"
        (repo / "experiments/storage").mkdir(parents=True)
        tests_path = repo / "experiments/storage/test_vm_capacity_domain.py"
        tests_path.write_bytes(b"synthetic tests")
        validation_path = repo / "validation.json"
        validation_path.write_bytes(b"synthetic validation")
        controller_identity = vm._controller_identity()
        path_review_path = repo / "path-review.json"
        path_review_path.write_bytes(b"synthetic path review")
        path_review_ref = {"path": "path-review.json", "sha256": "a" * 64}
        validation = {
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_VALIDATION",
            "status": "PASS_VALIDATED", "created_at_unix": time.time(),
            "current_sha256": vm.VM_DEFERRED_RESTORE_SHA256,
            "current_byte_count": vm.VM_DEFERRED_RESTORE_LOGICAL_BYTES,
            "current_controller_identity": controller_identity,
            "current_tests_sha256": hashlib.sha256(tests_path.read_bytes()).hexdigest(),
            "path_review_path": "path-review.json", "path_review_sha256": "a" * 64,
            "pending_receipt_sha256": "1" * 64,
            "old_authorization_sha256": "2" * 64,
            "old_predelete_review_sha256": "3" * 64,
            "r11_failure_sha256": "4" * 64,
            "r12_identity_contract_review_sha256": "5" * 64,
            "r12_quarantine_identity_review_sha256": "6" * 64,
            "expected_quarantine_path": "/synthetic/quarantine",
            "expected_quarantine_name": vm.VM_R13_EXPECTED_QUARANTINE_NAME,
            "quarantine_path": "/synthetic/quarantine",
            "quarantine_file_identity": dict(vm.VM_R13_EXPECTED_QUARANTINE_IDENTITY),
            "vm_volume_root_identity": dict(vm.VM_R13_EXPECTED_ROOT_IDENTITY),
            "namespace_state": {
                "canonical_restore_absent": True,
                "expected_quarantine_present": True,
                "exact_candidate_names": [vm.VM_R13_EXPECTED_QUARANTINE_NAME],
                "candidate_count": 1, "namespace_unambiguous": True,
            },
            "local_restore_payload_retired": False,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "vm_guest_base_sealed": False,
            "p2_cleanup_success_claimed": False,
            "capacity_commitment_inactivation_attempted": False,
        }
        ancestry = {key: validation[key] for key in (
            "pending_receipt_sha256", "old_authorization_sha256",
            "old_predelete_review_sha256", "r11_failure_sha256",
            "r12_identity_contract_review_sha256", "r12_quarantine_identity_review_sha256",
            "expected_quarantine_path", "expected_quarantine_name")}
        validation_sha = "7" * 64
        with mock.patch.object(policy, "REPO", repo), \
                mock.patch.object(vm, "_read_signed_record_snapshot",
                                  return_value=(validation, b"", validation_sha, ()) ), \
                mock.patch.object(vm, "_deferred_restore_recovery_ancestry",
                                  return_value=ancestry), \
                mock.patch.object(vm, "_deferred_recovery_path_review",
                                  return_value=({}, path_review_ref)):
            with self.assertRaises((OSError, RuntimeError)):
                vm.authorize_deferred_restore_recovery(
                    validation_path, repo / "missing-fresh-review.json")

    def test_absent_recovery_authorization_fails_before_payload_access(self) -> None:
        missing = self.base / "missing-recovery-authorization.json"
        with self.assertRaises((OSError, RuntimeError)):
            vm.perform_deferred_restore_recovery(missing, self.base / "review.json")

    @contextmanager
    def _synthetic_recovery_harness(
            self, *, replace_after_boundary_hash: bool = False,
            reject_post_unlink_measurement: bool = False,
            fail_exact_unlink: bool = False):
        from contextlib import contextmanager

        repo = self.base / f"repo-{uuid.uuid4().hex}"
        (repo / "experiments/storage").mkdir(parents=True)
        root = self.base / f"vm-{uuid.uuid4().hex}"
        root.mkdir()
        os.chmod(root, 0o775)
        restore_path = root / "restore.ipsw"
        quarantine = root / vm.VM_R13_EXPECTED_QUARANTINE_NAME
        payload = b"synthetic exact pending restore payload"
        quarantine.write_bytes(payload)
        os.chmod(quarantine, 0o600)
        identity = vm._deferred_restore_recovery_file_identity(quarantine.stat())
        root_info = root.stat()
        root_identity = {"device": int(root_info.st_dev), "inode": int(root_info.st_ino),
                         "owner_uid": int(root_info.st_uid),
                         "mode": int(stat.S_IMODE(root_info.st_mode))}
        expected_sha = hashlib.sha256(payload).hexdigest()
        marker_sha = "a" * 64
        ancestry = {
            "pending_receipt_path": "old-pending.json",
            "pending_receipt_sha256": "1" * 64,
            "old_authorization_path": "old-authorization.json",
            "old_authorization_sha256": "2" * 64,
            "old_predelete_review_path": "old-predelete-review.json",
            "old_predelete_review_sha256": "3" * 64,
            "r11_failure_path": "/tmp/synthetic-r11.json",
            "r11_failure_sha256": "4" * 64,
            "r12_identity_contract_review_path": "r12-identity.md",
            "r12_identity_contract_review_sha256": "5" * 64,
            "r12_quarantine_identity_review_path": "r12-quarantine.md",
            "r12_quarantine_identity_review_sha256": "6" * 64,
            "r12_controller_source_sha256": "7" * 64,
            "r12_test_source_sha256": "8" * 64,
            "branch_retirement_receipt_path": "branch.json",
            "branch_retirement_receipt_sha256": "9" * 64,
            "expected_quarantine_path": str(quarantine),
            "expected_quarantine_name": vm.VM_R13_EXPECTED_QUARANTINE_NAME,
            "expected_quarantine_identity": identity,
            "pending_root_identity": root_identity,
            "active_marker_sha256": marker_sha,
            "committed_growth_bytes": 19_350_155_264,
            "session_id": "b" * 32,
            "p1_provenance_historically_accepted": True,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "vm_guest_base_sealed": False,
        }
        pending_path = repo / "old-pending.json"
        pending_bytes = b"immutable synthetic PENDING receipt bytes"
        pending_path.write_bytes(pending_bytes)
        p1_admission_path = repo / "p1-admission.json"
        p1_verification_path = repo / "p1-verification.json"
        p1_admission_path.write_bytes(b"synthetic p1 admission")
        p1_verification_path.write_bytes(b"synthetic p1 verification")
        tests_path = repo / "experiments/storage/test_vm_capacity_domain.py"
        tests_path.write_bytes(b"synthetic recovery tests source")
        controller_identity = vm._controller_identity()
        tests_sha = hashlib.sha256(tests_path.read_bytes()).hexdigest()
        path_review_path = repo / "R6-VM-DEFERRED-RESTORE-RECOVERY-PATH-REVIEW-synthetic.json"
        path_review = {
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_PATH_REVIEW",
            "status": "PASS", "decision": vm.VM_R13_PATH_REVIEW_DECISION,
            "reviewer_identity": "GPT-6 Luna Max — Deferred Restore Recovery Path Reviewer",
            "model_identity": "GPT-6 Luna Max",
            "reviewed_controller_identity": controller_identity,
            "reviewed_tests_sha256": tests_sha,
            "reviewed_real_payload": False,
            "reviewed_at_unix": time.time(), "findings": [],
        }
        path_review_bytes = json.dumps(path_review, sort_keys=True).encode()
        path_review_path.write_bytes(path_review_bytes)
        path_review_ref = {
            "path": path_review_path.relative_to(repo).as_posix(),
            "sha256": hashlib.sha256(path_review_bytes).hexdigest(),
        }
        mount = {"mount_state": "MOUNTED_VERIFIED", "mount_point": str(root)}
        validation_path = repo / "R6-VM-DEFERRED-RESTORE-RECOVERY-VALIDATION-synthetic.json"
        validation = {
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_VALIDATION",
            "status": "PASS_VALIDATED",
            "created_at_unix": time.time() - 20,
            **ancestry,
            "path_review_path": path_review_ref["path"],
            "path_review_sha256": path_review_ref["sha256"],
            "current_controller_identity": controller_identity,
            "current_tests_sha256": tests_sha,
            "active_marker_sha256": marker_sha,
            "committed_growth_bytes": ancestry["committed_growth_bytes"],
            "p1_admission_path": "p1-admission.json",
            "p1_admission_sha256": "c" * 64,
            "p1_verification_path": "p1-verification.json",
            "p1_verification_sha256": "d" * 64,
            "quarantine_path": str(quarantine),
            "quarantine_file_identity": identity,
            "current_sha256": expected_sha,
            "current_byte_count": len(payload),
            "namespace_state": {"canonical_restore_absent": True,
                                "expected_quarantine_present": True,
                                "exact_candidate_names": [quarantine.name],
                                "candidate_count": 1, "namespace_unambiguous": True},
            "vm_mount_identity": mount,
            "vm_volume_root_identity": root_identity,
            "vm_process_scan_before": {"scan_status": "PASS", "blocking_processes": []},
            "vm_process_scan_after": {"scan_status": "PASS", "blocking_processes": []},
            "restore_descriptor_scan_before": {"scan_clear": True},
            "restore_descriptor_scan_after": {"scan_clear": True},
            "p1_restore_acquisition_historically_accepted": True,
            "p1_upstream_content_digest_validated": False,
            "local_restore_payload_retired": False,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "vm_guest_base_sealed": False,
            "p2_cleanup_success_claimed": False,
            "capacity_commitment_inactivation_attempted": False,
        }
        validation_bytes = json.dumps(validation, sort_keys=True).encode()
        validation_path.write_bytes(validation_bytes)
        validation_sha = hashlib.sha256(validation_bytes).hexdigest()
        review_path = repo / "recovery-review.json"
        review = {
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_REVIEW",
            "status": "PASS", "decision": vm.VM_R13_RECOVERY_REVIEW_DECISION,
            "reviewer_identity": "GPT-6 Luna Max — Quarantined Restore Recovery Reviewer",
            "model_identity": "GPT-6 Luna Max",
            "review_scope": "Read-only independent review of the exact signed full-hash recovery validation and linked R11/R12/PENDING evidence.",
            "reviewed_at_unix": time.time() - 10,
            "findings": [],
            "reviewed_validation_path": validation_path.relative_to(repo).as_posix(),
            "reviewed_validation_sha256": validation_sha,
            "reviewed_pending_receipt_sha256": ancestry["pending_receipt_sha256"],
            "reviewed_old_authorization_sha256": ancestry["old_authorization_sha256"],
            "reviewed_old_predelete_review_sha256": ancestry["old_predelete_review_sha256"],
            "reviewed_r11_failure_sha256": ancestry["r11_failure_sha256"],
            "reviewed_r12_identity_contract_review_sha256":
                ancestry["r12_identity_contract_review_sha256"],
            "reviewed_r12_quarantine_identity_review_sha256":
                ancestry["r12_quarantine_identity_review_sha256"],
            "reviewed_branch_retirement_receipt_sha256":
                ancestry["branch_retirement_receipt_sha256"],
            "reviewed_p1_admission_sha256": "c" * 64,
            "reviewed_p1_verification_sha256": "d" * 64,
            "reviewed_full_sha256": expected_sha,
            "reviewed_exact_byte_count": len(payload),
            "reviewed_quarantine_path": str(quarantine),
            "reviewed_quarantine_file_identity": identity,
            "reviewed_namespace_state": validation["namespace_state"],
            "reviewed_process_scan_before": validation["vm_process_scan_before"],
            "reviewed_process_scan_after": validation["vm_process_scan_after"],
            "reviewed_descriptor_scan_before": validation["restore_descriptor_scan_before"],
            "reviewed_descriptor_scan_after": validation["restore_descriptor_scan_after"],
            "reviewed_current_controller_identity": controller_identity,
            "reviewed_current_tests_sha256": tests_sha,
            "reviewed_p1_restore_acquisition_historically_accepted": True,
            "reviewed_p1_upstream_content_digest_validated": False,
            "reviewed_p2_base_provisioning_accepted": False,
            "reviewed_capacity_commitment_inactivation_attempted": False,
        }
        review_bytes = json.dumps(review, sort_keys=True).encode()
        review_path.write_bytes(review_bytes)
        review_sha = hashlib.sha256(review_bytes).hexdigest()
        authorization_path = repo / "R6-VM-DEFERRED-RESTORE-RECOVERY-AUTHORIZATION-synthetic.json"
        authorization = {
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION",
            "status": "AUTHORIZED", "created_at_unix": time.time() - 5,
            "action_id": "e" * 32,
            "action": "DELETE_EXACT_PENDING_QUARANTINED_RESTORE",
            "single_use": True,
            "recovery_validation_path": validation_path.relative_to(repo).as_posix(),
            "recovery_validation_sha256": validation_sha,
            "recovery_review_path": review_path.relative_to(repo).as_posix(),
            "recovery_review_sha256": review_sha,
            "path_review_path": path_review_ref["path"],
            "path_review_sha256": path_review_ref["sha256"],
            "pending_receipt_sha256": ancestry["pending_receipt_sha256"],
            "old_authorization_sha256": ancestry["old_authorization_sha256"],
            "old_predelete_review_sha256": ancestry["old_predelete_review_sha256"],
            "r11_failure_sha256": ancestry["r11_failure_sha256"],
            "r12_identity_contract_review_sha256":
                ancestry["r12_identity_contract_review_sha256"],
            "r12_quarantine_identity_review_sha256":
                ancestry["r12_quarantine_identity_review_sha256"],
            "restore_path": str(restore_path),
            "quarantine_path": str(quarantine),
            "restore_sha256": expected_sha,
            "restore_logical_bytes": len(payload),
            "fresh_full_sha256": expected_sha,
            "fresh_byte_count": len(payload),
            "restore_file_identity": identity,
            "vm_volume_root_identity": root_identity,
            "active_marker_sha256": marker_sha,
            "committed_growth_bytes": ancestry["committed_growth_bytes"],
            "current_controller_identity": controller_identity,
            "local_restore_payload_retired": False,
            "p1_restore_acquisition_historically_accepted": True,
            "p1_upstream_content_digest_validated": False,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "vm_guest_base_sealed": False,
            "p2_cleanup_success_claimed": False,
            "capacity_commitment_inactivation_attempted": False,
        }
        authorization_bytes = json.dumps(authorization, sort_keys=True).encode()
        authorization_path.write_bytes(authorization_bytes)
        authorization_sha = hashlib.sha256(authorization_bytes).hexdigest()
        marker = {"session_id": ancestry["session_id"], "state": "ACTIVE",
                  "phase_id": "P2_AUTHORIZED",
                  "committed_growth_bytes": ancestry["committed_growth_bytes"]}
        p1_verification = {"local_sha256": expected_sha,
                           "expected_length_bytes": len(payload),
                           "actual_length_bytes": len(payload)}
        snapshot = {
            "repo": repo, "root": root, "restore_path": restore_path,
            "quarantine": quarantine, "payload": payload, "identity": identity,
            "root_identity": root_identity, "expected_sha": expected_sha,
            "ancestry": ancestry, "controller_identity": controller_identity,
            "tests_sha": tests_sha, "path_review": path_review,
            "path_review_ref": path_review_ref, "path_review_path": path_review_path,
            "validation": validation, "validation_path": validation_path,
            "validation_sha": validation_sha, "review": review,
            "review_path": review_path, "review_sha": review_sha,
            "authorization": authorization, "authorization_path": authorization_path,
            "authorization_sha": authorization_sha, "marker": marker,
            "marker_sha": marker_sha, "p1_verification": p1_verification,
            "mount": mount, "pending_path": pending_path,
            "pending_bytes": pending_bytes,
        }
        context = {"written": [], "descriptor_scan_calls": 0,
                   "storage_scan_calls": 0, "observation_calls": 0}

        @contextmanager
        def allocation_lock():
            lock_path = repo / ".synthetic-storage-lock"
            descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
            lock_file = os.fdopen(descriptor, "a+")
            try:
                yield lock_file
            finally:
                lock_file.close()

        def signed_snapshot(path: Path, _signature: str):
            if Path(path).resolve() == authorization_path.resolve():
                return authorization, authorization_bytes, authorization_sha, ()
            if Path(path).resolve() == validation_path.resolve():
                return validation, validation_bytes, validation_sha, ()
            raise AssertionError(f"unexpected signed snapshot read: {path}")

        def descriptor_scan(_paths, *, allow_current_process_only=False):
            context["descriptor_scan_calls"] += 1
            if replace_after_boundary_hash and context["descriptor_scan_calls"] == 2:
                held = root / ".held-original-object"
                os.rename(quarantine, held)
                quarantine.write_bytes(b"substituted recovery object")
                os.chmod(quarantine, 0o600)
            if allow_current_process_only:
                return {"descriptor_identity_mismatches": [],
                        "open_file_descriptors": [{"files": [{"fd": "5"}]}],
                        "exact_target_descriptor_matches": [{"pid": os.getpid()}],
                        "open_process_ids": [str(os.getpid())]}
            return {"descriptor_identity_mismatches": [], "open_file_descriptors": [],
                    "exact_target_descriptor_matches": [], "open_process_ids": [],
                    "scan_clear": True}

        def write_unique(path: Path, record, _signature: str):
            raw = json.dumps(record, sort_keys=True).encode()
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, raw)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            context["written"].append((path, dict(record)))
            return dict(record)

        import p2_base_orchestrator as p2

        def storage_summary(observation):
            if reject_post_unlink_measurement and observation["index"] >= 2:
                raise RuntimeError("synthetic APFS cross-check disagreement")
            return {
                "conservative_shared_free_bytes": 1_000 if observation["index"] == 1 else 1_200,
                "vm_volume_uuid": vm.VM_DEFERRED_VM_VOLUME_UUID,
                "safety_volume_uuid": vm.VM_DEFERRED_SAFETY_VOLUME_UUID,
                "safety_volume_reserve_bytes": vm.VM_DEFERRED_SAFETY_RESERVE_BYTES,
            }

        original_unlink = os.unlink

        def unlink_with_optional_failure(path, *args, **kwargs):
            if (fail_exact_unlink and path == quarantine.name
                    and kwargs.get("dir_fd") is not None):
                raise OSError("synthetic exact unlink failure")
            return original_unlink(path, *args, **kwargs)

        patches = [
            mock.patch.object(policy, "REPO", repo),
            mock.patch.object(policy, "_acquire_storage_allocation_lock",
                              side_effect=allocation_lock),
            mock.patch.object(vm, "VM_DEFERRED_RESTORE_PATH", restore_path),
            mock.patch.object(vm, "VM_DEFERRED_P1_ADMISSION_PATH", p1_admission_path),
            mock.patch.object(vm, "VM_DEFERRED_P1_VERIFICATION_PATH", p1_verification_path),
            mock.patch.object(vm, "VM_DEFERRED_RESTORE_SHA256", expected_sha),
            mock.patch.object(vm, "VM_DEFERRED_RESTORE_LOGICAL_BYTES", len(payload)),
            mock.patch.object(vm, "VM_R13_EXPECTED_QUARANTINE_IDENTITY", identity),
            mock.patch.object(vm, "VM_R13_EXPECTED_ROOT_IDENTITY", root_identity),
            mock.patch.object(vm, "_deferred_restore_recovery_ancestry",
                              return_value=ancestry),
            mock.patch.object(vm, "_deferred_recovery_path_review",
                              return_value=(path_review, path_review_ref)),
            mock.patch.object(vm, "_deferred_read_exact_active_marker",
                              return_value=(marker, marker_sha)),
            mock.patch.object(vm, "_deferred_p1_evidence",
                              return_value=({}, "c" * 64, p1_verification, "d" * 64)),
            mock.patch.object(vm, "_controller_identity", return_value=controller_identity),
            mock.patch.object(vm, "_read_signed_record_snapshot", side_effect=signed_snapshot),
            mock.patch.object(vm, "_deferred_fresh_review_json", return_value=(review, review_sha)),
            mock.patch.object(vm, "_deferred_recovery_require_process_scan",
                              return_value={"scan_status": "PASS", "blocking_processes": []}),
            mock.patch.object(vm, "_deferred_recovery_require_descriptor_scan",
                              side_effect=descriptor_scan),
            mock.patch.object(vm, "_deferred_recovery_lock_evidence",
                              return_value={"held": True}),
            mock.patch.object(vm, "_volume_mount_info", return_value=mount),
            mock.patch.object(vm, "_verify_open_mount_fd"),
            mock.patch.object(vm, "_deferred_storage_summary", side_effect=storage_summary),
            mock.patch.object(vm.os, "unlink", side_effect=unlink_with_optional_failure),
            mock.patch.object(vm, "capture_host_observation", side_effect=lambda: {
                "index": (context.__setitem__("observation_calls",
                                                context["observation_calls"] + 1)
                          or context["observation_calls"])}),
            mock.patch.object(vm, "_volume_row", side_effect=lambda observation, _uuid: {
                "CapacityInUse": 500 if observation["index"] == 1 else 400}),
            mock.patch.object(p2, "_acquire_p2_cleanup_lock",
                              side_effect=lambda: (os.open(root, os.O_RDONLY), {"held": True})),
            mock.patch.object(vm, "_write_signed_json_unique", side_effect=write_unique),
        ]
        for patcher in patches:
            patcher.start()
        try:
            yield snapshot, context
        finally:
            for patcher in reversed(patches):
                patcher.stop()

    def test_exact_synthetic_pending_quarantine_recovers_to_separate_resolution(self) -> None:
        with self._synthetic_recovery_harness() as (case, context):
            resolution = vm.perform_deferred_restore_recovery(
                case["authorization_path"], case["review_path"])
            self.assertEqual(resolution["status"], "PASS")
            self.assertEqual(resolution["classification"],
                             "PENDING_TRANSACTION_RESOLVED_BY_FRESHLY_AUTHORIZED_RECOVERY")
            self.assertEqual(resolution["record_type"],
                             "R6_VM_DEFERRED_RESTORE_RETIREMENT_RESOLUTION")
            self.assertFalse(case["restore_path"].exists())
            self.assertFalse(case["quarantine"].exists())
            self.assertEqual(case["pending_path"].read_bytes(), case["pending_bytes"])
            self.assertEqual(resolution["first_current_full_sha256"], case["expected_sha"])
            self.assertEqual(resolution["destructive_boundary_full_sha256"], case["expected_sha"])
            self.assertEqual(resolution["exact_byte_count"], len(case["payload"]))
            self.assertGreaterEqual(context["descriptor_scan_calls"], 2)

    def test_recovery_resolution_preserves_false_p2_flags_and_active_commitment(self) -> None:
        with self._synthetic_recovery_harness() as (case, _context):
            resolution = vm.perform_deferred_restore_recovery(
                case["authorization_path"], case["review_path"])
            self.assertTrue(resolution["local_restore_payload_retired"])
            self.assertTrue(resolution["p1_restore_acquisition_historically_accepted"])
            self.assertFalse(resolution["p1_upstream_content_digest_validated"])
            self.assertFalse(resolution["p2_base_provisioning_accepted"])
            self.assertFalse(resolution["vm_guest_base_provisioned"])
            self.assertFalse(resolution["vm_guest_base_sealed"])
            self.assertFalse(resolution["p2_cleanup_success_claimed"])
            self.assertFalse(resolution["capacity_commitment_inactivation_attempted"])
            self.assertEqual(resolution["active_marker_sha256"], case["marker_sha"])

    def test_actual_reclaim_uses_measured_physical_change_not_payload_length(self) -> None:
        with self._synthetic_recovery_harness() as (case, _context):
            resolution = vm.perform_deferred_restore_recovery(
                case["authorization_path"], case["review_path"])
            self.assertEqual(resolution["measured_vm_quota_reduction_bytes"], 100)
            self.assertEqual(resolution["measured_shared_free_increase_bytes"], 200)
            self.assertEqual(resolution["RESTORE_RETIREMENT_RECLAIMED_BYTES"], 100)
            self.assertNotEqual(resolution["RESTORE_RETIREMENT_RECLAIMED_BYTES"],
                                len(case["payload"]))

    def test_exact_unlink_survives_rejected_immediate_measurement_without_reclaim_claim(self) -> None:
        with self._synthetic_recovery_harness(
                reject_post_unlink_measurement=True) as (case, context):
            with self.assertRaisesRegex(RuntimeError, "APFS cross-check disagreement"):
                vm.perform_deferred_restore_recovery(
                    case["authorization_path"], case["review_path"])
            self.assertFalse(case["quarantine"].exists())
            self.assertFalse(case["restore_path"].exists())
            self.assertEqual(case["pending_path"].read_bytes(), case["pending_bytes"])
            resolution_path = (case["repo"] / "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-"
                               f"{case['authorization']['action_id']}.json")
            self.assertFalse(resolution_path.exists())
            use_path = (case["repo"] / "R6-VM-DEFERRED-RESTORE-RECOVERY-USE-"
                        f"{case['authorization']['action_id']}.json")
            use_record = json.loads(use_path.read_text(encoding="utf-8"))
            self.assertTrue(use_record["boundary_full_hash_pending"])
            self.assertFalse(use_record["unlink_completed"])
            self.assertFalse(use_record["reclaimed_bytes_claimed"])
            self.assertNotIn("RESTORE_RETIREMENT_RECLAIMED_BYTES", use_record)
            self.assertEqual(context["observation_calls"], 2)

    def test_failed_exact_unlink_cannot_become_deletion_pass_from_successful_measurement(self) -> None:
        with self._synthetic_recovery_harness(fail_exact_unlink=True) as (case, context):
            with self.assertRaisesRegex(OSError, "synthetic exact unlink failure"):
                vm.perform_deferred_restore_recovery(
                    case["authorization_path"], case["review_path"])
            self.assertTrue(case["quarantine"].is_file())
            self.assertEqual(case["pending_path"].read_bytes(), case["pending_bytes"])
            resolution_path = (case["repo"] / "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-"
                               f"{case['authorization']['action_id']}.json")
            self.assertFalse(resolution_path.exists())
            self.assertEqual(context["observation_calls"], 1)

    def test_old_authorization_replay_is_blocked_after_recovery_use(self) -> None:
        with self._synthetic_recovery_harness() as (case, _context):
            vm.perform_deferred_restore_recovery(case["authorization_path"], case["review_path"])
            with self.assertRaisesRegex(RuntimeError, "already been consumed or resolved"):
                vm.perform_deferred_restore_recovery(
                    case["authorization_path"], case["review_path"])

    def test_failed_attempt_cannot_replay_by_selecting_an_alternate_use_receipt(self) -> None:
        with self._synthetic_recovery_harness(
                replace_after_boundary_hash=True) as (case, _context):
            with self.assertRaisesRegex(RuntimeError, "device|inode|identity"):
                vm.perform_deferred_restore_recovery(
                    case["authorization_path"], case["review_path"])
            canonical_use_path = case["repo"] / (
                "R6-VM-DEFERRED-RESTORE-RECOVERY-USE-"
                f"{case['authorization']['action_id']}.json")
            alternate_use_path = self.base / "alternate-recovery-use.json"
            self.assertTrue(canonical_use_path.is_file())
            with self.assertRaisesRegex(TypeError, "use_receipt_path"):
                vm.perform_deferred_restore_recovery(
                    case["authorization_path"], case["review_path"],
                    use_receipt_path=alternate_use_path)
            self.assertFalse(alternate_use_path.exists())
            self.assertTrue(case["quarantine"].is_file())
            self.assertEqual(case["pending_path"].read_bytes(), case["pending_bytes"])

    def test_recovery_cli_rejects_custom_use_receipt_path(self) -> None:
        with mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit) as raised:
                vm.main([
                    "recover-deferred-restore-retirement",
                    "--authorization", str(self.base / "authorization.json"),
                    "--recovery-review", str(self.base / "review.json"),
                    "--use-receipt", str(self.base / "alternate-use.json"),
                ])
        self.assertEqual(raised.exception.code, 2)

    def test_pre_unlink_substitution_fails_without_resolution_or_reclaim_claim(self) -> None:
        with self._synthetic_recovery_harness(replace_after_boundary_hash=True) as (case, context):
            with self.assertRaisesRegex(RuntimeError, "device|inode|identity"):
                vm.perform_deferred_restore_recovery(
                    case["authorization_path"], case["review_path"])
            self.assertTrue(case["quarantine"].is_file())
            self.assertEqual(case["quarantine"].read_bytes(), b"substituted recovery object")
            self.assertFalse((case["repo"] / "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-"
                              f"{case['authorization']['action_id']}.json").exists())
            use_path = case["repo"] / ("R6-VM-DEFERRED-RESTORE-RECOVERY-USE-"
                                        f"{case['authorization']['action_id']}.json")
            use_record = json.loads(use_path.read_text())
            self.assertFalse(use_record["reclaimed_bytes_claimed"])
            self.assertFalse(use_record["unlink_completed"])
            self.assertNotIn("RESTORE_RETIREMENT_RECLAIMED_BYTES", use_record)
            self.assertEqual(case["pending_path"].read_bytes(), case["pending_bytes"])

    def test_fresh_review_absent_and_fresh_authorization_absent_both_fail(self) -> None:
        missing_review = self.base / "absent-recovery-review.json"
        with self.assertRaises((OSError, RuntimeError)):
            vm._deferred_fresh_review_json(
                missing_review, decision=vm.VM_R13_RECOVERY_REVIEW_DECISION,
                expected_fields={})
        with self.assertRaises((OSError, RuntimeError)):
            vm.perform_deferred_restore_recovery(
                self.base / "absent-recovery-authorization.json", missing_review)

    def test_recovery_use_receipt_has_no_claimed_reclaim_on_failed_recovery(self) -> None:
        with self._synthetic_recovery_harness(replace_after_boundary_hash=True) as (case, _context):
            with self.assertRaises(RuntimeError):
                vm.perform_deferred_restore_recovery(
                    case["authorization_path"], case["review_path"])
            use_path = case["repo"] / ("R6-VM-DEFERRED-RESTORE-RECOVERY-USE-"
                                        f"{case['authorization']['action_id']}.json")
            use_record = json.loads(use_path.read_text())
            self.assertFalse(use_record["reclaimed_bytes_claimed"])
            self.assertFalse((case["repo"] / "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-"
                              f"{case['authorization']['action_id']}.json").exists())

    def test_r14_duplicate_terminal_resolution_is_rejected(self) -> None:
        repo = Path(self.temp.name) / "r14-duplicate-repo"
        repo.mkdir()
        existing = repo / "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-prior.json"
        existing.write_text("immutable prior resolution", encoding="utf-8")
        with mock.patch.object(policy, "REPO", repo):
            with self.assertRaisesRegex(RuntimeError, "already has a terminal resolution"):
                vm._assert_r14_resolution_not_duplicate(
                    repo / "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-new.json")

    def test_r14_accepts_only_exact_r13_boundary_hash_pass_wording_and_identity(self) -> None:
        evidence = {
            "boundary_full_hash_status": vm.VM_R13_BOUNDARY_HASH_STATUS,
            "boundary_full_sha256": vm.VM_DEFERRED_RESTORE_SHA256,
            "boundary_exact_byte_count": vm.VM_DEFERRED_RESTORE_LOGICAL_BYTES,
        }
        self.assertTrue(vm._r13_boundary_hash_evidence_passed(evidence))
        for changed in (
                {**evidence, "boundary_full_hash_status": "FAIL"},
                {**evidence, "boundary_full_hash_status": (
                    vm.VM_R13_BOUNDARY_HASH_STATUS.replace("PASSED", "FAILED"))},
                {**evidence, "boundary_full_sha256": "0" * 64},
                {**evidence, "boundary_exact_byte_count": (
                    vm.VM_DEFERRED_RESTORE_LOGICAL_BYTES - 1)}):
            with self.subTest(changed=changed):
                self.assertFalse(vm._r13_boundary_hash_evidence_passed(changed))

    def test_r14_null_reclaim_is_unknown_and_never_backfilled(self) -> None:
        resolution = {
            "payload_deletion_status": "CONFIRMED",
            "deleted_path": str(vm.VM_DEFERRED_RESTORE_PATH),
            "actual_unlinked_path": str(
                vm.VM_DEFERRED_RESTORE_PATH.parent / vm.VM_R13_EXPECTED_QUARANTINE_NAME),
            "reclaimed_bytes_exact": None,
            "RESTORE_RETIREMENT_RECLAIMED_BYTES": None,
            "immediate_reclaim_measurement_status": "REJECTED_APFS_CROSSCHECK_DISAGREEMENT",
            "reclaim_measurement_status": "IMMEDIATE_POST_UNLINK_MEASUREMENT_REJECTED",
            "historical_reclaim_backfilled": False,
            "later_delta_is_accepted_as_actual_reclaim": False,
            "current_state_measurement_required": True,
            "current_state_capacity_validated": False,
            "future_vm_work_requires_fresh_p1": True,
            "p2_base_provisioning_accepted": False,
            "capacity_commitment_inactivation_attempted": False,
        }
        vm._assert_r14_resolution_measurement_unknown(resolution)
        for changed in (
                {**resolution, "reclaimed_bytes_exact": 0},
                {**resolution, "RESTORE_RETIREMENT_RECLAIMED_BYTES": 1},
                {**resolution, "actual_unlinked_path": None},
                {**resolution, "future_vm_work_requires_fresh_p1": False},
                {**resolution, "later_delta_is_accepted_as_actual_reclaim": True}):
            with self.subTest(changed=changed), self.assertRaisesRegex(
                    RuntimeError, "conflates unlink proof"):
                vm._assert_r14_resolution_measurement_unknown(changed)

    def test_r14_resolution_rejects_conflicting_present_restore_paths(self) -> None:
        root = Path(self.temp.name) / "r14-conflicting-restore"
        root.mkdir()
        canonical = root / "restore.ipsw"
        canonical.write_bytes(b"unexpected restore candidate")
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            with self.assertRaisesRegex(RuntimeError, "restore payload or quarantine is present"):
                vm._assert_r14_resolution_namespace_absent(root_fd)
        finally:
            os.close(root_fd)

    def test_r14_resolution_rejects_second_quarantine_candidate(self) -> None:
        root = Path(self.temp.name) / "r14-duplicate-quarantine"
        root.mkdir()
        (root / vm.VM_R13_EXPECTED_QUARANTINE_NAME).write_bytes(b"unexpected candidate")
        root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            with self.assertRaisesRegex(RuntimeError, "restore payload or quarantine is present"):
                vm._assert_r14_resolution_namespace_absent(root_fd)
        finally:
            os.close(root_fd)


class DeferredReviewFreshnessContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.storage = self.repo / "experiments/storage"
        self.storage.mkdir(parents=True)
        self.vm_root = self.base / "vm-volume"
        self.vm_root.mkdir(mode=0o700)
        self.addCleanup(self.temp.cleanup)
        self.patchers = [
            mock.patch.object(policy, "REPO", self.repo),
            mock.patch.object(vm, "VM_R14_RECOVERY_MEASUREMENT_FAILURE_PATH",
                              self.storage / "failure.json"),
            mock.patch.object(vm, "VM_R13_PENDING_RECEIPT_PATH", self.repo / "pending.json"),
            mock.patch.object(vm, "VM_DEFERRED_RESTORE_PATH", self.vm_root / "restore.ipsw"),
            mock.patch.object(vm, "VM_DEFERRED_P1_ADMISSION_PATH", self.repo / "p1-admission.json"),
            mock.patch.object(vm, "VM_DEFERRED_P1_VERIFICATION_PATH", self.repo / "p1-verification.json"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _write_json(path: Path, value: dict[str, object]) -> bytes:
        raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return raw

    def _run_r14_resolution(self, case: dict[str, object], *, review_path: Path | None = None):
        import p2_base_orchestrator as p2

        real_file_sha256 = vm._file_sha256
        real_open_directory = policy._open_directory_nofollow
        test_source = self.repo / "experiments/storage/test_vm_capacity_domain.py"
        test_source.write_text("synthetic tests", encoding="utf-8")

        def read_signed(path: Path, _signature_field: str):
            return case["signed_records"][str(Path(path).resolve())]

        def file_sha(path: Path) -> str:
            if Path(path).resolve() == test_source.resolve():
                return case["tests_sha"]
            return real_file_sha256(path)

        def write_receipt(path: Path, record: dict[str, object], _signature_field: str):
            Path(path).write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
            return record

        def open_directory(path: Path) -> int:
            if Path(path).resolve() == self.vm_root.resolve():
                return os.open(self.vm_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            return real_open_directory(path)

        with ExitStack() as stack:
            for target, name, value in (
                    (vm, "_read_signed_record_snapshot", read_signed),
                    (vm, "_deferred_restore_recovery_ancestry", case["ancestry"]),
                    (vm, "_assert_deferred_restore_recovery_authorization", None),
                    (vm, "_assert_r13_post_unlink_measurement_failure", None),
                    (vm, "_controller_identity", case["controller_identity"]),
                    (vm, "_file_sha256", file_sha),
                    (vm, "capture_host_observation", {}),
                    (vm, "_volume_mount_info", case["mount"]),
                    (vm, "_verify_open_mount_fd", None),
                    (vm, "_deferred_restore_tree_evidence", {
                        "blockers": [], "restore_paths": [], "quarantine_paths": []}),
                    (vm, "_write_signed_json_unique", write_receipt),
                    (policy, "_acquire_storage_allocation_lock", lambda: nullcontext(object())),
                    (p2, "_acquire_p2_cleanup_lock", lambda: (os.open(os.devnull, os.O_RDONLY), {})),
                    (policy, "_open_directory_nofollow", open_directory),
                ):
                if value is None:
                    stack.enter_context(mock.patch.object(target, name))
                else:
                    stack.enter_context(mock.patch.object(target, name, side_effect=value)
                                         if name in {"_read_signed_record_snapshot",
                                                     "_file_sha256",
                                                     "_write_signed_json_unique",
                                                     "_acquire_storage_allocation_lock",
                                                     "_acquire_p2_cleanup_lock",
                                                     "_open_directory_nofollow"}
                                         else mock.patch.object(target, name, return_value=value))
            stack.enter_context(mock.patch.object(
                vm, "_deferred_read_exact_active_marker",
                return_value=(case["marker"], "a" * 64)))
            return vm.resolve_deferred_restore_measurement_failure(
                review_path or case["r14_review_path"], receipt_path=case["receipt_path"])

    def test_current_review_freshness_window_is_enforced(self) -> None:
        review_path = self.storage / "freshness.json"
        base = {
            "schema_version": 1, "status": "PASS", "decision": "current",
            "reviewer_identity": "independent reviewer", "model_identity": "GPT-6 Luna Max",
            "reviewed_at_unix": time.time(), "findings": [], "subject": "exact",
        }
        review_path.write_text(json.dumps(base), encoding="utf-8")
        accepted, _ = vm._deferred_fresh_review_json(
            review_path, decision="current", expected_fields={"subject": "exact"})
        self.assertEqual(accepted["status"], "PASS")
        review_path.write_text(json.dumps({
            **base, "reviewed_at_unix": time.time()
            - vm.VM_DEFERRED_REVIEW_MAX_AGE_SECONDS - 1,
        }), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "stale"):
            vm._deferred_fresh_review_json(
                review_path, decision="current", expected_fields={"subject": "exact"})

    def test_exact_aged_historical_review_requires_sha_action_subject_and_signed_chain(self) -> None:
        case = self._historical_review_case()
        review, digest, binding, fields = (
            case["historical_review"], case["historical_sha"],
            case["historical_binding"], case["historical_fields"])
        accepted, accepted_sha = vm._deferred_bound_historical_review_json(
            case["historical_path"],
            decision=vm.VM_R13_RECOVERY_REVIEW_DECISION,
            expected_record_type="R6_VM_DEFERRED_RESTORE_RECOVERY_CONTENT_REVIEW",
            expected_fields=fields, binding=binding)
        self.assertEqual(accepted["reviewed_at_unix"], review["reviewed_at_unix"])
        self.assertEqual(accepted_sha, digest)
        self.assertGreater(time.time() - accepted["reviewed_at_unix"],
                           vm.VM_DEFERRED_REVIEW_MAX_AGE_SECONDS)

        with self.subTest("wrong hash"):
            bad = vm._DeferredHistoricalReviewBinding(
                **{**binding.__dict__, "review_sha256": "f" * 64})
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                vm._deferred_bound_historical_review_json(
                    case["historical_path"], decision=vm.VM_R13_RECOVERY_REVIEW_DECISION,
                    expected_record_type="R6_VM_DEFERRED_RESTORE_RECOVERY_CONTENT_REVIEW",
                    expected_fields=fields, binding=bad)
        with self.subTest("wrong transaction action"):
            bad = vm._DeferredHistoricalReviewBinding(
                **{**binding.__dict__, "action_id": "0" * 32})
            with self.assertRaisesRegex(RuntimeError, "bound"):
                vm._deferred_bound_historical_review_json(
                    case["historical_path"], decision=vm.VM_R13_RECOVERY_REVIEW_DECISION,
                    expected_record_type="R6_VM_DEFERRED_RESTORE_RECOVERY_CONTENT_REVIEW",
                    expected_fields=fields, binding=bad)
        with self.subTest("wrong subject"):
            bad = vm._DeferredHistoricalReviewBinding(
                **{**binding.__dict__, "subject_sha256": "0" * 64})
            with self.assertRaisesRegex(RuntimeError, "bound"):
                vm._deferred_bound_historical_review_json(
                    case["historical_path"], decision=vm.VM_R13_RECOVERY_REVIEW_DECISION,
                    expected_record_type="R6_VM_DEFERRED_RESTORE_RECOVERY_CONTENT_REVIEW",
                    expected_fields=fields, binding=bad)

    def _historical_review_case(self) -> dict[str, object]:
        case = self._r14_case()
        review_path = case["content_review_path"]
        review = json.loads(review_path.read_text(encoding="utf-8"))
        binding = vm._DeferredHistoricalReviewBinding(
            action_id=case["action_id"],
            review_sha256=case["content_review_sha"],
            validation_sha256=case["validation_sha"],
            pending_receipt_sha256=case["pending_sha"],
            subject_sha256=vm.VM_DEFERRED_RESTORE_SHA256,
            authorization=case["authorization"],
            authorization_sha256=case["authorization_sha"],
            use_receipt=case["use"],
            use_receipt_sha256=case["use_sha"],
            failure=case["failure"],
            failure_sha256=case["failure_sha"],
        )
        return {
            "historical_review": review,
            "historical_sha": case["content_review_sha"],
            "historical_binding": binding,
            "historical_fields": {
                **vm._deferred_recovery_review_fields(
                    case["validation"], case["validation_path"], case["validation_sha"]),
                "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_CONTENT_REVIEW",
            },
            "historical_path": review_path,
        }

    def test_historical_substitution_mutation_and_conflicting_duplicate_fail(self) -> None:
        case = self._historical_review_case()
        binding = case["historical_binding"]
        call = lambda selected=case["historical_path"], bound=binding: (
            vm._deferred_bound_historical_review_json(
                selected, decision=vm.VM_R13_RECOVERY_REVIEW_DECISION,
                expected_record_type="R6_VM_DEFERRED_RESTORE_RECOVERY_CONTENT_REVIEW",
                expected_fields=case["historical_fields"], binding=bound))

        with self.subTest("unrelated old PASS substituted"):
            unrelated = dict(json.loads(case["historical_path"].read_text()))
            unrelated["reviewed_validation_sha256"] = "f" * 64
            self._write_json(case["historical_path"], unrelated)
            with self.assertRaises(RuntimeError):
                call()
            self._write_json(case["historical_path"], case["historical_review"])
        with self.subTest("wrong review type"):
            wrong_type = {**case["historical_review"], "record_type": "OTHER_REVIEW"}
            self._write_json(case["historical_path"], wrong_type)
            with self.assertRaisesRegex(RuntimeError, "does not PASS"):
                call()
            self._write_json(case["historical_path"], case["historical_review"])
        with self.subTest("modified bytes"):
            original = case["historical_path"].read_bytes()
            case["historical_path"].write_bytes(original + b" ")
            with self.assertRaisesRegex(RuntimeError, "SHA-256"):
                call()
            case["historical_path"].write_bytes(original)
        with self.subTest("conflicting duplicate disposition"):
            alias = case["historical_path"].with_name(
                "R6-VM-DEFERRED-RESTORE-RECOVERY-REVIEW-R13-conflict.json")
            self._write_json(alias, {
                **case["historical_review"],
                "decision": "FAIL — conflicting duplicate disposition",
            })
            with self.assertRaisesRegex(RuntimeError, "conflicting duplicate"):
                call()
        with self.subTest("historical review cannot satisfy current slot"):
            with self.assertRaisesRegex(RuntimeError, "does not PASS|stale"):
                vm._deferred_fresh_review_json(
                    case["historical_path"],
                    decision=vm.VM_R13_RECOVERY_REVIEW_DECISION,
                    expected_fields=case["historical_fields"])

    def _r14_case(self, *, fresh_review_age_seconds: float = 1,
                  include_fresh_review: bool = True) -> dict[str, object]:
        action_id = "16911d60bb634fb1a60ff6c5d36639be"
        validation_sha = "b" * 64
        pending_sha = "c" * 64
        authorization_sha = "d" * 64
        use_sha = "e" * 64
        review_path = self.storage / "R6-VM-DEFERRED-RESTORE-RECOVERY-REVIEW-R13-test.json"
        validation_path = self.storage / (
            "R6-VM-DEFERRED-RESTORE-RECOVERY-VALIDATION-R13-20261020T0651Z.json")
        now = time.time()
        subject_sha = vm.VM_DEFERRED_RESTORE_SHA256
        mount = {"mount_state": "MOUNTED_VERIFIED", "mount_point": str(self.vm_root),
                 "volume_uuid": "TEST-VM-VOLUME"}
        validation = {
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_VALIDATION",
            "status": "PASS_VALIDATED", "created_at_unix": now - 4_900,
            "pending_receipt_sha256": pending_sha,
            "old_authorization_sha256": "1" * 64,
            "old_predelete_review_sha256": "2" * 64,
            "r11_failure_sha256": "3" * 64,
            "r12_identity_contract_review_sha256": "4" * 64,
            "r12_quarantine_identity_review_sha256": "5" * 64,
            "branch_retirement_receipt_sha256": "6" * 64,
            "p1_admission_sha256": "7" * 64,
            "p1_verification_sha256": "8" * 64,
            "current_sha256": subject_sha,
            "current_byte_count": vm.VM_DEFERRED_RESTORE_LOGICAL_BYTES,
            "quarantine_path": str(
                self.vm_root / vm.VM_R13_EXPECTED_QUARANTINE_NAME),
            "quarantine_file_identity": {"device": 1, "inode": 2},
            "namespace_state": {"canonical_restore_absent": True},
            "vm_process_scan_before": {"scan_status": "PASS"},
            "vm_process_scan_after": {"scan_status": "PASS"},
            "restore_descriptor_scan_before": {"scan_clear": True},
            "restore_descriptor_scan_after": {"scan_clear": True},
            "current_controller_identity": {"controller": "r13"},
            "current_tests_sha256": "9" * 64,
            "vm_mount_identity": mount,
            "p1_restore_acquisition_historically_accepted": True,
            "p1_upstream_content_digest_validated": False,
            "p2_base_provisioning_accepted": False,
            "capacity_commitment_inactivation_attempted": False,
        }
        validation_raw = self._write_json(validation_path, validation)
        validation_sha = hashlib.sha256(validation_raw).hexdigest()
        pending_path = self.repo / "pending.json"
        pending_raw = b"immutable old PENDING bytes\n"
        pending_path.write_bytes(pending_raw)
        review_fields = vm._deferred_recovery_review_fields(
            validation, validation_path, validation_sha)
        historical_review = {
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_CONTENT_REVIEW",
            "status": "PASS", "decision": vm.VM_R13_RECOVERY_REVIEW_DECISION,
            "reviewer_identity": "GPT-6 Luna Max — Quarantined Restore Recovery Reviewer",
            "review_scope": "Read-only independent review of the exact signed full-hash recovery validation and linked R11/R12/PENDING evidence.",
            "model_identity": "GPT-6 Luna Max", "reviewed_at_unix": now - 4_700,
            "findings": [], **review_fields,
        }
        review_raw = self._write_json(review_path, historical_review)
        review_sha = hashlib.sha256(review_raw).hexdigest()
        authorization = {
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_AUTHORIZATION",
            "status": "AUTHORIZED", "action": "DELETE_EXACT_PENDING_QUARANTINED_RESTORE",
            "action_id": action_id, "single_use": True,
            "recovery_review_sha256": review_sha,
            "recovery_validation_sha256": validation_sha,
            "pending_receipt_sha256": pending_sha,
        }
        use_receipt = {
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_USE",
            "status": "CONSUMED_FOR_SINGLE_RECOVERY_ATTEMPT",
            "authorization_sha256": authorization_sha,
            "recovery_review_sha256": review_sha,
            "recovery_validation_sha256": validation_sha,
            "pending_receipt_sha256": pending_sha,
        }
        failure = {
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_POST_UNLINK_MEASUREMENT_FAILURE",
            "status": "FAIL_MEASUREMENT_RECONCILIATION", "created_at_unix": now - 20,
            "recovery_review_path": review_path.relative_to(self.repo).as_posix(),
            "recovery_review_sha256": review_sha,
            "recovery_validation_sha256": validation_sha,
            "pending_receipt_sha256": pending_sha,
            "recovery_authorization_sha256": authorization_sha,
            "recovery_use_receipt_sha256": use_sha,
            "recovery_use_receipt_path": f"R6-VM-DEFERRED-RESTORE-RECOVERY-USE-{action_id}.json",
            "active_marker_sha256": "a" * 64,
            "active_marker_committed_growth_bytes": 19_350_155_264,
            "boundary_full_sha256": subject_sha,
            "boundary_exact_byte_count": vm.VM_DEFERRED_RESTORE_LOGICAL_BYTES,
            "p1_admission_sha256": "7" * 64,
            "p1_verification_sha256": "8" * 64,
            "quarantine_file_identity": validation["quarantine_file_identity"],
            "post_attempt_namespace_entries": [],
            "recovery_path_review_sha256": "",
        }
        failure_path = vm.VM_R14_RECOVERY_MEASUREMENT_FAILURE_PATH
        failure_raw = self._write_json(failure_path, failure)
        authorization_path = self.repo / (
            "R6-VM-DEFERRED-RESTORE-RECOVERY-AUTHORIZATION-R13-20261020T065538Z.json")
        authorization_raw = self._write_json(authorization_path, authorization)
        use_path = self.repo / failure["recovery_use_receipt_path"]
        use_raw = self._write_json(use_path, use_receipt)
        root_info = os.stat(self.vm_root)
        root_identity = {
            "device": int(root_info.st_dev), "inode": int(root_info.st_ino),
            "owner_uid": int(root_info.st_uid), "mode": int(stat.S_IMODE(root_info.st_mode)),
        }
        mount = validation["vm_mount_identity"]
        ancestry = {
            "pending_receipt_path": "pending.json", "pending_receipt_sha256": pending_sha,
            "pending_root_identity": root_identity,
            "expected_quarantine_path": str(
                self.vm_root / vm.VM_R13_EXPECTED_QUARANTINE_NAME),
            "branch_retirement_receipt_path": "branch-retirement.json",
            "branch_retirement_receipt_sha256": "6" * 64,
            "old_authorization_path": "old-authorization.json",
            "old_authorization_sha256": "1" * 64,
            "old_predelete_review_path": "old-review.json",
            "old_predelete_review_sha256": "2" * 64,
            "r11_failure_path": "r11.json", "r11_failure_sha256": "3" * 64,
            "r12_identity_contract_review_path": "r12-identity.json",
            "r12_identity_contract_review_sha256": "4" * 64,
            "r12_quarantine_identity_review_path": "r12-quarantine.json",
            "r12_quarantine_identity_review_sha256": "5" * 64,
            "r12_controller_source_sha256": "a" * 64,
            "r12_test_source_sha256": "b" * 64,
        }
        path_review_path = self.storage / "path-review.json"
        path_review = {
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_RESTORE_RECOVERY_PATH_REVIEW",
            "status": "PASS", "decision": vm.VM_R13_PATH_REVIEW_DECISION,
            "reviewer_identity": "GPT-6 Luna Max — Deferred Restore Recovery Path Reviewer",
            "model_identity": "GPT-6 Luna Max", "reviewed_at_unix": now - 5_100,
            "reviewed_controller_identity": validation["current_controller_identity"],
            "reviewed_tests_sha256": validation["current_tests_sha256"],
            "reviewed_real_payload": False, "findings": [],
        }
        path_review_raw = self._write_json(path_review_path, path_review)
        failure["recovery_path_review_path"] = path_review_path.relative_to(self.repo).as_posix()
        failure["recovery_path_review_sha256"] = hashlib.sha256(path_review_raw).hexdigest()
        failure_raw = self._write_json(failure_path, failure)
        controller_identity = {"vm_capacity_controller_sha256": "1" * 64}
        tests_sha = "2" * 64
        with mock.patch.object(vm, "_deferred_restore_recovery_ancestry",
                               return_value=ancestry):
            r14_fields = vm._r14_resolution_review_fields(
                failure_path, vm.VM_R14_RECOVERY_MEASUREMENT_FAILURE_SHA256,
                failure, controller_identity, tests_sha)
        r14_review_path = self.storage / "r14-review.json"
        signed_records = {
            str(Path(failure_path).resolve()): (
                failure, failure_raw, vm.VM_R14_RECOVERY_MEASUREMENT_FAILURE_SHA256, (1, 1)),
            str(authorization_path.resolve()): (authorization, authorization_raw, authorization_sha, (1, 2)),
            str(validation_path.resolve()): (validation, validation_raw, validation_sha, (1, 3)),
            str(use_path.resolve()): (use_receipt, use_raw, use_sha, (1, 4)),
            str(pending_path.resolve()): ({"status": "PENDING"}, pending_raw, pending_sha, (1, 5)),
        }
        case = {
            "action_id": action_id, "authorization": authorization,
            "authorization_path": authorization_path, "authorization_sha": authorization_sha,
            "validation": validation, "validation_path": validation_path,
            "validation_sha": validation_sha, "use": use_receipt,
            "use_path": use_path, "use_sha": use_sha, "failure": failure,
            "failure_path": failure_path,
            "failure_sha": vm.VM_R14_RECOVERY_MEASUREMENT_FAILURE_SHA256,
            "failure_raw": failure_raw, "path_review_path": path_review_path,
            "content_review_path": review_path, "content_review_sha": review_sha,
            "r14_review_path": r14_review_path, "pending_path": pending_path,
            "pending_raw": pending_raw, "pending_sha": pending_sha,
            "root_identity": root_identity, "mount": mount, "ancestry": ancestry,
            "marker": {"committed_growth_bytes": 19_350_155_264, "session_id": "TEST-SESSION"},
            "controller_identity": controller_identity, "tests_sha": tests_sha,
            "signed_records": signed_records,
            "receipt_path": self.repo / "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-R14-test.json",
        }
        if include_fresh_review:
            r14_review = {
                "schema_version": 1,
                "record_type": "R6_VM_DEFERRED_RESTORE_RESOLUTION_REVIEW",
                "status": "PASS", "decision": vm.VM_R14_RESOLUTION_REVIEW_DECISION,
                "reviewer_identity": "GPT-6 Luna Max — R14 Post-Unlink Resolution Reviewer",
                "model_identity": "GPT-6 Luna Max",
                "reviewed_at_unix": now - fresh_review_age_seconds,
                "findings": [], **r14_fields,
            }
            self._write_json(r14_review_path, r14_review)
        return case

    def test_r14_resolves_exact_aged_r13_review_only_with_fresh_current_review(self) -> None:
        case = self._r14_case()
        result = self._run_r14_resolution(case)
        self.assertEqual(
            result["classification"],
            "PENDING_TRANSACTION_RESOLVED_BY_VERIFIED_UNLINK_WITH_MEASUREMENT_CAVEAT")
        self.assertTrue(result["local_restore_payload_retired"])
        self.assertEqual(result["RESTORE_RETIREMENT_RECLAIMED_BYTES"], None)
        self.assertEqual(result["reclaimed_bytes_exact"], None)
        self.assertEqual(
            result["immediate_reclaim_measurement_status"],
            "REJECTED_APFS_CROSSCHECK_DISAGREEMENT")
        self.assertTrue(result["future_vm_work_requires_fresh_p1"])
        self.assertFalse(result["p2_base_provisioning_accepted"])
        self.assertFalse(result["capacity_commitment_inactivation_attempted"])
        self.assertEqual(result["active_marker_committed_growth_bytes"], 19_350_155_264)
        self.assertEqual(case["pending_path"].read_bytes(), case["pending_raw"])
        self.assertEqual(json.loads(case["receipt_path"].read_text())["pending_receipt_remains_status"],
                         "PENDING")

    def test_r14_missing_or_stale_current_review_fails_without_receipt_or_pending_mutation(self) -> None:
        for label, age, include_review in (
                ("missing", 1, False),
                ("stale", vm.VM_DEFERRED_REVIEW_MAX_AGE_SECONDS + 1, True)):
            with self.subTest(label=label):
                case = self._r14_case(
                    fresh_review_age_seconds=age, include_fresh_review=include_review)
                review_path = (self.storage / "missing-r14-review.json"
                               if label == "missing" else case["r14_review_path"])
                with self.assertRaises((OSError, RuntimeError)):
                    self._run_r14_resolution(case, review_path=review_path)
                self.assertFalse(case["receipt_path"].exists())
                self.assertEqual(case["pending_path"].read_bytes(), case["pending_raw"])

        with self.subTest("wrong current review type"):
            case = self._r14_case()
            review = json.loads(case["r14_review_path"].read_text(encoding="utf-8"))
            review["record_type"] = "R6_VM_DEFERRED_RESTORE_CONTENT_REVIEW"
            self._write_json(case["r14_review_path"], review)
            with self.assertRaisesRegex(RuntimeError, "exact bound evidence"):
                self._run_r14_resolution(case)
            self.assertFalse(case["receipt_path"].exists())
            self.assertEqual(case["pending_path"].read_bytes(), case["pending_raw"])

    def test_r14_replay_and_conflicting_terminal_resolution_fail_closed(self) -> None:
        for name in (
                "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-existing.json",
                "R6-VM-DEFERRED-RESTORE-RETIREMENT-RESOLUTION-conflict.json"):
            with self.subTest(name=name):
                case = self._r14_case()
                (self.repo / name).write_text("prior terminal artifact", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "terminal resolution"):
                    self._run_r14_resolution(case)
                self.assertFalse(case["receipt_path"].exists())
                self.assertEqual(case["pending_path"].read_bytes(), case["pending_raw"])


if __name__ == "__main__":
    unittest.main()
