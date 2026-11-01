#!/usr/bin/env python3
"""One paired unpinned/pinned lsof probe using the accepted R33 scanner."""
from __future__ import annotations

import base64
import argparse
import ast
import hashlib
import json
import os
import re
import select
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import r6a_r33_live_open_capture as scanner_module
from r6a_r33_live_open_capture import (
    LSOF,
    LSOF_ARG,
    LSOF_VERSION,
    executable_identity,
    fstat_identity,
    process_identity,
    process_snapshot,
    row_for_report,
    scan_lsof_path,
)


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_BASE = ROOT / "experiments/storage"
EXPECTED_LSOF_PATH = "/usr/sbin/lsof"
HELPER_PROTOCOL = (
    "Create one root and child; one foreign helper opens and holds the child FD. Its readiness "
    "JSON reports PID/PPID, exact child FD identity, and a SHA-256 digest of sorted inherited "
    "environment entries without exposing values. Keep that same helper live across U and P; "
    "perform U then P without an inter-scan delay, and release it with one newline after both scans."
)
HELPER_CODE = r"""
import hashlib, json, os, sys
path = sys.argv[1]
fd = os.open(path, os.O_RDONLY)
info = os.fstat(fd)
environment = sorted(os.environb.items())
environment_bytes = b"\0".join(key + b"=" + value for key, value in environment)
print(json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "fd": fd,
                  "device": info.st_dev, "inode": info.st_ino,
                  "type": __import__('stat').S_IFMT(info.st_mode),
                  "environment_sha256": hashlib.sha256(environment_bytes).hexdigest(),
                  "environment_entries": len(environment)}), flush=True)
sys.stdin.read(1)
os.close(fd)
"""


def path_identity(path: Path) -> dict[str, Any]:
    info = os.stat(path, follow_symlinks=False)
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "type": stat.S_IFMT(info.st_mode),
        "is_directory": stat.S_ISDIR(info.st_mode),
    }


def file_identity(path: Path) -> dict[str, Any]:
    info = os.stat(path, follow_symlinks=False)
    return {
        "device": info.st_dev,
        "inode": info.st_ino,
        "type": stat.S_IFMT(info.st_mode),
        "size": info.st_size,
    }


