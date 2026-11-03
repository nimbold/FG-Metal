#!/bin/sh
# Compile the actual observer into the test executable so its static title
# present hook and ownership helpers are called directly. The R123 pump is the
# production implementation linked by the same build.
set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
ROOT="$(CDPATH= cd -- "$SCRIPT_DIR/../../.." && pwd)"
R93_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93"
OUT_DIR="${FGMETAL_R130_BUILD_DIR:-$R93_DIR/evidence/r130-actual-observer}"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
SOURCE_DIR="$OUT_DIR/source"

mkdir -p "$OUT_DIR" "$SOURCE_DIR"

# Compile an immutable input snapshot. The observer is evolving during R130;
# copying first makes the binary's exact source hashes available even if the
# workspace changes while the build runs.
cp "$SCRIPT_DIR/r130_observer_pre_capture_integration.m" "$SOURCE_DIR/"
cp "$SCRIPT_DIR/observer-title-inprocess-r127.m" "$SOURCE_DIR/"
cp "$SCRIPT_DIR/R123DisplayPump.h" "$SOURCE_DIR/"
cp "$SCRIPT_DIR/R123DisplayPump.m" "$SOURCE_DIR/"
cp "$SCRIPT_DIR/R127LatencyStats.h" "$SOURCE_DIR/"
cp "$SCRIPT_DIR/R127SourceAck.h" "$SOURCE_DIR/"
cp "$SCRIPT_DIR/R127SourceOwnership.h" "$SOURCE_DIR/"
cp "$R93_DIR/r116_gate_kernels.h" "$SOURCE_DIR/"
cp "$R93_DIR/MetalFXFrameInterpolationProvider.h" "$SOURCE_DIR/"
cp "$R93_DIR/MetalFXFrameInterpolationProvider.m" "$SOURCE_DIR/"
shasum -a 256 "$SOURCE_DIR"/* > "$OUT_DIR/source-sha256.txt"
git -C "$ROOT" rev-parse HEAD > "$OUT_DIR/source-commit.txt"

xcrun --sdk macosx clang \
  -fobjc-arc -arch arm64 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -Wall -Wextra -Wno-unused-parameter -Wno-unused-function \
  -I"$SOURCE_DIR" \
  -framework Foundation -framework Metal -framework MetalFX -framework QuartzCore -framework CoreGraphics -framework ImageIO \
  "$SOURCE_DIR/r130_observer_pre_capture_integration.m" \
  "$SOURCE_DIR/R123DisplayPump.m" \
  "$SOURCE_DIR/MetalFXFrameInterpolationProvider.m" \
  -o "$OUT_DIR/r130_observer_pre_capture_integration"

FGMETAL_EVIDENCE_SELFTEST=1 \
FGMETAL_EVIDENCE_DIR="$OUT_DIR/evidence" \
FGMETAL_R123_PUMP=1 \
FGMETAL_R124_TEST=0 \
  "$OUT_DIR/r130_observer_pre_capture_integration"

