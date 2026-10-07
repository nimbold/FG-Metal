#!/usr/bin/env python3
"""Build the bounded FG-Metal storage inventory without following symlinks."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import stat
import subprocess
import time
from collections import Counter
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
HOME = Path.home()
TEMP_ROOT = Path("/private/tmp")
OUT = REPO / "experiments/storage/storage-inventory.json"
ARTIFACT_EXTENSIONS = {
    ".dll", ".dylib", ".so", ".exe", ".a", ".lib", ".metallib", ".bin",
}
DIR_KEYWORDS = ("prefix", "build", "runtime", "evidence", "artifact", "source", "toolchain")
ROOT_LABELS = {
    "FG-Metal": "MAIN_CHECKOUT",
}


def discover_roots() -> list[tuple[Path, str]]:
    roots: dict[str, str] = {str(REPO): "MAIN_CHECKOUT"}
    listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=REPO,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    ).stdout
    for line in listing.splitlines():
        if line.startswith("worktree "):
            root = Path(line.removeprefix("worktree "))
            if root == REPO:
                continue
            if root.is_symlink():
                raise RuntimeError(f"registered Git worktree path is a symlink: {root}")
            if not root.is_dir():
                raise RuntimeError(f"registered Git worktree path is missing: {root}")
            roots[str(root)] = "GIT_WORKTREE"

    caches = HOME / "Library/Caches"
    project_cache_roots: list[Path] = []
    if caches.is_dir():
        for path in caches.iterdir():
            lower = path.name.lower()
            if path.is_dir() and (
                lower.startswith(("fgmetal", "fg-metal", "fgmetal-", "fg-metal-"))
                or lower == "lsfg-metal"
            ):
                roots[str(path)] = "PROJECT_CACHE"
                if (lower.startswith(("fgmetal", "fg-metal", "fgmetal-", "fg-metal-"))
                        or lower == "lsfg-metal"):
                    project_cache_roots.append(path)

    tmp = TEMP_ROOT
    if tmp.is_dir():
        project_stems = ("fgmetal", "fg-metal", "fg-", "dxmt-", "dxvk-",
                         "framegen-", "step10a", "step11b")
        for path in tmp.iterdir():
            lower = path.name.lower()
            if path.is_dir() and lower.startswith(project_stems):
                roots[str(path)] = "PROJECT_TEMP"
        # Use the same recursive, no-follow build discovery as the retention
        # gate so unbranded nested compiler roots participate in storage totals.
        storage_scripts = REPO / "experiments/storage"
        if str(storage_scripts) not in __import__("sys").path:
            __import__("sys").path.insert(0, str(storage_scripts))
        import build_retention
        for path in build_retention._all_root_candidates():
            if path.is_symlink():
                # Retention treats an external directory alias as UNKNOWN and
                # blocks Gate 0.  Inventory must also refuse to claim complete
                # totals when it cannot inspect the target without traversal.
                raise RuntimeError(f"storage inventory cannot account for unresolved build-root alias: {path}")
            try:
                path.relative_to(tmp)
            except ValueError:
                containing_cache = next((cache_root for cache_root in project_cache_roots
                                         if path != cache_root and cache_root in path.parents), None)
                if containing_cache is None:
                    continue
                kind = "CACHE_BUILD_ROOT_SYMLINK" if path.is_symlink() else "CACHE_BUILD_ROOT"
            else:
                kind = "TEMP_BUILD_ROOT_SYMLINK" if path.is_symlink() else "TEMP_BUILD_ROOT"
            if path.is_dir():
                roots[str(path)] = kind
    derived = HOME / "Library/Developer/Xcode/DerivedData"
    if derived.is_dir():
        for path in derived.iterdir():
            if path.is_dir() and not path.is_symlink() and path.name.lower().startswith("moltenvkpackaging-"):
                roots[str(path)] = "XCODE_DERIVED_DATA_BUILD"
    return sorted(((Path(path), kind) for path, kind in roots.items()), key=lambda item: item[0].as_posix())


def owner_for(path: Path, root: Path) -> str:
    rel = path.relative_to(root)
    parts = rel.parts
    for part in parts:
        if re.search(r"(?:step|r)[0-9]+|step-?[0-9]", part, re.IGNORECASE):
            return part
    if "evidence" in parts:
        i = parts.index("evidence")
        return parts[i + 1] if len(parts) > i + 1 else root.name
    return root.name


def classification(path: Path, root_kind: str) -> tuple[str, str]:
    low = path.as_posix().lower()
    if any("dxmt" in part.lower() or "framegen" in part.lower() for part in path.parts):
        return "OTHER PROJECT / PROTECTED WORKSTREAM", "DO_NOT_DELETE_OTHER_PROJECT_DATA"
    if Path("/private/tmp/fgmetal-step11d2-bridge-diagnostic-c-20261006") in path.parents or path == Path(
            "/private/tmp/fgmetal-step11d2-bridge-diagnostic-c-20261006"):
        return "PRESERVED EVIDENCE", "HISTORICAL_STEP11D2_SOURCE_EVIDENCE_CORPUS_HASHED_BY_BUILD_RETENTION"
    if "/artifacts/sha256/" in low:
        return "CANONICAL ARTIFACT", "PROTECT_CANONICAL_HASH_OBJECT"
    if "lsfg-metal" in low:
        return "REQUIRED ACTIVE", "PROTECT_USER_LSFG_CACHE"
    if "/.git/" in low or "/evidence/" in low or "source-snapshot" in low or "cleanup-" in low:
        return "PRESERVED EVIDENCE", "PRESERVE_OR_HARDLINK_DEDUP"
    if "/experiments/storage/" in low:
        return "PRESERVED EVIDENCE", "PRESERVE_GATE_MANIFESTS_AND_CLEANUP_RECEIPTS"
    if ("fgmetalstep11d-r/runtime-vulkan-overlay/wine" in low
            or "fgmetalstep11c1r/moltenvk-1.4.2/" in low
            or "fgmetalstep11c1r/moltenvk-loader-override/" in low):
        return "REQUIRED ACTIVE", "KEEP_PINNED_R5_RUNTIME_AND_MOLTENVK_CANDIDATE"
    if any(part.lower() == "prefix" or part.lower().startswith("prefix-")
           or part.lower().endswith("-prefix") or "prefix-clean" in part.lower()
           for part in path.parts):
        return "REGENERATABLE", "SNAPSHOT_REGISTRY_THEN_REVIEW_FOR_DELETE"
    generated_build_root = any(
        re.search(r"(?:^|-)build(?:-[a-z0-9]+)*$", part.lower())
        and "unmodified" not in part.lower()
        for part in path.parts
    )
    if "/build/" in low or generated_build_root:
        return "REGENERATABLE", "ARTIFACTIZE_AND_VERIFY_SOURCE_PROVENANCE_BEFORE_DELETE"
    if "/package/release/" in low:
        return "REGENERATABLE", "REBUILDABLE_RELEASE_OUTPUT_HOLD_UNTIL_ARTIFACTIZED"
    if "runtime" in low or "/wine" in low:
        return "REQUIRED ACTIVE", "KEEP_UNTIL_CANONICAL_RUNTIME_SELECTED"
    if root_kind in {"MAIN_CHECKOUT", "GIT_WORKTREE"}:
        return "REQUIRED ACTIVE", "PROTECT_USER_WORKTREE"
    return "PRESERVED EVIDENCE", "HOLD_UNCLASSIFIED_OBJECT_UNTIL_PROVENANCE_IS_VERIFIED"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def mounted_images() -> dict[str, dict[str, Any]]:
    """Return hdiutil's verified mount-to-image bindings, including process IDs."""
    result = subprocess.run(["hdiutil", "info", "-plist"], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        raise RuntimeError("hdiutil could not enumerate mounted disk images")
    try:
        value = plistlib.loads(result.stdout)
    except Exception as error:
        raise RuntimeError(f"hdiutil returned invalid mount metadata: {error}") from error
    images: dict[str, dict[str, Any]] = {}
    for image in value.get("images", []):
        image_path = image.get("image-path")
        if not isinstance(image_path, str):
            continue
        for entity in image.get("system-entities", []):
            mount_path = entity.get("mount-point")
            if isinstance(mount_path, str):
                images[mount_path] = {
                    "image_path": image_path,
                    "pid": image.get("hdid-pid"),
                    "read_only": image.get("writeable") is False,
                    "image_type": image.get("image-type"),
                    "virtual_size_bytes": int(image.get("blockcount", 0)) * int(image.get("blocksize", 0)),
                }
    return images


def _unlinked_image_length(binding: dict[str, Any]) -> int | None:
    """Read the length of hdiutil's still-open unlinked backing file via lsof."""
    pid = binding.get("pid")
    image_path = binding.get("image_path")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not isinstance(image_path, str):
        return None
    result = subprocess.run(["lsof", "-nP", "-a", "-p", str(pid), "+L1", "-Fpcfsn"],
                            text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            check=False)
    if result.returncode not in (0, 1):
        return None
    entry: dict[str, str] = {}
    found: list[int] = []
    for line in result.stdout.splitlines():
        if not line:
            continue
        field, content = line[0], line[1:]
        if field == "f":
            if entry.get("n") == image_path and entry.get("s", "").isdigit():
                found.append(int(entry["s"]))
            entry = {"f": content}
        elif field in {"s", "n"}:
            entry[field] = content
    if entry.get("n") == image_path and entry.get("s", "").isdigit():
        found.append(int(entry["s"]))
    return found[0] if len(found) == 1 else None


def main() -> int:
    roots = discover_roots()
    all_roots: list[dict[str, Any]] = []
    large_paths: list[dict[str, Any]] = []
    large_directories: list[dict[str, Any]] = []
    global_seen: set[tuple[int, int]] = set()
    global_allocated = 0
    global_logical = 0
    global_path_files = 0
    global_unique_inodes = 0
    scoped_seen: dict[tuple[int, int], tuple[int, bool]] = {}
    fgmetal_scoped_allocated = 0
    unlinked_image_upper_bound = 0
    unaccounted_unlinked_images: list[str] = []
    other_project_allocated = 0
    binary_hashes: dict[str, list[str]] = {}
    image_backings = mounted_images()
    mounted_filesystems: dict[str, dict[str, Any]] = {}

    for root, root_kind in roots:
        if root.is_symlink():
            raise RuntimeError(f"storage inventory root is a symlink: {root}")
        if not root.is_dir():
            continue
        root_logical = 0
        root_allocated_paths = 0
        root_unique_allocated = 0
        root_files = 0
        root_dirs = 0
        root_mtime = root.stat().st_mtime
        root_device = root.stat().st_dev
        root_seen: set[tuple[int, int]] = set()
        dir_acc: dict[str, dict[str, Any]] = {}
        candidate_dirs: set[Path] = {root}
        files: list[tuple[Path, os.stat_result]] = []

        def scan_error(error: OSError) -> None:
            raise RuntimeError(f"storage inventory could not completely scan {root}: {error}") from error

        for dirpath, dirnames, filenames in os.walk(root, followlinks=False, onerror=scan_error):
            directory = Path(dirpath)
            descend = []
            for name in dirnames:
                child = directory / name
                if child.is_symlink():
                    continue
                try:
                    child_stat = child.stat()
                except OSError as error:
                    raise RuntimeError(f"storage inventory could not inspect {child}: {error}") from error
                if child_stat.st_dev != root_device:
                    if os.path.ismount(child):
                        vfs = os.statvfs(child)
                        binding = image_backings.get(str(child), {})
                        backing_path = Path(binding["image_path"]) if binding.get("image_path") else None
                        backing_exists = bool(backing_path and backing_path.is_file()
                                              and not backing_path.is_symlink())
                        backing_allocated = None
                        missing_backing_upper_bound = None
                        if backing_exists and backing_path is not None:
                            backing_info = backing_path.stat(follow_symlinks=False)
                            backing_allocated = getattr(backing_info, "st_blocks", 0) * 512
                        elif (backing_path is not None and binding.get("read_only") is True
                              and binding.get("pid") is not None):
                            # The image's directory entry can be unlinked while hdiutil
                            # continues to hold its vnode.  lsof's exact vnode length
                            # gives a conservative allocated-byte upper bound (plus
                            # one APFS allocation block) without counting mount contents
                            # twice as ordinary path inodes.
                            length = _unlinked_image_length(binding)
                            if length is None:
                                unaccounted_unlinked_images.append(str(backing_path))
                            else:
                                missing_backing_upper_bound = ((length + 4095) // 4096) * 4096
                                if str(backing_path).startswith(str(HOME / "Library/Caches") + "/"):
                                    unlinked_image_upper_bound += missing_backing_upper_bound
                                    fgmetal_scoped_allocated += missing_backing_upper_bound
                                else:
                                    missing_backing_upper_bound = None
                        mounted_filesystems[str(child)] = {
                            "mount_path": str(child),
                            "backing_image": str(backing_path) if backing_path else None,
                            "backing_image_exists": backing_exists,
                            "backing_image_allocated_bytes": backing_allocated,
                            "unlinked_backing_image_upper_bound_bytes": missing_backing_upper_bound,
                            "image_type": binding.get("image_type"),
                            "hdiutil_pid": binding.get("pid"),
                            "device": child_stat.st_dev,
                            "total_bytes": vfs.f_blocks * vfs.f_frsize,
                            "free_bytes": vfs.f_bavail * vfs.f_frsize,
                            "read_only": bool(vfs.f_flag & getattr(os, "ST_RDONLY", 1)),
                            "counted_in_project_totals": False,
                            "reason": ("Separate mounted filesystem view. Its backing image is counted by path when present; "
                                       "an unlinked open image is represented by a conservative vnode-length upper bound."),
                        }
                    continue
                descend.append(name)
            dirnames[:] = descend
            root_dirs += len(dirnames)
            if directory == root or len(directory.relative_to(root).parts) <= 1 or any(
                word in directory.name.lower() for word in DIR_KEYWORDS
            ):
                candidate_dirs.add(directory)
            for name in filenames:
                path = directory / name
                try:
                    info = path.lstat()
                except OSError as error:
                    raise RuntimeError(f"storage inventory could not inspect {path}: {error}") from error
                if not stat.S_ISREG(info.st_mode):
                    continue
                files.append((path, info))
                root_files += 1
                root_logical += info.st_size
                root_allocated_paths += getattr(info, "st_blocks", 0) * 512
                root_mtime = max(root_mtime, info.st_mtime)
                key = (info.st_dev, info.st_ino)
                in_scope = not any("dxmt" in part.lower() or "framegen" in part.lower()
                                   or "step10b6_1" in part.lower() for part in path.parts)
                if key not in scoped_seen:
                    allocated_bytes = getattr(info, "st_blocks", 0) * 512
                    scoped_seen[key] = (allocated_bytes, in_scope)
                    if in_scope:
                        fgmetal_scoped_allocated += allocated_bytes
                    else:
                        other_project_allocated += allocated_bytes
                elif in_scope and not scoped_seen[key][1]:
                    allocated_bytes = scoped_seen[key][0]
                    fgmetal_scoped_allocated += allocated_bytes
                    other_project_allocated -= allocated_bytes
                    scoped_seen[key] = (allocated_bytes, True)
                if key not in root_seen:
                    root_seen.add(key)
                    root_unique_allocated += getattr(info, "st_blocks", 0) * 512
                if key not in global_seen:
                    global_seen.add(key)
                    global_allocated += getattr(info, "st_blocks", 0) * 512
                    global_unique_inodes += 1
                global_logical += info.st_size
                global_path_files += 1

        # Record aggregate stats for project roots and semantically important subtrees.
        for directory in candidate_dirs:
            dir_acc[str(directory)] = {
                "path": str(directory), "logical_size_bytes": 0,
                "allocated_size_bytes_unique_within_directory": 0,
                "file_count": 0, "mtime_epoch": directory.stat().st_mtime,
                "_inodes": set(),
            }
        for path, info in files:
            key = (info.st_dev, info.st_ino)
            for parent in path.parents:
                if parent == root.parent:
                    break
                item = dir_acc.get(str(parent))
                if item is not None:
                    item["logical_size_bytes"] += info.st_size
                    item["file_count"] += 1
                    item["mtime_epoch"] = max(item["mtime_epoch"], info.st_mtime)
                    if key not in item["_inodes"]:
                        item["_inodes"].add(key)
                        item["allocated_size_bytes_unique_within_directory"] += getattr(info, "st_blocks", 0) * 512
                if parent == root:
                    break
            suffix = path.suffix.lower()
            should_hash = (suffix in ARTIFACT_EXTENSIONS and info.st_size >= 1024 * 1024)
            if not (info.st_size >= 16 * 1024 * 1024 or should_hash):
                continue
            category, deletability = classification(path, root_kind)
            digest = sha256(path) if should_hash or info.st_size >= 128 * 1024 * 1024 else None
            record = {
                "path": str(path),
                "logical_size_bytes": info.st_size,
                "allocated_size_bytes": getattr(info, "st_blocks", 0) * 512,
                "mtime_epoch": info.st_mtime,
                "category": category,
                "owner_experiment": owner_for(path, root),
                "sha256": digest,
                "hard_link_count": info.st_nlink,
                "deletability_classification": deletability,
                "filesystem_device": info.st_dev,
                "inode": info.st_ino,
            }
            large_paths.append(record)
            if digest and suffix in ARTIFACT_EXTENSIONS:
                binary_hashes.setdefault(digest, []).append(str(path))

        scoped_path = not any("dxmt" in part.lower() or "framegen" in part.lower()
                              or "step10b6_1" in part.lower()
                              for part in root.parts)
        container_category = (
            "REQUIRED ACTIVE" if root_kind in {"MAIN_CHECKOUT", "GIT_WORKTREE"}
            else "OTHER PROJECT / PROTECTED WORKSTREAM" if not scoped_path
            else "PRESERVED EVIDENCE"
        )
        all_roots.append({
            "path": str(root), "root_type": root_kind,
            "logical_apparent_bytes": root_logical,
            "allocated_path_bytes_including_hardlink_aliases": root_allocated_paths,
            "allocated_bytes_hardlinks_deduplicated_within_root": root_unique_allocated,
            "file_count": root_files, "directory_count": root_dirs,
            "mtime_epoch": root_mtime,
            "workstream_scope": "FG_METAL_PROJECT" if scoped_path else "OTHER_PROJECT_PROTECTED",
            "category": container_category,
            "classification_note": "Container summary only; apply contained-path classifications individually.",
            "deletability": "NEVER_DELETE_CONTAINER_AS_A_UNIT; protect checkout/worktree and out-of-scope roots",
        })
        for item in dir_acc.values():
            item.pop("_inodes")
            category, deletability = classification(Path(item["path"]), root_kind)
            item.update({"root": str(root), "category": category,
                         "owner_experiment": owner_for(Path(item["path"]), root),
                         "deletability_classification": deletability})
            large_directories.append(item)

    duplicate_groups = [
        {"sha256": digest, "path_count": len(paths), "paths": paths}
        for digest, paths in binary_hashes.items() if len(paths) > 1
    ]
    duplicate_groups.sort(key=lambda group: group["path_count"], reverse=True)
    large_paths.sort(key=lambda row: (row["allocated_size_bytes"], row["path"]), reverse=True)
    large_directories.sort(key=lambda row: (row["allocated_size_bytes_unique_within_directory"], row["path"]), reverse=True)

    statvfs = os.statvfs(REPO)
    free = statvfs.f_bavail * statvfs.f_frsize
    def subtree_usage(root: Path) -> dict[str, int]:
        logical = allocated = unique = files = 0
        seen: set[tuple[int, int]] = set()
        if not root.exists() or root.is_symlink():
            return {"logical_bytes": 0, "unique_inode_allocated_bytes": 0, "file_count": 0}
        for base, dirs, names in os.walk(root, followlinks=False):
            dirs[:] = [name for name in dirs if not (Path(base) / name).is_symlink()]
            for name in names:
                path = Path(base) / name
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                files += 1
                logical += info.st_size
                key = (info.st_dev, info.st_ino)
                if key not in seen:
                    seen.add(key)
                    unique += getattr(info, "st_blocks", 0) * 512
                    allocated += getattr(info, "st_blocks", 0) * 512
        return {"logical_bytes": logical, "unique_inode_allocated_bytes": unique, "file_count": files}

    worktree_listing = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=REPO,
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False,
    ).stdout
    worktree_count = sum(line.startswith("worktree ") for line in worktree_listing.splitlines())
    try:
        import build_retention
        build_retention_status = build_retention.status_snapshot()
        import storage_policy
        prefix_count = (len(storage_policy.preserved_prefixes())
                        + len(storage_policy.uncertified_prefixes()))
        uncertified_prefix_count = len(storage_policy.uncertified_prefixes())
        active_prefix_lease_markers = len(storage_policy._active_lease_markers())
        prefix_state_fingerprint_sha256 = storage_policy.prefix_state_fingerprint().get("sha256")
    except Exception as error:
        build_retention_status = {"gate_status": "BLOCKED", "gate_blockers": [f"retention inventory failed: {error!r}"],
                                  "unknown": [{"reason": "retention controller unavailable"}]}
        prefix_count = uncertified_prefix_count = active_prefix_lease_markers = None
        prefix_state_fingerprint_sha256 = None

    artifact_store_usage = subtree_usage(HOME / "Library/Caches/FGMetal/artifacts/sha256")
    evidence_usage = subtree_usage(REPO / "experiments/storage")
    payload = {
        "schema_version": 2,
        "generated_at_unix": time.time(),
        "filesystem": {
            "mount_path": str(REPO), "free_bytes": free,
            "total_bytes": statvfs.f_blocks * statvfs.f_frsize,
            "warning_bytes": 35 * 1024 ** 3,
            "hard_stop_bytes": 25 * 1024 ** 3,
            "emergency_bytes": 15 * 1024 ** 3,
            "gate_minimum_bytes": 45 * 1024 ** 3,
            "preferred_free_bytes": 60 * 1024 ** 3,
            "allocation_note": ("Root and directory allocations deduplicate hard links by device/inode. APFS clone/shared extents are not identified; "
                                "unlinked open read-only cache images are conservatively represented by vnode-length upper bounds; "
                                "post-cleanup df is authoritative."),
            "project_scope_note": "FG-Metal project budget excludes DXMT/FrameGen path components; these workstreams remain inventoried and protected from deletion.",
            "path_alias_note": "/tmp resolves to /private/tmp; only /private/tmp was scanned.",
        },
        "totals": {
            "logical_bytes_across_paths": global_logical,
            "allocated_bytes_unique_inode_estimate": global_allocated,
            "allocated_bytes_unique_inode_estimate_including_unlinked_mount_upper_bound": (
                global_allocated + unlinked_image_upper_bound),
            "file_count_across_paths": global_path_files,
            "unique_inode_count": global_unique_inodes,
            "fgmetal_scoped_observed_allocated_inode_bytes": fgmetal_scoped_allocated - unlinked_image_upper_bound,
            "fgmetal_scoped_unlinked_mount_backing_upper_bound_bytes": unlinked_image_upper_bound,
            "unaccounted_unlinked_mounted_backing_images": unaccounted_unlinked_images,
            "fgmetal_scoped_allocated_inode_bytes": fgmetal_scoped_allocated,
            "other_project_protected_allocated_inode_bytes": other_project_allocated,
            "large_objects_by_category": dict(sorted(Counter(
                row["category"] for row in large_paths).items())),
            "large_directories_by_category": dict(sorted(Counter(
                row["category"] for row in large_directories).items())),
            "binary_hash_count": len(binary_hashes),
            "binary_duplicate_hash_count": len(duplicate_groups),
            "binary_duplicate_path_count": sum(group["path_count"] for group in duplicate_groups),
        },
        "storage_gate_metrics": {
            "worktree_count": worktree_count,
            "active_build_count": build_retention_status.get("active_build_count", 0),
            "preserved_build_count": build_retention_status.get("preserved_build_count", 0),
            "active_build_lease_markers": len(build_retention_status.get("active", [])),
            "build_retention_fingerprint_sha256": build_retention_status.get("retention_fingerprint", {}).get("sha256"),
            "prefix_state_fingerprint_sha256": prefix_state_fingerprint_sha256,
            "active_prefix_lease_markers": active_prefix_lease_markers,
            "prefix_count": prefix_count,
            "uncertified_prefix_count": uncertified_prefix_count,
            "artifact_store_size": artifact_store_usage,
            "evidence_size": evidence_usage,
            "build_retention": build_retention_status,
        },
        "roots": all_roots,
        "mounted_filesystems": list(mounted_filesystems.values()),
        "large_directories": large_directories,
        "large_objects": large_paths,
        "duplicate_binary_hash_groups": duplicate_groups,
    }
    temporary = OUT.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, OUT)
    print(json.dumps({"inventory": str(OUT), "roots": len(all_roots),
                      "large_objects": len(large_paths), "duplicate_binary_hash_groups": len(duplicate_groups),
                      "logical_bytes": global_logical, "allocated_inode_deduplicated_bytes": global_allocated,
                      "fgmetal_scoped_allocated_inode_bytes": fgmetal_scoped_allocated,
                      "unlinked_mounted_image_upper_bound_bytes": unlinked_image_upper_bound,
                      "free_bytes": free, "worktree_count": worktree_count,
                      "active_build_count": build_retention_status.get("active_build_count"),
                      "preserved_build_count": build_retention_status.get("preserved_build_count"),
                      "artifact_store_size": artifact_store_usage,
                      "evidence_size": evidence_usage}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
