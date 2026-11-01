#!/usr/bin/env python3
"""One read-only R23 parent/root identity precheck for R32.

This harness writes only its new diagnostic JSON/Markdown outputs in the
repository. It has no R23 closeout or state-write path and deliberately does
not launch lsof.
"""
from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import platform
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


BUILD_ID = "r6a-r23-dual-lineage-20261020t214331z"
OWNER = "R6-A-R23-DUAL-LINEAGE-20261020T214331Z"
ADMISSION_SHA256 = "681d0a379bcb2dde2d40802f9a5d079cf72a71b2774ad2801eff2dbae2b83673"
PLAN_FILE_SHA256 = "0d2c941be4add1ba0883b829d9cfc8f720841f602dd673c03b475f8fcf9ae894"
PLAN_CANONICAL_SHA256 = "507ef8142396f8cfa0feaa4c9abf924d7b2752e0d2a6d70abce23d774ab6a7bb"
R27_CONTROLLER_SHA256 = "3c1669e56713babf36f99037aee3a5a07e8c40e4ed6a41ac1558466d1a8f1997"
R27_TESTS_SHA256 = "00a8ceda17823358599de1832474f94378ff1db2514c07b62a7cc6803ecab19b"
POLICY_SHA256 = "83f57849e4ec634bfb70c6f9accb0ad5a813ebb8bbb2aaac5cea84c76049b28c"
PLAN_FILE = "R6-A-R23-DUAL-LINEAGE-EXECUTION-PLAN-20261020T214331Z.json"
ADMISSION_FILE = "experiments/storage/manifests/R6-A-R23-DUAL-LINEAGE-20261020T214331Z.json"
ALLOCATION_LOCK = ".storage-allocation.lock"
RETENTION_LOCK = ".retention.lock"
SIGNING_KEY_NAME = ".storage-manifest-signing-key"
REGISTRY_NAME = f"{BUILD_ID}.json"
OUTPUT_JSON_NAME = "R6-A-R32-R23-PARENT-ROOT-PRECHECK-20261021.json"
OUTPUT_MD_NAME = "R6-A-R32-R23-PARENT-ROOT-PRECHECK-20261021.md"
MISSING = object()

PARENT_FIELDS: dict[str, type] = {
    "device": int,
    "inode": int,
    "filesystem_id": int,
    "mode": int,
    "sticky": bool,
    "owner_uid": int,
}

REQUIRED_IDENTITY_ROW_IDS = {
    "APPROVED_PARENT_PATH_EXACT",
    "PARENT_PATH_CANONICAL",
    "PARENT_IS_DIRECTORY",
    "PARENT_SCHEMA_KEYS",
    "PARENT_PATH_DICT_EQUALITY",
    "PARENT_FD_DICT_EQUALITY",
    "PARENT_PATH_FD_DICT_EQUALITY",
    "ROOT_PATH_EXACT",
    "ROOT_PATH_CANONICAL",
    "ROOT_PARENT_RELATIONSHIP",
    "APPROVED_PARENT_EXACT_ROOT_BINDING",
    "PINNED_ROOT_DEVICE",
    "PINNED_ROOT_INODE",
    "PINNED_ROOT_DIRECTORY",
    "PINNED_ROOT_FSID",
    "NAMED_ROOT_DEVICE",
    "NAMED_ROOT_INODE",
    "NAMED_ROOT_DIRECTORY",
    "NAMED_PIN_DEVICE_EQUALITY",
    "NAMED_PIN_INODE_EQUALITY",
    "NAMED_PIN_TYPE_EQUALITY",
    "ROOT_NAMED_PIN_IDENTITY",
}
for _parent_field in PARENT_FIELDS:
    REQUIRED_IDENTITY_ROW_IDS.add(f"PARENT_PATH_{_parent_field.upper()}")
    REQUIRED_IDENTITY_ROW_IDS.add(f"PARENT_FD_{_parent_field.upper()}")
    REQUIRED_IDENTITY_ROW_IDS.add(f"PARENT_PATH_FD_{_parent_field.upper()}_EQUALITY")


class PrecheckError(RuntimeError):
    pass


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def process_snapshot() -> dict[str, Any]:
    """Capture checker identity before path or admission decisions."""
    pid = os.getpid()
    ppid = os.getppid()
    cwd = os.getcwd()
    cp = subprocess.run(
        ["/bin/ps", "-p", str(pid), "-o", "pid=", "-o", "ppid=", "-o", "lstart=", "-o", "command="],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    raw = cp.stdout.decode("utf-8", "strict").strip()
    if cp.returncode != 0 or not raw:
        raise PrecheckError(f"checker process query failed: status={cp.returncode}; stderr={cp.stderr!r}")
    parts = raw.split(None, 7)
    if len(parts) != 8:
        raise PrecheckError(f"checker process query was incomplete: {raw!r}")
    try:
        observed_pid = int(parts[0])
        observed_ppid = int(parts[1])
    except ValueError as error:
        raise PrecheckError(f"checker process query had invalid PID fields: {raw!r}") from error
    if observed_pid != pid or observed_ppid != ppid or pid <= 0 or ppid < 0 or not os.path.isabs(cwd):
        raise PrecheckError("checker PID/PPID/cwd did not agree with the live process query")
    return {
        "pid": pid,
        "pid_from_ps": observed_pid,
        "ppid": ppid,
        "ppid_from_ps": observed_ppid,
        "start_identity": " ".join(parts[2:7]),
        "command": parts[7],
        "cwd": cwd,
        "ps_argv": ["/bin/ps", "-p", str(pid), "-o", "pid=", "-o", "ppid=", "-o", "lstart=", "-o", "command="],
        "ps_returncode": cp.returncode,
        "ps_stdout": cp.stdout.decode("utf-8", "strict"),
        "ps_stderr": cp.stderr.decode("utf-8", "replace"),
    }


def open_dir_nofollow(path: Path) -> int:
    absolute = Path(os.path.abspath(os.fspath(path)))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor or "/", flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise PrecheckError(f"path component is not a directory: {absolute}")
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def read_at(directory_fd: int, name: str, *, limit: int = 1_048_576) -> bytes:
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=directory_fd)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size < 0 or info.st_size > limit:
            raise PrecheckError(f"expected bounded regular file: {name}")
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(fd, min(65_536, limit + 1 - total))
            if not block:
                return b"".join(blocks)
            blocks.append(block)
            total += len(block)
            if total > limit:
                raise PrecheckError(f"file exceeds bounded read limit: {name}")
    finally:
        os.close(fd)


def read_path_nofollow(path: Path, *, limit: int = 8 * 1024 * 1024) -> bytes:
    parent_fd = open_dir_nofollow(path.parent)
    try:
        return read_at(parent_fd, path.name, limit=limit)
    finally:
        os.close(parent_fd)


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def verify_lease_seal(payload: Any, signing_key: bytes) -> bool:
    if type(payload) is not dict:
        return False
    value = dict(payload)
    actual = value.pop("lease_hmac_sha256", MISSING)
    if type(actual) is not str:
        return False
    expected = hmac.new(signing_key, canonical_json(value), hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual, expected)


def verify_manifest_signature(payload: Any, signing_key: bytes) -> bool:
    """Verify the historical admission HMAC without importing workspace code."""
    if type(payload) is not dict:
        return False
    value = dict(payload)
    actual = value.pop("manifest_hmac_sha256", MISSING)
    if type(actual) is not str:
        return False
    expected = hmac.new(signing_key, canonical_json(value), hashlib.sha256).hexdigest()
    return hmac.compare_digest(actual, expected)


def stat_record(value: os.stat_result) -> dict[str, Any]:
    return {
        "st_dev": value.st_dev,
        "st_ino": value.st_ino,
        "st_mode": value.st_mode,
        "permission_mode": stat.S_IMODE(value.st_mode),
        "type": stat.S_IFMT(value.st_mode),
        "is_directory": stat.S_ISDIR(value.st_mode),
        "st_uid": value.st_uid,
        "st_gid": value.st_gid,
        "st_nlink": value.st_nlink,
    }


def identity_from_fd(fd: int) -> tuple[dict[str, Any], os.stat_result, Any]:
    info = os.fstat(fd)
    fsinfo = os.fstatvfs(fd)
    fsid = getattr(fsinfo, "f_fsid", MISSING)
    identity = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "filesystem_id": fsid,
        "mode": stat.S_IMODE(info.st_mode),
        "sticky": bool(info.st_mode & stat.S_ISVTX),
        "owner_uid": info.st_uid,
    }
    return identity, info, fsid


