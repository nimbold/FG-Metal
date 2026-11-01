#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended"
RUNTIME_DIR="/tmp/fgmetal-r108-unattended"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
mkdir -p "$RUNTIME_DIR"

xcrun --sdk macosx clang \
  -dynamiclib -fobjc-arc -arch x86_64 \
  -isysroot "$SDK" -mmacosx-version-min=26.0 \
  -I"$ROOT/experiments/metalfx_frame_interpolator_R93" \
  -framework Foundation -framework Metal -framework MetalFX -framework QuartzCore \
  "$OUT_DIR/observer-title-inprocess-r112-present.m" \
  "$ROOT/experiments/metalfx_frame_interpolator_R93/MetalFXFrameInterpolationProvider.m" \
  -o "$OUT_DIR/libfgmetal_r112_present_probe.dylib"

xcrun --sdk macosx swiftc \
  "$OUT_DIR/send_bounded_character_forward_to_game.swift" \
  -o "$RUNTIME_DIR/send_bounded_character_forward_to_game"

file "$OUT_DIR/libfgmetal_r112_present_probe.dylib" "$RUNTIME_DIR/send_bounded_character_forward_to_game"
shasum -a 256 "$OUT_DIR/libfgmetal_r112_present_probe.dylib" "$RUNTIME_DIR/send_bounded_character_forward_to_game"
