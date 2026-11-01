#!/bin/sh
# R116 offline gate-equivalence harness.
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r116-criticalpath/tools"
SDK="$(xcrun --sdk macosx --show-sdk-path)"

xcrun --sdk macosx clang \
  -fobjc-arc -O2 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -Wall -Wextra -Wno-unused-parameter \
  -I"$ROOT/experiments/metalfx_frame_interpolator_R93" \
  -framework Foundation -framework Metal \
  "$OUT_DIR/r116_gate_equivalence.m" \
  -o "$OUT_DIR/r116_gate_equivalence"

file "$OUT_DIR/r116_gate_equivalence"
