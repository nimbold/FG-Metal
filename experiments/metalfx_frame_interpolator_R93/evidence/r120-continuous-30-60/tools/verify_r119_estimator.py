#!/usr/bin/env python3
"""R119 build verification: prove the shipped dylib carries the R117 estimator.

Three independent checks, in increasing strength:

  1. SOURCE   -- the C string literal in the observer .m that is passed to
                 -[MTLDevice newLibraryWithSource:] is decoded and searched for
                 the R117 sample-outer refine form and the R110 candidate-outer
                 form.
  2. BINARY   -- the compiled dylib is scanned for the embedded shader literal
                 and the two forms are searched there instead. This is the check
                 that matters: it inspects what the dynamic loader will hand to
                 the Metal compiler at runtime, not what is on disk in the .m.
  3. IDENTITY -- the extracted binary literal is written out as .metal so the
                 existing bit-identity harness can compile it through the same
                 runtime path the product uses.

A "wrong kernel loaded" regression is the failure mode this guards against, so
every check reports both the presence of the R117 form and the absence of the
R110 form. Presence alone would not distinguish a tree that contains both.

Usage: verify_r119_estimator.py [--json out.json]
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path("/Users/nima/Documents/Code/FG-Metal")
EVID = ROOT / "experiments/metalfx_frame_interpolator_R93/evidence"
SRC = EVID / "r119-live-throughput/observer-title-inprocess-r119-throughput.m"
DYLIB = EVID / "r119-live-throughput/libfgmetal_r119_throughput_probe.dylib"
OUT = EVID / "r119-live-throughput"

# The R117 optimisation is defined by the sample-outer accumulation arrays.
R117_MARKERS = (
    "int2 shifts[81];",
    "float costSum[81];",
    "float weightSum[81];",
    "costSum[k] += weight * abs(sampleY - b);",
)
# The R110 form calls the patch cost inside the candidate loop.
R110_MARKERS = (
    "r110FinePatchCost(previous, current, center, target, shift)",
    "r110FinePatchCost(previous, current, center, target, clamp(",
)

# Both forms keep these five parabola probes; their presence is expected in
# either version and is only reported, never used as a discriminator.
PARABOLA_PROBES = (
    "float cx = r110FinePatchCost(",
    "float cl = r110FinePatchCost(",
    "float cr = r110FinePatchCost(",
    "float cu = r110FinePatchCost(",
    "float cd = r110FinePatchCost(",
)

C_ESCAPES = {"n": "\n", "t": "\t", "\\": "\\", '"': '"', "'": "'", "0": "\0"}


def decode_c_literals(block: str) -> str:
    """Decode the concatenated C string literals in `block` into one string."""
    pieces = re.findall(r'"((?:[^"\\]|\\.)*)"', block)
    out = []
    for piece in pieces:
        buf = []
        i = 0
        while i < len(piece):
            ch = piece[i]
            if ch != "\\":
                buf.append(ch)
                i += 1
                continue
            nxt = piece[i + 1] if i + 1 < len(piece) else ""
            buf.append(C_ESCAPES.get(nxt, nxt))
            i += 2
        out.append("".join(buf))
    return "".join(out)


def extract_source_shader() -> tuple[str, int, int]:
    """Decode the estimator shader literal out of the observer source."""
    text = SRC.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    start = None
    for idx, line in enumerate(lines):
        if line.strip().startswith('"#include <metal_stdlib>') and idx > 4000:
            start = idx
            break
    if start is None:
        raise SystemExit("estimator shader literal not found in observer source")
    end = start
    while end < len(lines) and "R116_GATE_KERNEL_SOURCE" not in lines[end]:
        end += 1
    if end >= len(lines):
        raise SystemExit("shader literal terminator not found")
    block = "\n".join(lines[start:end])
    return decode_c_literals(block), start + 1, end + 1


def extract_binary_shaders() -> list[tuple[bytes, int]]:
    """Return every embedded Metal source literal in the compiled dylib.

    Deliberately all of them, not just the longest. If a build ever carried both
    an optimised and an unoptimised estimator, picking the largest literal would
    hide it; the marker scan below runs over every literal found.
    """
    blob = DYLIB.read_bytes()
    needle = b"#include <metal_stdlib>"
    found: list[tuple[bytes, int]] = []
    off = 0
    while True:
        off = blob.find(needle, off)
        if off < 0:
            break
        end = blob.find(b"\x00", off)
        if end < 0:
            break
        found.append((blob[off:end], off))
        off = end
    if not found:
        raise SystemExit("no embedded Metal source found in dylib")
    return found


def marker_report(text: str) -> dict[str, object]:
    return {
        "r117FormPresent": all(m in text for m in R117_MARKERS),
        "r117MarkersFound": [m for m in R117_MARKERS if m in text],
        "r117MarkersMissing": [m for m in R117_MARKERS if m not in text],
        "r110FormPresent": any(m in text for m in R110_MARKERS),
        "r110MarkersFound": [m for m in R110_MARKERS if m in text],
        "parabolaProbesPresent": sum(1 for p in PARABOLA_PROBES if p in text),
        "bytes": len(text),
        "sha256": hashlib.sha256(text.encode("utf-8", "surrogateescape")).hexdigest(),
    }


def main() -> int:
    json_out = None
    if "--json" in sys.argv:
        json_out = Path(sys.argv[sys.argv.index("--json") + 1])

    src_shader, first, last = extract_source_shader()
    literals = extract_binary_shaders()
    # The estimator literal is the one that contains the estimator kernels.
    estimator_literals = [
        (blob, off) for blob, off in literals if b"r110FineRefine" in blob
    ]
    if not estimator_literals:
        raise SystemExit("no literal containing r110FineRefine in dylib")
    bin_shader, offset = max(estimator_literals, key=lambda item: len(item[0]))
    bin_text = bin_shader.decode("utf-8", "surrogateescape")

    src_report = marker_report(src_shader)
    bin_report = marker_report(bin_text)
    # Scan EVERY literal, not only the chosen one.
    all_literal_reports = [
        {"offset": off, "bytes": len(blob), **marker_report(blob.decode("utf-8", "surrogateescape"))}
        for blob, off in literals
    ]
    any_literal_with_r110_form = any(r["r110FormPresent"] for r in all_literal_reports)

    # The binary literal is the decoded source literal with the R116 gate
    # kernels spliced onto the end by the preprocessor macro. So the exact
    # statement is: the source literal is a prefix of the binary literal, and
    # the only thing appended is the gate source.
    prefix_match = bin_text.startswith(src_shader)
    appended = bin_text[len(src_shader):] if prefix_match else ""
    core_identical = prefix_match and appended.lstrip().startswith(
        ("struct R110GateGroupStats", "kernel void r110FastGate")
    )

    result = {
        "schema": "fgmetal-r119-estimator-build-verification-v1",
        "observerSource": str(SRC),
        "observerSourceLines": [first, last],
        "dylib": str(DYLIB),
        "dylibSha256": hashlib.sha256(DYLIB.read_bytes()).hexdigest(),
        "dylibEmbeddedShaderOffset": offset,
        "sourceLiteral": src_report,
        "binaryLiteral": bin_report,
        "embeddedLiteralCount": len(literals),
        "estimatorLiteralCount": len(estimator_literals),
        "anyEmbeddedLiteralHasR110Form": any_literal_with_r110_form,
        "allEmbeddedLiterals": all_literal_reports,
        "binaryLiteralStartsWithSource": prefix_match,
        "binaryLiteralAppendedBytes": len(appended),
        "binaryLiteralCoreMatchesSource": core_identical,
    }

    verdict = (
        bin_report["r117FormPresent"]
        and not bin_report["r110FormPresent"]
        and not any_literal_with_r110_form
        and core_identical
    )
    result["verdict"] = "R117_OPTIMIZED_ESTIMATOR_PRESENT" if verdict else "BUILD_MISMATCH"

    (OUT / "embedded-estimator-r119.metal").write_text(bin_text, encoding="utf-8")
    print(f"R119_BUILD_CHECK verdict={result['verdict']}")
    print(f"R119_BUILD_CHECK dylibSha256={result['dylibSha256']}")
    print(
        "R119_BUILD_CHECK binaryLiteral "
        f"r117Form={bin_report['r117FormPresent']} "
        f"r110Form={bin_report['r110FormPresent']} "
        f"parabolaProbes={bin_report['parabolaProbesPresent']} "
        f"bytes={bin_report['bytes']}"
    )
    print(
        "R119_BUILD_CHECK sourceLiteral "
        f"r117Form={src_report['r117FormPresent']} "
        f"r110Form={src_report['r110FormPresent']} "
        f"bytes={src_report['bytes']}"
    )
    print(f"R119_BUILD_CHECK coreMatch={core_identical} embeddedOffset={offset}")

    if json_out:
        json_out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
