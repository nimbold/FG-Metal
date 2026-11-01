#!/bin/sh
# R120: build the continuous 30->60 title observer.
#
# Stage 1 (this build): SOURCE cadence pacing only.
#
# The R119 build plus exactly two additive hunks:
#
#   1. Phase 0  the corpus guard's "own directory" is re-pointed from
#               r119-live-throughput to r120-continuous-30-60. R119 becomes a
#               protected historical corpus by the same rule that protected
#               R115..R118 from R119. No other guard logic changes.
#   2. Phase 1  FGMETAL_R120_PACE_HZ -- a bounded rate limiter on the public
#               SOURCE present call, plus its cadence evidence.
#
# Nothing else is touched. The R117 estimator, the R116 gate and early-B
# scheduling, the R115 tuple accounting, the R119 bounded diagnostics and the
# evidence isolation are all carried over unchanged, so the cadence measurement
# is taken on the accepted pipeline rather than on a re-derived one.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r120-continuous-30-60"
RUNTIME_DIR="/tmp/fgmetal-r108-unattended"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
mkdir -p "$RUNTIME_DIR"

xcrun --sdk macosx clang \
  -dynamiclib -fobjc-arc -arch x86_64 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -Wall -Wextra -Wno-unused-parameter -Wno-unused-function \
  -I"$ROOT/experiments/metalfx_frame_interpolator_R93" \
  -framework Foundation -framework Metal -framework MetalFX -framework QuartzCore \
  "$OUT_DIR/observer-title-inprocess-r120.m" \
  "$ROOT/experiments/metalfx_frame_interpolator_R93/MetalFXFrameInterpolationProvider.m" \
  -o "$OUT_DIR/libfgmetal_r120_pace_probe.dylib"

file "$OUT_DIR/libfgmetal_r120_pace_probe.dylib"
shasum -a 256 "$OUT_DIR/libfgmetal_r120_pace_probe.dylib"
