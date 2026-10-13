"""Index and bind per-process native traces for STEP 11D.3-B.

The host proxy writes one exclusive JSONL file per macOS PID.  This module
selects the supervised Wine PID by name and verifies that every row in every
trace belongs to the current target and experiment before the trace can be
used as evidence.  It deliberately returns metadata only; consumers that
need host events read the selected ``target_trace_path`` themselves.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping


class TraceProcessError(RuntimeError):
    """Raised when per-process host trace evidence is unsafe or incomplete."""

    def __init__(self, message: str, index: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.index = dict(index or {})


_TRACE_NAME = re.compile(r"^host-trace\.([1-9][0-9]*)\.jsonl$")
_TOKEN = re.compile(r"^[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PID_MAX = 2_147_483_647
_STARTUP_ROLES = {
    "wine", "wine_launcher", "wineserver", "winevulkan_dll", "winevulkan_so",
    "wine_host_loader", "winemac_so", "trace_proxy", "moltenvk",
}
_TARGET_STARTUP_EVENTS = {
    "process_identity", "vulkan_proxy_identity", "host_runtime_startup",
    "startup_module_identity",
}
_ACTIVITY_FRAME_EVENTS = {
    "next_drawable_begin", "next_drawable_end", "present_request",
    "presented_handler_registered", "presented_callback", "drawable_release",
}
_VK_SURFACE_ENDS = {"vkCreateMetalSurfaceEXT_end", "vkCreateMacOSSurfaceMVK_end"}


def _reject_duplicate_json_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value}")


def _is_int(value: Any) -> bool:
    return type(value) is int


def _pid(value: Any, label: str) -> int:
    if not _is_int(value) or value <= 0 or value > _PID_MAX:
        raise TraceProcessError(f"{label} must be a positive macOS PID")
    return value


def _canonical_existing_directory(path: Path, label: str) -> Path:
    try:
        absolute = Path(os.path.abspath(path))
        info = absolute.lstat()
        resolved = absolute.resolve(strict=True)
    except OSError as error:
        raise TraceProcessError(f"{label} is unavailable") from error
    if (stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode)
            or resolved != absolute):
        raise TraceProcessError(f"{label} is not a canonical real directory")
    if info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise TraceProcessError(f"{label} ownership or write permissions are unsafe")
    return absolute


def _add_error(index: dict[str, Any], message: str) -> None:
    if message not in index["errors"]:
        index["errors"].append(message)


def _write_index(run_root: Path, index: dict[str, Any]) -> None:
    """Atomically persist a private JSON index, replacing only its directory entry."""
    path = run_root / "trace-index.json"
    try:
        existing = path.lstat()
    except FileNotFoundError:
        existing = None
    except OSError as error:
        raise TraceProcessError("trace-index.json cannot be inspected", index) from error
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise TraceProcessError("trace-index.json is a symlink", index)

    temporary = run_root / f".trace-index.json.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(index, stream, indent=2, sort_keys=True, ensure_ascii=True,
                      allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        directory_fd = os.open(run_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except (OSError, TypeError, ValueError) as error:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise TraceProcessError("trace-index.json could not be persisted", index) from error


def _safe_file_bytes(path: Path, expected_uid: int) -> tuple[bytes, os.stat_result]:
    """Open without following symlinks and verify the opened object is the named file."""
    try:
        before = path.lstat()
    except OSError as error:
        raise TraceProcessError(f"trace {path.name} cannot be inspected") from error
    if (stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode)
            or before.st_uid != expected_uid or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_nlink != 1):
        raise TraceProcessError(f"trace {path.name} is not a private single-link regular file")

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if ((opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
                    or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or opened.st_uid != expected_uid
                    or stat.S_IMODE(opened.st_mode) != 0o600):
                raise TraceProcessError(f"trace {path.name} changed while opening")
            return stream.read(), opened
    except TraceProcessError:
        raise
    except OSError as error:
        raise TraceProcessError(f"trace {path.name} cannot be read") from error


def _parse_jsonl(data: bytes, filename: str) -> list[dict[str, Any]]:
    try:
        decoded = data.decode("utf-8", errors="strict")
    except UnicodeError as error:
        raise TraceProcessError(f"trace {filename} is not UTF-8") from error
    if not decoded or not decoded.endswith("\n"):
        raise TraceProcessError(f"trace {filename} is empty or lacks its final newline")

    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(decoded[:-1].split("\n"), start=1):
        if not raw or raw.endswith("\r"):
            raise TraceProcessError(f"trace {filename} has a blank or noncanonical line {line_number}")
        try:
            row = json.loads(raw, object_pairs_hook=_reject_duplicate_json_keys,
                             parse_constant=_reject_nonfinite)
        except (json.JSONDecodeError, ValueError) as error:
            raise TraceProcessError(f"trace {filename} has malformed JSON at line {line_number}") from error
        if not isinstance(row, dict):
            raise TraceProcessError(f"trace {filename} line {line_number} is not an object")
        if not isinstance(row.get("event"), str) or not row["event"]:
            raise TraceProcessError(f"trace {filename} line {line_number} has no event name")
        if not isinstance(row.get("origin"), str) or not row["origin"]:
            raise TraceProcessError(f"trace {filename} line {line_number} has no event origin")
        rows.append(row)
    if not rows:
        raise TraceProcessError(f"trace {filename} has no records")
    return rows


def _record_ownership_error(rows: Iterable[Mapping[str, Any]], file_pid: int,
                            target_pid: int, token: str) -> str | None:
    expected_role = "target" if file_pid == target_pid else "helper"
    for position, row in enumerate(rows, start=1):
        host_pid = row.get("host_pid")
        row_target_pid = row.get("target_unix_pid")
        if (not _is_int(host_pid) or host_pid != file_pid
                or not _is_int(row_target_pid) or row_target_pid != target_pid
                or row.get("experiment_token") != token
                or row.get("trace_role") != expected_role):
            return f"trace PID {file_pid} record {position} has conflicting ownership"
        event_id = row.get("event_id")
        if not _is_int(event_id) or event_id <= 0:
            return f"trace PID {file_pid} record {position} has an invalid event_id"
    return None


def _activity(rows: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    result = {"vulkan": 0, "surface": 0, "device": 0, "frame": 0}
    for row in rows:
        event = row.get("event")
        origin = row.get("origin")
        if (isinstance(event, str) and event.startswith("vk")
                and origin in {"WINE_SOURCE", "MVK_BOUNDARY"}):
            result["vulkan"] += 1
        if (event in _VK_SURFACE_ENDS and row.get("vk_result") == 0
                and _is_int(row.get("surface_id")) and row["surface_id"] > 0):
            result["surface"] += 1
        if any(_is_int(row.get(key)) and row[key] > 0
               for key in ("device_id", "queue_id")):
            result["device"] += 1
        if event in _ACTIVITY_FRAME_EVENTS:
            result["frame"] += 1
    return result


def _target_summary(rows: list[dict[str, Any]], file_pid: int,
                    errors: list[str]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    startup: dict[str, Any] = {}
    proxy: dict[str, Any] = {}
    host: dict[str, Any] = {}
    startup_id = f"pid-{file_pid}"
    specs = (
        ("clock_metadata", "INSTRUMENTATION"),
        ("process_identity", "INSTRUMENTATION"),
        ("vulkan_proxy_identity", "INSTRUMENTATION"),
        ("host_runtime_startup", "INSTRUMENTATION"),
        ("trace_summary", "LOGGER"),
    )
    selected: dict[str, dict[str, Any]] = {}
    for event, origin in specs:
        matches = [row for row in rows if row.get("event") == event]
        exact = [row for row in matches if row.get("origin") == origin]
        if len(exact) != 1 or len(matches) != 1:
            errors.append(f"target PID {file_pid} must have exactly one {event}")
        elif event != "clock_metadata" and event != "trace_summary" \
                and exact[0].get("startup_id") != startup_id:
            errors.append(f"target PID {file_pid} {event} is not bound to {startup_id}")
        else:
            selected[event] = exact[0]

    process = selected.get("process_identity", {})
    if process and (process.get("pid") != file_pid
                    or process.get("startup_id") != startup_id
                    or process.get("process_architecture") != "x86_64"
                    or process.get("process_translated") != 1):
        errors.append(f"target PID {file_pid} process identity is invalid")

    target_startup = selected.get("host_runtime_startup", {})
    if target_startup and (
            target_startup.get("startup_valid") is not True
            or target_startup.get("process_architecture") != "x86_64"
            or target_startup.get("process_translated") != 1
            or target_startup.get("moltenvk_version") != "MoltenVK v1.4.2"
            or target_startup.get("vulkan_runtime_version") != "1.4.357"):
        errors.append(f"target PID {file_pid} runtime startup identity or MoltenVK version is invalid")
    if any(row.get("event") in {"startup_identity_failure", "helper_delegate_identity",
                                 "helper_startup_skipped"} for row in rows):
        errors.append(f"target PID {file_pid} contains a helper or startup failure event")

    target_proxy = selected.get("vulkan_proxy_identity", {})
    if target_proxy:
        selected_proxy_path = target_proxy.get("trace_proxy_path")
        selected_proxy_sha = target_proxy.get("trace_proxy_sha256")
        loaded_proxy_path = target_proxy.get("loaded_proxy_path")
        loaded_proxy_sha = target_proxy.get("loaded_proxy_sha256")
        if (not isinstance(selected_proxy_path, str) or not selected_proxy_path.startswith("/")
                or not isinstance(selected_proxy_sha, str) or not _SHA256.fullmatch(selected_proxy_sha)
                or loaded_proxy_path != selected_proxy_path or loaded_proxy_sha != selected_proxy_sha
                or target_proxy.get("loaded_delegate_path") != target_startup.get("moltenvk_path")
                or target_proxy.get("loaded_delegate_sha256") != target_startup.get("moltenvk_sha256")):
            errors.append(f"target PID {file_pid} proxy/delegate identity is invalid")

    summary = selected.get("trace_summary", {})
    if summary:
        for key in ("dropped_count", "write_failure_count", "ring_capacity"):
            if not _is_int(summary.get(key)) or summary[key] < 0:
                errors.append(f"target PID {file_pid} trace summary has an invalid {key}")

    module_rows = [row for row in rows
                   if row.get("event") == "startup_module_identity"]
    roles: dict[str, dict[str, Any]] = {}
    for row in module_rows:
        role = row.get("role")
        if (row.get("origin") != "INSTRUMENTATION" or row.get("startup_id") != startup_id
                or not isinstance(role, str) or role not in _STARTUP_ROLES or role in roles):
            errors.append(f"target PID {file_pid} has an invalid or duplicate startup module identity")
            continue
        roles[role] = row
    if set(roles) != _STARTUP_ROLES:
        errors.append(f"target PID {file_pid} startup module identity set is incomplete")

    if process:
        startup = {
            "startup_id": startup_id,
            "process_identity": {
                key: process.get(key) for key in (
                    "pid", "process_architecture", "process_translated",
                    "main_executable_path", "main_executable_sha256", "process_start_monotonic_ns")
            },
            "module_roles": sorted(roles),
            "identity_rows_valid": set(roles) == _STARTUP_ROLES,
            "runtime_startup_valid": target_startup.get("startup_valid") is True,
        }
    if target_proxy:
        proxy = {key: target_proxy.get(key) for key in (
            "trace_proxy_path", "trace_proxy_sha256", "loaded_proxy_path",
            "loaded_proxy_sha256", "loaded_delegate_path", "loaded_delegate_sha256")}
    if target_startup:
        host = {key: target_startup.get(key) for key in (
            "moltenvk_path", "moltenvk_sha256", "moltenvk_version",
            "vulkan_runtime_version", "host_loader_role", "process_architecture",
            "process_translated", "loaded_moltenvk_count", "loaded_proxy_count",
            "loaded_winevulkan_count", "loaded_host_loader_count",
            "loaded_libvulkan_dylib_count")}
    return startup, proxy, host


def _helper_identity(rows: list[dict[str, Any]], file_pid: int,
                     errors: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    identities = [row for row in rows if row.get("event") == "helper_delegate_identity"]
    same_name = [row for row in rows if row.get("event") == "helper_delegate_identity"]
    if len(identities) != 1 or len(same_name) != 1:
        errors.append(f"helper PID {file_pid} must have exactly one delegate identity")
        return {}, {}
    row = identities[0]
    selected_path = row.get("moltenvk_path")
    selected_sha = row.get("moltenvk_sha256")
    loaded_path = row.get("loaded_delegate_path")
    loaded_sha = row.get("loaded_delegate_sha256")
    if (row.get("origin") != "INSTRUMENTATION"
            or row.get("startup_authorization") is not False
            or row.get("identity_match") is not True
            or row.get("moltenvk_runtime_version") != "1.4.2"
            or row.get("vulkan_runtime_version") != "1.4.357"
            or row.get("moltenvk_version") != "MoltenVK v1.4.2"
            or not isinstance(selected_path, str) or not selected_path.startswith("/")
            or not isinstance(selected_sha, str) or not _SHA256.fullmatch(selected_sha)
            or loaded_path != selected_path or loaded_sha != selected_sha):
        errors.append(f"helper PID {file_pid} delegate identity or MoltenVK version is invalid")
    forbidden = [row for row in rows if row.get("event") in _TARGET_STARTUP_EVENTS]
    if forbidden:
        errors.append(f"helper PID {file_pid} contains target-only startup evidence")
    if any(row.get("event") == "startup_identity_failure" for row in rows):
        errors.append(f"helper PID {file_pid} reported a startup identity failure")
    proxy = {key: row.get(key) for key in (
        "moltenvk_path", "moltenvk_sha256", "loaded_delegate_path",
        "loaded_delegate_sha256", "moltenvk_version", "moltenvk_runtime_version",
        "vulkan_runtime_version", "identity_match")}
    return {"startup_id": None, "helper_delegate_identity_valid": not any(
        message.startswith(f"helper PID {file_pid} delegate identity") for message in errors)}, proxy


def build_trace_index(run_dir: Path | str, target_unix_pid: int,
                      experiment_token: str) -> dict[str, Any]:
    """Validate and index all per-PID host traces; persist diagnostics even on failure.

    The returned dictionary is JSON-safe and contains no complete event arrays.
    If any trace is unsafe, malformed, misowned, or incomplete for the target,
    a failed index is written to ``run_dir/trace-index.json`` before raising
    :class:`TraceProcessError`.
    """
    target_pid = _pid(target_unix_pid, "target Unix PID")
    if not isinstance(experiment_token, str) or not _TOKEN.fullmatch(experiment_token):
        raise TraceProcessError("experiment token must be 32 lowercase hexadecimal characters")
    run_root = _canonical_existing_directory(Path(run_dir), "run root")
    index: dict[str, Any] = {
        "schema_version": 1,
        "status": "FAIL",
        "target_unix_pid": target_pid,
        "experiment_token": experiment_token,
        "host_trace_directory": str(run_root / "host-traces"),
        "target_trace_path": None,
        "target_trace_windows_path": None,
        "target_startup": {},
        "target_proxy": {},
        "target_host": {},
        "target_activity": {"vulkan": 0, "surface": 0, "device": 0, "frame": 0},
        "target_summary": {"state": "missing"},
        "processes": [],
        "errors": [],
    }
    host_root = run_root / "host-traces"
    try:
        host_root = _canonical_existing_directory(host_root, "host trace directory")
    except TraceProcessError as error:
        _add_error(index, str(error))
        _write_index(run_root, index)
        raise TraceProcessError("host trace directory is unsafe or missing", index) from error

    try:
        entries = sorted(os.scandir(host_root), key=lambda entry: entry.name)
    except OSError as error:
        _add_error(index, "host trace directory cannot be enumerated")
        _write_index(run_root, index)
        raise TraceProcessError("host trace directory cannot be enumerated", index) from error

    seen_pids: set[int] = set()
    for entry in entries:
        match = _TRACE_NAME.fullmatch(entry.name)
        if not match:
            _add_error(index, f"host trace directory contains a noncanonical entry {entry.name!r}")
            continue
        raw_pid = match.group(1)
        if len(raw_pid) > 10:
            _add_error(index, "host trace filename PID exceeds macOS pid_t")
            continue
        file_pid = int(raw_pid)
        if file_pid > _PID_MAX:
            _add_error(index, f"host trace filename PID {file_pid} exceeds macOS pid_t")
            continue
        if file_pid in seen_pids:
            _add_error(index, f"duplicate host trace filename PID {file_pid}")
            continue
        seen_pids.add(file_pid)
        path = host_root / entry.name
        process: dict[str, Any] = {
            "host_pid": file_pid,
            "target_unix_pid": target_pid,
            "trace_role": "target" if file_pid == target_pid else "helper",
            "path": str(path),
            "sha256": None,
            "size_bytes": None,
            "record_count": 0,
            "ownership_valid": False,
            "parse_status": "invalid",
            "startup_status": "unknown",
            "proxy": {},
            "host": {},
            "rosetta": {},
            "activity": {"vulkan": 0, "surface": 0, "device": 0, "frame": 0},
            "summary": {"state": "missing"},
        }
        index["processes"].append(process)
        if file_pid == target_pid:
            index["target_trace_path"] = str(path)
            index["target_trace_windows_path"] = "Z:" + str(path).replace("/", "\\")
        try:
            data, file_info = _safe_file_bytes(path, os.geteuid())
            process["size_bytes"] = file_info.st_size
            process["sha256"] = hashlib.sha256(data).hexdigest()
            rows = _parse_jsonl(data, entry.name)
            process["record_count"] = len(rows)
            process["parse_status"] = "valid"
            ownership_error = _record_ownership_error(rows, file_pid, target_pid, experiment_token)
            if ownership_error:
                _add_error(index, ownership_error)
            else:
                process["ownership_valid"] = True
            event_ids = [row["event_id"] for row in rows]
            if len(event_ids) != len(set(event_ids)):
                _add_error(index, f"trace PID {file_pid} has duplicate event_id values")

            activity = _activity(rows)
            process["activity"] = activity
            if file_pid == target_pid:
                index["target_activity"] = activity
                errors: list[str] = []
                startup, proxy, host = _target_summary(rows, file_pid, errors)
                process["startup_status"] = "complete" if not errors else "invalid"
                process["proxy"] = proxy
                process["host"] = host
                process["rosetta"] = {
                    "process_architecture": host.get("process_architecture"),
                    "process_translated": host.get("process_translated"),
                }
                index["target_startup"] = startup
                index["target_proxy"] = proxy
                index["target_host"] = host
                summaries = [row for row in rows if row.get("event") == "trace_summary"]
                if len(summaries) == 1:
                    row = summaries[0]
                    index["target_summary"] = {
                        "state": "present",
                        "dropped_count": row.get("dropped_count"),
                        "write_failure_count": row.get("write_failure_count"),
                        "ring_capacity": row.get("ring_capacity"),
                    }
                    process["summary"] = dict(index["target_summary"])
                else:
                    index["target_summary"] = {"state": "missing" if not summaries else "duplicate"}
                    process["summary"] = dict(index["target_summary"])
                for message in errors:
                    _add_error(index, message)
            else:
                errors = []
                startup, proxy = _helper_identity(rows, file_pid, errors)
                process["startup_status"] = "helper_only" if not errors else "invalid"
                process["proxy"] = proxy
                helper_summary = [row for row in rows if row.get("event") == "trace_summary"]
                if len(helper_summary) > 1:
                    errors.append(f"helper PID {file_pid} has duplicate trace summaries")
                elif helper_summary:
                    row = helper_summary[0]
                    for key in ("dropped_count", "write_failure_count", "ring_capacity"):
                        if not _is_int(row.get(key)) or row[key] < 0:
                            errors.append(f"helper PID {file_pid} trace summary has invalid {key}")
                    process["summary"] = {
                        "state": "present",
                        "dropped_count": row.get("dropped_count"),
                        "write_failure_count": row.get("write_failure_count"),
                        "ring_capacity": row.get("ring_capacity"),
                    }
                else:
                    process["summary"] = {"state": "missing"}
                process["host"] = {
                    "moltenvk_path": proxy.get("moltenvk_path"),
                    "moltenvk_sha256": proxy.get("moltenvk_sha256"),
                    "moltenvk_version": proxy.get("moltenvk_version"),
                    "vulkan_runtime_version": proxy.get("vulkan_runtime_version"),
                }
                process["rosetta"] = {"state": "not_target_authorized"}
                for message in errors:
                    _add_error(index, message)
                if errors:
                    process["startup_status"] = "invalid"
        except TraceProcessError as error:
            process["parse_status"] = "invalid"
            _add_error(index, str(error))

    index["processes"].sort(key=lambda process: process["host_pid"])
    if target_pid not in seen_pids:
        _add_error(index, f"target host trace for Unix PID {target_pid} is absent")
    if index["target_trace_path"] is None:
        index["target_summary"] = {"state": "missing"}
    index["status"] = "PASS" if not index["errors"] else "FAIL"
    _write_index(run_root, index)
    if index["errors"]:
        raise TraceProcessError("per-process host trace validation failed: "
                                + "; ".join(index["errors"]), index)
    return index


def validate_target_association(app_events: Iterable[Mapping[str, Any]],
                                index: Mapping[str, Any]) -> dict[str, Any]:
    """Bind APP_CONFIG to the indexed target and require target-only runtime activity."""
    if not isinstance(index, Mapping) or index.get("status") != "PASS":
        raise TraceProcessError("host trace index is absent or failed validation")
    target_pid = _pid(index.get("target_unix_pid"), "indexed target Unix PID")
    token = index.get("experiment_token")
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise TraceProcessError("host trace index experiment token is invalid")
    target_path = index.get("target_trace_path")
    target_wine_path = index.get("target_trace_windows_path")
    if (not isinstance(target_path, str) or not Path(target_path).is_absolute()
            or not isinstance(target_wine_path, str) or not target_wine_path.startswith("Z:\\")):
        raise TraceProcessError("host trace index target path is invalid")
    host_directory = index.get("host_trace_directory")
    expected_target_path = (Path(host_directory) / f"host-trace.{target_pid}.jsonl"
                            if isinstance(host_directory, str) else None)
    expected_wine_path = "Z:" + target_path.replace("/", "\\")
    processes = index.get("processes")
    if not isinstance(processes, list):
        raise TraceProcessError("host trace index process list is malformed")
    target_processes = [row for row in processes
                        if isinstance(row, Mapping) and row.get("host_pid") == target_pid]
    target_process = target_processes[0] if len(target_processes) == 1 else {}
    target_summary = target_process.get("summary")
    if (expected_target_path is None or target_path != str(expected_target_path)
            or target_wine_path != expected_wine_path or len(target_processes) != 1
            or target_process.get("trace_role") != "target"
            or target_process.get("ownership_valid") is not True
            or target_process.get("parse_status") != "valid"
            or target_process.get("startup_status") != "complete"
            or not isinstance(target_summary, Mapping)
            or target_summary.get("state") != "present"):
        raise TraceProcessError("host trace index target binding is incomplete or inconsistent")

    configs = [row for row in app_events
               if isinstance(row, Mapping) and row.get("origin") == "APP_CONFIG"
               and row.get("event") == "app_configuration"]
    if len(configs) != 1:
        raise TraceProcessError("expected exactly one APP_CONFIG app_configuration row")
    config = configs[0]
    if (not _is_int(config.get("target_unix_pid"))
            or config.get("target_unix_pid") != target_pid
            or config.get("experiment_token") != token
            or config.get("host_trace_path") != target_wine_path):
        raise TraceProcessError("APP_CONFIG does not identify the indexed target host trace")

    activity = index.get("target_activity")
    if not isinstance(activity, Mapping) or any(
            not _is_int(activity.get(key)) or activity[key] <= 0
            for key in ("vulkan", "surface", "device", "frame")):
        raise TraceProcessError("indexed target trace lacks Vulkan, surface, device, or frame activity")
    for key in ("device_id", "queue_id", "swapchain_id", "app_frames_started",
                "app_submits", "app_present_requests"):
        if not _is_int(config.get(key)) or config[key] <= 0:
            raise TraceProcessError(f"APP_CONFIG has no nonempty target {key} activity")

    return {
        "target_unix_pid": target_pid,
        "experiment_token": token,
        "host_trace_path": target_path,
        "activity": dict(activity),
        "app_config_process_id": config.get("process_id"),
        "status": "PASS",
    }

