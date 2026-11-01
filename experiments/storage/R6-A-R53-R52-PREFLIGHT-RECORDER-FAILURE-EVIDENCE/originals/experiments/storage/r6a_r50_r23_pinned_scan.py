#!/usr/bin/env python3
"""One fresh, read-only R23 self-pin scan using the accepted R50 scanner core."""
from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import stat
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
STORAGE = ROOT / "experiments/storage"
sys.path.insert(0, str(STORAGE))
import r6a_r33_live_open_capture as scanner  # noqa: E402

PRECHECK = STORAGE / "R6-A-R32-R23-PARENT-ROOT-PRECHECK-20261021.json"
PRECHECK_SHA256 = "2a648916c9042309b7feb10714e545eb6cefea10a01345917031de9e56dddeed"
R40_VERSION = STORAGE / "R6-A-R40-LSOF-PRECHECK-20261021T182103Z/R6-A-R40-LSOF-PRECHECK-VALIDATION.json"
R50_PROBE = STORAGE / "R6-A-R50-LSOF-DISPOSABLE-PROBE-20261022T082841Z/probe.json"
R50_PROBE_ANALYSIS = STORAGE / "R6-A-R50-LSOF-DISPOSABLE-PROBE-ANALYSIS.md"
RUN_ID = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-r50-r23"
PREFIX = STORAGE / f"R6-A-R50-R23-PINNED-SCAN-{RUN_ID}"
OUTPUTS = {
    "json": PREFIX.with_suffix(".json"),
    "markdown": PREFIX.with_suffix(".md"),
    "stdout": Path(str(PREFIX) + "-lsof.stdout.bin"),
    "stderr": Path(str(PREFIX) + "-lsof.stderr.bin"),
    "complete": Path(str(PREFIX) + "-COMPLETE.json"),
}


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def read_json(path: Path, *, expected_sha256: str | None = None) -> tuple[dict[str, Any], bytes]:
    raw = scanner.read_nofollow(str(path))
    if expected_sha256 is not None and digest(raw) != expected_sha256:
        raise scanner.CaptureError(f"evidence hash changed: {path}")
    value = json.loads(raw)
    if type(value) is not dict:
        raise scanner.CaptureError(f"evidence is not a JSON object: {path}")
    return value, raw


