#!/usr/bin/env python3
"""Offline exact-shape success/failure report serialization proof for R54."""
from __future__ import annotations

import base64
import enum
import hashlib
import json
import os
from pathlib import Path
from typing import Any

from r6a_r54_preflight_serialization import (
    assert_json_domain,
    assert_success_report_shape,
    serialize_report,
)


class ProbeEnum(enum.Enum):
    ACCEPTED = "accepted"


class Unsupported:
    def __str__(self) -> str:
        raise AssertionError("generic str() fallback was called")

    def __repr__(self) -> str:
        raise AssertionError("generic repr() fallback was called")


def _assert(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def build_success_report() -> dict[str, Any]:
    root = Path("/tmp/fgmetal-r54-offline/build-r6a")
    binary = b"\x00R23-row\xff"
    namespace_state = (41, 73, 1, 0)
    parsed_rows = [{
        "pid": 31841,
        "fd": 17,
        "device": 41,
        "inode": 73,
        "path": b"/tmp/fgmetal-r54-offline/build-r6a/file.bin",
        "path_display": "/tmp/fgmetal-r54-offline/build-r6a/file.bin",
        "lsof_type": b"REG",
        "suffix": b" (deleted)",
    }]
    scan = {
        "argv": ["/usr/sbin/lsof", "+D", os.fspath(root)],
        "returncode": 1,
        "child_pid": 9021,
        "timed_out": False,
        "stdout_raw": binary,
        "stderr_raw": b"",
    }
    named_check = {
        "result": "PASS",
        "target": root,
        "parent_fd": 31,
        "root_fd": 32,
        "expected_parent_identity": {"device": 41, "inode": 10},
        "expected_root_identity": {"build_id": "r6a-r23", "build_inode": 73},
        "observed": {
            "named_parent_identity": {"device": 41, "inode": 10},
            "pinned_parent_identity": {"device": 41, "inode": 10},
            "named_root": {"device": 41, "inode": 73, "mode": 16384},
            "pinned_root": {"device": 41, "inode": 73, "mode": 16384,
                            "filesystem_id": 9137},
        },
    }
    checks = [
        {"name": "binding.registry_unchanged", "result": "PASS",
         "observed": {"state": "ACTIVE", "sha256": "a" * 64},
         "expected": {"state": "ACTIVE", "sha256": "a" * 64}},
        {"name": "binding.marker_unchanged", "result": "PASS",
         "observed": {"state": "RESERVED", "sha256": "b" * 64},
         "expected": {"state": "RESERVED", "sha256": "b" * 64}},
        {"name": "post_scan.namespace_fence", "result": "PASS",
         "observed": {"digest": "c" * 64, "item_count": 2},
         "expected": {"digest": "c" * 64, "item_count": 2}},
    ]
    capture_events = {
        "lsof": [{"args": (["/usr/sbin/lsof", "+D", os.fspath(root)],),
                  "kwargs": {}, "argv_argument": scan["argv"],
                  "returncode": 1, "pass_through_identity": True}],
        "process_snapshot": [{"args": (), "kwargs": {},
                              "result": {"pid": 9021, "ppid": 1,
                                         "argv": ["lsof", "+D"]}}],
        "executable_identity": [{"args": ("/usr/sbin/lsof",), "kwargs": {},
                                 "result": {"path": "/usr/sbin/lsof",
                                            "sha256": "d" * 64,
                                            "device": 41, "inode": 91}}],
        "parse": [{"input_length": len(binary),
                   "input_sha256": hashlib.sha256(binary).hexdigest(),
                   "result": (parsed_rows, {"complete": True,
                                              "bytes_consumed": len(binary)})}],
        "namespace": [{"digest": "c" * 64, "item_count": 2,
                       "items": [{"path": b"/tmp/fgmetal-r54-offline/build-r6a",
                                  "device": 41, "inode": 73,
                                  "lsof_type": b"DIR", "state": namespace_state}],
                       "summary": [{"path_b64": base64.b64encode(
                           b"/tmp/fgmetal-r54-offline/build-r6a").decode("ascii"),
                           "device": 41, "inode": 73, "lsof_type": "DIR"}]}],
        "named_root_checks": [named_check, dict(named_check)],
        "scanner_loads": [{"module_id": 701, "module_name": "r51_scanner",
                           "module_file": "/repo/r6a_r33_live_open_capture.py",
                           "source_hash_matches_pinned": True,
                           "capture_limits": {"timeout_seconds": 15,
                                              "stdout_bytes": 1048576,
                                              "stderr_bytes": 1048576}}],
        "classification": [{"args": (1, 2), "kwargs": {"status": 1},
                            "result": "STATUS_ONE_EXPECTED_SEARCH_MISS"}],
        "production_helper_invocations": [{"target": root, "parent_fd": 31,
                                            "root_fd": 32, "outcome": "returned",
                                            "result": {"classification":
                                                "STATUS_ONE_EXPECTED_SEARCH_MISS",
                                                "namespace_item_count": 2,
                                                "row_count": 1}}],
        "scanner_exceptions": [],
        "evidence_capture_errors": [],
        "root_pin_row": {"row": parsed_rows[0], "stream": {"complete": True}},
    }
    production_gate_capture = {
        "lsof": {key: value for key, value in scan.items()
                 if key not in {"stdout_raw", "stderr_raw"}},
        "stdout": {"path": "lsof.stdout.bin", "length": len(binary),
                   "sha256": hashlib.sha256(binary).hexdigest()},
        "stderr": {"path": "lsof.stderr.bin", "length": 0,
                   "sha256": hashlib.sha256(b"").hexdigest()},
        "scanner_loads": capture_events["scanner_loads"],
        "scanner_lsof_calls": capture_events["lsof"],
        "scanner_call_counts": {"run_lsof_bounded": 1, "process_snapshot": 2,
                                "executable_identity": 2, "parse_lsof": 1},
        "scanner_capture_limits": capture_events["scanner_loads"][0]["capture_limits"],
        "process_snapshots": capture_events["process_snapshot"],
        "executable_identities": capture_events["executable_identity"],
        "parsed_rows_and_stream": capture_events["parse"],
        "namespace_before_and_after": capture_events["namespace"],
        "named_root_rechecks": capture_events["named_root_checks"],
        "root_pin_row": capture_events["root_pin_row"],
        "status_classification_call": capture_events["classification"],
        "scanner_exceptions": capture_events["scanner_exceptions"],
        "observer_callback_errors": [],
    }
    report = {
        "run_id": "R54-OFFLINE-SYNTHETIC",
        "status": "PASS",
        "result": "R23_ORPHAN_PREFLIGHT = PASS",
        "run_started_at_utc": "2026-10-22T00:00:00+00:00",
        "run_finished_at_utc": "2026-10-22T00:00:01+00:00",
        "process_identity": {"pid": 9000, "ppid": 1, "cwd": "/repo",
                             "python": "/usr/bin/python3"},
        "preflight_script_sha256": "e" * 64,
        "production_source_sha256": {"build_retention.py": "f" * 64,
                                      "accepted_scanner.py": "0" * 64},
        "binding": {"build_id": "r6a-r23", "owner_experiment": "R6-A-R23",
                    "expected_path": os.fspath(root), "registry_sha256": "a" * 64,
                    "marker_sha256": "b" * 64, "reservation_bytes": 5598724098},
        "locks": {"allocation": {"device": 41, "inode": 101},
                  "retention": {"device": 41, "inode": 102}},
        "checks": checks,
        "production_liveness_gate": {
            "classification": "STATUS_ONE_EXPECTED_SEARCH_MISS",
            "namespace_digest": "c" * 64, "namespace_item_count": 2,
            "row_count": 1,
        },
        "production_gate_capture": production_gate_capture,
        "production_helper_invocations": capture_events["production_helper_invocations"],
        "mutations": [],
        "diagnostic_scan_invoked": False,
        "production_gate_invocations": 1,
        "closeout_invoked": False,
    }
    return report


def main() -> None:
    semantic = build_success_report()
    assert_success_report_shape(semantic)
    encoded_tree = serialize_report(semantic)
    encoded = json.dumps(encoded_tree, indent=2, allow_nan=False)
    parsed = json.loads(encoded)
    assert_json_domain(parsed)

    path_wrapper = parsed["production_gate_capture"]["named_root_rechecks"][0]["target"]
    _assert(path_wrapper == {"$type": "pathlib.PosixPath",
                            "value": "/tmp/fgmetal-r54-offline/build-r6a"},
            "named-root Path value and type tag round-trip")
    helper_path = parsed["production_helper_invocations"][0]["target"]
    _assert(helper_path == path_wrapper, "helper target Path round-trip")

    namespace_bytes = parsed["production_gate_capture"]["namespace_before_and_after"][0][
        "items"][0]["path"]
    original_bytes = b"/tmp/fgmetal-r54-offline/build-r6a"
    _assert(base64.b64decode(namespace_bytes["base64"]) == original_bytes,
            "namespace path bytes retain exact payload")
    _assert(namespace_bytes["length"] == len(original_bytes),
            "namespace path bytes retain length")
    _assert(namespace_bytes["sha256"] == hashlib.sha256(original_bytes).hexdigest(),
            "namespace path bytes retain digest")

    tuple_wrapper = parsed["production_gate_capture"]["parsed_rows_and_stream"][0][
        "result"]
    _assert(tuple_wrapper["$type"] == "builtins.tuple" and len(tuple_wrapper["items"]) == 2,
            "parser tuple type and member order survive")
    _assert(parsed["production_gate_capture"]["namespace_before_and_after"][0][
        "items"][0]["state"]["$type"] == "builtins.tuple",
        "namespace state tuple type survives")
    _assert([row["name"] for row in parsed["checks"]] == [
        "binding.registry_unchanged", "binding.marker_unchanged",
        "post_scan.namespace_fence"], "check list order survives")
    _assert(parsed["production_liveness_gate"]["classification"] ==
            "STATUS_ONE_EXPECTED_SEARCH_MISS", "production decision survives")
    _assert(parsed["binding"]["registry_sha256"] == "a" * 64 and
            parsed["binding"]["marker_sha256"] == "b" * 64,
            "registry and marker evidence survives")
    _assert(parsed["production_gate_capture"]["named_root_rechecks"][0][
        "expected_root_identity"]["build_inode"] == 73,
        "named/pinned fence evidence survives")

    failure_shape = {
        "run_id": "R54-OFFLINE-FAILURE-SHAPE",
        "status": "FAIL",
        "first_failure": "TypeError: prior publication failure",
        "captured_gate_events": {"named_root_checks": semantic["production_gate_capture"][
            "named_root_rechecks"]},
        "production_gate_capture": semantic["production_gate_capture"]["lsof"],
        "production_helper_invocations": semantic["production_helper_invocations"],
        "mutations": [],
        "closeout_invoked": False,
    }
    failure_encoded = json.dumps(serialize_report(failure_shape), allow_nan=False)
    failure_parsed = json.loads(failure_encoded)
    _assert(failure_parsed["status"] == "FAIL" and
            failure_parsed["captured_gate_events"]["named_root_checks"][0]["target"] ==
            path_wrapper,
            "failure report uses the same Path-safe publication boundary")

    unsupported_rejected = False
    try:
        serialize_report({"unsupported": Unsupported()})
    except TypeError as error:
        unsupported_rejected = "__main__.Unsupported" in str(error)
    _assert(unsupported_rejected, "unsupported custom object is rejected by type")

    stat_value = os.stat_result((16384, 73, 41, 1, 501, 20, 0, 1, 2, 3))
    stat_wrapper = serialize_report({"stat": stat_value})["stat"]
    _assert(stat_wrapper["$type"] == "os.stat_result" and
            stat_wrapper["fields"]["st_ino"] == 73,
            "stat result receives an explicit typed representation")
    enum_wrapper = serialize_report({"enum": ProbeEnum.ACCEPTED})["enum"]
    _assert(enum_wrapper["$type"] == "__main__.ProbeEnum" and
            enum_wrapper["name"] == "ACCEPTED" and enum_wrapper["value"] == "accepted",
            "enum receives an explicit typed representation")

    print(json.dumps({
        "result": "PASS",
        "success_report_schema": "exact R23 success field order",
        "success_report_round_trip": "PASS",
        "failure_report_boundary_parity": "PASS",
        "path_type_and_value": "PASS",
        "bytes_base64_length_sha256": "PASS",
        "tuple_order_and_type": "PASS",
        "scanner_namespace_fences_and_liveness": "PASS",
        "stat_result_and_enum_encodings": "PASS",
        "unsupported_custom_type_rejected": "PASS",
        "r23_accessed": False,
        "r23_mutations": 0,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
