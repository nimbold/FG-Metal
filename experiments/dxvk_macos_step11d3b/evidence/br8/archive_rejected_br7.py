#!/usr/bin/env python3
"""Archive only the raw bulk artifacts from the two rejected BR7 attempts.

The default invocation is a read-only dry run.  Retirement requires the
explicit ``--execute`` switch.  The script is intentionally allowlist-based:
it never recursively adds or removes a run directory, and it never selects
canonical run JSON, manifests, hashes, receipts, reports, or source/runtime
artifacts.
"""

from __future__ import annotations

import argparse
import datetime as _datetime
import hashlib
import json
import os
import stat
import sys
import tarfile
import uuid
from pathlib import PurePosixPath
from typing import Any


EXPERIMENT_REL = "experiments/dxvk_macos_step11d3b"
RUNS_REL = f"{EXPERIMENT_REL}/runs"
EVIDENCE_REL = f"{EXPERIMENT_REL}/evidence/br8"
BR7_EVIDENCE_REL = f"{EXPERIMENT_REL}/evidence/br7"

REJECTED_TO_REPLACEMENT = {
    "step11d3b-br7-sample-03-20261010-075645-48e18549":
        "step11d3b-br7-sample-03-20261010-080324-f416269a",
    "step11d3b-br7-sample-05-20261010-081350-dbe1558c":
        "step11d3b-br7-sample-05-20261010-082920-99c26e88",
}

# These exact accepted runs are kept expanded.  The BR7 samples are also
# cross-checked against short-series.json before a plan or archive is made.
PROTECTED_ACCEPTED_BR6 = (
    "step11d3b-smoke-20261009-223106-0e5e3a6b",
    "step11d3b-smoke-20261010-053718-a78894d0",
    "step11d3b-baseline-01-20261009-223854-cfea0560",
    "step11d3b-baseline-02-20261010-054210-3a16e5aa",
    "step11d3b-baseline-03-20261010-062321-91bb8f4d",
)
PROTECTED_ACCEPTED_BR7_SMOKE_AND_BASELINE = (
    "step11d3b-smoke-20261010-074106-d99e7461",
    "step11d3b-baseline-02-20261010-084004-9ef35a0d",
)
PROTECTED_ACCEPTED_BR7_SAMPLES = (
    "step11d3b-br7-sample-01-20261010-074617-b3a0affe",
    "step11d3b-br7-sample-02-20261010-075131-ba511f38",
    "step11d3b-br7-sample-03-20261010-080324-f416269a",
    "step11d3b-br7-sample-04-20261010-080838-b42e94ae",
    "step11d3b-br7-sample-05-20261010-082920-99c26e88",
    "step11d3b-br7-sample-06-20261010-083433-e19389d4",
)
PROTECTED_CURRENT_BR8 = (
    "step11d3b-smoke-20261010-201913-d1f81d96",
    "step11d3b-br8-state-01-20261010-202408-c3f551ea",
)
PROTECTED_ACCEPTED_RUN_IDS = frozenset(
    PROTECTED_ACCEPTED_BR6
    + PROTECTED_ACCEPTED_BR7_SMOKE_AND_BASELINE
    + PROTECTED_ACCEPTED_BR7_SAMPLES
    + PROTECTED_CURRENT_BR8
)

REQUIRED_PRESERVED_ROOT_FILES = frozenset(
    {
        "execution-result.json",
        "prefix-cleanup.json",
        "prefix-registry-diff.txt.gz",
        "prefix-state.json",
        "process-context.json",
        "run-manifest.json",
        "run-result.json",
        "trace-index.json",
        "visual-observations.json",
    }
)
REQUIRED_PRESERVED_ROOT_DIRS = frozenset({"mvk", "trace-loader"})

CHUNK_SIZE = 1024 * 1024
DIR_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)
READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
CREATE_FLAGS = (
    os.O_RDWR
    | os.O_CREAT
    | os.O_EXCL
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_CLOEXEC", 0)
)


class GuardError(RuntimeError):
    """Raised when a safety or evidence precondition is not satisfied."""


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting archive evidence")
        view = view[written:]


def _identity(st: os.stat_result) -> dict[str, int]:
    return {
        "device": st.st_dev,
        "inode": st.st_ino,
        "mode": stat.S_IMODE(st.st_mode),
        "size": st.st_size,
        "mtime_ns": st.st_mtime_ns,
        "ctime_ns": st.st_ctime_ns,
    }


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return _identity(left) == _identity(right)


def _same_directory(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
        and stat.S_IFMT(left.st_mode) == stat.S_IFMT(right.st_mode)
        and stat.S_IMODE(left.st_mode) == stat.S_IMODE(right.st_mode)
    )


def _same_regular_object(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        stat.S_ISREG(left.st_mode)
        and stat.S_ISREG(right.st_mode)
        and left.st_dev == right.st_dev
        and left.st_ino == right.st_ino
    )


def _require_secure_posix() -> None:
    needed = ("O_DIRECTORY", "O_NOFOLLOW")
    missing = [name for name in needed if not getattr(os, name, 0)]
    if os.name != "posix" or missing:
        raise GuardError(f"secure dirfd/no-follow support unavailable: {missing or os.name}")
    if not hasattr(os, "pread"):
        raise GuardError("os.pread is required for offset-independent file verification")


