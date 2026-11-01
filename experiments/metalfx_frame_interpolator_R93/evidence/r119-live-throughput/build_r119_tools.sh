#!/bin/sh
# R119: build the live generation-throughput observer.
#
# The R118 build plus four additive measurement/policy hunks:
#
#   1. Phase 0  evidence-directory isolation: one shared accessor honouring
#               FGMETAL_EVIDENCE_DIR, one path composer, one blob writer, a
#               protected-corpus guard, and an in-process self test.
#   2. Phase 1  bounded diagnostic sample: full raw/motion/MetalFX artefacts for
#               the first, middle and last tuple only.
#   3. Phase 2  capture/handoff decomposition on the media clock.
#   4. Phase 3  estimator queue delay vs GPU execution on the same clock.
#
# No presentation, scheduling, gate, frontier, accounting or estimator code is
# touched, so the R116 frozen presentation path and the R117 estimator are
# preserved by construction. The estimator kernel carried by this build is the
# R117 sample-outer refine; verify it with tools/verify_r119_estimator.py before
# any live run. A build that silently reverted to the R110 candidate-outer kernel
# would still compile, would still produce correct motion, and would silently
# invalidate every timing number in the round.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r119-live-throughput"
RUNTIME_DIR="/tmp/fgmetal-r108-unattended"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
mkdir -p "$RUNTIME_DIR"

xcrun --sdk macosx clang \
  -dynamiclib -fobjc-arc -arch x86_64 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -Wall -Wextra -Wno-unused-parameter -Wno-unused-function \
  -I"$ROOT/experiments/metalfx_frame_interpolator_R93" \
  -framework Foundation -framework Metal -framework MetalFX -framework QuartzCore \
  "$OUT_DIR/observer-title-inprocess-r119-throughput.m" \
  "$ROOT/experiments/metalfx_frame_interpolator_R93/MetalFXFrameInterpolationProvider.m" \
  -o "$OUT_DIR/libfgmetal_r119_throughput_probe.dylib"

file "$OUT_DIR/libfgmetal_r119_throughput_probe.dylib"
shasum -a 256 "$OUT_DIR/libfgmetal_r119_throughput_probe.dylib"
