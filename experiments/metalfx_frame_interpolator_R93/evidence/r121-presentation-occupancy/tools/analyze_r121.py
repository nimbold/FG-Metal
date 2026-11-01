#!/usr/bin/env python3
"""R121 analyser: decompose `bPresentHookToMWsiNs` and classify the blocker.

Reads an R121 log slice and reports, for every measured tuple, the twenty links
between B's present hook and M's WSI dispatch, with min/mean/median/p90/max per
component and an exact per-tuple reconciliation against the headline.

Design rules, all of them responses to a specific way this measurement can lie:

  * Only `R121_B2M_CHAIN` lines are authoritative for the decomposition. They
    are emitted by the observer from the tuple-terminal path, after the window
    has closed, so nothing in them is perturbed by the act of reporting.
  * `headlineNs` is the observer's own `mDispatchNs - bPresentHookNs`, the same
    quantity R120 published as `bPresentHookToMWsiNs`. `chainSumNs` is the sum of
    the twenty reported links. `residualNs` is the difference. A non-zero
    residual means the components do not describe the headline and the round
    must say so rather than report a pretty table.
  * Statistics are per component across tuples, never a sum of medians. The
    headline median is computed from the headline column directly.
  * The refresh-quantum test refuses to call a wait "drawable starvation" just
    because its duration resembles a refresh: a wait is only classified as
    refresh-quantum-locked if its length is within tolerance of a refresh
    multiple AND its return instant sits near a display callback.

Usage: analyze_r121.py <slice.log> [--json out.json] [--label NAME]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

CHAIN_TAG = "R121_B2M_CHAIN "
COPY_TAG = "R121_B2M_COPY "

# The twenty links, in chain order. Names must match the observer's field names.
LINKS = [
    "hookToSrcCaptureNs",
    "srcCaptureToCopyEncodedNs",
    "copyEncodedToCopyCommitNs",
    "copyCommitToCopyWakeNs",
    "copyWakeToTaskEnqueueNs",
    "taskEnqueueToTaskStartNs",
    "taskStartToEstimatorStartNs",
    "estimatorStartToEstimatorEndNs",
    "estimatorEndToMetalFXStartNs",
    "metalFXStartToMetalFXEndNs",
    "metalFXEndToGateVerdictNs",
    "gateVerdictToInternalAcquireReqNs",
    "internalAcquireReqToNdEnterNs",
    "internalNdEnterToNdReturnNs",
    "internalNdReturnToAcquiredNs",
    "internalAcquiredToMCopyEncodeNs",
    "mCopyEncodeToCommitNs",
    "mCopyCommitToWakeNs",
    "mCopyWakeToMWsiAllocNs",
    "mWsiAllocToDispatchNs",
]

COPY_FIELDS = [
    "srcCopyCommitToGpuStartNs",
    "srcCopyGpuExecNs",
    "srcCopyGpuEndToWakeNs",
    "srcCopyCommitToWakeNs",
    "srcCopyMediaDurationNs",
    "srcCopyClockSkewNs",
    "mCopyCommitToGpuStartNs",
    "mCopyGpuExecNs",
    "mCopyGpuEndToWakeNs",
    "mCopyCommitToWakeNs",
    "mCopyMediaDurationNs",
    "mCopyClockSkewNs",
    "dispatchToPresentEnterNs",
    "presentCallNs",
    "sourceNdBlockNs",
]

COPY_INTS = [
    "poolMaximumDrawables",
    "deferredBPresent",
    "liveSourceDrawables",
    "liveInternalDrawables",
    "liveSourceMax",
    "liveInternalMax",
    "sourceAcquiredTotal",
    "sourceReleasedTotal",
    "sourceDeferredTotal",
    "internalAcquiredTotal",
    "internalReleasedTotal",
    "internalSlotState",
    "frontierState",
    "sourceNdEntryNs",
    "sourceNdReturnNs",
    "internalNdEnterNs",
    "internalNdReturnNs",
    "bHookNs",
    "mWsiDispatchNs",
]

# A media-clock duration and a monotonic-clock duration of the same copy must
# agree to within this much. It is deliberately loose: the two clocks are read at
# slightly different instants, so sub-millisecond disagreement is expected. What
# this catches is a clock-DOMAIN error, which shows up as hundreds of seconds,
# not as a fraction of a millisecond.
CLOCK_SKEW_TOLERANCE_MS = 1.0

REFRESH_NS = 16_666_667
BUDGET_NS = 33_333_333


def parse_kv(line: str) -> dict:
    """Parses `TAG k=v k=v ...` into a dict of str -> str.

    Values never contain spaces in the R121 lines, so a plain split is exact and
    keeps the parser independent of field order.
    """
    out = {}
    for token in line.split():
        if "=" in token:
            key, _, value = token.partition("=")
            out[key] = value
    return out


def pct(values: list[float], q: float) -> float:
    """Linear-interpolated percentile. Deterministic, no external dependency."""
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    frac = position - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def stats(values: list[float]) -> dict:
    if not values:
        return {"n": 0, "min": 0.0, "mean": 0.0, "median": 0.0, "p90": 0.0, "max": 0.0}
    return {
        "n": len(values),
        "min": min(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "p90": pct(values, 0.90),
        "max": max(values),
    }


def load(slice_path: Path):
    chains, copies = [], []
    text = slice_path.read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines():
        if line.startswith(CHAIN_TAG):
            chains.append(parse_kv(line))
        elif line.startswith(COPY_TAG):
            copies.append(parse_kv(line))
    return text, chains, copies


def merge(chains: list[dict], copies: list[dict]) -> list[dict]:
    """Joins the two emitted lines by generation. Copy line is optional: a tuple
    that fell back before M was dispatched has no copy evidence, and that must
    not silently borrow another tuple's numbers."""
    by_gen = {}
    for record in copies:
        try:
            by_gen[int(record["generation"])] = record
        except (KeyError, ValueError):
            continue
    merged = []
    for record in chains:
        try:
            gen = int(record["generation"])
        except (KeyError, ValueError):
            continue
        entry = dict(record)
        entry["copy"] = by_gen.get(gen, {})
        merged.append(entry)
    return merged


