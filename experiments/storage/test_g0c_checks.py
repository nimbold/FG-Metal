#!/usr/bin/env python3
"""Focused synthetic checks for Gate 0 evidence binding and post-run thresholds."""
from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import build_retention
import storage_policy as policy
import storage_gate0 as gate0

_REAL_GATE0_SIGNATURE_VERIFIER = policy._verify_gate0_signature
_REAL_ATTESTED_SMOKE_VERIFIER = policy._attested_smoke_evidence_blockers
_REAL_RETENTION_STATUS_SNAPSHOT = build_retention.status_snapshot
_REAL_CAS_AUDIT = build_retention.canonical_artifact_store_audit
_REAL_SUBPROCESS_RUN = subprocess.run
_REAL_PROJECT_USAGE_MEASURER = policy.measure_project_usage

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


def _synthetic_allocation_snapshot(allocated_bytes: int) -> dict[str, object]:
    row = {
        "path": "/synthetic/fgmetal/nonlease-baseline.bin",
        "root": "/synthetic/fgmetal",
        "classification": policy.ROOT_CLASS_MANDATORY,
        "category": "main_repo",
        "kind": "regular_file",
        "size_bytes": allocated_bytes,
        "allocated_bytes": allocated_bytes,
        "owner_uid": 501,
        "owner_gid": 20,
        "mode": 0o600,
        "link_count": 1,
        "device": 1,
        "inode": 2,
        "mtime_ns": 1,
        "ctime_ns": 1,
    }
    return policy._make_nonlease_allocation_snapshot([row], ())


