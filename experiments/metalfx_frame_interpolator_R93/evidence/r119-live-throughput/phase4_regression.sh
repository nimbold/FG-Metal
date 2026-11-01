#!/bin/sh
# R118 Phase 4: limited regression equivalence on a live pair.
#
# For one pair captured during the live run, re-run the *reference* R110
# estimator (candidate-outer refine) over the same source raws and compare it
# byte for byte with the motion field the shipped R117 estimator actually
# produced in the game. Two independent statements come out of it:
#
#   saved-flow vs reference-rerun  -> the live estimator and the reference agree
#   saved-flow vs embedded-rerun   -> the shipped library's own kernel text,
#                                     compiled again, reproduces what it did live
#
# Usage: phase4_regression.sh <run-dir> <pairA> <pairB> [width] [height]
set -eu

RUN_DIR="${1:?usage: phase4_regression.sh <run-dir> <pairA> <pairB> [w] [h]}"
PAIR_A="${2:?}"
PAIR_B="${3:?}"
WIDTH="${4:-1470}"
HEIGHT="${5:-956}"

EVID="/Users/nima/Documents/Code/FG-Metal/experiments/metalfx_frame_interpolator_R93/evidence"
BENCH="$EVID/r110-motion-redesign/r110_coarse_to_fine_bench"
REF_SHADER="$EVID/r110-motion-redesign/r110-coarse-to-fine-balanced.metal"
NEW_SHADER="$EVID/r119-live-throughput/embedded-estimator-r119.metal"
TMP="/tmp/fgmetal-r119-phase4"

mkdir -p "$TMP"

for f in "$BENCH" "$REF_SHADER" "$NEW_SHADER"; do
    if [ ! -f "$f" ]; then
        echo "R119_PHASE4=SKIP reason=missing_tool path=$f"
        exit 2
    fi
done

# Never compare against a stale output from a previous invocation: if the bench
# aborts (e.g. the kernel fails to compile) we must fail, not silently reuse an
# old field.
rm -f "$TMP/ref.rg16f" "$TMP/embedded.rg16f"

A="$RUN_DIR/source-sequence-$PAIR_A.raw"
B="$RUN_DIR/source-sequence-$PAIR_B.raw"
SAVED="$RUN_DIR/motion-pair-$PAIR_A-$PAIR_B.rg16f"

for f in "$A" "$B" "$SAVED"; do
    if [ ! -f "$f" ]; then
        echo "R119_PHASE4=SKIP reason=missing path=$f"
        exit 2
    fi
done

echo "R119_PHASE4 pair=$PAIR_A->$PAIR_B resolution=${WIDTH}x${HEIGHT}"
echo "R119_PHASE4 shader_reference=$(basename "$REF_SHADER") shader_embedded=$(basename "$NEW_SHADER")"

"$BENCH" "$A" "$B" "$WIDTH" "$HEIGHT" "$REF_SHADER" "$TMP/ref.rg16f" \
    | grep -E "R110_ESTIMATOR_MEDIAN_GPU_MS|R110_FLOW_SAVED" || true
"$BENCH" "$A" "$B" "$WIDTH" "$HEIGHT" "$NEW_SHADER" "$TMP/embedded.rg16f" \
    | grep -E "R110_ESTIMATOR_MEDIAN_GPU_MS|R110_FLOW_SAVED" || true

for f in "$TMP/ref.rg16f" "$TMP/embedded.rg16f"; do
    if [ ! -f "$f" ]; then
        echo "R119_PHASE4=FAIL reason=bench_produced_no_field path=$f"
        exit 3
    fi
done

STATUS=0
if cmp -s "$SAVED" "$TMP/ref.rg16f"; then
    echo "R119_PHASE4 live-vs-reference=BIT_IDENTICAL bytes=$(stat -f%z "$SAVED")"
else
    echo "R119_PHASE4 live-vs-reference=DIFFERS"
    cmp "$SAVED" "$TMP/ref.rg16f" 2>&1 | head -3 || true
    STATUS=1
fi

if cmp -s "$SAVED" "$TMP/embedded.rg16f"; then
    echo "R119_PHASE4 live-vs-embedded=BIT_IDENTICAL"
else
    echo "R119_PHASE4 live-vs-embedded=DIFFERS"
    STATUS=1
fi

if cmp -s "$TMP/ref.rg16f" "$TMP/embedded.rg16f"; then
    echo "R119_PHASE4 reference-vs-embedded=BIT_IDENTICAL"
else
    echo "R119_PHASE4 reference-vs-embedded=DIFFERS"
    STATUS=1
fi

exit $STATUS
