#!/bin/sh
# R127 build: compile the tracked runtime observer and bounded state-machine
# self-tests into the isolated, ignored R127 evidence directory.
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
R93_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93"
OUT_DIR="${FGMETAL_R127_BUILD_DIR:-$R93_DIR/evidence/r127-alpha-runtime}"
SDK="$(xcrun --sdk macosx --show-sdk-path)"

mkdir -p "$OUT_DIR"
cp "$SCRIPT_DIR/observer-title-inprocess-r127.m" "$OUT_DIR/observer-title-inprocess-r127.m"
cp "$SCRIPT_DIR/R123DisplayPump.h" "$OUT_DIR/R123DisplayPump.h"
cp "$SCRIPT_DIR/R123DisplayPump.m" "$OUT_DIR/R123DisplayPump.m"
cp "$SCRIPT_DIR/r123_pump_selftest.m" "$OUT_DIR/r123_pump_selftest.m"
cp "$SCRIPT_DIR/R127LatencyStats.h" "$OUT_DIR/R127LatencyStats.h"
cp "$SCRIPT_DIR/R127SourceAck.h" "$OUT_DIR/R127SourceAck.h"
cp "$SCRIPT_DIR/R127SourceOwnership.h" "$OUT_DIR/R127SourceOwnership.h"
cp "$SCRIPT_DIR/r127_latency_stats_selftest.c" "$OUT_DIR/r127_latency_stats_selftest.c"
cp "$SCRIPT_DIR/r127_source_ack_selftest.c" "$OUT_DIR/r127_source_ack_selftest.c"

clang -std=c11 -Wall -Wextra -Werror -I "$SCRIPT_DIR" \
  "$SCRIPT_DIR/r127_latency_stats_selftest.c" \
  -o "$OUT_DIR/r127_latency_stats_selftest"
"$OUT_DIR/r127_latency_stats_selftest"

clang -std=c11 -Wall -Wextra -Werror -I "$SCRIPT_DIR" \
  "$SCRIPT_DIR/r127_source_ack_selftest.c" \
  -o "$OUT_DIR/r127_source_ack_selftest"
"$OUT_DIR/r127_source_ack_selftest"

xcrun --sdk macosx clang \
  -fobjc-arc -arch x86_64 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -Wall -Wextra -Wno-unused-parameter \
  -I"$SCRIPT_DIR" \
  "$SCRIPT_DIR/r123_pump_selftest.m" \
  "$SCRIPT_DIR/R123DisplayPump.m" \
  -framework Foundation \
  -o "$OUT_DIR/r123_pump_selftest"

"$OUT_DIR/r123_pump_selftest"

xcrun --sdk macosx clang \
  -dynamiclib -fobjc-arc -arch x86_64 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -Wall -Wextra -Wno-unused-parameter -Wno-unused-function \
  -I"$SCRIPT_DIR" \
  -I"$R93_DIR" \
  -framework Foundation -framework Metal -framework MetalFX -framework QuartzCore -framework CoreGraphics -framework ImageIO \
  "$SCRIPT_DIR/observer-title-inprocess-r127.m" \
  "$SCRIPT_DIR/R123DisplayPump.m" \
  "$R93_DIR/MetalFXFrameInterpolationProvider.m" \
  -o "$OUT_DIR/libfgmetal_r127_alpha_runtime.dylib"

file "$OUT_DIR/libfgmetal_r127_alpha_runtime.dylib"
shasum -a 256 \
  "$OUT_DIR/libfgmetal_r127_alpha_runtime.dylib" \
  "$SCRIPT_DIR/observer-title-inprocess-r127.m" \
  "$SCRIPT_DIR/R123DisplayPump.m" \
  "$SCRIPT_DIR/R123DisplayPump.h" \
  "$R93_DIR/MetalFXFrameInterpolationProvider.m" \
  "$R93_DIR/MetalFXFrameInterpolationProvider.h"
