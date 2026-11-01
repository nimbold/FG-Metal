#!/usr/bin/env python3
"""Embed the selected R110 Metal estimator in the in-process title observer."""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
OBSERVER = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/observer-title-inprocess-r110-present.m"
REFERENCE_OBSERVER = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence/r108-unattended/observer-title-inprocess-r109-present.m"
SHADER = Path(__file__).with_name("r110-coarse-to-fine-balanced.metal")
EMBEDDED_SHADER = Path(__file__).with_name("r110-observer-embedded.metal")


def extract_embedded_source(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    function_start = text.index("static BOOL initializeR108Pipelines")
    declaration = text.index("    NSString *source =", function_start)
    start = text.index('"', declaration)
    end = text.index(';\n\n    NSError *error', start)
    literals = re.findall(r'"(?:\\.|[^"\\])*"', text[start:end])
    if not literals:
        raise RuntimeError(f"Metal string literals not found in {path}")
    return "".join(ast.literal_eval(item) for item in literals)


def extract_kernel(source: str, name: str) -> str:
    marker = f"kernel void {name}"
    if name == "r108Prepare":
        start = source.index(marker)
        end = source.index("kernel void r108ClearMotion", start)
        return source[start:end].strip()
    return next(line for line in source.splitlines() if marker in line).strip()


def embed() -> None:
    observer = OBSERVER.read_text(encoding="utf-8")
    previous = extract_embedded_source(OBSERVER)
    reference = extract_embedded_source(REFERENCE_OBSERVER)
    shader = SHADER.read_text(encoding="utf-8").rstrip() + "\n\n"
    suffix = "\n\n".join([
        extract_kernel(reference, "r108Prepare"),
        extract_kernel(reference, "r108ClearMotion"),
        previous[previous.index("struct R110GateGroupStats"):].rstrip(),
    ])
    combined = shader + suffix + "\n"
    EMBEDDED_SHADER.write_text(combined, encoding="utf-8")
    escaped = "\n        ".join(json.dumps(line, ensure_ascii=True)
                              for line in combined.splitlines(keepends=True))
    declaration = "    NSString *source = @\n        " + escaped + ";"
    function_start = observer.index("static BOOL initializeR108Pipelines")
    start = observer.index("    NSString *source =", function_start)
    end = observer.index(";\n\n    NSError *error", start) + 1
    observer = observer[:start] + declaration + observer[end:]
    OBSERVER.write_text(observer, encoding="utf-8")
    print(f"R110_EMBEDDED_SHADER={SHADER}")
    print(f"R110_EMBEDDED_SHADER_BYTES={len(combined.encode())}")
    print(f"R110_EMBEDDED_SHADER_EXTRACT={EMBEDDED_SHADER}")
    print(f"R110_OBSERVER={OBSERVER}")


if __name__ == "__main__":
    embed()
