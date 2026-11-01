#!/usr/bin/env python3
"""R66 Same-Process Dual-Lineage R6-A Execution Controller.

Executes R6-A dual build graphs and native verification within a single
controller process on a single BuildLease object:
- Validates fresh signed Gate 0 attestation and live capacity.
- Creates and signs the fresh R6-A admission manifest.
- Validates live capacity before reservation.
- Reserves exactly ONE BuildLease object.
- Immediately calls start_session() on the SAME returned BuildLease object.
- Executes Windows product graph (configure, build, install, stage PE).
- Executes native verification graph (configure, build Mach-O, build UNIT, build MODEL, stage runtime).
- Executes native tests: PRODUCTION_PATH, UNIT, MODEL.
- Executes immutable source verification.
- Finalizes session: writer quiescence, unmount, detach, detached verification,
  read-only reattach, read-only verification, output inventory and source-to-binary receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import time
from collections.abc import Mapping
sys.dont_write_bytecode = True
from pathlib import Path

# Repository root setup
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "experiments/storage"))

import build_retention
import storage_policy
import r6a_dependency_manifest

R65_DUAL_GRAPH_CONTRACT = "R6-A-R65-DUAL-GRAPH-FAIL-CLOSED-CONTRACT.md"
R65_GOVERNANCE_FILES = {
    "r65_dual_graph_identity_contract": "R6-A-R65-DUAL-GRAPH-IDENTITY-CONTRACT.md",
    "r65_orchestrator_reconciliation": "R6-A-R65-ORCHESTRATOR-RECONCILIATION.md",
    "r65_recovery_orchestrator_reconciliation": "R6-A-R65-RECOVERY-ORCHESTRATOR-RECONCILIATION.md",
    "r65_recovery_session_plan_reconciliation": "R6-A-R65-RECOVERY-SESSION-PLAN-RECONCILIATION.md",
    "r65_audit_session_plan_binding_trace": "R6-A-R65-RECOVERY-SESSION-PLAN-BINDING-TRACE.md",
    "r65_audit_session_plan_guard_review": "R6-A-R65-RECOVERY-SESSION-PLAN-GUARD-REVIEW.md",
    "r65_audit_fixture_fidelity": "R6-A-R65-RECOVERY-FIXTURE-FIDELITY.md",
    "r65_audit_runner_reservation_review": "R6-A-R65-RECOVERY-RUNNER-RESERVATION-REVIEW.md",
    "r65_gemini_disposition": "R6-A-R65-GEMINI-FINALIZATION-DISPOSITION.md",
    "r65_capacity_reconfirmation": "R6-A-R65-PREFIXLEASE-CAPACITY-RECONFIRMATION.md",
    "r65_evidence_loss_impact": "R6-A-R65-R64-EVIDENCE-LOSS-CURRENT-IMPACT.md",
    "r64_preserved_baseline": "R6-A-R65-R64-EVIDENCE-LOSS-AND-TAKEOVER-BASELINE.json",
    "r64_formal_disposition": "R64-GEMINI-ATTEMPT-DISPOSITION-R65.json",
    "r65_planner_stop_report": "R6-A-R65-RECOVERY-PLANNER-STOP-REPORT.md",
    "r65_planner_handoff": "R6-A-R65-PLANNER-HANDOFF-20261023.md",
    "r65_focused_harness_failure_evidence": (
        "R6-A-R65-RECOVERY-FOCUSED-HARNESS-FAILURE-EVIDENCE.json"),
    "r65_capture_interface_contract": "R6-A-R65-RECOVERY-CAPTURE-INTERFACE-CONTRACT.md",
    "r65_harness_contract": "R6-A-R65-RECOVERY-HARNESS-CONTRACT.md",
    "r65_preedit_manifest": (
        "R6-A-R65-RECOVERY-PREEDIT-20261023T123127Z/pre-edit-manifest.json"),
    "r65_preedit_implementation_diff": (
        "R6-A-R65-RECOVERY-PREEDIT-20261023T123127Z/R65-implementation.diff"),
    "r65_recovery_development_iterations": "R6-A-R65-RECOVERY-DEVELOPMENT-ITERATIONS.md",
    "r65_candidate4_preflight_builder": "experiments/storage/r65_prebuild_identity.py",
    "r65_candidate4_preflight_result": (
        "R6-A-R65-CANDIDATE-4-PREFLIGHT-RESULT-20261023T154500Z.json"),
    "r65_candidate4_preflight_manifest": (
        f"experiments/storage/build-manifests/{build_retention.BUILD_SESSION_PREBUILD_ARTIFACT_PREFIX}-20261023T154500Z/prebuild-manifest.json"),
    "r65_candidate4_stability_preflight_result": (
        "R6-A-R65-CANDIDATE-4-STABILITY-PREFLIGHT-RESULT-20261023T154800Z.json"),
    "r65_candidate4_stability_preflight_manifest": (
        f"experiments/storage/build-manifests/{build_retention.BUILD_SESSION_PREBUILD_ARTIFACT_PREFIX}-20261023T154800Z/prebuild-manifest.json"),
}
R65_CANDIDATE_PATH_PATTERN = re.compile(r"R6-A-R65-RECOVERY-CANDIDATE-[1-9][0-9]*\.json\Z")
R65_CANDIDATE_FILE_KEY = "r65_recovery_candidate"
R65_CANDIDATE_DIFF_FILE_KEY = "r65_recovery_candidate_diff"
R65_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py": "build_retention_control",
    "experiments/storage/test_build_retention.py": "build_retention_tests",
    "experiments/storage/run_r6a_execution.py": "r65_execution_runner",
    "experiments/storage/storage_policy.py": "source",
    "experiments/storage/r65_prebuild_identity.py": "r65_candidate4_preflight_builder",
}
R65_ADMISSION_CLAIM_DIRECTORY = storage_policy.R65_ADMISSION_CLAIM_DIRECTORY
R66_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R66-ATOMIC-COMPLETION-CANDIDATE-[1-3]\.json\Z")
R69_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R69-ADMISSION-BOUNDARY-CANDIDATE-1\.json\Z")
R70_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R70-ADMISSION-RESERVATION-CANDIDATE-[1-3]\.json\Z")
R71_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R71-PROVENANCE-CAPACITY-CANDIDATE-[1-3]\.json\Z")
R72_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R72-CAPACITY-RECALIBRATION-CANDIDATE-1\.json\Z")
R73_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R73-60GIB-POLICY-MIGRATION-CANDIDATE-1\.json\Z")
R74_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R74-HISTORICAL-OUTPUT-ACCOUNTING-CANDIDATE-1\.json\Z")
R75_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R75-G0C-EVIDENCE-SCHEMA-CANDIDATE-1\.json\Z")
R76_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R76-RUNNER-ROUTE-CANDIDATE-1\.json\Z")
R77_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R77-GATE-DRIFT-DIAGNOSTIC-CANDIDATE-1\.json\Z")
R78_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R78-BOUNDED-GATE-TRANSITION-CANDIDATE-1\.json\Z")
R79_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R79-FAILED-OUTPUT-DISPOSITION-CANDIDATE-1\.json\Z")
R80_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CANDIDATE-1\.json\Z")
R81_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R81-SMOKE-MANIFEST-PUBLICATION-CANDIDATE-1\.json\Z")
R86_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R86-CANONICAL-REGRESSION-REPAIR-CANDIDATE-1\.json\Z")
R87_CANDIDATE_PATH_PATTERN = re.compile(
    r"R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-(?:[2-9]|1[0-9]|2[0-7])\.json\Z")
R66_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py",
    "experiments/storage/run_r6a_execution.py",
}
R69_CHANGED_SOURCE_FILES = {
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_policy.py",
    "experiments/storage/storage_inventory.py",
}
R66_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_run_r6a_execution.py",
}
R69_CHANGED_TEST_FILES = {
    "experiments/storage/test_g0c_checks.py",
    "experiments/storage/test_run_r6a_execution.py",
}
R70_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_gate0.py",
    "experiments/storage/storage_policy.py",
}
R70_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_g0c_checks.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
}
R71_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py",
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_gate0.py",
    "experiments/storage/storage_inventory.py",
    "experiments/storage/storage_policy.py",
}
R71_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_g0c_checks.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
}
R72_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py",
    "experiments/storage/make_cleanup_plan.py",
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_policy.py",
}
R72_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
    "experiments/storage/test_storage_recovery.py",
}
R73_CHANGED_SOURCE_FILES = {
    "experiments/storage/HOST-V3-SHARED-CAPACITY-POLICY.md",
    "experiments/storage/build_retention.py",
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_policy.py",
}
R73_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_g0c_checks.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
    "experiments/storage/test_vm_capacity_domain.py",
}
R74_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py",
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_policy.py",
}
R74_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
}
R75_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py",
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_gate0.py",
    "experiments/storage/storage_policy.py",
}
R75_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_g0c_checks.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
}
R76_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py",
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_policy.py",
}
R76_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
}
R77_CHANGED_SOURCE_FILES = {
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_gate0.py",
    "experiments/storage/storage_policy.py",
}
R77_CHANGED_TEST_FILES = {
    "experiments/storage/test_g0c_checks.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
}
R78_CHANGED_SOURCE_FILES = {
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/smoke_prefix_lease.py",
    "experiments/storage/storage_gate0.py",
    "experiments/storage/storage_policy.py",
}
R78_CHANGED_TEST_FILES = {
    "experiments/storage/test_g0c_checks.py",
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
    "experiments/storage/test_storage_recovery.py",
}
R79_CHANGED_SOURCE_FILES = {
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_policy.py",
}
R80_CHANGED_SOURCE_FILES = {
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/smoke_prefix_lease.py",
    "experiments/storage/storage_policy.py",
}
R79_CHANGED_TEST_FILES = {
    "experiments/storage/test_failed_output_disposition.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
}
R80_CHANGED_TEST_FILES = {
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_prefixlease_wineprefix_contract.py",
    "experiments/storage/test_storage_policy.py",
}
R81_CHANGED_SOURCE_FILES = {
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/smoke_prefix_lease.py",
    "experiments/storage/storage_policy.py",
}
R81_CHANGED_TEST_FILES = {
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_recovery.py",
    "experiments/storage/test_storage_policy.py",
}
R86_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py",
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
}
R86_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_buildlease_v3_host_adversarial.py",
    "experiments/storage/test_buildlease_v3_fixture_cleanup_contract.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
}
R87_CHANGED_SOURCE_FILES = R86_CHANGED_SOURCE_FILES | {
    "experiments/storage/smoke_prefix_lease.py",
    "experiments/storage/storage_policy.py",
    "experiments/storage/storage_gate0.py",
}
R87_CHANGED_TEST_FILES = R86_CHANGED_TEST_FILES | {
    "experiments/storage/test_storage_policy.py",
    "experiments/storage/test_g0c_checks.py",
    "experiments/storage/test_failed_output_disposition.py",
}
R71_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.MANIFEST_NAME
R72_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R72_MANIFEST_NAME
R73_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R73_MANIFEST_NAME
R74_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R74_MANIFEST_NAME
R75_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R75_MANIFEST_NAME
R76_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R76_MANIFEST_NAME
R77_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R77_MANIFEST_NAME
R78_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R78_MANIFEST_NAME
R79_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R79_MANIFEST_NAME
R80_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R80_MANIFEST_NAME
R81_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R81_MANIFEST_NAME
R86_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R86_MANIFEST_NAME
R87_DEPENDENCY_MANIFEST_PATH = r6a_dependency_manifest.R87_MANIFEST_NAME
R78_PLAN_NAME = "R6-A-R78-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json"
R79_PLAN_NAME = "R6-A-R79-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json"
R80_PLAN_NAME = "R6-A-R80-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json"
R81_PLAN_NAME = "R6-A-R81-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json"
R86_PLAN_NAME = "R6-A-R86-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-1.json"
R87_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-2.json"
R87_CANDIDATE_3_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-3.json"
R87_CANDIDATE_4_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-4.json"
R87_CANDIDATE_5_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-5.json"
R87_CANDIDATE_6_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-6.json"
R87_CANDIDATE_7_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-7.json"
R87_CANDIDATE_8_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-8.json"
R90_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-15.json"
R91_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-17.json"
R92_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-20.json"
R93_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-21.json"
R94_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-23.json"
R95_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-24.json"
R96_PLAN_NAME = "R6-A-R87-DUAL-LINEAGE-EXECUTION-PLAN-CANDIDATE-25.json"
R90_CANDIDATE_13_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-13"
R90_CANDIDATE_14_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-14"
R90_CANDIDATE_15_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-15"
R90_CANDIDATE_16_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-16"
R90_CANDIDATE_17_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-17"
R90_CANDIDATE_18_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-18"
R90_CANDIDATE_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-19"
R91_HISTORICAL_CANDIDATE_20_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-20"
R91_CANDIDATE_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-21"
R92_CANDIDATE_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-23"
R93_CANDIDATE_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-24"
R94_CANDIDATE_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-25"
R95_CANDIDATE_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-26"
R96_CANDIDATE_ID = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-27"
R90_ADMISSION_OUTPUT_ROOT = "experiments/storage/r6a-admission-outputs"
R90_ADMISSION_OUTPUT_CONTRACT = "R90_ADMISSION_UNIQUE_SIBLING_ROOT_V1"
R87_FRESH_G0C_REVIEW_PATH = "experiments/storage/R6-A-R87-FRESH-G0C-REVIEW.json"
R87_CANDIDATE_5_FRESH_G0C_REVIEW_PATH = "experiments/storage/R6-A-R88-FRESH-G0C-REVIEW.json"
R89_CANDIDATE_8_FRESH_G0C_REVIEW_PATH = "experiments/storage/R6-A-R89-FRESH-G0C-REVIEW.json"
R89_CANDIDATE_9_FRESH_G0C_REVIEW_PATH = "experiments/storage/R6-A-R89-FRESH-G0C-REVIEW-CANDIDATE-9.json"
R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH = "experiments/storage/R6-A-R89-FRESH-G0C-REVIEW-CANDIDATE-10.json"
R89_LAUNCHER_G0C_REVIEW_PATH = "experiments/storage/g0c-launcher-independent-review-R89-CANDIDATE-10.json"
R90_CANDIDATE_11_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-1.json")
R90_CANDIDATE_13_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-3.json")
R90_CANDIDATE_14_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-4.json")
R90_CANDIDATE_15_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-5.json")
R90_CANDIDATE_16_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-6.json")
R90_CANDIDATE_17_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-7.json")
R90_CANDIDATE_18_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-9.json")
R90_CANDIDATE_19_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R90-FRESH-G0C-REVIEW-CANDIDATE-10.json")
R91_CANDIDATE_20_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R91-FRESH-G0C-REVIEW-CANDIDATE-20.json")
R91_CANDIDATE_21_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R91-FRESH-G0C-REVIEW-CANDIDATE-21.json")
R92_CANDIDATE_23_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R92-FRESH-G0C-REVIEW-CANDIDATE-23.json")
R93_CANDIDATE_24_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R93-FRESH-G0C-REVIEW-CANDIDATE-24.json")
R94_CANDIDATE_25_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R94-FRESH-G0C-REVIEW-CANDIDATE-25.json")
R95_CANDIDATE_26_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R95-FRESH-G0C-REVIEW-CANDIDATE-26.json")
R96_CANDIDATE_27_FRESH_G0C_REVIEW_PATH = (
    "experiments/storage/R6-A-R96-FRESH-G0C-REVIEW-CANDIDATE-27.json")
R90_LAUNCHER_G0C_REVIEW_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R90-CANDIDATE-10.json")
R91_LAUNCHER_G0C_REVIEW_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R91-CANDIDATE-21.json")
R92_LAUNCHER_G0C_REVIEW_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R92-CANDIDATE-23.json")
R93_LAUNCHER_G0C_REVIEW_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R93-CANDIDATE-24.json")
R94_LAUNCHER_G0C_REVIEW_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R94-CANDIDATE-25.json")
R95_LAUNCHER_G0C_REVIEW_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R95-CANDIDATE-26.json")
R96_LAUNCHER_G0C_REVIEW_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R96-CANDIDATE-27.json")
R92_HANDOFF_CONTRACT_PATH = (
    "experiments/storage/R6-A-R92-R71-HANDOFF-CONTRACT-CANDIDATE-23.json")
R93_HANDOFF_CONTRACT_PATH = (
    "experiments/storage/R6-A-R93-R71-HANDOFF-CONTRACT-CANDIDATE-24.json")
R94_HANDOFF_CONTRACT_PATH = (
    "experiments/storage/R6-A-R94-R71-HANDOFF-CONTRACT-CANDIDATE-25.json")
R95_HANDOFF_CONTRACT_PATH = (
    "experiments/storage/R6-A-R95-R71-HANDOFF-CONTRACT-CANDIDATE-26.json")
R96_HANDOFF_CONTRACT_PATH = (
    "experiments/storage/R6-A-R96-R71-HANDOFF-CONTRACT-CANDIDATE-27.json")
R91_WORKSPACE_TRANSITION_CONTRACT_PATH = (
    "experiments/storage/R6-A-R91-WORKSPACE-TRANSITION-CONTRACT-CANDIDATE-20.json")
R91_FAILURE_RECOVERY_CONTRACT_PATH = (
    "experiments/storage/R6-A-R91-SESSION-FAILURE-RECOVERY-CONTRACT-CANDIDATE-20.json")
R90_DISPOSITION_CONTRACT_PATH = (
    "experiments/storage/R6-A-R90-ADMISSION-DISPOSITION-CONTRACT.md")
R90_OUTPUT_ROOT_CONTRACT_PATH = (
    "experiments/storage/R6-A-R90-UNIQUE-OUTPUT-ROOT-CONTRACT.md")
R90_HISTORICAL_DISPOSITION_PATHS = {
    "experiments/storage/admission-dispositions/"
    "r6a-r87-admission-20261025T174249Z-r89-3d8d7a30.json",
    "experiments/storage/admission-dispositions/"
    "r6a-r87-admission-20261025T1957Z-r89-c9-7a4e.json",
}
R90_HISTORICAL_ATTEMPT_EVIDENCE_PATHS = {
    "experiments/storage/manifests/"
    "r6a-r87-admission-20261025t221237z-r90-c11-33863859.json",
    "experiments/storage/build-cleanup-receipts/orphaned-unstarted/"
    "r6a-r87-dual-lineage-20261025t221237z-r90-c11-33863859.json",
}
R90_CHANGED_SOURCE_FILES = {
    "experiments/storage/build_retention.py",
    "experiments/storage/r6a_dependency_manifest.py",
    "experiments/storage/run_r6a_execution.py",
    "experiments/storage/storage_gate0.py",
    "experiments/storage/storage_policy.py",
}
R90_CHANGED_TEST_FILES = {
    "experiments/storage/test_build_retention.py",
    "experiments/storage/test_buildlease_v3_fixture_cleanup_contract.py",
    "experiments/storage/test_buildlease_v3_host_adversarial.py",
    "experiments/storage/test_failed_output_disposition.py",
    "experiments/storage/test_g0c_checks.py",
    "experiments/storage/test_r6a_admission_disposition.py",
    "experiments/storage/test_r6a_dependency_manifest.py",
    "experiments/storage/test_run_r6a_execution.py",
    "experiments/storage/test_storage_policy.py",
}
R91_CHANGED_SOURCE_FILES = set(R90_CHANGED_SOURCE_FILES)
R91_CHANGED_TEST_FILES = set(R90_CHANGED_TEST_FILES)
R93_CHANGED_SOURCE_FILES = set(R91_CHANGED_SOURCE_FILES)
R93_CHANGED_TEST_FILES = set(R91_CHANGED_TEST_FILES)
R94_CHANGED_SOURCE_FILES = set(R93_CHANGED_SOURCE_FILES)
R94_CHANGED_TEST_FILES = set(R93_CHANGED_TEST_FILES)
R95_CHANGED_SOURCE_FILES = set(R94_CHANGED_SOURCE_FILES)
R95_CHANGED_TEST_FILES = set(R94_CHANGED_TEST_FILES)
R96_CHANGED_SOURCE_FILES = set(R95_CHANGED_SOURCE_FILES)
R96_CHANGED_TEST_FILES = set(R95_CHANGED_TEST_FILES)
R90_BEHAVIOR_VERDICTS = {
    "admission_disposition_eligibility", "historical_manifest_immutability",
    "unique_output_roots", "strict_overlap_rules", "state_b", "state_c",
    "allow_refuse_fail_closed", "capacity_safety", "runner_route", "gate_binding",
}
R91_BEHAVIOR_VERDICTS = R90_BEHAVIOR_VERDICTS | {
    "workspace_transition_exception", "unrelated_transition_fail_closed",
    "failure_cleanup_order", "failure_evidence_retirement",
}

R89_CANDIDATE_G0C_FIELDS = {
    "answer", "behavior_review", "candidate_core_sha256", "candidate_id",
    "candidate_path", "capacity_review", "exact_diff",
    "findings", "global_launcher_g0c", "graph_pair", "graph_pair_key",
    "identity_review", "milestone", "plan_canonical_sha256", "plan_path",
    "plan_raw_sha256", "question", "r87_binding", "review_scope",
    "reviewed_files", "schema_version", "status", "validation_evidence_review",
}
R89_GLOBAL_G0C_FIELDS = {
    "schema_version", "status", "findings", "review_scope", "reviewed_files",
    "limitations",
}

R89_BEHAVIOR_VERDICTS = {
    "role_separation", "gate_binding", "runner_route", "fixture_cleanup",
    "state_b", "state_c", "allow_refuse", "capacity_safety",
}
R89_CAPACITY_THRESHOLD_BYTES = {
    "project_warning_bytes": 52 * build_retention.GIB,
    "capacity_ceiling_bytes": 60 * build_retention.GIB,
    "projected_free_minimum_bytes": 50 * build_retention.GIB,
    "emergency_free_floor_bytes": 25 * build_retention.GIB,
}
R90_CAPACITY_THRESHOLD_BYTES = dict(R89_CAPACITY_THRESHOLD_BYTES)
# R87 models P against S0 and B against S1 = S0 + D. P includes the retained
# 32 MiB output allowance. B is B+Q plus the existing 10% margin; preserve the
# prior R88 accepted ceiling bound with its explicit 114,688-byte reconciliation
# uplift rather than folding that difference into the R87 operational formula.
R89_PREFIX_DURABLE_OUTPUT_BYTES = 32 * 1024 * 1024
R89_PHASE_RAW_GROWTH_BYTES = {
    "P": 3 * build_retention.GIB + R89_PREFIX_DURABLE_OUTPUT_BYTES,
    "B": (7 * build_retention.GIB // 2) + build_retention.GIB,
}
R89_PHASE_ACCEPTED_BOUND_RECONCILIATION_BYTES = {
    "P": 0,
    "B": 114_688,
}
R89_PHASE_PROJECTED_GROWTH_BYTES = {
    "P": 3_580_257_895,
    # Phase B growth is measured from S1 = S0 + D and retains the small,
    # documented conservative R88 bound reconciliation.
    "B": 5_315_136_717,
}
R90_PREFIX_DURABLE_OUTPUT_BYTES = R89_PREFIX_DURABLE_OUTPUT_BYTES
R90_PHASE_RAW_GROWTH_BYTES = dict(R89_PHASE_RAW_GROWTH_BYTES)
R90_PHASE_ACCEPTED_BOUND_RECONCILIATION_BYTES = dict(
    R89_PHASE_ACCEPTED_BOUND_RECONCILIATION_BYTES)
R90_PHASE_PROJECTED_GROWTH_BYTES = dict(R89_PHASE_PROJECTED_GROWTH_BYTES)
R89_REQUIRED_VALIDATION_EVIDENCE = {
    "canonical_storage_suite": "PASS",
    "current_pre_gate_fixture_scan": "PASS_ZERO_OWNED_FIXTURES",
    "fixture_scan_after_canonical_suite": "PASS_ZERO_OWNED_FIXTURES",
    "capacity_phase_snapshot": "PASS",
    "evidence_index": "PASS",
    "inventory": None,
    "strict_capacity_measurement": "PASS",
    "inventory_refresh_reconciliation": "PASS",
    "correctness_safety": "PASS",
    "provenance": "PASS",
    "pre_gate_mutation_ledger": "PASS",
}
R89_CORRECTNESS_SAFETY_CHECK_NAMES = {
    "role_separation", "gate_binding", "runner_validation", "fixture_lifecycle",
    "completion_commit_order", "state_b_nonce_replay", "state_c_prefix_identity",
    "allow_refuse_fail_closed", "capacity_thresholds", "phase_nonoverlap",
}
R89_PROVENANCE_CHECK_NAMES = {
    "candidate_core", "plan_raw_and_canonical", "exact_diff",
    "changed_source_roster", "changed_test_roster", "global_launcher_review",
    "candidate_review", "canonical_suite", "immediate_fixture_scans",
    "capacity_snapshot", "strict_capacity_measurement", "inventory",
    "inventory_reconciliation", "correctness_safety_review", "external_logs",
    "pre_gate_mutation_ledger", "dependency_manifest_closure",
}
R89_REVIEWER_IDENTITIES = {
    "correctness_safety": ("agent-g", "R89 Candidate 10 independent correctness and safety review"),
    "provenance": ("agent-h", "R89 Candidate 10 independent provenance review"),
}
R90_REQUIRED_VALIDATION_EVIDENCE = {
    **R89_REQUIRED_VALIDATION_EVIDENCE,
    "admission_disposition_tests": "PASS",
}
R91_REQUIRED_VALIDATION_EVIDENCE = {
    **R90_REQUIRED_VALIDATION_EVIDENCE,
    "workspace_transition_tests": "PASS",
    "failure_recovery_tests": "PASS",
}
R92_REQUIRED_VALIDATION_EVIDENCE = {
    **R91_REQUIRED_VALIDATION_EVIDENCE,
    "r92_handoff_tests": "PASS",
}
R93_REQUIRED_VALIDATION_EVIDENCE = {
    **R91_REQUIRED_VALIDATION_EVIDENCE,
    "r93_handoff_tests": "PASS",
}
R94_REQUIRED_VALIDATION_EVIDENCE = {
    **R91_REQUIRED_VALIDATION_EVIDENCE,
    "r94_running_authority_tests": "PASS",
}
R95_REQUIRED_VALIDATION_EVIDENCE = {
    **R94_REQUIRED_VALIDATION_EVIDENCE,
    "r95_successor_focused_tests": "PASS",
}
R96_REQUIRED_VALIDATION_EVIDENCE = {
    **R95_REQUIRED_VALIDATION_EVIDENCE,
    "r96_post_exit_watcher_tests": "PASS",
}
R90_CORRECTNESS_SAFETY_CHECK_NAMES = {
    "admission_disposition_eligibility", "historical_manifest_immutability",
    "unique_output_roots", "strict_overlap_rules", "state_b", "state_c",
    "allow_refuse_fail_closed", "capacity_safety", "runner_route", "gate_binding",
}
R90_PROVENANCE_CHECK_NAMES = {
    "candidate_core", "plan_raw_and_canonical", "exact_diff",
    "changed_source_roster", "changed_test_roster", "global_launcher_review",
    "candidate_review", "canonical_suite", "immediate_fixture_scans",
    "capacity_snapshot", "strict_capacity_measurement", "inventory",
    "inventory_reconciliation", "correctness_safety_review", "historical_dispositions",
    "external_logs", "pre_gate_mutation_ledger", "dependency_manifest_closure",
}
R90_REVIEWER_IDENTITIES = {
    "correctness_safety": (
        "agent-g", "R90 Candidate19 independent correctness and safety review"),
    "provenance": ("agent-h", "R90 Candidate19 independent provenance review"),
}
R90_MUTATION_LEDGER_EVENTS = {
    "review_freeze", "canonical_suite", "fixture_scans", "capacity_inventory",
    "correctness_safety", "provenance", "historical_dispositions",
    "pre_gate_mutation",
}
R91_CORRECTNESS_SAFETY_CHECK_NAMES = R90_CORRECTNESS_SAFETY_CHECK_NAMES | {
    "exact_active_buildlease_capacity_exception", "unrelated_mount_fail_closed",
    "failure_detach_before_liveness", "original_failure_and_cleanup_evidence",
}
R91_PROVENANCE_CHECK_NAMES = R90_PROVENANCE_CHECK_NAMES | {
    "workspace_transition_contract", "failure_recovery_contract",
    "focused_capacity_and_recovery_tests",
}
R91_REVIEWER_IDENTITIES = {
    "correctness_safety": (
        "agent-g", "R91 Candidate21 independent correctness and safety review"),
    "provenance": ("agent-h", "R91 Candidate21 independent provenance review"),
}
R92_BEHAVIOR_VERDICTS = R91_BEHAVIOR_VERDICTS | {
    "r71_consumption_workspace_identity", "pregraph_exception_capture",
}
R92_CORRECTNESS_SAFETY_CHECK_NAMES = R91_CORRECTNESS_SAFETY_CHECK_NAMES
R92_PROVENANCE_CHECK_NAMES = R91_PROVENANCE_CHECK_NAMES | {
    "r92_plan_candidate_manifest_bindings", "r92_canonical_and_leak_scan_evidence",
}
R93_BEHAVIOR_VERDICTS = set(R92_BEHAVIOR_VERDICTS)
R93_CORRECTNESS_SAFETY_CHECK_NAMES = set(R92_CORRECTNESS_SAFETY_CHECK_NAMES)
R93_PROVENANCE_CHECK_NAMES = set(R92_PROVENANCE_CHECK_NAMES)
R94_BEHAVIOR_VERDICTS = R93_BEHAVIOR_VERDICTS | {"running_process_session_command_binding"}
R95_BEHAVIOR_VERDICTS = R94_BEHAVIOR_VERDICTS | {
    "successor_plan_route", "fresh_buildlease_identity", "pre_dispatch_authority",
}
R96_BEHAVIOR_VERDICTS = R95_BEHAVIOR_VERDICTS | {
    "verified_child_exit_before_post_exit_capacity",
}
R94_CORRECTNESS_SAFETY_CHECK_NAMES = R93_CORRECTNESS_SAFETY_CHECK_NAMES | {
    "running_command_requires_live_process_session", "exact_step_receipt_and_mount_authority",
}
R94_PROVENANCE_CHECK_NAMES = R93_PROVENANCE_CHECK_NAMES | {
    "candidate25_plan23_manifest_binding", "canonical_suite_and_fixture_scan",
}
R95_CORRECTNESS_SAFETY_CHECK_NAMES = R94_CORRECTNESS_SAFETY_CHECK_NAMES | {
    "fresh_buildlease_identity", "successor_route_and_pre_dispatch_authority",
}
R96_CORRECTNESS_SAFETY_CHECK_NAMES = R95_CORRECTNESS_SAFETY_CHECK_NAMES | {
    "exact_popen_exit_status_before_capacity",
}
R95_PROVENANCE_CHECK_NAMES = R94_PROVENANCE_CHECK_NAMES | {
    "candidate26_plan24_manifest_binding", "successor_focused_regressions",
}
R96_PROVENANCE_CHECK_NAMES = R95_PROVENANCE_CHECK_NAMES | {
    "candidate27_plan25_manifest_binding", "r96_post_exit_watcher_tests",
}
R92_REVIEWER_IDENTITIES = {
    "correctness_safety": (
        "agent-r92-correctness", "R92 Candidate23 independent correctness and safety review"),
    "provenance": (
        "agent-r92-provenance", "R92 Candidate23 independent provenance review"),
}
R93_REVIEWER_IDENTITIES = {
    "correctness_safety": (
        "agent-r93-correctness", "R93 Candidate24 independent correctness and safety review"),
    "provenance": (
        "agent-r93-provenance", "R93 Candidate24 independent provenance review"),
}
R94_REVIEWER_IDENTITIES = {
    "correctness_safety": (
        "codex-subagent-track_a_review",
        "R94 Candidate25 independent correctness and safety review"),
    "provenance": (
        "codex-subagent-packet_route_audit",
        "R94 Candidate25 independent provenance review"),
}
R95_REVIEWER_IDENTITIES = {
    "correctness_safety": (
        "codex-subagent-r95-correctness",
        "R95 Candidate26 independent correctness and safety review"),
    "provenance": (
        "codex-subagent-r95-provenance",
        "R95 Candidate26 independent provenance review"),
}
R96_REVIEWER_IDENTITIES = {
    "correctness_safety": (
        "codex-primary-r96-correctness",
        "R96 Candidate27 post-exit correctness and safety review"),
    "provenance": (
        "codex-primary-r96-provenance",
        "R96 Candidate27 current-byte provenance review"),
}
R90_ALLOWED_EVIDENCE_ROOT = Path.home() / "Library/Logs/FG-Metal"
R90_EVIDENCE_MAX_BYTES = 32 * 1024 * 1024
R90_EVIDENCE_BASENAMES = {
    "canonical_storage_suite": re.compile(r"canonical-storage-suite[^/]*\.log\Z"),
    "current_pre_gate_fixture_scan": re.compile(r"pre-gate[^/]*fixture[^/]*\.json\Z"),
    "fixture_scan_after_canonical_suite": re.compile(r"(?:post|after)[^/]*fixture[^/]*\.json\Z"),
    "capacity_phase_snapshot": re.compile(r"[^/]*capacity[^/]*snapshot[^/]*\.json\Z"),
    "evidence_index": re.compile(r"[^/]*evidence-index[^/]*\.json\Z"),
    "inventory": re.compile(r"storage-inventory\.json\Z"),
    "strict_capacity_measurement": re.compile(r"[^/]*strict[^/]*capacity[^/]*measurement[^/]*\.json\Z"),
    "inventory_refresh_reconciliation": re.compile(r"[^/]*inventory[^/]*reconciliation[^/]*\.json\Z"),
    "correctness_safety": re.compile(r"[^/]*correctness[^/]*safety[^/]*\.json\Z"),
    "provenance": re.compile(r"[^/]*provenance[^/]*\.json\Z"),
    "pre_gate_mutation_ledger": re.compile(r"[^/]*mutation[^/]*ledger[^/]*\.json\Z"),
    "admission_disposition_tests": re.compile(r"[^/]*admission[^/]*disposition[^/]*tests[^/]*\.log\Z"),
}
R91_EVIDENCE_BASENAMES = {
    **R90_EVIDENCE_BASENAMES,
    "workspace_transition_tests": re.compile(
        r"workspace-transition-focused-tests[^/]*\.log\Z"),
    "failure_recovery_tests": re.compile(
        r"failure-recovery-focused-tests[^/]*\.log\Z"),
}
R92_EVIDENCE_BASENAMES = dict(R91_EVIDENCE_BASENAMES)
R92_EVIDENCE_BASENAMES["r92_handoff_tests"] = re.compile(
    r"r92-handoff-focused-tests[^/]*\.log\Z")
R93_EVIDENCE_BASENAMES = dict(R92_EVIDENCE_BASENAMES)
R93_EVIDENCE_BASENAMES.pop("r92_handoff_tests")
R93_EVIDENCE_BASENAMES["r93_handoff_tests"] = re.compile(
    r"r93-handoff-focused-tests[^/]*\.log\Z")
R94_EVIDENCE_BASENAMES = dict(R91_EVIDENCE_BASENAMES)
R94_EVIDENCE_BASENAMES["r94_running_authority_tests"] = re.compile(
    r"r94-running-authority-focused-tests[^/]*\.log\Z")
R95_EVIDENCE_BASENAMES = dict(R94_EVIDENCE_BASENAMES)
R95_EVIDENCE_BASENAMES["r95_successor_focused_tests"] = re.compile(
    r"r95-successor-focused-tests[^/]*\.log\Z")
R96_EVIDENCE_BASENAMES = dict(R95_EVIDENCE_BASENAMES)
R96_EVIDENCE_BASENAMES["r96_post_exit_watcher_tests"] = re.compile(
    r"r96-watcher-focused-tests[^/]*\.log\Z")
R90_CANDIDATE_G0C_FIELDS = set(R89_CANDIDATE_G0C_FIELDS)
R90_GLOBAL_G0C_FIELDS = set(R89_GLOBAL_G0C_FIELDS)
R91_CANDIDATE_G0C_FIELDS = set(R90_CANDIDATE_G0C_FIELDS)
R92_CANDIDATE_G0C_FIELDS = set(R90_CANDIDATE_G0C_FIELDS)
R91_GLOBAL_G0C_FIELDS = set(R90_GLOBAL_G0C_FIELDS)
R89_MUTATION_LEDGER_EVENTS = {
    "review_freeze", "canonical_suite", "fixture_scans", "capacity_inventory",
    "correctness_safety", "provenance", "pre_gate_mutation",
}
R89_ALLOWED_EVIDENCE_ROOT = Path.home() / "Library/Logs/FG-Metal"
R89_EVIDENCE_MAX_BYTES = 32 * 1024 * 1024
R89_EVIDENCE_BASENAMES = {
    "canonical_storage_suite": re.compile(r"canonical-storage-suite[^/]*\.log\Z"),
    "current_pre_gate_fixture_scan": re.compile(r"pre-gate[^/]*fixture[^/]*\.json\Z"),
    "fixture_scan_after_canonical_suite": re.compile(r"(?:post|after)[^/]*fixture[^/]*\.json\Z"),
    "capacity_phase_snapshot": re.compile(r"[^/]*capacity[^/]*snapshot[^/]*\.json\Z"),
    "evidence_index": re.compile(r"[^/]*evidence-index[^/]*\.json\Z"),
    "inventory": re.compile(r"storage-inventory\.json\Z"),
    "strict_capacity_measurement": re.compile(r"[^/]*strict[^/]*capacity[^/]*measurement[^/]*\.json\Z"),
    "inventory_refresh_reconciliation": re.compile(r"[^/]*inventory[^/]*reconciliation[^/]*\.json\Z"),
    "correctness_safety": re.compile(r"[^/]*correctness[^/]*safety[^/]*\.json\Z"),
    "provenance": re.compile(r"[^/]*provenance[^/]*\.json\Z"),
    "pre_gate_mutation_ledger": re.compile(r"[^/]*mutation[^/]*ledger[^/]*\.json\Z"),
}


def _read_r89_evidence_rows(rows: Mapping[str, object]) -> dict[str, tuple[dict[str, object] | None, bytes]]:
    """Read only the bounded R89 evidence roster under the designated log root."""
    evidence_root = Path(R89_ALLOWED_EVIDENCE_ROOT)
    if not evidence_root.is_absolute() or ".." in evidence_root.parts:
        raise RuntimeError("R89 allowed evidence root is not canonical")
    run_root: str | None = None
    loaded: dict[str, tuple[dict[str, object] | None, bytes]] = {}
    for name in R89_REQUIRED_VALIDATION_EVIDENCE:
        row = rows[name]
        relative_text = row["path"]
        relative = Path(relative_text)
        if (relative.is_absolute() or relative.as_posix() != relative_text
                or len(relative.parts) < 2 or ".." in relative.parts
                or relative.parts[0] in {".", ""}
                or re.fullmatch(r"R89-[A-Za-z0-9][A-Za-z0-9._-]*", relative.parts[0]) is None
                or R89_EVIDENCE_BASENAMES[name].fullmatch(relative.name) is None):
            raise RuntimeError(f"R89 evidence path is outside its role-bound evidence root: {name}")
        if run_root is None:
            run_root = relative.parts[0]
        elif relative.parts[0] != run_root:
            raise RuntimeError("R89 evidence rows span multiple evidence runs")
        try:
            raw = storage_policy._read_file_nofollow(
                evidence_root / relative, maximum_bytes=R89_EVIDENCE_MAX_BYTES)
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError(f"R89 evidence file is unavailable or unsafe: {name}") from error
        if (len(raw) != row["size_bytes"]
                or hashlib.sha256(raw).hexdigest() != row["sha256"]):
            raise RuntimeError(f"R89 evidence file changed after review: {name}")
        document: dict[str, object] | None = None
        if name not in {"canonical_storage_suite", "admission_disposition_tests"}:
            try:
                value = json.loads(raw, object_pairs_hook=_json_object_without_duplicate_keys)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(f"R89 evidence JSON is malformed: {name}") from error
            if not isinstance(value, dict):
                raise RuntimeError(f"R89 evidence JSON is not an object: {name}")
            document = value
        loaded[name] = (document, raw)
    return loaded


def _validate_r89_review_evidence(candidate_review: Mapping[str, object]) -> None:
    """Require substantive current behavior, capacity, and validation evidence."""
    behavior = candidate_review.get("behavior_review")
    if (not isinstance(behavior, dict) or set(behavior) != R89_BEHAVIOR_VERDICTS
            or any(not isinstance(verdict, str)
                   or not verdict.strip().startswith("PASS")
                   for verdict in behavior.values())):
        raise RuntimeError("R89 behavior review lacks required role/Gate/runner/fixture/State B/State C/ALLOW-REFUSE/capacity PASS verdicts")

    capacity = candidate_review.get("capacity_review")
    if not isinstance(capacity, dict):
        raise RuntimeError("R89 capacity review is missing")
    required_capacity = {
        "artifact_status", "capacity_ceiling_bytes", "cleanup_credit_bytes",
        "emergency_free_floor_bytes", "headroom_bytes", "inventory_path",
        "inventory_refresh_reconciliation_sha256", "inventory_sha256",
        "peak_bytes", "phase_aware", "phases", "p_b_overlap",
        "project_warning_bytes", "projected_free_bytes",
        "projected_free_minimum_bytes", "shared_quota_credit_bytes",
        "strict_inventory_matches", "strict_measurement_sha256",
        "strict_scoped_bytes", "uncertainty_margin",
        "warning_active",
    }
    if set(capacity) != required_capacity:
        raise RuntimeError("R89 capacity review is not the complete phase-aware schema")
    if (capacity.get("artifact_status") != "PASS"
            or capacity.get("phase_aware") is not True
            or capacity.get("p_b_overlap") is not False
            or capacity.get("cleanup_credit_bytes") != 0
            or capacity.get("shared_quota_credit_bytes") != 0
            or any(capacity.get(key) != expected
                   for key, expected in R89_CAPACITY_THRESHOLD_BYTES.items())):
        raise RuntimeError("R89 capacity review changes an accepted threshold or safety invariant")
    integer_fields = {
        "headroom_bytes", "peak_bytes", "projected_free_bytes", "strict_scoped_bytes",
        *R89_CAPACITY_THRESHOLD_BYTES, "cleanup_credit_bytes", "shared_quota_credit_bytes",
    }
    if (any(type(capacity.get(key)) is not int for key in integer_fields)
            or any(capacity[key] < 0 for key in integer_fields - {"headroom_bytes"})):
        raise RuntimeError("R89 capacity review has an untyped or negative byte value")
    if (capacity.get("strict_inventory_matches") is not True
            or type(capacity.get("warning_active")) is not bool
            or not isinstance(capacity.get("inventory_path"), str)
            or not capacity["inventory_path"].strip()
            or any(not isinstance(capacity.get(field), str)
                   or re.fullmatch(r"[0-9a-f]{64}", capacity[field]) is None
                   for field in ("inventory_sha256", "inventory_refresh_reconciliation_sha256",
                                 "strict_measurement_sha256"))):
        raise RuntimeError("R89 capacity review has invalid inventory or measurement bindings")
    uncertainty = capacity.get("uncertainty_margin")
    if (not isinstance(uncertainty, dict)
            or set(uncertainty) != {"basis", "percent"}
            or type(uncertainty.get("percent")) is not int
            or uncertainty.get("percent") != 10
            or uncertainty.get("basis") != "incremental_phase_growth"):
        raise RuntimeError("R89 capacity review changes the 10 percent uncertainty margin")
    phases = capacity.get("phases")
    if not isinstance(phases, dict) or set(phases) != {"P", "B"}:
        raise RuntimeError("R89 capacity review lacks separate P and B phase summaries")
    for phase_name, phase in phases.items():
        if (not isinstance(phase, dict)
                or set(phase) != {"baseline_scoped_bytes", "raw_growth_bytes",
                                  "accepted_bound_reconciliation_bytes",
                                  "uncertainty_bytes", "projected_growth_bytes",
                                  "projected_peak_bytes"}
                or any(type(phase.get(field)) is not int or phase[field] < 0
                       for field in ("baseline_scoped_bytes", "raw_growth_bytes",
                                     "accepted_bound_reconciliation_bytes",
                                     "uncertainty_bytes", "projected_growth_bytes",
                                     "projected_peak_bytes"))
                or phase["baseline_scoped_bytes"] != (
                    capacity.get("strict_scoped_bytes")
                    + (R89_PREFIX_DURABLE_OUTPUT_BYTES if phase_name == "B" else 0))
                or phase["raw_growth_bytes"] != R89_PHASE_RAW_GROWTH_BYTES[phase_name]
                or phase["accepted_bound_reconciliation_bytes"]
                    != R89_PHASE_ACCEPTED_BOUND_RECONCILIATION_BYTES[phase_name]
                or phase["projected_growth_bytes"]
                    != (phase["raw_growth_bytes"] + phase["uncertainty_bytes"]
                        + phase["accepted_bound_reconciliation_bytes"])
                or phase["projected_peak_bytes"]
                    != phase["baseline_scoped_bytes"] + phase["projected_growth_bytes"]
                or phase["uncertainty_bytes"] != (phase["raw_growth_bytes"] + 9) // 10
                or phase["projected_growth_bytes"]
                    != R89_PHASE_PROJECTED_GROWTH_BYTES[phase_name]):
            raise RuntimeError(f"R89 capacity review has an incomplete or unsafe {phase_name} phase summary")
    peak = max(phase["projected_peak_bytes"] for phase in phases.values())
    if (capacity.get("peak_bytes") != peak
            or peak < capacity["strict_scoped_bytes"]
            or peak > capacity["capacity_ceiling_bytes"]
            or capacity.get("headroom_bytes")
                != capacity["capacity_ceiling_bytes"] - peak
            or capacity.get("headroom_bytes", -1) < 0
            or capacity.get("projected_free_bytes")
                < capacity["projected_free_minimum_bytes"]
            or capacity.get("warning_active")
                != (capacity["strict_scoped_bytes"] >= capacity["project_warning_bytes"])):
        raise RuntimeError("R89 capacity review phase arithmetic or strict-scope verdict is inconsistent")

    evidence = candidate_review.get("validation_evidence_review")
    if (not isinstance(evidence, dict)
            or set(evidence) != {
                "candidate_bound_evidence", "evidence_boundary", "limitations", "run_id"}
            or not isinstance(evidence.get("evidence_boundary"), str)
            or not evidence["evidence_boundary"].strip()
            or not isinstance(evidence.get("limitations"), list)
            or any(not isinstance(item, str) or not item.strip()
                   for item in evidence["limitations"])):
        raise RuntimeError("R89 validation evidence review is empty or malformed")
    rows = evidence.get("candidate_bound_evidence")
    if (not isinstance(rows, dict)
            or set(rows) != set(R89_REQUIRED_VALIDATION_EVIDENCE)):
        raise RuntimeError("R89 validation evidence omits required suite/fixture/capacity/inventory/safety/provenance/mutation rows")
    descriptor_fields = {"path", "sha256", "size_bytes"}
    evidence_binding_fields = {
        "run_id", "candidate_id", "candidate_core_sha256",
        "plan_raw_sha256", "plan_canonical_sha256",
    }
    row_fields = {
        "canonical_storage_suite": descriptor_fields | {
            "status", "log_summary", "summary"},
        "current_pre_gate_fixture_scan": descriptor_fields | {
            "status", "mounts", "workspace_images"},
        "fixture_scan_after_canonical_suite": descriptor_fields | {
            "status", "mounts", "workspace_images"},
        "capacity_phase_snapshot": descriptor_fields | {"status"}
            | (set(candidate_review.get("capacity_review", {})) - {"artifact_status"})
            | evidence_binding_fields,
        "evidence_index": descriptor_fields | {"status", "rows"},
        "inventory": descriptor_fields | {"strict_scoped_bytes"},
        "strict_capacity_measurement": descriptor_fields | {
            "status", "disk_free_bytes", "strict_scoped_bytes", "inventory_sha256"},
        "inventory_refresh_reconciliation": descriptor_fields | {
            "status", "difference_bytes"},
        "correctness_safety": descriptor_fields | {"status"},
        "provenance": descriptor_fields | {"status"},
        "pre_gate_mutation_ledger": descriptor_fields | {"status"},
        "admission_disposition_tests": descriptor_fields | {"status"},
    }
    for name, expected_status in R89_REQUIRED_VALIDATION_EVIDENCE.items():
        row = rows.get(name)
        if (not isinstance(row, dict) or set(row) != row_fields[name]
                or not isinstance(row.get("path"), str) or not row["path"].strip()
                or not isinstance(row.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                or type(row.get("size_bytes")) is not int or row["size_bytes"] <= 0
                or (expected_status is not None and row.get("status") != expected_status)):
            raise RuntimeError(f"R89 required validation evidence row is missing or invalid: {name}")
    evidence_files = _read_r89_evidence_rows(rows)
    run_id = evidence.get("run_id")
    if (not isinstance(run_id, str)
            or re.fullmatch(r"R89-Candidate10-[A-Za-z0-9][A-Za-z0-9._-]*", run_id) is None
            or any(Path(row["path"]).parts[0] != run_id for row in rows.values())):
        raise RuntimeError("R89 evidence run directory is not bound to Candidate 10")
    candidate_id = candidate_review.get("candidate_id")
    candidate_core_sha256 = candidate_review.get("candidate_core_sha256")
    plan_raw_sha256 = candidate_review.get("plan_raw_sha256")
    plan_canonical_sha256 = candidate_review.get("plan_canonical_sha256")
    binding = candidate_review.get("r87_binding")
    manifest_envelope = binding.get("execution_dependency_manifest") if isinstance(binding, dict) else None

    bound_document_names = {
        "current_pre_gate_fixture_scan", "fixture_scan_after_canonical_suite",
        "capacity_phase_snapshot", "strict_capacity_measurement",
        "inventory_refresh_reconciliation", "correctness_safety", "provenance",
        "evidence_index", "pre_gate_mutation_ledger",
    }
    for name in bound_document_names:
        document = evidence_files[name][0]
        if (document.get("run_id") != run_id
                or document.get("candidate_id") != candidate_id
                or document.get("candidate_core_sha256") != candidate_core_sha256
                or document.get("plan_raw_sha256") != plan_raw_sha256
                or document.get("plan_canonical_sha256") != plan_canonical_sha256):
            raise RuntimeError(f"R89 evidence document is not bound to the current run/candidate/plan: {name}")

    suite_row = rows["canonical_storage_suite"]
    suite_raw = evidence_files["canonical_storage_suite"][1]
    try:
        suite_text = suite_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("R89 canonical suite log is not UTF-8") from error
    suite_lines = [line.strip() for line in suite_text.splitlines() if line.strip()]
    suite_run = re.search(r"^Ran ([1-9][0-9]*) tests? in ([0-9]+(?:\.[0-9]+)?s)$",
                          suite_text, re.MULTILINE)
    if (suite_run is None or not suite_lines or suite_lines[-1] != "OK"
            or re.search(r"(?im)^(?:FAILED|ERROR:|FAIL:)|failures=[1-9]|errors=[1-9]|skipped=[1-9]",
                         suite_text)
            or suite_row.get("log_summary") != [suite_run.group(0), "OK"]
            or suite_row.get("summary") != (
                f"{suite_run.group(0)}; 0 failures; 0 errors; 0 skips; OK")):
        raise RuntimeError("R89 canonical storage suite evidence does not prove zero failures/errors/skips")

    for name in ("current_pre_gate_fixture_scan", "fixture_scan_after_canonical_suite"):
        row = rows[name]
        document = evidence_files[name][0]
        if (type(row.get("mounts")) is not int or row["mounts"] != 0
                or type(row.get("workspace_images")) is not int or row["workspace_images"] != 0
                or document.get("status") != "PASS_ZERO_OWNED_FIXTURES"
                or document.get("mounts") != row["mounts"]
                or document.get("workspace_images") != row["workspace_images"]
                or type(document.get("mounts")) is not int
                or type(document.get("workspace_images")) is not int
                or any(not isinstance(document.get(field), list) or document[field]
                       for field in ("baseline_images", "baseline_mounts",
                                     "hdiutil_workspace_images", "private_tmp_fgmetal_mounts"))):
            raise RuntimeError(f"R89 immediate fixture scan is not clean: {name}")
    capacity_row = rows["capacity_phase_snapshot"]
    capacity_document = evidence_files["capacity_phase_snapshot"][0]
    if (capacity_document.get("status") != "PASS"
            or any(capacity_document.get(field) != capacity.get(field)
                   for field in (
                       "capacity_ceiling_bytes", "project_warning_bytes",
                       "projected_free_minimum_bytes", "emergency_free_floor_bytes",
                       "cleanup_credit_bytes", "shared_quota_credit_bytes",
                       "projected_free_bytes", "inventory_sha256",
                       "strict_measurement_sha256", "phase_aware", "p_b_overlap",
                       "uncertainty_margin", "peak_bytes", "headroom_bytes",
                       "strict_scoped_bytes", "phases"))):
        raise RuntimeError("R89 capacity snapshot contents do not support the reviewed capacity summary")
    if (type(rows["evidence_index"].get("rows")) is not int
            or rows["evidence_index"]["rows"] <= 0):
        raise RuntimeError("R89 candidate evidence index is empty")
    for field in ("capacity_ceiling_bytes", "projected_free_minimum_bytes",
                  "project_warning_bytes", "emergency_free_floor_bytes",
                  "cleanup_credit_bytes", "shared_quota_credit_bytes"):
        if capacity_row.get(field) != capacity.get(field):
            raise RuntimeError("R89 capacity evidence row disagrees with the reviewed capacity summary")
    if (capacity_row.get("phase_aware") is not True
            or capacity_row.get("p_b_overlap") is not False
            or capacity_row.get("uncertainty_margin") != uncertainty
            or capacity_row.get("phases") != phases
            or capacity_row.get("projected_free_bytes") != capacity["projected_free_bytes"]
            or capacity_row.get("strict_scoped_bytes") != capacity["strict_scoped_bytes"]
            or capacity_row.get("inventory_sha256") != capacity["inventory_sha256"]
            or capacity_row.get("strict_measurement_sha256")
                != capacity["strict_measurement_sha256"]
            or capacity_row.get("emergency_free_floor_bytes")
                != capacity["emergency_free_floor_bytes"]):
        raise RuntimeError("R89 capacity evidence row omits the reviewed phase-aware summary")
    inventory_row = rows["inventory"]
    if (inventory_row.get("sha256") != capacity.get("inventory_sha256")
            or inventory_row.get("path") != capacity.get("inventory_path")
            or inventory_row.get("strict_scoped_bytes") != capacity.get("strict_scoped_bytes")):
        raise RuntimeError("R89 inventory evidence row disagrees with the capacity review")
    strict_row = rows["strict_capacity_measurement"]
    if (type(strict_row.get("disk_free_bytes")) is not int
            or strict_row["disk_free_bytes"] < capacity["emergency_free_floor_bytes"]
            or strict_row.get("strict_scoped_bytes") != capacity["strict_scoped_bytes"]
            or strict_row.get("inventory_sha256") != capacity["inventory_sha256"]
            or strict_row.get("sha256") != capacity["strict_measurement_sha256"]
            or capacity["projected_free_bytes"] != strict_row["disk_free_bytes"]
                - (peak - capacity["strict_scoped_bytes"])):
        raise RuntimeError("R89 strict capacity measurement does not support the projected-free result")
    strict_document = evidence_files["strict_capacity_measurement"][0]
    if (strict_document.get("status") != "PASS"
            or strict_document.get("strict_inventory_matches") is not True
            or strict_document.get("disk_free_bytes") != strict_row["disk_free_bytes"]
            or strict_document.get("strict_scoped_bytes") != capacity["strict_scoped_bytes"]
            or strict_document.get("inventory_sha256") != capacity["inventory_sha256"]):
        raise RuntimeError("R89 strict measurement contents do not support the capacity claims")
    inventory_document = evidence_files["inventory"][0]
    inventory_totals = inventory_document.get("totals")
    filesystem = inventory_document.get("filesystem")
    if (not isinstance(inventory_totals, dict) or not isinstance(filesystem, dict)
            or inventory_totals.get("fgmetal_scoped_allocated_inode_bytes")
                != capacity["strict_scoped_bytes"]
            or type(filesystem.get("free_bytes")) is not int
            or filesystem.get("free_bytes") != strict_row.get("disk_free_bytes")
            or rows["inventory"].get("strict_scoped_bytes") != capacity["strict_scoped_bytes"]):
        raise RuntimeError("R89 inventory contents do not support the strict scoped measurement")
    reconciliation_row = rows.get("inventory_refresh_reconciliation")
    if (not isinstance(reconciliation_row, dict)
            or reconciliation_row.get("status") != "PASS"
            or reconciliation_row.get("difference_bytes") != 0
            or reconciliation_row.get("sha256")
                != capacity.get("inventory_refresh_reconciliation_sha256")):
        raise RuntimeError("R89 inventory refresh reconciliation does not match the capacity review")
    reconciliation_document = evidence_files["inventory_refresh_reconciliation"][0]
    if (reconciliation_document.get("status") != "PASS"
            or reconciliation_document.get("difference_bytes") != 0
            or reconciliation_document.get("inventory_sha256") != capacity["inventory_sha256"]
            or reconciliation_document.get("strict_scoped_bytes")
                != capacity["strict_scoped_bytes"]):
        raise RuntimeError("R89 reconciliation contents do not prove an exact inventory refresh")

    reviewer_identities: set[str] = set()
    for name, expected_checks in (
            ("correctness_safety", R89_CORRECTNESS_SAFETY_CHECK_NAMES),
            ("provenance", R89_PROVENANCE_CHECK_NAMES)):
        document = evidence_files[name][0]
        expected_identity, expected_scope = R89_REVIEWER_IDENTITIES[name]
        if (set(document) != {
                "status", "run_id", "candidate_id", "candidate_core_sha256",
                "plan_raw_sha256", "plan_canonical_sha256", "reviewer_role",
                "reviewer_scope", "reviewer_identity", "reviewer_identity_sha256",
                "findings", "checks",
                *({"dependency_manifest_path"} if name == "provenance" else set())}
                or document.get("status") != "PASS"
                or document.get("candidate_id") != candidate_id
                or document.get("candidate_core_sha256") != candidate_core_sha256
                or not isinstance(document.get("findings"), list)
                or document["findings"]
                or document.get("reviewer_role") != "independent_reviewer"
                or document.get("reviewer_scope") != expected_scope
                or document.get("reviewer_identity") != expected_identity
                or document.get("reviewer_identity_sha256")
                    != hashlib.sha256(expected_identity.encode("utf-8")).hexdigest()):
            raise RuntimeError(f"R89 {name} evidence contents do not support PASS for this candidate")
        reviewer_identities.add(document["reviewer_identity"])
        checks = document.get("checks")
        if (not isinstance(checks, list)
                or len(checks) != len(expected_checks)
                or {check.get("name") for check in checks if isinstance(check, dict)}
                    != expected_checks
                or any(not isinstance(check, dict)
                       or set(check) != {"name", "status", "details"}
                       or check.get("status") != "PASS"
                       or not isinstance(check.get("details"), str)
                       or not check["details"].strip()
                       for check in checks)):
            raise RuntimeError(f"R89 {name} evidence lacks passing checks")
    if len(reviewer_identities) != 2:
        raise RuntimeError("R89 correctness/safety and provenance reviewers must be distinct")
    expected_manifest_path = manifest_envelope.get("path") if isinstance(manifest_envelope, dict) else None
    if evidence_files["provenance"][0].get("dependency_manifest_path") != expected_manifest_path:
        raise RuntimeError("R89 provenance evidence does not name the exact dependency manifest")

    index_document = evidence_files["evidence_index"][0]
    index_entries = index_document.get("evidence")
    if (set(index_document) != {
            "status", "run_id", "candidate_id", "candidate_core_sha256",
            "plan_raw_sha256", "plan_canonical_sha256", "evidence"}
            or index_document.get("status") != "PASS"
            or index_document.get("candidate_id") != candidate_id
            or not isinstance(index_entries, list) or not index_entries
            or len(index_entries) != rows["evidence_index"]["rows"]
            or len(index_entries) != len(rows) - 1
            or any(not isinstance(entry, dict)
                   or not isinstance(entry.get("path"), str) or not entry["path"]
                   or not isinstance(entry.get("role"), str) or not entry["role"]
                   or not isinstance(entry.get("sha256"), str)
                   or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                   or type(entry.get("size_bytes")) is not int or entry["size_bytes"] <= 0
                   for entry in index_entries)):
        raise RuntimeError("R89 evidence index contents are empty or malformed")
    expected_index_rows = {
        name: {"path": row["path"], "sha256": row["sha256"],
               "size_bytes": row["size_bytes"]}
        for name, row in rows.items() if name != "evidence_index"
    }
    indexed: dict[str, dict[str, object]] = {}
    for entry in index_entries:
        role = entry["role"]
        if role in indexed:
            raise RuntimeError("R89 evidence index contains duplicate evidence roles")
        indexed[role] = {key: entry[key] for key in ("path", "sha256", "size_bytes")}
    if indexed != expected_index_rows:
        raise RuntimeError("R89 evidence index does not exactly cover the reviewed evidence rows")
    ledger_row = rows["pre_gate_mutation_ledger"]
    ledger_document = evidence_files["pre_gate_mutation_ledger"][0]
    events = ledger_document.get("events")
    if (set(ledger_document) != {
            "status", "run_id", "candidate_id", "candidate_core_sha256",
            "plan_raw_sha256", "plan_canonical_sha256", "events"}
            or ledger_document.get("candidate_id") != candidate_id
            or ledger_document.get("status") != ledger_row.get("status")
            or ledger_document.get("status") != "PASS"
            or not isinstance(events, list)
            or {event.get("event") for event in events if isinstance(event, dict)}
                != R89_MUTATION_LEDGER_EVENTS
            or len(events) != len(R89_MUTATION_LEDGER_EVENTS)
            or any(not isinstance(event, dict)
                   or set(event) != {"event", "status", "details"}
                   or not isinstance(event.get("event"), str) or not event["event"].strip()
                   or event.get("status") != "PASS"
                   or not isinstance(event.get("details"), str) or not event["details"].strip()
                   for event in events)):
        raise RuntimeError("R89 mutation ledger contents do not support its claimed status")



def _read_r90_evidence_rows(
        rows: Mapping[str, object], *,
        required_rows: Mapping[str, str | None] = R90_REQUIRED_VALIDATION_EVIDENCE,
        basenames: Mapping[str, re.Pattern[str]] = R90_EVIDENCE_BASENAMES,
        run_prefix: str = "R90") -> dict[str, tuple[dict[str, object] | None, bytes]]:
    """Read only the bounded R90 evidence roster under the designated log root."""
    evidence_root = Path(R90_ALLOWED_EVIDENCE_ROOT)
    if not evidence_root.is_absolute() or ".." in evidence_root.parts:
        raise RuntimeError("R90 allowed evidence root is not canonical")
    run_root: str | None = None
    loaded: dict[str, tuple[dict[str, object] | None, bytes]] = {}
    for name in required_rows:
        row = rows[name]
        relative_text = row["path"]
        relative = Path(relative_text)
        if (relative.is_absolute() or relative.as_posix() != relative_text
                or len(relative.parts) < 2 or ".." in relative.parts
                or relative.parts[0] in {".", ""}
                or re.fullmatch(
                    rf"{re.escape(run_prefix)}-[A-Za-z0-9][A-Za-z0-9._-]*",
                    relative.parts[0]) is None
                or basenames[name].fullmatch(relative.name) is None):
            raise RuntimeError(f"R90 evidence path is outside its role-bound evidence root: {name}")
        if run_root is None:
            run_root = relative.parts[0]
        elif relative.parts[0] != run_root:
            raise RuntimeError("R90 evidence rows span multiple evidence runs")
        try:
            raw = storage_policy._read_file_nofollow(
                evidence_root / relative, maximum_bytes=R90_EVIDENCE_MAX_BYTES)
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError(f"R90 evidence file is unavailable or unsafe: {name}") from error
        if (len(raw) != row["size_bytes"]
                or hashlib.sha256(raw).hexdigest() != row["sha256"]):
            raise RuntimeError(f"R90 evidence file changed after review: {name}")
        document: dict[str, object] | None = None
        if name not in {"canonical_storage_suite", "admission_disposition_tests",
                        "workspace_transition_tests", "failure_recovery_tests",
                        "r92_handoff_tests", "r93_handoff_tests",
                        "r94_running_authority_tests",
                        "r95_successor_focused_tests", "r96_post_exit_watcher_tests"}:
            try:
                value = json.loads(raw, object_pairs_hook=_json_object_without_duplicate_keys)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(f"R90 evidence JSON is malformed: {name}") from error
            if not isinstance(value, dict):
                raise RuntimeError(f"R90 evidence JSON is not an object: {name}")
            document = value
        loaded[name] = (document, raw)
    return loaded


def _r90_evidence_run_matches_candidate(run_id: object, candidate_id: object) -> bool:
    """Bind an R90 external evidence root to the candidate number it names."""
    if (not isinstance(run_id, str) or not isinstance(candidate_id, str)
            or R87_CANDIDATE_PATH_PATTERN.fullmatch(f"{candidate_id}.json") is None):
        return False
    candidate_number = candidate_id.rsplit("-", 1)[-1]
    run_prefix = ("R96" if candidate_id == R96_CANDIDATE_ID else
                  "R95" if candidate_id == R95_CANDIDATE_ID else
                  "R94" if candidate_id == R94_CANDIDATE_ID else
                  "R93" if candidate_id == R93_CANDIDATE_ID else
                  "R92" if candidate_id == R92_CANDIDATE_ID else
                  "R91" if candidate_id == R91_CANDIDATE_ID else "R90")
    return re.fullmatch(
        rf"{run_prefix}-Candidate{candidate_number}-[A-Za-z0-9][A-Za-z0-9._-]*",
        run_id) is not None


def _r90_review_evidence_row_fields(
        candidate_review: Mapping[str, object], *,
        required_rows: Mapping[str, str | None] = R90_REQUIRED_VALIDATION_EVIDENCE
        ) -> dict[str, set[str]]:
    """Return the exact row schema for every required R90 evidence role."""
    descriptor_fields = {"path", "sha256", "size_bytes"}
    evidence_binding_fields = {
        "run_id", "candidate_id", "candidate_core_sha256",
        "plan_raw_sha256", "plan_canonical_sha256",
    }
    capacity_review = candidate_review.get("capacity_review")
    capacity_fields = (set(capacity_review) - {"artifact_status"}
                       if isinstance(capacity_review, Mapping) else set())
    fields = {
        "canonical_storage_suite": descriptor_fields | {
            "status", "log_summary", "summary"},
        "current_pre_gate_fixture_scan": descriptor_fields | {
            "status", "mounts", "workspace_images"},
        "fixture_scan_after_canonical_suite": descriptor_fields | {
            "status", "mounts", "workspace_images"},
        "capacity_phase_snapshot": descriptor_fields | {"status"}
            | capacity_fields | evidence_binding_fields,
        "evidence_index": descriptor_fields | {"status", "rows"},
        "inventory": descriptor_fields | {"strict_scoped_bytes"},
        "strict_capacity_measurement": descriptor_fields | {
            "status", "disk_free_bytes", "strict_scoped_bytes", "inventory_sha256"},
        "inventory_refresh_reconciliation": descriptor_fields | {
            "status", "difference_bytes"},
        "correctness_safety": descriptor_fields | {"status"},
        "provenance": descriptor_fields | {"status"},
        "pre_gate_mutation_ledger": descriptor_fields | {"status"},
        "admission_disposition_tests": descriptor_fields | {"status"},
    }
    for name in set(required_rows) - set(fields):
        fields[name] = descriptor_fields | {"status"}
    return fields


def _r90_projected_free_bytes(*, inventory_free_bytes: int,
                              direct_free_bytes: int,
                              peak_bytes: int,
                              strict_scoped_bytes: int) -> int:
    """Project from the lower free-space sample when measurements are time-separated."""
    values = (inventory_free_bytes, direct_free_bytes, peak_bytes, strict_scoped_bytes)
    if (any(type(value) is not int or value < 0 for value in values)
            or peak_bytes < strict_scoped_bytes):
        raise RuntimeError("R90 projected-free inputs are malformed or inconsistent")
    return min(inventory_free_bytes, direct_free_bytes) - (peak_bytes - strict_scoped_bytes)


def _r90_reviewer_identity_policy(
        role: str,
        reviewer_identities: Mapping[str, tuple[str, str]],
        ) -> tuple[str, str]:
    """Return the fixed identity and scope for one independent reviewer role."""
    if not isinstance(reviewer_identities, Mapping):
        raise RuntimeError("R90 reviewer identity policy is malformed")
    expected = reviewer_identities.get(role)
    if (not isinstance(expected, tuple) or len(expected) != 2
            or any(not isinstance(value, str) or not value.strip() for value in expected)):
        raise RuntimeError("R90 reviewer identity policy is malformed")
    return expected


def _validate_r90_reviewer_evidence(
        evidence_documents: Mapping[str, object], *,
        candidate_id: str,
        candidate_core_sha256: str,
        expected_manifest_path: str | None,
        correctness_checks: set[str],
        provenance_checks: set[str],
        reviewer_identities: Mapping[str, tuple[str, str]]) -> None:
    """Validate the two independent reviewer records and their fixed identities."""
    seen_reviewer_identities: set[str] = set()
    for name, expected_checks in (
            ("correctness_safety", correctness_checks),
            ("provenance", provenance_checks)):
        document = evidence_documents.get(name)
        expected_identity, expected_scope = _r90_reviewer_identity_policy(
            name, reviewer_identities)
        if (not isinstance(document, dict)
                or set(document) != {
                    "status", "run_id", "candidate_id", "candidate_core_sha256",
                    "plan_raw_sha256", "plan_canonical_sha256", "reviewer_role",
                    "reviewer_scope", "reviewer_identity", "reviewer_identity_sha256",
                    "findings", "checks",
                    *({"dependency_manifest_path"} if name == "provenance" else set())}
                or document.get("status") != "PASS"
                or document.get("candidate_id") != candidate_id
                or document.get("candidate_core_sha256") != candidate_core_sha256
                or not isinstance(document.get("findings"), list)
                or document["findings"]
                or document.get("reviewer_role") != "independent_reviewer"
                or document.get("reviewer_scope") != expected_scope
                or document.get("reviewer_identity") != expected_identity
                or document.get("reviewer_identity_sha256")
                    != hashlib.sha256(expected_identity.encode("utf-8")).hexdigest()):
            raise RuntimeError(f"R90 {name} evidence contents do not support PASS for this candidate")
        seen_reviewer_identities.add(document["reviewer_identity"])
        checks = document.get("checks")
        if (not isinstance(checks, list)
                or len(checks) != len(expected_checks)
                or {check.get("name") for check in checks if isinstance(check, dict)}
                    != expected_checks
                or any(not isinstance(check, dict)
                       or set(check) != {"name", "status", "details"}
                       or check.get("status") != "PASS"
                       or not isinstance(check.get("details"), str)
                       or not check["details"].strip()
                       for check in checks)):
            raise RuntimeError(f"R90 {name} evidence lacks passing checks")
    if len(seen_reviewer_identities) != 2:
        raise RuntimeError("R90 correctness/safety and provenance reviewers must be distinct")
    provenance = evidence_documents["provenance"]
    if (not isinstance(provenance, dict)
            or provenance.get("dependency_manifest_path") != expected_manifest_path):
        raise RuntimeError("R90 provenance evidence does not name the exact dependency manifest")


def _validate_r90_review_evidence(
        candidate_review: Mapping[str, object], *,
        behavior_verdicts: set[str] = R90_BEHAVIOR_VERDICTS,
        required_rows: Mapping[str, str | None] = R90_REQUIRED_VALIDATION_EVIDENCE,
        evidence_basenames: Mapping[str, re.Pattern[str]] = R90_EVIDENCE_BASENAMES,
        run_prefix: str = "R90",
        correctness_checks: set[str] = R90_CORRECTNESS_SAFETY_CHECK_NAMES,
        provenance_checks: set[str] = R90_PROVENANCE_CHECK_NAMES,
        reviewer_identities: Mapping[str, tuple[str, str]] = R90_REVIEWER_IDENTITIES,
        mutation_events: set[str] = R90_MUTATION_LEDGER_EVENTS) -> None:
    """Require substantive current behavior, capacity, and validation evidence."""
    behavior = candidate_review.get("behavior_review")
    if (not isinstance(behavior, dict) or set(behavior) != behavior_verdicts
            or any(not isinstance(verdict, str)
                   or not verdict.strip().startswith("PASS")
                   for verdict in behavior.values())):
        raise RuntimeError("R90 behavior review lacks required role/Gate/runner/fixture/State B/State C/ALLOW-REFUSE/capacity PASS verdicts")

    capacity = candidate_review.get("capacity_review")
    if not isinstance(capacity, dict):
        raise RuntimeError("R90 capacity review is missing")
    required_capacity = {
        "artifact_status", "capacity_ceiling_bytes", "cleanup_credit_bytes",
        "emergency_free_floor_bytes", "headroom_bytes", "inventory_path",
        "inventory_refresh_reconciliation_sha256", "inventory_sha256",
        "peak_bytes", "phase_aware", "phases", "p_b_overlap",
        "project_warning_bytes", "projected_free_bytes",
        "projected_free_minimum_bytes", "shared_quota_credit_bytes",
        "strict_inventory_matches", "strict_measurement_sha256",
        "strict_scoped_bytes", "uncertainty_margin",
        "warning_active",
    }
    if set(capacity) != required_capacity:
        raise RuntimeError("R90 capacity review is not the complete phase-aware schema")
    if (capacity.get("artifact_status") != "PASS"
            or capacity.get("phase_aware") is not True
            or capacity.get("p_b_overlap") is not False
            or capacity.get("cleanup_credit_bytes") != 0
            or capacity.get("shared_quota_credit_bytes") != 0
            or any(capacity.get(key) != expected
                   for key, expected in R90_CAPACITY_THRESHOLD_BYTES.items())):
        raise RuntimeError("R90 capacity review changes an accepted threshold or safety invariant")
    integer_fields = {
        "headroom_bytes", "peak_bytes", "projected_free_bytes", "strict_scoped_bytes",
        *R90_CAPACITY_THRESHOLD_BYTES, "cleanup_credit_bytes", "shared_quota_credit_bytes",
    }
    if (any(type(capacity.get(key)) is not int for key in integer_fields)
            or any(capacity[key] < 0 for key in integer_fields - {"headroom_bytes"})):
        raise RuntimeError("R90 capacity review has an untyped or negative byte value")
    if (capacity.get("strict_inventory_matches") is not True
            or type(capacity.get("warning_active")) is not bool
            or not isinstance(capacity.get("inventory_path"), str)
            or not capacity["inventory_path"].strip()
            or any(not isinstance(capacity.get(field), str)
                   or re.fullmatch(r"[0-9a-f]{64}", capacity[field]) is None
                   for field in ("inventory_sha256", "inventory_refresh_reconciliation_sha256",
                                 "strict_measurement_sha256"))):
        raise RuntimeError("R90 capacity review has invalid inventory or measurement bindings")
    uncertainty = capacity.get("uncertainty_margin")
    if (not isinstance(uncertainty, dict)
            or set(uncertainty) != {"basis", "percent"}
            or type(uncertainty.get("percent")) is not int
            or uncertainty.get("percent") != 10
            or uncertainty.get("basis") != "incremental_phase_growth"):
        raise RuntimeError("R90 capacity review changes the 10 percent uncertainty margin")
    phases = capacity.get("phases")
    if not isinstance(phases, dict) or set(phases) != {"P", "B"}:
        raise RuntimeError("R90 capacity review lacks separate P and B phase summaries")
    for phase_name, phase in phases.items():
        if (not isinstance(phase, dict)
                or set(phase) != {"baseline_scoped_bytes", "raw_growth_bytes",
                                  "accepted_bound_reconciliation_bytes",
                                  "uncertainty_bytes", "projected_growth_bytes",
                                  "projected_peak_bytes"}
                or any(type(phase.get(field)) is not int or phase[field] < 0
                       for field in ("baseline_scoped_bytes", "raw_growth_bytes",
                                     "accepted_bound_reconciliation_bytes",
                                     "uncertainty_bytes", "projected_growth_bytes",
                                     "projected_peak_bytes"))
                or phase["baseline_scoped_bytes"] != (
                    capacity.get("strict_scoped_bytes")
                    + (R90_PREFIX_DURABLE_OUTPUT_BYTES if phase_name == "B" else 0))
                or phase["raw_growth_bytes"] != R90_PHASE_RAW_GROWTH_BYTES[phase_name]
                or phase["accepted_bound_reconciliation_bytes"]
                    != R90_PHASE_ACCEPTED_BOUND_RECONCILIATION_BYTES[phase_name]
                or phase["projected_growth_bytes"]
                    != (phase["raw_growth_bytes"] + phase["uncertainty_bytes"]
                        + phase["accepted_bound_reconciliation_bytes"])
                or phase["projected_peak_bytes"]
                    != phase["baseline_scoped_bytes"] + phase["projected_growth_bytes"]
                or phase["uncertainty_bytes"] != (phase["raw_growth_bytes"] + 9) // 10
                or phase["projected_growth_bytes"]
                    != R90_PHASE_PROJECTED_GROWTH_BYTES[phase_name]):
            raise RuntimeError(f"R90 capacity review has an incomplete or unsafe {phase_name} phase summary")
    peak = max(phase["projected_peak_bytes"] for phase in phases.values())
    if (capacity.get("peak_bytes") != peak
            or peak < capacity["strict_scoped_bytes"]
            or peak > capacity["capacity_ceiling_bytes"]
            or capacity.get("headroom_bytes")
                != capacity["capacity_ceiling_bytes"] - peak
            or capacity.get("headroom_bytes", -1) < 0
            or capacity.get("projected_free_bytes")
                < capacity["projected_free_minimum_bytes"]
            or capacity.get("warning_active")
                != (capacity["strict_scoped_bytes"] >= capacity["project_warning_bytes"])):
        raise RuntimeError("R90 capacity review phase arithmetic or strict-scope verdict is inconsistent")

    evidence = candidate_review.get("validation_evidence_review")
    if (not isinstance(evidence, dict)
            or set(evidence) != {
                "candidate_bound_evidence", "evidence_boundary", "limitations", "run_id"}
            or not isinstance(evidence.get("evidence_boundary"), str)
            or not evidence["evidence_boundary"].strip()
            or not isinstance(evidence.get("limitations"), list)
            or any(not isinstance(item, str) or not item.strip()
                   for item in evidence["limitations"])):
        raise RuntimeError("R90 validation evidence review is empty or malformed")
    rows = evidence.get("candidate_bound_evidence")
    if (not isinstance(rows, dict)
            or set(rows) != set(required_rows)):
        raise RuntimeError("R90 validation evidence omits required suite/fixture/capacity/inventory/safety/provenance/mutation rows")
    row_fields = _r90_review_evidence_row_fields(candidate_review, required_rows=required_rows)
    for name, expected_status in required_rows.items():
        row = rows.get(name)
        if (not isinstance(row, dict) or set(row) != row_fields[name]
                or not isinstance(row.get("path"), str) or not row["path"].strip()
                or not isinstance(row.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", row["sha256"]) is None
                or type(row.get("size_bytes")) is not int or row["size_bytes"] <= 0
                or (expected_status is not None and row.get("status") != expected_status)):
            raise RuntimeError(f"R90 required validation evidence row is missing or invalid: {name}")
    evidence_files = _read_r90_evidence_rows(
        rows, required_rows=required_rows, basenames=evidence_basenames,
        run_prefix=run_prefix)
    run_id = evidence.get("run_id")
    candidate_id = candidate_review.get("candidate_id")
    if (not _r90_evidence_run_matches_candidate(run_id, candidate_id)
            or any(Path(row["path"]).parts[0] != run_id for row in rows.values())):
        raise RuntimeError(
            f"R90 evidence run directory is not bound to candidate {candidate_id}")
    candidate_core_sha256 = candidate_review.get("candidate_core_sha256")
    plan_raw_sha256 = candidate_review.get("plan_raw_sha256")
    plan_canonical_sha256 = candidate_review.get("plan_canonical_sha256")
    binding = candidate_review.get("r87_binding")
    manifest_envelope = binding.get("execution_dependency_manifest") if isinstance(binding, dict) else None

    bound_document_names = {
        "current_pre_gate_fixture_scan", "fixture_scan_after_canonical_suite",
        "capacity_phase_snapshot", "strict_capacity_measurement",
        "inventory_refresh_reconciliation", "correctness_safety", "provenance",
        "evidence_index", "pre_gate_mutation_ledger",
    }
    for name in bound_document_names:
        document = evidence_files[name][0]
        if (document.get("run_id") != run_id
                or document.get("candidate_id") != candidate_id
                or document.get("candidate_core_sha256") != candidate_core_sha256
                or document.get("plan_raw_sha256") != plan_raw_sha256
                or document.get("plan_canonical_sha256") != plan_canonical_sha256):
            raise RuntimeError(f"R90 evidence document is not bound to the current run/candidate/plan: {name}")

    suite_row = rows["canonical_storage_suite"]
    suite_raw = evidence_files["canonical_storage_suite"][1]
    try:
        suite_text = suite_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("R90 canonical suite log is not UTF-8") from error
    suite_lines = [line.strip() for line in suite_text.splitlines() if line.strip()]
    suite_run = re.search(r"^Ran ([1-9][0-9]*) tests? in ([0-9]+(?:\.[0-9]+)?s)$",
                          suite_text, re.MULTILINE)
    if (suite_run is None or not suite_lines or suite_lines[-1] != "OK"
            or re.search(r"(?im)^(?:FAILED|ERROR:|FAIL:)|failures=[1-9]|errors=[1-9]|skipped=[1-9]",
                         suite_text)
            or suite_row.get("log_summary") != [suite_run.group(0), "OK"]
            or suite_row.get("summary") != (
                f"{suite_run.group(0)}; 0 failures; 0 errors; 0 skips; OK")):
        raise RuntimeError("R90 canonical storage suite evidence does not prove zero failures/errors/skips")

    for test_name in ("workspace_transition_tests", "failure_recovery_tests",
                      "r92_handoff_tests", "r93_handoff_tests",
                      "r94_running_authority_tests", "r95_successor_focused_tests"):
        if test_name not in evidence_files:
            continue
        test_text = evidence_files[test_name][1].decode("utf-8", errors="replace")
        if (not test_text.strip().endswith("OK")
                or re.search(r"(?im)^(?:FAILED|ERROR:|FAIL:)|failures=[1-9]|errors=[1-9]",
                             test_text)):
            raise RuntimeError(f"R90 focused test evidence does not prove a passing run: {test_name}")

    disposition_test_log = evidence_files["admission_disposition_tests"][1]
    try:
        disposition_test_text = disposition_test_log.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("R90 admission-disposition test log is not UTF-8") from error
    if (not disposition_test_text.strip().endswith("OK")
            or re.search(r"(?im)^(?:FAILED|ERROR:|FAIL:)|failures=[1-9]|errors=[1-9]",
                         disposition_test_text)):
        raise RuntimeError("R90 admission-disposition test evidence does not prove a passing run")

    for name in ("current_pre_gate_fixture_scan", "fixture_scan_after_canonical_suite"):
        row = rows[name]
        document = evidence_files[name][0]
        if (type(row.get("mounts")) is not int or row["mounts"] != 0
                or type(row.get("workspace_images")) is not int or row["workspace_images"] != 0
                or document.get("status") != "PASS_ZERO_OWNED_FIXTURES"
                or document.get("mounts") != row["mounts"]
                or document.get("workspace_images") != row["workspace_images"]
                or type(document.get("mounts")) is not int
                or type(document.get("workspace_images")) is not int
                or any(not isinstance(document.get(field), list) or document[field]
                       for field in ("baseline_images", "baseline_mounts",
                                     "hdiutil_workspace_images", "private_tmp_fgmetal_mounts"))):
            raise RuntimeError(f"R90 immediate fixture scan is not clean: {name}")
    capacity_row = rows["capacity_phase_snapshot"]
    capacity_document = evidence_files["capacity_phase_snapshot"][0]
    if (capacity_document.get("status") != "PASS"
            or any(capacity_document.get(field) != capacity.get(field)
                   for field in (
                       "capacity_ceiling_bytes", "project_warning_bytes",
                       "projected_free_minimum_bytes", "emergency_free_floor_bytes",
                       "cleanup_credit_bytes", "shared_quota_credit_bytes",
                       "projected_free_bytes", "inventory_sha256",
                       "strict_measurement_sha256", "phase_aware", "p_b_overlap",
                       "uncertainty_margin", "peak_bytes", "headroom_bytes",
                       "strict_scoped_bytes", "phases"))):
        raise RuntimeError("R90 capacity snapshot contents do not support the reviewed capacity summary")
    if (type(rows["evidence_index"].get("rows")) is not int
            or rows["evidence_index"]["rows"] <= 0):
        raise RuntimeError("R90 candidate evidence index is empty")
    for field in ("capacity_ceiling_bytes", "projected_free_minimum_bytes",
                  "project_warning_bytes", "emergency_free_floor_bytes",
                  "cleanup_credit_bytes", "shared_quota_credit_bytes"):
        if capacity_row.get(field) != capacity.get(field):
            raise RuntimeError("R90 capacity evidence row disagrees with the reviewed capacity summary")
    if (capacity_row.get("phase_aware") is not True
            or capacity_row.get("p_b_overlap") is not False
            or capacity_row.get("uncertainty_margin") != uncertainty
            or capacity_row.get("phases") != phases
            or capacity_row.get("projected_free_bytes") != capacity["projected_free_bytes"]
            or capacity_row.get("strict_scoped_bytes") != capacity["strict_scoped_bytes"]
            or capacity_row.get("inventory_sha256") != capacity["inventory_sha256"]
            or capacity_row.get("strict_measurement_sha256")
                != capacity["strict_measurement_sha256"]
            or capacity_row.get("emergency_free_floor_bytes")
                != capacity["emergency_free_floor_bytes"]):
        raise RuntimeError("R90 capacity evidence row omits the reviewed phase-aware summary")
    inventory_row = rows["inventory"]
    if (inventory_row.get("sha256") != capacity.get("inventory_sha256")
            or inventory_row.get("path") != capacity.get("inventory_path")
            or inventory_row.get("strict_scoped_bytes") != capacity.get("strict_scoped_bytes")):
        raise RuntimeError("R90 inventory evidence row disagrees with the capacity review")
    inventory_document = evidence_files["inventory"][0]
    inventory_totals = inventory_document.get("totals")
    filesystem = inventory_document.get("filesystem")
    if (not isinstance(inventory_totals, dict) or not isinstance(filesystem, dict)
            or inventory_totals.get("fgmetal_scoped_allocated_inode_bytes")
                != capacity["strict_scoped_bytes"]
            or type(filesystem.get("free_bytes")) is not int
            or filesystem["free_bytes"] < capacity["emergency_free_floor_bytes"]
            or inventory_row.get("strict_scoped_bytes") != capacity["strict_scoped_bytes"]):
        raise RuntimeError("R90 inventory contents do not support the strict scoped measurement")
    strict_row = rows["strict_capacity_measurement"]
    if (type(strict_row.get("disk_free_bytes")) is not int
            or strict_row["disk_free_bytes"] < capacity["emergency_free_floor_bytes"]
            or strict_row.get("strict_scoped_bytes") != capacity["strict_scoped_bytes"]
            or strict_row.get("inventory_sha256") != capacity["inventory_sha256"]
            or strict_row.get("sha256") != capacity["strict_measurement_sha256"]
            or capacity["projected_free_bytes"] != _r90_projected_free_bytes(
                inventory_free_bytes=filesystem["free_bytes"],
                direct_free_bytes=strict_row["disk_free_bytes"],
                peak_bytes=peak,
                strict_scoped_bytes=capacity["strict_scoped_bytes"])):
        raise RuntimeError("R90 strict capacity measurement does not support the projected-free result")
    strict_document = evidence_files["strict_capacity_measurement"][0]
    if (strict_document.get("status") != "PASS"
            or strict_document.get("strict_inventory_matches") is not True
            or strict_document.get("disk_free_bytes") != strict_row["disk_free_bytes"]
            or strict_document.get("strict_scoped_bytes") != capacity["strict_scoped_bytes"]
            or strict_document.get("inventory_sha256") != capacity["inventory_sha256"]):
        raise RuntimeError("R90 strict measurement contents do not support the capacity claims")
    if (not isinstance(inventory_totals, dict) or not isinstance(filesystem, dict)
            or inventory_totals.get("fgmetal_scoped_allocated_inode_bytes")
                != capacity["strict_scoped_bytes"]
            or rows["inventory"].get("strict_scoped_bytes") != capacity["strict_scoped_bytes"]):
        raise RuntimeError("R90 inventory contents do not support the strict scoped measurement")
    reconciliation_row = rows.get("inventory_refresh_reconciliation")
    if (not isinstance(reconciliation_row, dict)
            or reconciliation_row.get("status") != "PASS"
            or reconciliation_row.get("difference_bytes") != 0
            or reconciliation_row.get("sha256")
                != capacity.get("inventory_refresh_reconciliation_sha256")):
        raise RuntimeError("R90 inventory refresh reconciliation does not match the capacity review")
    reconciliation_document = evidence_files["inventory_refresh_reconciliation"][0]
    if (reconciliation_document.get("status") != "PASS"
            or reconciliation_document.get("difference_bytes") != 0
            or reconciliation_document.get("inventory_sha256") != capacity["inventory_sha256"]
            or reconciliation_document.get("strict_scoped_bytes")
                != capacity["strict_scoped_bytes"]):
        raise RuntimeError("R90 reconciliation contents do not prove an exact inventory refresh")

    expected_manifest_path = manifest_envelope.get("path") if isinstance(manifest_envelope, dict) else None
    _validate_r90_reviewer_evidence(
        {name: evidence_files[name][0] for name in ("correctness_safety", "provenance")},
        candidate_id=candidate_id,
        candidate_core_sha256=candidate_core_sha256,
        expected_manifest_path=expected_manifest_path,
        correctness_checks=correctness_checks,
        provenance_checks=provenance_checks,
        reviewer_identities=reviewer_identities)

    index_document = evidence_files["evidence_index"][0]
    index_entries = index_document.get("evidence")
    if (set(index_document) != {
            "status", "run_id", "candidate_id", "candidate_core_sha256",
            "plan_raw_sha256", "plan_canonical_sha256", "evidence"}
            or index_document.get("status") != "PASS"
            or index_document.get("candidate_id") != candidate_id
            or not isinstance(index_entries, list) or not index_entries
            or len(index_entries) != rows["evidence_index"]["rows"]
            or len(index_entries) != len(rows) - 1
            or any(not isinstance(entry, dict)
                   or not isinstance(entry.get("path"), str) or not entry["path"]
                   or not isinstance(entry.get("role"), str) or not entry["role"]
                   or not isinstance(entry.get("sha256"), str)
                   or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]) is None
                   or type(entry.get("size_bytes")) is not int or entry["size_bytes"] <= 0
                   for entry in index_entries)):
        raise RuntimeError("R90 evidence index contents are empty or malformed")
    expected_index_rows = {
        name: {"path": row["path"], "sha256": row["sha256"],
               "size_bytes": row["size_bytes"]}
        for name, row in rows.items() if name != "evidence_index"
    }
    indexed: dict[str, dict[str, object]] = {}
    for entry in index_entries:
        role = entry["role"]
        if role in indexed:
            raise RuntimeError("R90 evidence index contains duplicate evidence roles")
        indexed[role] = {key: entry[key] for key in ("path", "sha256", "size_bytes")}
    if indexed != expected_index_rows:
        raise RuntimeError("R90 evidence index does not exactly cover the reviewed evidence rows")
    ledger_row = rows["pre_gate_mutation_ledger"]
    ledger_document = evidence_files["pre_gate_mutation_ledger"][0]
    events = ledger_document.get("events")
    if (set(ledger_document) != {
            "status", "run_id", "candidate_id", "candidate_core_sha256",
            "plan_raw_sha256", "plan_canonical_sha256", "events"}
            or ledger_document.get("candidate_id") != candidate_id
            or ledger_document.get("status") != ledger_row.get("status")
            or ledger_document.get("status") != "PASS"
            or not isinstance(events, list)
            or {event.get("event") for event in events if isinstance(event, dict)}
                != mutation_events
            or len(events) != len(mutation_events)
            or any(not isinstance(event, dict)
                   or set(event) != {"event", "status", "details"}
                   or not isinstance(event.get("event"), str) or not event["event"].strip()
                   or event.get("status") != "PASS"
                   or not isinstance(event.get("details"), str) or not event["details"].strip()
                   for event in events)):
        raise RuntimeError("R90 mutation ledger contents do not support its claimed status")


def _assert_r89_gate_fresh_under_allocation_lock(
        *, expected_gate_sha256: str,
        supplemental_files: Mapping[str, tuple[Path, str, str]]) -> None:
    """Recheck the reviewed Gate and its R89 evidence while holding the shared lock."""
    try:
        gate_raw = storage_policy._read_file_nofollow(
            storage_policy.GATE0_ATTESTATION_PATH,
            maximum_bytes=storage_policy.GATE0_ATTESTATION_MAX_BYTES)
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeError("R89 Gate changed or became unavailable before admission") from error
    if hashlib.sha256(gate_raw).hexdigest() != expected_gate_sha256:
        raise RuntimeError("R89 Gate changed while the runner waited for the allocation lock")
    try:
        gate = json.loads(gate_raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("R89 Gate is malformed at the allocation-lock boundary") from error
    pins = gate.get("supplemental_evidence_sha256") if isinstance(gate, dict) else None
    if not isinstance(pins, dict):
        raise RuntimeError("R89 Gate lost its supplemental evidence bindings before admission")
    for name, (path, expected_relative, expected_sha256) in supplemental_files.items():
        if pins.get(name) != expected_sha256:
            raise RuntimeError(f"R89 Gate supplemental binding changed before admission: {name}")
        try:
            relative = path.relative_to(REPO).as_posix()
            raw = storage_policy._read_file_nofollow(path, maximum_bytes=R89_EVIDENCE_MAX_BYTES)
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError(f"R89 Gate-pinned evidence is unavailable before admission: {name}") from error
        if relative != expected_relative or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RuntimeError(f"R89 Gate-pinned evidence changed before admission: {name}")


def _assert_r89_plan_fresh_under_allocation_lock(
        *, plan_path: Path, expected_raw_sha256: str,
        expected_canonical_sha256: str, build_id: str,
        build_path: Path, expected_bytes: int) -> None:
    """Reparse and revalidate the exact signed plan at the allocation boundary."""
    try:
        raw = storage_policy._read_file_nofollow(plan_path, maximum_bytes=16 * 1024 * 1024)
        value = json.loads(raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("R89 execution plan changed or became unavailable before admission") from error
    if (not isinstance(value, dict)
            or hashlib.sha256(raw).hexdigest() != expected_raw_sha256):
        raise RuntimeError("R89 execution plan raw digest changed while waiting for allocation lock")
    try:
        normalized = build_retention._session_validate_plan(
            value, build_id=build_id, build_path=build_path,
            source_tree_hash=value["source_identity"]["sha256"],
            expected_bytes=expected_bytes)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise RuntimeError("R89 execution plan failed lock-time revalidation") from error
    _validate_r66_recovery_plan(normalized)
    observed_canonical_sha256 = hashlib.sha256(
        build_retention._canonical_json(normalized)).hexdigest()
    if observed_canonical_sha256 != expected_canonical_sha256:
        raise RuntimeError("R89 execution plan canonical digest changed while waiting for allocation lock")


def _assert_r89_candidate_evidence_fresh_under_allocation_lock(
        *, candidate_review_path: Path, expected_sha256: str) -> None:
    """Reparse the Gate-pinned review and rehash its evidence at admission time."""
    try:
        raw = storage_policy._read_file_nofollow(
            candidate_review_path, maximum_bytes=4 * 1024 * 1024)
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeError("R89 candidate review changed or became unavailable before admission") from error
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError("R89 candidate review changed while waiting for allocation lock")
    try:
        candidate_review = json.loads(
            raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("R89 candidate review is malformed at the allocation-lock boundary") from error
    if (not isinstance(candidate_review, dict)
            or set(candidate_review) != R89_CANDIDATE_G0C_FIELDS
            or candidate_review.get("candidate_id")
                != "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10"):
        raise RuntimeError("R89 candidate review has an unexpected lock-time schema")
    _validate_r89_review_evidence(candidate_review)



def _assert_r90_gate_fresh_under_allocation_lock(
        *, expected_gate_sha256: str,
        supplemental_files: Mapping[str, tuple[Path, str, str]]) -> None:
    """Recheck the reviewed Gate and its R90 evidence while holding the shared lock."""
    try:
        gate_raw = storage_policy._read_file_nofollow(
            storage_policy.GATE0_ATTESTATION_PATH,
            maximum_bytes=storage_policy.GATE0_ATTESTATION_MAX_BYTES)
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeError("R90 Gate changed or became unavailable before admission") from error
    if hashlib.sha256(gate_raw).hexdigest() != expected_gate_sha256:
        raise RuntimeError("R90 Gate changed while the runner waited for the allocation lock")
    try:
        gate = json.loads(gate_raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("R90 Gate is malformed at the allocation-lock boundary") from error
    pins = gate.get("supplemental_evidence_sha256") if isinstance(gate, dict) else None
    if not isinstance(pins, dict):
        raise RuntimeError("R90 Gate lost its supplemental evidence bindings before admission")
    for name, (path, expected_relative, expected_sha256) in supplemental_files.items():
        if pins.get(name) != expected_sha256:
            raise RuntimeError(f"R90 Gate supplemental binding changed before admission: {name}")
        try:
            relative = path.relative_to(REPO).as_posix()
            raw = storage_policy._read_file_nofollow(path, maximum_bytes=R90_EVIDENCE_MAX_BYTES)
        except (OSError, ValueError, RuntimeError) as error:
            raise RuntimeError(f"R90 Gate-pinned evidence is unavailable before admission: {name}") from error
        if relative != expected_relative or hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise RuntimeError(f"R90 Gate-pinned evidence changed before admission: {name}")

def _assert_r90_plan_fresh_under_allocation_lock(
        *, plan_path: Path, expected_raw_sha256: str,
        expected_canonical_sha256: str, build_id: str,
        build_path: Path, expected_bytes: int) -> None:
    """Reparse and revalidate the exact signed plan at the allocation boundary."""
    try:
        raw = storage_policy._read_file_nofollow(plan_path, maximum_bytes=16 * 1024 * 1024)
        value = json.loads(raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("R90 execution plan changed or became unavailable before admission") from error
    if (not isinstance(value, dict)
            or hashlib.sha256(raw).hexdigest() != expected_raw_sha256):
        raise RuntimeError("R90 execution plan raw digest changed while waiting for allocation lock")
    try:
        normalized = build_retention._session_validate_plan(
            value, build_id=build_id, build_path=build_path,
            source_tree_hash=value["source_identity"]["sha256"],
            expected_bytes=expected_bytes)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        raise RuntimeError("R90 execution plan failed lock-time revalidation") from error
    _validate_r66_recovery_plan(normalized)
    observed_canonical_sha256 = hashlib.sha256(
        build_retention._canonical_json(normalized)).hexdigest()
    if observed_canonical_sha256 != expected_canonical_sha256:
        raise RuntimeError("R90 execution plan canonical digest changed while waiting for allocation lock")

def _assert_r90_candidate_evidence_fresh_under_allocation_lock(
        *, candidate_review_path: Path, expected_sha256: str,
        candidate_id: str = R90_CANDIDATE_ID,
        behavior_verdicts: set[str] = R90_BEHAVIOR_VERDICTS,
        required_rows: Mapping[str, str | None] = R90_REQUIRED_VALIDATION_EVIDENCE,
        evidence_basenames: Mapping[str, re.Pattern[str]] = R90_EVIDENCE_BASENAMES,
        run_prefix: str = "R90",
        correctness_checks: set[str] = R90_CORRECTNESS_SAFETY_CHECK_NAMES,
        provenance_checks: set[str] = R90_PROVENANCE_CHECK_NAMES,
        reviewer_identities: Mapping[str, tuple[str, str]] = R90_REVIEWER_IDENTITIES,
        mutation_events: set[str] = R90_MUTATION_LEDGER_EVENTS) -> None:
    """Reparse the active R90 candidate review and rehash its evidence at admission time."""
    try:
        raw = storage_policy._read_file_nofollow(
            candidate_review_path, maximum_bytes=4 * 1024 * 1024)
    except (OSError, ValueError, RuntimeError) as error:
        raise RuntimeError("R90 candidate review changed or became unavailable before admission") from error
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise RuntimeError("R90 candidate review changed while waiting for allocation lock")
    try:
        candidate_review = json.loads(
            raw, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("R90 candidate review is malformed at the allocation-lock boundary") from error
    if (not isinstance(candidate_review, dict)
            or set(candidate_review) != R90_CANDIDATE_G0C_FIELDS
            or candidate_review.get("candidate_id") != candidate_id):
        raise RuntimeError("R90 candidate review has an unexpected lock-time schema")
    _validate_r90_review_evidence(
        candidate_review, behavior_verdicts=behavior_verdicts,
        required_rows=required_rows, evidence_basenames=evidence_basenames,
        run_prefix=run_prefix, correctness_checks=correctness_checks,
        provenance_checks=provenance_checks, reviewer_identities=reviewer_identities,
        mutation_events=mutation_events)



def _validate_r87_fresh_g0c_answer(*, descriptor: Mapping[str, object],
                                   answer: Mapping[str, object],
                                   report_sha256: str, candidate_id: str,
                                   candidate_core_sha256: str,
                                   plan_canonical_sha256: str) -> None:
    """Keep the R88 Candidate 5 G0C answer separate from accepted Candidate 4."""
    if candidate_id == "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-8":
        expected_path = R89_CANDIDATE_8_FRESH_G0C_REVIEW_PATH
    elif candidate_id == "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-9":
        expected_path = R89_CANDIDATE_9_FRESH_G0C_REVIEW_PATH
    elif candidate_id == "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10":
        expected_path = R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R90_CANDIDATE_13_ID:
        expected_path = R90_CANDIDATE_13_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R90_CANDIDATE_14_ID:
        expected_path = R90_CANDIDATE_14_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R90_CANDIDATE_15_ID:
        expected_path = R90_CANDIDATE_15_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R90_CANDIDATE_16_ID:
        expected_path = R90_CANDIDATE_16_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R90_CANDIDATE_17_ID:
        expected_path = R90_CANDIDATE_17_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R90_CANDIDATE_18_ID:
        expected_path = R90_CANDIDATE_18_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R90_CANDIDATE_ID:
        expected_path = R90_CANDIDATE_19_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R91_CANDIDATE_ID:
        expected_path = R91_CANDIDATE_21_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R92_CANDIDATE_ID:
        expected_path = R92_CANDIDATE_23_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R93_CANDIDATE_ID:
        expected_path = R93_CANDIDATE_24_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R94_CANDIDATE_ID:
        expected_path = R94_CANDIDATE_25_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R95_CANDIDATE_ID:
        expected_path = R95_CANDIDATE_26_FRESH_G0C_REVIEW_PATH
    elif candidate_id == R96_CANDIDATE_ID:
        expected_path = R96_CANDIDATE_27_FRESH_G0C_REVIEW_PATH
    elif candidate_id in {
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-5",
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-6",
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-7"}:
        expected_path = R87_CANDIDATE_5_FRESH_G0C_REVIEW_PATH
    elif (isinstance(candidate_id, str)
          and r6a_dependency_manifest.R87_CANDIDATE_ID_PATTERN.fullmatch(candidate_id)):
        expected_path = R87_FRESH_G0C_REVIEW_PATH
    else:
        raise RuntimeError("R87 G0C answer has no exact candidate route")
    if (descriptor.get("path") != expected_path
            or answer.get("milestone") != "R87"
            or answer.get("status") != "PASS"
            or answer.get("answer") != "NO"
            or answer.get("findings") != []
            or report_sha256 != descriptor.get("sha256")):
        raise RuntimeError("Fresh R87 G0C answer changed or lacks an explicit passing NO")
    if candidate_id in {
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-5",
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-6",
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-7",
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-9",
            "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10",
            R90_CANDIDATE_13_ID,
            R90_CANDIDATE_14_ID,
            R90_CANDIDATE_15_ID,
            R90_CANDIDATE_16_ID,
            R90_CANDIDATE_17_ID,
            R90_CANDIDATE_18_ID,
            R90_CANDIDATE_ID,
            R91_CANDIDATE_ID,
            R92_CANDIDATE_ID,
            R93_CANDIDATE_ID,
            R94_CANDIDATE_ID, R95_CANDIDATE_ID, R96_CANDIDATE_ID} and (
            answer.get("candidate_id") != candidate_id
            or answer.get("candidate_core_sha256") != candidate_core_sha256
            or answer.get("plan_canonical_sha256") != plan_canonical_sha256):
        raise RuntimeError("Fresh R88 G0C answer does not bind the exact candidate and plan")


def _validate_r89_g0c_reviews(*, global_review_bytes: bytes,
                              candidate_review_bytes: bytes,
                              gate_attestation: Mapping[str, object],
                              repo: Path, plan_path: Path, plan_bytes: bytes,
                              plan_canonical_sha256: str,
                              validated_plan: Mapping[str, object]) -> dict[str, object]:
    """Validate the launcher-wide and Candidate 10 G0C reviews as separate roles."""
    if not isinstance(global_review_bytes, bytes):
        raise RuntimeError("R89 global launcher G0C record is missing")
    if not isinstance(candidate_review_bytes, bytes):
        raise RuntimeError("R89 candidate-specific G0C record is missing")
    try:
        global_review, global_blockers = storage_policy.parse_g0c_reviewed_files(
            global_review_bytes, repo, storage_policy.R89_G0C_REVIEW_REQUIRED_FILES)
    except (TypeError, ValueError, RuntimeError) as error:
        raise RuntimeError(f"R89 global launcher G0C record is malformed: {error}") from error
    if global_blockers:
        raise RuntimeError("R89 global launcher G0C review failed: " + "; ".join(global_blockers))
    if any(re.fullmatch(r"r[0-9]+_binding", key) for key in global_review):
        raise RuntimeError("R89 global launcher G0C record contains a candidate binding")
    if (set(global_review) != R89_GLOBAL_G0C_FIELDS
            or global_review.get("schema_version") != 1
            or not isinstance(global_review.get("review_scope"), str)
            or not global_review["review_scope"].strip()
            or not isinstance(global_review.get("limitations"), list)
            or any(not isinstance(item, str) or not item.strip()
                   for item in global_review["limitations"])):
        raise RuntimeError(
            "R89 global launcher G0C record does not match the exact generic top-level schema")

    global_sha256 = hashlib.sha256(global_review_bytes).hexdigest()
    candidate_review_sha256 = hashlib.sha256(candidate_review_bytes).hexdigest()
    supplemental = gate_attestation.get("supplemental_evidence_sha256")
    if not isinstance(supplemental, Mapping):
        raise RuntimeError("R89 Gate attestation lacks supplemental evidence bindings")
    if supplemental.get(Path(R89_LAUNCHER_G0C_REVIEW_PATH).name) != global_sha256:
        raise RuntimeError("R89 Gate does not bind the exact global launcher G0C review")
    if supplemental.get(Path(R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH).name) != candidate_review_sha256:
        raise RuntimeError("R89 Gate does not bind the exact candidate-specific G0C review")

    try:
        candidate_review = json.loads(
            candidate_review_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"R89 candidate-specific G0C record is malformed: {error}") from error
    if (not isinstance(candidate_review, dict)
            or set(candidate_review) != R89_CANDIDATE_G0C_FIELDS
            or candidate_review.get("schema_version") != 1
            or candidate_review.get("milestone") != "R87"
            or candidate_review.get("status") != "PASS"
            or candidate_review.get("answer") != "NO"
            or candidate_review.get("findings") != []
            or candidate_review.get("question")
                != "Can durable SUCCESS exist without a validated committed manifest?"):
        raise RuntimeError("R89 candidate-specific G0C record has an unexpected schema or is not PASS/NO")
    if any(re.fullmatch(r"r[0-9]+_binding", key) and key != "r87_binding"
           for key in candidate_review):
        raise RuntimeError("R89 candidate-specific G0C mixes historical/current binding roles")
    selected_repo = repo.resolve(strict=True)
    selected_plan = plan_path.resolve(strict=True)
    try:
        plan_relative = selected_plan.relative_to(selected_repo).as_posix()
    except ValueError as error:
        raise RuntimeError("R89 plan escapes the repository") from error
    if (selected_plan.name != R87_CANDIDATE_8_PLAN_NAME
            or plan_relative != f"experiments/storage/{R87_CANDIDATE_8_PLAN_NAME}"
            or storage_policy._read_file_nofollow(
                selected_plan, maximum_bytes=16 * 1024 * 1024) != plan_bytes):
        raise RuntimeError("R89 candidate-specific G0C is routed to a different plan")
    if (candidate_review.get("plan_path") != plan_relative
            or candidate_review.get("plan_raw_sha256") != hashlib.sha256(plan_bytes).hexdigest()
            or candidate_review.get("plan_canonical_sha256") != plan_canonical_sha256):
        raise RuntimeError("R89 candidate-specific G0C does not bind the exact plan bytes")

    graph_rows = validated_plan.get("build_graphs")
    graph_pair = ([row.get("graph_id") for row in graph_rows if isinstance(row, Mapping)]
                  if isinstance(graph_rows, list) else None)
    graph_key = validated_plan.get("build_configuration_key_sha256")
    if (graph_pair != list(build_retention.BUILD_SESSION_DUAL_GRAPH_IDS)
            or candidate_review.get("graph_pair") != graph_pair
            or candidate_review.get("graph_pair_key") != graph_key):
        raise RuntimeError("R89 candidate-specific G0C does not bind the ordered graph pair")

    candidate_id = "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10"
    candidate_path_name = candidate_review.get("candidate_path")
    if (candidate_review.get("candidate_id") != candidate_id
            or not isinstance(candidate_path_name, str)
            or R87_CANDIDATE_PATH_PATTERN.fullmatch(candidate_path_name) is None
            or candidate_path_name != f"{candidate_id}.json"):
        raise RuntimeError("R89 candidate-specific G0C does not name exact Candidate 10")
    candidate_path, candidate_bytes = _repo_relative_file(
        selected_repo, candidate_path_name, label="R89 candidate", maximum_bytes=4 * 1024 * 1024)
    try:
        candidate = json.loads(candidate_bytes,
                               object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"R89 frozen candidate is malformed: {error}") from error
    if not isinstance(candidate, dict):
        raise RuntimeError("R89 frozen candidate is not an object")
    candidate_lineage_fields = {
        key for key in candidate if re.fullmatch(r"r[0-9]+_binding", key)}
    if candidate_lineage_fields != {
            "r80_binding", "r81_binding", "r86_binding", "r87_binding"}:
        raise RuntimeError("R89 candidate omits or adds an R80/R81/R86/R87 lineage binding")
    candidate_raw_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    candidate_core_sha256 = r6a_dependency_manifest._candidate_core_sha256(candidate)
    candidate_canonical_sha256 = hashlib.sha256(
        build_retention._canonical_json(candidate)).hexdigest()
    if (candidate.get("schema_version") != 1
            or candidate.get("milestone") != "R87"
            or candidate.get("candidate_id") != candidate_id
            or candidate.get("status") != "FROZEN_FOR_ACCEPTANCE_VALIDATION"
            or candidate_review.get("candidate_core_sha256") != candidate_core_sha256):
        raise RuntimeError("R89 candidate-specific G0C does not bind the exact candidate core")
    expected_plan = {
        "path": plan_relative,
        "raw_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "canonical_sha256": plan_canonical_sha256,
        "graph_pair_key": graph_key,
    }
    if (candidate.get("execution_plan") != expected_plan
            or candidate.get("graph_pair_key") != graph_key):
        raise RuntimeError("R89 candidate does not bind the exact plan and graph pair")

    exact_diff = candidate.get("exact_diff")
    if (not isinstance(exact_diff, dict)
            or set(exact_diff) != {"path", "sha256", "size_bytes"}
            or candidate_review.get("exact_diff") != exact_diff):
        raise RuntimeError("R89 candidate-specific G0C does not bind the exact diff descriptor")
    _diff_path, diff_bytes = _repo_relative_file(
        selected_repo, exact_diff.get("path"), label="R89 candidate diff",
        maximum_bytes=32 * 1024 * 1024)
    diff_sha256 = hashlib.sha256(diff_bytes).hexdigest()
    if diff_sha256 != exact_diff.get("sha256") or len(diff_bytes) != exact_diff.get("size_bytes"):
        raise RuntimeError("R89 candidate diff changed after review")

    changed_source_hashes = candidate.get("changed_source_hashes")
    changed_test_hashes = candidate.get("changed_test_hashes")
    reviewed_files = candidate_review.get("reviewed_files")
    if (not isinstance(changed_source_hashes, dict)
            or not isinstance(changed_test_hashes, dict)
            or set(changed_source_hashes) != R87_CHANGED_SOURCE_FILES
            or set(changed_test_hashes) != R87_CHANGED_TEST_FILES):
        raise RuntimeError("R89 candidate changed source/test maps do not match the fixed R87 roster")
    _validate_r89_review_evidence(candidate_review)
    if (not isinstance(changed_source_hashes, dict)
            or not isinstance(changed_test_hashes, dict)
            or not isinstance(reviewed_files, dict)
            or set(reviewed_files) != {"source", "tests"}
            or not isinstance(reviewed_files.get("source"), dict)
            or not isinstance(reviewed_files.get("tests"), dict)
            or set(reviewed_files["source"]) != set(changed_source_hashes)
            or set(reviewed_files["tests"]) != set(changed_test_hashes)):
        raise RuntimeError("R89 candidate G0C source/test roster differs from the frozen candidate")
    for role, expected_hashes in (("source", changed_source_hashes),
                                  ("tests", changed_test_hashes)):
        records = reviewed_files[role]
        for relative, expected_sha256 in expected_hashes.items():
            record = records.get(relative)
            if (not isinstance(record, dict) or set(record) != {"sha256", "size_bytes"}
                    or not isinstance(expected_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
                raise RuntimeError(f"R89 candidate G0C {role} binding is malformed: {relative}")
            _path, raw = _repo_relative_file(
                selected_repo, relative, label=f"R89 reviewed {role} input",
                maximum_bytes=32 * 1024 * 1024)
            digest = hashlib.sha256(raw).hexdigest()
            if (digest != expected_sha256 or record.get("sha256") != digest
                    or record.get("size_bytes") != len(raw)):
                raise RuntimeError(f"R89 candidate G0C {role} binding is stale: {relative}")

    identity_review = candidate_review.get("identity_review")
    if (not isinstance(identity_review, dict)
            or set(identity_review) != {
                "changed_source_bindings", "changed_test_bindings", "diff", "plan",
                "manifest_route"}
            or identity_review.get("changed_source_bindings") != reviewed_files["source"]
            or identity_review.get("changed_test_bindings") != reviewed_files["tests"]):
        raise RuntimeError("R89 candidate identity review disagrees with its exact source/test map")

    global_descriptor = candidate_review.get("global_launcher_g0c")
    if (not isinstance(global_descriptor, dict)
            or set(global_descriptor) != {
                "findings", "gate_attestation_pinned_sha256", "gate_attestation_status",
                "gate_pin_matches_current_global_review", "path", "reviewed_file_count",
                "sha256", "size_bytes", "status"}
            or global_descriptor.get("path") != R89_LAUNCHER_G0C_REVIEW_PATH
            or global_descriptor.get("sha256") != global_sha256
            or global_descriptor.get("status") != "PASS"
            or global_descriptor.get("findings") != []
            or global_descriptor.get("reviewed_file_count")
                != len(storage_policy.R89_G0C_REVIEW_REQUIRED_FILES)
            or global_descriptor.get("size_bytes") != len(global_review_bytes)):
        raise RuntimeError("R89 candidate G0C does not bind the exact global launcher review")

    r87_binding = candidate_review.get("r87_binding")
    if not isinstance(r87_binding, dict) or set(r87_binding) != {
            "candidate", "execution_dependency_manifest"}:
        raise RuntimeError("R89 candidate G0C lacks the exact R87 binding envelope")
    candidate_descriptor = r87_binding.get("candidate")
    if candidate_descriptor != {
            "path": candidate_path_name,
            "candidate_id": candidate_id,
            "candidate_core_sha256": candidate_core_sha256}:
        raise RuntimeError("R89 candidate G0C binding does not match Candidate 10 core")
    manifest_descriptor = r87_binding.get("execution_dependency_manifest")
    expected_manifest_path = r6a_dependency_manifest.manifest_name_for_candidate(candidate_id)
    if (not isinstance(manifest_descriptor, dict)
            or set(manifest_descriptor) != {"path", "sha256"}
            or manifest_descriptor.get("path") != expected_manifest_path
            or not isinstance(manifest_descriptor.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_descriptor["sha256"]) is None
            or supplemental.get(expected_manifest_path) != manifest_descriptor.get("sha256")):
        raise RuntimeError("R89 Gate or candidate G0C does not bind the exact dependency manifest")
    manifest_path = selected_repo / expected_manifest_path
    manifest_bytes = storage_policy._read_file_nofollow(manifest_path, maximum_bytes=32 * 1024 * 1024)
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_descriptor["sha256"]:
        raise RuntimeError("R89 dependency manifest changed after Gate")
    if candidate.get("r87_binding") != {
            "execution_dependency_manifest": dict(manifest_descriptor)}:
        raise RuntimeError("R89 candidate does not carry the exact dependency-manifest envelope")
    _validate_r87_transaction_contracts(candidate, repo=selected_repo)
    preserved_r86_path = selected_repo / R87_R86_G0C_PRESERVED_PATH
    preserved_r86_bytes = storage_policy._read_file_nofollow(
        preserved_r86_path, maximum_bytes=12 * 1024 * 1024)
    if (not isinstance(candidate.get("preserved_r86_g0c_sha256"), str)
            or hashlib.sha256(preserved_r86_bytes).hexdigest()
                != candidate.get("preserved_r86_g0c_sha256")):
        raise RuntimeError("R89 candidate does not preserve the exact prior active R86 G0C record")
    try:
        preserved_r86 = json.loads(
            preserved_r86_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Preserved R86 G0C review is malformed: {error}") from error
    if (not isinstance(preserved_r86, dict)
            or preserved_r86.get("status") != "PASS"
            or preserved_r86.get("findings") != []
            or not {"r78_binding", "r79_binding", "r80_binding",
                    "r81_binding", "r86_binding"}.issubset(preserved_r86)
            or candidate.get("r80_binding") != preserved_r86.get("r80_binding")
            or candidate.get("r81_binding") != preserved_r86.get("r81_binding")
            or candidate.get("r86_binding") != preserved_r86.get("r86_binding")):
        raise RuntimeError("R89 candidate changed the preserved R80/R81/R86 review lineage")
    try:
        dependency_identity = r6a_dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=candidate_review,
            candidate_path=candidate_path_name,
            manifest_sha256=manifest_descriptor["sha256"])
    except r6a_dependency_manifest.DependencyManifestError as error:
        raise RuntimeError(f"R89 candidate/G0C binding is invalid: {error}") from error
    identity_review = candidate_review["identity_review"]
    manifest_route = identity_review.get("manifest_route")
    if (not isinstance(manifest_route, dict)
            or set(manifest_route) != {
                "status", "path", "current_review_is_final_manifest_closure", "rebuild_pending"}
            or manifest_route.get("status") != "PASS"
            or manifest_route.get("path") != expected_manifest_path
            or manifest_route.get("current_review_is_final_manifest_closure") is not True
            or manifest_route.get("rebuild_pending") is not False):
        raise RuntimeError("R89 candidate review does not report a final dependency-manifest closure")
    _validate_r87_fresh_g0c_answer(
        descriptor={"path": R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH,
                    "sha256": candidate_review_sha256},
        answer=candidate_review, report_sha256=candidate_review_sha256,
        candidate_id=candidate_id, candidate_core_sha256=candidate_core_sha256,
        plan_canonical_sha256=plan_canonical_sha256)

    completion_contract = candidate.get("completion_contract")
    if (not isinstance(completion_contract, dict)
            or set(completion_contract) != {
                "path", "sha256", "manifest_commit_contract_path",
                "manifest_commit_contract_sha256"}):
        raise RuntimeError("R89 candidate completion contract is malformed")
    for desc, label in (
            (completion_contract, "completion contract"),
            (candidate.get("crash_matrix"), "crash matrix")):
        if not isinstance(desc, dict) or not isinstance(desc.get("path"), str):
            raise RuntimeError(f"R89 candidate {label} descriptor is malformed")
        _path, raw = _repo_relative_file(selected_repo, desc["path"], label=f"R89 {label}")
        if hashlib.sha256(raw).hexdigest() != desc.get("sha256"):
            raise RuntimeError(f"R89 candidate {label} changed after review")
    _manifest_contract_path, manifest_contract_bytes = _repo_relative_file(
        selected_repo, completion_contract["manifest_commit_contract_path"],
        label="R89 manifest commit contract")
    if hashlib.sha256(manifest_contract_bytes).hexdigest() != \
            completion_contract["manifest_commit_contract_sha256"]:
        raise RuntimeError("R89 manifest commit contract changed after review")
    try:
        manifest_identity = r6a_dependency_manifest.validate_manifest(
            manifest_path=manifest_path, expected_sha256=manifest_descriptor["sha256"],
            candidate=candidate, plan=validated_plan,
            plan_raw_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            plan_canonical_sha256=plan_canonical_sha256,
            g0c_sha256=dependency_identity["g0c_sha256"],
            candidate_path=candidate_path.relative_to(selected_repo).as_posix(),
            plan_path=plan_relative, repo=selected_repo,
            gate_control_roster=gate_attestation.get("retention_control_sha256"))
    except r6a_dependency_manifest.DependencyManifestError as error:
        raise RuntimeError(f"R89 dependency-manifest closure is invalid: {error}") from error
    if (manifest_identity.get("sha256") != manifest_descriptor["sha256"]
            or manifest_identity.get("candidate_core_sha256") != candidate_core_sha256
            or manifest_identity.get("g0c_sha256") != dependency_identity["g0c_sha256"]):
        raise RuntimeError("R89 final manifest closure does not match the candidate and G0C cores")
    return {
        "binding_sha256": hashlib.sha256(
            build_retention._canonical_json(candidate_review["r87_binding"])).hexdigest(),
        "contract_sha256": completion_contract["sha256"],
        "manifest_commit_contract_sha256": completion_contract["manifest_commit_contract_sha256"],
        "candidate_sha256": candidate_raw_sha256,
        "candidate_canonical_sha256": candidate_canonical_sha256,
        "candidate_diff_sha256": diff_sha256,
        "g0c_report_sha256": candidate_review_sha256,
        "completion_contract_path": str(selected_repo / completion_contract["path"]),
        "crash_matrix_path": str(selected_repo / candidate["crash_matrix"]["path"]),
        "g0c_report_path": str(selected_repo / R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH),
        "completion_review_path": str(selected_repo / R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH),
        "manifest_commit_contract_path": str(
            selected_repo / completion_contract["manifest_commit_contract_path"]),
        "candidate_path": str(candidate_path),
        "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "candidate_raw_sha256": candidate_raw_sha256,
        "candidate_id": candidate_id,
        "candidate_core_sha256": candidate_core_sha256,
        "plan_canonical_sha256": plan_canonical_sha256,
        "dependency_manifest": dict(manifest_descriptor),
        "g0c_core_sha256": manifest_identity["g0c_sha256"],
    }
    if candidate_id in {"R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-8",
                        "R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-10",
                        R90_CANDIDATE_ID} and (
            answer.get("candidate_id") != candidate_id
            or answer.get("candidate_core_sha256") != candidate_core_sha256
            or answer.get("plan_canonical_sha256") != plan_canonical_sha256):
        raise RuntimeError("Fresh R89 G0C answer does not bind the exact candidate and plan")



def _validate_r90_g0c_reviews(*, global_review_bytes: bytes,
                              candidate_review_bytes: bytes,
                              gate_attestation: Mapping[str, object],
                              repo: Path, plan_path: Path, plan_bytes: bytes,
                              plan_canonical_sha256: str,
                              validated_plan: Mapping[str, object],
                              candidate_id: str = R90_CANDIDATE_ID,
                              candidate_review_path: str = R90_CANDIDATE_19_FRESH_G0C_REVIEW_PATH,
                              plan_name: str = R90_PLAN_NAME,
                              launcher_review_path: str = R90_LAUNCHER_G0C_REVIEW_PATH,
                              changed_source_files: set[str] = R90_CHANGED_SOURCE_FILES,
                              changed_test_files: set[str] = R90_CHANGED_TEST_FILES,
                              behavior_verdicts: set[str] = R90_BEHAVIOR_VERDICTS,
                              required_rows: Mapping[str, str | None] = R90_REQUIRED_VALIDATION_EVIDENCE,
                              evidence_basenames: Mapping[str, re.Pattern[str]] = R90_EVIDENCE_BASENAMES,
                              run_prefix: str = "R90",
                              correctness_checks: set[str] = R90_CORRECTNESS_SAFETY_CHECK_NAMES,
                              provenance_checks: set[str] = R90_PROVENANCE_CHECK_NAMES,
                              reviewer_identities: Mapping[str, tuple[str, str]] = R90_REVIEWER_IDENTITIES,
                              mutation_events: set[str] = R90_MUTATION_LEDGER_EVENTS
                              ) -> dict[str, object]:
    """Validate the launcher-wide and active candidate G0C reviews as separate roles."""
    if not isinstance(global_review_bytes, bytes):
        raise RuntimeError("R90 global launcher G0C record is missing")
    if not isinstance(candidate_review_bytes, bytes):
        raise RuntimeError("R90 candidate-specific G0C record is missing")
    try:
        global_review, global_blockers = storage_policy.parse_g0c_reviewed_files(
            global_review_bytes, repo, storage_policy.G0C_REVIEW_REQUIRED_FILES)
    except (TypeError, ValueError, RuntimeError) as error:
        raise RuntimeError(f"R90 global launcher G0C record is malformed: {error}") from error
    if global_blockers:
        raise RuntimeError("R90 global launcher G0C review failed: " + "; ".join(global_blockers))
    if any(re.fullmatch(r"r[0-9]+_binding", key) for key in global_review):
        raise RuntimeError("R90 global launcher G0C record contains a candidate binding")
    if (set(global_review) != R90_GLOBAL_G0C_FIELDS
            or global_review.get("schema_version") != 1
            or not isinstance(global_review.get("review_scope"), str)
            or not global_review["review_scope"].strip()
            or not isinstance(global_review.get("limitations"), list)
            or any(not isinstance(item, str) or not item.strip()
                   for item in global_review["limitations"])):
        raise RuntimeError(
            "R90 global launcher G0C record does not match the exact generic top-level schema")

    global_sha256 = hashlib.sha256(global_review_bytes).hexdigest()
    candidate_review_sha256 = hashlib.sha256(candidate_review_bytes).hexdigest()
    supplemental = gate_attestation.get("supplemental_evidence_sha256")
    if not isinstance(supplemental, Mapping):
        raise RuntimeError("R90 Gate attestation lacks supplemental evidence bindings")
    if supplemental.get(Path(launcher_review_path).name) != global_sha256:
        raise RuntimeError("R90 Gate does not bind the exact global launcher G0C review")
    if supplemental.get(Path(candidate_review_path).name) != candidate_review_sha256:
        raise RuntimeError("R90 Gate does not bind the exact candidate-specific G0C review")

    try:
        candidate_review = json.loads(
            candidate_review_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"R90 candidate-specific G0C record is malformed: {error}") from error
    if (not isinstance(candidate_review, dict)
            or set(candidate_review) != R90_CANDIDATE_G0C_FIELDS
            or candidate_review.get("schema_version") != 1
            or candidate_review.get("milestone") != "R87"
            or candidate_review.get("status") != "PASS"
            or candidate_review.get("answer") != "NO"
            or candidate_review.get("findings") != []
            or candidate_review.get("question")
                != "Can durable SUCCESS exist without a validated committed manifest?"):
        raise RuntimeError("R90 candidate-specific G0C record has an unexpected schema or is not PASS/NO")
    if any(re.fullmatch(r"r[0-9]+_binding", key) and key != "r87_binding"
           for key in candidate_review):
        raise RuntimeError("R90 candidate-specific G0C mixes historical/current binding roles")
    selected_repo = repo.resolve(strict=True)
    selected_plan = plan_path.resolve(strict=True)
    try:
        plan_relative = selected_plan.relative_to(selected_repo).as_posix()
    except ValueError as error:
        raise RuntimeError("R90 plan escapes the repository") from error
    if (selected_plan.name != plan_name
            or plan_relative != f"experiments/storage/{plan_name}"
            or storage_policy._read_file_nofollow(
                selected_plan, maximum_bytes=16 * 1024 * 1024) != plan_bytes):
        raise RuntimeError("R90 candidate-specific G0C is routed to a different plan")
    if (candidate_review.get("plan_path") != plan_relative
            or candidate_review.get("plan_raw_sha256") != hashlib.sha256(plan_bytes).hexdigest()
            or candidate_review.get("plan_canonical_sha256") != plan_canonical_sha256):
        raise RuntimeError("R90 candidate-specific G0C does not bind the exact plan bytes")

    graph_rows = validated_plan.get("build_graphs")
    graph_pair = ([row.get("graph_id") for row in graph_rows if isinstance(row, Mapping)]
                  if isinstance(graph_rows, list) else None)
    graph_key = validated_plan.get("build_configuration_key_sha256")
    if (graph_pair != list(build_retention.BUILD_SESSION_DUAL_GRAPH_IDS)
            or candidate_review.get("graph_pair") != graph_pair
            or candidate_review.get("graph_pair_key") != graph_key):
        raise RuntimeError("R90 candidate-specific G0C does not bind the ordered graph pair")

    candidate_path_name = candidate_review.get("candidate_path")
    if (candidate_review.get("candidate_id") != candidate_id
            or not isinstance(candidate_path_name, str)
            or R87_CANDIDATE_PATH_PATTERN.fullmatch(candidate_path_name) is None
            or candidate_path_name != f"{candidate_id}.json"):
        raise RuntimeError("R90 candidate-specific G0C does not name the exact active candidate")
    candidate_path, candidate_bytes = _repo_relative_file(
        selected_repo, candidate_path_name, label="R90 candidate", maximum_bytes=4 * 1024 * 1024)
    try:
        candidate = json.loads(candidate_bytes,
                               object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"R90 frozen candidate is malformed: {error}") from error
    if not isinstance(candidate, dict):
        raise RuntimeError("R90 frozen candidate is not an object")
    candidate_lineage_fields = {
        key for key in candidate if re.fullmatch(r"r[0-9]+_binding", key)}
    if candidate_lineage_fields != {
            "r80_binding", "r81_binding", "r86_binding", "r87_binding"}:
        raise RuntimeError("R90 candidate omits or adds an R80/R81/R86/R87 lineage binding")
    candidate_raw_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    candidate_core_sha256 = r6a_dependency_manifest._candidate_core_sha256(candidate)
    candidate_canonical_sha256 = hashlib.sha256(
        build_retention._canonical_json(candidate)).hexdigest()
    if (candidate.get("schema_version") != 1
            or candidate.get("milestone") != "R87"
            or candidate.get("candidate_id") != candidate_id
            or candidate.get("status") != "FROZEN_FOR_ACCEPTANCE_VALIDATION"
            or candidate_review.get("candidate_core_sha256") != candidate_core_sha256):
        raise RuntimeError("R90 candidate-specific G0C does not bind the exact candidate core")
    expected_plan = {
        "path": plan_relative,
        "raw_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "canonical_sha256": plan_canonical_sha256,
        "graph_pair_key": graph_key,
    }
    if (candidate.get("execution_plan") != expected_plan
            or candidate.get("graph_pair_key") != graph_key):
        raise RuntimeError("R90 candidate does not bind the exact plan and graph pair")

    runner_map = candidate.get("runner_map")
    expected_contracts = candidate.get("transaction_contracts")
    _runner_path, runner_bytes = _repo_relative_file(
        selected_repo, "experiments/storage/run_r6a_execution.py",
        label="R90 runner map source")
    expected_runner_map = {
        "path": "experiments/storage/run_r6a_execution.py",
        "sha256": hashlib.sha256(runner_bytes).hexdigest(),
        "admission_output_root": R90_ADMISSION_OUTPUT_ROOT,
        "admission_output_contract": R90_ADMISSION_OUTPUT_CONTRACT,
        "admission_output_contract_path": R90_OUTPUT_ROOT_CONTRACT_PATH,
        "admission_disposition_contract_path": R90_DISPOSITION_CONTRACT_PATH,
        "historical_disposition_paths": sorted(R90_HISTORICAL_DISPOSITION_PATHS),
        "historical_attempt_evidence_paths": sorted(R90_HISTORICAL_ATTEMPT_EVIDENCE_PATHS),
    }
    if candidate_id == R91_CANDIDATE_ID:
        expected_runner_map.update({
            "workspace_transition_contract_path": R91_WORKSPACE_TRANSITION_CONTRACT_PATH,
            "failure_recovery_contract_path": R91_FAILURE_RECOVERY_CONTRACT_PATH,
        })
    if candidate_id == R92_CANDIDATE_ID:
        expected_runner_map["r92_handoff_contract_path"] = R92_HANDOFF_CONTRACT_PATH
    if candidate_id == R94_CANDIDATE_ID:
        expected_runner_map["r94_handoff_contract_path"] = R94_HANDOFF_CONTRACT_PATH
    if candidate_id == R96_CANDIDATE_ID:
        expected_runner_map["r96_handoff_contract_path"] = R96_HANDOFF_CONTRACT_PATH
    if candidate_id == R95_CANDIDATE_ID:
        expected_runner_map["r95_handoff_contract_path"] = R95_HANDOFF_CONTRACT_PATH
    if candidate_id == R93_CANDIDATE_ID:
        expected_runner_map["r93_handoff_contract_path"] = R93_HANDOFF_CONTRACT_PATH
    allowed_runner_map_keys = set(expected_runner_map) | {
        "admission_output_contract_sha256",
        "admission_disposition_contract_sha256"}
    if candidate_id == R91_CANDIDATE_ID:
        allowed_runner_map_keys |= {
            "workspace_transition_contract_sha256", "failure_recovery_contract_sha256"}
    if candidate_id == R92_CANDIDATE_ID:
        allowed_runner_map_keys.add("r92_handoff_contract_sha256")
    if candidate_id == R94_CANDIDATE_ID:
        allowed_runner_map_keys.add("r94_handoff_contract_sha256")
    if candidate_id == R96_CANDIDATE_ID:
        allowed_runner_map_keys.add("r96_handoff_contract_sha256")
    if candidate_id == R95_CANDIDATE_ID:
        allowed_runner_map_keys.add("r95_handoff_contract_sha256")
    if candidate_id == R93_CANDIDATE_ID:
        allowed_runner_map_keys.add("r93_handoff_contract_sha256")
    if (not isinstance(runner_map, dict)
            or set(runner_map) != allowed_runner_map_keys
            or any(runner_map.get(key) != value
                   for key, value in expected_runner_map.items())
            or not isinstance(expected_contracts, dict)
            or runner_map.get("admission_output_contract_sha256")
                != expected_contracts.get(R90_OUTPUT_ROOT_CONTRACT_PATH)
            or runner_map.get("admission_disposition_contract_sha256")
                != expected_contracts.get(R90_DISPOSITION_CONTRACT_PATH)):
        raise RuntimeError("R90 candidate runner map does not bind the exact output-root and disposition contracts")
    if candidate_id == R91_CANDIDATE_ID and (
            runner_map.get("workspace_transition_contract_sha256")
                != expected_contracts.get(R91_WORKSPACE_TRANSITION_CONTRACT_PATH)
            or runner_map.get("failure_recovery_contract_sha256")
                != expected_contracts.get(R91_FAILURE_RECOVERY_CONTRACT_PATH)):
        raise RuntimeError("R91 candidate runner map does not bind both lifecycle contracts")
    if candidate_id == R92_CANDIDATE_ID and (
            runner_map.get("r92_handoff_contract_sha256")
                != expected_contracts.get(R92_HANDOFF_CONTRACT_PATH)):
        raise RuntimeError("R92 candidate runner map does not bind the exact R71 handoff contract")
    if candidate_id == R93_CANDIDATE_ID and (
            runner_map.get("r93_handoff_contract_sha256")
                != expected_contracts.get(R93_HANDOFF_CONTRACT_PATH)):
        raise RuntimeError("R93 candidate runner map does not bind the exact R71 handoff contract")
    if candidate_id == R94_CANDIDATE_ID and (
            runner_map.get("r94_handoff_contract_sha256")
                != expected_contracts.get(R94_HANDOFF_CONTRACT_PATH)):
        raise RuntimeError("R94 candidate runner map does not bind the exact R71 handoff contract")
    if candidate_id == R95_CANDIDATE_ID and (
            runner_map.get("r95_handoff_contract_sha256")
                != expected_contracts.get(R95_HANDOFF_CONTRACT_PATH)):
        raise RuntimeError("R95 candidate runner map does not bind the exact R71 handoff contract")
    if candidate_id == R96_CANDIDATE_ID and (
            runner_map.get("r96_handoff_contract_sha256")
                != expected_contracts.get(R96_HANDOFF_CONTRACT_PATH)):
        raise RuntimeError("R96 candidate runner map does not bind the exact R71 handoff contract")

    exact_diff = candidate.get("exact_diff")
    if (not isinstance(exact_diff, dict)
            or set(exact_diff) != {"path", "sha256", "size_bytes"}
            or candidate_review.get("exact_diff") != exact_diff):
        raise RuntimeError("R90 candidate-specific G0C does not bind the exact diff descriptor")
    _diff_path, diff_bytes = _repo_relative_file(
        selected_repo, exact_diff.get("path"), label="R90 candidate diff",
        maximum_bytes=32 * 1024 * 1024)
    diff_sha256 = hashlib.sha256(diff_bytes).hexdigest()
    if diff_sha256 != exact_diff.get("sha256") or len(diff_bytes) != exact_diff.get("size_bytes"):
        raise RuntimeError("R90 candidate diff changed after review")

    changed_source_hashes = candidate.get("changed_source_hashes")
    changed_test_hashes = candidate.get("changed_test_hashes")
    reviewed_files = candidate_review.get("reviewed_files")
    if (not isinstance(changed_source_hashes, dict)
            or not isinstance(changed_test_hashes, dict)
            or set(changed_source_hashes) != changed_source_files
            or set(changed_test_hashes) != changed_test_files):
        raise RuntimeError("R90 candidate changed source/test maps do not match the fixed R87 roster")
    _validate_r90_review_evidence(
        candidate_review, behavior_verdicts=behavior_verdicts,
        required_rows=required_rows, evidence_basenames=evidence_basenames,
        run_prefix=run_prefix, correctness_checks=correctness_checks,
        provenance_checks=provenance_checks, reviewer_identities=reviewer_identities,
        mutation_events=mutation_events)
    if (not isinstance(changed_source_hashes, dict)
            or not isinstance(changed_test_hashes, dict)
            or not isinstance(reviewed_files, dict)
            or set(reviewed_files) != {"source", "tests"}
            or not isinstance(reviewed_files.get("source"), dict)
            or not isinstance(reviewed_files.get("tests"), dict)
            or set(reviewed_files["source"]) != set(changed_source_hashes)
            or set(reviewed_files["tests"]) != set(changed_test_hashes)):
        raise RuntimeError("R90 candidate G0C source/test roster differs from the frozen candidate")
    for role, expected_hashes in (("source", changed_source_hashes),
                                  ("tests", changed_test_hashes)):
        records = reviewed_files[role]
        for relative, expected_sha256 in expected_hashes.items():
            record = records.get(relative)
            if (not isinstance(record, dict) or set(record) != {"sha256", "size_bytes"}
                    or not isinstance(expected_sha256, str)
                    or re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None):
                raise RuntimeError(f"R90 candidate G0C {role} binding is malformed: {relative}")
            _path, raw = _repo_relative_file(
                selected_repo, relative, label=f"R90 reviewed {role} input",
                maximum_bytes=32 * 1024 * 1024)
            digest = hashlib.sha256(raw).hexdigest()
            if (digest != expected_sha256 or record.get("sha256") != digest
                    or record.get("size_bytes") != len(raw)):
                raise RuntimeError(f"R90 candidate G0C {role} binding is stale: {relative}")

    identity_review = candidate_review.get("identity_review")
    if (not isinstance(identity_review, dict)
            or set(identity_review) != {
                "changed_source_bindings", "changed_test_bindings", "diff", "plan",
                "manifest_route"}
            or identity_review.get("changed_source_bindings") != reviewed_files["source"]
            or identity_review.get("changed_test_bindings") != reviewed_files["tests"]):
        raise RuntimeError("R90 candidate identity review disagrees with its exact source/test map")

    global_descriptor = candidate_review.get("global_launcher_g0c")
    if (not isinstance(global_descriptor, dict)
            or set(global_descriptor) != {
                "findings", "gate_attestation_pinned_sha256", "gate_attestation_status",
                "gate_pin_matches_current_global_review", "path", "reviewed_file_count",
                "sha256", "size_bytes", "status"}
            or global_descriptor.get("path") != launcher_review_path
            or global_descriptor.get("sha256") != global_sha256
            or global_descriptor.get("status") != "PASS"
            or global_descriptor.get("findings") != []
            or global_descriptor.get("reviewed_file_count")
                != len(storage_policy.G0C_REVIEW_REQUIRED_FILES)
            or global_descriptor.get("size_bytes") != len(global_review_bytes)):
        raise RuntimeError("R90 candidate G0C does not bind the exact global launcher review")

    r87_binding = candidate_review.get("r87_binding")
    if not isinstance(r87_binding, dict) or set(r87_binding) != {
            "candidate", "execution_dependency_manifest"}:
        raise RuntimeError("R90 candidate G0C lacks the exact R87 binding envelope")
    candidate_descriptor = r87_binding.get("candidate")
    if candidate_descriptor != {
            "path": candidate_path_name,
            "candidate_id": candidate_id,
            "candidate_core_sha256": candidate_core_sha256}:
        raise RuntimeError("R90 candidate G0C binding does not match the active candidate core")
    manifest_descriptor = r87_binding.get("execution_dependency_manifest")
    expected_manifest_path = r6a_dependency_manifest.manifest_name_for_candidate(candidate_id)
    if (not isinstance(manifest_descriptor, dict)
            or set(manifest_descriptor) != {"path", "sha256"}
            or manifest_descriptor.get("path") != expected_manifest_path
            or not isinstance(manifest_descriptor.get("sha256"), str)
            or re.fullmatch(r"[0-9a-f]{64}", manifest_descriptor["sha256"]) is None
            or supplemental.get(expected_manifest_path) != manifest_descriptor.get("sha256")):
        raise RuntimeError("R90 Gate or candidate G0C does not bind the exact dependency manifest")
    manifest_path = selected_repo / expected_manifest_path
    manifest_bytes = storage_policy._read_file_nofollow(manifest_path, maximum_bytes=32 * 1024 * 1024)
    if hashlib.sha256(manifest_bytes).hexdigest() != manifest_descriptor["sha256"]:
        raise RuntimeError("R90 dependency manifest changed after Gate")
    if candidate.get("r87_binding") != {
            "execution_dependency_manifest": dict(manifest_descriptor)}:
        raise RuntimeError("R90 candidate does not carry the exact dependency-manifest envelope")
    _validate_r87_transaction_contracts(candidate, repo=selected_repo)
    preserved_r86_path = selected_repo / R87_R86_G0C_PRESERVED_PATH
    preserved_r86_bytes = storage_policy._read_file_nofollow(
        preserved_r86_path, maximum_bytes=12 * 1024 * 1024)
    if (not isinstance(candidate.get("preserved_r86_g0c_sha256"), str)
            or hashlib.sha256(preserved_r86_bytes).hexdigest()
                != candidate.get("preserved_r86_g0c_sha256")):
        raise RuntimeError("R90 candidate does not preserve the exact prior active R86 G0C record")
    try:
        preserved_r86 = json.loads(
            preserved_r86_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Preserved R86 G0C review is malformed: {error}") from error
    if (not isinstance(preserved_r86, dict)
            or preserved_r86.get("status") != "PASS"
            or preserved_r86.get("findings") != []
            or not {"r78_binding", "r79_binding", "r80_binding",
                    "r81_binding", "r86_binding"}.issubset(preserved_r86)
            or candidate.get("r80_binding") != preserved_r86.get("r80_binding")
            or candidate.get("r81_binding") != preserved_r86.get("r81_binding")
            or candidate.get("r86_binding") != preserved_r86.get("r86_binding")):
        raise RuntimeError("R90 candidate changed the preserved R80/R81/R86 review lineage")
    try:
        dependency_identity = r6a_dependency_manifest.validate_candidate_g0c_binding(
            candidate=candidate, g0c_review=candidate_review,
            candidate_path=candidate_path_name,
            manifest_sha256=manifest_descriptor["sha256"])
    except r6a_dependency_manifest.DependencyManifestError as error:
        raise RuntimeError(f"R90 candidate/G0C binding is invalid: {error}") from error
    identity_review = candidate_review["identity_review"]
    manifest_route = identity_review.get("manifest_route")
    if (not isinstance(manifest_route, dict)
            or set(manifest_route) != {
                "status", "path", "current_review_is_final_manifest_closure", "rebuild_pending"}
            or manifest_route.get("status") != "PASS"
            or manifest_route.get("path") != expected_manifest_path
            or manifest_route.get("current_review_is_final_manifest_closure") is not True
            or manifest_route.get("rebuild_pending") is not False):
        raise RuntimeError("R90 candidate review does not report a final dependency-manifest closure")
    _validate_r87_fresh_g0c_answer(
        descriptor={"path": candidate_review_path,
                    "sha256": candidate_review_sha256},
        answer=candidate_review, report_sha256=candidate_review_sha256,
        candidate_id=candidate_id, candidate_core_sha256=candidate_core_sha256,
        plan_canonical_sha256=plan_canonical_sha256)

    completion_contract = candidate.get("completion_contract")
    if (not isinstance(completion_contract, dict)
            or set(completion_contract) != {
                "path", "sha256", "manifest_commit_contract_path",
                "manifest_commit_contract_sha256"}):
        raise RuntimeError("R90 candidate completion contract is malformed")
    for desc, label in (
            (completion_contract, "completion contract"),
            (candidate.get("crash_matrix"), "crash matrix")):
        if not isinstance(desc, dict) or not isinstance(desc.get("path"), str):
            raise RuntimeError(f"R90 candidate {label} descriptor is malformed")
        _path, raw = _repo_relative_file(selected_repo, desc["path"], label=f"R90 {label}")
        if hashlib.sha256(raw).hexdigest() != desc.get("sha256"):
            raise RuntimeError(f"R90 candidate {label} changed after review")
    _manifest_contract_path, manifest_contract_bytes = _repo_relative_file(
        selected_repo, completion_contract["manifest_commit_contract_path"],
        label="R90 manifest commit contract")
    if hashlib.sha256(manifest_contract_bytes).hexdigest() != \
            completion_contract["manifest_commit_contract_sha256"]:
        raise RuntimeError("R90 manifest commit contract changed after review")
    try:
        manifest_identity = r6a_dependency_manifest.validate_manifest(
            manifest_path=manifest_path, expected_sha256=manifest_descriptor["sha256"],
            candidate=candidate, plan=validated_plan,
            plan_raw_sha256=hashlib.sha256(plan_bytes).hexdigest(),
            plan_canonical_sha256=plan_canonical_sha256,
            g0c_sha256=dependency_identity["g0c_sha256"],
            candidate_path=candidate_path.relative_to(selected_repo).as_posix(),
            plan_path=plan_relative, repo=selected_repo,
            gate_control_roster=gate_attestation.get("retention_control_sha256"))
    except r6a_dependency_manifest.DependencyManifestError as error:
        raise RuntimeError(f"R90 dependency-manifest closure is invalid: {error}") from error
    if (manifest_identity.get("sha256") != manifest_descriptor["sha256"]
            or manifest_identity.get("candidate_core_sha256") != candidate_core_sha256
            or manifest_identity.get("g0c_sha256") != dependency_identity["g0c_sha256"]):
        raise RuntimeError("R90 final manifest closure does not match the candidate and G0C cores")
    return {
        "binding_sha256": hashlib.sha256(
            build_retention._canonical_json(candidate_review["r87_binding"])).hexdigest(),
        "contract_sha256": completion_contract["sha256"],
        "manifest_commit_contract_sha256": completion_contract["manifest_commit_contract_sha256"],
        "candidate_sha256": candidate_raw_sha256,
        "candidate_canonical_sha256": candidate_canonical_sha256,
        "candidate_diff_sha256": diff_sha256,
        "g0c_report_sha256": candidate_review_sha256,
        "completion_contract_path": str(selected_repo / completion_contract["path"]),
        "crash_matrix_path": str(selected_repo / candidate["crash_matrix"]["path"]),
        "g0c_report_path": str(selected_repo / candidate_review_path),
        "completion_review_path": str(selected_repo / candidate_review_path),
        "manifest_commit_contract_path": str(
            selected_repo / completion_contract["manifest_commit_contract_path"]),
        "candidate_path": str(candidate_path),
        "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "candidate_raw_sha256": candidate_raw_sha256,
        "candidate_id": candidate_id,
        "candidate_core_sha256": candidate_core_sha256,
        "plan_canonical_sha256": plan_canonical_sha256,
        "dependency_manifest": dict(manifest_descriptor),
        "g0c_core_sha256": manifest_identity["g0c_sha256"],
    }
    if candidate_id in {"R6-A-R87-PHASE-AWARE-CAS-CAP-CANDIDATE-8",
                        R90_CANDIDATE_16_ID,
                        R90_CANDIDATE_ID,
                        R91_CANDIDATE_ID,
                        R92_CANDIDATE_ID,
                        R93_CANDIDATE_ID, R94_CANDIDATE_ID,
                        R95_CANDIDATE_ID, R96_CANDIDATE_ID} and (
            answer.get("candidate_id") != candidate_id
            or answer.get("candidate_core_sha256") != candidate_core_sha256
            or answer.get("plan_canonical_sha256") != plan_canonical_sha256):
        raise RuntimeError("Fresh R90 G0C answer does not bind the exact candidate and plan")


def _is_fresh_r6a_plan_path(plan_path: Path) -> bool:
    """Accept only an in-repository plan on a routed R6-A milestone path."""
    selected = Path(plan_path)
    repo = REPO.resolve(strict=True)
    if repo not in selected.parents:
        return False
    name = selected.name.upper()
    exact_names = {
        R87_PLAN_NAME.upper(), R87_CANDIDATE_3_PLAN_NAME.upper(),
        R87_CANDIDATE_4_PLAN_NAME.upper(), R87_CANDIDATE_5_PLAN_NAME.upper(),
        R87_CANDIDATE_6_PLAN_NAME.upper(), R87_CANDIDATE_7_PLAN_NAME.upper(),
        R87_CANDIDATE_8_PLAN_NAME.upper(), R90_PLAN_NAME.upper(),
        R91_PLAN_NAME.upper(), R92_PLAN_NAME.upper(), R93_PLAN_NAME.upper(),
        R94_PLAN_NAME.upper(), R95_PLAN_NAME.upper(), R96_PLAN_NAME.upper(),
        R86_PLAN_NAME.upper(), R81_PLAN_NAME.upper(),
        R80_PLAN_NAME.upper(), R79_PLAN_NAME.upper(), R78_PLAN_NAME.upper(),
    }
    routed_prefixes = (
        "R6-A-R77-", "R6-A-R76-", "R6-A-R75-", "R6-A-R74-", "R6-A-R73-",
        "R6-A-R72-", "R6-A-R71-", "R6-A-R70-", "R6-A-R66-", "R6-A-R69-",
    )
    return ((name in exact_names or name.startswith(routed_prefixes))
            and "R65" not in name and "R64" not in name)
R71_TRANSACTION_CONTRACT_FILES = {
    "experiments/storage/R6-A-R71-EXECUTION-DEPENDENCY-CLOSURE.md",
    "experiments/storage/R6-A-R71-CONSUMPTION-TIME-INTEGRITY.md",
    "experiments/storage/R6-A-R71-CAPACITY-SAFETY-CONTRACT.md",
    "experiments/storage/R6-A-R71-NONLEASE-DRIFT-PURPOSE.md",
    "experiments/storage/R6-A-R71-OS-ISOLATION-ALTERNATIVE.md",
    "experiments/storage/R6-A-R71-R70-FINDING-RECLASSIFICATION.md",
    "experiments/storage/R6-A-R71-THREAT-MODEL.md",
}
R78_TRANSACTION_CONTRACT_FILES = R71_TRANSACTION_CONTRACT_FILES | {
    "experiments/storage/R6-A-R78-GATE-EVIDENCE-CONTRACT.md",
    "experiments/storage/R6-A-R78-GATE-SIDECAR-SCHEMA.json",
    "experiments/storage/R6-A-R78-PREFIXLEASE-TRANSITION-CONTRACT.md",
    "experiments/storage/R6-A-R78-RESERVATION-CONTRACT.md",
    "experiments/storage/R6-A-R78-LATE-REFUSAL-EVIDENCE-CONTRACT.md",
    "experiments/storage/R6-A-R78-COMPLETION-VERIFIER.md",
    "experiments/storage/R6-A-R78-DUAL-GRAPH-ORDER-CONTRACT.md",
}
R79_R77_DISPOSITION_ID = "step11d3-storage-policy-smoke-20261024-69574b75b1a5456e"
R79_TRANSACTION_CONTRACT_FILES = R78_TRANSACTION_CONTRACT_FILES | {
    "experiments/storage/R6-A-R79-FAILED-OUTPUT-DISPOSITION-CONTRACT.md",
    "experiments/storage/R6-A-R79-R77-EVIDENCE-INVENTORY.md",
    "experiments/storage/g0c-launcher-independent-review-R78-PRESERVED.json",
    f"experiments/storage/failed-output-dispositions/{R79_R77_DISPOSITION_ID}/disposition.json",
    f"experiments/storage/failed-output-dispositions/{R79_R77_DISPOSITION_ID}/custody/legacy_diagnostic_transcript.bin",
    f"experiments/storage/failed-output-dispositions/{R79_R77_DISPOSITION_ID}/custody/legacy_reconciliation.bin",
    f"experiments/storage/manifests/{R79_R77_DISPOSITION_ID}.json",
    f"experiments/storage/smoke-claims/{R79_R77_DISPOSITION_ID}.json",
    f"experiments/storage/smoke-runs/{R79_R77_DISPOSITION_ID}/gate0-preflight-attestation.json",
}
R80_R79_G0C_PRESERVED_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R79-PRESERVED.json")
R80_R79_LIVE_ATTEMPT_ID = "step11d3-storage-policy-smoke-20261024-f7c2a643c96a41a7"
R80_R79_LIVE_ATTEMPT_FILES = {
    f"experiments/storage/manifests/{R80_R79_LIVE_ATTEMPT_ID}.json",
    f"experiments/storage/smoke-claims/{R80_R79_LIVE_ATTEMPT_ID}.json",
    f"experiments/storage/smoke-runs/{R80_R79_LIVE_ATTEMPT_ID}/gate0-preflight-attestation.json",
    f"experiments/storage/smoke-runs/{R80_R79_LIVE_ATTEMPT_ID}/gate0-path-state.json.zlib",
    f"experiments/storage/smoke-runs/{R80_R79_LIVE_ATTEMPT_ID}/prefix-state.json",
    f"experiments/storage/smoke-runs/{R80_R79_LIVE_ATTEMPT_ID}/prefix-cleanup.json",
}
R80_TRANSACTION_CONTRACT_FILES = R79_TRANSACTION_CONTRACT_FILES | {
    R80_R79_G0C_PRESERVED_PATH,
    "experiments/storage/R6-A-R80-POST-MATERIALIZATION-PREFIXLEASE-CONTRACT.md",
    "experiments/storage/R6-A-R80-CLEANUP-CONTRACT.md",
    "experiments/storage/R6-A-R80-R79-POST-MATERIALIZATION-FORENSICS.md",
} | R80_R79_LIVE_ATTEMPT_FILES
R81_R80_G0C_PRESERVED_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R80-PRESERVED.json")
R81_TRANSACTION_CONTRACT_FILES = R80_TRANSACTION_CONTRACT_FILES | {
    R81_R80_G0C_PRESERVED_PATH,
    "experiments/storage/R6-A-R81-SMOKE-MANIFEST-PUBLICATION-CONTRACT.md",
    "experiments/storage/R6-A-R81-CLI-OPT-IN-CONTRACT.md",
}
R86_R81_G0C_PRESERVED_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R81-PRESERVED.json")
R86_FRESH_G0C_REVIEW_PATH = "experiments/storage/R6-A-R86-FRESH-G0C-REVIEW.json"
R86_CANONICAL_REPAIR_CONTRACT_PATH = (
    "experiments/storage/R6-A-R86-CANONICAL-REPAIR-CONTRACT.md")
R86_TRANSACTION_CONTRACT_FILES = R81_TRANSACTION_CONTRACT_FILES | {
    R86_R81_G0C_PRESERVED_PATH,
    R86_CANONICAL_REPAIR_CONTRACT_PATH,
    "experiments/storage/R6-A-R86-RUNNER-MAP.md",
    "experiments/storage/R6-A-R86-COMPLETION-VERIFIER.md",
}
R87_R86_G0C_PRESERVED_PATH = (
    "experiments/storage/g0c-launcher-independent-review-R86-PRESERVED.json")
R87_TRANSACTION_CONTRACT_FILES = R86_TRANSACTION_CONTRACT_FILES | {
    R87_R86_G0C_PRESERVED_PATH,
    "experiments/storage/R6-A-R87-CAPACITY-AND-POST-SUCCESS-CAS-CONTRACT.md",
    "experiments/storage/R6-A-R87-RUNNER-MAP.md",
    "experiments/storage/R6-A-R87-COMPLETION-VERIFIER.md",
}
R90_TRANSACTION_CONTRACT_FILES = R87_TRANSACTION_CONTRACT_FILES | {
    R90_DISPOSITION_CONTRACT_PATH,
    R90_OUTPUT_ROOT_CONTRACT_PATH,
    *R90_HISTORICAL_DISPOSITION_PATHS,
    *R90_HISTORICAL_ATTEMPT_EVIDENCE_PATHS,
}
R91_TRANSACTION_CONTRACT_FILES = R90_TRANSACTION_CONTRACT_FILES | {
    R91_WORKSPACE_TRANSITION_CONTRACT_PATH,
    R91_FAILURE_RECOVERY_CONTRACT_PATH,
}
R92_TRANSACTION_CONTRACT_FILES = R90_TRANSACTION_CONTRACT_FILES | {
    R92_HANDOFF_CONTRACT_PATH,
}
R93_TRANSACTION_CONTRACT_FILES = R90_TRANSACTION_CONTRACT_FILES | {
    R93_HANDOFF_CONTRACT_PATH,
}
R94_TRANSACTION_CONTRACT_FILES = R90_TRANSACTION_CONTRACT_FILES | {
    R94_HANDOFF_CONTRACT_PATH,
}
R95_TRANSACTION_CONTRACT_FILES = R90_TRANSACTION_CONTRACT_FILES | {
    R95_HANDOFF_CONTRACT_PATH,
}
R96_TRANSACTION_CONTRACT_FILES = R90_TRANSACTION_CONTRACT_FILES | {
    R96_HANDOFF_CONTRACT_PATH,
}


def _validate_r71_transaction_contracts(candidate: dict[str, object], *, repo: Path) -> dict[str, str]:
    contracts = candidate.get("transaction_contracts")
    if (not isinstance(contracts, dict)
            or set(contracts) != R71_TRANSACTION_CONTRACT_FILES):
        raise RuntimeError("R71 candidate omits or adds an execution, capacity, or isolation contract")
    result: dict[str, str] = {}
    for relative in sorted(R71_TRANSACTION_CONTRACT_FILES):
        expected = contracts[relative]
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise RuntimeError(f"R71 contract digest is malformed: {relative}")
        _path, raw = _repo_relative_file(repo, relative, label="R71 transaction contract")
        if hashlib.sha256(raw).hexdigest() != expected:
            raise RuntimeError(f"R71 transaction contract changed: {relative}")
        result[relative] = expected
    return result


def _validate_r78_transaction_contracts(candidate: dict[str, object], *, repo: Path) -> dict[str, str]:
    contracts = candidate.get("transaction_contracts")
    if (not isinstance(contracts, dict)
            or set(contracts) != R78_TRANSACTION_CONTRACT_FILES):
        raise RuntimeError("R78 candidate omits or adds a Gate-transition or dual-lineage contract")
    result: dict[str, str] = {}
    for relative, expected_digest in contracts.items():
        if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise RuntimeError(f"R78 contract digest is malformed: {relative}")
        _path, raw = _repo_relative_file(repo, relative, label="R78 transaction contract")
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise RuntimeError(f"R78 transaction contract changed: {relative}")
        result[relative] = expected_digest
    return result


def _validate_r79_transaction_contracts(candidate: dict[str, object], *, repo: Path) -> dict[str, str]:
    contracts = candidate.get("transaction_contracts")
    if (not isinstance(contracts, dict)
            or set(contracts) != R79_TRANSACTION_CONTRACT_FILES):
        raise RuntimeError("R79 candidate omits or adds an R77 failed-output disposition dependency")
    result: dict[str, str] = {}
    for relative in sorted(R79_TRANSACTION_CONTRACT_FILES):
        expected_digest = contracts[relative]
        if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise RuntimeError(f"R79 contract digest is malformed: {relative}")
        maximum_bytes = (96 * 1024 * 1024
                         if relative.endswith(f"/{R79_R77_DISPOSITION_ID}/gate0-preflight-attestation.json")
                         else 16 * 1024 * 1024)
        _path, raw = _repo_relative_file(
            repo, relative, label="R79 transaction contract", maximum_bytes=maximum_bytes)
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise RuntimeError(f"R79 transaction contract changed: {relative}")
        result[relative] = expected_digest
    return result


def _validate_r80_transaction_contracts(candidate: dict[str, object], *, repo: Path) -> dict[str, str]:
    contracts = candidate.get("transaction_contracts")
    if (not isinstance(contracts, dict)
            or set(contracts) != R80_TRANSACTION_CONTRACT_FILES):
        raise RuntimeError(
            "R80 candidate omits or adds the post-materialization, cleanup, or preserved R79 evidence contract")
    result: dict[str, str] = {}
    for relative in sorted(R80_TRANSACTION_CONTRACT_FILES):
        expected_digest = contracts[relative]
        if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise RuntimeError(f"R80 contract digest is malformed: {relative}")
        _path, raw = _repo_relative_file(repo, relative, label="R80 transaction contract",
                                        maximum_bytes=96 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise RuntimeError(f"R80 transaction contract changed: {relative}")
        result[relative] = expected_digest
    return result


def _validate_r81_transaction_contracts(candidate: dict[str, object], *, repo: Path) -> dict[str, str]:
    contracts = candidate.get("transaction_contracts")
    if (not isinstance(contracts, dict)
            or set(contracts) != R81_TRANSACTION_CONTRACT_FILES):
        raise RuntimeError("R81 candidate omits or adds publication, CLI, or preserved R80 contracts")
    result: dict[str, str] = {}
    for relative in sorted(R81_TRANSACTION_CONTRACT_FILES):
        expected_digest = contracts[relative]
        if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise RuntimeError(f"R81 contract digest is malformed: {relative}")
        _path, raw = _repo_relative_file(
            repo, relative, label="R81 transaction contract", maximum_bytes=96 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise RuntimeError(f"R81 contract changed: {relative}")
        result[relative] = expected_digest
    return result


def _validate_r86_transaction_contracts(candidate: dict[str, object], *, repo: Path) -> dict[str, str]:
    contracts = candidate.get("transaction_contracts")
    if (not isinstance(contracts, dict)
            or set(contracts) != R86_TRANSACTION_CONTRACT_FILES):
        raise RuntimeError("R86 candidate omits or adds canonical-repair or preserved R81 contracts")
    result: dict[str, str] = {}
    for relative in sorted(R86_TRANSACTION_CONTRACT_FILES):
        expected_digest = contracts[relative]
        if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise RuntimeError(f"R86 contract digest is malformed: {relative}")
        _path, raw = _repo_relative_file(
            repo, relative, label="R86 transaction contract", maximum_bytes=96 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise RuntimeError(f"R86 contract changed: {relative}")
        result[relative] = expected_digest
    return result


def _validate_r87_transaction_contracts(candidate: dict[str, object], *, repo: Path) -> dict[str, str]:
    contracts = candidate.get("transaction_contracts")
    expected_contract_files = (
        R96_TRANSACTION_CONTRACT_FILES if candidate.get("candidate_id") == R96_CANDIDATE_ID
        else R95_TRANSACTION_CONTRACT_FILES if candidate.get("candidate_id") == R95_CANDIDATE_ID
        else R94_TRANSACTION_CONTRACT_FILES if candidate.get("candidate_id") == R94_CANDIDATE_ID
        else R93_TRANSACTION_CONTRACT_FILES if candidate.get("candidate_id") == R93_CANDIDATE_ID
        else R92_TRANSACTION_CONTRACT_FILES if candidate.get("candidate_id") == R92_CANDIDATE_ID
        else R91_TRANSACTION_CONTRACT_FILES if candidate.get("candidate_id") == R91_CANDIDATE_ID
        else R90_TRANSACTION_CONTRACT_FILES
        if candidate.get("candidate_id") == R90_CANDIDATE_ID
        else R87_TRANSACTION_CONTRACT_FILES)
    if (not isinstance(contracts, dict)
            or set(contracts) != expected_contract_files):
        raise RuntimeError("R87 candidate omits or adds capacity, CAS-order, runner, or preserved R86 contracts")
    result: dict[str, str] = {}
    for relative in sorted(expected_contract_files):
        expected_digest = contracts[relative]
        if not isinstance(expected_digest, str) or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None:
            raise RuntimeError(f"R87 contract digest is malformed: {relative}")
        _path, raw = _repo_relative_file(
            repo, relative, label="R87 transaction contract", maximum_bytes=96 * 1024 * 1024)
        if hashlib.sha256(raw).hexdigest() != expected_digest:
            raise RuntimeError(f"R87 contract changed: {relative}")
        result[relative] = expected_digest
    return result


def _json_object_without_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _validate_r65_g0c_binding(review_bytes: bytes, *, repo: Path, plan_path: Path,
                              plan_bytes: bytes, plan_canonical_sha256: str,
                              validated_plan: dict[str, object]) -> dict[str, str]:
    """Verify the fresh G0C R65 receipt against every live plan, control, and governance file."""
    try:
        review = json.loads(review_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Fresh G0C review is malformed or contains duplicate keys: {error}") from error
    if (not isinstance(review, dict) or review.get("status") != "PASS"
            or review.get("findings") != []):
        raise RuntimeError("Fresh G0C review must PASS with an empty findings list")
    binding = review.get("r65_binding")
    required_binding_fields = {
        "schema_version", "milestone", "status", "contract_sha256", "execution_plan",
        "candidate", "files",
    }
    if (not isinstance(binding, dict) or set(binding) != required_binding_fields
            or binding.get("schema_version") != 1 or binding.get("milestone") != "R65"
            or binding.get("status") != "PASS"):
        raise RuntimeError("Fresh G0C review lacks the exact PASS R65 binding section")

    selected_repo = repo.resolve(strict=True)
    selected_plan = plan_path.resolve(strict=True)
    try:
        plan_relative = selected_plan.relative_to(selected_repo).as_posix()
    except ValueError as error:
        raise RuntimeError("Fresh R65 execution plan escapes the repository") from error
    if "R65" not in selected_plan.name.upper() or "R64" in selected_plan.name.upper():
        raise RuntimeError("G0C R65 binding cannot authorize a stale R64 plan")
    current_plan_bytes = storage_policy._read_file_nofollow(
        selected_plan, maximum_bytes=16 * 1024 * 1024)
    if current_plan_bytes != plan_bytes:
        raise RuntimeError("Fresh R65 plan bytes changed after the runner read them")
    try:
        current_plan = json.loads(
            current_plan_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Fresh R65 plan is malformed or contains duplicate keys: {error}") from error
    if (not isinstance(current_plan, dict)
            or current_plan != validated_plan
            or hashlib.sha256(build_retention._canonical_json(current_plan)).hexdigest()
                != plan_canonical_sha256):
        raise RuntimeError("Fresh R65 plan bytes, validated plan, and canonical digest do not agree")
    contract_bytes = storage_policy._read_file_nofollow(selected_repo / R65_DUAL_GRAPH_CONTRACT)
    contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    if binding.get("contract_sha256") != contract_sha256:
        raise RuntimeError("Fresh G0C R65 binding does not match the live dual-graph contract")
    expected_plan = {
        "path": plan_relative,
        "sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "canonical_sha256": plan_canonical_sha256,
    }
    if binding.get("execution_plan") != expected_plan:
        raise RuntimeError("Fresh G0C R65 binding does not match the exact raw and canonical plan")

    candidate_binding = binding.get("candidate")
    if (not isinstance(candidate_binding, dict)
            or set(candidate_binding) != {"path", "sha256", "canonical_sha256"}
            or not isinstance(candidate_binding.get("path"), str)
            or R65_CANDIDATE_PATH_PATTERN.fullmatch(candidate_binding["path"]) is None):
        raise RuntimeError("Fresh G0C R65 binding lacks an exact frozen candidate-file descriptor")
    candidate_relative = candidate_binding["path"]
    candidate_path = selected_repo / candidate_relative
    try:
        candidate_bytes = storage_policy._read_file_nofollow(
            candidate_path, maximum_bytes=4 * 1024 * 1024)
    except (OSError, RuntimeError) as error:
        raise RuntimeError(f"Frozen R65 candidate file is missing or unsafe: {error}") from error
    try:
        candidate = json.loads(candidate_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Frozen R65 candidate file is malformed: {error}") from error
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    candidate_canonical_sha256 = hashlib.sha256(
        build_retention._canonical_json(candidate)).hexdigest()
    if (candidate_binding.get("sha256") != candidate_sha256
            or candidate_binding.get("canonical_sha256") != candidate_canonical_sha256):
        raise RuntimeError("Fresh G0C R65 binding does not match the exact frozen candidate bytes")

    if (not isinstance(candidate, dict)
            or set(candidate) != {"schema_version", "milestone", "source_identity", "execution_plan",
                                  "inputs", "changed_files", "exact_diff"}
            or type(candidate.get("schema_version")) is not int
            or candidate.get("schema_version") != 1
            or candidate.get("milestone") != "R65"
            or candidate.get("source_identity") != validated_plan.get("source_identity")
            or candidate.get("execution_plan") != expected_plan):
        raise RuntimeError("Frozen R65 candidate does not bind the exact source identity and execution plan")

    expected_paths = {
        key: (Path("experiments/storage") / relative_path).as_posix()
        for key, relative_path in storage_policy.G0C_REVIEW_REQUIRED_FILES.items()
    }
    expected_paths.update({
        "r65_execution_runner": "experiments/storage/run_r6a_execution.py",
        "r65_execution_plan": plan_relative,
        R65_CANDIDATE_FILE_KEY: candidate_relative,
        R65_CANDIDATE_DIFF_FILE_KEY: candidate_relative.removesuffix(".json") + ".diff",
        "r65_dual_graph_contract": R65_DUAL_GRAPH_CONTRACT,
        **R65_GOVERNANCE_FILES,
    })
    rows = binding.get("files")
    if not isinstance(rows, dict) or set(rows) != set(expected_paths):
        raise RuntimeError("Fresh G0C R65 binding has missing or extra control, test, or governance files")
    current_rows: dict[str, dict[str, object]] = {}
    for key, relative_path in expected_paths.items():
        try:
            current_bytes = (plan_bytes if key == "r65_execution_plan" else
                             candidate_bytes if key == R65_CANDIDATE_FILE_KEY else
                             storage_policy._read_file_nofollow(selected_repo / relative_path,
                                                                maximum_bytes=16 * 1024 * 1024))
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"Fresh G0C R65 binding target is unreadable: {relative_path}: {error}") from error
        row = rows.get(key)
        if (not isinstance(row, dict) or set(row) != {"path", "sha256", "size_bytes"}
                or row.get("path") != relative_path
                or row.get("sha256") != hashlib.sha256(current_bytes).hexdigest()
                or type(row.get("size_bytes")) is not int
                or row.get("size_bytes") != len(current_bytes)):
            raise RuntimeError(f"Fresh G0C R65 binding is stale or malformed for {relative_path}")
        current_rows[key] = {
            "path": relative_path,
            "sha256": hashlib.sha256(current_bytes).hexdigest(),
            "size_bytes": len(current_bytes),
        }
    expected_changed_files = {
        path: current_rows[key]["sha256"]
        for path, key in R65_CHANGED_SOURCE_FILES.items()
    }
    candidate_diff_path = candidate_relative.removesuffix(".json") + ".diff"
    expected_exact_diff = {
        "path": candidate_diff_path,
        "sha256": current_rows[R65_CANDIDATE_DIFF_FILE_KEY]["sha256"],
    }
    if candidate.get("changed_files") != expected_changed_files:
        raise RuntimeError("Frozen R65 candidate changed-file hashes do not match current production, test, and runner files")
    if (not isinstance(candidate.get("exact_diff"), dict)
            or set(candidate["exact_diff"]) != {"path", "sha256"}
            or candidate["exact_diff"] != expected_exact_diff):
        raise RuntimeError("Frozen R65 candidate exact-diff descriptor does not match the paired G0C-bound diff")
    candidate_inputs = candidate.get("inputs")
    expected_candidate_inputs = {
        key: row for key, row in current_rows.items()
        if key not in {R65_CANDIDATE_FILE_KEY, "r65_execution_plan"}
    }
    if candidate_inputs != expected_candidate_inputs:
        raise RuntimeError("Frozen R65 candidate inputs differ from the current bound source and control files")
    return {
        "binding_sha256": hashlib.sha256(build_retention._canonical_json(binding)).hexdigest(),
        "contract_sha256": contract_sha256,
        "candidate_sha256": candidate_sha256,
        "candidate_canonical_sha256": candidate_canonical_sha256,
    }


def _validate_r65_recovery_plan(plan: dict[str, object]) -> None:
    """Require the R65 recovery ID and the exact ordered Windows/native plan."""
    build_id = plan.get("build_id")
    admission = plan.get("admission_identity")
    admission_id = admission.get("admission_id") if isinstance(admission, dict) else None
    if (not isinstance(build_id, str)
            or re.fullmatch(r"r6a-r65-recovery-dual-lineage-[A-Za-z0-9][A-Za-z0-9._-]{0,80}",
                            build_id) is None
            or not isinstance(admission_id, str)
            or not admission_id.startswith("r6a-r65-")
            or admission_id == "r6a-r65-"):
        raise RuntimeError("R65 recovery plan must carry a new recovery build ID and fresh R65 admission ID")
    if not build_retention._session_dual_graph_enabled(plan):
        raise RuntimeError("R65 recovery admission requires the reviewed dual-graph capability")
    graphs = plan.get("build_graphs")
    graph_ids = ([row.get("graph_id") if isinstance(row, dict) else None for row in graphs]
                 if isinstance(graphs, list) else None)
    if graph_ids != list(build_retention.BUILD_SESSION_DUAL_GRAPH_IDS):
        raise RuntimeError("R65 recovery plan must declare the exact ordered Windows/native graph pair")
    prebuild = plan.get("prebuild_evidence")
    expected_prebuild_path = R65_GOVERNANCE_FILES["r65_candidate4_preflight_manifest"]
    if (not isinstance(prebuild, dict)
            or prebuild.get("manifest_path") != expected_prebuild_path
            or build_retention._validate_dual_graph_prebuild_identity(plan)
                != plan.get("build_configuration_key_sha256")):
        raise RuntimeError("R65 recovery plan must freeze the admitted prebuild graph-pair configuration key")


def _assert_r65_admission_id_unclaimed(admission_id: str, *,
                                       current_claim_sha256: str | None = None,
                                       current_manifest: dict[str, object] | None = None) -> None:
    """Reject prior/retained history; optionally admit only this claim-bound current manifest."""
    if re.fullmatch(r"r6a-r65-[A-Za-z0-9_-]+", admission_id) is None:
        raise RuntimeError("R65 admission ID is not path-safe")
    manifest_path = Path(storage_policy._experiment_manifest_path(admission_id))
    parent_fd: int | None = None
    try:
        parent_fd = storage_policy._open_directory_nofollow(manifest_path.parent)
        for entry in os.listdir(parent_fd):
            if not entry.endswith(".json"):
                continue
            try:
                before = os.stat(entry, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise RuntimeError(f"R65 manifest registry contains an unsafe entry: {entry}")
                fd = os.open(entry, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
                try:
                    opened = os.fstat(fd)
                    if ((opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size)
                            != (before.st_dev, before.st_ino, before.st_mode, before.st_size)):
                        raise RuntimeError(f"R65 manifest registry entry changed while opening: {entry}")
                    if opened.st_size > 16 * 1024 * 1024:
                        raise RuntimeError(f"R65 manifest registry entry exceeds its read bound: {entry}")
                    chunks: list[bytes] = []
                    remaining = 16 * 1024 * 1024 + 1
                    while remaining > 0:
                        chunk = os.read(fd, min(65536, remaining))
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    raw = b"".join(chunks)
                    after = os.fstat(fd)
                    if ((opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
                            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                            or len(raw) != opened.st_size):
                        raise RuntimeError(f"R65 manifest registry entry changed while reading: {entry}")
                finally:
                    os.close(fd)
                try:
                    manifest = json.loads(raw, object_pairs_hook=_json_object_without_duplicate_keys)
                except (TypeError, ValueError, json.JSONDecodeError) as error:
                    raise RuntimeError(f"R65 manifest registry entry is malformed: {entry}: {error}") from error
                if not isinstance(manifest, dict):
                    raise RuntimeError(f"R65 manifest registry entry is not an object: {entry}")
                if entry == manifest_path.name:
                    hashes = manifest.get("candidate_hashes")
                    if (current_claim_sha256 is not None
                            and current_manifest is not None
                            and manifest.get("experiment_id") == admission_id
                            and manifest == current_manifest
                            and isinstance(hashes, dict)
                            and hashes.get("r65_admission_claim_sha256") == current_claim_sha256):
                        continue
                    raise RuntimeError(
                        f"R65 admission identity has a canonical prior or unbound manifest: {entry}")
                if manifest.get("experiment_id") == admission_id:
                    raise RuntimeError(f"R65 admission identity has a retained manifest: {entry}")
            except FileNotFoundError as error:
                raise RuntimeError(f"R65 manifest registry changed during identity check: {entry}") from error
            except OSError as error:
                raise RuntimeError(f"could not safely inspect R65 manifest registry entry {entry}: {error}") from error
    except FileNotFoundError as error:
        raise RuntimeError("R65 admission manifest registry is unavailable") from error
    except OSError as error:
        raise RuntimeError(f"could not safely inspect R65 admission manifest registry: {error}") from error
    finally:
        if parent_fd is not None:
            os.close(parent_fd)


def _claim_r65_admission_id(admission_id: str, plan_canonical_sha256: str, *,
                            claims_root: Path | None = None) -> dict[str, str]:
    """Reject retained history, then consume the ID through storage's shared claim primitive."""
    _assert_r65_admission_id_unclaimed(admission_id)
    return storage_policy._claim_r65_admission_id(
        admission_id, plan_canonical_sha256, claims_root=claims_root)