def identity_from_path(path: Path) -> dict[str, Any]:
    """Independently open a canonical path with no-follow component traversal."""
    fd = open_dir_nofollow(path)
    try:
        identity, _, _ = identity_from_fd(fd)
        return identity
    finally:
        os.close(fd)


def typed_equal(expected: Any, observed: Any, expected_type: type) -> tuple[bool, str]:
    if expected is MISSING or observed is MISSING:
        return False, "required value missing"
    if type(expected) is not expected_type:
        return False, f"expected value type is {type(expected).__name__}, required {expected_type.__name__}"
    if type(observed) is not expected_type:
        return False, f"observed value type is {type(observed).__name__}, required {expected_type.__name__}"
    return expected == observed, "exact typed equality"


def serializable(value: Any) -> Any:
    if value is MISSING:
        return {"missing": True}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return repr(value)


def write_new_file(directory_fd: int, name: str, data: bytes) -> None:
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=directory_fd)
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short diagnostic write")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def markdown_report(report: dict[str, Any]) -> bytes:
    rows = [
        "# R6-A R32 R23 Parent/Root Read-Only Precheck",
        "",
        f"**Classification:** `{report['classification']}`",
        f"**First failure:** `{report['first_failure'] or 'none'}`",
        "**lsof:** not launched",
        "**R23 mutation:** none",
        "**Closeout:** not invoked",
        "",
        "## Process identity",
        "",
        "```json",
        json.dumps(report.get("process_identity"), indent=2, sort_keys=True),
        "```",
        "",
        "## Authentication and lock checks",
        "",
        "| ID | Predicate | Result | Expected | Observed |",
        "|---|---|---|---|---|",
    ]
    for check in report["checks"]:
        if check["phase"] != "parent_root_identity":
            rows.append(
                f"| {check['id']} | {check['name']} | {check['result']} | "
                f"{json.dumps(check['expected'], sort_keys=True)} | {json.dumps(check['observed'], sort_keys=True)} |"
            )
    rows += ["", "## Parent/root identity matrix", "", "| ID | Predicate | Result | Expected | Observed | Type |",
             "|---|---|---|---|---|---|"]
    for check in report["checks"]:
        if check["phase"] == "parent_root_identity":
            rows.append(
                f"| {check['id']} | {check['name']} | {check['result']} | "
                f"{json.dumps(check['expected'], sort_keys=True)} | {json.dumps(check['observed'], sort_keys=True)} | {check['value_type']} |"
            )
    rows += ["", "## Captured descriptors and revalidation", "", "```json",
             json.dumps(report.get("parent_capture"), indent=2, sort_keys=True), "```", "",
             "Diagnostic output files are outside R23. The harness has no lsof, closeout, registry-write, marker-write, or build-root write operation.", ""]
    return "\n".join(rows).encode("utf-8")


