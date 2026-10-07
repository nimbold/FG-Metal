#!/usr/bin/env python3
"""Refresh and attest the executable Step 11D.3 storage gate.

``preflight`` writes a narrowly scoped PREFLIGHT_PASS authorizing only the one
fixed storage-policy PrefixLease smoke. ``finalize`` requires its cleanup
receipt and emits the only status accepted by normal graphics runners.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import build_retention  # noqa: E402
import storage_policy  # noqa: E402

ATTESTATION = REPO / "experiments/storage/storage-gate0-attestation.json"
INVENTORY = REPO / "experiments/storage/storage-inventory.json"
RECEIPT = REPO / "experiments/storage/storage-cleanup-receipt.json"
TEST_REPORT = REPO / "experiments/storage/build-retention-tests.log"
LEGACY_BUILD_ROOT_REVIEW = REPO / "experiments/storage/storage-build-retention-review.json"
LEGACY_BUILD_ROOT_LIVENESS = REPO / "experiments/storage/build-cleanup-predelete-liveness.json"
DIAGNOSTIC_RUNNER = REPO / (
    "experiments/dxvk_macos_step11dR/evidence/step11d2-diagnostic/runner-diagnostic.py")
LEGACY_STANDALONE_DXVK_ROOTS = (
    {
        "path": Path("/Users/nima/Library/Caches/FGMetalStep11C1R/build-d1"),
        "build_record_path": REPO / "experiments/storage/build-records/build-d1/build-record.json",
        "build_record_sha256": "cfdb29d674a93ed428fd87ba4c4c0d6ccb1321fd7d54fb6afec79dddcacdc081",
        "decision": "DELETE_VERIFIED_WITH_PROVENANCE_GAP_RECORDED",
        "logical_bytes": 1551987821,
        "best_recoverable_bytes_estimate": 603533312,
        "binary_output_count": 21,
        "provenance_gap": "Build-time source/build relationship and a complete Ninja transcript were not established.",
    },
    {
        "path": Path("/Users/nima/Library/Caches/FGMetalStep11C1R/build-d1-repro"),
        "build_record_path": REPO / "experiments/storage/build-records/build-d1-repro/build-record.json",
        "build_record_sha256": "e4317c70a82b78a70806dc28b6b442d41ac9c2e46f09e1c1c15ee5920aecd217",
        "decision": "DELETE_VERIFIED",
        "logical_bytes": 1062546188,
        "best_recoverable_bytes_estimate": 471773184,
        "binary_output_count": 15,
        "provenance_gap": None,
    },
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"required Gate 0 evidence is not a regular file: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError(f"Gate 0 evidence is not a JSON object: {path}")
    return value


def _write_atomic(path: Path, value: dict[str, Any]) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.is_symlink():
        raise RuntimeError(f"refusing Gate 0 evidence write through a symlink: {path}")
    raw = (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        offset = 0
        while offset < len(raw):
            written = os.write(fd, raw[offset:])
            if written <= 0:
                raise OSError("short Gate 0 evidence write")
            offset += written
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temporary, path)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return raw


def _run_test_suite() -> str:
    result = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-v", "-s", str(HERE),
         "-p", "test_*.py"], cwd=REPO, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    raw = result.stdout.encode("utf-8", errors="replace")
    temporary = TEST_REPORT.with_name(f".{TEST_REPORT.name}.{os.getpid()}.tmp")
    temporary.write_bytes(raw)
    with temporary.open("rb") as stream:
        os.fsync(stream.fileno())
    os.replace(temporary, TEST_REPORT)
    if result.returncode != 0:
        raise RuntimeError(f"build-retention synthetic suite failed ({result.returncode}); see {TEST_REPORT}")
    ran = re.search(r"^Ran ([0-9]+) tests? ", result.stdout, re.MULTILINE)
    if (not ran or int(ran.group(1)) < 36 or "OK" not in result.stdout
            or "skipped=" in result.stdout or "expected failures=" in result.stdout):
        raise RuntimeError("build-retention synthetic suite did not report a complete successful run")
    return _sha(raw)


def _refresh_inventory() -> dict[str, Any]:
    result = subprocess.run([sys.executable, str(HERE / "storage_inventory.py")], cwd=REPO,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"storage inventory refresh failed: {result.stdout[-3000:]}")
    return _read_json(INVENTORY)


def _artifact_digests_referenced_by_retained_evidence_and_runner() -> list[str]:
    """Collect CAS identities before isolating legacy hardlinked payloads.

    Build manifests are envelope-checked before their artifact list is used.
    The currently authorized diagnostic runner is source-hash-bound by the
    same admission parser used by begin_experiment, and its artifact literals
    are limited to named artifact constants.
    """
    # The full store is enumerated here to validate membership, but only
    # retained build outputs, the admitted runner inputs, and this diagnostic's
    # retained run outputs are isolated. Copying every legacy runtime/input
    # hardlink would exceed the scoped project budget; the remaining store is
    # content- and link-count-fingerprinted separately by the full-store audit.
    store_digests = set(build_retention.canonical_artifact_digests())
    digests: set[str] = set()
    manifest_fd = build_retention._open_dir(build_retention.MANIFEST_ROOT)
    try:
        names = sorted(name for name in os.listdir(manifest_fd)
                       if name.endswith(".json") and "/" not in name and "\\" not in name)
        for name in names:
            raw = build_retention._read_at(manifest_fd, name, limit=64 * 1024 * 1024)
            value = json.loads(raw)
            if (not isinstance(value, dict)
                    or value.get("build_manifest_id") != build_retention._manifest_id(value)
                    or value.get("manifest_sha256") != build_retention._manifest_checksum(value)):
                raise RuntimeError(f"build manifest failed envelope validation during CAS migration: {name}")
            artifacts = value.get("artifacts")
            if not isinstance(artifacts, list) or not artifacts:
                raise RuntimeError(f"build manifest lacks artifact identities during CAS migration: {name}")
            for artifact in artifacts:
                digest = artifact.get("sha256") if isinstance(artifact, dict) else None
                canonical = artifact.get("canonical_artifact_path") if isinstance(artifact, dict) else None
                canonical_path = Path(canonical) if isinstance(canonical, str) else None
                if (not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                        or canonical_path is None
                        or canonical_path.parent != build_retention.ARTIFACT_ROOT / digest
                        or canonical != str(canonical_path)
                        or canonical_path.name in {"", ".", ".."}):
                    raise RuntimeError(f"build manifest has an unsafe artifact identity during CAS migration: {name}")
                digests.add(digest)
    finally:
        os.close(manifest_fd)

    # These two historical roots were already removed, but their output lists
    # are still authoritative inputs to CAS hardlink isolation. Their exact
    # build-record hashes are pinned above, and every output path must remain
    # lexically beneath its reviewed root before its digest is trusted.
    for spec in LEGACY_STANDALONE_DXVK_ROOTS:
        record_bytes = storage_policy._read_file_nofollow(spec["build_record_path"])
        if _sha(record_bytes) != spec["build_record_sha256"]:
            raise RuntimeError(f"legacy build record changed before CAS isolation: {spec['build_record_path']}")
        record = json.loads(record_bytes)
        root = Path(spec["path"])
        if Path(record.get("build_path", "")).absolute() != root:
            raise RuntimeError(f"legacy build record names a different root: {spec['build_record_path']}")
        outputs = record.get("binary_outputs")
        if not isinstance(outputs, list) or not outputs:
            raise RuntimeError(f"legacy build record lacks binary outputs: {spec['build_record_path']}")
        for output in outputs:
            path = Path(output.get("path", "")).absolute() if isinstance(output, dict) else None
            digest = output.get("sha256") if isinstance(output, dict) else None
            if (path is None or root not in path.parents
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise RuntimeError(f"legacy build record contains an unsafe output identity: {spec['build_record_path']}")
            digests.add(digest)

    runner_records = storage_policy._authorized_runner_records(
        [str(DIAGNOSTIC_RUNNER)], requires_managed_prefix=True)
    if runner_records.get(str(DIAGNOSTIC_RUNNER)) != _sha(storage_policy._read_file_nofollow(DIAGNOSTIC_RUNNER)):
        raise RuntimeError("diagnostic runner changed during CAS migration admission")
    tree = ast.parse(storage_policy._read_file_nofollow(DIAGNOSTIC_RUNNER),
                     filename=str(DIAGNOSTIC_RUNNER))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        names = [target.id for target in targets if isinstance(target, ast.Name)]
        if not any(name.endswith("_ARTIFACTS") or name.endswith("_ARTIFACT_SHA256")
                   for name in names):
            continue
        for child in ast.walk(node.value):
            if (isinstance(child, ast.Constant) and isinstance(child.value, str)
                    and re.fullmatch(r"[0-9a-f]{64}", child.value)):
                digests.add(child.value)
    if not digests.issubset(store_digests):
        raise RuntimeError("retained build or admitted-runner artifact identity is absent from the canonical store: "
                           + ", ".join(sorted(digests - store_digests)[:12]))

    evidence_root = DIAGNOSTIC_RUNNER.parent
    for root, directories, filenames in os.walk(evidence_root, followlinks=False):
        directories[:] = [name for name in directories
                          if not (Path(root) / name).is_symlink()]
        for name in filenames:
            if not name.endswith(".json"):
                continue
            path = Path(root) / name
            try:
                raw = storage_policy._read_file_nofollow(path)
                value = json.loads(raw)
            except (OSError, json.JSONDecodeError) as error:
                raise RuntimeError(f"retained diagnostic evidence manifest is unreadable: {path}: {error}") from error
            found = {match.decode("ascii") for match in re.findall(
                rb"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", raw)}
            digests.update(found & store_digests)
            # Binary fields in retained run manifests must resolve through the
            # store. Tree/source/report hashes are deliberately not treated as
            # binary identities.
            if isinstance(value, dict):
                for key in ("app_sha256", "wine_sha256", "moltenvk_sha256",
                            "winevulkan_pe_sha256", "winevulkan_unix_sha256"):
                    candidate = value.get(key)
                    if (isinstance(candidate, str) and re.fullmatch(r"[0-9a-f]{64}", candidate)
                            and candidate not in store_digests):
                        raise RuntimeError(f"retained run binary hash is absent from the canonical store: {path}:{key}")
                dlls = value.get("dll_sha256")
                if isinstance(dlls, dict):
                    missing = [digest for digest in dlls.values()
                               if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)
                               and digest not in store_digests]
                    if missing:
                        raise RuntimeError(f"retained DLL hash is absent from the canonical store: {path}")
    return sorted(digests)


def _retention_state(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "active_build_count": int(snapshot.get("active_build_count", -1)),
        "preserved_build_count": int(snapshot.get("preserved_build_count", -1)),
        "retained_build_count": int(snapshot.get("retained_build_count", -1)),
        "active_dxvk_count": int(snapshot.get("active_dxvk_count", -1)),
        "active_moltenvk_count": int(snapshot.get("active_moltenvk_count", -1)),
        "unknown_count": len(snapshot.get("unknown", [])),
        "completed_pending_retirement_count": len(snapshot.get("completed_pending_retirement", [])),
        "failed_or_retiring_count": len(snapshot.get("failed_or_retiring", [])),
        "retention_fingerprint_sha256": snapshot.get("retention_fingerprint", {}).get("sha256"),
    }


def _new_cleanup_actions(previous_updated: float) -> list[dict[str, Any]]:
    actions = []
    if not build_retention.RECEIPT_ROOT.exists():
        return actions
    for path in sorted(build_retention.RECEIPT_ROOT.glob("*.json")):
        try:
            record = build_retention._read_json(path, sealed=True)
        except Exception as error:
            raise RuntimeError(f"cannot verify build-retention cleanup receipt {path}: {error}") from error
        completed = record.get("completed_at_unix")
        if record.get("result") != "REMOVED" or not isinstance(completed, (int, float)):
            continue
        if completed <= previous_updated:
            continue
        actions.append({key: record.get(key) for key in (
            "path", "category", "state_before", "action", "logical_bytes",
            "best_recoverable_bytes_estimate", "allocated_unique_inode_bytes",
            "artifact_hashes_preserved", "build_manifest_id", "build_manifest_sha256",
            "result", "filesystem_free_before_bytes", "filesystem_free_after_bytes",
            "measured_free_delta_bytes", "completed_at_unix")})
    return actions


def _record_blocked_storage_preflight(reason: str, *, free_bytes: int,
                                      project_usage: dict[str, Any],
                                      hardlink_copy_estimate: list[dict[str, Any]],
                                      hardlink_copy_bytes: int) -> None:
    """Persist a fresh refusal receipt when safe CAS preparation cannot fit."""
    if not RECEIPT.is_file() or RECEIPT.is_symlink():
        raise RuntimeError("cannot write a blocked Gate 0 receipt without the verified cleanup receipt")
    receipt_bytes = storage_policy._read_file_nofollow(RECEIPT)
    receipt = json.loads(receipt_bytes)
    if not isinstance(receipt, dict) or not isinstance(receipt.get("phases"), list):
        raise RuntimeError("existing cleanup receipt is malformed during blocked preflight")
    now = time.time()
    project_bytes = int(project_usage["allocated_inode_deduplicated_bytes"])
    refusal = {
        "path": str(build_retention.ARTIFACT_ROOT),
        "category": "LEGACY CAS ARTIFACT HARDLINK ISOLATION",
        "state_before": "CANONICAL ARTIFACTS SHARE INODES WITH BUILD OUTPUT REFERENCES",
        "action": "REFUSED_BEFORE_COPY_OR_RETIREMENT",
        "logical_bytes": int(hardlink_copy_bytes),
        "best_recoverable_bytes_estimate": 0,
        "artifact_hashes_preserved": sorted(row["sha256"] for row in hardlink_copy_estimate),
        "result": "BLOCKED",
        "blocker": reason,
    }
    phase = {
        "phase_id": f"step11d3-g0-blocked-{time.time_ns()}",
        "phase": "build-retention-preflight",
        "generated_at_unix": now,
        "completed_at_unix": now,
        "status": "BLOCKED",
        "storage_hygiene_status": "BLOCKED",
        "storage_policy_reconciliation": True,
        "final_free_bytes": int(free_bytes),
        "fgmetal_scoped_allocated_bytes": project_bytes,
        "global_inode_deduplicated_bytes": None,
        "filesystem_free_before_bytes": int(free_bytes),
        "filesystem_free_after_bytes": int(free_bytes),
        "project_usage_after": project_usage,
        "artifact_hardlink_isolation_copy_estimate_bytes": int(hardlink_copy_bytes),
        "artifact_hardlink_isolation_copy_estimate": hardlink_copy_estimate,
        "build_cleanup_actions": [refusal],
        "blockers": [reason],
        "prefixlease_smoke_status": "NOT_RUN_STORAGE_CAP_BLOCKED",
    }
    receipt.setdefault("phases", []).append(phase)
    receipt["last_updated_at_unix"] = now
    receipt["gate0_latest_phase_id"] = phase["phase_id"]
    _write_atomic(RECEIPT, receipt)


def _legacy_standalone_root_reconciliation_rows(previous_receipt_sha256: str) -> list[dict[str, Any]]:
    """Reconcile the two historical C1R roots without pretending to have deleted them here."""
    review_bytes = storage_policy._read_file_nofollow(LEGACY_BUILD_ROOT_REVIEW)
    review = json.loads(review_bytes)
    liveness_bytes = storage_policy._read_file_nofollow(LEGACY_BUILD_ROOT_LIVENESS)
    liveness = json.loads(liveness_bytes)
    if not RECEIPT.is_file() or RECEIPT.is_symlink():
        raise RuntimeError("historical cleanup receipt is unavailable for legacy build reconciliation")
    current_receipt_sha256 = _sha(storage_policy._read_file_nofollow(RECEIPT))
    if current_receipt_sha256 != previous_receipt_sha256:
        raise RuntimeError("cleanup receipt changed during legacy build-root reconciliation")
    if not isinstance(review.get("items"), list) or not isinstance(liveness.get("items"), list):
        raise RuntimeError("legacy build review or pre-delete liveness evidence is malformed")

    def dict_rows(value: Any):
        if isinstance(value, dict):
            yield value
            for child in value.values():
                yield from dict_rows(child)
        elif isinstance(value, list):
            for child in value:
                yield from dict_rows(child)

    history = json.loads(storage_policy._read_file_nofollow(RECEIPT))
    rows: list[dict[str, Any]] = []
    approved_paths = set(build_retention.LEGACY_BUILD_ROOTS)
    for spec in LEGACY_STANDALONE_DXVK_ROOTS:
        root = spec["path"]
        if root not in approved_paths or root.parent.resolve(strict=True) != root.parent:
            raise RuntimeError(f"legacy standalone root is not an exact canonical reconciliation path: {root}")
        parent_fd = build_retention._open_dir(root.parent)
        try:
            parent_identity = build_retention._fs_identity(parent_fd)
            if (parent_identity.get("owner_uid") != os.getuid()
                    or int(parent_identity.get("mode", 0)) & 0o022
                    or parent_identity.get("filesystem_id") is None):
                raise RuntimeError(f"legacy root parent identity is not trusted: {root.parent}")
            try:
                os.stat(root.name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RuntimeError(f"legacy standalone build root reappeared and requires explicit adoption: {root}")
        finally:
            os.close(parent_fd)

        review_matches = [item for item in review["items"]
                          if isinstance(item, dict) and item.get("path") == str(root)]
        if len(review_matches) != 1:
            raise RuntimeError(f"legacy build review does not identify exactly one root: {root}")
        reviewed = review_matches[0]
        record_bytes = storage_policy._read_file_nofollow(spec["build_record_path"])
        record_sha256 = _sha(record_bytes)
        build_record = json.loads(record_bytes)
        if (record_sha256 != spec["build_record_sha256"]
                or reviewed.get("build_record_sha256") != record_sha256
                or reviewed.get("build_record_sha256_verified_now") not in {None, record_sha256}
                or reviewed.get("build_record_path") != str(spec["build_record_path"])
                or reviewed.get("decision") != spec["decision"]
                or reviewed.get("binary_output_count") != spec["binary_output_count"]
                or reviewed.get("logical_bytes") != spec["logical_bytes"]
                or reviewed.get("estimated_net_reclaim_by_link_count_bytes")
                != spec["best_recoverable_bytes_estimate"]):
            raise RuntimeError(f"legacy build record/review identity changed: {root}")
        record_outputs = build_record.get("binary_outputs")
        if (build_record.get("build_path") != str(root)
                or not isinstance(record_outputs, list)
                or len(record_outputs) != spec["binary_output_count"]):
            raise RuntimeError(f"pinned legacy build record has an incomplete output map: {root}")
        record_output_map: dict[tuple[str, str], str] = {}
        for record_output in record_outputs:
            if not isinstance(record_output, dict):
                raise RuntimeError(f"pinned legacy build output row is malformed: {root}")
            record_path = record_output.get("path")
            record_name = record_output.get("name")
            record_digest = record_output.get("sha256")
            absolute_record_path = Path(record_path).absolute() if isinstance(record_path, str) else None
            if (absolute_record_path is None or root not in absolute_record_path.parents
                    or not isinstance(record_name, str) or absolute_record_path.name != record_name
                    or not isinstance(record_digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", record_digest)):
                raise RuntimeError(f"pinned legacy build output identity is unsafe: {root}")
            record_key = (str(absolute_record_path), record_name)
            if record_key in record_output_map:
                raise RuntimeError(f"pinned legacy build record has duplicate output identities: {root}")
            record_output_map[record_key] = record_digest
        historical_deletions = [row for row in dict_rows(history)
                                if row.get("path") == str(root) and row.get("status") == "DELETED"
                                and row.get("build_record_sha256") == record_sha256]
        if not historical_deletions:
            raise RuntimeError(f"historical cleanup receipt lacks an exact deletion record: {root}")
        historical_liveness = [item for item in liveness["items"]
                               if isinstance(item, dict) and item.get("path") == str(root)]
        if historical_liveness:
            if (len(historical_liveness) != 1
                    or historical_liveness[0].get("lsof_exit_code") != 1
                    or historical_liveness[0].get("open_handle_count") != 0
                    or historical_liveness[0].get("open_handle_rows") != []):
                raise RuntimeError(f"legacy pre-delete liveness evidence is not clear: {root}")
            liveness_identity = {"path": str(LEGACY_BUILD_ROOT_LIVENESS),
                                 "sha256": _sha(liveness_bytes),
                                 "row": historical_liveness[0]}
        else:
            matched_lsof = [row.get("lsof") for row in historical_deletions
                            if isinstance(row.get("lsof"), dict)]
            if not matched_lsof or any(item.get("returncode") != 1 or item.get("rows") != []
                                       for item in matched_lsof):
                raise RuntimeError(f"legacy deletion evidence lacks a clear liveness check: {root}")
            liveness_identity = {"path": str(RECEIPT), "sha256": previous_receipt_sha256,
                                 "lsof": matched_lsof[-1]}

        outputs = reviewed.get("binary_outputs")
        if not isinstance(outputs, list) or len(outputs) != spec["binary_output_count"]:
            raise RuntimeError(f"legacy build review has an incomplete binary output list: {root}")
        retained_outputs = []
        matched_record_outputs: set[tuple[str, str]] = set()
        for output in outputs:
            if not isinstance(output, dict):
                raise RuntimeError(f"legacy build output row is malformed: {root}")
            name, digest, canonical = output.get("name"), output.get("sha256"), output.get("canonical_path")
            build_output = Path(output.get("build_output_path", ""))
            try:
                relative_output = build_output.relative_to(root)
            except ValueError as error:
                raise RuntimeError(f"legacy build output escaped its recorded root: {build_output}") from error
            if (output.get("build_output_status") != "MATCH" or not relative_output.parts
                    or relative_output.as_posix().startswith("../")
                    or not isinstance(name, str) or not name or Path(name).name != name
                    or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)):
                raise RuntimeError(f"legacy build output identity is invalid: {build_output}")
            output_key = (str(build_output.absolute()), name)
            if record_output_map.get(output_key) != digest or output_key in matched_record_outputs:
                raise RuntimeError(f"legacy build review output differs from its pinned build record: {build_output}")
            matched_record_outputs.add(output_key)
            canonical_path = Path(canonical or "")
            expected_path = build_retention.ARTIFACT_ROOT / digest / name
            artifact_row = {"sha256": digest, "canonical_artifact_path": str(canonical_path)}
            if (canonical_path != expected_path or not build_retention._verify_artifact(artifact_row)):
                raise RuntimeError(f"canonical legacy output is missing or has a hash mismatch: {canonical}")
            artifact_manifest_path = canonical_path.parent / "manifest.json"
            artifact_manifest_bytes = storage_policy._read_file_nofollow(artifact_manifest_path)
            artifact_manifest = json.loads(artifact_manifest_bytes)
            if (artifact_manifest.get("sha256") != digest
                    or artifact_manifest.get("canonical_path") != str(canonical_path)):
                raise RuntimeError(f"canonical artifact manifest does not bind its payload: {canonical}")
            retained_outputs.append({
                "artifact_name": name,
                "historical_build_output_path": str(build_output),
                "sha256": digest,
                "canonical_artifact_path": str(canonical_path),
                "artifact_manifest_id": _sha(artifact_manifest_bytes),
                "artifact_manifest_path": str(artifact_manifest_path),
            })
        if matched_record_outputs != set(record_output_map):
            raise RuntimeError(f"legacy build review omits pinned binary outputs: {root}")
        retained_outputs.sort(key=lambda item: (item["artifact_name"], item["sha256"]))
        identity = build_record.get("identity", {})
        manifest_material = {
            "schema_version": 1,
            "manifest_kind": "legacy-canonical-output-manifest",
            "build_id": build_record.get("build_id"),
            "historical_build_root": str(root),
            "build_record_path": str(spec["build_record_path"]),
            "build_record_sha256": record_sha256,
            "historical_build_identity": identity,
            "artifact_hashes": retained_outputs,
            "provenance_gap": spec["provenance_gap"],
            "evidence_scope": "Canonical output identity is verified now; historical build-time source/configuration linkage is not upgraded beyond the retained build record.",
        }
        build_manifest_id = _sha(build_retention._canonical_json(manifest_material))
        manifest = {**manifest_material, "build_manifest_id": build_manifest_id}
        build_manifest_sha256 = _sha(build_retention._canonical_json(manifest))
        rows.append({
            "build_id": build_record.get("build_id"),
            "path": str(root),
            "category": "COMPLETED-RECLAIMABLE",
            "state_before": "COMPLETED",
            "action": "VERIFY_ABSENCE_AND_CANONICAL_ARTIFACTS_NO_DELETE_ALREADY_ABSENT",
            "logical_bytes": spec["logical_bytes"],
            "best_recoverable_bytes_estimate": spec["best_recoverable_bytes_estimate"],
            "artifact_hashes_preserved": retained_outputs,
            "build_manifest_id": build_manifest_id,
            "build_manifest_sha256": build_manifest_sha256,
            "build_manifest_schema": "legacy-canonical-output-manifest-v1",
            "historical_build_record_sha256": record_sha256,
            "historical_review_sha256": _sha(review_bytes),
            "historical_cleanup_receipt_sha256": previous_receipt_sha256,
            "historical_liveness": liveness_identity,
            "current_parent_identity": parent_identity,
            "observed_absent_at_unix": time.time(),
            "provenance_gap": spec["provenance_gap"],
            "result": "ABSENT_VERIFIED_ARTIFACTS_VERIFIED_PRIOR_DELETE_METHOD_NOT_DESCRIPTOR_ATTESTED",
        })
    return rows


def _build_root_reconciliation(snapshot: dict[str, Any],
                               legacy_rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Bind current preserved roots and prior verified retirements into each fresh receipt."""
    rows: list[dict[str, Any]] = []
    for path in sorted(build_retention.RECEIPT_ROOT.glob("*.json")):
        record = build_retention._read_json(path, sealed=True)
        if record.get("result") != "REMOVED":
            continue
        rows.append({key: record.get(key) for key in (
            "build_id", "path", "category", "state_before", "action", "logical_bytes",
            "best_recoverable_bytes_estimate", "artifact_hashes_preserved", "build_manifest_id",
            "build_manifest_sha256", "result", "completed_at_unix")})

    for group, category in (("active", "ACTIVE"), ("preserved_debug", "PRESERVE-FOR-DEBUG")):
        for lease in snapshot.get(group, []):
            rows.append({"build_id": lease.get("build_id"), "path": lease.get("path"),
                         "category": category, "state_before": lease.get("state"),
                         "action": "RETAIN_UNDER_EXPLICIT_BUILD_LEASE",
                         "logical_bytes": lease.get("estimated_bytes"),
                         "best_recoverable_bytes_estimate": None,
                         "artifact_hashes_preserved": [],
                         "build_manifest_id": lease.get("build_manifest_id"),
                         "build_manifest_sha256": lease.get("build_manifest_sha256"),
                         "result": "RETAINED"})

    for group_name in ("classified_nonlarge_roots", "classified_non_retention_roots"):
        for root in snapshot.get(group_name, []):
            classification = root.get("classification", "UNKNOWN")
            hashes = []
            for field in ("content_tree_sha256", "nested_build_content_tree_sha256"):
                value = root.get(field)
                if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                    hashes.append(value)
            if classification == "HISTORICAL-SOURCE-AND-EVIDENCE-CORPUS":
                for name in ("REPORT.md", "HASHES.json"):
                    target = Path(root["path"]) / name
                    if target.is_file() and not target.is_symlink():
                        hashes.append(hashlib.sha256(storage_policy._read_file_nofollow(target)).hexdigest())
            rows.append({"path": root.get("path"), "category": classification,
                         "state_before": classification,
                         "action": root.get("action", "PRESERVE_AS_SOURCE_DEPENDENCY"),
                         "logical_bytes": root.get("logical_bytes"),
                         "best_recoverable_bytes_estimate": root.get("best_recoverable_bytes_estimate"),
                         "artifact_hashes_preserved": sorted(set(hashes)),
                         "build_manifest_id": None, "build_manifest_sha256": None,
                         "result": "PRESERVED_AND_INVENTORIED"})
    rows.extend(legacy_rows or [])
    return sorted(rows, key=lambda row: (str(row.get("path")), str(row.get("build_id"))))


