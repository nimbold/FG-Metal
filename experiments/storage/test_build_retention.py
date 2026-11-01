#!/usr/bin/env python3
"""Synthetic-only tests for fail-closed build retention; never create graphics builds."""
from __future__ import annotations

import hashlib
import copy
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
from collections.abc import Callable
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

_STORAGE_DIR = Path(__file__).resolve().parent
if str(_STORAGE_DIR) not in sys.path:
    sys.path.insert(0, str(_STORAGE_DIR))

import build_retention as retention
import r6a_dependency_manifest
import storage_policy
import storage_inventory
import storage_gate0
import run_r6a_execution

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


def _cleanup_host_v3_integration_workspace(root: Path, record: dict[str, object],
                                           preserve_temp: Callable[[], None]) -> None:
    """Retire a real integration-test mount and keep its backing image on uncertainty."""
    try:
        row = retention._session_image_row(root)
        session_record = record.get("session_v3")
        if not isinstance(session_record, dict):
            raise retention.RetentionError("integration lease lacks its session identity")
        rw_identity = session_record.get("workspace_mount_identity")
        ro_identity = session_record.get("readonly_mount_identity")
        rw_detached_identity = session_record.get("detached_workspace_identity")
        ro_detached_identity = session_record.get("readonly_detached_identity")
        if row is not None and row.get("writeable") is False:
            if not isinstance(ro_identity, dict):
                raise retention.RetentionError("read-only integration image lacks its mount identity")
            detached = _ORIGINAL_DETACH_SESSION_WORKSPACE_READONLY(root, ro_identity)
        elif row is not None:
            if not isinstance(rw_identity, dict):
                raise retention.RetentionError("writable integration image lacks its mount identity")
            detached = _ORIGINAL_DETACH_SESSION_WORKSPACE(root, rw_identity)
        elif isinstance(ro_detached_identity, dict):
            detached = retention._verify_session_workspace_detached(root, ro_detached_identity)
        elif isinstance(rw_detached_identity, dict):
            detached = retention._verify_session_workspace_detached(root, rw_detached_identity)
        else:
            raise retention.RetentionError("integration lease lacks a persisted detached workspace identity")
        if not isinstance(detached, dict) or detached.get("attached") is not False:
            raise retention.RetentionError("integration workspace detach was not confirmed")
        if retention._session_image_row(root) is not None:
            raise retention.RetentionError("integration workspace image remains attached")
    except BaseException:
        preserve_temp()
        raise


def _verify_auto_retired_host_v3_workspace(root: Path, record: dict[str, object]) -> dict[str, object]:
    """Accept a removed integration root only with completed retirement and detach proof."""
    receipt = retention._verify_completed_retirement(record)
    try:
        Path(root).lstat()
    except FileNotFoundError:
        pass
    else:
        raise retention.RetentionError("auto-retired integration root still exists")
    if (receipt.get("retirement_status") != "COMPLETED"
            or receipt.get("execution_outcome") != "SUCCEEDED"
            or receipt.get("outputs_accepted") is not True):
        raise retention.RetentionError("missing integration root lacks successful retirement proof")
    session = record.get("session_v3")
    if (record.get("state") != "COMPLETED"
            or record.get("completion_contract") != "HOST_BUILDLEASE_V3_MANIFEST_FIRST_V1"
            or not isinstance(session, dict)
            or session.get("executor_mode") != retention.BUILD_SESSION_EXECUTOR_MODE
            or session.get("state") != "ACCEPTED"):
        raise retention.RetentionError("missing integration root lacks an accepted Host-v3 session")
    rw_detached = session.get("detached_workspace_identity")
    ro_detached = session.get("readonly_detached_identity")
    if not isinstance(rw_detached, dict) or not isinstance(ro_detached, dict):
        raise retention.RetentionError("accepted Host-v3 session lacks persisted RW/RO detached identities")
    expected_image = str(root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME)
    expected_mountpoint = str(root / retention.BUILD_SESSION_WORKSPACE_NAME)
    for label, identity in (("writable", rw_detached), ("read-only", ro_detached)):
        if (identity.get("attached") is not False
                or identity.get("image_path") != expected_image
                or identity.get("mountpoint") != expected_mountpoint):
            raise retention.RetentionError(
                f"accepted Host-v3 {label} detached identity does not bind the removed workspace")
    if (rw_detached.get("image_device") is None
            or rw_detached.get("image_inode") is None
            or (rw_detached.get("image_device"), rw_detached.get("image_inode"))
                != (ro_detached.get("image_device"), ro_detached.get("image_inode"))):
        raise retention.RetentionError("accepted Host-v3 detached identities do not bind one backing image")

    for image_row in retention._session_hdiutil_info():
        image_path = image_row.get("image-path")
        if not isinstance(image_path, str):
            raise retention.RetentionError("hdiutil inventory omitted an image path during retirement check")
        if image_path == expected_image:
            raise retention.RetentionError("retired integration backing image is still attached")
        entities = image_row.get("system-entities")
        if not isinstance(entities, list):
            raise retention.RetentionError("hdiutil inventory omitted image entities during retirement check")
        for entity in entities:
            if not isinstance(entity, dict):
                raise retention.RetentionError("hdiutil inventory contains malformed image entities")
            if entity.get("mount-point") == expected_mountpoint:
                raise retention.RetentionError("retired integration mount point is still attached")
    return receipt


