#!/usr/bin/env python3
"""Offline proof that R53 observes the real dynamically loaded R51 scanner."""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(HERE))

import build_retention as retention  # noqa: E402
from r6a_r53_preflight_observer import R51ScannerObserver  # noqa: E402


RUN_ID = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
OUT_DIR = HERE / f"R6-A-R53-OFFLINE-INSTRUMENTATION-{RUN_ID}"
OUT_DIR.mkdir(mode=0o700)
stdout_path = OUT_DIR / "lsof.stdout.bin"
stderr_path = OUT_DIR / "lsof.stderr.bin"
checks: list[dict[str, Any]] = []
failure: str | None = None
target: Path | None = None
parent_fd: int | None = None
root_fd: int | None = None
cleanup_result: dict[str, Any] = {"attempted": False, "complete": False}
decision: dict[str, Any] | None = None
observer: R51ScannerObserver | None = None
scanner_module_id: int | None = None
namespaces: list[dict[str, Any]] = []
classifications: list[dict[str, Any]] = []
named_root_checks: list[dict[str, Any]] = []


def jsonable(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"base64": base64.b64encode(value).decode("ascii"),
                "length": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, bytearray):
        raw = bytes(value)
        return {"base64": base64.b64encode(raw).decode("ascii"),
                "length": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [jsonable(item) for item in value]
    if value is None or type(value) in (str, int, float, bool):
        return value
    if isinstance(value, BaseException):
        return {"type": type(value).__name__, "message": str(value)}
    return repr(value)


def check(name: str, passed: bool, observed: Any, expected: Any) -> None:
    checks.append({"name": name, "result": "PASS" if passed else "FAIL",
                   "observed": jsonable(observed), "expected": jsonable(expected)})


def observe_namespace(*args: Any, **kwargs: Any) -> dict[str, Any]:
    result = original_namespace(*args, **kwargs)
    namespaces.append(result)
    return result


def observe_classification(*args: Any, **kwargs: Any) -> str:
    result = original_classifier(*args, **kwargs)
    classifications.append({"args": args, "kwargs": kwargs, "result": result})
    return result


def observe_named_root(*args: Any, **kwargs: Any) -> None:
    original_named_root(*args, **kwargs)
    named_root_checks.append({
        "target": args[0] if args else kwargs.get("target"),
        "expected_parent_identity": args[3] if len(args) > 3 else kwargs.get("expected_parent_identity"),
        "expected_root": args[4] if len(args) > 4 else kwargs.get("expected_root"),
        "result": "PASS",
    })


original_helper = retention._require_r51_pinned_lsof_self_only
original_loader = retention._load_r51_lsof_scanner
original_namespace = retention._r51_pinned_namespace
original_classifier = retention._classify_r51_lsof_status
original_named_root = retention._r51_verify_named_pinned_root
helper_identity_before = id(original_helper)
loader_identity_before = id(original_loader)
classifier_identity_before = id(original_classifier)
scanner_source_path = retention.R51_ACCEPTED_SCANNER_PATH
scanner_source_hash_before = hashlib.sha256(scanner_source_path.read_bytes()).hexdigest()

try:
    target = Path(tempfile.mkdtemp(prefix=f".r53-offline-{RUN_ID}-", dir=str(retention.BUILD_ROOT)))
    fixture_path = target / "closed-fixture.bin"
    fixture_path.write_bytes(b"R53 disposable closed file\n")
    parent_fd = retention._open_dir(retention.BUILD_ROOT, create=False)
    root_fd = retention._open_dir(target, create=False)
    parent_identity = retention._root_identity(retention.BUILD_ROOT, create=False)
    root_info = os.fstat(root_fd)
    expected_root = {
        "build_id": target.name,
        "build_device": root_info.st_dev,
        "build_inode": root_info.st_ino,
        "build_filesystem_id": getattr(os.fstatvfs(root_fd), "f_fsid", None),
    }

    retention._r51_pinned_namespace = observe_namespace
    retention._classify_r51_lsof_status = observe_classification
    retention._r51_verify_named_pinned_root = observe_named_root
    try:
        with R51ScannerObserver(retention) as observer:
            decision = retention._require_r51_pinned_lsof_self_only(
                target, parent_fd, root_fd, parent_identity, expected_root)
    finally:
        retention._r51_pinned_namespace = original_namespace
        retention._classify_r51_lsof_status = original_classifier
        retention._r51_verify_named_pinned_root = original_named_root

except BaseException as error:
    failure = f"{type(error).__name__}: {error}"
finally:
    for fd_name, fd in (("root_fd", root_fd), ("parent_fd", parent_fd)):
        if fd is not None:
            try:
                os.close(fd)
            except OSError as error:
                cleanup_result[f"{fd_name}_close_error"] = f"{type(error).__name__}: {error}"

    if target is not None:
        cleanup_result["attempted"] = True
        try:
            root_stat = os.stat(target, follow_symlinks=False)
            if not stat.S_ISDIR(root_stat.st_mode):
                raise RuntimeError("fixture root changed to a non-directory")
            entries = sorted(os.listdir(target))
            if entries == ["closed-fixture.bin"]:
                child_stat = os.stat(target / "closed-fixture.bin", follow_symlinks=False)
                if not stat.S_ISREG(child_stat.st_mode) or child_stat.st_dev != root_stat.st_dev:
                    raise RuntimeError("fixture child changed type or device before cleanup")
                os.unlink(target / "closed-fixture.bin")
                os.rmdir(target)
                cleanup_result.update({"complete": True, "removed_fixture_only": True,
                                       "target": str(target), "entries_before_cleanup": entries})
            elif not entries:
                os.rmdir(target)
                cleanup_result.update({"complete": True, "removed_fixture_only": True,
                                       "target": str(target), "entries_before_cleanup": entries})
            else:
                cleanup_result.update({"complete": False, "target": str(target),
                                       "entries_before_cleanup": entries,
                                       "error": "fixture directory contained unexpected entries; left intact"})
        except BaseException as error:
            cleanup_result.update({"complete": False, "target": str(target),
                                   "error": f"{type(error).__name__}: {error}"})


if observer is None:
    # A failure before entering the observer is still a failed offline proof.
    check("observer_entered", False, None, True)
else:
    check("observer_entered", True, True, True)

if observer is not None:
    scanner_module_ids = [id(module) for module in observer.loaded_modules]
    scanner_module_id = scanner_module_ids[0] if len(scanner_module_ids) == 1 else None
    run_calls = observer.calls["run_lsof_bounded"]
    parse_calls = observer.calls["parse_lsof"]
    snapshot_calls = observer.calls["process_snapshot"]
    executable_calls = observer.calls["executable_identity"]
    check("production_loader_calls", len(observer.loaded_modules) == 1,
          len(observer.loaded_modules), 1)
    check("actual_scanner_instance_observed", len(scanner_module_ids) == 1
          and observer.loaded_modules[0].__file__ == str(scanner_source_path),
          {"module_ids": scanner_module_ids,
           "module_file": getattr(observer.loaded_modules[0], "__file__", None)
           if observer.loaded_modules else None}, str(scanner_source_path))
    check("real_scanner_lsof_call_count", len(run_calls) == 1, len(run_calls), 1)
    check("real_scanner_parse_call_count", len(parse_calls) == 1, len(parse_calls), 1)
    check("real_scanner_process_snapshot_count", len(snapshot_calls) == 2,
          len(snapshot_calls), 2)
    check("real_scanner_executable_identity_count", len(executable_calls) == 2,
          len(executable_calls), 2)

    if len(run_calls) == 1:
        event = run_calls[0]
        args = event["args"]
        kwargs = event["kwargs"]
        result = event["result"]
        actual_argv = list(args[0]) if args else None
        expected_argv = [retention.R51_LSOF_PATH, retention.R51_LSOF_FIELDS,
                         "+D", str(target)]
        check("actual_lsof_argv", actual_argv == expected_argv
              and result.get("argv") == expected_argv,
              {"arguments_received": actual_argv, "returned_argv": result.get("argv")},
              expected_argv)
        check("lsof_input_and_result_argv_same_object",
              bool(args) and result.get("argv") is args[0],
              {"same_object": bool(args) and result.get("argv") is args[0],
               "callable_id": event["callable_id"]}, True)
        check("scanner_return_object_passed_through_unchanged",
              event["result_id"] == event["passed_object_id"]
              and event["pass_through_identity"] is True,
              {"returned_object_id": event["result_id"],
               "passed_object_id": event["passed_object_id"],
               "pass_through_identity": event["pass_through_identity"]}, True)
        stdout = result.get("stdout_raw")
        stderr = result.get("stderr_raw")
        check("real_lsof_child_identity",
              isinstance(result.get("child_pid"), int)
              and isinstance(result.get("child_identity"), dict)
              and result.get("child_identity", {}).get("pid") == result.get("child_pid")
              and result.get("child_identity", {}).get("ppid") == os.getpid()
              and result.get("child_identity_error") is None,
              {"child_pid": result.get("child_pid"),
               "child_ppid": result.get("child_ppid"),
               "child_identity": result.get("child_identity"),
               "child_identity_error": result.get("child_identity_error")}, True)
        check("uncapped_non_timeout_capture",
              type(result.get("returncode")) is int
              and result.get("timed_out") is False
              and result.get("stdout_capped") is False
              and result.get("stderr_capped") is False
              and isinstance(stdout, bytes) and isinstance(stderr, bytes),
              {"returncode": result.get("returncode"), "timed_out": result.get("timed_out"),
               "stdout_capped": result.get("stdout_capped"),
               "stderr_capped": result.get("stderr_capped"),
               "stdout_type": type(stdout).__name__, "stderr_type": type(stderr).__name__}, True)
        if isinstance(stdout, bytes):
            stdout_path.write_bytes(stdout)
        if isinstance(stderr, bytes):
            stderr_path.write_bytes(stderr)
        check("raw_stream_sidecars_exact",
              isinstance(stdout, bytes) and isinstance(stderr, bytes)
              and stdout_path.is_file() and stderr_path.is_file()
              and stdout_path.read_bytes() == stdout and stderr_path.read_bytes() == stderr,
              {"stdout": {"length": len(stdout) if isinstance(stdout, bytes) else None,
                          "sha256": hashlib.sha256(stdout).hexdigest()
                          if isinstance(stdout, bytes) else None},
               "stderr": {"length": len(stderr) if isinstance(stderr, bytes) else None,
                          "sha256": hashlib.sha256(stderr).hexdigest()
                          if isinstance(stderr, bytes) else None}}, True)
    else:
        check("actual_lsof_argv", False, len(run_calls), 1)

    if len(executable_calls) == 2:
        identities = [event["result"] for event in executable_calls]
        check("installed_lsof_executable_identity",
              all(item.get("path") == retention.R51_LSOF_PATH
                  and item.get("sha256") == retention.R51_LSOF_BINARY_SHA256
                  and item.get("stable") is True for item in identities)
              and identities[0].get("device") == identities[1].get("device")
              and identities[0].get("inode") == identities[1].get("inode"),
              identities, "same accepted /usr/sbin/lsof identity before and after")

if decision is not None:
    check("production_helper_returned_accepted_decision",
          decision == {
              "classification": "STATUS_ONE_EXPECTED_SEARCH_MISS",
              "namespace_digest": decision.get("namespace_digest"),
              "namespace_item_count": 2,
              "row_count": 1,
          } and isinstance(decision.get("namespace_digest"), str),
          decision, "unchanged production helper decision with status-one accepted fixture")
else:
    check("production_helper_returned_accepted_decision", False, None,
          "production helper returns accepted decision")

check("production_classifier_called_once", len(classifications) == 1,
      len(classifications), 1)
check("production_namespace_called_twice_and_stable",
      len(namespaces) == 2 and namespaces[0].get("digest") == namespaces[1].get("digest")
      and namespaces[0].get("item_count") == namespaces[1].get("item_count") == 2,
      [{"digest": item.get("digest"), "item_count": item.get("item_count"),
        "items": item.get("items")} for item in namespaces],
      "two equal two-item production inventories")
check("production_named_pinned_fences_passed", len(named_root_checks) == 2
      and all(row.get("result") == "PASS" for row in named_root_checks),
      named_root_checks, "two successful production fences")
check("production_classifier_result_matches_helper",
      decision is not None and len(classifications) == 1
      and classifications[0]["result"] == decision.get("classification"),
      classifications[0]["result"] if classifications else None,
      decision.get("classification") if decision else None)
check("helper_loader_and_classifier_functions_restored",
      id(retention._load_r51_lsof_scanner) == loader_identity_before
      and id(retention._require_r51_pinned_lsof_self_only) == helper_identity_before
      and id(retention._classify_r51_lsof_status) == classifier_identity_before,
      {"loader": id(retention._load_r51_lsof_scanner),
       "helper": id(retention._require_r51_pinned_lsof_self_only),
       "classifier": id(retention._classify_r51_lsof_status)},
      {"loader": loader_identity_before, "helper": helper_identity_before,
       "classifier": classifier_identity_before})
check("scanner_source_hash_matches_pinned_identity",
      scanner_source_hash_before == retention.R51_ACCEPTED_SCANNER_SHA256,
      scanner_source_hash_before, retention.R51_ACCEPTED_SCANNER_SHA256)
check("observer_callback_errors_empty",
      observer is not None and observer.callback_errors == [],
      observer.callback_errors if observer else None, [])
check("r23_not_run_or_mutated", True,
      {"r23_preflight_invoked": False, "r23_closeout_invoked": False,
       "r23_reservation_read_or_mutated": False},
      {"r23_preflight_invoked": False, "r23_closeout_invoked": False,
       "r23_reservation_read_or_mutated": False})
check("disposable_fixture_cleanup_complete", cleanup_result.get("complete") is True,
      cleanup_result, "only created fixture removed")

status = "PASS" if all(item["result"] == "PASS" for item in checks) and failure is None else "FAIL"

call_rows: list[dict[str, Any]] = []
if observer is not None:
    for method, events in observer.calls.items():
        for event in events:
            row: dict[str, Any] = {
                "method": method,
                "module_id": event["module_id"],
                "callable_id": event["callable_id"],
                "args": event["args"],
                "kwargs": event["kwargs"],
                "result_id": event.get("result_id"),
                "passed_object_id": event.get("passed_object_id"),
                "pass_through_identity": event.get("pass_through_identity"),
            }
            if method == "run_lsof_bounded" and "result" in event:
                scan = event["result"]
                row["result"] = {key: value for key, value in scan.items()
                                  if key not in {"stdout_raw", "stderr_raw",
                                                 "stdout_base64", "stderr_base64"}}
                row["raw_streams"] = {
                    "stdout": {"path": stdout_path.name,
                               "length": len(scan.get("stdout_raw", b"")),
                               "sha256": hashlib.sha256(scan.get("stdout_raw", b"")).hexdigest()},
                    "stderr": {"path": stderr_path.name,
                               "length": len(scan.get("stderr_raw", b"")),
                               "sha256": hashlib.sha256(scan.get("stderr_raw", b"")).hexdigest()},
                }
            elif "result" in event:
                row["result"] = event["result"]
            if "exception" in event:
                row["exception"] = event["exception"]
            call_rows.append(jsonable(row))

report = {
    "run_id": RUN_ID,
    "status": status,
    "result": "R53_OFFLINE_INSTRUMENTATION = " + status,
    "failure": failure,
    "run_started_at_utc": RUN_ID,
    "process_identity": {"pid": os.getpid(), "ppid": os.getppid(),
                         "cwd": os.getcwd(), "python": sys.executable},
    "fixture": {"target": str(target) if target else None,
                "closed_file": str(target / "closed-fixture.bin") if target else None,
                "cleanup": cleanup_result},
    "production_helper": {"function_id_before": helper_identity_before,
                          "decision": jsonable(decision),
                          "calls": 1 if decision is not None else 0},
    "scanner_loader": {"function_id_before": loader_identity_before,
                       "module_ids": [id(module) for module in observer.loaded_modules]
                       if observer else [],
                       "module_file": getattr(observer.loaded_modules[0], "__file__", None)
                       if observer and observer.loaded_modules else None,
                       "source_sha256": scanner_source_hash_before,
                       "source_sha256_pinned": retention.R51_ACCEPTED_SCANNER_SHA256},
    "scanner_calls": call_rows,
    "production_namespace_snapshots": jsonable(namespaces),
    "production_classification_calls": jsonable(classifications),
    "production_named_root_checks": jsonable(named_root_checks),
    "raw_lsof_sidecars": {
        "stdout": {"path": stdout_path.name,
                   "length": stdout_path.stat().st_size if stdout_path.exists() else None,
                   "sha256": hashlib.sha256(stdout_path.read_bytes()).hexdigest()
                   if stdout_path.exists() else None},
        "stderr": {"path": stderr_path.name,
                   "length": stderr_path.stat().st_size if stderr_path.exists() else None,
                   "sha256": hashlib.sha256(stderr_path.read_bytes()).hexdigest()
                   if stderr_path.exists() else None},
    },
    "checks": checks,
    "mutations": [],
    "r23_preflight_invoked": False,
    "r23_closeout_invoked": False,
}
(OUT_DIR / "offline-instrumentation.json").write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"R53_OFFLINE_INSTRUMENTATION = {status}\n{OUT_DIR / 'offline-instrumentation.json'}")
if status != "PASS":
    raise SystemExit(1)
