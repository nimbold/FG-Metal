#!/usr/bin/env python3
"""One-shot read-only R23 closeout preflight using the accepted Host-v3 gate."""
from __future__ import annotations

import base64
import datetime as dt
import fcntl
import hashlib
import json
import os
import stat
import sys
from pathlib import Path
from typing import Any


PROCESS_IDENTITY = {
    "pid": os.getpid(),
    "ppid": os.getppid(),
    "cwd": os.getcwd(),
    "python": sys.executable,
}
RUN_ID = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
RUN_STARTED_AT_UTC = dt.datetime.now(dt.timezone.utc).isoformat()
HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
OUT_DIR = HERE / f"R6-A-R52-R23-PRODUCTION-PREFLIGHT-{RUN_ID}"
OUT_DIR.mkdir(mode=0o700)

BUILD_ID = "r6a-r23-dual-lineage-20261020t214331z"
OWNER_EXPERIMENT = "R6-A-R23-DUAL-LINEAGE-20261020T214331Z"
TARGET = Path.home() / "Library/Caches/FGMetal/builds" / BUILD_ID
PLAN_PATH = REPO / "R6-A-R23-DUAL-LINEAGE-EXECUTION-PLAN-20261020T214331Z.json"
OWNER_MANIFEST_PATH = HERE / "manifests" / f"{OWNER_EXPERIMENT}.json"
EXPECTED_REGISTRY_SHA256 = "437ca21abaeb8be17e802f4bada462ca3394c9722d0aa1b0e3d9039a74587a10"
EXPECTED_OWNER_MANIFEST_SHA256 = "681d0a379bcb2dde2d40802f9a5d079cf72a71b2774ad2801eff2dbae2b83673"
EXPECTED_PLAN_FILE_SHA256 = "0d2c941be4add1ba0883b829d9cfc8f720841f602dd673c03b475f8fcf9ae894"
EXPECTED_PLAN_IDENTITY_SHA256 = "507ef8142396f8cfa0feaa4c9abf924d7b2752e0d2a6d70abce23d774ab6a7bb"
EXPECTED_ADMISSION_IDENTITY = {
    "admission_id": OWNER_EXPERIMENT,
    "sha256": "184cc4776f4aac667e1afc4e5576f1a78d58d0c48547489d6380baa6290e2903",
}
EXPECTED_RESERVATION_BYTES = 5_598_724_098
EXPECTED_ROOT = str(TARGET)
MARKER = ".fgmetal-build-lease.json"

sys.path.insert(0, str(HERE))
import build_retention as retention  # noqa: E402
import storage_policy as policy  # noqa: E402


class PreflightStop(RuntimeError):
    pass


checks: list[dict[str, Any]] = []
captured_scan: dict[str, Any] = {}
capture_events: dict[str, list[Any]] = {
    "lsof": [], "process_snapshot": [], "executable_identity": [],
    "parse": [], "namespace": [], "named_root_checks": [],
}
open_fds: list[int] = []
lock_descriptors: list[int] = []
stdout_path = OUT_DIR / "lsof.stdout.bin"
stderr_path = OUT_DIR / "lsof.stderr.bin"


