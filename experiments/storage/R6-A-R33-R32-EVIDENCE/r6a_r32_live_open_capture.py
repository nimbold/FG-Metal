#!/usr/bin/env python3
"""One fail-closed structured lsof capture for the R23 historical build root."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


BUILD_ID = "r6a-r23-dual-lineage-20261020t214331z"
OWNER = "R6-A-R23-DUAL-LINEAGE-20261020T214331Z"
EXPECTED_PARENT = "/Users/nima/Library/Caches/FGMetal/builds"
EXPECTED_ROOT = f"{EXPECTED_PARENT}/{BUILD_ID}"
EXPECTED_PARENT_IDENTITY = {
    "device": 16777229,
    "inode": 93094313,
    "filesystem_id": 16777229,
    "mode": 0o700,
    "sticky": False,
    "owner_uid": 501,
}
EXPECTED_ROOT_IDENTITY = {
    "device": 16777229,
    "inode": 102865819,
    "filesystem_id": 16777229,
    "type": stat.S_IFDIR,
}
PRECHECK = "experiments/storage/R6-A-R32-R23-PARENT-ROOT-PRECHECK-20261021.json"
PRECHECK_SHA256 = "2a648916c9042309b7feb10714e545eb6cefea10a01345917031de9e56dddeed"
OUTPUT_JSON = "experiments/storage/R6-A-R32-R23-LIVE-OPEN-20261021.json"
OUTPUT_MD = "experiments/storage/R6-A-R32-R23-LIVE-OPEN-20261021.md"
LSOF = "/usr/sbin/lsof"


class CaptureError(RuntimeError):
    pass


def process_snapshot() -> dict[str, Any]:
    pid = os.getpid()
    ppid = os.getppid()
    cwd = os.getcwd()
    argv = [
        "/bin/ps", "-p", str(pid), "-o", "pid=", "-o", "ppid=",
        "-o", "lstart=", "-o", "command=",
    ]
    result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    raw = result.stdout.decode("utf-8", "strict").strip()
    parts = raw.split(None, 7)
    if result.returncode != 0 or len(parts) != 8:
        raise CaptureError(f"checker process identity query incomplete: status={result.returncode}; output={raw!r}")
    if int(parts[0]) != pid or int(parts[1]) != ppid or not os.path.isabs(cwd):
        raise CaptureError("checker process identity did not agree with /bin/ps")
    return {
        "pid": pid,
        "ppid": ppid,
        "start_identity": " ".join(parts[2:7]),
        "command": parts[7],
        "cwd": cwd,
        "ps_argv": argv,
        "ps_returncode": result.returncode,
    }


def open_dir_nofollow(path: str) -> int:
    absolute = Path(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(absolute.anchor or "/", flags)
    try:
        for component in absolute.parts[1:]:
            child = os.open(component, flags, dir_fd=fd)
            if not stat.S_ISDIR(os.fstat(child).st_mode):
                os.close(child)
                raise CaptureError(f"non-directory path component: {absolute}")
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def fstat_identity(fd: int, *, with_fsid: bool = False) -> dict[str, Any]:
    info = os.fstat(fd)
    result: dict[str, Any] = {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": stat.S_IMODE(info.st_mode),
        "type": stat.S_IFMT(info.st_mode),
        "is_directory": stat.S_ISDIR(info.st_mode),
        "owner_uid": info.st_uid,
        "owner_gid": info.st_gid,
        "nlink": info.st_nlink,
    }
    if with_fsid:
        result["filesystem_id"] = getattr(os.fstatvfs(fd), "f_fsid", None)
    return result


def parent_identity(fd: int) -> dict[str, Any]:
    identity = fstat_identity(fd, with_fsid=True)
    identity["sticky"] = bool(os.fstat(fd).st_mode & stat.S_ISVTX)
    return {
        "device": identity["device"],
        "inode": identity["inode"],
        "filesystem_id": identity["filesystem_id"],
        "mode": identity["mode"],
        "sticky": identity["sticky"],
        "owner_uid": identity["owner_uid"],
    }


def read_nofollow(path: str, limit: int = 8 * 1024 * 1024) -> bytes:
    parent_fd = open_dir_nofollow(os.path.dirname(os.path.abspath(path)))
    try:
        fd = os.open(
            os.path.basename(path),
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            dir_fd=parent_fd,
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size < 0 or info.st_size > limit:
                raise CaptureError(f"expected bounded regular evidence file: {path}")
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = os.read(fd, min(65536, limit + 1 - size))
                if not chunk:
                    return b"".join(chunks)
                chunks.append(chunk)
                size += len(chunk)
                if size > limit:
                    raise CaptureError(f"evidence file exceeds limit: {path}")
        finally:
            os.close(fd)
    finally:
        os.close(parent_fd)


def stat_at(directory_fd: int, name: str) -> dict[str, Any]:
    info = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "type": stat.S_IFMT(info.st_mode),
        "mode": stat.S_IMODE(info.st_mode),
        "owner_uid": info.st_uid,
        "owner_gid": info.st_gid,
        "nlink": info.st_nlink,
        "is_directory": stat.S_ISDIR(info.st_mode),
    }


def open_lock(directory_fd: int, name: str, prior: dict[str, Any], prefix: str) -> tuple[int, dict[str, Any]]:
    fd = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        dir_fd=directory_fd,
    )
    opened = fstat_identity(fd)
    named = stat_at(directory_fd, name)
    checks = {
        "regular_file": opened["type"] == stat.S_IFREG,
        "mode_0600": opened["mode"] == 0o600,
        "nlink_one": opened["nlink"] == 1,
        "owner_uid_current": opened["owner_uid"] == os.getuid(),
        "named_fd_device_equal": named["device"] == opened["device"],
        "named_fd_inode_equal": named["inode"] == opened["inode"],
        "device_matches_precheck": opened["device"] == prior[f"{prefix}_device"],
        "inode_matches_precheck": opened["inode"] == prior[f"{prefix}_inode"],
    }
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        checks["exclusive_lock_held"] = True
    except OSError as error:
        checks["exclusive_lock_held"] = False
        checks["lock_error"] = f"{type(error).__name__}: {error}"
    return fd, {
        "descriptor": fd,
        "opened": opened,
        "named": named,
        "checks": checks,
        "path_name": name,
    }


def parse_number(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        if raw.startswith(("0x", "0X")):
            return int(raw, 16)
        if raw.startswith("0t") and raw[2:].isdigit():
            return int(raw[2:], 10)
        return int(raw, 10)
    except ValueError:
        return None


def parse_lsof(raw: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stream_terminated = raw.endswith(b"\0")
    try:
        tokens = raw.decode("utf-8", "strict").split("\0")
        utf8_valid = True
    except UnicodeDecodeError:
        tokens = raw.decode("utf-8", "replace").split("\0")
        utf8_valid = False
    if tokens and tokens[-1] == "":
        tokens.pop()
    process: dict[str, str] = {}
    row: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    unattached: list[str] = []

    def finish() -> None:
        nonlocal row
        if row is not None:
            fields = row["field_values"]
            fd_raw = fields.get("f")
            fd_match = re.match(r"^([0-9]+)(.*)$", fd_raw or "")
            device_raw = fields.get("D") or fields.get("r")
            inode_raw = fields.get("i")
            row.update({
                "pid": parse_number(fields.get("p")),
                "ppid": parse_number(fields.get("R")),
                "command": fields.get("c"),
                "fd": fd_raw,
                "fd_number": int(fd_match.group(1)) if fd_match else None,
                "fd_suffix": fd_match.group(2) if fd_match else None,
                "fd_type": fields.get("t"),
                "device_raw": device_raw,
                "device_field": "D" if fields.get("D") is not None else ("r" if fields.get("r") is not None else None),
                "device": parse_number(device_raw),
                "inode_raw": inode_raw,
                "inode": parse_number(inode_raw),
                "name": fields.get("n"),
            })
            required = (
                row["pid"] is not None
                and fd_raw is not None
                and row["fd_type"] is not None
                and row["device"] is not None
                and row["inode"] is not None
                and row["name"] is not None
                and row["command"] is not None
                and utf8_valid
            )
            row["parser_status"] = "PASS" if required else "MALFORMED_OR_UNRESOLVED"
            rows.append(row)
            row = None

    for token in tokens:
        if not token:
            continue
        key, value = token[0], token[1:]
        if key == "p":
            finish()
            process = {"p": value}
        elif key == "f":
            finish()
            row = {"field_values": dict(process)}
            row["field_values"]["f"] = value
        elif row is not None:
            row["field_values"][key] = value
        elif key in {"c", "R"} and process:
            process[key] = value
        else:
            unattached.append(token)
    finish()
    return rows, {
        "null_terminated": stream_terminated,
        "utf8_valid": utf8_valid,
        "unattached_fields": unattached,
        "row_count": len(rows),
    }


def write_new(directory: str, name: str, payload: bytes) -> None:
    directory_fd = open_dir_nofollow(directory)
    try:
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def markdown(report: dict[str, Any]) -> bytes:
    lines = [
        "# R23 live-open structured capture",
        "",
        f"- Classification: {report.get('classification')}",
        f"- Checker PID: {report.get('checker', {}).get('pid')}",
        f"- lsof child PID: {report.get('lsof', {}).get('child_pid')}",
        f"- Root pin FD: {report.get('root_pin', {}).get('fd')}",
        f"- Root identity: {report.get('root_pin', {}).get('fstat')}",
        f"- lsof return code: {report.get('lsof', {}).get('returncode')}",
        f"- Complete structured stream: {report.get('stream', {}).get('complete')}",
        f"- Expected pin matches: {report.get('expected_pin_match_count')}",
        "",
        "## Lock identities",
        "",
    ]
    for label, item in report.get("locks", {}).items():
        lines.append(f"- {label}: fd {item.get('descriptor')}, identity {item.get('opened')}, checks {item.get('checks')}")
    lines.extend(["", "## Structured lsof rows", "", "| PID | PPID | Command | FD | Type | Device | Inode | Name | Parser | Classification |", "|---:|---:|---|---|---|---:|---:|---|---|---|"])
    for item in report.get("rows", []):
        values = [
            item.get("pid"), item.get("ppid"), item.get("command"), item.get("fd"),
            item.get("fd_type"), item.get("device"), item.get("inode"), item.get("name"),
            item.get("parser_status"), item.get("classification"),
        ]
        lines.append("| " + " | ".join(str(value).replace("|", "\\|") if value is not None else "null" for value in values) + " |")
    lines.extend([
        "",
        "## Scanner result",
        "",
        f"- Blockers: {report.get('blockers')}",
        f"- Root revalidation: {report.get('root_revalidation')}",
        f"- lsof stderr: {report.get('lsof', {}).get('stderr')}",
        "- lsof stdout is preserved byte-for-byte as base64 in the JSON report.",
        "- No R23 registry, marker, build-root, or closeout mutation was performed.",
        "",
    ])
    return ("\n".join(lines)).encode("utf-8")


def main() -> int:
    report: dict[str, Any] = {
        "schema_version": 1,
        "started_at_unix": time.time(),
        "build_id": BUILD_ID,
        "owner_experiment": OWNER,
        "expected_parent": EXPECTED_PARENT,
        "expected_root": EXPECTED_ROOT,
        "identity_review": {
            "verdict": "PASS — R23 PARENT/ROOT IDENTITY VERIFIED",
            "precheck_json_sha256": PRECHECK_SHA256,
        },
        "mutations": [],
        "closeout_invoked": False,
        "lsof": {"launched": False},
        "rows": [],
        "blockers": [],
    }
    opened: dict[str, int] = {}
    root_parent_fd: int | None = None
    try:
        report["checker"] = process_snapshot()

        precheck_bytes = read_nofollow(PRECHECK)
        precheck_hash = hashlib.sha256(precheck_bytes).hexdigest()
        if precheck_hash != PRECHECK_SHA256:
            raise CaptureError(f"precheck evidence hash changed: {precheck_hash}")
        precheck = json.loads(precheck_bytes)
        checks = {item["id"]: item for item in precheck["checks"]}
        identity_rows = [item for item in precheck["checks"] if item.get("phase") == "parent_root_identity"]
        identity_rows_ok = (
            precheck.get("classification") == "PARENT_ROOT_IDENTITY_PASS"
            and precheck.get("all_parent_root_rows_evaluated") is True
            and precheck.get("all_parent_root_rows_serialized") is True
            and precheck.get("missing_identity_row_ids") == []
            and len(identity_rows) == 40
            and all(item.get("result") == "PASS" for item in identity_rows)
        )
        if not identity_rows_ok:
            raise CaptureError("reviewed precheck no longer has all 40 passing parent/root identity rows")
        report["precheck"] = {
            "path": PRECHECK,
            "sha256": precheck_hash,
            "classification": precheck["classification"],
            "parent_root_row_count": len(identity_rows),
            "all_parent_root_rows_pass": identity_rows_ok,
        }

        canonical_parent = os.path.realpath(EXPECTED_PARENT)
        canonical_root = os.path.realpath(EXPECTED_ROOT)
        report["canonical_paths"] = {
            "expected_parent": EXPECTED_PARENT,
            "observed_parent": canonical_parent,
            "expected_root": EXPECTED_ROOT,
            "observed_root": canonical_root,
        }
        if canonical_parent != EXPECTED_PARENT or canonical_root != EXPECTED_ROOT:
            raise CaptureError("canonical parent/root paths differ from exact historical paths")

        root_parent_fd = open_dir_nofollow(EXPECTED_PARENT)
        current_parent_identity = parent_identity(root_parent_fd)
        if current_parent_identity != EXPECTED_PARENT_IDENTITY:
            raise CaptureError(f"approved parent identity changed: {current_parent_identity!r}")
        if current_parent_identity != precheck["parent_capture"]["parent_fd_identity"]:
            raise CaptureError("approved parent identity differs from reviewed precheck capture")
        report["parent"] = {
            "fd": root_parent_fd,
            "identity": current_parent_identity,
        }

        prior_values = {
            "allocation_device": checks["ALLOC_LOCK_NAMED_FD_DEVICE"]["observed"],
            "allocation_inode": checks["ALLOC_LOCK_NAMED_FD_INODE"]["observed"],
            "retention_device": checks["RETENTION_LOCK_NAMED_FD_DEVICE"]["observed"],
            "retention_inode": checks["RETENTION_LOCK_NAMED_FD_INODE"]["observed"],
        }
        cache_fd = open_dir_nofollow("/Users/nima/Library/Caches/FGMetal")
        opened["cache_fd"] = cache_fd
        allocation_fd, allocation = open_lock(cache_fd, ".storage-allocation.lock", prior_values, "allocation")
        opened["allocation_lock_fd"] = allocation_fd
        lease_fd = open_dir_nofollow(os.path.join(os.getcwd(), "experiments/storage/build-leases"))
        opened["lease_fd"] = lease_fd
        retention_fd, retention = open_lock(lease_fd, ".retention.lock", prior_values, "retention")
        opened["retention_lock_fd"] = retention_fd
        report["locks"] = {
            "allocation": allocation,
            "retention": retention,
        }
        for label, item in report["locks"].items():
            if not all(value is True for value in item["checks"].values() if isinstance(value, bool)):
                raise CaptureError(f"{label} lock identity or exclusive nonblocking lock check failed")

        root_name = os.path.basename(EXPECTED_ROOT)
        named_before = stat_at(root_parent_fd, root_name)
        root_fd = os.open(
            root_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=root_parent_fd,
        )
        opened["root_pin_fd"] = root_fd
        pinned = fstat_identity(root_fd, with_fsid=True)
        root_identity_ok = (
            named_before["device"] == EXPECTED_ROOT_IDENTITY["device"]
            and named_before["inode"] == EXPECTED_ROOT_IDENTITY["inode"]
            and named_before["type"] == EXPECTED_ROOT_IDENTITY["type"]
            and pinned["device"] == EXPECTED_ROOT_IDENTITY["device"]
            and pinned["inode"] == EXPECTED_ROOT_IDENTITY["inode"]
            and pinned["type"] == EXPECTED_ROOT_IDENTITY["type"]
            and pinned["filesystem_id"] == EXPECTED_ROOT_IDENTITY["filesystem_id"]
            and (named_before["device"], named_before["inode"], named_before["type"])
            == (pinned["device"], pinned["inode"], pinned["type"])
        )
        report["root_pin"] = {
            "fd": root_fd,
            "role": "root identity pin",
            "canonical_root": canonical_root,
            "named_root_before_pin": named_before,
            "fstat": pinned,
            "f_getpath": {"attempted": False, "supported": False, "value": None},
            "identity_pass": root_identity_ok,
        }
        if not root_identity_ok:
            raise CaptureError("new named-root/pinned-root identity does not match authenticated R23 identity")

        argv = [LSOF, "-F0pcRftDri", "+D", canonical_root]
        report["lsof"] = {
            "launched": True,
            "argv": argv,
            "child_ppid": report["checker"]["pid"],
        }
        started = time.monotonic()
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        report["lsof"]["child_pid"] = process.pid
        stdout, stderr = process.communicate()
        report["lsof"].update({
            "returncode": process.returncode,
            "stdout_base64": base64.b64encode(stdout).decode("ascii"),
            "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
            "stdout_bytes": len(stdout),
            "stderr": stderr.decode("utf-8", "replace"),
            "stderr_bytes": len(stderr),
            "elapsed_seconds": round(time.monotonic() - started, 6),
            "version": "lsof 4.91 (libproc-based)",
        })
        rows, stream = parse_lsof(stdout)
        stream["complete"] = (
            stream["null_terminated"]
            and stream["utf8_valid"]
            and not stream["unattached_fields"]
            and process.returncode == 0
            and len(stderr) == 0
        )
        report["stream"] = stream
        report["rows"] = rows

        expected_matches = 0
        for item in rows:
            if item["parser_status"] != "PASS":
                item["classification"] = "MALFORMED_OR_UNRESOLVED"
                item["identity_match_fields"] = {}
                continue
            same_process = item["pid"] == report["checker"]["pid"]
            same_fd = item["fd_number"] == root_fd
            same_type = item["fd_type"] == "DIR"
            same_device = item["device"] == pinned["device"]
            same_inode = item["inode"] == pinned["inode"]
            name = item["name"]
            canonical_name = os.path.realpath(os.path.normpath(name)) if os.path.isabs(name) else None
            same_name = canonical_name == canonical_root
            item["canonical_name"] = canonical_name
            item["identity_match_fields"] = {
                "checker_pid": same_process,
                "exact_root_pin_fd_number": same_fd,
                "root_pin_role": "root identity pin" if same_fd else None,
                "directory_type": same_type,
                "device": same_device,
                "inode": same_inode,
                "canonical_historical_root": same_name,
            }
            if all((same_process, same_fd, same_type, same_device, same_inode, same_name)):
                item["classification"] = "EXPECTED_IDENTITY_PIN"
                expected_matches += 1
            elif same_process:
                item["classification"] = "UNEXPECTED_SAME_PROCESS_OBJECT"
            else:
                item["classification"] = "FOREIGN_HOLDER"
        report["expected_pin_match_count"] = expected_matches

        revalidation: dict[str, Any] = {
            "named_root_after_scan": stat_at(root_parent_fd, root_name),
            "pinned_root_after_scan": fstat_identity(root_fd, with_fsid=True),
            "named_fd_same_device_inode_type": False,
            "root_matches_initial_pin": False,
            "parent_fd_identity_after_scan": parent_identity(root_parent_fd),
        }
        parent_path_fd_after = open_dir_nofollow(EXPECTED_PARENT)
        try:
            revalidation["parent_path_identity_after_scan"] = parent_identity(parent_path_fd_after)
        finally:
            os.close(parent_path_fd_after)
        named_after = revalidation["named_root_after_scan"]
        pinned_after = revalidation["pinned_root_after_scan"]
        revalidation["named_fd_same_device_inode_type"] = (
            (named_after["device"], named_after["inode"], named_after["type"])
            == (pinned_after["device"], pinned_after["inode"], pinned_after["type"])
        )
        revalidation["root_matches_initial_pin"] = (
            (named_after["device"], named_after["inode"], named_after["type"])
            == (named_before["device"], named_before["inode"], named_before["type"])
            and (pinned_after["device"], pinned_after["inode"], pinned_after["type"])
            == (pinned["device"], pinned["inode"], pinned["type"])
            and pinned_after["filesystem_id"] == pinned["filesystem_id"]
        )
        revalidation["parent_matches_initial_pin"] = (
            revalidation["parent_fd_identity_after_scan"] == current_parent_identity
            and revalidation["parent_path_identity_after_scan"] == current_parent_identity
        )
        for label, item in report["locks"].items():
            lock_fd = item["descriptor"]
            directory_fd = cache_fd if label == "allocation" else lease_fd
            current_fd = fstat_identity(lock_fd)
            current_named = stat_at(directory_fd, item["path_name"])
            item["after_scan"] = {
                "descriptor": current_fd,
                "named": current_named,
                "device_inode_stable": (
                    (current_fd["device"], current_fd["inode"])
                    == (item["opened"]["device"], item["opened"]["inode"])
                    and (current_named["device"], current_named["inode"])
                    == (current_fd["device"], current_fd["inode"])
                ),
            }
        report["root_revalidation"] = revalidation

        if process.returncode != 0:
            report["blockers"].append(f"lsof returned {process.returncode}")
        if stderr:
            report["blockers"].append("lsof emitted stderr; structured scan is unresolved")
        if not stream["complete"]:
            report["blockers"].append("structured lsof stream is incomplete or malformed")
        if not rows:
            report["blockers"].append("structured lsof scan returned no rows")
        if expected_matches != 1:
            report["blockers"].append(f"expected exactly one identity-pin row; observed {expected_matches}")
        for item in rows:
            if item["classification"] != "EXPECTED_IDENTITY_PIN":
                report["blockers"].append(
                    f"row {item.get('pid')}/{item.get('fd')} classified {item['classification']}"
                )
        if not revalidation["root_matches_initial_pin"] or not revalidation["named_fd_same_device_inode_type"]:
            report["blockers"].append("root named path or pin changed during lsof scan")
        if not revalidation["parent_matches_initial_pin"]:
            report["blockers"].append("approved parent identity changed during lsof scan")
        for label, item in report["locks"].items():
            if not item.get("after_scan", {}).get("device_inode_stable"):
                report["blockers"].append(f"{label} lock name/descriptor identity changed during lsof scan")
        if report["blockers"]:
            report["classification"] = "R23_LIVE_OPEN_BLOCKED"
        else:
            report["classification"] = "SELF_PIN_ONLY"
    except Exception as error:
        report["classification"] = "R23_LIVE_OPEN_BLOCKED"
        report["blockers"].append(f"capture failure: {type(error).__name__}: {error}")
    finally:
        report["finished_at_unix"] = time.time()
        try:
            report["script_sha256"] = hashlib.sha256(read_nofollow(__file__)).hexdigest()
        except Exception as error:
            report["script_sha256_error"] = f"{type(error).__name__}: {error}"
        for label, fd in reversed(list(opened.items())):
            try:
                if label in {"allocation_lock_fd", "retention_lock_fd"}:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError:
                pass
        if root_parent_fd is not None:
            try:
                os.close(root_parent_fd)
            except OSError:
                pass
        output_dir = os.path.dirname(OUTPUT_JSON)
        try:
            json_bytes = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
            write_new(output_dir, os.path.basename(OUTPUT_JSON), json_bytes)
            write_new(output_dir, os.path.basename(OUTPUT_MD), markdown(report))
        except Exception as error:
            print(f"diagnostic report write failed: {type(error).__name__}: {error}", file=sys.stderr)
            return 2
    print(report["classification"])
    if report["blockers"]:
        print("first_blocker=" + report["blockers"][0])
    return 0 if report.get("classification") == "SELF_PIN_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
