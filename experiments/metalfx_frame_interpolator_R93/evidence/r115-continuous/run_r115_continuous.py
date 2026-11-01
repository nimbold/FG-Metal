#!/usr/bin/env python3
"""R115 continuous re-arm controller.

The controller may only:
  - verify the exact game session / layer / device and the R115 observer mode;
  - confirm the in-world scene and start the bounded continuous run;
  - observe (never write) pair selection, which belongs to the observer process;
  - collect the observer's own lifecycle / ordering / latency evidence.

It never writes an exact A/B source pair and never sends synthetic gameplay input.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time


ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
OUT = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r115-continuous"
RUNTIME = Path("/tmp/fgmetal-r108-unattended")
START = RUNTIME / "r115-continuous-start"
WORLD = RUNTIME / "r112-world-scene-confirmed"
CONDITION = RUNTIME / "capture-condition"
RESULT_JSON = OUT / "r115-controller.json"
CONTROLLER_LOG = OUT / "r115-controller.log"

ACQUISITION = re.compile(r"R108_SOURCE_FRAME_ACQUIRED pid=(\d+) seq=(\d+) acquiredAtMonotonicNs=(\d+)")


def log(line: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with CONTROLLER_LOG.open("a", encoding="utf-8") as stream:
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
    max_tuples = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    post_frames = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    deadline = time.monotonic() + 120
    pid = 0
    observer = Path()
    while time.monotonic() < deadline:
        pid, observer = newest_observer()
        text = read(observer)
        if f"LOADED pid={pid} " in text and "R115_CONTINUOUS=ENABLED" in text:
            break
        time.sleep(0.5)
    else:
        raise SystemExit("R115 observer never loaded")

    text = read(observer)
    loaded = next(l for l in text.splitlines() if l.startswith(f"LOADED pid={pid} "))
    layers = [l for l in text.splitlines() if l.startswith(f"TITLE_LAYER_SELECTED pid={pid} ")]
    if "r114InprocessLatch=1" not in loaded:
        raise SystemExit("latch mode missing from LOADED line")
    if not layers or "deviceName=Apple M3" not in layers[0] or "registryID=4294968554" not in layers[0]:
        raise SystemExit("exact CAMetalLayer/device not verified")

    log(f"R115_PREFLIGHT=READY pid={pid} layerAndDevice=VERIFIED observer={observer} boundedTuples={max_tuples}")
    for path in (START,):
        try:
            path.unlink()
        except FileNotFoundError:
            pass
    WORLD.write_text("VISIBLE_WORLD\n", encoding="utf-8")
    CONDITION.write_text("CAMERA\n", encoding="utf-8")
    log(f"R115_WORLD_SCENE=CONFIRMED pid={pid} path={WORLD}")
    START.write_text("start\n", encoding="utf-8")
    log(f"R115_START=REQUESTED path={START}")

    result: dict[str, object] = {
        "schema": "fgmetal-r115-rearmable-continuous-v1",
        "gamePid": pid,
        "observerLog": str(observer),
        "manualPhysicalInputOnly": True,
        "syntheticGameplayInputSent": False,
        "externalExactPairSelection": False,
        "boundedTuples": max_tuples,
    }

    accepted = 0
    fallback = 0
    timings: list[dict[str, float]] = []
    complete_at: float | None = None
    seen_ready = False
    offset = 0
    last_line = time.monotonic()
    post_start_seq = 0
    post_start_ns = 0
    verdict_lines: list[str] = []

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
                        log(line)
                        log("R115_ACTION=ROTATE_CAMERA_NOW hold a steady physical camera "
                            "rotation for several seconds (touchpad/mouse, no synthetic input)")
                elif line.startswith("R115_TUPLE_ARMED") or line.startswith("R115_TUPLE_STATE") \
                        or line.startswith("R115_TUPLE_RESET") or line.startswith("R115_GATE_REJECTED") \
                        or line.startswith("R115_TUPLE_TIMING") or line.startswith("R115_CONTINUOUS_RUN=COMPLETE") \
                        or line.startswith("R115_SEED_MISSING") or line.startswith("R115_STALE_CALLBACK") \
                        or line.startswith("R115_DEADLOCK_REGRESSION") or line.startswith("R115_TUPLE_B_DEFERRED") \
                        or line.startswith("R115_REARM_ELIGIBLE") or line.startswith("R110_FAST_PRESENT_ELIGIBILITY=") \
                        or line.startswith("R114_LATCH=") or line.startswith("R110_TITLE_WSI_SEQUENCE="):
                    log(line)
                    if line.startswith("R110_FAST_PRESENT_ELIGIBILITY=ACCEPTED"):
                        accepted += 1
                    if line.startswith("R110_FAST_PRESENT_ELIGIBILITY=REJECTED"):
                        fallback += 1
                    if line.startswith("R110_TITLE_WSI_SEQUENCE="):
                        verdict_lines.append(line)
                    if line.startswith("R115_TUPLE_TIMING"):
                        parsed: dict[str, float] = {}
                        for key, value in re.findall(r"(\w+)=(-?\d+)", line):
                            parsed[key] = float(value)
                        timings.append(parsed)
                    if line.startswith("R115_CONTINUOUS_RUN=COMPLETE"):
                        complete_at = time.monotonic()
                        match = re.search(r"postRunStartSequence=(\d+)", line)
                        if match:
                            post_start_seq = int(match.group(1))
        if complete_at is not None and post_start_seq:
            text_now = read(observer)
            tail = text_now[offset:]
            last_seq = post_start_seq
            for match in ACQUISITION.finditer(text_now):
                last_seq = int(match.group(2))
            if last_seq - post_start_seq >= post_frames:
                post_start_ns = 0
                break
        if complete_at is not None and time.monotonic() - complete_at > 90:
            break
        if time.monotonic() - last_line > 45 and complete_at is None:
            log("R115_WATCHDOG=NO_PROGRESS_45S (game may be paused or window unfocused)")
            last_line = time.monotonic()
        time.sleep(0.25)

    text = read(observer)
    result.update({
        "acceptedEligibility": accepted,
        "rejectedEligibility": fallback,
        "tupleTimings": timings,
        "wsiSequenceVerdicts": verdict_lines,
        "observerTailBytes": len(text),
    })
    if timings:
        holds = [t.get("bHoldNs", 0.0) for t in timings if t.get("bHoldNs")]
        if holds:
            result["bHoldMs"] = {
                "min": min(holds) / 1e6,
                "max": max(holds) / 1e6,
                "mean": sum(holds) / len(holds) / 1e6,
            }
    RESULT_JSON.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    log(f"R115_CONTROLLER=DONE accepted={accepted} rejected={fallback} timings={len(timings)} "
        f"result={RESULT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
