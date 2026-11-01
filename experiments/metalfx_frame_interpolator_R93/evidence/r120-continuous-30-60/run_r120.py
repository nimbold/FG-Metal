#!/usr/bin/env python3
"""R120 Phase 1 live controller: establish a genuine ~30 fps SOURCE cadence.

Narrow by design. It may only:
  - verify the exact game session, CAMetalLayer, device and the R120 observer;
  - verify that the observer resolved the R120 evidence directory and that the
    corpus guard did not refuse it;
  - wait while the operator drives, then cut a time window out of the observer
    log and hand it to the analyser.

It never sends synthetic gameplay input, never selects a frame pair and never
touches presentation scheduling. The camera is the operator's.

The window, not the whole log, is what gets analysed: the pacer is active from
the first present after load, which includes menus and loading screens. Cutting
a window after the operator says "in world and rotating" keeps the cadence claim
about gameplay rather than about a title screen.

Usage: run_r120.py [seconds] [label]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
OUT = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r120-continuous-30-60"
RUNTIME = Path("/tmp/fgmetal-r108-unattended")
WORLD = RUNTIME / "r112-world-scene-confirmed"
CONDITION = RUNTIME / "capture-condition"
# Stale control files from earlier rounds. A leftover tuple target would arm the
# R110 deferral path and take presents out of the paced cadence, which would
# silently invalidate the measurement rather than fail it. Removed, not assumed.
STALE = (
    RUNTIME / "r112-target-source-b",
    RUNTIME / "r110-target-source-b",
    RUNTIME / "r115-continuous-start",
    RUNTIME / "r112-world-scene-confirmed",
    RUNTIME / "capture-condition",
)

EXPECTED_DYLIB = "libfgmetal_r120_pace_probe.dylib"
EXPECTED_EVIDENCE = str(OUT)


def log(line: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
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
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    label = sys.argv[2] if len(sys.argv) > 2 else "01-pace"
    run_dir = OUT / f"run-{label}"
    run_dir.mkdir(parents=True, exist_ok=True)
    controller_log = run_dir / "controller.log"
    result_json = run_dir / "controller.json"

    deadline = time.monotonic() + 300
    pid = 0
    observer = Path()
    while time.monotonic() < deadline:
        pid, observer = newest_observer()
        text = read(observer)
        if f"LOADED pid={pid} " in text and "R120_PACE_ENABLED" in text:
            break
        time.sleep(0.5)
    else:
        raise SystemExit("R120 observer never loaded with the pacer enabled")

    text = read(observer)
    loaded = next(l for l in text.splitlines() if l.startswith(f"LOADED pid={pid} "))
    layers = [l for l in text.splitlines() if l.startswith(f"TITLE_LAYER_SELECTED pid={pid} ")]
    evidence = [l for l in text.splitlines() if l.startswith("R120_EVIDENCE_DIRECTORY ")]
    refused = [l for l in text.splitlines() if l.startswith("R120_EVIDENCE_DIRECTORY=REFUSED")]
    pace = [l for l in text.splitlines() if l.startswith("R120_PACE_ENABLED")]
    boundary = [l for l in text.splitlines() if l.startswith("R120_EVIDENCE_BOUNDARY")]

    if EXPECTED_DYLIB not in loaded:
        raise SystemExit(f"{EXPECTED_DYLIB} not in the loaded image list")
    if not layers or "deviceName=Apple M3" not in layers[0]:
        raise SystemExit("exact CAMetalLayer/device not verified")
    if refused:
        raise SystemExit("evidence directory was refused by the corpus guard")
    if not evidence:
        raise SystemExit("R120_EVIDENCE_DIRECTORY line missing")
    if f"path={EXPECTED_EVIDENCE}" not in evidence[0]:
        raise SystemExit(f"unexpected evidence directory: {evidence[0]}")
    if "writable=1" not in evidence[0]:
        raise SystemExit(f"evidence directory not writable: {evidence[0]}")
    if not pace:
        raise SystemExit("R120_PACE_ENABLED missing: the pacer did not arm")

    log(f"R120_PREFLIGHT=READY pid={pid} dylib={EXPECTED_DYLIB} "
        f"layerAndDevice=VERIFIED evidenceDirectory=ISOLATED window={seconds:.1f}s",
        controller_log)
    log(layers[0], controller_log)
    log(evidence[0], controller_log)
    log(pace[0], controller_log)
    if boundary:
        log(boundary[0], controller_log)

    removed = []
    for path in STALE:
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            pass
    log(f"R120_STALE_STATE=REMOVED count={len(removed)} files={','.join(removed) or 'none'}",
        controller_log)

    WORLD.write_text("VISIBLE_WORLD\n", encoding="utf-8")
    CONDITION.write_text("CAMERA\n", encoding="utf-8")
    log(f"R120_WORLD_SCENE=CONFIRMED pid={pid}", controller_log)
    log("R120_ACTION=ROTATE_CAMERA_NOW hold a steady physical camera rotation for "
        f"the whole {seconds:.0f}s window (no synthetic input). The pacer is live.",
        controller_log)

    # Wait for the operator to get a few seconds of rotation in before the window
    # opens, so the window contains steady-state gameplay and not the transition.
    settle = min(5.0, seconds / 4.0)
    time.sleep(settle)
    window_start = time.monotonic()
    window_offset = len(read(observer))
    log(f"R120_WINDOW=OPEN pid={pid} offset={window_offset} settle={settle:.1f}s",
        controller_log)

    last_size = window_offset
    while time.monotonic() - window_start < seconds:
        time.sleep(0.5)
        size = len(read(observer))
        if size == last_size and time.monotonic() - window_start > 8.0:
            log("R120_WATCHDOG=NO_PROGRESS_8S (game may be paused or window unfocused)",
                controller_log)
        last_size = size

    full = read(observer)
    slice_text = full[window_offset:]
    slice_path = run_dir / "slice.log"
    slice_path.write_text(slice_text, encoding="utf-8")
    log(f"R120_WINDOW=CLOSED pid={pid} bytes={len(slice_text)} slice={slice_path}",
        controller_log)

    presents = slice_text.count("R120_PACE_ACQUIRE")
    acquisitions = slice_text.count("R108_SOURCE_FRAME_ACQUIRED")
    displayed = slice_text.count("R101_WSI_DISPLAYED")

    result = {
        "schema": "fgmetal-r120-pace-window-v1",
        "gamePid": pid,
        "observerLog": str(observer),
        "slice": str(slice_path),
        "dylib": EXPECTED_DYLIB,
        "evidenceDirectory": evidence[0],
        "paceBanner": pace[0],
        "windowSeconds": seconds,
        "windowSettleSeconds": settle,
        "pacedPresents": presents,
        "acquisitions": acquisitions,
        "displayedRecords": displayed,
        "manualPhysicalInputOnly": True,
        "syntheticGameplayInputSent": False,
    }
    result_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    log(f"R120_CONTROLLER=DONE presents={presents} acquisitions={acquisitions} "
        f"displayed={displayed} result={result_json}", controller_log)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
