#!/usr/bin/env python3
"""Run one reviewed, lifecycle-bound 20/40/20 BR8 Finder replay."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import pathlib
import re
import shutil
import signal
import stat
import statistics
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Any

REPO = pathlib.Path(__file__).resolve().parents[5]
EXPERIMENT = REPO / "experiments/dxvk_macos_step11d3b"
LOCAL_DIR = EXPERIMENT / "evidence/br9r2/z9-finder-replay-20261014"
sys.path.insert(0, str(LOCAL_DIR))
from z9_replay_safety import (  # noqa: E402
    covered_sampling_summary, desktop_preflight_eligible, identity_stability_summary,
    renderer_healthy, replay_execution_path_eligible,
    same_query_cover_state, transition_bracket_summary,
)
RUNNER = EXPERIMENT / "runner_b.py"
COLLECTOR_SOURCE = LOCAL_DIR / "public_cgwindow_inventory_z9.swift"
COLLECTOR = LOCAL_DIR / "public_cgwindow_inventory_z9"
DETECTOR = LOCAL_DIR / "z9_fast_zero_detector.py"
ANALYSIS_SOURCE = LOCAL_DIR / "z9_event_aligned_analysis.py"
OBSERVER_MODULE = EXPERIMENT / "evidence/br9r2/lifecycle_window_observer.py"
PUBLIC_OBSERVER_SOURCE = EXPERIMENT / "evidence/br9r2/public_window_observer.swift"
COVERER = pathlib.Path(
    "/Users/nima/Library/Caches/FGMetal/artifacts/sha256/"
    "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc/neutral-coverer"
)
APP = EXPERIMENT / "control/bare_vulkan_app-start-gated.exe"
AX_WINDOW_HELPER = EXPERIMENT / "evidence/br8/ax_window.py"
WINE_HOST = pathlib.Path(
    "/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine/"
    "lib/wine/x86_64-unix/wine"
)
WINDOW_TITLE = "FG-Metal Step 11D.3-B Bare Vulkan"
EXPECTED_TARGET_CG = {"x": 68.0, "y": 62.0, "width": 640.0, "height": 392.0}
FULL_COVER_SECONDS = 40
PRE_SECONDS = 20
RESTORED_SECONDS = 20
NS_WINDOW_OCCLUSION_STATE_VISIBLE = 1 << 1
MEASURED_PRE_DURATION_TOLERANCE_NS = 250_000_000
MAX_FINDER_ACTION_INVENTORY_AGE_NS = 500_000_000
RUNNER_WATCHDOG_SECONDS = 1500
OBSERVER_SAMPLE_MAX_AGE_NS = 1_500_000_000
GIB = 1024 ** 3


class MachTimebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
_libsystem.mach_absolute_time.restype = ctypes.c_uint64
_libsystem.mach_timebase_info.argtypes = [ctypes.POINTER(MachTimebase)]
_libsystem.mach_timebase_info.restype = ctypes.c_int
_timebase = MachTimebase()
if _libsystem.mach_timebase_info(ctypes.byref(_timebase)) != 0 or not _timebase.denom:
    raise RuntimeError("mach_timebase_info failed")


def mach_ns() -> int:
    ticks = int(_libsystem.mach_absolute_time())
    numerator, denominator = int(_timebase.numer), int(_timebase.denom)
    return (ticks // denominator) * numerator + ((ticks % denominator) * numerator) // denominator


def clock_pair() -> dict[str, int]:
    before = time.monotonic_ns()
    mach = mach_ns()
    after = time.monotonic_ns()
    return {"python_monotonic_before_ns": before, "python_monotonic_after_ns": after,
            "mach_absolute_time_ns": mach, "python_read_bracket_ns": after - before}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: pathlib.Path, value: dict[str, Any]) -> None:
    if path.is_symlink():
        raise RuntimeError(f"refusing to replace symlink: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


class JSONLTail:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.offset = 0
        self.partial = b""

    def read_new(self) -> list[dict[str, Any]]:
        if self.path.is_symlink() or not self.path.is_file():
            return []
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read()
            self.offset = stream.tell()
        if not data:
            return []
        pieces = (self.partial + data).split(b"\n")
        self.partial = pieces.pop()
        output: list[dict[str, Any]] = []
        for raw in pieces:
            if not raw:
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                self.partial = raw + b"\n" + self.partial
                continue
            if isinstance(value, dict):
                output.append(value)
        return output


def read_json(path: pathlib.Path) -> dict[str, Any] | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, json.JSONDecodeError):
        return None


def read_all_jsonl(path: pathlib.Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    if path.is_symlink() or not path.is_file():
        return result
    with path.open("r", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                result.append(row)
    return result


def evidence_root() -> pathlib.Path:
    raw = os.environ.get("FGMETAL_BR9R_EVIDENCE_ROOT")
    if raw is None:
        raise RuntimeError("FGMETAL_BR9R_EVIDENCE_ROOT is required")
    root = pathlib.Path(raw)
    if (not root.is_absolute() or root.parent != pathlib.Path("/private/tmp")
            or not re.fullmatch(r"codex-br9r2-[a-z0-9-]{8,96}", root.name)
            or root.resolve(strict=True) != root):
        raise RuntimeError("Z9 evidence root must be a canonical /private/tmp/codex-br9r2-* path")
    info = root.lstat()
    if (not stat.S_ISDIR(info.st_mode) or root.is_symlink() or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700):
        raise RuntimeError("Z9 evidence root must be owned and mode 0700")
    return root


def allocated_owned_tree_bytes(paths: list[pathlib.Path]) -> int:
    """Measure only the explicitly owned transient evidence trees, without hashing payloads."""
    seen: set[tuple[int, int]] = set()
    allocated = 0
    for root in paths:
        root_info = root.lstat()
        if (not stat.S_ISDIR(root_info.st_mode) or root.is_symlink()
                or root_info.st_uid != os.getuid() or stat.S_IMODE(root_info.st_mode) != 0o700):
            raise RuntimeError(f"transient evidence directory is not owned mode 0700: {root}")
        pending = [root]
        while pending:
            current = pending.pop()
            current_info = current.lstat()
            if (not stat.S_ISDIR(current_info.st_mode)
                    or current_info.st_dev != root_info.st_dev):
                raise RuntimeError("transient evidence tree changed type or filesystem")
            with os.scandir(current) as entries:
                for entry in entries:
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISLNK(info.st_mode):
                        raise RuntimeError(f"transient evidence contains a symlink: {entry.path}")
                    if stat.S_ISDIR(info.st_mode):
                        if info.st_dev != root_info.st_dev:
                            raise RuntimeError("transient evidence crosses a filesystem boundary")
                        pending.append(pathlib.Path(entry.path))
                    elif stat.S_ISREG(info.st_mode):
                        identity = (info.st_dev, info.st_ino)
                        if identity not in seen:
                            seen.add(identity)
                            allocated += getattr(info, "st_blocks", 0) * 512
                    else:
                        raise RuntimeError(f"transient evidence contains a special file: {entry.path}")
        after = root.lstat()
        if (after.st_dev, after.st_ino, after.st_mode) != (
                root_info.st_dev, root_info.st_ino, root_info.st_mode):
            raise RuntimeError(f"transient evidence directory identity changed: {root}")
    return allocated


def finder_pid() -> int:
    completed = subprocess.run(["/usr/bin/pgrep", "-x", "Finder"], check=True,
                               capture_output=True, text=True, timeout=5)
    values = [int(line) for line in completed.stdout.splitlines() if line.strip()]
    if len(values) != 1:
        raise RuntimeError(f"expected one exact Finder PID, found {values}")
    return values[0]


def applescript_literal(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def run_osascript(events_file: Any, action: str, script: str,
                  pre_query_end_mach_ns: int,
                  pre_action_inventory: dict[str, Any],
                  timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    begin = clock_pair()
    inventory_age_ns = begin["mach_absolute_time_ns"] - pre_query_end_mach_ns
    if not 0 <= inventory_age_ns <= MAX_FINDER_ACTION_INVENTORY_AGE_NS:
        end = clock_pair()
        events_file.write(json.dumps({
            "event": "finder_action_guard_rejected", "action": action,
            "reason": "pre-action WindowServer inventory exceeded its freshness bound",
            "begin": begin, "end": end,
            "pre_query_end_mach_ns": pre_query_end_mach_ns,
            "inventory_age_at_action_boundary_ns": inventory_age_ns,
            "freshness_limit_ns": MAX_FINDER_ACTION_INVENTORY_AGE_NS,
            "pre_action_inventory": pre_action_inventory,
            "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
        }, sort_keys=True) + "\n")
        events_file.flush()
        raise RuntimeError(f"{action}: fresh inventory expired before AppleScript launch")
    action_boundary_inventory = dict(pre_action_inventory)
    action_boundary_inventory.update({
        "action_begin_mach_ns": begin["mach_absolute_time_ns"],
        "age_at_action_begin_ns": inventory_age_ns,
    })
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", script], capture_output=True,
                                text=True, timeout=timeout, check=False)
    except BaseException as error:
        end = clock_pair()
        events_file.write(json.dumps({"event": "finder_action", "action": action,
                                      "begin": begin, "end": end,
                                      "exception": f"{type(error).__name__}: {error}",
                                      "pre_query_end_mach_ns": pre_query_end_mach_ns,
                                      "inventory_age_at_action_boundary_ns": inventory_age_ns,
                "pre_action_inventory": pre_action_inventory,
                                      "script_sha256": hashlib.sha256(script.encode()).hexdigest()},
                                     sort_keys=True) + "\n")
        events_file.flush()
        raise
    end = clock_pair()
    events_file.write(json.dumps({"event": "finder_action", "action": action,
                                  "begin": begin, "end": end,
                                  "returncode": result.returncode,
                                  "pre_query_end_mach_ns": pre_query_end_mach_ns,
                                  "inventory_age_at_action_boundary_ns": inventory_age_ns,
            "pre_action_inventory": action_boundary_inventory,
                                  "stdout": result.stdout.strip(),
                                  "stderr": result.stderr.strip(),
                                  "script_sha256": hashlib.sha256(script.encode()).hexdigest(),
                                  "script": script}, sort_keys=True) + "\n")
    events_file.flush()
    if result.returncode != 0:
        raise RuntimeError(f"Finder action {action} failed: {result.stderr.strip()}")
    return result


def parse_ints(value: str) -> list[int]:
    return [int(item) for item in re.findall(r"-?\d+", value)]


def rect_from_row(row: dict[str, Any] | None) -> dict[str, float] | None:
    value = row.get("bounds_cg") if isinstance(row, dict) else None
    if not isinstance(value, dict):
        return None
    try:
        return {key: float(value[key]) for key in ("x", "y", "width", "height")}
    except (KeyError, TypeError, ValueError):
        return None


def rect_contains(outer: dict[str, float] | None,
                  inner: dict[str, float] | None, tolerance: float = 0.0) -> bool:
    if outer is None or inner is None:
        return False
    return (outer["x"] <= inner["x"] + tolerance
            and outer["y"] <= inner["y"] + tolerance
            and outer["x"] + outer["width"] >= inner["x"] + inner["width"] - tolerance
            and outer["y"] + outer["height"] >= inner["y"] + inner["height"] - tolerance)


def rects_overlap(left: dict[str, float], right: dict[str, float]) -> bool:
    return (left["x"] < right["x"] + right["width"]
            and right["x"] < left["x"] + left["width"]
            and left["y"] < right["y"] + right["height"]
            and right["y"] < left["y"] + left["height"])


def rect_equal(left: dict[str, float] | None, right: dict[str, float] | None,
               tolerance: float = 1.0) -> bool:
    return bool(left is not None and right is not None and all(
        abs(left[key] - right[key]) <= tolerance
        for key in ("x", "y", "width", "height")))


def cg_frame_from_telemetry(telemetry: dict[str, Any],
                            display_inventory: list[dict[str, Any]]) -> dict[str, float] | None:
    frame = telemetry.get("window_frame")
    if not isinstance(frame, dict):
        frame = {
            "x": telemetry.get("window_frame_x"),
            "y": telemetry.get("window_frame_y"),
            "width": telemetry.get("window_frame_width"),
            "height": telemetry.get("window_frame_height"),
        }
    if not isinstance(frame, dict):
        return None
    try:
        x, y = float(frame["x"]), float(frame["y"])
        width, height = float(frame["width"]), float(frame["height"])
        screen_id = int(telemetry["screen_id"])
    except (KeyError, TypeError, ValueError):
        return None
    display = next((row for row in display_inventory if row.get("display_id") == screen_id), None)
    if not isinstance(display, dict):
        return None
    app = display.get("frame_appkit")
    cg = display.get("bounds_cg")
    if not isinstance(app, dict) or not isinstance(cg, dict):
        return None
    try:
        cg_x = float(cg["x"]) + x - float(app["x"])
        cg_y = float(cg["y"]) + float(cg["height"]) - (y - float(app["y"]) + height)
    except (KeyError, TypeError, ValueError):
        return None
    return {"x": cg_x, "y": cg_y, "width": width, "height": height}


def target_correspondence(sample: dict[str, Any] | None,
                          telemetry: dict[str, Any] | None,
                          target_pid: int,
                          display_inventory: list[dict[str, Any]]) -> dict[str, Any]:
    if not isinstance(sample, dict) or sample.get("event") != "sample":
        return {"status": "NO_EXTERNAL_SAMPLE", "candidate_count": 0}
    if (sample.get("all_window_query_succeeded") is not True
            or sample.get("onscreen_window_query_succeeded") is not True
            or not isinstance(telemetry, dict)):
        return {"status": "INCOMPLETE_QUERY_OR_TELEMETRY", "candidate_count": 0}
    expected = cg_frame_from_telemetry(telemetry, display_inventory)
    if expected is None:
        return {"status": "TARGET_FRAME_OR_SCREEN_UNAVAILABLE", "candidate_count": 0}
    all_rows = sample.get("windows")
    onscreen_rows = sample.get("onscreen_windows")
    if not isinstance(all_rows, list) or not isinstance(onscreen_rows, list):
        return {"status": "INVENTORY_ROWS_UNAVAILABLE", "candidate_count": 0}
    level = telemetry.get("window_level")
    alpha = telemetry.get("window_alpha")
    candidates = [row for row in all_rows
                  if isinstance(row, dict)
                  and row.get("owner_pid") == target_pid
                  and row.get("title") == WINDOW_TITLE
                  and row.get("layer") == level
                  and isinstance(alpha, (int, float))
                  and isinstance(row.get("alpha"), (int, float))
                  and abs(float(row["alpha"]) - float(alpha)) <= 0.001
                  and rect_equal(rect_from_row(row), expected, tolerance=0.5)]
    if len(candidates) != 1:
        return {"status": "UNIQUE_PUBLIC_TARGET_MATCH_FAILED", "candidate_count": len(candidates),
                "converted_target_frame_cg": expected}
    target = candidates[0]
    target_id = target.get("cg_window_id")
    onscreen_matches = [row for row in onscreen_rows
                        if isinstance(row, dict) and row.get("owner_pid") == target_pid
                        and row.get("cg_window_id") == target_id
                        and row.get("onscreen") is True
                        and row.get("title") == WINDOW_TITLE
                        and rect_equal(rect_from_row(row), expected, tolerance=0.5)]
    if len(onscreen_matches) != 1 or target.get("on_screen_joined") is not True:
        return {"status": "UNIQUE_ONSCREEN_JOIN_FAILED", "candidate_count": len(onscreen_matches),
                "converted_target_frame_cg": expected, "candidate": target}
    screen_match = onscreen_matches[0]
    number = telemetry.get("window_number")
    number_supports_id = isinstance(number, int) and number > 0 and number == target_id
    return {
        "status": "PASS",
        "candidate_count": 1,
        "on_screen_candidate_count": 1,
        "cg_window_id": target_id,
        "cg_window_number_supports_window_number": number_supports_id,
        "window_number_is_supporting_runtime_evidence_only": True,
        "window_number_runtime_support_status": (
            "MATCH" if number_supports_id else "MISMATCH_OR_UNAVAILABLE"),
        "public_match_basis": ["owner_pid", "exact_converted_frame", "title",
                               "layer", "alpha", "unique_on_screen_row"],
        "converted_target_frame_cg": expected,
        "target_row": screen_match,
    }


def finder_covers_target(sample: dict[str, Any] | None, target_row: dict[str, Any] | None,
                         finder_pid_value: int, *, exact_cg_id: int | None = None,
                         exact_title: str | None = None,
                         exact_layer: int | None = None) -> list[dict[str, Any]]:
    if not isinstance(sample, dict) or not isinstance(target_row, dict):
        return []
    finder_rows = sample.get("finder_onscreen_windows")
    if not isinstance(finder_rows, list):
        return []
    target_rect = rect_from_row(target_row)
    target_rank = target_row.get("rank_onscreen_windows")
    candidates = []
    for row in finder_rows:
        if (not isinstance(row, dict) or row.get("owner_pid") != finder_pid_value
                or row.get("on_screen_joined") is not True):
            continue
        if exact_cg_id is not None and row.get("cg_window_id") != exact_cg_id:
            continue
        if exact_title is not None and row.get("title") != exact_title:
            continue
        if exact_layer is not None and row.get("layer") != exact_layer:
            continue
        rank = row.get("rank_onscreen_windows")
        alpha = row.get("alpha")
        if (row.get("onscreen") is not True
                or type(rank) is not int or type(target_rank) is not int or rank >= target_rank
                or not isinstance(alpha, (int, float)) or float(alpha) < 0.999
                or not rect_contains(rect_from_row(row), target_rect)):
            continue
        candidates.append(row)
    return candidates


def telemetry_tuple(row: dict[str, Any]) -> tuple[Any, ...]:
    return tuple(row.get(key) for key in (
        "layer_id", "window_view_id", "window_lookup_path", "window_id", "window_number"))


def telemetry_state(row: dict[str, Any]) -> dict[str, Any]:
    state = {
        "app_active": row.get("app_active"),
        "window_key": row.get("window_key"),
        "window_main": row.get("window_main"),
        "window_visible": row.get("window_visible"),
        "window_miniaturized": row.get("window_miniaturized"),
        "window_occlusion_state": row.get("window_occlusion_state"),
        "visible_bit_set": (type(row.get("window_occlusion_state")) is int
                            and bool(row["window_occlusion_state"]
                                     & NS_WINDOW_OCCLUSION_STATE_VISIBLE)),
        "window_alpha": row.get("window_alpha"),
        "window_level": row.get("window_level"),
        "window_frame_appkit": {"x": row.get("window_frame_x"),
                                 "y": row.get("window_frame_y"),
                                 "width": row.get("window_frame_width"),
                                 "height": row.get("window_frame_height")},
        "screen_id": row.get("screen_id"),
        "screen_refresh_hz": row.get("screen_refresh_hz"),
    }
    return state


def root_hashes(paths: list[pathlib.Path]) -> dict[str, str]:
    return {str(path.resolve(strict=True)): sha256(path) for path in paths}


def require_valid_replay1_analysis(analysis_path: pathlib.Path,
                                  controller_path: pathlib.Path,
                                  controller: dict[str, Any]) -> dict[str, Any]:
    """Require the analyzer's R1-R4 result to bind this exact replay-1 run."""
    if (not analysis_path.is_absolute()
            or analysis_path.parent != controller_path.parent
            or analysis_path.is_symlink() or not analysis_path.is_file()
            or analysis_path.resolve(strict=True) != analysis_path):
        raise RuntimeError(
            "replay 2 requires one canonical replay-1 analysis JSON in the exact replay-1 evidence directory")
    if analysis_path.stat().st_mtime_ns < controller_path.stat().st_mtime_ns:
        raise RuntimeError("replay 2 is blocked because replay-1 analysis predates the final controller record")
    analysis = read_json(analysis_path)
    run_id = controller.get("run_id")
    target_pid = controller.get("target_pid")
    if (not isinstance(run_id, str) or type(target_pid) is not int or target_pid <= 1):
        raise RuntimeError("replay 1 lacks an exact run ID or target PID for analysis binding")
    expected_trace_path = (
        EXPERIMENT / "runs" / run_id / "host-traces" / f"host-trace.{target_pid}.jsonl")
    raw_trace_path = controller.get("target_trace_path")
    trace_path = pathlib.Path(raw_trace_path) if isinstance(raw_trace_path, str) else None
    if (trace_path != expected_trace_path or trace_path.is_symlink()
            or not trace_path.is_file()):
        raise RuntimeError("replay 1 target trace path is missing, stale, or mismatched")
    finder_identity = controller.get("finder_identity")
    expected_finder_id = (finder_identity.get("cg_window_id")
                          if isinstance(finder_identity, dict) else None)
    expected_target_id = controller.get("accepted_public_cg_window_id")
    expected_poller_path = controller.get("poller_path")
    if not isinstance(analysis, dict):
        raise RuntimeError("replay 2 is blocked because replay-1 analysis is missing or malformed")
    if (analysis.get("schema_version") != 1
            or analysis.get("analysis_source") != str(ANALYSIS_SOURCE.resolve(strict=True))
            or analysis.get("analysis_source_sha256") != sha256(ANALYSIS_SOURCE)
            or analysis.get("controller_path") != str(controller_path.resolve(strict=True))
            or analysis.get("trace_path") != str(trace_path.resolve(strict=True))
            or analysis.get("trace_sha256") != sha256(trace_path)
            or analysis.get("poller_path") != expected_poller_path
            or analysis.get("target_pid") != target_pid
            or analysis.get("target_cg_window_id") != expected_target_id
            or type(expected_finder_id) is not int
            or analysis.get("finder_cg_window_id") != expected_finder_id
            or analysis.get("sequence_integrity_ok") is not True
            or analysis.get("accepted_identity_continuity_ok") is not True
            or analysis.get("finder_full_cover_hold_complete") is not True
            or analysis.get("controller_technical_valid") is not True
            or analysis.get("replay_classification") not in {"R1", "R2", "R3", "R4"}):
        raise RuntimeError(
            "replay 2 is blocked because replay-1 analysis is stale, mismatched, invalid, R5, or unsupported")
    return {
        "path": str(analysis_path),
        "sha256": sha256(analysis_path),
        "replay_classification": analysis["replay_classification"],
        "trace_path": str(trace_path),
        "trace_sha256": analysis["trace_sha256"],
        "target_pid": target_pid,
    }


