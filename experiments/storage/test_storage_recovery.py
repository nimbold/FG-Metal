#!/usr/bin/env python3
"""Deterministic regression tests for storage recovery, optional-root disappearance race, and certification exclusion."""
from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
import sys
import unittest
from pathlib import Path
from unittest import mock

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import storage_policy
import build_retention
from storage_policy import (
    GIB,
    MIB,
    FREE_MINIMUM,
    GATE0_PROJECT_BUDGET,
    ROOT_CLASS_MANDATORY,
    ROOT_CLASS_OPTIONAL_EPHEMERAL,
    DiscoveredProjectRoot,
    acquire_storage_certification_lock,
    release_storage_certification_lock,
    storage_certification_guard,
    measure_project_usage,
    recover_interrupted_storage_smoke_lease_marker,
    _pending_prefix_recovery_receipts,
    _gate0_blockers,
    _verify_storage_evidence,
    _seal_storage_evidence,
    _write_json,
)


class OptionalRootRaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="fgmetal-test-storage-race-", dir="/private/tmp")
        self.base = Path(self.temp_dir.name)
        self.repo = self.base / "repo"
        self.repo.mkdir(parents=True, exist_ok=True)
        (self.repo / "test_file.txt").write_bytes(b"hello repo" * 100)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_optional_root_disappearance_between_discovery_and_stat(self) -> None:
        """Thread A discovers optional root; Thread B removes it before A stats it."""
        optional_root = self.base / "optional_ephemeral"
        optional_root.mkdir(parents=True, exist_ok=True)
        (optional_root / "temp.bin").write_bytes(b"x" * 2048)

        discovered = [
            DiscoveredProjectRoot(
                path=self.repo,
                classification=ROOT_CLASS_MANDATORY,
                category="main_repo",
                expected_dev=self.repo.stat().st_dev,
                expected_ino=self.repo.stat().st_ino,
            ),
            DiscoveredProjectRoot(
                path=optional_root,
                classification=ROOT_CLASS_OPTIONAL_EPHEMERAL,
                category="project_temp",
                expected_dev=optional_root.stat().st_dev,
                expected_ino=optional_root.stat().st_ino,
            ),
        ]

        # Simulate Thread B removing optional_root before stat
        for child in optional_root.iterdir():
            child.unlink()
        optional_root.rmdir()
        self.assertFalse(optional_root.exists())

        with mock.patch("storage_policy._classified_project_roots", return_value=discovered):
            with mock.patch("storage_policy._is_other_project_path", return_value=False):
                usage = measure_project_usage()

        # Measurement succeeded without uncaught FileNotFoundError
        self.assertIn("allocated_inode_deduplicated_bytes", usage)
        self.assertIn("disappearance_events", usage)
        events = usage["disappearance_events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["path"], str(optional_root))
        self.assertEqual(events[0]["classification"], ROOT_CLASS_OPTIONAL_EPHEMERAL)
        self.assertEqual(events[0]["event"], "OPTIONAL_ROOT_DISAPPEARED_BEFORE_STAT")

    def test_mandatory_root_disappearance_fails_closed(self) -> None:
        """Mandatory root disappears: must raise FileNotFoundError / fail closed."""
        missing_repo = self.base / "nonexistent_repo"
        discovered = [
            DiscoveredProjectRoot(
                path=missing_repo,
                classification=ROOT_CLASS_MANDATORY,
                category="main_repo",
                expected_dev=123,
                expected_ino=456,
            ),
        ]

        with mock.patch("storage_policy._classified_project_roots", return_value=discovered):
            with mock.patch("storage_policy._is_other_project_path", return_value=False):
                with self.assertRaises(FileNotFoundError):
                    measure_project_usage()

    def test_optional_root_symlink_substitution_fails_closed(self) -> None:
        """Optional root replaced with a symlink: must fail closed with OSError."""
        target_dir = self.base / "real_dir"
        target_dir.mkdir(parents=True, exist_ok=True)
        symlink_root = self.base / "symlink_root"
        os.symlink(target_dir, symlink_root)

        discovered = [
            DiscoveredProjectRoot(
                path=symlink_root,
                classification=ROOT_CLASS_OPTIONAL_EPHEMERAL,
                category="project_temp",
            ),
        ]

        with mock.patch("storage_policy._classified_project_roots", return_value=discovered):
            with mock.patch("storage_policy._is_other_project_path", return_value=False):
                with self.assertRaises(OSError) as ctx:
                    measure_project_usage()
                self.assertIn("unexpected symlink", str(ctx.exception))

    def test_optional_root_inode_replacement_fails_closed(self) -> None:
        """Optional root replaced with different inode: must fail closed with ESTALE."""
        dir1 = self.base / "root1"
        dir1.mkdir(parents=True, exist_ok=True)
        orig_st = dir1.stat()

        # Recreate dir1 to get a different inode or specify stale ino
        discovered = [
            DiscoveredProjectRoot(
                path=dir1,
                classification=ROOT_CLASS_OPTIONAL_EPHEMERAL,
                category="project_temp",
                expected_dev=orig_st.st_dev,
                expected_ino=orig_st.st_ino + 999999,  # Mismatched expected inode
            ),
        ]

        with mock.patch("storage_policy._classified_project_roots", return_value=discovered):
            with mock.patch("storage_policy._is_other_project_path", return_value=False):
                with self.assertRaises(OSError) as ctx:
                    measure_project_usage()
                self.assertIn("identity changed", str(ctx.exception))

    def test_root_is_not_directory_fails_closed(self) -> None:
        """Root is a regular file instead of a directory: must fail closed with ENOTDIR."""
        file_root = self.base / "some_file.txt"
        file_root.write_text("not a directory")

        discovered = [
            DiscoveredProjectRoot(
                path=file_root,
                classification=ROOT_CLASS_OPTIONAL_EPHEMERAL,
                category="project_temp",
            ),
        ]

        with mock.patch("storage_policy._classified_project_roots", return_value=discovered):
            with mock.patch("storage_policy._is_other_project_path", return_value=False):
                with self.assertRaises(OSError) as ctx:
                    measure_project_usage()
                self.assertIn("not a directory", str(ctx.exception))

    def test_root_permission_failure_fails_closed(self) -> None:
        """Permission error on root: must fail closed without treating as skip."""
        protected_root = self.base / "protected"
        protected_root.mkdir()

        discovered = [
            DiscoveredProjectRoot(
                path=protected_root,
                classification=ROOT_CLASS_OPTIONAL_EPHEMERAL,
                category="project_temp",
            ),
        ]

        with mock.patch.object(Path, "lstat", side_effect=PermissionError(errno.EACCES, "Permission denied")):
            with mock.patch("storage_policy._classified_project_roots", return_value=discovered):
                with mock.patch("storage_policy._is_other_project_path", return_value=False):
                    with self.assertRaises(PermissionError):
                        measure_project_usage()

    def test_root_io_error_fails_closed(self) -> None:
        """I/O error on root: must fail closed."""
        io_root = self.base / "io_root"
        io_root.mkdir()

        discovered = [
            DiscoveredProjectRoot(
                path=io_root,
                classification=ROOT_CLASS_OPTIONAL_EPHEMERAL,
                category="project_temp",
            ),
        ]

        with mock.patch.object(Path, "lstat", side_effect=OSError(errno.EIO, "Input/output error")):
            with mock.patch("storage_policy._classified_project_roots", return_value=discovered):
                with mock.patch("storage_policy._is_other_project_path", return_value=False):
                    with self.assertRaises(OSError) as ctx:
                        measure_project_usage()
                    self.assertEqual(ctx.exception.errno, errno.EIO)


class StorageCertificationLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="fgmetal-test-cert-lock-", dir="/private/tmp")
        self.cache = Path(self.temp_dir.name)
        self.patch = mock.patch("storage_policy.CACHE", self.cache)
        self.patch.start()

    def tearDown(self) -> None:
        self.patch.stop()
        self.temp_dir.cleanup()

    def test_exclusive_certification_lock_mutual_exclusion(self) -> None:
        """Two exclusive locks must not be acquired simultaneously."""
        lock1 = acquire_storage_certification_lock(exclusive=True, blocking=True)
        self.assertIsNotNone(lock1)
        try:
            # Second attempt in non-blocking mode must fail
            with self.assertRaises(OSError):
                acquire_storage_certification_lock(exclusive=True, blocking=False)
        finally:
            release_storage_certification_lock(lock1)

        # After release, lock can be acquired again
        lock2 = acquire_storage_certification_lock(exclusive=True, blocking=False)
        self.assertIsNotNone(lock2)
        release_storage_certification_lock(lock2)

    def test_storage_certification_guard_context_manager(self) -> None:
        """Verify context manager acquires and releases cleanly."""
        with storage_certification_guard(exclusive=True):
            with self.assertRaises(OSError):
                acquire_storage_certification_lock(exclusive=True, blocking=False)

        # Released outside context
        lock = acquire_storage_certification_lock(exclusive=True, blocking=False)
        release_storage_certification_lock(lock)


class StorageRecoveryUnitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory(prefix="fgmetal-test-recovery-", dir="/private/tmp")
        self.base = Path(self.temp_dir.name)
        self.repo = self.base / "repo"
        self.cache = self.base / "cache"
        self.prefixes = self.cache / "prefixes"
        self.active_leases = self.prefixes / ".active-leases"
        self.retired_leases = self.prefixes / ".retired-leases"
        self.reconciliation_dir = self.repo / "experiments/storage/prefix-lease-reconciliation"
        self.recovery_receipts = self.repo / "experiments/storage/prefix-recovery-receipts"
        self.manifest_dir = self.repo / "experiments/storage/manifests"
        self.smoke_runs = self.repo / "experiments/storage/smoke-runs"

        for p in (self.repo, self.cache, self.prefixes, self.active_leases, self.retired_leases,
                  self.reconciliation_dir, self.recovery_receipts, self.manifest_dir, self.smoke_runs):
            p.mkdir(parents=True, exist_ok=True)

        self.patches = [
            mock.patch("storage_policy.REPO", self.repo),
            mock.patch("storage_policy.CACHE", self.cache),
            mock.patch("storage_policy.PREFIX_ROOT", self.prefixes),
            mock.patch("storage_policy.ACTIVE_LEASE_ROOT", self.active_leases),
            mock.patch("storage_policy.PREFIX_RETIRED_MARKERS_ROOT", self.retired_leases),
            mock.patch("storage_policy.PREFIX_RECONCILIATION_ROOT", self.reconciliation_dir),
            mock.patch("storage_policy.PREFIX_RECOVERY_RECEIPTS_ROOT", self.recovery_receipts),
            mock.patch("storage_policy._manifest_signing_key", return_value=b"0" * 32),
            mock.patch("storage_policy.disk_free_bytes", return_value=80 * GIB),
            mock.patch("storage_policy.measure_project_usage", return_value={
                "allocated_inode_deduplicated_bytes": 10 * GIB,
                "logical_bytes": 10 * GIB,
                "file_count": 100,
                "disappearance_events": [],
            }),
            mock.patch("storage_policy.preserved_prefixes", return_value=[]),
            mock.patch("storage_policy.uncertified_prefixes", return_value=[]),
            mock.patch("storage_policy._pid_is_definitely_gone", return_value=True),
            mock.patch("build_retention.status_snapshot", return_value={
                "active_build_count": 0,
                "unknown": [],
                "gate_status": "PASS",
            }),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.temp_dir.cleanup()

    def _create_mock_manifest(self, experiment_id: str) -> Path:
        manifest_path = self.manifest_dir / f"{experiment_id}.json"
        declared_peak = 3 * GIB + 32 * MIB
        uncertainty_margin = storage_policy.admission_uncertainty_margin_bytes(declared_peak)
        record = {
            "experiment_id": experiment_id,
            "storage_policy_smoke": True,
            "decision": "ALLOW",
            "manifest_path": str(manifest_path),
            "output_dir": str(self.smoke_runs / experiment_id),
            "expected_duration_seconds": 300,
            "expected_prefix_bytes": 3 * GIB,
            "expected_build_bytes": 0,
            "estimated_persistent_bytes": 32 * MIB,
            "estimated_max_new_disk_bytes": declared_peak,
            "estimated_uncertainty_margin_bytes": uncertainty_margin,
            "estimated_projected_new_disk_bytes": declared_peak + uncertainty_margin,
            "project_budget_bytes": storage_policy.PROJECT_BUDGET,
            "expected_prefix_requirement": True,
            "expected_build_requirement": False,
            "storage_justification": "test",
        }
        signed = storage_policy._seal_storage_evidence(record, "manifest_hmac_sha256")
        _write_json(manifest_path, signed, strict_directory_sync=True)
        return manifest_path

    def _create_interrupted_marker(self, experiment_id: str, state: str = "MATERIALIZED", pid: int = 999999) -> tuple[Path, bytes]:
        manifest_path = self._create_mock_manifest(experiment_id)
        manifest_bytes = manifest_path.read_bytes()
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        output_dir = self.smoke_runs / experiment_id
        output_dir.mkdir(parents=True, exist_ok=True)
        marker_path = self.active_leases / f"{experiment_id}.json"
        marker_payload = {
            "experiment_id": experiment_id,
            "pid": pid,
            "state": state,
            "prefix_path": str(self.prefixes / f"storage-smoke-{experiment_id}"),
            "output_dir": str(output_dir),
            "manifest_path": str(manifest_path),
            "manifest_sha256": manifest_sha,
            "updated_at_unix": time.time() - 100,
        }
        _write_json(marker_path, marker_payload, strict_directory_sync=True)
        return marker_path, marker_path.read_bytes()

    def test_positive_recovery_workflow_and_receipt_contents(self) -> None:
        """Test full successful current-state recovery of an interrupted smoke."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000001"
        marker_path, original_bytes = self._create_interrupted_marker(smoke_id)
        self.assertTrue(marker_path.exists())

        receipt = recover_interrupted_storage_smoke_lease_marker(smoke_id)

        # 1. Active marker retired from active directory
        self.assertFalse(marker_path.exists())

        # 2. Retired marker preserved byte-for-byte in retired directory
        retired_marker = self.retired_leases / f"{smoke_id}.json"
        self.assertTrue(retired_marker.exists())
        self.assertEqual(retired_marker.read_bytes(), original_bytes)

        # 3. Companion retirement metadata created
        retired_meta = self.retired_leases / f"{smoke_id}.meta.json"
        self.assertTrue(retired_meta.exists())
        meta_data = json.loads(retired_meta.read_text())
        self.assertEqual(meta_data["state"], "RECOVERED_CURRENT_STATE")
        self.assertEqual(meta_data["experiment_id"], smoke_id)

        # 4. Authenticated recovery receipt created
        receipt_path = self.recovery_receipts / f"storage-recovery-receipt-{smoke_id}.json"
        self.assertTrue(receipt_path.exists())
        self.assertTrue(_verify_storage_evidence(receipt, "recovery_receipt_hmac_sha256"))

        # 5. Required semantics verified
        self.assertEqual(receipt["receipt_type"], "CURRENT_STATE_STORAGE_RECOVERY")
        self.assertFalse(receipt["historical_cleanup_certified"])
        self.assertTrue(receipt["current_state_reconciled"])
        self.assertEqual(receipt["original_cleanup_receipt_status"], "MISSING")
        self.assertEqual(receipt["interrupted_smoke_id"], smoke_id)
        self.assertEqual(receipt["original_marker_state"], "MATERIALIZED")
        self.assertIn("does NOT certify or establish", receipt["recovery_statement"])

        # 6. Historical smoke runs directory does not have fabricated prefix-cleanup.json
        output_cleanup = self.smoke_runs / smoke_id / "prefix-cleanup.json"
        self.assertFalse(output_cleanup.exists())

        # 7. Copy of recovery receipt present in smoke output dir
        output_receipt = self.smoke_runs / smoke_id / f"storage-recovery-receipt-{smoke_id}.json"
        self.assertTrue(output_receipt.exists())

    def test_idempotent_rerun_returns_existing_receipt(self) -> None:
        """Re-running recovery on already recovered smoke returns receipt without conflict."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000002"
        self._create_interrupted_marker(smoke_id)

        receipt1 = recover_interrupted_storage_smoke_lease_marker(smoke_id)
        receipt2 = recover_interrupted_storage_smoke_lease_marker(smoke_id)

        self.assertEqual(receipt1["original_marker_sha256"], receipt2["original_marker_sha256"])
        self.assertEqual(receipt1["recovery_receipt_hmac_sha256"], receipt2["recovery_receipt_hmac_sha256"])

    def test_refuse_still_live_pid(self) -> None:
        """Recovery MUST refuse if supervised PID is still alive."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000003"
        self._create_interrupted_marker(smoke_id)

        with mock.patch("storage_policy._pid_is_definitely_gone", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                recover_interrupted_storage_smoke_lease_marker(smoke_id)
            self.assertIn("still live or ambiguous", str(ctx.exception))

    def test_refuse_existing_disposable_prefix(self) -> None:
        """Recovery MUST refuse if disposable prefix path still exists on disk."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000004"
        self._create_interrupted_marker(smoke_id)
        prefix_path = self.prefixes / f"storage-smoke-{smoke_id}"
        prefix_path.mkdir(parents=True, exist_ok=True)

        with self.assertRaises(RuntimeError) as ctx:
            recover_interrupted_storage_smoke_lease_marker(smoke_id)
        self.assertIn("prefix path still exists", str(ctx.exception))

    def test_refuse_uncertified_prefix_conflict(self) -> None:
        """Recovery MUST refuse if uncertified prefixes exist."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000005"
        self._create_interrupted_marker(smoke_id)

        with mock.patch("storage_policy.uncertified_prefixes", return_value=[Path("/some/stray/prefix")]):
            with self.assertRaises(RuntimeError) as ctx:
                recover_interrupted_storage_smoke_lease_marker(smoke_id)
            self.assertIn("uncertified prefixes exist", str(ctx.exception))

    def test_refuse_active_build_conflict(self) -> None:
        """Recovery MUST refuse if an active build lease is running."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000006"
        self._create_interrupted_marker(smoke_id)

        with mock.patch("build_retention.status_snapshot", return_value={"active_build_count": 1, "gate_status": "PASS"}):
            with self.assertRaises(RuntimeError) as ctx:
                recover_interrupted_storage_smoke_lease_marker(smoke_id)
            self.assertIn("active build leases exist", str(ctx.exception))

    def test_refuse_other_active_lease_conflict(self) -> None:
        """Recovery MUST refuse if another active lease exists."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000007"
        self._create_interrupted_marker(smoke_id)
        other_marker = self.active_leases / "other-lease.json"
        other_marker.write_text(json.dumps({"state": "MATERIALIZED"}))

        with self.assertRaises(RuntimeError) as ctx:
            recover_interrupted_storage_smoke_lease_marker(smoke_id)
        self.assertIn("other active leases exist", str(ctx.exception))

    def test_refuse_free_space_below_threshold(self) -> None:
        """Recovery MUST refuse if free disk space < the 50 GiB normal minimum."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000008"
        self._create_interrupted_marker(smoke_id)

        with mock.patch("storage_policy.disk_free_bytes", return_value=40 * GIB):
            with self.assertRaises(RuntimeError) as ctx:
                recover_interrupted_storage_smoke_lease_marker(smoke_id)
            self.assertIn("live free space below threshold", str(ctx.exception))

    def test_refuse_project_usage_above_threshold(self) -> None:
        """Recovery MUST refuse if scoped project usage > the 55 GiB ceiling."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000009"
        self._create_interrupted_marker(smoke_id)

        with mock.patch("storage_policy.measure_project_usage", return_value={
            "allocated_inode_deduplicated_bytes": 56 * GIB,
            "logical_bytes": 56 * GIB,
            "file_count": 100,
            "disappearance_events": [],
        }):
            with self.assertRaises(RuntimeError) as ctx:
                recover_interrupted_storage_smoke_lease_marker(smoke_id)
            self.assertIn("live project usage above budget", str(ctx.exception))

    def test_refuse_when_valid_cleanup_receipt_exists(self) -> None:
        """Recovery MUST refuse if a normal valid cleanup receipt already exists (normal reconciliation required)."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000010"
        self._create_interrupted_marker(smoke_id)
        cleanup_path = self.smoke_runs / smoke_id / "prefix-cleanup.json"
        valid_cleanup = {
            "experiment_id": smoke_id,
            "receipt_finalization_complete": True,
            "cleanup_error": None,
            "prefix_path": str(self.prefixes / f"storage-smoke-{smoke_id}"),
            "prefix_created": True,
            "prefix_deleted": True,
            "prefix_preserved": False,
        }
        signed_cleanup = storage_policy._seal_storage_evidence(valid_cleanup, "cleanup_receipt_hmac_sha256")
        _write_json(cleanup_path, signed_cleanup, strict_directory_sync=True)

        with self.assertRaises(RuntimeError) as ctx:
            recover_interrupted_storage_smoke_lease_marker(smoke_id)
        self.assertIn("use standard reconciliation instead of recovery", str(ctx.exception))

    def test_refuse_tampered_manifest_sha(self) -> None:
        """Recovery MUST refuse if marker manifest_sha256 does not match signed manifest."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000011"
        marker_path, _ = self._create_interrupted_marker(smoke_id)
        marker_data = json.loads(marker_path.read_text())
        marker_data["manifest_sha256"] = "a" * 64
        _write_json(marker_path, marker_data, strict_directory_sync=True)

        with self.assertRaises(ValueError) as ctx:
            recover_interrupted_storage_smoke_lease_marker(smoke_id)
        self.assertIn("does not match signed manifest", str(ctx.exception))

    def test_refuse_invalid_manifest_signature(self) -> None:
        """Recovery MUST refuse if manifest signature is invalid."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000012"
        marker_path, _ = self._create_interrupted_marker(smoke_id)
        manifest_path = self.manifest_dir / f"{smoke_id}.json"
        manifest_data = json.loads(manifest_path.read_text())
        manifest_data["manifest_hmac_sha256"] = "b" * 64
        _write_json(manifest_path, manifest_data, strict_directory_sync=True)
        # update marker sha to match tampered manifest file
        marker_data = json.loads(marker_path.read_text())
        marker_data["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
        _write_json(marker_path, marker_data, strict_directory_sync=True)

        with self.assertRaises(ValueError) as ctx:
            recover_interrupted_storage_smoke_lease_marker(smoke_id)
        self.assertIn("manifest signature invalid", str(ctx.exception))

    def test_refuse_wrong_marker_schema(self) -> None:
        """Recovery MUST refuse malformed marker schema."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000013"
        marker_path, _ = self._create_interrupted_marker(smoke_id)
        # remove required field
        marker_data = json.loads(marker_path.read_text())
        del marker_data["pid"]
        _write_json(marker_path, marker_data, strict_directory_sync=True)

        with self.assertRaises(ValueError) as ctx:
            recover_interrupted_storage_smoke_lease_marker(smoke_id)
        self.assertIn("missing pid", str(ctx.exception))

    def test_refuse_path_outside_approved_root(self) -> None:
        """Recovery MUST refuse if prefix or output path is outside approved root."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000014"
        marker_path, _ = self._create_interrupted_marker(smoke_id)
        marker_data = json.loads(marker_path.read_text())
        marker_data["prefix_path"] = "/etc/passwd"
        _write_json(marker_path, marker_data, strict_directory_sync=True)

        with self.assertRaises(ValueError) as ctx:
            recover_interrupted_storage_smoke_lease_marker(smoke_id)
        self.assertIn("non-canonical prefix path", str(ctx.exception))

    def test_gate0_blockers_reject_tampered_recovery_receipt(self) -> None:
        """_gate0_blockers MUST block Gate 0 if a recovery receipt HMAC fails."""
        smoke_id = "step11d3-storage-policy-smoke-20261008-0000000000000015"
        self._create_interrupted_marker(smoke_id)
        recover_interrupted_storage_smoke_lease_marker(smoke_id)

        receipt_path = self.recovery_receipts / f"storage-recovery-receipt-{smoke_id}.json"
        tampered = json.loads(receipt_path.read_text())
        tampered["recovery_receipt_hmac_sha256"] = "c" * 64
        receipt_path.write_text(json.dumps(tampered))

        pending = _pending_prefix_recovery_receipts()
        self.assertIn(str(receipt_path), pending)


if __name__ == "__main__":
    unittest.main()
