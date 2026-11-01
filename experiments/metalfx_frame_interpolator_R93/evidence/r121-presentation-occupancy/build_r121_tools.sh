#!/bin/sh
# R121: build the presentation-occupancy observer.
#
# The R120 build plus exactly three additive hunks, none of which changes
# scheduling, presentation, accounting, the frontier or the estimator:
#
#   1. Phase 0  the corpus guard's "own directory" is re-pointed from
#               r120-continuous-30-60 to r121-presentation-occupancy. R120
#               becomes a protected historical corpus by the same derived rule
#               that protected R115..R119 from it.
#   2. Phase 1  the R121 chain: one CLOCK_MONOTONIC instant at each of the 21
#               boundaries between B's present hook and M's WSI dispatch, plus
#               the two-copy GPU closures, emitted as two lines per tuple from
#               the tuple-terminal path.
#   3. Phase 2  observer-side drawable liveness counters and the pool/slot/
#               frontier snapshot taken under the lock the acquisition path
#               already holds.
#
# The R117 estimator, the R116 gate and early-B scheduling, the R115 tuple
# accounting, the R119 bounded diagnostics and the R120 30-fps pacer are carried
# over byte-for-byte, so the decomposition is taken on the accepted pipeline.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r121-presentation-occupancy"
RUNTIME_DIR="/tmp/fgmetal-r108-unattended"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
mkdir -p "$RUNTIME_DIR"

xcrun --sdk macosx clang \
  -dynamiclib -fobjc-arc -arch x86_64 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -Wall -Wextra -Wno-unused-parameter -Wno-unused-function \
  -I"$ROOT/experiments/metalfx_frame_interpolator_R93" \
  -framework Foundation -framework Metal -framework MetalFX -framework QuartzCore \
  "$OUT_DIR/observer-title-inprocess-r121.m" \
  "$ROOT/experiments/metalfx_frame_interpolator_R93/MetalFXFrameInterpolationProvider.m" \
  -o "$OUT_DIR/libfgmetal_r121_occupancy_probe.dylib"

file "$OUT_DIR/libfgmetal_r121_occupancy_probe.dylib"
shasum -a 256 "$OUT_DIR/libfgmetal_r121_occupancy_probe.dylib"