def _read_r65_admission_claim(admission_id: str, plan_canonical_sha256: str, *,
                              expected_sha256: str,
                              claims_root: Path | None = None) -> dict[str, object]:
    """Compatibility wrapper for reservation callers; storage owns claim readback."""
    return storage_policy._read_r65_admission_claim(
        admission_id, plan_canonical_sha256, expected_sha256=expected_sha256,
        claims_root=claims_root)


_R6A_ADMISSION_ID_PATTERN = re.compile(
    r"r6a-r([1-9][0-9]*)-admission-[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z")
_R6A_BUILD_ID_PATTERN = re.compile(
    r"r6a-r([1-9][0-9]*)-dual-lineage-[A-Za-z0-9][A-Za-z0-9._-]{0,80}\Z")


def _r6a_id_milestone(value: object, pattern: re.Pattern[str]) -> str | None:
    """Return the milestone of a canonical path-safe R6-A identity."""
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        return None
    match = pattern.fullmatch(value)
    milestone_match = re.match(r"r6a-r([1-9][0-9]*)-", value)
    if match is None or milestone_match is None:
        return None
    milestone_number = int(milestone_match.group(1))
    if milestone_number != 66 and milestone_number < 69:
        return None
    return f"R{milestone_number}"


def _r90_admission_output_dir(plan: Mapping[str, object], *, repo: Path = REPO,
                              require_missing: bool = True) -> Path:
    """Resolve the R90 output root from the exact, canonical admission identity."""
    if plan.get("admission_output_contract") != R90_ADMISSION_OUTPUT_CONTRACT:
        raise RuntimeError("R90 plan does not declare the unique output-root contract")
    identity = plan.get("admission_identity")
    admission_id = identity.get("admission_id") if isinstance(identity, Mapping) else None
    if (_r6a_id_milestone(admission_id, _R6A_ADMISSION_ID_PATTERN) != "R87"
            or not isinstance(admission_id, str)
            or not admission_id.isascii()
            or admission_id != admission_id.casefold()):
        raise RuntimeError("R90 output root requires a canonical lowercase R87 admission ID")
    suffix = admission_id.split("-admission-", 1)[-1]
    if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", suffix, flags=re.ASCII) is None:
        raise RuntimeError("R90 admission ID is not a normalized path component")
    relative = f"{R90_ADMISSION_OUTPUT_ROOT}/{admission_id}"
    if plan.get("admission_output_dir") != relative:
        raise RuntimeError("R90 plan does not bind its exact admission-unique output root")
    repo_path = Path(repo).absolute()
    target = repo_path / relative
    if (target.is_absolute() is False or ".." in target.parts
            or target.as_posix() != f"{repo_path.as_posix()}/{relative}"):
        raise RuntimeError("R90 admission output path is not lexically canonical")
    try:
        repo_real = repo_path.resolve(strict=True)
        parent = target.parent
        parent_info = parent.lstat()
        if (not stat.S_ISDIR(parent_info.st_mode) or stat.S_ISLNK(parent_info.st_mode)
                or parent_info.st_uid != os.getuid()
                or stat.S_IMODE(parent_info.st_mode) & 0o022
                or parent.resolve(strict=True) != parent
                or repo_real not in parent.resolve(strict=True).parents):
            raise RuntimeError("R90 admission output root parent is not a canonical repository directory")
        with os.scandir(parent) as entries:
            if any(entry.name.casefold() == admission_id.casefold()
                   and entry.name != admission_id for entry in entries):
                raise RuntimeError("R90 admission output root has a case-fold collision")
    except OSError as error:
        raise RuntimeError("R90 admission output root parent is unavailable or unsafe") from error
    try:
        normalized = storage_policy._measured_output_dir(target, require_missing=require_missing)
    except (OSError, TypeError, ValueError) as error:
        raise RuntimeError(f"R90 admission output root is unsafe or already claimed: {error}") from error
    if normalized != target or normalized.resolve(strict=False) != target:
        raise RuntimeError("R90 admission output root changed under canonicalization")
    return normalized