def _artifact_hardlink_receipts() -> list[dict[str, Any]]:
    rows = []
    for path in sorted(build_retention.RECEIPT_ROOT.glob("artifact-hardlink-detach-*.json")):
        record = build_retention._read_json(path, sealed=True)
        if (record.get("receipt_type") != "LEGACY_CAS_HARDLINK_ISOLATION"
                or record.get("result") != "DETACHED_WITH_HASH_PRESERVED"
                or record.get("isolated_link_count") != 1
                or not re.fullmatch(r"[0-9a-f]{64}", str(record.get("sha256", "")))):
            raise RuntimeError(f"CAS hardlink isolation receipt is invalid: {path}")
        rows.append({"sha256": record["sha256"],
                     "path": record.get("canonical_artifact_path"),
                     "logical_bytes": record.get("size_bytes"),
                     "state_before": f"LEGACY_HARDLINKED_{record.get('previous_link_count')}",
                     "action": "DETACH_LEGACY_CAS_HARDLINKS",
                     "result": record["result"], "receipt_path": str(path)})
    return rows


def _prefix_metrics() -> dict[str, Any]:
    preserved = storage_policy.preserved_prefixes()
    uncertified = storage_policy.uncertified_prefixes()
    active = storage_policy._active_lease_markers()
    return {"prefix_count": len(preserved) + len(uncertified),
            "preserved_prefix_count": len(preserved),
            "uncertified_prefix_count": len(uncertified),
            "active_lease_count": len(active),
            "preserved_prefix_paths": [str(path) for path in preserved],
            "uncertified_prefix_paths": [str(path) for path in uncertified],
            "active_prefix_lease_markers": [str(path) for path in active]}


