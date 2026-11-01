#!/usr/bin/env python3
"""Synthetic PrefixLease WINEPREFIX contract regressions; never launches Wine."""
from __future__ import annotations

import hashlib
import errno
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import build_retention
import storage_policy as policy


class FakeWineProcess:
    def __init__(self, args: list[str], *, returncode: int | None = 0,
                 terminate_code: int = -15) -> None:
        self.args = args
        self.returncode = returncode
        self.terminate_code = terminate_code
        self.stdout = None

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout)
        return self.returncode

    def terminate(self) -> None:
        self.returncode = self.terminate_code

    def kill(self) -> None:
        self.returncode = -9

    def communicate(self, timeout: float | None = None) -> tuple[str, None]:
        return "synthetic wineserver shutdown\n", None


class PrefixLeaseWineprefixContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="fgmetal-prefix-contract-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.prefix_root = self.base / "prefixes with spaces"
        self.prefix_root.mkdir()
        self.prefix = self.prefix_root / "leased prefix with spaces"
        self.prefix.mkdir()
        (self.prefix / "system.reg").write_text("synthetic registry\n", encoding="utf-8")
        (self.prefix / "drive_c").mkdir()
        self.output = self.base / "output"
        self.output.mkdir()
        self.active_root = self.base / "active leases"
        self.active_root.mkdir()
        self.vm_capacity_root = self.base / "vm-capacity-domain"
        self.vm_capacity_root.mkdir(mode=0o700)
        self.vm_capacity_marker = self.vm_capacity_root / "active-session.json"
        self.manifest_parent = self.base / "manifests"
        self.manifest_parent.mkdir()
        self.manifest_path = self.manifest_parent / "wineprefix-contract.json"
        self.runtime = self.base / "runtime"
        self.wine = self.runtime / "bin/wine"
        self.wineserver = self.runtime / "bin/wineserver"
        self.wine.parent.mkdir(parents=True)
        self.wine.write_bytes(b"synthetic certified Wine executable\n")
        self.wine.chmod(0o755)
        self.wineserver.write_bytes(b"synthetic certified wineserver executable\n")
        self.wineserver.chmod(0o755)
        self.wine_sha256, self.wine_identity = policy._wine_loader_identity(self.wine)
        self.runtime_tree_sha256 = "a" * 64
        self.moltenvk_sha256 = "b" * 64
        self.template = self.base / "certified-template"
        self.template.mkdir()
        self.template_marker_path = self.template / "FGMETAL_PREFIX_TEMPLATE.json"
        self.gate_attestation_path = self.base / "gate0-attestation.json"
        self.gate_audit = {"sha256": "synthetic-canonical-artifact-audit"}
        self.gate_attestation_path.write_text(json.dumps({
            "canonical_artifact_store_audit": self.gate_audit,
        }), encoding="utf-8")
        self.gate_attestation_sha256 = hashlib.sha256(
            self.gate_attestation_path.read_bytes()).hexdigest()
        self.inventory_path = self.base / "storage-inventory.json"
        self.inventory_record = {"generated_at_unix": time.time()}
        self.inventory_path.write_text(json.dumps(self.inventory_record), encoding="utf-8")
        self.inventory_sha256 = hashlib.sha256(self.inventory_path.read_bytes()).hexdigest()
        self.experiment_id = "wineprefix-contract-test"
        self.template_marker = {
            "graphics_experiments_run": False,
            "runtime_identity": {
                "wine_runtime_root": str(self.runtime),
                "wine_binary_sha256": self.wine_sha256,
                "wine_runtime_tree_sha256": self.runtime_tree_sha256,
                "moltenvk_sha256_for_subsequent_graphics_runs": self.moltenvk_sha256,
            },
        }
        self.template_marker_path.write_text(json.dumps(self.template_marker), encoding="utf-8")
        self.runner_path = Path(__file__).resolve()
        runner_sha256 = hashlib.sha256(self.runner_path.read_bytes()).hexdigest()
        self.manifest = {
            "decision": "ALLOW",
            "experiment_id": self.experiment_id,
            "output_dir": str(self.output),
            "expected_prefix_requirement": "synthetic disposable Wine prefix",
            "estimated_persistent_bytes": 0,
            "estimated_max_new_disk_bytes": 0,
            "expected_prefix_bytes": policy.GIB,
            "expected_build_bytes": 0,
            "estimated_uncertainty_margin_bytes": policy.MIN_ADMISSION_UNCERTAINTY_MARGIN,
            "estimated_projected_new_disk_bytes": policy.GIB + policy.MIN_ADMISSION_UNCERTAINTY_MARGIN,
            "project_budget_bytes": policy.PROJECT_BUDGET,
            "gate0_attestation_sha256": self.gate_attestation_sha256,
            "candidate_hashes": {
                "wine_runtime_tree": self.runtime_tree_sha256,
                "wine_binary": self.wine_sha256,
                "moltenvk": self.moltenvk_sha256,
            },
            "authorized_runner_paths": [str(self.runner_path)],
            "authorized_runner_sha256": {str(self.runner_path): runner_sha256},
        }
        self.manifest_path.write_text(
            json.dumps(self.manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        manifest_bytes = self.manifest_path.read_bytes()
        self.manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        self._manifest_parent_fd = policy._open_directory_nofollow(self.manifest_parent)
        self.addCleanup(os.close, self._manifest_parent_fd)
        parent_info = os.fstat(self._manifest_parent_fd)
        self.manifest_parent_identity = (parent_info.st_dev, parent_info.st_ino)
        _bytes, self.manifest_file_identity = policy._read_file_from_directory_fd(
            self._manifest_parent_fd, self.manifest_path.name, display_path=self.manifest_path)

        self.patchers = [
            mock.patch.object(policy, "PREFIX_ROOT", self.prefix_root),
            mock.patch.object(policy, "ACTIVE_LEASE_ROOT", self.active_root),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ROOT", self.vm_capacity_root),
            mock.patch.object(policy, "VM_CAPACITY_DOMAIN_ACTIVE_MARKER", self.vm_capacity_marker),
            mock.patch.object(policy, "PREFIX_TEMPLATE", self.template),
            mock.patch.object(policy, "GATE0_ATTESTATION_PATH", self.gate_attestation_path),
            mock.patch.object(policy, "INVENTORY_PATH", self.inventory_path),
            mock.patch.object(policy, "_experiment_manifest_path",
                              side_effect=lambda experiment_id: self._manifest_for(experiment_id)),
            mock.patch.object(policy, "_measured_output_dir", side_effect=lambda value: Path(value)),
            mock.patch.object(policy, "_gate0_blockers", side_effect=self._gate0_pass),
            mock.patch.object(policy, "_verify_manifest_signature", return_value=True),
            mock.patch.object(policy, "_validated_manifest_estimates", return_value={
                "expected_prefix_bytes": policy.GIB,
                "expected_build_bytes": 0,
                "estimated_persistent_bytes": 0,
                "estimated_uncertainty_margin_bytes": policy.MIN_ADMISSION_UNCERTAINTY_MARGIN,
                "estimated_projected_new_disk_bytes": policy.GIB + policy.MIN_ADMISSION_UNCERTAINTY_MARGIN,
            }),
            mock.patch.object(policy, "disk_free_bytes", return_value=100 * policy.GIB),
            mock.patch.object(policy, "measure_project_usage", return_value={
                "allocated_inode_deduplicated_bytes": 0,
                "logical_bytes": 0,
                "file_count": 0,
            }),
            mock.patch.object(policy, "_storage_evidence_signature", return_value="synthetic-hmac"),
            mock.patch.object(policy, "_acquire_storage_allocation_lock", return_value=None),
            mock.patch.object(policy, "_release_storage_allocation_lock"),
            mock.patch.object(build_retention, "canonical_artifact_store_audit",
                              return_value=self.gate_audit),
        ]
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def _gate0_pass(**kwargs: object) -> list[str]:
        measurements = kwargs.get("measurements")
        if isinstance(measurements, dict):
            measurements.update({
                "free_bytes": 100 * policy.GIB,
                "project_usage": {"allocated_inode_deduplicated_bytes": 0},
                "conservative_project_bytes": 0,
                "nonlease_project_bytes": 0,
            })
        return []

    def _manifest_for(self, experiment_id: str) -> Path:
        if experiment_id != self.experiment_id:
            raise ValueError(f"unexpected synthetic experiment ID: {experiment_id}")
        return self.manifest_path

    def _lease(self, *, include_winearch: bool = True) -> policy.PrefixLease:
        lease = policy.PrefixLease(
            self.prefix, self.output, self.experiment_id, self.wineserver,
            template=self.template, manifest_path=self.manifest_path)
        lease._created = True
        lease._materialized = True
        lease._clone_process = None
        lease._prefix_created_any = True
        lease._leased_tree_absence_proven = False
        lease._storage_violation = False
        lease._unsafe_live_process = False
        lease._prefix_parent_fd = policy._open_directory_nofollow(self.prefix_root)
        self.addCleanup(lambda selected=lease: os.close(selected._prefix_parent_fd)
                        if selected._prefix_parent_fd is not None else None)
        prefix_info = self.prefix.stat(follow_symlinks=False)
        lease._prefix_identity = (prefix_info.st_dev, prefix_info.st_ino)
        lease._manifest = dict(self.manifest)
        lease._manifest_sha256 = self.manifest_sha256
        lease._manifest_parent_identity = self.manifest_parent_identity
        lease._manifest_file_identity = self.manifest_file_identity
        lease._free_before = 100 * policy.GIB
        lease._project_before = {"allocated_inode_deduplicated_bytes": 0}
        lease._release_global_lease_gate = lambda: None
        lease._wine_loader_path = self.wine
        lease._wine_loader_sha256 = self.wine_sha256
        lease._wine_loader_identity = self.wine_identity
        lease._runtime_env_verified = True
        lease._template_marker = self.template_marker
        lease._env = {
            "WINEPREFIX": str(self.prefix),
            "WINELOADER": str(self.wine),
            "WINESERVER": str(self.wineserver),
            "WINEARCH": "win64",
        }
        if not include_winearch:
            lease._env.pop("WINEARCH")
        lease._sealed_wine_env = dict(lease._env)
        lease._lease_marker_path = self.active_root / f"{self.experiment_id}.json"
        lease._lease_token = "c" * 64
        active_root_fd = policy._open_directory_nofollow(self.active_root)
        active_fd = os.open(
            lease._lease_marker_path.name,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600, dir_fd=active_root_fd)
        active_parent_info = os.fstat(active_root_fd)
        os.close(active_root_fd)
        def close_active_marker() -> None:
            try:
                os.close(active_fd)
            except OSError as error:
                if error.errno != errno.EBADF:
                    raise
        self.addCleanup(close_active_marker)
        lease._active_marker_fd = active_fd
        active_info = os.fstat(active_fd)
        lease._active_marker_identity = (active_info.st_dev, active_info.st_ino)
        lease._lease_marker_parent_identity = (active_parent_info.st_dev, active_parent_info.st_ino)
        binding = {
            "experiment_id": self.experiment_id,
            "pid": os.getpid(),
            "prefix_path": str(self.prefix),
            "output_dir": str(self.output),
            "manifest_path": str(self.manifest_path),
            "manifest_sha256": self.manifest_sha256,
            "lease_token": lease._lease_token,
        }
        prefix_bytes = policy._prefix_size(self.prefix)["allocated_inode_deduplicated_bytes"]
        output_bytes = policy._prefix_size(self.output)["allocated_inode_deduplicated_bytes"]
        lease._materialization_receipt = policy._seal_storage_evidence({
            "schema_version": 1,
            **binding,
            "prefix_device": prefix_info.st_dev,
            "prefix_inode": prefix_info.st_ino,
            "active_marker_path": str(lease._lease_marker_path),
            "active_marker_parent_device": active_parent_info.st_dev,
            "active_marker_parent_inode": active_parent_info.st_ino,
            "active_marker_device": active_info.st_dev,
            "active_marker_inode": active_info.st_ino,
            "active_marker_gid": active_info.st_gid,
            "gate0_attestation_sha256": self.gate_attestation_sha256,
            "inventory_sha256": self.inventory_sha256,
            "inventory_generated_at_unix": self.inventory_record["generated_at_unix"],
            "gate0_attested_observed_scope_bytes": 0,
            "gate0_attested_nonlease_scope_bytes": 0,
            "pre_materialization_free_bytes": 100 * policy.GIB,
            "post_materialization_free_bytes": 100 * policy.GIB,
            "pre_materialization_nonlease_scope_bytes": 0,
            "post_materialization_nonlease_scope_bytes": 0,
            "pre_materialization_output_allocated_bytes": output_bytes,
            "post_materialization_output_allocated_bytes": output_bytes,
            "materialized_prefix_allocated_bytes": prefix_bytes,
            "expected_prefix_bytes": policy.GIB,
            "estimated_persistent_bytes": 0,
            "estimated_projected_new_disk_bytes": policy.GIB + policy.MIN_ADMISSION_UNCERTAINTY_MARGIN,
            "post_materialization_scope_bytes": prefix_bytes,
            "created_at_unix": time.time(),
        }, "prefix_materialization_hmac_sha256")
        policy._write_json(self.prefix / "FGMETAL_PREFIX_LEASE.json", {
            **binding,
            "created_at_unix": 1.0,
        })
        lease._write_active_lease("MATERIALIZED")
        return lease

    def _start(self, lease: policy.PrefixLease, process: FakeWineProcess) -> mock.Mock:
        with mock.patch.object(policy, "_spawn_in_directory_fd", return_value=process) as spawn:
            lease.start_wine_process(
                [str(self.wine), "--synthetic-test"], env=lease._env,
                runner_path=self.runner_path)
        self.assertEqual(spawn.call_args.kwargs["env"]["WINEPREFIX"], str(self.prefix))
        self.assertEqual(spawn.call_args.kwargs["expected_child_identity"],
                         (self.prefix.name, *lease._prefix_identity, str(self.prefix)))
        return spawn

    def _complete_lifecycle(self, lease: policy.PrefixLease, wine_process: FakeWineProcess,
                            wine_error: BaseException | None = None) -> dict[str, object]:
        stop_processes = [FakeWineProcess(["wineserver", flag]) for flag in ("-k", "-w")]
        with mock.patch.object(policy, "_spawn_in_directory_fd",
                               side_effect=[wine_process, *stop_processes]) as spawn:
            started = lease.start_wine_process(
                [str(self.wine), "--synthetic-test"], env=lease._env,
                runner_path=self.runner_path)
            if wine_error is None:
                status = lease.wait_process(started, timeout_seconds=2)
                self.assertEqual(status, wine_process.returncode)
                exc_type = exc = None
            elif isinstance(wine_error, subprocess.TimeoutExpired):
                with self.assertRaises(subprocess.TimeoutExpired):
                    lease.wait_process(started, timeout_seconds=0)
                exc_type, exc = type(wine_error), wine_error
            else:
                status = lease.wait_process(started, timeout_seconds=2)
                self.assertNotEqual(status, 0)
                exc_type, exc = type(wine_error), wine_error
            self.assertFalse(lease.__exit__(exc_type, exc, None))
        receipt = json.loads((self.output / "prefix-cleanup.json").read_text(encoding="utf-8"))
        self.assertEqual(len(spawn.call_args_list), 3)
        for shutdown_call in spawn.call_args_list[1:]:
            self.assertEqual(shutdown_call.kwargs["env"]["WINEPREFIX"], str(self.prefix))
        return receipt

    def test_constructor_rejects_relative_and_parent_escape_prefix_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical absolute path"):
            policy.PrefixLease(Path("relative-prefix"), self.output, self.experiment_id,
                               self.wineserver, template=self.template)
        with self.assertRaisesRegex(ValueError, "canonical absolute path"):
            escaped = self.prefix_root / ".." / self.prefix.name
            policy.PrefixLease(escaped, self.output, self.experiment_id,
                               self.wineserver, template=self.template)

    def test_space_containing_canonical_prefix_is_used_for_launch_and_cleanup(self) -> None:
        lease = self._lease()
        wine_process = FakeWineProcess([str(self.wine), "--synthetic-test"])
        self._start(lease, wine_process)
        stop_processes = [FakeWineProcess(["wineserver", flag]) for flag in ("-k", "-w")]
        with mock.patch.object(policy, "_spawn_in_directory_fd", side_effect=stop_processes) as spawn:
            good, records = lease._stop_wineserver()
        self.assertTrue(good)
        self.assertEqual([record["WINEPREFIX"] for record in records], [str(self.prefix)] * 2)
        self.assertTrue(all(record["prefix_identity"]["path"] == str(self.prefix)
                            for record in records))
        self.assertEqual([call.kwargs["env"]["WINEPREFIX"] for call in spawn.call_args_list],
                         [str(self.prefix)] * 2)
        self.assertEqual([call.kwargs["expected_child_identity"] for call in spawn.call_args_list],
                         [(self.prefix.name, *lease._prefix_identity, str(self.prefix))] * 2)

    def test_cleanup_preserves_entire_sealed_environment_without_adding_winearch(self) -> None:
        lease = self._lease(include_winearch=False)
        launch = self._start(lease, FakeWineProcess([str(self.wine), "--synthetic-test"]))
        with mock.patch.object(policy, "_spawn_in_directory_fd", side_effect=[
                FakeWineProcess(["wineserver", "-k"]),
                FakeWineProcess(["wineserver", "-w"])]) as shutdown:
            good, _ = lease._stop_wineserver()
        self.assertTrue(good)
        for call in shutdown.call_args_list:
            self.assertEqual(call.kwargs["env"], launch.call_args.kwargs["env"])
            self.assertNotIn("WINEARCH", call.kwargs["env"])

    def test_relative_basename_other_alias_and_parent_escape_wineprefixes_fail(self) -> None:
        lease = self._lease()
        alias = self.base / "prefix alias"
        alias.symlink_to(self.prefix, target_is_directory=True)
        invalid_values = {
            "relative": "relative-prefix",
            "basename": self.prefix.name,
            "different absolute": str(self.base / "another prefix"),
            "symlink alias": str(alias),
            "parent escape": str(self.prefix_root / ".." / self.prefix.name),
        }
        for name, value in invalid_values.items():
            with self.subTest(name=name):
                env = {**lease._env, "WINEPREFIX": value}
                with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
                    with self.assertRaises(ValueError):
                        lease.start_wine_process(
                            [str(self.wine), "--synthetic-test"], env=env,
                            runner_path=self.runner_path)
                spawn.assert_not_called()

    def test_unrelated_environment_override_after_sealing_fails(self) -> None:
        lease = self._lease()
        env = {**lease._env, "LANG": "changed-after-sealing"}
        with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(ValueError, "differs from the verified PrefixLease environment"):
                lease.start_wine_process(
                    [str(self.wine), "--synthetic-test"], env=env,
                    runner_path=self.runner_path)
        spawn.assert_not_called()

    def test_prefix_symlink_substitution_after_sealing_fails(self) -> None:
        lease = self._lease()
        moved = self.prefix_root / "moved prefix"
        self.prefix.rename(moved)
        self.prefix.symlink_to(moved, target_is_directory=True)
        with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaises((OSError, RuntimeError)):
                lease.start_wine_process(
                    [str(self.wine), "--synthetic-test"], env=lease._env,
                    runner_path=self.runner_path)
        spawn.assert_not_called()

    def test_prefix_marker_mismatch_fails_launch(self) -> None:
        lease = self._lease()
        marker_path = self.prefix / "FGMETAL_PREFIX_LEASE.json"
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        marker["prefix_path"] = str(self.base / "different prefix")
        marker_path.write_text(json.dumps(marker), encoding="utf-8")
        with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(RuntimeError, "marker names a different prefix"):
                lease.start_wine_process(
                    [str(self.wine), "--synthetic-test"], env=lease._env,
                    runner_path=self.runner_path)
        spawn.assert_not_called()

    def test_stale_active_lease_fails_launch(self) -> None:
        lease = self._lease()
        active = json.loads(lease._lease_marker_path.read_text(encoding="utf-8"))
        active["state"] = "RECONCILIATION_REQUIRED"
        lease._lease_marker_path.write_text(json.dumps(active), encoding="utf-8")
        with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(RuntimeError, "active PrefixLease record"):
                lease.start_wine_process(
                    [str(self.wine), "--synthetic-test"], env=lease._env,
                    runner_path=self.runner_path)
        spawn.assert_not_called()

    def test_active_marker_rejects_64k_unaccounted_growth(self) -> None:
        lease = self._lease()
        active = json.loads(lease._lease_marker_path.read_text(encoding="utf-8"))
        active["unapproved_evidence"] = "x" * (64 * 1024)
        policy._write_json(lease._lease_marker_path, active)
        with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(RuntimeError, "bounded private allocation"):
                lease.start_wine_process(
                    [str(self.wine), "--synthetic-test"], env=lease._env,
                    runner_path=self.runner_path)
        spawn.assert_not_called()

    def test_active_marker_rejects_unknown_schema_field(self) -> None:
        lease = self._lease()
        active = json.loads(lease._lease_marker_path.read_text(encoding="utf-8"))
        active["unapproved_evidence"] = "small"
        policy._write_json(lease._lease_marker_path, active)
        with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(RuntimeError, "schema or canonical encoding"):
                lease.start_wine_process(
                    [str(self.wine), "--synthetic-test"], env=lease._env,
                    runner_path=self.runner_path)
        spawn.assert_not_called()

    def test_replaced_manifest_parent_inode_fails_even_with_same_manifest_bytes(self) -> None:
        lease = self._lease()
        original_bytes = self.manifest_path.read_bytes()
        moved = self.base / "old manifest parent"
        self.manifest_parent.rename(moved)
        self.manifest_parent.mkdir()
        (self.manifest_parent / self.manifest_path.name).write_bytes(original_bytes)
        with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(OSError, "manifest parent changed after admission"):
                lease.start_wine_process(
                    [str(self.wine), "--synthetic-test"], env=lease._env,
                    runner_path=self.runner_path)
        spawn.assert_not_called()

    def test_replaced_active_marker_parent_inode_fails_even_with_same_marker(self) -> None:
        lease = self._lease()
        original_bytes = lease._lease_marker_path.read_bytes()
        moved = self.base / "old active marker parent"
        self.active_root.rename(moved)
        self.active_root.mkdir()
        (self.active_root / lease._lease_marker_path.name).write_bytes(original_bytes)
        with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            with self.assertRaisesRegex(OSError, "active PrefixLease marker parent changed"):
                lease.start_wine_process(
                    [str(self.wine), "--synthetic-test"], env=lease._env,
                    runner_path=self.runner_path)
        spawn.assert_not_called()

    def test_cleanup_refuses_a_prefix_different_from_the_launch_seal(self) -> None:
        lease = self._lease()
        self._start(lease, FakeWineProcess([str(self.wine), "--synthetic-test"]))
        different_prefix = self.base / "different prefix"
        lease._env["WINEPREFIX"] = str(different_prefix)
        lease._sealed_wine_env["WINEPREFIX"] = str(different_prefix)
        with mock.patch.object(policy, "_spawn_in_directory_fd") as spawn:
            good, records = lease._stop_wineserver()
        self.assertFalse(good)
        self.assertEqual(records[0]["status"], "PREFIX_LEASE_BINDING_UNSAFE")
        spawn.assert_not_called()

    def test_exec_wrapper_checks_absolute_parent_and_prefix_inode(self) -> None:
        lease = self._lease()
        parent_info = os.fstat(lease._prefix_parent_fd)
        with mock.patch.object(policy.subprocess, "Popen", return_value=mock.sentinel.process) as popen:
            policy._spawn_in_directory_fd(
                lease._prefix_parent_fd, ["/usr/bin/true"],
                expected_directory_identity=(parent_info.st_dev, parent_info.st_ino),
                expected_child_identity=(self.prefix.name, *lease._prefix_identity, str(self.prefix)),
                env=lease._env)
        child_command = popen.call_args.args[0]
        pass_fds = popen.call_args.kwargs["pass_fds"]
        wrapper = child_command[4]
        compile(wrapper, "<descriptor-bound-prefix-check>", "exec")
        self.assertIn("open_absolute_directory_nofollow", wrapper)
        self.assertIn("absolute Wine prefix parent identity changed", wrapper)
        self.assertIn("absolute Wine prefix inode changed", wrapper)
        self.assertIn("Wine WINEPREFIX changed before exec", wrapper)
        completed = subprocess.run(child_command, pass_fds=pass_fds, env=lease._env,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)

        moved = self.base / "moved prefix parent"
        self.prefix_root.rename(moved)
        self.prefix_root.mkdir()
        failed = subprocess.run(child_command, pass_fds=pass_fds, env=lease._env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, check=False)
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("absolute Wine prefix parent identity changed", failed.stderr)

    def test_normal_pass_lifecycle_receipt_records_canonical_prefix_identity(self) -> None:
        lease = self._lease()
        receipt = self._complete_lifecycle(
            lease, FakeWineProcess([str(self.wine), "--synthetic-test"], returncode=0))
        self.assertEqual(receipt["status"], "PASS")
        self.assertTrue(receipt["prefix_deleted"])
        self.assertEqual(receipt["wineprefix"], str(self.prefix))
        identity = receipt["wineprefix_identity"]
        self.assertEqual(identity["canonical_absolute_path"], str(self.prefix))
        self.assertEqual(identity["launch_WINEPREFIX"], str(self.prefix))
        self.assertEqual(identity["cleanup_WINEPREFIX"], str(self.prefix))
        self.assertIsNotNone(identity["prefix_inode"])
        self.assertIsNotNone(identity["parent_inode"])

    def test_nonzero_wine_exit_cleanup_preserves_the_child_error(self) -> None:
        lease = self._lease()
        child = FakeWineProcess([str(self.wine), "--synthetic-test"], returncode=23)
        child_error = subprocess.CalledProcessError(23, child.args, output="synthetic Wine failure")
        receipt = self._complete_lifecycle(lease, child, child_error)
        self.assertEqual(receipt["status"], "FAIL")
        self.assertEqual(receipt["execution_status"], "FAIL")
        self.assertEqual(receipt["storage_hygiene_status"], "PASS")
        self.assertIn("CalledProcessError(23", receipt["exception"])
        self.assertIn("--synthetic-test", receipt["exception"])
        self.assertEqual(receipt["wineprefix_identity"]["cleanup_WINEPREFIX"], str(self.prefix))

    def test_timeout_cleanup_uses_same_prefix_and_preserves_timeout(self) -> None:
        lease = self._lease()
        child = FakeWineProcess([str(self.wine), "--synthetic-test"], returncode=None)
        timeout = subprocess.TimeoutExpired(child.args, 0)
        receipt = self._complete_lifecycle(lease, child, timeout)
        self.assertEqual(receipt["status"], "FAIL")
        self.assertEqual(receipt["execution_status"], "FAIL")
        self.assertEqual(receipt["storage_hygiene_status"], "PASS")
        self.assertIn("TimeoutExpired", receipt["exception"])
        self.assertEqual(receipt["wineprefix_identity"]["launch_WINEPREFIX"], str(self.prefix))
        self.assertEqual(receipt["wineprefix_identity"]["cleanup_WINEPREFIX"], str(self.prefix))

    def test_real_cleanup_failure_keeps_reconciliation_marker_and_failure_receipt(self) -> None:
        lease = self._lease()
        self._start(lease, FakeWineProcess([str(self.wine), "--synthetic-test"]))
        active = json.loads(lease._lease_marker_path.read_text(encoding="utf-8"))
        active["state"] = "RECONCILIATION_REQUIRED"
        lease._lease_marker_path.write_text(json.dumps(active), encoding="utf-8")
        stop_process = FakeWineProcess(["should-not-start", "-k"])
        with mock.patch.object(policy, "_spawn_in_directory_fd", return_value=stop_process) as spawn:
            with self.assertRaisesRegex(RuntimeError, "cleanup completed with status FAIL"):
                lease.__exit__(None, None, None)
        spawn.assert_not_called()
        self.assertTrue(self.prefix.is_dir())
        self.assertTrue(lease._lease_marker_path.is_file())
        active_after = json.loads(lease._lease_marker_path.read_text(encoding="utf-8"))
        self.assertEqual(active_after["state"], "RECONCILIATION_REQUIRED")
        receipt = json.loads((self.output / "prefix-cleanup.json").read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "FAIL")
        self.assertFalse(receipt["prefix_deleted"])
        self.assertEqual(receipt["wineprefix"], str(self.prefix))


if __name__ == "__main__":
    unittest.main()