def _validate_r66_recovery_plan(plan: dict[str, object]) -> None:
    """Require a fresh R66/R69 identity and the exact reviewed dual-graph plan."""
    build_id = plan.get("build_id")
    admission = plan.get("admission_identity")
    admission_id = admission.get("admission_id") if isinstance(admission, dict) else None
    admission_sha256 = admission.get("sha256") if isinstance(admission, dict) else None
    build_milestone = _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN)
    admission_milestone = _r6a_id_milestone(admission_id, _R6A_ADMISSION_ID_PATTERN)
    if build_milestone is None or admission_milestone is None or build_milestone != admission_milestone:
        raise RuntimeError("R66/R69/R70+ plan must carry a fresh dual-lineage build ID and admission ID")
    expected_admission_sha256 = hashlib.sha256(build_retention._canonical_json(
        {"admission_id": admission_id})).hexdigest()
    if (not isinstance(admission_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", admission_sha256) is None
            or admission_sha256 != expected_admission_sha256):
        raise RuntimeError("R66 plan admission identity digest is not canonical for its admission ID")
    if "admission_output_dir" in plan or "admission_output_contract" in plan:
        _r90_admission_output_dir(plan)
    if not build_retention._session_dual_graph_enabled(plan):
        raise RuntimeError("R66 admission requires the reviewed dual-graph capability")
    graphs = plan.get("build_graphs")
    graph_ids = ([row.get("graph_id") if isinstance(row, dict) else None for row in graphs]
                 if isinstance(graphs, list) else None)
    if graph_ids != list(build_retention.BUILD_SESSION_DUAL_GRAPH_IDS):
        raise RuntimeError("R66 plan must declare the exact ordered Windows/native graph pair")
    prebuild = plan.get("prebuild_evidence")
    if (not isinstance(prebuild, dict)
            or build_retention._validate_dual_graph_prebuild_identity(plan)
                != plan.get("build_configuration_key_sha256")):
        raise RuntimeError("R66 plan must freeze the validated prebuild graph-pair configuration key")
    if build_milestone == "R87":
        if plan.get("persistent_output_allowance_bytes") != build_retention.MAX_SESSION_PERSISTENT_OUTPUT_ALLOWANCE_BYTES:
            raise RuntimeError("R87 plan must bind the exact reviewed 1 GiB persistent-output cap")
    elif "persistent_output_allowance_bytes" in plan:
        raise RuntimeError("persistent-output allowance is reserved for the exact R87 route")


def _configured_plan_override(environ: Mapping[str, str] | None = None) -> str | None:
    """Select the newest explicit milestone plan override without changing older precedence."""
    values = os.environ if environ is None else environ
    return (values.get("FG_METAL_R96_PLAN_PATH")
            or values.get("FG_METAL_R95_PLAN_PATH")
            or values.get("FG_METAL_R94_PLAN_PATH")
            or values.get("FG_METAL_R93_PLAN_PATH")
            or values.get("FG_METAL_R92_PLAN_PATH")
            or values.get("FG_METAL_R91_PLAN_PATH")
            or values.get("FG_METAL_R90_PLAN_PATH")
            or values.get("FG_METAL_R87_PLAN_PATH")
            or values.get("FG_METAL_R86_PLAN_PATH")
            or values.get("FG_METAL_R81_PLAN_PATH")
            or values.get("FG_METAL_R80_PLAN_PATH")
            or values.get("FG_METAL_R79_PLAN_PATH")
            or values.get("FG_METAL_R78_PLAN_PATH")
            or values.get("FG_METAL_R77_PLAN_PATH")
            or values.get("FG_METAL_R76_PLAN_PATH")
            or values.get("FG_METAL_R75_PLAN_PATH")
            or values.get("FG_METAL_R74_PLAN_PATH")
            or values.get("FG_METAL_R73_PLAN_PATH")
            or values.get("FG_METAL_R72_PLAN_PATH")
            or values.get("FG_METAL_R71_PLAN_PATH")
            or values.get("FG_METAL_R70_PLAN_PATH")
            or values.get("FG_METAL_R69_PLAN_PATH")
            or values.get("FG_METAL_R66_PLAN_PATH"))


def _repo_relative_file(repo: Path, relative: object, *, label: str,
                        maximum_bytes: int = 16 * 1024 * 1024) -> tuple[Path, bytes]:
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or "\x00" in relative):
        raise RuntimeError(f"R66 {label} path is malformed")
    relative_path = Path(relative)
    if (relative_path.is_absolute() or relative_path.as_posix() != relative
            or any(part in {"", ".", ".."} for part in relative_path.parts)):
        raise RuntimeError(f"R66 {label} path is not canonical and repository-relative")
    selected_repo = repo.resolve(strict=True)
    path = selected_repo / relative_path
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(selected_repo)
    except (OSError, ValueError) as error:
        raise RuntimeError(f"R66 {label} escapes the repository or is missing") from error
    return path, storage_policy._read_file_nofollow(path, maximum_bytes=maximum_bytes)


