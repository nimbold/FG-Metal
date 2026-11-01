#!/usr/bin/env python3
"""Negative-and-positive self-test for analyze_r121.py.

The analyser is the only thing standing between a log and a performance claim,
so it is tested against synthetic slices in which the defect is known by
construction. Each case asserts the analyser reaches the verdict the defect
demands, not merely that it runs.

Cases
  1 CLEAN            twenty links, exact reconciliation, 30 fps SOURCE  -> PASS
  2 RECONCILIATION   chainSum off by 1 ms                              -> FAIL
  3 MISSING_LINK     one link absent                                   -> FAIL
  4 ORDER            one link negative                                 -> FAIL
  5 CLOCK_SKEW       copy closure residual 5 ms                        -> FAIL
  6 LIVENESS         liveSourceMax > pool                              -> FAIL
  7 BUDGET           headline above 33.3 ms                            -> FAIL
  8 REFRESH_TRAP     a 16.667 ms INTERNAL wait far from any callback    -> not counted
                                                                          as refresh-locked

Usage: test_analyze_r121.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyze_r121 as A  # noqa: E402

BASE = 312_000_000_000_000
REFRESH = 16_666_667


def chain_line(generation: int, deltas: list[int], *, sum_bias: int = 0,
               order_break: int | None = None) -> str:
    """Builds a CHAIN line whose reported sum may be deliberately wrong."""
    fields = {
        "generation": str(generation),
        "pair": f"{100 + generation}->{101 + generation}",
        "outcome": "ACCEPTED",
        "reason": "none",
        "headlineNs": str(sum(deltas)),
        "chainSumNs": str(sum(deltas) + sum_bias),
        "residualNs": str(sum_bias),
        "links": "20/20",
        "missing": "0",
        "orderViolations": "1" if order_break is not None else "0",
        "marks": "21",
    }
    for index, name in enumerate(A.LINKS):
        value = deltas[index]
        if order_break is not None and index == order_break:
            value = -abs(value) - 1
        fields[name] = str(value)
    body = " ".join(f"{key}={value}" for key, value in fields.items())
    return "R121_B2M_CHAIN " + body


def copy_line(generation: int, *, nd_block: int, nd_return_offset: int,
              outstanding: int = 2, pool: int = 3,
              src_skew: int = 0, m_skew: int = 0) -> str:
    # The observer publishes, for each copy, a media-clock duration and a
    # monotonic-clock duration of the same interval. Their difference is the
    # cross-clock validation: zero in a healthy run, hundreds of seconds if the
    # two domains were mixed.
    src_mono = 12_000_000 + 200_000 + 80_000
    m_mono = 600_000 + 300_000 + 70_000
    fields = {
        "generation": str(generation),
        "pair": f"{100 + generation}->{101 + generation}",
        "srcCopyCommitToGpuStartNs": "12000000",
        "srcCopyGpuExecNs": "200000",
        "srcCopyGpuEndToWakeNs": "80000",
        "srcCopyCommitToWakeNs": str(src_mono),
        "srcCopyMediaDurationNs": str(src_mono + src_skew),
        "srcCopyClockSkewNs": str(src_skew),
        "mCopyCommitToGpuStartNs": "600000",
        "mCopyGpuExecNs": "300000",
        "mCopyGpuEndToWakeNs": "70000",
        "mCopyCommitToWakeNs": str(m_mono),
        "mCopyMediaDurationNs": str(m_mono + m_skew),
        "mCopyClockSkewNs": str(m_skew),
        "dispatchToPresentEnterNs": "1000",
        "presentCallNs": "20000",
        "poolMaximumDrawables": str(pool),
        "deferredBPresent": "1",
        "liveSourceDrawables": str(outstanding),
        "liveInternalDrawables": "1",
        "liveSourceMax": str(outstanding),
        "liveInternalMax": "1",
        "sourceAcquiredTotal": "523",
        "sourceReleasedTotal": "512",
        "sourceDeferredTotal": "11",
        "internalAcquiredTotal": "11",
        "internalReleasedTotal": "11",
        "internalSlotState": "2",
        "frontierState": "3",
        "sourceNdEntryNs": str(BASE + 1_000_000),
        "sourceNdReturnNs": str(BASE + 1_000_000 + 50_000_000),
        "sourceNdBlockNs": "50000000",
        "internalNdEnterNs": str(BASE + 40_000_000),
        "internalNdReturnNs": str(BASE + 40_000_000 + nd_return_offset),
        "bHookNs": str(BASE),
        "mWsiDispatchNs": str(BASE + 47_000_000),
    }
    body = " ".join(f"{key}={value}" for key, value in fields.items())
    return "R121_B2M_COPY " + body


def display_events(count: int, start: int, step: int) -> str:
    lines = []
    for index in range(count):
        lines.append(
            f"R102_EVENT t_ns={start + index * step} event=INTERNAL_DISPLAYED_CALLBACK "
            f"ticketId={index} sourceSequence=0 internalPresentId={index} "
            f"wsiPresentId={index} drawableID={index} drawable=0x0 texture=0x0 "
            f"commandBuffer=0 encoder=0 queue=0 layer=0x0 device=0x0 "
            f"selector=addPresentedHandler: argument=0.000000000 reachedDisplay=1")
        lines.append(
            f"R101_WSI_DISPLAYED origin=SOURCE wsiPresentId={index} appFrameId={index} "
            f"internalPresentId=0 drawableID={index} presentedTime={1000.0 + index * 0.0333333:.9f} "
            f"reachedDisplay=1")
        # SOURCE acquisition cadence: 33.333 ms, i.e. a genuine 30 fps source.
        lines.append(
            f"R110_SOURCE_NEXT_DRAWABLE_ENTRY pid=1 callId={index} "
            f"entryMonotonicNs={start + index * REFRESH * 2} layer=0x0 device=0x0 "
            f"registryID=0")
    return "\n".join(lines)


CLEAN_DELTAS = [
    1_400_000,      # hook -> capture entry
    150_000,        # encode
    30_000,         # commit
    22_500_000,     # SOURCE copy GPU wait
    40_000,         # wake -> enqueue
    15_000,         # enqueue -> start
    200_000,        # prep
    15_600_000,     # estimator
    30_000,         # estimator -> MetalFX
    8_700_000,      # MetalFX
    100_000,        # gate
    60_000,         # gate -> acquire request
    10_000,         # request -> ND enter
    900_000,        # INTERNAL ND wait
    20_000,         # ND return -> acquired
    50_000,         # acquired -> M copy encode
    120_000,        # encode
    800_000,        # M copy commit -> wake
    60_000,         # wake -> WSI alloc
    30_000,         # alloc -> dispatch
]


def build_slice(cases: dict) -> str:
    lines = []
    for generation, (deltas, chain_kwargs, copy_kwargs) in cases.items():
        lines.append(chain_line(generation, deltas, **chain_kwargs))
        lines.append(copy_line(generation, **copy_kwargs))
    lines.append(display_events(8, BASE, REFRESH))
    return "\n".join(lines) + "\n"


def run(tmp: Path, text: str) -> dict:
    path = tmp / "slice.log"
    path.write_text(text, encoding="utf-8")
    return A.analyse(path)


def main() -> int:
    tmp = Path("/tmp/r121-selftest")
    tmp.mkdir(parents=True, exist_ok=True)
    failures = []

    def expect(name: str, condition: bool, detail: str = "") -> None:
        status = "PASS" if condition else "FAIL"
        print(f"SELFTEST {name} {status} {detail}")
        if not condition:
            failures.append(name)

    clean_copy = {"nd_block": 900_000, "nd_return_offset": 900_000}
    # FAST: the same shape as CLEAN but with the SOURCE-copy wait and the frozen
    # GPU work reduced enough to sit inside the 33.3 ms pair budget, so every
    # check including the budget check must pass.
    fast_deltas = list(CLEAN_DELTAS)
    fast_deltas[3] = 8_000_000     # SOURCE copy GPU wait
    fast_deltas[7] = 10_000_000    # estimator
    fast_deltas[9] = 6_000_000     # MetalFX
    fast = build_slice({1: (fast_deltas, {}, clean_copy),
                        2: (fast_deltas, {}, clean_copy)})
    result = run(tmp, fast)
    expect("FAST_PASSES", result["verdict"] == "PASS", f"verdict={result['verdict']}")
    expect("FAST_TWO_TUPLES", result["tuplesMeasured"] == 2,
           f"tuples={result['tuplesMeasured']}")
    expect("FAST_RESIDUAL_ZERO", result["reconciliation"]["maxAbsResidualNs"] == 0,
           f"max={result['reconciliation']['maxAbsResidualNs']}")

    # CLEAN: the R120-shaped case. It is over budget by construction, so exactly
    # the budget check must fail and nothing else.
    clean = build_slice({1: (CLEAN_DELTAS, {}, clean_copy),
                         2: (CLEAN_DELTAS, {}, clean_copy)})
    result = run(tmp, clean)
    failing = sorted(c["name"] for c in result["checks"] if not c["pass"])
    expect("CLEAN_ONLY_BUDGET_FAILS", failing == ["headline_within_budget"],
           f"failing={failing}")
    expect("CLEAN_CLASSIFICATION",
           result["classification"]["verdict"].startswith("SOURCE_COPY_GPU_SUBMIT"),
           f"verdict={result['classification']['verdict']}")

    # 2: reconciliation off by 1 ms.
    bad = build_slice({1: (CLEAN_DELTAS, {"sum_bias": 1_000_000}, clean_copy)})
    result = run(tmp, bad)
    check = next(c for c in result["checks"] if c["name"] == "reconciliation_exact")
    expect("RECONCILIATION_REJECTED", check["pass"] is False, check["detail"])

    # 3: a missing link is reported as incomplete.
    line = chain_line(1, CLEAN_DELTAS).replace("links=20/20", "links=19/20")
    line = line.replace("missing=0", "missing=1")
    result = run(tmp, line + "\n" + copy_line(1, **clean_copy) + "\n")
    expect("MISSING_LINK_REJECTED", result["tuplesMeasured"] == 0,
           f"tuples={result['tuplesMeasured']}")

    # 4: a negative link is an ordering violation.
    bad = build_slice({1: (CLEAN_DELTAS, {"order_break": 3}, clean_copy)})
    result = run(tmp, bad)
    check = next(c for c in result["checks"] if c["name"] == "no_order_violation")
    expect("ORDER_REJECTED", check["pass"] is False, check["detail"])

    # 5: the two clocks disagree on the duration of the same copy. This is the
    # defect that a tautological closure residual could not detect, so the case
    # exists specifically to prove the new check fires.
    bad = build_slice({1: (CLEAN_DELTAS, {},
                           {"nd_block": 900_000, "nd_return_offset": 900_000,
                            "src_skew": 5_000_000})})
    result = run(tmp, bad)
    check = next(c for c in result["checks"] if c["name"] == "copy_clock_compatible")
    expect("CLOCK_SKEW_REJECTED", check["pass"] is False, check["detail"])

    # 6: outstanding drawables exceeding the pool is a bookkeeping defect, not a
    # datum to report as pool pressure.
    bad = build_slice({1: (CLEAN_DELTAS, {},
                           {"nd_block": 900_000, "nd_return_offset": 900_000,
                            "outstanding": 4})})
    result = run(tmp, bad)
    check = next(c for c in result["checks"] if c["name"] == "drawable_bookkeeping_bounded")
    expect("LIVENESS_REJECTED", check["pass"] is False, check["detail"])

    # 7: headline over budget.
    slow = [value * 3 for value in CLEAN_DELTAS]
    bad = build_slice({1: (slow, {},
                           {"nd_block": 900_000, "nd_return_offset": 900_000})})
    result = run(tmp, bad)
    check = next(c for c in result["checks"] if c["name"] == "headline_within_budget")
    expect("BUDGET_REJECTED", check["pass"] is False, check["detail"])

    # 8: the refresh trap. A wait of exactly one refresh, but its return instant
    # is 40 ms away from every display callback, must NOT be counted as
    # refresh-locked.
    far = build_slice({1: (CLEAN_DELTAS, {},
                           {"nd_block": REFRESH, "nd_return_offset": REFRESH})})
    # Move the display events far from the INTERNAL return instant.
    far = far.replace(str(BASE + 40_000_000), str(BASE + 300_000_000))
    result = run(tmp, far)
    expect("REFRESH_TRAP_NOT_CORRELATED",
           result["refreshQuantum"]["returnsNearDisplayCallback"] == 0,
           f"nearCallback={result['refreshQuantum']['returnsNearDisplayCallback']}")

    print(f"SELFTEST_SUMMARY failures={len(failures)} {' '.join(failures)}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
