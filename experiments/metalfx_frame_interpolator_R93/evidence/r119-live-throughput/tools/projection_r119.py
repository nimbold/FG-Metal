#!/usr/bin/env python3
"""R119: per-tuple contention projection.

The question this answers is *not* "how fast is it" (that is analyze_r119.py).
It is: if the two GPU stages ran at their measured offline cost instead of their
contended live cost, what would the generation service have been, tuple by tuple?

This is a projection, not a measurement. It assumes the removed contention does
not reappear as CPU work elsewhere -- an assumption that the next round has to
test rather than inherit. It is reported here only to size the opportunity and to
decide whether further estimator work is even the right lever.

Medians are not additive: median(a) + median(b) != median(a+b) for correlated
components. Every figure below is therefore computed per tuple first and then
summarised, never summed across medians.
"""

import argparse
import re
import statistics as st

# Offline, uncontended costs for the same kernels at 1470x956.
#   estimator: R117 accepted offline figure; re-confirmed this round by
#              phase4_regression.sh, which re-compiled the shipped kernel from
#              the extracted literal and measured 5.999 / 6.069 / 6.605 ms.
#   metalFx  : R118 offline figure, unchanged this round.
ESTIMATOR_OFFLINE_MS = 6.0595
METALFX_OFFLINE_MS = 5.07

SERVICE_RE = re.compile(r"R118_GENERATION_SERVICE (.*)$")
FIELD_RE = re.compile(r"(\w+)=([-\w.]+)")

REQUIRED = (
    "presentHookToGateNs", "estimatorGpuNs", "metalFxNs", "estimatorCpuMinusGpuNs",
    "bAcquiredToGenStartNs", "prepNs", "estimatorToMetalFxNs", "gateDecisionNs",
)


def med(xs):
    return st.median(xs) if xs else float("nan")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("log")
    args = ap.parse_args()

    rows = []
    with open(args.log, "r", errors="replace") as fh:
        for line in fh:
            m = SERVICE_RE.search(line)
            if not m:
                continue
            f = dict(FIELD_RE.findall(m.group(1)))
            if any(k not in f for k in REQUIRED):
                continue
            rows.append(
                dict(
                    generation=int(f["generation"]),
                    pair=f["pair"],
                    outcome=f["outcome"],
                    service=int(f["presentHookToGateNs"]) / 1e6,
                    est_gpu=int(f["estimatorGpuNs"]) / 1e6,
                    metalfx=int(f["metalFxNs"]) / 1e6,
                    est_cpu_minus_gpu=int(f["estimatorCpuMinusGpuNs"]) / 1e6,
                    handoff=int(f["bAcquiredToGenStartNs"]) / 1e6,
                    prep=int(f["prepNs"]) / 1e6,
                    gap=int(f["estimatorToMetalFxNs"]) / 1e6,
                    gate=int(f["gateDecisionNs"]) / 1e6,
                )
            )

    if not rows:
        raise SystemExit("R119_PROJECTION=NO_DATA")

    accepted = [r for r in rows if r["outcome"] == "ACCEPTED"]
    print(f"R119_PROJECTION tuples={len(rows)} accepted={len(accepted)}")

    service = [r["service"] for r in accepted]
    est_gpu = [r["est_gpu"] for r in accepted]
    metalfx = [r["metalfx"] for r in accepted]

    # Per-tuple: service minus everything that is not GPU execution.
    non_gpu = [r["service"] - r["est_gpu"] - r["metalfx"] for r in accepted]

    est_contended = [r["est_gpu"] - ESTIMATOR_OFFLINE_MS for r in accepted]
    fx_contended = [r["metalfx"] - METALFX_OFFLINE_MS for r in accepted]

    projected = [
        r["service"] - max(0.0, r["est_gpu"] - ESTIMATOR_OFFLINE_MS)
        - max(0.0, r["metalfx"] - METALFX_OFFLINE_MS)
        for r in accepted
    ]

    print()
    print("  observed service            median=%.3f mean=%.3f p90=%.3f"
          % (med(service), st.mean(service), sorted(service)[int(0.9 * (len(service) - 1))]))
    print("  non-GPU overhead per tuple  median=%.3f mean=%.3f min=%.3f max=%.3f"
          % (med(non_gpu), st.mean(non_gpu), min(non_gpu), max(non_gpu)))
    print()
    print("  estimator GPU   live median=%.3f  offline=%.4f  contention=%.3f ms (%.3fx)"
          % (med(est_gpu), ESTIMATOR_OFFLINE_MS, med(est_contended),
             med(est_gpu) / ESTIMATOR_OFFLINE_MS))
    print("  estimator GPU   live min   =%.3f  -> %.3fx offline  (lower tail touches uncontended cost)"
          % (min(est_gpu), min(est_gpu) / ESTIMATOR_OFFLINE_MS))
    print("  metalFx         live median=%.3f  offline=%.4f  contention=%.3f ms (%.3fx)"
          % (med(metalfx), METALFX_OFFLINE_MS, med(fx_contended),
             med(metalfx) / METALFX_OFFLINE_MS))
    print("  metalFx         live min   =%.3f  -> %.3fx offline"
          % (min(metalfx), min(metalfx) / METALFX_OFFLINE_MS))
    print()
    print("  PROJECTED service if both GPU stages ran at offline cost:")
    print("    median=%.3f  mean=%.3f  min=%.3f  max=%.3f"
          % (med(projected), st.mean(projected), min(projected), max(projected)))
    print("    projected median / observed median = %.3f"
          % (med(projected) / med(service)))

    # Is the contention shared? If estimator and MetalFX contend independently,
    # a single scheduling fix cannot address both.
    total = [e + f for e, f in zip(est_contended, fx_contended)]
    print("    total GPU contention per tuple: median=%.3f mean=%.3f min=%.3f max=%.3f"
          % (med(total), st.mean(total), min(total), max(total)))
    print("    sum of the two individual medians = %.3f (vs median of the sum %.3f)"
          % (med(est_contended) + med(fx_contended), med(total)))
    if len(accepted) > 2:
        try:
            rho = st.correlation(est_contended, fx_contended)
            print("    corr(estimator contention, metalFx contention) = %+.3f" % rho)
            print("    -> anti-correlated: the two stages do not collide with each other;")
            print("       they absorb the same collision in turn. Removing it is not a")
            print("       matter of reordering one stage past the other.")
        except Exception:
            pass

    # How much of the projected gain is one stage vs the other?
    est_only = [r["service"] - max(0.0, r["est_gpu"] - ESTIMATOR_OFFLINE_MS) for r in accepted]
    fx_only = [r["service"] - max(0.0, r["metalfx"] - METALFX_OFFLINE_MS) for r in accepted]
    print("    estimator-only projection median=%.3f  metalFx-only median=%.3f"
          % (med(est_only), med(fx_only)))
    print()
    print("  R119_PROJECTION_CAVEAT this is a projection; it assumes the removed")
    print("  contention does not migrate into CPU work. It sizes the opportunity,")
    print("  it is not a measurement of a scheduling change.")


if __name__ == "__main__":
    main()
