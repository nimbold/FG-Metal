#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/../../../.." && pwd)"
OUT_DIR="$ROOT/experiments/metalfx_frame_interpolator_R93/evidence/r107-inprocess-title"
OBSERVER="$OUT_DIR/observer-title-inprocess-r107.m"
SHADER="/tmp/fgmetal-r107-motion-$$.metal"
PROBE="/tmp/fgmetal-r107-shader-probe-$$"
trap 'rm -f "$SHADER" "$PROBE"' EXIT
python3 - "$OBSERVER" "$SHADER" <<'PY'
import ast
import sys
from pathlib import Path
source = Path(sys.argv[1]).read_text().splitlines()
start = next(i for i, line in enumerate(source)
             if 'NSString *source = @"#include <metal_stdlib>' in line)
parts = []
for offset, line in enumerate(source[start:]):
    literal = line.strip().split('= ', 1)[1].strip() if offset == 0 else line.strip()
    if literal.startswith('@"'):
        literal = literal[1:]
    if not literal.startswith('"'):
        break
    final = literal.endswith('";')
    if final:
        literal = literal[:-1]
    parts.append(ast.literal_eval(literal))
    if final:
        break
shader = ''.join(parts)
if not shader.startswith('#include <metal_stdlib>') or 'r107ClearMotion' not in shader:
    raise SystemExit('failed to extract R107 Metal source')
Path(sys.argv[2]).write_text(shader)
PY
xcrun --sdk macosx clang -fobjc-arc -framework Foundation -framework Metal \
  "$OUT_DIR/r107_gpu_shader_compile_probe.m" -o "$PROBE"
"$PROBE" "$SHADER"
