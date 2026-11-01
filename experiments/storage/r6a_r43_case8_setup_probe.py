#!/usr/bin/env python3
"""One-shot R43 diagnostic reproduction of frozen R41 case-8 setup only."""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import pathlib
import platform
import stat
import sys
import tempfile
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePath

REPO = Path(__file__).resolve().parents[2]
STORAGE = REPO / "experiments" / "storage"
PRESERVATION_MANIFEST = STORAGE / "R6-A-R43-R42-CASE8-UNRESOLVED-EVIDENCE.json"
FROZEN_DIR = STORAGE / "R6-A-R43-R42-UNRESOLVED-PRESERVED-20261021T191627Z-c369e629"
FROZEN_RUNNER = FROZEN_DIR / "r6a_r33_scanner_regression_runner.py"
FROZEN_SCANNER = FROZEN_DIR / "r6a_r33_live_open_capture.py"
ONE_SHOT = STORAGE / "R6-A-R43-CASE8-SETUP-REPRODUCTION-USED"
NO_PATH = object()
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
PREFIX = f"R6-A-R43-CASE8-SETUP-REPRODUCTION-{RUN_ID}"
JSON_OUT = STORAGE / f"{PREFIX}.json"
MD_OUT = STORAGE / f"{PREFIX}.md"
COMPLETE_OUT = STORAGE / f"{PREFIX}-COMPLETE.json"

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def typename(value) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"

def canonical_json(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")

def normalize(value):
    """R35 typed-tree normalizer with an explicit pathlib adapter."""
    if value is None:
        return {"tag": "null"}
    if type(value) is bool:
        return {"tag": "bool", "value": value}
    if type(value) is str:
        return {"tag": "str", "value": value}
    if type(value) is bytes:
        return {
            "tag": "bytes",
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
            "length": len(value),
            "sha256": hashlib.sha256(value).hexdigest(),
        }
    if type(value) is int:
        return {"tag": "int", "value": str(value)}
    if type(value) is float:
        if math.isnan(value):
            return {"tag": "float-special", "value": "nan"}
        if math.isinf(value):
            return {"tag": "float-special", "value": "-inf" if value < 0 else "inf"}
        return {"tag": "float", "hex": value.hex()}
    if type(value) is list:
        return {"tag": "list", "items": [normalize(item) for item in value]}
    if type(value) is tuple:
        return {"tag": "tuple", "items": [normalize(item) for item in value]}
    if type(value) is dict:
        pairs = [(normalize(key), normalize(item)) for key, item in value.items()]
        pairs.sort(key=lambda pair: canonical_json(pair[0]))
        return {"tag": "dict", "entries": [[key, item] for key, item in pairs]}
    if isinstance(value, PurePath):
        fs_value = os.fspath(value)
        return {"tag": "path", "path_class": typename(value), "fspath": normalize(fs_value)}
    raise TypeError(f"unsupported evidence value type: {typename(value)}")

def exception_record(exc, operation_id: str | None = None, include_traceback: bool = True) -> dict:
    has_filename = hasattr(exc, "filename")
    has_filename2 = hasattr(exc, "filename2")
    filename = getattr(exc, "filename", None) if has_filename else None
    filename2 = getattr(exc, "filename2", None) if has_filename2 else None
    lines = traceback.format_exception(type(exc), exc, exc.__traceback__) if include_traceback else None
    try:
        cwd = os.getcwd()
        cwd_bytes = os.getcwdb()
        cwd_error = None
    except BaseException as cwd_exc:
        cwd = None
        cwd_bytes = None
        cwd_error = {
            "type_name": type(cwd_exc).__name__,
            "args": cwd_exc.args,
            "filename": getattr(cwd_exc, "filename", None),
        }
    result = {
        "type_name": type(exc).__name__,
        "type_qualified_name": typename(exc),
        "errno": getattr(exc, "errno", None),
        "strerror": getattr(exc, "strerror", None),
        "args": exc.args,
        "args_repr": repr(exc.args),
        "filename_present": has_filename,
        "filename": filename,
        "filename_repr": repr(filename) if has_filename else "<ABSENT>",
        "filename_type": typename(filename) if has_filename and filename is not None else (None if has_filename else "<ABSENT>"),
        "filename2_present": has_filename2,
        "filename2": filename2,
        "filename2_repr": repr(filename2) if has_filename2 else "<ABSENT>",
        "filename2_type": typename(filename2) if has_filename2 and filename2 is not None else (None if has_filename2 else "<ABSENT>"),
        "cwd": cwd,
        "cwd_bytes": cwd_bytes,
        "cwd_error": cwd_error,
        "operation_id": operation_id,
    }
    if include_traceback:
        result["traceback"] = "".join(lines)
        result["traceback_lines"] = lines
    return result

def fspath_info(value):
    if value is NO_PATH:
        return {"status": "not_applicable"}
    try:
        fs_value = os.fspath(value)
        return {
            "status": "ok",
            "runtime_type": typename(fs_value),
            "repr": repr(fs_value),
            "value": fs_value,
        }
    except BaseException as exc:
        return {"status": "error", "exception": exception_record(exc, include_traceback=True)}

def stat_fields(info) -> dict:
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "mode": info.st_mode,
        "file_type": stat.S_IFMT(info.st_mode),
        "size": info.st_size,
        "nlink": info.st_nlink,
        "uid": info.st_uid,
        "gid": info.st_gid,
    }

