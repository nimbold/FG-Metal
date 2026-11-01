#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r106-title-derived-motion"
SDK="$(xcrun --sdk macosx --show-sdk-path)"

xcrun --sdk macosx clang \
  -O2 -fobjc-arc -arch arm64 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -I"$ROOT/experiments/metalfx_frame_interpolator_R93" \
  -framework Foundation -framework Metal -framework MetalFX \
  "$OUT_DIR/r106_title_metafx_probe.m" \
  "$ROOT/experiments/metalfx_frame_interpolator_R93/MetalFXFrameInterpolationProvider.m" \
  -o "$OUT_DIR/r106_title_metafx_probe"

file "$OUT_DIR/r106_title_metafx_probe"
shasum -a 256 "$OUT_DIR/r106_title_metafx_probe"
