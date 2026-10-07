#!/usr/bin/env python3
"""Preserve completed build provenance and deduplicate immutable binaries."""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
HOME = Path.home()
PLAN = REPO / "experiments/storage/storage-cleanup-plan.json"
INVENTORY = REPO / "experiments/storage/storage-inventory.json"
RECORDS = REPO / "experiments/storage/build-records"
ARTIFACT_ROOT = HOME / "Library/Caches/FGMetal/artifacts/sha256"
EXTENSIONS = {".dll", ".dylib", ".so", ".exe", ".a", ".lib", ".metallib", ".bin"}


def sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def xattr_signature(path: Path) -> list[tuple[str, str]] | None:
    if not hasattr(os, "listxattr"):
        return []
    try:
        names = sorted(os.listxattr(path, follow_symlinks=False))
        return [(name, hashlib.sha256(os.getxattr(path, name, follow_symlinks=False)).hexdigest())
                for name in names]
    except (OSError, TypeError):
        return None


def mode_signature(path: Path) -> tuple[int, int, int, int, tuple | None]:
    info = path.stat()
    return (stat.S_IMODE(info.st_mode), info.st_uid, info.st_gid,
            getattr(info, "st_flags", 0), tuple(xattr_signature(path) or ()))


def prefix_path(path: Path) -> bool:
    for part in path.parts:
        low = part.lower()
        if low == "prefix" or low.startswith("prefix-") or low.endswith("-prefix") or "prefix-clean" in low:
            if "prefix-snapshot" not in low:
                return True
    return False


def file_records(build: Path) -> list[dict[str, Any]]:
    records = []
    for dirpath, dirnames, filenames in os.walk(build, followlinks=False):
        directory = Path(dirpath)
        dirnames[:] = [name for name in dirnames if not (directory / name).is_symlink()]
        for name in filenames:
            path = directory / name
            if path.suffix.lower() not in EXTENSIONS or path.is_symlink():
                continue
            try:
                info = path.stat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            records.append({"path": str(path), "name": path.name, "size_bytes": info.st_size,
                            "allocated_bytes": info.st_blocks * 512, "sha256": sha(path),
                            "mode": oct(stat.S_IMODE(info.st_mode)), "hard_link_count": info.st_nlink})
    return records


def parse_meson(build: Path) -> dict[str, Any]:
    log = build / "meson-logs/meson-log.txt"
    cmdline = build / "meson-private/cmd_line.txt"
    options = build / "meson-info/intro-buildoptions.json"
    compilers = build / "meson-info/intro-compilers.json"
    result: dict[str, Any] = {"kind": "meson", "build_path": str(build)}
    log_text = log.read_text(errors="replace") if log.is_file() else ""
    cmd_text = cmdline.read_text(errors="replace") if cmdline.is_file() else ""
    source_match = re.search(r"^Source dir:\s*(.+)$", log_text, re.MULTILINE)
    source = Path(source_match.group(1).strip()) if source_match else None
    cross_match = re.search(r"--cross-file(?:=|\s+)([^\s]+)", cmd_text + "\n" + log_text)
    cross = Path(cross_match.group(1).strip("'\"")) if cross_match else None

    compiler_versions: dict[str, Any] = {}
    if compilers.is_file():
        try:
            parsed = json.loads(compilers.read_text())
            compiler_versions = {
                machine: {name: value.get("version") for name, value in items.items()}
                for machine, items in parsed.items() if isinstance(items, dict)
            }
        except (ValueError, AttributeError):
            compiler_versions = {"parse_error": True}
    if not compiler_versions:
        compiler_versions = {"from_meson_log": sorted(set(re.findall(
            r"(?:gcc|clang|g\+\+|clang\+\+)\s+([0-9]+(?:\.[0-9]+)+)", log_text, re.IGNORECASE))) }

    source_identity: dict[str, Any] = {"path": str(source) if source else None,
                                       "git_commit": None, "git_tree": None,
                                       "content_tree_sha256": None}
    if source and source.is_dir():
        commit = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                check=False)
        if commit.returncode == 0:
            source_identity["git_commit"] = commit.stdout.strip()
            tree = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD^{tree}"],
                                  text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                  check=False)
            if tree.returncode == 0:
                source_identity["git_tree"] = tree.stdout.strip()
        # A source snapshot may not contain .git. Hash path, mode, and each file digest.
        if source_identity["git_tree"] is None:
            aggregate = hashlib.sha256()
            for dirpath, dirnames, filenames in os.walk(source, followlinks=False):
                directory = Path(dirpath)
                dirnames[:] = sorted(name for name in dirnames if not (directory / name).is_symlink())
                for name in sorted(filenames):
                    path = directory / name
                    try:
                        info = path.lstat()
                    except OSError:
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        continue
                    relative = path.relative_to(source).as_posix()
                    aggregate.update(relative.encode())
                    aggregate.update(b"\0")
                    aggregate.update(oct(stat.S_IMODE(info.st_mode)).encode())
                    aggregate.update(b"\0")
                    aggregate.update(sha(path).encode())
                    aggregate.update(b"\n")
            source_identity["content_tree_sha256"] = aggregate.hexdigest()

    cross_record = None
    if cross and cross.is_file():
        cross_record = {"path": str(cross), "sha256": sha(cross), "size_bytes": cross.stat().st_size}
    result.update({
        "source_identity": source_identity,
        "meson_options": json.loads(options.read_text()) if options.is_file() else None,
        "meson_cmd_line": cmd_text or None,
        "cross_file": cross_record,
        "compiler_versions": compiler_versions,
        "meson_log": ({"path": str(log), "size_bytes": log.stat().st_size,
                       "sha256": sha(log)} if log.is_file() else None),
    })
    return result