def identity(info):
    if info is None:
        return None
    return (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))

def stat_observation(path, follow_symlinks: bool, label: str) -> dict:
    try:
        info = os.stat(path, follow_symlinks=follow_symlinks)
        return {
            "status": "ok",
            "label": label,
            "follow_symlinks": follow_symlinks,
            "fields": stat_fields(info),
            "identity": identity(info),
            "is_directory": stat.S_ISDIR(info.st_mode),
            "is_regular": stat.S_ISREG(info.st_mode),
        }
    except BaseException as exc:
        return {
            "status": "error",
            "label": label,
            "follow_symlinks": follow_symlinks,
            "exception": exception_record(exc, include_traceback=False),
        }

def lstat_observation(path) -> dict:
    try:
        info = os.lstat(path)
        return {
            "status": "ok",
            "fields": stat_fields(info),
            "identity": identity(info),
            "is_directory": stat.S_ISDIR(info.st_mode),
            "is_regular": stat.S_ISREG(info.st_mode),
        }
    except BaseException as exc:
        return {"status": "error", "exception": exception_record(exc, include_traceback=False)}

def path_snapshot(path, held_fd=None) -> dict:
    if path is None:
        return {"status": "not_yet_known"}
    fs = fspath_info(path)
    snapshot = {
        "status": "observed",
        "path_raw": path,
        "path_runtime_type": typename(path),
        "path_repr": repr(path),
        "path_fspath": fs,
        "exists": None,
        "lstat": None,
        "stat": None,
        "canonical_path": None,
        "canonical_path_bytes": None,
        "canonical_stat": None,
        "named_canonical_identity_match": None,
        "is_directory": None,
        "held_descriptor": {"status": "not_applicable"},
    }
    if fs["status"] != "ok":
        snapshot["status"] = "path_conversion_error"
    else:
        fs_path = fs["value"]
        snapshot["lstat"] = lstat_observation(fs_path)
        snapshot["stat"] = stat_observation(fs_path, True, "stat_following_final_component")
        if snapshot["lstat"]["status"] == "ok":
            snapshot["exists"] = True
            snapshot["is_directory"] = snapshot["lstat"]["is_directory"]
        else:
            eno = snapshot["lstat"]["exception"].get("errno")
            snapshot["exists"] = False if eno == 2 else None
        try:
            canonical = os.path.realpath(fs_path, strict=True)
            canonical_bytes = os.fsencode(canonical)
            snapshot["canonical_path"] = canonical
            snapshot["canonical_path_bytes"] = canonical_bytes
            snapshot["canonical_stat"] = stat_observation(canonical, False, "canonical_path_stat")
            if snapshot["lstat"]["status"] == "ok" and snapshot["canonical_stat"]["status"] == "ok":
                snapshot["named_canonical_identity_match"] = (
                    snapshot["lstat"]["identity"] == snapshot["canonical_stat"]["identity"]
                )
        except BaseException as exc:
            snapshot["canonicalization_error"] = exception_record(exc, include_traceback=False)
    if held_fd is not None:
        try:
            info = os.fstat(held_fd)
            snapshot["held_descriptor"] = {
                "status": "ok",
                "fd": held_fd,
                "fields": stat_fields(info),
                "identity": identity(info),
            }
        except BaseException as exc:
            snapshot["held_descriptor"] = {
                "status": "error",
                "fd": held_fd,
                "exception": exception_record(exc, include_traceback=False),
            }
    return snapshot