def _open_absolute_directory(path: str) -> int:
    """Open every component without following symlinks and return its dirfd."""
    absolute = os.path.abspath(path)
    parts = PurePosixPath(absolute).parts
    if not parts or parts[0] != "/":
        raise GuardError(f"expected an absolute path: {path}")
    fd = os.open("/", DIR_FLAGS)
    try:
        for name in parts[1:]:
            if name in ("", ".", ".."):
                raise GuardError(f"unsafe path component in {path}")
            child = os.open(name, DIR_FLAGS, dir_fd=fd)
            st = os.fstat(child)
            if not stat.S_ISDIR(st.st_mode):
                os.close(child)
                raise GuardError(f"path component is not a directory: {name}")
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_dir_at(parent_fd: int, name: str) -> int:
    before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISDIR(before.st_mode):
        raise GuardError(f"expected a real directory, got another file type: {name}")
    child = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    after = os.fstat(child)
    if not _same_directory(before, after):
        os.close(child)
        raise GuardError(f"directory changed while opening: {name}")
    return child


def _open_dir_chain(root_fd: int, components: tuple[str, ...]) -> int:
    fd = os.dup(root_fd)
    try:
        for name in components:
            child = _open_dir_at(fd, name)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_regular_at(parent_fd: int, name: str, label: str) -> tuple[bytes, dict[str, int]]:
    before_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before_path.st_mode):
        raise GuardError(f"expected a regular file for {label}")
    fd = os.open(name, READ_FLAGS, dir_fd=parent_fd)
    try:
        before_fd = os.fstat(fd)
        if not stat.S_ISREG(before_fd.st_mode) or not _same_identity(before_path, before_fd):
            raise GuardError(f"file identity changed while opening {label}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, CHUNK_SIZE)
            if not chunk:
                break
            chunks.append(chunk)
        after_fd = os.fstat(fd)
        after_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(before_fd, after_fd) or not _same_identity(before_fd, after_path):
            raise GuardError(f"file changed while reading {label}")
        data = b"".join(chunks)
        if len(data) != before_fd.st_size:
            raise GuardError(f"short read for {label}")
        return data, _identity(before_fd)
    finally:
        os.close(fd)


def _read_json_at(parent_fd: int, name: str, label: str) -> tuple[Any, dict[str, Any]]:
    data, identity = _read_regular_at(parent_fd, name, label)
    try:
        parsed = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError(f"invalid JSON in {label}: {exc}") from exc
    return parsed, {"sha256": hashlib.sha256(data).hexdigest(), **identity}


def _sha256_fd(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, CHUNK_SIZE, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _capture_file(parent_fd: int, filename: str, run_id: str, parts: tuple[str, ...]) -> dict[str, Any]:
    before_path = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before_path.st_mode):
        raise GuardError(f"selected source is not a regular file: {run_id}/{'/'.join(parts)}")
    fd = os.open(filename, READ_FLAGS, dir_fd=parent_fd)
    try:
        before_fd = os.fstat(fd)
        if not stat.S_ISREG(before_fd.st_mode) or not _same_identity(before_path, before_fd):
            raise GuardError(f"selected source changed while opening: {run_id}/{'/'.join(parts)}")
        digest = _sha256_fd(fd)
        after_fd = os.fstat(fd)
        after_path = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(before_fd, after_fd) or not _same_identity(before_fd, after_path):
            raise GuardError(f"selected source changed while hashing: {run_id}/{'/'.join(parts)}")
        return {
            "run_id": run_id,
            "parts": parts,
            "filename": filename,
            "parent_fd": parent_fd,
            "source_relative_path": f"{RUNS_REL}/{run_id}/{'/'.join(parts)}",
            "member_name": f"{RUNS_REL}/{run_id}/{'/'.join(parts)}",
            "sha256": digest,
            "identity": _identity(before_fd),
            "fd": fd,
        }
    except BaseException:
        os.close(fd)
        raise


def _inventory_tree(dir_fd: int, prefix: tuple[str, ...] = ()) -> tuple[dict[tuple[str, ...], dict[str, int]], dict[tuple[str, ...], dict[str, int]]]:
    files: dict[tuple[str, ...], dict[str, int]] = {}
    directories: dict[tuple[str, ...], dict[str, int]] = {}
    for name in sorted(os.listdir(dir_fd)):
        if name in ("", ".", "..") or "/" in name or "\x00" in name:
            raise GuardError(f"unsafe directory entry: {name!r}")
        path_parts = prefix + (name,)
        st = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISDIR(st.st_mode):
            child_fd = _open_dir_at(dir_fd, name)
            try:
                directories[path_parts] = _identity(st)
                child_files, child_dirs = _inventory_tree(child_fd, path_parts)
                files.update(child_files)
                directories.update(child_dirs)
                after = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                opened = os.fstat(child_fd)
                if not _same_directory(st, after) or not _same_directory(st, opened):
                    raise GuardError(f"directory tree changed during inventory: {'/'.join(path_parts)}")
            finally:
                os.close(child_fd)
        elif stat.S_ISREG(st.st_mode):
            files[path_parts] = _identity(st)
        elif stat.S_ISLNK(st.st_mode):
            raise GuardError(f"symlink in candidate run tree: {'/'.join(path_parts)}")
        else:
            raise GuardError(f"unsupported file type in candidate run tree: {'/'.join(path_parts)}")
    return files, directories


def _select_raw_paths(run_id: str, files: dict[tuple[str, ...], dict[str, int]]) -> list[tuple[str, ...]]:
    selected: list[tuple[str, ...]] = []
    for parts in sorted(files):
        if parts == ("app-trace.jsonl",):
            selected.append(parts)
        elif parts in (("runtime.log",), ("process-runner.log",)):
            selected.append(parts)
        elif len(parts) == 1 and parts[0].endswith(".png"):
            selected.append(parts)
        elif len(parts) == 2 and parts[0] == "host-traces" and parts[1].startswith("host-trace.") and parts[1].endswith(".jsonl"):
            selected.append(parts)

    required = {
        ("app-trace.jsonl",),
        ("runtime.log",),
        ("process-runner.log",),
    }
    absent = sorted(required.difference(selected))
    host_traces = [p for p in selected if len(p) == 2 and p[0] == "host-traces"]
    screenshots = [p for p in selected if len(p) == 1 and p[0].endswith(".png")]
    if absent or not host_traces or not screenshots:
        raise GuardError(
            f"incomplete expected raw evidence in {run_id}: missing={absent}, "
            f"host_trace_count={len(host_traces)}, screenshot_count={len(screenshots)}"
        )
    return selected


def _validate_run_result(run_fd: int, run_id: str, expected: str) -> dict[str, Any]:
    result, file_record = _read_json_at(run_fd, "run-result.json", f"{run_id}/run-result.json")
    if not isinstance(result, dict) or result.get("experiment_id") != run_id:
        raise GuardError(f"run-result identity mismatch for {run_id}")
    if expected == "rejected":
        exception = result.get("exception")
        rejection = "dyld loader line 11178 has a malformed process prefix"
        if not (
            result.get("status") == "FAIL"
            and result.get("is_valid") is False
            and result.get("process_returncode") == 0
            and result.get("prefix_cleanup_pass") is True
            and isinstance(exception, str)
            and rejection in exception
            and result.get("gate0_pre", {}).get("gate0_status") == "PASS"
            and result.get("gate0_post", {}).get("gate0_status") == "PASS"
        ):
            raise GuardError(f"{run_id} no longer matches the recorded rejected-run evidence")
    elif expected == "accepted":
        if not (
            result.get("status") == "PASS"
            and result.get("is_valid") is True
            and result.get("process_returncode") == 0
            and result.get("prefix_cleanup_pass") is True
            and result.get("gate0_pre", {}).get("gate0_status") == "PASS"
            and result.get("gate0_post", {}).get("gate0_status") == "PASS"
        ):
            raise GuardError(f"protected accepted run does not currently record PASS: {run_id}")
    else:
        raise AssertionError(expected)
    return {
        "run_id": run_id,
        "status": result["status"],
        "is_valid": result["is_valid"],
        "process_returncode": result.get("process_returncode"),
        "prefix_cleanup_pass": result.get("prefix_cleanup_pass"),
        "gate0_pre": result.get("gate0_pre", {}).get("gate0_status"),
        "gate0_post": result.get("gate0_post", {}).get("gate0_status"),
        "exception": result.get("exception"),
        "run_result_file": file_record,
    }


def _open_run(runs_fd: int, run_id: str, protected: frozenset[str]) -> tuple[int, dict[str, Any]]:
    if run_id in protected:
        raise GuardError(f"refusing to treat protected accepted evidence as a candidate: {run_id}")
    if not run_id.startswith("step11d3b-br7-sample-") or "/" in run_id or run_id in (".", ".."):
        raise GuardError(f"unexpected target run identifier: {run_id}")
    run_fd = _open_dir_at(runs_fd, run_id)
    run_st = os.fstat(run_fd)
    return run_fd, {"run_id": run_id, "directory_identity": _identity(run_st)}


def _read_controls(repo_fd: int, runs_fd: int, evidence_fd: int) -> tuple[dict[str, Any], dict[str, Any]]:
    # Validate the six accepted short-series rows exactly; a replacement must
    # remain admitted and quantitative-ready at the time of execution.
    br7_fd = _open_dir_chain(repo_fd, tuple(BR7_EVIDENCE_REL.split("/")))
    try:
        short_series, short_series_file = _read_json_at(br7_fd, "short-series.json", f"{BR7_EVIDENCE_REL}/short-series.json")
    finally:
        os.close(br7_fd)
    if not isinstance(short_series, list):
        raise GuardError("BR7 short-series.json must be a list")
    rows_by_id: dict[str, dict[str, Any]] = {}
    for row in short_series:
        if not isinstance(row, dict) or not isinstance(row.get("run_id"), str):
            raise GuardError("malformed BR7 short-series row")
        if row["run_id"] in rows_by_id:
            raise GuardError(f"duplicate BR7 short-series run id: {row['run_id']}")
        rows_by_id[row["run_id"]] = row
    if set(rows_by_id) != set(PROTECTED_ACCEPTED_BR7_SAMPLES):
        raise GuardError("BR7 short-series accepted IDs differ from the explicit protected sample set")
    for run_id in PROTECTED_ACCEPTED_BR7_SAMPLES:
        row = rows_by_id[run_id]
        if not (
            row.get("status") == "PASS"
            and row.get("is_valid") is True
            and row.get("quantitative_ready") is True
            and row.get("returncode") == 0
            and row.get("prefix_cleanup_pass") is True
            and row.get("gate0_pre", {}).get("gate0_status") == "PASS"
            and row.get("gate0_post", {}).get("gate0_status") == "PASS"
        ):
            raise GuardError(f"BR7 accepted short-series row failed protection check: {run_id}")

    target_evidence: dict[str, Any] = {}
    target_fds: dict[str, int] = {}
    try:
        for run_id, replacement_id in REJECTED_TO_REPLACEMENT.items():
            run_fd, directory_record = _open_run(runs_fd, run_id, PROTECTED_ACCEPTED_RUN_IDS)
            target_fds[run_id] = run_fd
            result = _validate_run_result(run_fd, run_id, "rejected")
            target_evidence[run_id] = {
                **directory_record,
                "rejected_run_result": result,
                "replacement_run_id": replacement_id,
            }
        protected_evidence: list[dict[str, Any]] = []
        for run_id in sorted(PROTECTED_ACCEPTED_RUN_IDS):
            run_fd = _open_dir_at(runs_fd, run_id)
            try:
                result = _validate_run_result(run_fd, run_id, "accepted")
                protected_evidence.append(result)
            finally:
                os.close(run_fd)
        for rejected_id, replacement_id in REJECTED_TO_REPLACEMENT.items():
            row = rows_by_id.get(replacement_id)
            if row is None or not (
                row.get("status") == "PASS"
                and row.get("is_valid") is True
                and row.get("quantitative_ready") is True
                and row.get("returncode") == 0
                and row.get("prefix_cleanup_pass") is True
                and row.get("gate0_pre", {}).get("gate0_status") == "PASS"
                and row.get("gate0_post", {}).get("gate0_status") == "PASS"
            ):
                raise GuardError(f"replacement is not a passing quantitative BR7 sample: {replacement_id}")
            target_evidence[rejected_id]["replacement_short_series_row"] = {
                key: row.get(key)
                for key in (
                    "index", "run_id", "returncode", "status", "is_valid",
                    "quantitative_ready", "prefix_cleanup_pass",
                )
            }
        retention_plan, retention_plan_file = _read_regular_at(evidence_fd, "storage-retention-plan.md", f"{EVIDENCE_REL}/storage-retention-plan.md")
        plan_hash = hashlib.sha256(retention_plan).hexdigest()
        controls = {
            "short_series_file": short_series_file,
            "storage_retention_plan": {
                "path": f"{EVIDENCE_REL}/storage-retention-plan.md",
                "sha256": plan_hash,
                **retention_plan_file,
            },
            "protected_accepted_run_ids": sorted(PROTECTED_ACCEPTED_RUN_IDS),
            "protected_run_results": protected_evidence,
            "protection_sources": [
                f"{EXPERIMENT_REL}/STEP-11D3-BR6-REPORT.md",
                f"{EXPERIMENT_REL}/STEP-11D3-BR7-REPORT.md",
                f"{BR7_EVIDENCE_REL}/short-series.json",
                f"{EVIDENCE_REL}/storage-retention-plan.md",
            ],
        }
        return target_evidence, {"controls": controls, "target_fds": target_fds}
    except BaseException:
        for fd in target_fds.values():
            os.close(fd)
        raise


def _build_plan(repo_fd: int, runs_fd: int, evidence_fd: int) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    target_evidence, controls_and_fds = _read_controls(repo_fd, runs_fd, evidence_fd)
    controls = controls_and_fds["controls"]
    target_fds: dict[str, int] = controls_and_fds["target_fds"]
    targets: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    try:
        for run_id, replacement_id in REJECTED_TO_REPLACEMENT.items():
            run_fd = target_fds[run_id]
            files, directories = _inventory_tree(run_fd)
            reported_result = target_evidence[run_id]["rejected_run_result"]["run_result_file"]
            if files.get(("run-result.json",)) != {
                key: reported_result[key]
                for key in ("device", "inode", "mode", "size", "mtime_ns", "ctime_ns")
            }:
                raise GuardError(f"run-result changed between rejection validation and tree inventory: {run_id}")
            root_files = {parts[0] for parts in files if len(parts) == 1}
            root_dirs = {parts[0] for parts in directories if len(parts) == 1}
            missing_files = sorted(REQUIRED_PRESERVED_ROOT_FILES.difference(root_files))
            missing_dirs = sorted(REQUIRED_PRESERVED_ROOT_DIRS.difference(root_dirs))
            if missing_files or missing_dirs:
                raise GuardError(
                    f"canonical preserved evidence missing in {run_id}: "
                    f"files={missing_files}, directories={missing_dirs}"
                )
            selected_parts = _select_raw_paths(run_id, files)
            # Keep a stable inventory so the script can prove that the only
            # later tree changes are the explicitly selected per-file unlinks.
            targets.append(
                {
                    **target_evidence[run_id],
                    "replacement_run_id": replacement_id,
                    "initial_files": files,
                    "initial_directories": directories,
                    "selected_parts": selected_parts,
                    "preserved_relative_paths": [
                        "/".join(parts)
                        for parts in sorted(set(files) | set(directories))
                        if parts not in set(selected_parts)
                    ],
                }
            )
            if "host-traces" not in root_dirs:
                raise GuardError(f"missing host-traces directory in {run_id}")
            host_fd = _open_dir_at(run_fd, "host-traces")
            target_fds[f"{run_id}/host-traces"] = host_fd
            for parts in selected_parts:
                parent_fd = host_fd if len(parts) == 2 else run_fd
                selected.append(_capture_file(parent_fd, parts[-1], run_id, parts))
        if set(target_evidence) != set(REJECTED_TO_REPLACEMENT):
            raise GuardError("candidate set differs from the exact two rejected BR7 IDs")
        if len({record["source_relative_path"] for record in selected}) != len(selected):
            raise GuardError("duplicate selected path in archive plan")
        plan = {
            "controls": controls,
            "targets": targets,
            "selected_file_count": len(selected),
            "selected_logical_bytes": sum(record["identity"]["size"] for record in selected),
        }
        return plan, selected, target_fds
    except BaseException:
        for record in selected:
            try:
                os.close(record["fd"])
            except OSError:
                pass
        for fd in target_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        raise


def _verify_selected_record(record: dict[str, Any]) -> dict[str, int]:
    filename = record["filename"]
    parent_fd = record["parent_fd"]
    before_path = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before_path.st_mode):
        raise GuardError(f"selected path is not a regular file before retirement: {record['source_relative_path']}")
    fd = os.open(filename, READ_FLAGS, dir_fd=parent_fd)
    try:
        before_fd = os.fstat(fd)
        if not stat.S_ISREG(before_fd.st_mode) or not _same_identity(before_path, before_fd):
            raise GuardError(f"selected path identity changed before retirement: {record['source_relative_path']}")
        digest = _sha256_fd(fd)
        after_fd = os.fstat(fd)
        after_path = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
        identity = _identity(before_fd)
        if not _same_identity(before_fd, after_fd) or not _same_identity(before_fd, after_path):
            raise GuardError(f"selected path changed during pre-unlink verification: {record['source_relative_path']}")
        if identity != record["identity"] or digest != record["sha256"]:
            raise GuardError(f"selected path content or stat identity changed: {record['source_relative_path']}")
        return {"sha256": digest, **identity}
    finally:
        os.close(fd)