def source_related_metadata(build: Path) -> list[dict[str, str]]:
    evidence_roots = [
        REPO / "experiments/dxvk_macos_step11dR/evidence",
        HOME / ".codex/worktrees/step11d-r/FG-Metal/experiments/dxvk_macos_step11dR/evidence",
        HOME / ".codex/worktrees/step-11d/FG-Metal/experiments/dxvk_macos_step11d/evidence",
    ]
    lower = build.name.lower()
    tokens = {lower}
    for family in ("current-r3", "current-r4", "current-r5", "current-r6",
                   "11d-final-a", "11d-final-b", "repro-a", "repro-b",
                   "exact-build", "cache-build-traced", "diag2"):
        if family in lower:
            tokens.add(family)
    matched: list[dict[str, str]] = []
    for root in evidence_roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*.json"):
            if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                continue
            if any(token in path.as_posix().lower() for token in tokens):
                matched.append({"path": str(path), "sha256": sha(path)})
    return matched


def save_build_record(build: Path, explicit_mixed: bool = False) -> dict[str, Any]:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", build.name)
    target = RECORDS / slug
    target.mkdir(parents=True, exist_ok=True)
    outputs = file_records(build)
    metadata = parse_meson(build) if (build / "meson-logs/meson-log.txt").is_file() else {
        "kind": "non-meson-build-or-runtime", "build_path": str(build),
    }
    copied_metadata: list[dict[str, Any]] = []
    for relative in ("meson-private/cmd_line.txt", "meson-info/intro-buildoptions.json",
                     "meson-info/intro-compilers.json", "meson-info/intro-buildsystem_files.json",
                     "meson-info/intro-projectinfo.json"):
        source = build / relative
        if source.is_file():
            dest = target / relative.replace("/", "__")
            shutil.copy2(source, dest)
            copied_metadata.append({"source": str(source), "copy": str(dest),
                                    "sha256": sha(dest), "size_bytes": dest.stat().st_size})
    log = build / "meson-logs/meson-log.txt"
    if log.is_file():
        dest = target / "meson-log.txt.gz"
        with log.open("rb") as src, gzip.open(dest, "wb", compresslevel=6) as dst:
            shutil.copyfileobj(src, dst)
        copied_metadata.append({"source": str(log), "copy": str(dest),
                                "sha256": sha(dest), "size_bytes": dest.stat().st_size,
                                "source_sha256": sha(log), "compression": "gzip"})
    existing_results = []
    for candidate in (build / "result.json", build / "manifest.json"):
        if candidate.is_file():
            dest = target / candidate.name
            shutil.copy2(candidate, dest)
            existing_results.append({"source": str(candidate), "copy": str(dest), "sha256": sha(dest)})
    for name in ("intro-buildoptions.json", "intro-compilers.json"):
        # Preserve the option/compiler documents under stable names as well as the copied source path.
        source = build / "meson-info" / name
        if source.is_file():
            dest = target / name
            if not dest.exists():
                shutil.copy2(source, dest)

    record_path = target / "build-record.json"
    prior_outputs: dict[str, dict[str, Any]] = {}
    if record_path.is_file():
        try:
            old_record = json.loads(record_path.read_text(encoding="utf-8"))
            prior_outputs = {row["path"]: row for row in old_record.get("binary_outputs", [])}
        except (OSError, ValueError, KeyError):
            prior_outputs = {}
    current_by_path = {row["path"]: row for row in outputs}
    current_by_path.update({path: row for path, row in prior_outputs.items() if path not in current_by_path})
    outputs = list(current_by_path.values())
    record = {
        "schema_version": 1,
        "build_id": build.name,
        "build_path": str(build),
        "build_directory_category": "COMPLETED_BUILD_PENDING_REMOVAL" if not explicit_mixed else "MIXED_SUBTREE_PENDING_REMOVAL",
        "recorded_at_unix": time.time(),
        "identity": metadata,
        "associated_evidence_manifests": source_related_metadata(build),
        "binary_outputs": outputs,
        "metadata_copies": copied_metadata,
        "result_and_manifest_copies": existing_results,
        "retention": "Build directory is removable after each binary hash resolves to the canonical artifact store and this record exists.",
    }
    record_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"build_path": str(build), "record_path": str(record_path),
            "binary_count": len(outputs), "binary_hashes": [row["sha256"] for row in outputs]}