def cwd_record() -> dict:
    try:
        return {"str": os.getcwd(), "bytes": os.getcwdb(), "status": "ok"}
    except BaseException as exc:
        return {"status": "error", "exception": exception_record(exc, include_traceback=False)}

def identity_from_snapshot(snapshot):
    obs = snapshot.get("lstat") if isinstance(snapshot, dict) else None
    return obs.get("identity") if isinstance(obs, dict) and obs.get("status") == "ok" else None

def stability(anchor, snapshot):
    current = identity_from_snapshot(snapshot)
    return {
        "anchor_status": "not_established" if anchor is None else "established",
        "current_identity": current,
        "anchor_identity": anchor,
        "matches_anchor": None if anchor is None or current is None else current == anchor,
    }

STATE = {
    "manager": None,
    "suite_root": None,
    "case8_path": None,
    "child_path": None,
    "root_fd": None,
    "child_fd": None,
    "suite_root_anchor": None,
    "case8_anchor": None,
}
OPERATIONS = []
FAILURE = None
PROBE_ERRORS = []

def safe_result_value(value):
    if isinstance(value, tempfile.TemporaryDirectory):
        return {"object_type": typename(value), "name": value.name}
    if isinstance(value, os.stat_result):
        return {"tuple_value": tuple(value), "fields": stat_fields(value)}
    return value

def run_operation(
    operation_id: str,
    description: str,
    function_name: str,
    source: str,
    path_argument=NO_PATH,
    parent_path=None,
    call_arguments=None,
    action=None,
    on_result=None,
):
    global FAILURE
    started = datetime.now(timezone.utc)
    started_ns = time.monotonic_ns()
    parent_fd = STATE["root_fd"] if parent_path == STATE["case8_path"] else None
    before_root = path_snapshot(STATE["suite_root"])
    before_case8 = path_snapshot(STATE["case8_path"], STATE["root_fd"])
    before_parent = path_snapshot(parent_path, parent_fd)
    record = {
        "operation_id": operation_id,
        "description": description,
        "function": function_name,
        "source": source,
        "started_at_utc": started.isoformat(),
        "started_monotonic_ns": started_ns,
        "cwd_before": cwd_record(),
        "path_argument": None if path_argument is NO_PATH else path_argument,
        "path_argument_runtime_type": None if path_argument is NO_PATH else typename(path_argument),
        "path_argument_repr": None if path_argument is NO_PATH else repr(path_argument),
        "path_fspath": fspath_info(path_argument),
        "parent_path": parent_path,
        "parent_path_runtime_type": None if parent_path is None else typename(parent_path),
        "parent_path_repr": None if parent_path is None else repr(parent_path),
        "call_arguments": call_arguments or {},
        "parent_exists_before": before_parent.get("exists") if isinstance(before_parent, dict) else None,
        "parent_identity_before": before_parent,
        "operation_parent_identity_before": before_parent,
        "suite_root_before": before_root,
        "case_8_before": before_case8,
        "suite_root_stability_before": stability(STATE["suite_root_anchor"], before_root),
        "case_8_stability_before": stability(STATE["case8_anchor"], before_case8),
        "operation_result": None,
        "exception_structured": None,
        "operation_observation": None,
        "descriptor_identity_immediate": None,
    }
    raised = None
    result = None
    try:
        result = action()
        record["operation_result"] = {
            "status": "returned",
            "value": safe_result_value(result),
            "runtime_type": typename(result),
            "repr": repr(result),
        }
        if on_result is not None:
            try:
                observation = on_result(result)
                record["operation_observation"] = observation
                if isinstance(observation, dict) and "fd" in observation and "identity" in observation:
                    record["descriptor_identity_immediate"] = observation
            except BaseException as observer_exc:
                observer_data = exception_record(observer_exc, operation_id, include_traceback=True)
                record["operation_observation"] = {"status": "observer_error", "exception": observer_data}
                PROBE_ERRORS.append({"operation_id": operation_id, "kind": "immediate_descriptor_observer", "exception": observer_data})
                FAILURE = FAILURE or {"operation_id": operation_id, "type_name": type(observer_exc).__name__, "kind": "instrumentation_observer"}
    except BaseException as exc:
        raised = exc
        record["operation_result"] = {"status": "raised"}
        record["exception_structured"] = exception_record(exc, operation_id, include_traceback=True)
        FAILURE = FAILURE or {"operation_id": operation_id, "type_name": type(exc).__name__, "kind": "setup_operation"}
    after_root = path_snapshot(STATE["suite_root"])
    after_case8 = path_snapshot(STATE["case8_path"], STATE["root_fd"])
    after_parent = path_snapshot(parent_path, parent_fd)
    finished = datetime.now(timezone.utc)
    finished_ns = time.monotonic_ns()
    record.update({
        "parent_exists_after": after_parent.get("exists") if isinstance(after_parent, dict) else None,
        "parent_identity_after": after_parent,
        "operation_parent_identity_after": after_parent,
        "suite_root_after": after_root,
        "case_8_after": after_case8,
        "suite_root_stability_after": stability(STATE["suite_root_anchor"], after_root),
        "case_8_stability_after": stability(STATE["case8_anchor"], after_case8),
        "cwd_after": cwd_record(),
        "finished_at_utc": finished.isoformat(),
        "finished_monotonic_ns": finished_ns,
    })
    OPERATIONS.append(record)
    return result, raised, record

