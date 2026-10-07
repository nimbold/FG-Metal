#!/usr/bin/env python3
"""Replace certified redundant evidence DLL bytes with verified CAS references.

Only immutable, inventory-certified historical evidence is eligible. The CAS
payload stays a regular file; references do not share its inode. No source,
runtime, prefix, log, or manifest is retired. Darwin atomic exchange retains
the old entry until both destination identity and digest are checked.
"""
from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import json
import os
import stat
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import build_retention as br
import storage_policy as sp
from make_cleanup_plan import REQUIRED_ARTIFACT_PATHS

HERE = Path(__file__).resolve().parent
PLAN = HERE / "g0c-evidence-consolidation-plan.json"
RECEIPT = HERE / "g0c-evidence-consolidation-receipt.json"
PLAN_SIGNATURE = "plan_hmac_sha256"
RECEIPT_SIGNATURE = "receipt_hmac_sha256"
DLL_NAMES = {"d3d11.dll", "dxgi.dll", "d3d9.dll"}
BLOCKER_HASHES = {
    "531c71f02bc85b19b9788b7b5525a904779e4b0371cd47a5690ac676d97daded",
    "69f248484ab1fc9f6156e9a2a3b4ff9659a3484c65027e091a7b439eb382344f",
}
OPERATION = "PRESERVE_PATH_AND_SHA256_AS_CANONICAL_REFERENCE_ZERO_COPY"


def approved_roots() -> list[Path]:
    return [root / "experiments" / name / "evidence"
            for root in br._registered_worktree_roots()
            for name in ("dxvk_macos_step11dR", "dxvk_macos_step11d",
                         "dxvk_macos_step11b", "dxvk_macos_step11b1")]


def validate_path(path: Path, roots: list[Path]) -> None:
    if (not path.is_absolute() or str(path) != os.path.abspath(path)
            or not any(root in path.parents for root in roots)
            or path.name not in DLL_NAMES
            or path in REQUIRED_ARTIFACT_PATHS.values()):
        raise br.RetentionError(f"not an approved redundant evidence DLL: {path}")
    fd = br._open_dir(path.parent)
    os.close(fd)


def _xattr_signature(source: int) -> list[dict[str, str]]:
    library = ctypes.CDLL(None, use_errno=True)
    list_attrs = getattr(library, "flistxattr", None)
    get_attr = getattr(library, "fgetxattr", None)
    if list_attrs is None or get_attr is None:
        raise br.RetentionError("filesystem metadata APIs cannot verify extended attributes")
    list_attrs.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int)
    list_attrs.restype = ctypes.c_ssize_t
    get_attr.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p,
                         ctypes.c_size_t, ctypes.c_uint32, ctypes.c_int)
    get_attr.restype = ctypes.c_ssize_t
    size = list_attrs(source, None, 0, 0)
    if size < 0:
        error = ctypes.get_errno()
        if error in {errno.ENOTSUP, getattr(errno, "EOPNOTSUPP", errno.ENOTSUP)}:
            return []
        raise br.RetentionError(f"cannot list file extended attributes: {os.strerror(error)}")
    names_buffer = ctypes.create_string_buffer(max(size, 1))
    actual = list_attrs(source, names_buffer, size, 0)
    if actual < 0:
        error = ctypes.get_errno()
        raise br.RetentionError(f"cannot read file extended attributes: {os.strerror(error)}")
    names = [name.decode("utf-8", "surrogateescape")
             for name in names_buffer.raw[:actual].split(b"\0") if name]
    result = []
    for name in sorted(names):
        raw_name = os.fsencode(name)
        value_size = get_attr(source, raw_name, None, 0, 0, 0)
        if value_size < 0:
            error = ctypes.get_errno()
            raise br.RetentionError(f"cannot size extended attribute {name}: {os.strerror(error)}")
        value = ctypes.create_string_buffer(max(value_size, 1))
        actual_size = get_attr(source, raw_name, value, value_size, 0, 0)
        if actual_size < 0:
            error = ctypes.get_errno()
            raise br.RetentionError(f"cannot read extended attribute {name}: {os.strerror(error)}")
        result.append({"name": name,
                       "sha256": hashlib.sha256(value.raw[:actual_size]).hexdigest()})
    return result


