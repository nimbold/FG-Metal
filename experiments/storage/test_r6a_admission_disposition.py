#!/usr/bin/env python3
"""Synthetic tests for authenticated R6-A pre-BuildLease terminal dispositions."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

_STORAGE_DIR = Path(__file__).resolve().parent
if str(_STORAGE_DIR) not in sys.path:
    sys.path.insert(0, str(_STORAGE_DIR))

import build_retention
import storage_policy as policy


class R6AAdmissionDispositionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix=".r6a-admission-disposition-test-", dir=_STORAGE_DIR)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "repo"
        self.root.mkdir()
        self.storage = self.root / "experiments/storage"
        self.manifests = self.storage / "manifests"
        self.manifests.mkdir(parents=True)
        self.outputs = self.storage / "test-outputs"
        self.outputs.mkdir()
        self.support = self.storage / "support"
        self.support.mkdir()
        self.cache = Path(self.temp.name) / "cache"
        self.cache.mkdir()
        self.patches = [
            mock.patch.object(policy, "REPO", self.root),
            mock.patch.object(policy, "CACHE", self.cache),
            mock.patch.object(policy, "ADMISSION_TERMINAL_DISPOSITION_ROOT",
                              self.storage / "admission-dispositions"),
            mock.patch.object(policy, "_manifest_signing_key", return_value=b"k" * 32),
            mock.patch.object(policy, "_acquire_storage_allocation_lock", return_value=mock.Mock()),
            mock.patch.object(policy, "_release_storage_allocation_lock"),
        ]
        for patcher in self.patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.claim_path = self.storage / "claims" / "synthetic.claim"
        self.claim_path.parent.mkdir()
        self.claim = {"schema_version": 1, "admission_id": "synthetic"}
        self.claim_raw = policy._canonical_pretty_json_bytes(self.claim)
        self.claim_path.write_bytes(self.claim_raw)
        self.report = self.support / "r89-report.md"
        self.report.write_bytes(b"synthetic preserved R89 consolidated report\n")
        self.final_gate_path = self.storage / "preserved-final-gate.json"
        gate = {
            "schema_version": 1,
            "status": "PASS",
            "mode": "final",
            "prefixlease_smoke_status": "PASS",
            "prefixlease_smoke": {"status": "PASS"},
        }
        gate["gate0_hmac_sha256"] = policy._gate0_signature(gate)
        self.gate_raw = policy._canonical_pretty_json_bytes(gate)
        self.final_gate_path.write_bytes(self.gate_raw)
        self.build_lock_patch = mock.patch.object(build_retention, "_lock", return_value=(None, None))
        self.unlock_patch = mock.patch.object(build_retention, "_unlock")
        self.build_lock_patch.start()
        self.unlock_patch.start()
        self.addCleanup(self.build_lock_patch.stop)
        self.addCleanup(self.unlock_patch.stop)
        self._evidence_patch = mock.patch.object(
            policy, "_r6a_prebuildlease_execution_evidence", side_effect=self._execution_evidence)
        self._claim_patch = mock.patch.object(
            policy, "_r6a_prebuildlease_claim", side_effect=self._claim_evidence)
        self._evidence_patch.start()
        self._claim_patch.start()
        self.addCleanup(self._evidence_patch.stop)
        self.addCleanup(self._claim_patch.stop)
        self.created_receipts: dict[str, dict] = {}

    def _claim_evidence(self, manifest, manifest_raw):
        return self.claim, self.claim_raw, self.claim_path

    def _execution_evidence(self, manifest, *, retention_lock_fds=None):
        output = Path(manifest["output_dir"])
        allocated, tree_sha256 = policy._persistent_output_snapshot(output)
        output_state = policy._r6a_prebuildlease_output_state(output, allocated, tree_sha256)
        return {
            "build_id": manifest["r71_build_id"],
            "registry_directory": str(self.storage / "synthetic-build-registry"),
            "registry_absence_proven": True,
            "registry_scan": {
                "complete": True,
                "record_count": 0,
                "records": [],
                "records_sha256": hashlib.sha256(b"[]").hexdigest(),
            },
            "matched_registry_records": [],
            "paths": {
                "build_root": {"path": str(self.storage / "no-build-root"), "exists": False},
            },
            "output": output_state,
        }

    def _make_manifest(self, suffix: str, *, output: Path | None = None,
                       create_output: bool = True) -> tuple[str, Path, bytes]:
        admission_id = f"r6a-r87-admission-{suffix}"
        output_dir = output or self.outputs / admission_id
        if create_output:
            output_dir.mkdir(parents=True, exist_ok=True)
        persistent = 8 * policy.MIB
        estimates = {
            "expected_duration_seconds": 60,
            "expected_prefix_bytes": 0,
            "expected_build_bytes": 0,
            "estimated_persistent_bytes": persistent,
            "estimated_max_new_disk_bytes": persistent,
            "estimated_uncertainty_margin_bytes": policy.admission_uncertainty_margin_bytes(persistent),
            "estimated_projected_new_disk_bytes": persistent + policy.admission_uncertainty_margin_bytes(persistent),
            "project_budget_bytes": policy.PROJECT_BUDGET,
        }
        manifest = {
            **estimates,
            "experiment_id": admission_id,
            "decision": "ALLOW",
            "storage_policy_smoke": False,
            "expected_prefix_requirement": False,
            "expected_build_requirement": False,
            "candidate_hashes": {
                "r71_candidate_core_sha256": "a" * 64,
                "r71_dependency_plan_sha256": "b" * 64,
            },
            "build_session_plan_sha256": "c" * 64,
            "r71_build_id": f"r6a-r87-dual-lineage-{suffix}",
            "purpose": "synthetic authenticated pre-BuildLease disposition test",
            "created_at_unix": time.time(),
            "expected_prefix_requirement": False,
            "expected_prefix_bytes": 0,
            "expected_build_requirement": False,
            "expected_build_bytes": 0,
            "project_estimated_peak_bytes": persistent,
            "projected_free_bytes": 100 * policy.GIB,
            "projected_scoped_project_bytes": policy.GIB,
            "storage_admission_warnings": [],
            "project_budget_remaining_before_bytes": policy.PROJECT_BUDGET,
            "free_before_bytes": 100 * policy.GIB,
            "project_usage_before": {"allocated_inode_deduplicated_bytes": policy.GIB},
            "blockers": [],
            "warnings": [],
            "cleanup_policy": "synthetic",
            "retention_policy": "synthetic",
            "storage_justification": None,
            "persistent_growth_over_2g_approved": False,
            "persistent_growth_over_2g_approval_reason": None,
            "internal_storage_setup": False,
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
            "execution_admission_identity": {"admission_id": admission_id, "sha256": "d" * 64},
            "admission_claim": {"path": str(self.claim_path), "sha256": hashlib.sha256(self.claim_raw).hexdigest(),
                                "decision": "ALLOW", "build_id": f"r6a-r87-dual-lineage-{suffix}",
                                "plan_canonical_sha256": "c" * 64},
            "gate0_attestation_sha256": hashlib.sha256(self.gate_raw).hexdigest(),
            "output_dir": str(output_dir),
            "manifest_path": str(self.manifests / f"{admission_id}.json"),
        }
        manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
        raw = policy._canonical_pretty_json_bytes(manifest)
        path = self.manifests / f"{admission_id}.json"
        path.write_bytes(raw)
        path.chmod(0o600)
        return admission_id, output_dir, raw

    def _close(self, admission_id: str) -> dict:
        record = policy.close_r6a_admission_before_buildlease(
            admission_id,
            final_gate_path=self.final_gate_path,
            terminal_reason="No BuildLease was reserved for this synthetic admission.",
            support_evidence_paths={"r89_consolidated_report": self.report},
        )
        self.created_receipts[admission_id] = record
        return record

    def test_valid_close_preserves_original_allow_gate_and_releases_only_future_reservation(self) -> None:
        admission_id, output, original_manifest_raw = self._make_manifest("residual")
        record = self._close(admission_id)
        saved_manifest = (self.manifests / f"{admission_id}.json").read_bytes()
        saved_gate = self.final_gate_path.read_bytes()
        snapshot, _excluded, blockers = policy._signed_persistent_output_snapshot()
        binding = snapshot["bindings"][0]

        self.assertEqual(blockers, [])
        self.assertEqual(saved_manifest, original_manifest_raw)
        self.assertEqual(saved_gate, self.gate_raw)
        self.assertEqual(record["original_decision"], "ALLOW")
        self.assertEqual(record["terminal_execution_state"], "NEVER_STARTED")
        self.assertEqual(record["reserved_output_bytes"], 8 * policy.MIB)
        self.assertEqual(binding["output_allocated_bytes"], 0)
        self.assertFalse(binding["reservation_active"])
        self.assertEqual(binding["future_output_reservation_bytes"], 0)
        self.assertEqual(binding["prebuildlease_disposition_sha256"],
                         hashlib.sha256(policy._canonical_pretty_json_bytes(record)).hexdigest())
        self.assertEqual(binding["released_future_output_reservation_bytes"], 8 * policy.MIB)

    def test_missing_or_invalid_original_allow_refuses_closure(self) -> None:
        missing_id, _missing_output, _raw = self._make_manifest("missing-manifest")
        policy._experiment_manifest_path(missing_id).unlink()
        with self.assertRaises(FileNotFoundError):
            self._close(missing_id)

        invalid_id, _invalid_output, _raw = self._make_manifest("invalid-hmac")
        invalid_path = policy._experiment_manifest_path(invalid_id)
        invalid_manifest = json.loads(invalid_path.read_bytes())
        invalid_manifest["manifest_hmac_sha256"] = "0" * 64
        invalid_path.write_bytes(policy._canonical_pretty_json_bytes(invalid_manifest))
        with self.assertRaisesRegex(ValueError, "signed R6-A ALLOW"):
            self._close(invalid_id)
        self.assertFalse(policy._r6a_prebuildlease_disposition_path(invalid_id).exists())

    def test_buildlease_session_graph_finalizing_success_and_cas_evidence_refuse(self) -> None:
        # Exercise the production evidence scanner against isolated roots. A
        # matching lease record, any build-root graph/session tree, or any
        # terminal manifest state must prevent terminal disposition.
        self._evidence_patch.stop()
        lease_root = self.storage / "build-leases"
        build_root = self.storage / "builds"
        manifest_root = self.storage / "build-manifests"
        receipt_root = self.storage / "build-receipts"
        for directory in (lease_root, build_root, manifest_root, receipt_root):
            directory.mkdir()
        lease_fd = os.open(lease_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        self.addCleanup(os.close, lease_fd)
        lock_patch = mock.patch.object(
            build_retention, "_lock", return_value=(lease_fd, -1))
        verify_patch = mock.patch.object(
            build_retention, "_verify_named_retention_lock")
        seal_patch = mock.patch.object(build_retention, "_verify_seal", return_value=True)
        roots_patch = mock.patch.multiple(
            build_retention,
            LEASE_ROOT=lease_root,
            BUILD_ROOT=build_root,
            MANIFEST_ROOT=manifest_root,
            RECEIPT_ROOT=receipt_root,
        )
        lock_patch.start()
        verify_patch.start()
        seal_patch.start()
        roots_patch.start()
        self.addCleanup(roots_patch.stop)
        self.addCleanup(seal_patch.stop)
        self.addCleanup(verify_patch.stop)
        self.addCleanup(lock_patch.stop)

        evidence_cases = (
            ("buildlease", "registry"),
            ("session", "registry"),
            ("graph", "build_root"),
            ("finalizing", "build_manifest_evidence"),
            ("success", "build_manifest"),
            ("cas_promotion", "build_manifest"),
        )
        for suffix, kind in evidence_cases:
            with self.subTest(evidence=suffix):
                admission_id, _output, _raw = self._make_manifest(f"evidence-{suffix}")
                build_id = f"r6a-r87-dual-lineage-evidence-{suffix}"
                manifest_path = policy._experiment_manifest_path(admission_id)
                manifest = json.loads(manifest_path.read_bytes())
                manifest["r71_build_id"] = build_id
                manifest["admission_claim"]["build_id"] = build_id
                manifest["manifest_hmac_sha256"] = policy._manifest_signature(manifest)
                manifest_raw = policy._canonical_pretty_json_bytes(manifest)
                manifest_path.write_bytes(manifest_raw)
                registry_path = lease_root / f"{build_id}.json"
                if kind == "registry":
                    record = ({"build_id": build_id} if suffix == "buildlease" else {
                        "build_id": "other-build",
                        "session_v3": {"admission_identity": {"admission_id": admission_id}},
                    })
                    registry_path.write_text(json.dumps(record), encoding="utf-8")
                elif kind == "build_root":
                    (build_root / build_id / "graph-output").mkdir(parents=True)
                elif kind == "build_manifest_evidence":
                    (manifest_root / f"{build_id}.evidence").write_bytes(b"FINALIZING")
                else:
                    (manifest_root / f"{build_id}.json").write_bytes(b"SUCCESS/CAS manifest")
                with self.assertRaises((ValueError, RuntimeError)):
                    policy.close_r6a_admission_before_buildlease(
                        admission_id, final_gate_path=self.final_gate_path,
                        terminal_reason="Synthetic pre-BuildLease refusal case.",
                        support_evidence_paths={"r89_consolidated_report": self.report},
                    )
                self.assertFalse(policy._r6a_prebuildlease_disposition_path(admission_id).exists())

    def test_wrong_final_gate_and_receipt_candidate_or_plan_binding_refuse(self) -> None:
        wrong_gate_id, _output, _raw = self._make_manifest("wrong-final-gate")
        wrong_gate = json.loads(self.gate_raw)
        wrong_gate["fixture_variant"] = "different authenticated final gate"
        wrong_gate["gate0_hmac_sha256"] = policy._gate0_signature(wrong_gate)
        wrong_gate_path = self.support / "different-final-gate.json"
        wrong_gate_path.write_bytes(policy._canonical_pretty_json_bytes(wrong_gate))
        with self.assertRaisesRegex(ValueError, "ALLOW binding"):
            policy.close_r6a_admission_before_buildlease(
                wrong_gate_id, final_gate_path=wrong_gate_path,
                terminal_reason="Mismatched Final Gate digest.",
                support_evidence_paths={"r89_consolidated_report": self.report},
            )

        for suffix, field in (("wrong-candidate", "candidate_core_sha256"),
                              ("wrong-plan", "plan_raw_sha256")):
            with self.subTest(binding=field):
                admission_id, _output, _raw = self._make_manifest(suffix)
                self._close(admission_id)
                receipt_path = policy._r6a_prebuildlease_disposition_path(admission_id)
                receipt = json.loads(receipt_path.read_bytes())
                receipt.pop(policy.R6A_PREBUILDLEASE_DISPOSITION_HMAC_FIELD)
                receipt[field] = "f" * 64
                receipt = policy._seal_storage_evidence(
                    receipt, policy.R6A_PREBUILDLEASE_DISPOSITION_HMAC_FIELD)
                receipt_path.write_bytes(policy._canonical_pretty_json_bytes(receipt))
                manifest_raw = policy._experiment_manifest_path(admission_id).read_bytes()
                manifest = json.loads(manifest_raw)
                disposition, blocker = policy._read_r6a_prebuildlease_disposition(
                    manifest, manifest_raw, Path(manifest["output_dir"]))
                self.assertIsNone(disposition)
                self.assertIn("does not match the unchanged ALLOW", blocker or "")

    def test_duplicate_creation_and_tampered_receipt_fail_closed(self) -> None:
        admission_id, _output, _raw = self._make_manifest("duplicate")
        self._close(admission_id)
        with self.assertRaises(FileExistsError):
            self._close(admission_id)
        live_manifest_raw = (self.manifests / f"{admission_id}.json").read_bytes()
        live_manifest = json.loads(live_manifest_raw)
        with self.assertRaisesRegex(RuntimeError, "terminally closed"):
            policy._reject_r6a_admission_with_terminal_disposition(
                live_manifest, live_manifest_raw)

        receipt_path = policy._r6a_prebuildlease_disposition_path(admission_id)
        receipt = json.loads(receipt_path.read_bytes())
        receipt["terminal_reason"] = "edited without a matching HMAC"
        receipt_path.write_bytes(policy._canonical_pretty_json_bytes(receipt))
        manifest_raw = (self.manifests / f"{admission_id}.json").read_bytes()
        manifest = json.loads(manifest_raw)
        disposition, blocker = policy._read_r6a_prebuildlease_disposition(
            manifest, manifest_raw, Path(manifest["output_dir"]))
        self.assertIsNone(disposition)
        self.assertIn("HMAC", blocker or "")
        with self.assertRaisesRegex(RuntimeError, "disposition is invalid"):
            policy._reject_r6a_admission_with_terminal_disposition(manifest, manifest_raw)

    def test_contradictory_later_execution_evidence_invalidates_receipt(self) -> None:
        admission_id, _output, _raw = self._make_manifest("contradiction")
        self._close(admission_id)
        manifest_raw = (self.manifests / f"{admission_id}.json").read_bytes()
        manifest = json.loads(manifest_raw)
        original = self._execution_evidence(manifest)
        changed = json.loads(json.dumps(original))
        changed["matched_registry_records"].append({"registry_name": "contradictory.json"})
        with mock.patch.object(policy, "_r6a_prebuildlease_execution_evidence", return_value=changed):
            disposition, blocker = policy._read_r6a_prebuildlease_disposition(
                manifest, manifest_raw, Path(manifest["output_dir"]))
        self.assertIsNone(disposition)
        self.assertIn("contradictory later BuildLease", blocker or "")

    def test_closed_roots_do_not_overlap_active_roots_but_active_overlap_still_blocks(self) -> None:
        closed_id, parent_output, _raw = self._make_manifest("closed-parent")
        active_output = parent_output / "active-child"
        active_id, _, _raw = self._make_manifest(
            "active-child", output=active_output, create_output=False)
        self._close(closed_id)
        snapshot, _excluded, blockers = policy._signed_persistent_output_snapshot()
        self.assertEqual(blockers, [])
        self.assertEqual({row["experiment_id"] for row in snapshot["bindings"]}, {closed_id, active_id})

        second_active_id, _, _raw = self._make_manifest(
            "active-equal", output=active_output, create_output=False)
        _snapshot, _excluded, blockers = policy._signed_persistent_output_snapshot()
        self.assertTrue(any("reservations overlap" in item for item in blockers))
        self.assertNotEqual(active_id, second_active_id)

    def test_nonempty_output_refuses_closure_and_postclose_mutation_stays_counted(self) -> None:
        refused_id, refused_output, _raw = self._make_manifest("nonempty-refusal")
        (refused_output / "authoritative.bin").write_bytes(b"not proven terminal")
        with self.assertRaisesRegex(ValueError, "nonempty"):
            self._close(refused_id)
        self.assertFalse(policy._r6a_prebuildlease_disposition_path(refused_id).exists())

        closed_id, output, _raw = self._make_manifest("postclose-mutation")
        self._close(closed_id)
        mutated = output / "later-output.bin"
        mutated.write_bytes(b"physical bytes must remain counted" * 256)
        allocated, _tree = policy._persistent_output_snapshot(output)
        manifest_raw = (self.manifests / f"{closed_id}.json").read_bytes()
        manifest = json.loads(manifest_raw)
        disposition, blocker = policy._read_r6a_prebuildlease_disposition(
            manifest, manifest_raw, output)
        snapshot, exclusions, blockers = policy._signed_persistent_output_snapshot()
        self.assertIsNone(disposition)
        self.assertIn("became nonempty", blocker or "")
        self.assertTrue(any("disposition is invalid" in item for item in blockers))
        self.assertNotIn(output, exclusions)
        binding_ids = {row["experiment_id"] for row in snapshot["bindings"]}
        self.assertNotIn(closed_id, binding_ids)
        self.assertIn(refused_id, binding_ids)
        self.assertGreater(allocated, 0)

    def test_final_gate_and_support_evidence_are_mandatory(self) -> None:
        admission_id, _output, _raw = self._make_manifest("refusal")
        with self.assertRaisesRegex(ValueError, "preserved Final Gate"):
            policy.close_r6a_admission_before_buildlease(
                admission_id, final_gate_path=policy.GATE0_ATTESTATION_PATH,
                terminal_reason="never started", support_evidence_paths={"r89_consolidated_report": self.report})
        with self.assertRaisesRegex(ValueError, "support-evidence"):
            policy.close_r6a_admission_before_buildlease(
                admission_id, final_gate_path=self.final_gate_path,
                terminal_reason="never started", support_evidence_paths={})
        self.assertFalse(policy._r6a_prebuildlease_disposition_path(admission_id).exists())

    def test_gen2_basis_requires_and_binds_preserved_closeout(self) -> None:
        admission_id, _output, _raw = self._make_manifest("gen2-basis")
        closeout = self.support / "generation2-r6a-closeout.json"
        closeout.write_bytes(b"synthetic preserved generation2 closeout\n")
        record = policy.close_r6a_admission_before_buildlease(
            admission_id,
            final_gate_path=self.final_gate_path,
            terminal_reason="No BuildLease was reserved for this synthetic admission.",
            support_evidence_paths={
                "r89_consolidated_report": self.report,
                "generation2_closeout": closeout,
            },
        )
        self.assertEqual(record["historical_disposition_basis"],
                         "R89_REPORT_AND_GENERATION2_CLOSEOUT_PLUS_CURRENT_COMPLETE_BUILD_HISTORY_SCAN")
        self.assertEqual(record["support_evidence"]["generation2_closeout"]["sha256"],
                         hashlib.sha256(closeout.read_bytes()).hexdigest())

    def test_preserved_gate_without_final_pass_semantics_is_refused(self) -> None:
        admission_id, _output, _raw = self._make_manifest("bad-gate")
        altered = json.loads(self.gate_raw)
        altered["mode"] = "preflight"
        altered["gate0_hmac_sha256"] = policy._gate0_signature(altered)
        self.final_gate_path.write_bytes(policy._canonical_pretty_json_bytes(altered))
        with self.assertRaisesRegex(ValueError, "PASS/final state"):
            policy.close_r6a_admission_before_buildlease(
                admission_id,
                final_gate_path=self.final_gate_path,
                terminal_reason="never started",
                support_evidence_paths={"r89_consolidated_report": self.report},
            )

    def test_session_plan_accepts_exact_plan9_r87_output_contract(self) -> None:
        admission_id = "r6a-r87-admission-20261025t221237z-r90-c11-33863859"
        build_id = "r6a-r87-dual-lineage-plan9-contract-test"
        plan = {
            "schema_version": build_retention.BUILD_SESSION_SCHEMA_VERSION,
            "executor_mode": build_retention.BUILD_SESSION_EXECUTOR_MODE,
            "admission_identity": {"admission_id": admission_id, "sha256": "a" * 64},
            "source_identity": {},
            "build_id": build_id,
            "build_workspace": str(self.root / "build"),
            "reservation_bytes": 1,
            "stage_root": str(self.root / "stage"),
            "output_root": str(self.root / "output"),
            "environment": {},
            "tools": {},
            "steps": [],
            "admission_output_contract": "R90_ADMISSION_UNIQUE_SIBLING_ROOT_V1",
            "admission_output_dir": (
                f"experiments/storage/r6a-admission-outputs/{admission_id}"),
        }
        with self.assertRaisesRegex(build_retention.RetentionError, "source revision"):
            build_retention._session_validate_plan(
                plan, build_id=build_id, build_path=self.root / "build",
                source_tree_hash="b" * 64, expected_bytes=1)

        wrong_contract = dict(plan)
        wrong_contract["admission_output_contract"] = "R90_ADMISSION_UNIQUE_SIBLING_V1"
        with self.assertRaisesRegex(build_retention.RetentionError, "R90 admission output contract"):
            build_retention._session_validate_plan(
                wrong_contract, build_id=build_id, build_path=self.root / "build",
                source_tree_hash="b" * 64, expected_bytes=1)


if __name__ == "__main__":
    unittest.main()