def _assert_live_target(runs_fd: int, target: dict[str, Any], target_fd: int) -> None:
    run_id = target["run_id"]
    current_fd = _open_dir_at(runs_fd, run_id)
    try:
        if not _same_directory(os.fstat(current_fd), os.fstat(target_fd)):
            raise GuardError(f"candidate run directory identity changed: {run_id}")
    finally:
        os.close(current_fd)


def _assert_tree_matches(target: dict[str, Any], run_fd: int, already_unlinked: set[tuple[str, ...]]) -> None:
    current_files, current_dirs = _inventory_tree(run_fd)
    original_files: dict[tuple[str, ...], dict[str, int]] = target["initial_files"]
    original_dirs: dict[tuple[str, ...], dict[str, int]] = target["initial_directories"]
    expected_files = {path: identity for path, identity in original_files.items() if path not in already_unlinked}
    if set(current_files) != set(expected_files):
        extra = sorted(set(current_files).difference(expected_files))
        missing = sorted(set(expected_files).difference(current_files))
        raise GuardError(f"candidate tree path set changed for {target['run_id']}: extra={extra}, missing={missing}")
    for path, expected in expected_files.items():
        if current_files[path] != expected:
            raise GuardError(f"unselected or not-yet-retired file changed: {target['run_id']}/{'/'.join(path)}")
    if set(current_dirs) != set(original_dirs):
        raise GuardError(f"candidate directory set changed for {target['run_id']}")
    for path, expected in original_dirs.items():
        current = current_dirs[path]
        # Directory mtime changes when selected files are unlinked, so compare
        # directory identity and type/mode only.  Every file still gets a full
        # identity comparison, and selected files get a fresh SHA-256 below.
        if (current["device"], current["inode"], current["mode"]) != (
            expected["device"], expected["inode"], expected["mode"]
        ):
            raise GuardError(f"candidate directory identity changed: {target['run_id']}/{'/'.join(path)}")


