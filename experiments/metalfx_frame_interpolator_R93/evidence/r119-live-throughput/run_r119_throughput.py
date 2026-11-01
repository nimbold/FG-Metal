#!/usr/bin/env python3
"""R119 live generation-throughput controller.

Narrow by design. It may only:
  - verify the exact game session, CAMetalLayer, device and the R119 observer;
  - verify that the observer resolved the R119 evidence directory and that the
    corpus guard did not refuse it;
  - confirm the in-world scene and start the bounded continuous run;
  - collect the observer's own generation-service and contention evidence.

It never writes an exact A/B source pair, never sends synthetic gameplay input
and never touches presentation scheduling. The camera rotation is the operator's.

Usage: run_r119_throughput.py [maxTuples] [postRunFrames] [label]
"""

from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
OUT = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r119-live-throughput"
RUNTIME = Path("/tmp/fgmetal-r108-unattended")
START = RUNTIME / "r115-continuous-start"
WORLD = RUNTIME / "r112-world-scene-confirmed"
CONDITION = RUNTIME / "capture-condition"

EXPECTED_DYLIB = "libfgmetal_r119_throughput_probe.dylib"
EXPECTED_EVIDENCE = str(OUT)

ACQUISITION = re.compile(r"R108_SOURCE_FRAME_ACQUIRED pid=(\d+) seq=(\d+)")

CAPTURE_PREFIXES = (
    "R119_",
    "R115_CONTINUOUS_RUN_READY",
    "R115_TUPLE_ARMED",
    "R115_TUPLE_STATE",
    "R115_TUPLE_RESET",
    "R115_GATE_REJECTED",
    "R115_TUPLE_TIMING",
    "R115_CONTINUOUS_RUN=COMPLETE",
    "R115_SEED_MISSING",
    "R115_STALE_CALLBACK",
    "R115_DEADLOCK_REGRESSION",
    "R115_TUPLE_B_DEFERRED",
    "R115_REARM_ELIGIBLE",
    "R110_FAST_PRESENT_ELIGIBILITY=",
    "R114_LATCH=",
    "R110_TITLE_WSI_SEQUENCE=",
    "R116_",
    "R118_",
    "R103_FRONTIER_RECOVERY",
    "R103_SOURCE_FRONTIER=OPEN",
)