class Gate0EvidenceBindingTests(unittest.TestCase):
    def test_source_supplemental_roster_resolves_every_declared_artifact(self) -> None:
        names = set(policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertTrue(policy.R90_ONLY_SUPPLEMENTAL_EVIDENCE.issubset(names))
        missing = [
            (name, policy.gate0_supplemental_path(policy.REPO, name))
            for name in sorted(names)
            if not policy.gate0_supplemental_path(policy.REPO, name).is_file()
        ]
        self.assertEqual(missing, [], "Gate supplemental roster contains unmaterialized files")

    def test_r91_gate_recognizes_only_exact_candidate20_or_candidate21_packet(self) -> None:
        packets = (
            (policy.R91_CANDIDATE_20_G0C_REVIEW_RECORD_NAME,
             "R6-A-R91-FRESH-G0C-REVIEW-CANDIDATE-20.json",
             "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-20.json",
             "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-20",
             "experiments/storage/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-16.json"),
            (policy.R91_G0C_REVIEW_RECORD_NAME,
             "R6-A-R91-FRESH-G0C-REVIEW-CANDIDATE-21.json",
             "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-21.json",
             "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-21",
             "experiments/storage/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-17.json"),
        )
        for global_name, candidate_name, manifest_name, candidate_id, plan_path in packets:
            with self.subTest(candidate_id=candidate_id), tempfile.TemporaryDirectory(
                    dir="/private/tmp") as temporary:
                repo = Path(temporary)
                contents = {
                    global_name: {"status": "PASS", "findings": [],
                                  "review_scope": f"R91 {candidate_id} launcher review"},
                    candidate_name: {"candidate_id": candidate_id, "plan_path": plan_path},
                    manifest_name: {"candidate_id": candidate_id,
                                    "status": "FROZEN_DEPENDENCY_CLOSURE"},
                }
                supplemental = {}
                for name, document in contents.items():
                    path = policy.gate0_supplemental_path(repo, name)
                    path.parent.mkdir(parents=True, exist_ok=True)
                    raw = json.dumps(document, sort_keys=True).encode()
                    path.write_bytes(raw)
                    supplemental[name] = hashlib.sha256(raw).hexdigest()
                with mock.patch.object(policy, "REPO", repo):
                    attestation = {"supplemental_evidence_sha256": supplemental}
                    self.assertTrue(policy._gate0_is_r91_attestation(attestation))
                    names, _ = policy._gate0_evidence_contract(attestation)
                    self.assertEqual(names, policy.GATE0_SUPPLEMENTAL_EVIDENCE)

                    # Candidate20 remains a valid historical packet, but it
                    # cannot be relabeled as the current Candidate21 packet.
                    if candidate_id.endswith("CANDIDATE-20"):
                        self.assertNotEqual(candidate_id, "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-21")
                        tampered = dict(supplemental)
                        tampered[candidate_name] = "0" * 64
                        self.assertFalse(policy._gate0_is_r91_attestation(
                            {"supplemental_evidence_sha256": tampered}))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="fgmetal-g0c-binding-test-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        os.chown(self.root, os.getuid(), os.getgid())
        self.repo = self.root / "repo"
        self.storage = self.repo / "experiments/storage"
        self.storage.mkdir(parents=True)
        self.manifest_root = self.storage / "manifests"
        self.manifest_root.mkdir()
        self.smoke_runs_root = self.storage / "smoke-runs"
        self.smoke_runs_root.mkdir(mode=0o700)
        self.claim_root = self.storage / "smoke-claims"
        self.claim_root.mkdir(mode=0o700)
        self.attestation_path = self.storage / "storage-gate0-attestation.json"
        self.sidecar_path = self.storage / "storage-gate0-path-state.json.zlib"
        self.inventory_path = self.storage / "storage-inventory.json"
        self.receipt_path = self.storage / "storage-cleanup-receipt.json"
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.prefix_root = self.cache / "prefixes"
        self.prefix_root.mkdir(mode=0o700)
        self.active_root = self.prefix_root / ".active-leases"
        self.active_root.mkdir(mode=0o755)
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
        self.scope_bytes = 10 * policy.GIB

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
                "nonlease_allocation_snapshot": _synthetic_allocation_snapshot(10 * policy.GIB),
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
            supplemental_path = policy.gate0_supplemental_path(self.repo, name)
            supplemental_path.parent.mkdir(parents=True, exist_ok=True)
            supplemental_path.write_bytes(contents)
            self.attestation["supplemental_evidence_sha256"][name] = hashlib.sha256(contents).hexdigest()
        self.attestation["gate0_hmac_sha256"] = "synthetic-signature"
        self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(mock.patch.object(policy, "REPO", self.repo))
        self.patches.enter_context(mock.patch.object(policy, "CACHE", self.cache))
        self.patches.enter_context(mock.patch.object(
            policy, "_classified_project_roots", return_value=[
                policy.DiscoveredProjectRoot(self.repo, policy.ROOT_CLASS_MANDATORY, "main_repo"),
                policy.DiscoveredProjectRoot(self.cache, policy.ROOT_CLASS_MANDATORY, "project_cache"),
            ]))
        self.patches.enter_context(mock.patch.object(policy, "GATE0_ATTESTATION_PATH", self.attestation_path))
        self.patches.enter_context(mock.patch.object(
            policy, "GATE0_PATH_STATE_SIDECAR_PATH", self.sidecar_path))
        self.patches.enter_context(mock.patch.object(policy, "INVENTORY_PATH", self.inventory_path))
        self.patches.enter_context(mock.patch.object(policy, "STORAGE_CLEANUP_RECEIPT_PATH", self.receipt_path))
        self.patches.enter_context(mock.patch.object(policy, "PREFIX_TEMPLATE", self.template))
        self.patches.enter_context(mock.patch.object(policy, "PREFIX_ROOT", self.prefix_root))
        self.patches.enter_context(mock.patch.object(policy, "ACTIVE_LEASE_ROOT", self.active_root))
        self.patches.enter_context(mock.patch.object(policy, "SMOKE_CLAIM_ROOT", self.claim_root))
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
                                                     side_effect=lambda **_kwargs: {
                                                         "allocated_inode_deduplicated_bytes": self.scope_bytes,
                                                         "allocated_inode_deduplicated_bytes_excluding_paths": self.scope_bytes,
                                                     }))
        self.patches.enter_context(mock.patch.object(policy.subprocess, "run",
                                                     return_value=SimpleNamespace(stdout="")))
        self.patches.enter_context(mock.patch.object(build_retention, "status_snapshot", return_value=self.retention))
        self.patches.enter_context(mock.patch.object(build_retention, "canonical_artifact_store_audit",
                                                     return_value={"sha256": "cas-audit"}))
        # Authentication of complete real smoke receipts is tested separately.
        self.patches.enter_context(mock.patch.object(policy, "_attested_smoke_evidence_blockers", return_value=[]))
        self._refresh_storage_tree_baseline()

    def _refresh_storage_tree_baseline(self) -> None:
        base_snapshot = _synthetic_allocation_snapshot(10 * policy.GIB)
        # The sidecar excludes itself from the compressed payload, then the
        # production reader authenticates and appends its exact inode row.
        policy.write_gate_path_state_sidecar(policy.capture_storage_tree_state(), base_snapshot)
        for _ in range(2):
            tree_state = policy.capture_storage_tree_state()
            descriptor = policy.write_gate_path_state_sidecar(tree_state, base_snapshot)
            sidecar_state = policy.read_gate_path_state_sidecar({
                "path_state_sidecar": descriptor,
            })
            snapshot = sidecar_state["nonlease_allocation_snapshot"]
            self.scope_bytes = policy._allocation_snapshot_total_bytes(snapshot)
            self.attestation["path_state_sidecar"] = descriptor
            self.attestation["storage_tree_state"] = tree_state
            self.attestation["measurements"]["nonlease_allocation_snapshot"] = snapshot
            self.attestation["measurements"]["fgmetal_scoped_allocated_inode_bytes"] = self.scope_bytes
            self.attestation["measurements"]["fgmetal_scoped_observed_allocated_inode_bytes"] = self.scope_bytes
            self.attestation["measurements"]["fgmetal_scoped_observed_nonlease_allocated_inode_bytes"] = self.scope_bytes
            self.attestation["inventory_measurements"]["fgmetal_scoped_allocated_inode_bytes"] = self.scope_bytes
            self.attestation["inventory_measurements"]["fgmetal_scoped_observed_allocated_inode_bytes"] = self.scope_bytes
            self.attestation["inventory_measurements"]["fgmetal_scoped_observed_nonlease_allocated_inode_bytes"] = self.scope_bytes
            self.inventory["totals"]["fgmetal_scoped_allocated_inode_bytes"] = self.scope_bytes
            self.inventory["totals"]["fgmetal_scoped_observed_allocated_inode_bytes"] = self.scope_bytes
            self.receipt["phases"][-1]["fgmetal_scoped_allocated_bytes"] = self.scope_bytes
            inventory_raw = (json.dumps(self.inventory, sort_keys=True) + "\n").encode()
            receipt_raw = (json.dumps(self.receipt, sort_keys=True) + "\n").encode()
            if self.inventory_path.read_bytes() != inventory_raw:
                self.inventory_path.write_bytes(inventory_raw)
            if self.receipt_path.read_bytes() != receipt_raw:
                self.receipt_path.write_bytes(receipt_raw)
            self.attestation["inventory_sha256"] = hashlib.sha256(inventory_raw).hexdigest()
            self.attestation["cleanup_receipt_sha256"] = hashlib.sha256(receipt_raw).hexdigest()
            self.attestation["gate0_hmac_sha256"] = policy._gate0_signature(self.attestation)
            self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")

    def _blockers(self, **kwargs: object) -> list[str]:
        return policy._gate0_blockers(**kwargs)

    def test_gate_reuses_live_retention_lock_for_canonical_artifact_audit(self) -> None:
        lock_fds = build_retention._lock()
        audit_lock_fds: list[tuple[int, int] | None] = []

        def audit(*, retention_lock_fds: tuple[int, int] | None = None) -> dict[str, str]:
            audit_lock_fds.append(retention_lock_fds)
            return {"sha256": "cas-audit"}

        try:
            with (mock.patch.object(build_retention, "_status_snapshot_with_lock",
                                    return_value=self.retention),
                  mock.patch.object(build_retention, "canonical_artifact_store_audit",
                                    side_effect=audit)):
                blockers = self._blockers(retention_lock_fds=lock_fds)
            self.assertEqual(blockers, [], blockers)
            self.assertEqual(audit_lock_fds, [lock_fds])
        finally:
            build_retention._unlock(*lock_fds)

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

    def test_historical_r89_attestation_keeps_its_original_g0c_roster(self) -> None:
        old_review_path = self.storage / policy.R89_G0C_REVIEW_RECORD_NAME
        reviewed_files = policy.g0c_reviewed_files(
            self.repo, policy.R89_G0C_REVIEW_REQUIRED_FILES)
        old_review = {
            "status": "PASS", "findings": [], "reviewed_files": reviewed_files,
        }
        old_review_raw = (json.dumps(old_review, sort_keys=True) + "\n").encode()
        old_review_path.write_bytes(old_review_raw)
        supplemental = self.attestation["supplemental_evidence_sha256"]
        supplemental.pop(policy.G0C_REVIEW_RECORD_NAME, None)
        for name in policy.R90_ONLY_SUPPLEMENTAL_EVIDENCE:
            supplemental.pop(name, None)
        supplemental[policy.R89_G0C_REVIEW_RECORD_NAME] = hashlib.sha256(old_review_raw).hexdigest()
        self.attestation["supplemental_evidence_sha256"] = supplemental

        names, required_files = policy._gate0_evidence_contract(self.attestation)

        self.assertTrue(policy.R90_ONLY_SUPPLEMENTAL_EVIDENCE.isdisjoint(names))
        self.assertEqual(required_files, policy.R89_G0C_REVIEW_REQUIRED_FILES)
        self.assertEqual(policy._g0c_reviewed_file_blockers(self.attestation), [])

    def test_gate_admission_rejects_inconsistent_nonlease_path_snapshot(self) -> None:
        descriptor = self.attestation["path_state_sidecar"]
        assert isinstance(descriptor, dict)
        descriptor["compressed_sha256"] = "0" * 64
        self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")

        blockers = self._blockers()

        self.assertTrue(any("Gate 0 path-state sidecar is invalid" in item
                            for item in blockers), blockers)

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
        self._refresh_storage_tree_baseline()
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

    def test_synthetic_r71_gate_handoff_runs_real_retention_cas_and_smoke_verifiers(self) -> None:
        """Exercise a complete signed Gate fixture with no Gate-result stubs."""
        self._prepare_synthetic_r71_gate_pass()

    def _prepare_synthetic_r71_gate_pass(self) -> None:
        """Install reusable signed synthetic Gate evidence and assert a real PASS."""
        from contextlib import ExitStack

        policy._verify_gate0_signature.side_effect = _REAL_GATE0_SIGNATURE_VERIFIER
        policy._attested_smoke_evidence_blockers.side_effect = _REAL_ATTESTED_SMOKE_VERIFIER
        policy.subprocess.run.side_effect = _REAL_SUBPROCESS_RUN
        policy.measure_project_usage.side_effect = lambda **_kwargs: {
            "allocated_inode_deduplicated_bytes": self.scope_bytes,
            "allocated_inode_deduplicated_bytes_excluding_paths": self.scope_bytes,
        }
        build_retention.status_snapshot.side_effect = _REAL_RETENTION_STATUS_SNAPSHOT
        build_retention.canonical_artifact_store_audit.side_effect = _REAL_CAS_AUDIT

        home = self.root / "isolated-home"
        temp_root = self.root / "isolated-tmp"
        home.mkdir()
        temp_root.mkdir()
        roots = {
            "REPO": self.repo,
            "HOME": home,
            "CACHE_ROOT": self.cache,
            "BUILD_ROOT": self.cache / "builds",
            "ARTIFACT_ROOT": self.cache / "artifacts/sha256",
            "STORAGE_ROOT": self.storage,
            "LEASE_ROOT": self.storage / "build-leases",
            "MANIFEST_ROOT": self.storage / "build-manifests",
            "RECEIPT_ROOT": self.storage / "build-cleanup-receipts",
            "TEMP_ROOT": temp_root,
            "LEGACY_BUILD_ROOTS": (),
            "PROTECTED_EVIDENCE_ROOTS": (),
        }
        with ExitStack() as stack:
            for name, value in roots.items():
                stack.enter_context(mock.patch.object(build_retention, name, value))
            stack.enter_context(mock.patch.object(
                policy, "SMOKE_CLAIM_ROOT", self.storage / "smoke-claims"))
            for path_name in ("BUILD_ROOT", "ARTIFACT_ROOT", "LEASE_ROOT", "MANIFEST_ROOT", "RECEIPT_ROOT"):
                path = roots[path_name]
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.chmod(0o700)
            (self.storage / "smoke-claims").mkdir(mode=0o700, exist_ok=True)
            (self.storage / "smoke-claims").chmod(0o700)

            snapshot = _REAL_RETENTION_STATUS_SNAPSHOT()
            self.assertEqual(snapshot["gate_status"], "PASS", snapshot["gate_blockers"])
            self.assertEqual(snapshot["unknown"], [])
            state = build_retention._gate0_retention_state_projection(snapshot)
            fingerprint = snapshot["retention_fingerprint"]["sha256"]
            cas_audit = _REAL_CAS_AUDIT()
            self.attestation.update({
                "build_retention_state": state,
                "build_retention_fingerprint_sha256": fingerprint,
                "canonical_artifact_store_audit": cas_audit,
                "retention_control_sha256": policy._gate0_control_hashes(),
                "manifest_signing_key_sha256": hashlib.sha256(self.key).hexdigest(),
                "worktree_count": 0,
            })
            volume = policy._volume_identity(self.repo)
            self.inventory["filesystem"]["volume_identity"] = {
                "device": volume["device"], "fsid": volume["fsid"],
            }
            self.assertEqual(
                policy._volume_identity(self.cache / "builds"),
                policy._volume_identity(self.repo),
                "synthetic R71 fixture repo/build roots must share a volume")
            self.inventory["storage_gate_metrics"].update({
                "build_retention_fingerprint_sha256": fingerprint,
                "build_retention": {"gate_status": "PASS"},
            })
            self.receipt["phases"][-1].update({
                "build_retention_state": state,
                "worktree_count": 0,
            })

            smoke_id = (
                f"{policy.STORAGE_POLICY_SMOKE_ID_PREFIX}"
                f"{time.strftime('%Y%m%d', time.gmtime())}-1123456789abcdef")
            smoke_root = self.storage / "smoke-runs"
            smoke_root.mkdir(mode=0o700, exist_ok=True)
            smoke_root.chmod(0o700)
            smoke_dir = smoke_root / smoke_id
            smoke_dir.mkdir(mode=0o700)
            preflight_path = smoke_dir / "gate0-preflight-attestation.json"
            report_path = smoke_dir / "prefix-smoke-report.json"
            cleanup_path = smoke_dir / "prefix-cleanup.json"
            preflight = {
                "schema_version": 1,
                "status": "PREFLIGHT_PASS",
                "mode": "preflight",
                "created_at_unix": time.time() - 60,
                "prefixlease_smoke_status": "PENDING",
                "permitted_smoke_experiment_id": smoke_id,
                "path_state_sidecar": self.attestation["path_state_sidecar"],
            }
            preflight["gate0_hmac_sha256"] = policy._gate0_signature(preflight)
            preflight_raw = (json.dumps(preflight, sort_keys=True) + "\n").encode()
            preflight_path.write_bytes(preflight_raw)
            preflight_path.chmod(0o600)
            self.attestation_path.write_bytes(preflight_raw)
            self.attestation_path.chmod(0o600)
            claim, claim_raw = policy._claim_storage_smoke_nonce(smoke_id, preflight_raw)
            claim_path = policy._storage_smoke_claim_path(smoke_id)

            manifest_path = policy._experiment_manifest_path(smoke_id)
            output_dir = smoke_dir
            manifest = {key: None for key in policy.SIGNED_OUTPUT_MANIFEST_REQUIRED_FIELDS}
            manifest.update({
                "experiment_id": smoke_id,
                "purpose": "synthetic isolated Gate smoke evidence",
                "candidate_hashes": {},
                "created_at_unix": claim["claimed_at_unix"] + 0.01,
                "expected_duration_seconds": 1,
                "expected_prefix_requirement": False,
                "expected_prefix_bytes": 0,
                "expected_build_requirement": False,
                "expected_build_bytes": 0,
                "estimated_max_new_disk_bytes": 0,
                "estimated_uncertainty_margin_bytes": policy.admission_uncertainty_margin_bytes(0),
                "estimated_projected_new_disk_bytes": policy.admission_uncertainty_margin_bytes(0),
                "project_estimated_peak_bytes": 0,
                "projected_free_bytes": 60 * policy.GIB,
                "projected_scoped_project_bytes": 10 * policy.GIB,
                "storage_admission_warnings": [],
                "estimated_persistent_bytes": policy.STORAGE_SMOKE_OUTPUT_RESERVATION_BYTES,
                "project_budget_bytes": policy.PROJECT_BUDGET,
                "project_budget_remaining_before_bytes": policy.PROJECT_BUDGET - 10 * policy.GIB,
                "free_before_bytes": 60 * policy.GIB,
                "project_usage_before": {"allocated_inode_deduplicated_bytes": 10 * policy.GIB},
                "output_dir": str(output_dir),
                "decision": "ALLOW",
                "blockers": [],
                "warnings": [],
                "cleanup_policy": "synthetic",
                "retention_policy": "synthetic",
                "storage_justification": None,
                "persistent_growth_over_2g_approved": False,
                "persistent_growth_over_2g_approval_reason": None,
                "internal_storage_setup": False,
                "storage_policy_smoke": True,
                "gate0_attestation_sha256": hashlib.sha256(preflight_raw).hexdigest(),
                "authorized_runner_paths": [],
                "authorized_runner_sha256": {},
                "build_source_tree_hash": None,
                "build_configuration_key_sha256": None,
                "build_kind": None,
                "build_type": None,
                "dependency_state": None,
                "preserve_debug_reason": None,
                "estimated_debug_bytes": 0,
                "failure_capture_paths": [],
                "native_architecture": None,
                "manifest_path": str(manifest_path),
                "smoke_nonce_claim_path": str(claim_path),
                "smoke_nonce_claim_sha256": hashlib.sha256(claim_raw).hexdigest(),
            })
            manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
            manifest_raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
            manifest_path.write_bytes(manifest_raw)
            manifest_path.chmod(0o600)
            cleanup = policy._seal_storage_evidence({
                "experiment_id": smoke_id,
                "status": "PASS",
                "created_at_unix": claim["claimed_at_unix"] + 0.02,
                "prefix_deleted": True,
                "prefix_preserved": False,
                "receipt_finalization_complete": True,
            }, "cleanup_receipt_hmac_sha256")
            cleanup_raw = (json.dumps(cleanup, sort_keys=True, indent=2) + "\n").encode()
            cleanup_path.write_bytes(cleanup_raw)
            cleanup_path.chmod(0o600)
            report = policy._seal_storage_evidence({
                "experiment_id": smoke_id,
                "status": "PASS",
                "created_at_unix": claim["claimed_at_unix"] + 0.03,
                "graphics_experiments_run": False,
                "wine_app_launched": False,
                "manifest_path": str(manifest_path),
                "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "preflight_attestation_path": str(preflight_path),
                "preflight_attestation_sha256": hashlib.sha256(preflight_raw).hexdigest(),
                "smoke_nonce_claim_path": str(claim_path),
                "smoke_nonce_claim_sha256": hashlib.sha256(claim_raw).hexdigest(),
                "cleanup_receipt": str(cleanup_path),
                "cleanup_receipt_sha256": hashlib.sha256(cleanup_raw).hexdigest(),
            }, "smoke_report_hmac_sha256")
            report_raw = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
            report_path.write_bytes(report_raw)
            report_path.chmod(0o600)
            self.attestation["prefixlease_smoke"] = {
                "experiment_id": smoke_id,
                "report_path": str(report_path),
                "report_sha256": hashlib.sha256(report_raw).hexdigest(),
                "manifest_path": str(manifest_path),
                "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
                "preflight_attestation_path": str(preflight_path),
                "preflight_attestation_sha256": hashlib.sha256(preflight_raw).hexdigest(),
                "smoke_nonce_claim_path": str(claim_path),
                "smoke_nonce_claim_sha256": hashlib.sha256(claim_raw).hexdigest(),
                "cleanup_receipt_path": str(cleanup_path),
                "cleanup_receipt_sha256": hashlib.sha256(cleanup_raw).hexdigest(),
            }
            self.attestation["created_at_unix"] = claim["claimed_at_unix"] + 0.04
            output_snapshot, _exclusions, output_blockers = policy._signed_persistent_output_snapshot(
                baseline_experiment_ids={smoke_id})
            self.assertEqual(output_blockers, [], output_blockers)
            self.attestation["measurements"]["signed_persistent_output_bindings"] = output_snapshot["bindings"]

            inventory_raw = (json.dumps(self.inventory, sort_keys=True) + "\n").encode()
            receipt_raw = (json.dumps(self.receipt, sort_keys=True) + "\n").encode()
            self.inventory_path.write_bytes(inventory_raw)
            self.receipt_path.write_bytes(receipt_raw)
            self.attestation["inventory_sha256"] = hashlib.sha256(inventory_raw).hexdigest()
            self.attestation["cleanup_receipt_sha256"] = hashlib.sha256(receipt_raw).hexdigest()
            self.attestation["canonical_artifact_store_audit"] = _REAL_CAS_AUDIT()
            self.attestation["gate0_hmac_sha256"] = policy._gate0_signature(self.attestation)
            self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")
            self._refresh_storage_tree_baseline()
            self.attestation["gate0_hmac_sha256"] = policy._gate0_signature(self.attestation)
            self.attestation_path.write_text(json.dumps(self.attestation), encoding="utf-8")
            time.sleep(0.05)

            blockers = policy._gate0_blockers(allow_r71_handoff=True)
            self.assertEqual(blockers, [], blockers)

    def _synthetic_r71_capability_on_passing_gate(self, *, prefix_bytes: int = 3 * policy.GIB):
        """Build a synthetic acyclic R71 closure and real ALLOW capability on this Gate fixture."""
        import shutil
        import r6a_dependency_manifest as dependency_manifest

        # The dependency manifest binds the current Gate controls as file rows.
        # Place byte-identical controls under this test repo so its closure is
        # fully local while Gate itself still verifies the real installed code.
        controls_root = Path(policy.__file__).resolve().parent
        gate_controls = {}
        for control in dependency_manifest.GATE_CONTROL_FILES:
            source = controls_root / control
            target = self.storage / control
            shutil.copyfile(source, target)
            gate_controls[control] = hashlib.sha256(target.read_bytes()).hexdigest()
        gate_path = self.attestation_path
        self.attestation["retention_control_sha256"] = gate_controls
        review_path = self.storage / policy.G0C_REVIEW_RECORD_NAME
        review = json.loads(review_path.read_bytes())
        for field, relative in policy.G0C_REVIEW_REQUIRED_FILES.items():
            reviewed_path = self.storage / relative
            reviewed_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(controls_root / relative, reviewed_path)
            reviewed_raw = reviewed_path.read_bytes()
            review["reviewed_files"][field] = {
                "path": (Path("experiments/storage") / relative).as_posix(),
                "sha256": hashlib.sha256(reviewed_raw).hexdigest(),
                "size_bytes": len(reviewed_raw),
            }
        review_raw = (json.dumps(review, sort_keys=True) + "\n").encode()
        review_path.write_bytes(review_raw)
        self.attestation["supplemental_evidence_sha256"][policy.G0C_REVIEW_RECORD_NAME] = \
            hashlib.sha256(review_raw).hexdigest()
        gate_path.write_text(json.dumps(self.attestation), encoding="utf-8")
        self.attestation["gate0_hmac_sha256"] = policy._gate0_signature(self.attestation)
        gate_path.write_text(json.dumps(self.attestation), encoding="utf-8")

        candidate_id = "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-2"
        candidate_rel = f"experiments/storage/{candidate_id}.json"
        plan_rel = "experiments/storage/r71-capacity-execution-plan.json"
        g0c_rel = "experiments/storage/R6-A-R71-G0C-REVIEW.json"
        dependency_path = self.repo / dependency_manifest.MANIFEST_NAME
        runner_rel = "experiments/storage/run_r6a_execution.py"
        test_rel = "experiments/storage/test_g0c_checks.py"
        diff_rel = "experiments/storage/r71-capacity-fixture.diff"
        for relative in (runner_rel, test_rel):
            target = self.repo / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(controls_root / Path(relative).name, target)
        diff_path = self.repo / diff_rel
        diff_path.write_bytes(b"synthetic R71 capacity fixture diff\n")
        source_root = self.root / "r71-capacity-source"
        source_root.mkdir()
        (source_root / "meson.build").write_text("project('capacity-fixture', 'c')\n")
        source_hash, _ = build_retention._tree_hash(
            source_root, reject_symlinks=True, reject_external_hardlinks=True)
        graph_key = "1" * 64
        plan = {
            "schema_version": 1,
            "source_identity": {"root": str(source_root), "sha256": source_hash},
            "tools": {}, "input_receipts": [], "host_dependency_receipts": [],
            "runtime_receipts": [], "build_configuration_key_sha256": graph_key,
            "build_graphs": [
                {"graph_id": "windows-product-r6a"},
                {"graph_id": "native-verification-r6a"},
            ],
        }
        plan_raw = json.dumps(plan, sort_keys=True, separators=(",", ":"),
                              ensure_ascii=False).encode("utf-8")
        plan_path = self.repo / plan_rel
        plan_path.write_bytes(plan_raw)
        plan_sha = hashlib.sha256(plan_raw).hexdigest()
        candidate = {
            "candidate_id": candidate_id,
            "r71_binding": {"execution_dependency_manifest": {
                "path": dependency_manifest.MANIFEST_NAME, "sha256": "0" * 64}},
            "changed_source_hashes": {
                runner_rel: hashlib.sha256((self.repo / runner_rel).read_bytes()).hexdigest()},
            "changed_test_hashes": {
                test_rel: hashlib.sha256((self.repo / test_rel).read_bytes()).hexdigest()},
            "exact_diff": {"path": diff_rel,
                           "sha256": hashlib.sha256(diff_path.read_bytes()).hexdigest()},
            "transaction_contracts": {},
        }
        candidate_path = self.repo / candidate_rel
        candidate_path.write_bytes(dependency_manifest._canonical_bytes(candidate))
        candidate_core = dependency_manifest._candidate_core_sha256(candidate)
        g0c = {
            "schema_version": 1, "status": "PASS", "findings": [],
            "r71_binding": {
                "candidate": {"path": candidate_rel, "candidate_id": candidate_id,
                              "candidate_core_sha256": candidate_core},
                "execution_dependency_manifest": {
                    "path": dependency_manifest.MANIFEST_NAME, "sha256": "0" * 64},
            },
        }
        g0c_sha = dependency_manifest._g0c_core_sha256(g0c)
        g0c_path = self.repo / g0c_rel
        g0c_path.write_bytes(dependency_manifest._canonical_bytes(g0c))
        dependency = dependency_manifest.build_manifest(
            candidate=candidate, plan=plan, plan_raw_sha256=plan_sha,
            plan_canonical_sha256=plan_sha, g0c_sha256=g0c_sha,
            candidate_path=candidate_rel, plan_path=plan_rel, repo=self.repo)
        dependency_raw = dependency_manifest._canonical_bytes(dependency)
        dependency_sha = hashlib.sha256(dependency_raw).hexdigest()
        for payload in (candidate, g0c):
            payload["r71_binding"]["execution_dependency_manifest"]["sha256"] = dependency_sha
        candidate_path.write_bytes(dependency_manifest._canonical_bytes(candidate))
        g0c_path.write_bytes(dependency_manifest._canonical_bytes(g0c))
        dependency_path.write_bytes(dependency_raw)

        admission_id = "r6a-r71-admission-synthetic-capacity"
        build_id = "r6a-r71-dual-lineage-synthetic-capacity"
        session_plan_sha = "a" * 64
        identity_sha = hashlib.sha256(json.dumps(
            {"admission_id": admission_id}, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        additional = {
            "schema_version": 3,
            "build_session_plan_sha256": session_plan_sha,
            "execution_admission_identity": {"admission_id": admission_id, "sha256": identity_sha},
            "r71_build_id": build_id,
            "r71_execution_dependency_manifest_sha256": dependency_sha,
            "r71_dependency_manifest_path": dependency_manifest.MANIFEST_NAME,
            "r71_dependency_candidate_path": candidate_rel,
            "r71_dependency_plan_path": plan_rel,
            "r71_dependency_g0c_review_path": g0c_rel,
            "r71_candidate_core_sha256": dependency["candidate_core_sha256"],
            "r71_dependency_plan_sha256": plan_sha,
            "r71_g0c_core_sha256": g0c_sha,
        }
        output_dir = self.storage / f"{admission_id}-output"
        runner = self.storage / "initialize_prefix_template.py"
        shutil.copyfile(controls_root / runner.name, runner)
        admission_lock = policy._acquire_storage_allocation_lock()
        try:
            gate_before_admission = policy._gate0_blockers(allow_r71_handoff=True)
            if gate_before_admission:
                retention_debug = _REAL_RETENTION_STATUS_SNAPSHOT()
                raise AssertionError(
                    f"synthetic Gate drifted before R71 admission: {gate_before_admission}; "
                    f"retention unknown={retention_debug.get('unknown')}; "
                    f"fingerprint={retention_debug.get('retention_fingerprint')}; "
                    f"build-root={retention_debug.get('build_root_identity_fingerprint_sha256')}")
            allowed = policy.begin_experiment(
                experiment_id=admission_id, purpose="synthetic R71 threshold regression",
                candidate_hashes={
                    "r71_candidate_core_sha256": dependency["candidate_core_sha256"],
                    "r71_dependency_plan_sha256": plan_sha,
                    "r71_g0c_core_sha256": g0c_sha,
                },
                expected_duration_seconds=300, expected_prefix_bytes=prefix_bytes,
                expected_build_bytes=64 * policy.MIB, estimated_persistent_bytes=0,
                authorized_runner_paths=[runner], build_source_tree_hash=source_hash,
                build_configuration_key_sha256=graph_key, build_kind="dxvk", build_type="release",
                dependency_state={"status": "captured", "fixture": "synthetic"},
                output_dir=output_dir, _allocation_lock=admission_lock, additional=additional)
        except BaseException:
            policy._release_storage_allocation_lock(admission_lock)
            raise
        if allowed.get("decision") != "ALLOW" or allowed.reservation_capability is None:
            policy._release_storage_allocation_lock(admission_lock)
            raise AssertionError(f"synthetic Gate fixture did not produce R71 ALLOW: {allowed.get('blockers')}")
        self.addCleanup(policy._release_storage_allocation_lock, admission_lock)
        return allowed, plan_sha, dependency_sha, candidate_path, plan_path, g0c_path

    def test_r71_real_validator_accepts_safe_report_growth_and_blocks_live_threshold_crossings(self) -> None:
        """Use a real synthetic Gate + R71 validator + capacity arithmetic, without a reservation."""
        from contextlib import ExitStack

        self._prepare_synthetic_r71_gate_pass()
        capacity = {"free": 60 * policy.GIB, "project": 10 * policy.GIB}
        report = self.storage / "unrelated-capacity-report.log"

        def measured_usage(**_kwargs):
            delta = (report.stat().st_blocks * 512) if report.is_file() else 0
            value = capacity["project"] + delta
            return {
                "allocated_inode_deduplicated_bytes": value,
                "allocated_inode_deduplicated_bytes_excluding_paths": value,
            }

        policy.disk_free_bytes.side_effect = lambda _path: capacity["free"]
        policy.measure_project_usage.side_effect = measured_usage
        roots = {
            "REPO": self.repo, "HOME": self.root / "isolated-home",
            "CACHE_ROOT": self.cache,
            "BUILD_ROOT": self.cache / "builds",
            "ARTIFACT_ROOT": self.cache / "artifacts/sha256",
            "STORAGE_ROOT": self.storage,
            "LEASE_ROOT": self.storage / "build-leases",
            "MANIFEST_ROOT": self.storage / "build-manifests",
            "RECEIPT_ROOT": self.storage / "build-cleanup-receipts",
            "TEMP_ROOT": self.root / "isolated-tmp",
            "LEGACY_BUILD_ROOTS": (), "PROTECTED_EVIDENCE_ROOTS": (),
        }
        with ExitStack() as stack:
            for name, value in roots.items():
                stack.enter_context(mock.patch.object(build_retention, name, value))
            roots["HOME"].mkdir(parents=True, exist_ok=True)
            stack.enter_context(mock.patch.object(
                policy, "SMOKE_CLAIM_ROOT", self.storage / "smoke-claims"))
            for name in ("BUILD_ROOT", "ARTIFACT_ROOT", "LEASE_ROOT", "MANIFEST_ROOT", "RECEIPT_ROOT"):
                path = roots[name]
                path.mkdir(parents=True, exist_ok=True, mode=0o700)
                path.chmod(0o700)

            allowed, _plan_sha, _dependency_sha, _candidate, _plan, _g0c = \
                self._synthetic_r71_capability_on_passing_gate(prefix_bytes=3 * policy.GIB)
            manifest = dict(allowed)
            self.assertEqual(manifest["decision"], "ALLOW")
            self.assertEqual(manifest["expected_prefix_bytes"], 3 * policy.GIB)
            self.assertEqual(manifest["estimated_max_new_disk_bytes"],
                             3 * policy.GIB + 64 * policy.MIB)
            self.assertEqual(manifest["estimated_projected_new_disk_bytes"],
                             manifest["estimated_max_new_disk_bytes"]
                             + policy.admission_uncertainty_margin_bytes(
                                 manifest["estimated_max_new_disk_bytes"]))
            cap = allowed.reservation_capability
            self.assertIsNotNone(cap)

            report.write_bytes(b"r" * 4096)
            self.assertEqual(
                policy._gate0_blockers(active_experiment_id=manifest["experiment_id"],
                                       allow_r71_handoff=True,
                                       r71_build_id=manifest["r71_build_id"]), [],
                "R71 Gate handoff must accept unrelated report bytes while current capacity is safe")
            cap.validate(
                build_id=manifest["r71_build_id"],
                owner_experiment=manifest["experiment_id"],
                plan_sha256=manifest["build_session_plan_sha256"],
                manifest_path=Path(manifest["manifest_path"]))

            # Exercise actual Gate + R71 capability verification with each
            # conflicting retention state. The fixture's Gate receipt and
            # measurements are synthetic, but the blocker/validator decisions
            # below are the production implementations.
            baseline_retention = json.loads(json.dumps(_REAL_RETENTION_STATUS_SNAPSHOT()))
            own_id = manifest["experiment_id"]
            own_build = manifest["r71_build_id"]
            own_row = {
                "build_id": own_build, "owner_experiment": own_id,
                "path": str(roots["BUILD_ROOT"] / own_build),
                "state": "ACTIVE", "kind": "dxvk",
            }
            rejected_states = {
                "foreign active": {"active": [{**own_row, "build_id": "foreign-build",
                                                  "owner_experiment": "foreign-admission"}]},
                "additional same-owner": {"active": [own_row, {**own_row,
                                                                    "build_id": "extra-build"}]},
                "unknown lease/root": {"unknown": [{"path": "unknown-build-root"}]},
                "pending retirement": {"completed_pending_retirement": [
                    {"build_id": "pending-build"}]},
                "failed/retiring": {"failed_or_retiring": [{"build_id": "failed-build"}]},
            }
            for state_name, changes in rejected_states.items():
                with self.subTest(retention_state=state_name):
                    retention_state = json.loads(json.dumps(baseline_retention))
                    retention_state.update(changes)
                    with (mock.patch.object(build_retention, "status_snapshot",
                                            return_value=retention_state),
                          self.assertRaisesRegex(
                              RuntimeError,
                              "R71 dependency closure failed at handoff boundary:.*Gate/capacity/root check failed")):
                        cap.validate(
                            build_id=own_build,
                            owner_experiment=own_id,
                            plan_sha256=manifest["build_session_plan_sha256"],
                            manifest_path=Path(manifest["manifest_path"]))

            capacity["free"] = 49 * policy.GIB
            with self.assertRaisesRegex(
                    RuntimeError, "R71 dependency closure failed at handoff boundary:.*Gate/capacity/root check failed"):
                cap.validate(
                    build_id=manifest["r71_build_id"],
                    owner_experiment=manifest["experiment_id"],
                    plan_sha256=manifest["build_session_plan_sha256"],
                    manifest_path=Path(manifest["manifest_path"]))

            capacity.update({"free": 60 * policy.GIB, "project": 60 * policy.GIB})
            with self.assertRaisesRegex(
                    RuntimeError, "R71 dependency closure failed at handoff boundary:.*Gate/capacity/root check failed"):
                cap.validate(
                    build_id=manifest["r71_build_id"],
                    owner_experiment=manifest["experiment_id"],
                    plan_sha256=manifest["build_session_plan_sha256"],
                    manifest_path=Path(manifest["manifest_path"]))

            projected = policy.projected_storage_admission(
                current_free_bytes=52 * policy.GIB,
                current_scoped_bytes=10 * policy.GIB,
                peak_additional_bytes=manifest["estimated_projected_new_disk_bytes"])
            self.assertFalse(projected["admitted"])
            self.assertLess(projected["projected_free_bytes"], policy.FREE_MINIMUM)
            self.assertTrue(any("projected free disk is below" in blocker
                                for blocker in projected["blockers"]), projected["blockers"])

            projected_scope = policy.projected_storage_admission(
                current_free_bytes=80 * policy.GIB,
                current_scoped_bytes=(policy.PROJECT_BUDGET
                                      - manifest["estimated_projected_new_disk_bytes"] + 1),
                peak_additional_bytes=manifest["estimated_projected_new_disk_bytes"])
            self.assertFalse(projected_scope["admitted"])
            self.assertEqual(projected_scope["projected_scoped_bytes"], policy.PROJECT_BUDGET + 1)
            self.assertTrue(any("projected scoped FG-Metal usage exceeds" in blocker
                                for blocker in projected_scope["blockers"]),
                            projected_scope["blockers"])

    def test_r71_handoff_does_not_exempt_uncertified_prefix_blocker(self) -> None:
        orphan = self.cache / "prefixes" / "unknown-prefix"
        with mock.patch.object(policy, "uncertified_prefixes", return_value=[orphan]):
            blockers = policy._gate0_blockers(allow_r71_handoff=True)
        self.assertTrue(any("uncertified Wine prefixes require reconciliation" in item
                            for item in blockers), blockers)

    def test_unrelated_growth_after_gate0_fails_exact_observed_scope_check(self) -> None:
        current_bytes = self.scope_bytes + 64 * policy.MIB
        diagnostic_sink: dict[str, object] = {}
        baseline = self.attestation["measurements"]["nonlease_allocation_snapshot"]
        current_rows = [dict(row) for row in baseline["entries"]]
        changed_row = next(row for row in current_rows
                           if row["path"] == "/synthetic/fgmetal/nonlease-baseline.bin")
        changed_row["allocated_bytes"] += 64 * policy.MIB
        changed_row["size_bytes"] += 64 * policy.MIB
        current_snapshot = policy._make_nonlease_allocation_snapshot(current_rows, ())
        with mock.patch.object(policy, "measure_project_usage", return_value={
                "allocated_inode_deduplicated_bytes": current_bytes,
                "allocated_inode_deduplicated_bytes_excluding_paths": current_bytes,
                "nonlease_allocation_snapshot": current_snapshot}):
            blockers = self._blockers(diagnostic_sink=diagnostic_sink)
        self.assertIn("unrelated non-lease storage changed since Gate 0 certification", blockers)
        diagnostic = diagnostic_sink["gate0_nonlease_drift_diagnostic"]
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["aggregate_allocated_bytes_delta"], 64 * policy.MIB)
        self.assertEqual(diagnostic["changed_paths"][0]["change"], "allocation_changed")

    def test_protocol_looking_path_without_exact_signed_authority_still_refuses(self) -> None:
        baseline = self.attestation["measurements"]["nonlease_allocation_snapshot"]
        current_rows = [dict(row) for row in baseline["entries"]]
        protocol_path = "/private/tmp/FGMetalSynthetic/smoke-claims/unbound-claim.json"
        current_rows.append({
            "path": protocol_path,
            "root": "/private/tmp/FGMetalSynthetic",
            "classification": policy.ROOT_CLASS_MANDATORY,
            "category": "project_cache",
            "kind": "regular_file",
            "size_bytes": 4096,
            "allocated_bytes": 4096,
            "owner_uid": 501,
            "owner_gid": 20,
            "mode": 0o600,
            "link_count": 1,
            "device": 1,
            "inode": 3,
            "mtime_ns": 2,
            "ctime_ns": 2,
        })
        observed = self.scope_bytes + 4096
        calls = 0

        def measure(**kwargs: object) -> dict[str, object]:
            nonlocal calls
            calls += 1
            result: dict[str, object] = {
                "allocated_inode_deduplicated_bytes": observed,
                "allocated_inode_deduplicated_bytes_excluding_paths": observed,
            }
            if kwargs.get("include_allocation_snapshot") is True:
                result["nonlease_allocation_snapshot"] = policy._make_nonlease_allocation_snapshot(
                    current_rows, ())
            return result

        diagnostic_sink: dict[str, object] = {}
        with mock.patch.object(policy, "measure_project_usage", side_effect=measure):
            blockers = self._blockers(diagnostic_sink=diagnostic_sink)

        self.assertIn("unrelated non-lease storage changed since Gate 0 certification", blockers)
        diagnostic = diagnostic_sink["gate0_nonlease_drift_diagnostic"]
        self.assertEqual(diagnostic["status"], "COMPLETE")
        self.assertEqual(diagnostic["changed_paths"][0]["path"], protocol_path)
        self.assertEqual(diagnostic["changed_paths"][0]["ownership_class"],
                         policy.ROOT_CLASS_MANDATORY)
        self.assertEqual(calls, 2)

    def test_path_diagnostic_failure_preserves_exact_drift_refusal(self) -> None:
        diagnostic_sink: dict[str, object] = {}
        with mock.patch.object(
                policy, "measure_project_usage",
                side_effect=[
                    {"allocated_inode_deduplicated_bytes": 10 * policy.GIB + 8192},
                    OSError("synthetic diagnostic walk failure"),
                ]):
            blockers = self._blockers(diagnostic_sink=diagnostic_sink)
        self.assertIn("unrelated non-lease storage changed since Gate 0 certification", blockers)
        diagnostic = diagnostic_sink["gate0_nonlease_drift_diagnostic"]
        self.assertEqual(diagnostic["status"], "UNAVAILABLE")
        self.assertEqual(diagnostic["comparison_observed_nonlease_allocated_bytes"],
                         10 * policy.GIB + 8192)

    def test_unrelated_deletion_after_gate0_fails_exact_observed_scope_check(self) -> None:
        with mock.patch.object(policy, "measure_project_usage", return_value={
                "allocated_inode_deduplicated_bytes": 10 * policy.GIB - 4096}):
            blockers = self._blockers()
        self.assertIn("unrelated non-lease storage changed since Gate 0 certification", blockers)

    def _begin_r69_admission_with_event_trace(self, *, drift_bytes: int) -> tuple[dict, list[str], Path]:
        admission_id = f"r6a-r69-admission-order-{drift_bytes}"
        events: list[str] = []
        output_dir = self.storage / f"{admission_id}-output"
        identity_sha = hashlib.sha256(json.dumps(
            {"admission_id": admission_id}, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False).encode("utf-8")).hexdigest()
        additional = {
            "schema_version": 3,
            "build_session_plan_sha256": "a" * 64,
            "execution_admission_identity": {
                "admission_id": admission_id,
                "sha256": identity_sha,
            },
        }
        original_measure = policy.measure_project_usage
        original_gate = policy._gate0_blockers
        original_claim = policy._claim_r66_r69_admission_id
        original_write = policy._write_json

        def measure(*, exclude_paths=None, **kwargs):
            if kwargs:
                events.append("measure_extra_options:" + ",".join(sorted(kwargs)))
            events.append("gate_measurement" if exclude_paths is not None else "admission_measurement")
            measured = ((self.scope_bytes + drift_bytes) if exclude_paths is not None
                        else 10 * policy.GIB)
            return {
                "allocated_inode_deduplicated_bytes": measured,
                "allocated_inode_deduplicated_bytes_excluding_paths": measured,
            }

        def gate(*args, **kwargs):
            events.append("blocker_comparison_start")
            result = original_gate(*args, **kwargs)
            events.append("blocker_comparison_complete")
            return result

        def projected(*, current_free_bytes, current_scoped_bytes, peak_additional_bytes):
            events.append("capacity_comparison_complete")
            return {
                "blockers": [], "warnings": [],
                "projected_scoped_bytes": current_scoped_bytes + peak_additional_bytes,
                "projected_free_bytes": current_free_bytes - peak_additional_bytes,
            }

        def claim(*args, **kwargs):
            events.append("claim_publication_start")
            result = original_claim(*args, **kwargs)
            events.append("claim_durable")
            return result

        def write(path, payload, **kwargs):
            events.append("manifest_publication_start")
            result = original_write(path, payload, **kwargs)
            events.append("manifest_durable")
            return result

        stack = ExitStack()
        stack.enter_context(mock.patch.object(policy, "CACHE", self.root / "cache"))
        stack.enter_context(mock.patch.object(policy, "measure_project_usage", side_effect=measure))
        stack.enter_context(mock.patch.object(policy, "_gate0_blockers", side_effect=gate))
        stack.enter_context(mock.patch.object(policy, "projected_storage_admission", side_effect=projected))
        stack.enter_context(mock.patch.object(policy, "_claim_r66_r69_admission_id", side_effect=claim))
        stack.enter_context(mock.patch.object(policy, "_write_json", side_effect=write))
        stack.enter_context(mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB))
        stream = io.StringIO()
        try:
            with redirect_stdout(stream):
                try:
                    result = policy.begin_experiment(
                        experiment_id=admission_id,
                        purpose="synthetic R69 admission transaction ordering",
                        candidate_hashes={"runner_sha256": "c" * 64},
                        expected_duration_seconds=300,
                        expected_prefix_bytes=0,
                        expected_build_bytes=0,
                        estimated_persistent_bytes=0,
                        output_dir=output_dir,
                        additional=additional,
                    )
                except RuntimeError:
                    manifest_path = self.manifest_root / f"{admission_id}.json"
                    result = json.loads(manifest_path.read_bytes())
        finally:
            stack.close()
        self.assertEqual(result["decision"], "REFUSE" if drift_bytes else "ALLOW")
        return result, events, output_dir

    def test_refuse_receipt_follows_exact_measurement_and_blocker_comparison(self) -> None:
        manifest, events, _output_dir = self._begin_r69_admission_with_event_trace(drift_bytes=-4096)
        self.assertIn("unrelated non-lease storage changed since Gate 0 certification",
                      manifest["blockers"])
        self.assertLess(events.index("admission_measurement"), events.index("blocker_comparison_start"))
        self.assertLess(events.index("gate_measurement"), events.index("blocker_comparison_complete"))
        self.assertLess(events.index("blocker_comparison_complete"), events.index("capacity_comparison_complete"))
        self.assertLess(events.index("capacity_comparison_complete"), events.index("claim_publication_start"))
        self.assertLess(events.index("claim_durable"), events.index("manifest_publication_start"))
        self.assertLess(events.index("manifest_durable"), len(events))
        self.assertEqual(manifest["project_usage_before"]["allocated_inode_deduplicated_bytes"],
                         10 * policy.GIB)
        self.assertEqual(manifest["admission_claim"]["decision"], "REFUSE")

    def test_r71_claim_and_allow_publication_crashes_consume_ids_without_returning_capability(self) -> None:
        write_real = policy._write_json_exclusive
        for suffix, after_durable_allow in (("claim-crash", False), ("allow-crash", True)):
            with self.subTest(after_durable_allow=after_durable_allow):
                admission_id = f"r6a-r71-admission-{suffix}"
                build_id = f"r6a-r71-dual-lineage-{suffix}"
                plan_sha = "a" * 64
                identity_sha = hashlib.sha256(json.dumps(
                    {"admission_id": admission_id}, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False).encode("utf-8")).hexdigest()
                additional = {
                    "schema_version": 3,
                    "build_session_plan_sha256": plan_sha,
                    "execution_admission_identity": {
                        "admission_id": admission_id, "sha256": identity_sha},
                    "r71_build_id": build_id,
                    "r71_execution_dependency_manifest_sha256": "b" * 64,
                    "r71_candidate_core_sha256": "c" * 64,
                    "r71_dependency_plan_sha256": "d" * 64,
                    "r71_g0c_core_sha256": "e" * 64,
                    "r71_dependency_candidate_path": "experiments/storage/candidate.json",
                    "r71_dependency_plan_path": "experiments/storage/plan.json",
                    "r71_dependency_g0c_review_path": "experiments/storage/g0c.json",
                    "r71_dependency_manifest_path": (
                        "experiments/storage/R6-A-R71-EXECUTION-DEPENDENCY-MANIFEST.json"),
                }
                lock_path = self.root / f"{suffix}.allocation-lock"
                lock_file = lock_path.open("w+b")
                manifest_path = policy._experiment_manifest_path(admission_id)

                def interrupted_write(path, payload, **kwargs):
                    if after_durable_allow:
                        write_real(path, payload, **kwargs)
                    raise RuntimeError("injected R71 publication crash")

                try:
                    with (mock.patch.object(policy, "_gate0_blockers", return_value=[]),
                          mock.patch.object(policy, "_write_json_exclusive",
                                            side_effect=interrupted_write)):
                        with self.assertRaisesRegex(RuntimeError, "injected R71 publication crash"):
                            policy.begin_experiment(
                                experiment_id=admission_id,
                                purpose="synthetic R71 one-shot crash regression",
                                candidate_hashes={"synthetic_candidate_sha256": "f" * 64},
                                expected_duration_seconds=300,
                                expected_prefix_bytes=0,
                                expected_build_bytes=0,
                                estimated_persistent_bytes=0,
                                output_dir=self.storage / f"{suffix}-output",
                                _allocation_lock=lock_file,
                                additional=additional,
                            )
                    claim_path = self.cache / policy.R66_R69_ADMISSION_CLAIM_DIRECTORY / (
                        f"{admission_id}.claim")
                    self.assertTrue(claim_path.is_file())
                    self.assertEqual(manifest_path.is_file(), after_durable_allow)
                    if after_durable_allow:
                        self.assertEqual(json.loads(manifest_path.read_bytes())["decision"], "ALLOW")
                    with self.assertRaisesRegex(RuntimeError, "already been claimed"):
                        policy._assert_r66_r69_admission_id_unclaimed(admission_id)
                    with self.assertRaisesRegex(RuntimeError, "already been consumed"):
                        policy._assert_r70_build_id_unclaimed(build_id)
                finally:
                    lock_file.close()

    def test_allow_receipt_precedes_post_allow_logging_and_state_writes(self) -> None:
        manifest, events, output_dir = self._begin_r69_admission_with_event_trace(drift_bytes=0)
        self.assertEqual(manifest["blockers"], [])
        self.assertFalse(any(event.startswith("measure_extra_options:") for event in events),
                         "legacy R69 admission must keep the no-keyword measurement contract")
        self.assertLess(events.index("blocker_comparison_complete"), events.index("claim_publication_start"))
        self.assertLess(events.index("claim_durable"), events.index("manifest_publication_start"))
        self.assertLess(events.index("manifest_durable"), len(events))

        output_dir.mkdir()
        log_path = output_dir / "synthetic-execution.log"
        state_path = output_dir / "synthetic-execution-state.json"
        log_path.write_text("post-ALLOW execution log\n", encoding="utf-8")
        events.append("post_allow_log_write")
        state_path.write_text('{"state":"reserved-after-ALLOW"}\n', encoding="utf-8")
        events.append("post_allow_state_write")

        self.assertLess(events.index("manifest_durable"), events.index("post_allow_log_write"))
        self.assertLess(events.index("post_allow_log_write"), events.index("post_allow_state_write"))
        self.assertEqual(manifest["decision"], "ALLOW")
        self.assertEqual(manifest["project_usage_before"]["allocated_inode_deduplicated_bytes"],
                         10 * policy.GIB)
        self.assertEqual(len([event for event in events if event == "admission_measurement"]), 1)

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
                    "allocated_inode_deduplicated_bytes": 10 * policy.GIB,
                    "allocated_inode_deduplicated_bytes_excluding_paths": 10 * policy.GIB,
                    "nonlease_allocation_snapshot": _synthetic_allocation_snapshot(
                        10 * policy.GIB)}), \
                mock.patch.object(policy, "disk_free_bytes",
                                  side_effect=(60 * policy.GIB, 61 * policy.GIB)):
            measurements = gate0._write_exact_live_storage_baseline(attestation)
        descriptor = json.loads(self.attestation_path.read_text())["path_state_sidecar"]
        measured = 10 * policy.GIB + descriptor["allocated_bytes"]
        self.assertEqual(measurements, {
            "free_bytes": 60 * policy.GIB,
            "fgmetal_scoped_allocated_inode_bytes": measured,
            "fgmetal_scoped_observed_allocated_inode_bytes": measured,
            "fgmetal_scoped_observed_nonlease_allocated_inode_bytes": measured,
            "signed_persistent_output_bindings": [],
        })
        self.assertTrue(policy._verify_gate0_signature(json.loads(self.attestation_path.read_text())))

    def test_gate0_snapshot_stabilizes_including_its_atomically_replaced_attestation(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-gate-snapshot-", dir="/private/tmp") as temp:
            repo = Path(temp) / "repo"
            os.chown(Path(temp), os.getuid(), os.getgid())
            storage = repo / "experiments/storage"
            storage.mkdir(parents=True)
            (repo / "tracked-input.txt").write_text("synthetic source\n", encoding="utf-8")
            attestation_path = storage / "storage-gate0-attestation.json"
            sidecar_path = storage / "storage-gate0-path-state.json.zlib"
            roots = [policy.DiscoveredProjectRoot(
                repo, policy.ROOT_CLASS_MANDATORY, "main_repo")]
            with mock.patch.object(gate0, "REPO", repo), \
                    mock.patch.object(gate0, "ATTESTATION", attestation_path), \
                    mock.patch.object(policy, "REPO", repo), \
                    mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
                    mock.patch.object(policy, "GATE0_PATH_STATE_SIDECAR_PATH", sidecar_path), \
                    mock.patch.object(policy, "_classified_project_roots", return_value=roots), \
                    mock.patch.object(policy, "_signed_persistent_output_snapshot",
                                      return_value=({"bindings": []}, (), [])), \
                    mock.patch.object(policy, "measure_project_usage",
                                      side_effect=_REAL_PROJECT_USAGE_MEASURER), \
                    mock.patch.object(policy, "_manifest_signing_key", return_value=self.key), \
                    mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB):
                attestation = {
                    "state_fingerprint": {
                        "fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes": 0},
                    "inventory_measurements": {
                        "fgmetal_scoped_allocated_inode_bytes": 0},
                }
                measurements = gate0._write_exact_live_storage_baseline(attestation)

            saved = json.loads(attestation_path.read_text(encoding="utf-8"))
            with mock.patch.object(policy, "GATE0_PATH_STATE_SIDECAR_PATH", sidecar_path), \
                    mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
                    mock.patch.object(policy, "_classified_project_roots", return_value=roots):
                snapshot = policy.read_gate_path_state_sidecar(saved)["nonlease_allocation_snapshot"]
            gate_row = next(row for row in snapshot["entries"]
                            if row["path"] == str(attestation_path))
            self.assertTrue(gate_row["volatile_gate_attestation_identity"])
            self.assertEqual(gate_row["allocated_bytes"], attestation_path.stat().st_blocks * 512)
            with mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path):
                snapshot_total = policy._allocation_snapshot_total_bytes(snapshot)
            self.assertEqual(snapshot_total, saved["measurements"][
                "fgmetal_scoped_observed_nonlease_allocated_inode_bytes"])
            self.assertNotIn("nonlease_allocation_snapshot", measurements)
            self.assertTrue(policy._verify_gate0_signature(saved))

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
                    "allocated_inode_deduplicated_bytes": 10 * policy.GIB,
                    "allocated_inode_deduplicated_bytes_excluding_paths": 10 * policy.GIB,
                    "nonlease_allocation_snapshot": _synthetic_allocation_snapshot(
                        10 * policy.GIB)}), \
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