def opendir(path):
    return os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))

def record_immediate_fd(fd):
    info = os.fstat(fd)
    return {"status": "ok", "fd": fd, "fields": stat_fields(info), "identity": identity(info)}

def register_temp_manager(manager):
    STATE["manager"] = manager
    STATE["suite_root"] = manager.name
    return {"status": "created", "name": manager.name, "name_type": typename(manager.name)}

def register_roots(roots):
    STATE["case8_path"] = roots[8]
    return {"status": "constructed", "count": len(roots), "case8_path": roots[8]}

def construct_roots(temp):
    return {i: str(Path(temp) / f"case_{i}") for i in range(1, 17)}

def construct_child_path(root):
    return str(Path(root) / "child\nline.bin")

def register_child_path(path):
    STATE["child_path"] = path
    base = os.path.basename(path)
    return {
        "path": path,
        "basename": base,
        "basename_bytes": os.fsencode(base),
        "lf_character_count": base.count("\n"),
        "lf_byte_count": os.fsencode(base).count(b"\n"),
    }

def register_root_fd(fd):
    STATE["root_fd"] = fd
    return record_immediate_fd(fd)

def register_child_fd(fd):
    STATE["child_fd"] = fd
    return record_immediate_fd(fd)

def require_absolute(path):
    if not os.path.isabs(path):
        raise ValueError("path is not absolute")
    return True

def publish(path: Path, data: bytes):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb", closefd=False) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(fd)
    finally:
        os.close(fd)

