#!/usr/bin/env python3
"""Safely archive only exact superseded BR8 raw traces.

Default mode is a read-only dry run for the two incomplete state-02 attempts.
Retirement requires both ``--execute`` and ``--confirm-quiescent`` after a
quiescence review.  The separate partial-coverage candidate is selected only
with ``--partial-d`` (and still requires both flags to retire anything).  Every
JSONL receipt line is HMAC-signed with the existing storage evidence key.  The
script never removes a run directory or selects JSON, manifests, binaries,
reports, receipts, or any file outside its exact path and size allowlist.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
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
CHUNK = 1024 * 1024
DIR_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
READ_FLAGS = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
CREATE_FLAGS = os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
ZERO_HASH = "0" * 64


FOCUS_CANDIDATES = (
    {
        "run_id": "step11d3b-br8-state-02-20261010-205140-6eeb0bbd",
        "replacement_id": "step11d3b-br8-state-02-20261011-005208-590fa5da",
        "raw_files": {
            ("app-trace.jsonl",): 30_472_876,
            ("process-runner.log",): 528,
            ("runtime.log",): 1_168_289,
            ("host-traces", "host-trace.2627.jsonl"): 55_868_424,
            ("host-traces", "host-trace.2645.jsonl"): 1_823,
        },
        "result_sha256": "be86cc63d31acaf31d5e3ff10b99bdac949265bb28e6bc9f3b49db1b88782b3d",
        "assessment": "state-02-independent-assessment.md",
        "assessment_sha256": "69969bde386bf87b31a5fa73452f6ed6154657cb2f89d557027ccbabffaaa1cd",
        "proofs": ["The planned restoration was **not attained in the retained trace**."],
    },
    {
        "run_id": "step11d3b-br8-state-02-20261010-205756-a9b95137",
        "replacement_id": "step11d3b-br8-state-02-20261011-005208-590fa5da",
        "raw_files": {
            ("app-trace.jsonl",): 30_772_498,
            ("process-runner.log",): 527,
            ("runtime.log",): 1_161_948,
            ("host-traces", "host-trace.3197.jsonl"): 56_412_537,
            ("host-traces", "host-trace.3215.jsonl"): 1_823,
        },
        "result_sha256": "964f38fd53bfd885e4a3469c86adfc32b03ffcae9105bdcb07f60da8330e0c64",
        "assessment": "inactive-raise-repeat-independent-assessment.md",
        "assessment_sha256": "e55fc3eb7fb6a96b82ea6615293e1d397b249cd35a22d14f8e9d9badbdf45c24",
        "proofs": ["There are no target state notification transitions."],
    },
)

PARTIAL_D_CANDIDATE = {
    "run_id": "step11d3b-br8-state-04-20261010-210352-7c833128",
    "replacement_id": "step11d3b-br8-state-04-20261011-010352-740f768d",
    "raw_files": {
        ("app-trace.jsonl",): 31_294_618,
        ("process-runner.log",): 528,
        ("runtime.log",): 1_168_289,
        ("host-traces", "host-trace.3675.jsonl"): 57_368_535,
        ("host-traces", "host-trace.3693.jsonl"): 1_823,
    },
    "result_sha256": "5b7c52a3fad60b9bc624af508d7778e21e7c09f10063d749463f268bab33b1da",
    "assessment": "partial-coverage-independent-assessment.md",
    "assessment_sha256": "3bb83842a434365680747407aba64796f95ef04854fcea1edfd972199edc9000",
    "proofs": [
        "is **not a valid partial-coverage control**.",
        "This is a short severe transient in this run",
    ],
}

REPLACEMENT_RESULT_SHA256 = {
    "step11d3b-br8-state-02-20261011-005208-590fa5da": "e99189ffaec382e4be525f42de07225cd6d9b87bcff7c81dc2cee37ab4f2d247",
    "step11d3b-br8-state-04-20261011-010352-740f768d": "045d54fd5b6a9f167182c1ba4c4d937bace6cb3395ab3516e1d86244756a83b9",
}

# Explicit protection records aid review.  Selection itself is stricter: only
# the candidate IDs above can ever be opened for retirement, so later BR8
# repeats, current E/F/H/300 attempts, BR6/BR7 evidence, and state-01 remain
# outside the script's reach even if their IDs are not listed here.
PROTECTED_RUN_IDS = frozenset(
    {
        "step11d3b-smoke-20261009-223106-0e5e3a6b",
        "step11d3b-smoke-20261010-053718-a78894d0",
        "step11d3b-baseline-01-20261009-223854-cfea0560",
        "step11d3b-baseline-02-20261010-054210-3a16e5aa",
        "step11d3b-baseline-03-20261010-062321-91bb8f4d",
        "step11d3b-smoke-20261010-074106-d99e7461",
        "step11d3b-baseline-02-20261010-084004-9ef35a0d",
        "step11d3b-br7-sample-01-20261010-074617-b3a0affe",
        "step11d3b-br7-sample-02-20261010-075131-ba511f38",
        "step11d3b-br7-sample-03-20261010-080324-f416269a",
        "step11d3b-br7-sample-04-20261010-080838-b42e94ae",
        "step11d3b-br7-sample-05-20261010-082920-99c26e88",
        "step11d3b-br7-sample-06-20261010-083433-e19389d4",
        "step11d3b-smoke-20261010-201913-d1f81d96",
        "step11d3b-br8-state-01-20261010-202408-c3f551ea",
        "step11d3b-br8-state-01-20261010-204414-870ded2e",
        "step11d3b-br8-state-02-20261011-005208-590fa5da",
        "step11d3b-br8-state-03-20261011-005826-94c63186",
        "step11d3b-br8-state-04-20261011-010352-740f768d",
        "step11d3b-br8-state-05-20261011-010918-133301ef",
        "step11d3b-br8-state-05-20261011-011503-5a4e8f4e",
        "step11d3b-br8-state-05-20261011-012109-3c660a30",
    }
)

OUTPUTS = {
    "focus": (
        "br8-superseded-focus-raw-v1.tar.gz",
        "br8-superseded-focus-raw-v1.receipt.jsonl",
    ),
    "partial-d": (
        "br8-superseded-partial-d-raw-v1.tar.gz",
        "br8-superseded-partial-d-raw-v1.receipt.jsonl",
    ),
}


class GuardError(RuntimeError):
    pass


def _json_line(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while persisting signed receipt")
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


def _same_identity(a: os.stat_result, b: os.stat_result) -> bool:
    return _identity(a) == _identity(b)


def _same_directory(a: os.stat_result, b: os.stat_result) -> bool:
    return stat.S_ISDIR(a.st_mode) and stat.S_ISDIR(b.st_mode) and (a.st_dev, a.st_ino, stat.S_IMODE(a.st_mode)) == (b.st_dev, b.st_ino, stat.S_IMODE(b.st_mode))


def _matches_recorded_directory(expected: Any, actual: os.stat_result, label: str) -> bool:
    if not isinstance(expected, dict):
        raise GuardError(f"receipt has no recorded directory identity: {label}")
    if not stat.S_ISDIR(actual.st_mode) or any(
        type(expected.get(key)) is not int
        or expected[key] != value
        for key, value in (
            ("device", actual.st_dev),
            ("inode", actual.st_ino),
            ("mode", stat.S_IMODE(actual.st_mode)),
        )
    ):
        return False
    return True


def _same_regular(a: os.stat_result, b: os.stat_result) -> bool:
    return stat.S_ISREG(a.st_mode) and stat.S_ISREG(b.st_mode) and (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def _require_posix() -> None:
    if os.name != "posix" or not getattr(os, "O_DIRECTORY", 0) or not getattr(os, "O_NOFOLLOW", 0) or not hasattr(os, "pread"):
        raise GuardError("secure POSIX dirfd/no-follow/pread support is required")


def _open_absolute_directory(path: str) -> int:
    absolute = os.path.abspath(path)
    parts = PurePosixPath(absolute).parts
    if not parts or parts[0] != "/":
        raise GuardError(f"expected absolute directory path: {path}")
    fd = os.open("/", DIR_FLAGS)
    try:
        for name in parts[1:]:
            if name in ("", ".", ".."):
                raise GuardError(f"unsafe path component: {name!r}")
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
        raise GuardError(f"expected a real directory: {name}")
    fd = os.open(name, DIR_FLAGS, dir_fd=parent_fd)
    if not _same_directory(before, os.fstat(fd)):
        os.close(fd)
        raise GuardError(f"directory changed while opening: {name}")
    return fd


def _open_chain(root_fd: int, components: tuple[str, ...]) -> int:
    fd = os.dup(root_fd)
    try:
        for component in components:
            child = _open_dir_at(fd, component)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _read_regular(parent_fd: int, name: str, label: str) -> tuple[bytes, dict[str, int]]:
    before_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISREG(before_path.st_mode):
        raise GuardError(f"expected regular file: {label}")
    fd = os.open(name, READ_FLAGS, dir_fd=parent_fd)
    try:
        before_fd = os.fstat(fd)
        if not _same_identity(before_path, before_fd):
            raise GuardError(f"file changed while opening: {label}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        after_fd = os.fstat(fd)
        after_path = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if not _same_identity(before_fd, after_fd) or not _same_identity(before_fd, after_path):
            raise GuardError(f"file changed while reading: {label}")
        data = b"".join(chunks)
        if len(data) != before_fd.st_size:
            raise GuardError(f"short read: {label}")
        return data, _identity(before_fd)
    finally:
        os.close(fd)


def _read_json(parent_fd: int, name: str, label: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    raw, identity = _read_regular(parent_fd, name, label)
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GuardError(f"invalid JSON: {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise GuardError(f"expected JSON object: {label}")
    return value, identity, hashlib.sha256(raw).hexdigest()


def _hash_fd(fd: int) -> str:
    digest = hashlib.sha256()
    offset = 0
    while True:
        chunk = os.pread(fd, CHUNK, offset)
        if not chunk:
            break
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _inventory_tree(dir_fd: int, prefix: tuple[str, ...] = ()) -> tuple[dict[tuple[str, ...], dict[str, int]], dict[tuple[str, ...], dict[str, int]]]:
    files: dict[tuple[str, ...], dict[str, int]] = {}
    dirs: dict[tuple[str, ...], dict[str, int]] = {}
    for name in sorted(os.listdir(dir_fd)):
        if name in ("", ".", "..") or "/" in name or "\x00" in name:
            raise GuardError(f"unsafe directory entry: {name!r}")
        parts = prefix + (name,)
        before = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        if stat.S_ISDIR(before.st_mode):
            child = _open_dir_at(dir_fd, name)
            try:
                dirs[parts] = _identity(before)
                nested_files, nested_dirs = _inventory_tree(child, parts)
                files.update(nested_files)
                dirs.update(nested_dirs)
                after = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
                if not _same_directory(before, after) or not _same_directory(before, os.fstat(child)):
                    raise GuardError(f"directory changed during inventory: {'/'.join(parts)}")
            finally:
                os.close(child)
        elif stat.S_ISREG(before.st_mode):
            files[parts] = _identity(before)
        elif stat.S_ISLNK(before.st_mode):
            raise GuardError(f"symlink in candidate tree: {'/'.join(parts)}")
        else:
            raise GuardError(f"unsupported file type in candidate tree: {'/'.join(parts)}")
    return files, dirs


def _candidate_evidence(evidence_fd: int, runs_fd: int, candidate: dict[str, Any]) -> tuple[dict[str, Any], int, int, dict[str, Any]]:
    run_id = candidate["run_id"]
    if run_id in PROTECTED_RUN_IDS or run_id not in {c["run_id"] for c in FOCUS_CANDIDATES} | {PARTIAL_D_CANDIDATE["run_id"]}:
        raise GuardError(f"candidate is not in the exact superseded allowlist: {run_id}")
    run_fd = _open_dir_at(runs_fd, run_id)
    host_fd = -1
    try:
        result, result_identity, result_hash = _read_json(run_fd, "run-result.json", f"{run_id}/run-result.json")
        if result_hash != candidate["result_sha256"]:
            raise GuardError(f"saved result changed from reviewed evidence: {run_id}")
        if not (
            result.get("experiment_id") == run_id
            and result.get("status") == "PASS"
            and result.get("is_valid") is True
            and result.get("process_returncode") == 0
            and result.get("prefix_cleanup_pass") is True
            and result.get("gate0_pre", {}).get("gate0_status") == "PASS"
            and result.get("gate0_post", {}).get("gate0_status") == "PASS"
        ):
            raise GuardError(f"saved candidate run-result is not the reviewed PASS/cleanup/Gate case: {run_id}")
        execution, execution_identity, execution_hash = _read_json(run_fd, "execution-result.json", f"{run_id}/execution-result.json")
        if execution.get("process_returncode") != 0 or execution.get("timed_out") is not False:
            raise GuardError(f"execution result is incomplete or timed out: {run_id}")
        replacement_fd = _open_dir_at(runs_fd, candidate["replacement_id"])
        try:
            replacement, replacement_identity, replacement_hash = _read_json(
                replacement_fd, "run-result.json", f"{candidate['replacement_id']}/run-result.json"
            )
        finally:
            os.close(replacement_fd)
        if replacement_hash != REPLACEMENT_RESULT_SHA256[candidate["replacement_id"]] or not (
            replacement.get("experiment_id") == candidate["replacement_id"]
            and replacement.get("status") == "PASS"
            and replacement.get("is_valid") is True
            and replacement.get("process_returncode") == 0
            and replacement.get("prefix_cleanup_pass") is True
            and replacement.get("gate0_pre", {}).get("gate0_status") == "PASS"
            and replacement.get("gate0_post", {}).get("gate0_status") == "PASS"
        ):
            raise GuardError(f"attained accepted replacement no longer verifies: {candidate['replacement_id']}")
        assessment, assessment_identity = _read_regular(
            evidence_fd, candidate["assessment"], f"{EVIDENCE_REL}/{candidate['assessment']}"
        )
        assessment_hash = hashlib.sha256(assessment).hexdigest()
        if assessment_hash != candidate["assessment_sha256"]:
            raise GuardError(f"independent assessment changed from reviewed evidence: {candidate['assessment']}")
        if any(proof.encode("utf-8") not in assessment for proof in candidate["proofs"]):
            raise GuardError(f"independent assessment no longer contains the documented limitation: {candidate['assessment']}")

        host_fd = _open_dir_at(run_fd, "host-traces")
        files, dirs = _inventory_tree(run_fd)
        expected = candidate["raw_files"]
        if set(path for path in files if path in expected) != set(expected):
            raise GuardError(f"exact expected raw file set is incomplete: {run_id}")
        for parts, size in expected.items():
            if files[parts]["size"] != size:
                raise GuardError(f"raw file size changed for {run_id}/{'/'.join(parts)}")
        # Reject newly introduced raw evidence with the names this retirement
        # scope owns.  All unrelated files remain in the tree and are sealed
        # into the pre-retirement identity snapshot below.
        raw_candidates = {
            parts for parts in files
            if parts == ("app-trace.jsonl",)
            or parts in (("runtime.log",), ("process-runner.log",))
            or (len(parts) == 2 and parts[0] == "host-traces" and parts[1].startswith("host-trace.") and parts[1].endswith(".jsonl"))
        }
        if raw_candidates != set(expected):
            raise GuardError(f"unreviewed raw evidence appeared in {run_id}: {sorted(raw_candidates ^ set(expected))}")
        required_files = {("run-result.json",), ("run-manifest.json",), ("execution-result.json",), ("prefix-cleanup.json",), ("trace-index.json",)}
        required_dirs = {("mvk",), ("trace-loader",), ("host-traces",)}
        if not required_files.issubset(files) or not required_dirs.issubset(dirs):
            raise GuardError(f"canonical JSON or source/runtime directories are missing: {run_id}")
        evidence = {
            "run_id": run_id,
            "replacement_run_id": candidate["replacement_id"],
            "reviewed_retention_basis": candidate["proofs"],
            "candidate_result": {"sha256": result_hash, **result_identity},
            "execution_result": {"sha256": execution_hash, **execution_identity},
            "replacement_result": {"sha256": replacement_hash, **replacement_identity},
            "assessment": {"path": f"{EVIDENCE_REL}/{candidate['assessment']}", "sha256": assessment_hash, **assessment_identity},
            "run_directory_identity": _identity(os.fstat(run_fd)),
            "host_traces_directory_identity": _identity(os.fstat(host_fd)),
            "files": files,
            "directories": dirs,
            "selected_paths": expected,
        }
        return evidence, run_fd, host_fd, {"candidate": candidate, "run_result": result}
    except BaseException:
        if host_fd >= 0:
            os.close(host_fd)
        os.close(run_fd)
        raise


def _verify_selected(record: dict[str, Any]) -> dict[str, Any]:
    name, parent = record["filename"], record["parent_fd"]
    before_path = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before_path.st_mode):
        raise GuardError(f"selected path is no longer a regular file: {record['source_relative_path']}")
    fd = os.open(name, READ_FLAGS, dir_fd=parent)
    try:
        before_fd = os.fstat(fd)
        digest = _hash_fd(fd)
        after_fd = os.fstat(fd)
        after_path = os.stat(name, dir_fd=parent, follow_symlinks=False)
        if not _same_identity(before_path, before_fd) or not _same_identity(before_fd, after_fd) or not _same_identity(before_fd, after_path):
            raise GuardError(f"selected file identity changed: {record['source_relative_path']}")
        if _identity(before_fd) != record["identity"] or digest != record["sha256"]:
            raise GuardError(f"selected file bytes or identity changed: {record['source_relative_path']}")
        return {"sha256": digest, **_identity(before_fd)}
    finally:
        os.close(fd)


class _PreadReader:
    def __init__(self, fd: int):
        self.fd = fd
        self.offset = 0

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            size = CHUNK
            blocks = []
            while True:
                data = os.pread(self.fd, size, self.offset)
                if not data:
                    break
                self.offset += len(data)
                blocks.append(data)
            return b"".join(blocks)
        data = os.pread(self.fd, size, self.offset)
        self.offset += len(data)
        return data


def _write_archive(archive_fd: int, selected: list[dict[str, Any]]) -> None:
    with os.fdopen(os.dup(archive_fd), "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=1, mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as archive:
                for record in sorted(selected, key=lambda item: item["archive_member"]):
                    _verify_selected(record)
                    identity = record["identity"]
                    info = tarfile.TarInfo(record["archive_member"])
                    info.size = identity["size"]
                    info.mode = identity["mode"]
                    info.mtime = identity["mtime_ns"] // 1_000_000_000
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.type = tarfile.REGTYPE
                    archive.addfile(info, _PreadReader(record["fd"]))
    os.fsync(archive_fd)


def _verify_archive(evidence_fd: int, archive_name: str, selected: list[dict[str, Any]]) -> dict[str, Any]:
    fd = os.open(archive_name, READ_FLAGS, dir_fd=evidence_fd)
    try:
        before = os.fstat(fd)
        path_before = os.stat(archive_name, dir_fd=evidence_fd, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode) or not _same_identity(before, path_before) or before.st_size <= 0:
            raise GuardError("archive is missing, empty, or changed")
        archive_hash = _hash_fd(fd)
        expected = {r["archive_member"]: r for r in selected}
        # Hash and parse through the same open file description.  Reopening the
        # pathname here would let a concurrent replacement feed different tar
        # bytes to the parser than the bytes covered by archive_hash.
        os.lseek(fd, 0, os.SEEK_SET)
        with os.fdopen(os.dup(fd), "rb") as stream:
            with tarfile.open(fileobj=stream, mode="r:gz") as archive:
                members = archive.getmembers()
                names = [m.name for m in members]
                if len(names) != len(set(names)) or set(names) != set(expected):
                    raise GuardError("archive members do not exactly equal the raw-file allowlist")
                for member in members:
                    rel = PurePosixPath(member.name)
                    expected_record = expected[member.name]
                    if rel.is_absolute() or ".." in rel.parts or not member.isfile() or member.size != expected_record["identity"]["size"]:
                        raise GuardError(f"unsafe or mismatched archive member: {member.name}")
                    source = archive.extractfile(member)
                    if source is None:
                        raise GuardError(f"cannot read archive member: {member.name}")
                    digest = hashlib.sha256()
                    total = 0
                    with source:
                        while True:
                            chunk = source.read(CHUNK)
                            if not chunk:
                                break
                            total += len(chunk)
                            digest.update(chunk)
                    if total != expected_record["identity"]["size"] or digest.hexdigest() != expected_record["sha256"]:
                        raise GuardError(f"archive content hash mismatch: {member.name}")
        after = os.fstat(fd)
        path_after = os.stat(archive_name, dir_fd=evidence_fd, follow_symlinks=False)
        if not _same_identity(before, after) or not _same_identity(before, path_after):
            raise GuardError("archive changed during verification")
        return {"sha256": archive_hash, "size": before.st_size, "identity": _identity(before)}
    finally:
        os.close(fd)


def _load_signer(repo_path: str):
    module_path = os.path.join(repo_path, "experiments", "storage", "storage_policy.py")
    spec = importlib.util.spec_from_file_location("_br8_retention_storage_policy", module_path)
    if spec is None or spec.loader is None:
        raise GuardError("cannot load the established storage-evidence signer")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_build_retention(repo_path: str):
    module_path = os.path.join(repo_path, "experiments", "storage", "build_retention.py")
    spec = importlib.util.spec_from_file_location("_br8_retention_build_retention", module_path)
    if spec is None or spec.loader is None:
        raise GuardError("cannot load the established read-only build-lease helpers")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _assert_no_active_retention(repo_path: str) -> dict[str, Any]:
    """Read active PrefixLease and build lease records without inventory/reconciliation."""
    policy = _load_signer(repo_path)
    prefix_markers = policy._active_lease_markers()
    if prefix_markers:
        raise GuardError("active PrefixLease/NativeRunLease markers block raw-file retirement")

    builds = _load_build_retention(repo_path)
    lease_root = builds.LEASE_ROOT
    try:
        root_info = os.stat(lease_root, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise GuardError("build lease registry is missing; cannot prove quiescence") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise GuardError("build lease registry is not a real directory")
    active_build_ids = []
    for lease_path in builds._lease_files():
        try:
            info = os.stat(lease_path, follow_symlinks=False)
        except FileNotFoundError as exc:
            raise GuardError("build lease registry changed during quiescence check") from exc
        if not stat.S_ISREG(info.st_mode):
            raise GuardError("build lease registry contains a symlink or special entry")
        try:
            lease = builds._load_lease(lease_path.stem)
        except Exception as exc:
            raise GuardError(f"cannot verify build lease {lease_path.name}: {exc}") from exc
        if lease.get("build_id") != lease_path.stem:
            raise GuardError(f"build lease identity mismatch: {lease_path.name}")
        if lease.get("state") == "ACTIVE":
            active_build_ids.append(lease_path.stem)
    if active_build_ids:
        raise GuardError("active build leases block raw-file retirement: " + ", ".join(sorted(active_build_ids)))
    return {"active_prefix_lease_markers": 0, "active_build_lease_ids": []}


def _append_event(receipt_fd: int, evidence_fd: int, receipt_name: str, signer: Any, event: dict[str, Any], sequence: int, previous_hash: str) -> str:
    path_stat = os.stat(receipt_name, dir_fd=evidence_fd, follow_symlinks=False)
    if not _same_regular(os.fstat(receipt_fd), path_stat):
        raise GuardError("receipt path changed during retirement")
    body = {"schema": "br8-raw-retirement-receipt/v1", "sequence": sequence, "previous_event_sha256": previous_hash, **event}
    signed = signer._seal_storage_evidence(body, "retirement_receipt_hmac_sha256")
    encoded = _json_line(signed)
    _write_all(receipt_fd, encoded)
    os.fsync(receipt_fd)
    os.fsync(evidence_fd)
    return hashlib.sha256(encoded).hexdigest()


def _create_outputs(evidence_fd: int, scope: str) -> tuple[str, str, int, int]:
    archive_name, receipt_name = OUTPUTS[scope]
    archive_fd = receipt_fd = -1
    try:
        archive_fd = os.open(archive_name, CREATE_FLAGS, 0o600, dir_fd=evidence_fd)
        receipt_fd = os.open(
            receipt_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=evidence_fd,
        )
        os.fsync(evidence_fd)
        return archive_name, receipt_name, archive_fd, receipt_fd
    except BaseException:
        if receipt_fd >= 0:
            os.close(receipt_fd)
        if archive_fd >= 0:
            try:
                expected = os.fstat(archive_fd)
                current = os.stat(archive_name, dir_fd=evidence_fd, follow_symlinks=False)
                if _same_regular(expected, current):
                    os.unlink(archive_name, dir_fd=evidence_fd)
                    os.fsync(evidence_fd)
            except OSError:
                pass
            os.close(archive_fd)
        raise


def _assert_target_live(runs_fd: int, target: dict[str, Any], run_fd: int) -> None:
    current = _open_dir_at(runs_fd, target["run_id"])
    try:
        if not _same_directory(os.fstat(current), os.fstat(run_fd)):
            raise GuardError(f"candidate run directory changed: {target['run_id']}")
    finally:
        os.close(current)


def _assert_directory_binding(parent_fd: int, name: str, held_fd: int, expected: Any, label: str) -> None:
    current = _open_dir_at(parent_fd, name)
    try:
        if not _same_directory(os.fstat(current), os.fstat(held_fd)) or not _matches_recorded_directory(expected, os.fstat(current), label):
            raise GuardError(f"restore directory identity changed: {label}")
    finally:
        os.close(current)


def _assert_tree(target: dict[str, Any], run_fd: int, removed: set[tuple[str, ...]]) -> None:
    files, dirs = _inventory_tree(run_fd)
    initial_files = target["files"]
    initial_dirs = target["directories"]
    expected_files = {path: ident for path, ident in initial_files.items() if path not in removed}
    if set(files) != set(expected_files):
        raise GuardError(f"candidate tree path set changed for {target['run_id']}")
    for path, expected in expected_files.items():
        if files[path] != expected:
            raise GuardError(f"preserved or selected file changed: {target['run_id']}/{'/'.join(path)}")
    if set(dirs) != set(initial_dirs):
        raise GuardError(f"candidate directory set changed: {target['run_id']}")
    for path, expected in initial_dirs.items():
        current = dirs[path]
        if (current["device"], current["inode"], current["mode"]) != (expected["device"], expected["inode"], expected["mode"]):
            raise GuardError(f"candidate directory identity changed: {target['run_id']}/{'/'.join(path)}")


def _remove_owned_outputs(evidence_fd: int, archive_name: str, receipt_name: str, archive_fd: int, receipt_fd: int) -> None:
    for name, fd in ((receipt_name, receipt_fd), (archive_name, archive_fd)):
        try:
            expected = os.fstat(fd)
            current = os.stat(name, dir_fd=evidence_fd, follow_symlinks=False)
            if stat.S_ISREG(current.st_mode) and _same_identity(expected, current):
                os.unlink(name, dir_fd=evidence_fd)
                os.fsync(evidence_fd)
        except FileNotFoundError:
            pass


def _run_execute(repo_path: str, evidence_fd: int, runs_fd: int, targets: list[dict[str, Any]], selected: list[dict[str, Any]], held: dict[str, int], scope: str, signer: Any) -> None:
    quiescence = _assert_no_active_retention(repo_path)
    archive_name, receipt_name, archive_fd, receipt_fd = _create_outputs(evidence_fd, scope)
    pre_retirement_durable = False
    unlinked = {target["run_id"]: set() for target in targets}
    sequence = 0
    previous_hash = ZERO_HASH
    try:
        _write_archive(archive_fd, selected)
        os.fchmod(archive_fd, 0o400)
        os.fsync(archive_fd)
        os.fsync(evidence_fd)
        archive_info = _verify_archive(evidence_fd, archive_name, selected)
        for record in selected:
            _verify_selected(record)
        archive_path = f"{EVIDENCE_REL}/{archive_name}"
        receipt_path = f"{EVIDENCE_REL}/{receipt_name}"
        event = {
            "event": "archive-verified-before-retirement",
            "archive_relative_path": archive_path,
            "archive": archive_info,
            "receipt_relative_path": receipt_path,
            "scope": scope,
            "target_runs": [
                {
                    "run_id": t["run_id"],
                    "replacement_run_id": t["replacement_run_id"],
                    "reviewed_retention_basis": t["reviewed_retention_basis"],
                    "candidate_result": t["candidate_result"],
                    "replacement_result": t["replacement_result"],
                    "assessment": t["assessment"],
                    "run_directory_identity": t["run_directory_identity"],
                    "host_traces_directory_identity": t["host_traces_directory_identity"],
                    "preserved_relative_paths": [
                        f"{RUNS_REL}/{t['run_id']}/{'/'.join(p)}"
                        for p in sorted((set(t["files"]) | set(t["directories"])) - set(t["selected_paths"]))
                    ],
                }
                for t in targets
            ],
            "selected_files": [
                {
                    "run_id": r["run_id"],
                    "source_relative_path": r["source_relative_path"],
                    "archive_member": r["archive_member"],
                    "sha256": r["sha256"],
                    "identity": r["identity"],
                }
                for r in selected
            ],
            "protected_run_ids": sorted(PROTECTED_RUN_IDS),
            "operator_quiescence_confirmation": True,
            "active_lease_check": quiescence,
            "retirement_order": "verified archive and signed initial receipt are fsynced before individual source unlinks",
            "restore_rule": "restore only absent destinations; never overwrite a different or symlink destination",
        }
        previous_hash = _append_event(receipt_fd, evidence_fd, receipt_name, signer, event, sequence, previous_hash)
        sequence += 1
        pre_retirement_durable = True
        for target in targets:
            run_id = target["run_id"]
            run_fd = held[run_id]
            for record in (r for r in selected if r["run_id"] == run_id):
                _assert_target_live(runs_fd, target, run_fd)
                _assert_tree(target, run_fd, unlinked[run_id])
                archive_now = _verify_archive(evidence_fd, archive_name, selected)
                if archive_now != archive_info:
                    raise GuardError("verified archive changed before retirement")
                quiescence = _assert_no_active_retention(repo_path)
                verified = _verify_selected(record)
                previous_hash = _append_event(
                    receipt_fd, evidence_fd, receipt_name, signer,
                    {
                        "event": "source-unlink-armed",
                        "source_relative_path": record["source_relative_path"],
                        "verification": verified,
                        "active_lease_check": quiescence,
                        "operator_quiescence_confirmation": True,
                    },
                    sequence, previous_hash,
                )
                sequence += 1
                _assert_target_live(runs_fd, target, run_fd)
                _assert_tree(target, run_fd, unlinked[run_id])
                if _verify_selected(record) != verified:
                    raise GuardError(f"source changed after signed unlink intent: {record['source_relative_path']}")
                os.unlink(record["filename"], dir_fd=record["parent_fd"])
                os.fsync(record["parent_fd"])
                try:
                    os.stat(record["filename"], dir_fd=record["parent_fd"], follow_symlinks=False)
                except FileNotFoundError:
                    pass
                else:
                    raise GuardError(f"source still exists after unlink: {record['source_relative_path']}")
                unlinked[run_id].add(record["parts"])
                _assert_tree(target, run_fd, unlinked[run_id])
                previous_hash = _append_event(
                    receipt_fd, evidence_fd, receipt_name, signer,
                    {
                        "event": "source-unlinked-parent-fsynced",
                        "source_relative_path": record["source_relative_path"],
                        "sha256": verified["sha256"],
                        "size": verified["size"],
                        "device": verified["device"],
                        "inode": verified["inode"],
                        "mtime_ns": verified["mtime_ns"],
                    },
                    sequence, previous_hash,
                )
                sequence += 1
        for target in targets:
            _assert_target_live(runs_fd, target, held[target["run_id"]])
            _assert_tree(target, held[target["run_id"]], unlinked[target["run_id"]])
        previous_hash = _append_event(
            receipt_fd, evidence_fd, receipt_name, signer,
            {
                "event": "retirement-complete",
                "archive_relative_path": archive_path,
                "archive_sha256": archive_info["sha256"],
                "removed_source_count": len(selected),
                "candidate_directories_preserved": True,
                "unselected_tree_paths_preserved": True,
            },
            sequence, previous_hash,
        )
        os.fchmod(receipt_fd, 0o400)
        os.fsync(receipt_fd)
        os.fsync(evidence_fd)
        print("RETIREMENT COMPLETE")
        print(f"Archive: {archive_path} ({archive_info['size']} bytes, sha256 {archive_info['sha256']})")
        print(f"Signed receipt: {receipt_path}")
    except BaseException as exc:
        if not pre_retirement_durable:
            # Before any source unlink, these outputs contain no unique source
            # evidence.  Remove only files whose identities still match the
            # descriptors exclusively created by this invocation.
            _remove_owned_outputs(evidence_fd, archive_name, receipt_name, archive_fd, receipt_fd)
            raise GuardError(f"stopped before source retirement; inspect the reserved outputs: {exc}") from exc
        raise GuardError(f"stopped after durable archive/receipt; inspect the signed ledger before retry: {exc}") from exc
    finally:
        os.close(receipt_fd)
        os.close(archive_fd)


def _load_receipt(evidence_fd: int, scope: str, signer: Any) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    archive_name, receipt_name = OUTPUTS[scope]
    raw, _ = _read_regular(evidence_fd, receipt_name, f"{EVIDENCE_REL}/{receipt_name}")
    lines = raw.splitlines()
    if not lines:
        raise GuardError("signed receipt is empty")
    events: list[dict[str, Any]] = []
    previous = ZERO_HASH
    for sequence, line in enumerate(lines):
        try:
            event = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GuardError(f"invalid signed receipt line {sequence}: {exc}") from exc
        if not isinstance(event, dict) or event.get("schema") != "br8-raw-retirement-receipt/v1" or event.get("sequence") != sequence or event.get("previous_event_sha256") != previous:
            raise GuardError(f"receipt chain mismatch at line {sequence}")
        if not signer._verify_storage_evidence(event, "retirement_receipt_hmac_sha256"):
            raise GuardError(f"receipt HMAC failed at line {sequence}")
        events.append(event)
        previous = hashlib.sha256(line + b"\n").hexdigest()
    first = events[0]
    if first.get("event") != "archive-verified-before-retirement" or first.get("scope") != scope or first.get("archive_relative_path") != f"{EVIDENCE_REL}/{archive_name}":
        raise GuardError("receipt does not describe the exact requested archive scope")
    if any(event.get("event") == "retirement-complete" for event in events[1:]):
        final = next(event for event in reversed(events) if event.get("event") == "retirement-complete")
    else:
        final = {}
    selected = first.get("selected_files")
    if not isinstance(selected, list) or not selected:
        raise GuardError("receipt has no selected file records")
    return first, selected, final


def _restore(repo_fd: int, evidence_fd: int, scope: str, signer: Any) -> None:
    archive_name, _ = OUTPUTS[scope]
    first, selected, _ = _load_receipt(evidence_fd, scope, signer)
    archive_meta = first.get("archive")
    if not isinstance(archive_meta, dict):
        raise GuardError("receipt has no verified archive metadata")
    archive_path_before = os.stat(archive_name, dir_fd=evidence_fd, follow_symlinks=False)
    if not stat.S_ISREG(archive_path_before.st_mode):
        raise GuardError("signed archive path is not a regular file")
    archive_fd = os.open(archive_name, READ_FLAGS, dir_fd=evidence_fd)
    try:
        archive_before = os.fstat(archive_fd)
        if not _same_identity(archive_path_before, archive_before) or _hash_fd(archive_fd) != archive_meta.get("sha256") or archive_before.st_size != archive_meta.get("size"):
            raise GuardError("archive hash or size differs from its signed receipt")
        runs_fd = _open_chain(repo_fd, tuple(RUNS_REL.split("/")))
        try:
            target_rows = first.get("target_runs")
            if not isinstance(target_rows, list):
                raise GuardError("receipt has no target run directory identities")
            targets_by_id: dict[str, dict[str, Any]] = {}
            for target in target_rows:
                if not isinstance(target, dict) or not isinstance(target.get("run_id"), str):
                    raise GuardError("receipt has an invalid target run identity row")
                if target["run_id"] in targets_by_id:
                    raise GuardError("receipt repeats a target run identity")
                targets_by_id[target["run_id"]] = target
            selected_run_ids = {record.get("run_id") for record in selected if isinstance(record, dict)}
            if selected_run_ids != set(targets_by_id):
                raise GuardError("receipt target directory identities do not exactly match selected runs")
            os.lseek(archive_fd, 0, os.SEEK_SET)
            with os.fdopen(os.dup(archive_fd), "rb") as stream:
                with tarfile.open(fileobj=stream, mode="r:gz") as archive:
                    members = {member.name: member for member in archive.getmembers()}
                    expected_names = {record.get("archive_member") for record in selected}
                    if set(members) != expected_names or len(members) != len(selected):
                        raise GuardError("archive member set differs from signed receipt")
                    # Validate every archived byte before restoring the first
                    # missing source, so a damaged late member cannot leave a
                    # partially recovered set.
                    for record in selected:
                        member_name = record.get("archive_member")
                        member = members[member_name]
                        if not member.isfile() or member.size != record.get("identity", {}).get("size"):
                            raise GuardError(f"unsafe or size-mismatched archive member: {member_name}")
                        data = archive.extractfile(member)
                        if data is None:
                            raise GuardError(f"cannot read archive member during verification: {member_name}")
                        digest = hashlib.sha256()
                        total = 0
                        with data:
                            while True:
                                chunk = data.read(CHUNK)
                                if not chunk:
                                    break
                                total += len(chunk)
                                digest.update(chunk)
                        if total != record.get("identity", {}).get("size") or digest.hexdigest() != record.get("sha256"):
                            raise GuardError(f"archive member differs from signed receipt: {member_name}")
                    for record in selected:
                        member_name = record.get("archive_member")
                        member = members[member_name]
                        source_path = record.get("source_relative_path")
                        if not isinstance(source_path, str) or source_path != member_name or not member.isfile():
                            raise GuardError(f"unsafe restore member: {member_name}")
                        parts = PurePosixPath(source_path).parts
                        if PurePosixPath(source_path).is_absolute() or ".." in parts or tuple(parts[: len(RUNS_REL.split("/"))]) != tuple(RUNS_REL.split("/")):
                            raise GuardError(f"unsafe source path in receipt: {source_path}")
                        remaining = parts[len(RUNS_REL.split("/")):]
                        if len(remaining) < 2:
                            raise GuardError(f"source path is outside a run directory: {source_path}")
                        run_id, file_parts = remaining[0], tuple(remaining[1:])
                        candidate_ids = {c["run_id"] for c in FOCUS_CANDIDATES} | {PARTIAL_D_CANDIDATE["run_id"]}
                        if run_id not in candidate_ids or run_id in PROTECTED_RUN_IDS or file_parts not in _paths_for_run_id(run_id):
                            raise GuardError(f"restore path is outside the reviewed allowlist: {source_path}")
                        target = targets_by_id.get(run_id)
                        if target is None:
                            raise GuardError(f"restore receipt omits the target run identity: {run_id}")
                        run_fd = _open_dir_at(runs_fd, run_id)
                        try:
                            if not _matches_recorded_directory(
                                target.get("run_directory_identity"), os.fstat(run_fd), f"{run_id}"
                            ):
                                raise GuardError(f"restore run directory no longer matches the signed receipt: {run_id}")
                            _assert_directory_binding(
                                runs_fd, run_id, run_fd, target.get("run_directory_identity"), run_id
                            )
                            parent_fd = run_fd
                            opened_host_fd = -1
                            if len(file_parts) == 2:
                                if file_parts[0] != "host-traces":
                                    raise GuardError(f"restore nested path is outside host-traces: {source_path}")
                                opened_host_fd = _open_dir_at(run_fd, file_parts[0])
                                if not _matches_recorded_directory(
                                    target.get("host_traces_directory_identity"),
                                    os.fstat(opened_host_fd),
                                    f"{run_id}/host-traces",
                                ):
                                    raise GuardError(f"restore host-traces directory no longer matches the signed receipt: {run_id}")
                                parent_fd = opened_host_fd
                            try:
                                _assert_directory_binding(
                                    runs_fd, run_id, run_fd, target.get("run_directory_identity"), run_id
                                )
                                if opened_host_fd >= 0:
                                    _assert_directory_binding(
                                        run_fd,
                                        "host-traces",
                                        opened_host_fd,
                                        target.get("host_traces_directory_identity"),
                                        f"{run_id}/host-traces",
                                    )
                                try:
                                    current = os.stat(file_parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                                except FileNotFoundError:
                                    current = None
                                if current is not None:
                                    if not stat.S_ISREG(current.st_mode):
                                        raise GuardError(f"restore destination is not a regular file: {source_path}")
                                    present_fd = os.open(file_parts[-1], READ_FLAGS, dir_fd=parent_fd)
                                    try:
                                        present_before = os.fstat(present_fd)
                                        present_hash = _hash_fd(present_fd)
                                        present_after = os.fstat(present_fd)
                                        present_path_after = os.stat(file_parts[-1], dir_fd=parent_fd, follow_symlinks=False)
                                        if (
                                            not _same_identity(current, present_before)
                                            or not _same_identity(present_before, present_after)
                                            or not _same_identity(present_before, present_path_after)
                                            or present_hash != record.get("sha256")
                                            or present_before.st_size != record.get("identity", {}).get("size")
                                        ):
                                            raise GuardError(f"refusing to overwrite differing existing path: {source_path}")
                                    finally:
                                        os.close(present_fd)
                                    continue
                                _assert_directory_binding(
                                    runs_fd, run_id, run_fd, target.get("run_directory_identity"), run_id
                                )
                                if opened_host_fd >= 0:
                                    _assert_directory_binding(
                                        run_fd,
                                        "host-traces",
                                        opened_host_fd,
                                        target.get("host_traces_directory_identity"),
                                        f"{run_id}/host-traces",
                                    )
                                _restore_one(archive, member, parent_fd, file_parts[-1], record)
                            finally:
                                if opened_host_fd >= 0:
                                    os.close(opened_host_fd)
                        finally:
                            os.close(run_fd)
            if _hash_fd(archive_fd) != archive_meta.get("sha256") or not _same_identity(archive_before, os.fstat(archive_fd)):
                raise GuardError("archive changed during restore")
            archive_path_after = os.stat(archive_name, dir_fd=evidence_fd, follow_symlinks=False)
            if not _same_identity(archive_before, archive_path_after):
                raise GuardError("archive path changed during restore")
            os.fsync(runs_fd)
        finally:
            os.close(runs_fd)
    finally:
        os.close(archive_fd)
    print(f"RESTORE VERIFIED: {scope}; existing identical files were left untouched")


def _paths_for_run_id(run_id: str) -> set[tuple[str, ...]]:
    for candidate in (*FOCUS_CANDIDATES, PARTIAL_D_CANDIDATE):
        if candidate["run_id"] == run_id:
            return set(candidate["raw_files"])
    return set()


def _restore_one(archive: tarfile.TarFile, member: tarfile.TarInfo, parent_fd: int, filename: str, record: dict[str, Any]) -> None:
    source = archive.extractfile(member)
    if source is None:
        raise GuardError(f"cannot read archive member during restore: {member.name}")
    temporary = f".{filename}.br8-restore-{uuid.uuid4().hex}.tmp"
    temp_fd = os.open(temporary, CREATE_FLAGS, 0o600, dir_fd=parent_fd)
    digest = hashlib.sha256()
    total = 0
    try:
        with source:
            while True:
                chunk = source.read(CHUNK)
                if not chunk:
                    break
                _write_all(temp_fd, chunk)
                total += len(chunk)
                digest.update(chunk)
        identity = record.get("identity", {})
        if total != identity.get("size") or digest.hexdigest() != record.get("sha256"):
            raise GuardError(f"restored bytes differ from receipt: {member.name}")
        os.fchmod(temp_fd, identity.get("mode", 0o600))
        mtime_ns = identity.get("mtime_ns")
        if isinstance(mtime_ns, int):
            os.utime(temp_fd, ns=(mtime_ns, mtime_ns))
        os.fsync(temp_fd)
        os.link(temporary, filename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
        os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except BaseException:
        try:
            current = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
            if stat.S_ISREG(current.st_mode):
                os.unlink(temporary, dir_fd=parent_fd)
                os.fsync(parent_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(temp_fd)


def _print_plan(scope: str, targets: list[dict[str, Any]], selected: list[dict[str, Any]], evidence_fd: int) -> None:
    archive_name, receipt_name = OUTPUTS[scope]
    for name in (archive_name, receipt_name):
        try:
            os.stat(name, dir_fd=evidence_fd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        raise GuardError(f"reserved output already exists; refusing overwrite: {EVIDENCE_REL}/{name}")
    print("MODE: DRY RUN (no archive, receipt, or source changes)")
    print(f"Scope: {scope}")
    print("Targets: " + ", ".join(t["run_id"] for t in targets))
    print(f"Selected files: {len(selected)} ({sum(r['identity']['size'] for r in selected)} logical bytes)")
    print(f"Reserved archive: {EVIDENCE_REL}/{archive_name}")
    print(f"Reserved signed receipt: {EVIDENCE_REL}/{receipt_name}")
    for record in selected:
        print(f"  {record['source_relative_path']} size={record['identity']['size']} sha256={record['sha256']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="write/verify the archive and individually retire allowlisted raw files")
    mode.add_argument("--restore", action="store_true", help="restore from the signed archive without overwriting any path")
    parser.add_argument("--partial-d", action="store_true", help="select only the superseded BR8 partial-coverage run; never selected by default")
    parser.add_argument(
        "--confirm-quiescent",
        action="store_true",
        help="confirm the candidate run paths have a sole writer and no PrefixLease/NativeRunLease/build work is active; required with --execute",
    )
    args = parser.parse_args(argv)
    if args.execute and not args.confirm_quiescent:
        parser.error("--execute requires --confirm-quiescent after a quiescence review")
    if args.confirm_quiescent and not args.execute:
        parser.error("--confirm-quiescent is valid only with --execute")
    scope = "partial-d" if args.partial_d else "focus"
    repo_fd = evidence_fd = runs_fd = None
    targets: list[dict[str, Any]] = []
    selected: list[dict[str, Any]] = []
    held: dict[str, int] = {}
    try:
        _require_posix()
        script_path = os.path.abspath(__file__)
        evidence_path = os.path.dirname(script_path)
        experiment_path = os.path.dirname(os.path.dirname(evidence_path))
        repo_path = os.path.dirname(os.path.dirname(experiment_path))
        expected_script = os.path.join(repo_path, EVIDENCE_REL, "archive_superseded_focus_raw.py")
        script_st = os.stat(script_path, follow_symlinks=False)
        if not stat.S_ISREG(script_st.st_mode) or os.path.normpath(script_path) != os.path.normpath(expected_script):
            raise GuardError(f"script must be a regular file at its fixed repository path: {script_path}")
        repo_fd = _open_absolute_directory(repo_path)
        evidence_fd = _open_chain(repo_fd, tuple(EVIDENCE_REL.split("/")))
        if args.restore:
            signer = _load_signer(repo_path)
            _restore(repo_fd, evidence_fd, scope, signer)
            return 0
        runs_fd = _open_chain(repo_fd, tuple(RUNS_REL.split("/")))
        candidates = (PARTIAL_D_CANDIDATE,) if args.partial_d else FOCUS_CANDIDATES
        for candidate in candidates:
            evidence, run_fd, host_fd, _ = _candidate_evidence(evidence_fd, runs_fd, candidate)
            held[candidate["run_id"]] = run_fd
            held[f"{candidate['run_id']}/host-traces"] = host_fd
            targets.append(evidence)
            for parts, size in sorted(evidence["selected_paths"].items()):
                parent = host_fd if len(parts) == 2 else run_fd
                name = parts[-1]
                before_path = os.stat(name, dir_fd=parent, follow_symlinks=False)
                if not stat.S_ISREG(before_path.st_mode) or before_path.st_size != size:
                    raise GuardError(f"selected source changed: {candidate['run_id']}/{'/'.join(parts)}")
                fd = os.open(name, READ_FLAGS, dir_fd=parent)
                before_fd = os.fstat(fd)
                if not _same_identity(before_path, before_fd):
                    os.close(fd)
                    raise GuardError(f"selected source changed while opening: {candidate['run_id']}/{'/'.join(parts)}")
                digest = _hash_fd(fd)
                if not _same_identity(before_fd, os.fstat(fd)) or not _same_identity(before_fd, os.stat(name, dir_fd=parent, follow_symlinks=False)):
                    os.close(fd)
                    raise GuardError(f"selected source changed while hashing: {candidate['run_id']}/{'/'.join(parts)}")
                relative = f"{RUNS_REL}/{candidate['run_id']}/{'/'.join(parts)}"
                selected.append({"run_id": candidate["run_id"], "parts": parts, "filename": name, "parent_fd": parent, "fd": fd, "source_relative_path": relative, "archive_member": relative, "sha256": digest, "identity": _identity(before_fd)})
        if args.execute:
            signer = _load_signer(repo_path)
            _run_execute(repo_path, evidence_fd, runs_fd, targets, selected, held, scope, signer)
        else:
            _print_plan(scope, targets, selected, evidence_fd)
        return 0
    except (OSError, GuardError, RuntimeError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    finally:
        for record in selected:
            try:
                os.close(record["fd"])
            except OSError:
                pass
        for fd in held.values():
            try:
                os.close(fd)
            except OSError:
                pass
        for fd in (runs_fd, evidence_fd, repo_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass


if __name__ == "__main__":
    raise SystemExit(main())