class _PreadReader:
    """A small read-only stream view that does not share or alter file offset."""

    def __init__(self, fd: int):
        self.fd = fd
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = CHUNK_SIZE
            chunks = []
            while True:
                data = os.pread(self.fd, size, self.offset)
                if not data:
                    break
                self.offset += len(data)
                chunks.append(data)
            return b"".join(chunks)
        data = os.pread(self.fd, size, self.offset)
        self.offset += len(data)
        return data


def _write_tar(archive_fd: int, selected: list[dict[str, Any]]) -> None:
    writer = os.fdopen(os.dup(archive_fd), "w+b")
    try:
        with tarfile.open(fileobj=writer, mode="w:gz", compresslevel=1, format=tarfile.PAX_FORMAT) as archive:
            for record in selected:
                current = _verify_selected_record(record)
                info = tarfile.TarInfo(record["member_name"])
                info.size = record["identity"]["size"]
                info.mode = record["identity"]["mode"]
                info.mtime = record["identity"]["mtime_ns"] // 1_000_000_000
                info.type = tarfile.REGTYPE
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                archive.addfile(info, _PreadReader(record["fd"]))
                if current["sha256"] != record["sha256"]:
                    raise GuardError(f"selected content changed while writing archive: {record['source_relative_path']}")
    finally:
        writer.close()
    os.fsync(archive_fd)


