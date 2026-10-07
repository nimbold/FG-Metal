#!/usr/bin/env python3
"""Focused synthetic checks for Gate 0 evidence binding and post-run thresholds."""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import build_retention
import storage_policy as policy
import storage_gate0 as gate0


class Gate0EvidenceBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-g0c-binding-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.storage = self.repo / "experiments/storage"
        self.storage.mkdir(parents=True)
        self.attestation_path = self.storage / "storage-gate0-attestation.json"
        self.inventory_path = self.storage / "storage-inventory.json"
        self.receipt_path = self.storage / "storage-cleanup-receipt.json"
        self.template = self.root / "template"
        self.template.mkdir()
        marker_bytes = json.dumps({
            "graphics_experiments_run": False,
            "tree_identity": {"tree_sha256": "template-tree"},
        }).encode()
        (self.template / "FGMETAL_PREFIX_TEMPLATE.json").write_bytes(marker_bytes)
        self.key = b"synthetic Gate 0 signing key"

        now = time.time()
        retention_state = {
            "active_build_count": 0,
            "preserved_build_count": 0,
            "retained_build_count": 0,
            "active_dxvk_count": 0,
            "active_moltenvk_count": 0,
            "unknown_count": 0,
            "completed_pending_retirement_count": 0,
            "failed_or_retiring_count": 0,
            "retention_fingerprint_sha256": "retention-fingerprint",
        }
        self.inventory = {
            "generated_at_unix": now,
            "filesystem": {"free_bytes": 60 * policy.GIB},
            "totals": {
                "fgmetal_scoped_allocated_inode_bytes": 10 * policy.GIB,
                "fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes": 0,
                "unaccounted_unlinked_mounted_backing_images": [],
            },
            "storage_gate_metrics": {
                "build_retention_fingerprint_sha256": "retention-fingerprint",
                "build_retention": {"gate_status": "PASS"},
                "prefix_state_fingerprint_sha256": "prefix-fingerprint",
            },
        }
        self.receipt = {"phases": [{
            "phase_id": "synthetic-phase",
            "status": "PASS",
            "storage_hygiene_status": "PASS",
            "final_free_bytes": 60 * policy.GIB,
            "fgmetal_scoped_allocated_bytes": 10 * policy.GIB,
            "completed_at_unix": now,
            "prefix_state_fingerprint_sha256": "prefix-fingerprint",
            "worktree_count": 0,
            "preserved_prefix_count": 0,
            "uncertified_prefix_count": 0,
            "active_lease_count": 0,
            "build_retention_state": retention_state,
        }]}
        inventory_bytes = (json.dumps(self.inventory, sort_keys=True) + "\n").encode()
        receipt_bytes = (json.dumps(self.receipt, sort_keys=True) + "\n").encode()
        self.inventory_path.write_bytes(inventory_bytes)
        self.receipt_path.write_bytes(receipt_bytes)
        retention = {
            "gate_status": "PASS", "gate_blockers": [], "active": [],
            "active_build_count": 0, "preserved_build_count": 0,
            "retained_build_count": 0, "active_dxvk_count": 0,
            "active_moltenvk_count": 0, "unknown": [],
            "completed_pending_retirement": [], "failed_or_retiring": [],
            "retention_fingerprint": {"sha256": "retention-fingerprint"},
        }
        self.attestation = {
            "schema_version": 1,
            "status": "PASS",
            "mode": "final",
            "created_at_unix": now,
            "inventory_sha256": hashlib.sha256(inventory_bytes).hexdigest(),
            "cleanup_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
            "automatic_pre_run_disk_checks": True,
            "automatic_post_run_disk_checks": True,
            "automatic_retention_controls_installed": True,
            "runner_admission_contract": policy.RUNNER_ADMISSION_CONTRACT,
            "authorized_runner_source_hashes_required": True,
            "certified_template": True,
            "template_marker_sha256": hashlib.sha256(marker_bytes).hexdigest(),
            "template_tree_sha256": "template-tree",
            "retention_control_sha256": policy._gate0_control_hashes(),
            "manifest_signing_key_sha256": hashlib.sha256(self.key).hexdigest(),
            "build_retention_state": retention_state,
            "build_retention_fingerprint_sha256": "retention-fingerprint",
            "prefix_state_fingerprint_sha256": "prefix-fingerprint",
            "canonical_artifact_store_audit": {"sha256": "cas-audit"},
            "worktree_count": 0,
            "active_build_count": 0,
            "preserved_build_count": 0,
            "retained_build_count": 0,
            "prefix_count": 0,
            "preserved_prefix_count": 0,
            "uncertified_prefix_count": 0,
            "active_lease_count": 0,
            "measurements": {
                "free_bytes": 60 * policy.GIB,
                "fgmetal_scoped_allocated_inode_bytes": 10 * policy.GIB,
            },
            "prefixlease_smoke_status": "PASS",
        }
        self.attestation["supplemental_evidence_sha256"] = {}
        test_report = self.storage / "build-retention-tests.log"
        test_report.write_bytes(b"synthetic passing suite")
        self.attestation["build_retention_test_report_path"] = str(test_report)
        self.attestation["build_retention_test_report_sha256"] = hashlib.sha256(test_report.read_bytes()).hexdigest()
        for name in policy.GATE0_SUPPLEMENTAL_EVIDENCE:
            contents = ("synthetic evidence " + name).encode()
            (self.storage / name).write_bytes(contents)
            self.attestation["supplemental_evidence_sha256"][name] = hashlib.sha256(contents).hexdigest()
        self.attestation["gate0_hmac_sha256"] = "synthetic-signature"
        self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(policy, "REPO", self.repo))
        self.patches.enter_context(mock.patch.object(policy, "GATE0_ATTESTATION_PATH", self.attestation_path))
        self.patches.enter_context(mock.patch.object(policy, "INVENTORY_PATH", self.inventory_path))
        self.patches.enter_context(mock.patch.object(policy, "STORAGE_CLEANUP_RECEIPT_PATH", self.receipt_path))
        self.patches.enter_context(mock.patch.object(policy, "PREFIX_TEMPLATE", self.template))
        self.patches.enter_context(mock.patch.object(policy, "_verify_gate0_signature", return_value=True))
        self.patches.enter_context(mock.patch.object(policy, "_manifest_signing_key", return_value=self.key))
        self.patches.enter_context(mock.patch.object(policy, "preserved_prefixes", return_value=[]))
        self.patches.enter_context(mock.patch.object(policy, "uncertified_prefixes", return_value=[]))
        self.patches.enter_context(mock.patch.object(policy, "_active_lease_markers", return_value=[]))
        self.patches.enter_context(mock.patch.object(policy, "_pending_prefix_reconciliation_receipts", return_value=[]))
        self.patches.enter_context(mock.patch.object(policy, "prefix_state_fingerprint",
                                                     return_value={"sha256": "prefix-fingerprint"}))
        self.patches.enter_context(mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB))
        self.patches.enter_context(mock.patch.object(policy, "measure_project_usage",
                                                     return_value={"allocated_inode_deduplicated_bytes": 10 * policy.GIB}))
        self.patches.enter_context(mock.patch.object(policy.subprocess, "run",
                                                     return_value=SimpleNamespace(stdout="")))
        self.patches.enter_context(mock.patch.object(build_retention, "status_snapshot", return_value=retention))
        self.patches.enter_context(mock.patch.object(build_retention, "canonical_artifact_store_audit",
                                                     return_value={"sha256": "cas-audit"}))
        # Authentication of complete real smoke receipts is tested separately.
        self.patches.enter_context(mock.patch.object(policy, "_attested_smoke_evidence_blockers", return_value=[]))

    def _blockers(self) -> list[str]:
        return policy._gate0_blockers()

    def test_current_evidence_hashes_accept_admission(self) -> None:
        self.assertEqual(self._blockers(), [])

    def test_stale_supplemental_evidence_hash_rejects_admission(self) -> None:
        name = policy.GATE0_SUPPLEMENTAL_EVIDENCE[0]
        (self.storage / name).write_text("changed evidence")
        self.assertTrue(any(f"supplemental storage evidence changed or is not attested: {name}" in item
                            for item in self._blockers()))

    def test_stale_retention_test_log_rejects_admission(self) -> None:
        (self.storage / "build-retention-tests.log").write_text("changed test evidence")
        self.assertIn("build-retention test evidence changed or is not attested", self._blockers())

    def test_stale_inventory_hash_rejects_admission(self) -> None:
        inventory = json.loads(self.inventory_path.read_text(encoding="utf-8"))
        inventory["post_attestation_edit"] = True
        self.inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
        self.assertTrue(any("storage inventory changed after Gate 0 attestation" in item
                            for item in self._blockers()))

    def test_stale_cleanup_receipt_hash_rejects_admission(self) -> None:
        receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
        receipt["post_attestation_edit"] = True
        self.receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        self.assertTrue(any("storage cleanup receipt changed after Gate 0 attestation" in item
                            for item in self._blockers()))

    def test_stale_control_hash_rejects_admission(self) -> None:
        self.attestation["retention_control_sha256"]["storage_policy.py"] = "0" * 64
        self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")
        self.assertTrue(any("storage retention control changed or is not attested: storage_policy.py" in item
                            for item in self._blockers()))

    def test_smoke_evidence_requires_one_active_prefix_and_baseline_return(self) -> None:
        smoke_id = "step11d3-storage-policy-smoke-20261007-0123456789abcdef"
        prefix = policy.PREFIX_ROOT / f"storage-smoke-{smoke_id}"
        baseline = {
            "known_prefix_count": 1,
            "known_prefix_paths": [str(policy.PREFIX_TEMPLATE)],
            "preserved_prefix_count": 1,
            "preserved_prefix_paths": [str(policy.PREFIX_TEMPLATE)],
            "uncertified_prefix_count": 0,
            "uncertified_prefix_paths": [],
            "active_lease_count": 0,
            "active_leases": [],
            "leased_prefix_exists": False,
            "leased_prefix_is_symlink": False,
            "leased_prefix_device": None,
            "leased_prefix_inode": None,
            "leased_prefix_has_registry": False,
        }
        during = dict(baseline)
        during.update({
            "known_prefix_count": 2,
            "known_prefix_paths": [str(policy.PREFIX_TEMPLATE), str(prefix)],
            "active_lease_count": 1,
            "active_leases": [{
                "experiment_id": smoke_id,
                "state": "MATERIALIZED",
                "prefix_path": str(prefix),
                "marker_path": str(policy.ACTIVE_LEASE_ROOT / f"{smoke_id}.json"),
            }],
            "leased_prefix_exists": True,
            "leased_prefix_is_symlink": False,
            "leased_prefix_device": 1,
            "leased_prefix_inode": 2,
            "leased_prefix_has_registry": True,
        })
        evidence = {
            "baseline": baseline,
            "during": during,
            "after_cleanup": baseline,
            "controlled_command": {
                "command": ["/usr/bin/true"], "pid": 123,
                "returncode": 0, "started_at_unix": 100.0,
                "completed_at_unix": 100.1,
                "managed_by_prefixlease_wait_process": True,
                "wine_app_launched": False,
                "state_before": during, "state_after": during,
            },
            "exactly_one_temporary_prefix": True,
            "baseline_restored": True,
        }
        self.assertTrue(gate0._valid_prefix_state_evidence(evidence, smoke_id, prefix))
        evidence["during"]["known_prefix_count"] = 3
        self.assertFalse(gate0._valid_prefix_state_evidence(evidence, smoke_id, prefix))


