#!/usr/bin/env python3
"""R118 live generation-throughput analyser.

Answers one question from one observer log: can the generation path produce an
INTERNAL frame faster than SOURCE pairs arrive?

METRIC DEFINITIONS -- stated once and applied everywhere. These were chosen to be
conservative and to avoid charging the game's own work to FG, or FG's work to
the game.

  SOURCE ARRIVAL INTERVAL
      Difference between the CLOCK_MONOTONIC acquisition timestamps of two
      consecutive SOURCE frames (R108_SOURCE_FRAME_ACQUIRED). This is measured
      arrival, never the display's nominal 16.6667 ms.
      Caveat that must travel with the number: acquisition happens at
      nextDrawable, and nextDrawable returns before the game has drawn the
      frame, so this is "when the game started frame N", not "when frame N was
      finished". It is also endogenous -- FG's own per-frame cost is part of
      what sets it.

  FG GENERATION SERVICE (primary)
      presentHookToGateNs = gate verdict - B present hook.
      The B present hook is the first moment FG touches the pair: the source
      blit is encoded there. The gate verdict is the last moment FG needs it.
      In between: blit encode and commit, source copy on the GPU, the dispatch
      hop to the capture queue, preparation, the estimator, the motion
      diagnostic export, the MetalFX transition, and the gate decision.
      This excludes the game's own render time for B, which is why it is
      preferred over a nextDrawable-anchored window.

  GENERATION-THREAD SERVICE (secondary)
      genThreadServiceNs = gate verdict - processCapturedFrame entry.
      The same window minus the blit and the dispatch hop. Reported so the
      copy/hop component is visible rather than hidden inside the total.

  END-TO-END (context only, NOT an FG cost)
      endToEndServiceNs = gate verdict - B acquisition at nextDrawable.
      Contains the game's own render time for B. Reported only to show how much
      of the wall clock between drawable acquisition and gate verdict is not FG.

  THROUGHPUT RATIO
      service / arrival. Below 1.0 is sustainable; at or above 1.0 a look-ahead
      queue would accumulate backlog at any depth.

Usage: analyze_r118.py <observer.log> [--json out.json]
"""

from __future__ import annotations

import json
import re
import statistics
import sys
from pathlib import Path

KV = re.compile(r"(\w+)=(-?\d+(?:\.\d+)?)")
PAIR = re.compile(r"pair=(\d+)->(\d+)")

NS_MS = 1e6

# Every nanosecond field the observer emits on R118_GENERATION_SERVICE.
SERVICE_FIELDS = (
    "presentHookToGateNs",
    "bAcquiredToGenStartNs",
    "prepNs",
    "estimatorNs",
    "estimatorToMetalFxNs",
    "metalFxNs",
    "gateDecisionNs",
    "genThreadServiceNs",
    "endToEndServiceNs",
    "estimatorGpuNs",
    "estimatorCpuMinusGpuNs",
    "componentsSumNs",
)

PRIMARY = "presentHookToGateNs"


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
        "stdev": statistics.pstdev(v) if n > 1 else 0.0,
    }


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * fraction))]


