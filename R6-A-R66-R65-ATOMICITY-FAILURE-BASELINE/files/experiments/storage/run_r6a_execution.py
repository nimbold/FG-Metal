#!/usr/bin/env python3
"""R65 Same-Process Dual-Lineage R6-A Execution Controller.

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
from pathlib import Path

# Repository root setup
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "experiments/storage"))

import build_retention
import storage_policy

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


def run_r6a_execution() -> dict:
    start_time = time.time()
    print("==================================================")
    print("R65 SAME-PROCESS R6-A EXECUTION")
    print("Role: R6-A Execution Owner (GPT-6 Luna xHigh)")
    print("==================================================")

    # 1. Load the reviewed fresh R65 plan. The environment override permits the
    #    parent workflow to bind a dated plan path without accepting an R64 plan.
    configured_plan = os.environ.get("FG_METAL_R65_PLAN_PATH")
    plan_path = Path(configured_plan).expanduser() if configured_plan else (
        REPO / "R6-A-R65-DUAL-LINEAGE-EXECUTION-PLAN.json")
    if not plan_path.is_absolute():
        plan_path = REPO / plan_path
    plan_path = plan_path.resolve(strict=True)
    if (REPO.resolve(strict=True) not in plan_path.parents
            or "R65" not in plan_path.name.upper()
            or "R64" in plan_path.name.upper()):
        raise RuntimeError("Execution requires a fresh R65 plan file inside this repository")

    plan_bytes = storage_policy._read_file_nofollow(plan_path)
    plan_file_sha256 = hashlib.sha256(plan_bytes).hexdigest()
    try:
        plan = json.loads(plan_bytes, object_pairs_hook=_json_object_without_duplicate_keys)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Fresh R65 plan is malformed or contains duplicate keys: {error}") from error

    if not isinstance(plan, dict):
        raise RuntimeError("Fresh R65 recovery plan must be a JSON object")
    _validate_r65_recovery_plan(plan)
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
    _validate_r65_recovery_plan(validated_plan)
    plan_canonical = build_retention._canonical_json(validated_plan)
    plan_sha256 = hashlib.sha256(plan_canonical).hexdigest()
    if build_path.exists():
        raise RuntimeError("R65 recovery build ID has already been used")
    _assert_r65_admission_id_unclaimed(admission_id)

    print(f"[OK] Fresh R65 execution plan loaded: {plan_path.name}")
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
    g0c_review_path = REPO / "experiments/storage" / storage_policy.G0C_REVIEW_RECORD_NAME
    g0c_review_bytes = storage_policy._read_file_nofollow(g0c_review_path)
    g0c_review_sha = hashlib.sha256(g0c_review_bytes).hexdigest()
    r65_review_binding = _validate_r65_g0c_binding(
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
    output_dir = storage_policy.REPO / "experiments/storage/r6a-r65-admission-output"

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
        "r65_g0c_binding_sha256": r65_review_binding["binding_sha256"],
        "r65_candidate_sha256": r65_review_binding["candidate_sha256"],
        "dual_graph_finalization_contract_sha256": r65_review_binding["contract_sha256"],
        "windows_cross_file_sha256": windows_cross_sha,
        "toolchain_receipts_sha256": toolchain_receipts_sha,
        "prefix_smoke_report_sha256": smoke_report_sha,
    }

    additional_manifest_fields = {
        "build_session_plan_sha256": plan_sha256,
        "execution_admission_identity": plan["admission_identity"],
        "dual_build_graph_contract_version": 2,
        "dual_build_graphs": 1,
        "r65_g0c_binding_sha256": r65_review_binding["binding_sha256"],
        "dual_graph_finalization_contract_sha256": r65_review_binding["contract_sha256"],
        "policy_sha256": plan["policy_sha256"],
        "schema_version": 3,
    }

    admission_claim = _claim_r65_admission_id(admission_id, plan_sha256)
    candidate_hashes["r65_admission_claim_sha256"] = admission_claim["claim_sha256"]

    print(f"Signing fresh admission for experiment {admission_id}...")
    manifest = storage_policy.begin_experiment(
        experiment_id=admission_id,
        purpose="R65 same-process dual-lineage R6-A execution: Windows PE compile/link plus native PRODUCTION_PATH, UNIT, MODEL verification",
        candidate_hashes=candidate_hashes,
        expected_duration_seconds=43200,
        expected_prefix_bytes=0,
        expected_build_bytes=reservation_bytes,
        estimated_persistent_bytes=0,
        storage_justification=f"Fresh R65 dual-lineage build lease reservation of {reservation_bytes} bytes under 55 GiB budget.",
        authorized_runner_paths=[runner_file],
        build_source_tree_hash=src_hash,
        build_configuration_key_sha256=validated_plan["build_configuration_key_sha256"],
        build_kind="dxvk",
        build_type="release",
        dependency_state=dep_state,
        output_dir=output_dir,
        additional=additional_manifest_fields,
    )

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
        purpose="R65 same-process dual-lineage R6-A execution: Windows PE compile/link plus native PRODUCTION_PATH, UNIT, MODEL verification",
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
    reserved_path = lease.reserve()
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
    try:
        result = run_r6a_execution()
        print("\nEXECUTION SUCCESSFUL!")
        sys.exit(0)
    except Exception as error:
        print(f"\nEXECUTION FAILED: {error}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        sys.exit(1)
# AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