def safe(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii"),
                "length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [safe(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    return repr(value)


def record(name: str, passed: bool, observed: Any, expected: Any) -> None:
    row = {"name": name, "result": "PASS" if passed else "FAIL",
           "observed": safe(observed), "expected": safe(expected)}
    checks.append(row)
    if not passed:
        raise PreflightStop(f"{name}: observed {observed!r}, expected {expected!r}")


def equal(name: str, observed: Any, expected: Any) -> None:
    record(name, observed == expected, observed, expected)


def condition(name: str, passed: bool, observed: Any) -> None:
    record(name, passed is True, observed, True)


def exists_lstat(path: Path) -> bool:
    try:
        os.stat(path, follow_symlinks=False)
        return True
    except FileNotFoundError:
        return False


def acquire_existing_lock(parent: Path, basename: str, *, require_uid: int | None = None) -> tuple[int, int, dict[str, int]]:
    parent_fd = retention._open_dir(parent, create=False)
    open_fds.append(parent_fd)
    try:
        before = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
        condition(f"{basename}.named_lock_is_private_regular",
                  stat.S_ISREG(before.st_mode) and before.st_nlink == 1
                  and stat.S_IMODE(before.st_mode) == 0o600
                  and (require_uid is None or before.st_uid == require_uid),
                  {"device": before.st_dev, "inode": before.st_ino,
                   "mode": stat.S_IMODE(before.st_mode), "nlink": before.st_nlink,
                   "uid": before.st_uid})
        fd = os.open(basename, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        lock_descriptors.append(fd)
        opened = os.fstat(fd)
        condition(f"{basename}.opened_inode_matches_named_inode",
                  (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino)
                  and stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
                  and stat.S_IMODE(opened.st_mode) == 0o600,
                  {"opened_device": opened.st_dev, "opened_inode": opened.st_ino,
                   "named_device": before.st_dev, "named_inode": before.st_ino})
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        after = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
        condition(f"{basename}.exclusive_lock_and_name_stable",
                  (opened.st_dev, opened.st_ino) == (after.st_dev, after.st_ino),
                  {"opened_device": opened.st_dev, "opened_inode": opened.st_ino,
                   "named_device": after.st_dev, "named_inode": after.st_ino})
        return parent_fd, fd, {"device": opened.st_dev, "inode": opened.st_ino,
                               "mode": stat.S_IMODE(opened.st_mode), "nlink": opened.st_nlink}
    except BaseException:
        raise


def require_record_and_manifest(lease: dict[str, Any], registry_raw: bytes) -> dict[str, Any]:
    equal("registry.sha256", hashlib.sha256(registry_raw).hexdigest(), EXPECTED_REGISTRY_SHA256)
    condition("registry.hmac_valid", retention._verify_seal(lease), "HMAC verification")
    equal("registry.build_id", lease.get("build_id"), BUILD_ID)
    equal("registry.path", lease.get("path"), EXPECTED_ROOT)
    equal("registry.owner_experiment", lease.get("owner_experiment"), OWNER_EXPERIMENT)
    equal("registry.state", lease.get("state"), "ACTIVE")
    equal("registry.schema_version", lease.get("schema_version"), 1)
    equal("registry.expected_bytes", lease.get("expected_bytes"), EXPECTED_RESERVATION_BYTES)
    equal("registry.estimated_bytes", lease.get("estimated_bytes"), EXPECTED_RESERVATION_BYTES)

    session = lease.get("session_v3")
    condition("registry.session_v3_is_object", isinstance(session, dict), type(session).__name__)
    equal("session.state", session.get("state"), "RESERVED")
    equal("session.next_step_index", session.get("next_step_index"), 0)
    equal("session.peak_allocated_bytes", session.get("peak_allocated_bytes"), 0)
    equal("session.step_receipts", session.get("step_receipts"), [])
    equal("session.plan_sha256", session.get("plan_sha256"), EXPECTED_PLAN_IDENTITY_SHA256)
    equal("session.admission_identity", session.get("admission_identity"), EXPECTED_ADMISSION_IDENTITY)
    condition("session.not_started", lease.get("session_started") in (None, False)
              and lease.get("session_started_at_unix") is None
              and session.get("started_at_unix") is None,
              {"session_started": lease.get("session_started"),
               "lease_started_at": lease.get("session_started_at_unix"),
               "session_started_at": session.get("started_at_unix")})
    condition("session.no_mount_or_acceptance_state",
              session.get("workspace_mount_pending") is not True
              and session.get("workspace_mount_identity") is None
              and session.get("acceptance_state") is None
              and session.get("final_source_verification") is None,
              {key: session.get(key) for key in (
                  "workspace_mount_pending", "workspace_mount_identity",
                  "acceptance_state", "final_source_verification")})
    condition("registry.no_terminal_or_execution_receipts",
              lease.get("build_manifest_id") is None
              and lease.get("build_manifest_sha256") is None
              and lease.get("retired_at_unix") is None
              and lease.get("cleanup_receipt_path") is None
              and lease.get("build_execution_receipt") is None,
              {key: lease.get(key) for key in (
                  "build_manifest_id", "build_manifest_sha256", "retired_at_unix",
                  "cleanup_receipt_path", "build_execution_receipt")})

    plan_raw = PLAN_PATH.read_bytes()
    equal("plan.file_sha256", hashlib.sha256(plan_raw).hexdigest(), EXPECTED_PLAN_FILE_SHA256)
    plan = json.loads(plan_raw)
    equal("plan.canonical_identity_sha256",
          hashlib.sha256(retention._canonical_json(plan)).hexdigest(), EXPECTED_PLAN_IDENTITY_SHA256)
    equal("plan.build_id", plan.get("build_id"), BUILD_ID)
    equal("plan.reservation_bytes", plan.get("reservation_bytes"), EXPECTED_RESERVATION_BYTES)
    equal("plan.admission_identity", plan.get("admission_identity"), EXPECTED_ADMISSION_IDENTITY)
    equal("plan.build_workspace", plan.get("build_workspace"), str(TARGET / retention.BUILD_SESSION_WORKSPACE_NAME))

    manifest_fd = retention._open_dir(OWNER_MANIFEST_PATH.parent, create=False)
    open_fds.append(manifest_fd)
    manifest_raw = retention._read_at(manifest_fd, OWNER_MANIFEST_PATH.name, limit=1024 * 1024)
    equal("owner_manifest.file_sha256", hashlib.sha256(manifest_raw).hexdigest(),
          EXPECTED_OWNER_MANIFEST_SHA256)
    manifest = json.loads(manifest_raw)
    condition("owner_manifest.hmac_valid", policy._verify_manifest_signature(manifest),
              "manifest HMAC verification")
    equal("owner_manifest.experiment_id", manifest.get("experiment_id"), OWNER_EXPERIMENT)
    equal("owner_manifest.decision", manifest.get("decision"), "ALLOW")
    equal("owner_manifest.blockers", manifest.get("blockers"), [])
    equal("owner_manifest.plan_binding", manifest.get("build_session_plan_sha256"),
          EXPECTED_PLAN_IDENTITY_SHA256)
    equal("owner_manifest.admission_binding", manifest.get("execution_admission_identity"),
          EXPECTED_ADMISSION_IDENTITY)
    estimates = policy._validated_manifest_estimates(manifest)
    equal("owner_manifest.expected_build_bytes", estimates.get("expected_build_bytes"),
          EXPECTED_RESERVATION_BYTES)
    return session


def main() -> tuple[bool, str | None]:
    cache_parent_fd, allocation_lock_fd, allocation_identity = acquire_existing_lock(
        policy.CACHE, ".storage-allocation.lock", require_uid=os.getuid())
    lease_parent_fd, retention_lock_fd, retention_lock_identity = acquire_existing_lock(
        retention.LEASE_ROOT, retention.BUILD_LOCK)
    retention._verify_named_retention_lock(lease_parent_fd, retention_lock_fd)

    lease_dir_fd = retention._open_dir(retention.LEASE_ROOT, create=False)
    open_fds.append(lease_dir_fd)
    registry_raw = retention._read_at(lease_dir_fd, f"{BUILD_ID}.json", limit=1024 * 1024)
    lease = json.loads(registry_raw)
    session = require_record_and_manifest(lease, registry_raw)

    parent_fd = retention._open_dir(retention.BUILD_ROOT, create=False)
    open_fds.append(parent_fd)
    parent_identity = retention._root_identity(retention.BUILD_ROOT, create=False)
    equal("root.approved_parent", lease.get("approved_parent"), str(retention.BUILD_ROOT))
    equal("root.approved_parent_identity", lease.get("approved_parent_identity"), parent_identity)
    root_fd = os.open(BUILD_ID, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                      | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    open_fds.append(root_fd)
    retention._r51_verify_named_pinned_root(TARGET, parent_fd, root_fd, parent_identity, lease)
    root_info = os.fstat(root_fd)
    root_fsid = getattr(os.fstatvfs(root_fd), "f_fsid", None)
    equal("root.device", root_info.st_dev, lease.get("build_device"))
    equal("root.inode", root_info.st_ino, lease.get("build_inode"))
    equal("root.filesystem_id", root_fsid, lease.get("build_filesystem_id"))
    condition("root.directory_type", stat.S_ISDIR(root_info.st_mode), stat.S_IFDIR)

    marker_raw = retention._read_at(root_fd, MARKER, limit=1024 * 1024)
    marker = json.loads(marker_raw)
    condition("marker.hmac_valid", retention._verify_seal(marker), "HMAC verification")
    equal("marker.exact_registry_bytes", marker_raw, registry_raw)
    equal("marker.canonical_record", retention._canonical_json(marker),
          retention._canonical_json(lease))
    equal("root.entries", sorted(os.listdir(root_fd)), [MARKER])

    receipt_path = retention._orphan_closeout_receipt_path(BUILD_ID)
    normal_receipt = retention.RECEIPT_ROOT / f"{BUILD_ID}.json"
    condition("closeout.receipts_absent",
              not exists_lstat(receipt_path) and not exists_lstat(normal_receipt),
              {"orphan_receipt": str(receipt_path), "normal_receipt": str(normal_receipt)})
    absence_paths = [
        TARGET / retention.BUILD_SESSION_WORKSPACE_NAME,
        TARGET / retention.BUILD_SESSION_DIRECTORY,
        TARGET / retention.BUILD_SESSION_WORKSPACE_IMAGE_NAME,
        TARGET / retention.BUILD_EXECUTION_RECEIPT,
        TARGET / "build-command.json", TARGET / "build.log",
        retention.MANIFEST_ROOT / f"{BUILD_ID}.json",
        retention.MANIFEST_ROOT / f"{BUILD_ID}.failure",
    ]
    existing = [str(path) for path in absence_paths if exists_lstat(path)]
    image_row = retention._session_image_row(TARGET)
    condition("execution.workspace_and_outputs_absent", not existing and image_row is None
              and not os.path.ismount(TARGET / retention.BUILD_SESSION_WORKSPACE_NAME),
              {"existing_paths": existing, "attached_image": image_row,
               "workspace_is_mount": os.path.ismount(TARGET / retention.BUILD_SESSION_WORKSPACE_NAME)})

    other_records = [name for name in os.listdir(lease_dir_fd)
                     if name.endswith(".json") and name != f"{BUILD_ID}.json"]
    condition("competing_registry_scan_bounded", len(other_records) <= 4096, len(other_records))
    active_conflicts: list[str] = []
    for name in other_records:
        raw = retention._read_at(lease_dir_fd, name, limit=1024 * 1024)
        candidate = json.loads(raw)
        condition(f"competing_record.{name}.hmac_valid", isinstance(candidate, dict)
                  and retention._verify_seal(candidate), "HMAC verification")
        candidate_session = candidate.get("session_v3")
        if (candidate.get("state") == "ACTIVE"
                and (candidate.get("owner_experiment") == OWNER_EXPERIMENT
                     or (isinstance(candidate_session, dict)
                         and (candidate_session.get("plan_sha256") == EXPECTED_PLAN_IDENTITY_SHA256
                              or candidate_session.get("admission_identity")
                                  == EXPECTED_ADMISSION_IDENTITY)))):
            active_conflicts.append(name)
    equal("competing_active_reservations", active_conflicts, [])

    retention._verify_orphan_owner_absent(lease)
    record("owner.process_absent_or_proven_reuse", True,
           {"owner_pid": lease.get("owner_pid"), "created_at_unix": lease.get("created_at_unix")},
           "production orphan-owner predicate")
    retention._require_no_matching_orphan_controller(BUILD_ID, OWNER_EXPERIMENT)
    record("matching_controller.processes_absent", True, "production controller/descendant predicate", [])

    before_registry_sha = hashlib.sha256(registry_raw).hexdigest()
    before_marker_sha = hashlib.sha256(marker_raw).hexdigest()
    scanner = retention._load_r51_lsof_scanner()
    original_run = scanner.run_lsof_bounded
    original_snapshot = scanner.process_snapshot
    original_executable = scanner.executable_identity
    original_parse = scanner.parse_lsof
    original_namespace = retention._r51_pinned_namespace
    original_named_root = retention._r51_verify_named_pinned_root

    def capture_run(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_run(*args, **kwargs)
        captured_scan.update(result)
        capture_events["lsof"].append({"argv": result.get("argv"),
                                       "returncode": result.get("returncode")})
        return result

    def capture_snapshot(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_snapshot(*args, **kwargs)
        capture_events["process_snapshot"].append(result)
        return result

    def capture_executable(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_executable(*args, **kwargs)
        capture_events["executable_identity"].append(result)
        return result

    def capture_parse(*args: Any, **kwargs: Any) -> tuple[Any, Any]:
        result = original_parse(*args, **kwargs)
        capture_events["parse"].append(result)
        return result

    def capture_namespace(*args: Any, **kwargs: Any) -> dict[str, Any]:
        result = original_namespace(*args, **kwargs)
        capture_events["namespace"].append({
            "digest": result.get("digest"), "item_count": result.get("item_count"),
            "items": [{"path_b64": base64.b64encode(item["path"]).decode("ascii"),
                       "device": item["device"], "inode": item["inode"],
                       "lsof_type": item["lsof_type"].decode("ascii")}
                      for item in result.get("items", [])],
        })
        return result

    def capture_named_root(*args: Any, **kwargs: Any) -> None:
        original_named_root(*args, **kwargs)
        capture_events["named_root_checks"].append("PASS")

    scanner.run_lsof_bounded = capture_run
    scanner.process_snapshot = capture_snapshot
    scanner.executable_identity = capture_executable
    scanner.parse_lsof = capture_parse
    retention._r51_pinned_namespace = capture_namespace
    retention._r51_verify_named_pinned_root = capture_named_root
    try:
        liveness = retention._require_r51_pinned_lsof_self_only(
            TARGET, parent_fd, root_fd, parent_identity, lease)
    finally:
        scanner.run_lsof_bounded = original_run
        scanner.process_snapshot = original_snapshot
        scanner.executable_identity = original_executable
        scanner.parse_lsof = original_parse
        retention._r51_pinned_namespace = original_namespace
        retention._r51_verify_named_pinned_root = original_named_root

    if isinstance(captured_scan.get("stdout_raw"), bytes):
        stdout_path.write_bytes(captured_scan["stdout_raw"])
    if isinstance(captured_scan.get("stderr_raw"), bytes):
        stderr_path.write_bytes(captured_scan["stderr_raw"])

    equal("production_gate.lsof_invocation_count", len(capture_events["lsof"]), 1)
    equal("production_gate.classification", liveness.get("classification"),
          "STATUS_ONE_EXPECTED_SEARCH_MISS")
    equal("production_gate.row_count", liveness.get("row_count"), 1)
    equal("production_gate.namespace_item_count", liveness.get("namespace_item_count"), 2)
    equal("production_gate.namespace_before_after_count", len(capture_events["namespace"]), 2)
    equal("production_gate.namespace_digest_stable",
          [item.get("digest") for item in capture_events["namespace"]],
          [liveness.get("namespace_digest"), liveness.get("namespace_digest")])
    expected_items = [
        {"path_b64": base64.b64encode(os.fsencode(str(TARGET))).decode("ascii"),
         "device": root_info.st_dev, "inode": root_info.st_ino, "lsof_type": "DIR"},
        {"path_b64": base64.b64encode(os.fsencode(str(TARGET / MARKER))).decode("ascii"),
         "device": root_info.st_dev, "inode": os.stat(MARKER, dir_fd=root_fd,
                                                          follow_symlinks=False).st_ino,
         "lsof_type": "REG"},
    ]
    observed_items = capture_events["namespace"][0].get("items") if capture_events["namespace"] else None
    equal("production_gate.exact_namespace_items", observed_items, expected_items)
    equal("production_gate.exact_scan_argv", captured_scan.get("argv"),
          [retention.R51_LSOF_PATH, retention.R51_LSOF_FIELDS, "+D", str(TARGET)])
    condition("production_gate.raw_capture_present",
              isinstance(captured_scan.get("stdout_raw"), bytes)
              and isinstance(captured_scan.get("stderr_raw"), bytes),
              {"stdout_type": type(captured_scan.get("stdout_raw")).__name__,
               "stderr_type": type(captured_scan.get("stderr_raw")).__name__})
    equal("production_gate.stderr_empty", captured_scan.get("stderr_raw"), b"")
    equal("production_gate.returncode", captured_scan.get("returncode"), 1)

    after_registry_raw = retention._read_at(lease_dir_fd, f"{BUILD_ID}.json", limit=1024 * 1024)
    after_marker_raw = retention._read_at(root_fd, MARKER, limit=1024 * 1024)
    equal("post_scan.registry_bytes_unchanged", hashlib.sha256(after_registry_raw).hexdigest(),
          before_registry_sha)
    equal("post_scan.marker_bytes_unchanged", hashlib.sha256(after_marker_raw).hexdigest(),
          before_marker_sha)
    equal("post_scan.root_entries_unchanged", sorted(os.listdir(root_fd)), [MARKER])
    retention._r51_verify_named_pinned_root(TARGET, parent_fd, root_fd, parent_identity, lease)
    retention._verify_named_retention_lock(lease_parent_fd, retention_lock_fd)
    after_namespace = original_namespace(root_fd, TARGET)
    equal("post_scan.namespace_digest_unchanged", after_namespace.get("digest"),
          liveness.get("namespace_digest"))
    retention._verify_orphan_owner_absent(lease)
    retention._require_no_matching_orphan_controller(BUILD_ID, OWNER_EXPERIMENT)
    condition("post_scan.closeout_receipts_still_absent",
              not exists_lstat(receipt_path) and not exists_lstat(normal_receipt), True)

    stdout = captured_scan["stdout_raw"]
    stderr = captured_scan["stderr_raw"]
    result = {
        "run_id": RUN_ID,
        "status": "PASS",
        "result": "R23_ORPHAN_PREFLIGHT = PASS",
        "run_started_at_utc": RUN_STARTED_AT_UTC,
        "run_finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "process_identity": PROCESS_IDENTITY,
        "preflight_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "production_source_sha256": {
            "build_retention.py": hashlib.sha256(Path(retention.__file__).read_bytes()).hexdigest(),
            "accepted_scanner.py": hashlib.sha256(Path(retention.R51_ACCEPTED_SCANNER_PATH).read_bytes()).hexdigest(),
        },
        "binding": {"build_id": BUILD_ID, "owner_experiment": OWNER_EXPERIMENT,
                    "expected_path": EXPECTED_ROOT,
                    "registry_sha256": before_registry_sha,
                    "marker_sha256": before_marker_sha,
                    "owner_manifest_sha256": EXPECTED_OWNER_MANIFEST_SHA256,
                    "plan_file_sha256": EXPECTED_PLAN_FILE_SHA256,
                    "plan_identity_sha256": EXPECTED_PLAN_IDENTITY_SHA256,
                    "admission_identity": EXPECTED_ADMISSION_IDENTITY,
                    "reservation_bytes": EXPECTED_RESERVATION_BYTES},
        "locks": {"allocation": allocation_identity, "retention": retention_lock_identity},
        "checks": checks,
        "production_liveness_gate": safe(liveness),
        "production_gate_capture": {
            "lsof": {key: safe(value) for key, value in captured_scan.items()
                     if key not in {"stdout_raw", "stderr_raw"}},
            "stdout": {"path": stdout_path.name, "length": len(stdout),
                       "sha256": hashlib.sha256(stdout).hexdigest()},
            "stderr": {"path": stderr_path.name, "length": len(stderr),
                       "sha256": hashlib.sha256(stderr).hexdigest()},
            "process_snapshots": safe(capture_events["process_snapshot"]),
            "executable_identities": safe(capture_events["executable_identity"]),
            "parsed_rows_and_stream": safe(capture_events["parse"]),
            "namespace_before_and_after": capture_events["namespace"],
            "named_root_rechecks": capture_events["named_root_checks"],
        },
        "mutations": [],
        "diagnostic_scan_invoked": False,
        "production_gate_invocations": 1,
        "closeout_invoked": False,
    }
    report_path = OUT_DIR / "preflight.json"
    report_path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return True, str(report_path)


success = False
report_path: str | None = None
failure: str | None = None
try:
    success, report_path = main()
except BaseException as error:
    failure = f"{type(error).__name__}: {error}"
    failed_capture = {key: value for key, value in captured_scan.items()
                      if key not in {"stdout_raw", "stderr_raw"}}
    if isinstance(captured_scan.get("stdout_raw"), bytes):
        stdout_path.write_bytes(captured_scan["stdout_raw"])
        failed_capture["stdout"] = {"path": stdout_path.name,
                                    "length": len(captured_scan["stdout_raw"]),
                                    "sha256": hashlib.sha256(captured_scan["stdout_raw"]).hexdigest()}
    if isinstance(captured_scan.get("stderr_raw"), bytes):
        stderr_path.write_bytes(captured_scan["stderr_raw"])
        failed_capture["stderr"] = {"path": stderr_path.name,
                                    "length": len(captured_scan["stderr_raw"]),
                                    "sha256": hashlib.sha256(captured_scan["stderr_raw"]).hexdigest()}
    report = {
        "run_id": RUN_ID,
        "status": "FAIL",
        "result": "R23_ORPHAN_PREFLIGHT = FAIL",
        "process_identity": PROCESS_IDENTITY,
        "run_started_at_utc": RUN_STARTED_AT_UTC,
        "run_finished_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "preflight_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "checks": checks,
        "first_failure": failure,
        "captured_gate_events": safe(capture_events),
        "production_gate_capture": safe(failed_capture),
        "mutations": [],
        "diagnostic_scan_invoked": False,
        "production_gate_invocations": len(capture_events["lsof"]),
        "closeout_invoked": False,
    }
    report_path = str(OUT_DIR / "preflight.json")
    Path(report_path).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
finally:
    for fd in reversed(lock_descriptors):
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass
    for fd in reversed(open_fds):
        try:
            os.close(fd)
        except OSError:
            pass

if success:
    print(f"R23_ORPHAN_PREFLIGHT = PASS\n{report_path}")
else:
    print(f"R23_ORPHAN_PREFLIGHT = FAIL\n{failure}\n{report_path}", file=sys.stderr)
    sys.exit(1)