def slope_per_tuple(values: list[float]) -> float | None:
    """Least-squares slope of value vs tuple index, in ms per tuple."""
    n = len(values)
    if n < 3:
        return None
    xs = list(range(n))
    mx = sum(xs) / n
    my = sum(values) / n
    denom = sum((x - mx) ** 2 for x in xs)
    if denom == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, values)) / denom


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: analyze_r118.py <observer.log> [--json out.json]")
    path = Path(sys.argv[1])
    out_json = None
    if "--json" in sys.argv:
        out_json = Path(sys.argv[sys.argv.index("--json") + 1])

    acquisitions: list[tuple[int, int]] = []
    services: list[dict[str, object]] = []
    skipped = 0
    eligibility = {"ACCEPTED": 0, "REJECTED": 0}
    reject_reasons: dict[str, int] = {}
    gpu_inputs: dict[int, float] = {}
    summary_lines: list[str] = []
    run_complete: list[str] = []
    watchdog: list[str] = []
    ordering: list[str] = []
    display_failures: dict[str, int] = {}

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("R108_SOURCE_FRAME_ACQUIRED"):
            m = re.search(r"seq=(\d+)\s+acquiredAtMonotonicNs=(\d+)", raw)
            if m:
                acquisitions.append((int(m.group(1)), int(m.group(2))))
        elif raw.startswith("R118_GENERATION_SERVICE=SKIPPED"):
            skipped += 1
        elif raw.startswith("R118_GENERATION_SERVICE "):
            kv = nums(raw)
            pm = PAIR.search(raw)
            if not pm:
                continue
            om = re.search(r"outcome=(\w+)", raw)
            gp = re.search(r"gatePath=([\w-]+)", raw)
            record: dict[str, object] = {
                "pairA": int(pm.group(1)),
                "pairB": int(pm.group(2)),
                "outcome": om.group(1) if om else "UNKNOWN",
                "gatePath": gp.group(1) if gp else "unknown",
            }
            for field in SERVICE_FIELDS:
                record[field] = kv.get(field, 0.0) / NS_MS
            services.append(record)
        elif raw.startswith("R110_FAST_PRESENT_ELIGIBILITY="):
            if "=ACCEPTED" in raw:
                eligibility["ACCEPTED"] += 1
            else:
                eligibility["REJECTED"] += 1
                rm = re.search(r"reason=(\S+)", raw)
                if rm:
                    reject_reasons[rm.group(1)] = reject_reasons.get(rm.group(1), 0) + 1
        elif raw.startswith("R108_GPU_INPUTS_COMPLETE"):
            pm = PAIR.search(raw)
            gm = re.search(r"gpuMs=([\d.]+)", raw)
            if pm and gm:
                gpu_inputs[int(pm.group(2))] = float(gm.group(1))
        elif raw.startswith("R116_RUN_SUMMARY"):
            summary_lines.append(raw)
        elif raw.startswith("R115_CONTINUOUS_RUN=COMPLETE"):
            run_complete.append(raw)
        elif "WATCHDOG" in raw and "NOOP" not in raw:
            watchdog.append(raw)
        elif raw.startswith("R116_PRESENTATION_ORDER"):
            ordering.append(raw)
        elif raw.startswith("R102_INTERNAL_COPY=FAILED"):
            display_failures["internalCopyFailed"] = display_failures.get("internalCopyFailed", 0) + 1
        elif raw.startswith("INTERNAL_PRESENT_PREFLIGHT=FAILED"):
            display_failures["internalPreflightFailed"] = display_failures.get("internalPreflightFailed", 0) + 1

    # ---- source arrival -----------------------------------------------------
    acquisitions.sort()
    arrival_all: list[float] = []
    for (s0, t0), (s1, t1) in zip(acquisitions, acquisitions[1:]):
        if s1 == s0 + 1 and t1 > t0:
            arrival_all.append((t1 - t0) / NS_MS)

    by_seq = dict(acquisitions)
    arrival_in_tuple: list[float] = []
    for record in services:
        a = by_seq.get(int(record["pairA"]))
        b = by_seq.get(int(record["pairB"]))
        if a and b and b > a:
            arrival_in_tuple.append((b - a) / NS_MS)

    accepted = [s for s in services if s["outcome"] == "ACCEPTED"]
    rejected = [s for s in services if s["outcome"] != "ACCEPTED"]

    def column(rows: list[dict[str, object]], field: str) -> list[float]:
        return [float(r[field]) for r in rows if float(r[field]) > 0]

    primary = column(accepted, PRIMARY)
    gen_thread = column(accepted, "genThreadServiceNs")
    e2e = column(accepted, "endToEndServiceNs")
    estimator = column(accepted, "estimatorNs")
    estimator_gpu = column(accepted, "estimatorGpuNs")
    cpu_minus_gpu = column(accepted, "estimatorCpuMinusGpuNs")
    metalfx = column(accepted, "metalFxNs")
    prep = column(accepted, "prepNs")
    copy_hop = column(accepted, "bAcquiredToGenStartNs")
    diag_gap = column(accepted, "estimatorToMetalFxNs")
    gate_dec = column(accepted, "gateDecisionNs")
    component_sum = column(accepted, "componentsSumNs")

    d_primary = dist(primary)
    d_gen = dist(gen_thread)
    d_e2e = dist(e2e)
    d_arr_all = dist(arrival_all)
    d_arr_tuple = dist(arrival_in_tuple)

    # Decomposition self-check: components must reconstruct genThreadService.
    sum_error = [
        abs(float(r["componentsSumNs"]) - float(r["genThreadServiceNs"]))
        for r in accepted
        if float(r["componentsSumNs"]) > 0 and float(r["genThreadServiceNs"]) > 0
    ]
    max_sum_error = max(sum_error) if sum_error else None

    ratio = None
    if d_primary and d_arr_all and d_arr_all["median"] > 0:
        p10 = percentile(arrival_all, 0.10)
        ratio = {
            "medianService_over_medianArrival": d_primary["median"] / d_arr_all["median"],
            "meanService_over_meanArrival": d_primary["mean"] / d_arr_all["mean"],
            "p90Service_over_p90Arrival": d_primary["p90"] / d_arr_all["p90"],
            "medianService_over_p10Arrival": (d_primary["median"] / p10) if p10 else None,
            "medianService_over_nominalPeriod": d_primary["median"] / 16.6667,
        }

    # ---- backlog ------------------------------------------------------------
    half = len(primary) // 2
    backlog = {
        "serviceSlopeMsPerTuple": slope_per_tuple(primary),
        "estimatorSlopeMsPerTuple": slope_per_tuple(estimator),
        "metalFxSlopeMsPerTuple": slope_per_tuple(metalfx),
        "firstHalfMedianMs": statistics.median(primary[:half]) if half else None,
        "secondHalfMedianMs": statistics.median(primary[half:]) if half else None,
        "firstHalfEstimatorMedianMs": statistics.median(estimator[:half]) if half else None,
        "secondHalfEstimatorMedianMs": statistics.median(estimator[half:]) if half else None,
        "monotonicGrowth": None,
    }
    if len(primary) >= 6:
        first = backlog["firstHalfMedianMs"]
        second = backlog["secondHalfMedianMs"]
        backlog["monotonicGrowth"] = bool(
            first and second and second > first * 1.15
            and (backlog["serviceSlopeMsPerTuple"] or 0) > 0
        )

    result = {
        "schema": "fgmetal-r118-generation-throughput-v1",
        "observerLog": str(path),
        "primaryMetric": PRIMARY,
        "sourceFramesObserved": len(acquisitions),
        "generationServiceRecords": len(services),
        "skippedServiceRecords": skipped,
        "eligibility": eligibility,
        "rejectReasons": reject_reasons,
        "displayFailures": display_failures,
        "sourceArrivalMs": d_arr_all,
        "sourceArrivalInTupleMs": d_arr_tuple,
        "effectiveSourceRateHz": (1000.0 / d_arr_all["mean"]) if d_arr_all else None,
        "fgGenerationServiceMs": d_primary,
        "generationThreadServiceMs": d_gen,
        "endToEndContextMs": d_e2e,
        "componentsMs": {
            "bAcquiredToGenStart": dist(copy_hop),
            "prep": dist(prep),
            "estimatorCpuWall": dist(estimator),
            "estimatorGpu": dist(estimator_gpu),
            "estimatorCpuMinusGpu": dist(cpu_minus_gpu),
            "estimatorToMetalFx": dist(diag_gap),
            "metalFx": dist(metalfx),
            "gateDecision": dist(gate_dec),
            "componentSum": dist(component_sum),
        },
        "decompositionMaxErrorMs": max_sum_error,
        "throughputRatio": ratio,
        "backlog": backlog,
        "acceptedCount": len(accepted),
        "rejectedCount": len(rejected),
        "rejectedServiceMs": dist(column(rejected, PRIMARY)),
        "runComplete": run_complete,
        "runSummary": summary_lines,
        "watchdogEvents": watchdog,
        "presentationOrderCount": len(ordering),
        "perTuple": [
            {
                "pair": f"{s['pairA']}->{s['pairB']}",
                "outcome": s["outcome"],
                "gatePath": s["gatePath"],
                "fgServiceMs": s[PRIMARY],
                "genThreadMs": s["genThreadServiceNs"],
                "endToEndMs": s["endToEndServiceNs"],
                "estimatorMs": s["estimatorNs"],
                "estimatorGpuMs": s["estimatorGpuNs"],
                "metalFxMs": s["metalFxNs"],
                "copyHopMs": s["bAcquiredToGenStartNs"],
                "prepMs": s["prepNs"],
            }
            for s in services
        ],
    }

    def show(label: str, d: dict[str, float] | None, unit: str = "ms") -> None:
        if not d:
            print(f"{label:<34} n=0")
            return
        print(
            f"{label:<34} n={d['n']:<4} min={d['min']:.3f} mean={d['mean']:.3f} "
            f"median={d['median']:.3f} p90={d['p90']:.3f} max={d['max']:.3f} {unit}"
        )

    print(f"R118_SOURCE_FRAMES={len(acquisitions)} TUPLES={len(services)} "
          f"ACCEPTED={len(accepted)} REJECTED={len(rejected)} SKIPPED={skipped}")
    print(f"R118_DISPLAY_FAILURES {display_failures}")
    show("SOURCE_ARRIVAL", d_arr_all)
    show("SOURCE_ARRIVAL_IN_TUPLE", d_arr_tuple)
    show("FG_GENERATION_SERVICE (primary)", d_primary)
    show("GENERATION_THREAD_SERVICE", d_gen)
    show("END_TO_END (context)", d_e2e)
    show("  copy+dispatch hop", result["componentsMs"]["bAcquiredToGenStart"])
    show("  prep", result["componentsMs"]["prep"])
    show("  estimator (CPU wall)", result["componentsMs"]["estimatorCpuWall"])
    show("  estimator (GPU)", result["componentsMs"]["estimatorGpu"])
    show("  estimator cpu-gpu", result["componentsMs"]["estimatorCpuMinusGpu"])
    show("  estimator->MetalFX", result["componentsMs"]["estimatorToMetalFx"])
    show("  MetalFX", result["componentsMs"]["metalFx"])
    show("  gate decision", result["componentsMs"]["gateDecision"])
    if max_sum_error is not None:
        print(f"R118_DECOMPOSITION maxErrorMs={max_sum_error:.6f} "
              f"({'EXACT' if max_sum_error < 0.0005 else 'MISMATCH'})")
    if ratio:
        print(
            "R118_THROUGHPUT_RATIO "
            f"median_over_median={ratio['medianService_over_medianArrival']:.3f} "
            f"mean_over_mean={ratio['meanService_over_meanArrival']:.3f} "
            f"p90_over_p90={ratio['p90Service_over_p90Arrival']:.3f} "
            f"median_over_p10={ratio['medianService_over_p10Arrival']:.3f} "
            f"median_over_nominal={ratio['medianService_over_nominalPeriod']:.3f}"
        )
    if backlog["serviceSlopeMsPerTuple"] is not None:
        print(
            "R118_BACKLOG "
            f"serviceSlopeMsPerTuple={backlog['serviceSlopeMsPerTuple']:+.4f} "
            f"estimatorSlopeMsPerTuple={backlog['estimatorSlopeMsPerTuple']:+.4f} "
            f"metalFxSlopeMsPerTuple={backlog['metalFxSlopeMsPerTuple']:+.4f} "
            f"firstHalfMedian={backlog['firstHalfMedianMs']:.3f} "
            f"secondHalfMedian={backlog['secondHalfMedianMs']:.3f} "
            f"monotonicGrowth={backlog['monotonicGrowth']}"
        )
    if d_primary and d_arr_all:
        verdict = "ABOVE" if d_primary["median"] >= d_arr_all["median"] else "BELOW"
        print(f"R118_SERVICE_VS_ARRIVAL medianService{verdict} medianArrival")
    print(f"R118_REJECT_REASONS {reject_reasons}")

    if out_json:
        out_json.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"R118_JSON={out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
