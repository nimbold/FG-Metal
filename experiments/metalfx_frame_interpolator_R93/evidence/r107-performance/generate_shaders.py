#!/usr/bin/env python3
"""Extract exact R107 Metal source and make the step-4 local-search control."""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
OBSERVER = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r107-inprocess-title/observer-title-inprocess-r107.m"
OUT = Path(__file__).resolve().parent


def extract() -> str:
    text = OBSERVER.read_text(encoding="utf-8")
    start = text.index('NSString *source = @"') + len('NSString *source = @')
    end = text.index(';\n\n    NSError *error', start)
    block = text[start:end]
    literals = re.findall(r'"(?:\\.|[^"\\])*"', block)
    if not literals:
        raise RuntimeError("R107 Metal source literal not found")
    return "".join(ast.literal_eval(item) for item in literals)


def main() -> None:
    source = extract()
    exact_path = OUT / "r107_exact.metal"
    step4_path = OUT / "r107_step4_control.metal"
    exact_path.write_text(source, encoding="utf-8")

    # Change only the two integer local-refinement scans. The coarse search,
    # parabolic costs, propagation, and upsample remain byte-for-byte R107.
    step4 = source
    patterns = [
        ("bestX-4);dx<=min(20,bestX+4);++dx){float c=r107PatchCost(previous,current,center,target,int2(dx,0),8)",
         "bestX-4);dx<=min(20,bestX+4);++dx){float c=r107PatchCost(previous,current,center,target,int2(dx,0),4)"),
        ("bestY-4);dy<=min(20,bestY+4);++dy){float c=r107PatchCost(previous,current,center,target,int2(0,dy),8)",
         "bestY-4);dy<=min(20,bestY+4);++dy){float c=r107PatchCost(previous,current,center,target,int2(0,dy),4)"),
    ]
    for old, new in patterns:
        if source.count(old) != 1:
            raise RuntimeError(f"expected one R107 local-search expression, found {source.count(old)}: {old}")
        step4 = step4.replace(old, new, 1)
    if step4 == source:
        raise RuntimeError("step-4 control is identical to R107")
    step4_path.write_text(step4, encoding="utf-8")
    print(f"R107_SHADER_SOURCE={OBSERVER}")
    print(f"R107_SHADER_BYTES={len(source.encode())}")
    print("R107_STEP4_DELTA=exactly two local integer-refinement sample steps: 8 -> 4")
    print(f"R107_EXACT_SHA256={__import__('hashlib').sha256(source.encode()).hexdigest()}")
    print(f"R107_STEP4_SHA256={__import__('hashlib').sha256(step4.encode()).hexdigest()}")


if __name__ == "__main__":
    main()
