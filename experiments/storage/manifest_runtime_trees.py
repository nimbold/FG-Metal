#!/usr/bin/env python3
"""Record and compare exact Wine runtime trees before removing duplicates."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
OUT = REPO / "experiments/storage/runtime-manifests"
RUNTIMES = {
    "step11b_runtime_candidate": Path("/private/tmp/fgmetal-step11b/runtime-candidate/wine"),
    "step11_audit_runtime": Path("/private/tmp/fgmetal-dxvk-macos-audit-run/runtime/wine"),
    "step11c1r_frozen_runtime": Path("/Users/nima/Library/Caches/FGMetalStep11C1R/runtime-frozen/runtime/wine"),
    "step11d_r_active_runtime": Path("/Users/nima/Library/Caches/FGMetalStep11D-R/runtime-vulkan-overlay/wine"),
}


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def manifest_tree(label: str, root: Path) -> dict[str, Any]:
    if not root.is_dir() or root.is_symlink():
        raise FileNotFoundError(f"expected runtime directory is missing or unsafe: {root}")
    rows: list[dict[str, Any]] = []
    hash_cache: dict[tuple[int, int], str] = {}
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        current = Path(directory)
        dirnames[:] = sorted(name for name in dirnames if not (current / name).is_symlink())
        for name in sorted(filenames):
            path = current / name
            rel = path.relative_to(root).as_posix()
            info = path.lstat()
            base = {"relative_path": rel, "mode": oct(stat.S_IMODE(info.st_mode)),
                    "mtime_ns": info.st_mtime_ns}
            if stat.S_ISLNK(info.st_mode):
                row = {**base, "kind": "symlink", "target": os.readlink(path)}
            elif stat.S_ISREG(info.st_mode):
                inode = (info.st_dev, info.st_ino)
                if inode not in hash_cache:
                    hash_cache[inode] = sha(path)
                digest = hash_cache[inode]
                row = {**base, "kind": "file", "size_bytes": info.st_size,
                       "allocated_bytes": getattr(info, "st_blocks", 0) * 512,
                       "hard_link_count": info.st_nlink, "sha256": digest}
            else:
                row = {**base, "kind": "other"}
            rows.append(row)
    rows.sort(key=lambda row: row["relative_path"])
    aggregate = hashlib.sha256()
    for row in rows:
        aggregate.update(json.dumps(row, sort_keys=True, separators=(",", ":")).encode())
        aggregate.update(b"\n")
    result = {"label": label, "path": str(root), "created_at_unix": time.time(),
              "file_entry_count": len(rows), "tree_sha256": aggregate.hexdigest(),
              "entries": rows}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{label}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["manifest_path"] = str(path)
    return result


def main() -> int:
    trees = {label: manifest_tree(label, root) for label, root in RUNTIMES.items()}
    active = {row["relative_path"]: row for row in trees["step11d_r_active_runtime"]["entries"]}
    comparisons: dict[str, Any] = {}
    for label, tree in trees.items():
        if label == "step11d_r_active_runtime":
            continue
        entries = {row["relative_path"]: row for row in tree["entries"]}
        different = []
        for path in sorted(set(entries) | set(active)):
            left, right = entries.get(path), active.get(path)
            if left is None or right is None or left.get("kind") != right.get("kind"):
                different.append({"relative_path": path, "reason": "presence_or_type",
                                 "candidate": left, "active": right})
            elif left.get("kind") == "symlink":
                if left.get("target") != right.get("target"):
                    different.append({"relative_path": path, "reason": "symlink_target",
                                     "candidate": left, "active": right})
            elif left.get("sha256") != right.get("sha256") or left.get("mode") != right.get("mode"):
                different.append({"relative_path": path, "reason": "payload_or_mode",
                                 "candidate": left, "active": right})
        comparisons[label] = {"compared_to": "step11d_r_active_runtime",
                              "candidate_entry_count": len(entries),
                              "active_entry_count": len(active),
                              "different_entry_count": len(different),
                              "different_entries": different}
    comparison = {"schema_version": 1, "created_at_unix": time.time(),
                  "runtime_manifests": {label: {k: v for k, v in tree.items() if k != "entries"}
                                        for label, tree in trees.items()},
                  "comparisons": comparisons}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / "comparison.json"
    path.write_text(json.dumps(comparison, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"comparison": str(path),
                      "tree_manifests": {k: v["manifest_path"] for k, v in trees.items()},
                      "different_counts": {k: v["different_entry_count"] for k, v in comparisons.items()},
                      "unique_payload_hashes": {k: len({r.get('sha256') for r in trees[k]['entries'] if r.get('sha256')}) for k in trees}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
