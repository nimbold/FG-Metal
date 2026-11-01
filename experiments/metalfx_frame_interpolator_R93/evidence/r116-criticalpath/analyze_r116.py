#!/usr/bin/env python3
"""R116 latency / safety analyser.

Reads an R116 observer log and emits the report tables:
  - latency distributions (min / mean / median / p90 / max) for every R115 metric
    plus the new R116 gate and ordering metrics;
  - assertion counters and frontier / leak accounting;
  - throughput (accepted tuples per second, source interval).

Usage: analyze_r116.py <observer.log> [--json out.json]
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

KV = re.compile(r"(\w+)=(-?\d+(?:\.\d+)?)")


def nums(line: str) -> dict[str, float]:
    return {k: float(v) for k, v in KV.findall(line)}


def dist(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    v = sorted(values)
    n = len(v)
    return {
        "n": n,
        "min": v[0],
        "mean": sum(v) / n,
        "median": statistics.median(v),
        "p90": v[min(n - 1, int(n * 0.9))],
        "max": v[-1],
    }


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: analyze_r116.py <observer.log> [--json out.json]")
    path = Path(sys.argv[1])
    out_json = None
    if "--json" in sys.argv:
        out_json = Path(sys.argv[sys.argv.index("--json") + 1])

    timing: list[dict[str, float]] = []
    gate: list[dict[str, float]] = []
    order: list[dict[str, float]] = []
    eligibility = {"ACCEPTED": 0, "REJECTED": 0}
    reject_reasons: dict[str, int] = {}
    verdicts = {"VALIDATED": 0, "REJECTED": 0}
    leaks = 0
    stale = 0
    duplicate_m = 0
    deadlock_hops = 0
    watchdog = {"FIRED": 0, "EXPIRED_NOOP": 0}
    early_dispatch = 0
    late_m = 0
    summary_lines: list[str] = []
    gate_paths: dict[str, int] = {}
    source_gaps: list[float] = []
    last_seq_ns: tuple[int, int] | None = None

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("R115_TUPLE_TIMING"):
            timing.append(nums(line))
        elif line.startswith("R116_GATE_TIMING"):
            gate.append(nums(line))
        elif line.startswith("R116_PRESENTATION_ORDER"):
            order.append(nums(line))
        elif line.startswith("R110_FAST_PRESENT_ELIGIBILITY="):
            if "=ACCEPTED" in line:
                eligibility["ACCEPTED"] += 1
            else:
                eligibility["REJECTED"] += 1
                m = re.search(r"reason=([a-z0-9-]+)", line)
                if m:
                    reject_reasons[m.group(1)] = reject_reasons.get(m.group(1), 0) + 1
            m = re.search(r"gatePath=([a-z-]+)", line)
            if m:
                gate_paths[m.group(1)] = gate_paths.get(m.group(1), 0) + 1
        elif line.startswith("R110_TITLE_WSI_SEQUENCE=VALIDATED"):
            verdicts["VALIDATED"] += 1
        elif line.startswith("R110_TITLE_WSI_SEQUENCE=REJECTED"):
            verdicts["REJECTED"] += 1
        elif line.startswith("R116_COMMITTED_B_NO_PRESENT_WATCHDOG=FIRED"):
            watchdog["FIRED"] += 1
        elif line.startswith("R116_COMMITTED_B_NO_PRESENT_WATCHDOG=EXPIRED_NOOP"):
            watchdog["EXPIRED_NOOP"] += 1
        elif line.startswith("R116_EARLY_B_DISPATCH"):
            early_dispatch += 1
        elif line.startswith("R115_DEADLOCK_REGRESSION=HOPPED"):
            deadlock_hops += 1
        elif line.startswith("R116_RUN_SUMMARY"):
            summary_lines.append(line)
        elif line.startswith("R115_CONTINUOUS_RUN=COMPLETE"):
            m = re.search(r"sourceLeaksWhileClosed=(\d+) staleCallbacks=(\d+) "
                          r"duplicateMDispatch=(\d+)", line)
            if m:
                leaks = int(m.group(1))
                stale = int(m.group(2))
                duplicate_m = int(m.group(3))
        elif line.startswith("R116_LATE_M_CALLBACK") or "lateMCallbacksAfterEarlyB" in line:
            late_m += 1
        elif line.startswith("R108_SOURCE_FRAME_ACQUIRED"):
            m = re.search(r"seq=(\d+) acquiredAtMonotonicNs=(\d+)", line)
            if m:
                seq, ns = int(m.group(1)), int(m.group(2))
                if last_seq_ns and seq == last_seq_ns[0] + 1:
                    source_gaps.append((ns - last_seq_ns[1]) / 1e6)
                last_seq_ns = (seq, ns)

    ns_to_ms = 1e-6
    metrics = {
        "estimatorMs": [t["estimatorNs"] * ns_to_ms for t in timing if t.get("estimatorNs")],
        "metalfxAndGateMs": [t["metalfxNs"] * ns_to_ms for t in timing if t.get("metalfxNs")],
        "gateCpuMs": [t["gateNs"] * ns_to_ms for t in timing if t.get("gateNs")],
        "producerCompleteToMWsiMs": [t["producerCompleteToMWsiNs"] * ns_to_ms
                                     for t in timing if t.get("producerCompleteToMWsiNs")],
        "producerCompleteToMDisplayMs": [t["producerCompleteToMDisplayNs"] * ns_to_ms
                                         for t in timing if t.get("producerCompleteToMDisplayNs")],
        "mDisplayToBDispatchMs": [t["mDisplayToBDispatchNs"] * ns_to_ms
                                  for t in timing if t.get("mDisplayToBDispatchNs")],
        "bHoldMs": [t["bHoldNs"] * ns_to_ms for t in timing if t.get("bHoldNs")],
        "tupleTotalMs": [t["tupleTotalNs"] * ns_to_ms for t in timing if t.get("tupleTotalNs")],
        "bCommitToPresentHookMs": [t["bCommitToPresentHookNs"] * ns_to_ms
                                   for t in timing if t.get("bCommitToPresentHookNs")],
        "gateCpuDecisionMs": [g["gateCpuDecisionMs"] for g in gate if "gateCpuDecisionMs" in g],
        "transitionAndGateMs": [g["transitionAndGateMs"] for g in gate if "transitionAndGateMs" in g],
        "mDispatchToCallbackMs": [o["mDispatchToCallbackNs"] * ns_to_ms
                                  for o in order if o.get("mDispatchToCallbackNs")],
        "mToBPresentedDeltaMs": [o["deltaMs"] for o in order if "deltaMs" in o],
        "sourceIntervalMs": source_gaps,
    }

    report = {
        "observerLog": str(path),
        "eligibility": eligibility,
        "rejectReasons": reject_reasons,
        "gatePaths": gate_paths,
        "wsiVerdicts": verdicts,
        "assertions": {
            "sourceLeaksWhileClosed": leaks,
            "staleCallbacks": stale,
            "duplicateMDispatch": duplicate_m,
            "deadlockHops": deadlock_hops,
            "earlyBDispatch": early_dispatch,
            "lateMCallbacksAfterEarlyB": late_m,
            "watchdog": watchdog,
        },
        "ordering": {
            "samples": len(order),
            "ordered": sum(1 for o in order if o.get("ordered") == 1),
            "distinct": sum(1 for o in order if o.get("distinct") == 1),
            "coalesced": sum(1 for o in order if o.get("coalesced") == 1),
            "violations": sum(1 for o in order if o.get("ordered") == 0),
            "missingDisplay": sum(1 for o in order
                                  if o.get("mReachedDisplay") != 1 or o.get("bReachedDisplay") != 1),
        },
        "distributions": {k: dist(v) for k, v in metrics.items()},
        "runSummary": summary_lines,
    }

    print(f"log: {path}")
    print(f"eligibility: {eligibility}  rejectReasons={reject_reasons}")
    print(f"gatePaths: {gate_paths}")
    print(f"wsiVerdicts: {verdicts}")
    print(f"assertions: {report['assertions']}")
    print(f"ordering: {report['ordering']}")
    print()
    header = f"{'metric':32s} {'n':>4s} {'min':>9s} {'mean':>9s} {'median':>9s} {'p90':>9s} {'max':>9s}"
    print(header)
    print("-" * len(header))
    for name, d in report["distributions"].items():
        if not d:
            print(f"{name:32s} {'0':>4s}  (no samples)")
            continue
        print(f"{name:32s} {d['n']:4d} {d['min']:9.3f} {d['mean']:9.3f} "
              f"{d['median']:9.3f} {d['p90']:9.3f} {d['max']:9.3f}")

    if out_json:
        out_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"\nwrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
