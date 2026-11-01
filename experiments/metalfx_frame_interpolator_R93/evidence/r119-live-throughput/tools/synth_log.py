#!/usr/bin/env python3
"""Synthesise a self-consistent observer log to test analyze_r119.py.

The real measurement needs an operator, a game and a physical camera sweep. This
generator needs none of them. It emits a log whose R119_HANDOFF and
R119_ESTIMATOR_QUEUE rows satisfy their own reconciliation identities exactly,
so that a parser or arithmetic defect in the analyser fails here rather than
after a live run has been spent.

The numbers are invented. Nothing produced by this script is evidence, and it
must never be cited as a measurement. Its only purpose is to prove the analyser
parses and reconciles what the observer emits.

Usage: synth_log.py <out.log> [--tuples 20] [--budget 20]
"""

import argparse
import random

NOMINAL_NS = 16_666_700


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("out")
    parser.add_argument("--tuples", type=int, default=20)
    parser.add_argument("--budget", type=int, default=20)
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    lines = [
        "R119_EVIDENCE_DIRECTORY pid=99999 source=env "
        "path=/tmp/synthetic/r119-live-throughput writable=1 protected=0 "
        "override=/tmp/synthetic/r119-live-throughput allowLegacy=<unset>",
        "R119_DIAGNOSTIC_POLICY pid=99999 mode=bounded env=<unset> "
        "boundedSample=first-middle-last timingPerTuple=1 gatePerTuple=1 "
        "accountingPerTuple=1 wsiPerTuple=1",
    ]

    # A dense, jittery 60 Hz acquisition stream.
    clock = 1_000_000_000
    seq = 1000
    acquisitions = []
    for _ in range(4000):
        seq += 1
        clock += NOMINAL_NS + rng.randint(-400_000, 900_000)
        acquisitions.append((seq, clock))
        lines.append(
            f"R108_SOURCE_FRAME_ACQUIRED pid=99999 seq={seq} "
            f"acquiredAtMonotonicNs={clock} drawableID=1"
        )

    def sampled(ordinal, budget):
        return ordinal == 1 or ordinal == (budget + 1) // 2 or ordinal == budget

    for ordinal in range(1, args.tuples + 1):
        want = sampled(ordinal, args.budget)
        lines.append(
            f"R119_DIAG_SAMPLE tuple={ordinal} budget={args.budget} mode=bounded "
            f"fullArtefacts={1 if want else 0} policy=test"
        )
        pair_a = 2000 + ordinal * 2
        pair_b = pair_a + 1

        # Handoff chain, each stage invented but the sum exact by construction.
        hook = 900_000_000_000 + ordinal * 1_000_000_000
        hook_to_encode = 180_000
        encode = 120_000
        commit_to_copy = 300_000
        copy_exec = 640_000
        copy_end_to_wake = 210_000
        wake_to_dispatch = 90_000
        dispatch_to_queue = 40_000
        queue_to_start = 30_000
        hook_to_gen_start = (hook_to_encode + encode + commit_to_copy + copy_exec +
                             copy_end_to_wake + wake_to_dispatch + dispatch_to_queue +
                             queue_to_start)
        hook_to_copy_start = hook_to_encode + encode + commit_to_copy

        lines.append(
            f"R119_HANDOFF generation=1 pair={pair_a}->{pair_b} clock=media "
            f"hookToCaptureEntryNs={hook_to_encode} captureEntryToBlitCommitNs={encode} "
            f"blitCommitToCopyGpuStartNs={commit_to_copy} copyGpuExecNs={copy_exec} "
            f"copyGpuEndToWakeNs={copy_end_to_wake} wakeToDispatchNs={wake_to_dispatch} "
            f"dispatchToGenQueueEntryNs={dispatch_to_queue} "
            f"genQueueEntryToGenStartNs={queue_to_start} "
            f"hookToGenStartNs={hook_to_gen_start} "
            f"hookToCopyGpuStartNs={hook_to_copy_start}"
        )

        queue = 1_900_000
        gpu_exec = 10_200_000
        wake_latency = 340_000
        commit_to_wake = queue + gpu_exec + wake_latency
        lines.append(
            f"R119_ESTIMATOR_QUEUE generation=1 pair={pair_a}->{pair_b} clock=media "
            f"commitToGpuStartNs={queue} gpuExecNs={gpu_exec} "
            f"gpuEndToWakeNs={wake_latency} commitToWakeNs={commit_to_wake} "
            f"cpuWallNs={commit_to_wake + 120_000} "
            f"queueShareOfWall={queue / commit_to_wake:.4f}"
        )

        prep = 207_000
        estimator_wall = commit_to_wake + 120_000
        estimator_to_metalfx = 300_000
        metalfx = 5_656_000
        gate = 268_000
        gen_thread = prep + estimator_wall + estimator_to_metalfx + metalfx + gate
        components = prep + estimator_wall + estimator_to_metalfx + metalfx + gate
        service = hook_to_gen_start + gen_thread
        acquired_to_gen_start = 2_670_000
        end_to_end = acquired_to_gen_start + service
        lines.append(
            f"R118_GENERATION_SERVICE generation=1 pair={pair_a}->{pair_b} "
            f"outcome=ACCEPTED gatePath=transition-command-buffer "
            f"presentHookToGateNs={service} "
            f"bAcquiredToGenStartNs={acquired_to_gen_start} prepNs={prep} "
            f"estimatorNs={estimator_wall} estimatorToMetalFxNs={estimator_to_metalfx} "
            f"metalFxNs={metalfx} gateDecisionNs={gate} "
            f"genThreadServiceNs={gen_thread} endToEndServiceNs={end_to_end} "
            f"estimatorGpuNs={gpu_exec} estimatorCpuMinusGpuNs={estimator_wall - gpu_exec} "
            f"componentsSumNs={components} bAcquiredAtMonotonicNs={hook - acquired_to_gen_start} "
            f"bPresentHookAtMonotonicNs={hook} gsStartNs={hook + hook_to_gen_start} "
            f"gsEndNs={hook + service}"
        )
        lines.append(
            f"R110_FAST_PRESENT_ELIGIBILITY=ACCEPTED pair={pair_a}->{pair_b}"
        )
        if want:
            lines.append(f"R110_METALFX_DIAGNOSTIC=FINITE pair={pair_a}->{pair_b} saved=1")
            lines.append(f"R108_MOTION_DIAGNOSTIC_EXPORT=SAVED pair={pair_a}->{pair_b}")
        else:
            lines.append(
                f"R110_METALFX_DIAGNOSTIC=SKIPPED pair={pair_a}->{pair_b} "
                f"reason=r119-bounded-diagnostic-sample"
            )

    lines.append("R115_CONTINUOUS_RUN=COMPLETE accepted=20")
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    print(f"SYNTH wrote={args.out} lines={len(lines)} tuples={args.tuples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