def render_markdown(report) -> str:
    lines = [
        "# R43 Case-8 Setup Reproduction",
        "",
        f"Run ID: {report['run_id']}",
        "",
        f"**Result: {report['result']}**",
        "",
        "## Execution boundary",
        "",
        "- Scanner invocations: 0",
        "- lsof invocations: 0",
        "- Helper processes started: 0",
        "- Cases 1–7 replayed: no; those cases require scanner or helper execution, outside this setup-only reproduction.",
        "- The probe stopped at the first setup failure. It did not retry.",
        "",
        "## Operation records",
        "",
        "| Operation ID | Function | Result | Parent before | Parent after | Path argument |",
        "|---|---|---|---|---|---|",
    ]
    for operation in report.get("operations", []):
        result = operation.get("operation_result", {}).get("status", "unknown")
        parent_before = operation.get("parent_exists_before")
        parent_after = operation.get("parent_exists_after")
        path_repr = operation.get("path_argument_repr") or "n/a"
        printable = path_repr.replace("|", "&#124;").replace(chr(10), "&#10;")
        lines.append(
            f"| {operation['operation_id']} | {operation['function']} | {result} | "
            f"{parent_before} | {parent_after} | {printable} |"
        )
    lines.extend(["", "## Failure evidence", ""])
    if report.get("failure") is None:
        lines.append("HISTORICAL_FAILURE_NOT_REPRODUCED — all original pre-scan setup operations completed once.")
    else:
        lines.append(f"First failing operation: {report.get('first_failing_operation_id')}.")
        failed = next((op for op in report["operations"] if op["operation_id"] == report["first_failing_operation_id"]), None)
        if failed and failed.get("exception_structured"):
            exc = failed["exception_structured"]
            lines.extend([
                "",
                f"- Type: {exc['type_qualified_name']}",
                f"- errno: {exc.get('errno')!r}",
                f"- strerror: {exc.get('strerror')!r}",
                f"- filename repr/type: {exc.get('filename_repr')} / {exc.get('filename_type')}",
                f"- filename2 repr/type: {exc.get('filename2_repr')} / {exc.get('filename2_type')}",
                "",
                "The complete traceback, exact typed exception attributes, and raw bytes evidence are in the R35 typed JSON artifact.",
            ])
    lines.extend([
        "",
        "## Instrumentation note",
        "",
        "The probe preserves the frozen source path expression, payload, call order, and filesystem APIs. It adds before/after read-only lstat, stat, strict realpath, and fstat observations around named steps. It does not replay cases 1–7 because R43 prohibits scanner/lsof execution and helper startup is outside the failing setup statement. If the failure does not reproduce, these observation and sequence differences require differential review before any cause is assigned.",
        "",
    ])
    return "\n".join(lines)

