#!/usr/bin/env python3
"""Does analyze_r120_pairs.py actually reject a bad run?

An analyser that only ever prints VALIDATED is worthless as evidence, so this
test drives it with synthetic logs whose defects are known by construction. Each
case names the check that must fail; a case passes only if the analyser returns
NOT_VALIDATED *and* the expected check is among the failures.

The synthetic log reproduces the real record topology rather than a simplified
one, because that topology is exactly where an earlier version of the analyser
went wrong: a deferred SOURCE B records its display time only through
R116_PRESENTATION_ORDER, never through R101_WSI_DISPLAYED. A generator that
emitted B as R101 would let the analyser pass while being blind to real logs.

The clean case models the IDEAL: every consecutive pair interpolated, so pair n
is (n, n+1) and frame n+1 is simultaneously B of pair n and A of pair n+1. That
frame's display time is therefore known only from R116, not from R101 -- the
analyser must join on both or it cannot model continuous operation at all.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ANALYSER = HERE / "analyze_r120_pairs.py"
REFRESH = 1.0 / 60.0
SOURCE = 1.0 / 30.0
T0 = 1000.0


def build(pairs: int, *, drop_m: int = 0, coalesce_m: int = 0,
          omit_all_m: bool = False, dup_source: int = 0) -> str:
    lines: list[str] = []
    frames = pairs + 2
    # With no M at all there are no tuples, so every frame is a plain SOURCE.
    tuple_b = {} if omit_all_m else {n + 1: n for n in range(1, pairs + 1)}

    for seq in range(1, frames + 1):
        pt = T0 + (seq - 1) * SOURCE
        lines.append(f"R108_SOURCE_FRAME_ACQUIRED seq={seq} "
                     f"acquiredAtMonotonicNs={int(pt * 1e9)} drawableID={seq} "
                     f"layerDevice=0x2 textureDevice=0x2 sameDevice=1")
        # Only frames that are not a deferred B are presented through R101.
        if seq not in tuple_b:
            lines.append(f"R101_WSI_DISPLAYED origin=SOURCE wsiPresentId={seq} "
                         f"appFrameId={seq} internalPresentId=0 drawableID={seq} "
                         f"presentedTime={pt:.9f} reachedDisplay=1")
            if seq == dup_source:
                lines.append(f"R101_WSI_DISPLAYED origin=SOURCE wsiPresentId=9999 "
                             f"appFrameId={seq} internalPresentId=0 drawableID=9999 "
                             f"presentedTime={pt + 0.0001:.9f} reachedDisplay=1")

    for n in range(1, pairs + 1):
        a, b = n, n + 1
        mid = 1000 + n
        a_pt = T0 + (a - 1) * SOURCE
        m_pt = a_pt + REFRESH
        b_pt = a_pt + 2 * REFRESH
        if n == coalesce_m:
            m_pt = b_pt
        lines.append(f"R102_WSI_DISPATCH origin=INTERNAL ticketId={n} "
                     f"internalPresentId={mid} wsiPresentId={5000 + n} "
                     f"appFrameId=NONE sourcePair={a}->{b} drawable={mid} "
                     f"drawableID={mid} texture={mid} layer=0x1 device=0x2 queue=0x3")
        lines.append(f"R102_SOURCE_FORWARD_BEGIN ticketId={n} appFrameId={b} "
                     f"sourceSequence={b} state=6 reason=none")
        lines.append(f"R115_TUPLE_TIMING generation={n} pair={a}->{b} "
                     f"outcome=PENDING_DISPLAY reason=B-forwarded armToReserveNs=0 "
                     f"reserveToBCommitNs=66780000 bCommitToPresentHookNs=1000000 "
                     f"estimatorNs=19000000 metalfxNs=14400000 gateNs=84000 "
                     f"bHoldNs=0 producerCompleteToMWsiNs=0 "
                     f"producerCompleteToMDisplayNs=0 mDisplayToBDispatchNs=0 "
                     f"aDisplayToMDisplayNs=16666667 bPresentHookToMWsiNs=23000000 "
                     f"tupleTotalNs=0")
        if n == dup_source:
            lines.append(f"R101_WSI_DISPLAYED origin=SOURCE wsiPresentId=9999 "
                         f"appFrameId={a} internalPresentId=0 drawableID=9999 "
                         f"presentedTime={a_pt + 0.0001:.9f} reachedDisplay=1")
        if n == drop_m or omit_all_m:
            continue
        lines.append(f"R102_INTERNAL_PRESENTED_CALLBACK ticketId={n} "
                     f"internalPresentId={mid} wsiPresentId={5000 + n} "
                     f"sourcePair={a}->{b} drawableID={mid} drawable=0x1 "
                     f"layer=0x1 device=0x2 presentedTime={m_pt:.9f} reachedDisplay=1")
        delta = (b_pt - m_pt) * 1000.0
        ordered = 1 if b_pt > m_pt else 0
        distinct = 1 if abs(b_pt - m_pt) > 0.001 else 0
        coalesced = 0 if distinct else 1
        lines.append(f"R116_PRESENTATION_ORDER ticketId={n} "
                     f"mPresentedTime={m_pt:.9f} bPresentedTime={b_pt:.9f} "
                     f"deltaMs={delta:.3f} mReachedDisplay=1 bReachedDisplay=1 "
                     f"ordered={ordered} distinct={distinct} coalesced={coalesced} "
                     f"mDispatchToCallbackNs=0 bToBCallbackNs=16380000 "
                     f"verdict={'M' if distinct else 'FAILED'}")

    lines.append("R116_RUN_SUMMARY earlyBDispatch=10 lateMCallbacksAfterEarlyB=0 "
                 "orderingViolations=0 coalescedPresentations=0 "
                 "presentationCallbackMissing=0 watchdogArmed=10 watchdogFired=0 "
                 "displayConfirmed=10 displayRejected=0 displayEvidenceLost=0 "
                 "promotionEligible=1")
    lines.append("R115_CONTINUOUS_RUN=COMPLETE attempts=10 accepted=10 fallback=0 "
                 "sourceLeaksWhileClosed=0 staleCallbacks=0 duplicateMDispatch=0 "
                 "resets=0 postRunStartSequence=40 acceptanceRatio=1.000")
    return "\n".join(lines) + "\n"


def run(text: str) -> tuple[int, str]:
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as handle:
        handle.write(text)
        path = handle.name
    proc = subprocess.run([sys.executable, str(ANALYSER), path],
                          capture_output=True, text=True)
    Path(path).unlink()
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    cases = [
        ("clean-60hz-A-M-B", dict(), "VALIDATED", None),
        ("missing-M-for-pair-5", dict(drop_m=5), "NOT_VALIDATED", "displayed_order_A_M_B"),
        ("M-coalesced-with-B", dict(coalesce_m=5), "NOT_VALIDATED", "every_accepted_pair_displayed"),
        ("source-only-30fps", dict(omit_all_m=True), "NOT_VALIDATED", "output_cadence_60hz"),
        ("duplicate-source", dict(dup_source=1), "NOT_VALIDATED", "no_duplicate_source"),
    ]
    failures = 0
    for name, kwargs, expect, expect_check in cases:
        code, out = run(build(30, **kwargs))
        verdict = "VALIDATED" if "CONTINUOUS_30_TO_60_TITLE_FG=VALIDATED" in out else "NOT_VALIDATED"
        ok = verdict == expect
        if ok and expect_check:
            ok = f"FAIL {expect_check}" in out
        print(f"  {'PASS' if ok else 'FAIL'}  {name:26s} expected={expect:13s} got={verdict:13s}"
              + (f" requiredCheck={expect_check}" if expect_check else ""))
        if not ok:
            failures += 1
            print("\n".join("        " + l for l in out.splitlines()[-14:]))
    print(f"R120_PAIRS_ANALYSER_SELFTEST={'PASS' if failures == 0 else 'FAIL'} "
          f"cases={len(cases)} failures={failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