def log(line: str, path: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
    print(line, flush=True)


def newest_observer() -> tuple[int, Path]:
    candidates = sorted(RUNTIME.glob("observer.*.log"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit("no observer log found")
    path = candidates[-1]
    return int(path.name.split(".")[1]), path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    max_tuples = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    post_frames = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    label = sys.argv[3] if len(sys.argv) > 3 else f"{max_tuples}tuples"
    run_dir = OUT / f"run-{label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    controller_log = run_dir / "controller.log"
    result_json = run_dir / "controller.json"

    deadline = time.monotonic() + 240
    pid = 0
    observer = Path()
    while time.monotonic() < deadline:
        pid, observer = newest_observer()
        text = read(observer)
        if f"LOADED pid={pid} " in text and "R116_CRITICAL_PATH=ENABLED" in text:
            break
        time.sleep(0.5)
    else:
        raise SystemExit("R119 observer never loaded")

    text = read(observer)
    loaded = next(l for l in text.splitlines() if l.startswith(f"LOADED pid={pid} "))
    layers = [l for l in text.splitlines() if l.startswith(f"TITLE_LAYER_SELECTED pid={pid} ")]
    banner = [l for l in text.splitlines() if l.startswith("R116_CRITICAL_PATH=ENABLED")]
    evidence = [l for l in text.splitlines() if l.startswith("R119_EVIDENCE_DIRECTORY ")]
    refused = [l for l in text.splitlines() if l.startswith("R119_EVIDENCE_DIRECTORY=REFUSED")]
    policy = [l for l in text.splitlines() if l.startswith("R119_DIAGNOSTIC_POLICY")]

    if EXPECTED_DYLIB not in loaded:
        raise SystemExit(f"{EXPECTED_DYLIB} not in the loaded image list")
    if "r114InprocessLatch=1" not in loaded:
        raise SystemExit("latch mode missing from LOADED line")
    if not layers or "deviceName=Apple M3" not in layers[0]:
        raise SystemExit("exact CAMetalLayer/device not verified")
    if not banner:
        raise SystemExit("R116 banner missing")
    # Fail closed: a run whose diagnostics were refused, or whose directory is
    # not this round's, would produce a log with no per-pair evidence. Refuse to
    # start rather than spend an operator session on an empty corpus.
    if refused:
        raise SystemExit("evidence directory was refused by the corpus guard")
    if not evidence:
        raise SystemExit("R119_EVIDENCE_DIRECTORY line missing")
    if f"path={EXPECTED_EVIDENCE}" not in evidence[0]:
        raise SystemExit(f"unexpected evidence directory: {evidence[0]}")
    if "writable=1" not in evidence[0]:
        raise SystemExit(f"evidence directory not writable: {evidence[0]}")

    log(f"R119_PREFLIGHT=READY pid={pid} dylib={EXPECTED_DYLIB} "
        f"layerAndDevice=VERIFIED evidenceDirectory=ISOLATED boundedTuples={max_tuples}",
        controller_log)
    log(layers[0], controller_log)
    log(banner[0], controller_log)
    log(evidence[0], controller_log)
    if policy:
        log(policy[0], controller_log)
    try:
        START.unlink()
    except FileNotFoundError:
        pass
    WORLD.write_text("VISIBLE_WORLD\n", encoding="utf-8")
    CONDITION.write_text("CAMERA\n", encoding="utf-8")
    log(f"R119_WORLD_SCENE=CONFIRMED pid={pid}", controller_log)
    START.write_text("start\n", encoding="utf-8")
    log(f"R119_START=REQUESTED path={START}", controller_log)

    result: dict[str, object] = {
        "schema": "fgmetal-r119-live-generation-throughput-v1",
        "gamePid": pid,
        "observerLog": str(observer),
        "dylib": EXPECTED_DYLIB,
        "evidenceDirectory": evidence[0],
        "diagnosticPolicy": policy[0] if policy else "",
        "manualPhysicalInputOnly": True,
        "syntheticGameplayInputSent": False,
        "externalExactPairSelection": False,
        "boundedTuples": max_tuples,
        "r116Banner": banner[0] if banner else "",
    }

    accepted = 0
    fallback = 0
    services: list[dict[str, float]] = []
    complete_at: float | None = None
    seen_ready = False
    offset = 0
    last_line = time.monotonic()
    post_start_seq = 0
    summary_lines: list[str] = []
    complete_lines: list[str] = []
    run_deadline = time.monotonic() + 900
    timed_out = False

    while True:
        text = read(observer)
        if len(text) > offset:
            chunk = text[offset:]
            offset = len(text)
            last_line = time.monotonic()
            for line in chunk.splitlines():
                if line.startswith("R115_CONTINUOUS_RUN_READY"):
                    if not seen_ready:
                        seen_ready = True
                        log(line, controller_log)
                        log("R119_ACTION=ROTATE_CAMERA_NOW hold a steady physical "
                            "camera rotation for the whole bounded run (touchpad or "
                            "mouse, no synthetic input)", controller_log)
                    continue
                if not line.startswith(CAPTURE_PREFIXES):
                    continue
                log(line, controller_log)
                if line.startswith("R110_FAST_PRESENT_ELIGIBILITY=ACCEPTED"):
                    accepted += 1
                if line.startswith("R110_FAST_PRESENT_ELIGIBILITY=REJECTED"):
                    fallback += 1
                if line.startswith("R118_GENERATION_SERVICE"):
                    services.append(
                        {k: float(v) for k, v in re.findall(r"(\w+)=(-?\d+)", line)}
                    )
                if line.startswith("R116_RUN_SUMMARY"):
                    summary_lines.append(line)
                if line.startswith("R115_CONTINUOUS_RUN=COMPLETE"):
                    complete_lines.append(line)
                    complete_at = time.monotonic()
                    match = re.search(r"postRunStartSequence=(\d+)", line)
                    if match:
                        post_start_seq = int(match.group(1))
        if complete_at is not None and post_start_seq:
            text_now = read(observer)
            last_seq = post_start_seq
            for match in ACQUISITION.finditer(text_now):
                last_seq = int(match.group(2))
            if last_seq - post_start_seq >= post_frames:
                break
        if complete_at is not None and time.monotonic() - complete_at > 120:
            break
        if time.monotonic() - last_line > 45 and complete_at is None:
            log("R119_WATCHDOG=NO_PROGRESS_45S (game may be paused or window unfocused)",
                controller_log)
            last_line = time.monotonic()
        if time.monotonic() > run_deadline:
            timed_out = True
            log("R119_RUN=TIMEOUT reason=no-completion-within-900s "
                f"accepted={accepted} services={len(services)}", controller_log)
            break
        time.sleep(0.25)

    result.update({
        "acceptedEligibility": accepted,
        "rejectedEligibility": fallback,
        "generationServiceRecords": services,
        "runSummary": summary_lines,
        "runComplete": complete_lines,
        "timedOut": timed_out,
        "observerTailBytes": len(read(observer)),
    })
    result_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    log(f"R119_CONTROLLER=DONE accepted={accepted} rejected={fallback} "
        f"services={len(services)} result={result_json}", controller_log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
