"""Unit tests for the frozen R66 execution bindings."""

from __future__ import annotations

import copy
import ast
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import build_retention
import run_r6a_execution
import storage_policy


def _canonical(value: object) -> bytes:
    return build_retention._canonical_json(value)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class R66ExecutionBindingTests(unittest.TestCase):
    def test_r90_admission_output_roots_are_deterministic_unique_and_siblings(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r90-output-root-") as temp:
            repo = Path(temp).resolve()
            parent = repo / run_r6a_execution.R90_ADMISSION_OUTPUT_ROOT
            parent.mkdir(parents=True)
            with mock.patch.object(storage_policy, "REPO", repo):
                first_id = "r6a-r87-admission-20261026t120000z-r90-a1b2"
                second_id = "r6a-r87-admission-20261026t120001z-r90-c3d4"

                def plan_for(admission_id: str) -> dict[str, object]:
                    return {
                        "admission_identity": {"admission_id": admission_id},
                        "admission_output_contract": (
                            run_r6a_execution.R90_ADMISSION_OUTPUT_CONTRACT),
                        "admission_output_dir": (
                            f"{run_r6a_execution.R90_ADMISSION_OUTPUT_ROOT}/{admission_id}"),
                    }

                first = run_r6a_execution._r90_admission_output_dir(
                    plan_for(first_id), repo=repo)
                repeated = run_r6a_execution._r90_admission_output_dir(
                    plan_for(first_id), repo=repo)
                second = run_r6a_execution._r90_admission_output_dir(
                    plan_for(second_id), repo=repo)
                self.assertEqual(first, repeated)
                self.assertNotEqual(first, second)
                self.assertEqual(first.parent, second.parent)
                self.assertEqual(first.name, first_id)
                self.assertEqual(second.name, second_id)
                self.assertFalse(first.exists())
                self.assertFalse(second.exists())

    def test_r90_admission_output_root_rejects_path_aliases_and_collisions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r90-output-root-") as temp:
            repo = Path(temp).resolve()
            parent = repo / run_r6a_execution.R90_ADMISSION_OUTPUT_ROOT
            parent.mkdir(parents=True)
            admission_id = "r6a-r87-admission-20261026t120000z-r90-a1b2"
            plan: dict[str, object] = {
                "admission_identity": {"admission_id": admission_id},
                "admission_output_contract": (
                    run_r6a_execution.R90_ADMISSION_OUTPUT_CONTRACT),
                "admission_output_dir": (
                    f"{run_r6a_execution.R90_ADMISSION_OUTPUT_ROOT}/{admission_id}"),
            }
            with mock.patch.object(storage_policy, "REPO", repo):
                for invalid_id in (
                        "r6a-r87-admission-../escape",
                        "r6a-r87-admission-bad--component",
                        "r6a-r87-admission-trailing-",
                        "R6A-r87-admission-noncanonical",
                        "r6a-r87-admission-unicodé"):
                    bad = copy.deepcopy(plan)
                    bad["admission_identity"] = {"admission_id": invalid_id}
                    bad["admission_output_dir"] = (
                        f"{run_r6a_execution.R90_ADMISSION_OUTPUT_ROOT}/{invalid_id}")
                    with self.subTest(invalid_id=invalid_id), self.assertRaises(RuntimeError):
                        run_r6a_execution._r90_admission_output_dir(bad, repo=repo)

                bad_path = copy.deepcopy(plan)
                bad_path["admission_output_dir"] += "/child"
                with self.assertRaisesRegex(RuntimeError, "does not bind"):
                    run_r6a_execution._r90_admission_output_dir(bad_path, repo=repo)

                alias = parent / admission_id.upper()
                alias.mkdir()
                with self.assertRaisesRegex(RuntimeError, "case-fold collision"):
                    run_r6a_execution._r90_admission_output_dir(plan, repo=repo)

    def test_r90_admission_output_root_rejects_symlink_parent(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r90-output-root-") as temp:
            repo = Path(temp).resolve()
            storage = repo / "experiments" / "storage"
            storage.mkdir(parents=True)
            real_parent = repo / "real-admission-outputs"
            real_parent.mkdir()
            (storage / "r6a-admission-outputs").symlink_to(real_parent, target_is_directory=True)
            admission_id = "r6a-r87-admission-20261026t120000z-r90-a1b2"
            plan: dict[str, object] = {
                "admission_identity": {"admission_id": admission_id},
                "admission_output_contract": (
                    run_r6a_execution.R90_ADMISSION_OUTPUT_CONTRACT),
                "admission_output_dir": (
                    f"{run_r6a_execution.R90_ADMISSION_OUTPUT_ROOT}/{admission_id}"),
            }
            with mock.patch.object(storage_policy, "REPO", repo), \
                    self.assertRaises(RuntimeError):
                run_r6a_execution._r90_admission_output_dir(plan, repo=repo)

    def test_r81_runner_route_has_exact_plan_candidate_roster_and_r80_lineage(self) -> None:
        candidate_id = "R6-A-R81-SMOKE-MANIFEST-PUBLICATION-CANDIDATE-1"
        self.assertEqual(run_r6a_execution.R81_PLAN_NAME,
                         "R6-A-R81-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json")
        self.assertTrue(run_r6a_execution.R81_CANDIDATE_PATH_PATTERN.fullmatch(
            candidate_id + ".json"))
        self.assertEqual(run_r6a_execution.R81_DEPENDENCY_MANIFEST_PATH,
                         "R6-A-R81-EXECUTION-DEPENDENCY-MANIFEST.json")
        self.assertIn("experiments/storage/run_r6a_execution.py",
                      run_r6a_execution.R81_CHANGED_SOURCE_FILES)
        self.assertIn("experiments/storage/test_run_r6a_execution.py",
                      run_r6a_execution.R81_CHANGED_TEST_FILES)
        self.assertIn("experiments/storage/g0c-launcher-independent-review-R80-PRESERVED.json",
                      run_r6a_execution.R81_TRANSACTION_CONTRACT_FILES)
        self.assertTrue(run_r6a_execution.R80_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-1.json"))

    def test_r86_runner_route_has_closed_candidate_plan_rosters_and_r81_preservation(self) -> None:
        candidate_id = "R6-A-R86-CANONICAL-REGRESSION-REPAIR-CANDIDATE-1"
        self.assertEqual(run_r6a_execution.R86_PLAN_NAME,
                         "R6-A-R86-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json")
        self.assertTrue(run_r6a_execution.R86_CANDIDATE_PATH_PATTERN.fullmatch(
            candidate_id + ".json"))
        self.assertFalse(run_r6a_execution.R86_CANDIDATE_PATH_PATTERN.fullmatch(
            candidate_id.replace("CANDIDATE-1", "CANDIDATE-2") + ".json"))
        self.assertEqual(run_r6a_execution.R86_DEPENDENCY_MANIFEST_PATH,
                         "R6-A-R86-EXECUTION-DEPENDENCY-MANIFEST.json")
        self.assertEqual(run_r6a_execution.R86_CHANGED_SOURCE_FILES, {
            "experiments/storage/build_retention.py",
            "experiments/storage/r6a_dependency_manifest.py",
            "experiments/storage/run_r6a_execution.py",
        })
        self.assertEqual(run_r6a_execution.R86_CHANGED_TEST_FILES, {
            "experiments/storage/test_build_retention.py",
            "experiments/storage/test_buildlease_v3_host_adversarial.py",
            "experiments/storage/test_buildlease_v3_fixture_cleanup_contract.py",
            "experiments/storage/test_r6a_dependency_manifest.py",
            "experiments/storage/test_run_r6a_execution.py",
        })
        self.assertEqual(run_r6a_execution.R86_R81_G0C_PRESERVED_PATH,
                         "experiments/storage/g0c-launcher-independent-review-R81-PRESERVED.json")
        self.assertIn(run_r6a_execution.R86_CANONICAL_REPAIR_CONTRACT_PATH,
                      run_r6a_execution.R86_TRANSACTION_CONTRACT_FILES)
        self.assertIn("experiments/storage/R6-A-R86-RUNNER-MAP.md",
                      run_r6a_execution.R86_TRANSACTION_CONTRACT_FILES)
        self.assertIn("experiments/storage/R6-A-R86-COMPLETION-VERIFIER.md",
                      run_r6a_execution.R86_TRANSACTION_CONTRACT_FILES)

    def test_r86_plan_override_precedes_r81_without_changing_r81_fallback(self) -> None:
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R86_PLAN_PATH": "r86.json",
            "FG_METAL_R81_PLAN_PATH": "r81.json",
        }), "r86.json")
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R81_PLAN_PATH": "r81.json",
        }), "r81.json")

    def test_r87_plan_binds_phase_aware_capacity_and_exact_persistent_output_cap(self) -> None:
        plan = self._r66_plan()
        build_id = "r6a-r87-dual-lineage-phase-cap-test"
        admission_id = "r6a-r87-admission-phase-cap-test"
        plan["build_id"] = build_id
        plan["build_workspace"] = str(
            build_retention.BUILD_ROOT / build_id
            / build_retention.BUILD_SESSION_WORKSPACE_NAME)
        plan["environment"]["HOME"] = str(Path(plan["build_workspace"]) / ".home")
        plan["admission_identity"] = {
            "admission_id": admission_id,
            "sha256": _sha(_canonical({"admission_id": admission_id})),
        }
        plan["admission_output_contract"] = run_r6a_execution.R90_ADMISSION_OUTPUT_CONTRACT
        plan["admission_output_dir"] = (
            f"{run_r6a_execution.R90_ADMISSION_OUTPUT_ROOT}/{admission_id}")
        plan["persistent_output_allowance_bytes"] = build_retention.GIB
        run_r6a_execution._validate_r66_recovery_plan(plan)
        validated = build_retention._session_validate_plan(
            plan, build_id=build_id, build_path=build_retention.BUILD_ROOT / build_id,
            source_tree_hash=plan["source_identity"]["sha256"],
            expected_bytes=plan["reservation_bytes"])
        self.assertEqual(validated["persistent_output_allowance_bytes"], build_retention.GIB)

        missing_cap = copy.deepcopy(plan)
        missing_cap.pop("persistent_output_allowance_bytes")
        with self.assertRaisesRegex(RuntimeError, "exact reviewed 1 GiB persistent-output cap"):
            run_r6a_execution._validate_r66_recovery_plan(missing_cap)
        oversized = copy.deepcopy(plan)
        oversized["persistent_output_allowance_bytes"] += 1
        with self.assertRaisesRegex(RuntimeError, "exact reviewed 1 GiB persistent-output cap"):
            run_r6a_execution._validate_r66_recovery_plan(oversized)

        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R87_PLAN_PATH": "r87.json",
            "FG_METAL_R86_PLAN_PATH": "r86.json",
        }), "r87.json")
        self.assertEqual(run_r6a_execution.R87_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-2.json")
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-2.json"))
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-3.json"))
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-4.json"))
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-5.json"))
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-6.json"))
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-7.json"))
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-8.json"))
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-9.json"))
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10.json"))
        self.assertTrue(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-14.json"))
        self.assertFalse(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-1.json"))
        self.assertEqual(run_r6a_execution.R87_CANDIDATE_3_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-3.json")
        self.assertEqual(run_r6a_execution.R87_CANDIDATE_4_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-4.json")
        self.assertEqual(run_r6a_execution.R87_CANDIDATE_5_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-5.json")
        self.assertEqual(run_r6a_execution.R87_CANDIDATE_6_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-6.json")
        self.assertEqual(run_r6a_execution.R87_CANDIDATE_7_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-7.json")
        self.assertEqual(run_r6a_execution.R87_CANDIDATE_8_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-8.json")
        self.assertEqual(run_r6a_execution.R87_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R87-FRESH-G0C-REVIEW.json")
        self.assertEqual(run_r6a_execution.R87_CANDIDATE_5_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R88-FRESH-G0C-REVIEW.json")
        self.assertEqual(run_r6a_execution.R89_CANDIDATE_8_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R89-FRESH-G0C-REVIEW.json")
        self.assertEqual(run_r6a_execution.R89_CANDIDATE_9_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R89-FRESH-G0C-REVIEW-CANDIDATE-9.json")
        self.assertEqual(run_r6a_execution.R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R89-FRESH-G0C-REVIEW-CANDIDATE-10.json")
        self.assertEqual(run_r6a_execution.R89_LAUNCHER_G0C_REVIEW_PATH,
                         "experiments/storage/g0c-launcher-independent-review-R89-CANDIDATE-10.json")
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage" / run_r6a_execution.R87_CANDIDATE_3_PLAN_NAME))
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage" / run_r6a_execution.R87_CANDIDATE_4_PLAN_NAME))
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage" / run_r6a_execution.R87_CANDIDATE_5_PLAN_NAME))
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage" / run_r6a_execution.R87_CANDIDATE_6_PLAN_NAME))
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage" / run_r6a_execution.R87_CANDIDATE_7_PLAN_NAME))
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage" / run_r6a_execution.R87_CANDIDATE_8_PLAN_NAME))
        self.assertEqual(run_r6a_execution.R87_DEPENDENCY_MANIFEST_PATH,
                         "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-2.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-3"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-3.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-4"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-4.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-5"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-5.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-6"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-6.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-7"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-7.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-8"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-8.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-9"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-9.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-10.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-18"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-18.json")
        self.assertEqual(run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-19"),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-19.json")
        self.assertIn("experiments/storage/storage_gate0.py",
                      run_r6a_execution.R87_CHANGED_SOURCE_FILES)
        self.assertIn("experiments/storage/smoke_prefix_lease.py",
                      run_r6a_execution.R87_CHANGED_SOURCE_FILES)
        self.assertIn("experiments/storage/test_g0c_checks.py",
                      run_r6a_execution.R87_CHANGED_TEST_FILES)
        self.assertIn("experiments/storage/test_failed_output_disposition.py",
                      run_r6a_execution.R87_CHANGED_TEST_FILES)
        self.assertIn(run_r6a_execution.R87_R86_G0C_PRESERVED_PATH,
                      run_r6a_execution.R87_TRANSACTION_CONTRACT_FILES)
        self.assertEqual(storage_policy.G0C_REVIEW_RECORD_NAME,
                         "g0c-launcher-independent-review-R90-CANDIDATE-10.json")
        self.assertEqual(storage_policy.R89_G0C_REVIEW_RECORD_NAME,
                         "g0c-launcher-independent-review-R89-CANDIDATE-10.json")
        self.assertIn("R6-A-R89-FRESH-G0C-REVIEW-CANDIDATE-10.json",
                      storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-10.json",
                      storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-12.json",
                      storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-17.json",
                      storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-18.json",
                      storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("g0c-launcher-independent-review-R90-CANDIDATE-7.json",
                      storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("g0c-launcher-independent-review-R90-CANDIDATE-8.json",
                      storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("g0c-launcher-independent-review-R90-CANDIDATE-10.json",
                      storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertEqual(storage_policy.G0C_REVIEW_RECORD_NAME,
                         "g0c-launcher-independent-review-R90-CANDIDATE-10.json")
        self.assertEqual(run_r6a_execution.R90_LAUNCHER_G0C_REVIEW_PATH,
                         "experiments/storage/g0c-launcher-independent-review-R90-CANDIDATE-10.json")
        self.assertIn("R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-2.json",
                      storage_policy.R90_ONLY_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-6.json",
                      storage_policy.R90_ONLY_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-9.json",
                      storage_policy.R90_ONLY_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-10.json",
                      storage_policy.R90_ONLY_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("g0c-launcher-independent-review-R90-CANDIDATE-10.json",
                      storage_policy.R90_ONLY_SUPPLEMENTAL_EVIDENCE)
        self.assertIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-19.json",
                      storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertNotIn("g0c-launcher-independent-review-R90-CANDIDATE-4.json",
                         storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertNotIn("R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-3.json",
                         storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        self.assertNotIn("R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-13.json",
                         storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE)
        historical_root_review = "g0c-launcher-independent-review-R90-CANDIDATE-3.json"
        self.assertEqual(
            storage_policy.gate0_supplemental_path(REPO, historical_root_review),
            REPO / historical_root_review)

    def test_candidate5_g0c_answer_is_separate_and_plan_bound(self) -> None:
        descriptor = {
            "path": run_r6a_execution.R87_CANDIDATE_5_FRESH_G0C_REVIEW_PATH,
            "sha256": "a" * 64,
        }
        answer = {
            "milestone": "R87", "status": "PASS", "answer": "NO", "findings": [],
            "candidate_id": "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-5",
            "candidate_core_sha256": "b" * 64,
            "plan_canonical_sha256": "c" * 64,
        }
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=descriptor, answer=answer, report_sha256="a" * 64,
            candidate_id=answer["candidate_id"], candidate_core_sha256="b" * 64,
            plan_canonical_sha256="c" * 64)
        with self.assertRaisesRegex(RuntimeError, "exact candidate and plan"):
            run_r6a_execution._validate_r87_fresh_g0c_answer(
                descriptor=descriptor, answer=answer, report_sha256="a" * 64,
                candidate_id=answer["candidate_id"], candidate_core_sha256="d" * 64,
                plan_canonical_sha256="c" * 64)

    def test_candidate6_g0c_answer_is_separate_and_plan_bound(self) -> None:
        candidate_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-6"
        descriptor = {
            "path": run_r6a_execution.R87_CANDIDATE_5_FRESH_G0C_REVIEW_PATH,
            "sha256": "e" * 64,
        }
        answer = {
            "milestone": "R87", "status": "PASS", "answer": "NO", "findings": [],
            "candidate_id": candidate_id,
            "candidate_core_sha256": "f" * 64,
            "plan_canonical_sha256": "1" * 64,
        }
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=descriptor, answer=answer, report_sha256="e" * 64,
            candidate_id=candidate_id, candidate_core_sha256="f" * 64,
            plan_canonical_sha256="1" * 64)
        with self.assertRaisesRegex(RuntimeError, "exact candidate and plan"):
            run_r6a_execution._validate_r87_fresh_g0c_answer(
                descriptor=descriptor, answer=answer, report_sha256="e" * 64,
                candidate_id=candidate_id, candidate_core_sha256="0" * 64,
                plan_canonical_sha256="1" * 64)

    def test_candidate7_g0c_answer_is_separate_and_plan_bound(self) -> None:
        candidate_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-7"
        descriptor = {
            "path": run_r6a_execution.R87_CANDIDATE_5_FRESH_G0C_REVIEW_PATH,
            "sha256": "2" * 64,
        }
        answer = {
            "milestone": "R87", "status": "PASS", "answer": "NO", "findings": [],
            "candidate_id": candidate_id,
            "candidate_core_sha256": "3" * 64,
            "plan_canonical_sha256": "4" * 64,
        }
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=descriptor, answer=answer, report_sha256="2" * 64,
            candidate_id=candidate_id, candidate_core_sha256="3" * 64,
            plan_canonical_sha256="4" * 64)
        with self.assertRaisesRegex(RuntimeError, "exact candidate and plan"):
            run_r6a_execution._validate_r87_fresh_g0c_answer(
                descriptor=descriptor, answer=answer, report_sha256="2" * 64,
                candidate_id=candidate_id, candidate_core_sha256="5" * 64,
                plan_canonical_sha256="4" * 64)

    def test_candidate8_g0c_answer_uses_fresh_r89_path_and_plan_binding(self) -> None:
        candidate_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-8"
        descriptor = {"path": run_r6a_execution.R89_CANDIDATE_8_FRESH_G0C_REVIEW_PATH,
                      "sha256": "6" * 64}
        answer = {"milestone": "R87", "status": "PASS", "answer": "NO", "findings": [],
                  "candidate_id": candidate_id, "candidate_core_sha256": "7" * 64,
                  "plan_canonical_sha256": "8" * 64}
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=descriptor, answer=answer, report_sha256="6" * 64,
            candidate_id=candidate_id, candidate_core_sha256="7" * 64,
            plan_canonical_sha256="8" * 64)

    def test_candidate9_g0c_answer_uses_new_fresh_r89_path_and_plan_binding(self) -> None:
        candidate_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-9"
        descriptor = {"path": run_r6a_execution.R89_CANDIDATE_9_FRESH_G0C_REVIEW_PATH,
                      "sha256": "6" * 64}
        answer = {"milestone": "R87", "status": "PASS", "answer": "NO", "findings": [],
                  "candidate_id": candidate_id, "candidate_core_sha256": "7" * 64,
                  "plan_canonical_sha256": "8" * 64}
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=descriptor, answer=answer, report_sha256="6" * 64,
            candidate_id=candidate_id, candidate_core_sha256="7" * 64,
            plan_canonical_sha256="8" * 64)

    def test_candidate10_g0c_answer_uses_exact_fresh_r89_path_and_plan_binding(self) -> None:
        candidate_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10"
        descriptor = {"path": run_r6a_execution.R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH,
                      "sha256": "6" * 64}
        answer = {"milestone": "R87", "status": "PASS", "answer": "NO", "findings": [],
                  "candidate_id": candidate_id, "candidate_core_sha256": "7" * 64,
                  "plan_canonical_sha256": "8" * 64}
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=descriptor, answer=answer, report_sha256="6" * 64,
            candidate_id=candidate_id, candidate_core_sha256="7" * 64,
            plan_canonical_sha256="8" * 64)
        answer["candidate_core_sha256"] = "9" * 64
        with self.assertRaisesRegex(RuntimeError, "Fresh R88 G0C answer"):
            run_r6a_execution._validate_r87_fresh_g0c_answer(
                descriptor=descriptor, answer=answer, report_sha256="6" * 64,
                candidate_id=candidate_id, candidate_core_sha256="7" * 64,
                plan_canonical_sha256="8" * 64)

    def test_candidate19_g0c_answer_uses_fresh_route_and_preserves_candidate18_history(self) -> None:
        candidate_id = run_r6a_execution.R90_CANDIDATE_ID
        self.assertEqual(candidate_id, "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-19")
        self.assertEqual(run_r6a_execution.R90_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-15.json")
        descriptor = {"path": run_r6a_execution.R90_CANDIDATE_19_FRESH_G0C_REVIEW_PATH,
                      "sha256": "6" * 64}
        answer = {"milestone": "R87", "status": "PASS", "answer": "NO", "findings": [],
                  "candidate_id": candidate_id, "candidate_core_sha256": "7" * 64,
                  "plan_canonical_sha256": "8" * 64}
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=descriptor, answer=answer, report_sha256="6" * 64,
            candidate_id=candidate_id, candidate_core_sha256="7" * 64,
            plan_canonical_sha256="8" * 64)
        descriptor["path"] = run_r6a_execution.R90_CANDIDATE_18_FRESH_G0C_REVIEW_PATH
        with self.assertRaisesRegex(RuntimeError, "Fresh R87 G0C answer"):
            run_r6a_execution._validate_r87_fresh_g0c_answer(
                descriptor=descriptor, answer=answer, report_sha256="6" * 64,
                candidate_id=candidate_id, candidate_core_sha256="7" * 64,
                plan_canonical_sha256="8" * 64)
        historical_candidate18 = {
            "milestone": "R87", "status": "PASS", "answer": "NO", "findings": [],
            "candidate_id": run_r6a_execution.R90_CANDIDATE_18_ID,
            "candidate_core_sha256": "7" * 64, "plan_canonical_sha256": "8" * 64,
        }
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor={"path": run_r6a_execution.R90_CANDIDATE_18_FRESH_G0C_REVIEW_PATH,
                        "sha256": "6" * 64},
            answer=historical_candidate18, report_sha256="6" * 64,
            candidate_id=run_r6a_execution.R90_CANDIDATE_18_ID,
            candidate_core_sha256="7" * 64, plan_canonical_sha256="8" * 64)
        descriptor["path"] = run_r6a_execution.R90_CANDIDATE_14_FRESH_G0C_REVIEW_PATH
        with self.assertRaisesRegex(RuntimeError, "Fresh R87 G0C answer"):
            run_r6a_execution._validate_r87_fresh_g0c_answer(
                descriptor=descriptor, answer=answer, report_sha256="6" * 64,
                candidate_id=candidate_id, candidate_core_sha256="7" * 64,
                plan_canonical_sha256="8" * 64)
        descriptor["path"] = run_r6a_execution.R90_CANDIDATE_11_FRESH_G0C_REVIEW_PATH
        with self.assertRaisesRegex(RuntimeError, "Fresh R87 G0C answer"):
            run_r6a_execution._validate_r87_fresh_g0c_answer(
                descriptor=descriptor, answer=answer, report_sha256="6" * 64,
                candidate_id=candidate_id, candidate_core_sha256="7" * 64,
                plan_canonical_sha256="8" * 64)
        historical_id = run_r6a_execution.R90_CANDIDATE_13_ID
        historical_answer = {**answer, "candidate_id": historical_id}
        historical_descriptor = {
            "path": run_r6a_execution.R90_CANDIDATE_13_FRESH_G0C_REVIEW_PATH,
            "sha256": "6" * 64}
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=historical_descriptor, answer=historical_answer,
            report_sha256="6" * 64, candidate_id=historical_id,
            candidate_core_sha256="7" * 64, plan_canonical_sha256="8" * 64)
        previous_id = run_r6a_execution.R90_CANDIDATE_14_ID
        previous_descriptor = {
            "path": run_r6a_execution.R90_CANDIDATE_14_FRESH_G0C_REVIEW_PATH,
            "sha256": "6" * 64}
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=previous_descriptor,
            answer={**answer, "candidate_id": previous_id},
            report_sha256="6" * 64, candidate_id=previous_id,
            candidate_core_sha256="7" * 64, plan_canonical_sha256="8" * 64)
        previous_id = run_r6a_execution.R90_CANDIDATE_17_ID
        previous_descriptor = {
            "path": run_r6a_execution.R90_CANDIDATE_17_FRESH_G0C_REVIEW_PATH,
            "sha256": "6" * 64}
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=previous_descriptor,
            answer={**answer, "candidate_id": previous_id},
            report_sha256="6" * 64, candidate_id=previous_id,
            candidate_core_sha256="7" * 64, plan_canonical_sha256="8" * 64)
        previous_id = run_r6a_execution.R90_CANDIDATE_15_ID
        previous_descriptor = {
            "path": run_r6a_execution.R90_CANDIDATE_15_FRESH_G0C_REVIEW_PATH,
            "sha256": "6" * 64}
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=previous_descriptor,
            answer={**answer, "candidate_id": previous_id},
            report_sha256="6" * 64, candidate_id=previous_id,
            candidate_core_sha256="7" * 64, plan_canonical_sha256="8" * 64)
        previous_id = run_r6a_execution.R90_CANDIDATE_16_ID
        previous_descriptor = {
            "path": run_r6a_execution.R90_CANDIDATE_16_FRESH_G0C_REVIEW_PATH,
            "sha256": "6" * 64}
        run_r6a_execution._validate_r87_fresh_g0c_answer(
            descriptor=previous_descriptor,
            answer={**answer, "candidate_id": previous_id},
            report_sha256="6" * 64, candidate_id=previous_id,
            candidate_core_sha256="7" * 64, plan_canonical_sha256="8" * 64)

    def test_candidate16_transaction_contract_set_is_exact_and_candidate10_unchanged(self) -> None:
        with tempfile.TemporaryDirectory(prefix="fgmetal-r90-contracts-") as temp:
            repo = Path(temp)
            for relative in run_r6a_execution.R90_TRANSACTION_CONTRACT_FILES:
                path = repo / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(relative.encode("utf-8"))
            r90_contracts = {
                relative: _sha((repo / relative).read_bytes())
                for relative in run_r6a_execution.R90_TRANSACTION_CONTRACT_FILES
            }
            actual = run_r6a_execution._validate_r87_transaction_contracts(
                {"candidate_id": run_r6a_execution.R90_CANDIDATE_ID,
                 "transaction_contracts": r90_contracts}, repo=repo)
            self.assertEqual(set(actual), run_r6a_execution.R90_TRANSACTION_CONTRACT_FILES)
            self.assertTrue(run_r6a_execution.R90_HISTORICAL_ATTEMPT_EVIDENCE_PATHS
                            .issubset(run_r6a_execution.R90_TRANSACTION_CONTRACT_FILES))
            candidate10_contracts = {
                relative: _sha((repo / relative).read_bytes())
                for relative in run_r6a_execution.R87_TRANSACTION_CONTRACT_FILES
            }
            actual_candidate10 = run_r6a_execution._validate_r87_transaction_contracts(
                {"candidate_id": "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10",
                 "transaction_contracts": candidate10_contracts}, repo=repo)
            self.assertEqual(set(actual_candidate10),
                             run_r6a_execution.R87_TRANSACTION_CONTRACT_FILES)
            missing_receipt = dict(r90_contracts)
            missing_receipt.pop(sorted(run_r6a_execution.R90_HISTORICAL_DISPOSITION_PATHS)[0])
            with self.assertRaisesRegex(RuntimeError, "omits or adds"):
                run_r6a_execution._validate_r87_transaction_contracts(
                    {"candidate_id": run_r6a_execution.R90_CANDIDATE_ID,
                     "transaction_contracts": missing_receipt}, repo=repo)

    def test_r87_canonical_plan_route_accepts_exact_mixed_case_extension(self) -> None:
        canonical = (REPO / "experiments/storage" / run_r6a_execution.R87_PLAN_NAME)
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(canonical))
        self.assertFalse(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json"))
        self.assertFalse(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO.parent / "outside/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-2.json"))

    def test_r90_runner_routes_only_candidate19_and_plan15(self) -> None:
        self.assertEqual(
            run_r6a_execution.R90_PLAN_NAME,
            "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-15.json")
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage" / run_r6a_execution.R90_PLAN_NAME))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            f"{run_r6a_execution.R90_CANDIDATE_ID}.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-12.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-13.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-14.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-15.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-16.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-17.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-18.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-19.json"))

    def test_r91_runner_routes_candidate21_and_plan17_without_rebinding_history(self) -> None:
        self.assertEqual(run_r6a_execution.R90_CANDIDATE_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-19")
        self.assertEqual(run_r6a_execution.R90_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-15.json")
        self.assertEqual(run_r6a_execution.R91_CANDIDATE_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-21")
        self.assertEqual(run_r6a_execution.R91_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-17.json")
        self.assertEqual(run_r6a_execution.R91_HISTORICAL_CANDIDATE_20_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-20")
        self.assertEqual(run_r6a_execution.R91_CANDIDATE_20_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R91-FRESH-G0C-REVIEW-CANDIDATE-20.json")
        self.assertEqual(run_r6a_execution.R91_CANDIDATE_21_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R91-FRESH-G0C-REVIEW-CANDIDATE-21.json")
        self.assertEqual(run_r6a_execution.R91_LAUNCHER_G0C_REVIEW_PATH,
                         "experiments/storage/g0c-launcher-independent-review-R91-CANDIDATE-21.json")
        self.assertEqual(
            run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
                run_r6a_execution.R91_CANDIDATE_ID),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-21.json",
        )
        self.assertIn("experiments/storage/R6-A-R91-WORKSPACE-TRANSITION-CONTRACT-CANDIDATE-20.json",
                      run_r6a_execution.R91_TRANSACTION_CONTRACT_FILES)
        self.assertIn("experiments/storage/R6-A-R91-SESSION-FAILURE-RECOVERY-CONTRACT-CANDIDATE-20.json",
                      run_r6a_execution.R91_TRANSACTION_CONTRACT_FILES)
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage" / run_r6a_execution.R91_PLAN_NAME))
        self.assertFalse(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO.parent / "outside" / run_r6a_execution.R91_PLAN_NAME))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            f"{run_r6a_execution.R91_CANDIDATE_ID}.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-22.json"))
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-23.json"))

    def test_r92_runner_routes_fresh_candidate23_and_plan20(self) -> None:
        self.assertEqual(run_r6a_execution.R92_CANDIDATE_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-23")
        self.assertEqual(run_r6a_execution.R92_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-20.json")
        self.assertEqual(run_r6a_execution.R92_CANDIDATE_23_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R92-FRESH-G0C-REVIEW-CANDIDATE-23.json")
        self.assertEqual(run_r6a_execution.R92_LAUNCHER_G0C_REVIEW_PATH,
                         "experiments/storage/g0c-launcher-independent-review-R92-CANDIDATE-23.json")
        self.assertEqual(
            run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
                run_r6a_execution.R92_CANDIDATE_ID),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-23.json",
        )
        self.assertIn(run_r6a_execution.R92_HANDOFF_CONTRACT_PATH,
                      run_r6a_execution.R92_TRANSACTION_CONTRACT_FILES)
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO / "experiments/storage" / run_r6a_execution.R92_PLAN_NAME))

    def test_r93_runner_routes_fresh_candidate24_and_plan21_without_rebinding_r92(self) -> None:
        self.assertEqual(run_r6a_execution.R92_CANDIDATE_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-23")
        self.assertEqual(run_r6a_execution.R92_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-20.json")
        self.assertEqual(run_r6a_execution.R93_CANDIDATE_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-24")
        self.assertEqual(run_r6a_execution.R93_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-21.json")
        self.assertEqual(run_r6a_execution.R93_CANDIDATE_24_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R93-FRESH-G0C-REVIEW-CANDIDATE-24.json")
        self.assertEqual(run_r6a_execution.R93_LAUNCHER_G0C_REVIEW_PATH,
                         "experiments/storage/g0c-launcher-independent-review-R93-CANDIDATE-24.json")
        self.assertEqual(run_r6a_execution.R93_HANDOFF_CONTRACT_PATH,
                         "experiments/storage/R6-A-R93-R71-HANDOFF-CONTRACT-CANDIDATE-24.json")
        self.assertEqual(
            run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
                run_r6a_execution.R93_CANDIDATE_ID),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-24.json",
        )
        self.assertIn(run_r6a_execution.R93_HANDOFF_CONTRACT_PATH,
                      run_r6a_execution.R93_TRANSACTION_CONTRACT_FILES)
        self.assertEqual(run_r6a_execution.R93_CHANGED_SOURCE_FILES,
                         run_r6a_execution.R91_CHANGED_SOURCE_FILES)
        self.assertEqual(run_r6a_execution.R93_CHANGED_TEST_FILES,
                         run_r6a_execution.R91_CHANGED_TEST_FILES)
        plan_path = REPO / "experiments/storage" / run_r6a_execution.R93_PLAN_NAME
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(plan_path))
        self.assertFalse(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO.parent / "outside" / run_r6a_execution.R93_PLAN_NAME))
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R93_PLAN_PATH": "/tmp/r93-plan.json",
            "FG_METAL_R92_PLAN_PATH": "/tmp/r92-plan.json",
        }), "/tmp/r93-plan.json")
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R92_PLAN_PATH": "/tmp/r92-plan.json",
        }), "/tmp/r92-plan.json")
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            f"{run_r6a_execution.R93_CANDIDATE_ID}.json"))

    def test_r94_runner_routes_fresh_candidate25_and_plan23_without_rebinding_r93(self) -> None:
        self.assertEqual(run_r6a_execution.R93_CANDIDATE_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-24")
        self.assertEqual(run_r6a_execution.R93_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-21.json")
        self.assertEqual(run_r6a_execution.R94_CANDIDATE_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-25")
        self.assertEqual(run_r6a_execution.R94_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-23.json")
        self.assertEqual(run_r6a_execution.R94_CANDIDATE_25_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R94-FRESH-G0C-REVIEW-CANDIDATE-25.json")
        self.assertEqual(run_r6a_execution.R94_LAUNCHER_G0C_REVIEW_PATH,
                         "experiments/storage/g0c-launcher-independent-review-R94-CANDIDATE-25.json")
        self.assertEqual(run_r6a_execution.R94_HANDOFF_CONTRACT_PATH,
                         "experiments/storage/R6-A-R94-R71-HANDOFF-CONTRACT-CANDIDATE-25.json")
        self.assertEqual(
            run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
                run_r6a_execution.R94_CANDIDATE_ID),
            "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-25.json",
        )
        self.assertIn(run_r6a_execution.R94_HANDOFF_CONTRACT_PATH,
                      run_r6a_execution.R94_TRANSACTION_CONTRACT_FILES)
        self.assertEqual(run_r6a_execution.R94_CHANGED_SOURCE_FILES,
                         run_r6a_execution.R93_CHANGED_SOURCE_FILES)
        self.assertEqual(run_r6a_execution.R94_CHANGED_TEST_FILES,
                         run_r6a_execution.R93_CHANGED_TEST_FILES)
        self.assertEqual(run_r6a_execution.R93_CHANGED_SOURCE_FILES,
                         run_r6a_execution.R91_CHANGED_SOURCE_FILES)
        self.assertEqual(run_r6a_execution.R93_CHANGED_TEST_FILES,
                         run_r6a_execution.R91_CHANGED_TEST_FILES)
        self.assertIn("r94_running_authority_tests",
                      run_r6a_execution.R94_REQUIRED_VALIDATION_EVIDENCE)
        plan_path = REPO / "experiments/storage" / run_r6a_execution.R94_PLAN_NAME
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(plan_path))
        self.assertFalse(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO.parent / "outside" / run_r6a_execution.R94_PLAN_NAME))
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R94_PLAN_PATH": "/tmp/r94-plan.json",
            "FG_METAL_R93_PLAN_PATH": "/tmp/r93-plan.json",
        }), "/tmp/r94-plan.json")
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R93_PLAN_PATH": "/tmp/r93-plan.json",
        }), "/tmp/r93-plan.json")
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            f"{run_r6a_execution.R94_CANDIDATE_ID}.json"))

        # Plan23 is immutable historical evidence. Its old review packet is
        # intentionally not revalidated against the current R95 source bytes.
        plan = json.loads(storage_policy._read_file_nofollow(plan_path))
        self.assertEqual(
            plan["admission_identity"]["admission_id"],
            "r6a-r87-admission-20261027t103248z-r94-c25-g3-4bfcc8d2")
        self.assertEqual(
            plan["admission_output_dir"],
            "experiments/storage/r6a-admission-outputs/"
            "r6a-r87-admission-20261027t103248z-r94-c25-g3-4bfcc8d2")
        self.assertNotEqual(run_r6a_execution.R94_PLAN_NAME,
                            run_r6a_execution.R95_PLAN_NAME)

    def test_r95_runner_routes_fresh_candidate26_and_plan24_without_rebinding_r94(self) -> None:
        self.assertEqual(run_r6a_execution.R95_CANDIDATE_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-26")
        self.assertEqual(run_r6a_execution.R95_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-24.json")
        self.assertEqual(run_r6a_execution.R95_CANDIDATE_26_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R95-FRESH-G0C-REVIEW-CANDIDATE-26.json")
        self.assertEqual(run_r6a_execution.R95_LAUNCHER_G0C_REVIEW_PATH,
                         "experiments/storage/g0c-launcher-independent-review-R95-CANDIDATE-26.json")
        self.assertEqual(run_r6a_execution.R95_HANDOFF_CONTRACT_PATH,
                         "experiments/storage/R6-A-R95-R71-HANDOFF-CONTRACT-CANDIDATE-26.json")
        manifest_name = run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            run_r6a_execution.R95_CANDIDATE_ID)
        self.assertEqual(manifest_name,
                         "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-26.json")
        self.assertIn(run_r6a_execution.R95_HANDOFF_CONTRACT_PATH,
                      run_r6a_execution.R95_TRANSACTION_CONTRACT_FILES)
        self.assertEqual(run_r6a_execution.R95_CHANGED_SOURCE_FILES,
                         run_r6a_execution.R94_CHANGED_SOURCE_FILES)
        self.assertEqual(run_r6a_execution.R95_CHANGED_TEST_FILES,
                         run_r6a_execution.R94_CHANGED_TEST_FILES)
        self.assertIn("r95_successor_focused_tests",
                      run_r6a_execution.R95_REQUIRED_VALIDATION_EVIDENCE)

        plan_path = REPO / "experiments/storage" / run_r6a_execution.R95_PLAN_NAME
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(plan_path))
        self.assertFalse(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO.parent / "outside" / run_r6a_execution.R95_PLAN_NAME))
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R95_PLAN_PATH": "/tmp/r95-plan.json",
            "FG_METAL_R94_PLAN_PATH": "/tmp/r94-plan.json",
        }), "/tmp/r95-plan.json")
        self.assertEqual(storage_policy.current_g0c_review_record_name({
            "FG_METAL_R95_PLAN_PATH": str(plan_path),
            "FG_METAL_R94_PLAN_PATH": "/tmp/r94-plan.json",
        }), run_r6a_execution.R95_LAUNCHER_G0C_REVIEW_PATH.rsplit("/", 1)[-1])
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            f"{run_r6a_execution.R95_CANDIDATE_ID}.json"))

        plan_bytes = storage_policy._read_file_nofollow(plan_path)
        plan = json.loads(plan_bytes)
        predecessor = json.loads(storage_policy._read_file_nofollow(
            REPO / "experiments/storage/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-23.json"))
        self.assertNotEqual(plan["build_id"], predecessor["build_id"])
        self.assertNotEqual(plan["admission_identity"]["admission_id"],
                            predecessor["admission_identity"]["admission_id"])
        self.assertNotEqual(plan["admission_output_dir"], predecessor["admission_output_dir"])
        self.assertEqual(
            plan["admission_identity"]["sha256"],
            _sha(_canonical({"admission_id": plan["admission_identity"]["admission_id"]})))
        self.assertFalse((build_retention.BUILD_ROOT / plan["build_id"]).exists())
        self.assertFalse((REPO / plan["admission_output_dir"]).exists())
        normalized = build_retention._session_validate_plan(
            plan,
            build_id=plan["build_id"],
            build_path=build_retention.BUILD_ROOT / plan["build_id"],
            source_tree_hash=plan["source_identity"]["sha256"],
            expected_bytes=plan["reservation_bytes"],
        )
        run_r6a_execution._validate_r66_recovery_plan(normalized)

        candidate_path = REPO / f"{run_r6a_execution.R95_CANDIDATE_ID}.json"
        candidate_mirror = REPO / "experiments/storage" / candidate_path.name
        candidate_bytes = storage_policy._read_file_nofollow(candidate_path)
        candidate = json.loads(candidate_bytes)
        self.assertEqual(candidate_bytes, storage_policy._read_file_nofollow(candidate_mirror))
        self.assertEqual(candidate["candidate_id"], run_r6a_execution.R95_CANDIDATE_ID)
        self.assertEqual(candidate["execution_plan"], {
            "path": f"experiments/storage/{run_r6a_execution.R95_PLAN_NAME}",
            "raw_sha256": _sha(plan_bytes),
            "canonical_sha256": _sha(_canonical(plan)),
            "graph_pair_key": plan["build_configuration_key_sha256"],
        })
        self.assertEqual(candidate["r87_binding"]["execution_dependency_manifest"]["path"],
                         manifest_name)
        self.assertEqual(candidate["runner_map"]["r95_handoff_contract_path"],
                         run_r6a_execution.R95_HANDOFF_CONTRACT_PATH)
        self.assertEqual(candidate["runner_map"]["r95_handoff_contract_sha256"],
                         candidate["transaction_contracts"][run_r6a_execution.R95_HANDOFF_CONTRACT_PATH])
        # Candidate26 is the frozen R95 predecessor. The R96 implementation
        # edits several files in this same roster, so its historical byte
        # bindings must continue to match its review packet instead of being
        # rebound to current bytes.
        candidate_review = json.loads(storage_policy._read_file_nofollow(
            REPO / run_r6a_execution.R95_CANDIDATE_26_FRESH_G0C_REVIEW_PATH))
        self.assertEqual(candidate["changed_source_hashes"], {
            relative: record["sha256"]
            for relative, record in candidate_review["reviewed_files"]["source"].items()
        })
        self.assertEqual(candidate["changed_test_hashes"], {
            relative: record["sha256"]
            for relative, record in candidate_review["reviewed_files"]["tests"].items()
        })

    def test_r96_candidate27_remains_historical_and_pending(self) -> None:
        self.assertEqual(run_r6a_execution.R96_CANDIDATE_ID,
                         "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-27")
        self.assertEqual(run_r6a_execution.R96_PLAN_NAME,
                         "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-25.json")
        self.assertEqual(run_r6a_execution.R96_CANDIDATE_27_FRESH_G0C_REVIEW_PATH,
                         "experiments/storage/R6-A-R96-FRESH-G0C-REVIEW-CANDIDATE-27.json")
        self.assertEqual(run_r6a_execution.R96_LAUNCHER_G0C_REVIEW_PATH,
                         "experiments/storage/g0c-launcher-independent-review-R96-CANDIDATE-27.json")
        self.assertEqual(run_r6a_execution.R96_HANDOFF_CONTRACT_PATH,
                         "experiments/storage/R6-A-R96-R71-HANDOFF-CONTRACT-CANDIDATE-27.json")
        manifest_name = run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            run_r6a_execution.R96_CANDIDATE_ID)
        self.assertEqual(manifest_name,
                         "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-27.json")
        self.assertIn(run_r6a_execution.R96_HANDOFF_CONTRACT_PATH,
                      run_r6a_execution.R96_TRANSACTION_CONTRACT_FILES)
        self.assertEqual(run_r6a_execution.R96_CHANGED_SOURCE_FILES,
                         run_r6a_execution.R95_CHANGED_SOURCE_FILES)
        self.assertEqual(run_r6a_execution.R96_CHANGED_TEST_FILES,
                         run_r6a_execution.R95_CHANGED_TEST_FILES)
        self.assertIn("r96_post_exit_watcher_tests",
                      run_r6a_execution.R96_REQUIRED_VALIDATION_EVIDENCE)

        plan_path = REPO / "experiments/storage" / run_r6a_execution.R96_PLAN_NAME
        self.assertTrue(run_r6a_execution._is_fresh_r6a_plan_path(plan_path))
        self.assertFalse(run_r6a_execution._is_fresh_r6a_plan_path(
            REPO.parent / "outside" / run_r6a_execution.R96_PLAN_NAME))
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R96_PLAN_PATH": "/tmp/r96-plan.json",
            "FG_METAL_R95_PLAN_PATH": "/tmp/r95-plan.json",
        }), "/tmp/r96-plan.json")
        self.assertEqual(storage_policy.current_g0c_review_record_name({
            "FG_METAL_R96_PLAN_PATH": str(plan_path),
            "FG_METAL_R95_PLAN_PATH": "/tmp/r95-plan.json",
        }), run_r6a_execution.R96_LAUNCHER_G0C_REVIEW_PATH.rsplit("/", 1)[-1])
        self.assertIsNotNone(run_r6a_execution.R87_CANDIDATE_PATH_PATTERN.fullmatch(
            f"{run_r6a_execution.R96_CANDIDATE_ID}.json"))

        plan_bytes = storage_policy._read_file_nofollow(plan_path)
        plan = json.loads(plan_bytes)
        predecessor = json.loads(storage_policy._read_file_nofollow(
            REPO / "experiments/storage/R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-24.json"))
        self.assertNotEqual(plan["build_id"], predecessor["build_id"])
        self.assertNotEqual(plan["admission_identity"]["admission_id"],
                            predecessor["admission_identity"]["admission_id"])
        self.assertNotEqual(plan["admission_output_dir"], predecessor["admission_output_dir"])
        self.assertEqual(
            plan["admission_identity"]["sha256"],
            _sha(_canonical({"admission_id": plan["admission_identity"]["admission_id"]})))
        self.assertFalse((build_retention.BUILD_ROOT / plan["build_id"]).exists())
        self.assertFalse((REPO / plan["admission_output_dir"]).exists())
        normalized = build_retention._session_validate_plan(
            plan,
            build_id=plan["build_id"],
            build_path=build_retention.BUILD_ROOT / plan["build_id"],
            source_tree_hash=plan["source_identity"]["sha256"],
            expected_bytes=plan["reservation_bytes"],
        )
        run_r6a_execution._validate_r66_recovery_plan(normalized)

        candidate_path = REPO / f"{run_r6a_execution.R96_CANDIDATE_ID}.json"
        candidate_mirror = REPO / "experiments/storage" / candidate_path.name
        candidate_bytes = storage_policy._read_file_nofollow(candidate_path)
        candidate = json.loads(candidate_bytes)
        self.assertEqual(candidate_bytes, storage_policy._read_file_nofollow(candidate_mirror))
        self.assertEqual(candidate["candidate_id"], run_r6a_execution.R96_CANDIDATE_ID)
        self.assertEqual(candidate["execution_plan"], {
            "path": f"experiments/storage/{run_r6a_execution.R96_PLAN_NAME}",
            "raw_sha256": _sha(plan_bytes),
            "canonical_sha256": _sha(_canonical(plan)),
            "graph_pair_key": plan["build_configuration_key_sha256"],
        })
        self.assertEqual(candidate["r87_binding"]["execution_dependency_manifest"]["path"],
                         manifest_name)
        self.assertEqual(candidate["runner_map"]["r96_handoff_contract_path"],
                         run_r6a_execution.R96_HANDOFF_CONTRACT_PATH)
        self.assertEqual(candidate["runner_map"]["r96_handoff_contract_sha256"],
                         candidate["transaction_contracts"][run_r6a_execution.R96_HANDOFF_CONTRACT_PATH])
        # Candidate27 is historical R96 preparation evidence. Its frozen
        # hashes remain bound to its pending review and must not be rewritten
        # to make this packet look current after later source/test repairs.
        candidate_review = json.loads(storage_policy._read_file_nofollow(
            REPO / run_r6a_execution.R96_CANDIDATE_27_FRESH_G0C_REVIEW_PATH))
        self.assertEqual(candidate["changed_source_hashes"], {
            relative: record["sha256"]
            for relative, record in candidate_review["reviewed_files"]["source"].items()
        })
        self.assertEqual(candidate["changed_test_hashes"], {
            relative: record["sha256"]
            for relative, record in candidate_review["reviewed_files"]["tests"].items()
        })
        self.assertEqual(candidate["status"], "FROZEN_FOR_ACCEPTANCE_VALIDATION")
        self.assertEqual(candidate_review["status"], "PENDING_R96_VALIDATION")

    def test_r91_plan17_host_v3_validates_both_frozen_prebuild_profiles_offline(self) -> None:
        plan_path = REPO / "experiments/storage" / run_r6a_execution.R91_PLAN_NAME
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
        reserved_build_path = build_retention.BUILD_ROOT / plan["build_id"]
        self.assertFalse(reserved_build_path.exists())
        graph_profiles = {
            row["graph_id"]: row["prebuild_identity"]["configuration_profile_sha256"]
            for row in plan["build_graphs"]
        }
        self.assertEqual(set(graph_profiles), set(build_retention.BUILD_SESSION_DUAL_GRAPH_IDS))
        self.assertTrue(all(len(digest) == 64
                            and all(character in "0123456789abcdef" for character in digest)
                            for digest in graph_profiles.values()))
        run_r6a_execution._validate_r66_recovery_plan(plan)
        validated = build_retention._session_validate_plan(
            plan,
            build_id=plan["build_id"],
            build_path=reserved_build_path,
            source_tree_hash=plan["source_identity"]["sha256"],
            expected_bytes=plan["reservation_bytes"],
        )
        self.assertEqual(validated["executor_mode"], "HOST_BUILDLEASE_V3")
        self.assertEqual(
            validated["build_configuration_key_sha256"],
            plan["build_configuration_key_sha256"],
        )
        self.assertFalse(reserved_build_path.exists())

    def test_r90_evidence_run_directory_matches_candidate_number(self) -> None:
        self.assertTrue(run_r6a_execution._r90_evidence_run_matches_candidate(
            "R90-Candidate19-20261026T052221Z-84b9",
            run_r6a_execution.R90_CANDIDATE_ID))
        self.assertTrue(run_r6a_execution._r90_evidence_run_matches_candidate(
            "R90-Candidate18-20261026T044433Z-0a596d33",
            run_r6a_execution.R90_CANDIDATE_18_ID))
        self.assertFalse(run_r6a_execution._r90_evidence_run_matches_candidate(
            "R90-Candidate18-20261026T044433Z-0a596d33",
            run_r6a_execution.R90_CANDIDATE_ID))
        self.assertFalse(run_r6a_execution._r90_evidence_run_matches_candidate(
            "R90-Candidate19-20261026T052221Z-84b9",
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-20"))

    def test_live_cli_requires_explicit_opt_in_and_plan_before_runner_entry(self) -> None:
        script = HERE / "run_r6a_execution.py"
        for argv, expected in ((["--help"], 0), (["--unknown"], 2), ([], 2),
                               (["--execute"], 2)):
            with self.subTest(argv=argv):
                result = subprocess.run(
                    [sys.executable, str(script), *argv], cwd=REPO,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    check=False, timeout=15)
                self.assertEqual(result.returncode, expected)
                self.assertNotIn("R66 SAME-PROCESS R6-A EXECUTION", result.stdout)
                self.assertNotIn("Gate 0", result.stdout + result.stderr)

    def test_r71_candidate_filename_uses_provenance_capacity_contract(self) -> None:
        self.assertIsNotNone(run_r6a_execution.R71_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-1.json"))
        self.assertIsNone(run_r6a_execution.R71_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R71-EXECUTION-CANDIDATE-1.json"))
        self.assertIsNone(run_r6a_execution.R71_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-SYNTHETIC.json"))

    def test_r71_gate_inventory_and_transaction_contract_dependencies_are_mandatory(self) -> None:
        self.assertEqual(storage_policy.G0C_REVIEW_REQUIRED_FILES.get("inventory_control"),
                         "storage_inventory.py")
        self.assertIn("experiments/storage/storage_inventory.py",
                      run_r6a_execution.R71_CHANGED_SOURCE_FILES)
        self.assertIn("experiments/storage/storage_gate0.py",
                      run_r6a_execution.R71_CHANGED_SOURCE_FILES)
        self.assertIn("experiments/storage/test_g0c_checks.py",
                      run_r6a_execution.R71_CHANGED_TEST_FILES)
        contracts = {
            path: _sha((REPO / path).read_bytes())
            for path in run_r6a_execution.R71_TRANSACTION_CONTRACT_FILES
        }
        valid = run_r6a_execution._validate_r71_transaction_contracts(
            {"transaction_contracts": contracts}, repo=REPO)
        self.assertEqual(set(valid), run_r6a_execution.R71_TRANSACTION_CONTRACT_FILES)
        missing_inventory_contract = dict(contracts)
        missing_inventory_contract.pop(next(iter(sorted(missing_inventory_contract))))
        with self.assertRaisesRegex(RuntimeError, "omits or adds"):
            run_r6a_execution._validate_r71_transaction_contracts(
                {"transaction_contracts": missing_inventory_contract}, repo=REPO)
        stale_contract = dict(contracts)
        stale_contract[next(iter(sorted(stale_contract)))] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "changed"):
            run_r6a_execution._validate_r71_transaction_contracts(
                {"transaction_contracts": stale_contract}, repo=REPO)

    def _r66_plan(self) -> dict[str, object]:
        frozen = (REPO / "experiments/storage/"
                  "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-17.json")
        plan = json.loads(frozen.read_text(encoding="utf-8"))
        # This helper exercises the pre-R90 route contract. Plan17 is the
        # current graph-pair baseline, so remove its later R90 output binding
        # unless an R87-specific case opts back into that contract below.
        plan.pop("admission_output_contract", None)
        plan.pop("admission_output_dir", None)
        plan.pop("persistent_output_allowance_bytes", None)
        build_id = "r6a-r66-dual-lineage-unit-check"
        workspace = (build_retention.BUILD_ROOT / build_id
                     / build_retention.BUILD_SESSION_WORKSPACE_NAME)
        plan["build_id"] = build_id
        plan["build_workspace"] = str(workspace)
        plan["environment"]["HOME"] = str(workspace / ".home")
        admission_id = "r6a-r66-admission-unit-check"
        plan["admission_identity"] = {
            "admission_id": admission_id,
            "sha256": _sha(_canonical({"admission_id": admission_id})),
        }
        return plan

    def test_r66_plan_accepts_fresh_identity_and_frozen_dual_graph_pair(self) -> None:
        plan = self._r66_plan()
        run_r6a_execution._validate_r66_recovery_plan(plan)
        build_id = plan["build_id"]
        validated = build_retention._session_validate_plan(
            plan,
            build_id=build_id,
            build_path=build_retention.BUILD_ROOT / build_id,
            source_tree_hash=plan["source_identity"]["sha256"],
            expected_bytes=plan["reservation_bytes"],
        )
        self.assertEqual(
            validated["build_configuration_key_sha256"],
            "4cdbf9bd34dd0b2ae0b4a064e30a68432131c62d173e9a291f184d78c6be37d9",
        )

    def test_r66_plan_rejects_noncanonical_admission_digest(self) -> None:
        plan = self._r66_plan()
        plan["admission_identity"]["sha256"] = "1" * 64
        with self.assertRaisesRegex(RuntimeError, "digest is not canonical"):
            run_r6a_execution._validate_r66_recovery_plan(plan)

    def test_r66_refused_admission_id_cannot_be_reused(self) -> None:
        admission_id = "r6a-r66-admission-refused-unit-check"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            receipt = Path(temp) / f"{admission_id}.json"
            receipt.write_text('{"decision":"REFUSE"}\n', encoding="utf-8")
            with mock.patch.object(
                    run_r6a_execution.storage_policy,
                    "_experiment_manifest_path",
                    return_value=str(receipt)):
                with self.assertRaisesRegex(RuntimeError, "already been used"):
                    run_r6a_execution._assert_r66_admission_id_unclaimed(admission_id)
            self.assertEqual(receipt.read_text(encoding="utf-8"), '{"decision":"REFUSE"}\n')

    def test_r73_admission_id_passes_runner_guard(self) -> None:
        admission_id = "r6a-r73-admission-policy-migration-1"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            with (mock.patch.object(
                    run_r6a_execution.storage_policy,
                    "_experiment_manifest_path",
                    return_value=str(Path(temp) / "missing.json")),
                  mock.patch.object(
                    run_r6a_execution.storage_policy,
                    "_assert_r66_r69_admission_id_unclaimed") as assert_unclaimed):
                run_r6a_execution._assert_r66_admission_id_unclaimed(admission_id)
            assert_unclaimed.assert_called_once_with(admission_id)

    def test_r73_malformed_admission_ids_are_rejected_by_runner_guard(self) -> None:
        for admission_id in (
                "r6a-r73-admission-",
                "r6a-r73-admission-../escape",
                "R6A-r73-admission-noncanonical"):
            with self.subTest(admission_id=admission_id):
                with self.assertRaisesRegex(RuntimeError, "not path-safe"):
                    run_r6a_execution._assert_r66_admission_id_unclaimed(admission_id)

    def test_refuse_receipt_deletion_does_not_free_claimed_admission_id(self) -> None:
        admission_id = "r6a-r69-admission-refused-tombstone-check"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            cache = root / "cache"
            receipt = root / f"{admission_id}.json"
            receipt.write_text('{"decision":"REFUSE"}\n', encoding="utf-8")
            with mock.patch.object(run_r6a_execution.storage_policy, "CACHE", cache):
                run_r6a_execution.storage_policy._claim_r66_r69_admission_id(
                    admission_id, "a" * 64,
                    _sha(_canonical({"admission_id": admission_id})), "REFUSE")
                receipt.unlink()
                with mock.patch.object(
                        run_r6a_execution.storage_policy,
                        "_experiment_manifest_path",
                        return_value=receipt):
                    with self.assertRaisesRegex(RuntimeError, "already been claimed"):
                        run_r6a_execution._assert_r66_admission_id_unclaimed(admission_id)

    def test_allow_claimed_admission_id_cannot_be_reused(self) -> None:
        admission_id = "r6a-r69-admission-allow-tombstone-check"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            cache = Path(temp) / "cache"
            with mock.patch.object(run_r6a_execution.storage_policy, "CACHE", cache):
                run_r6a_execution.storage_policy._claim_r66_r69_admission_id(
                    admission_id, "b" * 64,
                    _sha(_canonical({"admission_id": admission_id})), "ALLOW")
                with mock.patch.object(
                        run_r6a_execution.storage_policy,
                        "_experiment_manifest_path",
                        return_value=Path(temp) / "missing.json"):
                    with self.assertRaisesRegex(RuntimeError, "already been claimed"):
                        run_r6a_execution._assert_r66_admission_id_unclaimed(admission_id)

    def test_signed_refuse_migration_preserves_consumed_id_after_receipt_deletion(self) -> None:
        admission_id = "r6a-r66-admission-migrated-refuse-check"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            cache = root / "cache"
            receipt = root / f"{admission_id}.json"
            manifest = {
                "decision": "REFUSE",
                "experiment_id": admission_id,
                "execution_admission_identity": {
                    "admission_id": admission_id,
                    "sha256": _sha(_canonical({"admission_id": admission_id})),
                },
                "build_session_plan_sha256": "e" * 64,
            }
            with (mock.patch.object(run_r6a_execution.storage_policy, "CACHE", cache),
                  mock.patch.object(run_r6a_execution.storage_policy, "_manifest_signing_key",
                                    return_value=b"synthetic refusal signing key 123456")):
                manifest["manifest_hmac_sha256"] = (
                    run_r6a_execution.storage_policy._manifest_signature(manifest))
                receipt.write_bytes(json.dumps(manifest, sort_keys=True).encode("utf-8"))
                run_r6a_execution.storage_policy._migrate_r66_r69_refusal_claim(receipt)
                receipt.unlink()
                with mock.patch.object(
                        run_r6a_execution.storage_policy,
                        "_experiment_manifest_path",
                        return_value=receipt):
                    with self.assertRaisesRegex(RuntimeError, "already been claimed"):
                        run_r6a_execution._assert_r66_admission_id_unclaimed(admission_id)

    def test_signed_refuse_migration_rejects_noncanonical_identity_digest(self) -> None:
        admission_id = "r6a-r69-admission-noncanonical-migration-check"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            receipt = Path(temp) / f"{admission_id}.json"
            manifest = {
                "decision": "REFUSE",
                "experiment_id": admission_id,
                "execution_admission_identity": {
                    "admission_id": admission_id,
                    "sha256": "d" * 64,
                },
                "build_session_plan_sha256": "e" * 64,
            }
            with mock.patch.object(
                    run_r6a_execution.storage_policy,
                    "_manifest_signing_key",
                    return_value=b"synthetic refusal signing key 123456"):
                manifest["manifest_hmac_sha256"] = (
                    run_r6a_execution.storage_policy._manifest_signature(manifest))
                receipt.write_bytes(json.dumps(manifest, sort_keys=True).encode("utf-8"))
                with self.assertRaisesRegex(
                        RuntimeError, "lacks its exact admission and plan identities"):
                    run_r6a_execution.storage_policy._migrate_r66_r69_refusal_claim(receipt)

    def test_consumed_build_id_path_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            build_root = Path(temp)
            build_id = "r6a-r66-dual-lineage-consumed-unit-check"
            (build_root / build_id).mkdir()
            with mock.patch.object(build_retention, "BUILD_ROOT", build_root):
                with self.assertRaisesRegex(
                        build_retention.RetentionError, "existing identity/history state"):
                    run_r6a_execution._assert_r66_build_id_unconsumed(build_id)

    def test_r73_build_id_passes_runner_guard(self) -> None:
        build_id = "r6a-r73-dual-lineage-policy-migration-1"
        with mock.patch.object(build_retention, "_assert_build_id_unclaimed") as assert_unclaimed:
            run_r6a_execution._assert_r66_build_id_unconsumed(build_id)
        assert_unclaimed.assert_called_once_with(build_id)

    def test_r73_malformed_build_ids_are_rejected_by_runner_guard(self) -> None:
        for build_id in (
                "r6a-r73-dual-lineage-",
                "r6a-r73-dual-lineage-../escape",
                "r6a-r73-DUAL-LINEAGE-noncanonical"):
            with self.subTest(build_id=build_id):
                with self.assertRaisesRegex(RuntimeError, "not path-safe"):
                    run_r6a_execution._assert_r66_build_id_unconsumed(build_id)

    def test_consumed_build_id_history_is_rejected_without_build_directory(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            build_id = "r6a-r69-dual-lineage-retained-history-unit-check"
            build_root = root / "builds"
            build_root.mkdir()
            manifest_root = root / "manifests"
            manifest_root.mkdir()
            (manifest_root / f"{build_id}.failure").write_text(
                "retained failure history\n", encoding="utf-8")
            with (mock.patch.object(build_retention, "BUILD_ROOT", build_root),
                  mock.patch.object(build_retention, "LEASE_ROOT", root / "leases"),
                  mock.patch.object(build_retention, "MANIFEST_ROOT", manifest_root),
                  mock.patch.object(build_retention, "RECEIPT_ROOT", root / "receipts")):
                self.assertFalse((build_root / build_id).exists())
                with self.assertRaisesRegex(
                        build_retention.RetentionError, "existing identity/history state"):
                    run_r6a_execution._assert_r66_build_id_unconsumed(build_id)

    def test_runner_start_does_not_create_project_bytecode_or_a_log_before_admission(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            cache_prefix = root / "pycache"
            missing_plan = root / "missing-plan.json"
            env = dict(os.environ)
            env.pop("PYTHONDONTWRITEBYTECODE", None)
            env["FG_METAL_R66_PLAN_PATH"] = str(missing_plan)
            result = subprocess.run(
                [sys.executable, "-X", f"pycache_prefix={cache_prefix}", str(HERE / "run_r6a_execution.py")],
                cwd=REPO,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                text=True,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("the following arguments are required: --execute, --plan", result.stderr)
            self.assertNotIn("R66 SAME-PROCESS R6-A EXECUTION", result.stdout)
            project_cache_entries = []
            if cache_prefix.exists():
                for cache_entry in cache_prefix.rglob("*.pyc"):
                    try:
                        cache_entry.relative_to(cache_prefix / Path(*REPO.parts[1:]))
                    except ValueError:
                        continue
                    project_cache_entries.append(cache_entry)
            self.assertEqual(project_cache_entries, [])
            self.assertEqual(list(root.glob("*.log")), [])

    def test_runner_has_no_pre_admission_temporary_file_creation(self) -> None:
        source = (HERE / "run_r6a_execution.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        runner = next(node for node in tree.body
                      if isinstance(node, ast.FunctionDef) and node.name == "run_r6a_execution")
        admission_line = next(node.lineno for node in ast.walk(runner)
                              if isinstance(node, ast.Call)
                              and isinstance(node.func, ast.Attribute)
                              and node.func.attr == "begin_experiment")
        forbidden_calls = {
            "mkstemp", "NamedTemporaryFile", "TemporaryFile", "TemporaryDirectory",
            "tempfile", "write_bytes", "write_text", "mkdir", "makedirs", "mkdtemp",
        }
        writes_before_admission = [
            node for node in ast.walk(runner)
            if isinstance(node, ast.Call) and node.lineno < admission_line
            and ((isinstance(node.func, ast.Name) and node.func.id in forbidden_calls)
                 or (isinstance(node.func, ast.Attribute) and node.func.attr in forbidden_calls))
        ]
        self.assertEqual(writes_before_admission, [])

    def test_r66_plan_rejects_r65_build_identity(self) -> None:
        plan = self._r66_plan()
        plan["build_id"] = "r6a-r65-recovery-dual-lineage-old"
        with self.assertRaisesRegex(RuntimeError, "fresh dual-lineage build ID"):
            run_r6a_execution._validate_r66_recovery_plan(plan)

    def test_r69_plan_accepts_r69_build_and_admission_identity_namespaces(self) -> None:
        plan = self._r66_plan()
        plan["build_id"] = "r6a-r69-dual-lineage-unit-check"
        admission_id = "r6a-r69-admission-unit-check"
        plan["admission_identity"] = {
            "admission_id": admission_id,
            "sha256": _sha(_canonical({"admission_id": admission_id})),
        }
        run_r6a_execution._validate_r66_recovery_plan(plan)

    def test_r70_plan_accepts_only_fresh_r70_identity_namespaces(self) -> None:
        plan = self._r66_plan()
        admission_id = "r6a-r70-admission-unit-check"
        plan["build_id"] = "r6a-r70-dual-lineage-unit-check"
        plan["build_workspace"] = str(
            build_retention.BUILD_ROOT / plan["build_id"]
            / build_retention.BUILD_SESSION_WORKSPACE_NAME)
        plan["environment"]["HOME"] = str(Path(plan["build_workspace"]) / ".home")
        plan["admission_identity"] = {
            "admission_id": admission_id,
            "sha256": _sha(_canonical({"admission_id": admission_id})),
        }
        run_r6a_execution._validate_r66_recovery_plan(plan)
        plan["admission_identity"]["admission_id"] = "r6a-r69-admission-old"
        with self.assertRaisesRegex(RuntimeError, "fresh dual-lineage"):
            run_r6a_execution._validate_r66_recovery_plan(plan)

    def test_r72_plan_accepts_only_fresh_r72_identity_namespaces(self) -> None:
        plan = self._r66_plan()
        admission_id = "r6a-r72-admission-unit-check"
        build_id = "r6a-r72-dual-lineage-unit-check"
        plan["build_id"] = build_id
        plan["build_workspace"] = str(
            build_retention.BUILD_ROOT / build_id
            / build_retention.BUILD_SESSION_WORKSPACE_NAME)
        plan["environment"]["HOME"] = str(Path(plan["build_workspace"]) / ".home")
        plan["admission_identity"] = {
            "admission_id": admission_id,
            "sha256": _sha(_canonical({"admission_id": admission_id})),
        }
        run_r6a_execution._validate_r66_recovery_plan(plan)
        plan["admission_identity"]["admission_id"] = "r6a-r71-admission-old"
        with self.assertRaisesRegex(RuntimeError, "fresh dual-lineage"):
            run_r6a_execution._validate_r66_recovery_plan(plan)

        self.assertTrue(run_r6a_execution.R72_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R72-CAPACITY-RECALIBRATION-CANDIDATE-1.json"))
        self.assertFalse(run_r6a_execution.R72_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-2.json"))

    def test_r73_plan_candidate_and_override_use_only_the_exact_r73_route(self) -> None:
        plan = self._r66_plan()
        admission_id = "r6a-r73-admission-unit-check"
        build_id = "r6a-r73-dual-lineage-unit-check"
        plan["build_id"] = build_id
        plan["build_workspace"] = str(
            build_retention.BUILD_ROOT / build_id
            / build_retention.BUILD_SESSION_WORKSPACE_NAME)
        plan["environment"]["HOME"] = str(Path(plan["build_workspace"]) / ".home")
        plan["admission_identity"] = {
            "admission_id": admission_id,
            "sha256": _sha(_canonical({"admission_id": admission_id})),
        }
        run_r6a_execution._validate_r66_recovery_plan(plan)

        candidate_path = "R6-A-R73-60GIB-POLICY-MIGRATION-CANDIDATE-1.json"
        self.assertTrue(run_r6a_execution.R73_CANDIDATE_PATH_PATTERN.fullmatch(candidate_path))
        self.assertFalse(run_r6a_execution.R73_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R73-60GIB-POLICY-MIGRATION-CANDIDATE-2.json"))
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R73_PLAN_PATH": "r73-plan.json",
            "FG_METAL_R72_PLAN_PATH": "r72-plan.json",
        }), "r73-plan.json")
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R72_PLAN_PATH": "r72-plan.json",
        }), "r72-plan.json")

        plan["admission_identity"]["admission_id"] = "r6a-r72-admission-cross-milestone"
        with self.assertRaisesRegex(RuntimeError, "fresh dual-lineage"):
            run_r6a_execution._validate_r66_recovery_plan(plan)

    def test_r74_plan_candidate_and_override_use_only_the_exact_r74_route(self) -> None:
        plan = self._r66_plan()
        admission_id = "r6a-r74-admission-unit-check"
        build_id = "r6a-r74-dual-lineage-unit-check"
        plan["build_id"] = build_id
        plan["build_workspace"] = str(
            build_retention.BUILD_ROOT / build_id
            / build_retention.BUILD_SESSION_WORKSPACE_NAME)
        plan["environment"]["HOME"] = str(Path(plan["build_workspace"]) / ".home")
        plan["admission_identity"] = {
            "admission_id": admission_id,
            "sha256": _sha(_canonical({"admission_id": admission_id})),
        }
        run_r6a_execution._validate_r66_recovery_plan(plan)

        candidate_path = "R6-A-R74-HISTORICAL-OUTPUT-ACCOUNTING-CANDIDATE-1.json"
        self.assertTrue(run_r6a_execution.R74_CANDIDATE_PATH_PATTERN.fullmatch(candidate_path))
        self.assertFalse(run_r6a_execution.R74_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R74-HISTORICAL-OUTPUT-ACCOUNTING-CANDIDATE-2.json"))
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R74_PLAN_PATH": "r74-plan.json",
            "FG_METAL_R73_PLAN_PATH": "r73-plan.json",
        }), "r74-plan.json")
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R73_PLAN_PATH": "r73-plan.json",
        }), "r73-plan.json")
        self.assertEqual(run_r6a_execution.R74_DEPENDENCY_MANIFEST_PATH,
                         run_r6a_execution.r6a_dependency_manifest.R74_MANIFEST_NAME)
        self.assertEqual(run_r6a_execution.R74_CHANGED_SOURCE_FILES, {
            "experiments/storage/build_retention.py",
            "experiments/storage/r6a_dependency_manifest.py",
            "experiments/storage/run_r6a_execution.py",
            "experiments/storage/storage_policy.py",
        })
        self.assertEqual(run_r6a_execution.R74_CHANGED_TEST_FILES, {
            "experiments/storage/test_build_retention.py",
            "experiments/storage/test_r6a_dependency_manifest.py",
            "experiments/storage/test_run_r6a_execution.py",
            "experiments/storage/test_storage_policy.py",
        })

        plan["admission_identity"]["admission_id"] = "r6a-r73-admission-cross-milestone"
        with self.assertRaisesRegex(RuntimeError, "fresh dual-lineage"):
            run_r6a_execution._validate_r66_recovery_plan(plan)

    def test_r75_plan_candidate_and_override_use_exact_r75_route(self) -> None:
        plan = self._r66_plan()
        admission_id = "r6a-r75-admission-unit-check"
        build_id = "r6a-r75-dual-lineage-unit-check"
        plan["build_id"] = build_id
        plan["build_workspace"] = str(
            build_retention.BUILD_ROOT / build_id
            / build_retention.BUILD_SESSION_WORKSPACE_NAME)
        plan["environment"]["HOME"] = str(Path(plan["build_workspace"]) / ".home")
        plan["admission_identity"] = {
            "admission_id": admission_id,
            "sha256": _sha(_canonical({"admission_id": admission_id})),
        }
        run_r6a_execution._validate_r66_recovery_plan(plan)
        candidate = "R6-A-R75-G0C-EVIDENCE-SCHEMA-CANDIDATE-1.json"
        self.assertTrue(run_r6a_execution.R75_CANDIDATE_PATH_PATTERN.fullmatch(candidate))
        self.assertFalse(run_r6a_execution.R75_CANDIDATE_PATH_PATTERN.fullmatch(candidate + ".bak"))
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R75_PLAN_PATH": "r75-plan.json",
            "FG_METAL_R74_PLAN_PATH": "r74-plan.json",
        }), "r75-plan.json")
        self.assertEqual(run_r6a_execution.R75_DEPENDENCY_MANIFEST_PATH,
                         run_r6a_execution.r6a_dependency_manifest.R75_MANIFEST_NAME)
        self.assertIn("experiments/storage/storage_gate0.py",
                      run_r6a_execution.R75_CHANGED_SOURCE_FILES)
        self.assertIn("experiments/storage/test_g0c_checks.py",
                      run_r6a_execution.R75_CHANGED_TEST_FILES)

    def test_r76_runner_route_accepts_only_matching_fresh_active_plan_ids(self) -> None:
        plan = self._r66_plan()
        admission_id = "r6a-r76-admission-future-plan-bound"
        build_id = "r6a-r76-dual-lineage-future-plan-bound"
        plan["build_id"] = build_id
        plan["build_workspace"] = str(
            build_retention.BUILD_ROOT / build_id
            / build_retention.BUILD_SESSION_WORKSPACE_NAME)
        plan["environment"]["HOME"] = str(Path(plan["build_workspace"]) / ".home")
        plan["admission_identity"] = {
            "admission_id": admission_id,
            "sha256": _sha(_canonical({"admission_id": admission_id})),
        }
        run_r6a_execution._validate_r66_recovery_plan(plan)
        validated = build_retention._session_validate_plan(
            plan, build_id=build_id, build_path=build_retention.BUILD_ROOT / build_id,
            source_tree_hash=plan["source_identity"]["sha256"],
            expected_bytes=plan["reservation_bytes"])
        run_r6a_execution._validate_r66_recovery_plan(validated)
        self.assertEqual(run_r6a_execution._r6a_id_milestone(
            build_id, run_r6a_execution._R6A_BUILD_ID_PATTERN), "R76")
        self.assertIsNone(run_r6a_execution._r6a_id_milestone(
            "r6a-r076-dual-lineage-leading-zero", run_r6a_execution._R6A_BUILD_ID_PATTERN))
        self.assertEqual(run_r6a_execution._r6a_id_milestone(
            "r6a-r100-dual-lineage-future", run_r6a_execution._R6A_BUILD_ID_PATTERN), "R100")
        self.assertEqual(storage_policy._r6a_admission_milestone(admission_id), 76)
        self.assertEqual(storage_policy._r6a_admission_milestone(
            "r6a-r100-admission-future"), 100)
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            with (mock.patch.object(storage_policy, "_experiment_manifest_path",
                                    return_value=Path(temp) / "missing.json"),
                  mock.patch.object(storage_policy,
                                    "_assert_r66_r69_admission_id_unclaimed") as admission_guard):
                run_r6a_execution._assert_r66_admission_id_unclaimed(admission_id)
            admission_guard.assert_called_once_with(admission_id)
        with mock.patch.object(build_retention, "_assert_build_id_unclaimed") as build_guard:
            run_r6a_execution._assert_r66_build_id_unconsumed(build_id)
        build_guard.assert_called_once_with(build_id)
        self.assertTrue(build_retention._is_r6a_r71_admission_id(admission_id))
        self.assertFalse(build_retention._is_r6a_r71_admission_id(
            "r6a-r65-admission-historical"))
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R80_PLAN_PATH": "r80-plan.json",
            "FG_METAL_R79_PLAN_PATH": "r79-plan.json",
            "FG_METAL_R78_PLAN_PATH": "r78-plan.json",
        }), "r80-plan.json")
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R79_PLAN_PATH": "r79-plan.json",
            "FG_METAL_R78_PLAN_PATH": "r78-plan.json",
        }), "r79-plan.json")
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R76_PLAN_PATH": "r76-plan.json",
            "FG_METAL_R75_PLAN_PATH": "r75-plan.json",
        }), "r76-plan.json")
        self.assertEqual(run_r6a_execution._configured_plan_override({
            "FG_METAL_R77_PLAN_PATH": "r77-plan.json",
            "FG_METAL_R76_PLAN_PATH": "r76-plan.json",
        }), "r77-plan.json")
        self.assertEqual(run_r6a_execution.R76_DEPENDENCY_MANIFEST_PATH,
                         run_r6a_execution.r6a_dependency_manifest.R76_MANIFEST_NAME)
        self.assertTrue(run_r6a_execution.R76_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R76-RUNNER-ROUTE-CANDIDATE-1.json"))
        self.assertFalse(run_r6a_execution.R76_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R76-RUNNER-ROUTE-CANDIDATE-2.json"))
        self.assertEqual(run_r6a_execution.R77_DEPENDENCY_MANIFEST_PATH,
                         run_r6a_execution.r6a_dependency_manifest.R77_MANIFEST_NAME)
        self.assertTrue(run_r6a_execution.R77_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R77-GATE-DRIFT-DIAGNOSTIC-CANDIDATE-1.json"))
        self.assertFalse(run_r6a_execution.R77_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R77-GATE-DRIFT-DIAGNOSTIC-CANDIDATE-2.json"))
        self.assertEqual(run_r6a_execution.R78_DEPENDENCY_MANIFEST_PATH,
                         run_r6a_execution.r6a_dependency_manifest.R78_MANIFEST_NAME)
        self.assertEqual(run_r6a_execution.R78_PLAN_NAME,
                         "R6-A-R78-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json")
        self.assertTrue(run_r6a_execution.R78_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R78-BOUNDED-GATE-TRANSITION-CANDIDATE-1.json"))
        self.assertFalse(run_r6a_execution.R78_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R78-BOUNDED-GATE-TRANSITION-CANDIDATE-2.json"))
        self.assertEqual(run_r6a_execution.R79_DEPENDENCY_MANIFEST_PATH,
                         run_r6a_execution.r6a_dependency_manifest.R79_MANIFEST_NAME)
        self.assertEqual(run_r6a_execution.R79_PLAN_NAME,
                         "R6-A-R79-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json")
        self.assertTrue(run_r6a_execution.R79_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R79-FAILED-OUTPUT-DISPOSITION-CANDIDATE-1.json"))
        self.assertFalse(run_r6a_execution.R79_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R79-FAILED-OUTPUT-DISPOSITION-CANDIDATE-2.json"))
        self.assertEqual(run_r6a_execution.R80_DEPENDENCY_MANIFEST_PATH,
                         run_r6a_execution.r6a_dependency_manifest.R80_MANIFEST_NAME)
        self.assertEqual(run_r6a_execution.R80_PLAN_NAME,
                         "R6-A-R80-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json")
        self.assertTrue(run_r6a_execution.R80_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-1.json"))
        self.assertFalse(run_r6a_execution.R80_CANDIDATE_PATH_PATTERN.fullmatch(
            "R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-2.json"))

        for wrong_build, wrong_admission in (
                ("r6a-r76-dual-lineage-../escape", admission_id),
                (build_id, "r6a-r76-admission-../escape"),
                ("r6a-r75-dual-lineage-safe-but-unbound", admission_id),
                (build_id, "r6a-r75-admission-safe-but-unbound"),
                ("r6a-r77-dual-lineage-safe-but-unbound", admission_id),
                ("r6a-r65-dual-lineage-historical", "r6a-r65-admission-historical")):
            invalid = copy.deepcopy(plan)
            invalid["build_id"] = wrong_build
            invalid["admission_identity"]["admission_id"] = wrong_admission
            invalid["admission_identity"]["sha256"] = _sha(
                _canonical({"admission_id": wrong_admission}))
            with self.subTest(build_id=wrong_build, admission_id=wrong_admission):
                with self.assertRaisesRegex(RuntimeError, "fresh dual-lineage"):
                    run_r6a_execution._validate_r66_recovery_plan(invalid)

    def test_r76_durable_claim_and_build_history_remain_one_use_across_plan_hashes(self) -> None:
        admission_id = "r6a-r76-admission-claim-once"
        build_id = "r6a-r76-dual-lineage-claim-once"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            cache = Path(temp) / "cache"
            claims = cache / "r66-r69-admission-claims"
            with mock.patch.object(storage_policy, "CACHE", cache):
                claim = storage_policy._claim_r66_r69_admission_id(
                    admission_id, "a" * 64,
                    _sha(_canonical({"admission_id": admission_id})), "ALLOW",
                    build_id=build_id, claims_root=claims)
                record = json.loads(Path(claim["claim_path"]).read_bytes())
                self.assertEqual(record["milestone"], "R76")
                self.assertEqual(record["build_id"], build_id)
                with self.assertRaisesRegex(RuntimeError, "already been consumed"):
                    storage_policy._claim_r66_r69_admission_id(
                        admission_id, "b" * 64,
                        _sha(_canonical({"admission_id": admission_id})), "ALLOW",
                        build_id=build_id, claims_root=claims)
                with self.assertRaisesRegex(RuntimeError, "already been consumed"):
                    storage_policy._assert_r70_build_id_unclaimed(
                        build_id, claims_root=claims)
                with mock.patch.object(storage_policy, "_experiment_manifest_path",
                                       return_value=Path(temp) / "missing.json"):
                    with self.assertRaisesRegex(RuntimeError, "already been claimed"):
                        run_r6a_execution._assert_r66_admission_id_unclaimed(admission_id)

    def test_r70_claim_consumes_admission_and_build_ids_together(self) -> None:
        admission_id = "r6a-r70-admission-one-shot"
        build_id = "r6a-r70-dual-lineage-one-shot"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            cache = Path(temp) / "cache"
            lock_file = None
            with mock.patch.object(run_r6a_execution.storage_policy, "CACHE", cache):
                run_r6a_execution.storage_policy._assert_r70_build_id_unclaimed(
                    build_id, claims_root=cache / "r66-r69-admission-claims")
                lock_file = run_r6a_execution.storage_policy._acquire_storage_allocation_lock()
                try:
                    claim = run_r6a_execution.storage_policy._claim_r66_r69_admission_id(
                        admission_id, "a" * 64,
                        _sha(_canonical({"admission_id": admission_id})), "ALLOW",
                        build_id=build_id, allocation_lock=lock_file)
                    record = json.loads(Path(claim["claim_path"]).read_bytes())
                    self.assertEqual(record["build_id"], build_id)
                    self.assertEqual(record["plan_canonical_sha256"], "a" * 64)
                    # Simulates a crash after the durable claim and before manifest publication.
                    self.assertFalse((Path(temp) / "experiments/storage/manifests"
                                      / f"{admission_id}.json").exists())
                    with self.assertRaisesRegex(RuntimeError, "build ID has already been consumed"):
                        run_r6a_execution.storage_policy._claim_r66_r69_admission_id(
                            "r6a-r70-admission-second", "b" * 64,
                            _sha(_canonical({"admission_id": "r6a-r70-admission-second"})),
                            "ALLOW", build_id=build_id, allocation_lock=lock_file)
                finally:
                    run_r6a_execution.storage_policy._release_storage_allocation_lock(lock_file)
            with mock.patch.object(run_r6a_execution.storage_policy, "CACHE", cache):
                with self.assertRaisesRegex(RuntimeError, "already been claimed"):
                    run_r6a_execution.storage_policy._assert_r66_r69_admission_id_unclaimed(admission_id)

    def test_r73_claim_records_exact_milestone_and_consumes_build_identity(self) -> None:
        admission_id = "r6a-r73-admission-one-shot"
        build_id = "r6a-r73-dual-lineage-one-shot"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            cache = Path(temp) / "cache"
            claims_root = cache / "r66-r69-admission-claims"
            with mock.patch.object(run_r6a_execution.storage_policy, "CACHE", cache):
                run_r6a_execution.storage_policy._assert_r70_build_id_unclaimed(
                    build_id, claims_root=claims_root)
                lock_file = run_r6a_execution.storage_policy._acquire_storage_allocation_lock()
                try:
                    claim = run_r6a_execution.storage_policy._claim_r66_r69_admission_id(
                        admission_id, "a" * 64,
                        _sha(_canonical({"admission_id": admission_id})), "ALLOW",
                        build_id=build_id, allocation_lock=lock_file)
                finally:
                    run_r6a_execution.storage_policy._release_storage_allocation_lock(lock_file)
                record = json.loads(Path(claim["claim_path"]).read_bytes())
                self.assertEqual(record["milestone"], "R73")
                self.assertEqual(record["build_id"], build_id)
                with self.assertRaisesRegex(RuntimeError, "build ID has already been consumed"):
                    run_r6a_execution.storage_policy._assert_r70_build_id_unclaimed(
                        build_id, claims_root=claims_root)
                with self.assertRaisesRegex(RuntimeError, "claim inputs are malformed"):
                    run_r6a_execution.storage_policy._claim_r66_r69_admission_id(
                        "r6a-r73-admission-../alias", "b" * 64,
                        _sha(_canonical({"admission_id": "r6a-r73-admission-../alias"})),
                        "ALLOW", build_id="r6a-r73-dual-lineage-alias")

    def test_r74_claim_records_exact_milestone_and_consumes_build_identity(self) -> None:
        admission_id = "r6a-r74-admission-one-shot"
        build_id = "r6a-r74-dual-lineage-one-shot"
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            cache = Path(temp) / "cache"
            claims_root = cache / "r66-r69-admission-claims"
            with mock.patch.object(run_r6a_execution.storage_policy, "CACHE", cache):
                run_r6a_execution.storage_policy._assert_r70_build_id_unclaimed(
                    build_id, claims_root=claims_root)
                lock_file = run_r6a_execution.storage_policy._acquire_storage_allocation_lock()
                try:
                    claim = run_r6a_execution.storage_policy._claim_r66_r69_admission_id(
                        admission_id, "a" * 64,
                        _sha(_canonical({"admission_id": admission_id})), "ALLOW",
                        build_id=build_id, allocation_lock=lock_file)
                finally:
                    run_r6a_execution.storage_policy._release_storage_allocation_lock(lock_file)
                record = json.loads(Path(claim["claim_path"]).read_bytes())
                self.assertEqual(record["milestone"], "R74")
                self.assertEqual(record["build_id"], build_id)
                with self.assertRaisesRegex(RuntimeError, "build ID has already been consumed"):
                    run_r6a_execution.storage_policy._assert_r70_build_id_unclaimed(
                        build_id, claims_root=claims_root)
                with self.assertRaisesRegex(RuntimeError, "claim inputs are malformed"):
                    run_r6a_execution.storage_policy._claim_r66_r69_admission_id(
                        "r6a-r74-admission-../alias", "b" * 64,
                        _sha(_canonical({"admission_id": "r6a-r74-admission-../alias"})),
                        "ALLOW", build_id="r6a-r74-dual-lineage-alias")

    def test_r70_crash_c1_to_c5_durable_identity_matrix(self) -> None:
        """Inject process stops around claim/ALLOW and inspect durable identity state."""
        for boundary in ("C1", "C2", "C3", "C4", "C5"):
            with self.subTest(boundary=boundary), tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
                root = Path(temp)
                cache = root / "cache"
                claims_root = cache / "r66-r69-admission-claims"
                admission_id = f"r6a-r70-admission-crash-{boundary.lower()}"
                build_id = f"r6a-r70-dual-lineage-crash-{boundary.lower()}"
                manifest_path = root / "experiments/storage/manifests" / f"{admission_id}.json"
                manifest_path.parent.mkdir(parents=True)
                with (mock.patch.object(run_r6a_execution.storage_policy, "CACHE", cache),
                      mock.patch.object(run_r6a_execution.storage_policy,
                                        "_experiment_manifest_path", return_value=manifest_path)):
                    if boundary != "C1":
                        lock = run_r6a_execution.storage_policy._acquire_storage_allocation_lock()
                        try:
                            claim = run_r6a_execution.storage_policy._claim_r66_r69_admission_id(
                                admission_id, "a" * 64,
                                _sha(_canonical({"admission_id": admission_id})), "ALLOW",
                                build_id=build_id, allocation_lock=lock)
                        finally:
                            run_r6a_execution.storage_policy._release_storage_allocation_lock(lock)
                    if boundary in {"C3", "C4", "C5"}:
                        partial_or_complete_allow = {
                            "experiment_id": admission_id, "decision": "ALLOW",
                            "r70_build_id": build_id,
                            "admission_claim": {"path": claim["claim_path"],
                                                "sha256": claim["claim_sha256"],
                                                "build_id": build_id},
                        }
                        run_r6a_execution.storage_policy._write_json_exclusive(
                            manifest_path, partial_or_complete_allow, strict_directory_sync=True)

                    claim_path = claims_root / f"{admission_id}.claim"
                    self.assertEqual(claim_path.exists(), boundary != "C1")
                    self.assertEqual(manifest_path.exists(), boundary in {"C3", "C4", "C5"})
                    self.assertFalse((build_retention.LEASE_ROOT / f"{build_id}.json").exists(),
                                     "no crash before reserve may create a BuildLease record")
                    if boundary == "C1":
                        run_r6a_execution._assert_r66_admission_id_unclaimed(admission_id)
                        run_r6a_execution.storage_policy._assert_r70_build_id_unclaimed(
                            build_id, claims_root=claims_root)
                    else:
                        with self.assertRaisesRegex(RuntimeError, "already been claimed"):
                            run_r6a_execution.storage_policy._assert_r66_r69_admission_id_unclaimed(
                                admission_id, claims_root=claims_root)
                        with self.assertRaisesRegex(RuntimeError, "already been consumed"):
                            run_r6a_execution.storage_policy._assert_r70_build_id_unclaimed(
                                build_id, claims_root=claims_root)
                    if boundary in {"C3", "C4", "C5"}:
                        saved = json.loads(manifest_path.read_bytes())
                        self.assertEqual(saved["decision"], "ALLOW")
                        self.assertEqual(saved["r70_build_id"], build_id)
                        self.assertEqual(saved["admission_claim"]["build_id"], build_id)

    def test_r66_plan_rejects_missing_graph(self) -> None:
        plan = self._r66_plan()
        plan["build_graphs"] = plan["build_graphs"][:1]
        with self.assertRaisesRegex(RuntimeError, "ordered Windows/native graph pair"):
            run_r6a_execution._validate_r66_recovery_plan(plan)

    def _binding_fixture(self, root: Path, *, milestone: str = "R66") -> tuple[bytes, Path, bytes, dict[str, object]]:
        storage = root / "experiments/storage"
        storage.mkdir(parents=True)
        source_hash_paths = (run_r6a_execution.R86_CHANGED_SOURCE_FILES if milestone == "R86" else
                             run_r6a_execution.R80_CHANGED_SOURCE_FILES if milestone == "R80" else
                             run_r6a_execution.R79_CHANGED_SOURCE_FILES if milestone == "R79" else
                             run_r6a_execution.R78_CHANGED_SOURCE_FILES if milestone == "R78" else
                             run_r6a_execution.R66_CHANGED_SOURCE_FILES if milestone == "R66" else
                             run_r6a_execution.R77_CHANGED_SOURCE_FILES if milestone == "R77" else
                             run_r6a_execution.R76_CHANGED_SOURCE_FILES if milestone == "R76" else
                             run_r6a_execution.R69_CHANGED_SOURCE_FILES)
        test_hash_paths = (run_r6a_execution.R86_CHANGED_TEST_FILES if milestone == "R86" else
                           run_r6a_execution.R80_CHANGED_TEST_FILES if milestone == "R80" else
                           run_r6a_execution.R79_CHANGED_TEST_FILES if milestone == "R79" else
                           run_r6a_execution.R78_CHANGED_TEST_FILES if milestone == "R78" else
                           run_r6a_execution.R66_CHANGED_TEST_FILES if milestone == "R66" else
                           run_r6a_execution.R77_CHANGED_TEST_FILES if milestone == "R77" else
                           run_r6a_execution.R76_CHANGED_TEST_FILES if milestone == "R76" else
                           run_r6a_execution.R69_CHANGED_TEST_FILES)
        source_paths = sorted(source_hash_paths)
        test_paths = sorted(test_hash_paths)
        for relative in source_paths + test_paths:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"fixture:{relative}\n", encoding="utf-8")

        plan_path = (storage / run_r6a_execution.R86_PLAN_NAME if milestone == "R86" else
                     storage / run_r6a_execution.R80_PLAN_NAME if milestone == "R80" else
                     storage / run_r6a_execution.R79_PLAN_NAME if milestone == "R79" else
                     storage / run_r6a_execution.R78_PLAN_NAME if milestone == "R78" else
                     root / f"R6-A-{milestone}-UNIT-PLAN.json")
        plan = {"schema_version": 3, "build_configuration_key_sha256": "a" * 64}
        if milestone in {"R66", "R69", "R76", "R77", "R78", "R79", "R80", "R86"}:
            admission_id = f"r6a-r{milestone[1:]}-admission-g0c-fixture"
            plan["build_id"] = f"r6a-r{milestone[1:]}-dual-lineage-g0c-fixture"
            plan["admission_identity"] = {
                "admission_id": admission_id,
                "sha256": _sha(_canonical({"admission_id": admission_id})),
            }
        plan_bytes = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
        plan_path.write_bytes(plan_bytes)
        plan_relative = plan_path.relative_to(root).as_posix()
        plan_digest = _sha(_canonical(plan))
        plan_descriptor = {
            "path": plan_relative,
            "raw_sha256": _sha(plan_bytes),
            "canonical_sha256": plan_digest,
            "graph_pair_key": plan["build_configuration_key_sha256"],
        }

        diff_path = root / f"R6-A-{milestone}-UNIT.diff"
        diff_path.write_text("candidate diff\n", encoding="utf-8")
        contract_path = root / f"R6-A-{milestone}-UNIT-CONTRACT.md"
        contract_path.write_text("manifest first\n", encoding="utf-8")
        manifest_contract_path = root / f"R6-A-{milestone}-UNIT-MANIFEST-CONTRACT.md"
        manifest_contract_path.write_text("manifest commit contract\n", encoding="utf-8")
        crash_path = root / f"R6-A-{milestone}-UNIT-CRASH-MATRIX.md"
        crash_path.write_text("crash boundaries\n", encoding="utf-8")
        g0c_path = (root / run_r6a_execution.R86_FRESH_G0C_REVIEW_PATH
                    if milestone == "R86" else
                    root / f"R6-A-{milestone}-UNIT-G0C.md")
        g0c_path.parent.mkdir(parents=True, exist_ok=True)
        if milestone == "R86":
            g0c_path.write_text(json.dumps({
                "milestone": "R86", "status": "PASS", "answer": "NO", "findings": [],
            }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        else:
            g0c_path.write_text(
                "Can durable SUCCESS exist without a validated committed manifest? NO\n",
                encoding="utf-8")
        completion_path = root / f"R6-A-{milestone}-UNIT-COMPLETION-REVIEW.md"
        completion_verdicts = [
            "PASS — BUILDLEASE SUCCESS CANNOT BECOME DURABLE BEFORE ALL COMPLETION IDENTITY VALIDATION AND MANIFEST COMMIT REQUIREMENTS ARE DURABLY SATISFIED",
            "PASS — HOST V3 DUAL-LINEAGE FINALIZATION STILL BINDS BOTH GRAPH IDENTITIES, BOTH GRAPH KEYS, THE FROZEN SESSION PLAN, AND OUTPUT LINEAGE",
        ]
        completion_path.write_text("\n".join(completion_verdicts) + "\n", encoding="utf-8")

        transaction_contracts = {}
        if milestone in {"R76", "R77", "R78", "R79", "R80", "R86"}:
            contract_paths = (run_r6a_execution.R86_TRANSACTION_CONTRACT_FILES
                              if milestone == "R86" else
                              run_r6a_execution.R80_TRANSACTION_CONTRACT_FILES
                              if milestone == "R80" else
                              run_r6a_execution.R79_TRANSACTION_CONTRACT_FILES
                              if milestone == "R79" else
                              run_r6a_execution.R78_TRANSACTION_CONTRACT_FILES
                              if milestone == "R78" else
                              run_r6a_execution.R71_TRANSACTION_CONTRACT_FILES)
            for relative in sorted(contract_paths):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                if (milestone == "R86"
                        and relative == run_r6a_execution.R86_R81_G0C_PRESERVED_PATH):
                    path.write_text(json.dumps({
                        "status": "PASS", "findings": [], "reviewed_files": {},
                        "r78_binding": {"fixture": "preserved-r78-binding"},
                        "r79_binding": {"fixture": "preserved-r79-binding"},
                        "r80_binding": {"fixture": "preserved-r80-binding"},
                        "r81_binding": {"fixture": "preserved-r81-binding"},
                    }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
                elif (milestone == "R86"
                      and relative == run_r6a_execution.R86_CANONICAL_REPAIR_CONTRACT_PATH):
                    path.write_text("R86 canonical regression repair contract\n", encoding="utf-8")
                elif (milestone == "R86"
                      and relative in {
                          "experiments/storage/R6-A-R86-RUNNER-MAP.md",
                          "experiments/storage/R6-A-R86-COMPLETION-VERIFIER.md",
                      }):
                    path.write_text(f"R86 contract fixture: {relative}\n", encoding="utf-8")
                elif (milestone == "R80"
                        and relative == "experiments/storage/g0c-launcher-independent-review-R78-PRESERVED.json"):
                    path.write_text(json.dumps({
                        "status": "PASS", "findings": [], "reviewed_files": {},
                        "r78_binding": {"fixture": "preserved-r78-binding"},
                    }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
                elif (milestone == "R80"
                      and relative == run_r6a_execution.R80_R79_G0C_PRESERVED_PATH):
                    path.write_text(json.dumps({
                        "status": "PASS", "findings": [], "reviewed_files": {},
                        "r78_binding": {"fixture": "preserved-r78-binding"},
                        "r79_binding": {"fixture": "preserved-r79-binding"},
                    }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
                elif (milestone == "R79"
                        and relative == "experiments/storage/g0c-launcher-independent-review-R78-PRESERVED.json"):
                    path.write_text(json.dumps({
                        "status": "PASS", "findings": [],
                        "reviewed_files": {},
                        "r78_binding": {"fixture": "preserved-r78-binding"},
                    }, sort_keys=True, separators=(",", ":")), encoding="utf-8")
                else:
                    path.write_text(f"fixture:{relative}\n", encoding="utf-8")
                transaction_contracts[relative] = _sha(path.read_bytes())

        candidate: dict[str, object] = {
            "schema_version": 1,
            "milestone": milestone,
            **({"candidate_id": ("R6-A-R86-CANONICAL-REGRESSION-REPAIR-CANDIDATE-1"
                                  if milestone == "R86" else
                                  "R6-A-R79-FAILED-OUTPUT-DISPOSITION-CANDIDATE-1"
                                  if milestone == "R79" else
                                  "R6-A-R78-BOUNDED-GATE-TRANSITION-CANDIDATE-1"
                                  if milestone == "R78" else
                                  "R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-1"
                                  if milestone == "R80" else
                                  "R6-A-R77-GATE-DRIFT-DIAGNOSTIC-CANDIDATE-1"
                                  if milestone == "R77" else
                                  "R6-A-R76-RUNNER-ROUTE-CANDIDATE-1"),
               ("r86_binding" if milestone == "R86" else
                "r80_binding" if milestone == "R80" else
                "r79_binding" if milestone == "R79" else
                "r78_binding" if milestone == "R78" else
                "r77_binding" if milestone == "R77" else "r76_binding"):
                   {"execution_dependency_manifest": {
                       "path": (run_r6a_execution.R86_DEPENDENCY_MANIFEST_PATH
                                if milestone == "R86" else
                                run_r6a_execution.R80_DEPENDENCY_MANIFEST_PATH
                                if milestone == "R80" else
                                run_r6a_execution.R79_DEPENDENCY_MANIFEST_PATH
                                if milestone == "R79" else
                                run_r6a_execution.R78_DEPENDENCY_MANIFEST_PATH
                                if milestone == "R78" else
                                run_r6a_execution.R77_DEPENDENCY_MANIFEST_PATH
                                if milestone == "R77" else
                                run_r6a_execution.R76_DEPENDENCY_MANIFEST_PATH),
                       "sha256": "f" * 64}}} if milestone in {"R76", "R77", "R78", "R79", "R80", "R86"} else {}),
            "candidate_number": 2 if milestone == "R66" else 1,
            "status": "FROZEN_FOR_ACCEPTANCE_VALIDATION",
            "changed_source_hashes": {
                relative: _sha((root / relative).read_bytes()) for relative in source_paths
            },
            "changed_test_hashes": {
                relative: _sha((root / relative).read_bytes()) for relative in test_paths
            },
            "exact_diff": {"path": diff_path.name, "sha256": _sha(diff_path.read_bytes())},
            "execution_plan": plan_descriptor,
            "graph_pair_key": plan["build_configuration_key_sha256"],
            "completion_contract": {
                "path": contract_path.name,
                "sha256": _sha(contract_path.read_bytes()),
                "manifest_commit_contract_path": manifest_contract_path.name,
                "manifest_commit_contract_sha256": _sha(manifest_contract_path.read_bytes()),
            },
            "crash_matrix": {"path": crash_path.name, "sha256": _sha(crash_path.read_bytes())},
        }
        if milestone in {"R76", "R77", "R78", "R79", "R80", "R86"}:
            candidate["transaction_contracts"] = transaction_contracts
        candidate_path = (root / "R6-A-R86-CANONICAL-REGRESSION-REPAIR-CANDIDATE-1.json" if milestone == "R86" else
                          root / "R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-1.json" if milestone == "R80" else
                          root / "R6-A-R79-FAILED-OUTPUT-DISPOSITION-CANDIDATE-1.json" if milestone == "R79" else
                          root / "R6-A-R78-BOUNDED-GATE-TRANSITION-CANDIDATE-1.json" if milestone == "R78" else
                          root / "R6-A-R66-ATOMIC-COMPLETION-CANDIDATE-2.json"
                          if milestone == "R66" else
                          root / "R6-A-R77-GATE-DRIFT-DIAGNOSTIC-CANDIDATE-1.json" if milestone == "R77" else
                          root / "R6-A-R76-RUNNER-ROUTE-CANDIDATE-1.json" if milestone == "R76" else
                          root / "R6-A-R69-ADMISSION-BOUNDARY-CANDIDATE-1.json")
        candidate_bytes = json.dumps(candidate, sort_keys=True, separators=(",", ":")).encode()
        candidate_path.write_bytes(candidate_bytes)
        candidate_descriptor = ({
            "path": candidate_path.name,
            "candidate_id": candidate["candidate_id"],
            "candidate_core_sha256": run_r6a_execution.r6a_dependency_manifest._candidate_core_sha256(candidate),
        } if milestone in {"R76", "R77", "R78", "R79", "R80", "R86"} else {
            "path": candidate_path.name,
            "raw_sha256": _sha(candidate_bytes),
            "canonical_sha256": _sha(_canonical(candidate)),
        })
        g0c_relative = g0c_path.relative_to(root).as_posix()
        reviewed_paths = (source_paths + test_paths + [
            plan_relative, contract_path.name, manifest_contract_path.name,
            crash_path.name, g0c_relative,
            completion_path.name, diff_path.name,
        ])
        if milestone in {"R76", "R77", "R78", "R79", "R80", "R86"}:
            reviewed_paths += sorted(transaction_contracts)
        binding = {
            "schema_version": 1,
            "milestone": milestone,
            "status": "PASS",
            "execution_plan": plan_descriptor,
            "candidate": candidate_descriptor,
            "completion_contract": candidate["completion_contract"],
            "crash_matrix": candidate["crash_matrix"],
            "completion_review": {
                "path": completion_path.name,
                "sha256": _sha(completion_path.read_bytes()),
            },
            "completion_review_verdicts": completion_verdicts,
            "fresh_g0c_review": {
                "path": g0c_relative,
                "sha256": _sha(g0c_path.read_bytes()),
            },
            "fresh_g0c_answer": (
                "Can durable SUCCESS exist without a validated committed manifest? NO"),
            "files": {relative: _sha((root / relative).read_bytes()) for relative in reviewed_paths},
        }
        if milestone in {"R76", "R77", "R78", "R79", "R80", "R86"}:
            binding["execution_dependency_manifest"] = {
                "path": (run_r6a_execution.R86_DEPENDENCY_MANIFEST_PATH
                         if milestone == "R86" else
                         run_r6a_execution.R80_DEPENDENCY_MANIFEST_PATH
                         if milestone == "R80" else
                         run_r6a_execution.R79_DEPENDENCY_MANIFEST_PATH
                         if milestone == "R79" else
                         run_r6a_execution.R78_DEPENDENCY_MANIFEST_PATH
                         if milestone == "R78" else
                         run_r6a_execution.R77_DEPENDENCY_MANIFEST_PATH
                         if milestone == "R77" else
                         run_r6a_execution.R76_DEPENDENCY_MANIFEST_PATH),
                "sha256": "f" * 64,
            }
        binding_key = ("r86_binding" if milestone == "R86" else
                       "r66_binding" if milestone == "R66" else
                       "r80_binding" if milestone == "R80" else
                       "r79_binding" if milestone == "R79" else
                       "r78_binding" if milestone == "R78" else
                       "r77_binding" if milestone == "R77" else
                       "r76_binding" if milestone == "R76" else "r69_binding")
        review = {"status": "PASS", "findings": [], binding_key: binding}
        if milestone == "R79":
            preserved = json.loads((root / "experiments/storage/g0c-launcher-independent-review-R78-PRESERVED.json").read_bytes())
            review["r78_binding"] = preserved["r78_binding"]
        if milestone == "R80":
            preserved_r78 = json.loads((root / "experiments/storage/g0c-launcher-independent-review-R78-PRESERVED.json").read_bytes())
            preserved_r79 = json.loads((root / run_r6a_execution.R80_R79_G0C_PRESERVED_PATH).read_bytes())
            review["r78_binding"] = preserved_r78["r78_binding"]
            review["r79_binding"] = preserved_r79["r79_binding"]
        if milestone == "R86":
            preserved_r81 = json.loads((root / run_r6a_execution.R86_R81_G0C_PRESERVED_PATH).read_bytes())
            for key in ("r78_binding", "r79_binding", "r80_binding", "r81_binding"):
                review[key] = preserved_r81[key]
        review_bytes = json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
        validated_plan = copy.deepcopy(plan)
        return review_bytes, plan_path, plan_bytes, validated_plan

    def test_g0c_binding_accepts_exact_candidate_plan_and_diff(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            review_bytes, plan_path, plan_bytes, plan = self._binding_fixture(root)
            result = run_r6a_execution._validate_r66_g0c_binding(
                review_bytes,
                repo=root,
                plan_path=plan_path,
                plan_bytes=plan_bytes,
                plan_canonical_sha256=_sha(_canonical(plan)),
                validated_plan=plan,
            )
        self.assertRegex(result["candidate_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(result["binding_sha256"], r"^[0-9a-f]{64}$")

    def test_r86_g0c_binding_accepts_exact_candidate_plan_roster_and_r81_lineage(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            review_bytes, plan_path, plan_bytes, plan = self._binding_fixture(
                root, milestone="R86")
            result = run_r6a_execution._validate_r66_g0c_binding(
                review_bytes,
                repo=root,
                plan_path=plan_path,
                plan_bytes=plan_bytes,
                plan_canonical_sha256=_sha(_canonical(plan)),
                validated_plan=plan,
            )
        self.assertEqual(result["candidate_id"],
                         "R6-A-R86-CANONICAL-REGRESSION-REPAIR-CANDIDATE-1")
        self.assertEqual(result["dependency_manifest"]["path"],
                         run_r6a_execution.R86_DEPENDENCY_MANIFEST_PATH)
        self.assertEqual(result["g0c_report_path"],
                         str(root / run_r6a_execution.R86_FRESH_G0C_REVIEW_PATH))

    def test_g0c_binding_rejects_source_change_after_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            review_bytes, plan_path, plan_bytes, plan = self._binding_fixture(root)
            (root / "experiments/storage/build_retention.py").write_text(
                "changed after review\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "source hash is stale"):
                run_r6a_execution._validate_r66_g0c_binding(
                    review_bytes,
                    repo=root,
                    plan_path=plan_path,
                    plan_bytes=plan_bytes,
                    plan_canonical_sha256=_sha(_canonical(plan)),
                    validated_plan=plan,
                )

    def test_r69_g0c_binding_accepts_exact_candidate_and_plan(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            review_bytes, plan_path, plan_bytes, plan = self._binding_fixture(root, milestone="R69")
            result = run_r6a_execution._validate_r66_g0c_binding(
                review_bytes,
                repo=root,
                plan_path=plan_path,
                plan_bytes=plan_bytes,
                plan_canonical_sha256=_sha(_canonical(plan)),
                validated_plan=plan,
            )
        self.assertEqual(result["candidate_path"], str(root / "R6-A-R69-ADMISSION-BOUNDARY-CANDIDATE-1.json"))

    def test_stale_r66_g0c_binding_rejects_fresh_r69_plan(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            review_bytes, _old_plan_path, plan_bytes, plan = self._binding_fixture(root)
            fresh_r69_plan_path = root / "R6-A-R69-ADMISSION-BOUNDARY-EXECUTION-PLAN.json"
            fresh_r69_plan_path.write_bytes(plan_bytes)
            with self.assertRaisesRegex(RuntimeError, "different milestone"):
                run_r6a_execution._validate_r66_g0c_binding(
                    review_bytes,
                    repo=root,
                    plan_path=fresh_r69_plan_path,
                    plan_bytes=plan_bytes,
                    plan_canonical_sha256=_sha(_canonical(plan)),
                    validated_plan=plan,
                )

    def test_r76_g0c_candidate_plan_and_manifest_selection_are_exact(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            review_bytes, plan_path, plan_bytes, plan = self._binding_fixture(root, milestone="R76")
            result = run_r6a_execution._validate_r66_g0c_binding(
                review_bytes, repo=root, plan_path=plan_path, plan_bytes=plan_bytes,
                plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)
            self.assertEqual(result["candidate_id"], "R6-A-R76-RUNNER-ROUTE-CANDIDATE-1")
            self.assertEqual(result["dependency_manifest"]["path"],
                             run_r6a_execution.R76_DEPENDENCY_MANIFEST_PATH)

            for wrong_milestone in ("R75", "R77", "R100"):
                with self.subTest(wrong_g0c_plan_milestone=wrong_milestone):
                    wrong_ids = copy.deepcopy(plan)
                    wrong_ids["build_id"] = f"r6a-r{wrong_milestone[1:]}-dual-lineage-safe-id"
                    wrong_admission_id = f"r6a-r{wrong_milestone[1:]}-admission-safe-id"
                    wrong_ids["admission_identity"] = {
                        "admission_id": wrong_admission_id,
                        "sha256": _sha(_canonical({"admission_id": wrong_admission_id})),
                    }
                    wrong_plan_bytes = _canonical(wrong_ids)
                    plan_path.write_bytes(wrong_plan_bytes)
                    with self.assertRaisesRegex(RuntimeError, "route milestone"):
                        run_r6a_execution._validate_r66_g0c_binding(
                            review_bytes, repo=root, plan_path=plan_path, plan_bytes=wrong_plan_bytes,
                            plan_canonical_sha256=_sha(_canonical(wrong_ids)), validated_plan=wrong_ids)
            plan_path.write_bytes(plan_bytes)

            # Safe syntax alone does not bind a replacement ID to this exact
            # frozen plan descriptor. Keep the R76 route and plan filename,
            # but alter IDs and their canonical digest while retaining G0C.
            safe_unbound = copy.deepcopy(plan)
            safe_unbound["build_id"] = "r6a-r76-dual-lineage-safe-unbound"
            safe_admission_id = "r6a-r76-admission-safe-unbound"
            safe_unbound["admission_identity"] = {
                "admission_id": safe_admission_id,
                "sha256": _sha(_canonical({"admission_id": safe_admission_id})),
            }
            safe_unbound_bytes = _canonical(safe_unbound)
            plan_path.write_bytes(safe_unbound_bytes)
            with self.assertRaisesRegex(RuntimeError, "exact plan and graph pair"):
                run_r6a_execution._validate_r66_g0c_binding(
                    review_bytes, repo=root, plan_path=plan_path, plan_bytes=safe_unbound_bytes,
                    plan_canonical_sha256=_sha(_canonical(safe_unbound)), validated_plan=safe_unbound)
            plan_path.write_bytes(plan_bytes)

            stale_plan_path = root / "R6-A-R75-STALE-PLAN.json"
            stale_plan_path.write_bytes(plan_bytes)
            with self.assertRaisesRegex(RuntimeError, "different milestone"):
                run_r6a_execution._validate_r66_g0c_binding(
                    review_bytes, repo=root, plan_path=stale_plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)

    def test_r77_g0c_candidate_plan_and_manifest_selection_are_exact(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            review_bytes, plan_path, plan_bytes, plan = self._binding_fixture(root, milestone="R77")
            result = run_r6a_execution._validate_r66_g0c_binding(
                review_bytes, repo=root, plan_path=plan_path, plan_bytes=plan_bytes,
                plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)
            self.assertEqual(result["candidate_id"],
                             "R6-A-R77-GATE-DRIFT-DIAGNOSTIC-CANDIDATE-1")
            self.assertEqual(result["dependency_manifest"]["path"],
                             run_r6a_execution.R77_DEPENDENCY_MANIFEST_PATH)

            for wrong_milestone in ("R76", "R78", "R100"):
                with self.subTest(wrong_g0c_plan_milestone=wrong_milestone):
                    wrong_ids = copy.deepcopy(plan)
                    wrong_ids["build_id"] = f"r6a-r{wrong_milestone[1:]}-dual-lineage-safe-id"
                    wrong_admission_id = f"r6a-r{wrong_milestone[1:]}-admission-safe-id"
                    wrong_ids["admission_identity"] = {
                        "admission_id": wrong_admission_id,
                        "sha256": _sha(_canonical({"admission_id": wrong_admission_id})),
                    }
                    wrong_plan_bytes = _canonical(wrong_ids)
                    plan_path.write_bytes(wrong_plan_bytes)
                    with self.assertRaisesRegex(RuntimeError, "route milestone"):
                        run_r6a_execution._validate_r66_g0c_binding(
                            review_bytes, repo=root, plan_path=plan_path, plan_bytes=wrong_plan_bytes,
                            plan_canonical_sha256=_sha(_canonical(wrong_ids)),
                            validated_plan=wrong_ids)
            plan_path.write_bytes(plan_bytes)

            unbound = copy.deepcopy(plan)
            unbound["build_id"] = "r6a-r77-dual-lineage-safe-unbound"
            unbound_admission_id = "r6a-r77-admission-safe-unbound"
            unbound["admission_identity"] = {
                "admission_id": unbound_admission_id,
                "sha256": _sha(_canonical({"admission_id": unbound_admission_id})),
            }
            unbound_bytes = _canonical(unbound)
            plan_path.write_bytes(unbound_bytes)
            with self.assertRaisesRegex(RuntimeError, "exact plan and graph pair"):
                run_r6a_execution._validate_r66_g0c_binding(
                    review_bytes, repo=root, plan_path=plan_path, plan_bytes=unbound_bytes,
                    plan_canonical_sha256=_sha(_canonical(unbound)), validated_plan=unbound)

    def test_r78_g0c_candidate_plan_and_manifest_selection_are_exact(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            review_bytes, plan_path, plan_bytes, plan = self._binding_fixture(root, milestone="R78")
            result = run_r6a_execution._validate_r66_g0c_binding(
                review_bytes, repo=root, plan_path=plan_path, plan_bytes=plan_bytes,
                plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)
            self.assertEqual(result["candidate_id"],
                             "R6-A-R78-BOUNDED-GATE-TRANSITION-CANDIDATE-1")
            self.assertEqual(result["dependency_manifest"]["path"],
                             run_r6a_execution.R78_DEPENDENCY_MANIFEST_PATH)

            wrong_ids = copy.deepcopy(plan)
            wrong_ids["build_id"] = "r6a-r77-dual-lineage-unbound"
            wrong_admission = "r6a-r77-admission-unbound"
            wrong_ids["admission_identity"] = {
                "admission_id": wrong_admission,
                "sha256": _sha(_canonical({"admission_id": wrong_admission})),
            }
            wrong_bytes = _canonical(wrong_ids)
            plan_path.write_bytes(wrong_bytes)
            with self.assertRaisesRegex(RuntimeError, "route milestone"):
                run_r6a_execution._validate_r66_g0c_binding(
                    review_bytes, repo=root, plan_path=plan_path, plan_bytes=wrong_bytes,
                    plan_canonical_sha256=_sha(_canonical(wrong_ids)), validated_plan=wrong_ids)

    def test_r79_failed_output_route_accepts_only_exact_plan_and_rejects_aliases(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            review_bytes, plan_path, plan_bytes, plan = self._binding_fixture(root, milestone="R79")
            result = run_r6a_execution._validate_r66_g0c_binding(
                review_bytes, repo=root, plan_path=plan_path, plan_bytes=plan_bytes,
                plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)
            self.assertEqual(result["candidate_id"],
                             "R6-A-R79-FAILED-OUTPUT-DISPOSITION-CANDIDATE-1")
            self.assertEqual(result["dependency_manifest"]["path"],
                             run_r6a_execution.R79_DEPENDENCY_MANIFEST_PATH)
            self.assertEqual(plan_path.relative_to(root).as_posix(),
                             "experiments/storage/" + run_r6a_execution.R79_PLAN_NAME)

            wrong_path = plan_path.with_name(run_r6a_execution.R79_PLAN_NAME.replace(
                ".json", "-ALIAS.json"))
            wrong_path.write_bytes(plan_bytes)
            with self.assertRaisesRegex(RuntimeError,
                                        "R79 G0C binding requires its one canonical dual-lineage plan path"):
                run_r6a_execution._validate_r66_g0c_binding(
                    review_bytes, repo=root, plan_path=wrong_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)

            wrong_milestone_path = plan_path.with_name(
                "R6-A-R78-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json")
            wrong_milestone_path.write_bytes(plan_bytes)
            with self.assertRaisesRegex(RuntimeError, "different milestone"):
                run_r6a_execution._validate_r66_g0c_binding(
                    review_bytes, repo=root, plan_path=wrong_milestone_path,
                    plan_bytes=plan_bytes, plan_canonical_sha256=_sha(_canonical(plan)),
                    validated_plan=plan)

            changed_review = json.loads(review_bytes)
            changed_review["r78_binding"] = {"fixture": "altered-history"}
            with self.assertRaisesRegex(RuntimeError, "exact preserved R78 binding"):
                run_r6a_execution._validate_r66_g0c_binding(
                    json.dumps(changed_review, sort_keys=True, separators=(",", ":")).encode(),
                    repo=root, plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)

    def test_r79_transaction_contracts_bind_disposition_and_original_evidence(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            contracts = {}
            for relative in sorted(run_r6a_execution.R79_TRANSACTION_CONTRACT_FILES):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"fixture:{relative}\n", encoding="utf-8")
                contracts[relative] = _sha(path.read_bytes())

            self.assertEqual(
                set(run_r6a_execution._validate_r79_transaction_contracts(
                    {"transaction_contracts": contracts}, repo=root)),
                run_r6a_execution.R79_TRANSACTION_CONTRACT_FILES)

            missing = dict(contracts)
            missing.pop(next(path for path in missing if "disposition.json" in path))
            with self.assertRaisesRegex(RuntimeError, "omits or adds an R77 failed-output"):
                run_r6a_execution._validate_r79_transaction_contracts(
                    {"transaction_contracts": missing}, repo=root)

            changed = next(path for path in contracts if "legacy_reconciliation.bin" in path)
            (root / changed).write_bytes(b"changed after review")
            with self.assertRaisesRegex(RuntimeError, "contract changed"):
                run_r6a_execution._validate_r79_transaction_contracts(
                    {"transaction_contracts": contracts}, repo=root)

    def test_r80_materialized_prefix_route_preserves_r78_and_r79_and_rejects_aliases(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            review_bytes, plan_path, plan_bytes, plan = self._binding_fixture(root, milestone="R80")
            result = run_r6a_execution._validate_r66_g0c_binding(
                review_bytes, repo=root, plan_path=plan_path, plan_bytes=plan_bytes,
                plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)
            self.assertEqual(result["candidate_id"],
                             "R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-1")
            self.assertEqual(result["dependency_manifest"]["path"],
                             run_r6a_execution.R80_DEPENDENCY_MANIFEST_PATH)
            self.assertEqual(plan_path.relative_to(root).as_posix(),
                             "experiments/storage/" + run_r6a_execution.R80_PLAN_NAME)

            wrong_path = plan_path.with_name(run_r6a_execution.R80_PLAN_NAME.replace(
                ".json", "-ALIAS.json"))
            wrong_path.write_bytes(plan_bytes)
            with self.assertRaisesRegex(RuntimeError,
                                        "R80 G0C binding requires its one canonical dual-lineage plan path"):
                run_r6a_execution._validate_r66_g0c_binding(
                    review_bytes, repo=root, plan_path=wrong_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)

            changed_review = json.loads(review_bytes)
            changed_review["r79_binding"] = {"fixture": "altered-preserved-r79"}
            with self.assertRaisesRegex(RuntimeError, "byte-preserved R78/R79 lineage"):
                run_r6a_execution._validate_r66_g0c_binding(
                    json.dumps(changed_review, sort_keys=True, separators=(",", ":")).encode(),
                    repo=root, plan_path=plan_path, plan_bytes=plan_bytes,
                    plan_canonical_sha256=_sha(_canonical(plan)), validated_plan=plan)

    def test_r80_changed_tests_bind_the_materialized_wineprefix_contract(self) -> None:
        self.assertIn(
            "experiments/storage/test_prefixlease_wineprefix_contract.py",
            run_r6a_execution.R80_CHANGED_TEST_FILES)

    def test_r80_transaction_contracts_bind_preserved_r79_and_materialization_contracts(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temp:
            root = Path(temp)
            contracts = {}
            for relative in sorted(run_r6a_execution.R80_TRANSACTION_CONTRACT_FILES):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(f"fixture:{relative}\n", encoding="utf-8")
                contracts[relative] = _sha(path.read_bytes())

            result = run_r6a_execution._validate_r80_transaction_contracts(
                {"transaction_contracts": contracts}, repo=root)
            self.assertEqual(set(result), run_r6a_execution.R80_TRANSACTION_CONTRACT_FILES)
            attempt_id = "step11d3-storage-policy-smoke-20261024-f7c2a643c96a41a7"
            self.assertEqual(run_r6a_execution.R80_R79_LIVE_ATTEMPT_ID, attempt_id)
            self.assertEqual(run_r6a_execution.R80_R79_LIVE_ATTEMPT_FILES, {
                f"experiments/storage/manifests/{attempt_id}.json",
                f"experiments/storage/smoke-claims/{attempt_id}.json",
                f"experiments/storage/smoke-runs/{attempt_id}/gate0-preflight-attestation.json",
                f"experiments/storage/smoke-runs/{attempt_id}/gate0-path-state.json.zlib",
                f"experiments/storage/smoke-runs/{attempt_id}/prefix-state.json",
                f"experiments/storage/smoke-runs/{attempt_id}/prefix-cleanup.json",
            })

            missing = dict(contracts)
            missing.pop(next(path for path in missing if path.endswith("/prefix-cleanup.json")))
            with self.assertRaisesRegex(RuntimeError, "omits or adds the post-materialization"):
                run_r6a_execution._validate_r80_transaction_contracts(
                    {"transaction_contracts": missing}, repo=root)

            changed = next(path for path in contracts if path.endswith("/gate0-preflight-attestation.json"))
            (root / changed).write_bytes(b"changed preserved R79 review")
            with self.assertRaisesRegex(RuntimeError, "R80 transaction contract changed"):
                run_r6a_execution._validate_r80_transaction_contracts(
                    {"transaction_contracts": contracts}, repo=root)

class R90ReviewEvidenceSchemaTests(unittest.TestCase):
    def test_projected_free_uses_the_lower_time_separated_free_sample(self) -> None:
        peak = 62 * build_retention.GIB
        strict = 57 * build_retention.GIB
        inventory_free = 86 * build_retention.GIB
        direct_free = inventory_free - 114_688

        self.assertEqual(
            run_r6a_execution._r90_projected_free_bytes(
                inventory_free_bytes=inventory_free,
                direct_free_bytes=direct_free,
                peak_bytes=peak,
                strict_scoped_bytes=strict),
            direct_free - (peak - strict))
        self.assertEqual(
            run_r6a_execution._r90_projected_free_bytes(
                inventory_free_bytes=direct_free,
                direct_free_bytes=inventory_free,
                peak_bytes=peak,
                strict_scoped_bytes=strict),
            direct_free - (peak - strict))
        with self.assertRaisesRegex(RuntimeError, "inputs are malformed"):
            run_r6a_execution._r90_projected_free_bytes(
                inventory_free_bytes=True,
                direct_free_bytes=direct_free,
                peak_bytes=peak,
                strict_scoped_bytes=strict)

    def test_required_evidence_rows_include_admission_disposition_tests(self) -> None:
        row_fields = run_r6a_execution._r90_review_evidence_row_fields(
            {"capacity_review": {"artifact_status": "PASS", "strict_scoped_bytes": 1}})

        self.assertEqual(set(row_fields),
                         set(run_r6a_execution.R90_REQUIRED_VALIDATION_EVIDENCE))
        self.assertEqual(row_fields["admission_disposition_tests"], {
            "path", "sha256", "size_bytes", "status"})

    def test_reviewer_evidence_validation_keeps_identity_policy_as_a_role_mapping(self) -> None:
        candidate_id = "R91-reviewer-evidence-test"
        candidate_core = "a" * 64
        expected_manifest = "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-21.json"
        reviewer_documents = {}
        for name, expected_checks in (
                ("correctness_safety", run_r6a_execution.R91_CORRECTNESS_SAFETY_CHECK_NAMES),
                ("provenance", run_r6a_execution.R91_PROVENANCE_CHECK_NAMES)):
            identity, scope = run_r6a_execution.R91_REVIEWER_IDENTITIES[name]
            document = {
                "status": "PASS", "run_id": "R91-reviewer-evidence-test-run",
                "candidate_id": candidate_id, "candidate_core_sha256": candidate_core,
                "plan_raw_sha256": "b" * 64, "plan_canonical_sha256": "c" * 64,
                "reviewer_role": "independent_reviewer", "reviewer_scope": scope,
                "reviewer_identity": identity,
                "reviewer_identity_sha256": _sha(identity.encode("utf-8")),
                "findings": [],
                "checks": [{"name": check, "status": "PASS", "details": "Reviewed."}
                           for check in sorted(expected_checks)],
            }
            if name == "provenance":
                document["dependency_manifest_path"] = expected_manifest
            reviewer_documents[name] = document

        run_r6a_execution._validate_r90_reviewer_evidence(
            reviewer_documents, candidate_id=candidate_id,
            candidate_core_sha256=candidate_core,
            expected_manifest_path=expected_manifest,
            correctness_checks=run_r6a_execution.R91_CORRECTNESS_SAFETY_CHECK_NAMES,
            provenance_checks=run_r6a_execution.R91_PROVENANCE_CHECK_NAMES,
            reviewer_identities=run_r6a_execution.R91_REVIEWER_IDENTITIES)

        duplicated_policy = {
            "correctness_safety": (
                "agent-shared", run_r6a_execution.R91_REVIEWER_IDENTITIES[
                    "correctness_safety"][1]),
            "provenance": (
                "agent-shared", run_r6a_execution.R91_REVIEWER_IDENTITIES["provenance"][1]),
        }
        duplicate_documents = {name: dict(document)
                               for name, document in reviewer_documents.items()}
        for name, document in duplicate_documents.items():
            identity = duplicated_policy[name][0]
            document["reviewer_identity"] = identity
            document["reviewer_identity_sha256"] = _sha(identity.encode("utf-8"))
        with self.assertRaisesRegex(RuntimeError, "reviewers must be distinct"):
            run_r6a_execution._validate_r90_reviewer_evidence(
                duplicate_documents, candidate_id=candidate_id,
                candidate_core_sha256=candidate_core,
                expected_manifest_path=expected_manifest,
                correctness_checks=run_r6a_execution.R91_CORRECTNESS_SAFETY_CHECK_NAMES,
                provenance_checks=run_r6a_execution.R91_PROVENANCE_CHECK_NAMES,
                reviewer_identities=duplicated_policy)

    def test_evidence_reader_accepts_admission_disposition_test_log_as_plain_text(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="fgmetal-r90-evidence-reader-", dir="/private/tmp") as temp:
            evidence_root = Path(temp)
            run_name = "R90-reader-regression"
            run_root = evidence_root / run_name
            run_root.mkdir()
            basenames = {
                "canonical_storage_suite": "canonical-storage-suite-reader.log",
                "current_pre_gate_fixture_scan": "pre-gate-current-fixture-reader.json",
                "fixture_scan_after_canonical_suite": "post-canonical-fixture-reader.json",
                "capacity_phase_snapshot": "capacity-phase-snapshot-reader.json",
                "evidence_index": "evidence-index-reader.json",
                "inventory": "storage-inventory.json",
                "strict_capacity_measurement": "strict-capacity-measurement-reader.json",
                "inventory_refresh_reconciliation": "inventory-refresh-reconciliation-reader.json",
                "correctness_safety": "correctness-safety-reader.json",
                "provenance": "provenance-reader.json",
                "pre_gate_mutation_ledger": "pre-gate-mutation-ledger-reader.json",
                "admission_disposition_tests": "admission-disposition-tests-reader.log",
            }
            rows: dict[str, dict[str, object]] = {}
            for name in run_r6a_execution.R90_REQUIRED_VALIDATION_EVIDENCE:
                relative = f"{run_name}/{basenames[name]}"
                raw = b"PASS\n12 tests\n" if name in {
                    "canonical_storage_suite", "admission_disposition_tests"} else b"{}\n"
                (evidence_root / relative).write_bytes(raw)
                rows[name] = {
                    "path": relative,
                    "size_bytes": len(raw),
                    "sha256": _sha(raw),
                }

            with mock.patch.object(run_r6a_execution, "R90_ALLOWED_EVIDENCE_ROOT", evidence_root):
                loaded = run_r6a_execution._read_r90_evidence_rows(rows)

        document, raw = loaded["admission_disposition_tests"]
        self.assertIsNone(document)
        self.assertEqual(raw, b"PASS\n12 tests\n")

    def test_r94_running_authority_evidence_is_a_checked_plain_text_log(self) -> None:
        with tempfile.TemporaryDirectory(
                prefix="fgmetal-r94-evidence-reader-", dir="/private/tmp") as temp:
            evidence_root = Path(temp)
            run_name = "R94-Candidate25-reader"
            relative = f"{run_name}/r94-running-authority-focused-tests-reader.log"
            raw = b"Ran 1 test in 0.01s\n\nOK\n"
            path = evidence_root / relative
            path.parent.mkdir()
            path.write_bytes(raw)
            rows = {"r94_running_authority_tests": {
                "path": relative, "size_bytes": len(raw), "sha256": _sha(raw),
                "status": "PASS"}}

            with mock.patch.object(run_r6a_execution, "R90_ALLOWED_EVIDENCE_ROOT", evidence_root):
                loaded = run_r6a_execution._read_r90_evidence_rows(
                    rows,
                    required_rows={"r94_running_authority_tests": "PASS"},
                    basenames=run_r6a_execution.R94_EVIDENCE_BASENAMES,
                    run_prefix="R94")

        document, observed = loaded["r94_running_authority_tests"]
        self.assertIsNone(document)
        self.assertEqual(observed, raw)
        self.assertTrue(observed.decode("utf-8").strip().endswith("OK"))


class R89G0CRoleSeparationTests(unittest.TestCase):
    candidate_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10"

    def _fixture(self, root: Path) -> dict[str, object]:
        storage = root / "experiments/storage"
        storage.mkdir(parents=True)
        plan = {
            "schema_version": 1,
            "build_id": "r6a-r87-dual-lineage-20261025t000000z-12345678",
            "admission_identity": {"admission_id": "r6a-r87-admission-20261025t000000z-12345678"},
            "build_graphs": [
                {"graph_id": "windows-product-r6a"},
                {"graph_id": "native-verification-r6a"},
            ],
            "build_configuration_key_sha256": "a" * 64,
        }
        plan["admission_identity"]["sha256"] = _sha(_canonical({
            "admission_id": plan["admission_identity"]["admission_id"]}))
        plan_bytes = _canonical(plan)
        plan_path = storage / run_r6a_execution.R87_CANDIDATE_8_PLAN_NAME
        plan_path.write_bytes(plan_bytes)
        graph_pair = ["windows-product-r6a", "native-verification-r6a"]
        plan_canonical_sha256 = _sha(_canonical(plan))
        expected_plan = {
            "path": f"experiments/storage/{plan_path.name}",
            "raw_sha256": _sha(plan_bytes),
            "canonical_sha256": plan_canonical_sha256,
            "graph_pair_key": plan["build_configuration_key_sha256"],
        }

        source_hashes, test_hashes = {}, {}
        review_source, review_tests = {}, {}
        for role, roster, digests, records in (
                ("source", run_r6a_execution.R87_CHANGED_SOURCE_FILES,
                 source_hashes, review_source),
                ("test", run_r6a_execution.R87_CHANGED_TEST_FILES,
                 test_hashes, review_tests)):
            for relative in sorted(roster):
                raw = f"r89 {role}:{relative}\n".encode()
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(raw)
                digests[relative] = _sha(raw)
                records[relative] = {"sha256": _sha(raw), "size_bytes": len(raw)}
        global_reviewed = {}
        for field, relative in storage_policy.R89_G0C_REVIEW_REQUIRED_FILES.items():
            path = storage / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if not path.exists():
                path.write_bytes(f"reviewed:{relative}\n".encode())
            raw = path.read_bytes()
            global_reviewed[field] = {
                "path": f"experiments/storage/{relative}",
                "sha256": _sha(raw),
                "size_bytes": len(raw),
            }
        global_review = json.dumps({
            "schema_version": 1, "status": "PASS", "findings": [],
            "review_scope": "Generic R89 launcher review",
            "reviewed_files": global_reviewed,
            "limitations": [],
        }, sort_keys=True, separators=(",", ":")).encode()
        (storage / Path(run_r6a_execution.R89_LAUNCHER_G0C_REVIEW_PATH).name).write_bytes(
            global_review)
        source_path = "experiments/storage/run_r6a_execution.py"
        test_path = "experiments/storage/test_run_r6a_execution.py"

        for relative, raw in (
                ("R89-candidate.diff", b"diff --git a/r89 b/r89\n"),
                ("R89-completion-contract.md", b"completion contract\n"),
                ("R89-manifest-commit-contract.md", b"manifest contract\n"),
                ("R89-crash-matrix.json", b"{}\n")):
            (root / relative).write_bytes(raw)
        diff_raw = (root / "R89-candidate.diff").read_bytes()
        completion_raw = (root / "R89-completion-contract.md").read_bytes()
        manifest_contract_raw = (root / "R89-manifest-commit-contract.md").read_bytes()
        crash_raw = (root / "R89-crash-matrix.json").read_bytes()
        manifest_path = run_r6a_execution.r6a_dependency_manifest.manifest_name_for_candidate(
            self.candidate_id)
        manifest_raw = b"{}\n"
        (root / manifest_path).write_bytes(manifest_raw)
        manifest_descriptor = {"path": manifest_path, "sha256": _sha(manifest_raw)}
        preserved_lineage = {
            "status": "PASS", "findings": [], "reviewed_files": {},
            "r78_binding": {"fixture": "r78"},
            "r79_binding": {"fixture": "r79"},
            "r80_binding": {"fixture": "r80"},
            "r81_binding": {"fixture": "r81"},
            "r86_binding": {"fixture": "r86"},
        }
        preserved_raw = _canonical(preserved_lineage)
        preserved_path = root / run_r6a_execution.R87_R86_G0C_PRESERVED_PATH
        preserved_path.parent.mkdir(parents=True, exist_ok=True)
        preserved_path.write_bytes(preserved_raw)
        candidate = {
            "schema_version": 1, "milestone": "R87", "candidate_id": self.candidate_id,
            "status": "FROZEN_FOR_ACCEPTANCE_VALIDATION",
            "execution_plan": expected_plan,
            "graph_pair_key": plan["build_configuration_key_sha256"],
            "changed_source_hashes": source_hashes,
            "changed_test_hashes": test_hashes,
            "exact_diff": {"path": "R89-candidate.diff", "sha256": _sha(diff_raw),
                           "size_bytes": len(diff_raw)},
            "preserved_r86_g0c_sha256": _sha(preserved_raw),
            "r80_binding": preserved_lineage["r80_binding"],
            "r81_binding": preserved_lineage["r81_binding"],
            "r86_binding": preserved_lineage["r86_binding"],
            "completion_contract": {
                "path": "R89-completion-contract.md", "sha256": _sha(completion_raw),
                "manifest_commit_contract_path": "R89-manifest-commit-contract.md",
                "manifest_commit_contract_sha256": _sha(manifest_contract_raw),
            },
            "crash_matrix": {"path": "R89-crash-matrix.json", "sha256": _sha(crash_raw)},
            "r87_binding": {
                "execution_dependency_manifest": manifest_descriptor,
            },
        }
        candidate_bytes = _canonical(candidate)
        candidate_path_name = f"{self.candidate_id}.json"
        (root / candidate_path_name).write_bytes(candidate_bytes)
        candidate_core = run_r6a_execution.r6a_dependency_manifest._candidate_core_sha256(
            candidate)
        strict_scoped_bytes = 40 * build_retention.GIB
        phase_rows = {}
        for phase_name, projected_growth_bytes in (
                run_r6a_execution.R89_PHASE_PROJECTED_GROWTH_BYTES.items()):
            baseline_scoped_bytes = strict_scoped_bytes + (
                run_r6a_execution.R89_PREFIX_DURABLE_OUTPUT_BYTES
                if phase_name == "B" else 0)
            raw_growth_bytes = run_r6a_execution.R89_PHASE_RAW_GROWTH_BYTES[phase_name]
            uncertainty_bytes = (raw_growth_bytes + 9) // 10
            accepted_bound_reconciliation_bytes = (
                run_r6a_execution.R89_PHASE_ACCEPTED_BOUND_RECONCILIATION_BYTES[phase_name])
            phase_rows[phase_name] = {
                "baseline_scoped_bytes": baseline_scoped_bytes,
                "raw_growth_bytes": raw_growth_bytes,
                "accepted_bound_reconciliation_bytes": accepted_bound_reconciliation_bytes,
                "uncertainty_bytes": uncertainty_bytes,
                "projected_growth_bytes": projected_growth_bytes,
                "projected_peak_bytes": baseline_scoped_bytes + projected_growth_bytes,
            }
        phase_projected_peak_bytes = max(
            phase["projected_peak_bytes"] for phase in phase_rows.values())
        projected_free_bytes = 70 * build_retention.GIB
        disk_free_bytes = projected_free_bytes + (
            phase_projected_peak_bytes - strict_scoped_bytes)
        evidence_root = root / "evidence-root"
        evidence_run = "R89-Candidate10-unit-fixture"
        evidence_dir = evidence_root / evidence_run
        evidence_dir.mkdir(parents=True)

        def put_evidence(name: str, basename: str, raw: bytes,
                         **claims: object) -> dict[str, object]:
            (evidence_dir / basename).write_bytes(raw)
            return {"path": f"{evidence_run}/{basename}", "sha256": _sha(raw),
                    "size_bytes": len(raw), **claims}

        inventory_relative = f"{evidence_run}/storage-inventory.json"
        inventory_doc = {
            "schema_version": 1,
            "filesystem": {"free_bytes": disk_free_bytes},
            "totals": {"fgmetal_scoped_allocated_inode_bytes": strict_scoped_bytes},
        }
        inventory_raw = _canonical(inventory_doc)
        inventory_sha256 = _sha(inventory_raw)
        report_plan_raw_sha256 = _sha(plan_bytes)
        evidence_binding = {
            "run_id": evidence_run,
            "candidate_id": self.candidate_id,
            "candidate_core_sha256": candidate_core,
            "plan_raw_sha256": report_plan_raw_sha256,
            "plan_canonical_sha256": plan_canonical_sha256,
        }
        strict_measurement_doc = {
            "status": "PASS", "strict_inventory_matches": True,
            "disk_free_bytes": disk_free_bytes,
            "strict_scoped_bytes": strict_scoped_bytes,
            "inventory_sha256": inventory_sha256,
            **evidence_binding,
        }
        strict_measurement_raw = _canonical(strict_measurement_doc)
        strict_measurement_sha256 = _sha(strict_measurement_raw)
        reconciliation_doc = {
            "status": "PASS", "difference_bytes": 0,
            "inventory_sha256": inventory_sha256,
            "strict_scoped_bytes": strict_scoped_bytes,
            **evidence_binding,
        }
        reconciliation_raw = _canonical(reconciliation_doc)
        reconciliation_sha256 = _sha(reconciliation_raw)
        capacity_review = {
            "artifact_status": "PASS",
            "capacity_ceiling_bytes": 60 * build_retention.GIB,
            "project_warning_bytes": 52 * build_retention.GIB,
            "projected_free_minimum_bytes": 50 * build_retention.GIB,
            "emergency_free_floor_bytes": 25 * build_retention.GIB,
            "cleanup_credit_bytes": 0, "shared_quota_credit_bytes": 0,
            "headroom_bytes": 60 * build_retention.GIB - phase_projected_peak_bytes,
            "inventory_path": inventory_relative,
            "inventory_refresh_reconciliation_sha256": reconciliation_sha256,
            "inventory_sha256": inventory_sha256,
            "peak_bytes": phase_projected_peak_bytes,
            "projected_free_bytes": projected_free_bytes,
            "strict_scoped_bytes": strict_scoped_bytes,
            "strict_inventory_matches": True,
            "strict_measurement_sha256": strict_measurement_sha256,
            "warning_active": False,
            "phase_aware": True,
            "p_b_overlap": False,
            "uncertainty_margin": {"basis": "incremental_phase_growth", "percent": 10},
            "phases": phase_rows,
        }
        capacity_document = {
            key: value for key, value in capacity_review.items()
            if key != "artifact_status"
        }
        capacity_document.update(status="PASS", **evidence_binding)
        capacity_raw = _canonical(capacity_document)
        suite_raw = b"Ran 1336 tests in 1.0s\n\nOK\n"
        fixture_scan_doc = {
            "status": "PASS_ZERO_OWNED_FIXTURES",
            "mounts": 0, "workspace_images": 0,
            "baseline_images": [], "baseline_mounts": [],
            "hdiutil_workspace_images": [], "private_tmp_fgmetal_mounts": [],
            **evidence_binding,
        }
        fixture_scan_raw = _canonical(fixture_scan_doc)
        correctness_doc = {
            "status": "PASS", "findings": [], **evidence_binding,
            "reviewer_role": "independent_reviewer",
            "reviewer_scope": run_r6a_execution.R89_REVIEWER_IDENTITIES[
                "correctness_safety"][1],
            "reviewer_identity": run_r6a_execution.R89_REVIEWER_IDENTITIES[
                "correctness_safety"][0],
            "reviewer_identity_sha256": _sha(
                run_r6a_execution.R89_REVIEWER_IDENTITIES[
                    "correctness_safety"][0].encode()),
            "checks": [{"name": name, "status": "PASS", "details": "Reviewed and passed."}
                       for name in sorted(run_r6a_execution.R89_CORRECTNESS_SAFETY_CHECK_NAMES)],
        }
        correctness_raw = _canonical(correctness_doc)
        provenance_doc = {
            "status": "PASS", "findings": [], **evidence_binding,
            "reviewer_role": "independent_reviewer",
            "reviewer_scope": run_r6a_execution.R89_REVIEWER_IDENTITIES[
                "provenance"][1],
            "reviewer_identity": run_r6a_execution.R89_REVIEWER_IDENTITIES[
                "provenance"][0],
            "reviewer_identity_sha256": _sha(
                run_r6a_execution.R89_REVIEWER_IDENTITIES["provenance"][0].encode()),
            "dependency_manifest_path": manifest_descriptor["path"],
            "checks": [{"name": name, "status": "PASS", "details": "Reviewed and passed."}
                       for name in sorted(run_r6a_execution.R89_PROVENANCE_CHECK_NAMES)],
        }
        provenance_raw = _canonical(provenance_doc)
        mutation_status = "PASS"
        ledger_doc = {
            "status": mutation_status, **evidence_binding,
            "events": [{"event": name, "status": "PASS", "details": "Reviewed and passed."}
                       for name in sorted(run_r6a_execution.R89_MUTATION_LEDGER_EVENTS)],
        }
        ledger_raw = _canonical(ledger_doc)
        reconciliation_doc.update(evidence_binding)
        reconciliation_raw = _canonical(reconciliation_doc)
        reconciliation_sha256 = _sha(reconciliation_raw)
        capacity_review["inventory_refresh_reconciliation_sha256"] = reconciliation_sha256
        capacity_document["inventory_refresh_reconciliation_sha256"] = reconciliation_sha256
        capacity_raw = _canonical(capacity_document)
        evidence_rows = {
            "canonical_storage_suite": put_evidence(
                "canonical_storage_suite", "canonical-storage-suite-r89.log", suite_raw,
                status="PASS",
                log_summary=["Ran 1336 tests in 1.0s", "OK"],
                summary="Ran 1336 tests in 1.0s; 0 failures; 0 errors; 0 skips; OK"),
            "current_pre_gate_fixture_scan": put_evidence(
                "current_pre_gate_fixture_scan", "pre-gate-current-fixture-scan-r89.json",
                fixture_scan_raw, status="PASS_ZERO_OWNED_FIXTURES", mounts=0,
                workspace_images=0),
            "fixture_scan_after_canonical_suite": put_evidence(
                "fixture_scan_after_canonical_suite", "post-canonical-fixture-scan-r89.json",
                fixture_scan_raw, status="PASS_ZERO_OWNED_FIXTURES", mounts=0,
                workspace_images=0),
            "capacity_phase_snapshot": put_evidence(
                "capacity_phase_snapshot", "candidate8-capacity-phase-snapshot-r89.json",
                capacity_raw, status="PASS", **{
                    key: value for key, value in capacity_document.items() if key != "status"}),
            "evidence_index": put_evidence(
                "evidence_index", "candidate8-evidence-index-r89.json", b"{}\n",
                status="PASS", rows=0),
            "inventory": put_evidence(
                "inventory", "storage-inventory.json", inventory_raw,
                strict_scoped_bytes=strict_scoped_bytes),
            "strict_capacity_measurement": put_evidence(
                "strict_capacity_measurement", "candidate8-strict-capacity-measurement-r89.json",
                strict_measurement_raw, status="PASS", disk_free_bytes=disk_free_bytes,
                strict_scoped_bytes=strict_scoped_bytes, inventory_sha256=inventory_sha256),
            "inventory_refresh_reconciliation": put_evidence(
                "inventory_refresh_reconciliation", "candidate8-inventory-reconciliation-r89.json",
                reconciliation_raw, status="PASS", difference_bytes=0),
            "correctness_safety": put_evidence(
                "correctness_safety", "candidate8-correctness-safety-r89.json", correctness_raw,
                status="PASS"),
            "provenance": put_evidence(
                "provenance", "candidate8-provenance-r89.json", provenance_raw, status="PASS"),
            "pre_gate_mutation_ledger": put_evidence(
                "pre_gate_mutation_ledger", "candidate8-pre-gate-mutation-ledger-r89.json",
                ledger_raw, status=mutation_status),
        }
        index_entries = [
            {"role": name, "path": row["path"], "sha256": row["sha256"],
             "size_bytes": row["size_bytes"]}
            for name, row in sorted(evidence_rows.items()) if name != "evidence_index"
        ]
        evidence_index_doc = {"status": "PASS", **evidence_binding,
                              "evidence": index_entries}
        evidence_index_raw = _canonical(evidence_index_doc)
        evidence_rows["evidence_index"] = put_evidence(
            "evidence_index", "candidate8-evidence-index-r89.json", evidence_index_raw,
            status="PASS", rows=len(index_entries))
        evidence_review = {
            "candidate_bound_evidence": evidence_rows,
            "evidence_boundary": "All R89 evidence is restricted to the current candidate evidence run.",
            "limitations": [],
            "run_id": evidence_run,
        }
        candidate_review = {
            "schema_version": 1, "milestone": "R87", "status": "PASS",
            "answer": "NO",
            "question": "Can durable SUCCESS exist without a validated committed manifest?",
            "findings": [], "candidate_id": self.candidate_id,
            "candidate_path": candidate_path_name,
            "candidate_core_sha256": candidate_core,
            "plan_path": f"experiments/storage/{plan_path.name}",
            "plan_raw_sha256": _sha(plan_bytes),
            "plan_canonical_sha256": plan_canonical_sha256,
            "graph_pair": graph_pair,
            "graph_pair_key": plan["build_configuration_key_sha256"],
            "exact_diff": candidate["exact_diff"],
            "reviewed_files": {"source": review_source, "tests": review_tests},
            "identity_review": {
                "changed_source_bindings": review_source,
                "changed_test_bindings": review_tests,
                "diff": {"matches_candidate_declaration": True,
                         "path": "R89-candidate.diff", "sha256": _sha(diff_raw),
                         "size_bytes": len(diff_raw)},
                "plan": {"raw_sha256": _sha(plan_bytes),
                         "canonical_sha256": plan_canonical_sha256},
                "manifest_route": {
                    "status": "PASS", "path": manifest_path,
                    "current_review_is_final_manifest_closure": True,
                    "rebuild_pending": False,
                },
            },
            "global_launcher_g0c": {
                "path": run_r6a_execution.R89_LAUNCHER_G0C_REVIEW_PATH,
                "sha256": _sha(global_review), "size_bytes": len(global_review),
                "status": "PASS", "findings": [],
                "reviewed_file_count": len(storage_policy.R89_G0C_REVIEW_REQUIRED_FILES),
                "gate_attestation_status": "PREFLIGHT_PASS",
                "gate_attestation_pinned_sha256": "b" * 64,
                "gate_pin_matches_current_global_review": False,
            },
            "r87_binding": {
                "candidate": {"path": candidate_path_name,
                              "candidate_id": self.candidate_id,
                              "candidate_core_sha256": candidate_core},
                "execution_dependency_manifest": manifest_descriptor,
            },
            "capacity_review": capacity_review,
            "behavior_review": {
                key: "PASS: independently reviewed and bound to R89"
                for key in run_r6a_execution.R89_BEHAVIOR_VERDICTS
            },
            "validation_evidence_review": evidence_review,
            "review_scope": "R89 exact current candidate review",
        }
        candidate_review_bytes = _canonical(candidate_review)
        candidate_review_path = storage / Path(
            run_r6a_execution.R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH).name
        candidate_review_path.write_bytes(candidate_review_bytes)
        gate = {
            "supplemental_evidence_sha256": {
                Path(run_r6a_execution.R89_LAUNCHER_G0C_REVIEW_PATH).name: _sha(global_review),
                candidate_review_path.name: _sha(candidate_review_bytes),
                manifest_path: _sha(manifest_raw),
            },
            "retention_control_sha256": {},
        }
        return {
            "root": root, "global_review": global_review,
            "candidate_review": candidate_review_bytes, "candidate": candidate,
            "candidate_review_path": candidate_review_path, "candidate_review_doc": candidate_review,
            "global_reviewed": global_reviewed, "gate": gate, "plan": plan,
            "plan_path": plan_path, "plan_bytes": plan_bytes,
            "plan_canonical_sha256": plan_canonical_sha256, "manifest_path": manifest_path,
            "manifest_raw": manifest_raw, "source_path": root / source_path,
            "candidate_path": root / candidate_path_name,
            "evidence_root": evidence_root,
        }

    def _validate_fixture(self, fixture: dict[str, object], **overrides: object) -> dict[str, object]:
        candidate_review = fixture["candidate_review_doc"]
        candidate = fixture["candidate"]
        manifest_descriptor = candidate_review["r87_binding"]["execution_dependency_manifest"]
        manifest_validation = {
            "sha256": manifest_descriptor["sha256"],
            "candidate_core_sha256": run_r6a_execution.r6a_dependency_manifest
                ._candidate_core_sha256(candidate),
            "g0c_sha256": run_r6a_execution.r6a_dependency_manifest
                ._g0c_core_sha256(candidate_review),
        }
        with mock.patch.object(
                run_r6a_execution.r6a_dependency_manifest, "validate_manifest",
                return_value=manifest_validation), \
                mock.patch.object(run_r6a_execution, "_validate_r87_transaction_contracts",
                                  return_value={}), \
                mock.patch.object(run_r6a_execution, "R89_ALLOWED_EVIDENCE_ROOT",
                                  fixture.get("evidence_root_override", fixture["evidence_root"])):
            return run_r6a_execution._validate_r89_g0c_reviews(
                global_review_bytes=overrides.get("global_review", fixture["global_review"]),
                candidate_review_bytes=overrides.get(
                    "candidate_review", fixture["candidate_review"]),
                gate_attestation=overrides.get("gate", fixture["gate"]),
                repo=fixture["root"], plan_path=fixture["plan_path"],
                plan_bytes=fixture["plan_bytes"],
                plan_canonical_sha256=fixture["plan_canonical_sha256"],
                validated_plan=fixture["plan"])

    def _reseal_candidate_review(self, fixture: dict[str, object]) -> None:
        candidate = fixture["candidate"]
        candidate_raw = _canonical(candidate)
        fixture["candidate_path"].write_bytes(candidate_raw)
        core = run_r6a_execution.r6a_dependency_manifest._candidate_core_sha256(candidate)
        report = fixture["candidate_review_doc"]
        report["candidate_core_sha256"] = core
        report["r87_binding"]["candidate"]["candidate_core_sha256"] = core
        fixture["candidate_review"] = _canonical(report)
        fixture["candidate_review_path"].write_bytes(fixture["candidate_review"])
        fixture["gate"]["supplemental_evidence_sha256"][
            fixture["candidate_review_path"].name] = _sha(fixture["candidate_review"])

    def _rewrite_evidence_doc(self, fixture: dict[str, object], name: str,
                              document: dict[str, object]) -> None:
        report = fixture["candidate_review_doc"]
        row = report["validation_evidence_review"]["candidate_bound_evidence"][name]
        path = fixture["evidence_root"] / row["path"]
        raw = _canonical(document)
        path.write_bytes(raw)
        row["sha256"], row["size_bytes"] = _sha(raw), len(raw)
        for field in ("status", "rows", "mounts", "workspace_images", "difference_bytes"):
            if field in document:
                row[field] = document[field]
        if name == "evidence_index":
            row["rows"] = len(document.get("evidence", []))
        else:
            index_row = report["validation_evidence_review"]["candidate_bound_evidence"][
                "evidence_index"]
            index_path = fixture["evidence_root"] / index_row["path"]
            index_document = json.loads(index_path.read_bytes())
            indexed_row = next(
                entry for entry in index_document["evidence"] if entry["role"] == name)
            indexed_row.update(path=row["path"], sha256=row["sha256"],
                               size_bytes=row["size_bytes"])
            index_raw = _canonical(index_document)
            index_path.write_bytes(index_raw)
            index_row.update(sha256=_sha(index_raw), size_bytes=len(index_raw))
        fixture["candidate_review"] = _canonical(report)
        fixture["candidate_review_path"].write_bytes(fixture["candidate_review"])
        fixture["gate"]["supplemental_evidence_sha256"][
            fixture["candidate_review_path"].name] = _sha(fixture["candidate_review"])

    def test_final_manifest_closure_builds_and_validates_without_candidate_raw_self_hash(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            candidate = fixture["candidate"]
            report = fixture["candidate_review_doc"]
            candidate_core = run_r6a_execution.r6a_dependency_manifest._candidate_core_sha256(
                candidate)
            g0c_core = run_r6a_execution.r6a_dependency_manifest._g0c_core_sha256(report)
            closure_raw = _canonical({
                "candidate_core_sha256": candidate_core,
                "g0c_core_sha256": g0c_core,
            })
            closure_sha256 = _sha(closure_raw)
            descriptor = {"path": Path(fixture["manifest_path"]).name,
                          "sha256": closure_sha256}
            candidate["r87_binding"]["execution_dependency_manifest"] = descriptor
            report["r87_binding"]["execution_dependency_manifest"] = descriptor
            self.assertEqual(
                run_r6a_execution.r6a_dependency_manifest._candidate_core_sha256(candidate),
                candidate_core)
            self.assertEqual(
                run_r6a_execution.r6a_dependency_manifest._g0c_core_sha256(report), g0c_core)
            fixture["candidate_path"].write_bytes(_canonical(candidate))
            fixture["candidate_review_doc"] = report
            fixture["candidate_review"] = _canonical(report)
            fixture["candidate_review_path"].write_bytes(fixture["candidate_review"])
            fixture["manifest_raw"] = closure_raw
            (fixture["root"] / fixture["manifest_path"]).write_bytes(closure_raw)
            fixture["gate"]["supplemental_evidence_sha256"][
                fixture["candidate_review_path"].name] = _sha(fixture["candidate_review"])
            fixture["gate"]["supplemental_evidence_sha256"][
                Path(fixture["manifest_path"]).name] = closure_sha256
            result = self._validate_fixture(fixture)
        self.assertEqual(result["candidate_id"], self.candidate_id)
        self.assertEqual(result["g0c_core_sha256"], g0c_core)
        self.assertNotIn("candidate_raw_sha256", fixture["candidate_review_doc"])
        route = fixture["candidate_review_doc"]["identity_review"]["manifest_route"]
        self.assertEqual(set(route), {
            "status", "path", "current_review_is_final_manifest_closure", "rebuild_pending"})

    def test_final_manifest_closure_rejects_missing_or_malformed_route_status(self) -> None:
        cases = (
            ("missing", lambda route: route.pop("status")),
            ("not pass", lambda route: route.update(status="PENDING")),
            ("cyclic digest field", lambda route: route.update(
                observed_manifest_sha256="0" * 64)),
        )
        for label, mutate in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
                fixture = self._fixture(Path(temporary))
                report = copy.deepcopy(fixture["candidate_review_doc"])
                mutate(report["identity_review"]["manifest_route"])
                raw = _canonical(report)
                gate = copy.deepcopy(fixture["gate"])
                gate["supplemental_evidence_sha256"][
                    fixture["candidate_review_path"].name] = _sha(raw)
                with self.assertRaisesRegex(RuntimeError, "final dependency-manifest closure"):
                    self._validate_fixture(fixture, candidate_review=raw, gate=gate)

    def test_missing_global_or_candidate_review_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            with self.assertRaisesRegex(RuntimeError, "global.*missing"):
                self._validate_fixture(fixture, global_review=None)
            with self.assertRaisesRegex(RuntimeError, "candidate-specific.*missing"):
                self._validate_fixture(fixture, candidate_review=None)

    def test_candidate_changed_source_and_test_maps_cannot_omit_r87_roster_entries(self) -> None:
        for field in ("changed_source_hashes", "changed_test_hashes"):
            with self.subTest(field=field), tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
                fixture = self._fixture(Path(temporary))
                fixture["candidate"][field].pop(sorted(fixture["candidate"][field])[0])
                self._reseal_candidate_review(fixture)
                with self.assertRaisesRegex(RuntimeError, "fixed R87 roster"):
                    self._validate_fixture(fixture)

    def test_r89_evidence_files_are_path_bound_rehashed_and_content_checked(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            suite_row = fixture["candidate_review_doc"][
                "validation_evidence_review"]["candidate_bound_evidence"][
                    "canonical_storage_suite"]
            suite_path = fixture["evidence_root"] / suite_row["path"]
            suite_path.write_bytes(b"Ran 1336 tests in 1.0s\nFAILED (failures=1)\n")
            with self.assertRaisesRegex(RuntimeError, "evidence file changed after review"):
                self._validate_fixture(fixture)

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            rows = fixture["candidate_review_doc"][
                "validation_evidence_review"]["candidate_bound_evidence"]
            rows["canonical_storage_suite"]["path"] = "../outside/canonical-storage-suite.log"
            raw = _canonical(fixture["candidate_review_doc"])
            gate = copy.deepcopy(fixture["gate"])
            gate["supplemental_evidence_sha256"][
                fixture["candidate_review_path"].name] = _sha(raw)
            with self.assertRaisesRegex(RuntimeError, "outside its role-bound evidence root"):
                self._validate_fixture(fixture, candidate_review=raw, gate=gate)

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            rows = fixture["candidate_review_doc"][
                "validation_evidence_review"]["candidate_bound_evidence"]
            row = rows["current_pre_gate_fixture_scan"]
            path = fixture["evidence_root"] / row["path"]
            scan_doc = json.loads(path.read_bytes())
            scan_doc.update({
                "status": "FAIL", "baseline_images": [], "baseline_mounts": [],
                "hdiutil_workspace_images": [], "private_tmp_fgmetal_mounts": [],
            })
            self._rewrite_evidence_doc(fixture, "current_pre_gate_fixture_scan", scan_doc)
            rows["current_pre_gate_fixture_scan"]["status"] = "PASS_ZERO_OWNED_FIXTURES"
            fixture["candidate_review"] = _canonical(fixture["candidate_review_doc"])
            fixture["candidate_review_path"].write_bytes(fixture["candidate_review"])
            fixture["gate"]["supplemental_evidence_sha256"][
                fixture["candidate_review_path"].name] = _sha(fixture["candidate_review"])
            raw = fixture["candidate_review"]
            gate = fixture["gate"]
            with self.assertRaisesRegex(RuntimeError, "fixture scan is not clean"):
                self._validate_fixture(fixture, candidate_review=raw, gate=gate)

    def test_fixture_scan_document_must_claim_zero_mounts_and_workspace_images(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            name = "current_pre_gate_fixture_scan"
            rows = fixture["candidate_review_doc"]["validation_evidence_review"][
                "candidate_bound_evidence"]
            scan_path = fixture["evidence_root"] / rows[name]["path"]
            scan = json.loads(scan_path.read_bytes())
            scan["mounts"] = 1
            self._rewrite_evidence_doc(fixture, name, scan)
            # Keep the reviewed row's zero claim so the validator has to inspect the
            # actual scan document independently of its row summary.
            rows[name]["mounts"] = 0
            fixture["candidate_review"] = _canonical(fixture["candidate_review_doc"])
            fixture["candidate_review_path"].write_bytes(fixture["candidate_review"])
            fixture["gate"]["supplemental_evidence_sha256"][
                fixture["candidate_review_path"].name] = _sha(fixture["candidate_review"])
            with self.assertRaisesRegex(RuntimeError, "fixture scan is not clean"):
                self._validate_fixture(fixture)

    def test_inventory_free_bytes_must_match_strict_capacity_measurement(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            report = fixture["candidate_review_doc"]
            rows = report["validation_evidence_review"]["candidate_bound_evidence"]
            inventory = json.loads((fixture["evidence_root"] / rows["inventory"]["path"])
                                   .read_bytes())
            inventory["filesystem"]["free_bytes"] += 1
            self._rewrite_evidence_doc(fixture, "inventory", inventory)
            new_inventory_sha = rows["inventory"]["sha256"]

            strict_path = fixture["evidence_root"] / rows[
                "strict_capacity_measurement"]["path"]
            strict_document = json.loads(strict_path.read_bytes())
            strict_document["inventory_sha256"] = new_inventory_sha
            self._rewrite_evidence_doc(fixture, "strict_capacity_measurement", strict_document)
            rows["strict_capacity_measurement"]["inventory_sha256"] = new_inventory_sha
            new_strict_sha = rows["strict_capacity_measurement"]["sha256"]

            reconciliation_path = fixture["evidence_root"] / rows[
                "inventory_refresh_reconciliation"]["path"]
            reconciliation_document = json.loads(reconciliation_path.read_bytes())
            reconciliation_document["inventory_sha256"] = new_inventory_sha
            self._rewrite_evidence_doc(
                fixture, "inventory_refresh_reconciliation", reconciliation_document)
            new_reconciliation_sha = rows["inventory_refresh_reconciliation"]["sha256"]

            capacity = report["capacity_review"]
            capacity.update(inventory_sha256=new_inventory_sha,
                            strict_measurement_sha256=new_strict_sha,
                            inventory_refresh_reconciliation_sha256=new_reconciliation_sha)
            capacity_path = fixture["evidence_root"] / rows["capacity_phase_snapshot"]["path"]
            capacity_document = json.loads(capacity_path.read_bytes())
            capacity_document.update(inventory_sha256=new_inventory_sha,
                                     strict_measurement_sha256=new_strict_sha,
                                     inventory_refresh_reconciliation_sha256=new_reconciliation_sha)
            self._rewrite_evidence_doc(fixture, "capacity_phase_snapshot", capacity_document)
            for key in ("inventory_sha256", "strict_measurement_sha256",
                        "inventory_refresh_reconciliation_sha256"):
                rows["capacity_phase_snapshot"][key] = capacity_document[key]
            self._rewrite_evidence_doc(fixture, "inventory", inventory)
            with self.assertRaisesRegex(RuntimeError, "inventory contents do not support"):
                self._validate_fixture(fixture)

    def test_independent_reviewers_have_fixed_distinct_identities_and_sha256(self) -> None:
        cases = (
            ("duplicate identity", "agent-g", None),
            ("identity digest mismatch", "agent-h", "0" * 64),
        )
        for label, identity, digest in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
                fixture = self._fixture(Path(temporary))
                row = fixture["candidate_review_doc"]["validation_evidence_review"][
                    "candidate_bound_evidence"]["provenance"]
                provenance = json.loads((fixture["evidence_root"] / row["path"]).read_bytes())
                provenance["reviewer_identity"] = identity
                provenance["reviewer_identity_sha256"] = digest or _sha(identity.encode())
                self._rewrite_evidence_doc(fixture, "provenance", provenance)
                with self.assertRaisesRegex(RuntimeError, "provenance evidence contents"):
                    self._validate_fixture(fixture)

    def test_correctness_and_provenance_check_rosters_are_complete(self) -> None:
        cases = (
            ("correctness_safety", "role_separation"),
            ("provenance", "external_logs"),
        )
        for role, missing_check in cases:
            with self.subTest(role=role), tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
                fixture = self._fixture(Path(temporary))
                row = fixture["candidate_review_doc"]["validation_evidence_review"][
                    "candidate_bound_evidence"][role]
                path = fixture["evidence_root"] / row["path"]
                review = json.loads(path.read_bytes())
                review["checks"] = [check for check in review["checks"]
                                    if check["name"] != missing_check]
                self._rewrite_evidence_doc(fixture, role, review)
                with self.assertRaisesRegex(RuntimeError, f"R89 {role} evidence lacks passing checks"):
                    self._validate_fixture(fixture)

    def test_reviewer_check_roster_rejects_duplicate_check_names(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            rows = fixture["candidate_review_doc"]["validation_evidence_review"][
                "candidate_bound_evidence"]
            path = fixture["evidence_root"] / rows["correctness_safety"]["path"]
            review = json.loads(path.read_bytes())
            review["checks"].append(copy.deepcopy(review["checks"][0]))
            self._rewrite_evidence_doc(fixture, "correctness_safety", review)
            with self.assertRaisesRegex(RuntimeError, "correctness_safety evidence lacks passing checks"):
                self._validate_fixture(fixture)

    def test_r89_evidence_changed_while_waiting_for_allocation_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            self._validate_fixture(fixture)
            review_path = fixture["candidate_review_path"]
            expected_sha256 = _sha(fixture["candidate_review"])
            suite_row = fixture["candidate_review_doc"]["validation_evidence_review"][
                "candidate_bound_evidence"]["canonical_storage_suite"]
            suite_path = fixture["evidence_root"] / suite_row["path"]

            def check_lock_time_evidence() -> None:
                run_r6a_execution._assert_r89_candidate_evidence_fresh_under_allocation_lock(
                    candidate_review_path=review_path, expected_sha256=expected_sha256)

            with mock.patch.object(run_r6a_execution, "R89_ALLOWED_EVIDENCE_ROOT",
                                   fixture["evidence_root"]):
                check_lock_time_evidence()

                class DelayedAllocationLock:
                    def __enter__(self):
                        suite_path.write_bytes(b"changed while waiting for allocation lock\n")
                        return self

                    def __exit__(self, *_args):
                        return False

                with self.assertRaisesRegex(RuntimeError, "evidence file changed after review"):
                    with DelayedAllocationLock():
                        check_lock_time_evidence()

    def test_r89_evidence_no_follow_rejects_symlink_artifact(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            rows = fixture["candidate_review_doc"][
                "validation_evidence_review"]["candidate_bound_evidence"]
            row = rows["canonical_storage_suite"]
            path = fixture["evidence_root"] / row["path"]
            run_id = fixture["candidate_review_doc"]["validation_evidence_review"]["run_id"]
            target = fixture["evidence_root"] / run_id / "safe-target.log"
            target.write_bytes(path.read_bytes())
            path.unlink()
            path.symlink_to(target)
            with self.assertRaisesRegex(RuntimeError, "unavailable or unsafe"):
                self._validate_fixture(fixture)

    def test_r89_evidence_root_and_run_directory_symlinks_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            symlink_root = fixture["root"] / "evidence-root-alias"
            symlink_root.symlink_to(fixture["evidence_root"], target_is_directory=True)
            fixture["evidence_root_override"] = symlink_root
            with self.assertRaisesRegex(RuntimeError, "unavailable or unsafe"):
                self._validate_fixture(fixture)

        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            run_id = fixture["candidate_review_doc"]["validation_evidence_review"]["run_id"]
            run_path = fixture["evidence_root"] / run_id
            real_run = fixture["evidence_root"] / "R89-Candidate10-real-run"
            run_path.rename(real_run)
            run_path.symlink_to(real_run, target_is_directory=True)
            with self.assertRaisesRegex(RuntimeError, "unavailable or unsafe"):
                self._validate_fixture(fixture)

    def test_r89_evidence_index_rejects_omitted_extra_and_duplicate_rows(self) -> None:
        for mutation, message in (
                ("omit", "malformed"), ("extra", "malformed"),
                ("duplicate", "malformed")):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory(
                    dir="/private/tmp") as temporary:
                fixture = self._fixture(Path(temporary))
                report = fixture["candidate_review_doc"]
                row = report["validation_evidence_review"]["candidate_bound_evidence"][
                    "evidence_index"]
                index_path = fixture["evidence_root"] / row["path"]
                document = json.loads(index_path.read_bytes())
                if mutation == "omit":
                    document["evidence"].pop()
                elif mutation == "extra":
                    document["evidence"].append({
                        "role": "extra", "path": "R89-Candidate10-unit-fixture/extra.json",
                        "sha256": "8" * 64, "size_bytes": 1,
                    })
                else:
                    document["evidence"].append(dict(document["evidence"][0]))
                self._rewrite_evidence_doc(fixture, "evidence_index", document)
                with self.assertRaisesRegex(RuntimeError, message):
                    self._validate_fixture(fixture)

    def test_r89_correctness_provenance_and_ledger_semantics_fail_closed(self) -> None:
        cases = (
            ("reviewer role", "correctness_safety", "reviewer_role", "evidence contents"),
            ("review check", "correctness_safety", "checks", "lacks passing checks"),
            ("provenance plan", "provenance", "plan_canonical_sha256", "not bound"),
            ("ledger pending", "pre_gate_mutation_ledger", "status", "mutation ledger contents"),
        )
        for label, name, field, expected in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
                fixture = self._fixture(Path(temporary))
                report = fixture["candidate_review_doc"]
                row = report["validation_evidence_review"]["candidate_bound_evidence"][name]
                document = json.loads((fixture["evidence_root"] / row["path"]).read_bytes())
                if label == "reviewer role":
                    document[field] = "candidate_author"
                elif label == "review check":
                    document[field][0]["name"] = "unapproved_check"
                elif label == "provenance plan":
                    document[field] = "0" * 64
                else:
                    document[field] = "PENDING"
                self._rewrite_evidence_doc(fixture, name, document)
                if label == "ledger pending":
                    report["validation_evidence_review"]["candidate_bound_evidence"][
                        name]["status"] = "PASS"
                    fixture["candidate_review"] = _canonical(report)
                    fixture["candidate_review_path"].write_bytes(fixture["candidate_review"])
                    fixture["gate"]["supplemental_evidence_sha256"][
                        fixture["candidate_review_path"].name] = _sha(fixture["candidate_review"])
                with self.assertRaisesRegex(RuntimeError, expected):
                    self._validate_fixture(fixture)

    def test_r89_gate_or_review_replaced_while_waiting_for_allocation_lock_fails_closed(self) -> None:
        for stale_target in ("gate", "candidate review"):
            with self.subTest(stale_target=stale_target), tempfile.TemporaryDirectory(
                    dir="/private/tmp") as temporary:
                root = Path(temporary)
                global_path = root / "experiments/storage/g0c-launcher-independent-review-R89-CANDIDATE-10.json"
                candidate_path = root / "experiments/storage/R6-A-R89-FRESH-G0C-REVIEW-CANDIDATE-10.json"
                manifest_path = root / "R6-A-R87-EXECUTION-DEPENDENCY-MANIFEST-CANDIDATE-10.json"
                for path in (global_path, candidate_path, manifest_path):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(f"original:{path.name}\n".encode())
                pin_paths = {
                    global_path.name: (global_path, global_path.relative_to(root).as_posix()),
                    candidate_path.name: (candidate_path,
                                          candidate_path.relative_to(root).as_posix()),
                    manifest_path.name: (manifest_path, manifest_path.relative_to(root).as_posix()),
                }
                pins = {name: _sha(path.read_bytes()) for name, (path, _relative) in pin_paths.items()}
                gate_path = root / "storage-gate0-attestation.json"
                gate_raw = _canonical({"supplemental_evidence_sha256": pins})
                gate_path.write_bytes(gate_raw)
                freshness_files = {
                    name: (path, relative, pins[name])
                    for name, (path, relative) in pin_paths.items()
                }

                with mock.patch.object(run_r6a_execution, "REPO", root), \
                        mock.patch.object(storage_policy, "GATE0_ATTESTATION_PATH", gate_path):
                    run_r6a_execution._assert_r89_gate_fresh_under_allocation_lock(
                        expected_gate_sha256=_sha(gate_raw),
                        supplemental_files=freshness_files)

                    class DelayedAllocationLock:
                        def __enter__(self):
                            if stale_target == "gate":
                                gate_path.write_bytes(gate_raw + b" ")
                            else:
                                candidate_path.write_bytes(b"replaced after initial review\n")
                            return self

                        def __exit__(self, *_args):
                            return False

                    with self.assertRaisesRegex(RuntimeError, "changed while the runner waited"
                                                 if stale_target == "gate"
                                                 else "evidence changed before admission"):
                        with DelayedAllocationLock():
                            run_r6a_execution._assert_r89_gate_fresh_under_allocation_lock(
                                expected_gate_sha256=_sha(gate_raw),
                                supplemental_files=freshness_files)

    def test_r89_plan_replaced_while_waiting_for_allocation_lock_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            root = Path(temporary)
            plan_path = root / "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-6.json"
            plan = {
                "build_id": "r6a-r87-dual-lineage-20261025t000000z-12345678",
                "source_identity": {"sha256": "a" * 64},
                "reservation_bytes": 1,
            }
            original = _canonical(plan)
            plan_path.write_bytes(original)
            expected_raw_sha = _sha(original)
            expected_canonical_sha = _sha(build_retention._canonical_json(plan))
            args = {
                "plan_path": plan_path,
                "expected_raw_sha256": expected_raw_sha,
                "expected_canonical_sha256": expected_canonical_sha,
                "build_id": plan["build_id"], "build_path": root / "build",
                "expected_bytes": 1,
            }
            with mock.patch.object(build_retention, "_session_validate_plan",
                                   side_effect=lambda value, **_kwargs: value), \
                    mock.patch.object(run_r6a_execution, "_validate_r66_recovery_plan"):
                run_r6a_execution._assert_r89_plan_fresh_under_allocation_lock(**args)

                class DelayedAllocationLock:
                    def __enter__(self):
                        plan_path.write_bytes(original + b" ")
                        return self

                    def __exit__(self, *_args):
                        return False

                with self.assertRaisesRegex(RuntimeError, "raw digest changed"):
                    with DelayedAllocationLock():
                        run_r6a_execution._assert_r89_plan_fresh_under_allocation_lock(**args)

    def test_empty_or_incomplete_r89_capacity_and_review_evidence_fails_closed(self) -> None:
        cases = (
            ("capacity absent", lambda report: report.update(capacity_review=None),
             "capacity review is missing"),
            ("capacity empty", lambda report: report.update(capacity_review={}),
             "complete phase-aware schema"),
            ("capacity threshold changed", lambda report: report[
                "capacity_review"].update(project_warning_bytes=53 * build_retention.GIB),
             "accepted threshold or safety invariant"),
            ("capacity ceiling changed", lambda report: report[
                "capacity_review"].update(capacity_ceiling_bytes=61 * build_retention.GIB),
             "accepted threshold or safety invariant"),
            ("projected free minimum changed", lambda report: report[
                "capacity_review"].update(projected_free_minimum_bytes=49 * build_retention.GIB),
             "accepted threshold or safety invariant"),
            ("emergency floor changed", lambda report: report[
                "capacity_review"].update(emergency_free_floor_bytes=24 * build_retention.GIB),
             "accepted threshold or safety invariant"),
            ("capacity P/B overlap", lambda report: report[
                "capacity_review"].update(p_b_overlap=True),
             "accepted threshold or safety invariant"),
            ("uncertainty applied to absolute baseline", lambda report: report[
                "capacity_review"]["phases"]["P"].update(
                    uncertainty_bytes=report["capacity_review"]["phases"]["P"][
                        "uncertainty_bytes"] + report["capacity_review"]["strict_scoped_bytes"]),
             "unsafe P phase summary"),
            ("B must use the post-PrefixLease baseline", lambda report: report[
                "capacity_review"]["phases"]["B"].update(
                    baseline_scoped_bytes=report["capacity_review"]["strict_scoped_bytes"]),
             "unsafe B phase summary"),
            ("B must retain the accepted R88 conservative-bound reconciliation", lambda report: report[
                "capacity_review"]["phases"]["B"].update(
                    accepted_bound_reconciliation_bytes=0),
             "unsafe B phase summary"),
            ("phase raw growth must match the R87 formula", lambda report: report[
                "capacity_review"]["phases"]["B"].update(
                    raw_growth_bytes=report["capacity_review"]["phases"]["B"][
                        "raw_growth_bytes"] + 1),
             "unsafe B phase summary"),
            ("projected free not supported by strict measurement", lambda report: (
                report["validation_evidence_review"]["candidate_bound_evidence"][
                    "strict_capacity_measurement"].update(disk_free_bytes=(
                        report["validation_evidence_review"]["candidate_bound_evidence"][
                            "strict_capacity_measurement"]["disk_free_bytes"] + 1))),
             "strict capacity measurement does not support"),
            ("capacity peak arithmetic mismatch", lambda report: report[
                "capacity_review"].update(peak_bytes=report["capacity_review"][
                    "peak_bytes"] + 1), "phase arithmetic or strict-scope verdict"),
            ("measured baseline plus accepted phase growth exceeds ceiling",
             lambda report: (
                 report["capacity_review"].update(strict_scoped_bytes=60 * build_retention.GIB),
                 report["capacity_review"]["phases"]["P"].update(
                     baseline_scoped_bytes=60 * build_retention.GIB,
                     projected_peak_bytes=60 * build_retention.GIB
                         + report["capacity_review"]["phases"]["P"]["projected_growth_bytes"]),
                 report["capacity_review"]["phases"]["B"].update(
                     baseline_scoped_bytes=60 * build_retention.GIB
                         + run_r6a_execution.R89_PREFIX_DURABLE_OUTPUT_BYTES,
                     projected_peak_bytes=60 * build_retention.GIB
                         + run_r6a_execution.R89_PREFIX_DURABLE_OUTPUT_BYTES
                         + report["capacity_review"]["phases"]["B"]["projected_growth_bytes"]),
                 report["capacity_review"].update(
                     peak_bytes=max(phase["projected_peak_bytes"] for phase in
                                    report["capacity_review"]["phases"].values()),
                     headroom_bytes=60 * build_retention.GIB - max(
                         phase["projected_peak_bytes"] for phase in
                         report["capacity_review"]["phases"].values()))),
             "phase arithmetic or strict-scope verdict"),
            ("behavior review empty", lambda report: report.update(behavior_review={}),
             "behavior review lacks required"),
            ("validation evidence empty", lambda report: report.update(
                validation_evidence_review={}), "validation evidence review is empty"),
            ("canonical suite omitted", lambda report: report[
                "validation_evidence_review"]["candidate_bound_evidence"].pop(
                    "canonical_storage_suite"), "omits required"),
            ("provenance evidence omitted", lambda report: report[
                "validation_evidence_review"]["candidate_bound_evidence"].pop(
                    "provenance"), "omits required"),
            ("fixture evidence empty", lambda report: report[
                "validation_evidence_review"]["candidate_bound_evidence"].update(
                    current_pre_gate_fixture_scan={}), "required validation evidence row"),
        )
        for label, mutate, error in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
                fixture = self._fixture(Path(temporary))
                report = copy.deepcopy(fixture["candidate_review_doc"])
                mutate(report)
                raw = _canonical(report)
                gate = copy.deepcopy(fixture["gate"])
                gate["supplemental_evidence_sha256"][
                    fixture["candidate_review_path"].name] = _sha(raw)
                with self.assertRaisesRegex(RuntimeError, error):
                    self._validate_fixture(fixture, candidate_review=raw, gate=gate)

    def test_wrong_candidate_core_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            report = dict(fixture["candidate_review_doc"])
            report["candidate_core_sha256"] = "0" * 64
            raw = _canonical(report)
            gate = dict(fixture["gate"])
            gate["supplemental_evidence_sha256"] = dict(gate["supplemental_evidence_sha256"])
            gate["supplemental_evidence_sha256"][
                fixture["candidate_review_path"].name] = _sha(raw)
            with self.assertRaisesRegex(RuntimeError, "exact candidate core"):
                self._validate_fixture(fixture, candidate_review=raw, gate=gate)

    def test_wrong_plan_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            report = dict(fixture["candidate_review_doc"])
            report["plan_raw_sha256"] = "0" * 64
            raw = _canonical(report)
            gate = dict(fixture["gate"])
            gate["supplemental_evidence_sha256"] = dict(gate["supplemental_evidence_sha256"])
            gate["supplemental_evidence_sha256"][fixture["candidate_review_path"].name] = _sha(raw)
            with self.assertRaisesRegex(RuntimeError, "exact plan bytes"):
                self._validate_fixture(fixture, candidate_review=raw, gate=gate)

    def test_wrong_dependency_manifest_or_gate_digest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            report = copy.deepcopy(fixture["candidate_review_doc"])
            report["r87_binding"]["execution_dependency_manifest"]["sha256"] = "0" * 64
            raw = _canonical(report)
            gate = copy.deepcopy(fixture["gate"])
            gate["supplemental_evidence_sha256"][fixture["candidate_review_path"].name] = _sha(raw)
            with self.assertRaisesRegex(RuntimeError, "exact dependency manifest"):
                self._validate_fixture(fixture, candidate_review=raw, gate=gate)
            with self.assertRaisesRegex(RuntimeError, "candidate-specific G0C review"):
                self._validate_fixture(fixture, gate={
                    **fixture["gate"],
                    "supplemental_evidence_sha256": {
                        **fixture["gate"]["supplemental_evidence_sha256"],
                        fixture["candidate_review_path"].name: "0" * 64,
                    },
                })

    def test_stale_candidate_and_stale_global_reviews_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            fixture["source_path"].write_bytes(b"changed after review\n")
            with self.assertRaisesRegex(RuntimeError, "global launcher G0C review failed"):
                self._validate_fixture(fixture)
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            global_review = fixture["global_review"] + b" "
            with self.assertRaisesRegex(RuntimeError, "global launcher G0C review"):
                self._validate_fixture(fixture, global_review=global_review)

    def test_mixed_historical_and_current_g0c_roles_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
            fixture = self._fixture(Path(temporary))
            global_review = json.loads(fixture["global_review"])
            global_review["r87_binding"] = {}
            with self.assertRaisesRegex(RuntimeError, "contains a candidate binding"):
                self._validate_fixture(fixture, global_review=_canonical(global_review))
            candidate_review = dict(fixture["candidate_review_doc"])
            candidate_review["r86_binding"] = {}
            raw = _canonical(candidate_review)
            gate = copy.deepcopy(fixture["gate"])
            gate["supplemental_evidence_sha256"][fixture["candidate_review_path"].name] = _sha(raw)
            with self.assertRaisesRegex(RuntimeError, "unexpected schema"):
                self._validate_fixture(fixture, candidate_review=raw, gate=gate)

    def test_global_launcher_enforces_generic_top_level_schema(self) -> None:
        identity_fields = {
            "candidate_id", "candidate_path", "candidate_core_sha256", "candidate_sha256",
            "candidate_review", "plan_path", "plan_raw_sha256", "plan_sha256",
            "plan_digest", "graph_pair_key", "graph_id", "graph_sha256",
            "exact_diff", "diff", "diff_sha256", "manifest_path", "manifest_sha256",
        }
        for field in sorted(identity_fields):
            with self.subTest(field=field), tempfile.TemporaryDirectory(dir="/private/tmp") as temporary:
                fixture = self._fixture(Path(temporary))
                global_review = json.loads(fixture["global_review"])
                global_review[field] = {"identity": "candidate-bound"}
                with self.assertRaisesRegex(RuntimeError, "exact generic top-level schema"):
                    self._validate_fixture(fixture, global_review=_canonical(global_review))


if __name__ == "__main__":
    unittest.main()