def canonical_source_priority(path: Path) -> tuple[int, int, str]:
    low = path.as_posix().lower()
    if "/experiments/" in low and "/evidence/" in low:
        rank = 0
    elif "/library/caches/fgmetal/" in low:
        rank = 1
    elif "/library/caches/" in low and "runtime" in low:
        rank = 2
    elif "/private/tmp/" in low and "build" in low:
        rank = 5
    else:
        rank = 3
    return (rank, len(str(path)), str(path))


def canonicalize_group(digest: str, paths: list[Path]) -> dict[str, Any]:
    paths = sorted({path for path in paths if path.is_file() and not path.is_symlink()},
                   key=canonical_source_priority)
    if not paths:
        return {"sha256": digest, "status": "NO_CURRENT_PATH"}
    store_device = ARTIFACT_ROOT.stat().st_dev
    external_volume_paths = [path for path in paths if path.stat().st_dev != store_device]
    paths = [path for path in paths if path.stat().st_dev == store_device]
    if not paths:
        return {"sha256": digest, "status": "EXTERNAL_FILESYSTEM_VIEW",
                "paths_not_in_data_volume_inventory": [str(path) for path in external_volume_paths]}
    actual_paths = []
    for path in paths:
        actual = sha(path)
        if actual != digest:
            raise RuntimeError(f"hash changed since inventory: {path} expected={digest} actual={actual}")
        actual_paths.append(path)
    representative = actual_paths[0]
    store_dir = ARTIFACT_ROOT / digest
    store_dir.mkdir(parents=True, exist_ok=True)
    canonical = store_dir / representative.name
    if canonical.exists():
        if canonical.is_symlink() or sha(canonical) != digest:
            raise RuntimeError(f"canonical store collision or corruption: {canonical}")
    else:
        os.link(representative, canonical)
    if sha(canonical) != digest:
        raise RuntimeError(f"canonical artifact failed post-link validation: {canonical}")

    inode_before = {(path.stat().st_dev, path.stat().st_ino) for path in actual_paths}
    linked_paths = 0
    skipped = []
    link_kinds: dict[str, str] = {}
    original_metadata = {str(path): repr(mode_signature(path)) for path in actual_paths}
    for path in actual_paths:
        if path == canonical:
            link_kinds[str(path)] = "canonical-file"
            continue
        try:
            if path.is_symlink() and Path(os.readlink(path)) == canonical:
                link_kinds[str(path)] = "absolute-symlink"
                continue
            if os.stat(path).st_dev == os.stat(canonical).st_dev and os.stat(path).st_ino == os.stat(canonical).st_ino:
                link_kinds[str(path)] = "hardlink"
                continue
            temp = path.with_name(f".{path.name}.canonical-link-{os.getpid()}")
            if temp.exists() or temp.is_symlink():
                raise FileExistsError(temp)
            if mode_signature(path) == mode_signature(canonical):
                os.link(canonical, temp)
                link_kind = "hardlink"
            else:
                os.symlink(str(canonical), temp)
                link_kind = "absolute-symlink"
            os.replace(temp, path)
            linked_paths += 1
            link_kinds[str(path)] = link_kind
        except OSError as exc:
            skipped.append({"path": str(path), "error": repr(exc)})

    manifest = {
        "schema_version": 1,
        "sha256": digest,
        "canonical_path": str(canonical),
        "canonical_name": canonical.name,
        "byte_size": canonical.stat().st_size,
        "mode": oct(stat.S_IMODE(canonical.stat().st_mode)),
        "source_path": str(representative),
        "original_paths": [str(path) for path in actual_paths],
        "paths_relinked_to_canonical_inode": linked_paths,
        "path_link_kinds": link_kinds,
        "original_metadata_signatures": original_metadata,
        "paths_not_relinked": skipped,
        "created_at_unix": time.time(),
    }
    metadata = store_dir / "manifest.json"
    metadata.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    after_inode_count = len({(os.stat(path).st_dev, os.stat(path).st_ino)
                             for path in actual_paths if path.exists()})
    status = "CANONICALIZED_WITH_SYMLINKS" if any(
        kind == "absolute-symlink" for kind in link_kinds.values()
    ) else "CANONICALIZED"
    return {"sha256": digest, "canonical_path": str(canonical), "status": status,
            "path_count": len(actual_paths), "physical_inode_count_before": len(inode_before),
            "physical_inode_count_after": after_inode_count, "linked_paths": linked_paths,
            "skipped": skipped, "external_volume_paths_not_relinked": [str(path) for path in external_volume_paths],
            "mode": oct(stat.S_IMODE(canonical.stat().st_mode))}


