#!/usr/bin/env python3
"""R120 analyser self-test.

An analyser that cannot fail is worse than no analyser. This drives
analyze_r120.py with synthetic observer logs whose cadence is known by
construction, and asserts the verdict for each:

  A  genuine 30 fps source, paced              -> ESTABLISHED
  B  source still at 60 fps, limiter did nothing -> NOT_ESTABLISHED
  C  the forbidden cheat: game still produces at 60 fps and only every other
     frame is counted                              -> NOT_ESTABLISHED
  D  30 fps with normal jitter                 -> ESTABLISHED

Case C is the one the brief names explicitly. It is constructed so that the
*present* cadence looks like 30 fps while *acquisition* does not -- which is
exactly what "discard every second frame" would produce, and exactly why the
verdict requires acquisition to agree rather than trusting the limiter.

Usage: test_analyze_r120.py
"""

from __future__ import annotations

import json
import random
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANALYSER = HERE / "analyze_r120.py"
PERIOD = 1e9 / 30.0
REFRESH = 1e9 / 60.0


def build_log(path: Path, acquisition_ns: list[int], present_ns: list[int]) -> None:
    lines = []
    for index, t in enumerate(acquisition_ns):
        lines.append(
            f"R108_SOURCE_FRAME_ACQUIRED pid=999 seq={index} "
            f"acquiredAtMonotonicNs={t} drawableID={index % 3} layer=0x1 "
            f"texture=0x2 layerDevice=0x3 textureDevice=0x3 sameDevice=1 "
            f"registryID=1 framebufferOnly=0 width=1470 height=956 "
            f"pixelFormat=80 usage=7"
        )
    for index, t in enumerate(present_ns):
        lines.append(
            f"R120_PACE_PRESENT callId={index} entryNs={t - 1000} resumeNs={t} "
            f"blockedNs=0 intervalNs=0 nextAllowedNs={t + int(PERIOD)} "
            f"presents={index + 1} blocked=0"
        )
    for index, t in enumerate(present_ns):
        lines.append(
            f"R101_WSI_DISPLAYED origin=SOURCE wsiPresentId={index + 1} "
            f"appFrameId={index} internalPresentId=0 drawableID={index % 3} "
            f"presentedTime={t / 1e9:.9f} reachedDisplay=1"
        )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(path: Path) -> tuple[int, dict]:
    out = path.with_suffix(".json")
    proc = subprocess.run(
        [sys.executable, str(ANALYSER), str(path), "--json", str(out)],
        capture_output=True, text=True,
    )
    return proc.returncode, json.loads(out.read_text())


def main() -> int:
    random.seed(20261031)
    failures = []
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        def case(name, acq_step, pres_step, expect, jitter=0.0, count=240):
            acq, pres = [], []
            ta = 1_000_000_000
            tp = 1_000_000_000
            for _ in range(count):
                acq.append(int(ta))
                pres.append(int(tp))
                ta += acq_step + (random.uniform(-jitter, jitter) if jitter else 0)
                tp += pres_step + (random.uniform(-jitter, jitter) if jitter else 0)
            path = root / f"{name}.log"
            build_log(path, acq, pres)
            code, result = run(path)
            got = result["verdict"]
            ok = got == expect
            if not ok:
                failures.append((name, expect, got, result["criteria"]))
            print(f"  {name:34s} expect={expect:15s} got={got:15s} "
                  f"acqMedian={result['acquisition']['interval'].get('median', 0):7.3f}ms "
                  f"presMedian={result['present']['interval'].get('median', 0):7.3f}ms "
                  f"{'OK' if ok else 'FAIL'}")
            return result

        print("R120_ANALYSER_SELFTEST")
        # A: genuine 30 fps on both sides.
        case("A-genuine-30fps", PERIOD, PERIOD, "ESTABLISHED")
        # B: the limiter did nothing; everything is still 60 fps.
        case("B-still-60fps", REFRESH, REFRESH, "NOT_ESTABLISHED")
        # C: the forbidden cheat -- acquisition stays at 60 fps, presents are
        #    thinned to 30 fps. Must fail on acquisition, not on presentation.
        case("C-every-other-frame-discarded", REFRESH, PERIOD, "NOT_ESTABLISHED")
        # D: genuine 30 fps with +/-2 ms jitter.
        case("D-30fps-with-jitter", PERIOD, PERIOD, "ESTABLISHED", jitter=2e6)

        # C must fail specifically on the acquisition criterion. If it ever
        # passes, the verdict has stopped being about production.
        path = root / "C-every-other-frame-discarded.log"
        _, result_c = run(path)
        c_reason_ok = not result_c["criteria"]["acquisitionMedianWithin10pct"]
        print(f"  C-fails-on-acquisition-criterion   "
              f"{'OK' if c_reason_ok else 'FAIL'}")
        if not c_reason_ok:
            failures.append(("C-reason", "acquisition criterion fails", "passed", {}))

    if failures:
        print(f"R120_ANALYSER_SELFTEST=FAIL failures={len(failures)}")
        for item in failures:
            print("  ", item)
        return 1
    print("R120_ANALYSER_SELFTEST=PASS cases=4 discriminators=1")
    return 0


if __name__ == "__main__":
    sys.exit(main())
