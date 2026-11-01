#!/usr/bin/env python3
"""One paired unpinned/pinned lsof probe using the accepted R33 scanner."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import select
import stat
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from r6a_r33_live_open_capture import (
    LSOF,
    LSOF_ARG,
    LSOF_VERSION,
    executable_identity,
    fstat_identity,
    lsof_version,
    process_identity,
    process_snapshot,
    row_for_report,
    scan_lsof_path,
)


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_BASE = ROOT / "experiments/storage"
HELPER_CODE = r"""
import json, os, sys
path = sys.argv[1]
fd = os.open(path, os.O_RDONLY)
info = os.fstat(fd)
print(json.dumps({"pid": os.getpid(), "ppid": os.getppid(), "fd": fd,
                  "device": info.st_dev, "inode": info.st_ino,
                  "type": __import__('stat').S_IFMT(info.st_mode)}), flush=True)
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


def main() -> int:
    if sys.platform != "darwin":
        raise RuntimeError(f"controlled R39 pin experiment requires Darwin, got {sys.platform}")
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir = OUTPUT_BASE / f"R6-A-R39-PIN-CAUSALITY-{run_id}"
    output_dir.mkdir(mode=0o700)
    report: dict[str, Any] = {
        "schema": "r6a-r39-pin-causality-v1",
        "run_id": run_id,
        "started_at_utc": started_at,
        "purpose": "Isolate whether holding the exact checker root directory FD changes this installed lsof scan's result.",
        "scanner": "experiments/storage/r6a_r33_live_open_capture.py:scan_lsof_path",
        "scanner_logic_modified": False,
        "variants": {},
        "blockers": [],
        "cleanup": {},
    }
    helper: subprocess.Popen[str] | None = None
    root_path: Path | None = None
    try:
        version = lsof_version()
        binary = executable_identity(LSOF)
        report["lsof"] = {"path": LSOF, "argv_field": LSOF_ARG,
                          "expected_version": LSOF_VERSION,
                          "version": version, "executable": binary}
        if not version.get("version_match"):
            raise RuntimeError("installed lsof is not the accepted 4.91/libproc build")
        if LSOF_ARG != "-F0pcRftDrin":
            raise RuntimeError("accepted structured scanner argv changed")

        with tempfile.TemporaryDirectory(prefix="r6a-r39-pin-causality-") as temporary:
            parent = Path(temporary).resolve(strict=True)
            root_path = parent / "root"
            root_path.mkdir(mode=0o700)
            child_path = root_path / "foreign-child.bin"
            with child_path.open("xb") as child:
                child.write(b"R39 pinned-versus-unpinned lsof causality fixture\n")
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
                "protocol": "Create one root and child; start one foreign helper that opens and holds the child FD; scan U then P with the same live helper and scanner; no sleep between the two scans.",
            }

            helper = subprocess.Popen(
                [sys.executable, "-c", HELPER_CODE, canonical_child],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, close_fds=True,
            )
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
            report["foreign_helper"] = {
                "handshake": helper_handshake,
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
                before = path_identity(root_path)
                child_identity_before = file_identity(child_path)
                helper_identity_pre = process_identity(helper.pid)
                pin_identity = fstat_identity(pin_fd, with_fsid=True) if pin_fd is not None else None
                if pin_fd is not None:
                    named = path_identity(root_path)
                    if ((named["device"], named["inode"], named["type"])
                            != (pin_identity["device"], pin_identity["inode"], pin_identity["type"])
                            or not pin_identity["is_directory"]):
                        raise RuntimeError("pinned root FD does not match exact named root")
                started_ns = time.monotonic_ns()
                capture = scan_lsof_path(canonical_root)
                ended_ns = time.monotonic_ns()
                after = path_identity(root_path)
                child_identity_after = file_identity(child_path)
                helper_identity_post = process_identity(helper.pid)
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
                return {
                    "variant": variant,
                    "root_pin_held": pin_fd is not None,
                    "checker_pin_fd": pin_fd,
                    "checker_pin_identity": pin_identity,
                    "checker_pin_exact_named_object": pin_fd is not None,
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

            report["fixture"]["root_identity_after_pair"] = path_identity(root_path)
            report["fixture"]["child_identity_after_pair"] = file_identity(child_path)
            report["fixture"]["root_stable_for_pair"] = (
                report["fixture"]["root_identity_after_pair"] == root_before
            )
            report["fixture"]["child_stable_for_pair"] = (
                report["fixture"]["child_identity_after_pair"] == child_before
            )
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

        report["cleanup"]["temporary_root_removed"] = root_path is not None and not root_path.exists()
        report["cleanup"]["helper_exited"] = helper is not None and helper.poll() is not None
        report["cleanup"]["all_required_checks_passed"] = (
            not report["blockers"]
            and report["cleanup"]["temporary_root_removed"]
            and report["cleanup"]["helper_exited"]
            and set(report["variants"]) == {"U", "P"}
        )
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        report["pin_status_comparison"] = {
            "unpinned_returncode": report["variants"]["U"]["capture"]["returncode"],
            "pinned_returncode": report["variants"]["P"]["capture"]["returncode"],
            "status_changed": (
                report["variants"]["U"]["capture"]["returncode"]
                != report["variants"]["P"]["capture"]["returncode"]
            ),
            "interpretation": "paired observation only; no status-1 result is treated as a complete search",
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
        report["cleanup"]["temporary_root_removed"] = root_path is not None and not root_path.exists()
        report["cleanup"]["helper_exited"] = helper is None or helper.poll() is not None

    json_path = output_dir / "R6-A-R39-PIN-CAUSALITY-EXPERIMENT.json"
    json_path.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=json_default) + "\n", encoding="utf-8")
    md_path = output_dir / "R6-A-R39-PIN-CAUSALITY-EXPERIMENT.md"
    lines = [
        "# R6-A R39 pin-causality experiment",
        "",
        f"- Run ID: `{run_id}`",
        f"- Valid paired capture and cleanup: `{report.get('cleanup', {}).get('all_required_checks_passed', False)}`",
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
        "pin_status_comparison": report.get("pin_status_comparison"),
    }, indent=2))
    return 0 if report.get("cleanup", {}).get("all_required_checks_passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
