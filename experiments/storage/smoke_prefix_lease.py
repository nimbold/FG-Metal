#!/usr/bin/env python3
"""Validate template cloning and finally cleanup without launching a graphics app."""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "experiments/storage"))
import storage_policy  # noqa: E402
from storage_policy import (  # noqa: E402
    GIB,
    GATE0_ATTESTATION_PATH,
    MIB,
    PREFIX_ROOT,
    PREFIX_TEMPLATE,
    PrefixLease,
    begin_experiment,
    scoped_lease,
    _seal_storage_evidence,
    _write_json,
)

RUNTIME = Path("/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine")
RUNTIME_MANIFEST = REPO / "experiments/storage/runtime-manifests/step11d_r_active_runtime.json"
ICD = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json")
MOLTENVK = Path("/Users/nima/Library/Caches/FGMetalStep11C1R/moltenvk-loader-override/libMoltenVK.1.dylib")


def sha(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def prefix_state_snapshot(prefix: Path) -> dict[str, object]:
    """Capture prefix inventory and active-lease state without mutating admission state."""
    active_leases = []
    for marker_path in storage_policy._active_lease_markers():
        raw = storage_policy._read_file_nofollow(marker_path)
        marker = json.loads(raw)
        if not isinstance(marker, dict):
            raise RuntimeError(f"active PrefixLease marker is not a JSON object: {marker_path}")
        active_leases.append({
            "experiment_id": marker.get("experiment_id"),
            "state": marker.get("state"),
            "prefix_path": marker.get("prefix_path"),
            "marker_path": str(marker_path),
        })
    active_leases.sort(key=lambda row: str(row["experiment_id"]))
    preserved = sorted(str(path) for path in storage_policy.preserved_prefixes())
    uncertified = sorted(str(path) for path in storage_policy.uncertified_prefixes())
    known_prefixes = set(preserved) | set(uncertified)
    known_prefixes.update(
        path for row in active_leases
        if isinstance((path := row.get("prefix_path")), str)
    )
    try:
        prefix_info = prefix.lstat()
    except FileNotFoundError:
        prefix_info = None
    prefix_is_real_directory = prefix_info is not None and stat.S_ISDIR(prefix_info.st_mode)
    return {
        "known_prefix_count": len(known_prefixes),
        "known_prefix_paths": sorted(known_prefixes),
        "preserved_prefix_count": len(preserved),
        "preserved_prefix_paths": preserved,
        "uncertified_prefix_count": len(uncertified),
        "uncertified_prefix_paths": uncertified,
        "active_lease_count": len(active_leases),
        "active_leases": active_leases,
        "leased_prefix_exists": prefix_is_real_directory,
        "leased_prefix_is_symlink": prefix_info is not None and stat.S_ISLNK(prefix_info.st_mode),
        "leased_prefix_device": prefix_info.st_dev if prefix_is_real_directory else None,
        "leased_prefix_inode": prefix_info.st_ino if prefix_is_real_directory else None,
        "leased_prefix_has_registry": (
            (prefix / "system.reg").is_file() and not (prefix / "system.reg").is_symlink()
            if prefix_is_real_directory else False
        ),
    }


def run_storage_control_command(lease: PrefixLease) -> dict[str, object]:
    """Run one fixed, non-Wine system command under PrefixLease process supervision."""
    before = prefix_state_snapshot(lease.prefix)
    expected_marker = {
        "experiment_id": lease.experiment_id,
        "state": "MATERIALIZED",
        "prefix_path": str(lease.prefix),
        "marker_path": str(storage_policy.ACTIVE_LEASE_ROOT / f"{lease.experiment_id}.json"),
    }
    if (before["active_lease_count"] != 1
            or before["active_leases"] != [expected_marker]
            or before["leased_prefix_exists"] is not True
            or before["leased_prefix_has_registry"] is not True):
        raise RuntimeError("storage control command requires exactly one materialized active PrefixLease")

    started_at = time.time()
    process = subprocess.Popen(
        ["/usr/bin/true"], cwd=lease.prefix, env={}, stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True,
    )
    returncode = lease.wait_process(process, timeout_seconds=30)
    completed_at = time.time()
    after = prefix_state_snapshot(lease.prefix)
    if before != after:
        raise RuntimeError("active PrefixLease state changed while the storage control command ran")
    if returncode != 0 or completed_at < started_at:
        raise RuntimeError(f"storage control command failed or has invalid timing: {returncode}")
    return {
        "command": ["/usr/bin/true"],
        "pid": process.pid,
        "returncode": returncode,
        "started_at_unix": started_at,
        "completed_at_unix": completed_at,
        "managed_by_prefixlease_wait_process": True,
        "wine_app_launched": False,
        "state_before": before,
        "state_after": after,
    }


def validate_prefix_state_evidence(evidence: object, experiment_id: str,
                                   prefix: Path) -> bool:
    if not isinstance(evidence, dict):
        return False
    baseline = evidence.get("baseline")
    during = evidence.get("during")
    after = evidence.get("after_cleanup")
    command = evidence.get("controlled_command")
    if not all(isinstance(item, dict) for item in (baseline, during, after, command)):
        return False
    expected_marker = {
        "experiment_id": experiment_id,
        "state": "MATERIALIZED",
        "prefix_path": str(prefix),
        "marker_path": str(storage_policy.ACTIVE_LEASE_ROOT / f"{experiment_id}.json"),
    }
    before_command = command.get("state_before")
    after_command = command.get("state_after")
    return (
        evidence.get("exactly_one_temporary_prefix") is True
        and evidence.get("baseline_restored") is True
        and isinstance(baseline.get("known_prefix_paths"), list)
        and isinstance(during.get("known_prefix_paths"), list)
        and baseline.get("active_lease_count") == 0
        and baseline.get("active_leases") == []
        and during.get("active_lease_count") == 1
        and during.get("active_leases") == [expected_marker]
        and during.get("known_prefix_count") == baseline.get("known_prefix_count", -1) + 1
        and set(during.get("known_prefix_paths", [])) - set(baseline.get("known_prefix_paths", [])) == {str(prefix)}
        and not (set(baseline.get("known_prefix_paths", [])) - set(during.get("known_prefix_paths", [])))
        and during.get("preserved_prefix_paths") == baseline.get("preserved_prefix_paths")
        and during.get("uncertified_prefix_paths") == baseline.get("uncertified_prefix_paths")
        and during.get("leased_prefix_exists") is True
        and during.get("leased_prefix_is_symlink") is False
        and during.get("leased_prefix_has_registry") is True
        and isinstance(before_command, dict)
        and isinstance(after_command, dict)
        and before_command == during
        and after_command == during
        and after == baseline
        and command.get("command") == ["/usr/bin/true"]
        and command.get("returncode") == 0
        and command.get("managed_by_prefixlease_wait_process") is True
        and command.get("wine_app_launched") is False
        and isinstance(command.get("pid"), int)
        and command.get("pid", 0) > 0
        and isinstance(command.get("started_at_unix"), (int, float))
        and isinstance(command.get("completed_at_unix"), (int, float))
        and command.get("completed_at_unix") >= command.get("started_at_unix")
    )


def run_synthetic_policy_suite() -> dict[str, object]:
    test_file = REPO / "experiments/storage/test_storage_policy.py"
    result = subprocess.run([sys.executable, str(test_file), "-v"], cwd=REPO,
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            check=False)
    required = (
        "test_normal_cleanup_removes_prefix",
        "test_original_failure_still_runs_and_finishes_cleanup",
        "test_system_exit_zero_cannot_hide_cleanup_failure",
        "test_uncertified_prefix_is_rejected",
        "test_preservation_stays_within_configured_cap",
        "test_full_preserved_cap_leaves_uncertified_prefix_for_gate_block",
    )
    missing = [name for name in required if name not in result.stdout]
    ran = re.search(r"^Ran ([0-9]+) tests? ", result.stdout, re.MULTILINE)
    failed_status = [name for name in required
                     if not re.search(rf"^{re.escape(name)} \([^\n]+\) \.\.\. ok$",
                                      result.stdout, re.MULTILINE)]
    if (result.returncode != 0 or missing or failed_status or not ran
            or int(ran.group(1)) < len(required) or "OK" not in result.stdout
            or "skipped=" in result.stdout or "expected failures=" in result.stdout):
        raise RuntimeError("synthetic PrefixLease policy suite failed or was incomplete: "
                           + result.stdout[-5000:])
    gate0_test_file = REPO / "experiments/storage/test_g0c_checks.py"
    gate0_result = subprocess.run(
        [sys.executable, str(gate0_test_file), "-v"], cwd=REPO,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=False,
    )
    gate0_required = (
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
    )
    gate0_missing = [name for name in gate0_required if name not in gate0_result.stdout]
    gate0_failed = [name for name in gate0_required
                    if not re.search(rf"^{re.escape(name)} \([^\n]+\) \.\.\. ok$",
                                     gate0_result.stdout, re.MULTILINE)]
    gate0_ran = re.search(r"^Ran ([0-9]+) tests? ", gate0_result.stdout, re.MULTILINE)
    if (gate0_result.returncode != 0 or gate0_missing or gate0_failed or not gate0_ran
            or int(gate0_ran.group(1)) < len(gate0_required) or "OK" not in gate0_result.stdout
            or "skipped=" in gate0_result.stdout or "expected failures=" in gate0_result.stdout):
        raise RuntimeError("focused Gate 0 admission/cleanup suite failed or was incomplete: "
                           + gate0_result.stdout[-5000:])
    return {"status": "PASS", "test_file": str(test_file),
            "sha256": sha(test_file), "test_count": int(ran.group(1)), "skipped_count": 0,
            "suite_output_sha256": hashlib.sha256(
                result.stdout.encode("utf-8")).hexdigest(), "tests": list(required),
            "focused_gate0_checks": {
                "status": "PASS", "test_file": str(gate0_test_file),
                "sha256": sha(gate0_test_file),
                "test_count": int(gate0_ran.group(1)), "skipped_count": 0,
                "suite_output_sha256": hashlib.sha256(
                    gate0_result.stdout.encode("utf-8")).hexdigest(),
                "tests": list(gate0_required),
            }}


def main() -> int:
    policy_suite = run_synthetic_policy_suite()
    preflight_bytes = storage_policy._read_file_nofollow(GATE0_ATTESTATION_PATH)
    preflight = json.loads(preflight_bytes)
    experiment_id = preflight.get("permitted_smoke_experiment_id")
    preflight_age = time.time() - float(preflight.get("created_at_unix", 0))
    if (not storage_policy._is_storage_policy_smoke_id(experiment_id)
            or preflight.get("status") != "PREFLIGHT_PASS"
            or preflight.get("mode") != "preflight"
            or preflight.get("prefixlease_smoke_status") != "PENDING"
            or not storage_policy._verify_gate0_signature(preflight)
            or preflight_age < 0 or preflight_age > storage_policy.GATE0_MAX_AGE_SECONDS):
        raise RuntimeError("storage smoke requires a fresh signed preflight with a nonce-bound run ID")
    runtime_manifest = json.loads(RUNTIME_MANIFEST.read_text(encoding="utf-8"))
    output = REPO / "experiments/storage/smoke-runs" / experiment_id
    prefix = PREFIX_ROOT / f"storage-smoke-{experiment_id}"
    manifest = begin_experiment(
        experiment_id=experiment_id,
        purpose="Storage-only validation of the certified prefix clone and finally cleanup; no Wine app or graphics experiment",
        candidate_hashes={
            "wine_runtime_tree": runtime_manifest["tree_sha256"],
            "wine_binary": sha(RUNTIME / "bin/wine"),
            "moltenvk": sha(MOLTENVK),
        },
        expected_duration_seconds=300,
        expected_prefix_bytes=3 * GIB,
        expected_build_bytes=0,
        estimated_persistent_bytes=32 * MIB,
        storage_justification="Storage-only check of one APFS template clone; the clone is removed in finally and no application is launched.",
        internal_storage_setup=False,
        storage_policy_smoke=True,
        output_dir=output,
        additional={
            "graphics_experiments_run": False,
            "presentation_or_rendering_experiment_run": False,
            "wine_app_launched": False,
            "retention_policy": "Keep only the template and one failure reproduction; remove this smoke clone in finally.",
        },
    )
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    preflight_copy = output / "gate0-preflight-attestation.json"
    descriptor = os.open(preflight_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        view = memoryview(preflight_bytes)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short retained preflight attestation write")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    output_fd = os.open(output, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fsync(output_fd)
    finally:
        os.close(output_fd)
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("WINE", "DXVK_", "FG_", "MVK_", "MTL_", "VK_", "DYLD_")):
            env.pop(key, None)
    runtime_paths = [
        RUNTIME / "lib/wine/x86_64-unix",
        RUNTIME / "lib/wine/x86_64-windows",
        RUNTIME / "lib/wine/i386-windows",
    ]
    dyld = ":".join((str(MOLTENVK.parent), str(runtime_paths[0])))
    env.update({
        "WINEPREFIX": str(prefix),
        "WINEARCH": "win64",
        "WINELOADER": str(RUNTIME / "bin/wine"),
        "WINESERVER": str(RUNTIME / "bin/wineserver"),
        "WINEDATADIR": str(RUNTIME / "share"),
        "WINEDLLPATH": ":".join(str(path) for path in runtime_paths),
        "WINEDLLOVERRIDES": "d3d11,dxgi=n,b;winemenubuilder.exe=d",
        "WINEDEBUG": "-all",
        "VK_DRIVER_FILES": str(ICD),
        "VK_ICD_FILENAMES": str(ICD),
        "DYLD_LIBRARY_PATH": dyld,
        "DYLD_FALLBACK_LIBRARY_PATH": dyld + ":" + str(RUNTIME / "lib") + ":/usr/local/lib:/usr/lib",
    })
    lease = PrefixLease(prefix, output, experiment_id, RUNTIME / "bin/wineserver",
                        env=env, template=PREFIX_TEMPLATE, manifest_path=Path(manifest["manifest_path"]))
    baseline_state = prefix_state_snapshot(prefix)
    if (baseline_state["active_lease_count"] != 0
            or baseline_state["leased_prefix_exists"]
            or str(prefix) in baseline_state["known_prefix_paths"]):
        raise RuntimeError("storage smoke baseline already contains its lease or prefix")
    with scoped_lease(lease):
        lease.set_env(env)
        lease.materialize()
        if not (prefix / "system.reg").is_file():
            raise RuntimeError("materialized prefix did not retain the initialized registry files")
        if not (prefix / "FGMETAL_PREFIX_LEASE.json").is_file():
            raise RuntimeError("materialized prefix lacks its active lease marker")
        during_state = prefix_state_snapshot(prefix)
        control_command = run_storage_control_command(lease)
        if control_command["state_before"] != during_state:
            raise RuntimeError("controlled command did not start from the materialized ACTIVE lease state")
        output.mkdir(parents=True, exist_ok=True)
        if not (output / "prefix-smoke-started.json").exists():
            (output / "prefix-smoke-started.json").write_text(json.dumps({
                "experiment_id": experiment_id,
                "prefix_path": str(prefix),
                "template_tree_sha256": json.loads((PREFIX_TEMPLATE / "FGMETAL_PREFIX_TEMPLATE.json").read_text())["tree_identity"]["tree_sha256"],
                "graphics_experiments_run": False,
                "wine_app_launched": False,
            }, indent=2) + "\n", encoding="utf-8")
    after_cleanup_state = prefix_state_snapshot(prefix)
    prefix_state_evidence = {
        "baseline": baseline_state,
        "during": during_state,
        "after_cleanup": after_cleanup_state,
        "controlled_command": control_command,
        "exactly_one_temporary_prefix": True,
        "baseline_restored": after_cleanup_state == baseline_state,
    }
    if not validate_prefix_state_evidence(prefix_state_evidence, experiment_id, prefix):
        raise RuntimeError("storage smoke prefix count/state evidence did not prove one-prefix baseline return")
    cleanup = json.loads((output / "prefix-cleanup.json").read_text(encoding="utf-8"))
    claim, claim_bytes = storage_policy._read_storage_smoke_claim(experiment_id)
    preflight_sha256 = hashlib.sha256(preflight_bytes).hexdigest()
    if claim.get("preflight_attestation_sha256") != preflight_sha256:
        raise RuntimeError("storage-smoke nonce claim does not bind the retained preflight")
    report = {
        "schema_version": 1,
        "experiment_id": experiment_id,
        "manifest_path": manifest["manifest_path"],
        "manifest_sha256": sha(Path(manifest["manifest_path"])),
        "created_at_unix": time.time(),
        "preflight_attestation_path": str(preflight_copy),
        "preflight_attestation_sha256": preflight_sha256,
        "smoke_nonce_claim_path": str(storage_policy._storage_smoke_claim_path(experiment_id)),
        "smoke_nonce_claim_sha256": hashlib.sha256(claim_bytes).hexdigest(),
        "cleanup_receipt": str(output / "prefix-cleanup.json"),
        "cleanup_receipt_sha256": sha(output / "prefix-cleanup.json"),
        "prefix_created": cleanup.get("prefix_created"),
        "prefix_deleted": cleanup.get("prefix_deleted"),
        "bytes_reclaimed": cleanup.get("bytes_reclaimed"),
        "storage_hygiene_status": cleanup.get("storage_hygiene_status"),
        "free_space_status": cleanup.get("free_space_status"),
        "status": cleanup.get("status"),
        "graphics_experiments_run": False,
        "wine_app_launched": False,
        "system_exit_zero_cleanup_precedence": True,
        "failure_cleanup_test": "PASS",
        "uncertified_prefix_rejection": "PASS",
        "preserved_prefix_policy": "PASS",
        "prefix_state_evidence": prefix_state_evidence,
        "synthetic_policy_suite": policy_suite,
    }
    report_path = output / "prefix-smoke-report.json"
    _write_json(report_path, _seal_storage_evidence(report, "smoke_report_hmac_sha256"),
                strict_directory_sync=True)
    print(json.dumps(report, indent=2))
    return 0 if (
        cleanup.get("prefix_created") is True
        and cleanup.get("prefix_deleted") is True
        and cleanup.get("storage_hygiene_status") == "PASS"
        and cleanup.get("free_space_status") == "PASS"
        and cleanup.get("status") == "PASS"
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
