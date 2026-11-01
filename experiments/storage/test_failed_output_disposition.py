#!/usr/bin/env python3
"""Focused synthetic tests for authenticated failed smoke-output disposition."""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import storage_policy as policy


class FailedOutputDispositionTests(unittest.TestCase):
    """Exercise the production Gate snapshot and disposition verifier in a temp repo."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(
            prefix="fgmetal-failed-output-disposition-", dir="/private/tmp")
        self.addCleanup(self.temp.cleanup)
        base = Path(self.temp.name)
        self.repo = base / "repo"
        self.storage = self.repo / "experiments/storage"
        self.manifests = self.storage / "manifests"
        self.smoke_runs = self.storage / "smoke-runs"
        self.claims = self.storage / "smoke-claims"
        self.dispositions = self.storage / "failed-output-dispositions"
        for directory in (self.manifests, self.smoke_runs, self.claims):
            directory.mkdir(parents=True)
        self.key_path = base / "signing-key"
        replacements = {
            "REPO": self.repo,
            "CACHE": base / "cache",
            "MANIFEST_SIGNING_KEY_PATH": self.key_path,
            "GATE0_ATTESTATION_PATH": self.storage / "storage-gate0-attestation.json",
            "SMOKE_CLAIM_ROOT": self.claims,
            "FAILED_OUTPUT_DISPOSITION_ROOT": self.dispositions,
        }
        self.patches = []
        for name, value in replacements.items():
            patcher = mock.patch.object(policy, name, value)
            patcher.start()
            self.patches.append(patcher)
            self.addCleanup(patcher.stop)
        root_patch = mock.patch.object(
            policy, "_classified_project_roots", return_value=[policy.DiscoveredProjectRoot(
                self.repo, policy.ROOT_CLASS_MANDATORY, "synthetic-failed-output-repo")])
        root_patch.start()
        self.addCleanup(root_patch.stop)
        policy._manifest_signing_key(create=True)

        self.experiment_id = policy.LEGACY_FAILED_OUTPUT_DISPOSITION_RUN_ID
        self.assertEqual(
            self.experiment_id,
            "step11d3-storage-policy-smoke-20261024-69574b75b1a5456e")
        self.assertEqual(policy.LEGACY_FAILED_OUTPUT_DISPOSITION_SOURCE_SHA256, {
            "legacy_diagnostic_transcript": (
                "d4ac1a286893758080006f9964fd4b1c7c89654508cd7b965aa1f86d209bc22d"),
            "legacy_reconciliation": (
                "c83842f848eef87b3394ea8394ac1758b73d190c04fe3a36e23855e61334fbb1"),
        })
        # Synthetic evidence exercises the same structured refusal and
        # pre-materialization fields while substituting fixture-local hashes.
        transcript = (
            f"{self.experiment_id}: historical failure; Gate refused PrefixLease\n".encode())
        transcript_path = base / "legacy_diagnostic_transcript.txt"
        transcript_path.write_bytes(transcript)
        transcript_path.chmod(0o600)
        self.source_paths = {"legacy_diagnostic_transcript": transcript_path}

        self.output = self.smoke_runs / self.experiment_id
        self.output.mkdir(mode=0o700)
        self.output_file = self.output / "diagnostic.bin"
        self.output_file.write_bytes(b"synthetic retained failed output\n")
        self.output_file.chmod(0o600)

        gate = {
            "schema_version": 1,
            "status": "PREFLIGHT_PASS",
            "mode": "preflight",
            "prefixlease_smoke_status": "PENDING",
            "permitted_smoke_experiment_id": self.experiment_id,
            "created_at_unix": time.time(),
        }
        gate["gate0_hmac_sha256"] = policy._gate0_signature(gate)
        self.gate_bytes = policy._canonical_pretty_json_bytes(gate)
        self.gate_hash = hashlib.sha256(self.gate_bytes).hexdigest()
        self.gate_path = self.output / "gate0-preflight-attestation.json"
        self.gate_path.write_bytes(self.gate_bytes)
        self.gate_path.chmod(0o600)
        policy.GATE0_ATTESTATION_PATH.write_bytes(self.gate_bytes)
        policy.GATE0_ATTESTATION_PATH.chmod(0o600)

        claim = policy._seal_storage_evidence({
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "claim_path": str(policy._storage_smoke_claim_path(self.experiment_id)),
            "preflight_attestation_path": str(policy.GATE0_ATTESTATION_PATH),
            "preflight_attestation_sha256": self.gate_hash,
            "preflight_created_at_unix": gate["created_at_unix"],
            "claimed_at_unix": time.time(),
            "claim_reason": "synthetic consumed historical smoke nonce",
            "pid": os.getpid(),
        }, "smoke_claim_hmac_sha256")
        claim_bytes = policy._canonical_pretty_json_bytes(claim)
        claim_path = policy._storage_smoke_claim_path(self.experiment_id)
        claim_path.write_bytes(claim_bytes)
        claim_path.chmod(0o600)
        manifest_path = policy._experiment_manifest_path(self.experiment_id)
        output_bytes = policy.STORAGE_SMOKE_OUTPUT_RESERVATION_BYTES
        self.manifest = {
            "experiment_id": self.experiment_id,
            "purpose": "synthetic failed-output disposition test",
            "candidate_hashes": {},
            "created_at_unix": time.time(),
            "expected_duration_seconds": 60,
            "expected_prefix_requirement": False,
            "expected_prefix_bytes": 0,
            "expected_build_requirement": False,
            "expected_build_bytes": 0,
            "estimated_max_new_disk_bytes": output_bytes,
            "estimated_uncertainty_margin_bytes": policy.admission_uncertainty_margin_bytes(output_bytes),
            "estimated_projected_new_disk_bytes": output_bytes + policy.admission_uncertainty_margin_bytes(output_bytes),
            "project_estimated_peak_bytes": output_bytes,
            "projected_free_bytes": 100 * policy.GIB,
            "projected_scoped_project_bytes": policy.GIB,
            "storage_admission_warnings": [],
            "estimated_persistent_bytes": output_bytes,
            "project_budget_bytes": policy.PROJECT_BUDGET,
            "project_budget_remaining_before_bytes": policy.PROJECT_BUDGET,
            "free_before_bytes": 100 * policy.GIB,
            "project_usage_before": {"allocated_inode_deduplicated_bytes": policy.GIB},
            "output_dir": str(self.output),
            "decision": "ALLOW",
            "blockers": [],
            "warnings": [],
            "cleanup_policy": "synthetic fixture",
            "retention_policy": "synthetic fixture",
            "storage_justification": None,
            "persistent_growth_over_2g_approved": False,
            "persistent_growth_over_2g_approval_reason": None,
            "internal_storage_setup": False,
            "storage_policy_smoke": True,
            "gate0_attestation_sha256": self.gate_hash,
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
            "manifest_path": str(manifest_path),
            "smoke_nonce_claim_path": str(policy._storage_smoke_claim_path(self.experiment_id)),
            "smoke_nonce_claim_sha256": hashlib.sha256(claim_bytes).hexdigest(),
        }
        self.manifest["manifest_hmac_sha256"] = policy._manifest_signature(self.manifest)
        manifest_path.write_bytes(policy._canonical_pretty_json_bytes(self.manifest))
        manifest_path.chmod(0o600)
        self.manifest_path = manifest_path
        self.manifest_bytes = manifest_path.read_bytes()
        self.claim = claim

        self.original_snapshot = policy._persistent_output_snapshot
        _actual_allocation, output_tree_sha256 = self.original_snapshot(self.output)
        output_allocated = 78_589_952
        reconciliation = {
            "schema_version": 1,
            "experiment_id": self.experiment_id,
            "status": "STOPPED_AFTER_SINGLE_PREFIXLEASE_FAILURE",
            "attempts": {"gate_preflight": 1, "prefix_materialization": 0,
                         "prefixlease_smoke": 1},
            "failure": {
                "exit_status": 1,
                "blockers": [
                    "current-run output exceeds its signed persistent-output reservation "
                    "(reported twice)",
                ],
                "expected_persistent_bytes": output_bytes,
                "gate_attestation_copy_sha256": self.gate_hash,
                "log_sha256": hashlib.sha256(transcript).hexdigest(),
                "output_bytes": output_allocated,
                "output_overage_bytes": output_allocated - output_bytes,
                "output_tree_sha256": output_tree_sha256,
            },
            "gate": {"allocated_bytes": output_allocated,
                     "sha256": self.gate_hash,
                     "signature_valid": True, "status": "PREFLIGHT_PASS"},
            "nonce_claim": {"consumed": True, "hmac_valid": True,
                            "sha256": hashlib.sha256(claim_bytes).hexdigest()},
            "reconciliation": {
                "mode": "read-only exact-ID state check",
                "canonical_recovery_invoked": False,
                "reason": ("PrefixLease.__enter__ failed before creating an active marker, "
                           "prefix, or clone; no active marker or prefix was created."),
            },
            "resources": {"active_marker_exists": False,
                          "cleanup_receipt_exists": False,
                          "no_cleanup_mutation_required": True,
                          "prefix_exists": False,
                          "clone_temporary_paths": []},
        }
        reconciliation_bytes = (
            json.dumps(reconciliation, indent=2, sort_keys=True) + "\n").encode()
        reconciliation_path = base / "legacy_reconciliation.json"
        reconciliation_path.write_bytes(reconciliation_bytes)
        reconciliation_path.chmod(0o600)
        self.sources = {
            "legacy_diagnostic_transcript": transcript,
            "legacy_reconciliation": reconciliation_bytes,
        }
        self.source_paths["legacy_reconciliation"] = reconciliation_path
        source_hashes = {role: hashlib.sha256(raw).hexdigest()
                         for role, raw in self.sources.items()}
        patcher = mock.patch.object(
            policy, "LEGACY_FAILED_OUTPUT_DISPOSITION_SOURCE_SHA256", source_hashes)
        patcher.start()
        self.addCleanup(patcher.stop)

        def exact_r77_measurement(
            path: Path, *, historical_device_alias: tuple[int, int] | None = None,
        ) -> tuple[int, str]:
            _allocated, tree_hash = self.original_snapshot(
                path, historical_device_alias=historical_device_alias)
            if Path(path) == self.output:
                return 78_589_952, tree_hash
            return _allocated, tree_hash

        self.measurement_patch = mock.patch.object(
            policy, "_persistent_output_snapshot", side_effect=exact_r77_measurement)
        self.measurement_patch.start()
        self.addCleanup(self.measurement_patch.stop)

    def _adjudicate(self) -> dict:
        return policy.adjudicate_failed_persistent_output(
            self.experiment_id, self.source_paths)

    def _snapshot(self):
        return policy._signed_persistent_output_snapshot()

    def _rewrite_disposition(self, **changes: object) -> None:
        path = policy._failed_output_disposition_path(self.experiment_id)
        record = json.loads(path.read_bytes())
        record.pop(policy.FAILED_OUTPUT_DISPOSITION_HMAC_FIELD)
        record.update(changes)
        record[policy.FAILED_OUTPUT_DISPOSITION_HMAC_FIELD] = policy._storage_evidence_signature(
            record, policy.FAILED_OUTPUT_DISPOSITION_HMAC_FIELD)
        policy._write_json(path, record)

    def test_active_invalid_failed_output_refuses_gate(self) -> None:
        snapshot, exclusions, blockers = self._snapshot()
        self.assertTrue(any("exceeds its persistent reservation" in blocker
                            for blocker in blockers))
        self.assertEqual(snapshot["bindings"], ())
        self.assertNotIn(self.output, exclusions)
        self.assertNotIn(self.manifest_path, exclusions)

    def test_authenticated_failed_disposition_is_evidence_only_and_capacity_stays_charged(self) -> None:
        before = policy.measure_project_usage()["allocated_inode_deduplicated_bytes"]
        record = self._adjudicate()
        snapshot, exclusions, blockers = self._snapshot()
        self.assertEqual(blockers, [])
        self.assertEqual(len(snapshot["bindings"]), 1)
        binding = snapshot["bindings"][0]
        self.assertEqual(binding["lifecycle_state"], "FAILED_RETIRED_EVIDENCE")
        self.assertEqual(binding["output_reservation_bytes"], 33_554_432)
        self.assertEqual(binding["output_allocated_bytes"], 78_589_952)
        self.assertEqual(binding["output_content_sha256"], record["output_content_sha256"])
        self.assertEqual(record["overrun_bytes"], 45_035_520)
        self.assertEqual(record["run_outcome"], "FAILED")
        self.assertEqual(record["acceptance"], "UNACCEPTED")
        self.assertFalse(record["execution_authorized"])
        self.assertFalse(record["accepted_success"])
        self.assertFalse(record["native_signed_refusal_present"])
        self.assertEqual(record["provenance_class"], "POST_HOC_OPERATOR_ADJUDICATION")
        self.assertIn(self.output, exclusions)
        self.assertIn(self.manifest_path, exclusions)
        after = policy.measure_project_usage()["allocated_inode_deduplicated_bytes"]
        custody_bytes = sum(Path(row["custody_path"]).stat().st_blocks * 512
                            for row in record["source_evidence"])
        self.assertGreaterEqual(after, before + custody_bytes)
        self.assertEqual(self.manifest_path.read_bytes(), self.manifest_bytes)
        self.assertEqual(policy._read_storage_smoke_claim(self.experiment_id)[1],
                         policy._canonical_pretty_json_bytes(self.claim))

    def test_legacy_apfs_device_alias_requires_exact_other_identity_and_tree(self) -> None:
        record = self._adjudicate()
        current_identity = list(policy._storage_entry_identity(self.output.lstat()))
        current_device = current_identity[0]
        recorded_device = current_device + 2
        recorded_identity = list(current_identity)
        recorded_identity[0] = recorded_device
        with (mock.patch.object(policy, "HISTORICAL_TERMINAL_APFS_OLD_DEVICE", recorded_device),
              mock.patch.object(
                  policy, "_historical_terminal_apfs_device_alias_matches", return_value=True)):
            _allocated, recorded_tree = policy._persistent_output_snapshot(
                self.output,
                historical_device_alias=(
                    current_device, recorded_device))

        rows = [dict(row) for row in record["source_evidence"]]
        for row in rows:
            row["custody_identity"] = list(row["custody_identity"])
            row["custody_identity"][0] = recorded_device
        self._rewrite_disposition(
            output_identity=recorded_identity,
            output_tree_sha256=recorded_tree,
            source_evidence=rows,
        )
        with (mock.patch.object(policy, "HISTORICAL_TERMINAL_APFS_OLD_DEVICE", recorded_device),
              mock.patch.object(
                    policy, "_historical_terminal_apfs_device_alias_matches", return_value=True),
              mock.patch.object(policy, "_legacy_reconciliation_matches", return_value=True)):
            snapshot, exclusions, blockers = self._snapshot()
        self.assertEqual(blockers, [])
        self.assertEqual(len(snapshot["bindings"]), 1)
        self.assertIn(self.output, exclusions)

        with (mock.patch.object(policy, "HISTORICAL_TERMINAL_APFS_OLD_DEVICE", recorded_device),
              mock.patch.object(
                    policy, "_historical_terminal_apfs_device_alias_matches", return_value=False),
              mock.patch.object(policy, "_legacy_reconciliation_matches", return_value=True)):
            _snapshot, _exclusions, blockers = self._snapshot()
        self.assertTrue(any("failed-output disposition is invalid" in item
                            for item in blockers))

    def test_legacy_apfs_device_alias_normalizes_file_symlink_identity(self) -> None:
        link = self.output / "diagnostic-link"
        link.symlink_to(self.output_file)
        current_device = self.output.lstat().st_dev
        old_device = current_device + 2
        original_identity = policy._storage_entry_identity

        def historical_identity(info: os.stat_result) -> tuple[int, ...]:
            identity = list(original_identity(info))
            identity[0] = old_device
            return tuple(identity)

        with mock.patch.object(policy, "_storage_entry_identity", side_effect=historical_identity):
            _expected_allocated, expected_tree = policy._persistent_output_snapshot(self.output)
        with (mock.patch.object(policy, "HISTORICAL_TERMINAL_APFS_OLD_DEVICE", old_device),
              mock.patch.object(
                  policy, "_historical_terminal_apfs_device_alias_matches", return_value=True)):
            _actual_allocated, actual_tree = policy._persistent_output_snapshot(
                self.output, historical_device_alias=(current_device, old_device))
        self.assertEqual(actual_tree, expected_tree)

    def test_unauthenticated_tamper_refuses(self) -> None:
        self._adjudicate()
        receipt_path = policy._failed_output_disposition_path(self.experiment_id)
        receipt_path.write_bytes(receipt_path.read_bytes() + b"tamper")
        _snapshot, _exclusions, blockers = self._snapshot()
        self.assertTrue(any("failed-output disposition is invalid" in blocker
                            for blocker in blockers))

    def test_missing_custody_target_refuses(self) -> None:
        self._adjudicate()
        custody_path = (policy._failed_output_disposition_path(self.experiment_id).parent
                        / "custody/legacy_reconciliation.bin")
        custody_path.unlink()
        _snapshot, _exclusions, blockers = self._snapshot()
        self.assertTrue(any("failed-output disposition is invalid" in blocker
                            for blocker in blockers))

    def test_output_digest_change_refuses(self) -> None:
        self._adjudicate()
        with self.output_file.open("ab") as stream:
            stream.write(b" changed")
        _snapshot, _exclusions, blockers = self._snapshot()
        self.assertTrue(any("exceeds its persistent reservation" in blocker
                            or "failed-output disposition is invalid" in blocker
                            for blocker in blockers))

    def test_wrong_run_binding_refuses(self) -> None:
        self._adjudicate()
        self._rewrite_disposition(experiment_id="step11d3-storage-policy-smoke-20261024-0000000000000000")
        _snapshot, _exclusions, blockers = self._snapshot()
        self.assertTrue(any("failed-output disposition is invalid" in blocker
                            for blocker in blockers))

    def test_boolean_schema_version_refuses(self) -> None:
        self._adjudicate()
        self._rewrite_disposition(schema_version=True)
        _snapshot, _exclusions, blockers = self._snapshot()
        self.assertTrue(any("failed-output disposition is invalid" in blocker
                            for blocker in blockers))

    def test_repeated_adjudication_does_not_rewrite_receipt(self) -> None:
        self._adjudicate()
        receipt_path = policy._failed_output_disposition_path(self.experiment_id)
        original = receipt_path.read_bytes()
        with self.assertRaises(FileExistsError):
            self._adjudicate()
        self.assertEqual(receipt_path.read_bytes(), original)

    def test_success_retirement_refuses(self) -> None:
        self._adjudicate()
        self._rewrite_disposition(run_outcome="SUCCESS")
        _snapshot, _exclusions, blockers = self._snapshot()
        self.assertTrue(any("failed-output disposition is invalid" in blocker
                            for blocker in blockers))

    def test_failed_to_accepted_conversion_refuses(self) -> None:
        self._adjudicate()
        self._rewrite_disposition(acceptance="ACCEPTED", accepted_success=True,
                                  execution_authorized=True)
        _snapshot, _exclusions, blockers = self._snapshot()
        self.assertTrue(any("failed-output disposition is invalid" in blocker
                            for blocker in blockers))

    def test_adjudicator_rejects_success_evidence(self) -> None:
        report = self.output / "prefix-smoke-final.json"
        report.write_bytes(policy._canonical_pretty_json_bytes({
            "experiment_id": self.experiment_id, "status": "PASS", "accepted_success": True,
        }))
        with self.assertRaisesRegex(ValueError, "success report"):
            self._adjudicate()

    def test_interrupted_custody_publication_can_resume_from_exact_copy(self) -> None:
        custody_dir = (policy._failed_output_disposition_path(self.experiment_id).parent
                       / "custody")
        custody_dir.mkdir(parents=True, mode=0o700)
        existing = custody_dir / "legacy_diagnostic_transcript.bin"
        existing.write_bytes(self.sources["legacy_diagnostic_transcript"])
        existing.chmod(0o600)

        record = self._adjudicate()

        row = next(item for item in record["source_evidence"]
                   if item["role"] == "legacy_diagnostic_transcript")
        self.assertEqual(Path(row["custody_path"]), existing)
        self.assertEqual(existing.read_bytes(), self.sources["legacy_diagnostic_transcript"])
        self.assertTrue(policy._verify_storage_evidence(
            record, policy.FAILED_OUTPUT_DISPOSITION_HMAC_FIELD))

    def test_unexpected_partial_custody_entry_refuses_adjudication(self) -> None:
        custody_dir = (policy._failed_output_disposition_path(self.experiment_id).parent
                       / "custody")
        custody_dir.mkdir(parents=True, mode=0o700)
        unexpected = custody_dir / "operator-added.bin"
        unexpected.write_bytes(b"unbound evidence\n")
        unexpected.chmod(0o600)

        with self.assertRaisesRegex(ValueError, "unexpected entries"):
            self._adjudicate()

    def test_source_mutation_during_custody_publication_refuses(self) -> None:
        write_copy = policy._write_custody_copy
        changed = False

        def write_then_mutate(path: Path, raw: bytes):
            nonlocal changed
            result = write_copy(path, raw)
            if not changed:
                changed = True
                with self.source_paths["legacy_diagnostic_transcript"].open("ab") as stream:
                    stream.write(b"changed during publication")
            return result

        with mock.patch.object(policy, "_write_custody_copy", side_effect=write_then_mutate):
            with self.assertRaisesRegex(RuntimeError, "changed during disposition custody"):
                self._adjudicate()


if __name__ == "__main__":
    unittest.main()
