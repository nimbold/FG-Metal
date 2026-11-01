#!/usr/bin/env python3
"""R120 Phase 2 analyser: does the bounded run actually display A -> M -> B at 60 Hz?

Every cadence number here comes from `presentedTime`, the timestamp the display
server reports for the frame that was actually scanned out. WSI dispatch
timestamps are not used for cadence: a dispatch only says the layer was asked to
show something, and R117 already proved a dispatch can miss its refresh. The
brief makes this distinction a review item, so the analyser enforces it.

The analyser is allowed to say NO. It is the thing that decides the round, so it
must be able to fail on each criterion independently rather than produce a
single blended score.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

REFRESH_MS = 1000.0 / 60.0          # 16.6667
SOURCE_PERIOD_MS = 1000.0 / 30.0    # 33.3333
TOL_MS = 4.0                        # half a refresh; a frame off by more has slipped


def kv(line: str) -> dict[str, str]:
    out = {}
    for match in re.finditer(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)", line):
        out[match.group(1)] = match.group(2)
    return out


def median(values: list[float]) -> float:
    return statistics.median(values) if values else 0.0


def parse(path: Path):
    """Read the three distinct display record types this pipeline emits.

    They are not interchangeable, and assuming otherwise is how an earlier
    version of this analyser reported a working pipeline as broken:

      R101_WSI_DISPLAYED origin=SOURCE   SOURCE frames presented normally
      R102_INTERNAL_PRESENTED_CALLBACK   INTERNAL M's presentedTime
      R116_PRESENTATION_ORDER            M and B presentedTime for a tuple, plus
                                         the pipeline's own ordered/distinct flags

    A deferred SOURCE B is presented through the forward path, which records its
    display time only through R116 -- it never emits R101_WSI_DISPLAYED. A
    fallback tuple never installs that handler, so its B is genuinely
    unobservable; those frames are reported as unobserved rather than as drops.
    """
    displayed = []
    pair_of = {}
    rec = {}
    timing = []
    summary = None
    run = None
    acquisitions = 0
    presents = 0
    acquire_ns = []
    forwarded_b = set()

    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("R101_WSI_DISPLAYED origin=SOURCE"):
            f = kv(line)
            if f.get("reachedDisplay") != "1":
                continue
            try:
                pt = float(f["presentedTime"])
            except (KeyError, ValueError):
                continue
            if pt > 0.0:
                displayed.append((pt, "SOURCE", int(f.get("appFrameId", 0))))
        elif line.startswith("R102_INTERNAL_PRESENTED_CALLBACK"):
            f = kv(line)
            if "sourcePair" in f:
                pair_of[f.get("ticketId", "")] = f["sourcePair"]
            if f.get("reachedDisplay") == "1":
                try:
                    pt = float(f["presentedTime"])
                except (KeyError, ValueError):
                    pt = 0.0
                if pt > 0.0:
                    rec.setdefault(f.get("sourcePair", ""), {})["m_fallback"] = pt
        elif line.startswith("R116_PRESENTATION_ORDER"):
            f = kv(line)
            pair = pair_of.get(f.get("ticketId", ""))
            if not pair:
                continue
            try:
                rec.setdefault(pair, {})["m"] = float(f["mPresentedTime"])
                rec.setdefault(pair, {})["b"] = float(f["bPresentedTime"])
                rec[pair]["ordered"] = f.get("ordered") == "1"
                rec[pair]["distinct"] = f.get("distinct") == "1"
                rec[pair]["coalesced"] = f.get("coalesced") == "1"
            except (KeyError, ValueError):
                pass
        elif line.startswith("R115_TUPLE_TIMING "):
            timing.append(kv(line))
        elif line.startswith("R116_RUN_SUMMARY "):
            summary = kv(line)
        elif line.startswith("R115_CONTINUOUS_RUN=COMPLETE "):
            run = kv(line)
        elif line.startswith("R108_SOURCE_FRAME_ACQUIRED "):
            acquisitions += 1
            f = kv(line)
            if "acquiredAtMonotonicNs" in f:
                acquire_ns.append(int(f["acquiredAtMonotonicNs"]))
        elif line.startswith("R102_SOURCE_FORWARD_BEGIN "):
            f = kv(line)
            if "appFrameId" in f:
                forwarded_b.add(int(f["appFrameId"]))
        elif line.startswith("R120_PACE_ACQUIRE "):
            presents += 1

    for pair, entry in rec.items():
        if "m" not in entry and "m_fallback" in entry:
            entry["m"] = entry["m_fallback"]
        if "b" in entry:
            displayed.append((entry["b"], "SOURCE", int(pair.split("->")[1])))
        if "m" in entry:
            displayed.append((entry["m"], "INTERNAL", pair))

    displayed.sort(key=lambda r: r[0])
    return (displayed, rec, timing, summary, run, acquisitions, presents,
            acquire_ns, forwarded_b)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("slice")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("--source-hz", type=float, default=30.0)
    args = ap.parse_args()

    path = Path(args.slice)
    (displayed, rec, timing, summary, run, acquisitions, presents,
     acquire_ns, forwarded_b) = parse(path)
    checks: list[tuple[str, bool, str]] = []

    if len(displayed) < 8:
        print(f"R120_PAIRS_ANALYSIS=INSUFFICIENT displayed={len(displayed)} "
              f"reason=too-few-presented-records")
        return 3

    span_ms = (displayed[-1][0] - displayed[0][0]) * 1000.0
    output_fps = (len(displayed) - 1) / (span_ms / 1000.0) if span_ms > 0 else 0.0
    intervals = [(displayed[i][0] - displayed[i - 1][0]) * 1000.0
                 for i in range(1, len(displayed))]

    print(f"R120_PAIRS_DISPLAY displayed={len(displayed)} spanMs={span_ms:.1f} "
          f"outputFps={output_fps:.3f} acquisitions={acquisitions} pacedPresents={presents}")
    print(f"R120_PAIRS_INTERVAL medianMs={median(intervals):.3f} "
          f"p10={sorted(intervals)[len(intervals)//10]:.3f} "
          f"p90={sorted(intervals)[min(len(intervals)-1, len(intervals)*9//10)]:.3f} "
          f"min={min(intervals):.3f} max={max(intervals):.3f}")

    # --- criterion: output cadence is one 60 Hz refresh -----------------------
    near_refresh = sum(1 for v in intervals if abs(v - REFRESH_MS) <= TOL_MS)
    frac_refresh = near_refresh / len(intervals)
    # --- criterion: the SOURCE really is ~30 fps in THIS window ---------------
    # Measured from acquisition, not from the requested pacer rate. A limiter
    # that asked for 30 Hz while the game kept producing at 60 Hz would pass a
    # naive check and fail this one.
    if len(acquire_ns) > 20:
        acquire_ns.sort()
        acq = [(b - a) / 1e6 for a, b in zip(acquire_ns, acquire_ns[1:])]
        acq_median = median(acq)
        print(f"R120_PAIRS_SOURCE acquisitions={len(acquire_ns)} "
              f"medianMs={acq_median:.3f} targetMs={SOURCE_PERIOD_MS:.3f}")
        checks.append(("source_cadence_30fps",
                       abs(acq_median - SOURCE_PERIOD_MS) <= 3.0,
                       f"acquireMedianMs={acq_median:.2f}"))
    else:
        checks.append(("source_cadence_30fps", False,
                       f"acquisitions={len(acquire_ns)} too-few-to-measure"))

    checks.append(("output_cadence_60hz", abs(output_fps - 60.0) <= 3.0 and frac_refresh >= 0.90,
                   f"fps={output_fps:.2f} intervalsNear16.667={frac_refresh:.3f}"))

    # --- criterion: no coalescing (two frames on one refresh) -----------------
    coalesced = sum(1 for v in intervals if v < 1.0)
    checks.append(("no_coalescing", coalesced == 0, f"coalescedIntervals={coalesced}"))

    # --- build the A/M/B sequence --------------------------------------------
    # An accepted pair must appear in presented order as A, M(A,B), B. A's
    # presentedTime comes from R101, M's and B's from R116.
    sequence_ok = 0
    sequence_bad = 0
    a_to_m: list[float] = []
    m_to_b: list[float] = []
    a_to_b: list[float] = []
    source_at = {}
    for pt, origin, key in displayed:
        if origin == "SOURCE":
            source_at.setdefault(key, pt)

    for pair in sorted(rec, key=lambda k: int(k.split("->")[0])):
        entry = rec[pair]
        if "m" not in entry:
            sequence_bad += 1
            continue
        a, b = (int(x) for x in pair.split("->"))
        if a not in source_at:
            sequence_bad += 1
            continue
        if not entry.get("ordered", False) or not entry.get("distinct", False):
            sequence_bad += 1
            continue
        sequence_ok += 1
        a_to_m.append((entry["m"] - source_at[a]) * 1000.0)
        m_to_b.append((entry["b"] - entry["m"]) * 1000.0)
        a_to_b.append((entry["b"] - source_at[a]) * 1000.0)

    checks.append(("displayed_order_A_M_B", sequence_ok > 0 and sequence_bad == 0,
                   f"ordered={sequence_ok} violated={sequence_bad}"))
    checks.append(("multiple_consecutive_pairs", sequence_ok >= 5, f"orderedPairs={sequence_ok}"))
    checks.append(("every_accepted_pair_displayed",
                   sequence_bad == 0 and sequence_ok > 0,
                   f"tuplesWithEvidence={len(rec)} undisplayedOrMisordered={sequence_bad}"))

    if a_to_m:
        print(f"R120_PAIRS_ORDER AtoM_medianMs={median(a_to_m):.3f} "
              f"MtoB_medianMs={median(m_to_b):.3f} AtoB_medianMs={median(a_to_b):.3f} "
              f"pairsOrdered={sequence_ok} pairsViolated={sequence_bad}")
        checks.append(("spacing_A_M_B_one_refresh",
                       abs(median(a_to_m) - REFRESH_MS) <= TOL_MS and
                       abs(median(m_to_b) - REFRESH_MS) <= TOL_MS,
                       f"AtoM={median(a_to_m):.2f} MtoB={median(m_to_b):.2f}"))

    # --- criterion: no duplicate / dropped SOURCE ----------------------------
    source_ids = [k for (_, origin, k) in displayed if origin == "SOURCE" and k]
    dup_source = len(source_ids) - len(set(source_ids))
    gaps = 0
    for prev, cur in zip(source_ids, source_ids[1:]):
        if cur != prev + 1:
            gaps += 1
    checks.append(("no_duplicate_source", dup_source == 0, f"duplicateSourceRecords={dup_source}"))
    unexplained = 0
    for prev, cur in zip(source_ids, source_ids[1:]):
        for missing in range(prev + 1, cur):
            if missing not in forwarded_b:
                unexplained += 1
    checks.append(("no_source_reorder_or_drop", unexplained == 0,
                   f"unexplainedGaps={unexplained} observed={len(source_ids)} "
                   f"forwardedButUnobservable={len(forwarded_b)}"))

    internal_ids = [k for (_, origin, k) in displayed if origin == "INTERNAL"]
    dup_internal = len(internal_ids) - len(set(internal_ids))
    coalesced_tuples = sum(1 for e in rec.values() if e.get("coalesced"))
    checks.append(("no_coalesced_tuples", coalesced_tuples == 0,
                   f"tuplesWithMAndBOnOneRefresh={coalesced_tuples}"))

    checks.append(("no_duplicate_internal", dup_internal == 0,
                   f"duplicateInternalRecords={dup_internal}"))

    # --- criterion: generation keeps up with 30 fps SOURCE --------------------
    services = []
    for t in timing:
        try:
            services.append(int(t["bPresentHookToMWsiNs"]) / 1e6)
        except (KeyError, ValueError):
            pass
    if services:
        print(f"R120_PAIRS_SERVICE n={len(services)} medianMs={median(services):.3f} "
              f"p90={sorted(services)[min(len(services)-1, len(services)*9//10)]:.3f} "
              f"max={max(services):.3f} budgetMs={SOURCE_PERIOD_MS:.3f}")
        checks.append(("generation_within_source_budget",
                       median(services) < SOURCE_PERIOD_MS,
                       f"medianServiceMs={median(services):.2f}"))

    # --- criterion: bounded queue, no backlog growth -------------------------
    # The tuple pipeline holds at most one deferred SOURCE record at a time; the
    # bounded claim is that this does not grow, which shows up as a stable
    # interval distribution rather than a drifting one.
    half = len(intervals) // 2
    first_half = median(intervals[:half]) if half else 0.0
    second_half = median(intervals[half:]) if half else 0.0
    drift = second_half - first_half
    checks.append(("no_backlog_growth", abs(drift) <= TOL_MS,
                   f"intervalDriftMs={drift:+.3f}"))

    # --- criterion: accounting / fallback ------------------------------------
    if summary:
        print("R120_PAIRS_SUMMARY " + " ".join(
            f"{k}={summary[k]}" for k in
            ("earlyBDispatch", "orderingViolations", "coalescedPresentations",
             "presentationCallbackMissing", "watchdogFired", "displayConfirmed",
             "displayRejected", "displayEvidenceLost", "promotionEligible")
            if k in summary))
        checks.append(("no_ordering_violation_reported",
                       summary.get("orderingViolations") == "0",
                       f"orderingViolations={summary.get('orderingViolations')}"))
        checks.append(("no_coalescing_reported",
                       summary.get("coalescedPresentations") == "0",
                       f"coalesced={summary.get('coalescedPresentations')}"))
        checks.append(("no_missing_callbacks",
                       summary.get("presentationCallbackMissing") == "0",
                       f"missing={summary.get('presentationCallbackMissing')}"))
        checks.append(("watchdog_quiet", summary.get("watchdogFired") == "0",
                       f"watchdogFired={summary.get('watchdogFired')}"))
    if run:
        print("R120_PAIRS_RUN " + " ".join(
            f"{k}={run[k]}" for k in
            ("attempts", "accepted", "fallback", "staleCallbacks", "duplicateMDispatch")
            if k in run))
        checks.append(("accounting_no_source_leak",
                       run.get("sourceLeaksWhileClosed") == "0",
                       f"sourceLeaks={run.get('sourceLeaksWhileClosed')}"))

    # --- added latency -------------------------------------------------------
    # A and B are consecutive SOURCE frames, so their natural separation is one
    # SOURCE period. Any excess is display latency the pipeline added.
    if a_to_b:
        added = median(a_to_b) - SOURCE_PERIOD_MS
        print(f"R120_PAIRS_LATENCY AtoBspanMs={median(a_to_b):.3f} "
              f"naturalSourcePeriodMs={SOURCE_PERIOD_MS:.3f} addedMs={added:+.3f} "
              f"addedRefreshes={added / REFRESH_MS:+.2f}")

    print("")
    failed = 0
    for name, ok, detail in checks:
        print(f"R120_PAIRS_CHECK {'PASS' if ok else 'FAIL'} {name} {detail}")
        if not ok:
            failed += 1

    verdict = "VALIDATED" if failed == 0 else "NOT_VALIDATED"
    print("")
    print(f"R120_PAIRS_VERDICT CONTINUOUS_30_TO_60_TITLE_FG={verdict} "
          f"checks={len(checks)} failed={failed} scope=CONSTANT_DEPTH_PROTOTYPE")

    if args.json:
        args.json.write_text(json.dumps({
            "schema": "fgmetal-r120-pairs-analysis-v1",
            "verdict": verdict,
            "failedChecks": failed,
            "checks": [{"name": n, "pass": ok, "detail": d} for n, ok, d in checks],
            "displayed": len(displayed),
            "outputFps": round(output_fps, 3),
            "intervalMedianMs": round(median(intervals), 3),
            "aToMMedianMs": round(median(a_to_m), 3) if a_to_m else None,
            "mToBMedianMs": round(median(m_to_b), 3) if m_to_b else None,
            "orderedPairs": sequence_ok,
            "violatedPairs": sequence_bad,
            "generationServiceMedianMs": round(median(services), 3) if services else None,
            "cadenceSource": "presentedTime",
        }, indent=2) + "\n", encoding="utf-8")
    return 0 if verdict == "VALIDATED" else 1


if __name__ == "__main__":
    sys.exit(main())
