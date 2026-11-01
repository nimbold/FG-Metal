"""Strict, evidence-preserving JSON boundary for the R23 preflight reports."""
from __future__ import annotations

import base64
import enum
import hashlib
import json
import math
import os
import stat
from pathlib import PurePath
from typing import Any


SUCCESS_REPORT_FIELD_ORDER = (
    "run_id", "status", "result", "run_started_at_utc",
    "run_finished_at_utc", "process_identity", "preflight_script_sha256",
    "production_source_sha256", "binding", "locks", "checks",
    "production_liveness_gate", "production_gate_capture",
    "production_helper_invocations", "mutations", "diagnostic_scan_invoked",
    "production_gate_invocations", "closeout_invoked",
)

SUCCESS_GATE_CAPTURE_FIELD_ORDER = (
    "lsof", "stdout", "stderr", "scanner_loads", "scanner_lsof_calls",
    "scanner_call_counts", "scanner_capture_limits", "process_snapshots",
    "executable_identities", "parsed_rows_and_stream",
    "namespace_before_and_after", "named_root_rechecks", "root_pin_row",
    "status_classification_call", "scanner_exceptions",
    "observer_callback_errors",
)


def _type_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _safe(value: Any, location: str) -> Any:
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"non-finite float at {location}")
        return value
    if isinstance(value, PurePath):
        path_value = os.fspath(value)
        if type(path_value) is not str:
            raise TypeError(f"path did not produce a string at {location}")
        return {"$type": _type_name(value), "value": path_value}
    if type(value) in (bytes, bytearray):
        raw = bytes(value)
        return {
            "base64": base64.b64encode(raw).decode("ascii"),
            "length": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    if isinstance(value, enum.Enum):
        return {
            "$type": _type_name(value),
            "name": value.name,
            "value": _safe(value.value, f"{location}.value"),
        }
    if isinstance(value, BaseException):
        return {
            "$type": _type_name(value),
            "args": _safe(value.args, f"{location}.args"),
        }
    if isinstance(value, os.stat_result):
        field_names = (
            "st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid",
            "st_size", "st_atime", "st_mtime", "st_ctime", "st_atime_ns",
            "st_mtime_ns", "st_ctime_ns", "st_birthtime", "st_birthtime_ns",
            "st_flags", "st_gen", "st_rdev", "st_blksize", "st_blocks",
        )
        fields = {
            name: _safe(getattr(value, name), f"{location}.{name}")
            for name in field_names if hasattr(value, name)
        }
        return {
            "$type": "os.stat_result",
            "items": [_safe(item, f"{location}.items[{index}]")
                      for index, item in enumerate(value)],
            "fields": fields,
        }
    if type(value) is dict:
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(
                    f"non-string report key at {location}: {_type_name(key)}")
            converted[key] = _safe(item, f"{location}.{key}")
        return converted
    if type(value) is list:
        return [_safe(item, f"{location}[{index}]")
                for index, item in enumerate(value)]
    if type(value) is tuple:
        return {
            "$type": "builtins.tuple",
            "items": [_safe(item, f"{location}[{index}]")
                      for index, item in enumerate(value)],
        }
    if type(value) in (set, frozenset):
        items = [_safe(item, f"{location}{{item}}") for item in value]
        items.sort(key=lambda item: json.dumps(
            item, ensure_ascii=True, allow_nan=False, sort_keys=True,
            separators=(",", ":")))
        return {"$type": _type_name(value), "items": items}
    raise TypeError(f"unsupported report value at {location}: {_type_name(value)}")


def assert_json_domain(value: Any, location: str = "$") -> None:
    """Validate a converted tree without changing it."""
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float:
        if math.isfinite(value):
            return
        raise ValueError(f"non-finite JSON number at {location}")
    if type(value) is list:
        for index, item in enumerate(value):
            assert_json_domain(item, f"{location}[{index}]")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError(f"non-string JSON key at {location}: {_type_name(key)}")
            assert_json_domain(item, f"{location}.{key}")
        return
    raise TypeError(f"non-JSON value at {location}: {_type_name(value)}")


def serialize_report(report: Any) -> Any:
    """Convert one complete report tree and assert JSON-domain closure."""
    converted = _safe(report, "$")
    assert_json_domain(converted)
    return converted


def assert_success_report_shape(report: Any) -> None:
    if type(report) is not dict:
        raise TypeError("success report must be a dict")
    if tuple(report) != SUCCESS_REPORT_FIELD_ORDER:
        raise ValueError("success report fields/order differ from the audited schema")
    capture = report.get("production_gate_capture")
    if type(capture) is not dict or tuple(capture) != SUCCESS_GATE_CAPTURE_FIELD_ORDER:
        raise ValueError("production gate capture fields/order differ from the audited schema")
