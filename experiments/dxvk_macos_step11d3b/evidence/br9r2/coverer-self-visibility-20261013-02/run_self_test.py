#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import hashlib
import json
import pathlib
import select
import subprocess
import sys
import time
from typing import Any


ROOT = pathlib.Path(__file__).resolve().parent
EXPECTED_SHA256 = "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc"
COVERER = pathlib.Path(
    "/Users/nima/Library/Caches/FGMetal/artifacts/sha256/"
    "1f6e6ade1d852825084b317031eae3cb3edb2de19b77f4c1e5a7ca85aad48fbc/neutral-coverer"
)


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds")


def write_json(path: pathlib.Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_json_line(process: subprocess.Popen[str], timeout: float = 10) -> dict[str, Any]:
    if process.stdout is None:
        raise RuntimeError("child stdout is unavailable")
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError(f"PID {process.pid} did not return a JSON response within {timeout}s")
    line = process.stdout.readline()
    if not line:
        raise RuntimeError(f"PID {process.pid} exited before replying ({process.poll()})")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("child returned a non-object response")
    return value


def send_json(process: subprocess.Popen[str], value: dict[str, Any]) -> dict[str, Any]:
    if process.stdin is None or process.poll() is not None:
        raise RuntimeError(f"PID {process.pid} exited before command {value.get('command')}")
    process.stdin.write(json.dumps(value, sort_keys=True) + "\n")
    process.stdin.flush()
    return read_json_line(process)


def run_poller(poller: pathlib.Path, target: dict[str, Any], coverer: dict[str, Any],
               phase: str, duration_ms: int, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sample_path = ROOT / f"{phase}.jsonl"
    process = subprocess.Popen(
        [str(poller), str(target["pid"]), str(target["windowNumber"]),
         str(coverer["pid"]), str(coverer["windowNumber"]),
         str(duration_ms), str(sample_path)],
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
    )
    started = time.monotonic_ns()
    events.append({"event": "poller_started", "phase": phase, "monotonic_ns": started,
                   "duration_ms": duration_ms, "sample_path": str(sample_path)})
    time.sleep(0.12)
    returncode = process.wait(timeout=(duration_ms / 1000) + 15)
    stderr = process.stderr.read() if process.stderr else ""
    events.append({"event": "poller_finished", "phase": phase,
                   "monotonic_ns": time.monotonic_ns(), "returncode": returncode,
                   "stderr": stderr[-2000:]})
    if returncode != 0:
        raise RuntimeError(f"public CGWindow poller failed for {phase}: {stderr[-2000:]}")
    return [json.loads(line) for line in sample_path.read_text(encoding="utf-8").splitlines() if line]


def qualifying(sample: dict[str, Any]) -> bool:
    coverer = sample.get("coverer")
    target = sample.get("target")
    return bool(
        sample.get("querySucceeded") is True
        and sample.get("covererOnScreen") is True
        and sample.get("targetOnScreen") is True
        and sample.get("covererInFrontOfTarget") is True
        and sample.get("targetAreaCoverageFraction", 0) >= 0.999999
        and sample.get("covererLayerAboveTarget") is True
        and isinstance(coverer, dict) and coverer.get("alpha") == 1
        and isinstance(target, dict) and target.get("alpha") == 1
    )


def attainment_summary(samples: list[dict[str, Any]], show_ns: int) -> dict[str, Any]:
    on_screen = [sample for sample in samples
                 if sample.get("sampledAtMonotonicNS", 0) >= show_ns
                 and sample.get("covererOnScreen") is True]
    valid = [sample for sample in on_screen if qualifying(sample)]
    longest_ns = 0
    segment_start = previous = None
    for sample in samples:
        stamp = sample.get("sampledAtMonotonicNS")
        if not isinstance(stamp, int) or stamp < show_ns or not qualifying(sample):
            if segment_start is not None and previous is not None:
                longest_ns = max(longest_ns, previous - segment_start)
            segment_start = previous = None
            continue
        if previous is None or stamp - previous > 250_000_000:
            if segment_start is not None and previous is not None:
                longest_ns = max(longest_ns, previous - segment_start)
            segment_start = stamp
        previous = stamp
    if segment_start is not None and previous is not None:
        longest_ns = max(longest_ns, previous - segment_start)
    first = on_screen[0] if on_screen else None
    intervals = [b["sampledAtMonotonicNS"] - a["sampledAtMonotonicNS"]
                 for a, b in zip(samples, samples[1:])
                 if isinstance(a.get("sampledAtMonotonicNS"), int)
                 and isinstance(b.get("sampledAtMonotonicNS"), int)]
    return {
        "sample_count": len(samples),
        "on_screen_sample_count_after_show": len(on_screen),
        "fully_covered_front_sample_count_after_show": len(valid),
        "first_on_screen_monotonic_ns": first.get("sampledAtMonotonicNS") if first else None,
        "first_on_screen_delay_ms": ((first["sampledAtMonotonicNS"] - show_ns) / 1_000_000
                                     if first else None),
        "longest_fully_covered_front_streak_ms": longest_ns / 1_000_000,
        "stable_for_at_least_2s": longest_ns >= 2_000_000_000,
        "max_sample_gap_ms": max(intervals, default=0) / 1_000_000,
        "last_coverer_on_screen_sample": on_screen[-1] if on_screen else None,
        "last_sample": samples[-1] if samples else None,
    }


def inferred_appkit_frame(cg: dict[str, Any], screens: list[dict[str, Any]]) -> dict[str, float] | None:
    mid_x = cg["x"] + cg["width"] / 2
    mid_y = cg["y"] + cg["height"] / 2
    for screen in screens:
        display = screen.get("boundsCG")
        frame = screen.get("frameAppKit")
        if not isinstance(display, dict) or not isinstance(frame, dict):
            continue
        if not (display["x"] <= mid_x <= display["x"] + display["width"]
                and display["y"] <= mid_y <= display["y"] + display["height"]):
            continue
        scale_x = frame["width"] / display["width"]
        scale_y = frame["height"] / display["height"]
        return {
            "x": frame["x"] + (cg["x"] - display["x"]) * scale_x,
            "y": frame["y"] + (display["y"] + display["height"] - cg["y"] - cg["height"]) * scale_y,
            "width": cg["width"] * scale_x,
            "height": cg["height"] * scale_y,
        }
    return None


def main() -> int:
    target_binary = ROOT / "diagnostic-target"
    poller_binary = ROOT / "public-cg-poller"
    if sha256(COVERER) != EXPECTED_SHA256:
        raise RuntimeError("refusing diagnostic: frozen coverer SHA-256 does not match the accepted artifact")
    events: list[dict[str, Any]] = []
    summary: dict[str, Any] = {
        "started_at_utc": utc_now(),
        "frozen_coverer_path": str(COVERER),
        "frozen_coverer_sha256_before": sha256(COVERER),
        "diagnostic_target_source_sha256": sha256(ROOT / "diagnostic_target.swift"),
        "public_cg_poller_source_sha256": sha256(ROOT / "public_cg_poller.swift"),
        "phases": [],
    }
    title = "FGMetal Frozen Coverer Self-Test 20261013-01"
    target_stderr = (ROOT / "diagnostic-target.stderr.log").open("w", encoding="utf-8")
    target = subprocess.Popen([str(target_binary), title], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, stderr=target_stderr,
                              text=True, bufsize=1)
    coverer_stderr = None
    coverer = None
    try:
        target_ready = read_json_line(target, 20)
        events.append({"event": "target_ready", "monotonic_ns": time.monotonic_ns(),
                       "response": target_ready})
        if target_ready.get("event") != "ready" or target_ready.get("status") not in (None, "PASS"):
            raise RuntimeError(f"diagnostic target did not become ready: {target_ready}")

        coverer_stderr = (ROOT / "frozen-coverer.stderr.log").open("w", encoding="utf-8")
        coverer = subprocess.Popen([str(COVERER)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=coverer_stderr,
                                   text=True, bufsize=1, close_fds=True)
        ready = read_json_line(coverer, 20)
        events.append({"event": "coverer_ready", "monotonic_ns": time.monotonic_ns(),
                       "response": ready})
        if ready.get("event") != "ready" or ready.get("status") != "PASS":
            raise RuntimeError(f"frozen coverer did not become ready: {ready}")
        coverer_identity = {"pid": ready.get("pid")}

        bind = send_json(coverer, {"command": "target", "pid": target_ready["pid"], "title": title})
        events.append({"event": "target_bound_simple", "monotonic_ns": time.monotonic_ns(),
                       "response": bind})
        if bind.get("status") != "PASS" or bind.get("target_window_number") is None:
            raise RuntimeError(f"coverer could not bind the simple diagnostic target: {bind}")
        coverer_identity["windowNumber"] = bind.get("coverer_window_number")
        target_identity = {"pid": target_ready["pid"], "windowNumber": bind["target_window_number"]}

        def show_phase(name: str, duration_ms: int = 3500) -> dict[str, Any]:
            poller = subprocess.Popen(
                [str(poller_binary), str(target_identity["pid"]), str(target_identity["windowNumber"]),
                 str(coverer_identity["pid"]), str(coverer_identity["windowNumber"]),
                 str(duration_ms), str(ROOT / f"{name}.jsonl")],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            poll_started = time.monotonic_ns()
            events.append({"event": "poller_started", "phase": name,
                           "monotonic_ns": poll_started, "duration_ms": duration_ms})
            time.sleep(0.12)
            show_ns = time.monotonic_ns()
            response = send_json(coverer, {"command": "show"})
            events.append({"event": "show_response", "phase": name,
                           "monotonic_ns": show_ns, "response": response})
            returncode = poller.wait(timeout=(duration_ms / 1000) + 15)
            stderr = poller.stderr.read() if poller.stderr else ""
            if returncode != 0:
                raise RuntimeError(f"public CGWindow poller failed during {name}: {stderr[-2000:]}")
            samples_path = ROOT / f"{name}.jsonl"
            samples = [json.loads(line) for line in samples_path.read_text(encoding="utf-8").splitlines() if line]
            status = send_json(coverer, {"command": "status"})
            events.append({"event": "post_wait_status", "phase": name,
                           "monotonic_ns": time.monotonic_ns(), "response": status})
            outcome = {
                "phase": name,
                "show_command_accepted_status": response.get("status"),
                "immediate_show_coverer_is_visible": response.get("coverer_is_visible"),
                "immediate_show_coverer_cg_bounds": response.get("coverer_bounds_cg"),
                "immediate_show_front_of_target": response.get("front_of_target"),
                "immediate_show_target_area_coverage_fraction": response.get("target_area_coverage_fraction"),
                "post_wait_status": status,
                "external_public_cg": attainment_summary(samples, show_ns),
                "sample_path": str(samples_path),
                "poller_process_stderr": stderr[-2000:],
            }
            summary["phases"].append(outcome)
            events.append({"event": "phase_complete", "phase": name,
                           "monotonic_ns": time.monotonic_ns(),
                           "summary": {key: value for key, value in outcome.items()
                                       if key not in ("post_wait_status", "external_public_cg")}})
            return outcome

        simple = show_phase("simple-show-1")
        simple_attained = simple["external_public_cg"]["on_screen_sample_count_after_show"] > 0
        summary["self_visibility_classification"] = "C1" if simple_attained else "C2"

        def hide_phase(name: str, duration_ms: int = 1000) -> dict[str, Any]:
            poller_path = ROOT / f"{name}.jsonl"
            poller = subprocess.Popen(
                [str(poller_binary), str(target_identity["pid"]), str(target_identity["windowNumber"]),
                 str(coverer_identity["pid"]), str(coverer_identity["windowNumber"]),
                 str(duration_ms), str(poller_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
            )
            poll_started = time.monotonic_ns()
            events.append({"event": "poller_started", "phase": name,
                           "monotonic_ns": poll_started, "duration_ms": duration_ms})
            time.sleep(0.12)
            hidden_ns = time.monotonic_ns()
            response = send_json(coverer, {"command": "hide"})
            events.append({"event": "hide_response", "phase": name,
                           "monotonic_ns": hidden_ns, "response": response})
            returncode = poller.wait(timeout=(duration_ms / 1000) + 15)
            stderr = poller.stderr.read() if poller.stderr else ""
            if returncode != 0:
                raise RuntimeError(f"public CGWindow poller failed during {name}: {stderr[-2000:]}")
            samples = [json.loads(line) for line in poller_path.read_text(encoding="utf-8").splitlines() if line]
            tail = samples[-max(1, len(samples) // 4):]
            outcome = {
                "phase": name,
                "hide_command_status": response.get("status"),
                "immediate_hide_coverer_is_visible": response.get("coverer_is_visible"),
                "immediate_hide_coverer_cg_bounds": response.get("coverer_bounds_cg"),
                "external_on_screen_count": sum(sample.get("covererOnScreen") is True for sample in samples),
                "external_tail_on_screen_count": sum(sample.get("covererOnScreen") is True for sample in tail),
                "external_tail_sample_count": len(tail),
                "sample_path": str(poller_path),
                "poller_process_stderr": stderr[-2000:],
            }
            summary["phases"].append(outcome)
            events.append({"event": "phase_complete", "phase": name,
                           "monotonic_ns": time.monotonic_ns(), "summary": outcome})
            return outcome

        simple_hide = hide_phase("simple-hide-1")
        if summary["self_visibility_classification"] == "C1":
            geometry = send_json(target, {"command": "wine_geometry"})
            events.append({"event": "target_set_wine_equivalent_geometry",
                           "monotonic_ns": time.monotonic_ns(), "response": geometry})
            if geometry.get("status") != "PASS":
                raise RuntimeError(f"controlled Wine-equivalent target geometry failed: {geometry}")
            settled_samples = run_poller(poller_binary, target_identity, coverer_identity,
                                         "wine-geometry-public-settle", 500, events)
            expected_bounds = geometry.get("targetBoundsCGRequested")
            public_bounds = [sample.get("target", {}).get("bounds")
                             for sample in settled_samples
                             if sample.get("targetOnScreen") is True and isinstance(sample.get("target"), dict)]
            if (not isinstance(expected_bounds, dict) or not public_bounds
                    or any(abs(public_bounds[-1][key] - expected_bounds[key]) > 0.5
                           for key in ("x", "y", "width", "height"))):
                raise RuntimeError("controlled target geometry did not settle to the retained Wine CG bounds")
            events.append({"event": "target_geometry_publicly_attained",
                           "monotonic_ns": time.monotonic_ns(),
                           "expected_cg_bounds": expected_bounds,
                           "observed_cg_bounds": public_bounds[-1],
                           "sample_count": len(settled_samples)})
            bind = send_json(coverer, {"command": "target", "pid": target_ready["pid"], "title": title})
            events.append({"event": "target_rebound_wine_geometry",
                           "monotonic_ns": time.monotonic_ns(), "response": bind})
            if bind.get("status") != "PASS" or bind.get("target_window_number") != target_identity["windowNumber"]:
                raise RuntimeError(f"coverer could not retain the exact diagnostic target identity: {bind}")
            actual_geometry_first = show_phase("wine-geometry-show-1")
            hide_phase("wine-geometry-hide-1")
            actual_geometry_second = show_phase("wine-geometry-show-2")
            hide_phase("wine-geometry-hide-2")
            summary["target_wine_equivalent_geometry"] = geometry
            summary["geometry_round_trip"] = {
                "input_target_cg_bounds": bind.get("target_bounds_cg"),
                "controller_requested_appkit_frame_inferred_by_source": inferred_appkit_frame(
                    expected_bounds, target_ready.get("screens", [])),
                "target_appkit_window_frame": geometry.get("windowFrameAppKit"),
                "coverer_cg_bounds_first": actual_geometry_first["post_wait_status"].get("coverer_bounds_cg"),
                "coverer_cg_bounds_second": actual_geometry_second["post_wait_status"].get("coverer_bounds_cg"),
                "coverer_cg_layer_first": actual_geometry_first["post_wait_status"].get("coverer_cg_layer"),
                "coverer_front_rank_first": actual_geometry_first["post_wait_status"].get("coverer_front_to_back_rank"),
                "target_front_rank_first": actual_geometry_first["post_wait_status"].get("target_front_to_back_rank"),
            }

        summary["frozen_coverer_sha256_after"] = sha256(COVERER)
        summary["finished_at_utc"] = utc_now()
        write_json(ROOT / "summary.json", summary)
        with (ROOT / "events.jsonl").open("w", encoding="utf-8") as stream:
            for event in events:
                stream.write(json.dumps(event, sort_keys=True) + "\n")
        return 0
    finally:
        if coverer is not None and coverer.poll() is None:
            try:
                response = send_json(coverer, {"command": "quit"})
                events.append({"event": "coverer_quit", "monotonic_ns": time.monotonic_ns(),
                               "response": response})
                coverer.wait(timeout=5)
            except Exception:
                coverer.terminate()
                try:
                    coverer.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    coverer.kill()
        if target.poll() is None:
            try:
                send_json(target, {"command": "quit"})
                target.wait(timeout=5)
            except Exception:
                target.terminate()
                try:
                    target.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    target.kill()
        target_stderr.close()
        if coverer_stderr is not None:
            coverer_stderr.close()


if __name__ == "__main__":
    try:
        result = main()
    except Exception as exc:
        error = {"status": "ERROR", "error": f"{type(exc).__name__}: {exc}",
                 "finished_at_utc": utc_now()}
        write_json(ROOT / "summary.json", error)
        with (ROOT / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": "fatal", "monotonic_ns": time.monotonic_ns(),
                                     "error": error["error"]}, sort_keys=True) + "\n")
        print(error["error"], file=sys.stderr)
        sys.exit(1)
    sys.exit(result)