def _verify_archive(archive_fd: int, evidence_fd: int, archive_name: str, selected: list[dict[str, Any]]) -> dict[str, Any]:
    fd_stat = os.fstat(archive_fd)
    path_stat = os.stat(archive_name, dir_fd=evidence_fd, follow_symlinks=False)
    if not stat.S_ISREG(path_stat.st_mode) or not _same_identity(fd_stat, path_stat):
        raise GuardError("new archive path no longer identifies the archive descriptor")
    if fd_stat.st_size <= 0:
        raise GuardError("archive is empty")
    archive_hash = _sha256_fd(archive_fd)
    expected = {record["member_name"]: record for record in selected}
    verify_fd = os.open(archive_name, READ_FLAGS, dir_fd=evidence_fd)
    try:
        verify_stat = os.fstat(verify_fd)
        if not _same_identity(fd_stat, verify_stat):
            raise GuardError("archive changed between creation and verification")
        with os.fdopen(os.dup(verify_fd), "rb") as stream:
            with tarfile.open(fileobj=stream, mode="r:gz") as archive:
                members = archive.getmembers()
                names = [member.name for member in members]
                if len(names) != len(set(names)) or set(names) != set(expected):
                    raise GuardError("archive member set is not an exact match for the selected allowlist")
                for member in members:
                    if not member.isfile() or member.issym() or member.islnk() or member.size != expected[member.name]["identity"]["size"]:
                        raise GuardError(f"unsupported or size-mismatched archive member: {member.name}")
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or ".." in path.parts:
                        raise GuardError(f"unsafe tar member path: {member.name}")
                    source = archive.extractfile(member)
                    if source is None:
                        raise GuardError(f"cannot read archive member: {member.name}")
                    digest = hashlib.sha256()
                    size = 0
                    with source:
                        while True:
                            chunk = source.read(CHUNK_SIZE)
                            if not chunk:
                                break
                            size += len(chunk)
                            digest.update(chunk)
                    expected_record = expected[member.name]
                    if size != expected_record["identity"]["size"] or digest.hexdigest() != expected_record["sha256"]:
                        raise GuardError(f"archive member hash or size mismatch: {member.name}")
    finally:
        os.close(verify_fd)
    final_path_stat = os.stat(archive_name, dir_fd=evidence_fd, follow_symlinks=False)
    if not _same_identity(fd_stat, final_path_stat):
        raise GuardError("archive path changed during verification")
    return {"sha256": archive_hash, "size": fd_stat.st_size, "identity": _identity(fd_stat)}