def _synthetic_dual_graph_option_rows(is_windows: bool, build_type: str) -> list[dict[str, object]]:
    values: dict[str, object] = {
        "backend": "ninja", "buildtype": build_type,
        "wrap_mode": "nodownload", "prefix": "/usr",
    }
    if not is_windows:
        values.update({
            "enable_dxgi": True, "enable_d3d11": True,
            "enable_d3d8": False, "enable_d3d9": False, "enable_d3d10": False,
            "native_sdl2": "enabled", "native_sdl3": "disabled", "native_glfw": "disabled",
        })
    return [{"name": name, "value": value,
             "section": "core" if name in {"backend", "buildtype", "wrap_mode", "prefix"} else "user",
             "machine": "any"}
            for name, value in values.items()]


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
        start = source.index("    def reserve(self, *, admission_transaction: Any | None = None) -> Path | None:",
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

    def test_completed_lease_without_manifest_blocks_and_unknown_state_fails_closed(self) -> None:
        self._write_lease("completed-host-v3", "COMPLETED")
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertEqual(len(blockers), 1)
        self.assertIn("state is unknown", blockers[0])
        self.assertIn("completed-host-v3", blockers[0])

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
        self._preserve_temp_directory = False
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
        self.home = home
        self.synthetic_dual_tool_pins: dict[str, dict[str, str]] = {}
        self.synthetic_dual_runtime_pins: dict[str, dict[str, str]] = {}
        self.synthetic_dual_host_dependency_pins: dict[str, dict[str, str]] = {}
        self.synthetic_dual_host_profile: dict[str, str] = {}
        self.synthetic_dual_meson_python_shebang = str(self.repo / "synthetic-meson-python3.14")
        self.native_toolchain_capture_patch = mock.patch.object(
            retention, "_capture_session_native_toolchain_selection",
            side_effect=self._synthetic_native_toolchain_selection)
        self.addCleanup(self._cleanup_temporary_directory)
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

    def _preserve_temp_root(self) -> None:
        self._preserve_temp_directory = True
        self.temp._finalizer.detach()

    def _cleanup_temporary_directory(self) -> None:
        if not self._preserve_temp_directory:
            try:
                self.temp.cleanup()
            except BaseException:
                self._preserve_temp_root()
                raise

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
                "mounted_mountpoint_device": workspace_info.st_dev,
                "mounted_mountpoint_inode": workspace_info.st_ino,
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
                or workspace_info.st_dev != identity.get("device")
                or (workspace_info.st_dev, workspace_info.st_ino)
                    != (identity.get("mounted_mountpoint_device"),
                        identity.get("mounted_mountpoint_inode"))):
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
                              experiment_id: str = "synthetic-test",
                              additional_manifest_fields: dict | None = None) -> None:
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
        manifest.update(additional_manifest_fields or {})
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

    def _capture_identity_for_build(self, build: Path, evidence: Path,
                                    *, session_plan: dict | None = None) -> dict:
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
                       persistent_output_allowance_bytes: int | None = None,
                       owner_experiment: str = "synthetic-test") -> retention.BuildLease:
        source = self.repo / f"source-{build_id}"
        source.mkdir(mode=0o700)
        (source / "meson.build").write_text("project('synthetic', 'c', 'cpp')\n", encoding="utf-8")
        if dual_graph:
            (source / "synthetic_vert.vert").write_text(
                "#version 450\nvoid main() { gl_Position = vec4(0.0); }\n", encoding="utf-8")
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
        source_entries = ["meson.build", *( ["build-win64.txt", "synthetic_vert.vert"] if dual_graph else [])]
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
            for runtime_id, filename in retention.BUILD_SESSION_DUAL_GRAPH_RUNTIME_SOURCE_NAMES.items():
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
                "build_type": "release",
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
            config_hash = self._install_synthetic_dual_graph_prebuild(plan, build_id)
            if persistent_output_allowance_bytes is not None:
                plan["persistent_output_allowance_bytes"] = persistent_output_allowance_bytes
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

    def _install_synthetic_dual_graph_prebuild(self, plan: dict[str, object], build_id: str) -> str:
        """Create isolated, synthetic configure/shader evidence for the dual-graph fixture."""
        artifact_root = self.repo / "experiments/storage/build-manifests/synthetic-prebuild-fixtures" / build_id
        if artifact_root.exists() or artifact_root.is_symlink():
            raise AssertionError(f"synthetic prebuild fixture already exists: {artifact_root}")
        evidence_root = artifact_root / "evidence"
        evidence_root.mkdir(parents=True, mode=0o700)
        source_identity = plan["source_identity"]
        assert isinstance(source_identity, dict)
        source_root = Path(source_identity["root"])
        plan_graphs = plan["build_graphs"]
        assert isinstance(plan_graphs, list)
        manifest_graphs = []
        graph_keys = {}
        generated_payloads = {}
        for graph in plan_graphs:
            assert isinstance(graph, dict)
            graph_id = graph["graph_id"]
            is_windows = graph["graph_role"] == "WINDOWS_PRODUCT"
            build_root = artifact_root / "disposable" / graph_id
            evidence_graph = evidence_root / graph_id
            for child in ("meson-info", "meson-logs", "meson-private"):
                (build_root / child).mkdir(parents=True, exist_ok=True, mode=0o700)
            options = _synthetic_dual_graph_option_rows(is_windows, plan["build_type"])
            compiler_identity = {}
            for machine in ("build", "host"):
                mingw = is_windows and machine == "host"
                c_tool = plan["tools"]["mingw_cc" if mingw else "native_cc"]
                cpp_tool = plan["tools"]["mingw_cxx" if mingw else "native_cxx"]
                compiler_id = "gcc" if mingw else "clang"
                linker_id = "ld.bfd" if mingw else "ld64"
                compiler_identity[machine] = {
                    "c": {"id": compiler_id, "version": "16.2.0" if mingw else "27.0",
                          "full_version": "synthetic GCC 16.2.0" if mingw else "synthetic Apple clang",
                          "exelist": [Path(c_tool["path"]).name],
                          "linker_exelist": [Path(c_tool["path"]).name], "linker_id": linker_id},
                    "cpp": {"id": compiler_id, "version": "16.2.0" if mingw else "27.0",
                            "full_version": "synthetic GCC 16.2.0" if mingw else "synthetic Apple clang",
                            "exelist": [Path(cpp_tool["path"]).name],
                            "linker_exelist": [Path(cpp_tool["path"]).name], "linker_id": linker_id},
                }
            (build_root / "meson-info/intro-buildoptions.json").write_text(
                json.dumps(options), encoding="utf-8")
            (build_root / "meson-info/intro-compilers.json").write_text(
                json.dumps(compiler_identity), encoding="utf-8")
            (build_root / "meson-info/intro-dependencies.json").write_text("[]", encoding="utf-8")
            log_lines = [f"Source dir: {source_root}", f"Build dir: {build_root}",
                         "Build type: cross build" if is_windows else "Build type: native build",
                         "Build linker: ld64",
                         "Host linker: ld.bfd" if is_windows else "Host linker: ld64"]
            if is_windows:
                log_lines.append(f"cross_file = ['{source_root}/build-win64.txt']")
            (build_root / "meson-logs/meson-log.txt").write_text(
                "\n".join(log_lines) + "\n", encoding="utf-8")
            command_lines = ["[options]", "backend = ninja",
                             f"buildtype = {plan['build_type']}",
                             "wrap_mode = nodownload", "prefix = /usr"]
            if not is_windows:
                command_lines.extend([
                    "enable_dxgi = true", "enable_d3d11 = true", "enable_d3d8 = false",
                    "enable_d3d9 = false", "enable_d3d10 = false", "native_sdl2 = enabled",
                    "native_sdl3 = disabled", "native_glfw = disabled"])
            if is_windows:
                command_lines.extend(["", "[properties]",
                                      f"cross_file = ['{source_root}/build-win64.txt']"])
            else:
                command_lines.extend(["", "[properties]"])
            (build_root / "meson-private/cmd_line.txt").write_text(
                "\n".join(command_lines) + "\n", encoding="utf-8")
            (build_root / "build.ninja").write_text("synthetic graph\n", encoding="utf-8")
            shader_relative = "meson-private/synthetic_vert_shader.h"
            shader_contents = f"synthetic shader bytes for {graph_id}\n".encode()
            shader_path = build_root / shader_relative
            shader_path.parent.mkdir(parents=True, exist_ok=True)
            shader_path.write_bytes(shader_contents)
            generated_payloads[graph_id] = {shader_relative: shader_contents}
            cross_file = source_root / "build-win64.txt" if is_windows else None
            meson_identity = _ORIGINAL_CAPTURE_IDENTITY(
                build_root, evidence_graph, require_direct_meson=True,
                expected_cross_file=cross_file, enforce_cross_file=True,
                cross_source_root=source_root)
            profile_sha = retention._validate_dual_graph_configuration_profile(
                plan, graph, meson_identity, plan["build_type"], evidence_graph)
            meson_identity["dependency_state"]["plan_profile_sha256"] = profile_sha
            config_key = retention._dual_graph_plan_bound_configuration_key(meson_identity, profile_sha)
            frozen = {"configuration_profile_sha256": profile_sha,
                      "config_key_sha256": config_key,
                      "meson_identity": meson_identity}
            graph["prebuild_identity"] = frozen
            graph_keys[graph_id] = {"graph_id": graph_id, "config_key_sha256": config_key,
                                    "meson_identity": meson_identity}
            manifest_graphs.append({
                "graph_id": graph_id,
                "configuration_identity": retention._dual_graph_configuration_identity(meson_identity),
                "prebuild_identity": frozen,
                "prebuild_identity_sha256": hashlib.sha256(
                    retention._canonical_json(frozen)).hexdigest(),
            })

        pair_key = retention._dual_graph_configuration_pair_sha256(graph_keys)
        plan["build_configuration_key_sha256"] = pair_key
        evidence_files = []
        for directory, _dirnames, filenames in os.walk(evidence_root, followlinks=False):
            for name in filenames:
                path = Path(directory) / name
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
                    raise AssertionError(f"synthetic prebuild evidence is not a private file: {path}")
                evidence_files.append({
                    "path": path.relative_to(evidence_root).as_posix(),
                    "sha256": retention._hash_path(path), "size_bytes": info.st_size,
                })
        evidence_files.sort(key=lambda row: row["path"])
        preflight = {
            "schema_version": 1, "status": "PASS", "mode": "SYNTHETIC_FIXTURE",
            "configure_graph_ids": list(retention.BUILD_SESSION_DUAL_GRAPH_IDS),
            "generator_graph_ids": list(retention.BUILD_SESSION_DUAL_GRAPH_IDS),
            "generated_output_rows": [
                {"graph_id": graph_id, "build_relative_path": path,
                 "sha256": hashlib.sha256(contents).hexdigest()}
                for graph_id in retention.BUILD_SESSION_DUAL_GRAPH_IDS
                for path, contents in generated_payloads[graph_id].items()
            ],
            "executed_production_targets": [],
        }
        manifest = {"schema_version": 1, "status": "PASS", "preflight": preflight,
                    "graphs": manifest_graphs, "graph_pair_key_sha256": pair_key,
                    "evidence_files": evidence_files}
        manifest_raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
        manifest_path = artifact_root / "prebuild-manifest.json"
        manifest_path.write_bytes(manifest_raw)
        plan["prebuild_evidence"] = {
            "manifest_path": manifest_path.relative_to(self.repo).as_posix(),
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
            "canonical_sha256": hashlib.sha256(retention._canonical_json(manifest)).hexdigest(),
            "evidence_root": evidence_root.relative_to(self.repo).as_posix(),
        }
        return pair_key

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
                graph = retention._session_dual_graph_by_id(
                    session.plan, step["graph_id"])
                graph_root = root / graph["build_directory"]
                for directory in ("meson-info", "meson-logs", "meson-private"):
                    (graph_root / directory).mkdir(parents=True, exist_ok=True)
                is_windows = graph["graph_role"] == "WINDOWS_PRODUCT"
                option_rows = _synthetic_dual_graph_option_rows(
                    is_windows, session.plan["build_type"])
                compiler_identity = {}
                for machine in ("build", "host"):
                    mingw = is_windows and machine == "host"
                    c_tool = session.plan["tools"]["mingw_cc" if mingw else "native_cc"]
                    cpp_tool = session.plan["tools"]["mingw_cxx" if mingw else "native_cxx"]
                    compiler_id = "gcc" if mingw else "clang"
                    linker_id = "ld.bfd" if mingw else "ld64"
                    compiler_identity[machine] = {
                        "c": {"id": compiler_id, "version": "16.2.0" if mingw else "27.0",
                              "full_version": "synthetic GCC 16.2.0" if mingw else "synthetic Apple clang",
                              "exelist": [Path(c_tool["path"]).name],
                              "linker_exelist": [Path(c_tool["path"]).name], "linker_id": linker_id},
                        "cpp": {"id": compiler_id, "version": "16.2.0" if mingw else "27.0",
                                "full_version": "synthetic GCC 16.2.0" if mingw else "synthetic Apple clang",
                                "exelist": [Path(cpp_tool["path"]).name],
                                "linker_exelist": [Path(cpp_tool["path"]).name], "linker_id": linker_id},
                    }
                (graph_root / "meson-info/intro-buildoptions.json").write_text(
                    json.dumps(option_rows), encoding="utf-8")
                (graph_root / "meson-info/intro-compilers.json").write_text(
                    json.dumps(compiler_identity), encoding="utf-8")
                (graph_root / "meson-info/intro-dependencies.json").write_text("[]", encoding="utf-8")
                source_root = session.plan["source_identity"]["root"]
                log_lines = [f"Source dir: {source_root}", f"Build dir: {graph_root}",
                             "Build type: cross build" if is_windows else "Build type: native build",
                             "Build linker: ld64",
                             "Host linker: ld.bfd" if is_windows else "Host linker: ld64"]
                if is_windows:
                    log_lines.append(f"cross_file = ['{source_root}/build-win64.txt']")
                (graph_root / "meson-logs/meson-log.txt").write_text(
                    "\n".join(log_lines) + "\n", encoding="utf-8")
                command_lines = ["[options]", "backend = ninja",
                                 f"buildtype = {session.plan['build_type']}",
                                 "wrap_mode = nodownload", "prefix = /usr"]
                if not is_windows:
                    command_lines.extend([
                        "enable_dxgi = true", "enable_d3d11 = true", "enable_d3d8 = false",
                        "enable_d3d9 = false", "enable_d3d10 = false", "native_sdl2 = enabled",
                        "native_sdl3 = disabled", "native_glfw = disabled"])
                if is_windows:
                    command_lines.extend(["", "[properties]",
                                          f"cross_file = ['{source_root}/build-win64.txt']"])
                else:
                    command_lines.extend(["", "[properties]"])
                (graph_root / "meson-private/cmd_line.txt").write_text(
                    "\n".join(command_lines) + "\n", encoding="utf-8")
                (graph_root / "build.ninja").write_text("synthetic graph\n", encoding="utf-8")
                (graph_root / ".ninja_log").write_text("synthetic execution\n", encoding="utf-8")
                prebuild_identity = graph.get("prebuild_identity")
                meson_identity = (prebuild_identity.get("meson_identity")
                                  if isinstance(prebuild_identity, dict) else None)
                shader_rows = (meson_identity.get("generated_shader_identity")
                               if isinstance(meson_identity, dict) else None)
                if not isinstance(shader_rows, list) or not shader_rows:
                    raise AssertionError("synthetic dual graph is missing frozen generated shader rows")
                for shader in shader_rows:
                    if not isinstance(shader, dict) or not isinstance(shader.get("retained_path"), str):
                        raise AssertionError("synthetic frozen shader row lacks its retained prebuild bytes")
                    shader_target = graph_root / shader["build_relative_path"]
                    shader_target.parent.mkdir(parents=True, exist_ok=True)
                    shader_target.write_bytes(Path(shader["retained_path"]).read_bytes())
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

    @_with_synthetic_buildlease_runtime_gate
    def test_r91_real_host_workspace_transition_strict_capacity_and_normal_detach(self) -> None:
        """Exercise real hdiutil attach, strict identity accounting, and normal retirement."""
        project_cache = self.home / "Library" / "Caches" / "FGMetal"
        project_cache.mkdir(mode=0o700, parents=True)
        for patcher in (
                mock.patch.object(retention, "CACHE_ROOT", project_cache),
                mock.patch.object(retention, "BUILD_ROOT", project_cache / "builds"),
                mock.patch.object(storage_policy, "CACHE", project_cache),
                mock.patch.object(Path, "home", return_value=self.home)):
            patcher.start()
            self.addCleanup(patcher.stop)
        lease = self._session_lease(
            "r91-real-host-workspace-transition", owner_experiment="synthetic-test")
        subprocess.run(["/usr/bin/git", "init", "--quiet", str(self.repo)], check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        lease.reserve()
        session = None
        capacity_samples: list[tuple[dict[str, object], dict[str, object]]] = []
        original_r71_classifier = retention._is_r6a_r71_admission_id

        def classify_r71_only_inside_session_start(value: object) -> bool:
            frame = sys._getframe(1)
            while frame is not None:
                caller_session = frame.f_locals.get("self")
                if (value == lease.owner_experiment
                        and frame.f_code.co_name == "_check_limits"
                        and frame.f_locals.get("phase") == "session-start"
                        and getattr(getattr(caller_session, "lease", None), "owner_experiment", None)
                            == lease.owner_experiment):
                    # This real-host fixture has no signed R6-A admission capability.
                    # Route only the session-start accounting branch through R71's
                    # strict post-attach validation so the test observes that exact call.
                    return True
                frame = frame.f_back
            return original_r71_classifier(value)

        def record_capacity_sample(**kwargs: object) -> dict[str, object]:
            measured = _ORIGINAL_MEASURE_PROJECT_USAGE(**kwargs)
            capacity_samples.append((dict(kwargs), measured))
            return measured

        try:
            with (self._real_session_runtime(),
                  mock.patch.object(retention, "_is_r6a_r71_admission_id",
                                    side_effect=classify_r71_only_inside_session_start),
                  mock.patch.object(storage_policy, "measure_project_usage",
                                    side_effect=record_capacity_sample),
                  mock.patch.object(storage_policy, "disk_free_bytes",
                                    side_effect=lambda path=storage_policy.REPO:
                                    os.statvfs(Path(path)).f_bavail
                                    * os.statvfs(Path(path)).f_frsize)):
                session = lease.start_session()
                record = retention._load_lease(lease.build_id)
                saved_identity = record["session_v3"]["workspace_mount_identity"]
                self.assertTrue(saved_identity["attached"])
                self.assertEqual(saved_identity["image_path"], str(lease.path /
                                 retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME))
                self.assertEqual(saved_identity["mountpoint"], str(lease.path /
                                 retention.BUILD_SESSION_WORKSPACE_NAME))
                workspace_fd = retention._open_dir(Path(saved_identity["mountpoint"]))
                try:
                    self.assertEqual(storage_policy._mountpoint_from_fd(workspace_fd),
                                     saved_identity["mountpoint"])
                finally:
                    os.close(workspace_fd)
                self.assertEqual(record["session_v3"]["next_step_index"], 0)
                self.assertEqual(record["session_v3"]["step_receipts"], [])
                self.assertEqual(record["session_v3"].get("graph_outputs", {}), {})
                self.assertEqual(len(capacity_samples), 1)
                capacity_kwargs, capacity_row = capacity_samples[0]
                self.assertIs(capacity_kwargs["strict"], True)
                self.assertEqual(capacity_kwargs["exclude_paths"], [lease.path])
                self.assertEqual(capacity_kwargs["active_buildlease_id"], lease.build_id)
                self.assertGreaterEqual(capacity_row["allocated_inode_deduplicated_bytes"], 0)
                self.assertIn("allocated_inode_deduplicated_bytes_excluding_paths", capacity_row)
                self.assertEqual(
                    retention._verified_buildlease_capacity_workspace(lease.build_id),
                    saved_identity)

                # End the host fixture through the ordinary failure/retirement
                # path; no graph command or output is created.
                session._fail("R91 real-host workspace transition validation cleanup", -1,
                              command=["r91-real-host-workspace-validation"])

                final = retention._load_lease(lease.build_id)
                self.assertEqual(final["retirement_status"], "COMPLETED")
                self.assertIs(final["session_v3"]["workspace_mount_identity"]["attached"], False)
                self.assertIsNotNone(
                    final["session_v3"]["workspace_mount_identity"].get("detached_at_unix"))
                self.assertFalse(lease.path.exists())
                self.assertFalse(saved_identity["mountpoint"] and
                                 Path(saved_identity["mountpoint"]).exists())

        finally:
            if lease.path.exists():
                try:
                    record = retention._load_lease(lease.build_id)
                    session_record = record.get("session_v3")
                    if (record.get("state") == "ACTIVE" and session is not None
                            and isinstance(session_record, dict)
                            and session_record.get("state") in {"ACTIVE", "RUNNING"}):
                        with self._real_session_runtime():
                            session._fail(
                                "R91 real-host workspace validation cleanup", -1,
                                command=["r91-real-host-workspace-validation"])
                    if lease.path.exists():
                        record = retention._load_lease(lease.build_id)
                        session_record = record.get("session_v3")
                        identity = (session_record.get("workspace_mount_identity")
                                    if isinstance(session_record, dict) else None)
                        if (isinstance(identity, dict) and identity.get("attached") is True
                                and retention._session_image_row(lease.path) is not None):
                            with self._real_session_runtime():
                                _ORIGINAL_DETACH_SESSION_WORKSPACE(lease.path, identity)
                except BaseException:
                    self._preserve_temp_root()
                    raise
            if lease.lock_fds:
                lease._stop_heartbeat()
                lease._release_lock()
        print("R91_REAL_HOST_WORKSPACE_TRANSITION " + json.dumps({
            "status": "PASS",
            "build_id": lease.build_id,
            "workspace_identity": saved_identity,
            "strict_scoped_bytes": capacity_row["allocated_inode_deduplicated_bytes"],
            "retirement_status": final["retirement_status"],
            "workspace_cleanup_status": "NORMAL_DETACH_VERIFIED",
            "graph_step_index": final["session_v3"]["next_step_index"],
            "outputs_accepted": False,
        }, sort_keys=True))

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

    def _start_r71_session(self, build_id: str) -> tuple[retention.BuildLease, retention.BuildLeaseSession]:
        # Recovery tests exercise the shared failure path. Keep R71 capability
        # validation out of this synthetic fixture; admission evidence is
        # injected explicitly by the individual failure tests below.
        return self._start_session(build_id)

    def _bind_session_to_synthetic_r71(self, lease: retention.BuildLease,
                                       dependency_sha: str = "9" * 64) -> str:
        """Attach synthetic R71 admission identity to an already-reserved test session."""
        admission_id = f"r6a-r71-admission-{lease.build_id}"
        record = retention._load_lease(lease.build_id)
        record.update({"owner_experiment": admission_id,
                       "r71_execution_dependency_manifest_sha256": dependency_sha,
                       "r71_dependency_manifest_path":
                           "experiments/storage/R6-A-R71-EXECUTION-DEPENDENCY-MANIFEST.json"})
        retention._store_lease(record)
        root_fd = retention._open_dir(lease.path)
        try:
            marker = retention._read_marker(root_fd)
            marker.update(record)
            retention._write_marker(root_fd, marker)
            os.fsync(root_fd)
        finally:
            os.close(root_fd)
        lease.owner_experiment = admission_id
        return dependency_sha

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

    def test_canonical_artifact_audit_reuses_held_retention_lock(self) -> None:
        lock_fds = retention._lock()
        try:
            with (mock.patch.object(retention, "canonical_artifact_digests", return_value=[]),
                  mock.patch.object(retention, "_lock",
                                    side_effect=AssertionError("CAS audit reacquired retention lock"))):
                result = retention.canonical_artifact_store_audit(
                    retention_lock_fds=lock_fds)
            self.assertEqual(result["artifact_count"], 0)
            self.assertEqual(result["shared_or_recovering_payload_count"], 0)
        finally:
            retention._unlock(*lock_fds)

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

    def test_cache_root_scan_skips_only_the_exact_active_buildlease_workspace(self) -> None:
        cache_root = retention.HOME / "Library/Caches/FGMetalStep11D-R"
        cache_root.mkdir(parents=True)
        workspace = cache_root / retention.BUILD_SESSION_WORKSPACE_NAME
        workspace.mkdir()
        root_fd = retention._open_dir(cache_root)
        try:
            root_info = os.fstat(root_fd)
            root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        finally:
            os.close(root_fd)
        identity = {
            "mountpoint": str(workspace),
            "device": root_info.st_dev,
            "filesystem_id": root_fsid,
            "mountpoint_dir_device": workspace.stat().st_dev,
            "mountpoint_dir_inode": workspace.stat().st_ino,
            "mounted_mountpoint_device": workspace.stat().st_dev,
            "mounted_mountpoint_inode": workspace.stat().st_ino,
        }
        original_mountpoint = storage_policy._mountpoint_from_fd

        def descriptor_mountpoint(fd: int) -> str:
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) == (workspace.stat().st_dev, workspace.stat().st_ino):
                return str(workspace)
            return original_mountpoint(fd)

        with (mock.patch.object(retention, "_verified_buildlease_capacity_workspace",
                                return_value=dict(identity)),
              mock.patch.object(storage_policy, "_mountpoint_from_fd",
                                side_effect=descriptor_mountpoint)):
            candidates = retention._all_root_candidates(
                active_buildlease_id="r6a-r87-dual-lineage-r92-root-scan",
                active_workspace_identity=identity)

        self.assertNotIn(workspace, candidates)

    def test_cache_root_scan_rejects_unrelated_mount_even_if_readonly_cache_image_is_claimed(self) -> None:
        cache_root = retention.HOME / "Library/Caches/FGMetalStep11D-R"
        cache_root.mkdir(parents=True)
        workspace = cache_root / retention.BUILD_SESSION_WORKSPACE_NAME
        unrelated = cache_root / "unrelated-same-filesystem-mount"
        workspace.mkdir()
        unrelated.mkdir()
        root_fd = retention._open_dir(cache_root)
        try:
            root_info = os.fstat(root_fd)
            root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
        finally:
            os.close(root_fd)
        identity = {
            "mountpoint": str(workspace),
            "device": root_info.st_dev,
            "filesystem_id": root_fsid,
            "mountpoint_dir_device": workspace.stat().st_dev,
            "mountpoint_dir_inode": workspace.stat().st_ino,
            "mounted_mountpoint_device": workspace.stat().st_dev,
            "mounted_mountpoint_inode": workspace.stat().st_ino,
        }
        original_mountpoint = storage_policy._mountpoint_from_fd
        mountpoint_by_identity = {
            (workspace.stat().st_dev, workspace.stat().st_ino): str(workspace),
            (unrelated.stat().st_dev, unrelated.stat().st_ino): str(unrelated),
        }

        def descriptor_mountpoint(fd: int) -> str:
            info = os.fstat(fd)
            return mountpoint_by_identity.get(
                (info.st_dev, info.st_ino), original_mountpoint(fd))

        with (mock.patch.object(retention, "_verified_buildlease_capacity_workspace",
                                return_value=dict(identity)),
              mock.patch.object(storage_policy, "_mountpoint_from_fd",
                                side_effect=descriptor_mountpoint),
              mock.patch.object(retention, "_known_readonly_cache_image_mount",
                                return_value=True) as known_readonly_image):
            with self.assertRaisesRegex(retention.RetentionError, "unknown filesystem transition"):
                retention._all_root_candidates(
                    active_buildlease_id="r6a-r87-dual-lineage-r91-root-scan",
                    active_workspace_identity=identity)
        known_readonly_image.assert_not_called()

    def test_active_r71_snapshot_passes_exact_workspace_to_project_cache_scan(self) -> None:
        build_id = "r6a-r87-dual-lineage-r92-snapshot-handoff"
        workspace_identity = {
            "mountpoint": str(self.cache / "builds" / build_id / "workspace"),
            "image_path": str(self.cache / "builds" / build_id / "workspace.sparseimage"),
            "attached": True,
        }
        with (mock.patch.object(retention, "_verified_buildlease_capacity_workspace",
                                return_value=workspace_identity) as verify_workspace,
              mock.patch.object(retention, "_all_root_candidates", return_value=[]) as scan_roots,
              mock.patch.object(retention, "_lease_files", return_value=[]),
              mock.patch.object(retention, "_r17_missing_root_rows", return_value=[]),
              mock.patch.object(retention, "_classified_non_retention_tmp_roots", return_value=[]),
              mock.patch.object(retention, "_read_r17_root_a_loss_disposition", return_value=None)):
            state = retention._snapshot(
                protected_roots=[], active_buildlease_id=build_id)

        verify_workspace.assert_called_once_with(build_id)
        scan_roots.assert_called_once_with(
            active_buildlease_id=build_id,
            active_workspace_identity=workspace_identity)
        self.assertEqual(state["active"], [])

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

    def test_host_v3_integration_cleanup_failure_is_propagated_and_preserves_temp_root(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-host-v3-cleanup-regression-", dir="/private/tmp")
        root = Path(temp.name).resolve(strict=True)
        image = root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        image.write_bytes(b"synthetic image marker")
        attached_row = {"writeable": True}
        record = {"session_v3": {"workspace_mount_identity": {"attached": True}}}
        preserve_called = False

        def preserve_temp() -> None:
            nonlocal preserve_called
            preserve_called = True
            temp._finalizer.detach()

        try:
            with (mock.patch.object(retention, "_session_image_row", return_value=attached_row),
                  mock.patch(__name__ + "._ORIGINAL_DETACH_SESSION_WORKSPACE",
                             return_value={"attached": True})):
                with self.assertRaisesRegex(retention.RetentionError, "detach was not confirmed"):
                    _cleanup_host_v3_integration_workspace(root, record, preserve_temp)
            self.assertTrue(preserve_called)
            self.assertTrue(root.exists())
            self.assertTrue(image.exists())
            self.assertIsNone(temp._finalizer())
        finally:
            if root.exists():
                shutil.rmtree(root)

    def test_host_v3_cleanup_uses_recorded_identity_when_backing_path_is_missing(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-host-v3-missing-image-", dir="/private/tmp")
        root = Path(temp.name).resolve(strict=True)
        image = root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        image.write_bytes(b"synthetic image marker")
        workspace = root / retention.BUILD_SESSION_WORKSPACE_NAME
        workspace.mkdir(mode=0o500)
        image_info = image.stat(follow_symlinks=False)
        workspace_info = workspace.stat(follow_symlinks=False)
        identity = {
            "attached": False,
            "image_path": str(image),
            "image_device": image_info.st_dev,
            "image_inode": image_info.st_ino,
            "mountpoint": str(workspace),
            "mountpoint_dir_device": workspace_info.st_dev,
            "mountpoint_dir_inode": workspace_info.st_ino,
        }
        image.unlink()
        record = {"session_v3": {"detached_workspace_identity": identity}}
        preserve_called = False

        def preserve_temp() -> None:
            nonlocal preserve_called
            preserve_called = True
            temp._finalizer.detach()

        try:
            with (mock.patch.object(retention, "_session_image_row", return_value=None),
                  mock.patch.object(retention, "_verify_session_workspace_detached",
                                    wraps=retention._verify_session_workspace_detached) as verify,
                  mock.patch(__name__ + "._ORIGINAL_DETACH_SESSION_WORKSPACE") as detach,
                  mock.patch(__name__ + "._ORIGINAL_DETACH_SESSION_WORKSPACE_READONLY") as detach_readonly):
                with self.assertRaises(FileNotFoundError):
                    _cleanup_host_v3_integration_workspace(root, record, preserve_temp)
            verify.assert_called_once_with(root, identity)
            detach.assert_not_called()
            detach_readonly.assert_not_called()
            self.assertTrue(preserve_called)
            self.assertTrue(root.exists())
            self.assertFalse(image.exists())
            self.assertIsNone(temp._finalizer())
        finally:
            if root.exists():
                shutil.rmtree(root)

    def test_host_v3_cleanup_verifies_persisted_detached_rw_and_ro_identities(self) -> None:
        for detached_key in ("detached_workspace_identity", "readonly_detached_identity"):
            with self.subTest(detached_identity=detached_key):
                temp = tempfile.TemporaryDirectory(prefix="fgmetal-host-v3-detached-", dir="/private/tmp")
                root = Path(temp.name).resolve(strict=True)
                rw_identity = {"attached": True, "kind": "stale-rw-mount"}
                ro_identity = {"attached": True, "kind": "stale-ro-mount"}
                rw_detached = {"attached": False, "kind": "persisted-rw-detached"}
                ro_detached = {"attached": False, "kind": "persisted-ro-detached"}
                expected = ro_detached if detached_key == "readonly_detached_identity" else rw_detached
                session_record = {
                    "workspace_mount_identity": rw_identity,
                    "readonly_mount_identity": ro_identity,
                    detached_key: expected,
                }
                record = {"session_v3": session_record}
                verified: list[tuple[Path, dict[str, object]]] = []

                def verify(actual_root: Path, identity: dict[str, object]) -> dict[str, object]:
                    verified.append((actual_root, identity))
                    return identity

                try:
                    with (mock.patch.object(retention, "_session_image_row", return_value=None),
                          mock.patch.object(retention, "_verify_session_workspace_detached",
                                            side_effect=verify),
                          mock.patch(__name__ + "._ORIGINAL_DETACH_SESSION_WORKSPACE") as detach,
                          mock.patch(__name__ + "._ORIGINAL_DETACH_SESSION_WORKSPACE_READONLY") as detach_readonly):
                        _cleanup_host_v3_integration_workspace(
                            root, record, lambda: temp._finalizer.detach())
                    self.assertEqual(verified, [(root, expected)])
                    detach.assert_not_called()
                    detach_readonly.assert_not_called()
                finally:
                    if root.exists():
                        shutil.rmtree(root)
                    temp.cleanup()

    def test_conditional_temp_cleanup_callback_preserves_marked_root(self) -> None:
        root = Path(self.temp.name).resolve(strict=True)
        self._preserve_temp_root()
        self._cleanup_temporary_directory()
        self.assertTrue(root.exists())
        shutil.rmtree(root)

    def test_missing_host_v3_root_requires_verified_successful_retirement(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-host-v3-retired-root-", dir="/private/tmp")
        root = Path(temp.name).resolve(strict=True) / "retired-build"
        receipt = {"retirement_status": "COMPLETED", "execution_outcome": "SUCCEEDED",
                   "outputs_accepted": True}
        image_path = str(root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME)
        mountpoint = str(root / retention.BUILD_SESSION_WORKSPACE_NAME)
        rw_detached = {"attached": False, "image_path": image_path, "mountpoint": mountpoint,
                       "image_device": 7, "image_inode": 11}
        ro_detached = dict(rw_detached)
        record = {
            "state": "COMPLETED",
            "completion_contract": "HOST_BUILDLEASE_V3_MANIFEST_FIRST_V1",
            "session_v3": {
                "executor_mode": retention.BUILD_SESSION_EXECUTOR_MODE,
                "state": "ACCEPTED",
                "detached_workspace_identity": rw_detached,
                "readonly_detached_identity": ro_detached,
            },
        }
        unrelated_image = {"image-path": "/synthetic/other.sparseimage",
                           "system-entities": [{"mount-point": "/synthetic/other-mount"}]}
        try:
            self.assertFalse(root.exists())
            with (mock.patch.object(retention, "_verify_completed_retirement", return_value=receipt) as verify,
                  mock.patch.object(retention, "_session_hdiutil_info", return_value=[unrelated_image]) as hdiutil):
                self.assertIs(_verify_auto_retired_host_v3_workspace(root, record), receipt)
            verify.assert_called_once_with(record)
            hdiutil.assert_called_once_with()
            self.assertFalse(root.exists())

            attached_rows = (
                ("exact image path", [{"image-path": image_path, "system-entities": []}],
                 "backing image is still attached"),
                ("exact mountpoint", [{"image-path": "/synthetic/other.sparseimage",
                                        "system-entities": [{"mount-point": mountpoint}]}],
                 "mount point is still attached"),
                ("malformed entity", [{"image-path": "/synthetic/other.sparseimage",
                                       "system-entities": [None]}],
                 "malformed image entities"),
            )
            for label, rows, message in attached_rows:
                with self.subTest(inventory_case=label), \
                        mock.patch.object(retention, "_verify_completed_retirement", return_value=receipt), \
                        mock.patch.object(retention, "_session_hdiutil_info", return_value=rows):
                    with self.assertRaisesRegex(retention.RetentionError, message):
                        _verify_auto_retired_host_v3_workspace(root, record)
        finally:
            temp.cleanup()

    def test_missing_host_v3_root_without_retirement_proof_fails_before_hdiutil_check(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-host-v3-unproven-root-", dir="/private/tmp")
        root = Path(temp.name).resolve(strict=True) / "missing-build"
        try:
            self.assertFalse(root.exists())
            with (mock.patch.object(retention, "_verify_completed_retirement",
                                    side_effect=retention.RetentionError("missing retirement proof")) as verify,
                  mock.patch.object(retention, "_session_hdiutil_info") as hdiutil):
                with self.assertRaisesRegex(retention.RetentionError, "missing retirement proof"):
                    _verify_auto_retired_host_v3_workspace(root, {})
            verify.assert_called_once_with({})
            hdiutil.assert_not_called()
        finally:
            temp.cleanup()

    @unittest.skipUnless(
        retention.sys.platform == "darwin"
        and Path("/usr/bin/hdiutil").is_file()
        and Path("/usr/bin/sandbox-exec").is_file(),
        "fixed-capacity workspace integration requires Darwin hdiutil and Seatbelt")
    def test_small_fixed_capacity_workspace_returns_enospc(self) -> None:
        root = self.repo / "small-fixed-image-root"
        root.mkdir(mode=0o700)
        identity: dict[str, object] | None = None
        observed_error: OSError | None = None
        primary_error: BaseException | None = None
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
        except BaseException as error:
            primary_error = error

        cleanup_error: BaseException | None = None
        if identity is not None and identity.get("attached") is True:
            try:
                _ORIGINAL_DETACH_SESSION_WORKSPACE(root, identity)
            except BaseException as error:
                cleanup_error = error
        if cleanup_error is None and root.exists():
            try:
                if retention._session_image_row(root) is not None:
                    raise retention.RetentionError("fixed-capacity workspace image remains attached")
                shutil.rmtree(root)
            except BaseException as error:
                cleanup_error = error

        if cleanup_error is not None:
            # Retain the complete fixture root if its normal identity-bound
            # cleanup did not finish; TemporaryDirectory would otherwise erase it.
            self.temp._finalizer.detach()
            if primary_error is not None:
                raise retention.RetentionError(
                    "fixed-capacity workspace primary failure: "
                    f"{primary_error}; observed write error: {observed_error}; "
                    f"normal cleanup failure: {cleanup_error}") from primary_error
            if observed_error is not None and observed_error.errno == errno.ENOSPC:
                raise retention.RetentionError(
                    "expected workspace ENOSPC occurred; normal cleanup failed: "
                    f"{cleanup_error}") from observed_error
            raise cleanup_error
        if primary_error is not None:
            raise primary_error

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
        oracle_read_open = True
        oracle_write_open = True

        def add_temp_preserving_cleanup(cleanup):
            def run_cleanup() -> None:
                try:
                    cleanup()
                except BaseException:
                    self._preserve_temp_root()
                    raise
            self.addCleanup(run_cleanup)

        def close_oracle_read() -> None:
            nonlocal oracle_read_open
            if oracle_read_open:
                try:
                    os.close(oracle_read)
                finally:
                    oracle_read_open = False

        def close_oracle_write() -> None:
            nonlocal oracle_write_open
            if oracle_write_open:
                try:
                    os.close(oracle_write)
                finally:
                    oracle_write_open = False

        add_temp_preserving_cleanup(close_oracle_read)
        add_temp_preserving_cleanup(close_oracle_write)
        os.set_inheritable(oracle_write, True)
        child_pids: list[int] = []
        controller_processes: list[subprocess.Popen] = []
        original_popen = subprocess.Popen

        def close_child_pids() -> None:
            for pid in child_pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        add_temp_preserving_cleanup(close_child_pids)
        add_temp_preserving_cleanup(lease._stop_heartbeat)
        add_temp_preserving_cleanup(lease._release_lock)

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
                started = True
                lease.reserve()
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
                close_oracle_write()
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
            self.assertEqual(terminal["state"], "FINALIZING")
            self.assertEqual(terminal["executor_mode"], "HOST_BUILDLEASE_V3")
            self.assertEqual(terminal["executor_version"], retention.BUILD_SESSION_SCHEMA_VERSION)
            self.assertEqual(terminal["acceptance_state"], "UNACCEPTED_FINALIZING_MANIFEST")
            self.assertEqual(terminal["output_tree_sha256"], observations["readonly_tree_before"][0])
            accepted_terminal_path = retention._completion_terminal_path(build_id)
            accepted_terminal = retention._read_json(accepted_terminal_path, sealed=True)
            accepted_lease = retention._load_lease(build_id)
            self.assertEqual(accepted_terminal["state"], "SUCCESS")
            self.assertEqual(accepted_terminal["build_manifest_id"], manifest["build_manifest_id"])
            self.assertEqual(accepted_terminal["build_manifest_sha256"],
                             accepted_lease["build_manifest_sha256"])
            self.assertEqual(retention._hash_path(accepted_terminal_path),
                             accepted_lease["completion_terminal_sha256"])
            self.assertEqual(manifest["build_identity"]["sandboxed_execution_executor_mode"],
                             "HOST_BUILDLEASE_V3")
            self.assertEqual(manifest["build_identity"]["source_identity"]["content_tree_sha256"],
                             pre_source_hash)
            self.assertEqual(observations["readonly_image_before"], observations["readonly_image_after"])
        finally:
            try:
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
                close_oracle_write()
                if started:
                    record = retention._load_lease(build_id)
                    if lease.path.exists():
                        _cleanup_host_v3_integration_workspace(
                            lease.path, record, self._preserve_temp_root)
                    else:
                        _verify_auto_retired_host_v3_workspace(lease.path, record)
            except BaseException:
                self._preserve_temp_root()
                raise

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
        original_build_manifest = retention._build_manifest
        build_observations: dict[str, object] = {}

        def build_manifest_while_unaccepted(*args: object, **kwargs: object) -> tuple[dict, str]:
            manifest, digest = original_build_manifest(*args, **kwargs)
            terminal = retention._read_json(
                session.session_dir / retention.BUILD_SESSION_TERMINAL, sealed=True)
            current = retention._load_lease(lease.build_id)
            self.assertEqual(terminal["state"], "FINALIZING")
            self.assertEqual(current["state"], "ACTIVE")
            self.assertEqual(current["session_v3"]["state"], "FINALIZING")
            with self.assertRaises(retention.RetentionError):
                retention._verify_build_manifest(lease.build_id)
            self.assertIsNone(retention.find_canonical_equivalent(
                config_key_sha256=manifest["config_key_sha256"], kind=manifest["kind"],
                source_tree_hash=manifest["source_tree_hash_at_lease"]))
            build_observations.update({"manifest": manifest, "digest": digest})
            return manifest, digest

        with mock.patch.object(retention, "_build_manifest", side_effect=build_manifest_while_unaccepted), \
                mock.patch.object(retention, "_retire", retire):
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
        manifest = build_observations["manifest"]
        self.assertEqual(terminal["build_manifest_id"], manifest["build_manifest_id"])
        self.assertEqual(terminal["build_manifest_sha256"], build_observations["digest"])
        self.assertEqual(record["completion_contract"], "HOST_BUILDLEASE_V3_MANIFEST_FIRST_V1")
        self.assertEqual(record["completion_terminal_sha256"], record["session_v3"]["terminal_sha256"])
        completion_terminal_path = retention._completion_terminal_path(lease.build_id)
        self.assertEqual(retention._hash_path(completion_terminal_path), record["completion_terminal_sha256"])
        retained_terminal = manifest["build_identity"]["sandboxed_execution_evidence"]["retained_path"]
        self.assertEqual(retention._read_json(Path(retained_terminal), sealed=True)["state"], "FINALIZING")

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_completion_binds_dependency_manifest_before_success(self) -> None:
        lease, session = self._start_session("r71-finalize-manifest-binding")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        dependency_sha = self._bind_session_to_synthetic_r71(lease)
        original_build_manifest = retention._build_manifest
        observations: dict[str, object] = {}

        def build_manifest_while_r71_unaccepted(*args: object, **kwargs: object) -> tuple[dict, str]:
            manifest, digest = original_build_manifest(*args, **kwargs)
            terminal = retention._read_json(
                session.session_dir / retention.BUILD_SESSION_TERMINAL, sealed=True)
            self.assertEqual(terminal["state"], "FINALIZING")
            self.assertEqual(terminal["acceptance_state"], "UNACCEPTED_FINALIZING_MANIFEST")
            self.assertEqual(terminal["r71_execution_dependency_manifest_sha256"], dependency_sha)
            self.assertEqual(manifest["build_identity"]["r71_execution_dependency_manifest_sha256"],
                             dependency_sha)
            observations.update({"manifest": manifest, "digest": digest})
            return manifest, digest

        with (mock.patch.object(lease, "_revalidate_r71_admission") as revalidate,
              mock.patch.object(retention, "_build_manifest",
                                side_effect=build_manifest_while_r71_unaccepted),
              mock.patch.object(retention, "_retire", return_value={"result": "synthetic retirement"})):
            result = session.finalize()
        self.assertEqual(result["status"], "COMPLETED_AND_AUTO_RETIRED")
        self.assertGreaterEqual(revalidate.call_count, 2,
                                "R71 dependency closure is rechecked at finalization boundaries")
        terminal = retention._read_json(session.session_dir / retention.BUILD_SESSION_TERMINAL, sealed=True)
        self.assertEqual(terminal["state"], "SUCCESS")
        self.assertEqual(terminal["r71_execution_dependency_manifest_sha256"], dependency_sha)
        manifest = observations["manifest"]
        self.assertEqual(manifest["build_identity"]["r71_execution_dependency_manifest_sha256"],
                         dependency_sha)

    def test_r87_finalization_budget_charges_before_each_durable_copy(self) -> None:
        budget = retention._FinalizationOutputBudget(
            build_path=Path("/private/tmp/r87-budget/build"),
            allowance_bytes=100_000, expected_build_bytes=64 * retention.GIB)
        with mock.patch.object(budget, "_capacity_admission") as capacity_check:
            budget.reserve(Path("/private/tmp/r87-budget/evidence/a.bin"), 4097, "first copy")
            remaining_after_first = 100_000 - (8192 + retention.FINALIZATION_OUTPUT_FILE_OVERHEAD_BYTES)
            self.assertEqual(budget.remaining_bytes, remaining_after_first)
            with self.assertRaisesRegex(retention.RetentionError, "allowance exceeded before second copy"):
                budget.reserve(Path("/private/tmp/r87-budget/evidence/b.bin"), 1, "second copy")
            self.assertEqual(budget.remaining_bytes, remaining_after_first,
                             "an over-cap write must be refused before charging or writing")
        capacity_check.assert_called_once_with(peak_output_bytes=100_000, where="first copy")

    def test_r87_finalization_capacity_replaces_live_build_allocation_with_signed_reservation(self) -> None:
        budget = retention._FinalizationOutputBudget(
            build_path=Path("/private/tmp/r87-capacity/build"),
            allowance_bytes=retention.GIB, expected_build_bytes=3 * retention.GIB)
        live_scope = 50 * retention.GIB
        build_usage = 2 * retention.GIB
        with (mock.patch.object(storage_policy, "disk_free_bytes", return_value=80 * retention.GIB),
              mock.patch.object(storage_policy, "measure_project_usage", return_value={}),
              mock.patch.object(storage_policy, "conservative_project_usage_bytes",
                                return_value=live_scope),
              mock.patch.object(retention, "_session_measure_tree",
                                return_value={"allocated_bytes": build_usage}),
              mock.patch.object(storage_policy, "projected_storage_admission",
                                return_value={"admitted": True}) as admission):
            budget._capacity_admission(peak_output_bytes=retention.GIB,
                                       where="synthetic finalization")
        arguments = admission.call_args.kwargs
        self.assertEqual(arguments["current_scoped_bytes"], live_scope - build_usage)
        self.assertEqual(
            arguments["peak_additional_bytes"],
            4 * retention.GIB + storage_policy.admission_uncertainty_margin_bytes(4 * retention.GIB))

    def test_r87_finalization_capacity_refuses_build_usage_beyond_signed_reservation(self) -> None:
        budget = retention._FinalizationOutputBudget(
            build_path=Path("/private/tmp/r87-capacity-overrun/build"),
            allowance_bytes=retention.GIB, expected_build_bytes=3 * retention.GIB)
        with (mock.patch.object(storage_policy, "disk_free_bytes", return_value=80 * retention.GIB),
              mock.patch.object(storage_policy, "measure_project_usage", return_value={}),
              mock.patch.object(storage_policy, "conservative_project_usage_bytes",
                                return_value=50 * retention.GIB),
              mock.patch.object(retention, "_session_measure_tree",
                                return_value={"allocated_bytes": 3 * retention.GIB + 1}),
              mock.patch.object(storage_policy, "projected_storage_admission") as admission):
            with self.assertRaisesRegex(retention.RetentionError,
                                        "exceeded its signed reservation"):
                budget._capacity_admission(peak_output_bytes=retention.GIB,
                                           where="synthetic finalization")
        admission.assert_not_called()

    def test_r87_finalization_budget_fails_closed_on_capacity_drift_before_charge(self) -> None:
        budget = retention._FinalizationOutputBudget(
            build_path=Path("/private/tmp/r87-capacity/build"),
            allowance_bytes=1 * retention.GIB, expected_build_bytes=64 * retention.GIB)
        with mock.patch.object(budget, "_capacity_admission",
                               side_effect=retention.RetentionError("capacity refusal")):
            with self.assertRaisesRegex(retention.RetentionError, "capacity refusal"):
                budget.reserve(Path("/private/tmp/r87-capacity/evidence/output"), 1024,
                               "output capture")
        self.assertEqual(budget.remaining_bytes, retention.GIB,
                         "a rejected capacity check must not permit a durable write")

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_bound_dependency_mutation_after_finalizing_blocks_manifest_publication(self) -> None:
        import r6a_dependency_manifest

        lease, session = self._start_session("r71-finalizing-dependency-drift")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        dependency_sha = self._bind_session_to_synthetic_r71(lease)
        dependency_file = self.repo / "synthetic-r71-bound-input.txt"
        dependency_file.write_text("frozen input\n", encoding="utf-8")
        expected_input_sha = r6a_dependency_manifest._snapshot_path(
            str(dependency_file), kind="file", role="synthetic-bound-input")["sha256"]
        revalidation_calls = 0
        terminal_path = session.session_dir / retention.BUILD_SESSION_TERMINAL
        original_write_json = retention._write_json

        def validate_synthetic_bound_input() -> None:
            nonlocal revalidation_calls
            revalidation_calls += 1
            observed = r6a_dependency_manifest._snapshot_path(
                str(dependency_file), kind="file", role="synthetic-bound-input")["sha256"]
            if observed != expected_input_sha:
                raise retention.RetentionError("synthetic bound dependency changed")

        def mutate_after_finalizing_receipt(path: Path, payload: object, **kwargs: object) -> bytes:
            raw = original_write_json(path, payload, **kwargs)
            if (path == terminal_path and isinstance(payload, dict)
                    and payload.get("state") == "FINALIZING"):
                dependency_file.write_text("mutated after FINALIZING\n", encoding="utf-8")
            return raw

        with (mock.patch.object(lease, "_revalidate_r71_admission",
                                side_effect=validate_synthetic_bound_input),
              mock.patch.object(retention, "_write_json", side_effect=mutate_after_finalizing_receipt),
              mock.patch.object(retention, "_build_manifest", wraps=retention._build_manifest) as build_manifest):
            with self.assertRaisesRegex(retention.RetentionError, "synthetic bound dependency changed"):
                session.finalize()

        self.assertEqual(revalidation_calls, 3,
                         "the third validation must occur after FINALIZING and before manifest construction")
        build_manifest.assert_not_called()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["r71_execution_dependency_manifest_sha256"], dependency_sha)
        self.assertNotEqual(record.get("session_v3", {}).get("state"), "ACCEPTED")
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
        self.assertFalse(retention._completion_terminal_path(lease.build_id).exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_real_validator_rechecks_after_finalizing_before_manifest(self) -> None:
        """Exercise the production R71 verifier at the pre-manifest completion boundary.

        The Host-v3 session uses a simple single-graph fixture, while its
        separately bound R6-A dependency plan still carries the ordered graph
        pair. The command runner and artifact identity are test fixtures; no
        real Gate/admission/execution evidence is created.
        """
        patches = self._r71_validation_boundary_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            lease, capability, _candidate, _plan, _dependency, runner_file = \
                self._r71_real_validator_fixture("real-finalizing-boundary", dual_session=False)
            self.addCleanup(lease._release_lock)
            self.addCleanup(lease._stop_heartbeat)
            lease.reserve(admission_transaction=capability)
            session = lease.start_session()
            self.assertIs(session, lease.session)
            self._finish_session_steps(session)

            terminal_path = session.session_dir / retention.BUILD_SESSION_TERMINAL
            original_write_json = retention._write_json
            mutated = False

            def mutate_after_finalizing(path: Path, payload: object, **kwargs: object) -> bytes:
                nonlocal mutated
                raw = original_write_json(path, payload, **kwargs)
                if (path == terminal_path and isinstance(payload, dict)
                        and payload.get("state") == "FINALIZING"):
                    runner_file.write_text("mutated after FINALIZING\n", encoding="utf-8")
                    mutated = True
                return raw

            with (mock.patch.object(retention, "_write_json", side_effect=mutate_after_finalizing),
                  mock.patch.object(retention, "_build_manifest",
                                    wraps=retention._build_manifest) as build_manifest):
                with self.assertRaisesRegex(
                        retention.RetentionError,
                        "R71 dependency closure changed before session consumption"):
                    session.finalize()

            self.assertTrue(mutated, "fixture did not reach the FINALIZING terminal write")
            build_manifest.assert_not_called()
            record = retention._load_lease(lease.build_id)
            self.assertEqual(record["r71_execution_dependency_manifest_sha256"],
                             capability.r71_validation["dependency_manifest_sha256"])
            self.assertNotEqual(record.get("session_v3", {}).get("state"), "ACCEPTED")
            self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
            self.assertFalse(retention._completion_terminal_path(lease.build_id).exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_completion_terminal_crash_before_lease_acceptance_remains_unaccepted(self) -> None:
        lease, session = self._start_session("r71-crash-after-completion-terminal")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        dependency_sha = self._bind_session_to_synthetic_r71(lease)
        original_write_json = retention._write_json
        completion_terminal_path = retention._completion_terminal_path(lease.build_id)

        def write_completion_terminal_then_stop(path: Path, payload: object, **kwargs: object) -> bytes:
            raw = original_write_json(path, payload, **kwargs)
            if (path == completion_terminal_path and isinstance(payload, dict)
                    and payload.get("state") == "SUCCESS"):
                raise OSError("synthetic interruption after completion terminal publication")
            return raw

        with (mock.patch.object(lease, "_revalidate_r71_admission"),
              mock.patch.object(retention, "_write_json", side_effect=write_completion_terminal_then_stop),
              mock.patch.object(session, "_fail", return_value=None)):
            with self.assertRaisesRegex(OSError, "after completion terminal publication"):
                session.finalize()

        record = retention._load_lease(lease.build_id)
        local_terminal = retention._read_json(
            session.session_dir / retention.BUILD_SESSION_TERMINAL, sealed=True)
        external_terminal = retention._read_json(completion_terminal_path, sealed=True)
        self.assertEqual(record["state"], "ACTIVE")
        self.assertEqual(record["session_v3"]["state"], "FINALIZING")
        self.assertEqual(record["r71_execution_dependency_manifest_sha256"], dependency_sha)
        self.assertEqual(local_terminal["state"], "SUCCESS")
        self.assertEqual(external_terminal["state"], "SUCCESS")
        self.assertEqual(external_terminal["r71_execution_dependency_manifest_sha256"], dependency_sha)
        with self.assertRaisesRegex(retention.RetentionError, "accepted lease completion"):
            retention._verify_build_manifest(lease.build_id)

        lease._release_lock()
        retention._reject_interrupted_host_v3_session(record)
        recovered = retention._load_lease(lease.build_id)
        self.assertEqual(recovered["state"], "FAILED")
        self.assertEqual(recovered["r71_execution_dependency_manifest_sha256"], dependency_sha)
        self.assertFalse(completion_terminal_path.exists())
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_completion_manifest_crash_recovery_keeps_digest_unaccepted(self) -> None:
        lease, session = self._start_session("r71-crash-after-completion-manifest")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        dependency_sha = self._bind_session_to_synthetic_r71(lease)
        original_write_json = retention._write_json
        terminal_path = session.session_dir / retention.BUILD_SESSION_TERMINAL

        def stop_before_success(path: Path, payload: object, **kwargs: object) -> bytes:
            if path == terminal_path and isinstance(payload, dict) and payload.get("state") == "SUCCESS":
                raise OSError("synthetic R71 interruption before SUCCESS")
            return original_write_json(path, payload, **kwargs)

        with (mock.patch.object(lease, "_revalidate_r71_admission"),
              mock.patch.object(retention, "_write_json", side_effect=stop_before_success),
              mock.patch.object(session, "_fail", return_value=None)):
            with self.assertRaisesRegex(OSError, "R71 interruption before SUCCESS"):
                session.finalize()
        record = retention._load_lease(lease.build_id)
        terminal = retention._read_json(terminal_path, sealed=True)
        self.assertEqual(record["r71_execution_dependency_manifest_sha256"], dependency_sha)
        self.assertEqual(record["session_v3"]["state"], "FINALIZING")
        self.assertEqual(terminal["state"], "FINALIZING")
        self.assertEqual(terminal["acceptance_state"], "UNACCEPTED_FINALIZING_MANIFEST")
        self.assertEqual(terminal["r71_execution_dependency_manifest_sha256"], dependency_sha)
        self.assertTrue((retention.MANIFEST_ROOT / f"{lease.build_id}.json").is_file())
        lease._release_lock()
        retention._reject_interrupted_host_v3_session(record)
        recovered = retention._load_lease(lease.build_id)
        self.assertEqual(recovered["state"], "FAILED")
        self.assertEqual(recovered["r71_execution_dependency_manifest_sha256"], dependency_sha)
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_success_terminal_crash_recovery_keeps_digest_unaccepted(self) -> None:
        lease, session = self._start_session("r71-crash-after-success-terminal")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        dependency_sha = self._bind_session_to_synthetic_r71(lease)
        original_write_json = retention._write_json
        terminal_path = session.session_dir / retention.BUILD_SESSION_TERMINAL

        def write_success_then_stop(path: Path, payload: object, **kwargs: object) -> bytes:
            raw = original_write_json(path, payload, **kwargs)
            if path == terminal_path and isinstance(payload, dict) and payload.get("state") == "SUCCESS":
                raise OSError("synthetic R71 interruption after SUCCESS")
            return raw

        with (mock.patch.object(lease, "_revalidate_r71_admission"),
              mock.patch.object(retention, "_write_json", side_effect=write_success_then_stop),
              mock.patch.object(session, "_fail", return_value=None)):
            with self.assertRaisesRegex(OSError, "R71 interruption after SUCCESS"):
                session.finalize()
        record = retention._load_lease(lease.build_id)
        terminal = retention._read_json(terminal_path, sealed=True)
        self.assertEqual(record["state"], "ACTIVE")
        self.assertEqual(record["session_v3"]["state"], "FINALIZING")
        self.assertEqual(record["r71_execution_dependency_manifest_sha256"], dependency_sha)
        self.assertEqual(terminal["state"], "SUCCESS")
        self.assertEqual(terminal["r71_execution_dependency_manifest_sha256"], dependency_sha)
        with self.assertRaisesRegex(retention.RetentionError, "accepted lease completion"):
            retention._verify_build_manifest(lease.build_id)
        lease._release_lock()
        retention._reject_interrupted_host_v3_session(record)
        recovered = retention._load_lease(lease.build_id)
        self.assertEqual(recovered["state"], "FAILED")
        self.assertEqual(recovered["r71_execution_dependency_manifest_sha256"], dependency_sha)
        self.assertFalse(terminal_path.exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_promotion_interruption_preserves_digest_without_success(self) -> None:
        lease, session = self._start_session("r71-promotion-interruption")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        dependency_sha = self._bind_session_to_synthetic_r71(lease)
        with (mock.patch.object(lease, "_revalidate_r71_admission"),
              mock.patch.object(retention, "_copy_tree_artifacts",
                                side_effect=OSError("synthetic R71 promotion interruption"))):
            with self.assertRaisesRegex(OSError, "R71 promotion interruption"):
                session.finalize()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["r71_execution_dependency_manifest_sha256"], dependency_sha)
        self.assertEqual(record["state"], "FAILED")
        self.assertNotEqual(record.get("session_v3", {}).get("state"), "ACCEPTED")
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
        terminal = session.session_dir / retention.BUILD_SESSION_TERMINAL
        if terminal.exists():
            self.assertNotEqual(retention._read_json(terminal, sealed=True).get("state"), "SUCCESS")

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_retirement_interruption_retry_preserves_dependency_digest(self) -> None:
        lease, session = self._start_session("r71-retirement-interruption")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        dependency_sha = self._bind_session_to_synthetic_r71(lease)
        retire_real = retention._retire

        def retire_then_interrupt(record: dict, *, reason: str) -> dict:
            receipt = retire_real(record, reason=reason)
            raise OSError("synthetic R71 retirement interruption after durable receipt")

        with (mock.patch.object(lease, "_revalidate_r71_admission"),
              mock.patch.object(retention, "_retire", side_effect=retire_then_interrupt)):
            with self.assertRaisesRegex(OSError, "retirement interruption"):
                session.finalize()
        retired = retention._load_lease(lease.build_id)
        self.assertEqual(retired["state"], "COMPLETED")
        self.assertEqual(retired["r71_execution_dependency_manifest_sha256"], dependency_sha)
        replay = retire_real(retired, reason="r71-retirement-recovery-retry")
        self.assertEqual(replay["result"], "REMOVED")
        recovered = retention._load_lease(lease.build_id)
        self.assertEqual(recovered["r71_execution_dependency_manifest_sha256"], dependency_sha)

    @_with_synthetic_buildlease_runtime_gate
    def test_session_manifest_validation_failure_never_persists_success(self) -> None:
        lease, session = self._start_session("session-manifest-validation-failure")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)

        def fail_finalizing_validation(identity: object, artifacts: object, evidence: object,
                                       **kwargs: object) -> None:
            self.assertEqual(identity["sandboxed_execution_acceptance_state"],
                             "UNACCEPTED_FINALIZING_MANIFEST")
            raise retention.RetentionError("synthetic manifest identity validation failure")

        with mock.patch.object(retention, "_validate_completed_identity",
                               side_effect=fail_finalizing_validation):
            with self.assertRaisesRegex(retention.RetentionError, "manifest identity validation"):
                session.finalize()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["session_v3"]["state"], "FAILED")
        self.assertEqual(record["session_v3"]["acceptance_state"], "REJECTED_INCOMPLETE_QUIESCENCE")
        self.assertFalse((session.session_dir / retention.BUILD_SESSION_TERMINAL).exists())
        self.assertFalse(retention._completion_terminal_path(lease.build_id).exists())
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.evidence").exists())
        failure = retention._read_json(Path(record["failure_evidence_path"]))
        self.assertIs(failure["outputs_accepted"], False)

    @_with_synthetic_buildlease_runtime_gate
    def test_session_manifest_construction_exception_never_persists_success(self) -> None:
        lease, session = self._start_session("session-manifest-construction-failure")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        with mock.patch.object(retention, "_build_manifest",
                               side_effect=RuntimeError("synthetic manifest construction exception")):
            with self.assertRaisesRegex(RuntimeError, "manifest construction exception"):
                session.finalize()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "FAILED")
        self.assertFalse((session.session_dir / retention.BUILD_SESSION_TERMINAL).exists())
        self.assertFalse(retention._completion_terminal_path(lease.build_id).exists())
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_session_manifest_durable_write_failure_never_persists_success(self) -> None:
        lease, session = self._start_session("session-manifest-write-failure")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        original_write_json = retention._write_json
        manifest_path = retention.MANIFEST_ROOT / f"{lease.build_id}.json"

        def write_manifest_then_fail(path: Path, payload: object, **kwargs: object) -> bytes:
            result = original_write_json(path, payload, **kwargs)
            if path == manifest_path:
                raise OSError("synthetic error after manifest publication")
            return result

        with mock.patch.object(retention, "_write_json", side_effect=write_manifest_then_fail):
            with self.assertRaisesRegex(OSError, "after manifest publication"):
                session.finalize()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "FAILED")
        self.assertFalse((session.session_dir / retention.BUILD_SESSION_TERMINAL).exists())
        self.assertFalse(retention._completion_terminal_path(lease.build_id).exists())
        self.assertFalse(manifest_path.exists())
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.evidence").exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_crash_after_manifest_before_success_is_not_reusable(self) -> None:
        lease, session = self._start_session("session-crash-after-manifest")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        original_write_json = retention._write_json
        terminal_path = session.session_dir / retention.BUILD_SESSION_TERMINAL

        def stop_before_success(path: Path, payload: object, **kwargs: object) -> bytes:
            if path == terminal_path and isinstance(payload, dict) and payload.get("state") == "SUCCESS":
                raise OSError("synthetic interruption before success terminal")
            return original_write_json(path, payload, **kwargs)

        with mock.patch.object(retention, "_write_json", side_effect=stop_before_success), \
                mock.patch.object(session, "_fail", return_value=None):
            with self.assertRaisesRegex(OSError, "before success terminal"):
                session.finalize()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "ACTIVE")
        self.assertEqual(record["session_v3"]["state"], "FINALIZING")
        self.assertEqual(retention._read_json(terminal_path, sealed=True)["state"], "FINALIZING")
        self.assertTrue((retention.MANIFEST_ROOT / f"{lease.build_id}.json").is_file())
        with self.assertRaisesRegex(retention.RetentionError, "accepted lease completion"):
            retention._verify_build_manifest(lease.build_id)
        self.assertIsNone(retention.find_canonical_equivalent(
            config_key_sha256=lease.configuration_key_sha256, kind=lease.kind,
            source_tree_hash=lease.source_tree_hash))
        lease._release_lock()
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertTrue(any("active Host BuildLease" in item for item in blockers))
        retention._reject_interrupted_host_v3_session(record)
        self.assertEqual(retention._load_lease(lease.build_id)["state"], "FAILED")
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
        self.assertFalse(retention._completion_terminal_path(lease.build_id).exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_crash_after_success_terminal_before_lease_commit_is_not_accepted(self) -> None:
        lease, session = self._start_session("session-crash-after-success-terminal")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        original_write_json = retention._write_json
        terminal_path = session.session_dir / retention.BUILD_SESSION_TERMINAL

        def publish_success_then_stop(path: Path, payload: object, **kwargs: object) -> bytes:
            raw = original_write_json(path, payload, **kwargs)
            if path == terminal_path and isinstance(payload, dict) and payload.get("state") == "SUCCESS":
                raise OSError("synthetic interruption after durable success terminal")
            return raw

        with mock.patch.object(retention, "_write_json", side_effect=publish_success_then_stop), \
                mock.patch.object(session, "_fail", return_value=None):
            with self.assertRaisesRegex(OSError, "after durable success terminal"):
                session.finalize()
        record = retention._load_lease(lease.build_id)
        self.assertEqual(record["state"], "ACTIVE")
        self.assertEqual(record["session_v3"]["state"], "FINALIZING")
        terminal = retention._read_json(terminal_path, sealed=True)
        self.assertEqual(terminal["state"], "SUCCESS")
        self.assertTrue(terminal.get("build_manifest_id"))
        with self.assertRaisesRegex(retention.RetentionError, "accepted lease completion"):
            retention._verify_build_manifest(lease.build_id)
        self.assertIsNone(retention.find_canonical_equivalent(
            config_key_sha256=lease.configuration_key_sha256, kind=lease.kind,
            source_tree_hash=lease.source_tree_hash))
        lease._release_lock()
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertTrue(any("active Host BuildLease" in item for item in blockers))
        retention._reject_interrupted_host_v3_session(record)
        self.assertFalse(terminal_path.exists())
        self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())

    @_with_synthetic_buildlease_runtime_gate
    def test_durable_lease_acceptance_commit_is_the_success_linearization_point(self) -> None:
        lease, session = self._start_session("session-crash-during-lease-commit")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        self._finish_session_steps(session)
        original_store_lease = retention._store_lease

        def commit_then_stop(value: object) -> dict:
            sealed = original_store_lease(value)
            if value.get("state") == "COMPLETED":
                raise OSError("synthetic interruption after durable acceptance commit")
            return sealed

        with mock.patch.object(retention, "_store_lease", side_effect=commit_then_stop), \
                mock.patch.object(session, "_fail", return_value=None):
            with self.assertRaisesRegex(OSError, "after durable acceptance commit"):
                session.finalize()
        lease._release_lock()
        accepted = retention._load_lease(lease.build_id)
        self.assertEqual(accepted["state"], "COMPLETED")
        self.assertEqual(accepted["session_v3"]["state"], "ACCEPTED")
        manifest, digest = retention._verify_build_manifest(lease.build_id)
        self.assertEqual(accepted["build_manifest_id"], manifest["build_manifest_id"])
        self.assertEqual(accepted["build_manifest_sha256"], digest)
        marker_fd = retention._open_dir(lease.path)
        try:
            stale_marker_digest = hashlib.sha256(retention._read_at(
                marker_fd, retention.BUILD_MARKER, limit=1024 * 1024)).hexdigest()
        finally:
            os.close(marker_fd)
        self.assertEqual(accepted["session_v3"]["preacceptance_marker_sha256"], stale_marker_digest)
        state = retention._snapshot()
        self.assertTrue(any(row.get("path") == str(lease.path) for row in state["unknown"]))
        receipt = retention._retire(accepted, reason="recover-after-lease-acceptance-commit")
        self.assertEqual(receipt["result"], "REMOVED")
        retired = retention._load_lease(lease.build_id)
        self.assertEqual(retired["state"], "COMPLETED")
        self.assertIsNotNone(retired.get("retired_at_unix"))
        recovered_state = retention._snapshot()
        self.assertFalse(any(row.get("path") == str(lease.path) for row in recovered_state["unknown"]))

    @_with_synthetic_buildlease_runtime_gate
    def test_completed_host_v3_consumers_reject_missing_or_mismatched_bindings(self) -> None:
        lease, session = self._start_session("session-accepted-consumer-bindings")
        self._finish_session_steps(session)
        with mock.patch.object(retention, "_retire", return_value={"result": "synthetic retirement"}):
            session.finalize()
        manifest_path = retention.MANIFEST_ROOT / f"{lease.build_id}.json"
        manifest_raw = manifest_path.read_bytes()
        manifest, manifest_digest = retention._verify_build_manifest(lease.build_id)
        self.assertEqual(retention.host_buildlease_p2_admission_blockers(), [])

        manifest_path.unlink()
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertTrue(any("state is unknown" in item for item in blockers))
        manifest_path.write_bytes(manifest_raw)

        lease_record = retention._load_lease(lease.build_id)
        lease_record["build_manifest_sha256"] = "f" * 64
        retention._store_lease(lease_record)
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertTrue(any("manifest" in item for item in blockers))
        lease_record["build_manifest_sha256"] = manifest_digest

        terminal_path = retention._completion_terminal_path(lease.build_id)
        terminal = retention._read_json(terminal_path, sealed=True)
        terminal["state"] = "FINALIZING"
        terminal_raw = retention._write_json(terminal_path, terminal, replace=True, sealed=True)
        terminal_digest = hashlib.sha256(terminal_raw).hexdigest()
        lease_record["completion_terminal_sha256"] = terminal_digest
        lease_record["session_v3"]["terminal_sha256"] = terminal_digest
        lease_record["session_v3"]["completion_terminal_sha256"] = terminal_digest
        retention._store_lease(lease_record)
        blockers = retention.host_buildlease_p2_admission_blockers()
        self.assertTrue(any("terminal" in item.lower() or "completion" in item.lower()
                            for item in blockers))

    @_with_synthetic_buildlease_runtime_gate
    def test_session_finalization_rejects_terminal_root_identity_mismatch(self) -> None:
        lease, session = self._start_session("session-terminal-root-identity")
        self.addCleanup(lease._release_lock)
        self.addCleanup(lease._stop_heartbeat)
        try:
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
        finally:
            # This error-path fixture intentionally prevents normal fail/retire
            # cleanup. Remove only its exact temporary root/registry so the
            # production R63 root scan in later tests sees a clean namespace.
            lease._stop_heartbeat()
            lease._release_lock()
            expected_root = (self.cache / "builds" / lease.build_id).absolute()
            self.assertEqual(lease.path.absolute(), expected_root)
            synthetic_contents = self._synthetic_workspace_contents_path(lease.path)
            if synthetic_contents.exists():
                shutil.rmtree(synthetic_contents)
            if lease.path.exists():
                shutil.rmtree(lease.path)
            lease_record_path = retention.LEASE_ROOT / f"{lease.build_id}.json"
            lease_record_path.unlink(missing_ok=True)

        self.assertFalse(lease.path.exists(), "fixture cleanup must remove the exact build root")
        self.assertFalse(lease_record_path.exists(), "fixture cleanup must remove its exact lease record")
        snapshot = retention.status_snapshot()
        self.assertEqual(snapshot["gate_status"], "PASS")
        self.assertEqual(snapshot["unknown"], [])
        self.assertFalse(any(row.get("path") == str(lease.path) for row in snapshot["unknown"]))

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

    def _start_dual_graph_session(
            self, build_id: str, *,
            persistent_output_allowance_bytes: int | None = None,
    ) -> tuple[retention.BuildLease, retention.BuildLeaseSession]:
        lease = self._session_lease(
            build_id, dual_graph=True,
            persistent_output_allowance_bytes=persistent_output_allowance_bytes)
        lease.reserve()
        return lease, lease.start_session()

    def test_r96_watcher_checks_capacity_after_zero_and_nonzero_exit(self) -> None:
        lease = object.__new__(retention.BuildLease)
        lease.storage_watch_events = []
        lease._free_warning_emitted = False
        phases: dict[str, int] = {}
        session = SimpleNamespace(
            _record_process_exit_observation=lambda _process, _code: None,
            _check_limits=lambda phase: phases.__setitem__(phase, phases.get(phase, 0) + 1))
        with mock.patch.object(storage_policy, "disk_free_bytes", return_value=100 * storage_policy.GIB):
            for code in (0, 7):
                with self.subTest(exit_code=code):
                    process = subprocess.Popen(
                        [sys.executable, "-c", f"raise SystemExit({code})"],
                        start_new_session=True)
                    try:
                        self.assertEqual(lease._wait_for_build_process(process, session=session), code)
                    finally:
                        if process.poll() is None:
                            process.kill()
                        process.wait(timeout=5)
        self.assertEqual(phases.get("during-step-exited"), 2)
        self.assertGreater(phases.get("during-step", 0), 0)

    def test_r96_post_exit_capacity_requires_exact_process_owner(self) -> None:
        session = object.__new__(retention.BuildLeaseSession)
        session.lease = SimpleNamespace(build_id="r96-owner-check")
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                   start_new_session=True)
        replacement = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(10)"],
                                       start_new_session=True)
        session._active_process = process
        session._load = lambda **_kwargs: ({}, {"state": "RUNNING",
                                               "process_session_id": replacement.pid})
        session._check_root_bindings = lambda *_args: None
        try:
            with self.assertRaisesRegex(retention.RetentionError, "exact in-flight process owner"):
                session._check_limits("during-step-exited")
            session._load = lambda **_kwargs: ({}, {"state": "RUNNING",
                                                   "process_session_id": None})
            with self.assertRaisesRegex(retention.RetentionError, "exact in-flight process owner"):
                session._check_limits("during-step-exited")
            session._load = lambda **_kwargs: ({}, {"state": "RUNNING",
                                                   "process_session_id": process.pid})
            with self.assertRaisesRegex(
                    retention.RetentionError, "lacks exact unreaped process exit evidence"):
                session._check_limits("during-step-exited")
            process.terminate()
            process.wait(timeout=5)
            with self.assertRaisesRegex(
                    retention.RetentionError, "lacks exact unreaped process exit evidence"):
                session._check_limits("during-step-exited")
        finally:
            for child in (process, replacement):
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)

    @_with_synthetic_buildlease_runtime_gate
    def test_r96_post_exit_capacity_rechecks_exact_workspace_authority(self) -> None:
        lease, session = self._start_dual_graph_session("r6a-r71-r96-post-exit-capacity")
        self._bind_session_to_synthetic_r71(lease)
        process = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        other = subprocess.Popen([sys.executable, "-c", "pass"], start_new_session=True)
        command_path = session.session_dir / "steps/step-000.command.json"
        try:
            current = retention._load_lease(lease.build_id)["session_v3"]
            step = session.plan["steps"][0]
            command, executable_identity = session._command_for_step(step, current)
            receipt = {
                "schema_version": retention.BUILD_SESSION_SCHEMA_VERSION,
                "build_id": lease.build_id,
                "executor_mode": retention.BUILD_SESSION_EXECUTOR_MODE,
                "executor_version": retention.BUILD_SESSION_SCHEMA_VERSION,
                "acceptance_state": "UNACCEPTED_UNTIL_WHOLE_LEASE_DETACH",
                "plan_sha256": session.plan_sha256,
                "step_index": 0,
                "kind": step["kind"],
                "command": command,
                "cwd": str(session.workspace),
                "registered_executable_identity": executable_identity,
                "graph_provenance": session._graph_provenance(step),
            }
            raw = retention._write_json(command_path, receipt, replace=False, sealed=True)
            record = retention._load_lease(lease.build_id)
            record["session_v3"].update({
                "state": "RUNNING", "inflight_step_index": 0,
                "inflight_command_receipt_sha256": hashlib.sha256(raw).hexdigest(),
                "process_session_id": process.pid,
            })
            session._store(record)
            session._active_process = process
            deadline = time.monotonic() + 5
            while True:
                exited = os.waitid(os.P_PID, process.pid,
                                   os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None and exited.si_pid == process.pid:
                    break
                if time.monotonic() >= deadline:
                    self.fail("synthetic child did not exit before post-exit authority check")
                time.sleep(0.01)

            exact_workspace = retention._load_lease(lease.build_id)["session_v3"][
                "workspace_mount_identity"]
            session._record_process_exit_observation(process, 0)
            measure_calls: list[dict[str, object]] = []

            def authenticate_workspace(**kwargs: object) -> dict[str, int]:
                measure_calls.append(dict(kwargs))
                self.assertEqual(kwargs.get("active_buildlease_id"), lease.build_id)
                self.assertEqual(kwargs.get("r71_exited_process_id"), process.pid)
                self.assertEqual(
                    retention._verified_buildlease_capacity_workspace(
                        lease.build_id, exited_process_id=process.pid), exact_workspace)
                return {"allocated_inode_deduplicated_bytes": 0}

            with (mock.patch.object(storage_policy, "measure_project_usage",
                                    side_effect=authenticate_workspace),
                  mock.patch.object(storage_policy, "disk_free_bytes",
                                    return_value=100 * storage_policy.GIB),
                  mock.patch.object(retention, "_session_measure_tree",
                                    return_value={"allocated_bytes": 0}),
                  mock.patch.object(session, "_check_controller_evidence_budget",
                                    return_value=(0, 0)),
                  mock.patch.object(storage_policy, "projected_storage_admission",
                                    return_value={"admitted": True, "warnings": [], "blockers": []})):
                session._check_limits("during-step-exited")
            self.assertEqual(len(measure_calls), 1)
            with self.assertRaisesRegex(retention.RetentionError, "exact process"):
                retention._verified_buildlease_capacity_workspace(
                    lease.build_id, exited_process_id=other.pid)
        finally:
            for child in (process, other):
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
            session._active_process = None
            current = retention._load_lease(lease.build_id)
            running = current.get("session_v3")
            if isinstance(running, dict) and running.get("state") == "RUNNING":
                running["state"] = "ACTIVE"
                running.pop("inflight_step_index", None)
                running.pop("inflight_command_receipt_sha256", None)
                running.pop("process_session_id", None)
                current["session_v3"] = running
                session._store(current)
                session._fail("R96 post-exit authority test cleanup", -1,
                              command=["r96-post-exit-cleanup"], step_index=None)
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_r96_post_exit_capacity_failure_keeps_durable_child_status(self) -> None:
        lease, session = self._start_dual_graph_session("r6a-r71-r96-exit-status-before-capacity")
        self._bind_session_to_synthetic_r71(lease)
        process = subprocess.Popen(
            [sys.executable, "-c", "raise SystemExit(23)"], start_new_session=True)
        command_path = session.session_dir / "steps/step-000.command.json"
        try:
            current = retention._load_lease(lease.build_id)["session_v3"]
            step = session.plan["steps"][0]
            command, executable_identity = session._command_for_step(step, current)
            receipt = {
                "schema_version": retention.BUILD_SESSION_SCHEMA_VERSION,
                "build_id": lease.build_id,
                "executor_mode": retention.BUILD_SESSION_EXECUTOR_MODE,
                "executor_version": retention.BUILD_SESSION_SCHEMA_VERSION,
                "acceptance_state": "UNACCEPTED_UNTIL_WHOLE_LEASE_DETACH",
                "plan_sha256": session.plan_sha256,
                "step_index": 0,
                "kind": step["kind"],
                "command": command,
                "cwd": str(session.workspace),
                "registered_executable_identity": executable_identity,
                "graph_provenance": session._graph_provenance(step),
            }
            raw = retention._write_json(command_path, receipt, replace=False, sealed=True)
            record = retention._load_lease(lease.build_id)
            record["session_v3"].update({
                "state": "RUNNING", "inflight_step_index": 0,
                "inflight_command_receipt_sha256": hashlib.sha256(raw).hexdigest(),
                "process_session_id": process.pid,
            })
            session._store(record)
            session._active_process = process
            deadline = time.monotonic() + 5
            while True:
                exited = os.waitid(os.P_PID, process.pid,
                                   os.WEXITED | os.WNOHANG | os.WNOWAIT)
                if exited is not None and exited.si_pid == process.pid:
                    break
                if time.monotonic() >= deadline:
                    self.fail("synthetic child did not exit before forced capacity failure")
                time.sleep(0.01)

            with (mock.patch.object(storage_policy, "disk_free_bytes",
                                    return_value=100 * storage_policy.GIB),
                  mock.patch.object(session, "_check_limits",
                                    side_effect=retention.RetentionError(
                                        "forced post-exit capacity check failure"))):
                with self.assertRaisesRegex(
                        retention.RetentionError, "forced post-exit capacity check failure"):
                    lease._wait_for_build_process(process, session=session)

            persisted = retention._load_lease(lease.build_id)["session_v3"][
                "process_exit_observation"]
            self.assertEqual(persisted["build_id"], lease.build_id)
            self.assertEqual(persisted["step_index"], 0)
            self.assertEqual(persisted["command_receipt_sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual(persisted["process_session_id"], process.pid)
            self.assertEqual(persisted["return_code"], 23)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            session._active_process = None
            current = retention._load_lease(lease.build_id)
            running = current.get("session_v3")
            if isinstance(running, dict) and running.get("state") == "RUNNING":
                running["state"] = "ACTIVE"
                running.pop("inflight_step_index", None)
                running.pop("inflight_command_receipt_sha256", None)
                running.pop("process_session_id", None)
                current["session_v3"] = running
                session._store(current)
                session._fail("R96 durable exit test cleanup", -1,
                              command=["r96-exit-status-cleanup"], step_index=None)
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_untracked_source_configuration_key_ignores_evidence_root(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-untracked-key-", dir="/private/tmp") as raw:
            root = Path(raw)
            source = root / "source"
            source.mkdir(mode=0o700)
            (source / "untracked-dir").mkdir()
            (source / "untracked-dir/nested.txt").write_bytes(b"nested source bytes\n")
            (source / "loose.txt").write_bytes(b"loose source bytes\n")
            (source / "loose-link").symlink_to("loose.txt")
            relatives = sorted((Path("untracked-dir"), Path("loose.txt"), Path("loose-link")),
                               key=lambda item: item.as_posix())

            def capture(evidence: Path) -> tuple[list[dict], dict, str]:
                evidence.mkdir(mode=0o700)
                rows = [retention._capture_untracked_source_path(source, relative, evidence)
                        for relative in relatives]
                stable_hash, evidence_hash = retention._untracked_source_identity_hashes(rows)
                source_identity = {
                    "path": str(source),
                    "content_tree_sha256": hashlib.sha256(b"same tracked source tree").hexdigest(),
                    "untracked_path_list_sha256": hashlib.sha256(
                        b"\0".join(os.fsencode(relative.as_posix()) for relative in relatives)).hexdigest(),
                    "untracked_files": rows,
                    "untracked_files_sha256": stable_hash,
                    "untracked_files_evidence_sha256": evidence_hash,
                }
                identity = {
                    "build_system": "meson",
                    "source_identity": source_identity,
                    "patch_set": {
                        "staged_patch_sha256": hashlib.sha256(b"").hexdigest(),
                        "unstaged_patch_sha256": hashlib.sha256(b"").hexdigest(),
                        "patch_set_sha256": hashlib.sha256(b"\0\0").hexdigest(),
                        "untracked_files_sha256": stable_hash,
                        "capture_scope": "artifactization-time snapshot",
                    },
                    "cross_file": {"status": "absent"},
                    "meson_options": [{"machine": "host", "section": "core",
                                       "name": "buildtype", "value": "release"}],
                    "compiler_identity": {"host": {"c": {"id": "clang"}}},
                    "compiler_identity_sha256": hashlib.sha256(b"compiler").hexdigest(),
                    "linker_identity": [{"command": "ld -v", "output": "synthetic linker"}],
                    "build_type": "release",
                    "dependency_state": {"status": "captured"},
                    "generated_shader_identity": [],
                }
                profile_hash = hashlib.sha256(b"fixed configure profile").hexdigest()
                identity["dependency_state"]["plan_profile_sha256"] = profile_hash
                graph_key = retention._dual_graph_plan_bound_configuration_key(identity, profile_hash)
                graph_rows = {
                    graph_id: {"graph_id": graph_id, "config_key_sha256": graph_key,
                               "meson_identity": identity}
                    for graph_id in retention.BUILD_SESSION_DUAL_GRAPH_IDS
                }
                pair_key = retention._dual_graph_configuration_pair_sha256(graph_rows)
                return rows, identity, pair_key

            rows_one, identity_one, pair_one = capture(root / "evidence-one")
            rows_two, identity_two, pair_two = capture(root / "evidence-two")
            self.assertNotEqual(rows_one[0].get("retained_path"), rows_two[0].get("retained_path"))
            self.assertNotEqual(identity_one["source_identity"]["untracked_files_evidence_sha256"],
                                identity_two["source_identity"]["untracked_files_evidence_sha256"])
            self.assertEqual(identity_one["source_identity"]["untracked_files_sha256"],
                             identity_two["source_identity"]["untracked_files_sha256"])
            self.assertEqual(retention._dual_graph_configuration_identity(identity_one),
                             retention._dual_graph_configuration_identity(identity_two))
            self.assertEqual(pair_one, pair_two)

            first_evidence_root = root / "evidence-one"
            retained_row = next(row for row in rows_one if row.get("type") == "regular")
            retained_path = Path(retained_row["retained_path"])
            retained_relative = retained_path.relative_to(first_evidence_root)
            retained_size = retained_path.stat().st_size
            retention._hash_file_beneath_nofollow(
                first_evidence_root, retained_relative,
                expected_sha256=retained_row["sha256"], expected_size_bytes=retained_size,
                label="synthetic untracked source evidence")
            retained_bytes = retained_path.read_bytes()
            retained_path.write_bytes(retained_bytes + b"tamper")
            with self.assertRaises(retention.RetentionError):
                retention._hash_file_beneath_nofollow(
                    first_evidence_root, retained_relative,
                    expected_sha256=retained_row["sha256"], expected_size_bytes=retained_size,
                    label="synthetic untracked source evidence")
            retained_path.write_bytes(retained_bytes)
            retained_path.unlink()
            with self.assertRaises(retention.RetentionError):
                retention._hash_file_beneath_nofollow(
                    first_evidence_root, retained_relative,
                    expected_sha256=retained_row["sha256"], expected_size_bytes=retained_size,
                    label="synthetic untracked source evidence")
            retained_path.write_bytes(retained_bytes)

            (source / "loose.txt").write_bytes(b"changed source bytes\n")
            _rows_changed, _identity_changed, pair_changed = capture(root / "evidence-content-change")
            self.assertNotEqual(pair_one, pair_changed, "source content changes must change the graph-pair key")

            (source / "loose.txt").rename(source / "renamed.txt")
            relatives[:] = sorted((Path("untracked-dir"), Path("renamed.txt"), Path("loose-link")),
                                  key=lambda item: item.as_posix())
            _rows_renamed, _identity_renamed, pair_renamed = capture(root / "evidence-path-change")
            self.assertNotEqual(pair_changed, pair_renamed, "source-relative path changes must change the graph-pair key")

    def test_candidate4_prebuild_projection_canonicalizes_meson_option_order(self) -> None:
        repository = Path(__file__).resolve().parents[2]
        manifest_paths = [
            repository / run_r6a_execution.R65_GOVERNANCE_FILES[key]
            for key in ("r65_candidate4_preflight_manifest",
                        "r65_candidate4_stability_preflight_manifest")
        ]
        manifests = [json.loads(path.read_bytes()) for path in manifest_paths]
        self.assertEqual(manifests[0]["graph_pair_key_sha256"],
                         manifests[1]["graph_pair_key_sha256"])
        graph_ids = retention.BUILD_SESSION_DUAL_GRAPH_IDS
        graph_maps = []
        for manifest in manifests:
            rows = {row["graph_id"]: row for row in manifest["graphs"]}
            self.assertEqual(tuple(rows), graph_ids)
            graph_map = {}
            for graph_id in graph_ids:
                frozen = rows[graph_id]["prebuild_identity"]
                meson_identity = frozen["meson_identity"]
                graph_map[graph_id] = {
                    "graph_id": graph_id,
                    "config_key_sha256": frozen["config_key_sha256"],
                    "meson_identity": meson_identity,
                }
            graph_maps.append(graph_map)
        self.assertEqual(retention._dual_graph_configuration_pair_sha256(graph_maps[0]),
                         retention._dual_graph_configuration_pair_sha256(graph_maps[1]))
        raw_option_orders_differ = False
        for graph_id in graph_ids:
            first = graph_maps[0][graph_id]["meson_identity"]
            second = graph_maps[1][graph_id]["meson_identity"]
            self.assertEqual(retention._dual_graph_configuration_identity(first),
                             retention._dual_graph_configuration_identity(second))
            raw_option_orders_differ |= first["meson_options"] != second["meson_options"]
        self.assertTrue(raw_option_orders_differ,
                        "the independent preflights must exercise Meson option-order variation")

        changed_graphs = copy.deepcopy(graph_maps[0])
        graph_id = graph_ids[0]
        changed_identity = changed_graphs[graph_id]["meson_identity"]
        buildtype = next(row for row in changed_identity["meson_options"]
                         if row.get("name") == "buildtype")
        buildtype["value"] = "debug"
        profile_sha256 = changed_identity["dependency_state"]["plan_profile_sha256"]
        changed_graphs[graph_id]["config_key_sha256"] = (
            retention._dual_graph_plan_bound_configuration_key(changed_identity, profile_sha256))
        self.assertNotEqual(retention._dual_graph_configuration_identity(
                                graph_maps[0][graph_id]["meson_identity"]),
                            retention._dual_graph_configuration_identity(changed_identity))
        self.assertNotEqual(retention._dual_graph_configuration_pair_sha256(graph_maps[0]),
                            retention._dual_graph_configuration_pair_sha256(changed_graphs),
                            "a real Meson option value change must still change the ordered pair key")

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
        lease, session = self._start_dual_graph_session(
            "dual-success-terminal-r87", persistent_output_allowance_bytes=retention.GIB)
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
        original_promote = retention._promote_dual_graph_artifacts
        promotion_observations: list[dict[str, object]] = []

        def promote_only_after_success(outputs: list[dict[str, object]]) -> list[dict[str, object]]:
            terminal = retention._read_json(
                session.session_dir / retention.BUILD_SESSION_TERMINAL, sealed=True)
            active_lease = retention._load_lease(lease.build_id)
            self.assertEqual(terminal["state"], "SUCCESS")
            self.assertEqual(active_lease["state"], "ACTIVE")
            self.assertEqual(active_lease["session_v3"]["state"], "FINALIZING")
            promotion_observations.append({"terminal": terminal, "lease": active_lease})
            return original_promote(outputs)

        original_verify_manifest = retention._verify_build_manifest
        post_promotion_verifications: list[dict[str, object]] = []

        def verify_committed_after_promotion(build_id: str, **kwargs: object):
            if kwargs.get("allow_pending_promotion") is False:
                current = retention._load_lease(build_id)
                terminal = retention._read_json(
                    session.session_dir / retention.BUILD_SESSION_TERMINAL, sealed=True)
                self.assertEqual(current["state"], "ACTIVE")
                self.assertEqual(terminal["state"], "SUCCESS")
                post_promotion_verifications.append(dict(kwargs))
            return original_verify_manifest(build_id, **kwargs)

        with mock.patch.object(retention, "_capture_identity", _ORIGINAL_CAPTURE_IDENTITY):
            with (mock.patch.object(retention._FinalizationOutputBudget, "_capacity_admission"),
                  mock.patch.object(retention, "_promote_dual_graph_artifacts",
                                    side_effect=promote_only_after_success),
                  mock.patch.object(retention, "_verify_build_manifest",
                                    side_effect=verify_committed_after_promotion)):
                session.finalize()
        self.assertEqual(len(promotion_observations), 1)
        self.assertEqual(post_promotion_verifications, [{
            "allow_uncommitted_host_v3": True, "allow_pending_promotion": False,
        }])
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

        manifest, _manifest_sha256 = retention._verify_build_manifest(lease.build_id)
        identity = manifest["build_identity"]
        artifacts = manifest["artifacts"]
        evidence = retention.MANIFEST_ROOT / f"{lease.build_id}.evidence"
        graphs = identity["build_graph_identities"]
        self.assertEqual(set(graphs), set(retention.BUILD_SESSION_DUAL_GRAPH_IDS))
        self.assertRegex(identity["session_plan_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(identity["graph_config_key_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(identity["session_plan_sha256"], identity["graph_config_key_sha256"])
        for graph_id in retention.BUILD_SESSION_DUAL_GRAPH_IDS:
            self.assertRegex(graphs[graph_id]["config_key_sha256"], r"^[0-9a-f]{64}$")
            self.assertNotEqual(identity["session_plan_sha256"], graphs[graph_id]["config_key_sha256"])
        expected_artifacts = {
            output["output_id"]: (graph["graph_id"], output["format"])
            for graph in session.plan["build_graphs"]
            for output in graph["expected_outputs"]
        }
        self.assertEqual({row["output_id"]: (row["graph_id"], row["format"])
                          for row in artifacts}, expected_artifacts)

        self._r65_case_assertions = set()

        def rejected_contract_case(label: str, *, case_id: int,
                                   identity_change=None, artifacts_change=None) -> None:
            attacked_identity = copy.deepcopy(identity)
            attacked_artifacts = copy.deepcopy(artifacts)
            if identity_change is not None:
                identity_change(attacked_identity)
            if artifacts_change is not None:
                artifacts_change(attacked_artifacts)
            with self.subTest(contract=label), self.assertRaises(retention.RetentionError):
                retention._validate_completed_identity(attacked_identity, attacked_artifacts, evidence)
            self._r65_case_assertions.add(case_id)

        self._r65_case_assertions.add(1)

        # The success above covers cases 1 and 13; cases 2-10, 12, and 14
        # mutate only copied receipts so the retained successful evidence stays intact.
        rejected_contract_case(
            "2 Windows graph missing",
            case_id=2,
            identity_change=lambda value: value["build_graph_identities"].pop("windows-product-r6a"))
        rejected_contract_case(
            "3 native graph missing",
            case_id=3,
            identity_change=lambda value: value["build_graph_identities"].pop("native-verification-r6a"))
        rejected_contract_case(
            "4 Windows graph configuration key missing",
            case_id=4,
            identity_change=lambda value: value["build_graph_identities"]["windows-product-r6a"].pop(
                "config_key_sha256"))
        rejected_contract_case(
            "5 native graph configuration key missing",
            case_id=5,
            identity_change=lambda value: value["build_graph_identities"]["native-verification-r6a"].pop(
                "config_key_sha256"))

        def mismatch_windows_profile(value: dict) -> None:
            options = value["build_graph_identities"]["windows-product-r6a"]["meson_identity"]["meson_options"]
            next(row for row in options if row["name"] == "backend")["value"] = "unapproved-backend"

        def mismatch_native_profile(value: dict) -> None:
            options = value["build_graph_identities"]["native-verification-r6a"]["meson_identity"]["meson_options"]
            next(row for row in options if row["name"] == "enable_d3d11")["value"] = False

        rejected_contract_case("6 Windows graph configuration mismatch", case_id=6,
                               identity_change=mismatch_windows_profile)
        rejected_contract_case("7 native graph configuration mismatch", case_id=7,
                               identity_change=mismatch_native_profile)
        rejected_contract_case(
            "6a Windows graph configuration key mismatch",
            case_id=6,
            identity_change=lambda value: value["build_graph_identities"]["windows-product-r6a"].__setitem__(
                "config_key_sha256", "0" * 64))
        rejected_contract_case(
            "7a native graph configuration key mismatch",
            case_id=7,
            identity_change=lambda value: value["build_graph_identities"]["native-verification-r6a"].__setitem__(
                "config_key_sha256", "0" * 64))

        def add_unplanned_windows_option(value: dict) -> None:
            options = value["build_graph_identities"]["windows-product-r6a"]["meson_identity"][
                "meson_options"]
            options.append({"name": "unplanned_option", "value": True,
                            "section": "user", "machine": "any"})

        def duplicate_native_option(value: dict) -> None:
            options = value["build_graph_identities"]["native-verification-r6a"]["meson_identity"][
                "meson_options"]
            options.append(dict(options[0]))

        rejected_contract_case("15 extra Meson option is not plan-authorized", case_id=15,
                               identity_change=add_unplanned_windows_option)
        rejected_contract_case("16 duplicate Meson option identity fails closed", case_id=16,
                               identity_change=duplicate_native_option)
        rejected_contract_case(
            "8 session plan mismatch",
            case_id=8,
            identity_change=lambda value: value.__setitem__("session_plan_sha256", "0" * 64))
        rejected_contract_case(
            "8a changed session plan content",
            case_id=8,
            identity_change=lambda value: value["session_plan"].__setitem__("build_type", "debug"))
        rejected_contract_case(
            "8b missing session plan",
            case_id=8,
            identity_change=lambda value: value.pop("session_plan"))

        def structurally_valid_wrong_plan(value: dict) -> None:
            plan = value["session_plan"]
            wrong_admission_id = plan["admission_identity"]["admission_id"] + "-other"
            plan["admission_identity"] = {
                "admission_id": wrong_admission_id,
                "sha256": hashlib.sha256(retention._canonical_json(
                    {"admission_id": wrong_admission_id})).hexdigest(),
            }
            value["admission_identity"] = copy.deepcopy(plan["admission_identity"])
            value["session_plan_sha256"] = hashlib.sha256(
                retention._canonical_json(plan)).hexdigest()
            validated_wrong_plan = retention._session_validate_plan(
                plan, build_id=plan["build_id"], build_path=lease.path,
                source_tree_hash=plan["source_identity"]["sha256"],
                expected_bytes=plan["reservation_bytes"])
            self.assertEqual(validated_wrong_plan, plan)

        rejected_contract_case(
            "9 structurally valid wrong session plan with recomputed digest",
            case_id=9, identity_change=structurally_valid_wrong_plan)
        rejected_contract_case(
            "12 wrong graph ID",
            case_id=12,
            identity_change=lambda value: value["build_graph_identities"]["windows-product-r6a"].__setitem__(
                "graph_id", "unrecognized-graph"))
        rejected_contract_case(
            "13 duplicate graph identity",
            case_id=13,
            identity_change=lambda value: value["build_graph_identities"]["native-verification-r6a"].__setitem__(
                "graph_id", "windows-product-r6a"))

        def swap_graph_identities(value: dict) -> None:
            graph_map = value["build_graph_identities"]
            graph_map["windows-product-r6a"], graph_map["native-verification-r6a"] = (
                graph_map["native-verification-r6a"], graph_map["windows-product-r6a"])

        rejected_contract_case("11 swapped graph identities", case_id=11,
                               identity_change=swap_graph_identities)

        def erase_dual_identity(value: dict) -> None:
            value.pop("executor_mode", None)
            value.pop("session_plan", None)
            value.pop("session_plan_sha256", None)
            value.pop("build_graph_identities", None)

        rejected_contract_case("14 Host-v3 dual cannot fall back to a single graph", case_id=14,
                               identity_change=erase_dual_identity)

        def swap_pe_and_macho_payloads(rows: list[dict]) -> None:
            by_output = {row["output_id"]: row for row in rows}
            pe = by_output["r6a-production-path-pe"]
            macho = by_output["r6a-production-path-macho"]
            pe["canonical_artifact_path"], macho["canonical_artifact_path"] = (
                macho["canonical_artifact_path"], pe["canonical_artifact_path"])

        rejected_contract_case("14 PE and Mach-O artifacts cannot cross graph identities", case_id=16,
                               artifacts_change=swap_pe_and_macho_payloads)

        def attribute_pe_to_native(rows: list[dict]) -> None:
            next(row for row in rows if row["output_id"] == "r6a-production-path-pe")["graph_id"] = (
                "native-verification-r6a")

        def attribute_macho_to_windows(rows: list[dict]) -> None:
            next(row for row in rows if row["output_id"] == "r6a-production-path-macho")["graph_id"] = (
                "windows-product-r6a")

        rejected_contract_case("15a Windows PE attributed to native graph", case_id=15,
                               artifacts_change=attribute_pe_to_native)
        rejected_contract_case("16a native Mach-O attributed to Windows graph", case_id=16,
                               artifacts_change=attribute_macho_to_windows)

        def mismatch_output_lineage(rows: list[dict]) -> None:
            next(row for row in rows if row["output_id"] == "r6a-production-path-pe")[
                "producer_target_id"] = "UNDECLARED_PRODUCER_TARGET"

        rejected_contract_case("18 output lineage mismatch", case_id=18,
                               artifacts_change=mismatch_output_lineage)

        with tempfile.TemporaryDirectory(prefix="fgmetal-r65-g0c-binding-", dir="/private/tmp") as name:
            g0c_repo = Path(name)
            recovery_build_id = "r6a-r65-recovery-dual-lineage-g0c-binding"
            recovery_plan = self._r65_recovery_plan(
                session.plan, build_id=recovery_build_id, admission_id=recovery_build_id)
            recovery_plan["build_workspace"] = str(
                retention.BUILD_ROOT / recovery_build_id / retention.BUILD_SESSION_WORKSPACE_NAME)
            plan_relative = "R6-A-R65-RECOVERY-DUAL-GRAPH-EXECUTION-PLAN.json"
            plan_path = g0c_repo / plan_relative
            plan_bytes = (json.dumps(recovery_plan, sort_keys=True, indent=2) + "\n").encode()
            plan_path.write_bytes(plan_bytes)
            file_paths = {
                key: (Path("experiments/storage") / relative).as_posix()
                for key, relative in storage_policy.G0C_REVIEW_REQUIRED_FILES.items()
            }
            file_paths.update({
                "r65_execution_runner": "experiments/storage/run_r6a_execution.py",
                "r65_execution_plan": plan_relative,
                run_r6a_execution.R65_CANDIDATE_FILE_KEY:
                    "R6-A-R65-RECOVERY-CANDIDATE-4.json",
                run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY:
                    "R6-A-R65-RECOVERY-CANDIDATE-4.diff",
                "r65_dual_graph_contract": run_r6a_execution.R65_DUAL_GRAPH_CONTRACT,
                **run_r6a_execution.R65_GOVERNANCE_FILES,
            })
            self.assertEqual(
                file_paths["r64_formal_disposition"],
                "R64-GEMINI-ATTEMPT-DISPOSITION-R65.json")
            self.assertEqual({key: file_paths[key] for key in (
                "r65_planner_stop_report", "r65_planner_handoff",
                "r65_focused_harness_failure_evidence", "r65_capture_interface_contract",
                "r65_harness_contract", "r65_preedit_manifest",
                "r65_preedit_implementation_diff",
            )}, {
                "r65_planner_stop_report": "R6-A-R65-RECOVERY-PLANNER-STOP-REPORT.md",
                "r65_planner_handoff": "R6-A-R65-PLANNER-HANDOFF-20261023.md",
                "r65_focused_harness_failure_evidence": (
                    "R6-A-R65-RECOVERY-FOCUSED-HARNESS-FAILURE-EVIDENCE.json"),
                "r65_capture_interface_contract": "R6-A-R65-RECOVERY-CAPTURE-INTERFACE-CONTRACT.md",
                "r65_harness_contract": "R6-A-R65-RECOVERY-HARNESS-CONTRACT.md",
                "r65_preedit_manifest": (
                    "R6-A-R65-RECOVERY-PREEDIT-20261023T123127Z/pre-edit-manifest.json"),
                "r65_preedit_implementation_diff": (
                    "R6-A-R65-RECOVERY-PREEDIT-20261023T123127Z/R65-implementation.diff"),
            })
            original_files: dict[str, bytes] = {}
            live_storage_root = Path(run_r6a_execution.__file__).resolve().parent
            for key, relative in file_paths.items():
                if key == run_r6a_execution.R65_CANDIDATE_FILE_KEY:
                    continue
                path = g0c_repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                changed_source_relative = next((source_path.removeprefix("experiments/storage/")
                                                for source_path in
                                                run_r6a_execution.R65_CHANGED_SOURCE_FILES
                                                if source_path == relative), None)
                contents = ((live_storage_root / changed_source_relative).read_bytes()
                            if changed_source_relative is not None else
                            plan_bytes if key == "r65_execution_plan" else
                            (self.repo / recovery_plan["prebuild_evidence"]["manifest_path"]).read_bytes()
                            if key in {"r65_candidate4_preflight_manifest",
                                       "r65_candidate4_stability_preflight_manifest"} else
                            b"diff --git a/experiments/storage/build_retention.py b/experiments/storage/build_retention.py\n"
                            if key == run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY else
                            f"fixture:{key}\n".encode())
                path.write_bytes(contents)
                original_files[key] = contents
            candidate_path, candidate_bytes, candidate_descriptor = self._write_r65_candidate_fixture(
                g0c_repo, plan=recovery_plan, plan_relative=plan_relative, plan_bytes=plan_bytes,
                file_paths=file_paths, source_bytes=original_files)
            canonical_plan_sha = hashlib.sha256(
                retention._canonical_json(recovery_plan)).hexdigest()
            file_rows = {
                key: {"path": relative,
                      "sha256": hashlib.sha256(original_files[key]).hexdigest(),
                      "size_bytes": len(original_files[key])}
                for key, relative in file_paths.items()
            }
            binding = {
                "schema_version": 1, "milestone": "R65", "status": "PASS",
                "contract_sha256": hashlib.sha256(original_files["r65_dual_graph_contract"]).hexdigest(),
                "execution_plan": {
                    "path": plan_relative,
                    "sha256": hashlib.sha256(plan_bytes).hexdigest(),
                    "canonical_sha256": canonical_plan_sha,
                },
                "candidate": candidate_descriptor,
                "files": file_rows,
            }
            runner_relative = file_paths["r65_execution_runner"]
            runner_record = file_rows["r65_execution_runner"]
            runner_bytes = (g0c_repo / runner_relative).read_bytes()
            self.assertEqual(runner_record["sha256"], hashlib.sha256(runner_bytes).hexdigest())
            self.assertEqual(runner_record["size_bytes"], len(runner_bytes))
            roster_runner_record = file_rows["r6a_runner_control"]
            self.assertEqual(roster_runner_record["sha256"], hashlib.sha256(runner_bytes).hexdigest())
            self.assertEqual(roster_runner_record["size_bytes"], len(runner_bytes))
            review = {"status": "PASS", "findings": [], "r65_binding": binding}
            review_bytes = json.dumps(review, sort_keys=True).encode()
            valid_binding = run_r6a_execution._validate_r65_g0c_binding(
                review_bytes, repo=g0c_repo, plan_path=plan_path,
                plan_bytes=plan_bytes, plan_canonical_sha256=canonical_plan_sha,
                validated_plan=recovery_plan)
            self.assertRegex(valid_binding["binding_sha256"], r"^[0-9a-f]{64}$")
            self._r65_case_assertions.add(19)

            for label, mutate in (
                    ("candidate descriptor missing", lambda value: value["r65_binding"].pop("candidate")),
                    ("candidate row missing", lambda value: value["r65_binding"]["files"].pop(
                        run_r6a_execution.R65_CANDIDATE_FILE_KEY)),
                    ("candidate diff row missing", lambda value: value["r65_binding"]["files"].pop(
                        run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY))):
                malformed = copy.deepcopy(review)
                mutate(malformed)
                with self.subTest(g0c_candidate=label), self.assertRaises(RuntimeError):
                    run_r6a_execution._validate_r65_g0c_binding(
                        json.dumps(malformed, sort_keys=True).encode(), repo=g0c_repo,
                        plan_path=plan_path, plan_bytes=plan_bytes,
                        plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            candidate_path.unlink()
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    review_bytes, repo=g0c_repo, plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            candidate_path.write_bytes(candidate_bytes + b"tampered after G0C\n")
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    review_bytes, repo=g0c_repo, plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            candidate_path.write_bytes(candidate_bytes)
            diff_path = g0c_repo / file_paths[run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY]
            diff_bytes = original_files[run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY]
            diff_path.unlink()
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    review_bytes, repo=g0c_repo, plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            diff_path.write_bytes(diff_bytes + b"tampered diff\n")
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    review_bytes, repo=g0c_repo, plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            diff_path.write_bytes(diff_bytes)
            changed_hash_candidate = copy.deepcopy(review)
            candidate_object = json.loads(candidate_bytes)
            candidate_object["changed_files"]["experiments/storage/build_retention.py"] = "0" * 64
            changed_candidate_bytes = (json.dumps(candidate_object, sort_keys=True, indent=2) + "\n").encode()
            candidate_path.write_bytes(changed_candidate_bytes)
            changed_hash_candidate["r65_binding"]["candidate"] = {
                "path": candidate_path.name,
                "sha256": hashlib.sha256(changed_candidate_bytes).hexdigest(),
                "canonical_sha256": hashlib.sha256(
                    retention._canonical_json(candidate_object)).hexdigest(),
            }
            changed_hash_candidate["r65_binding"]["files"][
                run_r6a_execution.R65_CANDIDATE_FILE_KEY] = {
                    "path": candidate_path.name,
                    "sha256": hashlib.sha256(changed_candidate_bytes).hexdigest(),
                    "size_bytes": len(changed_candidate_bytes),
                }
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    json.dumps(changed_hash_candidate, sort_keys=True).encode(),
                    repo=g0c_repo, plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            for field, invalid_value in (("path", "R6-A-R65-RECOVERY-CANDIDATE-2.diff"),
                                         ("sha256", "0" * 64)):
                invalid_diff_descriptor = copy.deepcopy(review)
                invalid_candidate = json.loads(candidate_bytes)
                invalid_candidate["exact_diff"][field] = invalid_value
                invalid_candidate_bytes = (
                    json.dumps(invalid_candidate, sort_keys=True, indent=2) + "\n").encode()
                candidate_path.write_bytes(invalid_candidate_bytes)
                invalid_diff_descriptor["r65_binding"]["candidate"] = {
                    "path": candidate_path.name,
                    "sha256": hashlib.sha256(invalid_candidate_bytes).hexdigest(),
                    "canonical_sha256": hashlib.sha256(
                        retention._canonical_json(invalid_candidate)).hexdigest(),
                }
                invalid_diff_descriptor["r65_binding"]["files"][
                    run_r6a_execution.R65_CANDIDATE_FILE_KEY] = {
                        "path": candidate_path.name,
                        "sha256": hashlib.sha256(invalid_candidate_bytes).hexdigest(),
                        "size_bytes": len(invalid_candidate_bytes),
                    }
                with self.subTest(candidate_diff_descriptor=field), self.assertRaises(RuntimeError):
                    run_r6a_execution._validate_r65_g0c_binding(
                        json.dumps(invalid_diff_descriptor, sort_keys=True).encode(),
                        repo=g0c_repo, plan_path=plan_path, plan_bytes=plan_bytes,
                        plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            candidate_path.write_bytes(candidate_bytes)
            missing_binding = copy.deepcopy(review)
            missing_binding.pop("r65_binding")
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    json.dumps(missing_binding, sort_keys=True).encode(), repo=g0c_repo,
                    plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            extra_row = copy.deepcopy(review)
            extra_row["r65_binding"]["files"]["unreviewed_file"] = {
                "path": "unreviewed_file", "sha256": "0" * 64, "size_bytes": 1}
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    json.dumps(extra_row, sort_keys=True).encode(), repo=g0c_repo,
                    plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            self._r65_case_assertions.add(22)
            malformed_row = copy.deepcopy(review)
            malformed_row["r65_binding"]["files"]["r65_execution_runner"]["size_bytes"] = True
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    json.dumps(malformed_row, sort_keys=True).encode(), repo=g0c_repo,
                    plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            for missing_key in (
                    "r65_execution_runner", "r65_execution_plan", "r65_dual_graph_contract",
                    "build_retention_tests", "r65_orchestrator_reconciliation",
                    "r65_recovery_orchestrator_reconciliation",
                    "r65_recovery_session_plan_reconciliation",
                    "r65_audit_session_plan_binding_trace",
                    "r65_audit_session_plan_guard_review", "r65_audit_fixture_fidelity",
                    "r65_audit_runner_reservation_review", "r64_formal_disposition",
                    "r65_recovery_development_iterations",
                    "r65_planner_stop_report", "r65_planner_handoff",
                    "r65_focused_harness_failure_evidence", "r65_capture_interface_contract",
                    "r65_harness_contract", "r65_preedit_manifest",
                    "r65_preedit_implementation_diff", "r65_candidate4_preflight_builder",
                    "r65_candidate4_preflight_result",
                    "r65_candidate4_preflight_manifest",
                    "r65_candidate4_stability_preflight_result",
                    "r65_candidate4_stability_preflight_manifest",
                    run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY):
                malformed = copy.deepcopy(review)
                malformed["r65_binding"]["files"].pop(missing_key)
                with self.subTest(g0c_binding_missing=missing_key), self.assertRaises(RuntimeError):
                    run_r6a_execution._validate_r65_g0c_binding(
                        json.dumps(malformed, sort_keys=True).encode(), repo=g0c_repo,
                        plan_path=plan_path, plan_bytes=plan_bytes,
                        plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            self._r65_case_assertions.add(23)
            for stale_key in (
                    "r65_execution_runner", "r65_dual_graph_contract", "build_retention_tests",
                    "r65_orchestrator_reconciliation", "r65_recovery_orchestrator_reconciliation",
                    "r64_preserved_baseline", "r65_recovery_development_iterations",
                    "r65_planner_stop_report", "r65_planner_handoff",
                    "r65_focused_harness_failure_evidence", "r65_capture_interface_contract",
                    "r65_harness_contract", "r65_preedit_manifest",
                    "r65_preedit_implementation_diff", "r65_candidate4_preflight_builder",
                    "r65_candidate4_preflight_result",
                    "r65_candidate4_stability_preflight_result",
                    "r65_candidate4_stability_preflight_manifest",
                    run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY):
                path = g0c_repo / file_paths[stale_key]
                path.write_bytes(original_files[stale_key] + b"stale")
                with self.subTest(g0c_binding_stale=stale_key), self.assertRaises(RuntimeError):
                    run_r6a_execution._validate_r65_g0c_binding(
                        review_bytes, repo=g0c_repo, plan_path=plan_path,
                        plan_bytes=plan_bytes, plan_canonical_sha256=canonical_plan_sha,
                        validated_plan=recovery_plan)
                path.write_bytes(original_files[stale_key])
            self._r65_case_assertions.add(24)
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    review_bytes, repo=g0c_repo, plan_path=plan_path,
                    plan_bytes=plan_bytes + b"stale",
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            canonical_substitution = copy.deepcopy(review)
            canonical_substitution["r65_binding"]["execution_plan"]["canonical_sha256"] = "0" * 64
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    json.dumps(canonical_substitution, sort_keys=True).encode(),
                    repo=g0c_repo, plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)
            self._r65_case_assertions.add(10)

            integral_float_size = copy.deepcopy(review)
            size_row = integral_float_size["r65_binding"]["files"]["r65_execution_runner"]
            size_row["size_bytes"] = float(size_row["size_bytes"])
            with self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_g0c_binding(
                    json.dumps(integral_float_size, sort_keys=True).encode(),
                    repo=g0c_repo, plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=canonical_plan_sha, validated_plan=recovery_plan)

        single_lease, single_session = self._start_session("dual-finalization-single-meson-compatibility")
        self._finish_session_steps(single_session)
        single_receipt = single_session.finalize()
        self.assertEqual(single_receipt["status"], "COMPLETED_AND_AUTO_RETIRED")
        single_manifest, _single_manifest_sha256 = retention._verify_build_manifest(single_lease.build_id)
        single_identity = single_manifest["build_identity"]
        self.assertEqual(single_identity["build_system"], "meson")
        self.assertRegex(single_identity["config_key_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotIn("build_graph_identities", single_identity)
        self._r65_case_assertions.add(30)

    def _r65_recovery_plan(self, plan: dict[str, object], *, build_id: str | None = None,
                           admission_id: str | None = None) -> dict[str, object]:
        value = copy.deepcopy(plan)
        if build_id is not None:
            value["build_id"] = build_id
        admission = value.get("admission_identity")
        if isinstance(admission, dict) and admission_id is not None:
            admission["admission_id"] = admission_id
        descriptor = value.get("prebuild_evidence")
        if isinstance(descriptor, dict):
            target_relative = run_r6a_execution.R65_GOVERNANCE_FILES[
                "r65_candidate4_preflight_manifest"]
            target = self.repo / target_relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source_manifest = self.repo / descriptor["manifest_path"]
            target.write_bytes(source_manifest.read_bytes())
            descriptor["manifest_path"] = target_relative
        return value

    @staticmethod
    def _write_r65_candidate_fixture(repo: Path, *, plan: dict[str, object],
                                     plan_relative: str, plan_bytes: bytes,
                                     file_paths: dict[str, str],
                                     source_bytes: dict[str, bytes]) -> tuple[Path, bytes, dict[str, str]]:
        candidate_relative = "R6-A-R65-RECOVERY-CANDIDATE-4.json"
        candidate_path = repo / candidate_relative
        candidate_inputs = {
            key: {"path": relative,
                  "sha256": hashlib.sha256(source_bytes[key]).hexdigest(),
                  "size_bytes": len(source_bytes[key])}
            for key, relative in file_paths.items()
            if key not in {"r65_execution_plan", run_r6a_execution.R65_CANDIDATE_FILE_KEY}
        }
        expected_plan = {
            "path": plan_relative,
            "sha256": hashlib.sha256(plan_bytes).hexdigest(),
            "canonical_sha256": hashlib.sha256(retention._canonical_json(plan)).hexdigest(),
        }
        candidate = {
            "schema_version": 1,
            "milestone": "R65",
            "source_identity": plan["source_identity"],
            "execution_plan": expected_plan,
            "inputs": candidate_inputs,
            "changed_files": {
                path: hashlib.sha256(source_bytes[key]).hexdigest()
                for path, key in run_r6a_execution.R65_CHANGED_SOURCE_FILES.items()
            },
            "exact_diff": {
                "path": file_paths[run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY],
                "sha256": hashlib.sha256(
                    source_bytes[run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY]).hexdigest(),
            },
        }
        candidate_bytes = (json.dumps(candidate, sort_keys=True, indent=2) + "\n").encode()
        candidate_path.write_bytes(candidate_bytes)
        candidate_binding = {
            "path": candidate_relative,
            "sha256": hashlib.sha256(candidate_bytes).hexdigest(),
            "canonical_sha256": hashlib.sha256(retention._canonical_json(candidate)).hexdigest(),
        }
        source_bytes[run_r6a_execution.R65_CANDIDATE_FILE_KEY] = candidate_bytes
        file_paths[run_r6a_execution.R65_CANDIDATE_FILE_KEY] = candidate_relative
        return candidate_path, candidate_bytes, candidate_binding

    def _assert_r65_recovery_plan_matrix(self) -> None:
        build_id = "r6a-r65-recovery-dual-lineage-plan-contract"
        lease = self._session_lease(build_id, dual_graph=True)
        assert lease.session_plan is not None
        admission_id = "r6a-r65-recovery-dual-lineage-plan-contract"
        valid = self._r65_recovery_plan(lease.session_plan, admission_id=admission_id)
        run_r6a_execution._validate_r65_recovery_plan(valid)
        self._r65_case_assertions.add(28)
        self.assertRegex(valid["build_id"],
                         r"^r6a-r65-recovery-dual-lineage-[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")

        plan_hash_substitution = self._r65_recovery_plan(valid)
        plan_hash_substitution["build_configuration_key_sha256"] = hashlib.sha256(
            retention._canonical_json(plan_hash_substitution)).hexdigest()
        with self.subTest(r65_prebuild_key="plan hash cannot replace graph pair"), \
                self.assertRaises(RuntimeError):
            run_r6a_execution._validate_r65_recovery_plan(plan_hash_substitution)
        wrong_pair_key = self._r65_recovery_plan(valid)
        wrong_pair_key["build_configuration_key_sha256"] = "0" * 64
        with self.subTest(r65_prebuild_key="wrong ordered graph pair"), \
                self.assertRaises(RuntimeError):
            run_r6a_execution._validate_r65_recovery_plan(wrong_pair_key)
        wrong_shader_hash = self._r65_recovery_plan(valid)
        wrong_shader_rows = wrong_shader_hash["build_graphs"][0]["prebuild_identity"][
            "meson_identity"]["generated_shader_identity"]
        wrong_shader_rows[0]["sha256"] = "0" * 64
        with self.subTest(r65_prebuild_key="prebuild shader hash differs from manifest"), \
                self.assertRaises(retention.RetentionError):
            run_r6a_execution._validate_r65_recovery_plan(wrong_shader_hash)

        symlinked_parent_plan = self._r65_recovery_plan(valid)
        evidence_root = self.repo / symlinked_parent_plan["prebuild_evidence"]["evidence_root"]
        manifest_path = self.repo / symlinked_parent_plan["prebuild_evidence"]["manifest_path"]
        evidence_manifest = json.loads(manifest_path.read_bytes())
        nested_parents = sorted({
            Path(row["path"]).parent
            for row in evidence_manifest["evidence_files"]
            if len(Path(row["path"]).parts) >= 3
        }, key=lambda path: (len(path.parts), path.as_posix()))
        self.assertTrue(nested_parents, "synthetic evidence must include a nested directory")
        linked_parent = evidence_root / nested_parents[0]
        outside_root = Path(tempfile.mkdtemp(prefix="fgmetal-r65-evidence-parent-", dir="/private/tmp"))
        outside_copy = outside_root / "evidence-parent"
        backup_parent = linked_parent.with_name(linked_parent.name + ".no-follow-test-backup")
        self.assertFalse(backup_parent.exists())
        shutil.copytree(linked_parent, outside_copy)
        linked_parent.rename(backup_parent)
        linked_parent.symlink_to(outside_copy, target_is_directory=True)
        try:
            with self.subTest(r65_prebuild_key="nested evidence parent symlink escapes root"), \
                    self.assertRaises(retention.RetentionError):
                run_r6a_execution._validate_r65_recovery_plan(symlinked_parent_plan)
        finally:
            linked_parent.unlink()
            backup_parent.rename(linked_parent)
            shutil.rmtree(outside_root)

        invalid_plans = {
            "legacy dual-lineage ID": self._r65_recovery_plan(
                valid, build_id="r6a-r65-dual-lineage-legacy"),
            "old contaminated R64 build ID": self._r65_recovery_plan(
                valid, build_id="r6a-r64-dual-lineage-build"),
            "non-R65 build ID": self._r65_recovery_plan(
                valid, build_id="r6a-r6a-dual-lineage-build"),
            "empty run ID": self._r65_recovery_plan(
                valid, build_id="r6a-r65-recovery-dual-lineage-"),
            "non-R65 admission ID": self._r65_recovery_plan(
                valid, admission_id="r6a-r64-admission"),
            "missing graph pair": self._r65_recovery_plan(valid),
        }
        invalid_plans["missing graph pair"].pop("build_graphs")
        legacy_dual_label = self._r65_recovery_plan(valid)
        legacy_dual_label.pop("build_graphs")
        invalid_plans["legacy single graph labeled dual"] = legacy_dual_label
        swapped = self._r65_recovery_plan(valid)
        swapped["build_graphs"].reverse()
        invalid_plans["swapped graph pair"] = swapped
        duplicate = self._r65_recovery_plan(valid)
        duplicate["build_graphs"][1]["graph_id"] = "windows-product-r6a"
        invalid_plans["duplicate graph ID"] = duplicate
        wrong = self._r65_recovery_plan(valid)
        wrong["build_graphs"][0]["graph_id"] = "unreviewed-windows-graph"
        invalid_plans["wrong graph ID"] = wrong
        for label, plan in invalid_plans.items():
            with self.subTest(r65_recovery_plan=label), self.assertRaises(RuntimeError):
                run_r6a_execution._validate_r65_recovery_plan(plan)
        self._r65_case_assertions.add(29)

        source_mismatch = self._r65_recovery_plan(valid)
        source_mismatch["build_graphs"][1]["source_identity"]["sha256"] = "0" * 64
        with self.subTest(r65_recovery_plan="different source identities between graphs"), \
                self.assertRaises(retention.RetentionError):
            self._validate_dual_graph_plan(lease, source_mismatch)
        self._r65_case_assertions.add(17)

    def _prepare_r65_reservation_binding(self) -> tuple[retention.BuildLease, Path, Path, dict]:
        build_id = "r6a-r65-recovery-dual-lineage-reservation-freshness"
        lease = self._session_lease(build_id, dual_graph=True, owner_experiment=build_id)
        assert lease.session_plan is not None
        admission_identity = {
            "admission_id": build_id,
            "sha256": hashlib.sha256(retention._canonical_json({"admission_id": build_id})).hexdigest(),
        }
        lease.session_plan["admission_identity"] = admission_identity
        lease.session_plan = self._r65_recovery_plan(lease.session_plan)
        lease.session_plan_sha256 = hashlib.sha256(
            retention._canonical_json(lease.session_plan)).hexdigest()
        lease.configuration_key_sha256 = lease.session_plan["build_configuration_key_sha256"]

        plan_relative = "experiments/storage/R6-A-R65-RECOVERY-EXECUTION-PLAN.json"
        plan_path = self.repo / plan_relative
        plan_path.parent.mkdir(parents=True, exist_ok=True)
        plan_raw = (json.dumps(lease.session_plan, sort_keys=True, indent=2) + "\n").encode()
        plan_path.write_bytes(plan_raw)
        canonical_plan_sha = hashlib.sha256(
            retention._canonical_json(lease.session_plan)).hexdigest()

        file_paths = {
            key: (Path("experiments/storage") / relative).as_posix()
            for key, relative in storage_policy.G0C_REVIEW_REQUIRED_FILES.items()
        }
        file_paths.update({
            "r65_execution_runner": "experiments/storage/run_r6a_execution.py",
            "r65_execution_plan": plan_relative,
            run_r6a_execution.R65_CANDIDATE_FILE_KEY:
                "R6-A-R65-RECOVERY-CANDIDATE-4.json",
            run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY:
                "R6-A-R65-RECOVERY-CANDIDATE-4.diff",
            "r65_dual_graph_contract": run_r6a_execution.R65_DUAL_GRAPH_CONTRACT,
            **run_r6a_execution.R65_GOVERNANCE_FILES,
        })
        source_bytes: dict[str, bytes] = {}
        live_storage_root = Path(run_r6a_execution.__file__).resolve().parent
        live_r65_source_paths = set(run_r6a_execution.R65_CHANGED_SOURCE_FILES)
        for key, relative in file_paths.items():
            if key == run_r6a_execution.R65_CANDIDATE_FILE_KEY:
                continue
            selected = self.repo / relative
            selected.parent.mkdir(parents=True, exist_ok=True)
            changed_source_relative = relative.removeprefix("experiments/storage/") \
                if relative in live_r65_source_paths else None
            contents = ((live_storage_root / changed_source_relative).read_bytes()
                        if changed_source_relative is not None else
                        plan_raw if key == "r65_execution_plan" else
                        (self.repo / lease.session_plan["prebuild_evidence"]["manifest_path"]).read_bytes()
                        if key in {"r65_candidate4_preflight_manifest",
                                   "r65_candidate4_stability_preflight_manifest"} else
                        b"diff --git a/experiments/storage/build_retention.py b/experiments/storage/build_retention.py\n"
                        if key == run_r6a_execution.R65_CANDIDATE_DIFF_FILE_KEY else
                        f"fixture:{key}\n".encode())
            selected.write_bytes(contents)
            source_bytes[key] = contents
        candidate_path, candidate_bytes, candidate_descriptor = self._write_r65_candidate_fixture(
            self.repo, plan=lease.session_plan, plan_relative=plan_relative,
            plan_bytes=plan_raw, file_paths=file_paths, source_bytes=source_bytes)
        contract_sha = hashlib.sha256(source_bytes["r65_dual_graph_contract"]).hexdigest()
        binding = {
            "schema_version": 1,
            "milestone": "R65",
            "status": "PASS",
            "contract_sha256": contract_sha,
            "execution_plan": {
                "path": plan_relative,
                "sha256": hashlib.sha256(plan_raw).hexdigest(),
                "canonical_sha256": canonical_plan_sha,
            },
            "candidate": candidate_descriptor,
            "files": {
                key: {
                    "path": relative,
                    "sha256": hashlib.sha256(source_bytes[key]).hexdigest(),
                    "size_bytes": len(source_bytes[key]),
                }
                for key, relative in file_paths.items()
            },
        }
        review_path = self.repo / "experiments/storage" / storage_policy.G0C_REVIEW_RECORD_NAME
        review_path.write_bytes((json.dumps(
            {"status": "PASS", "findings": [], "r65_binding": binding},
            sort_keys=True, indent=2) + "\n").encode())
        binding_sha = hashlib.sha256(retention._canonical_json(binding)).hexdigest()
        # `_session_lease` seeds an owner manifest for ordinary tests. Remove
        # that synthetic preclaim file so this fixture follows R65's real order:
        # exclusive claim first, signed admission manifest second.
        preclaim_manifest = self.manifests / f"{build_id}.json"
        if preclaim_manifest.exists():
            preclaim_manifest.unlink()
        admission_claim = run_r6a_execution._claim_r65_admission_id(
            build_id, canonical_plan_sha)
        manifest_fields = {
            "dual_build_graph_contract_version": 2,
            "dual_build_graphs": 1,
            "r65_g0c_binding_sha256": binding_sha,
            "dual_graph_finalization_contract_sha256": contract_sha,
            "candidate_hashes": {
                "r65_g0c_binding_sha256": binding_sha,
                "dual_graph_finalization_contract_sha256": contract_sha,
                "plan_file_sha256": hashlib.sha256(plan_raw).hexdigest(),
                "plan_canonical_sha256": canonical_plan_sha,
                "r65_candidate_sha256": candidate_descriptor["sha256"],
                "r65_admission_claim_sha256": admission_claim["claim_sha256"],
            },
        }
        self._write_owner_manifest(
            lease.source_tree_hash,
            lease.configuration_key_sha256,
            build_type="release",
            dependency_state=lease.metadata["dependency_state"],
            session_plan_sha256=lease.session_plan_sha256,
            admission_identity=admission_identity,
            experiment_id=build_id,
            additional_manifest_fields=manifest_fields,
        )
        manifest = json.loads((self.manifests / f"{build_id}.json").read_bytes())
        return lease, plan_path, review_path, manifest

    def _assert_r65_reservation_binding_freshness(self) -> None:
        lease, _plan_path, _review_path, manifest = self._prepare_r65_reservation_binding()
        retention._validate_r65_reservation_binding(
            manifest, build_id=lease.build_id, session_plan=lease.session_plan,
            session_plan_sha256=lease.session_plan_sha256)

        missing_plan_id = "r6a-r65-recovery-dual-lineage-missing-plan"
        missing_plan_lease = self._lease(missing_plan_id)
        missing_plan_manifest_path = self.manifests / "synthetic-test.json"
        missing_plan_manifest = json.loads(missing_plan_manifest_path.read_bytes())
        missing_plan_manifest.update({
            "experiment_id": missing_plan_id,
            "execution_admission_identity": {
                "admission_id": missing_plan_id,
                "sha256": "2" * 64,
            },
            "dual_build_graph_contract_version": 2,
            "dual_build_graphs": 1,
            "r65_g0c_binding_sha256": "0" * 64,
            "dual_graph_finalization_contract_sha256": "1" * 64,
        })
        self.assertNotIn("build_session_plan_sha256", missing_plan_manifest)
        missing_plan_manifest_path.write_text(json.dumps(missing_plan_manifest), encoding="utf-8")
        with self.assertRaises(retention.RetentionError):
            missing_plan_lease.reserve()
        self.assertFalse(missing_plan_lease.path.exists())
        self.assertFalse((retention.LEASE_ROOT / f"{missing_plan_id}.json").exists())
        self._r65_case_assertions.add(20)

        mismatched_marker = dict(manifest)
        mismatched_marker["dual_build_graphs"] = 0
        with self.assertRaises(retention.RetentionError):
            retention._validate_r65_reservation_binding(
                mismatched_marker, build_id=lease.build_id, session_plan=lease.session_plan,
                session_plan_sha256=lease.session_plan_sha256)

        manifest_path = self.manifests / f"{lease.owner_experiment}.json"
        signed_manifest_raw = manifest_path.read_bytes()
        missing_binding_manifest = json.loads(signed_manifest_raw)
        for field in ("dual_build_graph_contract_version", "dual_build_graphs",
                      "r65_g0c_binding_sha256", "dual_graph_finalization_contract_sha256"):
            missing_binding_manifest.pop(field, None)
        manifest_path.write_text(json.dumps(missing_binding_manifest), encoding="utf-8")
        with self.assertRaises(retention.RetentionError):
            lease.reserve()
        self.assertFalse(lease.path.exists())
        self.assertFalse((retention.LEASE_ROOT / f"{lease.build_id}.json").exists())
        manifest_path.write_bytes(signed_manifest_raw)

        signed_manifest = json.loads(signed_manifest_raw)
        signed_manifest["dual_build_graphs"] = 0
        manifest_path.write_text(json.dumps(signed_manifest), encoding="utf-8")
        with self.assertRaises(retention.RetentionError):
            lease.reserve()
        self.assertFalse(lease.path.exists())
        self.assertFalse((retention.LEASE_ROOT / f"{lease.build_id}.json").exists())
        manifest_path.write_bytes(signed_manifest_raw)

        review_path = self.repo / "experiments/storage" / storage_policy.G0C_REVIEW_RECORD_NAME
        signed_review_raw = review_path.read_bytes()
        tampered_review = json.loads(signed_review_raw)
        tampered_review["r65_binding"]["files"]["unreviewed_file"] = {
            "path": "unreviewed_file", "sha256": "0" * 64, "size_bytes": 1,
        }
        review_path.write_text(json.dumps(tampered_review), encoding="utf-8")
        with self.assertRaises(retention.RetentionError):
            lease.reserve()
        self.assertFalse(lease.path.exists())
        self.assertFalse((retention.LEASE_ROOT / f"{lease.build_id}.json").exists())
        review_path.write_bytes(signed_review_raw)
        self._r65_case_assertions.add(25)

        stale_governance = self.repo / run_r6a_execution.R65_GOVERNANCE_FILES[
            "r65_recovery_orchestrator_reconciliation"]
        stale_governance_raw = stale_governance.read_bytes()
        stale_governance.write_bytes(stale_governance.read_bytes() + b"stale after signing\n")
        with self.assertRaises(retention.RetentionError):
            lease.reserve()
        self.assertFalse(lease.path.exists())
        self.assertFalse((retention.LEASE_ROOT / f"{lease.build_id}.json").exists())
        stale_governance.write_bytes(stale_governance_raw)
        self._r65_case_assertions.add(21)

        # A prior retired identity remains claimed even after its build directory
        # is absent. Refusal must precede pending retirement and preserve bytes.
        old_record = {
            "schema_version": 1, "build_id": lease.build_id,
            "path": str(lease.path), "kind": "dxvk", "owner_experiment": lease.owner_experiment,
            "purpose": "synthetic prior retained identity", "state": "RETIRED",
            "created_at_unix": 1.0, "source_tree_hash": lease.source_tree_hash,
            "expected_bytes": lease.expected_bytes,
        }
        retention._store_lease(old_record)
        old_record_raw = (retention.LEASE_ROOT / f"{lease.build_id}.json").read_bytes()
        self.assertFalse(lease.path.exists())
        with mock.patch.object(retention, "_retire_pending_locked",
                               side_effect=AssertionError("retirement must not precede ID reuse check")):
            with self.assertRaises(retention.RetentionError):
                lease.reserve()
        self.assertEqual((retention.LEASE_ROOT / f"{lease.build_id}.json").read_bytes(), old_record_raw)
        (retention.LEASE_ROOT / f"{lease.build_id}.json").unlink()
        self._r65_case_assertions.add(26)

        retention.RECEIPT_ROOT.mkdir(parents=True, exist_ok=True)
        receipt_path = retention.RECEIPT_ROOT / f"{lease.build_id}.json"
        receipt_path.write_text("synthetic prior cleanup receipt\n", encoding="utf-8")
        with self.assertRaises(retention.RetentionError):
            lease.reserve()
        self.assertFalse(lease.path.exists())
        self.assertFalse((retention.LEASE_ROOT / f"{lease.build_id}.json").exists())
        receipt_path.unlink()

        claim_path = (self.cache / run_r6a_execution.R65_ADMISSION_CLAIM_DIRECTORY
                      / f"{lease.session_plan['admission_identity']['admission_id']}.claim")
        original_claim = claim_path.read_bytes()
        manifest_path = self.manifests / f"{lease.owner_experiment}.json"
        original_manifest = manifest_path.read_bytes()
        claim_path.unlink()
        with self.assertRaises(retention.RetentionError):
            lease.reserve()
        self.assertFalse(lease.path.exists())
        claim_path.write_bytes(original_claim)
        claim_path.chmod(0o600)

        invalid_claim = json.loads(original_claim)
        invalid_claim["admission_id"] = "r6a-r65-wrong-claim-identity"
        invalid_claim_raw = retention._canonical_json(invalid_claim)
        claim_path.write_bytes(invalid_claim_raw)
        claim_path.chmod(0o600)
        invalid_manifest = json.loads(original_manifest)
        invalid_manifest["candidate_hashes"]["r65_admission_claim_sha256"] = hashlib.sha256(
            invalid_claim_raw).hexdigest()
        manifest_path.write_bytes(retention._canonical_json(invalid_manifest))
        with self.assertRaisesRegex(retention.RetentionError, "exact admission and frozen plan"):
            lease.reserve()
        self.assertFalse(lease.path.exists())
        claim_path.write_bytes(original_claim)
        claim_path.chmod(0o600)
        manifest_path.write_bytes(original_manifest)

        # The fully bound synthetic R65 admission must pass the real reservation
        # path and persist matching plan, admission, and requested-key metadata.
        try:
            self.assertEqual(lease.reserve(), lease.path)
            reserved = retention._load_lease(lease.build_id)
            self.assertEqual(reserved["requested_config_key_sha256"],
                             lease.session_plan["build_configuration_key_sha256"])
            self.assertEqual(reserved["session_v3"]["plan_sha256"], lease.session_plan_sha256)
            self.assertEqual(reserved["session_v3"]["admission_identity"],
                             lease.session_plan["admission_identity"])
            self.assertEqual(reserved["state"], "ACTIVE")
            self._r65_case_assertions.add(19)
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    def _assert_r65_admission_claim_is_atomic(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r65-admission-claims-", dir="/private/tmp") as name:
            claims_root = Path(name) / "claims"
            plan_sha = "a" * 64
            broken_link_id = "r6a-r65-synthetic-broken-link"
            broken_link = self.manifests / f"{broken_link_id}.json"
            broken_link.symlink_to("missing-r65-manifest-target")
            with self.assertRaises(RuntimeError):
                run_r6a_execution._assert_r65_admission_id_unclaimed(broken_link_id)
            broken_link.unlink()

            retained_id = "r6a-r65-synthetic-retained-alias"
            retained_alias = self.manifests / "retained-old-manifest.json"
            retained_alias.write_text(json.dumps({"experiment_id": retained_id}), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "retained manifest"):
                run_r6a_execution._assert_r65_admission_id_unclaimed(retained_id)
            retained_alias.unlink()

            sequential_id = "r6a-r65-synthetic-sequential-claim"
            first = run_r6a_execution._claim_r65_admission_id(
                sequential_id, plan_sha, claims_root=claims_root)
            self.assertTrue(Path(first["claim_path"]).is_file())
            readback = run_r6a_execution._read_r65_admission_claim(
                sequential_id, plan_sha, expected_sha256=first["claim_sha256"],
                claims_root=claims_root)
            self.assertEqual(readback["admission_id"], sequential_id)
            self.assertEqual(readback["plan_canonical_sha256"], plan_sha)
            claim_info = Path(first["claim_path"]).stat(follow_symlinks=False)
            self.assertTrue(stat.S_ISREG(claim_info.st_mode))
            self.assertEqual(claim_info.st_nlink, 1)
            self.assertEqual(stat.S_IMODE(claim_info.st_mode), 0o600)
            self.assertEqual(claim_info.st_uid, os.getuid())
            with self.assertRaisesRegex(RuntimeError, "already been claimed"):
                run_r6a_execution._claim_r65_admission_id(
                    sequential_id, plan_sha, claims_root=claims_root)

            concurrent_id = "r6a-r65-synthetic-concurrent-claim"
            barrier = threading.Barrier(8)
            outcomes: list[tuple[str, object]] = []
            outcomes_lock = threading.Lock()

            def claim_from_thread() -> None:
                barrier.wait(timeout=5)
                try:
                    result: tuple[str, object] = (
                        "claimed", run_r6a_execution._claim_r65_admission_id(
                            concurrent_id, plan_sha, claims_root=claims_root))
                except RuntimeError as error:
                    result = ("rejected", error)
                with outcomes_lock:
                    outcomes.append(result)

            workers = [threading.Thread(target=claim_from_thread) for _ in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)
            self.assertTrue(all(not worker.is_alive() for worker in workers))
            self.assertEqual(sum(kind == "claimed" for kind, _ in outcomes), 1)
            self.assertEqual(sum(kind == "rejected" for kind, _ in outcomes), 7)
            claim_record = json.loads((claims_root / f"{concurrent_id}.claim").read_bytes())
            self.assertEqual(claim_record["admission_id"], concurrent_id)
            self.assertEqual(claim_record["plan_canonical_sha256"], plan_sha)

            begin_plan_sha = "b" * 64
            begin_id = "r6a-r65-synthetic-concurrent-manifest"
            begin_claim = run_r6a_execution._claim_r65_admission_id(begin_id, begin_plan_sha)
            begin_barrier = threading.Barrier(8)
            begin_outcomes: list[tuple[str, object]] = []
            begin_outcomes_lock = threading.Lock()

            def begin_from_thread(index: int) -> None:
                begin_barrier.wait(timeout=5)
                try:
                    result: tuple[str, object] = (
                        "created", storage_policy.begin_experiment(
                            experiment_id=begin_id,
                            purpose="synthetic R65 no-replace manifest race",
                            candidate_hashes={
                                "r65_admission_claim_sha256": begin_claim["claim_sha256"],
                            },
                            expected_duration_seconds=300,
                            expected_prefix_bytes=0,
                            expected_build_bytes=0,
                            estimated_persistent_bytes=0,
                            output_dir=self.repo / "experiments/storage" / f"r65-output-{index}",
                            additional={
                                "build_session_plan_sha256": begin_plan_sha,
                                "execution_admission_identity": {"admission_id": begin_id},
                            },
                        ))
                except Exception as error:
                    result = ("rejected", error)
                with begin_outcomes_lock:
                    begin_outcomes.append(result)

            storage_policy._manifest_signing_key(create=True)
            with (mock.patch.object(storage_policy, "_gate0_blockers", return_value=[]),
                  mock.patch.object(storage_policy, "uncertified_prefixes", return_value=[]),
                  mock.patch.object(storage_policy, "preserved_prefixes", return_value=[]),
                  mock.patch.object(storage_policy, "_active_lease_markers", return_value=[])):
                begin_workers = [threading.Thread(target=begin_from_thread, args=(index,))
                                 for index in range(8)]
                for worker in begin_workers:
                    worker.start()
                for worker in begin_workers:
                    worker.join(timeout=10)
            self.assertTrue(all(not worker.is_alive() for worker in begin_workers))
            created = [value for kind, value in begin_outcomes if kind == "created"]
            rejected = [value for kind, value in begin_outcomes if kind == "rejected"]
            self.assertEqual(len(created), 1, begin_outcomes)
            self.assertEqual(len(rejected), 7, begin_outcomes)
            persisted_manifest = self.manifests / f"{begin_id}.json"
            persisted_raw = persisted_manifest.read_bytes()
            self.assertEqual(
                persisted_raw,
                storage_policy._canonical_pretty_json_bytes(created[0]),
            )

            # Simulate a caller that does not take the storage lock winning the
            # filesystem race after begin_experiment's absence check. Exclusive
            # publication must preserve those bytes rather than replace them.
            interloper_id = "r6a-r65-synthetic-uncoordinated-writer"
            interloper_claim = run_r6a_execution._claim_r65_admission_id(
                interloper_id, begin_plan_sha)
            interloper_path = self.manifests / f"{interloper_id}.json"
            interloper_bytes = b'{"writer":"uncoordinated"}\n'
            original_publish = storage_policy._write_json_exclusive

            def publish_after_interloper(path: Path, payload: dict, *,
                                         strict_directory_sync: bool = False) -> None:
                path.write_bytes(interloper_bytes)
                original_publish(path, payload, strict_directory_sync=strict_directory_sync)

            with (mock.patch.object(storage_policy, "_gate0_blockers", return_value=[]),
                  mock.patch.object(storage_policy, "uncertified_prefixes", return_value=[]),
                  mock.patch.object(storage_policy, "preserved_prefixes", return_value=[]),
                  mock.patch.object(storage_policy, "_active_lease_markers", return_value=[]),
                  mock.patch.object(storage_policy, "_write_json_exclusive",
                                    side_effect=publish_after_interloper)):
                with self.assertRaises(FileExistsError):
                    storage_policy.begin_experiment(
                        experiment_id=interloper_id,
                        purpose="synthetic R65 uncoordinated writer collision",
                        candidate_hashes={
                            "r65_admission_claim_sha256": interloper_claim["claim_sha256"],
                        },
                        expected_duration_seconds=300,
                        expected_prefix_bytes=0,
                        expected_build_bytes=0,
                        estimated_persistent_bytes=0,
                        output_dir=self.repo / "experiments/storage/r65-interloper-output",
                        additional={
                            "build_session_plan_sha256": begin_plan_sha,
                            "execution_admission_identity": {"admission_id": interloper_id},
                        },
                    )
            self.assertEqual(interloper_path.read_bytes(), interloper_bytes)
            self._r65_case_assertions.add(27)

    def test_r65_recovery_focused_dual_graph_admission_matrix(self) -> None:
        """Synthetic coverage map: all 30 requested pre-live cases are represented below."""
        case_map = {
            1: "dual graph successful finalization",
            2: "missing Windows graph rejected",
            3: "missing native graph rejected",
            4: "missing Windows config key rejected",
            5: "missing native config key rejected",
            6: "Windows graph config mismatch rejected",
            7: "native graph config mismatch rejected",
            8: "missing or changed session plan rejected",
            9: "wrong session plan rejected",
            10: "raw and canonical plan substitutions rejected",
            11: "wrong graph order rejected",
            12: "wrong graph ID rejected",
            13: "duplicate graph IDs rejected",
            14: "legacy single graph cannot satisfy dual identity",
            15: "PE attributed to native rejected",
            16: "Mach-O attributed to Windows rejected",
            17: "graph source mismatch rejected",
            18: "output lineage mismatch rejected",
            19: "valid R65 G0C binding accepted",
            20: "missing R65 reservation binding rejected",
            21: "stale R65 binding rejected",
            22: "extra G0C binding row rejected",
            23: "missing required G0C binding row rejected",
            24: "modified bound file after G0C rejected",
            25: "reservation reread rejects tampered G0C",
            26: "reused BuildLease identity rejected",
            27: "sequential and concurrent admission ID reuse rejected",
            28: "valid recovery build ID accepted",
            29: "contaminated R64 build ID rejected",
            30: "historical single-Meson behavior accepted",
        }
        self.assertEqual(set(case_map), set(range(1, 31)))
        self.test_dual_graph_native_test_uses_staged_runtime_and_terminal_binds_both_graphs()
        self._assert_r65_recovery_plan_matrix()
        self._assert_r65_reservation_binding_freshness()
        self._assert_r65_admission_claim_is_atomic()
        self.assertEqual(self._r65_case_assertions, set(case_map),
                         f"case map does not match assertion-backed coverage: {self._r65_case_assertions}")

    def test_r51_pinned_lsof_status_policy_is_conjunctive(self) -> None:
        def classify(**changes):
            values = {
                "returncode": 0,
                "exact_binary": True,
                "root_pin_valid": True,
                "namespace_stable": True,
                "stdout_complete": True,
                "stderr_empty": True,
                "stream_complete": True,
                "parser_valid": True,
                "rows_bound": True,
                "identities_stable": True,
                "timed_out": False,
                "capped": False,
                "inventory_items": 1,
                "represented_items": 1,
                "probe_valid": True,
            }
            values.update(changes)
            return retention._classify_r51_lsof_status(**values)

        self.assertEqual(classify(), "STATUS_ZERO_ALL_SEARCH_ITEMS_MATCHED")
        self.assertEqual(
            classify(returncode=1, inventory_items=2, represented_items=1),
            "STATUS_ONE_EXPECTED_SEARCH_MISS")
        self.assertEqual(
            classify(returncode=0, inventory_items=2, represented_items=2),
            "STATUS_ZERO_ALL_SEARCH_ITEMS_MATCHED")
        self.assertEqual(
            classify(returncode=1, stderr_empty=False,
                     inventory_items=2, represented_items=1),
            "STATUS_ONE_UNEXPLAINED_ERROR")
        self.assertEqual(
            classify(returncode=1, stream_complete=False,
                     inventory_items=2, represented_items=1),
            "STATUS_ONE_UNEXPLAINED_ERROR")
        self.assertEqual(
            classify(returncode=1, namespace_stable=False,
                     inventory_items=2, represented_items=1),
            "STATUS_ONE_UNEXPLAINED_ERROR")
        self.assertEqual(
            classify(returncode=1, rows_bound=False,
                     inventory_items=2, represented_items=1),
            "STATUS_ONE_UNEXPLAINED_ERROR")
        self.assertEqual(
            classify(returncode=1, inventory_items=2, represented_items=2),
            "STATUS_ONE_UNEXPLAINED_ERROR")
        self.assertEqual(
            classify(returncode=1, timed_out=True,
                     inventory_items=2, represented_items=1),
            "STATUS_ONE_UNEXPLAINED_ERROR")
        self.assertEqual(
            classify(returncode=1, capped=True,
                     inventory_items=2, represented_items=1),
            "STATUS_ONE_UNEXPLAINED_ERROR")
        self.assertEqual(
            classify(returncode=1, exact_binary=False,
                     inventory_items=2, represented_items=1),
            "STATUS_ONE_UNEXPLAINED_ERROR")
        self.assertEqual(classify(returncode=2), "OTHER_NONZERO_ERROR")

    def test_r51_pinned_lsof_gate_uses_accepted_parser_and_exact_self_pin(self) -> None:
        scanner = retention._load_r51_lsof_scanner()
        with tempfile.TemporaryDirectory(
                prefix="fgmetal-r51-lsof-gate-", dir="/private/tmp") as name:
            parent = Path(name) / "builds"
            parent.mkdir(mode=0o700)
            target = parent / "synthetic-root"
            target.mkdir(mode=0o700)
            (target / "closed-child.bin").write_bytes(b"closed")
            with (mock.patch.object(retention, "BUILD_ROOT", parent),
                  mock.patch.object(retention, "_load_r51_lsof_scanner", return_value=scanner)):
                parent_fd = retention._open_dir(parent)
                root_fd = retention._open_dir(target)
                try:
                    parent_identity = retention._root_identity(parent, create=False)
                    root_info = os.fstat(root_fd)
                    root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
                    expected_root = {
                        "build_id": target.name,
                        "build_device": root_info.st_dev,
                        "build_inode": root_info.st_ino,
                        "build_filesystem_id": root_fsid,
                    }
                    root_row = (
                        b"f" + str(root_fd).encode("ascii") + b"\0tDIR\0D0x"
                        + f"{root_info.st_dev:x}".encode("ascii") + b"\0i"
                        + str(root_info.st_ino).encode("ascii") + b"\0n"
                        + os.fsencode(str(target)) + b"\0\n"
                    )
                    raw = (
                        b"p" + str(os.getpid()).encode("ascii") + b"\0R"
                        + str(os.getppid()).encode("ascii") + b"\0cpython\0\n"
                        + root_row
                    )
                    child_pid = os.getpid() + 100_000
                    capture = {
                        "argv": [retention.R51_LSOF_PATH, retention.R51_LSOF_FIELDS,
                                 "+D", str(target)],
                        "child_pid": child_pid,
                        "child_identity": {"pid": child_pid, "ppid": os.getpid()},
                        "returncode": 1,
                        "stdout_raw": raw,
                        "stderr_raw": b"",
                        "stdout_total_bytes": len(raw),
                        "stdout_bytes_captured": len(raw),
                        "stdout_sha256": hashlib.sha256(raw).hexdigest(),
                        "stdout_captured_sha256": hashlib.sha256(raw).hexdigest(),
                        "stderr_total_bytes": 0,
                        "stderr_bytes_captured": 0,
                        "stderr_sha256": hashlib.sha256(b"").hexdigest(),
                        "stderr_captured_sha256": hashlib.sha256(b"").hexdigest(),
                        "stdout_capped": False,
                        "stderr_capped": False,
                        "timed_out": False,
                    }
                    process_identity = {
                        "pid": os.getpid(),
                        "ppid": os.getppid(),
                        "start_identity": "synthetic-stable-process-incarnation",
                        "command": "python-test",
                        "cwd": os.getcwd(),
                    }
                    executable_identity = {
                        "path": retention.R51_LSOF_PATH,
                        "sha256": retention.R51_LSOF_BINARY_SHA256,
                        "stable": True,
                        "device": 1,
                        "inode": 2,
                        "size": 3,
                    }
                    with (mock.patch.object(scanner, "executable_identity",
                                            return_value=executable_identity),
                          mock.patch.object(scanner, "process_snapshot",
                                            return_value=process_identity),
                          mock.patch.object(scanner, "run_lsof_bounded",
                                            return_value=capture) as run_scan):
                        result = retention._require_r51_pinned_lsof_self_only(
                            target, parent_fd, root_fd, parent_identity, expected_root)
                    run_scan.assert_called_once_with([
                        retention.R51_LSOF_PATH, retention.R51_LSOF_FIELDS, "+D", str(target)])
                    self.assertEqual(result["classification"],
                                     "STATUS_ONE_EXPECTED_SEARCH_MISS")
                    self.assertEqual(result["namespace_item_count"], 2)
                    self.assertEqual(result["row_count"], 1)
                finally:
                    os.close(root_fd)
                    os.close(parent_fd)

    def test_r51_authenticates_current_apple_lsof_without_replacing_historical_pin(self) -> None:
        scanner = retention._load_r51_lsof_scanner()
        identity = scanner.executable_identity(retention.R51_LSOF_PATH)
        self.assertNotEqual(identity["sha256"], retention.R51_LSOF_BINARY_SHA256)
        self.assertTrue(retention._r51_current_system_lsof_is_authenticated(identity, scanner))
        self.assertEqual(
            retention.R51_LSOF_BINARY_SHA256,
            "deff85c7b4750a948a69fcd48eae3b394a37575e5048e5eb4a35b7a0482c3dc8")

        wrong_path = dict(identity)
        wrong_path["path"] = "/private/tmp/lsof"
        self.assertFalse(retention._r51_current_system_lsof_is_authenticated(wrong_path, scanner))
        failed_signature = mock.Mock(returncode=1, stdout=b"", stderr=b"signature rejected")
        with mock.patch.object(retention.subprocess, "run", return_value=failed_signature):
            self.assertFalse(
                retention._r51_current_system_lsof_is_authenticated(identity, scanner))

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

        with (mock.patch.object(retention.os, "kill", side_effect=ProcessLookupError),
              mock.patch.object(retention, "_require_no_live_process",
                                side_effect=AssertionError("pre-pin CHECK A must not authorize closeout"))
              as check_a,
              mock.patch.object(
                  retention, "_require_r51_pinned_lsof_self_only",
                  wraps=retention._require_r51_pinned_lsof_self_only) as check_b):
            receipt = self._close_orphan_fixture(args)
        check_a.assert_not_called()
        check_b.assert_called_once()

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

    def _r70_reservation_capability_fixture(self, build_suffix: str, *, milestone: str = "R70"):
        """Create a synthetic ALLOW/claim and same-process R70/R71 handoff for reserve tests."""
        milestone = milestone.upper()
        admission_id = f"r6a-{milestone.lower()}-admission-{build_suffix}"
        build_id = f"r6a-{milestone.lower()}-dual-lineage-{build_suffix}"
        lease = self._session_lease(build_id, owner_experiment=admission_id)
        manifest_path = self.manifests / f"{admission_id}.json"
        manifest = json.loads(manifest_path.read_bytes())
        claim_path = self.cache / "r66-r69-admission-claims" / f"{admission_id}.claim"
        claim_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        claim_raw = json.dumps({"milestone": milestone, "admission_id": admission_id, "build_id": build_id,
                                "decision": "ALLOW", "plan_canonical_sha256": "a" * 64},
                               sort_keys=True, separators=(",", ":")).encode() + b"\n"
        claim_path.write_bytes(claim_raw)
        manifest[f"{milestone.lower()}_build_id"] = build_id
        manifest["admission_claim"] = {"path": str(claim_path),
                                        "sha256": hashlib.sha256(claim_raw).hexdigest(),
                                        "decision": "ALLOW", "plan_canonical_sha256": "a" * 64,
                                        "build_id": build_id}
        manifest_raw = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode() + b"\n"
        manifest_path.write_bytes(manifest_raw)
        lock_file = storage_policy._acquire_storage_allocation_lock()
        state = {"schema_version": 1, "sha256": "f" * 64, "entries": []}
        capability = storage_policy.AdmissionReservationCapability(
            storage_policy._R70_ADMISSION_CAPABILITY_TOKEN,
            lock_file=lock_file,
            gate_sha256=hashlib.sha256(self.gate_attestation.read_bytes()).hexdigest(),
            admission_id=admission_id, build_id=build_id,
            plan_sha256=lease.session_plan_sha256 or "",
            manifest_path=manifest_path,
            manifest_sha256=hashlib.sha256(manifest_raw).hexdigest(),
            claim_path=claim_path, claim_sha256=hashlib.sha256(claim_raw).hexdigest(),
            post_state=state,
            r71_validation=({"candidate_path": "candidate.json", "plan_path": "plan.json",
                             "g0c_review_path": "g0c.json", "manifest_path": "manifest.json",
                             "dependency_manifest_sha256": "9" * 64}
                            if milestone == "R71" else None))
        return lease, capability, state, claim_path, manifest_path

    def _r71_real_validator_fixture(self, build_suffix: str, *, dual_session: bool = True):
        """Create a schema-valid synthetic R71 closure and signed admission inputs.

        The fixture deliberately uses current source bytes only inside a temporary
        repository. Gate policy observation is isolated by the caller; manifest,
        candidate/G0C, plan, ALLOW, claim, capability and BuildLease validation
        remain the production implementations.
        """
        admission_id = f"r6a-r71-admission-{build_suffix}"
        build_id = f"r6a-r71-dual-lineage-{build_suffix}"
        lease = self._session_lease(build_id, dual_graph=dual_session, owner_experiment=admission_id)
        (self.manifests / f"{admission_id}.json").unlink(missing_ok=True)
        admission_identity = {"admission_id": admission_id, "sha256": hashlib.sha256(
            retention._canonical_json({"admission_id": admission_id})).hexdigest()}
        lease.session_plan["admission_identity"] = admission_identity
        lease.session_plan_sha256 = hashlib.sha256(
            retention._canonical_json(lease.session_plan)).hexdigest()
        plan_rel = "experiments/storage/r71-execution-plan.json"
        candidate_rel = "experiments/storage/R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-1.json"
        g0c_rel = "experiments/storage/R6-A-R71-G0C-REVIEW.json"
        storage_dir = self.repo / "experiments/storage"
        storage_dir.mkdir(parents=True, exist_ok=True)
        gate_controls: dict[str, str] = {}
        for control in r6a_dependency_manifest.GATE_CONTROL_FILES:
            path = storage_dir / control
            path.write_text(f"synthetic current Gate control: {control}\n", encoding="utf-8")
            gate_controls[control] = hashlib.sha256(path.read_bytes()).hexdigest()
        gate_path = storage_dir / "storage-gate0-attestation.json"
        runner_rel = "experiments/storage/run_r6a_execution.py"
        test_rel = "experiments/storage/test_build_retention.py"
        runner_file = storage_dir / "run_r6a_execution.py"
        test_file = storage_dir / "test_build_retention.py"
        runner_file.write_text("synthetic runner bytes\n", encoding="utf-8")
        test_file.write_text("synthetic regression bytes\n", encoding="utf-8")
        gate_controls["test_build_retention.py"] = hashlib.sha256(
            test_file.read_bytes()).hexdigest()
        gate_path.write_bytes(json.dumps(
            {"retention_control_sha256": gate_controls}, sort_keys=True,
            separators=(",", ":")).encode() + b"\n")
        candidate = {
            "candidate_id": "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-1",
            "r71_binding": {"execution_dependency_manifest": {
                "path": r6a_dependency_manifest.MANIFEST_NAME, "sha256": "0" * 64}},
            "changed_source_hashes": {runner_rel: hashlib.sha256(
                runner_file.read_bytes()).hexdigest()},
            "changed_test_hashes": {test_rel: hashlib.sha256(test_file.read_bytes()).hexdigest()},
            "exact_diff": {"path": "experiments/storage/r71.diff",
                           "sha256": hashlib.sha256(b"synthetic exact R71 diff\n").hexdigest()},
            "transaction_contracts": {},
        }
        (self.repo / candidate_rel).write_bytes(r6a_dependency_manifest._canonical_bytes(candidate))
        (self.repo / "experiments/storage/r71.diff").write_bytes(b"synthetic exact R71 diff\n")
        # The R6-A dependency plan is a distinct, frozen two-graph object. A
        # Host-v3 test session may use a simpler single-graph session plan; the
        # validator binds both identities independently through ALLOW.
        if dual_session:
            plan = dict(lease.session_plan)
        else:
            dependency_plan_lease = self._session_lease(
                f"{build_id}-dependency-plan", dual_graph=True,
                owner_experiment=f"{admission_id}-dependency-plan")
            plan = dict(dependency_plan_lease.session_plan)
        plan_path = self.repo / plan_rel
        plan_raw = retention._canonical_json(plan)
        plan_path.write_bytes(plan_raw)
        plan_raw_sha = hashlib.sha256(plan_raw).hexdigest()
        plan_canonical_sha = plan_raw_sha
        candidate_core = r6a_dependency_manifest._candidate_core_sha256(candidate)
        g0c_review = {
            "schema_version": 1, "status": "PASS", "findings": [],
            "r71_binding": {
                "candidate": {"path": candidate_rel,
                              "candidate_id": candidate["candidate_id"],
                              "candidate_core_sha256": candidate_core},
                "execution_dependency_manifest": {
                    "path": r6a_dependency_manifest.MANIFEST_NAME, "sha256": "0" * 64},
            },
        }
        g0c_core = r6a_dependency_manifest._g0c_core_sha256(g0c_review)
        (self.repo / g0c_rel).write_bytes(r6a_dependency_manifest._canonical_bytes(g0c_review))
        dependency = r6a_dependency_manifest.build_manifest(
            candidate=candidate, plan=plan, plan_raw_sha256=plan_raw_sha,
            plan_canonical_sha256=plan_canonical_sha, g0c_sha256=g0c_core,
            candidate_path=candidate_rel, plan_path=plan_rel, repo=self.repo)
        dependency_raw = r6a_dependency_manifest._canonical_bytes(dependency)
        dependency_sha = hashlib.sha256(dependency_raw).hexdigest()
        for payload in (candidate, g0c_review):
            payload["r71_binding"]["execution_dependency_manifest"]["sha256"] = dependency_sha
        candidate_path = self.repo / candidate_rel
        candidate_path.write_bytes(r6a_dependency_manifest._canonical_bytes(candidate))
        (self.repo / g0c_rel).write_bytes(r6a_dependency_manifest._canonical_bytes(g0c_review))
        rebuilt = r6a_dependency_manifest.build_manifest(
            candidate=candidate, plan=plan, plan_raw_sha256=plan_raw_sha,
            plan_canonical_sha256=plan_canonical_sha, g0c_sha256=g0c_core,
            candidate_path=candidate_rel, plan_path=plan_rel, repo=self.repo)
        if r6a_dependency_manifest._canonical_bytes(rebuilt) != dependency_raw:
            raise AssertionError("R71 synthetic detached-envelope projection is recursive")
        dependency_path = self.repo / r6a_dependency_manifest.MANIFEST_NAME
        dependency_path.write_bytes(dependency_raw)

        additional = {
            "schema_version": 3,
            "build_session_plan_sha256": lease.session_plan_sha256,
            "execution_admission_identity": admission_identity,
            "r71_build_id": build_id,
            "r71_execution_dependency_manifest_sha256": dependency_sha,
            "r71_candidate_core_sha256": dependency["candidate_core_sha256"],
            "r71_dependency_plan_sha256": plan_raw_sha,
            "r71_g0c_core_sha256": g0c_core,
            "r71_dependency_candidate_path": candidate_rel,
            "r71_dependency_plan_path": plan_rel,
            "r71_dependency_g0c_review_path": g0c_rel,
            "r71_dependency_manifest_path": r6a_dependency_manifest.MANIFEST_NAME,
        }
        gate_path_patch = mock.patch.object(storage_policy, "GATE0_ATTESTATION_PATH", gate_path)
        gate_path_patch.start()
        self.addCleanup(gate_path_patch.stop)
        allocation_lock = storage_policy._acquire_storage_allocation_lock()
        allowed = storage_policy.begin_experiment(
            experiment_id=admission_id,
            purpose="synthetic R71 real-validator boundary regression",
            candidate_hashes={
                "r71_candidate_core_sha256": dependency["candidate_core_sha256"],
                "r71_dependency_plan_sha256": plan_raw_sha,
                "r71_g0c_core_sha256": g0c_core,
            },
            expected_duration_seconds=300, expected_prefix_bytes=0,
            expected_build_bytes=lease.expected_bytes, estimated_persistent_bytes=0,
            authorized_runner_paths=[self.runner_file],
            build_source_tree_hash=lease.source_tree_hash,
            build_configuration_key_sha256=lease.configuration_key_sha256,
            build_kind=lease.kind, build_type=lease.metadata["build_type"],
            dependency_state=lease.metadata["dependency_state"],
            output_dir=storage_dir / f"{build_suffix}-output",
            _allocation_lock=allocation_lock, additional=additional)
        if allowed.get("decision") != "ALLOW" or allowed.reservation_capability is None:
            raise AssertionError("synthetic admission fixture did not produce an ALLOW capability")
        return lease, allowed.reservation_capability, candidate_path, plan_path, dependency_path, runner_file

    def test_r70_crash_c6_before_and_after_durable_reservation_never_free_ids(self) -> None:
        for suffix, persist_before_failure in (("c6-before", False), ("c6-after", True)):
            with self.subTest(suffix=suffix):
                lease, capability, state, claim_path, manifest_path = \
                    self._r70_reservation_capability_fixture(suffix)
                store_real = retention._store_lease

                def crash_store(record):
                    if persist_before_failure:
                        store_real(record)
                    raise RuntimeError("injected C6 reservation interruption")

                try:
                    with (mock.patch.object(storage_policy, "capture_storage_tree_state",
                                            return_value=state),
                          mock.patch.object(retention, "_store_lease", side_effect=crash_store)):
                        with self.assertRaisesRegex(RuntimeError, "C6 reservation interruption"):
                            lease.reserve(admission_transaction=capability)
                    self.assertTrue(claim_path.is_file())
                    self.assertTrue(manifest_path.is_file())
                    self.assertTrue(capability.used)
                    lease_path = retention.LEASE_ROOT / f"{lease.build_id}.json"
                    self.assertEqual(lease_path.exists(), persist_before_failure)
                    if persist_before_failure:
                        record = retention._load_lease(lease.build_id)
                        self.assertEqual(record["session_v3"]["state"], "RESERVED")
                        self.assertFalse(record["session_started"])
                        self.assertFalse(lease.path.exists())
                    else:
                        with self.assertRaisesRegex(RuntimeError, "build ID has already been consumed"):
                            storage_policy._assert_r70_build_id_unclaimed(
                                lease.build_id,
                                claims_root=self.cache / "r66-r69-admission-claims")
                        self.assertFalse(lease.path.exists())
                    self.assertFalse((lease.path / retention.BUILD_SESSION_DIRECTORY).exists())
                    self.assertEqual(retention.reconcile_pending(), [])
                    self.assertTrue(claim_path.is_file(), "reconciliation must preserve consumed admission identity")
                finally:
                    lease._stop_heartbeat()
                    lease._release_lock()

    def test_r70_crash_c7_after_reservation_before_start_remains_reserved(self) -> None:
        lease, capability, state, claim_path, manifest_path = \
            self._r70_reservation_capability_fixture("c7")
        try:
            with mock.patch.object(storage_policy, "capture_storage_tree_state", return_value=state):
                lease.reserve(admission_transaction=capability)
            record = retention._load_lease(lease.build_id)
            self.assertEqual(record["session_v3"]["state"], "RESERVED")
            self.assertFalse(record["session_started"])
            self.assertTrue(capability.used)
            self.assertTrue(claim_path.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertFalse((lease.path / retention.BUILD_SESSION_DIRECTORY).exists())
            lease._stop_heartbeat()
            lease._release_lock()
            self.assertEqual(retention.reconcile_pending(), [])
            self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "RESERVED")
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    def test_r71_reservation_consumes_r71_build_id_field(self) -> None:
        snapshots: list[dict[str, Any]] = []

        def gate_blockers(**kwargs: Any) -> list[str]:
            lock_fds = kwargs.get("retention_lock_fds")
            if lock_fds is None:
                return []
            snapshots.append(retention._status_snapshot_with_lock(lock_fds))
            return []

        with (mock.patch.object(storage_policy, "_gate0_blockers",
                                side_effect=gate_blockers),
              mock.patch.object(storage_policy, "disk_free_bytes",
                                return_value=80 * storage_policy.GIB),
              mock.patch.object(storage_policy, "measure_project_usage",
                                return_value={"allocated_inode_deduplicated_bytes": 0}),
              mock.patch.object(storage_policy, "projected_storage_admission",
                                wraps=storage_policy.projected_storage_admission),
              mock.patch.object(retention, "_require_buildlease_vm_runtime",
                                return_value=None)):
            lease, capability, _candidate_path, _plan_path, _dependency_path, _runner = \
                self._r71_real_validator_fixture("held-lock-snapshot")
            self.addCleanup(lease._stop_heartbeat)
            self.addCleanup(lease._release_lock)
            with mock.patch.object(retention, "_lock", wraps=retention._lock) as lock_call:
                lease.reserve(admission_transaction=capability)
                lease.start_session()
                self.assertEqual(len(snapshots), 1,
                                 "durable reservation must use the held-lock Gate snapshot")
                self.assertEqual(lock_call.call_count, 1,
                                 "R71 revalidation must reuse the held lock, not recursively reacquire it")
                self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"],
                                 "ACTIVE")
                lease._revalidate_r71_admission()
                self.assertEqual(len(snapshots), 2,
                                 "the first post-mount R71 handoff must use the same held-lock Gate snapshot")

    def test_r71_reserved_admission_and_build_capability_cannot_be_replayed(self) -> None:
        lease, capability, state, _claim_path, _manifest_path = \
            self._r70_reservation_capability_fixture("c7-r71-replay", milestone="R71")
        try:
            with (mock.patch.object(storage_policy, "capture_storage_tree_state", return_value=state),
                  mock.patch.object(storage_policy.AdmissionReservationCapability, "_validate_r71")):
                lease.reserve(admission_transaction=capability)
                self.assertTrue(capability.used)
                with self.assertRaisesRegex(retention.RetentionError,
                                            "R70 admission/reservation handoff is invalid"):
                    lease.reserve(admission_transaction=capability)
            record = retention._load_lease(lease.build_id)
            self.assertEqual(record["session_v3"]["state"], "RESERVED")
            self.assertFalse(record["session_started"])
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    def test_r71_crash_before_or_after_reservation_record_keeps_ids_consumed(self) -> None:
        for suffix, persist_before_failure in (("reserve-before", False), ("reserve-after", True)):
            with self.subTest(persist_before_failure=persist_before_failure):
                lease, capability, _state, claim_path, manifest_path = \
                    self._r70_reservation_capability_fixture(
                        f"c6-r71-{suffix}", milestone="R71")
                store_real = retention._store_lease

                def interrupted_store(record):
                    if persist_before_failure:
                        store_real(record)
                    raise RuntimeError("injected R71 reservation persistence crash")

                try:
                    with (mock.patch.object(storage_policy.AdmissionReservationCapability,
                                            "_validate_r71"),
                          mock.patch.object(retention, "_store_lease",
                                            side_effect=interrupted_store)):
                        with self.assertRaisesRegex(RuntimeError,
                                                    "injected R71 reservation persistence crash"):
                            lease.reserve(admission_transaction=capability)
                    self.assertTrue(capability.used)
                    self.assertTrue(claim_path.is_file())
                    self.assertTrue(manifest_path.is_file())
                    record_path = retention.LEASE_ROOT / f"{lease.build_id}.json"
                    self.assertEqual(record_path.exists(), persist_before_failure)
                    if persist_before_failure:
                        record = retention._load_lease(lease.build_id)
                        self.assertEqual(record["session_v3"]["state"], "RESERVED")
                        self.assertFalse(record["session_started"])
                        self.assertFalse(lease.path.exists())
                    else:
                        with self.assertRaisesRegex(RuntimeError, "already been consumed"):
                            storage_policy._assert_r70_build_id_unclaimed(
                                lease.build_id,
                                claims_root=self.cache / "r66-r69-admission-claims")
                finally:
                    lease._stop_heartbeat()
                    lease._release_lock()

    def test_r71_crash_before_start_marker_leaves_durable_reservation_unstarted(self) -> None:
        lease, capability, _state, claim_path, manifest_path = \
            self._r70_reservation_capability_fixture("c7-r71-start-crash", milestone="R71")
        try:
            with mock.patch.object(storage_policy.AdmissionReservationCapability, "_validate_r71"):
                lease.reserve(admission_transaction=capability)
                with mock.patch.object(retention.BuildLeaseSession, "start",
                                       side_effect=RuntimeError("injected R71 session-start crash")):
                    with self.assertRaisesRegex(RuntimeError, "injected R71 session-start crash"):
                        lease.start_session()
            record = retention._load_lease(lease.build_id)
            self.assertEqual(record["session_v3"]["state"], "RESERVED")
            self.assertFalse(record["session_started"])
            self.assertTrue(capability.used)
            self.assertTrue(claim_path.is_file())
            self.assertTrue(manifest_path.is_file())
            self.assertFalse((lease.path / retention.BUILD_SESSION_DIRECTORY).exists())
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    def test_r71_allow_reserve_same_object_start_allows_safe_unrelated_growth(self) -> None:
        """Exercise the transaction sequence with synthetic evidence and no build commands."""
        admission_id = "r6a-r71-admission-safe-growth"
        build_id = "r6a-r71-dual-lineage-safe-growth"
        lease = self._session_lease(build_id, dual_graph=True, owner_experiment=admission_id)
        (self.manifests / f"{admission_id}.json").unlink()
        identity_sha = hashlib.sha256(retention._canonical_json(
            {"admission_id": admission_id})).hexdigest()
        admission_identity = {"admission_id": admission_id, "sha256": identity_sha}
        lease.session_plan["admission_identity"] = admission_identity
        lease.session_plan_sha256 = hashlib.sha256(
            retention._canonical_json(lease.session_plan)).hexdigest()
        (self.repo / "unrelated-4k-report.log").write_bytes(b"r" * 4096)
        additional = {
            "schema_version": 3,
            "build_session_plan_sha256": lease.session_plan_sha256,
            "execution_admission_identity": admission_identity,
            "r71_build_id": build_id,
            "r71_execution_dependency_manifest_sha256": "a" * 64,
            "r71_candidate_core_sha256": "b" * 64,
            "r71_dependency_plan_sha256": "c" * 64,
            "r71_g0c_core_sha256": "d" * 64,
            "r71_dependency_candidate_path": "experiments/storage/candidate.json",
            "r71_dependency_plan_path": "experiments/storage/plan.json",
            "r71_dependency_g0c_review_path": "experiments/storage/g0c.json",
            "r71_dependency_manifest_path": "experiments/storage/"
                                               "R6-A-R71-EXECUTION-DEPENDENCY-MANIFEST.json",
        }
        projected_storage_admission = storage_policy.projected_storage_admission
        allocation_lock = storage_policy._acquire_storage_allocation_lock()
        try:
            with (mock.patch.object(storage_policy, "_gate0_blockers", return_value=[]),
                  mock.patch.object(storage_policy, "measure_project_usage", return_value={
                      "allocated_inode_deduplicated_bytes": 10 * storage_policy.GIB}),
                  mock.patch.object(storage_policy, "disk_free_bytes",
                                    return_value=100 * storage_policy.GIB),
                  mock.patch.object(storage_policy, "projected_storage_admission", side_effect=lambda **kw: {
                      "admitted": True, "blockers": [], "warnings": [],
                      "projected_scoped_bytes": kw["current_scoped_bytes"] + kw["peak_additional_bytes"],
                      "projected_free_bytes": kw["current_free_bytes"] - kw["peak_additional_bytes"],
                  }),
                  mock.patch.object(storage_policy.AdmissionReservationCapability, "_validate_r71"),
                  mock.patch.object(retention, "_require_buildlease_vm_runtime", return_value=None)):
                allowed = storage_policy.begin_experiment(
                    experiment_id=admission_id,
                    purpose="synthetic R71 exact-manifest transaction regression",
                    candidate_hashes={"synthetic_candidate_sha256": "e" * 64},
                    expected_duration_seconds=300,
                    expected_prefix_bytes=0,
                    expected_build_bytes=lease.expected_bytes,
                    estimated_persistent_bytes=0,
                    authorized_runner_paths=[self.runner_file],
                    build_source_tree_hash=lease.source_tree_hash,
                    build_configuration_key_sha256=lease.configuration_key_sha256,
                    build_kind=lease.kind,
                    build_type=lease.metadata["build_type"],
                    dependency_state=lease.metadata["dependency_state"],
                    output_dir=self.repo / "experiments/storage/r71-safe-growth-output",
                    _allocation_lock=allocation_lock,
                    additional=additional,
                )
                self.assertEqual(allowed["decision"], "ALLOW")
                capability = allowed.reservation_capability
                self.assertIsNotNone(capability)
                self.assertEqual(lease.reserve(admission_transaction=capability), lease.path)
                # New unrelated bytes after the Gate/admission check remain a capacity
                # concern only. They are outside the synthetic dependency fixture.
                (self.repo / "another-unrelated-report.log").write_bytes(b"z" * 4096)
                session = lease.start_session()
                self.assertIs(session, lease.session)
                record = retention._load_lease(build_id)
                self.assertTrue(record["session_started"])
                self.assertEqual(record["session_v3"]["state"], "ACTIVE")
                first_command = session.session_dir / "steps/step-000.command.json"
                with (mock.patch.object(storage_policy.AdmissionReservationCapability,
                                        "_validate_r71"),
                      mock.patch.object(storage_policy, "disk_free_bytes",
                                        return_value=24 * storage_policy.GIB),
                      mock.patch.object(storage_policy, "measure_project_usage", return_value={
                          "allocated_inode_deduplicated_bytes": 10 * storage_policy.GIB}),
                      mock.patch.object(storage_policy, "projected_storage_admission",
                                        side_effect=projected_storage_admission)):
                    with self.assertRaisesRegex(retention.RetentionError,
                                                "current free space is below the 25 GiB emergency hard stop"):
                        session.run_next()
                self.assertFalse(first_command.exists(),
                                 "dispatch capacity stop must happen before command evidence or launch")
        finally:
            lease._stop_heartbeat()
            lease._release_lock()
            if allocation_lock and not allocation_lock.closed:
                storage_policy._release_storage_allocation_lock(allocation_lock)

    def _r71_validation_boundary_patches(self):
        """Isolate Gate/capacity inputs while exercising the R71 verifier."""
        return (
            mock.patch.object(storage_policy, "_gate0_blockers", return_value=[]),
            mock.patch.object(storage_policy, "disk_free_bytes", return_value=100 * storage_policy.GIB),
            mock.patch.object(storage_policy, "measure_project_usage", return_value={
                "allocated_inode_deduplicated_bytes": 0}),
            mock.patch.object(storage_policy, "projected_storage_admission",
                              wraps=storage_policy.projected_storage_admission),
            mock.patch.object(retention, "_require_buildlease_vm_runtime", return_value=None),
        )

    def test_r71_actual_gate0_blockers_fail_closed_on_incomplete_isolated_evidence(self) -> None:
        """Run the production Gate observer without replacing its decision."""
        blockers = storage_policy._gate0_blockers(
            active_experiment_id="r6a-r71-admission-gate-observer",
            allow_r71_handoff=True,
            r71_build_id="r6a-r71-dual-lineage-gate-observer",
        )
        self.assertTrue(blockers)
        self.assertTrue(any("Gate 0 evidence is unreadable" in blocker
                            and "missing-inventory.json" in blocker
                            for blocker in blockers), blockers)

    def test_r71_owned_reservation_requires_admission_capability(self) -> None:
        """An R71 owner cannot fall through to the ordinary BuildLease path."""
        build_id = "r6a-r71-missing-capability"
        admission_id = "r6a-r71-admission-missing-capability"
        lease = self._session_lease(build_id, owner_experiment=admission_id)
        self.addCleanup(lease._stop_heartbeat)
        self.addCleanup(lease._release_lock)
        lease_record_path = retention.LEASE_ROOT / f"{build_id}.json"

        with (mock.patch.object(storage_policy, "_acquire_storage_allocation_lock",
                                side_effect=AssertionError("ordinary allocation lock path reached")) as acquire,
              mock.patch.object(storage_policy, "disk_free_bytes",
                                side_effect=AssertionError("capacity path reached")) as disk_free,
              mock.patch.object(storage_policy, "measure_project_usage",
                                side_effect=AssertionError("capacity path reached")) as measure):
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "R71 BuildLease reservation requires its admission capability"):
                lease.reserve()

        acquire.assert_not_called()
        disk_free.assert_not_called()
        measure.assert_not_called()
        self.assertIsNone(lease.admission_reservation_capability)
        self.assertFalse(lease.started)
        self.assertFalse(lease.path.exists())
        self.assertFalse(lease_record_path.exists())
        with self.assertRaisesRegex(
                retention.RetentionError,
                "R71 BuildLease lost its admission capability before session consumption"):
            lease._revalidate_r71_admission()

    def test_r72_owned_reservation_cannot_bypass_r71_capability_path(self) -> None:
        """A fresh R72 owner still requires the R71 provenance capability."""
        build_id = "r6a-r72-dual-lineage-missing-capability"
        admission_id = "r6a-r72-admission-missing-capability"
        lease = self._session_lease(build_id, owner_experiment=admission_id)
        self.addCleanup(lease._stop_heartbeat)
        self.addCleanup(lease._release_lock)
        with mock.patch.object(storage_policy, "_acquire_storage_allocation_lock",
                               side_effect=AssertionError("ordinary allocation lock path reached")), \
             mock.patch.object(storage_policy, "disk_free_bytes",
                               side_effect=AssertionError("capacity path reached")):
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "R71 BuildLease reservation requires its admission capability"):
                lease.reserve()
        self.assertIsNone(lease.admission_reservation_capability)
        self.assertFalse(lease.started)
        with self.assertRaisesRegex(
                retention.RetentionError,
                "R71 BuildLease lost its admission capability before session consumption"):
            lease._revalidate_r71_admission()

    def test_r73_owned_reservation_cannot_bypass_r71_capability_path(self) -> None:
        """R73 adds a candidate identity while retaining the R71 provenance handoff."""
        build_id = "r6a-r73-dual-lineage-missing-capability"
        admission_id = "r6a-r73-admission-missing-capability"
        lease = self._session_lease(build_id, owner_experiment=admission_id)
        self.addCleanup(lease._stop_heartbeat)
        self.addCleanup(lease._release_lock)
        with mock.patch.object(storage_policy, "_acquire_storage_allocation_lock",
                               side_effect=AssertionError("ordinary allocation lock path reached")), \
             mock.patch.object(storage_policy, "disk_free_bytes",
                               side_effect=AssertionError("capacity path reached")):
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "R71 BuildLease reservation requires its admission capability"):
                lease.reserve()
        self.assertIsNone(lease.admission_reservation_capability)
        self.assertFalse(lease.started)
        with self.assertRaisesRegex(
                retention.RetentionError,
                "R71 BuildLease lost its admission capability before session consumption"):
            lease._revalidate_r71_admission()

    def test_r74_owned_reservation_cannot_bypass_r71_capability_path(self) -> None:
        """R74 keeps the R71 provenance handoff and reservation checks."""
        build_id = "r6a-r74-dual-lineage-missing-capability"
        admission_id = "r6a-r74-admission-missing-capability"
        lease = self._session_lease(build_id, owner_experiment=admission_id)
        self.addCleanup(lease._stop_heartbeat)
        self.addCleanup(lease._release_lock)
        with mock.patch.object(storage_policy, "_acquire_storage_allocation_lock",
                               side_effect=AssertionError("ordinary allocation lock path reached")), \
             mock.patch.object(storage_policy, "disk_free_bytes",
                               side_effect=AssertionError("capacity path reached")):
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "R71 BuildLease reservation requires its admission capability"):
                lease.reserve()
        self.assertIsNone(lease.admission_reservation_capability)
        self.assertFalse(lease.started)
        with self.assertRaisesRegex(
                retention.RetentionError,
                "R71 BuildLease lost its admission capability before session consumption"):
            lease._revalidate_r71_admission()

    def test_r75_owned_reservation_cannot_bypass_r71_capability_path(self) -> None:
        build_id = "r6a-r75-dual-lineage-missing-capability"
        admission_id = "r6a-r75-admission-missing-capability"
        lease = self._session_lease(build_id, owner_experiment=admission_id)
        self.addCleanup(lease._stop_heartbeat)
        self.addCleanup(lease._release_lock)
        with mock.patch.object(storage_policy, "_acquire_storage_allocation_lock",
                               side_effect=AssertionError("ordinary allocation lock path reached")), \
             mock.patch.object(storage_policy, "disk_free_bytes",
                               side_effect=AssertionError("capacity path reached")):
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "R71 BuildLease reservation requires its admission capability"):
                lease.reserve()
        self.assertIsNone(lease.admission_reservation_capability)
        self.assertFalse(lease.started)
        with self.assertRaisesRegex(
                retention.RetentionError,
                "R71 BuildLease lost its admission capability before session consumption"):
            lease._revalidate_r71_admission()

    def test_r76_owned_reservation_cannot_bypass_r71_capability_path(self) -> None:
        build_id = "r6a-r76-dual-lineage-missing-capability"
        admission_id = "r6a-r76-admission-missing-capability"
        lease = self._session_lease(build_id, owner_experiment=admission_id)
        self.addCleanup(lease._stop_heartbeat)
        self.addCleanup(lease._release_lock)
        with mock.patch.object(storage_policy, "_acquire_storage_allocation_lock",
                               side_effect=AssertionError("ordinary allocation lock path reached")), \
             mock.patch.object(storage_policy, "disk_free_bytes",
                               side_effect=AssertionError("capacity path reached")):
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "R71 BuildLease reservation requires its admission capability"):
                lease.reserve()
        self.assertIsNone(lease.admission_reservation_capability)
        self.assertFalse(lease.started)
        with self.assertRaisesRegex(
                retention.RetentionError,
                "R71 BuildLease lost its admission capability before session consumption"):
            lease._revalidate_r71_admission()

    def test_r76_capability_uses_exact_r71_plan_and_manifest_identity_route(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            lock_path = Path(temporary) / "allocation.lock"
            lock_path.write_bytes(b"")
            lock_file = lock_path.open("a+")
            self.addCleanup(lock_file.close)
            capability = storage_policy.AdmissionReservationCapability(
                storage_policy._R70_ADMISSION_CAPABILITY_TOKEN,
                lock_file=lock_file, gate_sha256="1" * 64,
                admission_id="r6a-r76-admission-capability-route",
                build_id="r6a-r76-dual-lineage-capability-route",
                plan_sha256="2" * 64,
                manifest_path=Path(temporary) / "allow.json", manifest_sha256="3" * 64,
                claim_path=Path(temporary) / "claim.json", claim_sha256="4" * 64,
                post_state=None,
                r71_validation={"candidate_path": "candidate.json", "plan_path": "plan.json",
                               "g0c_review_path": "g0c.json", "manifest_path": "dependency.json",
                               "dependency_manifest_sha256": "5" * 64})
            args = {"build_id": capability.build_id,
                    "owner_experiment": capability.admission_id,
                    "plan_sha256": capability.plan_sha256,
                    "manifest_path": Path(capability.manifest_path)}
            with mock.patch.object(storage_policy.AdmissionReservationCapability,
                                   "_validate_r71") as validate_r71:
                capability.validate(**args)
                validate_r71.assert_called_once_with(
                    build_id=capability.build_id, plan_sha256=capability.plan_sha256,
                    manifest_path=Path(capability.manifest_path), consumption=False,
                    retention_lock_fds=None)
                validate_r71.reset_mock()
                wrong_args = {**args, "build_id": "r6a-r76-dual-lineage-unbound-safe-id"}
                with self.assertRaisesRegex(RuntimeError, "identity mismatch or replay"):
                    capability.validate(**wrong_args)
                validate_r71.assert_not_called()

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_real_validator_reserve_start_dispatch_and_identity_drift(self) -> None:
        """Use real `_validate_r71` through reserve/start/dispatch with synthetic inputs."""
        patches = self._r71_validation_boundary_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            lease, capability, candidate_path, plan_path, dependency_path, runner_file = \
                self._r71_real_validator_fixture("real-boundaries")
            self.addCleanup(lease._stop_heartbeat)
            self.addCleanup(lease._release_lock)
            (self.repo / "unrelated-pre-reserve-report.log").write_bytes(b"r" * 4096)
            self.assertEqual(lease.reserve(admission_transaction=capability), lease.path)
            (self.repo / "unrelated-post-reserve-report.log").write_bytes(b"z" * 4096)
            session = lease.start_session()
            self.assertIs(session, lease.session)
            with self.assertRaises(retention.RetentionError):
                lease.complete()
            self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists(),
                             "an incomplete session cannot publish a completion manifest")
            first_command = session.session_dir / "steps/step-000.command.json"
            runner_file.write_text("runner changed after session start\n", encoding="utf-8")
            with self.assertRaisesRegex(retention.RetentionError,
                                        "R71 dependency closure changed before session consumption"):
                lease.complete()
            with self.assertRaisesRegex(retention.RetentionError,
                                        "dependency closure changed before session consumption"):
                session.run_next()
            self.assertFalse(first_command.exists(), "dispatch must validate closure before command receipt")
            current = retention._load_lease(lease.build_id)
            self.assertTrue(current["session_started"])
            self.assertEqual(current["session_v3"]["next_step_index"], 0)
            self.assertTrue(candidate_path.is_file())
            self.assertTrue(plan_path.is_file())
            self.assertTrue(dependency_path.is_file())

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_gate_handoff_is_enabled_only_after_durable_reservation(self) -> None:
        """Pre-store Gate sees no lease; consumption Gate binds the exact stored lease."""
        patches = self._r71_validation_boundary_patches()
        gate_calls = []
        measurement_calls = []
        scan_calls = []

        def record_root_scan(**kwargs):
            scan_calls.append(dict(kwargs))
            return []

        def record_gate_call(**kwargs):
            gate_calls.append(kwargs)
            lock_fds = kwargs.get("retention_lock_fds")
            if lock_fds is not None:
                active_id = kwargs.get("active_buildlease_id")
                active_experiment = kwargs.get("active_experiment_id") if active_id is not None else None
                with mock.patch.object(
                        retention, "_verified_buildlease_capacity_workspace",
                        return_value=(retention._load_lease(active_id)["session_v3"][
                            "workspace_mount_identity"] if active_id is not None else {})):
                    retention._status_snapshot_with_lock(
                        lock_fds, active_buildlease_id=active_id,
                        active_experiment_id=active_experiment)
            return []

        def record_project_measurement(**kwargs):
            measurement_calls.append(dict(kwargs))
            return {"allocated_inode_deduplicated_bytes": 0}

        with (mock.patch.object(storage_policy, "_gate0_blockers", side_effect=record_gate_call),
              mock.patch.object(retention, "_all_root_candidates",
                                side_effect=record_root_scan),
              patches[1], mock.patch.object(storage_policy, "measure_project_usage",
                                            side_effect=record_project_measurement),
              patches[3], patches[4]):
            lease, capability, _candidate, _plan, _manifest, _runner = \
                self._r71_real_validator_fixture("handoff-boundary-order")
            self.addCleanup(lease._stop_heartbeat)
            self.addCleanup(lease._release_lock)

            lease.reserve(admission_transaction=capability)
            reserve_call = gate_calls[-1]
            self.assertTrue(reserve_call["allow_r71_handoff"])
            self.assertIsNone(reserve_call["r71_build_id"])
            self.assertIsNone(reserve_call["active_buildlease_id"])

            gate_calls_before_start = len(gate_calls)
            session = lease.start_session()
            self.assertEqual(len(gate_calls), gate_calls_before_start,
                             "a RESERVED, unmounted workspace is not an R71 consumption boundary")
            with mock.patch.object(session, "_run_next_locked",
                                   return_value={"status": "SYNTHETIC_PRE_DISPATCH"}):
                self.assertEqual(session.run_next()["status"], "SYNTHETIC_PRE_DISPATCH")

            consumption_call = gate_calls[-1]
            self.assertTrue(consumption_call["allow_r71_handoff"])
            self.assertEqual(consumption_call["r71_build_id"], lease.build_id)
            self.assertEqual(consumption_call["active_buildlease_id"], lease.build_id)
            attached_call = gate_calls[-1]
            self.assertEqual(attached_call["r71_build_id"], lease.build_id)
            self.assertEqual(attached_call["active_buildlease_id"], lease.build_id)
            self.assertEqual(measurement_calls[-1], {
                "strict": True, "active_buildlease_id": lease.build_id,
                "r71_allow_pre_dispatch": False,
            })
            self.assertEqual(scan_calls[-1], {
                "active_buildlease_id": lease.build_id,
                "active_workspace_identity": retention._load_lease(lease.build_id)[
                    "session_v3"]["workspace_mount_identity"],
                "allow_pre_dispatch": False,
            })
            session._fail("R92 active-workspace handoff test cleanup", -1,
                          command=["session-start"])

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_status_reconciliation_preserves_only_exact_live_session(self) -> None:
        first, _first_session = self._start_session("r92-status-preserve-exact")
        self._bind_session_to_synthetic_r71(first)
        foreign_id = "r6a-r71-status-preserve-sibling"
        paths = [retention.LEASE_ROOT / f"{first.build_id}.json",
                 retention.LEASE_ROOT / f"{foreign_id}.json"]
        load_real = retention._load_lease
        foreign_record = {
            "build_id": foreign_id,
            "owner_experiment": f"r6a-r71-admission-{foreign_id}",
            "state": "ACTIVE", "session_started": True,
            "retirement_status": None, "build_manifest_id": None,
            "session_v3": {"state": "ACTIVE"},
        }
        first_identity = retention._load_lease(first.build_id)["session_v3"][
            "workspace_mount_identity"]
        verified_ids: list[str] = []
        recovery_ids: list[str] = []
        retired_ids: list[str] = []

        def verify(build_id: str) -> dict[str, object]:
            verified_ids.append(build_id)
            return first_identity

        def recover(lease: dict[str, object]) -> None:
            recovery_ids.append(str(lease.get("build_id")))

        def retire(lease: dict[str, object], *, reason: str) -> dict[str, object]:
            retired_ids.append(str(lease.get("build_id")))
            return {"status": "RETIRED", "reason": reason}

        def load(build_id: str) -> dict[str, object]:
            return (load_real(build_id) if build_id == first.build_id
                    else dict(foreign_record))

        try:
            with (mock.patch.object(retention, "_lease_files", return_value=paths),
                  mock.patch.object(retention, "_load_lease", side_effect=load),
                  mock.patch.object(retention, "_verified_buildlease_capacity_workspace",
                                    side_effect=verify),
                  mock.patch.object(retention, "_reject_interrupted_host_v3_session",
                                    side_effect=recover),
                  mock.patch.object(retention, "_retire", side_effect=retire)):
                retention._retire_pending_locked(
                    preserve_active_buildlease_id=first.build_id,
                    preserve_active_experiment_id=first.owner_experiment)

            self.assertEqual(verified_ids, [first.build_id])
            self.assertEqual(recovery_ids, [foreign_id],
                             "the unrelated started session must retain normal crash recovery")
            self.assertEqual(retired_ids, [foreign_id])
            self.assertEqual(load_real(first.build_id)["state"], "ACTIVE")
        finally:
            record = load_real(first.build_id)
            if record.get("state") == "ACTIVE":
                record["owner_experiment"] = "synthetic-test"
                retention._store_lease(record)
                root_fd = retention._open_dir(first.path)
                try:
                    marker = retention._read_marker(root_fd)
                    marker.update(record)
                    retention._write_marker(root_fd, marker)
                    os.fsync(root_fd)
                finally:
                    os.close(root_fd)
                first.owner_experiment = "synthetic-test"
                first.session._fail("R92 status reconciliation test cleanup", -1,
                                    command=["session-start"])
            first._stop_heartbeat()
            first._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_r92_pre_step_zero_r71_exception_is_durably_captured_with_cleanup(self) -> None:
        lease, session = self._start_session("r92-pre-step-zero-failure-capture")
        self._bind_session_to_synthetic_r71(lease)
        reason = "synthetic original R71 handoff failure"
        workspace_identity = retention._load_lease(lease.build_id)["session_v3"][
            "workspace_mount_identity"]
        try:
            with mock.patch.object(
                    lease, "_revalidate_r71_admission",
                    side_effect=retention.RetentionError(reason)), \
                 mock.patch.object(
                    retention, "_session_start_admission_evidence",
                    return_value={"admission_decision": "ALLOW",
                                  "admission_id": lease.owner_experiment,
                                  "admission_manifest_sha256": "a" * 64}), \
                 mock.patch.object(
                    session, "_load",
                    side_effect=AssertionError("pregraph capture must not re-enter session loading")):
                with self.assertRaisesRegex(retention.RetentionError, reason):
                    session.run_next()

            failure = retention._read_json(
                retention.MANIFEST_ROOT / f"{lease.build_id}.failure" / "failure-manifest.json")
            record = retention._load_lease(lease.build_id)
            cleanup_receipt = retention._read_json(
                retention.RECEIPT_ROOT / f"{lease.build_id}.json", sealed=True)
            original_exception = repr(retention.RetentionError(reason))
            self.assertEqual(failure["failure_reason"], original_exception)
            self.assertEqual(failure["triggering_exception_class"], "RetentionError")
            self.assertEqual(failure["triggering_exception_message"], reason)
            self.assertEqual(failure["failure_reason_sha256"], hashlib.sha256(
                original_exception.encode("utf-8")).hexdigest())
            self.assertFalse(failure["graph_execution_started"])
            self.assertEqual(failure["next_step_index"], 0)
            self.assertEqual(failure["step_receipts"], [])
            self.assertEqual(failure["graph_outputs"], {})
            self.assertIs(failure["outputs_accepted"], False)
            self.assertEqual(failure["workspace_cleanup"]["identity"], workspace_identity)
            self.assertEqual(failure["workspace_cleanup"]["status"], "DETACHED")
            self.assertEqual(failure["residual_holder_evidence"]["status"], "NO_HOLDERS")
            self.assertEqual(failure["retirement_result"]["status"],
                             "PENDING_AFTER_FAILURE_EVIDENCE")
            self.assertEqual(record["retirement_status"], "COMPLETED")
            self.assertEqual(cleanup_receipt["execution_outcome"], "FAILED")
            self.assertIs(cleanup_receipt["outputs_accepted"], False)
            self.assertFalse((session.session_dir / "steps/step-000.command.json").exists())
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_dependency_mutation_after_dispatch_entry_blocks_before_receipt_and_popen(self) -> None:
        """Revalidate prepared inputs immediately before command evidence/launch."""
        patches = self._r71_validation_boundary_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            lease, capability, _candidate, _plan, _manifest, runner_file = \
                self._r71_real_validator_fixture("dispatch-launch-boundary")
            self.addCleanup(lease._stop_heartbeat)
            self.addCleanup(lease._release_lock)
            lease.reserve(admission_transaction=capability)
            session = lease.start_session()
            first_command = session.session_dir / "steps/step-000.command.json"
            first_result = session.session_dir / "steps/step-000.result.json"
            original_command_for_step = session._command_for_step

            def mutate_after_entry_validation(*args, **kwargs):
                prepared = original_command_for_step(*args, **kwargs)
                runner_file.write_text("runner changed during dispatch preparation\\n",
                                       encoding="utf-8")
                return prepared

            original_popen = retention.subprocess.Popen
            launch_attempts = []

            def guard_sandbox_launch(*args, **kwargs):
                command = args[0] if args else kwargs.get("args")
                if isinstance(command, (list, tuple)) and command \
                        and command[0] == session.plan["tools"]["sandbox"]["path"]:
                    launch_attempts.append(command)
                    raise AssertionError("sandbox process launch must not be reached")
                return original_popen(*args, **kwargs)

            with (mock.patch.object(session, "_command_for_step",
                                    side_effect=mutate_after_entry_validation),
                  mock.patch.object(retention.subprocess, "Popen",
                                    side_effect=guard_sandbox_launch)):
                with self.assertRaisesRegex(
                        retention.RetentionError,
                        "dependency closure changed before session consumption"):
                    session.run_next()

            self.assertEqual(launch_attempts, [])
            self.assertFalse(first_command.exists(),
                             "changed dependencies must block before command receipt publication")
            self.assertFalse(first_result.exists(),
                             "changed dependencies must block before result receipt publication")
            self.assertEqual(
                retention._load_lease(lease.build_id)["session_v3"]["next_step_index"], 0)

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_dependency_mutation_after_receipt_blocks_at_final_popen_guard(self) -> None:
        """The final Popen guard catches drift after receipt/RUNNING publication."""
        patches = self._r71_validation_boundary_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            lease, capability, _candidate, _plan, _manifest, runner_file = \
                self._r71_real_validator_fixture("dispatch-final-popen-guard")
            self.addCleanup(lease._stop_heartbeat)
            self.addCleanup(lease._release_lock)
            lease.reserve(admission_transaction=capability)
            session = lease.start_session()
            command_path = session.session_dir / "steps/step-000.command.json"
            result_path = session.session_dir / "steps/step-000.result.json"
            original_check_root_bindings = session._check_root_bindings
            mutated = False
            receipt_present_when_mutated = False
            running_when_mutated = False

            def mutate_after_final_root_check(*args, **kwargs):
                nonlocal mutated, receipt_present_when_mutated, running_when_mutated
                result = original_check_root_bindings(*args, **kwargs)
                current = retention._load_lease(lease.build_id)
                if (not mutated and command_path.is_file()
                        and current.get("session_v3", {}).get("state") == "RUNNING"):
                    receipt_present_when_mutated = True
                    running_when_mutated = True
                    runner_file.write_text("runner changed after command receipt\\n",
                                           encoding="utf-8")
                    mutated = True
                return result

            original_popen = retention.subprocess.Popen
            launch_attempts = []

            def guard_sandbox_launch(*args, **kwargs):
                command = args[0] if args else kwargs.get("args")
                if isinstance(command, (list, tuple)) and command \
                        and command[0] == session.plan["tools"]["sandbox"]["path"]:
                    launch_attempts.append(command)
                    raise AssertionError("sandbox process launch must be blocked by final R71 validation")
                return original_popen(*args, **kwargs)

            with (mock.patch.object(session, "_check_root_bindings",
                                    side_effect=mutate_after_final_root_check),
                  mock.patch.object(retention.subprocess, "Popen",
                                    side_effect=guard_sandbox_launch)):
                with self.assertRaisesRegex(
                        retention.RetentionError,
                        "dependency closure changed before session consumption"):
                    session.run_next()

            self.assertTrue(mutated, "dependency mutation did not occur after receipt/RUNNING publication")
            self.assertTrue(receipt_present_when_mutated,
                            "dependency mutation must occur after command receipt publication")
            self.assertTrue(running_when_mutated,
                            "dependency mutation must occur after durable RUNNING state publication")
            self.assertFalse(result_path.exists(), "a blocked launch must not publish a result receipt")
            self.assertEqual(launch_attempts, [], "the final validator must run before sandbox Popen")
            record = retention._load_lease(lease.build_id)
            self.assertEqual(record.get("session_v3", {}).get("state"), "FAILED")
            self.assertNotEqual(record.get("state"), "SUCCESS")
            self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())

    def test_r71_real_validator_rejects_capacity_crossings_at_reserve_and_before_start(self) -> None:
        """Use actual R71 validation and projected-capacity arithmetic with isolated measurements."""
        from contextlib import ExitStack

        capacity = {"free": 100 * storage_policy.GIB, "project": 10 * storage_policy.GIB}

        def measured_usage(*, strict: bool = False):
            self.assertTrue(strict)
            return {"allocated_inode_deduplicated_bytes": capacity["project"]}

        with ExitStack() as stack:
            stack.enter_context(mock.patch.object(storage_policy, "_gate0_blockers", return_value=[]))
            stack.enter_context(mock.patch.object(
                storage_policy, "disk_free_bytes", side_effect=lambda _path: capacity["free"]))
            stack.enter_context(mock.patch.object(
                storage_policy, "measure_project_usage", side_effect=measured_usage))
            stack.enter_context(mock.patch.object(
                retention, "_require_buildlease_vm_runtime", return_value=None))

            for suffix, changed in (("reserve-free-threshold", {"free": 49 * storage_policy.GIB}),
                                    ("reserve-scope-threshold", {"project": 60 * storage_policy.GIB})):
                with self.subTest(boundary=suffix):
                    capacity.update({"free": 100 * storage_policy.GIB,
                                     "project": 10 * storage_policy.GIB})
                    lease, capability, *_ = self._r71_real_validator_fixture(suffix)
                    self.addCleanup(lease._stop_heartbeat)
                    self.addCleanup(lease._release_lock)
                    capacity.update(changed)
                    with self.assertRaisesRegex(
                            retention.RetentionError,
                            "live projected-peak check blocks the BuildLease reservation"):
                        lease.reserve(admission_transaction=capability)
                    self.assertFalse((retention.LEASE_ROOT / f"{lease.build_id}.json").exists())
                    self.assertFalse(lease.path.exists())
                    self.assertFalse(capability.used)

            capacity.update({"free": 100 * storage_policy.GIB,
                             "project": 10 * storage_policy.GIB})
            lease, capability, *_ = self._r71_real_validator_fixture("pre-start-free-threshold")
            self.addCleanup(lease._stop_heartbeat)
            self.addCleanup(lease._release_lock)
            lease.reserve(admission_transaction=capability)
            capacity["free"] = 49 * storage_policy.GIB
            with self.assertRaisesRegex(
                    retention.RetentionError,
                    "R71 projected-capacity boundary blocks session start"):
                lease.start_session()
            record = retention._load_lease(lease.build_id)
            self.assertFalse(record["session_started"], "capacity refusal must precede the durable start marker")
            self.assertEqual(record["session_v3"]["state"], "RESERVED")

    def test_r71_real_validator_rejects_pre_reserve_stale_candidate_and_replay(self) -> None:
        for suffix, mutation in (("stale-candidate", "candidate"),
                                 ("stale-plan", "plan"),
                                 ("stale-g0c", "g0c"),
                                 ("stale-manifest", "manifest")):
            with self.subTest(mutation=mutation):
                patches = self._r71_validation_boundary_patches()
                with patches[0], patches[1], patches[2], patches[3], patches[4]:
                    lease, capability, candidate_path, plan_path, dependency_path, _runner = \
                        self._r71_real_validator_fixture(suffix)
                    if mutation == "candidate":
                        value = json.loads(candidate_path.read_bytes())
                        value["candidate_id"] = "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-OTHER"
                        candidate_path.write_bytes(r6a_dependency_manifest._canonical_bytes(value))
                    elif mutation == "plan":
                        plan_path.write_bytes(plan_path.read_bytes() + b" ")
                    elif mutation == "g0c":
                        g0c_path = self.repo / "experiments/storage/R6-A-R71-G0C-REVIEW.json"
                        value = json.loads(g0c_path.read_bytes())
                        value["findings"].append({"id": "tampered"})
                        g0c_path.write_bytes(r6a_dependency_manifest._canonical_bytes(value))
                    else:
                        dependency_path.write_bytes(dependency_path.read_bytes() + b" ")
                    try:
                        with self.assertRaises(retention.RetentionError):
                            lease.reserve(admission_transaction=capability)
                        self.assertFalse((retention.LEASE_ROOT / f"{lease.build_id}.json").exists())
                        self.assertFalse(capability.used)
                    finally:
                        lease._stop_heartbeat()
                        lease._release_lock()
                        if not capability.lock_file.closed:
                            storage_policy._release_storage_allocation_lock(capability.lock_file)

    @_with_synthetic_buildlease_runtime_gate
    def test_r71_capability_rejects_final_gate_attestation_digest_drift(self) -> None:
        """The mutable Gate output is pinned by ALLOW even though it is not a closure row."""
        patches = self._r71_validation_boundary_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            lease, capability, _candidate, _plan, _dependency, _runner = \
                self._r71_real_validator_fixture("final-gate-digest-drift")
            self.addCleanup(lease._stop_heartbeat)
            self.addCleanup(lease._release_lock)
            gate_path = self.repo / "experiments/storage/storage-gate0-attestation.json"
            gate = json.loads(gate_path.read_bytes())
            gate["final_publication_revision"] = 2
            gate_path.write_bytes(r6a_dependency_manifest._canonical_bytes(gate))

            with self.assertRaisesRegex(RuntimeError,
                                        "R71 Gate, claim, candidate, plan, or build binding changed"):
                capability.validate(
                    build_id=lease.build_id,
                    owner_experiment=lease.owner_experiment,
                    plan_sha256=lease.session_plan_sha256,
                    manifest_path=Path(capability.manifest_path))
            self.assertFalse((retention.LEASE_ROOT / f"{lease.build_id}.json").exists(),
                             "stale final Gate output must block before BuildLease reservation")

    def test_r71_real_validator_rejects_dependency_drift_after_reserve_before_first_dispatch(self) -> None:
        patches = self._r71_validation_boundary_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            lease, capability, _candidate_path, _plan_path, _dependency_path, runner_file = \
                self._r71_real_validator_fixture("post-reserve-start-drift")
            self.addCleanup(lease._stop_heartbeat)
            self.addCleanup(lease._release_lock)
            lease.reserve(admission_transaction=capability)
            runner_file.write_text("runner changed after reserve\n", encoding="utf-8")
            session = lease.start_session()
            workspace_identity = retention._load_lease(lease.build_id)["session_v3"][
                "workspace_mount_identity"]
            with self.assertRaisesRegex(retention.RetentionError,
                                        "dependency closure changed before session consumption"):
                session.run_next()

            failure = retention._read_json(
                retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json")
            record = retention._load_lease(lease.build_id)
            cleanup_receipt = retention._read_json(
                retention.RECEIPT_ROOT / f"{lease.build_id}.json", sealed=True)
            self.assertTrue(record["session_started"])
            self.assertEqual(record["session_v3"]["state"], "FAILED")
            self.assertEqual(record["session_v3"]["next_step_index"], 0)
            self.assertEqual(record["session_v3"]["step_receipts"], [])
            self.assertIn(record["session_v3"].get("graph_outputs"), (None, {}))
            self.assertEqual(failure["triggering_exception_class"], "RetentionError")
            self.assertIn("dependency closure changed", failure["triggering_exception_message"])
            self.assertFalse(failure["graph_execution_started"])
            self.assertEqual(failure["next_step_index"], 0)
            self.assertEqual(failure["step_receipts"], [])
            self.assertEqual(failure["graph_outputs"], {})
            self.assertEqual(failure["workspace_cleanup"]["identity"], workspace_identity)
            self.assertEqual(failure["workspace_cleanup"]["status"], "DETACHED")
            self.assertEqual(failure["retirement_result"]["status"],
                             "PENDING_AFTER_FAILURE_EVIDENCE")
            self.assertEqual(record["retirement_status"], "COMPLETED")
            self.assertEqual(cleanup_receipt["execution_outcome"], "FAILED")
            self.assertIs(cleanup_receipt["outputs_accepted"], False)
            self.assertFalse((session.session_dir / "steps/step-000.command.json").exists())

    def test_r71_real_validator_rejects_replayed_reservation_capability(self) -> None:
        patches = self._r71_validation_boundary_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            lease, capability, _candidate_path, _plan_path, _dependency_path, _runner = \
                self._r71_real_validator_fixture("same-object-replay")
            self.addCleanup(lease._stop_heartbeat)
            self.addCleanup(lease._release_lock)
            lease.reserve(admission_transaction=capability)
            with self.assertRaisesRegex(retention.RetentionError,
                                        "handoff is invalid|identity mismatch or replay"):
                lease.reserve(admission_transaction=capability)
            self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"], "RESERVED")

    def test_r71_projected_threshold_failure_keeps_reservation_unstarted(self) -> None:
        lease, capability, state, _claim_path, _manifest_path = \
            self._r70_reservation_capability_fixture("c7-r71-capacity", milestone="R71")
        try:
            with (mock.patch.object(storage_policy.AdmissionReservationCapability, "_validate_r71"),
                  mock.patch.object(storage_policy, "capture_storage_tree_state", return_value=state)):
                lease.reserve(admission_transaction=capability)
                with (mock.patch.object(storage_policy, "disk_free_bytes", return_value=80 * storage_policy.GIB),
                      mock.patch.object(storage_policy, "measure_project_usage",
                                        return_value={"allocated_inode_deduplicated_bytes": 0}),
                      mock.patch.object(storage_policy, "projected_storage_admission",
                                        return_value={"admitted": False, "blockers": ["threshold"]})):
                    with self.assertRaisesRegex(retention.RetentionError, "projected-capacity boundary"):
                        lease._assert_live_session_reservation_capability()
            record = retention._load_lease(lease.build_id)
            self.assertFalse(record["session_started"])
            self.assertEqual(record["session_v3"]["state"], "RESERVED")
            self.assertFalse((lease.path / retention.BUILD_SESSION_DIRECTORY).exists())
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    def test_r71_free_and_scope_crossings_fail_at_reserve_and_prestart(self) -> None:
        cases = (
            ("reserve-free", "reserve", 24 * storage_policy.GIB, 0),
            ("reserve-scope", "reserve", 100 * storage_policy.GIB,
             storage_policy.PROJECT_BUDGET),
        )
        for suffix, boundary, free_bytes, project_bytes in cases:
            with self.subTest(boundary=boundary, suffix=suffix):
                lease, capability, _state, _claim_path, _manifest_path = \
                    self._r70_reservation_capability_fixture(
                        f"c7-r71-{suffix}", milestone="R71")
                try:
                    with (mock.patch.object(storage_policy.AdmissionReservationCapability,
                                            "_validate_r71"),
                          mock.patch.object(storage_policy, "disk_free_bytes",
                                            return_value=100 * storage_policy.GIB),
                          mock.patch.object(storage_policy, "measure_project_usage", return_value={
                              "allocated_inode_deduplicated_bytes": 0}),
                          mock.patch.object(storage_policy, "capture_storage_tree_state",
                                            return_value={"synthetic": "legacy-only"})):
                        if boundary == "start":
                            lease.reserve(admission_transaction=capability)
                        with (mock.patch.object(storage_policy, "disk_free_bytes",
                                                return_value=free_bytes),
                              mock.patch.object(storage_policy, "measure_project_usage", return_value={
                                  "allocated_inode_deduplicated_bytes": project_bytes})):
                            with self.assertRaisesRegex(
                                    retention.RetentionError,
                                    "live projected-peak check|projected-capacity boundary"):
                                if boundary == "reserve":
                                    lease.reserve(admission_transaction=capability)
                                else:
                                    lease._assert_live_session_reservation_capability()
                    record_path = retention.LEASE_ROOT / f"{lease.build_id}.json"
                    if boundary == "reserve":
                        self.assertFalse(record_path.exists())
                        self.assertFalse(capability.used)
                    else:
                        record = retention._load_lease(lease.build_id)
                        self.assertFalse(record["session_started"])
                        self.assertEqual(record["session_v3"]["state"], "RESERVED")
                finally:
                    lease._stop_heartbeat()
                    lease._release_lock()

    def _assert_r71_capacity_crossing_blocks_start(self, suffix: str, *,
                                                   free_bytes: int, project_bytes: int) -> None:
        lease, capability, _state, _claim_path, _manifest_path = \
            self._r70_reservation_capability_fixture(suffix, milestone="R71")
        try:
            with (mock.patch.object(storage_policy.AdmissionReservationCapability,
                                    "_validate_r71"),
                  mock.patch.object(storage_policy, "disk_free_bytes",
                                    return_value=100 * storage_policy.GIB),
                  mock.patch.object(storage_policy, "measure_project_usage", return_value={
                      "allocated_inode_deduplicated_bytes": 0}),
                  mock.patch.object(storage_policy, "capture_storage_tree_state",
                                    return_value={"synthetic": "legacy-only"})):
                lease.reserve(admission_transaction=capability)
                with (mock.patch.object(storage_policy, "disk_free_bytes", return_value=free_bytes),
                      mock.patch.object(storage_policy, "measure_project_usage", return_value={
                          "allocated_inode_deduplicated_bytes": project_bytes})):
                    with self.assertRaisesRegex(retention.RetentionError,
                                                "projected-capacity boundary"):
                        lease._assert_live_session_reservation_capability()
            record = retention._load_lease(lease.build_id)
            self.assertFalse(record["session_started"])
            self.assertEqual(record["session_v3"]["state"], "RESERVED")
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    def test_r71_free_crossing_after_reserve_blocks_start(self) -> None:
        self._assert_r71_capacity_crossing_blocks_start(
            "c7-r71-start-free", free_bytes=24 * storage_policy.GIB, project_bytes=0)

    def test_r71_scope_crossing_after_reserve_blocks_start(self) -> None:
        self._assert_r71_capacity_crossing_blocks_start(
            "c7-r71-start-scope", free_bytes=100 * storage_policy.GIB,
            project_bytes=storage_policy.PROJECT_BUDGET)

    def test_r70_crash_c8_during_allocation_lock_release_keeps_durable_reservation(self) -> None:
        lease, capability, state, claim_path, _manifest_path = \
            self._r70_reservation_capability_fixture("c8")
        release_real = storage_policy._release_storage_allocation_lock
        calls = 0

        def release_then_interrupt(lock_file):
            nonlocal calls
            calls += 1
            release_real(lock_file)
            if calls == 1:
                raise RuntimeError("injected C8 lock-release interruption")

        try:
            with (mock.patch.object(storage_policy, "capture_storage_tree_state", return_value=state),
                  mock.patch.object(storage_policy, "_release_storage_allocation_lock",
                                    side_effect=release_then_interrupt)):
                with self.assertRaisesRegex(RuntimeError, "C8 lock-release interruption"):
                    lease.reserve(admission_transaction=capability)
            record = retention._load_lease(lease.build_id)
            self.assertEqual(record["session_v3"]["state"], "RESERVED")
            self.assertFalse(record["session_started"])
            self.assertTrue(capability.used)
            self.assertTrue(claim_path.is_file())
            self.assertTrue(lease.path.is_dir())
            self.assertFalse((lease.path / retention.BUILD_SESSION_DIRECTORY).exists())
            self.assertEqual(calls, 1, "the allocation lock was released before the injected interruption")
            lease._stop_heartbeat()
            lease._release_lock()
            self.assertEqual(retention.reconcile_pending(), [])
            reconciled = retention._load_lease(lease.build_id)
            self.assertEqual(reconciled["session_v3"]["state"], "RESERVED")
            self.assertFalse(reconciled["session_started"])
        finally:
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
    def test_r91_active_buildlease_workspace_identity_is_bound_before_session_capacity_measurement(self) -> None:
        lease = self._session_lease("r91-active-workspace-capacity-identity")
        lease.reserve()
        try:
            with mock.patch.object(storage_policy, "measure_project_usage",
                                   return_value={"allocated_inode_deduplicated_bytes": 0,
                                                 "allocated_inode_deduplicated_bytes_excluding_paths": 0}) as measure:
                session = lease.start_session()
            measure.assert_called_once_with(
                strict=False, exclude_paths=(), active_buildlease_id=None)
            record = retention._load_lease(lease.build_id)
            persisted_identity = record["session_v3"]["workspace_mount_identity"]
            self.assertEqual(retention._verified_buildlease_capacity_workspace(lease.build_id),
                             persisted_identity)
            with self.assertRaises(FileNotFoundError):
                retention._verified_buildlease_capacity_workspace(lease.build_id + "-wrong")
            record = retention._load_lease(lease.build_id)
            record["state"] = "FAILED"
            retention._store_lease(record)
            with self.assertRaises(retention.RetentionError):
                retention._verified_buildlease_capacity_workspace(lease.build_id)
            record = retention._load_lease(lease.build_id)
            record["state"] = "ACTIVE"
            retention._store_lease(record)
            self.assertEqual(retention._verified_buildlease_capacity_workspace(lease.build_id),
                             persisted_identity)
            record = retention._load_lease(lease.build_id)
            record["session_started"] = False
            retention._store_lease(record)
            with self.assertRaises(retention.RetentionError):
                retention._verified_buildlease_capacity_workspace(lease.build_id)
            record = retention._load_lease(lease.build_id)
            record["session_started"] = True
            retention._store_lease(record)
            self.assertEqual(record["session_v3"]["next_step_index"], 0)
            self.assertEqual(record["session_v3"]["step_receipts"], [])

            original_identity = copy.deepcopy(persisted_identity)
            original_verify = retention._verify_session_workspace_mount

            def require_original_live_identity(root: Path, identity: dict) -> dict:
                if identity != original_identity:
                    raise retention.RetentionError("synthetic live workspace identity mismatch")
                return original_verify(root, identity)

            for key, replacement in (
                    ("mountpoint", str(lease.path / "substitute-workspace")),
                    ("volume_uuid", "R91-WRONG-VOLUME-UUID"),
                    ("image_inode", int(persisted_identity["image_inode"]) + 1),
                    ("device", int(persisted_identity["device"]) + 1),
                    ("filesystem_id", int(persisted_identity["filesystem_id"]) + 1),
                    ("filesystem_type", "apfs"),
                    ("image_path", str(lease.path / "substitute.sparseimage")),
                    ("disk_device", "/dev/disk-r91-substitute"),
                    ("device_node", "/dev/disk-r91s2"),
                    ("requested_capacity_bytes",
                     int(persisted_identity["requested_capacity_bytes"]) + 4096)):
                with self.subTest(identity_field=key):
                    record = retention._load_lease(lease.build_id)
                    tampered = dict(original_identity)
                    tampered[key] = replacement
                    record["session_v3"]["workspace_mount_identity"] = tampered
                    session._store(record)
                    receipt_path = session.session_dir / retention.BUILD_SESSION_PLAN
                    plan_receipt = retention._read_json(receipt_path, sealed=True)
                    plan_receipt["workspace_mount_identity"] = tampered
                    retention._write_json(receipt_path, plan_receipt, replace=True, sealed=True)
                    with mock.patch.object(
                            retention, "_verify_session_workspace_mount",
                            side_effect=require_original_live_identity):
                        with self.assertRaises(retention.RetentionError):
                            retention._verified_buildlease_capacity_workspace(lease.build_id)

                    record = retention._load_lease(lease.build_id)
                    record["session_v3"]["workspace_mount_identity"] = original_identity
                    session._store(record)
                    plan_receipt = retention._read_json(receipt_path, sealed=True)
                    plan_receipt["workspace_mount_identity"] = original_identity
                    retention._write_json(receipt_path, plan_receipt, replace=True, sealed=True)
                    self.assertEqual(
                        retention._verified_buildlease_capacity_workspace(lease.build_id),
                        original_identity)

            session._fail("synthetic cleanup after R91 identity test", -1,
                          command=["session-start"], step_index=None)
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_r94_running_workspace_requires_exact_process_session_receipt_and_mount_authority(self) -> None:
        lease, session = self._start_dual_graph_session(
            "r6a-r71-dual-lineage-r94-running-capacity-authority")
        original_workspace_identity = copy.deepcopy(
            retention._load_lease(lease.build_id)["session_v3"]["workspace_mount_identity"])
        # The synthetic process proves live PID/session-leader ownership. The sealed
        # command receipt is independently bound to the exact plan command below;
        # production dispatch binds both through Popen(command, start_new_session=True).
        process = subprocess.Popen(["/bin/sleep", "60"], start_new_session=True)
        unrelated_process = subprocess.Popen(["/bin/sleep", "60"])
        command_path = session.session_dir / "steps/step-000.command.json"
        plan_path = session.session_dir / retention.BUILD_SESSION_PLAN
        try:
            current = retention._load_lease(lease.build_id)["session_v3"]
            step = session.plan["steps"][0]
            command, executable_identity = session._command_for_step(step, current)
            receipt = {
                "schema_version": retention.BUILD_SESSION_SCHEMA_VERSION,
                "build_id": lease.build_id,
                "executor_mode": retention.BUILD_SESSION_EXECUTOR_MODE,
                "executor_version": retention.BUILD_SESSION_SCHEMA_VERSION,
                "acceptance_state": "UNACCEPTED_UNTIL_WHOLE_LEASE_DETACH",
                "plan_sha256": session.plan_sha256,
                "step_index": 0,
                "kind": step["kind"],
                "command": command,
                "cwd": str(session.workspace),
                "registered_executable_identity": executable_identity,
                "graph_provenance": session._graph_provenance(step),
            }
            receipt_raw = retention._write_json(command_path, receipt, replace=False, sealed=True)
            record = retention._load_lease(lease.build_id)
            record["session_v3"].update({
                "state": "RUNNING",
                "inflight_step_index": 0,
                "inflight_command_receipt_sha256": hashlib.sha256(receipt_raw).hexdigest(),
                "process_session_id": process.pid,
            })
            session._store(record)
            session._active_process = process

            self.assertEqual(
                retention._verified_buildlease_capacity_workspace(lease.build_id),
                original_workspace_identity)
            session._check_limits("during-step")

            record = retention._load_lease(lease.build_id)
            record["session_v3"]["process_session_id"] = None
            session._store(record)
            with self.assertRaisesRegex(
                    retention.RetentionError, "lacks its exact process-session leader"):
                retention._verified_buildlease_capacity_workspace(lease.build_id)
            session._active_process = None
            self.assertEqual(
                retention._verified_buildlease_capacity_workspace(
                    lease.build_id, allow_pre_dispatch=True),
                original_workspace_identity)
            self._bind_session_to_synthetic_r71(lease)
            admission_id = lease.owner_experiment
            pre_dispatch_snapshot_calls: list[dict[str, object]] = []

            def capture_pre_dispatch_snapshot(**kwargs: object) -> dict[str, object]:
                pre_dispatch_snapshot_calls.append(dict(kwargs))
                self.assertEqual(kwargs.get("active_buildlease_id"), lease.build_id)
                self.assertEqual(kwargs.get("active_experiment_id"), admission_id)
                self.assertIs(kwargs.get("allow_pre_dispatch"), True)
                return {
                    "active": [{"build_id": lease.build_id,
                                "owner_experiment": admission_id,
                                "state": "ACTIVE",
                                "path": str(retention.BUILD_ROOT / lease.build_id)}],
                    "completed_pending_retirement": [],
                    "failed_or_retiring": [],
                    "unknown": [],
                }

            with (mock.patch.object(storage_policy, "_active_lease_markers", return_value=[]),
                  mock.patch.object(retention, "_snapshot", side_effect=capture_pre_dispatch_snapshot),
                  mock.patch.object(storage_policy, "_historical_terminal_lsof_identity",
                                    return_value={"path": "/usr/sbin/lsof", "sha256": "a" * 64}),
                  mock.patch.object(storage_policy.subprocess, "run",
                                    return_value=subprocess.CompletedProcess(
                                        args=["/usr/sbin/lsof"], returncode=0, stdout="", stderr=""))):
                liveness = storage_policy._historical_terminal_liveness_check(
                    {"process_ids": [], "paths": {"output_dir": str(self.repo / "unused-output")},
                     "prefix_path": str(self.home / "unused-deleted-prefix"),
                     "prefix_was_deleted": True},
                    allowed_active_experiment_id=admission_id,
                    allowed_active_build_id=lease.build_id,
                    allow_pre_dispatch=True,
                    terminal_closed_at_unix=1.0)
            self.assertEqual(len(pre_dispatch_snapshot_calls), 1)
            self.assertEqual(liveness["build_retention_status"], "QUIESCENT")
            root_scan_calls: list[dict[str, object]] = []

            def verify_pre_dispatch_workspace(**kwargs: object) -> list[Path]:
                root_scan_calls.append(dict(kwargs))
                self.assertEqual(kwargs.get("active_buildlease_id"), lease.build_id)
                self.assertEqual(kwargs.get("active_workspace_identity"), original_workspace_identity)
                allow_pre_dispatch = kwargs.get("allow_pre_dispatch") is True
                self.assertTrue(allow_pre_dispatch)
                self.assertEqual(
                    retention._verified_capacity_workspace(
                        lease.build_id, allow_pre_dispatch=allow_pre_dispatch),
                    original_workspace_identity)
                return []

            completed_git = subprocess.CompletedProcess(
                args=["git", "worktree", "list"], returncode=0, stdout="", stderr="")
            (self.home / "Library" / "Caches").mkdir(parents=True, exist_ok=True)
            original_iterdir = Path.iterdir

            def isolated_iterdir(path: Path):
                if path == Path("/private/tmp"):
                    return iter(())
                return original_iterdir(path)

            with (mock.patch.object(storage_policy.Path, "home", return_value=self.home),
                  mock.patch.object(storage_policy.Path, "iterdir", isolated_iterdir),
                  mock.patch.object(storage_policy.subprocess, "run", return_value=completed_git),
                  mock.patch.object(retention, "_all_root_candidates",
                                    side_effect=verify_pre_dispatch_workspace)):
                storage_policy._classified_project_roots(
                    strict=True, active_buildlease_id=lease.build_id,
                    active_workspace_identity=original_workspace_identity,
                    allow_pre_dispatch=True)
            self.assertEqual(len(root_scan_calls), 1)

            measurement_calls: list[dict[str, object]] = []

            def capture_classified_roots(**kwargs: object) -> list[object]:
                measurement_calls.append(dict(kwargs))
                return []

            with (mock.patch.object(storage_policy, "_verified_r71_capacity_workspace",
                                    return_value=original_workspace_identity),
                  mock.patch.object(storage_policy, "_classified_project_roots",
                                    side_effect=capture_classified_roots)):
                _ORIGINAL_MEASURE_PROJECT_USAGE(
                    strict=True, active_buildlease_id=lease.build_id,
                    r71_allow_pre_dispatch=True)
            self.assertEqual(len(measurement_calls), 1)
            self.assertEqual(measurement_calls[0].get("allow_pre_dispatch"), True)

            root_scan_calls: list[dict[str, object]] = []

            def skip_host_mount_enumeration(**kwargs: object) -> list[Path]:
                root_scan_calls.append(dict(kwargs))
                self.assertEqual(kwargs.get("active_buildlease_id"), lease.build_id)
                self.assertEqual(kwargs.get("allow_pre_dispatch"), True)
                return []

            with mock.patch.object(retention, "_all_root_candidates",
                                   side_effect=skip_host_mount_enumeration):
                retention._status_snapshot_with_lock(
                    lease.lock_fds, active_buildlease_id=lease.build_id,
                    active_experiment_id=lease.owner_experiment, allow_pre_dispatch=True)
            self.assertEqual(len(root_scan_calls), 1)
            self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"],
                             "RUNNING",
                             "Gate retention reconciliation must preserve the exact pre-dispatch session")
            record = retention._load_lease(lease.build_id)
            record["session_v3"]["inflight_command_receipt_sha256"] = "0" * 64
            session._store(record)
            with self.assertRaisesRegex(
                    retention.RetentionError, "command receipt does not match its exact plan step"):
                retention._verified_buildlease_capacity_workspace(
                    lease.build_id, allow_pre_dispatch=True)
            with self.assertRaisesRegex(
                    retention.RetentionError, "command receipt does not match its exact plan step"):
                retention._status_snapshot_with_lock(
                    lease.lock_fds, active_buildlease_id=lease.build_id,
                    active_experiment_id=lease.owner_experiment, allow_pre_dispatch=True)
            self.assertEqual(retention._load_lease(lease.build_id)["session_v3"]["state"],
                             "RUNNING",
                             "failed pre-dispatch verification must not trigger nested crash recovery")
            record = retention._load_lease(lease.build_id)
            record["session_v3"]["process_session_id"] = process.pid
            record["session_v3"]["inflight_command_receipt_sha256"] = hashlib.sha256(receipt_raw).hexdigest()
            session._store(record)
            session._active_process = process

            record = retention._load_lease(lease.build_id)
            record["session_v3"]["process_session_id"] = unrelated_process.pid
            session._store(record)
            with self.assertRaisesRegex(retention.RetentionError, "process-session leader"):
                retention._verified_buildlease_capacity_workspace(lease.build_id)
            record = retention._load_lease(lease.build_id)
            record["session_v3"]["process_session_id"] = process.pid
            session._store(record)

            session._active_process = unrelated_process
            with self.assertRaisesRegex(retention.RetentionError, "exact in-flight process owner"):
                session._check_limits("during-step")
            session._active_process = process

            record = retention._load_lease(lease.build_id)
            record["session_v3"]["inflight_step_index"] = 1
            session._store(record)
            with self.assertRaisesRegex(retention.RetentionError, "inconsistent in-flight authority"):
                retention._verified_buildlease_capacity_workspace(lease.build_id)
            record = retention._load_lease(lease.build_id)
            record["session_v3"]["inflight_step_index"] = 0
            session._store(record)

            record = retention._load_lease(lease.build_id)
            record["session_v3"]["inflight_command_receipt_sha256"] = None
            session._store(record)
            with self.assertRaisesRegex(retention.RetentionError, "lacks its exact in-flight step receipt"):
                retention._verified_buildlease_capacity_workspace(lease.build_id)
            record = retention._load_lease(lease.build_id)
            record["session_v3"]["inflight_command_receipt_sha256"] = hashlib.sha256(
                command_path.read_bytes()).hexdigest()
            session._store(record)

            for field, replacement in (
                    ("graph_id", "native-verification-r6a"),
                    ("command", ["/bin/false"])):
                with self.subTest(receipt_field=field):
                    modified = retention._read_json(command_path, sealed=True)
                    if field == "graph_id":
                        modified["graph_provenance"] = dict(modified["graph_provenance"])
                        modified["graph_provenance"][field] = replacement
                    else:
                        modified[field] = replacement
                    modified_raw = retention._write_json(
                        command_path, modified, replace=True, sealed=True)
                    record = retention._load_lease(lease.build_id)
                    record["session_v3"]["inflight_command_receipt_sha256"] = hashlib.sha256(
                        modified_raw).hexdigest()
                    session._store(record)
                    with self.assertRaisesRegex(retention.RetentionError, "exact plan step"):
                        retention._verified_buildlease_capacity_workspace(lease.build_id)
                    restored_raw = retention._write_json(
                        command_path, receipt, replace=True, sealed=True)
                    record = retention._load_lease(lease.build_id)
                    record["session_v3"]["inflight_command_receipt_sha256"] = hashlib.sha256(
                        restored_raw).hexdigest()
                    session._store(record)

            record = retention._load_lease(lease.build_id)
            substituted = copy.deepcopy(original_workspace_identity)
            substituted["mountpoint"] = str(session.root / "unrelated-workspace")
            record["session_v3"]["workspace_mount_identity"] = substituted
            session._store(record)
            with self.assertRaisesRegex(retention.RetentionError, "plan or persisted identity"):
                retention._verified_buildlease_capacity_workspace(lease.build_id)
            record = retention._load_lease(lease.build_id)
            record["session_v3"]["workspace_mount_identity"] = original_workspace_identity
            session._store(record)

            unrelated_mount = copy.deepcopy(original_workspace_identity)
            unrelated_mount["device_node"] = "/dev/disk-r94-unrelated"
            unrelated_mount["volume_uuid"] = "R94-UNRELATED-VOLUME"
            record = retention._load_lease(lease.build_id)
            record["session_v3"]["workspace_mount_identity"] = unrelated_mount
            session._store(record)
            plan_receipt = retention._read_json(plan_path, sealed=True)
            plan_receipt["workspace_mount_identity"] = unrelated_mount
            retention._write_json(plan_path, plan_receipt, replace=True, sealed=True)

            def require_original_mount(_root: Path, identity: dict[str, object]) -> dict[str, object]:
                if identity != original_workspace_identity:
                    raise retention.RetentionError("synthetic unrelated mount rejected")
                return identity

            with mock.patch.object(retention, "_verify_session_workspace_mount",
                                   side_effect=require_original_mount):
                with self.assertRaisesRegex(retention.RetentionError, "unrelated mount"):
                    retention._verified_buildlease_capacity_workspace(lease.build_id)

            plan_receipt["workspace_mount_identity"] = original_workspace_identity
            retention._write_json(plan_path, plan_receipt, replace=True, sealed=True)
            record = retention._load_lease(lease.build_id)
            record["session_v3"]["workspace_mount_identity"] = original_workspace_identity
            session._store(record)
        finally:
            for child in (process, unrelated_process):
                if child.poll() is None:
                    child.terminate()
                child.wait(timeout=5)
            session._active_process = None
            try:
                record = retention._load_lease(lease.build_id)
                current = record.get("session_v3")
                if isinstance(current, dict) and current.get("state") == "RUNNING":
                    current["state"] = "ACTIVE"
                    current.pop("inflight_step_index", None)
                    current.pop("inflight_command_receipt_sha256", None)
                    current.pop("process_session_id", None)
                    record["session_v3"] = current
                    session._store(record)
                    session._fail("R94 focused authority test cleanup", -1,
                                  command=["r94-focused-test-cleanup"], step_index=None)
            finally:
                lease._stop_heartbeat()
                lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_r91_unrelated_project_growth_blocks_post_attach_capacity_recheck(self) -> None:
        lease, session = self._start_session("r91-unrelated-project-growth")
        self._bind_session_to_synthetic_r71(lease)
        unrelated = self.repo / "unrelated-project-growth.bin"
        unrelated.write_bytes(b"outside the exact BuildLease root")
        expected_exclusion = [lease.path]
        measured_calls: list[dict[str, object]] = []

        def measure_with_unrelated_growth(**kwargs: object) -> dict[str, object]:
            measured_calls.append(dict(kwargs))
            return {
                "allocated_inode_deduplicated_bytes": storage_policy.PROJECT_BUDGET + 1,
                "allocated_inode_deduplicated_bytes_excluding_paths":
                    storage_policy.PROJECT_BUDGET + 1,
            }

        try:
            self.assertEqual(
                retention._verified_buildlease_capacity_workspace(lease.build_id),
                retention._load_lease(lease.build_id)["session_v3"]["workspace_mount_identity"],
            )
            with (mock.patch.object(storage_policy, "measure_project_usage",
                                    side_effect=measure_with_unrelated_growth),
                  mock.patch.object(storage_policy, "disk_free_bytes",
                                    return_value=100 * storage_policy.GIB)):
                with self.assertRaisesRegex(
                        retention.RetentionError,
                        "runtime storage limits block continuation.*60 GiB ceiling"):
                    session._check_limits("session-start")

            self.assertEqual(len(measured_calls), 1)
            self.assertIs(measured_calls[0]["strict"], True)
            self.assertEqual(measured_calls[0]["exclude_paths"], expected_exclusion)
            self.assertEqual(measured_calls[0]["active_buildlease_id"], lease.build_id)
            self.assertTrue(unrelated.is_file())
        finally:
            # Restore a non-R71 synthetic owner before ordinary fixture cleanup;
            # the capacity failure above remains the observed test result.
            record = retention._load_lease(lease.build_id)
            record["owner_experiment"] = "synthetic-test"
            retention._store_lease(record)
            root_fd = retention._open_dir(lease.path)
            try:
                marker = retention._read_marker(root_fd)
                marker.update(record)
                retention._write_marker(root_fd, marker)
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            session._fail("R91 capacity growth regression cleanup", -1,
                          command=["session-start"])
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_r91_session_start_failure_detaches_before_residual_liveness_and_retires_only_after_evidence(self) -> None:
        lease = self._session_lease("r91-session-start-recovery-order")
        lease.reserve()
        sessions: list[retention.BuildLeaseSession] = []
        events: list[str] = []
        reason = "synthetic original session-start failure"
        original_record_failure = retention._record_unaccepted_session_failure
        original_retire = retention._retire

        def fail_after_workspace(session: retention.BuildLeaseSession, phase: str,
                                 **_kwargs: object) -> None:
            if phase == "session-start":
                sessions.append(session)
                raise retention.RetentionError(reason)
            raise AssertionError(f"unexpected storage check phase {phase}")

        def detach(root: Path, identity: dict | None) -> dict:
            events.append("detach")
            return self._fake_detach_session_workspace(root, identity)

        def liveness(path: Path) -> dict:
            events.append("liveness")
            active = retention._load_lease(lease.build_id)["session_v3"]
            self.assertIs(active["workspace_mount_identity"]["attached"], False)
            self.assertTrue((lease.path / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME).is_file())
            return {"path": str(path), "status": "NO_HOLDERS", "return_code": 1,
                    "process_ids": [], "process_ids_truncated": False, "stderr": ""}

        def capture(lease_record: dict, failure_reason: str, **kwargs: object) -> None:
            events.append("failure-evidence")
            original_record_failure(lease_record, failure_reason, **kwargs)

        def retire(lease_record: dict, *, reason: str) -> dict:
            events.append("retire")
            return original_retire(lease_record, reason=reason)

        try:
            with (mock.patch.object(retention.BuildLeaseSession, "_check_limits", autospec=True,
                                    side_effect=fail_after_workspace),
                  mock.patch.object(retention, "_session_start_admission_evidence",
                                    return_value={"admission_decision": "ALLOW",
                                                  "admission_id": "synthetic-signed-allow",
                                                  "admission_manifest_sha256": "a" * 64}),
                  mock.patch.object(retention, "_detach_session_workspace", side_effect=detach),
                  mock.patch.object(retention, "_capture_lsof_holder_evidence", side_effect=liveness),
                  mock.patch.object(retention, "_record_unaccepted_session_failure", side_effect=capture),
                  mock.patch.object(retention, "_retire", side_effect=retire)):
                with self.assertRaisesRegex(retention.RetentionError, "original session-start failure"):
                    lease.start_session()

            session = sessions[0]
            self.assertLess(events.index("detach"), events.index("liveness"))
            self.assertLess(events.index("liveness"), events.index("failure-evidence"))
            self.assertLess(events.index("failure-evidence"), events.index("retire"))
            record = retention._load_lease(lease.build_id)
            failure = retention._read_json(
                retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json")
            self.assertEqual(record["state"], "FAILED")
            self.assertEqual(record["retirement_status"], "COMPLETED")
            self.assertTrue(record["session_started"])
            self.assertEqual(record["session_v3"]["next_step_index"], 0)
            self.assertEqual(record["session_v3"]["step_receipts"], [])
            self.assertIn(record["session_v3"].get("graph_outputs"), (None, {}))
            self.assertFalse((lease.path / retention.BUILD_SESSION_TERMINAL).exists())
            self.assertFalse((retention.MANIFEST_ROOT / f"{lease.build_id}.json").exists())
            self.assertIs(failure["outputs_accepted"], False)
            original_failure = repr(retention.RetentionError(reason))
            self.assertEqual(failure["failure_reason"], original_failure)
            self.assertEqual(failure["failure_reason_sha256"],
                             hashlib.sha256(original_failure.encode("utf-8")).hexdigest())
            self.assertEqual(failure["admission_decision"], "ALLOW")
            self.assertTrue(failure["buildlease_acquired"])
            self.assertTrue(failure["session_start_attempted"])
            self.assertEqual(failure["workspace_cleanup"]["status"], "DETACHED")
            self.assertEqual(failure["residual_holder_evidence"]["status"], "NO_HOLDERS")
            self.assertEqual(failure["next_step_index"], 0)
            self.assertFalse(failure["graph_execution_started"])
            self.assertEqual(failure["graph_outputs"], {})
            self.assertEqual(failure["step_receipts"], [])
            self.assertFalse(failure["finalizing"])
            self.assertFalse(failure["success"])
            self.assertFalse(failure["r6a_accepted"])
            cleanup_receipt = retention._read_json(
                retention.RECEIPT_ROOT / f"{lease.build_id}.json", sealed=True)
            self.assertEqual(cleanup_receipt["execution_outcome"], "FAILED")
            self.assertIs(cleanup_receipt["outputs_accepted"], False)
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_r91_session_start_cleanup_and_residual_holder_blockers_preserve_both_errors(self) -> None:
        for failure_kind in ("cleanup", "eject-failure", "residual-holder", "wrong-identity"):
            with self.subTest(failure_kind=failure_kind):
                lease, session = self._start_r71_session(f"r91-recovery-blocker-{failure_kind}")
                reason = f"original failure for {failure_kind}"
                cleanup_error = ("hdiutil eject failed: device busy" if failure_kind == "eject-failure"
                                 else "normal unmount blocked by external holder pid 4321")
                detach = mock.Mock(side_effect=retention.RetentionError(cleanup_error))
                holder = {"path": str(lease.path), "status": "HOLDERS", "return_code": 0,
                          "process_ids": [4321], "process_ids_truncated": False,
                          "stderr": "external holder"}
                try:
                    if failure_kind == "wrong-identity":
                        record = retention._load_lease(lease.build_id)
                        record["session_v3"]["workspace_mount_identity"]["volume_uuid"] = "wrong-volume"
                        session._store(record)
                        detach = mock.Mock(side_effect=self.fail)
                    elif failure_kind == "residual-holder":
                        detach = mock.Mock(side_effect=lambda root, identity:
                                           self._fake_detach_session_workspace(root, identity))
                    def verify_workspace(root: Path, identity: dict) -> dict:
                        if failure_kind == "wrong-identity":
                            raise retention.RetentionError("wrong-volume workspace identity")
                        return self._fake_verify_session_workspace_mount(root, identity)
                    with (mock.patch.object(retention, "_session_start_admission_evidence",
                                            return_value={"admission_decision": "ALLOW",
                                                          "admission_id": "synthetic-signed-allow",
                                                          "admission_manifest_sha256": "b" * 64}),
                          mock.patch.object(retention, "_detach_session_workspace", detach),
                          mock.patch.object(retention, "_verify_session_workspace_mount",
                                            side_effect=verify_workspace),
                          mock.patch.object(retention, "_capture_lsof_holder_evidence",
                                            return_value=holder),
                          mock.patch.object(retention, "_retire",
                                            side_effect=AssertionError("blocked failure must not retire"))):
                        with self.assertRaises(retention.RetentionError):
                            session._fail(reason, -1, command=["session-start"], step_index=None)

                    detach.assert_not_called() if failure_kind == "wrong-identity" else detach.assert_called_once()
                    failure = retention._read_json(
                        retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json")
                    record = retention._load_lease(lease.build_id)
                    self.assertEqual(failure["failure_reason"], reason)
                    self.assertIs(failure["outputs_accepted"], False)
                    self.assertEqual(failure["residual_holder_evidence"]["process_ids"], [4321])
                    self.assertIsNone(record.get("retired_at_unix"))
                    self.assertTrue(lease.path.exists())
                    self.assertTrue((lease.path / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME).is_file())
                    self.assertEqual(failure["next_step_index"], 0)
                    self.assertEqual(failure["step_receipts"], [])
                    self.assertEqual(failure["graph_outputs"], {})
                    self.assertFalse(failure["graph_execution_started"])
                    self.assertFalse(failure["outputs_accepted"])
                    if failure_kind in {"cleanup", "eject-failure"}:
                        self.assertEqual(failure["workspace_cleanup"]["status"], "BLOCKED")
                        self.assertIn(cleanup_error.split(":")[0], failure["cleanup_error"])
                    elif failure_kind == "wrong-identity":
                        self.assertEqual(failure["workspace_cleanup"]["status"], "BLOCKED")
                        self.assertIn("wrong-volume", failure["cleanup_error"])
                    else:
                        self.assertEqual(failure["workspace_cleanup"]["status"], "DETACHED")
                        self.assertEqual(failure["residual_holder_evidence"]["status"], "HOLDERS")
                finally:
                    lease._stop_heartbeat()
                    lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_r91_unknown_signed_admission_blocks_pregraph_retirement_after_failure_evidence(self) -> None:
        lease = self._session_lease("r91-session-start-unknown-admission")
        lease.reserve()
        sessions: list[retention.BuildLeaseSession] = []
        reason = "synthetic original session-start failure"

        def fail_after_workspace(session: retention.BuildLeaseSession, phase: str,
                                 **_kwargs: object) -> None:
            if phase == "session-start":
                sessions.append(session)
                raise retention.RetentionError(reason)
            raise AssertionError(f"unexpected storage check phase {phase}")

        try:
            record = retention._load_lease(lease.build_id)
            record["owner_experiment"] = f"r6a-r71-admission-{lease.build_id}"
            retention._store_lease(record)
            root_fd = retention._open_dir(lease.path)
            try:
                marker = retention._read_marker(root_fd)
                marker.update(record)
                retention._write_marker(root_fd, marker)
                os.fsync(root_fd)
            finally:
                os.close(root_fd)
            with (mock.patch.object(retention.BuildLeaseSession, "_check_limits", autospec=True,
                                    side_effect=fail_after_workspace),
                  mock.patch.object(retention, "_session_start_admission_evidence",
                                    return_value={"admission_decision": "UNKNOWN",
                                                  "admission_manifest_sha256": "c" * 64}),
                  mock.patch.object(retention, "_capture_lsof_holder_evidence",
                                    return_value={"path": str(lease.path), "status": "NO_HOLDERS",
                                                  "return_code": 1, "process_ids": [],
                                                  "process_ids_truncated": False, "stderr": ""}),
                  mock.patch.object(retention, "_retire",
                                    side_effect=AssertionError("unverified R71 ALLOW must not retire"))):
                with self.assertRaisesRegex(retention.RetentionError, "authenticated signed ALLOW"):
                    lease.start_session()

            self.assertEqual(len(sessions), 1)
            failure = retention._read_json(
                retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json")
            record = retention._load_lease(lease.build_id)
            self.assertEqual(failure["failure_reason"], repr(retention.RetentionError(reason)))
            self.assertEqual(failure["admission_decision"], "UNKNOWN")
            self.assertEqual(failure["retirement_result"], {
                "status": "BLOCKED", "reason": "signed_admission_allow_unverified"})
            self.assertEqual(failure["workspace_cleanup"]["status"], "DETACHED")
            self.assertEqual(failure["residual_holder_evidence"]["status"], "NO_HOLDERS")
            self.assertEqual(record["state"], "FAILED")
            self.assertIsNone(record.get("retired_at_unix"))
            self.assertTrue(lease.path.is_dir())

            with mock.patch.object(retention, "_session_start_admission_evidence",
                                   return_value={"admission_decision": "UNKNOWN"}):
                with self.assertRaisesRegex(
                        retention.RetentionError, "requires its authenticated signed ALLOW"):
                    retention._retire(record, reason="pending-preflight-recovery")
        finally:
            lease._stop_heartbeat()
            lease._release_lock()

    @_with_synthetic_buildlease_runtime_gate
    def test_r91_unknown_signed_admission_blocks_pregraph_crash_recovery_retirement(self) -> None:
        lease, _session = self._start_session("r91-crash-recovery-unknown-admission")
        self._bind_session_to_synthetic_r71(lease)
        lease._release_lock()
        with (mock.patch.object(retention, "_session_start_admission_evidence",
                                return_value={"admission_decision": "UNKNOWN",
                                              "admission_manifest_sha256": "d" * 64}),
              mock.patch.object(retention, "_recover_failed_session_workspace_detach")):
            with self.assertRaisesRegex(
                    retention.RetentionError, "requires its authenticated signed ALLOW"):
                retention._retire_pending_locked()

        record = retention._load_lease(lease.build_id)
        failure = retention._read_json(
            retention.MANIFEST_ROOT / f"{lease.build_id}.failure/failure-manifest.json")
        self.assertEqual(record["state"], "FAILED")
        self.assertEqual(record["failed_command"],
                         ["host-buildlease-v3", "controller-crash-recovery"])
        self.assertIsNone(record.get("retired_at_unix"))
        self.assertTrue(lease.path.is_dir())
        self.assertEqual(failure["admission_decision"], "UNKNOWN")
        self.assertEqual(failure["retirement_result"], {
            "status": "BLOCKED", "reason": "signed_admission_allow_unverified"})
        self.assertFalse((retention.RECEIPT_ROOT / f"{lease.build_id}.json").exists())
        lease._stop_heartbeat()

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
    def test_r23_terminal_orphan_exact_device_identity_classifies_without_mutation(self) -> None:
        build_id = retention.R23_ORPHANED_ROOT_BUILD_ID
        root = retention.R23_ORPHANED_ROOT_PATH
        lease_path = retention.LEASE_ROOT / f"{build_id}.json"
        receipt_path = retention._orphan_closeout_receipt_path(build_id)
        lease = retention._load_lease(build_id)
        lease_before = retention._read_file_descriptor(lease_path)
        receipt_before = retention._read_file_descriptor(receipt_path)
        root_fd = retention._open_dir(root)
        try:
            marker_before = retention._read_at(root_fd, retention.BUILD_MARKER)
            entries_before = sorted(os.listdir(root_fd))
            self.assertEqual(lease["state"], retention.ORPHANED_UNSTARTED_STATE)
            root_info = os.fstat(root_fd)
            self.assertEqual(root_info.st_dev, lease["build_device"])
            self.assertEqual(getattr(os.fstatvfs(root_fd), "f_fsid", None),
                             lease["build_filesystem_id"])
        finally:
            os.close(root_fd)

        snapshot = retention._snapshot()
        unknown_paths = {row.get("path") for row in snapshot["unknown"]}
        orphan_ids = {row.get("build_id") for row in snapshot["orphaned_unstarted_retired"]}
        self.assertNotIn(str(root), unknown_paths)
        self.assertIn(build_id, orphan_ids)

        root_fd = retention._open_dir(root)
        try:
            self.assertEqual(retention._read_at(root_fd, retention.BUILD_MARKER), marker_before)
            self.assertEqual(sorted(os.listdir(root_fd)), entries_before)
        finally:
            os.close(root_fd)
        self.assertEqual(retention._read_file_descriptor(lease_path), lease_before)
        self.assertEqual(retention._read_file_descriptor(receipt_path), receipt_before)

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

    def test_r17_confirmed_loss_is_a_visible_zero_byte_historical_row(self) -> None:
        disposition = storage_inventory._read_r17_root_a_loss_disposition()
        self.assertIsInstance(disposition, dict)
        row = storage_inventory._r17_historical_loss_inventory_row(disposition)

        self.assertEqual(row["lifecycle_state"], "LOST_UNAUTHORIZED_HISTORICAL_EVIDENCE")
        self.assertEqual(row["path"], str(storage_inventory.R17_ROOT_A_PATH))
        self.assertEqual(row["historical_binding"]["inode"], 99994262)
        self.assertEqual(row["historical_allocated_bytes_observation"], 73728)
        self.assertEqual(row["current_physical_bytes"], 0)
        self.assertFalse(row["counts_as_current_live_scope_root"])
        self.assertFalse(row["original_physical_object_available"])
        self.assertFalse(row["authorized_retirement"])
        self.assertNotIn(row["lifecycle_state"], {"RETIRED", "RETIRED_HISTORICAL"})
        self.assertTrue(row["historical_provenance_caveat_required"])
        self.assertEqual(row["loss_finding_ref"]["path"],
                         storage_inventory.R17_ROOT_A_LOSS_FINDING_PATH)
        self.assertEqual(row["acceptance_ref"]["verdict"],
                         storage_inventory.R17_ROOT_A_ACCEPTANCE_VERDICT)
        identity_record = retention.R17_TEMP_ROOT_IDENTITIES[
            str(storage_inventory.R17_ROOT_A_PATH)]
        expected_identity = identity_record["filesystem_identity"]
        self.assertEqual(row["historical_binding"], {
            "path": str(storage_inventory.R17_ROOT_A_PATH),
            "type": "directory",
            "device": expected_identity["device"],
            "inode": expected_identity["inode"],
            "filesystem_id": expected_identity["filesystem_id"],
            "mode": f"{expected_identity['mode']:04o}",
            "owner_uid": expected_identity["owner_uid"],
            "sticky": expected_identity["sticky"],
        })

    def test_r17_missing_loss_record_and_zero_byte_live_root_do_not_create_loss_state(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-no-loss-state-",
                                         dir="/private/tmp") as temp:
            temp_root = Path(temp)
            home = temp_root / "home"
            home.mkdir()
            root = temp_root / "empty-live-root"
            root.mkdir()
            missing_record = temp_root / "missing-disposition.json"
            live_identity = self._identity_record(root, "UNRELATED_PROJECT_TEMP")
            live_disposition = {
                **live_identity,
                "logical_bytes": 0,
            }
            original_root_identity = (root.stat().st_dev, root.stat().st_ino,
                                      root.stat().st_mtime_ns, root.stat().st_ctime_ns)

            with (mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                  mock.patch.object(storage_inventory, "HOME", home),
                  mock.patch.object(storage_inventory, "R17_ROOT_A_DISPOSITION_PATH",
                                    missing_record),
                  mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(root): live_identity}, clear=True),
                  mock.patch.object(retention, "_r17_temp_root_disposition",
                                    return_value=live_disposition),
                  mock.patch.object(retention, "_all_root_candidates", return_value=[]),
                  mock.patch.object(storage_inventory.subprocess, "run",
                                    return_value=SimpleNamespace(stdout=""))):
                self.assertIsNone(storage_inventory._read_r17_root_a_loss_disposition())
                discovered = storage_inventory.discover_roots()

            self.assertIn((root, "OTHER_PROJECT_TEMP_ROOT"), discovered)
            self.assertFalse(missing_record.exists())
            self.assertEqual(list(root.iterdir()), [])
            self.assertEqual(original_root_identity,
                             (root.stat().st_dev, root.stat().st_ino,
                              root.stat().st_mtime_ns, root.stat().st_ctime_ns))

    def test_r17_accepted_lost_root_is_not_reprobed_as_the_old_live_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-lost-history-only-",
                                         dir="/private/tmp") as temp:
            temp_root = Path(temp)
            home = temp_root / "home"
            home.mkdir()
            lost_path = temp_root / "lost-original-root"
            accepted_history = {
                "root_key": "R17_ROOT_A_CURRENT_STATE",
                "state": "LOST_UNAUTHORIZED_HISTORICAL_EVIDENCE",
                "historical_binding": {"path": str(lost_path)},
            }
            old_identity = {
                "classification": "FGMETAL_HISTORICAL_PROBE_EVIDENCE",
                "accounting_scope": "FGMETAL_PROTECTED_EVIDENCE",
            }

            with (mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                  mock.patch.object(storage_inventory, "HOME", home),
                  mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(lost_path): old_identity}, clear=True),
                  mock.patch.object(retention, "_r17_temp_root_disposition",
                                    side_effect=AssertionError(
                                        "accepted lost identity must not be reverified")) as verify,
                  mock.patch.object(retention, "_all_root_candidates", return_value=[]),
                  mock.patch.object(storage_inventory.subprocess, "run",
                                    return_value=SimpleNamespace(stdout=""))):
                discovered = storage_inventory.discover_roots(
                    r17_loss_disposition=accepted_history)

            self.assertNotIn(str(lost_path), {str(root) for root, _kind in discovered})
            self.assertFalse(lost_path.exists())
            verify.assert_not_called()

    def test_r17_inventory_keeps_loss_history_out_of_current_capacity_totals(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-inventory-output-",
                                         dir="/private/tmp") as temp:
            temp_root = Path(temp)
            repo = temp_root / "repo"
            repo.mkdir()
            home = temp_root / "home"
            home.mkdir()
            current_root = temp_root / "current-live-root"
            current_root.mkdir()
            current_file = current_root / "current.bin"
            current_file.write_bytes(b"current physical evidence" * 512)
            lost_root = temp_root / "lost-original-root"
            output = temp_root / "inventory.json"

            accepted_loss = storage_inventory._read_r17_root_a_loss_disposition()
            self.assertIsInstance(accepted_loss, dict)
            loss_record = dict(accepted_loss)
            loss_record["historical_binding"] = {
                **accepted_loss["historical_binding"],
                "path": str(lost_root),
            }
            identity_record = self._identity_record(
                current_root, "FGMETAL_HISTORICAL_PROBE_EVIDENCE")
            identity_record["accounting_scope"] = "FGMETAL_PROTECTED_EVIDENCE"
            gate_blocker = (
                "R17-reviewed ownership root is missing; exact-root identity cannot be verified")
            blocked_retention_status = {
                "gate_status": "BLOCKED",
                "gate_blockers": [gate_blocker],
                "unknown": [{"path": str(lost_root), "reason": gate_blocker}],
                "retention_fingerprint": {"sha256": "synthetic-blocked-status"},
            }
            disposition_path = storage_inventory.R17_ROOT_A_DISPOSITION_PATH
            original_disposition_bytes = disposition_path.read_bytes()

            with (mock.patch.object(storage_inventory, "REPO", repo),
                  mock.patch.object(storage_inventory, "HOME", home),
                  mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                  mock.patch.object(storage_inventory, "OUT", output),
                  mock.patch.object(storage_inventory, "_read_r17_root_a_loss_disposition",
                                    return_value=loss_record),
                  mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(lost_root): {
                                      "classification": "FGMETAL_HISTORICAL_PROBE_EVIDENCE",
                                      "accounting_scope": "FGMETAL_PROTECTED_EVIDENCE",
                                  }, str(current_root): identity_record}, clear=True),
                  mock.patch.object(retention, "_r17_ownership_reports_match",
                                    return_value=True),
                  mock.patch.object(retention, "_all_root_candidates", return_value=[]),
                  mock.patch.object(storage_inventory, "mounted_images", return_value={}),
                  mock.patch.object(storage_inventory.subprocess, "run",
                                    return_value=SimpleNamespace(stdout="")),
                  mock.patch.object(retention, "status_snapshot",
                                    return_value=blocked_retention_status),
                  mock.patch.object(storage_policy, "preserved_prefixes", return_value=[]),
                  mock.patch.object(storage_policy, "uncertified_prefixes", return_value=[]),
                  mock.patch.object(storage_policy, "_active_lease_markers", return_value=[]),
                  mock.patch.object(storage_policy, "prefix_state_fingerprint",
                                    return_value={"sha256": "synthetic-prefix-state"}),
                  mock.patch("builtins.print")):
                result = storage_inventory.main()

            self.assertEqual(result, 0)
            self.assertTrue(output.is_file())
            payload = json.loads(output.read_text(encoding="utf-8"))
            historical_rows = payload["historical_root_dispositions"]
            self.assertEqual(len(historical_rows), 1)
            history = historical_rows[0]
            self.assertEqual(history["lifecycle_state"],
                             "LOST_UNAUTHORIZED_HISTORICAL_EVIDENCE")
            self.assertEqual(history["historical_binding"]["path"], str(lost_root))
            self.assertEqual(history["loss_finding_ref"]["path"],
                             storage_inventory.R17_ROOT_A_LOSS_FINDING_PATH)
            self.assertEqual(history["current_physical_bytes"], 0)
            self.assertFalse(history["counts_as_current_live_scope_root"])
            expected_loss_caveat = (
                "R17 physical historical evidence loss is confirmed. The original probe bytes are unavailable; "
                "historical custody and reproducibility claims remain caveated."
            )
            self.assertEqual(payload["historical_provenance_caveats"],
                             [expected_loss_caveat])
            self.assertEqual(storage_inventory.R17_ROOT_A_LOSS_CAVEAT, expected_loss_caveat)
            self.assertEqual(retention.R17_ROOT_A_LOSS_CAVEAT, expected_loss_caveat)
            self.assertEqual(
                {row["path"] for row in payload["roots"]},
                {str(repo), str(current_root)},
            )

            expected_current_allocation = current_file.stat().st_blocks * 512
            self.assertEqual(payload["totals"]["logical_bytes_across_paths"],
                             current_file.stat().st_size)
            self.assertEqual(payload["totals"]["allocated_bytes_unique_inode_estimate"],
                             expected_current_allocation)
            self.assertEqual(payload["totals"]["fgmetal_scoped_allocated_inode_bytes"],
                             expected_current_allocation)
            self.assertNotEqual(
                payload["totals"]["fgmetal_scoped_allocated_inode_bytes"],
                expected_current_allocation + history["historical_allocated_bytes_observation"],
            )
            self.assertEqual(payload["storage_gate_metrics"]["build_retention"]["gate_status"],
                             "BLOCKED")
            self.assertEqual(payload["storage_gate_metrics"]["build_retention"]["unknown"][0]["path"],
                             str(lost_root))
            self.assertFalse(lost_root.exists())
            self.assertEqual(disposition_path.read_bytes(), original_disposition_bytes)

    def test_r17_missing_live_root_without_disposition_fails_inventory_discovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-missing-live-root-",
                                         dir="/private/tmp") as temp:
            temp_root = Path(temp)
            home = temp_root / "home"
            home.mkdir()
            missing_root = temp_root / "missing-live-root"
            identity_record = {
                "classification": "FGMETAL_HISTORICAL_PROBE_EVIDENCE",
                "accounting_scope": "FGMETAL_PROTECTED_EVIDENCE",
            }

            with (mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                  mock.patch.object(storage_inventory, "HOME", home),
                  mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(missing_root): identity_record}, clear=True),
                  mock.patch.object(retention, "_r17_temp_root_disposition",
                                    return_value=None),
                  mock.patch.object(retention, "_all_root_candidates", return_value=[]),
                  mock.patch.object(storage_inventory.subprocess, "run",
                                    return_value=SimpleNamespace(stdout=""))):
                with self.assertRaisesRegex(RuntimeError,
                                            "cannot verify exact R17 root identity"):
                    storage_inventory.discover_roots(r17_loss_disposition=None)

            self.assertFalse(missing_root.exists())

    def test_r17_wrong_live_identity_fails_inventory_discovery(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-wrong-live-root-",
                                         dir="/private/tmp") as temp:
            temp_root = Path(temp)
            home = temp_root / "home"
            home.mkdir()
            root = temp_root / "wrong-live-root"
            root.mkdir()
            identity_record = self._identity_record(root, "FGMETAL_HISTORICAL_PROBE_EVIDENCE")
            identity_record["accounting_scope"] = "FGMETAL_PROTECTED_EVIDENCE"
            identity_record["filesystem_identity"] = dict(identity_record["filesystem_identity"])
            identity_record["filesystem_identity"]["inode"] += 1

            with (mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                  mock.patch.object(storage_inventory, "HOME", home),
                  mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(root): identity_record}, clear=True),
                  mock.patch.object(retention, "_r17_ownership_reports_match", return_value=True),
                  mock.patch.object(retention, "_all_root_candidates", return_value=[]),
                  mock.patch.object(storage_inventory.subprocess, "run",
                                    return_value=SimpleNamespace(stdout=""))):
                with self.assertRaisesRegex(RuntimeError,
                                            "cannot verify exact R17 root identity"):
                    storage_inventory.discover_roots(r17_loss_disposition=None)

    def test_r17_reused_lost_path_fails_before_identity_attribution_or_mutation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-reused-loss-path-",
                                         dir="/private/tmp") as temp:
            temp_root = Path(temp)
            home = temp_root / "home"
            home.mkdir()
            reused = temp_root / "reused-root-a"
            reused.write_bytes(b"separate unresolved occupant")
            before_info = reused.lstat()
            before_identity = (before_info.st_dev, before_info.st_ino,
                               before_info.st_mode, before_info.st_size,
                               before_info.st_mtime_ns, before_info.st_ctime_ns)
            before_bytes = reused.read_bytes()
            accepted_history = {
                "root_key": "R17_ROOT_A_CURRENT_STATE",
                "state": "LOST_UNAUTHORIZED_HISTORICAL_EVIDENCE",
                "historical_binding": {"path": str(reused)},
            }

            with (mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                  mock.patch.object(storage_inventory, "HOME", home),
                  mock.patch.object(storage_inventory.subprocess, "run",
                                    return_value=SimpleNamespace(stdout="")),
                  mock.patch.object(retention, "_r17_temp_root_disposition",
                                    side_effect=AssertionError(
                                        "reused pathname must not be attributed to the old identity")) as verify):
                with self.assertRaisesRegex(RuntimeError,
                                            "separate unresolved current object"):
                    storage_inventory.discover_roots(
                        r17_loss_disposition=accepted_history)
                verify.assert_not_called()

            after_info = reused.lstat()
            self.assertEqual(before_identity,
                             (after_info.st_dev, after_info.st_ino, after_info.st_mode,
                              after_info.st_size, after_info.st_mtime_ns, after_info.st_ctime_ns))
            self.assertEqual(before_bytes, reused.read_bytes())

    def test_r17_lost_path_reuse_during_discovery_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-late-reuse-",
                                         dir="/private/tmp") as temp:
            temp_root = Path(temp)
            home = temp_root / "home"
            home.mkdir()
            reused = temp_root / "lost-original-root"
            accepted_history = {
                "root_key": "R17_ROOT_A_CURRENT_STATE",
                "state": "LOST_UNAUTHORIZED_HISTORICAL_EVIDENCE",
                "historical_binding": {"path": str(reused)},
            }
            old_identity = {
                "classification": "FGMETAL_HISTORICAL_PROBE_EVIDENCE",
                "accounting_scope": "FGMETAL_PROTECTED_EVIDENCE",
            }
            occupant_bytes = b"unrelated object created during discovery"
            occupant_identity = None

            def create_unrelated_occupant():
                nonlocal occupant_identity
                reused.mkdir()
                occupant = reused / "marker.bin"
                occupant.write_bytes(occupant_bytes)
                info = reused.lstat()
                occupant_identity = (info.st_dev, info.st_ino, info.st_mode,
                                     info.st_mtime_ns, info.st_ctime_ns)
                return []

            with (mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                  mock.patch.object(storage_inventory, "HOME", home),
                  mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(reused): old_identity}, clear=True),
                  mock.patch.object(retention, "_r17_temp_root_disposition",
                                    side_effect=AssertionError(
                                        "new occupant must not inherit the old R17 identity")) as verify,
                  mock.patch.object(retention, "_all_root_candidates",
                                    side_effect=create_unrelated_occupant),
                  mock.patch.object(storage_inventory.subprocess, "run",
                                    return_value=SimpleNamespace(stdout=""))):
                with self.assertRaisesRegex(RuntimeError,
                                            "separate unresolved current object"):
                    storage_inventory.discover_roots(
                        r17_loss_disposition=accepted_history)

            info = reused.lstat()
            self.assertEqual(occupant_identity,
                             (info.st_dev, info.st_ino, info.st_mode,
                              info.st_mtime_ns, info.st_ctime_ns))
            self.assertEqual((reused / "marker.bin").read_bytes(), occupant_bytes)
            verify.assert_not_called()

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
                str(missing_firelink): {"classification": "UNRELATED_PROJECT_TEMP", "accounting_scope": "OTHER_PROJECT_PROTECTED"},
                str(missing_probe): {"classification": "FGMETAL_HISTORICAL_PROBE_EVIDENCE", "accounting_scope": "FG_METAL_PROJECT"},
            }
            with (mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES, identities, clear=True),
                  mock.patch.object(retention, "_lease_files", return_value=[]),
                  mock.patch.object(retention, "_all_root_candidates", return_value=[]),
                  mock.patch.object(retention, "_classified_non_retention_tmp_roots", return_value=[])):
                state = retention._snapshot(protected_roots=[])
            missing_paths = {row["path"] for row in state["unknown"]}
            self.assertEqual(missing_paths, {str(missing_probe)})
            self.assertNotIn(str(retention.R17_ROOT_A_PATH), missing_paths)
            self.assertNotIn(str(missing_firelink), missing_paths)
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
        expected_contracts = {
            "/private/tmp/buildlease-vz-entitlement.RVW7oO": {
                "role": "ROOT_A",
                "absent_class": retention.R17_CLASS_CONFIRMED_LOST,
                "is_external": False,
            },
            "/private/tmp/firelink-aria2-release.gGghjc/aria2-1.37.0": {
                "role": "EXTERNAL_PROTECTED",
                "absent_class": retention.R17_CLASS_OTHER_PROJECT_PROTECTED_ABSENT,
                "present_class": retention.R17_CLASS_OTHER_PROJECT_PROTECTED_PRESENT,
                "is_external": True,
            },
            "/private/tmp/firelink-aria2-validation.b8C6r1": {
                "role": "EXTERNAL_PROTECTED",
                "absent_class": retention.R17_CLASS_OTHER_PROJECT_PROTECTED_ABSENT,
                "present_class": retention.R17_CLASS_OTHER_PROJECT_PROTECTED_PRESENT,
                "is_external": True,
            },
            "/private/tmp/firelink-tauri3-alpha-target/debug/build": {
                "role": "EXTERNAL_PROTECTED",
                "absent_class": retention.R17_CLASS_OTHER_PROJECT_PROTECTED_ABSENT,
                "present_class": retention.R17_CLASS_OTHER_PROJECT_PROTECTED_PRESENT,
                "is_external": True,
            },
            "/private/tmp/firelink-tauri3-alpha5-1792487343268/src-tauri/target/debug/build": {
                "role": "EXTERNAL_PROTECTED",
                "absent_class": retention.R17_CLASS_OTHER_PROJECT_PROTECTED_ABSENT,
                "present_class": retention.R17_CLASS_OTHER_PROJECT_PROTECTED_PRESENT,
                "is_external": True,
            },
        }

        self.assertEqual(set(retention.R17_TEMP_ROOT_IDENTITIES), set(expected_contracts))

        for path, expected in retention.R17_TEMP_ROOT_IDENTITIES.items():
            contract = expected_contracts[path]
            with self.subTest(path=path):
                target = Path(path)
                is_present = target.exists() or target.is_symlink()
                evaluation = retention.classify_r17_root(path, expected)
                self.assertIsNotNone(evaluation)
                self.assertEqual(evaluation.is_present, is_present)
                self.assertEqual(evaluation.is_external_protected, contract["is_external"])

                if is_present:
                    if contract["role"] == "ROOT_A":
                        # Confirmed-lost Root A pathname must not be occupied by any present object
                        self.assertEqual(evaluation.root_class, retention.R17_CLASS_PATH_REUSED_DIFFERENT_OBJECT)
                        self.assertTrue(evaluation.is_blocking)
                    else:
                        # External protected roots: present object must strictly match exact historical identity
                        self.assertFalse(evaluation.is_blocking, f"present root failed identity verification: {evaluation.error}")
                        self.assertEqual(evaluation.root_class, contract["present_class"])
                        disposition = retention._r17_temp_root_disposition(path)
                        self.assertIsNotNone(disposition)
                        self.assertEqual(disposition["classification"], expected["classification"])
                        fs_id = disposition["filesystem_identity"]
                        expected_fs_id = expected["filesystem_identity"]
                        self.assertEqual(fs_id["device"], expected_fs_id["device"])
                        self.assertEqual(fs_id["inode"], expected_fs_id["inode"])
                        self.assertEqual(fs_id["mode"], expected_fs_id["mode"])
                        self.assertEqual(fs_id["owner_uid"], expected_fs_id["owner_uid"])
                        self.assertEqual(fs_id["sticky"], expected_fs_id["sticky"])
                else:
                    self.assertFalse(evaluation.is_blocking)
                    self.assertEqual(evaluation.root_class, contract["absent_class"])
                    if contract["role"] == "ROOT_A":
                        # Root A: absent evidence confirmed lost backed by valid disposition
                        self.assertIsNotNone(evaluation.disposition)
                        self.assertEqual(
                            evaluation.disposition.get("state"),
                            retention.R17_ROOT_A_LOSS_STATE,
                        )
                        # Without disposition, absent Root A must fail closed
                        fail_closed_eval = retention.classify_r17_root(
                            path, expected, r17_loss_disposition=None
                        )
                        self.assertIsNotNone(fail_closed_eval)
                        self.assertEqual(
                            fail_closed_eval.root_class,
                            retention.R17_CLASS_MISSING_FG_METAL_RETAINED,
                        )
                        self.assertTrue(fail_closed_eval.is_blocking)
                        # With invalid disposition, absent Root A must fail closed
                        with mock.patch.object(
                            retention,
                            "_read_r17_root_a_loss_disposition",
                            side_effect=retention.RetentionError("invalid"),
                        ):
                            invalid_eval = retention.classify_r17_root(path, expected)
                            self.assertIsNotNone(invalid_eval)
                            self.assertEqual(
                                invalid_eval.root_class,
                                retention.R17_CLASS_MISSING_FG_METAL_RETAINED,
                            )
                            self.assertTrue(invalid_eval.is_blocking)
                    else:
                        self.assertIsNone(evaluation.disposition)

    def test_r17_accepted_lost_disposition_suppresses_build_retention_missing_blocker(self) -> None:
        missing_rows = retention._r17_missing_root_rows()
        root_a_path = str(retention.R17_ROOT_A_PATH)
        self.assertNotIn(root_a_path, {row["path"] for row in missing_rows})
        state = retention._snapshot()
        self.assertNotIn(root_a_path, {row["path"] for row in state["unknown"]})
        self.assertIn("historical_root_dispositions", state)
        self.assertEqual(len(state["historical_root_dispositions"]), 1)
        disposition_row = state["historical_root_dispositions"][0]
        self.assertEqual(disposition_row["lifecycle_state"], "LOST_UNAUTHORIZED_HISTORICAL_EVIDENCE")
        self.assertEqual(disposition_row["path"], root_a_path)
        self.assertEqual(disposition_row["current_physical_bytes"], 0)
        self.assertTrue(disposition_row["historical_provenance_caveat_required"])

    def test_r17_absent_disposition_with_absent_root_fails_closed_in_retention(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-abs-disp-", dir="/private/tmp") as temp:
            nonexistent = Path(temp) / "nonexistent-disposition.json"
            with mock.patch.object(retention, "R17_ROOT_A_DISPOSITION_PATH", nonexistent):
                disp = retention._read_r17_root_a_loss_disposition()
                self.assertIsNone(disp)
                missing_rows = retention._r17_missing_root_rows()
                root_a_path = str(retention.R17_ROOT_A_PATH)
                matching = [row for row in missing_rows if row["path"] == root_a_path]
                self.assertEqual(len(matching), 1)
                self.assertIn("exact-root identity cannot be verified", matching[0]["reason"])
                self.assertIn("no valid loss disposition", matching[0]["reason"])

    def test_r17_tampered_disposition_digest_fails_closed_in_retention(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-tamp-disp-", dir="/private/tmp") as temp:
            tampered_file = Path(temp) / "tampered-disposition.json"
            tampered_file.write_text('{"schema_version": 1, "tampered": true}', encoding="utf-8")
            with mock.patch.object(retention, "R17_ROOT_A_DISPOSITION_PATH", tampered_file):
                with self.assertRaises(retention.RetentionError) as ctx:
                    retention._read_r17_root_a_loss_disposition()
                self.assertIn("digest does not match", str(ctx.exception))
                missing_rows = retention._r17_missing_root_rows()
                root_a_path = str(retention.R17_ROOT_A_PATH)
                matching = [row for row in missing_rows if row["path"] == root_a_path]
                self.assertEqual(len(matching), 1)
                self.assertIn("verification failed", matching[0]["reason"])

    def test_r17_wrong_evidence_hashes_fails_closed_in_retention(self) -> None:
        valid_disp = retention._read_r17_root_a_loss_disposition()
        self.assertIsInstance(valid_disp, dict)
        tampered_disp = dict(valid_disp)
        tampered_disp["loss_finding_ref"] = {
            "path": retention.R17_ROOT_A_LOSS_FINDING_PATH,
            "sha256": "0" * 64,
        }
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-bad-ev-", dir="/private/tmp") as temp:
            bad_ev_file = Path(temp) / "bad-ev-disposition.json"
            bad_ev_file.write_text(json.dumps(tampered_disp), encoding="utf-8")
            expected_sha = hashlib.sha256(bad_ev_file.read_bytes()).hexdigest()
            with mock.patch.object(retention, "R17_ROOT_A_DISPOSITION_PATH", bad_ev_file), \
                 mock.patch.object(retention, "R17_ROOT_A_DISPOSITION_SHA256", expected_sha):
                with self.assertRaises(retention.RetentionError) as ctx:
                    retention._read_r17_root_a_loss_disposition(
                        disposition_path=bad_ev_file, expected_sha256=expected_sha)
                self.assertIn("stale or unaccepted evidence", str(ctx.exception))

    def test_r17_wrong_historical_identity_fails_closed_in_retention(self) -> None:
        valid_disp = retention._read_r17_root_a_loss_disposition()
        self.assertIsInstance(valid_disp, dict)
        tampered_disp = dict(valid_disp)
        tampered_binding = dict(valid_disp["historical_binding"])
        tampered_binding["inode"] = 12345678  # wrong inode
        tampered_disp["historical_binding"] = tampered_binding
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-bad-id-", dir="/private/tmp") as temp:
            bad_id_file = Path(temp) / "bad-id-disposition.json"
            bad_id_file.write_text(json.dumps(tampered_disp), encoding="utf-8")
            expected_sha = hashlib.sha256(bad_id_file.read_bytes()).hexdigest()
            with mock.patch.object(retention, "R17_ROOT_A_DISPOSITION_PATH", bad_id_file), \
                 mock.patch.object(retention, "R17_ROOT_A_DISPOSITION_SHA256", expected_sha):
                with self.assertRaises(retention.RetentionError) as ctx:
                    retention._read_r17_root_a_loss_disposition(
                        disposition_path=bad_id_file, expected_sha256=expected_sha)
                self.assertIn("does not match the immutable historical binding", str(ctx.exception))

    def test_r17_state_falsely_changed_to_retired_fails_closed_in_retention(self) -> None:
        valid_disp = retention._read_r17_root_a_loss_disposition()
        self.assertIsInstance(valid_disp, dict)
        tampered_disp = dict(valid_disp)
        tampered_disp["state"] = "RETIRED_HISTORICAL"
        tampered_disp["authorized_retirement"] = True
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-false-ret-", dir="/private/tmp") as temp:
            false_ret_file = Path(temp) / "false-ret-disposition.json"
            false_ret_file.write_text(json.dumps(tampered_disp), encoding="utf-8")
            expected_sha = hashlib.sha256(false_ret_file.read_bytes()).hexdigest()
            with mock.patch.object(retention, "R17_ROOT_A_DISPOSITION_PATH", false_ret_file), \
                 mock.patch.object(retention, "R17_ROOT_A_DISPOSITION_SHA256", expected_sha):
                with self.assertRaises(retention.RetentionError) as ctx:
                    retention._read_r17_root_a_loss_disposition(
                        disposition_path=false_ret_file, expected_sha256=expected_sha)
                self.assertIn("invalid state", str(ctx.exception))

    def test_r17_reused_pathname_with_different_object_blocks_retention(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-reuse-", dir="/private/tmp") as temp:
            reused_path = Path(temp) / "reused-root-a"
            reused_path.mkdir()
            with self.assertRaises(retention.RetentionError) as ctx:
                retention._require_lost_r17_path_unoccupied(reused_path)
            self.assertIn("occupied by a separate unresolved current object", str(ctx.exception))

    def test_r17_live_retained_root_missing_still_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-missing-live-", dir="/private/tmp") as temp:
            missing_retained = Path(temp) / "missing-fgmetal-retained-root"
            identities = {
                str(retention.R17_ROOT_A_PATH): retention.R17_TEMP_ROOT_IDENTITIES[str(retention.R17_ROOT_A_PATH)],
                str(missing_retained): {
                    "classification": "FGMETAL_HISTORICAL_PROBE_EVIDENCE",
                    "accounting_scope": "FG_METAL_PROJECT",
                    "reason": "synthetic missing FG-Metal retained root",
                    "filesystem_identity": {"device": 16777229, "inode": 11111111,
                                            "filesystem_id": 16777229, "mode": 493, "sticky": False, "owner_uid": 501},
                },
            }
            with mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES, identities, clear=True):
                missing_rows = retention._r17_missing_root_rows()
                missing_paths = {row["path"] for row in missing_rows}
                self.assertNotIn(str(retention.R17_ROOT_A_PATH), missing_paths)
                self.assertIn(str(missing_retained), missing_paths)

    def test_r17_live_retained_root_wrong_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-wrong-id-", dir="/private/tmp") as temp:
            wrong_dir = Path(temp) / "wrong-identity-root"
            wrong_dir.mkdir()
            identities = {
                str(wrong_dir): {
                    "classification": "UNRELATED_PROJECT_TEMP",
                    "accounting_scope": "OTHER_PROJECT_PROTECTED",
                    "reason": "synthetic identity expectation",
                    "filesystem_identity": {"device": 999999, "inode": 999999,
                                            "filesystem_id": 999999, "mode": 493, "sticky": False, "owner_uid": 501},
                },
            }
            with mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES, identities, clear=True):
                disposition = retention._r17_temp_root_disposition(wrong_dir)
                self.assertIsNone(disposition)

    def test_r17_current_physical_scope_excludes_lost_bytes(self) -> None:
        disposition = retention._read_r17_root_a_loss_disposition()
        self.assertIsInstance(disposition, dict)
        row = retention._r17_historical_loss_inventory_row(disposition)
        self.assertEqual(row["current_physical_bytes"], 0)
        self.assertFalse(row["counts_as_current_live_scope_root"])

    def test_r17_historical_binding_remains_queryable(self) -> None:
        root_a_identity = retention.R17_TEMP_ROOT_IDENTITIES.get(str(retention.R17_ROOT_A_PATH))
        self.assertIsNotNone(root_a_identity)
        fs_id = root_a_identity["filesystem_identity"]
        self.assertEqual(fs_id["device"], 16777229)
        self.assertEqual(fs_id["inode"], 99994262)

    def test_r17_status_snapshot_unblocked_for_adjudicated_root_a(self) -> None:
        root_a_str = str(retention.R17_ROOT_A_PATH)
        live_state = retention.status_snapshot()
        unknown_paths = {row.get("path") for row in live_state.get("unknown", [])}
        self.assertNotIn(root_a_str, unknown_paths)
        for blocker in live_state.get("gate_blockers", []):
            self.assertNotIn(root_a_str, str(blocker))

        with (mock.patch.object(retention, "_lease_files", return_value=[]),
              mock.patch.object(retention, "_all_root_candidates", return_value=[]),
              mock.patch.object(retention, "_verify_protected_evidence_roots", return_value=[]),
              mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                              {root_a_str: retention.R17_TEMP_ROOT_IDENTITIES[root_a_str]},
                              clear=True)):
            isolated_state = retention.status_snapshot()
            self.assertEqual(isolated_state["gate_status"], "PASS")
            self.assertEqual(isolated_state["gate_blockers"], [])
            self.assertEqual(isolated_state["unknown"], [])

    def test_r17_root_b_external_protected_absent_does_not_block_inventory_or_retention(self) -> None:
        root_b_path = "/private/tmp/firelink-aria2-release.gGghjc/aria2-1.37.0"
        self.assertFalse(Path(root_b_path).exists())
        eval_res = retention.classify_r17_root(root_b_path)
        self.assertIsNotNone(eval_res)
        self.assertEqual(eval_res.root_class, retention.R17_CLASS_OTHER_PROJECT_PROTECTED_ABSENT)
        self.assertEqual(eval_res.accounting_scope, "OTHER_PROJECT_PROTECTED")
        self.assertTrue(eval_res.is_external_protected)
        self.assertFalse(eval_res.is_present)
        self.assertFalse(eval_res.is_blocking)

        missing = retention._r17_missing_root_rows()
        missing_paths = {row["path"] for row in missing}
        self.assertNotIn(root_b_path, missing_paths)

        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-inv-b-", dir="/private/tmp") as temp:
            temp_root = Path(temp)
            home = temp_root / "home"
            home.mkdir()
            with (mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                  mock.patch.object(storage_inventory, "HOME", home),
                  mock.patch.object(retention, "_all_root_candidates", return_value=[]),
                  mock.patch.object(storage_inventory.subprocess, "run",
                                    return_value=SimpleNamespace(stdout=""))):
                discovered = storage_inventory.discover_roots(r17_loss_disposition=retention._read_r17_root_a_loss_disposition())
                discovered_paths = {str(p) for p, _ in discovered}
                self.assertNotIn(root_b_path, discovered_paths)

    def test_r17_root_b_external_protected_present_is_excluded_from_scope(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-pres-b-", dir="/private/tmp") as temp:
            temp_root = Path(temp)
            present_root_b = temp_root / "aria2-1.37.0"
            present_root_b.mkdir()
            info = present_root_b.stat()
            identity_record = {
                "classification": "UNRELATED_PROJECT_TEMP",
                "accounting_scope": "OTHER_PROJECT_PROTECTED",
                "reason": "synthetic present Root B",
                "filesystem_identity": {
                    "device": info.st_dev,
                    "inode": info.st_ino,
                    "filesystem_id": info.st_dev,
                    "mode": stat.S_IMODE(info.st_mode),
                    "sticky": False,
                    "owner_uid": info.st_uid,
                },
            }
            with (mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(present_root_b): identity_record}, clear=True),
                  mock.patch.object(retention, "_r17_ownership_reports_match", return_value=True)):
                eval_res = retention.classify_r17_root(present_root_b)
                self.assertIsNotNone(eval_res)
                self.assertEqual(eval_res.root_class, retention.R17_CLASS_OTHER_PROJECT_PROTECTED_PRESENT)
                self.assertFalse(eval_res.is_blocking)
                self.assertTrue(eval_res.is_present)
                self.assertIsNotNone(eval_res.disposition)
                self.assertFalse(storage_inventory._is_fgmetal_scope_path(
                    present_root_b, "OTHER_PROJECT_TEMP_ROOT"))
                cat, de = storage_inventory.classification(present_root_b, "OTHER_PROJECT_TEMP_ROOT")
                self.assertEqual(cat, "OTHER PROJECT / PROTECTED WORKSTREAM")
                self.assertEqual(de, "DO_NOT_DELETE_OTHER_PROJECT_DATA")

    def test_r17_root_b_pathname_reused_different_object_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r17-reused-b-", dir="/private/tmp") as temp:
            temp_root = Path(temp)
            reused_root_b = temp_root / "aria2-reused"
            reused_root_b.mkdir()
            identity_record = {
                "classification": "UNRELATED_PROJECT_TEMP",
                "accounting_scope": "OTHER_PROJECT_PROTECTED",
                "reason": "synthetic mismatched Root B",
                "filesystem_identity": {
                    "device": 999999,
                    "inode": 999999,
                    "filesystem_id": 999999,
                    "mode": 493,
                    "sticky": False,
                    "owner_uid": 501,
                },
            }
            with (mock.patch.dict(retention.R17_TEMP_ROOT_IDENTITIES,
                                  {str(reused_root_b): identity_record}, clear=True),
                  mock.patch.object(retention, "_r17_ownership_reports_match", return_value=True)):
                eval_res = retention.classify_r17_root(reused_root_b)
                self.assertIsNotNone(eval_res)
                self.assertEqual(eval_res.root_class, retention.R17_CLASS_PATH_REUSED_DIFFERENT_OBJECT)
                self.assertTrue(eval_res.is_blocking)
                self.assertTrue(eval_res.is_present)
                self.assertIsNone(eval_res.disposition)

                missing = retention._r17_missing_root_rows()
                missing_paths = {row["path"] for row in missing}
                self.assertIn(str(reused_root_b), missing_paths)

                with (mock.patch.object(storage_inventory, "TEMP_ROOT", temp_root),
                      mock.patch.object(storage_inventory, "HOME", temp_root),
                      mock.patch.object(retention, "_all_root_candidates", return_value=[]),
                      mock.patch.object(storage_inventory.subprocess, "run",
                                        return_value=SimpleNamespace(stdout=""))):
                    with self.assertRaisesRegex(RuntimeError, "cannot verify exact R17 root identity"):
                        storage_inventory.discover_roots(r17_loss_disposition=None)

    def test_r17_roots_cde_lifecycle_contracts(self) -> None:
        roots_cde = [
            "/private/tmp/firelink-aria2-validation.b8C6r1",
            "/private/tmp/firelink-tauri3-alpha-target/debug/build",
            "/private/tmp/firelink-tauri3-alpha5-1792487343268/src-tauri/target/debug/build",
        ]
        for path_text in roots_cde:
            with self.subTest(root=path_text):
                self.assertFalse(Path(path_text).exists())
                eval_res = retention.classify_r17_root(path_text)
                self.assertIsNotNone(eval_res)
                self.assertEqual(eval_res.root_class, retention.R17_CLASS_OTHER_PROJECT_PROTECTED_ABSENT)
                self.assertEqual(eval_res.accounting_scope, "OTHER_PROJECT_PROTECTED")
                self.assertTrue(eval_res.is_external_protected)
                self.assertFalse(eval_res.is_present)
                self.assertFalse(eval_res.is_blocking)
        missing = retention._r17_missing_root_rows()
        missing_paths = {row["path"] for row in missing}
        for path_text in roots_cde:
            self.assertNotIn(path_text, missing_paths)

    def test_r17_external_protected_absence_cannot_auto_convert_to_lost_disposition(self) -> None:
        root_b_path = "/private/tmp/firelink-aria2-release.gGghjc/aria2-1.37.0"
        eval_res = retention.classify_r17_root(root_b_path)
        self.assertIsNotNone(eval_res)
        self.assertNotEqual(eval_res.root_class, retention.R17_ROOT_A_LOSS_STATE)
        self.assertEqual(eval_res.root_class, retention.R17_CLASS_OTHER_PROJECT_PROTECTED_ABSENT)
        self.assertIsNone(eval_res.disposition)

    def test_r17_inventory_and_retention_agree_on_root_classification(self) -> None:
        for path_text, record in retention.R17_TEMP_ROOT_IDENTITIES.items():
            with self.subTest(path=path_text):
                eval_res = retention.classify_r17_root(path_text, record)
                self.assertIsNotNone(eval_res)
                if path_text == str(retention.R17_ROOT_A_PATH):
                    self.assertEqual(eval_res.root_class, retention.R17_CLASS_CONFIRMED_LOST)
                    self.assertFalse(eval_res.is_blocking)
                else:
                    self.assertEqual(eval_res.root_class, retention.R17_CLASS_OTHER_PROJECT_PROTECTED_ABSENT)
                    self.assertFalse(eval_res.is_blocking)

    def test_r17_all_historical_bindings_remain_queryable(self) -> None:
        for path_text, record in retention.R17_TEMP_ROOT_IDENTITIES.items():
            with self.subTest(path=path_text):
                self.assertIn("classification", record)
                self.assertIn("accounting_scope", record)
                self.assertIn("filesystem_identity", record)
                fs_id = record["filesystem_identity"]
                self.assertIn("device", fs_id)
                self.assertIn("inode", fs_id)
                self.assertIn("mode", fs_id)
                self.assertIn("owner_uid", fs_id)

    def test_r17_status_snapshot_unblocked_by_absent_external_protected_roots(self) -> None:
        with (mock.patch.object(retention, "_lease_files", return_value=[]),
              mock.patch.object(retention, "_all_root_candidates", return_value=[]),
              mock.patch.object(retention, "_verify_protected_evidence_roots", return_value=[])):
            state = retention.status_snapshot()
            self.assertEqual(state["gate_status"], "PASS")
            self.assertEqual(state["gate_blockers"], [])
            self.assertEqual(state["unknown"], [])
            r17_paths = set(retention.R17_TEMP_ROOT_IDENTITIES)
            unknown_paths = {row.get("path") for row in state["unknown"]}
            self.assertTrue(r17_paths.isdisjoint(unknown_paths))