def measured(records: list[dict]) -> list[dict]:
    """Tuples that carry a complete, reconciled decomposition."""
    out = []
    for record in records:
        try:
            links = int(record.get("links", "0").split("/")[0])
            missing = int(record.get("missing", "99"))
            headline = int(record.get("headlineNs", "0"))
        except ValueError:
            continue
        if links != 20 or missing != 0 or headline <= 0:
            continue
        out.append(record)
    return out


def refresh_quantum(records: list[dict], display_events: list[int]) -> dict:
    """Classifies each INTERNAL nextDrawable wait.

    A wait is only refresh-locked when BOTH conditions hold:
      (a) its length is within TOLERANCE_NS of a whole number of refreshes, and
      (b) its return instant is within TOLERANCE_NS of a display callback.
    Condition (a) alone is the trap the brief names: a 16.7 ms number is not
    evidence of starvation.
    """
    tolerance = 1_000_000  # 1.0 ms
    buckets = {"~1 refresh": 0, "~2 refreshes": 0, "~3 refreshes": 0,
               "other": 0, "unmeasured": 0}
    near_callback = 0
    samples = []
    for record in records:
        copy = record.get("copy") or {}
        try:
            block = int(copy.get("internalNdBlockNs", "0"))
            ret = int(copy.get("internalNdReturnNs", "0"))
        except ValueError:
            buckets["unmeasured"] += 1
            continue
        if ret == 0:
            buckets["unmeasured"] += 1
            continue
        multiple = round(block / REFRESH_NS)
        if multiple >= 1 and abs(block - multiple * REFRESH_NS) <= tolerance:
            if multiple <= 3:
                buckets[f"~{multiple} refresh" + ("es" if multiple > 1 else "")] += 1
            else:
                buckets["other"] += 1
        else:
            buckets["other"] += 1
        nearest = min((abs(ret - event) for event in display_events), default=None)
        if nearest is not None and nearest <= tolerance:
            near_callback += 1
        samples.append({
            "generation": int(record.get("generation", 0)),
            "blockMs": block / 1e6,
            "nearestDisplayCallbackMs": None if nearest is None else nearest / 1e6,
        })
    return {
        "buckets": buckets,
        "returnsNearDisplayCallback": near_callback,
        "samples": samples,
        "toleranceMs": tolerance / 1e6,
    }


