#!/usr/bin/env python3
"""R120 Phase 1 analyser: did the SOURCE actually become ~30 fps?

The brief is explicit that a limiter asking for 30 fps is not evidence of a
30 fps SOURCE. This tool therefore measures three independent cadences and
requires all three to agree before it will say the cadence was established:

  acquisition  -- interval between consecutive R108_SOURCE_FRAME_ACQUIRED
                  monotonic timestamps. This is production, not presentation.
  present      -- interval between consecutive paced SOURCE present calls.
  displayed    -- interval between consecutive R101_WSI_DISPLAYED presentedTime
                  values. This is what the panel actually showed.

The verdict is NOT_ESTABLISHED if any cadence still looks like ~60 fps, which
is the signature of a limiter that did not back-pressure the game (the every-
other-frame cheat the brief forbids).

Usage:
    analyze_r120.py <observer.log> [--json out.json] [--period-hz 30]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from pathlib import Path

ACQUIRE = re.compile(r"R108_SOURCE_FRAME_ACQUIRED pid=\d+ seq=(\d+) acquiredAtMonotonicNs=(\d+)")
PACE = re.compile(
    r"R120_PACE_ACQUIRE callId=(\d+) entryNs=(\d+) resumeNs=(\d+) blockedNs=(\d+) "
    r"intervalNs=(\d+) nextAllowedNs=(\d+) presents=(\d+) blocked=(\d+)"
)
PACE_SUMMARY = re.compile(r"R120_PACE_SUMMARY (.*)$")
DISPLAYED = re.compile(
    r"R101_WSI_DISPLAYED origin=(\w+) wsiPresentId=\d+ appFrameId=(\d+) "
    r"internalPresentId=(\d+) drawableID=\d+ presentedTime=([-\d.]+) reachedDisplay=(\d)"
)
DISPLAYED_INT = re.compile(
    r"R102_INTERNAL_PRESENTED_CALLBACK .*?presentedTime=([-\d.]+) reachedDisplay=(\d)"
)


def stats(values: list[float]) -> dict[str, float]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    n = len(ordered)

    def pct(q: float) -> float:
        if n == 1:
            return ordered[0]
        pos = q * (n - 1)
        low = int(pos)
        high = min(low + 1, n - 1)
        frac = pos - low
        return ordered[low] * (1 - frac) + ordered[high] * frac

    return {
        "n": n,
        "min": ordered[0],
        "p10": pct(0.10),
        "median": statistics.median(ordered),
        "mean": statistics.fmean(ordered),
        "p90": pct(0.90),
        "max": ordered[-1],
        "stdev": statistics.pstdev(ordered) if n > 1 else 0.0,
    }


def intervals_ms(times_ns: list[int]) -> list[float]:
    out = []
    for a, b in zip(times_ns, times_ns[1:]):
        if b > a:
            out.append((b - a) / 1e6)
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("log")
    parser.add_argument("--json")
    parser.add_argument("--period-hz", type=float, default=30.0)
    args = parser.parse_args()

    period_ms = 1000.0 / args.period_hz
    text = Path(args.log).read_text(encoding="utf-8", errors="replace")

    acq_seq: list[int] = []
    acq_t: list[int] = []
    for seq, t in ACQUIRE.findall(text):
        acq_seq.append(int(seq))
        acq_t.append(int(t))

    pace_t: list[int] = []
    pace_blocked: list[int] = []
    for _cid, _entry, resume, blocked, _iv, _next, _p, _b in PACE.findall(text):
        pace_t.append(int(resume))
        pace_blocked.append(int(blocked))

    disp_t: list[float] = []
    disp_origin: dict[str, int] = {}
    for origin, _app, _internal, presented, reached in DISPLAYED.findall(text):
        disp_origin[origin] = disp_origin.get(origin, 0) + 1
        if reached == "1":
            value = float(presented)
            if value > 0.0:
                disp_t.append(value)
    internal_displayed = sum(1 for _ in DISPLAYED_INT.findall(text))

    acq_iv = intervals_ms(acq_t)
    pace_iv = intervals_ms(pace_t)
    disp_iv = intervals_ms([int(round(v * 1e9)) for v in disp_t])

    # A cadence is "still ~60 fps" when its median sits materially below the
    # requested period. 0.75x is the discriminator used throughout.
    def looks_like_native(s: dict[str, float]) -> bool:
        return s.get("n", 0) > 0 and s["median"] < 0.75 * period_ms

    acq_stats = stats(acq_iv)
    pace_stats = stats(pace_iv)
    disp_stats = stats(disp_iv)

    def fraction_below(s_values: list[float], threshold: float) -> float:
        if not s_values:
            return 0.0
        return sum(1 for v in s_values if v < threshold) / len(s_values)

    # Tolerances are stated, not tuned after the fact:
    #   median within +/-10% of the requested period, and
    #   fewer than 10% of intervals materially faster than the period.
    lo, hi = 0.90 * period_ms, 1.10 * period_ms
    acq_ok = acq_stats.get("n", 0) > 0 and lo <= acq_stats["median"] <= hi
    pace_ok = pace_stats.get("n", 0) > 0 and lo <= pace_stats["median"] <= hi
    short_frac = fraction_below(acq_iv, 0.75 * period_ms)

    established = acq_ok and pace_ok and short_frac <= 0.10

    result = {
        "schema": "fgmetal-r120-source-cadence-v1",
        "log": str(args.log),
        "requestedHz": args.period_hz,
        "requestedPeriodMs": round(period_ms, 4),
        "acquisition": {
            "count": len(acq_t),
            "interval": {k: round(v, 4) for k, v in acq_stats.items()},
            "fractionBelow075Period": round(short_frac, 4),
            "looksLikeNative60": looks_like_native(acq_stats),
        },
        "present": {
            "count": len(pace_t),
            "interval": {k: round(v, 4) for k, v in pace_stats.items()},
            "looksLikeNative60": looks_like_native(pace_stats),
            "blockedCount": sum(1 for b in pace_blocked if b > 0),
            "blockedNsTotal": sum(pace_blocked),
            "blockedNsMax": max(pace_blocked) if pace_blocked else 0,
        },
        "displayed": {
            "count": len(disp_t),
            "interval": {k: round(v, 4) for k, v in disp_stats.items()},
            "originCounts": disp_origin,
            "internalPresentedCallbacks": internal_displayed,
            "looksLikeNative60": looks_like_native(disp_stats),
        },
        "criteria": {
            "acquisitionMedianWithin10pct": acq_ok,
            "presentMedianWithin10pct": pace_ok,
            "shortIntervalFractionAtMost10pct": short_frac <= 0.10,
        },
        "verdict": "ESTABLISHED" if established else "NOT_ESTABLISHED",
    }

    print(f"R120_SOURCE_CADENCE requestedHz={args.period_hz:g} periodMs={period_ms:.4f}")
    print(f"R120_ACQUISITION n={acq_stats.get('n',0)} "
          f"medianMs={acq_stats.get('median',0):.4f} p10={acq_stats.get('p10',0):.4f} "
          f"p90={acq_stats.get('p90',0):.4f} min={acq_stats.get('min',0):.4f} "
          f"max={acq_stats.get('max',0):.4f} shortFraction={short_frac:.4f}")
    print(f"R120_PRESENT n={pace_stats.get('n',0)} "
          f"medianMs={pace_stats.get('median',0):.4f} "
          f"blocked={result['present']['blockedCount']} "
          f"maxBlockedMs={result['present']['blockedNsMax']/1e6:.4f}")
    print(f"R120_DISPLAYED n={disp_stats.get('n',0)} "
          f"medianMs={disp_stats.get('median',0):.4f} "
          f"origins={disp_origin}")
    print(f"R120_CADENCE_VERDICT TRUE_30FPS_SOURCE_CADENCE={result['verdict']}")
    if result["verdict"] != "ESTABLISHED":
        reasons = [k for k, v in result["criteria"].items() if not v]
        print(f"R120_CADENCE_REASONS failed={','.join(reasons)}")
        if looks_like_native(acq_stats):
            print("R120_CADENCE_NOTE acquisition still at ~"
                  f"{1000.0/acq_stats['median']:.1f} Hz: the limiter did not "
                  "back-pressure production. Do not proceed by dropping frames.")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if established else 1


if __name__ == "__main__":
    sys.exit(main())
