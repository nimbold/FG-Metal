#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended"
OUT="/tmp/fgmetal-r108-unattended/send_bounded_input_to_game"
mkdir -p "$(dirname -- "$OUT")"
xcrun --sdk macosx swiftc \
  "$OUT_DIR/send_bounded_input_to_game.swift" \
  -o "$OUT"
file "$OUT"
shasum -a 256 "$OUT"