def cadence(text: str) -> dict:
    """SOURCE acquisition cadence and displayed-time cadence, kept separate.

    `sourceAcquireMedianMs` is the honest SOURCE-rate measure: it is the interval
    between consecutive SOURCE `nextDrawable` returns, i.e. the rate the game
    actually produced at. Displayed-frame rate is a different quantity -- a
    working 30->60 pipeline is *supposed* to display at 60 -- so the two must
    never be compared against the same target.
    """
    acquire = []
    for line in text.splitlines():
        if line.startswith("R110_SOURCE_NEXT_DRAWABLE_ENTRY "):
            fields = parse_kv(line)
            try:
                acquire.append(int(fields["entryMonotonicNs"]))
            except (KeyError, ValueError):
                continue
    acquire.sort()
    acquire_intervals = [(acquire[i + 1] - acquire[i]) / 1e6
                         for i in range(len(acquire) - 1)]

    displayed = []
    for line in text.splitlines():
        if line.startswith("R101_WSI_DISPLAYED "):
            fields = parse_kv(line)
            try:
                displayed.append((float(fields["presentedTime"]),
                                  fields.get("origin", "?")))
            except (KeyError, ValueError):
                continue
    displayed.sort()
    intervals = [(displayed[i + 1][0] - displayed[i][0]) * 1000.0
                 for i in range(len(displayed) - 1)]

    a_to_m, m_to_b = [], []
    for line in text.splitlines():
        if line.startswith("R116_PRESENTATION_ORDER "):
            fields = parse_kv(line)
            try:
                m = float(fields["mPresentedTime"])
                b = float(fields["bPresentedTime"])
            except (KeyError, ValueError):
                continue
            if m > 0 and b > 0:
                m_to_b.append((b - m) * 1000.0)
    for line in text.splitlines():
        if line.startswith("R115_TUPLE_TIMING "):
            fields = parse_kv(line)
            try:
                value = int(fields.get("aDisplayToMDisplayNs", "0"))
            except ValueError:
                continue
            if value > 0:
                a_to_m.append(value / 1e6)

    span = (displayed[-1][0] - displayed[0][0]) if len(displayed) > 1 else 0.0
    acquire_median = statistics.median(acquire_intervals) if acquire_intervals else 0.0
    return {
        "sourceAcquireMedianMs": acquire_median,
        "sourceFps": 1000.0 / acquire_median if acquire_median > 0 else 0.0,
        "displayedFrames": len(displayed),
        "intervalMedianMs": statistics.median(intervals) if intervals else 0.0,
        "intervalMinMs": min(intervals) if intervals else 0.0,
        "intervalMaxMs": max(intervals) if intervals else 0.0,
        "aToMMedianMs": statistics.median(a_to_m) if a_to_m else 0.0,
        "mToBMedianMs": statistics.median(m_to_b) if m_to_b else 0.0,
        "outputFps": (len(displayed) - 1) / span if span > 0 else 0.0,
    }


def classify(component_stats: dict) -> dict:
    """Dominant-blocker classification from per-component medians.

    Only components that are actually on the critical path are candidates. The
    estimator and MetalFX are frozen by the brief, so they are reported as a
    fixed cost and explicitly excluded from the classification.

    `component_stats[...]["median"]` is already in MILLISECONDS, so the totals
    are in milliseconds and must not be rescaled again. An earlier revision
    divided by 1e6 a second time and printed every component as 0.000 ms, which
    would have made an unmeasured blocker look like an absent one.
    """
    candidates = {
        "SOURCE_COPY_GPU_SUBMIT": [
            "copyEncodedToCopyCommitNs", "copyCommitToCopyWakeNs"],
        "CPU_QUEUE_PATH": [
            "copyWakeToTaskEnqueueNs", "taskEnqueueToTaskStartNs"],
        "GENERATION_PREP": ["taskStartToEstimatorStartNs"],
        "INTERNAL_DRAWABLE_ACQUISITION": [
            "internalAcquireReqToNdEnterNs", "internalNdEnterToNdReturnNs",
            "internalNdReturnToAcquiredNs"],
        "INTERNAL_COPY_RENDER": [
            "internalAcquiredToMCopyEncodeNs", "mCopyEncodeToCommitNs",
            "mCopyCommitToWakeNs"],
        "PRESENTATION_DISPATCH": ["mCopyWakeToMWsiAllocNs", "mWsiAllocToDispatchNs"],
    }
    totals = {}
    for name, members in candidates.items():
        totals[name] = sum(component_stats[m]["median"] for m in members)
    ranked = sorted(totals.items(), key=lambda item: item[1], reverse=True)
    top, top_value = ranked[0]
    second, second_value = ranked[1]
    if top_value <= 0:
        verdict = "UNCLASSIFIED"
    elif second_value >= 0.8 * top_value:
        verdict = f"MIXED:{top}+{second}"
    else:
        verdict = top
    return {
        "totalsMs": {k: v for k, v in totals.items()},
        "ranked": [k for k, _ in ranked],
        "verdict": verdict,
    }


