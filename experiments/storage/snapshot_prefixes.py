#!/usr/bin/env python3
"""Preserve Wine registry/configuration state before prefix cleanup."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tarfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
PLAN = REPO / "experiments/storage/storage-cleanup-plan.json"
INVENTORY = REPO / "experiments/storage/storage-inventory.json"
OUT = REPO / "experiments/storage/prefix-snapshots"
KEEP_REPRO = Path.home() / "Library/Caches/FGMetalStep11D-R/step11d2-diag-60s-07-submit-reclaim-split-prefix"


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def snapshot_name(path: Path) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", f"{path.parent.name}__{path.name}")


def main() -> int:
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    all_records = inventory.get("large_objects", [])
    OUT.mkdir(parents=True, exist_ok=True)
    summaries = []
    for action in plan["prefix_actions"]:
        prefix = Path(action["path"])
        if not prefix.is_dir() or prefix.is_symlink():
            raise RuntimeError(f"prefix is missing or unsafe to snapshot: {prefix}")
        name = snapshot_name(prefix)
        target = OUT / name
        if target.exists():
            raise FileExistsError(f"refusing to overwrite existing prefix snapshot: {target}")
        target.mkdir()
        registry_files = []
        archive = target / "registry-config.tar.gz"
        candidates = [prefix / item for item in ("system.reg", "user.reg", "userdef.reg",
                                                  "drive_c/windows/system.ini")]
        with tarfile.open(archive, "w:gz", compresslevel=6) as bundle:
            for path in candidates:
                if path.is_file() and not path.is_symlink():
                    bundle.add(path, arcname=path.relative_to(prefix).as_posix(), recursive=False)
                    registry_files.append({"path": path.relative_to(prefix).as_posix(),
                                           "size_bytes": path.stat().st_size,
                                           "sha256": sha(path)})

        dosdevices: dict[str, str] = {}
        links = prefix / "dosdevices"
        if links.is_dir() and not links.is_symlink():
            for path in sorted(links.iterdir()):
                if path.is_symlink():
                    dosdevices[path.name] = os.readlink(path)

        contained_binaries = [
            {key: row.get(key) for key in ("path", "logical_size_bytes", "allocated_size_bytes",
                                           "mtime_epoch", "sha256", "hard_link_count")}
            for row in all_records
            if Path(row["path"]).is_relative_to(prefix)
            and Path(row["path"]).suffix.lower() in {".dll", ".exe", ".so", ".dylib", ".lib", ".a"}
        ]
        manifest: dict[str, Any] = {
            "source_path": str(prefix),
            "source_root_kind": "PRESERVED_FAILURE_REPRODUCTION" if prefix == KEEP_REPRO else "COMPLETED_DISPOSABLE_PREFIX",
            "plan_decision": action["decision"],
            "reason": action.get("reason"),
            "snapshot_time_unix": time.time(),
            "estimated_prefix_bytes": action["estimated"],
            "registry_files": registry_files,
            "registry_archive": {"path": archive.name, "size_bytes": archive.stat().st_size,
                                 "sha256": sha(archive)},
            "dosdevices_symlink_targets": dosdevices,
            "binary_path_hashes_from_storage_inventory": contained_binaries,
            "unique_evidence_note": "Run logs, manifests, source snapshots, and timing evidence remain in their original evidence directories; this snapshot preserves prefix registry/config state and binary identity references.",
        }
        (target / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                                               encoding="utf-8")
        if prefix == KEEP_REPRO:
            preserve = {
                "PRESERVE_PREFIX": True,
                "experiment_id": "step11d2-diag-60s-07-submit-reclaim-split",
                "reason": "Unique matched-stall failure reproduction for the 11D.2 source-ready queue-submit block.",
                "creation_time_unix": prefix.stat().st_ctime,
                "estimated_size_bytes": action["estimated"]["allocated_unique_inode_bytes"],
                "snapshot_manifest": str(target / "manifest.json"),
            }
            (prefix / "PRESERVE_PREFIX.json").write_text(json.dumps(preserve, indent=2) + "\n",
                                                        encoding="utf-8")
            (target / "PRESERVE_PREFIX.json").write_text(json.dumps(preserve, indent=2) + "\n",
                                                        encoding="utf-8")
        summaries.append({"prefix": str(prefix), "snapshot": str(target),
                          "registry_archive_bytes": archive.stat().st_size,
                          "binary_identity_count": len(contained_binaries),
                          "decision": action["decision"]})

    index = {"created_at_unix": time.time(), "prefix_count": len(summaries), "snapshots": summaries}
    (OUT / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"snapshot_index": str(OUT / "index.json"), "prefixes": len(summaries),
                      "kept_failure_prefix": str(KEEP_REPRO)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
