#!/usr/bin/env python3
"""R120 Phase 2 controller: bounded continuous 30->60 A/M/B pair run.

Scope is deliberately narrow. This tool may only:
  - verify the exact game session, layer, device, observer dylib and that the
    observer resolved the R120 evidence directory;
  - remove stale control files and write the R115 start request so the bounded
    tuple budget can arm;
  - cut a log slice covering the run and hand it to the analyser.

It never chooses a pair, never schedules a present, and never sends synthetic
gameplay input. The camera is the operator's.

Why a separate controller from the Phase 1 pacer run: Phase 1 must not have any
tuple armed, and this phase must have exactly one bounded budget. Keeping them
in separate tools means neither can silently run with the other's configuration.

Usage: run_r120_pairs.py [label] [--timeout 180]
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
START = RUNTIME / "r115-continuous-start"

# Files from earlier rounds that would arm or confuse the pipeline if left over.
# Removed rather than assumed absent: a stale arm request would arm a pair before
# the operator is ready, and a stale tuple target would leave the frontier closed.
STALE = (
    RUNTIME / "r112-target-source-b",
    RUNTIME / "r110-target-source-b",
    RUNTIME / "r114-arm-next-moving-pair",
    START,
    WORLD,
    CONDITION,
)

EXPECTED_DYLIB = "libfgmetal_r120_pace_probe.dylib"
EXPECTED_EVIDENCE = str(OUT)
COMPLETE = "R115_CONTINUOUS_RUN=COMPLETE"


def log(line: str, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        stream.write(line + "\n")
        stream.flush()
    print(line, flush=True)


def newest_observer() -> tuple[int, Path]:
    """Newest observer log that belongs to a real game process.

    Selection must not be "newest file wins": the dlopen probe host also writes
    observer logs, and it has no CAMetalLayer, so picking one makes the preflight
    fail with a misleading message. A log that contains TITLE_LAYER_SELECTED came
    from a process that actually selected the title layer.
    """
    candidates = sorted(RUNTIME.glob("observer.*.log"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise SystemExit("no observer log found")
    for path in reversed(candidates):
        try:
            head = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "TITLE_LAYER_SELECTED " in head:
            return int(path.name.split(".")[1]), path
    path = candidates[-1]
    return int(path.name.split(".")[1]), path


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    label = args[0] if args else "02-pairs10"
    timeout = 180.0
    for arg in sys.argv[1:]:
        if arg.startswith("--timeout"):
            timeout = float(arg.split("=", 1)[1])

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
        if (f"LOADED pid={pid} " in text and "R120_PACE_ENABLED" in text
                and "R120_CONTINUOUS_ENABLED pid=" in text):
            break
        # A process carrying the pacer but not the continuous pipeline is not a
        # slow start, it is the wrong environment: Highball caches the bottle
        # environment in memory when the Highball app itself starts, so a game
        # relaunched without restarting Highball keeps the previous flags. Report
        # that precisely instead of waiting out the preflight window.
        if f"R120_CONTINUOUS_ENABLED=NO pid={pid} " in text:
            line = next(l for l in text.splitlines()
                        if l.startswith(f"R120_CONTINUOUS_ENABLED=NO pid={pid} "))
            log(f"R120_PAIRS_REFUSED reason=wrong-environment-in-process {line}",
                controller_log)
            raise SystemExit(
                "the running game does not have the continuous pipeline: "
                "Highball was not restarted after the bottle environment changed. "
                "Quit the game AND Highball, relaunch Highball, then launch the pin.")
        time.sleep(0.5)
    else:
        raise SystemExit("observer never loaded with both the pacer and the continuous pipeline")

    text = read(observer)
    loaded = next(l for l in text.splitlines() if l.startswith(f"LOADED pid={pid} "))
    layers = [l for l in text.splitlines() if l.startswith(f"TITLE_LAYER_SELECTED pid={pid} ")]
    pools = [l for l in text.splitlines() if l.startswith(f"R120_DRAWABLE_POOL pid={pid} ")]
    evidence = [l for l in text.splitlines() if l.startswith("R120_EVIDENCE_DIRECTORY ")]
    refused = [l for l in text.splitlines() if l.startswith("R120_EVIDENCE_DIRECTORY=REFUSED")]
    pace = [l for l in text.splitlines() if l.startswith("R120_PACE_ENABLED")]
    cont = [l for l in text.splitlines() if l.startswith("R120_CONTINUOUS_ENABLED pid=")]

    if EXPECTED_DYLIB not in loaded:
        raise SystemExit(f"{EXPECTED_DYLIB} not in the loaded image list")
    if not layers or "deviceName=Apple M3" not in layers[0]:
        raise SystemExit("exact CAMetalLayer/device not verified")
    if refused:
        raise SystemExit("evidence directory was refused by the corpus guard")
    if not evidence or f"path={EXPECTED_EVIDENCE}" not in evidence[0]:
        raise SystemExit("R120 evidence directory not resolved to this round")
    if "writable=1" not in evidence[0]:
        raise SystemExit("evidence directory not writable")
    if not pace:
        raise SystemExit("R120_PACE_ENABLED missing: the pacer did not arm")
    if not cont:
        raise SystemExit("R120_CONTINUOUS_ENABLED missing: the continuous pipeline did not arm")
    if not pools:
        raise SystemExit("R120_DRAWABLE_POOL missing: pool depth was not measured")
    # Hard deadlock bound, not a preference. The deferred-B pipeline holds B's
    # drawable across the generation window while INTERNAL M needs a second, and
    # the game needs a third for the next SOURCE frame. With fewer than three the
    # render thread would block inside nextDrawable and the game would freeze
    # rather than fall back, so the run is refused before anything is armed.
    if "poolSufficient=0" in pools[0]:
        log(f"R120_PAIRS_REFUSED reason=drawable-pool-too-shallow {pools[0]}",
            controller_log)
        raise SystemExit("drawable pool cannot support deferred-B + M + next SOURCE; "
                         "refusing to arm a run that could deadlock the render thread")

    log(f"R120_PAIRS_PREFLIGHT=READY pid={pid} dylib={EXPECTED_DYLIB} "
        f"layerAndDevice=VERIFIED evidenceDirectory=ISOLATED timeout={timeout:.0f}s",
        controller_log)
    log(layers[0], controller_log)
    log(pools[0], controller_log)
    log(evidence[0], controller_log)
    log(pace[0], controller_log)
    log(cont[0], controller_log)

    removed = []
    for path in STALE:
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            pass
    log(f"R120_STALE_STATE=REMOVED count={len(removed)} files={','.join(removed) or 'none'}",
        controller_log)

    window_offset = len(text)
    log(f"R120_PAIRS_WINDOW=OPEN pid={pid} offset={window_offset}", controller_log)

    WORLD.write_text("VISIBLE_WORLD\n", encoding="utf-8")
    CONDITION.write_text("CAMERA\n", encoding="utf-8")
    log(f"R120_WORLD_SCENE=CONFIRMED pid={pid}", controller_log)

    time.sleep(1.0)
    START.write_text("CONTINUOUS\n", encoding="utf-8")
    log("R120_ACTION=ROTATE_CAMERA_NOW bounded run armed; keep a steady physical "
        "camera rotation going (no synthetic input)", controller_log)

    started = time.monotonic()
    complete = False
    while time.monotonic() - started < timeout:
        slice_text = read(observer)[window_offset:]
        if COMPLETE in slice_text:
            complete = True
            break
        time.sleep(0.5)

    # Let the last presented callbacks land before the slice is cut, so a tuple
    # that displayed on the final refresh is not counted as missing evidence.
    time.sleep(2.0)

    slice_text = read(observer)[window_offset:]
    slice_path = run_dir / "slice.log"
    slice_path.write_text(slice_text, encoding="utf-8")
    log(f"R120_PAIRS_WINDOW=CLOSED pid={pid} bytes={len(slice_text)} "
        f"elapsed={time.monotonic() - started:.1f}s complete={int(complete)} "
        f"slice={slice_path}", controller_log)

    def grab(prefix: str) -> list[str]:
        return [l for l in slice_text.splitlines() if l.startswith(prefix)]

    run_line = grab("R115_CONTINUOUS_RUN=COMPLETE")
    summary = grab("R116_RUN_SUMMARY ")
    gate = grab("R116_GATE_SUMMARY")
    fallbacks = [l for l in slice_text.splitlines() if "FALLBACK" in l]
    starvation = [l for l in slice_text.splitlines()
                  if "INTERNAL_DRAWABLE_ACQUIRE=FAILED" in l]

    result = {
        "schema": "fgmetal-r120-pairs-window-v1",
        "gamePid": pid,
        "observerLog": str(observer),
        "slice": str(slice_path),
        "dylib": EXPECTED_DYLIB,
        "evidenceDirectory": evidence[0],
        "paceBanner": pace[0],
        "continuousBanner": cont[0],
        "drawablePool": pools[0],
        "runComplete": complete,
        "runCompleteLine": run_line[0] if run_line else None,
        "runSummaryLine": summary[0] if summary else None,
        "gateSummaryLine": gate[0] if gate else None,
        "fallbackCount": len(fallbacks),
        "internalDrawableAcquireFailures": len(starvation),
        "manualPhysicalInputOnly": True,
        "syntheticGameplayInputSent": False,
    }
    result_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    log(f"R120_PAIRS_CONTROLLER=DONE complete={int(complete)} "
        f"fallbacks={len(fallbacks)} drawableAcquireFailures={len(starvation)} "
        f"result={result_json}", controller_log)
    if run_line:
        print(run_line[0])
    if summary:
        print(summary[0])
    return 0 if complete else 3


if __name__ == "__main__":
    raise SystemExit(main())
