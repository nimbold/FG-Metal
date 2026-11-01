#!/usr/bin/env python3
"""R119 live generation-throughput and contention analyser.

Reads one observer log and answers four questions:

  1. Is FG generation service below measured SOURCE arrival?
  2. Where does the capture/handoff interval go (CPU encode, GPU copy, GPU queue
     wait, CPU queue hops)?
  3. Is the live estimator cost GPU execution, command-buffer queue delay, or
     both? They are reported as separate distributions and are never summed into
     a single number called "GPU time".
  4. What did the bounded diagnostic policy remove, and did removing it change
     throughput?

METRIC DEFINITIONS -- unchanged from R118 where they overlap, so the two rounds
are comparable.

  SOURCE ARRIVAL INTERVAL
      Difference of CLOCK_MONOTONIC acquisition timestamps of two consecutive
      SOURCE frames (R108_SOURCE_FRAME_ACQUIRED). Measured, never the nominal
      16.6667 ms. Acquisition, not presentation: nextDrawable returns before the
      game has drawn. Endogenous: FG's own cost is part of what sets it.
      The arrival measured only across tuple-bounding frames is computed and
      reported as a NON-DENOMINATOR, because it is depressed by the stall being
      measured.

  FG GENERATION SERVICE (primary)
      presentHookToGateNs = gate verdict - B present hook. Includes blit encode
      and commit, the source copy, the dispatch hop, preparation, the estimator,
      MetalFX and the gate. Excludes the game's own render time for B.

  GENERATION-THREAD SERVICE (secondary)
      genThreadServiceNs = gate verdict - processCapturedFrame entry.

  END-TO-END (context only, NOT an FG cost)
      endToEndServiceNs = gate verdict - B acquisition at nextDrawable.

  R119 handoff intervals (media clock, CACurrentMediaTime domain)
      hookToBlitEncode          CPU: ticket publication -> capture entry
      blitEncode                CPU: encode of the source blit
      blitCommitToCopyGpuStart  GPU: the copy waiting to be scheduled
      copyGpuExec               GPU: the copy itself
      copyGpuEndToWake          GPU completion callback latency
      wakeToDispatch            CPU: liveness checks before handoff
      dispatchToGenQueueEntry   CPU: serial-queue wait
      genQueueEntryToGenStart   CPU: entry bookkeeping

  R119 estimator split (same clock)
      commitToGpuStartNs  queue delay. The command buffer was submitted; the GPU
                          had not begun it. NOT GPU execution.
      gpuExecNs           GPUEndTime - GPUStartTime. GPU execution.
      gpuEndToWakeNs      completion signalling latency.

Usage: analyze_r119.py <observer.log> [--json out.json] [--label name]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

KV = re.compile(r"(\w+)=(-?\d+(?:\.\d+)?)")
PAIR = re.compile(r"pair=(\d+)->(\d+)")
NS_MS = 1e6
NOMINAL_PERIOD_MS = 16.6667

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

HANDOFF_FIELDS = (
    "hookToCaptureEntryNs",
    "captureEntryToBlitCommitNs",
    "blitCommitToCopyGpuStartNs",
    "copyGpuExecNs",
    "copyGpuEndToWakeNs",
    "wakeToDispatchNs",
    "dispatchToGenQueueEntryNs",
    "genQueueEntryToGenStartNs",
    "hookToGenStartNs",
    "hookToCopyGpuStartNs",
)

ESTIMATOR_FIELDS = (
    "commitToGpuStartNs",
    "gpuExecNs",
    "gpuEndToWakeNs",
    "commitToWakeNs",
    "cpuWallNs",
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


def column(rows: list[dict[str, object]], field: str) -> list[float]:
    return [float(r[field]) for r in rows if float(r.get(field, 0)) > 0]


def parse(path: Path) -> dict[str, object]:
    acquisitions: list[tuple[int, int]] = []
    services: list[dict[str, object]] = []
    handoffs: dict[str, dict[str, float]] = {}
    estimators: dict[str, dict[str, float]] = {}
    diag_samples: list[dict[str, object]] = []
    skipped_service = 0
    eligibility = {"ACCEPTED": 0, "REJECTED": 0}
    reject_reasons: dict[str, int] = {}
    summary_lines: list[str] = []
    run_complete: list[str] = []
    watchdog: list[str] = []
    ordering: list[str] = []
    display_failures: dict[str, int] = {}
    evidence_dir: list[str] = []
    diag_policy: list[str] = []
    counters: dict[str, int] = {
        "sourceReadbackVerified": 0,
        "sourceReadbackUnverified": 0,
        "metalFxDiagnosticSaved": 0,
        "metalFxDiagnosticSkipped": 0,
        "motionExportSaved": 0,
        "handoffSkipped": 0,
        "estimatorQueueSkipped": 0,
        "estimatorQueueClockMismatch": 0,
        "presentReadyWithoutArtefact": 0,
    }

    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if raw.startswith("R108_SOURCE_FRAME_ACQUIRED"):
            m = re.search(r"seq=(\d+)\s+acquiredAtMonotonicNs=(\d+)", raw)
            if m:
                acquisitions.append((int(m.group(1)), int(m.group(2))))
        elif raw.startswith("R119_EVIDENCE_DIRECTORY"):
            evidence_dir.append(raw)
        elif raw.startswith("R119_DIAGNOSTIC_POLICY"):
            diag_policy.append(raw)
        elif raw.startswith("R119_DIAG_SAMPLE"):
            kv = nums(raw)
            diag_samples.append({
                "tuple": int(kv.get("tuple", 0)),
                "budget": int(kv.get("budget", 0)),
                "fullArtefacts": int(kv.get("fullArtefacts", 0)),
            })
        elif raw.startswith("R119_HANDOFF=SKIPPED"):
            counters["handoffSkipped"] += 1
        elif raw.startswith("R119_HANDOFF "):
            pm = PAIR.search(raw)
            if pm:
                kv = nums(raw)
                handoffs[f"{pm.group(1)}->{pm.group(2)}"] = {
                    f: kv.get(f, 0.0) / NS_MS for f in HANDOFF_FIELDS
                }
        elif raw.startswith("R119_ESTIMATOR_QUEUE=SKIPPED"):
            counters["estimatorQueueSkipped"] += 1
        elif raw.startswith("R119_ESTIMATOR_QUEUE=CLOCK_MISMATCH"):
            counters["estimatorQueueClockMismatch"] += 1
        elif raw.startswith("R119_ESTIMATOR_QUEUE "):
            pm = PAIR.search(raw)
            if pm:
                kv = nums(raw)
                record = {f: kv.get(f, 0.0) / NS_MS for f in ESTIMATOR_FIELDS}
                record["queueShareOfWall"] = kv.get("queueShareOfWall", 0.0)
                estimators[f"{pm.group(1)}->{pm.group(2)}"] = record
        elif raw.startswith("R118_GENERATION_SERVICE=SKIPPED"):
            skipped_service += 1
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
        elif raw.startswith("R108_SOURCE_DIAGNOSTIC_READBACK"):
            # Count actual files, not attempts: the R119 policy means most tuples
            # never reach this line at all, and of those that do, `verified` can
            # still be false.
            if "saved=1" in raw:
                counters["sourceReadbackVerified"] += 1
            else:
                counters["sourceReadbackUnverified"] += 1
        elif raw.startswith("R110_PRESENT_M_READY"):
            if "not-exported" in raw:
                counters["presentReadyWithoutArtefact"] += 1
        elif raw.startswith("R110_METALFX_DIAGNOSTIC="):
            if "=SKIPPED" in raw:
                counters["metalFxDiagnosticSkipped"] += 1
            elif "=FINITE" in raw:
                counters["metalFxDiagnosticSaved"] += 1
        elif raw.startswith("R108_MOTION_DIAGNOSTIC_EXPORT=SAVED"):
            counters["motionExportSaved"] += 1

    return {
        "acquisitions": acquisitions,
        "services": services,
        "handoffs": handoffs,
        "estimators": estimators,
        "diagSamples": diag_samples,
        "skippedService": skipped_service,
        "eligibility": eligibility,
        "rejectReasons": reject_reasons,
        "summaryLines": summary_lines,
        "runComplete": run_complete,
        "watchdog": watchdog,
        "ordering": ordering,
        "displayFailures": display_failures,
        "evidenceDir": evidence_dir,
        "diagPolicy": diag_policy,
        "counters": counters,
    }


def analyse(parsed: dict[str, object]) -> dict[str, object]:
    acquisitions = sorted(parsed["acquisitions"])
    services = parsed["services"]

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

    handoff_rows = [parsed["handoffs"][f"{s['pairA']}->{s['pairB']}"]
                    for s in accepted
                    if f"{s['pairA']}->{s['pairB']}" in parsed["handoffs"]]
    estimator_rows = [parsed["estimators"][f"{s['pairA']}->{s['pairB']}"]
                      for s in accepted
                      if f"{s['pairA']}->{s['pairB']}" in parsed["estimators"]]

    d_arr_all = dist(arrival_all)
    d_arr_tuple = dist(arrival_in_tuple)
    d_primary = dist(primary)

    ratio = None
    if d_primary and d_arr_all and d_arr_all["median"] > 0:
        p10 = percentile(arrival_all, 0.10)
        ratio = {
            "medianService_over_medianArrival": d_primary["median"] / d_arr_all["median"],
            "meanService_over_meanArrival": d_primary["mean"] / d_arr_all["mean"],
            "p90Service_over_p90Arrival": d_primary["p90"] / d_arr_all["p90"],
            "medianService_over_p10Arrival": (d_primary["median"] / p10) if p10 else None,
            "medianService_over_nominalPeriod": d_primary["median"] / NOMINAL_PERIOD_MS,
        }

    half = len(primary) // 2
    backlog = {
        "serviceSlopeMsPerTuple": slope_per_tuple(primary),
        "estimatorSlopeMsPerTuple": slope_per_tuple(estimator),
        "metalFxSlopeMsPerTuple": slope_per_tuple(metalfx),
        "firstHalfMedianMs": statistics.median(primary[:half]) if half else None,
        "secondHalfMedianMs": statistics.median(primary[half:]) if half else None,
        "monotonicGrowth": None,
    }
    if len(primary) >= 6:
        first, second = backlog["firstHalfMedianMs"], backlog["secondHalfMedianMs"]
        backlog["monotonicGrowth"] = bool(
            first and second and second > first * 1.15
            and (backlog["serviceSlopeMsPerTuple"] or 0) > 0
        )

    # Handoff reconciliation: the eight intervals must reconstruct hookToGenStart.
    handoff_errors = []
    for row in handoff_rows:
        parts = (row["hookToCaptureEntryNs"] + row["captureEntryToBlitCommitNs"] +
                 row["blitCommitToCopyGpuStartNs"] + row["copyGpuExecNs"] +
                 row["copyGpuEndToWakeNs"] + row["wakeToDispatchNs"] +
                 row["dispatchToGenQueueEntryNs"] + row["genQueueEntryToGenStartNs"])
        handoff_errors.append(abs(parts - row["hookToGenStartNs"]))

    # Estimator reconciliation: queue + execution + wake must reconstruct the
    # commit-to-wake interval. Any drift means a gap in the split.
    estimator_errors = []
    for row in estimator_rows:
        parts = row["commitToGpuStartNs"] + row["gpuExecNs"] + row["gpuEndToWakeNs"]
        estimator_errors.append(abs(parts - row["commitToWakeNs"]))

    sum_error = [
        abs(float(r["componentsSumNs"]) - float(r["genThreadServiceNs"]))
        for r in accepted
        if float(r["componentsSumNs"]) > 0 and float(r["genThreadServiceNs"]) > 0
    ]

    queue_share = [row["queueShareOfWall"] for row in estimator_rows
                   if row["commitToWakeNs"] > 0]

    return {
        "sourceFramesObserved": len(acquisitions),
        "generationServiceRecords": len(services),
        "skippedServiceRecords": parsed["skippedService"],
        "acceptedCount": len(accepted),
        "rejectedCount": len(rejected),
        "eligibility": parsed["eligibility"],
        "rejectReasons": parsed["rejectReasons"],
        "sourceArrivalMs": d_arr_all,
        "sourceArrivalInTupleMs": d_arr_tuple,
        "fgGenerationServiceMs": d_primary,
        "generationThreadServiceMs": dist(gen_thread),
        "endToEndContextMs": dist(e2e),
        "componentsMs": {
            "bAcquiredToGenStart": dist(copy_hop),
            "prep": dist(prep),
            "estimatorCpuWall": dist(estimator),
            "estimatorGpu": dist(estimator_gpu),
            "estimatorCpuMinusGpu": dist(cpu_minus_gpu),
            "estimatorToMetalFx": dist(diag_gap),
            "metalFx": dist(metalfx),
            "gateDecision": dist(gate_dec),
        },
        "decompositionMaxErrorMs": max(sum_error) if sum_error else None,
        "handoffMs": {f: dist([row[f] for row in handoff_rows]) for f in HANDOFF_FIELDS},
        "handoffReconciliationMaxErrorMs": max(handoff_errors) if handoff_errors else None,
        "estimatorSplitMs": {f: dist([row[f] for row in estimator_rows]) for f in ESTIMATOR_FIELDS},
        "estimatorReconciliationMaxErrorMs": max(estimator_errors) if estimator_errors else None,
        "estimatorQueueShareOfWall": dist(queue_share),
        # A queue delay of exactly zero next to a real GPU execution is the
        # signature of fgmetalMediaDeltaNs clamping a negative. It is reported
        # rather than smoothed: if this is non-zero the clock domains disagree and
        # the split must not be quoted.
        "estimatorQueueClampedToZero": sum(
            1 for row in estimator_rows
            if row["commitToGpuStartNs"] == 0 and row["gpuExecNs"] > 0),
        "throughputRatio": ratio,
        "backlog": backlog,
        "rejectedServiceMs": dist(column(rejected, PRIMARY)),
        "diagnosticSample": {
            "policy": parsed["diagPolicy"],
            "tuples": parsed["diagSamples"],
            "sampledTuples": sum(1 for item in parsed["diagSamples"] if item["fullArtefacts"]),
            "counters": parsed["counters"],
        },
        "evidenceDirectory": parsed["evidenceDir"],
        "runComplete": parsed["runComplete"],
        "runSummary": parsed["summaryLines"],
        "watchdogEvents": parsed["watchdog"],
        "presentationOrderCount": len(parsed["ordering"]),
        "displayFailures": parsed["displayFailures"],
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
                "hookToGenStartMs": parsed["handoffs"].get(
                    f"{s['pairA']}->{s['pairB']}", {}).get("hookToGenStartNs"),
                "estimatorQueueMs": parsed["estimators"].get(
                    f"{s['pairA']}->{s['pairB']}", {}).get("commitToGpuStartNs"),
                "estimatorGpuExecMs": parsed["estimators"].get(
                    f"{s['pairA']}->{s['pairB']}", {}).get("gpuExecNs"),
            }
            for s in services
        ],
    }


def show(label: str, d: dict[str, float] | None, unit: str = "ms") -> None:
    if not d:
        print(f"{label:<36} n=0")
        return
    print(
        f"{label:<36} n={d['n']:<4} min={d['min']:.3f} mean={d['mean']:.3f} "
        f"median={d['median']:.3f} p90={d['p90']:.3f} max={d['max']:.3f} {unit}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    parser.add_argument("--json")
    parser.add_argument("--label", default="r119")
    args = parser.parse_args()

    path = Path(args.log)
    parsed = parse(path)
    result = analyse(parsed)
    result["schema"] = "fgmetal-r119-generation-throughput-v2"
    result["observerLog"] = str(path)
    result["label"] = args.label
    result["primaryMetric"] = PRIMARY

    print(f"R119_LABEL={args.label} SOURCE_FRAMES={result['sourceFramesObserved']} "
          f"TUPLES={result['generationServiceRecords']} ACCEPTED={result['acceptedCount']} "
          f"REJECTED={result['rejectedCount']} SKIPPED={result['skippedServiceRecords']}")
    print(f"R119_DISPLAY_FAILURES {result['displayFailures']}")
    show("SOURCE_ARRIVAL", result["sourceArrivalMs"])
    show("SOURCE_ARRIVAL_IN_TUPLE (not a denominator)", result["sourceArrivalInTupleMs"])
    show("FG_GENERATION_SERVICE (primary)", result["fgGenerationServiceMs"])
    show("GENERATION_THREAD_SERVICE", result["generationThreadServiceMs"])
    show("END_TO_END (context)", result["endToEndContextMs"])
    for key, value in result["componentsMs"].items():
        show(f"  {key}", value)
    if result["decompositionMaxErrorMs"] is not None:
        exact = result["decompositionMaxErrorMs"] < 0.0005
        print(f"R119_DECOMPOSITION maxErrorMs={result['decompositionMaxErrorMs']:.6f} "
              f"({'EXACT' if exact else 'MISMATCH'})")

    print("--- R119 handoff decomposition (media clock) ---")
    for key, value in result["handoffMs"].items():
        show(f"  {key}", value)
    if result["handoffReconciliationMaxErrorMs"] is not None:
        exact = result["handoffReconciliationMaxErrorMs"] < 0.0005
        print(f"R119_HANDOFF_RECONCILIATION maxErrorMs="
              f"{result['handoffReconciliationMaxErrorMs']:.6f} "
              f"({'EXACT' if exact else 'MISMATCH'})")

    print("--- R119 estimator queue vs GPU execution ---")
    for key, value in result["estimatorSplitMs"].items():
        show(f"  {key}", value)
    if result["estimatorReconciliationMaxErrorMs"] is not None:
        exact = result["estimatorReconciliationMaxErrorMs"] < 0.0005
        print(f"R119_ESTIMATOR_RECONCILIATION maxErrorMs="
              f"{result['estimatorReconciliationMaxErrorMs']:.6f} "
              f"({'EXACT' if exact else 'MISMATCH'})")
    show("  queue share of commit->wake", result["estimatorQueueShareOfWall"], "ratio")
    queue_rows = result["estimatorSplitMs"]["commitToGpuStartNs"]
    print(f"R119_ESTIMATOR_QUEUE_CLAMPED_TO_ZERO="
          f"{result['estimatorQueueClampedToZero']}/{queue_rows['n'] if queue_rows else 0}")

    if result["throughputRatio"]:
        r = result["throughputRatio"]
        print(
            "R119_THROUGHPUT_RATIO "
            f"median_over_median={r['medianService_over_medianArrival']:.3f} "
            f"mean_over_mean={r['meanService_over_meanArrival']:.3f} "
            f"p90_over_p90={r['p90Service_over_p90Arrival']:.3f} "
            f"median_over_p10={r['medianService_over_p10Arrival']:.3f} "
            f"median_over_nominal={r['medianService_over_nominalPeriod']:.3f}"
        )
    b = result["backlog"]
    if b["serviceSlopeMsPerTuple"] is not None:
        print(
            "R119_BACKLOG "
            f"serviceSlopeMsPerTuple={b['serviceSlopeMsPerTuple']:+.4f} "
            f"estimatorSlopeMsPerTuple={b['estimatorSlopeMsPerTuple']:+.4f} "
            f"metalFxSlopeMsPerTuple={b['metalFxSlopeMsPerTuple']:+.4f} "
            f"firstHalfMedian={b['firstHalfMedianMs']:.3f} "
            f"secondHalfMedian={b['secondHalfMedianMs']:.3f} "
            f"monotonicGrowth={b['monotonicGrowth']}"
        )
    sample = result["diagnosticSample"]
    print(f"R119_DIAGNOSTIC_SAMPLE sampled={sample['sampledTuples']}/"
          f"{len(sample['tuples'])} counters={sample['counters']}")
    if result["evidenceDirectory"]:
        print("R119_EVIDENCE " + result["evidenceDirectory"][0])

    if result["fgGenerationServiceMs"] and result["sourceArrivalMs"]:
        verdict = ("ABOVE" if result["fgGenerationServiceMs"]["median"]
                   >= result["sourceArrivalMs"]["median"] else "BELOW")
        print(f"R119_SERVICE_VS_ARRIVAL medianService{verdict} medianArrival")
    print(f"R119_REJECT_REASONS {result['rejectReasons']}")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"R119_JSON={args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
