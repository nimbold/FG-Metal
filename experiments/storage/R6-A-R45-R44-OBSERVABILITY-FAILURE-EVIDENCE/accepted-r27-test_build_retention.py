#!/usr/bin/env python3
"""Synthetic-only tests for fail-closed build retention; never create graphics builds."""
from __future__ import annotations

import hashlib
import base64
import dataclasses
import errno
import io
import json
import os
import platform
import plistlib
import re
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import build_retention as retention
import storage_policy
import storage_inventory
import storage_gate0

_ORIGINAL_REQUIRE_NO_LIVE_PROCESS = retention._require_no_live_process
_ORIGINAL_MEASURE_PROJECT_USAGE = storage_policy.measure_project_usage
_ORIGINAL_ATTACH_SESSION_WORKSPACE = retention._attach_session_workspace
_ORIGINAL_VERIFY_SESSION_WORKSPACE_MOUNT = retention._verify_session_workspace_mount
_ORIGINAL_DETACH_SESSION_WORKSPACE = retention._detach_session_workspace
_ORIGINAL_ATTACH_SESSION_WORKSPACE_READONLY = retention._attach_session_workspace_readonly
_ORIGINAL_VERIFY_SESSION_WORKSPACE_READONLY = retention._verify_session_workspace_readonly
_ORIGINAL_DETACH_SESSION_WORKSPACE_READONLY = retention._detach_session_workspace_readonly
_ORIGINAL_CONTAIN_SESSION_PROCESSES = retention._contain_session_processes
_ORIGINAL_CAPTURE_IDENTITY = retention._capture_identity
_ORIGINAL_PROTECTED_EVIDENCE_ROOTS = tuple(retention.PROTECTED_EVIDENCE_ROOTS)


def _projected_without_vm_commitment(**inputs):
    empty_commitment = {"committed_growth_bytes": 0, "marker_sha256": None,
                        "marker_identity": None}
    with mock.patch.object(storage_policy, "_vm_capacity_commitment_snapshot",
                           return_value=empty_commitment):
        return storage_policy.projected_storage_admission(**inputs)


def _with_synthetic_buildlease_runtime_gate(test_method):
    """Use only synthetic command/profile fixtures for isolated controller tests.

    Production keeps both the readiness gate and retired host sandbox runner
    fail-closed. Decorated unit tests replace those two calls only while their
    assertions run; their Popen fixtures do not launch the returned command.
    """
    def synthetic_session_command(command, build_root, environment, *,
                                  write_root=None, temporary_directory=None):
        selected_write_root = Path(write_root or build_root).resolve()
        controller_root = (Path(build_root).parent / retention.BUILD_SESSION_DIRECTORY).resolve()

        def quote(path):
            return '"' + str(path).replace("\\", "\\\\").replace('"', '\\"') + '"'

        profile = "\n".join((
            "(version 1)", "(deny default)", "(allow process*)", "(allow file-read*)",
            f"(allow file-write* (subpath {quote(selected_write_root)}))",
            f"(deny file-write* (subpath {quote(controller_root)}))",
        ))
        selected_environment = dict(environment)
        selected_temporary = Path(
            temporary_directory or (selected_write_root / ".tmp")).resolve(strict=False)
        selected_home = (selected_temporary if write_root is not None
                         else (Path(build_root) / ".home").resolve(strict=False))
        selected_environment.update({
            "HOME": str(selected_home),
            "TMPDIR": str(selected_temporary),
            "TMP": str(selected_temporary),
            "TEMP": str(selected_temporary),
            "XDG_CACHE_HOME": str(selected_home / ".cache"),
            "XDG_CONFIG_HOME": str(selected_home / ".config"),
            "XDG_DATA_HOME": str(selected_home / ".local" / "share"),
        })
        launcher = "/usr/bin/sandbox-exec"
        return ([launcher, "-p", profile, *list(command)],
                selected_environment, launcher)

    @wraps(test_method)
    def run_with_synthetic_gate(self, *args, **kwargs):
        with (mock.patch.object(retention, "_require_buildlease_vm_runtime", return_value=None),
              mock.patch.object(retention, "_sandbox_session_command",
                                side_effect=synthetic_session_command)):
            return test_method(self, *args, **kwargs)
    return run_with_synthetic_gate


def _synthetic_native_toolchain_observation(request: retention.NativeMoltenVKBuildRequest) -> dict[str, str]:
    return {
        "xcode_version_string": request.xcode_version_string,
        "clang_version_string": request.clang_version_string,
        "clang_binary_sha256": request.clang_binary_sha256,
        "sdk_version_string": request.sdk_version_string,
        "linker_version_string": request.linker_version_string,
        "toolchain_identity_sha256": request.toolchain_identity_sha256,
    }


class HostBuildLeaseP2ExclusionScanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-p2-host-lease-scan-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.lease_root = Path(self.temp.name) / "build-leases"
        self.lease_root.mkdir(mode=0o700)
        lock_path = self.lease_root / retention.BUILD_LOCK
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(lock_fd)
        self.patches = [
            mock.patch.object(retention, "LEASE_ROOT", self.lease_root),
            mock.patch.object(retention, "_key", return_value=b"synthetic-build-lease-key"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_lease(self, build_id: str, state: str) -> bytes:
        retention._store_lease({
            "schema_version": 1,
            "build_id": build_id,
            "path": str(Path(self.temp.name) / "builds" / build_id),
            "owner_experiment": "synthetic-test",
            "kind": "reference",
            "created_at_unix": time.time(),
            "state": state,
            "session_v3": {
                "schema_version": retention.BUILD_SESSION_SCHEMA_VERSION,
                "executor_mode": retention.BUILD_SESSION_EXECUTOR_MODE,
                "state": "ACTIVE" if state == "ACTIVE" else "ACCEPTED",
            },
        })
        return (self.lease_root / f"{build_id}.json").read_bytes()

    def test_active_host_buildlease_blocks_read_only_p2_scan(self) -> None:
        raw = self._write_lease("active-host-v3", "ACTIVE")

        with mock.patch.object(retention, "status_snapshot", side_effect=AssertionError), \
                mock.patch.object(retention, "_retire_pending_locked", side_effect=AssertionError):
            blockers = retention.host_buildlease_p2_admission_blockers()

        self.assertEqual(len(blockers), 1)
        self.assertIn("active Host BuildLease prevents P2 admission", blockers[0])
        self.assertEqual((self.lease_root / "active-host-v3.json").read_bytes(), raw)

    def test_p2_guard_holds_retention_lock_for_its_commit_callback(self) -> None:
        with retention.host_buildlease_p2_admission_guard() as blockers:
            self.assertEqual(blockers, [])
            competing_fd = os.open(self.lease_root / retention.BUILD_LOCK, os.O_RDWR)
            try:
                with self.assertRaises(BlockingIOError):
                    retention.fcntl.flock(
                        competing_fd, retention.fcntl.LOCK_EX | retention.fcntl.LOCK_NB)
            finally:
                os.close(competing_fd)

    def test_missing_or_unsafe_host_lease_authority_fails_closed(self) -> None:
        missing_root = Path(self.temp.name) / "missing-build-leases"
        with mock.patch.object(retention, "LEASE_ROOT", missing_root):
            self.assertTrue(any("state is unknown" in item
                                for item in retention.host_buildlease_p2_admission_blockers()))

        lock_path = self.lease_root / retention.BUILD_LOCK
        lock_path.unlink()
        self.assertTrue(any("state is unknown" in item
                            for item in retention.host_buildlease_p2_admission_blockers()))
        lock_path.write_text("", encoding="utf-8")
        lock_path.chmod(0o600)

    def test_host_reservation_publishes_active_record_while_holding_shared_lock(self) -> None:
        source = Path(retention.__file__).read_text(encoding="utf-8")
        start = source.index("    def reserve(self) -> Path | None:",
                             source.index("class BuildLease:"))
        end = source.index("    def _release_allocation_lock(self)", start)
        reserve = source[start:end]
        lock_acquisition = reserve.index(
            "self.storage_allocation_lock_file = _acquire_storage_allocation_lock()")
        active_record_write = reserve.index("_store_lease(lease)")
        successful_unlock = reserve.rindex("self._release_allocation_lock()")

        self.assertLess(lock_acquisition, active_record_write)
        self.assertLess(active_record_write, successful_unlock)

        adopt_start = source.index("def adopt_legacy(")
        adopt_end = source.index("def _adopt_legacy_under_storage_allocation_lock(", adopt_start)
        adoption = source[adopt_start:adopt_end]
        self.assertLess(adoption.index("_acquire_storage_allocation_lock()"),
                        adoption.index("projected_storage_admission("))
        self.assertIn("peak_additional_bytes=0", adoption)
        self.assertIn("_adopt_legacy_under_storage_allocation_lock(", adoption)

    def test_legacy_adoption_reprojects_synthetic_vm_commitment_under_shared_lock(self) -> None:
        commitment = 19_350_155_264
        allocation_lock = object()
        trace = {"held": False, "projection": None}
        original_projection = storage_policy.projected_storage_admission

        def acquire_allocation_lock():
            self.assertFalse(trace["held"])
            trace["held"] = True
            return allocation_lock

        def release_allocation_lock(selected):
            self.assertIs(selected, allocation_lock)
            self.assertTrue(trace["held"])
            trace["held"] = False

        def gate_blockers(*, active_experiment_id):
            self.assertTrue(trace["held"])
            self.assertEqual(active_experiment_id, "synthetic-test")
            return []

        def complete_adoption(path, **kwargs):
            self.assertTrue(trace["held"])
            self.assertEqual(kwargs["kind"], "reference")
            return {"status": "SYNTHETIC_ADMITTED", "path": str(path)}

        def checked_projection(**kwargs):
            self.assertTrue(trace["held"])
            projection = original_projection(**kwargs)
            trace["projection"] = projection
            return projection

        with mock.patch.object(storage_policy, "_acquire_storage_allocation_lock",
                               side_effect=acquire_allocation_lock), \
                mock.patch.object(storage_policy, "_release_storage_allocation_lock",
                                  side_effect=release_allocation_lock), \
                mock.patch.object(retention, "_gate_blockers", side_effect=gate_blockers), \
                mock.patch.object(storage_policy, "disk_free_bytes", return_value=70 * retention.GIB), \
                mock.patch.object(storage_policy, "measure_project_usage",
                                  return_value={"allocated_inode_deduplicated_bytes": 0}), \
                mock.patch.object(storage_policy, "conservative_project_usage_bytes", return_value=0), \
                mock.patch.object(storage_policy, "projected_storage_admission",
                                  side_effect=checked_projection), \
                mock.patch.object(storage_policy, "_vm_capacity_commitment_snapshot", return_value={
                    "committed_growth_bytes": commitment,
                    "marker_sha256": "a" * 64,
                    "marker_identity": {"device": 1, "inode": 2},
                }), \
                mock.patch.object(retention, "_adopt_legacy_under_storage_allocation_lock",
                                  side_effect=complete_adoption):
            result = retention.adopt_legacy(
                Path("/synthetic/legacy-root"), kind="reference",
                owner_experiment="synthetic-test", purpose="capacity binding test")

        self.assertFalse(trace["held"])
        self.assertEqual(result["status"], "SYNTHETIC_ADMITTED")
        self.assertEqual(trace["projection"]["peak_additional_bytes"], 0)
        self.assertEqual(trace["projection"]["shared_capacity_commitment_bytes"], commitment)
        self.assertEqual(trace["projection"]["projected_free_bytes"], 70 * retention.GIB - commitment)

    def test_completed_lease_does_not_block_and_unknown_state_fails_closed(self) -> None:
        self._write_lease("completed-host-v3", "COMPLETED")
        self.assertEqual(retention.host_buildlease_p2_admission_blockers(), [])

        self._write_lease("unknown-state", "UNRECOGNIZED")
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertTrue(any("state is unknown" in item for item in blockers))

    def test_malformed_unsigned_symlink_and_unexpected_entries_fail_closed(self) -> None:
        malformed = self.lease_root / "malformed.json"
        malformed.write_text("not json\n", encoding="utf-8")
        malformed.chmod(0o600)
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertTrue(any("state is unknown" in item for item in blockers))

        malformed.unlink()
        (self.lease_root / "outside.json").write_text("{}\n", encoding="utf-8")
        (self.lease_root / "outside.json").chmod(0o600)
        (self.lease_root / "symlink.json").symlink_to(self.lease_root / "outside.json")
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertTrue(any("state is unknown" in item for item in blockers))

        (self.lease_root / "symlink.json").unlink()
        (self.lease_root / "outside.json").unlink()
        (self.lease_root / "unexpected-entry").write_text("unknown\n", encoding="utf-8")
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertTrue(any("unexpected registry entry" in item for item in blockers))


class _SubprocessPopenProxy:
    """Override only the controller's direct Popen calls, not subprocess.run internals."""

    def __init__(self, popen: mock.Mock):
        self.Popen = popen

    def __getattr__(self, name: str) -> object:
        return getattr(subprocess, name)


class BuildRetentionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-retention-test-", dir="/private/tmp")
        base = Path(self.temp.name)
        self.repo = base / "repo"
        self.cache = base / "cache"
        self.repo.mkdir(mode=0o700)
        self.cache.mkdir(mode=0o700)
        self.dual_package_bin = self.repo / "dual-package-tools"
        self.dual_package_bin.mkdir(mode=0o700)
        self.synthetic_mingw_root = self.repo / "mingw/toolchain-x86_64"
        self.synthetic_dual_path = os.pathsep.join((
            "/usr/bin", "/bin", "/usr/sbin", "/sbin",
            str(self.synthetic_mingw_root / "bin"), str(self.dual_package_bin)))
        self.manifests = self.repo / "experiments/storage/manifests"
        self.manifests.mkdir(parents=True, mode=0o700)
        self.build_manifests = self.repo / "experiments/storage/build-manifests"
        self.build_manifests.mkdir(parents=True, mode=0o700)
        self.gate_attestation = self.repo / "storage-gate0-attestation.json"
        self.gate_attestation.write_text("synthetic-attestation", encoding="utf-8")
        self.runner_file = self.repo / "synthetic-runner.py"
        self.runner_file.write_text("# synthetic runner\n", encoding="utf-8")
        temp_roots = base / "tmp-roots"
        home = base / "home"
        temp_roots.mkdir()
        home.mkdir()
        self.synthetic_dual_tool_pins: dict[str, dict[str, str]] = {}
        self.synthetic_dual_runtime_pins: dict[str, dict[str, str]] = {}
        self.synthetic_dual_host_dependency_pins: dict[str, dict[str, str]] = {}
        self.synthetic_dual_host_profile: dict[str, str] = {}
        self.synthetic_dual_meson_python_shebang = str(self.repo / "synthetic-meson-python3.14")
        self.native_toolchain_capture_patch = mock.patch.object(
            retention, "_capture_session_native_toolchain_selection",
            side_effect=self._synthetic_native_toolchain_selection)
        self.addCleanup(self.temp.cleanup)
        self.patches = [
            mock.patch.object(retention, "REPO", self.repo),
            mock.patch.object(retention, "CACHE_ROOT", self.cache),
            mock.patch.object(retention, "BUILD_ROOT", self.cache / "builds"),
            mock.patch.object(retention, "ARTIFACT_ROOT", self.cache / "artifacts/sha256"),
            mock.patch.object(retention, "STORAGE_ROOT", self.repo / "experiments/storage"),
            mock.patch.object(retention, "LEASE_ROOT", self.repo / "experiments/storage/build-leases"),
            mock.patch.object(retention, "MANIFEST_ROOT", self.build_manifests),
            mock.patch.object(retention, "RECEIPT_ROOT", self.repo / "experiments/storage/build-cleanup-receipts"),
            mock.patch.object(retention, "LEGACY_BUILD_ROOTS", ()),
            mock.patch.object(retention, "PROTECTED_EVIDENCE_ROOTS", ()),
            mock.patch.object(retention, "TEMP_ROOT", temp_roots),
            mock.patch.object(retention, "HOME", home),
            mock.patch.object(retention, "BUILD_SESSION_DUAL_GRAPH_TOOL_PINS",
                              self.synthetic_dual_tool_pins),
            mock.patch.object(retention, "BUILD_SESSION_DUAL_GRAPH_PATH",
                              self.synthetic_dual_path),
            mock.patch.object(retention, "BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS",
                              self.synthetic_dual_runtime_pins),
            mock.patch.object(retention, "BUILD_SESSION_DUAL_GRAPH_HOST_DEPENDENCY_PINS",
                              self.synthetic_dual_host_dependency_pins),
            mock.patch.object(retention, "BUILD_SESSION_DUAL_GRAPH_NATIVE_HOST_PROFILE",
                              self.synthetic_dual_host_profile),
            self.native_toolchain_capture_patch,
            mock.patch.object(retention, "BUILD_SESSION_DUAL_GRAPH_MESON_PYTHON_SHEBANG",
                              self.synthetic_dual_meson_python_shebang),
            mock.patch.object(retention, "_require_no_live_process", return_value=None),
            mock.patch.object(retention, "_key", return_value=b"r" * 32),
            mock.patch.object(storage_policy, "REPO", self.repo),
            mock.patch.object(storage_policy, "CACHE", self.cache),
            mock.patch.object(storage_policy, "MANIFEST_SIGNING_KEY_PATH",
                              self.cache / ".storage-manifest-signing-key"),
            mock.patch.object(storage_policy, "INVENTORY_PATH", self.repo / "missing-inventory.json"),
            mock.patch.object(storage_policy, "GATE0_ATTESTATION_PATH", self.gate_attestation),
            mock.patch.object(storage_policy, "_vm_capacity_commitment_snapshot", return_value={
                "committed_growth_bytes": 0, "marker_sha256": None, "marker_identity": None,
            }),
            mock.patch.object(storage_policy, "_experiment_manifest_path",
                              side_effect=lambda experiment_id: self.manifests / f"{experiment_id}.json"),
            mock.patch.object(storage_policy, "_verify_manifest_signature", return_value=True),
            mock.patch.object(storage_policy, "_validated_manifest_estimates", side_effect=self._manifest_estimates),
            mock.patch.object(storage_policy, "disk_free_bytes", return_value=100 * retention.GIB),
            mock.patch.object(storage_policy, "measure_project_usage",
                              return_value={"allocated_inode_deduplicated_bytes": 0}),
            mock.patch.object(retention, "_gate_blockers", return_value=[]),
            mock.patch.object(retention, "_capture_identity", side_effect=self._capture_identity_for_build),
            mock.patch.object(retention, "_attach_session_workspace",
                              side_effect=self._fake_attach_session_workspace),
            mock.patch.object(retention, "_verify_session_workspace_mount",
                              side_effect=self._fake_verify_session_workspace_mount),
            mock.patch.object(retention, "_detach_session_workspace",
                              side_effect=self._fake_detach_session_workspace),
            mock.patch.object(retention, "_attach_session_workspace_readonly",
                              side_effect=self._fake_attach_session_workspace_readonly),
            mock.patch.object(retention, "_verify_session_workspace_readonly",
                              side_effect=self._fake_verify_session_workspace_readonly),
            mock.patch.object(retention, "_detach_session_workspace_readonly",
                              side_effect=self._fake_detach_session_workspace_readonly),
            mock.patch.object(retention, "_contain_session_processes",
                              side_effect=lambda session_id, **_kwargs: {
                                  "session_id": session_id, "members_seen": [session_id],
                                  "survivors": [], "leader_held_unreaped": True}),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self._write_owner_manifest()

    @staticmethod
    def _fake_attach_session_workspace(root: Path, capacity_bytes: int) -> dict[str, object]:
        root = Path(root)
        image = root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
        workspace.mkdir(mode=0o700)
        image.write_bytes(b"synthetic sparseimage backing\n")
        root_info = root.stat(follow_symlinks=False)
        fs = os.statvfs(root)
        workspace_info = workspace.stat(follow_symlinks=False)
        image_info = image.stat(follow_symlinks=False)
        return {"image_path": str(image), "image_device": image_info.st_dev,
                "image_inode": image_info.st_ino, "mountpoint": str(workspace),
                "mountpoint_dir_device": workspace_info.st_dev,
                "mountpoint_dir_inode": workspace_info.st_ino,
                "mountpoint_attached_mode": stat.S_IMODE(workspace_info.st_mode),
                "device_node": "synthetic-disk", "volume_uuid": "synthetic-volume",
                "device": workspace_info.st_dev, "filesystem_id": getattr(fs, "f_fsid", None),
                "capacity_bytes": capacity_bytes, "requested_capacity_bytes": capacity_bytes,
                "filesystem_type": "hfs", "disk_device": "synthetic-image-device",
                "hdiutil_path": "/synthetic/hdiutil", "hdiutil_sha256": "0" * 64,
                "diskutil_path": "/synthetic/diskutil", "diskutil_sha256": "0" * 64,
                "attached": True, "attached_at_unix": time.time(),
                "root_device": root_info.st_dev}

    @staticmethod
    def _fake_verify_session_workspace_mount(root: Path, identity: dict[str, object]) -> dict[str, object]:
        root = Path(root)
        image = root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
        image_info = image.stat(follow_symlinks=False)
        workspace_info = workspace.stat(follow_symlinks=False)
        if (not stat.S_ISREG(image_info.st_mode) or image_info.st_nlink != 1
                or (image_info.st_dev, image_info.st_ino)
                    != (identity.get("image_device"), identity.get("image_inode"))
                or workspace_info.st_dev != identity.get("device")):
            raise retention.RetentionError("synthetic workspace mount identity changed")
        return dict(identity)

    @staticmethod
    def _fake_detach_session_workspace(root: Path, identity: dict[str, object] | None) -> dict[str, object]:
        root = Path(root)
        if identity is not None and identity.get("attached") is True:
            BuildRetentionTests._fake_verify_session_workspace_mount(root, identity)
        detached = {**(dict(identity) if identity is not None else {}),
                    "attached": False, "detached_at_unix": time.time()}
        if identity is not None:
            BuildRetentionTests._stash_synthetic_workspace_contents(root)
            workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
            workspace.chmod(0o500)
            image_identity = retention._session_image_content_identity(
                Path(str(identity["image_path"])))
            if (image_identity["path"] != identity.get("image_path")
                    or (image_identity["device"], image_identity["inode"])
                    != (identity.get("image_device"), identity.get("image_inode"))):
                raise AssertionError("synthetic detached image identity changed from its attached identity")
            detached["detached_image_identity"] = image_identity
            return BuildRetentionTests._verify_synthetic_detached_workspace(root, detached)
        return detached

    @staticmethod
    def _synthetic_workspace_contents_path(root: Path) -> Path:
        root = Path(root)
        return Path(retention.TEMP_ROOT).parent / f".{root.name}.synthetic-image-contents"

    @staticmethod
    def _stash_synthetic_workspace_contents(root: Path) -> None:
        root = Path(root)
        workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
        contents = BuildRetentionTests._synthetic_workspace_contents_path(root)
        if contents.exists():
            raise AssertionError("synthetic workspace contents were already detached")
        contents.mkdir(mode=0o700)
        for entry in workspace.iterdir():
            os.replace(entry, contents / entry.name)

    @staticmethod
    def _restore_synthetic_workspace_contents(root: Path) -> None:
        root = Path(root)
        workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
        contents = BuildRetentionTests._synthetic_workspace_contents_path(root)
        if not contents.is_dir():
            raise AssertionError("synthetic detached workspace contents are unavailable")
        workspace.chmod(0o700)
        for entry in contents.iterdir():
            os.replace(entry, workspace / entry.name)
        contents.rmdir()

    @staticmethod
    def _verify_synthetic_detached_workspace(
            root: Path, identity: dict[str, object]) -> dict[str, object]:
        with (mock.patch.object(retention, "_session_hdiutil",
                                return_value=(Path("/synthetic/hdiutil"), "0" * 64)),
              mock.patch.object(retention, "_session_diskutil",
                                return_value=(Path("/synthetic/diskutil"), "0" * 64)),
              mock.patch.object(retention, "_session_image_row", return_value=None)):
            return retention._verify_session_workspace_detached(root, identity)

    @staticmethod
    def _fake_attach_session_workspace_readonly(
            root: Path, detached_identity: dict[str, object]) -> dict[str, object]:
        root = Path(root)
        detached = BuildRetentionTests._verify_synthetic_detached_workspace(root, detached_identity)
        expected_image = detached.get("detached_image_identity")
        if not isinstance(expected_image, dict):
            raise AssertionError("synthetic detached image lacks its content identity")
        image = root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        image_identity = retention._session_image_content_identity(image)
        if image_identity != expected_image:
            raise AssertionError("synthetic read-only reattach changed the detached image identity")
        BuildRetentionTests._restore_synthetic_workspace_contents(root)
        workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
        workspace_info = workspace.stat(follow_symlinks=False)
        fs = os.statvfs(workspace)
        readonly = {**detached,
                    "image_size_bytes": image_identity["size_bytes"],
                    "image_sha256": image_identity["sha256"],
                    "detached_image_identity": dict(expected_image),
                    "device": workspace_info.st_dev,
                    "filesystem_id": getattr(fs, "f_fsid", None),
                    "readonly": True, "attached": True,
                    "diskutil_writable": False, "hdiutil_writeable": False,
                    "attached_at_unix": time.time()}
        BuildRetentionTests._fake_verify_session_workspace_readonly(root, readonly)
        return readonly

    @staticmethod
    def _fake_verify_session_workspace_readonly(
            root: Path, identity: dict[str, object]) -> dict[str, object]:
        root = Path(root)
        image = root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
        expected_image = identity.get("detached_image_identity")
        image_info = image.stat(follow_symlinks=False)
        workspace_info = workspace.stat(follow_symlinks=False)
        fs = os.statvfs(workspace)
        observed_image = retention._session_image_content_identity(image)
        if (identity.get("readonly") is not True or identity.get("attached") is not True
                or identity.get("image_path") != str(image)
                or (image_info.st_dev, image_info.st_ino)
                    != (identity.get("image_device"), identity.get("image_inode"))
                or not isinstance(expected_image, dict) or observed_image != expected_image
                or identity.get("image_size_bytes") != observed_image["size_bytes"]
                or identity.get("image_sha256") != observed_image["sha256"]
                or identity.get("mountpoint") != str(workspace)
                or (workspace_info.st_dev, workspace_info.st_ino)
                    != (identity.get("mountpoint_dir_device"), identity.get("mountpoint_dir_inode"))
                or workspace_info.st_dev != identity.get("device")
                or getattr(fs, "f_fsid", None) != identity.get("filesystem_id")
                or identity.get("capacity_bytes") != identity.get("requested_capacity_bytes")
                or stat.S_IMODE(workspace_info.st_mode) != identity.get("mountpoint_attached_mode")
                or identity.get("diskutil_writable") is not False
                or identity.get("hdiutil_writeable") is not False):
            raise AssertionError("synthetic read-only attachment identity or image hash changed")
        return dict(identity)

    @staticmethod
    def _fake_detach_session_workspace_readonly(
            root: Path, identity: dict[str, object]) -> dict[str, object]:
        root = Path(root)
        BuildRetentionTests._fake_verify_session_workspace_readonly(root, identity)
        BuildRetentionTests._stash_synthetic_workspace_contents(root)
        (root / retention.BUILD_SESSION_WORKSPACE_NAME).chmod(0o500)
        detached_image = retention._session_image_content_identity(
            root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME)
        if detached_image != identity.get("detached_image_identity"):
            raise AssertionError("synthetic read-only detach changed the accepted image identity")
        detached = {**dict(identity), "attached": False, "detached_at_unix": time.time(),
                    "detached_image_identity": detached_image}
        return BuildRetentionTests._verify_synthetic_detached_workspace(root, detached)

    def _protected_evidence_fixture(self) -> dict[str, Path]:
        """Install synthetic parent-bound roots and exact authority for one protection test."""
        relative_roots = {
            "R6-A-BUILD-1-EXPLORATORY": (
                "r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/build.x86_64"),
            "R6-A-BUILD-1-FINAL": (
                "r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/build.x86_64-final"),
            "R6-A-BUILD-1-GENERATED": (
                "r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/evidence/generated/files"),
            retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID: (
                "r6/workspaces/R6-A-ACCOUNTING-SNAPSHOT-1-BUILD-WORKSPACE/build.x86_64"),
        }
        roots = {identity: self.repo / relative for identity, relative in relative_roots.items()}
        (roots["R6-A-BUILD-1-EXPLORATORY"] / "meson-private").mkdir(parents=True)
        (roots["R6-A-BUILD-1-EXPLORATORY"] / ".ninja_log").write_bytes(b"preflight log\n")
        (roots["R6-A-BUILD-1-EXPLORATORY"] / "meson-private/meson-log.txt").write_bytes(b"exploratory\n")
        (roots["R6-A-BUILD-1-FINAL"] / "meson-private").mkdir(parents=True)
        (roots["R6-A-BUILD-1-FINAL"] / "nested").mkdir(parents=True)
        (roots["R6-A-BUILD-1-FINAL"] / "build.ninja").write_bytes(b"ninja rules\n")
        (roots["R6-A-BUILD-1-FINAL"] / "meson-private/build.dat").write_bytes(b"build metadata\n")
        (roots["R6-A-BUILD-1-FINAL"] / "nested/.ninja_deps").write_bytes(b"deps\n")
        (roots["R6-A-BUILD-1-GENERATED"] / "include").mkdir(parents=True)
        (roots["R6-A-BUILD-1-GENERATED"] / "generated.c").write_bytes(b"generated source\n")
        (roots["R6-A-BUILD-1-GENERATED"] / "include/generated.h").write_bytes(b"generated header\n")
        rejected_r5_root = roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID]
        (rejected_r5_root / "tests/present-accounting").mkdir(parents=True)
        (rejected_r5_root / retention.REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE).write_bytes(
            b"synthetic historical R5 executable\n")
        (rejected_r5_root / "build.ninja").write_bytes(b"synthetic historical build graph\n")

        root_rows = {identity: retention._protected_evidence_root_inventory(path)[0]
                     for identity, path in roots.items()}
        artifacts: list[dict[str, object]] = []
        for identity, rows in root_rows.items():
            if identity == retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID:
                continue
            relative_root = relative_roots[identity]
            for row in rows:
                if row["kind"] == "file":
                    artifacts.append({"path": f"{relative_root}/{row['path']}",
                                      "size_bytes": row["size_bytes"],
                                      "sha256": row["sha256"],
                                      "role": "synthetic BUILD-1 evidence"})
        final_file_rows = [row for row in root_rows["R6-A-BUILD-1-FINAL"] if row["kind"] == "file"]
        manifest_path = self.repo / retention.BUILD1_DIRECTORY_MANIFEST_RELATIVE
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "build_id": retention.BUILD1_ID,
            "build_directory": str(roots["R6-A-BUILD-1-FINAL"]),
            "directory_count": sum(row["kind"] == "directory"
                                    for row in root_rows["R6-A-BUILD-1-FINAL"]),
            "file_entry_count": len(final_file_rows),
            "total_regular_file_bytes": sum(row["size_bytes"] for row in final_file_rows),
            "entries": [{"type": "file", "path": row["path"], "size_bytes": row["size_bytes"],
                         "sha256": row["sha256"], "mode": 420}
                        for row in final_file_rows],
        }
        manifest_raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        manifest_path.write_bytes(manifest_raw)

        generated_path = self.repo / retention.BUILD1_GENERATED_INPUTS_RELATIVE
        generated_path.parent.mkdir(parents=True, exist_ok=True)
        generated_raw = json.dumps({"build_id": retention.BUILD1_ID,
                                    "baseline_id": retention.BUILD1_BASELINE_ID,
                                    "receipt_kind": "synthetic"}, sort_keys=True).encode()
        generated_path.write_bytes(generated_raw)
        receipt_path = self.repo / retention.BUILD1_PARENT_RECEIPT_RELATIVE
        receipt_path.parent.mkdir(parents=True, exist_ok=True)
        receipt = {
            "baseline_id": retention.BUILD1_BASELINE_ID,
            "build_id": retention.BUILD1_ID,
            "closure_status": "COMPLETE_PASS_WITH_NON_BLOCKING_CAVEATS",
            "build_directory_manifest_ref": {
                "path": retention.BUILD1_DIRECTORY_MANIFEST_RELATIVE.as_posix(),
                "sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "size_bytes": len(manifest_raw),
            },
            "generated_inputs_ref": {
                "path": retention.BUILD1_GENERATED_INPUTS_RELATIVE.as_posix(),
                "sha256": hashlib.sha256(generated_raw).hexdigest(),
                "size_bytes": len(generated_raw),
            },
        }
        receipt_raw = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
        receipt_path.write_bytes(receipt_raw)

        index_path = self.repo / retention.BUILD1_INDEX_RELATIVE
        index = {
            "baseline_id": retention.BUILD1_BASELINE_ID,
            "build_id": retention.BUILD1_ID,
            "classification": "R6_BASELINE_PASS",
            "index_kind": "supplemental-build",
            "source_to_binary_receipt": {
                "closure_status": receipt["closure_status"],
                "path": retention.BUILD1_PARENT_RECEIPT_RELATIVE.as_posix(),
                "sha256": hashlib.sha256(receipt_raw).hexdigest(),
                "size_bytes": len(receipt_raw),
            },
            "artifact_count": len(artifacts),
            "artifacts": artifacts,
        }
        index_raw = (json.dumps(index, sort_keys=True, indent=2) + "\n").encode()
        index_path.write_bytes(index_raw)

        review_path = self.repo / retention.BUILD1_FINAL_REVIEW_RELATIVE
        review_path.parent.mkdir(parents=True, exist_ok=True)
        review_raw = b"Synthetic accepted direct final review.\n"
        review_path.write_bytes(review_raw)
        supplement_path = self.repo / retention.BUILD1_REVIEW_SUPPLEMENT_RELATIVE
        supplement_path.write_text(json.dumps({
            "baseline_id": retention.BUILD1_BASELINE_ID,
            "build_id": retention.BUILD1_ID,
            "parent_result": "PARENT_RECEIPT_CLOSED",
            "existing_indexes_replaced": False,
            "finalized_receipt": {
                "path": retention.BUILD1_PARENT_RECEIPT_RELATIVE.as_posix(),
                "sha256": hashlib.sha256(receipt_raw).hexdigest(),
                "size_bytes": len(receipt_raw),
            },
            "prior_build_1_index": {
                "path": retention.BUILD1_INDEX_RELATIVE.as_posix(),
                "sha256": hashlib.sha256(index_raw).hexdigest(),
                "size_bytes": len(index_raw),
            },
            "supplemental_review": {
                "path": retention.BUILD1_FINAL_REVIEW_RELATIVE.as_posix(),
                "sha256": hashlib.sha256(review_raw).hexdigest(),
                "size_bytes": len(review_raw),
            },
        }, sort_keys=True, indent=2) + "\n", encoding="utf-8")

        rejected_r5_relative = relative_roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID]
        rejected_r5_rows = root_rows[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID]
        rejected_r5_files = [row for row in rejected_r5_rows if row["kind"] == "file"]
        critical_relative = retention.REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE
        critical_row = next(row for row in rejected_r5_files if row["path"] == critical_relative)
        critical_hash = critical_row["sha256"]
        test_summary_path = Path("r6/experiments/R6-A-ACCOUNTING-SNAPSHOT-1/provenance/R6-A-TEST-SUMMARY.json")
        execution_review_path = Path(
            "r6/experiments/R6-A-ACCOUNTING-SNAPSHOT-1/provenance/reviews/R6-A-EXECUTION-REVIEW.md")
        test_summary_raw = (json.dumps({
            "source_revision": "R5",
            "source_tree_sha256": "488e953058160c2cc0796fc29d17086f4e0043c87eb6593ceabda93553241673",
            "test_binaries": {"windows_x86_64_pe": {
                "path": f"{rejected_r5_relative}/{critical_relative}", "sha256": critical_hash}},
        }, sort_keys=True, indent=2) + "\n").encode()
        execution_review_raw = (
            f"{rejected_r5_relative}/{critical_relative}\n{critical_hash}\n").encode()
        r8_index_path = retention.REJECTED_R5_BUILDROOT_R8_PATH
        r8_artifacts = [
            {"path": test_summary_path.as_posix(),
             "sha256": hashlib.sha256(test_summary_raw).hexdigest(),
             "size_bytes": len(test_summary_raw)},
            {"path": execution_review_path.as_posix(),
             "sha256": hashlib.sha256(execution_review_raw).hexdigest(),
             "size_bytes": len(execution_review_raw)},
        ]
        r8_index_raw = (json.dumps({
            "schema_version": 1,
            "revision": retention.REJECTED_R5_BUILDROOT_R8_REVISION,
            "derivative_id": retention.REJECTED_R5_BUILDROOT_R8_DERIVATIVE_ID,
            "artifacts": r8_artifacts,
        }, sort_keys=True, indent=2) + "\n").encode()

        required_references = []
        for pinned in retention.REJECTED_R5_BUILDROOT_REQUIRED_AUTHORITY_REFERENCES:
            relative = Path(pinned["path"])
            if relative.as_posix() == r8_index_path.as_posix():
                raw = r8_index_raw
            elif relative == test_summary_path:
                raw = test_summary_raw
            elif relative == execution_review_path:
                raw = execution_review_raw
            elif relative.name == "R6-A-FINAL-GPT6-ACCEPTANCE-REVIEW.md":
                raw = b"FAIL \xe2\x80\x94 R6-A NOT ACCEPTED\n"
            else:
                raw = f"synthetic authority evidence: {relative.as_posix()}\n".encode()
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
            required_references.append({
                "path": relative.as_posix(),
                "sha256": hashlib.sha256(raw).hexdigest(),
                "size_bytes": len(raw),
                "role": pinned["role"],
            })
        reference_by_path = {row["path"]: row for row in required_references}
        r8_claims = [
            {key: claim[key] for key in ("path", "sha256", "size_bytes")}
            for claim in r8_artifacts
        ]
        r8_identity = {
            "path": r8_index_path.as_posix(),
            "sha256": reference_by_path[r8_index_path.as_posix()]["sha256"],
            "revision": retention.REJECTED_R5_BUILDROOT_R8_REVISION,
            "derivative_id": retention.REJECTED_R5_BUILDROOT_R8_DERIVATIVE_ID,
            "indexed_historical_claims": r8_claims,
        }
        inventory_sha256 = hashlib.sha256(retention._canonical_json(rejected_r5_rows)).hexdigest()
        tree_content_sha256 = retention._protected_evidence_tree_content_sha256(rejected_r5_rows)
        rejected_r5_authority = {
            "schema_version": 1,
            "evidence_id": retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID,
            "disposition": "HISTORICAL_REJECTED_EXECUTION_EVIDENCE",
            "R6_A_ACCEPTANCE": False,
            "execution_authorization": "NONE",
            "active_BuildLease": False,
            "R5_repair_admission": "NOT_CREATED",
            "exact_normalized_root_path": str(roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID].absolute()),
            "accounting": {
                "policy": "FULLY_CHARGED",
                "storage_credit": False,
                "reservation_credit": False,
                "free_space_exclusion": False,
                "project_scope_exclusion": False,
                "BuildLease_exemption": False,
            },
            "identity": {
                "regular_file_count": len(rejected_r5_files),
                "directory_count": sum(row["kind"] == "directory" for row in rejected_r5_rows),
                "logical_bytes": sum(row["size_bytes"] for row in rejected_r5_files),
                "symlink_count": 0,
                "multiply_linked_regular_file_count": 0,
                "inventory_sha256": inventory_sha256,
                "tree_content_sha256": tree_content_sha256,
                "critical_executable": {
                    "relative_path": critical_relative,
                    "sha256": critical_hash,
                    "size_bytes": critical_row["size_bytes"],
                },
            },
            "allocation_observation": {
                "allocated_regular_file_bytes": 128_933_888,
                "method": "sum regular-file st_blocks times 512",
                "identity_binding": False,
            },
            "r8_evidence_index": r8_identity,
            "authority_references": required_references,
            "complete_inventory": rejected_r5_rows,
        }
        authority_path = self.repo / retention.REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE
        authority_path.parent.mkdir(parents=True, exist_ok=True)
        authority_raw = (json.dumps(rejected_r5_authority, sort_keys=True, indent=2) + "\n").encode()
        authority_path.write_bytes(authority_raw)

        specs = []
        for identity, relative in relative_roots.items():
            rows = root_rows[identity]
            files = [row for row in rows if row["kind"] == "file"]
            spec_fields: dict[str, object] = {}
            if identity == retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID:
                spec_fields = {
                    "tree_content_sha256": tree_content_sha256,
                    "critical_file_relative_path": critical_relative,
                    "critical_file_sha256": critical_hash,
                    "authority_relative_path": retention.REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE.as_posix(),
                    "authority_sha256": hashlib.sha256(authority_raw).hexdigest(),
                }
            specs.append(retention.ProtectedEvidenceRoot(
                evidence_id=identity,
                relative_path=relative,
                inventory_sha256=hashlib.sha256(retention._canonical_json(rows)).hexdigest(),
                expected_file_count=len(files),
                expected_logical_bytes=sum(row["size_bytes"] for row in files),
                **spec_fields,
            ))
        patcher = mock.patch.multiple(
            retention,
            PROTECTED_EVIDENCE_ROOTS=tuple(specs),
            REJECTED_R5_BUILDROOT_AUTHORITY_SHA256=hashlib.sha256(authority_raw).hexdigest(),
            REJECTED_R5_BUILDROOT_TREE_CONTENT_SHA256=tree_content_sha256,
            REJECTED_R5_BUILDROOT_CRITICAL_FILE_SHA256=critical_hash,
            REJECTED_R5_BUILDROOT_REQUIRED_AUTHORITY_REFERENCES=tuple(required_references),
            REJECTED_R5_BUILDROOT_R8_CLAIMS=tuple(r8_claims),
            BUILD1_PARENT_RECEIPT_SHA256=hashlib.sha256(receipt_raw).hexdigest(),
            BUILD1_FINAL_REVIEW_SHA256=hashlib.sha256(review_raw).hexdigest(),
            BUILD1_REVIEW_SUPPLEMENT_SHA256=hashlib.sha256(supplement_path.read_bytes()).hexdigest(),
            BUILD1_INDEX_SHA256=hashlib.sha256(index_raw).hexdigest(),
            BUILD1_DIRECTORY_MANIFEST_SHA256=hashlib.sha256(manifest_raw).hexdigest(),
            BUILD1_GENERATED_INPUTS_SHA256=hashlib.sha256(generated_raw).hexdigest(),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return roots

    def _manifest_estimates(self, manifest: dict) -> dict[str, int]:
        declared_peak = 64 * 1024 ** 2
        margin = storage_policy.admission_uncertainty_margin_bytes(declared_peak)
        return {"expected_build_bytes": 64 * 1024 ** 2,
                "estimated_max_new_disk_bytes": declared_peak,
                "estimated_uncertainty_margin_bytes": margin,
                "estimated_projected_new_disk_bytes": declared_peak + margin}

    def _write_owner_manifest(self, source_hash: str | None = None, config_hash: str | None = None,
                              build_kind: str = "dxvk", build_type: str = "release",
                              preserve_debug_reason: str | None = None,
                              estimated_debug_bytes: int | None = None,
                              failure_capture_paths: list[str] | None = None,
                              dependency_state: dict | None = None,
                              session_plan_sha256: str | None = None,
                              admission_identity: dict | None = None,
                              experiment_id: str = "synthetic-test") -> None:
        state = dependency_state if dependency_state is not None else {"status": "captured", "lock": "synthetic dependency state"}
        source_hash = source_hash or hashlib.sha256(b"completed source tree").hexdigest()
        config_hash = config_hash or retention.configuration_key_from_identity(
            self._synthetic_identity(source_hash, state), dependency_state=state)
        manifest = {"experiment_id": experiment_id, "decision": "ALLOW",
                    "storage_policy_smoke": False, "expected_build_bytes": 64 * 1024 ** 2,
                    "build_source_tree_hash": source_hash,
                    "build_configuration_key_sha256": config_hash,
                    "build_kind": build_kind,
                    "build_type": build_type,
                    "dependency_state": state,
                    "preserve_debug_reason": preserve_debug_reason,
                    "estimated_debug_bytes": estimated_debug_bytes,
                    "failure_capture_paths": failure_capture_paths or [],
                    "authorized_runner_paths": [str(self.runner_file.resolve())],
                    "authorized_runner_sha256": {
                        str(self.runner_file.resolve()): hashlib.sha256(self.runner_file.read_bytes()).hexdigest()},
                    "gate0_attestation_sha256": hashlib.sha256(self.gate_attestation.read_bytes()).hexdigest()}
        if session_plan_sha256 is not None:
            manifest["build_session_plan_sha256"] = session_plan_sha256
            manifest["execution_admission_identity"] = admission_identity
        (self.manifests / f"{experiment_id}.json").write_text(
            __import__("json").dumps(manifest), encoding="utf-8")

    @staticmethod
    def _synthetic_identity(source_hash: str, dependency_state: dict, build_type: str = "release") -> dict:
        return {"build_system": "meson", "source_identity": {"content_tree_sha256": source_hash},
                "patch_set": {"patch_set_sha256": hashlib.sha256(b"no patches").hexdigest()},
                "cross_file": {"status": "absent"},
                "meson_options": [{"name": "buildtype", "value": "release"}],
                "compiler_identity": {"c": {"version": "synthetic cc 1"}},
                "compiler_identity_sha256": hashlib.sha256(b"synthetic cc identity").hexdigest(),
                "linker_identity": [{"command": "ld -v", "output": "synthetic ld"}],
                "generated_shader_identity": [], "build_type": build_type,
                "dependency_state": dependency_state}

    def _capture_identity(self, lease: retention.BuildLease, evidence: Path) -> dict:
        identity = self._synthetic_identity(lease.source_tree_hash, lease.metadata["dependency_state"],
                                            lease.metadata["build_type"])
        rows = {
            "meson_log_evidence": ("logs/meson-log.txt", b"synthetic meson configure log"),
            "meson_command_line_evidence": ("configuration/meson-cmd-line.txt", b"meson setup synthetic"),
            "meson_options_evidence": ("configuration/meson-build-options.json", b"synthetic options"),
            "compiler_evidence": ("configuration/meson-compilers.json", b"synthetic compiler"),
            "build_ninja_evidence": ("configuration/build.ninja", b"synthetic graph"),
            "ninja_log_evidence": ("logs/ninja-log.txt", b"synthetic ninja execution"),
            "intro-dependencies_json_evidence": ("configuration/meson-intro-dependencies.json", b"[]"),
        }
        for key, (relative, contents) in rows.items():
            record = retention._write_evidence_bytes(evidence / relative, contents)
            identity[key] = record
        identity.update({
            "meson_log_sha256": identity["meson_log_evidence"]["sha256"],
            "meson_options_sha256": hashlib.sha256(b"synthetic options").hexdigest(),
            "meson_command_line_sha256": hashlib.sha256(b"meson setup synthetic").hexdigest(),
            "ninja_log_sha256": identity["ninja_log_evidence"]["sha256"],
            "ninja_execution_log_status": "captured",
            "build_log_sha256": None,
            "build_log_status": "no stdout build transcript present; configure log and Ninja execution log retained separately",
        })
        identity["config_key_sha256"] = retention.configuration_key_from_identity(
            identity, dependency_state=identity["dependency_state"])
        return identity

    def _capture_identity_for_build(self, build: Path, evidence: Path) -> dict:
        build_id = (build.parent.name if build.name == retention.BUILD_SESSION_WORKSPACE_NAME
                    else build.name)
        lease = retention._load_lease(build_id)
        lease_view = type("LeaseView", (), {
            "source_tree_hash": lease["source_tree_hash"], "metadata": lease.get("metadata", {})})()
        identity = self._capture_identity(lease_view, evidence)
        session = lease.get("session_v3")
        if isinstance(session, dict):
            source = session.get("source_identity")
            if (not isinstance(source, dict) or not isinstance(source.get("root"), str)
                    or not isinstance(source.get("revision"), str)
                    or not isinstance(source.get("sha256"), str)):
                raise AssertionError("synthetic v3 identity lacks the admitted source binding")
            if source["sha256"] != lease.get("source_tree_hash"):
                raise AssertionError("synthetic v3 source identity differs from the lease tree hash")
            identity["source_identity"] = {
                "path": source["root"],
                "content_tree_sha256": source["sha256"],
                "git_commit": source["revision"],
                "capture_scope": "synthetic v3 accepted-plan binding",
            }
        return identity

    def _lease(self, build_id: str, *, kind: str = "dxvk", preserve: bool = False,
                source_tree_hash: str | None = None, build_type: str = "release",
                failure_capture_paths: list[str] | None = None,
                debug_reason: str | None = None, estimated_debug_bytes: int | None = None) -> retention.BuildLease:
        metadata = {"preserve_debug_expiry_unix": time.time() + 3600} if preserve else None
        dependency_state = {"status": "captured", "lock": "synthetic dependency state"}
        preserve_reason = debug_reason or ("needed to test debug retention" if preserve else None)
        metadata = {**(metadata or {}), "dependency_state": dependency_state, "build_type": build_type,
                    "failure_capture_paths": failure_capture_paths or [],
                    "estimated_debug_bytes": estimated_debug_bytes}
        source_hash = source_tree_hash or hashlib.sha256(b"completed source tree").hexdigest()
        config_hash = retention.configuration_key_from_identity(
            self._synthetic_identity(source_hash, dependency_state, build_type), dependency_state=dependency_state)
        self._write_owner_manifest(source_hash, config_hash, build_kind=kind, build_type=build_type,
                                   preserve_debug_reason=preserve_reason,
                                   estimated_debug_bytes=estimated_debug_bytes,
                                   failure_capture_paths=failure_capture_paths)
        return retention.BuildLease(
            build_id, kind=kind, owner_experiment="synthetic-test", purpose="retention test",
            source_tree_hash=source_hash,
            expected_bytes=64 * 1024 * 1024,
            metadata=metadata, preserve_debug_reason=preserve_reason,
            runner_path=self.runner_file,
            configuration_key_sha256=config_hash,
        )

    def _synthetic_dual_host_dependencies(self, build_id: str) -> tuple[list[dict[str, str]], dict[str, str]]:
        """Build tiny synthetic closed host-input receipts for dual-graph tests."""
        root = self.repo / f"host-inputs-{build_id}"
        root.mkdir(mode=0o700, exist_ok=True)

        def tree(input_id: str, relative: str) -> dict[str, str]:
            path = root / relative
            path.mkdir(parents=True, mode=0o700)
            (path / "synthetic-input.txt").write_text(input_id + "\n", encoding="utf-8")
            digest, _ = retention._tree_hash(path, reject_symlinks=True,
                                             reject_external_hardlinks=True)
            return {"kind": "tree", "path": str(path.resolve()), "sha256": digest}

        def file(input_id: str, relative: str, data: bytes) -> dict[str, str]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(data)
            return {"kind": "file", "path": str(path.resolve()),
                    "sha256": hashlib.sha256(data).hexdigest()}

        def symlink(input_id: str, relative: str, target_path: Path) -> dict[str, str]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = os.path.relpath(target_path, path.parent)
            path.unlink(missing_ok=True)
            path.symlink_to(target)
            return {"kind": "symlink", "path": str(path), "target": target,
                    "resolved_path": str(target_path.resolve()),
                    "sha256": retention._hash_path(target_path.resolve())}

        xcode_contents = root / "Xcode/Contents"
        developer = xcode_contents / "Developer"
        sdk_parent = developer / "Platforms/MacOSX.platform/Developer/SDKs"
        sdk_real = sdk_parent / "MacOSX.sdk"
        sdk_real.mkdir(parents=True, mode=0o700)
        sdk_alias = sdk_parent / "MacOSX27.0.sdk"
        sdk_alias.symlink_to("MacOSX.sdk")
        xcode_info_path = xcode_contents / "Info.plist"
        system_version_path = root / "System/Library/CoreServices/SystemVersion.plist"
        sdk_settings_path = sdk_real / "SDKSettings.plist"
        sdk_settings_raw = plistlib.dumps({"Version": "27.0"})
        xcode_info_raw = plistlib.dumps({"CFBundleShortVersionString": "27.1",
                                         "DTXcodeBuild": "SYNTHETIC-INFO-BUILD"})
        system_version_raw = plistlib.dumps({"ProductVersion": "27.0.1",
                                             "ProductBuildVersion": "SYNTHETIC-OS-BUILD"})
        sdk_settings_path.write_bytes(sdk_settings_raw)
        xcode_info_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        xcode_info_path.write_bytes(xcode_info_raw)
        system_version_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        system_version_path.write_bytes(system_version_raw)

        toolchain_bin = developer / "Toolchains/XcodeDefault.xctoolchain/usr/bin"
        toolchain_bin.mkdir(parents=True, mode=0o700)
        selected_tool_files = {
            "XCODEBUILD": file("XCODEBUILD", "Xcode/Contents/Developer/usr/bin/xcodebuild",
                                b"synthetic xcodebuild\n"),
            "XCODE_CLANG": file("XCODE_CLANG",
                                "Xcode/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/clang",
                                b"synthetic selected clang\n"),
            "XCODE_LD": file("XCODE_LD",
                             "Xcode/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ld",
                             b"synthetic selected linker\n"),
            "XCODE_AR": file("XCODE_AR",
                             "Xcode/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/ar",
                             b"synthetic selected archiver\n"),
            "XCODE_RANLIB": file("XCODE_RANLIB",
                                 "Xcode/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/libtool",
                                 b"synthetic selected libtool\n"),
            "XCODE_AS": file("XCODE_AS",
                             "Xcode/Contents/Developer/Toolchains/XcodeDefault.xctoolchain/usr/bin/as",
                             b"synthetic selected assembler\n"),
            "XCODE_SELECT": file("XCODE_SELECT", "system/bin/xcode-select",
                                 b"synthetic xcode-select tool\n"),
            "XCRUN": file("XCRUN", "system/bin/xcrun", b"synthetic xcrun tool\n"),
        }
        (toolchain_bin / "clang++").symlink_to("clang")
        (toolchain_bin / "ranlib").symlink_to("libtool")

        homebrew = root / "homebrew"
        sdl2_include_real = homebrew / "Cellar/sdl2-compat/2.32.74/include/SDL2"
        sdl2_include_real.mkdir(parents=True, mode=0o700)
        (sdl2_include_real / "SDL_vulkan.h").write_text("/* synthetic SDL2 header */\n", encoding="utf-8")
        sdl2_include_alias = homebrew / "include/SDL2"
        sdl2_include_alias.parent.mkdir(parents=True, mode=0o700)
        sdl2_include_alias.symlink_to("../Cellar/sdl2-compat/2.32.74/include/SDL2")

        sdl2_cellar_lib = homebrew / "Cellar/sdl2-compat/2.32.74/lib"
        sdl2_runtime_path = sdl2_cellar_lib / "libSDL2-2.0.0.dylib"
        sdl2_runtime_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        sdl2_runtime_path.write_bytes(self._tiny_macho(6))
        sdl2_runtime_path.chmod(0o500)
        sdl2_test_archive_path = sdl2_cellar_lib / "libSDL2_test.a"
        sdl2_test_archive_path.write_bytes(b"synthetic SDL2 test archive\n")
        sdl2_main_archive_path = sdl2_cellar_lib / "libSDL2main.a"
        sdl2_main_archive_path.write_bytes(b"synthetic SDL2 main archive\n")
        sdl2_pkgconfig_path = sdl2_cellar_lib / "pkgconfig/sdl2-compat.pc"
        sdl2_pkgconfig_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        sdl2_pkgconfig_path.write_bytes(b"prefix=/synthetic\nName: sdl2\nVersion: 2.32.74\n")
        sdl2_lib_alias_2_0 = sdl2_cellar_lib / "libSDL2-2.0.dylib"
        sdl2_lib_alias_2_0.symlink_to(sdl2_runtime_path.name)
        sdl2_lib_alias = sdl2_cellar_lib / "libSDL2.dylib"
        sdl2_lib_alias.symlink_to(sdl2_lib_alias_2_0.name)
        sdl2_pkgconfig_alias_cellar = sdl2_pkgconfig_path.parent / "sdl2.pc"
        sdl2_pkgconfig_alias_cellar.symlink_to(sdl2_pkgconfig_path.name)
        sdl2_global_lib = homebrew / "lib"
        sdl2_global_lib.mkdir(parents=True, mode=0o700)
        sdl2_lib_alias_paths = {
            "SDL2_LIB_ALIAS_2_0_0": sdl2_global_lib / "libSDL2-2.0.0.dylib",
            "SDL2_LIB_ALIAS_2_0": sdl2_global_lib / "libSDL2-2.0.dylib",
            "SDL2_LIB_ALIAS": sdl2_global_lib / "libSDL2.dylib",
            "SDL2_TEST_LIB_ALIAS": sdl2_global_lib / "libSDL2_test.a",
            "SDL2_MAIN_LIB_ALIAS": sdl2_global_lib / "libSDL2main.a",
        }
        sdl2_targets = {
            "SDL2_LIB_ALIAS_2_0_0": sdl2_runtime_path,
            "SDL2_LIB_ALIAS_2_0": sdl2_lib_alias_2_0,
            "SDL2_LIB_ALIAS": sdl2_lib_alias,
            "SDL2_TEST_LIB_ALIAS": sdl2_test_archive_path,
            "SDL2_MAIN_LIB_ALIAS": sdl2_main_archive_path,
        }
        sdl2_global_aliases = {
            key: symlink(key, f"homebrew/lib/{path.name}", target)
            for key, path, target in ((key, path, sdl2_targets[key])
                                      for key, path in sdl2_lib_alias_paths.items())
        }
        sdl2_pkgconfig_global = homebrew / "lib/pkgconfig"
        sdl2_pkgconfig_global.mkdir(parents=True, mode=0o700)
        sdl2_pkgconfig_global_aliases = {
            "SDL2_PKGCONFIG_GLOBAL_ALIAS": symlink(
                "SDL2_PKGCONFIG_GLOBAL_ALIAS", "homebrew/lib/pkgconfig/sdl2.pc",
                sdl2_pkgconfig_alias_cellar),
            "SDL2_COMPAT_PKGCONFIG_GLOBAL_ALIAS": symlink(
                "SDL2_COMPAT_PKGCONFIG_GLOBAL_ALIAS", "homebrew/lib/pkgconfig/sdl2-compat.pc",
                sdl2_pkgconfig_path),
        }

        mingw_root = self.synthetic_mingw_root
        mingw_bin = mingw_root / "bin"
        mingw_bin.mkdir(parents=True, exist_ok=True, mode=0o700)
        for relative, data in (
                ("libexec/gcc/x86_64-w64-mingw32/16.2.0/cc1", b"synthetic cc1\n"),
                ("libexec/gcc/x86_64-w64-mingw32/16.2.0/cc1plus", b"synthetic cc1plus\n"),
                ("libexec/gcc/x86_64-w64-mingw32/16.2.0/collect2", b"synthetic collect2\n"),
                ("libexec/gcc/x86_64-w64-mingw32/16.2.0/lto-wrapper", b"synthetic lto wrapper\n"),
                ("x86_64-w64-mingw32/lib/libmingwex.a", b"synthetic MinGW runtime archive\n")):
            path = mingw_root / relative
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.write_bytes(data)
        mingw_alias = mingw_root / "mingw"
        if not mingw_alias.is_symlink() or os.readlink(mingw_alias) != "x86_64-w64-mingw32":
            mingw_alias.unlink(missing_ok=True)
            mingw_alias.symlink_to("x86_64-w64-mingw32")
        mingw_tree_sha256, mingw_file_count = retention._tree_hash(
            mingw_root, reject_external_hardlinks=True, require_internal_symlinks=True,
            include_root_git=True)
        mingw_tree_pin = {"kind": "tree_internal_symlinks", "path": str(mingw_root.resolve()),
                          "sha256": mingw_tree_sha256, "file_count": mingw_file_count}

        pins = {
            "MINGW_TOOLCHAIN_TREE": mingw_tree_pin,
            "MESON_PYTHON_RUNTIME": file("MESON_PYTHON_RUNTIME", "python/Python", b"synthetic Python framework\n"),
            "MESON_PACKAGE_TREE": tree("MESON_PACKAGE_TREE", "meson/site-packages/mesonbuild"),
            "MESON_DISTRIBUTION_METADATA": tree("MESON_DISTRIBUTION_METADATA",
                                                  "meson/site-packages/meson-1.12.1.dist-info"),
            "SDL2_PKGCONFIG": {"kind": "file", "path": str(sdl2_pkgconfig_path.resolve()),
                                "sha256": retention._hash_path(sdl2_pkgconfig_path)},
            "SDL2_PKGCONFIG_CELLAR_ALIAS": symlink(
                "SDL2_PKGCONFIG_CELLAR_ALIAS", "homebrew/Cellar/sdl2-compat/2.32.74/lib/pkgconfig/sdl2.pc",
                sdl2_pkgconfig_path),
            **sdl2_pkgconfig_global_aliases,
            "SDL2_COMPAT_LIBRARY": {"kind": "file", "path": str(sdl2_runtime_path.resolve()),
                                    "sha256": retention._hash_path(sdl2_runtime_path)},
            "SDL2_TEST_ARCHIVE": file("SDL2_TEST_ARCHIVE",
                                       "homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2_test.a",
                                       b"synthetic SDL2 test archive\n"),
            **sdl2_global_aliases,
            "SDL2_CELLAR_ALIAS_2_0": symlink(
                "SDL2_CELLAR_ALIAS_2_0", "homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2-2.0.dylib",
                sdl2_runtime_path),
            "SDL2_CELLAR_ALIAS": symlink(
                "SDL2_CELLAR_ALIAS", "homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2.dylib",
                sdl2_lib_alias_2_0),
            "SDL2MAIN_ARCHIVE": file("SDL2MAIN_ARCHIVE",
                                      "homebrew/Cellar/sdl2-compat/2.32.74/lib/libSDL2main.a",
                                      b"synthetic SDL2 main archive\n"),
            "SDL2_HEADERS": {"kind": "tree", "path": str(sdl2_include_real.resolve()),
                             "sha256": retention._tree_hash(sdl2_include_real, reject_symlinks=True,
                                                            reject_external_hardlinks=True)[0]},
            "GLSLANG_RUNTIME": file("GLSLANG_RUNTIME", "glslang/lib/libglslang.dylib", b"synthetic glslang\n"),
            "GLSLANG_RESOURCE_LIMITS_RUNTIME": file("GLSLANG_RESOURCE_LIMITS_RUNTIME",
                                                     "glslang/lib/libglslang-default-resource-limits.dylib",
                                                     b"synthetic glslang limits\n"),
            "GLSLANG_SPIRV_RUNTIME": file("GLSLANG_SPIRV_RUNTIME", "glslang/lib/libSPIRV.dylib",
                                           b"synthetic glslang SPIRV\n"),
            "SPIRV_TOOLS_RUNTIME": file("SPIRV_TOOLS_RUNTIME", "spirv-tools/lib/libSPIRV-Tools.dylib",
                                        b"synthetic SPIRV tools\n"),
            "SPIRV_TOOLS_OPT_RUNTIME": file("SPIRV_TOOLS_OPT_RUNTIME",
                                            "spirv-tools/lib/libSPIRV-Tools-opt.dylib",
                                            b"synthetic SPIRV tools opt\n"),
            "SDK_SETTINGS": {"kind": "file", "path": str(sdk_settings_path.resolve()),
                             "sha256": hashlib.sha256(sdk_settings_raw).hexdigest()},
            "XCODE_INFO": {"kind": "file", "path": str(xcode_info_path.resolve()),
                           "sha256": hashlib.sha256(xcode_info_raw).hexdigest()},
            "SYSTEM_VERSION": {"kind": "file", "path": str(system_version_path.resolve()),
                               "sha256": hashlib.sha256(system_version_raw).hexdigest()},
            **selected_tool_files,
            "XCODE_CLANGXX": dict(selected_tool_files["XCODE_CLANG"]),
        }
        profile = {
            "host_architecture": retention._session_native_architecture(),
            "os_product_version": "27.0.1", "os_build": "SYNTHETIC-OS-BUILD",
            "xcode_version": "27.1", "xcode_build": "SYNTHETIC-XCODE-CLI",
            "xcode_info_build": "SYNTHETIC-INFO-BUILD",
            "developer_directory": str(developer.resolve()),
            "xcrun_selected_tool_paths": {
                "clang": str((toolchain_bin / "clang").resolve()),
                "clang++": str(toolchain_bin / "clang++"),
                "ld": str((toolchain_bin / "ld").resolve()),
                "ar": str((toolchain_bin / "ar").resolve()),
                "ranlib": str(toolchain_bin / "ranlib"),
                "as": str((toolchain_bin / "as").resolve()),
            },
            "sdk_path": str(sdk_alias), "sdk_symlink_target": "MacOSX.sdk",
            "sdk_resolved_path": str(sdk_real.resolve()), "sdk_version": "27.0",
            "sdk_settings_sha256": pins["SDK_SETTINGS"]["sha256"],
            "xcode_info_path": str(xcode_info_path.resolve()),
            "system_version_path": str(system_version_path.resolve()),
            "sdl2_header_search_path": str(sdl2_include_alias),
            "sdl2_header_symlink_target": "../Cellar/sdl2-compat/2.32.74/include/SDL2",
            "sdl2_header_real_path": str(sdl2_include_real.resolve()),
        }
        self.synthetic_dual_host_dependency_pins.clear()
        self.synthetic_dual_host_dependency_pins.update(pins)
        self.synthetic_dual_host_profile.clear()
        self.synthetic_dual_host_profile.update(profile)
        return [{"input_id": input_id, **pin} for input_id, pin in pins.items()], profile

    @staticmethod
    def _synthetic_native_toolchain_selection(plan: dict, dependencies: dict) -> dict:
        profile = plan["native_host_profile"]
        selected_tools = {}
        for tool_name, input_id in retention.BUILD_SESSION_DUAL_GRAPH_NATIVE_SELECTED_TOOL_RECEIPTS.items():
            identity = dependencies[input_id]
            selected_tools[tool_name] = {
                "reported_path": profile["xcrun_selected_tool_paths"][tool_name],
                "resolved_path": identity["path"],
                "sha256": identity["sha256"],
            }
        return {
            "active_developer_directory": profile["developer_directory"],
            "developer_dir_environment": profile["developer_directory"],
            "sdkroot_environment": profile["sdk_resolved_path"],
            "sdk_path": profile["sdk_path"],
            "sdk_resolved_path": profile["sdk_resolved_path"],
            "sdk_version": profile["sdk_version"],
            "selected_tools": selected_tools,
        }

    def _session_lease(self, build_id: str = "session-test", *,
                       configure_argv: list[str] | None = None,
                       install_argv: list[str] | None = None,
                       test_executable: str = "tests/present-accounting.exe",
                       stage_root: str = "stage",
                       failure_capture_paths: list[str] | None = None,
                       source_identity_override: dict[str, str] | None = None,
                       environment_overrides: dict[str, str] | None = None,
                       dual_graph: bool = False,
                       owner_experiment: str = "synthetic-test") -> retention.BuildLease:
        source = self.repo / f"source-{build_id}"
        source.mkdir(mode=0o700)
        (source / "meson.build").write_text("project('synthetic', 'c', 'cpp')\n", encoding="utf-8")
        if dual_graph:
            (source / "build-win64.txt").write_text(
                "[binaries]\n"
                "c = 'x86_64-w64-mingw32-gcc'\n"
                "cpp = 'x86_64-w64-mingw32-g++'\n"
                "ar = 'x86_64-w64-mingw32-ar'\n"
                "strip = 'x86_64-w64-mingw32-strip'\n"
                "windres = 'x86_64-w64-mingw32-windres'\n\n"
                "[properties]\nneeds_exe_wrapper = true\n\n"
                "[host_machine]\nsystem = 'windows'\ncpu_family = 'x86_64'\n"
                "cpu = 'x86_64'\nendian = 'little'\n", encoding="utf-8")
        git = Path("/usr/bin/git")
        subprocess.run([str(git), "init", "--quiet", str(source)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        source_entries = ["meson.build", *( ["build-win64.txt"] if dual_graph else [])]
        subprocess.run([str(git), "-C", str(source), "add", *source_entries], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        git_env = {**os.environ, "GIT_AUTHOR_NAME": "Synthetic", "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
                   "GIT_COMMITTER_NAME": "Synthetic", "GIT_COMMITTER_EMAIL": "synthetic@example.invalid"}
        subprocess.run([str(git), "-C", str(source), "commit", "--quiet", "-m", "synthetic"],
                       check=True, env=git_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        source_hash, _ = retention._tree_hash(source)
        revision = subprocess.check_output([str(git), "-C", str(source), "rev-parse", "--verify", "HEAD"], text=True).strip()

        tool_bin = self.dual_package_bin if dual_graph else self.repo / f"tool-bin-{build_id}"
        tool_bin.mkdir(mode=0o700, exist_ok=True)
        tool_paths: dict[str, Path] = {}
        for name in (("meson", "ninja") if dual_graph else ("meson", "ninja", "cc", "cxx")):
            path = tool_bin / name
            if dual_graph and name == "meson":
                path.write_text(f"#!{retention.BUILD_SESSION_DUAL_GRAPH_MESON_PYTHON_SHEBANG}\n"
                                "# synthetic Meson launcher\n", encoding="utf-8")
            else:
                path.write_bytes(f"synthetic direct tool: {name}\n".encode())
            path.chmod(0o700)
            tool_paths[name] = path
        dual_tool_ids = {
            "mingw_cc": "x86_64-w64-mingw32-gcc",
            "mingw_cxx": "x86_64-w64-mingw32-g++",
            "mingw_ar": "x86_64-w64-mingw32-ar",
            "mingw_windres": "x86_64-w64-mingw32-windres",
            "mingw_strip": "x86_64-w64-mingw32-strip",
            "mingw_linker": "x86_64-w64-mingw32-ld",
            "mingw_assembler": "x86_64-w64-mingw32-as",
            "meson_python": "python3.14",
            "native_glslang": "glslang",
            "native_pkg_config": "pkgconf",
        }
        if dual_graph:
            for receipt_id, name in dual_tool_ids.items():
                if receipt_id.startswith("mingw_"):
                    path = self.synthetic_mingw_root / "bin" / name
                    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                else:
                    path = tool_bin / name
                path.write_bytes(f"synthetic direct tool: {receipt_id}\n".encode())
                path.chmod(0o700)
                tool_paths[receipt_id] = path
            python_alias = Path(retention.BUILD_SESSION_DUAL_GRAPH_MESON_PYTHON_SHEBANG)
            if python_alias.exists() or python_alias.is_symlink():
                python_alias.unlink()
            python_alias.symlink_to(tool_paths["meson_python"])
            for alias, target_id in (("pkg-config", "native_pkg_config"),
                                     ("glslangValidator", "native_glslang")):
                alias_path = tool_bin / alias
                alias_path.unlink(missing_ok=True)
                alias_path.symlink_to(tool_paths[target_id].name)
            for receipt_id, name in {
                    "native_cc": "clang", "native_cxx": "clang++",
                    "native_linker": "ld", "native_ar": "ar", "native_ranlib": "ranlib",
                    "native_python3": "python3", "native_touch": "touch", "native_false": "false",
            }.items():
                tool_paths[receipt_id] = Path("/usr/bin") / name
        sandbox = Path("/usr/bin/sandbox-exec")
        tool_receipts = {
            name: {"path": str(path.resolve()), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for name, path in tool_paths.items()
        }
        tool_receipts.update({
            "git": {"path": str(git.resolve()), "sha256": retention._hash_path(git)},
            "sandbox": {"path": str(sandbox), "sha256": retention._hash_path(sandbox)},
        })
        if dual_graph:
            self.synthetic_dual_tool_pins.clear()
            self.synthetic_dual_tool_pins.update({
                name: {"path": receipt["path"], "sha256": receipt["sha256"]}
                for name, receipt in tool_receipts.items()})
        path_value = retention.BUILD_SESSION_DUAL_GRAPH_PATH if dual_graph else (
            str(tool_bin.resolve()) + os.pathsep + "/usr/bin")
        admission_identity = {
            "admission_id": f"synthetic-admission-{build_id}",
            "sha256": hashlib.sha256(retention._canonical_json({"admission_id": f"synthetic-admission-{build_id}"})).hexdigest(),
        }
        dependency_state = {"status": "captured", "lock": "synthetic dependency state"}
        config_hash = retention.configuration_key_from_identity(
            self._synthetic_identity(source_hash, dependency_state), dependency_state=dependency_state)
        build_path = retention.BUILD_ROOT / build_id
        build_workspace = build_path / retention.BUILD_SESSION_WORKSPACE_NAME
        if configure_argv is None:
            configure_argv = [tool_receipts["meson"]["path"], "setup", str(build_workspace), str(source.resolve()),
                              "--prefix=/usr", "--backend=ninja", "--wrap-mode=nodownload",
                              "-Dbuildtype=release"]
        source_identity = {"root": str(source.resolve()), "revision": revision, "sha256": source_hash}
        if source_identity_override is not None:
            source_identity.update(source_identity_override)
        environment = {
            "HOME": str(build_workspace / ".home"), "PATH": path_value,
            "LANG": "C", "LC_ALL": "C",
        }
        if not dual_graph:
            environment.update({"CC": tool_receipts["cc"]["path"],
                                "CXX": tool_receipts["cxx"]["path"]})
        if environment_overrides is not None:
            environment.update(environment_overrides)
        if install_argv is None:
            install_argv = [tool_receipts["meson"]["path"], "install", "-C", str(build_workspace),
                            "--destdir", str(build_workspace / stage_root), "--no-rebuild"]
        plan = {
            "schema_version": retention.BUILD_SESSION_SCHEMA_VERSION,
            "executor_mode": retention.BUILD_SESSION_EXECUTOR_MODE,
            "admission_identity": admission_identity,
            "source_identity": source_identity,
            "build_id": build_id,
            "build_workspace": str(build_workspace),
            "reservation_bytes": 64 * 1024 * 1024,
            "stage_root": stage_root,
            "output_root": ".",
            "environment": environment,
            "tools": tool_receipts,
            "input_receipts": [],
            "steps": [
                {"kind": "configure", "argv": configure_argv, "cwd": "."},
                {"kind": "build", "argv": [tool_receipts["ninja"]["path"], "-C", str(build_workspace)], "cwd": "."},
                {"kind": "install", "argv": install_argv, "cwd": "."},
                {"kind": "test", "executable": test_executable, "arguments": [], "cwd": "."},
                {"kind": "source_verification"},
            ],
        }
        if dual_graph:
            cross_file = source / "build-win64.txt"
            host_dependency_receipts, host_profile = self._synthetic_dual_host_dependencies(build_id)
            runtime_root = self.repo / f"runtime-inputs-{build_id}"
            runtime_root.mkdir(mode=0o700)
            runtime_receipts = []
            for runtime_id, filename in retention.BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES.items():
                if runtime_id == "SDL2_COMPAT":
                    runtime_path = Path(self.synthetic_dual_host_dependency_pins[
                        "SDL2_COMPAT_LIBRARY"]["path"])
                else:
                    runtime_path = runtime_root / filename
                    runtime_path.write_bytes(self._tiny_macho(6))
                    runtime_path.chmod(0o500)
                runtime_receipts.append({"runtime_id": runtime_id, "path": str(runtime_path.resolve()),
                                        "sha256": retention._hash_path(runtime_path)})
            self.synthetic_dual_runtime_pins.clear()
            self.synthetic_dual_runtime_pins.update({
                row["runtime_id"]: {"path": row["path"], "sha256": row["sha256"]}
                for row in runtime_receipts})
            shared_source = dict(source_identity)
            plan.update({
                "policy_sha256": retention.BUILD_SESSION_HOST_V3_DUAL_GRAPH_POLICY_AMENDMENT_SHA256,
                "dual_build_graphs": retention.BUILD_SESSION_DUAL_GRAPH_CAPABILITY,
                "build_graphs": [
                    {
                        "graph_id": "windows-product-r6a", "graph_role": "WINDOWS_PRODUCT",
                        "build_directory": "build.win64-r6a", "configure_profile": "R6A_WINDOWS_PRODUCT",
                        "cross_file_input": "build-win64.txt",
                        "targets": ["DEFAULT_PRODUCTION_OUTPUTS", "R6A_PRODUCTION_PATH_PE"],
                        "machine_role": "CROSS_WINDOWS_X86_64",
                        "expected_outputs": [{"output_id": "r6a-production-path-pe",
                                              "path": "tests/present-accounting/present-accounting-production-path.exe",
                                              "format": "PE32PLUS_X86_64",
                                              "producer_target": "R6A_PRODUCTION_PATH_PE"}],
                        "stage_role": "WINDOWS_PRODUCT_OUTPUTS", "runtime_stage_role": "NONE",
                        "executable_test_eligibility": False,
                        "tool_receipt_ids": list(retention.BUILD_SESSION_DUAL_GRAPH_TOOL_RECEIPTS[
                            "WINDOWS_PRODUCT"]),
                        "runtime_receipt_ids": [], "source_identity": dict(shared_source),
                    },
                    {
                        "graph_id": "native-verification-r6a", "graph_role": "NATIVE_VERIFICATION",
                        "build_directory": "build.native-r6a", "configure_profile": "R6A_NATIVE_VERIFICATION_SDL2",
                        "cross_file_input": None,
                        "targets": ["R6A_PRODUCTION_PATH_MACHO", "R6A_PRESENT_ACCOUNTING_UNIT",
                                    "R6A_PRESENT_ACCOUNTING_MODEL"],
                        "machine_role": "NATIVE_DARWIN_HOST",
                        "expected_outputs": [
                            {"output_id": "r6a-production-path-macho",
                             "path": "tests/present-accounting/present-accounting-production-path",
                             "format": "MACHO_NATIVE_ARCH",
                             "producer_target": "R6A_PRODUCTION_PATH_MACHO"},
                            {"output_id": "r6a-present-accounting-unit",
                             "path": "tests/present-accounting/present-accounting-unit",
                             "format": "MACHO_NATIVE_ARCH",
                             "producer_target": "R6A_PRESENT_ACCOUNTING_UNIT"},
                            {"output_id": "r6a-present-accounting-model",
                             "path": "tests/present-accounting/present-accounting-model",
                             "format": "MACHO_NATIVE_ARCH",
                             "producer_target": "R6A_PRESENT_ACCOUNTING_MODEL"},
                        ],
                        "stage_role": "NATIVE_VERIFICATION_EXECUTION_ONLY",
                        "runtime_stage_role": "NATIVE_SDL2_RUNTIME_CLOSURE",
                        "executable_test_eligibility": True,
                        "tool_receipt_ids": list(retention.BUILD_SESSION_DUAL_GRAPH_TOOL_RECEIPTS[
                            "NATIVE_VERIFICATION"]),
                        "runtime_receipt_ids": ["SDL2_COMPAT", "SDL3"],
                        "source_identity": dict(shared_source),
                    },
                ],
                "input_receipts": [{"path": "build-win64.txt", "sha256": retention._hash_path(cross_file)}],
                "runtime_receipts": runtime_receipts,
                "native_host_profile": dict(host_profile),
                "host_dependency_receipts": host_dependency_receipts,
                "steps": [
                    {"kind": "configure", "graph_id": "windows-product-r6a", "cwd": "."},
                    {"kind": "configure", "graph_id": "native-verification-r6a", "cwd": "."},
                    {"kind": "build", "graph_id": "windows-product-r6a",
                     "target_id": "DEFAULT_PRODUCTION_OUTPUTS", "cwd": "."},
                    {"kind": "build", "graph_id": "windows-product-r6a",
                     "target_id": "R6A_PRODUCTION_PATH_PE", "cwd": "."},
                    {"kind": "build", "graph_id": "native-verification-r6a",
                     "target_id": "R6A_PRODUCTION_PATH_MACHO", "cwd": "."},
                    {"kind": "build", "graph_id": "native-verification-r6a",
                     "target_id": "R6A_PRESENT_ACCOUNTING_UNIT", "cwd": "."},
                    {"kind": "build", "graph_id": "native-verification-r6a",
                     "target_id": "R6A_PRESENT_ACCOUNTING_MODEL", "cwd": "."},
                    {"kind": "install", "graph_id": "windows-product-r6a", "cwd": "."},
                    {"kind": "stage_windows_product", "graph_id": "windows-product-r6a",
                     "output_id": "r6a-production-path-pe", "cwd": "."},
                    {"kind": "stage_native_runtime", "graph_id": "native-verification-r6a",
                     "runtime_ids": ["SDL2_COMPAT", "SDL3"], "cwd": "."},
                    {"kind": "test", "graph_id": "native-verification-r6a",
                     "output_id": "r6a-production-path-macho", "test_class": "PRODUCTION_PATH",
                     "arguments": [], "cwd": "."},
                    {"kind": "test", "graph_id": "native-verification-r6a",
                     "output_id": "r6a-present-accounting-unit", "test_class": "UNIT",
                     "arguments": [], "cwd": "."},
                    {"kind": "test", "graph_id": "native-verification-r6a",
                     "output_id": "r6a-present-accounting-model", "test_class": "MODEL",
                     "arguments": [], "cwd": "."},
                    {"kind": "source_verification"},
                ],
            })
        plan_sha256 = hashlib.sha256(retention._canonical_json(plan)).hexdigest()
        self._write_owner_manifest(source_hash, config_hash, build_type="release", dependency_state=dependency_state,
                                   failure_capture_paths=failure_capture_paths,
                                   session_plan_sha256=plan_sha256, admission_identity=admission_identity,
                                   experiment_id=owner_experiment)
        return retention.BuildLease(
            build_id, kind="dxvk", owner_experiment=owner_experiment, purpose="session retention test",
            source_tree_hash=source_hash, expected_bytes=64 * 1024 * 1024,
            metadata={"dependency_state": dependency_state, "build_type": "release",
                      "failure_capture_paths": failure_capture_paths or [], "estimated_debug_bytes": None},
            runner_path=self.runner_file, configuration_key_sha256=config_hash, session_plan=plan)

    @contextmanager
    def _patch_session_popen(self, *, side_effect: object = None,
                             return_value: object = mock.DEFAULT):
        popen = mock.Mock(side_effect=side_effect)
        if return_value is not mock.DEFAULT:
            popen.return_value = return_value
        with mock.patch.object(retention, "subprocess", _SubprocessPopenProxy(popen)):
            yield popen

    @staticmethod
    def _tiny_macho(filetype: int, *, load_paths: tuple[str, ...] = ()) -> bytes:
        import struct

        commands = []
        for load_path in load_paths:
            encoded = load_path.encode("utf-8") + b"\0"
            command_size = ((24 + len(encoded) + 3) // 4) * 4
            command = bytearray(command_size)
            struct.pack_into("<IIIIII", command, 0, 0xC, command_size, 24, 0, 0, 0)
            command[24:24 + len(encoded)] = encoded
            commands.append(bytes(command))
        command_table = b"".join(commands)
        native = platform.machine().lower()
        native = {"aarch64": "arm64", "amd64": "x86_64"}.get(native, native)
        cpu_type = {"arm64": 0x0100000C, "x86_64": 0x01000007}[native]
        header = struct.pack("<IIIIIIII", 0xFEEDFACF, cpu_type, 0, filetype,
                             len(commands), len(command_table), 0, 0)
        return header + command_table

    def _session_popen(self, session: retention.BuildLeaseSession, *, return_code: int = 0,
                       extra_build_bytes: int = 0, interpreter_test: bool = False) -> SimpleNamespace:
        lease_record = retention._load_lease(session.lease.build_id)
        index = lease_record["session_v3"]["next_step_index"]
        step = session.plan["steps"][index]
        root = session.workspace
        if retention._session_dual_graph_enabled(session.plan):
            if step["kind"] == "configure":
                (root / retention._session_dual_graph_by_id(
                    session.plan, step["graph_id"])["build_directory"]).mkdir()
            elif step["kind"] == "build" and step["target_id"] in {
                    "R6A_PRODUCTION_PATH_PE", "R6A_PRODUCTION_PATH_MACHO",
                    "R6A_PRESENT_ACCOUNTING_UNIT", "R6A_PRESENT_ACCOUNTING_MODEL"}:
                graph = retention._session_dual_graph_by_id(session.plan, step["graph_id"])
                output = next(row for row in graph["expected_outputs"]
                              if row["producer_target"] == step["target_id"])
                output_path = root / graph["build_directory"] / output["path"]
                output_path.parent.mkdir(parents=True, exist_ok=True)
                if output["format"] == "PE32PLUS_X86_64":
                    contents = bytearray(90)
                    contents[:2] = b"MZ"
                    contents[0x3C:0x40] = (64).to_bytes(4, "little")
                    contents[64:68] = b"PE\0\0"
                    contents[68:70] = (0x8664).to_bytes(2, "little")
                    contents[88:90] = (0x20B).to_bytes(2, "little")
                else:
                    loads = ("@rpath/libSDL2-2.0.0.dylib",) if (
                        output["output_id"] == "r6a-production-path-macho") else ()
                    contents = self._tiny_macho(2, load_paths=loads)
                output_path.write_bytes(contents)
                output_path.chmod(0o700)
            elif step["kind"] == "install":
                staged = root / "stage/usr/share/synthetic/product.dat"
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_bytes(b"synthetic Windows product output\n")
        elif step["kind"] == "configure":
            (root / "meson-private").mkdir(exist_ok=True)
            (root / "meson-private/cmd_line.txt").write_text("synthetic configure\n", encoding="utf-8")
        elif step["kind"] == "build":
            planned_test = next(row for row in session.plan["steps"] if row["kind"] == "test")
            test_path = root / session.plan["output_root"] / planned_test["executable"]
            test_path.parent.mkdir(parents=True, exist_ok=True)
            test_path.write_bytes(b"#!/usr/bin/python\n" if interpreter_test else b"synthetic test executable\n")
            test_path.chmod(0o700)
            (root / "libsynthetic.dylib").write_bytes(b"synthetic artifact\n")
            (root / "build.ninja").write_text("synthetic graph\n", encoding="utf-8")
            (root / ".ninja_log").write_text("synthetic execution\n", encoding="utf-8")
            if extra_build_bytes:
                (root / "extra-peak.bin").write_bytes(b"p" * extra_build_bytes)
        elif step["kind"] == "install":
            staged = root / "stage/usr/lib/libsynthetic.dylib"
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(b"synthetic staged artifact\n")

        class SyntheticProcess:
            pid = 987654321

            def __init__(self) -> None:
                read_fd, write_fd = os.pipe()
                os.write(write_fd, b"synthetic command output\n")
                os.close(write_fd)
                self.stdout = os.fdopen(read_fd, "rb", buffering=0)

            def wait(self, timeout: float | None = None) -> int:
                return return_code

            def poll(self) -> int:
                return return_code

        process = SyntheticProcess()
        self.addCleanup(process.stdout.close)
        return process

    @contextmanager
    def _real_session_runtime(self):
        with (mock.patch.object(retention, "_attach_session_workspace",
                                _ORIGINAL_ATTACH_SESSION_WORKSPACE),
              mock.patch.object(retention, "_verify_session_workspace_mount",
                                _ORIGINAL_VERIFY_SESSION_WORKSPACE_MOUNT),
              mock.patch.object(retention, "_detach_session_workspace",
                                _ORIGINAL_DETACH_SESSION_WORKSPACE),
              mock.patch.object(retention, "_attach_session_workspace_readonly",
                                _ORIGINAL_ATTACH_SESSION_WORKSPACE_READONLY),
              mock.patch.object(retention, "_verify_session_workspace_readonly",
                                _ORIGINAL_VERIFY_SESSION_WORKSPACE_READONLY),
              mock.patch.object(retention, "_detach_session_workspace_readonly",
                                _ORIGINAL_DETACH_SESSION_WORKSPACE_READONLY),
              mock.patch.object(retention, "_contain_session_processes",
                                _ORIGINAL_CONTAIN_SESSION_PROCESSES)):
            yield

    def _compile_session_test(self, session: retention.BuildLeaseSession,
                              source_text: str, *, label: str = "session-test-helper") -> Path:
        compiler = Path("/usr/bin/cc")
        if not compiler.is_file():
            self.skipTest("the Darwin C compiler is unavailable for sandbox integration tests")
        source = self.repo / f"{label}.c"
        source.write_text(source_text, encoding="utf-8")
        planned = next(row for row in session.plan["steps"] if row["kind"] == "test")
        output = session.workspace / session.plan["output_root"] / planned["executable"]
        output.parent.mkdir(parents=True, exist_ok=True)
        output.unlink(missing_ok=True)
        result = subprocess.run([str(compiler), "-O0", str(source), "-o", str(output)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        output.chmod(0o700)
        return output

    def _run_session_step(self, session: retention.BuildLeaseSession, expected_kind: str, **kwargs: object) -> dict:
        index = retention._load_lease(session.lease.build_id)["session_v3"]["next_step_index"]
        self.assertEqual(session.plan["steps"][index]["kind"], expected_kind)
        with self._patch_session_popen(
                side_effect=lambda *args, **call_kwargs: self._session_popen(session, **kwargs)):
            return session.run_next()

    def _start_session(self, build_id: str = "session-test") -> tuple[retention.BuildLease, retention.BuildLeaseSession]:
        lease = self._session_lease(build_id)
        lease.reserve()
        session = lease.start_session()
        return lease, session

    def _assert_failed_session_retired(self, lease: retention.BuildLease,
                                       session: retention.BuildLeaseSession) -> tuple[dict, dict, dict]:
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["retirement_status"], "COMPLETED")
        self.assertEqual(record["retirement_from_state"], "FAILED")
        self.assertEqual(record["session_v3"]["state"], "FAILED")
        self.assertTrue(record["session_v3"].get("acceptance_state", "").startswith("REJECTED_"))
        self.assertEqual(record["session_v3"]["failed_step_index"], 0)
        self.assertFalse(lease.path.exists())

        receipt_path = retention.RECEIPT_ROOT / f"{lease.build_id}.json"
        self.assertEqual(record["cleanup_receipt_path"], str(receipt_path))
        cleanup_receipt = retention._read_json(receipt_path, sealed=True)
        self.assertEqual(cleanup_receipt["category"], "FAILED BUILD DIRECTORY")
        self.assertEqual(cleanup_receipt["state_before"], "FAILED")
        self.assertEqual(cleanup_receipt["retirement_status"], "COMPLETED")
        self.assertEqual(cleanup_receipt["execution_outcome"], "FAILED")
        self.assertIs(cleanup_receipt["outputs_accepted"], False)
        self.assertEqual(cleanup_receipt["failure_evidence_index_sha256"],
                         record["failure_evidence_index_sha256"])
        self.assertEqual(cleanup_receipt["result"], "REMOVED")

        failure_path = Path(record["failure_evidence_path"])
        self.assertEqual(failure_path,
                         retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json")
        failure_manifest = retention._read_json(failure_path)
        self.assertEqual(failure_manifest["build_id"], lease.build_id)
        self.assertEqual(failure_manifest["return_code"], record["return_code"])
        self.assertEqual(failure_manifest["command"], record["failed_command"])
        self.assertIs(failure_manifest["outputs_accepted"], False)
        self.assertEqual(failure_manifest["failure_reason_sha256"],
                         record["session_v3"]["failure_reason_sha256"])
        self.assertEqual(record["failure_reason_sha256"],
                         record["session_v3"]["failure_reason_sha256"])
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
        self.assertFalse((session.session_dir / retention.BUILD_SESSION_TERMINAL).exists())
        return record, cleanup_receipt, failure_manifest

    @staticmethod
    def _output(lease: retention.BuildLease, payload: bytes = b"synthetic DLL") -> None:
        (lease.path / "synthetic.dll").write_bytes(payload)
        (lease.path / "build.ninja").write_text("synthetic graph", encoding="utf-8")
        (lease.path / ".ninja_log").write_text("synthetic execution", encoding="utf-8")

    def _complete_without_retirement(self, lease: retention.BuildLease) -> dict:
        self._authorize_synthetic_execution(lease)
        with mock.patch.object(retention, "_retire", return_value={"result": "PENDING"}):
            result = lease.complete()
        lease._release_lock()
        return result

    @staticmethod
    def _authorize_synthetic_execution(lease: retention.BuildLease) -> None:
        (lease.path / "build.log").write_bytes(b"synthetic successful build log\n")
        with (lease.path / "build.log").open("rb") as stream:
            os.fsync(stream.fileno())
        native_request_data = lease.metadata.get("native_moltenvk_request")
        native_kwargs = {}
        if native_request_data:
            request = retention._dict_to_native_request(native_request_data)
            declared_environment = dict(request.normalized_environment)
            temporary_path = str(lease.path / ".tmp")
            effective_environment = {
                **declared_environment,
                "TMPDIR": temporary_path,
                "TMP": temporary_path,
                "TEMP": temporary_path,
            }
            native_kwargs = {
                "declared_environment": declared_environment,
                "effective_environment": effective_environment,
                "native_toolchain_observation": _synthetic_native_toolchain_observation(request),
            }
        lease._record_successful_sandboxed_run(
            ["meson", "compile", "-C", str(lease.path)], time.time(),
            retention._sandbox_profile_text(lease.path),
            retention._hash_path(Path("/usr/bin/sandbox-exec")),
            **native_kwargs)

    @staticmethod
    def _complete_native(lease: retention.BuildLease) -> dict:
        request = retention._dict_to_native_request(lease.metadata["native_moltenvk_request"])
        with mock.patch.object(
                retention, "_capture_native_xcode_toolchain",
                return_value=_synthetic_native_toolchain_observation(request)):
            return lease.complete()

    def test_completed_build_is_automatically_removed(self) -> None:
        lease = self._lease("completed-auto")
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        result = lease.complete()
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")
        self.assertFalse(lease.path.exists())
        self.assertEqual(result["cleanup_receipt"]["result"], "REMOVED")
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "COMPLETED")
        self.assertEqual(record["retirement_status"], "COMPLETED")
        self.assertEqual(result["cleanup_receipt"]["execution_outcome"], "SUCCEEDED")
        self.assertIs(result["cleanup_receipt"]["outputs_accepted"], True)

    def test_new_build_kind_is_bound_to_owner_manifest_and_capacity_class(self) -> None:
        with self.assertRaisesRegex(ValueError, "capped DXVK, MoltenVK, or reference/debug kind"):
            self._lease("uncapped-wine-alias", kind="wine")

        lease = self._lease("kind-manifest-mismatch")
        self._write_owner_manifest(lease.source_tree_hash,
                                   lease.configuration_key_sha256, build_kind="moltenvk")
        with self.assertRaisesRegex(retention.RetentionError, "differs from its signed experiment build contract"):
            lease.reserve()

    def test_active_build_is_preserved(self) -> None:
        lease = self._lease("active-preserved")
        lease.reserve()
        self._output(lease)
        self.assertTrue(lease.path.is_dir())
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "ACTIVE")
        lease.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_preserve_debug_build_is_preserved(self) -> None:
        lease = self._lease("debug-preserved", preserve=True)
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        result = lease.complete(preserve_debug=True)
        self.assertEqual(result["status"], "PRESERVE_DEBUG")
        self.assertTrue(lease.path.is_dir())
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "PRESERVE_DEBUG")
        lease._release_lock()
        with mock.patch.object(retention, "_retire", side_effect=retention.RetentionError("injected close retry")):
            with self.assertRaisesRegex(retention.RetentionError, "injected close retry"):
                retention.close_debug_preservation(lease.build_id, reason="synthetic debug task closed")
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "COMPLETED")
        receipt = retention.close_debug_preservation(lease.build_id, reason="synthetic debug task closed")
        self.assertEqual(receipt["result"], "REMOVED")
        self.assertFalse(lease.path.exists())

    def test_full_debug_build_requires_signed_reason_and_estimate(self) -> None:
        with self.assertRaisesRegex(ValueError, "full debug/unstripped builds require a reason"):
            self._lease("debug-without-opt-in", build_type="debug")
        lease = self._lease("debug-with-explicit-opt-in", build_type="debug",
                            debug_reason="Inspect optimizer-sensitive compiler output",
                            estimated_debug_bytes=32 * 1024 * 1024)
        lease.reserve()
        saved = retention._load_lease(lease.build_id)
        self.assertEqual(saved["metadata"]["build_type"], "debug")
        self.assertEqual(saved["metadata"]["estimated_debug_bytes"], 32 * 1024 * 1024)
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        result = lease.complete()
        manifest, _ = retention._verify_build_manifest(lease.build_id)
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")
        self.assertEqual(manifest["requested_build_type"], "debug")
        self.assertEqual(manifest["build_type"], "debug")
        self.assertEqual(manifest["estimated_debug_bytes"], 32 * 1024 * 1024)
        self.assertEqual(manifest["preserve_debug_reason"], "Inspect optimizer-sensitive compiler output")

    def test_signed_failure_capture_path_is_retained_then_build_is_removed(self) -> None:
        lease = self._lease("selected-generated-failure",
                            failure_capture_paths=["generated/diagnostic.c", "generated__diagnostic.c"])
        lease.reserve()
        self._output(lease)
        generated = lease.path / "generated/diagnostic.c"
        generated.parent.mkdir()
        generated.write_text("compiler diagnostic source\n", encoding="utf-8")
        colliding_flattened_name = lease.path / "generated__diagnostic.c"
        colliding_flattened_name.write_text("different file with former flat-name collision\n", encoding="utf-8")
        receipt = lease.fail(command=["synthetic"], return_code=2, error="compile failed")
        self.assertEqual(receipt["result"], "REMOVED")
        self.assertFalse(lease.path.exists())
        evidence = retention.MANIFEST_ROOT / f"{lease.build_id}.failure" / "failure-manifest.json"
        failure = retention._read_json(evidence)
        retained = {row["relative_path"]: row for row in failure["generated_diagnostics"]}
        self.assertEqual(retained["generated/diagnostic.c"]["status"], "CAPTURED")
        self.assertEqual(retained["generated__diagnostic.c"]["status"], "CAPTURED")
        self.assertNotEqual(retained["generated/diagnostic.c"]["retained_path"],
                            retained["generated__diagnostic.c"]["retained_path"])
        self.assertEqual(retained["generated/diagnostic.c"]["sha256"],
                         hashlib.sha256(b"compiler diagnostic source\n").hexdigest())

    def test_missing_optional_failure_capture_is_recorded_and_build_is_removed(self) -> None:
        lease = self._lease("missing-optional-diagnostic", failure_capture_paths=["generated/maybe.c"])
        lease.reserve()
        self._output(lease)
        result = lease.fail(command=["synthetic"], return_code=1, error="failed before source generation")
        self.assertEqual(result["result"], "REMOVED")
        manifest = retention._read_json(
            retention.MANIFEST_ROOT / f"{lease.build_id}.failure" / "failure-manifest.json")
        self.assertEqual(manifest["generated_diagnostics"], [
            {"relative_path": "generated/maybe.c", "status": "MISSING"}])
        self.assertFalse(lease.path.exists())

    def test_symlinked_selected_failure_diagnostic_refuses_retirement(self) -> None:
        lease = self._lease("unsafe-selected-diagnostic", failure_capture_paths=["generated/link.c"])
        lease.reserve()
        outside = self.repo / "outside-diagnostic.c"
        outside.write_text("outside source must remain untouched\n", encoding="utf-8")
        selected = lease.path / "generated/link.c"
        selected.parent.mkdir()
        selected.symlink_to(outside)
        with self.assertRaisesRegex(retention.RetentionError, "is a symlink"):
            lease.fail(command=["synthetic"], return_code=1, error="unsafe diagnostic")
        self.assertTrue(lease.path.is_dir())
        self.assertEqual(outside.read_text(encoding="utf-8"), "outside source must remain untouched\n")
        selected.unlink()
        lease.fail(command=["synthetic"], return_code=1, error="retry without unsafe link")

    def test_expired_preserved_debug_build_is_retired_during_reconciliation(self) -> None:
        lease = self._lease("debug-expiry-reconcile", preserve=True)
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        lease.complete(preserve_debug=True)
        expires = retention._load_lease(lease.build_id)["preserve_debug_expires_at_unix"]
        lock_fds = retention._lock()
        try:
            with mock.patch.object(retention.time, "time", return_value=expires + 1):
                receipts = retention._retire_pending_locked()
        finally:
            retention._unlock(*lock_fds)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["result"], "REMOVED")
        self.assertEqual(retention._load_lease(lease.build_id)["retirement_reason"],
                         "debug-preservation-expired")
        self.assertFalse(lease.path.exists())

    def test_preserved_debug_slot_blocks_legacy_promotion_before_artifactization(self) -> None:
        retained = self._lease("debug-slot-occupant", preserve=True)
        retained.reserve()
        self._output(retained)
        self._authorize_synthetic_execution(retained)
        retained.complete(preserve_debug=True)

        second_debug = self._lease("debug-slot-second-request", preserve=True,
                                   source_tree_hash=hashlib.sha256(b"different debug source").hexdigest())
        second_debug.reserve()
        self._output(second_debug)
        self._authorize_synthetic_execution(second_debug)
        with self.assertRaisesRegex(retention.RetentionError, "preservation slot is occupied"):
            second_debug.complete(preserve_debug=True)
        self.assertTrue(second_debug.path.is_dir())
        second_debug.fail(command=["synthetic"], return_code=-1, error="preservation cap refused")

        parent = self.repo / "legacy-debug-promotion-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-debug-promotion"
        legacy.mkdir(mode=0o700)
        (legacy / "synthetic.dll").write_bytes(b"debug slot probe")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(
                legacy, kind="reference", owner_experiment="synthetic-test", purpose="slot promotion test",
                expected_expiry_unix=time.time() + 3600,
                preserve_debug_reason="Retain only if the configured debug slot is available")
            with self.assertRaisesRegex(retention.RetentionError, "preservation slot is occupied"):
                retention.complete_adopted(adopted["build_id"], preserve_debug=True)
            self.assertTrue(Path(adopted["path"]).is_dir())
            self.assertFalse((retention.MANIFEST_ROOT / f"{adopted['build_id']}.json").exists())
            retention.fail_adopted(adopted["build_id"], reason="promotion refused", capture_paths=())
        retention.close_debug_preservation(retained.build_id, reason="synthetic debug task closed")

    def test_legacy_adoption_respects_global_build_cap_before_move(self) -> None:
        dxvk = self._lease("adoption-cap-dxvk")
        dxvk.reserve()
        dxvk._release_lock()
        moltenvk = self._lease("adoption-cap-moltenvk", kind="moltenvk")
        moltenvk.reserve()
        moltenvk._release_lock()
        parent = self.repo / "legacy-adoption-cap-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-adoption-cap"
        legacy.mkdir(mode=0o700)
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            with self.assertRaisesRegex(retention.RetentionError, "global active large-build cap"):
                retention.adopt_legacy(legacy, kind="reference", owner_experiment="synthetic-test",
                                       purpose="adoption cap test")
        self.assertTrue(legacy.is_dir())
        self.assertFalse(list(retention.BUILD_ROOT.glob("legacy-adoption-cap-*")))
        moltenvk.fail(command=["synthetic"], return_code=1, error="test cleanup")
        dxvk.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_active_dxvk_cap_blocks_another_build(self) -> None:
        first = self._lease("cap-first")
        first.reserve()
        first._release_lock()
        with self.assertRaises(retention.RetentionError):
            self._lease("cap-second").reserve()
        first.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_one_dxvk_and_one_moltenvk_can_coexist_but_third_active_build_is_blocked(self) -> None:
        dxvk = self._lease("active-dxvk-serialized")
        dxvk.reserve()
        dxvk._release_lock()
        moltenvk = self._lease("active-moltenvk-serialized", kind="moltenvk")
        moltenvk.reserve()
        moltenvk._release_lock()
        extra = self._lease("active-extra-dxvk-serialized", source_tree_hash=hashlib.sha256(b"extra source").hexdigest())
        with self.assertRaisesRegex(retention.RetentionError, "global active large-build cap"):
            extra.reserve()
        self.assertFalse(extra.path.exists())
        moltenvk.fail(command=["synthetic"], return_code=1, error="test cleanup")
        dxvk.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_completed_build_retires_before_new_capacity_is_granted(self) -> None:
        first = self._lease("cap-retire-first")
        first.reserve()
        self._output(first)
        self._complete_without_retirement(first)
        self.assertTrue(first.path.exists())
        second = self._lease("cap-retire-second", source_tree_hash=hashlib.sha256(b"different source").hexdigest())
        second.reserve()
        self.assertFalse(first.path.exists())
        self.assertTrue(second.path.exists())
        second.fail(command=["synthetic"], return_code=1, error="test cleanup")

    def test_missing_artifact_refuses_deletion(self) -> None:
        lease = self._lease("missing-artifact")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        manifest = retention._read_json(retention.MANIFEST_ROOT / f"{lease.build_id}.json")
        payload = Path(manifest["artifacts"][0]["canonical_artifact_path"])
        payload.unlink()
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(lease.path.exists())

    def test_artifact_hash_mismatch_refuses_deletion(self) -> None:
        lease = self._lease("bad-artifact-hash")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        manifest = retention._read_json(retention.MANIFEST_ROOT / f"{lease.build_id}.json")
        payload = Path(manifest["artifacts"][0]["canonical_artifact_path"])
        payload.chmod(0o600)
        payload.write_bytes(b"tampered")
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(lease.path.exists())

    def test_symlink_escape_is_refused(self) -> None:
        lease = self._lease("symlink-escape")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        outside = self.repo / "outside.txt"
        outside.write_text("keep", encoding="utf-8")
        (lease.path / "escape").symlink_to(outside)
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(outside.exists())
        self.assertTrue(lease.path.exists())

    def test_path_replacement_race_is_refused(self) -> None:
        lease = self._lease("path-replaced")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        displaced = lease.path.with_name(lease.path.name + ".displaced")
        lease.path.rename(displaced)
        lease.path.mkdir(mode=0o700)
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(displaced.exists())
        self.assertTrue(lease.path.exists())

    def test_namespace_swap_at_quarantine_is_refused_without_deleting_original(self) -> None:
        lease = self._lease("quarantine-race")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        real_rename = retention._rename_exclusive
        displaced = lease.path.with_name(lease.path.name + ".raced-original")
        def swap_then_rename(parent_fd: int, source: str, target: str) -> None:
            os.rename(source, displaced.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            os.mkdir(source, 0o700, dir_fd=parent_fd)
            real_rename(parent_fd, source, target)
        with mock.patch.object(retention, "_rename_exclusive", side_effect=swap_then_rename):
            lock_fds = retention._lock()
            try:
                with self.assertRaises(retention.RetentionError):
                    retention._retire(retention._load_lease(lease.build_id), reason="test-race")
            finally:
                retention._unlock(*lock_fds)
        self.assertTrue((displaced / "synthetic.dll").is_file())
        current = retention._load_lease(lease.build_id)
        self.assertTrue((retention.BUILD_ROOT / current["quarantine_name"]).is_dir())

    def test_legacy_adoption_replacement_is_rolled_back_and_lease_removed(self) -> None:
        parent = self.repo / "legacy-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-build"
        legacy.mkdir(mode=0o700)
        (legacy / "original.txt").write_text("original directory\n", encoding="utf-8")
        replacement_backup = parent / "original-renamed-by-racer"
        original_rename = retention._rename_exclusive_between

        def replace_source_then_move(source_fd: int, source: str, target_fd: int, target: str) -> None:
            if source == legacy.name and target_fd != source_fd:
                os.rename(source, replacement_backup.name, src_dir_fd=source_fd, dst_dir_fd=source_fd)
                os.mkdir(source, 0o700, dir_fd=source_fd)
                replacement_fd = os.open(source, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0), dir_fd=source_fd)
                try:
                    descriptor = os.open("replacement.txt", os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                         0o600, dir_fd=replacement_fd)
                    os.write(descriptor, b"replacement directory\n")
                    os.close(descriptor)
                finally:
                    os.close(replacement_fd)
            original_rename(source_fd, source, target_fd, target)

        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)), \
                mock.patch.object(retention, "_rename_exclusive_between", side_effect=replace_source_then_move):
            with self.assertRaisesRegex(retention.RetentionError, "namespace was rolled back"):
                retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                       purpose="race regression")
        self.assertTrue((legacy / "replacement.txt").is_file())
        self.assertTrue((replacement_backup / "original.txt").is_file())
        self.assertFalse(list(retention.BUILD_ROOT.iterdir()))
        self.assertFalse(list(retention.LEASE_ROOT.glob("legacy-*.json")))

    def test_legacy_adoption_lease_persistence_failure_rolls_back(self) -> None:
        parent = self.repo / "legacy-lease-write-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-build"
        legacy.mkdir(mode=0o700)
        (legacy / "source.txt").write_text("original directory\n", encoding="utf-8")
        original_store = retention._store_lease

        def fail_moved_lease(lease):
            if lease.get("adopted_at_unix") is not None:
                raise OSError("injected lease persistence failure")
            return original_store(lease)

        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)), \
                mock.patch.object(retention, "_store_lease", side_effect=fail_moved_lease):
            with self.assertRaisesRegex(retention.RetentionError, "namespace and provisional lease were rolled back"):
                retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                       purpose="lease persistence failure recovery")
        self.assertEqual((legacy / "source.txt").read_text(encoding="utf-8"), "original directory\n")
        self.assertFalse((legacy / retention.BUILD_MARKER).exists())
        self.assertEqual(list(retention.BUILD_ROOT.iterdir()), [])
        self.assertEqual(list(retention.LEASE_ROOT.glob("legacy-legacy-build-*.json")), [])

    def test_legacy_adoption_marker_write_failure_rolls_back(self) -> None:
        parent = self.repo / "legacy-marker-write-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-build"
        legacy.mkdir(mode=0o700)
        (legacy / "source.txt").write_text("original directory\n", encoding="utf-8")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)), \
                mock.patch.object(retention, "_write_marker", side_effect=OSError("injected marker write failure")):
            with self.assertRaisesRegex(retention.RetentionError, "namespace and provisional lease were rolled back"):
                retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                       purpose="marker persistence failure recovery")
        self.assertEqual((legacy / "source.txt").read_text(encoding="utf-8"), "original directory\n")
        self.assertFalse((legacy / retention.BUILD_MARKER).exists())
        self.assertEqual(list(retention.BUILD_ROOT.iterdir()), [])
        self.assertEqual(list(retention.LEASE_ROOT.glob("legacy-legacy-build-*.json")), [])

    def test_legacy_adoption_directory_fsync_failure_rolls_back(self) -> None:
        parent = self.repo / "legacy-fsync-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-build"
        legacy.mkdir(mode=0o700)
        (legacy / "source.txt").write_text("original directory\n", encoding="utf-8")
        original_identity = legacy.stat()
        original_fsync = os.fsync
        root_fsyncs = 0

        def fail_final_root_fsync(fd: int) -> None:
            nonlocal root_fsyncs
            info = os.fstat(fd)
            if stat.S_ISDIR(info.st_mode) and (info.st_dev, info.st_ino) == (
                    original_identity.st_dev, original_identity.st_ino):
                root_fsyncs += 1
                if root_fsyncs == 2:
                    raise OSError("injected final directory fsync failure")
            original_fsync(fd)

        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)), \
                mock.patch.object(retention.os, "fsync", side_effect=fail_final_root_fsync):
            with self.assertRaisesRegex(retention.RetentionError, "namespace and provisional lease were rolled back"):
                retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                       purpose="directory fsync failure recovery")
        self.assertEqual(root_fsyncs, 3)  # marker durability, injected failure, then marker removal
        self.assertEqual((legacy / "source.txt").read_text(encoding="utf-8"), "original directory\n")
        self.assertFalse((legacy / retention.BUILD_MARKER).exists())
        self.assertEqual(list(retention.BUILD_ROOT.iterdir()), [])
        self.assertEqual(list(retention.LEASE_ROOT.glob("legacy-legacy-build-*.json")), [])

    def test_legacy_failure_capture_is_selective_and_retryable(self) -> None:
        parent = self.repo / "legacy-failure-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-failure-build"
        legacy.mkdir(mode=0o700)
        (legacy / "config.log").write_text("configure failed: synthetic diagnostic\n", encoding="utf-8")
        (legacy / "unneeded-bulk.bin").write_bytes(b"bulk output" * 64)
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="wine", owner_experiment="synthetic-test",
                                             purpose="selected failure evidence test")
            build_id = adopted["build_id"]
            with mock.patch.object(retention, "_retire", side_effect=retention.RetentionError("injected")):
                with self.assertRaisesRegex(retention.RetentionError, "injected"):
                    retention.fail_adopted(build_id, reason="synthetic configure failure",
                                           capture_paths=("config.log",))
            self.assertEqual(retention._load_lease(build_id)["state"], "FAILED")
            result = retention.fail_adopted(build_id, reason="synthetic configure failure",
                                             capture_paths=("config.log",))
        self.assertEqual(result["result"], "REMOVED")
        self.assertFalse(Path(adopted["path"]).exists())
        evidence = retention.MANIFEST_ROOT / f"{build_id}.failure"
        failure_manifest = retention._read_json(evidence / "failure-manifest.json")
        self.assertEqual(failure_manifest["capture_paths"], ["config.log"])
        self.assertEqual(len(failure_manifest["retained_files"]), 1)
        retained = Path(failure_manifest["retained_files"][0]["retained_path"])
        self.assertTrue(retained.is_file())
        self.assertNotIn("unneeded-bulk.bin", retained.name)

    def test_legacy_copy_all_failure_capture_requires_a_bounded_small_root(self) -> None:
        parent = self.repo / "legacy-large-failure-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-large-failure-build"
        legacy.mkdir(mode=0o700)
        with (legacy / "oversized-diagnostic.bin").open("wb") as stream:
            stream.truncate(retention.MAX_COPY_ALL_FAILURE_ROOT_BYTES + 1)
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                             purpose="copy-all size refusal")
            with self.assertRaisesRegex(retention.RetentionError, "copy-all legacy failure evidence"):
                retention.fail_adopted(adopted["build_id"], reason="synthetic failure")
        self.assertTrue(Path(adopted["path"]).is_dir())
        self.assertFalse((retention.MANIFEST_ROOT / f"{adopted['build_id']}.failure" / "failure-manifest.json").exists())

    def test_legacy_copy_all_failure_capture_bounds_many_empty_files_before_capture(self) -> None:
        parent = self.repo / "legacy-many-empty-files-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-many-empty-files-build"
        legacy.mkdir(mode=0o700)
        for index in range(9):
            (legacy / f"empty-{index:02d}.log").touch()
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                             purpose="bounded empty-file scan")
            with mock.patch.object(retention, "MAX_FAILURE_CAPTURE_SCAN_ENTRIES", 8):
                with self.assertRaisesRegex(retention.RetentionError, "bounded entry-count limit"):
                    retention.fail_adopted(adopted["build_id"], reason="too many empty diagnostics")
        evidence = retention.MANIFEST_ROOT / f"{adopted['build_id']}.failure"
        self.assertTrue(Path(adopted["path"]).is_dir())
        self.assertFalse((evidence / "failure-manifest.json").exists())
        self.assertFalse((evidence / "configuration" / "files").exists())

    def test_bounded_tree_scan_closes_queued_directory_descriptors_on_refusal(self) -> None:
        root = self.repo / "nested-scan-bound"
        root.mkdir(mode=0o700)
        for index in range(8):
            (root / f"child-{index:02d}").mkdir(mode=0o700)
        root_fd = retention._open_dir(root)
        opened_child_fds: list[int] = []
        real_open = os.open

        def tracking_open(*args: object, **kwargs: object) -> int:
            descriptor = real_open(*args, **kwargs)
            opened_child_fds.append(descriptor)
            return descriptor

        try:
            with mock.patch.object(retention.os, "open", side_effect=tracking_open):
                with self.assertRaisesRegex(retention.RetentionError, "bounded entry-count limit"):
                    retention._tree_stats(root_fd, os.fstat(root_fd).st_dev, root,
                                          max_scan_entries=2)
            self.assertGreater(len(opened_child_fds), 0)
            for descriptor in opened_child_fds:
                with self.assertRaises(OSError):
                    os.fstat(descriptor)
        finally:
            os.close(root_fd)

    def test_legacy_copy_all_failure_capture_bounds_empty_symlink_fanout(self) -> None:
        parent = self.repo / "legacy-symlink-fanout-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-symlink-fanout"
        legacy.mkdir(mode=0o700)
        for index in range(retention.MAX_FAILURE_CAPTURE_FILES + 1):
            (legacy / f"link-{index:02d}").symlink_to("target")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                             purpose="symlink fanout limit")
            with self.assertRaisesRegex(retention.RetentionError, "bounded evidence entry count"):
                retention.fail_adopted(adopted["build_id"], reason="too many symlink descriptions")
        self.assertTrue(Path(adopted["path"]).is_dir())
        self.assertFalse((retention.MANIFEST_ROOT / f"{adopted['build_id']}.failure" / "failure-manifest.json").exists())

    def test_evidence_copy_rejects_fifo_without_blocking(self) -> None:
        source = self.repo / "failure-fifo"
        os.mkfifo(source, 0o600)
        with self.assertRaisesRegex(retention.RetentionError, "not a regular file"):
            retention._copy_evidence(source, self.repo / "evidence/fifo-copy")

    def test_legacy_artifactization_refuses_missing_dependency_state(self) -> None:
        parent = self.repo / "legacy-complete-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-complete-build"
        legacy.mkdir(mode=0o700)
        (legacy / "synthetic.dll").write_bytes(b"legacy product")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(legacy, kind="dxvk", owner_experiment="synthetic-test",
                                             purpose="legacy artifactization provenance test")
            with mock.patch.object(retention, "_capture_identity", return_value=retention._empty_identity()):
                with self.assertRaisesRegex(retention.RetentionError, "lacks dependency-state evidence"):
                    retention.complete_adopted(adopted["build_id"])
        self.assertEqual(retention._load_lease(adopted["build_id"])["state"], "ACTIVE")
        self.assertTrue(Path(adopted["path"]).is_dir())

    def test_legacy_reference_debug_preservation_binds_outputs_and_expires(self) -> None:
        parent = self.repo / "legacy-debug-parent"
        parent.mkdir(mode=0o700)
        legacy = parent / "legacy-debug-build"
        legacy.mkdir(mode=0o700)
        outputs = {"FGMetalBridge.dll": b"synthetic PE output", "fgmetalbridge.so": b"synthetic Mach-O output"}
        for name, payload in outputs.items():
            (legacy / name).write_bytes(payload)
        (legacy / "bridge_pe.o").write_bytes(b"debug object PE")
        (legacy / "bridge_unix.o").write_bytes(b"debug object Unix")
        context = self.repo / "experiments/storage/legacy-run.json"
        context.write_text(__import__("json").dumps({
            "dll_sha256": {name: hashlib.sha256(payload).hexdigest() for name, payload in outputs.items()}}),
            encoding="utf-8")
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (legacy,)):
            adopted = retention.adopt_legacy(
                legacy, kind="reference", owner_experiment="synthetic-test",
                purpose="legacy debug retention validation",
                expected_expiry_unix=time.time() + 3600,
                preserve_debug_reason="Synthetic reference build is retained for one-hour debug validation.")
            with mock.patch.object(retention, "_capture_identity", return_value=retention._empty_identity()):
                result = retention.complete_adopted(adopted["build_id"], preserve_debug=True,
                                                    legacy_debug_context_paths=(context,))
        self.assertEqual(result["status"], "PRESERVE_DEBUG")
        self.assertTrue(Path(adopted["path"]).is_dir())
        manifest, _ = retention._verify_build_manifest(adopted["build_id"])
        self.assertIsNone(manifest["config_key_sha256"])
        self.assertEqual(manifest["configuration_key_provenance"], "legacy-debug-preservation-only")
        self.assertEqual(len(manifest["build_identity"]["legacy_debug_symbols"]), 2)

    def test_child_name_swap_between_stat_and_open_is_refused(self) -> None:
        root = retention.BUILD_ROOT / "child-race"
        root.mkdir(parents=True, mode=0o700)
        child = root / "payload"
        child.write_text("old", encoding="utf-8")
        root_fd = retention._open_dir(root)
        root_device = os.fstat(root_fd).st_dev
        real_stat = os.stat
        swapped = False
        def swap_after_stat(path, *args, **kwargs):
            nonlocal swapped
            result = real_stat(path, *args, **kwargs)
            if path == "payload" and kwargs.get("dir_fd") == root_fd and not swapped:
                swapped = True
                os.rename("payload", "old-payload", src_dir_fd=root_fd, dst_dir_fd=root_fd)
                fd = os.open("payload", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=root_fd)
                os.write(fd, b"replacement")
                os.close(fd)
            return result
        try:
            with mock.patch.object(retention.os, "stat", side_effect=swap_after_stat):
                with self.assertRaises(retention.RetentionError):
                    retention._remove_tree_contents(root_fd, root_device, root)
        finally:
            os.close(root_fd)
        self.assertEqual((root / "payload").read_text(encoding="utf-8"), "replacement")
        self.assertEqual((root / "old-payload").read_text(encoding="utf-8"), "old")

    def test_cleanup_failure_is_fatal_and_recoverable(self) -> None:
        lease = self._lease("cleanup-failure")
        lease.reserve()
        self._output(lease)
        with mock.patch.object(retention, "_remove_tree_contents", side_effect=OSError("injected cleanup failure")):
            self._authorize_synthetic_execution(lease)
            with self.assertRaises(retention.RetentionError):
                lease.complete()
        lease._release_lock()
        current = retention._load_lease(lease.build_id)
        self.assertEqual(current["state"], "RETIRING")
        self.assertTrue((retention.BUILD_ROOT / current["quarantine_name"]).exists())
        receipts = retention.reconcile_pending()
        self.assertTrue(receipts)
        self.assertFalse(lease.path.exists())

    def test_cleanup_failure_fails_the_build_command(self) -> None:
        lease = self._lease("cleanup-failure-command")
        command = ["meson", "compile", "-C", "."]

        class CompletedSyntheticBuild:
            returncode = None
            args = command
            pid = os.getpid()

            def wait(self, timeout=None):
                (lease.path / "synthetic.dll").write_bytes(b"command output")
                self.returncode = 0
                return 0

            def poll(self):
                return self.returncode

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def communicate(self, input=None, timeout=None):
                return (b"", b"")

        real_popen = retention.subprocess.Popen

        def synthetic_popen(command, *args, **kwargs):
            if isinstance(command, (list, tuple)) and command[:1] == ["/usr/bin/sandbox-exec"]:
                return CompletedSyntheticBuild()
            return real_popen(command, *args, **kwargs)

        with mock.patch.object(retention, "_remove_tree_contents", side_effect=OSError("injected cleanup failure")):
            with mock.patch.object(retention.subprocess, "Popen", side_effect=synthetic_popen):
                with self.assertRaises(retention.RetentionError):
                    lease.run(command)
        current = retention._load_lease(lease.build_id)
        self.assertEqual(current["state"], "RETIRING")
        self.assertTrue((retention.BUILD_ROOT / current["quarantine_name"]).is_dir())
        self.assertTrue(retention.reconcile_pending())
        self.assertFalse(lease.path.exists())

    def test_failed_build_closes_supervisor_log_before_liveness_check(self) -> None:
        lease = self._lease("failed-build-closes-log")
        command = ["meson", "compile", "-C", "."]

        class FailedSyntheticBuild:
            pid = os.getpid()

            def wait(self, timeout=None):
                return 41

            def poll(self):
                return 41

        real_popen = retention.subprocess.Popen

        def synthetic_popen(args, *positional, **options):
            if isinstance(args, (list, tuple)) and args[:1] == ["/usr/bin/sandbox-exec"]:
                return FailedSyntheticBuild()
            return real_popen(args, *positional, **options)

        with mock.patch.object(retention, "_require_no_live_process",
                               side_effect=_ORIGINAL_REQUIRE_NO_LIVE_PROCESS):
            with mock.patch.object(retention.subprocess, "Popen", side_effect=synthetic_popen):
                with self.assertRaises(subprocess.CalledProcessError) as raised:
                    lease.run(command)

        self.assertEqual(raised.exception.returncode, 41)
        self.assertFalse(lease.path.exists())
        failure = retention._read_json(
            retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json")
        self.assertEqual(failure["return_code"], 41)
        self.assertIsNone(failure["error"])

    def test_build_emergency_free_space_stop_terminates_and_records_failure_evidence(self) -> None:
        lease = self._lease("build-emergency-free-stop")
        command = ["meson", "compile", "-C", "."]

        class RunningSyntheticBuild:
            args = command
            pid = 987654
            returncode = None

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                if self.returncode is None and timeout is not None:
                    raise subprocess.TimeoutExpired(self.args, timeout)
                self.returncode = -15
                return self.returncode

        process = RunningSyntheticBuild()
        real_popen = retention.subprocess.Popen

        def synthetic_popen(args, *positional, **options):
            if isinstance(args, (list, tuple)) and args[:1] == ["/usr/bin/sandbox-exec"]:
                return process
            return real_popen(args, *positional, **options)

        def terminate_synthetic_group(pid, signum):
            self.assertEqual(pid, process.pid)
            self.assertEqual(signum, retention.signal.SIGTERM)
            process.returncode = -15

        with mock.patch.object(storage_policy, "disk_free_bytes",
                               side_effect=[60 * retention.GIB, 24 * retention.GIB]):
            with mock.patch.object(retention.subprocess, "Popen", side_effect=synthetic_popen):
                with mock.patch.object(retention.os, "killpg", side_effect=terminate_synthetic_group) as killpg:
                    with self.assertRaisesRegex(retention.RetentionError, "25 GiB emergency hard stop"):
                        lease.run(command)

        killpg.assert_called_once_with(process.pid, retention.signal.SIGTERM)
        self.assertEqual(process.returncode, -15)
        failure = retention._read_json(
            retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json")
        self.assertEqual(failure["storage_watch_events"][0]["action"], "HARD_STOP_TERMINATE")
        self.assertEqual(failure["storage_watch_events"][0]["free_bytes"], 24 * retention.GIB)

    def test_build_runtime_warning_below_admission_threshold_is_not_an_emergency_stop(self) -> None:
        lease = self._lease("build-runtime-warning")

        class CompletedSyntheticBuild:
            waits = 0

            def wait(self, timeout=None):
                self.waits += 1
                if self.waits == 1:
                    raise subprocess.TimeoutExpired(["synthetic-build"], timeout)
                return 0

        with mock.patch.object(storage_policy, "disk_free_bytes",
                               side_effect=[49 * retention.GIB, 49 * retention.GIB]):
            with mock.patch.object(retention.time, "sleep", return_value=None):
                self.assertEqual(lease._wait_for_build_process(CompletedSyntheticBuild()), 0)

        self.assertEqual(len(lease.storage_watch_events), 2)
        self.assertEqual(lease.storage_watch_events[0]["action"], "WARNING_NO_NEW_EXPERIMENTS")
        self.assertEqual(lease.storage_watch_events[1]["action"], "PROCESS_COMPLETED")

    def test_duplicate_completion_is_idempotent(self) -> None:
        lease = self._lease("duplicate-completion")
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        first = lease.complete()
        second = lease.complete()
        self.assertEqual(first["build_manifest_id"], second["build_manifest_id"])
        self.assertTrue(second["idempotent_replay"])
        self.assertFalse(lease.path.exists())

    def test_manifest_write_replay_before_lease_transition_is_idempotent(self) -> None:
        lease = self._lease("manifest-write-replay")
        lease.reserve()
        self._output(lease)
        self._authorize_synthetic_execution(lease)
        evidence = retention.MANIFEST_ROOT / f"{lease.build_id}.evidence"
        evidence_fd = retention._open_dir(evidence, create=True)
        os.close(evidence_fd)
        execution_evidence = retention._copy_evidence(
            lease.path / retention.BUILD_EXECUTION_RECEIPT, evidence / "execution/build-execution.json")
        artifacts, identity = retention._copy_tree_artifacts(lease.path, lease.build_id, evidence)
        identity["sandboxed_execution_evidence"] = execution_evidence
        identity["sandboxed_execution_status"] = "verified_successful_seatbelt_run"
        identity["sandboxed_execution_executor_mode"] = "HOST_SEATBELT_V1"
        identity["sandboxed_execution_profile_sha256"] = hashlib.sha256(
            retention._sandbox_profile_text(lease.path).encode("utf-8")).hexdigest()
        identity["config_key_sha256"] = retention.configuration_key_from_identity(
            identity, dependency_state=identity["dependency_state"])
        lease_record = retention._load_lease(lease.build_id)
        first, first_digest = retention._build_manifest(
            lease_record, artifacts, identity, evidence,
            provenance_scope="captured during successful build completion")
        second, second_digest = retention._build_manifest(
            lease_record, artifacts, identity, evidence,
            provenance_scope="captured during successful build completion")
        self.assertEqual(second["build_manifest_id"], first["build_manifest_id"])
        self.assertEqual(second_digest, first_digest)
        result = lease.complete()
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")

    def test_duplicate_configuration_reuses_verified_canonical_artifacts(self) -> None:
        source_hash = hashlib.sha256(b"same source tree").hexdigest()
        first = self._lease("canonical-first", source_tree_hash=source_hash)
        first.reserve()
        self._output(first)
        self._authorize_synthetic_execution(first)
        first.complete()
        second = self._lease("canonical-second", source_tree_hash=source_hash)
        result = second.run([os.sys.executable, "-c", "raise SystemExit('must not build')"])
        self.assertEqual(result["status"], "REUSED_CANONICAL")
        self.assertFalse(second.path.exists())

    def test_existing_canonical_artifact_is_not_replaced(self) -> None:
        first = self.repo / "first.dll"
        second = self.repo / "second.dll"
        first.write_bytes(b"canonical bytes")
        second.write_bytes(b"canonical bytes")
        original = retention._artifactize(first)
        reused = retention._artifactize(second)
        self.assertEqual(original["sha256"], reused["sha256"])
        canonical = Path(original["canonical_artifact_path"])
        self.assertEqual(canonical.read_bytes(), b"canonical bytes")
        canonical.chmod(0o600)
        canonical.write_bytes(b"conflicting bytes")
        with self.assertRaises(retention.RetentionError):
            retention._artifactize(first)

    def test_artifact_store_cross_filesystem_group_is_rejected_by_verify_and_copy(self) -> None:
        retention.ARTIFACT_ROOT.mkdir(parents=True)
        payload = b"synthetic mounted CAS payload"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir()
        (group / "payload.dll").write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 2, "sha256": digest,
            "canonical_path": str(group / "payload.dll"), "size_bytes": len(payload),
        }), encoding="utf-8")
        group_info = group.stat()
        real_fstatvfs = retention.os.fstatvfs

        def mounted_group_fsid(fd: int):
            result = real_fstatvfs(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (group_info.st_dev, group_info.st_ino):
                return SimpleNamespace(f_fsid=getattr(result, "f_fsid", 0) + 1)
            return result

        row = {"sha256": digest, "canonical_artifact_path": str(group / "payload.dll")}
        source = self.repo / "new-output.dll"
        source.write_bytes(b"new artifact must not be copied into a mounted group")
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
        source_group = retention.ARTIFACT_ROOT / source_digest
        source_group.mkdir()
        source_group_info = source_group.stat()

        def mounted_either_group_fsid(fd: int):
            result = real_fstatvfs(fd)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) in {
                    (group_info.st_dev, group_info.st_ino),
                    (source_group_info.st_dev, source_group_info.st_ino)}:
                return SimpleNamespace(f_fsid=getattr(result, "f_fsid", 0) + 1)
            return result

        with mock.patch.object(retention.os, "fstatvfs", side_effect=mounted_either_group_fsid):
            self.assertFalse(retention._verify_artifact(row))
            with self.assertRaisesRegex(retention.RetentionError, "crossed its verified store filesystem"):
                retention._artifactize(source)
        self.assertFalse((source_group / "new-output.dll").exists())

    def test_hardlinked_or_fifo_cas_payloads_are_rejected_without_blocking(self) -> None:
        payload = b"synthetic CAS object"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        object_path = group / "object.dll"
        object_path.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 2, "sha256": digest, "canonical_path": str(object_path),
            "size_bytes": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "mutable-hardlink.dll"
        os.link(object_path, alias)
        row = {"sha256": digest, "canonical_artifact_path": str(object_path)}
        self.assertFalse(retention._verify_artifact(row))
        with self.assertRaisesRegex(retention.RetentionError, "non-legacy canonical artifact is hardlinked"):
            retention.legacy_artifact_hardlink_estimate([digest])
        alias.unlink()
        object_path.unlink()
        os.mkfifo(object_path)
        self.assertFalse(retention._verify_artifact(row))

    def test_legacy_hardlinked_cas_object_is_detached_without_changing_identity(self) -> None:
        payload = b"legacy output shared with historical evidence"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "historical-evidence.dll"
        os.link(canonical, alias)
        before_inode = canonical.stat().st_ino
        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate, [{"sha256": digest, "size_bytes": len(payload),
                                     "link_count": 2,
                                     "copy_required": True, "recovery_required": False,
                                     "canonical_artifact_path": str(canonical)}])
        actions = retention.detach_legacy_artifact_hardlinks([digest], reason="synthetic migration test")
        after = canonical.stat()
        self.assertEqual(len(actions), 1)
        self.assertNotEqual(after.st_ino, before_inode)
        self.assertEqual(after.st_nlink, 1)
        self.assertEqual(alias.stat().st_nlink, 1)
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)
        self.assertTrue(retention._verify_artifact({
            "sha256": digest, "canonical_artifact_path": str(canonical)}))
        self.assertEqual(retention.detach_legacy_artifact_hardlinks(
            [digest], reason="idempotency check"), [])

    def test_canonical_artifact_inventory_includes_unreferenced_store_objects(self) -> None:
        payload = b"historical run manifest is the only reference"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "historical.bin"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        self.assertEqual(retention.canonical_artifact_digests(), [digest])
        (retention.ARTIFACT_ROOT / "unrecognized-entry").write_text("must fail closed", encoding="utf-8")
        with self.assertRaisesRegex(retention.RetentionError, "unrecognized entry"):
            retention.canonical_artifact_digests()

    def test_legacy_hardlink_detachment_refuses_mismatched_bytes(self) -> None:
        payload = b"expected canonical bytes"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(b"tampered canonical bytes")
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "preserved-alias.dll"
        os.link(canonical, alias)
        original_inode = canonical.stat().st_ino
        with self.assertRaises(retention.RetentionError):
            retention.detach_legacy_artifact_hardlinks([digest], reason="mismatch test")
        self.assertEqual(canonical.stat().st_ino, original_inode)
        self.assertEqual(alias.read_bytes(), b"tampered canonical bytes")

    def test_legacy_hardlink_detachment_recovers_durable_intent_after_receipt_write_failure(self) -> None:
        payload = b"recover canonical isolation receipt after replacement"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "historical-evidence.dll"
        os.link(canonical, alias)
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        original_write = retention._write_json
        failed = False

        def fail_once(path, record, *, replace=True, sealed=False):
            nonlocal failed
            if (not failed and path == receipt
                    and record.get("result") == "DETACHED_WITH_HASH_PRESERVED"):
                failed = True
                raise OSError("synthetic final receipt write failure")
            return original_write(path, record, replace=replace, sealed=sealed)

        with mock.patch.object(retention, "_write_json", side_effect=fail_once):
            with self.assertRaisesRegex(OSError, "synthetic final receipt write failure"):
                retention.detach_legacy_artifact_hardlinks([digest], reason="receipt recovery test")
        self.assertEqual(canonical.stat().st_nlink, 1)
        self.assertEqual(retention._read_json(receipt, sealed=True)["result"], "ISOLATION_PREPARED")
        recovery_estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(recovery_estimate, [{"sha256": digest, "size_bytes": 0,
                                              "link_count": 1, "copy_required": False,
                                              "recovery_required": True,
                                              "canonical_artifact_path": str(canonical)}])
        recovered = retention.detach_legacy_artifact_hardlinks([digest], reason="receipt recovery test")
        self.assertEqual(recovered[0]["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(retention._read_json(receipt, sealed=True)["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)

    def test_aliases_disappearing_during_prepared_isolation_recovers_by_previous_inode(self) -> None:
        payload = b"prepared detached inode is redundant after aliases disappear"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "vanishing-alias.dll"
        os.link(canonical, alias)
        old = canonical.stat()
        temporary_name = f".{canonical.name}.{('a' * 32)}.detach"
        temporary = group / temporary_name
        temporary.write_bytes(payload)
        isolated = temporary.stat()
        alias.unlink()
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        retention._write_json(receipt, {
            "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
            "sha256": digest, "canonical_artifact_path": str(canonical),
            "size_bytes": len(payload), "previous_inode": old.st_ino,
            "previous_link_count": 2, "isolated_inode": isolated.st_ino,
            "isolated_link_count": 1, "temporary_device": isolated.st_dev,
            "temporary_inode": isolated.st_ino, "temporary_name": temporary_name,
            "result": "ISOLATION_PREPARED",
        }, sealed=True)

        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate, [{"sha256": digest, "size_bytes": 0,
                                     "link_count": 1, "copy_required": False,
                                     "recovery_required": True,
                                     "canonical_artifact_path": str(canonical)}])
        actions = retention.detach_legacy_artifact_hardlinks([digest], reason="alias-free recovery")
        self.assertEqual(actions[0]["result"], "ALIASES_ALREADY_UNLINKED_WITH_HASH_PRESERVED")
        self.assertEqual(canonical.stat().st_ino, old.st_ino)
        self.assertEqual(canonical.stat().st_nlink, 1)
        self.assertFalse(temporary.exists())
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)

    def test_copy_planned_unbound_temp_from_create_progress_crash_is_rebound(self) -> None:
        payload = b"durably recover copy after O_EXCL before COPY_IN_PROGRESS receipt"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "crash-boundary-alias.dll"
        os.link(canonical, alias)
        previous = canonical.stat()
        temporary_name = f".{canonical.name}.{('c' * 32)}.detach"
        temporary = group / temporary_name
        temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(temp_fd, b"partial")
        os.fsync(temp_fd)
        os.close(temp_fd)
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        retention._write_json(receipt, {
            "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
            "sha256": digest, "canonical_artifact_path": str(canonical),
            "size_bytes": len(payload), "previous_device": previous.st_dev,
            "previous_inode": previous.st_ino, "previous_link_count": 2,
            "temporary_name": temporary_name, "result": "COPY_PLANNED",
        }, sealed=True)

        estimates = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertTrue(estimates[0]["copy_required"])
        actions = retention.detach_legacy_artifact_hardlinks(
            [digest], reason="recover crash after exclusive temp creation")
        self.assertEqual(actions[0]["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(canonical.stat().st_nlink, 1)
        self.assertEqual(alias.stat().st_nlink, 1)
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)

    def test_copy_planned_unbound_temp_with_disappeared_alias_preflights_zero_copy(self) -> None:
        payload = b"aliases may disappear after planned temp creation"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "vanishing-copy-planned-alias.dll"
        os.link(canonical, alias)
        previous = canonical.stat()
        temporary_name = f".{canonical.name}.{('d' * 32)}.detach"
        temporary = group / temporary_name
        temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(temp_fd, b"partial")
        os.fsync(temp_fd)
        os.close(temp_fd)
        alias.unlink()
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        retention._write_json(receipt, {
            "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
            "sha256": digest, "canonical_artifact_path": str(canonical),
            "size_bytes": len(payload), "previous_device": previous.st_dev,
            "previous_inode": previous.st_ino, "previous_link_count": 2,
            "temporary_name": temporary_name, "result": "COPY_PLANNED",
        }, sealed=True)

        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate, [{"sha256": digest, "size_bytes": 0,
                                     "link_count": 1, "copy_required": False,
                                     "recovery_required": True,
                                     "canonical_artifact_path": str(canonical)}])
        actions = retention.detach_legacy_artifact_hardlinks(
            [digest], reason="recover alias-free copy-planned temp")
        self.assertEqual(actions[0]["result"], "ALIASES_ALREADY_UNLINKED_WITH_HASH_PRESERVED")
        self.assertFalse(temporary.exists())
        self.assertEqual(canonical.stat().st_nlink, 1)
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)

    def test_complete_in_progress_temp_reduces_preflight_copy_requirement_to_zero(self) -> None:
        payload = b"reuse complete durable copy after interrupted CAS isolation"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "complete-copy-alias.dll"
        os.link(canonical, alias)
        previous = canonical.stat()
        temporary_name = f".{canonical.name}.{('d' * 32)}.detach"
        temporary = group / temporary_name
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, payload)
        os.fsync(fd)
        os.close(fd)
        isolated = temporary.stat()
        receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
        retention._write_json(receipt, {
            "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
            "sha256": digest, "canonical_artifact_path": str(canonical),
            "size_bytes": len(payload), "previous_device": previous.st_dev,
            "previous_inode": previous.st_ino, "previous_link_count": 2,
            "temporary_device": isolated.st_dev, "temporary_inode": isolated.st_ino,
            "temporary_name": temporary_name, "result": "COPY_IN_PROGRESS",
        }, sealed=True)

        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate, [{"sha256": digest, "size_bytes": 0,
                                     "link_count": 2, "copy_required": False,
                                     "recovery_required": True,
                                     "canonical_artifact_path": str(canonical)}])
        actions = retention.detach_legacy_artifact_hardlinks([digest], reason="reuse completed durable copy")
        self.assertEqual(actions[0]["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)
        self.assertEqual(alias.stat().st_nlink, 1)

    def test_post_exchange_alias_mutation_does_not_block_receipted_recovery(self) -> None:
        payload = b"canonical bytes before legacy alias changes"
        mutated_alias = b"external legacy alias mutation remains intact"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "mutable-legacy-alias.dll"
        os.link(canonical, alias)
        real_unlink = retention._unlink_pinned_regular_at
        interrupted = False

        def mutate_then_interrupt(directory_fd: int, name: str, expected: os.stat_result) -> None:
            nonlocal interrupted
            if not interrupted and retention._detach_temp_name_is_safe(canonical.name, name):
                interrupted = True
                alias.write_bytes(mutated_alias)
                raise OSError("synthetic process death after swap and alias mutation")
            real_unlink(directory_fd, name, expected)

        with mock.patch.object(retention, "_unlink_pinned_regular_at", side_effect=mutate_then_interrupt):
            with self.assertRaisesRegex(OSError, "after swap and alias mutation"):
                retention.detach_legacy_artifact_hardlinks([digest], reason="alias mutation recovery")
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)
        self.assertEqual(alias.read_bytes(), mutated_alias)

        estimate = retention.legacy_artifact_hardlink_estimate([digest])
        self.assertEqual(estimate[0]["recovery_required"], True)
        recovered = retention.detach_legacy_artifact_hardlinks([digest], reason="alias mutation recovery")
        self.assertEqual(recovered[0]["result"], "DETACHED_WITH_HASH_PRESERVED")
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)
        self.assertEqual(alias.read_bytes(), mutated_alias)

    def test_failed_atomic_exchange_preserves_known_isolated_temporary_for_recovery(self) -> None:
        payload = b"isolated temp survives a failed exchange verification"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "exchange-alias.dll"
        os.link(canonical, alias)
        real_swap = retention._rename_swap_at
        calls = 0

        def swap_twice(directory_fd: int, left: str, right: str) -> None:
            nonlocal calls
            real_swap(directory_fd, left, right)
            calls += 1
            if calls == 1:
                # Return to the pre-exchange names before the caller's identity
                # check to model a namespace change during the exchange window.
                real_swap(directory_fd, left, right)

        with mock.patch.object(retention, "_rename_swap_at", side_effect=swap_twice):
            with self.assertRaisesRegex(retention.RetentionError, "identity changed during atomic exchange"):
                retention.detach_legacy_artifact_hardlinks([digest], reason="exchange race test")
        receipt = retention._read_json(
            retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json", sealed=True)
        temporary = group / receipt["temporary_name"]
        self.assertTrue(temporary.is_file())
        self.assertEqual((temporary.stat().st_dev, temporary.stat().st_ino),
                         (receipt["temporary_device"], receipt["isolated_inode"]))
        self.assertEqual(hashlib.sha256(temporary.read_bytes()).hexdigest(), digest)
        self.assertEqual(canonical.stat().st_nlink, 2)
        self.assertTrue(alias.exists())

    def test_replaced_receipted_temp_is_refused_and_left_untouched(self) -> None:
        payload = b"expected CAS source"
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.dll"
        canonical.write_bytes(payload)
        (group / "manifest.json").write_text(__import__("json").dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        alias = self.repo / "temp-replacement-alias.dll"
        os.link(canonical, alias)
        old = canonical.stat()
        temporary_name = f".{canonical.name}.{('b' * 32)}.detach"
        temporary = group / temporary_name
        old_temp_fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(old_temp_fd, payload)
            old_temp = os.fstat(old_temp_fd)
            receipt = retention.RECEIPT_ROOT / f"artifact-hardlink-detach-{digest}.json"
            retention._write_json(receipt, {
                "schema_version": 1, "receipt_type": "LEGACY_CAS_HARDLINK_ISOLATION",
                "sha256": digest, "canonical_artifact_path": str(canonical),
                "size_bytes": len(payload), "previous_inode": old.st_ino,
                "previous_link_count": 2, "temporary_device": old_temp.st_dev,
                "temporary_inode": old_temp.st_ino, "temporary_name": temporary_name,
                "result": "COPY_IN_PROGRESS",
            }, sealed=True)
            temporary.unlink()
            temporary.write_bytes(b"foreign replacement")
            foreign = temporary.stat()
            self.assertNotEqual((foreign.st_dev, foreign.st_ino),
                                (old_temp.st_dev, old_temp.st_ino))
            with self.assertRaisesRegex(retention.RetentionError, "temporary lacks a durable inode binding"):
                retention.detach_legacy_artifact_hardlinks([digest], reason="replacement test")
            self.assertEqual(temporary.read_bytes(), b"foreign replacement")
            self.assertEqual(canonical.stat().st_nlink, 2)
        finally:
            os.close(old_temp_fd)

    def test_unmanifested_fifo_cas_payload_is_rejected_without_blocking(self) -> None:
        source = self.repo / "fifo-output.dll"
        source.write_bytes(b"canonical copy candidate")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        os.mkfifo(group / source.name)
        with self.assertRaises(retention.RetentionError):
            retention._artifactize(source)

    def test_fifo_artifact_manifest_is_rejected_without_blocking(self) -> None:
        group = retention.ARTIFACT_ROOT / ("a" * 64)
        group.mkdir(parents=True)
        os.mkfifo(group / "manifest.json")
        group_fd = retention._open_dir(group)
        try:
            with self.assertRaisesRegex(retention.RetentionError, "regular file"):
                retention._read_at(group_fd, "manifest.json")
        finally:
            os.close(group_fd)

    def test_wine_prefix_signature_scan_refuses_a_replaced_root(self) -> None:
        root = self.repo / "replace-during-prefix-classification"
        root.mkdir()
        (root / "system.reg").write_text("wine registry", encoding="utf-8")
        (root / "drive_c").mkdir()
        (root / "dosdevices").mkdir()
        displaced = root.with_name(root.name + ".original")
        open_directory = retention._open_dir

        def open_then_replace(path, *, create=False):
            descriptor = open_directory(path, create=create)
            os.rename(root, displaced)
            root.mkdir()
            (root / "build.ninja").write_text("replacement build", encoding="utf-8")
            return descriptor

        with mock.patch.object(retention, "_open_dir", side_effect=open_then_replace):
            with self.assertRaisesRegex(retention.RetentionError, "replaced while classifying Wine prefix"):
                retention._is_wine_prefix_tree(root)
        self.assertTrue((displaced / "system.reg").is_file())
        self.assertTrue((root / "build.ninja").is_file())

    def test_mount_discovery_rejects_failed_hdiutil_enumeration(self) -> None:
        mount = self.cache / "readonly-mount"
        mount.mkdir()
        failed = mock.Mock(returncode=1, stdout=b"partial mount data")
        with mock.patch.object(retention.os.path, "ismount", return_value=True), \
                mock.patch.object(retention.os, "statvfs",
                                  return_value=SimpleNamespace(f_flag=getattr(os, "ST_RDONLY", 1))), \
                mock.patch.object(retention.subprocess, "run", return_value=failed):
            self.assertFalse(retention._known_readonly_cache_image_mount(mount, self.cache))

    def test_mount_discovery_accepts_only_exact_plist_cache_binding(self) -> None:
        mount = self.cache / "readonly-mount"
        mount.mkdir()
        image = self.cache / "source.dmg"
        image.write_bytes(b"synthetic disk image")
        metadata = {"images": [{
            "image-path": str(image),
            "writeable": False,
            "system-entities": [{"mount-point": str(mount)}],
        }]}
        result = mock.Mock(returncode=0, stdout=plistlib.dumps(metadata))
        with mock.patch.object(retention.os.path, "ismount", return_value=True), \
                mock.patch.object(retention.os, "statvfs",
                                  return_value=SimpleNamespace(f_flag=getattr(os, "ST_RDONLY", 1))), \
                mock.patch.object(retention.subprocess, "run", return_value=result) as run:
            self.assertTrue(retention._known_readonly_cache_image_mount(mount, self.cache))
        self.assertEqual(run.call_args.args[0], ["hdiutil", "info", "-plist"])

    def test_inventory_refuses_external_build_root_symlink_alias(self) -> None:
        target = Path(self.temp.name) / "outside-inventory-build"
        target.mkdir()
        (target / "build.ninja").write_text("synthetic external build", encoding="utf-8")
        alias = self.repo / "out"
        alias.symlink_to(target, target_is_directory=True)
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", self.cache), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", Path(self.temp.name)), \
                mock.patch.object(retention, "_all_root_candidates", return_value=[alias]):
            with self.assertRaisesRegex(RuntimeError, "cannot account for unresolved build-root alias"):
                storage_inventory.discover_roots()

    def test_signed_receipt_from_another_build_does_not_attest_retirement(self) -> None:
        first = self._lease("receipt-binding-a")
        first.reserve()
        self._output(first)
        self._authorize_synthetic_execution(first)
        first.complete()
        second = self._lease("receipt-binding-b", source_tree_hash=hashlib.sha256(b"receipt other source").hexdigest())
        second.reserve()
        self._output(second)
        self._authorize_synthetic_execution(second)
        second.complete()
        lease = retention._load_lease(first.build_id)
        other_receipt = retention.RECEIPT_ROOT / f"{second.build_id}.json"
        lease["cleanup_receipt_path"] = str(other_receipt)
        retention._store_lease(lease)
        snapshot = retention.status_snapshot()
        self.assertEqual(snapshot["gate_status"], "BLOCKED")
        self.assertTrue(any("canonical receipt path" in row["reason"] for row in snapshot["unknown"]))

    def test_unleased_dxvk_cache_root_is_discovered_as_a_cap_blocker(self) -> None:
        root = Path(self.temp.name) / "dxvk-cache"
        root.mkdir()
        (root / "build.ninja").write_text("synthetic", encoding="utf-8")
        self.assertTrue(retention._has_build_signature(root))
        with mock.patch.object(retention, "_all_root_candidates", return_value=[root]):
            blockers = retention.capacity_blockers()
        self.assertIn("ambiguous or unleased build roots require reconciliation", blockers)

    def test_unbranded_ninja_build_root_is_discovered_by_real_scan(self) -> None:
        root = retention.TEMP_ROOT / "ninja-build-unbranded"
        root.mkdir()
        (root / "build.ninja").write_text("# synthetic ninja tree\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())
        state = retention._snapshot()
        self.assertTrue(any(str(root) == row.get("path") for row in state["unknown"]))

    def test_nested_unbranded_temporary_build_root_is_discovered(self) -> None:
        root = retention.TEMP_ROOT / "scratch" / "project" / "out" / "build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# nested synthetic build\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())

    def test_unleased_nested_build_in_step11d_cache_blocks_the_cap(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11D-R/scratch/project/new-build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic cache build root\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())
        state = retention._snapshot()
        self.assertTrue(any(str(root) == row.get("path") for row in state["unknown"]))

    def test_nested_build_in_every_project_cache_generation_blocks_the_cap(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11D/scratch/project/new-build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic non-R cache build root\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())
        state = retention._snapshot()
        self.assertTrue(any(str(root) == row.get("path") for row in state["unknown"]))

    def test_noncanonical_artifacts_directory_is_scanned_for_builds(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11D-R/artifacts/old-copy/build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# not the canonical content-addressed store\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())

    def test_unbranded_cache_symlink_is_reported_without_following(self) -> None:
        cache_root = retention.HOME / "Library/Caches/FGMetalStep11D-R"
        (cache_root / "prefix-cache").mkdir(parents=True)
        target = Path(self.temp.name) / "external-build-root"
        target.mkdir()
        (target / "build.ninja").write_text("# external synthetic build\n", encoding="utf-8")
        alias = cache_root / "prefix-cache" / "out"
        alias.symlink_to(target, target_is_directory=True)
        candidates = retention._all_root_candidates()
        self.assertIn(alias, candidates)
        state = retention._snapshot()
        self.assertTrue(any(row.get("path") == str(alias) for row in state["unknown"]))

    def test_external_temp_symlink_named_out_is_a_blocker(self) -> None:
        target = Path(self.temp.name) / "outside-build-tree"
        target.mkdir()
        (target / "build.ninja").write_text("synthetic", encoding="utf-8")
        alias = retention.TEMP_ROOT / "scratch" / "prefix-cache" / "out"
        alias.parent.mkdir(parents=True)
        alias.symlink_to(target, target_is_directory=True)
        self.assertIn(alias, retention._all_root_candidates())

    def test_external_worktree_symlink_named_out_is_a_blocker(self) -> None:
        target = Path(self.temp.name) / "outside-worktree-build"
        target.mkdir()
        (target / "build.ninja").write_text("synthetic", encoding="utf-8")
        alias = self.repo / "out"
        alias.symlink_to(target, target_is_directory=True)
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(retention.subprocess, "run", return_value=listing):
            self.assertIn(alias, retention._worktree_build_root_candidates())

    def test_queued_cache_mount_replacement_fails_closed(self) -> None:
        mount = retention.HOME / "Library/Caches/FGMetalStep11D-R/mount"
        mount.mkdir(parents=True)
        expected = retention._verified_cache_scan_root(mount)
        old = mount.with_name("mount-old")
        mount.rename(old)
        mount.mkdir()
        with self.assertRaisesRegex(retention.RetentionError, "identity changed after discovery"):
            retention._verified_cache_scan_root(mount, expected)

    def test_lsfg_scoped_cache_build_is_not_omitted_from_inventory(self) -> None:
        root = retention.HOME / "Library/Caches/lsfg-metal/cache-build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic scoped cache build\n", encoding="utf-8")
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", retention.HOME), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", retention.TEMP_ROOT), \
                mock.patch.object(retention.subprocess, "run", return_value=listing):
            roots = storage_inventory.discover_roots()
        self.assertIn((root, "CACHE_BUILD_ROOT"), roots)

    def test_unlinked_mounted_image_length_is_read_from_exact_lsof_vnode(self) -> None:
        path = "/Users/nima/Library/Caches/FGMetalStep11C1R/MetalSharp.dmg"
        result = mock.Mock(returncode=0,
                           stdout=("p51186\ncdiskimages-helper\nftxt\ns77412\n"
                                   "/private/var/db/diagnostics/logd/cache.plist\n"
                                   "f3\ns1436105885\nn" + path + "\n"))
        with mock.patch.object(storage_inventory.subprocess, "run", return_value=result) as run:
            self.assertEqual(storage_inventory._unlinked_image_length(
                {"pid": 51186, "image_path": path}), 1436105885)
        self.assertEqual(run.call_args.args[0],
                         ["lsof", "-nP", "-a", "-p", "51186", "+L1", "-Fpcfsn"])

    def test_unlinked_mounted_image_without_matching_vnode_has_no_estimate(self) -> None:
        result = mock.Mock(returncode=0, stdout="p51186\nfdiskimages-helper\ns9\nnot-the-image\n")
        with mock.patch.object(storage_inventory.subprocess, "run", return_value=result):
            self.assertIsNone(storage_inventory._unlinked_image_length(
                {"pid": 51186, "image_path": "/missing/image.dmg"}))

    def test_non_r_cache_build_is_listed_in_storage_inventory(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11C1R/project/out/build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic legacy cache build root\n", encoding="utf-8")
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", retention.HOME), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", retention.TEMP_ROOT), \
                mock.patch.object(retention.subprocess, "run", return_value=listing):
            roots = storage_inventory.discover_roots()
        self.assertIn((root, "CACHE_BUILD_ROOT"), roots)

    def test_nested_step11d_cache_build_is_listed_in_storage_inventory(self) -> None:
        root = retention.HOME / "Library/Caches/FGMetalStep11D-R/scratch/project/new-build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic cache build root\n", encoding="utf-8")
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", retention.HOME), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", retention.TEMP_ROOT), \
                mock.patch.object(retention.subprocess, "run", return_value=listing):
            roots = storage_inventory.discover_roots()
        self.assertIn((root, "CACHE_BUILD_ROOT"), roots)

    def test_nested_temp_build_root_is_in_storage_inventory_scope(self) -> None:
        root = retention.TEMP_ROOT / "scratch" / "project" / "out" / "build"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# nested synthetic build\n", encoding="utf-8")
        listing = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\n", stderr="")
        with mock.patch.object(storage_inventory, "REPO", self.repo), \
                mock.patch.object(storage_inventory, "HOME", self.cache.parent), \
                mock.patch.object(storage_inventory, "TEMP_ROOT", retention.TEMP_ROOT), \
                mock.patch.object(retention.subprocess, "run", return_value=listing):
            roots = storage_inventory.discover_roots()
        self.assertIn((root, "TEMP_BUILD_ROOT"), roots)

    def test_registered_worktree_symlink_fails_closed(self) -> None:
        registered = self.repo / "registered-worktree"
        registered.symlink_to(self.cache, target_is_directory=True)
        result = mock.Mock(returncode=0, stdout=f"worktree {self.repo}\nworktree {registered}\n", stderr="")
        with mock.patch.object(retention.subprocess, "run", return_value=result):
            with self.assertRaisesRegex(retention.RetentionError, "registered Git worktree path is a symlink"):
                retention._registered_worktree_roots()

    def test_completion_requires_successful_sandboxed_execution_receipt(self) -> None:
        lease = self._lease("completion-requires-run-proof")
        lease.reserve()
        self._output(lease)
        with self.assertRaisesRegex(retention.RetentionError, "successful sandboxed build execution evidence"):
            lease.complete()
        lease._release_lock()
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "ACTIVE")
        lease.fail(command=["synthetic"], return_code=1, error="completion correctly refused")

    def test_gate0_finalize_command_dispatches_final_mode(self) -> None:
        report = self.repo / "prefix-smoke-report.json"
        with mock.patch.object(storage_gate0, "_refresh", return_value={"status": "PASS"}) as refresh, \
                mock.patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(storage_gate0.main(["finalize", "--smoke-report", str(report)]), 0)
        refresh.assert_called_once_with("final", report)

    def test_repository_worktree_build_root_is_discovered(self) -> None:
        root = self.repo / "experiments/dxvk-synthetic/work-output"
        root.mkdir(parents=True)
        (root / "build.ninja").write_text("# synthetic worktree build root\n", encoding="utf-8")
        with mock.patch.object(retention, "_registered_worktree_roots", return_value=[self.repo]):
            self.assertIn(root, retention._worktree_build_root_candidates())

    def test_worktree_scan_fails_closed_on_traversal_error(self) -> None:
        def failing_walk(path, *, followlinks, onerror):
            onerror(PermissionError("synthetic unreadable directory"))
            return iter(())

        with mock.patch.object(retention, "_registered_worktree_roots", return_value=[self.repo]), \
                mock.patch.object(retention.os, "walk", side_effect=failing_walk):
            with self.assertRaisesRegex(retention.RetentionError, "cannot completely scan"):
                retention._worktree_build_root_candidates()

    def test_step10b6_1_name_class_is_not_a_broad_build_scan_exclusion(self) -> None:
        root = retention.TEMP_ROOT / "step10b6_1-anything"
        root.mkdir()
        (root / "build.ninja").write_text("# synthetic hidden root\n", encoding="utf-8")
        self.assertIn(root, retention._all_root_candidates())

    def test_build_tool_rejects_external_makefiles_install_modes_and_wrappers(self) -> None:
        root = self.cache / "builds" / "unsafe-command-check"
        root.mkdir(parents=True)
        source = self.repo / "source"
        source.mkdir()
        with self.assertRaisesRegex(retention.RetentionError, "build tool makefile"):
            retention._validate_build_command_paths(["make", "-C", str(root), "-f", "/private/tmp/Makefile"], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "install targets"):
            retention._validate_build_command_paths(["ninja", "install"], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "unsupported build executable"):
            retention._validate_build_command_paths([os.sys.executable, "-c", "pass"], root, root)

    def test_sandbox_build_command_confines_synthetic_writes(self) -> None:
        root = self.cache / "builds" / "sandbox-confinement"
        root.mkdir(parents=True)
        outside = Path(self.temp.name) / "outside-build-write"
        if os.sys.platform != "darwin" or not Path("/usr/bin/sandbox-exec").is_file():
            with self.assertRaisesRegex(retention.RetentionError, "requires macOS Seatbelt"):
                retention._sandbox_build_command(["/usr/bin/touch", str(root / "inside")], root)
            return
        inside_args, child_env = retention._sandbox_build_command(
            ["/usr/bin/touch", str(root / "inside")], root)
        inside = retention.subprocess.run(inside_args, env=child_env, stdout=retention.subprocess.PIPE,
                                          stderr=retention.subprocess.PIPE, check=False)
        self.assertEqual(inside.returncode, 0, inside.stderr.decode("utf-8", errors="replace"))
        self.assertTrue((root / "inside").is_file())
        outside_args, child_env = retention._sandbox_build_command(
            ["/usr/bin/touch", str(outside)], root)
        outside_result = retention.subprocess.run(outside_args, env=child_env,
                                                  stdout=retention.subprocess.PIPE,
                                                  stderr=retention.subprocess.PIPE, check=False)
        self.assertNotEqual(outside_result.returncode, 0)
        self.assertFalse(outside.exists())

    def test_descriptor_bound_removal_refuses_replacement_and_unlinks_symlink_only(self) -> None:
        parent = self.cache / "removal-root"
        parent.mkdir()
        target = parent / "target"
        target.mkdir()
        (target / "payload").write_text("original", encoding="utf-8")
        outside = self.cache / "outside-sentinel"
        outside.write_text("keep", encoding="utf-8")
        (target / "escape").symlink_to(outside)
        parent_fd = retention._open_dir(parent)
        try:
            original = target.stat()
            expected = (original.st_dev, original.st_ino)
            moved = parent / "original-moved"
            target.rename(moved)
            target.mkdir()
            (target / "payload").write_text("replacement", encoding="utf-8")
            with self.assertRaisesRegex(OSError, "replaced directory"):
                storage_policy._remove_child_directory_nofollow(parent_fd, "target", expected_identity=expected)
            self.assertEqual((target / "payload").read_text(encoding="utf-8"), "replacement")
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
            target.rename(parent / "safe")
            safe_info = (parent / "safe").stat()
            storage_policy._remove_child_directory_nofollow(
                parent_fd, "safe", expected_identity=(safe_info.st_dev, safe_info.st_ino))
            self.assertFalse((parent / "safe").exists())
            self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        finally:
            os.close(parent_fd)

    def test_build_tool_output_directory_must_stay_in_lease(self) -> None:
        root = self.cache / "builds" / "output-argument-check"
        root.mkdir(parents=True)
        source = self.repo / "source"
        source.mkdir()
        retention._validate_build_command_paths(["meson", "setup", ".", str(source)], root, root)
        retention._validate_build_command_paths(["cmake", "-S", str(source), "-B", "."], root, root)
        retention._validate_build_command_paths(["ninja", "-C", str(root)], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "Meson build directory"):
            retention._validate_build_command_paths(["meson", "setup", "../outside", str(source)], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "CMake build directory"):
            retention._validate_build_command_paths(["cmake", "-S", str(source), "-B", "/private/tmp/outside"], root, root)
        with self.assertRaisesRegex(retention.RetentionError, "build tool directory"):
            retention._validate_build_command_paths(["ninja", "-C", "../../outside"], root, root)

    def test_historical_source_evidence_corpus_is_explicit_and_fingerprinted(self) -> None:
        corpus = retention.TEMP_ROOT / "step11d2-diagnostic-corpus"
        (corpus / "source").mkdir(parents=True)
        (corpus / "evidence").mkdir()
        (corpus / "build" / "variant-a").mkdir(parents=True)
        (corpus / "REPORT.md").write_text("historical report\n", encoding="utf-8")
        (corpus / "HASHES.json").write_text("{}\n", encoding="utf-8")
        (corpus / "source/bridge_protocol.h").write_text("protocol\n", encoding="utf-8")
        (corpus / "evidence/run.log").write_text("run evidence\n", encoding="utf-8")
        build_log = corpus / "build/variant-a/build.log"
        build_log.write_text("historical build output\n", encoding="utf-8")
        with mock.patch.object(retention, "LEGACY_STEP11D2_EVIDENCE_CORPUS", corpus), \
                mock.patch.object(retention, "LEGACY_STEP11D2_EVIDENCE_CORPORA", {corpus}), \
                mock.patch.object(retention, "LEGACY_STEP11D2_REPORT_SHA256",
                                  hashlib.sha256((corpus / "REPORT.md").read_bytes()).hexdigest()), \
                mock.patch.object(retention, "LEGACY_STEP11D2_HASHES_SHA256",
                                  hashlib.sha256((corpus / "HASHES.json").read_bytes()).hexdigest()), \
                mock.patch.object(retention, "_legacy_step11d2_evidence_symlink_roots", return_value=[]):
            first = next(row for row in retention._classified_non_retention_tmp_roots()
                         if row["path"] == str(corpus))
            self.assertEqual(first["classification"], "HISTORICAL-SOURCE-AND-EVIDENCE-CORPUS")
            self.assertEqual(first["nested_build_directory_names"], ["variant-a"])
            self.assertNotIn(corpus, retention._all_root_candidates())
            initial_hash = first["nested_build_content_tree_sha256"]
            build_log.write_text("changed historical output\n", encoding="utf-8")
            second = next(row for row in retention._classified_non_retention_tmp_roots()
                          if row["path"] == str(corpus))
        self.assertNotEqual(initial_hash, second["nested_build_content_tree_sha256"])

    def test_generated_spirv_tables_are_classified_as_source_not_build_output(self) -> None:
        dependency = Path(self.temp.name) / "SPIRV-Tools"
        source = dependency / "source"
        tables = dependency / "build"
        source.mkdir(parents=True)
        tables.mkdir()
        (dependency / "CMakeLists.txt").write_text("# dependency source\n", encoding="utf-8")
        for name in retention.SPIRV_GENERATED_TABLE_FILES:
            (tables / name).write_text(f"// {name}\n", encoding="utf-8")
        with mock.patch.object(retention, "SPIRV_GENERATED_TABLE_ROOT", tables):
            self.assertTrue(retention._is_spirv_generated_table_root(tables))
            self.assertFalse(retention._has_build_signature(tables))

    def test_evidence_mutation_refuses_retirement(self) -> None:
        lease = self._lease("evidence-mutation")
        lease.reserve()
        self._output(lease)
        self._complete_without_retirement(lease)
        evidence = retention.MANIFEST_ROOT / f"{lease.build_id}.evidence"
        (evidence / "attacker.txt").write_text("changed", encoding="utf-8")
        lock_fds = retention._lock()
        try:
            with self.assertRaises(retention.RetentionError):
                retention._retire(retention._load_lease(lease.build_id), reason="test")
        finally:
            retention._unlock(*lock_fds)
        self.assertTrue(lease.path.exists())

    def _native_request(self, **overrides) -> retention.NativeMoltenVKBuildRequest:
        developer_dir = self.repo / "Synthetic Xcode/Contents/Developer"
        xcodebuild = developer_dir / "usr/bin/xcodebuild"
        xcodebuild.parent.mkdir(parents=True, exist_ok=True)
        xcodebuild.write_text("synthetic xcodebuild binary\n", encoding="utf-8")
        defaults = {
            "pristine_git_commit": "db66022459ffb663aa2b50f6b018bc2e124f5edf",
            "pristine_git_tree": "efebd918bbe772eead074100c0a18789ad39606d",
            "source_tree_sha256": hashlib.sha256(b"pristine tree").hexdigest(),
            "external_revisions": {"SPIRV-Tools": "abc1234"},
            "vulkan_headers_revision": "e3b1eec",
            "vulkan_headers_tree_sha256": hashlib.sha256(b"vulkan headers").hexdigest(),
            "patch_sha256": hashlib.sha256(b"ring buffer patch").hexdigest(),
            "patch_size_bytes": 12345,
            "modified_files": (("MoltenVK/GPUObjects/MVKImage.mm", "1111", "2222"),),
            "build_system": "xcode_native",
            "project_relative_path": "MoltenVKPackaging.xcodeproj",
            "project_file_sha256": hashlib.sha256(b"project file").hexdigest(),
            "target_or_scheme": "MoltenVK-macOS-dylib",
            "configuration": "Release",
            "architectures": ("x86_64",),
            "deployment_target": "12.0",
            "sdk_name": "macosx",
            "xcode_version_string": "Xcode 16.0",
            "clang_version_string": "Apple Clang 16.0.0",
            "clang_binary_sha256": hashlib.sha256(b"clang binary").hexdigest(),
            "sdk_version_string": "15.0",
            "linker_version_string": "ld64-1115.7.3",
            "toolchain_identity_sha256": retention.native_toolchain_identity_sha256(
                xcode_version_string="Xcode 16.0",
                clang_version_string="Apple Clang 16.0.0",
                clang_binary_sha256=hashlib.sha256(b"clang binary").hexdigest(),
                sdk_version_string="15.0",
                linker_version_string="ld64-1115.7.3",
            ),
            "normalized_environment": {"DEVELOPER_DIR": str(developer_dir)},
            "normalized_arguments": (str(xcodebuild), "-project", "MoltenVKPackaging.xcodeproj",
                                     "-scheme", "MoltenVK-macOS-dylib", "-configuration", "Release",
                                     "-derivedDataPath", ".", "ARCHS=x86_64"),
            "declared_outputs": (
                retention.DeclaredOutputRole(
                    role="moltenvk_dylib",
                    relative_path="Build/Products/Release/libMoltenVK.dylib",
                    artifact_name="libMoltenVK.dylib",
                    mandatory=True,
                    is_macho=True,
                    expected_architectures=("x86_64",),
                ),
            ),
        }
        defaults.update(overrides)
        return retention.NativeMoltenVKBuildRequest(**defaults)

    @staticmethod
    def _macho_dylib_bytes() -> bytes:
        import struct
        return struct.pack("<4I", 0xfeedfacf, 0x01000007, 0, 6) + b"\x00" * 16

    @staticmethod
    def _arm64_macho_dylib_bytes() -> bytes:
        import struct
        return struct.pack("<4I", 0xfeedfacf, 0x0100000c, 0, 6) + b"\x00" * 16

    def _write_execution_receipt(self, lease: retention.BuildLease) -> None:
        self._authorize_synthetic_execution(lease)

    def _native_lease(self, build_id: str, request: retention.NativeMoltenVKBuildRequest) -> retention.BuildLease:
        config_hash = retention.compute_native_moltenvk_config_key(request)
        dep_state = {"status": "captured", "lock": "native moltenvk"}
        metadata = {
            "build_type": request.configuration.casefold(),
            "dependency_state": dep_state,
            "native_moltenvk_request": retention._native_request_to_dict(request),
            "failure_capture_paths": [],
        }
        self._write_owner_manifest(
            source_hash=request.source_tree_sha256,
            config_hash=config_hash,
            build_kind="moltenvk",
            build_type=request.configuration.casefold(),
            dependency_state=dep_state,
        )
        return retention.BuildLease(
            build_id,
            kind="moltenvk",
            owner_experiment="synthetic-test",
            purpose="test native moltenvk build",
            source_tree_hash=request.source_tree_sha256,
            expected_bytes=64 * 1024 ** 2,
            metadata=metadata,
            runner_path=self.runner_file,
            configuration_key_sha256=config_hash,
        )

    def test_native_moltenvk_config_key_invariance_to_generated_metadata(self) -> None:
        req = self._native_request()
        key1 = retention.compute_native_moltenvk_config_key(req)
        # Recomputing key strictly depends on declared inputs
        key2 = retention.compute_native_moltenvk_config_key(req)
        self.assertEqual(key1, key2)

    def _prepared_source_fixture(self) -> tuple[Path, retention.NativeMoltenVKBuildRequest]:
        build_root = Path(self.temp.name) / f"native-source-build-{time.time_ns()}"
        source = build_root / "MoltenVKSource"
        source.mkdir(parents=True)
        git_env = dict(os.environ)
        git_env.update({"GIT_AUTHOR_NAME": "Synthetic Test", "GIT_AUTHOR_EMAIL": "test@example.invalid",
                        "GIT_COMMITTER_NAME": "Synthetic Test", "GIT_COMMITTER_EMAIL": "test@example.invalid"})

        def run_git(directory: Path, *arguments: str) -> str:
            result = subprocess.run(["/usr/bin/git", "-C", str(directory), *arguments], env=git_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if result.returncode != 0:
                raise AssertionError(result.stderr.decode("utf-8", errors="replace"))
            return result.stdout.decode("utf-8").strip()

        run_git(source, "init", "-q")
        (source / ".gitignore").write_text("External/\n", encoding="utf-8")
        (source / "tracked.txt").write_text("pristine source\n", encoding="utf-8")
        project_file = source / "MoltenVK.xcodeproj" / "project.pbxproj"
        project_file.parent.mkdir()
        project_file.write_text("pristine project\n", encoding="utf-8")
        run_git(source, "add", ".gitignore", "tracked.txt", "MoltenVK.xcodeproj/project.pbxproj")
        run_git(source, "commit", "-q", "-m", "pristine")
        commit = run_git(source, "rev-parse", "HEAD")
        tree = run_git(source, "rev-parse", "HEAD^{tree}")

        external_names = ("SPIRV-Cross", "SPIRV-Tools", "Volk", "Vulkan-Headers", "Vulkan-Tools", "cereal")
        external_revisions: dict[str, str] = {}
        for name in external_names:
            dependency = source / "External" / name
            dependency.mkdir(parents=True)
            run_git(dependency, "init", "-q")
            (dependency / "source.txt").write_text(name + "\n", encoding="utf-8")
            run_git(dependency, "add", "source.txt")
            run_git(dependency, "commit", "-q", "-m", "dependency")
            external_revisions[name] = run_git(dependency, "rev-parse", "HEAD")

        pristine = (source / "tracked.txt").read_bytes()
        (source / "tracked.txt").write_text("prepared source\n", encoding="utf-8")
        project_pristine = project_file.read_bytes()
        project_file.write_text("prepared project\n", encoding="utf-8")
        added = source / "added.txt"
        added.write_text("new patch file\n", encoding="utf-8")
        modified_files = (
            ("added.txt", "ABSENT", hashlib.sha256(added.read_bytes()).hexdigest()),
            ("MoltenVK.xcodeproj/project.pbxproj", hashlib.sha256(project_pristine).hexdigest(),
             hashlib.sha256(project_file.read_bytes()).hexdigest()),
            ("tracked.txt", hashlib.sha256(pristine).hexdigest(), hashlib.sha256((source / "tracked.txt").read_bytes()).hexdigest()),
        )
        headers_tree, _ = retention._tree_hash(source / "External" / "Vulkan-Headers")
        request = self._native_request(
            pristine_git_commit=commit,
            pristine_git_tree=tree,
            prepared_source_root_relative_path="MoltenVKSource",
            external_revisions=external_revisions,
            vulkan_headers_revision=external_revisions["Vulkan-Headers"],
            vulkan_headers_tree_sha256=headers_tree,
            project_file_sha256=hashlib.sha256(project_file.read_bytes()).hexdigest(),
            modified_files=modified_files,
            project_relative_path="MoltenVKSource/MoltenVK.xcodeproj",
        )
        return build_root, request

    def test_native_prepared_source_verification_binds_pins_patch_and_clean_status(self) -> None:
        build_root, request = self._prepared_source_fixture()
        result = retention._verify_native_prepared_source(build_root, request)
        self.assertEqual(result["status"], "verified_prepared_source")
        self.assertEqual(result["pristine_git_commit"], request.pristine_git_commit)
        self.assertEqual(result["external_revisions"], dict(request.external_revisions))
        self.assertEqual(len(result["modified_files"]), 3)

    def test_native_prepared_source_verification_rejects_mutation_and_extra_files(self) -> None:
        build_root, request = self._prepared_source_fixture()
        target = build_root / "MoltenVKSource" / "tracked.txt"
        target.write_text("tampered\n", encoding="utf-8")
        with self.assertRaisesRegex(retention.RetentionError, "patched source hash mismatch"):
            retention._verify_native_prepared_source(build_root, request)

        build_root, request = self._prepared_source_fixture()
        (build_root / "MoltenVKSource" / "unexpected.txt").write_text("unexpected\n", encoding="utf-8")
        with self.assertRaisesRegex(retention.RetentionError, "unexpected changes after build"):
            retention._verify_native_prepared_source(build_root, request)

    def test_native_moltenvk_altered_toolchain_changes_key(self) -> None:
        req1 = self._native_request(clang_version_string="Apple Clang 16.0.0")
        changed_clang = "Apple Clang 16.0.1"
        req2 = self._native_request(
            clang_version_string=changed_clang,
            toolchain_identity_sha256=retention.native_toolchain_identity_sha256(
                xcode_version_string="Xcode 16.0",
                clang_version_string=changed_clang,
                clang_binary_sha256=hashlib.sha256(b"clang binary").hexdigest(),
                sdk_version_string="15.0",
                linker_version_string="ld64-1115.7.3",
            ),
        )
        key1 = retention.compute_native_moltenvk_config_key(req1)
        key2 = retention.compute_native_moltenvk_config_key(req2)
        self.assertNotEqual(key1, key2)

    def test_native_moltenvk_altered_patch_hash_fails_reservation(self) -> None:
        req = self._native_request(patch_sha256=hashlib.sha256(b"altered patch").hexdigest())
        lease = self._native_lease("native-altered-patch", req)
        # Manifest has tampered key so manifest passes but native request validation fails
        self._write_owner_manifest(
            source_hash=req.source_tree_sha256,
            config_hash="0" * 64,
            build_kind="moltenvk",
            build_type=req.configuration.casefold(),
            dependency_state=lease.metadata["dependency_state"],
        )
        lease.configuration_key_sha256 = "0" * 64
        with self.assertRaisesRegex(retention.RetentionError, "configuration_key_sha256 does not match"):
            lease.reserve()

    def test_native_moltenvk_altered_source_tree_fails_reservation(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-altered-source", req)
        lease.source_tree_hash = "f" * 64
        with self.assertRaisesRegex(retention.RetentionError, "BuildLease request differs"):
            lease.reserve()

    def test_native_moltenvk_altered_build_args_changes_key(self) -> None:
        req1 = self._native_request(normalized_arguments=("xcodebuild", "-configuration", "Release"))
        req2 = self._native_request(normalized_arguments=("xcodebuild", "-configuration", "Debug"))
        key1 = retention.compute_native_moltenvk_config_key(req1)
        key2 = retention.compute_native_moltenvk_config_key(req2)
        self.assertNotEqual(key1, key2)

    def test_native_moltenvk_execution_must_match_signed_command_environment_and_toolchain(self) -> None:
        request = self._native_request()
        build_root = self.cache / "builds/native-command-binding"
        build_root.mkdir(parents=True)
        observed = {
            "xcode_version_string": request.xcode_version_string,
            "clang_version_string": request.clang_version_string,
            "clang_binary_sha256": request.clang_binary_sha256,
            "sdk_version_string": request.sdk_version_string,
            "linker_version_string": request.linker_version_string,
            "toolchain_identity_sha256": request.toolchain_identity_sha256,
        }
        self.assertEqual(
            retention._validate_native_build_execution_binding(
                request, list(request.normalized_arguments), request.normalized_environment,
                build_root, build_root, observed_toolchain=observed),
            observed)
        with self.assertRaisesRegex(retention.RetentionError, "command differs"):
            retention._validate_native_build_execution_binding(
                request, [*request.normalized_arguments, "-quiet"], request.normalized_environment,
                build_root, build_root, observed_toolchain=observed)
        with self.assertRaisesRegex(retention.RetentionError, "environment differs"):
            retention._validate_native_build_execution_binding(
                request, list(request.normalized_arguments),
                {**request.normalized_environment, "DYLD_INSERT_LIBRARIES": "/tmp/injected.dylib"},
                build_root, build_root, observed_toolchain=observed)
        changed_toolchain = {**observed, "clang_binary_sha256": "0" * 64}
        with self.assertRaisesRegex(retention.RetentionError, "toolchain differs.*clang_binary_sha256"):
            retention._validate_native_build_execution_binding(
                request, list(request.normalized_arguments), request.normalized_environment,
                build_root, build_root, observed_toolchain=changed_toolchain)
        with self.assertRaisesRegex(retention.RetentionError, "cwd must be the leased build root"):
            retention._validate_native_build_execution_binding(
                request, list(request.normalized_arguments), request.normalized_environment,
                self.repo, build_root, observed_toolchain=observed)

    def test_native_moltenvk_architecture_changes_key(self) -> None:
        request_x86 = self._native_request()
        request_arm = self._native_request(
            architectures=("arm64",),
            declared_outputs=(dataclasses.replace(
                request_x86.declared_outputs[0], expected_architectures=("arm64",)),))
        self.assertNotEqual(retention.compute_native_moltenvk_config_key(request_x86),
                            retention.compute_native_moltenvk_config_key(request_arm))

    def test_native_moltenvk_rejects_unknown_or_multiple_architectures(self) -> None:
        for architectures in [("universal",), ("x86_64", "arm64")]:
            with self.subTest(architectures=architectures):
                request = self._native_request(architectures=architectures)
                with self.assertRaisesRegex(retention.RetentionError, "one supported, explicit architecture"):
                    retention.compute_native_moltenvk_config_key(request)

    def test_native_moltenvk_missing_mandatory_output_fails_completion(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-missing-out", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        # Create empty products dir without libMoltenVK.dylib
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        with self.assertRaisesRegex(retention.RetentionError, "mandatory output role 'moltenvk_dylib' missing"):
            self._complete_native(lease)

    def test_native_moltenvk_rejects_output_architecture_mismatch(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-wrong-arch-output", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._arm64_macho_dylib_bytes())
        with self.assertRaisesRegex(retention.RetentionError, "architecture mismatch.*actual=arm64"):
            self._complete_native(lease)

    def test_native_moltenvk_rejects_fat_output(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-fat-output", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(
            b"\xca\xfe\xba\xbe\x00\x00\x00\x01" + b"\x00" * 20)
        with self.assertRaisesRegex(retention.RetentionError, "fat Mach-O output is unsupported"):
            self._complete_native(lease)

    def test_native_moltenvk_unexpected_binary_fails_completion(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-unexpected-bin", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        # Create mandatory output
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        # Create unexpected binary escape
        (products / "libextra.dylib").write_bytes(self._macho_dylib_bytes())
        with self.assertRaisesRegex(retention.RetentionError, "undeclared binary output discovered"):
            self._complete_native(lease)

    def test_native_moltenvk_output_hash_mismatch_refuses_retirement(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-hash-mismatch", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        # Mock _artifactize to corrupt sha
        with mock.patch.object(retention, "_artifactize", side_effect=retention.RetentionError("bit corruption")):
            with self.assertRaises(retention.RetentionError):
                self._complete_native(lease)
        self.assertTrue(lease.path.exists())

    def test_native_moltenvk_cleanup_failure_records_recovery_intent(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-cleanup-fail", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        with mock.patch.object(retention, "_remove_tree_contents", side_effect=OSError("permission denied")):
            with self.assertRaises(retention.RetentionError):
                self._complete_native(lease)
        lease._release_lock()
        current = retention._load_lease(lease.build_id)
        self.assertEqual(current["state"], "RETIRING")
        self.assertTrue((retention.BUILD_ROOT / current["quarantine_name"]).exists())

    def test_native_moltenvk_idempotent_completion_replay(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-idempotent", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        result1 = self._complete_native(lease)
        self.assertEqual(result1["status"], "COMPLETED_AND_AUTO_RETIRED")
        # Second call returns idempotent replay
        result2 = self._complete_native(lease)
        self.assertTrue(result2.get("idempotent_replay"))
        self.assertEqual(result2["build_manifest_id"], result1["build_manifest_id"])

    def test_native_moltenvk_canonical_equivalent_reuse(self) -> None:
        req = self._native_request()
        lease = self._native_lease("native-canonical-first", req)
        lease.reserve()
        self._write_execution_receipt(lease)
        products = lease.path / "Build/Products/Release"
        products.mkdir(parents=True)
        (products / "libMoltenVK.dylib").write_bytes(self._macho_dylib_bytes())
        result = self._complete_native(lease)
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")

        # Second reservation with identical key returns None (reused)
        lease2 = self._native_lease("native-canonical-second", req)
        reserved_path = lease2.reserve()
        self.assertIsNone(reserved_path)
        self.assertIsNotNone(lease2.reused_manifest)

    def test_build_lease_does_not_block_54_gib_project_from_old_45_gib_ceiling(self) -> None:
        lease = self._lease("build-project-54gib")
        with mock.patch.object(storage_policy, "_vm_capacity_commitment_snapshot", return_value={
                    "committed_growth_bytes": 0, "marker_sha256": None, "marker_identity": None,
                }), \
                mock.patch.object(storage_policy, "disk_free_bytes", return_value=60 * retention.GIB), \
                mock.patch.object(storage_policy, "measure_project_usage", return_value={
                    "allocated_inode_deduplicated_bytes": 54 * retention.GIB,
                }), \
                mock.patch.object(retention, "capacity_blockers", return_value=["synthetic post-admission stop"]):
            with self.assertRaisesRegex(retention.RetentionError, "build cap blocks creation"):
                lease.reserve()

    def test_all_four_protected_roots_verify_without_acceptance_or_lease_credit(self) -> None:
        roots = self._protected_evidence_fixture()
        with mock.patch.object(retention, "_all_root_candidates", return_value=list(roots.values())):
            state = retention.status_snapshot()
        self.assertEqual(state["gate_status"], "PASS")
        self.assertEqual(state["unknown"], [])
        self.assertEqual(state["protected_evidence_root_count"], 4)
        self.assertEqual(state["active_build_count"], 0)
        self.assertEqual(state["preserved_build_count"], 0)
        self.assertEqual(len(state["retention_fingerprint"]["protected_in_place_evidence"]), 4)
        ids = {row["evidence_id"] for row in state["protected_in_place_evidence"]}
        self.assertEqual(ids, {
            "R6-A-BUILD-1-EXPLORATORY", "R6-A-BUILD-1-FINAL", "R6-A-BUILD-1-GENERATED",
            retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID,
        })
        self.assertTrue(all(row["accounting_policy"] == "FULLY_CHARGED"
                            for row in state["protected_in_place_evidence"]))
        rejected = next(row for row in state["protected_in_place_evidence"]
                        if row["evidence_id"] == retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID)
        self.assertEqual(rejected["tree_content_sha256"], retention.REJECTED_R5_BUILDROOT_TREE_CONTENT_SHA256)
        self.assertEqual(rejected["critical_file_relative_path"],
                         retention.REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE)
        self.assertEqual(rejected["critical_file_sha256"],
                         retention.REJECTED_R5_BUILDROOT_CRITICAL_FILE_SHA256)
        self.assertFalse(rejected["storage_credit"])
        self.assertFalse(rejected["reservation_credit"])
        self.assertFalse(rejected["free_space_exclusion"])
        self.assertFalse(rejected["project_scope_exclusion"])
        self.assertFalse(rejected["build_lease_exemption"])
        metadata = json.loads((self.repo / retention.REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE).read_text())
        self.assertEqual(metadata["disposition"], "HISTORICAL_REJECTED_EXECUTION_EVIDENCE")
        self.assertIs(metadata["R6_A_ACCEPTANCE"], False)
        self.assertEqual(metadata["execution_authorization"], "NONE")
        self.assertIs(metadata["active_BuildLease"], False)
        self.assertEqual(metadata["R5_repair_admission"], "NOT_CREATED")
        self.assertEqual(metadata["accounting"]["policy"], "FULLY_CHARGED")
        self.assertIs(metadata["allocation_observation"]["identity_binding"], False)

    def test_existing_build1_protected_root_identities_are_unchanged(self) -> None:
        actual = [
            (spec.evidence_id, spec.relative_path, spec.inventory_sha256,
             spec.expected_file_count, spec.expected_logical_bytes)
            for spec in _ORIGINAL_PROTECTED_EVIDENCE_ROOTS[:3]
        ]
        self.assertEqual(actual, [
            (
                "R6-A-BUILD-1-EXPLORATORY",
                "r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/build.x86_64",
                "9148ae52e2f0500baee790c202a4e19936116bc3aa3f593f0e9a9ecd32e32604",
                34, 1_193_851,
            ),
            (
                "R6-A-BUILD-1-FINAL",
                "r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/build.x86_64-final",
                "e38e5611d750e5594b39f0be81f07b5f7c4889c175a72e272411c252f0b78c2e",
                665, 113_970_767,
            ),
            (
                "R6-A-BUILD-1-GENERATED",
                "r6/workspaces/R6-BASELINE-0-BUILD-1-WORKSPACE/evidence/generated/files",
                "ddcd8b6abf146c132481ce4da6118a84dcf761a58a30380b222b42f6023974bd",
                37, 1_155_955,
            ),
        ])

    def test_protected_roots_remain_fully_charged_to_project_usage(self) -> None:
        usage_before = _ORIGINAL_MEASURE_PROJECT_USAGE()
        roots = self._protected_evidence_fixture()
        usage_after = _ORIGINAL_MEASURE_PROJECT_USAGE()
        protected_allocated = sum(path.lstat().st_blocks * 512
                                  for root in roots.values()
                                  for path in root.rglob("*") if path.is_file() and not path.is_symlink())
        self.assertGreater(protected_allocated, 0)
        self.assertGreaterEqual(
            usage_after["allocated_inode_deduplicated_bytes"] -
            usage_before["allocated_inode_deduplicated_bytes"], protected_allocated)
        self.assertGreater(usage_after["logical_bytes"], usage_before["logical_bytes"])

    def test_protected_root_content_mutation_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        (roots["R6-A-BUILD-1-FINAL"] / "build.ninja").write_bytes(b"changed build rules\n")
        with self.assertRaisesRegex(retention.RetentionError, "differ from accepted BUILD-1 index"):
            retention._verify_protected_evidence_roots()

    def test_rejected_r5_critical_executable_mutation_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        target = roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID] / \
            retention.REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE
        target.write_bytes(b"mutated historical executable\n")
        with self.assertRaisesRegex(retention.RetentionError, "membership/content digest changed"):
            retention._verify_protected_evidence_roots()

    def test_rejected_r5_critical_executable_removal_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        target = roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID] / \
            retention.REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE
        target.unlink()
        with self.assertRaisesRegex(retention.RetentionError, "membership/content digest changed"):
            retention._verify_protected_evidence_roots()

    def test_rejected_r5_critical_executable_symlink_substitution_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        target = roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID] / \
            retention.REJECTED_R5_BUILDROOT_CRITICAL_FILE_RELATIVE
        content = target.read_bytes()
        target.unlink()
        external_copy = self.repo / "external-executable-copy.exe"
        external_copy.write_bytes(content)
        target.symlink_to(external_copy)
        with self.assertRaises(retention.RetentionError):
            retention._verify_protected_evidence_roots()

    def test_rejected_r5_root_movement_blocks_at_the_bound_path(self) -> None:
        roots = self._protected_evidence_fixture()
        root = roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID]
        root.rename(root.with_name("build.x86_64-moved"))
        with self.assertRaisesRegex(retention.RetentionError, "cannot safely inventory protected evidence root"):
            retention._verify_protected_evidence_roots()

    def test_rejected_r5_authority_metadata_mutation_blocks(self) -> None:
        self._protected_evidence_fixture()
        authority_path = self.repo / retention.REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE
        authority = json.loads(authority_path.read_text(encoding="utf-8"))
        authority["R6_A_ACCEPTANCE"] = True
        authority_path.write_text(json.dumps(authority), encoding="utf-8")
        with self.assertRaisesRegex(retention.RetentionError, "authority hash changed"):
            retention._verify_protected_evidence_roots()

    def test_protected_root_added_file_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        (roots["R6-A-BUILD-1-GENERATED"] / "unexpected.c").write_bytes(b"unreviewed\n")
        with self.assertRaisesRegex(retention.RetentionError, "differ from accepted BUILD-1 index"):
            retention._verify_protected_evidence_roots()

    def test_protected_root_added_empty_directory_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        (roots["R6-A-BUILD-1-GENERATED"] / "unexpected-empty-directory").mkdir()
        with self.assertRaisesRegex(retention.RetentionError, "membership/content digest changed"):
            retention._verify_protected_evidence_roots()

    def test_same_content_file_replacement_does_not_bind_unaccepted_inode(self) -> None:
        roots = self._protected_evidence_fixture()
        target = roots["R6-A-BUILD-1-EXPLORATORY"] / ".ninja_log"
        replacement = target.with_suffix(".replacement")
        replacement.write_bytes(target.read_bytes())
        replacement.replace(target)
        rows = retention._verify_protected_evidence_roots()
        record = next(row for row in rows if row["evidence_id"] == "R6-A-BUILD-1-EXPLORATORY")
        self.assertEqual(record["root_inventory_sha256"],
                         next(spec.inventory_sha256 for spec in retention.PROTECTED_EVIDENCE_ROOTS
                              if spec.evidence_id == "R6-A-BUILD-1-EXPLORATORY"))
        self.assertNotIn("_file_identities", record)

    def test_protected_root_removed_file_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        (roots["R6-A-BUILD-1-EXPLORATORY"] / ".ninja_log").unlink()
        with self.assertRaisesRegex(retention.RetentionError, "differ from accepted BUILD-1 index"):
            retention._verify_protected_evidence_roots()

    def test_protected_root_renamed_from_bound_path_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        moved = roots["R6-A-BUILD-1-EXPLORATORY"].with_name("build.x86_64-renamed")
        roots["R6-A-BUILD-1-EXPLORATORY"].rename(moved)
        with self.assertRaises(retention.RetentionError):
            retention._verify_protected_evidence_roots()

    def test_protected_root_symlink_substitution_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        original = roots["R6-A-BUILD-1-FINAL"]
        moved = original.with_name("build.x86_64-final-real")
        original.rename(moved)
        original.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(retention.RetentionError):
            retention._verify_protected_evidence_roots()

    def test_copied_protection_metadata_at_another_path_does_not_enroll_root(self) -> None:
        roots = self._protected_evidence_fixture()
        duplicate = self.repo / "copied-protected-root"
        shutil.copytree(roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID], duplicate)
        shutil.copyfile(
            self.repo / retention.REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE,
            duplicate / retention.REJECTED_R5_BUILDROOT_AUTHORITY_RELATIVE.name)
        with mock.patch.object(retention, "_all_root_candidates", return_value=[duplicate]):
            state = retention._snapshot()
        self.assertTrue(any(row["path"] == str(duplicate)
                            and row["reason"].startswith("unleased small build root")
                            for row in state["unknown"]))
        self.assertNotIn(str(duplicate), {row["path"] for row in state["protected_in_place_evidence"]})

    def test_protection_roster_cannot_expand_to_arbitrary_experiment_output(self) -> None:
        self._protected_evidence_fixture()
        extra = retention.ProtectedEvidenceRoot(
            evidence_id="ARBITRARY-NEW-OUTPUT", relative_path="new-experiment/output",
            inventory_sha256="0" * 64, expected_file_count=1, expected_logical_bytes=1)
        with mock.patch.object(retention, "PROTECTED_EVIDENCE_ROOTS",
                               (*retention.PROTECTED_EVIDENCE_ROOTS, extra)):
            with self.assertRaisesRegex(retention.RetentionError, "roster differs from the exact four-root"):
                retention._verify_protected_evidence_roots()

    def test_unlisted_sibling_build_root_remains_ambiguous_and_unleased(self) -> None:
        roots = self._protected_evidence_fixture()
        protected = roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID]
        sibling = protected.with_name("build.x86_64-sibling")
        sibling.mkdir()
        (sibling / "build.ninja").write_text("unleased sibling\n", encoding="utf-8")
        with mock.patch.object(retention, "_all_root_candidates", return_value=[protected, sibling]):
            state = retention._snapshot()
        self.assertTrue(any(row["path"] == str(sibling)
                            and row["reason"].startswith("unleased small build root")
                            for row in state["unknown"]))
        self.assertIn("ambiguous or unleased build roots require reconciliation",
                      retention.capacity_blockers(state))

    def test_rejected_r5_root_does_not_supply_build_admission_or_create_a_buildlease(self) -> None:
        roots = self._protected_evidence_fixture()
        lease = self._lease("rejected-r5-root-is-not-admission")
        with mock.patch.object(storage_policy, "_verify_manifest_signature", return_value=False), \
                mock.patch.object(retention, "_all_root_candidates", return_value=list(roots.values())):
            with self.assertRaisesRegex(retention.RetentionError, "owner manifest is unsigned"):
                lease.reserve()
        self.assertFalse(lease.path.exists())
        self.assertEqual(retention._lease_files(), [])
        state = retention._snapshot()
        self.assertEqual(state["active_build_count"], 0)
        self.assertEqual(state["preserved_build_count"], 0)

    def test_gate_preflight_verifies_protected_roots_before_prefix_reconciliation(self) -> None:
        self._protected_evidence_fixture()
        events: list[str] = []
        original_verify = retention._verify_protected_evidence_roots

        def verify(**kwargs: object) -> list[dict[str, object]]:
            events.append("protected roots")
            return original_verify(**kwargs)

        def stop_before_reconciliation() -> list[dict[str, object]]:
            events.append("prefix reconciliation")
            raise RuntimeError("sentinel stop before mutation")

        with mock.patch.object(retention, "_verify_protected_evidence_roots", side_effect=verify), \
                mock.patch.object(storage_policy, "reconcile_abandoned_storage_smoke_lease_markers",
                                  side_effect=stop_before_reconciliation):
            with self.assertRaisesRegex(RuntimeError, "sentinel stop"):
                storage_gate0._refresh_locked("preflight", None, "synthetic-test-hash", None)
        self.assertEqual(events, ["protected roots", "prefix reconciliation"])

    def test_gate_refresh_verifies_roots_before_review_or_test_work(self) -> None:
        self._protected_evidence_fixture()
        events: list[str] = []
        original_verify = retention._verify_protected_evidence_roots

        def verify(**kwargs: object) -> list[dict[str, object]]:
            events.append("protected roots")
            return original_verify(**kwargs)

        def stop_before_tests() -> None:
            events.append("review and suite")
            raise RuntimeError("sentinel stop before tests")

        with mock.patch.object(retention, "_verify_protected_evidence_roots", side_effect=verify), \
                mock.patch.object(storage_gate0, "_verify_g0c_completion_evidence", side_effect=stop_before_tests):
            with self.assertRaisesRegex(RuntimeError, "sentinel stop"):
                storage_gate0._refresh("preflight")
        self.assertEqual(events, ["protected roots", "review and suite"])

    def test_active_lease_overlapping_protected_root_blocks(self) -> None:
        roots = self._protected_evidence_fixture()
        protected = roots["R6-A-BUILD-1-FINAL"]
        for index, overlapping_path in enumerate((protected, protected.parent, protected / "nested-lease")):
            with self.subTest(overlap=str(overlapping_path)):
                lease_path = retention.LEASE_ROOT / f"overlapping-{index}.json"
                overlapping = {"build_id": f"overlapping-{index}", "path": str(overlapping_path),
                               "kind": "dxvk", "state": "ACTIVE", "owner_experiment": "test",
                               "estimated_bytes": 1, "build_device": 0, "build_inode": 0,
                               "build_filesystem_id": 0, "build_manifest_id": None,
                               "build_manifest_sha256": None, "requested_config_key_sha256": None,
                               "preserve_debug_expires_at_unix": None}
                with mock.patch.object(retention, "_lease_files", return_value=[lease_path]), \
                        mock.patch.object(retention, "_load_lease", return_value=overlapping), \
                        mock.patch.object(retention, "_all_root_candidates", return_value=[]):
                    state = retention._snapshot()
                self.assertTrue(any("overlaps protected parent evidence" in row["reason"]
                                    for row in state["unknown"]))
                self.assertIn("ambiguous or unleased build roots require reconciliation",
                              retention.capacity_blockers(state))

    def test_cleanup_refuses_protected_root_before_removing_any_entry(self) -> None:
        roots = self._protected_evidence_fixture()
        root = roots["R6-A-BUILD-1-GENERATED"]
        before = (root / "generated.c").read_bytes()
        parent_fd = retention._open_dir(root.parent)
        try:
            with self.assertRaisesRegex(retention.RetentionError, "cleanup overlaps protected"):
                retention._remove_verified_tree_at(
                    parent_fd, root.name, expected_device=0, expected_inode=0,
                    logical_path=root, expected_filesystem_id=None)
        finally:
            os.close(parent_fd)
        self.assertTrue(root.is_dir())
        self.assertEqual((root / "generated.c").read_bytes(), before)

    def test_legacy_adoption_refuses_protected_root_even_if_allowlisted(self) -> None:
        roots = self._protected_evidence_fixture()
        root = roots["R6-A-BUILD-1-EXPLORATORY"]
        with mock.patch.object(retention, "LEGACY_BUILD_ROOTS", (root,)):
            with self.assertRaisesRegex(retention.RetentionError, "legacy adoption overlaps protected"):
                retention.adopt_legacy(root, kind="reference", owner_experiment="test", purpose="test")
        self.assertTrue(root.is_dir())
        self.assertTrue((root / ".ninja_log").is_file())

    def test_cas_isolation_does_not_replace_file_inside_protected_root(self) -> None:
        roots = self._protected_evidence_fixture()
        protected_file = roots["R6-A-BUILD-1-EXPLORATORY"] / ".ninja_log"
        payload = protected_file.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        group = retention.ARTIFACT_ROOT / digest
        group.mkdir(parents=True)
        canonical = group / "legacy.log"
        os.link(protected_file, canonical)
        (group / "manifest.json").write_text(json.dumps({
            "schema_version": 1, "sha256": digest, "canonical_path": str(canonical),
            "byte_size": len(payload),
        }), encoding="utf-8")
        protected_before = protected_file.stat()
        actions = retention.detach_legacy_artifact_hardlinks([digest], reason="protected-root regression")
        protected_after = protected_file.stat()
        self.assertEqual(len(actions), 1)
        self.assertEqual((protected_after.st_dev, protected_after.st_ino),
                         (protected_before.st_dev, protected_before.st_ino))
        self.assertEqual(protected_after.st_nlink, 1)
        self.assertEqual(protected_file.read_bytes(), payload)
        self.assertEqual(hashlib.sha256(canonical.read_bytes()).hexdigest(), digest)

    def test_unrelated_growth_still_blocks_under_normal_project_scope_policy(self) -> None:
        self._protected_evidence_fixture()
        before = _ORIGINAL_MEASURE_PROJECT_USAGE()["allocated_inode_deduplicated_bytes"]
        (self.repo / "unrelated-growth.bin").write_bytes(b"u" * (1024 * 1024))
        after = _ORIGINAL_MEASURE_PROJECT_USAGE()["allocated_inode_deduplicated_bytes"]
        self.assertGreater(after, before)
        with mock.patch.object(storage_policy, "PROJECT_BUDGET", before + 512):
            admission = _projected_without_vm_commitment(
                current_free_bytes=100 * storage_policy.GIB,
                current_scoped_bytes=after,
                peak_additional_bytes=0)
        self.assertFalse(admission["admitted"])
        self.assertTrue(any("scoped FG-Metal usage" in blocker for blocker in admission["blockers"]))

    def test_project_scope_ceiling_applies_to_protected_bytes(self) -> None:
        self._protected_evidence_fixture()
        scoped = _ORIGINAL_MEASURE_PROJECT_USAGE()["allocated_inode_deduplicated_bytes"]
        with mock.patch.object(storage_policy, "PROJECT_BUDGET", scoped - 1):
            admission = _projected_without_vm_commitment(
                current_free_bytes=100 * storage_policy.GIB,
                current_scoped_bytes=scoped,
                peak_additional_bytes=0)
        self.assertFalse(admission["admitted"])
        self.assertTrue(any("scoped FG-Metal usage" in blocker for blocker in admission["blockers"]))

    def test_free_space_minimum_and_emergency_floor_still_apply(self) -> None:
        self._protected_evidence_fixture()
        scoped = _ORIGINAL_MEASURE_PROJECT_USAGE()["allocated_inode_deduplicated_bytes"]
        below_minimum = _projected_without_vm_commitment(
            current_free_bytes=storage_policy.FREE_MINIMUM - 1,
            current_scoped_bytes=scoped,
            peak_additional_bytes=0)
        self.assertFalse(below_minimum["admitted"])
        self.assertTrue(any("projected free disk" in blocker for blocker in below_minimum["blockers"]))
        below_emergency = _projected_without_vm_commitment(
            current_free_bytes=storage_policy.FREE_HARD_STOP - 1,
            current_scoped_bytes=scoped,
            peak_additional_bytes=0)
        self.assertFalse(below_emergency["admitted"])
        self.assertTrue(any("emergency hard stop" in blocker for blocker in below_emergency["blockers"]))

    def test_stale_parent_receipt_binding_blocks(self) -> None:
        self._protected_evidence_fixture()
        receipt_path = self.repo / retention.BUILD1_PARENT_RECEIPT_RELATIVE
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["build_id"] = "stale-parent-build"
        raw = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode()
        receipt_path.write_bytes(raw)
        with mock.patch.object(retention, "BUILD1_PARENT_RECEIPT_SHA256", hashlib.sha256(raw).hexdigest()):
            with self.assertRaisesRegex(retention.RetentionError, "parent receipt identity"):
                retention._verify_protected_evidence_roots()

    def test_invalid_review_supplement_binding_blocks(self) -> None:
        self._protected_evidence_fixture()
        supplement_path = self.repo / retention.BUILD1_REVIEW_SUPPLEMENT_RELATIVE
        supplement = json.loads(supplement_path.read_text(encoding="utf-8"))
        supplement["prior_build_1_index"]["sha256"] = "0" * 64
        raw = (json.dumps(supplement, sort_keys=True, indent=2) + "\n").encode()
        supplement_path.write_bytes(raw)
        with mock.patch.object(retention, "BUILD1_REVIEW_SUPPLEMENT_SHA256", hashlib.sha256(raw).hexdigest()):
            with self.assertRaisesRegex(retention.RetentionError, "review supplement does not bind"):
                retention._verify_protected_evidence_roots()

    def test_disappeared_protected_root_blocks_even_when_not_discovered(self) -> None:
        roots = self._protected_evidence_fixture()
        shutil.rmtree(roots["R6-A-BUILD-1-GENERATED"])
        with mock.patch.object(retention, "_all_root_candidates", return_value=[]):
            with self.assertRaises(retention.RetentionError):
                retention._snapshot()

    def test_allocation_block_drift_is_not_part_of_protected_content_identity(self) -> None:
        roots = self._protected_evidence_fixture()
        root = roots["R6-A-BUILD-1-FINAL"]
        expected_rows, _ids, _logical = retention._protected_evidence_root_inventory(root)
        expected_hash = hashlib.sha256(retention._canonical_json(expected_rows)).hexdigest()
        rejected_root = roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID]
        rejected_rows, _rejected_ids, _rejected_logical = retention._protected_evidence_root_inventory(rejected_root)
        rejected_inventory_hash = hashlib.sha256(retention._canonical_json(rejected_rows)).hexdigest()
        rejected_content_hash = retention._protected_evidence_tree_content_sha256(rejected_rows)
        original_stat = os.stat

        class StatWithDifferentBlocks:
            def __init__(self, wrapped: os.stat_result) -> None:
                self.wrapped = wrapped

            def __getattr__(self, name: str) -> object:
                if name == "st_blocks":
                    return self.wrapped.st_blocks + 1_000_000
                return getattr(self.wrapped, name)

        def changed_allocation_only(path: object, *args: object, **kwargs: object) -> StatWithDifferentBlocks:
            return StatWithDifferentBlocks(original_stat(path, *args, **kwargs))

        with mock.patch.object(retention.os, "stat", side_effect=changed_allocation_only):
            changed_rows, _changed_ids, _changed_logical = retention._protected_evidence_root_inventory(root)
            changed_rejected_rows, _changed_rejected_ids, _changed_rejected_logical = (
                retention._protected_evidence_root_inventory(rejected_root))
        changed_hash = hashlib.sha256(retention._canonical_json(changed_rows)).hexdigest()
        changed_rejected_inventory_hash = hashlib.sha256(
            retention._canonical_json(changed_rejected_rows)).hexdigest()
        self.assertEqual(changed_rows, expected_rows)
        self.assertEqual(changed_hash, expected_hash)
        self.assertEqual(changed_rejected_rows, rejected_rows)
        self.assertEqual(changed_rejected_inventory_hash, rejected_inventory_hash)
        self.assertEqual(
            retention._protected_evidence_tree_content_sha256(changed_rejected_rows),
            rejected_content_hash,
        )

    def _finish_session_steps(self, session: retention.BuildLeaseSession) -> None:
        for kind in ("configure", "build", "install", "test"):
            self._run_session_step(session, kind)
        result = session.verify_source()
        self.assertEqual(result["lease_state"], "ACTIVE")

    def test_session_source_symlinks_fail_before_reservation_or_process_launch(self) -> None:
        for suffix in ("external", "dangling", "chain"):
            with self.subTest(kind=suffix):
                build_id = f"session-source-symlink-{suffix}"
                lease = self._session_lease(build_id)
                source = Path(lease.session_plan["source_identity"]["root"])
                if suffix == "external":
                    external = self.repo / f"{build_id}-external.txt"
                    external.write_text("outside source root\n", encoding="utf-8")
                    (source / "source-link").symlink_to(external)
                elif suffix == "dangling":
                    (source / "source-link").symlink_to(source / "missing-target")
                else:
                    external = self.repo / f"{build_id}-external.txt"
                    external.write_text("outside source root\n", encoding="utf-8")
                    (source / "source-link").symlink_to("second-link")
                    (source / "second-link").symlink_to(external)
                with self._patch_session_popen() as popen:
                    with self.assertRaisesRegex(retention.RetentionError, "source tree contains a symlink"):
                        lease.reserve()
                    popen.assert_not_called()
                self.assertFalse(lease.path.exists())
                self.assertFalse(lease.started)

    def test_session_source_hardlink_outside_tree_fails_before_reservation(self) -> None:
        lease = self._session_lease("session-source-hardlink")
        source = Path(lease.session_plan["source_identity"]["root"])
        external = self.repo / "source-file-hardlink-outside"
        os.link(source / "meson.build", external)
        with self._patch_session_popen() as popen:
            with self.assertRaisesRegex(retention.RetentionError, "hardlinks outside the admitted tree"):
                lease.reserve()
            popen.assert_not_called()
        self.assertFalse(lease.path.exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_session_output_hardlink_outside_tree_fails_accounting(self) -> None:
        lease, session = self._start_session("session-output-hardlink")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        output = session.workspace / "output-hardlink-target.bin"
        external = lease.path / "output-hardlink-outside.bin"
        output.write_bytes(b"hardlink source\n")
        os.link(output, external)
        with self.assertRaisesRegex(retention.RetentionError, "hardlinks outside its tree"):
            retention._session_measure_tree(
                lease.path,
                workspace_mount_identity=retention._load_lease(lease.build_id)["session_v3"]
                    ["workspace_mount_identity"])

    @unittest.skipUnless(
        retention.sys.platform == "darwin"
        and Path("/usr/bin/hdiutil").is_file()
        and Path("/usr/bin/sandbox-exec").is_file(),
        "fixed-capacity workspace integration requires Darwin hdiutil and Seatbelt")
    def test_small_fixed_capacity_workspace_returns_enospc(self) -> None:
        root = self.repo / "small-fixed-image-root"
        root.mkdir(mode=0o700)
        identity: dict[str, object] | None = None
        try:
            identity = _ORIGINAL_ATTACH_SESSION_WORKSPACE(root, 32 * 1024 * 1024)
            self.assertEqual(identity["requested_capacity_bytes"], 32 * 1024 * 1024)
            self.assertGreater(identity["capacity_bytes"], 0)
            self.assertLessEqual(identity["capacity_bytes"], 32 * 1024 * 1024)
            workspace = Path(identity["mountpoint"])
            payload_path = workspace / "fill-until-enospc.bin"
            payload = b"X" * (1024 * 1024)
            fd = os.open(payload_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            observed_error: OSError | None = None
            try:
                while True:
                    view = memoryview(payload)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError(errno.EIO, "short write without progress")
                        view = view[written:]
                    os.fsync(fd)
            except OSError as error:
                observed_error = error
            finally:
                os.close(fd)
            self.assertIsNotNone(observed_error, "the fixed-capacity image accepted writes beyond its capacity")
            self.assertEqual(observed_error.errno, errno.ENOSPC)
            payload_path.unlink()
        finally:
            if identity is not None and identity.get("attached") is True:
                _ORIGINAL_DETACH_SESSION_WORKSPACE(root, identity)
            shutil.rmtree(root, ignore_errors=True)

    def test_session_process_containment_keeps_pid_evidence_bounded(self) -> None:
        session_id = 90_001
        members = set(range(10_000, 10_000 + retention.BUILD_SESSION_PROCESS_EVIDENCE_SAMPLE_PIDS + 40))
        members.add(session_id)
        snapshots = [members, members, members, members, members, {session_id}, {session_id}]
        with mock.patch.object(retention, "_contain_session_processes",
                               _ORIGINAL_CONTAIN_SESSION_PROCESSES), \
                mock.patch.object(retention, "_session_sid_members", side_effect=snapshots), \
                mock.patch.object(retention, "_session_process_state", return_value="T"), \
                mock.patch.object(retention, "_session_signal_pid", return_value=True), \
                mock.patch.object(retention, "_session_leader_exited", return_value=True):
            result = retention._contain_session_processes(session_id, timeout=1.0)
        self.assertLessEqual(len(result["members_seen"]),
                             retention.BUILD_SESSION_PROCESS_EVIDENCE_SAMPLE_PIDS)
        self.assertIn(session_id, result["members_seen"])
        self.assertTrue(result["members_seen_sample_truncated"])
        self.assertGreaterEqual(result["members_seen_observation_count"], len(members))
        self.assertEqual(result["members_seen_snapshot_count"], len(snapshots))
        self.assertRegex(result["members_seen_sha256"], r"^[0-9a-f]{64}$")

    @unittest.skipUnless(
        retention.sys.platform == "darwin"
        and Path("/usr/bin/hdiutil").is_file()
        and Path("/usr/sbin/diskutil").is_file()
        and Path("/usr/bin/sandbox-exec").is_file()
        and Path("/usr/bin/cc").is_file()
        and Path("/usr/bin/c++").is_file()
        and Path("/opt/homebrew/Cellar/meson/1.12.1/bin/meson").is_file()
        and Path("/opt/homebrew/Cellar/ninja/1.13.2/bin/ninja").is_file(),
        "production-structure proof requires the pinned Darwin host toolchain",
    )
    def test_host_buildlease_v3_real_meson_ninja_setsid_production_structure(self) -> None:
        """Run a real non-R6 build/test tree through the v3 controller and detach boundary."""
        build_id = "host-v3-production-structure"
        lease = self._session_lease(build_id)
        source = Path(lease.session_plan["source_identity"]["root"])
        workspace = Path(lease.session_plan["build_workspace"])
        image = lease.path / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        library_source = source / "library.c"
        test_source = source / "tests/present-accounting.c"
        (source / "tests").mkdir()
        (source / "meson.build").write_text(
            "project('host-v3-synthetic', 'c', version: '1.0.0')\n"
            "shared_library('synthetic', 'library.c', install: true)\n"
            "subdir('tests')\n",
            encoding="utf-8",
        )
        library_source.write_text(
            "int fgmetal_synthetic_accounting_value(void) { return 42; }\n",
            encoding="utf-8",
        )
        (source / "tests/meson.build").write_text(
            "present_accounting = executable('present-accounting', 'present-accounting.c', "
            "name_suffix: 'exe')\n"
            "test('present-accounting', present_accounting)\n",
            encoding="utf-8",
        )

        escaped_writer = source / "tests/escaped_writer.py"
        escaped_writer.write_text(
            "import os, signal, sys, time\n"
            "oracle_fd = int(sys.argv[1])\n"
            "workspace, image, source_file, evidence, cache = sys.argv[2:7]\n"
            "ready = os.path.join(workspace, '.tmp', 'escaped-present-accounting')\n"
            "def attempt(path, flags):\n"
            "    try:\n"
            "        fd = os.open(path, flags, 0o600)\n"
            "    except OSError as error:\n"
            "        return error.errno\n"
            "    else:\n"
            "        os.close(fd)\n"
            "        return 0\n"
            "def report(line):\n"
            "    os.write(oracle_fd, line.encode('ascii'))\n"
            "def after_detach(_signum, _frame):\n"
            "    paths = [os.path.join(workspace, 'after-detach-write'), image, source_file, evidence, cache]\n"
            "    flags = [os.O_WRONLY | os.O_CREAT | os.O_EXCL, os.O_WRONLY, os.O_WRONLY, os.O_WRONLY, os.O_WRONLY | os.O_CREAT | os.O_EXCL]\n"
            "    values = [attempt(path, flag) for path, flag in zip(paths, flags)]\n"
            "    report('RW:' + ','.join(map(str, values)) + ';PID:' + str(os.getpid()) + '\\n')\n"
            "def after_readonly(_signum, _frame):\n"
            "    path = os.path.join(workspace, 'readonly-write')\n"
            "    value = attempt(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)\n"
            "    report('RO:' + str(value) + ';PID:' + str(os.getpid()) + '\\n')\n"
            "signal.signal(signal.SIGUSR1, after_detach)\n"
            "signal.signal(signal.SIGUSR2, after_readonly)\n"
            "with open(ready, 'x', encoding='ascii') as output:\n"
            "    output.write(str(os.getpid()))\n"
            "while True:\n"
            "    signal.pause()\n",
            encoding="utf-8",
        )

        oracle_read, oracle_write = os.pipe()
        os.set_inheritable(oracle_write, True)
        self.addCleanup(lambda fd=oracle_read: os.close(fd) if fd >= 0 else None)
        child_pids: list[int] = []
        controller_processes: list[subprocess.Popen] = []
        original_popen = subprocess.Popen

        def close_child_pids() -> None:
            for pid in child_pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        self.addCleanup(close_child_pids)
        self.addCleanup(lease._stop_heartbeat)
        self.addCleanup(lease._release_lock)

        test_program = r"""
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/types.h>
#include <unistd.h>

int main(int argc, char **argv) {
  if (argc != 9) return 80;
  int oracle_fd = atoi(argv[1]);
  const char *workspace = argv[2];
  const char *python = argv[7], *helper = argv[8];
  char ready[4096];
  snprintf(ready, sizeof(ready), "%s/.tmp/escaped-present-accounting", workspace);
  pid_t child = fork();
  if (child < 0) return 81;
  if (child > 0) {
    for (int i = 0; i < 500 && access(ready, F_OK) != 0; ++i) usleep(10000);
    return access(ready, F_OK) == 0 ? 0 : 82;
  }

  if (setsid() < 0 || chdir("/") != 0) _exit(83);
  for (int fd = 0; fd < 256; ++fd) {
    if (fd == oracle_fd) continue;
    close(fd);
  }
  execl(python, python, helper, argv[1], argv[2], argv[3], argv[4], argv[5], argv[6],
        (char *)NULL);
  _exit(84);
}
"""
        test_source.write_text(test_program, encoding="utf-8")
        git = Path("/usr/bin/git")
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Synthetic",
            "GIT_AUTHOR_EMAIL": "synthetic@example.invalid",
            "GIT_COMMITTER_NAME": "Synthetic",
            "GIT_COMMITTER_EMAIL": "synthetic@example.invalid",
        }
        subprocess.run([str(git), "-C", str(source), "add", "-A"], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        subprocess.run([str(git), "-C", str(source), "commit", "--quiet", "-m", "production structure"],
                       check=True, env=git_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        source_hash, _source_count = retention._tree_hash(
            source, reject_symlinks=True, reject_external_hardlinks=True)
        revision = subprocess.check_output(
            [str(git), "-C", str(source), "rev-parse", "--verify", "HEAD"], text=True).strip()

        meson = Path("/opt/homebrew/Cellar/meson/1.12.1/bin/meson")
        ninja = Path("/opt/homebrew/Cellar/ninja/1.13.2/bin/ninja")
        cc = Path("/usr/bin/cc")
        cxx = Path("/usr/bin/c++")
        sandbox = Path("/usr/bin/sandbox-exec")
        git = Path("/usr/bin/git")
        path_value = os.pathsep.join((str(meson.parent), str(ninja.parent), "/usr/bin", "/usr/sbin"))
        tools = {name: {"path": str(path), "sha256": retention._hash_path(path)}
                 for name, path in (("meson", meson), ("ninja", ninja), ("cc", cc),
                                    ("cxx", cxx), ("git", git), ("sandbox", sandbox))}
        environment = {"PATH": path_value, "HOME": str(workspace / ".home"), "LANG": "C",
                       "LC_ALL": "C", "CC": str(cc), "CXX": str(cxx)}
        setup_argv = [str(meson), "setup", str(workspace), str(source), "--prefix=/usr",
                      "--backend=ninja", "--wrap-mode=nodownload", "-Dbuildtype=release"]
        prototype = self.cache / "host-v3-prototype-build"
        prototype_evidence = self.cache / "host-v3-prototype-evidence"
        prototype.mkdir()
        (prototype / ".tmp").mkdir()
        (prototype / ".home").mkdir()
        prototype_command, prototype_environment, _prototype_launcher = retention._sandbox_session_command(
            [str(meson), "setup", str(prototype), str(source), "--prefix=/usr", "--backend=ninja",
             "--wrap-mode=nodownload", "-Dbuildtype=release"], prototype, environment)
        setup = subprocess.run(prototype_command, env=prototype_environment, cwd=prototype,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, check=False, timeout=90)
        self.assertEqual(setup.returncode, 0, setup.stdout[-6000:])
        prototype_identity = _ORIGINAL_CAPTURE_IDENTITY(prototype, prototype_evidence)
        dependency_state = prototype_identity["dependency_state"]
        config_hash = retention.configuration_key_from_identity(
            prototype_identity, dependency_state=dependency_state)
        reordered_prototype_identity = {
            **prototype_identity,
            "meson_options": list(reversed(prototype_identity["meson_options"])),
        }
        self.assertEqual(
            config_hash,
            retention.configuration_key_from_identity(
                reordered_prototype_identity, dependency_state=dependency_state),
            "Meson option-record ordering must not change the canonical config key",
        )

        plan = lease.session_plan
        plan["source_identity"].update({"root": str(source), "revision": revision, "sha256": source_hash})
        plan["tools"] = tools
        plan["environment"] = environment
        plan["steps"][0]["argv"] = setup_argv
        plan["steps"][1]["argv"] = [str(ninja), "-C", str(workspace)]
        plan["steps"][2]["argv"] = [str(meson), "install", "-C", str(workspace),
                                      "--destdir", str(workspace / plan["stage_root"]), "--no-rebuild"]
        frozen_source_target = source / "meson.build"
        frozen_source_target_hash = retention._hash_path(frozen_source_target)
        evidence_target = self.repo / "experiments/storage/protected-evidence/synthetic-receipt.json"
        evidence_target.parent.mkdir(parents=True, exist_ok=True)
        evidence_target.write_text("protected synthetic evidence\n", encoding="utf-8")
        evidence_target_hash = retention._hash_path(evidence_target)
        cache_target = self.cache / "outside-cache/probe.bin"
        cache_target.parent.mkdir(parents=True, exist_ok=True)
        python = Path(sys.executable).resolve(strict=True)
        plan["steps"][3]["arguments"] = [str(oracle_write), str(workspace), str(image),
                                            str(frozen_source_target), str(evidence_target),
                                            str(cache_target), str(python), str(escaped_writer)]
        lease.source_tree_hash = source_hash
        lease.configuration_key_sha256 = config_hash
        lease.metadata["dependency_state"] = dependency_state
        lease.session_plan_sha256 = hashlib.sha256(retention._canonical_json(plan)).hexdigest()
        self._write_owner_manifest(
            source_hash, config_hash, dependency_state=dependency_state,
            session_plan_sha256=lease.session_plan_sha256,
            admission_identity=plan["admission_identity"],
        )

        pre_source_hash = source_hash
        observations: dict[str, object] = {"events": [], "readonly_tree_before": None,
                                           "readonly_tree_after": None, "readonly_image_before": None,
                                           "readonly_image_after": None}
        child_pid: int | None = None

        def read_pipe_line(label: str) -> str:
            readable, _, _ = select.select([oracle_read], [], [], 8)
            if not readable:
                raise AssertionError(f"setsid test child did not report {label}")
            data = bytearray()
            while not data.endswith(b"\n"):
                chunk = os.read(oracle_read, 1)
                if not chunk:
                    raise AssertionError(f"setsid test child closed its oracle before {label}")
                data.extend(chunk)
            return data.decode("ascii").strip()

        def audited_detach(root: Path, identity: object) -> dict:
            nonlocal child_pid
            if Path(root) == lease.path and isinstance(identity, dict) and identity.get("attached") is True:
                record = retention._load_lease(build_id)
                session_record = record["session_v3"]
                terminal = lease.path / retention.BUILD_SESSION_DIRECTORY / retention.BUILD_SESSION_TERMINAL
                pending = lease.path / retention.BUILD_SESSION_DIRECTORY / "session-terminal.pending.json"
                manifest = retention.MANIFEST_ROOT / f"{build_id}.json"
                evidence_dir = retention.MANIFEST_ROOT / f"{build_id}.evidence"
                self.assertEqual(session_record["state"], "QUIESCING")
                self.assertEqual(session_record["acceptance_state"],
                                 "UNACCEPTED_UNTIL_NORMAL_UNMOUNT_AND_DETACH")
                self.assertFalse(terminal.exists())
                self.assertFalse(pending.exists())
                self.assertFalse(manifest.exists())
                self.assertFalse(evidence_dir.exists())
                observations["events"].append("NO_ACCEPTED_OUTPUT_BEFORE_WRITABLE_DETACH")
                detached = _ORIGINAL_DETACH_SESSION_WORKSPACE(root, identity)
                self.assertFalse(detached["attached"])
                observations["events"].append("NORMAL_UNMOUNT_AND_DETACH_SUCCEEDED")
                if child_pid is None:
                    ready = workspace / ".tmp/escaped-present-accounting"
                    child_pid = int(ready.read_text(encoding="ascii"))
                    child_pids.append(child_pid)
                os.kill(child_pid, signal.SIGUSR1)
                rw_report = read_pipe_line("post-detach writes")
                observations["rw_report"] = rw_report
                return detached
            return _ORIGINAL_DETACH_SESSION_WORKSPACE(root, identity)

        def audited_readonly_attach(root: Path, detached: object) -> dict:
            self.assertIn("NORMAL_UNMOUNT_AND_DETACH_SUCCEEDED", observations["events"])
            terminal = lease.path / retention.BUILD_SESSION_DIRECTORY / retention.BUILD_SESSION_TERMINAL
            pending = lease.path / retention.BUILD_SESSION_DIRECTORY / "session-terminal.pending.json"
            self.assertFalse(terminal.exists())
            self.assertFalse(pending.exists())
            readonly = _ORIGINAL_ATTACH_SESSION_WORKSPACE_READONLY(root, detached)
            self.assertTrue(readonly["readonly"])
            observations["events"].append("EXACT_IMAGE_READONLY_REATTACHED")
            assert child_pid is not None
            os.kill(child_pid, signal.SIGUSR2)
            observations["ro_report"] = read_pipe_line("read-only mount write attempt")
            return readonly

        original_copy_tree = retention._copy_tree_artifacts

        def audited_copy_tree(root: Path, selected_build_id: str, evidence_root: Path):
            self.assertIn("EXACT_IMAGE_READONLY_REATTACHED", observations["events"])
            record = retention._load_lease(build_id)
            readonly_identity = record["session_v3"]["readonly_mount_identity"]
            self.assertIs(retention._session_image_row(lease.path)["writeable"], False)
            retention._verify_session_workspace_readonly(lease.path, readonly_identity)
            tree_before = retention._session_tree_digest(root, exclude_controller_state=True)
            image_before = retention._session_image_content_identity(image)
            time.sleep(0.2)
            tree_after = retention._session_tree_digest(root, exclude_controller_state=True)
            image_after = retention._session_image_content_identity(image)
            self.assertEqual(tree_before, tree_after)
            self.assertEqual(image_before, image_after)
            observations["readonly_tree_before"] = tree_before
            observations["readonly_tree_after"] = tree_after
            observations["readonly_image_before"] = image_before
            observations["readonly_image_after"] = image_after
            observations["events"].append("READONLY_OUTPUT_HASH_STABLE")
            return original_copy_tree(root, selected_build_id, evidence_root)

        def popen_with_test_oracle(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args")
            if isinstance(argv, (list, tuple)) and len(argv) >= 4 and str(argv[0]) == str(sandbox):
                if str(argv[3]) == str(workspace / "tests/present-accounting.exe"):
                    self.assertEqual(kwargs.get("close_fds"), True)
                    self.assertEqual(tuple(kwargs.get("pass_fds", ())), ())
                    kwargs["pass_fds"] = (oracle_write,)
                    observations["events"].append("TEST_ONLY_PIPE_ORACLE_ADDED_TO_NATIVE_TEST")
            process = original_popen(*args, **kwargs)
            controller_processes.append(process)
            return process

        started = False
        try:
            with (mock.patch.object(retention, "BUILD_SESSION_HOST_V3_TEST_BYPASS", True),
                  mock.patch.object(retention, "_capture_identity", _ORIGINAL_CAPTURE_IDENTITY),
                  mock.patch.object(retention, "_attach_session_workspace",
                                    _ORIGINAL_ATTACH_SESSION_WORKSPACE),
                  mock.patch.object(retention, "_verify_session_workspace_mount",
                                    _ORIGINAL_VERIFY_SESSION_WORKSPACE_MOUNT),
                  mock.patch.object(retention, "_detach_session_workspace", side_effect=audited_detach),
                  mock.patch.object(retention, "_attach_session_workspace_readonly",
                                    side_effect=audited_readonly_attach),
                  mock.patch.object(retention, "_verify_session_workspace_readonly",
                                    _ORIGINAL_VERIFY_SESSION_WORKSPACE_READONLY),
                  mock.patch.object(retention, "_detach_session_workspace_readonly",
                                    _ORIGINAL_DETACH_SESSION_WORKSPACE_READONLY),
                  mock.patch.object(retention, "_contain_session_processes",
                                    _ORIGINAL_CONTAIN_SESSION_PROCESSES),
                  mock.patch.object(retention, "_require_no_live_process", _ORIGINAL_REQUIRE_NO_LIVE_PROCESS),
                  mock.patch.object(retention, "_copy_tree_artifacts", side_effect=audited_copy_tree),
                  mock.patch.object(retention.subprocess, "Popen", side_effect=popen_with_test_oracle)):
                lease.reserve()
                started = True
                session = lease.start_session()
                for kind in ("configure", "build", "install", "test"):
                    try:
                        result = session.run_next()
                    except BaseException as error:
                        failed_record = retention._load_lease(build_id)
                        step_index = failed_record.get("session_v3", {}).get("failed_step_index")
                        step_log = (session.session_dir / "steps" / f"step-{step_index:03d}.log"
                                    if isinstance(step_index, int) else None)
                        log_tail = step_log.read_text(errors="replace")[-6000:] if step_log and step_log.exists() else ""
                        raise AssertionError(
                            f"real BuildLease {kind} step failed: {error}; log tail: {log_tail}") from error
                    self.assertEqual(result["lease_state"], "ACTIVE")
                    if kind == "test":
                        ready = workspace / ".tmp/escaped-present-accounting"
                        child_pid = int(ready.read_text(encoding="ascii"))
                        child_pids.append(child_pid)
                os.close(oracle_write)
                verification = session.verify_source()
                self.assertEqual(verification["lease_state"], "ACTIVE")
                self.assertFalse((session.session_dir / retention.BUILD_SESSION_TERMINAL).exists())
                result = session.finalize()

            self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")
            self.assertIn("NO_ACCEPTED_OUTPUT_BEFORE_WRITABLE_DETACH", observations["events"])
            self.assertIn("NORMAL_UNMOUNT_AND_DETACH_SUCCEEDED", observations["events"])
            self.assertIn("EXACT_IMAGE_READONLY_REATTACHED", observations["events"])
            self.assertIn("READONLY_OUTPUT_HASH_STABLE", observations["events"])
            expected_rw = [errno.EACCES, errno.EPERM, errno.EPERM, errno.EPERM, errno.EPERM]
            rw_report = observations["rw_report"]
            self.assertEqual(rw_report, "RW:" + ",".join(str(value) for value in expected_rw)
                             + f";PID:{child_pid}")
            self.assertEqual(observations["ro_report"], f"RO:{errno.EROFS};PID:{child_pid}")
            self.assertEqual(retention._tree_hash(source)[0], pre_source_hash)
            self.assertEqual(retention._hash_path(frozen_source_target), frozen_source_target_hash)
            self.assertEqual(retention._hash_path(evidence_target), evidence_target_hash)
            os.kill(child_pid, 0)

            manifest = retention._read_json(retention.MANIFEST_ROOT / f"{build_id}.json")
            terminal = retention._read_json(
                retention.MANIFEST_ROOT / f"{build_id}.evidence/execution/session-terminal.json",
                sealed=True,
            )
            self.assertEqual(terminal["state"], "SUCCESS")
            self.assertEqual(terminal["executor_mode"], "HOST_BUILDLEASE_V3")
            self.assertEqual(terminal["executor_version"], retention.BUILD_SESSION_SCHEMA_VERSION)
            self.assertEqual(terminal["acceptance_state"], "HOST_BUILDLEASE_V3_WRITER_QUIESCED")
            self.assertEqual(terminal["output_tree_sha256"], observations["readonly_tree_before"][0])
            self.assertEqual(manifest["build_identity"]["sandboxed_execution_executor_mode"],
                             "HOST_BUILDLEASE_V3")
            self.assertEqual(manifest["build_identity"]["source_identity"]["content_tree_sha256"],
                             pre_source_hash)
            self.assertEqual(observations["readonly_image_before"], observations["readonly_image_after"])
        finally:
            for process in controller_processes:
                if process.poll() is None:
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
            if child_pid is not None:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                os.close(oracle_write)
            except OSError:
                pass
            if started:
                try:
                    record = retention._load_lease(build_id)
                    session_record = record.get("session_v3", {})
                    if retention._session_image_row(lease.path) is not None:
                        if retention._session_image_row(lease.path).get("writeable") is False:
                            readonly_identity = session_record.get("readonly_mount_identity")
                            if isinstance(readonly_identity, dict):
                                _ORIGINAL_DETACH_SESSION_WORKSPACE_READONLY(lease.path, readonly_identity)
                        else:
                            workspace_identity = session_record.get("workspace_mount_identity")
                            if isinstance(workspace_identity, dict):
                                _ORIGINAL_DETACH_SESSION_WORKSPACE(lease.path, workspace_identity)
                except (OSError, retention.RetentionError):
                    pass

    def test_policy_gate_rejects_legacy_v2_plans_after_v3_admission(self) -> None:
        legacy_plans = (
            {"schema_version": 2, "executor_mode": "HOST_BUILDLEASE_V2"},
            {"schema_version": 2, "executor_mode": "HOST_BUILDLEASE_V3"},
            {"schema_version": 3, "executor_mode": "HOST_BUILDLEASE_V2"},
        )
        with mock.patch.object(retention, "BUILD_SESSION_HOST_V3_POLICY_ACCEPTED", True), \
                mock.patch.object(retention, "BUILD_SESSION_HOST_V3_TEST_BYPASS", False):
            for legacy_plan in legacy_plans:
                with self.subTest(plan=legacy_plan), self.assertRaisesRegex(
                        retention.RetentionError,
                        "BuildLease accepts only an explicit HOST_BUILDLEASE_V3 execution plan"):
                    retention._require_buildlease_vm_runtime(legacy_plan)

    def test_policy_gate_pins_exact_reviewed_amendment_bytes(self) -> None:
        v3_plan = {"schema_version": 3, "executor_mode": "HOST_BUILDLEASE_V3"}
        with mock.patch.object(retention, "BUILD_SESSION_HOST_V3_POLICY_ACCEPTED", True), \
                mock.patch.object(retention, "BUILD_SESSION_HOST_V3_TEST_BYPASS", False):
            retention._require_buildlease_vm_runtime(v3_plan)
            altered_amendment = self.repo / "altered-policy-amendment.md"
            altered_amendment.write_text("different policy bytes\n", encoding="utf-8")
            with mock.patch.object(retention, "BUILD_SESSION_HOST_V3_POLICY_AMENDMENT_PATH",
                                   altered_amendment), self.assertRaisesRegex(
                    retention.RetentionError,
                    "reviewed BuildLease v3 policy amendment hash does not match"):
                retention._require_buildlease_vm_runtime(v3_plan)

    @_with_synthetic_buildlease_runtime_gate
    def test_session_holds_one_active_lease_through_ordered_steps_and_source_verification(self) -> None:
        lease, session = self._start_session("session-one-active")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        root_identity = retention._load_lease(lease.build_id)["build_inode"]
        for index, kind in enumerate(("configure", "build", "install", "test")):
            result = self._run_session_step(session, kind)
            self.assertEqual(result["lease_state"], "ACTIVE")
            record = retention._load_lease(lease.build_id)
            self.assertEqual(record["state"], "ACTIVE")
            self.assertEqual(record["session_v3"]["state"], "ACTIVE")
            self.assertEqual(record["session_v3"]["next_step_index"], index + 1)
            self.assertEqual(record["build_inode"], root_identity)
            self.assertFalse((lease.path / retention.BUILD_EXECUTION_RECEIPT).exists())
            self.assertFalse((lease.path / "build-command.json").exists())
        verification = session.verify_source()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(verification["lease_state"], "ACTIVE")
        self.assertEqual(record["state"], "ACTIVE")
        self.assertEqual(record["session_v3"]["next_step_index"], 5)
        self.assertEqual(record["session_v3"]["state"], "ACTIVE")
        self.assertEqual(len(record["session_v3"]["step_receipts"]), 5)
        self.assertFalse((session.session_dir / retention.BUILD_SESSION_TERMINAL).exists())
        with self.assertRaisesRegex(retention.RetentionError, "cannot be preserved as debug builds"):
            lease.complete(preserve_debug=True)
        still_active = retention._load_lease(lease.build_id)
        self.assertEqual(still_active["state"], "ACTIVE")
        self.assertEqual(still_active["session_v3"]["state"], "ACTIVE")
        self.assertTrue(still_active["session_v3"]["workspace_mount_identity"]["attached"])
        session.finalize()
        terminal_path = (retention.MANIFEST_ROOT / f"{lease.build_id}.evidence"
                         / "execution/session-terminal.json")
        terminal = retention._read_json(terminal_path, sealed=True)
        completed = retention._load_lease(lease.build_id)
        self.assertEqual(terminal["storage_watch_events"],
                         completed["session_v3"]["storage_watch_events"])
        self.assertLessEqual(len(terminal["storage_watch_events"]),
                             retention.BUILD_SESSION_MAX_STORAGE_WATCH_EVENTS)

    def test_v2_host_session_plan_fails_before_workspace_or_process_launch(self) -> None:
        lease = self._session_lease("session-vm-runtime-required")
        legacy_plan = dict(lease.session_plan or {})
        legacy_plan.update({"schema_version": 2, "executor_mode": "HOST_BUILDLEASE_V2"})
        with mock.patch.object(retention, "BUILD_SESSION_HOST_V3_POLICY_ACCEPTED", True), \
                mock.patch.object(retention, "BUILD_SESSION_HOST_V3_TEST_BYPASS", False):
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "unsupported BuildLease session plan version"):
                retention._session_validate_plan(
                    legacy_plan, build_id=lease.build_id, build_path=lease.path,
                    source_tree_hash=lease.source_tree_hash, expected_bytes=lease.expected_bytes)

    def test_v2_success_receipt_verification_cannot_accept_host_sandbox_evidence(self) -> None:
        lease = self._session_lease("session-vm-receipt-required")
        legacy_session = {
            "schema_version": 2, "executor_mode": "HOST_BUILDLEASE_V2", "executor_version": 2,
            "state": "READY_TO_FINALIZE", "plan_sha256": lease.session_plan_sha256,
        }
        with self.assertRaisesRegex(
                retention.RetentionError,
                "v3 session is not ready for its single explicit finalization"):
            lease._verified_session_execution_receipt({"session_v3": legacy_session})

    def test_serialized_active_v2_session_cannot_resume_on_host(self) -> None:
        lease = self._session_lease("session-vm-resume-required")
        lease.reserve()
        # Start a synthetic v3-shaped reservation, then model persisted legacy v2 state.
        with mock.patch.object(retention, "_require_buildlease_vm_runtime", return_value=None):
            session = lease.start_session()
        record = retention._load_lease(lease.build_id)
        record["session_v3"].update({
            "schema_version": 2, "executor_mode": "HOST_BUILDLEASE_V2", "executor_version": 2,
        })
        retention._store_lease(record)
        with self._patch_session_popen() as popen:
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "v3 BuildLease session is no longer the same ACTIVE reservation"):
                session.run_next()
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "v3 BuildLease session is no longer the same ACTIVE reservation"):
                session.verify_source()
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "v3 BuildLease session is no longer the same ACTIVE reservation"):
                session.finalize()
        popen.assert_not_called()
        session._fail("model-only resumed-session cleanup", -1,
                      command=["session-resume-model-cleanup"], step_index=None)

    @_with_synthetic_buildlease_runtime_gate
    def test_session_storage_watch_poll_samples_and_failure_evidence_are_bounded(self) -> None:
        lease, session = self._start_session("session-watch-telemetry-bound")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        lease_record = retention._load_lease(lease.build_id)
        session_record = lease_record["session_v3"]
        session_record["peak_allocated_bytes"] = 1
        lease_record["session_v3"] = session_record
        baseline = len(lease.storage_watch_events)
        admission = {"admitted": True, "warnings": [], "blockers": []}
        with mock.patch.object(session, "_load", return_value=(lease_record, session_record)), \
                mock.patch.object(session, "_check_root_bindings"), \
                mock.patch.object(retention, "_session_measure_tree", return_value={"allocated_bytes": 1}), \
                mock.patch.object(session, "_check_controller_evidence_budget", return_value=(0, 0)), \
                mock.patch.object(session, "_store"), \
                mock.patch.object(storage_policy, "disk_free_bytes", return_value=100 * retention.GIB), \
                mock.patch.object(storage_policy, "measure_project_usage", return_value={}), \
                mock.patch.object(storage_policy, "conservative_project_usage_bytes", return_value=0), \
                mock.patch.object(storage_policy, "projected_storage_admission", return_value=admission):
            for _ in range(100):
                session._check_limits("during-step")
        self.assertEqual(len(lease.storage_watch_events), baseline)
        for index in range(retention.BUILD_SESSION_MAX_STORAGE_WATCH_EVENTS - baseline):
            lease._append_session_storage_event({"synthetic_event": index})
        with self.assertRaisesRegex(retention.RetentionError, "fixed event limit"):
            lease._append_session_storage_event({"synthetic_event": "overflow"})
        self.assertEqual(len(lease.storage_watch_events), retention.BUILD_SESSION_MAX_STORAGE_WATCH_EVENTS)
        session._fail("bounded telemetry regression cleanup", -1,
                      command=["session-telemetry-regression"], step_index=None)
        failed = retention._load_lease(lease.build_id)
        failure = retention._read_json(Path(failed["failure_evidence_path"]))
        self.assertEqual(len(failure["storage_watch_events"],),
                         retention.BUILD_SESSION_MAX_STORAGE_WATCH_EVENTS)
        self.assertLessEqual(len(failed["session_v3"]["storage_watch_events"]),
                             retention.BUILD_SESSION_MAX_STORAGE_WATCH_EVENTS)

    @_with_synthetic_buildlease_runtime_gate
    def test_buildlease_session_requires_its_reserved_owner(self) -> None:
        lease = self._session_lease("session-owner-construction")
        with self.assertRaisesRegex(retention.RetentionError, "created once by its owning BuildLease"):
            retention.BuildLeaseSession(lease)
        lease.reserve()
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        with self.assertRaisesRegex(retention.RetentionError, "created once by its owning BuildLease"):
            retention.BuildLeaseSession(lease)
        session = lease.start_session()
        self.assertIs(lease.session, session)
        with self.assertRaisesRegex(retention.RetentionError, "created once by its owning BuildLease"):
            retention.BuildLeaseSession(lease)

    @_with_synthetic_buildlease_runtime_gate
    def test_heartbeat_rereads_state_before_persisting_under_session_mutex(self) -> None:
        lease, session = self._start_session("session-heartbeat-state-race")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        original_load = retention._load_lease
        heartbeat_read = threading.Event()
        release_heartbeat = threading.Event()
        heartbeat_errors: list[BaseException] = []
        paused = False

        def pause_after_initial_read(build_id: str) -> dict[str, object]:
            nonlocal paused
            record = original_load(build_id)
            if build_id == lease.build_id and not paused:
                paused = True
                heartbeat_read.set()
                if not release_heartbeat.wait(5):
                    raise RuntimeError("synthetic heartbeat was not released")
            return record

        def run_heartbeat() -> None:
            try:
                lease.heartbeat()
            except BaseException as error:
                heartbeat_errors.append(error)

        heartbeat = threading.Thread(target=run_heartbeat, name="synthetic-session-heartbeat-test")
        with mock.patch.object(retention, "_load_lease", side_effect=pause_after_initial_read):
            heartbeat.start()
            try:
                self.assertTrue(heartbeat_read.wait(5), "heartbeat did not pause after its initial read")
                record = original_load(lease.build_id)
                session_record = record["session_v3"]
                session_record["state"] = "RUNNING"
                session_record["inflight_step_index"] = 0
                record["session_v3"] = session_record
                session._store(record)
            finally:
                release_heartbeat.set()
                heartbeat.join(timeout=10)
        self.assertFalse(heartbeat.is_alive(), "heartbeat did not finish after release")
        self.assertEqual(heartbeat_errors, [])
        lease_record = original_load(lease.build_id)
        self.assertEqual(lease_record["state"], "ACTIVE")
        self.assertEqual(lease_record["session_v3"]["state"], "RUNNING")
        root_fd = retention._open_dir(lease.path)
        try:
            marker = retention._read_marker(root_fd)
        finally:
            os.close(root_fd)
        self.assertEqual(marker["session_v3"]["state"], "RUNNING")

    @_with_synthetic_buildlease_runtime_gate
    def test_overlapping_session_operations_reject_without_mutating_running_step(self) -> None:
        lease, session = self._start_session("session-operation-lock")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        entered_wait = threading.Event()
        release_wait = threading.Event()
        results: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def hold_process_wait(*_args: object, **_kwargs: object) -> int:
            entered_wait.set()
            if not release_wait.wait(5):
                raise RuntimeError("synthetic process wait was not released")
            return 0

        def run_configure() -> None:
            try:
                results.append(session.run_next())
            except BaseException as error:
                errors.append(error)

        worker = threading.Thread(target=run_configure, name="synthetic-session-operation-test")
        with self._patch_session_popen(
                side_effect=lambda *_args, **_kwargs: self._session_popen(session)):
            with mock.patch.object(lease, "_wait_for_build_process", side_effect=hold_process_wait):
                with mock.patch.object(retention, "_session_process_group_exists", return_value=False):
                    worker.start()
                    try:
                        self.assertTrue(entered_wait.wait(5), "configure did not reach its active process wait")
                        active = retention._load_lease(lease.build_id)
                        self.assertEqual(active["state"], "ACTIVE")
                        self.assertEqual(active["session_v3"]["state"], "RUNNING")
                        for operation in (session.run_next, session.verify_source, session.finalize):
                            with self.assertRaisesRegex(
                                    retention.RetentionError,
                                    "overlaps another active session operation"):
                                operation()
                        unchanged = retention._load_lease(lease.build_id)
                        self.assertEqual(unchanged["state"], "ACTIVE")
                        self.assertEqual(unchanged["session_v3"]["state"], "RUNNING")
                        self.assertEqual(unchanged["session_v3"]["next_step_index"], 0)
                    finally:
                        release_wait.set()
                        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), "configure worker did not finish after release")
        self.assertEqual(errors, [])
        self.assertEqual(results[0]["lease_state"], "ACTIVE")
        finished = retention._load_lease(lease.build_id)
        self.assertEqual(finished["session_v3"]["state"], "ACTIVE")
        self.assertEqual(finished["session_v3"]["next_step_index"], 1)

    @_with_synthetic_buildlease_runtime_gate
    def test_session_finalization_verifies_ordered_plan_and_completes_exactly_once(self) -> None:
        lease, session = self._start_session("session-finalize-once")
        self._finish_session_steps(session)
        retire = mock.Mock(return_value={"result": "synthetic retirement"})
        with mock.patch.object(retention, "_retire", retire):
            result = session.finalize()
            self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")
            with self.assertRaises(retention.RetentionError):
                session.finalize()
            with self.assertRaises(retention.RetentionError):
                lease.complete()
            with self.assertRaises(retention.RetentionError):
                session.run_next()
            with self.assertRaises(retention.RetentionError):
                session.verify_source()
        self.assertEqual(retire.call_count, 1)
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "COMPLETED")
        self.assertEqual(record["session_v3"]["state"], "ACCEPTED")
        terminal = retention._read_json(session.session_dir / retention.BUILD_SESSION_TERMINAL, sealed=True)
        self.assertEqual(terminal["state"], "SUCCESS")
        self.assertEqual(terminal["plan_sha256"], session.plan_sha256)
        self.assertEqual(terminal["step_count"], len(session.plan["steps"]))

    @_with_synthetic_buildlease_runtime_gate
    def test_session_finalization_rejects_terminal_root_identity_mismatch(self) -> None:
        lease, session = self._start_session("session-terminal-root-identity")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        original_complete = lease.complete

        def tamper_terminal_then_complete() -> dict[str, object]:
            terminal_path = session.session_dir / retention.BUILD_SESSION_TERMINAL
            terminal = retention._read_json(terminal_path, sealed=True)
            terminal["build_root_identity"] = dict(terminal["build_root_identity"])
            terminal["build_root_identity"]["inode"] += 1
            retention._write_json(terminal_path, terminal, replace=True, sealed=True)
            record = retention._load_lease(lease.build_id)
            record["session_v3"]["terminal_sha256"] = retention._hash_path(terminal_path)
            retention._store_lease(record)
            root_fd = retention._open_dir(lease.path)
            try:
                marker = retention._read_marker(root_fd)
                marker.update(record)
                retention._write_marker(root_fd, marker)
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            return original_complete()

        with mock.patch.object(lease, "complete", side_effect=tamper_terminal_then_complete):
            with mock.patch.object(lease, "fail", side_effect=retention.RetentionError("synthetic stop")):
                with self.assertRaisesRegex(retention.RetentionError,
                                            "v3 terminal receipt does not bind its signed plan and active lease"):
                    session.finalize()
        failed = retention._load_lease(lease.build_id)
        self.assertEqual(failed["state"], "FAILED")
        self.assertFalse((lease.path / retention.BUILD_EXECUTION_RECEIPT).exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_session_cumulative_peak_is_one_lease_accounting_peak(self) -> None:
        lease, session = self._start_session("session-one-peak")
        self._run_session_step(session, "configure")
        self._run_session_step(session, "build", extra_build_bytes=512 * 1024)
        record = retention._load_lease(lease.build_id)
        peak = record["session_v3"]["peak_allocated_bytes"]
        self.assertGreater(peak, 0)
        self.assertLessEqual(peak, lease.expected_bytes)
        self.assertEqual(record["expected_bytes"], 64 * 1024 * 1024)
        self.assertEqual(record["session_v3"]["next_step_index"], 2)
        self.assertEqual(len(list(retention.LEASE_ROOT.glob("*.json"))), 1)
        self.assertGreaterEqual(peak, retention._session_measure_tree(lease.path)["allocated_bytes"])

    @_with_synthetic_buildlease_runtime_gate
    def test_session_cumulative_growth_beyond_reservation_terminalizes(self) -> None:
        lease, session = self._start_session("session-reservation-overrun")
        measure = retention._session_measure_tree
        injected_overflow = False

        def measure_with_overflow(path: Path, **kwargs: object) -> dict[str, int]:
            observed = measure(path, **kwargs)
            if injected_overflow and path == lease.path:
                observed = dict(observed)
                observed["allocated_bytes"] = lease.expected_bytes + 4096
            return observed

        def launch_and_grow(*_args: object, **_kwargs: object) -> SimpleNamespace:
            nonlocal injected_overflow
            injected_overflow = True
            return self._session_popen(session)

        with mock.patch.object(retention, "_session_measure_tree", side_effect=measure_with_overflow):
            with self._patch_session_popen(side_effect=launch_and_grow):
                with self.assertRaisesRegex(retention.RetentionError, "cumulative allocation"):
                    session.run_next()
        self._assert_failed_session_retired(lease, session)
        self.assertFalse((session.session_dir / "steps/step-000.result.json").exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_session_runtime_free_space_hard_stop_blocks_launch(self) -> None:
        lease, session = self._start_session("session-free-hard-stop")
        with mock.patch.object(storage_policy, "disk_free_bytes", return_value=storage_policy.FREE_HARD_STOP - 1):
            with self._patch_session_popen(
                    side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                with self.assertRaisesRegex(retention.RetentionError, "runtime storage limits"):
                    session.run_next()
        popen.assert_not_called()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_session_runtime_project_scope_hard_limit_blocks_launch(self) -> None:
        lease, session = self._start_session("session-project-hard-stop")
        over_limit = {"allocated_inode_deduplicated_bytes": storage_policy.PROJECT_BUDGET}
        with mock.patch.object(storage_policy, "measure_project_usage", return_value=over_limit):
            with self._patch_session_popen(
                    side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                with self.assertRaisesRegex(retention.RetentionError, "runtime storage limits"):
                    session.run_next()
        popen.assert_not_called()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_meson_install_destination_is_stage_bound_and_writes_stay_in_lease(self) -> None:
        lease, session = self._start_session("session-stage-accepted")
        self._run_session_step(session, "configure")
        self._run_session_step(session, "build")
        stage = session.workspace / "stage"
        workspace_extra = session.workspace / "install-log-outside-stage.bin"
        outside_workspace = lease.path / "install-output-outside-workspace.bin"
        denied_attempts: list[Path] = []

        def installer_attempt(argv: list[str], **kwargs: object) -> SimpleNamespace:
            profile = argv[2]
            workspace_rule = f'(allow file-write* (subpath "{session.workspace}"))'
            lease_wide_rule = f'(allow file-write* (subpath "{lease.path}"))'
            controller_deny = f'(deny file-write* (subpath "{session.session_dir}"))'
            self.assertIn(workspace_rule, profile)
            self.assertNotIn(lease_wide_rule, profile)
            self.assertIn(controller_deny, profile)
            self.assertEqual(kwargs["env"]["TMPDIR"], str(stage / ".buildlease-install-tmp"))
            workspace_extra.write_bytes(b"Meson install log remains inside the writable lease")
            try:
                if session.workspace == outside_workspace or session.workspace in outside_workspace.parents:
                    outside_workspace.write_bytes(b"blocked install escape probe")
                else:
                    raise PermissionError("Seatbelt profile denies writes outside the writable lease")
            except PermissionError:
                denied_attempts.append(outside_workspace)
            return self._session_popen(session)

        with self._patch_session_popen(side_effect=installer_attempt) as popen:
            stage_result = session.run_next()
        popen.assert_called_once()
        staged_file = session.workspace / "stage/usr/lib/libsynthetic.dylib"
        self.assertTrue(staged_file.is_file())
        self.assertTrue(workspace_extra.is_file())
        self.assertEqual(stage_result["lease_state"], "ACTIVE")
        self.assertEqual(denied_attempts, [outside_workspace])
        self.assertFalse(outside_workspace.exists())
        self.assertFalse((stage / ".buildlease-install-tmp").exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_sandboxed_build_cannot_rewrite_or_precreate_session_receipts(self) -> None:
        lease, session = self._start_session("session-receipt-tamper")
        self._run_session_step(session, "configure")
        prior_receipt = session.session_dir / "steps/step-000.command.json"
        prior_digest = retention._hash_path(prior_receipt)
        current_result = session.session_dir / "steps/step-001.result.json"
        future_receipt = session.session_dir / "steps/step-004.command.json"
        denied_attempts: list[Path] = []

        def attacker_attempt(argv: list[str], **kwargs: object) -> SimpleNamespace:
            profile = argv[2]
            workspace_allow = f'(allow file-write* (subpath "{session.workspace}"))'
            controller_deny = f'(deny file-write* (subpath "{session.session_dir}"))'
            self.assertIn(workspace_allow, profile)
            self.assertNotIn(f'(allow file-write* (subpath "{lease.path}"))', profile)
            self.assertIn(controller_deny, profile)

            def sandboxed_write(path: Path, payload: bytes) -> None:
                if (controller_deny in profile
                        and (path == session.session_dir or session.session_dir in path.parents)):
                    raise PermissionError("Seatbelt profile denies controller evidence writes")
                path.write_bytes(payload)

            for path, payload in ((prior_receipt, b"tampered prior receipt"),
                                  (current_result, b"forged current result"),
                                  (future_receipt, b"precreated future command")):
                try:
                    sandboxed_write(path, payload)
                except PermissionError:
                    denied_attempts.append(path)
            return self._session_popen(session)

        index = retention._load_lease(lease.build_id)["session_v3"]["next_step_index"]
        self.assertEqual(session.plan["steps"][index]["kind"], "build")
        with self._patch_session_popen(side_effect=attacker_attempt) as popen:
            session.run_next()
        popen.assert_called_once()
        self.assertEqual(denied_attempts, [prior_receipt, current_result, future_receipt])
        self.assertEqual(retention._hash_path(prior_receipt), prior_digest)
        self.assertTrue(current_result.is_file())
        current_record = retention._read_json(current_result, sealed=True)
        self.assertEqual(current_record["status"], "SUCCESS")
        self.assertEqual(current_record["step_index"], 1)
        self.assertFalse(future_receipt.exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_session_fails_closed_when_descendant_keeps_stdout_pipe_open(self) -> None:
        lease, session = self._start_session("session-inherited-output-pipe")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        read_fd, inherited_writer_fd = os.pipe()
        process_stdout = os.fdopen(read_fd, "rb", buffering=0)

        class ExitedLeader:
            pid = 54321

            def __init__(self) -> None:
                self.stdout = process_stdout

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def poll(self) -> int:
                return 0

        process = ExitedLeader()
        contain_session = mock.Mock(return_value={"session_id": 54321,
                                                   "members_seen": [54321, 54322],
                                                   "survivors": [],
                                                   "leader_held_unreaped": True})
        try:
            with self._patch_session_popen(return_value=process):
                with mock.patch.object(lease, "_wait_for_build_process", return_value=0):
                    with mock.patch.object(retention, "BUILD_SESSION_OUTPUT_DRAIN_SECONDS", 0.01):
                        with mock.patch.object(retention, "_contain_session_processes", contain_session):
                            with self.assertRaisesRegex(
                                    retention.RetentionError,
                                    "output capture ended without verified EOF"):
                                session.run_next()
            contain_session.assert_called_once_with(process.pid)
            blocked = retention._load_lease(lease.build_id)
            self.assertEqual(blocked["state"], "ACTIVE")
            self.assertEqual(blocked["session_v3"]["state"], "RUNNING")
            self.assertTrue(blocked["session_v3"]["output_capture_uncertain"])
            self.assertFalse((session.session_dir / "steps/step-000.result.json").exists())
        finally:
            os.close(inherited_writer_fd)
            process_stdout.close()

    @_with_synthetic_buildlease_runtime_gate
    def test_live_output_reader_keeps_session_running_and_reservation_blocking(self) -> None:
        lease, session = self._start_session("session-live-output-reader")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)

        class StuckReader:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.ident: int | None = None

            def start(self) -> None:
                self.ident = 4242

            def join(self, timeout: float | None = None) -> None:
                return None

            def is_alive(self) -> bool:
                return self.ident is not None

        contain_session = mock.Mock(return_value={"session_id": 987654321,
                                                   "members_seen": [987654321],
                                                   "survivors": [],
                                                   "leader_held_unreaped": True})
        launched_processes: list[SimpleNamespace] = []

        def fake_popen(*_args: object, **_kwargs: object) -> SimpleNamespace:
            process = self._session_popen(session)
            launched_processes.append(process)
            return process

        with self._patch_session_popen(side_effect=fake_popen):
            with mock.patch.object(lease, "_wait_for_build_process", return_value=0):
                with mock.patch.object(retention, "BUILD_SESSION_OUTPUT_DRAIN_SECONDS", 0.01):
                    with mock.patch.object(retention, "_contain_session_processes", contain_session):
                        with mock.patch.object(retention.threading, "Thread", StuckReader):
                            fail = mock.Mock(wraps=session._fail)
                            with mock.patch.object(session, "_fail", fail):
                                with self.assertRaisesRegex(retention.RetentionError,
                                                            "output reader did not stop"):
                                    session.run_next()
                                fail.assert_not_called()

        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "ACTIVE")
        self.assertEqual(record["session_v3"]["state"], "RUNNING")
        self.assertTrue(record["session_v3"]["output_capture_uncertain"])
        self.assertIsNotNone(lease.lock_fds)
        self.assertFalse((session.session_dir / "steps/step-000.result.json").exists())
        launched_processes[0].stdout.close()
        with self.assertRaisesRegex(retention.RetentionError, "remains durably RUNNING"):
            session.run_next()
        with self.assertRaisesRegex(
                retention.RetentionError,
                "cannot capture or retire a session with uncertain process or output containment"):
            lease.fail(command=["session-failure"], return_code=-1, error="synthetic")
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "ACTIVE")

    @_with_synthetic_buildlease_runtime_gate
    def test_session_log_fsync_failure_keeps_output_capture_uncertain(self) -> None:
        lease, session = self._start_session("session-log-fsync-failure")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        log_path = session.session_dir / "steps/step-000.log"
        original_fsync = os.fsync

        def fail_only_session_log(fd: int) -> None:
            try:
                current_info = os.fstat(fd)
                log_info = log_path.stat()
            except FileNotFoundError:
                original_fsync(fd)
                return
            if (current_info.st_dev, current_info.st_ino) == (log_info.st_dev, log_info.st_ino):
                raise OSError(errno.EIO, "synthetic log fsync failure")
            original_fsync(fd)

        with self._patch_session_popen(
                side_effect=lambda *_args, **_kwargs: self._session_popen(session)):
            with mock.patch.object(retention.os, "fsync", side_effect=fail_only_session_log):
                with self.assertRaisesRegex(retention.RetentionError, "output log durability is uncertain"):
                    session.run_next()

        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "ACTIVE")
        self.assertEqual(record["session_v3"]["state"], "RUNNING")
        self.assertTrue(record["session_v3"]["output_capture_uncertain"])
        self.assertTrue(log_path.is_file())
        self.assertFalse((session.session_dir / "steps/step-000.result.json").exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_install_destination_outside_declared_stage_root_is_rejected(self) -> None:
        build_id = "session-stage-redirection"
        meson = self.repo / f"tool-bin-{build_id}/meson"
        lease = self._session_lease(
            build_id,
            install_argv=[str(meson), "install", "-C", str(retention.BUILD_ROOT / build_id),
                          "--destdir", str(self.repo / "external-stage")])
        lease.reserve()
        session = lease.start_session()
        self._run_session_step(session, "configure")
        self._run_session_step(session, "build")
        with self.assertRaisesRegex(retention.RetentionError, "declared session stage root"):
            session.run_next()
        self.assertFalse((self.repo / "external-stage").exists())
        self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    def test_stage_root_traversal_is_rejected_before_lease_creation(self) -> None:
        with self.assertRaises(retention.RetentionError):
            self._session_lease("session-stage-traversal", stage_root="../outside")
        with self.assertRaises(retention.RetentionError):
            self._session_lease("session-destdir-environment", environment_overrides={
                "DESTDIR": str(self.repo / "external-stage")})
        self.assertFalse((retention.BUILD_ROOT / "session-stage-traversal").exists())

    def test_stage_root_symlink_escape_is_rejected_and_external_target_is_untouched(self) -> None:
        lease = self._session_lease("session-stage-symlink")
        lease.reserve()
        outside = self.repo / "outside-stage"
        outside.mkdir()
        (lease.path / "stage").symlink_to(outside, target_is_directory=True)
        with self.assertRaises(retention.RetentionError):
            lease.start_session()
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_wrappers_arbitrary_builders_and_unapproved_meson_options_are_rejected(self) -> None:
        build_id = "session-unapproved-meson-option"
        invalid_meson = str(self.repo / f"tool-bin-{build_id}/meson")
        invalid_build = str(retention.BUILD_ROOT / build_id)
        invalid_source = str(self.repo / f"source-{build_id}")
        cases = (
            ("session-shell-wrapper", ["/bin/sh", "-c", "echo escaped"]),
            ("session-cmake-builder", ["/usr/bin/cmake", "-S", ".", "-B", "build"]),
            (build_id, [invalid_meson, "setup", invalid_build, invalid_source, "--prefix=/tmp"]),
        )
        for build_id, argv in cases:
            with self.subTest(argv=argv):
                lease = self._session_lease(build_id, configure_argv=argv)
                lease.reserve()
                session = lease.start_session()
                input_receipt = lease._validate_session_inputs()
                with mock.patch.object(lease, "_validate_session_inputs", return_value=input_receipt):
                    with self._patch_session_popen() as popen:
                        with self.assertRaises(retention.RetentionError):
                            session.run_next()
                        popen.assert_not_called()

        valid_lease = self._session_lease("session-command-substitution-plan")
        invalid_plan = dict(valid_lease.session_plan)
        invalid_plan["steps"] = [dict(step) for step in valid_lease.session_plan["steps"]]
        invalid_plan["steps"][0]["argv"] = [*invalid_plan["steps"][0]["argv"], "--prefix=$(touch /tmp/escape)"]
        with self._patch_session_popen() as popen:
            with self.assertRaisesRegex(retention.RetentionError, "argv is invalid"):
                retention._session_validate_plan(
                    invalid_plan, build_id=valid_lease.build_id, build_path=valid_lease.path,
                    source_tree_hash=valid_lease.source_tree_hash, expected_bytes=valid_lease.expected_bytes)
            popen.assert_not_called()

    def test_test_executable_must_be_registered_beneath_admitted_output_root(self) -> None:
        with self.assertRaises(retention.RetentionError):
            self._session_lease("session-test-outside", test_executable="/bin/true")
        with self.assertRaises(retention.RetentionError):
            self._session_lease("session-test-traversal", test_executable="../escape")

    @_with_synthetic_buildlease_runtime_gate
    def test_interpreter_based_registered_test_is_rejected(self) -> None:
        lease, session = self._start_session("session-test-interpreter")
        self._run_session_step(session, "configure")
        with self._patch_session_popen(
                side_effect=lambda *_args, **_kwargs: self._session_popen(
                    session, interpreter_test=True)) as popen:
            with self.assertRaisesRegex(retention.RetentionError, "interpreter-based"):
                session.run_next()
        popen.assert_called_once()
        self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_changed_registered_test_identity_is_rejected_before_execution(self) -> None:
        lease, session = self._start_session("session-test-identity-change")
        self._run_session_step(session, "configure")
        self._run_session_step(session, "build")
        self._run_session_step(session, "install")
        test_path = session.workspace / "tests/present-accounting.exe"
        test_path.write_bytes(b"replacement executable\n")
        test_path.chmod(0o700)
        with self._patch_session_popen(
                side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
            with self.assertRaisesRegex(retention.RetentionError, "identity changed"):
                session.run_next()
        popen.assert_not_called()
        self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_competing_reservation_waits_for_session_serialization_scope(self) -> None:
        first, session = self._start_session("session-serialization-first")
        second = self._session_lease("session-serialization-second")
        self.addCleanup(second._release_lock)
        self.addCleanup(second._stop_heartbeat)
        finished = threading.Event()
        outcome: dict[str, object] = {}

        def reserve_second() -> None:
            try:
                outcome["path"] = second.reserve()
            except BaseException as error:
                outcome["error"] = error
            finally:
                finished.set()

        worker = threading.Thread(target=reserve_second, daemon=True)
        worker.start()
        self.assertFalse(finished.wait(0.15), "competing reservation bypassed the active lease serialization lock")
        self.assertEqual(retention._load_lease(first.build_id)["state"], "ACTIVE")
        self._finish_session_steps(session)
        session.finalize()
        self.assertTrue(finished.wait(5), "competing reservation did not resume after explicit finalization")
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["path"], second.path)
        self.assertEqual(retention._load_lease(second.build_id)["state"], "ACTIVE")

    @_with_synthetic_buildlease_runtime_gate
    def test_failed_workspace_detach_stays_blocking_and_retries_from_pending_state(self) -> None:
        lease, session = self._start_session("session-detach-recovery")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        detach_error = retention.RetentionError("synthetic image is temporarily busy")
        with mock.patch.object(retention, "_detach_session_workspace",
                               side_effect=[detach_error, detach_error]):
            with self.assertRaisesRegex(retention.RetentionError, "could not be detached"):
                session._fail("synthetic detach failure", -1,
                              command=["workspace-detach-test"], step_index=None)
            failed = retention._load_lease(lease.build_id)
            self.assertEqual(failed["state"], "FAILED")
            self.assertTrue(failed["workspace_detach_uncertain"])
            self.assertTrue(failed["session_v3"]["workspace_mount_identity"]["attached"])
            self.assertTrue(lease.path.exists())
            with self.assertRaisesRegex(retention.RetentionError, "remains blocking until detach"):
                retention._retire_pending_locked()
        still_blocked = retention._load_lease(lease.build_id)
        self.assertEqual(still_blocked["state"], "FAILED")
        self.assertTrue(still_blocked["workspace_detach_uncertain"])
        self.assertTrue(lease.path.exists())
        with mock.patch.object(retention, "_detach_session_workspace",
                               side_effect=self._fake_detach_session_workspace):
            receipts = retention._retire_pending_locked()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["state"], "REMOVED")
        self.assertFalse(lease.path.exists())
        retired = retention._load_lease(lease.build_id)
        self.assertEqual(retired["state"], "FAILED")
        self.assertEqual(retired["retirement_status"], "COMPLETED")
        self.assertNotIn("workspace_detach_uncertain", retired)

    def test_detach_recovery_requires_the_original_empty_mountpoint_and_no_other_attachment(self) -> None:
        root = self.repo / "detached-workspace-identity"
        root.mkdir(mode=0o700)
        image = root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
        image.write_bytes(b"synthetic sparseimage identity\n")
        workspace.mkdir(mode=0o500)
        image_info = image.stat(follow_symlinks=False)
        workspace_info = workspace.stat(follow_symlinks=False)
        identity = {
            "image_path": str(image), "image_device": image_info.st_dev,
            "image_inode": image_info.st_ino, "mountpoint": str(workspace),
            "mountpoint_dir_device": workspace_info.st_dev,
            "mountpoint_dir_inode": workspace_info.st_ino,
            "device_node": "synthetic-disk", "volume_uuid": "synthetic-volume",
            "hdiutil_path": "/synthetic/hdiutil", "hdiutil_sha256": "0" * 64,
            "diskutil_path": "/synthetic/diskutil", "diskutil_sha256": "0" * 64,
            "attached": True,
        }
        with mock.patch.object(retention, "_session_hdiutil",
                               return_value=(Path("/synthetic/hdiutil"), "0" * 64)), \
                mock.patch.object(retention, "_session_diskutil",
                                  return_value=(Path("/synthetic/diskutil"), "0" * 64)), \
                mock.patch.object(retention, "_session_image_row", return_value=None):
            detached = retention._verify_session_workspace_detached(root, identity)
            self.assertFalse(detached["attached"])

        moved = root / "original-mountpoint"
        workspace.rename(moved)
        workspace.mkdir(mode=0o700)
        with mock.patch.object(retention, "_session_hdiutil",
                               return_value=(Path("/synthetic/hdiutil"), "0" * 64)), \
                mock.patch.object(retention, "_session_diskutil",
                                  return_value=(Path("/synthetic/diskutil"), "0" * 64)), \
                mock.patch.object(retention, "_session_image_row", return_value=None):
            with self.assertRaisesRegex(retention.RetentionError,
                                        "workspace or backing image identity changed"):
                retention._verify_session_workspace_detached(root, identity)

        workspace.rmdir()
        moved.rename(workspace)
        with mock.patch.object(retention, "_session_hdiutil",
                               return_value=(Path("/synthetic/hdiutil"), "0" * 64)), \
                mock.patch.object(retention, "_session_diskutil",
                                  return_value=(Path("/synthetic/diskutil"), "0" * 64)), \
                mock.patch.object(retention, "_session_image_row",
                                  return_value={"image-path": str(image), "writeable": True}):
            with self.assertRaisesRegex(retention.RetentionError, "image remains attached"):
                retention._verify_session_workspace_detached(root, identity)

        with mock.patch.object(retention, "_detach_session_workspace",
                               _ORIGINAL_DETACH_SESSION_WORKSPACE), \
                mock.patch.object(retention, "_session_image_row",
                                  return_value={"image-path": str(image), "writeable": True}):
            with self.assertRaisesRegex(retention.RetentionError,
                                        "workspace image is attached without a verified mount identity"):
                retention._detach_session_workspace(root, None)

    @_with_synthetic_buildlease_runtime_gate
    def test_failed_step_is_terminal_and_emits_no_success_receipt(self) -> None:
        lease, session = self._start_session("session-failure-terminal")
        with self.assertRaisesRegex(retention.RetentionError, "failed with exit 7"):
            self._run_session_step(session, "configure", return_code=7)
        result_path = session.session_dir / "steps/step-000.result.json"
        record, _cleanup_receipt, _failure_manifest = self._assert_failed_session_retired(lease, session)
        self.assertEqual(record["retirement_from_state"], "FAILED")
        self.assertEqual(retention.host_buildlease_p2_admission_blockers(), [])
        with self.assertRaisesRegex(retention.RetentionError, "finalized only once"):
            lease.complete()
        self.assertFalse(result_path.exists())
        self.assertFalse((session.session_dir / retention.BUILD_SESSION_TERMINAL).exists())
        with self.assertRaises(retention.RetentionError):
            session.run_next()

    @_with_synthetic_buildlease_runtime_gate
    def test_failed_session_retirement_is_idempotent_and_keeps_execution_failed(self) -> None:
        lease, session = self._start_session("session-retirement-idempotent")
        with self.assertRaisesRegex(retention.RetentionError, "failed with exit 7"):
            self._run_session_step(session, "configure", return_code=7)
        terminal = retention._load_lease(lease.build_id)
        lease_raw_before = (retention.LEASE_ROOT / f"{lease.build_id}.json").read_bytes()
        receipt_path = retention.RECEIPT_ROOT / f"{lease.build_id}.json"
        receipt_raw_before = receipt_path.read_bytes()

        receipt = retention._retire(terminal, reason="duplicate-terminal-retirement")

        after = retention._load_lease(lease.build_id)
        self.assertEqual(after["state"], "FAILED")
        self.assertEqual(after["session_v3"]["state"], "FAILED")
        self.assertEqual(after["retirement_status"], "COMPLETED")
        self.assertIs(receipt["outputs_accepted"], False)
        self.assertEqual((retention.LEASE_ROOT / f"{lease.build_id}.json").read_bytes(), lease_raw_before)
        self.assertEqual(receipt_path.read_bytes(), receipt_raw_before)

    @_with_synthetic_buildlease_runtime_gate
    def test_p2_reader_rejects_failed_retirement_receipt_claiming_accepted_outputs(self) -> None:
        lease, session = self._start_session("session-retirement-acceptance-tamper")
        with self.assertRaisesRegex(retention.RetentionError, "failed with exit 7"):
            self._run_session_step(session, "configure", return_code=7)
        receipt_path = retention.RECEIPT_ROOT / f"{lease.build_id}.json"
        receipt = retention._read_json(receipt_path, sealed=True)
        receipt["outputs_accepted"] = True
        retention._write_json(receipt_path, receipt, sealed=True)

        blockers = retention.host_buildlease_p2_admission_blockers()

        self.assertTrue(any("state is unknown" in item and lease.build_id in item for item in blockers))

    @_with_synthetic_buildlease_runtime_gate
    def test_interrupted_retirement_recovery_preserves_failed_execution_provenance(self) -> None:
        lease, session = self._start_session("session-retirement-recovery-provenance")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        original_store_lease = retention._store_lease

        def interrupt_terminal_lease_write(record: dict) -> dict:
            if (record.get("retirement_status") == "COMPLETED"
                    and record.get("retired_at_unix") is not None):
                raise retention.RetentionError("synthetic terminal lease write interruption")
            return original_store_lease(record)

        with mock.patch.object(retention, "_store_lease", side_effect=interrupt_terminal_lease_write):
            with self.assertRaisesRegex(retention.RetentionError, "terminal lease write interruption"):
                self._run_session_step(session, "configure", return_code=7)

        interrupted = retention._load_lease(lease.build_id)
        self.assertEqual(interrupted["state"], "RETIRING")
        self.assertEqual(interrupted["retirement_status"], "PENDING")
        self.assertEqual(interrupted["session_v3"]["state"], "FAILED")
        self.assertFalse(lease.path.exists())
        pending_receipt = retention._read_json(
            retention.RECEIPT_ROOT / f"{lease.build_id}.json", sealed=True)
        self.assertEqual(pending_receipt["state"], "REMOVED")
        self.assertEqual(pending_receipt["execution_outcome"], "FAILED")
        self.assertIs(pending_receipt["outputs_accepted"], False)

        receipts = retention._retire_pending_locked()

        recovered = retention._load_lease(lease.build_id)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(recovered["state"], "FAILED")
        self.assertEqual(recovered["retirement_status"], "COMPLETED")
        self.assertEqual(recovered["session_v3"]["state"], "FAILED")
        self.assertEqual(recovered["session_v3"]["failure_reason"],
                         interrupted["session_v3"]["failure_reason"])
        lease._stop_heartbeat()
        lease._release_lock()
        self.assertEqual(retention.host_buildlease_p2_admission_blockers(), [])

    @_with_synthetic_buildlease_runtime_gate
    def test_duplicate_rejected_v3_failure_cannot_replace_first_failure_reason(self) -> None:
        lease, _session = self._start_session("session-first-failure-immutable")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        record = retention._load_lease(lease.build_id)
        session_record = record["session_v3"]
        first_reason = "first terminal failure"
        first_reason_sha256 = hashlib.sha256(first_reason.encode("utf-8")).hexdigest()
        failed_at = time.time()
        session_record.update({
            "state": "FAILED", "acceptance_state": "REJECTED_NO_OUTPUT_PROMOTION",
            "failure_reason": first_reason, "failure_reason_sha256": first_reason_sha256,
            "failed_at_unix": failed_at,
        })
        record.update({
            "state": "FAILED", "session_v3": session_record,
            "failed_command": ["first-failure-command"], "return_code": 7,
            "failed_at_unix": failed_at,
        })
        retention._store_lease(record)
        lease.fail(command=["first-failure-command"], return_code=7, error=first_reason)
        lease_record_path = retention.LEASE_ROOT / f"{lease.build_id}.json"
        marker_path = lease.path / retention.BUILD_MARKER
        registry_before = lease_record_path.read_bytes()
        marker_before = marker_path.read_bytes()
        manifest_path = retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json"
        manifest_before = manifest_path.read_bytes()

        with self.assertRaisesRegex(retention.RetentionError, "first failure provenance is immutable"):
            lease.fail(command=["first-failure-command"], return_code=7, error="replacement reason")

        self.assertEqual(lease_record_path.read_bytes(), registry_before)
        self.assertEqual(marker_path.read_bytes(), marker_before)
        self.assertEqual(manifest_path.read_bytes(), manifest_before)
        self.assertEqual(retention._read_json(manifest_path)["failure_reason"], first_reason)

    @_with_synthetic_buildlease_runtime_gate
    def test_source_mutation_after_reservation_fails_closed(self) -> None:
        lease, session = self._start_session("session-source-immutable")
        self._run_session_step(session, "configure")
        source = Path(session.plan["source_identity"]["root"])
        (source / "meson.build").write_text("project('mutated')\n", encoding="utf-8")
        with self._patch_session_popen(
                side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
            with self.assertRaisesRegex(retention.RetentionError, "source tree changed"):
                session.run_next()
        popen.assert_not_called()
        self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

        mutable_lease, mutable_session = self._start_session("session-plan-immutable")
        mutable_session.plan["source_identity"]["revision"] = "0" * 40
        with self.assertRaisesRegex(retention.RetentionError, "plan changed"):
            mutable_session.run_next()
        self.assertEqual(retention._load_lease(mutable_lease.build_id)["session_v3"]["state"], "FAILED")

    def test_rejected_r5_protected_root_cannot_authorize_an_r6_session(self) -> None:
        roots = self._protected_evidence_fixture()
        r5_root = roots[retention.REJECTED_R5_BUILDROOT_EVIDENCE_ID]
        lease = self._session_lease("session-r5-source-rejected",
                                    source_identity_override={"root": str(r5_root),
                                                              "revision": "0" * 40})
        with self.assertRaisesRegex(retention.RetentionError, "source tree changed"):
            lease.reserve()
        self.assertFalse(lease.path.exists())
        self.assertFalse((retention.LEASE_ROOT / f"{lease.build_id}.json").exists())

    def test_protected_r5_bytes_remain_in_project_usage_during_session_admission(self) -> None:
        before = _ORIGINAL_MEASURE_PROJECT_USAGE()["allocated_inode_deduplicated_bytes"]
        self._protected_evidence_fixture()
        protected_usage = _ORIGINAL_MEASURE_PROJECT_USAGE()["allocated_inode_deduplicated_bytes"]
        lease = self._session_lease("session-no-r5-credit")
        original_admission = storage_policy.projected_storage_admission
        observed: list[dict[str, int]] = []

        def capture_admission(**kwargs: int) -> dict[str, object]:
            observed.append(dict(kwargs))
            return original_admission(**kwargs)

        with mock.patch.object(storage_policy, "measure_project_usage",
                               side_effect=_ORIGINAL_MEASURE_PROJECT_USAGE):
            with mock.patch.object(storage_policy, "projected_storage_admission",
                                   side_effect=capture_admission):
                lease.reserve()
        self.assertTrue(observed)
        current = observed[0]["current_scoped_bytes"]
        self.assertGreater(current, before)
        self.assertGreaterEqual(current, protected_usage)
        lease._stop_heartbeat()
        lease._release_lock()

    def test_legacy_one_shot_buildlease_run_and_receipt_remain_compatible(self) -> None:
        lease = self._lease("legacy-one-shot-compatibility")

        class SyntheticProcess:
            pid = 12346

            def wait(self, timeout: float | None = None) -> int:
                return 0

            def poll(self) -> int:
                return 0

        def legacy_popen(argv: list[str], **_kwargs: object) -> SyntheticProcess:
            self.assertEqual(argv[0], "/usr/bin/sandbox-exec")
            self.assertEqual(argv[1], "-p")
            self.assertEqual(argv[3:], ["ninja", "-C", str(lease.path)])
            (lease.path / "synthetic.dll").write_bytes(b"legacy output")
            (lease.path / "build.ninja").write_text("legacy graph\n", encoding="utf-8")
            (lease.path / ".ninja_log").write_text("legacy execution\n", encoding="utf-8")
            return SyntheticProcess()

        with self._patch_session_popen(side_effect=legacy_popen) as popen:
            with mock.patch.object(retention, "_retire", return_value={"result": "synthetic retirement"}):
                result = lease.run(["ninja", "-C", str(lease.path)])
        popen.assert_called_once()
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")
        self.assertIsNone(lease.session_plan)
        self.assertTrue((lease.path / retention.BUILD_EXECUTION_RECEIPT).is_file())
        self.assertFalse((lease.path / retention.BUILD_SESSION_DIRECTORY).exists())


    def _validate_dual_graph_plan(self, lease: retention.BuildLease,
                                  plan: dict[str, object] | None = None) -> dict[str, object]:
        selected = plan if plan is not None else lease.session_plan
        assert selected is not None
        return retention._session_validate_plan(
            selected, build_id=lease.build_id, build_path=lease.path,
            source_tree_hash=lease.source_tree_hash, expected_bytes=lease.expected_bytes)

    def _start_dual_graph_session(self, build_id: str) -> tuple[retention.BuildLease,
                                                                 retention.BuildLeaseSession]:
        lease = self._session_lease(build_id, dual_graph=True)
        lease.reserve()
        return lease, lease.start_session()

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_plan_acceptance_and_17_closed_schema_attacks(self) -> None:
        lease = self._session_lease("dual-plan-schema-valid", dual_graph=True)
        self.assertEqual(self._validate_dual_graph_plan(lease)["policy_sha256"],
                         retention.BUILD_SESSION_HOST_V3_DUAL_GRAPH_POLICY_AMENDMENT_SHA256)
        self.assertEqual(lease.session_plan["environment"]["PATH"],
                         retention.BUILD_SESSION_DUAL_GRAPH_PATH)
        path_entries = retention.BUILD_SESSION_DUAL_GRAPH_PATH.split(os.pathsep)
        self.assertEqual(path_entries[:4], ["/usr/bin", "/bin", "/usr/sbin", "/sbin"])
        self.assertGreater(path_entries.index(str(self.dual_package_bin)), 3)

        def clone() -> dict[str, object]:
            assert lease.session_plan is not None
            return json.loads(retention._canonical_json(lease.session_plan))

        attacks = []
        plan = clone()
        plan["build_graphs"].pop()
        attacks.append(("undeclared second graph", plan))
        plan = clone()
        plan["build_graphs"][1]["source_identity"]["sha256"] = "0" * 64
        attacks.append(("different graph source", plan))
        plan = clone()
        plan["build_graphs"][0]["build_directory"] = "../outside"
        attacks.append(("graph directory traversal", plan))
        plan = clone()
        plan["build_graphs"][1]["graph_id"] = plan["build_graphs"][0]["graph_id"]
        attacks.append(("duplicate graph ID", plan))
        plan = clone()
        plan["build_graphs"][0]["graph_role"] = "UNREVIEWED_ROLE"
        attacks.append(("unknown graph role", plan))
        plan = clone()
        plan["tools"]["mingw_cc"] = dict(plan["tools"]["native_cc"])
        attacks.append(("Windows role with native compiler", plan))
        plan = clone()
        plan["build_graphs"][1]["cross_file_input"] = "build-win64.txt"
        attacks.append(("native role with cross target", plan))
        plan = clone()
        plan["steps"][10]["graph_id"] = "windows-product-r6a"
        plan["steps"][10]["output_id"] = "r6a-production-path-pe"
        attacks.append(("test executes Windows PE", plan))
        plan = clone()
        plan["steps"][10]["output_id"] = "undeclared-native-output"
        attacks.append(("test references undeclared graph output", plan))
        plan = clone()
        plan["build_graphs"][0]["expected_outputs"][0]["path"] = (
            plan["build_graphs"][1]["expected_outputs"][0]["path"])
        attacks.append(("native file substituted for Windows product", plan))
        plan = clone()
        plan["steps"][3]["target_id"] = "present-accounting-production-path; touch injected"
        attacks.append(("arbitrary Ninja target injection", plan))
        plan = clone()
        plan["steps"][3]["argv"] = ["sh", "-c", "ninja; touch injected"]
        attacks.append(("shell syntax injection", plan))
        plan = clone()
        plan["policy_sha256"] = retention.BUILD_SESSION_HOST_V3_LEGACY_POLICY_AMENDMENT_SHA256
        attacks.append(("old policy digest with dual graph fields", plan))
        shadow = self.repo / "shadow-tools"
        shadow.mkdir(mode=0o700)
        (shadow / "touch").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (shadow / "touch").chmod(0o700)
        plan = clone()
        plan["environment"]["PATH"] = str(shadow) + os.pathsep + plan["environment"]["PATH"]
        attacks.append(("caller PATH shadows a pinned system utility", plan))
        plan = clone()
        plan["tools"].pop("mingw_strip")
        attacks.append(("missing Windows strip receipt", plan))
        plan = clone()
        plan["tools"]["mingw_strip"] = dict(plan["tools"]["mingw_linker"])
        attacks.append(("Windows strip substituted with linker", plan))
        plan = clone()
        plan["environment"]["MACOSX_DEPLOYMENT_TARGET"] = "14.0"
        attacks.append(("caller deployment target overrides native SDK policy", plan))

        self.assertEqual(len(attacks), 17)
        for label, attacked in attacks:
            with self.subTest(case=label), self.assertRaises(retention.RetentionError):
                self._validate_dual_graph_plan(lease, attacked)

    def test_windows_product_stage_rejects_every_macho_magic_variant(self) -> None:
        workspace = self.repo / "synthetic-windows-stage-macho"
        product = workspace / "stage/usr/share/product.dat"
        product.parent.mkdir(parents=True)
        session_stub = SimpleNamespace(workspace=workspace, plan={"stage_root": "stage"})
        for magic in sorted(retention._MACHO_MAGIC_PREFIXES):
            product.write_bytes(magic + (b"\x00" * 32))
            with self.subTest(magic=magic.hex()), self.assertRaisesRegex(
                    retention.RetentionError, "native Mach-O artifact"):
                retention.BuildLeaseSession._stage_install_snapshot(session_stub)

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_path_shadow_after_session_creation_fails_before_dispatch(self) -> None:
        lease, session = self._start_dual_graph_session("dual-path-shadow-runtime")
        shadow = self.repo / "runtime-shadow-tools"
        shadow.mkdir(mode=0o700)
        for name in ("touch", "python3"):
            executable = shadow / name
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(0o700)
        session.plan["environment"]["PATH"] = (
            str(shadow) + os.pathsep + retention.BUILD_SESSION_DUAL_GRAPH_PATH)
        with self.assertRaisesRegex(retention.RetentionError, "controller-owned closed search path"):
            lease._validate_session_inputs()

    def test_dual_graph_unreceipted_optional_programs_must_remain_absent(self) -> None:
        for command_name in retention.BUILD_SESSION_DUAL_GRAPH_PATH_ABSENT_EXECUTABLES:
            with self.subTest(command=command_name):
                lease = self._session_lease(f"dual-unreceipted-{command_name}", dual_graph=True)
                original_which = retention.shutil.which

                def shadowed_which(name: str, *args: object, **kwargs: object) -> str | None:
                    if name == command_name:
                        return str(self.repo / "unreceipted" / command_name)
                    return original_which(name, *args, **kwargs)

                with mock.patch.object(retention.shutil, "which", side_effect=shadowed_which):
                    with self.assertRaisesRegex(
                            retention.RetentionError,
                            f"unreceipted executable {command_name}"):
                        lease._validate_session_inputs()

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_native_test_labels_are_bound_to_exact_outputs(self) -> None:
        lease = self._session_lease("dual-test-label-binding", dual_graph=True)
        assert lease.session_plan is not None
        for index, wrong_class in ((11, "MODEL"), (12, "UNIT")):
            attacked = json.loads(retention._canonical_json(lease.session_plan))
            attacked["steps"][index]["test_class"] = wrong_class
            with self.subTest(step=index), self.assertRaises(retention.RetentionError):
                self._validate_dual_graph_plan(lease, attacked)

    def test_legacy_single_graph_plan_remains_valid_under_additive_policy(self) -> None:
        lease = self._session_lease("dual-plan-legacy-preserved")
        legacy_plan = json.loads(retention._canonical_json(lease.session_plan or {}))
        legacy_plan["environment"]["MACOSX_DEPLOYMENT_TARGET"] = "11.0"
        validated = retention._session_validate_plan(
            legacy_plan, build_id=lease.build_id, build_path=lease.path,
            source_tree_hash=lease.source_tree_hash, expected_bytes=lease.expected_bytes)
        self.assertNotIn("dual_build_graphs", validated)
        self.assertEqual(validated["environment"]["MACOSX_DEPLOYMENT_TARGET"], "11.0")
        self.assertEqual([step["kind"] for step in validated["steps"]],
                         ["configure", "build", "install", "test", "source_verification"])

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_source_mutations_between_configure_and_build_fail_closed(self) -> None:
        lease, session = self._start_dual_graph_session("dual-source-between-configs")
        self._run_session_step(session, "configure")
        source = Path(session.plan["source_identity"]["root"])
        (source / "meson.build").write_text("project('mutated-between-configs')\n", encoding="utf-8")
        with self._patch_session_popen(side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
            with self.assertRaisesRegex(retention.RetentionError, "source tree changed"):
                session.run_next()
        popen.assert_not_called()
        self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

        lease, session = self._start_dual_graph_session("dual-source-between-builds")
        self._run_session_step(session, "configure")
        self._run_session_step(session, "configure")
        self._run_session_step(session, "build")
        source = Path(session.plan["source_identity"]["root"])
        (source / "meson.build").write_text("project('mutated-between-builds')\n", encoding="utf-8")
        with self._patch_session_popen(side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
            with self.assertRaisesRegex(retention.RetentionError, "source tree changed"):
                session.run_next()
        popen.assert_not_called()
        self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_product_and_native_output_mutations_are_rejected(self) -> None:
        for build_id, changed_output_id in (
                ("dual-pe-output-mutated", "r6a-production-path-pe"),
                ("dual-native-output-mutated", "r6a-production-path-macho")):
            with self.subTest(output=changed_output_id):
                lease, session = self._start_dual_graph_session(build_id)
                self._run_session_step(session, "configure")
                self._run_session_step(session, "configure")
                self._run_session_step(session, "build")
                self._run_session_step(session, "build")
                if changed_output_id == "r6a-production-path-macho":
                    self._run_session_step(session, "build")
                output = next(row for graph in session.plan["build_graphs"]
                              for row in graph["expected_outputs"]
                              if row["output_id"] == changed_output_id)
                graph = next(graph for graph in session.plan["build_graphs"]
                             if output in graph["expected_outputs"])
                output_path = session.workspace / graph["build_directory"] / output["path"]
                output_path.write_bytes(output_path.read_bytes() + b"mutated")
                with self._patch_session_popen(
                        side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                    with self.assertRaisesRegex(retention.RetentionError, "graph output changed"):
                        session.run_next()
                popen.assert_not_called()
                self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_runtime_and_tool_mutations_fail_before_dispatch(self) -> None:
        for build_id, mutate in (
                ("dual-runtime-input-mutated", "runtime"),
                ("dual-native-tool-mutated", "tool"),
                ("dual-windows-strip-mutated", "strip"),
                ("dual-host-pkgconfig-mutated", "host_dependency"),
                ("dual-xcode-compiler-mutated", "xcode_tool")):
            with self.subTest(input=mutate):
                lease, session = self._start_dual_graph_session(build_id)
                self._run_session_step(session, "configure")
                if mutate == "runtime":
                    target = Path(session.plan["runtime_receipts"][0]["path"])
                elif mutate == "host_dependency":
                    receipt = next(row for row in session.plan["host_dependency_receipts"]
                                   if row["input_id"] == "SDL2_PKGCONFIG")
                    target = Path(receipt["path"])
                elif mutate == "strip":
                    target = Path(session.plan["tools"]["mingw_strip"]["path"])
                elif mutate == "xcode_tool":
                    receipt = next(row for row in session.plan["host_dependency_receipts"]
                                   if row["input_id"] == "XCODE_CLANG")
                    target = Path(receipt["path"])
                else:
                    target = Path(session.plan["tools"]["native_glslang"]["path"])
                if target.stat().st_mode & 0o222 == 0:
                    target.chmod(0o700)
                target.write_bytes(target.read_bytes() + b"changed")
                with self._patch_session_popen(
                        side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                    with self.assertRaisesRegex(retention.RetentionError, "changed"):
                        session.run_next()
                popen.assert_not_called()
                self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_mingw_support_helpers_are_revalidated_before_dispatch(self) -> None:
        for build_id, helper in (
                ("dual-mingw-cc1-mutated", "cc1"),
                ("dual-mingw-cc1plus-mutated", "cc1plus"),
                ("dual-mingw-collect2-mutated", "collect2")):
            with self.subTest(helper=helper):
                lease, session = self._start_dual_graph_session(build_id)
                self._run_session_step(session, "configure")
                target = (self.synthetic_mingw_root
                          / "libexec/gcc/x86_64-w64-mingw32/16.2.0" / helper)
                target.write_bytes(target.read_bytes() + b"mutated helper\n")
                with self._patch_session_popen(
                        side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                    with self.assertRaisesRegex(retention.RetentionError, "changed from its admitted identity"):
                        session.run_next()
                popen.assert_not_called()
                self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_mingw_support_tree_is_revalidated_after_operation(self) -> None:
        lease, session = self._start_dual_graph_session("dual-mingw-helper-mutated-during-step")
        self._run_session_step(session, "configure")
        helper = (self.synthetic_mingw_root
                  / "libexec/gcc/x86_64-w64-mingw32/16.2.0/cc1plus")

        def mutate_during_operation(*_args: object, **_kwargs: object) -> SimpleNamespace:
            process = self._session_popen(session)
            helper.write_bytes(helper.read_bytes() + b"mutated while step ran\n")
            return process

        with self._patch_session_popen(side_effect=mutate_during_operation) as popen:
            with self.assertRaisesRegex(
                    retention.RetentionError, "changed during session operation|changed from its admitted identity"):
                session.run_next()
        popen.assert_called_once()
        record = retention._load_lease(lease.build_id)["session_v3"]
        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["next_step_index"], 1)

    @_with_synthetic_buildlease_runtime_gate
    def test_sdl2_link_alias_substitution_and_target_mutation_fail_before_dispatch(self) -> None:
        for build_id, mutation in (
                ("dual-sdl2-link-alias-substituted", "alias"),
                ("dual-sdl2-link-target-mutated", "target"),
                ("dual-sdl2-unreceipted-alias-added", "extra_alias")):
            with self.subTest(mutation=mutation):
                lease, session = self._start_dual_graph_session(build_id)
                self._run_session_step(session, "configure")
                if mutation == "alias":
                    receipt = next(row for row in session.plan["host_dependency_receipts"]
                                   if row["input_id"] == "SDL2_LIB_ALIAS")
                    alias = Path(receipt["path"])
                    target = Path(next(row["path"] for row in session.plan["host_dependency_receipts"]
                                       if row["input_id"] == "SDL2_TEST_ARCHIVE"))
                    alias.unlink()
                    alias.symlink_to(os.path.relpath(target, alias.parent))
                elif mutation == "target":
                    receipt = next(row for row in session.plan["host_dependency_receipts"]
                                   if row["input_id"] == "SDL2MAIN_ARCHIVE")
                    target = Path(receipt["path"])
                    target.write_bytes(target.read_bytes() + b"mutated linker target\n")
                else:
                    alias_dir = Path(next(row["path"] for row in session.plan["host_dependency_receipts"]
                                          if row["input_id"] == "SDL2_LIB_ALIAS")).parent
                    (alias_dir / "libSDL2_unreceipted.a").write_bytes(b"unreceipted alias\n")
                with self._patch_session_popen(
                        side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                    with self.assertRaisesRegex(
                            retention.RetentionError,
                            "pinned linker alias|admitted identity|changed|substituted"):
                        session.run_next()
                popen.assert_not_called()
                self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    def test_tree_hash_requires_internal_resolving_symlinks_for_mingw_closure(self) -> None:
        root = self.repo / "mingw-tree-symlink-guard"
        root.mkdir(mode=0o700)
        (root / "helper").write_bytes(b"helper\n")
        (root / "alias").symlink_to("helper")
        digest, count = retention._tree_hash(
            root, reject_external_hardlinks=True, require_internal_symlinks=True)
        self.assertEqual(count, 1)
        (root / "helper").write_bytes(b"changed helper\n")
        self.assertNotEqual(digest, retention._tree_hash(
            root, reject_external_hardlinks=True, require_internal_symlinks=True)[0])
        git_metadata = root / ".git"
        git_metadata.mkdir(mode=0o700)
        (git_metadata / "config").write_bytes(b"synthetic metadata\n")
        git_included_digest, git_included_count = retention._tree_hash(
            root, reject_external_hardlinks=True, require_internal_symlinks=True,
            include_root_git=True)
        self.assertEqual(git_included_count, 2)
        self.assertNotEqual(git_included_digest, retention._tree_hash(
            root, reject_external_hardlinks=True, require_internal_symlinks=True)[0])
        outside = self.repo / "outside-mingw-target"
        outside.write_bytes(b"outside\n")
        (root / "alias").unlink()
        (root / "alias").symlink_to(outside)
        with self.assertRaisesRegex(retention.RetentionError, "escapes or changed"):
            retention._tree_hash(root, reject_external_hardlinks=True,
                                  require_internal_symlinks=True)

    @_with_synthetic_buildlease_runtime_gate
    def test_native_toolchain_selection_probe_is_scrubbed_and_exactly_pinned(self) -> None:
        lease = self._session_lease("dual-native-selection-probe", dual_graph=True)
        plan = self._validate_dual_graph_plan(lease)
        dependency_rows, _profile, _selection = retention._session_host_dependency_identities(plan)
        dependencies = {row["input_id"]: row for row in dependency_rows}
        host_profile = plan["native_host_profile"]
        base_environment = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": "/var/empty",
            "LANG": "C",
            "LC_ALL": "C",
        }

        def probe_result(*, selector: str | None = None,
                         selected_overrides: dict[str, str] | None = None):
            selected_overrides = selected_overrides or {}

            def run(command: list[str], **kwargs: object) -> SimpleNamespace:
                if command[0] == dependencies["XCODE_SELECT"]["path"]:
                    output = selector or host_profile["developer_directory"]
                elif command[0] == dependencies["XCRUN"]["path"]:
                    if command[1:2] == ["--find"]:
                        tool_name = command[2]
                        output = selected_overrides.get(
                            tool_name, host_profile["xcrun_selected_tool_paths"][tool_name])
                    elif command[-1:] == ["--show-sdk-path"]:
                        output = host_profile["sdk_path"]
                    elif command[-1:] == ["--show-sdk-version"]:
                        output = host_profile["sdk_version"]
                    else:
                        raise AssertionError(f"unexpected synthetic xcrun arguments: {command[1:]}")
                else:
                    raise AssertionError(f"probe used an unpinned executable: {command[0]}")
                return SimpleNamespace(returncode=0, stdout=output + "\n", stderr="")

            return run

        hostile_overrides = {
            "DEVELOPER_DIR": "/user/selected/Developer",
            "SDKROOT": "/user/selected/SDK",
            "TOOLCHAINS": "com.user.toolchain",
        }
        # Keep the global synthetic identity boundary in place for every other
        # session test, but exercise the real probe implementation below.
        self.native_toolchain_capture_patch.stop()
        with mock.patch.dict(os.environ, hostile_overrides, clear=False):
            with mock.patch.object(retention.subprocess, "run", side_effect=probe_result()) as run:
                observation = retention._capture_session_native_toolchain_selection(plan, dependencies)
        self.assertEqual(run.call_count, 9)
        for call in run.call_args_list:
            command = call.args[0]
            environment = call.kwargs["env"]
            self.assertEqual(command[0], dependencies[
                "XCODE_SELECT" if command[0] == dependencies["XCODE_SELECT"]["path"]
                else "XCRUN"]["path"])
            self.assertEqual(call.kwargs["timeout"], 15)
            self.assertEqual(environment["PATH"], base_environment["PATH"])
            self.assertNotIn("TOOLCHAINS", environment)
            if command[0] == dependencies["XCODE_SELECT"]["path"]:
                self.assertNotIn("DEVELOPER_DIR", environment)
                self.assertNotIn("SDKROOT", environment)
                self.assertEqual(environment, base_environment)
            else:
                self.assertEqual(environment, {
                    **base_environment,
                    "DEVELOPER_DIR": host_profile["developer_directory"],
                    "SDKROOT": host_profile["sdk_resolved_path"],
                })
        self.assertEqual(retention._validate_session_native_toolchain_selection(
            plan, dependencies, observation), observation)

        with mock.patch.object(retention.subprocess, "run",
                               side_effect=probe_result(selector="/other/Xcode/Contents/Developer")):
            with self.assertRaisesRegex(retention.RetentionError, "active xcode-select developer directory"):
                retention._capture_session_native_toolchain_selection(plan, dependencies)

        copied_clang = self.repo / "same-hash-but-wrong-xcrun-clang"
        copied_clang.write_bytes(Path(dependencies["XCODE_CLANG"]["path"]).read_bytes())
        copied_clang.chmod(0o700)
        with mock.patch.object(retention.subprocess, "run", side_effect=probe_result(
                selected_overrides={"clang": str(copied_clang)})):
            wrong_path = retention._capture_session_native_toolchain_selection(plan, dependencies)
        with self.assertRaisesRegex(retention.RetentionError, "xcrun-selected native clang"):
            retention._validate_session_native_toolchain_selection(plan, dependencies, wrong_path)

        clang_path = Path(dependencies["XCODE_CLANG"]["path"])
        original_clang = clang_path.read_bytes()
        original_mode = stat.S_IMODE(clang_path.stat().st_mode)
        try:
            clang_path.chmod(0o700)
            clang_path.write_bytes(original_clang + b"mutated synthetic selected compiler\n")
            with mock.patch.object(retention.subprocess, "run", side_effect=probe_result()):
                with self.assertRaisesRegex(retention.RetentionError,
                                            "xcrun-selected native clang input changed"):
                    retention._capture_session_native_toolchain_selection(plan, dependencies)
        finally:
            clang_path.write_bytes(original_clang)
            clang_path.chmod(original_mode)

        wrong_hash = json.loads(retention._canonical_json(observation))
        wrong_hash["selected_tools"]["clang"]["sha256"] = "0" * 64
        with self.assertRaisesRegex(retention.RetentionError, "xcrun-selected native clang"):
            retention._validate_session_native_toolchain_selection(plan, dependencies, wrong_hash)

    @_with_synthetic_buildlease_runtime_gate
    def test_native_toolchain_selection_drift_after_admission_fails_before_dispatch(self) -> None:
        cases = (
            ("selector", lambda value: value.update(
                active_developer_directory="/user/selected/Developer")),
            ("xcrun-path", lambda value: value["selected_tools"]["clang"].update(
                reported_path="/unapproved/Toolchains/clang")),
            ("xcrun-hash", lambda value: value["selected_tools"]["clang"].update(
                sha256="0" * 64)),
        )
        for label, mutate in cases:
            with self.subTest(observation=label):
                lease, session = self._start_dual_graph_session(f"dual-selection-drift-{label}")
                baseline = self._synthetic_native_toolchain_selection(
                    session.plan, {
                        row["input_id"]: row
                        for row in session.plan["host_dependency_receipts"]
                    })
                changed = json.loads(retention._canonical_json(baseline))
                mutate(changed)
                with mock.patch.object(retention, "_capture_session_native_toolchain_selection",
                                       return_value=changed):
                    with self._patch_session_popen(
                            side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                        with self.assertRaises(retention.RetentionError):
                            session.run_next()
                popen.assert_not_called()
                self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_native_toolchain_selection_drift_during_step_is_revalidated(self) -> None:
        lease, session = self._start_dual_graph_session("dual-selection-drift-during-step")
        baseline = self._synthetic_native_toolchain_selection(
            session.plan, {row["input_id"]: row for row in session.plan["host_dependency_receipts"]})
        changed = json.loads(retention._canonical_json(baseline))
        changed["sdk_version"] = "unapproved-sdk-version"
        dispatched = False

        def capture(_plan: dict, _dependencies: dict) -> dict:
            return changed if dispatched else baseline

        def dispatch(*_args: object, **_kwargs: object) -> object:
            nonlocal dispatched
            dispatched = True
            return self._session_popen(session)

        with mock.patch.object(retention, "_capture_session_native_toolchain_selection",
                               side_effect=capture):
            with self._patch_session_popen(side_effect=dispatch) as popen:
                with self.assertRaisesRegex(retention.RetentionError, "Xcode/SDK selection"):
                    session.run_next()
        popen.assert_called_once()
        self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_native_load_command_must_match_the_staged_runtime_closure(self) -> None:
        lease, session = self._start_dual_graph_session("dual-native-unknown-load")
        for _ in range(2):
            self._run_session_step(session, "configure")
        for _ in range(3):
            self._run_session_step(session, "build")
        graph = retention._session_dual_graph_by_id(session.plan, "native-verification-r6a")
        output = next(row for row in graph["expected_outputs"]
                      if row["output_id"] == "r6a-production-path-macho")
        output_path = session.workspace / graph["build_directory"] / output["path"]
        output_path.write_bytes(self._tiny_macho(2, load_paths=("@rpath/libnot-admitted.dylib",)))
        output_path.chmod(0o700)
        with self._patch_session_popen(
                side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
            with self.assertRaisesRegex(retention.RetentionError,
                                        "outside the admitted system/runtime closure"):
                session.run_next()
        popen.assert_not_called()
        self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_native_test_output_mutation_during_execution_fails_closed(self) -> None:
        lease, session = self._start_dual_graph_session("dual-native-output-mutated-during-test")
        while retention._load_lease(lease.build_id)["session_v3"]["next_step_index"] < 10:
            index = retention._load_lease(lease.build_id)["session_v3"]["next_step_index"]
            step = session.plan["steps"][index]
            if step["kind"] in {"stage_windows_product", "stage_native_runtime"}:
                session.run_next()
            else:
                self._run_session_step(session, step["kind"])
        graph = retention._session_dual_graph_by_id(session.plan, "native-verification-r6a")
        output = next(row for row in graph["expected_outputs"]
                      if row["output_id"] == "r6a-production-path-macho")
        output_path = session.workspace / graph["build_directory"] / output["path"]

        def mutate_after_start(*_args: object, **_kwargs: object) -> object:
            result = self._session_popen(session)
            output_path.write_bytes(output_path.read_bytes() + b"mutated while test ran")
            return result

        with self._patch_session_popen(side_effect=mutate_after_start) as popen:
            with self.assertRaisesRegex(retention.RetentionError,
                                        "native test output differs from its declared graph producer identity"):
                session.run_next()
        popen.assert_called_once()
        self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")
        self.assertFalse((session.session_dir / retention.BUILD_SESSION_TERMINAL).exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_build_and_runtime_test_failures_prevent_acceptance(self) -> None:
        cases = (
            ("dual-default-graph-failure", 2),
            ("dual-windows-link-failure", 3),
            ("dual-native-unit-build-failure", 5),
            ("dual-native-model-build-failure", 6),
            ("dual-native-production-test-failure", 10),
            ("dual-native-unit-test-failure", 11),
            ("dual-native-model-test-failure", 12),
        )
        for build_id, failure_index in cases:
            with self.subTest(step=failure_index):
                lease, session = self._start_dual_graph_session(build_id)
                while retention._load_lease(lease.build_id)["session_v3"]["next_step_index"] < failure_index:
                    index = retention._load_lease(lease.build_id)["session_v3"]["next_step_index"]
                    current = session.plan["steps"][index]
                    if current["kind"] in {"stage_windows_product", "stage_native_runtime"}:
                        session.run_next()
                    else:
                        self._run_session_step(session, current["kind"])
                current = session.plan["steps"][failure_index]
                with self.assertRaisesRegex(retention.RetentionError, "failed with exit 17"):
                    self._run_session_step(session, current["kind"], return_code=17)
                record = retention._load_lease(lease.build_id)
                self.assertEqual(record["retirement_from_state"], "FAILED")
                self.assertEqual(record["session_v3"]["failed_step_index"], failure_index)
                self.assertFalse((session.session_dir / retention.BUILD_SESSION_TERMINAL).exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_staged_runtime_mutation_fails_before_native_test_dispatch(self) -> None:
        for build_id, target_test_index in (
                ("dual-staged-runtime-mutated-before-production", 10),
                ("dual-staged-runtime-mutated-before-unit", 11)):
            with self.subTest(before_test=target_test_index):
                lease, session = self._start_dual_graph_session(build_id)
                while retention._load_lease(lease.build_id)["session_v3"]["next_step_index"] \
                        < target_test_index:
                    index = retention._load_lease(lease.build_id)["session_v3"]["next_step_index"]
                    step = session.plan["steps"][index]
                    if step["kind"] in {"stage_windows_product", "stage_native_runtime"}:
                        session.run_next()
                    else:
                        self._run_session_step(session, step["kind"])
                staged = (session.workspace / retention.BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE
                          / retention.BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES["SDL2_COMPAT"])
                staged.chmod(0o700)
                staged.write_bytes(staged.read_bytes() + b"mutated after stage receipt")
                with self._patch_session_popen(
                        side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                    with self.assertRaisesRegex(retention.RetentionError, "declared stage tree changed"):
                        session.run_next()
                popen.assert_not_called()
                self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "FAILED")

    @_with_synthetic_buildlease_runtime_gate
    def test_dual_graph_native_test_uses_staged_runtime_and_terminal_binds_both_graphs(self) -> None:
        lease, session = self._start_dual_graph_session("dual-success-terminal")
        native_configure = next(step for step in session.plan["steps"]
                                if step.get("kind") == "configure"
                                and step.get("graph_id") == "native-verification-r6a")
        session.plan["environment"]["MACOSX_DEPLOYMENT_TARGET"] = "14.0"
        self.assertNotIn("MACOSX_DEPLOYMENT_TARGET",
                         session._environment_for_step(native_configure, session))
        session.plan["environment"].pop("MACOSX_DEPLOYMENT_TARGET")
        for index, step in enumerate(session.plan["steps"][:-1]):
            if step["kind"] in {"stage_windows_product", "stage_native_runtime"}:
                session.run_next()
            elif step["kind"] == "test":
                with self._patch_session_popen(
                        side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                    session.run_next()
                effective_environment = popen.call_args.kwargs["env"]
                self.assertEqual(effective_environment["PATH"],
                                 retention.BUILD_SESSION_DUAL_GRAPH_PATH)
                command = retention._read_json(
                    session.session_dir / f"steps/step-{index:03d}.command.json", sealed=True)
                self.assertEqual(command["graph_provenance"]["graph_role"], "NATIVE_VERIFICATION")
                self.assertEqual(command["graph_provenance"]["test_class"], step["test_class"])
                if step["test_class"] == "PRODUCTION_PATH":
                    expected_loader = str(
                        session.workspace / retention.BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE)
                    self.assertEqual(effective_environment["DYLD_LIBRARY_PATH"], expected_loader)
                    self.assertEqual(effective_environment["DXVK_WSI_DRIVER"], "SDL2")
                    self.assertEqual(command["native_loader_provenance"]["dyld_resolution_verified"], False)
                    bindings = command["native_loader_provenance"][
                        "output_macho_dependency_bindings"]
                    self.assertEqual(len(bindings), 1)
                    self.assertEqual(bindings[0]["runtime_id"], "SDL2_COMPAT")
                    self.assertEqual(bindings[0]["staged_path"],
                                     (retention.BUILD_SESSION_DUAL_GRAPH_NATIVE_RUNTIME_RELATIVE
                                      / retention.BUILD_SESSION_DUAL_GRAPH_RUNTIME_NAMES[
                                          "SDL2_COMPAT"]).as_posix())
                    self.assertEqual(bindings[0]["sha256"],
                                     retention.BUILD_SESSION_DUAL_GRAPH_RUNTIME_PINS[
                                         "SDL2_COMPAT"]["sha256"])
                else:
                    self.assertEqual(effective_environment["DEVELOPER_DIR"],
                                     session.plan["native_host_profile"]["developer_directory"])
                    self.assertEqual(effective_environment["SDKROOT"],
                                     session.plan["native_host_profile"]["sdk_resolved_path"])
                    self.assertNotIn("DYLD_LIBRARY_PATH", effective_environment)
                    self.assertNotIn("DXVK_WSI_DRIVER", effective_environment)
                    self.assertNotIn("native_loader_provenance", command)
                self.assertNotIn("MACOSX_DEPLOYMENT_TARGET", effective_environment)
            elif step["kind"] in {"configure", "build", "install"}:
                with self._patch_session_popen(
                        side_effect=lambda *_args, **_kwargs: self._session_popen(session)) as popen:
                    session.run_next()
                effective_environment = popen.call_args.kwargs["env"]
                self.assertNotIn("MACOSX_DEPLOYMENT_TARGET", effective_environment)
                if step["graph_id"] == "native-verification-r6a":
                    self.assertEqual(effective_environment["DEVELOPER_DIR"],
                                     session.plan["native_host_profile"]["developer_directory"])
                    self.assertEqual(effective_environment["SDKROOT"],
                                     session.plan["native_host_profile"]["sdk_resolved_path"])
                else:
                    self.assertNotIn("DEVELOPER_DIR", effective_environment)
                    self.assertNotIn("SDKROOT", effective_environment)
            else:
                self._run_session_step(session, step["kind"])
        session.verify_source()
        session.finalize()
        terminal_path = (retention.MANIFEST_ROOT / f"{lease.build_id}.evidence"
                         / "execution/session-terminal.json")
        terminal = retention._read_json(terminal_path, sealed=True)
        self.assertEqual({row["graph_id"] for row in terminal["graph_output_identities"]},
                         {"windows-product-r6a", "native-verification-r6a"})
        install_identity = terminal["stage_operation_identity"]["stage_install_tree_identity"]
        self.assertEqual(install_identity["graph_id"], "windows-product-r6a")
        self.assertEqual(install_identity["graph_role"], "WINDOWS_PRODUCT")
        self.assertEqual(install_identity["producer_target_id"], "DEFAULT_PRODUCTION_OUTPUTS")
        self.assertEqual(install_identity["identity_boundary"],
                         "FIXED_WINDOWS_INSTALL_GRAPH_AND_PER_FILE_HASH_FORMAT_RECEIPTS")
        self.assertEqual([row["path"] for row in install_identity["files"]],
                         ["share/synthetic/product.dat"])
        self.assertEqual(install_identity["files"][0]["graph_id"], "windows-product-r6a")
        self.assertEqual(install_identity["files"][0]["graph_role"], "WINDOWS_PRODUCT")
        self.assertEqual(install_identity["files"][0]["producer_target_id"],
                         "DEFAULT_PRODUCTION_OUTPUTS")
        self.assertEqual(install_identity["files"][0]["file_identity"]["sha256"],
                         hashlib.sha256(b"synthetic Windows product output\n").hexdigest())
        self.assertFalse(terminal["detached_workspace_identity"]["attached"])
        self.assertTrue(terminal["readonly_mount_identity"]["readonly"])

    @contextmanager
    def _isolated_orphan_namespace(self):
        with tempfile.TemporaryDirectory(prefix="fgmetal-orphan-case-", dir="/private/tmp") as name:
            root = Path(name)
            build_root = root / "builds"
            lease_root = root / "storage/build-leases"
            manifest_root = root / "storage/build-manifests"
            receipt_root = root / "storage/build-cleanup-receipts"
            for directory in (build_root, lease_root, manifest_root, receipt_root):
                directory.mkdir(parents=True, mode=0o700)
            with (mock.patch.object(retention, "BUILD_ROOT", build_root),
                  mock.patch.object(retention, "LEASE_ROOT", lease_root),
                  mock.patch.object(retention, "MANIFEST_ROOT", manifest_root),
                  mock.patch.object(retention, "RECEIPT_ROOT", receipt_root)):
                yield

    @staticmethod
    def _absent_process_id() -> int:
        candidate = max(1_000_000, os.getpid() + 500_000)
        for _ in range(10_000):
            try:
                os.kill(candidate, 0)
            except ProcessLookupError:
                return candidate
            except OSError as error:
                raise AssertionError(f"could not prove synthetic owner PID {candidate} absent") from error
            candidate += 1
        raise AssertionError("could not find a process ID proven absent for orphan fixture")

    def _orphan_fixture(self, label: str, *, owner_experiment: str = "synthetic-test",
                        legacy_session: bool = True, exact_r23_identity: bool = False,
                        keep_lock: bool = False, owner_pid_mode: str = "dead") -> tuple[retention.BuildLease, dict, dict]:
        build_id = re.sub(r"[^A-Za-z0-9._-]", "-", label)[:100]
        lease = self._session_lease(build_id, owner_experiment=owner_experiment)
        self.assertEqual(lease.reserve(), lease.path)
        if not keep_lock:
            lease._stop_heartbeat()
            lease._release_lock()
        record = retention._load_lease(build_id)
        if not keep_lock:
            if owner_pid_mode == "dead":
                record["owner_pid"] = self._absent_process_id()
            elif owner_pid_mode == "live":
                record["owner_pid"] = os.getpid()
            else:
                raise ValueError(f"unknown orphan fixture owner mode: {owner_pid_mode}")
        if legacy_session:
            record.pop("session_started", None)
            record.pop("session_started_at_unix", None)
        if exact_r23_identity:
            lease.expected_bytes = 5_598_724_098
            record["source_tree_hash"] = (
                "df7e631dfa2b2e1633e2a204aa4669cfda7536c5fe980cb975d5cd16471b9700")
            record["expected_bytes"] = 5_598_724_098
            record["estimated_bytes"] = 5_598_724_098
            record["owner_pid"] = 10793
            record["session_v3"]["plan_sha256"] = (
                "507ef8142396f8cfa0feaa4c9abf924d7b2752e0d2a6d70abce23d774ab6a7bb")
            record["session_v3"]["admission_identity"] = {
                "admission_id": "R6-A-R23-DUAL-LINEAGE-20261020T214331Z",
                "sha256": "184cc4776f4aac667e1afc4e5576f1a78d58d0c48547489d6380baa6290e2903"}
        retention._store_lease(record)
        root_fd = retention._open_dir(lease.path)
        try:
            retention._write_marker(root_fd, record)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        lease_fd = retention._open_dir(retention.LEASE_ROOT)
        try:
            raw = retention._read_at(lease_fd, f"{build_id}.json", limit=1024 * 1024)
        finally:
            os.close(lease_fd)
        session = record["session_v3"]
        args = {
            "build_id": build_id,
            "expected_path": lease.path,
            "expected_owner_experiment": owner_experiment,
            "expected_admission_identity": dict(session["admission_identity"]),
            "expected_plan_sha256": session["plan_sha256"],
            "expected_record_sha256": hashlib.sha256(raw).hexdigest(),
        }
        return lease, record, args

    @staticmethod
    def _orphan_tree_signature(root: Path) -> tuple[tuple[str, str, bytes | str | None], ...]:
        rows = []
        for path in sorted(root.rglob("*")):
            info = path.lstat()
            relative = path.relative_to(root).as_posix()
            if stat.S_ISLNK(info.st_mode):
                rows.append((relative, "symlink", os.readlink(path)))
            elif stat.S_ISDIR(info.st_mode):
                rows.append((relative, "directory", None))
            elif stat.S_ISREG(info.st_mode):
                rows.append((relative, "file", path.read_bytes()))
            else:
                rows.append((relative, "other", None))
        return tuple(rows)

    def _orphan_closeout_state(self, lease: retention.BuildLease, args: dict) -> dict:
        lease_fd = retention._open_dir(retention.LEASE_ROOT)
        try:
            registry = retention._read_at(lease_fd, f"{args['build_id']}.json")
        finally:
            os.close(lease_fd)
        root_fd = retention._open_dir(lease.path)
        try:
            marker = retention._read_at(root_fd, retention.BUILD_MARKER)
        finally:
            os.close(root_fd)
        artifacts = {}
        for path in (retention._orphan_closeout_receipt_path(args["build_id"]),
                     retention.RECEIPT_ROOT / f"{args['build_id']}.json",
                     retention.MANIFEST_ROOT / f"{args['build_id']}.json",
                     retention.MANIFEST_ROOT / f"{args['build_id']}.failure"):
            artifacts[str(path)] = (self._orphan_tree_signature(path) if path.is_dir()
                                    else path.read_bytes() if path.is_file() else None)
        snapshot = retention._snapshot(protected_roots=[])
        active_rows = [row for row in snapshot["active"] if row["build_id"] == args["build_id"]]
        charge = (len(active_rows), active_rows[0].get("estimated_bytes") if active_rows else None)
        return {"registry": registry, "marker": marker,
                "build_tree": self._orphan_tree_signature(lease.path),
                "artifacts": artifacts, "charge": charge}

    def _assert_orphan_closeout_rejected_without_mutation(
            self, lease: retention.BuildLease, args: dict, *, state_args: dict | None = None) -> None:
        state_args = state_args or args
        before = self._orphan_closeout_state(lease, state_args)
        with mock.patch.object(retention, "_retire_pending_locked",
                               side_effect=AssertionError("orphan closeout entered session recovery")) as recovery:
            with self.assertRaises(retention.RetentionError):
                self._close_orphan_fixture(args)
        recovery.assert_not_called()
        self.assertEqual(self._orphan_closeout_state(lease, state_args), before)

    @staticmethod
    def _write_orphan_record_and_marker(record: dict) -> bytes:
        retention._store_lease(record)
        root_fd = retention._open_dir(record["path"])
        try:
            retention._write_marker(root_fd, record)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        lease_fd = retention._open_dir(retention.LEASE_ROOT)
        try:
            return retention._read_at(lease_fd, f"{record['build_id']}.json", limit=1024 * 1024)
        finally:
            os.close(lease_fd)

    def _close_orphan_fixture(self, args: dict) -> dict:
        return retention.close_orphaned_unstarted_reservation(**args)

    def test_orphan_closeout_exact_r23_temp_fixture_preserves_bytes_and_releases_charge_once(self) -> None:
        lease, record, args = self._orphan_fixture(
            "r6a-r23-dual-lineage-20261020t214331z",
            owner_experiment="R6-A-R23-DUAL-LINEAGE-20261020T214331Z",
            exact_r23_identity=True)
        self.assertEqual(args["build_id"], "r6a-r23-dual-lineage-20261020t214331z")
        self.assertEqual(args["expected_owner_experiment"],
                         "R6-A-R23-DUAL-LINEAGE-20261020T214331Z")
        self.assertEqual(args["expected_plan_sha256"],
                         "507ef8142396f8cfa0feaa4c9abf924d7b2752e0d2a6d70abce23d774ab6a7bb")
        self.assertEqual(args["expected_admission_identity"], {
            "admission_id": "R6-A-R23-DUAL-LINEAGE-20261020T214331Z",
            "sha256": "184cc4776f4aac667e1afc4e5576f1a78d58d0c48547489d6380baa6290e2903",
        })
        self.assertEqual(lease.expected_bytes, 5_598_724_098)
        self.assertEqual(record["expected_bytes"], 5_598_724_098)
        lease_fd = retention._open_dir(retention.LEASE_ROOT)
        try:
            original_registry = retention._read_at(lease_fd, f"{args['build_id']}.json")
        finally:
            os.close(lease_fd)
        root_fd = retention._open_dir(lease.path)
        try:
            original_marker = retention._read_at(root_fd, retention.BUILD_MARKER)
        finally:
            os.close(root_fd)
        before = retention._snapshot(protected_roots=[])
        self.assertEqual(before["active_build_count"], 1)

        with mock.patch.object(retention.os, "kill", side_effect=ProcessLookupError):
            receipt = self._close_orphan_fixture(args)

        terminal = retention._load_lease(args["build_id"])
        self.assertEqual(terminal["state"], retention.ORPHANED_UNSTARTED_STATE)
        self.assertEqual(terminal["session_v3"]["state"], "RESERVED")
        self.assertEqual(terminal["session_v3"]["next_step_index"], 0)
        self.assertEqual(terminal["session_v3"]["peak_allocated_bytes"], 0)
        self.assertEqual(receipt["result"], retention.ORPHANED_UNSTARTED_STATE)
        self.assertFalse(receipt["outputs_accepted"])
        self.assertTrue(receipt["active_lease_charge_released"])
        self.assertEqual(receipt["released_reservation_bytes"], 5_598_724_098)
        self.assertEqual(base64.b64decode(receipt["original_registry_record_b64"]), original_registry)
        self.assertEqual(base64.b64decode(receipt["original_marker_record_b64"]), original_marker)
        self.assertTrue(lease.path.is_dir())
        self.assertFalse((lease.path / retention.BUILD_SESSION_DIRECTORY).exists())
        self.assertFalse((lease.path / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME).exists())
        self.assertFalse((lease.path / retention.BUILD_SESSION_WORKSPACE_NAME).exists())

        after = retention._snapshot(protected_roots=[])
        self.assertEqual(after["active_build_count"], 0)
        self.assertEqual(after["retained_build_count"], 0)
        self.assertEqual(len(after["orphaned_unstarted_retired"]), 1)
        self.assertEqual(after["unknown"], [])
        self.assertEqual(retention._verify_orphan_closeout_receipt(terminal)["outputs_accepted"], False)
        self.assertFalse((retention.RECEIPT_ROOT / f"{args['build_id']}.json").exists())
        with self.assertRaises(retention.RetentionError):
            self._close_orphan_fixture(args)
        with self.assertRaises(retention.RetentionError):
            lease.start_session()

    def test_orphan_closeout_rejects_all_unsafe_predicates(self) -> None:
        cases = (
            "live-owner", "session-started", "next-step", "peak-bytes", "session-directory",
            "workspace-image", "workspace", "mounted-workspace", "stage", "output",
            "accepted-output", "wrong-admission", "wrong-plan", "wrong-build-id",
            "changed-record", "conflicting-terminal",
        )
        for case in cases:
            with self.subTest(case=case):
                with self._isolated_orphan_namespace():
                    owner_pid_mode = "live" if case == "live-owner" else "dead"
                    lease, record, args = self._orphan_fixture(
                        f"orphan-negative-{case}", owner_pid_mode=owner_pid_mode,
                        legacy_session=case != "live-owner")
                    if case == "live-owner":
                        self.assertEqual(record["owner_pid"], os.getpid())
                        self.assertLessEqual(
                            retention._process_start_time_unix(record["owner_pid"]),
                            record["created_at_unix"])
                    alternate_args = dict(args)
                    if case == "session-started":
                        record["session_started"] = True
                        record["session_started_at_unix"] = time.time()
                        self._write_orphan_record_and_marker(record)
                    elif case == "next-step":
                        record["session_v3"]["next_step_index"] = 1
                        self._write_orphan_record_and_marker(record)
                    elif case == "peak-bytes":
                        record["session_v3"]["peak_allocated_bytes"] = 1
                        self._write_orphan_record_and_marker(record)
                    elif case == "session-directory":
                        (lease.path / retention.BUILD_SESSION_DIRECTORY).mkdir(mode=0o700)
                    elif case == "workspace-image":
                        (lease.path / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME).write_bytes(b"image")
                    elif case == "workspace":
                        (lease.path / retention.BUILD_SESSION_WORKSPACE_NAME).mkdir(mode=0o700)
                    elif case == "stage":
                        (lease.path / "stage").mkdir(mode=0o700)
                    elif case == "output":
                        (lease.path / "output").write_bytes(b"staged output")
                    elif case == "accepted-output":
                        (retention.MANIFEST_ROOT / f"{args['build_id']}.json").write_text(
                            "{}", encoding="utf-8")
                    elif case == "wrong-admission":
                        alternate_args["expected_admission_identity"] = {"sha256": "0" * 64}
                    elif case == "wrong-plan":
                        alternate_args["expected_plan_sha256"] = "0" * 64
                    elif case == "wrong-build-id":
                        alternate_args["build_id"] = "different-build-id"
                    elif case == "changed-record":
                        record["purpose"] = "authenticated but changed reservation"
                        self._write_orphan_record_and_marker(record)
                    elif case == "conflicting-terminal":
                        (retention.RECEIPT_ROOT / f"{args['build_id']}.json").write_text(
                            "{}", encoding="utf-8")
                    if case in {"session-started", "next-step", "peak-bytes"}:
                        raw = self._write_orphan_record_and_marker(record)
                        alternate_args["expected_record_sha256"] = hashlib.sha256(raw).hexdigest()
                    if case == "mounted-workspace":
                        before = self._orphan_closeout_state(lease, args)
                        with mock.patch.object(retention.os.path, "ismount", return_value=True), \
                                mock.patch.object(retention, "_retire_pending_locked",
                                                  side_effect=AssertionError(
                                                      "orphan closeout entered session recovery")) as recovery:
                            with self.assertRaises(retention.RetentionError):
                                self._close_orphan_fixture(args)
                        recovery.assert_not_called()
                        self.assertEqual(self._orphan_closeout_state(lease, args), before)
                    else:
                        self._assert_orphan_closeout_rejected_without_mutation(
                            lease, alternate_args, state_args=args)

    def test_orphan_closeout_allows_only_proven_later_pid_reuse(self) -> None:
        with self._isolated_orphan_namespace():
            lease, record, args = self._orphan_fixture(
                "orphan-pid-reused", owner_pid_mode="live")
            process_started = retention._process_start_time_unix(os.getpid())
            record["created_at_unix"] = process_started - 5
            self.assertEqual(record["owner_pid"], os.getpid())
            self.assertGreater(process_started, record["created_at_unix"])
            raw = self._write_orphan_record_and_marker(record)
            args["expected_record_sha256"] = hashlib.sha256(raw).hexdigest()
            receipt = self._close_orphan_fixture(args)
        self.assertEqual(receipt["result"], retention.ORPHANED_UNSTARTED_STATE)

    def test_orphan_closeout_allows_a_process_id_proven_absent(self) -> None:
        with self._isolated_orphan_namespace():
            lease, record, args = self._orphan_fixture("orphan-owner-absent")
            with self.assertRaises(ProcessLookupError):
                os.kill(record["owner_pid"], 0)
            receipt = self._close_orphan_fixture(args)
            self.assertEqual(receipt["result"], retention.ORPHANED_UNSTARTED_STATE)

    def test_orphan_closeout_rejects_ambiguous_pid_start_time_without_mutation(self) -> None:
        with self._isolated_orphan_namespace():
            lease, record, args = self._orphan_fixture(
                "orphan-pid-start-equal", owner_pid_mode="live")
            process_started = retention._process_start_time_unix(os.getpid())
            record["created_at_unix"] = process_started
            self.assertEqual(process_started, record["created_at_unix"])
            raw = self._write_orphan_record_and_marker(record)
            args["expected_record_sha256"] = hashlib.sha256(raw).hexdigest()
            self._assert_orphan_closeout_rejected_without_mutation(lease, args)

        with self._isolated_orphan_namespace():
            lease, record, args = self._orphan_fixture(
                "orphan-pid-start-unavailable", owner_pid_mode="live")
            raw = self._write_orphan_record_and_marker(record)
            args["expected_record_sha256"] = hashlib.sha256(raw).hexdigest()
            with mock.patch.object(retention, "_process_start_time_unix",
                                   side_effect=retention.RetentionError("synthetic unavailable process start")):
                self._assert_orphan_closeout_rejected_without_mutation(lease, args)

    def test_orphan_closeout_rejects_active_lock_owner_and_matching_controller(self) -> None:
        with self._isolated_orphan_namespace():
            lease, _record, args = self._orphan_fixture("orphan-active-lock")
            lock_path = retention.LEASE_ROOT / retention.BUILD_LOCK
            helper_code = (
                "import fcntl, os, sys\n"
                "fd = os.open(sys.argv[1], os.O_RDWR)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "print('LOCK_HELD', flush=True)\n"
                "sys.stdin.readline()\n"
                "fcntl.flock(fd, fcntl.LOCK_UN)\n"
                "os.close(fd)\n"
            )
            helper = subprocess.Popen(
                [sys.executable, "-c", helper_code, str(lock_path)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True)
            try:
                ready, _, _ = select.select([helper.stdout], [], [], 5)
                self.assertTrue(ready, "cross-process lock helper did not signal readiness")
                self.assertEqual(helper.stdout.readline().strip(), "LOCK_HELD")
                self._assert_orphan_closeout_rejected_without_mutation(lease, args)
            finally:
                if helper.poll() is None:
                    assert helper.stdin is not None
                    helper.stdin.write("release\n")
                    helper.stdin.flush()
                _stdout, stderr = helper.communicate(timeout=5)
                self.assertEqual(helper.returncode, 0, stderr)

        with self._isolated_orphan_namespace():
            second, _second_record, second_args = self._orphan_fixture("orphan-matching-controller")
            fake_ps = type("ProcessTable", (), {
                "returncode": 0,
                "stdout": (f"{os.getpid() + 99999} /controller --build-id "
                           f"{second_args['build_id']}\n"),
                "stderr": "",
            })()
            with mock.patch.object(retention.subprocess, "run", return_value=fake_ps):
                self._assert_orphan_closeout_rejected_without_mutation(second, second_args)

    def test_reserved_never_started_status_is_not_auto_failed_or_retired(self) -> None:
        lease, _record, _args = self._orphan_fixture("orphan-status-remains-reserved")
        state = retention.status_snapshot()
        current = retention._load_lease(lease.build_id)
        self.assertEqual(current["state"], "ACTIVE")
        self.assertEqual(current["session_v3"]["state"], "RESERVED")
        self.assertFalse(current.get("session_started", False))
        self.assertTrue(any(row["build_id"] == lease.build_id for row in state["active"]))
        with mock.patch.object(retention, "_verify_protected_evidence_roots", return_value=[]):
            self.assertEqual(retention.reconcile_pending(), [])
        current = retention._load_lease(lease.build_id)
        self.assertEqual(current["state"], "ACTIVE")
        self.assertEqual(current["session_v3"]["state"], "RESERVED")

    def test_execute_admitted_plan_reserves_then_starts_same_object_before_step_loop(self) -> None:
        lease = self._session_lease("same-process-execute")
        events: list[str] = []
        reserve_real = lease.reserve

        class ProbeSession:
            def run_next(self) -> dict:
                events.append("run_next")
                return {"status": "PASS"}

            def verify_source(self) -> dict:
                events.append("verify_source")
                return {"status": "PASS"}

            def finalize(self) -> dict:
                events.append("finalize")
                return {"status": "COMPLETED"}

        ProbeSession.plan = lease.session_plan

        def reserve_and_record() -> Path | None:
            events.append("reserve")
            result = reserve_real()
            events.append("reserved")
            return result

        def start_exact_object() -> ProbeSession:
            events.append("start_session")
            self.assertTrue(lease.started)
            self.assertEqual(len(lease.lock_fds or ()), 2)
            for descriptor in lease.lock_fds or ():
                os.fstat(descriptor)
            record = retention._load_lease(lease.build_id)
            self.assertEqual(record["session_started"], False)
            code = (
                "import errno,fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR); "
                "\ntry: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB); print('FREE')"
                "\nexcept OSError as e: print('BUSY' if e.errno in (errno.EACCES,errno.EAGAIN) else 'ERROR')"
            )
            probe = subprocess.run([sys.executable, "-c", code,
                                    str(retention.LEASE_ROOT / retention.BUILD_LOCK)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, check=False, timeout=10)
            self.assertEqual(probe.returncode, 0, probe.stderr)
            self.assertEqual(probe.stdout.strip(), "BUSY",
                             "separate controller process raced between reserve and session start")
            return ProbeSession()

        with (mock.patch.object(lease, "reserve", side_effect=reserve_and_record) as reserve_call,
              mock.patch.object(lease, "start_session", side_effect=start_exact_object) as start_call,
              mock.patch.object(lease, "_validate_session_inputs",
                                wraps=lease._validate_session_inputs) as source_validation,
              mock.patch.object(retention, "_gate_blockers", wraps=retention._gate_blockers) as gate_check):
            result = lease.execute_admitted_plan()
        reserve_call.assert_called_once_with()
        start_call.assert_called_once_with()
        source_validation.assert_called()
        gate_check.assert_called()
        self.assertEqual(events[:3], ["reserve", "reserved", "start_session"])
        self.assertEqual(events.count("run_next"), len(lease.session_plan["steps"]) - 1)
        self.assertEqual(events[-2:], ["verify_source", "finalize"])
        self.assertEqual(result["status"], "COMPLETED")
        lease._stop_heartbeat()
        lease._release_lock()

    def test_serialized_plan_cannot_start_without_live_capability_and_loss_fails_closed(self) -> None:
        lease = self._session_lease("serialized-plan-cannot-start")
        with self.assertRaises(retention.RetentionError):
            lease.start_session()
        duplicate = self._session_lease("serialized-plan-cannot-start-copy")
        with self.assertRaises(retention.RetentionError):
            duplicate.start_session()

        lost = self._session_lease("same-process-capability-loss")
        reserve_real = lost.reserve
        released_descriptors: list[int] = []

        def reserve_then_drop_capability() -> Path | None:
            result = reserve_real()
            assert lost.lock_fds is not None
            lease_fd, lock_fd = lost.lock_fds
            os.close(lock_fd)
            released_descriptors.append(lease_fd)
            lost.lock_fds = (lease_fd, lock_fd)
            return result

        with mock.patch.object(lost, "reserve", side_effect=reserve_then_drop_capability) as reserve_call:
            with self.assertRaisesRegex(retention.RetentionError, "capability was lost"):
                lost.execute_admitted_plan()
        reserve_call.assert_called_once_with()
        current = retention._load_lease(lost.build_id)
        self.assertEqual(current["state"], "ACTIVE")
        self.assertEqual(current["session_v3"]["state"], "RESERVED")
        self.assertFalse(current["session_started"])
        self.assertFalse((lost.path / retention.BUILD_SESSION_DIRECTORY).exists())
        lost._stop_heartbeat()
        lost.lock_fds = None
        for descriptor in released_descriptors:
            os.close(descriptor)

    @_with_synthetic_buildlease_runtime_gate
    def test_session_started_marker_precedes_workspace_side_effect_and_failed_start_is_host_failure(self) -> None:
        lease = self._session_lease("session-start-marker-first")
        lease.reserve()
        observed: list[bool] = []

        def attach_after_marker(root: Path, capacity_bytes: int) -> dict:
            observed.append(retention._load_lease(lease.build_id).get("session_started") is True)
            raise retention.RetentionError("synthetic attach refusal after durable start marker")

        with mock.patch.object(retention, "_attach_session_workspace", side_effect=attach_after_marker):
            with self.assertRaises(retention.RetentionError):
                lease.start_session()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(observed, [True])
        self.assertTrue(record["session_started"])
        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["session_v3"]["state"], "FAILED")
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
        self.assertFalse((lease.path / retention.BUILD_SESSION_TERMINAL).exists())
        self.assertFalse((lease.path / retention.BUILD_SESSION_WORKSPACE_NAME).exists())
        lease._stop_heartbeat()
        lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_session_start_failure_before_start_marker_remains_unstarted_reservation(self) -> None:
        lease = self._session_lease("session-start-before-marker-failure")
        lease.reserve()
        try:
            with mock.patch.object(lease, "_validate_session_inputs",
                                   side_effect=retention.RetentionError("synthetic pre-start validation failure")):
                with self.assertRaisesRegex(retention.RetentionError, "pre-start validation failure"):
                    lease.start_session()
            record = retention._load_lease(lease.build_id)
            self.assertEqual(record["state"], "ACTIVE")
            self.assertFalse(record["session_started"])
            self.assertEqual(record["session_v3"]["state"], "RESERVED")
            self.assertFalse((lease.path / retention.BUILD_SESSION_DIRECTORY).exists())
            self.assertFalse((lease.path / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME).exists())
            self.assertFalse((lease.path / retention.BUILD_SESSION_WORKSPACE_NAME).exists())
            self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.failure").exists())
            self.assertFalse((retention.RECEIPT_ROOT / f"{lease.build_id}.json").exists())
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_session_start_failure_during_workspace_creation_is_failed_unaccepted(self) -> None:
        lease = self._session_lease("session-start-workspace-creation-failure")
        lease.reserve()
        observed: list[bool] = []

        def fail_during_attach(root: Path, _capacity_bytes: int) -> dict:
            root = Path(root)
            observed.append(retention._load_lease(lease.build_id).get("session_started") is True)
            (root / retention.BUILD_SESSION_WORKSPACE_NAME).mkdir(mode=0o700)
            (root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME).write_bytes(b"partial synthetic image")
            raise retention.RetentionError("synthetic workspace creation failed after partial side effects")

        try:
            with mock.patch.object(retention, "_attach_session_workspace", side_effect=fail_during_attach):
                with self.assertRaisesRegex(retention.RetentionError, "workspace creation failed"):
                    lease.start_session()
            record = retention._load_lease(lease.build_id)
            self.assertEqual(observed, [True])
            self.assertTrue(record["session_started"])
            self.assertEqual(record["state"], "FAILED")
            self.assertEqual(record["session_v3"]["state"], "FAILED")
            self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
            self.assertFalse((lease.path / retention.BUILD_SESSION_TERMINAL).exists())
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_session_start_failure_after_workspace_exists_before_step_one_is_failed(self) -> None:
        lease = self._session_lease("session-start-before-step-one-failure")
        lease.reserve()
        observed: list[tuple[bool, bool, bool]] = []
        check_limits = retention.BuildLeaseSession._check_limits

        def fail_after_workspace(session, phase: str, **kwargs):
            if phase == "session-start":
                observed.append((
                    (session.root / retention.BUILD_SESSION_WORKSPACE_NAME).is_dir(),
                    (session.root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME).is_file(),
                    retention._load_lease(lease.build_id)["session_started"] is True,
                ))
                raise retention.RetentionError("synthetic failure after workspace creation")
            return check_limits(session, phase, **kwargs)

        try:
            with mock.patch.object(retention.BuildLeaseSession, "_check_limits", autospec=True,
                                   side_effect=fail_after_workspace):
                with self.assertRaisesRegex(retention.RetentionError, "after workspace creation"):
                    lease.start_session()
            record = retention._load_lease(lease.build_id)
            self.assertEqual(observed, [(True, True, True)])
            self.assertEqual(record["state"], "FAILED")
            self.assertEqual(record["retirement_status"], "COMPLETED")
            self.assertEqual(record["session_v3"]["state"], "FAILED")
            self.assertEqual(record["session_v3"]["next_step_index"], 0)
            self.assertEqual(record["session_v3"]["step_receipts"], [])
            cleanup_receipt = retention._read_json(
                retention.RECEIPT_ROOT / f"{lease.build_id}.json", sealed=True)
            self.assertEqual(cleanup_receipt["execution_outcome"], "FAILED")
            self.assertIs(cleanup_receipt["outputs_accepted"], False)
            self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
            self.assertFalse((lease.path / retention.BUILD_SESSION_TERMINAL).exists())
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_session_start_success_persists_active_workspace_before_step_one(self) -> None:
        lease = self._session_lease("session-start-success")
        lease.reserve()
        try:
            session = lease.start_session()
            record = retention._load_lease(lease.build_id)
            self.assertIs(session, lease.session)
            self.assertTrue(record["session_started"])
            self.assertIsInstance(record["session_started_at_unix"], (int, float))
            self.assertEqual(record["state"], "ACTIVE")
            self.assertEqual(record["session_v3"]["state"], "ACTIVE")
            self.assertIsInstance(record["session_v3"]["started_at_unix"], (int, float))
            self.assertEqual(record["session_v3"]["next_step_index"], 0)
            self.assertEqual(record["session_v3"]["step_receipts"], [])
            self.assertTrue((lease.path / retention.BUILD_SESSION_DIRECTORY).is_dir())
            self.assertTrue((lease.path / retention.BUILD_SESSION_WORKSPACE_NAME).is_dir())
            self.assertTrue((lease.path / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME).is_file())
            self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
            session._fail("synthetic test cleanup after successful start", -1,
                          command=["synthetic-test-cleanup"])
            terminal = retention._load_lease(lease.build_id)
            self.assertEqual(terminal["state"], "FAILED")
            self.assertEqual(terminal["session_v3"]["state"], "FAILED")
        finally:
            lease._stop_heartbeat()
            lease._release_lock()


class R17OwnershipBoundaryTests(unittest.TestCase):
    def _identity_record(self, root: Path, classification: str) -> dict[str, object]:
        root_fd = retention._open_dir(root)
        try:
            identity = retention._fs_identity(root_fd)
        finally:
            os.close(root_fd)
        return {
            "classification": classification,
            "accounting_scope": "OTHER_PROJECT_PROTECTED",
            "reason": "synthetic exact identity fixture",
            "filesystem_identity": identity,
        }

    def test_r17_disposition_requires_exact_path_root_identity_and_reports(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-identity-", dir="/private/tmp") as temp:
            root = Path(temp) / "reviewed"
            root.mkdir()
            nested = root / "nested"
            nested.mkdir()
            payload = nested / "source.txt"
            payload.write_text("Firelink fixture\n", encoding="utf-8")
            sibling = Path(temp) / "copy"
            sibling.mkdir()
            (sibling / "source.txt").write_text("Firelink fixture\n", encoding="utf-8")
            record = self._identity_record(root, "UNRELATED_PROJECT_TEMP")
            with (mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES, {str(root): record}),
                  mock.patch.object(retention, "_r17_ownership_reports_match", return_value=True)):
                disposition = retention._r17_temp_root_disposition(root)
                self.assertIsNotNone(disposition)
                self.assertEqual(disposition["classification"], "UNRELATED_PROJECT_TEMP")
                self.assertIsNone(retention._r17_temp_root_disposition(sibling))
                original_root_times = (root.stat().st_mtime_ns, root.stat().st_ctime_ns)
                payload.write_text("Changed nested contents with a different size\n", encoding="utf-8")
                self.assertEqual(original_root_times,
                                 (root.stat().st_mtime_ns, root.stat().st_ctime_ns))
                changed = retention._r17_temp_root_disposition(root)
                self.assertIsNotNone(changed)
                self.assertEqual(changed["filesystem_identity"], disposition["filesystem_identity"])
                self.assertGreater(changed["logical_bytes"], disposition["logical_bytes"])

    def test_r17_disposition_rejects_changed_root_identity_and_report_binding(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-binding-", dir="/private/tmp") as temp:
            root = Path(temp) / "reviewed"
            root.mkdir()
            (root / "source.txt").write_text("Firelink fixture\n", encoding="utf-8")
            sibling = Path(temp) / "copy"
            sibling.mkdir()
            record = self._identity_record(sibling, "UNRELATED_PROJECT_TEMP")
            with mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES, {str(root): record}):
                with mock.patch.object(retention, "_r17_ownership_reports_match", return_value=True):
                    self.assertIsNone(retention._r17_temp_root_disposition(root))
                with mock.patch.object(retention, "_r17_ownership_reports_match", return_value=False):
                    self.assertIsNone(retention._r17_temp_root_disposition(root))

    def test_r17_discovery_fails_closed_when_exact_root_is_replaced_by_a_file(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-file-root-", dir="/private/tmp") as temp:
            temp_root = Path(temp)
            home = temp_root / "home"
            home.mkdir()
            root_file = temp_root / "firelink-root"
            root_file.write_text("replacement, not reviewed directory", encoding="utf-8")
            with (mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                  mock.patch.object(storage_inventory, "HOME", home),
                  mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(root_file): {"classification": "UNRELATED_PROJECT_TEMP",
                                                    "accounting_scope": "OTHER_PROJECT_PROTECTED"}}),
                  mock.patch.object(retention, "_r17_ownership_reports_match", return_value=True),
                  mock.patch.object(retention, "_all_root_candidates", return_value=[])):
                with self.assertRaisesRegex(RuntimeError, "cannot verify exact R17 root identity"):
                    storage_inventory.discover_roots()

    def test_r17_inventory_walk_stays_anchored_when_root_path_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-fwalk-", dir="/private/tmp") as temp:
            root = Path(temp) / "root"
            root.mkdir()
            original_file = root / "original.bin"
            original_file.write_bytes(b"original root contents")
            root_fd = retention._open_dir(root)
            walk_rows = storage_inventory._r17_root_walk(
                root_fd, lambda error: (_ for _ in ()).throw(error))
            try:
                _dirpath, _dirnames, filenames, directory_fd = next(walk_rows)
                self.assertIn("original.bin", filenames)
                original_info = os.stat("original.bin", dir_fd=directory_fd,
                                        follow_symlinks=False)
                moved = Path(temp) / "moved-original-root"
                root.rename(moved)
                root.mkdir()
                (root / "replacement.bin").write_bytes(b"replacement root contents")
                pinned_info = os.stat("original.bin", dir_fd=directory_fd,
                                      follow_symlinks=False)
                self.assertEqual((pinned_info.st_dev, pinned_info.st_ino),
                                 (original_info.st_dev, original_info.st_ino))
                self.assertFalse((root / "original.bin").exists())
                self.assertTrue((root / "replacement.bin").is_file())
            finally:
                walk_rows.close()
                os.close(root_fd)

    def test_r17_inventory_hash_stays_anchored_when_root_path_is_swapped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-hash-fd-", dir="/private/tmp") as temp:
            root = Path(temp) / "root"
            root.mkdir()
            original = root / "artifact.dylib"
            original.write_bytes(b"original artifact" * 128 * 1024)
            root_fd = retention._open_dir(root)
            try:
                expected = os.stat("artifact.dylib", dir_fd=root_fd, follow_symlinks=False)
                original_digest = hashlib.sha256(original.read_bytes()).hexdigest()
                moved = Path(temp) / "moved-original-root"
                root.rename(moved)
                root.mkdir()
                (root / "artifact.dylib").write_bytes(b"replacement artifact" * 128 * 1024)
                pinned_digest = storage_inventory.sha256_at(
                    root_fd, Path("artifact.dylib"), expected)
                self.assertEqual(pinned_digest, original_digest)
                self.assertNotEqual(pinned_digest,
                                    hashlib.sha256((root / "artifact.dylib").read_bytes()).hexdigest())
            finally:
                os.close(root_fd)

    def test_r17_missing_unrelated_roots_block_retention_snapshot(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-missing-", dir="/private/tmp") as temp:
            missing_firelink = Path(temp) / "missing-firelink-root"
            missing_probe = Path(temp) / "missing-probe-root"
            identities = {
                str(missing_firelink): {"classification": "UNRELATED_PROJECT_TEMP"},
                str(missing_probe): {"classification": "FGMETAL_HISTORICAL_PROBE_EVIDENCE"},
            }
            with (mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES, identities, clear=True),
                  mock.patch.object(retention, "_lease_files", return_value=[]),
                  mock.patch.object(retention, "_all_root_candidates", return_value=[]),
                  mock.patch.object(retention, "_classified_non_retention_tmp_roots", return_value=[])):
                state = retention._snapshot(protected_roots=[])
            missing_paths = {row["path"] for row in state["unknown"]}
            self.assertEqual(missing_paths, {str(missing_firelink), str(missing_probe)})
            self.assertTrue(all("exact-root identity cannot be verified" in row["reason"]
                                for row in state["unknown"]))

    def test_verified_r17_roots_prune_nested_retention_candidates(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-prune-", dir="/private/tmp") as temp:
            temp_root = Path(temp)
            home = temp_root / "home"
            home.mkdir()
            root = temp_root / "firelink" / "target" / "debug" / "build"
            nested_build = root / "nested" / "build"
            nested_build.mkdir(parents=True)
            (nested_build / "build.ninja").write_text("foreign build graph\n", encoding="utf-8")
            reviewed_disposition = {
                "classification": "UNRELATED_PROJECT_TEMP",
                "accounting_scope": "OTHER_PROJECT_PROTECTED",
            }

            def disposition(path):
                return reviewed_disposition if Path(path) == root else None

            with (mock.patch.object(retention, "TEMP_ROOT", temp_root),
                  mock.patch.object(retention, "HOME", home),
                  mock.patch.object(retention, "LEGACY_BUILD_ROOTS", ()),
                  mock.patch.object(retention, "BUILD_ROOT", home / "cache" / "builds"),
                  mock.patch.object(retention, "_worktree_build_root_candidates", return_value=[]),
                  mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(root): {"classification": "UNRELATED_PROJECT_TEMP",
                                               "accounting_scope": "OTHER_PROJECT_PROTECTED"}}),
                  mock.patch.object(retention, "_r17_temp_root_disposition", side_effect=disposition)):
                candidates = retention._all_root_candidates()

            self.assertIn(root, candidates)
            self.assertNotIn(nested_build, candidates)

    def test_inventory_keeps_firelink_out_of_scope_and_fgmetal_probe_in_scope(self) -> None:
        firelink = Path("/private/tmp/firelink-aria2-release.gGghjc/aria2-1.37.0")
        probe = Path("/private/tmp/buildlease-vz-entitlement.RVW7oO")
        self.assertFalse(storage_inventory._is_fgmetal_scope_path(
            firelink, "OTHER_PROJECT_TEMP_ROOT"))
        self.assertTrue(storage_inventory._is_fgmetal_scope_path(
            probe, "PRESERVED_FGMETAL_HISTORICAL_TEMP_ROOT"))
        self.assertFalse(storage_inventory._is_fgmetal_scope_path(
            Path("/workspace/experiments/dxmt_framegen/evidence"), "MAIN_CHECKOUT"))

    def test_current_r17_roots_match_the_independent_review_bindings(self) -> None:
        missing = [path for path in retention.R17_TEMP_ROOT_IDENTITIES if not Path(path).exists()]
        if missing:
            self.skipTest(f"host-scoped R17 roots are unavailable: {missing}")
        for path, expected in retention.R17_TEMP_ROOT_IDENTITIES.items():
            with self.subTest(path=path):
                disposition = retention._r17_temp_root_disposition(path)
                self.assertIsNotNone(disposition)
                self.assertEqual(disposition["classification"], expected["classification"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