def _valid_prefix_state_evidence(evidence: Any, smoke_id: str,
                                 expected_prefix: Path) -> bool:
    if not isinstance(evidence, dict):
        return False
    baseline = evidence.get("baseline")
    during = evidence.get("during")
    after = evidence.get("after_cleanup")
    command = evidence.get("controlled_command")
    if not all(isinstance(item, dict) for item in (baseline, during, after, command)):
        return False
    marker_path = storage_policy.ACTIVE_LEASE_ROOT / f"{smoke_id}.json"
    expected_marker = [{
        "experiment_id": smoke_id,
        "state": "MATERIALIZED",
        "prefix_path": str(expected_prefix),
        "marker_path": str(marker_path),
    }]
    baseline_paths = baseline.get("known_prefix_paths")
    during_paths = during.get("known_prefix_paths")
    baseline_preserved = baseline.get("preserved_prefix_paths")
    during_preserved = during.get("preserved_prefix_paths")
    baseline_uncertified = baseline.get("uncertified_prefix_paths")
    during_uncertified = during.get("uncertified_prefix_paths")
    if (not isinstance(baseline_paths, list) or not isinstance(during_paths, list)
            or not isinstance(baseline_preserved, list) or not isinstance(during_preserved, list)
            or not isinstance(baseline_uncertified, list) or not isinstance(during_uncertified, list)
            or not all(isinstance(path, str) for path in (
                baseline_paths + during_paths + baseline_preserved + during_preserved
                + baseline_uncertified + during_uncertified))):
        return False
    started = command.get("started_at_unix")
    completed = command.get("completed_at_unix")
    pid = command.get("pid")
    device = during.get("leased_prefix_device")
    inode = during.get("leased_prefix_inode")
    return (
        evidence.get("exactly_one_temporary_prefix") is True
        and evidence.get("baseline_restored") is True
        and baseline.get("known_prefix_count") == len(baseline_paths)
        and during.get("known_prefix_count") == len(during_paths)
        and baseline.get("preserved_prefix_count") == len(baseline_preserved)
        and during.get("preserved_prefix_count") == len(during_preserved)
        and baseline.get("uncertified_prefix_count") == len(baseline_uncertified)
        and during.get("uncertified_prefix_count") == len(during_uncertified)
        and baseline.get("active_lease_count") == 0
        and baseline.get("active_leases") == []
        and during.get("active_lease_count") == 1
        and during.get("active_leases") == expected_marker
        and during.get("known_prefix_count") == baseline.get("known_prefix_count", -1) + 1
        and set(during_paths) - set(baseline_paths) == {str(expected_prefix)}
        and not (set(baseline_paths) - set(during_paths))
        and during_preserved == baseline_preserved
        and during_uncertified == baseline_uncertified
        and during.get("leased_prefix_exists") is True
        and during.get("leased_prefix_is_symlink") is False
        and during.get("leased_prefix_has_registry") is True
        and isinstance(device, int) and not isinstance(device, bool) and device >= 0
        and isinstance(inode, int) and not isinstance(inode, bool) and inode > 0
        and command.get("command") == ["/usr/bin/true"]
        and command.get("returncode") == 0
        and command.get("managed_by_prefixlease_wait_process") is True
        and command.get("wine_app_launched") is False
        and isinstance(pid, int) and not isinstance(pid, bool) and pid > 0
        and isinstance(started, (int, float)) and not isinstance(started, bool)
        and isinstance(completed, (int, float)) and not isinstance(completed, bool)
        and completed >= started and completed - started <= 30
        and command.get("state_before") == during
        and command.get("state_after") == during
        and after == baseline
    )