def require_valid_replay1(root: pathlib.Path, smoke_id: str,
                          binding: dict[str, Any],
                          candidate_hashes: dict[str, str],
                          runner_b: Any,
                          analysis_path: pathlib.Path) -> dict[str, Any]:
    matches = []
    for controller_path in sorted(root.glob("z9-replay-1-*/controller.json")):
        if controller_path.is_symlink() or not controller_path.is_file():
            continue
        record = read_json(controller_path)
        if isinstance(record, dict) and record.get("smoke_authorization_id") == smoke_id:
            matches.append((controller_path, record))
    if len(matches) != 1:
        raise RuntimeError(
            f"replay 2 requires exactly one replay 1 under this current authorization; found {len(matches)}")
    controller_path, record = matches[0]
    final_binding = record.get("runner_final_gate0_post")
    final_path = pathlib.Path(final_binding.get("path", "")) if isinstance(final_binding, dict) else None
    if (not isinstance(final_path, pathlib.Path) or not final_path.is_absolute()
            or final_path.parent != controller_path.parent
            or final_path.name != "baseline-final-gate0-post.json"
            or final_path.is_symlink() or not final_path.is_file()
            or sha256(final_path) != final_binding.get("sha256")):
        raise RuntimeError("replay 1 lacks an intact final Gate 0 snapshot after run-result persistence")
    final_gate = read_json(final_path)
    runner_summary = record.get("runner_result")
    runner_final_artifact = (runner_summary.get("gate0_post_final_artifact")
                             if isinstance(runner_summary, dict) else None)
    run_artifact = record.get("runner_run_result_artifact", {})
    run_path = pathlib.Path(run_artifact.get("path", "")) if isinstance(run_artifact, dict) else None
    expected_run_path = EXPERIMENT / "runs" / str(record.get("run_id")) / "run-result.json"
    run_result = (read_json(run_path) if isinstance(run_path, pathlib.Path)
                  and run_path == expected_run_path and run_path.is_file()
                  and not run_path.is_symlink() else None)
    transient_dirs = record.get("transient_evidence_directories")
    observer_samples = record.get("observer", {}).get("samples_path")
    expected_transient_dirs = [str(controller_path.parent),
                               str(pathlib.Path(observer_samples).parent)
                               if isinstance(observer_samples, str) else None]
    transient_limit = record.get("storage_admission", {}).get(
        "transient_evidence_limit_bytes")
    transient_allocated = None
    try:
        if (transient_dirs == expected_transient_dirs
                and all(isinstance(path, str) for path in transient_dirs)
                and all(pathlib.Path(path).is_absolute()
                        and pathlib.Path(path).parent == root for path in transient_dirs)):
            transient_allocated = allocated_owned_tree_bytes(
                [pathlib.Path(path) for path in transient_dirs])
    except (OSError, RuntimeError):
        transient_allocated = None
    expected_final_artifact = {
        "path": str(final_path),
        "sha256": sha256(final_path) if final_path.is_file() and not final_path.is_symlink() else None,
    }
    if (record.get("run_index") != 1
            or not replay_execution_path_eligible(record)
            or record.get("technical_validity", {}).get("status") != "PASS"
            or record.get("technical_valid") is not True
            or record.get("runner_returncode") != 0
            or record.get("smoke_authorization_status") != "PASS"
            or record.get("current_binding_snapshot") != binding
            or record.get("candidate_hashes") != candidate_hashes
            or record.get("required_hashes_match") is not True
            or record.get("target_telemetry_identity_continuity", {}).get("identity_continuous") is not True
            or record.get("covered_sampling_summary", {}).get("status") != "PASS"
            or record.get("full_cover_transition_bracket", {}).get("status") != "PASS"
            or record.get("restoration_transition_bracket", {}).get("status") != "PASS"
            or not isinstance(final_gate, dict)
            or final_gate.get("gate0_status") != "PASS"
            or final_gate.get("free_bytes", 0) < 50 * GIB
            or final_gate.get("scoped_fgmetal_bytes", 55 * GIB + 1) > 55 * GIB
            or final_gate.get("signed_output_within_reservation") is not True
            or final_gate.get("signed_output_allocated_bytes", 0)
            > final_gate.get("signed_output_limit_bytes", -1)
            or final_gate.get("signed_output_limit_bytes")
            != record.get("storage_admission", {}).get("signed_run_output_limit_bytes")
            or not isinstance(runner_summary, dict)
            or runner_summary.get("gate0_post_final_good") is not True
            or runner_final_artifact != expected_final_artifact
            or runner_summary.get("signed_output_allocated_bytes_post_final")
            != final_gate.get("signed_output_allocated_bytes")
            or runner_summary.get("signed_output_limit_bytes")
            != record.get("storage_admission", {}).get("signed_run_output_limit_bytes")
            or transient_allocated is None
            or type(transient_limit) is not int
            or transient_allocated > transient_limit
            or record.get("transient_output_budget_final", {}).get("within_limit") is not True
            or not isinstance(run_result, dict)
            or run_result.get("experiment_id") != record.get("run_id")
            or run_result.get("phase") != "baseline-01"
            or run_result.get("is_valid") is not True
            or run_result.get("prefix_cleanup_pass") is not True
            or run_artifact.get("sha256") != sha256(run_path)):
        raise RuntimeError("replay 2 is blocked because replay 1 is not technically valid under current hashes")
    analysis_binding = require_valid_replay1_analysis(
        analysis_path, controller_path, record)
    return {"controller_path": str(controller_path),
            "controller_sha256": sha256(controller_path),
            "final_gate0_post_path": str(final_path),
            "final_gate0_post_sha256": sha256(final_path),
            "run_result_path": str(run_path),
            "run_result_sha256": sha256(run_path),
            "analysis": analysis_binding}