class Gate0CompletionEvidenceTests(unittest.TestCase):
    def test_stale_independent_review_cannot_be_attested(self) -> None:
        with mock.patch.object(gate0, "_read_json", return_value={
                "status": "PASS", "storage_policy_sha256": "0" * 64}):
            with self.assertRaisesRegex(RuntimeError, "independent review"):
                gate0._verify_g0c_completion_evidence()

    def test_failed_consolidation_cannot_be_attested(self) -> None:
        review = {"status": "PASS", "reviewed_files": {}}
        for name, field in (("storage_policy.py", "source"),
                            ("test_wine_launcher_policy.py", "tests")):
            review["reviewed_files"][field] = {"sha256": gate0._sha(policy._read_file_nofollow(gate0.HERE / name))}
        with mock.patch.object(gate0, "_read_json", side_effect=[review, {"status": "FAIL"}]), \
                mock.patch.object(policy, "_verify_storage_evidence", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "consolidation"):
                gate0._verify_g0c_completion_evidence()


class PrefixLeasePostRunChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-g0c-post-run-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_cleanup_cannot_pass_below_gate0_free_or_project_threshold(self) -> None:
        record = {
            "storage_hygiene_status": "PASS",
            "free_space_status": "PASS",
            "free_after_bytes": 44 * policy.GIB,
            "project_usage_after": {
                "allocated_inode_deduplicated_bytes": 46 * policy.GIB,
            },
            "receipt_finalization_complete": True,
            "prefix_created": True,
            "prefix_deleted": True,
        }
        self.assertEqual(policy._cleanup_gate0_threshold_failures(record), [
            "free_space_below_gate0_minimum", "project_usage_above_gate0_budget",
        ])
        self.assertEqual(policy._cleanup_result_status(record), "FAIL")

    def test_threshold_cleanup_blocks_signed_gate_for_next_admission(self) -> None:
        key_path = self.root / "signing-key"
        attestation_path = self.root / "gate0-attestation.json"
        with mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path):
            key = policy._manifest_signing_key(create=True)
            attestation = {"status": "PASS", "created_at_unix": time.time()}
            attestation["gate0_hmac_sha256"] = policy._gate0_signature(attestation)
            attestation_path.write_text(json.dumps(attestation), encoding="utf-8")
            result = policy._invalidate_gate0_after_cleanup_threshold_failure(
                "synthetic-run", ["free_space_below_gate0_minimum"],
                free_after_bytes=44 * policy.GIB,
                project_after_bytes=10 * policy.GIB,
            )
            blocked = json.loads(attestation_path.read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "BLOCKED")
            self.assertEqual(blocked["status"], "BLOCKED")
            self.assertTrue(policy._verify_gate0_signature(blocked))
            self.assertEqual(blocked["post_cleanup_threshold_failure"]["experiment_id"], "synthetic-run")

            inventory_path = self.root / "inventory.json"
            receipt_path = self.root / "receipt.json"
            inventory_path.write_text(json.dumps({"filesystem": {}, "totals": {}}), encoding="utf-8")
            receipt_path.write_text(json.dumps({"phases": []}), encoding="utf-8")
            with mock.patch.object(policy, "INVENTORY_PATH", inventory_path), \
                    mock.patch.object(policy, "STORAGE_CLEANUP_RECEIPT_PATH", receipt_path), \
                    mock.patch.object(policy, "REPO", self.root), \
                    mock.patch.object(policy, "PREFIX_TEMPLATE", self.root / "missing-template"), \
                    mock.patch.object(policy, "_manifest_signing_key", return_value=key), \
                    mock.patch.object(policy, "preserved_prefixes", return_value=[]), \
                    mock.patch.object(policy, "uncertified_prefixes", return_value=[]), \
                    mock.patch.object(policy, "_active_lease_markers", return_value=[]), \
                    mock.patch.object(policy, "_pending_prefix_reconciliation_receipts", return_value=[]), \
                    mock.patch.object(policy, "prefix_state_fingerprint", return_value={"sha256": "missing"}), \
                    mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB), \
                    mock.patch.object(policy, "measure_project_usage",
                                      return_value={"allocated_inode_deduplicated_bytes": 10 * policy.GIB}), \
                    mock.patch.object(policy.subprocess, "run", return_value=SimpleNamespace(stdout="")), \
                    mock.patch.object(build_retention, "status_snapshot", return_value={"gate_status": "PASS"}), \
                    mock.patch.object(build_retention, "canonical_artifact_store_audit", return_value={}):
                blockers = policy._gate0_blockers()
            self.assertTrue(any("Gate 0 requires a final PASS attestation" in item for item in blockers))

    def test_invalid_signed_gate_is_not_rewritten_during_threshold_failure(self) -> None:
        key_path = self.root / "signing-key"
        attestation_path = self.root / "gate0-attestation.json"
        with mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path):
            policy._manifest_signing_key(create=True)
            original = {"status": "PASS", "gate0_hmac_sha256": "not-a-valid-signature"}
            attestation_path.write_text(json.dumps(original), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "cannot invalidate an untrusted"):
                policy._invalidate_gate0_after_cleanup_threshold_failure(
                    "synthetic-run", ["free_space_below_gate0_minimum"],
                    free_after_bytes=44 * policy.GIB,
                    project_after_bytes=10 * policy.GIB,
                )
            self.assertEqual(json.loads(attestation_path.read_text(encoding="utf-8")), original)

    def test_failed_initial_threshold_observation_cannot_be_overwritten_by_later_sample(self) -> None:
        record = {
            "storage_hygiene_status": "PASS",
            "free_space_status": "PASS",
            "free_after_bytes": 50 * policy.GIB,
            "project_usage_after": {
                "allocated_inode_deduplicated_bytes": 10 * policy.GIB,
            },
            "gate0_threshold_failures": ["free_space_below_gate0_minimum"],
            "receipt_finalization_complete": True,
            "prefix_created": True,
            "prefix_deleted": True,
        }
        self.assertEqual(policy._cleanup_gate0_threshold_failures(record), [
            "free_space_below_gate0_minimum",
        ])
        self.assertEqual(policy._cleanup_result_status(record), "FAIL")

    def test_wait_process_timeout_reaps_child_and_reports_timeout(self) -> None:
        lease = policy.PrefixLease(self.root / "prefix", self.root / "output", "timeout-test",
                                   self.root / "wineserver", clone_template=False)
        lease._clone_process = None
        process = mock.Mock()
        process.args = ["/usr/bin/sleep", "60"]
        process.poll.return_value = None
        with mock.patch.object(policy.time, "monotonic", side_effect=[10.0, 11.0, 11.0]), \
                mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB), \
                mock.patch.object(policy.PrefixLease, "_terminate_and_reap", return_value=True) as reap:
            with self.assertRaises(subprocess.TimeoutExpired):
                lease.wait_process(process, timeout_seconds=0.5)
        reap.assert_called_once_with(process)
        self.assertEqual(lease._managed_processes, [])
        self.assertEqual(lease.storage_watch_events[-1]["action"], "PROCESS_TIMEOUT_TERMINATE")

    def test_unreaped_timeout_marks_lease_unsafe(self) -> None:
        lease = policy.PrefixLease(self.root / "prefix", self.root / "output", "unreaped-timeout-test",
                                   self.root / "wineserver", clone_template=False)
        lease._clone_process = None
        process = mock.Mock()
        process.args = ["/usr/bin/sleep", "60"]
        process.poll.return_value = None
        with mock.patch.object(policy.time, "monotonic", side_effect=[10.0, 11.0, 11.0]), \
                mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB), \
                mock.patch.object(policy.PrefixLease, "_terminate_and_reap", return_value=False):
            with self.assertRaises(subprocess.TimeoutExpired):
                lease.wait_process(process, timeout_seconds=0.5)
        self.assertTrue(lease._unsafe_live_process)
        self.assertIn(process, lease._managed_processes)
        self.assertTrue(lease.storage_watch_events[-1]["process_still_running"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