def _make_output_pair(evidence_fd: int) -> tuple[str, str, int, int]:
    for _ in range(20):
        stamp = _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        suffix = uuid.uuid4().hex
        archive_name = f"br7-rejected-raw-evidence-{stamp}-{suffix}.tar.gz"
        receipt_name = f"br7-rejected-raw-evidence-{stamp}-{suffix}.receipt.jsonl"
        archive_fd: int | None = None
        receipt_fd: int | None = None
        try:
            archive_fd = os.open(archive_name, CREATE_FLAGS, 0o600, dir_fd=evidence_fd)
            receipt_fd = os.open(
                receipt_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
                | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                0o600,
                dir_fd=evidence_fd,
            )
            os.fsync(evidence_fd)
            return archive_name, receipt_name, archive_fd, receipt_fd
        except FileExistsError:
            for name, fd in ((receipt_name, receipt_fd), (archive_name, archive_fd)):
                if fd is None:
                    continue
                try:
                    expected = os.fstat(fd)
                    current = os.stat(name, dir_fd=evidence_fd, follow_symlinks=False)
                    if _same_regular_object(expected, current):
                        os.unlink(name, dir_fd=evidence_fd)
                except FileNotFoundError:
                    pass
                finally:
                    os.close(fd)
            os.fsync(evidence_fd)
            continue
        except BaseException:
            for name, fd in ((receipt_name, receipt_fd), (archive_name, archive_fd)):
                if fd is None:
                    continue
                try:
                    expected = os.fstat(fd)
                    current = os.stat(name, dir_fd=evidence_fd, follow_symlinks=False)
                    if _same_regular_object(expected, current):
                        os.unlink(name, dir_fd=evidence_fd)
                except OSError:
                    pass
                finally:
                    os.close(fd)
            try:
                os.fsync(evidence_fd)
            except OSError:
                pass
            raise
    raise GuardError("could not reserve unique archive and receipt names")


def _verify_receipt_path(receipt_fd: int, evidence_fd: int, receipt_name: str) -> None:
    path_stat = os.stat(receipt_name, dir_fd=evidence_fd, follow_symlinks=False)
    if not _same_regular_object(os.fstat(receipt_fd), path_stat):
        raise GuardError("receipt path no longer identifies the receipt descriptor")


def _verify_archive_path(archive_fd: int, evidence_fd: int, archive_name: str, expected: dict[str, Any]) -> None:
    fd_stat = os.fstat(archive_fd)
    path_stat = os.stat(archive_name, dir_fd=evidence_fd, follow_symlinks=False)
    if not _same_identity(fd_stat, path_stat) or _identity(path_stat) != expected["identity"]:
        raise GuardError("verified archive identity changed before source retirement")


def _receipt_event(receipt_fd: int, evidence_fd: int, receipt_name: str, event: dict[str, Any]) -> None:
    _verify_receipt_path(receipt_fd, evidence_fd, receipt_name)
    _write_all(receipt_fd, _json_bytes(event))
    os.fsync(receipt_fd)
    os.fsync(evidence_fd)