class Gate0R96CanonicalSuiteEnvironmentTests(unittest.TestCase):
    def test_r96_canonical_suite_child_drops_live_plan_route_selectors(self) -> None:
        with tempfile.TemporaryDirectory(prefix="r96-gate-suite-env-") as temporary:
            report_path = Path(temporary) / "canonical.log"
            mocked_result = subprocess.CompletedProcess(
                args=["python", "-m", "unittest"], returncode=0,
                stdout="Ran 36 tests in 0.123s\n\nOK\n")
            with (mock.patch.dict(os.environ, {
                      "FG_METAL_R96_PLAN_PATH": "/tmp/plan25.json",
                      "FG_METAL_R95_PLAN_PATH": "/tmp/plan24.json",
                  }, clear=False),
                  mock.patch.object(gate0, "TEST_REPORT", report_path),
                  mock.patch.object(gate0.subprocess, "run", return_value=mocked_result) as run):
                self.assertEqual(gate0._run_test_suite(), hashlib.sha256(
                    mocked_result.stdout.encode()).hexdigest())
            child_environment = run.call_args.kwargs["env"]
            self.assertNotIn("FG_METAL_R96_PLAN_PATH", child_environment)
            self.assertNotIn("FG_METAL_R95_PLAN_PATH", child_environment)
            self.assertEqual(report_path.read_text(), mocked_result.stdout)