class R63ParentContinuityTests(unittest.TestCase):
    """Regressions for R63 APFS parent identity continuity closure."""

    def setUp(self) -> None:
        self.live_identity = retention._root_identity(retention.BUILD_ROOT, create=False)
        self.sample_build_id = "legacy-fgmetal-step11d-build-067c124b"
        self.sample_lease = retention._load_lease(self.sample_build_id)

    def test_01_retired_legacy_lease_with_only_historical_st_dev_diff_accepted(self) -> None:
        """1. retired legacy lease + only historical st_dev difference: accepted under explicit compatibility rule."""
        self.assertNotEqual(self.sample_lease["approved_parent_identity"]["device"], self.live_identity["device"])
        result = retention._verify_completed_retirement(self.sample_lease)
        self.assertIsInstance(result, dict)
        self.assertEqual(result.get("build_id"), self.sample_build_id)

    def test_02_retired_legacy_lease_with_inode_mismatch_fails_closed(self) -> None:
        """2. retired legacy lease + inode mismatch: FAIL CLOSED."""
        tampered = copy.deepcopy(self.sample_lease)
        tampered["approved_parent_identity"]["inode"] = 99999999
        with self.assertRaises(retention.RetentionError) as ctx:
            retention._verify_completed_retirement(tampered)
        self.assertIn("inode", str(ctx.exception))

    def test_03_retired_legacy_lease_with_mode_mismatch_fails_closed(self) -> None:
        """3. retired legacy lease + mode mismatch: FAIL CLOSED."""
        tampered = copy.deepcopy(self.sample_lease)
        tampered["approved_parent_identity"]["mode"] = 0o777
        with self.assertRaises(retention.RetentionError) as ctx:
            retention._verify_completed_retirement(tampered)
        self.assertIn("mode", str(ctx.exception))

    def test_04_retired_legacy_lease_with_owner_mismatch_fails_closed(self) -> None:
        """4. retired legacy lease + owner mismatch: FAIL CLOSED."""
        tampered = copy.deepcopy(self.sample_lease)
        tampered["approved_parent_identity"]["owner_uid"] = 0
        with self.assertRaises(retention.RetentionError) as ctx:
            retention._verify_completed_retirement(tampered)
        self.assertIn("owner", str(ctx.exception))

    def test_05_retired_legacy_lease_canonical_path_substitution_fails_closed(self) -> None:
        """5. retired legacy lease + canonical-path substitution: FAIL CLOSED."""
        tampered = copy.deepcopy(self.sample_lease)
        tampered["approved_parent"] = "/private/tmp/other-parent"
        with self.assertRaises(retention.RetentionError) as ctx:
            retention._verify_completed_retirement(tampered)
        self.assertTrue("approved" in str(ctx.exception) or "canonical" in str(ctx.exception))

    def test_06_symlink_path_swap_fails_closed(self) -> None:
        """6. symlink/path swap: FAIL CLOSED."""
        with tempfile.TemporaryDirectory(prefix="fgmetal-symlink-test-", dir="/private/tmp") as temp_dir:
            temp_path = Path(temp_dir)
            fake_target = temp_path / "fake_builds"
            fake_target.mkdir(mode=0o700)
            symlink_root = temp_path / "symlink_builds"
            symlink_root.symlink_to(fake_target)
            with mock.patch.object(retention, "BUILD_ROOT", symlink_root):
                with self.assertRaises((retention.RetentionError, OSError)):
                    retention._verify_completed_retirement(self.sample_lease)

    def test_07_unsealed_or_invalid_retirement_receipt_fails_closed(self) -> None:
        """7. unsealed or invalid retirement receipt: FAIL CLOSED."""
        with tempfile.TemporaryDirectory(prefix="fgmetal-receipt-test-", dir="/private/tmp") as temp_dir:
            temp_receipt_root = Path(temp_dir)
            corrupt_receipt = temp_receipt_root / f"{self.sample_build_id}.json"
            corrupt_receipt.write_text(json.dumps({"tampered": True}), encoding="utf-8")
            with mock.patch.object(retention, "RECEIPT_ROOT", temp_receipt_root):
                tampered = copy.deepcopy(self.sample_lease)
                tampered["cleanup_receipt_path"] = str(corrupt_receipt)
                with self.assertRaises(retention.RetentionError):
                    retention._verify_completed_retirement(tampered)

    def test_08_active_lease_device_mismatch_fails_closed(self) -> None:
        """8. ACTIVE lease + device mismatch: FAIL CLOSED."""
        active_lease = copy.deepcopy(self.sample_lease)
        active_lease["state"] = "ACTIVE"
        active_lease["retirement_status"] = None
        active_lease["retired_at_unix"] = None
        with self.assertRaises(retention.RetentionError):
            retention._verify_completed_retired_parent_identity(self.live_identity, active_lease)

    def test_09_reserved_unstarted_lease_device_mismatch_fails_closed(self) -> None:
        """9. RESERVED/unstarted lease + device mismatch: FAIL CLOSED."""
        reserved_lease = copy.deepcopy(self.sample_lease)
        reserved_lease["state"] = "RESERVED"
        reserved_lease["retirement_status"] = None
        reserved_lease["retired_at_unix"] = None
        with self.assertRaises(retention.RetentionError):
            retention._verify_completed_retired_parent_identity(self.live_identity, reserved_lease)

    def test_10_current_live_buildlease_parent_device_checks_remain_strict(self) -> None:
        """10. current live BuildLease parent/device checks remain strict."""
        mismatched_identity = dict(self.live_identity)
        mismatched_identity["device"] = 9999999
        fd = retention._open_dir(retention.BUILD_ROOT)
        try:
            self.assertNotEqual(retention._fs_identity(fd), mismatched_identity)
        finally:
            os.close(fd)

    def test_11_compatibility_does_not_mutate_historical_receipt(self) -> None:
        """11. compatibility does not mutate historical receipt."""
        receipt_path = retention.RECEIPT_ROOT / f"{self.sample_build_id}.json"
        before_hash = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        retention._verify_completed_retirement(self.sample_lease)
        after_hash = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        self.assertEqual(before_hash, after_hash)

    def test_12_all_29_legacy_retired_leases_classify_correctly(self) -> None:
        """12. all 29 legacy retired leases classify correctly."""
        legacy_leases = []
        for lease_path in retention._lease_files():
            lease = retention._load_lease(lease_path.stem)
            parent_identity = lease.get("approved_parent_identity")
            if (lease.get("retired_at_unix") is not None
                    and lease.get("approved_parent") == str(retention.BUILD_ROOT)
                    and isinstance(parent_identity, dict)
                    and parent_identity.get("device") != self.live_identity.get("device")):
                legacy_leases.append(lease)
        self.assertEqual(len(legacy_leases), 29)

        historical_build_paths: set[str] = set()
        for lease in legacy_leases:
            result = retention._verify_completed_retirement(lease)
            self.assertEqual(result.get("build_id"), lease.get("build_id"))
            historical_build_paths.add(str(lease.get("path")))

        # The R63 assertion is scoped to terminal historical leases. Current
        # live reservations remain subject to their own strict identity checks.
        snapshot = retention.status_snapshot()
        unknown_paths = {row.get("path") for row in snapshot.get("unknown", [])}
        self.assertFalse(historical_build_paths & unknown_paths)

    def test_13_unrelated_unknown_lease_remains_blocking(self) -> None:
        """13. unrelated unknown lease remains blocking."""
        with tempfile.TemporaryDirectory(prefix="fgmetal-unknown-lease-", dir="/private/tmp") as temp_dir:
            temp_lease_root = Path(temp_dir)
            bad_lease_path = temp_lease_root / "bad-lease-12345678.json"
            bad_lease_path.write_text(json.dumps({"invalid_schema": True}), encoding="utf-8")
            with mock.patch.object(retention, "LEASE_ROOT", temp_lease_root):
                snapshot = retention.status_snapshot()
                self.assertEqual(snapshot.get("gate_status"), "BLOCKED")
                self.assertTrue(len(snapshot.get("unknown", [])) > 0 or len(snapshot.get("gate_blockers", [])) > 0)

    def test_14_r17_policy_tests_remain_green(self) -> None:
        """14. R17 policy tests remain green."""
        disposition = storage_inventory._read_r17_root_a_loss_disposition()
        self.assertIsInstance(disposition, dict)
        self.assertEqual(disposition.get("state"), "LOST_UNAUTHORIZED_HISTORICAL_EVIDENCE")


if __name__ == "__main__":
    unittest.main(verbosity=2)
