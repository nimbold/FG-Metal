#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r106-constant-depth"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
xcrun --sdk macosx clang \
  -fobjc-arc -arch arm64 -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -framework Foundation -framework Vision -framework CoreVideo \
  -framework Metal -framework QuartzCore \
  "$OUT_DIR/r106_vision_flow_probe.m" \
  -o "$OUT_DIR/r106_vision_flow_probe"
file "$OUT_DIR/r106_vision_flow_probe"
shasum -a 256 "$OUT_DIR/r106_vision_flow_probe"
