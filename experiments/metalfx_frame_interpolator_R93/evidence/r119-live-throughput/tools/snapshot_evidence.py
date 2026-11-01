#!/usr/bin/env python3
"""Byte-and-mtime snapshot of the FG-Metal evidence corpus.

Purpose: prove that a run wrote only into its own round directory. The snapshot
records, for every regular file under the corpus root, the size, the nanosecond
mtime and the SHA-256 of the full contents. Two snapshots taken before and after
a run must differ only inside the round directory that run was allowed to write.

Full-content hashing is deliberate. Size and mtime alone would miss a rewrite
that preserved both (e.g. an in-place overwrite of a same-length file followed by
a restored mtime), which is exactly the failure mode R118 exhibited on the R115
source raws.

Usage:
    snapshot_evidence.py --root <corpus> --out <json> [--exclude <subdir> ...]
    snapshot_evidence.py --compare <before.json> <after.json> [--allow <subdir> ...]
"""

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

CHUNK = 1 << 20


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def scan(root, excludes):
    entries = {}
    exclude_abs = [os.path.abspath(os.path.join(root, item)) for item in excludes]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            name for name in dirnames
            if os.path.abspath(os.path.join(dirpath, name)) not in exclude_abs
        )
        for name in sorted(filenames):
            full = os.path.join(dirpath, name)
            if os.path.abspath(full) in exclude_abs:
                continue
            try:
                stat = os.stat(full, follow_symlinks=False)
            except OSError:
                continue
            if not os.path.isfile(full) or os.path.islink(full):
                continue
            entries[os.path.relpath(full, root)] = {
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
    return entries


def hash_all(root, entries, workers):
    def worker(item):
        relative = item
        try:
            return relative, sha256_file(os.path.join(root, relative))
        except OSError:
            return relative, None

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for relative, digest in pool.map(worker, entries.keys()):
            entries[relative]["sha256"] = digest
    return entries


def command_snapshot(args):
    started = time.time()
    entries = scan(args.root, args.exclude)
    if not args.no_hash:
        entries = hash_all(args.root, entries, args.workers)
    total = sum(entry["size"] for entry in entries.values())
    payload = {
        "root": os.path.abspath(args.root),
        "generated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "file_count": len(entries),
        "total_bytes": total,
        "hashed": not args.no_hash,
        "excluded": args.exclude,
        "elapsed_seconds": round(time.time() - started, 3),
        "files": entries,
    }
    with open(args.out, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=1, sort_keys=True)
    print(f"SNAPSHOT root={payload['root']} files={payload['file_count']} "
          f"bytes={payload['total_bytes']} hashed={payload['hashed']} "
          f"elapsed={payload['elapsed_seconds']}s out={args.out}")
    return 0


def command_compare(args):
    with open(args.before, encoding="utf-8") as handle:
        before = json.load(handle)
    with open(args.after, encoding="utf-8") as handle:
        after = json.load(handle)
    allowed = [os.path.abspath(item) for item in args.allow]
    before_files = before["files"]
    after_files = after["files"]

    added, removed, changed = [], [], []
    for relative in sorted(after_files):
        if relative not in before_files:
            added.append(relative)
            continue
        old, new = before_files[relative], after_files[relative]
        if old.get("size") != new.get("size") or old.get("mtime_ns") != new.get("mtime_ns"):
            changed.append(relative)
        elif old.get("sha256") and new.get("sha256") and old["sha256"] != new["sha256"]:
            changed.append(relative)
    for relative in sorted(before_files):
        if relative not in after_files:
            removed.append(relative)

    def permitted(relative):
        full = os.path.abspath(os.path.join(after["root"], relative))
        return any(full == item or full.startswith(item + os.sep) for item in allowed)

    violations = [item for item in added + removed + changed if not permitted(item)]
    permitted_changes = [item for item in added + removed + changed if permitted(item)]

    verdict = "UNCHANGED_OUTSIDE_ALLOWED" if not violations else "VIOLATION"
    print(f"COMPARE before={before['file_count']} after={after['file_count']} "
          f"added={len(added)} removed={len(removed)} changed={len(changed)}")
    print(f"COMPARE allowed_roots={len(allowed)} allowed_changes={len(permitted_changes)} "
          f"violations={len(violations)}")
    print(f"EVIDENCE_ISOLATION={verdict}")
    for item in violations[:50]:
        print(f"  VIOLATION {item}")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump({
                "verdict": verdict,
                "added": added,
                "removed": removed,
                "changed": changed,
                "allowed_changes": permitted_changes,
                "violations": violations,
                "before": before["generated_utc"],
                "after": after["generated_utc"],
            }, handle, indent=1)
    return 0 if not violations else 1


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    snap = sub.add_parser("snapshot")
    snap.add_argument("--root", required=True)
    snap.add_argument("--out", required=True)
    snap.add_argument("--exclude", action="append", default=[])
    snap.add_argument("--workers", type=int, default=8)
    snap.add_argument("--no-hash", action="store_true")
    snap.set_defaults(func=command_snapshot)

    comp = sub.add_parser("compare")
    comp.add_argument("before")
    comp.add_argument("after")
    comp.add_argument("--allow", action="append", default=[])
    comp.add_argument("--json")
    comp.set_defaults(func=command_compare)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
