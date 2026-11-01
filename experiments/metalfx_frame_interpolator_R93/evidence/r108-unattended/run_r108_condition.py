#!/usr/bin/env python3
"""Run one event-gated capture condition in an already-loaded Elden Ring world."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
RUN_DIR = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/run-01"
RUNTIME_DIR = Path("/tmp/fgmetal-r108-unattended")
CONDITION_FILE = RUNTIME_DIR / "capture-condition"
CAPTURE_TRIGGER = RUNTIME_DIR / "start-title-gpu-capture"
INPUT_LOG = RUN_DIR / "input-events.jsonl"
HELPER = RUNTIME_DIR / "send_bounded_input_to_game"
MIN_POST_WARM_SOURCE_RUNWAY = 20
PAIR_NEIGHBOR_WINDOW_NS = 500_000_000


def stop(reason: str) -> None:
    print(f"R108_CONDITION_RUN=BLOCKED reason={reason}", file=sys.stderr)
    raise SystemExit(2)


def find_observer_log(pid: int) -> Path:
    now_ns = time.monotonic_ns()
    path = RUNTIME_DIR / f"observer.{pid}.log"
    try:
        os.kill(pid, 0)
        stat = path.stat()
        data = path.read_bytes()
    except OSError:
        stop("live-window-owner-has-no-r108-observer-log")
    head = data[:65_536].decode("utf-8", errors="replace")
    tail = data[-262_144:].decode("utf-8", errors="replace")
    if not re.search(rf"TITLE_LAYER_SELECTED pid={pid} ", head + tail):
        stop("r108-observer-log-does-not-bind-to-verified-window-owner")
    acquisitions = re.findall(
        r"R108_SOURCE_FRAME_ACQUIRED pid=(\d+) seq=\d+ acquiredAtMonotonicNs=(\d+)", tail
    )
    if not acquisitions or int(acquisitions[-1][0]) != pid:
        stop("r108-observer-has-no-recent-source-acquisition-for-verified-pid")
    if now_ns - int(acquisitions[-1][1]) > 10_000_000_000 or time.time() - stat.st_mtime > 10:
        stop("r108-observer-source-acquisition-is-stale")
    return path


def helper_run(action: str) -> str:
    return helper_run_for_pid(action, None)


def helper_run_for_pid(action: str, expected_pid: int | None) -> str:
    command = [str(HELPER), action]
    if expected_pid is not None:
        command.append(str(expected_pid))
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            timeout=6,
        )
    except (subprocess.SubprocessError, OSError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        stop(f"bounded-input-helper-{action}-failed:{detail.strip()}")
    return result.stdout.strip()


def next_line(stream, deadline: float, observed: list[str]) -> str:
    while time.monotonic() < deadline:
        line = stream.readline()
        if line:
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            observed.append(decoded)
            return decoded
        time.sleep(0.05)
    stop("timed-out-waiting-for-observer-event")


def drain_available(stream, observed: list[str]) -> None:
    while True:
        line = stream.readline()
        if not line:
            return
        observed.append(line.decode("utf-8", errors="replace").rstrip("\n"))


def parse_acquisitions(lines: list[str]) -> dict[int, int]:
    result: dict[int, int] = {}
    pattern = re.compile(
        r"R108_SOURCE_FRAME_ACQUIRED pid=\d+ seq=(\d+) acquiredAtMonotonicNs=(\d+)"
    )
    for line in lines:
        match = pattern.search(line)
        if match:
            result[int(match.group(1))] = int(match.group(2))
    return result


def parse_producer_commits(lines: list[str]) -> dict[int, dict[str, int | str]]:
    result: dict[int, dict[str, int | str]] = {}
    pattern = re.compile(
        r"R108_SOURCE_PRODUCER_COMMIT sourceSequence=(\d+).*?queue=(0x[0-9a-fA-F]+) "
        r"acquiredAtMonotonicNs=(\d+) originalCommitEntryMonotonicNs=(\d+) "
        r"originalCommitReturnMonotonicNs=(\d+).*?registryID=(\d+)"
    )
    for line in lines:
        match = pattern.search(line)
        if match:
            result[int(match.group(1))] = {
                "producerQueueIdentity": match.group(2),
                "acquiredAtMonotonicNs": int(match.group(3)),
                "originalCommitEntryMonotonicNs": int(match.group(4)),
                "originalCommitReturnMonotonicNs": int(match.group(5)),
                "deviceRegistryID": int(match.group(6)),
            }
    return result


def parse_motion_exports(lines: list[str]) -> tuple[dict[str, str], set[str]]:
    states: dict[str, str] = {}
    estimator_pairs: set[str] = set()
    pattern = re.compile(r"pair=(\d+)->(\d+)")
    for line in lines:
        match = pattern.search(line)
        if not match:
            continue
        pair = f"{match.group(1)}->{match.group(2)}"
        if "R108_GPU_INPUTS_COMPLETE" in line and "motionSource=R108_GPU_ESTIMATOR" in line:
            estimator_pairs.add(pair)
        if "R108_MOTION_DIAGNOSTIC_EXPORT=" not in line:
            continue
        if "=ENQUEUED" in line:
            states.setdefault(pair, "ENQUEUED")
        elif "=SAVED" in line:
            states[pair] = "SAVED"
        elif "=FAILED" in line:
            states[pair] = "FAILED"
    return states, estimator_pairs


def build_pair_candidates(
    start_sequence: int,
    end_sequence: int,
    acquisitions: dict[int, int],
    commits: dict[int, dict[str, int | str]],
    first_event_ns: int,
    last_event_ns: int,
    observed: list[str],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    motion_states, _ = parse_motion_exports(observed)
    estimator_lines: dict[str, str] = {}
    transition_lines: dict[str, str] = {}
    pair_regex = re.compile(r"pair=(\d+)->(\d+)")
    transition_pair_regex = re.compile(
        r"previousSourceSequence=(\d+) currentSourceSequence=(\d+)"
    )
    for line in observed:
        if "R108_GPU_INPUTS_COMPLETE" in line:
            match = pair_regex.search(line)
            if not match:
                continue
            pair = f"{match.group(1)}->{match.group(2)}"
            estimator_lines[pair] = line
        elif "R108_METALFX_TRANSITION" in line:
            match = transition_pair_regex.search(line)
            if not match:
                continue
            pair = f"{match.group(1)}->{match.group(2)}"
            transition_lines[pair] = line

    candidates: list[tuple[int, dict[str, object]]] = []
    nearby_unattributed: list[tuple[int, dict[str, object]]] = []
    for current in range(start_sequence + 1, end_sequence + 1):
        previous = current - 1
        acquired_a = acquisitions.get(previous)
        acquired_b = acquisitions.get(current)
        if acquired_a is None or acquired_b is None:
            continue
        interval_overlaps = acquired_a <= last_event_ns and acquired_b >= first_event_ns
        interval_distance = (
            0 if interval_overlaps else
            first_event_ns - acquired_b if acquired_b < first_event_ns else
            acquired_a - last_event_ns
        )
        commit_a = commits.get(previous)
        commit_b = commits.get(current)
        commit_entry_b = commit_b.get("originalCommitEntryMonotonicNs") if commit_b else None
        commit_distance = (
            commit_entry_b - first_event_ns
            if commit_entry_b is not None and commit_entry_b > first_event_ns
            else 2**63 - 1
        )
        distance = min(interval_distance, commit_distance)
        if distance > PAIR_NEIGHBOR_WINDOW_NS:
            continue
        pair = f"{previous}->{current}"
        same_producer_lineage = bool(
            commit_a and commit_b
            and commit_a["producerQueueIdentity"] == commit_b["producerQueueIdentity"]
            and commit_a["deviceRegistryID"] == commit_b["deviceRegistryID"]
            and commit_a["originalCommitEntryMonotonicNs"]
                <= commit_b["originalCommitEntryMonotonicNs"]
            and commit_a["originalCommitReturnMonotonicNs"]
                <= commit_b["originalCommitReturnMonotonicNs"]
            and commit_a["acquiredAtMonotonicNs"]
                <= commit_a["originalCommitEntryMonotonicNs"]
            and commit_b["acquiredAtMonotonicNs"]
                <= commit_b["originalCommitEntryMonotonicNs"]
        )
        source_b_acquired_after_input = acquired_b > first_event_ns
        post_input_commit = (
            same_producer_lineage
            and
            source_b_acquired_after_input
            and
            commit_entry_b is not None
            and first_event_ns < commit_entry_b <= last_event_ns + PAIR_NEIGHBOR_WINDOW_NS
        )
        details = {
            "pair": pair,
            "previousSourceAcquiredAtMonotonicNs": acquired_a,
            "currentSourceAcquiredAtMonotonicNs": acquired_b,
            "acquisitionIntervalOverlapsInput": interval_overlaps,
            "currentSourceAcquiredAfterInputStart": source_b_acquired_after_input,
            "previousProducerCommit": commit_a,
            "currentProducerCommit": commit_b,
            "sameProducerQueueAndDevice": same_producer_lineage,
            "currentProducerCommitAfterInputStart": post_input_commit,
            "estimatorCommand": estimator_lines.get(pair),
            "motionDiagnostic": motion_states.get(pair, "NOT_OBSERVED"),
            "metalFXState": transition_lines.get(pair),
        }
        if post_input_commit:
            details["attributionStatus"] = "SOURCE_B_ACQUIRED_AND_PRODUCER_COMMITTED_AFTER_INPUT"
            candidates.append((distance, details))
        elif interval_distance <= PAIR_NEIGHBOR_WINDOW_NS:
            details["attributionStatus"] = "NEARBY_UNATTRIBUTED_PRE_INPUT_OR_PROXIMITY_ONLY"
            nearby_unattributed.append((interval_distance, details))
    candidates.sort(key=lambda item: (item[0], item[1]["pair"]))
    nearby_unattributed.sort(key=lambda item: (item[0], item[1]["pair"]))
    return (
        [item[1] for item in candidates[:10]],
        [item[1] for item in nearby_unattributed[:10]],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("condition", choices=["STILL", "CAMERA", "CHARACTER", "COMBINED"])
    parser.add_argument("--attempt", type=int, choices=[1, 2], required=True)
    parser.add_argument(
        "--action",
        choices=["camera-left", "camera-right", "character-forward", "combined-right-forward"],
        help="Optional bounded movement variant; STILL never accepts an input action.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=30.0)
    args = parser.parse_args()

    expected_actions = {
        "STILL": {None},
        "CAMERA": {"camera-left", "camera-right"},
        "CHARACTER": {"character-forward"},
        "COMBINED": {"combined-right-forward"},
    }
    if args.action not in expected_actions[args.condition]:
        stop("input-action-does-not-match-condition")
    if not HELPER.is_file():
        stop("input-helper-not-built")

    helper_ready = helper_run("check")
    helper_pid = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", helper_ready)
    if not helper_pid:
        stop("safe-target-check-returned-no-pid")
    log_pid = int(helper_pid.group(1))
    observer_log = find_observer_log(log_pid)

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    if CAPTURE_TRIGGER.exists():
        CAPTURE_TRIGGER.unlink()
    CONDITION_FILE.write_text(args.condition + "\n", encoding="utf-8")
    observed: list[str] = []
    with observer_log.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        start_offset = stream.tell()
        CAPTURE_TRIGGER.write_text("R108 one-shot bounded capture\n", encoding="utf-8")
        deadline = time.monotonic() + args.timeout_seconds

        armed = None
        while armed is None:
            line = next_line(stream, deadline, observed)
            match = re.search(
                r"R108_TITLE_CAPTURE=ARMED window=(\d+) .*startSequence=(\d+) expectedEndSequence=(\d+)",
                line,
            )
            if match:
                armed = {
                    "line": line,
                    "window": int(match.group(1)),
                    "startSequence": int(match.group(2)),
                    "endSequence": int(match.group(3)),
                }

        warm = None
        warm_pair = None
        while warm is None:
            line = next_line(stream, deadline, observed)
            if "R108_PROVIDER_WARM_STATE=READY" not in line or f"condition={args.condition} " not in line:
                continue
            pair_match = re.search(r"completedEligiblePair=(\d+)->(\d+) ", line)
            next_match = re.search(r"nextExpectedEligiblePair=(\d+)->(\d+) ", line)
            if not pair_match or not next_match:
                continue
            pair = (int(pair_match.group(1)), int(pair_match.group(2)))
            next_pair = (int(next_match.group(1)), int(next_match.group(2)))
            if not (armed["startSequence"] <= pair[0] < pair[1] <= armed["endSequence"]):
                continue
            if pair[1] != next_pair[0] or next_pair[1] != next_pair[0] + 1:
                continue
            if next_pair[1] > armed["endSequence"]:
                continue
            warm = line
            warm_pair = {"previous": pair[0], "current": pair[1]}

        warm_timestamp = re.search(r"eventTimestampMonotonicNs=(\d+)", warm)
        if args.action:
            refreshed_check = helper_run_for_pid("check", log_pid)
            refreshed_pid = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", refreshed_check)
            if not refreshed_pid or int(refreshed_pid.group(1)) != log_pid:
                stop("verified-window-owner-changed-before-input")
        drain_available(stream, observed)
        acquisition_snapshot = parse_acquisitions(observed)
        acquisition_frontier = max(acquisition_snapshot, default=armed["startSequence"] - 1)
        runway = armed["endSequence"] - acquisition_frontier
        input_result = "R108_INPUT_TO_GAME=NONE action=STILL"
        input_not_sent_reason = None
        event_match = None
        if args.action:
            if runway < MIN_POST_WARM_SOURCE_RUNWAY:
                input_not_sent_reason = "insufficient-source-runway-after-provider-warm"
                input_result = "R108_INPUT_TO_GAME=NOT_SENT reason=insufficient-source-runway"
            else:
                input_result = helper_run_for_pid(args.action, log_pid)
                action_pid = re.search(r"(?:^|\s)pid=(\d+)(?:\s|$)", input_result)
                if not action_pid or int(action_pid.group(1)) != log_pid:
                    stop("input-target-does-not-match-armed-observer-process")
                event_match = re.search(
                    r"startMonotonicNs=(\d+) firstEventMonotonicNs=(\d+) "
                    r"lastEventMonotonicNs=(\d+) endMonotonicNs=(\d+)",
                    input_result,
                )
                if not event_match:
                    stop("input-helper-returned-no-monotonic-event-timestamps")
                if warm_timestamp and int(event_match.group(1)) <= int(warm_timestamp.group(1)):
                    stop("input-event-did-not-follow-provider-warm-event")

        processed = None
        while processed is None:
            line = next_line(stream, deadline, observed)
            match = re.search(
                r"R108_TITLE_CAPTURE=PROCESSED_WINDOW_COMPLETE window=(\d+) endSequence=(\d+)",
                line,
            )
            if match and int(match.group(1)) == armed["window"]:
                processed = {"line": line, "endSequence": int(match.group(2))}

        motion_states, expected_motion_pairs = parse_motion_exports(observed)
        while expected_motion_pairs and not all(
            motion_states.get(pair) in {"SAVED", "FAILED"} for pair in expected_motion_pairs
        ):
            next_line(stream, deadline, observed)
            motion_states, expected_motion_pairs = parse_motion_exports(observed)

    acquisitions = parse_acquisitions(observed)
    commits = parse_producer_commits(observed)
    if event_match:
        first_event_ns = int(event_match.group(2))
        last_event_ns = int(event_match.group(3))
        candidates, nearby_unattributed = build_pair_candidates(
            armed["startSequence"], processed["endSequence"], acquisitions, commits,
            first_event_ns, last_event_ns, observed,
        )
    else:
        candidates, nearby_unattributed = [], []
    motion_states, expected_motion_pairs = parse_motion_exports(observed)
    diagnostics_terminal = bool(expected_motion_pairs) and all(
        motion_states.get(pair) in {"SAVED", "FAILED"} for pair in expected_motion_pairs
    )
    diagnostics_complete = bool(expected_motion_pairs) and all(
        motion_states.get(pair) == "SAVED" for pair in expected_motion_pairs
    )
    record = {
        "condition": args.condition,
        "attempt": args.attempt,
        "action": args.action or "none",
        "observerPid": log_pid,
        "observerLog": str(observer_log),
        "captureWindow": armed["window"],
        "captureStartSequence": armed["startSequence"],
        "captureEndSequence": processed["endSequence"],
        "captureArmedLine": armed["line"],
        "providerWarmLine": warm,
        "providerWarmMonotonicNs": int(warm_timestamp.group(1)) if warm_timestamp else None,
        "providerWarmPair": warm_pair,
        "inputResult": input_result,
        "inputNotSentReason": input_not_sent_reason,
        "inputStartMonotonicNs": int(event_match.group(1)) if event_match else None,
        "inputFirstEventMonotonicNs": int(event_match.group(2)) if event_match else None,
        "inputLastEventMonotonicNs": int(event_match.group(3)) if event_match else None,
        "inputEndMonotonicNs": int(event_match.group(4)) if event_match else None,
        "sourceAcquisitionFrontierBeforeInput": acquisition_frontier,
        "remainingSourceRunwayBeforeInput": runway,
        "pairsBoundToInputEvent": candidates,
        "nearbyUnattributedExactPairs": nearby_unattributed,
        "inputPairBound": bool(candidates) if event_match else None,
        "stillControlPair": warm_pair if not args.action else None,
        "motionEstimatorPairs": sorted(expected_motion_pairs),
        "motionDiagnosticsSaved": sorted(pair for pair in expected_motion_pairs if motion_states.get(pair) == "SAVED"),
        "motionDiagnosticsFailed": sorted(pair for pair in expected_motion_pairs if motion_states.get(pair) == "FAILED"),
        "motionDiagnosticsTerminal": diagnostics_terminal,
        "motionDiagnosticsComplete": diagnostics_complete,
        "processedWindowLine": processed["line"],
        "logStartOffset": start_offset,
    }
    with INPUT_LOG.open("a", encoding="utf-8") as output:
        output.write(json.dumps(record, sort_keys=True) + "\n")
    if input_not_sent_reason:
        status = "CAPTURED_NO_INPUT"
    elif args.action and not candidates:
        status = "CAPTURED_UNBOUND"
    else:
        status = "CAPTURED"
    print(f"R108_CONDITION_RUN={status} " + json.dumps(record, sort_keys=True))


if __name__ == "__main__":
    main()