def _validate_r66_g0c_binding(review_bytes: bytes, *, repo: Path, plan_path: Path,
                              plan_bytes: bytes, plan_canonical_sha256: str,
                              validated_plan: dict[str, object]) -> dict[str, str]:
    """Verify a frozen R66/R69 review, candidate, plan, and exact source diff."""
    try:
        review = json.loads(review_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Fresh R66 G0C record is malformed: {error}") from error
    base_binding_fields = {
        "schema_version", "milestone", "status", "execution_plan", "candidate",
        "completion_contract", "crash_matrix", "completion_review",
        "completion_review_verdicts", "fresh_g0c_review", "fresh_g0c_answer", "files",
    }
    binding_fields = set(base_binding_fields)
    binding = None
    milestone = None
    if isinstance(review, dict):
        present_bindings = [key for key in ("r66_binding", "r69_binding", "r70_binding", "r71_binding", "r72_binding", "r73_binding", "r74_binding", "r75_binding", "r76_binding", "r77_binding", "r78_binding", "r79_binding", "r80_binding", "r81_binding", "r86_binding", "r87_binding") if key in review]
        active_binding_key = (
            "r87_binding" if "r87_binding" in present_bindings
            and set(present_bindings) == {"r87_binding"}
            else
            "r86_binding" if "r86_binding" in present_bindings
            and set(present_bindings) in ({"r86_binding"}, {"r81_binding", "r86_binding"},
                                          {"r78_binding", "r79_binding", "r80_binding",
                                           "r81_binding", "r86_binding"})
            else
            "r81_binding" if "r81_binding" in present_bindings
            and set(present_bindings) in ({"r81_binding"}, {"r80_binding", "r81_binding"},
                                          {"r79_binding", "r80_binding", "r81_binding"},
                                          {"r78_binding", "r79_binding", "r80_binding", "r81_binding"})
            else "r80_binding" if "r80_binding" in present_bindings
            and set(present_bindings) in ({"r80_binding"}, {"r79_binding", "r80_binding"},
                                          {"r78_binding", "r79_binding", "r80_binding"})
            else "r79_binding" if "r79_binding" in present_bindings
            and set(present_bindings) in ({"r79_binding"}, {"r78_binding", "r79_binding"})
            else present_bindings[0] if len(present_bindings) == 1 else None)
        if active_binding_key is not None:
            binding = review[active_binding_key]
            milestone = {"r66_binding": "R66", "r69_binding": "R69",
                         "r70_binding": "R70", "r71_binding": "R71",
                         "r72_binding": "R72", "r73_binding": "R73",
                         "r74_binding": "R74", "r75_binding": "R75",
                         "r76_binding": "R76", "r77_binding": "R77",
                         "r78_binding": "R78", "r79_binding": "R79",
                         "r80_binding": "R80", "r81_binding": "R81",
                         "r86_binding": "R86", "r87_binding": "R87"}[active_binding_key]
            if milestone in {"R71", "R72", "R73", "R74", "R75", "R76", "R77", "R78", "R79", "R80", "R81", "R86", "R87"}:
                binding_fields.add("execution_dependency_manifest")
    if (not isinstance(review, dict) or review.get("status") != "PASS"
            or review.get("findings") != [] or not isinstance(binding, dict)
            or set(binding) != binding_fields or binding.get("schema_version") != 1
            or binding.get("milestone") != milestone or binding.get("status") != "PASS"):
        raise RuntimeError("Fresh G0C record lacks one exact passing milestone-specific binding")

    selected_repo = repo.resolve(strict=True)
    selected_plan = plan_path.resolve(strict=True)
    try:
        plan_relative = selected_plan.relative_to(selected_repo).as_posix()
    except ValueError as error:
        raise RuntimeError("Fresh R66 plan escapes the repository") from error
    if not selected_plan.name.upper().startswith(f"R6-A-{milestone}-"):
        raise RuntimeError("G0C binding cannot authorize a plan from a different milestone")
    if milestone == "R78" and plan_relative != f"experiments/storage/{R78_PLAN_NAME}":
        raise RuntimeError("R78 G0C binding requires its one canonical dual-lineage plan path")
    if milestone == "R79" and plan_relative != f"experiments/storage/{R79_PLAN_NAME}":
        raise RuntimeError("R79 G0C binding requires its one canonical dual-lineage plan path")
    if milestone == "R80" and plan_relative != f"experiments/storage/{R80_PLAN_NAME}":
        raise RuntimeError("R80 G0C binding requires its one canonical dual-lineage plan path")
    if milestone == "R81" and plan_relative != f"experiments/storage/{R81_PLAN_NAME}":
        raise RuntimeError("R81 G0C binding requires its one canonical dual-lineage plan path")
    if milestone == "R87" and plan_relative not in {
            f"experiments/storage/{R87_PLAN_NAME}",
            f"experiments/storage/{R87_CANDIDATE_3_PLAN_NAME}",
            f"experiments/storage/{R87_CANDIDATE_4_PLAN_NAME}",
            f"experiments/storage/{R87_CANDIDATE_5_PLAN_NAME}"}:
        raise RuntimeError("R87 G0C binding requires its one canonical phase-aware plan path")
    if milestone == "R86" and plan_relative != f"experiments/storage/{R86_PLAN_NAME}":
        raise RuntimeError("R86 G0C binding requires its one canonical dual-lineage plan path")
    current_plan_bytes = storage_policy._read_file_nofollow(
        selected_plan, maximum_bytes=16 * 1024 * 1024)
    if current_plan_bytes != plan_bytes:
        raise RuntimeError("Fresh R66 plan bytes changed after the runner read them")
    try:
        current_plan = json.loads(
            current_plan_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Fresh R66 plan is malformed: {error}") from error
    if (not isinstance(current_plan, dict) or current_plan != validated_plan
            or hashlib.sha256(build_retention._canonical_json(current_plan)).hexdigest()
                != plan_canonical_sha256):
        raise RuntimeError("Fresh R66 plan bytes, validated plan, and canonical digest disagree")
    admission_identity = current_plan.get("admission_identity")
    selected_build_milestone = _r6a_id_milestone(
        current_plan.get("build_id"), _R6A_BUILD_ID_PATTERN)
    selected_admission_milestone = _r6a_id_milestone(
        admission_identity.get("admission_id") if isinstance(admission_identity, dict) else None,
        _R6A_ADMISSION_ID_PATTERN)
    if (selected_build_milestone != milestone
            or selected_admission_milestone != milestone):
        raise RuntimeError("Fresh G0C route milestone does not match the plan's build and admission IDs")

    expected_plan = {
        "path": plan_relative,
        "raw_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "canonical_sha256": plan_canonical_sha256,
        "graph_pair_key": validated_plan.get("build_configuration_key_sha256"),
    }
    plan_binding = binding.get("execution_plan")
    if plan_binding != expected_plan:
        raise RuntimeError("Fresh R66 G0C binding does not match the exact plan and graph pair")

    candidate_binding = binding.get("candidate")
    candidate_pattern = {"R66": R66_CANDIDATE_PATH_PATTERN,
                         "R69": R69_CANDIDATE_PATH_PATTERN,
                         "R70": R70_CANDIDATE_PATH_PATTERN,
                         "R71": R71_CANDIDATE_PATH_PATTERN,
                         "R72": R72_CANDIDATE_PATH_PATTERN,
                         "R73": R73_CANDIDATE_PATH_PATTERN,
                         "R74": R74_CANDIDATE_PATH_PATTERN,
                         "R75": R75_CANDIDATE_PATH_PATTERN,
                         "R76": R76_CANDIDATE_PATH_PATTERN,
                         "R77": R77_CANDIDATE_PATH_PATTERN,
                         "R78": R78_CANDIDATE_PATH_PATTERN,
                         "R79": R79_CANDIDATE_PATH_PATTERN,
                         "R80": R80_CANDIDATE_PATH_PATTERN,
                         "R81": R81_CANDIDATE_PATH_PATTERN,
                         "R86": R86_CANDIDATE_PATH_PATTERN,
                         "R87": R87_CANDIDATE_PATH_PATTERN}[milestone]
    candidate_binding_fields = ({"path", "candidate_id", "candidate_core_sha256"}
                                if milestone in {"R71", "R72", "R73", "R74", "R75", "R76", "R77", "R78", "R79", "R80", "R81", "R86", "R87"} else
                                {"path", "raw_sha256", "canonical_sha256"})
    if (not isinstance(candidate_binding, dict)
            or set(candidate_binding) != candidate_binding_fields
            or not isinstance(candidate_binding.get("path"), str)
            or candidate_pattern.fullmatch(candidate_binding["path"]) is None):
        raise RuntimeError(f"Fresh {milestone} G0C binding lacks its exact candidate descriptor")
    candidate_path, candidate_bytes = _repo_relative_file(
        selected_repo, candidate_binding["path"], label="candidate", maximum_bytes=4 * 1024 * 1024)
    try:
        candidate = json.loads(candidate_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Frozen {milestone} candidate is malformed: {error}") from error
    candidate_sha256 = hashlib.sha256(candidate_bytes).hexdigest()
    candidate_canonical_sha256 = hashlib.sha256(
        build_retention._canonical_json(candidate)).hexdigest()
    candidate_id = None
    candidate_core_sha256 = None
    if milestone in {"R71", "R72", "R73", "R74", "R75", "R76", "R77", "R78", "R79", "R80", "R81", "R86", "R87"}:
        candidate_id = candidate.get("candidate_id") if isinstance(candidate, dict) else None
        if milestone == "R87" and candidate_id in {
                R90_CANDIDATE_ID, R91_HISTORICAL_CANDIDATE_20_ID,
                R91_CANDIDATE_ID, R92_CANDIDATE_ID, R93_CANDIDATE_ID,
                R94_CANDIDATE_ID, R95_CANDIDATE_ID, R96_CANDIDATE_ID}:
            raise RuntimeError("Current R87 candidate requires its exact fresh-review route")
        try:
            r6a_dependency_manifest._manifest_envelope(candidate, role="R71 candidate")
            candidate_core_sha256 = r6a_dependency_manifest._candidate_core_sha256(candidate)
        except r6a_dependency_manifest.DependencyManifestError as error:
            raise RuntimeError(f"R71 candidate binding is malformed: {error}") from error
        if (candidate_id != Path(candidate_binding["path"]).stem
                or candidate_binding.get("candidate_id") != candidate_id
                or candidate_binding.get("candidate_core_sha256") != candidate_core_sha256):
            raise RuntimeError("Fresh R71 G0C binding does not match the candidate core identity")
    elif (candidate_binding.get("raw_sha256") != candidate_sha256
          or candidate_binding.get("canonical_sha256") != candidate_canonical_sha256):
        raise RuntimeError(f"Fresh {milestone} G0C binding does not match the exact candidate bytes")
    if (not isinstance(candidate, dict) or candidate.get("schema_version") != 1
            or candidate.get("milestone") != milestone
            or candidate.get("status") != "FROZEN_FOR_ACCEPTANCE_VALIDATION"
            or candidate.get("execution_plan") != expected_plan
            or candidate.get("graph_pair_key") != expected_plan["graph_pair_key"]):
        raise RuntimeError(f"Frozen {milestone} candidate does not bind the exact plan and graph pair")
    if milestone == "R70":
        lineage = candidate.get("candidate3_completion_lineage")
        expected_lineage_fields = {
            "candidate_path", "candidate_raw_sha256", "candidate_canonical_sha256",
            "candidate_diff_path", "candidate_diff_sha256", "completion_contract",
            "completion_crash_matrix",
        }
        if (not isinstance(lineage, dict) or set(lineage) != expected_lineage_fields
                or lineage.get("candidate_path") != "R6-A-R66-ATOMIC-COMPLETION-CANDIDATE-3.json"):
            raise RuntimeError("R70 candidate lacks the exact Candidate 3 completion lineage")
        c3_path, c3_raw = _repo_relative_file(
            selected_repo, lineage["candidate_path"], label="Candidate 3 lineage")
        c3 = json.loads(c3_raw, object_pairs_hook=_json_object_without_duplicate_keys)
        c3_digest = hashlib.sha256(c3_raw).hexdigest()
        c3_canonical = hashlib.sha256(build_retention._canonical_json(c3)).hexdigest()
        c3_diff_path, c3_diff_raw = _repo_relative_file(
            selected_repo, lineage["candidate_diff_path"], label="Candidate 3 diff", maximum_bytes=32 * 1024 * 1024)
        if (lineage.get("candidate_raw_sha256") != c3_digest
                or lineage.get("candidate_canonical_sha256") != c3_canonical
                or hashlib.sha256(c3_diff_raw).hexdigest() != lineage.get("candidate_diff_sha256")
                or lineage.get("completion_contract") != c3.get("completion_contract")
                or lineage.get("completion_crash_matrix") != c3.get("crash_matrix")
                or candidate.get("completion_contract") != c3.get("completion_contract")):
            raise RuntimeError("R70 Candidate 3 completion lineage changed or does not match the frozen contract")

    contract_desc = binding.get("completion_contract")
    candidate_contract = candidate.get("completion_contract")
    contract_fields = {
        "path", "sha256", "manifest_commit_contract_path", "manifest_commit_contract_sha256",
    }
    if (not isinstance(contract_desc, dict)
            or set(contract_desc) != contract_fields
            or contract_desc != candidate_contract):
        raise RuntimeError("Fresh R66 completion contract binding differs from the candidate")
    contract_path, contract_bytes = _repo_relative_file(
        selected_repo, contract_desc.get("path"), label="completion contract")
    contract_sha256 = hashlib.sha256(contract_bytes).hexdigest()
    if contract_sha256 != contract_desc.get("sha256"):
        raise RuntimeError("Fresh R66 completion contract changed after review")
    manifest_contract_path, manifest_contract_bytes = _repo_relative_file(
        selected_repo, contract_desc.get("manifest_commit_contract_path"),
        label="manifest commit contract")
    manifest_contract_sha256 = hashlib.sha256(manifest_contract_bytes).hexdigest()
    if manifest_contract_sha256 != contract_desc.get("manifest_commit_contract_sha256"):
        raise RuntimeError("Fresh R66 manifest commit contract changed after review")

    crash_desc = binding.get("crash_matrix")
    if not isinstance(crash_desc, dict) or crash_desc != candidate.get("crash_matrix"):
        raise RuntimeError("Fresh R66 crash-matrix binding differs from the candidate")
    crash_path, crash_bytes = _repo_relative_file(
        selected_repo, crash_desc.get("path"), label="crash matrix")
    if hashlib.sha256(crash_bytes).hexdigest() != crash_desc.get("sha256"):
        raise RuntimeError("Fresh R66 crash matrix changed after review")

    expected_completion_verdicts = [
        "PASS — BUILDLEASE SUCCESS CANNOT BECOME DURABLE BEFORE ALL COMPLETION IDENTITY VALIDATION AND MANIFEST COMMIT REQUIREMENTS ARE DURABLY SATISFIED",
        "PASS — HOST V3 DUAL-LINEAGE FINALIZATION STILL BINDS BOTH GRAPH IDENTITIES, BOTH GRAPH KEYS, THE FROZEN SESSION PLAN, AND OUTPUT LINEAGE",
    ]
    completion_verdicts = binding.get("completion_review_verdicts")
    completion_desc = binding.get("completion_review")
    if (completion_verdicts != expected_completion_verdicts
            or not isinstance(completion_desc, dict)
            or set(completion_desc) != {"path", "sha256"}):
        raise RuntimeError("Fresh R66 completion review does not carry both exact PASS verdicts")
    completion_path, completion_report_bytes = _repo_relative_file(
        selected_repo, completion_desc.get("path"), label="completion review",
        maximum_bytes=4 * 1024 * 1024)
    completion_report_sha256 = hashlib.sha256(completion_report_bytes).hexdigest()
    completion_report_text = completion_report_bytes.decode("utf-8", errors="strict")
    if (completion_report_sha256 != completion_desc.get("sha256")
            or any(verdict not in completion_report_text for verdict in expected_completion_verdicts)):
        raise RuntimeError("Fresh R66 completion review report changed after review")

    g0c_desc = binding.get("fresh_g0c_review")
    if (not isinstance(g0c_desc, dict) or set(g0c_desc) != {"path", "sha256"}
            or binding.get("fresh_g0c_answer")
                != "Can durable SUCCESS exist without a validated committed manifest? NO"):
        raise RuntimeError("Fresh R66 G0C report lacks the required explicit NO verdict")
    g0c_path, g0c_report_bytes = _repo_relative_file(
        selected_repo, g0c_desc.get("path"), label="G0C report", maximum_bytes=4 * 1024 * 1024)
    g0c_report_sha256 = hashlib.sha256(g0c_report_bytes).hexdigest()
    g0c_report_text = g0c_report_bytes.decode("utf-8", errors="strict")
    if milestone == "R86":
        try:
            g0c_answer = json.loads(
                g0c_report_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Fresh R86 G0C answer is malformed: {error}") from error
        if (g0c_desc.get("path") != R86_FRESH_G0C_REVIEW_PATH
                or not isinstance(g0c_answer, dict)
                or g0c_answer.get("milestone") != "R86"
                or g0c_answer.get("status") != "PASS"
                or g0c_answer.get("answer") != "NO"
                or g0c_answer.get("findings") != []
                or g0c_report_sha256 != g0c_desc.get("sha256")):
            raise RuntimeError("Fresh R86 G0C answer changed or lacks an explicit passing NO")
    elif milestone == "R87":
        try:
            g0c_answer = json.loads(
                g0c_report_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Fresh R87 G0C answer is malformed: {error}") from error
        if not isinstance(g0c_answer, dict):
            raise RuntimeError("Fresh R87 G0C answer is malformed")
        _validate_r87_fresh_g0c_answer(
            descriptor=g0c_desc, answer=g0c_answer,
            report_sha256=g0c_report_sha256, candidate_id=candidate_id,
            candidate_core_sha256=candidate_core_sha256 or "",
            plan_canonical_sha256=plan_canonical_sha256)
    elif (g0c_report_sha256 != g0c_desc.get("sha256")
          or "Can durable SUCCESS exist without a validated committed manifest?" not in g0c_report_text
          or not re.search(r"\bNO\b", g0c_report_text)):
        raise RuntimeError("Fresh R66 G0C report changed after review")

    exact_diff = candidate.get("exact_diff")
    if (not isinstance(exact_diff, dict) or set(exact_diff) != {"path", "sha256"}):
        raise RuntimeError("Frozen R66 candidate lacks its exact diff descriptor")
    diff_path, diff_bytes = _repo_relative_file(
        selected_repo, exact_diff.get("path"), label="candidate diff", maximum_bytes=32 * 1024 * 1024)
    diff_sha256 = hashlib.sha256(diff_bytes).hexdigest()
    if diff_sha256 != exact_diff.get("sha256"):
        raise RuntimeError("Frozen R66 candidate diff changed after review")

    transaction_contracts = candidate.get("transaction_contracts", {})
    if milestone == "R70":
        required_transaction_files = {
            "R6-A-R70-ORCHESTRATOR-RECONCILIATION.md",
            "R6-A-R70-POST-ALLOW-4K-ATTRIBUTION.md",
            "R6-A-R70-NONLEASE-MEASUREMENT-CONTRACT.md",
            "R6-A-R70-ADMISSION-TRANSACTION-CONTRACT.md",
            "R6-A-R70-ADMISSION-RESERVATION-HANDOFF-DESIGN.md",
            "R6-A-R70-ADMISSION-RESERVATION-CRASH-MATRIX.md",
            "R6-A-R70-EXTERNAL-DRIFT-SAFETY.md",
        }
        if (not isinstance(transaction_contracts, dict)
                or set(transaction_contracts) != required_transaction_files):
            raise RuntimeError("R70 candidate omits or adds an admission-transaction audit binding")
        for relative, expected_digest in transaction_contracts.items():
            _path, raw = _repo_relative_file(selected_repo, relative, label="R70 contract")
            if hashlib.sha256(raw).hexdigest() != expected_digest:
                raise RuntimeError(f"R70 admission-transaction contract changed: {relative}")
    elif milestone in {"R71", "R72", "R73", "R74", "R75", "R76", "R77"}:
        transaction_contracts = _validate_r71_transaction_contracts(
            candidate, repo=selected_repo)
    elif milestone == "R78":
        transaction_contracts = _validate_r78_transaction_contracts(
            candidate, repo=selected_repo)
    elif milestone == "R79":
        transaction_contracts = _validate_r79_transaction_contracts(
            candidate, repo=selected_repo)
        if "r78_binding" in review:
            preserved_path = selected_repo / "experiments/storage/g0c-launcher-independent-review-R78-PRESERVED.json"
            preserved_bytes = storage_policy._read_file_nofollow(
                preserved_path, maximum_bytes=4 * 1024 * 1024)
            try:
                preserved_review = json.loads(
                    preserved_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise RuntimeError(f"Preserved R78 G0C review is malformed: {error}") from error
            if (not isinstance(preserved_review, dict)
                    or set(preserved_review) != {"status", "findings", "reviewed_files", "r78_binding"}
                    or preserved_review.get("status") != "PASS"
                    or preserved_review.get("findings") != []
                    or review.get("r78_binding") != preserved_review.get("r78_binding")):
                raise RuntimeError("R79 G0C review changed or omitted the exact preserved R78 binding")
    elif milestone == "R80":
        transaction_contracts = _validate_r80_transaction_contracts(
            candidate, repo=selected_repo)
        preserved_r78_path = selected_repo / "experiments/storage/g0c-launcher-independent-review-R78-PRESERVED.json"
        preserved_r78_bytes = storage_policy._read_file_nofollow(
            preserved_r78_path, maximum_bytes=4 * 1024 * 1024)
        preserved_r79_path = selected_repo / R80_R79_G0C_PRESERVED_PATH
        preserved_r79_bytes = storage_policy._read_file_nofollow(
            preserved_r79_path, maximum_bytes=8 * 1024 * 1024)
        try:
            preserved_r78 = json.loads(
                preserved_r78_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
            preserved_r79 = json.loads(
                preserved_r79_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Preserved R78/R79 G0C review is malformed: {error}") from error
        if (not isinstance(preserved_r78, dict)
                or set(preserved_r78) != {"status", "findings", "reviewed_files", "r78_binding"}
                or preserved_r78.get("status") != "PASS"
                or preserved_r78.get("findings") != []
                or not isinstance(preserved_r79, dict)
                or set(preserved_r79) != {"status", "findings", "reviewed_files", "r78_binding", "r79_binding"}
                or preserved_r79.get("status") != "PASS"
                or preserved_r79.get("findings") != []
                or preserved_r79.get("r78_binding") != preserved_r78.get("r78_binding")
                or review.get("r78_binding") != preserved_r78.get("r78_binding")
                or review.get("r79_binding") != preserved_r79.get("r79_binding")):
            raise RuntimeError("R80 G0C review changed or omitted the byte-preserved R78/R79 lineage")
    elif milestone == "R81":
        transaction_contracts = _validate_r81_transaction_contracts(
            candidate, repo=selected_repo)
        preserved_r80_path = selected_repo / R81_R80_G0C_PRESERVED_PATH
        preserved_r80_bytes = storage_policy._read_file_nofollow(
            preserved_r80_path, maximum_bytes=8 * 1024 * 1024)
        try:
            preserved_r80 = json.loads(
                preserved_r80_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Preserved R80 G0C review is malformed: {error}") from error
        if (not isinstance(preserved_r80, dict)
                or set(preserved_r80) != {"status", "findings", "reviewed_files",
                                         "r78_binding", "r79_binding", "r80_binding"}
                or preserved_r80.get("status") != "PASS"
                or preserved_r80.get("findings") != []
                or review.get("r80_binding") != preserved_r80.get("r80_binding")
                or review.get("r79_binding") != preserved_r80.get("r79_binding")
                or review.get("r78_binding") != preserved_r80.get("r78_binding")):
            raise RuntimeError("R81 G0C review changed or omitted the byte-preserved R78/R79/R80 lineage")
    elif milestone == "R86":
        transaction_contracts = _validate_r86_transaction_contracts(
            candidate, repo=selected_repo)
        preserved_r81_path = selected_repo / R86_R81_G0C_PRESERVED_PATH
        preserved_r81_bytes = storage_policy._read_file_nofollow(
            preserved_r81_path, maximum_bytes=8 * 1024 * 1024)
        try:
            preserved_r81 = json.loads(
                preserved_r81_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Preserved R81 G0C review is malformed: {error}") from error
        if (not isinstance(preserved_r81, dict)
                or set(preserved_r81) != {"status", "findings", "reviewed_files",
                                         "r78_binding", "r79_binding", "r80_binding", "r81_binding"}
                or preserved_r81.get("status") != "PASS"
                or preserved_r81.get("findings") != []
                or review.get("r78_binding") != preserved_r81.get("r78_binding")
                or review.get("r79_binding") != preserved_r81.get("r79_binding")
                or review.get("r80_binding") != preserved_r81.get("r80_binding")
                or review.get("r81_binding") != preserved_r81.get("r81_binding")):
            raise RuntimeError("R86 G0C review changed or omitted the byte-preserved R78/R79/R80/R81 lineage")
    elif milestone == "R87":
        transaction_contracts = _validate_r87_transaction_contracts(
            candidate, repo=selected_repo)
        preserved_r86_path = selected_repo / R87_R86_G0C_PRESERVED_PATH
        preserved_r86_bytes = storage_policy._read_file_nofollow(
            preserved_r86_path, maximum_bytes=12 * 1024 * 1024)
        preserved_r86_sha256 = hashlib.sha256(preserved_r86_bytes).hexdigest()
        preserved_expected_sha256 = candidate.get("preserved_r86_g0c_sha256")
        if (not re.fullmatch(r"[0-9a-f]{64}", str(preserved_expected_sha256 or ""))
                or preserved_r86_sha256 != preserved_expected_sha256):
            raise RuntimeError("R87 candidate does not preserve the exact prior active R86 G0C record")
        try:
            preserved_r86 = json.loads(
                preserved_r86_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Preserved R86 G0C review is malformed: {error}") from error
        if (not isinstance(preserved_r86, dict)
                or preserved_r86.get("status") != "PASS"
                or preserved_r86.get("findings") != []
                or "r86_binding" not in preserved_r86):
            raise RuntimeError("R87 preserved R86 G0C record is not the exact passing historical chain")

    changed_source_hashes = candidate.get("changed_source_hashes")
    changed_test_hashes = candidate.get("changed_test_hashes")
    expected_source_paths = {"R66": R66_CHANGED_SOURCE_FILES,
                             "R69": R69_CHANGED_SOURCE_FILES,
                             "R70": R70_CHANGED_SOURCE_FILES,
                             "R71": R71_CHANGED_SOURCE_FILES,
                             "R72": R72_CHANGED_SOURCE_FILES,
                             "R73": R73_CHANGED_SOURCE_FILES,
                             "R74": R74_CHANGED_SOURCE_FILES,
                             "R75": R75_CHANGED_SOURCE_FILES,
                             "R76": R76_CHANGED_SOURCE_FILES,
                             "R77": R77_CHANGED_SOURCE_FILES,
                             "R78": R78_CHANGED_SOURCE_FILES,
                             "R79": R79_CHANGED_SOURCE_FILES,
                             "R80": R80_CHANGED_SOURCE_FILES,
                             "R81": R81_CHANGED_SOURCE_FILES,
                             "R86": R86_CHANGED_SOURCE_FILES,
                             "R87": R87_CHANGED_SOURCE_FILES}[milestone]
    expected_test_paths = {"R66": R66_CHANGED_TEST_FILES,
                           "R69": R69_CHANGED_TEST_FILES,
                           "R70": R70_CHANGED_TEST_FILES,
                           "R71": R71_CHANGED_TEST_FILES,
                           "R72": R72_CHANGED_TEST_FILES,
                           "R73": R73_CHANGED_TEST_FILES,
                           "R74": R74_CHANGED_TEST_FILES,
                           "R75": R75_CHANGED_TEST_FILES,
                           "R76": R76_CHANGED_TEST_FILES,
                           "R77": R77_CHANGED_TEST_FILES,
                           "R78": R78_CHANGED_TEST_FILES,
                           "R79": R79_CHANGED_TEST_FILES,
                           "R80": R80_CHANGED_TEST_FILES,
                           "R81": R81_CHANGED_TEST_FILES,
                           "R86": R86_CHANGED_TEST_FILES,
                           "R87": R87_CHANGED_TEST_FILES}[milestone]
    if (not isinstance(changed_source_hashes, dict)
            or set(changed_source_hashes) != expected_source_paths
            or not isinstance(changed_test_hashes, dict)
            or set(changed_test_hashes) != expected_test_paths):
        raise RuntimeError(f"Frozen {milestone} candidate omits or adds source or test files")
    expected_file_paths = set(changed_source_hashes) | set(changed_test_hashes) | {
        plan_relative, contract_desc["path"], crash_desc["path"], g0c_desc["path"],
        contract_desc["manifest_commit_contract_path"], completion_desc["path"],
        exact_diff["path"],
    }
    if milestone == "R70":
        expected_file_paths.update(transaction_contracts)
        expected_file_paths.update({lineage["candidate_path"], lineage["candidate_diff_path"]})
    elif milestone in {"R71", "R72", "R73", "R74", "R75", "R76", "R77", "R78", "R79", "R80", "R81", "R86", "R87"}:
        expected_file_paths.update(transaction_contracts)
    file_rows = binding.get("files")
    if not isinstance(file_rows, dict) or set(file_rows) != expected_file_paths:
        raise RuntimeError("Fresh R66 G0C file binding omits or adds candidate inputs")
    for relative in sorted(expected_file_paths):
        maximum_bytes = (96 * 1024 * 1024
                         if milestone in {"R79", "R80", "R81", "R86", "R87"} and relative.endswith(
                             f"/{R79_R77_DISPOSITION_ID}/gate0-preflight-attestation.json")
                         else 32 * 1024 * 1024)
        path, raw = _repo_relative_file(selected_repo, relative, label="reviewed input",
                                        maximum_bytes=maximum_bytes)
        digest = hashlib.sha256(raw).hexdigest()
        if relative in changed_source_hashes and changed_source_hashes[relative] != digest:
            raise RuntimeError(f"Frozen R66 candidate source hash is stale: {relative}")
        if relative in changed_test_hashes and changed_test_hashes[relative] != digest:
            raise RuntimeError(f"Frozen R66 candidate test hash is stale: {relative}")
        if file_rows.get(relative) != digest:
            raise RuntimeError(f"Fresh R66 G0C review does not bind current {relative}")
    dependency_manifest = None
    g0c_core_sha256 = None
    if milestone in {"R71", "R72", "R73", "R74", "R75", "R76", "R77", "R78", "R79", "R80", "R81", "R86", "R87"}:
        dependency_manifest = binding.get("execution_dependency_manifest")
        if (not isinstance(dependency_manifest, dict)
                or set(dependency_manifest) != {"path", "sha256"}
                or dependency_manifest.get("path") != (
                    r6a_dependency_manifest.manifest_name_for_candidate(candidate_id)
                    if milestone == "R87" else
                    R86_DEPENDENCY_MANIFEST_PATH if milestone == "R86" else
                    R81_DEPENDENCY_MANIFEST_PATH if milestone == "R81" else
                    R80_DEPENDENCY_MANIFEST_PATH if milestone == "R80" else
                    R79_DEPENDENCY_MANIFEST_PATH if milestone == "R79" else
                    R78_DEPENDENCY_MANIFEST_PATH if milestone == "R78" else
                    R77_DEPENDENCY_MANIFEST_PATH if milestone == "R77" else
                    R76_DEPENDENCY_MANIFEST_PATH if milestone == "R76" else
                    R75_DEPENDENCY_MANIFEST_PATH if milestone == "R75" else
                    R74_DEPENDENCY_MANIFEST_PATH if milestone == "R74" else
                    R73_DEPENDENCY_MANIFEST_PATH if milestone == "R73" else
                    R72_DEPENDENCY_MANIFEST_PATH if milestone == "R72" else
                    R71_DEPENDENCY_MANIFEST_PATH)
                or not isinstance(dependency_manifest.get("sha256"), str)
                or re.fullmatch(r"[0-9a-f]{64}", dependency_manifest["sha256"]) is None):
            raise RuntimeError("R71 G0C envelope lacks the exact canonical dependency-manifest digest")
        try:
            r71_binding_identity = r6a_dependency_manifest.validate_candidate_g0c_binding(
                candidate=candidate, g0c_review=review,
                candidate_path=candidate_binding["path"],
                manifest_sha256=dependency_manifest["sha256"])
        except r6a_dependency_manifest.DependencyManifestError as error:
            raise RuntimeError(f"R71 candidate/G0C binding DAG is invalid: {error}") from error
        g0c_core_sha256 = r71_binding_identity["g0c_sha256"]
    return {
        "binding_sha256": hashlib.sha256(build_retention._canonical_json(binding)).hexdigest(),
        "contract_sha256": contract_sha256,
        "manifest_commit_contract_sha256": manifest_contract_sha256,
        "candidate_sha256": candidate_sha256,
        "candidate_canonical_sha256": candidate_canonical_sha256,
        "candidate_diff_sha256": diff_sha256,
        "g0c_report_sha256": g0c_report_sha256,
        "completion_contract_path": str(contract_path),
        "crash_matrix_path": str(crash_path),
        "g0c_report_path": str(g0c_path),
        "completion_review_path": str(completion_path),
        "manifest_commit_contract_path": str(manifest_contract_path),
        "candidate_path": str(candidate_path),
        "plan_file_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "candidate_raw_sha256": candidate_sha256,
        "candidate_canonical_sha256": candidate_canonical_sha256,
        "candidate_id": candidate_id,
        "candidate_core_sha256": candidate_core_sha256,
        "plan_canonical_sha256": plan_canonical_sha256,
        "dependency_manifest": dependency_manifest,
        "g0c_core_sha256": g0c_core_sha256,
    }


def _assert_r66_admission_id_unclaimed(admission_id: str) -> None:
    """Refuse reuse of an R66/R69 ID with a receipt or durable external claim."""
    if _r6a_id_milestone(admission_id, _R6A_ADMISSION_ID_PATTERN) is None:
        raise RuntimeError("R66/R69 admission ID is not path-safe")
    manifest_path = Path(storage_policy._experiment_manifest_path(admission_id))
    if manifest_path.exists() or manifest_path.is_symlink():
        raise RuntimeError(f"R66 admission ID has already been used: {admission_id}")
    storage_policy._assert_r66_r69_admission_id_unclaimed(admission_id)


def _assert_r66_build_id_unconsumed(build_id: str) -> None:
    """Refuse reuse of an ID present in any durable BuildLease identity history."""
    if _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN) is None:
        raise RuntimeError("R66/R69 build ID is not path-safe")
    # Keep the pre-admission guard aligned with BuildLease reservation: durable
    # lease, manifest, failure/evidence, receipt, and orphan-closeout history
    # all consume the identity even when BUILD_ROOT no longer contains it.
    build_retention._assert_build_id_unclaimed(build_id)


def run_r6a_execution(explicit_plan_path: str | Path) -> dict:
    start_time = time.time()
    print("==================================================")
    print("R66 SAME-PROCESS R6-A EXECUTION")
    print("Role: R6-A Execution Owner (GPT-6 Luna High)")
    print("==================================================")

    # 1. Load the reviewed fresh R6-A plan. The highest milestone override
    #    takes precedence while retaining prior variables for historical reviews.
    if explicit_plan_path is None:
        raise ValueError("R6-A execution requires an explicit plan identity")
    plan_path = Path(explicit_plan_path).expanduser()
    if not plan_path.is_absolute():
        plan_path = REPO / plan_path
    plan_path = plan_path.resolve(strict=True)
    if not _is_fresh_r6a_plan_path(plan_path):
        raise RuntimeError("Execution requires a fresh R6-A plan file inside this repository")

    plan_bytes = storage_policy._read_file_nofollow(plan_path)
    plan_file_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    try:
        plan = json.loads(plan_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Fresh R66 plan is malformed or contains duplicate keys: {error}") from error

    if not isinstance(plan, dict):
        raise RuntimeError("Fresh R66 recovery plan must be a JSON object")
    _validate_r66_recovery_plan(plan)
    build_id = plan["build_id"]
    admission_id = plan["admission_identity"]["admission_id"]
    reservation_bytes = plan["reservation_bytes"]
    build_path = build_retention.BUILD_ROOT / build_id

    # Validate finite Host-v3 plan contract
    src_hash = plan["source_identity"]["sha256"]
    validated_plan = build_retention._session_validate_plan(
        plan, build_id=build_id, build_path=build_path,
        source_tree_hash=src_hash, expected_bytes=reservation_bytes
    )
    _validate_r66_recovery_plan(validated_plan)
    plan_canonical = build_retention._canonical_json(validated_plan)
    plan_sha256 = hashlib.sha256(plan_canonical).hexdigest()
    _assert_r66_build_id_unconsumed(build_id)
    _assert_r66_admission_id_unclaimed(admission_id)

    print(f"[OK] Fresh R6-A execution plan loaded: {plan_path.name}")
    print(f"     Build ID: {build_id}")
    print(f"     Admission ID: {admission_id}")
    print(f"     Reservation: {reservation_bytes:,} bytes")
    print(f"     Canonical plan SHA-256: {plan_sha256}")

    # 2. Verify Gate 0 Attestation
    gate_attestation_bytes = storage_policy._read_file_nofollow(storage_policy.GATE0_ATTESTATION_PATH)
    gate_attestation = json.loads(gate_attestation_bytes)
    if gate_attestation.get("status") != "PASS":
        raise RuntimeError(f"Gate 0 attestation status is {gate_attestation.get('status')}, expected PASS")
    gate_attestation_sha = hashlib.sha256(gate_attestation_bytes).hexdigest()
    print(f"[OK] Gate 0 attestation verified: status=PASS, sha256={gate_attestation_sha}")

    inventory_bytes = storage_policy._read_file_nofollow(storage_policy.INVENTORY_PATH)
    inventory_sha = hashlib.sha256(inventory_bytes).hexdigest()
    if gate_attestation.get("inventory_sha256") != inventory_sha:
        raise RuntimeError("Fresh Gate 0 attestation does not bind the current storage inventory")
    selected_launcher_review_path = (
        R96_LAUNCHER_G0C_REVIEW_PATH if plan_path.name == R96_PLAN_NAME else
        R95_LAUNCHER_G0C_REVIEW_PATH if plan_path.name == R95_PLAN_NAME else
        R94_LAUNCHER_G0C_REVIEW_PATH if plan_path.name == R94_PLAN_NAME else
        R93_LAUNCHER_G0C_REVIEW_PATH if plan_path.name == R93_PLAN_NAME else
        R92_LAUNCHER_G0C_REVIEW_PATH if plan_path.name == R92_PLAN_NAME else
        R91_LAUNCHER_G0C_REVIEW_PATH if plan_path.name == R91_PLAN_NAME else
        R90_LAUNCHER_G0C_REVIEW_PATH if plan_path.name == R90_PLAN_NAME else
        R89_LAUNCHER_G0C_REVIEW_PATH if plan_path.name == R87_CANDIDATE_8_PLAN_NAME else
        storage_policy.G0C_REVIEW_RECORD_NAME)
    g0c_review_path = REPO / "experiments/storage" / Path(selected_launcher_review_path).name
    g0c_review_bytes = storage_policy._read_file_nofollow(g0c_review_path)
    # The canonical admission field `g0c_review_sha256` continues to identify
    # the Gate-pinned global launcher review. R89's candidate-specific review
    # has its own `r66_fresh_g0c_report_sha256` field below.
    g0c_review_sha = hashlib.sha256(g0c_review_bytes).hexdigest()
    r89_supplemental_files = None
    r90_supplemental_files = None
    r91_supplemental_files = None
    r92_supplemental_files = None
    r93_supplemental_files = None
    r94_supplemental_files = None
    r95_supplemental_files = None
    r96_supplemental_files = None
    if (plan_path.name == R87_CANDIDATE_8_PLAN_NAME
            and _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN) == "R87"):
        candidate_review_path = REPO / R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH
        candidate_review_bytes = storage_policy._read_file_nofollow(candidate_review_path)
        r66_review_binding = _validate_r89_g0c_reviews(
            global_review_bytes=g0c_review_bytes,
            candidate_review_bytes=candidate_review_bytes,
            gate_attestation=gate_attestation, repo=REPO,
            plan_path=plan_path, plan_bytes=plan_bytes,
            plan_canonical_sha256=plan_sha256,
            validated_plan=validated_plan)
        candidate_report_sha = r66_review_binding["g0c_report_sha256"]
        dependency_descriptor = r66_review_binding["dependency_manifest"]
        candidate_manifest_path = REPO / dependency_descriptor["path"]
        r89_supplemental_files = {
            Path(R89_LAUNCHER_G0C_REVIEW_PATH).name: (
                REPO / R89_LAUNCHER_G0C_REVIEW_PATH,
                R89_LAUNCHER_G0C_REVIEW_PATH, g0c_review_sha),
            Path(R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH).name: (
                candidate_review_path, R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH,
                candidate_report_sha),
            dependency_descriptor["path"]: (
                candidate_manifest_path, dependency_descriptor["path"],
                dependency_descriptor["sha256"]),
        }
        # The R6-A provenance field names the candidate review actually consumed.
        g0c_review_path = candidate_review_path
    elif (plan_path.name == R96_PLAN_NAME
            and _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN) == "R87"):
        candidate_review_path = REPO / R96_CANDIDATE_27_FRESH_G0C_REVIEW_PATH
        candidate_review_bytes = storage_policy._read_file_nofollow(candidate_review_path)
        r66_review_binding = _validate_r90_g0c_reviews(
            global_review_bytes=g0c_review_bytes,
            candidate_review_bytes=candidate_review_bytes,
            gate_attestation=gate_attestation, repo=REPO,
            plan_path=plan_path, plan_bytes=plan_bytes,
            plan_canonical_sha256=plan_sha256,
            validated_plan=validated_plan,
            candidate_id=R96_CANDIDATE_ID,
            candidate_review_path=R96_CANDIDATE_27_FRESH_G0C_REVIEW_PATH,
            plan_name=R96_PLAN_NAME,
            launcher_review_path=R96_LAUNCHER_G0C_REVIEW_PATH,
            changed_source_files=R96_CHANGED_SOURCE_FILES,
            changed_test_files=R96_CHANGED_TEST_FILES,
            behavior_verdicts=R96_BEHAVIOR_VERDICTS,
            required_rows=R96_REQUIRED_VALIDATION_EVIDENCE,
            evidence_basenames=R96_EVIDENCE_BASENAMES,
            run_prefix="R96",
            correctness_checks=R96_CORRECTNESS_SAFETY_CHECK_NAMES,
            provenance_checks=R96_PROVENANCE_CHECK_NAMES,
            reviewer_identities=R96_REVIEWER_IDENTITIES,
            mutation_events=R90_MUTATION_LEDGER_EVENTS)
        candidate_report_sha = r66_review_binding["g0c_report_sha256"]
        dependency_descriptor = r66_review_binding["dependency_manifest"]
        candidate_manifest_path = REPO / dependency_descriptor["path"]
        r96_supplemental_files = {
            Path(R96_LAUNCHER_G0C_REVIEW_PATH).name: (
                REPO / R96_LAUNCHER_G0C_REVIEW_PATH,
                R96_LAUNCHER_G0C_REVIEW_PATH, g0c_review_sha),
            Path(R96_CANDIDATE_27_FRESH_G0C_REVIEW_PATH).name: (
                candidate_review_path, R96_CANDIDATE_27_FRESH_G0C_REVIEW_PATH,
                candidate_report_sha),
            dependency_descriptor["path"]: (
                candidate_manifest_path, dependency_descriptor["path"],
                dependency_descriptor["sha256"]),
        }
        g0c_review_path = candidate_review_path
    elif (plan_path.name == R95_PLAN_NAME
            and _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN) == "R87"):
        candidate_review_path = REPO / R95_CANDIDATE_26_FRESH_G0C_REVIEW_PATH
        candidate_review_bytes = storage_policy._read_file_nofollow(candidate_review_path)
        r66_review_binding = _validate_r90_g0c_reviews(
            global_review_bytes=g0c_review_bytes,
            candidate_review_bytes=candidate_review_bytes,
            gate_attestation=gate_attestation, repo=REPO,
            plan_path=plan_path, plan_bytes=plan_bytes,
            plan_canonical_sha256=plan_sha256,
            validated_plan=validated_plan,
            candidate_id=R95_CANDIDATE_ID,
            candidate_review_path=R95_CANDIDATE_26_FRESH_G0C_REVIEW_PATH,
            plan_name=R95_PLAN_NAME,
            launcher_review_path=R95_LAUNCHER_G0C_REVIEW_PATH,
            changed_source_files=R95_CHANGED_SOURCE_FILES,
            changed_test_files=R95_CHANGED_TEST_FILES,
            behavior_verdicts=R95_BEHAVIOR_VERDICTS,
            required_rows=R95_REQUIRED_VALIDATION_EVIDENCE,
            evidence_basenames=R95_EVIDENCE_BASENAMES,
            run_prefix="R95",
            correctness_checks=R95_CORRECTNESS_SAFETY_CHECK_NAMES,
            provenance_checks=R95_PROVENANCE_CHECK_NAMES,
            reviewer_identities=R95_REVIEWER_IDENTITIES,
            mutation_events=R90_MUTATION_LEDGER_EVENTS)
        candidate_report_sha = r66_review_binding["g0c_report_sha256"]
        dependency_descriptor = r66_review_binding["dependency_manifest"]
        candidate_manifest_path = REPO / dependency_descriptor["path"]
        r95_supplemental_files = {
            Path(R95_LAUNCHER_G0C_REVIEW_PATH).name: (
                REPO / R95_LAUNCHER_G0C_REVIEW_PATH,
                R95_LAUNCHER_G0C_REVIEW_PATH, g0c_review_sha),
            Path(R95_CANDIDATE_26_FRESH_G0C_REVIEW_PATH).name: (
                candidate_review_path, R95_CANDIDATE_26_FRESH_G0C_REVIEW_PATH,
                candidate_report_sha),
            dependency_descriptor["path"]: (
                candidate_manifest_path, dependency_descriptor["path"],
                dependency_descriptor["sha256"]),
        }
        g0c_review_path = candidate_review_path
    elif (plan_path.name == R94_PLAN_NAME
            and _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN) == "R87"):
        candidate_review_path = REPO / R94_CANDIDATE_25_FRESH_G0C_REVIEW_PATH
        candidate_review_bytes = storage_policy._read_file_nofollow(candidate_review_path)
        r66_review_binding = _validate_r90_g0c_reviews(
            global_review_bytes=g0c_review_bytes,
            candidate_review_bytes=candidate_review_bytes,
            gate_attestation=gate_attestation, repo=REPO,
            plan_path=plan_path, plan_bytes=plan_bytes,
            plan_canonical_sha256=plan_sha256,
            validated_plan=validated_plan,
            candidate_id=R94_CANDIDATE_ID,
            candidate_review_path=R94_CANDIDATE_25_FRESH_G0C_REVIEW_PATH,
            plan_name=R94_PLAN_NAME,
            launcher_review_path=R94_LAUNCHER_G0C_REVIEW_PATH,
            changed_source_files=R94_CHANGED_SOURCE_FILES,
            changed_test_files=R94_CHANGED_TEST_FILES,
            behavior_verdicts=R94_BEHAVIOR_VERDICTS,
            required_rows=R94_REQUIRED_VALIDATION_EVIDENCE,
            evidence_basenames=R94_EVIDENCE_BASENAMES,
            run_prefix="R94",
            correctness_checks=R94_CORRECTNESS_SAFETY_CHECK_NAMES,
            provenance_checks=R94_PROVENANCE_CHECK_NAMES,
            reviewer_identities=R94_REVIEWER_IDENTITIES,
            mutation_events=R90_MUTATION_LEDGER_EVENTS)
        candidate_report_sha = r66_review_binding["g0c_report_sha256"]
        dependency_descriptor = r66_review_binding["dependency_manifest"]
        candidate_manifest_path = REPO / dependency_descriptor["path"]
        r94_supplemental_files = {
            Path(R94_LAUNCHER_G0C_REVIEW_PATH).name: (
                REPO / R94_LAUNCHER_G0C_REVIEW_PATH,
                R94_LAUNCHER_G0C_REVIEW_PATH, g0c_review_sha),
            Path(R94_CANDIDATE_25_FRESH_G0C_REVIEW_PATH).name: (
                candidate_review_path, R94_CANDIDATE_25_FRESH_G0C_REVIEW_PATH,
                candidate_report_sha),
            dependency_descriptor["path"]: (
                candidate_manifest_path, dependency_descriptor["path"],
                dependency_descriptor["sha256"]),
        }
        g0c_review_path = candidate_review_path
    elif (plan_path.name == R93_PLAN_NAME
            and _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN) == "R87"):
        candidate_review_path = REPO / R93_CANDIDATE_24_FRESH_G0C_REVIEW_PATH
        candidate_review_bytes = storage_policy._read_file_nofollow(candidate_review_path)
        r66_review_binding = _validate_r90_g0c_reviews(
            global_review_bytes=g0c_review_bytes,
            candidate_review_bytes=candidate_review_bytes,
            gate_attestation=gate_attestation, repo=REPO,
            plan_path=plan_path, plan_bytes=plan_bytes,
            plan_canonical_sha256=plan_sha256,
            validated_plan=validated_plan,
            candidate_id=R93_CANDIDATE_ID,
            candidate_review_path=R93_CANDIDATE_24_FRESH_G0C_REVIEW_PATH,
            plan_name=R93_PLAN_NAME,
            launcher_review_path=R93_LAUNCHER_G0C_REVIEW_PATH,
            changed_source_files=R93_CHANGED_SOURCE_FILES,
            changed_test_files=R93_CHANGED_TEST_FILES,
            behavior_verdicts=R93_BEHAVIOR_VERDICTS,
            required_rows=R93_REQUIRED_VALIDATION_EVIDENCE,
            evidence_basenames=R93_EVIDENCE_BASENAMES,
            run_prefix="R93",
            correctness_checks=R93_CORRECTNESS_SAFETY_CHECK_NAMES,
            provenance_checks=R93_PROVENANCE_CHECK_NAMES,
            reviewer_identities=R93_REVIEWER_IDENTITIES,
            mutation_events=R90_MUTATION_LEDGER_EVENTS)
        candidate_report_sha = r66_review_binding["g0c_report_sha256"]
        dependency_descriptor = r66_review_binding["dependency_manifest"]
        candidate_manifest_path = REPO / dependency_descriptor["path"]
        r93_supplemental_files = {
            Path(R93_LAUNCHER_G0C_REVIEW_PATH).name: (
                REPO / R93_LAUNCHER_G0C_REVIEW_PATH,
                R93_LAUNCHER_G0C_REVIEW_PATH, g0c_review_sha),
            Path(R93_CANDIDATE_24_FRESH_G0C_REVIEW_PATH).name: (
                candidate_review_path, R93_CANDIDATE_24_FRESH_G0C_REVIEW_PATH,
                candidate_report_sha),
            dependency_descriptor["path"]: (
                candidate_manifest_path, dependency_descriptor["path"],
                dependency_descriptor["sha256"]),
        }
        g0c_review_path = candidate_review_path
    elif (plan_path.name == R92_PLAN_NAME
            and _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN) == "R87"):
        candidate_review_path = REPO / R92_CANDIDATE_23_FRESH_G0C_REVIEW_PATH
        candidate_review_bytes = storage_policy._read_file_nofollow(candidate_review_path)
        r66_review_binding = _validate_r90_g0c_reviews(
            global_review_bytes=g0c_review_bytes,
            candidate_review_bytes=candidate_review_bytes,
            gate_attestation=gate_attestation, repo=REPO,
            plan_path=plan_path, plan_bytes=plan_bytes,
            plan_canonical_sha256=plan_sha256,
            validated_plan=validated_plan,
            candidate_id=R92_CANDIDATE_ID,
            candidate_review_path=R92_CANDIDATE_23_FRESH_G0C_REVIEW_PATH,
            plan_name=R92_PLAN_NAME,
            launcher_review_path=R92_LAUNCHER_G0C_REVIEW_PATH,
            changed_source_files=R91_CHANGED_SOURCE_FILES,
            changed_test_files=R91_CHANGED_TEST_FILES,
            behavior_verdicts=R92_BEHAVIOR_VERDICTS,
            required_rows=R92_REQUIRED_VALIDATION_EVIDENCE,
            evidence_basenames=R92_EVIDENCE_BASENAMES,
            run_prefix="R92",
            correctness_checks=R92_CORRECTNESS_SAFETY_CHECK_NAMES,
            provenance_checks=R92_PROVENANCE_CHECK_NAMES,
            reviewer_identities=R92_REVIEWER_IDENTITIES,
            mutation_events=R90_MUTATION_LEDGER_EVENTS)
        candidate_report_sha = r66_review_binding["g0c_report_sha256"]
        dependency_descriptor = r66_review_binding["dependency_manifest"]
        candidate_manifest_path = REPO / dependency_descriptor["path"]
        r92_supplemental_files = {
            Path(R92_LAUNCHER_G0C_REVIEW_PATH).name: (
                REPO / R92_LAUNCHER_G0C_REVIEW_PATH,
                R92_LAUNCHER_G0C_REVIEW_PATH, g0c_review_sha),
            Path(R92_CANDIDATE_23_FRESH_G0C_REVIEW_PATH).name: (
                candidate_review_path, R92_CANDIDATE_23_FRESH_G0C_REVIEW_PATH,
                candidate_report_sha),
            dependency_descriptor["path"]: (
                candidate_manifest_path, dependency_descriptor["path"],
                dependency_descriptor["sha256"]),
        }
        g0c_review_path = candidate_review_path
    elif (plan_path.name == R91_PLAN_NAME
            and _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN) == "R87"):
        candidate_review_path = REPO / R91_CANDIDATE_21_FRESH_G0C_REVIEW_PATH
        candidate_review_bytes = storage_policy._read_file_nofollow(candidate_review_path)
        r66_review_binding = _validate_r90_g0c_reviews(
            global_review_bytes=g0c_review_bytes,
            candidate_review_bytes=candidate_review_bytes,
            gate_attestation=gate_attestation, repo=REPO,
            plan_path=plan_path, plan_bytes=plan_bytes,
            plan_canonical_sha256=plan_sha256,
            validated_plan=validated_plan,
            candidate_id=R91_CANDIDATE_ID,
            candidate_review_path=R91_CANDIDATE_21_FRESH_G0C_REVIEW_PATH,
            plan_name=R91_PLAN_NAME,
            launcher_review_path=R91_LAUNCHER_G0C_REVIEW_PATH,
            changed_source_files=R91_CHANGED_SOURCE_FILES,
            changed_test_files=R91_CHANGED_TEST_FILES,
            behavior_verdicts=R91_BEHAVIOR_VERDICTS,
            required_rows=R91_REQUIRED_VALIDATION_EVIDENCE,
            evidence_basenames=R91_EVIDENCE_BASENAMES,
            run_prefix="R91",
            correctness_checks=R91_CORRECTNESS_SAFETY_CHECK_NAMES,
            provenance_checks=R91_PROVENANCE_CHECK_NAMES,
            reviewer_identities=R91_REVIEWER_IDENTITIES,
            mutation_events=R90_MUTATION_LEDGER_EVENTS)
        candidate_report_sha = r66_review_binding["g0c_report_sha256"]
        dependency_descriptor = r66_review_binding["dependency_manifest"]
        candidate_manifest_path = REPO / dependency_descriptor["path"]
        r91_supplemental_files = {
            Path(R91_LAUNCHER_G0C_REVIEW_PATH).name: (
                REPO / R91_LAUNCHER_G0C_REVIEW_PATH,
                R91_LAUNCHER_G0C_REVIEW_PATH, g0c_review_sha),
            Path(R91_CANDIDATE_21_FRESH_G0C_REVIEW_PATH).name: (
                candidate_review_path, R91_CANDIDATE_21_FRESH_G0C_REVIEW_PATH,
                candidate_report_sha),
            dependency_descriptor["path"]: (
                candidate_manifest_path, dependency_descriptor["path"],
                dependency_descriptor["sha256"]),
        }
        g0c_review_path = candidate_review_path
    elif (plan_path.name == R90_PLAN_NAME
            and _r6a_id_milestone(build_id, _R6A_BUILD_ID_PATTERN) == "R87"):
        candidate_review_path = REPO / R90_CANDIDATE_19_FRESH_G0C_REVIEW_PATH
        candidate_review_bytes = storage_policy._read_file_nofollow(candidate_review_path)
        r66_review_binding = _validate_r90_g0c_reviews(
            global_review_bytes=g0c_review_bytes,
            candidate_review_bytes=candidate_review_bytes,
            gate_attestation=gate_attestation, repo=REPO,
            plan_path=plan_path, plan_bytes=plan_bytes,
            plan_canonical_sha256=plan_sha256,
            validated_plan=validated_plan)
        candidate_report_sha = r66_review_binding["g0c_report_sha256"]
        dependency_descriptor = r66_review_binding["dependency_manifest"]
        candidate_manifest_path = REPO / dependency_descriptor["path"]
        r90_supplemental_files = {
            Path(R90_LAUNCHER_G0C_REVIEW_PATH).name: (
                REPO / R90_LAUNCHER_G0C_REVIEW_PATH,
                R90_LAUNCHER_G0C_REVIEW_PATH, g0c_review_sha),
            Path(R90_CANDIDATE_19_FRESH_G0C_REVIEW_PATH).name: (
                candidate_review_path, R90_CANDIDATE_19_FRESH_G0C_REVIEW_PATH,
                candidate_report_sha),
            dependency_descriptor["path"]: (
                candidate_manifest_path, dependency_descriptor["path"],
                dependency_descriptor["sha256"]),
        }
        g0c_review_path = candidate_review_path
    else:
        r66_review_binding = _validate_r66_g0c_binding(
            g0c_review_bytes, repo=REPO, plan_path=plan_path,
            plan_bytes=plan_bytes, plan_canonical_sha256=plan_sha256,
            validated_plan=validated_plan)
        supplemental = gate_attestation.get("supplemental_evidence_sha256", {})
        if supplemental.get(storage_policy.G0C_REVIEW_RECORD_NAME) != g0c_review_sha:
            raise RuntimeError("Fresh Gate 0 attestation does not bind the current G0C review")
    smoke = gate_attestation.get("prefixlease_smoke")
    if (gate_attestation.get("prefixlease_smoke_status") != "PASS"
            or not isinstance(smoke, dict)):
        raise RuntimeError("Fresh Gate 0 attestation lacks a passing PrefixLease smoke receipt")
    smoke_report_path = Path(smoke["report_path"])
    smoke_report_bytes = storage_policy._read_file_nofollow(smoke_report_path)
    smoke_report_sha = hashlib.sha256(smoke_report_bytes).hexdigest()
    if smoke_report_sha != smoke.get("report_sha256"):
        raise RuntimeError("Fresh PrefixLease smoke report changed after Gate 0")

    # 3. Formulate Fresh Signed Admission
    runner_file = Path(__file__).resolve()
    admission_milestone = _r6a_id_milestone(admission_id, _R6A_ADMISSION_ID_PATTERN)
    milestone_number = int(admission_milestone[1:])
    is_r71 = milestone_number >= 71
    is_r70 = milestone_number == 70
    is_r7x = is_r70 or is_r71
    output_dir = (_r90_admission_output_dir(validated_plan)
                  if plan_path.name in {R90_PLAN_NAME, R91_PLAN_NAME, R92_PLAN_NAME, R93_PLAN_NAME,
                                        R94_PLAN_NAME, R95_PLAN_NAME, R96_PLAN_NAME}
                  else storage_policy.REPO / (
                      f"experiments/storage/r6a-r{milestone_number}-admission-output" if is_r7x
                      else "experiments/storage/r6a-r66-admission-output"))

    windows_cross_sha = next(
        row["sha256"] for row in validated_plan["input_receipts"]
        if row["path"] == "build-win64.txt")
    toolchain_receipts_sha = hashlib.sha256(build_retention._canonical_json({
        "tools": validated_plan["tools"],
        "host_dependency_receipts": validated_plan["host_dependency_receipts"],
        "runtime_receipts": validated_plan["runtime_receipts"],
    })).hexdigest()
    dep_state = {
        "policy_sha256": plan["policy_sha256"],
        "session_plan_sha256": plan_sha256,
        "source_tree_hash": src_hash,
        "status": "captured",
        "toolchain_receipt_sha256": toolchain_receipts_sha,
    }

    candidate_hashes = {
        "plan_file_sha256": plan_file_sha256,
        "plan_canonical_sha256": plan_sha256,
        "build_configuration_key_sha256": validated_plan["build_configuration_key_sha256"],
        "gate0_final_sha256": gate_attestation_sha,
        "source_tree_sha256": src_hash,
        "policy_sha256": plan["policy_sha256"],
        "inventory_sha256": inventory_sha,
        "g0c_review_sha256": g0c_review_sha,
        "r66_g0c_binding_sha256": r66_review_binding["binding_sha256"],
        "r66_candidate_sha256": r66_review_binding["candidate_sha256"],
        "r66_candidate_diff_sha256": r66_review_binding["candidate_diff_sha256"],
        "r66_completion_contract_sha256": r66_review_binding["contract_sha256"],
        "r66_fresh_g0c_report_sha256": r66_review_binding["g0c_report_sha256"],
        "windows_cross_file_sha256": windows_cross_sha,
        "toolchain_receipts_sha256": toolchain_receipts_sha,
        "graph_pair_key_sha256": validated_plan["build_configuration_key_sha256"],
        "r66_plan_file_sha256": plan_file_sha256,
        "r66_plan_canonical_sha256": plan_sha256,
        "prefix_smoke_report_sha256": smoke_report_sha,
    }

    additional_manifest_fields = {
        "build_session_plan_sha256": plan_sha256,
        "execution_admission_identity": plan["admission_identity"],
        "r66_g0c_binding_sha256": r66_review_binding["binding_sha256"],
        "r66_candidate_sha256": r66_review_binding["candidate_sha256"],
        "r66_completion_contract_sha256": r66_review_binding["contract_sha256"],
        "policy_sha256": plan["policy_sha256"],
        "schema_version": 3,
    }
    if plan_path.name in {R90_PLAN_NAME, R91_PLAN_NAME, R92_PLAN_NAME, R93_PLAN_NAME,
                          R94_PLAN_NAME, R95_PLAN_NAME, R96_PLAN_NAME}:
        additional_manifest_fields["admission_output_contract"] = R90_ADMISSION_OUTPUT_CONTRACT
        additional_manifest_fields["admission_output_dir"] = str(output_dir)
    if is_r7x:
        additional_manifest_fields["r71_build_id" if is_r71 else "r70_build_id"] = build_id

    print(f"Signing fresh admission for experiment {admission_id}...")
    allocation_lock = storage_policy._acquire_storage_allocation_lock() if is_r7x else None
    try:
        if r89_supplemental_files is not None:
            _assert_r89_plan_fresh_under_allocation_lock(
                plan_path=plan_path, expected_raw_sha256=plan_file_sha256,
                expected_canonical_sha256=plan_sha256, build_id=build_id,
                build_path=build_path, expected_bytes=reservation_bytes)
            _assert_r89_gate_fresh_under_allocation_lock(
                expected_gate_sha256=gate_attestation_sha,
                supplemental_files=r89_supplemental_files)
            candidate_review_path, _candidate_review_relative, candidate_review_sha = (
                r89_supplemental_files[
                    Path(R89_CANDIDATE_10_FRESH_G0C_REVIEW_PATH).name])
            _assert_r89_candidate_evidence_fresh_under_allocation_lock(
                candidate_review_path=candidate_review_path,
                expected_sha256=candidate_review_sha)
        elif r96_supplemental_files is not None:
            _assert_r90_plan_fresh_under_allocation_lock(
                plan_path=plan_path, expected_raw_sha256=plan_file_sha256,
                expected_canonical_sha256=plan_sha256, build_id=build_id,
                build_path=build_path, expected_bytes=reservation_bytes)
            _assert_r90_gate_fresh_under_allocation_lock(
                expected_gate_sha256=gate_attestation_sha,
                supplemental_files=r96_supplemental_files)
            candidate_review_path, _candidate_review_relative, candidate_review_sha = (
                r96_supplemental_files[
                    Path(R96_CANDIDATE_27_FRESH_G0C_REVIEW_PATH).name])
            _assert_r90_candidate_evidence_fresh_under_allocation_lock(
                candidate_review_path=candidate_review_path,
                expected_sha256=candidate_review_sha,
                candidate_id=R96_CANDIDATE_ID,
                behavior_verdicts=R96_BEHAVIOR_VERDICTS,
                required_rows=R96_REQUIRED_VALIDATION_EVIDENCE,
                evidence_basenames=R96_EVIDENCE_BASENAMES,
                run_prefix="R96",
                correctness_checks=R96_CORRECTNESS_SAFETY_CHECK_NAMES,
                provenance_checks=R96_PROVENANCE_CHECK_NAMES,
                reviewer_identities=R96_REVIEWER_IDENTITIES,
                mutation_events=R90_MUTATION_LEDGER_EVENTS)
        elif r95_supplemental_files is not None:
            _assert_r90_plan_fresh_under_allocation_lock(
                plan_path=plan_path, expected_raw_sha256=plan_file_sha256,
                expected_canonical_sha256=plan_sha256, build_id=build_id,
                build_path=build_path, expected_bytes=reservation_bytes)
            _assert_r90_gate_fresh_under_allocation_lock(
                expected_gate_sha256=gate_attestation_sha,
                supplemental_files=r95_supplemental_files)
            candidate_review_path, _candidate_review_relative, candidate_review_sha = (
                r95_supplemental_files[
                    Path(R95_CANDIDATE_26_FRESH_G0C_REVIEW_PATH).name])
            _assert_r90_candidate_evidence_fresh_under_allocation_lock(
                candidate_review_path=candidate_review_path,
                expected_sha256=candidate_review_sha,
                candidate_id=R95_CANDIDATE_ID,
                behavior_verdicts=R95_BEHAVIOR_VERDICTS,
                required_rows=R95_REQUIRED_VALIDATION_EVIDENCE,
                evidence_basenames=R95_EVIDENCE_BASENAMES,
                run_prefix="R95",
                correctness_checks=R95_CORRECTNESS_SAFETY_CHECK_NAMES,
                provenance_checks=R95_PROVENANCE_CHECK_NAMES,
                reviewer_identities=R95_REVIEWER_IDENTITIES,
                mutation_events=R90_MUTATION_LEDGER_EVENTS)
        elif r94_supplemental_files is not None:
            _assert_r90_plan_fresh_under_allocation_lock(
                plan_path=plan_path, expected_raw_sha256=plan_file_sha256,
                expected_canonical_sha256=plan_sha256, build_id=build_id,
                build_path=build_path, expected_bytes=reservation_bytes)
            _assert_r90_gate_fresh_under_allocation_lock(
                expected_gate_sha256=gate_attestation_sha,
                supplemental_files=r94_supplemental_files)
            candidate_review_path, _candidate_review_relative, candidate_review_sha = (
                r94_supplemental_files[
                    Path(R94_CANDIDATE_25_FRESH_G0C_REVIEW_PATH).name])
            _assert_r90_candidate_evidence_fresh_under_allocation_lock(
                candidate_review_path=candidate_review_path,
                expected_sha256=candidate_review_sha,
                candidate_id=R94_CANDIDATE_ID,
                behavior_verdicts=R94_BEHAVIOR_VERDICTS,
                required_rows=R94_REQUIRED_VALIDATION_EVIDENCE,
                evidence_basenames=R94_EVIDENCE_BASENAMES,
                run_prefix="R94",
                correctness_checks=R94_CORRECTNESS_SAFETY_CHECK_NAMES,
                provenance_checks=R94_PROVENANCE_CHECK_NAMES,
                reviewer_identities=R94_REVIEWER_IDENTITIES,
                mutation_events=R90_MUTATION_LEDGER_EVENTS)
        elif r93_supplemental_files is not None:
            _assert_r90_plan_fresh_under_allocation_lock(
                plan_path=plan_path, expected_raw_sha256=plan_file_sha256,
                expected_canonical_sha256=plan_sha256, build_id=build_id,
                build_path=build_path, expected_bytes=reservation_bytes)
            _assert_r90_gate_fresh_under_allocation_lock(
                expected_gate_sha256=gate_attestation_sha,
                supplemental_files=r93_supplemental_files)
            candidate_review_path, _candidate_review_relative, candidate_review_sha = (
                r93_supplemental_files[
                    Path(R93_CANDIDATE_24_FRESH_G0C_REVIEW_PATH).name])
            _assert_r90_candidate_evidence_fresh_under_allocation_lock(
                candidate_review_path=candidate_review_path,
                expected_sha256=candidate_review_sha,
                candidate_id=R93_CANDIDATE_ID,
                behavior_verdicts=R93_BEHAVIOR_VERDICTS,
                required_rows=R93_REQUIRED_VALIDATION_EVIDENCE,
                evidence_basenames=R93_EVIDENCE_BASENAMES,
                run_prefix="R93",
                correctness_checks=R93_CORRECTNESS_SAFETY_CHECK_NAMES,
                provenance_checks=R93_PROVENANCE_CHECK_NAMES,
                reviewer_identities=R93_REVIEWER_IDENTITIES,
                mutation_events=R90_MUTATION_LEDGER_EVENTS)
        elif r92_supplemental_files is not None:
            _assert_r90_plan_fresh_under_allocation_lock(
                plan_path=plan_path, expected_raw_sha256=plan_file_sha256,
                expected_canonical_sha256=plan_sha256, build_id=build_id,
                build_path=build_path, expected_bytes=reservation_bytes)
            _assert_r90_gate_fresh_under_allocation_lock(
                expected_gate_sha256=gate_attestation_sha,
                supplemental_files=r92_supplemental_files)
            candidate_review_path, _candidate_review_relative, candidate_review_sha = (
                r92_supplemental_files[
                    Path(R92_CANDIDATE_23_FRESH_G0C_REVIEW_PATH).name])
            _assert_r90_candidate_evidence_fresh_under_allocation_lock(
                candidate_review_path=candidate_review_path,
                expected_sha256=candidate_review_sha,
                candidate_id=R92_CANDIDATE_ID,
                behavior_verdicts=R92_BEHAVIOR_VERDICTS,
                required_rows=R92_REQUIRED_VALIDATION_EVIDENCE,
                evidence_basenames=R92_EVIDENCE_BASENAMES,
                run_prefix="R92",
                correctness_checks=R92_CORRECTNESS_SAFETY_CHECK_NAMES,
                provenance_checks=R92_PROVENANCE_CHECK_NAMES,
                reviewer_identities=R92_REVIEWER_IDENTITIES,
                mutation_events=R90_MUTATION_LEDGER_EVENTS)
        elif r91_supplemental_files is not None:
            _assert_r90_plan_fresh_under_allocation_lock(
                plan_path=plan_path, expected_raw_sha256=plan_file_sha256,
                expected_canonical_sha256=plan_sha256, build_id=build_id,
                build_path=build_path, expected_bytes=reservation_bytes)
            _assert_r90_gate_fresh_under_allocation_lock(
                expected_gate_sha256=gate_attestation_sha,
                supplemental_files=r91_supplemental_files)
            candidate_review_path, _candidate_review_relative, candidate_review_sha = (
                r91_supplemental_files[
                    Path(R91_CANDIDATE_21_FRESH_G0C_REVIEW_PATH).name])
            _assert_r90_candidate_evidence_fresh_under_allocation_lock(
                candidate_review_path=candidate_review_path,
                expected_sha256=candidate_review_sha,
                candidate_id=R91_CANDIDATE_ID,
                behavior_verdicts=R91_BEHAVIOR_VERDICTS,
                required_rows=R91_REQUIRED_VALIDATION_EVIDENCE,
                evidence_basenames=R91_EVIDENCE_BASENAMES,
                run_prefix="R91",
                correctness_checks=R91_CORRECTNESS_SAFETY_CHECK_NAMES,
                provenance_checks=R91_PROVENANCE_CHECK_NAMES,
                reviewer_identities=R91_REVIEWER_IDENTITIES,
                mutation_events=R90_MUTATION_LEDGER_EVENTS)
        elif r90_supplemental_files is not None:
            _assert_r90_plan_fresh_under_allocation_lock(
                plan_path=plan_path, expected_raw_sha256=plan_file_sha256,
                expected_canonical_sha256=plan_sha256, build_id=build_id,
                build_path=build_path, expected_bytes=reservation_bytes)
            _assert_r90_gate_fresh_under_allocation_lock(
                expected_gate_sha256=gate_attestation_sha,
                supplemental_files=r90_supplemental_files)
            candidate_review_path, _candidate_review_relative, candidate_review_sha = (
                r90_supplemental_files[
                    Path(R90_CANDIDATE_19_FRESH_G0C_REVIEW_PATH).name])
            _assert_r90_candidate_evidence_fresh_under_allocation_lock(
                candidate_review_path=candidate_review_path,
                expected_sha256=candidate_review_sha)
        if is_r71:
            candidate_path = Path(r66_review_binding["candidate_path"])
            candidate_raw = storage_policy._read_file_nofollow(candidate_path, maximum_bytes=4 * 1024 * 1024)
            candidate = json.loads(candidate_raw, object_pairs_hook=_json_object_without_duplicate_keys)
            manifest_descriptor = r66_review_binding["dependency_manifest"]
            manifest_path = REPO / manifest_descriptor["path"]
            r71_dependency_identity = r6a_dependency_manifest.validate_manifest(
                manifest_path=manifest_path, expected_sha256=manifest_descriptor["sha256"],
                candidate=candidate, plan=validated_plan,
                plan_raw_sha256=plan_file_sha256,
                plan_canonical_sha256=plan_sha256,
                g0c_sha256=r66_review_binding["g0c_core_sha256"],
                candidate_path=Path(candidate_path).relative_to(REPO).as_posix(),
                plan_path=plan_path.relative_to(REPO).as_posix(), repo=REPO)
            candidate_hashes.update({
                "r71_execution_dependency_manifest_sha256": r71_dependency_identity["sha256"],
                "r71_candidate_core_sha256": r71_dependency_identity["candidate_core_sha256"],
                "r71_dependency_plan_sha256": r71_dependency_identity["plan_raw_sha256"],
                "r71_g0c_core_sha256": r66_review_binding["g0c_core_sha256"],
            })
            additional_manifest_fields["r71_execution_dependency_manifest_sha256"] = (
                r71_dependency_identity["sha256"])
            additional_manifest_fields["r71_dependency_candidate_path"] = (
                Path(candidate_path).relative_to(REPO).as_posix())
            additional_manifest_fields["r71_dependency_plan_path"] = (
                plan_path.relative_to(REPO).as_posix())
            additional_manifest_fields["r71_dependency_g0c_review_path"] = (
                g0c_review_path.relative_to(REPO).as_posix())
            additional_manifest_fields["r71_dependency_manifest_path"] = manifest_descriptor["path"]
            additional_manifest_fields["r71_g0c_core_sha256"] = r71_dependency_identity["g0c_sha256"]
        else:
            r71_dependency_identity = None
        manifest = storage_policy.begin_experiment(
            experiment_id=admission_id,
            purpose=f"R{milestone_number if is_r7x else 66} same-process dual-lineage R6-A execution: Windows PE compile/link plus native PRODUCTION_PATH, UNIT, MODEL verification",
            candidate_hashes=candidate_hashes,
            expected_duration_seconds=43200,
            expected_prefix_bytes=0,
            expected_build_bytes=reservation_bytes,
            estimated_persistent_bytes=int(validated_plan.get("persistent_output_allowance_bytes", 0)),
            storage_justification=(
                f"Fresh R{milestone_number if is_r7x else 66} dual-lineage BuildLease of "
                f"{reservation_bytes} bytes plus signed durable finalization output allowance "
                f"of {int(validated_plan.get('persistent_output_allowance_bytes', 0))} bytes under 60 GiB budget."),
            authorized_runner_paths=[runner_file],
            build_source_tree_hash=src_hash,
            build_configuration_key_sha256=validated_plan["build_configuration_key_sha256"],
            build_kind="dxvk",
            build_type="release",
            dependency_state=dep_state,
            output_dir=output_dir,
            additional=additional_manifest_fields,
            _allocation_lock=allocation_lock,
        )
    except BaseException:
        if allocation_lock is not None:
            storage_policy._release_storage_allocation_lock(allocation_lock)
        raise

    if manifest.get("decision") != "ALLOW":
        raise RuntimeError(f"Admission refused: {manifest.get('blockers')}")
    print(f"[OK] Fresh R6-A admission signed: decision=ALLOW, manifest={manifest['manifest_path']}")
    print(f"     Projected scoped bytes: {manifest['projected_scoped_project_bytes']:,} <= {storage_policy.PROJECT_BUDGET:,}")
    print(f"     Projected free bytes:   {manifest['projected_free_bytes']:,} >= {storage_policy.FREE_MINIMUM:,}")

    # 4. Instantiate BuildLease
    lease = build_retention.BuildLease(
        build_id=build_id,
        kind="dxvk",
        owner_experiment=admission_id,
        purpose="R66 same-process dual-lineage R6-A execution: Windows PE compile/link plus native PRODUCTION_PATH, UNIT, MODEL verification",
        source_tree_hash=src_hash,
        expected_bytes=reservation_bytes,
        metadata={
            "build_type": "release",
            "dependency_state": dep_state,
            "failure_capture_paths": [],
            "estimated_debug_bytes": None,
        },
        preserve_debug_reason=None,
        runner_path=runner_file,
        configuration_key_sha256=validated_plan["build_configuration_key_sha256"],
        session_plan=validated_plan,
    )

    # 5. Reserve exactly ONE BuildLease
    print("\nReserving BuildLease (exactly ONE reservation)...")
    handoff = manifest.reservation_capability if is_r7x else None
    try:
        reserved_path = lease.reserve(admission_transaction=handoff) if is_r7x else lease.reserve()
    finally:
        # reserve owns and releases the same descriptor on both success and
        # failure. This fallback covers errors between ALLOW publication and
        # entering reserve without acquiring a second flock descriptor.
        if allocation_lock is not None:
            storage_policy._release_storage_allocation_lock(allocation_lock)
            allocation_lock = None
    if reserved_path != lease.path:
        raise RuntimeError(f"Reservation path mismatch: {reserved_path} != {lease.path}")
    print(f"[OK] BuildLease reserved at: {reserved_path}")
    print(f"     Active lease state: {lease.started}")

    # 6. Immediately Start Session on the SAME exact object
    print("\nStarting session on the SAME BuildLease object...")
    lease._assert_live_session_reservation_capability()
    session = lease.start_session()
    print(f"[OK] Session started on exact returned object.")
    print(f"     Workspace mounted at: {session.workspace}")

    # 7. Execute all 14 steps in order
    steps = session.plan["steps"]
    step_results = []
    print(f"\nExecuting {len(steps)} session steps in sequence:")

    for idx, step in enumerate(steps):
        step_start = time.time()
        kind = step.get("kind")
        graph_id = step.get("graph_id", "N/A")
        target_id = step.get("target_id", step.get("output_id", ""))
        print(f"\n--- Step {idx+1}/{len(steps)}: kind={kind} graph={graph_id} target={target_id} ---")

        if kind == "source_verification":
            res = session.verify_source()
        else:
            res = session.run_next()

        step_elapsed = time.time() - step_start
        status = res.get("status", "UNKNOWN")
        print(f"    Status: {status} in {step_elapsed:.2f}s")
        if status not in ("STEP_SUCCEEDED_SESSION_ACTIVE", "SOURCE_VERIFIED_SESSION_ACTIVE", "PASS"):
            raise RuntimeError(f"Step {idx+1} ({kind}) FAILED: {res}")
        step_results.append(res)

    # 8. Finalize Session
    print("\nFinalizing session (unmount, detach, RO reattach, RO verification, receipts)...")
    finalize_start = time.time()
    # finalize() owns the one BuildLease.complete() call and returns its
    # synchronous promotion/retirement receipt. Do not call complete() again.
    final_receipt = session.finalize()
    finalize_elapsed = time.time() - finalize_start
    print(f"[OK] Session finalized in {finalize_elapsed:.2f}s!")
    if (final_receipt.get("status") != "COMPLETED_AND_AUTO_RETIRED"
            or final_receipt.get("idempotent_replay") is True):
        raise RuntimeError(f"Single finalization did not return fresh synchronous completion: {final_receipt}")
    print(f"     Completion and promotion: {final_receipt.get('status')}")

    total_elapsed = time.time() - start_time
    print("\n==================================================")
    print(f"R6-A EXECUTION COMPLETE in {total_elapsed:.2f}s")
    print("==================================================")
    return {
        "status": "PASS",
        "completion_call_count": 1,
        "build_id": build_id,
        "admission_id": admission_id,
        "plan_sha256": plan_sha256,
        "final_receipt": final_receipt,
        "step_count": len(step_results),
        "total_elapsed_seconds": total_elapsed,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Execute the explicitly selected, reviewed R6-A plan.")
    parser.add_argument("--execute", action="store_true", required=True,
                        help="explicitly opt in to live R6-A execution")
    parser.add_argument("--plan", required=True,
                        help="repository-relative or absolute reviewed R6-A plan path")
    args = parser.parse_args()
    try:
        result = run_r6a_execution(args.plan)
        print("\nEXECUTION SUCCESSFUL!")
        sys.exit(0)
    except Exception as error:
        print(f"\nEXECUTION FAILED: {error}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
# AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
