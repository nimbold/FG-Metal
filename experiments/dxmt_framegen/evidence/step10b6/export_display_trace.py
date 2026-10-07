#!/usr/bin/env python3
"""Export the four Step 10B.6 Display/Core Animation tables from one trace."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


TABLES = {
    "display-vsyncs-interval": "display-vsyncs-interval.xml",
    "displayed-surfaces-interval": "displayed-surfaces-interval.xml",
    "ca-client-present-request": "ca-client-present-request.xml",
    "ca-client-presented-handler": "ca-client-presented-handler.xml",
}


def run_xctrace(*args: str) -> None:
    subprocess.run(["xcrun", "xctrace", *args], check=True)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    if len(sys.argv) != 3:
        print(f"usage: {Path(sys.argv[0]).name} TRACE.trace OUTPUT_DIR", file=sys.stderr)
        return 2
    trace = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    if not trace.exists():
        print(f"missing trace: {trace}", file=sys.stderr)
        return 2
    output.mkdir(parents=True, exist_ok=True)
    output_names = {"xctrace-toc.xml", *TABLES.values(), "evidence.sha256"}
    conflicts = [output / name for name in output_names if (output / name).exists()]
    if conflicts:
        print(f"refusing to overwrite existing evidence files: {conflicts}", file=sys.stderr)
        return 2

    toc_path = output / "xctrace-toc.xml"
    run_xctrace("export", str(trace), "--toc", "--output", str(toc_path))
    toc = ET.parse(toc_path).getroot()
    table_nodes = toc.findall("./run[1]/data/table")
    indices: dict[str, list[int]] = {}
    for index, node in enumerate(table_nodes, start=1):
        schema = node.get("schema")
        if schema in TABLES:
            indices.setdefault(schema, []).append(index)
    missing = set(TABLES) - set(indices)
    ambiguous = {name: values for name, values in indices.items() if len(values) != 1}
    if missing or ambiguous:
        raise RuntimeError(f"required Display table selection failed: missing={sorted(missing)} ambiguous={ambiguous}")

    project_root = Path(__file__).resolve().parents[4]
    for schema, filename in TABLES.items():
        path = output / filename
        xpath = f"//trace-toc[1]/run[1]/data[1]/table[{indices[schema][0]}]"
        run_xctrace("export", str(trace), "--xpath", xpath, "--output", str(path))
        parsed = ET.parse(path).getroot()
        names = [node.get("name") for node in parsed.iter("schema")]
        row_count = sum(1 for _ in parsed.iter("row"))
        if schema not in names or row_count == 0:
            raise RuntimeError(f"unreadable or empty export: {path}; schemas={names}; rows={row_count}")
        print(f"{schema}: rows={row_count} bytes={path.stat().st_size}")

    files = sorted(p for p in output.iterdir() if p.is_file() and p.name != "evidence.sha256")
    with (output / "evidence.sha256").open("w", encoding="utf-8") as manifest:
        for path in files:
            manifest.write(f"{sha256(path)}  {path.relative_to(project_root).as_posix()}\n")
    print(f"sha256_manifest={output / 'evidence.sha256'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