class Gate0CompletionEvidenceTests(unittest.TestCase):
    @staticmethod
    def _current_independent_review() -> dict[str, object]:
        reviewed_files = policy.g0c_reviewed_files(gate0.REPO, gate0.G0C_REVIEW_REQUIRED_FILES)
        return {"status": "PASS", "findings": [], "reviewed_files": reviewed_files,
                "reviewer_identity": "focused-schema-fixture"}

    def _verify_with_review(self, review: dict[str, object]) -> None:
        raw = (json.dumps(review, sort_keys=True) + "\n").encode()
        original = policy._read_file_nofollow

        def read(path: Path, *args, **kwargs):
            if path == gate0.HERE / policy.G0C_REVIEW_RECORD_NAME:
                return raw
            return original(path, *args, **kwargs)

        with mock.patch.object(policy, "_read_file_nofollow", side_effect=read):
            gate0._verify_g0c_completion_evidence()

    def test_stale_independent_review_cannot_be_attested(self) -> None:
        review = {"status": "PASS", "findings": [], "reviewed_files": {}}
        with mock.patch.object(gate0, "_read_json", return_value={
                "status": "FAIL"}):
            with self.assertRaisesRegex(RuntimeError, "independent review"):
                self._verify_with_review(review)

    def test_stale_required_review_hash_cannot_be_attested(self) -> None:
        for field in gate0.G0C_REVIEW_REQUIRED_FILES:
            with self.subTest(field=field):
                review = self._current_independent_review()
                review["reviewed_files"][field]["sha256"] = "0" * 64
                with self.assertRaisesRegex(RuntimeError, "does not bind current"):
                    self._verify_with_review(review)

    def test_missing_size_is_rejected_by_pre_live_completion_verifier(self) -> None:
        review = self._current_independent_review()
        del review["reviewed_files"]["source"]["size_bytes"]
        with self.assertRaisesRegex(RuntimeError, "does not bind current"):
            self._verify_with_review(review)

    def test_duplicate_json_key_is_rejected_by_pre_live_completion_verifier(self) -> None:
        raw = b'{"status":"PASS","status":"PASS","findings":[],"reviewed_files":{}}'
        original = policy._read_file_nofollow

        def read(path: Path, *args, **kwargs):
            if path == gate0.HERE / policy.G0C_REVIEW_RECORD_NAME:
                return raw
            return original(path, *args, **kwargs)

        with mock.patch.object(policy, "_read_file_nofollow", side_effect=read):
            with self.assertRaisesRegex(RuntimeError, "malformed"):
                gate0._verify_g0c_completion_evidence()

    def test_missing_required_review_file_cannot_be_attested(self) -> None:
        review = self._current_independent_review()
        del review["reviewed_files"]["build_retention_tests"]
        with self.assertRaisesRegex(RuntimeError, "omits or adds"):
            self._verify_with_review(review)

    def test_shared_capacity_policy_is_an_exact_required_review_input(self) -> None:
        self.assertEqual(
            gate0.G0C_REVIEW_REQUIRED_FILES["shared_capacity_policy"],
            "HOST-V3-SHARED-CAPACITY-POLICY.md",
        )
        review = self._current_independent_review()
        del review["reviewed_files"]["shared_capacity_policy"]
        with self.assertRaisesRegex(RuntimeError, "omits or adds"):
            self._verify_with_review(review)

    def test_r78_source_and_test_roster_is_shared_and_complete(self) -> None:
        self.assertEqual(gate0.G0C_REVIEW_REQUIRED_FILES,
                         policy.G0C_REVIEW_REQUIRED_FILES)
        expected_paths = {
            "storage_policy.py", "storage_gate0.py", "smoke_prefix_lease.py",
            "r6a_dependency_manifest.py", "run_r6a_execution.py",
            "test_storage_policy.py", "test_g0c_checks.py",
            "test_r6a_dependency_manifest.py", "test_run_r6a_execution.py",
        }
        roster_paths = set(gate0.G0C_REVIEW_REQUIRED_FILES.values())
        self.assertTrue(expected_paths <= roster_paths)

    def test_failed_consolidation_cannot_be_attested(self) -> None:
        review = self._current_independent_review()
        with mock.patch.object(gate0, "_read_json", side_effect=[{"status": "FAIL"}]), \
                mock.patch.object(policy, "_verify_storage_evidence", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "consolidation"):
                self._verify_with_review(review)


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
                "allocated_inode_deduplicated_bytes": policy.GATE0_PROJECT_BUDGET + 1,
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
