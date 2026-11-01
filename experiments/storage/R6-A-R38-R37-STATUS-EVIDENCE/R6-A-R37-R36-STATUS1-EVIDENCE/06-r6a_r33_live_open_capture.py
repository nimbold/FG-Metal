#!/usr/bin/env python3
"""One fail-closed structured lsof capture for the R23 historical build root."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import selectors
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
OUTPUT_JSON = "experiments/storage/R6-A-R33-R23-LIVE-OPEN-20261021.json"
OUTPUT_MD = "experiments/storage/R6-A-R33-R23-LIVE-OPEN-20261021.md"
LSOF = "/usr/sbin/lsof"
LSOF_VERSION = "4.91"
LSOF_FIELDS = "0pcRftDrin"
LSOF_ARG = f"-F{LSOF_FIELDS}"
LSOF_TIMEOUT_SECONDS = 30.0
LSOF_STDOUT_LIMIT = 16 * 1024 * 1024
LSOF_STDERR_LIMIT = 1024 * 1024
MAX_PID = 2**31 - 1


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
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=5, env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CaptureError("checker process identity query incomplete") from error
    raw = result.stdout.decode("utf-8", "backslashreplace").strip()
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


def process_identity(pid: int) -> dict[str, Any]:
    """Capture the PID incarnation used to attribute a structured lsof row."""
    argv = ["/bin/ps", "-p", str(pid), "-o", "pid=", "-o", "ppid=", "-o", "lstart=", "-o", "command="]
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=5, env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CaptureError(f"process identity query failed for PID {pid}") from error
    raw = result.stdout.decode("utf-8", "backslashreplace").strip()
    parts = raw.split(None, 7)
    if result.returncode != 0 or len(parts) != 8 or int(parts[0]) != pid:
        raise CaptureError(f"process identity query incomplete for PID {pid}")
    return {
        "pid": int(parts[0]),
        "ppid": int(parts[1]),
        "start_identity": " ".join(parts[2:7]),
        "command": parts[7],
        "argv": argv,
        "returncode": result.returncode,
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


class LsofParseError(CaptureError):
    def __init__(self, message: str, rows: list[dict[str, Any]]):
        super().__init__(message)
        self.rows = rows


def _decimal(raw: bytes, label: str, *, minimum: int, maximum: int) -> int:
    if not raw or re.fullmatch(rb"[0-9]+", raw) is None:
        raise CaptureError(f"malformed {label} decimal field")
    value = int(raw, 10)
    if value < minimum or value > maximum:
        raise CaptureError(f"{label} field is outside the supported range")
    return value


def _fd_identity(raw: bytes) -> tuple[int | None, bytes | None]:
    match = re.fullmatch(rb"([0-9]+)([rwu]?)", raw)
    if match:
        return _decimal(match.group(1), "FD", minimum=0, maximum=MAX_PID), match.group(2) or None
    if raw in {b"cwd", b"rtd", b"txt", b"mem", b"DEL", b"err", b"NOFD"}:
        return None, None
    if raw and raw[:1].isdigit():
        raise CaptureError("malformed numeric FD field")
    if not raw or any(byte < 0x21 or byte > 0x7e for byte in raw):
        raise CaptureError("malformed special FD field")
    # Preserve unfamiliar nonnumeric lsof object descriptors as blocking rows.
    return None, None


def _finish_row(process: dict[str, bytes], fields: dict[str, bytes]) -> dict[str, Any]:
    if set(process) != {"p", "R", "c"}:
        raise CaptureError("process set lacks exactly p, R, and c fields")
    if set(fields) != {"f", "t", "D", "i", "n"}:
        raise CaptureError("file set lacks required fields or has an ambiguous device field")
    pid = _decimal(process["p"], "PID", minimum=1, maximum=MAX_PID)
    ppid = _decimal(process["R"], "PPID", minimum=0, maximum=MAX_PID)
    if not process["c"]:
        raise CaptureError("empty command field")
    fd_number, fd_suffix = _fd_identity(fields["f"])
    if fields["D"][:2].lower() != b"0x" or re.fullmatch(rb"0[xX][0-9a-fA-F]+", fields["D"]) is None:
        raise CaptureError("malformed lsof D device field")
    device = int(fields["D"][2:], 16)
    if device > 2**64 - 1:
        raise CaptureError("device field is outside the supported range")
    inode = _decimal(fields["i"], "inode", minimum=0, maximum=2**64 - 1)
    if not fields["t"] or not fields["n"]:
        raise CaptureError("empty type or name field")
    return {
        "pid": pid,
        "ppid": ppid,
        "command_raw": process["c"],
        "fd_raw": fields["f"],
        "fd_number": fd_number,
        "fd_suffix": fd_suffix,
        "fd_type_raw": fields["t"],
        "device_raw": fields["D"],
        "device": device,
        "inode_raw": fields["i"],
        "inode": inode,
        "name_raw": fields["n"],
        "parser_status": "PASS",
    }


def parse_lsof(raw: bytes) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Parse the installed lsof -F0 process/file grammar without text decoding."""
    if not isinstance(raw, bytes):
        raise TypeError("lsof parser requires raw bytes")
    rows: list[dict[str, Any]] = []
    process: dict[str, bytes] | None = None
    process_rows = 0
    file_fields: dict[str, bytes] | None = None
    state = "EXPECT_PROCESS"
    offset = 0
    field_ids = {ord("p"), ord("R"), ord("c"), ord("f"), ord("t"), ord("D"), ord("r"), ord("i"), ord("n")}

    def fail(message: str) -> None:
        raise LsofParseError(f"{message} at byte offset {offset}", rows.copy())

    try:
        while offset < len(raw):
            byte = raw[offset]
            if byte == 0x0a:
                if state == "PROCESS_HEADER":
                    if process is None or set(process) != {"p", "R", "c"}:
                        fail("incomplete process header at set boundary")
                    state = "EXPECT_FILE"
                    offset += 1
                    continue
                if state == "FILE_SET":
                    if process is None or file_fields is None:
                        fail("file-set boundary without process context")
                    try:
                        rows.append(_finish_row(process, file_fields))
                    except CaptureError as error:
                        fail(str(error))
                    process_rows += 1
                    file_fields = None
                    state = "EXPECT_FILE_OR_PROCESS"
                    offset += 1
                    continue
                fail("unexpected NL outside a process or file set")

            if byte == 0 or byte not in field_ids:
                fail(f"unknown structural byte 0x{byte:02x}")
            nul = raw.find(b"\0", offset + 1)
            if nul < 0:
                fail("truncated final NUL-terminated field")
            key = chr(byte)
            value = raw[offset + 1:nul]
            if key == "p":
                if state not in {"EXPECT_PROCESS", "EXPECT_FILE_OR_PROCESS"}:
                    fail("process field appeared before the previous sets closed")
                if state == "EXPECT_FILE_OR_PROCESS" and process_rows < 1:
                    fail("process set closed without a file set")
                process = {"p": value}
                process_rows = 0
                state = "PROCESS_HEADER"
            elif key in {"R", "c"}:
                if state != "PROCESS_HEADER" or process is None:
                    fail("process metadata appeared without process context")
                if key in process:
                    fail(f"duplicate process field {key}")
                process[key] = value
            elif key == "f":
                if state not in {"EXPECT_FILE", "EXPECT_FILE_OR_PROCESS"} or process is None:
                    fail("file set started without a closed process header")
                file_fields = {"f": value}
                state = "FILE_SET"
            else:
                if state != "FILE_SET" or file_fields is None:
                    fail(f"file field {key} appeared outside a file set")
                if key in file_fields:
                    fail(f"duplicate file field {key}")
                if key in {"D", "r"} and ({"D", "r"} & set(file_fields)):
                    fail("ambiguous duplicate D/r device representation")
                if key == "r":
                    fail("raw r device field is not an accepted identity encoding")
                file_fields[key] = value
            offset = nul + 1

        if state == "EXPECT_PROCESS":
            return [], {"complete": True, "empty": True, "row_count": 0, "bytes_consumed": 0}
        if state != "EXPECT_FILE_OR_PROCESS" or process is None or process_rows < 1 or file_fields is not None:
            fail("end of stream before a complete file-set boundary")
        return rows, {"complete": True, "empty": False, "row_count": len(rows), "bytes_consumed": offset}
    except LsofParseError:
        raise
    except CaptureError as error:
        fail(str(error))
    raise AssertionError("unreachable parser state")


