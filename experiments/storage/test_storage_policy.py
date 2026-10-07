#!/usr/bin/env python3
"""Synthetic PrefixLease cleanup-policy tests; these tests never launch Wine."""
from __future__ import annotations

import tempfile
import unittest
import json
import hashlib
import os
import signal
import struct
import subprocess
import time
import uuid
from pathlib import Path
from unittest import mock

import storage_policy as policy
import storage_gate0 as gate0
import build_retention


class PrefixLeasePolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-prefix-policy-test-", dir="/private/tmp")
        self.base = Path(self.temp.name)
        self.prefix_root = self.base / "prefixes"
        self.prefix_root.mkdir()
        (self.base / "Library/Caches/FGMetalStep11D-R").mkdir(parents=True)
        self.gate_attestation = self.base / "gate0-attestation.json"
        self.gate_attestation.write_text(json.dumps({
            "canonical_artifact_store_audit": {"sha256": "synthetic-cas-audit"}
        }), encoding="utf-8")
        self.addCleanup(self.temp.cleanup)
        patchers = [
            mock.patch.object(policy, "PREFIX_ROOT", self.prefix_root),
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

    def test_wine_launcher_accepts_certified_arch_exec(self) -> None:
        lease = self._wine_launch_lease()
        env = {"WINEPREFIX": str(lease.prefix)}
        process = mock.Mock()
        with mock.patch.object(policy, "assert_prefix_lease"), \
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
        with mock.patch.object(policy, "assert_prefix_lease"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "must execute the certified Wine binary"):
                lease.start_wine_process(
                    ["/bin/sh", "-c", "sleep 60 &", str(lease._wine_loader_path)],
                    env=env, runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_wine_launcher_rejects_process_control_overrides(self) -> None:
        lease = self._wine_launch_lease("wine-executable-override-rejected")
        env = {"WINEPREFIX": str(lease.prefix)}
        with mock.patch.object(policy, "assert_prefix_lease"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "unsupported process-control options: executable"):
                lease.start_wine_process(
                    [str(lease._wine_loader_path), "wineboot", "-u"], env=env,
                    runner_path=Path(__file__).resolve(), executable="/bin/sh")
        spawn.assert_not_called()

    def test_wine_launcher_rejects_environment_changes_after_sealing(self) -> None:
        lease = self._wine_launch_lease("wine-env-mismatch-rejected")
        env = {**lease._env, "DYLD_INSERT_LIBRARIES": "/tmp/host-child.dylib"}
        with mock.patch.object(policy, "assert_prefix_lease"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "forbidden dynamic-loader control"):
                lease.start_wine_process(
                    [str(lease._wine_loader_path), "wineboot", "-u"], env=env,
                    runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_wine_launcher_rejects_non_loader_startup_hooks(self) -> None:
        lease = self._wine_launch_lease("wine-startup-hook-rejected")
        env = {**lease._env, "LD_PRELOAD": "/tmp/host-child.dylib"}
        with mock.patch.object(policy, "assert_prefix_lease"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "forbidden process startup hook"):
                lease.start_wine_process(
                    [str(lease._wine_loader_path), "wineboot", "-u"], env=env,
                    runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_wine_launcher_rejects_python_site_startup_environment(self) -> None:
        lease = self._wine_launch_lease("wine-python-site-rejected")
        env = {**lease._env, "PYTHONUSERBASE": "/tmp/python-user-site"}
        with mock.patch.object(policy, "assert_prefix_lease"), \
                mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "forbidden process startup hook"):
                lease.start_wine_process(
                    [str(lease._wine_loader_path), "wineboot", "-u"], env=env,
                    runner_path=Path(__file__).resolve())
        spawn.assert_not_called()

    def test_wine_launcher_rejects_environment_mutation_even_if_not_a_loader_hook(self) -> None:
        lease = self._wine_launch_lease("wine-env-seal-rejected")
        env = {**lease._env, "LANG": "attacker-controlled"}
        with mock.patch.object(policy, "assert_prefix_lease"), \
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
        self.assertEqual(receipt["status"], "PASS")

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
        active_root.mkdir()
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
        active_root.mkdir()
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
        active_root.mkdir()
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
        marker_root.mkdir()
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
        marker_root.mkdir()
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
        marker_root.mkdir()
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
        marker_root.mkdir()
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
    def _preflight(self, path: Path, smoke_id: str, key_path: Path) -> None:
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
        self._preflight(attestation_path, smoke_id, key_path)
        with mock.patch.object(policy, "REPO", repo), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
                mock.patch.object(policy, "SMOKE_CLAIM_ROOT", claims), \
                mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path):
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
        prefix_root = base / "prefixes"
        prefix_root.mkdir()
        active_root = prefix_root / ".active-leases"
        active_root.mkdir()
        key_path = base / "signing-key"
        attestation_path = repo / "experiments/storage/storage-gate0-attestation.json"
        smoke_id = (f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}{time.strftime('%Y%m%d', time.gmtime())}-"
                    f"{uuid.uuid4().hex[:16]}")
        output = repo / "experiments/storage/smoke-runs" / smoke_id
        output.mkdir(parents=True)
        manifest_path = repo / "experiments/storage/manifests" / f"{smoke_id}.json"
        preflight_path = output / "gate0-preflight-attestation.json"
        self._preflight(attestation_path, smoke_id, key_path)
        preflight_bytes = attestation_path.read_bytes()
        preflight_path.write_bytes(preflight_bytes)
        with mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path):
            manifest = {
                "experiment_id": smoke_id, "storage_policy_smoke": True, "decision": "ALLOW",
                "manifest_path": str(manifest_path), "output_dir": str(output),
                "gate0_attestation_sha256": policy.hashlib.sha256(preflight_bytes).hexdigest(),
            }
            manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
            manifest_path.parent.mkdir(parents=True)
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
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
            marker = {
                "experiment_id": smoke_id, "pid": 99999999, "state": "RECONCILIATION_REQUIRED",
                "prefix_path": str(expected_prefix), "output_dir": str(output),
                "manifest_path": str(manifest_path),
                "manifest_sha256": policy.hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
            }
            marker_path = active_root / f"{smoke_id}.json"
            marker_path.write_text(json.dumps(marker), encoding="utf-8")

        with mock.patch.object(policy, "REPO", repo), \
                mock.patch.object(policy, "PREFIX_ROOT", prefix_root), \
                mock.patch.object(policy, "ACTIVE_LEASE_ROOT", active_root), \
                mock.patch.object(policy, "PREFIX_RECONCILIATION_ROOT", repo / "experiments/storage/prefix-lease-reconciliation"), \
                mock.patch.object(policy, "SMOKE_CLAIM_ROOT", repo / "experiments/storage/smoke-claims"), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
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
            mock.patch.object(policy, "ACTIVE_LEASE_ROOT", self.active_root),
            mock.patch.object(policy, "GATE0_ATTESTATION_PATH", self.gate_attestation),
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
                       native_architecture: str = "arm64") -> Path:
        attestation_bytes = self.gate_attestation.read_bytes()
        attestation_sha = hashlib.sha256(attestation_bytes).hexdigest()
        manifest_path = self.manifest_root / f"{exp_id}.json"
        manifest = {
            "experiment_id": exp_id,
            "decision": "ALLOW",
            "expected_prefix_bytes": expected_prefix_bytes,
            "expected_duration_seconds": 60,
            "estimated_persistent_bytes": 1024,
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