def main() -> int:
    free_before = shutil.disk_usage(REPO).free
    plan = json.loads(PLAN.read_text(encoding="utf-8"))
    inventory = json.loads(INVENTORY.read_text(encoding="utf-8"))
    RECORDS.mkdir(parents=True, exist_ok=True)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)

    builds = [Path(action["path"]) for action in plan["exact_subtree_actions"]
              if action["category"] == "COMPLETED BUILD DIRECTORY"]
    mixed_builds = [Path(action["path"]) for action in plan["exact_subtree_actions"]
                    if action["path"].endswith("/MoltenVK/External/build")]
    build_records = [save_build_record(path) for path in builds if path.is_dir()]
    build_records.extend(save_build_record(path, explicit_mixed=True) for path in mixed_builds if path.is_dir())

    required_paths = {Path(path) for path in plan["required_artifact_paths"].values()
                      if Path(path).suffix.lower() in EXTENSIONS}
    groups: dict[str, list[Path]] = {}
    for row in inventory["large_objects"]:
        path = Path(row["path"])
        if row.get("category") == "REGENERATABLE" or prefix_path(path):
            continue
        if path.suffix.lower() not in EXTENSIONS or not row.get("sha256"):
            continue
        groups.setdefault(row["sha256"], []).append(path)
    for path in required_paths:
        if not path.is_file() or path.is_symlink():
            raise FileNotFoundError(f"required artifact path is unavailable: {path}")
        groups.setdefault(sha(path), []).append(path)
    for build in builds + mixed_builds:
        if build.is_dir():
            for row in file_records(build):
                groups.setdefault(row["sha256"], []).append(Path(row["path"]))
    artifact_results = []
    for digest, paths in sorted(groups.items()):
        artifact_results.append(canonicalize_group(digest, paths))
    for directory in sorted(ARTIFACT_ROOT.glob("*/"), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()

    free_after = shutil.disk_usage(REPO).free
    report = {
        "schema_version": 1,
        "created_at_unix": time.time(),
        "artifact_store_root": str(ARTIFACT_ROOT),
        "free_before_bytes": free_before,
        "free_after_bytes": free_after,
        "measured_free_delta_bytes": free_after - free_before,
        "build_records": build_records,
        "artifact_count": len(artifact_results),
        "artifacts": artifact_results,
        "deduplication_note": "Every verified immutable binary path listed by the inventory outside Wine prefixes was rehashed before hardlinking. Files whose mode/owner/flags/xattrs differed or whose filesystem disallowed hardlinking remain separate and are listed as skipped.",
        "apfs_note": "The free-space delta is measured with disk usage; APFS clone sharing can make inode allocation estimates differ.",
    }
    report_path = REPO / "experiments/storage/artifactization-report.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"report": str(report_path), "build_records": len(build_records),
                      "artifacts": len(artifact_results), "free_before": free_before,
                      "free_after": free_after, "free_delta": free_after - free_before}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
