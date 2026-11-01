#!/bin/sh
# R118: build the live generation-throughput observer.
#
# Byte-for-byte the R117 build except for the source file, which is the R117
# observer plus the R118 generation-service instrumentation (one entry
# timestamp, one gate-verdict timestamp, one additional log line). No
# presentation, scheduling, gate or accounting code is touched, so the R116
# frozen presentation path is preserved by construction.
#
# The estimator kernel carried by this build is the R117 sample-outer refine.
# Verify it with tools/verify_r118_estimator.py before any live run -- a build
# that silently reverted to the R110 candidate-outer kernel would still compile
# and would still produce correct motion, and would silently invalidate every
# timing number in the round.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended"
RUNTIME_DIR="/tmp/fgmetal-r108-unattended"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
mkdir -p "$RUNTIME_DIR"

xcrun --sdk macosx clang \
  -dynamiclib -fobjc-arc -arch x86_64 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -Wall -Wextra -Wno-unused-parameter -Wno-unused-function \
  -I"$ROOT/experiments/metalfx_frame_interpolator_R93" \
  -framework Foundation -framework Metal -framework MetalFX -framework QuartzCore \
  "$OUT_DIR/observer-title-inprocess-r118-throughput.m" \
  "$ROOT/experiments/metalfx_frame_interpolator_R93/MetalFXFrameInterpolationProvider.m" \
  -o "$OUT_DIR/libfgmetal_r118_throughput_probe.dylib"

file "$OUT_DIR/libfgmetal_r118_throughput_probe.dylib"
shasum -a 256 "$OUT_DIR/libfgmetal_r118_throughput_probe.dylib"