def _safe_bytes(raw: bytes) -> str:
    return ascii(raw)


def row_for_report(row: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: _safe_bytes(value) if isinstance(value, bytes) else value
        for key, value in row.items()
    }
    result.update({
        "command": _safe_bytes(row["command_raw"]),
        "fd": _safe_bytes(row["fd_raw"]),
        "fd_type": _safe_bytes(row["fd_type_raw"]),
        "name": _safe_bytes(row["name_raw"]),
        "name_base64": base64.b64encode(row["name_raw"]).decode("ascii"),
        "name_bytes": len(row["name_raw"]),
    })
    return result


def executable_identity(path: str) -> dict[str, Any]:
    """Hash the exact no-follow lsof binary used by the diagnostic scan."""
    parent_fd = open_dir_nofollow(os.path.dirname(path))
    name = os.path.basename(path)
    fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode) or before.st_size <= 0 or before.st_size > 128 * 1024 * 1024:
            raise CaptureError("lsof executable is not a bounded regular file")
        digest = hashlib.sha256()
        total = 0
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            total += len(block)
        after = os.fstat(fd)
        named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        stable = (
            (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            == (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            == (named.st_dev, named.st_ino, named.st_size, named.st_mtime_ns)
        )
        if total != before.st_size or not stable:
            raise CaptureError("lsof executable changed while its identity was captured")
        return {
            "path": path,
            "device": before.st_dev,
            "inode": before.st_ino,
            "size": before.st_size,
            "mode": stat.S_IMODE(before.st_mode),
            "owner_uid": before.st_uid,
            "sha256": digest.hexdigest(),
            "stable": stable,
        }
    finally:
        os.close(fd)
        os.close(parent_fd)


def lsof_version() -> dict[str, Any]:
    argv = [LSOF, "-v"]
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=5, env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise CaptureError("lsof version query failed") from error
    text = result.stdout.decode("ascii", "replace")
    return {
        "argv": argv,
        "returncode": result.returncode,
        "stdout_base64": base64.b64encode(result.stdout).decode("ascii"),
        "stdout_sha256": hashlib.sha256(result.stdout).hexdigest(),
        "stderr_base64": base64.b64encode(result.stderr).decode("ascii"),
        "stderr_sha256": hashlib.sha256(result.stderr).hexdigest(),
        "text": text,
        "expected_version": LSOF_VERSION,
        "version_match": (
            result.returncode == 0
            and f"revision: {LSOF_VERSION}" in text
            and "configuration info: libproc-based" in text
        ),
    }


def run_lsof_bounded(argv: list[str], *, timeout: float = LSOF_TIMEOUT_SECONDS,
                     stdout_limit: int = LSOF_STDOUT_LIMIT,
                     stderr_limit: int = LSOF_STDERR_LIMIT) -> dict[str, Any]:
    """Run lsof with bounded byte capture, timeout, and child-incarnation evidence."""
    started = time.monotonic()
    deadline = started + timeout
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
        env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
    )
    result: dict[str, Any] = {
        "child_pid": proc.pid,
        "child_ppid": os.getpid(),
        "argv": argv,
        "stdout": bytearray(),
        "stderr": bytearray(),
        "stdout_hash": hashlib.sha256(),
        "stderr_hash": hashlib.sha256(),
        "stdout_total_bytes": 0,
        "stderr_total_bytes": 0,
        "stdout_capped": False,
        "stderr_capped": False,
        "timed_out": False,
    }
    try:
        result["child_identity"] = process_identity(proc.pid)
        if result["child_identity"]["ppid"] != os.getpid():
            raise CaptureError("lsof child PPID does not match the checker PID")
    except CaptureError as error:
        result["child_identity_error"] = str(error)

    def kill_child() -> None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass

    selector = selectors.DefaultSelector()
    assert proc.stdout is not None and proc.stderr is not None
    for label, stream, limit in (("stdout", proc.stdout, stdout_limit), ("stderr", proc.stderr, stderr_limit)):
        os.set_blocking(stream.fileno(), False)
        selector.register(stream, selectors.EVENT_READ, (label, limit))
    kill_at: float | None = None
    try:
        while selector.get_map():
            now = time.monotonic()
            if now >= deadline and kill_at is None:
                result["timed_out"] = True
                if proc.poll() is None:
                    kill_child()
                kill_at = now + 2.0
            if kill_at is not None and now >= kill_at:
                for key in list(selector.get_map().values()):
                    try:
                        selector.unregister(key.fileobj)
                    except Exception:
                        pass
                    try:
                        key.fileobj.close()
                    except Exception:
                        pass
                break
            if kill_at is not None or proc.poll() is not None:
                wait_for = 0.1
            else:
                wait_for = min(0.1, max(0.0, deadline - now))
            events = selector.select(wait_for)
            for key, _ in events:
                label, limit = key.data
                try:
                    chunk = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    key.fileobj.close()
                    continue
                result[f"{label}_total_bytes"] += len(chunk)
                result[f"{label}_hash"].update(chunk)
                buffer: bytearray = result[label]
                remaining = max(0, limit - len(buffer))
                if remaining:
                    buffer.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    result[f"{label}_capped"] = True
                    if proc.poll() is None:
                        kill_child()
                        kill_at = time.monotonic() + 2.0
        try:
            returncode = proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            kill_child()
            returncode = proc.wait(timeout=2)
            result["timed_out"] = True
    except BaseException:
        kill_child()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        raise
    finally:
        selector.close()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None and not stream.closed:
                stream.close()
    result.update({
        "returncode": returncode,
        "elapsed_seconds": round(time.monotonic() - started, 6),
        "stdout_sha256": result["stdout_hash"].hexdigest(),
        "stderr_sha256": result["stderr_hash"].hexdigest(),
        "stdout_bytes_captured": len(result["stdout"]),
        "stderr_bytes_captured": len(result["stderr"]),
        "stdout_captured_sha256": hashlib.sha256(result["stdout"]).hexdigest(),
        "stderr_captured_sha256": hashlib.sha256(result["stderr"]).hexdigest(),
        "stdout_base64": base64.b64encode(result["stdout"]).decode("ascii"),
        "stderr_base64": base64.b64encode(result["stderr"]).decode("ascii"),
        "stdout_raw": bytes(result["stdout"]),
        "stderr_raw": bytes(result["stderr"]),
    })
    result.pop("stdout_hash")
    result.pop("stderr_hash")
    result.pop("stdout")
    result.pop("stderr")
    return result


def scan_lsof_path(path: str, *, timeout: float = LSOF_TIMEOUT_SECONDS) -> dict[str, Any]:
    """Importable exact +D structured scan used by temporary-root regressions."""
    required = set("pcRftDrin")
    if LSOF_ARG != "-F0pcRftDrin" or not required.issubset(set(LSOF_ARG[3:])):
        raise CaptureError("structured lsof field selection differs from parser schema")
    argv = [LSOF, LSOF_ARG, "+D", path]
    capture = run_lsof_bounded(argv, timeout=timeout)
    try:
        rows, stream = parse_lsof(capture["stdout_raw"])
    except LsofParseError as error:
        rows = error.rows
        stream = {
            "complete": False,
            "empty": len(capture["stdout_raw"]) == 0,
            "row_count": len(rows),
            "parse_error": str(error),
        }
    capture["rows"] = rows
    capture["stream"] = stream
    capture["scan_complete"] = (
        stream.get("complete") is True
        and capture.get("returncode") == 0
        and capture.get("stderr_total_bytes") == 0
        and not capture.get("stdout_capped")
        and not capture.get("stderr_capped")
        and not capture.get("timed_out")
        and "child_identity_error" not in capture
    )
    return capture


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
    lsof = report.get("lsof", {})
    lines = [
        "# R23 live-open structured capture (R33)",
        "",
        f"- Classification: {report.get('classification')}",
        f"- Checker PID: {report.get('checker', {}).get('pid')}",
        f"- lsof child PID: {report.get('lsof', {}).get('child_pid')}",
        f"- Root pin FD: {report.get('root_pin', {}).get('fd')}",
        f"- Root identity: {report.get('root_pin', {}).get('fstat')}",
        f"- lsof return code: {lsof.get('returncode')}",
        f"- lsof executable SHA-256: {report.get('lsof_executable', {}).get('before', {}).get('sha256')}",
        f"- lsof version accepted: {report.get('lsof_executable', {}).get('version', {}).get('version_match')}",
        f"- Complete structured stream: {report.get('stream', {}).get('complete')}",
        f"- Complete scan contract: {report.get('stream', {}).get('scan_complete')}",
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
        f"- lsof stderr bytes: {lsof.get('stderr_total_bytes')}; SHA-256: {lsof.get('stderr_sha256')}; preview: {lsof.get('stderr_preview')}",
        f"- lsof stdout bytes: {lsof.get('stdout_total_bytes')}; SHA-256: {lsof.get('stdout_sha256')}; capture SHA-256: {lsof.get('stdout_captured_sha256')}",
        "- Captured lsof stdout and stderr bytes are base64 encoded in the JSON report.",
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
    report_write_failed = False
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

        executable_before = executable_identity(LSOF)
        version = lsof_version()
        report["lsof_executable"] = {"before": executable_before, "version": version}
        if (not version.get("version_match") or version.get("stderr_base64") != ""
                or executable_before.get("owner_uid") != 0
                or executable_before.get("mode", 0) & 0o022):
            raise CaptureError("installed lsof version is not the reviewed 4.91 build")

        capture = scan_lsof_path(canonical_root)
        stdout = capture.pop("stdout_raw")
        stderr = capture.pop("stderr_raw")
        rows = capture.pop("rows")
        stream = capture.pop("stream")
        report["lsof"] = {"launched": True, **capture, "stderr_preview": repr(stderr[:512])}
        report["stream"] = stream

        executable_after = executable_identity(LSOF)
        report["lsof_executable"]["after"] = executable_after
        report["lsof_executable"]["stable"] = executable_before == executable_after
        report["lsof_executable"]["accepted"] = (
            report["lsof_executable"]["stable"]
            and executable_before.get("owner_uid") == 0
            and executable_before.get("mode", 0) & 0o022 == 0
        )
        if not report["lsof_executable"]["accepted"]:
            report["blockers"].append("lsof executable identity or ownership changed/untrusted")

        try:
            checker_after = process_identity(report["checker"]["pid"])
            report["checker_after"] = checker_after
            checker_stable = (
                checker_after["pid"] == report["checker"]["pid"]
                and checker_after["ppid"] == report["checker"]["ppid"]
                and checker_after["start_identity"] == report["checker"]["start_identity"]
                and os.getpid() == report["checker"]["pid"]
                and os.getppid() == report["checker"]["ppid"]
            )
        except CaptureError as error:
            report["checker_after_error"] = str(error)
            checker_stable = False
        report["checker_identity_stable"] = checker_stable
        if not checker_stable:
            report["blockers"].append("checker PID/PPID/start identity changed or could not be revalidated")

        scan_complete = (
            stream.get("complete") is True
            and capture.get("returncode") == 0
            and capture.get("stderr_total_bytes") == 0
            and not capture.get("stdout_capped")
            and not capture.get("stderr_capped")
            and not capture.get("timed_out")
            and "child_identity_error" not in capture
            and report["lsof_executable"]["accepted"]
            and checker_stable
        )
        report["stream"]["scan_complete"] = scan_complete

        expected_matches = 0
        for item in rows:
            if item["parser_status"] != "PASS":
                item["classification"] = "MALFORMED_OR_UNRESOLVED"
                item["identity_match_fields"] = {}
                continue
            same_process = item["pid"] == report["checker"]["pid"]
            same_fd = item["fd_number"] == root_fd and item["fd_raw"] == str(root_fd).encode("ascii")
            same_type = item["fd_type_raw"] == b"DIR"
            same_device = item["device"] == pinned["device"]
            same_inode = item["inode"] == pinned["inode"]
            same_name = item["name_raw"] == os.fsencode(canonical_root)
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
        report["rows"] = [row_for_report(item) for item in rows]

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

        if capture.get("returncode") != 0:
            report["blockers"].append(f"lsof returned {capture.get('returncode')}")
        if stderr:
            report["blockers"].append("lsof emitted stderr; structured scan is unresolved")
        if not stream.get("complete"):
            report["blockers"].append("structured lsof stream is incomplete or malformed")
        if capture.get("timed_out"):
            report["blockers"].append("lsof exceeded the bounded scan timeout")
        if capture.get("stdout_capped") or capture.get("stderr_capped"):
            report["blockers"].append("lsof output exceeded the bounded capture limit")
        if "child_identity_error" in capture:
            report["blockers"].append("lsof child process identity could not be captured")
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
            json_bytes = (json.dumps(report, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
            write_new(output_dir, os.path.basename(OUTPUT_JSON), json_bytes)
            write_new(output_dir, os.path.basename(OUTPUT_MD), markdown(report))
        except Exception as error:
            print(f"diagnostic report write failed: {type(error).__name__}: {error}", file=sys.stderr)
            report_write_failed = True
    if report_write_failed:
        return 2
    print(report["classification"])
    if report["blockers"]:
        print("first_blocker=" + report["blockers"][0])
    return 0 if report.get("classification") == "SELF_PIN_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
