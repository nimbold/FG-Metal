#!/usr/bin/env python3
"""Focused synthetic checks for Gate 0 evidence binding and post-run thresholds."""
from __future__ import annotations

import ast
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

GATE0_RETENTION_STATE_FIELDS = {
    "active_build_count",
    "preserved_build_count",
    "retained_build_count",
    "active_dxvk_count",
    "active_moltenvk_count",
    "unknown_count",
    "completed_pending_retirement_count",
    "failed_or_retiring_count",
    "protected_evidence_root_count",
    "protected_in_place_evidence",
    "retention_fingerprint_sha256",
}


class Gate0EvidenceBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-g0c-binding-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo"
        self.storage = self.repo / "experiments/storage"
        self.storage.mkdir(parents=True)
        self.manifest_root = self.storage / "manifests"
        self.manifest_root.mkdir()
        self.attestation_path = self.storage / "storage-gate0-attestation.json"
        self.inventory_path = self.storage / "storage-inventory.json"
        self.receipt_path = self.storage / "storage-cleanup-receipt.json"
        self.template = self.root / "template"
        self.template.mkdir()
        self.vm_capacity_root = self.root / "vm-capacity-domain"
        self.vm_capacity_root.mkdir(mode=0o700)
        self.vm_capacity_marker = self.vm_capacity_root / "active-session.json"
        marker_bytes = json.dumps({
            "graphics_experiments_run": False,
            "tree_identity": {"tree_sha256": "template-tree"},
        }).encode()
        (self.template / "FGMETAL_PREFIX_TEMPLATE.json").write_bytes(marker_bytes)
        self.key = b"synthetic Gate 0 signing key"

        now = time.time()
        self.retention = {
            "gate_status": "PASS", "gate_blockers": [], "active": [],
            "active_build_count": 0, "preserved_build_count": 0,
            "retained_build_count": 0, "active_dxvk_count": 0,
            "active_moltenvk_count": 0, "unknown": [],
            "completed_pending_retirement": [], "failed_or_retiring": [],
            "retention_fingerprint": {"sha256": "retention-fingerprint"},
        }
        retention_state = gate0._retention_state(self.retention)
        self.inventory = {
            "generated_at_unix": now,
            "filesystem": {"free_bytes": 60 * policy.GIB},
            "totals": {
                "fgmetal_scoped_allocated_inode_bytes": 10 * policy.GIB,
                "fgmetal_scoped_observed_allocated_inode_bytes": 10 * policy.GIB,
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
            "inventory_measurements": {
                "free_bytes": 60 * policy.GIB,
                "fgmetal_scoped_allocated_inode_bytes": 10 * policy.GIB,
                "fgmetal_scoped_observed_allocated_inode_bytes": 10 * policy.GIB,
                "fgmetal_scoped_observed_nonlease_allocated_inode_bytes": 10 * policy.GIB,
            },
            "measurements": {
                "free_bytes": 60 * policy.GIB,
                "fgmetal_scoped_allocated_inode_bytes": 10 * policy.GIB,
                "fgmetal_scoped_observed_allocated_inode_bytes": 10 * policy.GIB,
                "fgmetal_scoped_observed_nonlease_allocated_inode_bytes": 10 * policy.GIB,
                "signed_persistent_output_bindings": [],
            },
            "prefixlease_smoke_status": "PASS",
        }
        self.attestation["supplemental_evidence_sha256"] = {}
        test_report = self.storage / "build-retention-tests.log"
        test_report.write_bytes(b"synthetic passing suite")
        self.attestation["build_retention_test_report_path"] = str(test_report)
        self.attestation["build_retention_test_report_sha256"] = hashlib.sha256(test_report.read_bytes()).hexdigest()
        for name in policy.GATE0_SUPPLEMENTAL_EVIDENCE:
            if name == policy.G0C_REVIEW_RECORD_NAME:
                reviewed_files = {}
                for field, relative_path in policy.G0C_REVIEW_REQUIRED_FILES.items():
                    reviewed_path = self.storage / relative_path
                    contents = ("synthetic reviewed G0C input " + relative_path + "\n").encode()
                    reviewed_path.write_bytes(contents)
                    reviewed_files[field] = {
                        "path": (Path("experiments/storage") / relative_path).as_posix(),
                        "sha256": hashlib.sha256(contents).hexdigest(),
                        "size_bytes": len(contents),
                    }
                contents = (json.dumps({
                    "status": "PASS",
                    "findings": [],
                    "reviewed_files": reviewed_files,
                }, sort_keys=True) + "\n").encode()
            else:
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
        self.patches.enter_context(mock.patch.object(
            policy, "VM_CAPACITY_DOMAIN_ROOT", self.vm_capacity_root))
        self.patches.enter_context(mock.patch.object(
            policy, "VM_CAPACITY_DOMAIN_ACTIVE_MARKER", self.vm_capacity_marker))
        self.patches.enter_context(mock.patch.object(policy, "_verify_gate0_signature", return_value=True))
        self.patches.enter_context(mock.patch.object(policy, "_manifest_signing_key", return_value=self.key))
        self.patches.enter_context(mock.patch.object(policy, "preserved_prefixes", return_value=[]))
        self.patches.enter_context(mock.patch.object(policy, "uncertified_prefixes", return_value=[]))
        self.patches.enter_context(mock.patch.object(policy, "_active_lease_markers", return_value=[]))
        self.patches.enter_context(mock.patch.object(policy, "_pending_prefix_reconciliation_receipts", return_value=[]))
        self.patches.enter_context(mock.patch.object(policy, "_pending_prefix_recovery_receipts", return_value=[]))
        self.patches.enter_context(mock.patch.object(policy, "prefix_state_fingerprint",
                                                     return_value={"sha256": "prefix-fingerprint"}))
        self.patches.enter_context(mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB))
        self.patches.enter_context(mock.patch.object(policy, "measure_project_usage",
                                                     return_value={"allocated_inode_deduplicated_bytes": 10 * policy.GIB}))
        self.patches.enter_context(mock.patch.object(policy.subprocess, "run",
                                                     return_value=SimpleNamespace(stdout="")))
        self.patches.enter_context(mock.patch.object(build_retention, "status_snapshot", return_value=self.retention))
        self.patches.enter_context(mock.patch.object(build_retention, "canonical_artifact_store_audit",
                                                     return_value={"sha256": "cas-audit"}))
        # Authentication of complete real smoke receipts is tested separately.
        self.patches.enter_context(mock.patch.object(policy, "_attested_smoke_evidence_blockers", return_value=[]))

    def _blockers(self) -> list[str]:
        return policy._gate0_blockers()

    def test_live_gate_admission_rejects_mutated_g0c_reviewed_controller(self) -> None:
        self.assertEqual(self._blockers(), [])
        changed = self.storage / policy.G0C_REVIEW_REQUIRED_FILES["vm_capacity_controller"]
        changed.write_text("mutated after the pinned G0C review\n", encoding="utf-8")

        blockers = self._blockers()

        self.assertTrue(any(
            "G0C independent review does not bind current experiments/storage/vm_capacity_domain.py"
            in item for item in blockers))

    def test_live_gate_admission_rejects_missing_or_unpinned_g0c_review(self) -> None:
        review_path = self.storage / policy.G0C_REVIEW_RECORD_NAME
        review_path.unlink()
        blockers = self._blockers()
        self.assertTrue(any("G0C independent review record is missing or malformed" in item
                            for item in blockers))

        review_path.write_text("{}\n", encoding="utf-8")
        blockers = self._blockers()
        self.assertTrue(any("not the exact supplemental evidence pinned by Gate 0" in item
                            for item in blockers))

    def _protected_rows(self) -> list[dict[str, object]]:
        rows = [
            {
                "evidence_id": f"R6-A-BUILD-1-{name}",
                "path": f"/protected/{name.lower()}",
                "parent_receipt_sha256": "1" * 64,
                "final_review_sha256": "2" * 64,
                "review_supplement_sha256": "3" * 64,
                "build_index_sha256": "4" * 64,
                "build_directory_manifest_sha256": "5" * 64 if name == "FINAL" else None,
                "root_inventory_sha256": f"{index:064x}",
                "file_count": index + 1,
                "logical_bytes": (index + 1) * 4096,
                "accounting_policy": "FULLY_CHARGED",
                "mutation_policy": "FAIL_CLOSED",
                "relocation_allowed": False,
                "cleanup_allowed": False,
                "cas_mutation_allowed": False,
                "_file_identities": {"evidence.bin": {"device": 1, "inode": index + 10}},
            }
            for index, name in enumerate(("EXPLORATORY", "FINAL", "GENERATED"), start=1)
        ]
        rows.append({
            "evidence_id": "R6-A-REJECTED-R5-BUILDROOT",
            "path": "/protected/rejected-r5-buildroot",
            "parent_receipt_sha256": None,
            "final_review_sha256": None,
            "review_supplement_sha256": None,
            "build_index_sha256": None,
            "build_directory_manifest_sha256": None,
            "root_inventory_sha256": "6" * 64,
            "file_count": 667,
            "logical_bytes": 127_359_989,
            "accounting_policy": "FULLY_CHARGED",
            "mutation_policy": "FAIL_CLOSED",
            "relocation_allowed": False,
            "cleanup_allowed": False,
            "cas_mutation_allowed": False,
            "tree_content_sha256": "5b9c69bbd99ef04daa843c5066e59ff19f45c69f78640dabbae57137e6510043",
            "critical_file_relative_path": "tests/present-accounting/present-accounting.exe",
            "critical_file_sha256": "aae5ee4bffbecd11eba50215bfa36814f650f8392bc61c1db8238d805eb8ac12",
            "authority_metadata_path": "r6/provenance/R6-A-REJECTED-R5-BUILDROOT-EVIDENCE.json",
            "authority_metadata_sha256": "7" * 64,
            "storage_credit": False,
            "reservation_credit": False,
            "free_space_exclusion": False,
            "project_scope_exclusion": False,
            "build_lease_exemption": False,
        })
        return rows

    def _persist_retention_state(self, protected_rows: list[dict[str, object]]) -> dict[str, object]:
        self.retention["protected_in_place_evidence"] = protected_rows
        state = gate0._retention_state(self.retention)
        self.attestation["build_retention_state"] = json.loads(json.dumps(state))
        self.receipt["phases"][-1]["build_retention_state"] = json.loads(json.dumps(state))
        receipt_bytes = (json.dumps(self.receipt, sort_keys=True) + "\n").encode()
        self.receipt_path.write_bytes(receipt_bytes)
        self.attestation["cleanup_receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
        self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")
        return state

    def _refresh_locked_retention_bindings(self) -> tuple[set[str], set[str], int]:
        source = Path(gate0.__file__).read_text(encoding="utf-8")
        module = ast.parse(source)
        refresh = next(node for node in module.body
                       if isinstance(node, ast.FunctionDef) and node.name == "_refresh_locked")
        dictionaries: dict[str, ast.Dict] = {}
        projection_calls = 0
        for node in ast.walk(refresh):
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "retention_state"
                    for target in node.targets):
                if (isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name)
                        and node.value.func.id == "_retention_state"):
                    projection_calls += 1
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if node.targets[0].id in {"phase", "attestation"} and isinstance(node.value, ast.Dict):
                    dictionaries[node.targets[0].id] = node.value

        def bound_retention_fields(value: ast.Dict) -> set[str]:
            return {
                key.value for key, field_value in zip(value.keys, value.values)
                if isinstance(key, ast.Constant) and key.value == "build_retention_state"
                and isinstance(field_value, ast.Name) and field_value.id == "retention_state"
            }

        self.assertEqual(projection_calls, 2, "preflight and final snapshots must use the Gate projection")
        self.assertEqual(set(dictionaries), {"phase", "attestation"})
        return bound_retention_fields(dictionaries["phase"]), bound_retention_fields(
            dictionaries["attestation"]), projection_calls

    @staticmethod
    def _canonical_bytes(value: object) -> bytes:
        return build_retention._canonical_json(value)

    def test_gate_projection_matches_attestation_cleanup_and_live_readback_for_zero_and_four_roots(self) -> None:
        phase_fields, attestation_fields, _ = self._refresh_locked_retention_bindings()
        self.assertEqual(phase_fields, {"build_retention_state"})
        self.assertEqual(attestation_fields, {"build_retention_state"})

        for protected_rows in ([], self._protected_rows()):
            with self.subTest(protected_root_count=len(protected_rows)):
                expected = self._persist_retention_state(protected_rows)
                self.assertEqual(set(expected), GATE0_RETENTION_STATE_FIELDS)
                self.assertEqual(expected["protected_evidence_root_count"], len(protected_rows))
                self.assertEqual(len(expected["protected_in_place_evidence"]), len(protected_rows))
                if protected_rows:
                    rejected = next(row for row in expected["protected_in_place_evidence"]
                                    if row["evidence_id"] == "R6-A-REJECTED-R5-BUILDROOT")
                    self.assertEqual(rejected["authority_metadata_sha256"], "7" * 64)
                    self.assertEqual(rejected["tree_content_sha256"],
                                     "5b9c69bbd99ef04daa843c5066e59ff19f45c69f78640dabbae57137e6510043")
                self.assertTrue(all("_file_identities" not in row
                                    for row in expected["protected_in_place_evidence"]))

                live = build_retention._gate0_retention_state_projection(self.retention)
                attested = json.loads(self.attestation_path.read_text(encoding="utf-8"))["build_retention_state"]
                receipt = json.loads(self.receipt_path.read_text(encoding="utf-8"))
                cleanup = receipt["phases"][-1]["build_retention_state"]
                expected_bytes = self._canonical_bytes(expected)
                self.assertEqual(self._canonical_bytes(live), expected_bytes)
                self.assertEqual(self._canonical_bytes(attested), expected_bytes)
                self.assertEqual(self._canonical_bytes(cleanup), expected_bytes)
                self.assertEqual(self._blockers(), [])

    def test_gate0_readback_rejects_protected_and_ordinary_retention_drift(self) -> None:
        baseline = self._persist_retention_state(self._protected_rows())
        self.assertEqual(self._blockers(), [])

        protected_mutation = self._protected_rows()
        protected_mutation[1]["root_inventory_sha256"] = "f" * 64
        self.retention["protected_in_place_evidence"] = protected_mutation
        blockers = self._blockers()
        self.assertIn("live build-retention state differs from the Gate 0 attestation", blockers)
        self.assertIn("latest cleanup receipt build-retention state differs from live state", blockers)

        self._persist_retention_state(self._protected_rows())
        self.retention["preserved_build_count"] = baseline["preserved_build_count"] + 1
        blockers = self._blockers()
        self.assertIn("live build-retention state differs from the Gate 0 attestation", blockers)
        self.assertIn("latest cleanup receipt build-retention state differs from live state", blockers)

        self._persist_retention_state(self._protected_rows())
        changed_authority = self._protected_rows()
        changed_authority[-1]["authority_metadata_sha256"] = "8" * 64
        self.retention["protected_in_place_evidence"] = changed_authority
        blockers = self._blockers()
        self.assertIn("live build-retention state differs from the Gate 0 attestation", blockers)
        self.assertIn("latest cleanup receipt build-retention state differs from live state", blockers)

    def test_gate0_readback_rejects_either_protected_field_missing_from_either_persisted_side(self) -> None:
        protected_fields = ("protected_evidence_root_count", "protected_in_place_evidence")
        for side in ("attestation", "cleanup receipt"):
            for field in protected_fields:
                with self.subTest(side=side, field=field):
                    state = self._persist_retention_state(self._protected_rows())
                    persisted_attestation = json.loads(json.dumps(state))
                    persisted_cleanup = json.loads(json.dumps(state))
                    target = persisted_attestation if side == "attestation" else persisted_cleanup
                    del target[field]
                    if side == "attestation":
                        self.attestation["build_retention_state"] = persisted_attestation
                        self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")
                    else:
                        self.receipt["phases"][-1]["build_retention_state"] = persisted_cleanup
                        receipt_bytes = (json.dumps(self.receipt, sort_keys=True) + "\n").encode()
                        self.receipt_path.write_bytes(receipt_bytes)
                        self.attestation["cleanup_receipt_sha256"] = hashlib.sha256(receipt_bytes).hexdigest()
                        self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")

                    blockers = self._blockers()
                    expected_blocker = (
                        "live build-retention state differs from the Gate 0 attestation"
                        if side == "attestation"
                        else "latest cleanup receipt build-retention state differs from live state"
                    )
                    self.assertIn(expected_blocker, blockers)

    def test_current_evidence_hashes_accept_admission(self) -> None:
        self.assertEqual(self._blockers(), [])

    def test_unrelated_growth_after_gate0_fails_exact_observed_scope_check(self) -> None:
        with mock.patch.object(policy, "measure_project_usage", return_value={
                "allocated_inode_deduplicated_bytes": 10 * policy.GIB + 64 * policy.MIB}):
            blockers = self._blockers()
        self.assertIn("unrelated non-lease storage changed since Gate 0 certification", blockers)

    def test_changed_historical_output_identity_fails_gate0(self) -> None:
        baseline = {"experiment_id": "historical-run", "output_allocated_bytes": 1}
        self.attestation["measurements"]["signed_persistent_output_bindings"] = [baseline]
        self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")
        snapshot = {"bindings": ({"experiment_id": "historical-run", "output_allocated_bytes": 2},)}
        with mock.patch.object(policy, "_signed_persistent_output_snapshot",
                               return_value=(snapshot, (), [])):
            blockers = self._blockers()
        self.assertIn("signed historical experiment outputs changed since Gate 0 certification", blockers)

    def test_new_reserved_output_does_not_change_frozen_gate0_outputs(self) -> None:
        snapshot = {"bindings": ({
            "experiment_id": "new-run-after-gate0",
            "manifest_gate0_attestation_sha256": "new-run-manifest-bound-to-current-gate0",
        },)}
        with mock.patch.object(policy, "_signed_persistent_output_snapshot",
                               return_value=(snapshot, (), [])):
            blockers = self._blockers()
        self.assertNotIn("signed historical experiment outputs changed since Gate 0 certification", blockers)

    def test_gate0_attestation_records_stable_post_write_observed_scope(self) -> None:
        attestation = {
            "state_fingerprint": {"fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes": 0},
            "inventory_measurements": {"fgmetal_scoped_allocated_inode_bytes": 10 * policy.GIB},
        }
        with mock.patch.object(gate0, "ATTESTATION", self.attestation_path), \
                mock.patch.object(policy, "_manifest_signing_key", return_value=self.key), \
                mock.patch.object(policy, "_signed_persistent_output_snapshot",
                                  return_value=({"bindings": []}, (), [])), \
                mock.patch.object(policy, "measure_project_usage", return_value={
                    "allocated_inode_deduplicated_bytes": 10 * policy.GIB}), \
                mock.patch.object(policy, "disk_free_bytes",
                                  side_effect=(60 * policy.GIB, 61 * policy.GIB)):
            measurements = gate0._write_exact_live_storage_baseline(attestation)
        self.assertEqual(measurements, {
            "free_bytes": 60 * policy.GIB,
            "fgmetal_scoped_allocated_inode_bytes": 10 * policy.GIB,
            "fgmetal_scoped_observed_allocated_inode_bytes": 10 * policy.GIB,
            "fgmetal_scoped_observed_nonlease_allocated_inode_bytes": 10 * policy.GIB,
            "signed_persistent_output_bindings": [],
        })
        self.assertTrue(policy._verify_gate0_signature(json.loads(self.attestation_path.read_text())))

    def test_gate0_baseline_rejects_free_drift_above_existing_bound_with_diagnostic(self) -> None:
        attestation = {
            "state_fingerprint": {"fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes": 0},
            "inventory_measurements": {"fgmetal_scoped_allocated_inode_bytes": 10 * policy.GIB},
        }
        free_samples = [value for _ in range(8)
                        for value in (60 * policy.GIB, 62 * policy.GIB + 1)]
        with mock.patch.object(gate0, "ATTESTATION", self.attestation_path), \
                mock.patch.object(policy, "_manifest_signing_key", return_value=self.key), \
                mock.patch.object(policy, "_signed_persistent_output_snapshot",
                                  return_value=({"bindings": []}, (), [])), \
                mock.patch.object(policy, "measure_project_usage", return_value={
                    "allocated_inode_deduplicated_bytes": 10 * policy.GIB}), \
                mock.patch.object(policy, "disk_free_bytes", side_effect=free_samples):
            with self.assertRaisesRegex(RuntimeError, str(2 * policy.GIB + 1)):
                gate0._write_exact_live_storage_baseline(attestation)

    def test_incomplete_gate0_baseline_is_persisted_as_signed_blocked(self) -> None:
        attestation = {"status": "PREFLIGHT_PASS", "mode": "preflight"}
        with mock.patch.object(gate0, "ATTESTATION", self.attestation_path), \
                mock.patch.object(policy, "_manifest_signing_key", return_value=self.key), \
                mock.patch.object(gate0, "_write_exact_live_storage_baseline",
                                  side_effect=RuntimeError("synthetic stability failure")):
            with self.assertRaisesRegex(RuntimeError, "synthetic stability failure"):
                gate0._write_baseline_or_fail_closed(attestation)
        saved = json.loads(self.attestation_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "BLOCKED")
        self.assertTrue(policy._verify_gate0_signature(saved))
        self.assertTrue(any("synthetic stability failure" in item
                            for item in saved["gate0_blockers"]))

    def test_gate0_free_drift_tolerance_reuses_existing_two_gib_bound(self) -> None:
        self.assertEqual(policy.GATE0_FREE_DRIFT_TOLERANCE_BYTES, 2 * policy.GIB)

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
    @staticmethod
    def _current_independent_review() -> dict[str, object]:
        reviewed_files = {}
        for field, relative_path in gate0.G0C_REVIEW_REQUIRED_FILES.items():
            path = gate0.HERE / relative_path
            reviewed_files[field] = {
                "path": path.relative_to(gate0.REPO).as_posix(),
                "sha256": gate0._sha(policy._read_file_nofollow(path)),
            }
        return {"status": "PASS", "findings": [], "reviewed_files": reviewed_files}

    def test_stale_independent_review_cannot_be_attested(self) -> None:
        with mock.patch.object(gate0, "_read_json", return_value={
                "status": "PASS", "storage_policy_sha256": "0" * 64}):
            with self.assertRaisesRegex(RuntimeError, "independent review"):
                gate0._verify_g0c_completion_evidence()

    def test_stale_required_review_hash_cannot_be_attested(self) -> None:
        for field in gate0.G0C_REVIEW_REQUIRED_FILES:
            with self.subTest(field=field):
                review = self._current_independent_review()
                review["reviewed_files"][field]["sha256"] = "0" * 64
                with mock.patch.object(gate0, "_read_json", return_value=review):
                    with self.assertRaisesRegex(RuntimeError, "does not bind current"):
                        gate0._verify_g0c_completion_evidence()

    def test_missing_required_review_file_cannot_be_attested(self) -> None:
        review = self._current_independent_review()
        del review["reviewed_files"]["build_retention_tests"]
        with mock.patch.object(gate0, "_read_json", return_value=review):
            with self.assertRaisesRegex(RuntimeError, "does not bind current"):
                gate0._verify_g0c_completion_evidence()

    def test_shared_capacity_policy_is_an_exact_required_review_input(self) -> None:
        self.assertEqual(
            gate0.G0C_REVIEW_REQUIRED_FILES["shared_capacity_policy"],
            "HOST-V3-SHARED-CAPACITY-POLICY.md",
        )
        review = self._current_independent_review()
        del review["reviewed_files"]["shared_capacity_policy"]
        with mock.patch.object(gate0, "_read_json", return_value=review):
            with self.assertRaisesRegex(RuntimeError, "does not bind current"):
                gate0._verify_g0c_completion_evidence()

    def test_failed_consolidation_cannot_be_attested(self) -> None:
        review = self._current_independent_review()
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
                "allocated_inode_deduplicated_bytes": 56 * policy.GIB,
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
