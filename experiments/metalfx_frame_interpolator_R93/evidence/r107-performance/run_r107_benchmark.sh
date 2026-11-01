#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../../../.." && pwd)"
HERE="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r107-performance"
PAIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r104-gpu-resident/run-03"

python3 "$HERE/generate_shaders.py" | tee "$HERE/shader-lineage.log"
xcrun --sdk macosx clang -fobjc-arc -O2 \
  -framework Foundation -framework Metal \
  "$HERE/offline_motion_bench.m" -o "$HERE/offline_motion_bench"
"$HERE/offline_motion_bench" \
  "$PAIR/source-sequence-3.raw" "$PAIR/source-sequence-4.raw" \
  "$HERE/r107_exact.metal" "$HERE/r107_step4_control.metal" \
  "$HERE/r107-exact-flow.rg16f" "$HERE/r107-step4-flow.rg16f" \
  "$HERE/benchmark.log"

python3 "$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r107-motion-validation/analyze_title_pair.py" \
  --source-a "$PAIR/source-sequence-3.raw" --source-b "$PAIR/source-sequence-4.raw" \
  --motion "$HERE/r107-exact-flow.rg16f" --width 1280 --height 720 \
  --source-a-sequence 3 --source-b-sequence 4 --condition R104_HARNESS \
  --json-out "$HERE/r107-exact-warp.json" --markdown-out "$HERE/r107-exact-warp.md"
python3 "$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r107-motion-validation/analyze_title_pair.py" \
  --source-a "$PAIR/source-sequence-3.raw" --source-b "$PAIR/source-sequence-4.raw" \
  --motion "$HERE/r107-step4-flow.rg16f" --width 1280 --height 720 \
  --source-a-sequence 3 --source-b-sequence 4 --condition R104_HARNESS \
  --json-out "$HERE/r107-step4-warp.json" --markdown-out "$HERE/r107-step4-warp.md"