def main() -> int:
    global FAILURE
    if not PRESERVATION_MANIFEST.is_file():
        raise RuntimeError(f"missing evidence freeze: {PRESERVATION_MANIFEST}")
    lock_fd = os.open(ONE_SHOT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(lock_fd, "w", encoding="ascii") as guard:
        guard.write(RUN_ID + "\n")
        guard.flush()
        os.fsync(guard.fileno())

    report = {
        "schema": "r6-a-r43-case8-setup-reproduction-v1",
        "run_id": RUN_ID,
        "started_at_utc": utc_now(),
        "host": {
            "platform": platform.platform(),
            "python_version": sys.version,
            "cwd": os.getcwd(),
            "cwd_bytes": os.getcwdb(),
        },
        "source_identity": {
            "frozen_runner_sha256": hashlib.sha256(FROZEN_RUNNER.read_bytes()).hexdigest(),
            "frozen_scanner_sha256": hashlib.sha256(FROZEN_SCANNER.read_bytes()).hexdigest(),
            "probe_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "preservation_manifest_sha256": hashlib.sha256(PRESERVATION_MANIFEST.read_bytes()).hexdigest(),
        },
        "execution_boundary": {
            "fresh_disposable_suite_root": True,
            "lsof_invocations": 0,
            "scanner_invocations": 0,
            "helper_processes_started": 0,
            "cases_1_through_7_replayed": False,
            "cases_1_through_7_omission_reason": "They invoke scanner/lsof or start helper holders; R43 forbids lsof and helper startup is not in the case-8 setup statement.",
            "fixture_semantics_changed": False,
            "intended_basename_source_expression": "child\nline.bin",
            "intended_basename_bytes_hex": "6368696c640a6c696e652e62696e",
            "instrumentation": "read-only root/parent identity observations before and after every named setup operation; no replacement API",
        },
        "operations": OPERATIONS,
        "probe_errors": PROBE_ERRORS,
    }

    temp_parent = tempfile.gettempdir()
    manager_result = run_operation(
        "suite.tempdir.construct",
        "Construct the original TemporaryDirectory; its constructor creates the disposable root.",
        "tempfile.TemporaryDirectory",
        "frozen runner line 567: tempfile.TemporaryDirectory(prefix='r40-scanner-regressions-')",
        parent_path=temp_parent,
        call_arguments={"prefix": "r40-scanner-regressions-"},
        action=lambda: tempfile.TemporaryDirectory(prefix="r40-scanner-regressions-"),
        on_result=register_temp_manager,
    )
    manager = manager_result[0]
    if manager_result[1] is None:
        STATE["suite_root_anchor"] = identity_from_snapshot(manager_result[2]["suite_root_after"])
        enter_result = run_operation(
            "suite.tempdir.enter",
            "Enter the original TemporaryDirectory context and receive the str root path.",
            "TemporaryDirectory.__enter__",
            "frozen runner line 567: with tempfile.TemporaryDirectory(...) as temp",
            parent_path=temp_parent,
            call_arguments={"manager_name": manager.name},
            action=manager.__enter__,
        )
        temp = enter_result[0]
        if enter_result[1] is None and FAILURE is None:
            roots_result = run_operation(
                "suite.case_roots.construct",
                "Build the exact frozen roots dict comprehension.",
                "Path / f-string / str comprehension",
                "frozen runner line 568: roots={i:str(Path(temp)/f'case_{i}') for i in range(1,17)}",
                path_argument=temp,
                parent_path=temp,
                call_arguments={"range_start": 1, "range_stop_exclusive": 17},
                action=lambda: construct_roots(temp),
                on_result=register_roots,
            )
            roots = roots_result[0]
            if roots_result[1] is None and FAILURE is None:
                for index, path in roots.items():
                    mkdir_result = run_operation(
                        f"suite.case_root.mkdir[{index}]",
                        f"Create case_{index} exactly as the original os.mkdir loop does.",
                        "os.mkdir",
                        "frozen runner line 569: for p in roots.values(): os.mkdir(p)",
                        path_argument=path,
                        parent_path=temp,
                        call_arguments={"path": path},
                        action=lambda path=path: os.mkdir(path),
                    )
                    if index == 8 and mkdir_result[1] is None:
                        STATE["case8_anchor"] = identity_from_snapshot(mkdir_result[2]["case_8_after"])
                    if mkdir_result[1] is not None or FAILURE is not None:
                        break

            if FAILURE is None:
                path_result = run_operation(
                    "case8.child_path.construct",
                    "Evaluate the exact case-8 child path expression, preserving its LF.",
                    "Path / and str",
                    "frozen runner line 682: child8=str(Path(roots[8])/'child\\nline.bin')",
                    path_argument=roots[8],
                    parent_path=roots[8],
                    call_arguments={"basename_source_literal": "child\nline.bin"},
                    action=lambda: construct_child_path(roots[8]),
                    on_result=register_child_path,
                )
                child8 = path_result[0]
                if path_result[1] is None and FAILURE is None:
                    path_object_result = run_operation(
                        "case8.child_path_object.construct",
                        "Evaluate Path(child8) as the first expression in Path(child8).write_bytes(b'x').",
                        "pathlib.Path",
                        "frozen runner line 682: Path(child8).write_bytes(b'x')",
                        path_argument=child8,
                        parent_path=roots[8],
                        call_arguments={"path": child8},
                        action=lambda: Path(child8),
                    )
                    child_path_object = path_object_result[0]
                    if path_object_result[1] is None and FAILURE is None:
                        write_result = run_operation(
                            "case8.child.write_bytes",
                            "Call the original Path.write_bytes(b'x') without changing the path or payload.",
                            "pathlib.Path.write_bytes",
                            "frozen runner line 682: Path(child8).write_bytes(b'x')",
                            path_argument=child_path_object,
                            parent_path=roots[8],
                            call_arguments={"data": b"x"},
                            action=lambda: child_path_object.write_bytes(b"x"),
                        )
                        if write_result[1] is None and FAILURE is None:
                            root_open_result = run_operation(
                                "case8.root_pin.open",
                                "Call the original opendir helper for roots[8].",
                                "opendir -> os.open",
                                "frozen runner line 682; helper line 383: opendir(roots[8])",
                                path_argument=roots[8],
                                parent_path=temp,
                                call_arguments={"path": roots[8], "flags": os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)},
                                action=lambda: opendir(roots[8]),
                                on_result=register_root_fd,
                            )
                            root_fd = root_open_result[0]
                            if root_open_result[1] is None and FAILURE is None:
                                child_open_result = run_operation(
                                    "case8.child_fd.open",
                                    "Open the created child for reading using the original os.open call.",
                                    "os.open",
                                    "frozen runner line 682: os.open(child8, os.O_RDONLY)",
                                    path_argument=child8,
                                    parent_path=roots[8],
                                    call_arguments={"path": child8, "flags": os.O_RDONLY},
                                    action=lambda: os.open(child8, os.O_RDONLY),
                                    on_result=register_child_fd,
                                )
                                child_fd = child_open_result[0]
                                if child_open_result[1] is None and FAILURE is None:
                                    encode_result = run_operation(
                                        "case8.child.path_evidence.fsencode",
                                        "Encode the child str path exactly as path_evidence does.",
                                        "os.fsencode",
                                        "frozen runner line 684; helper lines 402-404",
                                        path_argument=child8,
                                        parent_path=roots[8],
                                        call_arguments={"path": child8},
                                        action=lambda: os.fsencode(child8),
                                    )
                                    raw_child = encode_result[0]
                                    if encode_result[1] is None and FAILURE is None:
                                        absolute_result = run_operation(
                                            "case8.child.canonicalize.validate_absolute",
                                            "Apply the frozen canonical_path absolute-path check.",
                                            "os.path.isabs",
                                            "frozen runner helper lines 385-390",
                                            path_argument=raw_child,
                                            parent_path=roots[8],
                                            call_arguments={"path": raw_child},
                                            action=lambda: require_absolute(raw_child),
                                        )
                                        if absolute_result[1] is None and FAILURE is None:
                                            canonical_result = run_operation(
                                                "case8.child.path_evidence.canonicalize",
                                                "Run strict realpath on the fsencoded bytes path.",
                                                "os.path.realpath(strict=True)",
                                                "frozen runner line 684; helpers lines 385-390 and 402-407",
                                                path_argument=raw_child,
                                                parent_path=roots[8],
                                                call_arguments={"path": raw_child, "strict": True},
                                                action=lambda: os.path.realpath(raw_child, strict=True),
                                            )
                                            canonical_child = canonical_result[0]
                                            if canonical_result[1] is None and FAILURE is None:
                                                b64 = base64.b64encode(canonical_child).decode("ascii")
                                                decode_result = run_operation(
                                                    "case8.child.canonical_b64_decode",
                                                    "Decode the canonical-path Base64 field as the original statement does.",
                                                    "base64.b64decode",
                                                    "frozen runner line 684",
                                                    parent_path=roots[8],
                                                    call_arguments={"data": b64},
                                                    action=lambda: base64.b64decode(b64),
                                                )
                                                if decode_result[1] is None and FAILURE is None:
                                                    named_result = run_operation(
                                                        "case8.child.named_stat_before",
                                                        "Capture the original no-follow named child stat before scanning.",
                                                        "os.stat(follow_symlinks=False)",
                                                        "frozen runner line 685",
                                                        path_argument=child8,
                                                        parent_path=roots[8],
                                                        call_arguments={"path": child8, "follow_symlinks": False},
                                                        action=lambda: os.stat(child8, follow_symlinks=False),
                                                    )
                                                    named_info = named_result[0]
                                                    if named_result[1] is None and FAILURE is None:
                                                        child_fd_result = run_operation(
                                                            "case8.child.fd_stat_before_scan",
                                                            "Capture the original os.fstat(cf) before scanning.",
                                                            "os.fstat",
                                                            "frozen runner line 685",
                                                            parent_path=roots[8],
                                                            call_arguments={"fd": child_fd},
                                                            action=lambda: os.fstat(child_fd),
                                                        )
                                                        fd_info = child_fd_result[0]
                                                        if child_fd_result[1] is None and FAILURE is None:
                                                            canonical_stat_result = run_operation(
                                                                "case8.child.canonical_stat_before_scan",
                                                                "Record the canonical path identity before any scanner call.",
                                                                "os.stat(follow_symlinks=False)",
                                                                "R43 required pre-scan identity evidence",
                                                                path_argument=canonical_child,
                                                                parent_path=roots[8],
                                                                call_arguments={"path": canonical_child, "follow_symlinks": False},
                                                                action=lambda: os.stat(canonical_child, follow_symlinks=False),
                                                            )
                                                            canonical_info = canonical_stat_result[0]
                                                            if canonical_stat_result[1] is None and FAILURE is None:
                                                                report["pre_scan_identity"] = {
                                                                    "child_path": child8,
                                                                    "child_path_repr": repr(child8),
                                                                    "child_basename": os.path.basename(child8),
                                                                    "child_basename_bytes": os.fsencode(os.path.basename(child8)),
                                                                    "lf_character_count": os.path.basename(child8).count("\n"),
                                                                    "lf_byte_count": os.fsencode(os.path.basename(child8)).count(b"\n"),
                                                                    "child_stat": stat_fields(named_info),
                                                                    "child_fd_stat": stat_fields(fd_info),
                                                                    "canonical_child_path": canonical_child,
                                                                    "canonical_child_bytes": os.fsencode(canonical_child),
                                                                    "canonical_child_stat": stat_fields(canonical_info),
                                                                    "named_fd_identity_equal": identity(named_info) == identity(fd_info),
                                                                    "named_canonical_identity_equal": identity(named_info) == identity(canonical_info),
                                                                    "child_is_regular": stat.S_ISREG(named_info.st_mode),
                                                                    "root_fd": root_fd,
                                                                    "root_fd_identity_immediate": root_open_result[2]["descriptor_identity_immediate"],
                                                                    "child_fd": child_fd,
                                                                    "child_fd_identity_immediate": child_open_result[2]["descriptor_identity_immediate"],
                                                                    "scanner_or_lsof_invoked": False,
                                                                }

    report["operations"] = OPERATIONS
    report["probe_errors"] = PROBE_ERRORS
    report["failure"] = FAILURE
    report["result"] = "HISTORICAL_FAILURE_NOT_REPRODUCED" if FAILURE is None else "SETUP_FAILURE_REPRODUCED"
    report["first_failing_operation_id"] = None if FAILURE is None else FAILURE.get("operation_id")
    report["scanner_invocations"] = 0
    report["lsof_invocations"] = 0
    report["helper_processes_started"] = 0

    cleanup_errors = []
    for fd_name in ("child_fd", "root_fd"):
        fd = STATE.get(fd_name)
        if fd is not None:
            try:
                os.close(fd)
            except BaseException as exc:
                cleanup_errors.append({"operation": f"os.close({fd_name})", "exception": exception_record(exc, include_traceback=True)})
            STATE[fd_name] = None
    manager = STATE.get("manager")
    if manager is not None:
        try:
            manager.cleanup()
        except BaseException as exc:
            cleanup_errors.append({"operation": "TemporaryDirectory.cleanup", "exception": exception_record(exc, include_traceback=True)})
    report["cleanup_errors"] = cleanup_errors
    report["finished_at_utc"] = utc_now()
    report["operation_count"] = len(OPERATIONS)

    document = {
        "evidence_encoding": "r6a-r35-typed-json",
        "version": 1,
        "root": normalize(report),
    }
    json_bytes = canonical_json(document) + b"\n"
    markdown = render_markdown(report).encode("utf-8")
    completion = {
        "schema": "r6-a-r43-setup-probe-completion-v1",
        "run_id": RUN_ID,
        "status": "COMPLETE",
        "result": report["result"],
        "json": {"path": JSON_OUT.name, "size_bytes": len(json_bytes), "sha256": hashlib.sha256(json_bytes).hexdigest()},
        "markdown": {"path": MD_OUT.name, "size_bytes": len(markdown), "sha256": hashlib.sha256(markdown).hexdigest()},
    }
    publish(JSON_OUT, json_bytes)
    publish(MD_OUT, markdown)
    publish(COMPLETE_OUT, canonical_json(completion) + b"\n")
    print(f"RESULT={report['result']}")
    print(f"FIRST_FAILING_OPERATION={report['first_failing_operation_id']}")
    print(f"JSON={JSON_OUT}")
    print(f"MARKDOWN={MD_OUT}")
    print(f"COMPLETE={COMPLETE_OUT}")
    print(f"JSON_SHA256={completion['json']['sha256']}")
    print(f"MARKDOWN_SHA256={completion['markdown']['sha256']}")
    return 0 if FAILURE is None else 1

if __name__ == "__main__":
    raise SystemExit(main())
