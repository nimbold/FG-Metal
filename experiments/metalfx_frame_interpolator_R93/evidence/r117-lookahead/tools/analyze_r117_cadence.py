#!/usr/bin/env python3
"""R117 Phase 1 — display/source cadence analysis from a real R116 observer log.

Answers, from presented-time evidence only:

  * DISPLAY_REFRESH_PERIOD  — the quantum every presented delta lands on
  * SOURCE_PRESENT_PERIOD   — the interval between consecutive SOURCE presentations
  * free refresh slots      — refreshes between consecutive SOURCE frames that are
                              not already occupied by the game's own presentation

The last one is the whole question. 2x requires the source to leave a free
refresh between consecutive frames. If the source already presents on every
refresh there is no slot for an INTERNAL frame to occupy, and no scheduler can
create one.

Only records that actually reached the display are counted: the observer logs
`reachedDisplay=0` at dispatch time with presentedTime=0, and the real evidence
is the later `reachedDisplay=1` record.
"""
import argparse
import json
import re
import statistics as st

RE_SRC = re.compile(
    r"R101_WSI_DISPLAYED origin=SOURCE wsiPresentId=(\d+) appFrameId=(\d+) "
    r"internalPresentId=(\d+) drawableID=(\d+) presentedTime=([\d.]+) reachedDisplay=(\d)")
RE_INT = re.compile(
    r"R102_INTERNAL_PRESENTED_CALLBACK ticketId=(\d+) internalPresentId=(\d+) "
    r"wsiPresentId=(\d+) sourcePair=(\d+)->(\d+) drawableID=(\d+) .*?"
    r"presentedTime=([\d.]+) reachedDisplay=(\d)")
RE_ORDER = re.compile(
    r"R116_PRESENTATION_ORDER ticketId=(\d+) mPresentedTime=([\d.]+) "
    r"bPresentedTime=([\d.]+) deltaMs=([\d.]+)")


def pct(v, p):
    if not v:
        return float("nan")
    s = sorted(v)
    if len(s) == 1:
        return s[0]
    idx = p * (len(s) - 1)
    lo, hi = int(idx), min(int(idx) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def stats(name, v, scale=1000.0):
    if not v:
        return {"name": name, "n": 0}
    s = sorted(v)
    return {
        "name": name,
        "n": len(s),
        "min": round(s[0] * scale, 4),
        "mean": round(st.fmean(s) * scale, 4),
        "median": round(pct(v, 0.5) * scale, 4),
        "p90": round(pct(v, 0.9) * scale, 4),
        "p99": round(pct(v, 0.99) * scale, 4),
        "max": round(s[-1] * scale, 4),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    ap.add_argument("--json")
    args = ap.parse_args()

    source, internal, order = {}, {}, []
    with open(args.log, "r", errors="replace") as fh:
        for line in fh:
            m = RE_SRC.search(line)
            if m:
                if m.group(6) != "1":
                    continue
                t = float(m.group(5))
                if t <= 0.0:
                    continue
                source[int(m.group(2))] = {"seq": int(m.group(2)),
                                           "wsi": int(m.group(1)), "t": t}
                continue
            m = RE_INT.search(line)
            if m:
                if m.group(8) != "1":
                    continue
                t = float(m.group(7))
                if t <= 0.0:
                    continue
                internal[int(m.group(1))] = {
                    "ticket": int(m.group(1)),
                    "internalId": int(m.group(2)),
                    "wsi": int(m.group(3)),
                    "pair": (int(m.group(4)), int(m.group(5))),
                    "t": t,
                }
                continue
            m = RE_ORDER.search(line)
            if m:
                order.append({"ticket": int(m.group(1)), "m": float(m.group(2)),
                              "b": float(m.group(3)), "delta": float(m.group(4))})

    src = [source[k] for k in sorted(source)]
    itl = [internal[k] for k in sorted(internal)]
    src.sort(key=lambda r: r["t"])

    out = {
        "log": args.log,
        "sourcePresented": len(src),
        "internalPresented": len(itl),
        "orderRecords": len(order),
    }

    # ---- SOURCE presented interval over consecutive *presented* frames ----
    src_deltas = [b["t"] - a["t"] for a, b in zip(src, src[1:]) if b["t"] > a["t"]]
    out["sourcePresentInterval"] = stats("sourcePresentIntervalMs", src_deltas)

    # ---- display quantum: the modal (not the minimum) delta ----
    if src_deltas:
        buckets = {}
        for d in src_deltas:
            buckets[round(d * 1000.0, 3)] = buckets.get(round(d * 1000.0, 3), 0) + 1
        modal = max(buckets.items(), key=lambda kv: kv[1])
        out["displayQuantumMs"] = modal[0]
        out["displayQuantumCount"] = modal[1]
        q = modal[0] / 1000.0
        mult = {}
        for d in src_deltas:
            k = int(round(d / q))
            mult[k] = mult.get(k, 0) + 1
        out["sourceIntervalMultiplesOfQuantum"] = dict(sorted(mult.items()))
        # residual: how far every delta sits from an exact integer multiple
        res = [abs(d / q - round(d / q)) * q * 1000.0 for d in src_deltas]
        out["quantizationResidualMs"] = stats("quantizationResidualMs", [r / 1000.0 for r in res])

        gaps = [max(0, int(round(d / q)) - 1) for d in src_deltas]
        out["freeRefreshSlotsPerSourceInterval"] = stats(
            "freeRefreshSlots", gaps, scale=1.0)
        out["freeRefreshSlotHistogram"] = {
            str(k): gaps.count(k) for k in sorted(set(gaps))}

    # ---- A -> M, M -> B, and the interleaved displayed stream ----
    if order:
        out["mToB"] = stats("mToBMs", [o["b"] - o["m"] for o in order])
    a_to_m = []
    for o in order:
        pair_a = next((v["pair"][0] for v in internal.values() if v["ticket"] == o["ticket"]), None)
        if pair_a is None or pair_a not in source:
            continue
        a_to_m.append(o["m"] - source[pair_a]["t"])
    out["aToM"] = stats("aToMMs", a_to_m)

    stream = sorted([{"t": r["t"], "kind": "S"} for r in src] +
                    [{"t": r["t"], "kind": "M"} for r in itl], key=lambda r: r["t"])
    if len(stream) > 1:
        deltas = [b["t"] - a["t"] for a, b in zip(stream, stream[1:]) if b["t"] > a["t"]]
        out["displayedStreamInterval"] = stats("displayedStreamIntervalMs", deltas)
        out["displayedStreamFrames"] = len(stream)

    # ---- the arithmetically decisive number ----
    if src_deltas:
        src_med = pct(src_deltas, 0.5) * 1000.0
        q = out.get("displayQuantumMs")
        out["sourceRateHz"] = round(1000.0 / src_med, 3)
        if q:
            out["displayRateHz"] = round(1000.0 / q, 3)
            out["sourceIntervalOverDisplayPeriod"] = round(src_med / q, 4)
            out["maxAchievableDisplayRateWithInterpolation"] = round(1000.0 / q, 3)
            out["x2RequiresDisplayHz"] = round(2000.0 / src_med, 3)
            out["x2FeasibleOnThisDisplay"] = bool(src_med >= 2.0 * q - 0.5)

    print(json.dumps(out, indent=2))
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=2)


if __name__ == "__main__":
    main()
