#!/usr/bin/env python3
"""Synthetic PrefixLease cleanup-policy tests; these tests never launch Wine."""
from __future__ import annotations

import tempfile
import unittest
import datetime
import copy
import json
import hashlib
import hmac
import os
import plistlib
import shutil
import signal
import stat
import struct
import subprocess
import sys
import threading
import time
import uuid
import zlib
from types import SimpleNamespace
from pathlib import Path
from typing import Any, Mapping, Sequence
from contextlib import ExitStack
from unittest import mock

import storage_policy as policy
import storage_gate0 as gate0
import build_retention
import smoke_prefix_lease


def _projected_without_vm_commitment(**inputs):
    empty_commitment = {"committed_growth_bytes": 0, "marker_sha256": None,
                        "marker_identity": None}
    with mock.patch.object(policy, "_vm_capacity_commitment_snapshot",
                           return_value=empty_commitment):
        return policy.projected_storage_admission(**inputs)


def _post_materialization_without_vm_commitment(**inputs):
    empty_commitment = {"committed_growth_bytes": 0, "marker_sha256": None,
                        "marker_identity": None}
    with mock.patch.object(policy, "_vm_capacity_commitment_snapshot",
                           return_value=empty_commitment):
        return policy._post_materialization_prefix_admission(**inputs)


class R76DurableIdentityRouteTests(unittest.TestCase):
    def test_r76_claim_is_paired_one_use_and_path_safe(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary) / "claims"
            lock_path = Path(temporary) / "allocation.lock"
            lock_path.write_bytes(b"")
            lock = lock_path.open("a+")
            self.addCleanup(lock.close)
            with mock.patch.object(policy, "CACHE", Path(temporary) / "cache"):
                claim = policy._claim_r66_r69_admission_id(
                    "r6a-r76-admission-route-test", "a" * 64, "b" * 64, "ALLOW",
                    claims_root=root, build_id="r6a-r76-dual-lineage-route-test",
                    allocation_lock=lock)
                record = json.loads(Path(claim["claim_path"]).read_bytes())
                self.assertEqual(record["milestone"], "R76")
                self.assertEqual(record["build_id"], "r6a-r76-dual-lineage-route-test")
                with self.assertRaisesRegex(RuntimeError, "already been claimed"):
                    policy._claim_r66_r69_admission_id(
                        "r6a-r76-admission-route-test", "c" * 64, "b" * 64, "ALLOW",
                        claims_root=root, build_id="r6a-r76-dual-lineage-replay-build",
                        allocation_lock=lock)
                with self.assertRaisesRegex(RuntimeError, "already been consumed"):
                    policy._claim_r66_r69_admission_id(
                        "r6a-r76-admission-route-test", "c" * 64, "b" * 64, "ALLOW",
                        claims_root=root, build_id="r6a-r76-dual-lineage-route-test",
                        allocation_lock=lock)
                with self.assertRaisesRegex(RuntimeError, "already been consumed"):
                    policy._claim_r66_r69_admission_id(
                        "r6a-r76-admission-second", "c" * 64, "d" * 64, "ALLOW",
                        claims_root=root, build_id="r6a-r76-dual-lineage-route-test",
                        allocation_lock=lock)

                with self.assertRaisesRegex(RuntimeError, "inputs are malformed"):
                    policy._claim_r66_r69_admission_id(
                        "r6a-r76-admission-cross", "e" * 64, "f" * 64, "ALLOW",
                        claims_root=root, build_id="r6a-r77-dual-lineage-cross",
                        allocation_lock=lock)
                for unsafe_admission, unsafe_build in (
                    ("r6a-r76-admission-../escape", "r6a-r76-dual-lineage-safe"),
                    ("r6a-r76-admission-safe", "r6a-r76-dual-lineage-../escape"),
                    ("r6a-r076-admission-leading-zero", "r6a-r076-dual-lineage-leading-zero"),
                ):
                    with self.subTest(admission=unsafe_admission, build=unsafe_build):
                        with self.assertRaisesRegex(RuntimeError, "inputs are malformed"):
                            policy._claim_r66_r69_admission_id(
                                unsafe_admission, "1" * 64, "2" * 64, "ALLOW",
                                claims_root=root, build_id=unsafe_build,
                                allocation_lock=lock)

    def test_r100_claim_consumes_build_id_for_generic_replay_check(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary) / "claims"
            lock_path = Path(temporary) / "allocation.lock"
            lock_path.write_bytes(b"")
            lock = lock_path.open("a+")
            self.addCleanup(lock.close)
            with mock.patch.object(policy, "CACHE", Path(temporary) / "cache"):
                build_id = "r6a-r100-dual-lineage-future-test"
                policy._claim_r66_r69_admission_id(
                    "r6a-r100-admission-future-test", "3" * 64, "4" * 64, "ALLOW",
                    claims_root=root, build_id=build_id, allocation_lock=lock)
                with self.assertRaisesRegex(RuntimeError, "already been consumed"):
                    policy._assert_r70_build_id_unclaimed(build_id, claims_root=root)


class R73ProjectScopeBoundaryMatrixTests(unittest.TestCase):
    def test_current_60_gib_policy_matrix_and_warning_boundary(self) -> None:
        candidate_like = 56 * policy.GIB + 65 * policy.GIB // 100
        cases = (
            ("51 GiB", 51 * policy.GIB, True, False),
            ("exactly 52 GiB", 52 * policy.GIB, True, False),
            ("52 GiB plus one byte", 52 * policy.GIB + 1, True, True),
            ("55 GiB", 55 * policy.GIB, True, True),
            ("candidate-like 56.65 GiB", candidate_like, True, True),
            ("59 GiB", 59 * policy.GIB, True, True),
            ("exactly 60 GiB", 60 * policy.GIB, True, True),
            ("60 GiB plus one byte", 60 * policy.GIB + 1, False, True),
        )
        for label, projected_scope, admitted, warning in cases:
            with self.subTest(boundary=label):
                result = _projected_without_vm_commitment(
                    current_free_bytes=100 * policy.GIB,
                    current_scoped_bytes=projected_scope,
                    peak_additional_bytes=0,
                )
                self.assertEqual(result["projected_scoped_bytes"], projected_scope)
                self.assertEqual(result["admitted"], admitted)
                self.assertEqual(
                    any("52 GiB warning threshold" in item for item in result["warnings"]),
                    warning,
                )
                if projected_scope == 60 * policy.GIB + 1:
                    self.assertIn(
                        "projected scoped FG-Metal usage exceeds the 60 GiB ceiling",
                        result["blockers"],
                    )
                else:
                    self.assertNotIn(
                        "projected scoped FG-Metal usage exceeds the 60 GiB ceiling",
                        result["blockers"],
                    )


class R70AdmissionTreeStateTests(unittest.TestCase):
    def test_handoff_capability_is_pid_bound_one_shot_and_exact_identity_bound(self):
        state = {"schema_version": 1, "sha256": "a" * 64, "entries": []}
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            lock_path = Path(temporary) / "lock"
            lock_path.write_bytes(b"")
            lock_file = lock_path.open("a+")
            self.addCleanup(lock_file.close)
            cap = policy.AdmissionReservationCapability(
                policy._R70_ADMISSION_CAPABILITY_TOKEN, lock_file=lock_file,
                gate_sha256="1" * 64, admission_id="r6a-r70-admission-x",
                build_id="r6a-r70-dual-lineage-x", plan_sha256="2" * 64,
                manifest_path=Path(temporary) / "manifest.json",
                manifest_sha256="3" * 64, claim_path=Path(temporary) / "claim",
                claim_sha256="4" * 64, post_state=state)
            with mock.patch.object(policy, "capture_storage_tree_state", return_value=state):
                cap.validate(build_id=cap.build_id, owner_experiment=cap.admission_id,
                             plan_sha256=cap.plan_sha256, manifest_path=Path(cap.manifest_path))
                for kwargs in (
                    {"build_id": "r6a-r70-dual-lineage-other"},
                    {"owner_experiment": "r6a-r70-admission-other"},
                    {"plan_sha256": "5" * 64},
                    {"manifest_path": Path(temporary) / "other.json"},
                ):
                    args = {"build_id": cap.build_id, "owner_experiment": cap.admission_id,
                            "plan_sha256": cap.plan_sha256, "manifest_path": Path(cap.manifest_path)}
                    args.update(kwargs)
                    with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                        cap.validate(**args)
                with mock.patch.object(policy, "capture_storage_tree_state",
                                       return_value={**state, "sha256": "b" * 64}):
                    with self.assertRaisesRegex(RuntimeError, "unrelated storage mutation"):
                        cap.validate(build_id=cap.build_id, owner_experiment=cap.admission_id,
                                     plan_sha256=cap.plan_sha256,
                                     manifest_path=Path(cap.manifest_path))
                cap.used = True
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    cap.validate(build_id=cap.build_id, owner_experiment=cap.admission_id,
                                 plan_sha256=cap.plan_sha256, manifest_path=Path(cap.manifest_path))

    def test_capture_includes_paths_directories_and_directory_children(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo"
            cache = Path(temporary) / "cache"
            old_repo, old_cache = policy.REPO, policy.CACHE
            old_gate, old_roots = policy.GATE0_ATTESTATION_PATH, policy._classified_project_roots
            self.addCleanup(setattr, policy, "REPO", old_repo)
            self.addCleanup(setattr, policy, "CACHE", old_cache)
            self.addCleanup(setattr, policy, "GATE0_ATTESTATION_PATH", old_gate)
            self.addCleanup(setattr, policy, "_classified_project_roots", old_roots)
            (root / "experiments/storage").mkdir(parents=True)
            cache.mkdir()
            gate = root / "experiments/storage/gate.json"
            gate.write_text("signed", encoding="utf-8")
            policy.REPO = root
            policy.CACHE = cache
            policy.GATE0_ATTESTATION_PATH = gate
            policy._classified_project_roots = lambda: [
                policy.DiscoveredProjectRoot(root, policy.ROOT_CLASS_MANDATORY, "main_repo"),
                policy.DiscoveredProjectRoot(cache, policy.ROOT_CLASS_MANDATORY, "project_cache"),
            ]
            (root / "experiments/storage/base.bin").write_bytes(b"base")
            before = policy.capture_storage_tree_state()
            rows = {row["path"]: row for row in before["entries"]}
            self.assertIn(str(root), rows)
            self.assertEqual(rows[str(root)]["kind"], "directory")
            self.assertIn("base.bin", rows[str(root / "experiments/storage")]["children"])
            self.assertNotIn(str(gate), rows)
            (root / "experiments/storage/unrelated").write_bytes(b"")
            after = policy.capture_storage_tree_state()
            self.assertNotEqual(before, after)

    def test_admission_transition_attributes_one_file_and_exact_parent_metadata(self):
        before = {"entries": [
            {"path": "/repo", "kind": "directory", "device": 1, "inode": 1,
             "mode": 0o700, "link_count": 2, "size_bytes": 64,
             "allocated_bytes": 4096, "children": ["experiments"]},
            {"path": "/repo/experiments", "kind": "directory", "device": 1, "inode": 2,
             "mode": 0o700, "link_count": 2, "size_bytes": 64,
             "allocated_bytes": 4096, "children": ["storage"]},
            {"path": "/repo/experiments/storage", "kind": "directory", "device": 1, "inode": 3,
             "mode": 0o700, "link_count": 2, "size_bytes": 64,
             "allocated_bytes": 4096, "children": ["manifests"]},
            {"path": "/repo/experiments/storage/manifests", "kind": "directory", "device": 1, "inode": 4,
             "mode": 0o700, "link_count": 2, "size_bytes": 64,
             "allocated_bytes": 4096, "children": []},
            {"path": "/cache", "kind": "directory", "device": 1, "inode": 5,
             "mode": 0o700, "link_count": 2, "size_bytes": 64,
             "allocated_bytes": 4096, "children": ["claims"]},
            {"path": "/cache/claims", "kind": "directory", "device": 1, "inode": 6,
             "mode": 0o700, "link_count": 2, "size_bytes": 64,
             "allocated_bytes": 4096, "children": []},
        ]}
        after = json.loads(json.dumps(before))
        by_path = {row["path"]: row for row in after["entries"]}
        manifest = "/repo/experiments/storage/manifests/r6a-r70-admission-x.json"
        claim = "/cache/claims/r6a-r70-admission-x.claim"
        by_path["/repo/experiments/storage/manifests"]["children"] = ["r6a-r70-admission-x.json"]
        by_path["/cache/claims"]["children"] = ["r6a-r70-admission-x.claim"]
        by_path["/repo/experiments/storage/manifests"]["allocated_bytes"] += 4096
        by_path["/cache/claims"]["allocated_bytes"] += 4096
        for path, kind in ((manifest, "file"), (claim, "file")):
            after["entries"].append({"path": path, "kind": kind, "device": 1,
                "inode": 90 if kind == "file" else 91, "mode": 0o600,
                "link_count": 1, "size_bytes": 64, "allocated_bytes": 4096})
        claim_state = json.loads(json.dumps(before))
        claim_rows = {row["path"]: row for row in claim_state["entries"]}
        claim_rows["/cache/claims"]["children"] = ["r6a-r70-admission-x.claim"]
        claim_rows["/cache/claims"]["allocated_bytes"] += 4096
        claim_state["entries"].append(next(row for row in after["entries"] if row["path"] == claim))
        self.assertTrue(policy._storage_tree_transition_is_admission_owned(
            before, claim_state, manifest_path=Path(manifest), claim_path=Path(claim),
            authorized_file_paths={claim}))
        self.assertTrue(policy._storage_tree_transition_is_admission_owned(
            claim_state, after, manifest_path=Path(manifest), claim_path=Path(claim),
            authorized_file_paths={manifest}))
        external = json.loads(json.dumps(after))
        external_rows = {row["path"]: row for row in external["entries"]}
        external_rows["/repo"]["allocated_bytes"] += 4096
        self.assertFalse(policy._storage_tree_transition_is_admission_owned(
            before, external, manifest_path=Path(manifest), claim_path=Path(claim)))
        external = json.loads(json.dumps(claim_state))
        external_rows = {row["path"]: row for row in external["entries"]}
        external_rows["/cache/claims"]["children"].append("unrelated.claim")
        external_rows["/cache/claims"]["allocated_bytes"] += 4096
        self.assertFalse(policy._storage_tree_transition_is_admission_owned(
            claim_state, external, manifest_path=Path(manifest), claim_path=Path(claim),
            authorized_file_paths={manifest}))
        external = json.loads(json.dumps(claim_state))
        external_rows = {row["path"]: row for row in external["entries"]}
        external_rows["/cache/claims"]["allocated_bytes"] += 4096
        self.assertFalse(policy._storage_tree_transition_is_admission_owned(
            claim_state, external, manifest_path=Path(manifest), claim_path=Path(claim),
            authorized_file_paths={manifest}))
        external = json.loads(json.dumps(claim_state))
        external_rows = {row["path"]: row for row in external["entries"]}
        external_rows["/cache/claims"]["link_count"] += 1
        self.assertFalse(policy._storage_tree_transition_is_admission_owned(
            before, external, manifest_path=Path(manifest), claim_path=Path(claim),
            authorized_file_paths={claim}))
        external = json.loads(json.dumps(after))
        external_rows = {row["path"]: row for row in external["entries"]}
        external_rows["/repo/experiments/storage/manifests"]["children"].append("external")
        self.assertFalse(policy._storage_tree_transition_is_admission_owned(
            before, external, manifest_path=Path(manifest), claim_path=Path(claim)))

    def test_strict_project_usage_raises_on_walk_failure_instead_of_undercounting(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            info = root.stat()
            discovered = policy.DiscoveredProjectRoot(
                path=root, classification=policy.ROOT_CLASS_MANDATORY,
                category="main_repo", expected_dev=info.st_dev, expected_ino=info.st_ino)

            def failing_walk(*args, onerror=None, **kwargs):
                onerror(OSError("injected unreadable descendant"))
                return iter(())

            with (mock.patch.object(policy, "_classified_project_roots", return_value=[discovered]),
                  mock.patch.object(policy.os, "fwalk", side_effect=failing_walk)):
                with self.assertRaisesRegex(OSError, "injected unreadable descendant"):
                    policy.measure_project_usage(strict=True)

    def test_strict_project_usage_skips_symlink_leaves_without_following_targets(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            base = Path(temporary)
            root = base / "classified-root"
            outside = base / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "regular.txt").write_text("count this", encoding="utf-8")
            target = outside / "target.dylib"
            target.write_bytes(b"outside target")
            valid_link = root / "libdxvk_valid.dylib"
            valid_link.symlink_to(target)
            missing_target = outside / "missing.dylib"
            broken_link = root / "libdxvk_d3d11.dylib"
            broken_link.symlink_to(missing_target)
            root_info = root.stat()
            discovered = policy.DiscoveredProjectRoot(
                path=root, classification=policy.ROOT_CLASS_MANDATORY,
                category="main_repo", expected_dev=root_info.st_dev,
                expected_ino=root_info.st_ino)
            original_open = os.open
            forbidden_paths = {valid_link, broken_link, target, missing_target}

            def reject_link_or_target_open(path, flags, *args, **kwargs):
                if Path(path) in forbidden_paths:
                    raise AssertionError(f"capacity walk opened a symlink or its target: {path}")
                return original_open(path, flags, *args, **kwargs)

            original_identity = policy._entry_volume_identity

            def reject_link_identity_open(path, *, directory):
                if path in {valid_link, broken_link}:
                    raise AssertionError(f"capacity walk requested a followed link identity: {path}")
                return original_identity(path, directory=directory)

            with (mock.patch.object(policy, "_classified_project_roots",
                                    return_value=[discovered]),
                  mock.patch.object(policy.os, "open", side_effect=reject_link_or_target_open),
                  mock.patch.object(policy, "_entry_volume_identity",
                                    side_effect=reject_link_identity_open)):
                result = policy.measure_project_usage(strict=True)

            self.assertEqual(result["logical_bytes"], len(b"count this"))
            self.assertEqual(result["file_count"], 1,
                             "valid and broken symlink leaves are skipped like inventory non-files")

    def test_strict_project_usage_rejects_symlink_leaf_on_unclassified_device(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            base = Path(temporary)
            root = base / "classified-root"
            root.mkdir()
            outside = base / "target"
            outside.write_bytes(b"target")
            link = root / "external.dylib"
            link.symlink_to(outside)
            root_info = root.stat()
            discovered = policy.DiscoveredProjectRoot(
                path=root, classification=policy.ROOT_CLASS_MANDATORY,
                category="main_repo", expected_dev=root_info.st_dev,
                expected_ino=root_info.st_ino)
            original_stat = os.stat

            def different_device_stat(path, *args, **kwargs):
                info = original_stat(path, *args, **kwargs)
                if (path == link.name and kwargs.get("dir_fd") is not None
                        and kwargs.get("follow_symlinks") is False):
                    fields = list(info)
                    fields[2] = root_info.st_dev + 1
                    return os.stat_result(fields)
                return info

            with (mock.patch.object(policy, "_classified_project_roots",
                                    return_value=[discovered]),
                  mock.patch.object(policy.os, "stat", side_effect=different_device_stat)):
                with self.assertRaisesRegex(OSError, "unbound symlink leaf"):
                    policy.measure_project_usage(strict=True)

    def test_strict_project_usage_rejects_unbound_nested_mount(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            nested = root / "nested-mount"
            nested.mkdir()
            info = root.stat()
            discovered = policy.DiscoveredProjectRoot(
                path=root, classification=policy.ROOT_CLASS_MANDATORY,
                category="main_repo", expected_dev=info.st_dev, expected_ino=info.st_ino)
            original_identity = policy._entry_mount_identity_at

            def cross_device_identity(parent_fd, name, *, directory=True):
                if name == nested.name:
                    _device, _inode, fsid, root_mountpoint = policy._entry_mount_identity(
                        root, directory=True)
                    return info.st_dev + 1, nested.stat().st_ino, fsid, root_mountpoint
                return original_identity(parent_fd, name, directory=directory)

            with (mock.patch.object(policy, "_classified_project_roots", return_value=[discovered]),
                  mock.patch.object(policy, "_entry_mount_identity_at", side_effect=cross_device_identity)):
                with self.assertRaisesRegex(OSError, "unbound nested mount"):
                    policy.measure_project_usage(strict=True)

    def test_strict_project_usage_rejects_same_device_different_fsid(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            nested = root / "nested-mount"
            nested.mkdir()
            info = root.stat()
            discovered = policy.DiscoveredProjectRoot(
                path=root, classification=policy.ROOT_CLASS_MANDATORY,
                category="main_repo", expected_dev=info.st_dev, expected_ino=info.st_ino)
            fsid = policy._volume_identity(root)["fsid"]
            original_identity = policy._entry_mount_identity_at

            def alternate_fsid(parent_fd, name, *, directory=True):
                if name == nested.name:
                    _device, _inode, _fsid, root_mountpoint = policy._entry_mount_identity(
                        root, directory=True)
                    return info.st_dev, nested.stat().st_ino, fsid + 1, root_mountpoint
                return original_identity(parent_fd, name, directory=directory)

            with (mock.patch.object(policy, "_classified_project_roots", return_value=[discovered]),
                  mock.patch.object(policy, "_entry_mount_identity_at", side_effect=alternate_fsid)):
                with self.assertRaisesRegex(OSError, "unbound nested mount"):
                    policy.measure_project_usage(strict=True)

    def test_strict_project_usage_rejects_mountpoint_with_same_device_and_fsid(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            nested = root / "same-filesystem-mount"
            nested.mkdir()
            info = root.stat()
            discovered = policy.DiscoveredProjectRoot(
                path=root, classification=policy.ROOT_CLASS_MANDATORY,
                category="main_repo", expected_dev=info.st_dev, expected_ino=info.st_ino)
            fsid = policy._volume_identity(root)["fsid"]
            original_identity = policy._entry_mount_identity_at

            def same_filesystem_mount(parent_fd, name, *, directory=True):
                if name == nested.name:
                    return info.st_dev, nested.stat().st_ino, fsid, str(nested)
                return original_identity(parent_fd, name, directory=directory)

            with (mock.patch.object(policy, "_classified_project_roots", return_value=[discovered]),
                  mock.patch.object(policy, "_entry_mount_identity_at", side_effect=same_filesystem_mount),
                  mock.patch.object(policy.os.path, "ismount", return_value=False)):
                with self.assertRaisesRegex(OSError, "unbound nested mount"):
                    policy.measure_project_usage(strict=True)

    def test_strict_project_usage_rejects_root_inode_change_during_identity_capture(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            info = root.stat()
            discovered = policy.DiscoveredProjectRoot(
                path=root, classification=policy.ROOT_CLASS_MANDATORY,
                category="main_repo", expected_dev=info.st_dev, expected_ino=info.st_ino)
            original_identity = policy._descriptor_mount_identity

            def replaced_root_identity(fd, *, label="directory"):
                device, inode, fsid, mountpoint = original_identity(fd, label=label)
                if label == str(root):
                    return device, inode + 1, fsid, mountpoint
                return device, inode, fsid, mountpoint

            with (mock.patch.object(policy, "_classified_project_roots", return_value=[discovered]),
                  mock.patch.object(policy, "_descriptor_mount_identity", side_effect=replaced_root_identity)):
                with self.assertRaisesRegex(OSError, "storage root identity changed during mountpoint capture"):
                    policy.measure_project_usage(strict=True)

    def test_strict_project_usage_rejects_descendant_replacement_before_descent(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            nested = root / "nested"
            nested.mkdir()
            replacement = root / "replacement"
            replacement.mkdir()
            (replacement / "hidden-payload.bin").write_bytes(b"must not be silently substituted")
            info = root.stat()
            discovered = policy.DiscoveredProjectRoot(
                path=root, classification=policy.ROOT_CLASS_MANDATORY,
                category="main_repo", expected_dev=info.st_dev, expected_ino=info.st_ino)

            def substituted_fwalk(path, *, topdown, onerror, follow_symlinks, dir_fd):
                root_entry_fd = os.dup(dir_fd)
                try:
                    yield ".", [nested.name], [], root_entry_fd
                finally:
                    os.close(root_entry_fd)
                replacement_fd = os.open(
                    replacement, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                try:
                    yield nested.name, [], [], replacement_fd
                finally:
                    os.close(replacement_fd)

            with (mock.patch.object(policy, "_classified_project_roots", return_value=[discovered]),
                  mock.patch.object(policy.os, "fwalk", side_effect=substituted_fwalk)):
                with self.assertRaisesRegex(OSError, "storage directory identity changed during capacity walk"):
                    policy.measure_project_usage(strict=True)

    def test_fstatfs_mountpoint_identity_is_pinned_to_the_open_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            root_fd = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            parent_fd = os.open(root.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                root_mountpoint = policy._mountpoint_from_fd(root_fd)
                parent_mountpoint = policy._mountpoint_from_fd(parent_fd)
            finally:
                os.close(root_fd)
                os.close(parent_fd)
            self.assertEqual(root_mountpoint, parent_mountpoint)
            self.assertNotEqual(root_mountpoint, str(root))

    def test_strict_root_discovery_rejects_matching_xcode_root_symlink(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary)
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            derived = home / "Library" / "Developer" / "Xcode" / "DerivedData"
            derived.mkdir(parents=True)
            target = home / "elsewhere"
            target.mkdir()
            (derived / "MoltenVKPackaging-unexpected").symlink_to(target, target_is_directory=True)
            original_iterdir = Path.iterdir

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter(())
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache)):
                with self.assertRaisesRegex(RuntimeError, "Xcode DerivedData project root"):
                    policy._classified_project_roots(strict=True)

    def test_strict_root_discovery_rejects_failed_worktree_enumeration(self) -> None:
        failed = subprocess.CompletedProcess(args=["git"], returncode=2, stdout="", stderr="failed")
        with mock.patch.object(policy.subprocess, "run", return_value=failed):
            with self.assertRaisesRegex(RuntimeError, "registered Git worktrees"):
                policy._classified_project_roots(strict=True)

    def test_strict_root_discovery_rejects_unrecognized_marked_cache_root(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary)
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            unknown = cache_root / "compiler-data"
            unknown.mkdir()
            (unknown / ".fgmetal-storage-root.json").write_text("{}\n", encoding="utf-8")
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter(())
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[])):
                with self.assertRaisesRegex(RuntimeError, "unrecognized cache build/lease root"):
                    policy._classified_project_roots(strict=True)

    def test_strict_root_discovery_rejects_unrecognized_cache_directory_alias_with_nested_build(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary) / "home"
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            target = Path(temporary) / "unclassified-cache-tree"
            nested_build = target / "sdk" / "variant" / "build"
            nested_build.mkdir(parents=True)
            (nested_build / "build.ninja").write_text("synthetic\n", encoding="utf-8")
            (nested_build / "meson-private").mkdir()
            (cache_root / "ExternalSDKCacheAlias").symlink_to(target, target_is_directory=True)
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter(())
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[])):
                with self.assertRaisesRegex(RuntimeError, "unrecognized cache directory alias"):
                    policy._classified_project_roots(strict=True)

    def test_strict_root_discovery_allows_unrelated_cache_report_file(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary) / "home"
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            report = cache_root / "external-sdk-build-report.txt"
            report.write_text("unrelated report\n", encoding="utf-8")
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter(())
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[])):
                roots = policy._classified_project_roots(strict=True)
            self.assertTrue(any(root.path == cache for root in roots))
            self.assertFalse(any(root.path == report for root in roots))

    def test_strict_root_discovery_rejects_symlinked_project_marker_in_unknown_cache(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary) / "home"
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            unknown = cache_root / "compiler-data"
            unknown.mkdir()
            target = Path(temporary) / "marker-target"
            target.write_text("{}\n", encoding="utf-8")
            (unknown / ".fgmetal-storage-root.json").symlink_to(target)
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter(())
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[])):
                with self.assertRaisesRegex(RuntimeError, "unsafe build-root signature object"):
                    policy._classified_project_roots(strict=True)

    def test_strict_root_discovery_rejects_symlinked_compiler_signature_in_unknown_cache(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary) / "home"
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            unknown = cache_root / "compiler-data"
            unknown.mkdir()
            target = Path(temporary) / "build.ninja-target"
            target.write_text("ninja\n", encoding="utf-8")
            (unknown / "build.ninja").symlink_to(target)
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter(())
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[])):
                with self.assertRaisesRegex(RuntimeError, "unsafe build-root signature object"):
                    policy._classified_project_roots(strict=True)

    def test_strict_root_discovery_rejects_symlinked_project_marker_in_unknown_temp(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary) / "home"
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            unknown = Path(temporary) / "compiler-temp-root"
            unknown.mkdir()
            target = Path(temporary) / "marker-target"
            target.write_text("{}\n", encoding="utf-8")
            (unknown / ".fgmetal-build-lease.json").symlink_to(target)
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter((unknown,))
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[])):
                with self.assertRaisesRegex(RuntimeError, "unsafe build-root signature object"):
                    policy._classified_project_roots(strict=True)

    def test_strict_root_discovery_rejects_symlinked_compiler_signature_in_unknown_temp(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary) / "home"
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            unknown = Path(temporary) / "compiler-temp-root"
            unknown.mkdir()
            target = Path(temporary) / "meson-private-target"
            target.mkdir()
            (unknown / "meson-private").symlink_to(target, target_is_directory=True)
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter((unknown,))
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[])):
                with self.assertRaisesRegex(RuntimeError, "unsafe build-root signature object"):
                    policy._classified_project_roots(strict=True)

    def test_strict_root_discovery_rejects_unrecognized_marked_temp_root_and_skips_logs(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary) / "home"
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            unknown = Path(temporary) / "compiler-temp-root"
            unknown.mkdir()
            (unknown / ".fgmetal-build-lease.json").write_text("{}\n", encoding="utf-8")
            log = Path(temporary) / "fgmetal-build.log"
            log.write_text("log\n", encoding="utf-8")
            named_cache = cache_root / "FGMetalFuture"
            named_cache.mkdir()
            cache_log = cache_root / "FGMetal-run.log"
            cache_log.write_text("log\n", encoding="utf-8")
            named_temp = Path(temporary) / "fgmetal-future-build"
            named_temp.mkdir()
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter((unknown, log, named_temp))
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[])):
                with self.assertRaisesRegex(RuntimeError, "unrecognized temporary build/lease root"):
                    policy._classified_project_roots(strict=True)

                (unknown / ".fgmetal-build-lease.json").unlink()
                roots = policy._classified_project_roots(strict=True)
                root_paths = {item.path for item in roots}
                self.assertIn(named_cache, root_paths)
                self.assertIn(named_temp, root_paths)
                self.assertNotIn(log, root_paths)
                self.assertNotIn(cache_log, root_paths)

    def test_strict_root_discovery_rejects_unbranded_semantic_build_roots(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary) / "home"
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            unknown_cache = cache_root / "compiler-cache"
            unknown_cache.mkdir()
            (unknown_cache / "build.ninja").write_text("ninja\n", encoding="utf-8")
            unknown_temp = Path(temporary) / "arbitrary-run-dir"
            unknown_temp.mkdir()
            (unknown_temp / "CMakeCache.txt").write_text("cmake\n", encoding="utf-8")
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter((unknown_temp,))
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed)):
                with self.assertRaisesRegex(RuntimeError, "unrecognized cache build/lease root"):
                    policy._classified_project_roots(strict=True)
                (unknown_cache / "build.ninja").unlink()
                with self.assertRaisesRegex(RuntimeError, "unrecognized temporary build/lease root"):
                    policy._classified_project_roots(strict=True)

    def test_strict_root_discovery_blocks_unknown_semantic_candidate_without_counting_other_project(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            home = Path(temporary) / "home"
            cache_root = home / "Library" / "Caches"
            cache_root.mkdir(parents=True)
            cache = cache_root / "FGMetal"
            cache.mkdir()
            unknown = Path(temporary) / "unbranded-build-root"
            unknown.mkdir()
            foreign = Path(temporary) / "known-other-project-build"
            foreign.mkdir()
            original_iterdir = Path.iterdir
            completed = subprocess.CompletedProcess(args=["git"], returncode=0,
                                                    stdout="", stderr="")

            def isolated_iterdir(path):
                if path == Path("/private/tmp"):
                    return iter(())
                return original_iterdir(path)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[unknown])):
                with self.assertRaisesRegex(RuntimeError, "unrecognized compiler build root"):
                    policy._classified_project_roots(strict=True)

            with (mock.patch.object(Path, "home", return_value=home),
                  mock.patch.object(Path, "iterdir", isolated_iterdir),
                  mock.patch.object(policy, "CACHE", cache),
                  mock.patch.object(policy.subprocess, "run", return_value=completed),
                  mock.patch.object(build_retention, "_all_root_candidates", return_value=[foreign]),
                  mock.patch.object(build_retention, "_r17_temp_root_disposition",
                                    return_value={"accounting_scope": "OTHER_PROJECT_PROTECTED"})):
                roots = policy._classified_project_roots(strict=True)
                self.assertNotIn(foreign, {item.path for item in roots})

    def test_live_volume_identity_contains_device_fsid_and_total(self) -> None:
        value = policy._volume_identity(policy.REPO)
        self.assertEqual(set(value), {"device", "fsid", "total_bytes"})
        self.assertIsInstance(value["device"], int)
        self.assertIsInstance(value["fsid"], int)
        self.assertGreater(value["total_bytes"], 0)
        inventory = {"filesystem": {"volume_identity": {"device": 4, "fsid": 9}}}
        self.assertEqual(policy._r71_volume_identity_blockers(
            inventory, repo_volume={"device": 4, "fsid": 9},
            build_volume={"device": 4, "fsid": 9}), [])
        self.assertEqual(len(policy._r71_volume_identity_blockers(
            inventory, repo_volume={"device": 5, "fsid": 9},
            build_volume={"device": 6, "fsid": 9})), 2)

    def test_r71_capability_skips_tree_snapshot_but_keeps_consumed_id_one_shot(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            lock_path = Path(temporary) / "lock"
            lock_path.write_bytes(b"")
            lock_file = lock_path.open("a+")
            self.addCleanup(lock_file.close)
            cap = policy.AdmissionReservationCapability(
                policy._R70_ADMISSION_CAPABILITY_TOKEN, lock_file=lock_file,
                gate_sha256="1" * 64, admission_id="r6a-r71-admission-x",
                build_id="r6a-r71-dual-lineage-x", plan_sha256="2" * 64,
                manifest_path=Path(temporary) / "manifest.json", manifest_sha256="3" * 64,
                claim_path=Path(temporary) / "claim", claim_sha256="4" * 64,
                post_state=None, r71_validation={"candidate_path": "c", "plan_path": "p",
                                               "g0c_review_path": "g", "manifest_path": "m"})
            args = {"build_id": cap.build_id, "owner_experiment": cap.admission_id,
                    "plan_sha256": cap.plan_sha256, "manifest_path": Path(cap.manifest_path)}
            with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                cap.validate(**args, consumption=True)
            with (mock.patch.object(policy, "capture_storage_tree_state",
                                    side_effect=AssertionError("R71 must not snapshot whole tree")),
                  mock.patch.object(policy.AdmissionReservationCapability, "_validate_r71")):
                cap.validate(**args)
                cap.used = True
                lock_file.close()  # reserve deliberately releases this lock before session start
                cap.validate(**args, consumption=True)
                with self.assertRaisesRegex(RuntimeError, "identity mismatch"):
                    cap.validate(**args)

    def test_r71_retention_projection_allows_only_exact_own_active_lease(self) -> None:
        admission = "r6a-r71-admission-own"
        build_id = "r6a-r71-dual-lineage-own"
        active = {"build_id": build_id, "owner_experiment": admission,
                  "path": str(policy.CACHE / "builds" / build_id),
                  "state": "ACTIVE", "kind": "dxvk"}
        snapshot = {"active": [active], "unknown": [], "completed_pending_retirement": [],
                    "failed_or_retiring": [], "active_build_count": 1,
                    "retained_build_count": 1, "active_dxvk_count": 1,
                    "active_moltenvk_count": 0, "retention_fingerprint": {"sha256": "changed"}}
        controller = mock.Mock()
        controller._retention_fingerprint.return_value = {"sha256": "gate-baseline"}
        projected = policy._r71_retention_gate_projection(
            snapshot, active_experiment_id=admission, build_id=build_id,
            build_retention=controller)
        self.assertEqual(projected["active"], [])
        self.assertEqual(projected["active_build_count"], 0)
        self.assertEqual(projected["retention_fingerprint"]["sha256"], "gate-baseline")
        for rejected in (
            {**snapshot, "active": [active, {**active, "build_id": "extra"}]},
            {**snapshot, "active": [{**active, "owner_experiment": "foreign"}]},
            {**snapshot, "unknown": [{"path": "unknown-root"}]},
            {**snapshot, "completed_pending_retirement": [{"build_id": "pending"}]},
            {**snapshot, "failed_or_retiring": [{"build_id": "failed"}]},
        ):
            with self.assertRaises(RuntimeError):
                policy._r71_retention_gate_projection(
                    rejected, active_experiment_id=admission, build_id=build_id,
                    build_retention=controller)


class ProjectedStorageAdmissionTests(unittest.TestCase):
    def test_scoped_54_gib_with_projected_free_60_gib_passes(self) -> None:
        result = _projected_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=54 * policy.GIB,
            peak_additional_bytes=0,
        )
        self.assertTrue(result["admitted"])
        self.assertEqual(result["projected_free_bytes"], 60 * policy.GIB)
        self.assertEqual(result["projected_scoped_bytes"], 54 * policy.GIB)

    def test_exact_projected_limits_are_admitted(self) -> None:
        result = _projected_without_vm_commitment(
            current_free_bytes=51 * policy.GIB,
            current_scoped_bytes=59 * policy.GIB,
            peak_additional_bytes=policy.GIB,
        )
        self.assertTrue(result["admitted"])
        self.assertEqual(result["projected_free_bytes"], 50 * policy.GIB)
        self.assertEqual(result["projected_scoped_bytes"], 60 * policy.GIB)
        self.assertIn("projected scoped FG-Metal usage exceeds the 52 GiB warning threshold",
                      result["warnings"])

    def test_existing_45_plus_gib_allocation_is_not_blocked_by_old_ceiling(self) -> None:
        result = _projected_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=46 * policy.GIB,
            peak_additional_bytes=policy.GIB,
        )
        self.assertTrue(result["admitted"])

    def test_projected_scope_over_60_gib_is_rejected_even_when_current_scope_passes(self) -> None:
        result = _projected_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=59 * policy.GIB,
            peak_additional_bytes=policy.GIB + 1,
        )
        self.assertFalse(result["admitted"])
        self.assertEqual(result["projected_scoped_bytes"], 60 * policy.GIB + 1)

    def test_projected_free_below_50_gib_is_rejected_when_scope_passes(self) -> None:
        result = _projected_without_vm_commitment(
            current_free_bytes=50 * policy.GIB + 1,
            current_scoped_bytes=54 * policy.GIB,
            peak_additional_bytes=2,
        )
        self.assertFalse(result["admitted"])
        self.assertEqual(result["projected_scoped_bytes"], 54 * policy.GIB + 2)
        self.assertEqual(result["projected_free_bytes"], 50 * policy.GIB - 1)

    def test_prefix_materialization_keeps_full_cow_peak_for_free_admission(self) -> None:
        prefix_bytes = 3 * policy.GIB
        persistent_bytes = 100 * policy.MIB
        declared_bytes = prefix_bytes + persistent_bytes
        margin = policy.admission_uncertainty_margin_bytes(declared_bytes)
        estimates = {
            "expected_prefix_bytes": prefix_bytes,
            "expected_build_bytes": 0,
            "estimated_persistent_bytes": persistent_bytes,
            "estimated_uncertainty_margin_bytes": margin,
            "estimated_projected_new_disk_bytes": declared_bytes + margin,
        }
        current_free = 52 * policy.GIB
        current_scope = 48 * policy.GIB

        old_incomplete_recheck = _projected_without_vm_commitment(
            current_free_bytes=current_free,
            current_scoped_bytes=current_scope,
            peak_additional_bytes=persistent_bytes + margin,
        )
        corrected_recheck = _post_materialization_without_vm_commitment(
            current_free_bytes=current_free,
            current_scoped_bytes=current_scope,
            manifest_estimates=estimates,
        )

        self.assertTrue(old_incomplete_recheck["admitted"])
        self.assertFalse(corrected_recheck["admitted"])
        self.assertIn("projected free disk is below the 50 GiB normal minimum",
                      corrected_recheck["blockers"])
        self.assertEqual(corrected_recheck["peak_additional_bytes"], declared_bytes + margin)

    def test_prefix_materialization_keeps_full_cow_peak_for_scope_admission(self) -> None:
        prefix_bytes = 3 * policy.GIB
        declared_bytes = prefix_bytes
        margin = policy.admission_uncertainty_margin_bytes(declared_bytes)
        estimates = {
            "expected_prefix_bytes": prefix_bytes,
            "expected_build_bytes": 0,
            "estimated_persistent_bytes": 0,
            "estimated_uncertainty_margin_bytes": margin,
            "estimated_projected_new_disk_bytes": declared_bytes + margin,
        }

        result = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=53 * policy.GIB,
            manifest_estimates=estimates,
        )

        self.assertTrue(result["admitted"])
        self.assertEqual(result["blockers"], [])
        self.assertIn("projected scoped FG-Metal usage exceeds the 52 GiB warning threshold",
                      result["warnings"])

    def test_unlinked_mounted_image_upper_bound_is_included(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-inventory-bound-", dir="/private/tmp") as temp:
            inventory_path = Path(temp) / "inventory.json"
            inventory_path.write_text(json.dumps({"totals": {
                "fgmetal_scoped_allocated_inode_bytes": 48 * policy.GIB,
                "fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes": policy.GIB,
            }}), encoding="utf-8")
            with mock.patch.object(policy, "INVENTORY_PATH", inventory_path):
                measured = policy.conservative_project_usage_bytes({
                    "allocated_inode_deduplicated_bytes": 47 * policy.GIB,
                })
        self.assertEqual(measured, 48 * policy.GIB)


class R64PrefixLeaseCapacityAccountingRegressionTests(unittest.TestCase):
    """Section 16: Required Capacity Regressions for R64 PrefixLease Repair."""

    def _make_estimates(self, prefix_bytes: int = 3 * policy.GIB,
                        persistent_bytes: int = 32 * policy.MIB,
                        build_bytes: int = 0) -> dict[str, int]:
        declared = prefix_bytes + persistent_bytes + build_bytes
        margin = policy.admission_uncertainty_margin_bytes(declared)
        return {
            "expected_prefix_bytes": prefix_bytes,
            "expected_build_bytes": build_bytes,
            "estimated_persistent_bytes": persistent_bytes,
            "estimated_uncertainty_margin_bytes": margin,
            "estimated_projected_new_disk_bytes": declared + margin,
        }

    def test_01_baseline_plus_declared_peak_above_60_gib_rejects(self) -> None:
        """1. baseline + declared peak above 60 GiB -> reject."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=32 * policy.MIB)
        # Baseline = 58 GiB. The full ~3.33 GiB declared peak projects above 60 GiB.
        result = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=58 * policy.GIB,
            manifest_estimates=estimates,
            materialized_prefix_bytes=0,
        )
        self.assertFalse(result["admitted"])
        self.assertIn("projected scoped FG-Metal usage exceeds the 60 GiB ceiling",
                      result["blockers"])

    def test_02_baseline_plus_declared_peak_below_60_gib_admits(self) -> None:
        """2. baseline + declared peak below 60 GiB -> admit."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=32 * policy.MIB)
        # Baseline = 56 GiB. The full ~3.33 GiB declared peak stays below 60 GiB.
        result = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=56 * policy.GIB,
            manifest_estimates=estimates,
            materialized_prefix_bytes=0,
        )
        self.assertTrue(result["admitted"])
        self.assertEqual(result["blockers"], [])
        self.assertLessEqual(result["projected_scoped_bytes"], 60 * policy.GIB)

    def test_03_after_partial_materialization_realized_bytes_accounted_exactly_once(self) -> None:
        """3. after partial materialization, realized bytes are accounted exactly once."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=32 * policy.MIB)
        baseline = 50 * policy.GIB
        realized_prefix = 2 * policy.GIB
        post_clone_scoped = baseline + realized_prefix  # 52 GiB

        result = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=post_clone_scoped,
            manifest_estimates=estimates,
            materialized_prefix_bytes=realized_prefix,
        )
        self.assertTrue(result["admitted"])
        # Expected: baseline (50 GiB) + declared_peak (~3.33 GiB) = ~53.33 GiB, NOT 52 + 3.33 = 55.33 GiB.
        expected_projected_scope = baseline + estimates["estimated_projected_new_disk_bytes"]
        self.assertEqual(result["projected_scoped_bytes"], expected_projected_scope)

    def test_04_remaining_peak_is_accounted_exactly_once(self) -> None:
        """4. remaining peak is accounted exactly once."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=32 * policy.MIB)
        realized_prefix = 2303463424  # actual empirical R63 prefix allocation (~2.15 GiB)
        declared_peak = estimates["estimated_projected_new_disk_bytes"]
        expected_scoped_additional = declared_peak - realized_prefix

        result = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=52 * policy.GIB,
            manifest_estimates=estimates,
            materialized_prefix_bytes=realized_prefix,
        )
        self.assertEqual(result["scoped_additional_bytes"], expected_scoped_additional)
        self.assertEqual(result["projected_scoped_bytes"], 52 * policy.GIB + expected_scoped_additional)

    def test_05_persistent_bytes_accounted_exactly_once(self) -> None:
        """5. persistent bytes accounted exactly once."""
        est_32m = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=32 * policy.MIB)
        est_64m = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=64 * policy.MIB)
        realized_prefix = 2 * policy.GIB

        res_32m = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=50 * policy.GIB,
            manifest_estimates=est_32m,
            materialized_prefix_bytes=realized_prefix,
        )
        res_64m = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=50 * policy.GIB,
            manifest_estimates=est_64m,
            materialized_prefix_bytes=realized_prefix,
        )
        diff = res_64m["projected_scoped_bytes"] - res_32m["projected_scoped_bytes"]
        expected_diff = est_64m["estimated_projected_new_disk_bytes"] - est_32m["estimated_projected_new_disk_bytes"]
        self.assertEqual(diff, expected_diff)

    def test_06_uncertainty_accounted_exactly_once(self) -> None:
        """6. uncertainty accounted exactly once."""
        prefix_bytes = 3 * policy.GIB
        persistent_bytes = 32 * policy.MIB
        declared_base = prefix_bytes + persistent_bytes
        margin = policy.admission_uncertainty_margin_bytes(declared_base)
        estimates = {
            "expected_prefix_bytes": prefix_bytes,
            "expected_build_bytes": 0,
            "estimated_persistent_bytes": persistent_bytes,
            "estimated_uncertainty_margin_bytes": margin,
            "estimated_projected_new_disk_bytes": declared_base + margin,
        }
        result = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=50 * policy.GIB,
            manifest_estimates=estimates,
            materialized_prefix_bytes=0,
        )
        self.assertEqual(result["peak_additional_bytes"], declared_base + margin)
        self.assertEqual(result["projected_scoped_bytes"], 50 * policy.GIB + declared_base + margin)

    def test_build_reservation_is_included_in_declared_peak(self) -> None:
        build_bytes = 2 * policy.GIB
        estimates = self._make_estimates(
            prefix_bytes=3 * policy.GIB,
            persistent_bytes=32 * policy.MIB,
            build_bytes=build_bytes,
        )
        declared = 3 * policy.GIB + 32 * policy.MIB + build_bytes
        margin = policy.admission_uncertainty_margin_bytes(declared)
        result = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=40 * policy.GIB,
            manifest_estimates=estimates,
            materialized_prefix_bytes=0,
        )
        self.assertEqual(estimates["estimated_projected_new_disk_bytes"], declared + margin)
        self.assertEqual(result["peak_additional_bytes"], declared + margin)
        self.assertEqual(result["projected_scoped_bytes"], 40 * policy.GIB + declared + margin)

    def test_07_unexpectedly_oversized_materialization_fails_closed(self) -> None:
        """7. unexpectedly oversized materialization -> fail closed."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB)
        oversized = 3 * policy.GIB + 100 * policy.MIB

        receipt = {
            "expected_prefix_bytes": 3 * policy.GIB,
            "materialized_prefix_allocated_bytes": oversized,
            "pre_materialization_output_allocated_bytes": 0,
            "post_materialization_output_allocated_bytes": 0,
            "estimated_persistent_bytes": 32 * policy.MIB,
            "pre_materialization_free_bytes": 60 * policy.GIB,
            "post_materialization_free_bytes": 57 * policy.GIB,
            "estimated_projected_new_disk_bytes": estimates["estimated_projected_new_disk_bytes"],
            "gate0_attested_nonlease_scope_bytes": 50 * policy.GIB,
            "pre_materialization_nonlease_scope_bytes": 50 * policy.GIB,
            "post_materialization_nonlease_scope_bytes": 50 * policy.GIB,
        }
        sealed = policy._seal_storage_evidence(receipt, "prefix_materialization_hmac_sha256")
        blockers = policy._prefix_lease_accounting_blockers(
            sealed,
            attested_free_bytes=60 * policy.GIB,
            current_free_bytes=57 * policy.GIB,
            current_project_bytes=50 * policy.GIB + oversized,
            current_nonlease_project_bytes=50 * policy.GIB,
            attested_nonlease_project_bytes=50 * policy.GIB,
            current_prefix_allocated_bytes=oversized,
            current_output_allocated_bytes=0,
        )
        self.assertIn("materialized PrefixLease allocation no longer matches its signed reservation receipt",
                      blockers)

    def test_08_template_growth_beyond_certified_bound_fails_closed(self) -> None:
        """8. template growth beyond certified/expected bound -> fail closed."""
        # Test validation on negative or boolean input for materialized bytes
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB)
        with self.assertRaises(ValueError):
            policy._post_materialization_prefix_admission(
                current_free_bytes=60 * policy.GIB,
                current_scoped_bytes=50 * policy.GIB,
                manifest_estimates=estimates,
                materialized_prefix_bytes=-1,
            )

    def test_09_free_space_floor_remains_enforced(self) -> None:
        """9. free-space floor remains enforced."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=32 * policy.MIB)
        # Even with low scoped usage (40 GiB), if projected free falls below 50 GiB, must block.
        # current_free = 52 GiB, declared_peak = ~3.33 GiB -> projected free = ~48.67 GiB < 50 GiB.
        result = _post_materialization_without_vm_commitment(
            current_free_bytes=52 * policy.GIB,
            current_scoped_bytes=40 * policy.GIB,
            manifest_estimates=estimates,
            materialized_prefix_bytes=2 * policy.GIB,
        )
        self.assertFalse(result["admitted"])
        self.assertIn("projected free disk is below the 50 GiB normal minimum",
                      result["blockers"])

    def test_10_emergency_threshold_remains_enforced(self) -> None:
        """10. emergency threshold remains enforced."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB)
        # current_free = 24 GiB < 25 GiB hard stop.
        result = _post_materialization_without_vm_commitment(
            current_free_bytes=24 * policy.GIB,
            current_scoped_bytes=40 * policy.GIB,
            manifest_estimates=estimates,
            materialized_prefix_bytes=2 * policy.GIB,
        )
        self.assertFalse(result["admitted"])
        self.assertIn("current free space is below the 25 GiB emergency hard stop",
                      result["blockers"])

    def test_11_active_lease_accounting_remains_intact(self) -> None:
        """11. active lease accounting remains intact."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=32 * policy.MIB)
        realized = 2303463424
        receipt = {
            "expected_prefix_bytes": 3 * policy.GIB,
            "materialized_prefix_allocated_bytes": realized,
            "pre_materialization_output_allocated_bytes": 0,
            "post_materialization_output_allocated_bytes": 0,
            "estimated_persistent_bytes": 32 * policy.MIB,
            "pre_materialization_free_bytes": 60 * policy.GIB,
            "post_materialization_free_bytes": 60 * policy.GIB,
            "estimated_projected_new_disk_bytes": estimates["estimated_projected_new_disk_bytes"],
            "gate0_attested_nonlease_scope_bytes": 50 * policy.GIB,
            "pre_materialization_nonlease_scope_bytes": 50 * policy.GIB,
            "post_materialization_nonlease_scope_bytes": 50 * policy.GIB,
        }
        sealed = policy._seal_storage_evidence(receipt, "prefix_materialization_hmac_sha256")
        with mock.patch.object(policy, "_vm_capacity_commitment_snapshot",
                               return_value={"committed_growth_bytes": 0, "marker_sha256": None, "marker_identity": None}):
            blockers = policy._prefix_lease_accounting_blockers(
                sealed,
                attested_free_bytes=60 * policy.GIB,
                current_free_bytes=60 * policy.GIB,
                current_project_bytes=50 * policy.GIB + realized,
                current_nonlease_project_bytes=50 * policy.GIB,
                attested_nonlease_project_bytes=50 * policy.GIB,
                current_prefix_allocated_bytes=realized,
                current_output_allocated_bytes=0,
            )
        self.assertEqual(blockers, [])

    def test_12_cleanup_after_failed_materialization_removes_temporary_allocation_and_marker(self) -> None:
        """12. cleanup after failed materialization removes temporary allocation and marker."""
        with tempfile.TemporaryDirectory(prefix="fgmetal-test-cleanup-", dir="/private/tmp") as temp_dir:
            temp_path = Path(temp_dir)
            prefix_root = temp_path / "prefixes"
            prefix_root.mkdir()
            prefix = prefix_root / "test-prefix"
            prefix.mkdir()
            (prefix / "system.reg").write_text("test")
            output = temp_path / "test-output"
            output.mkdir()
            active_root = temp_path / "active"
            active_root.mkdir()
            exp_id = "test-exp-cleanup"
            marker_file = active_root / f"{exp_id}.json"
            marker_file.write_text(json.dumps({
                "experiment_id": exp_id,
                "prefix_path": str(prefix),
                "manifest_sha256": None,
                "state": "MATERIALIZING",
            }))
            marker_file.chmod(0o600)

            lease = policy.PrefixLease(
                prefix=prefix,
                output_dir=output,
                experiment_id=exp_id,
                wine_server=temp_path / "wineserver",
                template=temp_path / "template",
            )
            lease._created = True
            lease._prefix_created_any = True
            lease._clone_complete = True
            lease._lease_marker_path = marker_file
            lease._manifest_sha256 = None
            active_root_fd = policy._open_directory_nofollow(active_root)
            try:
                active_root_info = os.fstat(active_root_fd)
                marker_fd = os.open(marker_file.name,
                                    os.O_RDWR | getattr(os, "O_NOFOLLOW", 0),
                                    dir_fd=active_root_fd)
            finally:
                os.close(active_root_fd)
            marker_info = os.fstat(marker_fd)
            lease._active_marker_fd = marker_fd
            lease._active_marker_identity = (marker_info.st_dev, marker_info.st_ino)
            lease._lease_marker_parent_identity = (
                active_root_info.st_dev, active_root_info.st_ino)
            info = prefix.stat(follow_symlinks=False)
            prefix_parent_fd = policy._open_directory_nofollow(prefix_root)
            lease._prefix_parent_fd = prefix_parent_fd
            lease._prefix_identity = (info.st_dev, info.st_ino)
            lease._project_before = {"allocated_inode_deduplicated_bytes": 0}
            lease._free_before = 100 * policy.GIB
            lease._manifest = {"estimated_persistent_bytes": 0, "estimated_max_new_disk_bytes": 0}

            gate_attestation = temp_path / "gate0-attestation.json"
            gate_attestation.write_text(json.dumps({
                "canonical_artifact_store_audit": {"sha256": "synthetic-cas-audit"}
            }), encoding="utf-8")

            with mock.patch.object(policy, "ACTIVE_LEASE_ROOT", active_root), \
                 mock.patch.object(policy, "GATE0_ATTESTATION_PATH", gate_attestation), \
                 mock.patch.object(build_retention, "canonical_artifact_store_audit", return_value={"sha256": "synthetic-cas-audit"}), \
                 mock.patch.object(policy, "disk_free_bytes", return_value=100 * policy.GIB), \
                 mock.patch.object(policy, "measure_project_usage", return_value={"allocated_inode_deduplicated_bytes": 0}):
                lease.__exit__(RuntimeError, RuntimeError("injected materialization failure"), None)

            self.assertFalse(prefix.exists())
            self.assertFalse(marker_file.exists())
            cleanup_path = output / "prefix-cleanup.json"
            self.assertTrue(cleanup_path.is_file())
            receipt = json.loads(cleanup_path.read_text(encoding="utf-8"))
            self.assertTrue(receipt["prefix_deleted"])
            self.assertTrue(receipt["active_lease_marker_removed_after_receipt"])
            self.assertEqual(receipt["storage_hygiene_status"], "PASS")


class SharedCapacityCommitmentTests(unittest.TestCase):
    SIGNING_KEY = b"storage-policy-test-signing-key" * 2
    COMMITMENT_BYTES = 19_350_155_264
    VM_VOLUME_UUID = "11111111-2222-3333-4444-555555555555"
    SAFETY_VOLUME_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

    @classmethod
    def _marker(cls, *, current_schema: bool = True,
                commitment_bytes: int | None = None) -> dict[str, object]:
        marker: dict[str, object] = {
            "schema_version": 1,
            "session_id": "a" * 32,
            "state": "ACTIVE",
            "phase_id": "P2_AUTHORIZED",
            "container_uuid": policy.VM_CAPACITY_DOMAIN_EXPECTED_CONTAINER_UUID,
            "volume_uuid": cls.VM_VOLUME_UUID,
            "quota_bytes": 50 * policy.GIB,
            "committed_growth_bytes": (cls.COMMITMENT_BYTES if commitment_bytes is None
                                        else commitment_bytes),
            "plan_sha256": "b" * 64,
            "created_at_unix": 1_800_000_000.0,
            "updated_at_unix": 1_800_000_001.0,
        }
        if current_schema:
            marker.update({
                "volume_name": "FGMetal-VM",
                "safety_volume_uuid": cls.SAFETY_VOLUME_UUID,
                "safety_volume_in_use_bytes": 0,
                "safety_container_uuid": policy.VM_CAPACITY_DOMAIN_EXPECTED_CONTAINER_UUID,
                "safety_physical_store_uuid": policy.VM_CAPACITY_DOMAIN_EXPECTED_PHYSICAL_STORE_UUID,
                "safety_reserve_bytes": policy.VM_CAPACITY_DOMAIN_EXPECTED_SAFETY_RESERVE_BYTES,
                "pre_reserve_shared_free_bytes": 70 * policy.GIB,
                "unreserved_free_ceiling_bytes": 55 * policy.GIB,
                "p1_admission_path": str(policy.REPO / "R6-VM-P1-ADMISSION.json"),
                "p1_admission_sha256": "c" * 64,
                "p1_freshness_renewal": {
                    "path": str(policy.REPO / "R6-VM-P1-RENEWAL.json"),
                    "sha256": "d" * 64,
                    "sequence": 1,
                    "predecessor_path": None,
                    "predecessor_sha256": None,
                    "use_state": "READY",
                    "attempt_id": None,
                    "claimed_at_unix": None,
                    "body_start_evidence": None,
                },
            })
        return marker

    def _with_marker(self, marker: dict[str, object], callback, *,
                     verification_key: bytes | None = None,
                     signing_key: bytes | None = None):
        selected_signing_key = signing_key or self.SIGNING_KEY
        selected_verification_key = verification_key or selected_signing_key
        with tempfile.TemporaryDirectory(prefix="fgmetal-capacity-marker-", dir="/private/tmp") as temp:
            repo_root = Path(temp) / "repo"
            repo_root.mkdir()
            marker_root = repo_root / "vm-capacity-domain"
            marker_root.mkdir(mode=0o700)
            marker_path = marker_root / policy.VM_CAPACITY_DOMAIN_ACTIVE_MARKER.name
            deferred_inactivation_path = marker_root / "deferred-capacity-inactivation.json"
            with mock.patch.object(policy, "_manifest_signing_key",
                                   return_value=selected_signing_key):
                signed = policy._seal_storage_evidence(marker, "vm_session_hmac_sha256")
            marker_path.write_bytes(policy._canonical_pretty_json_bytes(signed))
            marker_path.chmod(0o600)
            with mock.patch.object(policy, "REPO", repo_root), \
                    mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ROOT", marker_root), \
                    mock.patch.object(policy, "VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION",
                                      deferred_inactivation_path), \
                    mock.patch.object(policy, "_manifest_signing_key",
                                      return_value=selected_verification_key):
                return callback(signed)

    def _write_test_deferred_inactivation(
            self, marker: dict[str, object], *,
            review_overrides: dict[str, object] | None = None,
            inactivation_created_at_unix: float = 1_800_000_010.0,
            inactivation_schema_version: object = 1) -> None:
        marker_bytes = policy._canonical_pretty_json_bytes(marker)
        marker_sha = hashlib.sha256(marker_bytes).hexdigest()
        branch_path = policy.REPO / "R6-VM-DEFERRED-BRANCH-RETIREMENT-test.json"
        restore_path = policy.REPO / "R6-VM-DEFERRED-RESTORE-RETIREMENT-test.json"
        review_path = policy.REPO / "R6-VM-DEFERRED-PARKING-REVIEW-test.json"
        branch = policy._seal_storage_evidence({
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_BRANCH_RETIREMENT",
            "status": "PASS",
            "created_at_unix": 1_800_000_000.0,
            "decision": "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM",
            "session_id": marker["session_id"],
            "active_marker_sha256": marker_sha,
            "committed_growth_bytes": marker["committed_growth_bytes"],
            "capability_state_before": "READY",
            "capability_state_after": "SUPERSEDED",
            "capability_claimed": False,
            "guest_assets_created": False,
            "helper_process_created": False,
            "guest_base_provisioned": False,
            "guest_base_sealed": False,
            "active_marker_rewritten": False,
        }, "vm_deferred_branch_retirement_hmac_sha256")
        branch_path.write_bytes(policy._canonical_pretty_json_bytes(branch))
        branch_path.chmod(0o600)
        branch_sha = hashlib.sha256(branch_path.read_bytes()).hexdigest()
        restore = policy._seal_storage_evidence({
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT",
            "status": "PASS",
            "created_at_unix": 1_800_000_001.0,
            "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
            "branch_retirement_receipt_sha256": branch_sha,
            "p1_provenance_preserved": True,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "vm_guest_base_sealed": False,
            "p2_cleanup_success_claimed": False,
            "future_vm_work_requires_fresh_p1": True,
            "deleted_path": "/Volumes/FGMetal-VM/restore.ipsw",
            "deleted_file_sha256": "2f016638293c3e641b8b25391a76fbc16563b3711915a5551cf8aa0f5598a5c1",
            "restore_absent": True,
            "RESTORE_RETIREMENT_RECLAIMED_BYTES": 0,
        }, "vm_deferred_restore_retirement_hmac_sha256")
        restore_path.write_bytes(policy._canonical_pretty_json_bytes(restore))
        restore_path.chmod(0o600)
        restore_sha = hashlib.sha256(restore_path.read_bytes()).hexdigest()
        review = {
            "schema_version": 1,
            "status": "PASS",
            "decision": "PASS — DEFERRED VM BRANCH PARKED SAFELY",
            "reviewer_identity": "GPT-6 Luna Max independent parking reviewer test fixture",
            "model_identity": "GPT-6 Luna Max",
            "reviewed_at_unix": 1_800_000_005.0,
            "reviewed_branch_retirement_receipt_path": branch_path.name,
            "reviewed_branch_retirement_receipt_sha256": branch_sha,
            "reviewed_restore_retirement_receipt_path": restore_path.name,
            "reviewed_restore_retirement_receipt_sha256": restore_sha,
            "findings": [],
        }
        review.update(review_overrides or {})
        review_path.write_bytes(policy._canonical_pretty_json_bytes(review))
        review_path.chmod(0o600)
        inactivation = policy._seal_storage_evidence({
            "schema_version": inactivation_schema_version,
            "record_type": "R6_VM_DEFERRED_CAPACITY_INACTIVATION",
            "status": "PASS",
            "created_at_unix": inactivation_created_at_unix,
            "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
            "session_id": marker["session_id"],
            "active_marker_path": str(policy.VM_CAPACITY_DOMAIN_ACTIVE_MARKER),
            "active_marker_sha256": marker_sha,
            "container_uuid": marker["container_uuid"],
            "vm_volume_uuid": marker["volume_uuid"],
            "quota_bytes": marker["quota_bytes"],
            "safety_volume_uuid": marker["safety_volume_uuid"],
            "safety_reserve_bytes": marker["safety_reserve_bytes"],
            "historical_committed_growth_bytes": marker["committed_growth_bytes"],
            "effective_committed_growth_bytes": 0,
            "branch_retirement_receipt_path": branch_path.name,
            "branch_retirement_receipt_sha256": branch_sha,
            "restore_retirement_receipt_path": restore_path.name,
            "restore_retirement_receipt_sha256": restore_sha,
            "parking_review_path": review_path.name,
            "parking_review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
            "parking_review_decision": review["decision"],
            "reviewer_identity": review["reviewer_identity"],
            "future_vm_reacquisition_required": True,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "active_marker_rewritten": False,
            "quota_changed": False,
            "safety_reserve_released": False,
        }, "vm_deferred_capacity_inactivation_hmac_sha256")
        release_path = policy.VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION
        release_path.write_bytes(policy._canonical_pretty_json_bytes(inactivation))
        release_path.chmod(0o600)

    def _write_test_r14_inactivation(self, marker: dict[str, object], *,
                                     restore_reclaim: int | None = None,
                                     restore_deleted_path: str = (
                                         policy.VM_CAPACITY_DOMAIN_EXPECTED_RESTORE_PATH),
                                     actual_unlinked_path: str | None = (
                                         policy.VM_CAPACITY_DOMAIN_EXPECTED_UNLINKED_RESTORE_PATH),
                                     future_vm_work_requires_fresh_p1: bool = True) -> None:
        now = time.time()
        marker_sha = hashlib.sha256(
            policy._canonical_pretty_json_bytes(marker)).hexdigest()
        branch_path = policy.REPO / "R6-VM-DEFERRED-BRANCH-RETIREMENT-r14-test.json"
        restore_path = policy.REPO / "R6-VM-DEFERRED-RESTORE-RESOLUTION-r14-test.json"
        inventory_path = policy.REPO / "experiments/storage/R6-VM-DEFERRED-CAPACITY-INVENTORY-r14-test.json"
        review_path = policy.REPO / "R6-VM-FUTURE-COMMITMENT-REVIEW-r14-test.json"
        branch = policy._seal_storage_evidence({
            "schema_version": 1, "record_type": "R6_VM_DEFERRED_BRANCH_RETIREMENT",
            "status": "PASS", "created_at_unix": now - 10,
            "decision": "ADMISSION_VALID_EXECUTION_PACKET_SUPERSEDED_BEFORE_CLAIM",
            "session_id": marker["session_id"], "active_marker_sha256": marker_sha,
            "committed_growth_bytes": self.COMMITMENT_BYTES,
            "capability_state_before": "READY", "capability_state_after": "SUPERSEDED",
            "capability_claimed": False, "guest_assets_created": False,
            "helper_process_created": False, "guest_base_provisioned": False,
            "guest_base_sealed": False, "active_marker_rewritten": False,
        }, "vm_deferred_branch_retirement_hmac_sha256")
        branch_path.write_bytes(policy._canonical_pretty_json_bytes(branch))
        branch_path.chmod(0o600)
        branch_sha = hashlib.sha256(branch_path.read_bytes()).hexdigest()
        restore = policy._seal_storage_evidence({
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_RESTORE_RETIREMENT_RESOLUTION",
            "status": "RESOLVED_WITH_MEASUREMENT_CAVEAT", "created_at_unix": now - 8,
            "classification": "PENDING_TRANSACTION_RESOLVED_BY_VERIFIED_UNLINK_WITH_MEASUREMENT_CAVEAT",
            "action": "DELETE_DEFERRED_VM_LOCAL_RESTORE_ONLY",
            "payload_deletion_status": "CONFIRMED", "local_restore_payload_retired": True,
            "restore_payload_present": False, "restore_absent": True,
            "branch_retirement_receipt_sha256": branch_sha,
            "deleted_path": restore_deleted_path,
            "actual_unlinked_path": actual_unlinked_path,
            "deleted_file_sha256": "2f016638293c3e641b8b25391a76fbc16563b3711915a5551cf8aa0f5598a5c1",
            "reclaimed_bytes_exact": restore_reclaim,
            "RESTORE_RETIREMENT_RECLAIMED_BYTES": restore_reclaim,
            "historical_reclaim_backfilled": False,
            "later_delta_is_accepted_as_actual_reclaim": False,
            "immediate_reclaim_measurement_status": "REJECTED_APFS_CROSSCHECK_DISAGREEMENT",
            "current_state_capacity_validated": False,
            "current_state_measurement_required": True,
            "capacity_commitment_inactivation_attempted": False,
            "pending_receipt_remains_status": "PENDING",
            "old_pending_receipt_rewritten": False,
            "p1_provenance_preserved": True,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False, "vm_guest_base_sealed": False,
            "p2_cleanup_success_claimed": False,
            "future_vm_work_requires_fresh_p1": future_vm_work_requires_fresh_p1,
            "active_marker_sha256": marker_sha,
            "active_marker_committed_growth_bytes": self.COMMITMENT_BYTES,
            "post_unlink_measurement_failure_sha256": "9b88a4bd9b82a2337a0ae0db4d10b186e2a606f77ca0085b228dd662481e4478",
        }, "vm_deferred_restore_retirement_hmac_sha256")
        restore_path.write_bytes(policy._canonical_pretty_json_bytes(restore))
        restore_path.chmod(0o600)
        restore_sha = hashlib.sha256(restore_path.read_bytes()).hexdigest()
        peak = 4_423_832_374
        current_free = 80 * policy.GIB
        scoped = 40 * policy.GIB
        projected_free_active = current_free - self.COMMITMENT_BYTES - peak
        projected_free_inactive = current_free - peak
        projected_scope = scoped + peak
        inventory = policy._seal_storage_evidence({
            "schema_version": 1,
            "record_type": "R6_VM_DEFERRED_CAPACITY_CURRENT_INVENTORY",
            "status": "PASS", "created_at_unix": now - 5,
            "branch_retirement_receipt_path": branch_path.name,
            "branch_retirement_receipt_sha256": branch_sha,
            "restore_resolution_path": restore_path.name,
            "restore_resolution_sha256": restore_sha,
            "source_storage_inventory_path": "experiments/storage/storage-inventory.json",
            "source_storage_inventory_sha256": "c" * 64,
            "source_storage_inventory_generated_at_unix": now - 6,
            "source_storage_inventory_bytes": 100,
            "current_state_capacity_validated": True,
            "shared_physical_free_bytes": current_free,
            "shared_apfs_container_free_bytes": current_free + 1,
            "shared_data_container_free_bytes": current_free,
            "canonical_filesystem_free_bytes": current_free,
            "conservative_admission_free_bytes": current_free,
            "fgmetal_scoped_allocated_bytes": scoped,
            "vm_volume_current_use_bytes": 1 * policy.GIB,
            "vm_volume_quota_bytes": 50 * policy.GIB,
            "vm_volume_quota_headroom_bytes": 49 * policy.GIB,
            "safety_volume_current_use_bytes": policy.VM_CAPACITY_DOMAIN_EXPECTED_SAFETY_RESERVE_BYTES,
            "safety_volume_reserve_bytes": policy.VM_CAPACITY_DOMAIN_EXPECTED_SAFETY_RESERVE_BYTES,
            "active_marker_sha256": marker_sha,
            "historical_committed_growth_bytes": self.COMMITMENT_BYTES,
            "effective_committed_growth_bytes": self.COMMITMENT_BYTES,
            "r6a_projected_peak_bytes": peak,
            "r6a_projected_peak_basis": "FROZEN_PLAN_ESTIMATE_WITH_10_PERCENT_UNCERTAINTY_NOT_YET_OWNER_MANIFEST_ADMISSION",
            "r6a_projected_peak_source_path": "R6-A-C-TEST-PROVENANCE.md",
            "r6a_projected_peak_source_sha256": "a583d7842163170ec1aac70b206ef66f8e003e2f0d47f4cac0628fc7343e48b4",
            "r6a_uncertainty_included_in_projected_peak": True,
            "projected_free_with_current_commitment_bytes": projected_free_active,
            "projected_scope_after_r6a_bytes": projected_scope,
            "projected_free_if_future_commitment_inactivated_bytes": projected_free_inactive,
            "projected_admission_with_current_commitment": True,
            "projected_admission_if_future_commitment_inactivated": True,
            "gate_minimum_free_bytes": policy.FREE_MINIMUM,
            "gate_hard_stop_free_bytes": policy.FREE_HARD_STOP,
            "gate_project_scope_ceiling_bytes": policy.GATE0_PROJECT_BUDGET,
            "hfs_test_mount_path": "/private/tmp/fgmetal-host-v3-adversarial-xhvisnca/build-root/workspace",
            "hfs_test_mount_present": False,
            "p1_restore_acquisition_historically_accepted": True,
            "p2_base_provisioning_accepted": False,
            "restore_reclaimed_bytes_exact": None,
        }, "vm_deferred_capacity_inventory_hmac_sha256")
        inventory_path.parent.mkdir(parents=True, exist_ok=True)
        inventory_path.write_bytes(policy._canonical_pretty_json_bytes(inventory))
        inventory_path.chmod(0o600)
        inventory_sha = hashlib.sha256(inventory_path.read_bytes()).hexdigest()
        review = {
            "schema_version": 1, "status": "PASS",
            "decision": "PASS — FUTURE P2 CAPACITY COMMITMENT MAY BE INACTIVATED",
            "reviewer_identity": "GPT-6 Luna Max — Future Commitment Reviewer Test Fixture",
            "model_identity": "GPT-6 Luna Max", "reviewed_at_unix": now - 2,
            "reviewed_branch_retirement_receipt_path": branch_path.name,
            "reviewed_branch_retirement_receipt_sha256": branch_sha,
            "reviewed_restore_resolution_path": restore_path.name,
            "reviewed_restore_resolution_sha256": restore_sha,
            "reviewed_capacity_inventory_path": "experiments/storage/" + inventory_path.name,
            "reviewed_capacity_inventory_sha256": inventory_sha,
            "reviewed_current_state_capacity_validated": True,
            "reviewed_shared_physical_free_bytes": current_free,
            "reviewed_canonical_filesystem_free_bytes": current_free,
            "reviewed_fgmetal_scoped_allocated_bytes": scoped,
            "reviewed_vm_volume_current_use_bytes": 1 * policy.GIB,
            "reviewed_vm_volume_quota_bytes": 50 * policy.GIB,
            "reviewed_safety_volume_current_use_bytes": policy.VM_CAPACITY_DOMAIN_EXPECTED_SAFETY_RESERVE_BYTES,
            "reviewed_safety_volume_reserve_bytes": policy.VM_CAPACITY_DOMAIN_EXPECTED_SAFETY_RESERVE_BYTES,
            "reviewed_active_future_commitment_bytes": self.COMMITMENT_BYTES,
            "reviewed_active_marker_sha256": marker_sha,
            "reviewed_projected_free_with_current_commitment_bytes": projected_free_active,
            "reviewed_r6a_projected_peak_bytes": peak,
            "reviewed_projected_scope_after_r6a_bytes": projected_scope,
            "reviewed_projected_free_if_inactivated_bytes": projected_free_inactive,
            "reviewed_projected_admission_with_current_commitment": True,
            "reviewed_hfs_test_mount_path": "/private/tmp/fgmetal-host-v3-adversarial-xhvisnca/build-root/workspace",
            "reviewed_hfs_test_mount_present": False,
            "reviewed_reclaimed_bytes_exact": None,
            "reviewed_p2_base_provisioning_accepted": False,
            "findings": [],
        }
        review_path.write_bytes(policy._canonical_pretty_json_bytes(review))
        review_path.chmod(0o600)
        release = policy._seal_storage_evidence({
            "schema_version": 2,
            "record_type": "R6_VM_DEFERRED_CAPACITY_INACTIVATION",
            "status": "PASS", "created_at_unix": now - 1,
            "deferred_decision": "VM_CONTAINMENT_DEFERRED_OPTIONAL",
            "session_id": marker["session_id"],
            "active_marker_path": str(policy.VM_CAPACITY_DOMAIN_ACTIVE_MARKER),
            "active_marker_sha256": marker_sha,
            "container_uuid": marker["container_uuid"],
            "vm_volume_uuid": marker["volume_uuid"],
            "quota_bytes": marker["quota_bytes"],
            "safety_volume_uuid": marker["safety_volume_uuid"],
            "safety_reserve_bytes": marker["safety_reserve_bytes"],
            "historical_committed_growth_bytes": self.COMMITMENT_BYTES,
            "effective_committed_growth_bytes": 0,
            "branch_retirement_receipt_path": branch_path.name,
            "branch_retirement_receipt_sha256": branch_sha,
            "restore_retirement_receipt_path": restore_path.name,
            "restore_retirement_receipt_sha256": restore_sha,
            "restore_reclaimed_bytes_exact": None,
            "capacity_inventory_path": "experiments/storage/" + inventory_path.name,
            "capacity_inventory_sha256": inventory_sha,
            "capacity_inventory_decision": "PASS_FRESH_CURRENT_STATE_CAPACITY",
            "future_commitment_review_path": review_path.name,
            "future_commitment_review_sha256": hashlib.sha256(review_path.read_bytes()).hexdigest(),
            "future_commitment_review_decision": review["decision"],
            "reviewer_identity": review["reviewer_identity"],
            "future_vm_reacquisition_required": True,
            "p2_base_provisioning_accepted": False,
            "vm_guest_base_provisioned": False,
            "active_marker_rewritten": False,
            "quota_changed": False,
            "safety_reserve_released": False,
        }, "vm_deferred_capacity_inactivation_hmac_sha256")
        release_path = policy.VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION
        release_path.write_bytes(policy._canonical_pretty_json_bytes(release))
        release_path.chmod(0o600)

    def test_r14_inactivation_consumes_fresh_capacity_and_preserves_unknown_reclaim(self) -> None:
        marker = self._marker()

        def exercise(signed_marker):
            self._write_test_r14_inactivation(signed_marker)
            return policy._vm_capacity_commitment_snapshot()

        snapshot = self._with_marker(marker, exercise)
        self.assertEqual(snapshot["committed_growth_bytes"], 0)
        self.assertEqual(snapshot["historical_committed_growth_bytes"], self.COMMITMENT_BYTES)
        self.assertIsInstance(snapshot["capacity_inactivation_sha256"], str)

    def test_r14_inactivation_rejects_fabricated_zero_reclaim(self) -> None:
        marker = self._marker()

        def exercise(signed_marker):
            self._write_test_r14_inactivation(signed_marker, restore_reclaim=0)
            return policy._vm_capacity_commitment_snapshot()

        with self.assertRaisesRegex(RuntimeError, "R14 unlink resolution"):
            self._with_marker(marker, exercise)

    def test_r14_inactivation_readback_rejects_restore_identity_and_fresh_p1_drift(self) -> None:
        cases = (
            {"restore_deleted_path": "/Volumes/FGMetal-VM/quarantine"},
            {"actual_unlinked_path": None},
            {"actual_unlinked_path": "/Volumes/FGMetal-VM/other-quarantine"},
            {"future_vm_work_requires_fresh_p1": False},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides):
                marker = self._marker()

                def exercise(signed_marker):
                    self._write_test_r14_inactivation(signed_marker, **overrides)
                    return policy._vm_capacity_commitment_snapshot()

                with self.assertRaises(RuntimeError):
                    self._with_marker(marker, exercise)

    def test_r14_inactivation_rejects_authenticated_contract_mutations(self) -> None:
        signature_field = "vm_deferred_capacity_inactivation_hmac_sha256"
        cases = (
            "extra-field",
            "wrong-hmac",
            "active-marker-hash",
            "branch-retirement-binding",
            "restore-retirement-binding",
            "historical-commitment",
            "nonzero-effective-commitment",
            "p2-accepted",
        )

        for case in cases:
            with self.subTest(case=case):
                marker = self._marker()

                def exercise(signed_marker):
                    self._write_test_r14_inactivation(signed_marker)
                    release_path = policy.VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION
                    release = json.loads(release_path.read_bytes())
                    if case == "extra-field":
                        release["unexpected_field"] = True
                    elif case == "wrong-hmac":
                        release[signature_field] = "0" * 64
                    elif case == "active-marker-hash":
                        release["active_marker_sha256"] = "f" * 64
                    elif case == "branch-retirement-binding":
                        release["branch_retirement_receipt_sha256"] = "f" * 64
                    elif case == "restore-retirement-binding":
                        release["restore_retirement_receipt_sha256"] = "f" * 64
                    elif case == "historical-commitment":
                        release["historical_committed_growth_bytes"] += 1
                    elif case == "nonzero-effective-commitment":
                        release["effective_committed_growth_bytes"] = 1
                    elif case == "p2-accepted":
                        release["p2_base_provisioning_accepted"] = True
                    if case != "wrong-hmac":
                        release = policy._seal_storage_evidence(release, signature_field)
                    release_path.write_bytes(policy._canonical_pretty_json_bytes(release))
                    release_path.chmod(0o600)
                    return policy._vm_capacity_commitment_snapshot()

                with self.assertRaises(RuntimeError):
                    self._with_marker(marker, exercise)

    def test_legacy_and_current_authenticated_markers_are_capacity_only(self) -> None:
        for current_schema in (False, True):
            with self.subTest(current_schema=current_schema):
                marker = self._marker(current_schema=current_schema)
                capacity_snapshot, execution_blockers, field_count = self._with_marker(
                    marker,
                    lambda signed: (policy._vm_capacity_commitment_snapshot(),
                                    policy._active_vm_capacity_session_blockers(),
                                    len(signed)),
                )
                self.assertEqual(field_count, 23 if current_schema else 12)
                self.assertEqual(capacity_snapshot["committed_growth_bytes"], self.COMMITMENT_BYTES)
                self.assertIsInstance(capacity_snapshot["marker_sha256"], str)
                self.assertEqual(execution_blockers, [])

    def test_authenticated_commitment_is_subtracted_once_from_apfs_free(self) -> None:
        marker = self._marker(current_schema=True)
        result = self._with_marker(
            marker,
            lambda _signed: policy.projected_storage_admission(
                current_free_bytes=71 * policy.GIB,
                current_scoped_bytes=40 * policy.GIB,
                peak_additional_bytes=2 * policy.GIB,
            ),
        )
        expected_free = 71 * policy.GIB - self.COMMITMENT_BYTES - 2 * policy.GIB
        self.assertEqual(result["shared_capacity_commitment_bytes"], self.COMMITMENT_BYTES)
        self.assertEqual(result["projected_free_bytes"], expected_free)
        self.assertEqual(result["projected_scoped_bytes"], 42 * policy.GIB)
        # Quota, safety reserve, and allocated safety-volume bytes are not
        # subtracted again: their actual APFS use is already reflected in free.
        self.assertTrue(result["admitted"])
        released = self._with_marker(
            marker,
            lambda signed: self._projected_after_deferred_inactivation(signed),
        )
        self.assertEqual(released["shared_capacity_commitment_bytes"], 0)
        self.assertEqual(released["snapshot"]["historical_committed_growth_bytes"], self.COMMITMENT_BYTES)
        self.assertEqual(released["projected_free_bytes"], 69 * policy.GIB)
        self.assertTrue(released["admitted"])

    def _projected_after_deferred_inactivation(self, signed_marker: dict[str, object]):
        self._write_test_deferred_inactivation(signed_marker)
        snapshot = policy._vm_capacity_commitment_snapshot()
        result = policy.projected_storage_admission(
            current_free_bytes=71 * policy.GIB,
            current_scoped_bytes=40 * policy.GIB,
            peak_additional_bytes=2 * policy.GIB,
        )
        return {**result, "snapshot": snapshot}

    def test_authenticated_smoke_peak_stays_blocked_below_50_gib(self) -> None:
        marker = self._marker(current_schema=True)
        declared_smoke_peak = 3 * policy.GIB + 32 * policy.MIB
        smoke_peak_with_uncertainty = (
            declared_smoke_peak + policy.admission_uncertainty_margin_bytes(declared_smoke_peak))
        result = self._with_marker(
            marker,
            lambda _signed: policy.projected_storage_admission(
                current_free_bytes=58_855_575_552,
                current_scoped_bytes=45 * policy.GIB,
                peak_additional_bytes=smoke_peak_with_uncertainty,
            ),
        )
        self.assertEqual(result["shared_capacity_commitment_bytes"], self.COMMITMENT_BYTES)
        self.assertLess(result["projected_free_bytes"], 50 * policy.GIB)
        self.assertFalse(result["admitted"])
        self.assertIn("projected free disk is below the 50 GiB normal minimum",
                      result["blockers"])

    def test_unknown_schema_and_bad_hmac_fail_closed_without_zero_claim(self) -> None:
        unknown = self._marker(current_schema=True)
        unknown["unexpected_commitment_semantics"] = "not reviewed"
        forged = self._marker(current_schema=True)
        missing_required = self._marker(current_schema=True)
        del missing_required["unreserved_free_ceiling_bytes"]
        partial_p1 = self._marker(current_schema=True)
        del partial_p1["p1_admission_sha256"]
        for name, marker, verification_key in (
                ("unknown", unknown, self.SIGNING_KEY),
                ("bad_hmac", forged, b"different-verification-key"),
                ("missing_required", missing_required, self.SIGNING_KEY),
                ("partial_p1_identity", partial_p1, self.SIGNING_KEY)):
            with self.subTest(name=name):
                result = self._with_marker(
                    marker,
                    lambda _signed: policy.projected_storage_admission(
                        current_free_bytes=70 * policy.GIB,
                        current_scoped_bytes=40 * policy.GIB,
                        peak_additional_bytes=policy.GIB,
                    ),
                    verification_key=verification_key,
                )
                self.assertFalse(result["admitted"])
                self.assertIsNone(result["shared_capacity_commitment_bytes"])
                self.assertIn("authenticated shared VM capacity commitment is unavailable",
                              result["blockers"][0])
        valid_marker = self._marker(current_schema=True)

        def tampered_release_after_write(signed):
            self._write_test_deferred_inactivation(signed)
            release_path = policy.VM_CAPACITY_DOMAIN_DEFERRED_INACTIVATION
            release = json.loads(release_path.read_text())
            release["effective_committed_growth_bytes"] = 1
            release_path.write_bytes(policy._canonical_pretty_json_bytes(release))
            return policy.projected_storage_admission(
                current_free_bytes=70 * policy.GIB,
                current_scoped_bytes=40 * policy.GIB,
                peak_additional_bytes=policy.GIB,
            )

        invalid_release_result = self._with_marker(valid_marker, tampered_release_after_write)
        self.assertFalse(invalid_release_result["admitted"])
        self.assertIsNone(invalid_release_result["shared_capacity_commitment_bytes"])
        self.assertIn("authenticated shared VM capacity commitment is unavailable",
                      invalid_release_result["blockers"][0])

        invalid_review_cases = (
            ("equal_receipt_time", {"reviewed_at_unix": 1_800_000_001.0}, 1_800_000_010.0, 1),
            ("equal_release_time", {}, 1_800_000_005.0, 1),
            ("missing_status", {"status": None}, 1_800_000_010.0, 1),
            ("wrong_model", {"model_identity": "unverified model"}, 1_800_000_010.0, 1),
            ("boolean_review_schema", {"schema_version": True}, 1_800_000_010.0, 1),
            ("boolean_release_schema", {}, 1_800_000_010.0, True),
            ("stale_review", {}, 1_800_001_806.0, 1),
            ("wrong_branch_path", {
                "reviewed_branch_retirement_receipt_path": "other-branch.json"},
             1_800_000_010.0, 1),
        )
        for name, overrides, release_time, release_schema in invalid_review_cases:
            with self.subTest(deferred_review=name):
                marker = self._marker(current_schema=True)
                def reject_invalid_deferred_review(signed):
                    self._write_test_deferred_inactivation(
                        signed, review_overrides=overrides,
                        inactivation_created_at_unix=release_time,
                        inactivation_schema_version=release_schema)
                    return policy.projected_storage_admission(
                        current_free_bytes=70 * policy.GIB,
                        current_scoped_bytes=40 * policy.GIB,
                        peak_additional_bytes=policy.GIB,
                    )
                result = self._with_marker(marker, reject_invalid_deferred_review)
                self.assertFalse(result["admitted"])
                self.assertIsNone(result["shared_capacity_commitment_bytes"])
                self.assertIn("authenticated shared VM capacity commitment is unavailable",
                              result["blockers"][0])


class G0CReviewedFileBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-g0c-live-binding-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.storage = self.repo / "experiments/storage"
        self.storage.mkdir(parents=True)
        self.review_path = self.storage / policy.G0C_REVIEW_RECORD_NAME
        self.reviewed_files: dict[str, dict[str, object]] = {}
        for field, relative_path in policy.G0C_REVIEW_REQUIRED_FILES.items():
            contents = ("synthetic current G0C input " + relative_path + "\n").encode()
            (self.storage / relative_path).write_bytes(contents)
        self.reviewed_files = policy.g0c_reviewed_files(self.repo, policy.G0C_REVIEW_REQUIRED_FILES)
        self.attestation: dict[str, object] = {"supplemental_evidence_sha256": {}}
        self.patcher = mock.patch.object(policy, "REPO", self.repo)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self._write_review()

    def test_r95_current_review_name_is_candidate26_and_prior_routes_stay_historical(self) -> None:
        self.assertEqual(
            policy.current_g0c_review_record_name({"FG_METAL_R91_PLAN_PATH": "plan17.json"}),
            "g0c-launcher-independent-review-R91-CANDIDATE-21.json",
        )
        self.assertEqual(policy.current_g0c_review_record_name({}),
                         policy.R90_G0C_REVIEW_RECORD_NAME)
        self.assertEqual(
            policy.current_g0c_review_record_name({"FG_METAL_R92_PLAN_PATH": "plan20.json"}),
            "g0c-launcher-independent-review-R92-CANDIDATE-23.json",
        )
        self.assertEqual(
            policy.current_g0c_review_record_name({"FG_METAL_R93_PLAN_PATH": "plan21.json"}),
            "g0c-launcher-independent-review-R93-CANDIDATE-24.json",
        )
        self.assertEqual(
            policy.current_g0c_review_record_name({"FG_METAL_R94_PLAN_PATH": "plan22.json"}),
            "g0c-launcher-independent-review-R94-CANDIDATE-25.json",
        )
        self.assertEqual(
            policy.current_g0c_review_record_name({"FG_METAL_R95_PLAN_PATH": "plan24.json"}),
            "g0c-launcher-independent-review-R95-CANDIDATE-26.json",
        )
        self.assertIn("g0c-launcher-independent-review-R91-CANDIDATE-20.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R91-FRESH-G0C-REVIEW-CANDIDATE-20.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-20.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R91-FRESH-G0C-REVIEW-CANDIDATE-21.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("g0c-launcher-independent-review-R92-CANDIDATE-22.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R92-FRESH-G0C-REVIEW-CANDIDATE-22.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-22.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("g0c-launcher-independent-review-R93-CANDIDATE-24.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("g0c-launcher-independent-review-R94-CANDIDATE-25.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R94-FRESH-G0C-REVIEW-CANDIDATE-25.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-25.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("g0c-launcher-independent-review-R95-CANDIDATE-26.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R95-FRESH-G0C-REVIEW-CANDIDATE-26.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-26.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R93-FRESH-G0C-REVIEW-CANDIDATE-24.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-24.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("g0c-launcher-independent-review-R92-CANDIDATE-23.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R92-FRESH-G0C-REVIEW-CANDIDATE-23.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-23.json",
                      policy.GATE0_SUPPLEMENTAL_EVIDENCE)

    def test_r94_attestation_requires_exact_candidate25_plan23_and_selects_r94_review(self) -> None:
        names = (
            "g0c-launcher-independent-review-R94-CANDIDATE-25.json",
            "R6-A-R94-FRESH-G0C-REVIEW-CANDIDATE-25.json",
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-25.json",
        )
        documents = (
            {"status": "PASS", "findings": [],
             "review_scope": "R94 Candidate25 global launcher/control review",
             "reviewed_files": self.reviewed_files},
            {"candidate_id": "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-25",
             "plan_path": "experiments/storage/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-23.json"},
            {"candidate_id": "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-25",
             "status": "FROZEN_DEPENDENCY_CLOSURE"},
        )
        supplemental: dict[str, str] = {}
        for name, document in zip(names, documents):
            raw = (json.dumps(document, sort_keys=True) + "\n").encode()
            path = policy.gate0_supplemental_path(self.repo, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            supplemental[name] = hashlib.sha256(raw).hexdigest()

        attestation = {"supplemental_evidence_sha256": supplemental}

        self.assertTrue(policy._gate0_is_r94_attestation(attestation))
        evidence_names, required_files = policy._gate0_evidence_contract(attestation)
        self.assertEqual(evidence_names, policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertEqual(required_files, policy.G0C_REVIEW_REQUIRED_FILES)
        self.assertEqual(policy._g0c_reviewed_file_blockers(attestation), [])

        wrong_hash = dict(attestation)
        wrong_hash["supplemental_evidence_sha256"] = dict(supplemental)
        wrong_hash["supplemental_evidence_sha256"][names[0]] = "0" * 64
        self.assertFalse(policy._gate0_is_r94_attestation(wrong_hash))

        for field, wrong_value in (("candidate_id", "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-24"),
                                   ("plan_path", "experiments/storage/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-21.json")):
            with self.subTest(field=field):
                candidate_review = dict(documents[1])
                candidate_review[field] = wrong_value
                raw = (json.dumps(candidate_review, sort_keys=True) + "\n").encode()
                path = policy.gate0_supplemental_path(self.repo, names[1])
                path.write_bytes(raw)
                bad = dict(supplemental)
                bad[names[1]] = hashlib.sha256(raw).hexdigest()
                self.assertFalse(policy._gate0_is_r94_attestation(
                    {"supplemental_evidence_sha256": bad}))

    def test_r95_attestation_requires_exact_candidate26_plan24_and_selects_r95_review(self) -> None:
        names = (
            "g0c-launcher-independent-review-R95-CANDIDATE-26.json",
            "R6-A-R95-FRESH-G0C-REVIEW-CANDIDATE-26.json",
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-26.json",
        )
        documents = (
            {"status": "PASS", "findings": [],
             "review_scope": "R95 Candidate26 global launcher/control review",
             "reviewed_files": self.reviewed_files},
            {"candidate_id": "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-26",
             "plan_path": "experiments/storage/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-24.json"},
            {"candidate_id": "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-26",
             "status": "FROZEN_DEPENDENCY_CLOSURE"},
        )
        supplemental: dict[str, str] = {}
        for name, document in zip(names, documents):
            raw = (json.dumps(document, sort_keys=True) + "\n").encode()
            path = policy.gate0_supplemental_path(self.repo, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            supplemental[name] = hashlib.sha256(raw).hexdigest()

        attestation = {"supplemental_evidence_sha256": supplemental}
        self.assertTrue(policy._gate0_is_r95_attestation(attestation))
        evidence_names, required_files = policy._gate0_evidence_contract(attestation)
        self.assertEqual(evidence_names, policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertEqual(required_files, policy.G0C_REVIEW_REQUIRED_FILES)
        self.assertEqual(policy._g0c_reviewed_file_blockers(attestation), [])

        wrong_hash = dict(attestation)
        wrong_hash["supplemental_evidence_sha256"] = dict(supplemental)
        wrong_hash["supplemental_evidence_sha256"][names[0]] = "0" * 64
        self.assertFalse(policy._gate0_is_r95_attestation(wrong_hash))

    def test_r96_attestation_requires_exact_candidate27_plan25_and_selects_r96_review(self) -> None:
        names = (
            "g0c-launcher-independent-review-R96-CANDIDATE-27.json",
            "R6-A-R96-FRESH-G0C-REVIEW-CANDIDATE-27.json",
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-27.json",
        )
        documents = (
            {"status": "PASS", "findings": [],
             "review_scope": "R96 Candidate27 global launcher/control review",
             "reviewed_files": self.reviewed_files},
            {"candidate_id": "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-27",
             "plan_path": "experiments/storage/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-25.json"},
            {"candidate_id": "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-27",
             "status": "FROZEN_DEPENDENCY_CLOSURE"},
        )
        supplemental: dict[str, str] = {}
        for name, document in zip(names, documents):
            raw = (json.dumps(document, sort_keys=True) + "\n").encode()
            path = policy.gate0_supplemental_path(self.repo, name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            supplemental[name] = hashlib.sha256(raw).hexdigest()

        attestation = {"supplemental_evidence_sha256": supplemental}
        self.assertTrue(policy._gate0_is_r96_attestation(attestation))
        evidence_names, required_files = policy._gate0_evidence_contract(attestation)
        self.assertEqual(evidence_names, policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertEqual(required_files, policy.G0C_REVIEW_REQUIRED_FILES)
        self.assertEqual(policy._g0c_reviewed_file_blockers(attestation), [])

        wrong_hash = dict(attestation)
        wrong_hash["supplemental_evidence_sha256"] = dict(supplemental)
        wrong_hash["supplemental_evidence_sha256"][names[0]] = "0" * 64
        self.assertFalse(policy._gate0_is_r96_attestation(wrong_hash))

    def test_r96_current_review_name_is_candidate27_and_prior_routes_stay_historical(self) -> None:
        self.assertEqual(
            policy.current_g0c_review_record_name({"FG_METAL_R96_PLAN_PATH": "plan25.json"}),
            "g0c-launcher-independent-review-R96-CANDIDATE-27.json",
        )
        self.assertEqual(
            policy.current_g0c_review_record_name({"FG_METAL_R95_PLAN_PATH": "plan24.json"}),
            "g0c-launcher-independent-review-R95-CANDIDATE-26.json",
        )
        for name in (
                "g0c-launcher-independent-review-R96-CANDIDATE-27.json",
                "R6-A-R96-FRESH-G0C-REVIEW-CANDIDATE-27.json",
                "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-27.json"):
            self.assertIn(name, policy.GATE0_SUPPLEMENTAL_EVIDENCE)

    def _write_review(self, *, reviewed_files: dict[str, dict[str, object]] | None = None,
                      status: str = "PASS", findings: list[object] | None = None) -> None:
        raw = (json.dumps({
            "status": status,
            "findings": [] if findings is None else findings,
            "reviewed_files": self.reviewed_files if reviewed_files is None else reviewed_files,
        }, sort_keys=True) + "\n").encode()
        self.review_path.write_bytes(raw)
        supplemental = self.attestation["supplemental_evidence_sha256"]
        assert isinstance(supplemental, dict)
        supplemental[policy.G0C_REVIEW_RECORD_NAME] = hashlib.sha256(raw).hexdigest()

    def test_exact_pinned_review_hashes_bind_all_normal_admission_inputs(self) -> None:
        self.assertEqual(policy._g0c_reviewed_file_blockers(self.attestation), [])

    def test_failed_output_disposition_regression_is_a_required_review_input(self) -> None:
        self.assertEqual(
            policy.G0C_REVIEW_REQUIRED_FILES["failed_output_disposition_tests"],
            "test_failed_output_disposition.py",
        )
        self.assertIn("failed_output_disposition_tests", self.reviewed_files)
        self.assertEqual(policy._g0c_reviewed_file_blockers(self.attestation), [])

    def test_producer_derives_path_digest_and_size_from_same_read(self) -> None:
        relative_path = "experiments/storage/source.bin"
        contents = b"one byte source payload\x00"
        (self.storage / "source.bin").write_bytes(contents)
        record = policy.g0c_reviewed_file_record(self.repo, relative_path)
        self.assertEqual(record, {
            "path": relative_path,
            "sha256": hashlib.sha256(contents).hexdigest(),
            "size_bytes": len(contents),
        })
        with self.assertRaisesRegex(ValueError, "duplicate paths"):
            policy.g0c_reviewed_files(self.repo, {"first": "same.py", "second": "same.py"})

    def test_record_schema_rejects_stale_and_malformed_values(self) -> None:
        relative_path = policy.G0C_REVIEW_REQUIRED_FILES["vm_capacity_controller"]
        expected = self.reviewed_files["vm_capacity_controller"]
        for name, mutate in (
                ("stale size", lambda row: row.update(size_bytes=row["size_bytes"] + 1)),
                ("negative size", lambda row: row.update(size_bytes=-1)),
                ("string size", lambda row: row.update(size_bytes="1")),
                ("boolean size", lambda row: row.update(size_bytes=True)),
                ("stale digest", lambda row: row.update(sha256="0" * 64)),
                ("wrong path", lambda row: row.update(path="experiments/storage/other.py")),
                ("extra key", lambda row: row.update(unexpected=True))):
            with self.subTest(name=name):
                malformed = dict(self.reviewed_files)
                record = dict(expected)
                mutate(record)
                malformed["vm_capacity_controller"] = record
                self._write_review(reviewed_files=malformed)
                self.assertTrue(policy._g0c_reviewed_file_blockers(self.attestation))

    def test_duplicate_path_and_unreadable_file_fail_closed(self) -> None:
        malformed = dict(self.reviewed_files)
        duplicate = dict(malformed["vm_capacity_tests"])
        duplicate["path"] = malformed["vm_capacity_controller"]["path"]
        malformed["vm_capacity_tests"] = duplicate
        self._write_review(reviewed_files=malformed)
        self.assertTrue(policy._g0c_reviewed_file_blockers(self.attestation))

        self._write_review()
        (self.storage / policy.G0C_REVIEW_REQUIRED_FILES["vm_capacity_controller"]).unlink()
        self.assertTrue(policy._g0c_reviewed_file_blockers(self.attestation))

    def test_duplicate_json_keys_fail_closed(self) -> None:
        raw = b'{"status":"PASS","status":"PASS","findings":[],"reviewed_files":{}}\n'
        self.review_path.write_bytes(raw)
        supplemental = self.attestation["supplemental_evidence_sha256"]
        assert isinstance(supplemental, dict)
        supplemental[policy.G0C_REVIEW_RECORD_NAME] = hashlib.sha256(raw).hexdigest()
        self.assertTrue(policy._g0c_reviewed_file_blockers(self.attestation))

    def test_current_file_change_blocks_even_when_review_record_stays_pinned(self) -> None:
        changed = self.storage / policy.G0C_REVIEW_REQUIRED_FILES["vm_capacity_controller"]
        changed.write_bytes(b"synthetic post-review change\n")

        blockers = policy._g0c_reviewed_file_blockers(self.attestation)

        self.assertEqual(len(blockers), 1)
        self.assertIn("does not bind current experiments/storage/vm_capacity_domain.py", blockers[0])

    def test_missing_or_mismatched_supplemental_pin_fails_closed(self) -> None:
        supplemental = self.attestation["supplemental_evidence_sha256"]
        assert isinstance(supplemental, dict)
        # Preserve a separate R90-only pin so this malformed R90 attestation
        # remains distinguishable from an older signed R89 Gate record.
        supplemental["R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-11.json"] = "0" * 64
        supplemental.pop(policy.G0C_REVIEW_RECORD_NAME)
        self.assertTrue(any("not the exact supplemental evidence pinned" in item
                            for item in policy._g0c_reviewed_file_blockers(self.attestation)))

    def test_missing_or_added_review_binding_fails_closed(self) -> None:
        incomplete = dict(self.reviewed_files)
        incomplete.pop("vm_capacity_tests")
        self._write_review(reviewed_files=incomplete)
        self.assertTrue(any("omits or adds required reviewed_files bindings" in item
                            for item in policy._g0c_reviewed_file_blockers(self.attestation)))

    def test_boolean_size_is_not_accepted_as_reviewed_byte_count(self) -> None:
        relative_path = policy.G0C_REVIEW_REQUIRED_FILES["vm_capacity_controller"]
        contents = b"x"
        (self.storage / relative_path).write_bytes(contents)
        malformed = dict(self.reviewed_files)
        malformed["vm_capacity_controller"] = {
            "path": (Path("experiments/storage") / relative_path).as_posix(),
            "sha256": hashlib.sha256(contents).hexdigest(),
            "size_bytes": True,
        }
        self._write_review(reviewed_files=malformed)
        self.assertTrue(any("does not bind current experiments/storage/vm_capacity_domain.py" in item
                            for item in policy._g0c_reviewed_file_blockers(self.attestation)))

        extra = dict(self.reviewed_files)
        extra["unreviewed_file"] = {"path": "x", "sha256": "0" * 64, "size_bytes": 1}
        self._write_review(reviewed_files=extra)
        self.assertTrue(any("omits or adds required reviewed_files bindings" in item
                            for item in policy._g0c_reviewed_file_blockers(self.attestation)))

    def test_malformed_or_nonpassing_review_fails_closed(self) -> None:
        self.review_path.write_text("not json\n", encoding="utf-8")
        self.assertTrue(any("record is missing or malformed" in item
                            for item in policy._g0c_reviewed_file_blockers(self.attestation)))

        self._write_review(status="FAIL")
        self.assertTrue(any("does not PASS" in item
                            for item in policy._g0c_reviewed_file_blockers(self.attestation)))


class NonleaseAllocationDiagnosticTests(unittest.TestCase):
    def _row(self, path: str, *, inode: int, allocated: int = 4096,
             size: int | None = None, classification: str = policy.ROOT_CLASS_OPTIONAL_EPHEMERAL,
             category: str = "project_temp", mtime_ns: int = 1) -> dict[str, object]:
        return {
            "path": path,
            "root": "/private/tmp/FGMetalSynthetic",
            "classification": classification,
            "category": category,
            "kind": "regular_file",
            "size_bytes": allocated if size is None else size,
            "allocated_bytes": allocated,
            "owner_uid": 501,
            "owner_gid": 20,
            "mode": 0o600,
            "link_count": 1,
            "device": 7,
            "inode": inode,
            "mtime_ns": mtime_ns,
            "ctime_ns": 1,
        }

    def _diagnostic(self, baseline: list[dict[str, object]],
                    current: list[dict[str, object]], *,
                    observed_delta: int,
                    exclusions: tuple[Path, ...] = ()) -> dict[str, object]:
        baseline_snapshot = policy._make_nonlease_allocation_snapshot(baseline, ())
        excluded = set(exclusions)
        current_in_scope = [
            row for row in current
            if not policy._is_project_usage_path_excluded(Path(str(row["path"])), excluded)
        ]
        current_snapshot = policy._make_nonlease_allocation_snapshot(current_in_scope, exclusions)
        baseline_total = policy._allocation_snapshot_total_bytes(baseline_snapshot)
        current_total = policy._allocation_snapshot_total_bytes(current_snapshot)
        attestation = {"measurements": {
            "fgmetal_scoped_observed_nonlease_allocated_inode_bytes": baseline_total,
            "nonlease_allocation_snapshot": baseline_snapshot,
        }}
        return policy._nonlease_storage_drift_diagnostic(
            attestation=attestation,
            gate_attestation_sha256="a" * 64,
            experiment_id="r6a-r77-synthetic",
            exclusion_paths=exclusions,
            decision_observed_nonlease_bytes=baseline_total + observed_delta
            if observed_delta else current_total,
            current_snapshot=current_snapshot,
        )

    def test_identical_snapshot_has_no_drift(self) -> None:
        row = self._row("/private/tmp/FGMetalSynthetic/baseline.log", inode=11)
        diagnostic = self._diagnostic([row], [dict(row)], observed_delta=0)
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["aggregate_allocated_bytes_delta"], 0)
        self.assertEqual(diagnostic["changed_paths"], [])

    def test_unrelated_optional_root_growth_is_attributed_without_exemption(self) -> None:
        baseline = [self._row("/private/tmp/FGMetalSynthetic/baseline.log", inode=11)]
        created = self._row("/private/tmp/FGMetalSynthetic/gate-preflight.stdout.log",
                            inode=12, allocated=8192)
        diagnostic = self._diagnostic(baseline, baseline + [created], observed_delta=8192)
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["aggregate_allocated_bytes_delta"], 8192)
        self.assertEqual(diagnostic["changed_paths"][0]["change"], "created")
        self.assertEqual(diagnostic["changed_paths"][0]["ownership_class"],
                         policy.ROOT_CLASS_OPTIONAL_EPHEMERAL)
        self.assertEqual(diagnostic["changed_paths"][0]["path"], created["path"])

    def test_unrelated_deletion_is_attributed_with_negative_allocation_delta(self) -> None:
        retained = self._row("/private/tmp/FGMetalSynthetic/retained.bin", inode=11)
        deleted = self._row("/private/tmp/FGMetalSynthetic/deleted.bin", inode=12)
        diagnostic = self._diagnostic([retained, deleted], [retained], observed_delta=-4096)
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["aggregate_allocated_bytes_delta"], -4096)
        self.assertEqual(diagnostic["changed_paths"][0]["change"], "deleted")

    def test_exact_signed_run_exclusion_is_applied_to_both_sides(self) -> None:
        baseline = [self._row("/private/tmp/FGMetalSynthetic/baseline.bin", inode=11)]
        output = Path("/private/tmp/FGMetalSynthetic/authorized-output")
        protocol_file = self._row(str(output / "result.json"), inode=12)
        diagnostic = self._diagnostic(
            baseline, baseline + [protocol_file], observed_delta=0, exclusions=(output,))
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["changed_paths"], [])
        self.assertEqual(diagnostic["comparison_rule"],
                         "exact_nonlease_allocated_inode_deduplicated_bytes_equality")

    def test_protocol_owned_path_without_signed_exclusion_remains_visible(self) -> None:
        baseline = [self._row("/private/tmp/FGMetalSynthetic/baseline.bin", inode=11)]
        protocol = self._row("/private/tmp/FGMetalSynthetic/lease-claim.json",
                             inode=12, classification=policy.ROOT_CLASS_MANDATORY,
                             category="project_cache")
        diagnostic = self._diagnostic(baseline, baseline + [protocol], observed_delta=4096)
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["aggregate_allocated_bytes_delta"], 4096)
        self.assertEqual(diagnostic["changed_paths"][0]["ownership_class"],
                         policy.ROOT_CLASS_MANDATORY)

    def test_hardlinked_alias_is_not_double_counted(self) -> None:
        first = self._row("/private/tmp/FGMetalSynthetic/a.bin", inode=11)
        alias = dict(first, path="/private/tmp/FGMetalSynthetic/b.bin", link_count=2)
        snapshot = policy._make_nonlease_allocation_snapshot([first, alias], ())
        self.assertEqual(policy._allocation_snapshot_total_bytes(snapshot), 4096)
        diagnostic = self._diagnostic([first], [first, alias], observed_delta=0)
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["aggregate_allocated_bytes_delta"], 0)
        self.assertEqual(diagnostic["changed_paths"][0]["change"], "created")

    def test_multiple_deltas_are_sorted_bounded_and_hash_stable(self) -> None:
        changed = [
            self._row(f"/private/tmp/FGMetalSynthetic/new-{index:03d}.bin",
                      inode=1000 + index)
            for index in range(policy.NONLEASE_DRIFT_DIAGNOSTIC_MAX_PATHS + 4)
        ]
        first = self._diagnostic([], changed, observed_delta=4096 * len(changed))
        second = self._diagnostic([], list(reversed(changed)),
                                  observed_delta=4096 * len(changed))
        self.assertEqual(first["status"], "COMPLETE")
        self.assertEqual(first["changed_path_count"], len(changed))
        self.assertEqual(len(first["changed_paths"]),
                         policy.NONLEASE_DRIFT_DIAGNOSTIC_MAX_PATHS)
        self.assertEqual(first["changed_path_rows_truncated"], 4)
        self.assertEqual(first["changed_path_rows_sha256"], second["changed_path_rows_sha256"])
        paths = [row["path"] for row in first["changed_paths"]]
        self.assertEqual(paths, sorted(paths))

    def test_metadata_change_is_recorded_even_when_allocation_is_unchanged(self) -> None:
        before = self._row("/private/tmp/FGMetalSynthetic/metadata.bin", inode=11)
        after = dict(before, mtime_ns=2)
        diagnostic = self._diagnostic([before], [after], observed_delta=0)
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["changed_paths"][0]["change"], "metadata_changed")

    def test_claim_rewrite_and_unrelated_growth_share_decision_observation(self) -> None:
        claim_path = "/private/tmp/FGMetalSynthetic/smoke-claim.json"
        unrelated_path = "/private/tmp/FGMetalSynthetic/unrelated.bin"
        claim_before = self._row(claim_path, inode=11, size=463,
                                 classification=policy.ROOT_CLASS_MANDATORY,
                                 category="project_cache")
        claim_after = dict(claim_before, size_bytes=1370, mtime_ns=2, ctime_ns=2)
        unrelated = self._row(unrelated_path, inode=12, allocated=4096)
        output_path = Path("/private/tmp/FGMetalSynthetic/authorized-output")
        exclusions = (output_path,)
        baseline_snapshot = policy._make_nonlease_allocation_snapshot(
            [claim_before], exclusions)
        current_snapshot = policy._make_nonlease_allocation_snapshot(
            [claim_after, unrelated], exclusions)
        attestation = {"measurements": {
            "fgmetal_scoped_observed_nonlease_allocated_inode_bytes": 4096,
            "nonlease_allocation_snapshot": baseline_snapshot,
        }}
        diagnostic = policy._nonlease_storage_drift_diagnostic(
            attestation=attestation,
            gate_attestation_sha256="a" * 64,
            experiment_id="r88-candidate7-mixed-claim-growth",
            exclusion_paths=exclusions,
            decision_observed_nonlease_bytes=8192,
            current_snapshot=current_snapshot,
        )
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["aggregate_allocated_bytes_delta"], 4096)
        self.assertEqual(diagnostic["snapshot_baseline_nonlease_allocated_bytes"], 4096)
        self.assertEqual(diagnostic["snapshot_current_nonlease_allocated_bytes"], 8192)
        self.assertEqual(diagnostic["comparison_observed_nonlease_allocated_bytes"], 8192)
        self.assertEqual({row["path"] for row in diagnostic["changed_paths"]},
                         {claim_path, unrelated_path})


class ProjectUsageExclusionTraversalTests(unittest.TestCase):
    def test_path_snapshot_reconciles_with_exclusion_and_hardlink_measurement(self) -> None:
        with tempfile.TemporaryDirectory(prefix="usage-snapshot-", dir="/private/tmp") as temp:
            root = Path(temp) / "repo"
            root.mkdir()
            payload = root / "payload.bin"
            payload.write_bytes(b"same-inode" * 512)
            alias = root / "payload-alias.bin"
            os.link(payload, alias)
            output = root / "authorized-output"
            output.mkdir()
            (output / "result.bin").write_bytes(b"excluded" * 512)
            roots = [policy.DiscoveredProjectRoot(
                root, policy.ROOT_CLASS_MANDATORY, "main_repo")]
            with mock.patch.object(policy, "_classified_project_roots", return_value=roots):
                measured = policy.measure_project_usage(
                    exclude_paths=(output,), include_allocation_snapshot=True)

            snapshot = measured["nonlease_allocation_snapshot"]
            self.assertEqual(
                policy._allocation_snapshot_total_bytes(snapshot),
                measured["allocated_inode_deduplicated_bytes_excluding_paths"])
            rows = snapshot["entries"]
            self.assertEqual({row["path"] for row in rows}, {str(payload), str(alias)})
            self.assertEqual(len(rows), 2)

    def test_path_snapshot_overflow_fails_closed(self) -> None:
        snapshot = policy._make_nonlease_allocation_snapshot([], ())
        snapshot["entry_count"] = policy.NONLEASE_ALLOCATION_SNAPSHOT_MAX_PATHS + 1
        with self.assertRaisesRegex(ValueError, "malformed"):
            policy._allocation_snapshot_total_bytes(snapshot)

    def test_exclusion_lookup_work_tracks_path_depth_not_exclusion_count(self) -> None:
        class CountingPathSet(set[Path]):
            membership_checks = 0

            def __contains__(self, value: object) -> bool:
                self.membership_checks += 1
                return super().__contains__(value)

        exclusions = CountingPathSet(Path(f"/unrelated/exclusion-{index}")
                                      for index in range(2_000))
        candidate = Path("/private/tmp/project/experiments/output/result.bin")

        self.assertFalse(policy._is_project_usage_path_excluded(candidate, exclusions))
        self.assertLessEqual(exclusions.membership_checks, len(candidate.parts))

        exclusions.clear()
        exclusions.membership_checks = 0
        exclusions.add(candidate.parent.parent)
        self.assertTrue(policy._is_project_usage_path_excluded(candidate, exclusions))
        self.assertEqual(exclusions.membership_checks, 3)

        exclusions.membership_checks = 0
        exclusions.clear()
        exclusions.add(candidate)
        self.assertTrue(policy._is_project_usage_path_excluded(candidate, exclusions))
        self.assertEqual(exclusions.membership_checks, 1)

    def test_git_bytes_are_included_and_overlapping_roots_deduplicate_allocation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="usage-measure-overlap-", dir="/private/tmp") as temp:
            root = Path(temp) / "repo"
            nested = root / "experiments"
            git_object = root / ".git/objects/pack/project.pack"
            nested_file = nested / "evidence.bin"
            git_object.parent.mkdir(parents=True)
            nested.mkdir(parents=True)
            git_object.write_bytes(b"git-object" * 512)
            nested_file.write_bytes(b"nested-evidence" * 512)
            roots = [
                policy.DiscoveredProjectRoot(root, policy.ROOT_CLASS_MANDATORY, "synthetic-repo"),
                policy.DiscoveredProjectRoot(nested, policy.ROOT_CLASS_MANDATORY, "synthetic-nested-root"),
            ]

            with mock.patch.object(policy, "_classified_project_roots", return_value=roots):
                measured = policy.measure_project_usage()

            expected_allocated = (git_object.stat().st_blocks + nested_file.stat().st_blocks) * 512
            self.assertEqual(measured["allocated_inode_deduplicated_bytes"], expected_allocated)
            self.assertEqual(measured["file_count"], 3)


class PrefixLeaseAllocationAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-prefix-allocation-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.vm_capacity_root = Path(self.temp.name) / "vm-capacity-domain"
        self.vm_capacity_root.mkdir(mode=0o700)
        self.vm_capacity_marker = self.vm_capacity_root / "active-session.json"
        self._capacity_root_patchers = [
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ROOT", self.vm_capacity_root),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ACTIVE_MARKER", self.vm_capacity_marker),
        ]
        for patcher in self._capacity_root_patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.key_path = Path(self.temp.name) / "signing-key"
        self.key = policy._manifest_signing_key
        self._key_patch = mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", self.key_path)
        self._key_patch.start()
        self.addCleanup(self._key_patch.stop)
        policy._manifest_signing_key(create=True)
        self.attested_scope = 47_831_126_016
        self.attested_free = 68_871_286_784
        self.nonlease_scope = 47_865_233_408
        self.gate0_observed_scope = self.nonlease_scope
        self.prefix_bytes = 2_303_463_424
        self.output_bytes = 0
        self.current_scope = 50_168_705_024
        self.current_free = 67_814_637_568
        self.expected_prefix = 3 * policy.GIB
        self.persistent = 100 * policy.MIB
        self.projected_peak = 3_658_691_380
        self.binding = {
            "experiment_id": "z4-regression",
            "pid": os.getpid(),
            "lease_token": "a" * 64,
            "prefix_path": "/tmp/z4-regression-prefix",
            "active_marker_path": "/tmp/z4-active/z4-regression.json",
            "output_dir": "/tmp/z4-regression-output",
        }
        self.receipt = self._receipt()

    def _receipt(self, **overrides: object) -> dict[str, object]:
        record: dict[str, object] = {
            "schema_version": 1,
            **self.binding,
            "pre_materialization_free_bytes": 67_819_061_248,
            "post_materialization_free_bytes": 67_816_251_392,
            "gate0_attested_observed_scope_bytes": self.gate0_observed_scope,
            "gate0_attested_nonlease_scope_bytes": self.gate0_observed_scope,
            "pre_materialization_nonlease_scope_bytes": self.nonlease_scope,
            "post_materialization_nonlease_scope_bytes": self.nonlease_scope,
            "pre_materialization_output_allocated_bytes": self.output_bytes,
            "post_materialization_output_allocated_bytes": self.output_bytes,
            "materialized_prefix_allocated_bytes": self.prefix_bytes,
            "expected_prefix_bytes": self.expected_prefix,
            "estimated_persistent_bytes": self.persistent,
            "estimated_projected_new_disk_bytes": self.projected_peak,
            "post_materialization_scope_bytes": self.current_scope,
            "created_at_unix": time.time(),
            **overrides,
        }
        return policy._seal_storage_evidence(record, "prefix_materialization_hmac_sha256")

    def _blockers(self, *, receipt: dict[str, object] | None = None,
                  current_free: int | None = None,
                  current_scope: int | None = None,
                  current_nonlease_scope: int | None = None,
                  prefix_bytes: int | None = None,
                  output_bytes: int | None = None,
                  other_leases: int = 0) -> list[str]:
        selected_receipt = receipt or self.receipt
        return policy._prefix_lease_accounting_blockers(
            selected_receipt,
            current_free_bytes=self.current_free if current_free is None else current_free,
            current_project_bytes=self.current_scope if current_scope is None else current_scope,
            current_nonlease_project_bytes=(
                self.nonlease_scope if current_nonlease_scope is None else current_nonlease_scope),
            attested_nonlease_project_bytes=self.gate0_observed_scope,
            current_prefix_allocated_bytes=self.prefix_bytes if prefix_bytes is None else prefix_bytes,
            current_output_allocated_bytes=self.output_bytes if output_bytes is None else output_bytes,
            attested_free_bytes=self.attested_free,
            other_active_lease_count=other_leases,
            expected_binding=self.binding,
        )

    def test_a_no_materialization_delta_passes(self) -> None:
        receipt = self._receipt(materialized_prefix_allocated_bytes=0)
        self.assertEqual(self._blockers(receipt=receipt, current_scope=self.nonlease_scope + 8_192,
                                        prefix_bytes=0), [])

    def test_b_legitimate_materialization_within_reservation_passes(self) -> None:
        self.assertEqual(self._blockers(), [])

    def test_c_materialization_near_reservation_passes_when_policy_limits_hold(self) -> None:
        near_bound = self.expected_prefix
        current_scope = self.nonlease_scope + near_bound + 8_192
        receipt = self._receipt(materialized_prefix_allocated_bytes=near_bound,
                                post_materialization_scope_bytes=current_scope)
        self.assertEqual(self._blockers(receipt=receipt, current_scope=current_scope,
                                        prefix_bytes=near_bound), [])

    def test_d_materialization_over_authenticated_reservation_fails(self) -> None:
        over_bound = self.expected_prefix + 1
        receipt = self._receipt(materialized_prefix_allocated_bytes=over_bound)
        self.assertTrue(any("signed reservation receipt" in blocker
                            for blocker in self._blockers(receipt=receipt, prefix_bytes=over_bound)))

    def test_e_unrelated_scoped_growth_during_active_lease_fails(self) -> None:
        excess = 64 * policy.MIB
        self.assertTrue(any("unrelated non-lease storage changed since Gate 0" in blocker
                            for blocker in self._blockers(
                                current_scope=self.current_scope + excess,
                                current_nonlease_scope=self.nonlease_scope + excess)))

    def test_materialization_rebase_rejects_concurrent_unrelated_growth(self) -> None:
        growth = 64 * policy.MIB
        receipt = self._receipt(
            post_materialization_nonlease_scope_bytes=self.nonlease_scope + growth)
        blockers = self._blockers(
            receipt=receipt,
            current_scope=self.current_scope + growth,
            current_nonlease_scope=self.nonlease_scope + growth)
        self.assertTrue(any("changed during PrefixLease materialization" in blocker
                            for blocker in blockers))

    def test_gate0_to_pre_materialization_growth_fails_without_tolerance(self) -> None:
        excess = 64 * policy.MIB
        receipt = self._receipt(
            gate0_attested_nonlease_scope_bytes=self.attested_scope,
            pre_materialization_nonlease_scope_bytes=self.nonlease_scope,
            post_materialization_nonlease_scope_bytes=self.nonlease_scope)
        self.assertTrue(any("unrelated non-lease storage changed since Gate 0" in blocker
                            for blocker in self._blockers(
                                receipt=receipt,
                                current_scope=self.current_scope,
                                current_nonlease_scope=self.attested_scope + excess)))

    def test_f_second_active_prefix_lease_fails(self) -> None:
        self.assertTrue(any("another or interrupted PrefixLease" in blocker
                            for blocker in self._blockers(other_leases=1)))

    def test_g_wrong_or_stale_lease_binding_fails(self) -> None:
        wrong_lease = self._receipt(experiment_id="different-experiment")
        self.assertTrue(any("stale or different lease" in blocker
                            for blocker in self._blockers(receipt=wrong_lease)))
        tampered = dict(self.receipt)
        tampered["lease_token"] = "b" * 64
        self.assertTrue(any("unauthenticated" in blocker
                            for blocker in self._blockers(receipt=tampered)))

    def test_h_projected_free_below_50_gib_fails(self) -> None:
        blockers = self._blockers(current_free=49 * policy.GIB,
                                  current_scope=self.current_scope)
        self.assertTrue(any("50 GiB" in blocker for blocker in blockers))

    def test_i_current_or_projected_scope_over_60_gib_fails(self) -> None:
        blockers = self._blockers(current_scope=60 * policy.GIB + 1)
        self.assertTrue(any("60 GiB" in blocker for blocker in blockers))
        self.assertTrue(any("projected scoped FG-Metal usage exceeds the 60 GiB ceiling"
                            in blocker for blocker in blockers))

    def test_z3_byte_exact_regression_fails_old_guard_and_passes_lease_guard(self) -> None:
        old_delta = self.current_scope - self.attested_scope
        old_upper_bound = self.attested_scope + policy.GIB
        self.assertEqual(old_delta, 2_337_579_008)
        self.assertEqual(old_upper_bound, 48_904_867_840)
        self.assertEqual(self.current_scope - old_upper_bound, 1_263_837_184)
        self.assertEqual(self.nonlease_scope - self.attested_scope, 34_107_392)
        self.assertEqual(self.current_scope, self.nonlease_scope + self.prefix_bytes + 8_192)
        self.assertEqual(self.prefix_bytes, 2_303_463_424)
        historical_receipt = self._receipt(
            gate0_attested_nonlease_scope_bytes=self.attested_scope)
        self.assertTrue(any("stale Gate 0 storage baseline" in blocker
                            for blocker in self._blockers(receipt=historical_receipt)))
        # A fresh Gate 0 binds the exact pre-materialization observed scope.
        fresh_receipt = self._receipt(
            gate0_attested_nonlease_scope_bytes=self.nonlease_scope)
        self.assertEqual(self._blockers(receipt=fresh_receipt), [])

    def test_policy_thresholds_remain_unchanged(self) -> None:
        self.assertEqual(policy.GATE0_PROJECT_BUDGET, 60 * policy.GIB)
        self.assertEqual(policy.PROJECT_BUDGET, 60 * policy.GIB)
        self.assertEqual(policy.NORMAL_TEMPORARY_TARGET, 3 * policy.GIB)
        self.assertEqual(policy.FREE_MINIMUM, 50 * policy.GIB)
        self.assertEqual(policy.FREE_PREFERRED, 60 * policy.GIB)
        self.assertEqual(policy.PROJECT_WARNING, 52 * policy.GIB)
        self.assertEqual(policy.FREE_HARD_STOP, 25 * policy.GIB)
        self.assertEqual(policy.MIN_ADMISSION_UNCERTAINTY_MARGIN, 256 * policy.MIB)
        self.assertEqual(policy.ADMISSION_UNCERTAINTY_BASIS_POINTS, 1000)

    def test_excluding_lease_tree_keeps_hardlinks_that_exist_outside_it(self) -> None:
        root = Path(self.temp.name) / "measured-project"
        root.mkdir()
        prefix = root / "prefix"
        prefix.mkdir()
        outside = root / "outside.bin"
        outside.write_bytes(b"outside-linked-data" * 1024)
        os.link(outside, prefix / "outside-link.bin")
        (prefix / "lease-only.bin").write_bytes(b"lease-owned-data" * 1024)
        discovered = policy.DiscoveredProjectRoot(
            root, policy.ROOT_CLASS_MANDATORY, "synthetic-test")

        with mock.patch.object(policy, "_classified_project_roots", return_value=[discovered]):
            total = policy.measure_project_usage()
            excluded = policy.measure_project_usage(exclude_paths=(prefix,))

        outside_bytes = outside.stat().st_blocks * 512
        lease_only_bytes = (prefix / "lease-only.bin").stat().st_blocks * 512
        self.assertEqual(total["allocated_inode_deduplicated_bytes"], outside_bytes + lease_only_bytes)
        self.assertEqual(excluded["allocated_inode_deduplicated_bytes"], outside_bytes + lease_only_bytes)
        self.assertEqual(excluded["allocated_inode_deduplicated_bytes_excluding_paths"], outside_bytes)


class SignedPersistentOutputAccountingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="fgmetal-signed-output-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.manifests = self.repo / "experiments/storage/manifests"
        self.manifests.mkdir(parents=True)
        self.runs = self.repo / "experiments/test-runs"
        self.runs.mkdir(parents=True)
        self.key_path = Path(self.temp.name) / "signing-key"
        self.key_patch = mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", self.key_path)
        self.key_patch.start()
        self.addCleanup(self.key_patch.stop)
        policy._manifest_signing_key(create=True)
        self.repo_patch = mock.patch.object(policy, "REPO", self.repo)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)
        self.discovered = policy.DiscoveredProjectRoot(
            self.repo, policy.ROOT_CLASS_MANDATORY, "synthetic-signed-output-test")

    def _write_manifest(self, experiment_id: str, *, persistent: int = 64 * policy.MIB,
                        gate0_hash: str = "0" * 64,
                        output_dir: Path | None = None,
                        storage_justification: str | None = None,
                        project_budget_bytes: int | None = None) -> tuple[Path, Path]:
        selected_output = output_dir or self.runs / experiment_id
        selected_output.mkdir(parents=True, exist_ok=True)
        manifest_path = self.manifests / f"{experiment_id}.json"
        selected_project_budget = (policy.PROJECT_BUDGET if project_budget_bytes is None
                                   else project_budget_bytes)
        estimates = {
            "expected_duration_seconds": 60,
            "expected_prefix_bytes": 0,
            "expected_build_bytes": 0,
            "estimated_persistent_bytes": persistent,
            "estimated_max_new_disk_bytes": persistent,
            "estimated_uncertainty_margin_bytes": policy.admission_uncertainty_margin_bytes(persistent),
            "estimated_projected_new_disk_bytes": (
                persistent + policy.admission_uncertainty_margin_bytes(persistent)),
            "project_budget_bytes": selected_project_budget,
        }
        manifest = {
            **estimates,
            "experiment_id": experiment_id,
            "purpose": "synthetic signed-output accounting test",
            "candidate_hashes": {},
            "created_at_unix": time.time(),
            "expected_prefix_requirement": False,
            "expected_build_requirement": False,
            "project_estimated_peak_bytes": persistent,
            "projected_free_bytes": 100 * policy.GIB,
            "projected_scoped_project_bytes": 1 * policy.GIB,
            "storage_admission_warnings": [],
            "project_budget_remaining_before_bytes": policy.PROJECT_BUDGET,
            "free_before_bytes": 100 * policy.GIB,
            "project_usage_before": {"allocated_inode_deduplicated_bytes": policy.GIB},
            "blockers": [],
            "warnings": [],
            "cleanup_policy": "synthetic cleanup",
            "retention_policy": "synthetic retention",
            "storage_justification": storage_justification,
            "persistent_growth_over_2g_approved": False,
            "persistent_growth_over_2g_approval_reason": None,
            "internal_storage_setup": False,
            "storage_policy_smoke": False,
            "gate0_attestation_sha256": gate0_hash,
            "authorized_runner_paths": [],
            "authorized_runner_sha256": {},
            "build_source_tree_hash": None,
            "build_configuration_key_sha256": None,
            "build_kind": None,
            "build_type": None,
            "dependency_state": None,
            "preserve_debug_reason": None,
            "estimated_debug_bytes": None,
            "failure_capture_paths": [],
            "native_architecture": None,
            "output_dir": str(selected_output),
            "manifest_path": str(manifest_path),
            "decision": "ALLOW",
        }
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest, create_key=True)
        policy._write_json(manifest_path, manifest)
        return manifest_path, selected_output

    def test_signed_run_output_stays_in_whole_total_but_not_nonlease_total(self) -> None:
        _manifest_path, output = self._write_manifest("signed-output-a")
        (output / "run.bin").write_bytes(b"r" * (1024 * 1024))
        unrelated = self.repo / "unrelated.bin"
        unrelated.write_bytes(b"u" * (512 * 1024))

        with mock.patch.object(policy, "_classified_project_roots", return_value=[self.discovered]):
            snapshot, exclusions, blockers = policy._signed_persistent_output_snapshot()
            total = policy.measure_project_usage()
            nonlease = policy.measure_project_usage(exclude_paths=exclusions)

        self.assertEqual(blockers, [])
        self.assertEqual(len(snapshot["bindings"]), 1)
        self.assertEqual(nonlease["allocated_inode_deduplicated_bytes_excluding_paths"],
                         unrelated.stat().st_blocks * 512)
        self.assertGreater(total["allocated_inode_deduplicated_bytes"],
                           nonlease["allocated_inode_deduplicated_bytes_excluding_paths"])
        self.assertGreaterEqual(policy.conservative_project_usage_bytes(total),
                                total["allocated_inode_deduplicated_bytes"])

    def test_signed_output_over_reservation_blocks_and_is_not_excluded(self) -> None:
        manifest_path, output = self._write_manifest("signed-output-over", persistent=policy.MIB)
        (output / "oversized.bin").write_bytes(b"o" * (8 * policy.MIB))

        snapshot, exclusions, blockers = policy._signed_persistent_output_snapshot()

        self.assertTrue(any("exceeds its persistent reservation" in item for item in blockers))
        self.assertEqual(snapshot["bindings"], ())
        self.assertNotIn(output, exclusions)
        self.assertNotIn(manifest_path, exclusions)

    def test_fixed_historical_overrun_requires_closure_and_stays_in_project_total(self) -> None:
        experiment_id = policy.HISTORICAL_TERMINAL_RUN_ID
        output = policy._historical_terminal_run_paths()["output_dir"]
        manifest_path, output = self._write_manifest(
            experiment_id,
            persistent=policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES,
            output_dir=output,
            storage_justification="fixed historical terminal output reservation",
        )
        payload = output / "oversized.bin"
        payload.write_bytes(b"h" * (8 * policy.MIB))
        measured_overrun = policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES + 512
        valid_closure = {
            "closure_path": str(policy._historical_terminal_closure_path()),
            "closure_sha256": "a" * 64,
            "closure_tree_sha256": "b" * 64,
        }
        overrun_blocker = (
            "signed experiment output exceeds its persistent reservation: " + experiment_id)

        with mock.patch.object(policy, "_persistent_output_snapshot",
                               return_value=(measured_overrun, "c" * 64)), \
                mock.patch.object(policy, "_verify_historical_terminal_closure",
                                  return_value=(valid_closure, [])), \
                mock.patch.object(policy, "_classified_project_roots", return_value=[self.discovered]):
            snapshot, exclusions, blockers = policy._signed_persistent_output_snapshot()
            total = policy.measure_project_usage()
            nonlease = policy.measure_project_usage(exclude_paths=exclusions)

        self.assertNotIn(overrun_blocker, blockers)
        self.assertEqual(len(snapshot["bindings"]), 1)
        self.assertEqual(snapshot["bindings"][0]["output_allocated_bytes"], measured_overrun)
        self.assertEqual(snapshot["bindings"][0]["output_reservation_bytes"],
                         policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES)
        self.assertFalse(snapshot["bindings"][0]["reservation_active"])
        self.assertEqual(snapshot["bindings"][0]["future_output_reservation_bytes"], 0)
        self.assertIn(output, exclusions)
        self.assertIn(manifest_path, exclusions)
        self.assertEqual(
            total["allocated_inode_deduplicated_bytes"]
            - nonlease["allocated_inode_deduplicated_bytes_excluding_paths"],
            payload.stat().st_blocks * 512 + manifest_path.stat().st_blocks * 512,
        )

        with mock.patch.object(policy, "_persistent_output_snapshot",
                               return_value=(measured_overrun, "c" * 64)), \
                mock.patch.object(policy, "_verify_historical_terminal_closure",
                                  return_value=(None, ["historical closure verification failed"])), \
                mock.patch.object(policy, "_classified_project_roots", return_value=[self.discovered]):
            invalid_snapshot, invalid_exclusions, invalid_blockers = (
                policy._signed_persistent_output_snapshot())

        self.assertIn(overrun_blocker, invalid_blockers)
        self.assertEqual(invalid_snapshot["bindings"], ())
        self.assertNotIn(output, invalid_exclusions)
        self.assertNotIn(manifest_path, invalid_exclusions)

    def test_historical_signed_output_accepts_exact_original_project_budget(self) -> None:
        experiment_id = policy.HISTORICAL_TERMINAL_RUN_ID
        output = policy._historical_terminal_run_paths()["output_dir"]
        manifest_path, output = self._write_manifest(
            experiment_id,
            persistent=policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES,
            output_dir=output,
            storage_justification="fixed historical terminal output reservation",
            project_budget_bytes=policy.HISTORICAL_TERMINAL_ORIGINAL_PROJECT_BUDGET_BYTES,
        )
        payload = output / "required.bin"
        payload.write_bytes(b"historical terminal output")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "project budget differs from the installed policy"):
            policy._validated_manifest_estimates(manifest)
        valid_closure = {
            "closure_path": str(policy._historical_terminal_closure_path()),
            "closure_sha256": "a" * 64,
            "closure_tree_sha256": "b" * 64,
        }

        with mock.patch.object(policy, "_verify_historical_terminal_closure",
                               return_value=(valid_closure, [])), \
                mock.patch.object(policy, "_classified_project_roots", return_value=[self.discovered]):
            snapshot, exclusions, blockers = policy._signed_persistent_output_snapshot()
            total = policy.measure_project_usage()
            nonlease = policy.measure_project_usage(exclude_paths=exclusions)

        self.assertEqual(blockers, [])
        self.assertEqual(len(snapshot["bindings"]), 1)
        self.assertEqual(snapshot["bindings"][0]["experiment_id"], experiment_id)
        self.assertEqual(snapshot["bindings"][0]["output_reservation_bytes"],
                         policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES)
        self.assertIn(output, exclusions)
        self.assertIn(manifest_path, exclusions)
        self.assertEqual(
            total["allocated_inode_deduplicated_bytes"]
            - nonlease["allocated_inode_deduplicated_bytes_excluding_paths"],
            payload.stat().st_blocks * 512 + manifest_path.stat().st_blocks * 512,
        )

    def test_historical_project_budget_exception_rejects_wrong_budget(self) -> None:
        experiment_id = policy.HISTORICAL_TERMINAL_RUN_ID
        output = policy._historical_terminal_run_paths()["output_dir"]
        manifest_path, output = self._write_manifest(
            experiment_id,
            persistent=policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES,
            output_dir=output,
            storage_justification="fixed historical terminal output reservation",
            project_budget_bytes=policy.HISTORICAL_TERMINAL_ORIGINAL_PROJECT_BUDGET_BYTES - 1,
        )
        (output / "required.bin").write_bytes(b"historical terminal output")

        snapshot, exclusions, blockers = policy._signed_persistent_output_snapshot()

        self.assertTrue(any("historical terminal storage manifest or output path is invalid" in item
                            for item in blockers))
        self.assertEqual(snapshot["bindings"], ())
        self.assertNotIn(output, exclusions)
        self.assertNotIn(manifest_path, exclusions)

    def test_nonhistorical_signed_output_cannot_use_original_project_budget(self) -> None:
        manifest_path, output = self._write_manifest(
            "signed-output-legacy-budget",
            project_budget_bytes=policy.HISTORICAL_TERMINAL_ORIGINAL_PROJECT_BUDGET_BYTES,
        )
        (output / "run.bin").write_bytes(b"not a historical terminal run")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

        with self.assertRaisesRegex(ValueError, "project budget differs from the installed policy"):
            policy._validated_manifest_estimates(manifest)

        snapshot, exclusions, blockers = policy._signed_persistent_output_snapshot()

        self.assertEqual(blockers, [])
        self.assertEqual(snapshot["bindings"], ())
        self.assertNotIn(output, exclusions)
        self.assertNotIn(manifest_path, exclusions)

    def test_tampered_signed_manifest_does_not_authorize_output_exclusion(self) -> None:
        manifest_path, output = self._write_manifest("signed-output-tampered")
        (output / "run.bin").write_bytes(b"t" * (1024 * 1024))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["output_dir"] = str(self.runs / "different-output")
        policy._write_json(manifest_path, manifest)

        with mock.patch.object(policy, "_classified_project_roots", return_value=[self.discovered]):
            _snapshot, exclusions, blockers = policy._signed_persistent_output_snapshot()
            total = policy.measure_project_usage()
            unexcluded = policy.measure_project_usage(exclude_paths=exclusions)

        self.assertEqual(blockers, [])
        self.assertEqual(exclusions, ())
        self.assertEqual(total["allocated_inode_deduplicated_bytes"],
                         unexcluded["allocated_inode_deduplicated_bytes"])

    def test_signed_output_snapshot_rebinds_allocation_growth(self) -> None:
        _manifest_path, output = self._write_manifest("signed-output-rebind")
        (output / "first.bin").write_bytes(b"a" * (1024 * 1024))
        before, before_exclusions, before_blockers = policy._signed_persistent_output_snapshot()
        (output / "second.bin").write_bytes(b"b" * (1024 * 1024))
        after, after_exclusions, after_blockers = policy._signed_persistent_output_snapshot()

        self.assertEqual(before_blockers, [])
        self.assertEqual(after_blockers, [])
        self.assertEqual(before_exclusions, after_exclusions)
        self.assertNotEqual(before, after)

    def test_signed_output_snapshot_detects_same_allocation_file_mutation(self) -> None:
        _manifest_path, output = self._write_manifest("signed-output-content-rebind")
        file_path = output / "evidence.bin"
        file_path.write_bytes(b"a" * (1024 * 1024))
        before, _before_exclusions, before_blockers = policy._signed_persistent_output_snapshot()
        before_allocation = before["bindings"][0]["output_allocated_bytes"]
        with file_path.open("r+b") as stream:
            stream.write(b"b")
            stream.flush()
            os.fsync(stream.fileno())
        after, _after_exclusions, after_blockers = policy._signed_persistent_output_snapshot()

        self.assertEqual(before_blockers, [])
        self.assertEqual(after_blockers, [])
        self.assertEqual(after["bindings"][0]["output_allocated_bytes"], before_allocation)
        self.assertNotEqual(before["bindings"][0]["output_tree_sha256"],
                            after["bindings"][0]["output_tree_sha256"])

    def test_new_signed_output_must_bind_current_gate0_attestation(self) -> None:
        manifest_path, _output = self._write_manifest(
            "signed-output-current-gate", gate0_hash="a" * 64)
        _snapshot, _exclusions, blockers = policy._signed_persistent_output_snapshot(
            expected_gate0_attestation_sha256="a" * 64)
        self.assertEqual(blockers, [])

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["gate0_attestation_sha256"] = "b" * 64
        manifest.pop("manifest_hmac_sha256")
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
        policy._write_json(manifest_path, manifest)
        _snapshot, _exclusions, blockers = policy._signed_persistent_output_snapshot(
            expected_gate0_attestation_sha256="a" * 64)
        self.assertTrue(any("does not bind the current Gate 0" in item for item in blockers))

    def test_current_run_scope_binding_rejects_signed_manifest_missing_required_field(self) -> None:
        experiment_id = "signed-current-run-schema"
        attestation_bytes = b"synthetic-gate0-attestation"
        manifest_path, _output = self._write_manifest(
            experiment_id,
            gate0_hash=hashlib.sha256(attestation_bytes).hexdigest())

        binding, blockers = policy._current_run_scope_binding(experiment_id, attestation_bytes)
        self.assertIsNotNone(binding)
        self.assertEqual(blockers, [])

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.pop("purpose")
        manifest.pop("manifest_hmac_sha256")
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
        policy._write_json(manifest_path, manifest)

        binding, blockers = policy._current_run_scope_binding(experiment_id, attestation_bytes)
        self.assertIsNone(binding)
        self.assertTrue(any("missing required fields" in item for item in blockers))


class HistoricalTerminalClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="fgmetal-terminal-closure-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.run_id = policy.HISTORICAL_TERMINAL_RUN_ID
        self.output = (self.root / "experiments/dxvk_macos_step11d3b/runs" / self.run_id)
        self.output.mkdir(parents=True)
        self.manifest_path = (self.root / "experiments/storage/manifests" / f"{self.run_id}.json")
        self.manifest_path.parent.mkdir(parents=True)
        self.manifest_bytes = b"synthetic immutable storage manifest\n"
        self.manifest_path.write_bytes(self.manifest_bytes)
        self.manifest_sha256 = hashlib.sha256(self.manifest_bytes).hexdigest()
        self.payload = self.output / "required.bin"
        self.payload.write_bytes(b"synthetic terminal evidence")
        self.repo_patch = mock.patch.object(policy, "REPO", self.root)
        self.repo_patch.start()
        self.addCleanup(self.repo_patch.stop)
        for name, value in (
                ("HISTORICAL_TERMINAL_REPORT_PREFIX_BYTES", 5),
                ("HISTORICAL_TERMINAL_REPORT_PREFIX_SHA256", "5" * 64)):
            patcher = mock.patch.object(policy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self._patch_tree_contract()
        self.tree = policy._historical_terminal_tree_snapshot(self.output)
        self.evidence = {
            "paths": {
                "run": str(self.output),
                "output_dir": str(self.output),
                "report": str(policy._historical_terminal_run_paths()["report"]),
                "storage_manifest": str(self.manifest_path),
            },
            "sha256": {
                "run_result": "1" * 64,
                "storage_manifest": self.manifest_sha256,
            },
            "manifest_hmac_sha256": "2" * 64,
            "gate0_post_allocation_provenance": "report reconstruction; not a numeric field in the embedded POST object",
            "gate0_post_status": "BLOCKED",
            "gate0_post_blockers": list(policy.HISTORICAL_TERMINAL_BLOCKERS),
            "run_status": "FAIL",
            "r5_classification": "INVALID / INCOMPLETE",
            "t8_classification": "INSUFFICIENT TIMING EVIDENCE",
            "finder_treatment": "NOT EXECUTED",
            "report_prefix_bytes": 5,
            "report_prefix_sha256": "5" * 64,
            "controller_evidence_sha256": {"controller.json": "3" * 64},
            "prefix_path": "/private/tmp/synthetic-historical-prefix",
            "prefix_was_deleted": True,
            "process_ids": [58921, 58942],
        }
        self.metadata_tree_sha256 = "4" * 64

    def _patch_tree_contract(self) -> None:
        info = self.output.lstat()
        payload_info = self.payload.lstat()
        self.contract_patches = [
            mock.patch.object(policy, "HISTORICAL_TERMINAL_ROOT_IDENTITY",
                              (info.st_dev, info.st_ino)),
            mock.patch.object(policy, "HISTORICAL_TERMINAL_OUTPUT_BYTES",
                              payload_info.st_blocks * 512),
            mock.patch.object(policy, "HISTORICAL_TERMINAL_OUTPUT_LOGICAL_BYTES",
                              payload_info.st_size),
            mock.patch.object(policy, "HISTORICAL_TERMINAL_TRUSTED_FILES", {
                "required.bin": hashlib.sha256(self.payload.read_bytes()).hexdigest()}),
        ]
        for patcher in self.contract_patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write_closure(self, *, changes: dict[str, object] | None = None) -> dict[str, object]:
        path = policy._historical_terminal_closure_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        directory_fd, directory_identity = policy._open_historical_terminal_closure_directory()
        os.close(directory_fd)
        record: dict[str, object] = {
            "schema_version": 3,
            "created_at_unix": time.time(),
            "closure_policy": "history-preserving-terminal-overrun-v1",
            "experiment_id": self.run_id,
            "closure_path": str(path),
            "closure_revision": 0,
            "storage_manifest_path": str(self.manifest_path),
            "storage_manifest_sha256": self.manifest_sha256,
            "original_reservation_bytes": policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES,
            "historical_failure_preserved": True,
            "authorizes_execution": False,
            "changes_original_gate_result": False,
            "counts_toward_project_scope": True,
            "report_prefix": {"path": str(policy._historical_terminal_run_paths()["report"]),
                              "bytes": 5, "sha256": "5" * 64},
            "closure_directory_identity": list(directory_identity),
            "liveness_at_closure": {"status": "QUIESCENT"},
            "source_sha256": policy._historical_terminal_source_hashes(),
            "gate0_control_sha256": policy._gate0_control_hashes(),
            "historical_evidence": self.evidence,
            "manifest_hmac_sha256": self.evidence["manifest_hmac_sha256"],
            "gate0_post_allocation_bytes": policy.HISTORICAL_TERMINAL_POST_OUTPUT_BYTES,
            "gate0_post_allocation_provenance": (
                "report reconstruction; not a numeric field in the embedded POST object"),
            "gate0_post_status": "BLOCKED",
            "gate0_post_blockers": list(policy.HISTORICAL_TERMINAL_BLOCKERS),
            "run_result_status": "FAIL",
            "r5_classification": "INVALID / INCOMPLETE",
            "t8_classification": "INSUFFICIENT TIMING EVIDENCE",
            "finder_treatment": "NOT EXECUTED",
            "output_dir": str(self.output),
            "metadata_output_allocated_bytes": self.tree["allocated_bytes"],
            "metadata_output_tree_sha256": self.metadata_tree_sha256,
            "tree_inventory": self.tree,
            "closed_tree_allocated_bytes": self.tree["allocated_bytes"],
            "closed_tree_logical_bytes": self.tree["logical_bytes"],
            "tree_sha256": self.tree["tree_sha256"],
            "tree_root_identity": self.tree["root_identity"],
        }
        if changes:
            record.update(changes)
        sealed = policy._seal_storage_evidence(record, "closure_hmac_sha256")
        path.write_bytes(policy._canonical_pretty_json_bytes(sealed))
        path.chmod(0o600)
        return sealed

    def _write_legacy_closure_missing_path(self) -> tuple[dict[str, object], bytes]:
        path = policy._historical_terminal_closure_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        directory_fd, directory_identity = policy._open_historical_terminal_closure_directory()
        os.close(directory_fd)
        record: dict[str, object] = {
            "schema_version": 2,
            "closure_policy": "history-preserving-terminal-overrun-v1",
            "experiment_id": self.run_id,
            "created_at_unix": 123.0,
            "storage_manifest_path": str(self.manifest_path),
            "storage_manifest_sha256": self.manifest_sha256,
            "original_reservation_bytes": policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES,
            "historical_failure_preserved": True,
            "authorizes_execution": False,
            "changes_original_gate_result": False,
            "counts_toward_project_scope": True,
            "report_prefix": {"path": str(policy._historical_terminal_run_paths()["report"]),
                              "bytes": 5, "sha256": "5" * 64},
            "closure_directory_identity": list(directory_identity),
            "liveness_at_closure": {"status": "QUIESCENT"},
            "source_sha256": policy._historical_terminal_source_hashes(),
            "gate0_control_sha256": policy._gate0_control_hashes(),
            "historical_evidence": self.evidence,
            "manifest_hmac_sha256": self.evidence["manifest_hmac_sha256"],
            "gate0_post_allocation_bytes": policy.HISTORICAL_TERMINAL_POST_OUTPUT_BYTES,
            "gate0_post_allocation_provenance": (
                "report reconstruction; not a numeric field in the embedded POST object"),
            "gate0_post_status": "BLOCKED",
            "gate0_post_blockers": list(policy.HISTORICAL_TERMINAL_BLOCKERS),
            "run_result_status": "FAIL",
            "r5_classification": "INVALID / INCOMPLETE",
            "t8_classification": "INSUFFICIENT TIMING EVIDENCE",
            "finder_treatment": "NOT EXECUTED",
            "output_dir": str(self.output),
            "metadata_output_allocated_bytes": self.tree["allocated_bytes"],
            "metadata_output_tree_sha256": self.metadata_tree_sha256,
            "tree_inventory": self.tree,
            "closed_tree_allocated_bytes": self.tree["allocated_bytes"],
            "closed_tree_logical_bytes": self.tree["logical_bytes"],
            "tree_sha256": self.tree["tree_sha256"],
            "tree_root_identity": self.tree["root_identity"],
        }
        sealed = policy._seal_storage_evidence(record, "closure_hmac_sha256")
        raw = policy._canonical_pretty_json_bytes(sealed)
        path.write_bytes(raw)
        path.chmod(0o600)
        return sealed, raw

    def _write_correction_for_legacy(self, legacy_raw: bytes) -> dict[str, object]:
        path = policy._historical_terminal_closure_correction_path()
        directory_fd, directory_identity = policy._open_historical_terminal_closure_directory()
        os.close(directory_fd)
        record: dict[str, object] = {
            "schema_version": 3,
            "created_at_unix": time.time(),
            "closure_policy": "history-preserving-terminal-overrun-v1",
            "experiment_id": self.run_id,
            "closure_path": str(path),
            "closure_revision": 1,
            "supersedes_closure_path": str(policy._historical_terminal_closure_path()),
            "supersedes_closure_sha256": hashlib.sha256(legacy_raw).hexdigest(),
            "supersedes_closure_reason": "Synthetic append-only correction fixture.",
            "storage_manifest_path": str(self.manifest_path),
            "storage_manifest_sha256": self.manifest_sha256,
            "manifest_hmac_sha256": self.evidence["manifest_hmac_sha256"],
            "gate0_post_allocation_bytes": policy.HISTORICAL_TERMINAL_POST_OUTPUT_BYTES,
            "gate0_post_allocation_provenance": (
                "report reconstruction; not a numeric field in the embedded POST object"),
            "gate0_post_status": "BLOCKED",
            "gate0_post_blockers": list(policy.HISTORICAL_TERMINAL_BLOCKERS),
            "run_result_status": "FAIL",
            "r5_classification": "INVALID / INCOMPLETE",
            "t8_classification": "INSUFFICIENT TIMING EVIDENCE",
            "finder_treatment": "NOT EXECUTED",
            "original_reservation_bytes": policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES,
            "historical_failure_preserved": True,
            "authorizes_execution": False,
            "changes_original_gate_result": False,
            "counts_toward_project_scope": True,
            "report_prefix": {"path": str(policy._historical_terminal_run_paths()["report"]),
                              "bytes": 5, "sha256": "5" * 64},
            "closure_directory_identity": list(directory_identity),
            "liveness_at_closure": {"status": "QUIESCENT"},
            "source_sha256": policy._historical_terminal_source_hashes(),
            "gate0_control_sha256": policy._gate0_control_hashes(),
            "historical_evidence": self.evidence,
            "output_dir": str(self.output),
            "metadata_output_allocated_bytes": self.tree["allocated_bytes"],
            "metadata_output_tree_sha256": self.metadata_tree_sha256,
            "tree_inventory": self.tree,
            "closed_tree_allocated_bytes": self.tree["allocated_bytes"],
            "tree_sha256": self.tree["tree_sha256"],
            "tree_root_identity": self.tree["root_identity"],
        }
        sealed = policy._seal_storage_evidence(record, "closure_hmac_sha256")
        path.write_bytes(policy._canonical_pretty_json_bytes(sealed))
        path.chmod(0o600)
        return sealed

    def _verify(self, *, inventory: dict[str, object] | None = None,
                manifest_sha256: str | None = None,
                actual_tree: bool = False) -> tuple[dict[str, object] | None, list[str]]:
        with mock.patch.object(policy, "_historical_terminal_evidence", return_value=self.evidence), \
                mock.patch.object(policy, "_historical_terminal_liveness_check",
                                  return_value={"status": "QUIESCENT"}):
            verify = lambda: policy._verify_historical_terminal_closure(
                {"experiment_id": self.run_id,
                 "estimated_persistent_bytes": policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES},
                manifest_sha256 or self.manifest_sha256,
                self.output,
                allocated_bytes=self.tree["allocated_bytes"],
                metadata_tree_sha256=self.metadata_tree_sha256,
            )
            if actual_tree:
                return verify()
            with mock.patch.object(policy, "_historical_terminal_tree_snapshot",
                                   return_value=inventory if inventory is not None else self.tree):
                return verify()

    def test_exact_failed_run_accepts_authenticated_closure_without_authorizing_execution(self) -> None:
        closure = self._write_closure()
        binding, blockers = self._verify()

        self.assertEqual(blockers, [])
        self.assertIsNotNone(binding)
        self.assertEqual(closure["authorizes_execution"], False)
        self.assertEqual(closure["original_reservation_bytes"], 314_572_800)
        self.assertEqual(closure["manifest_hmac_sha256"], self.evidence["manifest_hmac_sha256"])
        with self.assertRaises(ValueError):
            policy._validated_manifest_estimates(closure)

    def test_signed_historical_source_hash_tampering_without_hmac_is_rejected(self) -> None:
        self._write_closure()
        path = policy._historical_terminal_closure_path()
        tampered = json.loads(path.read_bytes())
        source_name = next(iter(tampered["source_sha256"]))
        tampered["source_sha256"][source_name] = "f" * 64
        path.write_bytes(policy._canonical_pretty_json_bytes(tampered))
        path.chmod(0o600)

        binding, blockers = self._verify()

        self.assertIsNone(binding)
        self.assertTrue(any("signature is invalid" in item for item in blockers))

    def test_wrong_run_or_manifest_hash_fails(self) -> None:
        self._write_closure(changes={"experiment_id": "copied-to-another-run"})
        binding, blockers = self._verify()
        self.assertIsNone(binding)
        self.assertTrue(any("identity" in item or "schema" in item for item in blockers))

        closure_path = policy._historical_terminal_closure_path()
        closure_path.unlink()
        self._write_closure()
        binding, blockers = self._verify(manifest_sha256="f" * 64)
        self.assertIsNone(binding)
        self.assertTrue(any("manifest" in item for item in blockers))

    def test_valid_hmac_with_wrong_report_prefix_is_rejected(self) -> None:
        self._write_closure(changes={
            "report_prefix": {
                "path": str(policy._historical_terminal_run_paths()["report"]),
                "bytes": 5,
                "sha256": "6" * 64,
            }
        })
        binding, blockers = self._verify()
        self.assertIsNone(binding)
        self.assertTrue(any("closure schema" in item for item in blockers))

    def test_original_manifest_mutation_cannot_reuse_closure(self) -> None:
        self._write_closure()
        self.manifest_path.write_bytes(self.manifest_bytes + b"changed")
        binding, blockers = self._verify()
        self.assertIsNone(binding)
        self.assertTrue(any("original storage manifest changed" in item for item in blockers))

    def test_legacy_schema_certificate_is_rejected(self) -> None:
        self._write_closure(changes={"schema_version": 1})
        binding, blockers = self._verify()
        self.assertIsNone(binding)
        self.assertTrue(any("closure schema" in item for item in blockers))

    def test_copied_closure_file_is_rejected(self) -> None:
        self._write_closure()
        (policy._historical_terminal_closure_path().parent / "another-run.json").write_bytes(b"copy")
        binding, blockers = self._verify()
        self.assertIsNone(binding)
        self.assertTrue(any("unexpected entries" in item for item in blockers))

    def test_append_only_correction_binds_legacy_certificate_without_path(self) -> None:
        legacy, legacy_raw = self._write_legacy_closure_missing_path()
        self.assertNotIn("closure_path", legacy)
        with mock.patch.object(policy, "HISTORICAL_TERMINAL_LEGACY_CLOSURE_SHA256",
                               hashlib.sha256(legacy_raw).hexdigest()):
            correction = self._write_correction_for_legacy(legacy_raw)
            correction_raw = policy._historical_terminal_closure_correction_path().read_bytes()
            with mock.patch.object(policy, "HISTORICAL_TERMINAL_CORRECTION1_SHA256",
                                   hashlib.sha256(correction_raw).hexdigest()), \
                    mock.patch.object(policy, "HISTORICAL_TERMINAL_CORRECTION1_HMAC_SHA256",
                                      correction["closure_hmac_sha256"]):
                binding, blockers = self._verify()

                self.assertEqual(blockers, [])
                self.assertIsNotNone(binding)
                self.assertEqual(binding["closure_path"], str(policy._historical_terminal_closure_correction_path()))
                self.assertEqual(correction["supersedes_closure_sha256"], hashlib.sha256(legacy_raw).hexdigest())
                self.assertEqual(policy._historical_terminal_closure_path().read_bytes(), legacy_raw)

                policy._historical_terminal_closure_path().write_bytes(legacy_raw + b"tamper")
                binding, blockers = self._verify()
                self.assertIsNone(binding)
                self.assertTrue(any("closure" in item for item in blockers))

    def test_edited_certificate_and_symlinked_closure_directory_fail_closed(self) -> None:
        self._write_closure()
        path = policy._historical_terminal_closure_path()
        tampered = path.read_bytes().replace(b'"authorizes_execution": false',
                                             b'"authorizes_execution": true')
        path.write_bytes(tampered)
        path.chmod(0o600)
        binding, blockers = self._verify()
        self.assertIsNone(binding)
        self.assertTrue(any("signature" in item or "canonical" in item for item in blockers))

        path.unlink()
        outside = path.parent.with_name("closure-target")
        outside.mkdir()
        path.parent.rmdir()
        path.parent.symlink_to(outside, target_is_directory=True)
        binding, blockers = self._verify()
        self.assertIsNone(binding)
        self.assertTrue(any("cannot be opened safely" in item for item in blockers))

    def test_future_tree_mutation_fails_even_when_allocated_bytes_do_not_change(self) -> None:
        self._write_closure()
        changed = dict(self.tree)
        changed["tree_sha256"] = "f" * 64
        binding, blockers = self._verify(inventory=changed)
        self.assertIsNone(binding)
        self.assertTrue(any("tree changed" in item for item in blockers))

    def test_same_length_content_change_fails_even_when_allocation_is_unchanged(self) -> None:
        original = self.payload.read_bytes()
        before = self.payload.stat(follow_symlinks=False)
        self.payload.write_bytes(b"x" * len(original))
        os.utime(self.payload, ns=(before.st_atime_ns, before.st_mtime_ns))
        after = self.payload.stat(follow_symlinks=False)
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(after.st_blocks, before.st_blocks)
        with self.assertRaisesRegex(OSError, "content differs from the provenance inventory"):
            policy._historical_terminal_tree_snapshot(self.output)

    def test_historical_tree_apfs_alias_normalizes_only_device_identity_field(self) -> None:
        current_device = self.output.lstat().st_dev
        recorded_device = current_device + 2
        self.assertNotEqual(current_device, recorded_device)
        current_root = self.output.lstat()
        with (mock.patch.object(
                    policy, "HISTORICAL_TERMINAL_ROOT_IDENTITY",
                    (recorded_device, current_root.st_ino)),
              mock.patch.object(
                    policy, "_historical_terminal_apfs_device_alias_matches",
                    return_value=True)):
            inventory = policy._historical_terminal_tree_snapshot(self.output)
        self.assertEqual(inventory["root_identity"][0], recorded_device)
        self.assertEqual(inventory["parent_identity"][0], recorded_device)
        self.assertTrue(all(row["identity"][0] == recorded_device
                            for row in inventory["entries"]))
        self.assertEqual(inventory["entries"][0]["identity"][1], current_root.st_ino)

        with (mock.patch.object(
                    policy, "HISTORICAL_TERMINAL_ROOT_IDENTITY",
                    (recorded_device, current_root.st_ino)),
              mock.patch.object(
                    policy, "_historical_terminal_apfs_device_alias_matches",
                    return_value=False),
              self.assertRaisesRegex(OSError, "root identity is unexpected")):
            policy._historical_terminal_tree_snapshot(self.output)

    def test_closure_directory_apfs_alias_requires_same_inode_and_volume_proof(self) -> None:
        self._write_closure()
        path = policy._historical_terminal_closure_path()
        current_device = path.parent.stat().st_dev
        recorded_device = current_device + 2
        record = json.loads(path.read_bytes())
        _hmac = record.pop("closure_hmac_sha256")
        record["closure_directory_identity"][0] = recorded_device
        sealed = policy._seal_storage_evidence(record, "closure_hmac_sha256")
        path.write_bytes(policy._canonical_pretty_json_bytes(sealed))
        path.chmod(0o600)

        directory_fd, directory_identity = policy._open_historical_terminal_closure_directory()
        try:
            with (mock.patch.object(policy, "HISTORICAL_TERMINAL_APFS_OLD_DEVICE", recorded_device),
                  mock.patch.object(
                      policy, "_historical_terminal_apfs_device_alias_matches", return_value=True)):
                verified, raw, error = policy._read_historical_terminal_closure_file(
                    directory_fd, path.name, directory_identity)
            self.assertIsNone(error)
            self.assertEqual(verified, sealed)
            self.assertEqual(raw, path.read_bytes())

            with (mock.patch.object(policy, "HISTORICAL_TERMINAL_APFS_OLD_DEVICE", recorded_device),
                  mock.patch.object(
                      policy, "_historical_terminal_apfs_device_alias_matches", return_value=False)):
                verified, _raw, error = policy._read_historical_terminal_closure_file(
                    directory_fd, path.name, directory_identity)
            self.assertIsNone(verified)
            self.assertIn("identity is not bound", error or "")
        finally:
            os.close(directory_fd)

    def test_lsof_identity_uses_current_signed_binary_and_preserves_historical_pin(self) -> None:
        path = Path(policy.HISTORICAL_TERMINAL_LSOF_PATH)
        current_device = path.lstat().st_dev
        with mock.patch.object(
                policy, "_historical_terminal_apfs_device_alias_matches", return_value=True):
            identity = policy._historical_terminal_lsof_identity()
        self.assertEqual(identity["file_identity"][0], current_device)
        self.assertEqual(identity["parent_identity"][0], current_device)
        self.assertEqual(identity["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        self.assertEqual(
            policy.HISTORICAL_TERMINAL_LSOF_SHA256,
            "deff85c7b4750a948a69fcd48eae3b394a37575e5048e5eb4a35b7a0482c3dc8")

        with (mock.patch.object(
                    policy, "HISTORICAL_TERMINAL_APFS_OLD_DEVICE", current_device + 2),
              mock.patch.object(
                    policy, "_historical_terminal_apfs_device_alias_matches", return_value=False),
              self.assertRaisesRegex(RuntimeError, "outside the authenticated historical APFS volume")):
            policy._historical_terminal_lsof_identity()

    def test_apfs_device_crosswalk_binds_pinned_uuid_to_live_descriptors(self) -> None:
        evidence_path = self.root / policy.HISTORICAL_TERMINAL_APFS_MOUNT_EVIDENCE
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        current_device = self.root.stat().st_dev
        recorded_device = current_device + 2
        evidence = {
            "status": "PASS",
            "blockers": [],
            "full_readonly_observation": {
                "data_volume_info": {
                    "VolumeUUID": policy.HISTORICAL_TERMINAL_APFS_VOLUME_UUID,
                    "canonical_mount_identity": {
                        "volume_uuid": policy.HISTORICAL_TERMINAL_APFS_VOLUME_UUID,
                        "mount_state": "MOUNTED_VERIFIED",
                        "mount_table_mount_point": "/System/Volumes/Data",
                        "mount_table_filesystem_id": [recorded_device, 26],
                        "mount_table_expected_path_records": [{
                            "filesystem_id": [recorded_device, 26],
                            "filesystem_type": "apfs",
                            "mount_point": "/System/Volumes/Data",
                            "source": "/dev/disk3s1",
                        }],
                    },
                },
            },
        }
        raw = policy._canonical_pretty_json_bytes(evidence)
        evidence_path.write_bytes(raw)
        volume_plist = plistlib.dumps({
            "DiskUUID": policy.HISTORICAL_TERMINAL_APFS_VOLUME_UUID,
            "MountPoint": "/System/Volumes/Data",
            "FilesystemType": "apfs",
        })

        def completed(stdout: bytes):
            return mock.Mock(returncode=0, stdout=stdout, stderr=b"")

        def matching_diskutil(*args: object, **kwargs: object):
            command = args[0]
            self.assertIn(str(command[-1]), {"/System/Volumes/Data", str(self.root)})
            return completed(volume_plist)

        with (mock.patch.object(policy, "HISTORICAL_TERMINAL_APFS_OLD_DEVICE", recorded_device),
              mock.patch.object(policy, "HISTORICAL_TERMINAL_REPOSITORY", self.root.resolve()),
              mock.patch.object(
                  policy, "HISTORICAL_TERMINAL_APFS_MOUNT_EVIDENCE_SHA256",
                  hashlib.sha256(raw).hexdigest()),
              mock.patch.object(policy.subprocess, "run", side_effect=matching_diskutil)):
            self.assertTrue(policy._historical_terminal_apfs_device_alias_matches(
                recorded_device, current_device))

            wrong_uuid = plistlib.dumps({
                "DiskUUID": "00000000-0000-0000-0000-000000000000",
                "MountPoint": "/System/Volumes/Data",
                "FilesystemType": "apfs",
            })
            with mock.patch.object(
                    policy.subprocess, "run", side_effect=lambda *a, **k: completed(wrong_uuid)):
                self.assertFalse(policy._historical_terminal_apfs_device_alias_matches(
                    recorded_device, current_device))

            wrong_mount = plistlib.dumps({
                "DiskUUID": policy.HISTORICAL_TERMINAL_APFS_VOLUME_UUID,
                "MountPoint": "/Volumes/Other",
                "FilesystemType": "apfs",
            })
            with mock.patch.object(
                    policy.subprocess, "run", side_effect=lambda *a, **k: completed(wrong_mount)):
                self.assertFalse(policy._historical_terminal_apfs_device_alias_matches(
                    recorded_device, current_device))

            original_fstatvfs = policy.os.fstatvfs

            def mismatched_fsid(descriptor: int):
                result = original_fstatvfs(descriptor)
                return SimpleNamespace(f_fsid=result.f_fsid + 1)

            with mock.patch.object(policy.os, "fstatvfs", side_effect=mismatched_fsid):
                self.assertFalse(policy._historical_terminal_apfs_device_alias_matches(
                    recorded_device, current_device))

            with mock.patch.object(
                    policy, "HISTORICAL_TERMINAL_APFS_MOUNT_EVIDENCE_SHA256", "0" * 64):
                self.assertFalse(policy._historical_terminal_apfs_device_alias_matches(
                    recorded_device, current_device))

    def test_apfs_device_crosswalk_rejects_replaced_mountpoint_descriptor(self) -> None:
        evidence_path = self.root / policy.HISTORICAL_TERMINAL_APFS_MOUNT_EVIDENCE
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        current_device = self.root.stat().st_dev
        recorded_device = current_device + 2
        evidence = {
            "status": "PASS",
            "blockers": [],
            "full_readonly_observation": {
                "data_volume_info": {
                    "VolumeUUID": policy.HISTORICAL_TERMINAL_APFS_VOLUME_UUID,
                    "canonical_mount_identity": {
                        "volume_uuid": policy.HISTORICAL_TERMINAL_APFS_VOLUME_UUID,
                        "mount_state": "MOUNTED_VERIFIED",
                        "mount_table_mount_point": "/System/Volumes/Data",
                        "mount_table_filesystem_id": [recorded_device, 26],
                        "mount_table_expected_path_records": [{
                            "filesystem_id": [recorded_device, 26],
                            "filesystem_type": "apfs",
                            "mount_point": "/System/Volumes/Data",
                            "source": "/dev/disk3s1",
                        }],
                    },
                },
            },
        }
        raw = policy._canonical_pretty_json_bytes(evidence)
        evidence_path.write_bytes(raw)
        volume_plist = plistlib.dumps({
            "DiskUUID": policy.HISTORICAL_TERMINAL_APFS_VOLUME_UUID,
            "MountPoint": "/System/Volumes/Data",
            "FilesystemType": "apfs",
        })
        data_open_count = 0
        original_open = policy._open_directory_nofollow

        def substituted_open(path: Path, *args: object, **kwargs: object) -> int:
            nonlocal data_open_count
            if Path(path) == Path("/System/Volumes/Data"):
                data_open_count += 1
                if data_open_count == 2:
                    return original_open(Path("/System/Volumes/Data/Users"))
            return original_open(Path(path), *args, **kwargs)

        with (mock.patch.object(policy, "HISTORICAL_TERMINAL_APFS_OLD_DEVICE", recorded_device),
              mock.patch.object(policy, "HISTORICAL_TERMINAL_REPOSITORY", self.root.resolve()),
              mock.patch.object(
                  policy, "HISTORICAL_TERMINAL_APFS_MOUNT_EVIDENCE_SHA256",
                  hashlib.sha256(raw).hexdigest()),
              mock.patch.object(
                  policy.subprocess, "run",
                  side_effect=lambda *a, **k: mock.Mock(
                      returncode=0, stdout=volume_plist, stderr=b"")),
              mock.patch.object(policy, "_open_directory_nofollow", side_effect=substituted_open)):
            self.assertFalse(policy._historical_terminal_apfs_device_alias_matches(
                recorded_device, current_device))

    def test_historical_identity_comparator_allows_only_proven_device_field(self) -> None:
        current = [16777231, 12345, 0o100600, 501, 1, 12, 100, 101, 8]
        recorded = list(current)
        recorded[0] = policy.HISTORICAL_TERMINAL_APFS_OLD_DEVICE
        with mock.patch.object(
                policy, "_historical_terminal_apfs_device_alias_matches", return_value=True):
            self.assertTrue(policy._identity_matches_historical_apfs_device_alias(
                recorded, current, current_device=current[0]))
            for index in range(1, len(recorded)):
                changed = list(recorded)
                changed[index] += 1
                self.assertFalse(policy._identity_matches_historical_apfs_device_alias(
                    changed, current, current_device=current[0]))
        with mock.patch.object(
                policy, "_historical_terminal_apfs_device_alias_matches", return_value=False):
            self.assertFalse(policy._identity_matches_historical_apfs_device_alias(
                recorded, current, current_device=current[0]))

        same_device_identity = [
            policy.HISTORICAL_TERMINAL_APFS_OLD_DEVICE,
            12345, 0o100600, 501, 1, 12, 100, 101, 8,
        ]
        with mock.patch.object(
                policy, "_historical_terminal_apfs_device_alias_matches",
                return_value=False) as crosswalk:
            self.assertTrue(policy._identity_matches_historical_apfs_device_alias(
                same_device_identity, same_device_identity,
                current_device=policy.HISTORICAL_TERMINAL_APFS_OLD_DEVICE))
            self.assertTrue(policy._directory_identity_matches_historical_apfs_device_alias(
                [policy.HISTORICAL_TERMINAL_APFS_OLD_DEVICE, 99],
                (policy.HISTORICAL_TERMINAL_APFS_OLD_DEVICE, 99)))
            crosswalk.assert_not_called()
        with mock.patch.object(
                policy, "_historical_terminal_apfs_device_alias_matches", return_value=False):
            self.assertFalse(policy._directory_identity_matches_historical_apfs_device_alias(
                [policy.HISTORICAL_TERMINAL_APFS_OLD_DEVICE + 2, 99],
                (policy.HISTORICAL_TERMINAL_APFS_OLD_DEVICE, 99)))

    def test_unexpected_64_mib_tree_growth_is_rejected(self) -> None:
        self._write_closure()
        unexpected = self.output / "unexpected-growth.bin"
        with unexpected.open("wb") as stream:
            block = b"x" * (1024 * 1024)
            for _ in range(64):
                stream.write(block)
        binding, blockers = self._verify(actual_tree=True)
        self.assertIsNone(binding)
        self.assertTrue(any("provenance inventory" in item for item in blockers))

    def test_descriptor_snapshot_rejects_missing_file_symlink_and_replaced_root(self) -> None:
        self.payload.unlink()
        with self.assertRaises(OSError):
            policy._historical_terminal_tree_snapshot(self.output)
        self.payload.write_bytes(b"synthetic terminal evidence")
        self.payload.unlink()
        self.payload.symlink_to(self.output / "does-not-exist")
        with self.assertRaises(OSError):
            policy._historical_terminal_tree_snapshot(self.output)

        self.payload.unlink()
        self.payload.write_bytes(b"synthetic terminal evidence")
        moved = self.output.with_name(self.output.name + "-old")
        self.output.rename(moved)
        self.output.mkdir()
        (self.output / "required.bin").write_bytes(b"synthetic terminal evidence")
        with self.assertRaises(OSError):
            policy._historical_terminal_tree_snapshot(self.output)

    def test_live_prefix_marker_blocks_liveness_check(self) -> None:
        with mock.patch.object(policy, "_active_lease_markers", return_value=[Path("/tmp/active.json")]):
            with self.assertRaisesRegex(RuntimeError, "PrefixLease markers"):
                policy._historical_terminal_liveness_check(self.evidence)

    def test_live_build_state_blocks_liveness_check(self) -> None:
        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value={
                    "active": [{"build_id": "live"}],
                    "completed_pending_retirement": [],
                    "failed_or_retiring": [],
                    "unknown": [],
                }) as snapshot_mock:
            with self.assertRaisesRegex(RuntimeError, "BuildLease state"):
                policy._historical_terminal_liveness_check(self.evidence)
        snapshot_mock.assert_called_once_with(active_buildlease_id=None)

    def test_exact_r71_handoff_buildlease_does_not_block_historical_closure(self) -> None:
        admission_id = "r6a-r87-admission-20261026t010000z-r90-c13-12345678"
        build_id = "r6a-r87-dual-lineage-20261026t010000z-r90-c13-12345678"
        active = {
            "build_id": build_id,
            "owner_experiment": admission_id,
            "state": "ACTIVE",
            "path": str(build_retention.BUILD_ROOT / build_id),
        }
        build_state = {
            "active": [active],
            "completed_pending_retirement": [],
            "failed_or_retiring": [],
            "unknown": [],
        }
        trusted_lsof = {
            "path": "/usr/sbin/lsof", "sha256": "a" * 64,
            "file_identity": [1, 2, 3, 4, 5], "owner_uid": 0,
            "mode": 0o755, "parent_identity": [1, 6],
        }

        def no_live_process(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "/bin/ps":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            if args[0] == "/usr/sbin/lsof":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            self.fail(f"unexpected process inspection command: {args!r}")

        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value=build_state) as snapshot_mock, \
                mock.patch.object(policy, "_historical_terminal_lsof_identity",
                                  return_value=trusted_lsof), \
                mock.patch.object(policy.subprocess, "run", side_effect=no_live_process):
            result = policy._historical_terminal_liveness_check(
                self.evidence,
                allowed_active_experiment_id=admission_id,
                allowed_active_build_id=build_id,
                terminal_closed_at_unix=time.time() - 60,
            )
        snapshot_mock.assert_called_once_with(active_buildlease_id=build_id)

        self.assertEqual(result["build_retention_status"], "QUIESCENT")
        self.assertEqual(result["active_prefix_markers"], 0)

    def test_r71_handoff_buildlease_allowance_rejects_mismatches_and_pending_state(self) -> None:
        admission_id = "r6a-r87-admission-20261026t010000z-r90-c13-12345678"
        build_id = "r6a-r87-dual-lineage-20261026t010000z-r90-c13-12345678"
        expected = {
            "build_id": build_id,
            "owner_experiment": admission_id,
            "state": "ACTIVE",
            "path": str(build_retention.BUILD_ROOT / build_id),
        }
        rows = [
            {**expected, "build_id": build_id + "-other"},
            {**expected, "owner_experiment": admission_id + "-other"},
            {**expected, "path": str(build_retention.BUILD_ROOT / (build_id + "-other"))},
            [expected, {**expected, "build_id": build_id + "-other"}],
        ]
        for active in rows:
            with self.subTest(active=active):
                with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                        mock.patch.object(build_retention, "_snapshot", return_value={
                            "active": active if isinstance(active, list) else [active],
                            "completed_pending_retirement": [],
                            "failed_or_retiring": [],
                            "unknown": [],
                        }):
                    with self.assertRaisesRegex(RuntimeError, "exact active R71"):
                        policy._historical_terminal_liveness_check(
                            self.evidence,
                            allowed_active_experiment_id=admission_id,
                            allowed_active_build_id=build_id,
                        )

        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value={
                    "active": [expected],
                    "completed_pending_retirement": [{"build_id": "pending"}],
                    "failed_or_retiring": [],
                    "unknown": [],
                }):
            with self.assertRaisesRegex(RuntimeError, "BuildLease state"):
                policy._historical_terminal_liveness_check(
                    self.evidence,
                    allowed_active_experiment_id=admission_id,
                    allowed_active_build_id=build_id,
                )

    def test_live_historical_process_blocks_liveness_check(self) -> None:
        def live_process(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, stdout="58921 live process\n", stderr="")

        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value={
                    "active": [], "completed_pending_retirement": [],
                    "failed_or_retiring": [], "unknown": [],
                }), \
                mock.patch.object(policy.subprocess, "run", side_effect=live_process):
            with self.assertRaisesRegex(RuntimeError, "pid 58921 is still present"):
                policy._historical_terminal_liveness_check(self.evidence)

    def test_reused_historical_pid_after_authenticated_closure_is_not_old_process(self) -> None:
        started = datetime.datetime.fromtimestamp(
            time.time() - 10, datetime.timezone.utc).strftime("%a %b %d %H:%M:%S %Y")
        closed_at = time.time() - 60
        trusted_lsof = {
            "path": "/usr/sbin/lsof", "sha256": "a" * 64,
            "file_identity": [1, 2, 3, 4, 5], "owner_uid": 0,
            "mode": 0o755, "parent_identity": [1, 6],
        }

        def reused_pid(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "/bin/ps":
                pid = args[2]
                return subprocess.CompletedProcess(
                    args, 0, stdout=f"{pid} {started} /usr/sbin/diskutil info\n", stderr="")
            if args[0] == "/usr/sbin/lsof":
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            self.fail(f"unexpected process inspection command: {args!r}")

        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value={
                    "active": [], "completed_pending_retirement": [],
                    "failed_or_retiring": [], "unknown": [],
                }), \
                mock.patch.object(policy, "_historical_terminal_lsof_identity",
                                  return_value=trusted_lsof), \
                mock.patch.object(policy.subprocess, "run", side_effect=reused_pid):
            result = policy._historical_terminal_liveness_check(
                self.evidence, terminal_closed_at_unix=closed_at)
            repeated = policy._historical_terminal_liveness_check(
                self.evidence, terminal_closed_at_unix=closed_at)

        self.assertEqual(result, repeated)
        self.assertEqual(result["historical_processes"], [
            {"pid": 58921, "status": "GONE"},
            {"pid": 58942, "status": "GONE"},
        ])

    def test_reused_pid_that_could_predate_closure_still_blocks(self) -> None:
        started = datetime.datetime.fromtimestamp(
            time.time() - 120, datetime.timezone.utc).strftime("%a %b %d %H:%M:%S %Y")

        def prior_pid(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            pid = args[2]
            return subprocess.CompletedProcess(
                args, 0, stdout=f"{pid} {started} /usr/bin/wine\n", stderr="")

        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value={
                    "active": [], "completed_pending_retirement": [],
                    "failed_or_retiring": [], "unknown": [],
                }), \
                mock.patch.object(policy.subprocess, "run", side_effect=prior_pid):
            with self.assertRaisesRegex(RuntimeError, "pid 58921 is still present"):
                policy._historical_terminal_liveness_check(
                    self.evidence, terminal_closed_at_unix=time.time() - 60)

    def test_ambiguous_reused_pid_start_time_fails_closed(self) -> None:
        def malformed_pid(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 0, stdout="58921 unreadable process row\n", stderr="")

        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value={
                    "active": [], "completed_pending_retirement": [],
                    "failed_or_retiring": [], "unknown": [],
                }), \
                mock.patch.object(policy.subprocess, "run", side_effect=malformed_pid):
            with self.assertRaisesRegex(RuntimeError, "identity is ambiguous for pid 58921"):
                policy._historical_terminal_liveness_check(
                    self.evidence, terminal_closed_at_unix=time.time() - 60)

    def test_future_reused_pid_start_time_fails_closed(self) -> None:
        started = datetime.datetime.fromtimestamp(
            time.time() + 60, datetime.timezone.utc).strftime("%a %b %d %H:%M:%S %Y")

        def future_pid(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            pid = args[2]
            return subprocess.CompletedProcess(
                args, 0, stdout=f"{pid} {started} /usr/sbin/diskutil info\n", stderr="")

        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value={
                    "active": [], "completed_pending_retirement": [],
                    "failed_or_retiring": [], "unknown": [],
                }), \
                mock.patch.object(policy.subprocess, "run", side_effect=future_pid):
            with self.assertRaisesRegex(RuntimeError, "start time is in the future for pid 58921"):
                policy._historical_terminal_liveness_check(
                    self.evidence, terminal_closed_at_unix=time.time() - 60)

    def test_unknown_process_liveness_blocks_closure(self) -> None:
        def unknown_process(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(args, 1, stdout="", stderr="permission denied")

        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value={
                    "active": [], "completed_pending_retirement": [],
                    "failed_or_retiring": [], "unknown": [],
                }), \
                mock.patch.object(policy.subprocess, "run", side_effect=unknown_process):
            with self.assertRaisesRegex(RuntimeError, "could not be checked"):
                policy._historical_terminal_liveness_check(self.evidence)

    def test_lsof_lookup_cannot_be_redirected_through_path(self) -> None:
        trusted_identity = {
            "path": "/usr/sbin/lsof", "sha256": "a" * 64,
            "file_identity": [1, 2, 3, 4, 5], "owner_uid": 0,
            "mode": 0o755, "parent_identity": [1, 6],
        }

        def empty_liveness_output(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            if args[0] == "/bin/ps":
                return subprocess.CompletedProcess(args, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                mock.patch.object(build_retention, "_snapshot", return_value={
                    "active": [], "completed_pending_retirement": [],
                    "failed_or_retiring": [], "unknown": [],
                }), \
                mock.patch.object(policy, "_historical_terminal_lsof_identity",
                                  return_value=trusted_identity), \
                mock.patch.object(policy.subprocess, "run", side_effect=empty_liveness_output) as run, \
                mock.patch.object(policy.shutil, "which", return_value="/private/tmp/spoofed/lsof") as which:
            result = policy._historical_terminal_liveness_check(self.evidence)

        self.assertEqual(result["lsof"], trusted_identity)
        self.assertEqual(run.call_args_list[-1].args[0][0], "/usr/sbin/lsof")
        which.assert_not_called()

    def test_tree_mutation_between_closure_measurements_leaves_no_certificate(self) -> None:
        changed_tree = dict(self.tree)
        changed_tree["tree_sha256"] = "e" * 64
        evidence = {
            "paths": {"output_dir": str(self.output)},
        }
        with mock.patch.object(policy, "acquire_storage_certification_lock", return_value=object()), \
                mock.patch.object(policy, "_acquire_storage_allocation_lock", return_value=object()), \
                mock.patch.object(policy, "release_storage_certification_lock"), \
                mock.patch.object(policy, "_release_storage_allocation_lock"), \
                mock.patch.object(policy, "_historical_terminal_copy_controller_evidence", return_value={}), \
                mock.patch.object(policy, "_historical_terminal_evidence", return_value=evidence), \
                mock.patch.object(policy, "_historical_terminal_liveness_check",
                                  return_value={"status": "QUIESCENT"}), \
                mock.patch.object(policy, "_historical_terminal_tree_snapshot",
                                  side_effect=[self.tree, changed_tree]):
            with self.assertRaisesRegex(RuntimeError, "changed between closure measurements"):
                policy.close_historical_terminal_run()
        self.assertFalse(policy._historical_terminal_closure_path().exists())
        self.assertFalse(policy._historical_terminal_closure_correction_path().exists())

    def test_writer_appends_correction_without_rewriting_legacy_certificate(self) -> None:
        _legacy, legacy_raw = self._write_legacy_closure_missing_path()
        legacy_hash_patcher = mock.patch.object(
            policy, "HISTORICAL_TERMINAL_LEGACY_CLOSURE_SHA256",
            hashlib.sha256(legacy_raw).hexdigest())
        legacy_hash_patcher.start()
        self.addCleanup(legacy_hash_patcher.stop)
        lock_patches = [
            mock.patch.object(policy, "acquire_storage_certification_lock", return_value=object()),
            mock.patch.object(policy, "_acquire_storage_allocation_lock", return_value=object()),
            mock.patch.object(policy, "release_storage_certification_lock"),
            mock.patch.object(policy, "_release_storage_allocation_lock"),
            mock.patch.object(policy, "_historical_terminal_copy_controller_evidence", return_value={}),
            mock.patch.object(policy, "_historical_terminal_evidence", return_value=self.evidence),
            mock.patch.object(policy, "_historical_terminal_liveness_check",
                              return_value={"status": "QUIESCENT"}),
            mock.patch.object(policy, "_historical_terminal_tree_snapshot", return_value=self.tree),
            mock.patch.object(policy, "_persistent_output_snapshot",
                              return_value=(self.tree["allocated_bytes"], self.metadata_tree_sha256)),
            mock.patch.object(policy, "_gate0_control_hashes", return_value={"gate0": "a" * 64}),
        ]
        for patcher in lock_patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        original_reader = policy._read_historical_terminal_closure

        def read_synthetic_correction() -> tuple[dict[str, object] | None, bytes | None, str | None]:
            correction_path = policy._historical_terminal_closure_correction_path()
            if correction_path.exists():
                correction_raw = correction_path.read_bytes()
                correction = json.loads(correction_raw)
                with mock.patch.object(policy, "HISTORICAL_TERMINAL_CORRECTION1_SHA256",
                                       hashlib.sha256(correction_raw).hexdigest()), \
                        mock.patch.object(policy, "HISTORICAL_TERMINAL_CORRECTION1_HMAC_SHA256",
                                          correction.get("closure_hmac_sha256")):
                    return original_reader()
            return original_reader()

        with mock.patch.object(policy, "_read_historical_terminal_closure",
                               side_effect=read_synthetic_correction):
            result = policy.close_historical_terminal_run()

        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(result["closure_path"], str(policy._historical_terminal_closure_correction_path()))
        self.assertEqual(policy._historical_terminal_closure_path().read_bytes(), legacy_raw)
        correction = json.loads(policy._historical_terminal_closure_correction_path().read_text())
        self.assertEqual(correction["supersedes_closure_sha256"], hashlib.sha256(legacy_raw).hexdigest())
        self.assertEqual(correction["closure_revision"], 1)

    def test_writer_refuses_legacy_certificate_with_wrong_pinned_hash(self) -> None:
        _legacy, legacy_raw = self._write_legacy_closure_missing_path()
        patchers = [
            mock.patch.object(policy, "acquire_storage_certification_lock", return_value=object()),
            mock.patch.object(policy, "_acquire_storage_allocation_lock", return_value=object()),
            mock.patch.object(policy, "release_storage_certification_lock"),
            mock.patch.object(policy, "_release_storage_allocation_lock"),
            mock.patch.object(policy, "_historical_terminal_copy_controller_evidence", return_value={}),
            mock.patch.object(policy, "HISTORICAL_TERMINAL_LEGACY_CLOSURE_SHA256", "f" * 64),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        with self.assertRaisesRegex(FileExistsError, "cannot be superseded"):
            policy.close_historical_terminal_run()

        self.assertEqual(policy._historical_terminal_closure_path().read_bytes(), legacy_raw)
        self.assertFalse(policy._historical_terminal_closure_correction_path().exists())


class HistoricalTerminalCorrection2Tests(unittest.TestCase):
    """Synthetic append and admission regressions for the one exact H5 transition."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-terminal-correction2-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.source_repo = Path(policy.__file__).resolve().parents[2]
        self.run_id = policy.HISTORICAL_TERMINAL_RUN_ID
        source_closure_root = self.source_repo / "experiments/storage/historical-terminal-closures"
        self.source_original_raw = (source_closure_root / f"{self.run_id}.json").read_bytes()
        self.source_correction1_raw = (source_closure_root / f"{self.run_id}.correction-1.json").read_bytes()
        self.original_template = json.loads(self.source_original_raw)
        self.correction1_template = json.loads(self.source_correction1_raw)

        self.repo_patcher = mock.patch.object(policy, "REPO", self.root)
        self.repo_patcher.start()
        self.addCleanup(self.repo_patcher.stop)
        self.output = policy._historical_terminal_run_paths()["output_dir"]
        self.output.mkdir(parents=True)
        self.manifest_path = policy._experiment_manifest_path(self.run_id)
        self.manifest_path.parent.mkdir(parents=True)
        self.manifest_raw = (self.source_repo / "experiments/storage/manifests"
                             / f"{self.run_id}.json").read_bytes()
        self.manifest_path.write_bytes(self.manifest_raw)
        self.manifest_sha256 = hashlib.sha256(self.manifest_raw).hexdigest()
        self.report_path = policy._historical_terminal_run_paths()["report"]
        self.report_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_path.write_bytes(b"synthetic immutable historical report prefix\n")

        self.closure_dir = policy._historical_terminal_closure_path().parent
        self.closure_dir.mkdir(parents=True, mode=0o700)
        _closure_fd, self.closure_identity = policy._open_historical_terminal_closure_directory()
        os.close(_closure_fd)
        self.source_hashes = policy._historical_terminal_source_hashes()
        self.control_hashes = policy._gate0_control_hashes()
        self.review_hashes = dict(policy.HISTORICAL_TERMINAL_H5_REVIEW_SHA256)
        self._copy_frozen_reviews()

        self.evidence = json.loads(json.dumps(self.correction1_template["historical_evidence"]))
        self.evidence["paths"]["output_dir"] = str(self.output)
        self.evidence["paths"]["run"] = str(self.output)
        self.evidence["paths"]["storage_manifest"] = str(self.manifest_path)
        self.evidence["paths"]["report"] = str(self.report_path)
        self.liveness = {"status": "QUIESCENT", "active_prefixes": [], "active_builds": []}

        self.original_path = policy._historical_terminal_closure_path()
        self.correction1_path = policy._historical_terminal_closure_correction_path()
        self.correction2_path = policy._historical_terminal_closure_correction2_path()
        self._write_synthetic_chain()
        self._write_precreation_validation()

        self.patchers = [
            mock.patch.object(policy, "HISTORICAL_TERMINAL_LEGACY_CLOSURE_SHA256",
                              hashlib.sha256(self.original_raw).hexdigest()),
            mock.patch.object(policy, "HISTORICAL_TERMINAL_CORRECTION1_SHA256",
                              hashlib.sha256(self.correction1_raw).hexdigest()),
            mock.patch.object(policy, "HISTORICAL_TERMINAL_CORRECTION1_HMAC_SHA256",
                              self.correction1["closure_hmac_sha256"]),
            mock.patch.object(policy, "_historical_terminal_source_hashes",
                              return_value=self.source_hashes),
            mock.patch.object(policy, "_gate0_control_hashes", return_value=self.control_hashes),
            mock.patch.object(policy, "_historical_terminal_copy_controller_evidence", return_value={}),
            mock.patch.object(policy, "_historical_terminal_evidence", return_value=self.evidence),
            mock.patch.object(policy, "_historical_terminal_liveness_check", return_value=self.liveness),
            mock.patch.object(policy, "_historical_terminal_tree_snapshot", return_value=self.current_tree),
            mock.patch.object(policy, "_persistent_output_snapshot", return_value=(
                policy.HISTORICAL_TERMINAL_CORRECTION2_OUTPUT_BYTES,
                policy.HISTORICAL_TERMINAL_CORRECTION2_METADATA_SHA256)),
            mock.patch.object(policy, "acquire_storage_certification_lock", return_value=object()),
            mock.patch.object(policy, "_acquire_storage_allocation_lock", return_value=object()),
            mock.patch.object(policy, "release_storage_certification_lock"),
            mock.patch.object(policy, "_release_storage_allocation_lock"),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _copy_frozen_reviews(self) -> None:
        source_root = (self.source_repo / "experiments/dxvk_macos_step11d3b/evidence/br9r2"
                       / "z9-finder-replay-20261014")
        target_root = policy._historical_terminal_h5_evidence_root()
        for name in self.review_hashes:
            target = target_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((source_root / name).read_bytes())

    def _write_synthetic_chain(self) -> None:
        _directory_fd, directory_identity = policy._open_historical_terminal_closure_directory()
        os.close(_directory_fd)
        report_prefix = {
            "path": str(self.report_path),
            "bytes": policy.HISTORICAL_TERMINAL_REPORT_PREFIX_BYTES,
            "sha256": policy.HISTORICAL_TERMINAL_REPORT_PREFIX_SHA256,
        }

        original = dict(self.original_template)
        original.update({
            "closure_directory_identity": list(directory_identity),
            "storage_manifest_path": str(self.manifest_path),
            "report_prefix": report_prefix,
            "output_dir": str(self.output),
            "historical_evidence": self.evidence,
            "tree_inventory": dict(original["tree_inventory"], output_dir=str(self.output)),
        })
        original = policy._seal_storage_evidence(original, "closure_hmac_sha256")
        self.original_raw = policy._canonical_pretty_json_bytes(original)
        self.original_path.write_bytes(self.original_raw)
        self.original_path.chmod(0o600)

        correction1 = dict(self.correction1_template)
        correction1.update({
            "closure_path": str(self.correction1_path),
            "closure_revision": 1,
            "supersedes_closure_path": str(self.original_path),
            "supersedes_closure_sha256": hashlib.sha256(self.original_raw).hexdigest(),
            "closure_directory_identity": list(directory_identity),
            "storage_manifest_path": str(self.manifest_path),
            "report_prefix": report_prefix,
            "output_dir": str(self.output),
            "historical_evidence": self.evidence,
            "tree_inventory": dict(correction1["tree_inventory"], output_dir=str(self.output)),
            "tree_root_identity": correction1["tree_inventory"]["root_identity"],
            "tree_parent_identity": correction1["tree_inventory"]["parent_identity"],
        })
        correction1 = policy._seal_storage_evidence(correction1, "closure_hmac_sha256")
        self.correction1_raw = policy._canonical_pretty_json_bytes(correction1)
        self.correction1_path.write_bytes(self.correction1_raw)
        self.correction1_path.chmod(0o600)
        self.correction1 = correction1
        predecessor = correction1["tree_inventory"]
        self.current_tree = policy._historical_terminal_correction2_expected_inventory(predecessor)

    def _write_precreation_validation(self) -> None:
        evidence_root = policy._historical_terminal_h5_evidence_root()
        test_runs: dict[str, dict[str, object]] = {}
        for key, name, count in (
                ("focused", "H5-C-FOCUSED-TESTS.log", 1),
                ("full_storage", "H5-C-FULL-STORAGE-TESTS.log", 2)):
            log_path = evidence_root / name
            log_raw = f"Ran {count} tests in 0.001s\n\nOK\n".encode("utf-8")
            log_path.write_bytes(log_raw)
            test_runs[key] = {
                "command": "/opt/homebrew/bin/python3.14 -m unittest test_storage_policy",
                "log_path": str(log_path),
                "log_sha256": hashlib.sha256(log_raw).hexdigest(),
                "tests_run": count,
                "status": "PASS",
            }
        record = {
            "schema_version": 1,
            "artifact_type": "H5-C-PRECREATION-VALIDATION",
            "status": "PASS",
            "experiment_id": self.run_id,
            "original_closure_path": str(self.original_path),
            "correction1_path": str(self.correction1_path),
            "correction1_sha256": hashlib.sha256(self.correction1_raw).hexdigest(),
            "correction1_hmac_sha256": self.correction1["closure_hmac_sha256"],
            "source_sha256": self.source_hashes,
            "gate0_control_sha256": self.control_hashes,
            "review_sha256": self.review_hashes,
            "test_runs": test_runs,
        }
        path = policy._historical_terminal_h5_precreation_validation_path()
        path.write_bytes(policy._canonical_pretty_json_bytes(record))
        path.chmod(0o600)

    def _write_d_binding(self, correction2_raw: bytes) -> None:
        evidence_root = policy._historical_terminal_h5_evidence_root()
        report_path = evidence_root / "H5-D-FRESH-REVIEW.md"
        report_raw = b"Independent synthetic D review of the exact correction-2 fixture.\n"
        report_path.write_bytes(report_raw)
        record = {
            "schema_version": 1,
            "artifact_type": "H5-D-FINAL-BINDING",
            "status": "PASS",
            "experiment_id": self.run_id,
            "correction2_path": str(self.correction2_path),
            "correction2_sha256": hashlib.sha256(correction2_raw).hexdigest(),
            "precreation_validation_path": str(policy._historical_terminal_h5_precreation_validation_path()),
            "precreation_validation_sha256": hashlib.sha256(
                policy._historical_terminal_h5_precreation_validation_path().read_bytes()).hexdigest(),
            "source_sha256": self.source_hashes,
            "gate0_control_sha256": self.control_hashes,
            "review_sha256": self.review_hashes,
            "review_report_path": str(report_path),
            "review_report_sha256": hashlib.sha256(report_raw).hexdigest(),
        }
        path = policy._historical_terminal_h5_final_review_binding_path()
        path.write_bytes(policy._canonical_pretty_json_bytes(record))
        path.chmod(0o600)

    def _verify(self, *, inventory: dict[str, object] | None = None) -> tuple[dict[str, object] | None, list[str]]:
        with mock.patch.object(policy, "_historical_terminal_tree_snapshot",
                               return_value=inventory if inventory is not None else self.current_tree):
            return policy._verify_historical_terminal_closure(
                {"experiment_id": self.run_id,
                 "estimated_persistent_bytes": policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES},
                self.manifest_sha256,
                self.output,
                allocated_bytes=policy.HISTORICAL_TERMINAL_CORRECTION2_OUTPUT_BYTES,
                metadata_tree_sha256=policy.HISTORICAL_TERMINAL_CORRECTION2_METADATA_SHA256,
            )

    def _append(self) -> dict[str, object]:
        return policy.close_historical_terminal_run()

    def test_exact_transition_appends_once_verifies_and_cannot_authorize_prefixlease(self) -> None:
        original_before = self.original_path.read_bytes()
        correction1_before = self.correction1_path.read_bytes()
        result = self._append()
        correction2_raw = self.correction2_path.read_bytes()
        correction2 = json.loads(correction2_raw)
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(correction2["closure_revision"], 2)
        self.assertEqual(correction2["tree_inventory"], self.current_tree)
        self.assertEqual(correction2["closed_tree_allocated_bytes"], 724_766_720)
        self.assertEqual(correction2["closed_tree_logical_bytes"], 720_231_108)
        self.assertEqual(self.original_path.read_bytes(), original_before)
        self.assertEqual(self.correction1_path.read_bytes(), correction1_before)

        self._write_d_binding(correction2_raw)
        binding, blockers = self._verify()
        self.assertEqual(blockers, [])
        self.assertIsNotNone(binding)
        self.assertEqual(binding["closure_path"], str(self.correction2_path))
        self.assertFalse(correction2["authorizes_execution"])
        self.assertTrue(correction2["counts_toward_project_scope"])
        with self.assertRaises(ValueError):
            policy._validated_manifest_estimates(correction2)
        self.assertFalse(policy._verify_manifest_signature(correction2))

        prefix_root = self.root / "prefixes"
        prefix_root.mkdir()
        output = self.root / "prefixlease-output"
        output.mkdir()
        with mock.patch.object(policy, "PREFIX_ROOT", prefix_root), \
                mock.patch.object(Path, "home", return_value=self.root):
            lease = policy.PrefixLease(
                prefix_root / "attempted-prefix", output, "synthetic-prefixlease", self.root / "wineserver",
                template=self.root / "template", manifest_path=self.correction2_path)
            with self.assertRaisesRegex(RuntimeError, "edited or unsigned storage manifest"):
                lease.__enter__()

    def test_revision2_verifies_against_capture_time_source_and_control_maps(self) -> None:
        self._append()
        correction2_raw = self.correction2_path.read_bytes()
        correction2 = json.loads(correction2_raw)
        self._write_d_binding(correction2_raw)
        current_source_hashes = {
            name: hashlib.sha256(("S2-source:" + name).encode()).hexdigest()
            for name in self.source_hashes
        }
        current_control_hashes = {
            name: hashlib.sha256(("S2-control:" + name).encode()).hexdigest()
            for name in self.control_hashes
        }

        with mock.patch.object(policy, "_historical_terminal_source_hashes",
                               return_value=current_source_hashes), \
                mock.patch.object(policy, "_gate0_control_hashes",
                                  return_value=current_control_hashes):
            binding, blockers = self._verify()

        self.assertEqual(blockers, [])
        self.assertIsNotNone(binding)
        self.assertEqual(correction2["source_sha256"], self.source_hashes)
        self.assertEqual(correction2["gate0_control_sha256"], self.control_hashes)
        self.assertNotEqual(correction2["source_sha256"], current_source_hashes)
        self.assertNotEqual(correction2["gate0_control_sha256"], current_control_hashes)

    def test_exact_transition_rejects_every_unlisted_inventory_change(self) -> None:
        mutations = {
            "content digest": lambda row: row["entries"][1].update(sha256="f" * 64),
            "runtime inode": lambda row: row["entries"][[e["path"] for e in row["entries"]].index(
                "runtime.log")]["identity"].__setitem__(1, 7),
            "runtime block count too low": lambda row: row["entries"][[e["path"] for e in row["entries"]].index(
                "runtime.log")]["identity"].__setitem__(8, 2_303),
            "runtime block count unchanged": lambda row: row["entries"][[e["path"] for e in row["entries"]].index(
                "runtime.log")]["identity"].__setitem__(8, 4_104),
            "other file allocation": lambda row: row["entries"][1]["identity"].__setitem__(8,
                row["entries"][1]["identity"][8] + 1),
            "other file allocation decrease": lambda row: row["entries"][1]["identity"].__setitem__(8,
                row["entries"][1]["identity"][8] - 1),
            "extra path": lambda row: row["entries"].append({"path": "extra", "kind": "directory",
                "identity": [1] * 9}),
            "missing path": lambda row: row["entries"].pop(),
            "renamed path": lambda row: row["entries"][1].update(path="renamed"),
            "root identity": lambda row: row.update(root_identity=[1, 2, 3]),
            "parent identity": lambda row: row.update(parent_identity=[1, 2]),
            "logical total": lambda row: row.update(logical_bytes=row["logical_bytes"] + 1),
            "allocation total": lambda row: row.update(allocated_bytes=row["allocated_bytes"] + 512),
            "tree digest": lambda row: row.update(tree_sha256="e" * 64),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(self.current_tree))
                mutate(changed)
                with self.assertRaises(ValueError):
                    policy._require_historical_terminal_correction2_transition(
                        self.correction1["tree_inventory"], changed)

    def test_preappend_race_does_not_create_correction2(self) -> None:
        changed = json.loads(json.dumps(self.current_tree))
        changed["tree_sha256"] = "f" * 64
        with mock.patch.object(policy, "_historical_terminal_tree_snapshot",
                               side_effect=[self.current_tree, changed]):
            with self.assertRaisesRegex(ValueError, "exact correction-2 predecessor transition"):
                self._append()
        self.assertFalse(self.correction2_path.exists())
        self.assertEqual(self.original_path.read_bytes(), self.original_raw)
        self.assertEqual(self.correction1_path.read_bytes(), self.correction1_raw)

    def test_postwrite_race_keeps_invalid_append_and_preserves_predecessors(self) -> None:
        original_before = self.original_path.read_bytes()
        correction1_before = self.correction1_path.read_bytes()
        changed = json.loads(json.dumps(self.current_tree))
        changed["tree_sha256"] = "f" * 64
        with mock.patch.object(policy, "_historical_terminal_tree_snapshot",
                               side_effect=[self.current_tree, self.current_tree, self.current_tree,
                                            self.current_tree, changed]):
            with self.assertRaisesRegex(ValueError, "exact correction-2 predecessor transition"):
                self._append()
        self.assertTrue(self.correction2_path.exists())
        self.assertEqual(self.original_path.read_bytes(), original_before)
        self.assertEqual(self.correction1_path.read_bytes(), correction1_before)
        invalid_correction2 = self.correction2_path.read_bytes()
        with self.assertRaisesRegex(FileExistsError, "append-only"):
            self._append()
        self.assertEqual(self.correction2_path.read_bytes(), invalid_correction2)

    def test_liveness_and_stale_test_log_block_before_append(self) -> None:
        with mock.patch.object(policy, "_historical_terminal_liveness_check",
                               side_effect=RuntimeError("unknown liveness")):
            with self.assertRaisesRegex(RuntimeError, "unknown liveness"):
                self._append()
        self.assertFalse(self.correction2_path.exists())

        log_path = policy._historical_terminal_h5_evidence_root() / "H5-C-FOCUSED-TESTS.log"
        log_path.write_bytes(log_path.read_bytes() + b"stale\n")
        with self.assertRaisesRegex(ValueError, "test log is not a matching passing run"):
            self._append()
        self.assertFalse(self.correction2_path.exists())

    def test_stale_source_control_or_review_binding_blocks_before_append(self) -> None:
        validation_path = policy._historical_terminal_h5_precreation_validation_path()
        pristine = validation_path.read_bytes()
        original = json.loads(pristine)
        for field in ("source_sha256", "gate0_control_sha256"):
            with self.subTest(field=field):
                changed = json.loads(json.dumps(original))
                first_key = next(iter(changed[field]))
                changed[field][first_key] = "f" * 64
                validation_path.write_bytes(policy._canonical_pretty_json_bytes(changed))
                with self.assertRaisesRegex(ValueError, "stale or does not bind"):
                    self._append()
                self.assertFalse(self.correction2_path.exists())
        validation_path.write_bytes(pristine)

        review_path = policy._historical_terminal_h5_evidence_root() / "H4-FRESH-REVIEW.md"
        review_path.write_bytes(review_path.read_bytes() + b"stale review\n")
        with self.assertRaisesRegex(RuntimeError, "frozen H5/H4 review changed"):
            self._append()
        self.assertFalse(self.correction2_path.exists())

    def test_immutable_classification_transition_and_d_binding_are_enforced(self) -> None:
        self._append()
        pristine = self.correction2_path.read_bytes()
        mutations = {
            "wrong runtime block claim": lambda row: row["correction2_transition"].update(current=2_305),
            "wrong logical total": lambda row: row.update(closed_tree_logical_bytes=720_231_109),
            "wrong parent identity": lambda row: row.update(tree_parent_identity=[1, 2]),
            "wrong run": lambda row: row.update(experiment_id="another-run"),
            "wrong C1 path": lambda row: row.update(supersedes_closure_path="/copied/correction-1.json"),
            "wrong predecessor SHA": lambda row: row.update(supersedes_closure_sha256="f" * 64),
            "wrong predecessor HMAC": lambda row: row.update(supersedes_closure_hmac_sha256="f" * 64),
            "wrong manifest path": lambda row: row.update(storage_manifest_path="/copied/manifest.json"),
            "wrong manifest SHA": lambda row: row.update(storage_manifest_sha256="f" * 64),
            "wrong manifest HMAC": lambda row: row.update(manifest_hmac_sha256="f" * 64),
            "wrong reservation": lambda row: row.update(original_reservation_bytes=400_000_000),
            "POST changed": lambda row: row.update(gate0_post_status="PASS"),
            "blockers changed": lambda row: row.update(gate0_post_blockers=[]),
            "result changed": lambda row: row.update(run_result_status="PASS"),
            "R5 changed": lambda row: row.update(r5_classification="VALID"),
            "T8 changed": lambda row: row.update(t8_classification="PASS"),
            "Finder changed": lambda row: row.update(finder_treatment="EXECUTED"),
            "execution authorization": lambda row: row.update(authorizes_execution=True),
            "project accounting omission": lambda row: row.update(counts_toward_project_scope=False),
            "controller evidence changed": lambda row: row["historical_evidence"][
                "controller_evidence_sha256"].update({"controller.json": "f" * 64}),
            "report prefix changed": lambda row: row["report_prefix"].update(sha256="f" * 64),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                correction2 = json.loads(pristine)
                mutate(correction2)
                raw = policy._canonical_pretty_json_bytes(
                    policy._seal_storage_evidence(correction2, "closure_hmac_sha256"))
                self.correction2_path.write_bytes(raw)
                self._write_d_binding(raw)
                binding, blockers = self._verify()
                self.assertIsNone(binding)
                self.assertTrue(blockers)
                self.correction2_path.write_bytes(pristine)
        self._write_d_binding(pristine)

    def test_extra_chain_member_and_stale_d_binding_fail_closed(self) -> None:
        self._append()
        correction2_raw = self.correction2_path.read_bytes()
        self._write_d_binding(correction2_raw)
        binding_path = policy._historical_terminal_h5_final_review_binding_path()
        binding = json.loads(binding_path.read_bytes())
        binding["correction2_sha256"] = "f" * 64
        binding_path.write_bytes(policy._canonical_pretty_json_bytes(binding))
        result, blockers = self._verify()
        self.assertIsNone(result)
        self.assertTrue(any("independent H5-D binding" in item for item in blockers))

        binding_path.write_bytes(policy._canonical_pretty_json_bytes(
            self._expected_d_binding(correction2_raw)))
        (self.closure_dir / "unexpected-copy.json").write_bytes(b"copy")
        result, blockers = self._verify()
        self.assertIsNone(result)
        self.assertTrue(any("missing or unexpected entries" in item for item in blockers))

    def test_missing_correction1_predecessor_blocks_correction2(self) -> None:
        self._append()
        correction1_raw = self.correction1_path.read_bytes()
        self.correction1_path.unlink()
        result, blockers = self._verify()
        self.assertIsNone(result)
        self.assertTrue(blockers)
        self.assertTrue(self.correction2_path.exists())
        self.assertEqual(self.original_path.read_bytes(), self.original_raw)
        self.correction1_path.write_bytes(correction1_raw)
        self.correction1_path.chmod(0o600)

    def _expected_d_binding(self, correction2_raw: bytes) -> dict[str, object]:
        self._write_d_binding(correction2_raw)
        return json.loads(policy._historical_terminal_h5_final_review_binding_path().read_bytes())


class HistoricalTerminalCorrection3Tests(unittest.TestCase):
    """Synthetic append, verifier, and race regressions for the exact G2 successor."""

    def setUp(self) -> None:
        self.fixture = HistoricalTerminalCorrection2Tests("test_exact_transition_appends_once_verifies_and_cannot_authorize_prefixlease")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture._append()
        self.correction2_raw = self.fixture.correction2_path.read_bytes()
        self.correction2 = json.loads(self.correction2_raw)
        self.fixture._write_d_binding(self.correction2_raw)
        self.correction3_path = policy._historical_terminal_closure_correction3_path()
        self.original_raw = self.fixture.original_path.read_bytes()
        self.correction1_raw = self.fixture.correction1_path.read_bytes()
        self.current_tree = policy._historical_terminal_correction3_expected_inventory(
            self.correction2["tree_inventory"])
        self.current_source_hashes = {
            name: hashlib.sha256(("correction3-source:" + name).encode()).hexdigest()
            for name in self.fixture.source_hashes
        }
        self.current_control_hashes = {
            name: hashlib.sha256(("correction3-control:" + name).encode()).hexdigest()
            for name in self.fixture.control_hashes
        }
        policy._historical_terminal_source_hashes.return_value = self.current_source_hashes
        policy._gate0_control_hashes.return_value = self.current_control_hashes
        policy._historical_terminal_tree_snapshot.return_value = self.current_tree
        policy._persistent_output_snapshot.return_value = (
            policy.HISTORICAL_TERMINAL_CORRECTION3_OUTPUT_BYTES,
            policy.HISTORICAL_TERMINAL_CORRECTION3_METADATA_SHA256,
        )
        self.sha_patcher = mock.patch.object(
            policy, "HISTORICAL_TERMINAL_CORRECTION2_SHA256",
            hashlib.sha256(self.correction2_raw).hexdigest())
        self.hmac_patcher = mock.patch.object(
            policy, "HISTORICAL_TERMINAL_CORRECTION2_HMAC_SHA256",
            self.correction2["closure_hmac_sha256"])
        self.sha_patcher.start()
        self.hmac_patcher.start()
        self.addCleanup(self.hmac_patcher.stop)
        self.addCleanup(self.sha_patcher.stop)
        self._write_review_reports()

    def _write_review_reports(self) -> None:
        reconciliation = policy._historical_terminal_correction3_reconciliation_report_path()
        reconciliation.write_text(
            "Classification: A1 — allocation-only.\n\nG2 trigger: met.\n\nSAFE_RECONCILIATION = YES\n",
            encoding="utf-8")
        review = policy._historical_terminal_correction3_review_path()
        review.write_text(
            "Independent synthetic correction-3 review fixture.\n"
            f"CORRECTION2_SHA256 = {hashlib.sha256(self.correction2_raw).hexdigest()}\n"
            f"RECONCILIATION_SHA256 = {hashlib.sha256(reconciliation.read_bytes()).hexdigest()}\n"
            "SOURCE_SHA256 = " + json.dumps(
                self.current_source_hashes, sort_keys=True, separators=(",", ":")) + "\n"
            "GATE0_CONTROL_SHA256 = " + json.dumps(
                self.current_control_hashes, sort_keys=True, separators=(",", ":")) + "\n"
            "CORRECTION3_REVIEW = PASS\n",
            encoding="utf-8")

    def _verify_current(self, inventory: dict[str, object] | None = None
                        ) -> tuple[dict[str, object] | None, list[str]]:
        current = inventory if inventory is not None else self.current_tree
        with mock.patch.object(policy, "_historical_terminal_tree_snapshot", return_value=current):
            return policy._verify_historical_terminal_closure(
                {"experiment_id": self.fixture.run_id,
                 "estimated_persistent_bytes": policy.HISTORICAL_TERMINAL_ORIGINAL_RESERVATION_BYTES},
                self.fixture.manifest_sha256,
                self.fixture.output,
                allocated_bytes=policy.HISTORICAL_TERMINAL_CORRECTION3_OUTPUT_BYTES,
                metadata_tree_sha256=policy.HISTORICAL_TERMINAL_CORRECTION3_METADATA_SHA256,
            )

    def _append(self) -> dict[str, object]:
        return policy.append_historical_terminal_correction3()

    def test_exact_transition_appends_once_verifies_and_preserves_predecessors(self) -> None:
        before = {
            self.fixture.original_path: self.original_raw,
            self.fixture.correction1_path: self.correction1_raw,
            self.fixture.correction2_path: self.correction2_raw,
        }
        result = self._append()
        correction3_raw = self.correction3_path.read_bytes()
        correction3 = json.loads(correction3_raw)
        self.assertEqual(result["status"], "CLOSED")
        self.assertEqual(correction3["closure_revision"], 3)
        self.assertEqual(correction3["tree_inventory"], self.current_tree)
        self.assertEqual(correction3["closed_tree_allocated_bytes"], 720_261_120)
        self.assertEqual(correction3["closed_tree_logical_bytes"], 720_231_108)
        self.assertEqual(correction3["correction3_transition"]["allocation_delta_bytes"], -4_505_600)
        for path, raw in before.items():
            self.assertEqual(path.read_bytes(), raw)
        verified, blockers = self._verify_current()
        self.assertEqual(blockers, [])
        self.assertIsNotNone(verified)
        self.assertEqual(verified["closure_path"], str(self.correction3_path))
        self.assertFalse(correction3["authorizes_execution"])
        self.assertTrue(correction3["counts_toward_project_scope"])
        self.assertEqual(correction3["original_reservation_bytes"], 314_572_800)
        self.assertEqual(correction3["gate0_post_status"], "BLOCKED")
        self.assertEqual(correction3["r5_classification"], "INVALID / INCOMPLETE")
        self.assertEqual(correction3["t8_classification"], "INSUFFICIENT TIMING EVIDENCE")
        self.assertEqual(correction3["finder_treatment"], "NOT EXECUTED")

    def test_revision3_rechecks_use_authenticated_closure_timestamp(self) -> None:
        self._append()
        correction3 = json.loads(self.correction3_path.read_bytes())
        liveness_check = mock.Mock(return_value=self.fixture.liveness)
        with mock.patch.object(policy, "_historical_terminal_liveness_check", liveness_check):
            verified, blockers = self._verify_current()

        self.assertEqual(blockers, [])
        self.assertIsNotNone(verified)
        self.assertEqual(liveness_check.call_count, 2)
        for call in liveness_check.call_args_list:
            self.assertEqual(
                call.kwargs.get("terminal_closed_at_unix"),
                correction3["created_at_unix"],
            )

    def test_revision3_keeps_correction2_and_review_maps_when_current_identity_moves(self) -> None:
        self._append()
        correction3 = json.loads(self.correction3_path.read_bytes())
        current_source_hashes = {
            name: hashlib.sha256(("S3-source:" + name).encode()).hexdigest()
            for name in self.current_source_hashes
        }
        current_control_hashes = {
            name: hashlib.sha256(("S3-control:" + name).encode()).hexdigest()
            for name in self.current_control_hashes
        }

        with mock.patch.object(policy, "_historical_terminal_source_hashes",
                               return_value=current_source_hashes), \
                mock.patch.object(policy, "_gate0_control_hashes",
                                  return_value=current_control_hashes):
            binding, blockers = self._verify_current()

        self.assertEqual(blockers, [])
        self.assertIsNotNone(binding)
        self.assertEqual(correction3["source_sha256"], self.current_source_hashes)
        self.assertEqual(correction3["gate0_control_sha256"], self.current_control_hashes)
        self.assertEqual(correction3["correction2_source_sha256"], self.correction2["source_sha256"])
        self.assertEqual(correction3["correction2_gate0_control_sha256"],
                         self.correction2["gate0_control_sha256"])
        self.assertEqual(correction3["correction3_independent_review"]["source_sha256"],
                         self.current_source_hashes)
        self.assertEqual(correction3["correction3_independent_review"]["gate0_control_sha256"],
                         self.current_control_hashes)
        self.assertNotEqual(correction3["source_sha256"], current_source_hashes)
        self.assertNotEqual(correction3["gate0_control_sha256"], current_control_hashes)

    def test_exact_transition_rejects_every_unlisted_inventory_change(self) -> None:
        trace = "host-traces/host-trace.58921.jsonl"
        trace_index = [row["path"] for row in self.current_tree["entries"]].index(trace)
        other_file_index = 1
        mutations = {
            "changed content": lambda row: row["entries"][trace_index].update(sha256="f" * 64),
            "added path": lambda row: row["entries"].append({"path": "added", "kind": "directory",
                "identity": [1] * 9}),
            "deleted path": lambda row: row["entries"].pop(),
            "path substitution": lambda row: row["entries"][trace_index].update(path="renamed.jsonl"),
            "inode substitution": lambda row: row["entries"][trace_index]["identity"].__setitem__(1, 7),
            "unexpected trace allocation": lambda row: row["entries"][trace_index]["identity"].__setitem__(8, 712_217),
            "unexpected other allocation": lambda row: row["entries"][other_file_index]["identity"].__setitem__(
                8, row["entries"][other_file_index]["identity"][8] + 1),
            "extra total allocation": lambda row: row.update(allocated_bytes=row["allocated_bytes"] + 512),
            "tree digest substitution": lambda row: row.update(tree_sha256="a" * 64),
            "root substitution": lambda row: row.update(root_identity=[1, 2, 3]),
            "parent substitution": lambda row: row.update(parent_identity=[1, 2]),
            "logical total change": lambda row: row.update(logical_bytes=row["logical_bytes"] + 1),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(json.dumps(self.current_tree))
                mutate(changed)
                with self.assertRaises(ValueError):
                    policy._require_historical_terminal_correction3_transition(
                        self.correction2["tree_inventory"], changed)

    def test_append_rejects_stale_source_review_and_active_liveness(self) -> None:
        review_path = policy._historical_terminal_correction3_review_path()
        pristine_review = review_path.read_bytes()
        review_path.write_bytes(pristine_review.replace(
            b"CORRECTION3_REVIEW = PASS", b"CORRECTION3_REVIEW = FAIL"))
        with self.assertRaisesRegex(ValueError, "fresh correction-3 review"):
            self._append()
        self.assertFalse(self.correction3_path.exists())
        review_path.write_bytes(pristine_review)

        with mock.patch.object(policy, "_historical_terminal_source_hashes",
                               return_value={"stale.py": "f" * 64}):
            with self.assertRaisesRegex(ValueError, "fresh correction-3 review"):
                self._append()
        self.assertFalse(self.correction3_path.exists())

        for blocker in ("historical process 58921 is still present", "active PrefixLease marker"):
            with self.subTest(blocker=blocker):
                with mock.patch.object(policy, "_historical_terminal_liveness_check",
                                       side_effect=RuntimeError(blocker)):
                    with self.assertRaisesRegex(RuntimeError, blocker):
                        self._append()
                self.assertFalse(self.correction3_path.exists())

    def test_preappend_inventory_race_does_not_create_correction3(self) -> None:
        changed = json.loads(json.dumps(self.current_tree))
        changed["tree_sha256"] = "f" * 64
        with mock.patch.object(policy, "_historical_terminal_tree_snapshot",
                               side_effect=[self.current_tree, changed]):
            with self.assertRaisesRegex(ValueError, "exact correction-3 predecessor transition"):
                self._append()
        self.assertFalse(self.correction3_path.exists())
        self.assertEqual(self.fixture.correction2_path.read_bytes(), self.correction2_raw)

    def test_postwrite_inventory_race_keeps_invalid_append_and_preserves_predecessors(self) -> None:
        changed = json.loads(json.dumps(self.current_tree))
        changed["tree_sha256"] = "f" * 64
        with mock.patch.object(policy, "_historical_terminal_tree_snapshot",
                               side_effect=[self.current_tree, self.current_tree, self.current_tree]
                               + [changed] * 20):
            with self.assertRaisesRegex((RuntimeError, ValueError), "correction-3"):
                self._append()
        self.assertTrue(self.correction3_path.exists())
        self.assertEqual(self.fixture.original_path.read_bytes(), self.original_raw)
        self.assertEqual(self.fixture.correction1_path.read_bytes(), self.correction1_raw)
        self.assertEqual(self.fixture.correction2_path.read_bytes(), self.correction2_raw)

    def test_verifier_rejects_wrong_predecessor_stale_source_review_copy_and_authorization(self) -> None:
        self._append()
        pristine = self.correction3_path.read_bytes()
        mutations = {
            "wrong predecessor hash": lambda row: row.update(supersedes_closure_sha256="f" * 64),
            "wrong predecessor HMAC": lambda row: row.update(supersedes_closure_hmac_sha256="f" * 64),
            "wrong current source binding": lambda row: row.update(source_sha256={"storage_policy.py": "f" * 64}),
            "unexpected authorization": lambda row: row.update(authorizes_execution=True),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label):
                changed = json.loads(pristine)
                mutate(changed)
                sealed = policy._canonical_pretty_json_bytes(
                    policy._seal_storage_evidence(changed, "closure_hmac_sha256"))
                self.correction3_path.write_bytes(sealed)
                verified, blockers = self._verify_current()
                self.assertIsNone(verified)
                self.assertTrue(blockers)
                self.correction3_path.write_bytes(pristine)

        review_path = policy._historical_terminal_correction3_review_path()
        review_raw = review_path.read_bytes()
        review_path.write_bytes(review_raw + b"changed review\n")
        verified, blockers = self._verify_current()
        self.assertIsNone(verified)
        self.assertTrue(any("correction-3" in item for item in blockers))
        review_path.write_bytes(review_raw)

        copied = self.correction3_path.with_name(self.correction3_path.name + ".copy")
        copied.write_bytes(pristine)
        verified, blockers = self._verify_current()
        self.assertIsNone(verified)
        self.assertTrue(any("missing or unexpected entries" in item for item in blockers))
        copied.unlink()


class PrefixLeasePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-prefix-policy-test-", dir="/private/tmp")
        self.base = Path(self.temp.name)
        self.prefix_root = self.base / "prefixes"
        self.prefix_root.mkdir()
        self.active_lease_root = self.prefix_root / ".active-leases"
        self.active_lease_root.mkdir()
        (self.base / "Library/Caches/FGMetalStep11D-R").mkdir(parents=True)
        self.gate_attestation = self.base / "gate0-attestation.json"
        self.gate_attestation.write_text(json.dumps({
            "canonical_artifact_store_audit": {"sha256": "synthetic-cas-audit"}
        }), encoding="utf-8")
        self.addCleanup(self.temp.cleanup)
        patchers = [
            mock.patch.object(policy, "PREFIX_ROOT", self.prefix_root),
            mock.patch.object(policy, "ACTIVE_LEASE_ROOT", self.active_lease_root),
            mock.patch.object(policy, "GATE0_ATTESTATION_PATH", self.gate_attestation),
            mock.patch.object(Path, "home", return_value=self.base),
            mock.patch.object(policy, "_active_lease_markers", return_value=[]),
            mock.patch.object(policy, "disk_free_bytes", return_value=100 * policy.GIB),
            mock.patch.object(policy, "measure_project_usage",
                              return_value={"allocated_inode_deduplicated_bytes": 0}),
            mock.patch.object(build_retention, "canonical_artifact_store_audit",
                              return_value={"sha256": "synthetic-cas-audit"}),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _lease(self, name: str = "synthetic-prefix") -> policy.PrefixLease:
        prefix = self.prefix_root / name
        prefix.mkdir()
        (prefix / "system.reg").write_text("synthetic registry\n", encoding="utf-8")
        (prefix / "drive_c").mkdir()
        output = self.base / f"output-{name}"
        output.mkdir()
        lease = policy.PrefixLease(prefix, output, f"test-{name}", self.base / "wineserver",
                                   template=self.base / "template")
        lease._free_before = 100 * policy.GIB
        lease._project_before = {"allocated_inode_deduplicated_bytes": 0}
        lease._manifest = {"estimated_persistent_bytes": 0, "estimated_max_new_disk_bytes": 0}
        lease._prefix_created_any = True
        info = prefix.stat(follow_symlinks=False)
        lease._prefix_parent_fd = policy._open_directory_nofollow(self.prefix_root)
        lease._prefix_identity = (info.st_dev, info.st_ino)
        lease._leased_tree_absence_proven = False
        lease._release_global_lease_gate = lambda: None
        return lease

    def _wine_launch_lease(self, name: str = "wine-launch") -> policy.PrefixLease:
        lease = self._lease(name)
        lease._materialized = True
        lease._wine_loader_path = self.base / "runtime/bin/wine"
        lease._wine_loader_path.parent.mkdir(parents=True, exist_ok=True)
        lease._wine_loader_path.write_bytes(b"synthetic certified Wine executable\n")
        lease._wine_loader_path.chmod(0o755)
        (lease._wine_loader_sha256, lease._wine_loader_identity) = \
            policy._wine_loader_identity(lease._wine_loader_path)
        lease._env = {"WINEPREFIX": str(lease.prefix)}
        lease._sealed_wine_env = dict(lease._env)
        lease._runtime_env_verified = True
        return lease

    def test_prefixlease_output_budget_guard_stops_before_signed_limit(self) -> None:
        lease = self._lease("output-budget-guard")
        process = mock.Mock()
        process.poll.return_value = None
        process.args = ["/certified/wine"]
        lease._clone_process = None
        limit = 8 * policy.MIB
        threshold = limit - policy.MIB
        with mock.patch.object(policy, "_persistent_output_allocated_bytes",
                               return_value=threshold), \
                mock.patch.object(lease, "_terminate_and_reap", return_value=True) as terminate:
            with self.assertRaisesRegex(RuntimeError, "signed output budget guard stopped"):
                lease.wait_process(process, timeout_seconds=10, output_limit_bytes=limit)
        terminate.assert_called_once_with(process)
        event = lease._storage_watch_events[-1]
        self.assertEqual(event["action"], "STOP_OUTPUT_BUDGET_GUARD")
        self.assertEqual(event["output_allocated_bytes"], threshold)
        self.assertEqual(event["output_limit_bytes"], limit)
        self.assertFalse(lease._storage_violation)
        self.assertEqual(lease._managed_processes, [])

    def test_prefixlease_output_budget_guard_rejects_boolean_limit(self) -> None:
        lease = self._lease("output-budget-bool-rejected")
        with self.assertRaisesRegex(ValueError, "positive integer"):
            lease.wait_process(mock.Mock(), timeout_seconds=1, output_limit_bytes=True)

    def test_wine_launcher_accepts_certified_arch_exec(self) -> None:
        lease = self._wine_launch_lease()
        env = {"WINEPREFIX": str(lease.prefix)}
        process = mock.Mock()
        # This test isolates command selection; durable lease bindings are
        # exercised with real marker files in test_prefixlease_wineprefix_contract.
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(lease, "_assert_bound_prefix_lease"), \
                mock.patch.object(policy, "_spawn_in_directory_fd", return_value=process) as spawn:
            result = lease.start_wine_process(
                ["/usr/bin/arch", "-x86_64", str(lease._wine_loader_path), "wineboot", "-u"],
                env=env, runner_path=Path(__file__).resolve(), stdout=mock.sentinel.stdout)
        self.assertIs(result, process)
        self.assertEqual(spawn.call_args.args[1][:3],
                         ["/usr/bin/arch", "-x86_64", str(lease._wine_loader_path)])

    def test_wine_launcher_rejects_unrelated_executable_with_wine_argument(self) -> None:
        lease = self._wine_launch_lease("wine-shell-rejected")
        env = {"WINEPREFIX": str(lease.prefix)}
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "must execute the certified Wine binary"):
                lease.start_wine_process(
                    ["/bin/sh", "-c", "sleep 60 &", str(lease._wine_loader_path)],
                    env=env, runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_wine_launcher_rejects_process_control_overrides(self) -> None:
        lease = self._wine_launch_lease("wine-executable-override-rejected")
        env = {"WINEPREFIX": str(lease.prefix)}
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "unsupported process-control options: executable"):
                lease.start_wine_process(
                    [str(lease._wine_loader_path), "wineboot", "-u"], env=env,
                    runner_path=Path(__file__).resolve(), executable="/bin/sh")
        spawn.assert_not_called()

    def test_wine_launcher_rejects_environment_changes_after_sealing(self) -> None:
        lease = self._wine_launch_lease("wine-env-mismatch-rejected")
        env = {**lease._env, "DYLD_INSERT_LIBRARIES": "/tmp/host-child.dylib"}
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "forbidden dynamic-loader control"):
                lease.start_wine_process(
                    [str(lease._wine_loader_path), "wineboot", "-u"], env=env,
                    runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_wine_launcher_rejects_non_loader_startup_hooks(self) -> None:
        lease = self._wine_launch_lease("wine-startup-hook-rejected")
        env = {**lease._env, "LD_PRELOAD": "/tmp/host-child.dylib"}
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "forbidden process startup hook"):
                lease.start_wine_process(
                    [str(lease._wine_loader_path), "wineboot", "-u"], env=env,
                    runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_wine_launcher_rejects_python_site_startup_environment(self) -> None:
        lease = self._wine_launch_lease("wine-python-site-rejected")
        env = {**lease._env, "PYTHONUSERBASE": "/tmp/python-user-site"}
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "forbidden process startup hook"):
                lease.start_wine_process(
                    [str(lease._wine_loader_path), "wineboot", "-u"], env=env,
                    runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_wine_launcher_rejects_environment_mutation_even_if_not_a_loader_hook(self) -> None:
        lease = self._wine_launch_lease("wine-env-seal-rejected")
        env = {**lease._env, "LANG": "attacker-controlled"}
        with mock.patch.object(policy, "_assert_prefix_lease_locked"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "differs from the verified PrefixLease environment"):
                lease.start_wine_process(
                    [str(lease._wine_loader_path), "wineboot", "-u"], env=env,
                    runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_normal_cleanup_removes_prefix(self) -> None:
        lease = self._lease()
        self.assertFalse(lease.__exit__(None, None, None))
        self.assertFalse(lease.prefix.exists())
        receipt = json.loads((lease.output_dir / "prefix-cleanup.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "PASS")
        self.assertTrue(receipt["prefix_deleted"])

    def test_original_failure_still_runs_and_finishes_cleanup(self) -> None:
        lease = self._lease()
        failure = ValueError("synthetic run failure")
        self.assertFalse(lease.__exit__(ValueError, failure, None))
        self.assertFalse(lease.prefix.exists())
        receipt = json.loads((lease.output_dir / "prefix-cleanup.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["exception"], "ValueError('synthetic run failure')")
        self.assertEqual(receipt["status"], "FAIL")
        self.assertEqual(receipt["storage_hygiene_status"], "PASS")

    def test_system_exit_zero_cannot_hide_cleanup_failure(self) -> None:
        lease = self._lease()
        with mock.patch.object(policy, "_remove_child_directory_nofollow",
                               side_effect=PermissionError("synthetic cleanup failure")):
            with mock.patch.object(policy, "preserved_prefixes", return_value=[]):
                with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
                    lease.__exit__(SystemExit, SystemExit(0), None)
        self.assertTrue(lease.prefix.is_dir())
        receipt = json.loads((lease.output_dir / "prefix-cleanup.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "FAIL")
        self.assertFalse(receipt["prefix_deleted"])

    def test_uncertified_prefix_is_rejected(self) -> None:
        prefix = self.prefix_root / "orphan"
        prefix.mkdir()
        (prefix / "system.reg").write_text("orphan\n", encoding="utf-8")
        (prefix / "drive_c").mkdir()
        self.assertIn(prefix, policy.uncertified_prefixes())

    def test_preservation_stays_within_configured_cap(self) -> None:
        lease = self._lease()
        lease._materialized = True
        lease._stop_wineserver = lambda: (False, [{"returncode": 1}])
        with mock.patch.object(policy, "preserved_prefixes", return_value=[self.base / "existing-preserved"]):
            with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
                lease.__exit__(None, None, None)
        marker = lease.prefix / "PRESERVE_PREFIX.json"
        self.assertTrue(marker.is_file())
        self.assertEqual(len(policy.preserved_prefixes()), 1)

    def test_full_preserved_cap_leaves_uncertified_prefix_for_gate_block(self) -> None:
        lease = self._lease()
        lease._materialized = True
        lease._stop_wineserver = lambda: (False, [{"returncode": 1}])
        existing = [self.base / "kept-one", self.base / "kept-two"]
        with mock.patch.object(policy, "preserved_prefixes", return_value=existing):
            with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
                lease.__exit__(None, None, None)
        self.assertFalse((lease.prefix / "PRESERVE_PREFIX.json").exists())
        self.assertIn(lease.prefix, policy.uncertified_prefixes())

    def test_clone_target_identity_is_bound_before_materialization(self) -> None:
        lease = self._lease("clone-identity")
        os.close(lease._prefix_parent_fd)
        lease._prefix_parent_fd = policy._open_directory_nofollow(self.prefix_root)
        name = ".clone-target"
        (self.prefix_root / name).mkdir()
        info = (self.prefix_root / name).stat()
        lease._clone_temp_name = name
        lease._clone_temp_identity = (info.st_dev, info.st_ino)
        lease._clone_temp_fd = os.open(name, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                       | getattr(os, "O_NOFOLLOW", 0), dir_fd=lease._prefix_parent_fd)
        lease._assert_clone_target_real()
        (self.prefix_root / name).rename(self.prefix_root / ".original-clone-target")
        (self.prefix_root / name).mkdir()
        with self.assertRaisesRegex(OSError, "identity or filesystem changed"):
            lease._assert_clone_target_real()
        os.close(lease._clone_temp_fd)
        lease._clone_temp_fd = None
        os.close(lease._prefix_parent_fd)
        lease._prefix_parent_fd = None

    def test_unmaterialized_lease_never_deletes_directory_that_appears_after_entry(self) -> None:
        prefix = self.prefix_root / "appeared-after-entry"
        output = self.base / "output-appeared-after-entry"
        output.mkdir()
        lease = policy.PrefixLease(prefix, output, "test-appeared-after-entry", self.base / "wineserver",
                                   template=self.base / "template")
        lease._free_before = 100 * policy.GIB
        lease._project_before = {"allocated_inode_deduplicated_bytes": 0}
        lease._manifest = {"estimated_persistent_bytes": 0, "estimated_max_new_disk_bytes": 0}
        lease._release_global_lease_gate = lambda: None
        active_root = self.prefix_root / ".active-leases"
        active_root.mkdir(exist_ok=True)
        lease._lease_marker_path = active_root / "test-appeared-after-entry.json"
        lease._manifest_sha256 = "d" * 64
        lease._write_active_lease("PREPARED")
        prefix.mkdir()
        sentinel = prefix / "owner-data.txt"
        sentinel.write_text("belongs to another process", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
            lease.__exit__(None, None, None)

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "belongs to another process")
        self.assertFalse((prefix / "PRESERVE_PREFIX.json").exists())
        self.assertTrue(lease._lease_marker_path.is_file())
        marker = json.loads(lease._lease_marker_path.read_text())
        self.assertEqual(marker["state"], "RECONCILIATION_REQUIRED")

    def test_unbound_prefix_observation_keeps_marker_if_foreign_tree_moves_away(self) -> None:
        prefix = self.prefix_root / "foreign-tree-moves-away"
        output = self.base / "output-foreign-tree-moves-away"
        output.mkdir()
        lease = policy.PrefixLease(prefix, output, "test-foreign-tree-moves-away", self.base / "wineserver",
                                   template=self.base / "template")
        lease._free_before = 100 * policy.GIB
        lease._project_before = {"allocated_inode_deduplicated_bytes": 0}
        lease._manifest = {"estimated_persistent_bytes": 0, "estimated_max_new_disk_bytes": 0}
        lease._release_global_lease_gate = lambda: None
        active_root = self.prefix_root / ".active-leases"
        active_root.mkdir(exist_ok=True)
        lease._lease_marker_path = active_root / "test-foreign-tree-moves-away.json"
        lease._manifest_sha256 = "f" * 64
        lease._write_active_lease("PREPARED")
        prefix.mkdir()
        sentinel = prefix / "owner-data.txt"
        sentinel.write_text("belongs to another process", encoding="utf-8")
        moved = self.base / "foreign-tree-moved-away"
        stat_original = policy._stat_entry_nofollow
        lease_path_calls = 0

        def move_after_unsafe_observation(path):
            nonlocal lease_path_calls
            if Path(path) == prefix:
                lease_path_calls += 1
                if lease_path_calls == 2:
                    prefix.rename(moved)
            return stat_original(path)

        with mock.patch.object(policy, "_stat_entry_nofollow", side_effect=move_after_unsafe_observation):
            with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
                lease.__exit__(None, None, None)

        self.assertEqual(lease_path_calls, 3)
        self.assertEqual((moved / "owner-data.txt").read_text(encoding="utf-8"),
                         "belongs to another process")
        self.assertTrue(lease._lease_marker_path.is_file())
        self.assertEqual(json.loads(lease._lease_marker_path.read_text())["state"], "RECONCILIATION_REQUIRED")
        receipt = json.loads((output / "prefix-cleanup.json").read_text(encoding="utf-8"))
        self.assertFalse(receipt["active_lease_marker_removed_after_receipt"])

    def test_unbound_symlink_observation_keeps_marker_if_link_moves_away(self) -> None:
        prefix = self.prefix_root / "foreign-link-moves-away"
        output = self.base / "output-foreign-link-moves-away"
        output.mkdir()
        target = self.base / "foreign-link-target"
        target.mkdir()
        sentinel = target / "owner-data.txt"
        sentinel.write_text("belongs to another process", encoding="utf-8")
        lease = policy.PrefixLease(prefix, output, "test-foreign-link-moves-away", self.base / "wineserver",
                                   template=self.base / "template")
        lease._free_before = 100 * policy.GIB
        lease._project_before = {"allocated_inode_deduplicated_bytes": 0}
        lease._manifest = {"estimated_persistent_bytes": 0, "estimated_max_new_disk_bytes": 0}
        lease._release_global_lease_gate = lambda: None
        active_root = self.prefix_root / ".active-leases"
        active_root.mkdir(exist_ok=True)
        lease._lease_marker_path = active_root / "test-foreign-link-moves-away.json"
        lease._manifest_sha256 = "a" * 64
        lease._write_active_lease("PREPARED")
        prefix.symlink_to(target, target_is_directory=True)
        moved = self.base / "foreign-link-moved-away"
        stat_original = policy._stat_entry_nofollow
        calls = 0

        def move_after_unsafe_observation(path):
            nonlocal calls
            info = stat_original(path)
            if Path(path) == prefix:
                calls += 1
                if calls == 1:
                    prefix.rename(moved)
            return info

        with mock.patch.object(policy, "_stat_entry_nofollow", side_effect=move_after_unsafe_observation):
            with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
                lease.__exit__(None, None, None)

        self.assertTrue(moved.is_symlink())
        self.assertEqual(os.readlink(moved), str(target))
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "belongs to another process")
        self.assertTrue(lease._lease_marker_path.is_file())
        self.assertEqual(json.loads(lease._lease_marker_path.read_text())["state"], "RECONCILIATION_REQUIRED")

    def test_moved_bound_prefix_does_not_release_marker_as_if_deleted(self) -> None:
        lease = self._lease("moved-bound-prefix")
        lease._created = True
        lease._materialized = True
        marker_root = self.prefix_root / ".active-leases"
        marker_root.mkdir(exist_ok=True)
        lease._lease_marker_path = marker_root / "test-moved-bound-prefix.json"
        lease._manifest_sha256 = "e" * 64
        lease._write_active_lease("MATERIALIZED")
        moved = self.base / "moved-bound-prefix"
        lease.prefix.rename(moved)

        with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
            lease.__exit__(None, None, None)

        self.assertTrue((moved / "system.reg").is_file())
        self.assertTrue(lease._lease_marker_path.is_file())
        marker = json.loads(lease._lease_marker_path.read_text())
        self.assertEqual(marker["state"], "RECONCILIATION_REQUIRED")
        receipt = json.loads((lease.output_dir / "prefix-cleanup.json").read_text())
        self.assertFalse(receipt["prefix_deleted"])
        self.assertFalse(receipt["active_lease_marker_removed_after_receipt"])

    def test_descriptor_bound_clone_child_cannot_be_redirected_by_name_swap(self) -> None:
        target = self.prefix_root / (".prefix.clone-" + "a" * 32)
        target.mkdir()
        outside = self.base / "outside"
        outside.mkdir()
        target_fd = os.open(target, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                            | getattr(os, "O_NOFOLLOW", 0))
        moved = self.prefix_root / ".prefix.clone-original"
        target.rename(moved)
        redirected = self.prefix_root / (".prefix.clone-" + "a" * 32)
        redirected.symlink_to(outside, target_is_directory=True)
        try:
            proc = policy._spawn_in_directory_fd(
                target_fd,
                [os.sys.executable, "-c", "from pathlib import Path; Path('copied').write_text('pinned')"],
                stdout=policy.subprocess.PIPE,
                stderr=policy.subprocess.PIPE,
            )
            proc.communicate(timeout=10)
            self.assertEqual(proc.returncode, 0)
            self.assertIn("-I", proc.args)
            self.assertIn("-S", proc.args)
            self.assertEqual((moved / "copied").read_text(encoding="utf-8"), "pinned")
            self.assertFalse((outside / "copied").exists())
        finally:
            os.close(target_fd)

    def test_prefix_cleanup_quarantines_then_refuses_replaced_leaf(self) -> None:
        target = self.prefix_root / "quarantine-target"
        target.mkdir()
        payload = target / "payload"
        payload.write_text("original", encoding="utf-8")
        parent_fd = policy._open_directory_nofollow(self.prefix_root)
        real_rename = policy._rename_child_exclusive
        swapped = False

        def replace_before_quarantine(directory_fd, source_name, target_name, *, sync=True):
            nonlocal swapped
            if source_name == "payload" and not swapped:
                swapped = True
                os.rename(source_name, "original-moved", src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
                replacement = os.open(source_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                      0o600, dir_fd=directory_fd)
                os.write(replacement, b"replacement")
                os.close(replacement)
            return real_rename(directory_fd, source_name, target_name, sync=sync)

        try:
            with mock.patch.object(policy, "_rename_child_exclusive", side_effect=replace_before_quarantine):
                with self.assertRaisesRegex(OSError, "entry changed during atomic cleanup quarantine"):
                    policy._remove_child_directory_nofollow(
                        parent_fd, target.name, expected_identity=(target.stat().st_dev, target.stat().st_ino))
        finally:
            os.close(parent_fd)
        self.assertEqual((target / "payload").read_text(encoding="utf-8"), "replacement")
        self.assertEqual((target / "original-moved").read_text(encoding="utf-8"), "original")

    def test_live_clone_process_keeps_clone_tree_and_active_marker(self) -> None:
        lease = self._lease("live-clone-preserved")
        marker_root = self.prefix_root / ".active-leases"
        marker_root.mkdir(exist_ok=True)
        lease._lease_marker_path = marker_root / "test-live-clone-preserved.json"
        lease._manifest_sha256 = "b" * 64
        lease._clone_temp_name = ".live-clone"
        target = self.prefix_root / lease._clone_temp_name
        target.mkdir()
        info = target.stat()
        lease._clone_temp_identity = (info.st_dev, info.st_ino)
        lease._clone_temp_fd = os.open(lease._clone_temp_name,
                                       os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                       | getattr(os, "O_NOFOLLOW", 0), dir_fd=lease._prefix_parent_fd)
        lease._write_active_lease("MATERIALIZING")
        lease._unsafe_live_process = True
        with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
            lease.__exit__(None, None, None)
        self.assertTrue(target.is_dir())
        self.assertTrue(lease._lease_marker_path.is_file())
        self.assertEqual(json.loads(lease._lease_marker_path.read_text())["state"], "RECONCILIATION_REQUIRED")

    def test_lost_clone_name_does_not_authorize_marker_removal(self) -> None:
        lease = self._lease("lost-clone-name")
        marker_root = self.prefix_root / ".active-leases"
        marker_root.mkdir(exist_ok=True)
        lease._lease_marker_path = marker_root / "test-lost-clone-name.json"
        lease._manifest_sha256 = "c" * 64
        lease._clone_temp_name = ".lost-clone"
        target = self.prefix_root / lease._clone_temp_name
        target.mkdir()
        info = target.stat()
        lease._clone_temp_identity = (info.st_dev, info.st_ino)
        lease._clone_temp_fd = os.open(lease._clone_temp_name,
                                       os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                                       | getattr(os, "O_NOFOLLOW", 0), dir_fd=lease._prefix_parent_fd)
        lease._write_active_lease("MATERIALIZING")
        target.rename(self.base / "untracked-moved-clone")
        with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
            lease.__exit__(None, None, None)
        self.assertTrue((self.base / "untracked-moved-clone").is_dir())
        self.assertTrue(lease._lease_marker_path.is_file())

    def test_failed_run_releases_marker_after_durable_cleanup_when_resource_is_absent(self) -> None:
        lease = self._lease("failed-run-cleaned")
        marker_root = self.prefix_root / ".active-leases"
        marker_root.mkdir(exist_ok=True)
        lease._lease_marker_path = marker_root / "test-failed-run-cleaned.json"
        lease._manifest_sha256 = "a" * 64
        lease._storage_violation = True
        lease._write_active_lease("PREPARED")
        with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
            lease.__exit__(SystemExit, SystemExit(0), None)
        self.assertFalse(lease._lease_marker_path.exists())
        receipt = json.loads((lease.output_dir / "prefix-cleanup.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "FAIL")
        self.assertTrue(receipt["receipt_finalization_complete"])
        self.assertTrue(receipt["active_lease_marker_removed_after_receipt"])


class ArtifactResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-artifact-resolution-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / "artifacts/sha256"
        self.store.mkdir(parents=True)
        patcher = mock.patch.object(policy, "ARTIFACT_STORE", self.store)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _artifact(self, payload: bytes = b"verified canonical artifact") -> tuple[str, Path, dict]:
        digest = policy.hashlib.sha256(payload).hexdigest()
        group = self.store / digest
        group.mkdir()
        target = group / "output.dll"
        target.write_bytes(payload)
        manifest = {"schema_version": 2, "sha256": digest,
                    "canonical_path": str(target), "size_bytes": len(payload)}
        (group / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        return digest, target, manifest

    def test_canonical_artifact_resolver_checks_manifest_and_payload_hash(self) -> None:
        digest, target, _manifest = self._artifact()
        with policy.open_canonical_artifact(digest) as stream:
            self.assertEqual(stream.read(), target.read_bytes())

    def test_canonical_artifact_resolver_accepts_legacy_byte_size_manifest(self) -> None:
        digest, target, manifest = self._artifact()
        manifest.pop("size_bytes")
        manifest["schema_version"] = 1
        manifest["byte_size"] = target.stat().st_size
        (target.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with policy.open_canonical_artifact(digest) as stream:
            self.assertEqual(stream.read(), target.read_bytes())

    def test_canonical_artifact_resolver_rejects_conflicting_legacy_sizes(self) -> None:
        digest, target, manifest = self._artifact()
        manifest["byte_size"] = target.stat().st_size + 1
        (target.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(OSError, "size differs from its manifest"):
            with policy.open_canonical_artifact(digest):
                self.fail("conflicting canonical artifact sizes unexpectedly opened")

    def test_canonical_artifact_resolver_rejects_malformed_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "lowercase SHA-256"):
            with policy.open_canonical_artifact("../" + "a" * 64):
                self.fail("malformed digest unexpectedly opened")

    def test_canonical_artifact_resolver_rejects_manifest_path_escape(self) -> None:
        digest, target, manifest = self._artifact()
        manifest["canonical_path"] = str(target.parent / ".." / "outside.dll")
        (target.parent / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(OSError, "outside its exact digest directory"):
            with policy.open_canonical_artifact(digest):
                self.fail("escaped manifest unexpectedly opened")

    def test_canonical_artifact_resolver_rejects_wrong_payload_bytes(self) -> None:
        digest, target, _manifest = self._artifact()
        target.write_bytes(b"X" * len(b"verified canonical artifact"))
        with self.assertRaisesRegex(OSError, "failed SHA-256"):
            with policy.open_canonical_artifact(digest):
                self.fail("mismatched payload unexpectedly opened")

    def test_canonical_artifact_resolver_rejects_hardlinked_payload(self) -> None:
        digest, target, _manifest = self._artifact()
        os.link(target, target.parent / "writable-alias.dll")
        with self.assertRaisesRegex(OSError, "regular file on its hash-store filesystem"):
            with policy.open_canonical_artifact(digest):
                self.fail("mutable hardlinked canonical artifact unexpectedly opened")

    def test_canonical_artifact_resolver_rejects_fifo_manifest_and_payload_without_blocking(self) -> None:
        digest, target, _manifest = self._artifact()
        target.unlink()
        os.mkfifo(target)
        with self.assertRaisesRegex(OSError, "regular file on its hash-store filesystem"):
            with policy.open_canonical_artifact(digest):
                self.fail("FIFO canonical artifact payload unexpectedly opened")
        target.unlink()
        (target.parent / "manifest.json").unlink()
        os.mkfifo(target.parent / "manifest.json")
        with self.assertRaisesRegex(OSError, "bounded regular file"):
            with policy.open_canonical_artifact(digest):
                self.fail("FIFO canonical artifact manifest unexpectedly opened")


class RunnerAdmissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-runner-admission-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.runner = Path(self.temp.name) / "runner.py"

    def test_active_build_overlap_is_allowed_only_when_same_manifest_reserved_it(self) -> None:
        active = [{"build_id": "other", "owner_experiment": "other-run"},
                  {"build_id": "same", "owner_experiment": "this-run"}]
        self.assertEqual(policy._foreign_active_builds(active, "this-run"), [active[0]])
        self.assertEqual(policy._foreign_active_builds(active, None), active)

    def test_prefix_runner_must_use_scoped_lease_and_managed_process_wait(self) -> None:
        self.runner.write_text("def main():\n    subprocess.run(['wine'])\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "scoped_lease.*PrefixLease.wait_process"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_admitted_runner_source_is_sha256_bound(self) -> None:
        source = ("def run_managed():\n"
                  "    process = lease.start_wine_process(command)\n"
                  "    lease.wait_process(process, timeout_seconds=1)\n"
                  "def main():\n"
                  "    with scoped_lease(lease):\n"
                  "        return run_managed()\n")
        self.runner.write_text(source, encoding="utf-8")
        records = policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)
        self.assertEqual(records[str(self.runner)], hashlib.sha256(source.encode()).hexdigest())

    def test_raw_subprocess_run_is_never_admitted(self) -> None:
        self.runner.write_text(
            "import subprocess\nfrom pathlib import Path\nRUNTIME = Path('/wine')\n"
            "def run_managed():\n"
            "    process = lease.start_wine_process(command)\n"
            "    lease.wait_process(process, timeout_seconds=1)\n"
            "    subprocess.run(['/usr/bin/arch', '-x86_64', str(RUNTIME / 'bin/wine'), '--version'], "
            "env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=15, check=False)\n"
            "def main():\n"
            "    with scoped_lease(lease):\n"
            "        return run_managed()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_version_probe_shape_cannot_whitelist_shell_execution(self) -> None:
        self.runner.write_text(
            "import subprocess\n"
            "def run_managed():\n"
            "    process = lease.start_wine_process(command)\n"
            "    lease.wait_process(process, timeout_seconds=1)\n"
            "    subprocess.run(['/bin/sh', '-c', 'touch /tmp/ast-bypass', "
            "str('/fake/bin/wine'), '--version'], timeout=1)\n"
            "def main():\n"
            "    with scoped_lease(lease):\n"
            "        return run_managed()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_assignment_alias_cannot_hide_raw_popen(self) -> None:
        self.runner.write_text(
            "import subprocess\nsp = subprocess\npopen = sp.Popen\n"
            "def run_managed():\n"
            "    process = lease.start_wine_process(command)\n"
            "    lease.wait_process(process, timeout_seconds=1)\n"
            "    popen(['wine'])\n"
            "def main():\n"
            "    with scoped_lease(lease):\n"
            "        return run_managed()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_shadowing_assignment_cannot_erase_global_subprocess_alias(self) -> None:
        self.runner.write_text(
            "import subprocess as sp\n"
            "def owned():\n"
            "    sp.Popen(['/usr/bin/touch', '/tmp/alias-bypass'])\n"
            "    process = lease.start_wine_process(command)\n"
            "    lease.wait_process(process, timeout_seconds=1)\n"
            "def shadow():\n"
            "    sp = None\n"
            "def main():\n"
            "    with scoped_lease(lease):\n"
            "        return owned()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_shell_helpers_and_raw_fork_are_rejected(self) -> None:
        for raw_call in ("subprocess.getoutput('touch /tmp/admission-bypass')", "os.fork()"):
            with self.subTest(raw_call=raw_call):
                self.runner.write_text(
                    "import os\nimport subprocess\n"
                    "def run_managed():\n"
                    "    " + raw_call + "\n"
                    "    process = lease.start_wine_process(command)\n"
                    "    lease.wait_process(process, timeout_seconds=1)\n"
                    "def main():\n"
                    "    with scoped_lease(lease):\n"
                    "        return run_managed()\n",
                    encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
                    policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_dynamic_process_api_lookup_is_rejected(self) -> None:
        self.runner.write_text(
            "import subprocess\n"
            "def run_managed():\n"
            "    process = lease.start_wine_process(command)\n"
            "    lease.wait_process(process, timeout_seconds=1)\n"
            "    getattr(subprocess, 'Popen')(['/usr/bin/touch', '/tmp/dynamic-bypass'])\n"
            "def main():\n"
            "    with scoped_lease(lease):\n"
            "        return run_managed()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_subprocess_module_dictionary_alias_is_rejected(self) -> None:
        self.runner.write_text(
            "import subprocess\n"
            "raw = subprocess.__dict__['Popen']\n"
            "def run_managed():\n"
            "    process = lease.start_wine_process(command)\n"
            "    lease.wait_process(process, timeout_seconds=1)\n"
            "    raw(['/usr/bin/touch', '/tmp/dynamic-bypass'])\n"
            "def main():\n"
            "    with scoped_lease(lease):\n"
            "        return run_managed()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_process_api_higher_order_and_dynamic_builtin_aliases_are_rejected(self) -> None:
        cases = {
            "higher_order_constructor": (
                "import subprocess\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return (lambda spawn: spawn(['/usr/bin/touch', '/tmp/higher-order']))(subprocess.Popen)\n"),
            "assigned_getattr": (
                "import subprocess\n"
                "lookup = getattr\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    raw = lookup(subprocess, 'Popen')\n"
                "    return raw(['/usr/bin/touch', '/tmp/dynamic-alias'])\n"),
            "sys_module_dictionary": (
                "import sys\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    module = sys.__dict__['modules']['subprocess']\n"
                "    return module.__dict__['Popen'](['/usr/bin/touch', '/tmp/sys-dict'])\n"),
            "raw_hardlink": (
                "import os\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    os.link('/tmp/canonical', '/tmp/alias')\n"),
            "imported_sys_frame_api": (
                "from sys import _getframe as frame\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return frame()\n"),
            "process_module_imported_from_helper": (
                "from storage_policy import subprocess as proc\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return proc.run(['/usr/bin/touch', '/tmp/helper-process-module'])\n"),
            "process_module_read_from_helper_module": (
                "import storage_policy as policy\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return policy.subprocess.run(['/usr/bin/touch', '/tmp/helper-module-process'])\n"),
            "helper_reexported_sys_modules": (
                "from storage_policy import sys as system\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return system.modules['subprocess'].Popen(['/usr/bin/touch', '/tmp/helper-sys'])\n"),
            "helper_reexported_ctypes": (
                "from storage_policy import ctypes as native\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return native.CDLL(None).system(b'touch /tmp/helper-ctypes')\n"),
            "helper_module_ctypes_attribute": (
                "import storage_policy as policy\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return policy.ctypes.CDLL(None).system(b'touch /tmp/helper-ctypes-attr')\n"),
            "helper_module_dict_subprocess": (
                "import storage_policy as policy\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    raw = policy.__dict__['subprocess'].Popen\n"
                "    return raw(['/usr/bin/touch', '/tmp/helper-dict'])\n"),
            "helper_module_getattribute_subprocess": (
                "import build_retention as retention\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    raw = retention.__getattribute__('subprocess').Popen\n"
                "    return raw(['/usr/bin/touch', '/tmp/helper-getattribute'])\n"),
            "object_getattribute_subprocess": (
                "import storage_policy as policy\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    raw = object.__getattribute__(policy, 'subprocess').Popen\n"
                "    return raw(['/usr/bin/touch', '/tmp/object-getattribute'])\n"),
            "operator_attrgetter_subprocess": (
                "import storage_policy as policy\n"
                "from operator import attrgetter\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    raw = attrgetter('__dict__')(policy)['subprocess'].Popen\n"
                "    return raw(['/usr/bin/touch', '/tmp/operator-attrgetter'])\n"),
            "inspect_static_subprocess": (
                "import storage_policy as policy\n"
                "from inspect import getattr_static as lookup\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    raw = lookup(policy, 'subprocess').Popen\n"
                "    return raw(['/usr/bin/touch', '/tmp/inspect-static'])\n"),
            "imported_module_dictionary": (
                "from storage_policy import __dict__ as module_globals\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return module_globals['subprocess'].Popen(['/usr/bin/touch', '/tmp/imported-dict'])\n"),
            "private_helper_process_launcher": (
                "import storage_policy as policy\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    fd = policy.os.open('/tmp', policy.os.O_RDONLY)\n"
                "    return policy._spawn_in_directory_fd(fd, ['/usr/bin/touch', '/tmp/private-launcher'])\n"),
            "imported_private_helper_process_launcher": (
                "from storage_policy import _spawn_in_directory_fd as spawn\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return spawn(fd, ['/usr/bin/touch', '/tmp/imported-private-launcher'])\n"),
            "posix_spawn": (
                "import posix\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return posix.posix_spawn('/usr/bin/touch', ['touch', '/tmp/posix-spawn'], os.environ)\n"),
            "concurrent_process_pool": (
                "from concurrent.futures import ProcessPoolExecutor\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    pool = ProcessPoolExecutor()\n"
                "    return pool.submit(write_marker)\n"),
            "venv_process_wrapper": (
                "import venv\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return venv.EnvBuilder(with_pip=True).create('/private/tmp/unmanaged-venv')\n"),
            "stdlib_reexported_os_process_api": (
                "import shutil\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return shutil.os.system('touch /private/tmp/unmanaged')\n"),
            "pathlib_reexported_os_process_api": (
                "import pathlib\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return pathlib.os.system('touch /private/tmp/unmanaged')\n"),
            "builtin_subclasses_process_reflection": (
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    popen = next(cls for cls in object.__subclasses__()\n"
                "                 if cls.__name__ == 'Popen' and cls.__module__ == 'subprocess')\n"
                "    return popen(['/usr/bin/touch', '/private/tmp/unmanaged'])\n"),
            "relative_package_import": (
                "from . import payload\n"
                "def run_managed():\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return payload.launch_unmanaged_child()\n"),
            "private_lease_state_mutation": (
                "def run_managed():\n"
                "    lease._sealed_wine_env = {'DYLD_INSERT_LIBRARIES': '/tmp/inject.dylib'}\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return process\n"),
            "mutable_wineserver_path": (
                "def run_managed():\n"
                "    lease.wine_server = '/bin/sh'\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return process\n"),
            "mutable_template_path": (
                "def run_managed():\n"
                "    lease.template = '/tmp/oversized-template'\n"
                "    process = lease.start_wine_process(command)\n"
                "    lease.wait_process(process, timeout_seconds=1)\n"
                "    return process\n"),
        }
        for name, helper in cases.items():
            with self.subTest(name=name):
                self.runner.write_text(
                    helper +
                    "def main():\n"
                    "    with scoped_lease(lease):\n"
                    "        return run_managed()\n",
                    encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
                    policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_aliased_builtin_dynamic_lookup_is_rejected(self) -> None:
        self.runner.write_text(
            "import subprocess\nfrom builtins import getattr as lookup\n"
            "def run_managed():\n"
            "    process = lease.start_wine_process(command)\n"
            "    lease.wait_process(process, timeout_seconds=1)\n"
            "    lookup(subprocess, 'Popen')(['/usr/bin/touch', '/tmp/dynamic-bypass'])\n"
            "def main():\n"
            "    with scoped_lease(lease):\n"
            "        return run_managed()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_imported_process_api_method_alias_is_rejected(self) -> None:
        self.runner.write_text(
            "from subprocess import Popen as Proc\n"
            "def run_managed():\n"
            "    process = lease.start_wine_process(command)\n"
            "    lease.wait_process(process, timeout_seconds=1)\n"
            "    Proc.__new__(Proc)\n"
            "def main():\n"
            "    with scoped_lease(lease):\n"
            "        return run_managed()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "without unbounded/raw process creation"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_dead_code_does_not_authorize_an_unmanaged_main(self) -> None:
        self.runner.write_text(
            "def unused():\n"
            "    with scoped_lease(lease):\n"
            "        lease.wait_process(process, timeout_seconds=1)\n"
            "def main():\n"
            "    process = subprocess.Popen(['wine', 'app.exe'])\n"
            "    return process.wait()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "process-owning helper"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)

    def test_managed_runner_rejects_arbitrary_blocking_subprocess_run(self) -> None:
        self.runner.write_text(
            "def run_managed():\n"
            "    return subprocess.run(['wine', 'app.exe'])\n"
            "    lease.start_wine_process(command)\n"
            "    lease.wait_process(process, timeout_seconds=1)\n"
            "def main():\n"
            "    with scoped_lease(lease):\n"
            "        return run_managed()\n",
            encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "unbounded/raw process creation"):
            policy._authorized_runner_records([str(self.runner)], requires_managed_prefix=True)


class Gate0SmokeEvidenceTests(unittest.TestCase):
    def _preflight(self, path: Path, smoke_id: str, key_path: Path) -> Path:
        storage_root = path.parent
        smoke_root = storage_root / "smoke-runs"
        manifest_root = storage_root / "manifests"
        claim_root = storage_root / "smoke-claims"
        repo_root = storage_root.parent.parent
        os.chown(repo_root.parent, os.getuid(), os.getgid())
        os.chown(repo_root, os.getuid(), os.getgid())
        prefix_root = repo_root / "prefixes"
        active_root = prefix_root / ".active-leases"
        for directory in (storage_root, smoke_root, manifest_root, claim_root,
                          prefix_root, active_root):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chown(directory, os.getuid(), os.getgid())
            if directory in (smoke_root, claim_root, prefix_root):
                directory.chmod(0o700)
        output_dir = smoke_root / smoke_id
        output_dir.mkdir(mode=0o700, exist_ok=True)
        os.chown(output_dir, os.getuid(), os.getgid())
        output_dir.chmod(0o700)
        with mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path):
            policy._manifest_signing_key(create=True)
            value = {
                "schema_version": 1, "status": "PREFLIGHT_PASS", "mode": "preflight",
                "created_at_unix": time.time(), "prefixlease_smoke_status": "PENDING",
                "permitted_smoke_experiment_id": smoke_id,
            }
            value["gate0_hmac_sha256"] = policy._gate0_signature(value)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)
        return active_root

    def test_fabricated_smoke_json_is_rejected_without_authenticated_run_receipt(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-gate0-smoke-test-", dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name) / "repo"
        repo.mkdir()
        suite_file = Path(gate0.HERE) / "test_storage_policy.py"
        test_names = {
            "test_normal_cleanup_removes_prefix",
            "test_original_failure_still_runs_and_finishes_cleanup",
            "test_system_exit_zero_cannot_hide_cleanup_failure",
            "test_uncertified_prefix_is_rejected",
            "test_preservation_stays_within_configured_cap",
            "test_full_preserved_cap_leaves_uncertified_prefix_for_gate_block",
        }
        smoke_id = (f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}{time.strftime('%Y%m%d', time.gmtime())}-"
                    f"{uuid.uuid4().hex[:16]}")
        report_path = repo / "experiments/storage/smoke-runs" / smoke_id / "prefix-smoke-report.json"
        report_path.parent.mkdir(parents=True)
        report_path.write_text(json.dumps({
            "experiment_id": smoke_id,
            "status": "PASS", "graphics_experiments_run": False, "wine_app_launched": False,
            "prefix_created": True, "prefix_deleted": True,
            "storage_hygiene_status": "PASS", "free_space_status": "PASS",
            "system_exit_zero_cleanup_precedence": True, "failure_cleanup_test": "PASS",
            "uncertified_prefix_rejection": "PASS", "preserved_prefix_policy": "PASS",
            "synthetic_policy_suite": {"status": "PASS", "sha256": gate0._sha(suite_file.read_bytes()),
                                        "suite_output_sha256": "a" * 64, "tests": sorted(test_names)},
        }), encoding="utf-8")
        attestation_path = repo / "experiments/storage/storage-gate0-attestation.json"
        attestation_path.parent.mkdir(parents=True, exist_ok=True)
        key_path = Path(temp.name) / "signing-key"
        self._preflight(attestation_path, smoke_id, key_path)
        with mock.patch.object(gate0, "REPO", repo), mock.patch.object(gate0, "ATTESTATION", attestation_path), \
                mock.patch.object(policy, "REPO", repo), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
                mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path):
            with self.assertRaisesRegex(RuntimeError, "signature is missing or invalid"):
                gate0._test_smoke_report(report_path)

    def test_old_smoke_run_cannot_replay_after_new_preflight(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-gate0-stale-smoke-test-", dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name) / "repo"
        repo.mkdir()
        suffix = uuid.uuid4().hex[:16]
        old_id = f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}{time.strftime('%Y%m%d', time.gmtime())}-{suffix}"
        new_id = f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}{time.strftime('%Y%m%d', time.gmtime())}-{uuid.uuid4().hex[:16]}"
        old_report = repo / "experiments/storage/smoke-runs" / old_id / "prefix-smoke-report.json"
        old_report.parent.mkdir(parents=True)
        old_report.write_text("{}\n", encoding="utf-8")
        attestation_path = repo / "experiments/storage/storage-gate0-attestation.json"
        key_path = Path(temp.name) / "signing-key"
        self._preflight(attestation_path, new_id, key_path)
        with mock.patch.object(gate0, "REPO", repo), mock.patch.object(gate0, "ATTESTATION", attestation_path), \
                mock.patch.object(policy, "REPO", repo), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
                mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path):
            with self.assertRaisesRegex(RuntimeError, "one fixed canonical report path"):
                gate0._test_smoke_report(old_report)

    def test_pending_smoke_nonce_is_consumed_durably_once(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-gate0-smoke-claim-test-", dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name) / "repo"
        repo.mkdir()
        key_path = Path(temp.name) / "signing-key"
        attestation_path = repo / "experiments/storage/storage-gate0-attestation.json"
        smoke_id = (f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}{time.strftime('%Y%m%d', time.gmtime())}-"
                    f"{uuid.uuid4().hex[:16]}")
        claims = repo / "experiments/storage/smoke-claims"
        active_root = self._preflight(attestation_path, smoke_id, key_path)
        with mock.patch.object(policy, "REPO", repo), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
                mock.patch.object(policy, "ACTIVE_LEASE_ROOT", active_root), \
                mock.patch.object(policy, "SMOKE_CLAIM_ROOT", claims), \
                mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path), \
                mock.patch.object(policy, "_storage_smoke_parent_allocation_growth", return_value=0):
            raw = policy._read_file_nofollow(attestation_path)
            self.assertEqual(policy._current_preflight_smoke_id(), smoke_id)
            claim, claim_bytes = policy._claim_storage_smoke_nonce(smoke_id, raw)
            self.assertTrue(policy._verify_storage_evidence(claim, "smoke_claim_hmac_sha256"))
            self.assertEqual(policy._current_preflight_smoke_id(), None)
            # Removing conventional run artifacts cannot reauthorize the consumed ID.
            with self.assertRaises(FileExistsError):
                policy._claim_storage_smoke_nonce(smoke_id, raw)
            readback, readback_bytes = policy._read_storage_smoke_claim(smoke_id)
            self.assertEqual(readback, claim)
            self.assertEqual(readback_bytes, claim_bytes)

    def test_storage_smoke_claim_rejects_noncanonical_trailing_bytes(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-gate0-smoke-claim-canonical-", dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name) / "repo"
        repo.mkdir()
        key_path = Path(temp.name) / "signing-key"
        attestation_path = repo / "experiments/storage/storage-gate0-attestation.json"
        smoke_id = (f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}{time.strftime('%Y%m%d', time.gmtime())}-"
                    f"{uuid.uuid4().hex[:16]}")
        claims = repo / "experiments/storage/smoke-claims"
        active_root = self._preflight(attestation_path, smoke_id, key_path)
        with mock.patch.object(policy, "REPO", repo), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
                mock.patch.object(policy, "ACTIVE_LEASE_ROOT", active_root), \
                mock.patch.object(policy, "SMOKE_CLAIM_ROOT", claims), \
                mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path), \
                mock.patch.object(policy, "_storage_smoke_parent_allocation_growth", return_value=0):
            raw = policy._read_file_nofollow(attestation_path)
            _, claim_bytes = policy._claim_storage_smoke_nonce(smoke_id, raw)
            claim_path = policy._storage_smoke_claim_path(smoke_id)
            claim_path.write_bytes(claim_bytes + b" ")
            with self.assertRaisesRegex(ValueError, "missing, malformed, or unauthenticated"):
                policy._read_storage_smoke_claim(smoke_id)

    def test_reconciliation_record_short_write_does_not_leave_partial_final(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-reconciliation-atomic-test-", dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        key_path = base / "signing-key"
        target = base / "reconciliations" / "synthetic.json"
        with mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path):
            policy._manifest_signing_key(create=True)
            real_write = os.write
            calls = 0

            def fail_after_partial_write(fd: int, data) -> int:
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_write(fd, bytes(data[:16]))
                raise OSError("synthetic short-write failure")

            with mock.patch.object(policy.os, "write", side_effect=fail_after_partial_write):
                with self.assertRaisesRegex(OSError, "synthetic short-write failure"):
                    policy._write_reconciliation_record(target, {"state": "PREPARED"}, create=True)
            self.assertFalse(target.exists())
            self.assertEqual(list(target.parent.iterdir()), [])
            raw = policy._write_reconciliation_record(target, {"state": "PREPARED"}, create=True)
            value = json.loads(raw)
            self.assertTrue(policy._verify_storage_evidence(value, "reconciliation_hmac_sha256"))

    def test_stale_smoke_marker_reconciliation_requires_signed_absence_proof(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-prefix-reconcile-test-", dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        repo = base / "repo"
        repo.mkdir()
        key_path = base / "signing-key"
        attestation_path = repo / "experiments/storage/storage-gate0-attestation.json"
        smoke_id = (f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}{time.strftime('%Y%m%d', time.gmtime())}-"
                    f"{uuid.uuid4().hex[:16]}")
        output = repo / "experiments/storage/smoke-runs" / smoke_id
        output.mkdir(parents=True, mode=0o700)
        output.chmod(0o700)
        manifest_path = repo / "experiments/storage/manifests" / f"{smoke_id}.json"
        preflight_path = output / "gate0-preflight-attestation.json"
        active_root = self._preflight(attestation_path, smoke_id, key_path)
        prefix_root = active_root.parent
        preflight_bytes = attestation_path.read_bytes()
        preflight_path.write_bytes(preflight_bytes)
        preflight_path.chmod(0o600)
        with mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path):
            manifest = {
                "experiment_id": smoke_id, "storage_policy_smoke": True, "decision": "ALLOW",
                "blockers": [],
                "manifest_path": str(manifest_path), "output_dir": str(output),
                "estimated_persistent_bytes": policy.STORAGE_SMOKE_OUTPUT_RESERVATION_BYTES,
                "gate0_attestation_sha256": policy.hashlib.sha256(preflight_bytes).hexdigest(),
            }
            manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
            manifest_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            manifest_path.chmod(0o600)
            expected_prefix = prefix_root / f"storage-smoke-{smoke_id}"
            cleanup_path = output / "prefix-cleanup.json"
            cleanup = policy._seal_storage_evidence({
                "experiment_id": smoke_id, "prefix_path": str(expected_prefix),
                "status": "FAIL", "prefix_created": False, "prefix_deleted": False,
                "prefix_preserved": False, "cleanup_error": None, "receipt_write_error": None,
                "receipt_finalization_complete": True, "active_lease_marker_removed_after_receipt": False,
                "created_at_unix": time.time(),
            }, "cleanup_receipt_hmac_sha256")
            cleanup_path.write_text(json.dumps(cleanup), encoding="utf-8")
            cleanup_path.chmod(0o600)
            marker = {
                "experiment_id": smoke_id, "pid": 99999999, "state": "RECONCILIATION_REQUIRED",
                "prefix_path": str(expected_prefix), "output_dir": str(output),
                "manifest_path": str(manifest_path),
                "manifest_sha256": policy.hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            }
            marker_path = active_root / f"{smoke_id}.json"
            marker_path.write_text(json.dumps(marker), encoding="utf-8")
            marker_path.chmod(0o600)

        with mock.patch.object(policy, "REPO", repo), \
                mock.patch.object(policy, "PREFIX_ROOT", prefix_root), \
                mock.patch.object(policy, "ACTIVE_LEASE_ROOT", active_root), \
                mock.patch.object(policy, "PREFIX_RECONCILIATION_ROOT", repo / "experiments/storage/prefix-lease-reconciliation"), \
                mock.patch.object(policy, "SMOKE_CLAIM_ROOT", repo / "experiments/storage/smoke-claims"), \
                mock.patch.object(policy, "ACTIVE_LEASE_ROOT", active_root), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
                mock.patch.object(policy, "_storage_smoke_parent_allocation_growth", return_value=0), \
                mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path), \
                mock.patch.object(policy, "_acquire_prefix_reconciliation_locks",
                                  side_effect=lambda: (policy._open_directory_nofollow(active_root), None, None)), \
                mock.patch.object(policy, "_release_prefix_reconciliation_locks", return_value=None), \
                mock.patch.object(policy.os, "kill", side_effect=ProcessLookupError):
            actions = policy.reconcile_abandoned_storage_smoke_lease_markers()
            self.assertEqual(len(actions), 1)
            self.assertFalse(marker_path.exists())
            self.assertFalse(expected_prefix.exists())
            self.assertEqual(json.loads(cleanup_path.read_text(encoding="utf-8"))["status"], "FAIL")
            reconciliation_path = repo / "experiments/storage/prefix-lease-reconciliation" / f"{smoke_id}.json"
            reconciliation = json.loads(reconciliation_path.read_text(encoding="utf-8"))
            self.assertEqual(reconciliation["state"], "COMPLETE")
            self.assertTrue(policy._verify_storage_evidence(reconciliation, "reconciliation_hmac_sha256"))
            self.assertTrue(policy._read_storage_smoke_claim(smoke_id)[0]["claim_reason"].startswith("failed smoke"))

    def test_final_gate_rechecks_attested_smoke_files(self) -> None:
        temp = tempfile.TemporaryDirectory(prefix="fgmetal-gate0-smoke-retention-test-", dir="/private/tmp")
        self.addCleanup(temp.cleanup)
        repo = Path(temp.name) / "repo"
        repo.mkdir()
        smoke_id = f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}{time.strftime('%Y%m%d', time.gmtime())}-{uuid.uuid4().hex[:16]}"
        output = repo / "experiments/storage/smoke-runs" / smoke_id
        with mock.patch.object(policy, "REPO", repo):
            result = policy._attested_smoke_evidence_blockers({
                "prefixlease_smoke": {
                    "experiment_id": smoke_id,
                    "report_path": str(output / "prefix-smoke-report.json"),
                    "report_sha256": "0" * 64,
                },
            })

class NativeRunLeasePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-native-lease-test-", dir="/private/tmp")
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True)
        self.active_root = self.base / "active_leases"
        self.active_root.mkdir(parents=True)
        self.manifest_root = self.repo / "experiments/storage/manifests"
        self.manifest_root.mkdir(parents=True)
        self.key_path = self.base / "signing.key"
        self.key_path.write_bytes(b"K" * 32)
        self.key_path.chmod(0o600)
        self.cache_dir = self.base / "cache"
        self.cache_dir.mkdir(parents=True)
        self.vm_capacity_root = self.cache_dir / "vm-capacity-domain"
        self.vm_capacity_root.mkdir(mode=0o700)
        self.vm_capacity_marker = self.vm_capacity_root / "active-session.json"
        self.gate_attestation = self.base / "gate0-attestation.json"
        self.gate_attestation.write_text(json.dumps({
            "status": "PASS",
            "canonical_artifact_store_audit": {"sha256": "audit"}
        }), encoding="utf-8")
        self.output_dir = self.base / "output"
        self.output_dir.mkdir(parents=True)
        self.bin_dir = self.base / "bin"
        self.bin_dir.mkdir(parents=True)
        self.lib_dir = self.base / "lib"
        self.lib_dir.mkdir(parents=True)

        self.executable_path, self.executable_sha = self._make_macho(
            self.bin_dir / "native_control", filetype=2)
        self.dylib_path, self.dylib_sha = self._make_macho(
            self.lib_dir / "libMoltenVK.dylib", filetype=6)

        self.addCleanup(self.temp.cleanup)
        patchers = [
            mock.patch.object(policy, "REPO", self.repo),
            mock.patch.object(policy, "CACHE", self.cache_dir),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ROOT", self.vm_capacity_root),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ACTIVE_MARKER", self.vm_capacity_marker),
            mock.patch.object(policy, "ACTIVE_LEASE_ROOT", self.active_root),
            mock.patch.object(policy, "GATE0_ATTESTATION_PATH", self.gate_attestation),
            mock.patch.object(policy, "INVENTORY_PATH", self.base / "missing-inventory.json"),
            mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", self.key_path),
            mock.patch.object(policy, "disk_free_bytes", return_value=100 * policy.GIB),
            mock.patch.object(policy, "measure_project_usage",
                              return_value={"allocated_inode_deduplicated_bytes": 0}),
            mock.patch.object(policy, "_gate0_blockers", return_value=[]),
        ]
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _make_macho(self, path: Path, architecture: str = "arm64", filetype: int = 2,
                    payload: bytes = b"code") -> tuple[Path, str]:
        path.parent.mkdir(parents=True, exist_ok=True)
        cputype = {"arm64": 0x0100000c, "x86_64": 0x01000007}[architecture]
        header = struct.pack("<4I", 0xfeedfacf, cputype, 0, filetype)
        content = header + payload + b"\x00" * 32
        path.write_bytes(content)
        path.chmod(0o755)
        digest = hashlib.sha256(content).hexdigest()
        return path, digest

    def _make_manifest(self, exp_id: str, output_dir: Path, expected_prefix_bytes: int = 0,
                       native_architecture: str = "arm64",
                       estimated_persistent_bytes: int = 1024) -> Path:
        attestation_bytes = self.gate_attestation.read_bytes()
        attestation_sha = hashlib.sha256(attestation_bytes).hexdigest()
        manifest_path = self.manifest_root / f"{exp_id}.json"
        declared_peak = expected_prefix_bytes + estimated_persistent_bytes
        uncertainty_margin = policy.admission_uncertainty_margin_bytes(declared_peak)
        manifest = {
            "experiment_id": exp_id,
            "decision": "ALLOW",
            "expected_duration_seconds": 60,
            "expected_prefix_requirement": expected_prefix_bytes > 0,
            "expected_prefix_bytes": expected_prefix_bytes,
            "expected_build_requirement": False,
            "expected_build_bytes": 0,
            "estimated_persistent_bytes": estimated_persistent_bytes,
            "estimated_max_new_disk_bytes": declared_peak,
            "estimated_uncertainty_margin_bytes": uncertainty_margin,
            "estimated_projected_new_disk_bytes": declared_peak + uncertainty_margin,
            "storage_justification": "synthetic bounded evidence estimate",
            "project_budget_bytes": policy.PROJECT_BUDGET,
            "native_architecture": native_architecture,
            "manifest_path": str(manifest_path),
            "output_dir": str(output_dir),
            "gate0_attestation_sha256": attestation_sha,
        }
        with mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", self.key_path):
            manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path

    def _create_lease(self, exp_id: str, **kwargs: Any) -> policy.NativeRunLease:
        manifest_path = kwargs.pop("manifest_path", None)
        manifest_architecture = kwargs.pop("manifest_architecture", "arm64")
        if manifest_path is None:
            manifest_path = self._make_manifest(exp_id, self.output_dir,
                                                native_architecture=manifest_architecture)
        defaults: dict[str, Any] = {
            "experiment_id": exp_id,
            "executable_path": self.executable_path,
            "output_dir": self.output_dir,
            "candidate_dylibs": {"moltenvk": self.dylib_path},
            "expected_executable_sha256": self.executable_sha,
            "expected_dylib_hashes": {"moltenvk": self.dylib_sha},
            "manifest_path": manifest_path,
            "timeout_seconds": 10.0,
        }
        defaults.update(kwargs)
        return policy.NativeRunLease(**defaults)

    def test_native_lease_admits_54_gib_scope_with_60_gib_projected_free(self) -> None:
        manifest_path = self._make_manifest(
            "test-native-54gib", self.output_dir, estimated_persistent_bytes=0)
        lease = self._create_lease("test-native-54gib", manifest_path=manifest_path)
        margin = policy.admission_uncertainty_margin_bytes(0)
        with mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB + margin), \
                mock.patch.object(policy, "measure_project_usage", return_value={
                    "allocated_inode_deduplicated_bytes": 54 * policy.GIB,
                }):
            with lease:
                self.assertTrue(lease.scratch_directory.is_dir())
                self.assertTrue(any(
                    event.get("action") == "PROJECTED_STORAGE_WARNING_BEFORE_NATIVE_LEASE"
                    and "52 GiB" in event.get("warning", "")
                    for event in lease._storage_watch_events))

    def test_native_lease_rejects_projected_peak_above_60_gib(self) -> None:
        manifest_path = self._make_manifest(
            "test-native-over-60gib", self.output_dir,
            estimated_persistent_bytes=6 * policy.GIB + 1)
        lease = self._create_lease("test-native-over-60gib", manifest_path=manifest_path)
        with mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB), \
                mock.patch.object(policy, "measure_project_usage", return_value={
                    "allocated_inode_deduplicated_bytes": 54 * policy.GIB,
                }):
            with self.assertRaisesRegex(RuntimeError, "projected scoped FG-Metal usage exceeds the 60 GiB ceiling"):
                with lease:
                    pass

    def test_native_lease_rejects_projected_free_below_50_with_scope_inside_limit(self) -> None:
        manifest_path = self._make_manifest(
            "test-native-low-projected-free", self.output_dir, estimated_persistent_bytes=2)
        lease = self._create_lease("test-native-low-projected-free", manifest_path=manifest_path)
        with mock.patch.object(policy, "disk_free_bytes", return_value=50 * policy.GIB + 1), \
                mock.patch.object(policy, "measure_project_usage", return_value={
                    "allocated_inode_deduplicated_bytes": 54 * policy.GIB,
                }):
            with self.assertRaisesRegex(RuntimeError, "projected free disk is below the 50 GiB normal minimum"):
                with lease:
                    pass

    def test_native_lease_normal_exit_and_receipt_verification(self) -> None:
        lease = self._create_lease("test-native-normal")
        proc = mock.Mock()
        proc.poll.side_effect = [None, 0]
        proc.args = [str(self.executable_path)]
        with mock.patch.object(policy.subprocess, "Popen", return_value=proc) as popen:
            with lease:
                scratch = lease.scratch_directory
                self.assertTrue(scratch.is_dir())
                self.assertEqual(scratch.parent, self.output_dir)
                lease.set_env({"PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
                               "HOME": str(scratch), "TMPDIR": str(scratch),
                               "LANG": "en_US.UTF-8"})
                child = lease.start_native_process([str(self.executable_path)])
                self.assertIs(child, proc)
                rc = lease.wait_process(child)
                self.assertEqual(rc, 0)
                self.assertEqual(popen.call_args.kwargs["cwd"], str(scratch))
                self.assertEqual(popen.call_args.kwargs["env"]["HOME"], str(scratch))
                self.assertEqual(popen.call_args.kwargs["env"]["TMPDIR"], str(scratch))
        receipt_path = self.output_dir / "native-run-cleanup.json"
        self.assertTrue(receipt_path.exists())
        self.assertEqual(len(receipt_path.read_bytes()), 64 * 1024)
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "PASS")
        self.assertEqual(receipt["storage_hygiene_status"], "PASS")
        self.assertEqual(receipt["free_space_status"], "PASS")
        self.assertTrue(receipt["temporary_removed"])
        self.assertFalse(scratch.exists())
        with self.assertRaisesRegex(RuntimeError, "scratch directory is not active"):
            _ = lease.scratch_directory
        self.assertTrue(policy._verify_storage_evidence(receipt, "cleanup_receipt_hmac_sha256"))
        self.assertFalse((self.active_root / "test-native-normal.json").exists())

    def test_native_lease_nonzero_exit(self) -> None:
        lease = self._create_lease("test-native-nonzero")
        proc = mock.Mock()
        proc.poll.side_effect = [None, 42]
        proc.args = [str(self.executable_path)]
        with mock.patch.object(policy.subprocess, "Popen", return_value=proc):
            with lease:
                child = lease.start_native_process([str(self.executable_path)])
                rc = lease.wait_process(child)
                self.assertEqual(rc, 42)
        receipt_path = self.output_dir / "native-run-cleanup.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "PASS")
        self.assertFalse((self.active_root / "test-native-nonzero.json").exists())

    def test_native_lease_supervisor_exception(self) -> None:
        lease = self._create_lease("test-native-exc")
        with self.assertRaisesRegex(RuntimeError, "crash in supervisor"):
            with lease:
                raise RuntimeError("crash in supervisor")
        receipt_path = self.output_dir / "native-run-cleanup.json"
        self.assertTrue(receipt_path.exists())
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertIn("crash in supervisor", receipt.get("exception", ""))
        self.assertEqual(receipt["status"], "PASS")
        self.assertFalse((self.active_root / "test-native-exc.json").exists())

    def test_native_lease_process_timeout_handling(self) -> None:
        lease = self._create_lease("test-native-timeout", timeout_seconds=0.05)
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.args = [str(self.executable_path)]
        proc.terminate = mock.Mock()
        proc.kill = mock.Mock()
        def mock_wait(timeout: float = 0.0) -> int:
            proc.poll.return_value = -15
            return -15
        proc.wait = mock.Mock(side_effect=mock_wait)
        with mock.patch.object(policy.subprocess, "Popen", return_value=proc):
            with self.assertRaises(subprocess.TimeoutExpired):
                with lease:
                    child = lease.start_native_process([str(self.executable_path)])
                    lease.wait_process(child, timeout_seconds=0.01, poll_interval_seconds=0.005)
            proc.terminate.assert_called()
        receipt_path = self.output_dir / "native-run-cleanup.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "PASS")

    def test_native_lease_sigterm_handling(self) -> None:
        lease = self._create_lease("test-native-sigterm")
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.terminate = mock.Mock()
        proc.wait = mock.Mock(return_value=0)
        with mock.patch.object(policy.subprocess, "Popen", return_value=proc):
            with self.assertRaises(KeyboardInterrupt):
                with lease:
                    child = lease.start_native_process([str(self.executable_path)])
                    lease._handle_sigterm(signal.SIGTERM, None)
            proc.terminate.assert_called()

    def test_native_lease_rejects_path_substitution(self) -> None:
        lease = self._create_lease("test-native-subst")
        other_bin = self.bin_dir / "other_bin"
        self._make_macho(other_bin, "arm64")
        with lease:
            with self.assertRaisesRegex(ValueError, "executable path substitution detected"):
                lease.start_native_process([str(other_bin)])

    def test_native_lease_rejects_non_macho_or_wrong_arch(self) -> None:
        bad_bin = self.bin_dir / "bad_bin"
        bad_bin.write_bytes(b"not a macho binary at all\n")
        bad_bin.chmod(0o755)
        lease = self._create_lease("test-native-bad-macho", executable_path=bad_bin, expected_executable_sha256=None)
        with self.assertRaisesRegex(ValueError, "not a valid thin 64-bit Mach-O binary"):
            with lease:
                pass

        x86_bin = self.bin_dir / "x86_bin"
        x86_bin.write_bytes(struct.pack("<4I", 0xfeedfacf, 0x01000007, 0, 2) + b"\x00" * 32)
        x86_bin.chmod(0o755)
        lease_x86 = self._create_lease("test-native-x86", executable_path=x86_bin, expected_executable_sha256=None)
        with self.assertRaisesRegex(ValueError, "architecture mismatch: declared arm64, actual x86_64"):
            with lease_x86:
                pass

    def test_native_lease_accepts_expected_x86_64_architecture(self) -> None:
        x86_executable, x86_hash = self._make_macho(self.bin_dir / "native_x86", "x86_64")
        x86_lib_dir = self.base / "lib_x86"
        x86_dylib, dylib_hash = self._make_macho(x86_lib_dir / "libMoltenVK.dylib", "x86_64", filetype=6)
        lease = self._create_lease(
            "test-native-x86-success", executable_path=x86_executable,
            candidate_dylibs={"moltenvk": x86_dylib}, expected_executable_sha256=x86_hash,
            expected_dylib_hashes={"moltenvk": dylib_hash}, manifest_architecture="x86_64",
            expected_architecture="x86_64")
        with lease:
            self.assertEqual(lease._native_architecture, "x86_64")
            self.assertEqual(lease._bound_dylibs["moltenvk"]["architecture"], "x86_64")

    def test_native_lease_rejects_signed_x86_64_manifest_with_arm64_binary(self) -> None:
        lease = self._create_lease("test-native-x86-arm-binary", manifest_architecture="x86_64")
        with self.assertRaisesRegex(ValueError, "declared x86_64, actual arm64"):
            with lease:
                pass

    def test_native_lease_rejects_unknown_declared_architecture(self) -> None:
        lease = self._create_lease("test-native-unknown-arch", manifest_architecture="universal")
        with self.assertRaisesRegex(ValueError, "supported native_architecture"):
            with lease:
                pass

    def test_native_lease_rejects_malformed_and_fat_macho(self) -> None:
        malformed = self.bin_dir / "malformed_fat"
        malformed.write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x02")
        malformed.chmod(0o755)
        lease = self._create_lease("test-native-malformed-fat", executable_path=malformed,
                                   expected_executable_sha256=None)
        with self.assertRaisesRegex(ValueError, "fat Mach-O architecture table is truncated"):
            with lease:
                pass

        fat = self.bin_dir / "fat_binary"
        fat.write_bytes(b"\xca\xfe\xba\xbe\x00\x00\x00\x02" + b"\x00" * 40)
        fat.chmod(0o755)
        lease_fat = self._create_lease("test-native-fat", executable_path=fat,
                                       expected_executable_sha256=None)
        with self.assertRaisesRegex(ValueError, "fat Mach-O binaries are unsupported"):
            with lease_fat:
                pass

    def test_native_lease_rejects_runtime_architecture_override(self) -> None:
        lease = self._create_lease("test-native-arch-override", expected_architecture="x86_64")
        with self.assertRaisesRegex(ValueError, "architecture differs from the signed manifest"):
            with lease:
                pass

    def test_native_lease_rejects_sha256_mismatch(self) -> None:
        lease = self._create_lease("test-native-sha-mismatch", expected_executable_sha256="0" * 64)
        with self.assertRaisesRegex(ValueError, "content SHA-256 mismatch"):
            with lease:
                pass

    def test_native_lease_rejects_symlink_executable_and_dylibs(self) -> None:
        sym_bin = self.bin_dir / "sym_bin"
        os.symlink(self.executable_path, sym_bin)
        lease = self._create_lease("test-native-sym-bin", executable_path=sym_bin, expected_executable_sha256=None)
        with self.assertRaisesRegex(ValueError, "not a symlink"):
            with lease:
                pass

        sym_dylib = self.lib_dir / "sym.dylib"
        os.symlink(self.dylib_path, sym_dylib)
        lease_dylib = self._create_lease("test-native-sym-dylib",
                                          candidate_dylibs={"moltenvk": sym_dylib},
                                          expected_dylib_hashes=None)
        with self.assertRaisesRegex(ValueError, "not a symlink"):
            with lease_dylib:
                pass

    def test_native_lease_rejects_unapproved_binary_in_candidate_dir(self) -> None:
        extra_dylib = self.lib_dir / "unapproved.dylib"
        extra_dylib.write_bytes(b"extra")
        lease = self._create_lease("test-native-unapproved-binary")
        with self.assertRaisesRegex(ValueError, "candidate directory contains unapproved binary"):
            with lease:
                pass

    def test_native_lease_rejects_forbidden_env_variables(self) -> None:
        lease = self._create_lease("test-native-forbidden-env")
        with self.assertRaisesRegex(ValueError, "forbidden loader variable: DYLD_INSERT_LIBRARIES"):
            lease.set_env({"DYLD_INSERT_LIBRARIES": "/evil.dylib"})
        with self.assertRaisesRegex(ValueError, "forbidden loader variable: PYTHONPATH"):
            lease.set_env({"PYTHONPATH": "/tmp"})
        with self.assertRaisesRegex(ValueError, "forbidden execution hook: BASH_ENV"):
            lease.set_env({"BASH_ENV": "/tmp/hook.sh"})

    def test_native_lease_rejects_env_mutation_after_sealing(self) -> None:
        lease = self._create_lease("test-native-env-mutation")
        proc = mock.Mock()
        proc.poll.return_value = 0
        with mock.patch.object(policy.subprocess, "Popen", return_value=proc):
            with lease:
                with self.assertRaisesRegex(ValueError, "environment mutation after sealing is forbidden"):
                    lease.start_native_process([str(self.executable_path)], env={"MUTATED": "1"})

    def test_native_lease_rejects_wine_prefix_manifest(self) -> None:
        manifest_path = self._make_manifest("test-native-wine-pref", self.output_dir, expected_prefix_bytes=3 * policy.GIB)
        lease = self._create_lease("test-native-wine-pref", manifest_path=manifest_path)
        with self.assertRaisesRegex(ValueError, "requires expected_prefix_bytes == 0"):
            with lease:
                pass

    def test_native_lease_rejects_tampered_manifest(self) -> None:
        manifest_path = self._make_manifest("test-native-tampered", self.output_dir)
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        raw["manifest_hmac_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(raw), encoding="utf-8")
        lease = self._create_lease("test-native-tampered", manifest_path=manifest_path)
        with self.assertRaisesRegex(RuntimeError, "refuses an edited or unsigned storage manifest"):
            with lease:
                pass

    def test_native_lease_rejects_stale_gate0_attestation(self) -> None:
        manifest_path = self._make_manifest("test-native-stale-gate0", self.output_dir)
        self.gate_attestation.write_text(json.dumps({"status": "PASS", "nonce": "different"}), encoding="utf-8")
        lease = self._create_lease("test-native-stale-gate0", manifest_path=manifest_path)
        with self.assertRaisesRegex(RuntimeError, "manifest does not bind to the current Gate 0 attestation"):
            with lease:
                pass

    def test_native_lease_active_marker_collision(self) -> None:
        marker = self.active_root / "test-native-collision.json"
        marker.write_text(json.dumps({"experiment_id": "test-native-collision"}), encoding="utf-8")
        lease = self._create_lease("test-native-collision")
        with self.assertRaises((FileExistsError, RuntimeError)):
            with lease:
                pass

    def test_native_lease_orphan_process_quarantines_scratch_and_invalidates_gate0(self) -> None:
        lease = self._create_lease("test-native-orphan")
        proc = mock.Mock()
        proc.poll.return_value = None
        proc.args = [str(self.executable_path)]
        with mock.patch.object(policy.subprocess, "Popen", return_value=proc), \
             mock.patch.object(policy, "_invalidate_gate0_after_cleanup_threshold_failure") as mock_inval:
            with self.assertRaisesRegex(RuntimeError, "NativeRunLease cleanup failed"):
                with lease:
                    child = lease.start_native_process([str(self.executable_path)])
                    lease._terminate_and_reap = mock.Mock(return_value=False)
            mock_inval.assert_called_once()
        receipt_path = self.output_dir / "native-run-cleanup.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "FAIL")
        self.assertFalse(receipt["temporary_removed"])
        marker = json.loads((self.active_root / "test-native-orphan.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["state"], "RECONCILIATION_REQUIRED")

    def test_native_lease_cleanup_failure_invalidates_gate0(self) -> None:
        lease = self._create_lease("test-native-cleanup-fail")
        with mock.patch.object(policy, "_remove_child_directory_nofollow", side_effect=OSError("permission denied")), \
             mock.patch.object(policy, "_invalidate_gate0_after_cleanup_threshold_failure") as mock_inval:
            with self.assertRaisesRegex(RuntimeError, "NativeRunLease cleanup failed"):
                with lease:
                    pass
            mock_inval.assert_called_once()
        receipt_path = self.output_dir / "native-run-cleanup.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "FAIL")
        marker = json.loads((self.active_root / "test-native-cleanup-fail.json").read_text(encoding="utf-8"))
        self.assertEqual(marker["state"], "RECONCILIATION_REQUIRED")

    def test_native_lease_log_size_exceeded(self) -> None:
        lease = self._create_lease("test-native-log-exceeded", max_log_bytes=100)
        with mock.patch.object(policy, "_invalidate_gate0_after_cleanup_threshold_failure") as mock_inval:
            with self.assertRaisesRegex(RuntimeError, "NativeRunLease cleanup failed"):
                with lease:
                    (self.output_dir / "trace.jsonl").write_bytes(b"X" * 200)
            mock_inval.assert_called_once()
        receipt_path = self.output_dir / "native-run-cleanup.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "FAIL")
        self.assertIn("log file exceeds maximum size limit", receipt.get("cleanup_error", ""))

    def test_assert_native_lease_helper(self) -> None:
        exp_id = "test-native-assert"
        runner = self.repo / "native-runner.py"
        runner.write_text("# authorized native runner\n", encoding="utf-8")
        runner_hash = hashlib.sha256(runner.read_bytes()).hexdigest()
        manifest_path = self._make_manifest(exp_id, self.output_dir, native_architecture="arm64")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update({"authorized_runner_paths": [str(runner)],
                         "authorized_runner_sha256": {str(runner): runner_hash}})
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "requires an active NativeRunLease marker"):
            policy.assert_native_lease(exp_id, self.executable_path, runner)

        marker_path = self.active_root / f"{exp_id}.json"
        marker_data = {
            "experiment_id": exp_id,
            "lease_type": "NativeRunLease",
            "state": "RUNNING",
            "executable_path": str(self.executable_path),
            "manifest_path": str(manifest_path),
            "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            "native_architecture": "arm64",
        }
        marker_path.write_text(json.dumps(marker_data), encoding="utf-8")
        record = policy.assert_native_lease(exp_id, self.executable_path, runner)
        self.assertEqual(record["state"], "RUNNING")

        runner.write_text("# changed after admission\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "not authorized by the signed manifest"):
            policy.assert_native_lease(exp_id, self.executable_path, runner)
        runner.write_text("# authorized native runner\n", encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "Native lease marker names different executable"):
            policy.assert_native_lease(exp_id, self.bin_dir / "other", runner)

    def test_scoped_lease_with_native_run_lease(self) -> None:
        lease = self._create_lease("test-scoped-native")
        with self.assertRaises(RuntimeError):
            policy.current_lease()
        with policy.scoped_lease(lease):
            self.assertIs(policy.current_lease(), lease)
        with self.assertRaises(RuntimeError):
            policy.current_lease()


class R78StorageSmokeTransitionTests(unittest.TestCase):
    """Synthetic Gate-to-PrefixLease admission tests; no prefix is materialized."""

    def test_smoke_python_bytecode_suppression_covers_parent_and_children(self) -> None:
        self.assertEqual(os.environ.get("PYTHONDONTWRITEBYTECODE"), "1")
        self.assertTrue(smoke_prefix_lease.sys.dont_write_bytecode)

        policy_tests = (
            "test_normal_cleanup_removes_prefix",
            "test_original_failure_still_runs_and_finishes_cleanup",
            "test_system_exit_zero_cannot_hide_cleanup_failure",
            "test_uncertified_prefix_is_rejected",
            "test_preservation_stays_within_configured_cap",
            "test_full_preserved_cap_leaves_uncertified_prefix_for_gate_block",
        )
        gate0_tests = (
            "test_stale_inventory_hash_rejects_admission",
            "test_stale_cleanup_receipt_hash_rejects_admission",
            "test_stale_control_hash_rejects_admission",
            "test_cleanup_cannot_pass_below_gate0_free_or_project_threshold",
            "test_threshold_cleanup_blocks_signed_gate_for_next_admission",
            "test_smoke_evidence_requires_one_active_prefix_and_baseline_return",
            "test_invalid_signed_gate_is_not_rewritten_during_threshold_failure",
            "test_failed_initial_threshold_observation_cannot_be_overwritten_by_later_sample",
            "test_wait_process_timeout_reaps_child_and_reports_timeout",
            "test_unreaped_timeout_marks_lease_unsafe",
        )

        def completed_output(names: Sequence[str]) -> str:
            lines = [f"{name} (SyntheticTests) ... ok" for name in names]
            return "\n".join(lines) + f"\nRan {len(names)} tests in 0.01s\n\nOK\n"

        results = [
            SimpleNamespace(returncode=0, stdout=completed_output(policy_tests)),
            SimpleNamespace(returncode=0, stdout=completed_output(gate0_tests)),
        ]
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(smoke_prefix_lease.subprocess, "run", side_effect=results) as run:
            smoke_prefix_lease.run_synthetic_policy_suite()

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertIn("-B", call.args[0])
            self.assertEqual(call.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")

    def test_gate0_python_bytecode_suppression_covers_parent_and_children(self) -> None:
        self.assertEqual(os.environ.get("PYTHONDONTWRITEBYTECODE"), "1")
        self.assertTrue(gate0.sys.dont_write_bytecode)

        with tempfile.TemporaryDirectory(prefix="fgmetal-gate0-bytecode-") as temporary:
            root = Path(temporary)
            report_path = root / "build-retention-tests.log"
            inventory_path = root / "storage-inventory.json"
            inventory_path.write_text('{"synthetic": true}', encoding="utf-8")
            output = "Ran 36 tests in 0.01s\n\nOK\n"
            results = [
                SimpleNamespace(returncode=0, stdout=output),
                SimpleNamespace(returncode=0, stdout=""),
            ]
            with mock.patch.dict(
                    os.environ, {
                        "FG_METAL_R93_PLAN_PATH": "plan21.json",
                        "FG_METAL_R94_PLAN_PATH": "plan22.json",
                        "FG_METAL_R92_PLAN_PATH": "plan20.json",
                    }, clear=True), \
                 mock.patch.object(gate0, "TEST_REPORT", report_path), \
                 mock.patch.object(gate0, "INVENTORY", inventory_path), \
                 mock.patch.object(gate0.subprocess, "run", side_effect=results) as run:
                gate0._run_test_suite()
                self.assertEqual(gate0._refresh_inventory(), {"synthetic": True})
                self.assertTrue(report_path.exists())

        self.assertEqual(run.call_count, 2)
        for call in run.call_args_list:
            self.assertIn("-B", call.args[0])
            self.assertEqual(call.kwargs["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertNotIn("FG_METAL_R92_PLAN_PATH", run.call_args_list[0].kwargs["env"])
        self.assertNotIn("FG_METAL_R93_PLAN_PATH", run.call_args_list[0].kwargs["env"])
        self.assertNotIn("FG_METAL_R94_PLAN_PATH", run.call_args_list[0].kwargs["env"])

    def test_smoke_wine_environment_filters_host_startup_hooks(self) -> None:
        parent_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": "/Users/example",
            "PYTHONPATH": "experiments/storage",
            "pythonhome": "/tmp/python-home",
            "PYTHONSTARTUP": "/tmp/startup.py",
            "LD_PRELOAD": "/tmp/host.dylib",
            "__XPC_DYLD_INSERT_LIBRARIES": "/tmp/xpc.dylib",
            "WINEPREFIX": "/tmp/other-prefix",
            "FG_B_TRACE_DIRECTORY": "/tmp/other-run",
            "DYLD_LIBRARY_PATH": "/tmp/host-libraries",
        }

        child_env = smoke_prefix_lease.wine_child_environment(parent_env)

        self.assertEqual(child_env, {"PATH": "/usr/bin:/bin", "HOME": "/Users/example"})
        self.assertIn("PYTHONPATH", parent_env)

    def test_smoke_cli_requires_explicit_opt_in_and_nonce_identity(self) -> None:
        script = Path(__file__).with_name("smoke_prefix_lease.py")
        for argv, expected in ((["--help"], 0), (["--invalid"], 2), ([], 2),
                               (["--execute"], 2),
                               (["--execute", "--experiment-id", "bad"], 2)):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [sys.executable, str(script), *argv], cwd=Path(__file__).parents[2],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    check=False, timeout=15)
                self.assertEqual(result.returncode, expected)
                self.assertNotIn("storage certification", result.stdout + result.stderr)

    def _fixture(self, *, admit: bool = True) -> dict[str, Any]:
        temporary = tempfile.TemporaryDirectory(prefix="fgmetal-r78-smoke-", dir="/private/tmp")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        os.chown(base, os.getuid(), os.getgid())
        repo = base / "repo"
        storage = repo / "experiments/storage"
        cache = base / "cache"
        repo.mkdir()
        cache.mkdir()
        prefix_root = cache / "prefixes"
        prefix_root.mkdir()
        active_root = prefix_root / ".active-leases"
        active_root.mkdir(mode=0o755)
        template = prefix_root / "wine-template"
        template.mkdir()
        (template / "system.reg").write_bytes(b"synthetic clean template")
        (template / "drive_c").mkdir()
        (template / "drive_c" / "nested.reg").write_bytes(b"synthetic nested template entry")
        runtime_root = base / "wine-runtime"
        (runtime_root / "bin").mkdir(parents=True)
        (runtime_root / "bin/wineserver").write_bytes(b"synthetic wineserver")
        wine_binary = runtime_root / "bin/wine"
        wine_binary.write_bytes(b"synthetic Wine loader")
        wine_binary.chmod(0o755)
        wine_binary_sha256 = hashlib.sha256(wine_binary.read_bytes()).hexdigest()
        runtime_tree_sha256 = "c" * 64
        moltenvk_sha256 = "d" * 64
        template_marker = {
            "graphics_experiments_run": False,
            "tree_identity": policy._tree_fingerprint(template),
            "runtime_identity": {
                "wine_runtime_root": str(runtime_root),
                "wine_runtime_tree_sha256": runtime_tree_sha256,
                "wine_binary_sha256": wine_binary_sha256,
                "moltenvk_sha256_for_subsequent_graphics_runs": moltenvk_sha256,
            },
        }
        (template / "FGMETAL_PREFIX_TEMPLATE.json").write_text(
            json.dumps(template_marker, sort_keys=True), encoding="utf-8")
        for directory in (storage / "manifests", storage / "smoke-claims",
                          storage / "smoke-runs"):
            directory.mkdir(parents=True, exist_ok=True)
        synthetic_runner_path = storage / "smoke_prefix_lease.py"
        synthetic_runner_path.write_bytes(b"synthetic authorized runner source\n")
        attestation_path = storage / "storage-gate0-attestation.json"
        sidecar_path = storage / "storage-gate0-path-state.json.zlib"
        inventory_path = storage / "storage-inventory.json"
        receipt_path = storage / "storage-cleanup-receipt.json"
        claim_root = storage / "smoke-claims"
        key_path = cache / "signing-key"
        retention = {"gate_status": "PASS", "gate_blockers": [], "active": [],
                     "active_build_count": 0, "preserved_build_count": 0,
                     "retained_build_count": 0, "active_dxvk_count": 0,
                     "active_moltenvk_count": 0, "unknown": [],
                     "completed_pending_retirement": [], "failed_or_retiring": [],
                     "retention_fingerprint": {"sha256": "synthetic-retention-fingerprint"}}

        def synthetic_usage(**_kwargs: Any) -> dict[str, int]:
            allocated = (sidecar_path.lstat().st_blocks * 512
                         if sidecar_path.exists() else 0)
            return {
                "allocated_inode_deduplicated_bytes": allocated,
                "allocated_inode_deduplicated_bytes_excluding_paths": allocated,
            }

        roots = [
            policy.DiscoveredProjectRoot(repo, policy.ROOT_CLASS_MANDATORY, "main_repo"),
            policy.DiscoveredProjectRoot(cache, policy.ROOT_CLASS_MANDATORY, "project_cache"),
        ]
        replacements = {
            "REPO": repo,
            "CACHE": cache,
            "GATE0_ATTESTATION_PATH": attestation_path,
            "GATE0_PATH_STATE_SIDECAR_PATH": sidecar_path,
            "INVENTORY_PATH": inventory_path,
            "STORAGE_CLEANUP_RECEIPT_PATH": receipt_path,
            "SMOKE_CLAIM_ROOT": claim_root,
            "MANIFEST_SIGNING_KEY_PATH": key_path,
            "PREFIX_ROOT": prefix_root,
            "PREFIX_TEMPLATE": template,
            "ACTIVE_LEASE_ROOT": active_root,
        }
        for name, value in replacements.items():
            patcher = mock.patch.object(policy, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patches = [
            mock.patch.object(policy, "_classified_project_roots", return_value=roots),
            mock.patch.object(policy, "_acquire_storage_allocation_lock",
                              side_effect=lambda: (base / "allocation.lock").open("a+")),
            mock.patch.object(policy, "disk_free_bytes", return_value=100 * policy.GIB),
            mock.patch.object(policy, "measure_project_usage", side_effect=synthetic_usage),
            mock.patch.object(policy, "uncertified_prefixes", return_value=[]),
            mock.patch.object(policy, "preserved_prefixes", return_value=[]),
            mock.patch.object(policy, "_active_lease_markers", return_value=[]),
            mock.patch.object(policy, "_active_vm_capacity_session_blockers", return_value=[]),
            mock.patch.object(policy, "_vm_capacity_commitment_snapshot", return_value={
                "committed_growth_bytes": 0, "marker_sha256": None,
                "marker_identity": None,
            }),
            mock.patch.object(policy, "_pending_prefix_reconciliation_receipts", return_value=[]),
            mock.patch.object(policy, "_pending_prefix_recovery_receipts", return_value=[]),
            mock.patch.object(policy, "prefix_state_fingerprint",
                              return_value={"sha256": "synthetic-prefix-fingerprint"}),
            mock.patch.object(policy.subprocess, "run",
                              return_value=SimpleNamespace(stdout="")),
            mock.patch.object(build_retention, "status_snapshot", side_effect=lambda: retention),
            mock.patch.object(build_retention, "canonical_artifact_store_audit",
                              return_value={"sha256": "synthetic-cas-audit"}),
            mock.patch.object(policy, "_attested_smoke_evidence_blockers", return_value=[]),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

        policy._manifest_signing_key(create=True)
        smoke_id = (f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}"
                    f"{time.strftime('%Y%m%d', time.gmtime())}-{uuid.uuid4().hex[:16]}")
        output_dir = storage / "smoke-runs" / smoke_id
        manifest_path = storage / "manifests" / f"{smoke_id}.json"
        claim_path = claim_root / f"{smoke_id}.json"

        now = time.time()
        retention_state = gate0._retention_state(retention)
        inventory = {
            "generated_at_unix": now,
            "filesystem": {"free_bytes": 100 * policy.GIB},
            "totals": {
                "fgmetal_scoped_allocated_inode_bytes": 0,
                "fgmetal_scoped_observed_allocated_inode_bytes": 0,
                "fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes": 0,
                "unaccounted_unlinked_mounted_backing_images": [],
            },
            "storage_gate_metrics": {
                "build_retention_fingerprint_sha256": "synthetic-retention-fingerprint",
                "build_retention": {"gate_status": "PASS"},
                "prefix_state_fingerprint_sha256": "synthetic-prefix-fingerprint",
            },
        }
        inventory_bytes = (json.dumps(inventory, sort_keys=True) + "\n").encode()
        inventory_path.write_bytes(inventory_bytes)
        receipt = {"phases": [{
            "phase_id": "synthetic-r78-phase", "status": "PASS",
            "storage_hygiene_status": "PASS", "storage_policy_reconciliation": True,
            "final_free_bytes": 100 * policy.GIB,
            "fgmetal_scoped_allocated_bytes": 0,
            "completed_at_unix": now,
            "prefix_state_fingerprint_sha256": "synthetic-prefix-fingerprint",
            "worktree_count": 0, "preserved_prefix_count": 0,
            "uncertified_prefix_count": 0, "active_lease_count": 0,
            "build_retention_state": retention_state,
        }]}
        receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        receipt_path.write_bytes(receipt_bytes)
        test_report = storage / "build-retention-tests.log"
        test_report.write_bytes(b"synthetic passing suite")
        supplemental_hashes: dict[str, str] = {}
        for name in policy.GATE0_SUPPLEMENTAL_EVIDENCE:
            supplemental_path = policy.gate0_supplemental_path(repo, name)
            supplemental_path.parent.mkdir(parents=True, exist_ok=True)
            if name == policy.G0C_REVIEW_RECORD_NAME:
                reviewed_files: dict[str, Any] = {}
                for field, relative_path in policy.G0C_REVIEW_REQUIRED_FILES.items():
                    reviewed_path = storage / relative_path
                    reviewed_path.parent.mkdir(parents=True, exist_ok=True)
                    contents = ("synthetic reviewed R78 input " + relative_path + "\n").encode()
                    reviewed_path.write_bytes(contents)
                    reviewed_files[field] = {
                        "path": (Path("experiments/storage") / relative_path).as_posix(),
                        "sha256": hashlib.sha256(contents).hexdigest(),
                        "size_bytes": len(contents),
                    }
                contents = (json.dumps({"status": "PASS", "findings": [],
                                        "reviewed_files": reviewed_files},
                                       sort_keys=True) + "\n").encode()
            else:
                contents = ("synthetic evidence " + name).encode()
            supplemental_path.write_bytes(contents)
            supplemental_hashes[name] = hashlib.sha256(contents).hexdigest()
        # Gate evidence names exist before the signed baseline so their parent
        # directory link count is already settled on filesystems that count
        # atomic sidecar/attestation publication in st_nlink.
        attestation_path.write_bytes(b"{}\n")
        attestation_path.chmod(0o600)
        initial_tree = policy.capture_storage_tree_state()
        allocation = policy._make_nonlease_allocation_snapshot([], [])
        sidecar_descriptor = policy.write_gate_path_state_sidecar(initial_tree, allocation)
        attestation: dict[str, Any] = {
            "schema_version": 1,
            "status": "PREFLIGHT_PASS",
            "mode": "preflight",
            "created_at_unix": time.time(),
            "prefixlease_smoke_status": "PENDING",
            "permitted_smoke_experiment_id": smoke_id,
            "path_state_sidecar": sidecar_descriptor,
            "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
            "cleanup_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "automatic_pre_run_disk_checks": True,
            "automatic_post_run_disk_checks": True,
            "automatic_retention_controls_installed": True,
            "runner_admission_contract": policy.RUNNER_ADMISSION_CONTRACT,
            "authorized_runner_source_hashes_required": True,
            "certified_template": True,
            "template_marker_sha256": hashlib.sha256(
                (template / "FGMETAL_PREFIX_TEMPLATE.json").read_bytes()).hexdigest(),
            "template_tree_sha256": template_marker["tree_identity"]["tree_sha256"],
            "retention_control_sha256": policy._gate0_control_hashes(),
            "manifest_signing_key_sha256": hashlib.sha256(
                policy._manifest_signing_key()).hexdigest(),
            "build_retention_state": retention_state,
            "build_retention_fingerprint_sha256": "synthetic-retention-fingerprint",
            "prefix_state_fingerprint_sha256": "synthetic-prefix-fingerprint",
            "canonical_artifact_store_audit": {"sha256": "synthetic-cas-audit"},
            "worktree_count": 0, "active_build_count": 0,
            "preserved_build_count": 0, "retained_build_count": 0,
            "prefix_count": 0, "preserved_prefix_count": 0,
            "uncertified_prefix_count": 0, "active_lease_count": 0,
            "inventory_measurements": {
                "free_bytes": 100 * policy.GIB,
                "fgmetal_scoped_allocated_inode_bytes": 0,
                "fgmetal_scoped_observed_allocated_inode_bytes": 0,
                "fgmetal_scoped_observed_nonlease_allocated_inode_bytes": 0,
            },
            "measurements": {
                "free_bytes": 100 * policy.GIB,
                "fgmetal_scoped_allocated_inode_bytes": 0,
                "fgmetal_scoped_observed_allocated_inode_bytes": 0,
                "fgmetal_scoped_observed_nonlease_allocated_inode_bytes": 0,
                "signed_persistent_output_bindings": [],
                "nonlease_allocation_snapshot": allocation,
            },
            "supplemental_evidence_sha256": supplemental_hashes,
            "build_retention_test_report_path": str(test_report),
            "build_retention_test_report_sha256": hashlib.sha256(
                test_report.read_bytes()).hexdigest(),
        }
        attestation["smoke_nonce_claim_slot"] = policy._write_storage_smoke_claim_slot(
            smoke_id)
        before = policy.capture_storage_tree_state()
        sidecar_descriptor = policy.write_gate_path_state_sidecar(before, allocation)
        nonlease_allocated_bytes = sidecar_descriptor["allocated_bytes"]
        inventory["totals"]["fgmetal_scoped_allocated_inode_bytes"] = nonlease_allocated_bytes
        inventory["totals"]["fgmetal_scoped_observed_allocated_inode_bytes"] = nonlease_allocated_bytes
        inventory["storage_gate_metrics"]["fgmetal_scoped_observed_nonlease_allocated_inode_bytes"] = nonlease_allocated_bytes
        inventory_bytes = (json.dumps(inventory, sort_keys=True) + "\n").encode()
        inventory_path.write_bytes(inventory_bytes)
        receipt["phases"][0]["fgmetal_scoped_allocated_bytes"] = nonlease_allocated_bytes
        receipt_bytes = (json.dumps(receipt, sort_keys=True) + "\n").encode()
        receipt_path.write_bytes(receipt_bytes)
        before = policy.capture_storage_tree_state()
        sidecar_descriptor = policy.write_gate_path_state_sidecar(before, allocation)
        nonlease_allocated_bytes = sidecar_descriptor["allocated_bytes"]
        attestation["path_state_sidecar"] = sidecar_descriptor
        attestation["inventory_sha256"] = hashlib.sha256(inventory_bytes).hexdigest()
        attestation["cleanup_receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
        attestation["inventory_measurements"]["fgmetal_scoped_allocated_inode_bytes"] = nonlease_allocated_bytes
        attestation["inventory_measurements"]["fgmetal_scoped_observed_allocated_inode_bytes"] = nonlease_allocated_bytes
        attestation["measurements"]["fgmetal_scoped_allocated_inode_bytes"] = nonlease_allocated_bytes
        attestation["measurements"]["fgmetal_scoped_observed_allocated_inode_bytes"] = nonlease_allocated_bytes
        attestation["measurements"]["fgmetal_scoped_observed_nonlease_allocated_inode_bytes"] = nonlease_allocated_bytes
        attestation["measurements"]["nonlease_allocation_snapshot"] = (
            policy.read_gate_path_state_sidecar(attestation)["nonlease_allocation_snapshot"])
        attestation["gate0_hmac_sha256"] = policy._gate0_signature(attestation)
        attestation_bytes = ((json.dumps(attestation, indent=2, sort_keys=True) + "\n")
                             .encode("utf-8"))
        attestation_path.write_bytes(attestation_bytes)
        sidecar_bytes = sidecar_path.read_bytes()

        # ALLOW creation is the bootstrap transaction; the subsequent PrefixLease
        # checks use the actual signed Gate evaluator and exact tree transition.
        manifest = None
        if admit:
            with mock.patch.object(policy, "__file__", str(storage / "storage_policy.py")), \
                    mock.patch.object(policy, "_gate0_blockers", return_value=[]):
                manifest = policy.begin_experiment(
                    experiment_id=smoke_id,
                    purpose="R78 synthetic Gate-to-PrefixLease lifecycle test",
                    candidate_hashes={
                        "wine_runtime_tree": runtime_tree_sha256,
                        "wine_binary": wine_binary_sha256,
                        "moltenvk": moltenvk_sha256,
                    },
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    authorized_runner_paths=[synthetic_runner_path],
                    storage_policy_smoke=True,
                    output_dir=output_dir,
                )
            output_dir.mkdir(mode=0o700)
            preflight_copy = output_dir / "gate0-preflight-attestation.json"
            sidecar_copy = output_dir / "gate0-path-state.json.zlib"
            preflight_copy.write_bytes(attestation_bytes)
            sidecar_copy.write_bytes(sidecar_bytes)
            preflight_copy.chmod(0o600)
            sidecar_copy.chmod(0o600)
        after = policy.capture_storage_tree_state()
        return {
            "base": base,
            "repo": repo,
            "storage": storage,
            "cache": cache,
            "prefix_root": prefix_root,
            "active_root": active_root,
            "template": template,
            "runtime_root": runtime_root,
            "attestation_path": attestation_path,
            "attestation_bytes": attestation_bytes,
            "sidecar_path": sidecar_path,
            "sidecar_bytes": sidecar_bytes,
            "claim_path": claim_path,
            "manifest_path": manifest_path,
            "runner_path": synthetic_runner_path,
            "manifest": manifest,
            "manifest_bytes": manifest_path.read_bytes() if manifest_path.exists() else None,
            "output_dir": output_dir,
            "smoke_id": smoke_id,
            "before": before,
            "after": after,
        }

    def _transition(self, fixture: Mapping[str, Any], **overrides: Any) -> bool:
        args = {
            "experiment_id": fixture["smoke_id"],
            "attestation_bytes": fixture["attestation_bytes"],
            "sidecar_bytes": fixture["sidecar_bytes"],
            "output_dir": fixture["output_dir"],
            "manifest_path": fixture["manifest_path"],
            "claim_path": fixture["claim_path"],
        }
        args.update(overrides)
        return policy.evaluate_storage_smoke_transition(
            fixture["before"], fixture["after"], **args)

    def _refresh_preflight_tree_after_fixture_reset(self, fixture: dict[str, Any]) -> None:
        """Re-sign a fresh synthetic Gate after tests undo a completed smoke."""
        attestation = json.loads(fixture["attestation_bytes"])
        attestation["smoke_nonce_claim_slot"] = policy._write_storage_smoke_claim_slot(
            fixture["smoke_id"])
        allocation = json.loads(fixture["attestation_bytes"])["measurements"][
            "nonlease_allocation_snapshot"]
        allocation = dict(allocation)
        allocation["entries"] = [
            row for row in allocation["entries"]
            if row.get("path") != str(policy.GATE0_PATH_STATE_SIDECAR_PATH)
        ]
        allocation["entry_count"] = len(allocation["entries"])
        allocation["entries_sha256"] = policy._allocation_snapshot_entries_sha256(
            allocation["entries"])
        tree = policy.capture_storage_tree_state()
        descriptor = policy.write_gate_path_state_sidecar(tree, allocation)
        attestation["path_state_sidecar"] = descriptor
        attestation.pop("gate0_hmac_sha256", None)
        attestation["gate0_hmac_sha256"] = policy._gate0_signature(attestation)
        fixture["attestation_bytes"] = (json.dumps(attestation, indent=2, sort_keys=True)
                                        + "\n").encode()
        fixture["attestation_path"].write_bytes(fixture["attestation_bytes"])
        fixture["sidecar_bytes"] = fixture["sidecar_path"].read_bytes()

    def _pre_admission_claim_fixture(self) -> tuple[dict[str, Any], dict[str, Any], bytes,
                                                     dict[str, Any], list[Any]]:
        fixture = self._fixture(admit=False)
        preflight_bytes = fixture["attestation_bytes"]
        preflight = json.loads(preflight_bytes)
        baseline = policy.read_gate_path_state_sidecar(preflight)["storage_tree_state"]
        claim, claim_bytes = policy._claim_storage_smoke_nonce(
            fixture["smoke_id"], preflight_bytes)
        roots = policy._classified_project_roots()
        return fixture, claim, claim_bytes, baseline, roots

    def _pre_admission_decision(
            self, fixture: Mapping[str, Any], claim: Mapping[str, Any],
            claim_bytes: bytes, baseline: Mapping[str, Any],
            roots: Sequence[Any]) -> dict[str, Any]:
        current = policy.capture_storage_tree_state(classified_roots=roots)
        return policy._storage_smoke_pre_admission_path_delta(
            baseline, current,
            experiment_id=fixture["smoke_id"],
            gate_attestation_sha256=hashlib.sha256(
                fixture["attestation_bytes"]).hexdigest(),
            claim=claim, claim_bytes=claim_bytes, mandatory_roots=roots)

    def test_pre_admission_exact_nonce_claim_transition_is_allowed(self) -> None:
        fixture, claim, claim_bytes, baseline, roots = self._pre_admission_claim_fixture()
        result = self._pre_admission_decision(
            fixture, claim, claim_bytes, baseline, roots)

        self.assertEqual(result["decision"], "ALLOW")
        self.assertEqual(result["unexpected_paths"], [])
        self.assertEqual(result["unexpected_leaf_paths"], [])
        self.assertEqual(result["path_state_changed_path_count"], 1)
        changed = {row["path"]: row for row in result["changed_paths"]}
        self.assertEqual(
            changed[str(fixture["claim_path"])]["expected_classification"],
            "EXPECTED_AUTHENTICATED_NONCE_CLAIM")
        self.assertEqual(set(changed), {str(fixture["claim_path"])})
        self.assertIsNone(policy._current_preflight_smoke_id())
        with self.assertRaises(FileExistsError):
            policy._claim_storage_smoke_nonce(
                fixture["smoke_id"], fixture["attestation_bytes"])

    def test_pre_admission_same_size_claim_rewrite_after_tree_capture_is_refused(self) -> None:
        fixture, claim, claim_bytes, baseline, roots = self._pre_admission_claim_fixture()
        original_capture = policy.capture_storage_tree_state
        mutated = False

        def capture_then_rewrite(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
            nonlocal mutated
            snapshot = original_capture(*args, **kwargs)
            if not mutated:
                raw = fixture["claim_path"].read_bytes()
                rewritten = raw.replace(
                    b"PrefixLease smoke allocation",
                    b"PrefixLease smoke allocatioX", 1)
                self.assertNotEqual(rewritten, raw)
                self.assertEqual(len(rewritten), len(raw))
                with fixture["claim_path"].open("r+b") as stream:
                    stream.write(rewritten)
                    stream.flush()
                    os.fsync(stream.fileno())
                mutated = True
            return snapshot

        with mock.patch.object(policy, "capture_storage_tree_state",
                               side_effect=capture_then_rewrite):
            result = self._pre_admission_decision(
                fixture, claim, claim_bytes, baseline, roots)

        self.assertTrue(mutated)
        self.assertEqual(result["decision"], "REFUSE")
        self.assertEqual(result["unexpected_paths"], [str(fixture["claim_path"])])
        changed = {row["path"]: row for row in result["changed_paths"]}
        self.assertEqual(changed[str(fixture["claim_path"])]["predicate_result"], "FAIL")
        self.assertNotIn(fixture["smoke_id"],
                         policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS)

    def test_concurrent_nonce_slot_consumption_succeeds_exactly_once(self) -> None:
        fixture = self._fixture(admit=False)
        barrier = threading.Barrier(2)
        thread_state = threading.local()
        original_read_slot = policy._read_storage_smoke_claim_slot
        results: list[tuple[str, Any]] = []

        def synchronized_read_slot(experiment_id: str, descriptor: Any):
            result = original_read_slot(experiment_id, descriptor)
            if not getattr(thread_state, "passed_initial_slot_read", False):
                thread_state.passed_initial_slot_read = True
                barrier.wait(timeout=5)
            return result

        def consume() -> None:
            try:
                result = policy._claim_storage_smoke_nonce(
                    fixture["smoke_id"], fixture["attestation_bytes"])
                results.append(("success", result))
            except BaseException as error:
                results.append(("error", error))

        with mock.patch.object(policy, "_read_storage_smoke_claim_slot",
                               side_effect=synchronized_read_slot):
            workers = [threading.Thread(target=consume) for _ in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=10)

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        self.assertEqual([kind for kind, _value in results].count("success"), 1)
        errors = [value for kind, value in results if kind == "error"]
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], FileExistsError)
        claim, claim_bytes = policy._read_storage_smoke_claim(fixture["smoke_id"])
        self.assertEqual(claim["experiment_id"], fixture["smoke_id"])
        self.assertEqual(sum(1 for kind, _value in results if kind == "success"), 1)
        self.assertTrue(claim_bytes)
        self.assertIsNone(policy._current_preflight_smoke_id())
        with self.assertRaises(FileExistsError):
            policy._claim_storage_smoke_nonce(
                fixture["smoke_id"], fixture["attestation_bytes"])

    def test_pre_admission_claim_parent_only_utime_is_refused_and_readable(self) -> None:
        fixture = self._fixture(admit=False)
        preflight = json.loads(fixture["attestation_bytes"])
        baseline = policy.read_gate_path_state_sidecar(preflight)["storage_tree_state"]
        roots = policy._classified_project_roots()
        parent_path = fixture["claim_path"].parent
        parent_before = next(
            row for row in baseline["entries"] if row["path"] == str(parent_path))
        slot_inode = preflight["smoke_nonce_claim_slot"]["state"]["inode"]
        original_write = os.write
        utime_injected: list[bool] = []

        def write_then_utime(fd: int, data: bytes) -> int:
            written = original_write(fd, data)
            info = os.fstat(fd)
            if info.st_ino == slot_inode and not utime_injected:
                parent_stat = parent_path.stat()
                os.utime(parent_path, ns=(
                    parent_stat.st_atime_ns, parent_before["mtime_ns"] + 5_000_000_000))
                utime_injected.append(True)
            return written

        with mock.patch.object(policy.os, "write", side_effect=write_then_utime):
            claim, claim_bytes = policy._claim_storage_smoke_nonce(
                fixture["smoke_id"], fixture["attestation_bytes"])
        self.assertEqual(utime_injected, [True])

        result = self._pre_admission_decision(
            fixture, claim, claim_bytes, baseline, roots)

        self.assertEqual(result["decision"], "REFUSE")
        self.assertEqual(result["unexpected_paths"], [str(parent_path)])
        changed = {row["path"]: row for row in result["changed_paths"]}
        self.assertEqual(set(changed), {str(fixture["claim_path"]), str(parent_path)})
        self.assertEqual(
            changed[str(fixture["claim_path"])]["expected_classification"],
            "EXPECTED_AUTHENTICATED_NONCE_CLAIM")
        self.assertEqual(
            changed[str(parent_path)]["expected_classification"],
            "UNEXPECTED_GATE_PATH_STATE_DRIFT")

        parent_fd = policy._open_directory_nofollow(policy.SMOKE_CLAIM_ROOT)
        try:
            artifact, _compressed = policy._publish_pre_admission_delta_sidecar(
                result, parent_fd=parent_fd)
        finally:
            os.close(parent_fd)
        manifest = {
            "experiment_id": fixture["smoke_id"],
            "storage_policy_smoke": True,
            "decision": "REFUSE",
            "gate0_attestation_sha256": hashlib.sha256(
                fixture["attestation_bytes"]).hexdigest(),
            "smoke_nonce_claim_path": str(fixture["claim_path"]),
            "smoke_nonce_claim_sha256": hashlib.sha256(claim_bytes).hexdigest(),
            "pre_admission_path_delta": artifact,
            "pre_admission_path_delta_binding": artifact["binding"],
        }
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(
            manifest, create_key=True)
        verified = policy.read_storage_smoke_pre_admission_path_delta(manifest)
        self.assertEqual(verified["unexpected_paths"], [str(parent_path)])

    def test_gate0_deferred_smoke_tree_check_without_sink_fails_closed(self) -> None:
        fixture = self._fixture(admit=False)
        gate = json.loads(fixture["attestation_bytes"])
        baseline = policy.read_gate_path_state_sidecar(gate)["storage_tree_state"]
        with mock.patch.object(policy, "capture_storage_tree_state",
                               return_value=baseline) as capture:
            blockers = policy._gate0_blockers(
                active_experiment_id=fixture["smoke_id"],
                allow_storage_smoke=True,
                defer_pre_admission_smoke_tree_check=True,
            )

        self.assertTrue(any(
            "deferred smoke path-state check requires" in blocker
            for blocker in blockers))
        capture.assert_called_once()

    def test_gate0_deferred_smoke_tree_check_with_sink_preserves_signed_baseline(self) -> None:
        fixture = self._fixture(admit=False)
        gate = json.loads(fixture["attestation_bytes"])
        baseline = policy.read_gate_path_state_sidecar(gate)["storage_tree_state"]
        diagnostic_sink: dict[str, Any] = {}
        with mock.patch.object(policy, "capture_storage_tree_state",
                               return_value=baseline) as capture:
            blockers = policy._gate0_blockers(
                active_experiment_id=fixture["smoke_id"],
                allow_storage_smoke=True,
                diagnostic_sink=diagnostic_sink,
                defer_pre_admission_smoke_tree_check=True,
            )

        self.assertFalse(any(
            "deferred smoke path-state check requires" in blocker
            for blocker in blockers))
        self.assertEqual(
            diagnostic_sink["pre_admission_gate_path_state"]["baseline"], baseline)
        self.assertEqual(
            diagnostic_sink["pre_admission_gate_path_state"]["gate_attestation_sha256"],
            hashlib.sha256(fixture["attestation_bytes"]).hexdigest())
        capture.assert_not_called()

    def test_pre_admission_python_cache_mutation_is_explicit_and_refused(self) -> None:
        fixture, claim, claim_bytes, baseline, roots = self._pre_admission_claim_fixture()
        cache_leaf = fixture["storage"] / "__pycache__" / "storage_policy.cpython-312.pyc"
        cache_leaf.parent.mkdir()
        cache_leaf.write_bytes(b"injected bytecode cache")

        result = self._pre_admission_decision(
            fixture, claim, claim_bytes, baseline, roots)

        self.assertEqual(result["decision"], "REFUSE")
        self.assertIn(str(cache_leaf), result["unexpected_leaf_paths"])
        cache_rows = {row["path"]: row for row in result["changed_paths"]}
        self.assertEqual(cache_rows[str(cache_leaf)]["expected_classification"],
                         "UNEXPECTED_GATE_PATH_STATE_DRIFT")
        self.assertEqual(cache_rows[str(cache_leaf)]["after"]["kind"], "file")
        self.assertFalse(cache_rows[str(cache_leaf)]["predicate_result"] == "PASS")

    def test_pre_admission_git_metadata_mutation_is_explicit_and_refused(self) -> None:
        fixture, claim, claim_bytes, baseline, roots = self._pre_admission_claim_fixture()
        git_leaf = fixture["repo"] / ".git" / "index"
        git_leaf.parent.mkdir()
        git_leaf.write_bytes(b"injected Git index")

        result = self._pre_admission_decision(
            fixture, claim, claim_bytes, baseline, roots)

        self.assertEqual(result["decision"], "REFUSE")
        self.assertIn(str(git_leaf), result["unexpected_leaf_paths"])
        git_rows = {row["path"]: row for row in result["changed_paths"]}
        self.assertEqual(git_rows[str(git_leaf)]["expected_classification"],
                         "UNEXPECTED_GATE_PATH_STATE_DRIFT")
        self.assertEqual(git_rows[str(git_leaf)]["after"]["kind"], "file")

    def test_pre_admission_mixed_claim_and_leaf_mutation_is_signed_and_bound(self) -> None:
        fixture, claim, claim_bytes, baseline, roots = self._pre_admission_claim_fixture()
        unrelated = fixture["claim_path"].parent / "unrelated-pre-admission.bin"
        unrelated.write_bytes(b"injected unrelated mutation")
        result = self._pre_admission_decision(
            fixture, claim, claim_bytes, baseline, roots)
        self.assertEqual(result["decision"], "REFUSE")
        self.assertEqual(result["unexpected_leaf_paths"], [str(unrelated)])

        parent_fd = policy._open_directory_nofollow(policy.SMOKE_CLAIM_ROOT)
        try:
            artifact, _compressed = policy._publish_pre_admission_delta_sidecar(
                result, parent_fd=parent_fd)
        finally:
            os.close(parent_fd)
        manifest = {
            "experiment_id": fixture["smoke_id"],
            "storage_policy_smoke": True,
            "decision": "REFUSE",
            "gate0_attestation_sha256": hashlib.sha256(
                fixture["attestation_bytes"]).hexdigest(),
            "smoke_nonce_claim_path": str(fixture["claim_path"]),
            "smoke_nonce_claim_sha256": hashlib.sha256(claim_bytes).hexdigest(),
            "pre_admission_path_delta": artifact,
            "pre_admission_path_delta_binding": artifact["binding"],
        }
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(
            manifest, create_key=True)

        verified = policy.read_storage_smoke_pre_admission_path_delta(manifest)

        self.assertTrue(policy._verify_manifest_signature(manifest))
        self.assertEqual(verified["decision"], "REFUSE")
        self.assertEqual(verified["nonce_identity"]["claim_identity"]["sha256"],
                         hashlib.sha256(claim_bytes).hexdigest())
        self.assertEqual(verified["unexpected_leaf_paths"], [str(unrelated)])
        changed = {row["path"]: row for row in verified["changed_paths"]}
        self.assertEqual(changed[str(fixture["claim_path"])]["expected_classification"],
                         "EXPECTED_AUTHENTICATED_NONCE_CLAIM")
        self.assertEqual(changed[str(unrelated)]["expected_classification"],
                         "UNEXPECTED_GATE_PATH_STATE_DRIFT")
        self.assertIn(str(fixture["claim_path"].parent), changed)
        self.assertEqual(artifact["path_state_changed_path_count"], len(changed))

    def test_pre_admission_reader_rejects_authenticated_malformed_row(self) -> None:
        fixture, claim, claim_bytes, baseline, roots = self._pre_admission_claim_fixture()
        unrelated = fixture["storage"] / "malformed-row-target.bin"
        unrelated.write_bytes(b"synthetic drift")
        result = self._pre_admission_decision(
            fixture, claim, claim_bytes, baseline, roots)
        row = next(item for item in result["changed_paths"]
                   if item["path"] == str(unrelated))
        row["after"].pop("inode")
        parent_fd = policy._open_directory_nofollow(policy.SMOKE_CLAIM_ROOT)
        try:
            artifact, _compressed = policy._publish_pre_admission_delta_sidecar(
                result, parent_fd=parent_fd)
        finally:
            os.close(parent_fd)
        manifest = {
            "experiment_id": fixture["smoke_id"],
            "storage_policy_smoke": True,
            "decision": "REFUSE",
            "gate0_attestation_sha256": hashlib.sha256(
                fixture["attestation_bytes"]).hexdigest(),
            "smoke_nonce_claim_path": str(fixture["claim_path"]),
            "smoke_nonce_claim_sha256": hashlib.sha256(claim_bytes).hexdigest(),
            "pre_admission_path_delta": artifact,
            "pre_admission_path_delta_binding": artifact["binding"],
        }
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(
            manifest, create_key=True)

        with self.assertRaisesRegex(ValueError, "invalid inode"):
            policy.read_storage_smoke_pre_admission_path_delta(manifest)

    def test_begin_experiment_binds_exact_pre_admission_refusal_delta(self) -> None:
        fixture = self._fixture(admit=False)
        unrelated = fixture["storage"] / "injected-before-smoke-admission.bin"
        original_capture = policy.capture_storage_tree_state

        def capture_after_injected_leaf(*, excluded_paths=(), classified_roots=None):
            unrelated.write_bytes(b"injected drift immediately before admission observation")
            return original_capture(
                excluded_paths=excluded_paths, classified_roots=classified_roots)

        with (mock.patch.object(policy, "__file__",
                                str(fixture["storage"] / "storage_policy.py")),
              mock.patch.object(policy, "_gate0_blockers", return_value=[]),
              mock.patch.object(policy, "capture_storage_tree_state",
                                side_effect=capture_after_injected_leaf)):
            with self.assertRaisesRegex(RuntimeError, "storage gate refused experiment"):
                policy.begin_experiment(
                    experiment_id=fixture["smoke_id"],
                    purpose="Synthetic end-to-end pre-admission attribution test",
                    candidate_hashes={"synthetic": "c" * 64},
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    storage_justification="Bounded synthetic smoke admission test.",
                    storage_policy_smoke=True,
                    output_dir=fixture["output_dir"],
                )

        manifest = json.loads(fixture["manifest_path"].read_bytes())
        self.assertTrue(policy._verify_manifest_signature(manifest))
        self.assertEqual(manifest["decision"], "REFUSE")
        self.assertFalse(manifest.get("execution_authorized", False))
        delta = policy.read_storage_smoke_pre_admission_path_delta(manifest)
        self.assertEqual(delta["decision"], "REFUSE")
        self.assertEqual(delta["unexpected_leaf_paths"], [str(unrelated)])
        changed = {row["path"]: row for row in delta["changed_paths"]}
        self.assertIn(str(unrelated), changed)
        self.assertEqual(
            changed[str(unrelated)]["expected_classification"],
            "UNEXPECTED_GATE_PATH_STATE_DRIFT")
        self.assertEqual(
            changed[str(unrelated)]["predicate_result"], "FAIL")
        self.assertIn(str(fixture["claim_path"]), changed)
        self.assertNotIn(str(fixture["claim_path"].parent), changed)
        self.assertEqual(
            manifest["pre_admission_path_delta"]["path_state_changed_path_count"],
            len(changed))

    def test_begin_experiment_binds_post_snapshot_claim_rewrite_in_refusal_sidecar(self) -> None:
        fixture = self._fixture(admit=False)
        original_capture = policy.capture_storage_tree_state
        observed: dict[str, bytes] = {}

        def capture_then_rewrite(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
            snapshot = original_capture(*args, **kwargs)
            if not observed:
                original = fixture["claim_path"].read_bytes()
                replacement = json.loads(original)
                replacement["claim_reason"] = "PrefixLease smoke allocatioX"
                replacement = policy._seal_storage_evidence(
                    replacement, "smoke_claim_hmac_sha256")
                rewritten = policy._canonical_pretty_json_bytes(replacement)
                self.assertNotEqual(rewritten, original)
                self.assertEqual(len(rewritten), len(original))
                observed["expected"] = original
                observed["current"] = rewritten
                with fixture["claim_path"].open("r+b") as stream:
                    stream.write(rewritten)
                    stream.flush()
                    os.fsync(stream.fileno())
            return snapshot

        with (mock.patch.object(policy, "__file__",
                                str(fixture["storage"] / "storage_policy.py")),
              mock.patch.object(policy, "_gate0_blockers", return_value=[]),
              mock.patch.object(policy, "capture_storage_tree_state",
                                side_effect=capture_then_rewrite)):
            with self.assertRaisesRegex(RuntimeError, "storage gate refused experiment"):
                policy.begin_experiment(
                    experiment_id=fixture["smoke_id"],
                    purpose="Synthetic post-snapshot claim mutation diagnostic test",
                    candidate_hashes={"synthetic": "c" * 64},
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    storage_justification="Bounded synthetic claim mutation test.",
                    storage_policy_smoke=True,
                    output_dir=fixture["output_dir"],
                )

        manifest = json.loads(fixture["manifest_path"].read_bytes())
        self.assertTrue(policy._verify_manifest_signature(manifest))
        self.assertEqual(manifest["decision"], "REFUSE")
        self.assertFalse(manifest.get("execution_authorized", False))
        self.assertNotIn(fixture["smoke_id"],
                         policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS)
        delta = policy.read_storage_smoke_pre_admission_path_delta(manifest)
        claim_row = next(row for row in delta["changed_paths"]
                         if row["path"] == str(fixture["claim_path"]))
        self.assertEqual(claim_row["expected_classification"],
                         "UNEXPECTED_GATE_PATH_STATE_DRIFT")
        self.assertEqual(claim_row["predicate_result"], "FAIL")
        observation = claim_row["claim_observation"]
        self.assertEqual(observation["expected"]["sha256"],
                         hashlib.sha256(observed["expected"]).hexdigest())
        self.assertEqual(observation["current"]["sha256"],
                         hashlib.sha256(observed["current"]).hexdigest())
        self.assertNotEqual(observation["expected"]["sha256"],
                            observation["current"]["sha256"])
        self.assertEqual(observation["current"],
                         policy._storage_smoke_claim_observation(fixture["smoke_id"]))
        snapshot_state = claim_row["after"]
        self.assertTrue(any(
            observation["current"]["row"][key] != snapshot_state[key]
            for key in ("mtime_ns", "ctime_ns")))
        incorrectly_bound = dict(manifest)
        incorrectly_bound["smoke_nonce_claim_sha256"] = "0" * 64
        incorrectly_bound.pop("manifest_hmac_sha256")
        incorrectly_bound["manifest_hmac_sha256"] = policy._manifest_signature(
            incorrectly_bound)
        with self.assertRaisesRegex(ValueError, "authenticated nonce identity"):
            policy.read_storage_smoke_pre_admission_path_delta(incorrectly_bound)

    def test_begin_experiment_revalidates_claim_before_manifest_and_handoff(self) -> None:
        def rewrite_claim(fixture: Mapping[str, Any]) -> tuple[bytes, bytes]:
            path = fixture["claim_path"]
            original = path.read_bytes()
            record = json.loads(original)
            record["claim_reason"] = "PrefixLease smoke allocatioX"
            rewritten = policy._canonical_pretty_json_bytes(policy._seal_storage_evidence(
                record, "smoke_claim_hmac_sha256"))
            self.assertEqual(len(rewritten), len(original))
            with path.open("r+b") as stream:
                stream.write(rewritten)
                stream.flush()
                os.fsync(stream.fileno())
            return original, rewritten

        for boundary in ("before_manifest", "before_handoff"):
            with self.subTest(boundary=boundary):
                fixture = self._fixture(admit=False)
                original_delta = policy._storage_smoke_pre_admission_path_delta
                original_write = policy._write_json
                delta_calls = 0
                mutation: dict[str, bytes] = {}
                unrelated = fixture["storage"] / "race-unrelated-leaf.bin"

                def delta_then_mutate(*args: Any, **kwargs: Any) -> dict[str, Any]:
                    nonlocal delta_calls
                    result = original_delta(*args, **kwargs)
                    delta_calls += 1
                    if boundary == "before_manifest" and delta_calls == 1:
                        self.assertEqual(result["decision"], "ALLOW")
                        mutation["expected"], mutation["current"] = rewrite_claim(fixture)
                        unrelated.write_bytes(b"mixed unrelated race drift")
                    return result

                def write_then_mutate(path: Path, payload: Mapping[str, Any], **kwargs: Any) -> None:
                    original_write(path, payload, **kwargs)
                    if (boundary == "before_handoff" and path == fixture["manifest_path"]
                            and not mutation):
                        self.assertEqual(payload.get("decision"), "ALLOW")
                        mutation["expected"], mutation["current"] = rewrite_claim(fixture)

                with (mock.patch.object(policy, "__file__",
                                        str(fixture["storage"] / "storage_policy.py")),
                      mock.patch.object(policy, "_gate0_blockers", return_value=[]),
                      mock.patch.object(policy, "_storage_smoke_pre_admission_path_delta",
                                        side_effect=delta_then_mutate),
                      mock.patch.object(policy, "_write_json", side_effect=write_then_mutate)):
                    with self.assertRaisesRegex(RuntimeError, "storage gate refused experiment"):
                        policy.begin_experiment(
                            experiment_id=fixture["smoke_id"],
                            purpose=f"Synthetic {boundary} claim race revalidation",
                            candidate_hashes={"synthetic": "c" * 64},
                            expected_duration_seconds=300,
                            expected_prefix_bytes=3 * policy.GIB,
                            expected_build_bytes=0,
                            estimated_persistent_bytes=32 * policy.MIB,
                            storage_justification="Bounded synthetic claim race test.",
                            storage_policy_smoke=True,
                            output_dir=fixture["output_dir"],
                        )

                manifest = json.loads(fixture["manifest_path"].read_bytes())
                self.assertTrue(policy._verify_manifest_signature(manifest))
                self.assertEqual(manifest["decision"], "REFUSE")
                self.assertNotIn(fixture["smoke_id"],
                                 policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS)
                delta = policy.read_storage_smoke_pre_admission_path_delta(manifest)
                claim_row = next(row for row in delta["changed_paths"]
                                 if row["path"] == str(fixture["claim_path"]))
                observation = claim_row["claim_observation"]
                self.assertEqual(observation["expected"]["sha256"],
                                 hashlib.sha256(mutation["expected"]).hexdigest())
                self.assertEqual(observation["current"]["sha256"],
                                 hashlib.sha256(mutation["current"]).hexdigest())
                self.assertEqual(observation["current"],
                                 policy._storage_smoke_claim_observation(
                                     fixture["smoke_id"]))
                self.assertIn(str(fixture["claim_path"]), delta["unexpected_paths"])
                if boundary == "before_manifest":
                    self.assertIn(str(unrelated), delta["unexpected_paths"])
                self.assertTrue(delta["unexpected_paths"])

    def test_begin_experiment_clean_pre_admission_allow_is_handed_off(self) -> None:
        fixture = self._fixture(admit=False)
        with (mock.patch.object(policy, "__file__",
                                str(fixture["storage"] / "storage_policy.py")),
              mock.patch.object(policy, "_gate0_blockers", return_value=[])):
            manifest = policy.begin_experiment(
                experiment_id=fixture["smoke_id"],
                purpose="Synthetic clean pre-admission handoff test",
                candidate_hashes={"synthetic": "c" * 64},
                expected_duration_seconds=300,
                expected_prefix_bytes=3 * policy.GIB,
                expected_build_bytes=0,
                estimated_persistent_bytes=32 * policy.MIB,
                storage_justification="Bounded synthetic clean handoff test.",
                storage_policy_smoke=True,
                output_dir=fixture["output_dir"],
            )

        self.assertEqual(manifest["decision"], "ALLOW")
        self.assertTrue(policy._verify_manifest_signature(manifest))
        manifest_raw = fixture["manifest_path"].read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_raw).hexdigest()
        self.assertEqual(
            policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS[fixture["smoke_id"]],
            (os.getpid(), manifest_sha256))
        self.assertTrue(policy._consume_storage_smoke_published_allow(
            fixture["smoke_id"], manifest_sha256))

    def test_pre_admission_sidecar_captures_absent_and_nonregular_claim_paths(self) -> None:
        for replacement_kind in ("absent", "symlink", "directory", "special"):
            with self.subTest(replacement_kind=replacement_kind):
                fixture, claim, claim_bytes, baseline, roots = self._pre_admission_claim_fixture()
                claim_path = fixture["claim_path"]
                claim_path.unlink()
                target = fixture["repo"] / "claim-symlink-target"
                target.write_bytes(b"outside target remains unopened")
                if replacement_kind == "symlink":
                    claim_path.symlink_to(target)
                elif replacement_kind == "directory":
                    claim_path.mkdir()
                elif replacement_kind == "special":
                    os.mkfifo(claim_path, 0o600)

                after = policy.capture_storage_tree_state(classified_roots=roots)
                result = policy._storage_smoke_pre_admission_path_delta(
                    baseline, after,
                    experiment_id=fixture["smoke_id"],
                    gate_attestation_sha256=hashlib.sha256(
                        fixture["attestation_bytes"]).hexdigest(),
                    claim=claim, claim_bytes=claim_bytes, mandatory_roots=roots)
                self.assertEqual(result["decision"], "REFUSE")
                claim_row = next(row for row in result["changed_paths"]
                                 if row["path"] == str(claim_path))
                observation = claim_row["claim_observation"]["current"]
                self.assertEqual(observation,
                                 policy._storage_smoke_claim_observation(fixture["smoke_id"]))
                if replacement_kind == "absent":
                    self.assertIsNone(claim_row["after"])
                    self.assertEqual(observation, {"sha256": None, "row": None})
                else:
                    expected_kind = "symlink" if replacement_kind == "symlink" else (
                        "directory" if replacement_kind == "directory" else "special")
                    self.assertEqual(claim_row["after"]["kind"], expected_kind)
                    self.assertEqual(observation["row"], claim_row["after"])
                if replacement_kind == "symlink":
                    self.assertEqual(observation["row"]["target"], str(target))

                parent_fd = policy._open_directory_nofollow(policy.SMOKE_CLAIM_ROOT)
                try:
                    artifact, _compressed = policy._publish_pre_admission_delta_sidecar(
                        result, parent_fd=parent_fd)
                finally:
                    os.close(parent_fd)
                manifest = {
                    "experiment_id": fixture["smoke_id"],
                    "storage_policy_smoke": True,
                    "decision": "REFUSE",
                    "gate0_attestation_sha256": hashlib.sha256(
                        fixture["attestation_bytes"]).hexdigest(),
                    "smoke_nonce_claim_path": str(claim_path),
                    "smoke_nonce_claim_sha256": hashlib.sha256(claim_bytes).hexdigest(),
                    "pre_admission_path_delta": artifact,
                    "pre_admission_path_delta_binding": artifact["binding"],
                }
                manifest["manifest_hmac_sha256"] = policy._manifest_signature(
                    manifest, create_key=True)
                verified = policy.read_storage_smoke_pre_admission_path_delta(manifest)
                verified_row = next(row for row in verified["changed_paths"]
                                    if row["path"] == str(claim_path))
                self.assertEqual(verified_row["claim_observation"]["current"], observation)

    def test_begin_experiment_publishes_symlink_claim_refusal_sidecar_before_reservation_fails(self) -> None:
        fixture = self._fixture(admit=False)
        target = fixture["repo"] / "claim-symlink-target"
        target.write_bytes(b"target must not be opened")
        original_capture = policy.capture_storage_tree_state
        replaced = False

        def capture_then_replace_claim(*args: Any, **kwargs: Any) -> Mapping[str, Any]:
            nonlocal replaced
            snapshot = original_capture(*args, **kwargs)
            if not replaced:
                fixture["claim_path"].unlink()
                fixture["claim_path"].symlink_to(target)
                replaced = True
            return snapshot

        with (mock.patch.object(policy, "__file__",
                                str(fixture["storage"] / "storage_policy.py")),
              mock.patch.object(policy, "_gate0_blockers", return_value=[]),
              mock.patch.object(policy, "capture_storage_tree_state",
                                side_effect=capture_then_replace_claim)):
            with self.assertRaises(BaseException):
                policy.begin_experiment(
                    experiment_id=fixture["smoke_id"],
                    purpose="Synthetic symlink claim sidecar ordering test",
                    candidate_hashes={"synthetic": "c" * 64},
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    storage_justification="Bounded synthetic claim replacement test.",
                    storage_policy_smoke=True,
                    output_dir=fixture["output_dir"],
                )

        self.assertTrue(replaced)
        artifact_path = policy._pre_admission_delta_artifact_path(fixture["smoke_id"])
        compressed = policy._read_file_nofollow(
            artifact_path, maximum_bytes=policy.SMOKE_PRE_ADMISSION_DELTA_MAX_COMPRESSED_BYTES)
        payload = json.loads(zlib.decompress(compressed))
        self.assertEqual(payload["decision"], "REFUSE")
        self.assertTrue(policy._verify_storage_evidence(
            payload, policy.SMOKE_PRE_ADMISSION_DELTA_HMAC_FIELD))
        claim_row = next(row for row in payload["changed_paths"]
                         if row["path"] == str(fixture["claim_path"]))
        self.assertEqual(claim_row["claim_observation"]["current"]["row"]["kind"], "symlink")
        self.assertEqual(claim_row["claim_observation"]["current"]["row"]["target"],
                         str(target))
        self.assertFalse(fixture["manifest_path"].exists())

    def test_pre_admission_delta_publication_failure_publishes_refuse_without_handoff(self) -> None:
        fixture = self._fixture(admit=False)
        unrelated = fixture["storage"] / "injected-before-smoke-admission.bin"
        unrelated.write_bytes(b"injected drift")
        with (mock.patch.object(policy, "__file__",
                                str(fixture["storage"] / "storage_policy.py")),
              mock.patch.object(policy, "_gate0_blockers", return_value=[]),
              mock.patch.object(policy, "_publish_pre_admission_delta_sidecar",
                                side_effect=OSError("injected diagnostic write failure"))):
            with self.assertRaisesRegex(RuntimeError, "storage gate refused experiment"):
                policy.begin_experiment(
                    experiment_id=fixture["smoke_id"],
                    purpose="Synthetic pre-admission delta publication failure test",
                    candidate_hashes={"synthetic": "c" * 64},
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    storage_justification="Bounded synthetic smoke admission test.",
                    storage_policy_smoke=True,
                    output_dir=fixture["output_dir"],
                )
        manifest = json.loads(fixture["manifest_path"].read_bytes())
        self.assertTrue(policy._verify_manifest_signature(manifest))
        self.assertEqual(manifest["decision"], "REFUSE")
        self.assertFalse(manifest.get("execution_authorized", False))
        self.assertEqual(manifest["pre_admission_path_delta_status"]["status"], "UNAVAILABLE")
        self.assertIn("publication failed closed", " ".join(manifest["blockers"]))
        self.assertNotIn(fixture["smoke_id"], policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS)

    def _prefix_lease(self, fixture: Mapping[str, Any]) -> policy.PrefixLease:
        return policy.PrefixLease(
            prefix=policy.PREFIX_ROOT / f"storage-smoke-{fixture['smoke_id']}",
            output_dir=fixture["output_dir"],
            experiment_id=fixture["smoke_id"],
            wine_server=fixture["runtime_root"] / "bin/wineserver",
            template=fixture["template"],
            manifest_path=fixture["manifest_path"],
        )

    def _materialized_validator_fixture(
            self, *, pre_capture_parent_drift: bool = False,
    ) -> tuple[dict[str, Any], Path, Path, dict[str, Any]]:
        fixture = self._fixture()
        prefix = policy.PREFIX_ROOT / f"storage-smoke-{fixture['smoke_id']}"
        if pre_capture_parent_drift:
            parent_mode = stat.S_IMODE(prefix.parent.stat().st_mode)
            prefix.parent.chmod(parent_mode ^ 0o020)
        shutil.copytree(fixture["template"], prefix,
                        ignore=shutil.ignore_patterns("FGMETAL_PREFIX_TEMPLATE.json"))
        prefix.chmod(0o700)
        active_path = policy.ACTIVE_LEASE_ROOT / f"{fixture['smoke_id']}.json"
        active_path.write_bytes(b"prepared")
        active_path.chmod(0o600)
        active_info = active_path.stat()
        lock_path = policy.ACTIVE_LEASE_ROOT / ".lease.lock"
        lock_path.write_bytes(b"")
        lock_path.chmod(0o600)
        lock_info = lock_path.stat()
        token = "a" * 64
        prefix_marker = {
            "experiment_id": fixture["smoke_id"], "pid": os.getpid(),
            "prefix_path": str(prefix), "output_dir": str(fixture["output_dir"]),
            "manifest_path": str(fixture["manifest_path"]),
            "manifest_sha256": hashlib.sha256(fixture["manifest_path"].read_bytes()).hexdigest(),
            "lease_token": token, "created_at_unix": time.time(),
        }
        policy._write_json(prefix / "FGMETAL_PREFIX_LEASE.json", prefix_marker)
        prefix_info = prefix.lstat()
        marker = {
            "experiment_id": fixture["smoke_id"], "pid": os.getpid(),
            "state": "MATERIALIZED", "prefix_path": str(prefix),
            "output_dir": str(fixture["output_dir"]),
            "manifest_path": str(fixture["manifest_path"]),
            "manifest_sha256": prefix_marker["manifest_sha256"],
            "lease_token": token, "updated_at_unix": time.time(),
        }
        inventory = policy._prefix_identity_inventory(prefix)
        inventory_commitment = policy._prefix_identity_inventory_fingerprint(inventory)
        gate_parent_state = policy.capture_storage_tree_state()
        gate_parent_row = next(row for row in gate_parent_state["entries"]
                               if row.get("path") == str(prefix.parent))
        parent_commitment = policy._gate_directory_metadata_commitment(gate_parent_row)
        gate_before_parent_row = next(row for row in fixture["before"]["entries"]
                                      if row.get("path") == str(prefix.parent))
        gate_before_parent_commitment = policy._gate_directory_metadata_commitment(
            gate_before_parent_row)
        temp_child_name = f".{prefix.name}.clone-{'c' * 32}"
        temp_child_commitment = dict(parent_commitment)
        temp_child_commitment["children_count"] = len(gate_before_parent_row["children"]) + 1
        temp_child_commitment["children_sha256"] = hashlib.sha256(json.dumps(
            sorted([*gate_before_parent_row["children"], temp_child_name]),
            ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
        parent_info = prefix.parent.stat()
        receipt = policy._seal_storage_evidence({
            "schema_version": 1, "experiment_id": fixture["smoke_id"],
            "pid": os.getpid(), "lease_token": token, "prefix_path": str(prefix),
            "prefix_device": prefix_info.st_dev, "prefix_inode": prefix_info.st_ino,
            "prefix_root_metadata": policy._prefix_root_metadata_commitment(prefix_info),
            "active_marker_path": str(active_path),
            "active_marker_parent_device": active_path.parent.stat().st_dev,
            "active_marker_parent_inode": active_path.parent.stat().st_ino,
            "active_marker_device": active_info.st_dev,
            "active_marker_inode": active_info.st_ino,
            "active_marker_gid": active_info.st_gid,
            "lease_lock_device": lock_info.st_dev, "lease_lock_inode": lock_info.st_ino,
            "lease_lock_size_bytes": lock_info.st_size,
            "lease_lock_allocated_bytes": getattr(lock_info, "st_blocks", 0) * 512,
            "prefix_parent_device": parent_info.st_dev, "prefix_parent_inode": parent_info.st_ino,
            "prefix_parent_pre_materialization_metadata": gate_before_parent_commitment,
            "prefix_parent_clone_temp_name": temp_child_name,
            "prefix_parent_temp_child_metadata": temp_child_commitment,
            "prefix_parent_temp_mkdir_started_ns": temp_child_commitment["mtime_ns"],
            "prefix_parent_temp_mkdir_finished_ns": temp_child_commitment["mtime_ns"],
            "prefix_parent_rename_started_ns": parent_commitment["mtime_ns"],
            "prefix_parent_rename_finished_ns": parent_commitment["mtime_ns"],
            "prefix_parent_post_materialization_metadata": parent_commitment,
            "output_dir": str(fixture["output_dir"]),
            "manifest_path": str(fixture["manifest_path"]),
            "manifest_sha256": prefix_marker["manifest_sha256"],
            "gate0_attestation_sha256": hashlib.sha256(fixture["attestation_bytes"]).hexdigest(),
            "materialized_prefix_allocated_bytes": policy._prefix_size(prefix)[
                "allocated_inode_deduplicated_bytes"],
            "prefix_identity_inventory_sha256": inventory_commitment["sha256"],
            "prefix_identity_inventory_entry_count": inventory_commitment["entry_count"],
            "expected_prefix_bytes": 3 * policy.GIB,
            "estimated_persistent_bytes": 32 * policy.MIB,
            "estimated_projected_new_disk_bytes": fixture["manifest"][
                "estimated_projected_new_disk_bytes"],
        }, "prefix_materialization_hmac_sha256")
        marker["materialization_receipt"] = receipt
        active_path.write_bytes(policy._canonical_pretty_json_bytes(marker))
        after = policy.capture_storage_tree_state()
        args = {
            "experiment_id": fixture["smoke_id"],
            "attestation_bytes": fixture["attestation_bytes"],
            "sidecar_bytes": fixture["sidecar_bytes"],
            "output_dir": fixture["output_dir"],
            "manifest_path": fixture["manifest_path"],
            "claim_path": fixture["claim_path"],
            "active_marker_bytes": active_path.read_bytes(),
            "materialization_receipt": receipt,
        }
        return {**fixture, "after": after}, prefix, lock_path, args

    def test_signed_baseline_nonce_allow_transition_and_pre_materialization_check(self) -> None:
        fixture = self._fixture()
        self.assertEqual(fixture["manifest"]["decision"], "ALLOW")
        self.assertTrue(policy._verify_manifest_signature(fixture["manifest"]))
        claim, claim_bytes = policy._read_storage_smoke_claim(fixture["smoke_id"])
        self.assertEqual(claim["preflight_attestation_sha256"],
                         hashlib.sha256(fixture["attestation_bytes"]).hexdigest())
        self.assertEqual(fixture["manifest"]["smoke_nonce_claim_sha256"],
                         hashlib.sha256(claim_bytes).hexdigest())
        self.assertEqual(fixture["manifest"]["gate0_attestation_sha256"],
                         hashlib.sha256(fixture["attestation_bytes"]).hexdigest())
        loaded = policy.read_gate_path_state_sidecar(
            json.loads(fixture["attestation_bytes"]))
        self.assertEqual(loaded["storage_tree_state"], fixture["before"])
        self.assertTrue(self._transition(fixture))

        lease = self._prefix_lease(fixture)
        with mock.patch.object(policy, "_gate0_blockers", wraps=policy._gate0_blockers) as gate_check, \
                mock.patch.object(policy.signal, "signal"):
            entered = lease.__enter__()
        self.addCleanup(entered._release_global_lease_gate)
        gate_check.assert_has_calls([
            mock.call(active_experiment_id=fixture["smoke_id"], allow_storage_smoke=True),
            mock.call(active_experiment_id=fixture["smoke_id"], allow_storage_smoke=True),
        ])
        self.assertIs(entered, lease)
        self.assertEqual(gate_check.call_count, 2)
        self.assertFalse(lease._materialized)
        self.assertFalse(lease.prefix.exists())

    def test_gate0_nonlease_scope_keeps_preprovisioned_smoke_claim_in_baseline(self) -> None:
        fixture = self._fixture()
        attestation = json.loads(fixture["attestation_bytes"])
        baseline_nonlease = attestation["measurements"][
            "fgmetal_scoped_observed_nonlease_allocated_inode_bytes"]
        observed_exclusions: list[tuple[Path, ...]] = []

        def measured_usage(**kwargs: Any) -> dict[str, int]:
            exclusions = tuple(Path(path) for path in kwargs["exclude_paths"])
            observed_exclusions.append(exclusions)
            self.assertTrue(kwargs["include_allocation_snapshot"])
            self.assertNotIn(fixture["claim_path"], exclusions)
            self.assertIn(fixture["manifest_path"], exclusions)
            self.assertIn(fixture["output_dir"], exclusions)
            return {
                "allocated_inode_deduplicated_bytes": baseline_nonlease,
                "allocated_inode_deduplicated_bytes_excluding_paths": baseline_nonlease,
            }

        with mock.patch.object(policy, "measure_project_usage", side_effect=measured_usage):
            blockers = policy._gate0_blockers(
                active_experiment_id=fixture["smoke_id"], allow_storage_smoke=True)

        self.assertEqual(len(observed_exclusions), 1)
        self.assertFalse(any("unrelated non-lease storage changed" in blocker
                             for blocker in blockers), blockers)
        self.assertFalse(any("storage path, inode, directory allocation, or metadata changed"
                             in blocker for blocker in blockers), blockers)

    def test_exact_materialized_state_transition_passes_and_materialization_drift_refuses(self) -> None:
        fixture = self._fixture()
        prefix = policy.PREFIX_ROOT / f"storage-smoke-{fixture['smoke_id']}"
        shutil.copytree(fixture["template"], prefix,
                        ignore=shutil.ignore_patterns("FGMETAL_PREFIX_TEMPLATE.json"))
        prefix.chmod(0o700)
        prefix_identity = prefix.lstat()
        manifest_sha = hashlib.sha256(fixture["manifest_path"].read_bytes()).hexdigest()
        active_path = policy.ACTIVE_LEASE_ROOT / f"{fixture['smoke_id']}.json"
        active_parent = active_path.parent.stat()
        active_path.write_bytes(b"prepared")
        active_path.chmod(0o600)
        active_identity = active_path.stat()
        lock_path = policy.ACTIVE_LEASE_ROOT / ".lease.lock"
        lock_path.write_bytes(b"")
        lock_path.chmod(0o600)
        lock_info = lock_path.stat()
        prefix_parent = prefix.parent.stat()
        token = "a" * 64
        prefix_marker = {
            "experiment_id": fixture["smoke_id"], "pid": os.getpid(),
            "prefix_path": str(prefix), "output_dir": str(fixture["output_dir"]),
            "manifest_path": str(fixture["manifest_path"]),
            "manifest_sha256": manifest_sha, "lease_token": token,
            "created_at_unix": time.time(),
        }
        policy._write_json(prefix / "FGMETAL_PREFIX_LEASE.json", prefix_marker)
        prefix_identity = prefix.lstat()
        gate_parent_state = policy.capture_storage_tree_state()
        gate_parent_row = next(row for row in gate_parent_state["entries"]
                               if row.get("path") == str(prefix.parent))
        gate_parent_commitment = policy._gate_directory_metadata_commitment(gate_parent_row)
        gate_before_parent_row = next(row for row in fixture["before"]["entries"]
                                      if row.get("path") == str(prefix.parent))
        gate_before_parent_commitment = policy._gate_directory_metadata_commitment(
            gate_before_parent_row)
        temp_child_name = f".{prefix.name}.clone-{'c' * 32}"
        temp_child_commitment = dict(gate_parent_commitment)
        temp_child_commitment["children_count"] = len(gate_before_parent_row["children"]) + 1
        temp_child_commitment["children_sha256"] = hashlib.sha256(json.dumps(
            sorted([*gate_before_parent_row["children"], temp_child_name]),
            ensure_ascii=True, separators=(",", ":")).encode("ascii")).hexdigest()
        marker = {
            "experiment_id": fixture["smoke_id"], "pid": os.getpid(),
            "state": "MATERIALIZED", "prefix_path": str(prefix),
            "output_dir": str(fixture["output_dir"]),
            "manifest_path": str(fixture["manifest_path"]),
            "manifest_sha256": manifest_sha, "lease_token": token,
            "updated_at_unix": time.time(),
        }
        prefix_inventory = policy._prefix_identity_inventory(prefix)
        prefix_inventory_commitment = policy._prefix_identity_inventory_fingerprint(prefix_inventory)
        receipt = policy._seal_storage_evidence({
            "schema_version": 1, "experiment_id": fixture["smoke_id"],
            "pid": os.getpid(), "lease_token": token, "prefix_path": str(prefix),
            "prefix_device": prefix_identity.st_dev, "prefix_inode": prefix_identity.st_ino,
            "prefix_root_metadata": policy._prefix_root_metadata_commitment(prefix_identity),
            "active_marker_path": str(active_path),
            "active_marker_parent_device": active_parent.st_dev,
            "active_marker_parent_inode": active_parent.st_ino,
            "active_marker_device": active_identity.st_dev,
            "active_marker_inode": active_identity.st_ino,
            "active_marker_gid": active_identity.st_gid,
            "lease_lock_device": lock_info.st_dev,
            "lease_lock_inode": lock_info.st_ino,
            "lease_lock_size_bytes": lock_info.st_size,
            "lease_lock_allocated_bytes": getattr(lock_info, "st_blocks", 0) * 512,
            "prefix_parent_device": prefix_parent.st_dev,
            "prefix_parent_inode": prefix_parent.st_ino,
            "prefix_parent_pre_materialization_metadata": gate_before_parent_commitment,
            "prefix_parent_clone_temp_name": temp_child_name,
            "prefix_parent_temp_child_metadata": temp_child_commitment,
            "prefix_parent_temp_mkdir_started_ns": temp_child_commitment["mtime_ns"],
            "prefix_parent_temp_mkdir_finished_ns": temp_child_commitment["mtime_ns"],
            "prefix_parent_rename_started_ns": gate_parent_commitment["mtime_ns"],
            "prefix_parent_rename_finished_ns": gate_parent_commitment["mtime_ns"],
            "prefix_parent_post_materialization_metadata": gate_parent_commitment,
            "output_dir": str(fixture["output_dir"]),
            "manifest_path": str(fixture["manifest_path"]),
            "manifest_sha256": manifest_sha,
            "gate0_attestation_sha256": hashlib.sha256(fixture["attestation_bytes"]).hexdigest(),
            "materialized_prefix_allocated_bytes": policy._prefix_size(prefix)["allocated_inode_deduplicated_bytes"],
            "prefix_identity_inventory_sha256": prefix_inventory_commitment["sha256"],
            "prefix_identity_inventory_entry_count": prefix_inventory_commitment["entry_count"],
            "expected_prefix_bytes": 3 * policy.GIB,
            "estimated_persistent_bytes": 32 * policy.MIB,
            "estimated_projected_new_disk_bytes": fixture["manifest"]["estimated_projected_new_disk_bytes"],
        }, "prefix_materialization_hmac_sha256")
        marker["materialization_receipt"] = receipt
        with active_path.open("wb") as stream:
            stream.write(policy._canonical_pretty_json_bytes(marker))
        after = policy.capture_storage_tree_state()
        args = {
            "experiment_id": fixture["smoke_id"],
            "attestation_bytes": fixture["attestation_bytes"],
            "sidecar_bytes": fixture["sidecar_bytes"],
            "output_dir": fixture["output_dir"],
            "manifest_path": fixture["manifest_path"],
            "claim_path": fixture["claim_path"],
            "active_marker_bytes": active_path.read_bytes(),
            "materialization_receipt": receipt,
        }
        self.assertTrue(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], after, **args))
        gate_blockers = policy._gate0_blockers(
            active_experiment_id=fixture["smoke_id"], allow_storage_smoke=True,
            active_prefix_receipt=receipt)
        self.assertFalse(any("storage path, inode, directory allocation, or metadata changed"
                             in blocker for blocker in gate_blockers), gate_blockers)

        # A materialized lease cannot hide unrelated non-lease allocation drift.
        allocation_drift = copy.deepcopy(after)
        unrelated_row = next(row for row in allocation_drift["entries"]
                             if row.get("path") == str(fixture["runner_path"]))
        unrelated_row["allocated_bytes"] = unrelated_row.get("allocated_bytes", 0) + 4096
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], allocation_drift, **args))

        wrong_run_marker = dict(marker, experiment_id="different-experiment")
        wrong_run_marker["materialization_receipt"] = receipt
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], after, **{**args,
                "active_marker_bytes": policy._canonical_pretty_json_bytes(wrong_run_marker)}))

        wrong_gate_receipt = dict(receipt, gate0_attestation_sha256="0" * 64)
        wrong_gate_receipt.pop("prefix_materialization_hmac_sha256", None)
        wrong_gate_receipt = policy._seal_storage_evidence(
            wrong_gate_receipt, "prefix_materialization_hmac_sha256")
        wrong_gate_marker = dict(marker, materialization_receipt=wrong_gate_receipt)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], after, **{**args,
                "active_marker_bytes": policy._canonical_pretty_json_bytes(wrong_gate_marker),
                "materialization_receipt": wrong_gate_receipt}))

        wrong_filesystem_receipt = dict(receipt, prefix_device=receipt["prefix_device"] + 1)
        wrong_filesystem_receipt.pop("prefix_materialization_hmac_sha256", None)
        wrong_filesystem_receipt = policy._seal_storage_evidence(
            wrong_filesystem_receipt, "prefix_materialization_hmac_sha256")
        wrong_filesystem_marker = dict(marker, materialization_receipt=wrong_filesystem_receipt)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], after, **{**args,
                "active_marker_bytes": policy._canonical_pretty_json_bytes(
                    wrong_filesystem_marker),
                "materialization_receipt": wrong_filesystem_receipt}))

        real_stat_nofollow = policy._stat_entry_nofollow
        def active_marker_gid_drift(path: Path):
            info = real_stat_nofollow(path)
            if info is not None and path == active_path:
                values = {field: getattr(info, field) for field in (
                    "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
                    "st_size", "st_mtime_ns", "st_ctime_ns", "st_blocks")
                    if hasattr(info, field)}
                values["st_gid"] = info.st_gid + 1
                return SimpleNamespace(**values)
            return info

        with mock.patch.object(policy, "_stat_entry_nofollow",
                               side_effect=active_marker_gid_drift):
            self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
                fixture["before"], after, **args))

        def active_marker_owner_drift(path: Path):
            info = real_stat_nofollow(path)
            if info is not None and path == active_path:
                values = {field: getattr(info, field) for field in (
                    "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
                    "st_size", "st_mtime_ns", "st_ctime_ns", "st_blocks")
                    if hasattr(info, field)}
                values["st_uid"] = os.getuid() + 1
                return SimpleNamespace(**values)
            return info

        with mock.patch.object(policy, "_stat_entry_nofollow",
                               side_effect=active_marker_owner_drift):
            self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
                fixture["before"], after, **args))

        def active_marker_mode_drift(path: Path):
            info = real_stat_nofollow(path)
            if info is not None and path == active_path:
                values = {field: getattr(info, field) for field in (
                    "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
                    "st_size", "st_mtime_ns", "st_ctime_ns", "st_blocks")
                    if hasattr(info, field)}
                values["st_mode"] = stat.S_IFREG | 0o644
                return SimpleNamespace(**values)
            return info

        with mock.patch.object(policy, "_stat_entry_nofollow",
                               side_effect=active_marker_mode_drift):
            self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
                fixture["before"], after, **args))

        def active_marker_device_drift(path: Path):
            info = real_stat_nofollow(path)
            if info is not None and path == active_path:
                values = {field: getattr(info, field) for field in (
                    "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_gid",
                    "st_size", "st_mtime_ns", "st_ctime_ns", "st_blocks")
                    if hasattr(info, field)}
                values["st_dev"] = info.st_dev + 1
                return SimpleNamespace(**values)
            return info

        with mock.patch.object(policy, "_stat_entry_nofollow",
                               side_effect=active_marker_device_drift):
            self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
                fixture["before"], after, **args))

        for bad_binding in (
                {"lease_token": "b" * 64},
                {"prefix_path": str(prefix.parent / "escaped-prefix")},
                {"prefix_path": str(prefix.parent / ".." / prefix.name)},
                {"manifest_sha256": "0" * 64}):
            bad_marker = dict(marker, **bad_binding)
            bad_marker["materialization_receipt"] = receipt
            self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
                fixture["before"], after, **{**args,
                    "active_marker_bytes": policy._canonical_pretty_json_bytes(bad_marker)}))

        over_reserved = dict(receipt)
        over_reserved["materialized_prefix_allocated_bytes"] = 4 * policy.GIB
        over_reserved.pop("prefix_materialization_hmac_sha256", None)
        over_reserved = policy._seal_storage_evidence(
            over_reserved, "prefix_materialization_hmac_sha256")
        over_reserved_marker = dict(marker, materialization_receipt=over_reserved)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], after, **{**args,
                "active_marker_bytes": policy._canonical_pretty_json_bytes(over_reserved_marker),
                "materialization_receipt": over_reserved}))

        # Byte-identical marker replacement must fail because the receipt binds
        # the inode captured before the in-place MATERIALIZED update.
        held_marker = active_path.with_suffix(".held")
        active_path.rename(held_marker)
        active_path.write_bytes(args["active_marker_bytes"])
        active_path.chmod(0o600)
        replaced_bytes = active_path.read_bytes()
        self.assertEqual(replaced_bytes, args["active_marker_bytes"])
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **{**args,
                "active_marker_bytes": replaced_bytes}))
        active_path.unlink()
        held_marker.rename(active_path)

        # A descendant with identical bytes and mode but a new inode is also
        # refused by the signed per-entry inventory.
        system_reg = prefix / "system.reg"
        saved_system_reg = prefix / "system.reg.held"
        system_reg.rename(saved_system_reg)
        system_reg.write_bytes(saved_system_reg.read_bytes())
        system_reg.chmod(0o644)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))
        system_reg.unlink()
        saved_system_reg.rename(system_reg)

        nested_file = prefix / "drive_c" / "nested.reg"
        saved_nested = nested_file.with_suffix(".held")
        nested_file.rename(saved_nested)
        nested_file.write_bytes(saved_nested.read_bytes())
        nested_file.chmod(0o644)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))
        nested_file.unlink()
        saved_nested.rename(nested_file)

        unexpected_parent_child = prefix.parent / (prefix.name + ".unexpected")
        unexpected_parent_child.write_text("unreserved sibling", encoding="utf-8")
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))
        unexpected_parent_child.unlink()

        wrong_active = dict(marker)
        wrong_active["state"] = "PREPARED"
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], after, **{**args,
                "active_marker_bytes": policy._canonical_pretty_json_bytes(wrong_active)}))
        with mock.patch.object(policy.os, "getuid", return_value=os.getuid() + 10000):
            self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
                fixture["before"], after, **args))

        prefix_marker_path = prefix / "FGMETAL_PREFIX_LEASE.json"
        for wrong_prefix_marker in (
                dict(prefix_marker, experiment_id="different-experiment"),
                dict(prefix_marker, lease_token="b" * 64)):
            policy._write_json(prefix_marker_path, wrong_prefix_marker)
            self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
                fixture["before"], policy.capture_storage_tree_state(), **args))
        policy._write_json(prefix_marker_path, prefix_marker)
        prefix_marker_path.chmod(0o644)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))
        prefix_marker_path.chmod(0o600)
        prefix_marker_path.unlink()
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))
        policy._write_json(prefix_marker_path, prefix_marker)

        (prefix / "system.reg").unlink()
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))
        shutil.copy2(fixture["template"] / "system.reg", prefix / "system.reg")

        (prefix / "unrelated-sibling").write_text("drift", encoding="utf-8")
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))
        (prefix / "unrelated-sibling").unlink()

        original_prefix = prefix.with_name(prefix.name + ".replaced")
        prefix.rename(original_prefix)
        shutil.copytree(fixture["template"], prefix,
                        ignore=shutil.ignore_patterns("FGMETAL_PREFIX_TEMPLATE.json"))
        prefix.chmod(0o700)
        policy._write_json(prefix / "FGMETAL_PREFIX_LEASE.json", prefix_marker)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))

        unrelated = fixture["repo"] / "experiments/unrelated-after-materialization.bin"
        unrelated.write_bytes(b"unexpected")
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))

    def test_production_materialize_assert_true_control_and_authenticated_cleanup(self) -> None:
        fixture = self._fixture()
        runner_path = fixture["runner_path"]
        manifest = json.loads(fixture["manifest_path"].read_bytes())
        manifest["authorized_runner_paths"] = [str(runner_path)]
        manifest["authorized_runner_sha256"] = {
            str(runner_path): hashlib.sha256(runner_path.read_bytes()).hexdigest()}
        manifest.pop("manifest_hmac_sha256", None)
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
        policy._write_json(fixture["manifest_path"], manifest)
        lease = self._prefix_lease(fixture)
        child_env = {
            "WINEPREFIX": str(lease.prefix),
            "WINELOADER": str(fixture["runtime_root"] / "bin/wine"),
            "WINESERVER": str(fixture["runtime_root"] / "bin/wineserver"),
        }
        with mock.patch.object(policy.signal, "signal"):
            lease.__enter__()
        self.assertFalse(lease._materialized)
        lease._runtime_env_verified = True
        lease._sealed_wine_env = dict(child_env)
        lease._env = dict(child_env)
        lease.materialize()
        self.assertTrue(lease._materialized)
        real_read_json_at = policy._read_json_at
        inherited_gid = 0 if os.getgid() != 0 else 1

        def read_with_setgid_inheritance(fd: int, name: str, *, maximum_bytes: int = 1024 * 1024):
            payload, raw, info = real_read_json_at(fd, name, maximum_bytes=maximum_bytes)
            if name == "FGMETAL_PREFIX_LEASE.json":
                values = {field: getattr(info, field) for field in (
                    "st_dev", "st_ino", "st_mode", "st_nlink", "st_uid", "st_size",
                    "st_mtime_ns", "st_ctime_ns", "st_blocks") if hasattr(info, field)}
                values["st_gid"] = inherited_gid
                info = SimpleNamespace(**values)
            return payload, raw, info

        # A setgid parent can give the private in-prefix marker a GID different
        # from the process primary GID; that group cannot read the 0600 file.
        with mock.patch.object(policy, "_read_json_at", side_effect=read_with_setgid_inheritance):
            assertion = policy.assert_prefix_lease(
                lease.prefix, child_env["WINELOADER"], runner_path, child_env)
        self.assertEqual(assertion["experiment_id"], fixture["smoke_id"])
        subprocess.run(["/usr/bin/true"], check=True)
        lease.__exit__(None, None, None)
        cleanup_path = fixture["output_dir"] / "prefix-cleanup.json"
        cleanup = json.loads(cleanup_path.read_bytes())
        self.assertTrue(policy._verify_storage_evidence(
            cleanup, "cleanup_receipt_hmac_sha256"))
        self.assertTrue(cleanup["prefix_created"])
        self.assertTrue(cleanup["prefix_deleted"])
        self.assertEqual(cleanup["storage_hygiene_status"], "PASS")
        self.assertFalse(lease.prefix.exists())
        self.assertEqual(len(list(fixture["claim_path"].parent.glob("*.json"))), 1)

    def test_production_materialize_refuses_gate_parent_metadata_drift_before_clone(self) -> None:
        for drift_key in ("mtime_ns", "size_bytes", "allocated_bytes"):
            with self.subTest(drift_key=drift_key):
                fixture = self._fixture()
                runner_path = fixture["runner_path"]
                manifest = json.loads(fixture["manifest_path"].read_bytes())
                manifest["authorized_runner_paths"] = [str(runner_path)]
                manifest["authorized_runner_sha256"] = {
                    str(runner_path): hashlib.sha256(runner_path.read_bytes()).hexdigest()}
                manifest.pop("manifest_hmac_sha256", None)
                manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
                policy._write_json(fixture["manifest_path"], manifest)
                lease = self._prefix_lease(fixture)
                child_env = {
                    "WINEPREFIX": str(lease.prefix),
                    "WINELOADER": str(fixture["runtime_root"] / "bin/wine"),
                    "WINESERVER": str(fixture["runtime_root"] / "bin/wineserver"),
                }
                with mock.patch.object(policy.signal, "signal"):
                    lease.__enter__()
                lease._runtime_env_verified = True
                lease._sealed_wine_env = dict(child_env)
                lease._env = dict(child_env)

                # Model a fresh retained-fd observation after a pre-clone touch,
                # directory-size change, or allocation change. State B is bypassed
                # only here so this State C fence itself is exercised.
                actual = policy._gate_directory_metadata_from_fd
                def drifted(fd: int) -> dict[str, Any]:
                    observed = actual(fd)
                    observed[drift_key] += 1_000_000 if drift_key == "mtime_ns" else 4096
                    return observed

                with mock.patch.object(policy, "_gate_directory_metadata_from_fd",
                                       side_effect=drifted):
                    with mock.patch.object(policy, "_gate0_blockers", return_value=[]):
                        with self.assertRaisesRegex(
                                RuntimeError, "parent metadata changed after Gate") as caught:
                            lease.materialize()
                lease.__exit__(RuntimeError, caught.exception, None)
                self.assertFalse(lease._materialized)
                self.assertFalse(lease.prefix.exists())
                self.assertFalse(lease._lease_marker_path.exists())
                cleanup = json.loads((fixture["output_dir"] / "prefix-cleanup.json").read_bytes())
                self.assertTrue(policy._verify_storage_evidence(
                    cleanup, "cleanup_receipt_hmac_sha256"))
                self.assertEqual(cleanup["status"], "FAIL")
                self.assertEqual(cleanup["storage_hygiene_status"], "FAIL_STORAGE_HYGIENE")
                self.assertEqual(len(list(fixture["claim_path"].parent.glob("*.json"))), 1)

    def test_production_materialize_refuses_parent_drift_after_rename_before_receipt(self) -> None:
        for drift_key in ("mtime_ns", "size_bytes", "allocated_bytes"):
            with self.subTest(drift_key=drift_key):
                fixture = self._fixture()
                runner_path = fixture["runner_path"]
                manifest = json.loads(fixture["manifest_path"].read_bytes())
                manifest["authorized_runner_paths"] = [str(runner_path)]
                manifest["authorized_runner_sha256"] = {
                    str(runner_path): hashlib.sha256(runner_path.read_bytes()).hexdigest()}
                manifest.pop("manifest_hmac_sha256", None)
                manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
                policy._write_json(fixture["manifest_path"], manifest)
                lease = self._prefix_lease(fixture)
                child_env = {
                    "WINEPREFIX": str(lease.prefix),
                    "WINELOADER": str(fixture["runtime_root"] / "bin/wine"),
                    "WINESERVER": str(fixture["runtime_root"] / "bin/wineserver"),
                }
                with mock.patch.object(policy.signal, "signal"):
                    lease.__enter__()
                lease._runtime_env_verified = True
                lease._sealed_wine_env = dict(child_env)
                lease._env = dict(child_env)

                real_snapshot = policy._gate_directory_metadata_from_fd
                snapshot_count = 0

                def inject_before_receipt(fd: int) -> dict[str, Any]:
                    nonlocal snapshot_count
                    snapshot_count += 1
                    observed = real_snapshot(fd)
                    # Calls 1-3 are Gate fence, post-mkdir, and post-copy/pre-rename.
                    # Call 4 is the receipt-time re-read after final rename.
                    if snapshot_count == 4:
                        observed[drift_key] += 1_000_000 if drift_key == "mtime_ns" else 4096
                    return observed

                with mock.patch.object(policy, "_gate_directory_metadata_from_fd",
                                       side_effect=inject_before_receipt):
                    with self.assertRaisesRegex(
                            RuntimeError, "changed after clone rename and before receipt capture") as caught:
                        lease.materialize()
                self.assertEqual(snapshot_count, 4)
                lease.__exit__(RuntimeError, caught.exception, None)
                self.assertFalse(lease.prefix.exists())
                self.assertFalse(lease._lease_marker_path.exists())
                cleanup = json.loads((fixture["output_dir"] / "prefix-cleanup.json").read_bytes())
                self.assertTrue(policy._verify_storage_evidence(
                    cleanup, "cleanup_receipt_hmac_sha256"))
                self.assertEqual(cleanup["status"], "FAIL")
                self.assertTrue(cleanup["prefix_created"])
                self.assertTrue(cleanup["prefix_deleted"])
                self.assertEqual(len(list(fixture["claim_path"].parent.glob("*.json"))), 1)

    def test_production_materialize_refuses_parent_drift_in_creation_and_rename_capture_windows(self) -> None:
        for window, target_call in (("after_mkdir_before_sample", 2),
                                    ("after_rename_before_sample", 5)):
            for drift_key in ("mtime_ns", "size_bytes", "allocated_bytes"):
                with self.subTest(window=window, drift_key=drift_key):
                    fixture = self._fixture()
                    runner_path = fixture["runner_path"]
                    manifest = json.loads(fixture["manifest_path"].read_bytes())
                    manifest["authorized_runner_paths"] = [str(runner_path)]
                    manifest["authorized_runner_sha256"] = {
                        str(runner_path): hashlib.sha256(runner_path.read_bytes()).hexdigest()}
                    manifest.pop("manifest_hmac_sha256", None)
                    manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
                    policy._write_json(fixture["manifest_path"], manifest)
                    lease = self._prefix_lease(fixture)
                    child_env = {
                        "WINEPREFIX": str(lease.prefix),
                        "WINELOADER": str(fixture["runtime_root"] / "bin/wine"),
                        "WINESERVER": str(fixture["runtime_root"] / "bin/wineserver"),
                    }
                    with mock.patch.object(policy.signal, "signal"):
                        lease.__enter__()
                    lease._runtime_env_verified = True
                    lease._sealed_wine_env = dict(child_env)
                    lease._env = dict(child_env)

                    real_row_snapshot = policy._gate_directory_row_from_fd
                    snapshot_count = 0

                    def inject_before_sample(fd: int) -> dict[str, Any]:
                        nonlocal snapshot_count
                        snapshot_count += 1
                        observed = real_row_snapshot(fd)
                        if snapshot_count == target_call:
                            observed[drift_key] += (
                                1_000_000 if drift_key == "mtime_ns" else 4096)
                        return observed

                    with mock.patch.object(policy, "_gate_directory_row_from_fd",
                                           side_effect=inject_before_sample):
                        with self.assertRaisesRegex(RuntimeError, "PrefixLease parent changed") as caught:
                            lease.materialize()
                    lease.__exit__(RuntimeError, caught.exception, None)
                    self.assertFalse(lease.prefix.exists())
                    cleanup = json.loads(
                        (fixture["output_dir"] / "prefix-cleanup.json").read_bytes())
                    self.assertTrue(policy._verify_storage_evidence(
                        cleanup, "cleanup_receipt_hmac_sha256"))
                    self.assertEqual(cleanup["status"], "FAIL")
                    self.assertEqual(cleanup["prefix_deleted"], window == "after_rename_before_sample")
                    if lease._lease_marker_path.exists():
                        self.assertEqual(json.loads(lease._lease_marker_path.read_bytes())["state"],
                                         "RECONCILIATION_REQUIRED")
                    self.assertEqual(len(list(fixture["claim_path"].parent.glob("*.json"))), 1)

    def test_parent_fd_snapshot_rejects_st_blocks_change_during_capture(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-parent-fd-") as temporary:
            directory = Path(temporary)
            fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                real = os.fstat(fd)
                fields = {
                    "st_dev": real.st_dev, "st_ino": real.st_ino,
                    "st_uid": real.st_uid, "st_gid": real.st_gid,
                    "st_mode": real.st_mode, "st_nlink": real.st_nlink,
                    "st_size": real.st_size, "st_blocks": getattr(real, "st_blocks", 0),
                    "st_mtime_ns": real.st_mtime_ns,
                    "st_ctime_ns": getattr(real, "st_ctime_ns", 0),
                }
                first = SimpleNamespace(**fields)
                changed_fields = dict(fields)
                changed_fields["st_blocks"] += 1
                second = SimpleNamespace(**changed_fields)
                with mock.patch.object(policy.os, "fstat", side_effect=(first, second)):
                    with self.assertRaisesRegex(OSError, "changed during metadata capture"):
                        policy._gate_directory_row_from_fd(fd)
            finally:
                os.close(fd)

    def test_materialized_prefix_parent_descendant_and_lock_metadata_drift_refuse(self) -> None:
        fixture, _prefix, _lock, args = self._materialized_validator_fixture(
            pre_capture_parent_drift=True)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], fixture["after"], **args))

        fixture, prefix, _lock, args = self._materialized_validator_fixture()
        self.assertTrue(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], fixture["after"], **args))
        prefix.chmod(0o711)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))

        fixture, prefix, _lock, args = self._materialized_validator_fixture()
        parent = prefix.parent
        parent_info = parent.stat()
        os.utime(parent, ns=(parent_info.st_atime_ns, parent_info.st_mtime_ns + 1_000_000))
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))

        fixture, prefix, _lock, args = self._materialized_validator_fixture()
        descendant = prefix / "system.reg"
        original_identity = descendant.stat().st_ino
        original_bytes = descendant.read_bytes()
        with descendant.open("r+b") as stream:
            stream.write(original_bytes)
            stream.flush()
            os.fsync(stream.fileno())
        self.assertEqual(descendant.stat().st_ino, original_identity)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))

        fixture, _prefix, lock_path, args = self._materialized_validator_fixture()
        original_lock_inode = lock_path.stat().st_ino
        with lock_path.open("ab") as stream:
            stream.write(b"growth")
            stream.flush()
            os.fsync(stream.fileno())
        self.assertEqual(lock_path.stat().st_ino, original_lock_inode)
        self.assertFalse(policy.evaluate_materialized_storage_smoke_transition(
            fixture["before"], policy.capture_storage_tree_state(), **args))

    def test_production_post_materialization_assertion_refusal_is_evidenced_then_cleaned(self) -> None:
        fixture = self._fixture()
        runner_path = fixture["runner_path"]
        manifest = json.loads(fixture["manifest_path"].read_bytes())
        manifest["authorized_runner_paths"] = [str(runner_path)]
        manifest["authorized_runner_sha256"] = {
            str(runner_path): hashlib.sha256(runner_path.read_bytes()).hexdigest()}
        manifest.pop("manifest_hmac_sha256", None)
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
        policy._write_json(fixture["manifest_path"], manifest)
        lease = self._prefix_lease(fixture)
        child_env = {
            "WINEPREFIX": str(lease.prefix),
            "WINELOADER": str(fixture["runtime_root"] / "bin/wine"),
            "WINESERVER": str(fixture["runtime_root"] / "bin/wineserver"),
        }
        with mock.patch.object(policy.signal, "signal"):
            lease.__enter__()
        lease._runtime_env_verified = True
        lease._sealed_wine_env = dict(child_env)
        lease._env = dict(child_env)
        lease.materialize()
        unrelated = fixture["repo"] / "experiments/unrelated-after-materialization.bin"
        unrelated.write_bytes(b"injected post-materialization drift")
        original_error = None
        lease._refusal_stage = "storage_smoke.assert_prefix_lease.post_materialization"
        try:
            policy.assert_prefix_lease(lease.prefix, child_env["WINELOADER"],
                                       runner_path, child_env)
        except RuntimeError as error:
            original_error = error
        self.assertIsNotNone(original_error)
        policy._record_prefixlease_gate_refusal(
            lease, [str(original_error)], original_error)
        refusal_path = fixture["output_dir"] / "prefixlease-refusal.json"
        refusal = json.loads(refusal_path.read_bytes())
        self.assertTrue(policy._verify_storage_evidence(
            refusal, "prefixlease_refusal_hmac_sha256"))
        self.assertEqual(refusal["decision"], "REFUSE")
        self.assertEqual(refusal["original_exception_message"], str(original_error)[:512])
        self.assertEqual(refusal["stage"],
                         "storage_smoke.assert_prefix_lease.post_materialization")
        self.assertIn(refusal["lifecycle_stage"], {
            refusal["stage"], "STATE_C_GATE_TREE_DECISION"})
        self.assertEqual(refusal["nonce_id"], fixture["smoke_id"])
        self.assertEqual(refusal["gate_identity"]["expected_attestation_sha256"],
                         hashlib.sha256(fixture["attestation_bytes"]).hexdigest())
        self.assertEqual(refusal["gate_identity"]["observed_attestation_sha256"],
                         hashlib.sha256(fixture["attestation_bytes"]).hexdigest())
        self.assertEqual(refusal["allow_identity"]["path"], str(fixture["manifest_path"]))
        self.assertEqual(refusal["allow_identity"]["sha256"],
                         hashlib.sha256(fixture["manifest_path"].read_bytes()).hexdigest())
        self.assertEqual(refusal["allow_identity"]["hmac_sha256"],
                         manifest["manifest_hmac_sha256"])
        self.assertEqual(refusal["allow_identity"]["decision"], "ALLOW")
        self.assertEqual(refusal["claim_identity"]["path"], str(fixture["claim_path"]))
        self.assertEqual(refusal["claim_identity"]["sha256"],
                         hashlib.sha256(fixture["claim_path"].read_bytes()).hexdigest())
        self.assertEqual(refusal["claim_identity"]["hmac_sha256"],
                         json.loads(fixture["claim_path"].read_bytes())["smoke_claim_hmac_sha256"])
        self.assertEqual(refusal["claim_identity"]["preflight_attestation_sha256"],
                         hashlib.sha256(fixture["attestation_bytes"]).hexdigest())
        self.assertEqual(refusal["nonce_identity"]["nonce_id"], fixture["smoke_id"])
        self.assertEqual(refusal["nonce_identity"]["claim_sha256"],
                         refusal["claim_identity"]["sha256"])
        self.assertEqual(refusal["prefix_identity"]["path"], str(lease.prefix))
        self.assertEqual(refusal["prefix_identity"]["device"], lease.prefix.stat().st_dev)
        self.assertEqual(refusal["prefix_identity"]["inode"], lease.prefix.stat().st_ino)
        self.assertEqual(refusal["active_marker_state"]["state"], "MATERIALIZED")
        self.assertEqual(refusal["active_marker_state"]["experiment_id"], fixture["smoke_id"])
        self.assertEqual(refusal["active_marker_state"]["prefix_path"], str(lease.prefix))
        self.assertTrue(refusal["active_marker_state"]["materialization_receipt_authenticated"])
        self.assertTrue(refusal["materialization_completed"])
        self.assertFalse(refusal["execution_authorized"])
        self.assertFalse(refusal["success_authorized"])
        self.assertEqual(refusal["success_status"], "NOT_AUTHORIZED")
        self.assertTrue(refusal["no_success"])
        self.assertTrue(refusal["diagnostic_write_failure_cannot_authorize_execution"])
        self.assertEqual(refusal["path_state_delta"]["status"], "CAPTURED")
        self.assertGreater(refusal["path_state_delta"]["count"], 0)
        self.assertEqual(len(refusal["path_state_delta"]["sha256"]), 64)
        self.assertLessEqual(len(refusal["path_state_delta"]["changed_paths"]), 8)
        delta_artifact = refusal["path_state_delta_artifact"]
        self.assertEqual(delta_artifact["status"], "CAPTURED")
        compressed_delta = Path(delta_artifact["path"]).read_bytes()
        self.assertEqual(hashlib.sha256(compressed_delta).hexdigest(),
                         delta_artifact["compressed_sha256"])
        complete_delta_bytes = policy.zlib.decompress(compressed_delta)
        self.assertEqual(hashlib.sha256(complete_delta_bytes).hexdigest(),
                         delta_artifact["uncompressed_sha256"])
        complete_delta = policy.read_prefixlease_refusal_path_delta(refusal)
        self.assertTrue(policy._verify_storage_evidence(
            complete_delta, "prefixlease_path_delta_hmac_sha256"))
        self.assertEqual(complete_delta["binding"], refusal["path_state_delta_binding"])
        for identity_name in ("gate_identity", "allow_identity", "claim_identity",
                              "prefix_identity", "active_marker_identity"):
            self.assertEqual(complete_delta["binding"][identity_name], refusal[identity_name]
                             if identity_name != "active_marker_identity"
                             else refusal["active_marker_state"])
        self.assertEqual(complete_delta["binding"]["nonce_id"], refusal["nonce_id"])
        self.assertEqual(delta_artifact["binding_sha256"], hashlib.sha256(
            policy._canonical_pretty_json_bytes(refusal["path_state_delta_binding"])).hexdigest())
        self.assertEqual(delta_artifact["hmac_sha256"],
                         complete_delta["prefixlease_path_delta_hmac_sha256"])
        self.assertEqual(delta_artifact["hmac_field"], "prefixlease_path_delta_hmac_sha256")
        self.assertEqual(complete_delta["path_state_changed_path_count"],
                         delta_artifact["path_state_changed_path_count"])
        self.assertEqual(complete_delta["path_state_changed_path_count"],
                         refusal["path_state_delta"]["count"])
        self.assertEqual(complete_delta["path_state_delta_sha256"],
                         delta_artifact["path_state_delta_sha256"])
        self.assertEqual(complete_delta["path_state_delta_sha256"],
                         refusal["path_state_delta"]["sha256"])
        rows_by_path = {row["path"]: row for row in complete_delta["changed_paths"]}
        prefix_row = rows_by_path[str(lease.prefix)]
        self.assertIsNone(prefix_row["before"])
        self.assertEqual(prefix_row["ownership_classification"], "CURRENT_RUN_PREFIX_ROOT")
        self.assertEqual(prefix_row["expected_classification"], "EXPECTED_MATERIALIZATION_ROOT")
        self.assertIsInstance(prefix_row["after"]["children"], list)
        self.assertIn("FGMETAL_PREFIX_LEASE.json", prefix_row["after"]["children"])
        parent_row = rows_by_path[str(lease.prefix.parent)]
        self.assertIsInstance(parent_row["before"]["children"], list)
        self.assertIsInstance(parent_row["after"]["children"], list)
        self.assertNotIn(lease.prefix.name, parent_row["before"]["children"])
        self.assertIn(lease.prefix.name, parent_row["after"]["children"])
        unrelated_row = rows_by_path[str(unrelated)]
        self.assertEqual(unrelated_row["ownership_classification"], "PROJECT_NONLEASE")
        self.assertEqual(unrelated_row["expected_classification"],
                         "UNEXPECTED_NONLEASE_DRIFT")
        self.assertFalse(refusal["execution_authorized"])
        lease.__exit__(RuntimeError, original_error, None)
        cleanup = json.loads((fixture["output_dir"] / "prefix-cleanup.json").read_bytes())
        self.assertTrue(policy._verify_storage_evidence(
            cleanup, "cleanup_receipt_hmac_sha256"))
        self.assertEqual(cleanup["status"], "FAIL")
        refusal_raw = refusal_path.read_bytes()
        self.assertFalse(cleanup["execution_authorized"])
        self.assertFalse(cleanup["success_authorized"])
        self.assertFalse(cleanup["accepted"])
        self.assertTrue(cleanup["failure_remains_failure"])
        self.assertTrue(cleanup["success_cannot_be_inferred_from_cleanup"])
        self.assertEqual(cleanup["prior_prefixlease_refusal"]["status"],
                         "AUTHENTICATED_REFUSAL")
        self.assertEqual(cleanup["prior_prefixlease_refusal"]["sha256"],
                         hashlib.sha256(refusal_raw).hexdigest())
        self.assertTrue(cleanup["prefix_deleted"])
        self.assertFalse(lease.prefix.exists())
        self.assertFalse((policy.ACTIVE_LEASE_ROOT / f"{fixture['smoke_id']}.json").exists())
        self.assertEqual(len(list(fixture["claim_path"].parent.glob("*.json"))), 1)
        delta_path = Path(delta_artifact["path"])
        intact_delta = delta_path.read_bytes()
        delta_path.write_bytes(intact_delta[:-1] + bytes([intact_delta[-1] ^ 0x01]))
        with self.assertRaisesRegex(ValueError, "compressed identity is invalid"):
            policy.read_prefixlease_refusal_path_delta(refusal)

    def test_refusal_diagnostic_publication_failure_cannot_authorize_or_hide_failure(self) -> None:
        fixture = self._fixture()
        runner_path = fixture["runner_path"]
        manifest = json.loads(fixture["manifest_path"].read_bytes())
        manifest["authorized_runner_paths"] = [str(runner_path)]
        manifest["authorized_runner_sha256"] = {
            str(runner_path): hashlib.sha256(runner_path.read_bytes()).hexdigest()}
        manifest.pop("manifest_hmac_sha256", None)
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
        policy._write_json(fixture["manifest_path"], manifest)
        lease = self._prefix_lease(fixture)
        child_env = {
            "WINEPREFIX": str(lease.prefix),
            "WINELOADER": str(fixture["runtime_root"] / "bin/wine"),
            "WINESERVER": str(fixture["runtime_root"] / "bin/wineserver"),
        }
        with mock.patch.object(policy.signal, "signal"):
            lease.__enter__()
        lease._runtime_env_verified = True
        lease._sealed_wine_env = dict(child_env)
        lease._env = dict(child_env)
        lease.materialize()
        unrelated = fixture["repo"] / "experiments/unrelated-after-materialization.bin"
        unrelated.write_bytes(b"injected post-materialization drift")
        with self.assertRaisesRegex(RuntimeError, "storage path, inode") as caught:
            policy.assert_prefix_lease(lease.prefix, child_env["WINELOADER"],
                                       runner_path, child_env)
        original_error = caught.exception
        real_write_binary = policy._write_binary_exclusive

        def fail_delta_publication(path: Path, contents: bytes, **kwargs: Any) -> None:
            if path.name == "prefixlease-refusal-path-delta.json.zlib":
                raise OSError("injected refusal-delta publication failure")
            real_write_binary(path, contents, **kwargs)

        with mock.patch.object(policy, "_write_binary_exclusive",
                               side_effect=fail_delta_publication):
            policy._record_prefixlease_gate_refusal(
                lease, [str(original_error)], original_error)

        refusal_path = fixture["output_dir"] / "prefixlease-refusal.json"
        refusal = json.loads(refusal_path.read_bytes())
        self.assertTrue(policy._verify_storage_evidence(
            refusal, "prefixlease_refusal_hmac_sha256"))
        self.assertEqual(refusal["decision"], "REFUSE")
        self.assertEqual(refusal["path_state_delta_artifact"]["status"], "UNAVAILABLE")
        self.assertIn("injected refusal-delta publication failure",
                      refusal["path_state_read_error"])
        self.assertFalse(refusal["execution_authorized"])
        self.assertFalse(refusal["success_authorized"])
        self.assertTrue(refusal["no_success"])

        self.assertFalse(lease.__exit__(RuntimeError, original_error, None))
        cleanup_path = fixture["output_dir"] / "prefix-cleanup.json"
        cleanup = json.loads(cleanup_path.read_bytes())
        self.assertTrue(policy._verify_storage_evidence(
            cleanup, "cleanup_receipt_hmac_sha256"))
        self.assertEqual(cleanup["status"], "FAIL")
        self.assertFalse(cleanup["execution_authorized"])
        self.assertFalse(cleanup["success_authorized"])
        self.assertFalse(cleanup["accepted"])
        self.assertTrue(cleanup["failure_remains_failure"])
        self.assertTrue(cleanup["success_cannot_be_inferred_from_cleanup"])
        self.assertEqual(cleanup["prior_prefixlease_refusal"]["status"],
                         "AUTHENTICATED_REFUSAL")
        self.assertTrue(cleanup["prefix_deleted"])
        self.assertFalse(lease.prefix.exists())
        self.assertFalse((policy.ACTIVE_LEASE_ROOT / f"{fixture['smoke_id']}.json").exists())
        self.assertEqual(len(list(fixture["claim_path"].parent.glob("*.json"))), 1)

    def test_unrelated_project_file_and_extra_protocol_sibling_refuse_transition(self) -> None:
        fixture = self._fixture()
        unrelated = fixture["repo"] / "experiments/unrelated.bin"
        unrelated.write_bytes(b"unrelated")
        fixture["after"] = policy.capture_storage_tree_state()
        self.assertFalse(self._transition(fixture))

    def test_output_directory_link_count_accepts_only_supported_filesystem_values(self) -> None:
        for link_count in (3, 5):
            with self.subTest(link_count=link_count):
                fixture = self._fixture()
                after = copy.deepcopy(fixture["after"])
                output_row = next(
                    row for row in after["entries"]
                    if row.get("path") == str(fixture["output_dir"])
                )
                output_row["link_count"] = link_count
                fixture["after"] = after
                self.assertFalse(self._transition(fixture))

        fixture = self._fixture()
        fixture["after"]["entries"].append(dict(fixture["after"]["entries"][0]))
        self.assertFalse(self._transition(fixture))

        fixture = self._fixture()
        (fixture["output_dir"] / "unexpected-protocol-sibling.json").write_text("{}\n")
        fixture["after"] = policy.capture_storage_tree_state()
        self.assertFalse(self._transition(fixture))

    def test_wrong_nonce_object_and_tampered_claim_are_rejected(self) -> None:
        fixture = self._fixture()
        claim, _ = policy._read_storage_smoke_claim(fixture["smoke_id"])
        claim.pop("smoke_claim_hmac_sha256")
        claim["experiment_id"] = "step11d3-storage-policy-smoke-wrong-20261024"
        claim["smoke_claim_hmac_sha256"] = policy._seal_storage_evidence(
            claim, "smoke_claim_hmac_sha256")["smoke_claim_hmac_sha256"]
        fixture["claim_path"].write_bytes(
            (json.dumps(claim, indent=2, sort_keys=True) + "\n").encode())
        self.assertFalse(self._transition(fixture))

        fixture = self._fixture()
        raw = fixture["claim_path"].read_bytes()
        fixture["claim_path"].write_bytes(raw.replace(b"PrefixLease smoke allocation",
                                                        b"XrefixLease smoke allocation"))
        self.assertFalse(self._transition(fixture))

    def test_tampered_manifest_gate_digest_and_gate_signature_are_rejected(self) -> None:
        fixture = self._fixture()
        fixture["manifest"]["decision"] = "REFUSE"
        fixture["manifest_path"].write_text(json.dumps(fixture["manifest"]))
        self.assertFalse(self._transition(fixture))

        fixture = self._fixture()
        manifest = json.loads(fixture["manifest_bytes"])
        manifest["gate0_attestation_sha256"] = "0" * 64
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
        fixture["manifest_path"].write_text(json.dumps(manifest))
        self.assertFalse(self._transition(fixture))

        fixture = self._fixture()
        bad_gate = fixture["attestation_bytes"].replace(b"PREFLIGHT_PASS", b"PREFLIGHT_FAIL")
        self.assertFalse(self._transition(fixture, attestation_bytes=bad_gate))

        fixture = self._fixture()
        self.assertFalse(self._transition(fixture, attestation_bytes=b"[]"))

    def test_missing_or_tampered_sidecar_copy_is_rejected(self) -> None:
        fixture = self._fixture()
        (fixture["output_dir"] / "gate0-path-state.json.zlib").unlink()
        self.assertFalse(self._transition(fixture))

        fixture = self._fixture()
        path = fixture["output_dir"] / "gate0-path-state.json.zlib"
        path.write_bytes(path.read_bytes() + b"tamper")
        self.assertFalse(self._transition(fixture))

    def test_storage_smoke_rejects_oversized_output_reservation(self) -> None:
        fixture = self._fixture(admit=False)
        with self.assertRaisesRegex(ValueError, "fixed storage smoke must use its bounded"):
            policy.begin_experiment(
                experiment_id=fixture["smoke_id"],
                purpose="oversized synthetic output reservation",
                candidate_hashes={},
                expected_duration_seconds=300,
                expected_prefix_bytes=3 * policy.GIB,
                expected_build_bytes=0,
                estimated_persistent_bytes=32 * policy.MIB + 1,
                storage_policy_smoke=True,
                output_dir=fixture["output_dir"],
            )

    def test_aggregate_reservation_counts_protocol_artifacts_and_refuses_projected_write(self) -> None:
        fixture = self._fixture()
        active_root = fixture["active_root"]
        marker = active_root / f"{fixture['smoke_id']}.json"
        marker.write_bytes(b'{"synthetic":"active marker"}\n')
        marker.chmod(0o600)
        refusal = fixture["output_dir"] / "prefixlease-refusal.json"
        refusal.write_bytes(b'{"synthetic":"signed refusal placeholder"}\n')
        refusal.chmod(0o600)
        (fixture["output_dir"] / "reservation-fill-a.bin").write_bytes(
            b"a" * (15 * policy.MIB))
        (fixture["output_dir"] / "reservation-fill-b.bin").write_bytes(
            b"b" * (15 * policy.MIB))
        for path in fixture["output_dir"].iterdir():
            if path.is_file():
                path.chmod(0o600)

        aggregate = policy._assert_storage_smoke_output_reservation(fixture["output_dir"])
        expected_inodes: dict[tuple[int, int], int] = {}
        for directory, dirnames, filenames in os.walk(fixture["output_dir"]):
            for path in [Path(directory), *(Path(directory) / name for name in dirnames),
                         *(Path(directory) / name for name in filenames)]:
                info = path.lstat()
                expected_inodes.setdefault(
                    (info.st_dev, info.st_ino), getattr(info, "st_blocks", 0) * 512)
        for path in (fixture["manifest_path"], fixture["claim_path"], marker):
            info = path.lstat()
            expected_inodes.setdefault(
                (info.st_dev, info.st_ino), getattr(info, "st_blocks", 0) * 512)
        parent_growth = policy._storage_smoke_parent_allocation_growth(
            fixture["smoke_id"], fixture["output_dir"])
        self.assertEqual(aggregate, sum(expected_inodes.values()) + parent_growth)
        self.assertGreaterEqual(aggregate, sum(
            marker.stat().st_blocks * 512 for _ in (0,)))
        self.assertGreater(aggregate, sum(
            path.stat().st_blocks * 512 for path in fixture["output_dir"].iterdir()
            if path.is_file()))

        projected_path = fixture["output_dir"] / "reservation-crossing.bin"
        crossing_bytes = policy.STORAGE_SMOKE_OUTPUT_RESERVATION_BYTES - aggregate + 1
        self.assertLessEqual(crossing_bytes, policy.STORAGE_SMOKE_OTHER_OUTPUT_OBJECT_MAX_BYTES)
        with self.assertRaisesRegex(RuntimeError, "32 MiB allocation reservation"):
            policy._assert_storage_smoke_output_reservation(
                fixture["output_dir"], projected_write_bytes=crossing_bytes,
                projected_path=projected_path)
        self.assertFalse(projected_path.exists())

    def test_output_root_mkdir_reservation_is_projection_only(self) -> None:
        fixture = self._fixture()
        output_dir = fixture["output_dir"]
        for child in output_dir.iterdir():
            child.unlink()
        output_dir.rmdir()
        self.assertFalse(output_dir.exists())
        with mock.patch.object(
                policy, "_storage_smoke_directory_growth_reserve",
                wraps=policy._storage_smoke_directory_growth_reserve) as growth_reserve:
            current = policy._assert_storage_smoke_output_reservation(
                output_dir, projected_directory_create=output_dir)
        self.assertGreater(current, 0)
        self.assertGreaterEqual(growth_reserve.call_count, 1)
        self.assertFalse(output_dir.exists())

    def test_gate_sidecar_reader_rejects_missing_tampered_and_oversized_payload(self) -> None:
        fixture = self._fixture()
        attestation = json.loads(fixture["attestation_bytes"])
        fixture["sidecar_path"].unlink()
        with self.assertRaises((FileNotFoundError, OSError)):
            policy.read_gate_path_state_sidecar(attestation)

        fixture = self._fixture()
        attestation = json.loads(fixture["attestation_bytes"])
        fixture["sidecar_path"].write_bytes(fixture["sidecar_bytes"] + b"bad")
        with self.assertRaisesRegex(RuntimeError, "sidecar filesystem identity|digest or authentication"):
            policy.read_gate_path_state_sidecar(attestation)

        fixture = self._fixture()
        attestation = json.loads(fixture["attestation_bytes"])
        descriptor = attestation["path_state_sidecar"]
        payload = json.dumps({"schema_version": 1, "storage_tree_state": {},
                              "nonlease_allocation_snapshot": {}},
                             sort_keys=True, separators=(",", ":")).encode()
        compressed = zlib.compress(payload)
        fixture["sidecar_path"].write_bytes(compressed)
        info = fixture["sidecar_path"].lstat()
        descriptor.update({
            "compressed_bytes": len(compressed),
            "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
            "compressed_hmac_sha256": hmac.new(
                policy._manifest_signing_key(), compressed, hashlib.sha256).hexdigest(),
            "uncompressed_bytes": len(payload),
            "uncompressed_sha256": hashlib.sha256(payload).hexdigest(),
            "allocated_bytes": getattr(info, "st_blocks", 0) * 512,
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": info.st_mode & 0o7777,
            "link_count": info.st_nlink,
            "owner_uid": info.st_uid,
            "owner_gid": info.st_gid,
            "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        })
        attestation["gate0_hmac_sha256"] = policy._gate0_signature(attestation)
        with mock.patch.object(policy, "GATE_PATH_STATE_SIDECAR_MAX_UNCOMPRESSED_BYTES", 16):
            with self.assertRaisesRegex(RuntimeError, "decompression bound"):
                policy.read_gate_path_state_sidecar(attestation)

    def test_gate_sidecar_reader_rejects_duplicate_json_keys(self) -> None:
        fixture = self._fixture()
        attestation = json.loads(fixture["attestation_bytes"])
        duplicate_key_payload = (
            b'{"schema_version":1,"schema_version":1,'
            b'"storage_tree_state":{"schema_version":1,"sha256":"' + b"0" * 64
            + b'","entries":[]},"nonlease_allocation_snapshot":'
            b'{"schema_version":1,"complete":true,"entry_count":0,'
            b'"entries_sha256":"' + hashlib.sha256(b"[]").hexdigest().encode()
            + b'","exclusion_paths_sha256":"' + b"1" * 64 + b'","entries":[]}}'
        )
        compressed = zlib.compress(duplicate_key_payload)
        fixture["sidecar_path"].write_bytes(compressed)
        info = fixture["sidecar_path"].lstat()
        descriptor = attestation["path_state_sidecar"]
        descriptor.update({
            "compressed_bytes": len(compressed),
            "compressed_sha256": hashlib.sha256(compressed).hexdigest(),
            "compressed_hmac_sha256": hmac.new(
                policy._manifest_signing_key(), compressed, hashlib.sha256).hexdigest(),
            "uncompressed_bytes": len(duplicate_key_payload),
            "uncompressed_sha256": hashlib.sha256(duplicate_key_payload).hexdigest(),
            "allocated_bytes": getattr(info, "st_blocks", 0) * 512,
            "device": info.st_dev,
            "inode": info.st_ino,
            "mode": info.st_mode & 0o7777,
            "link_count": info.st_nlink,
            "owner_uid": info.st_uid,
            "owner_gid": info.st_gid,
            "size_bytes": info.st_size,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        })
        with self.assertRaisesRegex((RuntimeError, ValueError), "duplicate"):
            policy.read_gate_path_state_sidecar(attestation)

    def test_gate_sidecar_writer_enforces_compressed_and_uncompressed_limits(self) -> None:
        fixture = self._fixture()
        tree = policy.capture_storage_tree_state()
        allocation = policy._make_nonlease_allocation_snapshot([], [])
        with mock.patch.object(policy, "GATE_PATH_STATE_SIDECAR_MAX_UNCOMPRESSED_BYTES", 1):
            with self.assertRaisesRegex(RuntimeError, "128 MiB uncompressed bound"):
                policy.write_gate_path_state_sidecar(tree, allocation)
        with mock.patch.object(policy, "GATE_PATH_STATE_SIDECAR_MAX_COMPRESSED_BYTES", 1):
            with self.assertRaisesRegex(RuntimeError, "compressed bound"):
                policy.write_gate_path_state_sidecar(tree, allocation)
        self.assertTrue(fixture["repo"].is_dir())

    def test_nonlease_snapshot_hardlink_aliases_are_deduplicated_and_duplicate_paths_refused(self) -> None:
        fixture = self._fixture()
        first = fixture["repo"] / "experiments/storage/hardlink-source.bin"
        alias = fixture["repo"] / "experiments/storage/hardlink-alias.bin"
        first.write_bytes(b"hardlink payload" * 128)
        os.link(first, alias)
        info = first.stat()
        common = {
            "root": str(fixture["repo"]),
            "classification": policy.ROOT_CLASS_MANDATORY,
            "category": "main_repo",
            "kind": "regular_file",
            "size_bytes": info.st_size,
            "allocated_bytes": getattr(info, "st_blocks", 0) * 512,
            "owner_uid": info.st_uid,
            "owner_gid": info.st_gid,
            "mode": info.st_mode & 0o7777,
            "link_count": info.st_nlink,
            "device": info.st_dev,
            "inode": info.st_ino,
            "mtime_ns": info.st_mtime_ns,
            "ctime_ns": info.st_ctime_ns,
        }
        entries = [dict(common, path=str(path)) for path in sorted((first, alias), key=str)]
        snapshot = policy._make_nonlease_allocation_snapshot(entries, [])
        self.assertEqual(policy._allocation_snapshot_total_bytes(snapshot), common["allocated_bytes"])
        duplicate_path_snapshot = policy._make_nonlease_allocation_snapshot(
            [entries[0], dict(entries[0])], [])
        with self.assertRaisesRegex(ValueError, "invalid path identity"):
            policy._allocation_snapshot_total_bytes(duplicate_path_snapshot)

    def test_prefixlease_locked_recheck_refusal_is_signed_and_bounded(self) -> None:
        fixture = self._fixture()
        lease = self._prefix_lease(fixture)
        with mock.patch.object(
                policy, "_gate0_blockers",
                side_effect=[[], ["synthetic Gate drift during allocation-lock recheck"]]) as gate_check, \
                mock.patch.object(policy.signal, "signal"):
            with self.assertRaisesRegex(RuntimeError, "Gate 0 changed while acquiring the shared allocation lock"):
                lease.__enter__()
        gate_check.assert_has_calls([
            mock.call(active_experiment_id=fixture["smoke_id"], allow_storage_smoke=True),
            mock.call(active_experiment_id=fixture["smoke_id"], allow_storage_smoke=True),
        ])
        refusal_path = fixture["output_dir"] / "prefixlease-refusal.json"
        refusal = json.loads(refusal_path.read_bytes())
        self.assertEqual(refusal["decision"], "REFUSE")
        self.assertEqual(refusal["stage"], "PrefixLease.__enter__.locked_gate_recheck")
        self.assertTrue(refusal["allow_then_refuse"])
        self.assertEqual(refusal["blockers"], [
            "RuntimeError: Gate 0 changed while acquiring the shared allocation lock: "
            "synthetic Gate drift during allocation-lock recheck"])
        self.assertFalse(lease.prefix.exists())
        self.assertTrue(refusal["no_prefix_materialized"])
        self.assertFalse(refusal["prefix_materialized_at_refusal"])
        self.assertFalse(refusal["prefix_path_exists_at_refusal"])
        self.assertEqual(refusal["sequence"], 1)
        self.assertEqual(refusal["allow_manifest_decision"], "ALLOW")
        self.assertEqual(refusal["manifest_sha256"],
                         hashlib.sha256(fixture["manifest_bytes"]).hexdigest())
        self.assertEqual(refusal["allow_manifest_hmac_sha256"],
                         fixture["manifest"]["manifest_hmac_sha256"])
        self.assertEqual(refusal["allow_reserved_projected_bytes"],
                         fixture["manifest"]["estimated_projected_new_disk_bytes"])
        self.assertEqual(refusal["allow_reservation_tuple"], {
            "expected_prefix_bytes": fixture["manifest"]["expected_prefix_bytes"],
            "expected_build_bytes": fixture["manifest"]["expected_build_bytes"],
            "estimated_persistent_bytes": fixture["manifest"]["estimated_persistent_bytes"],
            "estimated_projected_new_disk_bytes":
                fixture["manifest"]["estimated_projected_new_disk_bytes"],
        })
        self.assertEqual(refusal["pre_enter_reservation_status"], "SIGNED_ALLOW_VALIDATED")
        self.assertEqual(refusal["reservation_observation"]["stage"],
                         "unlocked_pre_materialization")
        self.assertTrue(refusal["reservation_observation"]["admitted"])
        reservation_result = refusal["reservation_result"]
        self.assertEqual(reservation_result["status"], "SIGNED_ALLOW_RESERVATION")
        self.assertEqual(reservation_result["pre_enter_status"], "SIGNED_ALLOW")
        self.assertEqual(reservation_result["expected_prefix_bytes"],
                         fixture["manifest"]["expected_prefix_bytes"])
        self.assertEqual(reservation_result["expected_build_bytes"],
                         fixture["manifest"]["expected_build_bytes"])
        self.assertEqual(reservation_result["estimated_persistent_bytes"],
                         fixture["manifest"]["estimated_persistent_bytes"])
        self.assertEqual(reservation_result["estimated_projected_new_disk_bytes"],
                         fixture["manifest"]["estimated_projected_new_disk_bytes"])
        self.assertGreaterEqual(len(reservation_result["admission_observations"]), 1)
        self.assertEqual(reservation_result["admission_observations"][-1],
                         refusal["reservation_observation"])
        self.assertEqual(refusal["consumed_nonce_claim_path"], str(fixture["claim_path"]))
        self.assertEqual(refusal["consumed_nonce_claim_sha256"],
                         hashlib.sha256(fixture["claim_path"].read_bytes()).hexdigest())
        self.assertEqual(refusal["consumed_nonce_claim_status"], "VERIFIED")
        self.assertIsNone(refusal["consumed_nonce_claim_read_error"])
        self.assertEqual(refusal["path_state_before_sha256"], fixture["before"]["sha256"])
        self.assertIsInstance(refusal["path_state_after_sha256"], str)
        self.assertIsInstance(refusal["path_state_changed_path_count"], int)
        delta = refusal["path_state_delta"]
        self.assertEqual(delta["status"], "CAPTURED")
        self.assertEqual(delta["count"], refusal["path_state_changed_path_count"])
        self.assertRegex(delta["sha256"], r"^[0-9a-f]{64}$")
        self.assertIsInstance(delta["truncated"], bool)
        self.assertEqual(delta["truncated_count"],
                         max(0, delta["count"] - len(delta["changed_paths"])))
        self.assertLessEqual(len(delta["changed_paths"]), delta["count"])
        self.assertLessEqual(len(json.dumps(refusal, sort_keys=True).encode()), 16 * 1024)
        self.assertIsNone(refusal["path_state_read_error"])
        self.assertTrue(policy._verify_storage_evidence(refusal, "prefixlease_refusal_hmac_sha256"))
        self.assertLessEqual(refusal_path.stat().st_size, 16 * 1024)

    def test_prefixlease_refusal_truncates_unbounded_gate_diagnostics(self) -> None:
        fixture = self._fixture()
        lease = self._prefix_lease(fixture)
        blockers = ["synthetic diagnostic " + ("x" * 1024) for _ in range(128)]
        with mock.patch.object(policy, "_gate0_blockers", return_value=blockers):
            with self.assertRaises(RuntimeError):
                lease.__enter__()
        refusal_path = fixture["output_dir"] / "prefixlease-refusal.json"
        refusal = json.loads(refusal_path.read_bytes())
        self.assertLessEqual(refusal_path.stat().st_size, 16 * 1024)
        self.assertTrue(refusal.get("blockers_truncated") or
                        len(refusal.get("blockers", [])) < len(blockers))
        self.assertTrue(policy._verify_storage_evidence(refusal, "prefixlease_refusal_hmac_sha256"))

    def test_begin_experiment_publication_failure_records_consumed_claim_and_removes_allow(self) -> None:
        fixture = self._fixture()
        manifest_path = fixture["manifest_path"]
        old_manifest = fixture["manifest"]
        fixture["claim_path"].unlink()
        manifest_path.unlink()
        shutil.rmtree(fixture["output_dir"])
        self._refresh_preflight_tree_after_fixture_reset(fixture)
        original_atomic_write = policy._atomic_write_bytes
        attempted_manifest_bytes: bytes | None = None

        def rename_then_raise(path: Path, contents: bytes, **kwargs: Any) -> None:
            nonlocal attempted_manifest_bytes
            original_atomic_write(path, contents, **kwargs)
            if path == manifest_path:
                attempted_manifest_bytes = contents
                raise OSError("injected post-rename directory durability ambiguity")

        with mock.patch.object(policy, "_atomic_write_bytes", side_effect=rename_then_raise), \
                mock.patch.object(policy, "_gate0_blockers", return_value=[]):
            with self.assertRaisesRegex(OSError, "durability ambiguity"):
                policy.begin_experiment(
                    experiment_id=fixture["smoke_id"],
                    purpose="synthetic publication failure after consumed nonce",
                    candidate_hashes=old_manifest["candidate_hashes"],
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    storage_policy_smoke=True,
                    output_dir=fixture["output_dir"],
                )
        self.assertFalse(manifest_path.exists(), "an unaccepted ALLOW must be removed")
        claim, claim_bytes = policy._read_storage_smoke_claim(fixture["smoke_id"])
        self.assertEqual(claim["experiment_id"], fixture["smoke_id"])
        self.assertTrue(claim_bytes)
        failure_path = policy._storage_smoke_publication_failure_path(fixture["smoke_id"])
        failure = json.loads(failure_path.read_bytes())
        self.assertTrue(policy._verify_storage_evidence(
            failure, "publication_failure_hmac_sha256"))
        self.assertEqual(failure["record_type"], "PREFIXLEASE_MANIFEST_PUBLICATION_FAILURE")
        self.assertEqual(failure["decision"], "REFUSE")
        self.assertFalse(failure["execution_authorized"])
        self.assertFalse(failure["success_authorized"])
        self.assertTrue(failure["no_allow_committed"])
        self.assertTrue(failure["no_prefix_materialized"])
        self.assertFalse(failure["retry_permitted"])
        self.assertEqual(failure["publication_failure"]["intended_decision"], "ALLOW")
        self.assertTrue(failure["publication_failure"]["signature_verified"])
        self.assertTrue(failure["publication_failure"]["manifest_file_observed_before_guarded_removal"])
        self.assertEqual(failure["publication_failure"]["canonical_manifest_sha256"],
                         hashlib.sha256(attempted_manifest_bytes or b"").hexdigest())
        self.assertEqual(failure["smoke_nonce_claim_sha256"], hashlib.sha256(claim_bytes).hexdigest())
        self.assertLessEqual(failure_path.stat().st_size, 16 * 1024)
        policy._write_json(manifest_path, old_manifest, strict_directory_sync=True)
        with self.assertRaisesRegex(RuntimeError, "terminal publication-failure evidence"):
            self._prefix_lease(fixture).__enter__()

    def test_publication_and_terminal_evidence_write_errors_are_both_preserved(self) -> None:
        fixture = self._fixture(admit=False)
        manifest_path = fixture["manifest_path"]
        failure_path = policy._storage_smoke_publication_failure_path(fixture["smoke_id"])
        runtime_identity = json.loads(
            (fixture["template"] / "FGMETAL_PREFIX_TEMPLATE.json").read_text()
        )["runtime_identity"]
        candidate_hashes = {
            "wine_runtime_tree": runtime_identity["wine_runtime_tree_sha256"],
            "wine_binary": runtime_identity["wine_binary_sha256"],
            "moltenvk": runtime_identity["moltenvk_sha256_for_subsequent_graphics_runs"],
        }
        original_atomic_write = policy._atomic_write_bytes

        def fail_both(path: Path, contents: bytes, **kwargs: Any) -> None:
            if path == manifest_path:
                original_atomic_write(path, contents, **kwargs)
                raise OSError("manifest rename durability error")
            if path == failure_path:
                raise OSError("terminal evidence write error")
            original_atomic_write(path, contents, **kwargs)

        with mock.patch.object(policy, "_atomic_write_bytes", side_effect=fail_both), \
                mock.patch.object(policy, "_gate0_blockers", return_value=[]):
            with self.assertRaises(BaseExceptionGroup) as caught:
                policy.begin_experiment(
                    experiment_id=fixture["smoke_id"],
                    purpose="synthetic dual publication failure",
                    candidate_hashes=candidate_hashes,
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    storage_policy_smoke=True,
                    output_dir=fixture["output_dir"],
                )
        rendered = repr(caught.exception)
        self.assertIn("manifest rename durability error", rendered)
        self.assertIn("terminal evidence write error", rendered)
        self.assertTrue(fixture["claim_path"].exists())
        self.assertFalse(manifest_path.exists())

    def test_leftover_allow_after_both_cleanup_failures_cannot_authorize_prefixlease(self) -> None:
        fixture = self._fixture(admit=False)
        manifest_path = fixture["manifest_path"]
        failure_path = policy._storage_smoke_publication_failure_path(fixture["smoke_id"])
        original_atomic_write = policy._atomic_write_bytes

        def publish_then_raise(path: Path, contents: bytes, **kwargs: Any) -> None:
            if path == manifest_path:
                original_atomic_write(path, contents, **kwargs)
                raise OSError("injected manifest read-back failure after rename")
            if path == failure_path:
                raise OSError("injected terminal-evidence failure")
            original_atomic_write(path, contents, **kwargs)

        with mock.patch.object(policy, "_atomic_write_bytes", side_effect=publish_then_raise), \
                mock.patch.object(policy, "_discard_unaccepted_smoke_manifest",
                                  side_effect=OSError("injected guarded-removal failure")), \
                mock.patch.object(policy, "_gate0_blockers", return_value=[]):
            with self.assertRaises(BaseExceptionGroup) as caught:
                policy.begin_experiment(
                    experiment_id=fixture["smoke_id"],
                    purpose="synthetic double-failure authorization fence",
                    candidate_hashes={"wine_runtime_tree": "c" * 64},
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    storage_policy_smoke=True,
                    output_dir=fixture["output_dir"],
                )
        self.assertIn("injected guarded-removal failure", repr(caught.exception))
        self.assertIn("injected terminal-evidence failure", repr(caught.exception))
        self.assertFalse(failure_path.exists())
        persisted = json.loads(manifest_path.read_bytes())
        self.assertEqual(persisted["decision"], "ALLOW")
        self.assertTrue(policy._verify_manifest_signature(persisted))
        self.assertNotIn(fixture["smoke_id"], policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS)
        prefix = policy.PREFIX_ROOT / f"storage-smoke-{fixture['smoke_id']}"
        with mock.patch.object(policy, "_record_prefixlease_gate_refusal", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "successful same-process publication read-back"):
                self._prefix_lease(fixture).__enter__()
        self.assertFalse(prefix.exists(), "an unaccepted leftover ALLOW must not materialize a prefix")

    def test_publication_handoff_cannot_be_consumed_by_another_process(self) -> None:
        experiment_id = "step11d3-storage-policy-smoke-20261025-0123456789abcdef"
        manifest_sha256 = "a" * 64
        parent_pid = os.getpid()
        policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS[experiment_id] = (
            parent_pid, manifest_sha256)

        with mock.patch.object(policy.os, "getpid", return_value=parent_pid + 1):
            self.assertFalse(policy._consume_storage_smoke_published_allow(
                experiment_id, manifest_sha256))

        self.assertNotIn(experiment_id, policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS)

        policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS[experiment_id] = (
            parent_pid, manifest_sha256)
        policy._reset_storage_smoke_publication_handoffs_after_fork()
        self.assertNotIn(experiment_id, policy._STORAGE_SMOKE_PUBLISHED_ALLOW_DIGESTS)

    def test_claim_postwrite_error_is_terminalized_without_retry(self) -> None:
        fixture = self._fixture(admit=False)
        runtime_identity = json.loads(
            (fixture["template"] / "FGMETAL_PREFIX_TEMPLATE.json").read_text()
        )["runtime_identity"]
        candidate_hashes = {
            "wine_runtime_tree": runtime_identity["wine_runtime_tree_sha256"],
            "wine_binary": runtime_identity["wine_binary_sha256"],
            "moltenvk": runtime_identity["moltenvk_sha256_for_subsequent_graphics_runs"],
        }
        original_claim_writer = policy._write_storage_smoke_claim

        def claim_then_raise(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], bytes]:
            original_claim_writer(*args, **kwargs)
            raise OSError("injected post-claim reservation verification error")

        with mock.patch.object(policy, "_write_storage_smoke_claim", side_effect=claim_then_raise), \
                mock.patch.object(policy, "_gate0_blockers", return_value=[]):
            with self.assertRaisesRegex(OSError, "post-claim reservation verification"):
                policy.begin_experiment(
                    experiment_id=fixture["smoke_id"],
                    purpose="synthetic claim post-write failure",
                    candidate_hashes=candidate_hashes,
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    storage_policy_smoke=True,
                    output_dir=fixture["output_dir"],
                )
        claim, claim_bytes = policy._read_storage_smoke_claim(fixture["smoke_id"])
        self.assertEqual(claim["experiment_id"], fixture["smoke_id"])
        failure = json.loads(policy._storage_smoke_publication_failure_path(
            fixture["smoke_id"]).read_bytes())
        self.assertTrue(policy._verify_storage_evidence(
            failure, "publication_failure_hmac_sha256"))
        self.assertEqual(failure["smoke_nonce_claim_sha256"], hashlib.sha256(claim_bytes).hexdigest())
        self.assertFalse(failure["success_authorized"])
        self.assertTrue(failure["no_allow_committed"])
        self.assertFalse(failure["publication_failure"]["signature_verified"])
        self.assertFalse(failure["publication_failure"]["manifest_file_observed_before_guarded_removal"])
        with self.assertRaises(FileExistsError):
            policy._claim_storage_smoke_nonce(fixture["smoke_id"], fixture["attestation_bytes"])

    def test_prefixlease_rejects_duplicate_and_noncanonical_persisted_manifest(self) -> None:
        fixture = self._fixture()
        path = fixture["manifest_path"]
        raw = path.read_bytes()
        path.write_bytes(b'{"decision":"ALLOW",' + raw[1:])
        lease = self._prefix_lease(fixture)
        with self.assertRaisesRegex(RuntimeError, "duplicate-key"):
            lease.__enter__()
        path.write_bytes(raw + b" ")
        with self.assertRaisesRegex(RuntimeError, "noncanonical"):
            self._prefix_lease(fixture).__enter__()

    def test_real_manifest_writer_accepts_only_authentic_exact_smoke_refusal(self) -> None:
        fixture = self._fixture()
        refusal = dict(fixture["manifest"])
        refusal.pop("manifest_hmac_sha256")
        refusal["decision"] = "REFUSE"
        refusal["blockers"] = ["synthetic signed refusal after nonce claim"]
        refusal["manifest_hmac_sha256"] = policy._manifest_signature(refusal)
        policy._write_json(fixture["manifest_path"], refusal, strict_directory_sync=True)
        stored = json.loads(fixture["manifest_path"].read_bytes())
        self.assertEqual(stored, refusal)
        self.assertTrue(policy._verify_manifest_signature(stored))
        invalid = dict(refusal)
        invalid["decision"] = "DENY"
        invalid.pop("manifest_hmac_sha256")
        invalid["manifest_hmac_sha256"] = policy._manifest_signature(invalid)
        with self.assertRaisesRegex(RuntimeError, "exact signed ALLOW or REFUSE"):
            policy._write_json(fixture["manifest_path"], invalid, strict_directory_sync=True)

    def test_begin_experiment_publishes_signed_refusal_after_nonce_consumption(self) -> None:
        fixture = self._fixture()
        fixture["manifest_path"].unlink()
        fixture["claim_path"].unlink()
        shutil.rmtree(fixture["output_dir"])
        self._refresh_preflight_tree_after_fixture_reset(fixture)
        with mock.patch.object(
                policy, "_gate0_blockers",
                return_value=["synthetic signed Gate refusal after nonce claim"]):
            with self.assertRaisesRegex(RuntimeError, "storage gate refused experiment"):
                policy.begin_experiment(
                    experiment_id=fixture["smoke_id"],
                    purpose="synthetic post-claim signed refusal",
                    candidate_hashes=fixture["manifest"]["candidate_hashes"],
                    expected_duration_seconds=300,
                    expected_prefix_bytes=3 * policy.GIB,
                    expected_build_bytes=0,
                    estimated_persistent_bytes=32 * policy.MIB,
                    storage_policy_smoke=True,
                    output_dir=fixture["output_dir"],
                )
        stored = json.loads(fixture["manifest_path"].read_bytes())
        self.assertEqual(stored["decision"], "REFUSE")
        self.assertEqual(stored["blockers"], [
            "synthetic signed Gate refusal after nonce claim"])
        self.assertTrue(policy._verify_manifest_signature(stored))
        claim, claim_bytes = policy._read_storage_smoke_claim(fixture["smoke_id"])
        self.assertEqual(stored["smoke_nonce_claim_sha256"], hashlib.sha256(claim_bytes).hexdigest())
        self.assertEqual(claim["experiment_id"], fixture["smoke_id"])

    def test_post_materialization_refusal_evidence_captures_complete_delta_digest(self) -> None:
        fixture = self._fixture()
        lease = self._prefix_lease(fixture)
        lease._manifest = fixture["manifest"]
        lease._manifest_sha256 = hashlib.sha256(fixture["manifest_path"].read_bytes()).hexdigest()
        lease._materialized = True
        lease._refusal_stage = "storage_smoke.assert_prefix_lease.post_materialization"
        lease.prefix.mkdir()
        (lease.prefix / "materialized-object").write_bytes(b"synthetic")
        original = RuntimeError("injected post-materialization assertion refusal")
        policy._record_prefixlease_gate_refusal(
            lease, [str(original)], original)
        refusal_path = fixture["output_dir"] / "prefixlease-refusal.json"
        refusal = json.loads(refusal_path.read_bytes())
        self.assertEqual(refusal["stage"], lease._refusal_stage)
        self.assertTrue(refusal["prefix_materialized_at_refusal"])
        self.assertFalse(refusal["execution_authorized"])
        self.assertEqual(refusal["original_exception_message"], str(original))
        delta = refusal["path_state_delta"]
        self.assertEqual(delta["status"], "CAPTURED")
        self.assertGreater(delta["count"], 0)
        self.assertRegex(delta["sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(delta["truncated_count"], max(0, delta["count"] - len(delta["changed_paths"])))
        self.assertTrue(policy._verify_storage_evidence(refusal, "prefixlease_refusal_hmac_sha256"))


class R91BuildLeaseWorkspaceCapacityTests(unittest.TestCase):
    def _workspace_fixture(self, temporary: str) -> tuple[Path, Path, Path, dict[str, object], int, int]:
        root = Path(temporary) / "project-cache"
        root.mkdir()
        image = root / build_retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME
        image.write_bytes(b"sparseimage backing allocation" * 64)
        workspace = root / build_retention.BUILD_SESSION_WORKSPACE_NAME
        workspace.mkdir()
        (workspace / "in-image-payload.bin").write_bytes(b"payload charged through its image")
        root_volume = policy._volume_identity(root)
        workspace_device = root_volume["device"] + 1
        workspace_fsid = root_volume["fsid"] + 1
        identity: dict[str, object] = {
            "image_path": str(image), "image_device": image.stat().st_dev,
            "image_inode": image.stat().st_ino, "mountpoint": str(workspace),
            "mountpoint_dir_device": workspace_device,
            "mountpoint_dir_inode": workspace.stat().st_ino,
            "mounted_mountpoint_device": workspace_device,
            "mounted_mountpoint_inode": workspace.stat().st_ino,
            "device_node": "/dev/disk-r91s1", "volume_uuid": "R91-EXPECTED-UUID",
            "device": workspace_device, "filesystem_id": workspace_fsid,
            "capacity_bytes": 64 * policy.MIB,
            "requested_capacity_bytes": 64 * policy.MIB,
            "filesystem_type": "hfs", "disk_device": "/dev/disk-r91",
            "hdiutil_path": "/usr/bin/hdiutil", "hdiutil_sha256": "a" * 64,
            "diskutil_path": "/usr/sbin/diskutil", "diskutil_sha256": "b" * 64,
            "attached": True,
        }
        return root, image, workspace, identity, workspace_device, workspace_fsid

    def _patch_capacity_roots(self, root: Path, workspace: Path, identity: dict[str, object], *,
                              observed_identity: dict[str, object] | None = None,
                              extra_transition: Path | None = None,
                              extra_identity: tuple[int, int] | None = None,
                              extra_mountpoint: Path | None = None):
        root_info = root.stat()
        discovered = policy.DiscoveredProjectRoot(
            path=root, classification=policy.ROOT_CLASS_MANDATORY, category="project_cache",
            expected_dev=root_info.st_dev, expected_ino=root_info.st_ino)
        original_identity = policy._entry_mount_identity_at
        observed = observed_identity or identity

        def entry_identity(parent_fd: int, name: str, *, directory: bool = True) -> tuple[int, int, int, str]:
            if name == workspace.name:
                return (int(observed["mounted_mountpoint_device"]),
                        int(observed["mounted_mountpoint_inode"]),
                        int(identity["filesystem_id"]),
                        str(workspace))
            if extra_transition is not None and name == extra_transition.name:
                assert extra_identity is not None
                path = root / name
                return (extra_identity[0], path.stat().st_ino, extra_identity[1],
                        str(extra_mountpoint or extra_transition))
            return original_identity(parent_fd, name, directory=directory)

        return (
            mock.patch.object(policy, "_classified_project_roots", return_value=[discovered]),
            mock.patch.object(policy, "_entry_mount_identity_at", side_effect=entry_identity),
            mock.patch.object(build_retention, "_verified_buildlease_capacity_workspace",
                              return_value=dict(identity)),
        )

    def test_exact_active_workspace_transition_is_accepted_and_backing_bytes_stay_scoped(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r91-capacity-", dir="/private/tmp") as temporary:
            root, image, workspace, identity, _device, _fsid = self._workspace_fixture(temporary)
            with ExitStack() as stack:
                for patcher in self._patch_capacity_roots(root, workspace, identity):
                    stack.enter_context(patcher)
                usage = policy.measure_project_usage(
                    strict=True, active_buildlease_id="r6a-r87-dual-lineage-r91-capacity")
            image_allocated = image.stat().st_blocks * 512
            self.assertGreater(image_allocated, 0)
            self.assertEqual(usage["allocated_inode_deduplicated_bytes"], image_allocated)
            self.assertEqual(usage["file_count"], 1)

    def test_unknown_workspace_or_second_mount_remains_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r91-capacity-", dir="/private/tmp") as temporary:
            root, _image, workspace, identity, _device, _fsid = self._workspace_fixture(temporary)
            with ExitStack() as stack:
                discovered = policy.DiscoveredProjectRoot(
                    root, policy.ROOT_CLASS_MANDATORY, "project_cache")
                stack.enter_context(mock.patch.object(
                    policy, "_classified_project_roots", return_value=[discovered]))
                original_identity = policy._entry_mount_identity_at

                def mounted_workspace(parent_fd: int, name: str, *, directory: bool = True) -> tuple[int, int, int, str]:
                    if name == workspace.name:
                        return (int(identity["mountpoint_dir_device"]), int(identity["mountpoint_dir_inode"]),
                                int(identity["filesystem_id"]),
                                str(workspace))
                    return original_identity(parent_fd, name, directory=directory)

                stack.enter_context(mock.patch.object(
                    policy, "_entry_mount_identity_at", side_effect=mounted_workspace))
                with self.assertRaisesRegex(OSError, "unbound nested mount"):
                    policy.measure_project_usage(strict=True)

            second = root / "unexpected-workspace"
            second.mkdir()
            with ExitStack() as stack:
                for patcher in self._patch_capacity_roots(
                        root, workspace, identity, extra_transition=second,
                        extra_identity=(int(identity["device"]) + 1,
                                        int(identity["filesystem_id"]) + 1)):
                    stack.enter_context(patcher)
                with self.assertRaisesRegex(OSError, "unbound nested mount"):
                    policy.measure_project_usage(
                        strict=True, active_buildlease_id="r6a-r87-dual-lineage-r91-capacity")

    def test_workspace_mountpoint_inode_must_match_the_persisted_identity(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r91-capacity-", dir="/private/tmp") as temporary:
            root, _image, workspace, identity, _device, _fsid = self._workspace_fixture(temporary)
            observed_identity = dict(identity)
            identity["mounted_mountpoint_inode"] = int(identity["mounted_mountpoint_inode"]) + 1
            with ExitStack() as stack:
                for patcher in self._patch_capacity_roots(
                        root, workspace, identity, observed_identity=observed_identity):
                    stack.enter_context(patcher)
                with self.assertRaisesRegex(OSError, "authenticated BuildLease workspace mount changed"):
                    policy.measure_project_usage(
                        strict=True, active_buildlease_id="r6a-r87-dual-lineage-r91-capacity")

    def test_sibling_mount_with_same_workspace_device_and_fsid_is_not_authorized(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r91-capacity-", dir="/private/tmp") as temporary:
            root, _image, workspace, identity, _device, _fsid = self._workspace_fixture(temporary)
            sibling = root / "same-lineage-unrelated-mount"
            sibling.mkdir()
            same_workspace_lineage = (int(identity["device"]), int(identity["filesystem_id"]))
            with ExitStack() as stack:
                for patcher in self._patch_capacity_roots(
                        root, workspace, identity, extra_transition=sibling,
                        extra_identity=same_workspace_lineage):
                    stack.enter_context(patcher)
                with self.assertRaisesRegex(OSError, "unbound nested mount"):
                    policy.measure_project_usage(
                        strict=True, active_buildlease_id="r6a-r87-dual-lineage-r91-capacity")

    def test_expected_workspace_plus_same_filesystem_unrelated_mount_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r91-capacity-", dir="/private/tmp") as temporary:
            root, _image, workspace, identity, _device, _fsid = self._workspace_fixture(temporary)
            unrelated = root / "unrelated-same-filesystem-mount"
            unrelated.mkdir()
            root_volume = policy._volume_identity(root)
            with ExitStack() as stack:
                for patcher in self._patch_capacity_roots(
                        root, workspace, identity, extra_transition=unrelated,
                        extra_identity=(int(root_volume["device"]), int(root_volume["fsid"])),
                        extra_mountpoint=unrelated):
                    stack.enter_context(patcher)
                with self.assertRaisesRegex(OSError, "unbound nested mount"):
                    policy.measure_project_usage(
                        strict=True, active_buildlease_id="r6a-r87-dual-lineage-r91-capacity")

    def test_wrong_mount_identity_and_inactive_authority_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r91-capacity-", dir="/private/tmp") as temporary:
            root, _image, workspace, identity, _device, _fsid = self._workspace_fixture(temporary)
            wrong_identity = dict(identity, volume_uuid="R91-WRONG-UUID")
            with ExitStack() as stack:
                for patcher in self._patch_capacity_roots(root, workspace, wrong_identity):
                    stack.enter_context(patcher)
                stack.enter_context(mock.patch.object(
                    build_retention, "_verified_buildlease_capacity_workspace",
                    side_effect=RuntimeError("persisted volume UUID differs from live mount")))
                with self.assertRaisesRegex(RuntimeError, "could not be authenticated"):
                    policy.measure_project_usage(
                        strict=True, active_buildlease_id="r6a-r87-dual-lineage-r91-capacity")

            with ExitStack() as stack:
                stack.enter_context(mock.patch.object(
                    policy, "_classified_project_roots", return_value=[
                        policy.DiscoveredProjectRoot(root, policy.ROOT_CLASS_MANDATORY, "project_cache")]))
                stack.enter_context(mock.patch.object(
                    build_retention, "_verified_buildlease_capacity_workspace",
                    side_effect=RuntimeError("lease is terminal")))
                with self.assertRaisesRegex(RuntimeError, "could not be authenticated"):
                    policy.measure_project_usage(
                        strict=True, active_buildlease_id="r6a-r87-dual-lineage-r91-capacity")


if __name__ == "__main__":
    unittest.main(verbosity=2)