def _cleanup_owned_outputs(
    evidence_fd: int,
    archive_name: str,
    receipt_name: str,
    archive_fd: int,
    receipt_fd: int,
) -> list[str]:
    failures: list[str] = []
    for name, fd in ((receipt_name, receipt_fd), (archive_name, archive_fd)):
        try:
            expected = os.fstat(fd)
            current = os.stat(name, dir_fd=evidence_fd, follow_symlinks=False)
            if not stat.S_ISREG(current.st_mode) or not _same_identity(expected, current):
                failures.append(f"left changed output in place: {name}")
                continue
            os.unlink(name, dir_fd=evidence_fd)
            os.fsync(evidence_fd)
        except FileNotFoundError:
            pass
        except OSError as exc:
            failures.append(f"could not remove own incomplete output {name}: {exc}")
    return failures


def _selected_receipt_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_relative_path": record["source_relative_path"],
        "archive_member": record["member_name"],
        "sha256": record["sha256"],
        **record["identity"],
    }


def _restoration_instructions(archive_path: str, receipt_path: str) -> list[str]:
    return [
        "Keep the archive and receipt together; first confirm the archive SHA-256 recorded in the receipt.",
        "Extract the archive into a new empty scratch directory, never over the live repository tree.",
        "For every member, verify its SHA-256 and byte size against the receipt before restoring it.",
        "Restore each member to its recorded source_relative_path only if that destination is absent; create files exclusively, write all bytes, fsync each restored file, and fsync its parent directory. If a destination already exists, stop and compare it without overwriting.",
        f"Archive: {archive_path}",
        f"Receipt: {receipt_path}",
    ]