def identity(info: os.stat_result, source: int) -> dict[str, Any]:
    """Capture filesystem identity and all metadata the symlink replacement drops."""
    return {"device": info.st_dev, "inode": info.st_ino, "size": info.st_size,
            "mtime_ns": info.st_mtime_ns, "mode": info.st_mode,
            "uid": info.st_uid, "gid": info.st_gid,
            "flags": getattr(info, "st_flags", 0),
            "xattrs": _xattr_signature(source)}


def _entry_identity(info: os.stat_result) -> dict[str, int]:
    return {"device": info.st_dev, "inode": info.st_ino, "mode": info.st_mode}


def _identity_at(parent_fd: int, name: str) -> dict[str, Any]:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise br.RetentionError(f"expected a regular file: {name}")
        return identity(info, descriptor)
    finally:
        os.close(descriptor)


def canonical_record(digest: str) -> tuple[Path, dict[str, Any]]:
    root_fd, group_fd = br._open_artifact_group(digest)
    canonical_fd: int | None = None
    try:
        raw = br._read_at(group_fd, "manifest.json", limit=1024 * 1024)
        manifest = json.loads(raw)
        canonical = Path(manifest.get("canonical_path", ""))
        if (manifest.get("sha256") != digest or manifest.get("schema_version") not in {1, 2}
                or canonical.parent != br.ARTIFACT_ROOT / digest):
            raise br.RetentionError("canonical manifest reference is invalid")
        verified = br._verify_artifact_payload_at(
            group_fd, canonical.name, digest, br._artifact_size(manifest),
            allow_legacy_hardlinks=manifest["schema_version"] == 1)
        canonical_fd = os.open(canonical.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                               dir_fd=group_fd)
        info = os.fstat(canonical_fd)
        if (info.st_dev, info.st_ino, info.st_size) != (
                verified.st_dev, verified.st_ino, verified.st_size):
            raise br.RetentionError("canonical payload identity changed while opening")
        paths = manifest.get("original_paths")
        if not isinstance(paths, list) or any(not isinstance(item, str) for item in paths):
            raise br.RetentionError("canonical manifest original_paths is malformed")
        return canonical, {"identity": identity(info, canonical_fd),
                           "link_count": info.st_nlink,
                           "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                           "original_paths": paths}
    finally:
        if canonical_fd is not None:
            os.close(canonical_fd)
        os.close(group_fd)
        os.close(root_fd)


def _inventory_row_for_path(inventory: dict[str, Any], path: str) -> dict[str, Any]:
    matches = [row for row in inventory.get("large_objects", [])
               if isinstance(row, dict) and row.get("path") == path]
    if len(matches) != 1:
        raise br.RetentionError(f"inventory does not contain exactly one certified row: {path}")
    return matches[0]


def _validate_inventory_row(row: dict[str, Any], inventory: dict[str, Any]) -> None:
    path = row.get("path")
    if not isinstance(path, str):
        raise br.RetentionError("plan row has no evidence path")
    certified = _inventory_row_for_path(inventory, path)
    original = row.get("original_identity")
    if (certified.get("deletability_classification") != "PRESERVE_OR_HARDLINK_DEDUP"
            or certified.get("sha256") != row.get("sha256")
            or certified.get("logical_size_bytes") != original.get("size")
            or certified.get("filesystem_device") != original.get("device")
            or certified.get("inode") != original.get("inode")):
        raise br.RetentionError(f"inventory no longer certifies this evidence row: {path}")


def _validate_cas_binding(row: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    canonical, current = canonical_record(row["sha256"])
    if (str(canonical) != row.get("canonical_path")
            or current["manifest_sha256"] != row.get("canonical", {}).get("manifest_sha256")
            or row["path"] not in current["original_paths"]):
        raise br.RetentionError(f"CAS manifest no longer certifies evidence path: {row.get('path')}")
    expected = row.get("canonical", {}).get("identity")
    if current["identity"] != expected:
        raise br.RetentionError(f"canonical payload identity changed since plan: {row['sha256']}")
    return canonical, current


def _plan_rows(inventory: dict[str, Any], roots: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in inventory["large_objects"]:
        path = Path(row["path"])
        if (row["deletability_classification"] != "PRESERVE_OR_HARDLINK_DEDUP"
                or path.name not in {"d3d11.dll", "dxgi.dll", "d3d9.dll"}
                or row["logical_size_bytes"] < 100 * sp.MIB
                or path in REQUIRED_ARTIFACT_PATHS.values()):
            continue
        validate_path(path, roots)
        parent_fd = br._open_dir(path.parent)
        descriptor: int | None = None
        try:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                 dir_fd=parent_fd)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or (info.st_dev, info.st_ino, info.st_size) !=
                    (row["filesystem_device"], row["inode"], row["logical_size_bytes"])):
                raise br.RetentionError(f"inventory identity changed: {path}")
            digest = br._hash_fd(descriptor)
            original_identity = identity(info, descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)
        if digest != row["sha256"]:
            raise br.RetentionError(f"evidence digest changed: {path}")
        canonical, record = canonical_record(digest)
        if str(path) not in record["original_paths"]:
            raise br.RetentionError(f"CAS manifest does not certify original evidence path: {path}")
        rows.append({"path": str(path), "sha256": digest,
                     "canonical_path": str(canonical), "original_identity": original_identity,
                     "original_link_count": info.st_nlink,
                     "canonical": {key: value for key, value in record.items()
                                   if key != "original_paths"},
                     "certification": row["deletability_classification"]})

    clusters: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        old = row["original_identity"]
        clusters[(old["device"], old["inode"])].append(row)
    selected: list[dict[str, Any]] = []
    for key, cluster in clusters.items():
        representative = cluster[0]
        canonical_info = representative["canonical"]["identity"]
        shares_canonical = key == (canonical_info["device"], canonical_info["inode"])
        expected_count = len(cluster) + int(shares_canonical)
        if any(item["original_link_count"] != expected_count for item in cluster):
            continue
        if ((not shares_canonical and len(cluster) == representative["original_link_count"])
                or (shares_canonical and representative["sha256"] in BLOCKER_HASHES
                    and len(cluster) + 1 == representative["original_link_count"])):
            selected.extend(cluster)
    return sorted(selected, key=lambda item: item["path"])


def plan() -> dict[str, Any]:
    raw = sp._read_file_nofollow(sp.INVENTORY_PATH)
    inventory = json.loads(raw)
    roots = approved_roots()
    result = {"schema_version": 2, "generated_at_unix": time.time(),
              "inventory_sha256": hashlib.sha256(raw).hexdigest(),
              "approved_roots": list(map(str, roots)), "operation": OPERATION,
              "rows": _plan_rows(inventory, roots)}
    sealed = sp._seal_storage_evidence(result, PLAN_SIGNATURE)
    sp._write_json(PLAN, sealed, strict_directory_sync=True)
    return sealed


def validate_reviewed_plan(reviewed: dict[str, Any], raw_plan: bytes,
                           inventory: dict[str, Any], raw_inventory: bytes,
                           roots: list[Path]) -> list[list[dict[str, Any]]]:
    if not sp._verify_storage_evidence(reviewed, PLAN_SIGNATURE):
        raise br.RetentionError("evidence consolidation plan signature is missing or invalid")
    if (reviewed.get("schema_version") != 2 or reviewed.get("operation") != OPERATION
            or reviewed.get("approved_roots") != list(map(str, roots))
            or reviewed.get("inventory_sha256") != hashlib.sha256(raw_inventory).hexdigest()):
        raise br.RetentionError("plan contract, roots, or certified inventory changed")
    rows = reviewed.get("rows")
    if not isinstance(rows, list) or not rows:
        raise br.RetentionError("signed evidence consolidation plan has no rows")
    seen_paths: set[str] = set()
    for row in rows:
        path = row.get("path") if isinstance(row, dict) else None
        if not isinstance(path, str) or path in seen_paths:
            raise br.RetentionError(f"duplicate or invalid plan path: {path}")
        seen_paths.add(path)
    groups: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if not isinstance(row, dict):
            raise br.RetentionError("plan contains a malformed row")
        path = row.get("path")
        if (row.get("certification") != "PRESERVE_OR_HARDLINK_DEDUP"
                or not isinstance(row.get("sha256"), str)
                or len(row["sha256"]) != 64):
            raise br.RetentionError(f"plan row lacks the required evidence certification: {path}")
        validate_path(Path(path), roots)
        _validate_inventory_row(row, inventory)
        _validate_cas_binding(row)
        old = row.get("original_identity")
        if (not isinstance(old, dict) or not isinstance(old.get("device"), int)
                or not isinstance(old.get("inode"), int)
                or not isinstance(row.get("original_link_count"), int)):
            raise br.RetentionError(f"plan row has malformed inode identity: {path}")
        groups[(old["device"], old["inode"])].append(row)
    return [groups[key] for key in sorted(groups)]


def _reference_at(parent_fd: int, name: str, canonical: str,
                  expected_identity: dict[str, int] | None = None) -> bool:
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    if not stat.S_ISLNK(info.st_mode):
        return False
    if expected_identity is not None and _entry_identity(info) != expected_identity:
        return False
    return os.readlink(name, dir_fd=parent_fd) == canonical


def _open_original(parent_fd: int, name: str, expected: dict[str, Any], digest: str) -> int:
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                         dir_fd=parent_fd)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or identity(info, descriptor) != expected
                or br._hash_fd(descriptor) != digest):
            raise br.RetentionError(f"original evidence entry no longer matches its receipt: {name}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def recover_transaction(tx: dict[str, Any], persist: Callable[[dict[str, Any]], None]) -> str:
    """Restore the original name after interruption, or prove reference completion."""
    path = Path(tx["path"])
    parent_fd = br._open_dir(path.parent)
    original_fd: int | None = None
    try:
        canonical, current = _validate_cas_binding(tx["row"])
        if (str(canonical) != tx["canonical_path"]
                or current["identity"] != tx["canonical_identity"]):
            raise br.RetentionError("cannot recover against a changed canonical payload")
        source_name, temporary = path.name, tx["temporary"]
        source_ref = _reference_at(parent_fd, source_name, tx["canonical_path"],
                                   tx.get("temporary_identity"))
        try:
            temp_info = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            temp_info = None
        temp_ref = (temp_info is not None and stat.S_ISLNK(temp_info.st_mode)
                    and _reference_at(parent_fd, temporary, tx["canonical_path"],
                                      tx.get("temporary_identity")))

        if not source_ref:
            try:
                original_fd = _open_original(parent_fd, source_name,
                                             tx["original_identity"], tx["sha256"])
            except FileNotFoundError as error:
                raise br.RetentionError("recovery found neither the original nor a verified reference") from error
            if temp_info is None:
                tx["phase"] = "ROLLED_BACK"
                persist(tx)
                return "ROLLED_BACK"
            if not temp_ref:
                raise br.RetentionError("recovery found an unexpected temporary entry")
            if tx.get("temporary_identity") is not None and _entry_identity(temp_info) != tx["temporary_identity"]:
                raise br.RetentionError("temporary reference inode differs from the durable intent")
            os.unlink(temporary, dir_fd=parent_fd)
            os.fsync(parent_fd)
            tx["phase"] = "ROLLED_BACK"
            persist(tx)
            return "ROLLED_BACK"

        if temp_info is None:
            if tx.get("phase") not in {"DELETE_INTENT", "DELETED", "COMPLETE"}:
                raise br.RetentionError("reference has no displaced original and no durable delete intent")
            if not _reference_at(parent_fd, source_name, tx["canonical_path"],
                                 tx.get("temporary_identity")):
                raise br.RetentionError("installed canonical reference identity cannot be proven")
            tx["phase"] = "COMPLETE"
            persist(tx)
            return "COMPLETE"

        if not stat.S_ISREG(temp_info.st_mode):
            raise br.RetentionError("recovery found a non-regular displaced entry")
        original_fd = _open_original(parent_fd, temporary,
                                     tx["original_identity"], tx["sha256"])
        if tx.get("phase") not in {"SWAP_INTENT", "SWAPPED", "DELETE_INTENT"}:
            raise br.RetentionError("unexpected swapped entry without durable swap intent")
        tx["phase"] = "RESTORE_INTENT"
        persist(tx)
        br._rename_swap_at(parent_fd, source_name, temporary)
        tx["phase"] = "RESTORED"
        persist(tx)
        if _identity_at(parent_fd, source_name) != tx["original_identity"]:
            raise br.RetentionError("recovery did not restore the original evidence identity")
        if not _reference_at(parent_fd, temporary, tx["canonical_path"],
                             tx.get("temporary_identity")):
            raise br.RetentionError("recovery did not retain the expected temporary reference")
        os.unlink(temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
        tx["phase"] = "ROLLED_BACK"
        persist(tx)
        return "ROLLED_BACK"
    finally:
        if original_fd is not None:
            os.close(original_fd)
        os.close(parent_fd)


def replace_reference(row: dict[str, Any], roots: list[Path],
                      persist: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    """Install one reference with a durable intent before every irreversible step."""
    if persist is None:
        persist = lambda _tx: None
    path = Path(row["path"])
    validate_path(path, roots)
    canonical, before = _validate_cas_binding(row)
    parent_fd = br._open_dir(path.parent)
    source_fd: int | None = None
    tx: dict[str, Any] | None = None
    try:
        source_fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                            dir_fd=parent_fd)
        original = os.fstat(source_fd)
        original_link_count_before = original.st_nlink
        if (not stat.S_ISREG(original.st_mode)
                or identity(original, source_fd) != row["original_identity"]):
            raise br.RetentionError("evidence identity or metadata changed before consolidation")
        if br._hash_fd(source_fd) != row["sha256"]:
            raise br.RetentionError("evidence hash changed before consolidation")
        temporary = f".{path.name}.cas-reference-{uuid.uuid4().hex}"
        tx = {"schema_version": 1, "operation_id": uuid.uuid4().hex,
              "phase": "PREPARED", "path": str(path), "temporary": temporary,
              "sha256": row["sha256"], "canonical_path": str(canonical),
              "canonical_identity": before["identity"],
              "original_identity": row["original_identity"],
              "original_link_count_before": original_link_count_before, "row": row}
        persist(tx)
        os.symlink(str(canonical), temporary, dir_fd=parent_fd)
        os.fsync(parent_fd)
        link_info = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        tx["temporary_identity"] = _entry_identity(link_info)
        tx["phase"] = "TEMP_READY"
        persist(tx)

        if _identity_at(parent_fd, path.name) != row["original_identity"]:
            raise br.RetentionError("evidence name replaced before atomic exchange")
        if br._hash_fd(source_fd) != row["sha256"]:
            raise br.RetentionError("evidence bytes changed before atomic exchange")
        tx["phase"] = "SWAP_INTENT"
        persist(tx)
        br._rename_swap_at(parent_fd, path.name, temporary)
        tx["phase"] = "SWAPPED"
        persist(tx)

        installed = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        if (not _reference_at(parent_fd, path.name, str(canonical), tx["temporary_identity"])
                or (installed.st_dev, installed.st_ino) !=
                (link_info.st_dev, link_info.st_ino)):
            raise br.RetentionError("installed canonical reference changed")
        canonical_after, after = _validate_cas_binding(row)
        if canonical_after != canonical or after["identity"] != before["identity"]:
            raise br.RetentionError("canonical payload changed before old alias unlink")
        displaced_fd = _open_original(parent_fd, temporary,
                                      row["original_identity"], row["sha256"])
        os.close(displaced_fd)
        tx["phase"] = "DELETE_INTENT"
        persist(tx)
        displaced = os.stat(temporary, dir_fd=parent_fd, follow_symlinks=False)
        br._unlink_pinned_regular_at(parent_fd, temporary, displaced)
        tx["phase"] = "DELETED"
        persist(tx)
        canonical_after, after = _validate_cas_binding(row)
        if canonical_after != canonical or after["identity"] != before["identity"]:
            raise br.RetentionError("canonical payload changed after alias unlink")
        tx["phase"] = "COMPLETE"
        persist(tx)
        remaining_links = os.fstat(source_fd).st_nlink
        return {**row, "operation_id": tx["operation_id"],
                "result": "REFERENCE_INSTALLED_HASH_VERIFIED", "canonical_after": after,
                "copy_bytes": 0,
                "original_inode_link_count_before": original_link_count_before,
                "original_inode_link_count_after": remaining_links,
                "completed_at_unix": time.time()}
    except BaseException:
        if tx is not None:
            # Leave a signed, recoverable intent if restoration itself fails.
            recover_transaction(tx, persist)
        raise
    finally:
        if source_fd is not None:
            os.close(source_fd)
        os.close(parent_fd)


def validate_group_closure(cluster: list[dict[str, Any]], roots: list[Path]) -> list[dict[str, Any]]:
    """Prove every current link to the original inode is enumerated by this group."""
    first = cluster[0]
    original = first["original_identity"]
    old_key = (original["device"], original["inode"])
    if any((item["original_identity"]["device"], item["original_identity"]["inode"])
           != old_key or item["sha256"] != first["sha256"]
           for item in cluster):
        raise br.RetentionError("inode group contains inconsistent planned identities")
    canonical, current = _validate_cas_binding(first)
    canonical_info = current["identity"]
    shares_canonical = (canonical_info["device"], canonical_info["inode"]) == old_key
    planned_count = len(cluster) + int(shares_canonical)
    if any(item["original_link_count"] != planned_count for item in cluster):
        raise br.RetentionError("plan does not enumerate the complete original hardlink group")

    regular_entries: list[tuple[int, str]] = []
    try:
        for row in cluster:
            path = Path(row["path"])
            validate_path(path, roots)
            parent_fd = br._open_dir(path.parent)
            descriptor: int | None = None
            try:
                try:
                    entry = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                except FileNotFoundError as error:
                    raise br.RetentionError(f"planned evidence path disappeared: {path}") from error
                if stat.S_ISLNK(entry.st_mode):
                    if not _reference_at(parent_fd, path.name, str(canonical)):
                        raise br.RetentionError(f"planned path is an unexpected symlink: {path}")
                elif stat.S_ISREG(entry.st_mode):
                    descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                         dir_fd=parent_fd)
                    info = os.fstat(descriptor)
                    if (identity(info, descriptor) != row["original_identity"]
                            or br._hash_fd(descriptor) != row["sha256"]):
                        raise br.RetentionError(f"live evidence inode differs from signed plan: {path}")
                    regular_entries.append((descriptor, str(path)))
                    descriptor = None
                else:
                    raise br.RetentionError(f"planned path is not a regular file or reference: {path}")
            finally:
                if descriptor is not None:
                    os.close(descriptor)
                os.close(parent_fd)

        expected_live_links = len(regular_entries) + int(shares_canonical)
        if regular_entries:
            if any(os.fstat(fd).st_nlink != expected_live_links for fd, _ in regular_entries):
                raise br.RetentionError("live hardlink count does not match the enumerated inode group")
        elif shares_canonical:
            if current["link_count"] != 1:
                raise br.RetentionError("completed CAS reference leaves unexpected canonical hardlinks")
        # For a separate evidence inode, all row paths are either live regular
        # aliases or already-consolidated references. An empty set is complete.
        if regular_entries and not shares_canonical and any(
                os.fstat(fd).st_nlink != len(regular_entries) for fd, _ in regular_entries):
            raise br.RetentionError("separate evidence inode has an unlisted hardlink")
    finally:
        for descriptor, _ in regular_entries:
            os.close(descriptor)
    return cluster


def _save_receipt(receipt: dict[str, Any]) -> None:
    sp._write_json(RECEIPT, sp._seal_storage_evidence(receipt, RECEIPT_SIGNATURE),
                   strict_directory_sync=True)


def _read_optional_receipt() -> dict[str, Any] | None:
    try:
        raw = sp._read_file_nofollow(RECEIPT, maximum_bytes=16 * 1024 * 1024)
    except FileNotFoundError:
        return None
    value = json.loads(raw)
    if not sp._verify_storage_evidence(value, RECEIPT_SIGNATURE):
        raise br.RetentionError("existing consolidation receipt signature is missing or invalid")
    return value


def execute() -> dict[str, Any]:
    raw_plan = sp._read_file_nofollow(PLAN, maximum_bytes=16 * 1024 * 1024)
    reviewed = json.loads(raw_plan)
    raw_inventory = sp._read_file_nofollow(sp.INVENTORY_PATH)
    inventory = json.loads(raw_inventory)
    roots = approved_roots()
    groups = validate_reviewed_plan(reviewed, raw_plan, inventory, raw_inventory, roots)
    plan_hash = hashlib.sha256(raw_plan).hexdigest()
    receipt: dict[str, Any] | None = None
    with sp._acquire_storage_allocation_lock():
        lease_fd, lock_fd = br._lock()
        try:
            prior = _read_optional_receipt()
            if prior is not None and prior.get("active_operation") is not None:
                if prior.get("plan_sha256") != plan_hash:
                    raise br.RetentionError("pending transaction belongs to a different signed plan")
                # FAIL can be written after a failed recovery attempt. Preserve
                # and retry its durable intent instead of abandoning a temp name.
                receipt = prior
                receipt["status"] = "IN_PROGRESS"
                receipt.pop("error", None)
            else:
                receipt = {"schema_version": 2, "started_at_unix": time.time(),
                           "status": "IN_PROGRESS", "plan_sha256": plan_hash,
                           "copy_bytes": 0, "free_before": sp.disk_free_bytes(sp.REPO),
                           "project_before": sp.measure_project_usage(), "actions": [],
                           "active_operation": None}
                _save_receipt(receipt)

            def persist(tx: dict[str, Any]) -> None:
                assert receipt is not None
                receipt["active_operation"] = dict(tx)
                _save_receipt(receipt)

            active = receipt.get("active_operation")
            if active is not None:
                outcome = recover_transaction(active, persist)
                if outcome == "COMPLETE":
                    receipt["actions"].append({**active["row"],
                        "operation_id": active["operation_id"],
                        "result": "RECOVERED_COMPLETED_REFERENCE",
                        "copy_bytes": 0,
                        "original_inode_link_count_before": active.get("original_link_count_before"),
                        "original_inode_link_count_after": (
                            canonical_record(active["row"]["sha256"])[1]["link_count"]
                            if (active["canonical_identity"]["device"],
                                active["canonical_identity"]["inode"])
                               == (active["original_identity"]["device"],
                                   active["original_identity"]["inode"]) else 0),
                        "completed_at_unix": time.time()})
                receipt["active_operation"] = None
                _save_receipt(receipt)

            for group in groups:
                validate_group_closure(group, roots)
                for row in group:
                    path = Path(row["path"])
                    parent_fd = br._open_dir(path.parent)
                    try:
                        info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
                        if stat.S_ISLNK(info.st_mode):
                            continue
                    finally:
                        os.close(parent_fd)

                    def persist_row(tx: dict[str, Any]) -> None:
                        persist(tx)

                    action = replace_reference(row, roots, persist_row)
                    receipt["actions"].append(action)
                    receipt["active_operation"] = None
                    _save_receipt(receipt)
            receipt["status"] = "PASS"
        except BaseException as error:
            if receipt is not None:
                receipt["status"] = "FAIL"
                receipt["error"] = repr(error)
            raise
        finally:
            br._unlock(lease_fd, lock_fd)
            if receipt is not None:
                receipt.update(completed_at_unix=time.time(),
                               free_after=sp.disk_free_bytes(sp.REPO),
                               project_after=sp.measure_project_usage())
                _save_receipt(receipt)
    assert receipt is not None
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "execute"))
    args = parser.parse_args()
    result = plan() if args.command == "plan" else execute()
    print(json.dumps({"status": result.get("status", "PLANNED"),
                      "rows": len(result.get("rows", result.get("actions", []))),
                      "copy_bytes": 0}, indent=2))