def json_default(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii"), "bytes": len(value)}
    raise TypeError(f"cannot serialize {type(value).__name__}")


def scanner_control_identity() -> dict[str, Any]:
    """Capture the imported scanner/parser code and its fixed subprocess controls."""
    source_path = Path(scanner_module.__file__).resolve(strict=True)
    source_bytes = source_path.read_bytes()
    source_text = source_bytes.decode("utf-8")
    tree = ast.parse(source_text, filename=str(source_path))
    bounded_function = next(
        node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "run_lsof_bounded"
    )
    environment_candidates = []
    for node in ast.walk(bounded_function):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if not (isinstance(function, ast.Attribute) and function.attr == "Popen"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "env":
                environment_candidates.append(ast.literal_eval(keyword.value))
    if len(environment_candidates) != 1 or not isinstance(environment_candidates[0], dict):
        raise RuntimeError("cannot establish one literal lsof subprocess environment")
    raw_environment = environment_candidates[0]
    safe_environment_keys = ("PATH", "LANG", "LC_ALL")
    sanitized_environment = {
        key: raw_environment[key] for key in safe_environment_keys if key in raw_environment
    }
    redacted_environment_keys = sorted(
        key for key in raw_environment if key not in safe_environment_keys
    )

    function_hashes: dict[str, dict[str, Any]] = {}
    for name in ("parse_lsof", "run_lsof_bounded", "scan_lsof_path"):
        function_node = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
        )
        function_source_text = ast.get_source_segment(source_text, function_node)
        if function_source_text is None:
            raise RuntimeError(f"cannot extract source for scanner function {name}")
        function_source = function_source_text.encode("utf-8")
        function_hashes[name] = {
            "qualified_name": f"{scanner_module.__name__}.{name}",
            "source_bytes": len(function_source),
            "source_sha256": hashlib.sha256(function_source).hexdigest(),
        }
    return {
        "module": scanner_module.__name__,
        "source_path": str(source_path),
        "source_bytes": len(source_bytes),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "function_hashes": function_hashes,
        "lsof_environment": sanitized_environment,
        "redacted_environment_keys": redacted_environment_keys,
        "lsof_environment_sha256": hashlib.sha256(
            json.dumps(sanitized_environment, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "lsof_timeout_seconds": scanner_module.LSOF_TIMEOUT_SECONDS,
        "lsof_stdout_limit_bytes": scanner_module.LSOF_STDOUT_LIMIT,
        "lsof_stderr_limit_bytes": scanner_module.LSOF_STDERR_LIMIT,
        "lsof_fields": scanner_module.LSOF_FIELDS,
        "lsof_arg": scanner_module.LSOF_ARG,
    }


def json_sha256(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def compare_control_snapshots(variants: dict[str, Any]) -> dict[str, Any]:
    """Require byte-for-byte JSON-equivalent control snapshots before interpreting U/P."""
    if set(variants) != {"U", "P"}:
        return {
            "status": "PIN_EFFECT_UNRESOLVED",
            "controls_equal": False,
            "differences": ["paired U/P control snapshots are incomplete"],
        }
    unpinned = variants["U"].get("control_snapshot")
    pinned = variants["P"].get("control_snapshot")
    if not isinstance(unpinned, dict) or not isinstance(pinned, dict):
        return {
            "status": "PIN_EFFECT_UNRESOLVED",
            "controls_equal": False,
            "differences": ["one or both variant control snapshots are missing"],
        }
    fields = sorted(set(unpinned) | set(pinned))
    differences = [
        {"field": key, "U": unpinned.get(key), "P": pinned.get(key)}
        for key in fields if unpinned.get(key) != pinned.get(key)
    ]
    baseline_changes = []
    for label in ("U", "P"):
        item = variants[label]
        if item.get("scanner_source_unchanged_from_baseline") is not True:
            baseline_changes.append(f"{label} scanner source/settings differ from pre-fixture baseline")
        if item.get("checker_process_unchanged_from_baseline") is not True:
            baseline_changes.append(f"{label} checker PID/process identity/cwd changed from baseline")
        if item.get("lsof_executable_unchanged_from_baseline") is not True:
            baseline_changes.append(f"{label} lsof executable identity changed from validated baseline")
        if item.get("lsof_executable_unchanged_during_scan") is not True:
            baseline_changes.append(f"{label} lsof executable identity changed around the scan")
    differences.extend({"field": "scanner_baseline", "detail": item} for item in baseline_changes)
    return {
        "status": "PASS" if not differences else "PIN_EFFECT_UNRESOLVED",
        "controls_equal": not differences,
        "compared_fields": fields,
        "u_control_snapshot_sha256": json_sha256(unpinned),
        "p_control_snapshot_sha256": json_sha256(pinned),
        "differences": differences,
    }


def _row_name_bytes(row: dict[str, Any]) -> bytes | None:
    encoded = row.get("name_base64")
    if not isinstance(encoded, str):
        return None
    try:
        return base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error):
        return None


def _foreign_child_row_identity(
    variant: dict[str, Any], helper_pid: int, helper_fd: int,
    child_identity: dict[str, Any], canonical_child: str,
) -> list[dict[str, Any]]:
    matches = []
    for row in variant.get("parsed_rows", []):
        if (
            row.get("pid") == helper_pid
            and row.get("fd_number") == helper_fd
            and row.get("fd_type_raw") == "b'REG'"
            and row.get("device") == child_identity.get("device")
            and row.get("inode") == child_identity.get("inode")
            and row.get("name_bytes") == len(canonical_child.encode())
            and _row_name_bytes(row) == canonical_child.encode()
        ):
            matches.append({
                key: row.get(key) for key in (
                    "pid", "ppid", "command_raw", "fd_raw", "fd_number", "fd_suffix",
                    "fd_type_raw", "device", "inode", "name_base64",
                )
            })
    return matches


def _exact_root_pin_rows(
    variant: dict[str, Any], checker_pid: int, pin_fd: int,
    root_identity: dict[str, Any], canonical_root: str,
) -> list[dict[str, Any]]:
    matches = []
    for row in variant.get("parsed_rows", []):
        if (
            row.get("pid") == checker_pid
            and row.get("fd_number") == pin_fd
            and row.get("fd_type_raw") == "b'DIR'"
            and row.get("device") == root_identity.get("device")
            and row.get("inode") == root_identity.get("inode")
            and row.get("name_bytes") == len(canonical_root.encode())
            and _row_name_bytes(row) == canonical_root.encode()
        ):
            matches.append({
                key: row.get(key) for key in (
                    "pid", "ppid", "command_raw", "fd_raw", "fd_number", "fd_suffix",
                    "fd_type_raw", "device", "inode", "name_base64",
                )
            })
    return matches


def _structurally_valid_scan(variant: dict[str, Any], expected_argv: list[str]) -> bool:
    capture = variant.get("capture", {})
    framing = variant.get("framing", {})
    child = capture.get("child_identity")
    return bool(
        isinstance(capture.get("returncode"), int)
        and variant.get("scanner_argv") == expected_argv
        and framing.get("complete") is True
        and framing.get("row_count") == len(variant.get("parsed_rows", []))
        and capture.get("stderr_total_bytes") == 0
        and capture.get("stderr_bytes_captured") == 0
        and capture.get("stdout_total_bytes") == capture.get("stdout_bytes_captured")
        and capture.get("stderr_total_bytes") == capture.get("stderr_bytes_captured")
        and capture.get("stdout_capped") is False
        and capture.get("stderr_capped") is False
        and capture.get("timed_out") is False
        and capture.get("child_identity_error") is None
        and isinstance(child, dict)
        and child.get("pid") == capture.get("child_pid")
        and child.get("ppid") == capture.get("child_ppid")
        and all(row.get("parser_status") == "PASS" for row in variant.get("parsed_rows", []))
    )


def classify_pin_status_effect(
    variants: dict[str, Any], equality_gate: dict[str, Any], *,
    helper_pid: int, helper_fd: int, child_identity: dict[str, Any], canonical_child: str,
    checker_pid: int, root_identity: dict[str, Any], canonical_root: str,
) -> dict[str, Any]:
    """Classify only after the explicit control equality gate and structural checks."""
    if equality_gate.get("controls_equal") is not True:
        return {
            "classification": "PIN_EFFECT_UNRESOLVED",
            "reason": "control equality gate failed; no status interpretation made",
        }
    if set(variants) != {"U", "P"}:
        return {
            "classification": "PIN_EFFECT_UNRESOLVED",
            "reason": "paired U/P scans are incomplete",
        }
    expected_argv = [EXPECTED_LSOF_PATH, LSOF_ARG, "+D", canonical_root]
    u_scan = variants["U"]
    p_scan = variants["P"]
    invalid = [
        label for label, item in (("U", u_scan), ("P", p_scan))
        if not _structurally_valid_scan(item, expected_argv)
    ]
    if invalid:
        return {
            "classification": "PIN_EFFECT_UNRESOLVED",
            "reason": f"captured scan structure was invalid for {', '.join(invalid)}",
        }

    u_children = _foreign_child_row_identity(
        u_scan, helper_pid, helper_fd, child_identity, canonical_child
    )
    p_children = _foreign_child_row_identity(
        p_scan, helper_pid, helper_fd, child_identity, canonical_child
    )
    p_pins = _exact_root_pin_rows(
        p_scan, checker_pid, p_scan.get("checker_pin_fd"), root_identity, canonical_root
    )
    u_status = u_scan["capture"]["returncode"]
    p_status = p_scan["capture"]["returncode"]
    same_child_row = len(u_children) == 1 and len(p_children) == 1 and u_children[0] == p_children[0]

    if (u_status == 1 and len(u_children) == 1 and p_status == 0
            and len(p_pins) == 1 and same_child_row):
        classification = "PIN_STATUS_NORMALIZES_1_TO_0"
        reason = "U reports the exact foreign child with status 1; P reports the same child and exact checker root pin with status 0"
    elif u_status == p_status:
        classification = "PIN_HAS_NO_STATUS_EFFECT"
        reason = "U and P have the same lsof status semantics"
    else:
        classification = "PIN_EFFECT_DIFFERENT_FROM_EXPECTED"
        reason = (
            "controls and streams are valid, but the observed statuses/rows do not match either "
            "the 1-to-0 normalization contract or the same-status contract"
        )
    return {
        "classification": classification,
        "reason": reason,
        "U_returncode": u_status,
        "P_returncode": p_status,
        "U_foreign_child_match_count": len(u_children),
        "P_foreign_child_match_count": len(p_children),
        "P_exact_root_pin_match_count": len(p_pins),
        "same_foreign_child_row": same_child_row,
        "U_foreign_child_row_identity": u_children,
        "P_foreign_child_row_identity": p_children,
        "P_exact_root_pin_row_identity": p_pins,
    }


def capture_lsof_version() -> dict[str, Any]:
    """Capture the diagnostic version command without assuming its output stream."""
    argv = [EXPECTED_LSOF_PATH, "-v"]
    capture: dict[str, Any] = {
        "argv": argv,
        "expected_path": EXPECTED_LSOF_PATH,
        "configured_scanner_path": LSOF,
        "path_exact": argv[0] == EXPECTED_LSOF_PATH and LSOF == EXPECTED_LSOF_PATH,
        "returncode": None,
        "stdout_raw": b"",
        "stderr_raw": b"",
        "command_error": None,
    }
    try:
        result = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
            timeout=5, env={"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
        capture.update({
            "returncode": result.returncode,
            "stdout_raw": result.stdout,
            "stderr_raw": result.stderr,
        })
    except (OSError, subprocess.SubprocessError) as error:
        capture["command_error"] = f"{type(error).__name__}: {error}"

    combined = capture["stdout_raw"] + b"\n" + capture["stderr_raw"]
    text_value = combined.decode("ascii", "replace")
    capture.update({
        "stdout_base64": base64.b64encode(capture["stdout_raw"]).decode("ascii"),
        "stdout_sha256": hashlib.sha256(capture["stdout_raw"]).hexdigest(),
        "stdout_bytes": len(capture["stdout_raw"]),
        "stderr_base64": base64.b64encode(capture["stderr_raw"]).decode("ascii"),
        "stderr_sha256": hashlib.sha256(capture["stderr_raw"]).hexdigest(),
        "stderr_bytes": len(capture["stderr_raw"]),
        "text": text_value,
        "expected_version": LSOF_VERSION,
        "revision_4_91_detected": re.search(r"(?m)^\s*revision:\s*4\.91\s*$", text_value) is not None,
        "libproc_based_detected": re.search(
            r"(?m)^\s*configuration info:\s*libproc-based\s*$", text_value
        ) is not None,
    })
    capture["version_match"] = bool(
        capture["path_exact"]
        and capture["returncode"] == 0
        and capture["revision_4_91_detected"]
        and capture["libproc_based_detected"]
    )
    return capture


def preserve_version_streams(output_dir: Path, version: dict[str, Any]) -> None:
    """Write each raw version stream unchanged and record its durable identity."""
    for stream in ("stdout", "stderr"):
        raw = version.pop(f"{stream}_raw")
        path = output_dir / f"lsof-version.{stream}.bin"
        path.write_bytes(raw)
        preserved = path.read_bytes()
        if preserved != raw:
            raise OSError(f"lsof version {stream} sidecar did not preserve captured bytes")
        version[f"{stream}_sidecar"] = {
            "path": path.relative_to(ROOT).as_posix(),
            "bytes": len(preserved),
            "sha256": hashlib.sha256(preserved).hexdigest(),
        }


def run_precheck_only(output_base: Path = OUTPUT_BASE) -> int:
    """Validate and record the installed lsof identity without entering the fixture path."""
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = output_base / f"R6-A-R40-LSOF-PRECHECK-{run_id}"
    output_dir.mkdir(mode=0o700)
    version = capture_lsof_version()
    preserve_version_streams(output_dir, version)
    binary: dict[str, Any] | None = None
    binary_error: str | None = None
    if version["path_exact"]:
        try:
            binary = executable_identity(EXPECTED_LSOF_PATH)
        except (OSError, RuntimeError) as error:
            binary_error = f"{type(error).__name__}: {error}"
    precheck_passed = version["version_match"] and binary is not None and binary.get("stable") is True
    report = {
        "schema": "r6a-r40-lsof-version-precheck-v1",
        "run_id": run_id,
        "started_at_utc": started_at,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "precheck_only": True,
        "version_precheck_passed": precheck_passed,
        "lsof": {
            "path": EXPECTED_LSOF_PATH,
            "version": version,
            "executable": binary,
            "executable_error": binary_error,
        },
        "fixture_creation_entered": False,
        "temporary_root_created": False,
        "temporary_root_removed_if_created": None,
        "helper_launch_entered": False,
        "helper_started": False,
        "variant_u_started": False,
        "variant_p_started": False,
        "blockers": [] if precheck_passed else [
            "version precheck did not establish exact /usr/sbin/lsof 4.91 libproc-based identity"
        ],
    }
    json_path = output_dir / "R6-A-R40-LSOF-PRECHECK-VALIDATION.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    md_path = output_dir / "R6-A-R40-LSOF-PRECHECK-VALIDATION.md"
    md_path.write_text(
        "\n".join([
            "# R40 lsof version precheck validation",
            "",
            f"- Run ID: `{run_id}`",
            f"- Exact executable path: `{EXPECTED_LSOF_PATH}` (`{version['path_exact']}`)",
            f"- Return code: `{version['returncode']}`",
            f"- Revision 4.91 detected: `{version['revision_4_91_detected']}`",
            f"- libproc-based detected: `{version['libproc_based_detected']}`",
            f"- Raw stdout preserved: `{version['stdout_sidecar']['path']}` "
            f"({version['stdout_sidecar']['bytes']} bytes, SHA-256 `{version['stdout_sidecar']['sha256']}`)",
            f"- Raw stderr preserved: `{version['stderr_sidecar']['path']}` "
            f"({version['stderr_sidecar']['bytes']} bytes, SHA-256 `{version['stderr_sidecar']['sha256']}`)",
            f"- Fixture/helper started: `{report['fixture_creation_entered']}` / `{report['helper_started']}`",
            f"- Validation: `{'PASS' if precheck_passed else 'FAIL'}`",
            "",
            *(f"- Blocker: {item}" for item in report["blockers"]),
            "",
        ]),
        encoding="utf-8",
    )
    print(json_path.relative_to(ROOT))
    print(md_path.relative_to(ROOT))
    print(json.dumps({
        "version_precheck_passed": report["version_precheck_passed"],
        "fixture_creation_entered": report["fixture_creation_entered"],
        "temporary_root_created": report["temporary_root_created"],
        "helper_started": report["helper_started"],
        "blockers": report["blockers"],
    }, indent=2))
    return 0 if report["version_precheck_passed"] else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--precheck-only", action="store_true",
        help="validate and preserve /usr/sbin/lsof -v evidence without creating a fixture or helper",
    )
    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        raise RuntimeError(f"controlled R40 pin experiment requires Darwin, got {sys.platform}")
    if args.precheck_only:
        return run_precheck_only()
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = OUTPUT_BASE / f"R6-A-R40-PIN-CAUSALITY-{run_id}"
    output_dir.mkdir(mode=0o700)
    report: dict[str, Any] = {
        "schema": "r6a-r40-pin-causality-v1",
        "run_id": run_id,
        "started_at_utc": started_at,
        "purpose": "Isolate whether holding the exact checker root directory FD changes this installed lsof scan's result.",
        "scanner": "experiments/storage/r6a_r33_live_open_capture.py:scan_lsof_path",
        "scanner_logic_modified": False,
        "variants": {},
        "blockers": [],
        "fixture_creation_entered": False,
        "temporary_root_created": False,
        "temporary_root_removed_if_created": None,
        "helper_launch_entered": False,
        "helper_started": False,
        "causal_classification": None,
        "cleanup": {
            "temporary_root_created": False,
            "temporary_root_removed_if_created": None,
            "helper_started": False,
        },
    }
    helper: subprocess.Popen[str] | None = None
    root_path: Path | None = None
    try:
        version = capture_lsof_version()
        preserve_version_streams(output_dir, version)
        binary: dict[str, Any] | None = None
        binary_error: str | None = None
        try:
            binary = executable_identity(EXPECTED_LSOF_PATH)
        except (OSError, RuntimeError) as error:
            binary_error = f"{type(error).__name__}: {error}"
        report["lsof"] = {"path": EXPECTED_LSOF_PATH, "argv_field": LSOF_ARG,
                          "expected_version": LSOF_VERSION, "version": version,
                          "executable": binary, "executable_error": binary_error}
        if not version.get("version_match"):
            raise RuntimeError("installed lsof is not the accepted 4.91/libproc build")
        if binary is None or binary.get("stable") is not True:
            raise RuntimeError("installed lsof executable identity is unavailable or unstable")
        if LSOF_ARG != "-F0pcRftDrin":
            raise RuntimeError("accepted structured scanner argv changed")
        scanner_baseline = scanner_control_identity()
        report["scanner_control_baseline"] = scanner_baseline
        report["helper_implementation_sha256"] = hashlib.sha256(HELPER_CODE.encode("utf-8")).hexdigest()
        report["helper_protocol"] = HELPER_PROTOCOL

        report["fixture_creation_entered"] = True
        with tempfile.TemporaryDirectory(prefix="r6a-r40-pin-causality-") as temporary:
            parent = Path(temporary).resolve(strict=True)
            root_path = parent / "root"
            root_path.mkdir(mode=0o700)
            report["temporary_root_created"] = True
            report["cleanup"]["temporary_root_created"] = True
            child_path = root_path / "foreign-child.bin"
            with child_path.open("xb") as child:
                child.write(b"R40 pinned-versus-unpinned lsof causality fixture\n")
                child.flush()
                os.fsync(child.fileno())

            canonical_root = str(root_path.resolve(strict=True))
            canonical_child = str(child_path.resolve(strict=True))
            root_before = path_identity(root_path)
            child_before = file_identity(child_path)
            report["fixture"] = {
                "parent": str(parent),
                "root_path": str(root_path),
                "canonical_root": canonical_root,
                "child_path": str(child_path),
                "canonical_child": canonical_child,
                "root_identity_before": root_before,
                "child_identity_before": child_before,
                "helper_cwd": os.getcwd(),
                "checker_cwd": os.getcwd(),
                "protocol": HELPER_PROTOCOL,
            }

            report["helper_launch_entered"] = True
            helper_argv = [sys.executable, "-c", HELPER_CODE, canonical_child]
            helper = subprocess.Popen(
                helper_argv,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, close_fds=True,
            )
            report["helper_started"] = True
            report["cleanup"]["helper_started"] = True
            assert helper.stdout is not None and helper.stdin is not None
            ready, _, _ = select.select([helper.stdout], [], [], 10.0)
            if not ready:
                raise RuntimeError("foreign helper did not complete its readiness handshake")
            handshake_line = helper.stdout.readline()
            if not handshake_line:
                raise RuntimeError("foreign helper exited before readiness handshake")
            helper_handshake = json.loads(handshake_line)
            helper_identity_before = process_identity(helper.pid)
            if (helper_handshake.get("pid") != helper.pid
                    or helper_handshake.get("ppid") != os.getpid()
                    or helper_identity_before.get("ppid") != os.getpid()):
                raise RuntimeError("foreign helper PID/PPID identity failed")
            if (helper_handshake.get("device"), helper_handshake.get("inode"), helper_handshake.get("type")) != (
                    child_before["device"], child_before["inode"], child_before["type"]):
                raise RuntimeError("foreign helper FD does not identify the exact child object")
            if (not isinstance(helper_handshake.get("environment_sha256"), str)
                    or re.fullmatch(r"[0-9a-f]{64}", helper_handshake["environment_sha256"]) is None
                    or not isinstance(helper_handshake.get("environment_entries"), int)):
                raise RuntimeError("foreign helper environment fingerprint is incomplete")
            report["foreign_helper"] = {
                "handshake": helper_handshake,
                "argv": helper_argv,
                "implementation_sha256": hashlib.sha256(HELPER_CODE.encode("utf-8")).hexdigest(),
                "protocol": HELPER_PROTOCOL,
                "environment_fingerprint": {
                    "algorithm": "SHA-256 over sorted os.environb entries joined with NUL",
                    "sha256": helper_handshake["environment_sha256"],
                    "entry_count": helper_handshake["environment_entries"],
                    "values_recorded": False,
                },
                "process_identity_before": helper_identity_before,
                "child_fd_identity": {
                    "device": helper_handshake["device"],
                    "inode": helper_handshake["inode"],
                    "type": helper_handshake["type"],
                },
                "held_from_before_unpinned_scan_through_after_pinned_scan": True,
            }

            checker = process_snapshot()
            if checker["pid"] != os.getpid():
                raise RuntimeError("checker process snapshot PID mismatch")
            report["checker"] = checker

            def take_scan(variant: str, pin_fd: int | None) -> dict[str, Any]:
                scanner_control_before = scanner_control_identity()
                checker_pid_before = os.getpid()
                checker_cwd_before = os.getcwd()
                before = path_identity(root_path)
                child_identity_before = file_identity(child_path)
                helper_identity_pre = process_identity(helper.pid)
                pin_identity = fstat_identity(pin_fd, with_fsid=True) if pin_fd is not None else None
                pin_exact_named_object: bool | None = None
                if pin_fd is not None:
                    named = path_identity(root_path)
                    if ((named["device"], named["inode"], named["type"])
                            != (pin_identity["device"], pin_identity["inode"], pin_identity["type"])
                            or not pin_identity["is_directory"]):
                        raise RuntimeError("pinned root FD does not match exact named root")
                    pin_exact_named_object = True
                lsof_executable_before = executable_identity(EXPECTED_LSOF_PATH)
                started_ns = time.monotonic_ns()
                capture = scan_lsof_path(canonical_root)
                ended_ns = time.monotonic_ns()
                lsof_executable_after = executable_identity(EXPECTED_LSOF_PATH)
                after = path_identity(root_path)
                child_identity_after = file_identity(child_path)
                helper_identity_post = process_identity(helper.pid)
                checker_pid_after = os.getpid()
                checker_cwd_after = os.getcwd()
                scanner_control_after = scanner_control_identity()
                stdout = capture["stdout_raw"]
                stderr = capture["stderr_raw"]
                stdout_name = f"{variant}.stdout.bin"
                stderr_name = f"{variant}.stderr.bin"
                (output_dir / stdout_name).write_bytes(stdout)
                (output_dir / stderr_name).write_bytes(stderr)
                stream = capture["stream"]
                same_helper = helper_identity_pre == helper_identity_post
                stable_root = before == after == root_before
                stable_child = child_identity_before == child_identity_after == child_before
                if not same_helper:
                    report["blockers"].append(f"{variant}: foreign helper identity changed")
                if not stable_root:
                    report["blockers"].append(f"{variant}: named root identity changed")
                if not stable_child:
                    report["blockers"].append(f"{variant}: foreign child identity changed")
                control_snapshot = {
                    "root_identity_before_scan": before,
                    "root_identity_after_scan": after,
                    "child_identity_before_scan": child_identity_before,
                    "child_identity_after_scan": child_identity_after,
                    "canonical_child_path": canonical_child,
                    "checker_pid": checker["pid"],
                    "checker_cwd": checker["cwd"],
                    "checker_pid_before_scan": checker_pid_before,
                    "checker_pid_after_scan": checker_pid_after,
                    "checker_cwd_before_scan": checker_cwd_before,
                    "checker_cwd_after_scan": checker_cwd_after,
                    "checker_start_identity": checker["start_identity"],
                    "checker_process_identity": checker,
                    "helper": {
                        "implementation_sha256": hashlib.sha256(HELPER_CODE.encode("utf-8")).hexdigest(),
                        "protocol": HELPER_PROTOCOL,
                        "argv": helper_argv,
                        "held_child_fd": helper_handshake["fd"],
                        "child_fd_identity": {
                            "device": helper_handshake["device"],
                            "inode": helper_handshake["inode"],
                            "type": helper_handshake["type"],
                        },
                        "process_identity_before": helper_identity_pre,
                        "process_identity_after": helper_identity_post,
                        "environment_fingerprint_sha256": helper_handshake["environment_sha256"],
                        "environment_fingerprint_entries": helper_handshake["environment_entries"],
                    },
                    "lsof": {
                        "argv": capture["argv"],
                        "validated_executable_identity_baseline": report["lsof"]["executable"],
                        "executable_identity_before_scan": lsof_executable_before,
                        "executable_identity_after_scan": lsof_executable_after,
                        "environment": scanner_control_before["lsof_environment"],
                        "environment_sha256": scanner_control_before["lsof_environment_sha256"],
                        "timeout_seconds": scanner_control_before["lsof_timeout_seconds"],
                        "stdout_limit_bytes": scanner_control_before["lsof_stdout_limit_bytes"],
                        "stderr_limit_bytes": scanner_control_before["lsof_stderr_limit_bytes"],
                    },
                    "scanner_parser_identity_before": scanner_control_before,
                    "scanner_parser_identity_after": scanner_control_after,
                    "scanner_baseline_sha256": json_sha256(scanner_baseline),
                }
                scanner_unchanged = (
                    scanner_control_before == scanner_baseline
                    and scanner_control_after == scanner_baseline
                )
                checker_unchanged = (
                    checker_pid_before == checker["pid"] == checker_pid_after
                    and checker_cwd_before == checker["cwd"] == checker_cwd_after
                )
                lsof_executable_unchanged = (
                    lsof_executable_before == report["lsof"]["executable"]
                    and lsof_executable_after == report["lsof"]["executable"]
                )
                return {
                    "variant": variant,
                    "root_pin_held": pin_fd is not None,
                    "checker_pin_fd": pin_fd,
                    "checker_pin_identity": pin_identity,
                    "checker_pin_exact_named_object": pin_exact_named_object,
                    "root_pin_control": {
                        "fd": pin_fd,
                        "identity": pin_identity,
                        "live_during_scan": pin_fd is not None,
                        "exact_named_object": pin_exact_named_object,
                        "excluded_from_control_equality": True,
                    },
                    "intended_independent_variable": {
                        "checker_root_pin_live": pin_fd is not None,
                        "checker_pin_fd": pin_fd,
                        "excluded_from_control_equality": True,
                    },
                    "scanner_argv": capture["argv"],
                    "started_monotonic_ns": started_ns,
                    "ended_monotonic_ns": ended_ns,
                    "elapsed_ns": ended_ns - started_ns,
                    "root_identity_before_scan": before,
                    "root_identity_after_scan": after,
                    "root_identity_stable": stable_root,
                    "child_identity_before_scan": child_identity_before,
                    "child_identity_after_scan": child_identity_after,
                    "child_identity_stable": stable_child,
                    "foreign_helper_identity_before": helper_identity_pre,
                    "foreign_helper_identity_after": helper_identity_post,
                    "foreign_helper_identity_stable": same_helper,
                    "scanner_source_unchanged_from_baseline": scanner_unchanged,
                    "checker_process_unchanged_from_baseline": checker_unchanged,
                    "lsof_executable_unchanged_from_baseline": lsof_executable_unchanged,
                    "lsof_executable_unchanged_during_scan": lsof_executable_before == lsof_executable_after,
                    "control_snapshot": control_snapshot,
                    "control_snapshot_sha256": json_sha256(control_snapshot),
                    "capture": {
                        key: capture.get(key) for key in (
                            "child_pid", "child_ppid", "child_identity", "child_identity_error",
                            "returncode", "elapsed_seconds", "stdout_total_bytes", "stdout_bytes_captured",
                            "stdout_sha256", "stdout_captured_sha256", "stdout_capped",
                            "stderr_total_bytes", "stderr_bytes_captured", "stderr_sha256",
                            "stderr_captured_sha256", "stderr_capped", "timed_out",
                        )
                    },
                    "stdout_sidecar": {
                        "path": (output_dir / stdout_name).relative_to(ROOT).as_posix(),
                        "bytes": len(stdout),
                        "sha256": hashlib.sha256(stdout).hexdigest(),
                        "base64": base64.b64encode(stdout).decode("ascii"),
                    },
                    "stderr_sidecar": {
                        "path": (output_dir / stderr_name).relative_to(ROOT).as_posix(),
                        "bytes": len(stderr),
                        "sha256": hashlib.sha256(stderr).hexdigest(),
                        "base64": base64.b64encode(stderr).decode("ascii"),
                    },
                    "framing": stream,
                    "parsed_rows": [row_for_report(row) for row in capture["rows"]],
                    "scan_complete_legacy_predicate": capture["scan_complete"],
                }

            report["variants"]["U"] = take_scan("U-unpinned", None)

            pin_fd = os.open(root_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                             | getattr(os, "O_NOFOLLOW", 0))
            try:
                pin_info = fstat_identity(pin_fd, with_fsid=True)
                pin_named = path_identity(root_path)
                if ((pin_named["device"], pin_named["inode"], pin_named["type"])
                        != (pin_info["device"], pin_info["inode"], pin_info["type"])
                        or (pin_info["device"], pin_info["inode"], pin_info["type"])
                        != (root_before["device"], root_before["inode"], root_before["type"])
                        or not stat.S_ISDIR(pin_info["type"])):
                    raise RuntimeError("pinned variant failed exact validated root identity")
                report["variants"]["P"] = take_scan("P-pinned", pin_fd)
                report["variants"]["P"]["checker_pin_validation"] = {
                    "named_root": pin_named,
                    "fstat": pin_info,
                    "matches_before_identity": True,
                }
            finally:
                os.close(pin_fd)

            equality_gate = compare_control_snapshots(report["variants"])
            report["control_equality_gate"] = equality_gate
            classification = classify_pin_status_effect(
                report["variants"], equality_gate,
                helper_pid=helper_handshake["pid"],
                helper_fd=helper_handshake["fd"],
                child_identity=child_before,
                canonical_child=canonical_child,
                checker_pid=checker["pid"],
                root_identity=root_before,
                canonical_root=canonical_root,
            )
            report["causal_classification"] = classification["classification"]
            report["causal_classification_evidence"] = classification
            if report["causal_classification"] == "PIN_EFFECT_UNRESOLVED":
                detail = "; ".join(
                    item.get("field", item.get("detail", "unknown control mismatch"))
                    for item in equality_gate.get("differences", [])
                )
                reason = classification.get("reason", "control or stream validation failed")
                report["blockers"].append(f"PIN_EFFECT_UNRESOLVED: {detail or reason}")

            report["fixture"]["root_identity_after_pair"] = path_identity(root_path)
            report["fixture"]["child_identity_after_pair"] = file_identity(child_path)
            report["fixture"]["root_stable_for_pair"] = (
                report["fixture"]["root_identity_after_pair"] == root_before
            )
            report["fixture"]["child_stable_for_pair"] = (
                report["fixture"]["child_identity_after_pair"] == child_before
            )
            if not report["fixture"]["root_stable_for_pair"]:
                report["blockers"].append("named root identity changed across the paired scans")
            if not report["fixture"]["child_stable_for_pair"]:
                report["blockers"].append("child identity changed across the paired scans")
            report["foreign_helper"]["process_identity_after_pair"] = process_identity(helper.pid)
            helper.stdin.write("\n")
            helper.stdin.flush()
            helper_returncode = helper.wait(timeout=5)
            report["foreign_helper"]["returncode_after_release"] = helper_returncode
            report["foreign_helper"]["stderr_after_release"] = helper.stderr.read() if helper.stderr else ""
            if helper_returncode != 0:
                report["blockers"].append("foreign helper did not exit cleanly")
            if report["variants"]["U"]["scanner_argv"] != report["variants"]["P"]["scanner_argv"]:
                report["blockers"].append("scanner argv differed between paired variants")
            if "-Q" in report["variants"]["U"]["scanner_argv"]:
                report["blockers"].append("forbidden -Q appeared in experiment argv")

        report["temporary_root_removed_if_created"] = (
            not root_path.exists() if report["temporary_root_created"] and root_path is not None else None
        )
        report["cleanup"]["temporary_root_removed_if_created"] = report["temporary_root_removed_if_created"]
        report["cleanup"]["helper_exited"] = helper is not None and helper.poll() is not None
        report["cleanup"]["all_required_checks_passed"] = (
            not report["blockers"]
            and report["temporary_root_created"]
            and report["temporary_root_removed_if_created"] is True
            and report["cleanup"]["helper_exited"]
            and set(report["variants"]) == {"U", "P"}
            and report.get("control_equality_gate", {}).get("controls_equal") is True
            and report.get("causal_classification") != "PIN_EFFECT_UNRESOLVED"
        )
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        if set(report["variants"]) == {"U", "P"}:
            report["pin_status_comparison"] = {
                "unpinned_returncode": report["variants"]["U"]["capture"]["returncode"],
                "pinned_returncode": report["variants"]["P"]["capture"]["returncode"],
                "status_changed": (
                    report["variants"]["U"]["capture"]["returncode"]
                    != report["variants"]["P"]["capture"]["returncode"]
                ),
                "classification": report["causal_classification"],
                "interpretation": report["causal_classification_evidence"]["reason"],
            }
        else:
            report["pin_status_comparison"] = {
                "status_changed": None,
                "classification": "PIN_EFFECT_UNRESOLVED",
                "interpretation": "paired U/P capture incomplete; no status interpretation made",
            }
    except BaseException as error:
        report["blockers"].append(f"{type(error).__name__}: {error}")
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        if helper is not None and helper.poll() is None:
            helper.kill()
            try:
                helper.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
        report["temporary_root_removed_if_created"] = (
            not root_path.exists() if report["temporary_root_created"] and root_path is not None else None
        )
        report["cleanup"]["temporary_root_removed_if_created"] = report["temporary_root_removed_if_created"]
        report["cleanup"]["helper_exited"] = helper is None or helper.poll() is not None
        if "control_equality_gate" not in report:
            report["control_equality_gate"] = {
                "status": "PIN_EFFECT_UNRESOLVED",
                "controls_equal": False,
                "differences": ["control gate could not be reached before the first blocker"],
            }
        if report.get("causal_classification") is None:
            report["causal_classification"] = "PIN_EFFECT_UNRESOLVED"
            report["causal_classification_evidence"] = {
                "classification": "PIN_EFFECT_UNRESOLVED",
                "reason": "experiment stopped before control equality and status classification",
            }
        if "pin_status_comparison" not in report:
            report["pin_status_comparison"] = {
                "status_changed": None,
                "classification": "PIN_EFFECT_UNRESOLVED",
                "interpretation": report["causal_classification_evidence"]["reason"],
            }

    json_path = output_dir / "R6-A-R40-PIN-CAUSALITY-EXPERIMENT.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=json_default) + "\n", encoding="utf-8")
    md_path = output_dir / "R6-A-R40-PIN-CAUSALITY-EXPERIMENT.md"
    lines = [
        "# R6-A R40 pin-causality experiment",
        "",
        f"- Run ID: `{run_id}`",
        f"- Valid paired capture and cleanup: `{report.get('cleanup', {}).get('all_required_checks_passed', False)}`",
        f"- Control equality gate: `{report.get('control_equality_gate', {}).get('status', 'PIN_EFFECT_UNRESOLVED')}`",
        f"- Causal classification: `{report.get('causal_classification', 'PIN_EFFECT_UNRESOLVED')}`",
        f"- lsof: `{LSOF}` version `{report.get('lsof', {}).get('version', {}).get('text', '').splitlines()[1:2]}`",
        "- Scanner: existing `scan_lsof_path()` and parser; scanner source was not modified.",
        "- The foreign helper held one child FD across both scans.",
        "- Both scans used identical argv. No `-Q` was used.",
        "",
        "| Variant | Checker root pin | Return code | Rows | Complete framing | Empty stderr | Scan complete predicate |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for label, title in (("U", "U unpinned"), ("P", "P pinned")):
        item = report.get("variants", {}).get(label, {})
        capture = item.get("capture", {})
        lines.append(
            f"| {title} | {item.get('root_pin_held')} | {capture.get('returncode')} | "
            f"{item.get('framing', {}).get('row_count')} | {item.get('framing', {}).get('complete')} | "
            f"{capture.get('stderr_total_bytes') == 0 if capture else None} | "
            f"{item.get('scan_complete_legacy_predicate')} |"
        )
    lines.extend(["", "## Interpretation", "", report.get("pin_status_comparison", {}).get("interpretation", "Pair incomplete."), ""])
    if report.get("blockers"):
        lines.extend(["## Blockers", "", *[f"- {item}" for item in report["blockers"]], ""])
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(json_path.relative_to(ROOT))
    print(md_path.relative_to(ROOT))
    print(json.dumps({
        "cleanup": report.get("cleanup"),
        "blockers": report.get("blockers"),
        "control_equality_gate": report.get("control_equality_gate"),
        "causal_classification": report.get("causal_classification"),
        "pin_status_comparison": report.get("pin_status_comparison"),
    }, indent=2))
    return 0 if report.get("cleanup", {}).get("all_required_checks_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