def main() -> int:
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": "R6-A-R32-R23-PARENT-ROOT-PRECHECK-20261021",
        "scope": "one corrected read-only R23 parent/root identity precheck; lsof and closeout excluded",
        "argv": list(sys.argv),
        "environment": {"python": sys.version},
        "checks": [],
        "first_failure": None,
        "classification": "PARENT_ROOT_IDENTITY_FAIL",
        "parent_capture": {},
        "mutations": [],
        "lsof": {"launched": False},
        "closeout": {"invoked": False},
    }
    check_ids: set[str] = set()

    def add_check(check_id: str, phase: str, name: str, expected: Any, observed: Any,
                  passed: bool, *, value_type: str, comparison: str,
                  authority_source: str, reason: str | None = None) -> None:
        row = {
            "id": check_id,
            "phase": phase,
            "name": name,
            "expected": serializable(expected),
            "observed": serializable(observed),
            "value_type": value_type,
            "comparison": comparison,
            "result": "PASS" if passed else "FAIL",
            "authority_source": authority_source,
        }
        if reason:
            row["reason"] = reason
        report["checks"].append(row)
        check_ids.add(check_id)
        if not passed and report["first_failure"] is None:
            report["first_failure"] = check_id

    def add_typed(check_id: str, phase: str, name: str, expected: Any, observed: Any,
                  expected_type: type, authority_source: str) -> bool:
        passed, reason = typed_equal(expected, observed, expected_type)
        add_check(check_id, phase, name, expected, observed, passed,
                  value_type=expected_type.__name__, comparison="exact typed equality",
                  authority_source=authority_source, reason=None if passed else reason)
        return passed

    process_ok = False
    try:
        # Required to be the first runtime capture, before canonical path or identity decisions.
        try:
            report["process_identity"] = process_snapshot()
            report["environment"]["platform"] = platform.platform()
            process_capture = report["process_identity"]
            process_checks = [
                ("PROCESS_PID", "checker PID matches /bin/ps", process_capture["pid"], process_capture["pid_from_ps"], int,
                 type(process_capture["pid"]) is int and process_capture["pid"] > 0
                 and process_capture["pid"] == process_capture["pid_from_ps"]),
                ("PROCESS_PPID", "checker PPID matches /bin/ps", process_capture["ppid"], process_capture["ppid_from_ps"], int,
                 type(process_capture["ppid"]) is int and process_capture["ppid"] >= 0
                 and process_capture["ppid"] == process_capture["ppid_from_ps"]),
                ("PROCESS_START_IDENTITY", "checker process-start identity captured", "nonempty ps lstart string",
                 process_capture["start_identity"], str,
                 type(process_capture["start_identity"]) is str and bool(process_capture["start_identity"])),
                ("PROCESS_CWD", "checker cwd captured as an absolute path", "absolute path",
                 process_capture["cwd"], str,
                 type(process_capture["cwd"]) is str and os.path.isabs(process_capture["cwd"])),
                ("PROCESS_COMMAND", "checker command captured", "nonempty ps command string",
                 process_capture["command"], str,
                 type(process_capture["command"]) is str and bool(process_capture["command"])),
                ("PROCESS_PS_STATUS", "checker process query completed", 0, process_capture["ps_returncode"], int,
                 type(process_capture["ps_returncode"]) is int and process_capture["ps_returncode"] == 0),
            ]
            process_ok = True
            for check_id, name, expected_value, observed_value, value_type, passed in process_checks:
                add_check(check_id, "process", name, expected_value, observed_value, passed,
                          value_type=value_type.__name__, comparison="individual exact process-capture predicate",
                          authority_source="os.getpid/os.getppid/os.getcwd and /bin/ps")
                process_ok = process_ok and passed
        except Exception as error:
            add_check("PROCESS_IDENTITY", "process", "checker PID/PPID/start/cwd captured first",
                      "valid live process snapshot", f"{type(error).__name__}: {error}", False,
                      value_type="process snapshot", comparison="required capture",
                      authority_source="os and /bin/ps", reason=str(error))

        if not process_ok:
            raise PrecheckError("process identity capture failed; R23 authentication and identity paths were not accessed")

        script_path = Path(os.path.realpath(__file__))
        repo = script_path.parents[2]
        storage_dir = repo / "experiments" / "storage"
        cache_root = Path.home() / "Library" / "Caches" / "FGMetal"
        build_root = cache_root / "builds"
        target_path = build_root / BUILD_ID
        lease_root = storage_dir / "build-leases"
        admission_path = repo / ADMISSION_FILE
        plan_path = repo / PLAN_FILE
        output_json_path = storage_dir / OUTPUT_JSON_NAME
        output_md_path = storage_dir / OUTPUT_MD_NAME
        report["canonical_paths_captured_before_security_decisions"] = {
            "script": str(script_path),
            "repository": str(repo),
            "cwd": os.path.realpath(os.getcwd()),
            "approved_parent_expected": os.path.realpath(str(build_root)),
            "historical_root_expected": os.path.realpath(str(target_path)),
            "manifest_signing_key": os.path.realpath(str(cache_root / SIGNING_KEY_NAME)),
            "lease_root": os.path.realpath(str(lease_root)),
            "registry": os.path.realpath(str(lease_root / REGISTRY_NAME)),
            "marker": os.path.realpath(str(target_path / ".fgmetal-build-lease.json")),
            "allocation_lock": os.path.realpath(str(cache_root / ALLOCATION_LOCK)),
            "retention_lock": os.path.realpath(str(lease_root / RETENTION_LOCK)),
            "admission": os.path.realpath(str(admission_path)),
            "plan": os.path.realpath(str(plan_path)),
        }
        for output_path in (output_json_path, output_md_path):
            if os.path.lexists(output_path):
                raise PrecheckError(f"refusing to overwrite diagnostic output: {output_path}")

        controller_raw = read_path_nofollow(storage_dir / "build_retention.py")
        tests_raw = read_path_nofollow(storage_dir / "test_build_retention.py")
        controller_hash = sha256(controller_raw)
        tests_hash = sha256(tests_raw)
        add_typed("R27_CONTROLLER_HASH", "source", "accepted R27 controller bytes",
                  R27_CONTROLLER_SHA256, controller_hash, str, "R27 accepted hash supplied in R32 request")
        add_typed("R27_TESTS_HASH", "source", "accepted R27 test bytes",
                  R27_TESTS_SHA256, tests_hash, str, "R27 accepted hash supplied in R32 request")
        source_failures = [row["id"] for row in report["checks"]
                           if row["phase"] == "source" and row["result"] != "PASS"]
        if source_failures:
            raise PrecheckError(f"accepted R27 source hash check failed: {source_failures}")

        cache_fd = open_dir_nofollow(cache_root)
        allocation_fd = os.open(ALLOCATION_LOCK, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                | getattr(os, "O_NONBLOCK", 0), dir_fd=cache_fd)
        allocation_before = os.fstat(allocation_fd)
        allocation_named = os.stat(ALLOCATION_LOCK, dir_fd=cache_fd, follow_symlinks=False)
        allocation_type_ok = stat.S_ISREG(allocation_before.st_mode)
        allocation_mode_ok = stat.S_IMODE(allocation_before.st_mode) == 0o600
        allocation_nlink_ok = allocation_before.st_nlink == 1
        allocation_owner_ok = allocation_before.st_uid == os.getuid()
        allocation_name_fd_device_ok = allocation_before.st_dev == allocation_named.st_dev
        allocation_name_fd_inode_ok = allocation_before.st_ino == allocation_named.st_ino
        try:
            fcntl.flock(allocation_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            allocation_held = True
        except OSError:
            allocation_held = False
        for check_id, name, expected_value, observed_value, passed, comparison in [
            ("ALLOC_LOCK_TYPE", "allocation lock is a regular file", "regular file", stat.S_IFMT(allocation_before.st_mode), allocation_type_ok, "S_ISREG(fstat.st_mode)"),
            ("ALLOC_LOCK_MODE", "allocation lock permission mode is 0600", 0o600, stat.S_IMODE(allocation_before.st_mode), allocation_mode_ok, "exact permission mode"),
            ("ALLOC_LOCK_NLINK", "allocation lock link count is one", 1, allocation_before.st_nlink, allocation_nlink_ok, "exact link count"),
            ("ALLOC_LOCK_OWNER", "allocation lock is owned by current UID", os.getuid(), allocation_before.st_uid, allocation_owner_ok, "exact UID equality"),
            ("ALLOC_LOCK_NAMED_FD_DEVICE", "allocation lock named device equals descriptor device",
             allocation_named.st_dev, allocation_before.st_dev, allocation_name_fd_device_ok, "exact device equality"),
            ("ALLOC_LOCK_NAMED_FD_INODE", "allocation lock named inode equals descriptor inode",
             allocation_named.st_ino, allocation_before.st_ino, allocation_name_fd_inode_ok, "exact inode equality"),
            ("ALLOC_LOCK_EXCLUSIVE", "allocation lock acquired exclusively without waiting", True, allocation_held, allocation_held, "nonblocking exclusive flock"),
        ]:
            add_check(check_id, "locks", name, expected_value, observed_value, passed,
                      value_type="individual lock predicate", comparison=comparison,
                      authority_source="descriptor fstat, no-follow named stat, fcntl.flock")
        allocation_valid = all((allocation_type_ok, allocation_mode_ok, allocation_nlink_ok,
                                 allocation_owner_ok, allocation_name_fd_device_ok,
                                 allocation_name_fd_inode_ok, allocation_held))
        if not allocation_valid or not allocation_held:
            raise PrecheckError("allocation lock precondition failed")

        lease_fd = open_dir_nofollow(lease_root)
        retention_fd = os.open(RETENTION_LOCK, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                               | getattr(os, "O_NONBLOCK", 0), dir_fd=lease_fd)
        retention_before = os.fstat(retention_fd)
        retention_named = os.stat(RETENTION_LOCK, dir_fd=lease_fd, follow_symlinks=False)
        retention_type_ok = stat.S_ISREG(retention_before.st_mode)
        retention_mode_ok = stat.S_IMODE(retention_before.st_mode) == 0o600
        retention_nlink_ok = retention_before.st_nlink == 1
        retention_owner_ok = retention_before.st_uid == os.getuid()
        retention_name_fd_device_ok = retention_before.st_dev == retention_named.st_dev
        retention_name_fd_inode_ok = retention_before.st_ino == retention_named.st_ino
        try:
            fcntl.flock(retention_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            retention_held = True
        except OSError:
            retention_held = False
        for check_id, name, expected_value, observed_value, passed, comparison in [
            ("RETENTION_LOCK_TYPE", "retention lock is a regular file", "regular file", stat.S_IFMT(retention_before.st_mode), retention_type_ok, "S_ISREG(fstat.st_mode)"),
            ("RETENTION_LOCK_MODE", "retention lock permission mode is 0600", 0o600, stat.S_IMODE(retention_before.st_mode), retention_mode_ok, "exact permission mode"),
            ("RETENTION_LOCK_NLINK", "retention lock link count is one", 1, retention_before.st_nlink, retention_nlink_ok, "exact link count"),
            ("RETENTION_LOCK_OWNER", "retention lock is owned by current UID", os.getuid(), retention_before.st_uid, retention_owner_ok, "exact UID equality"),
            ("RETENTION_LOCK_NAMED_FD_DEVICE", "retention lock named device equals descriptor device",
             retention_named.st_dev, retention_before.st_dev, retention_name_fd_device_ok, "exact device equality"),
            ("RETENTION_LOCK_NAMED_FD_INODE", "retention lock named inode equals descriptor inode",
             retention_named.st_ino, retention_before.st_ino, retention_name_fd_inode_ok, "exact inode equality"),
            ("RETENTION_LOCK_EXCLUSIVE", "retention lock acquired exclusively without waiting", True, retention_held, retention_held, "nonblocking exclusive flock"),
        ]:
            add_check(check_id, "locks", name, expected_value, observed_value, passed,
                      value_type="individual lock predicate", comparison=comparison,
                      authority_source="descriptor fstat, no-follow named stat, fcntl.flock")
        retention_valid = all((retention_type_ok, retention_mode_ok, retention_nlink_ok, retention_owner_ok,
                               retention_name_fd_device_ok, retention_name_fd_inode_ok,
                               retention_held))
        if not retention_valid or not retention_held:
            raise PrecheckError("retention lock precondition failed")

        signing_key_fd = os.open(SIGNING_KEY_NAME, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                                 | getattr(os, "O_NONBLOCK", 0), dir_fd=cache_fd)
        signing_key_before = os.fstat(signing_key_fd)
        signing_key_named = os.stat(SIGNING_KEY_NAME, dir_fd=cache_fd, follow_symlinks=False)
        signing_key_regular = stat.S_ISREG(signing_key_before.st_mode)
        signing_key_mode_ok = stat.S_IMODE(signing_key_before.st_mode) == 0o600
        signing_key_nlink_ok = signing_key_before.st_nlink == 1
        signing_key_owner_ok = signing_key_before.st_uid == os.getuid()
        signing_key_name_device_ok = signing_key_named.st_dev == signing_key_before.st_dev
        signing_key_name_inode_ok = signing_key_named.st_ino == signing_key_before.st_ino
        for check_id, name, expected_value, observed_value, passed, comparison in [
            ("SIGNING_KEY_TYPE", "manifest signing key is a regular file", "regular file",
             stat.S_IFMT(signing_key_before.st_mode), signing_key_regular, "S_ISREG(fstat.st_mode)"),
            ("SIGNING_KEY_MODE", "manifest signing key permission mode is 0600", 0o600,
             stat.S_IMODE(signing_key_before.st_mode), signing_key_mode_ok, "exact permission mode"),
            ("SIGNING_KEY_NLINK", "manifest signing key link count is one", 1,
             signing_key_before.st_nlink, signing_key_nlink_ok, "exact link count"),
            ("SIGNING_KEY_OWNER", "manifest signing key is owned by current UID", os.getuid(),
             signing_key_before.st_uid, signing_key_owner_ok, "exact UID equality"),
            ("SIGNING_KEY_NAMED_FD_DEVICE", "manifest key name device equals descriptor device",
             signing_key_named.st_dev, signing_key_before.st_dev, signing_key_name_device_ok, "exact device equality"),
            ("SIGNING_KEY_NAMED_FD_INODE", "manifest key name inode equals descriptor inode",
             signing_key_named.st_ino, signing_key_before.st_ino, signing_key_name_inode_ok, "exact inode equality"),
        ]:
            add_check(check_id, "authentication", name, expected_value, observed_value, passed,
                      value_type="individual signing-key predicate", comparison=comparison,
                      authority_source="no-follow key open, descriptor fstat, and no-follow named stat")
        if not all((signing_key_regular, signing_key_mode_ok, signing_key_nlink_ok,
                    signing_key_owner_ok, signing_key_name_device_ok, signing_key_name_inode_ok)):
            raise PrecheckError("manifest signing key metadata or name binding failed")
        key_buffer = bytearray()
        while len(key_buffer) <= 32:
            block = os.read(signing_key_fd, 33 - len(key_buffer))
            if not block:
                break
            key_buffer.extend(block)
        key_length_ok = len(key_buffer) == 32
        add_check("SIGNING_KEY_LENGTH", "authentication", "manifest signing key contains exactly 32 bytes",
                  32, len(key_buffer), key_length_ok,
                  value_type="byte count", comparison="exact length equality",
                  authority_source="read from retained no-follow signing-key descriptor")
        if not key_length_ok:
            raise PrecheckError("manifest signing key must contain exactly 32 bytes")
        key = bytes(key_buffer)
        report["authentication_method"] = {
            "workspace_modules_imported": [],
            "manifest_signature": "stdlib HMAC-SHA256 over canonical JSON without manifest_hmac_sha256",
            "lease_seal": "stdlib HMAC-SHA256 over canonical JSON without lease_hmac_sha256",
            "signing_key_source": SIGNING_KEY_NAME,
            "signing_key_bytes_reported": False,
        }

        admission_raw = read_path_nofollow(admission_path)
        admission = json.loads(admission_raw)
        admission_hash = sha256(admission_raw)
        admission_is_dict = type(admission) is dict
        admission_signature_valid = admission_is_dict and verify_manifest_signature(admission, key)
        add_check("ADMISSION_OBJECT_TYPE", "authentication", "historical admission is a JSON object",
                  "dict", type(admission).__name__, admission_is_dict,
                  value_type="JSON type", comparison="exact dict type",
                  authority_source="historical R23 admission file")
        add_typed("ADMISSION_SHA256", "authentication", "historical admission bytes match the exact accepted SHA-256",
                  ADMISSION_SHA256, admission_hash, str, "R32 exact historical admission hash")
        add_check("ADMISSION_SIGNATURE", "authentication", "historical admission has a valid production signature",
                  True, admission_signature_valid, admission_signature_valid,
                  value_type="boolean", comparison="stdlib HMAC-SHA256 over canonical JSON without manifest_hmac_sha256",
                  authority_source="historical admission signature contract and protected signing-key descriptor")
        if not (admission_is_dict and admission_hash == ADMISSION_SHA256 and admission_signature_valid):
            raise PrecheckError("R23 admission type, exact hash, or signature check failed")

        plan_raw = read_path_nofollow(plan_path)
        plan = json.loads(plan_raw)
        plan_file_hash = sha256(plan_raw)
        plan_canonical_hash = sha256(canonical_json(plan))
        plan_is_dict = type(plan) is dict
        add_check("PLAN_OBJECT_TYPE", "authentication", "R23 plan is a JSON object",
                  "dict", type(plan).__name__, plan_is_dict,
                  value_type="JSON type", comparison="exact dict type", authority_source="historical R23 plan file")
        add_typed("PLAN_FILE_SHA256", "authentication", "R23 plan file bytes match exact historical SHA-256",
                  PLAN_FILE_SHA256, plan_file_hash, str, "historical R23 plan file hash")
        add_typed("PLAN_CANONICAL_SHA256", "authentication", "R23 plan canonical JSON matches exact historical SHA-256",
                  PLAN_CANONICAL_SHA256, plan_canonical_hash, str, "historical R23 canonical plan hash")
        if not (plan_is_dict and plan_file_hash == PLAN_FILE_SHA256
                and plan_canonical_hash == PLAN_CANONICAL_SHA256):
            raise PrecheckError("R23 plan type or exact hash check failed")

        registry_raw = read_at(lease_fd, REGISTRY_NAME)
        registry = json.loads(registry_raw)
        registry_is_dict = type(registry) is dict
        registry_sealed = registry_is_dict and verify_lease_seal(registry, key)
        add_check("REGISTRY_OBJECT_TYPE", "authentication", "R23 registry is a JSON object",
                  "dict", type(registry).__name__, registry_is_dict,
                  value_type="JSON type", comparison="exact dict type",
                  authority_source="authenticated Host-v3 registry schema")
        add_check("REGISTRY_SIGNATURE", "authentication", "R23 registry has a valid Host-v3 lease seal",
                  True, registry_sealed, registry_sealed,
                  value_type="boolean", comparison="HMAC-SHA256 over canonical JSON",
                  authority_source="accepted Host-v3 seal contract and existing manifest signing key")
        if not (registry_is_dict and registry_sealed):
            raise PrecheckError("R23 registry type or seal validation failed")

        session = registry.get("session_v3", MISSING)
        session_is_dict = type(session) is dict
        add_check("REGISTRY_SESSION_TYPE", "authentication", "R23 session_v3 record is an object",
                  "dict", type(session).__name__, session_is_dict,
                  value_type="JSON type", comparison="exact dict type",
                  authority_source="authenticated R23 registry schema")
        session = session if session_is_dict else {}
        candidate_hashes = admission.get("candidate_hashes", MISSING)
        candidate_hashes_is_dict = type(candidate_hashes) is dict
        add_check("ADMISSION_CANDIDATE_HASHES_TYPE", "authentication", "admission candidate_hashes is an object",
                  "dict", type(candidate_hashes).__name__, candidate_hashes_is_dict,
                  value_type="JSON type", comparison="exact dict type",
                  authority_source="signed R23 admission schema")
        candidate_hashes = candidate_hashes if candidate_hashes_is_dict else {}

        add_typed("ADMISSION_OWNER", "authentication", "admission experiment_id equals exact R23 owner",
                  OWNER, admission.get("experiment_id", MISSING), str, "signed R23 admission and historical owner")
        add_typed("ADMISSION_DECISION", "authentication", "admission decision equals ALLOW",
                  "ALLOW", admission.get("decision", MISSING), str, "signed R23 admission")
        blockers = admission.get("blockers", MISSING)
        blockers_is_list = type(blockers) is list
        add_check("ADMISSION_BLOCKERS_TYPE", "authentication", "admission blockers is a list",
                  "list", type(blockers).__name__, blockers_is_list,
                  value_type="JSON type", comparison="exact list type", authority_source="signed R23 admission")
        add_check("ADMISSION_BLOCKERS_EMPTY", "authentication", "admission has no blockers",
                  [], blockers, blockers_is_list and blockers == [],
                  value_type="list", comparison="exact empty list equality", authority_source="signed R23 admission")

        registry_admission_identity = session.get("admission_identity", MISSING)
        admission_identity = admission.get("execution_admission_identity", MISSING)
        registry_identity_is_dict = type(registry_admission_identity) is dict
        admission_identity_is_dict = type(admission_identity) is dict
        add_check("REGISTRY_ADMISSION_IDENTITY_TYPE", "authentication", "registry admission identity is an object",
                  "dict", type(registry_admission_identity).__name__, registry_identity_is_dict,
                  value_type="JSON type", comparison="exact dict type", authority_source="authenticated R23 registry")
        add_check("ADMISSION_IDENTITY_TYPE", "authentication", "admission execution identity is an object",
                  "dict", type(admission_identity).__name__, admission_identity_is_dict,
                  value_type="JSON type", comparison="exact dict type", authority_source="signed R23 admission")
        add_check("ADMISSION_IDENTITY_EQUALITY", "authentication", "admission identity exactly equals registry identity",
                  registry_admission_identity, admission_identity,
                  registry_identity_is_dict and admission_identity_is_dict
                  and registry_admission_identity == admission_identity,
                  value_type="identity mapping", comparison="exact dictionary equality",
                  authority_source="authenticated R23 registry and signed R23 admission")

        add_typed("REGISTRY_PLAN_CANONICAL_SHA256", "authentication", "registry session plan digest matches exact R23 plan",
                  PLAN_CANONICAL_SHA256, session.get("plan_sha256", MISSING), str,
                  "authenticated R23 registry and historical canonical plan hash")
        add_typed("ADMISSION_PLAN_FILE_SHA256", "authentication", "admission binds exact R23 plan file hash",
                  PLAN_FILE_SHA256, candidate_hashes.get("plan_file_sha256", MISSING), str,
                  "signed R23 admission and historical plan file hash")
        add_typed("ADMISSION_PLAN_CANONICAL_SHA256", "authentication", "admission binds exact R23 canonical plan hash",
                  PLAN_CANONICAL_SHA256, candidate_hashes.get("plan_canonical_sha256", MISSING), str,
                  "signed R23 admission and historical canonical plan hash")
        add_typed("ADMISSION_POLICY_SHA256", "authentication", "admission binds exact R23 policy hash",
                  POLICY_SHA256, candidate_hashes.get("policy_sha256", MISSING), str,
                  "signed R23 admission and historical policy hash")

        identity = registry.get("approved_parent_identity", MISSING)
        root_path_expected = registry.get("path", MISSING)
        parent_path_expected = registry.get("approved_parent", MISSING)
        identity_is_dict = type(identity) is dict
        add_typed("REGISTRY_BUILD_ID", "authentication", "registry build_id equals exact R23 build ID",
                  BUILD_ID, registry.get("build_id", MISSING), str, "authenticated R23 registry and historical build ID")
        add_typed("REGISTRY_OWNER", "authentication", "registry owner_experiment equals exact R23 owner",
                  OWNER, registry.get("owner_experiment", MISSING), str, "authenticated R23 registry and historical owner")
        add_check("REGISTRY_PATH_TYPE", "authentication", "registry root path is a string",
                  "str", type(root_path_expected).__name__, type(root_path_expected) is str,
                  value_type="JSON type", comparison="exact str type", authority_source="authenticated R23 registry")
        add_check("REGISTRY_PARENT_PATH_TYPE", "authentication", "registry approved_parent path is a string",
                  "str", type(parent_path_expected).__name__, type(parent_path_expected) is str,
                  value_type="JSON type", comparison="exact str type", authority_source="authenticated R23 registry")
        add_check("REGISTRY_PARENT_IDENTITY_TYPE", "authentication", "registry approved_parent_identity is an object",
                  "dict", type(identity).__name__, identity_is_dict,
                  value_type="JSON type", comparison="exact dict type", authority_source="authenticated R23 registry")
        identity_keys = set(identity) if identity_is_dict else set()
        add_check("REGISTRY_PARENT_IDENTITY_KEYS", "authentication", "registry parent identity has exact production key set",
                  sorted(PARENT_FIELDS), sorted(identity_keys),
                  identity_is_dict and identity_keys == set(PARENT_FIELDS),
                  value_type="string-key set", comparison="exact key-set equality",
                  authority_source="accepted Host-v3 _fs_identity schema")
        add_check("REGISTRY_PARENT_PATH_EXACT", "authentication", "authenticated approved_parent equals exact configured historical parent",
                  str(build_root), parent_path_expected,
                  type(parent_path_expected) is str and parent_path_expected == str(build_root),
                  value_type="absolute path string", comparison="exact byte-for-byte path equality",
                  authority_source="authenticated R23 registry and R32 historical path")
        add_check("REGISTRY_ROOT_PATH_EXACT", "authentication", "authenticated root path equals exact configured historical root",
                  str(target_path), root_path_expected,
                  type(root_path_expected) is str and root_path_expected == str(target_path),
                  value_type="absolute path string", comparison="exact byte-for-byte path equality",
                  authority_source="authenticated R23 registry and R32 historical path")
        failed_authentication_rows = [row["id"] for row in report["checks"]
                                      if row["phase"] == "authentication" and row["result"] != "PASS"]
        if failed_authentication_rows:
            raise PrecheckError(f"R23 authentication/binding checks failed: {failed_authentication_rows}")

        # Open the authenticated parent without following links, and capture the named child before pinning it.
        parent_fd = open_dir_nofollow(Path(parent_path_expected))
        parent_path_id = identity_from_path(Path(parent_path_expected))
        parent_fd_identity, parent_fd_stat, parent_fsid = identity_from_fd(parent_fd)
        named_root_stat = os.stat(BUILD_ID, dir_fd=parent_fd, follow_symlinks=False)
        named_root_record = stat_record(named_root_stat)
        named_root_captured_before_pin = True
        report["parent_capture"] = {
            "parent_fd": parent_fd,
            "parent_path_identity": parent_path_id,
            "parent_fd_identity": parent_fd_identity,
            "parent_fd_stat": stat_record(parent_fd_stat),
            "parent_fd_filesystem_id": serializable(parent_fsid),
            "named_root_before_pin": named_root_record,
            "named_root_observed_before_pin": named_root_captured_before_pin,
            "root_pin": None,
            "root_f_getpath": {"attempted": False, "supported": False, "value": None},
        }
        root_fd = os.open(BUILD_ID, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        root_pin_identity, root_pin_stat, root_fsid = identity_from_fd(root_fd)
        report["parent_capture"]["root_pin_fd"] = root_fd
        report["parent_capture"]["root_pin"] = stat_record(root_pin_stat)
        report["parent_capture"]["root_pin_identity"] = root_pin_identity
        report["parent_capture"]["root_pin_filesystem_id"] = serializable(root_fsid)
        report["parent_capture"]["canonical_expected_root"] = os.path.realpath(root_path_expected)
        report["parent_capture"]["canonical_expected_parent"] = os.path.realpath(parent_path_expected)

        marker_raw = read_at(root_fd, ".fgmetal-build-lease.json")
        marker = json.loads(marker_raw)
        marker_is_dict = type(marker) is dict
        marker_valid = marker_is_dict and verify_lease_seal(marker, key)
        marker_canonical_equal = (marker_is_dict and canonical_json(marker) == canonical_json(registry))
        marker_raw_equal = marker_raw == registry_raw
        add_check("MARKER_OBJECT_TYPE", "authentication", "root marker is a JSON object",
                  "dict", type(marker).__name__, marker_is_dict,
                  value_type="JSON type", comparison="exact dict type", authority_source="root-pin marker")
        add_check("MARKER_SIGNATURE", "authentication", "root marker has a valid Host-v3 lease seal",
                  True, marker_valid, marker_valid,
                  value_type="boolean", comparison="HMAC-SHA256 over canonical JSON",
                  authority_source="accepted Host-v3 seal contract and existing manifest signing key")
        add_check("MARKER_CANONICAL_REGISTRY_EQUALITY", "authentication", "marker canonical record equals authenticated registry record",
                  sha256(canonical_json(registry)), sha256(canonical_json(marker)) if marker_is_dict else MISSING,
                  marker_canonical_equal, value_type="canonical JSON SHA-256",
                  comparison="exact canonical record equality",
                  authority_source="root-pin marker and authenticated lease registry")
        add_check("MARKER_RAW_REGISTRY_EQUALITY", "authentication", "marker raw bytes equal authenticated registry bytes",
                  sha256(registry_raw), sha256(marker_raw), marker_raw_equal,
                  value_type="raw byte SHA-256 plus byte equality", comparison="exact byte equality",
                  authority_source="root-pin marker bytes and authenticated lease registry")

        # Evaluate every parent/root conjunct separately; only now can the overall identity be classified.
        authority = "authenticated R23 registry / accepted Host-v3 identity contract"
        expected_parent = identity
        add_check("APPROVED_PARENT_PATH_EXACT", "parent_root_identity",
                  "authenticated approved_parent equals exact configured historical parent",
                  str(build_root), parent_path_expected,
                  type(parent_path_expected) is str and parent_path_expected == str(build_root),
                  value_type="absolute path string", comparison="exact byte-for-byte path string equality",
                  authority_source="authenticated R23 registry and configured Host-v3 BUILD_ROOT")
        required_parent_fields_ok = True
        for field, expected_type in PARENT_FIELDS.items():
            expected_value = expected_parent[field] if field in expected_parent else MISSING
            path_value = parent_path_id[field] if type(parent_path_id) is dict and field in parent_path_id else MISSING
            fd_value = parent_fd_identity[field] if field in parent_fd_identity else MISSING
            required_parent_fields_ok = add_typed(f"PARENT_PATH_{field.upper()}", "parent_root_identity",
                                                   f"authenticated parent {field} equals independently observed path identity",
                                                   expected_value, path_value, expected_type, authority) and required_parent_fields_ok
            add_typed(f"PARENT_FD_{field.upper()}", "parent_root_identity",
                      f"authenticated parent {field} equals retained parent descriptor identity",
                      expected_value, fd_value, expected_type, authority)
            path_value_for_binding = parent_path_id[field] if type(parent_path_id) is dict and field in parent_path_id else MISSING
            fd_value_for_binding = parent_fd_identity[field] if field in parent_fd_identity else MISSING
            add_typed(f"PARENT_PATH_FD_{field.upper()}_EQUALITY", "parent_root_identity",
                      f"independent path-derived parent {field} equals retained parent descriptor {field}",
                      path_value_for_binding, fd_value_for_binding, expected_type,
                      "independent path open plus retained descriptor fstat/fstatvfs")
        extra_expected_keys = sorted(set(expected_parent) - set(PARENT_FIELDS))
        add_check("PARENT_SCHEMA_KEYS", "parent_root_identity", "authenticated parent identity has exact production key set",
                  sorted(PARENT_FIELDS), sorted(expected_parent), not extra_expected_keys and set(expected_parent) == set(PARENT_FIELDS),
                  value_type="string-key set", comparison="exact key-set equality",
                  authority_source="accepted _fs_identity producer and R23 authenticated schema",
                  reason=None if not extra_expected_keys and set(expected_parent) == set(PARENT_FIELDS)
                  else f"missing={sorted(set(PARENT_FIELDS)-set(expected_parent))}; extra={extra_expected_keys}")
        add_check("PARENT_PATH_DICT_EQUALITY", "parent_root_identity", "full authenticated parent dictionary equals path-derived identity",
                  expected_parent, parent_path_id,
                  type(parent_path_id) is dict and expected_parent == parent_path_id,
                  value_type="identity mapping", comparison="full dictionary equality after per-field typed checks",
                  authority_source="authenticated R23 identity and production _root_identity(path)")
        add_check("PARENT_FD_DICT_EQUALITY", "parent_root_identity", "full authenticated parent dictionary equals retained parent descriptor identity",
                  expected_parent, parent_fd_identity,
                  type(parent_fd_identity) is dict and expected_parent == parent_fd_identity,
                  value_type="identity mapping", comparison="full dictionary equality after per-field typed checks",
                  authority_source="authenticated R23 identity and retained descriptor fstat/fstatvfs")
        add_check("PARENT_PATH_FD_DICT_EQUALITY", "parent_root_identity",
                  "full path-derived parent dictionary equals retained descriptor identity",
                  parent_path_id, parent_fd_identity,
                  type(parent_path_id) is dict and type(parent_fd_identity) is dict
                  and parent_path_id == parent_fd_identity,
                  value_type="identity mapping", comparison="full dictionary equality after per-field typed checks",
                  authority_source="independent no-follow path open and retained descriptor fstat/fstatvfs")

        add_check("PARENT_PATH_CANONICAL", "parent_root_identity", "canonical parent pathname equals authenticated approved_parent",
                  parent_path_expected, os.path.realpath(parent_path_expected),
                  type(parent_path_expected) is str and os.path.realpath(parent_path_expected) == parent_path_expected,
                  value_type="canonical absolute path string", comparison="exact string equality",
                  authority_source="authenticated approved_parent and captured canonical path")
        add_check("PARENT_IS_DIRECTORY", "parent_root_identity", "retained approved parent descriptor is a directory",
                  True, stat.S_ISDIR(parent_fd_stat.st_mode), stat.S_ISDIR(parent_fd_stat.st_mode),
                  value_type="boolean", comparison="S_ISDIR(fstat(parent_fd).st_mode)", authority_source="descriptor fstat")

        expected_root_device = registry.get("build_device", MISSING)
        expected_root_inode = registry.get("build_inode", MISSING)
        expected_root_fsid = registry.get("build_filesystem_id", MISSING)
        add_check("ROOT_PATH_CANONICAL", "parent_root_identity", "canonical expected root pathname equals authenticated path",
                  root_path_expected, os.path.realpath(root_path_expected),
                  type(root_path_expected) is str and os.path.realpath(root_path_expected) == root_path_expected,
                  value_type="canonical absolute path string", comparison="exact string equality",
                  authority_source="authenticated R23 path and captured canonical path")
        add_check("ROOT_PATH_EXACT", "parent_root_identity", "authenticated root path equals exact configured historical build root",
                  str(target_path), root_path_expected,
                  type(root_path_expected) is str and root_path_expected == str(target_path),
                  value_type="absolute path string", comparison="exact byte-for-byte path string equality",
                  authority_source="authenticated R23 path and configured historical build-root path")
        add_check("ROOT_PARENT_RELATIONSHIP", "parent_root_identity", "exact historical root path is child of approved parent",
                  parent_path_expected, str(Path(root_path_expected).parent),
                  type(root_path_expected) is str and type(parent_path_expected) is str
                  and str(Path(root_path_expected).parent) == parent_path_expected
                  and Path(root_path_expected).name == BUILD_ID,
                  value_type="path relationship", comparison="exact parent component and historical build ID",
                  authority_source="authenticated approved_parent/path strings")
        add_check("APPROVED_PARENT_EXACT_ROOT_BINDING", "parent_root_identity",
                  "approved parent and exact historical root basename bind as one path",
                  {"parent": str(build_root), "root": str(target_path), "basename": BUILD_ID},
                  {"parent": parent_path_expected, "root": root_path_expected,
                   "basename": Path(root_path_expected).name if type(root_path_expected) is str else MISSING},
                  (type(parent_path_expected) is str and type(root_path_expected) is str
                   and parent_path_expected == str(build_root) and root_path_expected == str(target_path)
                   and Path(root_path_expected).parent == Path(parent_path_expected)
                   and Path(root_path_expected).name == BUILD_ID),
                  value_type="bound parent/root path pair", comparison="exact parent, root path, and basename relationship",
                  authority_source="authenticated R23 path fields and historical build ID")
        add_typed("PINNED_ROOT_DEVICE", "parent_root_identity", "pinned root st_dev equals authenticated build_device",
                  expected_root_device, root_pin_stat.st_dev, int, authority)
        add_typed("PINNED_ROOT_INODE", "parent_root_identity", "pinned root st_ino equals authenticated build_inode",
                  expected_root_inode, root_pin_stat.st_ino, int, authority)
        add_check("PINNED_ROOT_DIRECTORY", "parent_root_identity", "pinned root is a directory",
                  True, stat.S_ISDIR(root_pin_stat.st_mode), stat.S_ISDIR(root_pin_stat.st_mode),
                  value_type="boolean", comparison="S_ISDIR(fstat(root_pin_fd).st_mode)", authority_source="descriptor-native fstat")
        add_typed("PINNED_ROOT_FSID", "parent_root_identity", "pinned root filesystem ID equals authenticated build_filesystem_id",
                  expected_root_fsid, root_fsid, int, authority)
        add_typed("NAMED_ROOT_DEVICE", "parent_root_identity", "named root st_dev equals authenticated build_device",
                  expected_root_device, named_root_stat.st_dev, int, authority)
        add_typed("NAMED_ROOT_INODE", "parent_root_identity", "named root st_ino equals authenticated build_inode",
                  expected_root_inode, named_root_stat.st_ino, int, authority)
        named_type = stat.S_IFMT(named_root_stat.st_mode)
        pin_type = stat.S_IFMT(root_pin_stat.st_mode)
        add_check("NAMED_ROOT_DIRECTORY", "parent_root_identity", "named root is a directory",
                  True, stat.S_ISDIR(named_root_stat.st_mode), stat.S_ISDIR(named_root_stat.st_mode),
                  value_type="boolean", comparison="S_ISDIR(no-follow named stat)", authority_source="no-follow stat relative to retained parent FD")
        add_check("NAMED_PIN_DEVICE_EQUALITY", "parent_root_identity", "named-root device equals pinned-root device",
                  root_pin_stat.st_dev, named_root_stat.st_dev, root_pin_stat.st_dev == named_root_stat.st_dev,
                  value_type="stat integer", comparison="exact equality", authority_source="no-follow named stat and pin fstat")
        add_check("NAMED_PIN_INODE_EQUALITY", "parent_root_identity", "named-root inode equals pinned-root inode",
                  root_pin_stat.st_ino, named_root_stat.st_ino, root_pin_stat.st_ino == named_root_stat.st_ino,
                  value_type="stat integer", comparison="exact equality", authority_source="no-follow named stat and pin fstat")
        add_check("NAMED_PIN_TYPE_EQUALITY", "parent_root_identity", "named-root type equals pinned-root type",
                  pin_type, named_type, named_type == pin_type,
                  value_type="stat file type", comparison="exact S_IFMT equality", authority_source="no-follow named stat and pin fstat")
        add_check("ROOT_NAMED_PIN_IDENTITY", "parent_root_identity", "named historical root equals pinned root in device/inode/type",
                  {"device": root_pin_stat.st_dev, "inode": root_pin_stat.st_ino, "type": pin_type},
                  {"device": named_root_stat.st_dev, "inode": named_root_stat.st_ino, "type": named_type},
                  (named_root_stat.st_dev, named_root_stat.st_ino, named_type)
                  == (root_pin_stat.st_dev, root_pin_stat.st_ino, pin_type),
                  value_type="three-field identity tuple", comparison="exact tuple equality",
                  authority_source="named child no-follow stat before pin and descriptor fstat")
        report["parent_capture"]["root_uid_mode_diagnostic_only"] = {
            "st_uid": root_pin_stat.st_uid,
            "permission_mode": stat.S_IMODE(root_pin_stat.st_mode),
            "authenticated_or_gating": False,
        }
        report["parent_capture"]["parent_gid_diagnostic_only"] = {
            "st_gid": parent_fd_stat.st_gid,
            "authenticated_or_gating": False,
        }

        # Re-read every authority/path used above while both authoritative locks remain held.
        final_parent_identity = identity_from_path(Path(parent_path_expected))
        final_named_root = os.stat(BUILD_ID, dir_fd=parent_fd, follow_symlinks=False)
        final_pin_identity, final_pin_stat, final_pin_fsid = identity_from_fd(root_fd)
        for field, expected_type in PARENT_FIELDS.items():
            add_typed(f"PARENT_PATH_REVALIDATION_{field.upper()}", "revalidation",
                      f"path-derived parent {field} remains unchanged after capture",
                      parent_path_id[field], final_parent_identity.get(field, MISSING), expected_type,
                      "fresh independent no-follow path open plus fstat/fstatvfs")
        add_typed("NAMED_ROOT_REVALIDATION_DEVICE", "revalidation", "named root st_dev remains unchanged",
                  named_root_stat.st_dev, final_named_root.st_dev, int,
                  "no-follow stat relative to retained parent descriptor")
        add_typed("NAMED_ROOT_REVALIDATION_INODE", "revalidation", "named root st_ino remains unchanged",
                  named_root_stat.st_ino, final_named_root.st_ino, int,
                  "no-follow stat relative to retained parent descriptor")
        add_typed("NAMED_ROOT_REVALIDATION_TYPE", "revalidation", "named root file type remains unchanged",
                  stat.S_IFMT(named_root_stat.st_mode), stat.S_IFMT(final_named_root.st_mode), int,
                  "no-follow stat relative to retained parent descriptor")
        add_typed("ROOT_PIN_REVALIDATION_DEVICE", "revalidation", "pinned root st_dev remains unchanged",
                  root_pin_stat.st_dev, final_pin_stat.st_dev, int, "descriptor-native fstat")
        add_typed("ROOT_PIN_REVALIDATION_INODE", "revalidation", "pinned root st_ino remains unchanged",
                  root_pin_stat.st_ino, final_pin_stat.st_ino, int, "descriptor-native fstat")
        add_typed("ROOT_PIN_REVALIDATION_TYPE", "revalidation", "pinned root file type remains unchanged",
                  pin_type, stat.S_IFMT(final_pin_stat.st_mode), int, "descriptor-native fstat")
        add_typed("ROOT_PIN_REVALIDATION_FSID", "revalidation", "pinned root filesystem ID remains unchanged",
                  root_fsid, final_pin_fsid, int, "descriptor-native fstatvfs")
        registry_after_raw = read_at(lease_fd, REGISTRY_NAME)
        marker_after_raw = read_at(root_fd, ".fgmetal-build-lease.json")
        admission_after_raw = read_path_nofollow(admission_path)
        plan_after_raw = read_path_nofollow(plan_path)
        controller_after_raw = read_path_nofollow(storage_dir / "build_retention.py")
        tests_after_raw = read_path_nofollow(storage_dir / "test_build_retention.py")
        add_check("REGISTRY_REVALIDATION", "revalidation", "authenticated registry bytes remain unchanged",
                  sha256(registry_raw), sha256(registry_after_raw), registry_after_raw == registry_raw,
                  value_type="SHA-256 and exact bytes", comparison="exact byte equality",
                  authority_source="retained lease-root descriptor")
        add_check("MARKER_REVALIDATION", "revalidation", "authenticated marker bytes remain unchanged",
                  sha256(marker_raw), sha256(marker_after_raw), marker_after_raw == marker_raw,
                  value_type="SHA-256 and exact bytes", comparison="exact byte equality",
                  authority_source="retained root-pin descriptor")
        signing_key_after_stat = os.fstat(signing_key_fd)
        signing_key_named_after = os.stat(SIGNING_KEY_NAME, dir_fd=cache_fd, follow_symlinks=False)
        os.lseek(signing_key_fd, 0, os.SEEK_SET)
        signing_key_after_buffer = bytearray()
        while len(signing_key_after_buffer) <= 32:
            block = os.read(signing_key_fd, 33 - len(signing_key_after_buffer))
            if not block:
                break
            signing_key_after_buffer.extend(block)
        add_typed("SIGNING_KEY_REVALIDATION_DEVICE", "revalidation", "manifest key descriptor device remains unchanged",
                  signing_key_before.st_dev, signing_key_after_stat.st_dev, int, "retained signing-key descriptor fstat")
        add_typed("SIGNING_KEY_REVALIDATION_INODE", "revalidation", "manifest key descriptor inode remains unchanged",
                  signing_key_before.st_ino, signing_key_after_stat.st_ino, int, "retained signing-key descriptor fstat")
        add_check("SIGNING_KEY_REVALIDATION_TYPE", "revalidation", "manifest signing key remains a regular file",
                  True, stat.S_ISREG(signing_key_after_stat.st_mode),
                  stat.S_ISREG(signing_key_after_stat.st_mode),
                  value_type="boolean", comparison="S_ISREG(retained fstat.st_mode)",
                  authority_source="retained signing-key descriptor fstat")
        add_typed("SIGNING_KEY_REVALIDATION_MODE", "revalidation", "manifest signing key mode remains 0600",
                  0o600, stat.S_IMODE(signing_key_after_stat.st_mode), int, "retained signing-key descriptor fstat")
        add_typed("SIGNING_KEY_REVALIDATION_NLINK", "revalidation", "manifest signing key link count remains one",
                  1, signing_key_after_stat.st_nlink, int, "retained signing-key descriptor fstat")
        add_typed("SIGNING_KEY_REVALIDATION_OWNER", "revalidation", "manifest signing key owner remains current UID",
                  os.getuid(), signing_key_after_stat.st_uid, int, "retained signing-key descriptor fstat")
        add_typed("SIGNING_KEY_NAME_FD_DEVICE_REVALIDATION", "revalidation", "manifest signing key name still matches descriptor device",
                  signing_key_after_stat.st_dev, signing_key_named_after.st_dev, int,
                  "retained key descriptor fstat and no-follow named stat")
        add_typed("SIGNING_KEY_NAME_FD_INODE_REVALIDATION", "revalidation", "manifest signing key name still matches descriptor inode",
                  signing_key_after_stat.st_ino, signing_key_named_after.st_ino, int,
                  "retained key descriptor fstat and no-follow named stat")
        add_check("SIGNING_KEY_BYTES_REVALIDATION", "revalidation", "manifest signing key bytes remain unchanged",
                  True, bytes(signing_key_after_buffer) == key and len(signing_key_after_buffer) == 32,
                  bytes(signing_key_after_buffer) == key and len(signing_key_after_buffer) == 32,
                  value_type="boolean", comparison="exact in-memory key equality and 32-byte length",
                  authority_source="retained no-follow signing-key descriptor")
        for row_id, name, before, after, source in (
            ("ADMISSION_REVALIDATION", "signed admission", admission_raw, admission_after_raw, admission_path),
            ("PLAN_REVALIDATION", "R23 execution plan", plan_raw, plan_after_raw, plan_path),
            ("R27_CONTROLLER_REVALIDATION", "accepted R27 controller source", controller_raw, controller_after_raw,
             storage_dir / "build_retention.py"),
            ("R27_TESTS_REVALIDATION", "accepted R27 tests source", tests_raw, tests_after_raw,
             storage_dir / "test_build_retention.py"),
        ):
            add_check(row_id, "revalidation", f"{name} bytes remain unchanged during the precheck",
                      sha256(before), sha256(after), before == after,
                      value_type="SHA-256 and exact bytes", comparison="exact byte equality",
                      authority_source=f"no-follow bounded read of {source}")

        named_lock_after = os.stat(ALLOCATION_LOCK, dir_fd=cache_fd, follow_symlinks=False)
        retention_lock_after = os.stat(RETENTION_LOCK, dir_fd=lease_fd, follow_symlinks=False)
        allocation_after_fd = os.fstat(allocation_fd)
        retention_after_fd = os.fstat(retention_fd)
        for prefix, captured_fd, current_fd, current_named in (
            ("ALLOC_LOCK", allocation_before, allocation_after_fd, named_lock_after),
            ("RETENTION_LOCK", retention_before, retention_after_fd, retention_lock_after),
        ):
            for field, captured_value, fd_value, named_value in (
                ("DEVICE", captured_fd.st_dev, current_fd.st_dev, current_named.st_dev),
                ("INODE", captured_fd.st_ino, current_fd.st_ino, current_named.st_ino),
            ):
                add_typed(f"{prefix}_FD_{field}_REVALIDATION", "revalidation",
                          f"{prefix.lower()} descriptor {field.lower()} remains unchanged",
                          captured_value, fd_value, int, "retained lock descriptor fstat")
                add_typed(f"{prefix}_NAME_FD_{field}_REVALIDATION", "revalidation",
                          f"{prefix.lower()} named {field.lower()} still equals descriptor",
                          fd_value, named_value, int, "retained descriptor fstat and no-follow named stat")
            add_check(f"{prefix}_TYPE_REVALIDATION", "revalidation",
                      f"{prefix.lower()} remains a regular file",
                      "regular file", stat.S_IFMT(current_fd.st_mode), stat.S_ISREG(current_fd.st_mode),
                      value_type="file type", comparison="S_ISREG(current fstat.st_mode)",
                      authority_source="retained lock descriptor fstat")
            add_typed(f"{prefix}_MODE_REVALIDATION", "revalidation",
                      f"{prefix.lower()} permission mode remains 0600", 0o600,
                      stat.S_IMODE(current_fd.st_mode), int, "retained lock descriptor fstat")
            add_typed(f"{prefix}_NLINK_REVALIDATION", "revalidation",
                      f"{prefix.lower()} link count remains one", 1, current_fd.st_nlink, int,
                      "retained lock descriptor fstat")
            add_typed(f"{prefix}_OWNER_REVALIDATION", "revalidation",
                      f"{prefix.lower()} remains owned by current UID", os.getuid(), current_fd.st_uid,
                      int, "retained lock descriptor fstat")

        actual_ids = {row["id"] for row in report["checks"] if row["phase"] == "parent_root_identity"}
        missing_ids = sorted(REQUIRED_IDENTITY_ROW_IDS - actual_ids)
        for missing_id in missing_ids:
            report["checks"].append({
                "id": missing_id,
                "phase": "parent_root_identity",
                "name": f"required identity predicate {missing_id} was not evaluated",
                "expected": "unavailable: blocked before this row",
                "observed": "not observed",
                "value_type": "not run",
                "comparison": "not run",
                "result": "NOT_RUN",
                "authority_source": "R32 corrected precheck procedure",
                "reason": "required matrix row missing before aggregate classification",
            })
        identity_pass = (not missing_ids and all(
            row["result"] == "PASS" for row in report["checks"]
            if row["phase"] in {"parent_root_identity", "revalidation"}
        ) and all(row["result"] == "PASS" for row in report["checks"] if row["phase"] in {"source", "locks", "authentication"}))
        report["classification"] = "PARENT_ROOT_IDENTITY_PASS" if identity_pass else "PARENT_ROOT_IDENTITY_FAIL"
        report["identity_row_count"] = len(actual_ids) + len(missing_ids)
        report["all_parent_root_rows_serialized"] = not missing_ids
        report["all_parent_root_rows_evaluated"] = (not missing_ids and all(
            row["result"] in {"PASS", "FAIL"} for row in report["checks"]
            if row["phase"] == "parent_root_identity"))
        report["missing_identity_row_ids"] = missing_ids
        report["registry_sha256"] = sha256(registry_raw)
        report["marker_sha256"] = sha256(marker_raw)
        report["admission_sha256"] = admission_hash
        report["plan_file_sha256"] = plan_file_hash
        report["plan_canonical_sha256"] = plan_canonical_hash
        report["root_pin_fd"] = root_fd
        report["root_pin_fstat"] = stat_record(root_pin_stat)
        report["root_pin_fstatvfs_fsid"] = serializable(root_fsid)
        report["f_getpath"] = {"attempted": False, "supported": False,
                                "note": "optional corroboration not attempted; descriptor fstat is authoritative"}
    except Exception as error:
        report["execution_error"] = {"type": type(error).__name__, "message": str(error)}
        if report["first_failure"] is None:
            report["first_failure"] = "PRECHECK_EXECUTION"
        report["checks"].append({
            "id": "PRECHECK_EXECUTION",
            "phase": "precheck",
            "name": "precheck completed without an uncaught execution error",
            "expected": "completed read-only precheck",
            "observed": {"type": type(error).__name__, "message": str(error)},
            "value_type": "execution status",
            "comparison": "no exception",
            "result": "FAIL",
            "authority_source": "R32 harness execution",
        })
        present_identity_ids = {row["id"] for row in report["checks"] if row["phase"] == "parent_root_identity"}
        for missing_id in sorted(REQUIRED_IDENTITY_ROW_IDS - present_identity_ids):
            report["checks"].append({
                "id": missing_id,
                "phase": "parent_root_identity",
                "name": f"required identity predicate {missing_id} was not evaluated",
                "expected": "unavailable: blocked before this row",
                "observed": "not observed",
                "value_type": "not run",
                "comparison": "not run",
                "result": "NOT_RUN",
                "authority_source": "R32 corrected precheck procedure",
                "reason": "precheck stopped before this matrix row",
            })
        report["classification"] = "PARENT_ROOT_IDENTITY_FAIL"
        report["identity_row_count"] = len(REQUIRED_IDENTITY_ROW_IDS)
        report["all_parent_root_rows_serialized"] = True
        report["all_parent_root_rows_evaluated"] = False
        report["missing_identity_row_ids"] = []
    finally:
        # Unlock and close all descriptors; no R23 path or record is written.
        for fd_name in ("root_fd", "parent_fd", "signing_key_fd", "retention_fd", "lease_fd", "allocation_fd", "cache_fd"):
            fd = locals().get(fd_name)
            if type(fd) is int:
                try:
                    if fd_name in {"allocation_fd", "retention_fd"}:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)
                except OSError:
                    pass
        try:
            script_bytes = read_path_nofollow(Path(__file__))
            report["script_sha256"] = sha256(script_bytes)
        except Exception as error:
            report["script_sha256_error"] = f"{type(error).__name__}: {error}"
        report["finished_at_unix"] = time.time()
        report["diagnostic_outputs"] = [OUTPUT_JSON_NAME, OUTPUT_MD_NAME]
        report["mutations"] = []
        report["lsof"] = {"launched": False}
        report["closeout"] = {"invoked": False}
        # If process capture failed, canonical paths may not yet exist; write through the script's fixed directory.
        try:
            script_path = Path(os.path.realpath(__file__))
            storage_dir = script_path.parent
            storage_fd = open_dir_nofollow(storage_dir)
            try:
                json_bytes = (json.dumps(serializable(report), indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
                write_new_file(storage_fd, OUTPUT_JSON_NAME, json_bytes)
                write_new_file(storage_fd, OUTPUT_MD_NAME, markdown_report(serializable(report)))
            finally:
                os.close(storage_fd)
        except Exception as output_error:
            print(f"diagnostic output failure: {type(output_error).__name__}: {output_error}", file=sys.stderr)
    print(report["classification"])
    if report["first_failure"]:
        print(f"first_failure={report['first_failure']}")
    return 0 if report["classification"] == "PARENT_ROOT_IDENTITY_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