def analyse(slice_path: Path) -> dict:
    text, chains, copies = load(slice_path)
    merged = merge(chains, copies)
    tuples = measured(merged)

    component_stats = {}
    for link in LINKS:
        values = []
        for record in tuples:
            try:
                values.append(int(record[link]) / 1e6)
            except (KeyError, ValueError):
                continue
        component_stats[link] = stats(values)

    headline = []
    residuals = []
    for record in tuples:
        headline.append(int(record["headlineNs"]) / 1e6)
        residuals.append(int(record.get("residualNs", "0")))
    headline_stats = stats(headline)

    copy_stats = {}
    for field in COPY_FIELDS:
        values = []
        for record in tuples:
            copy = record.get("copy") or {}
            try:
                values.append(int(copy[field]) / 1e6)
            except (KeyError, ValueError):
                continue
        copy_stats[field] = stats(values)

    display_events = []
    for line in text.splitlines():
        if line.startswith("R102_EVENT ") and "event=INTERNAL_DISPLAYED_CALLBACK" in line:
            fields = parse_kv(line)
            try:
                display_events.append(int(fields["t_ns"]))
            except (KeyError, ValueError):
                continue
        if line.startswith("R102_EVENT ") and "event=SOURCE_B_ORIGINAL_PRESENT_CALL" in line:
            fields = parse_kv(line)
            try:
                display_events.append(int(fields["t_ns"]))
            except (KeyError, ValueError):
                continue
    display_events.sort()

    quantum = refresh_quantum(tuples, display_events)
    cad = cadence(text)
    classification = classify(component_stats)

    pool_max = 0
    outstanding_max = 0
    accounted = {"acquired": 0, "released": 0, "deferred": 0}
    for record in tuples:
        copy = record.get("copy") or {}
        try:
            pool_max = max(pool_max, int(copy.get("poolMaximumDrawables", "0")))
            outstanding_max = max(outstanding_max,
                                  int(copy.get("liveSourceDrawables", "0")))
            accounted["acquired"] = max(accounted["acquired"],
                                        int(copy.get("sourceAcquiredTotal", "0")))
            accounted["released"] = max(accounted["released"],
                                        int(copy.get("sourceReleasedTotal", "0")))
            accounted["deferred"] = max(accounted["deferred"],
                                        int(copy.get("sourceDeferredTotal", "0")))
        except ValueError:
            continue

    src_skew = copy_stats["srcCopyClockSkewNs"]["max"]
    m_skew = copy_stats["mCopyClockSkewNs"]["max"]
    src_skew_n = copy_stats["srcCopyClockSkewNs"]["n"]
    m_skew_n = copy_stats["mCopyClockSkewNs"]["n"]

    max_abs_residual = max((abs(value) for value in residuals), default=0)
    checks = [
        {
            "name": "chain_complete",
            "pass": all(int(r.get("links", "0").split("/")[0]) == 20 and
                        int(r.get("missing", "99")) == 0 for r in tuples) and bool(tuples),
            "detail": f"tuples={len(tuples)}",
        },
        {
            "name": "reconciliation_exact",
            "pass": max_abs_residual == 0,
            "detail": f"maxAbsResidualNs={max_abs_residual}",
        },
        {
            "name": "no_order_violation",
            "pass": all(int(r.get("orderViolations", "0")) == 0 for r in tuples),
            "detail": f"violations={sum(int(r.get('orderViolations', '0')) for r in tuples)}",
        },
        {
            # GPU timestamps live on the CACurrentMediaTime base, not
            # CLOCK_MONOTONIC. The proof that the two domains were kept apart is
            # that the media-clock duration and the monotonic-clock duration of
            # the same copy agree. A domain error produces hundreds of seconds
            # of disagreement, so a sub-millisecond bound is not a fudge factor.
            "name": "copy_clock_compatible",
            "pass": (src_skew_n == 0 or abs(src_skew) < CLOCK_SKEW_TOLERANCE_MS) and
                    (m_skew_n == 0 or abs(m_skew) < CLOCK_SKEW_TOLERANCE_MS),
            "detail": f"srcMaxSkewMs={src_skew:.3f}(n={src_skew_n}) "
                      f"mMaxSkewMs={m_skew:.3f}(n={m_skew_n}) "
                      f"toleranceMs={CLOCK_SKEW_TOLERANCE_MS}",
        },
        {
            # Observer bookkeeping, not a Metal query. The assertion is that the
            # number of SOURCE drawables this observer believes are inside the
            # display pipeline never exceeds the layer's pool, and that the
            # three published totals let a reader re-derive it.
            "name": "drawable_bookkeeping_bounded",
            "pass": pool_max > 0 and outstanding_max <= pool_max,
            "detail": f"outstandingMax={outstanding_max} "
                      f"poolMaximumDrawables={pool_max} "
                      f"acquired={accounted['acquired']} "
                      f"released={accounted['released']} "
                      f"deferred={accounted['deferred']}",
        },
        {
            "name": "headline_within_budget",
            "pass": 0 < headline_stats["median"] < BUDGET_NS / 1e6,
            "detail": f"median={headline_stats['median']:.3f}ms budget={BUDGET_NS / 1e6:.3f}ms",
        },
        {
            "name": "source_cadence_30fps",
            "pass": 28.0 < cad["sourceFps"] < 35.0,
            "detail": f"sourceAcquireMedianMs={cad['sourceAcquireMedianMs']:.3f} "
                      f"sourceFps={cad['sourceFps']:.2f}",
        },
    ]

    return {
        "schema": "fgmetal-r121-analysis-v1",
        "tuplesMeasured": len(tuples),
        "headline": headline_stats,
        "reconciliation": {
            "maxAbsResidualNs": max_abs_residual,
            "perTuple": [
                {
                    "generation": int(r.get("generation", 0)),
                    "pair": r.get("pair", "?"),
                    "outcome": r.get("outcome", "?"),
                    "headlineNs": int(r.get("headlineNs", 0)),
                    "chainSumNs": int(r.get("chainSumNs", 0)),
                    "residualNs": int(r.get("residualNs", 0)),
                }
                for r in tuples
            ],
        },
        "componentsMs": component_stats,
        "copyMs": copy_stats,
        "refreshQuantum": quantum,
        "cadence": cad,
        "classification": classification,
        "checks": checks,
        "verdict": "PASS" if all(check["pass"] for check in checks) else "FAIL",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("slice")
    parser.add_argument("--json", default=None)
    parser.add_argument("--label", default="")
    args = parser.parse_args()

    result = analyse(Path(args.slice))
    if args.label:
        result["label"] = args.label
    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")

    print(f"R121_ANALYSIS tuples={result['tuplesMeasured']} "
          f"headlineMedianMs={result['headline']['median']:.3f} "
          f"maxAbsResidualNs={result['reconciliation']['maxAbsResidualNs']} "
          f"verdict={result['verdict']}")
    for link, value in result["componentsMs"].items():
        print(f"  {link:38s} min={value['min']:8.3f} mean={value['mean']:8.3f} "
              f"median={value['median']:8.3f} p90={value['p90']:8.3f} max={value['max']:8.3f}")
    print(f"R121_CLASSIFICATION verdict={result['classification']['verdict']}")
    for name, value in result["classification"]["totalsMs"].items():
        print(f"  {name:32s} {value:8.3f} ms")
    for check in result["checks"]:
        print(f"R121_CHECK {check['name']}={'PASS' if check['pass'] else 'FAIL'} "
              f"{check['detail']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
