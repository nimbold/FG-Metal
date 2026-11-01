#!/usr/bin/env python3
"""Synthetic-only regression tests for BuildLease v3 image cleanup contracts."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

STORAGE_DIR = Path(__file__).resolve().parent
if str(STORAGE_DIR) not in sys.path:
    sys.path.insert(0, str(STORAGE_DIR))

import build_retention as retention
from test_buildlease_v3_host_adversarial import _cleanup_adversarial_fixture


class FixtureRoot:
    """Create ordinary files and directories only; never invoke disk utilities."""

    def __init__(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-r85-cleanup-contract-")
        self.root = Path(self.temp.name).resolve(strict=True)
        self.image = self.root / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        self.workspace = self.root / retention.BUILD_SESSION_WORKSPACE_NAME
        self.image.write_bytes(b"synthetic sparseimage bytes; not a disk image")
        self.workspace.mkdir(mode=0o500)
        self.workspace.chmod(0o500)
        image_info = self.image.stat(follow_symlinks=False)
        workspace_info = self.workspace.stat(follow_symlinks=False)
        self.identity = {
            "image_path": str(self.image),
            "image_device": image_info.st_dev,
            "image_inode": image_info.st_ino,
            "mountpoint": str(self.workspace),
            "mountpoint_dir_device": workspace_info.st_dev,
            "mountpoint_dir_inode": workspace_info.st_ino,
            "device_node": "/dev/disk-r85s1",
            "volume_uuid": "R85-SYNTHETIC-VOLUME-UUID",
            "filesystem_type": "hfs",
            "disk_device": "/dev/disk-r85",
            "hdiutil_path": "/synthetic/hdiutil",
            "hdiutil_sha256": "a" * 64,
            "diskutil_path": "/synthetic/diskutil",
            "diskutil_sha256": "b" * 64,
            "attached": True,
        }
        self.detached_identity = {**self.identity, "attached": False}

    def close(self) -> None:
        try:
            if self.root.exists():
                # Only remove this test's exact synthetic TemporaryDirectory.
                if self.root != Path(self.temp.name).resolve(strict=True) or self.root.is_symlink():
                    raise AssertionError("synthetic test root identity changed")
                shutil.rmtree(self.root)
        finally:
            self.temp.cleanup()


def _row(fixture: FixtureRoot, *, writable: bool = True, mounted: bool = True,
         whole_device: str = "/dev/disk-r85",
         slice_device: str = "/dev/disk-r85s1") -> dict:
    return {
        "image-path": str(fixture.image),
        "writeable": writable,
        "system-entities": [
            {"dev-entry": whole_device, "content-hint": "GUID_partition_scheme"},
            {"dev-entry": slice_device,
             "mount-point": str(fixture.workspace) if mounted else None},
        ],
    }


def _image_content_identity(fixture: FixtureRoot) -> dict:
    info = fixture.image.stat(follow_symlinks=False)
    return {"path": str(fixture.image), "device": info.st_dev, "inode": info.st_ino,
            "size_bytes": info.st_size, "sha256": "c" * 64}


class WritableCleanupContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = FixtureRoot()
        self.addCleanup(self.fixture.close)

    def _patch_post_unmount_inspection(self, fixture: FixtureRoot, *,
                                       row_value: dict | None = None,
                                       slice_info: dict | None = None,
                                       whole_info: dict | None = None):
        selected_row = row_value or _row(fixture, mounted=False)
        expected_slice_info = {
            "DeviceIdentifier": "disk-r85s1",
            "ParentWholeDisk": "disk-r85",
            "VolumeUUID": fixture.identity["volume_uuid"],
            "FilesystemType": "hfs",
            "Writable": True,
            "MountPoint": None,
        }
        expected_whole_info = {"DeviceIdentifier": "disk-r85", "Whole": True}
        return (
            mock.patch.object(retention, "_session_image_row", return_value=selected_row),
            mock.patch.object(retention, "_session_hdiutil",
                              return_value=(Path(fixture.identity["hdiutil_path"]),
                                            fixture.identity["hdiutil_sha256"])),
            mock.patch.object(retention, "_session_diskutil",
                              return_value=(Path(fixture.identity["diskutil_path"]),
                                            fixture.identity["diskutil_sha256"])),
            mock.patch.object(retention, "_session_diskutil_info",
                              side_effect=lambda device: (slice_info or expected_slice_info)
                              if device == fixture.identity["device_node"]
                              else (whole_info or expected_whole_info)),
            mock.patch.object(retention, "_session_image_content_identity",
                              return_value=_image_content_identity(fixture)),
        )

    def test_unmount_uses_exact_mountpoint_and_successful_identity_gate_allows_one_eject(self) -> None:
        fixture = self.fixture
        state = {"mounted": True, "attached": True}
        commands: list[list[str]] = []

        def row(_root: Path):
            if not state["attached"]:
                return None
            return _row(fixture, mounted=state["mounted"])

        def entity(_root: Path):
            if not state["mounted"]:
                return None
            return {"dev-entry": fixture.identity["device_node"],
                    "mount-point": str(fixture.workspace),
                    "image-path": str(fixture.image), "image-writeable": True}

        def run(command, *args, **kwargs):
            argv = [str(part) for part in command]
            commands.append(argv)
            if argv[1:] == ["unmount", str(fixture.workspace)]:
                state["mounted"] = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if argv[1:] == ["eject", fixture.identity["disk_device"]]:
                self.assertFalse(state["mounted"])
                state["attached"] = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            self.fail(f"unexpected synthetic disk utility command: {argv}")

        with (mock.patch.object(retention, "_verify_session_workspace_mount",
                                side_effect=lambda _root, identity: dict(identity)),
              mock.patch.object(retention, "_session_image_row", side_effect=row),
              mock.patch.object(retention, "_session_image_entity", side_effect=entity),
              mock.patch.object(retention, "_session_hdiutil",
                                return_value=(Path(fixture.identity["hdiutil_path"]),
                                              fixture.identity["hdiutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil",
                                return_value=(Path(fixture.identity["diskutil_path"]),
                                              fixture.identity["diskutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil_info", side_effect=lambda device: {
                  "DeviceIdentifier": "disk-r85s1" if device.endswith("s1") else "disk-r85",
                  "ParentWholeDisk": "disk-r85", "VolumeUUID": fixture.identity["volume_uuid"],
                  "FilesystemType": "hfs", "Writable": True, "MountPoint": None,
                  "Whole": device == fixture.identity["disk_device"],
              }),
              mock.patch.object(retention, "_session_image_content_identity",
                                return_value=_image_content_identity(fixture)),
              mock.patch.object(retention, "_verify_session_workspace_detached",
                                side_effect=lambda _root, identity, **_kwargs:
                                {**dict(identity), "attached": False}),
              mock.patch.object(retention.subprocess, "run", side_effect=run)):
            retention._detach_session_workspace(fixture.root, fixture.identity)

        self.assertEqual(commands, [
            [fixture.identity["diskutil_path"], "unmount", str(fixture.workspace)],
            [fixture.identity["diskutil_path"], "eject", fixture.identity["disk_device"]],
        ])

    def test_writable_eject_captures_finalized_content_as_detached_identity(self) -> None:
        fixture = self.fixture
        state = {"mounted": True, "attached": True}
        content_identities: list[dict] = []
        commands: list[list[str]] = []
        original_capture = retention._session_image_content_identity

        def row(_root: Path):
            if not state["attached"]:
                return None
            return _row(fixture, mounted=state["mounted"])

        def entity(_root: Path):
            if not state["mounted"]:
                return None
            return {"dev-entry": fixture.identity["device_node"],
                    "mount-point": str(fixture.workspace),
                    "image-path": str(fixture.image), "image-writeable": True}

        def capture(path: Path) -> dict:
            value = original_capture(path)
            content_identities.append(value)
            return value

        def run(command, *args, **kwargs):
            argv = [str(part) for part in command]
            commands.append(argv)
            if argv[1] == "unmount":
                state["mounted"] = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if argv[1] == "eject":
                # Model writable sparseimage metadata finalized by the eject transition.
                fixture.image.write_bytes(b"synthetic image bytes after successful eject")
                state["attached"] = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            self.fail(f"unexpected synthetic disk utility command: {argv}")

        with (mock.patch.object(retention, "_verify_session_workspace_mount",
                                side_effect=lambda _root, identity: dict(identity)),
              mock.patch.object(retention, "_session_image_row", side_effect=row),
              mock.patch.object(retention, "_session_image_entity", side_effect=entity),
              mock.patch.object(retention, "_session_hdiutil",
                                return_value=(Path(fixture.identity["hdiutil_path"]),
                                              fixture.identity["hdiutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil",
                                return_value=(Path(fixture.identity["diskutil_path"]),
                                              fixture.identity["diskutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil_info", side_effect=lambda device: {
                  "DeviceIdentifier": "disk-r85s1" if device.endswith("s1") else "disk-r85",
                  "ParentWholeDisk": "disk-r85", "VolumeUUID": fixture.identity["volume_uuid"],
                  "FilesystemType": "hfs", "Writable": True, "MountPoint": None,
                  "Whole": device == fixture.identity["disk_device"],
              }),
              mock.patch.object(retention, "_session_image_content_identity", side_effect=capture),
              mock.patch.object(retention.subprocess, "run", side_effect=run)):
            detached = retention._detach_session_workspace(fixture.root, fixture.identity)

        self.assertEqual(commands, [
            [fixture.identity["diskutil_path"], "unmount", str(fixture.workspace)],
            [fixture.identity["diskutil_path"], "eject", fixture.identity["disk_device"]],
        ])
        self.assertEqual(len(content_identities), 2)
        self.assertNotEqual(content_identities[0]["sha256"], content_identities[1]["sha256"])
        self.assertEqual(detached["detached_image_identity"], content_identities[1])

    def test_image_content_hash_rejects_path_substitution_during_read(self) -> None:
        fixture = self.fixture
        replacement = fixture.root / "replacement-image.sparseimage"
        original_hash_fd = retention._hash_fd

        def replace_during_hash(fd: int) -> str:
            replacement.write_bytes(b"substituted image bytes")
            os.replace(replacement, fixture.image)
            return original_hash_fd(fd)

        with mock.patch.object(retention, "_hash_fd", side_effect=replace_during_hash):
            with self.assertRaisesRegex(retention.RetentionError, "changed while hashing"):
                retention._session_image_content_identity(fixture.image)

    def test_unmount_failure_stops_before_eject(self) -> None:
        fixture = self.fixture
        commands: list[list[str]] = []

        def run(command, *args, **kwargs):
            commands.append([str(part) for part in command])
            return subprocess.CompletedProcess(command, 1, b"", b"injected unmount failure")

        with (mock.patch.object(retention, "_verify_session_workspace_mount",
                                side_effect=lambda _root, identity: dict(identity)),
              mock.patch.object(retention, "_session_image_row", return_value=_row(fixture)),
              mock.patch.object(retention, "_session_image_entity", return_value={
                  "dev-entry": fixture.identity["device_node"], "mount-point": str(fixture.workspace),
                  "image-path": str(fixture.image), "image-writeable": True}),
              mock.patch.object(retention, "_session_hdiutil",
                                return_value=(Path(fixture.identity["hdiutil_path"]),
                                              fixture.identity["hdiutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil",
                                return_value=(Path(fixture.identity["diskutil_path"]),
                                              fixture.identity["diskutil_sha256"])),
              mock.patch.object(retention.subprocess, "run", side_effect=run)):
            with self.assertRaisesRegex(retention.RetentionError, "normal unmount failed"):
                retention._detach_session_workspace(fixture.root, fixture.identity)
        self.assertEqual(commands, [[fixture.identity["diskutil_path"], "unmount", str(fixture.workspace)]])

    def test_post_unmount_identity_mismatches_fail_closed(self) -> None:
        fixture = self.fixture
        cases = [
            ("wrong whole disk", _row(fixture, mounted=False,
                                       whole_device="/dev/disk-substitute"), None, None),
            ("wrong slice lineage", _row(fixture, mounted=False,
                                          slice_device="/dev/disk-other1"), None, None),
            ("wrong image mapping absent", {"writeable": True, "system-entities": []}, None, None),
            ("wrong UUID", _row(fixture, mounted=False), {
                "DeviceIdentifier": "disk-r85s1", "ParentWholeDisk": "disk-r85",
                "VolumeUUID": "OTHER-UUID", "FilesystemType": "hfs", "Writable": True,
                "MountPoint": None}, None),
        ]
        for label, row_value, slice_info, whole_info in cases:
            with self.subTest(identity_failure=label):
                patches = self._patch_post_unmount_inspection(
                    fixture, row_value=row_value, slice_info=slice_info, whole_info=whole_info)
                with ExitStack() as stack:
                    for patcher in patches:
                        stack.enter_context(patcher)
                    with self.assertRaises(retention.RetentionError):
                        retention._verify_session_workspace_unmounted_for_eject(
                            fixture.root, fixture.identity)

    def test_backing_image_substitution_fails_closed(self) -> None:
        fixture = self.fixture
        moved = fixture.root / "original-image"
        fixture.image.rename(moved)
        fixture.image.write_bytes(b"substitute backing image")
        patches = self._patch_post_unmount_inspection(fixture)
        with ExitStack() as stack:
            for patcher in patches:
                stack.enter_context(patcher)
            with self.assertRaisesRegex(retention.RetentionError, "backing image"):
                retention._verify_session_workspace_unmounted_for_eject(
                    fixture.root, fixture.identity)

    def test_image_row_resolver_and_post_unmount_gate_reject_substitute_backing_path(self) -> None:
        fixture = self.fixture
        substitute = fixture.root / "different-existing-image.sparseimage"
        substitute.write_bytes(b"not the expected fixture backing image")
        foreign_row = {
            "image-path": str(substitute),
            "writeable": True,
            "system-entities": [
                {"dev-entry": fixture.identity["disk_device"],
                 "content-hint": "GUID_partition_scheme"},
                {"dev-entry": fixture.identity["device_node"], "mount-point": None},
            ],
        }
        with mock.patch.object(retention, "_session_hdiutil_info", return_value=[foreign_row]):
            self.assertIsNone(retention._session_image_row(fixture.root))
            with self.assertRaisesRegex(retention.RetentionError, "image mapping is absent"):
                retention._verify_session_workspace_unmounted_for_eject(
                    fixture.root, fixture.identity)

    def test_missing_backing_path_fails_before_disk_mutation_and_preserves_root(self) -> None:
        fixture = self.fixture
        fixture.image.unlink()
        with (mock.patch.object(retention, "_session_image_row", return_value=None),
              mock.patch.object(retention.subprocess, "run") as run):
            with self.assertRaises(FileNotFoundError):
                retention._detach_session_workspace(fixture.root, fixture.identity)
        run.assert_not_called()
        self.assertTrue(fixture.root.is_dir())
        self.assertTrue(fixture.workspace.is_dir())
        self.assertFalse(fixture.image.exists())

    def test_eject_failure_propagates(self) -> None:
        fixture = self.fixture
        state = {"mounted": True, "attached": True}

        def row(_root: Path):
            if not state["attached"]:
                return None
            return _row(fixture, mounted=state["mounted"])

        def run(command, *args, **kwargs):
            argv = [str(part) for part in command]
            if argv[1] == "unmount":
                state["mounted"] = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if argv[1] == "eject":
                return subprocess.CompletedProcess(command, 1, b"", b"injected eject failure")
            self.fail(f"unexpected synthetic disk utility command: {argv}")

        with (mock.patch.object(retention, "_verify_session_workspace_mount",
                                side_effect=lambda _root, identity: dict(identity)),
              mock.patch.object(retention, "_session_image_row", side_effect=row),
              mock.patch.object(retention, "_session_image_entity", side_effect=lambda _root: None if not state["mounted"] else {
                  "dev-entry": fixture.identity["device_node"], "mount-point": str(fixture.workspace),
                  "image-path": str(fixture.image), "image-writeable": True}),
              mock.patch.object(retention, "_session_hdiutil",
                                return_value=(Path(fixture.identity["hdiutil_path"]),
                                              fixture.identity["hdiutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil",
                                return_value=(Path(fixture.identity["diskutil_path"]),
                                              fixture.identity["diskutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil_info", side_effect=lambda device: {
                  "DeviceIdentifier": "disk-r85s1" if device.endswith("s1") else "disk-r85",
                  "ParentWholeDisk": "disk-r85", "VolumeUUID": fixture.identity["volume_uuid"],
                  "FilesystemType": "hfs", "Writable": True, "MountPoint": None,
                  "Whole": device == fixture.identity["disk_device"],
              }),
              mock.patch.object(retention, "_session_image_content_identity",
                                return_value=_image_content_identity(fixture)),
              mock.patch.object(retention.subprocess, "run", side_effect=run)):
            with self.assertRaisesRegex(retention.RetentionError, "normal detach failed"):
                retention._detach_session_workspace(fixture.root, fixture.identity)

    def test_successful_eject_command_with_remaining_image_is_failure(self) -> None:
        fixture = self.fixture
        state = {"mounted": True, "attached": True}

        def row(_root: Path):
            if not state["attached"]:
                return None
            return _row(fixture, mounted=state["mounted"])

        def run(command, *args, **kwargs):
            argv = [str(part) for part in command]
            if argv[1] == "unmount":
                state["mounted"] = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if argv[1] == "eject":
                # Simulate a successful command whose claimed attachment remains.
                return subprocess.CompletedProcess(command, 0, b"", b"")
            self.fail(f"unexpected synthetic disk utility command: {argv}")

        with (mock.patch.object(retention, "_verify_session_workspace_mount",
                                side_effect=lambda _root, identity: dict(identity)),
              mock.patch.object(retention, "_session_image_row",
                                side_effect=row),
              mock.patch.object(retention, "_session_image_entity",
                                side_effect=lambda _root: None if not state["mounted"] else {
                                    "dev-entry": fixture.identity["device_node"],
                                    "mount-point": str(fixture.workspace),
                                    "image-path": str(fixture.image), "image-writeable": True}),
              mock.patch.object(retention, "_session_hdiutil",
                                return_value=(Path(fixture.identity["hdiutil_path"]),
                                              fixture.identity["hdiutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil",
                                return_value=(Path(fixture.identity["diskutil_path"]),
                                              fixture.identity["diskutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil_info", side_effect=lambda device: {
                  "DeviceIdentifier": "disk-r85s1" if device.endswith("s1") else "disk-r85",
                  "ParentWholeDisk": "disk-r85", "VolumeUUID": fixture.identity["volume_uuid"],
                  "FilesystemType": "hfs", "Writable": True, "MountPoint": None,
                  "Whole": device == fixture.identity["disk_device"],
              }),
              mock.patch.object(retention, "_session_image_content_identity",
                                return_value=_image_content_identity(fixture)),
              mock.patch.object(retention.subprocess, "run", side_effect=run)):
            with self.assertRaisesRegex(retention.RetentionError, "image remains attached"):
                retention._detach_session_workspace(fixture.root, fixture.identity)

    def test_partial_attach_with_cleanup_failure_preserves_root_and_both_errors(self) -> None:
        fixture = self.fixture
        fixture.image.unlink()
        fixture.workspace.chmod(0o700)
        fixture.workspace.rmdir()
        state = {"attached": False, "mounted": False}
        commands: list[list[str]] = []
        attach_error_text = "could not attach the fixed-capacity BuildLease workspace"

        def row(_root: Path):
            if not state["attached"]:
                return None
            return _row(fixture, mounted=state["mounted"])

        def entity(_root: Path):
            if not state["mounted"]:
                return None
            return {"dev-entry": fixture.identity["device_node"],
                    "mount-point": str(fixture.workspace), "image-path": str(fixture.image),
                    "image-writeable": True}

        def run(command, *args, **kwargs):
            argv = [str(part) for part in command]
            commands.append(argv)
            if argv[1] == "create":
                fixture.image.write_bytes(b"synthetic created image")
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if argv[1] == "attach":
                state.update(attached=True, mounted=True)
                return subprocess.CompletedProcess(command, 1, b"", b"partial attachment")
            if argv[1:] == ["unmount", str(fixture.workspace)]:
                state["mounted"] = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if argv[1:] == ["eject", fixture.identity["disk_device"]]:
                return subprocess.CompletedProcess(command, 1, b"", b"injected eject cleanup failure")
            self.fail(f"unexpected synthetic setup command: {argv}")

        def capture_cleanup_identity(_root: Path, _placeholder_info) -> dict:
            current = fixture.image.stat(follow_symlinks=False)
            return {**fixture.identity, "image_device": current.st_dev, "image_inode": current.st_ino}

        with (mock.patch.object(retention, "_session_hdiutil",
                                return_value=(Path(fixture.identity["hdiutil_path"]),
                                              fixture.identity["hdiutil_sha256"])),
              mock.patch.object(retention, "_session_diskutil",
                                return_value=(Path(fixture.identity["diskutil_path"]),
                                              fixture.identity["diskutil_sha256"])),
              mock.patch.object(retention, "_session_image_row", side_effect=row),
              mock.patch.object(retention, "_session_image_entity", side_effect=entity),
              mock.patch.object(retention, "_session_image_device",
                                return_value=fixture.identity["disk_device"]),
              mock.patch.object(retention, "_capture_session_workspace_cleanup_identity",
                                side_effect=capture_cleanup_identity),
              mock.patch.object(retention, "_session_diskutil_info", side_effect=lambda device: {
                  "DeviceIdentifier": "disk-r85s1" if device.endswith("s1") else "disk-r85",
                  "ParentWholeDisk": "disk-r85", "VolumeUUID": fixture.identity["volume_uuid"],
                  "FilesystemType": "hfs", "Writable": True,
                  "MountPoint": str(fixture.workspace) if state["mounted"] else None,
                  "Whole": device == fixture.identity["disk_device"],
              }),
              mock.patch.object(retention, "_verify_session_workspace_unmounted_for_eject",
                                side_effect=lambda _root, identity: dict(identity)),
              mock.patch.object(retention.subprocess, "run", side_effect=run)):
            with self.assertRaisesRegex(retention.RetentionError,
                                        "workspace attach failed and normal cleanup failed") as caught:
                retention._attach_session_workspace(fixture.root, 32 * 1024 * 1024)

        self.assertIsInstance(caught.exception.__cause__, retention.RetentionError)
        self.assertIn(attach_error_text, str(caught.exception.__cause__))
        self.assertIn("normal cleanup detach failed", str(caught.exception))
        self.assertTrue(fixture.root.exists(), "cleanup failure must preserve the backing root")
        self.assertTrue(fixture.image.exists(), "cleanup failure must preserve backing image evidence")
        self.assertEqual(commands[-2:], [
            [fixture.identity["diskutil_path"], "unmount", str(fixture.workspace)],
            [fixture.identity["diskutil_path"], "eject", fixture.identity["disk_device"]],
        ])

    def _run_unmounted_partial_attach(self, *, wrong_parent: bool = False) -> tuple[FixtureRoot, list[list[str]], dict]:
        fixture = FixtureRoot()
        fixture.image.unlink()
        fixture.workspace.chmod(0o700)
        fixture.workspace.rmdir()
        state = {"attached": False}
        commands: list[list[str]] = []
        row = {
            "image-path": str(fixture.image),
            "writeable": True,
            "system-entities": [
                {"dev-entry": fixture.identity["disk_device"],
                 "content-hint": "GUID_partition_scheme"},
                {"dev-entry": fixture.identity["device_node"], "mount-point": None},
            ],
        }

        def fake_run(command, *args, **kwargs):
            argv = [str(item) for item in command]
            commands.append(argv)
            if argv[1] == "create":
                fixture.image.write_bytes(b"synthetic attached but unmounted image")
                return subprocess.CompletedProcess(command, 0, b"", b"")
            if argv[1] == "attach":
                state["attached"] = True
                # Attach failed after creating an exact writable whole-image row,
                # but no mounted volume entity exists.
                return subprocess.CompletedProcess(command, 1, b"", b"partial unmounted attach")
            if argv[1:] == ["eject", fixture.identity["disk_device"]]:
                state["attached"] = False
                return subprocess.CompletedProcess(command, 0, b"", b"")
            self.fail(f"unexpected command for unmounted partial attach: {argv}")

        def image_row(_root: Path):
            return row if state["attached"] else None

        def disk_info(device: str) -> dict:
            is_slice = device == fixture.identity["device_node"]
            return {
                "DeviceIdentifier": Path(device).name,
                "ParentWholeDisk": "disk-substitute" if wrong_parent
                else Path(fixture.identity["disk_device"]).name,
                "VolumeUUID": "R85-SYNTHETIC-VOLUME-UUID",
                "FilesystemType": "hfs",
                "Writable": True,
                "MountPoint": None,
                "Whole": not is_slice,
            }

        try:
            with (mock.patch.object(retention, "_session_hdiutil",
                                    return_value=(Path(fixture.identity["hdiutil_path"]),
                                                  fixture.identity["hdiutil_sha256"])),
                  mock.patch.object(retention, "_session_diskutil",
                                    return_value=(Path(fixture.identity["diskutil_path"]),
                                                  fixture.identity["diskutil_sha256"])),
                  mock.patch.object(retention, "_session_image_row", side_effect=image_row),
                  mock.patch.object(retention, "_session_image_entity", return_value=None),
                  mock.patch.object(retention, "_session_image_device",
                                    return_value=fixture.identity["disk_device"]),
                  mock.patch.object(retention, "_session_diskutil_info", side_effect=disk_info),
                  mock.patch.object(retention.subprocess, "run", side_effect=fake_run)):
                try:
                    result = retention._attach_session_workspace(fixture.root, 32 * 1024 * 1024)
                except BaseException as error:
                    outcome = {"error": error, "root_exists": fixture.root.exists(),
                               "image_exists": fixture.image.exists(), "attached": state["attached"]}
                else:
                    outcome = {"result": result, "root_exists": fixture.root.exists(),
                               "image_exists": fixture.image.exists(), "attached": state["attached"]}
            return fixture, commands, outcome
        except BaseException:
            fixture.close()
            raise

    def test_unmounted_partial_attach_ejects_once_without_unmount(self) -> None:
        fixture, commands, outcome = self._run_unmounted_partial_attach(
            wrong_parent=False)
        self.addCleanup(fixture.close)
        self.assertIn("error", outcome)
        self.assertRegex(str(outcome["error"]), "could not attach")
        self.assertEqual(commands, [
            [fixture.identity["hdiutil_path"], "create", "-quiet", "-size", "65536b",
             "-fs", "HFS+", "-type", "SPARSE", "-volname", "FGMetalBuildWorkspace",
             str(fixture.image)],
            [fixture.identity["hdiutil_path"], "attach", "-quiet", "-nobrowse", "-noautoopen",
             "-mountpoint", str(fixture.workspace), str(fixture.image)],
            [fixture.identity["diskutil_path"], "eject", fixture.identity["disk_device"]],
        ])
        self.assertTrue(outcome["root_exists"])
        self.assertTrue(outcome["image_exists"])
        self.assertFalse(outcome["attached"], "normal eject and detached verification must finish")

    def test_unmounted_partial_identity_mismatch_refuses_eject_and_preserves_evidence(self) -> None:
        fixture, commands, outcome = self._run_unmounted_partial_attach(
            wrong_parent=True)
        self.addCleanup(fixture.close)
        self.assertIn("error", outcome)
        self.assertIn("workspace attach failed and normal cleanup failed", str(outcome["error"]))
        self.assertIn("partial BuildLease volume slice or UUID lineage cannot be established",
                      str(outcome["error"]))
        self.assertIn("could not attach", str(outcome["error"].__cause__))
        self.assertEqual(commands[-1][1], "attach")
        self.assertFalse(any(command[1] == "unmount" for command in commands))
        self.assertFalse(any(command[1] == "eject" for command in commands))
        self.assertTrue(outcome["root_exists"])
        self.assertTrue(outcome["image_exists"])
        self.assertTrue(outcome["attached"])


class ReadonlyCleanupContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = FixtureRoot()
        self.fixture.detached_identity["detached_image_identity"] = {"synthetic": True}
        self.addCleanup(self.fixture.close)

    def _patch_tool(self):
        return mock.patch.object(retention, "_session_hdiutil",
                                 return_value=(Path(self.fixture.identity["hdiutil_path"]),
                                               self.fixture.identity["hdiutil_sha256"]))

    def _mounted_entity(self, *, image_path: str | None = None,
                        writable: bool = False) -> dict:
        return {"dev-entry": self.fixture.identity["device_node"],
                "mount-point": str(self.fixture.workspace),
                "image-path": image_path or str(self.fixture.image),
                "image-writeable": writable}

    def test_successful_normal_detach_is_checked_and_verified(self) -> None:
        fixture = self.fixture
        attached = {"value": True}
        commands: list[list[str]] = []

        def row(_root: Path):
            return _row(fixture, writable=False) if attached["value"] else None

        def run(command, *args, **kwargs):
            argv = [str(part) for part in command]
            commands.append(argv)
            attached["value"] = False
            return subprocess.CompletedProcess(command, 0, b"", b"")

        with (self._patch_tool(),
              mock.patch.object(retention, "_session_image_row", side_effect=row),
              mock.patch.object(retention, "_session_image_device", return_value=fixture.identity["disk_device"]),
              mock.patch.object(retention, "_session_image_entity", return_value=self._mounted_entity()),
              mock.patch.object(retention, "_session_diskutil_info", return_value={
                  "Writable": False, "VolumeUUID": fixture.identity["volume_uuid"],
                  "FilesystemType": fixture.identity["filesystem_type"],
                  "MountPoint": str(fixture.workspace)}),
              mock.patch.object(retention, "_verify_session_workspace_detached", return_value={"attached": False}),
              mock.patch.object(retention.subprocess, "run", side_effect=run)):
            retention._best_effort_detach_unverified_readonly_image(fixture.root,
                                                                    fixture.detached_identity)
        self.assertEqual(commands, [[fixture.identity["hdiutil_path"], "detach", "-quiet",
                                     fixture.identity["disk_device"]]])

    def test_detach_failure_propagates_and_image_remains_attached_is_failure(self) -> None:
        fixture = self.fixture
        with (self._patch_tool(),
              mock.patch.object(retention, "_session_image_row", return_value=_row(fixture, writable=False)),
              mock.patch.object(retention, "_session_image_device", return_value=fixture.identity["disk_device"]),
              mock.patch.object(retention, "_session_image_entity", return_value=self._mounted_entity()),
              mock.patch.object(retention, "_session_diskutil_info", return_value={
                  "Writable": False, "VolumeUUID": fixture.identity["volume_uuid"],
                  "FilesystemType": fixture.identity["filesystem_type"],
                  "MountPoint": str(fixture.workspace)}),
              mock.patch.object(retention.subprocess, "run", return_value=subprocess.CompletedProcess(
                  ["hdiutil", "detach"], 1, b"", b"busy"))):
            with self.assertRaisesRegex(retention.RetentionError, "detach failed"):
                retention._best_effort_detach_unverified_readonly_image(fixture.root,
                                                                        fixture.detached_identity)

        calls = iter([_row(fixture, writable=False), _row(fixture, writable=False)])
        with (self._patch_tool(),
              mock.patch.object(retention, "_session_image_row", side_effect=lambda _root: next(calls)),
              mock.patch.object(retention, "_session_image_device", return_value=fixture.identity["disk_device"]),
              mock.patch.object(retention, "_session_image_entity", return_value=self._mounted_entity()),
              mock.patch.object(retention, "_session_diskutil_info", return_value={
                  "Writable": False, "VolumeUUID": fixture.identity["volume_uuid"],
                  "FilesystemType": fixture.identity["filesystem_type"],
                  "MountPoint": str(fixture.workspace)}),
              mock.patch.object(retention, "_verify_session_workspace_detached", return_value={"attached": False}),
              mock.patch.object(retention.subprocess, "run", return_value=subprocess.CompletedProcess(
                  ["hdiutil", "detach"], 0, b"", b""))):
            with self.assertRaisesRegex(retention.RetentionError, "remains attached"):
                retention._best_effort_detach_unverified_readonly_image(fixture.root,
                                                                        fixture.detached_identity)

    def test_missing_or_substituted_identity_refuses_detach(self) -> None:
        fixture = self.fixture
        invalid = [
            ("missing identity", fixture.detached_identity, None, None),
            ("writable image", fixture.detached_identity, _row(fixture, writable=True), None),
            ("wrong image mapping", fixture.detached_identity, _row(fixture, writable=False),
             self._mounted_entity(image_path="/tmp/other.sparseimage")),
            ("wrong UUID", fixture.detached_identity, _row(fixture, writable=False),
             self._mounted_entity()),
        ]
        for label, detached_identity, row_value, entity_value in invalid:
            with self.subTest(identity_failure=label):
                with (self._patch_tool(),
                      mock.patch.object(retention, "_session_image_row",
                                        return_value=row_value or _row(fixture, writable=False)),
                      mock.patch.object(retention, "_session_image_device",
                                        return_value=fixture.identity["disk_device"]),
                      mock.patch.object(retention, "_session_image_entity",
                                        return_value=entity_value or self._mounted_entity()),
                      mock.patch.object(retention, "_session_diskutil_info",
                                        return_value={
                                            "Writable": False, "VolumeUUID": fixture.identity["volume_uuid"],
                                            "FilesystemType": "hfs", "MountPoint": str(fixture.workspace)}),
                      mock.patch.object(retention.subprocess, "run") as run):
                    if label == "missing identity":
                        with self.assertRaises(retention.RetentionError):
                            retention._best_effort_detach_unverified_readonly_image(fixture.root, {})
                    elif label == "wrong UUID":
                        with mock.patch.object(retention, "_session_diskutil_info", return_value={
                                "Writable": False, "VolumeUUID": "OTHER-UUID",
                                "FilesystemType": "hfs", "MountPoint": str(fixture.workspace)}):
                            with self.assertRaises(retention.RetentionError):
                                retention._best_effort_detach_unverified_readonly_image(
                                    fixture.root, detached_identity)
                    else:
                        with self.assertRaises(retention.RetentionError):
                            retention._best_effort_detach_unverified_readonly_image(
                                fixture.root, detached_identity)
                    run.assert_not_called()

    def test_attach_error_and_cleanup_error_are_both_observable(self) -> None:
        fixture = self.fixture
        setup_error = retention.RetentionError("synthetic read-only attach verification failed")
        cleanup_error = retention.RetentionError("synthetic exact cleanup could not detach")
        with (mock.patch.object(retention, "_attach_session_workspace_readonly_impl",
                                side_effect=setup_error),
              mock.patch.object(retention, "_best_effort_detach_unverified_readonly_image",
                                side_effect=cleanup_error)):
            with self.assertRaisesRegex(retention.RetentionError, "setup error:.*cleanup error") as caught:
                retention._attach_session_workspace_readonly(fixture.root,
                                                             fixture.detached_identity)
        self.assertIs(caught.exception.__cause__, setup_error)
        self.assertIn(str(setup_error), str(caught.exception))
        self.assertIn(str(cleanup_error), str(caught.exception))

    def test_attach_error_survives_when_cleanup_succeeds(self) -> None:
        fixture = self.fixture
        setup_error = retention.RetentionError("synthetic read-only attach failed")
        with (mock.patch.object(retention, "_attach_session_workspace_readonly_impl",
                                side_effect=setup_error),
              mock.patch.object(retention, "_best_effort_detach_unverified_readonly_image") as cleanup):
            with self.assertRaisesRegex(retention.RetentionError, "synthetic read-only attach failed") as caught:
                retention._attach_session_workspace_readonly(fixture.root,
                                                             fixture.detached_identity)
        self.assertIs(caught.exception, setup_error)
        cleanup.assert_called_once_with(fixture.root, fixture.detached_identity)


class TemporaryRootPreservationTests(unittest.TestCase):
    def test_temporary_root_is_preserved_after_fixture_cleanup_failure(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-r85-preserve-root-")
        root = Path(temp.name).resolve(strict=True)
        image = root / "synthetic-image.sparseimage"
        image.write_bytes(b"evidence stays on cleanup error")
        try:
            with mock.patch.object(retention, "_detach_session_workspace",
                                   side_effect=retention.RetentionError("synthetic detach failure")):
                with self.assertRaisesRegex(retention.RetentionError, "synthetic detach failure"):
                    _cleanup_adversarial_fixture(temp, [(root, {"attached": True})])
            self.assertTrue(root.is_dir())
            self.assertEqual(image.read_bytes(), b"evidence stays on cleanup error")
        finally:
            # The helper intentionally detached TemporaryDirectory's finalizer
            # on cleanup failure; clean only this test's private root ourselves.
            if root.exists():
                shutil.rmtree(root)
            temp.cleanup()


if __name__ == "__main__":
    unittest.main()