def wire(value: Any) -> Any:
    if isinstance(value, bytes):
        return {
            "encoding": "base64",
            "data": base64.b64encode(value).decode("ascii"),
            "length": len(value),
            "sha256": digest(value),
        }
    if isinstance(value, dict):
        return {str(key): wire(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [wire(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    return repr(value)


def write_new(path: Path, content: bytes) -> dict[str, Any]:
    parent_fd = scanner.open_dir_nofollow(str(path.parent))
    try:
        fd = os.open(
            path.name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_fd,
        )
        try:
            view = memoryview(content)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short evidence write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return {"path": str(path), "bytes": len(content), "sha256": digest(content)}


def markdown(report: dict[str, Any]) -> bytes:
    rows = report.get("rows", [])
    lines = [
        "# R50 fresh R23 pinned scan",
        "",
        f"- Run ID: `{report['run_id']}`",
        f"- Result: **{report.get('classification')}**",
        f"- Checker PID: `{report.get('checker', {}).get('pid')}`",
        f"- Exact root pin FD: `{report.get('root_pin', {}).get('fd')}`",
        f"- Canonical root: `{report.get('canonical_root')}`",
        f"- lsof scan invocation count: `{report.get('lsof_scan_invocation_count')}`",
        f"- Return code: `{report.get('lsof', {}).get('returncode')}`",
        f"- Complete status-0 contract: `{report.get('scan_contract', {}).get('accepted')}`",
        f"- Exact pin rows: `{report.get('exact_pin_match_count')}`; total rows: `{len(rows)}`",
        f"- Blockers: `{report.get('blockers')}`",
        "",
        "## Structured rows",
        "",
        "| PID | FD | Type | Device | Inode | Decoded name | Classification |",
        "|---:|---|---|---:|---:|---|---|",
    ]
    for row in rows:
        name = row.get("name_decoded", "")
        lines.append(
            "| " + " | ".join(
                str(value).replace("|", "\\|")
                for value in (
                    row.get("pid"), row.get("fd"), row.get("fd_type"),
                    row.get("device"), row.get("inode"), name,
                    row.get("classification"),
                )
            ) + " |"
        )
    lines.extend([
        "",
        "## Identity binding",
        "",
        "```json",
        json.dumps(wire({
            "root_pin": report.get("root_pin"),
            "root_revalidation": report.get("root_revalidation"),
            "lsof_executable": report.get("lsof_executable"),
            "scan_contract": report.get("scan_contract"),
            "version_evidence": report.get("version_evidence"),
        }), sort_keys=True, indent=2),
        "```",
        "",
        "No R23 registry, marker, build-root, or closeout mutation was performed.",
        "",
    ])
    return "\n".join(lines).encode("utf-8")


def main() -> int:
    report: dict[str, Any] = {
        "schema": "r6a-r50-r23-pinned-scan-v1",
        "run_id": RUN_ID,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "scanner_path": str(Path(scanner.__file__).resolve()),
        "scanner_sha256": digest(Path(scanner.__file__).read_bytes()),
        "runner_path": str(Path(__file__).resolve()),
        "runner_sha256": digest(Path(__file__).read_bytes()),
        "build_id": scanner.BUILD_ID,
        "owner_experiment": scanner.OWNER,
        "expected_root": scanner.EXPECTED_ROOT,
        "closeout_invoked": False,
        "mutations": [],
        "lsof_scan_invocation_count": 0,
        "lsof_scan_call_entered": False,
        "rows": [],
        "blockers": [],
    }
    opened: dict[str, int] = {}
    parent_fd: int | None = None
    allocation_dir_fd: int | None = None
    lease_dir_fd: int | None = None
    raw_stdout = b""
    raw_stderr = b""
    cleanup_errors: list[str] = []
    try:
        for path in OUTPUTS.values():
            if path.exists():
                raise scanner.CaptureError(f"R50 output already exists: {path}")

        precheck, precheck_raw = read_json(PRECHECK, expected_sha256=PRECHECK_SHA256)
        parent_rows = [item for item in precheck.get("checks", []) if item.get("phase") == "parent_root_identity"]
        if not (
            precheck.get("classification") == "PARENT_ROOT_IDENTITY_PASS"
            and precheck.get("all_parent_root_rows_evaluated") is True
            and precheck.get("all_parent_root_rows_serialized") is True
            and precheck.get("missing_identity_row_ids") == []
            and len(parent_rows) == 40
            and all(item.get("result") == "PASS" for item in parent_rows)
        ):
            raise scanner.CaptureError("reviewed R23 parent/root precheck no longer has 40 passing identity rows")
        report["precheck"] = {
            "path": str(PRECHECK),
            "sha256": digest(precheck_raw),
            "classification": precheck["classification"],
            "parent_root_row_count": len(parent_rows),
        }

        admission_path = ROOT / "experiments/storage/manifests/R6-A-R23-DUAL-LINEAGE-20261020T214331Z.json"
        admission_raw = scanner.read_nofollow(str(admission_path))
        if digest(admission_raw) != precheck.get("admission_sha256"):
            raise scanner.CaptureError("R23 signed admission bytes differ from the reviewed precheck")
        report["admission"] = {"path": str(admission_path), "sha256": digest(admission_raw)}

        canonical_parent = os.path.realpath(scanner.EXPECTED_PARENT, strict=True)
        canonical_root = os.path.realpath(scanner.EXPECTED_ROOT, strict=True)
        if canonical_parent != scanner.EXPECTED_PARENT or canonical_root != scanner.EXPECTED_ROOT:
            raise scanner.CaptureError("canonical R23 parent/root differs from exact reviewed paths")
        report["canonical_root"] = canonical_root

        checker = scanner.process_snapshot()
        report["checker"] = checker

        parent_fd = scanner.open_dir_nofollow(scanner.EXPECTED_PARENT)
        parent_before = scanner.parent_identity(parent_fd)
        if parent_before != scanner.EXPECTED_PARENT_IDENTITY:
            raise scanner.CaptureError("approved R23 parent identity differs from the reviewed identity")
        if parent_before != precheck["parent_capture"]["parent_fd_identity"]:
            raise scanner.CaptureError("approved R23 parent differs from the exact precheck parent FD")
        report["parent"] = {"fd": parent_fd, "identity_before": parent_before}

        checks = {item["id"]: item for item in precheck["checks"]}
        prior = {
            "allocation_device": checks["ALLOC_LOCK_NAMED_FD_DEVICE"]["observed"],
            "allocation_inode": checks["ALLOC_LOCK_NAMED_FD_INODE"]["observed"],
            "retention_device": checks["RETENTION_LOCK_NAMED_FD_DEVICE"]["observed"],
            "retention_inode": checks["RETENTION_LOCK_NAMED_FD_INODE"]["observed"],
        }
        allocation_dir_fd = scanner.open_dir_nofollow("/Users/nima/Library/Caches/FGMetal")
        alloc_fd, allocation = scanner.open_lock(allocation_dir_fd, ".storage-allocation.lock", prior, "allocation")
        opened["allocation_lock"] = alloc_fd
        lease_dir_fd = scanner.open_dir_nofollow(str(STORAGE / "build-leases"))
        retain_fd, retention = scanner.open_lock(lease_dir_fd, ".retention.lock", prior, "retention")
        opened["retention_lock"] = retain_fd
        report["locks"] = {"allocation": allocation, "retention": retention}
        for label, item in report["locks"].items():
            if not item["checks"] or not all(value is True for value in item["checks"].values() if isinstance(value, bool)):
                raise scanner.CaptureError(f"R23 {label} lock identity or nonblocking exclusive lock check failed")

        root_name = os.path.basename(scanner.EXPECTED_ROOT)
        named_before = scanner.stat_at(parent_fd, root_name)
        root_fd = os.open(
            root_name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        opened["root_pin"] = root_fd
        pin_before = scanner.fstat_identity(root_fd, with_fsid=True)
        root_tuple = (named_before["device"], named_before["inode"], named_before["type"])
        pin_tuple = (pin_before["device"], pin_before["inode"], pin_before["type"])
        expected_tuple = (
            scanner.EXPECTED_ROOT_IDENTITY["device"],
            scanner.EXPECTED_ROOT_IDENTITY["inode"],
            scanner.EXPECTED_ROOT_IDENTITY["type"],
        )
        if (
            root_tuple != expected_tuple
            or pin_tuple != expected_tuple
            or pin_before["filesystem_id"] != scanner.EXPECTED_ROOT_IDENTITY["filesystem_id"]
        ):
            raise scanner.CaptureError("named and pinned R23 root do not match the exact admitted identity")
        report["root_pin"] = {
            "fd": root_fd,
            "role": "exact R23 root identity pin",
            "canonical_root": canonical_root,
            "named_before": named_before,
            "fstat_before": pin_before,
        }

        version, _version_raw = read_json(R40_VERSION)
        version_record = version.get("lsof", {})
        version_capture = version_record.get("version", {})
        version_exec = version_record.get("executable", {})
        version_stderr_meta = version_capture.get("stderr_sidecar", {})
        version_stderr_path = ROOT / version_stderr_meta.get("path", "")
        version_stderr = scanner.read_nofollow(str(version_stderr_path))
        if (
            version_capture.get("returncode") != 0
            or version_capture.get("version_match") is not True
            or version_exec.get("path") != scanner.LSOF
            or "revision: 4.91" not in version_capture.get("text", "")
            or "configuration info: libproc-based" not in version_capture.get("text", "")
            or version_capture.get("stdout_bytes") != 0
            or digest(version_stderr) != version_stderr_meta.get("sha256")
            or digest(version_stderr) != version_capture.get("stderr_sha256")
            or len(version_stderr) != version_capture.get("stderr_bytes")
        ):
            raise scanner.CaptureError("preserved R40 lsof 4.91/libproc version evidence failed integrity checks")

        probe, _probe_raw = read_json(R50_PROBE)
        probe_stdout_path = ROOT / probe["stdout"]["path"]
        probe_stderr_path = ROOT / probe["stderr"]["path"]
        probe_stdout = scanner.read_nofollow(str(probe_stdout_path))
        probe_stderr = scanner.read_nofollow(str(probe_stderr_path))
        probe_analysis = scanner.read_nofollow(str(R50_PROBE_ANALYSIS))
        if (
            probe.get("invocation_count") != 1
            or probe.get("field_mode") != scanner.LSOF_ARG
            or probe.get("returncode") != 1
            or probe.get("timeout") is not None
            or probe.get("lsof_executable", {}).get("path") != scanner.LSOF
            or probe.get("lsof_executable", {}).get("realpath") != scanner.LSOF
            or digest(probe_stdout) != probe["stdout"]["sha256"]
            or len(probe_stdout) != probe["stdout"]["length"]
            or digest(probe_stderr) != probe["stderr"]["sha256"]
            or len(probe_stderr) != probe["stderr"]["length"]
            or len(probe_stderr) != 0
            or probe.get("all_named_and_fd_identities_stable") is not True
        ):
            raise scanner.CaptureError("preserved R50 lsof name-probe evidence failed integrity checks")

        executable_before = scanner.executable_identity(scanner.LSOF)
        probe_exe = probe["lsof_executable"]
        executable_named_stat = os.stat(scanner.LSOF, follow_symlinks=False)
        if (
            executable_before["path"] != scanner.LSOF
            or executable_before["stable"] is not True
            or executable_before["sha256"] != version_exec.get("sha256")
            or executable_before["device"] != probe_exe.get("device")
            or executable_before["inode"] != probe_exe.get("inode")
            or executable_before["size"] != probe_exe.get("size")
            or executable_named_stat.st_mtime_ns != probe_exe.get("mtime_ns")
            or executable_before["owner_uid"] != version_exec.get("owner_uid")
            or executable_before["mode"] != version_exec.get("mode")
            or executable_before["owner_uid"] != 0
            or executable_before["mode"] & 0o022
        ):
            raise scanner.CaptureError("current lsof binary is not the exact root-owned R40/R50 reviewed executable")
        report["version_evidence"] = {
            "source": str(R40_VERSION),
            "revision": "4.91",
            "configuration": "libproc-based",
            "version_query_returncode": version_capture["returncode"],
            "version_text_stream": "stderr",
            "version_stderr_sha256": digest(version_stderr),
            "live_version_query_invoked": False,
            "executable_hash_matches_R40": True,
            "executable_identity_matches_R50_probe": True,
        }
        report["lsof_name_probe"] = {
            "probe_json": str(R50_PROBE),
            "probe_json_sha256": digest(scanner.read_nofollow(str(R50_PROBE))),
            "analysis_report": str(R50_PROBE_ANALYSIS),
            "analysis_sha256": digest(probe_analysis),
            "invocation_count": probe["invocation_count"],
            "field_mode": probe["field_mode"],
            "returncode": probe["returncode"],
            "stdout_bytes": len(probe_stdout),
            "stdout_sha256": digest(probe_stdout),
            "stderr_bytes": len(probe_stderr),
            "stderr_sha256": digest(probe_stderr),
            "all_named_and_fd_identities_stable": probe["all_named_and_fd_identities_stable"],
        }
        report["lsof_executable"] = {"before": executable_before}

        report["lsof_scan_invocation_count"] = 1
        report["lsof_scan_call_entered"] = True
        capture = scanner.scan_lsof_path(canonical_root)
        raw_stdout = capture.pop("stdout_raw")
        raw_stderr = capture.pop("stderr_raw")
        rows = capture.pop("rows")
        stream = capture.pop("stream")
        report["lsof"] = capture
        report["stream"] = stream
        report["parsed_rows"] = wire(rows)

        exact_rows = []
        for row in rows:
            exact = (
                row.get("pid") == checker["pid"]
                and row.get("ppid") == checker["ppid"]
                and row.get("fd_number") == root_fd
                and row.get("fd_raw") == str(root_fd).encode("ascii")
                and row.get("fd_type_raw") == b"DIR"
                and row.get("device") == pin_before["device"]
                and row.get("inode") == pin_before["inode"]
                and row.get("name_decoded") == os.fsencode(canonical_root)
            )
            row["classification"] = "EXPECTED_IDENTITY_PIN" if exact else (
                "UNEXPECTED_SAME_PROCESS_OBJECT" if row.get("pid") == checker["pid"] else "FOREIGN_HOLDER"
            )
            row["identity_match"] = exact
            if exact:
                exact_rows.append(row)
        report["exact_pin_match_count"] = len(exact_rows)
        report["rows"] = [scanner.row_for_report(row) for row in rows]

        scan_contract = {
            "returncode_zero": capture.get("returncode") == 0,
            "stderr_empty": capture.get("stderr_total_bytes") == 0 and raw_stderr == b"",
            "complete_stream": stream.get("complete") is True,
            "all_rows_parse": all(row.get("parser_status") == "PASS" for row in rows),
            "stdout_fully_captured": capture.get("stdout_total_bytes") == capture.get("stdout_bytes_captured") and not capture.get("stdout_capped"),
            "stderr_fully_captured": capture.get("stderr_total_bytes") == capture.get("stderr_bytes_captured") and not capture.get("stderr_capped"),
            "not_timed_out": capture.get("timed_out") is False,
            "child_identity_valid": "child_identity_error" not in capture,
            "scanner_scan_complete": capture.get("scan_complete") is True,
            "exactly_one_row": len(rows) == 1,
            "exactly_one_expected_pin": len(exact_rows) == 1,
        }
        scan_contract["accepted"] = all(scan_contract.values())
        report["scan_contract"] = scan_contract
        report["lsof"] = capture

        checker_after = scanner.process_identity(checker["pid"])
        report["checker_after"] = checker_after
        report["checker_identity_stable"] = (
            checker_after.get("pid") == checker["pid"]
            and checker_after.get("ppid") == checker["ppid"]
            and checker_after.get("start_identity") == checker["start_identity"]
        )
        executable_after = scanner.executable_identity(scanner.LSOF)
        report["lsof_executable"]["after"] = executable_after
        report["lsof_executable"]["stable"] = executable_before == executable_after

        named_after = scanner.stat_at(parent_fd, root_name)
        pin_after = scanner.fstat_identity(root_fd, with_fsid=True)
        parent_after_fd = scanner.parent_identity(parent_fd)
        parent_path_fd = scanner.open_dir_nofollow(scanner.EXPECTED_PARENT)
        try:
            parent_after_path = scanner.parent_identity(parent_path_fd)
        finally:
            os.close(parent_path_fd)
        report["root_revalidation"] = {
            "named_before": named_before,
            "named_after": named_after,
            "pinned_before": pin_before,
            "pinned_after": pin_after,
            "named_equals_pin_after": (
                (named_after["device"], named_after["inode"], named_after["type"])
                == (pin_after["device"], pin_after["inode"], pin_after["type"])
            ),
            "named_stable": named_after == named_before,
            "pinned_stable": pin_after == pin_before,
            "parent_fd_before": parent_before,
            "parent_fd_after": parent_after_fd,
            "parent_path_after": parent_after_path,
            "parent_stable": parent_after_fd == parent_before == parent_after_path,
        }
        for label, item in report["locks"].items():
            lock_fd = item["descriptor"]
            current_fd = scanner.fstat_identity(lock_fd)
            directory_fd = allocation_dir_fd if label == "allocation" else lease_dir_fd
            current_named = scanner.stat_at(directory_fd, item["path_name"])
            item["after_scan"] = {
                "descriptor": current_fd,
                "named": current_named,
                "stable": (
                    current_fd["device"] == item["opened"]["device"]
                    and current_fd["inode"] == item["opened"]["inode"]
                    and current_named["device"] == current_fd["device"]
                    and current_named["inode"] == current_fd["inode"]
                ),
            }

        if not report["checker_identity_stable"]:
            report["blockers"].append("checker PID/PPID/start identity changed across the scan")
        if not report["lsof_executable"]["stable"]:
            report["blockers"].append("lsof executable identity changed across the scan")
        if not report["root_revalidation"]["named_equals_pin_after"] or not report["root_revalidation"]["named_stable"] or not report["root_revalidation"]["pinned_stable"]:
            report["blockers"].append("named R23 root or pinned root identity changed across the scan")
        if not report["root_revalidation"]["parent_stable"]:
            report["blockers"].append("approved R23 parent identity changed across the scan")
        for label, item in report["locks"].items():
            if not item.get("after_scan", {}).get("stable"):
                report["blockers"].append(f"{label} lock name/descriptor identity changed across the scan")
        if not scan_contract["accepted"]:
            report["blockers"].append("structured R23 scan did not meet the complete status-zero empty-stderr contract")
        if len(rows) != 1 or len(exact_rows) != 1:
            report["blockers"].append(f"SELF_PIN_ONLY requires exactly one exact root-pin row; rows={len(rows)}, matches={len(exact_rows)}")
        report["classification"] = "R23_LIVE_OPEN_BLOCKED" if report["blockers"] else "SELF_PIN_ONLY"
    except BaseException as error:
        report["classification"] = "R23_LIVE_OPEN_BLOCKED"
        report["blockers"].append(f"capture failure: {type(error).__name__}: {error}")
    finally:
        report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
        for label, fd in reversed(list(opened.items())):
            try:
                if label in {"allocation_lock", "retention_lock"}:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError as error:
                cleanup_errors.append(f"{label}: {type(error).__name__}: {error}")
        for fd in (lease_dir_fd, allocation_dir_fd, parent_fd):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError as error:
                    cleanup_errors.append(f"directory fd {fd}: {type(error).__name__}: {error}")
        report["cleanup_errors"] = cleanup_errors
        if cleanup_errors:
            report["blockers"].append("scanner preflight/run descriptor cleanup was incomplete")
            report["classification"] = "R23_LIVE_OPEN_BLOCKED"

    stdout_meta = write_new(OUTPUTS["stdout"], raw_stdout)
    stderr_meta = write_new(OUTPUTS["stderr"], raw_stderr)
    report["raw_lsof_sidecars"] = {"stdout": stdout_meta, "stderr": stderr_meta}
    json_bytes = (json.dumps(wire(report), sort_keys=True, indent=2, ensure_ascii=True) + "\n").encode("utf-8")
    markdown_bytes = markdown(report)
    json_meta = write_new(OUTPUTS["json"], json_bytes)
    markdown_meta = write_new(OUTPUTS["markdown"], markdown_bytes)
    manifest = {
        "schema": "r6a-r50-r23-pinned-scan-complete-v1",
        "run_id": RUN_ID,
        "classification": report.get("classification"),
        "files": {"json": json_meta, "markdown": markdown_meta, "stdout": stdout_meta, "stderr": stderr_meta},
        "runner_sha256": report["runner_sha256"],
        "scanner_sha256": report["scanner_sha256"],
    }
    manifest_bytes = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("utf-8")
    manifest_meta = write_new(OUTPUTS["complete"], manifest_bytes)
    print(json.dumps({
        "classification": report.get("classification"),
        "run_id": RUN_ID,
        "blockers": report.get("blockers"),
        "scan_invocation_count": report.get("lsof_scan_invocation_count"),
        "report": json_meta,
        "markdown": markdown_meta,
        "complete_manifest": manifest_meta,
    }, sort_keys=True))
    return 0 if report.get("classification") == "SELF_PIN_ONLY" else 1


if __name__ == "__main__":
    raise SystemExit(main())