def fresh_replay_admission(runner_b: Any, budget: Any) -> dict[str, Any]:
    snapshot = runner_b.gate0_snapshot("Z9_CONTROLLER_PRE")
    if (snapshot.get("gate0_status") != "PASS"
            or snapshot.get("free_bytes", 0) < 50 * GIB
            or snapshot.get("scoped_fgmetal_bytes", 55 * GIB + 1) > 55 * GIB):
        raise RuntimeError(f"fresh read-only Gate 0 is blocked before replay: {snapshot}")
    budget_detail = budget.reservation_breakdown()
    output_reservation = budget_detail["required_persistent_output_bytes"]
    peak = budget.PREFIX_BYTES + output_reservation
    uncertainty = runner_b.storage_policy.admission_uncertainty_margin_bytes(peak)
    projected = runner_b.storage_policy.projected_storage_admission(
        current_free_bytes=snapshot["free_bytes"],
        current_scoped_bytes=snapshot["scoped_fgmetal_bytes"],
        peak_additional_bytes=peak + uncertainty,
    )
    if (projected.get("admitted") is not True
            or projected.get("projected_free_bytes", 0) < 50 * GIB
            or projected.get("projected_scoped_bytes", 55 * GIB + 1) > 55 * GIB):
        raise RuntimeError(f"derived replay reservation fails unchanged 55/50 GiB admission: {projected}")
    return {
        "gate0_pre": snapshot,
        "output_reservation_bytes": output_reservation,
        "signed_run_output_limit_bytes": budget_detail["signed_run_output_limit_bytes"],
        "transient_evidence_limit_bytes": budget_detail["transient_evidence_limit_bytes"],
        "collector_jsonl_output_limit_bytes": budget_detail["collector_jsonl_output_limit_bytes"],
        "prefix_reservation_bytes": budget.PREFIX_BYTES,
        "uncertainty_margin_bytes": uncertainty,
        "projected_peak_additional_bytes": peak + uncertainty,
        "admission": projected,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("smoke_authorization_id")
    parser.add_argument("--run-index", type=int, choices=(1, 2), required=True)
    parser.add_argument(
        "--replay1-analysis", type=pathlib.Path,
        help="exact replay-1 event-aligned analysis JSON, required for run index 2")
    args = parser.parse_args()
    if not re.fullmatch(r"step11d3b-smoke-\d{8}-\d{6}-[0-9a-f]{8}", args.smoke_authorization_id):
        parser.error("a current exact authenticated smoke ID is required")
    if args.run_index == 2 and args.replay1_analysis is None:
        parser.error("--replay1-analysis is required for run index 2")
    if args.run_index == 1 and args.replay1_analysis is not None:
        parser.error("--replay1-analysis is only valid for run index 2")

    sys.path.insert(0, str(REPO / "experiments/storage"))
    sys.path.insert(0, str(EXPERIMENT / "evidence/br9r2"))
    sys.path.insert(0, str(EXPERIMENT))
    from lifecycle_window_observer import (  # pylint: disable=import-outside-toplevel
        LifecycleWindowObserver, current_canonical_uncovered_streak)
    import runner_b  # pylint: disable=import-outside-toplevel
    import z9_replay_budget  # pylint: disable=import-outside-toplevel

    root = evidence_root()
    run_tag = f"z9-replay-{args.run_index}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
    if args.run_index == 1:
        mode = "baseline"
        baseline_index = 1
    else:
        mode = "baseline"
        baseline_index = 2
    for path in (COLLECTOR_SOURCE, DETECTOR, RUNNER, APP, WINE_HOST, COVERER):
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"required fixed regular input is missing or unsafe: {path}")
    if sha256(COVERER) != "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc":
        raise RuntimeError("frozen neutral coverer identity changed; refusing observer start")
    authorization, authorization_path = runner_b.verify_smoke_authorization(args.smoke_authorization_id)
    binding = runner_b.smoke_authorization_bindings()
    candidate = runner_b.candidate_files()
    candidate_hashes = {key: value["sha256"] for key, value in candidate.items()}
    required_user_hashes = {
        "runner": "f30c13df04bb0408f572480483b612bae58695d652281fdae025ed51ebfe8324",
        "process_runner": "a6708ef4dd0c2d718419daaa4dda0800781d7da92ef61b8fad60a4b66d0b5f82",
        "trace_proxy_source": "08d721e9871c9715594a2ec5e6c140ce2bfd26e9beeb4febc6109247f1ea596a",
        "trace_proxy": "04bbfc075f6ab999d0538f4a5371f3abd65fb0f0e307be0dea56e47aaefd5157",
        "wine_runtime_tree": "2fea825b205073bf011ce948ec4cbe6560d9d90e26982233ad05822a17a92bfe",
        "moltenvk_original": "aef00b13bcc808adf15b85bef9ae67393d92be7ed5dfe41cad16fa809e4a4c5f",
    }
    binding_mismatch = {name: {"expected": expected, "actual": candidate_hashes.get(name)}
                        for name, expected in required_user_hashes.items()
                        if candidate_hashes.get(name) != expected}
    if binding_mismatch:
        raise RuntimeError(f"current telemetry/runtime binding differs from accepted hashes: {binding_mismatch}")
    if authorization.get("bindings") != binding:
        raise RuntimeError("current lifecycle authorization does not bind the exact replay implementation")
    replay1_binding = (require_valid_replay1(root, args.smoke_authorization_id,
                                             binding, candidate_hashes, runner_b,
                                             args.replay1_analysis)
                       if args.run_index == 2 else None)
    admission_evidence = fresh_replay_admission(runner_b, z9_replay_budget)
    current_finder_pid = finder_pid()

    run_directory = root / run_tag
    run_directory.mkdir(mode=0o700, exist_ok=False)
    observer = LifecycleWindowObserver(
        evidence_root=root, runs_root=EXPERIMENT / "runs",
        tag=run_tag, coverer_path=COVERER, expected_display_id=1)
    events_path = run_directory / "timeline.jsonl"

    collector_binary = run_directory / "public_cgwindow_inventory_z9"
    final_post_path = run_directory / "baseline-final-gate0-post.json"
    collector_compile = subprocess.run(
        ["/usr/bin/swiftc", "-O", "-framework", "AppKit", "-framework", "CoreGraphics",
         "-framework", "Foundation",
         "-o", str(collector_binary), str(COLLECTOR_SOURCE)],
        capture_output=True, text=True, timeout=120, check=False)
    (run_directory / "collector-compile.log").write_text(
        collector_compile.stdout + collector_compile.stderr, encoding="utf-8")
    if (collector_compile.returncode != 0 or not collector_binary.is_file()
            or collector_binary.is_symlink()):
        raise RuntimeError(f"Z9 collector compilation failed; see {run_directory / 'collector-compile.log'}")

    handoff_path = observer.handoff_path
    completion_path = observer.completion_path
    poll_path = run_directory / "public-cgwindow-inventory.jsonl"
    poll_log_path = run_directory / "public-cgwindow-inventory.stderr.log"
    runner_log_path = run_directory / "runner.log"
    controller_path = run_directory / "controller.json"
    if any(path.exists() or path.is_symlink() for path in
           (poll_path, poll_log_path, runner_log_path, controller_path, events_path,
            final_post_path)):
        raise RuntimeError("refusing to replace a Z9 output")

    record: dict[str, Any] = {
        "schema_version": 1,
        "status": "PREPARING",
        "run_tag": run_tag,
        "run_index": args.run_index,
        "created_at_utc": utc_now(),
        "created_clock_pair": clock_pair(),
        "smoke_authorization_id": args.smoke_authorization_id,
        "smoke_authorization_status": authorization.get("status"),
        "smoke_authorization_quantitative_ready": authorization.get("quantitative_ready"),
        "smoke_authorization_artifact_path": str(authorization_path.resolve()),
        "smoke_authorization_artifact_sha256": sha256(authorization_path),
        "smoke_authorization_experiment_id": authorization.get("experiment_id"),
        "current_binding_snapshot": binding,
        "candidate_hashes": candidate_hashes,
        "required_hashes_match": True,
        "target_title": WINDOW_TITLE,
        "expected_target_cg_rect": EXPECTED_TARGET_CG,
        "poll_interval_ms": 50,
        "collector_jsonl_output_limit_bytes": admission_evidence["collector_jsonl_output_limit_bytes"],
        "transient_evidence_limit_bytes": admission_evidence["transient_evidence_limit_bytes"],
        "transient_evidence_directories": [str(run_directory), str(observer.directory)],
        "pre_seconds": PRE_SECONDS,
        "full_cover_seconds": FULL_COVER_SECONDS,
        "restored_seconds": RESTORED_SECONDS,
        "runner_command": [sys.executable, str(RUNNER), mode, str(baseline_index),
                            args.smoke_authorization_id, "--z9-replay"],
        "runner_sha256": sha256(RUNNER),
        "process_runner_sha256": candidate_hashes.get("process_runner"),
        "collector_source_path": str(COLLECTOR_SOURCE.resolve()),
        "collector_source_sha256": sha256(COLLECTOR_SOURCE),
        "collector_binary_path": str(collector_binary.resolve()),
        "collector_binary_sha256": sha256(collector_binary),
        "final_gate0_post_path": str(final_post_path),
        "storage_admission": admission_evidence,
        "replay1_validity_binding": replay1_binding,
        "detector_source_path": str(DETECTOR.resolve()),
        "detector_source_sha256": sha256(DETECTOR),
        "neutral_coverer_sha256": sha256(COVERER),
        "neutral_coverer_launched": False,
        "finder_pid": current_finder_pid,
        "finder_window_created_by_this_run": False,
        "finder_window_closed_by_this_run": False,
        "observer": observer.controller_evidence(),
        "timeline_path": str(events_path),
        "poller_path": str(poll_path),
        "status_path": str(controller_path),
    }
    atomic_json(controller_path, record)

    event_stream = events_path.open("x", encoding="utf-8")
    poll_log = poll_log_path.open("x", encoding="utf-8")
    runner_log = runner_log_path.open("x", encoding="utf-8")
    collector: subprocess.Popen[bytes] | None = None
    runner: subprocess.Popen[bytes] | None = None
    finder_window_id: int | None = None
    finder_cg_id: int | None = None
    finder_title: str | None = None
    finder_folder: pathlib.Path | None = None
    finder_initial_script_bounds: list[int] | None = None
    finder_stage_bounds: dict[str, float] | None = None
    runner_result: dict[str, Any] | None = None
    observer_started = False
    completed_measured_sequence = False
    measured_pre_completed = False
    cleanup_errors: list[str] = []
    poller_rows: list[dict[str, Any]] = []
    new_poller_samples: list[dict[str, Any]] = []
    observer_samples: list[dict[str, Any]] = []
    telemetry_rows: list[dict[str, Any]] = []
    notification_rows: list[dict[str, Any]] = []
    identity_tuples: list[tuple[Any, ...]] = []
    layer_configuration_rows: list[dict[str, Any]] = []
    identity_change_rows: list[dict[str, Any]] = []
    public_identity_rows: list[dict[str, Any]] = []
    public_identity_sample_indices: set[int] = set()
    recent_trace_events: list[dict[str, Any]] = []
    runner_output_tail = ""
    last_identity_error: str | None = None
    transient_budget_guard: dict[str, Any] = {"triggered": False}
    phase_timeline: dict[str, Any] = {}
    target_handoff: dict[str, Any] | None = None
    target_pid: int | None = None
    target_trace_path: pathlib.Path | None = None
    trace_tailer: JSONLTail | None = None
    poll_tailer = JSONLTail(poll_path)
    observer_tailer = JSONLTail(observer.samples_path)
    target_link_latest: dict[str, Any] | None = None
    display_inventory: list[dict[str, Any]] = []
    target_external_latest: dict[str, Any] | None = None
    accepted_identity_tuple: tuple[Any, ...] | None = None
    accepted_public_cg_id: int | None = None
    accepted_identity_start_mach_ns: int | None = None

    def exact_finder_action(action: str, script: str,
                            timeout: float = 20.0,
                            expect_present_after: bool = True,
                            expect_front_after: bool = False
                            ) -> subprocess.CompletedProcess[str]:
        if not measured_pre_completed:
            raise RuntimeError(
                f"{action}: refusing Finder treatment action before successful action-free measured PRE")
        actual_pid = finder_pid()
        if actual_pid != current_finder_pid:
            raise RuntimeError(
                f"Finder PID changed from {current_finder_pid} to {actual_pid}; refusing window-ID action")
        read_new_sources()
        pre_sample = latest_control_sample()
        if (not isinstance(pre_sample, dict) or pre_sample.get("event") != "sample"
                or pre_sample.get("all_window_query_succeeded") is not True
                or pre_sample.get("onscreen_window_query_succeeded") is not True
                or not isinstance(pre_sample.get("finder_windows"), list)
                or not isinstance(pre_sample.get("finder_onscreen_windows"), list)):
            raise RuntimeError(f"{action}: no fresh complete same-PID WindowServer inventory before action")
        pre_query_begin, pre_query_end = control_query_bounds(pre_sample)
        precheck_mach_ns = mach_ns()
        if (type(pre_query_begin) is not int or type(pre_query_end) is not int
                or pre_query_end < pre_query_begin or precheck_mach_ns < pre_query_end
                or precheck_mach_ns - pre_query_end > MAX_FINDER_ACTION_INVENTORY_AGE_NS):
            raise RuntimeError(f"{action}: pre-action WindowServer inventory is stale or unbounded")
        pre_action_inventory = {
            "sample_index": pre_sample.get("sample_index"),
            "query_started_mach_ns": pre_query_begin,
            "query_completed_mach_ns": pre_query_end,
            "precheck_mach_ns": precheck_mach_ns,
            "age_at_precheck_ns": precheck_mach_ns - pre_query_end,
            "freshness_limit_ns": MAX_FINDER_ACTION_INVENTORY_AGE_NS,
            "same_pid_all_rows": pre_sample["finder_windows"],
            "same_pid_onscreen_rows": pre_sample["finder_onscreen_windows"],
        }
        pre_identity = None
        if finder_window_id is not None:
            pre_identity = require_controlled_finder_row(pre_sample, action, "before")
        result = run_osascript(event_stream, action, script,
                               pre_query_end_mach_ns=pre_query_end,
                               pre_action_inventory=pre_action_inventory,
                               timeout=timeout)
        action_started_event = next((row for row in reversed(read_all_jsonl(events_path))
                                     if row.get("event") == "finder_action"
                                     and row.get("action") == action), None)
        action_begin_mach_ns = (action_started_event.get("begin", {}).get("mach_absolute_time_ns")
                                if isinstance(action_started_event, dict) else None)
        if (type(action_begin_mach_ns) is not int or action_begin_mach_ns < pre_query_end
                or action_begin_mach_ns - pre_query_end > MAX_FINDER_ACTION_INVENTORY_AGE_NS):
            raise RuntimeError(f"{action}: pre-action WindowServer inventory exceeded its 500 ms age bound")
        pre_action_inventory["action_begin_mach_ns"] = action_begin_mach_ns
        pre_action_inventory["age_at_action_begin_ns"] = action_begin_mach_ns - pre_query_end
        if finder_window_id is not None:
            action_event = next((row for row in reversed(read_all_jsonl(events_path))
                                 if row.get("event") == "finder_action"
                                 and row.get("action") == action), None)
            action_end_mach_ns = (action_event.get("end", {}).get("mach_absolute_time_ns")
                                  if isinstance(action_event, dict) else None)
            if type(action_end_mach_ns) is not int:
                raise RuntimeError(f"{action}: action completion lacks its exact Mach timestamp")
            action_returned = clock_pair()
            post_sample = wait_for_controlled_finder_sample(
                action_end_mach_ns, action,
                expect_present_after, expect_front_after)
            append_event({
                "event": "finder_action_identity_guard",
                "action": action,
                "stage": "before_and_after",
                "finder_pid": current_finder_pid,
                "finder_apple_script_window_id": finder_window_id,
                "finder_cg_window_id": finder_cg_id,
                "finder_title": finder_title,
                "finder_layer": (record.get("finder_identity", {}).get("layer")
                                 if isinstance(record.get("finder_identity"), dict) else None),
                "pre_action_inventory": pre_action_inventory,
                "before": pre_identity,
                "after": (None if not expect_present_after else
                          require_controlled_finder_row(post_sample, action, "after",
                                                        expect_front=expect_front_after)),
                "after_absence_sample": (post_sample if not expect_present_after else None),
                "action_returned": action_returned,
            })
            if not expect_present_after:
                record["finder_close_external_first_proof"] = {
                    "sample_index": post_sample.get("sample_index"),
                    "query_started_mach_absolute_ns": control_query_bounds(post_sample)[0],
                    "query_completed_mach_absolute_ns": control_query_bounds(post_sample)[1],
                    "exact_cg_window_id_absent": finder_cg_id not in {
                        row.get("cg_window_id") for row in post_sample.get("finder_windows", [])
                        if isinstance(row, dict) and row.get("owner_pid") == current_finder_pid},
                    "unique_title_absent": not any(
                        row.get("title") == finder_title
                        for row in post_sample.get("finder_windows", [])
                        if isinstance(row, dict) and row.get("owner_pid") == current_finder_pid),
                }
        return result

    def append_event(value: dict[str, Any]) -> None:
        event_stream.write(json.dumps(value, sort_keys=True) + "\n")
        event_stream.flush()

    def read_new_sources() -> None:
        nonlocal target_external_latest, display_inventory, runner_output_tail, last_identity_error
        new_identity_samples: list[dict[str, Any]] = []
        for row in poll_tailer.read_new():
            poller_rows.append(row)
            if row.get("event") == "capture_start":
                display_inventory = row.get("display_inventory") or []
            elif row.get("event") == "sample":
                target_external_latest = row
                new_poller_samples.append(row)
                new_identity_samples.append(row)
        observer_samples.extend(row for row in observer_tailer.read_new()
                                if row.get("event") == "sample")
        if trace_tailer is not None:
            for row in trace_tailer.read_new():
                if row.get("event") == "window_telemetry":
                    telemetry_rows.append(row)
                    identity = telemetry_tuple(row)
                    identity_tuples.append(identity)
                    if accepted_identity_tuple is not None and identity != accepted_identity_tuple:
                        identity_change_rows.append({
                            "timestamp_mach_ns": row.get("ts_mach_ns"),
                            "before": list(accepted_identity_tuple),
                            "after": list(identity),
                            "notification_name": row.get("notification_name"),
                            "trigger": row.get("trigger"),
                        })
                        last_identity_error = "tracked WineMetalLayer/NSView/NSWindow identity changed"
                    if row.get("notification_name") is not None:
                        notification_rows.append(row)
                elif row.get("event") == "layer_configuration":
                    layer_configuration_rows.append(row)
                elif row.get("event") in {
                    "vkAcquireNextImageKHR_begin", "vkAcquireNextImageKHR_end",
                    "vkQueueSubmit2_end", "vkQueuePresentKHR_begin", "next_drawable_begin",
                    "next_drawable_end", "presented_callback", "drawable_release",
                }:
                    recent_trace_events.append(row)
                    cutoff = mach_ns() - 5_000_000_000
                    recent_trace_events[:] = [event for event in recent_trace_events
                                              if type(event.get("ts_mach_ns")) is int
                                              and event["ts_mach_ns"] >= cutoff
                                              or (event.get("event") == "next_drawable_begin"
                                                  and type(event.get("call_begin_mach_ns")) is int
                                                  and event["call_begin_mach_ns"] >= cutoff)]
        try:
            runner_output_tail = runner_log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
        except OSError:
            pass
        for sample in new_identity_samples:
            current_link(sample)
        if observer_started and not transient_budget_guard["triggered"]:
            transient_allocated = allocated_owned_tree_bytes(
                [run_directory, observer.directory])
            transient_limit = admission_evidence["transient_evidence_limit_bytes"]
            stop_margin = min(64 * 1024 * 1024, max(8 * 1024 * 1024, transient_limit // 20))
            stop_threshold = transient_limit - stop_margin
            if transient_allocated >= stop_threshold:
                transient_budget_guard.update({
                    "triggered": True,
                    "allocated_bytes": transient_allocated,
                    "limit_bytes": transient_limit,
                    "stop_threshold_bytes": stop_threshold,
                    "stop_margin_bytes": stop_margin,
                })
                record["transient_output_budget_guard"] = dict(transient_budget_guard)
                append_event({"event": "TRANSIENT_OUTPUT_BUDGET_GUARD",
                              **transient_budget_guard, "stamp": clock_pair()})
                if collector is not None and collector.poll() is None:
                    collector.terminate()
                    try:
                        record["collector_returncode"] = collector.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        collector.kill()
                        record["collector_returncode"] = collector.wait(timeout=5)
                if (observer.process is not None and observer.process.poll() is None):
                    observer.process.terminate()
                    try:
                        record["transient_guard_observer_returncode"] = observer.process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        observer.process.kill()
                        record["transient_guard_observer_returncode"] = observer.process.wait(timeout=5)
                raise RuntimeError(
                    "transient evidence reached its reserved stop threshold; collectors were stopped before the cap")

    def control_query_bounds(sample: dict[str, Any]) -> tuple[int | None, int | None]:
        if sample.get("event") == "pre_handoff_sample":
            return (sample.get("onscreen_window_query_started_mach_absolute_ns"),
                    sample.get("onscreen_window_query_completed_mach_absolute_ns"))
        return (sample.get("onscreen_query_started_mach_absolute_ns"),
                sample.get("onscreen_query_completed_mach_absolute_ns"))

    def latest_control_sample() -> dict[str, Any] | None:
        samples = [row for row in poller_rows
                   if row.get("event") in {"sample", "pre_handoff_sample"}
                   and type(control_query_bounds(row)[1]) is int]
        return max(samples, key=lambda row: control_query_bounds(row)[1], default=None)

    def require_controlled_finder_row(sample: dict[str, Any] | None, action: str,
                                       stage: str, *, expect_front: bool = False
                                       ) -> dict[str, Any]:
        if not isinstance(sample, dict) or sample.get("event") not in {
                "sample", "pre_handoff_sample"}:
            raise RuntimeError(f"{action} {stage}: no external WindowServer sample")
        if (sample.get("all_window_query_succeeded") is not True
                or sample.get("onscreen_window_query_succeeded") is not True):
            raise RuntimeError(f"{action} {stage}: WindowServer query failed")
        begin_ns, end_ns = control_query_bounds(sample)
        now_ns = mach_ns()
        if (type(begin_ns) is not int or type(end_ns) is not int
                or now_ns - end_ns < 0 or now_ns - end_ns > 500_000_000):
            raise RuntimeError(f"{action} {stage}: external identity sample is stale or unbounded")
        all_rows = sample.get("finder_windows")
        screen_rows = sample.get("finder_onscreen_windows")
        if not isinstance(all_rows, list) or not isinstance(screen_rows, list):
            raise RuntimeError(f"{action} {stage}: Finder inventory rows are unavailable")
        identity = record.get("finder_identity")
        if (not isinstance(identity, dict) or type(finder_cg_id) is not int
                or not isinstance(finder_title, str)):
            raise RuntimeError(f"{action} {stage}: exact Finder public identity is not bound")
        exact_all = [row for row in all_rows if isinstance(row, dict)
                     and row.get("owner_pid") == current_finder_pid
                     and row.get("cg_window_id") == finder_cg_id]
        title_all = [row for row in all_rows if isinstance(row, dict)
                     and row.get("owner_pid") == current_finder_pid
                     and row.get("title") == finder_title]
        exact_screen = [row for row in screen_rows if isinstance(row, dict)
                        and row.get("owner_pid") == current_finder_pid
                        and row.get("cg_window_id") == finder_cg_id
                        and row.get("title") == finder_title
                        and row.get("onscreen") is True]
        if (len(exact_all) != 1 or len(title_all) != 1 or len(exact_screen) != 1
                or exact_all[0].get("title") != finder_title
                or exact_all[0].get("layer") != identity.get("layer")
                or exact_screen[0].get("layer") != identity.get("layer")
                or not isinstance(identity.get("alpha"), (int, float))
                or not isinstance(exact_all[0].get("alpha"), (int, float))
                or not isinstance(exact_screen[0].get("alpha"), (int, float))
                or abs(float(exact_all[0]["alpha"]) - float(identity["alpha"])) > 0.001
                or abs(float(exact_screen[0]["alpha"]) - float(identity["alpha"])) > 0.001):
            raise RuntimeError(f"{action} {stage}: exact Finder PID/CG ID/title/layer/alpha guard failed")
        row = exact_screen[0]
        proof: dict[str, Any] = {
            "pid": current_finder_pid, "cg_window_id": finder_cg_id,
            "title": finder_title, "layer": row.get("layer"), "alpha": row.get("alpha"),
            "rank_all_windows": row.get("rank_all_windows"),
            "rank_onscreen_windows": row.get("rank_onscreen_windows"),
            "query_started_mach_ns": begin_ns, "query_completed_mach_ns": end_ns,
            "sample_index": sample.get("sample_index"), "row": row,
        }
        if expect_front:
            if sample.get("event") != "sample":
                raise RuntimeError(f"{action} {stage}: target handoff is required for front-order proof")
            link = current_link(sample)
            target_row = link.get("target_row") if link.get("status") == "PASS" else None
            finder_rank = row.get("rank_onscreen_windows")
            target_rank = target_row.get("rank_onscreen_windows") if isinstance(target_row, dict) else None
            if (link.get("cg_window_id") != accepted_public_cg_id
                    or type(finder_rank) is not int or type(target_rank) is not int
                    or finder_rank >= target_rank):
                raise RuntimeError(f"{action} {stage}: exact Finder window is not proven in front of the accepted target")
            proof["target_cg_window_id"] = link.get("cg_window_id")
            proof["target_rank_onscreen_windows"] = target_rank
            proof["finder_proven_in_front"] = True
        return proof

    def wait_for_controlled_finder_sample(after_mach_ns: int, action: str,
                                          expect_present: bool, expect_front: bool
                                          ) -> dict[str, Any]:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            read_new_sources()
            newer = []
            for sample in poller_rows:
                if sample.get("event") not in {"sample", "pre_handoff_sample"}:
                    continue
                query_start, query_end = control_query_bounds(sample)
                if (type(query_start) is int and query_start > after_mach_ns
                        and type(query_end) is int and query_end >= query_start
                        and sample.get("all_window_query_succeeded") is True
                        and sample.get("onscreen_window_query_succeeded") is True):
                    newer.append(sample)
            newer.sort(key=lambda row: control_query_bounds(row)[1])
            for sample in newer:
                rows = sample.get("finder_windows")
                screen_rows = sample.get("finder_onscreen_windows")
                if not isinstance(rows, list) or not isinstance(screen_rows, list):
                    continue
                if expect_present:
                    try:
                        require_controlled_finder_row(sample, action, "after",
                                                      expect_front=expect_front)
                    except RuntimeError:
                        continue
                    return sample
                exact_present = any(isinstance(row, dict)
                                    and row.get("owner_pid") == current_finder_pid
                                    and row.get("cg_window_id") == finder_cg_id
                                    for row in rows + screen_rows)
                title_present = any(isinstance(row, dict)
                                    and row.get("owner_pid") == current_finder_pid
                                    and row.get("title") == finder_title for row in rows)
                if not exact_present and not title_present:
                    return sample
            time.sleep(0.02)
        raise TimeoutError(f"{action}: no fresh external proof followed the Finder action")

    def wait_for_new_finder_sample(after_mach_ns: int, before_ids: set[Any],
                                   title: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            read_new_sources()
            candidates = [sample for sample in poller_rows
                          if sample.get("event") in {"sample", "pre_handoff_sample"}
                          and type(control_query_bounds(sample)[0]) is int
                          and control_query_bounds(sample)[0] > after_mach_ns
                          and sample.get("all_window_query_succeeded") is True
                          and sample.get("onscreen_window_query_succeeded") is True]
            candidates.sort(key=lambda sample: control_query_bounds(sample)[1] or -1)
            for sample in candidates:
                all_rows = sample.get("finder_windows")
                screen_rows = sample.get("finder_onscreen_windows")
                if not isinstance(all_rows, list) or not isinstance(screen_rows, list):
                    continue
                title_rows = [row for row in all_rows if isinstance(row, dict)
                              and row.get("owner_pid") == current_finder_pid
                              and row.get("title") == title]
                new_rows = [row for row in title_rows
                            if row.get("cg_window_id") not in before_ids]
                if len(title_rows) != 1 or len(new_rows) != 1:
                    continue
                candidate_id = new_rows[0].get("cg_window_id")
                onscreen = [row for row in screen_rows if isinstance(row, dict)
                            and row.get("owner_pid") == current_finder_pid
                            and row.get("cg_window_id") == candidate_id
                            and row.get("title") == title
                            and row.get("onscreen") is True]
                if type(candidate_id) is int and len(onscreen) == 1:
                    return sample, new_rows[0], onscreen[0]
            time.sleep(0.02)
        raise TimeoutError("new dedicated Finder window was not uniquely proven in a later public sample")

    def recover_created_finder_identity_for_cleanup() -> bool:
        nonlocal finder_cg_id
        if finder_cg_id is not None or finder_window_id is None or not finder_title:
            return finder_cg_id is not None
        read_new_sources()
        sample = latest_control_sample()
        if (not isinstance(sample, dict)
                or sample.get("all_window_query_succeeded") is not True
                or sample.get("onscreen_window_query_succeeded") is not True):
            return False
        inventory = record.get("finder_creation_pre_action_inventory", {})
        before_ids = set(inventory.get("same_pid_window_ids", []))
        all_rows = sample.get("finder_windows")
        screen_rows = sample.get("finder_onscreen_windows")
        if not isinstance(all_rows, list) or not isinstance(screen_rows, list):
            return False
        new_title_rows = [row for row in all_rows if isinstance(row, dict)
                          and row.get("owner_pid") == current_finder_pid
                          and row.get("title") == finder_title
                          and row.get("cg_window_id") not in before_ids]
        if len(new_title_rows) != 1:
            return False
        candidate = new_title_rows[0]
        candidate_id = candidate.get("cg_window_id")
        screen = [row for row in screen_rows if isinstance(row, dict)
                  and row.get("owner_pid") == current_finder_pid
                  and row.get("cg_window_id") == candidate_id
                  and row.get("title") == finder_title
                  and row.get("onscreen") is True]
        if (type(candidate_id) is not int or len(screen) != 1
                or type(screen[0].get("layer")) is not int
                or not isinstance(screen[0].get("alpha"), (int, float))):
            return False
        finder_cg_id = candidate_id
        record["finder_identity"] = {
            "pid": current_finder_pid, "cg_window_id": finder_cg_id,
            "title": finder_title, "layer": screen[0].get("layer"),
            "alpha": screen[0].get("alpha"),
            "initial_bounds_cg": rect_from_row(screen[0]),
            "rank_all_windows": screen[0].get("rank_all_windows"),
            "rank_onscreen_windows": screen[0].get("rank_onscreen_windows"),
            "recovered_for_exact_cleanup": True,
        }
        record["finder_created_windowserver_identity"] = record["finder_identity"]
        return True

    def guarded_finder_script(body: str) -> str:
        if type(finder_window_id) is not int or not isinstance(finder_title, str):
            raise RuntimeError("cannot bind Finder AppleScript window ID and unique target title")
        return (
            'tell application "Finder"\n'
            f'set z9Window to window id {finder_window_id}\n'
            f'if (id of z9Window as integer) is not {finder_window_id} then error "Z9 Finder ID mismatch"\n'
            f'if (name of target of z9Window as text) is not {applescript_literal(finder_title)} '
            'then error "Z9 Finder target title mismatch"\n'
            f'{body.rstrip()}\n'
            'end tell')

    def run_guarded_ax_raise(action: str) -> dict[str, Any]:
        if not measured_pre_completed:
            raise RuntimeError(
                f"{action}: refusing Finder treatment action before successful action-free measured PRE")
        actual_pid = finder_pid()
        if actual_pid != current_finder_pid:
            raise RuntimeError(f"{action}: Finder PID changed; refusing AX action")
        read_new_sources()
        pre_sample = latest_control_sample()
        pre_identity = require_controlled_finder_row(pre_sample, action, "before")
        pre_query_begin, pre_query_end = control_query_bounds(pre_sample)
        if type(pre_query_end) is not int or type(pre_query_begin) is not int:
            raise RuntimeError(f"{action}: AX raise preguard lacks exact WindowServer query bounds")
        pre_inventory = {
            "sample_index": pre_sample.get("sample_index"),
            "query_started_mach_ns": pre_query_begin,
            "query_completed_mach_ns": pre_query_end,
            "freshness_limit_ns": MAX_FINDER_ACTION_INVENTORY_AGE_NS,
            "same_pid_all_rows": pre_sample.get("finder_windows"),
            "same_pid_onscreen_rows": pre_sample.get("finder_onscreen_windows"),
        }
        begin = clock_pair()
        inventory_age_ns = begin["mach_absolute_time_ns"] - pre_query_end
        if not 0 <= inventory_age_ns <= MAX_FINDER_ACTION_INVENTORY_AGE_NS:
            end = clock_pair()
            append_event({"event": "finder_action_guard_rejected", "action": action,
                          "reason": "pre-action WindowServer inventory exceeded its freshness bound",
                          "begin": begin, "end": end,
                          "inventory_age_at_action_boundary_ns": inventory_age_ns,
                          "pre_action_inventory": pre_inventory})
            raise RuntimeError(f"{action}: fresh inventory expired before AX raise")
        pre_inventory["action_begin_mach_ns"] = begin["mach_absolute_time_ns"]
        pre_inventory["age_at_action_begin_ns"] = inventory_age_ns
        command = [sys.executable, str(AX_WINDOW_HELPER), str(current_finder_pid),
                   "raise", str(finder_title)]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=15, check=False)
        except BaseException as error:
            end = clock_pair()
            append_event({"event": "finder_action", "action": action, "begin": begin,
                          "end": end, "exception": f"{type(error).__name__}: {error}",
                          "helper_path": str(AX_WINDOW_HELPER.resolve()),
                          "pre_action_inventory": pre_inventory,
                          "helper_sha256": sha256(AX_WINDOW_HELPER), "command": command})
            raise
        end = clock_pair()
        append_event({"event": "finder_action", "action": action, "begin": begin,
                      "end": end, "returncode": result.returncode,
                      "stdout": result.stdout.strip(), "stderr": result.stderr.strip(),
                      "inventory_age_at_action_boundary_ns": inventory_age_ns,
                      "pre_action_inventory": pre_inventory,
                      "helper_path": str(AX_WINDOW_HELPER.resolve()),
                      "helper_sha256": sha256(AX_WINDOW_HELPER), "command": command})
        if result.returncode != 0:
            raise RuntimeError(f"{action}: exact-title Finder AXRaise failed: {result.stderr.strip()}")
        post_sample = wait_for_controlled_finder_sample(
            end["mach_absolute_time_ns"], action, True, True)
        post_identity = require_controlled_finder_row(
            post_sample, action, "after", expect_front=True)
        append_event({"event": "finder_action_identity_guard", "action": action,
                      "stage": "before_and_after", "finder_pid": current_finder_pid,
                      "finder_apple_script_window_id": finder_window_id,
                      "finder_cg_window_id": finder_cg_id, "finder_title": finder_title,
                      "before": pre_identity, "after": post_identity})
        return {"begin": begin, "end": end, "helper_path": str(AX_WINDOW_HELPER.resolve()),
                "helper_sha256": sha256(AX_WINDOW_HELPER), "command": command,
                "returncode": result.returncode, "stdout": result.stdout.strip(),
                "stderr": result.stderr.strip(), "before": pre_identity,
                "after": post_identity}

    def latest_telemetry() -> dict[str, Any] | None:
        return max(telemetry_rows, key=lambda row: row.get("ts_mach_ns", 0), default=None)

    def current_link(sample_override: dict[str, Any] | None = None) -> dict[str, Any]:
        nonlocal last_identity_error
        telemetry = latest_telemetry()
        if telemetry is None:
            return {"status": "NO_TARGET_TELEMETRY"}
        external_sample = sample_override or target_external_latest
        result = target_correspondence(external_sample, telemetry,
                                       target_pid or -1, display_inventory)
        cover_state = (same_query_cover_state(external_sample)
                       if isinstance(external_sample, dict)
                       else {"status": "UNKNOWN", "cover_rows": []})
        result["same_query_cover_state"] = cover_state
        if (not isinstance(target_handoff, dict)
                or not isinstance(external_sample, dict)
                or external_sample.get("target_unix_pid") != target_pid
                or external_sample.get("run_id") != target_handoff.get("run_id")
                or external_sample.get("experiment_token")
                != target_handoff.get("experiment_token")):
            result["status"] = "PUBLIC_SAMPLE_PID_RUN_OR_TOKEN_MISMATCH"
        evidence = external_sample.get("same_query_cover_evidence") if isinstance(external_sample, dict) else None
        if (result.get("status") == "PASS"
                and (not isinstance(evidence, dict)
                     or evidence.get("target_cg_window_id") != result.get("cg_window_id"))):
            result["status"] = "SAME_QUERY_TARGET_JOIN_MISMATCH"
        result["telemetry"] = telemetry_state(telemetry)
        result["telemetry_identity_tuple"] = list(telemetry_tuple(telemetry))
        result["telemetry_monotonic_ns"] = telemetry.get("ts_mach_ns")
        telemetry_stamp = telemetry.get("ts_mach_ns")
        now_mach = mach_ns()
        telemetry_fresh = (type(telemetry_stamp) is int and 0 <= now_mach - telemetry_stamp
                           <= OBSERVER_SAMPLE_MAX_AGE_NS)
        result["target_telemetry_age_ns"] = (now_mach - telemetry_stamp
                                               if type(telemetry_stamp) is int else None)
        result["target_telemetry_fresh"] = telemetry_fresh
        query_end = (external_sample.get("onscreen_query_completed_mach_absolute_ns")
                     if isinstance(external_sample, dict) else None)
        query_aligned = (type(query_end) is int and type(telemetry_stamp) is int
                         and abs(query_end - telemetry_stamp) <= OBSERVER_SAMPLE_MAX_AGE_NS)
        external_fresh = (type(query_end) is int and 0 <= now_mach - query_end <= 500_000_000)
        result["external_query_age_ns"] = (now_mach - query_end
                                            if type(query_end) is int else None)
        result["external_query_fresh"] = external_fresh
        result["target_query_telemetry_delta_ns"] = (
            query_end - telemetry_stamp
            if type(query_end) is int and type(telemetry_stamp) is int else None)
        result["target_query_telemetry_aligned"] = query_aligned
        result["layer_class"] = (layer_configuration_rows[-1].get("layer_class")
                                  if layer_configuration_rows else None)
        result["layer_class_identity"] = (layer_configuration_rows[-1]
                                          if layer_configuration_rows else None)
        if telemetry.get("window_associated") is not True:
            result["status"] = "TARGET_WINDOW_NOT_ASSOCIATED"
        if telemetry.get("window_lookup_path") != "direct_delegate":
            result["status"] = "NOT_DIRECT_DELEGATE"
        if (not isinstance(telemetry.get("layer_id"), int) or telemetry["layer_id"] <= 0
                or not isinstance(telemetry.get("window_view_id"), int)
                or telemetry["window_view_id"] <= 0
                or not isinstance(telemetry.get("window_id"), int)
                or telemetry["window_id"] <= 0
                or not isinstance(telemetry.get("window_number"), int)
                or telemetry["window_number"] <= 0):
            result["status"] = "INCOMPLETE_LAYER_VIEW_WINDOW_IDENTITY"
        if (not layer_configuration_rows
                or layer_configuration_rows[-1].get("layer_class") != "WineMetalLayer"
                or layer_configuration_rows[-1].get("layer_is_cametal_layer") is not True
                or layer_configuration_rows[-1].get("layer_id") != telemetry.get("layer_id")):
            result["status"] = "TRACKED_LAYER_CLASS_IDENTITY_MISMATCH"
        if not telemetry_fresh or not query_aligned or not external_fresh:
            result["status"] = "STALE_OR_UNALIGNED_TARGET_TELEMETRY"
        if accepted_identity_tuple is not None and telemetry_tuple(telemetry) != accepted_identity_tuple:
            result["status"] = "TRACKED_WINDOW_ROLE_CHANGED"
        if (accepted_public_cg_id is not None and result.get("status") == "PASS"
                and result.get("cg_window_id") != accepted_public_cg_id):
            result["status"] = "PUBLIC_WINDOWSERVER_ID_CHANGED"
            result["accepted_public_cg_window_id"] = accepted_public_cg_id
            result["observed_public_cg_window_id"] = result.get("cg_window_id")
            if last_identity_error is None:
                last_identity_error = "unique public target CGWindow ID changed after canonical acceptance"
        if (isinstance(external_sample, dict)
                and type(external_sample.get("sample_index")) is int
                and external_sample["sample_index"] not in public_identity_sample_indices):
            public_identity_sample_indices.add(external_sample["sample_index"])
            public_identity_rows.append({
                "sample_index": external_sample["sample_index"],
                "sample_started_mach_ns": external_sample.get("sample_started_mach_absolute_ns"),
                "query_started_mach_ns": external_sample.get("onscreen_query_started_mach_absolute_ns"),
                "query_completed_mach_ns": query_end,
                "onscreen_query_succeeded": external_sample.get("onscreen_window_query_succeeded"),
                "all_window_query_succeeded": external_sample.get("all_window_query_succeeded"),
                "target_telemetry_mach_ns": telemetry_stamp,
                "status": result.get("status"),
                "cg_window_id": result.get("cg_window_id"),
                "same_query_target_cg_window_id": (
                    external_sample.get("same_query_cover_evidence", {}).get("target_cg_window_id")
                    if isinstance(external_sample.get("same_query_cover_evidence"), dict) else None),
                "same_query_target_join_status": (
                    external_sample.get("same_query_cover_evidence", {}).get("target_join_status")
                    if isinstance(external_sample.get("same_query_cover_evidence"), dict) else None),
                "same_query_cover_complete": (
                    external_sample.get("same_query_cover_evidence", {}).get("cover_evidence_complete")
                    if isinstance(external_sample.get("same_query_cover_evidence"), dict) else None),
                "same_query_cover_state": cover_state.get("status"),
                "telemetry_identity_tuple": list(telemetry_tuple(telemetry)),
                "window_number": telemetry.get("window_number"),
                "window_number_support_status": result.get("window_number_runtime_support_status"),
                "target_row": result.get("target_row"),
            })
        return result

    def latest_observer() -> dict[str, Any] | None:
        return observer.latest()

    def full_cover_rows(sample: dict[str, Any] | None,
                        exact: bool = False) -> list[dict[str, Any]]:
        if not isinstance(sample, dict):
            return []
        link = current_link(sample)
        target_row = link.get("target_row") if link.get("status") == "PASS" else None
        if exact:
            return finder_covers_target(sample, target_row, current_finder_pid,
                                        exact_cg_id=finder_cg_id, exact_title=finder_title,
                                        exact_layer=(record.get("finder_identity", {}).get("layer")
                                                     if isinstance(record.get("finder_identity"), dict)
                                                     else None))
        return finder_covers_target(sample, target_row, current_finder_pid)

    def canonical_precondition() -> dict[str, Any]:
        read_new_sources()
        handoff = read_json(observer.handoff_path)
        if not isinstance(handoff, dict) or target_pid is None:
            return {"status": "NO_HANDOFF"}
        proof = current_canonical_uncovered_streak(
            observer_samples, target_pid=target_pid, latest_state=latest_observer())
        telemetry = latest_telemetry()
        external_sample = target_external_latest
        link = current_link()
        latest_sample = observer_samples[-1] if observer_samples else None
        streak_pass = proof is not None and proof.get("stable_duration_ns", 0) >= 2_000_000_000
        sample_fresh = False
        if isinstance(latest_sample, dict):
            sample_ns = latest_sample.get("sampledAtMonotonicNS")
            now_mono = time.monotonic_ns()
            sample_fresh = (type(sample_ns) is int and now_mono >= sample_ns
                            and now_mono - sample_ns <= OBSERVER_SAMPLE_MAX_AGE_NS)
        identity_points = [row for row in telemetry_rows
                           if row.get("window_associated") is True
                           and row.get("window_lookup_path") == "direct_delegate"
                           and type(row.get("ts_mach_ns")) is int]
        now_mach = mach_ns()
        identity_stability = identity_stability_summary(
            identity_points, now_mach_ns=now_mach)
        stable_identity = identity_stability.get("stable") is True
        recent_public = sorted(
            (row for row in public_identity_rows
             if type(row.get("query_completed_mach_ns")) is int
             and now_mach - 2_200_000_000 <= row["query_completed_mach_ns"] <= now_mach),
            key=lambda row: row["query_completed_mach_ns"],
        )
        public_sample_starts = [row.get("sample_started_mach_ns") for row in recent_public]
        public_sample_gaps = [right - left for left, right in
                              zip(public_sample_starts, public_sample_starts[1:])
                              if type(left) is int and type(right) is int]
        public_interval_ns = (
            recent_public[-1].get("query_completed_mach_ns", 0)
            - recent_public[0].get("query_started_mach_ns", 0)
            if recent_public else 0)
        public_rows_complete = bool(recent_public) and all(
            row.get("status") == "PASS"
            and row.get("all_window_query_succeeded") is True
            and row.get("onscreen_query_succeeded") is True
            and row.get("same_query_target_join_status") == "PASS"
            and row.get("same_query_cover_complete") is True
            and row.get("same_query_cover_state") == "CLEAR"
            and row.get("same_query_target_cg_window_id") == row.get("cg_window_id")
            for row in recent_public)
        stable_public_identity = bool(
            len(recent_public) >= 3
            and public_interval_ns >= 2_000_000_000
            and public_rows_complete
            and len(public_sample_gaps) == len(recent_public) - 1
            and all(0 < gap <= 100_000_000 for gap in public_sample_gaps)
            and len({row.get("cg_window_id") for row in recent_public}) == 1
            and recent_public[-1]["cg_window_id"] == link.get("cg_window_id")
            and now_mach - recent_public[-1]["query_completed_mach_ns"] <= 500_000_000)
        state = telemetry_state(telemetry) if telemetry else {}
        state_ok = (state.get("window_visible") is True
                    and state.get("window_miniaturized") is False
                    and state.get("window_occlusion_state") == 8194
                    and state.get("visible_bit_set") is True
                    and state.get("app_active") is True
                    and state.get("window_key") is True
                    and state.get("window_main") is True)
        renderer = renderer_healthy(
            recent_trace_events, mach_ns(), expected_pid=target_pid,
            expected_token=handoff.get("experiment_token", ""))
        current_cover_state = link.get("same_query_cover_state", {}).get("status")
        no_finder_cover = (isinstance(external_sample, dict)
                           and current_cover_state == "CLEAR"
                           and not full_cover_rows(external_sample))
        target_external_onscreen = link.get("status") == "PASS"
        canonical = (proof is not None and streak_pass and sample_fresh and stable_identity
                     and stable_public_identity
                     and state_ok and renderer.get("healthy") is True
                     and no_finder_cover and target_external_onscreen)
        return {
            "status": "PASS" if canonical else "WAITING",
            "canonical_streak": proof,
            "observer_sample_fresh": sample_fresh,
            "stable_i1_identity": stable_identity,
            "i1_identity_stability": identity_stability,
            "stable_public_cg_identity": stable_public_identity,
            "public_cg_identity_sample_count_last_2s": len(recent_public),
            "public_cg_identity_max_sample_start_gap_ns": max(public_sample_gaps, default=None),
            "public_cg_identity_interval_ns": public_interval_ns,
            "public_cg_window_id": link.get("cg_window_id"),
            "same_query_cover_state": current_cover_state,
            "same_query_cover_rows": link.get("same_query_cover_state", {}).get("cover_rows"),
            "identity_tuple": list(telemetry_tuple(telemetry)) if telemetry else None,
            "state": state,
            "state_ok": state_ok,
            "renderer_health": renderer,
            "external_target_correlation": link,
            "no_finder_full_cover": no_finder_cover,
            "target_pid": target_pid,
            "telemetry_sample_count": len(identity_points),
        }

    def set_stage_for_target(target_rect: dict[str, float],
                             display_inventory_rows: list[dict[str, Any]],
                             target_screen_id: int | None = None) -> dict[str, float]:
        expected_screen_id = target_screen_id if type(target_screen_id) is int else 1
        screen = next((row for row in display_inventory_rows
                       if row.get("display_id") == expected_screen_id), None)
        cg_bounds = screen.get("bounds_cg") if isinstance(screen, dict) else None
        if not isinstance(cg_bounds, dict):
            raise RuntimeError("expected display CG bounds are unavailable")
        left, top = float(cg_bounds["x"]), float(cg_bounds["y"])
        right, bottom = left + float(cg_bounds["width"]), top + float(cg_bounds["height"])
        stage_x = target_rect["x"] + target_rect["width"] + 16
        stage_y = target_rect["y"]
        if stage_x + target_rect["width"] > right:
            stage_x = left
            stage_y = target_rect["y"] + target_rect["height"] + 16
        if stage_y + target_rect["height"] > bottom or stage_x + target_rect["width"] > right:
            stage_x = max(left, target_rect["x"] - target_rect["width"] - 16)
            stage_y = target_rect["y"]
        stage = {"x": stage_x, "y": stage_y,
                 "width": target_rect["width"], "height": target_rect["height"]}
        if rects_overlap(stage, target_rect):
            raise RuntimeError("could not choose a non-overlapping Finder staging rectangle")
        return stage

    try:
        observer.build_and_start()
        observer_started = True
        record["observer_started"] = True
        record["observer_started_monotonic_ns"] = time.monotonic_ns()

        collector = subprocess.Popen(
            [str(collector_binary), str(handoff_path), str(completion_path), str(poll_path),
             "50", str(current_finder_pid),
             str(admission_evidence["collector_jsonl_output_limit_bytes"])],
            cwd=str(run_directory), stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=poll_log, close_fds=True)
        record["collector_pid"] = collector.pid
        record["collector_started_clock_pair"] = clock_pair()
        atomic_json(controller_path, record)

        pre_tailer = JSONLTail(poll_path)
        pre_rows: list[dict[str, Any]] = []
        desktop_preflight: dict[str, Any] | None = None
        pre_deadline = time.monotonic() + 30
        while time.monotonic() < pre_deadline and (not pre_rows or desktop_preflight is None):
            new_rows = pre_tailer.read_new()
            pre_rows.extend(row for row in new_rows
                            if row.get("event") == "pre_handoff_sample")
            desktop_preflight = next((row for row in new_rows
                                      if row.get("event") == "desktop_preflight"),
                                     desktop_preflight)
            time.sleep(0.02)
        if not pre_rows or desktop_preflight is None:
            raise RuntimeError("WindowServer collector lacks fresh desktop eligibility/pre-creation inventory")
        record["desktop_preflight"] = desktop_preflight
        desktop_eligible = desktop_preflight_eligible(desktop_preflight, os.getuid())
        if not desktop_eligible:
            record["status"] = "DESKTOP_PREFLIGHT_BLOCKED"
            record["desktop_preflight_eligible"] = False
            atomic_json(controller_path, record)
            raise RuntimeError(f"current desktop eligibility was not established safely: {desktop_preflight}")
        record["desktop_preflight_eligible"] = True
        atomic_json(controller_path, record)
        display_inventory = pre_rows[-1].get("display_inventory")
        if (not isinstance(display_inventory, list)
                or not any(isinstance(row, dict) and row.get("display_id") == 1
                           for row in display_inventory)):
            raise RuntimeError("pre-handoff display inventory is unavailable for controlled Finder staging")
        initial_finder_ids = {row.get("cg_window_id") for row in
                              (pre_rows[-1].get("finder_windows") or [])
                              if isinstance(row, dict) and row.get("owner_pid") == current_finder_pid}
        record["finder_precreation_inventory"] = {
            "sample_index": pre_rows[-1].get("sample_index"),
            "query_started_mach_absolute_ns": pre_rows[-1].get(
                "onscreen_window_query_started_mach_absolute_ns"),
            "query_completed_mach_absolute_ns": pre_rows[-1].get(
                "onscreen_window_query_completed_mach_absolute_ns"),
            "same_pid_window_ids": sorted(value for value in initial_finder_ids
                                           if type(value) is int),
            "rows": pre_rows[-1].get("finder_windows"),
        }
        folder_title = f"FGMetal-Z9-Finder-{uuid.uuid4().hex[:10]}"
        finder_title = folder_title
        finder_folder = run_directory / folder_title
        finder_folder.mkdir(mode=0o700, exist_ok=False)
        record["finder_unique_title"] = finder_title
        record["finder_folder_path"] = str(finder_folder)
        atomic_json(controller_path, record)

        baseline_command = [sys.executable, str(RUNNER), mode, str(baseline_index),
                            args.smoke_authorization_id, "--z9-replay"]
        baseline_environment = os.environ.copy()
        baseline_environment.update(observer.runner_environment())
        baseline_environment["FG_Z9_FINAL_POST_PATH"] = str(final_post_path)
        runner = subprocess.Popen(baseline_command, cwd=str(REPO), env=baseline_environment,
                                  stdin=subprocess.DEVNULL, stdout=runner_log,
                                  stderr=subprocess.STDOUT, close_fds=True)
        record["runner_pid"] = runner.pid
        record["runner_started_clock_pair"] = clock_pair()
        record["status"] = "RUNNING_WAITING_FOR_HANDOFF"
        atomic_json(controller_path, record)

        handoff_deadline = time.monotonic() + 1200
        while time.monotonic() < handoff_deadline:
            target_handoff = read_json(handoff_path)
            if target_handoff is not None:
                break
            if runner.poll() is not None:
                raise RuntimeError(f"Wine runner exited before target handoff: {runner.returncode}")
            time.sleep(0.05)
        if not isinstance(target_handoff, dict):
            raise TimeoutError("runner did not produce the exact lifecycle-bound target handoff")
        target_pid = target_handoff.get("target_unix_pid")
        if type(target_pid) is not int or target_pid <= 1:
            raise RuntimeError("target handoff lacks an exact valid Wine PID")
        run_id = target_handoff.get("run_id")
        if not isinstance(run_id, str) or not run_id.startswith("step11d3b-baseline-"):
            raise RuntimeError("target handoff run ID is not the requested baseline phase")
        target_trace_path = pathlib.Path(target_handoff.get("target_trace_path", ""))
        expected_trace = EXPERIMENT / "runs" / run_id / "host-traces" / f"host-trace.{target_pid}.jsonl"
        if target_trace_path != expected_trace or target_trace_path.is_symlink():
            raise RuntimeError("target trace path does not match exact handoff run ID and Wine PID")
        trace_tailer = JSONLTail(target_trace_path)
        record["target_handoff"] = target_handoff
        record["target_pid"] = target_pid
        record["run_id"] = run_id
        record["target_trace_path"] = str(target_trace_path)
        record["target_trace_sha256_at_handoff"] = sha256(target_trace_path) if target_trace_path.exists() else None
        record["status"] = "WAITING_FOR_CANONICAL_PRECONDITION"
        atomic_json(controller_path, record)

        # The actual target may differ from the expected frame. Compute the exact
        # BR8 disjoint staging bounds after fresh I1 acceptance; create/raise the
        # dedicated Finder window only after the full action-free measured PRE.
        precondition_deadline = time.monotonic() + 180
        precondition: dict[str, Any] | None = None
        stage_proven = False
        while time.monotonic() < precondition_deadline:
            precondition = canonical_precondition()
            if precondition.get("status") == "PASS":
                accepted_telemetry = latest_telemetry()
                accepted_correlation = precondition.get("external_target_correlation", {})
                if (not isinstance(accepted_telemetry, dict)
                        or type(accepted_telemetry.get("ts_mach_ns")) is not int
                        or type(accepted_correlation.get("cg_window_id")) is not int):
                    raise RuntimeError("canonical acceptance lacks exact telemetry/public-window identity")
                accepted_identity_tuple = telemetry_tuple(accepted_telemetry)
                accepted_public_cg_id = accepted_correlation["cg_window_id"]
                accepted_identity_start_mach_ns = accepted_telemetry["ts_mach_ns"]
                record["accepted_i1_identity_tuple"] = list(accepted_identity_tuple)
                record["accepted_i1_identity_start_mach_ns"] = accepted_identity_start_mach_ns
                record["accepted_public_cg_window_id"] = accepted_public_cg_id
                atomic_json(controller_path, record)
                target_actual = precondition["external_target_correlation"]["converted_target_frame_cg"]
                fresh_screen_id = latest_telemetry().get("screen_id") if latest_telemetry() else None
                desired_stage = set_stage_for_target(
                    target_actual, display_inventory,
                    fresh_screen_id if type(fresh_screen_id) is int else None)
                latest_sample = target_external_latest
                if isinstance(latest_sample, dict) and not full_cover_rows(latest_sample):
                    precondition["disjoint_stage_target_bounds_cg"] = desired_stage
                    stage_proven = True
                    break
            time.sleep(0.1)
        if precondition is None or precondition.get("status") != "PASS" or not stage_proven:
            raise RuntimeError(f"canonical uncovered PRE precondition not proven: {precondition}")
        if (accepted_identity_tuple is None or accepted_public_cg_id is None
                or accepted_identity_start_mach_ns is None):
            raise RuntimeError("canonical I1 identity was not locked before measured PRE")
        record["canonical_precondition"] = precondition
        finder_stage_bounds = precondition["disjoint_stage_target_bounds_cg"]
        target_rect = precondition["external_target_correlation"]["converted_target_frame_cg"]

        # The measured PRE is a complete action-free baseline. No Finder
        # treatment window is created or touched until this interval succeeds.
        pre_begin = clock_pair()
        pre_begin_mono = pre_begin["python_monotonic_after_ns"]
        phase_timeline["PRE"] = {
            "begin": pre_begin,
            "duration_seconds": PRE_SECONDS,
            "finder_actions": [],
            "treatment_window_created": False,
        }
        record["status"] = "MEASURED_PRE"
        record["measured_phases"] = phase_timeline
        atomic_json(controller_path, record)
        append_event({"event": "phase_begin", "phase": "PRE", "stamp": pre_begin})
        pre_end_deadline = pre_begin_mono + PRE_SECONDS * 1_000_000_000
        while time.monotonic_ns() < pre_end_deadline:
            read_new_sources()
            current = current_link()
            if current.get("status") != "PASS":
                raise RuntimeError("I1 target correlation failed during measured PRE")
            if current.get("same_query_cover_state", {}).get("status") != "CLEAR":
                raise RuntimeError("same-query public-window cover evidence is not clear during PRE")
            renderer = renderer_healthy(
                recent_trace_events, mach_ns(), expected_pid=target_pid,
                expected_token=target_handoff.get("experiment_token", ""))
            if renderer.get("healthy") is not True:
                raise RuntimeError(f"actual replay renderer left healthy PRE: {renderer}")
            telemetry = latest_telemetry()
            state = telemetry_state(telemetry) if telemetry else {}
            if (state.get("window_visible") is not True
                    or state.get("window_miniaturized") is not False):
                raise RuntimeError("target ceased to be visible or became miniaturized during uncovered PRE")
            if full_cover_rows(target_external_latest):
                raise RuntimeError("a Finder full cover appeared during uncovered PRE")
            time.sleep(0.02)

        pre_end = clock_pair()
        measured_pre_ns = pre_end["python_monotonic_after_ns"] - pre_begin_mono
        phase_timeline["PRE"]["end"] = pre_end
        phase_timeline["PRE"]["measured_seconds"] = measured_pre_ns / 1_000_000_000
        phase_timeline["PRE"]["measured_duration_ns"] = measured_pre_ns
        append_event({"event": "phase_end", "phase": "PRE", "stamp": pre_end})
        required_pre_ns = PRE_SECONDS * 1_000_000_000
        if (measured_pre_ns < required_pre_ns
                or measured_pre_ns - required_pre_ns > MEASURED_PRE_DURATION_TOLERANCE_NS):
            raise RuntimeError("uncovered PRE duration was outside 20.000 s plus the 250 ms capture tolerance")

        # Persist the successful, action-free PRE boundary before any Finder
        # activation or window creation can occur.
        phase_timeline["PRE"]["completed_successfully"] = True
        phase_timeline["PRE"]["treatment_window_created"] = False
        record["status"] = "MEASURED_PRE_COMPLETE"
        record["measured_phases"] = phase_timeline
        atomic_json(controller_path, record)
        measured_pre_completed = True

        post_pre_actions: dict[str, Any] = {}
        if "finder_activate_request" not in post_pre_actions:
            activation_script = ('tell application "Finder"\n'
                                 'activate\nreturn "activated"\nend tell')
            result = exact_finder_action("finder_activate_request", activation_script)
            activation_event = next((row for row in reversed(read_all_jsonl(events_path))
                                     if row.get("event") == "finder_action"
                                     and row.get("action") == "finder_activate_request"), None)
            activation_begin = (activation_event.get("begin", {}).get("mach_absolute_time_ns")
                                if isinstance(activation_event, dict) else None)
            if type(activation_begin) is not int:
                raise RuntimeError("Finder activation request lacks its exact Mach timestamp")
            post_pre_actions["finder_activate_request"] = {
                "pre_completed_mach_ns": pre_end["mach_absolute_time_ns"],
                "action": activation_event,
                "result": result.stdout.strip(),
            }
        if "finder_window_creation" not in post_pre_actions:
            read_new_sources()
            before_creation = latest_control_sample()
            if (not isinstance(before_creation, dict)
                    or before_creation.get("all_window_query_succeeded") is not True
                    or before_creation.get("onscreen_window_query_succeeded") is not True):
                raise RuntimeError("fresh complete Finder inventory unavailable before dedicated-window creation")
            before_rows = before_creation.get("finder_windows")
            if not isinstance(before_rows, list):
                raise RuntimeError("Finder same-PID inventory missing before dedicated-window creation")
            before_creation_ids = {row.get("cg_window_id") for row in before_rows
                                   if isinstance(row, dict)
                                   and row.get("owner_pid") == current_finder_pid}
            before_start, before_end = control_query_bounds(before_creation)
            if (type(before_end) is not int or type(before_start) is not int
                    or before_end < before_start or before_end > mach_ns()):
                raise RuntimeError("dedicated Finder creation pre-action inventory has invalid Mach bounds")
            record["finder_creation_pre_action_inventory"] = {
                "sample_index": before_creation.get("sample_index"),
                "query_started_mach_ns": before_start,
                "query_completed_mach_ns": before_end,
                "freshness_limit_ns": MAX_FINDER_ACTION_INVENTORY_AGE_NS,
                "same_pid_window_ids": sorted(value for value in before_creation_ids
                                               if type(value) is int),
                "rows": before_rows,
            }
            create_script = (
                'tell application "Finder"\n'
                'set z9Window to make new Finder window\n'
                f'set target of z9Window to (POSIX file {applescript_literal(str(finder_folder))}) as alias\n'
                'return (id of z9Window as text) & "|" & ((bounds of z9Window) as text)\n'
                'end tell')
            create_result = exact_finder_action("finder_window_creation", create_script)
            creation_event = next((row for row in reversed(read_all_jsonl(events_path))
                                   if row.get("event") == "finder_action"
                                   and row.get("action") == "finder_window_creation"), None)
            creation_begin = (creation_event.get("begin", {}).get("mach_absolute_time_ns")
                              if isinstance(creation_event, dict) else None)
            if type(creation_begin) is not int:
                raise RuntimeError("Finder window creation lacks its exact Mach timestamp")
            creation_boundary_inventory = creation_event.get("pre_action_inventory", {})
            if (not isinstance(creation_boundary_inventory, dict)
                    or type(creation_boundary_inventory.get("query_started_mach_ns")) is not int
                    or type(creation_boundary_inventory.get("query_completed_mach_ns")) is not int
                    or not isinstance(creation_boundary_inventory.get("same_pid_all_rows"), list)
                    or not isinstance(creation_boundary_inventory.get("same_pid_onscreen_rows"), list)
                    or type(creation_boundary_inventory.get("age_at_action_begin_ns")) is not int
                    or creation_boundary_inventory["age_at_action_begin_ns"]
                    > MAX_FINDER_ACTION_INVENTORY_AGE_NS):
                raise RuntimeError("Finder creation lacks its fresh action-boundary inventory proof")
            if (creation_begin < before_end
                    or creation_begin - before_end > MAX_FINDER_ACTION_INVENTORY_AGE_NS):
                raise RuntimeError("dedicated Finder creation inventory was stale at the action boundary")
            record["finder_creation_pre_action_inventory"].update({
                "action_begin_mach_ns": creation_begin,
                "age_at_action_begin_ns": creation_begin - before_end,
                "action_boundary_preguard": creation_boundary_inventory,
            })
            create_fields = create_result.stdout.strip().split("|", maxsplit=1)
            create_values = parse_ints(" ".join(create_fields))
            if create_values:
                finder_window_id = create_values[0]
                record["finder_window_created_by_this_run"] = True
                record["finder_apple_script_window_id"] = finder_window_id
            if len(create_fields) != 2 or len(create_values) < 5:
                raise RuntimeError("dedicated Finder creation returned an unparseable AppleScript identity")
            finder_initial_script_bounds = create_values[1:5]
            record["finder_initial_script_bounds"] = finder_initial_script_bounds
            record["finder_pid_after_creation"] = finder_pid()
            if record["finder_pid_after_creation"] != current_finder_pid:
                raise RuntimeError("Finder PID changed during exact-window creation")
            creation_returned = clock_pair()
            creation_end_mach_ns = (creation_event.get("end", {}).get("mach_absolute_time_ns")
                                     if isinstance(creation_event, dict) else None)
            if type(creation_end_mach_ns) is not int:
                raise RuntimeError("Finder creation completion lacks its exact Mach timestamp")
            creation_sample, creation_all_row, creation_screen_row = wait_for_new_finder_sample(
                creation_end_mach_ns, before_creation_ids, finder_title)
            finder_cg_id = creation_all_row.get("cg_window_id")
            if type(finder_cg_id) is not int:
                raise RuntimeError("new Finder public identity lacks an exact CGWindow ID")
            screen_layer = creation_screen_row.get("layer")
            screen_alpha = creation_screen_row.get("alpha")
            if (type(screen_layer) is not int or not isinstance(screen_alpha, (int, float))
                    or float(screen_alpha) < 0.999):
                raise RuntimeError("new Finder window has incomplete layer/alpha identity")
            query_start, query_end = control_query_bounds(creation_sample)
            record["finder_creation_external_first_proof"] = {
                "sample_index": creation_sample.get("sample_index"),
                "query_started_mach_absolute_ns": query_start,
                "query_completed_mach_absolute_ns": query_end,
                "row": creation_screen_row,
            }
            record["finder_created_windowserver_identity"] = {
                "pid": current_finder_pid,
                "cg_window_id": finder_cg_id,
                "title": finder_title,
                "creation_bounds_script": finder_initial_script_bounds,
                "creation_bounds_cg": rect_from_row(creation_screen_row),
                "layer": screen_layer,
                "alpha": screen_alpha,
                "rank_all_windows": creation_screen_row.get("rank_all_windows"),
                "rank_onscreen_windows": creation_screen_row.get("rank_onscreen_windows"),
                "new_to_pre_action_inventory": True,
            }
            record["finder_identity"] = {
                "pid": current_finder_pid,
                "cg_window_id": finder_cg_id,
                "title": finder_title,
                "creation_bounds_script": finder_initial_script_bounds,
                "creation_bounds_cg": rect_from_row(creation_screen_row),
                "initial_bounds_cg": rect_from_row(creation_screen_row),
                "layer": screen_layer,
                "alpha": screen_alpha,
                "rank_all_windows": creation_screen_row.get("rank_all_windows"),
                "rank_onscreen_windows": creation_screen_row.get("rank_onscreen_windows"),
            }
            post_pre_actions["finder_window_creation"] = {
                "pre_completed_mach_ns": pre_end["mach_absolute_time_ns"],
                "apple_script_window_id": finder_window_id,
                "cg_window_id": finder_cg_id,
                "creation_returned": creation_returned,
                "first_external_proof": record["finder_creation_external_first_proof"],
                "created_row": creation_all_row,
            }
            atomic_json(controller_path, record)
        if "finder_exact_window_raise_request" not in post_pre_actions:
            raise_result = run_guarded_ax_raise("finder_exact_window_raise_request")
            raise_result["pre_completed_mach_ns"] = pre_end["mach_absolute_time_ns"]
            post_pre_actions["finder_exact_window_raise_request"] = raise_result
        if "finder_disjoint_stage_request" not in post_pre_actions:
            desired_stage = precondition["disjoint_stage_target_bounds_cg"]
            stage_script = guarded_finder_script(
                f'set bounds of z9Window to {{{int(desired_stage["x"])}, '
                f'{int(desired_stage["y"])}, '
                f'{int(desired_stage["x"] + desired_stage["width"])}, '
                f'{int(desired_stage["y"] + desired_stage["height"])}}}\n'
                'return (id of z9Window as text) & "|" & ((bounds of z9Window) as text)')
            result = exact_finder_action("finder_disjoint_stage_request", stage_script,
                                         expect_front_after=True)
            stage_event = next((row for row in reversed(read_all_jsonl(events_path))
                                if row.get("event") == "finder_action"
                                and row.get("action") == "finder_disjoint_stage_request"), None)
            stage_begin = (stage_event.get("begin", {}).get("mach_absolute_time_ns")
                           if isinstance(stage_event, dict) else None)
            if type(stage_begin) is not int:
                raise RuntimeError("Finder disjoint stage request lacks its exact Mach timestamp")
            values = parse_ints(" ".join(result.stdout.strip().split("|", maxsplit=1)))
            if len(values) < 5 or values[0] != finder_window_id:
                raise RuntimeError("BR8-matched disjoint stage action lost exact Finder window identity")
            finder_stage_bounds = {"x": float(values[1]), "y": float(values[2]),
                                   "width": float(values[3] - values[1]),
                                   "height": float(values[4] - values[2])}
            if rects_overlap(finder_stage_bounds, target_rect):
                raise RuntimeError("BR8-matched Finder stage overlaps fresh target geometry")
            post_pre_actions["finder_disjoint_stage_request"] = {
                "pre_completed_mach_ns": pre_end["mach_absolute_time_ns"],
                "result": result.stdout.strip(),
                "requested_bounds_cg": desired_stage,
                "returned_bounds_cg": finder_stage_bounds,
            }
            stage_sample = latest_control_sample()
            stage_identity = require_controlled_finder_row(
                stage_sample, "finder_disjoint_stage_request", "after",
                expect_front=True)
            record["finder_stage_requested_bounds"] = desired_stage
            record["finder_stage_script_returned_bounds"] = finder_stage_bounds
            record["finder_identity"].update({
                "initial_bounds_cg": finder_stage_bounds,
                "layer": stage_identity.get("layer"),
                "alpha": stage_identity.get("alpha"),
                "rank_all_windows": stage_identity.get("rank_all_windows"),
                "rank_onscreen_windows": stage_identity.get("rank_onscreen_windows"),
            })
            post_pre_actions["finder_disjoint_stage_request"]["identity_after"] = stage_identity
            atomic_json(controller_path, record)
        record["finder_setup_actions"] = post_pre_actions
        record["measured_phases"] = phase_timeline
        atomic_json(controller_path, record)

        current_target = current_link()
        if current_target.get("status") != "PASS":
            raise RuntimeError("fresh I1 correlation failed at the PRE-to-cover boundary")
        if not rect_equal(current_target.get("converted_target_frame_cg"), target_rect, tolerance=0.5):
            target_rect = current_target["converted_target_frame_cg"]
            record["target_rectangle_changed_at_cover_boundary"] = True
            record["target_rectangle_at_cover_boundary_cg"] = target_rect
        if finder_stage_bounds is None or rects_overlap(finder_stage_bounds, target_rect):
            raise RuntimeError(
                "the exact Finder window is not proven staged disjoint from the fresh target frame")
        cover_bounds = {
            "left": math.floor(target_rect["x"]),
            "top": math.floor(target_rect["y"]),
            "right": math.ceil(target_rect["x"] + target_rect["width"]),
            "bottom": math.ceil(target_rect["y"] + target_rect["height"]),
        }
        cover_script = guarded_finder_script(
            f'set bounds of z9Window to {{{cover_bounds["left"]}, {cover_bounds["top"]}, '
            f'{cover_bounds["right"]}, {cover_bounds["bottom"]}}}\n'
            'return (id of z9Window as text) & "|" & ((bounds of z9Window) as text)')
        record["status"] = "FINDER_FULL_COVER_REQUESTED"
        record["finder_full_cover_requested_bounds_cg"] = {
            "x": cover_bounds["left"], "y": cover_bounds["top"],
            "width": cover_bounds["right"] - cover_bounds["left"],
            "height": cover_bounds["bottom"] - cover_bounds["top"],
        }
        atomic_json(controller_path, record)
        cover_result = exact_finder_action("finder_full_cover_position_request", cover_script,
                                            expect_front_after=True)
        cover_values = parse_ints(" ".join(cover_result.stdout.strip().split("|", maxsplit=1)))
        if len(cover_values) < 5 or cover_values[0] != finder_window_id:
            raise RuntimeError("Finder full-cover request did not identify the exact new Finder window")
        record["finder_full_cover_script_returned_bounds"] = cover_values[1:5]
        atomic_json(controller_path, record)

        cover_proof_deadline = time.monotonic() + 15
        cover_proof: dict[str, Any] | None = None
        while time.monotonic() < cover_proof_deadline:
            read_new_sources()
            sample = target_external_latest
            link = current_link()
            if (isinstance(sample, dict) and link.get("status") == "PASS"
                    and sample.get("onscreen_window_query_succeeded") is True):
                rows = full_cover_rows(sample, exact=True)
                all_finder_cover_rows = full_cover_rows(sample)
                if (len(rows) == 1 and len(all_finder_cover_rows) == 1
                        and all_finder_cover_rows[0].get("cg_window_id") == finder_cg_id
                        and same_query_cover_state(
                            sample, allowed_cover_identity=(current_finder_pid, finder_cg_id)
                        ).get("status") == "EXPECTED_COVER"):
                    cover_proof = sample
                    record["finder_full_cover_external_first_proof"] = {
                        "sample_index": sample.get("sample_index"),
                        "query_started_mach_absolute_ns": sample.get("onscreen_query_started_mach_absolute_ns"),
                        "query_completed_mach_absolute_ns": sample.get("onscreen_query_completed_mach_absolute_ns"),
                        "target_cg_window_id": link.get("cg_window_id"),
                        "finder_row": rows[0],
                        "target_row": link.get("target_row"),
                    }
                    record["status"] = "FINDER_FULL_COVER_PROVEN"
                    atomic_json(controller_path, record)
                    break
            time.sleep(0.01)
        if not isinstance(cover_proof, dict):
            raise RuntimeError("exact Finder full cover was not externally proven within 15 seconds")
        cover_start_mach = cover_proof.get("onscreen_query_completed_mach_absolute_ns")
        if type(cover_start_mach) is not int:
            raise RuntimeError("full-cover proof lacks its on-screen query-end Mach timestamp")
        cover_start_mono = cover_proof.get("onscreen_query_completed_monotonic_ns")
        if type(cover_start_mono) is not int:
            raise RuntimeError("full-cover proof lacks its on-screen query-end monotonic timestamp")
        phase_timeline["COVERED"] = {
            "begin": {"mach_absolute_time_ns": cover_start_mach,
                      "python_monotonic_ns": cover_start_mono,
                      "anchor": "first successful same-query on-screen containment and front-rank proof"},
            "duration_seconds": FULL_COVER_SECONDS,
        }
        append_event({"event": "phase_begin", "phase": "COVERED",
                      "sample_index": cover_proof.get("sample_index"),
                      "mach_absolute_time_ns": cover_start_mach,
                      "python_monotonic_ns": cover_start_mono})
        covered_deadline_mach = cover_start_mach + FULL_COVER_SECONDS * 1_000_000_000
        observed_cover_samples: list[int] = []
        new_poller_samples.clear()
        while mach_ns() < covered_deadline_mach:
            read_new_sources()
            current_samples = list(new_poller_samples)
            new_poller_samples.clear()
            for sample in current_samples:
                if (sample.get("onscreen_window_query_succeeded") is not True
                        or sample.get("all_window_query_succeeded") is not True):
                    raise RuntimeError("WindowServer query failed during the full-cover interval")
                exact_rows = full_cover_rows(sample, exact=True)
                all_rows = full_cover_rows(sample)
                if (len(exact_rows) == 1 and len(all_rows) == 1
                        and all_rows[0].get("cg_window_id") == finder_cg_id
                        and same_query_cover_state(
                            sample, allowed_cover_identity=(current_finder_pid, finder_cg_id)
                        ).get("status") == "EXPECTED_COVER"):
                    observed_cover_samples.append(int(sample.get("sample_index", -1)))
                else:
                    raise RuntimeError("the exact Finder full-cover state was not continuous in observed samples")
            current = current_link()
            if current.get("status") != "PASS":
                raise RuntimeError("I1 target correlation failed during Finder full cover")
            time.sleep(0.01)
        cover_end = clock_pair()
        phase_timeline["COVERED"]["end_request_begin"] = cover_end
        phase_timeline["COVERED"]["observed_full_cover_sample_indices"] = observed_cover_samples
        phase_timeline["COVERED"]["observed_full_cover_sample_count"] = len(observed_cover_samples)
        append_event({"event": "phase_hold_complete", "phase": "COVERED", "stamp": cover_end,
                      "observed_sample_count": len(observed_cover_samples)})

        restore_script = guarded_finder_script(
            f'set bounds of z9Window to {{{int(finder_stage_bounds["x"])}, '
            f'{int(finder_stage_bounds["y"])}, '
            f'{int(finder_stage_bounds["x"] + finder_stage_bounds["width"])}, '
            f'{int(finder_stage_bounds["y"] + finder_stage_bounds["height"])}}}\n'
            'return (id of z9Window as text) & "|" & ((bounds of z9Window) as text)')
        restore_result = exact_finder_action("finder_restore_staged_bounds_request", restore_script,
                                              expect_front_after=True)
        restore_values = parse_ints(" ".join(restore_result.stdout.strip().split("|", maxsplit=1)))
        if len(restore_values) < 5 or restore_values[0] != finder_window_id:
            raise RuntimeError("Finder restoration did not identify the exact new Finder window")
        record["finder_restore_script_returned_bounds"] = restore_values[1:5]

        restored_proof_deadline = time.monotonic() + 15
        restored_proof: dict[str, Any] | None = None
        while time.monotonic() < restored_proof_deadline:
            read_new_sources()
            sample = target_external_latest
            link = current_link()
            if (isinstance(sample, dict) and link.get("status") == "PASS"
                    and sample.get("onscreen_window_query_succeeded") is True):
                exact_row = [row for row in sample.get("finder_onscreen_windows", [])
                             if isinstance(row, dict) and row.get("owner_pid") == current_finder_pid
                             and row.get("cg_window_id") == finder_cg_id]
                if (len(exact_row) == 1 and not full_cover_rows(sample)
                        and same_query_cover_state(sample).get("status") == "CLEAR"):
                    restored_proof = sample
                    record["finder_restored_external_first_proof"] = {
                        "sample_index": sample.get("sample_index"),
                        "query_started_mach_absolute_ns": sample.get("onscreen_query_started_mach_absolute_ns"),
                        "query_completed_mach_absolute_ns": sample.get("onscreen_query_completed_mach_absolute_ns"),
                        "finder_row": exact_row[0], "target_row": link.get("target_row"),
                    }
                    record["status"] = "RESTORED_GEOMETRY_PROVEN"
                    atomic_json(controller_path, record)
                    break
            time.sleep(0.01)
        if not isinstance(restored_proof, dict):
            raise RuntimeError("restored no-full-cover state was not externally proven")
        restored_start_mach = restored_proof.get("onscreen_query_completed_mach_absolute_ns")
        restored_start_mono = restored_proof.get("onscreen_query_completed_monotonic_ns")
        if type(restored_start_mach) is not int or type(restored_start_mono) is not int:
            raise RuntimeError("restored geometry proof lacks a bounded on-screen query timestamp")
        phase_timeline["RESTORED"] = {
            "begin": {"mach_absolute_time_ns": restored_start_mach,
                      "python_monotonic_ns": restored_start_mono,
                      "anchor": "first successful same-query on-screen proof no longer fully covers"},
            "duration_seconds": RESTORED_SECONDS,
        }
        append_event({"event": "phase_begin", "phase": "RESTORED",
                      "sample_index": restored_proof.get("sample_index"),
                      "mach_absolute_time_ns": restored_start_mach,
                      "python_monotonic_ns": restored_start_mono})
        restored_deadline = restored_start_mach + RESTORED_SECONDS * 1_000_000_000
        while mach_ns() < restored_deadline:
            read_new_sources()
            current = current_link()
            if current.get("status") != "PASS":
                raise RuntimeError("I1 target correlation failed during restored phase")
            if (full_cover_rows(target_external_latest)
                    or same_query_cover_state(target_external_latest).get("status") != "CLEAR"):
                raise RuntimeError("a Finder full cover remained over the target during restored interval")
            time.sleep(0.01)
        restored_end = clock_pair()
        phase_timeline["RESTORED"]["end"] = restored_end
        phase_timeline["RESTORED"]["measured_seconds"] = (
            restored_end["mach_absolute_time_ns"] - restored_start_mach) / 1_000_000_000
        append_event({"event": "phase_end", "phase": "RESTORED", "stamp": restored_end})
        completed_measured_sequence = True

        # Close only our exact AppleScript Finder window after the measured 20/40/20.
        close_script = guarded_finder_script('close z9Window\nreturn "closed"')
        close_result = exact_finder_action("finder_close_exact_window_request", close_script,
                                           expect_present_after=False)
        record["finder_close_result"] = close_result.stdout.strip()
        record["finder_window_closed_by_this_run"] = close_result.stdout.strip() == "closed"
        record["status"] = "MEASURED_SEQUENCE_COMPLETE_UNMANIPULATED_TAIL"
        record["measured_phases"] = phase_timeline
        atomic_json(controller_path, record)
        try:
            finder_folder.rmdir()
            record["finder_folder_removed"] = True
        except OSError as error:
            record["finder_folder_removed"] = False
            record["finder_folder_cleanup_error"] = str(error)

        # Keep sampling and observing through the runner's full 300 s lifecycle.
        while runner.poll() is None:
            read_new_sources()
            if target_pid is not None and latest_telemetry() is not None:
                current_link()
            if finder_pid() != current_finder_pid:
                last_identity_error = "Finder PID changed after measured sequence"
            time.sleep(0.2)
        runner_output_tail = runner_log_path.read_text(encoding="utf-8", errors="replace")[-20000:]
        record["runner_returncode"] = runner.returncode
        record["runner_output_tail"] = runner_output_tail
        json_blocks = list(re.finditer(r"(?m)^\{\s*$", runner_output_tail))
        for match in reversed(json_blocks):
            try:
                value, _end = json.JSONDecoder().raw_decode(runner_output_tail[match.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("experiment_id") == run_id:
                runner_result = value
                break
        record["runner_result"] = runner_result
        run_result_path = EXPERIMENT / "runs" / run_id / "run-result.json"
        runner_run_result = read_json(run_result_path)
        record["runner_run_result_artifact"] = {
            "path": str(run_result_path),
            "sha256": sha256(run_result_path) if run_result_path.is_file()
            and not run_result_path.is_symlink() else None,
            "status": runner_run_result.get("status") if runner_run_result else None,
            "is_valid": runner_run_result.get("is_valid") if runner_run_result else None,
            "experiment_id": runner_run_result.get("experiment_id") if runner_run_result else None,
            "phase": runner_run_result.get("phase") if runner_run_result else None,
            "duration_seconds_requested": (runner_run_result.get("duration_seconds_requested")
                                            if runner_run_result else None),
            "process_returncode": runner_run_result.get("process_returncode") if runner_run_result else None,
            "run_result_error": runner_run_result.get("execution_result_error") if runner_run_result else None,
        }
        final_gate_snapshot = read_json(final_post_path)
        final_gate_binding = {
            "path": str(final_post_path),
            "sha256": sha256(final_post_path)
            if final_post_path.is_file() and not final_post_path.is_symlink() else None,
        }
        runner_final_binding = (runner_result.get("gate0_post_final_artifact")
                                if isinstance(runner_result, dict) else None)
        signed_limit = admission_evidence.get("signed_run_output_limit_bytes")
        final_gate_good = bool(
            isinstance(final_gate_snapshot, dict)
            and final_gate_snapshot.get("gate0_status") == "PASS"
            and final_gate_snapshot.get("free_bytes", 0) >= 50 * GIB
            and final_gate_snapshot.get("scoped_fgmetal_bytes", 55 * GIB + 1) <= 55 * GIB
            and final_gate_snapshot.get("signed_output_within_reservation") is True
            and final_gate_snapshot.get("signed_output_allocated_bytes", 0)
            <= final_gate_snapshot.get("signed_output_limit_bytes", -1)
            and final_gate_snapshot.get("signed_output_limit_bytes")
            == signed_limit
            and isinstance(runner_result, dict)
            and runner_result.get("gate0_post_final_good") is True
            and runner_final_binding == final_gate_binding
            and runner_result.get("signed_output_limit_bytes") == signed_limit
            and runner_result.get("signed_output_allocated_bytes_post_final")
            == final_gate_snapshot.get("signed_output_allocated_bytes"))
        record["runner_final_gate0_post"] = (
            final_gate_binding if final_gate_binding["sha256"] is not None else None)
        record["runner_final_gate0_post_snapshot"] = final_gate_snapshot
        record["runner_final_gate0_post_good"] = final_gate_good
        record["finder_actions"] = [row for row in read_all_jsonl(events_path)
                                     if row.get("event") == "finder_action"]
        record["status"] = "RUNNER_COMPLETED"
        record["measured_sequence_completed"] = completed_measured_sequence
        record["measured_phases"] = phase_timeline
        record["process_lifecycle_complete_clock_pair"] = clock_pair()
        atomic_json(controller_path, record)
    except BaseException as error:
        record["status"] = "INVALID_OR_INCOMPLETE"
        record["controller_error"] = f"{type(error).__name__}: {error}"
        record["measured_sequence_completed"] = completed_measured_sequence
        record["measured_phases"] = phase_timeline
        record["failure_clock_pair"] = clock_pair()
        atomic_json(controller_path, record)
        append_event({"event": "controller_error", "error": record["controller_error"],
                      "stamp": record["failure_clock_pair"]})
    finally:
        if finder_window_id is not None and not record.get("finder_window_closed_by_this_run"):
            try:
                if not recover_created_finder_identity_for_cleanup():
                    raise RuntimeError("cannot bind the created Finder window's exact public identity for cleanup")
                cleanup_result = exact_finder_action(
                    "cleanup_close_only_exact_new_finder_window",
                    guarded_finder_script('close z9Window\nreturn "closed"'), timeout=10,
                    expect_present_after=False)
                record["finder_window_closed_by_this_run"] = cleanup_result.stdout.strip() == "closed"
            except BaseException as cleanup_error:
                cleanup_errors.append(f"exact Finder window cleanup: {type(cleanup_error).__name__}: {cleanup_error}")
        if finder_window_id is not None and "finder_folder" in locals():
            try:
                if finder_folder.exists() and not finder_folder.is_symlink():
                    finder_folder.rmdir()
                    record["finder_folder_removed"] = True
            except BaseException as cleanup_error:
                cleanup_errors.append(f"owned Finder folder cleanup: {type(cleanup_error).__name__}: {cleanup_error}")
        if runner is not None:
            try:
                runner_return = runner.wait(timeout=RUNNER_WATCHDOG_SECONDS)
                record["runner_returncode"] = runner_return
            except subprocess.TimeoutExpired:
                cleanup_errors.append("baseline runner exceeded outer 1500-second watchdog; termination requested")
                runner.terminate()
                try:
                    record["runner_returncode"] = runner.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    runner.kill()
                    record["runner_returncode"] = runner.wait(timeout=10)
        if observer_started:
            try:
                interim = read_json(EXPERIMENT / "runs" / str(record.get("run_id")) / "execution-result.json")
                if runner is None:
                    if observer.process is not None and observer.process.poll() is None:
                        observer.process.terminate()
                        try:
                            observer.process.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            observer.process.kill()
                            observer.process.wait(timeout=10)
                    record["observer_result"] = "ABORTED_BEFORE_RUNNER_START"
                else:
                    if (interim is not None and not observer.completion_path.exists()
                            and type(interim.get("target_unix_pid")) is int
                            and type(interim.get("experiment_token")) is str):
                        observer.signal_completion(
                            run_id=str(record.get("run_id")), status="CONTROLLER_RECOVERY_AFTER_TARGET_EXIT",
                            runner_returncode=record.get("runner_returncode"),
                            target_pid=interim.get("target_unix_pid"),
                            experiment_token=interim.get("experiment_token"))
                        record["observer_completion_recovered_by_controller"] = True
                    record["observer_result"] = observer.wait(timeout_seconds=OBSERVER_SAMPLE_MAX_AGE_NS / 1e9
                                                               + 1800)
            except BaseException as cleanup_error:
                cleanup_errors.append(f"lifecycle observer cleanup: {type(cleanup_error).__name__}: {cleanup_error}")
        read_new_sources()
        if collector is not None:
            try:
                collector_return = collector.wait(timeout=45)
                record["collector_returncode"] = collector_return
            except subprocess.TimeoutExpired:
                cleanup_errors.append("WindowServer collector did not finish; termination requested")
                collector.terminate()
                try:
                    record["collector_returncode"] = collector.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    collector.kill()
                    record["collector_returncode"] = collector.wait(timeout=10)
        poller_rows = read_all_jsonl(poll_path)
        all_samples = [row for row in poller_rows if row.get("event") == "sample"]
        pre_samples = [row for row in poller_rows if row.get("event") == "pre_handoff_sample"]
        all_gaps = [row.get("inter_sample_gap_ns") for row in pre_samples + all_samples
                    if type(row.get("inter_sample_gap_ns")) is int]
        handoff_boundary_gap = None
        if pre_samples and all_samples:
            last_pre_start = pre_samples[-1].get("sample_started_monotonic_ns")
            first_lifecycle_start = all_samples[0].get("sample_started_monotonic_ns")
            if type(last_pre_start) is int and type(first_lifecycle_start) is int:
                handoff_boundary_gap = first_lifecycle_start - last_pre_start
                all_gaps.append(handoff_boundary_gap)
        finder_action_rows = [row for row in read_all_jsonl(events_path)
                              if row.get("event") == "finder_action"]

        def finder_action_bounds(action_name: str) -> tuple[int | None, int | None]:
            action = next((row for row in finder_action_rows
                           if row.get("action") == action_name), None)
            begin = action.get("begin") if isinstance(action, dict) else None
            end = action.get("end") if isinstance(action, dict) else None
            begin_ns = begin.get("mach_absolute_time_ns") if isinstance(begin, dict) else None
            end_ns = end.get("mach_absolute_time_ns") if isinstance(end, dict) else None
            return (begin_ns if type(begin_ns) is int else None,
                    end_ns if type(end_ns) is int else None)

        cover_action_begin, cover_action_end = finder_action_bounds(
            "finder_full_cover_position_request")
        restore_action_begin, restore_action_end = finder_action_bounds(
            "finder_restore_staged_bounds_request")
        covered_phase = phase_timeline.get("COVERED", {})
        covered_start_mono = covered_phase.get("begin", {}).get("python_monotonic_ns")
        restore_action_mono = next((row.get("begin", {}).get("python_monotonic_before_ns")
                                    for row in finder_action_rows
                                    if row.get("action") == "finder_restore_staged_bounds_request"
                                    and isinstance(row.get("begin"), dict)), None)
        cover_identity = ((current_finder_pid, finder_cg_id)
                          if type(current_finder_pid) is int and type(finder_cg_id) is int
                          else None)
        if (type(covered_start_mono) is int and type(restore_action_mono) is int
                and cover_identity is not None):
            record["covered_sampling_summary"] = covered_sampling_summary(
                all_samples, start_monotonic_ns=covered_start_mono,
                end_monotonic_ns=restore_action_mono,
                required_cover_identity=cover_identity,
                expected_target_pid=target_pid,
                expected_run_id=str(record.get("run_id")),
                expected_token=str(target_handoff.get("experiment_token", "")))
        else:
            record["covered_sampling_summary"] = {
                "status": "FAIL", "sample_count": 0,
                "reason": "measured covered phase or exact Finder identity is unavailable",
            }
        record["full_cover_transition_bracket"] = (
            transition_bracket_summary(
                all_samples, from_state="CLEAR", to_state="EXPECTED_COVER",
                action_end_mach_ns=cover_action_end,
                expected_target_pid=target_pid,
                expected_run_id=str(record.get("run_id")),
                expected_token=str(target_handoff.get("experiment_token", "")),
                allowed_cover_identity=cover_identity)
            if type(cover_action_end) is int and cover_identity is not None
            else {"status": "FAIL", "reason": "full-cover action/identity is unavailable"})
        record["restoration_transition_bracket"] = (
            transition_bracket_summary(
                all_samples, from_state="EXPECTED_COVER", to_state="CLEAR",
                action_end_mach_ns=restore_action_end,
                expected_target_pid=target_pid,
                expected_run_id=str(record.get("run_id")),
                expected_token=str(target_handoff.get("experiment_token", "")),
                allowed_cover_identity=cover_identity)
            if type(restore_action_end) is int and cover_identity is not None
            else {"status": "FAIL", "reason": "restoration action/identity is unavailable"})
        record["finder_cover_transition_action_bounds_mach_ns"] = {
            "full_cover_request": [cover_action_begin, cover_action_end],
            "restore_request": [restore_action_begin, restore_action_end],
        }
        record["windowserver_collector_summary"] = {
            "pre_handoff_sample_count": len(pre_samples),
            "wine_lifecycle_sample_count": len(all_samples),
            "sample_query_failures": sum(1 for row in pre_samples + all_samples
                                          if row.get("all_window_query_succeeded") is not True
                                          or row.get("onscreen_window_query_succeeded") is not True),
            "maximum_inter_sample_gap_ns": max(all_gaps) if all_gaps else None,
            "pre_handoff_to_lifecycle_boundary_gap_ns": handoff_boundary_gap,
            "maximum_query_duration_ns": max(
                (row.get("query_duration_ns", 0) for row in pre_samples + all_samples
                 if type(row.get("query_duration_ns")) is int), default=None),
            "skipped_deadline_sample_count": sum(1 for row in pre_samples + all_samples
                                                   if (row.get("skipped_deadlines_before_sample") or 0) > 0),
            "max_skipped_deadlines": max(
                (row.get("skipped_deadlines_before_sample", 0) for row in pre_samples + all_samples
                 if type(row.get("skipped_deadlines_before_sample", 0)) is int), default=0),
            "first_prehandoff_query_mach_ns": (pre_samples[0].get("query_started_mach_absolute_ns")
                                                 if pre_samples else None),
            "last_prehandoff_query_mach_ns": (pre_samples[-1].get("query_completed_mach_absolute_ns")
                                                if pre_samples else None),
        }
        bootstrap_telemetry_rows = [row for row in telemetry_rows
                                    if type(accepted_identity_start_mach_ns) is int
                                    and type(row.get("ts_mach_ns")) is int
                                    and row["ts_mach_ns"] < accepted_identity_start_mach_ns]
        measured_telemetry_rows = [row for row in telemetry_rows
                                   if type(accepted_identity_start_mach_ns) is int
                                   and type(row.get("ts_mach_ns")) is int
                                   and row["ts_mach_ns"] >= accepted_identity_start_mach_ns]
        all_telemetry_ids = {telemetry_tuple(row) for row in measured_telemetry_rows}
        telemetry_identity_continuous = bool(
            accepted_identity_tuple is not None and measured_telemetry_rows
            and len(all_telemetry_ids) == 1 and all_telemetry_ids == {accepted_identity_tuple}
            and all(row.get("window_associated") is True for row in measured_telemetry_rows))
        measured_public_identity_rows = [row for row in public_identity_rows
                                         if type(accepted_identity_start_mach_ns) is int
                                         and type(row.get("query_completed_mach_ns")) is int
                                         and row["query_completed_mach_ns"] >= accepted_identity_start_mach_ns]
        public_identity_continuous = bool(
            type(accepted_public_cg_id) is int and measured_public_identity_rows
            and all(row.get("status") == "PASS"
                    and row.get("cg_window_id") == accepted_public_cg_id
                    for row in measured_public_identity_rows))
        combined_identity_continuous = (telemetry_identity_continuous
                                        and public_identity_continuous)
        record["target_telemetry_identity_continuity"] = {
            "telemetry_row_count": len(telemetry_rows),
            "pre_identity_bootstrap_row_count": len(bootstrap_telemetry_rows),
            "measured_identity_row_count": len(measured_telemetry_rows),
            "notification_row_count": len(notification_rows),
            "unique_identity_tuple_count": len(all_telemetry_ids),
            "identity_tuple": list(next(iter(all_telemetry_ids))) if len(all_telemetry_ids) == 1 else None,
            "accepted_identity_tuple": (list(accepted_identity_tuple)
                                         if accepted_identity_tuple is not None else None),
            "accepted_identity_start_mach_ns": accepted_identity_start_mach_ns,
            "accepted_public_cg_window_id": accepted_public_cg_id,
            "telemetry_identity_continuous": telemetry_identity_continuous,
            "public_cg_identity_row_count": len(measured_public_identity_rows),
            "public_cg_identity_continuous": public_identity_continuous,
            "public_cg_identity_samples": measured_public_identity_rows,
            "identity_continuous": combined_identity_continuous,
            "notification_rows": [{"ts_mach_ns": row.get("ts_mach_ns"),
                                    "notification_name": row.get("notification_name"),
                                    "trigger": row.get("trigger"),
                                    "state": telemetry_state(row)} for row in notification_rows],
            "layer_class_rows": [row for row in telemetry_rows if row.get("event") == "layer_configuration"],
        }
        record["identity_monitor_error"] = last_identity_error
        record["runner_result"] = runner_result or record.get("runner_result")
        record["measured_sequence_completed"] = completed_measured_sequence
        record["cleanup_errors"] = cleanup_errors
        record["finder_actions"] = [row for row in read_all_jsonl(events_path)
                                     if row.get("event") == "finder_action"]
        poller_result = next((row for row in reversed(poller_rows)
                              if row.get("event") == "poller_result"), None)
        record["windowserver_collector_terminal_result"] = poller_result
        technical_blockers = []
        if not replay_execution_path_eligible(record):
            technical_blockers.append(
                "controller did not complete normally, or the transient evidence budget guard stopped collection")
        if not completed_measured_sequence:
            technical_blockers.append("measured 20/40/20 sequence did not complete")
        if record.get("runner_returncode") != 0:
            technical_blockers.append("baseline runner process did not return success")
        if final_gate_good is not True:
            technical_blockers.append("post-run-result final Gate 0 snapshot is absent, failed, or violates 55/50 limits")
        runner_binding = record.get("runner_run_result_artifact", {})
        if runner_binding.get("is_valid") is not True or runner_binding.get("status") != "PASS":
            technical_blockers.append("baseline run-result artifact is not PASS/valid")
        if (runner_binding.get("experiment_id") != record.get("run_id")
                or runner_binding.get("phase") != f"baseline-{args.run_index:02d}"
                or runner_binding.get("duration_seconds_requested") != 300):
            technical_blockers.append("baseline run-result phase or identity does not match this Z9 replay")
        if (record.get("smoke_authorization_status") != "PASS"
                or record.get("smoke_authorization_quantitative_ready") is not True):
            technical_blockers.append("authenticated current smoke authorization was not quantitatively ready")
        if poller_result is None or poller_result.get("status") != "PASS":
            technical_blockers.append("WindowServer collector terminal result is not PASS")
        if record["windowserver_collector_summary"].get("sample_query_failures") != 0:
            technical_blockers.append("one or more WindowServer inventory queries failed")
        if record["covered_sampling_summary"].get("status") != "PASS":
            technical_blockers.append("covered phase lacks 350 complete same-query samples at <=100 ms gaps")
        if record["full_cover_transition_bracket"].get("status") != "PASS":
            technical_blockers.append("full-cover transition lacks adjacent successful same-query brackets")
        if record["restoration_transition_bracket"].get("status") != "PASS":
            technical_blockers.append("restoration transition lacks adjacent successful same-query brackets")
        if record["target_telemetry_identity_continuity"].get("identity_continuous") is not True:
            technical_blockers.append("tracked AppKit/public WindowServer identity changed after canonical acceptance")
        if last_identity_error is not None:
            technical_blockers.append("identity monitor recorded a transition")
        if observer.metadata.get("status") != "PASS":
            technical_blockers.append("lifecycle observer did not finish with PASS")
        if not record.get("finder_window_closed_by_this_run"):
            technical_blockers.append("the exact dedicated Finder window was not closed")
        if record.get("finder_folder_removed") is not True:
            technical_blockers.append("the exact Z9-owned Finder directory was not removed")
        if cleanup_errors:
            technical_blockers.append("cleanup reported one or more errors")
        record["technical_validity"] = {
            "status": "PASS" if not technical_blockers else "FAIL",
            "blockers": technical_blockers,
            "runner_run_result_is_valid": runner_binding.get("is_valid"),
            "collector_status": poller_result.get("status") if poller_result else None,
            "identity_continuous": record["target_telemetry_identity_continuity"].get("identity_continuous"),
            "observer_status": observer.metadata.get("status"),
            "measured_sequence_completed": completed_measured_sequence,
        }
        record["technical_valid"] = not technical_blockers
        record["finished_at_utc"] = utc_now()
        record["finished_clock_pair"] = clock_pair()
        atomic_json(controller_path, record)
        append_event({"event": "controller_finished", "stamp": record["finished_clock_pair"],
                      "status": record.get("status"), "cleanup_errors": cleanup_errors})
        event_stream.close()
        runner_log.close()
        poll_log.close()

        transient_limit = admission_evidence["transient_evidence_limit_bytes"]
        transient_paths = [run_directory, observer.directory]
        transient_budget_final: dict[str, Any] = {
            "limit_bytes": transient_limit,
            "measurement_method": "allocated st_blocks, deduplicated by device/inode",
        }
        try:
            transient_before_final_record = allocated_owned_tree_bytes(transient_paths)
            transient_budget_final.update({
                "allocated_bytes_before_final_record": transient_before_final_record,
                "within_limit": transient_before_final_record <= transient_limit,
            })
        except (OSError, RuntimeError) as budget_error:
            transient_budget_final.update({
                "allocated_bytes_before_final_record": None,
                "within_limit": False,
                "measurement_error": f"{type(budget_error).__name__}: {budget_error}",
            })
        record["transient_output_budget_final"] = transient_budget_final
        if transient_budget_final["within_limit"] is not True:
            blocker = "final owned transient evidence allocation is unmeasurable or exceeds its derived limit"
            record["technical_validity"]["blockers"].append(blocker)
            record["technical_validity"]["status"] = "FAIL"
            record["technical_valid"] = False
        atomic_json(controller_path, record)
        try:
            transient_after_final_record = allocated_owned_tree_bytes(transient_paths)
        except (OSError, RuntimeError) as budget_error:
            transient_after_final_record = None
            transient_budget_final["final_measurement_error"] = (
                f"{type(budget_error).__name__}: {budget_error}")
        transient_budget_final["allocated_bytes_after_final_record"] = transient_after_final_record
        transient_budget_final["within_limit"] = (
            type(transient_after_final_record) is int
            and transient_after_final_record <= transient_limit)
        if transient_budget_final["within_limit"] is not True:
            blocker = "owned transient evidence exceeded its derived limit after final controller persistence"
            if blocker not in record["technical_validity"]["blockers"]:
                record["technical_validity"]["blockers"].append(blocker)
            record["technical_validity"]["status"] = "FAIL"
            record["technical_valid"] = False
        atomic_json(controller_path, record)
        try:
            transient_after_receipt = allocated_owned_tree_bytes(transient_paths)
        except (OSError, RuntimeError) as budget_error:
            transient_after_receipt = None
            transient_budget_final["final_verification_error"] = (
                f"{type(budget_error).__name__}: {budget_error}")
        transient_budget_final["allocated_bytes_after_final_verification"] = transient_after_receipt
        if type(transient_after_receipt) is not int or transient_after_receipt > transient_limit:
            blocker = "owned transient evidence failed its final post-write allocation check"
            if blocker not in record["technical_validity"]["blockers"]:
                record["technical_validity"]["blockers"].append(blocker)
            record["technical_validity"]["status"] = "FAIL"
            record["technical_valid"] = False
            transient_budget_final["within_limit"] = False
            atomic_json(controller_path, record)

    print(json.dumps({
        "status": record.get("status"),
        "run_id": record.get("run_id"),
        "target_pid": record.get("target_pid"),
        "finder_pid": current_finder_pid,
        "finder_cg_window_id": finder_cg_id,
        "measured_sequence_completed": completed_measured_sequence,
        "runner_returncode": record.get("runner_returncode"),
        "collector_returncode": record.get("collector_returncode"),
        "transient_output_budget_final": record.get("transient_output_budget_final"),
        "controller": str(controller_path),
    }, indent=2, sort_keys=True))
    return 0 if record.get("technical_valid") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
