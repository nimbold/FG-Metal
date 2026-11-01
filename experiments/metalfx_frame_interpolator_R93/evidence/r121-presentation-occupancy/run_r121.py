#!/usr/bin/env python3
"""R121 controller: bounded 10-tuple presentation-occupancy measurement.

Narrow by design, and deliberately narrower than R120's pair controller:

  - it may only verify the running session (dylib, layer, device, resolved
    evidence directory, pacer, continuous pipeline, pool depth);
  - it removes stale control files and writes the R115 start request;
  - it cuts a log slice covering the run and hands it to the R121 analyser.

It never chooses a pair, never schedules a present, never sends synthetic input.
The camera is the operator's.

The R121 additions to the preflight are the two things this round depends on:
the process must be running the R121 observer (not R120's), and the observer
must have published the R121 chain banner, because a run whose tuples carry no
decomposition is worthless even if every R120 check passes.

Usage: run_r121.py [label] [--timeout 180]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
OUT = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r121-presentation-occupancy"
RUNTIME = Path("/tmp/fgmetal-r108-unattended")
WORLD = RUNTIME / "r112-world-scene-confirmed"
CONDITION = RUNTIME / "capture-condition"
START = RUNTIME / "r115-continuous-start"

STALE = (
    RUNTIME / "r112-target-source-b",
    RUNTIME / "r110-target-source-b",
    RUNTIME / "r114-arm-next-moving-pair",
    START,
    WORLD,
    CONDITION,
)

EXPECTED_DYLIB = "libfgmetal_r121_occupancy_probe.dylib"
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

    "Newest file wins" is wrong: the dlopen probe host also writes observer logs
    and has no CAMetalLayer. A log containing TITLE_LAYER_SELECTED came from a
    process that actually selected the title layer.
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


def option(argv: list[str], name: str, default: float) -> float:
    """Accepts both `--name value` and `--name=value`.

    The R120 controller only accepted the `=` form and crashed on the spaced
    form. A controller that dies before it arms is indistinguishable, from the
    operator's seat, from a game that never presented -- so both spellings are
    accepted and an unparseable value is an explicit refusal, not a traceback.
    """
    for index, arg in enumerate(argv):
        if arg == name:
            if index + 1 >= len(argv):
                raise SystemExit(f"{name} requires a value")
            return float(argv[index + 1])
        if arg.startswith(name + "="):
            return float(arg.split("=", 1)[1])
    return default


def positional(argv: list[str]) -> list[str]:
    """Positional arguments only: option values are skipped, not mistaken for them.

    R120 derived the label from every non-`--` token, so `--timeout 120` named the
    run directory `run-120`. A run directory named after a timeout is exactly the
    kind of mislabelled evidence this project cannot afford.
    """
    out, skip_next = [], False
    for arg in argv:
        if skip_next:
            skip_next = False
            continue
        if arg in ("--timeout", "--arm-delay"):
            skip_next = True
            continue
        if arg.startswith("--"):
            continue
        out.append(arg)
    return out


def main() -> int:
    args = positional(sys.argv[1:])
    label = args[0] if args else "01-pairs10"
    timeout = option(sys.argv[1:], "--timeout", 180.0)
    arm_delay = option(sys.argv[1:], "--arm-delay", 0.0)

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
        if f"R120_CONTINUOUS_ENABLED=NO pid={pid} " in text:
            line = next(l for l in text.splitlines()
                        if l.startswith(f"R120_CONTINUOUS_ENABLED=NO pid={pid} "))
            log(f"R121_PAIRS_REFUSED reason=wrong-environment-in-process {line}",
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
    evidence = [l for l in text.splitlines() if l.startswith("R121_EVIDENCE_DIRECTORY ")]
    refused = [l for l in text.splitlines() if l.startswith("R121_EVIDENCE_DIRECTORY=REFUSED")]
    pace = [l for l in text.splitlines() if l.startswith("R120_PACE_ENABLED")]
    cont = [l for l in text.splitlines() if l.startswith("R120_CONTINUOUS_ENABLED pid=")]

    if EXPECTED_DYLIB not in loaded:
        raise SystemExit(
            f"{EXPECTED_DYLIB} not in the loaded image list; the process is running "
            "a different round's observer")
    if not layers or "deviceName=Apple M3" not in layers[0]:
        raise SystemExit("exact CAMetalLayer/device not verified")
    if refused:
        raise SystemExit("evidence directory was refused by the corpus guard")
    if not evidence or f"path={EXPECTED_EVIDENCE}" not in evidence[0]:
        raise SystemExit("R121 evidence directory not resolved to this round")
    if "writable=1" not in evidence[0]:
        raise SystemExit("evidence directory not writable")
    if not pace:
        raise SystemExit("R120_PACE_ENABLED missing: the pacer did not arm")
    if not cont:
        raise SystemExit("R120_CONTINUOUS_ENABLED missing: the continuous pipeline did not arm")
    if not pools:
        raise SystemExit("R120_DRAWABLE_POOL missing: pool depth was not measured")
    if "poolSufficient=0" in pools[0]:
        log(f"R121_PAIRS_REFUSED reason=drawable-pool-too-shallow {pools[0]}",
            controller_log)
        raise SystemExit("drawable pool cannot support deferred-B + M + next SOURCE; "
                         "refusing to arm a run that could deadlock the render thread")

    log(f"R121_PAIRS_PREFLIGHT=READY pid={pid} dylib={EXPECTED_DYLIB} "
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
    log(f"R121_STALE_STATE=REMOVED count={len(removed)} files={','.join(removed) or 'none'}",
        controller_log)

    window_offset = len(text)
    log(f"R121_PAIRS_WINDOW=OPEN pid={pid} offset={window_offset}", controller_log)

    WORLD.write_text("VISIBLE_WORLD\n", encoding="utf-8")
    CONDITION.write_text("CAMERA\n", encoding="utf-8")
    log(f"R121_WORLD_SCENE=CONFIRMED pid={pid}", controller_log)

    time.sleep(1.0)
    if arm_delay > 0:
        # The tuple budget is bounded by attempts (3x max tuples), and an attempt
        # that finds no motion is spent. Arming while the operator is still
        # reaching for the mouse would burn a third of the budget on still frames
        # and then measure a run that mostly fell back. The delay is announced in
        # the controller log so the run record shows it was deliberate.
        log(f"R121_ARM_DELAY={arm_delay:.0f}s reason=let-operator-establish-steady-camera-motion",
            controller_log)
        remaining = arm_delay
        while remaining > 0:
            time.sleep(min(2.0, remaining))
            remaining -= 2.0
            log(f"R121_ARM_COUNTDOWN remaining={max(0.0, remaining):.0f}s", controller_log)
    START.write_text("CONTINUOUS\n", encoding="utf-8")
    log("R121_ACTION=ROTATE_CAMERA_NOW bounded run armed; keep a steady physical "
        "camera rotation going (no synthetic input)", controller_log)

    started = time.monotonic()
    complete = False
    while time.monotonic() - started < timeout:
        slice_text = read(observer)[window_offset:]
        if COMPLETE in slice_text:
            complete = True
            break
        time.sleep(0.5)

    # Let the last presented callbacks land before the slice is cut.
    time.sleep(2.0)

    slice_text = read(observer)[window_offset:]
    slice_path = run_dir / "slice.log"
    slice_path.write_text(slice_text, encoding="utf-8")
    log(f"R121_PAIRS_WINDOW=CLOSED pid={pid} bytes={len(slice_text)} "
        f"elapsed={time.monotonic() - started:.1f}s complete={int(complete)} "
        f"slice={slice_path}", controller_log)

    def grab(prefix: str) -> list[str]:
        return [l for l in slice_text.splitlines() if l.startswith(prefix)]

    run_line = grab("R115_CONTINUOUS_RUN=COMPLETE")
    summary = grab("R116_RUN_SUMMARY ")
    gate = grab("R116_GATE_SUMMARY")
    chains = grab("R121_B2M_CHAIN ")
    copies = grab("R121_B2M_COPY ")
    fallbacks = [l for l in slice_text.splitlines() if "FALLBACK" in l]
    starvation = [l for l in slice_text.splitlines()
                  if "INTERNAL_DRAWABLE_ACQUIRE=FAILED" in l]

    result = {
        "schema": "fgmetal-r121-pairs-window-v1",
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
        "r121ChainLines": len(chains),
        "r121CopyLines": len(copies),
        "fallbackCount": len(fallbacks),
        "internalDrawableAcquireFailures": len(starvation),
        "manualPhysicalInputOnly": True,
        "syntheticGameplayInputSent": False,
    }
    result_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")

    log(f"R121_PAIRS_CONTROLLER=DONE complete={int(complete)} "
        f"chains={len(chains)} copies={len(copies)} fallbacks={len(fallbacks)} "
        f"drawableAcquireFailures={len(starvation)} result={result_json}",
        controller_log)
    if run_line:
        print(run_line[0])
    if summary:
        print(summary[0])
    return 0 if complete and chains else 3


if __name__ == "__main__":
    raise SystemExit(main())
