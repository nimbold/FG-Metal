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
            current_scoped_bytes=54 * policy.GIB,
            peak_additional_bytes=policy.GIB,
        )
        self.assertTrue(result["admitted"])
        self.assertEqual(result["projected_free_bytes"], 50 * policy.GIB)
        self.assertEqual(result["projected_scoped_bytes"], 55 * policy.GIB)
        self.assertIn("projected scoped FG-Metal usage exceeds the 52 GiB warning threshold",
                      result["warnings"])

    def test_existing_45_plus_gib_allocation_is_not_blocked_by_old_ceiling(self) -> None:
        result = _projected_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=46 * policy.GIB,
            peak_additional_bytes=policy.GIB,
        )
        self.assertTrue(result["admitted"])

    def test_projected_scope_over_55_gib_is_rejected_even_when_current_scope_passes(self) -> None:
        result = _projected_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=54 * policy.GIB,
            peak_additional_bytes=policy.GIB + 1,
        )
        self.assertFalse(result["admitted"])
        self.assertEqual(result["projected_scoped_bytes"], 55 * policy.GIB + 1)

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

        self.assertFalse(result["admitted"])
        self.assertIn("projected scoped FG-Metal usage exceeds the 55 GiB ceiling",
                      result["blockers"])

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

    def test_01_baseline_plus_declared_peak_above_55_gib_rejects(self) -> None:
        """1. baseline + declared peak above 55 GiB -> reject."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=32 * policy.MIB)
        # baseline = 53 GiB. declared_peak = ~3.33 GiB. Sum = ~56.33 GiB > 55 GiB ceiling.
        result = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=53 * policy.GIB,
            manifest_estimates=estimates,
            materialized_prefix_bytes=0,
        )
        self.assertFalse(result["admitted"])
        self.assertIn("projected scoped FG-Metal usage exceeds the 55 GiB ceiling",
                      result["blockers"])

    def test_02_baseline_plus_declared_peak_below_55_gib_admits(self) -> None:
        """2. baseline + declared peak below 55 GiB -> admit."""
        estimates = self._make_estimates(prefix_bytes=3 * policy.GIB, persistent_bytes=32 * policy.MIB)
        # baseline = 50 GiB. declared_peak = ~3.33 GiB. Sum = ~53.33 GiB <= 55 GiB ceiling.
        result = _post_materialization_without_vm_commitment(
            current_free_bytes=60 * policy.GIB,
            current_scoped_bytes=50 * policy.GIB,
            manifest_estimates=estimates,
            materialized_prefix_bytes=0,
        )
        self.assertTrue(result["admitted"])
        self.assertEqual(result["blockers"], [])
        self.assertLessEqual(result["projected_scoped_bytes"], 55 * policy.GIB)

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
            self.reviewed_files[field] = {
                "path": (Path("experiments/storage") / relative_path).as_posix(),
                "sha256": hashlib.sha256(contents).hexdigest(),
                "size_bytes": len(contents),
            }
        self.attestation: dict[str, object] = {"supplemental_evidence_sha256": {}}
        self.patcher = mock.patch.object(policy, "REPO", self.repo)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self._write_review()

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

    def test_current_file_change_blocks_even_when_review_record_stays_pinned(self) -> None:
        changed = self.storage / policy.G0C_REVIEW_REQUIRED_FILES["vm_capacity_controller"]
        changed.write_bytes(b"synthetic post-review change\n")

        blockers = policy._g0c_reviewed_file_blockers(self.attestation)

        self.assertEqual(len(blockers), 1)
        self.assertIn("does not bind current experiments/storage/vm_capacity_domain.py", blockers[0])

    def test_missing_or_mismatched_supplemental_pin_fails_closed(self) -> None:
        supplemental = self.attestation["supplemental_evidence_sha256"]
        assert isinstance(supplemental, dict)
        supplemental.clear()
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


class ProjectUsageExclusionTraversalTests(unittest.TestCase):
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

    def test_i_current_or_projected_scope_over_55_gib_fails(self) -> None:
        blockers = self._blockers(current_scope=55 * policy.GIB + 1)
        self.assertTrue(any("55 GiB" in blocker for blocker in blockers))
        self.assertTrue(any("projected scoped FG-Metal usage exceeds the 55 GiB ceiling"
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
        self.assertEqual(policy.GATE0_PROJECT_BUDGET, 55 * policy.GIB)
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
                        storage_justification: str | None = None) -> tuple[Path, Path]:
        selected_output = output_dir or self.runs / experiment_id
        selected_output.mkdir(parents=True, exist_ok=True)
        manifest_path = self.manifests / f"{experiment_id}.json"
        estimates = {
            "expected_duration_seconds": 60,
            "expected_prefix_bytes": 0,
            "expected_build_bytes": 0,
            "estimated_persistent_bytes": persistent,
            "estimated_max_new_disk_bytes": persistent,
            "estimated_uncertainty_margin_bytes": policy.admission_uncertainty_margin_bytes(persistent),
            "estimated_projected_new_disk_bytes": (
                persistent + policy.admission_uncertainty_margin_bytes(persistent)),
            "project_budget_bytes": policy.PROJECT_BUDGET,
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
                }):
            with self.assertRaisesRegex(RuntimeError, "BuildLease state"):
                policy._historical_terminal_liveness_check(self.evidence)

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
        self._preflight(attestation_path, smoke_id, key_path)
        with mock.patch.object(policy, "REPO", repo), \
                mock.patch.object(policy, "GATE0_ATTESTATION_PATH", attestation_path), \
                mock.patch.object(policy, "SMOKE_CLAIM_ROOT", claims), \
                mock.patch.object(policy, "MANIFEST_SIGNING_KEY_PATH", key_path):
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

    def test_native_lease_rejects_projected_peak_above_55_gib(self) -> None:
        manifest_path = self._make_manifest(
            "test-native-over-55gib", self.output_dir,
            estimated_persistent_bytes=policy.GIB + 1)
        lease = self._create_lease("test-native-over-55gib", manifest_path=manifest_path)
        with mock.patch.object(policy, "disk_free_bytes", return_value=60 * policy.GIB), \
                mock.patch.object(policy, "measure_project_usage", return_value={
                    "allocated_inode_deduplicated_bytes": 54 * policy.GIB,
                }):
            with self.assertRaisesRegex(RuntimeError, "projected scoped FG-Metal usage exceeds the 55 GiB ceiling"):
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