def _test_smoke_report(path: Path) -> dict[str, Any]:
    preflight_bytes = storage_policy._read_file_nofollow(ATTESTATION)
    preflight = json.loads(preflight_bytes)
    preflight_sha256 = _sha(preflight_bytes)
    smoke_id = preflight.get("permitted_smoke_experiment_id")
    preflight_age = time.time() - float(preflight.get("created_at_unix", 0))
    if (preflight.get("status") != "PREFLIGHT_PASS" or preflight.get("mode") != "preflight"
            or preflight.get("prefixlease_smoke_status") != "PENDING"
            or not storage_policy._is_storage_policy_smoke_id(smoke_id)
            or not storage_policy._verify_gate0_signature(preflight)
            or preflight_age < 0 or preflight_age > storage_policy.GATE0_MAX_AGE_SECONDS):
        raise RuntimeError("PrefixLease smoke must use a fresh authenticated Gate 0 preflight")
    expected_output = REPO / "experiments/storage/smoke-runs" / smoke_id
    expected_report = expected_output / "prefix-smoke-report.json"
    expected_cleanup = expected_output / "prefix-cleanup.json"
    expected_preflight = expected_output / "gate0-preflight-attestation.json"
    expected_claim = storage_policy._storage_smoke_claim_path(smoke_id)
    expected_manifest = storage_policy._experiment_manifest_path(smoke_id)
    expected_prefix = storage_policy.PREFIX_ROOT / f"storage-smoke-{smoke_id}"
    if Path(os.path.abspath(path)) != expected_report:
        raise RuntimeError("PrefixLease smoke report must be the one fixed canonical report path")
    report = _read_json(path)
    if (report.get("experiment_id") != smoke_id or report.get("status") != "PASS"
            or report.get("graphics_experiments_run") is not False
            or report.get("wine_app_launched") is not False
            or report.get("prefix_created") is not True
            or report.get("prefix_deleted") is not True
            or report.get("storage_hygiene_status") != "PASS"
            or report.get("free_space_status") != "PASS"
            or report.get("system_exit_zero_cleanup_precedence") is not True
            or report.get("failure_cleanup_test") != "PASS"
            or report.get("uncertified_prefix_rejection") != "PASS"
            or report.get("preserved_prefix_policy") != "PASS"):
        raise RuntimeError("PrefixLease smoke report is incomplete or failed its storage-only contract")
    if not storage_policy._verify_storage_evidence(report, "smoke_report_hmac_sha256"):
        raise RuntimeError("PrefixLease smoke report signature is missing or invalid")
    if not _valid_prefix_state_evidence(
            report.get("prefix_state_evidence"), smoke_id, expected_prefix):
        raise RuntimeError("PrefixLease smoke lacks proof of one active prefix and exact baseline restoration")
    report_created = report.get("created_at_unix")
    if (not isinstance(report_created, (int, float)) or report_created < preflight.get("created_at_unix", 0)
            or time.time() - float(report_created) < 0
            or time.time() - float(report_created) > storage_policy.GATE0_MAX_AGE_SECONDS):
        raise RuntimeError("PrefixLease smoke report is missing a fresh timestamp after preflight")
    suite = report.get("synthetic_policy_suite")
    required_tests = {
        "test_normal_cleanup_removes_prefix",
        "test_original_failure_still_runs_and_finishes_cleanup",
        "test_system_exit_zero_cannot_hide_cleanup_failure",
        "test_uncertified_prefix_is_rejected",
        "test_preservation_stays_within_configured_cap",
        "test_full_preserved_cap_leaves_uncertified_prefix_for_gate_block",
    }
    suite_path = HERE / "test_storage_policy.py"
    if (not isinstance(suite, dict) or suite.get("status") != "PASS"
            or suite.get("sha256") != _sha(storage_policy._read_file_nofollow(suite_path))
            or set(suite.get("tests", [])) != required_tests
            or suite.get("test_count", 0) < len(required_tests)
            or suite.get("skipped_count") != 0
            or not isinstance(suite.get("suite_output_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", suite["suite_output_sha256"])):
        raise RuntimeError("PrefixLease smoke report does not bind the current synthetic policy test suite")
    focused_suite = suite.get("focused_gate0_checks") if isinstance(suite, dict) else None
    focused_tests = {
        "test_stale_inventory_hash_rejects_admission",
        "test_stale_cleanup_receipt_hash_rejects_admission",
        "test_stale_control_hash_rejects_admission",
        "test_cleanup_cannot_pass_below_gate0_free_or_project_threshold",
        "test_threshold_cleanup_blocks_signed_gate_for_next_admission",
        "test_smoke_evidence_requires_one_active_prefix_and_baseline_return",
        "test_invalid_signed_gate_is_not_rewritten_during_threshold_failure",
        "test_failed_initial_threshold_observation_cannot_be_overwritten_by_later_sample",
        "test_wait_process_timeout_reaps_child_and_reports_timeout",
        "test_unreaped_timeout_marks_lease_unsafe",
    }
    focused_path = HERE / "test_g0c_checks.py"
    if (not isinstance(focused_suite, dict) or focused_suite.get("status") != "PASS"
            or focused_suite.get("test_file") != str(focused_path)
            or focused_suite.get("sha256") != _sha(storage_policy._read_file_nofollow(focused_path))
            or set(focused_suite.get("tests", [])) != focused_tests
            or focused_suite.get("test_count", 0) < len(focused_tests)
            or focused_suite.get("skipped_count") != 0
            or not isinstance(focused_suite.get("suite_output_sha256"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", focused_suite["suite_output_sha256"])):
        raise RuntimeError("PrefixLease smoke report does not bind the focused Gate 0 checks")
    manifest_path = Path(report.get("manifest_path", ""))
    if (manifest_path != expected_manifest
            or report.get("manifest_sha256") != _sha(storage_policy._read_file_nofollow(manifest_path))):
        raise RuntimeError("PrefixLease smoke report does not bind the canonical experiment manifest")
    manifest = _read_json(manifest_path)
    if (not storage_policy._verify_manifest_signature(manifest)
            or manifest.get("experiment_id") != smoke_id
            or manifest.get("decision") != "ALLOW"
            or manifest.get("storage_policy_smoke") is not True
            or manifest.get("output_dir") != str(expected_output)
            or manifest.get("expected_duration_seconds") != 300
            or manifest.get("expected_prefix_bytes") != 3 * storage_policy.GIB
            or manifest.get("expected_build_bytes") != 0
            or manifest.get("estimated_persistent_bytes") != 32 * storage_policy.MIB
            or manifest.get("gate0_attestation_sha256") != preflight_sha256
            or not isinstance(manifest.get("created_at_unix"), (int, float))
            or manifest["created_at_unix"] < preflight.get("created_at_unix", 0)):
        raise RuntimeError("PrefixLease smoke manifest is not a signed fixed-contract ALLOW")
    if (report.get("preflight_attestation_path") != str(expected_preflight)
            or report.get("preflight_attestation_sha256") != preflight_sha256):
        raise RuntimeError("PrefixLease smoke report does not bind the current preflight attestation")
    copied_preflight = storage_policy._read_file_nofollow(expected_preflight)
    if copied_preflight != preflight_bytes:
        raise RuntimeError("PrefixLease smoke retained preflight copy differs from the current signed preflight")
    copied_preflight_json = json.loads(copied_preflight)
    if (not storage_policy._verify_gate0_signature(copied_preflight_json)
            or copied_preflight_json.get("permitted_smoke_experiment_id") != smoke_id):
        raise RuntimeError("retained PrefixLease preflight evidence is invalid")
    claim, claim_bytes = storage_policy._read_storage_smoke_claim(smoke_id)
    claim_sha256 = _sha(claim_bytes)
    if (claim.get("preflight_attestation_sha256") != preflight_sha256
            or claim.get("claim_path") != str(expected_claim)
            or claim_sha256 != manifest.get("smoke_nonce_claim_sha256")
            or manifest.get("smoke_nonce_claim_path") != str(expected_claim)
            or report.get("smoke_nonce_claim_path") != str(expected_claim)
            or report.get("smoke_nonce_claim_sha256") != claim_sha256
            or claim.get("claimed_at_unix", float("inf")) < preflight.get("created_at_unix", 0)
            or claim.get("claimed_at_unix", float("inf")) > manifest.get("created_at_unix", float("inf"))):
        raise RuntimeError("PrefixLease smoke nonce claim is missing, replayed, or bound to another preflight")
    cleanup_path = Path(report.get("cleanup_receipt", ""))
    if (cleanup_path != expected_cleanup
            or report.get("cleanup_receipt_sha256") != _sha(storage_policy._read_file_nofollow(cleanup_path))):
        raise RuntimeError("PrefixLease smoke report does not name the canonical cleanup receipt")
    cleanup = _read_json(cleanup_path)
    cleanup_timestamp = storage_policy._cleanup_receipt_timestamp(cleanup)
    preflight_cas_audit = preflight.get("canonical_artifact_store_audit")
    if (not storage_policy._verify_storage_evidence(cleanup, "cleanup_receipt_hmac_sha256")
            or cleanup.get("status") != "PASS"
            or cleanup.get("experiment_id") != smoke_id
            or cleanup.get("prefix_path") != str(expected_prefix)
            or cleanup.get("prefix_created") is not True
            or cleanup.get("prefix_deleted") is not True
            or cleanup.get("prefix_preserved") is not False
            or cleanup.get("storage_hygiene_status") != "PASS"
            or cleanup.get("free_space_status") != "PASS"
            or cleanup.get("receipt_finalization_complete") is not True
            or cleanup.get("active_lease_marker_removed_after_receipt") is not True
            or cleanup.get("canonical_artifact_store_audit") != preflight_cas_audit
            or cleanup_timestamp is None
            or cleanup_timestamp < manifest.get("created_at_unix", 0)
            or cleanup_timestamp > report_created):
        raise RuntimeError("PrefixLease smoke cleanup receipt does not prove successful deletion")
    if (expected_prefix.exists() or expected_prefix.is_symlink()
            or storage_policy._active_lease_markers()
            or storage_policy.uncertified_prefixes()):
        raise RuntimeError("PrefixLease smoke left a prefix or active/uncertified lease behind")
    return {"experiment_id": smoke_id,
            # Attestations persist repository-rooted canonical paths regardless of
            # whether the CLI argument was relative or absolute.  The final
            # read-back verifier compares this exact identity after serialization.
            "report_path": str(expected_report), "report_sha256": _sha(path.read_bytes()),
            "manifest_path": str(expected_manifest), "manifest_sha256": _sha(expected_manifest.read_bytes()),
            "cleanup_receipt_path": str(cleanup_path),
            "cleanup_receipt_sha256": _sha(cleanup_path.read_bytes()),
            "preflight_attestation_path": str(expected_preflight),
            "preflight_attestation_sha256": preflight_sha256,
            "smoke_nonce_claim_path": str(expected_claim),
            "smoke_nonce_claim_sha256": claim_sha256,
            "status": "PASS"}


def _verify_g0c_completion_evidence() -> None:
    """Do not attest around a failed migration, stale review, or partial tests."""
    review = _read_json(HERE / "g0c-launcher-independent-review.json")
    reviewed_files = review.get("reviewed_files", {})
    if (review.get("status") != "PASS"
            or reviewed_files.get("source", {}).get("sha256") != _sha(storage_policy._read_file_nofollow(HERE / "storage_policy.py"))
            or reviewed_files.get("tests", {}).get("sha256") != _sha(storage_policy._read_file_nofollow(HERE / "test_wine_launcher_policy.py"))
            or review.get("findings")):
        raise RuntimeError("launcher independent review is failed or does not bind the current source")
    consolidation = _read_json(HERE / "g0c-evidence-consolidation-receipt.json")
    if (not storage_policy._verify_storage_evidence(consolidation, "receipt_hmac_sha256")
            or consolidation.get("status") != "PASS"
            or consolidation.get("copy_bytes") != 0
            or consolidation.get("active_operation") is not None
            or not consolidation.get("actions")):
        raise RuntimeError("zero-copy evidence consolidation has no authenticated completed PASS")
    plan_bytes = storage_policy._read_file_nofollow(HERE / "g0c-evidence-consolidation-plan.json")
    if consolidation.get("plan_sha256") != _sha(plan_bytes):
        raise RuntimeError("consolidation receipt does not bind the exact reviewed plan")
    plan = json.loads(plan_bytes)
    actions = consolidation["actions"]
    if (not storage_policy._verify_storage_evidence(plan, "plan_hmac_sha256")
            or len(actions) != len(plan.get("rows", []))
            or {(row["path"], row["sha256"]) for row in actions}
            != {(row["path"], row["sha256"]) for row in plan.get("rows", [])}):
        raise RuntimeError("consolidation receipt omits or changes reviewed evidence references")
    # Verify the canonical path, manifest reference and bytes remain live after
    # consolidation. None of these reference paths depend on retired build roots.
    import consolidate_evidence
    approved_roots = consolidate_evidence.approved_roots()
    for action in consolidation["actions"]:
        path = Path(action["path"])
        consolidate_evidence.validate_path(path, approved_roots)
        canonical, current = consolidate_evidence.canonical_record(action["sha256"])
        if (action.get("result") not in {"REFERENCE_INSTALLED_HASH_VERIFIED", "RECOVERED_COMPLETED_REFERENCE"}
                or str(canonical) != action["canonical_path"]
                or action["path"] not in current["original_paths"]
                or not path.is_symlink() or os.readlink(path) != str(canonical)):
            raise RuntimeError("consolidation reference failed live identity verification")
    for name, minimum in (("g0c-launcher-tests.log", 14), ("g0c-checks-tests.log", 13),
                          ("g0c-consolidation-tests.log", 7)):
        log = storage_policy._read_file_nofollow(HERE / name).decode("utf-8")
        ran = re.search(r"^Ran ([0-9]+) tests? ", log, re.MULTILINE)
        if (not ran or int(ran.group(1)) < minimum or not re.search(r"^OK$", log, re.MULTILINE)
                or "skipped=" in log or "expected failures=" in log):
            raise RuntimeError(f"supplemental synthetic suite is failed or incomplete: {name}")


def _refresh(mode: str, smoke_report_path: Path | None = None) -> dict[str, Any]:
    if mode not in {"preflight", "final"}:
        raise ValueError(mode)
    if mode == "preflight" and smoke_report_path is not None:
        raise ValueError("preflight does not accept a smoke report")
    if mode == "final" and smoke_report_path is None:
        raise ValueError("finalization requires the PrefixLease smoke report")

    _verify_g0c_completion_evidence()

    prefix_reconciliation = (storage_policy.reconcile_abandoned_storage_smoke_lease_markers()
                             if mode == "preflight" else [])
    test_report_sha = _run_test_suite()
    smoke = _test_smoke_report(smoke_report_path) if smoke_report_path else None
    artifact_store_audit: dict[str, Any] = {}
    preflight_cas_audit_sha256: str | None = None
    if mode == "final":
        preflight_bytes = storage_policy._read_file_nofollow(ATTESTATION)
        preflight = json.loads(preflight_bytes)
        if (not storage_policy._verify_gate0_signature(preflight)
                or preflight.get("status") != "PREFLIGHT_PASS"
                or preflight.get("mode") != "preflight"
                or smoke is None
                or smoke.get("preflight_attestation_sha256") != _sha(preflight_bytes)):
            raise RuntimeError("final CAS audit is not bound to the authenticated current preflight")
        preflight_cas_audit_sha256 = preflight.get("canonical_artifact_store_audit", {}).get("sha256")
        if not isinstance(preflight_cas_audit_sha256, str):
            raise RuntimeError("preflight does not contain a whole-store CAS link/content audit")
        artifact_store_audit = build_retention.canonical_artifact_store_audit()
        if artifact_store_audit.get("sha256") != preflight_cas_audit_sha256:
            raise RuntimeError("whole-store CAS content or link-count identity changed after PrefixLease preflight")
    hardlink_copy_estimate = []
    hardlink_copy_bytes = 0
    hardlink_migration_actions = []
    if mode == "preflight":
        with storage_policy._acquire_storage_allocation_lock():
            artifact_digests = _artifact_digests_referenced_by_retained_evidence_and_runner()
            hardlink_copy_estimate = build_retention.legacy_artifact_hardlink_estimate(artifact_digests)
            hardlink_copy_bytes = sum(int(row["size_bytes"]) for row in hardlink_copy_estimate
                                      if row.get("copy_required", True))
            free_before_copy = storage_policy.disk_free_bytes(REPO)
            project_before_copy = storage_policy.measure_project_usage()
            project_bytes_before_copy = int(project_before_copy["allocated_inode_deduplicated_bytes"])
            if free_before_copy - hardlink_copy_bytes < storage_policy.FREE_MINIMUM:
                reason = "legacy CAS hardlink isolation would cross Gate 0's 45 GiB free-space minimum"
                _record_blocked_storage_preflight(
                    reason, free_bytes=free_before_copy, project_usage=project_before_copy,
                    hardlink_copy_estimate=hardlink_copy_estimate,
                    hardlink_copy_bytes=hardlink_copy_bytes)
                raise RuntimeError(reason)
            if project_bytes_before_copy + hardlink_copy_bytes > storage_policy.GATE0_PROJECT_BUDGET:
                reason = "legacy CAS hardlink isolation would cross Gate 0's scoped project budget"
                _record_blocked_storage_preflight(
                    reason, free_bytes=free_before_copy, project_usage=project_before_copy,
                    hardlink_copy_estimate=hardlink_copy_estimate,
                    hardlink_copy_bytes=hardlink_copy_bytes)
                raise RuntimeError(reason)
            hardlink_migration_actions = build_retention.detach_legacy_artifact_hardlinks(
                [row["sha256"] for row in hardlink_copy_estimate],
                reason="Step 11D.3 Gate 0 isolates schema-v1 CAS payloads from mutable build-tree hardlink aliases")
            remaining_referenced_hardlinks = build_retention.legacy_artifact_hardlink_estimate(artifact_digests)
            if remaining_referenced_hardlinks:
                raise RuntimeError("retained build/run evidence still has linked or unrecovered CAS outputs: "
                                   + ", ".join(row["sha256"] for row in remaining_referenced_hardlinks[:12]))
            artifact_store_audit = build_retention.canonical_artifact_store_audit()
    previous_updated = 0.0
    previous_receipt_sha256 = None
    if RECEIPT.exists() and not RECEIPT.is_symlink():
        old_receipt_bytes = storage_policy._read_file_nofollow(RECEIPT)
        old_receipt = json.loads(old_receipt_bytes)
        previous_updated = float(old_receipt.get("last_updated_at_unix", old_receipt.get("created_at_unix", 0)))
        previous_receipt_sha256 = _sha(old_receipt_bytes)
    if previous_receipt_sha256 is None:
        raise RuntimeError("a prior cleanup receipt is required to reconcile historical build roots")
    legacy_root_rows = _legacy_standalone_root_reconciliation_rows(previous_receipt_sha256)
    reconciled = build_retention.reconcile_pending()
    retention_snapshot = build_retention.status_snapshot()
    retention_state = _retention_state(retention_snapshot)
    initial_retention_fingerprint = retention_state["retention_fingerprint_sha256"]
    if retention_snapshot.get("gate_status") != "PASS" or retention_snapshot.get("gate_blockers"):
        raise RuntimeError("build-retention state is not ready: "
                           + "; ".join(retention_snapshot.get("gate_blockers", [])))
    if retention_state["active_build_count"] != 0:
        raise RuntimeError("Gate 0 attestation requires zero active large-build leases")
    if mode == "final" and (not reconciled or any(row.get("result") != "REMOVED" for row in reconciled)):
        # A clean final phase is valid when no retirement was pending, so only reject
        # a returned non-removed record.
        if any(row.get("result") != "REMOVED" for row in reconciled):
            raise RuntimeError("final build-retention reconciliation did not complete cleanly")

    prefix = _prefix_metrics()
    prefix_identity = storage_policy.prefix_state_fingerprint()
    if prefix["uncertified_prefix_count"] != 0 or prefix["active_lease_count"] != 0:
        raise RuntimeError("prefix inventory is not clean before Gate 0 refresh")
    worktree_listing = subprocess.run(["git", "worktree", "list", "--porcelain"], cwd=REPO,
                                      text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      check=False).stdout
    worktree_count = sum(line.startswith("worktree ") for line in worktree_listing.splitlines())

    # Build a phase first, write it durably, then inventory the receipt and controls.
    # Allocated sizes are measured independently; disk free remains the authoritative
    # post-cleanup signal on APFS, where clones cannot be attributed exactly.
    free = storage_policy.disk_free_bytes(REPO)
    project = storage_policy.measure_project_usage()
    project_bytes = int(project["allocated_inode_deduplicated_bytes"])
    build_actions = _new_cleanup_actions(previous_updated)
    if RECEIPT.exists() and not RECEIPT.is_symlink():
        receipt = _read_json(RECEIPT)
    else:
        receipt = {"schema_version": 2, "created_at_unix": time.time(), "phases": []}
    phase = {
        "phase_id": f"step11d3-g0-{mode}-{time.time_ns()}",
        "phase": "build-retention-preflight" if mode == "preflight" else "prefixlease-smoke-and-final-reconciliation",
        "generated_at_unix": time.time(), "completed_at_unix": time.time(),
        "status": "PASS", "storage_hygiene_status": "PASS",
        "storage_policy_reconciliation": mode == "preflight",
        "final_free_bytes": free,
        "fgmetal_scoped_allocated_bytes": project_bytes,
        "global_inode_deduplicated_bytes": None,
        "worktree_count": worktree_count,
        **{key: prefix[key] for key in ("preserved_prefix_count", "uncertified_prefix_count")},
        "active_lease_count": 0,
        "active_prefix_lease_markers": [],
        "build_retention_state": retention_state,
        "build_retention_fingerprint_sha256": retention_state["retention_fingerprint_sha256"],
        "prefix_state_fingerprint_sha256": prefix_identity["sha256"],
        "build_retention_status": retention_snapshot,
        "build_cleanup_actions": build_actions,
        "legacy_standalone_build_root_reconciliation_status": "PASS",
        "legacy_standalone_build_root_reconciliation_count": len(legacy_root_rows),
        "artifact_hardlink_isolation_actions": _artifact_hardlink_receipts(),
        "artifact_hardlink_isolation_copy_estimate_bytes": hardlink_copy_bytes,
        "artifact_hardlink_isolation_actions_this_phase": hardlink_migration_actions,
        "canonical_artifact_store_audit": artifact_store_audit,
        "build_root_reconciliation": _build_root_reconciliation(retention_snapshot, legacy_root_rows),
        "build_reconcile_returned_receipts": reconciled,
        "prefix_lease_reconciliation_actions": prefix_reconciliation,
        "preserved_prefix_paths": prefix["preserved_prefix_paths"],
        "uncertified_prefix_paths": [],
        "prefixlease_smoke": smoke,
        "prefixlease_smoke_status": "PENDING" if mode == "preflight" else "PASS",
        "build_retention_test_report": str(TEST_REPORT),
        "build_retention_test_report_sha256": test_report_sha,
        "filesystem_free_before_bytes": free,
        "filesystem_free_after_bytes": free,
        "project_usage_after": project,
    }
    if free < storage_policy.FREE_MINIMUM:
        raise RuntimeError(f"free disk is below 45 GiB ({free} bytes)")
    if project_bytes > storage_policy.GATE0_PROJECT_BUDGET:
        raise RuntimeError(f"scoped project usage exceeds 45 GiB ({project_bytes} bytes)")
    if _sha(storage_policy._read_file_nofollow(RECEIPT)) != previous_receipt_sha256:
        raise RuntimeError("cleanup receipt changed before the fresh reconciliation phase could be committed")
    receipt.setdefault("phases", []).append(phase)
    receipt["last_updated_at_unix"] = phase["completed_at_unix"]
    receipt["gate0_latest_phase_id"] = phase["phase_id"]
    _write_atomic(RECEIPT, receipt)

    with storage_policy._acquire_storage_allocation_lock():
        if mode == "final":
            artifact_store_audit = build_retention.canonical_artifact_store_audit()
            if artifact_store_audit.get("sha256") != preflight_cas_audit_sha256:
                raise RuntimeError("whole-store CAS content or link-count identity changed after PrefixLease preflight")
        inventory = _refresh_inventory()
        inventory_bytes = INVENTORY.read_bytes()
        metrics = inventory.get("storage_gate_metrics", {})
        retention_snapshot = build_retention.status_snapshot()
        retention_state = _retention_state(retention_snapshot)
        prefix = _prefix_metrics()
        prefix_identity = storage_policy.prefix_state_fingerprint()
        free = int(inventory.get("filesystem", {}).get("free_bytes", 0))
        scoped = int(inventory.get("totals", {}).get("fgmetal_scoped_allocated_inode_bytes", 0))
        if retention_snapshot.get("gate_status") != "PASS" or retention_snapshot.get("gate_blockers"):
            raise RuntimeError("fresh inventory shows build-retention blockers")
        if retention_state["active_build_count"] != 0:
            raise RuntimeError("Gate 0 attestation requires zero active large-build leases at final snapshot")
        if retention_state["retention_fingerprint_sha256"] != initial_retention_fingerprint:
            raise RuntimeError("build-retention identity changed during Gate 0 refresh")
        if prefix["uncertified_prefix_count"] != 0 or prefix["active_lease_count"] != 0:
            raise RuntimeError("fresh inventory shows uncertified or active Wine prefixes")
        if metrics.get("build_retention_fingerprint_sha256") != retention_state["retention_fingerprint_sha256"]:
            raise RuntimeError("fresh inventory build-retention identity fingerprint changed during Gate 0 refresh")
        if metrics.get("prefix_state_fingerprint_sha256") != prefix_identity["sha256"]:
            raise RuntimeError("fresh inventory preserved-prefix identity changed during Gate 0 refresh")
        if int(metrics.get("worktree_count", -1)) != worktree_count:
            raise RuntimeError("fresh inventory worktree count changed during Gate 0 refresh")
        if free < storage_policy.FREE_MINIMUM or scoped > storage_policy.GATE0_PROJECT_BUDGET:
            raise RuntimeError("fresh inventory does not satisfy Gate 0 free-space/project-size thresholds")

        template_marker_path = storage_policy.PREFIX_TEMPLATE / "FGMETAL_PREFIX_TEMPLATE.json"
        marker_raw = template_marker_path.read_bytes()
        template_marker = json.loads(marker_raw)
        template_identity = storage_policy._tree_fingerprint(
            storage_policy.PREFIX_TEMPLATE, exclude={template_marker_path.name})
        if (template_marker.get("graphics_experiments_run") is not False
                or template_marker.get("tree_identity") != template_identity
                or template_marker.get("initialization", {}).get("wineboot_returncode") != 0):
            raise RuntimeError("certified prefix template failed live content or initialization verification")
        if mode == "final" and smoke is None:
            raise RuntimeError("final Gate 0 attestation requires the completed PrefixLease smoke")

        permitted_smoke_id = None
        if mode == "preflight":
            permitted_smoke_id = (f"{storage_policy.STORAGE_POLICY_SMOKE_ID_PREFIX}"
                                  f"{time.strftime('%Y%m%d', time.gmtime())}-{uuid.uuid4().hex[:16]}")

        state_fingerprint = {
            "worktree_count": worktree_count,
            "preserved_prefix_count": prefix["preserved_prefix_count"],
            "uncertified_prefix_count": prefix["uncertified_prefix_count"],
            "active_prefix_lease_count": prefix["active_lease_count"],
            "build_retention_state": retention_state,
            "build_retention_fingerprint_sha256": retention_state["retention_fingerprint_sha256"],
            "prefix_state_fingerprint_sha256": prefix_identity["sha256"],
            "inventory_sha256": _sha(inventory_bytes),
            "free_bytes": free,
            "fgmetal_scoped_allocated_inode_bytes": scoped,
            "fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes": inventory.get("totals", {}).get(
                "fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes"),
            "global_inode_deduplicated_bytes_including_unlinked_mount_upper_bound": inventory.get(
                "totals", {}).get("allocated_bytes_unique_inode_estimate_including_unlinked_mount_upper_bound"),
            "artifact_store_size": metrics.get("artifact_store_size"),
            "canonical_artifact_store_audit": artifact_store_audit,
            "evidence_size": metrics.get("evidence_size"),
        }
        attestation = {
            "schema_version": 1,
            "status": "PREFLIGHT_PASS" if mode == "preflight" else "PASS",
            "mode": mode,
            "created_at_unix": time.time(),
            "inventory_path": str(INVENTORY),
            "inventory_sha256": _sha(inventory_bytes),
            "cleanup_receipt_path": str(RECEIPT),
            "cleanup_receipt_sha256": _sha(RECEIPT.read_bytes()),
            "supplemental_evidence_sha256": {
                name: _sha(storage_policy._read_file_nofollow(HERE / name))
                for name in storage_policy.GATE0_SUPPLEMENTAL_EVIDENCE
            },
            "automatic_retention_controls_installed": True,
        "automatic_pre_run_disk_checks": (
            storage_policy.RUNNER_ADMISSION_CONTRACT == "prefixlease-scoped-managed-process-v16"),
        "automatic_post_run_disk_checks": (
            storage_policy.RUNNER_ADMISSION_CONTRACT == "prefixlease-scoped-managed-process-v16"),
            "runner_admission_contract": storage_policy.RUNNER_ADMISSION_CONTRACT,
            "authorized_runner_source_hashes_required": True,
            "certified_template": True,
            "template_marker_sha256": _sha(marker_raw),
            "template_tree_sha256": template_identity["tree_sha256"],
            "template_file_count": template_identity["file_count"],
            "manifest_signing_key_sha256": _sha(storage_policy._manifest_signing_key()),
            "retention_control_sha256": storage_policy._gate0_control_hashes(),
            "build_retention_state": retention_state,
            "build_retention_fingerprint_sha256": retention_state["retention_fingerprint_sha256"],
            "prefix_state_fingerprint_sha256": prefix_identity["sha256"],
            "worktree_count": worktree_count,
            "active_build_count": retention_state["active_build_count"],
            "preserved_build_count": retention_state["preserved_build_count"],
            "retained_build_count": retention_state["retained_build_count"],
            "prefix_count": prefix["prefix_count"],
            "preserved_prefix_count": prefix["preserved_prefix_count"],
            "uncertified_prefix_count": prefix["uncertified_prefix_count"],
            "active_lease_count": prefix["active_lease_count"],
            "artifact_store_size": metrics.get("artifact_store_size"),
            "canonical_artifact_store_audit": artifact_store_audit,
            "evidence_size": metrics.get("evidence_size"),
            "global_inode_deduplicated_bytes": inventory.get("totals", {}).get("allocated_bytes_unique_inode_estimate"),
            "global_inode_deduplicated_bytes_including_unlinked_mount_upper_bound": inventory.get(
                "totals", {}).get("allocated_bytes_unique_inode_estimate_including_unlinked_mount_upper_bound"),
            "evidence_logical_bytes": (metrics.get("evidence_size") or {}).get("logical_bytes"),
            "evidence_unique_inode_bytes": (metrics.get("evidence_size") or {}).get("unique_inode_allocated_bytes"),
            "measurements": {"free_bytes": free, "fgmetal_scoped_allocated_inode_bytes": scoped},
            "state_fingerprint": state_fingerprint,
            "build_retention_test_report_path": str(TEST_REPORT),
            "build_retention_test_report_sha256": test_report_sha,
            "prefixlease_smoke_status": "PENDING" if mode == "preflight" else "PASS",
            "prefixlease_smoke": smoke,
            "permitted_smoke_experiment_id": permitted_smoke_id,
            "preferred_free_space_met": free >= storage_policy.FREE_PREFERRED,
        }
        attestation["gate0_hmac_sha256"] = hmac.new(
            storage_policy._manifest_signing_key(),
            json.dumps(attestation, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
            hashlib.sha256).hexdigest()
        _write_atomic(ATTESTATION, attestation)
        blockers = storage_policy._gate0_blockers(
            active_experiment_id=permitted_smoke_id,
            allow_storage_smoke=mode == "preflight")
        if blockers:
            # Never leave a signed PASS on disk when the live read-back disagrees.
            attestation["status"] = "BLOCKED"
            attestation["gate0_hmac_sha256"] = hmac.new(
                storage_policy._manifest_signing_key(),
                json.dumps(attestation, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(),
                hashlib.sha256).hexdigest()
            _write_atomic(ATTESTATION, attestation)
            raise RuntimeError("new Gate 0 attestation failed read-back: " + "; ".join(blockers))
        return {"status": attestation["status"], "attestation": str(ATTESTATION),
                "inventory": str(INVENTORY), "cleanup_receipt": str(RECEIPT),
                "free_bytes": free, "fgmetal_scoped_allocated_inode_bytes": scoped,
                "build_retention_state": retention_state, "prefixlease_smoke_status": attestation["prefixlease_smoke_status"],
                "preferred_free_space_met": attestation["preferred_free_space_met"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="write the smoke-only preflight attestation")
    final = sub.add_parser("finalize", help="write the final Gate 0 PASS after the storage smoke")
    final.add_argument("--smoke-report", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        mode = "final" if args.command == "finalize" else "preflight"
        report = _refresh(mode, args.smoke_report if mode == "final" else None)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0
    except BaseException as error:
        print(f"Gate 0 refresh blocked: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