def _execute(
    repo_fd: int,
    runs_fd: int,
    evidence_fd: int,
    plan: dict[str, Any],
    selected: list[dict[str, Any]],
    target_fds: dict[str, int],
) -> tuple[str, str]:
    archive_name, receipt_name, archive_fd, receipt_fd = _make_output_pair(evidence_fd)
    archive_durable_receipt_written = False
    retirement_started = False
    already_unlinked: dict[str, set[tuple[str, ...]]] = {run_id: set() for run_id in REJECTED_TO_REPLACEMENT}
    try:
        os.fchmod(archive_fd, 0o600)
        _write_tar(archive_fd, selected)
        os.fchmod(archive_fd, 0o400)
        os.fsync(archive_fd)
        os.fsync(evidence_fd)
        archive_record = _verify_archive(archive_fd, evidence_fd, archive_name, selected)
        for record in selected:
            _verify_selected_record(record)

        archive_path = f"{EVIDENCE_REL}/{archive_name}"
        receipt_path = f"{EVIDENCE_REL}/{receipt_name}"
        pre_retirement = {
            "schema": "br8-br7-retention-receipt/v1",
            "event": "archive-verified-before-retirement",
            "created_at_utc": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
            "archive_relative_path": archive_path,
            "archive": archive_record,
            "receipt_relative_path": receipt_path,
            "target_runs": [
                {
                    "run_id": target["run_id"],
                    "replacement_run_id": target["replacement_run_id"],
                    "run_directory_identity": target["directory_identity"],
                    "rejected_run_result": target["rejected_run_result"],
                    "replacement_short_series_row": plan["targets_by_id"][target["run_id"]]["replacement_short_series_row"],
                    "selected_file_count": sum(1 for record in selected if record["run_id"] == target["run_id"]),
                    "preserved_relative_paths": target["preserved_relative_paths"],
                }
                for target in plan["targets"]
            ],
            "selected_files": [_selected_receipt_record(record) for record in selected],
            "protected_accepted_evidence": plan["controls"],
            "retirement_protocol": [
                "Archive members and SHA-256 values were verified before any source unlink.",
                "The complete archive and this receipt event are fsynced before any source unlink.",
                "Immediately before each unlink, the source is reopened without following symlinks and its SHA-256, size, inode, mtime_ns, and device are compared with this receipt.",
                "Each selected source is unlinked individually through its held parent dirfd; no directory is removed.",
                "The affected source parent directory is fsynced after each unlink, followed by a fsynced receipt event.",
            ],
            "restore_instructions": _restoration_instructions(archive_path, receipt_path),
        }
        _receipt_event(receipt_fd, evidence_fd, receipt_name, pre_retirement)
        archive_durable_receipt_written = True

        for target in plan["targets"]:
            run_id = target["run_id"]
            run_fd = target_fds[run_id]
            for record in [item for item in selected if item["run_id"] == run_id]:
                _assert_live_target(runs_fd, target, run_fd)
                _assert_tree_matches(target, run_fd, already_unlinked[run_id])
                _verify_archive_path(archive_fd, evidence_fd, archive_name, archive_record)
                verification = _verify_selected_record(record)
                _receipt_event(
                    receipt_fd,
                    evidence_fd,
                    receipt_name,
                    {
                        "schema": "br8-br7-retention-receipt/v1",
                        "event": "source-unlink-armed",
                        "created_at_utc": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
                        "source_relative_path": record["source_relative_path"],
                        "pre_unlink_verification": verification,
                    },
                )
                # Receipt fsync may take time. Revalidate both durable outputs
                # and the exact source immediately before the unlink syscall.
                _assert_live_target(runs_fd, target, run_fd)
                _assert_tree_matches(target, run_fd, already_unlinked[run_id])
                _verify_archive_path(archive_fd, evidence_fd, archive_name, archive_record)
                final_verification = _verify_selected_record(record)
                if final_verification != verification:
                    raise GuardError(f"selected source changed after arming its unlink: {record['source_relative_path']}")
                retirement_started = True
                os.unlink(record["filename"], dir_fd=record["parent_fd"])
                os.fsync(record["parent_fd"])
                try:
                    os.stat(record["filename"], dir_fd=record["parent_fd"], follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise GuardError(f"source name still exists after individual unlink: {record['source_relative_path']}")
                already_unlinked[run_id].add(record["parts"])
                _assert_tree_matches(target, run_fd, already_unlinked[run_id])
                _receipt_event(
                    receipt_fd,
                    evidence_fd,
                    receipt_name,
                    {
                        "schema": "br8-br7-retention-receipt/v1",
                        "event": "source-unlinked-and-parent-fsynced",
                        "created_at_utc": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
                        "source_relative_path": record["source_relative_path"],
                        "sha256": verification["sha256"],
                        "size": verification["size"],
                        "inode": verification["inode"],
                        "mtime_ns": verification["mtime_ns"],
                        "device": verification["device"],
                    },
                )

        for target in plan["targets"]:
            _assert_live_target(runs_fd, target, target_fds[target["run_id"]])
            _assert_tree_matches(target, target_fds[target["run_id"]], already_unlinked[target["run_id"]])
        _receipt_event(
            receipt_fd,
            evidence_fd,
            receipt_name,
            {
                "schema": "br8-br7-retention-receipt/v1",
                "event": "retirement-complete",
                "created_at_utc": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
                "archive_relative_path": archive_path,
                "archive_sha256": archive_record["sha256"],
                "removed_source_count": len(selected),
                "candidate_run_directories_preserved": True,
                "unselected_tree_paths_preserved": True,
                "protected_accepted_run_ids": sorted(PROTECTED_ACCEPTED_RUN_IDS),
            },
        )
        return archive_path, receipt_path
    except BaseException as exc:
        if not archive_durable_receipt_written and not retirement_started:
            cleanup_failures = _cleanup_owned_outputs(
                evidence_fd, archive_name, receipt_name, archive_fd, receipt_fd
            )
            detail = f"; cleanup: {'; '.join(cleanup_failures)}" if cleanup_failures else ""
            raise GuardError(f"execution stopped before source retirement: {exc}{detail}") from exc
        raise GuardError(
            f"execution stopped after durable archive/receipt; inspect the receipt before any retry: {exc}"
        ) from exc
    finally:
        os.close(receipt_fd)
        os.close(archive_fd)


def _print_dry_run(plan: dict[str, Any], selected: list[dict[str, Any]], evidence_fd: int) -> None:
    stamp = _datetime.datetime.now(_datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = uuid.uuid4().hex
    archive_name = f"br7-rejected-raw-evidence-{stamp}-{suffix}.tar.gz"
    receipt_name = f"br7-rejected-raw-evidence-{stamp}-{suffix}.receipt.jsonl"
    for name in (archive_name, receipt_name):
        try:
            os.stat(name, dir_fd=evidence_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise GuardError(f"dry-run output name unexpectedly exists: {name}")
    print("MODE: DRY RUN (no archive or receipt created; no source files unlinked)")
    print(f"Targets: {', '.join(REJECTED_TO_REPLACEMENT)}")
    print(f"Protected accepted runs: {len(PROTECTED_ACCEPTED_RUN_IDS)}")
    print(f"Selected files: {plan['selected_file_count']} ({plan['selected_logical_bytes']} bytes)")
    print(f"Planned archive: {EVIDENCE_REL}/{archive_name}")
    print(f"Planned receipt: {EVIDENCE_REL}/{receipt_name}")
    for record in selected:
        identity = record["identity"]
        print(
            f"  {record['source_relative_path']} size={identity['size']} "
            f"sha256={record['sha256']} dev={identity['device']} "
            f"ino={identity['inode']} mtime_ns={identity['mtime_ns']}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="create the verified archive and individually retire only the allowlisted raw files",
    )
    args = parser.parse_args(argv)

    root_fd = evidence_fd = runs_fd = None
    plan: dict[str, Any] | None = None
    selected: list[dict[str, Any]] = []
    target_fds: dict[str, int] = {}
    try:
        _require_secure_posix()
        script_path = os.path.abspath(__file__)
        evidence_path = os.path.dirname(script_path)
        experiment_path = os.path.dirname(os.path.dirname(evidence_path))
        repo_path = os.path.dirname(os.path.dirname(experiment_path))
        expected_script = os.path.join(repo_path, EVIDENCE_REL, "archive_rejected_br7.py")
        if os.path.normpath(script_path) != os.path.normpath(expected_script):
            raise GuardError(f"script is not at its fixed repository location: {script_path}")
        root_fd = _open_absolute_directory(repo_path)
        evidence_fd = _open_dir_chain(root_fd, tuple(EVIDENCE_REL.split("/")))
        runs_fd = _open_dir_chain(root_fd, tuple(RUNS_REL.split("/")))
        plan, selected, target_fds = _build_plan(root_fd, runs_fd, evidence_fd)
        plan["targets_by_id"] = {target["run_id"]: target for target in plan["targets"]}
        if args.execute:
            archive_path, receipt_path = _execute(root_fd, runs_fd, evidence_fd, plan, selected, target_fds)
            print("RETIREMENT COMPLETE")
            print(f"Archive: {archive_path}")
            print(f"Receipt: {receipt_path}")
        else:
            _print_dry_run(plan, selected, evidence_fd)
        return 0
    except (GuardError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        for record in selected:
            try:
                os.close(record["fd"])
            except OSError:
                pass
        for fd in target_fds.values():
            try:
                os.close(fd)
            except OSError:
                pass
        for fd in (runs_fd, evidence_fd, root_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
